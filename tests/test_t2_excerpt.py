"""T2-EXCERPT: a frozen report under an excerpt-only export rule shows an
ad's excerpt cut to its pinned reviewed bounds, never the detector excerpt
of the wider original bounds; and a research report goes stale on its
query or refs, not on the view revision every guarded write moves.

Fixture: tests/backend_fixtures (episode e1: 0-10 "Welcome to the show",
10-20 "This episode is brought to you by Lumen", 20-30 "Enjoy your cafe
lumen blend"; fixture-ad orig 10-20). Each excerpt case reshapes
fixture-ad's original bounds and stored (pinned) excerpt, saves a review
revision through ops.save_review (which publishes the pinned state), and
freezes a research report through campaigns.freeze_customer_report with
the export rule narrowed to excerpts (exports.export_locators).
"""
from __future__ import annotations

import unittest
from unittest import mock

from earwitness.app import campaigns, citations, exports, ops

WELCOME = "Welcome to the show"
AD = "This episode is brought to you by Lumen"
CAFE = "Enjoy your cafe lumen blend"
STORED = "Stored detector words, kept byte for byte:  Welcome  …"


def _excerpt_only(con, ref, ctx=None):
    return exports._as_excerpt(citations.captured_disclosure(con, ref, ctx)), False


class ReviewedExcerptTest(unittest.TestCase):

    def setUp(self):
        from tests.test_backend_allocation import _con
        self.con = _con()
        self.addCleanup(self.con.close)

    def shape(self, orig, stored, eff=None):
        """fixture-ad with these original bounds and stored excerpt (live
        row and its pinned history row), then a review revision with eff
        bounds (None: a judgment-only review, effective = original)."""
        self.con.execute("UPDATE evidence SET orig_start_s=?, orig_end_s=?, excerpt=?"
                         " WHERE id='fixture-ad'", (orig[0], orig[1], stored))
        self.con.execute("UPDATE evidence_history SET orig_start_s=?, orig_end_s=?, excerpt=?"
                         " WHERE evidence_id='fixture-ad'", (orig[0], orig[1], stored))
        rev = self.con.execute("SELECT revision FROM reviews WHERE evidence_id='fixture-ad'"
                               ).fetchone()[0]
        body = {"judgment": "ad"}
        if eff is not None:
            body.update(eff_start_s=eff[0], eff_end_s=eff[1])
        ops.save_review(self.con, "fixture-ad", body, rev)

    def frozen_text(self):
        ref = citations.current_source_ref(self.con, "evidence", "fixture-ad")
        camp = campaigns.save_customer_campaign(self.con, {"campaign": {
            "name": "T2", "mode": "research", "brand": "Lumen"}})["campaign"]
        target = campaigns.create_target(self.con, camp["id"], "episode", episode_id="e1",
                                         actor="owner",
                                         expected_campaign_revision=camp["revision"])
        rev = self.con.execute("SELECT revision FROM campaigns WHERE id=?",
                               (camp["id"],)).fetchone()[0]
        campaigns.attach_target_evidence(self.con, camp["id"], target["id"], ref, rev)
        draft = campaigns.build_customer_draft(self.con, camp["id"])
        with mock.patch.object(exports, "export_locators", _excerpt_only):
            frozen = campaigns.freeze_customer_report(
                self.con, camp["id"], draft["campaign"]["revision"], draft["pins"],
                ctx=citations._owner_context())
            report = campaigns.get_customer_report(self.con, camp["id"], frozen["report_no"],
                                                   ctx=citations._owner_context())["frozen"]
        captures = report["captured"][target["id"]]
        self.assertEqual([c["capture_scope"] for c in captures], ["export_excerpt"])
        self.assertEqual(captures[0]["capture_digest"], citations.capture_digest(captures[0]))
        return captures[0]["text"]

    # -- narrowed reads ------------------------------------------------------

    def test_c9_narrowed_start_drops_the_ad_before_it(self):
        # c9 ShipStation: orig 1800-1835.2, reviewed 1805.36-1835.2; the
        # stored excerpt opens on 4.2 s of a Wayfair ad.
        self.shape((0.0, 20.0), WELCOME + " " + AD, eff=(10.0, 20.0))
        text = self.frozen_text()
        self.assertEqual(text, AD)
        self.assertNotIn("Welcome", text)

    def test_c7_p18_narrowed_end_drops_the_ad_after_it(self):
        # c7 P18 (reviewed inside the original, one edge moved).
        self.shape((10.0, 30.0), AD + " " + CAFE, eff=(10.0, 20.0))
        text = self.frozen_text()
        self.assertEqual(text, AD)
        self.assertNotIn("cafe", text)

    def test_c7_p4_reviewed_end_past_the_original_keeps_only_the_overlap(self):
        # c7 P4: orig 2880-2940, reviewed 2922-2952.8: the excerpt is the
        # overlap 2922-2940, never text past the original end.
        self.shape((0.0, 20.0), WELCOME + " " + AD, eff=(10.0, 30.0))
        text = self.frozen_text()
        self.assertEqual(text, AD)
        self.assertNotIn("Welcome", text)
        self.assertNotIn("cafe", text)

    def test_a_segment_crossing_the_reviewed_edge_is_left_out(self):
        # 0-10 overlaps 9.8-20 but starts before 9.8 - 0.5: wholly-inside rule.
        self.shape((0.0, 20.0), WELCOME + " " + AD, eff=(9.8, 20.0))
        self.assertEqual(self.frozen_text(), AD)

    def test_a_segment_past_the_original_edge_is_left_out(self):
        # 10-20 lies within the reviewed 10-19.8 by the 0.5 s rule but runs
        # past the original end 19.8: never wider than the original read.
        self.shape((0.0, 19.8), WELCOME + " " + AD, eff=(10.0, 19.8))
        self.assertEqual(self.frozen_text(), "")

    def test_no_segment_inside_the_narrowed_bounds_gives_an_empty_excerpt(self):
        # Fallback: the stored text is known to run past the reviewed bounds.
        self.shape((0.0, 30.0), WELCOME + " " + AD + " " + CAFE, eff=(12.0, 18.0))
        self.assertEqual(self.frozen_text(), "")

    def test_segments_without_finite_ordered_timings_are_left_out(self):
        # r1 F1/F2: a segment with no, null, zero, string, NaN or infinite
        # timing, or a reversed one, is never kept (nor raises).
        from earwitness.app import observations
        self.shape((0.0, 20.0), WELCOME + " " + AD, eff=(10.0, 20.0))
        good = {"start": 10.0, "end": 20.0, "text": AD}
        bad = [{"text": "LEAK"}, {"start": 12.0, "text": "LEAK"},
               {"start": 12.0, "end": None, "text": "LEAK"},
               {"start": None, "end": 14.0, "text": "LEAK"},
               {"start": 12.0, "end": 0, "text": "LEAK"},
               {"start": "13", "end": "14", "text": "LEAK"},
               {"start": float("nan"), "end": 14.0, "text": "LEAK"},
               {"start": 12.0, "end": float("inf"), "text": "LEAK"},
               {"start": True, "end": 14.0, "text": "LEAK"},
               {"start": 14.0, "end": 13.0, "text": "LEAK"}]
        for segment in bad:
            with self.subTest(segment=segment), \
                    mock.patch.object(observations, "_segments_of",
                                      lambda con, vid, seg=segment: [seg, good]):
                self.assertEqual(self.frozen_text(), AD)

    def test_only_the_pinned_transcript_version_is_read(self):
        # A newer transcript version of the episode (other words) is never used.
        from tests import backend_fixtures
        newer = backend_fixtures._add_version(self.con, "transcript:e1", {"segments": [
            {"start": 10.0, "end": 20.0, "text": "LEAK newer transcript words"}]})
        pinned = self.con.execute("SELECT transcript_version_id FROM evidence"
                                  " WHERE id='fixture-ad'").fetchone()[0]
        self.assertNotEqual(newer, pinned)
        self.shape((0.0, 20.0), WELCOME + " " + AD, eff=(10.0, 20.0))
        self.assertEqual(self.frozen_text(), AD)

    def test_reversed_or_non_finite_bounds_keep_the_stored_excerpt(self):
        nan, inf = float("nan"), float("inf")
        for original, effective in (([0.0, 20.0], [18.0, 12.0]), ([20.0, 0.0], [10.0, 15.0]),
                                    ([0.0, 20.0], [nan, 15.0]), ([0.0, 20.0], [10.0, nan]),
                                    ([nan, 20.0], [10.0, 15.0]), ([0.0, inf], [10.0, 15.0]),
                                    ([0.0, 20.0], [None, 15.0]), ([0.0, 20.0, 30.0], [10.0, 15.0])):
            publication = {"bounds": {"original": original, "effective": effective},
                           "projection": {"transcript_version_id": self.con.execute(
                               "SELECT transcript_version_id FROM evidence WHERE id='fixture-ad'"
                           ).fetchone()[0]}}
            with self.subTest(original=original, effective=effective):
                self.assertEqual(campaigns._reviewed_excerpt(self.con, publication, STORED), STORED)

    # -- reads that keep the stored excerpt ----------------------------------

    def test_an_unnarrowed_read_keeps_its_stored_excerpt_byte_for_byte(self):
        self.shape((10.0, 20.0), STORED)
        self.assertEqual(self.frozen_text(), STORED)

    def test_reviewed_bounds_equal_to_the_original_keep_the_stored_excerpt(self):
        self.shape((10.0, 20.0), STORED, eff=(10.0, 20.0))
        self.assertEqual(self.frozen_text(), STORED)

    def test_wider_reviewed_bounds_keep_the_stored_excerpt(self):
        self.shape((10.0, 20.0), STORED, eff=(0.0, 30.0))
        self.assertEqual(self.frozen_text(), STORED)

    def test_reviewed_bounds_outside_the_original_keep_the_stored_excerpt(self):
        self.shape((10.0, 20.0), STORED, eff=(20.0, 30.0))
        self.assertEqual(self.frozen_text(), STORED)

    def test_the_stored_excerpt_is_still_capped(self):
        long = (AD + " ") * 20
        self.shape((10.0, 20.0), long)
        self.assertEqual(self.frozen_text(), exports.cap_excerpt(long))


