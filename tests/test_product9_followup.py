"""Product-9 followup regressions: Tasks 1-9 behavior proof (2026-09-28 plan).

Red-capable coverage command (run BEFORE the fix it must fail with
state == 'complete' on a partial transcript; AFTER it must print
incomplete with explicit missing-interval evidence):

    python3 -m unittest tests.test_product9_followup -v

Every test here asserts observable behavior a consumer can hit, not
source strings or route wiring. Fixtures reuse the shared helpers from
test_customer_campaigns; the partial-transcript helper stays the red
case, and complete coverage needs full-interval recorded stage proof
through the validating attestation write path (never a monkeypatched
outcome).
"""
from __future__ import annotations

import json
import sqlite3
import unittest

from earwitness.app import campaigns, coverage, customer, db, observations, ops
from earwitness.app import citations, customer_contracts, customer_query

from tests.test_customer_campaigns import (
    add_candidate,
    add_placement,
    fresh_db,
    seed_campaign,
    seed_episode,
    seed_full_episode,
    verify_full_ad,
)


def _rev(con, cid):
    return con.execute("SELECT revision FROM campaigns WHERE id=?",
                       (cid,)).fetchone()[0]


def _ref(con, ev_id):
    """Current SourceRef1 for an evidence row. Attach takes an exact
    published state (sr1:); legacy "ev:<id>@v<n>" only qualifies when one
    stored state carries that version (citations.qualify_legacy_source_ref),
    and the fixture republishes evidence, so it is ambiguous by design."""
    return citations.current_source_ref(con, "evidence", ev_id)


def _replace_transcript(con, ep_id, segments):
    """Replace an episode's transcript the way the product producers do
    (import_sources upload path, app_worker ingest, ops episode edits):
    prior versions deactivated, new immutable source version, episode
    revision bump, history snapshot, then republication.
    ``segments=None`` clears the transcript pin."""
    tv = None
    if segments is not None:
        con.execute("UPDATE source_versions SET is_active=0 WHERE source_key=?",
                    (f"transcript:{ep_id}",))
        version_no = con.execute(
            "SELECT COALESCE(MAX(version_no), 0) + 1 FROM source_versions"
            " WHERE source_key=?", (f"transcript:{ep_id}",)).fetchone()[0]
        tv = con.execute(
            "INSERT INTO source_versions (source_key, version_no, sha256,"
            " payload, is_active, created_at) VALUES (?, ?, 'replaced', ?, 1, ?)",
            (f"transcript:{ep_id}", version_no,
             json.dumps({"segments": segments}), db.utcnow())).lastrowid
    con.execute("UPDATE episodes SET transcript_version_id=?,"
                " revision=revision+1, updated_at=? WHERE id=?",
                (tv, db.utcnow(), ep_id))
    db.record_episode_snapshot(con, ep_id, "fixture")
    citations.publish_episode_sources(con, ep_id, "fixture")
    con.commit()
    return tv


def _repin_current(t, ev_id="x"):
    """Explicitly re-attach the placement to the evidence's current
    published state. An attachment is a source binding pinned to one
    publication (backend plan Task 7, "Default attach is a source
    binding"); field confirm/clear publishes a new evidence state
    (observations.confirm_observation_field/clear_observation_field), which
    the old pin deliberately does not see."""
    current = _ref(t.con, ev_id)
    for ref in campaigns.placement_attachments(t.con, t.cid, t.pid):
        if ref != current:
            campaigns.remove_placement_evidence(
                t.con, t.cid, t.pid, ref, _rev(t.con, t.cid))
            campaigns.save_placement_evidence(
                t.con, t.cid, t.pid, current, _rev(t.con, t.cid))
    t.con.commit()


class CoverageAttestationTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()

    def tearDown(self):
        self.con.close()

    def _placement_coverage(self, cid, pid):
        draft = campaigns.build_customer_draft(self.con, cid)
        return draft["coverage_by_placement"][pid]

    def test_partial_transcript_never_complete(self):
        """RED-CAPABLE Task 1: 60-minute episode, 150s transcript, a
        reviewed 10s ad. Before the fix this reported complete with
        searchable_hours 0.041667; after, incomplete with the missing
        interval named."""
        seed_episode(self.con)
        cid = seed_campaign(self.con)
        add_candidate(self.con, "x")
        verify_full_ad(self.con, "x")
        pid = add_placement(self.con, cid, episode_id="ep-1")
        cov = self._placement_coverage(cid, pid)
        self.assertNotEqual(cov["state"], "complete")
        self.assertEqual(cov["state"], "incomplete")
        self.assertEqual(cov["missing"], ["ep-1"])
        exclusions = " ".join(cov.get("exclusions", []))
        self.assertIn("150.0s", exclusions)
        self.assertIn("3600.0s", exclusions)

    def test_complete_with_full_stage_proof(self):
        seed_full_episode(self.con, ep_id="ep-full")
        cid = seed_campaign(self.con)
        add_candidate(self.con, "x", episode_id="ep-full")
        verify_full_ad(self.con, "x", episode_id="ep-full")
        pid = add_placement(self.con, cid, episode_id="ep-full")
        cov = self._placement_coverage(cid, pid)
        self.assertEqual(cov["state"], "complete")
        self.assertAlmostEqual(cov["searchable_hours"], 1.0, places=4)

    def test_holes_stay_incomplete_overlaps_complete(self):
        seed_full_episode(self.con, ep_id="ep-full")
        cid = seed_campaign(self.con)
        pid = add_placement(self.con, cid, episode_id="ep-full")
        con = self.con
        con.execute("DELETE FROM coverage_attestations WHERE episode_id='ep-full'")
        pin = "source-version:1"
        coverage.record_attestation(con, "ep-full", "transcribe",
                                    (0.0, 3600.0), (0.0, 3600.0),
                                    source_pin=pin, actor="owner")
        for stage in ("screen", "observation_publication", "index"):
            coverage.record_attestation(con, "ep-full", stage,
                                        (0.0, 3600.0), (0.0, 1800.0),
                                        source_pin=pin, actor="owner")
            coverage.record_attestation(con, "ep-full", stage,
                                        (0.0, 3600.0), (2400.0, 3600.0),
                                        source_pin=pin, actor="owner")
        con.commit()
        cov = self._placement_coverage(cid, pid)
        self.assertEqual(cov["state"], "incomplete")  # 600s hole
        # Close the hole with an overlapping attestation: complete.
        for stage in ("screen", "observation_publication", "index"):
            coverage.record_attestation(con, "ep-full", stage,
                                        (0.0, 3600.0), (1200.0, 3000.0),
                                        source_pin=pin, actor="owner")
        con.commit()
        cov = self._placement_coverage(cid, pid)
        self.assertEqual(cov["state"], "complete")

    def test_failed_latest_supersedes_earlier_success(self):
        seed_full_episode(self.con, ep_id="ep-full")
        cid = seed_campaign(self.con)
        pid = add_placement(self.con, cid, episode_id="ep-full")
        coverage.record_attestation(
            self.con, "ep-full", "screen", (0.0, 3600.0), (0.0, 3600.0),
            source_pin="source-version:1", actor="owner", status="failed")
        self.con.commit()
        cov = self._placement_coverage(cid, pid)
        self.assertNotEqual(cov["state"], "complete")
        self.assertIn("screen", " ".join(cov.get("exclusions", [])))

    def test_failed_fetch_blocks_complete(self):
        seed_full_episode(self.con, ep_id="ep-full")
        cid = seed_campaign(self.con)
        pid = add_placement(self.con, cid, episode_id="ep-full")
        coverage.record_attestation(
            self.con, "ep-full", "fetch", (0.0, 3600.0), (0.0, 3600.0),
            source_pin="fetch:feed-1", actor="owner", status="failed")
        self.con.commit()
        cov = self._placement_coverage(cid, pid)
        self.assertNotEqual(cov["state"], "complete")
        self.assertIn("fetch", " ".join(cov.get("exclusions", [])))

    def test_replaced_source_loses_old_proof(self):
        """RED-CAPABLE correction: full proof recorded against transcript
        v2, then the transcript is replaced by a 150s partial (v3).
        Before the fix this reported complete with searchable_hours 1.0
        from the stale union; after, incomplete with a stale-source
        exclusion, and the new partial never inherits old proof."""
        old_tv = seed_full_episode(self.con, ep_id="ep-stale")
        cid = seed_campaign(self.con)
        pid = add_placement(self.con, cid, episode_id="ep-stale")
        new_tv = _replace_transcript(
            self.con, "ep-stale",
            [{"start": 0, "end": 150, "text": "replacement partial"}])
        self.assertNotEqual(old_tv, new_tv)
        # The placement captured the old source scope (backend plan
        # Task 6): a replacement invalidates it instead of silently
        # widening, so coverage is unknown until the scope is recaptured.
        cov = self._placement_coverage(cid, pid)
        self.assertNotEqual(cov["state"], "complete")
        self.assertEqual(cov["state"], "unknown")
        self.assertIn("captured source scope is stale", cov["ambiguity"])
        # Explicit episode-scope replacement recaptures the current source.
        placement_rev = self.con.execute(
            "SELECT revision FROM expected_placements WHERE id=?",
            (pid,)).fetchone()[0]
        campaigns.save_customer_placement(
            self.con, cid,
            {"placement": {"scope": "episode", "episode_id": "ep-stale"}},
            placement_id=pid, expected_revision=placement_rev,
            expected_campaign_revision=_rev(self.con, cid))
        self.con.commit()
        cov = self._placement_coverage(cid, pid)
        self.assertNotEqual(cov["state"], "complete")
        self.assertEqual(cov["state"], "incomplete")
        exclusions = " ".join(cov.get("exclusions", []))
        self.assertIn("predates the current source", exclusions)
        self.assertIn("150.0s", exclusions)
        # Recording fresh proof against the current source restores
        # exactly what the new source proves: still incomplete here
        # because the replacement transcript is only a 150s partial.
        pin = f"source-version:{new_tv}"
        for stage in ("transcribe", "screen", "observation_publication",
                      "index"):
            coverage.record_attestation(
                self.con, "ep-stale", stage, (0.0, 3600.0), (0.0, 150.0),
                source_pin=pin, actor="owner")
        self.con.commit()
        cov = self._placement_coverage(cid, pid)
        self.assertEqual(cov["state"], "incomplete")
        self.assertNotIn("predates the current source",
                         " ".join(cov.get("exclusions", [])))

    def test_episode_without_proof_is_unknown(self):
        seed_episode(self.con)
        _replace_transcript(self.con, "ep-1", None)
        cid = seed_campaign(self.con)
        pid = add_placement(self.con, cid, episode_id="ep-1")
        cov = self._placement_coverage(cid, pid)
        self.assertEqual(cov["state"], "unknown")

    def test_attestation_write_path_rejects_invention(self):
        seed_episode(self.con)
        con = self.con
        with self.assertRaises(ValueError):
            coverage.record_attestation(con, "ep-1", "watch", (0.0, 10.0),
                                        (0.0, 10.0), source_pin="x",
                                        actor="owner")
        with self.assertRaises(ValueError):
            coverage.record_attestation(con, "ep-1", "screen", (0.0, 10.0),
                                        (0.0, 99.0), source_pin="x",
                                        actor="owner")
        with self.assertRaises(ValueError):
            coverage.record_attestation(con, "ep-1", "screen", (0.0, 10.0),
                                        (0.0, 10.0), source_pin="x",
                                        actor="reader")
        with self.assertRaises(ValueError):
            coverage.record_attestation(con, "nope", "screen", (0.0, 10.0),
                                        (0.0, 10.0), source_pin="x",
                                        actor="owner")

    def test_attach_after_freeze_marks_report_stale(self):
        seed_full_episode(self.con, ep_id="ep-full")
        cid = seed_campaign(self.con)
        add_candidate(self.con, "x", episode_id="ep-full")
        verify_full_ad(self.con, "x", episode_id="ep-full")
        pid = add_placement(self.con, cid, episode_id="ep-full")
        rev = _rev(self.con, cid)
        out = campaigns.save_placement_evidence(self.con, cid, pid, _ref(self.con, "x"), rev)
        draft = campaigns.build_customer_draft(self.con, cid)
        frozen = campaigns.freeze_customer_report(
            self.con, cid, out["campaign_revision"], draft["pins"])
        self.assertEqual(
            campaigns.get_customer_report(
                self.con, cid, frozen["report_no"])["stale"], False)
        add_candidate(self.con, "y", episode_id="ep-full")
        verify_full_ad(self.con, "y", episode_id="ep-full")
        rev2 = _rev(self.con, cid)
        campaigns.save_placement_evidence(self.con, cid, pid, _ref(self.con, "y"), rev2)
        self.assertEqual(
            campaigns.get_customer_report(
                self.con, cid, frozen["report_no"])["stale"], True)


