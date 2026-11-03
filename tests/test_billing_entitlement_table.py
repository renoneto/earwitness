"""BY1: the entitlement table (design 8.6), comped workspaces, in-app
checkout off, and the portal configuration (design 10.3).

Every row runs through the real gate (services._require_entitlement via
real HostedApp HTTP) for every member: the owner's browser session and an
editor's bearer grant. Fixture state only; no provider, no network.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import unittest
import urllib.parse
from unittest import mock

from earwitness.app import asgi as _asgi
from earwitness.app import billing as _billing
from earwitness.app import health as _health
from tests.test_backend_authority import HostedReaderHttpTest

HOUR = 3600
DAY = 86400
QUERY = {"version": 2, "mode": "ads", "q": "", "brands": None, "shows": None,
         "categories": None, "statuses": None, "audio": None,
         "date_status": "all", "from": None, "to": None, "sort": "newest",
         "page": 1, "ids": None}
ENV = {"STRIPE_SECRET_KEY": "sk_fixture", "STRIPE_WEBHOOK_SECRET": "whsec_x",
       "EARWITNESS_BILLING_SUCCESS_URL": "/billing/success",
       "EARWITNESS_BILLING_CANCEL_URL": "/billing/cancel",
       "EARWITNESS_BILLING_PRICES": json.dumps({"price_a": {"amount_cents": 9900,
                                                          "offer": "ew-a"}}),
       "EARWITNESS_BILLING_ENTITLEMENT_REQUIRED": "1",
       "EARWITNESS_BILLING_PAST_DUE_GRACE_HOURS": "336"}


class _Base(unittest.TestCase):
    def setUp(self):
        self.http = HostedReaderHttpTest()
        self.http.setUp()
        self.addCleanup(self.http.tearDown)
        env = mock.patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        self.calls = []
        transport = mock.patch.object(_billing, "_stripe_request", self._stripe)
        transport.start()
        self.addCleanup(transport.stop)

    def _stripe(self, config, method, path, fields, **kw):
        self.calls.append((method, path, dict(fields)))
        if path == "/checkout/sessions":
            return {"id": "cs_1", "url": "https://checkout.invalid/s", "livemode": True}
        if path == "/billing_portal/sessions":
            return {"url": "https://portal.invalid/p", "livemode": True}
        raise AssertionError(f"unexpected provider call {method} {path}")

    def store(self, status, *, period=None, past_due_since=0, cancel_at=0):
        now = int(time.time())
        con = sqlite3.connect(self.http.db_path)
        try:
            _billing.ensure_billing_schema(con)
            con.execute("DELETE FROM billing_entitlement")
            _billing._store_entitlement(
                con, "local", "", status, "price_a", 0,
                now + DAY if period is None else period, past_due_since,
                cancel_at)
            con.commit()
        finally:
            con.close()

    def members(self):
        """(name, status) of a gated read for every member."""
        q = "query=" + urllib.parse.quote(json.dumps(QUERY))
        owner = self.http._call("/api/v1/customer/search", method="GET",
                                headers=self.http._csrf(), query=q)
        editor = self.http._call(
            "/api/v1/customer/search", method="GET", query=q,
            headers={"authorization": "Bearer " + self.http.reader_token})
        return {"owner": owner[0], "editor": editor[0]}

    def assert_gate(self, allowed: bool, label: str):
        got = self.members()
        want = 200 if allowed else 402
        self.assertEqual(got, {"owner": want, "editor": want}, label)


class EntitlementTableTest(_Base):
    def test_every_row_through_the_real_gate_for_every_member(self):
        now = int(time.time())
        rows = [
            ("active inside period", dict(status="active", period=now + DAY), True),
            ("active past period end", dict(status="active", period=now - 5), False),
            ("active past cancel_at, period later",
             dict(status="active", period=now + 30 * DAY, cancel_at=now - 5), False),
            ("active before cancel_at", dict(status="active", period=now + DAY,
                                             cancel_at=now + HOUR), True),
            ("past_due inside grace",
             dict(status="past_due", past_due_since=now - DAY), True),
            ("past_due inside grace but past cancel_at",
             dict(status="past_due", past_due_since=now - DAY,
                  cancel_at=now - 5), False),
            ("past_due after grace",
             dict(status="past_due", past_due_since=now - 15 * DAY), False),
            ("canceled, period end in the future",
             dict(status="canceled", period=now + 20 * DAY), False),
            ("unpaid, period end in the future",
             dict(status="unpaid", period=now + 20 * DAY), False),
            ("incomplete", dict(status="incomplete"), False),
            ("incomplete_expired", dict(status="incomplete_expired"), False),
            ("trialing", dict(status="trialing", period=now + DAY), False),
            ("paused", dict(status="paused"), False),
            # No verified period end: refused, never open-ended (Astra R1-02).
            ("active, no verified period end",
             dict(status="active", period=0), False),
            ("active, no verified period end, cancel_at later",
             dict(status="active", period=0, cancel_at=now + DAY), False),
            ("past_due inside grace, no verified period end",
             dict(status="past_due", period=0, past_due_since=now - DAY), False),
        ]
        for label, kw, allowed in rows:
            with self.subTest(label):
                self.store(**kw)
                self.assert_gate(allowed, label)

    def test_no_record_is_refused(self):
        self.assert_gate(False, "no record")

    def test_compare_and_research_query_pass_the_same_gate(self):
        # PBA-R4-01: both now read in the pool; the gate still answers 402.
        now = int(time.time())
        compare = "brands=" + urllib.parse.quote(json.dumps(["lumen"]))
        research = {"query": dict(QUERY), "selector": None, "summaries": ["week"]}
        callers = {"owner": self.http._csrf(),
                   "editor": {"authorization": "Bearer " + self.http.reader_token}}
        for label, kw, want in (("active", dict(status="active", period=now + DAY), 200),
                                ("canceled", dict(status="canceled", period=now + DAY), 402)):
            self.store(**kw)
            for name, headers in callers.items():
                with self.subTest(label, caller=name):
                    self.assertEqual(self.http._call(
                        "/api/v1/customer/compare", method="GET", headers=dict(headers),
                        query=compare)[0], want)
                    self.assertEqual(self.http._call(
                        "/api/v1/customer/research-query", method="POST",
                        headers=dict(headers), body=research)[0], want)


class StoredStatusTest(unittest.TestCase):
    """What a verified current resource stores (design 8.6 column 2)."""

    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        _billing.ensure_billing_schema(self.con)

    def apply(self, eid, status, *, failures=None, cancel_at=0, raw=None):
        now = int(time.time())
        res = raw or {"id": "sub_1", "customer": "cus_1", "status": status,
                      "price_id": "price_a", "current_period_end": now + 20 * DAY,
                      "trial_end": 0, "cancel_at_period_end": False,
                      "cancel_at": cancel_at, "metadata_workspace": "acme"}
        fix = _billing.FixtureSubscriptionProtocol({"sub_1": res}, failures)
        cfg = _billing.BillingConfig(prices={"price_a": {}}, livemode_required=False)
        event = {"id": eid, "type": "customer.subscription.updated",
                 "data": {"object": {"id": "sub_1", "customer": "cus_1"}}}
        out = _billing.apply_event(self.con, "acme", event, config=cfg,
                                   provider=fix)
        self.con.commit()
        return out

    def stored(self):
        row = self.con.execute("SELECT status FROM billing_entitlement").fetchone()
        return row[0] if row else None

    def test_statuses_are_stored_as_stripe_reports_them(self):
        for n, status in enumerate(("active", "past_due", "unpaid", "incomplete",
                                    "incomplete_expired", "trialing", "paused",
                                    "canceled")):
            with self.subTest(status):
                self.apply(f"evt_{n}", status)
                self.assertEqual(self.stored(), status)

    def test_cancel_at_is_stored_and_reported(self):
        at = int(time.time()) + 3 * DAY
        self.apply("evt_c", "active", cancel_at=at)
        ent = _billing.entitlement_status(self.con, "acme")
        self.assertTrue(ent["allowed"])
        self.assertEqual(ent["cancel_at"], at)

    def test_stored_cancel_at_reads_old_flags_as_no_end_date(self):
        for raw, want in ((0, 0), (1, 0), (None, 0), ("x", 0), (1_900_000_000, 1_900_000_000)):
            self.assertEqual(_billing.stored_cancel_at({"cancel_at_period_end": raw}), want)

    def test_only_the_accessor_reads_the_cancel_column(self):
        # The column holds a timestamp; a boolean read would treat any end
        # date as "cancels at period end". Every source line naming the
        # column must be the DDL, the column list, the one writer, or the
        # accessor. Reads of the normalized Stripe resource use the
        # resource key, which this scan allows by its quoting.
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        allowed = {
            "    cancel_at_period_end INTEGER NOT NULL DEFAULT 0,",
            '    "current_period_end", "past_due_since", "cancel_at_period_end",',
            '        " cancel_at_period_end, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"',
            '        " cancel_at_period_end=excluded.cancel_at_period_end,"',
            '        value = int(ent.get("cancel_at_period_end") or 0)',
        }
        resource_reads = ('sub.get("cancel_at_period_end")',
                          'resource.get("cancel_at_period_end")',
                          '"cancel_at_period_end": bool(')
        offenders = []
        for base in ("earwitness", "scripts"):
            for path in (root / base).rglob("*.py"):
                for n, line in enumerate(path.read_text().splitlines(), 1):
                    if "cancel_at_period_end" not in line or line.lstrip().startswith("#"):
                        continue
                    if line in allowed or any(r in line for r in resource_reads):
                        continue
                    if line.strip().startswith(('"""', "`cancel_at_period_end`", '"trial_end", "cancel_at_period_end"')):
                        continue  # docstrings
                    offenders.append(f"{path.relative_to(root)}:{n}: {line.strip()}")
        self.assertEqual(offenders, [])

    def test_unreadable_current_resource_leaves_the_stored_state(self):
        self.apply("evt_1", "active")
        self.apply("evt_2", "canceled",
                   failures={"sub_1": _billing.ProviderError("timeout")})
        self.assertEqual(self.stored(), "active")

    def test_active_resource_without_a_period_end_stores_nothing(self):
        # Unreadable: no row on a first response; the prior verified state
        # (status and period) kept after one (Astra R1-02).
        for status in ("active", "past_due"):
            with self.subTest(status):
                self.con.execute("DELETE FROM billing_entitlement")
                res = {"id": "sub_1", "customer": "cus_1", "status": status,
                       "price_id": "price_a", "current_period_end": 0,
                       "trial_end": 0, "cancel_at_period_end": False,
                       "cancel_at": 0, "metadata_workspace": "acme"}
                self.apply(f"evt_first_{status}", status, raw=res)
                self.assertIsNone(self.stored())
                self.apply(f"evt_ok_{status}", "active")
                row = self.con.execute("SELECT status, current_period_end"
                                       " FROM billing_entitlement").fetchone()
                self.apply(f"evt_bad_{status}", status, raw=res)
                self.assertEqual(tuple(self.con.execute(
                    "SELECT status, current_period_end"
                    " FROM billing_entitlement").fetchone()), tuple(row))

    def test_gone_for_a_verified_binding_is_canceled_and_refused_at_once(self):
        self.apply("evt_1", "active")
        self.apply("evt_2", "active",
                   failures={"sub_1": _billing.ProviderNotFound("gone")})
        self.assertEqual(self.stored(), "canceled")
        self.assertFalse(_billing.entitlement_status(self.con, "acme")["allowed"])

    def test_pinned_version_period_end_comes_from_the_subscription_item(self):
        end = int(time.time()) + 9 * DAY
        sub = {"id": "sub_1", "object": "subscription", "customer": "cus_1",
               "status": "active", "cancel_at": end - DAY,
               "metadata": {"workspace_id": "acme"},
               "items": {"data": [{"price": {"id": "price_a"},
                                   "current_period_end": end}]}}
        got = _billing._normalize_subscription(sub)
        self.assertEqual(got["current_period_end"], end)
        self.assertEqual(got["cancel_at"], end - DAY)
        self.assertEqual(got["metadata_workspace"], "acme")


