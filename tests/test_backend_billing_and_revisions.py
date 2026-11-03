"""Follow-up C: the billing provider call stays off the event loop and keeps
its fences; hosted revisions parse strictly. The fixtures are reused by
reference so the borrowed test classes are not collected twice."""
from __future__ import annotations

import asyncio
import json
import time
import unittest
from unittest import mock

from earwitness.app import asgi as _asgi
from earwitness.app import billing as _billing
from earwitness.app import services as _services
from tests import test_backend_authority as _authority
from tests import test_backend_http as _http

CHECKOUT = "/api/v1/billing/checkout"


class BillingProviderCallTest(unittest.TestCase):
    HEADER = _authority.SessionMutationOrderingTest.HEADER
    setUp = _authority.SessionMutationOrderingTest.setUp

    def _config(self):
        return _billing.BillingConfig(secret_key="sk_test_x", webhook_secret="whsec_x",
                                      success_url="/ok", cancel_url="/cancel",
                                      prices={"price_pro": "Pro"},
                                      inapp_checkout=True)

    def _epoch_headers(self):
        status, session = self.fixture._call("/api/v1/session", method="GET", headers=self.headers)
        self.assertEqual(status, 200)
        return {**self.headers, self.HEADER: str(session["workspace_switch_revision"])}

    def _successes(self, operation=CHECKOUT):
        control = self.fixture.app.store._connect()
        try:
            return control.execute("SELECT COUNT(*) FROM agent_audit WHERE operation=?"
                                   " AND outcome='success'", (operation,)).fetchone()[0]
        finally:
            control.close()

    def test_slow_provider_never_stalls_a_concurrent_request(self):
        fx = self.fixture
        fx.app.limiter = _asgi.RateLimiter()
        headers = self._epoch_headers()

        def slow_provider(*_args, **_kwargs):
            time.sleep(1.5)  # stands in for urlopen(timeout=20) to the provider
            return {"checkout_session_id": "cs_1", "checkout_url": "https://pay.example/c1"}

        async def request(path, method, body, hdrs_in):
            raw = json.dumps(body).encode() if body is not None else b""
            hdrs = [(b"host", b"127.0.0.1:8771"), (b"content-length", str(len(raw)).encode())]
            hdrs += [(k.encode(), v.encode()) for k, v in hdrs_in.items()]
            sent = []

            async def receive():
                return {"type": "http.request", "body": raw, "more_body": False}

            async def send(message):
                sent.append(message)
            started = time.monotonic()
            await fx.app({"type": "http", "method": method, "path": path, "scheme": "http",
                          "headers": hdrs, "query_string": b""}, receive, send)
            return (next(m["status"] for m in sent if m["type"] == "http.response.start"),
                    time.monotonic() - started)

        async def other(t0):
            await asyncio.sleep(0.2)
            status, _ = await request("/api/v1/session", "GET", None, self.headers)
            return status, time.monotonic() - t0  # finish time since both were sent

        async def both():
            t0 = time.monotonic()
            return await asyncio.gather(
                request(CHECKOUT, "POST", {"price_id": "price_pro"}, headers), other(t0))
        with mock.patch.object(_services, "_billing_config", return_value=self._config()), \
                mock.patch.object(_billing, "create_checkout_session", side_effect=slow_provider):
            (checkout_status, checkout_elapsed), (other_status, other_done) = asyncio.run(both())
        self.assertEqual(checkout_status, 200)
        self.assertGreaterEqual(checkout_elapsed, 1.4)
        self.assertEqual(other_status, 200)
        self.assertLess(other_done, 1.0, "a concurrent GET waited on the provider call")

    def test_authority_revoked_during_provider_call_records_no_success(self):
        headers = self._epoch_headers()

        def revoking_provider(*_args, **_kwargs):
            control = self.fixture.app.store._connect()
            try:
                control.execute("BEGIN IMMEDIATE")
                control.execute("UPDATE browser_sessions SET revoked_at=?", (time.time(),))
                control.commit()
            finally:
                control.close()
            return {"checkout_session_id": "cs_1", "checkout_url": "https://pay.example/c1"}
        with mock.patch.object(_services, "_billing_config", return_value=self._config()), \
                mock.patch.object(_billing, "create_checkout_session", side_effect=revoking_provider):
            status, out = self.fixture._call(CHECKOUT, method="POST",
                                             body={"price_id": "price_pro"}, headers=headers)
        self.assertEqual(status, 403, out)
        self.assertNotIn("checkout_url", json.dumps(out))
        self.assertEqual(self._successes(), 0)

    def test_failed_provider_call_or_handler_error_records_no_success(self):
        headers = self._epoch_headers()

        def failing_provider(*_args, **_kwargs):
            raise ValueError("stripe request failed: card_declined")
        with mock.patch.object(_services, "_billing_config", return_value=self._config()), \
                mock.patch.object(_billing, "create_checkout_session", side_effect=failing_provider):
            status, out = self.fixture._call(CHECKOUT, method="POST",
                                             body={"price_id": "price_pro"}, headers=headers)
        self.assertEqual(status, 400, out)
        self.assertEqual(self._successes(), 0)
        # A handler that answers with an error status (no such method on the
        # external path) is aborted too: nothing is recorded as a success.
        self.fixture.app.limiter = _asgi.RateLimiter()
        status, out = self.fixture._call(CHECKOUT, method="DELETE", headers=self._epoch_headers())
        self.assertEqual(status, 404, out)
        self.assertEqual(self._successes(), 0)

    def test_successful_provider_call_is_recorded_once(self):
        with mock.patch.object(_services, "_billing_config", return_value=self._config()), \
                mock.patch.object(_billing, "create_checkout_session",
                                  return_value={"checkout_session_id": "cs_1",
                                                "checkout_url": "https://pay.example/c1"}):
            status, out = self.fixture._call(CHECKOUT, method="POST",
                                             body={"price_id": "price_pro"},
                                             headers=self._epoch_headers())
        self.assertEqual(status, 200, out)
        self.assertEqual(out["checkout_url"], "https://pay.example/c1")
        self.assertEqual(self._successes(), 1)