def _full_query(**over):
    base = {"version": 2, "mode": "mentions", "q": "acme",
            "brands": ["Acme"], "from": "2026-03-01", "to": "2026-03-31",
            "statuses": ["confirmed"], "sort": "newest"}
    base.update(over)
    return base


class AlertScopeTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con)
        add_candidate(self.con, "x")
        verify_full_ad(self.con, "x")

    def tearDown(self):
        self.con.close()

    def test_full_scope_round_trip_and_name_only_edit(self):
        saved = customer.save_alert(self.con, "watch", _full_query())
        first = customer.get_alert(self.con, saved["id"])
        stored = first["query"]
        for key in ("mode", "q", "brands", "from", "to", "statuses"):
            self.assertEqual(stored[key], _full_query()[key])
        renamed = customer.save_alert(
            self.con, "renamed", _full_query(),
            alert_id=saved["id"], expected_revision=saved["revision"])
        after = customer.get_alert(self.con, saved["id"])
        self.assertEqual(after["query"], first["query"])
        self.assertEqual(after["baseline_count"], first["baseline_count"])
        self.assertTrue(after["baseline_complete"])
        self.assertEqual(after["definition_revision"],
                         first["definition_revision"])
        self.assertEqual(renamed["revision"], saved["revision"] + 1)

    def test_explicit_criteria_edit_invalidates_until_silent_check(self):
        saved = customer.save_alert(self.con, "watch", _full_query())
        customer.check_alert(self.con, saved["id"])
        before = customer.get_alert(self.con, saved["id"])
        self.assertIsNotNone(before["last_successful_at"])
        edited = customer.save_alert(
            self.con, "watch", _full_query(**{"to": "2026-03-10"}),
            alert_id=saved["id"], expected_revision=before["revision"])
        after = customer.get_alert(self.con, saved["id"])
        self.assertEqual(edited["definition_revision"],
                         before["definition_revision"] + 1)
        # Invalidated, not recomputed: prior baseline retained as
        # history, no success claimed for the new definition.
        self.assertFalse(after["baseline_complete"])
        self.assertEqual(after["last_successful_at"],
                         before["last_successful_at"])
        self.assertEqual(after["last_successful_definition_revision"],
                         before["definition_revision"])
        # The next successful check establishes the new definition
        # silently.
        self.assertEqual(
            customer.check_alert(self.con, saved["id"])["emitted"], 0)
        self.assertTrue(customer.get_alert(
            self.con, saved["id"])["baseline_complete"])

    def test_exact_zero_scope_establishes_empty_baseline(self):
        saved = customer.save_alert(
            self.con, "zero", _full_query(**{"q": "zzz-no-such-term"}))
        self.assertFalse(saved.get("setup_required"))
        out = customer.check_alert(self.con, saved["id"])
        self.assertTrue(out["ok"])
        alert = customer.get_alert(self.con, saved["id"])
        self.assertIsNotNone(alert["last_successful_at"])
        self.assertTrue(alert["baseline_complete"])
        self.assertEqual(alert["baseline_count"], 0)


class BrandCountTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con)
        seed_campaign(self.con)

    def tearDown(self):
        self.con.close()

    def _counts(self):
        directory = {b["brand"]: b["confirmed"]
                     for b in customer.brands_directory(self.con)}
        profile = customer.brand_detail(self.con, "acme")
        compared = customer.compare(
            self.con, ["Acme"],
            {"from": "2026-01-01", "to": "2026-12-31"})
        return directory, profile, compared

    def test_duplicate_refs_count_once_everywhere(self):
        # Two evidence rows for one occurrence: the second row resolves
        # to the first row's observation/occurrence, so every surface
        # counts one confirmed occurrence, not two association rows.
        add_candidate(self.con, "dup-a")
        made = verify_full_ad(self.con, "dup-a")
        add_candidate(self.con, "dup-b",
                      attrs={"observation_id": made["observation_id"]})
        ops.save_review(self.con, "dup-b", {"judgment": "ad"}, 0)
        ops.set_brand_assocs(
            self.con, "dup-b",
            [{"brand": "acme", "basis": "confirmed", "channel": "spoken"}], 0)
        directory, profile, compared = self._counts()
        self.assertEqual(directory.get("acme"), 1)
        self.assertEqual(len(profile["confirmed_occurrences"]), 1)
        cell = [c for c in compared["cells"] if c["brand"] == "Acme"][0]
        # PLAIN-ADS (DEP7): Compare counts ads; the two rows are one ad.
        self.assertEqual(cell["a"]["ads"], 1)

    def test_rejecting_both_rows_zeroes_counts(self):
        add_candidate(self.con, "neg-a")
        ops.save_review(self.con, "neg-a", {"judgment": "not_ad"}, 0)
        ops.set_brand_assocs(
            self.con, "neg-a",
            [{"brand": "acme", "basis": "confirmed", "channel": "spoken"}], 0)
        # The directory is built from the exact ads scope
        # (customer.brands_directory): a brand whose only rows were judged
        # not-an-ad has no rows there, so it leaves the directory and its
        # profile is unknown rather than a zero-count profile.
        directory = {b["brand"]: b["confirmed"]
                     for b in customer.brands_directory(self.con)}
        self.assertNotIn("acme", directory)
        with self.assertRaisesRegex(ops.BadInput, "unknown brand"):
            customer.brand_detail(self.con, "acme")
        compared = customer.compare(
            self.con, ["Acme"], {"from": "2026-01-01", "to": "2026-12-31"})
        cell = [c for c in compared["cells"] if c["brand"] == "Acme"][0]
        self.assertEqual(cell["a"]["count"], 0)


class ScopeDatesTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con, ep_id="ep-mar", pub_date="2026-03-10")
        seed_episode(self.con, ep_id="ep-jun", pub_date="2026-06-10",
                     version_no=2)

    def tearDown(self):
        self.con.close()

    def test_date_range_narrows_and_clearing_restores(self):
        narrowed = customer.query(
            self.con, customer_contracts.upcast_query_v1({"mode": "episodes", "from": "2026-03-01", "to": "2026-03-31"})[0])
        self.assertEqual(narrowed["total"], 1)
        cleared = customer.query(
            self.con, customer_contracts.upcast_query_v1({"mode": "episodes"})[0])
        self.assertEqual(cleared["total"], 2)

    def test_invalid_dates_rejected(self):
        from earwitness.app import search as _search
        with self.assertRaises(_search.FilterRejected):
            customer_contracts.upcast_query_v1({"mode": "episodes", "from": "2026-13-01"})[0]
        with self.assertRaises(_search.FilterRejected):
            customer_contracts.upcast_query_v1({"mode": "episodes", "from": "2026-03-10",
                                  "to": "2026-03-01"})[0]


class AttachContractTest(unittest.TestCase):
    """Task 5: one explicit ref/cid/pid/revision contract for the inline
    form and the modal. The domain half is pinned here (exact pair,
    wrong campaign/placement, failed-fetch disclosure, stale revision with
    the retained current revision); both UI entrypoints submit the same
    four values from the submitted form only."""

    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con)
        self.cid = seed_campaign(self.con)
        add_candidate(self.con, "x")
        verify_full_ad(self.con, "x")
        self.pid = add_placement(self.con, self.cid, episode_id="ep-1")

    def tearDown(self):
        self.con.close()

    def test_exact_attach_ok(self):
        out = campaigns.save_placement_evidence(
            self.con, self.cid, self.pid, _ref(self.con, "x"), _rev(self.con, self.cid))
        self.assertEqual(out["placement_id"], self.pid)
        self.assertEqual(out["ref"], _ref(self.con, "x"))
        self.assertIn("campaign_revision", out)

    def test_wrong_campaign_pair_rejected(self):
        other = seed_campaign(self.con, brand="Other")
        with self.assertRaises(ops.BadInput):
            campaigns.save_placement_evidence(
                self.con, other, self.pid, _ref(self.con, "x"),
                _rev(self.con, other))

    def test_unknown_placement_rejected(self):
        with self.assertRaises(ops.BadInput):
            campaigns.save_placement_evidence(
                self.con, self.cid, "exp:missing", _ref(self.con, "x"),
                _rev(self.con, self.cid))

    def test_failed_latest_fetch_is_disclosed_not_rejected(self):
        # The latest fetch attestation for the episode failed. Attach is a
        # source binding to an exact published state (7833051 removed the
        # fetch-failure attach guard; backend plan Task 7): it succeeds,
        # and the failure is disclosed in coverage instead. Earlier success
        # is never reused to claim complete coverage.
        from earwitness.app import coverage as _coverage
        _coverage.record_attestation(
            self.con, "ep-1", "fetch", (0.0, 60.0), (0.0, 60.0),
            source_pin="source-version:2", producer="test-fixture",
            actor="owner", status="failed")
        self.con.commit()
        out = campaigns.save_placement_evidence(
            self.con, self.cid, self.pid, _ref(self.con, "x"),
            _rev(self.con, self.cid))
        self.assertEqual(out["ref"], _ref(self.con, "x"))
        draft = campaigns.build_customer_draft(self.con, self.cid)
        cov = draft["coverage_by_placement"][self.pid]
        self.assertNotEqual(cov["state"], "complete")
        self.assertEqual(cov["state"], "incomplete")
        self.assertIn("ep-1: latest fetch attestation failed", cov["exclusions"])
        evaluation = draft["allocation"]["evaluations"][self.pid]
        self.assertEqual(evaluation["outcome"], "supported")
        self.assertIn("supported with remaining source gaps disclosed",
                      evaluation["reasons"])
        self.assertEqual(evaluation["coverage_state"], "incomplete")

    def test_stale_revision_conflict_carries_current(self):
        campaigns.save_placement_evidence(
            self.con, self.cid, self.pid, _ref(self.con, "x"), _rev(self.con, self.cid))
        add_candidate(self.con, "y")
        verify_full_ad(self.con, "y")
        with self.assertRaises(ops.Conflict) as ctx:
            campaigns.save_placement_evidence(
                self.con, self.cid, self.pid, _ref(self.con, "y"), 1)
        self.assertEqual(ctx.exception.current_revision,
                         _rev(self.con, self.cid))


