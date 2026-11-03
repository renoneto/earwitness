"""The export contract fixture (docs/export-contract.md) is self-consistent.

The app importer and the corpus export both work against this fixture, so
its named reads must carry the contract fields with values that match the
rest of the export: clip files and offsets, supporting keys that name real
records, and display spellings with their basis.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.backend_fixtures import TRANSCRIPT_TIER_EP, add_transcript_tier_episode

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "export-contract"
NAMED = "experiments/named-reads/output/candidates_normalized.json"
DET = "experiments/next-validation-detector/output/candidates_normalized.json"
JEV = "experiments/jev-ad-triage-20260921/results.json"


def load(rel):
    return json.loads((FIXTURE / rel).read_text())


class ExportContractFixture(unittest.TestCase):
    def test_named_reads_carry_contract_fields(self):
        for read in load(NAMED):
            self.assertIn("clip", read)
            self.assertIn("supporting_keys", read)
            self.assertIn("brand_display", read)
            self.assertIn(read["brand_display_basis"], ("brand_list", "transcript_cased", "none"))
            if read["brand_display_basis"] == "none":
                self.assertIsNone(read["brand_display"])
            else:
                self.assertIsInstance(read["brand_display"], str)

    def test_supporting_keys_name_overlapping_records(self):
        det = {f"det:{c['candidate_id']}": c for c in load(DET)}
        jev = {}
        for ep in load(JEV)["episodes"]:
            for wid, w in ep["answers"].items():
                if w["noul"] >= 0.8:
                    jev[f"jev:{ep['episode_id']}:{wid}"] = dict(w, ep_id=ep["episode_id"])
        for read in load(NAMED):
            for key in read["supporting_keys"]:
                record = det.get(key) or jev.get(key)
                self.assertIsNotNone(record, key)
                start = record.get("start_s")
                end = record.get("end_s")
                self.assertEqual(record["ep_id"], read["ep_id"])
                self.assertTrue(start < read["end_s"] and end > read["start_s"], key)

    def test_clip_links_point_at_listed_checked_files(self):
        episodes = {e["ep_id"]: e for e in load("data/episodes.json")}
        for read in load(NAMED):
            clip = read["clip"]
            if clip is None:
                continue
            self.assertIn(clip["file"], episodes[read["ep_id"]]["clips"])
            index = load(f"clips/{read['ep_id']}/index.json")
            entry = next(c for c in index["clips"] if f"clips/{read['ep_id']}/{c['file']}" == clip["file"])
            self.assertEqual(entry["start_s"], clip["clip_start_s"])
            self.assertAlmostEqual(read["start_s"] - clip["clip_start_s"], clip["offset_s"])
            data = (FIXTURE / clip["file"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"])
            self.assertTrue(entry["start_s"] <= read["start_s"] and read["end_s"] <= entry["end_s"])

    def test_episode_rows_carry_screening_fields(self):
        for ep in load("data/episodes.json"):
            self.assertIn("name_status", ep)
            self.assertIn("jev_status", ep)
            self.assertTrue(ep["pipeline_status"] == "done"
                            or ep["pipeline_status"].startswith("dead:"))


class TranscriptTierRows(unittest.TestCase):
    """A transcript-tier episode as the corpus export writes it (SETUP.md,
    coverage tiers): it has transcripts and the `coverage_tier` key, and no
    detector, Jev, named, screen or clip rows. Full-tier rows have no key."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.export = self.tmp / "export"
        shutil.copytree(FIXTURE, self.export)
        add_transcript_tier_episode(self.export)

    def rows(self, rel):
        return json.loads((self.export / rel).read_text())

    def test_only_the_transcript_row_carries_the_tier_key(self):
        eps = {e["ep_id"]: e for e in self.rows("data/episodes.json")}
        self.assertEqual(eps[TRANSCRIPT_TIER_EP]["coverage_tier"], "transcript")
        self.assertEqual([k for k, e in eps.items() if "coverage_tier" in e], [TRANSCRIPT_TIER_EP])

    def test_the_transcript_row_has_a_transcript_and_no_ad_rows(self):
        ep = next(e for e in self.rows("data/episodes.json") if e["ep_id"] == TRANSCRIPT_TIER_EP)
        self.assertTrue((self.export / "transcripts" / f"{TRANSCRIPT_TIER_EP}.json").is_file())
        self.assertEqual((ep["jev_status"], ep["name_status"], ep["screen_status"], ep["clips"]),
                         (None, None, None, []))
        for rel in (NAMED, DET):
            self.assertNotIn(TRANSCRIPT_TIER_EP, {r["ep_id"] for r in self.rows(rel)})
        self.assertNotIn(TRANSCRIPT_TIER_EP, {e["episode_id"] for e in self.rows(JEV)["episodes"]})
        self.assertFalse((self.export / "clips" / TRANSCRIPT_TIER_EP).exists())


if __name__ == "__main__":
    unittest.main()
