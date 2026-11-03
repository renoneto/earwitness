"""BY1: one billing webhook (design 8.1-8.4; billing R2-02).

Real HostedApp HTTP, registered workspaces with their own SQLite files,
locally signed events with a throwaway secret, FixtureSubscriptionProtocol
or the real bounded helper against local fixture servers. The audit hook
from test_billing_helper_deadline refuses every other socket connect.

Rules under test:
  * One route, POST /api/v1/billing/webhook; the per-workspace route
    answers the fixed 404.
  * Order of work: billing off 404; bounded body, signature, parse (400);
    the completed-delivery journal (a duplicate is answered from it with
    no provider call and no workspace open); the per-subscription lock
    (at most 1,000 entries, then 503) with the journal re-checked after
    it; the bounded provider read behind two non-waiting slots (503 when
    full, a slot freed only when its thread ends); the workspace commit,
    then the completion; crash recovery from the workspace journal.
  * Routing through seat_for_subscription: no seat 200 with no change; a
    conflict (another workspace or another price) 404 with nothing written.
  * BY2: the seat is a `purchase_seats` row (billing.PurchaseSeats), not
    the event's binding. The fixtures give each scripted subscription a
    `bound` seat on its workspace, and each price an offer.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
import unittest
from unittest import mock

from earwitness.app import billing as _billing
from earwitness.app import connections as _conn
from tests import backend_fixtures
from tests.test_backend_authority import HostedReaderHttpTest
from tests.test_billing_helper_deadline import (Guarded, Server, _reply,
                                                client_context, need_tls)

SECRET = "whsec_by1_throwaway"
PRICES = {"price_a": {"amount_cents": 9900, "offer": "ew-a"},
          "price_b": {"amount_cents": 19900, "offer": "ew-b"}}
ENV = {"STRIPE_SECRET_KEY": "sk_fixture", "STRIPE_WEBHOOK_SECRET": SECRET,
       "EARWITNESS_BILLING_SUCCESS_URL": "/billing/success",
       "EARWITNESS_BILLING_CANCEL_URL": "/billing/cancel",
       "EARWITNESS_BILLING_PRICES": json.dumps(PRICES)}
ROUTE = "/api/v1/billing/webhook"


def sign(raw: bytes, *, secret=SECRET, ts=None) -> str:
    ts = int(time.time()) if ts is None else ts
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + raw,
                      hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


async def asgi_call(app, path, *, method="POST", raw=b"", headers=()):
    hdrs = [(b"host", b"127.0.0.1:8771"),
            (b"content-length", str(len(raw)).encode()), *headers]
    scope = {"type": "http", "method": method, "path": path, "scheme": "http",
             "headers": hdrs, "query_string": b""}
    sent = []

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent
                    if m["type"] == "http.response.body")
    return start["status"], body


async def asgi_stream(app, path, chunks, *, declared=None, headers=()):
    """Send `chunks` one receive() at a time (more_body on all but the
    last). A receive() after the last chunk fails the test. Returns
    (status, body, receive calls)."""
    hdrs = [(b"host", b"127.0.0.1:8771"), *headers]
    if declared is not None:
        hdrs.append((b"content-length", str(declared).encode()))
    scope = {"type": "http", "method": "POST", "path": path, "scheme": "http",
             "headers": hdrs, "query_string": b""}
    sent, calls = [], []

    async def receive():
        calls.append(len(calls))
        if len(calls) > len(chunks):
            raise AssertionError("receive() called after the last chunk")
        n = len(calls) - 1
        return {"type": "http.request", "body": chunks[n],
                "more_body": n < len(chunks) - 1}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent
                    if m["type"] == "http.response.body")
    return start["status"], body, len(calls)


async def wait_until(predicate, what, timeout=5.0):
    """Waits for a condition the test needs; a regression fails here in
    seconds instead of hanging the suite."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.005)


def sub_event(eid, sub, customer, identity, etype="customer.subscription.updated"):
    obj = {"id": sub, "object": "subscription", "customer": customer}
    if identity is not None:
        obj["metadata"] = {"workspace_id": identity}
    return {"id": eid, "type": etype, "livemode": True, "api_version":
            _billing.STRIPE_VERSION, "data": {"object": obj}}


def invoice_event(eid, sub, customer, identity, etype="invoice.paid"):
    details = {"subscription": sub}
    if identity is not None:
        details["metadata"] = {"workspace_id": identity}
    return {"id": eid, "type": etype, "livemode": True,
            "api_version": _billing.STRIPE_VERSION,
            "data": {"object": {"id": "in_" + eid, "object": "invoice",
                                "customer": customer,
                                "parent": {"type": "subscription_details",
                                           "subscription_details": details}}}}


def resource(sub, customer, identity, *, status="active", price="price_a",
             period=None, cancel_at=0):
    return {"id": sub, "customer": customer, "status": status,
            "price_id": price,
            "current_period_end": period or int(time.time()) + 30 * 86400,
            "trial_end": 0, "cancel_at_period_end": False,
            "cancel_at": cancel_at, "metadata_workspace": identity}


def add_seat(store, sub, workspace, state="bound", offer="ew-a"):
    """A purchase_seats row as the window leaves it (the BY2 seam)."""
    from earwitness.app import purchases as _purchases
    con = store.auth_connection()
    try:
        _purchases.ensure_schema(con)
        con.execute("INSERT INTO purchase_seats (subscription_id, offer,"
                    " customer_id, principal, email, workspace_id, state,"
                    " run_key, state_at) VALUES (?, ?, 'cus', ?, ?, ?, ?,"
                    " 'r', '2026-10-21T00:00:00+00:00')",
                    (sub, offer, "oidc:x:" + sub, sub + "@example.com",
                     workspace, state))
        con.commit()
    finally:
        con.close()