class RevisionAdvanceTest(unittest.TestCase):
    """Task 6: the owning campaign revision advances 1->2->3 on actual
    changes; idempotent duplicates change nothing; stale removes fail."""

    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con)
        self.cid = seed_campaign(self.con)
        add_candidate(self.con, "x")
        verify_full_ad(self.con, "x")
        add_candidate(self.con, "y")
        verify_full_ad(self.con, "y")
        self.pid = add_placement(self.con, self.cid, episode_id="ep-1")

    def tearDown(self):
        self.con.close()

    def _snapshots(self):
        return self.con.execute(
            "SELECT COUNT(*) FROM campaign_history WHERE campaign_id=?",
            (self.cid,)).fetchone()[0]

    def _audits(self):
        return self.con.execute(
            "SELECT COUNT(*) FROM audit WHERE target_type='campaign'"
            " AND target_id=?", (str(self.cid),)).fetchone()[0]

    def test_attach_detach_advance_revision_and_audit(self):
        self.assertEqual(_rev(self.con, self.cid), 1)
        first = campaigns.save_placement_evidence(
            self.con, self.cid, self.pid, _ref(self.con, "x"), 1)
        self.assertEqual(first["campaign_revision"], 2)
        snaps, audits = self._snapshots(), self._audits()
        self.assertGreaterEqual(snaps, 1)
        self.assertGreaterEqual(audits, 1)
        second = campaigns.save_placement_evidence(
            self.con, self.cid, self.pid, _ref(self.con, "y"),
            first["campaign_revision"])
        self.assertEqual(second["campaign_revision"], 3)
        removed = campaigns.remove_placement_evidence(
            self.con, self.cid, self.pid, _ref(self.con, "x"),
            second["campaign_revision"])
        self.assertEqual(removed["campaign_revision"], 4)
        self.assertEqual(self._snapshots(), snaps + 2)
        self.assertEqual(_rev(self.con, self.cid), 4)

    def test_duplicate_attach_is_idempotent_no_change(self):
        first = campaigns.save_placement_evidence(
            self.con, self.cid, self.pid, _ref(self.con, "x"), 1)
        snaps, audits = self._snapshots(), self._audits()
        dup = campaigns.save_placement_evidence(
            self.con, self.cid, self.pid, _ref(self.con, "x"),
            first["campaign_revision"])
        self.assertTrue(dup.get("duplicate"))
        self.assertEqual(dup["campaign_revision"],
                         first["campaign_revision"])
        self.assertEqual(self._snapshots(), snaps)
        self.assertEqual(self._audits(), audits)

    def test_stale_remove_rejected(self):
        first = campaigns.save_placement_evidence(
            self.con, self.cid, self.pid, _ref(self.con, "x"), 1)
        campaigns.save_placement_evidence(
            self.con, self.cid, self.pid, _ref(self.con, "y"),
            first["campaign_revision"])
        with self.assertRaises(ops.Conflict):
            campaigns.remove_placement_evidence(
                self.con, self.cid, self.pid, _ref(self.con, "x"),
                first["campaign_revision"])


