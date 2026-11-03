"""Walkthrough decisions 8 and 16 (2026-10-16).

Decision 8: new frozen reports use campaign-report/4. The capture date is
readable in UTC ("Oct 16, 2026, 5:10 AM UTC"), every model dump sits under
one "Technical record" heading, plain wording replaces "non-evaluative",
and the report's own style block is allowed by hash. Reports frozen by an
older renderer are served with the bytes and digest they were stored with
(fixtures under tests/fixtures/uat_d8 were frozen by the df64a8e renderer
before this change). Decision 1 is on hold: the report keeps today's
"detection"/"read" nouns.

Decision 16 (block): on an archived campaign every local legacy owner
write route answers 409 CAMPAIGN_ARCHIVED, like the gated customer routes,
and writes nothing. A stale revision still gets the conflict first.
Restore reopens each route. Creating a campaign is unaffected. Hosted
serves only GET for the legacy campaign paths. Personal notes on reads in
an archived campaign stay editable.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from earwitness.app import campaigns, citations, customer_saved, ops
from earwitness.app import db as _db
from tests.test_customer_campaigns import (add_candidate, add_placement, fresh_db,
                                           seed_campaign, seed_episode, verify_full_ad)
from tests import test_uat_cui as _cui
from tests.test_uat_cui import _rev, _show_of, _window

FIXTURES = Path(__file__).parent / "fixtures" / "uat_d8"
# sha256 of the stored bytes, recorded when the fixtures were frozen (df64a8e).
OLD_REPORTS = (("report-3.1.html", "campaign-report/3.1",
                "e610036b98709d9b3a30dfac51098bc90bd837e0fa721a4d81312a00468387a6"),
               ("report-3.html", "campaign-report/3",
                "6cff10cefbbf9248084a6289ef8a988b719f02ff83a4ce1f06da34b8d06b9c7c"))
MESSAGE = "This campaign is archived. Restore it to make changes."
TECH = "<h2>Technical record</h2>"
# MKT-02: a campaign-report/4 report frozen before the verdict rewording.
PRE_MKT02 = ("report-4-pre-mkt02.html", "campaign-report/4",
             "eacbc0aaff02812de17ea578eb52916e0715038ac31f739573299121e3300043")
OLD_VERDICT = "has a result yet — transcript coverage is incomplete."
NEW_VERDICT = "has a result yet — not every episode in scope has been fully checked for ads yet."


def _freeze_one(con, cid, now=None):
    pid = add_placement(con, cid, episode_id="ep-1", requirements={"duration_s": [5, 30]})
    add_candidate(con, "x")
    verify_full_ad(con, "x")
    out = campaigns.save_placement_evidence(
        con, cid, pid, citations.current_source_ref(con, "evidence", "x"), _rev(con, cid))
    draft = campaigns.build_customer_draft(con, cid)
    if now is None:
        return campaigns.freeze_customer_report(con, cid, out["campaign_revision"], draft["pins"])
    with mock.patch.object(campaigns, "_now_iso", return_value=now):
        return campaigns.freeze_customer_report(con, cid, out["campaign_revision"], draft["pins"])


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con)
        self.cid = seed_campaign(self.con)

    def tearDown(self):
        self.con.close()

    def _body(self, frozen):
        return campaigns.get_customer_report_html(self.con, self.cid, frozen["report_no"])[0]

    def test_new_freeze_stores_campaign_report_4(self):
        frozen = _freeze_one(self.con, self.cid)
        row = self.con.execute("SELECT renderer_version, report_json FROM campaign_reports"
                               " WHERE campaign_id=? AND report_no=?",
                               (self.cid, frozen["report_no"])).fetchone()
        self.assertEqual(campaigns.RENDERER_VERSION, "campaign-report/4.3")
        self.assertEqual(row["renderer_version"], "campaign-report/4.3")
        self.assertEqual(json.loads(row["report_json"])["renderer"], "campaign-report/4.3")

    def test_capture_date_reads_in_utc(self):
        body = self._body(_freeze_one(self.con, self.cid, now="2026-10-16T05:10:07+00:00"))
        human = body[:body.index(TECH)]
        self.assertIn("Report 1 captured Oct 16, 2026, 5:10 AM UTC.", human)
        self.assertNotIn("2026-10-16T05:10", human)

    def test_readable_utc_times(self):
        cases = {"2026-10-16T05:10:07+00:00": "Oct 16, 2026, 5:10 AM UTC",
                 "2026-10-16T00:04:00+00:00": "Oct 16, 2026, 12:04 AM UTC",
                 "2026-03-01T12:00:00+00:00": "Mar 1, 2026, 12:00 PM UTC",
                 "2026-12-31T23:59:59Z": "Dec 31, 2026, 11:59 PM UTC",
                 # A non-UTC offset is converted, never shown as UTC as-is.
                 "2026-10-15T22:10:00-07:00": "Oct 16, 2026, 5:10 AM UTC"}
        for raw, want in cases.items():
            self.assertEqual(campaigns._report_utc_time(raw), want, raw)
        # Unreadable values are shown as stored, never invented.
        self.assertEqual(campaigns._report_utc_time("sometime"), "sometime")

    def test_every_model_dump_sits_under_one_technical_record_heading(self):
        body = self._body(_freeze_one(self.con, self.cid))
        self.assertEqual(body.count(TECH), 1)
        tech = body.index(TECH)
        self.assertLess(body.index("<h2>Summary</h2>"), tech)
        self.assertGreater(body.count("<pre>"), 10)
        self.assertNotIn("<pre>", body[:tech])
        self.assertNotIn("<details>", body[:tech])
        # Inside the technical record the dumps are sub-headings, never
        # another top-level section.
        self.assertNotIn("<h2>", body[tech + len(TECH):])
        for key in ("mode", "campaign", "assessment scope", "brief versions", "research scope",
                    "scope sources", "allocation", "pins", "synthetic",
                    "synthetic placements", "limitation"):
            self.assertIn("<h3>" + key[0].upper() + key[1:] + "</h3>", body[tech:], key)
            self.assertNotIn("<h2>" + key + "</h2>", body)
        # Personal notes are no part of a report (PRIVACY-FEED-FIX1).
        self.assertNotIn("<h3>Notes</h3>", body)
        # The captured passage stays readable above the technical record.
        self.assertIn("<h3>Captured passage 1</h3><blockquote>acme bright widget offer WIDGET10</blockquote>", body[:tech])

    def test_plain_wording_replaces_non_evaluative(self):
        body = self._body(_freeze_one(self.con, self.cid))
        self.assertNotIn("non-evaluative", body)
        self.assertIn("Research snapshots record scope and evidence only, with no pass, fail"
                      " or delivery credit.", body)

    def test_row_1_vocabulary_is_unchanged(self):
        # Decision 1 is on hold: the Summary keeps today's verdict nouns.
        body = self._body(_freeze_one(self.con, self.cid))
        self.assertIn("0 of 1 counted placement has a result yet — not every episode in scope has been fully checked for ads yet.", body)

    def test_style_policy_allows_the_4_style_block_by_hash(self):
        self.assertEqual(campaigns.STYLE_HASH_RENDERERS,
                         ("campaign-report/3", "campaign-report/3.1", "campaign-report/4", "campaign-report/4.1", "campaign-report/4.2", "campaign-report/4.3"))
        frozen = _freeze_one(self.con, self.cid)
        body = self._body(frozen)
        css = re.search(r"<style>(.*?)</style>", body, re.S).group(1)
        want = "style-src 'sha256-" + base64.b64encode(hashlib.sha256(css.encode()).digest()).decode() + "'"
        self.assertEqual(campaigns.report_style_src(self.con, self.cid, frozen["report_no"], body), want)
        self.assertEqual(len(re.findall(r"<style>", body)), 1)
        self.assertNotIn("style=", body)

    def _store_old(self, report_no, name, version):
        """A report stored by an earlier renderer: its exact stored bytes."""
        payload = (FIXTURES / name).read_bytes().decode("utf-8")
        digest = hashlib.sha256(payload.encode()).hexdigest()
        export_id = self.con.execute(
            "SELECT export_id FROM campaign_reports WHERE campaign_id=? AND report_no=?",
            (self.cid, report_no)).fetchone()[0]
        # The frozen JSON is digest-bound to its export, so it stays as is;
        # serving reads the stored bytes and renderer_version only.
        self.con.execute("UPDATE exports SET payload=?, digest=? WHERE id=?", (payload, digest, export_id))
        self.con.execute("UPDATE campaign_reports SET export_digest=?, renderer_version=?"
                         " WHERE campaign_id=? AND report_no=?", (digest, version, self.cid, report_no))
        return payload

    def test_old_3_and_3_1_reports_keep_their_bytes_and_digest(self):
        first = _freeze_one(self.con, self.cid)["report_no"]
        second = campaigns.freeze_customer_report(
            self.con, self.cid, _rev(self.con, self.cid),
            campaigns.build_customer_draft(self.con, self.cid)["pins"])["report_no"]
        stored = {}
        for report_no, (name, version, digest) in zip((first, second), OLD_REPORTS):
            payload = self._store_old(report_no, name, version)
            self.assertEqual(hashlib.sha256(payload.encode()).hexdigest(), digest, name)
            stored[report_no] = (payload, digest, version)
        # A later /4 freeze never re-renders a stored report.
        third = campaigns.freeze_customer_report(
            self.con, self.cid, _rev(self.con, self.cid),
            campaigns.build_customer_draft(self.con, self.cid)["pins"])["report_no"]
        self.assertIn(TECH, self._body({"report_no": third}))
        for report_no, (payload, digest, version) in stored.items():
            self.assertEqual(campaigns.get_customer_report_html(self.con, self.cid, report_no),
                             (payload, digest))
            self.assertNotIn(TECH, payload)
            self.assertIn("non-evaluative", payload)
            row = self.con.execute("SELECT renderer_version FROM campaign_reports WHERE campaign_id=?"
                                   " AND report_no=?", (self.cid, report_no)).fetchone()
            self.assertEqual(row[0], version)
            css = re.search(r"<style>(.*?)</style>", payload, re.S).group(1)
            self.assertEqual(campaigns.report_style_src(self.con, self.cid, report_no, payload),
                             "style-src 'sha256-" + base64.b64encode(
                                 hashlib.sha256(css.encode()).digest()).decode() + "'")
            listed = {r["report_no"]: r for r in campaigns.list_customer_reports(self.con, self.cid)}
            self.assertEqual(listed[report_no]["digest"], digest)
            self.assertTrue(listed[report_no]["available"])


    def test_a_4_report_frozen_before_mkt02_keeps_its_bytes_and_digest(self):
        # MKT-02: the verdict now reads "not every episode in scope has a
        # full-episode check yet". A /4 report frozen before that
        # change (its stored bytes, frozen at 1eff46f0) still verifies and
        # serves unchanged; only new freezes get the new text.
        first = _freeze_one(self.con, self.cid)["report_no"]
        payload = self._store_old(first, PRE_MKT02[0], PRE_MKT02[1])
        self.assertEqual(hashlib.sha256(payload.encode()).hexdigest(), PRE_MKT02[2])
        self.assertIn(OLD_VERDICT, payload)
        self.assertIn("coverage incomplete; brief checks:", payload)  # old bullet kept in old bytes
        second = campaigns.freeze_customer_report(
            self.con, self.cid, _rev(self.con, self.cid),
            campaigns.build_customer_draft(self.con, self.cid)["pins"])["report_no"]
        new_body = self._body({"report_no": second})
        self.assertIn(NEW_VERDICT, new_body)
        self.assertNotIn(OLD_VERDICT, new_body)
        self.assertIn("not fully checked; brief checks:", new_body)
        self.assertNotIn("coverage incomplete", new_body)
        self.assertEqual(campaigns.get_customer_report_html(self.con, self.cid, first),
                         (payload, PRE_MKT02[2]))
        listed = {r["report_no"]: r for r in campaigns.list_customer_reports(self.con, self.cid)}
        self.assertEqual(listed[first]["digest"], PRE_MKT02[2])
        self.assertTrue(listed[first]["available"])
        css = re.search(r"<style>(.*?)</style>", payload, re.S).group(1)
        self.assertEqual(campaigns.report_style_src(self.con, self.cid, first, payload),
                         "style-src 'sha256-" + base64.b64encode(
                             hashlib.sha256(css.encode()).digest()).decode() + "'")

    def test_report_verdict_one_checked_one_missing_episode(self):
        # MKT-02 code review r2: incomplete can mean some episodes are
        # checked and some are not; the verdict never says none is.
        frozen = {"placements": [{"id": "p1"}], "allocation": {"citations": {}},
                  "coverage_by_placement": {"p1": {
                      "state": "incomplete", "missing": ["e2"], "failed": [],
                      "episodes": ["e1", "e2"], "episode_labels": {"e1": "A", "e2": "B"}}},
                  "evaluations": {"p1": {"outcome": "incomplete_coverage"}}}
        line = campaigns._report_verdict(frozen)
        self.assertEqual(line, "0 of 1 counted placement " + NEW_VERDICT)
        html = campaigns._report_summary_html(dict(frozen, mode="supplied_requirements"),
                                              lambda v: str(v))
        self.assertIn("<p>0 of 1 counted placement " + NEW_VERDICT + "</p>", html)
        self.assertNotIn("for the episodes in scope", html)

    def test_summary_label_uses_the_titles_the_report_captured(self):
        # POLISH 6e: a placement whose episode has no draft title is named
        # by the title the report captured in its sources (_report_titles),
        # in the Summary line as in the section headings.
        frozen = {"placements": [{"id": "p1", "scope": "episode", "episode_id": "ep9"}],
                  "allocation": {"citations": {}}, "episode_titles": {},
                  "captured": {"p1": [{"text": "x", "episode": {
                      "episode_id": "ep9", "public": {"ep_title": "Captured Only Title"}}}]},
                  "coverage_by_placement": {"p1": {"state": "incomplete", "missing": ["ep9"]}},
                  "evaluations": {"p1": {"outcome": "incomplete_coverage"}}}
        html = campaigns._report_summary_html(dict(frozen, mode="supplied_requirements"),
                                              lambda v: str(v))
        self.assertIn("<li>Episode Captured Only Title — ", html)
        self.assertNotIn("Episode ep9", html)


class ArchivedLegacyRouteTests(unittest.TestCase):
    """scripts/app_server.py legacy owner writes on an archived campaign."""

    PORT = 18831
    HOST = f"127.0.0.1:{PORT}"
    CSRF = "csrf-local"
    _req = _cui.LocalRoutesTest._req

    def setUp(self):
        from scripts import app_server as srv
        self.srv = srv
        self.con = fresh_db()
        seed_episode(self.con, show="Casefile")
        add_candidate(self.con, "ev-1", "ep-1")
        add_candidate(self.con, "ev-2", "ep-1")
        self.ref = citations.current_source_ref(self.con, "evidence", "ev-1")
        self.cid = seed_campaign(self.con, brand="kachava")
        self.show = _show_of(self.con)
        self.pid = _window(self.con, self.cid, self.show)
        self.con.execute("INSERT INTO campaign_evidence VALUES (?, 'ev-1')", (self.cid,))
        ops.save_check(
            self.con, {"campaign_id": self.cid, "campaign_revision": _rev(self.con, self.cid),
                       "check_type": "literal_phrase", "params": {"phrase": "first"}},
            None, None, "owner")
        self.check_id = self.con.execute("SELECT MAX(id) FROM campaign_checks").fetchone()[0]
        self._customer_status("archived")
        self.con.commit()
        self.ws = Path(self.con.execute("PRAGMA database_list").fetchone()[2]).parent
        self._prev = {k: srv._state.get(k) for k in ("workspace", "port", "csrf")}
        srv._state.update({"workspace": self.ws, "port": self.PORT, "csrf": self.CSRF})

    def tearDown(self):
        self.srv._state.update(self._prev)
        self.con.close()

    def _customer_status(self, status):
        return campaigns.save_customer_campaign(
            self.con, {"campaign": {"status": status}}, self.cid, _rev(self.con, self.cid))

    def _routes(self):
        base = f"/api/v1/campaigns/{self.cid}"
        rev = _rev(self.con, self.cid)
        check_rev = self.con.execute("SELECT revision FROM campaign_checks WHERE id=?",
                                     (self.check_id,)).fetchone()[0]
        return {
            "check_post": ("POST", "/api/v1/checks",
                           {"campaign_id": self.cid, "campaign_revision": rev,
                            "check_type": "literal_phrase", "params": {"phrase": "second"}}),
            "check_put": ("PUT", f"/api/v1/checks/{self.check_id}",
                          {"campaign_revision": rev, "expected_revision": check_rev,
                           "params": {"phrase": "changed"}}),
            "put": ("PUT", base, {"name": "Renamed", "expected_revision": rev}),
            "attach": ("POST", base + "/evidence", {"evidence_id": "ev-2", "expected_revision": rev}),
            "detach": ("DELETE", base + "/evidence/ev-1", {"expected_revision": rev}),
            "assess": ("POST", f"{base}/placements/{self.pid}/assess",
                       {"author": "Reno", "verdict": "supported", "reason": "heard it", "evidence": []}),
            "evaluate": ("POST", base + "/evaluate",
                         {"occurrences": [], "coverage": {}, "now_iso": "2026-10-16T05:10:00+00:00"}),
            "freeze": ("POST", base + "/evaluations/freeze",
                       {"evaluation": {"evaluations": {self.pid: {"outcome": "unknown"}}},
                        "pins": {"campaign_revision": rev}}),
        }

    def _written(self):
        tables = [r[0] for r in self.con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        counts = {t: self.con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("campaign_evidence", "placement_evaluations", "manual_assessments",
                            "campaign_checks")
                  if t in tables}
        row = self.con.execute("SELECT name, revision FROM campaigns WHERE id=?", (self.cid,)).fetchone()
        checks = tuple(tuple(r) for r in self.con.execute(
            "SELECT id, params, revision FROM campaign_checks ORDER BY id"))
        return (_db.aggregate_revision(self.con), tuple(row), counts, checks)

    def test_each_legacy_write_route_refuses_an_archived_campaign(self):
        for name, (method, path, body) in self._routes().items():
            before = self._written()
            status, data = self._req(method, path, body)
            self.assertEqual(status, 409, (name, data))
            self.assertEqual(data["error"]["code"], "CAMPAIGN_ARCHIVED", name)
            self.assertEqual(data["error"]["message"], MESSAGE, name)
            self.assertEqual(self._written(), before, f"{name} wrote nothing")

    def test_the_refusal_matches_the_gated_customer_route(self):
        status, gated = self._req("PUT", f"/api/v1/customer/campaigns/{self.cid}",
                                  {"campaign": {"name": "x"}, "expected_revision": _rev(self.con, self.cid)})
        self.assertEqual(status, 409, gated)
        for name, (method, path, body) in self._routes().items():
            status, data = self._req(method, path, body)
            self.assertEqual((status, data), (409, gated), name)

    def test_a_stale_revision_still_gets_the_conflict(self):
        routes = self._routes()
        for name in ("put", "attach", "detach"):
            method, path, body = routes[name]
            status, data = self._req(method, path, {**body, "expected_revision": body["expected_revision"] - 1})
            self.assertEqual(status, 412, (name, data))
            self.assertEqual(data["error"]["code"], "CONFLICT", name)

    def test_a_stale_campaign_revision_on_a_check_still_gets_the_conflict(self):
        for name in ("check_post", "check_put"):
            method, path, body = self._routes()[name]
            status, data = self._req(method, path, {**body, "campaign_revision": body["campaign_revision"] - 1})
            self.assertEqual(status, 412, (name, data))
            self.assertEqual(data["error"]["code"], "CONFLICT", name)

    def test_restore_reopens_each_route(self):
        self._customer_status("active")
        self.con.commit()
        for name in self._routes():
            method, path, body = self._routes()[name]  # each write moves the revision on
            status, data = self._req(method, path, body)
            self.assertEqual(status, 200, (name, data))
        self.assertEqual(self.con.execute("SELECT name FROM campaigns WHERE id=?", (self.cid,)).fetchone()[0],
                         "Renamed")

    def test_legacy_restore_is_the_one_allowed_status_write(self):
        rev = _rev(self.con, self.cid)
        status, data = self._req("PUT", f"/api/v1/campaigns/{self.cid}", {"status": "archived", "expected_revision": rev})
        self.assertEqual(status, 409, data)
        status, data = self._req("PUT", f"/api/v1/campaigns/{self.cid}",
                                 {"status": "active", "name": "x", "expected_revision": rev})
        self.assertEqual(status, 409, data)
        status, data = self._req("PUT", f"/api/v1/campaigns/{self.cid}", {"status": "active", "expected_revision": rev})
        self.assertEqual(status, 200, data)
        self.assertEqual(self.con.execute("SELECT status FROM campaigns WHERE id=?", (self.cid,)).fetchone()[0],
                         "active")

    def test_creating_a_campaign_is_unaffected(self):
        status, data = self._req("POST", "/api/v1/campaigns", {"name": "Another"})
        self.assertEqual(status, 200, data)
        self.assertEqual(self.con.execute("SELECT status FROM campaigns WHERE id=?", (data["id"],)).fetchone()[0],
                         "active")

    def test_personal_notes_on_reads_stay_editable(self):
        note = customer_saved.save_note(self.con, self.ref, "Mine")
        self.assertTrue(customer_saved.list_notes(self.con, ref=self.ref)["notes"])
        self.assertTrue(note)


class HostedLegacyRoutesTest(unittest.TestCase):
    """Hosted serves the legacy campaign paths read-only (GET only)."""

    def test_route_table_has_only_get_for_legacy_campaign_paths(self):
        from earwitness.app import asgi
        legacy = [(m, p) for m, p, _ in asgi.AGENT_ROUTES if p.startswith("/api/v1/campaigns")]
        self.assertEqual(legacy, [("GET", "/api/v1/campaigns"), ("GET", "/api/v1/campaigns/{campaign_id}")])

    def test_legacy_write_paths_are_not_served(self):
        from tests.test_backend_http import HostedHttpTest
        harness = HostedHttpTest("setUp")
        harness.setUp()
        try:
            con = sqlite3.connect(harness.db_path, isolation_level=None)
            con.row_factory = sqlite3.Row
            seed_episode(con, ep_id="ep-h", show="Casefile", version_no=99)
            cid = seed_campaign(con, brand="kachava")
            pid = _window(con, cid, _show_of(con, "ep-h"))
            before = _db.aggregate_revision(con)
            base = f"/api/v1/campaigns/{cid}"
            for method, path in (("PUT", base), ("POST", base + "/evidence"),
                                 ("DELETE", base + "/evidence/ev-1"),
                                 ("POST", f"{base}/placements/{pid}/assess"),
                                 ("POST", base + "/evaluate"), ("POST", base + "/evaluations/freeze")):
                status, _h, data = harness._call(path, method, {"expected_revision": _rev(con, cid)})
                self.assertIn(status, (404, 405), (method, path, data))
            self.assertEqual(_db.aggregate_revision(con), before)
            con.close()
        finally:
            harness.tearDown()


if __name__ == "__main__":
    unittest.main()