class _WebhookBase(Guarded):
    WORKSPACES = ("bravo", "charlie")

    def setUp(self):
        super().setUp()
        self.http = HostedReaderHttpTest()
        self.http.setUp()
        self.addCleanup(self.http.tearDown)
        self.app = self.http.app
        self.paths = {"acme": self.http.db_path}
        for name in self.WORKSPACES:
            self.paths[name] = str(backend_fixtures.make_workspace(
                os.path.join(self.http.data_dir, name), schema_version=8))
            _conn.register_workspace(self.app.store, name, self.paths[name],
                                     self.http.data_dir)
        self.ids = {name: self._identity(name) for name in self.paths}
        env = mock.patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        self.provider = _billing.FixtureSubscriptionProtocol()
        self.app.billing_provider = self.provider

    def _identity(self, name):
        entry = _conn.resolve_workspace(self.app.store, name)
        return f"{name}:{entry['db_identity']}"

    def script(self, sub, customer, workspace, **kw):
        self.provider._resources[sub] = resource(sub, customer,
                                                 self.ids[workspace], **kw)
        if not self.seat_row(sub):
            self.seat(sub, workspace)

    def seat(self, sub, workspace, state="bound", offer="ew-a"):
        add_seat(self.app.store, sub, workspace, state, offer)

    def seat_row(self, sub):
        from earwitness.app import purchases as _purchases
        con = self.app.store.auth_connection()
        try:
            return _purchases.seat(con, sub)
        finally:
            con.close()

    async def adeliver(self, event, *, path=ROUTE, signature=None):
        raw = event if isinstance(event, bytes) else json.dumps(event).encode()
        sig = sign(raw) if signature is None else signature
        return await asgi_call(self.app, path, raw=raw,
                               headers=[(b"stripe-signature", sig.encode())])

    def deliver(self, event, **kw):
        return asyncio.run(self.adeliver(event, **kw))

    def dump(self, workspace):
        con = sqlite3.connect(self.paths[workspace])
        try:
            return "\n".join(con.iterdump())
        finally:
            con.close()

    def query(self, workspace, sql, args=()):
        con = sqlite3.connect(self.paths[workspace])
        try:
            return con.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            con.close()

    def completions(self):
        con = self.app.store.auth_connection()
        try:
            return [tuple(r) for r in con.execute(
                "SELECT event_id, status_code FROM billing_deliveries"
                " ORDER BY event_id")]
        except sqlite3.OperationalError:
            return []
        finally:
            con.close()

    def entitlement(self, workspace):
        rows = self.query(workspace, "SELECT status FROM billing_entitlement")
        return rows[0][0] if rows else None


class RouteTest(_WebhookBase):
    def test_billing_off_single_route_is_the_fixed_404(self):
        os.environ.pop("STRIPE_WEBHOOK_SECRET")
        status, body = self.deliver(sub_event("evt_off", "sub_1", "cus_1",
                                              self.ids["acme"]))
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["message"], "unknown object")
        self.assertEqual(self.provider.calls, [])

    def test_per_workspace_route_is_the_fixed_404_even_when_signed(self):
        self.script("sub_1", "cus_1", "acme")
        event = sub_event("evt_pw", "sub_1", "cus_1", self.ids["acme"])
        before = self.dump("acme")
        for path in ("/api/v1/billing/acme/webhook",
                     "/api/v1/billing/no-such-co/webhook"):
            status, body = self.deliver(event, path=path)
            self.assertEqual(status, 404, path)
            self.assertEqual(json.loads(body)["error"]["message"], "unknown object")
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.dump("acme"), before)

    def test_invalid_deliveries_get_the_fixed_400(self):
        event = sub_event("evt_bad", "sub_1", "cus_1", self.ids["acme"])
        raw = json.dumps(event).encode()
        big = json.dumps({**event, "pad": "x" * (64 * 1024)}).encode()
        cases = {"no signature": (raw, ""),
                 "wrong secret": (raw, sign(raw, secret="whsec_other")),
                 "stale": (raw, sign(raw, ts=int(time.time()) - 3600)),
                 "over 64 KiB, validly signed": (big, sign(big)),
                 "not json": (b"nope", sign(b"nope"))}
        bodies = set()
        for name, (payload, sig) in cases.items():
            with self.subTest(name):
                status, body = self.deliver(payload, signature=sig)
                self.assertEqual(status, 400, name)
                bodies.add(body)
        self.assertEqual(len(bodies), 1)
        self.assertEqual(self.provider.calls, [])

    def test_body_bound_is_64_kib_at_the_http_entry(self):
        # Exactly 64 KiB is read and worked; one byte more is the fixed 400.
        self.script("sub_1", "cus_1", "acme")
        event = sub_event("evt_edge", "sub_1", "cus_1", self.ids["acme"])
        base = json.dumps({**event, "pad": ""}).encode()
        pad = 64 * 1024 - len(base)
        exact = json.dumps({**event, "pad": "x" * pad}).encode()
        self.assertEqual(len(exact), 64 * 1024)
        status, body = self.deliver(exact)
        self.assertEqual(status, 200, body)
        over = json.dumps({**event, "id": "evt_edge2",
                           "pad": "x" * (pad + 1)}).encode()
        status, body, calls = asyncio.run(asgi_stream(
            self.app, ROUTE, [over],
            headers=[(b"stripe-signature", sign(over).encode())]))
        self.assertEqual((status, json.loads(body)["error"]["message"]),
                         (400, "invalid webhook delivery"))
        self.assertEqual(calls, 1)
        self.assertEqual(self.provider.calls, ["sub_1"])