class ExactEmptyTest(unittest.TestCase):
    """Task 7: explicit empty ids are exact-empty at the parser and
    short-circuit matching, even under an exhausted scan budget.
    Omission stays unrestricted."""

    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con)
        # Two segments so an exhausted budget (cap 1) actually truncates
        # the unrestricted scan while exact-empty short-circuits it.
        row = self.con.execute(
            "SELECT payload FROM source_versions WHERE source_key='transcript:ep-1'"
        ).fetchone()
        segments = json.loads(row["payload"])["segments"]
        segments.append({"start": 150.0, "end": 300.0,
                         "text": "more acme talk"})
        _replace_transcript(self.con, "ep-1", segments)
        self._cap = customer.SCAN_SEGMENT_CAP

    def tearDown(self):
        customer.SCAN_SEGMENT_CAP = self._cap
        customer_query._VIEW_MEMO.clear()  # the cap is an input outside the memo key
        self.con.close()

    def test_explicit_empty_exact_under_exhausted_budget(self):
        customer.SCAN_SEGMENT_CAP = 1
        customer_query._VIEW_MEMO.clear()  # the cap is an input outside the memo key
        for raw_ids in ([], "__none__", ""):
            with self.subTest(raw_ids=raw_ids):
                criteria = customer_contracts.upcast_query_v1({"mode": "mentions", "ids": raw_ids})[0]
                self.assertEqual(criteria["ids"], [])
                result = customer.query(self.con, criteria)
                self.assertEqual(result["total"], 0)
                self.assertEqual(result["total_relation"], "exact")
                self.assertIsNone(result["truncation_reason"])
                self.assertEqual(result["items"], [])

    def test_empty_csv_succeeds(self):
        customer.SCAN_SEGMENT_CAP = 1
        customer_query._VIEW_MEMO.clear()  # the cap is an input outside the memo key
        out = customer_query.capture_export(self.con,
            customer_contracts.upcast_query_v1({"mode": "mentions", "ids": []})[0], None, "all_matches", "csv",
            customer_query._view_revision(self.con))
        self.assertEqual(out["rows"], [])
        self.assertEqual(out["total"], 0)

    def test_empty_baseline_succeeds(self):
        customer.SCAN_SEGMENT_CAP = 1
        customer_query._VIEW_MEMO.clear()  # the cap is an input outside the memo key
        saved = customer.save_alert(
            self.con, "empty",
            {"version": 2, "mode": "mentions", "ids": []})
        self.assertFalse(saved.get("setup_required"))
        out = customer.check_alert(self.con, saved["id"])
        self.assertTrue(out["ok"])
        alert = customer.get_alert(self.con, saved["id"])
        self.assertIsNotNone(alert["last_successful_at"])

    def test_omission_stays_unrestricted(self):
        customer.SCAN_SEGMENT_CAP = 1
        customer_query._VIEW_MEMO.clear()  # the cap is an input outside the memo key
        criteria = customer_contracts.upcast_query_v1({"mode": "mentions"})[0]
        self.assertIsNone(criteria["ids"])
        result = customer.query(self.con, criteria)
        self.assertEqual(result["total_relation"], "lower_bound")
        self.assertEqual(result["truncation_reason"], "SCAN_CAP")

    def test_invalid_ids_still_fail(self):
        from earwitness.app import search as _search
        with self.assertRaises(_search.FilterRejected):
            customer_contracts.upcast_query_v1({"mode": "mentions", "ids": "nope"})[0]
        with self.assertRaises(_search.FilterRejected):
            customer_contracts.upcast_query_v1({"mode": "mentions", "ids": [123]})[0]
        with self.assertRaises(_search.FilterRejected):
            customer_contracts.upcast_query_v1({"mode": "mentions", "ids": [f"ev:{i}@v1" for i in range(201)]})[0]


class ShowIdentityTest(unittest.TestCase):
    """Task 8: the episode breadcrumb uses the authoritative show
    identity. Same-title collisions resolve to the episode's own feed;
    unknown identities stay unlinked, never guessed."""

    def setUp(self):
        self.con = fresh_db()
        # Same display title, two distinct feeds: the authoritative
        # mapping keys on feed identity, never on title equality. The
        # feed metadata exists before the episodes are published, because
        # the first publication builds the show map and
        # rebuild_customer_show_map never reassigns an existing mapping (a
        # later conflict becomes "unresolved"). seed_episode's own
        # single-feed episodes.json rows are older versions (1, 2), so this
        # newest version (3) is what each episode resolves to.
        self.con.execute(
            "INSERT INTO source_versions (source_key, version_no, sha256, payload,"
            " is_active, created_at) VALUES ('episodes.json', 3, 'sh2', ?, 1, ?)",
            (json.dumps([{"ep_id": "ep-a", "show_title": "Morning Show",
                          "feed_url": "https://a.example/rss"},
                         {"ep_id": "ep-b", "show_title": "Morning Show",
                          "feed_url": "https://b.example/rss"}]),
             db.utcnow()))
        self.con.commit()
        seed_episode(self.con, ep_id="ep-a", show="Morning Show")
        seed_episode(self.con, ep_id="ep-b", show="Morning Show",
                     version_no=2)

    def tearDown(self):
        self.con.close()

    def test_collision_resolves_own_feed(self):
        customer.ensure_customer_show_map(self.con)
        rec_a = customer.record(self.con, "ep:ep-a")
        rec_b = customer.record(self.con, "ep:ep-b")
        self.assertIn("show", rec_a)
        self.assertIn("show", rec_b)
        self.assertFalse(rec_a["show"].get("unresolved"))
        self.assertFalse(rec_b["show"].get("unresolved"))
        self.assertNotEqual(rec_a["show"]["show_identity_id"],
                            rec_b["show"]["show_identity_id"])

    def test_unknown_show_stays_unlinked(self):
        self.con.execute(
            "INSERT INTO episodes (id, kind, show_title, ep_title, category,"
            " pub_date, duration_min, fetch_date, revision, updated_at)"
            " VALUES ('ep-ghost', 'supplied', 'Ghost Show', 'G', 'news',"
            " '2026-03-10', 30.0, '2026-09-21', 1, ?)", (db.utcnow(),))
        citations.publish_episode_sources(self.con, "ep-ghost", "fixture")
        self.con.commit()
        rec = customer.record(self.con, "ep:ep-ghost")
        self.assertTrue(rec["show"].get("unresolved"))


