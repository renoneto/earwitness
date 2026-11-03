"""UAT 2026-10-15 fix round, review #1 (r1b), backend half.

R1-07: archived campaigns are not tracked on Overview: they and their
placement gaps are left out, like read-only ones are not counted (hosted
and local share customer.overview).
R1-08: Archive and Restore never make a frozen report stale. Only a
revision whose retained snapshot changed the status and nothing else is
excused; a real content edit, or a revision nothing explains, still stales
the report. Frozen bytes never change. The write conflict rule holds.
R1-13: new freezes record the current renderer (campaign-report/4 since
decision 8, was /3.1); stored reports keep
their renderer, bytes and style policy.
"""
from __future__ import annotations

import io
import json
import sqlite3
import unittest
from pathlib import Path

from earwitness.app import campaigns, customer, exports, ops
from tests.test_customer_campaigns import fresh_db, seed_campaign, seed_episode
from tests.test_uat_cui import _rev, _show_of, _window


def _status(con, cid, status):
    return campaigns.save_customer_campaign(
        con, {"campaign": {"status": status}}, cid, _rev(con, cid))


def _freeze(con, cid):
    draft = campaigns.build_customer_draft(con, cid)
    return campaigns.freeze_customer_report(con, cid, _rev(con, cid), draft["pins"])


def _library_report(con, cid, report_no):
    return next(i for i in exports.archive_collection(con, kind="report")["items"]
                if i["campaign_id"] == cid and i["report_no"] == report_no)


class OverviewArchivedTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con, show="Casefile")
        self.show = _show_of(self.con)
        self.live = seed_campaign(self.con)
        _window(self.con, self.live, self.show)
        self.old = seed_campaign(self.con)
        _window(self.con, self.old, self.show, "2026-04-01", "2026-04-30")

    def tearDown(self):
        self.con.close()

    def test_archived_campaign_and_its_gaps_are_left_out(self):
        before = customer.overview(self.con)
        self.assertEqual({c["id"] for c in before["tracked_campaigns"]}, {self.live, self.old})
        self.assertIn(self.old, {g["campaign_id"] for g in before["campaign_gaps"]})
        _status(self.con, self.old, "archived")
        out = customer.overview(self.con)
        self.assertEqual([c["id"] for c in out["tracked_campaigns"]], [self.live])
        self.assertEqual({g["campaign_id"] for g in out["campaign_gaps"]}, {self.live})
        _status(self.con, self.old, "active")
        self.assertIn(self.old, {g["campaign_id"] for g in customer.overview(self.con)["campaign_gaps"]})


class ArchiveKeepsReportsTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con)
        self.cid = seed_campaign(self.con)
        _window(self.con, self.cid, _show_of(self.con))
        self.report_no = _freeze(self.con, self.cid)["report_no"]
        self.bytes = campaigns.get_customer_report_html(self.con, self.cid, self.report_no)

    def tearDown(self):
        self.con.close()

    def _stale(self):
        one = campaigns.get_customer_report(self.con, self.cid, self.report_no)
        listed = next(r for r in campaigns.list_customer_reports(self.con, self.cid)
                      if r["report_no"] == self.report_no)
        library = _library_report(self.con, self.cid, self.report_no)
        self.assertEqual(one["stale"], listed["stale"])
        self.assertEqual(one["stale"], library["stale"], library)
        return one

    def test_archive_and_restore_leave_the_report_fresh_and_byte_identical(self):
        self.assertFalse(self._stale()["stale"])
        _status(self.con, self.cid, "archived")
        out = self._stale()
        self.assertFalse(out["stale"], out["stale_reasons"])
        _status(self.con, self.cid, "active")
        out = self._stale()
        self.assertFalse(out["stale"], out["stale_reasons"])
        self.assertEqual(campaigns.get_customer_report_html(self.con, self.cid, self.report_no), self.bytes)

    def test_a_content_edit_still_stales_the_report(self):
        # UAT-100: an archived campaign is read-only, so the edit comes
        # first; archiving afterwards never hides it.
        campaigns.save_customer_campaign(self.con, {"campaign": {"message": "New words"}},
                                         self.cid, _rev(self.con, self.cid))
        _status(self.con, self.cid, "archived")
        out = self._stale()
        self.assertTrue(out["stale"])
        self.assertIn("brief", out["stale_reasons"])
        _status(self.con, self.cid, "active")
        self.assertTrue(self._stale()["stale"], "restoring never hides the edit")
        self.assertEqual(campaigns.get_customer_report_html(self.con, self.cid, self.report_no), self.bytes)

    def test_status_and_content_in_one_write_still_stales(self):
        campaigns.save_customer_campaign(self.con, {"campaign": {"status": "archived", "offer": "50% off"}},
                                         self.cid, _rev(self.con, self.cid))
        self.assertTrue(self._stale()["stale"])

    def test_an_unexplained_revision_still_reads_campaign(self):
        _status(self.con, self.cid, "archived")
        campaigns._touch_campaign(self.con, self.cid, "owner", "campaign_update", {})
        out = self._stale()
        self.assertTrue(out["stale"])
        self.assertEqual(out["stale_reasons"], ["campaign"])

    def test_a_missing_snapshot_is_never_excused(self):
        _status(self.con, self.cid, "archived")
        self.con.execute("DELETE FROM campaign_history WHERE campaign_id=? AND campaign_revision=?",
                         (self.cid, _rev(self.con, self.cid)))
        self.assertEqual(self._stale()["stale_reasons"], ["campaign"])

    def test_archive_keeps_the_write_conflict_rule(self):
        rev = _rev(self.con, self.cid)
        _status(self.con, self.cid, "archived")
        self.assertEqual(_rev(self.con, self.cid), rev + 1, "Archive still advances the revision")
        with self.assertRaises(ops.Conflict):
            campaigns.save_customer_campaign(self.con, {"campaign": {"status": "active"}}, self.cid, rev)
        with self.assertRaises(ops.Conflict):
            campaigns.freeze_customer_report(self.con, self.cid, rev,
                                             campaigns.build_customer_draft(self.con, self.cid)["pins"])


class RendererVersionTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con)
        self.cid = seed_campaign(self.con)
        _window(self.con, self.cid, _show_of(self.con))

    def tearDown(self):
        self.con.close()

    def _renderer(self, report_no):
        return self.con.execute("SELECT renderer_version FROM campaign_reports WHERE campaign_id=?"
                                " AND report_no=?", (self.cid, report_no)).fetchone()[0]

    def test_new_freezes_record_v4_and_stored_reports_keep_theirs(self):
        # Decision 8 moved new freezes from campaign-report/3.1 to /4.
        self.assertEqual(campaigns.RENDERER_VERSION, "campaign-report/4.3")
        first = _freeze(self.con, self.cid)["report_no"]
        self.assertEqual(self._renderer(first), "campaign-report/4.3")
        frozen = json.loads(self.con.execute("SELECT report_json FROM campaign_reports WHERE campaign_id=?"
                                             " AND report_no=?", (self.cid, first)).fetchone()[0])
        self.assertEqual(frozen["renderer"], "campaign-report/4.3")
        # A report stored by the earlier renderer keeps its version, bytes
        # and style policy; a later freeze never re-renders it.
        self.con.execute("UPDATE campaign_reports SET renderer_version='campaign-report/3'"
                         " WHERE campaign_id=? AND report_no=?", (self.cid, first))
        before = campaigns.get_customer_report_html(self.con, self.cid, first)
        policy = campaigns.report_style_src(self.con, self.cid, first, before[0])
        self.assertTrue(policy.startswith("style-src 'sha256-"), policy)
        _freeze(self.con, self.cid)
        self.assertEqual(campaigns.get_customer_report_html(self.con, self.cid, first), before)
        self.assertEqual(self._renderer(first), "campaign-report/3")
        self.assertEqual(campaigns.report_style_src(self.con, self.cid, first, before[0]), policy)