class EarlyBodyTest(_WebhookBase):
    """Design 8.4 at the real HTTP entry: billing off and the retired
    route refuse before any body read; the first reader of the webhook
    body enforces 64 KiB and answers the fixed invalid-delivery 400."""

    FIXED_400 = (400, "invalid webhook delivery")

    def stream(self, chunks, *, path=ROUTE, declared=None):
        status, body, calls = asyncio.run(asgi_stream(
            self.app, path, chunks, declared=declared,
            headers=[(b"stripe-signature", b"t=1,v1=00")]))
        return status, json.loads(body)["error"]["message"], calls

    def test_billing_off_refuses_before_any_body_read(self):
        os.environ.pop("STRIPE_WEBHOOK_SECRET")
        for path in (ROUTE, "/api/v1/billing/acme/webhook"):
            for declared in (1, 2_000_001, None):
                with self.subTest(path=path, declared=declared):
                    got = self.stream([], path=path, declared=declared)
                    self.assertEqual(got, (404, "unknown object", 0))
        self.assertEqual(self.provider.calls, [])

    def test_retired_route_refuses_before_any_body_read_when_billing_is_on(self):
        for path in ("/api/v1/billing/acme/webhook",
                     "/api/v1/billing/no-such-co/webhook"):
            for declared in (10, 2_000_001):
                with self.subTest(path=path, declared=declared):
                    got = self.stream([], path=path, declared=declared)
                    self.assertEqual(got, (404, "unknown object", 0))

    def test_declared_oversize_is_the_fixed_400_without_a_read(self):
        for declared in (64 * 1024 + 1, 2_000_000, 2_000_001, 50_000_000):
            with self.subTest(declared=declared):
                got = self.stream([], declared=declared)
                self.assertEqual(got, (*self.FIXED_400, 0))
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.completions(), [])

    def test_streamed_oversize_stops_at_the_chunk_that_crosses_64_kib(self):
        cases = {
            "one chunk over, more to come": [b"x" * (64 * 1024 + 1), b"y"],
            "second chunk crosses": [b"x" * (40 * 1024), b"x" * (40 * 1024), b"y"],
            "far past the generic 2 MB limit": [b"x" * (3 * 1024 * 1024), b"y"],
        }
        for name, chunks in cases.items():
            with self.subTest(name):
                status, message, calls = self.stream(chunks)
                self.assertEqual((status, message), self.FIXED_400)
                self.assertEqual(calls, len(chunks) - 1,
                                 "read on after crossing 64 KiB")
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.completions(), [])
        self.assertEqual(self.app.body_budget.buffered, 0)

    def test_only_the_four_event_types_are_handled(self):
        self.assertEqual(set(_billing.WEBHOOK_EVENT_TYPES), {
            "customer.subscription.updated", "customer.subscription.deleted",
            "invoice.paid", "invoice.payment_failed"})
        before = self.dump("acme")
        for etype in ("customer.created", "checkout.session.completed",
                      "customer.subscription.created"):
            with self.subTest(etype):
                event = sub_event("evt_" + etype, "sub_1", "cus_1",
                                  self.ids["acme"], etype=etype)
                status, body = self.deliver(event)
                self.assertEqual(status, 200, body)
                self.assertEqual(json.loads(body)["status"], "ignored")
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.dump("acme"), before)