class FieldConfirmationTest(unittest.TestCase):
    """Task 9: real persisted workflow — propose, explicitly confirm each
    field, evaluate supported/mismatch, then revoke/change source and
    watch the predicate return to unknown/stale. No hardcoded
    verified=True fixture substitutes for the write path."""

    def setUp(self):
        self.con = fresh_db()
        seed_full_episode(self.con, ep_id="ep-full")
        self.cid = seed_campaign(self.con)
        add_candidate(self.con, "x", episode_id="ep-full",
                      attrs={"read_type": "host_read", "message": "bright",
                             "offer": "WIDGET10", "code": "WIDGET10"})
        made = verify_full_ad(self.con, "x", episode_id="ep-full")
        self.obs_id = made["observation_id"]
        self.pid = add_placement(self.con, self.cid, episode_id="ep-full",
                                 requirements={"duration_s": [5, 30],
                                               "message": "bright",
                                               "offer": "WIDGET10"})
        rev = _rev(self.con, self.cid)
        campaigns.save_placement_evidence(
            self.con, self.cid, self.pid, _ref(self.con, "x"), rev)

    def tearDown(self):
        self.con.close()

    def _confirm_all(self, revision):
        rev = revision
        for field, value in (("read_type", "host_read"),
                             ("message", "bright"),
                             ("offer", "WIDGET10"),
                             ("code", "WIDGET10")):
            out = observations.confirm_observation_field(
                self.con, self.obs_id, field, value, rev,
                evidence={"segment_ref": 0}, actor="owner")
            rev = out["revision"]
        return rev

    def _comparisons(self):
        _repin_current(self)
        draft = campaigns.build_customer_draft(self.con, self.cid)
        alloc = draft["allocation"]["evaluations"][self.pid]
        rows = {}
        for row in draft["comparisons"].get(self.pid, []):
            rows[row["field"]] = row
        return alloc, rows

    def test_unverified_copy_stays_unknown(self):
        # Campaign-compared copy fields (message/offer) stay unknown
        # without a current human confirmation.
        _alloc, rows = self._comparisons()
        for field in ("message", "offer"):
            self.assertEqual(rows[field]["state"], "unknown")
            self.assertIsNone(rows[field]["confirmation"])

    def test_confirmed_matching_copy_supports(self):
        obs_rev = self.con.execute(
            "SELECT revision FROM ad_observations WHERE id=?",
            (self.obs_id,)).fetchone()[0]
        self._confirm_all(obs_rev)
        _alloc, rows = self._comparisons()
        for field in ("message", "offer"):
            self.assertEqual(rows[field]["state"], "supported")
            self.assertEqual(rows[field]["confirmation"]["actor"], "owner")

    def test_confirmed_conflicting_value_mismatches(self):
        # The placement expects OTHER10; the verified observed value is
        # WIDGET10: a verified conflicting value mismatches, and the
        # confirmation provenance rides along.
        self.con.execute(
            "UPDATE expected_placements SET requirements=? WHERE id=?",
            (json.dumps({"duration_s": [5, 30], "message": "bright",
                         "offer": "OTHER10"}), self.pid))
        self.con.commit()
        obs_rev = self.con.execute(
            "SELECT revision FROM ad_observations WHERE id=?",
            (self.obs_id,)).fetchone()[0]
        self._confirm_all(obs_rev)
        _alloc, rows = self._comparisons()
        self.assertEqual(rows["offer"]["state"], "mismatch")
        self.assertEqual(rows["offer"]["observed"], "WIDGET10")
        self.assertEqual(rows["offer"]["confirmation"]["actor"], "owner")
        self.assertEqual(rows["message"]["state"], "supported")

    def test_clear_returns_predicate_to_unknown(self):
        obs_rev = self.con.execute(
            "SELECT revision FROM ad_observations WHERE id=?",
            (self.obs_id,)).fetchone()[0]
        rev = self._confirm_all(obs_rev)
        _alloc, rows = self._comparisons()
        self.assertEqual(rows["message"]["state"], "supported")
        observations.clear_observation_field(
            self.con, self.obs_id, "message", rev, actor="owner")
        _alloc, rows = self._comparisons()
        self.assertEqual(rows["message"]["state"], "unknown")
        self.assertIsNone(rows["message"]["confirmation"])

    def test_machine_actor_and_stale_revision_rejected(self):
        with self.assertRaises(ops.BadInput):
            observations.confirm_observation_field(
                self.con, self.obs_id, "message", "bright", 2,
                evidence={"segment_ref": 0}, actor="pipeline")
        with self.assertRaises(ops.Conflict):
            observations.confirm_observation_field(
                self.con, self.obs_id, "message", "bright", 999,
                evidence={"segment_ref": 0}, actor="owner")


