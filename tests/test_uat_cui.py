"""UAT 2026-10-15 campaign UI fix round (backend half).

VL-06: the live draft names the shows its placements and research targets
point at (show_titles); frozen payloads never carry it.
VL-25 / V-17: the campaign list, detail and draft carry the brand's display
name, the same one Overview shows.
VL-10: a legacy campaign (no mode) reads "Read-only", never "Tracking".
VL-09 / V-36: the Overview placement label formats its dates like the
campaign page and says "expects N ad(s)" (d16 R2-01: the unit is an ad).
B-8: a frozen Summary with nothing counted says so plainly (new reports
only; stored reports keep their bytes).
Placement order: a new frozen Summary lists placements by window start date.
Malformed dates: an episode whose publication date is not a real ISO date
is undated for placement scope and window matching.
Archive: the customer campaign route sets status archived/active with the
usual revision rule, hosted and local.
"""
from __future__ import annotations

import io
import json
import re
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from earwitness.app import campaigns, customer, db, ops
from tests.test_customer_campaigns import fresh_db, seed_campaign, seed_episode


def _rev(con, cid):
    return con.execute("SELECT revision FROM campaigns WHERE id=?", (cid,)).fetchone()[0]


def _brief_ref(con, cid):
    projection = json.loads(con.execute(
        "SELECT brief_ref_json FROM campaigns WHERE id=?", (cid,)).fetchone()[0])
    return {"kind": "supplied_brief", "brief_id": projection["brief_id"],
            "version": projection["version"]}


def _window(con, cid, show, date_from="2026-03-01", date_to="2026-03-31", **kw):
    return campaigns.save_expected_placement(
        con, cid, scope="show_window", show_id=show, episode_id=None,
        date_from=date_from, date_to=date_to, timezone_name="UTC", expected_count=1,
        requirements={"duration_s": [5, 30]},
        provenance={"brief_ref": _brief_ref(con, cid), **kw}, priority=0,
        actor="owner")["placement_id"]


def _show_of(con, ep_id="ep-1"):
    return customer.customer_show_for_episode(con, ep_id)["show_identity_id"]


def _summary(body):
    return re.search(r"<section><h2>Summary</h2>(.*?)</section>", body, re.S).group(1)


class DraftNamesTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con, show="Casefile")
        self.cid = seed_campaign(self.con, brand="kachava")
        self.show = _show_of(self.con)

    def tearDown(self):
        self.con.close()

    def test_draft_names_the_placement_show(self):
        _window(self.con, self.cid, self.show)
        draft = campaigns.build_customer_draft(self.con, self.cid)
        self.assertEqual(draft["show_titles"], {self.show: "Casefile"})

    def test_show_titles_list_only_shows_in_scope(self):
        draft = campaigns.build_customer_draft(self.con, self.cid)
        self.assertEqual(draft["show_titles"], {})

    def test_research_target_show_is_named(self):
        created = campaigns.save_customer_campaign(self.con, {"campaign": {
            "name": "Research", "mode": "research", "brand": "Acme", "notes": ""}})
        rid = created["campaign"]["id"]
        campaigns.create_target(self.con, rid, "show_window", show_id=self.show,
                                date_from="2026-03-01", date_to="2026-03-31",
                                expected_campaign_revision=_rev(self.con, rid))
        draft = campaigns.build_customer_draft(self.con, rid)
        self.assertEqual(draft["show_titles"], {self.show: "Casefile"})

    def test_frozen_payload_never_carries_show_titles_or_brand_display(self):
        _window(self.con, self.cid, self.show)
        draft = campaigns.build_customer_draft(self.con, self.cid)
        frozen = campaigns.freeze_customer_report(
            self.con, self.cid, _rev(self.con, self.cid), draft["pins"])
        stored = self.con.execute(
            "SELECT report_json FROM campaign_reports WHERE campaign_id=? AND report_no=?",
            (self.cid, frozen["report_no"])).fetchone()[0]
        self.assertNotIn("show_titles", stored)
        self.assertNotIn("brand_display", stored)

    def test_draft_campaign_carries_the_brand_display(self):
        draft = campaigns.build_customer_draft(self.con, self.cid)
        self.assertEqual(draft["campaign"]["brand"], "kachava")
        self.assertEqual(draft["campaign"]["brand_display"], "Kachava")

    def test_list_and_detail_carry_the_same_brand_display_as_overview(self):
        row = dict(self.con.execute("SELECT * FROM campaigns WHERE id=?", (self.cid,)).fetchone())
        listed = campaigns.customer_list_summaries(self.con, [dict(row)])[0]
        detail = campaigns.campaign_projection(self.con, dict(row))
        card = next(c for c in customer.customer_campaign_cards(self.con) if c["id"] == self.cid)
        self.assertEqual(listed["brand_display"], "Kachava")
        self.assertEqual(detail["brand_display"], "Kachava")
        self.assertEqual(card["brand_display"], "Kachava")

    def test_curated_spelling_wins_and_no_brand_stays_empty(self):
        from earwitness.app.customer import BRAND_DISPLAY_NAMES
        key, shown = next(iter(BRAND_DISPLAY_NAMES.items()))
        self.assertEqual(campaigns.campaign_brand_display(self.con, key), shown)
        self.assertIsNone(campaigns.campaign_brand_display(self.con, None))
        self.assertIsNone(campaigns.campaign_brand_display(self.con, ""))


class ReadOnlyStateTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con)

    def tearDown(self):
        self.con.close()

    def test_legacy_campaign_reads_read_only_not_tracking(self):
        cid = self.con.execute(
            "INSERT INTO campaigns (name, status, mode, updated_at) VALUES ('Old', 'active', NULL, ?)",
            (db.utcnow(),)).lastrowid
        campaigns.save_expected_placement(self.con, cid, expected_count=1,
                                          provenance={"brief_ref": "unregistered-original"})
        self.assertEqual(customer.campaign_customer_state(self.con, cid), "Read-only")
        rows = campaigns.customer_list_summaries(self.con, [dict(self.con.execute(
            "SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone())])
        self.assertEqual(rows[0]["state"], "Read-only")

    def test_active_campaigns_keep_their_states(self):
        cid = seed_campaign(self.con)
        self.assertEqual(customer.campaign_customer_state(self.con, cid), "Draft")
        _window(self.con, cid, _show_of(self.con))
        self.assertEqual(customer.campaign_customer_state(self.con, cid), "Tracking")


class OverviewLabelTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con, show="Casefile")

    def tearDown(self):
        self.con.close()

    def test_window_label_formats_dates_and_ads(self):
        p = {"scope": "show_window", "show_id": "cs-1", "date_from": "2026-09-01",
             "date_to": "2026-09-30", "expected_count": 1}
        self.assertEqual(customer._placement_scope_label(self.con, p, {"cs-1": "Casefile"}),
                         "Casefile · Sep 1–Sep 30, 2026 · expects 1 ad")
        p.update(expected_count=2, date_from="2025-12-01", date_to=None)
        self.assertEqual(customer._placement_scope_label(self.con, p, {}),
                         "Show window · Dec 1, 2025–latest · expects 2 ads")

    def test_overview_gap_uses_the_label(self):
        cid = seed_campaign(self.con)
        pid = _window(self.con, cid, _show_of(self.con))
        gap = next(g for g in customer.overview(self.con)["campaign_gaps"] if g["placement_id"] == pid)
        self.assertEqual(gap["placement_label"], "Casefile · Mar 1–Mar 31, 2026 · expects 1 ad")


class OverviewOrderTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con, show="Casefile")

    def tearDown(self):
        self.con.close()

    def test_overview_gaps_follow_the_window_dates(self):
        cid = seed_campaign(self.con)
        show = _show_of(self.con)
        for start, end in (("2026-05-01", "2026-05-31"), ("2026-03-01", "2026-03-31"),
                           ("2026-04-01", "2026-04-30")):
            _window(self.con, cid, show, start, end)
        labels = [g["placement_label"] for g in customer.overview(self.con)["campaign_gaps"]
                  if g["campaign_id"] == cid]
        self.assertEqual([l.split(" · ")[1][:3] for l in labels], ["Mar", "Apr", "May"])


class CitationVerdictTest(unittest.TestCase):
    """B-8: nothing counted reads plainly in new frozen Summaries."""

    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con)
        self.cid = seed_campaign(self.con)
        self.show = _show_of(self.con)

    def tearDown(self):
        self.con.close()

    def _freeze(self):
        draft = campaigns.build_customer_draft(self.con, self.cid)
        return campaigns.freeze_customer_report(
            self.con, self.cid, _rev(self.con, self.cid), draft["pins"])

    def _body(self, frozen):
        return campaigns.get_customer_report_html(self.con, self.cid, frozen["report_no"])[0]

    def test_one_citation_only(self):
        _window(self.con, self.cid, self.show, binding="citation_only")
        summary = _summary(self._body(self._freeze()))
        self.assertIn("No placement is counted for credit. This placement is a citation only, never counted.", summary)
        self.assertNotIn("1 more", summary)

    def test_several_citations_only(self):
        _window(self.con, self.cid, self.show, binding="citation_only")
        _window(self.con, self.cid, self.show, "2026-04-01", "2026-04-30", binding="citation_only")
        summary = _summary(self._body(self._freeze()))
        self.assertIn("No placement is counted for credit. All 2 placements are citations only, never counted.", summary)

    def test_some_counted_keeps_the_more_tail(self):
        _window(self.con, self.cid, self.show)
        _window(self.con, self.cid, self.show, "2026-04-01", "2026-04-30", binding="citation_only")
        summary = _summary(self._body(self._freeze()))
        self.assertIn("1 more is a citation only, never counted.", summary)

    def test_a_stored_report_keeps_its_bytes(self):
        _window(self.con, self.cid, self.show, binding="citation_only")
        render = campaigns._render_report_html
        old = lambda frozen: render(frozen).replace(
            "No placement is counted for credit. This placement is a citation only, never counted.",
            "No placement is counted for credit yet. 1 more is a citation only, never counted.")
        with mock.patch.object(campaigns, "_render_report_html", old):
            first = self._freeze()
        before = campaigns.get_customer_report_html(self.con, self.cid, first["report_no"])
        self.assertIn("counted for credit yet. 1 more is a citation only", before[0])
        self._freeze()
        self.assertEqual(campaigns.get_customer_report_html(self.con, self.cid, first["report_no"]), before)


class ReportOrderTest(unittest.TestCase):
    """New frozen reports list placements by window start date."""

    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con)
        self.cid = seed_campaign(self.con)
        self.show = _show_of(self.con)

    def tearDown(self):
        self.con.close()

    def test_summary_and_sections_follow_the_window_dates(self):
        for start, end in (("2026-05-01", "2026-05-31"), ("2026-03-01", "2026-03-31"),
                           ("2026-04-01", "2026-04-30")):
            _window(self.con, self.cid, self.show, start, end)
        draft = campaigns.build_customer_draft(self.con, self.cid)
        frozen = campaigns.freeze_customer_report(
            self.con, self.cid, _rev(self.con, self.cid), draft["pins"])
        body = campaigns.get_customer_report_html(self.con, self.cid, frozen["report_no"])[0]
        months = re.findall(r"<li>Show window · (\w{3}) 1", _summary(body))
        self.assertEqual(months, ["Mar", "Apr", "May"])
        heads = re.findall(r"<section><h2>Show window · (\w{3}) 1", body)
        self.assertEqual(heads, ["Mar", "Apr", "May"])


class MalformedDateTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()

    def tearDown(self):
        self.con.close()

    def test_malformed_publication_date_is_undated_for_scope(self):
        seed_episode(self.con, ep_id="ep-ok", pub_date="2026-03-10")
        seed_episode(self.con, ep_id="ep-bad", pub_date="2026-13-45", version_no=2)
        seed_episode(self.con, ep_id="ep-word", pub_date="unknown", version_no=3)
        # One show holds all three (the fixture feed keeps only the last).
        show = _show_of(self.con, "ep-ok")
        members = mock.patch.object(customer, "resolve_customer_show",
                                    return_value=["ep-bad", "ep-ok", "ep-word"])
        members.start()
        self.addCleanup(members.stop)
        for window in (("2026-01-01", "2026-12-31"), (None, None), ("2026-03-01", None)):
            eps, problem = campaigns._placement_scope_episodes(self.con, {
                "scope": "show_window", "show_id": show,
                "date_from": window[0], "date_to": window[1]})
            self.assertIsNone(problem)
            self.assertEqual(eps, ["ep-ok"], window)

    def test_malformed_occurrence_date_is_uncertain_not_in_window(self):
        placement = {"scope": "show_window", "date_from": "2026-01-01", "date_to": "2026-12-31",
                     "timezone": "UTC"}
        ok, reason = campaigns._in_window({"pub_date": "2026-13-45", "date_precision": "date"}, placement)
        self.assertFalse(ok)
        self.assertEqual(reason.code, "date_uncertain")
        self.assertEqual(campaigns._in_window({"pub_date": "2026-03-10", "date_precision": "date"}, placement),
                         (True, None))


class ArchiveBackendTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con)
        self.cid = seed_campaign(self.con)

    def tearDown(self):
        self.con.close()

    def test_archive_and_restore_with_the_revision_rule(self):
        rev = _rev(self.con, self.cid)
        out = campaigns.save_customer_campaign(self.con, {"campaign": {"status": "archived"}}, self.cid, rev)
        self.assertEqual(out["campaign"]["status"], "archived")
        self.assertEqual(customer.campaign_customer_state(self.con, self.cid), "Archived")
        with self.assertRaises(ops.Conflict):
            campaigns.save_customer_campaign(self.con, {"campaign": {"status": "active"}}, self.cid, rev)
        campaigns.save_customer_campaign(self.con, {"campaign": {"status": "active"}}, self.cid,
                                         _rev(self.con, self.cid))
        self.assertEqual(customer.campaign_customer_state(self.con, self.cid), "Draft")


class LocalRoutesTest(unittest.TestCase):
    """scripts/app_server.py serves the new fields and the archive write."""

    PORT = 18797
    HOST = f"127.0.0.1:{PORT}"
    CSRF = "csrf-local"

    def setUp(self):
        from scripts import app_server as srv
        self.srv = srv
        self.con = fresh_db()
        seed_episode(self.con, show="Casefile")
        self.cid = seed_campaign(self.con, brand="kachava")
        self.show = _show_of(self.con)
        _window(self.con, self.cid, self.show)
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

    def test_list_detail_and_draft_carry_names(self):
        status, data = self._req("GET", "/api/v1/customer/campaigns")
        self.assertEqual(status, 200, data)
        row = next(c for c in data["campaigns"] if c["id"] == self.cid)
        self.assertEqual(row["brand_display"], "Kachava")
        status, data = self._req("GET", f"/api/v1/customer/campaigns/{self.cid}")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["campaign"]["brand_display"], "Kachava")
        status, data = self._req("GET", f"/api/v1/customer/campaigns/{self.cid}/draft")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["draft"]["show_titles"], {self.show: "Casefile"})
        self.assertEqual(data["draft"]["campaign"]["brand_display"], "Kachava")

    def test_archive_and_restore_through_the_route(self):
        rev = _rev(self.con, self.cid)
        status, data = self._req("PUT", f"/api/v1/customer/campaigns/{self.cid}",
                                 {"campaign": {"status": "archived"}, "expected_revision": rev})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["campaign"]["status"], "archived")
        status, data = self._req("PUT", f"/api/v1/customer/campaigns/{self.cid}",
                                 {"campaign": {"status": "active"}, "expected_revision": rev})
        self.assertIn(status, (409, 412), data)
        status, data = self._req("GET", "/api/v1/customer/campaigns")
        self.assertEqual(next(c for c in data["campaigns"] if c["id"] == self.cid)["state"], "Archived")


class HostedRoutesTest(unittest.TestCase):
    """The hosted customer routes serve the same fields and write."""

    def setUp(self):
        from tests.test_backend_http import HostedHttpTest
        self._harness = HostedHttpTest
        HostedHttpTest.setUp(self)
        con = sqlite3.connect(self.db_path, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            seed_episode(con, ep_id="ep-h", show="Casefile", version_no=99)
            self.cid = seed_campaign(con, brand="kachava")
            self.show = _show_of(con, "ep-h")
            _window(con, self.cid, self.show)
            self.rev = _rev(con, self.cid)
        finally:
            con.close()

    def tearDown(self):
        self._harness.tearDown(self)

    def _call(self, *args, **kwargs):
        return self._harness._call(self, *args, **kwargs)

    def test_list_detail_and_draft_carry_names(self):
        status, _h, data = self._call("/api/v1/customer/campaigns")
        self.assertEqual(status, 200, data)
        self.assertEqual(next(c for c in data["campaigns"] if c["id"] == self.cid)["brand_display"], "Kachava")
        status, _h, data = self._call(f"/api/v1/customer/campaigns/{self.cid}")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["campaign"]["brand_display"], "Kachava")
        status, _h, data = self._call(f"/api/v1/customer/campaigns/{self.cid}/draft")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["draft"]["show_titles"], {self.show: "Casefile"})

    def test_archive_and_restore_through_the_route(self):
        status, _h, data = self._call(f"/api/v1/customer/campaigns/{self.cid}", "PUT",
                                      {"campaign": {"status": "archived"}, "expected_revision": self.rev})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["campaign"]["status"], "archived")
        status, _h, data = self._call(f"/api/v1/customer/campaigns/{self.cid}", "PUT",
                                      {"campaign": {"status": "active"}, "expected_revision": self.rev})
        self.assertIn(status, (409, 412), data)
        status, _h, data = self._call("/api/v1/customer/campaigns")
        self.assertEqual(next(c for c in data["campaigns"] if c["id"] == self.cid)["state"], "Archived")


if __name__ == "__main__":
    unittest.main()
