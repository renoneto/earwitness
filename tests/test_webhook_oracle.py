"""L23: the billing webhook is not a workspace-existence oracle (red-capable).

Astra L8D-R1-04: an anonymous POST to /api/v1/billing/<id>/webhook told a
real workspace from a made-up one (400 versus 404) because the route opened
the URL-selected workspace before it checked configuration or signature.

BY1 replaced that route with the one endpoint POST /api/v1/billing/webhook
(design 8.1); the old per-workspace URL answers the fixed 404 for every id.
The oracle rules now hold for the workspace an event names:

  * Billing off (no webhook secret): one fixed 404 "unknown object" before
    any workspace is resolved, whatever the event names.
  * Billing on: the raw delivery (signature and timestamp) is verified
    before any workspace lookup; every invalid delivery gets one fixed 400
    that never depends on the workspace the event names.
  * A validly signed event of an unhandled type gets the same answer for a
    real and a made-up workspace, and writes nothing.
  * The removed per-workspace URL answers the same 404 for a real and a
    made-up id, billing on or off, and resolves nothing.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import time
import unittest
from unittest import mock

from earwitness.app import billing as _billing
from earwitness.app import connections as _conn
from tests.test_backend_authority import HostedReaderHttpTest

SECRET = "whsec_oracle"
REAL = "acme"
FAKE = "no-such-customer-co"
ROUTE = "/api/v1/billing/webhook"


class WebhookOracleTest(unittest.TestCase):
    def setUp(self):
        self.http = HostedReaderHttpTest()
        self.http.setUp()
        self.addCleanup(self.http.tearDown)
        self.app = self.http.app
        entry = _conn.resolve_workspace(self.app.store, REAL)
        self.identities = {REAL: f"{REAL}:{entry['db_identity']}",
                           FAKE: f"{FAKE}:ws-00000000000000000000000000000000"}

    def post(self, raw=b"{}", signature=None, length=None, path=ROUTE):
        headers = [(b"host", b"127.0.0.1:8771"),
                   (b"content-length",
                    str(len(raw) if length is None else length).encode())]
        if signature is not None:
            headers.append((b"stripe-signature", signature.encode()))
        scope = {"type": "http", "method": "POST", "scheme": "http",
                 "path": path, "headers": headers, "query_string": b""}
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

    def sign(self, raw, *, ts=None, secret=SECRET):
        ts = int(time.time()) if ts is None else ts
        digest = hmac.new(secret.encode(), f"{ts}.".encode() + raw,
                          hashlib.sha256).hexdigest()
        return f"t={ts},v1={digest}"

    def event(self, workspace, *, eid="evt_l23", etype="customer.subscription.updated"):
        return json.dumps({
            "id": eid, "type": etype, "livemode": True,
            "data": {"object": {"id": "sub_1", "customer": "cus_1",
                                "metadata": {"workspace_id": self.identities[workspace]}}},
        }).encode()

    @contextlib.contextmanager
    def no_workspace_read(self):
        """Spies on the openers the webhook uses (billing.open_seat_db and
        the registry lookup); calling either fails the request."""
        with mock.patch.object(_billing, "open_seat_db",
                               side_effect=AssertionError("opened")) as seat, \
                mock.patch.object(_conn, "resolve_workspace",
                                  side_effect=AssertionError("resolved")) as reg:
            yield seat, reg

    def assert_none_called(self, spies):
        for spy in spies:
            spy.assert_not_called()

    def billing_on(self):
        patcher = mock.patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": SECRET})
        patcher.start()
        self.addCleanup(patcher.stop)

    def billing_off(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        os.environ.pop("STRIPE_WEBHOOK_SECRET", None)
        self.addCleanup(patcher.stop)

    def assert_pair_identical(self, make):
        """`make(workspace)` gives post() kwargs for an event naming it."""
        real = self.post(**make(REAL))
        fake = self.post(**make(FAKE))
        self.assertEqual(real, fake)
        return real

    def test_billing_off_real_and_fabricated_are_identical_404(self):
        self.billing_off()
        makers = (lambda w: {"raw": self.event(w)},
                  lambda w: {"raw": self.event(w), "signature": "t=1,v1=deadbeef"},
                  lambda w: {"raw": self.event(w), "signature": self.sign(self.event(w))})
        for make in makers:
            status, body, _ = self.assert_pair_identical(make)
            self.assertEqual(status, 404)
            self.assertEqual(json.loads(body)["error"]["message"], "unknown object")

    def test_billing_off_resolves_no_workspace(self):
        self.billing_off()
        with self.no_workspace_read() as spies:
            self.post(self.event(REAL), signature=self.sign(self.event(REAL)))
            self.post(self.event(FAKE))
        self.assert_none_called(spies)

    def test_billing_on_invalid_deliveries_are_identical_400(self):
        self.billing_on()
        cases = {
            "no signature": lambda w: {"raw": self.event(w)},
            "garbage signature": lambda w: {"raw": self.event(w),
                                            "signature": "t=1,v1=deadbeef"},
            "stale timestamp": lambda w: {"raw": self.event(w), "signature": self.sign(
                self.event(w), ts=int(time.time()) - 86400)},
            "wrong secret": lambda w: {"raw": self.event(w), "signature": self.sign(
                self.event(w), secret="whsec_other")},
            "no v1": lambda w: {"raw": self.event(w),
                                "signature": f"t={int(time.time())}"},
        }
        seen = set()
        for name, make in cases.items():
            with self.subTest(name):
                status, body, hdrs = self.assert_pair_identical(make)
                self.assertEqual(status, 400)
                seen.add(body)
        self.assertEqual(len(seen), 1)  # one fixed body for every invalid case

    def test_billing_on_never_opens_a_workspace_before_verification(self):
        self.billing_on()
        with self.no_workspace_read() as spies:
            self.post(self.event(REAL))
            self.post(self.event(FAKE), signature="t=1,v1=deadbeef")
            self.post(self.event(REAL),
                      signature=self.sign(self.event(REAL), secret="whsec_other"))
        self.assert_none_called(spies)

    def test_valid_signed_unhandled_event_is_the_same_for_real_and_made_up(self):
        self.billing_on()
        before = open(self.http.db_path, "rb").read()
        answers = []
        for n, workspace in enumerate((REAL, FAKE)):
            raw = self.event(workspace, eid=f"evt_c{n}", etype="customer.created")
            status, body, hdrs = self.post(raw, signature=self.sign(raw))
            self.assertEqual(status, 200, body)
            payload = json.loads(body)
            self.assertEqual(payload["status"], "ignored")
            payload.pop("event_id")
            answers.append((status, payload, hdrs))
        self.assertEqual(answers[0], answers[1])
        self.assertEqual(open(self.http.db_path, "rb").read(), before)

    def test_removed_per_workspace_url_is_the_same_404_for_every_id(self):
        for switch in (self.billing_off, self.billing_on):
            switch()
            with self.no_workspace_read() as spies:
                raw = self.event(REAL)
                real = self.post(raw, signature=self.sign(raw),
                                 path=f"/api/v1/billing/{REAL}/webhook")
                fake = self.post(raw, signature=self.sign(raw),
                                 path=f"/api/v1/billing/{FAKE}/webhook")
            self.assert_none_called(spies)
            self.assertEqual(real, fake)
            self.assertEqual(real[0], 404)
            self.assertEqual(json.loads(real[1])["error"]["message"], "unknown object")


if __name__ == "__main__":
    unittest.main()
