"""K11 hosted-HTTP contract mapping tests (in-process ASGI, real auth/dispatch).

Asserts the corrected customer wire: canonical Query2 entry, B5 tracker DTO,
typed saved items, brief/campaign/allocation/freeze shapes, export digest
downloads and denial mapping. No sockets, no mocks, no socketpair: the app
object serves every request. Local loopback proof is the coordinator host
lane over real TCP (see scripts/backend_acceptance.py --mode local-http).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import unittest

from earwitness.app import asgi as _asgi
from earwitness.app import browser_auth as _auth
from earwitness.app import campaigns as _campaigns
from earwitness.app import collaboration as _collab
from earwitness.app import connections as _conn
from tests import backend_fixtures


def _register_session(app, session_id, subject, csrf="tok-csrf"):
    digest = hashlib.sha256(f"ew-browser-v1:{session_id}".encode()).hexdigest()
    now = time.time()
    raw = app.store.auth_connection()
    try:
        _auth.ensure_auth_schema(raw)
        raw.execute(
            "INSERT INTO browser_sessions (session_hash, issuer, subject,"
            " email, email_verified, csrf_token, active_workspace,"
            " created_at, expires_at, revoked_at)"
            " VALUES (?, 'https://i.example', ?, '', 0, ?, 'ws', ?, ?, NULL)",
            (digest, subject, csrf, now, now + 3600))
        raw.commit()
    finally:
        raw.close()


Q2 = {"version": 2, "mode": "mentions", "q": "lumen", "brands": None,
      "shows": None, "categories": None, "statuses": None, "audio": None,
      "date_status": "all", "from": None, "to": None, "sort": "newest",
      "page": 1, "ids": None}


class HostedHttpTest(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = os.path.realpath(os.path.join(self.tmp.name, "data"))
        os.makedirs(self.data_dir)
        ws_dir = os.path.join(self.data_dir, "ws")
        db_path = str(backend_fixtures.make_workspace(ws_dir, schema_version=8))
        self.db_path = db_path
        import sqlite3
        con = sqlite3.connect(db_path, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            _collab.create_workspace(con, "local", "Acme",
                                     "oidc:https://i.example:u-owner")
            con.commit()
        finally:
            con.close()
        grants = os.path.join(self.tmp.name, "grants.sqlite3")
        self.app = _asgi.HostedApp(_asgi.HostedConfig(
            data_dir=self.data_dir, grants_db=grants))
        _conn.register_workspace(self.app.store, "ws", db_path, self.data_dir)
        self._session_id = "sess-k11-owner"
        _register_session(self.app, self._session_id, "u-owner")
        self._viewer_id = "sess-k11-viewer"
        _register_session(self.app, self._viewer_id, "u-viewer",
                          csrf="tok-viewer")
        con = sqlite3.connect(db_path, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            invite = _collab.create_invite(con, "local", "oidc:https://i.example:u-viewer",
                                           "viewer", "oidc:https://i.example:u-owner")
            _collab.accept_invite(con, invite["invite_id"],
                                  "oidc:https://i.example:u-viewer")
            con.commit()
        finally:
            con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _call(self, path, method="GET", body=None, session=None,
              csrf=True, query="", match=None, workspace_revision=True):
        session = session or self._session_id
        revision = None
        if workspace_revision and method in ("POST", "PUT", "DELETE") and \
                not path.startswith("/api/v1/session/"):
            status, _headers, current = self._call("/api/v1/session", session=session)
            if status == 200 and current.get("authenticated"):
                revision = current["workspace_switch_revision"]
        raw_body = json.dumps(body or {}).encode()
        token = "tok-csrf" if session == self._session_id else "tok-viewer"
        hdrs = [(b"host", b"127.0.0.1:8771"),
                (b"cookie", f"ew_session={session}".encode()),
                (b"content-length", str(len(raw_body)).encode())]
        if csrf and method in ("POST", "PUT", "DELETE"):
            hdrs.append((b"x-csrf-token", token.encode()))
            hdrs.append((b"origin", b"http://127.0.0.1:8771/"))
        if match is not None:
            hdrs.append((b"if-match", str(match).encode()))
        if revision is not None:
            hdrs.append((b"x-earwitness-workspace-switch-revision", str(revision).encode()))
        scope = {"type": "http", "method": method, "path": path,
                 "scheme": "http", "headers": hdrs,
                 "query_string": query.encode()}
        sent = []

        async def receive():
            return {"type": "http.request", "body": raw_body,
                    "more_body": False}

        async def send(message):
            sent.append(message)

        asyncio.run(self.app(scope, receive, send))
        start = next(m for m in sent
                     if m["type"] == "http.response.start")
        payload = b"".join(m.get("body", b"") for m in sent
                            if m["type"] == "http.response.body")
        headers = {k.decode(): v.decode()
                   for k, v in start.get("headers", [])}
        raw = payload.decode("utf-8", "replace")
        try:
            data = json.loads(raw or "{}")
        except ValueError:
            data = {"_raw": raw}
        data["_raw_body"] = raw
        return start["status"], headers, data

    def test_research_query_cohort_envelope(self):
        status, _, data = self._call(
            "/api/v1/customer/research-query", "POST",
            {"query": dict(Q2), "selector": None, "summaries": []})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])
        self.assertIn("workspace_context", data)
        self.assertTrue(isinstance(data["revision"], str)
                        and len(data["revision"]) > 0)
        _, _, repeat = self._call(
            "/api/v1/customer/research-query", "POST",
            {"query": dict(Q2), "selector": None, "summaries": []})
        self.assertEqual(repeat["revision"], data["revision"])
        self.assertGreaterEqual(data["result"]["total"], 1)
        for item in data["result"]["items"]:
            self.assertTrue(item["ref"].startswith("sr1:"), item["ref"])

    def test_query_v1_rejected_without_mutation(self):
        status, _, data = self._call(
            "/api/v1/customer/research-query", "POST",
            {"query": {"mode": "mentions", "q": "lumen"},
             "selector": None, "summaries": []})
        self.assertEqual(status, 400, data)
        self.assertEqual(data["error"]["code"], "INVALID_ARGUMENT")

    def test_research_export_stale_basis_conflicts(self):
        status, _, data = self._call(
            "/api/v1/customer/research-export", "POST",
            {"query": dict(Q2), "scope": "all_matches", "format": "json",
             "expected_view_revision": "vr2-stale"})
        self.assertEqual(status, 409, data)
        self.assertEqual(data["error"]["code"], "VIEW_CHANGED")

    def test_export_download_same_bytes_digest_gated(self):
        _, _, rq = self._call(
            "/api/v1/customer/research-query", "POST",
            {"query": dict(Q2), "selector": None, "summaries": []})
        _, _, made = self._call(
            "/api/v1/customer/research-export", "POST",
            {"query": dict(Q2), "selector": None, "scope": "all_matches",
             "format": "json", "expected_view_revision": rq["revision"],
             "title": "http proof"})
        export_id, digest = made["export_id"], made["digest"]
        self.assertNotIn("rows", made)
        d_status, d_headers, d_data = self._call(
            f"/api/v1/customer/exports/{export_id}")
        self.assertEqual(d_status, 200, d_data)
        body = d_data["_raw_body"]
        self.assertEqual(hashlib.sha256(body.encode()).hexdigest(), digest)
        self.assertIn(f"sha256={digest}", d_headers.get("x-export-digest", ""))
        again_status, _, again = self._call(
            f"/api/v1/customer/exports/{export_id}")
        self.assertEqual(again_status, 200, again)
        self.assertEqual(again["_raw_body"], body)

    def test_unknown_export_not_found(self):
        status, _, data = self._call("/api/v1/customer/exports/424242")
        self.assertEqual(status, 404, data)

    def test_unknown_saved_item_non_disclosing_404(self):
        status, _, data = self._call(
            "/api/v1/customer/saved-items/si-does-not-exist")
        self.assertEqual(status, 404, data)

    def test_saved_item_delete_reads_revision_from_body(self):
        status, _, made = self._call(
            "/api/v1/customer/saved-items", "POST",
            {"type": "query", "title": "Body delete",
             "query": {"version": 2, "mode": "mentions", "q": "lumen"}})
        self.assertEqual(status, 200, made)
        item = made.get("item") or made
        path = "/api/v1/customer/saved-items/" + str(item["id"])
        status, _, missing = self._call(path, "DELETE", {})
        self.assertEqual(status, 428, missing)
        status, _, stale = self._call(
            path, "DELETE", {"expected_revision": item["revision"] + 5})
        self.assertIn(status, (409, 412), stale)
        status, _, gone = self._call(
            path, "DELETE", {"expected_revision": item["revision"]})
        self.assertEqual(status, 200, gone)
        status, _, data = self._call(path)
        self.assertEqual(status, 404, data)

    def test_saved_item_delete_refuses_loose_revisions(self):
        status, _, made = self._call(
            "/api/v1/customer/saved-items", "POST",
            {"type": "query", "title": "Loose delete",
             "query": {"version": 2, "mode": "mentions", "q": "lumen"}})
        self.assertEqual(status, 200, made)
        item = made.get("item") or made
        path = "/api/v1/customer/saved-items/" + str(item["id"])
        for loose in (True, 1.9, float(item["revision"]), "\u0663", [item["revision"]]):
            with self.subTest(loose=loose):
                status, _, error = self._call(path, "DELETE", {"expected_revision": loose})
                self.assertEqual(status, 400, error)
        status, _, data = self._call(path)
        self.assertEqual(status, 200, data)

    def test_campaign_put_envelope_stale_fences_without_writes(self):
        _, _, brief = self._call(
            "/api/v1/customer/briefs", "POST",
            {"title": "Fence brief", "provided_text": "Say lumen."})
        bref = {"kind": "supplied_brief", "brief_id": brief["brief_id"],
                "version": 1}
        _, _, camp = self._call(
            "/api/v1/customer/campaigns", "POST",
            {"campaign": {"name": "Fence", "mode": "supplied_requirements",
                          "status": "active", "brand": "lumen",
                          "source_query": None, "message": None,
                          "offer": None, "notes": "",
                          "requirement_provenance": {"brief_ref": bref}}})
        cid = camp["campaign"]["id"]
        status, _, data = self._call(
            f"/api/v1/customer/campaigns/{cid}", "PUT",
            {"campaign": {"name": "Fence stale",
                          "mode": "supplied_requirements", "status": "active",
                          "brand": "lumen", "source_query": None,
                          "message": None, "offer": None, "notes": "",
                          "requirement_provenance": {"brief_ref": bref}},
             "expected_revision": -1})
        self.assertIn(status, (409, 412, 428), data)
        _, _, cur = self._call(f"/api/v1/customer/campaigns/{cid}")
        self.assertEqual(cur["campaign"]["name"], "Fence", cur)

    def test_tracker_birth_active_local_silent_baseline(self):
        _, _, made = self._call(
            "/api/v1/customer/alerts", "POST",
            {"name": "http watch", "query": dict(Q2)})
        self.assertEqual(made["lifecycle"], "active_local", made)
        self.assertTrue(made["baseline_complete"], made)
        self.assertEqual(made["definition_revision"], 1, made)
        aid = made["id"]
        g_status, _, got = self._call(f"/api/v1/customer/alerts/{aid}")
        self.assertEqual(g_status, 200, got)
        dto = got["alert"]
        for field in ("id", "name", "revision", "definition_revision",
                      "query", "selector", "lifecycle", "baseline_complete",
                      "baseline_count", "count_units", "current_matches",
                      "unread_event_count", "events"):
            self.assertIn(field, dto, field)
        self.assertNotIn("query_json", dto)
        self.assertNotIn("baseline_json", dto)

    def test_tracker_read_summaries_option_matches_local(self):
        """F-16: ?summaries=0 skips the event summaries, like the local route."""
        import sqlite3
        _, _, made = self._call(
            "/api/v1/customer/alerts", "POST",
            {"name": "light read", "query": dict(Q2)})
        aid = made["id"]
        con = sqlite3.connect(self.db_path, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            ref = con.execute("SELECT id FROM source_states WHERE workspace_id='local'"
                              " ORDER BY publication_order DESC LIMIT 1").fetchone()["id"]
            con.execute("INSERT INTO customer_alert_events (workspace_id, alert_id, kind,"
                        " logical_identity, after_ref, observed_at, checked_at)"
                        " VALUES ('local', ?, 'new_text', 'light', ?, 'now', 'now')",
                        (aid, ref))
        finally:
            con.close()
        status, _, full = self._call(f"/api/v1/customer/alerts/{aid}")
        self.assertEqual(status, 200, full)
        self.assertIn("summary", full["alert"]["events"][0])
        status, _, light = self._call(f"/api/v1/customer/alerts/{aid}", query="summaries=0")
        self.assertEqual(status, 200, light)
        self.assertEqual(len(light["alert"]["events"]), 1)
        self.assertNotIn("summary", light["alert"]["events"][0])
        self.assertEqual(light["alert"]["unread_event_count"], full["alert"]["unread_event_count"])

    def test_tracker_events_carry_the_same_summary_as_the_python_read(self):
        import sqlite3
        from earwitness.app import customer as _customer
        _, _, made = self._call(
            "/api/v1/customer/alerts", "POST",
            {"name": "summary parity", "query": dict(Q2)})
        aid = made["id"]
        con = sqlite3.connect(self.db_path, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            ref = con.execute("SELECT id FROM source_states WHERE workspace_id='local'"
                              " ORDER BY publication_order DESC LIMIT 1").fetchone()["id"]
            con.execute("INSERT INTO customer_alert_events (workspace_id, alert_id, kind,"
                        " logical_identity, after_ref, observed_at, checked_at)"
                        " VALUES ('local', ?, 'new_text', 'parity', ?, 'now', 'now')",
                        (aid, ref))
            local = _customer.get_alert(con, aid)["events"][0]["summary"]
        finally:
            con.close()
        status, _, got = self._call(f"/api/v1/customer/alerts/{aid}")
        self.assertEqual(status, 200, got)
        hosted = got["alert"]["events"][0]["summary"]
        self.assertIsNotNone(hosted)
        self.assertEqual(set(hosted), set(local))
        self.assertEqual(hosted["episode_id"], local["episode_id"])

    def test_tracker_client_verification_denied_without_writes(self):
        status, _, data = self._call(
            "/api/v1/customer/alerts", "POST",
            {"name": "forged", "query": dict(Q2),
             "verification_context": "verified",
             "verified_via": "customer says so"})
        self.assertEqual(status, 400, data)

    def test_tracker_pause_refuses_check_stop_is_terminal(self):
        _, _, made = self._call(
            "/api/v1/customer/alerts", "POST",
            {"name": "lifecycle probe", "query": dict(Q2)})
        aid = made["id"]
        _, _, paused = self._call(
            f"/api/v1/customer/alerts/{aid}/lifecycle", "POST",
            {"lifecycle": "paused", "expected_revision": made["revision"]})
        c_status, _, _ = self._call(
            f"/api/v1/customer/alerts/{aid}/check", "POST", {})
        self.assertEqual(c_status, 400)
        _, _, resumed = self._call(
            f"/api/v1/customer/alerts/{aid}/lifecycle", "POST",
            {"lifecycle": "active_local",
             "expected_revision": paused["revision"]})
        c_status, _, checked = self._call(
            f"/api/v1/customer/alerts/{aid}/check", "POST", {})
        self.assertEqual(c_status, 200, checked)
        self.assertTrue(checked["ok"])
        _, _, stopped = self._call(
            f"/api/v1/customer/alerts/{aid}/lifecycle", "POST",
            {"lifecycle": "stopped",
             "expected_revision": resumed["revision"]})
        t_status, _, _ = self._call(
            f"/api/v1/customer/alerts/{aid}/check", "POST", {})
        self.assertEqual(t_status, 400)
        r_status, _, _ = self._call(
            f"/api/v1/customer/alerts/{aid}/lifecycle", "POST",
            {"lifecycle": "active_local",
             "expected_revision": stopped["revision"]})
        self.assertIn(r_status, (400, 409, 412))

    def test_viewer_mutation_forbidden(self):
        status, _, data = self._call(
            "/api/v1/customer/alerts", "POST",
            {"name": "viewer probe", "query": dict(Q2)},
            session=self._viewer_id)
        self.assertEqual(status, 403, data)

    def test_outcomes_constant_exact_seven(self):
        # D-2 follow-up: complete coverage with only unreviewed detections in
        # scope is its own outcome, never "no observation" (hard rule 1).
        self.assertEqual(tuple(_campaigns.OUTCOMES),
                         ("ambiguous", "supported", "pending",
                          "incomplete_coverage", "contradicted",
                          "no_observation_in_covered_sources",
                          "unreviewed_detections_in_scope"))

    def test_brief_versions_and_supplied_freeze_chain(self):
        _, _, brief = self._call(
            "/api/v1/customer/briefs", "POST",
            {"title": "HTTP brief", "provided_text": "Say lumen."})
        self.assertIn("brief_id", brief, brief)
        self.assertIn("digest", brief, brief)
        _, _, second = self._call(
            f"/api/v1/customer/briefs/{brief['brief_id']}/versions", "POST",
            {"title": "HTTP brief", "provided_text": "Say lumen, v2.",
             "expected_revision": 1})
        self.assertEqual(second["version"], 2, second)
        self.assertNotEqual(second["digest"], brief["digest"], second)
        bref = {"kind": "supplied_brief", "brief_id": brief["brief_id"],
                "version": 1}
        _, _, camp = self._call(
            "/api/v1/customer/campaigns", "POST",
            {"campaign": {"name": "HTTP supplied",
                          "mode": "supplied_requirements", "status": "active",
                          "brand": "lumen", "source_query": None,
                          "message": None, "offer": None, "notes": "",
                          "requirement_provenance": {"brief_ref": bref}}})
        cid = camp["campaign"]["id"]
        _, _, cur = self._call(f"/api/v1/customer/campaigns/{cid}")
        _, _, placed = self._call(
            f"/api/v1/customer/campaigns/{cid}/placements", "POST",
            {"placement": {"scope": "episode", "episode_id": "e1",
                           "timezone": "UTC", "expected_count": 1,
                           "binding": "eligible", "requirements": {},
                           "provenance": {"brief_ref": bref}},
             "expected_campaign_revision": cur["campaign"]["revision"]})
        self.assertIn("placement", placed, placed)
        self.assertIn("source_scope", placed["placement"], placed)
        _, _, draft = self._call(f"/api/v1/customer/campaigns/{cid}/draft")
        _, _, frozen = self._call(
            f"/api/v1/customer/campaigns/{cid}/reports", "POST",
            {"expected_revision": draft["draft"]["campaign"]["revision"],
             "pins": draft["draft"]["pins"]})
        self.assertIn("digest", frozen, frozen)
        r_status, _, rep = self._call(
            f"/api/v1/customer/campaigns/{cid}/reports/{frozen['report_no']}")
        self.assertEqual(r_status, 200, rep)


class SourceDigestBindingTest(unittest.TestCase):
    def test_observed_digest_matches_implementation(self):
        from earwitness.app import conversion as _conversion
        from scripts import backend_acceptance as _ba
        self.assertEqual(_ba._observed_source_digest(),
                         _conversion.api_source_digest())

    def test_binding_rejects_mid_run_source_change(self):
        from scripts import backend_acceptance as _ba
        same = _ba._digest_binding("a", "a")
        self.assertFalse(same["source_changed_during_run"])
        self.assertEqual(same["api_source_digest"], "a")
        self.assertNotIn("error", same)
        changed = _ba._digest_binding("a", "b")
        self.assertTrue(changed["source_changed_during_run"])
        self.assertEqual(changed["api_source_digest"], "b")
        self.assertIn("error", changed)

    def test_only_runs_actual_prerequisites(self):
        from scripts import backend_acceptance as _ba
        self.assertIsNone(_ba._expand_only(None))
        self.assertEqual(_ba._expand_only(["C13"]), ["C12", "C13"])
        self.assertEqual(_ba._expand_only(["C13", "C04"]),
                         ["C04", "C12", "C13"])
        self.assertEqual(_ba._expand_only(["C01"]), ["C01"])


if __name__ == "__main__":
    unittest.main()
