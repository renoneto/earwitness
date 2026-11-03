"""Job-ledger and worker crash-safety regressions (stdlib unittest).

Each test reproduces a confirmed way a crashed, slow, contended or
filtered worker strands or duplicates work: unreclaimed leases, missing
heartbeats, poison-job loops, --run hold-and-release races, lock errors
escaping the worker, profile head-of-line misses, a crash between
publish and reduction, and default job-id collisions. Disposable state
only: in-memory ledgers and throwaway temp workspaces.
"""
from __future__ import annotations

import importlib.util
import shutil
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

from earwitness.app import db
from earwitness.pipeline import jobs

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))


def _load_worker():
    name = "ew_app_worker_crash_safety"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, REPO / "scripts" / "app_worker.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


aw = _load_worker()


def fresh():
    # Production-shaped: explicit autocommit, so transaction() really
    # issues BEGIN IMMEDIATE/COMMIT instead of nesting savepoints.
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON;")
    db.init_schema(con)
    jobs.ensure_tables(con)
    return con


def state(con, job_id):
    return con.execute("SELECT state FROM processing_jobs WHERE id=?",
                       (job_id,)).fetchone()["state"]


class WorkerCase(unittest.TestCase):
    """Throwaway workspace wired into the worker's resolve_workspace."""

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="ew-crash-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.ws = Path(tmp) / "ws"
        self.ws.mkdir()
        orig = aw._ws.resolve_workspace
        aw._ws.resolve_workspace = lambda value: self.ws
        self.addCleanup(setattr, aw._ws, "resolve_workspace", orig)
        con = self.connect()
        db.init_schema(con)
        jobs.ensure_tables(con)
        con.close()

    def connect(self):
        return db.connect(self.ws)

    def patch(self, obj, name, value):
        orig = getattr(obj, name, None)
        setattr(obj, name, value)
        self.addCleanup(setattr, obj, name, orig)

    def submit(self, key, ids, stage="noop"):
        con = self.connect()
        try:
            return jobs.submit_run(
                con, stages=[{"job_id": j, "stage": stage, "input_digest": j}
                             for j in ids], idempotency_key=key)
        finally:
            con.close()

    def work(self, **over):
        args = {"workspace": "ignored", "workspace_id": "local",
                "profile": "any", "handler": "noop", "run": "",
                "max_jobs": 10}
        args.update(over)
        return aw.cmd_work(types.SimpleNamespace(**args))

    def states(self):
        con = self.connect()
        try:
            return dict(con.execute("SELECT id, state FROM processing_jobs"
                                    " ORDER BY rowid").fetchall())
        finally:
            con.close()


class LeaseReclaimTest(WorkerCase):
    def test_worker_reclaims_job_of_killed_worker(self):
        # Bug 1: a worker killed mid-job left the job running forever.
        self.submit("k-dead", ["dead"])
        con = self.connect()
        claimed = jobs.claim_next(con, worker_id="killed", now=time.time() - 500)
        con.close()
        self.assertEqual(claimed["id"], "dead")
        self.assertEqual(self.work(), 0)
        self.assertEqual(self.states()["dead"], "succeeded")


class HeartbeatTest(WorkerCase):
    def _slow(self, seconds):
        def handler(job, args):
            time.sleep(seconds)
            return {"output_digest": f"slow:{job['id']}"}
        self.patch(aw, "HANDLERS", {**aw.HANDLERS, "slow": handler})

    def test_handler_longer_than_lease_still_publishes(self):
        # Bug 2: no heartbeat, so any handler longer than the lease lost it.
        self.patch(jobs, "DEFAULT_LEASE_S", 1.0)
        self.patch(jobs, "DEFAULT_HEARTBEAT_S", 0.2)
        self._slow(2.0)
        self.submit("k-slow", ["slow"])
        self.assertEqual(self.work(handler="slow"), 0)
        self.assertEqual(self.states()["slow"], "succeeded")

    def test_hung_handler_loses_lease_after_ceiling(self):
        # Bug 2 ceiling: past --max-job-seconds the heartbeat stops renewing,
        # so a hung handler cannot hold its lease forever.
        self.patch(jobs, "DEFAULT_LEASE_S", 1.0)
        self.patch(jobs, "DEFAULT_HEARTBEAT_S", 0.2)
        self._slow(2.5)
        self.submit("k-hung", ["hung"])
        self.assertEqual(self.work(handler="slow", max_jobs=1,
                                   max_job_seconds=0.5), 1)
        self.assertEqual(self.states()["hung"], "running")
        con = self.connect()
        try:
            self.assertEqual(jobs.expire_leases(con), ["hung"])
        finally:
            con.close()


