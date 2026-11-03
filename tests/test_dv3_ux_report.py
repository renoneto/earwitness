"""Design v3 UX review (UX-07): frozen report summary for NEW freezes.

New freezes store their reason codes and render a plain Summary section
(campaign-report/3). Old stored reports are never re-rendered: their
bytes and digest stay identical. A v3 report's own style block is
allowed by hash in its sandboxed content policy; older reports keep the
policy they had. Synthetic briefs are never verification.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import unittest
from unittest import mock

from earwitness.app import campaigns, citations
from tests.test_customer_campaigns import (add_candidate, add_placement, fresh_db,
                                           seed_campaign, seed_episode,
                                           synthetic_brief, verify_full_ad)


def _rev(con, cid):
    return con.execute("SELECT revision FROM campaigns WHERE id=?", (cid,)).fetchone()[0]


def _summary(body):
    m = re.search(r"<section><h2>Summary</h2>(.*?)</section>", body, re.S)
    return m.group(1) if m else None


class NewFreezeSummaryTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con)
        self.cid = seed_campaign(self.con)

    def tearDown(self):
        self.con.close()

    def _freeze_with_one_placement(self, **placement):
        pid = add_placement(self.con, self.cid, episode_id="ep-1",
                            requirements={"duration_s": [5, 30]}, **placement)
        add_candidate(self.con, "x")
        verify_full_ad(self.con, "x")
        out = campaigns.save_placement_evidence(
            self.con, self.cid, pid, citations.current_source_ref(self.con, "evidence", "x"),
            _rev(self.con, self.cid))
        draft = campaigns.build_customer_draft(self.con, self.cid)
        frozen = campaigns.freeze_customer_report(
            self.con, self.cid, out["campaign_revision"], draft["pins"])
        return pid, draft, frozen

    def test_new_freeze_stores_reason_codes_renderer_and_summary(self):
        pid, draft, frozen = self._freeze_with_one_placement()
        row = self.con.execute(
            "SELECT report_json, renderer_version FROM campaign_reports"
            " WHERE campaign_id=? AND report_no=?", (self.cid, frozen["report_no"])).fetchone()
        stored = json.loads(row["report_json"])
        self.assertEqual(row["renderer_version"], "campaign-report/4.3")  # decision 8: new report format
        self.assertEqual(stored["renderer"], "campaign-report/4.3")  # decision 8: new report format
        self.assertEqual(stored["reason_codes"], draft["reason_codes"])
        self.assertIn(pid, stored["reason_codes"]["evaluations"])
        body, _ = campaigns.get_customer_report_html(self.con, self.cid, frozen["report_no"])
        self.assertLess(body.index("<h2>Summary</h2>"), body.index("<h2>Technical record</h2>"))  # decision 8: dumps under one heading
        summary = _summary(body)
        # The partial seed transcript keeps coverage incomplete. FUNC-2b: the
        # verified read makes ep-1 heard, so the Summary counts by episodes
        # (as the app's campaign page does); the placement-level lines move
        # into the Technical record.
        self.assertIn("1 of 1 counted placement has a result: 1 with an ad found.", summary)
        self.assertIn("Confirmed heard in 1 of 1 checked episode; not fully checked; brief checks:", summary)  # MKT-02 coverage chip kept
        record = body[body.index("<h2>Technical record</h2>"):]
        self.assertIn("<p>0 of 1 counted placement has a result yet — not every episode in scope has been fully checked for ads yet.</p>", record)
        self.assertIn("<p>Not enough data; not fully checked; brief checks:", record)
        self.assertNotIn("coverage incomplete", summary)
        self.assertNotRegex(summary, r"(?i)\bverified\b|\bverifiable\b|\bdelivered\b")
        self.assertNotIn("synthetic", summary)

    def test_citation_only_placement_is_never_counted(self):
        projection = json.loads(self.con.execute(
            "SELECT brief_ref_json FROM campaigns WHERE id=?", (self.cid,)).fetchone()[0])
        campaigns.save_expected_placement(
            self.con, self.cid, scope="episode", episode_id="ep-1",
            date_from="2026-03-01", date_to="2026-03-31", timezone_name="UTC",
            expected_count=1, requirements={"duration_s": [5, 30]},
            provenance={"brief_ref": {"kind": "supplied_brief", "brief_id": projection["brief_id"],
                                      "version": projection["version"]},
                        "binding": "citation_only"}, priority=0, actor="owner")
        draft = campaigns.build_customer_draft(self.con, self.cid)
        frozen = campaigns.freeze_customer_report(
            self.con, self.cid, _rev(self.con, self.cid), draft["pins"])
        body, _ = campaigns.get_customer_report_html(self.con, self.cid, frozen["report_no"])
        summary = _summary(body)
        self.assertIn("No placement is counted for credit. This placement is a citation only, never counted.", summary)
        self.assertIn("citation only, never counted; not fully checked", summary)

    def test_synthetic_summary_says_not_contractual_and_never_verified(self):
        saved = campaigns.save_expected_placement(
            self.con, self.cid, scope="episode", episode_id="ep-1",
            date_from="2026-03-01", date_to="2026-03-31", timezone_name="UTC",
            expected_count=1, requirements={"duration_s": [5, 30]},
            provenance={"brief_ref": synthetic_brief(self.con)}, priority=0, actor="owner")
        self.assertTrue(saved["synthetic"])
        draft = campaigns.build_customer_draft(self.con, self.cid)
        frozen = campaigns.freeze_customer_report(
            self.con, self.cid, _rev(self.con, self.cid), draft["pins"])
        body, _ = campaigns.get_customer_report_html(self.con, self.cid, frozen["report_no"])
        summary = _summary(body)
        self.assertIn("Brief type: synthetic — not contractual verification.", summary)
        self.assertNotRegex(summary.replace("not contractual verification", ""), r"(?i)verif")

    def test_old_report_bytes_and_digest_never_change(self):
        _pid, _draft, first = self._freeze_with_one_placement()
        before = campaigns.get_customer_report_html(self.con, self.cid, first["report_no"])
        original = campaigns._render_report_html
        try:
            # A later renderer change must never rewrite a stored report.
            campaigns._render_report_html = lambda frozen: "<html><body>renderer v99</body></html>\n"
            draft = campaigns.build_customer_draft(self.con, self.cid)
            second = campaigns.freeze_customer_report(
                self.con, self.cid, _rev(self.con, self.cid), draft["pins"])
        finally:
            campaigns._render_report_html = original
        after = campaigns.get_customer_report_html(self.con, self.cid, first["report_no"])
        self.assertEqual(after, before)
        self.assertEqual(after[1], hashlib.sha256(after[0].encode()).hexdigest())
        self.assertIn("renderer v99", campaigns.get_customer_report_html(
            self.con, self.cid, second["report_no"])[0])

    def test_style_policy_hash_only_for_v3_reports(self):
        _pid, _draft, frozen = self._freeze_with_one_placement()
        body, _ = campaigns.get_customer_report_html(self.con, self.cid, frozen["report_no"])
        css = re.search(r"<style>(.*?)</style>", body, re.S).group(1)
        want = "'sha256-" + base64.b64encode(hashlib.sha256(css.encode()).digest()).decode() + "'"
        self.assertEqual(campaigns.report_style_src(self.con, self.cid, frozen["report_no"], body),
                         "style-src " + want)
        self.assertNotIn("unsafe-inline", campaigns.report_style_src(self.con, self.cid, frozen["report_no"], body))
        # A pre-v3 report keeps the policy it had: no style allowance.
        self.con.execute("UPDATE campaign_reports SET renderer_version='campaign-report/2'"
                         " WHERE campaign_id=? AND report_no=?", (self.cid, frozen["report_no"]))
        self.assertEqual(campaigns.report_style_src(self.con, self.cid, frozen["report_no"], body), "")


class ShowWindowSummaryLabelTest(unittest.TestCase):
    """F-11: the Summary names a show window by its dates, never by the raw
    show id; a report already stored keeps its bytes."""

    SHOW_ID = "cs-0123456789ab"

    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con)
        self.cid = seed_campaign(self.con)
        projection = json.loads(self.con.execute(
            "SELECT brief_ref_json FROM campaigns WHERE id=?", (self.cid,)).fetchone()[0])
        campaigns.save_expected_placement(
            self.con, self.cid, scope="show_window", show_id=self.SHOW_ID, episode_id=None,
            date_from="2026-03-01", date_to="2026-03-31", timezone_name="UTC", expected_count=1,
            requirements={"duration_s": [5, 30]},
            provenance={"brief_ref": {"kind": "supplied_brief", "brief_id": projection["brief_id"],
                                      "version": projection["version"]}},
            priority=0, actor="owner")

    def tearDown(self):
        self.con.close()

    def _freeze(self):
        draft = campaigns.build_customer_draft(self.con, self.cid)
        return campaigns.freeze_customer_report(
            self.con, self.cid, _rev(self.con, self.cid), draft["pins"])

    def test_summary_names_the_window_by_its_dates(self):
        frozen = self._freeze()
        body, _ = campaigns.get_customer_report_html(self.con, self.cid, frozen["report_no"])
        summary = _summary(body)
        self.assertIn("<li>Show window · Mar 1–Mar 31, 2026 — ", summary)
        self.assertNotIn(self.SHOW_ID, summary)

    def test_a_stored_report_with_the_old_label_keeps_its_bytes(self):
        # A report frozen before this change printed the id; reading it
        # back never re-renders it with the new label.
        render = campaigns._render_report_html
        old = lambda frozen: render(frozen).replace("<li>Show window · ", f"<li>Show window {self.SHOW_ID} · ")
        with mock.patch.object(campaigns, "_render_report_html", old):
            first = self._freeze()
        before = campaigns.get_customer_report_html(self.con, self.cid, first["report_no"])
        self.assertIn(f"<li>Show window {self.SHOW_ID} · Mar 1", _summary(before[0]))
        second = self._freeze()
        after = campaigns.get_customer_report_html(self.con, self.cid, first["report_no"])
        self.assertEqual(after, before)
        self.assertEqual(after[1], first["digest"])
        self.assertNotIn(self.SHOW_ID, _summary(campaigns.get_customer_report_html(
            self.con, self.cid, second["report_no"])[0]))

    def test_two_windows_with_the_same_dates_get_numbered_lines(self):
        # R-01: several shows bought for one flight must not print identical
        # Summary lines; numbering only the clashing labels keeps them apart.
        projection = json.loads(self.con.execute(
            "SELECT brief_ref_json FROM campaigns WHERE id=?", (self.cid,)).fetchone()[0])
        for show in ("cs-bbbbbbbbbbbb", None):
            campaigns.save_expected_placement(
                self.con, self.cid, scope="show_window", show_id=show or "cs-cccccccccccc",
                episode_id=None, date_from="2026-03-01" if show else "2026-04-01",
                date_to="2026-03-31" if show else "2026-04-30", timezone_name="UTC",
                expected_count=1, requirements={"duration_s": [5, 30]},
                provenance={"brief_ref": {"kind": "supplied_brief", "brief_id": projection["brief_id"],
                                          "version": projection["version"]}},
                priority=0, actor="owner")
        frozen = self._freeze()
        summary = _summary(campaigns.get_customer_report_html(self.con, self.cid, frozen["report_no"])[0])
        lines = re.findall(r"<li>(Show window[^—]*) — ", summary)
        self.assertEqual(sorted(lines), ["Show window 1 · Mar 1–Mar 31, 2026",
                                         "Show window 2 · Mar 1–Mar 31, 2026",
                                         "Show window · Apr 1–Apr 30, 2026"])
        self.assertNotIn("cs-", summary)


def _style_hash(body):
    css = re.search(r"<style>(.*?)</style>", body, re.S).group(1)
    return "'sha256-" + base64.b64encode(hashlib.sha256(css.encode()).digest()).decode() + "'"


class LocalReportPolicyTest(unittest.TestCase):
    """scripts/app_server.py keeps its sandbox policy and appends only the
    report's own style hash."""

    def _headers(self, style_src):
        from scripts import app_server as srv
        sent = []
        handler = srv.Handler.__new__(srv.Handler)
        handler.send_response = lambda status: None
        handler.send_header = lambda k, v: sent.append((k, v))
        handler.end_headers = lambda: None
        handler.wfile = __import__("io").BytesIO()
        body = "<html><head><style>p{color:red}</style></head></html>"
        handler._send_report_html(body, hashlib.sha256(body.encode()).hexdigest(), False, "1", style_src)
        return dict(sent)["Content-Security-Policy"]

    def test_policy_with_and_without_style_hash(self):
        base = ("sandbox; default-src 'none'; base-uri 'none';"
                " form-action 'none'; frame-ancestors 'self'")
        self.assertEqual(self._headers(""), base)
        self.assertEqual(self._headers("style-src 'sha256-abc'"), base + "; style-src 'sha256-abc'")