class HostedRevisionParsingTest(unittest.TestCase):
    setUp = _http.HostedHttpTest.setUp
    tearDown = _http.HostedHttpTest.tearDown
    _call = _http.HostedHttpTest._call

    def _prefs(self):
        status, _, data = self._call("/api/v1/customer/preferences")
        self.assertEqual(status, 200, data)
        return data["preferences"]

    def test_if_match_refuses_non_ascii_or_loose_integers(self):
        before = self._prefs()
        revision = before["revision"]
        digits = str(revision)
        fullwidth = "".join(chr(0xFF10 + int(d)) for d in digits)
        arabic_indic = "".join(chr(0x0660 + int(d)) for d in digits)
        for loose in ("1_0", f"{digits[:1]}_{digits[1:] or '0'}", fullwidth, arabic_indic,
                      f'" {digits} "', f'"\t{digits}\t"', f"+{digits}", "3 junk", "0x1"):
            with self.subTest(loose=loose):
                status, _, error = self._call("/api/v1/customer/preferences", "PUT",
                                              {"workspace_name": "Loose If-Match"}, match=loose)
                self.assertEqual(status, 400, error)
                self.assertEqual(self._prefs(), before)

    def test_quoted_if_match_and_string_body_revision_are_accepted(self):
        revision = self._prefs()["revision"]
        status, _, data = self._call("/api/v1/customer/preferences", "PUT",
                                     {"workspace_name": "Quoted If-Match"}, match=f'"{revision}"')
        self.assertEqual(status, 200, data)
        after = self._prefs()
        self.assertEqual(after["workspace_name"], "Quoted If-Match")
        status, _, data = self._call("/api/v1/customer/preferences", "PUT",
                                     {"workspace_name": "String revision",
                                      "expected_revision": str(after["revision"])})
        self.assertEqual(status, 200, data)
        self.assertEqual(self._prefs()["workspace_name"], "String revision")


if __name__ == "__main__":
    unittest.main()
