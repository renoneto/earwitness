"""D-2: one-off, operator-run legacy coverage attestation import.

Legacy workspaces carry no coverage attestations, so every placement stays
"Coverage incomplete" and Freeze stays disabled. The import looks only at
episodes whose stored transcript ends within TOLERANCE_S (half the 0.1-min
duration rounding step) of the stored duration, and attests each stage
only from its own stored evidence on the current transcript copy, as its
own auditable producer. Dry-run writes nothing; a second run records
nothing; short, duration-less and transcript-less episodes are skipped with
their reason.

Also here: a placement with complete coverage and reads still waiting for
review in scope is never "no match" (hard rule 1).
"""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from earwitness.app import (ad_reads, campaigns, citations, coverage, coverage_import,
                            customer, db, transcript_index)
from earwitness.pipeline import jev_screen
from tests.backend_fixtures import record_screening
from tests.test_customer_campaigns import (add_candidate, add_placement, fresh_db,
                                           seed_campaign)

REPO = Path(__file__).resolve().parent.parent
ALL_STAGES = ("transcribe", "screen", "observation_publication", "index")


def seed_legacy_episode(con, ep_id, duration_min, extent, with_transcript=True):
    """Legacy-shaped episode: transcript and duration, no attestation."""
    tv = None
    if with_transcript:
        segments = [{"start": 0.0, "end": extent / 2.0, "text": "acme bright"},
                    {"start": extent / 2.0, "end": extent, "text": "more talk"}]
        text = json.dumps({"segments": segments})
        # sha256 = the payload digest, as a real load writes it (jev_rescreen
        # caches copy digests by (version id, sha256)).
        tv = con.execute(
            "INSERT INTO source_versions (source_key, version_no, sha256, payload,"
            " is_active, created_at) VALUES (?, 1, ?, ?, 1, ?)",
            (f"transcript:{ep_id}", hashlib.sha256(text.encode()).hexdigest(), text,
             db.utcnow())).lastrowid
    con.execute(
        "INSERT INTO episodes (id, kind, show_title, ep_title, category, pub_date,"
        " duration_min, fetch_date, transcript_version_id, revision, updated_at)"
        " VALUES (?, 'supplied', 'Show', 'Ep', 'business', '2026-03-10', ?,"
        " '2026-09-21', ?, 1, ?)",
        (ep_id, duration_min, tv, db.utcnow()))
    citations.publish_source_state(con, kind="episode", record_id=ep_id, actor="fixture")
    con.commit()
    return tv


def add_index(con, tv):
    transcript_index.index_versions(con, [tv])
    con.commit()


def add_screening(con, ep_id, extent, segment_count=2, tier="full", screened=1,
                  stopped=0, no_ads=0, jev_tv="current"):
    """Screening record + the screening run entry that read a copy with
    segment_count segments, in 60 s windows to the transcript end, naming
    the copy it read as a Jev re-screen records it (transcript_version_id +
    segments digest of jev_tv: "current" = the episode's current copy;
    None = an older run that never recorded its copy), + the run's window
    record (jev:<episode>:window_000) on that copy."""
    con.execute("INSERT OR REPLACE INTO episode_screening (episode_id, jev_screened,"
                " stopped_partway, coverage_tier, screened_no_ads) VALUES (?, ?, ?, ?, ?)",
                (ep_id, screened, stopped, tier, no_ads))
    if jev_tv is not None:
        if jev_tv == "current":
            jev_tv = con.execute("SELECT transcript_version_id FROM episodes WHERE id=?",
                                 (ep_id,)).fetchone()[0]
        con.execute(
            "INSERT INTO evidence (id, source, source_version_id, episode_id, orig_start_s,"
            " orig_end_s, excerpt, attrs, revision, created_at, transcript_version_id,"
            " source_record_key, assoc_revision, record_kind)"
            " VALUES (?, 'jev', ?, ?, 0.0, 60.0, 'window', '{}', 1, ?, ?, ?, 0, 'candidate')",
            (f"jev:{ep_id}:window_000", jev_tv, ep_id, db.utcnow(), jev_tv,
             f"jev:{ep_id}:window_000"))
    ad_reads._set_meta(con, ad_reads.BUILT, "1")
    row = con.execute("SELECT id, payload FROM source_versions WHERE source_key='jev-run'"
                      " AND is_active=1").fetchone()
    payload = json.loads(row["payload"]) if row else {"run_id": "fixture", "episodes": []}
    answers, start, i = {}, 0.0, 0
    while start < extent:
        answers[f"window_{i:03d}"] = {"noul": 0.1, "start_s": start, "end_s": min(start + 60.0, extent)}
        start += 60.0
        i += 1
    entry = {"episode_id": ep_id, "segment_count": segment_count, "answers": answers}
    if jev_tv is not None:
        copy = json.loads(con.execute("SELECT payload FROM source_versions WHERE id=?",
                                      (jev_tv,)).fetchone()[0])
        entry["transcript_version_id"] = jev_tv
        entry["segments_sha256"] = jev_screen.segments_digest(copy["segments"])
    payload["episodes"].append(entry)
    # The run version's sha256 is its payload's digest, as a real load
    # writes it: jev_rescreen caches run entries by (version id, sha256).
    text = json.dumps(payload)
    digest = hashlib.sha256(text.encode()).hexdigest()
    if row:
        con.execute("UPDATE source_versions SET payload=?, sha256=? WHERE id=?",
                    (text, digest, row["id"]))
    else:
        con.execute("INSERT INTO source_versions (source_key, version_no, sha256, payload,"
                    " is_active, created_at) VALUES ('jev-run', 1, ?, ?, 1, ?)",
                    (digest, text, db.utcnow()))
    con.commit()