class RoutingTest(_WebhookBase):
    def test_subscription_event_applies_to_its_seat(self):
        self.script("sub_1", "cus_1", "acme")
        status, body = self.deliver(sub_event("evt_1", "sub_1", "cus_1",
                                              self.ids["acme"]))
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["status"], "reconciled")
        self.assertEqual(self.entitlement("acme"), "active")
        self.assertIsNone(self.entitlement("bravo"))
        self.assertEqual(self.completions(), [("evt_1", 200)])

    def test_invoice_uses_the_pinned_parent_subscription_field(self):
        self.assertEqual(_billing.INVOICE_SUBSCRIPTION_PATH,
                         ("parent", "subscription_details", "subscription"))
        self.script("sub_1", "cus_1", "bravo")
        status, body = self.deliver(invoice_event("evt_inv", "sub_1", "cus_1",
                                                  self.ids["bravo"]))
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["status"], "reconciled")
        self.assertEqual(self.entitlement("bravo"), "active")
        self.assertEqual(self.provider.calls, ["sub_1"])
        # A pre-basil invoice (top-level 'subscription') names no
        # subscription under the pinned version: ignored, nothing read.
        legacy = {"id": "evt_legacy", "type": "invoice.paid", "livemode": True,
                  "data": {"object": {"id": "in_x", "customer": "cus_1",
                                      "subscription": "sub_1"}}}
        status, body = self.deliver(legacy)
        self.assertEqual((status, json.loads(body)["status"]), (200, "ignored"))
        self.assertEqual(self.provider.calls, ["sub_1"])

    def workspace(self, name):
        """Register one more workspace (a seat holds one for life)."""
        if name not in self.paths:
            self.paths[name] = str(backend_fixtures.make_workspace(
                os.path.join(self.http.data_dir, name), schema_version=8))
            _conn.register_workspace(self.app.store, name, self.paths[name],
                                     self.http.data_dir)
            self.ids[name] = self._identity(name)
        return name

    def test_no_seat_cases_are_200_with_no_change_and_no_provider_call(self):
        # BY2 (design 8.2 step 2): no seat, a refused row (NULL
        # workspace), and every state outside bound, active and
        # awaiting_account complete as no_seat, even on a registered
        # workspace whose identity the resource names. BY3 r9: needs_refund
        # is no longer a webhook state (a webhook must never re-allow it).
        cases = [("no seat", None), ("refused", "refused"),
                 ("reserved", "reserved"), ("recorded", "recorded"),
                 ("needs_refund", "needs_refund"),
                 ("ended", "ended"), ("retired", "retired"),
                 ("ended, no workspace", "ended")]
        for n, (name, state) in enumerate(cases):
            self.workspace(f"ns-{n}")
        before = {name: self.dump(name) for name in self.paths}
        for n, (name, state) in enumerate(cases):
            with self.subTest(name):
                sub, ws = f"sub_ns{n}", f"ns-{n}"
                self.provider._resources[sub] = resource(sub, "cus_1", self.ids[ws])
                if state is not None:
                    held = None if state == "refused" or name.endswith("no workspace") else ws
                    self.seat(sub, held, state=state)
                status, body = self.deliver(sub_event(f"evt_ns{n}", sub, "cus_1",
                                                      self.ids[ws]))
                self.assertEqual(status, 200, body)
                self.assertEqual(json.loads(body)["status"], "no_seat")
        self.assertEqual(self.provider.calls, [])
        self.assertEqual({name: self.dump(name) for name in self.paths}, before)

    def test_each_workspace_state_takes_the_workspace_path(self):
        for n, state in enumerate(("bound", "active", "awaiting_account")):
            with self.subTest(state):
                workspace = self.workspace(f"st-{n}")
                sub = f"sub_st{n}"
                self.seat(sub, workspace, state=state)
                self.script(sub, "cus_st", workspace)
                status, body = self.deliver(sub_event(f"evt_st{n}", sub, "cus_st",
                                                      self.ids[workspace]))
                self.assertEqual(status, 200, body)
                self.assertEqual(json.loads(body)["status"], "reconciled")
                self.assertEqual(self.entitlement(workspace), "active")

    def test_a_bound_binding_without_a_seat_is_no_seat(self):
        # The pre-launch binding-backed workspaces (judges' demo, owner) are
        # comped, not seats: their deliveries no longer touch the workspace.
        self.provider._resources["sub_c"] = resource("sub_c", "cus_c", self.ids["acme"])
        before = self.dump("acme")
        status, body = self.deliver(sub_event("evt_c", "sub_c", "cus_c", self.ids["acme"]))
        self.assertEqual((status, json.loads(body)["status"]), (200, "no_seat"))
        self.assertEqual(self.dump("acme"), before)
        self.assertEqual(self.provider.calls, [])

    def test_a_seat_on_an_unregistered_workspace_is_no_seat(self):
        self.seat("sub_u", "gone-ws")
        self.provider._resources["sub_u"] = resource("sub_u", "cus_u", "gone-ws:ws-x")
        status, body = self.deliver(sub_event("evt_u", "sub_u", "cus_u", "gone-ws:ws-x"))
        self.assertEqual((status, json.loads(body)["status"]), (200, "no_seat"))
        self.assertEqual(self.provider.calls, [])

    def test_seat_seam_returns_workspace_and_offer(self):
        self.script("sub_1", "cus_1", "acme")
        seats = _billing.PurchaseSeats(self.app.store)
        self.assertEqual(seats.seat_for_subscription("sub_1"), ("acme", "ew-a"))
        self.assertIsNone(seats.seat_for_subscription("sub_other"))
        self.assertIsNone(seats.seat_for_subscription(""))

    def test_conflicting_identities_in_the_event_are_404_nothing_written(self):
        event = sub_event("evt_mix", "sub_1", "cus_1", self.ids["acme"])
        event["data"]["object"]["client_reference_id"] = self.ids["bravo"]
        before = {name: self.dump(name) for name in self.paths}
        status, body = self.deliver(event)
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["message"], "unknown object")
        self.assertEqual({name: self.dump(name) for name in self.paths}, before)
        self.assertEqual(self.completions(), [])

    def test_event_naming_another_workspace_than_the_seat_is_404_nothing_written(self):
        # The seat and the Stripe resource say bravo; the event names acme.
        self.script("sub_1", "cus_1", "bravo")
        before = {name: self.dump(name) for name in self.paths}
        status, _ = self.deliver(sub_event("evt_x", "sub_1", "cus_1",
                                           self.ids["acme"]))
        self.assertEqual(status, 404)
        self.assertEqual({name: self.dump(name) for name in self.paths}, before)
        self.assertEqual(self.completions(), [])

    def test_resource_naming_another_workspace_than_seat_and_event_is_404(self):
        # The seat and the event agree on acme; only the Stripe resource's
        # metadata names bravo. The provider identity check alone refuses.
        self.seat("sub_1", "acme")
        self.provider._resources["sub_1"] = resource("sub_1", "cus_1", self.ids["bravo"])
        before = {name: self.dump(name) for name in self.paths}
        status, body = self.deliver(sub_event("evt_x", "sub_1", "cus_1",
                                              self.ids["acme"]))
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["message"], "unknown object")
        self.assertEqual({name: self.dump(name) for name in self.paths}, before)
        self.assertEqual(self.completions(), [])
        self.assertEqual(self.seat_row("sub_1")["workspace_id"], "acme")

    def test_resource_for_another_price_is_404_nothing_written(self):
        self.script("sub_1", "cus_1", "acme")
        self.deliver(sub_event("evt_1", "sub_1", "cus_1", self.ids["acme"]))
        self.script("sub_1", "cus_1", "acme", price="price_b")
        before = self.dump("acme")
        status, _ = self.deliver(sub_event("evt_2", "sub_1", "cus_1",
                                           self.ids["acme"]))
        self.assertEqual(status, 404)
        self.assertEqual(self.dump("acme"), before)
        self.assertEqual(self.completions(), [("evt_1", 200)])

    def test_gone_counts_as_canceled_only_for_a_verified_binding(self):
        self.provider._failures["sub_gone"] = _billing.ProviderNotFound("gone")
        self.seat("sub_gone", "bravo")
        before = self.dump("bravo")
        status, _ = self.deliver(sub_event(
            "evt_g0", "sub_gone", "cus_9", self.ids["bravo"],
            etype="customer.subscription.deleted"))
        self.assertEqual(status, 404)
        self.assertEqual(self.dump("bravo"), before)
        self.script("sub_1", "cus_1", "acme")
        self.deliver(sub_event("evt_1", "sub_1", "cus_1", self.ids["acme"]))
        self.provider._failures["sub_1"] = _billing.ProviderNotFound("gone")
        status, body = self.deliver(sub_event(
            "evt_g1", "sub_1", "cus_1", self.ids["acme"],
            etype="customer.subscription.deleted"))
        self.assertEqual(status, 200, body)
        self.assertEqual(self.entitlement("acme"), "canceled")

    def test_out_of_order_updated_after_deleted_keeps_todays_state(self):
        ident = self.ids["acme"]
        self.script("sub_1", "cus_1", "acme", status="canceled")
        deleted = sub_event("evt_del", "sub_1", "cus_1", ident,
                            etype="customer.subscription.deleted")
        updated = sub_event("evt_upd_old", "sub_1", "cus_1", ident)
        self.assertEqual(self.deliver(deleted)[0], 200)
        self.assertEqual(self.deliver(updated)[0], 200)
        self.assertEqual(self.entitlement("acme"), "canceled")
        # And the other order: an old 'deleted' after a newer resubscribe
        # state cannot exist (one subscription per workspace), but an old
        # 'updated' then 'deleted' still lands on the current resource.
        self.script("sub_2", "cus_2", "bravo", status="active")
        self.deliver(sub_event("evt_d2", "sub_2", "cus_2", self.ids["bravo"],
                               etype="customer.subscription.deleted"))
        self.assertEqual(self.entitlement("bravo"), "active")