class PoisonJobTest(unittest.TestCase):
    def _crash(self, con, t, job_id="poison"):
        job = jobs.claim_next(con, worker_id="w", now=t)
        self.assertEqual(job["id"], job_id)
        jobs.expire_leases(con, now=t + 121)
        return t + 121

    def _submit(self, con, job_id):
        jobs.submit_run(con, stages=[{"job_id": job_id, "stage": "noop",
                                      "input_digest": job_id}],
                        idempotency_key=job_id)

    def test_crash_looping_job_dead_letters(self):
        # Bug 3: a job that kills its worker every time was requeued
        # first, forever, starving every job behind it.
        con = fresh()
        self._submit(con, "poison")
        self._submit(con, "good")
        t = 1000.0
        for _ in range(jobs.DEFAULT_MAX_ATTEMPTS):
            t = self._crash(con, t)
        row = con.execute("SELECT state, failure_class FROM processing_jobs"
                          " WHERE id='poison'").fetchone()
        self.assertEqual((row["state"], row["failure_class"]),
                         ("failed", "MAX_ATTEMPTS_EXCEEDED"))
        last = con.execute("SELECT error FROM job_attempts WHERE job_id='poison'"
                           " ORDER BY attempt_no DESC LIMIT 1").fetchone()["error"]
        self.assertIn("max_attempts_exceeded", last)
        self.assertEqual(jobs.claim_next(con, worker_id="w", now=t)["id"], "good")

    def test_once_expired_job_not_starved_by_inflow(self):
        # Review: ordering by crash count first starved a once-expired job
        # behind every newer job under steady inflow.
        con = fresh()
        self._submit(con, "x")
        t = self._crash(con, 1000.0, "x")
        claimed = []
        for i in range(20):
            self._submit(con, f"new{i}")
            job = jobs.claim_next(con, worker_id="w", now=t)
            claimed.append(job["id"])
            jobs.publish_success(con, job["id"], job["fencing_token"], "d", now=t)
        self.assertIn("x", claimed)

    def test_legacy_expired_rows_do_not_count(self):
        # Review: 'expired' attempts written by the old --run hold-and-release
        # were not crashes, yet counted toward dead-lettering.
        con = fresh()
        self._submit(con, "legacy")
        for n in range(jobs.DEFAULT_MAX_ATTEMPTS):
            con.execute("INSERT INTO job_attempts (workspace_id, job_id,"
                        " attempt_no, state, heartbeat_at, created_at)"
                        " VALUES ('local', 'legacy', ?, 'expired', '0', 't')", (n,))
        self._crash(con, 1000.0, "legacy")
        self.assertEqual(state(con, "legacy"), "queued")

    def test_operator_retry_resets_crash_count(self):
        # Review: the expiry count survived request_retry, so the first
        # expiry after an operator retry dead-lettered again immediately.
        con = fresh()
        self._submit(con, "poison")
        t = 1000.0
        for _ in range(jobs.DEFAULT_MAX_ATTEMPTS):
            t = self._crash(con, t)
        run_id = con.execute("SELECT run_id FROM processing_jobs"
                             " WHERE id='poison'").fetchone()[0]
        jobs.request_retry(con, run_id, ["poison"], "retry-1")
        t = self._crash(con, t)
        self.assertEqual(state(con, "poison"), "queued")


class RunFilterTest(WorkerCase):
    def test_run_filter_never_claims_foreign_jobs(self):
        # Bug 4: --run X claimed and held every foreign job for the whole
        # pass, then reset them unfenced (a live reclaim ran twice).
        self.submit("k-foreign", ["foreign0", "foreign1"])
        mine = self.submit("k-mine", ["mine"])
        seen = {}

        def spy(job, args):
            seen.update(self.states())
            return {"output_digest": "x"}
        self.patch(aw, "HANDLERS", {**aw.HANDLERS, "spy": spy})
        self.assertEqual(self.work(handler="spy", run=mine["run_id"]), 0)
        self.assertEqual(seen["foreign0"], "queued")
        self.assertEqual(seen["foreign1"], "queued")
        con = self.connect()
        try:
            n = con.execute("SELECT COUNT(*) FROM job_attempts WHERE job_id"
                            " LIKE 'foreign%'").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(n, 0)
        self.assertEqual(self.states()["mine"], "succeeded")


