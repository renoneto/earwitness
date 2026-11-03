"""P01 job-ledger regressions (stdlib unittest, disposable state only).

Every test fails on a plausible ledger bug: stale-worker publication,
duplicate logical operations, auto-rebill after ambiguity, stranded
empty downstreams, screening failures blocking transcripts, or
reconciliation bypass. No network, no models, no owner state.
"""
from __future__ import annotations

import json
import sqlite3
import unittest

from earwitness.app import db
from earwitness.pipeline import jobs


def fresh():
    # Configured like production (explicit autocommit): transaction() then
    # really issues BEGIN IMMEDIATE/COMMIT instead of nesting savepoints
    # inside an implicit transaction the test never commits.
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON;")
    db.init_schema(con)
    jobs.ensure_tables(con)
    return con


def one_stage(con, **kw):
    args = {"stages": [{"stage": "noop", "input_digest": "in-1"}],
            "config_digest": "cfg-1", "initiator": "test",
            "idempotency_key": "k-1"}
    args.update(kw)
    return jobs.submit_run(con, **args)


def seed_admitted_op(con, job_id, op_id, cost, provider_key, now_s="2026-10-20T00:00:00+00:00"):
    """Insert an `admitted` paid operation directly (L4p).

    `reserve_paid` and `admit_dispatch` now refuse (paid processing is off in
    the pilot), so the post-admission ledger logic (ambiguity, reconciliation)
    keeps its coverage from a row shaped exactly like the ones they wrote.
    """
    reservation = json.dumps({"worst_case_cost": float(cost), "reserved_at": now_s})
    con.execute("INSERT INTO paid_operations (id, workspace_id, job_id, reservation,"
                " provider_key, disposition, receipt, revision, updated_at)"
                " VALUES (?, 'local', ?, ?, ?, 'admitted', '{}', 2, ?)",
                (op_id, job_id, reservation, provider_key, now_s))
    return dict(con.execute("SELECT * FROM paid_operations WHERE id=?",
                            (op_id,)).fetchone())


class LeaseFencingTest(unittest.TestCase):
    def test_expired_worker_publication_rejected(self):
        con = fresh()
        run = one_stage(con)
        job = jobs.claim_next(con, worker_id="w1", now=1000.0)
        self.assertIsNotNone(job)
        token = job["fencing_token"]
        # Heartbeat after lease expiry reports the claim as dead.
        self.assertFalse(jobs.heartbeat(con, job["id"], token, now=1000.0 + 121.0))
        # And the stale worker cannot publish: fencing rejects it.
        with self.assertRaises(jobs.LeaseExpired):
            jobs.publish_success(con, job["id"], token, "digest-x",
                                 now=1000.0 + 121.0)
        # The job is not silently completed.
        state = con.execute("SELECT state FROM processing_jobs WHERE id=?",
                            (job["id"],)).fetchone()["state"]
        self.assertEqual(state, "running")
        # Recovery reclaims the claim without inventing work ...
        reclaimed = jobs.expire_leases(con, now=1000.0 + 200.0)
        self.assertEqual(reclaimed, [job["id"]])
        # ... and a fresh fenced claim can complete it exactly once.
        job2 = jobs.claim_next(con, worker_id="w2", now=1200.0)
        self.assertEqual(job2["id"], job["id"])
        self.assertNotEqual(job2["fencing_token"], token)
        jobs.publish_success(con, job2["id"], job2["fencing_token"],
                             "digest-x", now=1200.0)
        with self.assertRaises(jobs.BlockedError):
            jobs.publish_success(con, job2["id"], job2["fencing_token"],
                                 "digest-x", now=1200.0)

    def test_wrong_token_cannot_publish(self):
        con = fresh()
        one_stage(con)
        job = jobs.claim_next(con, worker_id="w1", now=1000.0)
        with self.assertRaises(jobs.FencingMismatch):
            jobs.publish_success(con, job["id"], "w9:impostor", "digest-x",
                                 now=1001.0)


class IdempotencyTest(unittest.TestCase):
    def test_concurrent_same_key_submissions_one_logical_op(self):
        con = fresh()
        first = one_stage(con, idempotency_key="same-key")
        second = one_stage(con, idempotency_key="same-key")
        self.assertEqual(first["run_id"], second["run_id"])
        count = con.execute("SELECT COUNT(*) FROM processing_runs").fetchone()[0]
        self.assertEqual(count, 1)

    def test_same_key_divergent_body_conflicts(self):
        con = fresh()
        one_stage(con, idempotency_key="same-key")
        with self.assertRaises(jobs.ConflictError):
            jobs.submit_run(con, stages=[{"stage": "noop", "input_digest": "other"}],
                            config_digest="cfg-1", initiator="test",
                            idempotency_key="same-key")