def add_observation(con, obs_id, ep_id, tv, start, end):
    now = db.utcnow()
    con.execute("INSERT INTO ad_observations (id, episode_id, source_version_id, status,"
                " start_s, end_s, created_at, updated_at) VALUES (?, ?, ?, 'proposed', ?, ?, ?, ?)",
                (obs_id, ep_id, tv, start, end, now, now))
    con.commit()


def attestations(con):
    return [dict(r) for r in con.execute(
        "SELECT * FROM coverage_attestations ORDER BY id").fetchall()]


class LegacyCoverageImport(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        self.addCleanup(self.con.close)
        # 60.0 min = 3600 s. full: ends past the rounded duration, with
        # every stage's own evidence; within: 2.5 s short (inside the 3 s
        # tolerance), transcript + index only; short: 3.5 s short; nodur; notx.
        self.tv_full = seed_legacy_episode(self.con, "ep-full", 60.0, 3601.2)
        add_index(self.con, self.tv_full)
        add_screening(self.con, "ep-full", 3601.2)
        add_observation(self.con, "obs:full-1", "ep-full", self.tv_full, 100.0, 160.0)
        self.tv_within = seed_legacy_episode(self.con, "ep-within", 60.0, 3597.5)
        add_index(self.con, self.tv_within)
        seed_legacy_episode(self.con, "ep-short", 60.0, 3596.5)
        seed_legacy_episode(self.con, "ep-nodur", None, 1200.0)
        seed_legacy_episode(self.con, "ep-notx", 60.0, 0.0, with_transcript=False)

    def verdicts(self, result):
        return {e["episode_id"]: e for e in result["episodes"]}

    def stages_of(self, ep):
        return {r["stage"] for r in attestations(self.con) if r["episode_id"] == ep}

    def test_tolerance_is_half_the_duration_rounding_step(self):
        self.assertEqual(coverage_import.TOLERANCE_S, 3.0)

    def test_dry_run_writes_nothing_and_reports_every_episode(self):
        before_rev = db.aggregate_revision(self.con)
        audits = self.con.execute("SELECT count(*) FROM audit").fetchone()[0]
        result = coverage_import.run(self.con, dry_run=True, run_id="r-dry")
        self.assertEqual(attestations(self.con), [])
        self.assertEqual(db.aggregate_revision(self.con), before_rev)
        self.assertEqual(self.con.execute("SELECT count(*) FROM audit").fetchone()[0], audits)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["recorded_attestations"], 0)
        self.assertEqual(result["would_record_episodes"], 2)
        rows = self.verdicts(result)
        full = rows["ep-full"]
        self.assertEqual(full["transcript_version_id"], self.tv_full)
        self.assertAlmostEqual(full["last_segment_end_s"], 3601.2)
        self.assertAlmostEqual(full["known_duration_s"], 3600.0)
        self.assertAlmostEqual(full["gap_s"], -1.2)
        self.assertEqual(full["verdict"], "record")
        self.assertEqual(rows["ep-within"]["verdict"], "record")
        text = coverage_import.format_report(result)
        for field in ("ep-full", str(self.tv_full), "3601.2", "3600.0", "-1.2", "record"):
            self.assertIn(field, text)
        counts = result["stage_counts"]
        self.assertEqual(counts["transcribe"]["record_episodes"], 2)
        self.assertEqual(counts["screen"]["record_episodes"], 1)
        self.assertEqual(counts["screen"]["skipped"], {"no screening record": 1})

    def test_each_stage_is_attested_only_from_its_own_evidence(self):
        coverage_import.run(self.con, dry_run=False, run_id="r-1")
        self.assertEqual(self.stages_of("ep-full"), set(ALL_STAGES))
        # No screening record, no observations: those stages stay unattested.
        self.assertEqual(self.stages_of("ep-within"), {"transcribe", "index"})
        rows = attestations(self.con)
        pub = [r for r in rows if r["episode_id"] == "ep-full"
               and r["stage"] == "observation_publication"]
        self.assertEqual([(r["succ_start_s"], r["succ_end_s"]) for r in pub], [(100.0, 160.0)])
        for r in (r for r in rows if r["episode_id"] == "ep-full"):
            self.assertEqual(r["source_identity"], self.tv_full)
            self.assertEqual(r["source_pin"], f"source-version:{self.tv_full}")
            self.assertEqual(r["status"], "success")
        tx = [r for r in rows if r["episode_id"] == "ep-full" and r["stage"] == "transcribe"]
        self.assertEqual([(r["succ_start_s"], r["succ_end_s"]) for r in tx], [(0.0, 3601.2)])

    def test_screening_of_another_transcript_copy_is_not_attested(self):
        tv = seed_legacy_episode(self.con, "ep-recopy", 60.0, 3600.0)
        add_screening(self.con, "ep-recopy", 3600.0, segment_count=7)
        result = coverage_import.run(self.con, dry_run=False, run_id="r-1")
        stage = self.verdicts(result)["ep-recopy"]["stages"]["screen"]
        self.assertEqual(stage["verdict"], "skip")
        self.assertIn("another transcript copy", stage["reason"])
        self.assertNotIn("screen", self.stages_of("ep-recopy"))
        self.assertTrue(tv)

    def test_screening_with_same_segment_count_on_an_earlier_copy_is_not_attested(self):
        # R1-18: the segment count alone can match by chance; the run entry
        # must name the current copy (version id + segments digest).
        seed_legacy_episode(self.con, "ep-samecount", 60.0, 3600.0)
        older = self.con.execute(
            "INSERT INTO source_versions (source_key, version_no, sha256, payload,"
            " is_active, created_at) VALUES ('transcript:ep-samecount-old', 1, 'old', ?, 0, ?)",
            (json.dumps({"segments": [{"start": 0.0, "end": 1.0, "text": "a"},
                                      {"start": 1.0, "end": 2.0, "text": "b"}]}),
             db.utcnow())).lastrowid
        self.con.commit()
        add_screening(self.con, "ep-samecount", 3600.0, segment_count=2, jev_tv=older)
        seed_legacy_episode(self.con, "ep-nocopy", 60.0, 3600.0)
        add_screening(self.con, "ep-nocopy", 3600.0, segment_count=2, jev_tv=None)
        rows = self.verdicts(coverage_import.run(self.con, dry_run=False, run_id="r-1"))
        same = rows["ep-samecount"]["stages"]["screen"]
        self.assertEqual(same["verdict"], "skip")
        self.assertIn("another transcript copy", same["reason"])
        self.assertIn(str(older), same["reason"])
        nocopy = rows["ep-nocopy"]["stages"]["screen"]
        self.assertEqual(nocopy["verdict"], "skip")
        self.assertIn("transcript copy", nocopy["reason"])
        self.assertFalse({"screen"} & (self.stages_of("ep-samecount") | self.stages_of("ep-nocopy")))
        # The matching copy still records (ep-full, set up with its current copy).
        self.assertIn("screen", self.stages_of("ep-full"))

    def test_screening_record_must_be_full_and_screened(self):
        seed_legacy_episode(self.con, "ep-tier", 60.0, 3600.0)
        add_screening(self.con, "ep-tier", 3600.0, tier="transcript")
        seed_legacy_episode(self.con, "ep-unscreened", 60.0, 3600.0)
        add_screening(self.con, "ep-unscreened", 3600.0, screened=0)
        seed_legacy_episode(self.con, "ep-stopped", 60.0, 3600.0)
        add_screening(self.con, "ep-stopped", 3600.0, stopped=1)
        # Tier 'full' is only the column default: a tier-only row is no evidence.
        seed_legacy_episode(self.con, "ep-noads", 60.0, 3600.0)
        add_screening(self.con, "ep-noads", 3600.0, screened=0, no_ads=1)
        rows = self.verdicts(coverage_import.run(self.con, dry_run=True))
        self.assertEqual(rows["ep-noads"]["stages"]["screen"]["verdict"], "record")
        self.assertIn("screened_no_ads", rows["ep-noads"]["stages"]["screen"]["basis"])
        self.assertIn("tier", rows["ep-tier"]["stages"]["screen"]["reason"])
        self.assertIn("not screened", rows["ep-unscreened"]["stages"]["screen"]["reason"])
        self.assertIn("stopped partway", rows["ep-stopped"]["stages"]["screen"]["reason"])

    def test_unindexed_transcript_gets_no_index_attestation(self):
        result = coverage_import.run(self.con, dry_run=True)
        self.assertEqual(self.verdicts(result)["ep-short"]["verdict"], "skip")
        seed_legacy_episode(self.con, "ep-noindex", 60.0, 3600.0)
        stage = self.verdicts(coverage_import.run(self.con, dry_run=True))["ep-noindex"]["stages"]["index"]
        self.assertEqual(stage["verdict"], "skip")
        self.assertIn("not indexed", stage["reason"])

    def test_skipped_episodes_carry_their_reason(self):
        result = coverage_import.run(self.con, dry_run=False, run_id="r-1")
        rows = self.verdicts(result)
        self.assertEqual(rows["ep-short"]["verdict"], "skip")
        self.assertAlmostEqual(rows["ep-short"]["gap_s"], 3.5)
        self.assertIn("short", rows["ep-short"]["reason"])
        self.assertEqual(rows["ep-nodur"]["verdict"], "skip")
        self.assertIn("no known duration", rows["ep-nodur"]["reason"])
        self.assertEqual(rows["ep-notx"]["verdict"], "skip")
        self.assertIn("no transcript", rows["ep-notx"]["reason"])
        self.assertIsNone(rows["ep-notx"]["transcript_version_id"])
        recorded = {r["episode_id"] for r in attestations(self.con)}
        self.assertFalse(recorded & {"ep-short", "ep-nodur", "ep-notx"})

    def test_second_run_records_zero(self):
        coverage_import.run(self.con, dry_run=False, run_id="r-1")
        first = attestations(self.con)
        rev = db.aggregate_revision(self.con)
        again = coverage_import.run(self.con, dry_run=False, run_id="r-2")
        self.assertEqual(again["recorded_attestations"], 0)
        self.assertEqual(again["recorded_episodes"], 0)
        self.assertEqual(attestations(self.con), first)
        self.assertEqual(db.aggregate_revision(self.con), rev)
        row = self.verdicts(again)["ep-full"]
        self.assertEqual(row["verdict"], "skip")
        self.assertIn("already attested", row["stages"]["transcribe"]["reason"])

    def test_rows_are_auditable_and_distinct_from_real_producers(self):
        coverage_import.run(self.con, dry_run=False, run_id="r-audit", actor="reno-operator")
        rows = attestations(self.con)
        self.assertTrue(rows)
        for r in rows:
            self.assertEqual(r["producer"], f"{coverage_import.PRODUCER}:r-audit")
            self.assertEqual(r["producer_rev"], coverage_import.PRODUCER_REV)
            self.assertEqual(r["actor"], "reno-operator")
        self.assertEqual(coverage_import.PRODUCER, "legacy-coverage-import")
        for real in ("seed-import", "worker-ingest", "index-rebuild", "test-fixture"):
            self.assertFalse(coverage_import.PRODUCER.startswith(real))
        audit = self.con.execute(
            "SELECT * FROM audit WHERE action='coverage_import'").fetchall()
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["target_id"], "r-audit")
        detail = json.loads(audit[0]["detail"])
        self.assertEqual(detail["producer"], "legacy-coverage-import:r-audit")
        self.assertEqual(detail["tolerance_s"], coverage_import.TOLERANCE_NOTE)
        self.assertEqual(coverage_import.TOLERANCE_NOTE,
                         "3 s for 0.1-min grid durations, otherwise exact")
        self.assertIn(coverage_import.TOLERANCE_NOTE, coverage_import.BASIS)
        self.assertIn("transcript", detail["basis"])
        by_ep = {e["episode_id"]: e for e in detail["episodes"]}
        self.assertEqual(set(by_ep), {"ep-full", "ep-within"})
        within = by_ep["ep-within"]
        self.assertEqual(within["stored_duration_min"], 60.0)
        self.assertAlmostEqual(within["last_segment_end_s"], 3597.5)
        self.assertAlmostEqual(within["gap_s"], 2.5)
        self.assertEqual(within["tolerance_s"], 3.0)
        ids = sorted(i for v in by_ep["ep-full"]["attestation_ids"].values() for i in v)
        self.assertEqual(ids, sorted(r["id"] for r in rows if r["episode_id"] == "ep-full"))

    def test_existing_producer_proof_is_never_overridden(self):
        coverage.record_attestation(
            self.con, "ep-full", "screen", (0.0, 3600.0), (0.0, 3600.0),
            source_pin=f"source-version:{self.tv_full}", producer="worker-ingest",
            actor="worker", status="failed")
        coverage.record_attestation(
            self.con, "ep-within", "index", (0.0, 3597.5), (0.0, 3597.5),
            source_pin=f"source-version:{self.tv_within}", producer="index-rebuild",
            actor="index-rebuild")
        self.con.commit()
        result = coverage_import.run(self.con, dry_run=False, run_id="r-1")
        rows = self.verdicts(result)
        self.assertEqual(rows["ep-full"]["verdict"], "skip")
        self.assertIn("failed", rows["ep-full"]["reason"])
        imported = [r for r in attestations(self.con)
                    if r["producer"].startswith(coverage_import.PRODUCER)]
        self.assertFalse([r for r in imported if r["episode_id"] == "ep-full"])
        self.assertEqual({r["stage"] for r in imported if r["episode_id"] == "ep-within"},
                         {"transcribe"})

    def test_superseded_transcript_attestation_does_not_block_current(self):
        coverage_import.run(self.con, dry_run=False, run_id="r-1")
        new_tv = self.con.execute(
            "INSERT INTO source_versions (source_key, version_no, sha256, payload,"
            " is_active, created_at) VALUES ('transcript:ep-full', 2, 'sh2', ?, 1, ?)",
            (json.dumps({"segments": [{"start": 0.0, "end": 3000.0, "text": "x"}]}),
             db.utcnow())).lastrowid
        self.con.execute("UPDATE episodes SET transcript_version_id=? WHERE id='ep-full'", (new_tv,))
        self.con.commit()
        row = self.verdicts(coverage_import.run(self.con, dry_run=False, run_id="r-2"))["ep-full"]
        self.assertEqual(row["transcript_version_id"], new_tv)
        self.assertEqual(row["verdict"], "skip")
        self.assertIn("short", row["reason"])

    def test_never_touches_another_workspace_or_unknown_episodes(self):
        other = fresh_db()
        self.addCleanup(other.close)
        seed_legacy_episode(other, "ep-other", 60.0, 3601.0)
        coverage_import.run(self.con, dry_run=False, run_id="r-1")
        self.assertEqual(attestations(other), [])
        known = {r[0] for r in self.con.execute("SELECT id FROM episodes")}
        self.assertTrue({r["episode_id"] for r in attestations(self.con)} <= known)
        self.assertNotIn("ep-other", {e["episode_id"] for e in
                                      coverage_import.run(self.con, dry_run=True)["episodes"]})

    def test_placement_reaches_complete_only_with_every_stage_own_evidence(self):
        cid = seed_campaign(self.con)
        within = add_placement(self.con, cid, "p2", episode_id="ep-within")

        def state(pid):
            return campaigns.build_customer_draft(self.con, cid)["coverage_by_placement"][pid]["state"]
        coverage_import.run(self.con, dry_run=False, run_id="r-1")
        # Transcript + index only: screening and publication proof missing.
        self.assertNotEqual(state(within), "complete")

    def test_publication_at_observation_bounds_is_not_needed_for_complete(self):
        # Observation publication is recorded at each observation's own
        # bounds (as the observation producer does), so it never proves the
        # whole episode. DET-PLAN item 6: "fully checked" rests on the
        # screen proof, the screening record and the index version, not on
        # publication, so the imported episode reads complete.
        cid = seed_campaign(self.con)
        pid = add_placement(self.con, cid, "p1", episode_id="ep-full")
        coverage_import.run(self.con, dry_run=False, run_id="r-1")
        cov = campaigns.build_customer_draft(self.con, cid)["coverage_by_placement"][pid]
        self.assertEqual(cov["state"], "complete")
        self.assertEqual(cov["episode_checks"]["ep-full"]["state"], "fully_checked")