class LockedPublishTest(WorkerCase):
    def test_locked_publish_leaves_job_and_worker_continues(self):
        # Bug 5: sqlite3.OperationalError from publish escaped the worker.
        self.submit("k-lock", ["locked", "after"])
        for name in ("publish_success", "publish_failure"):
            def locked(con, job_id, *a, _real=getattr(jobs, name), **kw):
                if job_id == "locked":
                    raise sqlite3.OperationalError("database is locked")
                return _real(con, job_id, *a, **kw)
            self.patch(jobs, name, locked)
        self.patch(aw, "PUBLISH_RETRY_DELAY_S", 0.0)
        self.assertEqual(self.work(), 1)
        states = self.states()
        # Given up at the end of the pass: released with its true reason
        # (never left running to be counted as a crash).
        self.assertEqual(states["locked"], "queued")
        self.assertEqual(states["after"], "succeeded")
        con = self.connect()
        try:
            error = con.execute("SELECT error FROM job_attempts WHERE"
                                " job_id='locked'").fetchone()["error"]
        finally:
            con.close()
        self.assertIn("publish_failed", error)


class LeaseLossReasonTest(WorkerCase):
    def test_publish_lock_lease_loss_is_not_a_crash(self):
        # Review: a successful handler whose publish stayed locked until its
        # lease lapsed was counted as a crash attempt toward dead-lettering.
        self.patch(jobs, "DEFAULT_LEASE_S", 1.0)
        self.patch(jobs, "DEFAULT_HEARTBEAT_S", 0.2)
        self.patch(aw, "PUBLISH_RETRY_DELAY_S", 0.0)

        def slow(job, args):
            time.sleep(1.5)
            return {"output_digest": f"slow:{job['id']}"}
        self.patch(aw, "HANDLERS", {**aw.HANDLERS, "slow": slow})
        real = jobs.publish_success
        unlock = []

        def locked(con, job_id, *a, **kw):
            if job_id == "locked":
                unlock.append(unlock[0] if unlock else time.monotonic() + 1.0)
                if time.monotonic() < unlock[0]:
                    raise sqlite3.OperationalError("database is locked")
            return real(con, job_id, *a, **kw)
        self.patch(jobs, "publish_success", locked)
        self.submit("k-reason", ["locked", "after"])
        self.work(handler="slow")
        con = self.connect()
        try:
            rows = con.execute("SELECT state, error FROM job_attempts WHERE"
                               " job_id='locked' ORDER BY attempt_no").fetchall()
        finally:
            con.close()
        self.assertNotIn("expired", [r["state"] for r in rows])
        self.assertTrue(any("publish_failed" in r["error"] for r in rows), rows)


class LockedPassTest(WorkerCase):
    def _locked(self, name):
        real = getattr(jobs, name)

        def locked(con, job_id, *a, **kw):
            if job_id == "j":
                raise sqlite3.OperationalError("database is locked")
            return real(con, job_id, *a, **kw)
        self.patch(jobs, name, locked)
        self.patch(aw, "PUBLISH_RETRY_DELAY_S", 0.0)

    def _three_passes(self, **over):
        self.submit("k-pass", ["j"])
        for _ in range(3):
            self.work(**over)
            con = self.connect()
            try:
                # The worker is gone: its lease lapses before the next pass.
                con.execute("UPDATE job_attempts SET heartbeat_at='0'"
                            " WHERE job_id='j'")
            finally:
                con.close()
        con = self.connect()
        try:
            jobs.expire_leases(con)
            return con.execute("SELECT state, failure_class FROM processing_jobs"
                               " WHERE id='j'").fetchone()
        finally:
            con.close()

    def test_locked_success_publish_never_dead_letters(self):
        # Review: a publish locked for a whole pass left the job running
        # unrenewed; each lapse counted as a crash until MAX_ATTEMPTS_EXCEEDED.
        self._locked("publish_success")
        row = self._three_passes()
        self.assertNotEqual(row["failure_class"], "MAX_ATTEMPTS_EXCEEDED")

    def test_locked_failure_publish_never_dead_letters(self):
        self._locked("publish_failure")
        row = self._three_passes(handler="fail")
        self.assertNotEqual(row["failure_class"], "MAX_ATTEMPTS_EXCEEDED")