class PaidDispatchTest(unittest.TestCase):
    def _ambiguous(self, con):
        run = one_stage(con, idempotency_key="paid-run")
        job = jobs.claim_next(con, worker_id="w1", now=1000.0)
        op = seed_admitted_op(con, job["id"], "op-1", 2.50, "prov-key-1")
        jobs.mark_ambiguous(con, op["id"], "worker died after admitted send",
                            now=1002.0)
        return run, job, op

    @unittest.skip("deferred until L4: paid processing unavailable in pilot")
    def test_reserve_admit_then_ambiguous_full_flow(self):
        # The original flow through the real entrypoints. It returns with the
        # full L4 budget authority (docs/plans/2026-10-20-budget-authority.md).
        con = fresh()
        one_stage(con, idempotency_key="paid-run")
        job = jobs.claim_next(con, worker_id="w1", now=1000.0)
        op = jobs.reserve_paid(con, job_id=job["id"], worst_case_cost=2.50,
                               provider_key="prov-key-1", idempotency_key="op-1")
        jobs.admit_dispatch(con, op["id"], now=1001.0)
        jobs.mark_ambiguous(con, op["id"], "worker died after admitted send",
                            now=1002.0)
        self.assertEqual(jobs.get_paid_operation(con, op["id"])["disposition"],
                         "unknown")

    def test_ambiguous_dispatch_needs_reconciliation_never_auto_rebill(self):
        con = fresh()
        run, job, op = self._ambiguous(con)
        state = con.execute("SELECT state FROM processing_jobs WHERE id=?",
                            (job["id"],)).fetchone()["state"]
        self.assertEqual(state, "reconciliation_required")
        summary = jobs.run_summary(con, run["run_id"])
        self.assertEqual(summary["financial_status"], "reconciliation_required")
        # Ordinary retry cannot select it: no blind resend.
        with self.assertRaises(jobs.BlockedError):
            jobs.request_retry(con, run["run_id"], [job["id"]],
                               idempotency_key="retry-1")
        # Resume cannot bypass reconciliation either.
        with self.assertRaises(jobs.BlockedError):
            jobs.resume(con, run["run_id"], [job["id"]],
                        expected_revision=summary["revision"])
        # Lease expiry cannot release or replace the reservation.
        jobs.expire_leases(con, now=2000.0)
        op2 = jobs.get_paid_operation(con, op["id"])
        self.assertEqual(op2["disposition"], "unknown")

    def test_reconcile_requires_owner_and_operation_linked_evidence(self):
        con = fresh()
        run, job, op = self._ambiguous(con)
        rev = jobs.get_paid_operation(con, op["id"])["revision"]
        with self.assertRaises(jobs.AuthorizationError):
            jobs.reconcile(con, op["id"], {"disposition": "still_unknown"},
                           rev, "rec-1", "editor")
        with self.assertRaises(jobs.JobError):
            jobs.reconcile(con, op["id"],
                           {"disposition": "confirmed_nonacceptance",
                            "receipt": {"operation_id": op["id"]},  # not definitive
                            "rationale": "looks quiet"},
                           rev, "rec-2", "owner")
        with self.assertRaises(jobs.JobError):
            jobs.reconcile(con, op["id"],
                           {"disposition": "confirmed_nonacceptance",
                            "receipt": {"operation_id": "some-other-op",
                                        "definitive": True},
                            "rationale": "guess"},
                           rev, "rec-3", "owner")
        settled = jobs.reconcile(
            con, op["id"],
            {"disposition": "confirmed_nonacceptance",
             "receipt": {"operation_id": op["id"], "definitive": True,
                         "provider": "no accepted work"},
             "rationale": "provider lookup confirms no acceptance"},
            rev, "rec-4", "owner")
        self.assertEqual(settled["disposition"], "released")
        # Idempotent replay returns the same settled operation.
        again = jobs.reconcile(
            con, op["id"],
            {"disposition": "confirmed_nonacceptance",
             "receipt": {"operation_id": op["id"], "definitive": True},
             "rationale": "replay"},
            settled["revision"], "rec-4", "owner")
        self.assertEqual(again["disposition"], "released")
        # And the failed work is now eligible for an explicit authorized retry.
        summary = jobs.request_retry(con, run["run_id"], [job["id"]],
                                     idempotency_key="retry-after-rec")
        self.assertIn(summary["status"], ("queued", "running", "blocked"))