class TranscriptPastRoundedDuration(unittest.TestCase):
    """Reader rule the import depends on (found on owner data, D-2).

    An episode placement requests max(transcript extent, known duration)
    (_capture_episode_scope). Supplied durations are one-decimal minutes,
    so a transcript often ends a little past them. Proof covering the
    whole request must read as complete; it was clipped to the shorter
    rounded duration and could never be complete.
    """

    def test_full_proof_past_rounded_duration_is_complete(self):
        con = fresh_db()
        self.addCleanup(con.close)
        tv = seed_legacy_episode(con, "ep-long", 60.0, 3601.2)
        cid = seed_campaign(con)
        pid = add_placement(con, cid, "p1", episode_id="ep-long")
        for stage in ALL_STAGES:
            coverage.record_attestation(
                con, "ep-long", stage, (0.0, 3601.2), (0.0, 3601.2),
                source_pin=f"source-version:{tv}", producer="test-fixture", actor="owner")
        con.commit()
        record_screening(con, "ep-long")
        cov = campaigns.build_customer_draft(con, cid)["coverage_by_placement"][pid]
        self.assertEqual(cov["state"], "complete")

    def test_proof_short_of_the_transcript_stays_incomplete(self):
        con = fresh_db()
        self.addCleanup(con.close)
        tv = seed_legacy_episode(con, "ep-long", 60.0, 3601.2)
        cid = seed_campaign(con)
        pid = add_placement(con, cid, "p1", episode_id="ep-long")
        for stage in ALL_STAGES:
            coverage.record_attestation(
                con, "ep-long", stage, (0.0, 3601.2), (0.0, 3600.0),
                source_pin=f"source-version:{tv}", producer="test-fixture", actor="owner")
        con.commit()
        cov = campaigns.build_customer_draft(con, cid)["coverage_by_placement"][pid]
        self.assertEqual(cov["state"], "incomplete")