class LostLeaseTest(WorkerCase):
    def test_lost_lease_mid_run_skips_blind_publish(self):
        # Review: a heartbeat that found its lease gone said nothing, and
        # the worker still tried to publish.
        self.patch(jobs, "DEFAULT_HEARTBEAT_S", 0.1)
        calls = []
        real = jobs.publish_success

        def spy(*a, **kw):
            calls.append(a[1])
            return real(*a, **kw)
        self.patch(jobs, "publish_success", spy)

        def stolen(job, args):
            con = self.connect()
            try:
                con.execute("UPDATE processing_jobs SET lease_token='other'"
                            " WHERE id=?", (job["id"],))
            finally:
                con.close()
            time.sleep(0.5)
            return {"output_digest": "x"}
        self.patch(aw, "HANDLERS", {**aw.HANDLERS, "stolen": stolen})
        self.submit("k-lost", ["lost"])
        self.assertEqual(self.work(handler="stolen", max_jobs=1), 1)
        self.assertEqual(calls, [])


class UpgradeHelperTablesTest(unittest.TestCase):
    def test_current_workspace_upgrade_creates_job_profiles(self):
        # Review: upgrade on an already-current v8 workspace returned early
        # without ensure_tables, so /processing/* answered 409 forever.
        import app_server as srv
        import app_workspace as ws_mod
        tmp = tempfile.mkdtemp(prefix="ew-upgrade-")
        self.addCleanup(shutil.rmtree, tmp, True)
        ws = Path(tmp) / "ws"
        con = db.connect(ws)
        try:
            db.init_schema(con)
            # Base-schema workspace: built before the helper table and index.
            con.execute("DROP TABLE p01_job_profiles")
            con.execute("DROP INDEX IF EXISTS idx_jobs_state")
        finally:
            con.close()
        result = ws_mod.upgrade_workspace(ws)
        self.assertFalse(result["upgraded"])
        con = db.connect(ws)
        try:
            names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
            self.assertTrue({"p01_job_profiles", "idx_jobs_state"} <= names)
            srv._p09_require(con)
        finally:
            con.close()


class HeartbeatStartTest(WorkerCase):
    def test_heartbeat_connect_failure_fails_fast(self):
        # Review: a heartbeat thread that could not open its connection died
        # silently while the handler ran on an unrenewed lease.
        import threading
        real = db.connect

        def connect(workspace):
            if threading.current_thread() is not threading.main_thread():
                raise sqlite3.OperationalError("unable to open database file")
            return real(workspace)
        self.patch(db, "connect", connect)
        ran = []

        def spy(job, args):
            ran.append(job["id"])
            return {"output_digest": "x"}
        self.patch(aw, "HANDLERS", {**aw.HANDLERS, "spy": spy})
        self.submit("k-hb", ["hb"])
        self.assertEqual(self.work(handler="spy"), 1)
        self.assertEqual(ran, [])
        self.assertEqual(self.states()["hb"], "queued")


