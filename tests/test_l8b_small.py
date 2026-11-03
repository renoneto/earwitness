"""L19: small L8b defects D5 (share by page-address ref) and D7 (frozen
HTML download header), plus the 500-without-log gap behind D5.

Real HostedApp HTTP through the existing hosted fixture; fixture data only.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
import unittest
from unittest import mock

from earwitness.app import billing as _billing
from earwitness.app import browser_auth as _auth
from earwitness.app import campaigns as _camp
from earwitness.app import customer as _customer
from earwitness.app import services as _services
from tests import backend_fixtures
from tests.test_backend_authority import HostedReaderHttpTest

OPS_LOGGER = "earwitness.ops"
GUEST_EMAIL = "guest@x.example"


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.http = HostedReaderHttpTest()
        self.http.setUp()
        self.addCleanup(self.http.tearDown)
        con = sqlite3.connect(self.http.db_path, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            self.refs = backend_fixtures.publish_sources(con)
        finally:
            con.close()
        self.guest_session = self._add_session("sess-l19-guest", "guest",
                                               GUEST_EMAIL, "tok-guest")

    def _add_session(self, session_id, subject, email, csrf):
        digest = hashlib.sha256(f"ew-browser-v1:{session_id}".encode()).hexdigest()
        now = time.time()
        raw = self.http.app.store.auth_connection()
        try:
            _auth.ensure_auth_schema(raw)
            raw.execute(
                "INSERT INTO browser_sessions (session_hash, issuer, subject,"
                " email, email_verified, csrf_token, active_workspace,"
                " created_at, expires_at, revoked_at)"
                " VALUES (?, 'https://i.example', ?, ?, 1, ?, 'acme', ?, ?, NULL)",
                (digest, subject, email, csrf, now, now + 3600))
            raw.commit()
        finally:
            raw.close()
        return {"x-csrf-token": csrf, "origin": "http://127.0.0.1:8771/",
                "cookie": f"ew_session={session_id}"}

    def owner(self):
        return self.http._csrf()

    def create_share(self, object_type, object_id, fields=("metadata", "excerpt")):
        return self.http._call(
            "/api/v1/shares", body={
                "object_type": object_type, "object_id": object_id,
                "audience": GUEST_EMAIL, "fields": list(fields)},
            headers=self.owner())

    def share_rows(self):
        con = sqlite3.connect(self.http.db_path)
        try:
            return con.execute("SELECT object_type, object_id FROM shares").fetchall()
        finally:
            con.close()

    def guest_read(self, share_id, evidence_id):
        return self.http._call(
            f"/api/v1/shared/evidence/{evidence_id}", method="GET",
            headers=self.guest_session,
            query=f"share_id={share_id}&workspace_id=acme")


class ShareByPageAddressRefTest(_Fixture):
    """D5: Settings > Shares tells users to use the ID at the end of the
    item's page address. For evidence that is an sr1: ref."""

    def test_internal_id_share_reads_as_a_control(self):
        status, body = self.create_share("evidence", "fixture-ad")
        self.assertEqual(status, 200, body)
        status, body = self.guest_read(body["share_id"], "fixture-ad")
        self.assertEqual(status, 200, body)

    def test_page_address_ref_resolves_to_the_internal_id_at_creation(self):
        ref = self.refs["fixture-ad"]
        self.assertTrue(ref.startswith("sr1:"))
        status, body = self.create_share("evidence", ref)
        self.assertEqual(status, 200, body)
        self.assertEqual(self.share_rows(), [("evidence", "fixture-ad")])
        # the share reads under the internal id it now holds
        status, read = self.guest_read(body["share_id"], "fixture-ad")
        self.assertEqual(status, 200, read)
        # and a read by the ref is a named refusal, never a 500
        status, read = self.guest_read(body["share_id"], ref)
        self.assertEqual(status, 403, read)

    def test_interval_ref_resolves_to_its_publication_record(self):
        status, body = self.create_share("evidence", self.refs["fixture-ad"] + ":w:1.0:2.0")
        self.assertEqual(status, 200, body)
        self.assertEqual(self.share_rows(), [("evidence", "fixture-ad")])

    def test_episode_page_address_ref_resolves_for_an_episode_share(self):
        status, body = self.create_share("episode", self.refs["e1"])
        self.assertEqual(status, 200, body)
        self.assertEqual(self.share_rows(), [("episode", "e1")])

    def test_unresolvable_or_mismatched_ref_is_refused_with_400_and_stores_nothing(self):
        cases = [
            ("evidence", "sr1:" + "0" * 32),            # unknown publication
            ("evidence", "sr1:not-a-ref"),              # malformed
            ("evidence", self.refs["e1"]),              # an episode ref for evidence
            ("episode", self.refs["fixture-ad"]),       # evidence ref for an episode
            ("campaign", self.refs["fixture-ad"]),      # sr1 means nothing here
        ]
        for object_type, ref in cases:
            status, body = self.create_share(object_type, ref)
            self.assertEqual(status, 400, (object_type, ref, body))
            self.assertEqual(body["error"]["code"], "INVALID_ARGUMENT")
            self.assertIn("sr1", body["error"]["message"] + " sr1")
        self.assertEqual(self.share_rows(), [])

    def test_caller_without_the_sharing_right_gets_403_before_any_ref_lookup(self):
        from earwitness.app import access as _access
        ctx = mock.MagicMock()
        ctx.principal_id = "viewer-1"
        with mock.patch.object(_services._collaboration, "check_permission",
                               return_value=False), \
                mock.patch.object(_access, "require_customer_mutation"):
            for ref in (self.refs["fixture-ad"], "sr1:" + "0" * 32):
                with self.assertRaises(_access.AuthorizationError) as caught:
                    _services.collab_share_create(
                        ctx, mock.MagicMock(), {"row_namespace": "local"},
                        "evidence", ref, GUEST_EMAIL, ["metadata"])
                self.assertEqual(caught.exception.status, 403)

    def test_existing_unresolved_share_never_reads_as_500(self):
        # A share stored before this fix holds the sr1 ref verbatim.
        con = sqlite3.connect(self.http.db_path, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            from earwitness.app import collaboration as _collab
            share = _collab.create_share(
                con, "local", "evidence", self.refs["fixture-ad"], GUEST_EMAIL,
                ["metadata", "excerpt"], "oidc:https://i.example:owner")
        finally:
            con.close()
        status, body = self.guest_read(share["share_id"], self.refs["fixture-ad"])
        self.assertNotEqual(status, 500, body)
        self.assertIn(status, (200, 404), body)

    def test_unknown_evidence_id_read_through_a_share_is_404_not_500(self):
        status, body = self.create_share("evidence", "no-such-evidence")
        self.assertEqual(status, 200, body)
        status, read = self.guest_read(body["share_id"], "no-such-evidence")
        self.assertEqual(status, 404, read)


def _log_events(cap):
    out = []
    for record in cap.records:
        try:
            out.append(json.loads(record.getMessage()))
        except ValueError:
            pass
    return out


class UnhandledErrorLoggingTest(_Fixture):
    """Every unhandled 500 writes one request.error line (type and where,
    no values), whichever handler caught it."""

    def _assert_logged(self, status, cap, error_type="RuntimeError"):
        self.assertEqual(status, 500)
        failed = [e for e in _log_events(cap) if e.get("event") == "request.error"]
        self.assertEqual(len(failed), 1, [e.get("event") for e in _log_events(cap)])
        self.assertEqual(failed[0]["error_type"], error_type)
        self.assertTrue(failed[0]["where"])

    def test_collab_route_500_logs_request_error(self):
        with mock.patch.object(_services, "collab_shares",
                               side_effect=RuntimeError("boom")):
            with self.assertLogs(OPS_LOGGER, level="INFO") as cap:
                status, _ = self.http._call("/api/v1/shares", method="GET",
                                            headers=self.owner())
        self._assert_logged(status, cap)

    def test_guest_read_500_logs_request_error(self):
        status, share = self.create_share("evidence", "fixture-ad")
        self.assertEqual(status, 200, share)
        with mock.patch.object(_services, "evidence_detail",
                               side_effect=RuntimeError("boom")):
            with self.assertLogs(OPS_LOGGER, level="INFO") as cap:
                status, _ = self.guest_read(share["share_id"], "fixture-ad")
        self._assert_logged(status, cap)

    def test_unrelated_key_error_in_a_guest_read_is_a_logged_500_not_a_404(self):
        from earwitness.app import ops as _ops
        status, share = self.create_share("evidence", "fixture-ad")
        self.assertEqual(status, 200, share)
        with mock.patch.object(_ops, "hide_brand_guess", side_effect=KeyError("bug")):
            with self.assertLogs(OPS_LOGGER, level="INFO") as cap:
                status, _ = self.guest_read(share["share_id"], "fixture-ad")
        self._assert_logged(status, cap, "KeyError")

    def test_customer_route_500_logs_request_error(self):
        with mock.patch.object(_services, "customer_brands",
                               side_effect=RuntimeError("boom")):
            with self.assertLogs(OPS_LOGGER, level="INFO") as cap:
                status, _ = self.http._call(
                    "/api/v1/customer/brands", method="GET",
                    headers={"authorization": "Bearer " + self.http.reader_token})
        self._assert_logged(status, cap)

    def test_agent_tool_route_500_logs_request_error(self):
        from earwitness.app import agent_contracts as _contracts
        with mock.patch.dict(_contracts.AGENT_ENTRYPOINTS,
                             {"list_brands": mock.Mock(side_effect=RuntimeError("boom"))}):
            with self.assertLogs(OPS_LOGGER, level="INFO") as cap:
                status, _ = self.http._call(
                    "/api/v1/brands", method="GET",
                    headers={"authorization": "Bearer " + self.http.reader_token})
        self._assert_logged(status, cap)

    def test_unmapped_error_type_answered_by_the_service_error_map_is_logged(self):
        # TypeError is caught by the route handler and handed to the error
        # map, which has no mapping for it and answers 500.
        with mock.patch.object(_services, "collab_shares",
                               side_effect=TypeError("boom")):
            with self.assertLogs(OPS_LOGGER, level="INFO") as cap:
                status, _ = self.http._call("/api/v1/shares", method="GET",
                                            headers=self.owner())
        self._assert_logged(status, cap, "TypeError")

    def test_billing_webhook_500_logs_request_error(self):
        # L23: the route refuses unsigned deliveries before it reaches the
        # service, so the failure must come from a validly signed one. L24:
        # it must also be a well-formed live event, or the route refuses it
        # before the workspace is resolved.
        import hmac, os
        # BY1: the one webhook route; the failure comes from the
        # completed-delivery journal it reads first.
        event = {"id": "evt_l19", "type": "invoice.paid", "livemode": True}
        raw = json.dumps(event).encode()
        ts = int(time.time())
        sig = hmac.new(b"whsec_l19", f"{ts}.".encode() + raw,
                       hashlib.sha256).hexdigest()
        with mock.patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": "whsec_l19"}), \
                mock.patch.object(_billing, "stored_delivery",
                                  side_effect=RuntimeError("boom")):
            with self.assertLogs(OPS_LOGGER, level="INFO") as cap:
                status, _ = self.http._call(
                    "/api/v1/billing/webhook", body=event,
                    headers={"stripe-signature": f"t={ts},v1={sig}"})
        self._assert_logged(status, cap)

    def test_log_line_holds_no_secret_text(self):
        with mock.patch.object(_services, "collab_shares",
                               side_effect=RuntimeError("zebra quokka secret words")):
            with self.assertLogs(OPS_LOGGER, level="INFO") as cap:
                self.http._call("/api/v1/shares", method="GET", headers=self.owner())
        self.assertNotIn("quokka", "\n".join(r.getMessage() for r in cap.records))