def add_observation_read(con, ep_id, status=None, attributions=(), start=0.0, end=1800.0):
    """A pipeline observation (its own customer read, pipe:<id>:r1, at
    start-end) whose review lives on the observation (status) and its
    attributions."""
    from earwitness.app import observations
    tv = con.execute("SELECT transcript_version_id FROM episodes WHERE id=?",
                     (ep_id,)).fetchone()[0]
    prop = observations.propose_observation(
        con, ep_id, start, end, bounds_evidence={"excerpt": "a read"},
        fields={"read_type": {"value": "host_read"}},
        attributions=[{"entity": e, "channel": "url", "status": st, "method": "review"}
                      for e, st in attributions],
        source_pins={"source_version_id": tv, "transcript_version_id": tv,
                     "fetch_date": "2026-09-21"}, actor="owner")
    if status:
        observations.review_observation(con, prop["observation_id"], status, 1, actor="owner")
    con.commit()
    return prop


def set_brand(con, ev_id, brand, basis="suggested"):
    from earwitness.app import ops
    rev = con.execute("SELECT assoc_revision FROM evidence WHERE id=?", (ev_id,)).fetchone()[0]
    ops.set_brand_assocs(con, ev_id, [{"brand": brand, "basis": basis, "channel": "spoken"}],
                         rev, actor="owner")
    con.commit()