class OrderOfWorkTest(_WebhookBase):
    def test_duplicate_after_completion_is_identical_with_zero_provider_calls(self):
        self.script("sub_1", "cus_1", "acme")
        event = sub_event("evt_dup", "sub_1", "cus_1", self.ids["acme"])
        first = self.deliver(event)
        self.assertEqual(first[0], 200)
        calls = list(self.provider.calls)
        with mock.patch.object(_billing, "open_seat_db",
                               side_effect=AssertionError("opened")) as seat, \
                mock.patch.object(_conn, "resolve_workspace",
                                  side_effect=AssertionError("resolved")) as reg:
            second = self.deliver(event)
        seat.assert_not_called()
        reg.assert_not_called()
        self.assertEqual(second, first)
        self.assertEqual(self.provider.calls, calls)

    def test_duplicate_waiting_on_the_lock_is_answered_from_the_journal(self):
        self.script("sub_1", "cus_1", "acme")
        block = threading.Event()
        self.provider.block = block
        event = sub_event("evt_wait", "sub_1", "cus_1", self.ids["acme"])

        async def scenario():
            first = asyncio.ensure_future(self.adeliver(event))
            await wait_until(lambda: self.provider.calls, "the provider call")
            second = asyncio.ensure_future(self.adeliver(event))
            await asyncio.sleep(0.05)
            self.assertFalse(second.done(), "the duplicate did not wait")
            block.set()
            return await first, await second

        a, b = asyncio.run(scenario())
        self.assertEqual(a, b)
        self.assertEqual(a[0], 200)
        self.assertEqual(self.provider.calls, ["sub_1"])

    def test_crash_between_workspace_commit_and_completion_recovers(self):
        self.script("sub_1", "cus_1", "acme")
        event = sub_event("evt_crash", "sub_1", "cus_1", self.ids["acme"])
        real = _billing.complete_delivery
        with mock.patch.object(_billing, "complete_delivery",
                               side_effect=RuntimeError("power cut")):
            status, _ = self.deliver(event)
        self.assertNotEqual(status, 200)
        self.assertEqual(self.query("acme", "SELECT status FROM billing_events"
                                    " WHERE event_id='evt_crash'"),
                         [("reconciled",)])
        self.assertEqual(self.completions(), [])
        self.assertIs(_billing.complete_delivery, real)
        retry = self.deliver(event)
        self.assertEqual(retry[0], 200)
        self.assertEqual(json.loads(retry[1])["status"], "reconciled")
        self.assertEqual(self.provider.calls, ["sub_1"], "retry read Stripe again")
        self.assertEqual(self.completions(), [("evt_crash", 200)])
        self.assertEqual(self.deliver(event), retry)

    def test_workspace_journal_row_and_reconcile_are_one_transaction(self):
        # Design 8.4 step 6: a failure inside the reconcile leaves neither
        # the billing_events row nor any billing write behind.
        self.script("sub_1", "cus_1", "acme")
        before = self.dump("acme")
        with mock.patch.object(_billing, "_write_verified",
                               side_effect=RuntimeError("disk full")):
            status, _ = self.deliver(sub_event("evt_tx", "sub_1", "cus_1",
                                               self.ids["acme"]))
        self.assertEqual(status, 500)
        self.assertEqual(self.dump("acme"), before)
        self.assertEqual(self.completions(), [])

    def test_crash_before_the_workspace_commit_leaves_no_completion(self):
        # The completion is written only after the workspace commit.
        self.script("sub_1", "cus_1", "acme")
        before = self.dump("acme")
        with mock.patch.object(_billing, "_journal",
                               side_effect=RuntimeError("power cut")):
            self.deliver(sub_event("evt_pre", "sub_1", "cus_1", self.ids["acme"]))
        self.assertEqual(self.completions(), [])
        self.assertEqual(self.dump("acme"), before)

    def test_lock_table_is_capped_then_503(self):
        self.assertEqual(_billing.SubscriptionLocks().max_entries, 1000)
        self.app.billing_locks = _billing.SubscriptionLocks(max_entries=1)
        self.script("sub_1", "cus_1", "acme")
        self.script("sub_2", "cus_2", "bravo")
        block = threading.Event()
        self.provider.block = block
        before = self.dump("bravo")

        async def scenario():
            first = asyncio.ensure_future(self.adeliver(
                sub_event("evt_l1", "sub_1", "cus_1", self.ids["acme"])))
            await wait_until(lambda: self.provider.calls, "the provider call")
            second = await self.adeliver(
                sub_event("evt_l2", "sub_2", "cus_2", self.ids["bravo"]))
            block.set()
            return await first, second

        first, second = asyncio.run(scenario())
        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 503)
        self.assertEqual(self.dump("bravo"), before)
        self.assertEqual(len(self.app.billing_locks), 0)

    def test_provider_failure_is_503_and_nothing_completed(self):
        self.seat("sub_1", "acme")
        self.provider._failures["sub_1"] = _billing.ProviderError("down")
        before = self.dump("acme")
        status, _ = self.deliver(sub_event("evt_f", "sub_1", "cus_1",
                                           self.ids["acme"]))
        self.assertEqual(status, 503)
        self.assertEqual(self.dump("acme"), before)
        self.assertEqual(self.completions(), [])


    def test_completed_duplicate_is_answered_before_the_lock(self):
        # Design 8.4 step 3: a completed delivery is answered from the
        # journal at once, even while another delivery for the same
        # subscription holds the lock (code review L1).
        self.script("sub_1", "cus_1", "acme")
        done = sub_event("evt_a", "sub_1", "cus_1", self.ids["acme"])
        first = self.deliver(done)
        self.assertEqual(first[0], 200)
        block = threading.Event()
        self.provider.block = block
        calls = len(self.provider.calls)

        async def scenario():
            other = asyncio.ensure_future(self.adeliver(sub_event(
                "evt_b", "sub_1", "cus_1", self.ids["acme"])))
            deadline = time.monotonic() + 5.0
            while len(self.provider.calls) == calls:
                if time.monotonic() > deadline:
                    block.set()
                    self.fail("the second delivery never reached the provider")
                await asyncio.sleep(0.01)
            try:
                again = await asyncio.wait_for(self.adeliver(done), 2.0)
            except TimeoutError:
                again = None
            holders = self.app.billing_locks._entries["sub_1"][1]
            still_blocked = not other.done()
            block.set()
            return again, holders, still_blocked, await other

        again, holders, blocked, other = asyncio.run(scenario())
        self.assertTrue(blocked)
        self.assertEqual(again, first, "the duplicate waited on the lock")
        self.assertEqual(holders, 1)
        self.assertEqual(other[0], 200)
        self.assertEqual(len(self.app.billing_locks), 0)


