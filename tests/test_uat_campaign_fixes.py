"""UAT 2026-10-15 campaign findings (backend half).

UAT-092: the Add to campaign picker learns, before submit, which
placements the attach route would refuse for a read and why. The fit
answer is the attach route's own scope rule run read-only; the rule
itself is unchanged (a refused read is still refused on attach).
UAT-097: new frozen reports name a show window by its dates in the
placement section headings (numbered only when two clash); stored
reports keep their bytes.
UAT-098: the stale reason lists only what changed (a new research target
is not "the brief" or "attached sources").
"""
from __future__ import annotations

import io
import json
import re
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from earwitness.app import campaigns, citations, db, ops
from tests.test_customer_campaigns import (add_candidate, add_placement, fresh_db,
                                           seed_campaign, seed_episode)


def _rev(con, cid):
    return con.execute("SELECT revision FROM campaigns WHERE id=?", (cid,)).fetchone()[0]


def _retranscribe(con, ep_id="ep-1"):
    """Publish a newer transcript for the episode: reads detected on the
    old transcript keep pinning it."""
    cur = con.execute(
        "INSERT INTO source_versions (source_key, version_no, sha256, payload,"
        " is_active, created_at) VALUES (?, 2, 'sh2', ?, 1, ?)",
        (f"transcript:{ep_id}",
         json.dumps({"segments": [{"start": 0.0, "end": 150.0,
                                    "text": "acme bright widget offer WIDGET10 again"}]}),
         db.utcnow()))
    con.execute("UPDATE episodes SET transcript_version_id=?, revision=revision+1 WHERE id=?",
                (cur.lastrowid, ep_id))
    citations.publish_source_state(con, kind="episode", record_id=ep_id, actor="fixture")
    con.commit()


class EvidenceFitTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con)
        self.cid = seed_campaign(self.con)

    def tearDown(self):
        self.con.close()

    def test_a_read_on_an_older_transcript_is_named_before_submit(self):
        add_candidate(self.con, "old")
        old_ref = citations.current_source_ref(self.con, "evidence", "old")
        _retranscribe(self.con)
        pid = add_placement(self.con, self.cid, episode_id="ep-1")
        fit = campaigns.placement_evidence_fit(self.con, self.cid, old_ref)
        self.assertEqual(fit["placements"], {pid: "transcript_version"})
        # The attach rule itself is unchanged: the same read is refused.
        with self.assertRaisesRegex(ops.BadInput, "outside the captured placement scope"):
            campaigns.save_placement_evidence(self.con, self.cid, pid, old_ref, _rev(self.con, self.cid))

    def test_a_read_the_placement_accepts_fits_and_attaches(self):
        pid = add_placement(self.con, self.cid, episode_id="ep-1")
        add_candidate(self.con, "new")
        ref = citations.current_source_ref(self.con, "evidence", "new")
        self.assertEqual(campaigns.placement_evidence_fit(self.con, self.cid, ref)["placements"], {pid: None})
        out = campaigns.save_placement_evidence(self.con, self.cid, pid, ref, _rev(self.con, self.cid))
        self.assertEqual(out["placement_id"], pid)

    def test_other_episode_and_show_misfits_are_named(self):
        seed_episode(self.con, ep_id="ep-2", version_no=2)
        ep_pid = add_placement(self.con, self.cid, pid_suffix="e", episode_id="ep-2")
        win_pid = add_placement(self.con, self.cid, pid_suffix="w")  # no proven show
        add_candidate(self.con, "x")
        ref = citations.current_source_ref(self.con, "evidence", "x")
        fit = campaigns.placement_evidence_fit(self.con, self.cid, ref)["placements"]
        self.assertEqual(fit[ep_pid], "other_episode")
        self.assertEqual(fit[win_pid], "outside_show")
        for pid in (ep_pid, win_pid):
            with self.assertRaises(ops.BadInput):
                campaigns.save_placement_evidence(self.con, self.cid, pid, ref, _rev(self.con, self.cid))

    def test_fit_is_read_only_and_refuses_bad_input(self):
        pid = add_placement(self.con, self.cid, episode_id="ep-1")
        add_candidate(self.con, "x")
        ref = citations.current_source_ref(self.con, "evidence", "x")
        before = (_rev(self.con, self.cid), db.aggregate_revision(self.con))
        campaigns.placement_evidence_fit(self.con, self.cid, ref)
        self.assertEqual((_rev(self.con, self.cid), db.aggregate_revision(self.con)), before)
        self.assertEqual(campaigns.placement_attachments(self.con, self.cid, pid), [])
        with self.assertRaisesRegex(ops.BadInput, "malformed"):
            campaigns.placement_evidence_fit(self.con, self.cid, "nonsense")