class DependencyReductionTest(unittest.TestCase):
    def test_empty_candidate_set_succeeds_downstream(self):
        con = fresh()
        run = jobs.submit_run(
            con, stages=[{"job_id": "screen", "stage": "screen", "input_digest": "m"},
                         {"job_id": "publish", "stage": "noop", "input_digest": "m",
                          "parents": ["screen"]}],
            config_digest="c", initiator="t", idempotency_key="empty-1")
        screen = jobs.claim_next(con, worker_id="w", now=1000.0)
        self.assertEqual(screen["id"], "screen")
        jobs.publish_success(con, screen["id"], screen["fencing_token"], "",
                             empty_ok=True, now=1001.0)
        nxt = jobs.claim_next(con, worker_id="w", now=1002.0)
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt["id"], "publish")
        jobs.publish_success(con, nxt["id"], nxt["fencing_token"], "d", now=1003.0)
        self.assertEqual(jobs.run_summary(con, run["run_id"])["status"], "succeeded")

    def test_failed_screening_does_not_block_transcript_publication(self):
        con = fresh()
        run = jobs.submit_run(
            con, stages=[{"job_id": "fetch", "stage": "fetch", "input_digest": "m"},
                         {"job_id": "screen", "stage": "screen", "input_digest": "m",
                          "parents": ["fetch"]},
                         {"job_id": "transcript", "stage": "transcribe",
                          "input_digest": "m", "parents": ["fetch"],
                          "optional_parents": ["screen"]}],
            config_digest="c", initiator="t", idempotency_key="opt-1")
        fetch = jobs.claim_next(con, worker_id="w", now=1000.0)
        jobs.publish_success(con, fetch["id"], fetch["fencing_token"], "m", now=1001.0)
        screen = jobs.claim_next(con, worker_id="w", now=1002.0)
        self.assertEqual(screen["id"], "screen")
        jobs.publish_failure(con, screen["id"], screen["fencing_token"],
                             failure_class="SCREEN_FAILED", error="boom", now=1003.0)
        # The transcript child is still runnable despite screening failure.
        nxt = jobs.claim_next(con, worker_id="w", now=1004.0)
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt["id"], "transcript")
        jobs.publish_success(con, nxt["id"], nxt["fencing_token"], "t", now=1005.0)
        summary = jobs.run_summary(con, run["run_id"])
        self.assertEqual(summary["status"], "partial")

    def test_failed_required_parent_closes_dependents_retry_reopens_chain(self):
        con = fresh()
        run = jobs.submit_run(
            con, stages=[{"job_id": "a", "stage": "noop", "input_digest": "m"},
                         {"job_id": "b", "stage": "noop", "input_digest": "m",
                          "parents": ["a"]}],
            config_digest="c", initiator="t", idempotency_key="chain-1")
        job_a = jobs.claim_next(con, worker_id="w", now=1000.0)
        jobs.publish_failure(con, job_a["id"], job_a["fencing_token"],
                             failure_class="DECODE", error="x", now=1001.0)
        state_b = con.execute("SELECT state, failure_class FROM processing_jobs"
                              " WHERE id='b'").fetchone()
        self.assertEqual(state_b["state"], "failed")
        self.assertEqual(state_b["failure_class"], "DEPENDENCY_FAILED")
        summary = jobs.request_retry(con, run["run_id"], ["a"],
                                     idempotency_key="retry-chain")
        self.assertIn(summary["status"], ("queued", "running"))
        states = {r["id"]: r["state"] for r in
                  con.execute("SELECT id, state FROM processing_jobs").fetchall()}
        self.assertEqual(states["a"], "queued")
        self.assertEqual(states["b"], "queued")


