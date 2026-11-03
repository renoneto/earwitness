"""P08 coverage/intelligence regressions (stdlib unittest).

Worked-example exact numbers (snapshot B latest-failed), totals reconcile
to units, missing-language/failed-source visibility. No economics invented.
"""
from __future__ import annotations

import unittest

from earwitness.app import coverage


def worked_example():
    snaps = [
        {"episode_id": "ep-1", "fetch_id": "A", "fetch_start": "2026-01-01T00:00:00+00:00",
         "req_start": 0.0, "req_end": 3600.0, "duration_s": 3600.0,
         "has_transcript": True, "language": "en",
         "stages": {"transcription": True, "screening": True,
                    "observation_publication": True, "index": True},
         "successful_intervals": [(0.0, 3600.0)]},
        {"episode_id": "ep-1", "fetch_id": "B", "fetch_start": "2026-01-02T00:00:00+00:00",
         "req_start": 0.0, "req_end": 3600.0, "duration_s": 3600.0,
         "has_transcript": True, "language": "en", "fetch_failed": False,
         "stages": {"transcription": True, "screening": False,
                    "observation_publication": False, "index": True},
         "successful_intervals": []},
    ]
    return snaps


class CoverageTest(unittest.TestCase):
    def test_worked_example_latest_failed(self):
        out = coverage.summarize_scope(worked_example(), scope={"episode": "ep-1"})
        self.assertEqual(out["definition_revision"], 1)
        # Default selection is B: searchable = 60 minutes.
        self.assertAlmostEqual(out["searchable_transcript_hours"], 1.0)
        # Automated observation coverage is zero; complete episodes zero.
        self.assertAlmostEqual(out["automated_observation_hours"], 0.0)
        self.assertEqual(out["distinct_covered_episodes"], [])
        self.assertTrue(out["incomplete"])
        self.assertEqual(
            [s["fetch_id"] for s in out["selected_snapshots"]], ["B"])

    def test_multi_snapshot_labels_sum(self):
        out = coverage.summarize_scope(worked_example(), multi_snapshot=True)
        self.assertIn("snapshot_audio_hours", out)
        self.assertAlmostEqual(out["snapshot_audio_hours"], 1.0)
        self.assertEqual(len(out["selected_snapshots"]), 2)

    def test_totals_reconcile(self):
        obs = [
            {"id": "o1", "brand": "acme", "category": "audio", "show": "S",
             "judgment": "reviewed_ad", "superseded": False,
             "observed_at": "2026-01-02", "status": "verified"},
            {"id": "o2", "brand": "acme", "category": "audio", "show": "S",
             "judgment": "reviewed_ad", "superseded": False,
             "observed_at": "2026-01-03", "status": "verified"},
            {"id": "p1", "brand": "acme", "category": "audio", "show": "S",
             "judgment": "proposed", "status": "proposed",
             "observed_at": "2026-01-04"},
        ]
        comp = coverage.compare_groups(obs, "brand", unit="reviewed_occurrences",
                                       include_proposals=True)
        self.assertEqual(comp["unit"], "reviewed_occurrences")
        self.assertEqual(comp["totals"]["reviewed_occurrences"], 2)
        self.assertEqual(comp["totals"]["proposals_separate"], 1)
        # Unknown bucket separation.
        obs2 = [{"id": "u1", "judgment": "reviewed_ad", "observed_at": "2026-01-02"}]
        comp2 = coverage.compare_groups(obs2, "brand")
        self.assertEqual(comp2["groups"][0]["group"], "unknown_brand")

    def test_missing_language_failed_visible(self):
        snaps = [{"episode_id": "ep-9", "fetch_id": "F1",
                  "fetch_start": "2026-01-05T00:00:00+00:00",
                  "req_start": 0.0, "req_end": 600.0, "duration_s": 600.0,
                  "has_transcript": True, "language": "unknown",
                  "stages": {"transcription": True, "screening": True,
                             "observation_publication": True, "index": True},
                  "successful_intervals": [(0.0, 600.0)]}]
        out = coverage.summarize_scope(snaps)
        self.assertTrue(any("language" in e for e in out["exclusions"]))

    def test_no_economics(self):
        comp = coverage.compare_groups([], "brand")
        blob = str(comp)
        for banned in ("spend", "reach", "market_share", "market-share"):
            self.assertNotIn(banned, blob)


if __name__ == "__main__":
    unittest.main()
