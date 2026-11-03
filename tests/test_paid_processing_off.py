"""L4p: paid processing is unavailable in the pilot (stdlib unittest).

`reserve_paid` and `admit_dispatch` refuse unconditionally. No cap or
approval setting changes that: no budget authority exists, so a "cap
configuration" must never stand in for one. Disposable in-memory ledgers
and throwaway workspaces only; the fake provider counts calls and never
touches a network.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from earwitness.app import db
from earwitness.pipeline import jobs

import app_workspace as _ws

PAID_STAGES = ("fetch", "transcribe", "screen", "refine")

# Every way a cap or approval might be configured. None of them may enable
# paid work. The names are the ones a future budget design would likely use;
# the refusal must not read any of them.
CAP_CONFIGS = {
    "missing": {},
    "zero_cap": {"EARWITNESS_PAID_CAP_USD": "0", "EARWITNESS_PAID_APPROVED": "0"},
    "positive_approved": {"EARWITNESS_PAID_CAP_USD": "100",
                          "EARWITNESS_PAID_APPROVED": "1"},
}
CAP_ENV_NAMES = tuple({k for cfg in CAP_CONFIGS.values() for k in cfg})


class FakeProvider:
    """A paid provider that must never be reached."""

    def __init__(self):
        self.calls = 0

    def send(self, *_a, **_k):
        self.calls += 1
        return {"accepted": True}


def fresh():
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON;")
    db.init_schema(con)
    jobs.ensure_tables(con)
    return con


def claimed_job(con, key="l4p-run", stage="noop"):
    jobs.submit_run(con, stages=[{"job_id": f"{key}-0", "stage": stage,
                                  "input_digest": "in-1"}],
                    config_digest="cfg-1", initiator="test",
                    idempotency_key=key)
    return jobs.claim_next(con, worker_id="w1", now=1000.0)


def seed_op(con, job_id, op_id="op-restored", disposition="reserved"):
    """A paid_operations row that did not come from reserve_paid.

    Stands for a restored backup row or a legacy row.
    """
    con.execute(
        "INSERT INTO paid_operations (id, workspace_id, job_id, reservation,"
        " provider_key, disposition, receipt, revision, updated_at)"
        " VALUES (?, 'local', ?, '{\"worst_case_cost\": 2.5}', 'prov-1', ?,"
        " '{}', 1, 't')", (op_id, job_id, disposition))


TABLES = ("paid_operations", "processing_jobs", "processing_runs",
          "job_attempts")


def snapshot(con):
    return {t: [tuple(r) for r in con.execute(f"SELECT * FROM {t} ORDER BY 1")]
            for t in TABLES}


class PaidEntrypointsRefuseTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh()
        self.job = claimed_job(self.con)
        self.provider = FakeProvider()

    def _env(self, name):
        clean = {k: v for k, v in os.environ.items() if k not in CAP_ENV_NAMES}
        clean.update(CAP_CONFIGS[name])
        return mock.patch.dict(os.environ, clean, clear=True)

    def _dispatch(self, op_id):
        """The only order a paid send may follow: admit, then send."""
        jobs.admit_dispatch(self.con, op_id, now=1001.0)
        self.provider.send()

    def _assert_unchanged(self, before):
        self.assertEqual(snapshot(self.con), before)

    def test_reserve_refuses_under_every_cap_config(self):
        for name in CAP_CONFIGS:
            for cost in (2.5, 0.0, 1e9):
                with self.subTest(config=name, cost=cost), self._env(name):
                    before = snapshot(self.con)
                    changes = self.con.total_changes
                    with self.assertRaises(jobs.PaidProcessingUnavailable) as ctx:
                        jobs.reserve_paid(
                            self.con, job_id=self.job["id"],
                            worst_case_cost=cost, provider_key="prov-1",
                            idempotency_key=f"op-{name}-{cost}")
                    self.assertEqual(ctx.exception.code,
                                     "PAID_PROCESSING_UNAVAILABLE")
                    self.assertEqual(str(ctx.exception), jobs.PAID_PROCESSING_UNAVAILABLE)
                    self.assertNotIn("/", str(ctx.exception))  # review LOW-3: no internal paths
                    self._assert_unchanged(before)
                    self.assertEqual(self.con.total_changes, changes)
        self.assertEqual(self.provider.calls, 0)

    def test_reserve_refusal_precedes_argument_validation(self):
        # Even malformed requests get the product refusal, not a path that
        # could be mistaken for "valid once configured".
        with self.assertRaises(jobs.PaidProcessingUnavailable):
            jobs.reserve_paid(self.con, job_id=self.job["id"],
                              worst_case_cost=0, provider_key="",
                              idempotency_key="")

    def test_admit_refuses_under_every_cap_config(self):
        seed_op(self.con, self.job["id"])
        for name in CAP_CONFIGS:
            with self.subTest(config=name), self._env(name):
                before = snapshot(self.con)
                changes = self.con.total_changes
                with self.assertRaises(jobs.PaidProcessingUnavailable):
                    self._dispatch("op-restored")
                self._assert_unchanged(before)
                self.assertEqual(self.con.total_changes, changes)
                op = jobs.get_paid_operation(self.con, "op-restored")
                self.assertEqual(op["disposition"], "reserved")
                self.assertEqual(op["revision"], 1)
        self.assertEqual(self.provider.calls, 0)

    def test_admit_refuses_unknown_operation_without_lookup_error(self):
        # Unavailable beats "not found": no state or id probing.
        with self.assertRaises(jobs.PaidProcessingUnavailable):
            jobs.admit_dispatch(self.con, "no-such-op")

    def test_replay_of_existing_reservation_is_refused(self):
        seed_op(self.con, self.job["id"], op_id="op-replay")
        for name in CAP_CONFIGS:
            with self.subTest(config=name), self._env(name):
                before = snapshot(self.con)
                with self.assertRaises(jobs.PaidProcessingUnavailable):
                    jobs.reserve_paid(
                        self.con, job_id=self.job["id"], worst_case_cost=2.5,
                        provider_key="prov-1", idempotency_key="op-replay")
                self._assert_unchanged(before)
        self.assertEqual(self.provider.calls, 0)

    def test_restored_reserved_row_never_reaches_the_provider(self):
        seed_op(self.con, self.job["id"], op_id="op-backup")
        with self._env("positive_approved"):
            with self.assertRaises(jobs.PaidProcessingUnavailable):
                self._dispatch("op-backup")
        self.assertEqual(
            jobs.get_paid_operation(self.con, "op-backup")["disposition"],
            "reserved")
        self.assertEqual(self.provider.calls, 0)

    def test_refusal_is_a_job_error_callers_can_catch(self):
        self.assertTrue(issubclass(jobs.PaidProcessingUnavailable,
                                   jobs.JobError))


def _load_worker():
    name = "ew_app_worker_l4p"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, REPO / "scripts" / "app_worker.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


aw = _load_worker()


def _args(workspace, handler, run):
    return types.SimpleNamespace(
        workspace=workspace, workspace_id="local", profile="any",
        handler=handler, run=run, max_jobs=10, source_file="",
        episode_id="", max_entries=25)


class PaidStageListTest(unittest.TestCase):
    """Review LOW-2: the worker's refusal list covers every stage the app
    submits for processing (app_server.P09_STAGES), read from source so the
    heavy server module is not imported."""

    def test_every_submitted_stage_is_refused(self):
        import ast
        tree = ast.parse((REPO / "scripts" / "app_server.py").read_text())
        stages = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                    getattr(t, "id", None) == "P09_STAGES" for t in node.targets):
                stages = set(ast.literal_eval(node.value))
        self.assertTrue(stages, "P09_STAGES not found in app_server.py")
        self.assertLessEqual(stages, set(aw.PAID_STAGES))


class WorkerPaidStagesRefuseTest(unittest.TestCase):
    """Real worker selection: a paid stage is never a successful no-op."""

    def setUp(self):
        self.workdir = REPO / "test-work-l4p"
        if self.workdir.exists():
            shutil.rmtree(self.workdir)
        self.workdir.mkdir(parents=True)
        self.workspace = "test-work-l4p"

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def _con(self):
        con = db.connect(_ws.resolve_workspace(self.workspace))
        db.init_schema(con)
        return con

    def _run_stage(self, stage, handler):
        con = self._con()
        try:
            summary = jobs.submit_run(
                con, workspace_id="local",
                stages=[{"job_id": f"{stage}-{handler}-0", "stage": stage,
                         "input_digest": f"d-{handler}", "parents": [],
                         "backend_profile": "any", "cost_estimate": {}}],
                config_digest="c", mode="new_episodes", initiator="test",
                idempotency_key=f"k-{stage}-{handler}")
            con.commit()
        finally:
            con.close()
        code = aw.cmd_work(_args(self.workspace, handler, summary["run_id"]))
        con = self._con()
        try:
            job = con.execute("SELECT state, failure_class FROM processing_jobs"
                              " WHERE run_id=?", (summary["run_id"],)).fetchone()
            attempts = con.execute(
                "SELECT state, error FROM job_attempts").fetchall()
            paid = con.execute("SELECT COUNT(*) FROM paid_operations"
                               ).fetchone()[0]
        finally:
            con.close()
        return code, job, attempts, paid

    def test_auto_fails_visibly_for_every_paid_and_unknown_stage(self):
        for stage in PAID_STAGES + ("frobnicate",):
            with self.subTest(stage=stage):
                code, job, attempts, paid = self._run_stage(stage, "auto")
                self.assertEqual(code, 1)
                self.assertEqual(job[0], "failed")
                self.assertEqual(attempts[-1][0], "failed")
                self.assertIn("no fake success", attempts[-1][1])
                self.assertEqual(paid, 0)

    def test_explicit_handler_cannot_noop_a_paid_stage(self):
        for stage in PAID_STAGES:
            for handler in ("noop", "ingest", "monitor"):
                with self.subTest(stage=stage, handler=handler):
                    code, job, attempts, paid = self._run_stage(stage, handler)
                    self.assertEqual(code, 1)
                    self.assertEqual(job[0], "failed")
                    self.assertNotEqual(job[0], "succeeded")
                    self.assertIn("unavailable", attempts[-1][1])
                    self.assertEqual(paid, 0)

    def test_explicit_noop_still_runs_as_mechanics_for_noop_stage(self):
        code, job, _attempts, _paid = self._run_stage("noop", "noop")
        self.assertEqual(code, 0)
        self.assertEqual(job[0], "succeeded")


if __name__ == "__main__":
    unittest.main()