class CancelTest(unittest.TestCase):
    def test_cancel_during_local_decode_stops_new_stages(self):
        con = fresh()
        run = jobs.submit_run(
            con, stages=[{"job_id": "decode", "stage": "transcribe", "input_digest": "m"},
                         {"job_id": "later", "stage": "noop", "input_digest": "m",
                          "parents": ["decode"]}],
            config_digest="c", initiator="t", idempotency_key="cancel-1")
        worker = jobs.claim_next(con, worker_id="w", now=1000.0)
        rev = jobs.run_summary(con, run["run_id"])["revision"]
        cancelled = jobs.cancel(con, run["run_id"], rev, now=1001.0)
        self.assertEqual(cancelled["status"], "cancel_requested")
        # Cancellation fences publication: the running worker cannot commit.
        with self.assertRaises(jobs.CancelledError):
            jobs.publish_success(con, worker["id"], worker["fencing_token"],
                                 "late-bytes", now=1002.0)
        later = con.execute("SELECT state FROM processing_jobs WHERE id='later'").fetchone()
        self.assertEqual(later["state"], "cancelled")
        # Once owned local execution stops (lease reclaimed), settle to cancelled.
        jobs.expire_leases(con, now=2000.0)
        rev2 = jobs.run_summary(con, run["run_id"])["revision"]
        settled = jobs.cancel(con, run["run_id"], rev2, now=2001.0)
        self.assertEqual(settled["status"], "cancelled")
        # A cancelled run is not silently resumed; retry is refused.
        with self.assertRaises(jobs.BlockedError):
            jobs.request_retry(con, run["run_id"], ["decode"],
                               idempotency_key="retry-cancelled")


class CancelStickinessTest(unittest.TestCase):
    def test_cancelled_run_never_resurrects(self):
        con = fresh()
        run = one_stage(con, idempotency_key="sticky-1")
        job = jobs.claim_next(con, worker_id="w1", now=1000.0)
        rev = jobs.run_summary(con, run["run_id"])["revision"]
        jobs.cancel(con, run["run_id"], rev, now=1001.0)
        jobs.expire_leases(con, now=2000.0)
        self.assertEqual(jobs.run_summary(con, run["run_id"])["status"],
                         "cancelled")
        # No claim escapes a cancelled run, even after lease-expiry churn.
        self.assertIsNone(jobs.claim_next(con, worker_id="w2", now=2001.0))


class ParentGateTest(unittest.TestCase):
    def test_resume_retry_publish_require_succeeded_parents(self):
        con = fresh()
        run = jobs.submit_run(
            con, stages=[{"job_id": "a", "stage": "noop", "input_digest": "m"},
                         {"job_id": "b", "stage": "noop", "input_digest": "m",
                          "parents": ["a"]}],
            config_digest="c", initiator="t", idempotency_key="parents-1")
        rev = jobs.run_summary(con, run["run_id"])["revision"]
        with self.assertRaises(jobs.BlockedError):
            jobs.resume(con, run["run_id"], ["b"], rev)
        # Retry of the child alone is refused while its parent never succeeded.
        ja = jobs.claim_next(con, worker_id="w", now=1000.0)
        jobs.publish_failure(con, ja["id"], ja["fencing_token"],
                             failure_class="DECODE", error="x", now=1001.0)
        with self.assertRaises(jobs.BlockedError):
            jobs.request_retry(con, run["run_id"], ["b"],
                               idempotency_key="rt-b-alone")
        # Out-of-order publish of an illicitly queued child is refused.
        con.execute("UPDATE processing_jobs SET state='queued' WHERE id='b'")
        con.commit()
        jb = jobs.claim_next(con, worker_id="w2", now=1002.0)
        self.assertEqual(jb["id"], "b")
        with self.assertRaises(jobs.BlockedError):
            jobs.publish_success(con, jb["id"], jb["fencing_token"],
                                 "d", now=1003.0)


class StageKeyReuseTest(unittest.TestCase):
    def test_same_input_different_backend_does_not_reuse(self):
        con = fresh()
        jobs.submit_run(
            con, stages=[{"job_id": "k1", "stage": "transcribe",
                          "input_digest": "same", "backend_profile": "local"}],
            config_digest="c", initiator="t", idempotency_key="reuse-1")
        job = jobs.claim_next(con, worker_id="w", now=1000.0)
        jobs.publish_success(con, job["id"], job["fencing_token"],
                             "out", now=1001.0)
        jobs.submit_run(
            con, stages=[{"job_id": "k2", "stage": "transcribe",
                          "input_digest": "same",
                          "backend_profile": "gpu-a100"}],
            config_digest="c", initiator="t", idempotency_key="reuse-2")
        state = con.execute(
            "SELECT state FROM processing_jobs WHERE id='k2'").fetchone()["state"]
        self.assertEqual(state, "queued")
        # Identical full stage key still reuses without re-running the stage.
        jobs.submit_run(
            con, stages=[{"job_id": "k3", "stage": "transcribe",
                          "input_digest": "same", "backend_profile": "local"}],
            config_digest="c", initiator="t", idempotency_key="reuse-3")
        state3 = con.execute(
            "SELECT state FROM processing_jobs WHERE id='k3'").fetchone()["state"]
        self.assertEqual(state3, "succeeded")