class ResearchScopeStaleTest(unittest.TestCase):

    def setUp(self):
        from tests.test_backend_allocation import _con
        self.con = _con()
        self.addCleanup(self.con.close)

    def test_a_write_that_leaves_the_refs_unchanged_is_not_stale(self):
        camp = campaigns.save_customer_campaign(self.con, {"campaign": {
            "name": "R", "mode": "research", "brand": "Lumen",
            "source_query": {"version": 2, "mode": "mentions", "q": "Lumen"}}})["campaign"]
        target = campaigns.create_target(self.con, camp["id"], "episode", episode_id="e1",
                                         actor="owner",
                                         expected_campaign_revision=camp["revision"])
        rev = self.con.execute("SELECT revision FROM campaigns WHERE id=?",
                               (camp["id"],)).fetchone()[0]
        campaigns.attach_target_evidence(self.con, camp["id"], target["id"],
                                         citations.current_source_ref(self.con, "evidence",
                                                                      "fixture-ad"), rev)
        draft = campaigns.build_customer_draft(self.con, camp["id"])
        frozen = campaigns.freeze_customer_report(self.con, camp["id"],
                                                  draft["campaign"]["revision"], draft["pins"])
        report = campaigns.get_customer_report(self.con, camp["id"], frozen["report_no"])
        self.assertEqual((report["stale"], report["stale_reasons"]), (False, []))
        # A guarded workspace write (a review note) moves the view revision only.
        review = self.con.execute("SELECT revision FROM reviews WHERE evidence_id='fixture-ad'"
                                  ).fetchone()[0]
        ops.save_review(self.con, "fixture-ad", {"notes": "checked"}, review)
        after = campaigns.build_customer_draft(self.con, camp["id"])["pins"]["research_scope"]
        before = draft["pins"]["research_scope"]
        self.assertNotEqual(after["revision"], before["revision"])
        self.assertEqual((after["query"], after["refs"]), (before["query"], before["refs"]))
        report = campaigns.get_customer_report(self.con, camp["id"], frozen["report_no"])
        self.assertEqual((report["stale"], report["stale_reasons"]), (False, []))

    def scope(self, query="q", revision="r1", refs=("a", "b")):
        return {"brief_digest": "d", "campaign_revision": 1,
                "research_scope": {"query": {"q": query}, "revision": revision,
                                   "refs": list(refs)}}

    def test_only_the_revision_changed_is_not_stale(self):
        self.assertEqual(campaigns._stale_reasons(self.scope(revision="r2"), self.scope()), [])

    def test_a_refs_change_is_stale(self):
        for refs in (("a",), ("a", "b", "c"), ("b", "a")):
            self.assertEqual(campaigns._stale_reasons(self.scope(refs=refs), self.scope()),
                             ["research_scope"], refs)
            self.assertEqual(campaigns._stale_reasons(self.scope(refs=refs, revision="r2"),
                                                      self.scope()), ["research_scope"], refs)

    def test_a_query_change_is_stale(self):
        self.assertEqual(campaigns._stale_reasons(self.scope(query="other"), self.scope()),
                         ["research_scope"])

    def test_a_scope_added_or_removed_is_stale_and_none_is_not(self):
        none = {"brief_digest": "d", "campaign_revision": 1, "research_scope": None}
        self.assertEqual(campaigns._stale_reasons(none, self.scope()), ["research_scope"])
        self.assertEqual(campaigns._stale_reasons(self.scope(), none), ["research_scope"])
        self.assertEqual(campaigns._stale_reasons(none, dict(none)), [])


if __name__ == "__main__":
    unittest.main()