class HostedReportPolicyTest(unittest.TestCase):
    """The hosted report route serves the same bytes and allows only the
    new-freeze report's own style block."""

    def setUp(self):
        from tests.test_backend_http import HostedHttpTest
        self._harness = HostedHttpTest
        HostedHttpTest.setUp(self)

    def tearDown(self):
        self._harness.tearDown(self)

    def _call(self, *args, **kwargs):
        return self._harness._call(self, *args, **kwargs)

    def test_new_freeze_html_policy_allows_its_style_by_hash(self):
        _, _, brief = self._call("/api/v1/customer/briefs", "POST",
                                 {"title": "B", "provided_text": "Say lumen."})
        bref = {"kind": "supplied_brief", "brief_id": brief["brief_id"], "version": 1}
        _, _, camp = self._call("/api/v1/customer/campaigns", "POST", {"campaign": {
            "name": "Hosted v3", "mode": "supplied_requirements", "status": "active", "brand": "lumen",
            "source_query": None, "message": None, "offer": None, "notes": "",
            "requirement_provenance": {"brief_ref": bref}}})
        cid = camp["campaign"]["id"]
        _, _, cur = self._call(f"/api/v1/customer/campaigns/{cid}")
        self._call(f"/api/v1/customer/campaigns/{cid}/placements", "POST",
                   {"placement": {"scope": "episode", "episode_id": "e1", "timezone": "UTC",
                                  "expected_count": 1, "binding": "eligible", "requirements": {},
                                  "provenance": {"brief_ref": bref}},
                    "expected_campaign_revision": cur["campaign"]["revision"]})
        _, _, draft = self._call(f"/api/v1/customer/campaigns/{cid}/draft")
        _, _, frozen = self._call(f"/api/v1/customer/campaigns/{cid}/reports", "POST",
                                  {"expected_revision": draft["draft"]["campaign"]["revision"],
                                   "pins": draft["draft"]["pins"]})
        status, headers, data = self._call(
            f"/api/v1/customer/campaigns/{cid}/reports/{frozen['report_no']}/html")
        self.assertEqual(status, 200, data)
        body = data["_raw_body"]
        self.assertIn("<h2>Summary</h2>", body)
        self.assertEqual(hashlib.sha256(body.encode()).hexdigest(), frozen["digest"])
        policy = headers["content-security-policy"]
        self.assertEqual(policy, "default-src 'none'; style-src " + _style_hash(body))
        self.assertNotIn("unsafe-inline", policy)


