"""SCREENED (DET-PLAN item 6): one data definition of "screened" and
"fully checked", used by campaign coverage and by the episode badge data.

- Screened: a current transcript copy; the latest screen attestation on
  that copy succeeded and covers [0, transcript end]; the screening record
  says screened (jev_screened or screened_no_ads), not stopped partway.
- Fully checked: screened, and the transcript reaches the stored duration
  within FULLY_CHECKED_TOLERANCE_S (15 s); the copy has an index version;
  no latest fetch or refine attestation failed. Missing evidence (no
  stored duration) is never fully checked.
"""
from __future__ import annotations

import unittest

from earwitness.app import campaigns, coverage, customer
from tests.test_customer_campaigns import add_placement, fresh_db, seed_campaign
from tests.test_uat_dec_cov import add_index, add_screening, seed_legacy_episode


def attest(con, ep_id, stage, start, end, status="success"):
    tv = con.execute("SELECT transcript_version_id FROM episodes WHERE id=?",
                     (ep_id,)).fetchone()[0]
    coverage.record_attestation(con, ep_id, stage, (0.0, max(end, 1.0)), (start, max(end, start + 1.0)),
                                source_pin=f"source-version:{tv}", producer="test-fixture",
                                actor="pipeline", status=status)
    con.commit()


def screened_episode(con, ep_id, duration_min=60.0, extent=3600.0, index=True,
                     screen_end=None, record=True, **screening):
    """A transcript of `extent` s, its transcribe and screen attestations
    (screen to screen_end, default the transcript end), the screening
    record, and the index version."""
    tv = seed_legacy_episode(con, ep_id, duration_min, extent)
    attest(con, ep_id, "transcribe", 0.0, extent)
    attest(con, ep_id, "screen", 0.0, extent if screen_end is None else screen_end)
    if record:
        add_screening(con, ep_id, extent, **screening)
    if index:
        add_index(con, tv)
    return tv