class LocalFitRouteTest(unittest.TestCase):
    """scripts/app_server.py serves the same fit answer."""

    PORT = 18794
    HOST = f"127.0.0.1:{PORT}"

    def setUp(self):
        from scripts import app_server as srv
        self.srv = srv
        self.con = fresh_db()
        seed_episode(self.con)
        self.cid = seed_campaign(self.con)
        add_candidate(self.con, "old")
        self.ref = citations.current_source_ref(self.con, "evidence", "old")
        _retranscribe(self.con)
        self.pid = add_placement(self.con, self.cid, episode_id="ep-1")
        self.con.commit()
        self.ws = Path(self.con.execute("PRAGMA database_list").fetchone()[2]).parent
        self._prev = {k: srv._state[k] for k in ("workspace", "port", "csrf")}
        srv._state.update({"workspace": self.ws, "port": self.PORT, "csrf": "csrf-local"})

    def tearDown(self):
        self.srv._state.update(self._prev)
        self.con.close()

    def _get(self, path):
        handler = self.srv.Handler.__new__(self.srv.Handler)
        handler.command, handler.path = "GET", path
        handler.request_version = "HTTP/1.1"
        handler.requestline = f"GET {path} HTTP/1.1"
        handler._headers_buffer = []
        handler.log_message = lambda *a: None
        handler.headers = {"Host": self.HOST, "Content-Length": "0"}
        handler.rfile, handler.wfile = io.BytesIO(b""), io.BytesIO()
        handler.close_connection = True
        handler.do_GET()
        head, _, rest = handler.wfile.getvalue().partition(b"\r\n\r\n")
        return int(head.split(b" ")[1]), json.loads(rest.decode())

    def test_local_route_matches_the_backend(self):
        from urllib.parse import quote
        status, data = self._get(f"/api/v1/customer/campaigns/{self.cid}/evidence-fit?ref={quote(self.ref)}")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["placements"], {self.pid: "transcript_version"})
        self.assertEqual(data["ref"], self.ref)
        status, data = self._get(f"/api/v1/customer/campaigns/{self.cid}/evidence-fit?ref=nonsense")
        self.assertEqual(status, 400, data)