class _RawProvider(_billing.FixtureSubscriptionProtocol):
    """Scripted raw Stripe subscriptions, read through the real normalizer."""

    def get_subscription(self, subscription_id: str) -> dict:
        return _billing._normalize_subscription(
            super().get_subscription(subscription_id))


_MISSING = object()


class WebhookPeriodTest(_Base):
    """An active or past_due current resource without a valid positive
    period end is unreadable (Astra R1-02): the delivery is a retryable
    503, nothing is written or completed, and the prior verified state
    stands. Signed deliveries through the real HTTP webhook, the real
    normalizer, and the real gate for every member."""

    BAD = {"item period missing": _MISSING, "item period null": None,
           "item period zero": 0, "item period negative": -5,
           "item period text": "soon", "item period empty": ""}

    def setUp(self):
        super().setUp()
        from earwitness.app import connections as _conn
        self.app = self.http.app
        self.provider = _RawProvider()
        self.app.billing_provider = self.provider
        entry = _conn.resolve_workspace(self.app.store, "acme")
        self.ident = f"acme:{entry['db_identity']}"
        # BY2: the webhook routes through the subscription's purchase seat.
        from tests.test_billing_webhook_single import add_seat
        add_seat(self.app.store, "sub_1", "acme")

    def script(self, status, period=_MISSING, *, at_period_end=False):
        item = {"price": {"id": "price_a"}}
        if period is not _MISSING:
            item["current_period_end"] = period
        self.provider._resources["sub_1"] = {
            "id": "sub_1", "object": "subscription", "customer": "cus_1",
            "status": status, "cancel_at": None,
            "cancel_at_period_end": at_period_end,
            "metadata": {"workspace_id": self.ident},
            "items": {"object": "list", "data": [item]}}

    def deliver(self, eid):
        from tests.test_billing_webhook_single import asgi_call, sign, sub_event
        raw = json.dumps(sub_event(eid, "sub_1", "cus_1", self.ident)).encode()
        return asyncio.run(asgi_call(self.app, "/api/v1/billing/webhook", raw=raw,
                                     headers=[(b"stripe-signature",
                                               sign(raw, secret="whsec_x").encode())]))

    def dump(self):
        con = sqlite3.connect(self.http.db_path)
        try:
            return "\n".join(con.iterdump())
        finally:
            con.close()

    def completed(self):
        con = self.app.store.auth_connection()
        try:
            return sorted(r[0] for r in con.execute(
                "SELECT event_id FROM billing_deliveries"))
        except sqlite3.OperationalError:
            return []
        finally:
            con.close()

    def assert_unreadable(self, eid, label, *, allowed):
        before = self.dump()
        done = self.completed()
        status, body = self.deliver(eid)
        self.assertEqual(status, 503, (label, body))
        self.assertEqual(self.dump(), before, label)
        self.assertEqual(self.completed(), done, label)
        self.assert_gate(allowed, label)

    def test_first_malformed_response_writes_nothing_and_is_refused(self):
        for status in ("active", "past_due"):
            for name, value in self.BAD.items():
                label = f"{status}, {name}"
                with self.subTest(label):
                    self.script(status, value)
                    self.assert_unreadable("evt_" + label.replace(" ", "_"),
                                           label, allowed=False)

    def test_malformed_after_a_verified_state_keeps_it_and_retries(self):
        now = int(time.time())
        failed = []
        for status in ("active", "past_due"):
            self.script(status, now + 20 * DAY)
            ok = self.deliver(f"evt_ok_{status}")
            self.assertEqual(ok[0], 200, ok[1])
            self.assert_gate(True, f"verified {status}")
            for n, (name, value) in enumerate(self.BAD.items()):
                label = f"{status} after verified, {name}"
                with self.subTest(label):
                    self.script(status, value)
                    failed.append(f"evt_bad_{status}_{n}")
                    self.assert_unreadable(failed[-1], label, allowed=True)
        # Retryable: a refused event applies once the resource is readable.
        self.script("active", now + 30 * DAY)
        status, body = self.deliver(failed[0])
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["status"], "reconciled")
        self.assertIn(failed[0], self.completed())
        self.assert_gate(True, "retried")

    def test_cancel_at_period_end_without_cancel_at_ends_past_due_at_the_period(self):
        # Code review L2: when Stripe sets only cancel_at_period_end, the
        # verified period end is the end date. past_due inside grace with
        # that period already past is refused; without the flag it is not.
        past = int(time.time()) - DAY
        self.script("past_due", past)
        self.assertEqual(self.deliver("evt_pd")[0], 200)
        self.assert_gate(True, "past_due inside grace, no end date")
        self.script("past_due", past, at_period_end=True)
        self.assertEqual(self.deliver("evt_pd_end")[0], 200)
        self.assert_gate(False, "past_due, cancels at a period end already past")


