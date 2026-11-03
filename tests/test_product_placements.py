"""P13 product regressions: placements (stdlib unittest)."""
from __future__ import annotations

import sqlite3
import unittest

from earwitness.app import campaigns, db
from earwitness.app import ops


def fresh_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON;")
    db.init_schema(con)
    return con


def seed_campaign(con, cid=1):
    con.execute("INSERT INTO campaigns (id, name, status, target_shows, message, offer,"
                " notes, manual_reason, revision, updated_at)"
                " VALUES (?, 'Nike', 'active', '[]', '', '', '', '', 1, ?)",
                (cid, db.utcnow()))
    con.commit()


SYNTH = {"synthetic": True, "brief_type": "synthetic"}
REAL = {"brief_ref": "brief-2026-001"}


def occ(oid, **kw):
    d = {"id": oid, "correspondence": "confirmed",
         "review_status": "reviewed_ad", "advertiser_confirmed": True,
         "published_at": "2026-03-10T12:00:00+00:00", "episode_id": "ep-1",
         "source_matched": True, "alignment_verified": True,
         "bounds_verified": True, "full_episode_duration_s": 3600.0,
         "duration_s": 30.0, "position_s": 120.0,
         "fields": {}, "verified_fields": [], "date_precision": "instant"}
    d.update(kw)
    return d


def placement(pid, **kw):
    d = {"id": pid, "scope": "show_window", "show_id": "show-1",
         "episode_id": None, "date_from": "2026-03-01", "date_to": "2026-03-31",
         "timezone": "America/New_York", "expected_count": 1,
         "requirements": {}, "provenance": dict(REAL)}
    d.update(kw)
    return d