def review(con, ev_id, judgment):
    from earwitness.app import ops
    ops.save_review(con, ev_id, {"judgment": judgment, "notes": ""}, 0, "owner")
    con.commit()


class _CompleteEpisode(unittest.TestCase):
    """ep-1 with every stage attested; campaign brand Acme; one placement."""

    def setUp(self):
        self.con = fresh_db()
        self.addCleanup(self.con.close)
        self.tv = seed_legacy_episode(self.con, "ep-1", 60.0, 3600.0)
        for stage in ALL_STAGES:
            coverage.record_attestation(
                self.con, "ep-1", stage, (0.0, 3600.0), (0.0, 3600.0),
                source_pin=f"source-version:{self.tv}", producer="test-fixture", actor="owner")
        self.con.commit()
        record_screening(self.con, "ep-1")
        self.cid = seed_campaign(self.con)
        self.pid = add_placement(self.con, self.cid, "p1", episode_id="ep-1")

    def draft(self):
        return campaigns.build_customer_draft(self.con, self.cid)

    def evaluation(self):
        d = self.draft()
        return d, d["coverage_by_placement"][self.pid], d["allocation"]["evaluations"][self.pid]

    def waiting(self):
        return self.evaluation()[1]["waiting_for_review"]


class WaitingForReviewNeverReadsAsNoMatch(_CompleteEpisode):
    """Hard rule 1 + R1-03: complete coverage with reads in scope still
    waiting for review is its own amber linkage `unreviewed`, "N waiting
    for review": never "no match", never a possible detection; on the
    draft, the customer linkage and the frozen report alike."""

    def test_no_detection_at_all_is_no_match(self):
        _d, cov, ev = self.evaluation()
        self.assertEqual(cov["state"], "complete")
        self.assertEqual(cov.get("waiting_for_review"), 0)
        self.assertEqual(ev["outcome"], "no_observation_in_covered_sources")
        self.assertEqual(customer.placement_linkage_label("complete", ev["outcome"], cov),
                         ("none", customer.LINKAGE_NONE))

    def test_waiting_read_in_scope_is_its_own_amber_linkage(self):
        add_candidate(self.con, "det:1", episode_id="ep-1", start=100.0, end=160.0)
        _d, cov, ev = self.evaluation()
        self.assertEqual(cov["waiting_for_review"], 1)
        self.assertEqual(ev["outcome"], "unreviewed_detections_in_scope")
        self.assertIn("unreviewed_detections_in_scope",
                      [getattr(r, "code", None) for r in ev["reasons"]])
        self.assertEqual(customer.placement_linkage_label("complete", ev["outcome"], cov),
                         ("unreviewed", "1 waiting for review"))
        self.assertEqual(customer.placement_customer_linkage(self.con, self.cid, self.pid)
                         ["linkage"], "unreviewed")
        add_candidate(self.con, "det:2", episode_id="ep-1", start=300.0, end=360.0)
        _d, cov, ev = self.evaluation()
        self.assertEqual(customer.placement_linkage_label("complete", ev["outcome"], cov),
                         ("unreviewed", "2 waiting for review"))

    def test_supported_stays_supported_with_waiting_reads(self):
        from tests.test_customer_campaigns import occ, placement
        now = "2026-04-01T00:00:00+00:00"
        cov = {"state": "complete", "waiting_for_review": 3}
        self.assertEqual(campaigns.evaluate_one(placement("p1"), [occ("o1")], cov, now)["outcome"],
                         "supported")
        self.assertEqual(campaigns.evaluate_one(placement("p1", date_to="2026-12-31"), [], cov,
                                                now)["outcome"], "pending")

    def test_earlier_copy_and_rejected_detections_do_not_count(self):
        add_candidate(self.con, "det:old", episode_id="ep-1", start=100.0, end=160.0)
        self.con.execute("UPDATE evidence SET transcript_version_id=NULL WHERE id='det:old'")
        add_candidate(self.con, "det:no", episode_id="ep-1", start=200.0, end=260.0)
        review(self.con, "det:no", "not_ad")
        _d, cov, ev = self.evaluation()
        self.assertEqual(cov["waiting_for_review"], 0)
        self.assertEqual(ev["outcome"], "no_observation_in_covered_sources")

    def test_frozen_report_says_the_same(self):
        add_candidate(self.con, "det:1", episode_id="ep-1", start=100.0, end=160.0)
        add_candidate(self.con, "det:2", episode_id="ep-1", start=300.0, end=360.0)
        d = self.draft()
        rev = self.con.execute("SELECT revision FROM campaigns WHERE id=?", (self.cid,)).fetchone()[0]
        frozen = campaigns.freeze_customer_report(self.con, self.cid, rev, d["pins"])
        body, _ = campaigns.get_customer_report_html(self.con, self.cid, frozen["report_no"])
        self.assertIn("2 waiting for review", body)
        self.assertIn("0 of 1 counted placement has a result yet — 1 with ads waiting for review.", body)
        self.assertNotIn("possible detection", body.lower())
        self.assertNotIn("no matching detection", body.lower())
        self.assertNotIn("unreviewed detection", body.lower())

    def test_overview_lists_the_placement_under_needs_attention(self):
        add_candidate(self.con, "det:1", episode_id="ep-1", start=100.0, end=160.0)
        gaps = [g for g in customer.overview(self.con)["campaign_gaps"]
                if g["placement_id"] == self.pid]
        self.assertEqual(len(gaps), 1)
        self.assertEqual((gaps[0]["linkage"], gaps[0]["label"], gaps[0]["outcome"]),
                         ("unreviewed", "1 waiting for review", "unreviewed_detections_in_scope"))