class CompedTest(_Base):
    def test_comped_workspace_skips_the_gate_for_every_member(self):
        os.environ["EARWITNESS_BILLING_COMPED_WORKSPACES"] = "acme"
        self.assert_gate(True, "comped, no record")
        self.store("canceled", period=int(time.time()) - 5)
        self.assert_gate(True, "comped, canceled")

    def test_comped_ids_are_exact(self):
        os.environ["EARWITNESS_BILLING_COMPED_WORKSPACES"] = "acm,ACME, acme-2"
        self.assert_gate(False, "near miss")

    def pools_file(self, pools):
        """BY2: the pools come from the admission file's purchase_seats."""
        path = os.path.join(self.http.tmp.name, "admission.json")
        with open(path, "w") as handle:
            json.dump({"status": "approved", "principals": [],
                       "verified_emails": [], "purchase_seats": {
                           "enabled": False, "pools": {"ew-a": pools}}}, handle)
        return path

    def test_config_parses_comped_and_pool_lists(self):
        cfg = _billing.config_from_env({
            "EARWITNESS_BILLING_COMPED_WORKSPACES": "demo, owner",
            "EARWITNESS_ADMISSION_FILE": self.pools_file(["a1", "a2"])})
        self.assertEqual(cfg.comped_workspaces, ("demo", "owner"))
        self.assertEqual(cfg.pool_workspaces, ("a1", "a2"))

    def test_startup_refuses_a_comped_workspace_in_a_pool(self):
        env = {"EARWITNESS_BILLING_COMPED_WORKSPACES": "demo,owner",
               "EARWITNESS_ADMISSION_FILE": self.pools_file(["a1", "owner"])}
        with self.assertRaises(ValueError) as caught:
            _billing.config_from_env(env)
        self.assertIn("owner", str(caught.exception))
        self.assertIn("EARWITNESS_BILLING_COMPED_WORKSPACES", str(caught.exception))
        os.environ.update(env)
        config = _asgi.HostedConfig(mode="local-test", data_dir=self.http.data_dir,
                                    grants_db=os.path.join(self.http.tmp.name,
                                                           "g2.sqlite3"))
        with self.assertRaises(_health.ConfigError) as refused:
            _asgi.create_app(config)
        self.assertIn("EARWITNESS_BILLING_COMPED_WORKSPACES", str(refused.exception))


