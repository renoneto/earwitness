"""Post-decisions item 3: an episode with no episode_screening row (a
benchmark or upload episode) shows the screening status its active jev-run
entry proves, by the same rule the coverage import uses (_screen_evidence
and run_entry_current), with the same wording as every other episode.
Display only: nothing is written. Offline."""
from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_uat_dec_rescreen as R  # noqa: E402

from earwitness.app import coverage_import, customer, db  # noqa: E402


class RunEntryScreeningStatus(R._ImportBase):
    def setUp(self):
        super().setUp()
        self.run_import()
        # s1 (one flagged window) and s2 (none) as bench/upload-like episodes.
        self.con.execute("DELETE FROM episode_screening WHERE episode_id IN ('s1','s2')")
        self.con.commit()

    def page(self, eid):
        with db.transaction(self.con, write=False):
            return customer.episode_reads(self.con, eid)

    def show_entry(self, eid):
        show = customer.customer_show_for_episode(self.con, eid)["show_identity_id"]
        with db.transaction(self.con, write=False):
            return next(e for e in customer.show_detail(self.con, show)["episodes"]
                        if e["id"] == eid)

    def edit_entry(self, eid, edit):
        vid, run = self.active_run()
        for e in run["episodes"]:
            if e["episode_id"] == eid:
                edit(e)
        # A changed run gets its own sha256, as a load writes it:
        # jev_rescreen caches run entries by (version id, sha256).
        text = json.dumps(run)
        self.con.execute("UPDATE source_versions SET payload=?, sha256=? WHERE id=?",
                         (text, hashlib.sha256(text.encode()).hexdigest(), vid))
        self.con.commit()

    def coverage_accepts(self, eid):
        tv = self.con.execute("SELECT transcript_version_id FROM episodes WHERE id=?",
                              (eid,)).fetchone()[0]
        segs = (coverage_import._payload(self.con, tv) or {}).get("segments") or []
        return coverage_import._screen_evidence(
            self.con, eid, tv, segs, coverage_import._jev_run(self.con))[1] is None

    def test_flagged_entry_says_screened_like_the_other_episodes(self):
        sc = self.page("s1")["screening"]
        self.assertEqual(sc["label"], "Screened")
        self.assertEqual((sc["jev_screened"], sc["screened_no_ads"], sc["stopped_partway"],
                          sc["coverage_tier"], sc["transcript_only"], sc["stage"]),
                         (True, False, False, "full", False, None))
        self.assertFalse(self.page("s1")["screened_no_ads"])

    def test_entry_without_flagged_windows_says_screened_no_ads_found(self):
        page = self.page("s2")
        self.assertEqual(page["screening"]["label"], "Screened, no ads found")
        self.assertTrue(page["screening"]["screened_no_ads"])
        self.assertTrue(page["screened_no_ads"])

    def test_entry_without_flagged_windows_but_a_listed_read_says_screened(self):
        # bench_3: the new entry flags nothing, but an earlier run's row
        # stays active on the current copy and the page lists it: never
        # "no ads found" next to a listed read.
        self.con.execute(
            "INSERT INTO evidence (id, source, source_version_id, episode_id, orig_start_s,"
            " orig_end_s, excerpt, attrs, revision, created_at, transcript_version_id,"
            " source_record_key) VALUES ('jev:old-s2', 'jev', ?, 's2', 60.0, 120.0,"
            " 'old window', '{}', 1, ?, ?, 'jev:s2:window_001')",
            (self.ids["old_run"], db.utcnow(), self.ids["tv2"]))
        vid, _run = self.active_run()
        self.con.execute("INSERT OR REPLACE INTO evidence_source_membership VALUES"
                         " ('jev:old-s2', ?, 1)", (vid,))
        from earwitness.app import ad_reads
        with db.transaction(self.con, write=True):
            ad_reads.rebuild_from_workspace(self.con)
        self.con.execute("DELETE FROM episode_screening WHERE episode_id='s2'")
        self.con.commit()
        page = self.page("s2")
        self.assertTrue(page["ads"], "the carried row is listed")
        for sc in (page["screening"], self.show_entry("s2")["screening"]):
            self.assertEqual(sc["label"], "Screened")
            self.assertEqual((sc["jev_screened"], sc["screened_no_ads"]), (True, False))
        self.assertFalse(page["screened_no_ads"])

    def test_a_transcript_short_of_the_stored_duration_blocks_the_claim(self):
        # R1-07: the import's own gate (transcript reaches the stored
        # duration; no failed latest attestation) applies to the page too.
        self.assertEqual(self.page("s1")["screening"]["label"], "Screened")
        self.con.execute("UPDATE episodes SET duration_min=60.0 WHERE id='s1'")
        self.con.commit()
        self.assertIsNone(self.page("s1")["screening"], "transcript stops short")
        self.assertIsNone(self.show_entry("s1")["screening"])
        self.con.execute("UPDATE episodes SET duration_min=4.0 WHERE id='s1'")
        self.con.commit()
        self.assertEqual(self.page("s1")["screening"]["label"], "Screened")

    def test_a_failed_latest_screen_attestation_blocks_the_claim(self):
        from earwitness.app import coverage
        self.assertEqual(self.page("s1")["screening"]["label"], "Screened")
        tv = self.ids["tv1"]
        coverage.record_attestation(self.con, "s1", "screen", (0.0, 240.0), (0.0, 240.0),
                                    source_pin=f"source-version:{tv}", producer="test",
                                    actor="owner", status="failed")
        self.con.commit()
        self.assertIsNone(self.page("s1")["screening"], "latest screen attestation failed")
        self.assertIsNone(self.show_entry("s1")["screening"])
        self.assertEqual(self.page("s2")["screening"]["label"], "Screened, no ads found")

    def test_a_failed_latest_non_screen_attestation_keeps_the_claim(self):
        # R3-01: per stage, as the coverage proof: a failed latest
        # attestation of another stage does not hide the screening claim.
        from earwitness.app import coverage
        tv = self.ids["tv1"]
        for stage in ("observation_publication", "transcribe", "index"):
            with self.subTest(stage):
                coverage.record_attestation(self.con, "s1", stage, (0.0, 240.0), (0.0, 240.0),
                                            source_pin=f"source-version:{tv}", producer="test",
                                            actor="owner", status="failed")
                self.con.commit()
                self.assertEqual(self.page("s1")["screening"]["label"], "Screened")
                self.assertEqual(self.show_entry("s1")["screening"]["label"], "Screened")
                self.assertEqual(coverage.attestation_union(
                    self.con, "s1", coverage.STAGE_GROUPS["screening"], source_identity=tv)[1], None)

    def test_show_page_lists_the_same_state(self):
        self.assertEqual(self.show_entry("s1")["screening"], self.page("s1")["screening"])
        self.assertEqual(self.show_entry("s2")["screening"], self.page("s2")["screening"])

    def test_display_only_nothing_is_written(self):
        tables = ("episode_screening", "coverage_attestations", "evidence", "ad_reads", "audit")
        before = {t: self.con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}
        rev = self.con.execute("PRAGMA data_version").fetchone()[0]
        self.page("s1"), self.page("s2"), self.show_entry("s1")
        after = {t: self.con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}
        self.assertEqual(before, after)
        self.assertEqual(self.con.execute("SELECT count(*) FROM episode_screening WHERE"
                                          " episode_id IN ('s1','s2')").fetchone()[0], 0)

    def test_entry_that_does_not_match_the_copy_claims_nothing(self):
        cases = {
            "other digest": lambda e: e.update(segments_sha256="0" * 64),
            "other segment count": lambda e: e.update(segment_count=e["segment_count"] + 1),
            "other copy": lambda e: e.update(transcript_version_id=self.ids["old_s1"]),
            "no recorded copy": lambda e: e.pop("transcript_version_id"),
            "gap in windows": lambda e: e["answers"].pop(sorted(e["answers"])[1]),
            "other model": lambda e: e.update(model="jev-1.12.0"),
        }
        for name, edit in cases.items():
            with self.subTest(name):
                vid, run = self.active_run()
                saved = self.con.execute("SELECT payload, sha256 FROM source_versions"
                                         " WHERE id=?", (vid,)).fetchone()
                self.edit_entry("s1", edit)
                self.assertFalse(self.coverage_accepts("s1"))
                self.assertIsNone(self.page("s1")["screening"])
                self.assertIsNone(self.show_entry("s1")["screening"])
                self.con.execute("UPDATE source_versions SET payload=?, sha256=? WHERE id=?",
                                 (saved["payload"], saved["sha256"], vid))
                self.con.commit()
                self.assertTrue(self.coverage_accepts("s1"))
                self.assertEqual(self.page("s1")["screening"]["label"], "Screened")

    def test_flagged_row_hidden_from_the_active_run_claims_nothing(self):
        vid, _run = self.active_run()
        row = self.con.execute("SELECT id FROM evidence WHERE episode_id='s1' AND"
                               " source_version_id=?", (vid,)).fetchone()
        self.con.execute("INSERT OR REPLACE INTO evidence_source_membership VALUES (?, ?, 0)",
                         (row[0], vid))
        self.con.commit()
        self.assertIsNone(self.page("s1")["screening"])

    def test_episode_without_a_matching_entry_is_unchanged(self):
        # s3 without a row: its entry never recorded its copy, so no claim.
        self.con.execute("DELETE FROM episode_screening WHERE episode_id='s3'")
        self.con.commit()
        self.assertIsNone(self.page("s3")["screening"])
        self.assertEqual(self.page("s3")["screened_no_ads"], False)

    def test_a_screening_row_keeps_its_label_whatever_the_entry_says(self):
        for row, label in (((1, "full", "stopped:name"), "Stopped partway (name)"),
                           ((0, "transcript", "ok"), "Not screened for ads (transcript only)"),
                           ((0, "full", "ok"), "Not screened")):
            with self.subTest(label):
                self.con.execute("INSERT OR REPLACE INTO episode_screening (episode_id,"
                                 " jev_screened, stopped_partway, coverage_tier, pipeline_status)"
                                 " VALUES ('s1', 0, ?, ?, ?)", row)
                self.con.commit()
                self.assertEqual(self.page("s1")["screening"]["label"], label)
                self.assertEqual(self.show_entry("s1")["screening"]["label"], label)


if __name__ == "__main__":
    unittest.main()