class OnlyReadsThatCouldBeThePlacementsBrand(_CompleteEpisode):
    """R1-04: only waiting reads that are unnamed or name the placement's
    brand count; a read naming only another brand never makes it uncertain."""

    def test_reads_naming_another_brand_do_not_count(self):
        add_candidate(self.con, "det:other", episode_id="ep-1", start=100.0, end=160.0)
        # A known brand: a machine suggestion of no known brand names nothing.
        set_brand(self.con, "det:other", "Progressive")
        self.assertEqual(self.waiting(), 0)
        self.assertEqual(self.evaluation()[2]["outcome"], "no_observation_in_covered_sources")

    def test_unnamed_and_same_brand_reads_count(self):
        add_candidate(self.con, "det:unnamed", episode_id="ep-1", start=100.0, end=160.0)
        add_candidate(self.con, "det:acme", episode_id="ep-1", start=200.0, end=260.0)
        set_brand(self.con, "det:acme", "ACME")
        # add_observation_read records url-channel names: the other brand must
        # be a known one (R3-01: an unknown web address names nothing).
        # Decision 1 counts ads: each read sits apart so each is its own ad
        # (0-1800 observations would overlap both candidates on one copy and
        # make one ad: WaitingCountsAds in test_uat_d1).
        add_observation_read(self.con, "ep-1", attributions=[("Progressive", "suggested")],
                             start=400.0, end=460.0)
        add_observation_read(self.con, "ep-1", attributions=[("acme", "suggested")],
                             start=600.0, end=660.0)
        self.assertEqual(self.waiting(), 3)

    def test_rejected_attribution_names_nothing(self):
        add_observation_read(self.con, "ep-1", attributions=[("Globex", "rejected")])
        self.assertEqual(self.waiting(), 1)


class OneSettledRuleOnBothStoragePaths(_CompleteEpisode):
    """R1-05: one definition of a settled read, the same whichever table
    holds the review; a reviewed read is never called unreviewed."""

    def test_read_settled_rule(self):
        settled = campaigns.read_settled
        for judgment, obs in (("ad", None), (None, "reviewed_ad"), ("finding", "reviewed_ad")):
            self.assertTrue(settled(judgment, obs, True), (judgment, obs))
            self.assertFalse(settled(judgment, obs, False), (judgment, obs))
        for judgment, obs in (("not_ad", None), (None, "reviewed_not_ad"), (None, "superseded")):
            self.assertTrue(settled(judgment, obs, False), (judgment, obs))
        for judgment, obs in ((None, None), ("finding", None), ("unsure", None),
                              (None, "proposed"), (None, "unsure")):
            self.assertFalse(settled(judgment, obs, True), (judgment, obs))

    def test_evidence_review_path(self):
        add_candidate(self.con, "det:ad-unconfirmed", episode_id="ep-1", start=100.0, end=160.0)
        review(self.con, "det:ad-unconfirmed", "ad")
        set_brand(self.con, "det:ad-unconfirmed", "Acme")
        self.assertEqual(self.waiting(), 1)
        set_brand(self.con, "det:ad-unconfirmed", "Acme", basis="confirmed")
        self.assertEqual(self.waiting(), 0)

    def test_observation_review_path(self):
        # R1-22: the observation-reviewed skip, under the same rule.
        add_observation_read(self.con, "ep-1", status="reviewed_ad",
                             attributions=[("Acme", "suggested")])
        self.assertEqual(self.waiting(), 1)
        self.con.execute("UPDATE observation_attributions SET status='confirmed'")
        self.con.commit()
        self.assertEqual(self.waiting(), 0)
        add_observation_read(self.con, "ep-1", status="reviewed_not_ad")
        self.assertEqual(self.waiting(), 0)

    def test_reason_and_copy_never_say_unreviewed(self):
        add_candidate(self.con, "det:1", episode_id="ep-1", start=100.0, end=160.0)
        review(self.con, "det:1", "ad")
        d, cov, ev = self.evaluation()
        label = customer.placement_linkage_label("complete", ev["outcome"], cov)[1]
        self.assertEqual(label, "1 waiting for review")
        self.assertNotIn("unreviewed", " ".join(str(r) for r in ev["reasons"]).lower())