class TransientSeatFaultTest(_WebhookBase):
    """A transient fault while reading the seat is a retryable 503 that
    writes and completes nothing (code review L3)."""

    def faulty_open(self, needle):
        from earwitness.app import db as _db
        real = _db.open_existing
        hits = []

        def opener(path, *, readonly):
            con = real(path, readonly=readonly)
            execute = con.execute

            def faulty(sql, *args):
                if needle in sql:
                    hits.append(sql)
                    raise sqlite3.OperationalError("disk I/O error")
                return execute(sql, *args)

            con.execute = faulty
            return con

        return mock.patch.object(_db, "open_existing", opener), hits

    @contextlib.contextmanager
    def faulty_seat_read(self):
        """BY2: the seat read on the control store fails transiently."""
        from earwitness.app import purchases as _purchases
        hits = []

        def boom(con, subscription_id):
            hits.append(subscription_id)
            raise sqlite3.OperationalError("disk I/O error")

        with mock.patch.object(_purchases, "seat", boom):
            yield hits

    def test_seat_read_error_is_503_not_a_no_seat_completion(self):
        self.script("sub_1", "cus_1", "acme")
        self.assertEqual(self.deliver(sub_event(
            "evt_1", "sub_1", "cus_1", self.ids["acme"]))[0], 200)
        self.script("sub_1", "cus_1", "acme", price="price_b")
        before = self.dump("acme")
        with self.faulty_seat_read() as hits:
            status, _ = self.deliver(sub_event("evt_2", "sub_1", "cus_1",
                                               self.ids["acme"]))
        self.assertTrue(hits)
        self.assertEqual(status, 503)
        self.assertEqual(self.dump("acme"), before)
        self.assertEqual(self.completions(), [("evt_1", 200)])
        self.assertEqual(self.provider.calls, ["sub_1"])

    def test_transient_workspace_fault_is_503_not_a_no_seat_completion(self):
        self.script("sub_1", "cus_1", "acme")
        before = self.dump("acme")
        patch, hits = self.faulty_open("FROM meta WHERE key='workspace_id'")
        with patch:
            status, body = self.deliver(sub_event("evt_t", "sub_1", "cus_1",
                                                  self.ids["acme"]))
        self.assertTrue(hits)
        self.assertEqual(status, 503, body)
        self.assertEqual(self.dump("acme"), before)
        self.assertEqual(self.completions(), [])

    def test_deleted_delivery_survives_a_transient_fault_and_applies_on_retry(self):
        self.script("sub_1", "cus_1", "acme")
        self.assertEqual(self.deliver(sub_event(
            "evt_1", "sub_1", "cus_1", self.ids["acme"]))[0], 200)
        self.script("sub_1", "cus_1", "acme", status="canceled")
        deleted = sub_event("evt_del", "sub_1", "cus_1", self.ids["acme"],
                            etype="customer.subscription.deleted")
        for needle in ("FROM meta WHERE key='workspace_id'", "seat read"):
            with self.subTest(needle):
                patch, hits = self.faulty_open(needle) if needle != "seat read" \
                    else (self.faulty_seat_read(), None)
                with patch as seen:
                    hits = hits if hits is not None else seen
                    status, _ = self.deliver(deleted)
                self.assertTrue(hits)
                self.assertEqual(status, 503)
                self.assertEqual(self.entitlement("acme"), "active")
                self.assertEqual(self.completions(), [("evt_1", 200)])
        status, body = self.deliver(deleted)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["status"], "reconciled")
        self.assertEqual(self.entitlement("acme"), "canceled")
        self.assertEqual(self.completions(), [("evt_1", 200), ("evt_del", 200)])


class SeatFaultBranchTest(_WebhookBase):
    """One test per fault branch of billing.open_seat_db (code review r2
    L1): each is a retryable 503 that writes and completes nothing, and the
    delivery (a subscription deleted) applies on retry."""

    def setUp(self):
        super().setUp()
        self.script("sub_1", "cus_1", "acme")
        self.assertEqual(self.deliver(sub_event(
            "evt_1", "sub_1", "cus_1", self.ids["acme"]))[0], 200)
        self.script("sub_1", "cus_1", "acme", status="canceled")
        self.deleted = sub_event("evt_del", "sub_1", "cus_1", self.ids["acme"],
                                 etype="customer.subscription.deleted")

    def assert_fault_then_retry(self, fault):
        before = self.dump("acme")
        calls = list(self.provider.calls)
        with fault as hits:
            status, body = self.deliver(self.deleted)
        self.assertTrue(hits, "the fault was never reached")
        self.assertEqual(status, 503, body)
        self.assertEqual(self.dump("acme"), before)
        self.assertEqual(self.entitlement("acme"), "active")
        self.assertEqual(self.completions(), [("evt_1", 200)])
        self.assertEqual(self.provider.calls, calls, "provider read on a fault")
        status, body = self.deliver(self.deleted)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["status"], "reconciled")
        self.assertEqual(self.entitlement("acme"), "canceled")
        self.assertEqual(self.completions(), [("evt_1", 200), ("evt_del", 200)])

    @contextlib.contextmanager
    def patched(self, target, name, error):
        hits = []

        def boom(*args, **kwargs):
            hits.append(name)
            raise error

        with mock.patch.object(target, name, boom):
            yield hits

    def test_registry_read_error_is_503_and_retries(self):
        self.assert_fault_then_retry(self.patched(
            _conn, "resolve_workspace",
            sqlite3.OperationalError("database is locked")))

    def test_failed_workspace_open_is_503_and_retries(self):
        from earwitness.app import db as _db
        self.assert_fault_then_retry(self.patched(
            _db, "open_existing", OSError(24, "Too many open files")))

    def test_non_404_registry_error_is_503_and_retries(self):
        control = self.app.store.auth_connection()
        try:
            control.execute("UPDATE workspace_registry SET"
                            " row_namespace='misconfigured'"
                            " WHERE workspace_id='acme'")
            control.commit()
        finally:
            control.close()

        @contextlib.contextmanager
        def misconfigured():
            yield [True]
            con = self.app.store.auth_connection()
            try:
                con.execute("UPDATE workspace_registry SET row_namespace='local'"
                            " WHERE workspace_id='acme'")
                con.commit()
            finally:
                con.close()

        self.assert_fault_then_retry(misconfigured())