class HostedFitRouteTest(unittest.TestCase):
    """The hosted customer route serves the same fit answer."""

    def setUp(self):
        from tests.test_backend_http import HostedHttpTest
        self._harness = HostedHttpTest
        HostedHttpTest.setUp(self)

    def tearDown(self):
        self._harness.tearDown(self)

    def _call(self, *args, **kwargs):
        return self._harness._call(self, *args, **kwargs)

    def test_hosted_route_matches_the_backend(self):
        con = sqlite3.connect(self.db_path, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            seed_episode(con, ep_id="ep-h", version_no=99)
            cid = seed_campaign(con)
            add_candidate(con, "old-h", episode_id="ep-h")
            ref = citations.current_source_ref(con, "evidence", "old-h")
            _retranscribe(con, "ep-h")
            pid = add_placement(con, cid, episode_id="ep-h")
            want = campaigns.placement_evidence_fit(con, cid, ref)
        finally:
            con.close()
        from urllib.parse import quote
        status, _h, data = self._call(f"/api/v1/customer/campaigns/{cid}/evidence-fit",
                                      query="ref=" + quote(ref))
        self.assertEqual(status, 200, data)
        self.assertEqual(data["placements"], want["placements"])
        self.assertEqual(data["placements"], {pid: "transcript_version"})


def _sections(body):
    return re.findall(r"<section><h2>([^<]*)</h2>", body)


class ReportWindowHeadingTest(unittest.TestCase):
    """UAT-097: new freezes name a show window by its dates in the
    placement section headings, never by the raw show id."""

    SHOW = "cs-0123456789ab"

    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con)
        self.cid = seed_campaign(self.con)
        self.brief = json.loads(self.con.execute(
            "SELECT brief_ref_json FROM campaigns WHERE id=?", (self.cid,)).fetchone()[0])

    def tearDown(self):
        self.con.close()

    def _window(self, show, date_from, date_to):
        campaigns.save_expected_placement(
            self.con, self.cid, scope="show_window", show_id=show, episode_id=None,
            date_from=date_from, date_to=date_to, timezone_name="UTC", expected_count=1,
            requirements={"duration_s": [5, 30]},
            provenance={"brief_ref": {"kind": "supplied_brief", "brief_id": self.brief["brief_id"],
                                      "version": self.brief["version"]}},
            priority=0, actor="owner")

    def _freeze(self):
        draft = campaigns.build_customer_draft(self.con, self.cid)
        return campaigns.freeze_customer_report(
            self.con, self.cid, _rev(self.con, self.cid), draft["pins"])

    def test_section_heading_names_the_window_by_its_dates(self):
        self._window(self.SHOW, "2026-03-01", "2026-03-31")
        body, _ = campaigns.get_customer_report_html(self.con, self.cid, self._freeze()["report_no"])
        self.assertIn("Show window · Mar 1–Mar 31, 2026", _sections(body))
        self.assertFalse([h for h in _sections(body) if "cs-" in h])
        # The raw id stays in the section's technical model only.
        self.assertIn(self.SHOW, body)

    def test_clashing_windows_are_numbered_in_headings_like_the_summary(self):
        self._window(self.SHOW, "2026-03-01", "2026-03-31")
        self._window("cs-bbbbbbbbbbbb", "2026-03-01", "2026-03-31")
        self._window("cs-cccccccccccc", "2026-04-01", "2026-04-30")
        body, _ = campaigns.get_customer_report_html(self.con, self.cid, self._freeze()["report_no"])
        heads = [h for h in _sections(body) if h.startswith("Show window")]
        self.assertEqual(sorted(heads), ["Show window 1 · Mar 1–Mar 31, 2026",
                                         "Show window 2 · Mar 1–Mar 31, 2026",
                                         "Show window · Apr 1–Apr 30, 2026"])

    def test_a_stored_report_keeps_its_old_heading_bytes(self):
        self._window(self.SHOW, "2026-03-01", "2026-03-31")
        render = campaigns._render_report_html
        old = lambda frozen: render(frozen).replace("<section><h2>Show window · ",
                                                    f"<section><h2>Show window {self.SHOW} · ")
        with mock.patch.object(campaigns, "_render_report_html", old):
            first = self._freeze()
        before = campaigns.get_customer_report_html(self.con, self.cid, first["report_no"])
        self.assertIn(f"<h2>Show window {self.SHOW} · Mar 1", before[0])
        self._freeze()
        self.assertEqual(campaigns.get_customer_report_html(self.con, self.cid, first["report_no"]), before)


class StaleReasonTest(unittest.TestCase):
    """UAT-098: only what changed is listed."""

    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con)
        created = campaigns.save_customer_campaign(self.con, {"campaign": {
            "name": "Research", "mode": "research", "brand": "Acme", "notes": ""}})
        self.cid = created["campaign"]["id"]
        self.show = __import__("earwitness.app.customer", fromlist=["x"]).customer_show_for_episode(
            self.con, "ep-1")["show_identity_id"]

    def tearDown(self):
        self.con.close()

    def _freeze(self):
        draft = campaigns.build_customer_draft(self.con, self.cid)
        return campaigns.freeze_customer_report(
            self.con, self.cid, _rev(self.con, self.cid), draft["pins"])

    def test_a_new_research_target_is_the_only_reason(self):
        campaigns.create_target(self.con, self.cid, "show_window", show_id=self.show,
                                date_from="2026-03-01", date_to="2026-03-31")
        frozen = self._freeze()
        campaigns.create_target(self.con, self.cid, "episode", episode_id="ep-1")
        status = campaigns.get_customer_report(self.con, self.cid, frozen["report_no"])
        self.assertTrue(status["stale"])
        self.assertEqual(status["stale_reasons"], ["targets"])
        listed = campaigns.list_customer_reports(self.con, self.cid)
        self.assertEqual(listed[0]["stale_reasons"] if isinstance(listed, list)
                         else listed["reports"][0]["stale_reasons"], ["targets"])

    def test_a_renamed_campaign_is_still_stale_and_says_the_brief(self):
        frozen = self._freeze()
        camp = campaigns.save_customer_campaign(self.con, {"campaign": {"name": "Renamed"}},
                                                self.cid, _rev(self.con, self.cid))
        self.assertTrue(camp)
        status = campaigns.get_customer_report(self.con, self.cid, frozen["report_no"])
        self.assertTrue(status["stale"])
        self.assertIn("brief", status["stale_reasons"])

    def test_a_revision_alone_still_stales_the_report(self):
        reasons = campaigns._stale_reasons({"campaign_revision": 5, "brief_digest": "d"},
                                           {"campaign_revision": 4, "brief_digest": "d"})
        self.assertEqual(reasons, ["campaign"])


if __name__ == "__main__":
    unittest.main()