class _HiddenReads:
    """Viewer that may see transcripts but no detector record."""

    def disclosure_allowed(self, con, key, category):
        return not str(key).startswith("detector:")


class HiddenDataIsUnknownNeverZero(_CompleteEpisode):
    """R1-19: a read or an episode hidden from the viewer makes the count
    unknown (None), never zero, so hidden data never reads as "no match"."""

    def test_hidden_read_is_unknown(self):
        sv = self.con.execute(
            "INSERT INTO source_versions (source_key, version_no, sha256, payload, is_active,"
            " created_at) VALUES ('detector:det:h', 1, 'd', '{}', 1, ?)", (db.utcnow(),)).lastrowid
        self.con.commit()
        add_candidate(self.con, "det:h", episode_id="ep-1", start=100.0, end=160.0,
                      source_version=sv)
        self.assertEqual(campaigns._waiting_for_review(self.con, ["ep-1"], "Acme", None), 1)
        self.assertIsNone(campaigns._waiting_for_review(self.con, ["ep-1"], "Acme", _HiddenReads()))

    def test_hidden_episode_is_unknown(self):
        class NoEpisode:
            def disclosure_allowed(self, con, key, category):
                return False
        self.assertIsNone(campaigns._waiting_for_review(self.con, ["ep-1"], "Acme", NoEpisode()))

    def test_unknown_count_reads_as_not_enough_data(self):
        from unittest import mock
        with mock.patch.object(campaigns, "_waiting_for_review", return_value=None):
            d, cov, ev = self.evaluation()
        self.assertEqual(cov["state"], "unknown")
        self.assertNotEqual(ev["outcome"], "no_observation_in_covered_sources")
        self.assertEqual(customer.placement_linkage_label(cov["state"], ev["outcome"], cov),
                         ("insufficient", customer.LINKAGE_INSUFFICIENT))


class OnePassPerDraftAndOverview(_CompleteEpisode):
    """R1-20: no per-episode episode_reads; one pass over the ad rows per
    draft, and one for the whole Overview."""

    def test_one_pass(self):
        from unittest import mock
        add_candidate(self.con, "det:1", episode_id="ep-1", start=100.0, end=160.0)
        add_placement(self.con, self.cid, "p2", episode_id="ep-1")
        other = seed_campaign(self.con)
        add_placement(self.con, other, "p3", episode_id="ep-1")
        real = customer._customer_ad_rows
        calls = []

        def counting(*a, **k):
            calls.append(1)
            return real(*a, **k)
        with mock.patch.object(customer, "_customer_ad_rows", counting), \
                mock.patch.object(customer, "episode_reads", side_effect=AssertionError("per-episode reads")):
            d = self.draft()
            self.assertEqual(len(calls), 1)
            self.assertEqual({c["waiting_for_review"] for c in d["coverage_by_placement"].values()}, {1})
            # RS1-MEMO: the draft's ad rows are remembered for this database
            # state, so Overview reuses them (no pass); from an empty memo it
            # still makes exactly one pass for every campaign.
            calls.clear()
            gaps = customer.overview(self.con)["campaign_gaps"]
            self.assertEqual(len(calls), 0)
            from earwitness.app import customer_query
            with customer_query._VIEW_MEMO_LOCK:
                customer_query._VIEW_MEMO.clear()
            calls.clear()
            cold = customer.overview(self.con)["campaign_gaps"]
            self.assertEqual(len(calls), 1)
        self.assertEqual(len(gaps), 3)
        self.assertEqual(cold, gaps)


class FrozenReportsAndTheWaitingCount(_CompleteEpisode):
    """R1-21: the waiting count stays out of the coverage pin, so a review
    that leaves the result as it is never stales a frozen report; a review
    that changes the result still does (through the allocation pin)."""

    def test_count_change_alone_never_stales(self):
        add_candidate(self.con, "det:1", episode_id="ep-1", start=100.0, end=160.0)
        add_candidate(self.con, "det:2", episode_id="ep-1", start=300.0, end=360.0)
        d = self.draft()
        rev = self.con.execute("SELECT revision FROM campaigns WHERE id=?", (self.cid,)).fetchone()[0]
        campaigns.freeze_customer_report(self.con, self.cid, rev, d["pins"])
        review(self.con, "det:1", "not_ad")
        d2 = self.draft()
        self.assertEqual(d2["coverage_by_placement"][self.pid]["waiting_for_review"], 1)
        self.assertEqual(d2["pins"]["coverage"], d["pins"]["coverage"])
        report = campaigns.list_customer_reports(self.con, self.cid)[0]
        self.assertFalse(report["stale"], report["stale_reasons"])
        review(self.con, "det:2", "not_ad")
        report = campaigns.list_customer_reports(self.con, self.cid)[0]
        self.assertTrue(report["stale"])
        self.assertIn("allocation", report["stale_reasons"])