class RetryLogTest(_WebhookBase):
    """Every retryable webhook 503 logs one warning with a fixed reason
    code, the event id and the subscription id only (code review r2 L2)."""

    def deliver_logged(self, event):
        with self.assertLogs("earwitness.ops", level="WARNING") as cap:
            status, _ = self.deliver(event)
        self.assertEqual(status, 503)
        self.assertEqual(len(cap.records), 1, cap.output)
        text = cap.records[0].getMessage()
        record = json.loads(text)
        self.assertEqual(record["event"], "billing.webhook.retry")
        self.assertEqual(record["level"], "warning")
        self.assertEqual(
            set(record) - {"ts", "level", "event", "request_id", "job_id"},
            {"event_id", "subscription_id", "reason"})
        self.assertEqual(record["subscription_id"], "sub_1")
        for private in ("cus_1", SECRET, "price_a", self.ids["acme"]):
            self.assertNotIn(private, text)
        return record

    def test_period_unreadable(self):
        self.script("sub_1", "cus_1", "acme", period=-5)
        record = self.deliver_logged(sub_event(
            "evt_p", "sub_1", "cus_1", self.ids["acme"]))
        self.assertEqual((record["event_id"], record["reason"]),
                         ("evt_p", "period_unreadable"))

    def test_stripe_length_ids_stay_readable(self):
        # Astra BY1-R3-01: real ids (evt_/sub_ + 24 characters) are longer
        # than the generic token-run redaction; the warning must keep them.
        for eid, sid, ws in (("evt_1PqR2sT3uV4wX5yZ6aB7cD8e", "sub_1PqR2sT3uV4wX5yZ6aB7cD8f", "acme"),
                             ("evt_9ZyXwVuTsRqPoNmLkJiHgFeD", "sub_9ZyXwVuTsRqPoNmLkJiHgFeE", "bravo")):
            with self.subTest(eid):
                self.script(sid, "cus_1", ws, period=-5)
                with self.assertLogs("earwitness.ops", level="WARNING") as cap:
                    status, _ = self.deliver(sub_event(
                        eid, sid, "cus_1", self.ids[ws]))
                self.assertEqual(status, 503)
                record = json.loads(cap.records[0].getMessage())
                self.assertEqual((record["event_id"], record["subscription_id"],
                                  record["reason"]),
                                 (eid, sid, "period_unreadable"))

    def test_seat_unavailable(self):
        self.script("sub_1", "cus_1", "acme")
        with mock.patch.object(_conn, "resolve_workspace",
                               side_effect=sqlite3.OperationalError("locked")):
            record = self.deliver_logged(sub_event(
                "evt_s", "sub_1", "cus_1", self.ids["acme"]))
        self.assertEqual((record["event_id"], record["reason"]),
                         ("evt_s", "seat_unavailable"))

    def test_provider_failed(self):
        self.seat("sub_1", "acme")
        self.provider._failures["sub_1"] = _billing.ProviderError("down")
        record = self.deliver_logged(sub_event(
            "evt_f", "sub_1", "cus_1", self.ids["acme"]))
        self.assertEqual(record["reason"], "provider_failed")

    def test_provider_busy(self):
        self.script("sub_1", "cus_1", "acme")
        self.app.billing_slots_used = self.app.billing_slots
        record = self.deliver_logged(sub_event(
            "evt_b", "sub_1", "cus_1", self.ids["acme"]))
        self.assertEqual(record["reason"], "provider_busy")

    def test_lock_table_full(self):
        self.script("sub_1", "cus_1", "acme")
        self.app.billing_locks = _billing.SubscriptionLocks(max_entries=0)
        record = self.deliver_logged(sub_event(
            "evt_l", "sub_1", "cus_1", self.ids["acme"]))
        self.assertEqual(record["reason"], "lock_table_full")