class PlacementEvalTest(unittest.TestCase):
    def test_supported_with_eligible(self):
        p = placement("p-1")
        ev = campaigns.evaluate_one(p, [occ("o-1")], {"state": "complete"},
                                    "2026-04-01T00:00:00+00:00")
        self.assertEqual(ev["outcome"], "supported")

    def test_missing_source_vs_no_match_distinct(self):
        p = placement("p-1")
        inc = campaigns.evaluate_one(p, [], {"state": "incomplete",
                                             "missing": ["feed-x"], "failed": []},
                                     "2026-04-01T00:00:00+00:00")
        nom = campaigns.evaluate_one(p, [], {"state": "complete"},
                                     "2026-04-01T00:00:00+00:00")
        self.assertEqual(inc["outcome"], "incomplete_coverage")
        self.assertEqual(nom["outcome"], "no_observation_in_covered_sources")
        self.assertNotEqual(inc["outcome"], nom["outcome"])

    def test_pending_while_window_open(self):
        p = placement("p-1", date_to="2099-12-31")
        ev = campaigns.evaluate_one(p, [], {"state": "complete"},
                                    "2026-04-01T00:00:00+00:00")
        self.assertEqual(ev["outcome"], "pending")

    def test_insufficient_after_close_is_ambiguous_count(self):
        p = placement("p-1", expected_count=3)
        ev = campaigns.evaluate_one(p, [occ("o-1")], {"state": "complete"},
                                    "2026-04-01T00:00:00+00:00")
        self.assertEqual(ev["outcome"], "ambiguous")
        self.assertIn(campaigns.INSUFFICIENT_SUPPORTED_COUNT, ev["reasons"])

    def test_unverified_required_field_never_satisfied(self):
        p = placement("p-1", requirements={"message": "20% off"})
        ev = campaigns.evaluate_one(p, [occ("o-1", fields={"message": "20% off"})],
                                    {"state": "complete"},
                                    "2026-04-01T00:00:00+00:00")
        # Copy matches but is human-unverified -> unknown -> not eligible.
        self.assertNotEqual(ev["outcome"], "supported")

    def test_uncertain_date_precision_is_ambiguous(self):
        p = placement("p-1")
        ev = campaigns.evaluate_one(
            p, [occ("o-1", date_precision="uncertain")],
            {"state": "complete"}, "2026-04-01T00:00:00+00:00")
        self.assertEqual(ev["outcome"], "ambiguous")

    def test_dai_repeats_do_not_overcount(self):
        p = placement("p-1", expected_count=2)
        # Two fetch snapshots of the SAME occurrence id.
        seen = [occ("o-1"), occ("o-1")]
        uniq = {o["id"]: o for o in seen}.values()
        ev = campaigns.evaluate_one(p, list(uniq), {"state": "complete"},
                                    "2026-04-01T00:00:00+00:00")
        # One distinct occurrence cannot satisfy count 2.
        self.assertNotEqual(ev["outcome"], "supported")

    def test_position_needs_verified_alignment_not_source_matched(self):
        p = placement("p-1", requirements={"position_s": [100, 140]})
        ev = campaigns.evaluate_one(
            p, [occ("o-1", alignment_verified=False)],
            {"state": "complete"}, "2026-04-01T00:00:00+00:00")
        self.assertNotEqual(ev["outcome"], "supported")

    def test_synthetic_stays_labeled(self):
        con = fresh_db()
        try:
            seed_campaign(con)
            out = campaigns.save_expected_placement(
                con, 1, expected_count=1, provenance=dict(SYNTH))
            self.assertTrue(out["synthetic"])
            rows = campaigns.list_expected(con, 1)
            import json
            prov = rows[0]["provenance"]
            self.assertTrue(prov["synthetic"])
            with self.assertRaises(ops.BadInput):
                campaigns.save_expected_placement(con, 1, expected_count=1,
                                                  provenance={})
        finally:
            con.close()

    def test_stale_invalidation_on_input_change(self):
        frozen = {"brief_revision": 1, "source_revision": 2,
                  "attribution_revision": 1, "correspondence_revision": 1,
                  "review_revision": 5, "coverage_revision": 3}
        same = campaigns.mark_stale(dict(frozen), dict(frozen))
        self.assertFalse(same["stale"])
        changed = campaigns.mark_stale({**frozen, "review_revision": 6}, frozen)
        self.assertTrue(changed["stale"])
        self.assertIn("review_revision", changed["changed"])

    def test_allocation_stability_and_competition_visible(self):
        p1 = placement("p-1", requirements={"priority": 5})
        p2 = placement("p-2")
        occs = [occ("o-2"), occ("o-1")]
        r1 = campaigns.allocate_campaign([p1, p2], occs, {"state": "complete"},
                                         "2026-04-01T00:00:00+00:00")
        r2 = campaigns.allocate_campaign([p2, p1], list(reversed(occs)),
                                         {"state": "complete"},
                                         "2026-04-01T00:00:00+00:00")
        self.assertEqual(r1["evaluations"]["p-1"]["outcome"],
                         r2["evaluations"]["p-1"]["outcome"])
        # Each occurrence used at most once across the campaign.
        self.assertEqual(sorted(r1["used"]), ["o-1", "o-2"])
        # Competition visible: single occurrence cannot satisfy two placements.
        solo = campaigns.allocate_campaign([p1, p2], [occ("o-1")],
                                           {"state": "complete"},
                                           "2026-04-01T00:00:00+00:00")
        second = solo["evaluations"]["p-2"]
        self.assertTrue(second["allocated_elsewhere"] == ["o-1"]
                        or second["outcome"] != "supported")

    def test_freeze_retains_history(self):
        con = fresh_db()
        try:
            seed_campaign(con)
            saved = campaigns.save_expected_placement(con, 1, expected_count=1,
                                                      provenance=dict(REAL))
            pid = saved["placement_id"]
            alloc = campaigns.allocate_campaign(
                [{**placement(pid), "id": pid}], [occ("o-1")],
                {"state": "complete"}, "2026-04-01T00:00:00+00:00")
            fr = campaigns.freeze_evaluation(con, 1, alloc,
                                             {"campaign_revision": 2})
            self.assertEqual(len(fr["frozen_ids"]), 1)
            hist = campaigns.evaluation_history(con, pid)
            self.assertEqual(len(hist), 1)
            self.assertEqual(hist[0]["outcome"], "supported")
        finally:
            con.close()

    def test_manual_assessment_separate(self):
        con = fresh_db()
        try:
            seed_campaign(con)
            saved = campaigns.save_expected_placement(con, 1, expected_count=1,
                                                      provenance=dict(REAL))
            m = campaigns.save_manual_assessment(
                con, saved["placement_id"], "ann", "supported",
                "listened to full episode", ["ev-1"])
            self.assertIn("assessment_id", m)
        finally:
            con.close()


class DeadSortKeyTest(unittest.TestCase):
    def test_dead_placement_sort_key_stays_deleted(self):
        # Coordinator decision: dead helper ordered opposite to
        # allocate_campaign and had zero callers. P13 views define
        # their own ordering when the plan box lands.
        self.assertFalse(hasattr(campaigns, "placement_sort_key"))


if __name__ == "__main__":
    unittest.main()