class AmbiguityFencingTest(unittest.TestCase):
    def _live_admitted(self, con, key, job_id, op_id, now):
        jobs.submit_run(
            con, stages=[{"job_id": job_id, "stage": "noop",
                          "input_digest": "m"}],
            config_digest="c", initiator="t", idempotency_key=key)
        job = jobs.claim_next(con, worker_id="w1", now=now)
        op = seed_admitted_op(con, job["id"], op_id, 1.0, "pk-" + key)
        return job, op

    def test_mark_ambiguous_requires_fencing_revision_and_live_state(self):
        con = fresh()
        job, op = self._live_admitted(con, "fence-1", "g1", "op-g1", 1000.0)
        with self.assertRaises(jobs.FencingMismatch):
            jobs.mark_ambiguous(con, op["id"], "x", fencing_token="impostor",
                                now=1002.0)
        rev = jobs.get_paid_operation(con, op["id"])["revision"]
        with self.assertRaises(jobs.ConflictError):
            jobs.mark_ambiguous(con, op["id"], "x",
                                fencing_token=job["fencing_token"],
                                expected_revision=rev + 9, now=1002.0)
        # Live token + current revision on an admitted op: designed path works.
        jobs.mark_ambiguous(con, op["id"], "worker died after admitted send",
                            fencing_token=job["fencing_token"],
                            expected_revision=rev, now=1002.0)
        state = con.execute(
            "SELECT state FROM processing_jobs WHERE id='g1'").fetchone()["state"]
        self.assertEqual(state, "reconciliation_required")

    def test_mark_ambiguous_never_regresses_confirmed_success(self):
        con = fresh()
        job0, op0 = self._live_admitted(con, "fence-2", "g2", "op-g2", 2000.0)
        run_id, job_id, op_id = job0["run_id"], "g2", "op-g2"
        jobs.publish_success(con, job_id, job0["fencing_token"],
                             "out", now=2002.0)
        rev = jobs.get_paid_operation(con, op_id)["revision"]
        jobs.reconcile(con, op_id,
                       {"disposition": "confirmed_success",
                        "receipt": {"operation_id": op_id, "definitive": True},
                        "rationale": "provider confirms acceptance"},
                       rev, "rec-g2", "owner")
        jobs.request_retry(con, run_id, [job_id], idempotency_key="rt-g2")
        job2 = jobs.claim_next(con, worker_id="w2", now=2003.0)
        jobs.publish_success(con, job2["id"], job2["fencing_token"],
                             "out2", now=2004.0)
        with self.assertRaises(jobs.BlockedError):
            jobs.mark_ambiguous(con, op_id, "late duplicate report", now=2005.0)
        state = con.execute(
            "SELECT state FROM processing_jobs WHERE id='g2'").fetchone()["state"]
        self.assertEqual(state, "succeeded")


class ContendedClaimTest(unittest.TestCase):
    def test_claim_loser_gets_clean_none_not_exception(self):
        # F1: the dead total_changes guard is gone; under contention the
        # loser still gets a clean None, never an exception.
        con = fresh()
        one_stage(con)
        winner = jobs.claim_next(con, worker_id="w1", now=1000.0)
        self.assertIsNotNone(winner)
        try:
            loser = jobs.claim_next(con, worker_id="w2", now=1001.0)
        except Exception as exc:  # noqa: BLE001 - must prove no raise
            self.fail(f"contended claim raised {type(exc).__name__}: {exc}")
        self.assertIsNone(loser)