class HumanCorrectionTest(unittest.TestCase):
    """RED-CAPABLE correction: a revision-checked human correction to a
    field becomes the confirmed observed value in campaign comparison,
    not discarded for differing from the machine proposal. Before the
    fix the customer row read `Message | bright | wrong proposal |
    Unknown`; after it reads `Message | bright | bright | Supported`
    with human provenance and current pins."""

    def setUp(self):
        self.con = fresh_db()
        seed_full_episode(self.con, ep_id="ep-full")
        self.cid = seed_campaign(self.con)
        add_candidate(self.con, "x", episode_id="ep-full",
                      attrs={"read_type": "host_read",
                             "message": "wrong proposal",
                             "offer": "WIDGET10", "code": "WIDGET10"})
        made = verify_full_ad(self.con, "x", episode_id="ep-full")
        self.obs_id = made["observation_id"]
        self.pid = add_placement(self.con, self.cid, episode_id="ep-full",
                                 requirements={"duration_s": [5, 30],
                                               "message": "bright",
                                               "offer": "WIDGET10"})
        rev = _rev(self.con, self.cid)
        campaigns.save_placement_evidence(
            self.con, self.cid, self.pid, _ref(self.con, "x"), rev)

    def tearDown(self):
        self.con.close()

    def _rows(self):
        _repin_current(self)
        draft = campaigns.build_customer_draft(self.con, self.cid)
        return {r["field"]: r for r in draft["comparisons"].get(self.pid, [])}

    def _confirm(self, field, value, evidence, actor="owner"):
        rev = self.con.execute(
            "SELECT revision FROM ad_observations WHERE id=?",
            (self.obs_id,)).fetchone()[0]
        return observations.confirm_observation_field(
            self.con, self.obs_id, field, value, rev,
            evidence=evidence, actor=actor)

    def test_correction_becomes_confirmed_observed(self):
        self._confirm("message", "bright", {"segment_ref": 0})
        self.con.commit()
        rows = self._rows()
        self.assertEqual(rows["message"]["observed"], "bright")
        self.assertEqual(rows["message"]["state"], "supported")
        conf = rows["message"]["confirmation"]
        self.assertEqual(conf["actor"], "owner")
        self.assertIsNotNone(conf["observation_revision"])
        self.assertIsNotNone(conf["source_version_id"])
        # Unconfirmed fields keep the proposal and stay unknown.
        self.assertEqual(rows["offer"]["observed"], "WIDGET10")
        self.assertEqual(rows["offer"]["state"], "unknown")
        self.assertIsNone(rows["offer"]["confirmation"])

    def test_confirmed_conflicting_value_mismatches(self):
        self._confirm("message", "other copy", {"segment_ref": 0})
        self.con.commit()
        rows = self._rows()
        self.assertEqual(rows["message"]["observed"], "other copy")
        self.assertEqual(rows["message"]["state"], "mismatch")

    def test_clear_ends_correction_authority(self):
        out = self._confirm("message", "bright", {"segment_ref": 0})
        self.con.commit()
        self.assertEqual(self._rows()["message"]["state"], "supported")
        observations.clear_observation_field(
            self.con, self.obs_id, "message", out["revision"], actor="owner")
        self.con.commit()
        rows = self._rows()
        self.assertEqual(rows["message"]["observed"], "wrong proposal")
        self.assertEqual(rows["message"]["state"], "unknown")
        self.assertIsNone(rows["message"]["confirmation"])

    def test_machine_actor_row_has_no_authority(self):
        observations.ensure_confirmation_schema(self.con)
        pin = self.con.execute(
            "SELECT revision, source_version_id, transcript_version_id"
            " FROM ad_observations WHERE id=?", (self.obs_id,)).fetchone()
        self.con.execute(
            "INSERT INTO field_confirmations (workspace_id, observation_id,"
            " field, value, cleared, observation_revision, source_version_id,"
            " transcript_version_id, segment_ref, char_start, char_end,"
            " audio_ref, actor, created_at) VALUES ('local', ?, 'message',"
            " 'bright', 0, ?, ?, ?, 0, NULL, NULL, '', 'pipeline', ?)",
            (self.obs_id, pin[0], pin[1], pin[2], db.utcnow()))
        # Any later producer publication of the evidence captures the row;
        # without one the attachment pin predates it and the check is vacuous.
        citations.publish_source_state(self.con, kind="evidence",
                                       record_id="x", actor="fixture")
        self.con.commit()
        rows = self._rows()
        self.assertEqual(rows["message"]["observed"], "wrong proposal")
        self.assertEqual(rows["message"]["state"], "unknown")

    def test_audio_citation_needs_current_episode_audio(self):
        self.con.execute("UPDATE episodes SET audio_url='https://x.example/a.mp3',"
                         " media_id=NULL WHERE id='ep-full'")
        self.con.commit()
        self._confirm("message", "bright",
                      {"audio_ref": "https://x.example/a.mp3"})
        self.con.commit()
        self.assertEqual(self._rows()["message"]["state"], "supported")
        self.con.execute("UPDATE episodes SET audio_url=NULL, media_id=NULL"
                         " WHERE id='ep-full'")
        self.con.commit()
        rows = self._rows()
        self.assertEqual(rows["message"]["state"], "unknown")
        self.assertIsNone(rows["message"]["confirmation"])

    def test_audio_ref_must_match_exact_current_asset(self):
        """Req 3: audio evidence must name the exact current episode
        media asset. Before: any non-empty audio_ref granted authority
        while the episode merely had some audio. After: a wrong
        locator is refused at write; a media-ID locator is accepted;
        rotating the asset ends authority without rewriting history."""
        self.con.execute("UPDATE episodes SET audio_url='https://x.example/a.mp3',"
                         " media_id=NULL WHERE id='ep-full'")
        self.con.commit()
        with self.assertRaises(ops.BadInput):
            self._confirm("message", "bright",
                          {"audio_ref": "not-the-current-audio"})
        self.con.commit()
        rows = self._rows()
        self.assertEqual(rows["message"]["state"], "unknown")
        self.assertIsNone(rows["message"]["confirmation"])
        self.con.execute("UPDATE episodes SET audio_url=NULL,"
                         " media_id='media-1' WHERE id='ep-full'")
        self.con.commit()
        self._confirm("message", "bright", {"audio_ref": "media-1"})
        self.con.commit()
        self.assertEqual(self._rows()["message"]["state"], "supported")
        self.con.execute("UPDATE episodes SET audio_url='https://x.example/b.mp3',"
                         " media_id='media-2' WHERE id='ep-full'")
        self.con.commit()
        rows = self._rows()
        self.assertEqual(rows["message"]["state"], "unknown")
        self.assertIsNone(rows["message"]["confirmation"])


if __name__ == "__main__":
    unittest.main()