class ClaimLockTest(WorkerCase):
    def test_locked_claim_backs_off_instead_of_crashing(self):
        # Review: OperationalError from expire_leases/claim_next escaped.
        self.patch(aw, "PUBLISH_RETRY_DELAY_S", 0.0)
        real = jobs.expire_leases
        calls = []

        def flaky(*a, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            return real(*a, **kw)
        self.patch(jobs, "expire_leases", flaky)
        self.submit("k-claim", ["c"])
        self.assertEqual(self.work(), 0)
        self.assertEqual(self.states()["c"], "succeeded")


class ProfileScanTest(unittest.TestCase):
    def test_matching_job_found_behind_twenty_others(self):
        # Bug 6: claim_next scanned only the first 20 queued jobs.
        con = fresh()
        jobs.submit_run(con, stages=[
            {"job_id": f"g{i}", "stage": "transcribe", "input_digest": f"g{i}",
             "backend_profile": "gpu"} for i in range(20)], idempotency_key="gpu")
        jobs.submit_run(con, stages=[
            {"job_id": "c0", "stage": "transcribe", "input_digest": "c0",
             "backend_profile": "cpu"}], idempotency_key="cpu")
        job = jobs.claim_next(con, worker_id="cpu", profiles=["cpu"])
        self.assertIsNotNone(job)
        self.assertEqual(job["id"], "c0")


class ClaimPerformanceTest(unittest.TestCase):
    def test_profile_claim_uses_index_with_match_last(self):
        # Review: the profile filter re-walked each run's selection JSON for
        # every candidate inside the write transaction (seconds per claim).
        # The only match sits behind 4000 non-matching queued jobs.
        con = fresh()
        for r in range(5):
            jobs.submit_run(con, stages=[
                {"job_id": f"r{r}-{i}", "stage": "transcribe",
                 "input_digest": f"r{r}-{i}", "backend_profile": "gpu"}
                for i in range(800)], idempotency_key=f"perf-{r}")
        jobs.submit_run(con, stages=[{"job_id": "last", "stage": "transcribe",
                                      "input_digest": "last",
                                      "backend_profile": "local"}],
                        idempotency_key="perf-last")
        sql, params = jobs._claim_sql("local", time.time(), None, ["local"])
        plan = " | ".join(r[3] for r in con.execute("EXPLAIN QUERY PLAN " + sql,
                                                    params))
        self.assertIn("SEARCH p USING INDEX", plan)
        self.assertNotIn("json_each", plan)
        started = time.perf_counter()
        job = jobs.claim_next(con, worker_id="w", profiles=["local"])
        elapsed = time.perf_counter() - started
        self.assertEqual(job["id"], "last")
        self.assertLess(elapsed, 2.0, f"{elapsed * 1000:.1f} ms")  # was ~30 s

    def test_lease_scan_uses_state_index(self):
        # Review: expire_leases scanned the whole job table every pass.
        con = fresh()
        plan = " | ".join(r[3] for r in con.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM processing_jobs"
            " WHERE state='running'"))
        self.assertIn("idx_jobs_state", plan)

    def test_profile_backfilled_for_existing_rows(self):
        # Upgrade path: jobs submitted before the denormalized profile
        # existed keep their profile restriction after ensure_tables.
        con = fresh()
        jobs.submit_run(con, stages=[{"job_id": "g", "stage": "transcribe",
                                      "input_digest": "g",
                                      "backend_profile": "gpu"}],
                        idempotency_key="old")
        con.execute("DROP TABLE p01_job_profiles")
        jobs.ensure_tables(con)
        self.assertIsNone(jobs.claim_next(con, worker_id="w", profiles=["cpu"]))
        self.assertEqual(jobs.claim_next(con, worker_id="w",
                                         profiles=["gpu"])["id"], "g")


class PublishReduceAtomicTest(unittest.TestCase):
    def test_crash_between_publish_and_reduce_strands_nothing(self):
        # Bug 7: publish_success committed, then reduced separately; a crash
        # in between left the next stage blocked forever.
        con = fresh()
        jobs.submit_run(con, stages=[
            {"job_id": "fetch", "stage": "fetch", "input_digest": "a"},
            {"job_id": "tx", "stage": "transcribe", "input_digest": "b",
             "parents": ["fetch"]}], idempotency_key="k")
        job = jobs.claim_next(con, worker_id="w")
        real = jobs.reduce_run

        def killed(*a, **kw):
            raise SystemExit("worker killed between publish and reduce")
        jobs.reduce_run = killed
        try:
            with self.assertRaises(SystemExit):
                jobs.publish_success(con, "fetch", job["fencing_token"], "d")
        finally:
            jobs.reduce_run = real
        pair = (state(con, "fetch"), state(con, "tx"))
        self.assertIn(pair, [("running", "blocked"), ("succeeded", "queued")])


class DefaultJobIdTest(unittest.TestCase):
    def test_default_ids_do_not_collide_across_runs(self):
        # Bug 9: default ids s0, s1, ... are the global primary key.
        con = fresh()
        stages = [{"stage": "noop", "input_digest": "x"},
                  {"stage": "noop", "input_digest": "y", "parents": [0]}]
        one = jobs.submit_run(con, stages=stages, idempotency_key="a")
        two = jobs.submit_run(con, stages=stages, idempotency_key="b")
        self.assertNotEqual(one["run_id"], two["run_id"])
        self.assertEqual(jobs.submit_run(con, stages=stages,
                                         idempotency_key="b")["run_id"],
                         two["run_id"])
        detail = jobs.run_detail(con, two["run_id"])
        ids = {j["id"] for j in detail["jobs"]}
        self.assertEqual(len(ids), 2)
        child = [s for s in detail["selection"]["stages"] if s["parents"]][0]
        self.assertIn(child["parents"][0], ids)
        self.assertIn(child["job_id"], ids)


if __name__ == "__main__":
    unittest.main()