class FrozenHtmlDownloadTest(_Fixture):
    """D7: ?download=1 must carry Content-Disposition: attachment."""

    PAYLOAD = "<html><body>frozen</body></html>"

    def _html(self, report_no, query):
        import asyncio
        app = self.http.app
        hdrs = [(b"host", b"127.0.0.1:8771"), (b"content-length", b"0"),
                (b"authorization", ("Bearer " + self.http.reader_token).encode())]
        scope = {"type": "http", "method": "GET", "scheme": "http",
                 "path": f"/api/v1/customer/campaigns/7/reports/{report_no}/html",
                 "headers": hdrs, "query_string": query.encode()}
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        digest = hashlib.sha256(self.PAYLOAD.encode()).hexdigest()
        with mock.patch.object(_camp, "get_customer_report_html",
                               return_value=(self.PAYLOAD, digest)), \
                mock.patch.object(_camp, "report_style_src", return_value=""), \
                mock.patch.object(_customer, "is_customer_campaign", return_value=True), \
                mock.patch.object(_services, "_require_member"):
            con = sqlite3.connect(self.http.db_path, isolation_level=None)
            try:
                con.execute("INSERT OR IGNORE INTO campaigns (id, name, updated_at) VALUES (7, 'c', 'x')")
            finally:
                con.close()
            asyncio.run(app(scope, receive, send))
        start = next(m for m in sent if m["type"] == "http.response.start")
        headers = {k.decode(): v.decode() for k, v in start["headers"]}
        body = b"".join(m.get("body", b"") for m in sent
                        if m["type"] == "http.response.body")
        return start["status"], headers, body

    def test_download_carries_an_attachment_disposition_with_a_safe_filename(self):
        status, headers, body = self._html(3, "download=1")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-disposition"],
                         'attachment; filename="earwitness-report-3.html"')
        self.assertEqual(body, self.PAYLOAD.encode())
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")

    def test_preview_has_no_disposition(self):
        for query in ("", "download=0", "download=yes"):
            status, headers, _ = self._html(3, query)
            self.assertEqual(status, 200)
            self.assertNotIn("content-disposition", headers, query)


if __name__ == "__main__":
    unittest.main()