if __name__ == "__main__":
    unittest.main()


class ResearchQueryUnitLabelTest(unittest.TestCase):
    """c7 (FIVE-TASK T2 Q2): a research campaign's query unit (captured
    "source_query") was headed "Show window N" next to its real windows.
    It reads "Research query results"; real windows keep "Show window"."""

    def test_the_query_unit_has_its_own_heading(self):
        from tests.test_backend_allocation import _con
        con = _con()
        self.addCleanup(con.close)
        cid = campaigns.save_customer_campaign(con, {"campaign": {
            "name": "Research", "mode": "research", "brand": "Lumen",
            "source_query": {"version": 2, "mode": "mentions", "q": "Lumen"}}})["campaign"]["id"]
        draft = campaigns.build_customer_draft(con, cid)
        self.assertTrue(draft["research_scope"]["refs"])  # fixture: the query matches reads
        out = campaigns.freeze_customer_report(con, cid, draft["campaign"]["revision"],
                                               draft["pins"])
        body, _digest = campaigns.get_customer_report_html(con, cid, out["report_no"])
        heads = re.findall(r"<section><h2>([^<]*)</h2>", body)
        self.assertIn("Research query results", heads)
        self.assertFalse([h for h in heads if h.startswith("Show window")], heads)

    def test_a_show_window_keeps_its_label(self):
        self.assertEqual(campaigns._report_placement_label(
            {"scope": "show_window", "id": "w", "date_from": None, "date_to": None}, {}),
            "Show window")
        self.assertEqual(campaigns._report_placement_label(
            {"scope": "source_query", "id": "source_query"}, {}), "Research query results")