class LocalRoutesTest(unittest.TestCase):
    """scripts/app_server.py: Overview and report staleness after Archive."""

    PORT = 18798
    HOST = f"127.0.0.1:{PORT}"
    CSRF = "csrf-local"

    def setUp(self):
        from scripts import app_server as srv
        self.srv = srv
        self.con = fresh_db()
        seed_episode(self.con, show="Casefile")
        self.cid = seed_campaign(self.con)
        _window(self.con, self.cid, _show_of(self.con))
        self.report_no = _freeze(self.con, self.cid)["report_no"]
        self.con.commit()
        self.ws = Path(self.con.execute("PRAGMA database_list").fetchone()[2]).parent
        self._prev = {k: srv._state.get(k) for k in ("workspace", "port", "csrf")}
        srv._state.update({"workspace": self.ws, "port": self.PORT, "csrf": self.CSRF})

    def tearDown(self):
        self.srv._state.update(self._prev)
        self.con.close()

    def _req(self, method, path, body=None):
        handler = self.srv.Handler.__new__(self.srv.Handler)
        handler.command, handler.path = method, path
        handler.request_version = "HTTP/1.1"
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler._headers_buffer = []
        handler.log_message = lambda *a: None
        payload = json.dumps(body).encode() if body is not None else b""
        headers = {"Host": self.HOST, "Content-Length": str(len(payload))}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if method != "GET":
            headers["X-CSRF-Token"] = self.CSRF
            headers["Origin"] = f"http://{self.HOST}"
        handler.headers = headers
        handler.rfile, handler.wfile = io.BytesIO(payload), io.BytesIO()
        handler.close_connection = True
        getattr(handler, "do_" + method)()
        head, _, rest = handler.wfile.getvalue().partition(b"\r\n\r\n")
        return int(head.split(b" ")[1]), json.loads(rest.decode())

    def test_archive_leaves_overview_and_keeps_the_report_fresh(self):
        status, data = self._req("PUT", f"/api/v1/customer/campaigns/{self.cid}",
                                 {"campaign": {"status": "archived"}, "expected_revision": _rev(self.con, self.cid)})
        self.assertEqual(status, 200, data)
        status, data = self._req("GET", "/api/v1/customer/overview")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["tracked_campaigns"], [])
        self.assertEqual(data["campaign_gaps"], [])
        status, data = self._req("GET", f"/api/v1/customer/campaigns/{self.cid}/reports/{self.report_no}")
        self.assertEqual(status, 200, data)
        self.assertFalse(data["stale"], data)


class HostedRoutesTest(unittest.TestCase):
    """The hosted routes give the same Overview and report staleness."""

    def setUp(self):
        from tests.test_backend_http import HostedHttpTest
        self._harness = HostedHttpTest
        HostedHttpTest.setUp(self)
        con = sqlite3.connect(self.db_path, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            seed_episode(con, ep_id="ep-h", show="Casefile", version_no=99)
            self.cid = seed_campaign(con)
            _window(con, self.cid, _show_of(con, "ep-h"))
            self.report_no = _freeze(con, self.cid)["report_no"]
            self.rev = _rev(con, self.cid)
        finally:
            con.close()

    def tearDown(self):
        self._harness.tearDown(self)

    def _call(self, *args, **kwargs):
        return self._harness._call(self, *args, **kwargs)

    def test_archive_leaves_overview_and_keeps_the_report_fresh(self):
        status, _h, data = self._call(f"/api/v1/customer/campaigns/{self.cid}", "PUT",
                                      {"campaign": {"status": "archived"}, "expected_revision": self.rev})
        self.assertEqual(status, 200, data)
        status, _h, data = self._call("/api/v1/customer/overview")
        self.assertEqual(status, 200, data)
        self.assertNotIn(self.cid, [c["id"] for c in data["tracked_campaigns"]])
        self.assertNotIn(self.cid, [g["campaign_id"] for g in data["campaign_gaps"]])
        status, _h, data = self._call(f"/api/v1/customer/campaigns/{self.cid}/reports/{self.report_no}")
        self.assertEqual(status, 200, data)
        self.assertFalse(data["stale"], data)


if __name__ == "__main__":
    unittest.main()