class CheckoutAndPortalTest(_Base):
    def _checkout(self):
        return self.http._call("/api/v1/billing/checkout",
                               body={"price_id": "price_a"},
                               headers=self.http._csrf())

    def _portal(self):
        return self.http._call("/api/v1/billing/portal", body={},
                               headers=self.http._csrf())

    def test_in_app_checkout_is_off_by_default_403_before_any_call(self):
        self.assertFalse(_billing.config_from_env({}).inapp_checkout)
        before = self.http.db_path and open(self.http.db_path, "rb").read()
        status, body = self._checkout()
        self.assertEqual(status, 403, body)
        self.assertEqual(self.calls, [])
        self.assertEqual(open(self.http.db_path, "rb").read(), before)
        os.environ["EARWITNESS_BILLING_INAPP_CHECKOUT"] = "off"
        self.assertEqual(self._checkout()[0], 403)

    def test_in_app_checkout_on_reaches_the_provider(self):
        os.environ["EARWITNESS_BILLING_INAPP_CHECKOUT"] = "on"
        status, body = self._checkout()
        self.assertEqual(status, 200, body)
        self.assertEqual([c[1] for c in self.calls], ["/checkout/sessions"])

    def _bind(self):
        con = sqlite3.connect(self.http.db_path)
        con.row_factory = sqlite3.Row
        try:
            _billing.bind_billing_customer(con, "local", "cus_ops", operator="reno")
            con.commit()
        finally:
            con.close()

    def test_portal_requires_its_configuration_and_passes_it(self):
        self._bind()
        status, body = self._portal()
        self.assertEqual(status, 400, body)
        self.assertEqual(self.calls, [])
        os.environ["EARWITNESS_BILLING_PORTAL_CONFIGURATION"] = "bpc_fixture"
        status, body = self._portal()
        self.assertEqual(status, 200, body)
        self.assertEqual(self.calls[0][2].get("configuration"), "bpc_fixture")


if __name__ == "__main__":
    unittest.main()
