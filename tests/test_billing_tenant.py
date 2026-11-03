"""L24: billing binds deliveries to a globally unique workspace identity.

BY1 moved deliveries to the one route POST /api/v1/billing/webhook
(design 8.1): the workspace now comes from the billing identity the
signed event names, never from the URL, and the Stripe read must name the
same identity. BY2: the workspace is the subscription's purchase seat
(billing.PurchaseSeats); each test gives its subscriptions seats, and the
event's claim and the Stripe resource must both name the seat's
workspace. The L24 rules below still hold; "delivered to B" now means
"the event claims B".

Astra L8D-R1-03: hosted workspaces share the row namespace 'local', and
checkout stamped that namespace on the provider resources. A valid signed
event for workspace A's subscription delivered to /api/v1/billing/B/webhook
(misrouted, or replayed inside the signature window) could bind B to A's
customer, give B A's entitlement and A's billing portal. The same replay
told a real workspace id from a made-up one (200 versus 404).

Rules under test (two registered workspaces, two SQLite files, real
registry entries, real HostedApp HTTP, no network):
  * Checkout stamps the workspace's billing identity, '<external id>:<db
    identity>', never the shared namespace.
  * A's signed event delivered to B answers exactly the unknown-id 404 and
    leaves B's database byte-for-byte unchanged; delivered to A it binds.
  * Legacy 'local' (or missing) provider metadata is refused the same way.
  * A binding that no delivery verified for this identity gives no portal,
    no checkout or trial, and no entitlement.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import time
import unittest
from unittest import mock

from earwitness.app import billing as _billing
from earwitness.app import connections as _conn
from tests import backend_fixtures
from tests.test_backend_authority import HostedReaderHttpTest

SECRET = "whsec_tenant"
PRICES = {"price_pro": {"amount_cents": 100, "trial_days": 14, "offer": "ew-a"}}
ENV = {"STRIPE_SECRET_KEY": "sk_fixture", "STRIPE_WEBHOOK_SECRET": SECRET,
       "EARWITNESS_BILLING_SUCCESS_URL": "/billing/success",
       "EARWITNESS_BILLING_CANCEL_URL": "/billing/cancel",
       "EARWITNESS_BILLING_PRICES": json.dumps(PRICES),
       "EARWITNESS_BILLING_INAPP_CHECKOUT": "on",
       "EARWITNESS_BILLING_PORTAL_CONFIGURATION": "bpc_fixture"}
ROUTE = "/api/v1/billing/webhook"


class _Provider:
    """Fixture Stripe transport: scripted subscriptions, recorded calls."""

    def __init__(self):
        self.subs: dict = {}
        self.calls: list = []

    def add(self, sub, customer, workspace_meta, price="price_pro"):
        meta = {} if workspace_meta is None else {"workspace_id": workspace_meta}
        self.subs[sub] = {
            "id": sub, "customer": customer, "status": "active",
            "items": {"data": [{"price": {"id": price}}]},
            "current_period_end": int(time.time()) + 30 * 86400,
            "metadata": meta}

    def __call__(self, config, method, path, fields):
        self.calls.append((method, path, dict(fields)))
        if method == "GET" and path.startswith("/subscriptions/"):
            sid = path.rsplit("/", 1)[1]
            if sid not in self.subs:
                raise ValueError(f"stripe request failed: No such subscription: '{sid}'")
            return dict(self.subs[sid])
        if path == "/checkout/sessions":
            return {"id": "cs_1", "url": "https://checkout.invalid/s", "livemode": True}
        if path == "/billing_portal/sessions":
            return {"url": "https://portal.invalid/p", "livemode": True}
        raise AssertionError(f"unexpected provider call {method} {path}")


class BillingTenantTest(unittest.TestCase):
    def setUp(self):
        self.http = HostedReaderHttpTest()
        self.http.setUp()
        self.addCleanup(self.http.tearDown)
        self.app = self.http.app
        self.db_a = self.http.db_path
        self.db_b = str(backend_fixtures.make_workspace(
            os.path.join(self.http.data_dir, "ws2"), schema_version=8))
        _conn.register_workspace(self.app.store, "bravo", self.db_b,
                                 self.http.data_dir)
        self.id_a = self._identity("acme")
        self.id_b = self._identity("bravo")
        env = mock.patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        self.provider = _Provider()
        transport = mock.patch.object(_billing, "_stripe_request", self.provider)
        transport.start()
        self.addCleanup(transport.stop)

    def seat(self, sub, workspace):
        from tests.test_billing_webhook_single import add_seat
        add_seat(self.app.store, sub, workspace)

    def _identity(self, workspace):
        entry = _conn.resolve_workspace(self.app.store, workspace)
        return f"{workspace}:{entry['db_identity']}"

    # -- transport helpers -------------------------------------------------

    def _post_webhook(self, event):
        raw = event if isinstance(event, bytes) else json.dumps(event).encode()
        ts = int(time.time())
        digest = hmac.new(SECRET.encode(), f"{ts}.".encode() + raw,
                          hashlib.sha256).hexdigest()
        headers = [(b"host", b"127.0.0.1:8771"),
                   (b"content-length", str(len(raw)).encode()),
                   (b"stripe-signature", f"t={ts},v1={digest}".encode())]
        scope = {"type": "http", "method": "POST", "scheme": "http",
                 "path": ROUTE,
                 "headers": headers, "query_string": b""}
        sent = []

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}

        async def send(message):
            sent.append(message)

        asyncio.run(self.app(scope, receive, send))
        start = next(m for m in sent if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in sent
                        if m["type"] == "http.response.body")
        hdrs = sorted((k.decode().lower(), v.decode())
                      for k, v in start.get("headers", [])
                      if k.decode().lower() != "x-request-id")
        return start["status"], body, hdrs

    def _event(self, eid, sub="sub_a", customer="cus_a", etype="invoice.paid",
               claim=None, **obj):
        """An event in the pinned version's shape. `claim` is the billing
        identity it names (default: A's)."""
        claim = self.id_a if claim is None else claim
        meta = {"workspace_id": claim} if claim else {}
        if etype.startswith("customer.subscription."):
            body = {"id": sub, "customer": customer, "metadata": meta}
        else:
            body = {"id": "in_1", "customer": customer,
                    "parent": {"type": "subscription_details",
                               "subscription_details": {"subscription": sub,
                                                        "metadata": meta}}}
        body.update(obj)
        return {"id": eid, "type": etype, "livemode": True,
                "data": {"object": body}}

    def _dump(self, path):
        con = sqlite3.connect(path)
        try:
            return "\n".join(con.iterdump())
        finally:
            con.close()

    def _query(self, path, sql, args=()):
        con = sqlite3.connect(path)
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def _write(self, path, *statements):
        con = sqlite3.connect(path)
        try:
            for sql, args in statements:
                con.execute(sql, args)
            con.commit()
        finally:
            con.close()

    def assert_refused_unchanged(self, event):
        before = (self._dump(self.db_a), self._dump(self.db_b))
        answer = self._post_webhook(event)
        self.assertEqual(answer[0], 404, answer)
        self.assertEqual(json.loads(answer[1])["error"]["message"], "unknown object")
        self.assertEqual((self._dump(self.db_a), self._dump(self.db_b)), before,
                         "refused delivery changed a workspace")

    def assert_no_seat_unchanged(self, event):
        before = (self._dump(self.db_a), self._dump(self.db_b))
        status, body, _ = self._post_webhook(event)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["status"], "no_seat")
        self.assertEqual((self._dump(self.db_a), self._dump(self.db_b)), before)

    # -- checkout stamps the unique identity --------------------------------

    def test_identities_are_distinct_and_not_the_namespace(self):
        self.assertNotEqual(self.id_a, self.id_b)
        for ident in (self.id_a, self.id_b):
            self.assertNotEqual(ident, "local")

    def test_checkout_metadata_carries_the_workspace_identity(self):
        status, body = self.http._call("/api/v1/billing/checkout",
                                       body={"price_id": "price_pro"},
                                       headers=self.http._csrf())
        self.assertEqual(status, 200, body)
        fields = [c for c in self.provider.calls if c[1] == "/checkout/sessions"][0][2]
        for key in ("client_reference_id", "metadata[workspace_id]",
                    "subscription_data[metadata][workspace_id]"):
            self.assertEqual(fields[key], self.id_a, key)

    # -- cross-tenant delivery ---------------------------------------------

    def test_a_subscription_claimed_for_b_is_unknown_and_changes_nothing(self):
        self.seat("sub_a", "acme")
        self.provider.add("sub_a", "cus_a", self.id_a)
        for etype in ("invoice.paid", "customer.subscription.updated"):
            with self.subTest(etype):
                self.assert_refused_unchanged(
                    self._event(f"evt_{etype}", etype=etype, claim=self.id_b))

    def test_event_naming_two_identities_refused_before_provider_read(self):
        self.provider.add("sub_a", "cus_a", self.id_a)
        event = self._event("evt_meta", etype="customer.subscription.updated",
                            client_reference_id=self.id_b)
        self.assert_refused_unchanged(event)
        self.assertEqual(self.provider.calls, [])

    def test_unhandled_event_naming_a_identity_is_ignored(self):
        event = self._event("evt_upcoming", etype="invoice.upcoming")
        before = (self._dump(self.db_a), self._dump(self.db_b))
        status, body, _ = self._post_webhook(event)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["status"], "ignored")
        self.assertEqual((self._dump(self.db_a), self._dump(self.db_b)), before)

    def test_a_event_binds_a(self):
        self.seat("sub_a", "acme")
        self.provider.add("sub_a", "cus_a", self.id_a)
        status, body, _ = self._post_webhook(self._event("evt_ok"))
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["status"], "reconciled")
        self.assertEqual(self._query(self.db_a, "SELECT customer_id FROM billing_bindings"),
                         [("cus_a",)])
        self.assertEqual(self._query(self.db_b, "SELECT COUNT(*) FROM billing_bindings"),
                         [(0,)])

    def test_bound_b_refuses_a_subscription_claimed_for_b(self):
        self.seat("sub_a", "acme")
        self.seat("sub_b", "bravo")
        self.provider.add("sub_a", "cus_a", self.id_a)
        self.provider.add("sub_b", "cus_b", self.id_b)
        status, body, _ = self._post_webhook(
            self._event("evt_b", sub="sub_b", customer="cus_b", claim=self.id_b))
        self.assertEqual(status, 200, body)
        self.assert_refused_unchanged(self._event("evt_a2", claim=self.id_b))

    def test_legacy_local_claim_has_no_seat_and_changes_nothing(self):
        self.provider.add("sub_l", "cus_l", "local")
        self.assert_no_seat_unchanged(
            self._event("evt_l", sub="sub_l", customer="cus_l", claim="local"))
        self.assertEqual(self.provider.calls, [])

    def test_missing_or_legacy_provider_metadata_refused_without_side_effects(self):
        self.seat("sub_l", "acme")
        self.seat("sub_n", "bravo")
        self.provider.add("sub_l", "cus_l", "local")
        self.provider.add("sub_n", "cus_n", None)
        for n, ident in enumerate((self.id_a, self.id_b)):
            with self.subTest(ident):
                self.assert_refused_unchanged(self._event(
                    f"evt_l_{n}", sub="sub_l", customer="cus_l", claim=ident))
                self.assert_refused_unchanged(self._event(
                    f"evt_n_{n}", sub="sub_n", customer="cus_n", claim=ident))

    def test_gone_subscription_unknown_to_b_is_refused(self):
        self.seat("sub_gone", "bravo")
        self.assert_refused_unchanged(
            self._event("evt_gone", sub="sub_gone", claim=self.id_b,
                        etype="customer.subscription.deleted"))

    # -- portal, trial and entitlement refuse an unverified binding ----------

    def _misbind_a(self):
        """A's database holds a webhook-made binding to someone else's
        customer, as the pre-L24 'local' rule could create it."""
        self._write(self.db_a, (
            "INSERT INTO billing_bindings (workspace_id, customer_id, bound_by,"
            " bound_at) VALUES ('local', 'cus_other', 'webhook-first-purchase', 'x')", ()), (
            "INSERT INTO billing_entitlement (workspace_id, customer_id, status,"
            " price_id, current_period_end, updated_at)"
            " VALUES ('local', 'cus_other', 'active', 'price_pro', ?, 'x')",
            (int(time.time()) + 86400,)), (
            "INSERT INTO billing_events (event_id, workspace_id, customer_id,"
            " subscription_id, event_type, status, created_at)"
            " VALUES ('evt_old', 'local', 'cus_other', 'sub_other',"
            " 'invoice.paid', 'reconciled', 'x')", ()))

    def test_portal_refuses_an_unverified_binding(self):
        self._misbind_a()
        status, body = self.http._call("/api/v1/billing/portal", body={},
                                       headers=self.http._csrf())
        self.assertEqual(status, 400, body)
        self.assertNotIn("cus_other", json.dumps(body))
        self.assertEqual([c for c in self.provider.calls
                          if c[1] == "/billing_portal/sessions"], [])

    def test_checkout_and_trial_refuse_an_unverified_binding(self):
        self._misbind_a()
        before = self._dump(self.db_a)
        status, body = self.http._call("/api/v1/billing/checkout",
                                       body={"price_id": "price_pro"},
                                       headers=self.http._csrf())
        self.assertEqual(status, 400, body)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self._dump(self.db_a), before)

    def test_status_reports_no_entitlement_for_an_unverified_binding(self):
        self._misbind_a()
        status, body = self.http._call("/api/v1/billing/status", method="GET",
                                       headers=self.http._csrf())
        self.assertEqual(status, 200, body)
        self.assertFalse(body["entitlement"]["allowed"])

    def test_verified_binding_opens_the_portal(self):
        self.seat("sub_a", "acme")
        self.provider.add("sub_a", "cus_a", self.id_a)
        status, body, _ = self._post_webhook(self._event("evt_ok"))
        self.assertEqual(status, 200, body)
        status, body = self.http._call("/api/v1/billing/portal", body={},
                                       headers=self.http._csrf())
        self.assertEqual(status, 200, body)
        portal = [c for c in self.provider.calls if c[1] == "/billing_portal/sessions"]
        self.assertEqual(portal[0][2]["customer"], "cus_a")
        status, body = self.http._call("/api/v1/billing/status", method="GET",
                                       headers=self.http._csrf())
        self.assertTrue(body["entitlement"]["allowed"], body)

    def _portal(self):
        return self.http._call("/api/v1/billing/portal", body={},
                               headers=self.http._csrf())

    def test_deletion_naming_no_identity_never_verifies_a_legacy_binding(self):
        # Review LOW-1: a legacy webhook binding plus a subscription known
        # under 'local'; the provider no longer has it. A deletion naming
        # no identity must not make the binding count.
        self._misbind_a()
        self.seat("sub_other", "acme")
        self._write(self.db_a, (
            "INSERT INTO billing_subscriptions (subscription_id, workspace_id,"
            " customer_id, verified_at) VALUES ('sub_other', 'local',"
            " 'cus_other', 'x')", ()))
        self.assert_refused_unchanged(
            self._event("evt_del", sub="sub_other", customer="cus_other",
                        etype="customer.subscription.deleted"))
        status, body = self._portal()
        self.assertEqual(status, 400, body)
        self.assertEqual([c for c in self.provider.calls
                          if c[1] == "/billing_portal/sessions"], [])

    def test_a_price_outside_the_seats_offer_is_unknown_and_changes_nothing(self):
        # BY2 (design 8.2 step 3): the resource must be for the seat's
        # price, an EARWITNESS_BILLING_PRICES price of the seat's offer.
        # This replaces BY1's "unpriced" first purchase (review LOW-2): a
        # seat only exists for a subscription the window checked against
        # its offer, so another price is a conflict, not a first purchase.
        self.seat("sub_a", "acme")
        self.provider.add("sub_a", "cus_a", self.id_a, price="price_gone")
        self.assert_refused_unchanged(self._event("evt_unpriced"))
        self.assertEqual(self._portal()[0], 400)

    def test_legacy_binding_counts_after_metadata_is_set_to_the_identity(self):
        # docs/hosting/billing.md legacy step 3: the provider's metadata now
        # names this identity and the customer is the bound one.
        self._write(self.db_a, (
            "INSERT INTO billing_bindings (workspace_id, customer_id, bound_by,"
            " bound_at) VALUES ('local', 'cus_a', 'webhook-first-purchase', 'x')", ()))
        self.assertEqual(self._portal()[0], 400)
        self.seat("sub_a", "acme")
        self.provider.add("sub_a", "cus_a", self.id_a)
        status, body, _ = self._post_webhook(
            self._event("evt_fix", etype="customer.subscription.updated"))
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["status"], "reconciled")
        status, body = self._portal()
        self.assertEqual(status, 200, body)

    def test_signed_malformed_delivery_is_the_fixed_400(self):
        # Review NIT-1: parse and mode checks run before any workspace is
        # resolved.
        cases = {"not json": b"not json", "not an object": b"[1]",
                 "no id/type": json.dumps({"livemode": True}).encode(),
                 "test mode": json.dumps({"id": "evt_t", "type": "invoice.paid",
                                          "livemode": False}).encode()}
        for name, raw in cases.items():
            with self.subTest(name):
                before = self._dump(self.db_a)
                real = self._post_webhook(raw)
                self.assertEqual(real[0], 400)
                self.assertEqual(json.loads(real[1])["error"]["message"],
                                 "invalid webhook delivery")
                self.assertEqual(self._dump(self.db_a), before)

    def test_explicit_operator_binding_is_honoured(self):
        con = sqlite3.connect(self.db_a)
        con.row_factory = sqlite3.Row
        try:
            _billing.bind_billing_customer(con, "local", "cus_ops", operator="reno")
            con.commit()
        finally:
            con.close()
        status, body = self.http._call("/api/v1/billing/portal", body={},
                                       headers=self.http._csrf())
        self.assertEqual(status, 200, body)


if __name__ == "__main__":
    unittest.main()