class WorkerRunFilterTest(unittest.TestCase):
    def test_run_filter_continues_past_foreign_jobs(self):
        # F5: --run B with run-A jobs queued first must still process B;
        # foreign claims are held out of the way and released back queued.
        import importlib.util
        import shutil
        import sys
        import tempfile
        import types
        from pathlib import Path
        repo = Path(__file__).resolve().parent.parent
        mod_name = "ew_app_worker_regression"
        if mod_name in sys.modules:
            aw = sys.modules[mod_name]
        else:
            spec = importlib.util.spec_from_file_location(
                mod_name, repo / "scripts" / "app_worker.py")
            aw = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = aw
            spec.loader.exec_module(aw)
        tmp = tempfile.mkdtemp(prefix="ew-f5-")
        self.addCleanup(shutil.rmtree, tmp, True)
        ws = Path(tmp) / "ws"
        ws.mkdir()
        orig_resolve = aw._ws.resolve_workspace
        aw._ws.resolve_workspace = lambda value: ws
        self.addCleanup(setattr, aw._ws, "resolve_workspace", orig_resolve)
        con = aw._db.connect(ws)
        aw._db.init_schema(con)
        aw._jobs.ensure_tables(con)
        aw._artifacts.ensure_tables(con)
        aw._jobs.submit_run(
            con, stages=[{"job_id": "fa0", "stage": "noop",
                          "input_digest": "a1"},
                         {"job_id": "fa1", "stage": "noop",
                          "input_digest": "a2"}],
            config_digest="cfg", initiator="t", idempotency_key="k-f5-A")
        run_b = aw._jobs.submit_run(
            con, stages=[{"job_id": "fb0", "stage": "noop",
                          "input_digest": "b1"}],
            config_digest="cfg", initiator="t", idempotency_key="k-f5-B")
        con.commit()
        con.close()
        args = types.SimpleNamespace(
            workspace="ignored", workspace_id="local", profile="any",
            handler="noop", max_jobs=10, run=run_b["run_id"])
        rc = aw.cmd_work(args)
        self.assertEqual(rc, 0)
        con = aw._db.connect(ws)
        b_state = con.execute(
            "SELECT state FROM processing_jobs WHERE id='fb0'").fetchone()["state"]
        a_states = [r[0] for r in con.execute(
            "SELECT state FROM processing_jobs WHERE id IN ('fa0','fa1')"
            " ORDER BY id").fetchall()]
        con.close()
        self.assertEqual(b_state, "succeeded")
        self.assertEqual(a_states, ["queued", "queued"])

    def test_run_filter_release_rederives_foreign_run_status(self):
        # R3/M3: --run B hold-and-release must leave foreign run A queued,
        # not stale running with all jobs queued.
        import importlib.util
        import shutil
        import sys
        import tempfile
        import types
        from pathlib import Path
        repo = Path(__file__).resolve().parent.parent
        mod_name = "ew_app_worker_regression"
        if mod_name in sys.modules:
            aw = sys.modules[mod_name]
        else:
            spec = importlib.util.spec_from_file_location(
                mod_name, repo / "scripts" / "app_worker.py")
            aw = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = aw
            spec.loader.exec_module(aw)
        tmp = tempfile.mkdtemp(prefix="ew-r3m3-")
        self.addCleanup(shutil.rmtree, tmp, True)
        ws = Path(tmp) / "ws"
        ws.mkdir()
        orig_resolve = aw._ws.resolve_workspace
        aw._ws.resolve_workspace = lambda value: ws
        self.addCleanup(setattr, aw._ws, "resolve_workspace", orig_resolve)
        con = aw._db.connect(ws)
        aw._db.init_schema(con)
        aw._jobs.ensure_tables(con)
        aw._artifacts.ensure_tables(con)
        run_a = aw._jobs.submit_run(
            con, stages=[{"job_id": "fa0", "stage": "noop",
                          "input_digest": "a1"}],
            config_digest="cfg", initiator="t", idempotency_key="k-r3m3-A")
        run_b = aw._jobs.submit_run(
            con, stages=[{"job_id": "fb0", "stage": "noop",
                          "input_digest": "b1"}],
            config_digest="cfg", initiator="t", idempotency_key="k-r3m3-B")
        con.commit()
        con.close()
        args = types.SimpleNamespace(
            workspace="ignored", workspace_id="local", profile="any",
            handler="noop", max_jobs=10, run=run_b["run_id"])
        rc = aw.cmd_work(args)
        self.assertEqual(rc, 0)
        con = aw._db.connect(ws)
        a_status = con.execute(
            "SELECT status FROM processing_runs WHERE id=?",
            (run_a["run_id"],)).fetchone()["status"]
        b_job = con.execute(
            "SELECT state FROM processing_jobs WHERE id='fb0'").fetchone()["state"]
        con.close()
        self.assertEqual(b_job, "succeeded")
        self.assertEqual(a_status, "queued")


if __name__ == "__main__":
    unittest.main()