class LegacyCoverageImportCli(unittest.TestCase):
    """Operator command: app_workspace.py import-coverage [--dry-run].

    R1-02, the record-alias pattern: the dry run opens the workspace
    read-only and writes nothing (no file creation, DDL, migration or
    backup); the run refuses while a server runs, takes the workspace
    lock, backs up first when something will be recorded, and needs the
    current schema."""

    def setUp(self):
        import tempfile
        sys.path.insert(0, str(REPO / "scripts"))
        import app_workspace
        self.app_workspace = app_workspace
        self.tmp = tempfile.TemporaryDirectory(prefix=".tmp-cov-cli-", dir=REPO)
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        con = db.connect(self.ws)
        db.init_schema(con)
        con.commit()
        seed_legacy_episode(con, "ep-full", 60.0, 3601.2)
        seed_legacy_episode(con, "ep-short", 60.0, 3590.0)
        con.close()

    def cli(self, *extra, ws=None):
        out, err = io.StringIO(), io.StringIO()
        from contextlib import redirect_stderr
        with redirect_stdout(out), redirect_stderr(err):
            code = self.app_workspace.main(["import-coverage", "--workspace",
                                            str(ws or self.ws), *extra])
        return code, out.getvalue(), err.getvalue()

    def snapshot(self, ws=None):
        ws = ws or self.ws
        # Workspace data: the database and any WAL content. The lock file is
        # the lock, and the -shm index is what any SQLite reader (the server
        # too) maps; an empty WAL holds nothing.
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                for p in sorted(ws.iterdir()) if p.is_file()
                and p.name not in ("workspace.lock", "workspace.sqlite3-shm")
                and not (p.name.endswith("-wal") and p.stat().st_size == 0)}

    def count(self):
        con = sqlite3.connect(self.ws / "workspace.sqlite3")
        try:
            return con.execute("SELECT count(*) FROM coverage_attestations").fetchone()[0]
        finally:
            con.close()

    def test_dry_run_writes_nothing(self):
        before = self.snapshot()
        code, out, err = self.cli("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("ep-full", out)
        self.assertIn("ep-short", out)
        self.assertIn("dry run", out.lower())
        self.assertIn("transcribe: qualifying 1", out)
        self.assertEqual(self.snapshot(), before)

    def test_dry_run_on_an_old_schema_refuses_and_migrates_nothing(self):
        from tests.backend_fixtures import make_workspace
        old = self.ws / "old"
        make_workspace(old, schema_version=7)
        before = self.snapshot(old)
        code, _out, err = self.cli("--dry-run", ws=old)
        self.assertEqual(code, 1)
        self.assertIn("schema", err)
        self.assertEqual(self.snapshot(old), before)

    def test_run_on_an_old_schema_refuses_and_writes_nothing_but_the_lock(self):
        # R3-05: the real run (not only the dry run) needs the current schema.
        from tests.backend_fixtures import make_workspace
        old = self.ws / "old"
        make_workspace(old, schema_version=7)
        files = lambda: {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                         for p in sorted(old.iterdir()) if p.is_file()}
        before = files()
        code, _out, err = self.cli("--run-id", "cli-old", ws=old)
        self.assertEqual(code, 1)
        self.assertIn("schema", err)
        after = files()
        self.assertEqual(set(after) - set(before), {"workspace.lock"} - set(before))
        self.assertEqual({k: v for k, v in after.items() if k != "workspace.lock"},
                         {k: v for k, v in before.items() if k != "workspace.lock"})

    def test_missing_workspace_is_never_created(self):
        missing = self.ws / "nowhere"
        code, _out, _err = self.cli("--dry-run", ws=missing)
        self.assertEqual(code, 1)
        self.assertFalse(missing.exists())

    def test_run_refuses_while_server_runs(self):
        import os
        (self.ws / "server.pid").write_text(str(os.getpid()))
        code, _out, err = self.cli("--run-id", "cli-1")
        self.assertEqual(code, 1)
        self.assertIn("server", err)
        self.assertEqual(self.count(), 0)

    def test_run_backs_up_first_then_rerun_is_a_noop(self):
        code, out, err = self.cli("--run-id", "cli-1")
        self.assertEqual(code, 0, err)
        backup = self.ws / "before-import-coverage-cli-1.sqlite3"
        self.assertTrue(backup.is_file())
        self.assertIn(str(backup), out)
        bcon = sqlite3.connect(backup)
        self.assertEqual(bcon.execute("SELECT count(*) FROM coverage_attestations").fetchone()[0], 0)
        bcon.close()
        con = sqlite3.connect(self.ws / "workspace.sqlite3")
        rows = con.execute("SELECT DISTINCT episode_id, producer FROM coverage_attestations").fetchall()
        con.close()
        self.assertEqual(rows, [("ep-full", "legacy-coverage-import:cli-1")])
        code, out, err = self.cli("--run-id", "cli-2")
        self.assertEqual(code, 0, err)
        self.assertIn("recorded 0 attestations", out)
        self.assertFalse((self.ws / "before-import-coverage-cli-2.sqlite3").exists())

    def test_a_run_that_records_nothing_writes_no_audit_row(self):
        # The repo's no-op pattern (record-alias): nothing changes, nothing
        # is written, not even an audit row.
        self.cli("--run-id", "cli-1")
        con = sqlite3.connect(self.ws / "workspace.sqlite3")
        audits = con.execute("SELECT count(*) FROM audit WHERE action='coverage_import'").fetchone()[0]
        con.close()
        self.cli("--run-id", "cli-2")
        con = sqlite3.connect(self.ws / "workspace.sqlite3")
        self.assertEqual(con.execute("SELECT count(*) FROM audit WHERE action='coverage_import'")
                         .fetchone()[0], audits)
        con.close()


if __name__ == "__main__":
    unittest.main()