class SlotsTest(_WebhookBase):
    def test_slots_are_two(self):
        self.assertEqual(self.app.billing_slots, 2)

    def test_three_concurrent_deliveries_with_two_slots_one_503(self):
        block = threading.Event()
        self.provider.block = block
        names = ("acme", "bravo", "charlie")
        for n, name in enumerate(names):
            self.script(f"sub_{n}", f"cus_{n}", name)
        before = {name: self.dump(name) for name in names}

        async def scenario():
            tasks = [asyncio.ensure_future(self.adeliver(sub_event(
                f"evt_s{n}", f"sub_{n}", f"cus_{n}", self.ids[name])))
                for n, name in enumerate(names)]
            for _ in range(300):
                if sum(t.done() for t in tasks) == 1:
                    break
                await asyncio.sleep(0.01)
            block.set()
            return [await t for t in tasks]

        answers = asyncio.run(scenario())
        statuses = [a[0] for a in answers]
        self.assertEqual(sorted(statuses), [200, 200, 503], answers)
        busy = names[statuses.index(503)]
        self.assertEqual(self.dump(busy), before[busy], "the 503 wrote")
        self.assertNotIn(f"evt_s{names.index(busy)}",
                         [c[0] for c in self.completions()])
        self.assertEqual(len(self.provider.calls), 2)

    def test_blocked_provider_leaves_healthz_and_reads_responsive(self):
        self.script("sub_1", "cus_1", "acme")
        self.provider.delay = 2.0
        bearer = [(b"authorization",
                   ("Bearer " + self.http.reader_token).encode())]

        async def timed(coro):
            started = time.monotonic()
            result = await coro
            return result, time.monotonic() - started

        async def scenario():
            # Warm the read path first: its first call pays one-time module
            # imports, which are not what this test measures.
            await asgi_call(self.app, "/api/v1/brands", method="GET",
                            headers=bearer)
            # Timed from the delivery's start: a provider call that blocks the
            # event loop also stalls this wait, so timing only the later
            # requests would miss it.
            started = time.monotonic()
            delivery = asyncio.ensure_future(self.adeliver(sub_event(
                "evt_slow", "sub_1", "cus_1", self.ids["acme"])))
            await wait_until(lambda: self.provider.calls, "the provider call")
            health = (await asgi_call(self.app, "/healthz", method="GET"),
                      time.monotonic() - started)
            read = await timed(asgi_call(self.app, "/api/v1/brands",
                                         method="GET", headers=bearer))
            still_running = not delivery.done()
            return health, read, still_running, await delivery

        health, read, running, delivered = asyncio.run(scenario())
        self.assertTrue(running)
        # A blocked answer waits out the provider's 2 s delay; a bound of a
        # quarter of it catches that without measuring machine load (an idle
        # read alone measured up to 0.094 s on a busy host).
        bound = self.provider.delay / 4
        self.assertEqual(health[0][0], 200)
        self.assertLess(health[1], bound)
        self.assertEqual(read[0][0], 200, read[0][1][:300])
        self.assertLess(read[1], bound)
        self.assertEqual(delivered[0], 200)

    def test_cancelled_request_keeps_its_slot_while_the_thread_lives(self):
        self.script("sub_1", "cus_1", "acme")
        block = threading.Event()
        self.provider.block = block

        async def scenario():
            task = asyncio.ensure_future(self.adeliver(sub_event(
                "evt_cancel", "sub_1", "cus_1", self.ids["acme"])))
            await wait_until(lambda: self.provider.calls, "the provider call")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            held = self.app.billing_slots_used
            block.set()
            for _ in range(300):
                if self.app.billing_slots_used == 0:
                    break
                await asyncio.sleep(0.01)
            return held, self.app.billing_slots_used

        held, after = asyncio.run(scenario())
        self.assertEqual(held, 1, "the slot was freed while the thread lived")
        self.assertEqual(after, 0)
        self.assertEqual(self.completions(), [])


class RealHelperTest(_WebhookBase):
    """The default provider (StripeSubscriptionProtocol over the bounded
    helper) against local fixture servers and a resolver stub."""

    def setUp(self):
        super().setUp()
        self.app.billing_provider = None

    def test_over_size_reply_is_503_and_nothing_written(self):
        need_tls(self)
        server = Server(_reply(body=b"[" + b"0," * (140 * 1024) + b"0]"))
        self.addCleanup(server.close)
        self.seat("sub_1", "acme")
        before = self.dump("acme")
        # Prove the 503 comes from the size cap, not the 10 s deadline (a
        # loaded machine can hit the deadline first): spy on the helper and
        # give this test a long deadline.
        raised = []
        real = _billing.bounded_request

        def spy(*args, **kwargs):
            try:
                return real(*args, **kwargs)
            except Exception as exc:
                raised.append(type(exc))
                raise

        with mock.patch.object(_billing, "STRIPE_API", server.url), \
                mock.patch.object(_billing, "_tls_context", client_context), \
                mock.patch.object(_billing, "PROVIDER_DEADLINE_S", 60.0), \
                mock.patch.object(_billing, "bounded_request", spy):
            status, _ = self.deliver(sub_event("evt_big", "sub_1", "cus_1",
                                               self.ids["acme"]))
        self.assertEqual(status, 503)
        self.assertEqual(self.dump("acme"), before)
        self.assertEqual(raised, [_billing.ProviderTooLarge])
        self.assertEqual(self.completions(), [])
        # The fixture server records the request before it replies.
        self.assertEqual(len(server.requests), 1)

    def test_blocking_resolver_holds_slots_new_calls_503_threads_bounded(self):
        release = threading.Event()
        entered = []

        def stuck_resolve(host, port):
            entered.append(host)
            release.wait(10)
            raise OSError("resolver stub gave up")

        names = ("acme", "bravo", "charlie")
        for n, name in enumerate(names):
            self.seat(f"sub_{n}", name)

        async def scenario():
            tasks = [asyncio.ensure_future(self.adeliver(sub_event(
                f"evt_dns{n}", f"sub_{n}", f"cus_{n}", self.ids[name])))
                for n, name in enumerate(names[:2])]
            await wait_until(lambda: len(entered) >= 2, "both deliveries to enter")
            threads = threading.active_count()
            extra = []
            for k in range(5):
                extra.append(await self.adeliver(sub_event(
                    f"evt_more{k}", "sub_2", "cus_2", self.ids["charlie"])))
            grown = threading.active_count() - threads
            held = self.app.billing_slots_used
            release.set()
            first = [await t for t in tasks]
            return first, extra, grown, held

        with mock.patch.object(_billing, "_resolve", stuck_resolve):
            first, extra, grown, held = asyncio.run(scenario())
        self.assertEqual(held, 2)
        self.assertEqual([a[0] for a in extra], [503] * 5)
        self.assertLessEqual(grown, 0, "new calls started threads")
        self.assertEqual(len(entered), 2)
        self.assertEqual([a[0] for a in first], [503, 503])
        self.assertEqual(self.app.billing_slots_used, 0)
        self.assert_no_refused_connects()


if __name__ == "__main__":
    unittest.main()