class EpisodeScreenState(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        self.addCleanup(self.con.close)

    def state(self, ep_id):
        return coverage.episode_screen_state(self.con, [ep_id])[ep_id]

    def test_tolerance_is_fifteen_seconds(self):
        self.assertEqual(coverage.FULLY_CHECKED_TOLERANCE_S, 15.0)

    def test_copy_extent_matches_transcript_extent(self):
        tv = seed_legacy_episode(self.con, "ep-1", 60.0, 3600.0)
        coverage._EXTENT_CACHE.clear()
        self.assertEqual(coverage._copy_extent(self.con, tv),
                         campaigns._transcript_extent(self.con, tv))
        self.assertEqual(coverage._copy_extent(self.con, tv), 3600.0)

    def test_copy_extent_keys_on_payload_not_stored_sha(self):
        tv1 = seed_legacy_episode(self.con, "ep-1", 60.0, 3600.0)
        tv2 = seed_legacy_episode(self.con, "ep-2", 30.0, 1800.0)
        self.con.execute("UPDATE source_versions SET sha256='same'")
        self.con.commit()
        coverage._EXTENT_CACHE.clear()
        self.assertEqual(coverage._copy_extent(self.con, tv1), 3600.0)
        self.assertEqual(coverage._copy_extent(self.con, tv2), 1800.0)

    def test_state_unchanged_when_extent_cache_is_warm(self):
        screened_episode(self.con, "ep-1")
        coverage._EXTENT_CACHE.clear()
        cold = self.state("ep-1")
        warm = self.state("ep-1")
        self.assertEqual(cold, warm)

    def test_no_transcript(self):
        seed_legacy_episode(self.con, "ep-1", 60.0, 3600.0, with_transcript=False)
        st = self.state("ep-1")
        self.assertEqual(st["state"], "no_transcript")
        self.assertFalse(st["screened"])
        self.assertFalse(st["fully_checked"])
        self.assertIsNone(st["transcript_short_s"])

    def test_fully_checked(self):
        screened_episode(self.con, "ep-1")
        st = self.state("ep-1")
        self.assertEqual(st, {"state": "fully_checked", "screened": True, "fully_checked": True,
                              "transcript_short_s": 0.0, "not_fully_checked": []})

    def test_screened_no_ads_counts_as_screened(self):
        screened_episode(self.con, "ep-1", screened=0, no_ads=1)
        self.assertTrue(self.state("ep-1")["fully_checked"])

    def test_transcript_within_fifteen_seconds_is_fully_checked(self):
        screened_episode(self.con, "ep-1", extent=3586.0)
        st = self.state("ep-1")
        self.assertTrue(st["fully_checked"])
        self.assertEqual(st["transcript_short_s"], 14.0)

    def test_transcript_ending_early_is_screened_only(self):
        screened_episode(self.con, "ep-1", extent=3580.0)
        st = self.state("ep-1")
        self.assertEqual(st["state"], "screened")
        self.assertTrue(st["screened"])
        self.assertFalse(st["fully_checked"])
        self.assertEqual(st["transcript_short_s"], 20.0)
        self.assertEqual(st["not_fully_checked"], ["transcript_short"])

    def test_record_without_screen_attestation_is_not_screened(self):
        tv = seed_legacy_episode(self.con, "ep-1", 60.0, 3600.0)
        attest(self.con, "ep-1", "transcribe", 0.0, 3600.0)
        add_screening(self.con, "ep-1", 3600.0)
        add_index(self.con, tv)
        st = self.state("ep-1")
        self.assertEqual(st["state"], "not_screened")
        self.assertFalse(st["screened"])
        self.assertFalse(st["fully_checked"])
        self.assertIn("not_screened", st["not_fully_checked"])

    def test_screen_attestation_without_record_is_not_screened(self):
        screened_episode(self.con, "ep-1", record=False)
        self.assertEqual(self.state("ep-1")["state"], "not_screened")

    def test_record_not_screened_or_stopped_is_not_screened(self):
        screened_episode(self.con, "ep-1", screened=0)
        screened_episode(self.con, "ep-2", stopped=1)
        self.assertEqual(self.state("ep-1")["state"], "not_screened")
        self.assertEqual(self.state("ep-2")["state"], "not_screened")

    def test_screen_short_of_transcript_end_is_not_screened(self):
        screened_episode(self.con, "ep-1", screen_end=3500.0)
        self.assertEqual(self.state("ep-1")["state"], "not_screened")

    def test_transcript_past_the_rounded_duration_needs_proof_to_the_duration(self):
        """A load attests at most the requested (stored) duration: a
        transcript ending 0.4 s past it is screened with proof to the
        duration, never with proof short of it."""
        screened_episode(self.con, "ep-1", extent=3600.4, screen_end=3600.0)
        screened_episode(self.con, "ep-2", extent=3600.4, screen_end=3599.6)
        self.assertEqual(self.state("ep-1")["state"], "fully_checked")
        self.assertEqual(self.state("ep-2")["state"], "not_screened")

    def test_transcript_far_past_the_duration_needs_proof_to_its_end(self):
        """Only the rounding half-step past the stored duration is forgiven:
        a transcript running far past it needs proof to its own end."""
        screened_episode(self.con, "ep-1", duration_min=30.0, extent=3600.0, screen_end=1800.0)
        screened_episode(self.con, "ep-2", duration_min=30.0, extent=3600.0)
        self.assertEqual(self.state("ep-1")["state"], "not_screened")
        st = self.state("ep-2")
        self.assertEqual(st["state"], "fully_checked")
        self.assertEqual(st["transcript_short_s"], 0.0)

    def test_no_transcript_with_failed_fetch_lists_both_reasons(self):
        seed_legacy_episode(self.con, "ep-1", 60.0, 3600.0, with_transcript=False)
        self.record_failed_fetch("ep-1")
        self.assertEqual(self.state("ep-1")["not_fully_checked"],
                         ["no_transcript", "fetch_failed"])

    def record_failed_fetch(self, ep_id):
        coverage.record_attestation(self.con, ep_id, "fetch", (0.0, 3600.0), (0.0, 3600.0),
                                    source_pin="source-version:none", producer="test-fixture",
                                    actor="pipeline", status="failed")
        self.con.commit()

    def test_latest_screen_failure_is_not_screened(self):
        screened_episode(self.con, "ep-1")
        attest(self.con, "ep-1", "screen", 0.0, 3600.0, status="failed")
        self.assertEqual(self.state("ep-1")["state"], "not_screened")

    def test_unindexed_copy_is_not_fully_checked(self):
        screened_episode(self.con, "ep-1", index=False)
        st = self.state("ep-1")
        self.assertEqual(st["state"], "screened")
        self.assertEqual(st["not_fully_checked"], ["not_indexed"])

    def test_failed_fetch_or_refine_is_not_fully_checked(self):
        screened_episode(self.con, "ep-1")
        attest(self.con, "ep-1", "fetch", 0.0, 3600.0, status="failed")
        screened_episode(self.con, "ep-2")
        attest(self.con, "ep-2", "refine", 0.0, 3600.0, status="failed")
        self.assertEqual(self.state("ep-1")["not_fully_checked"], ["fetch_failed"])
        self.assertEqual(self.state("ep-2")["not_fully_checked"], ["refine_failed"])
        self.assertFalse(self.state("ep-1")["fully_checked"])

    def test_unknown_duration_is_never_fully_checked(self):
        screened_episode(self.con, "ep-1", duration_min=None)
        st = self.state("ep-1")
        self.assertEqual(st["state"], "screened")
        self.assertIsNone(st["transcript_short_s"])
        self.assertEqual(st["not_fully_checked"], ["no_duration"])

    def test_publication_and_index_attestations_are_not_required(self):
        """Today's rule needs observation_publication and index
        attestations that no load writes; the copy's index version is the
        index fact."""
        screened_episode(self.con, "ep-1")
        n = self.con.execute("SELECT count(*) FROM coverage_attestations WHERE stage IN"
                             " ('observation_publication','index')").fetchone()[0]
        self.assertEqual(n, 0)
        self.assertTrue(self.state("ep-1")["fully_checked"])


class CampaignCoverageUsesTheDefinition(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        self.addCleanup(self.con.close)
        self.cid = seed_campaign(self.con)

    def coverage_of(self, ep_id):
        pid = add_placement(self.con, self.cid, "p-" + ep_id, episode_id=ep_id)
        return campaigns.build_customer_draft(self.con, self.cid)["coverage_by_placement"][pid]

    def test_fully_checked_episode_is_complete(self):
        screened_episode(self.con, "ep-1", extent=3590.0)
        cov = self.coverage_of("ep-1")
        self.assertEqual(cov["state"], "complete")
        self.assertEqual(cov["episode_checks"]["ep-1"],
                         coverage.episode_screen_state(self.con, ["ep-1"])["ep-1"])

    def test_screened_short_episode_is_incomplete_with_its_gap(self):
        screened_episode(self.con, "ep-1", extent=3580.0)
        cov = self.coverage_of("ep-1")
        self.assertEqual(cov["state"], "incomplete")
        self.assertEqual(cov["missing"], ["ep-1"])
        check = cov["episode_checks"]["ep-1"]
        self.assertEqual((check["state"], check["transcript_short_s"]), ("screened", 20.0))

    def test_not_screened_episode_is_incomplete(self):
        screened_episode(self.con, "ep-1", record=False)
        cov = self.coverage_of("ep-1")
        self.assertEqual(cov["state"], "incomplete")
        self.assertEqual(cov["episode_checks"]["ep-1"]["state"], "not_screened")

    def test_transcript_far_past_the_duration_is_incomplete_without_proof_to_its_end(self):
        screened_episode(self.con, "ep-1", duration_min=30.0, extent=3600.0, screen_end=1800.0)
        self.assertEqual(self.coverage_of("ep-1")["state"], "incomplete")

    def test_no_transcript_with_failed_fetch_is_incomplete(self):
        seed_legacy_episode(self.con, "ep-1", 60.0, 3600.0, with_transcript=False)
        coverage.record_attestation(self.con, "ep-1", "fetch", (0.0, 3600.0), (0.0, 3600.0),
                                    source_pin="source-version:none", producer="test-fixture",
                                    actor="pipeline", status="failed")
        self.con.commit()
        cov = self.coverage_of("ep-1")
        self.assertEqual(cov["state"], "incomplete")
        self.assertEqual(cov["missing"], ["ep-1"])
        self.assertIn("ep-1: latest fetch attestation failed", cov["exclusions"])
        self.assertEqual(cov["episode_checks"]["ep-1"]["not_fully_checked"],
                         ["no_transcript", "fetch_failed"])

    def test_campaign_and_episode_check_agree_at_the_tolerance_edge(self):
        """8.3 min * 60 = 498.00000000000006 s: a transcript ending at 483.0 s
        is exactly 15 s short; the campaign uses the definition's comparison."""
        screened_episode(self.con, "ep-1", duration_min=8.3, extent=483.0)
        cov = self.coverage_of("ep-1")
        check = cov["episode_checks"]["ep-1"]
        self.assertTrue(check["fully_checked"])
        self.assertEqual(cov["state"], "complete")

    def test_every_stage_attested_without_screening_record_is_not_complete(self):
        tv = seed_legacy_episode(self.con, "ep-1", 60.0, 3600.0)
        for stage in ("transcribe", "screen", "observation_publication", "index"):
            attest(self.con, "ep-1", stage, 0.0, 3600.0)
        add_index(self.con, tv)
        cov = self.coverage_of("ep-1")
        self.assertNotEqual(cov["state"], "complete")

    def test_whole_episode_scope_captured_longer_than_transcript_is_incomplete(self):
        # Captured end_s=3600 from a 60 min duration; a later import lowers
        # the duration to 30 min, so the 1800 s copy is fully checked against
        # the new duration but covers half the captured scope.
        screened_episode(self.con, "ep-1", duration_min=60.0, extent=1800.0)
        pid = add_placement(self.con, self.cid, "p-ep-1", episode_id="ep-1")
        self.con.execute("UPDATE episodes SET duration_min=30.0 WHERE id='ep-1'")
        self.con.commit()
        self.assertEqual(coverage.episode_screen_state(self.con, ["ep-1"])["ep-1"]["state"],
                         "fully_checked")
        cov = campaigns.build_customer_draft(self.con, self.cid)["coverage_by_placement"][pid]
        self.assertEqual(cov["state"], "incomplete")
        self.assertEqual(cov["missing"], ["ep-1"])
        self.assertTrue(any("transcript covers 1800.0s of requested 3600.0s" in x
                            for x in cov["exclusions"]), cov["exclusions"])

    def test_whole_episode_scope_with_transcript_reaching_captured_end_is_complete(self):
        screened_episode(self.con, "ep-1", duration_min=60.0, extent=3600.0)
        cov = self.coverage_of("ep-1")
        self.assertEqual(cov["state"], "complete")


class PlacementCoverageMemoisesScreening(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        self.addCleanup(self.con.close)
        self.cid = seed_campaign(self.con)

    def test_one_screening_lookup_per_draft_and_same_coverage(self):
        from unittest import mock
        from earwitness.app import ad_reads
        screened_episode(self.con, "ep-1", extent=3590.0)
        screened_episode(self.con, "ep-2", duration_min=30.0, extent=1000.0)
        p1 = add_placement(self.con, self.cid, "p-1", episode_id="ep-1")
        p2 = add_placement(self.con, self.cid, "p-2", episode_id="ep-1")
        p3 = add_placement(self.con, self.cid, "p-3", episode_id="ep-2")
        with mock.patch.object(ad_reads, "screening_display_many",
                               wraps=ad_reads.screening_display_many) as spy:
            draft = campaigns.build_customer_draft(self.con, self.cid)
        calls = [tuple(c.args[1]) for c in spy.call_args_list]
        self.assertEqual(sorted(calls), [("ep-1",), ("ep-2",)], calls)
        self.assertEqual(set(draft["coverage_by_placement"]), {p1, p2, p3})
        self.assertEqual(draft["coverage_by_placement"][p1]["state"], "complete")

    def test_jev_run_parsed_once_per_draft_for_rowless_episodes(self):
        """Episodes without an episode_screening row fall back to their
        jev-run entry; the run is parsed once per draft, not once per
        placement, and coverage equals the uncached result."""
        from unittest import mock
        from earwitness.app import coverage_import
        import hashlib
        import json
        from earwitness.pipeline import jev_screen
        pids = []
        for i in range(3):
            ep = f"ep-{i}"
            screened_episode(self.con, ep)
            self.con.execute("DELETE FROM episode_screening WHERE episode_id=?", (ep,))
            pids.append(add_placement(self.con, self.cid, f"p-{i}", episode_id=ep))
        # A row-less episode is screened by its run entry only when the entry
        # names the pinned model (coverage_import.run_entry_screening).
        row = self.con.execute("SELECT id, payload FROM source_versions"
                               " WHERE source_key='jev-run' AND is_active=1").fetchone()
        run = json.loads(row["payload"])
        for entry in run["episodes"]:
            entry["model"] = jev_screen.DEFAULT_MODEL
        text = json.dumps(run)
        self.con.execute("UPDATE source_versions SET payload=?, sha256=? WHERE id=?",
                         (text, hashlib.sha256(text.encode()).hexdigest(), row["id"]))
        self.con.commit()
        with mock.patch.object(coverage_import, "_jev_run",
                               wraps=coverage_import._jev_run) as spy:
            draft = campaigns.build_customer_draft(self.con, self.cid)
        self.assertEqual(spy.call_count, 1)
        placements = {p["id"]: p for p in campaigns.list_expected(self.con, self.cid)}
        for i, pid in enumerate(pids):
            cov = draft["coverage_by_placement"][pid]
            self.assertEqual(cov["state"], "complete")
            self.assertEqual(cov["episode_checks"][f"ep-{i}"]["state"], "fully_checked")
            plain = campaigns._placement_coverage(self.con, placements[pid], None, None, None)
            self.assertEqual(cov["state"], plain["state"])
            self.assertEqual(cov["episode_checks"], plain["episode_checks"])

    def test_memoised_coverage_equals_uncached_coverage(self):
        screened_episode(self.con, "ep-1", extent=3590.0)
        screened_episode(self.con, "ep-2", record=False)
        pids = [add_placement(self.con, self.cid, "p-1", episode_id="ep-1"),
                add_placement(self.con, self.cid, "p-2", episode_id="ep-2")]
        placements = {p["id"]: p for p in campaigns.list_expected(self.con, self.cid)}
        self.assertEqual(set(placements), set(pids))
        for pid in pids:
            placement = placements[pid]
            cache: dict = {}
            first = campaigns._placement_coverage(self.con, placement, None, None, cache)
            second = campaigns._placement_coverage(self.con, placement, None, None, cache)
            plain = campaigns._placement_coverage(self.con, placement, None, None, None)
            self.assertEqual(first, plain)
            self.assertEqual(second, plain)


class BadgeDataUsesTheDefinition(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        self.addCleanup(self.con.close)

    def test_episode_page_carries_the_check(self):
        screened_episode(self.con, "ep-1", extent=3580.0)
        page = customer.episode_reads(self.con, "ep-1")
        self.assertEqual(page["screen_check"],
                         coverage.episode_screen_state(self.con, ["ep-1"])["ep-1"])
        self.assertEqual(page["screen_check"]["state"], "screened")

    def test_episode_page_without_record_is_not_screened(self):
        tv = seed_legacy_episode(self.con, "ep-1", 60.0, 3600.0)
        attest(self.con, "ep-1", "transcribe", 0.0, 3600.0)
        add_index(self.con, tv)
        page = customer.episode_reads(self.con, "ep-1")
        self.assertEqual(page["screen_check"]["state"], "not_screened")


if __name__ == "__main__":
    unittest.main()
