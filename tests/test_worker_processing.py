"""Worker Task-14 regressions: real local processing, never fake success.

Covers the corrected worker contract: ingest (transcript file ->
source version -> transcribe attestation -> index rebuild; feed file ->
metadata registration with explicit gaps), the scheduled-monitor path
(stored opt-in alerts, matches computed from the local index, outbox
via the sandbox fixture), and auto-mode refusal of unqualified stages.
No network, no model downloads, no live sends.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import sys
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from earwitness.app import customer as _customer
from earwitness.app import db as _db
from earwitness.app import monitoring as _mon
from earwitness.pipeline import jobs as _jobs

import app_workspace as _ws

# Feed parsing runs only in the worker/ingest CLIs (scripts/app_worker.py,
# scripts/ingest.py) on the local runtime pinned by requirements.txt.
# The hosted ASGI service and app_server.py never parse feeds, so
# requirements-hosted.txt does not pin feedparser; under .venv-hosted the
# feed path is skipped here and is exercised under .venv instead.
HAS_FEEDPARSER = importlib.util.find_spec("feedparser") is not None
FEEDPARSER_REASON = ("feedparser not installed: feed ingest is a worker-CLI path on"
                     " the local runtime (requirements.txt), not the hosted service")


def _load_worker():
    spec = importlib.util.spec_from_file_location(
        "ew_app_worker", str(REPO / "scripts" / "app_worker.py"))
    module = importlib.util.module_from_spec(spec)
    old_argv = sys.argv
    sys.argv = ["app_worker.py"]
    try:
        spec.loader.exec_module(module)
    finally:
        sys.argv = old_argv
    return module


_WORKER = _load_worker()


def _args(workspace, profile="any", handler="auto", **over):
    base = {"workspace": workspace, "workspace_id": "local",
            "profile": profile, "handler": handler, "run": "",
            "max_jobs": 10, "source_file": "", "episode_id": "",
            "max_entries": 25}
    base.update(over)
    return types.SimpleNamespace(**base)


def _seed_corpus(con):
    meta = [{"ep_id": "s1", "show_title": "Show One",
             "feed_url": "https://one.example/rss", "ep_title": "First",
             "pub_date": "2026-03-10", "category": "business"}]
    con.execute(
        "INSERT INTO source_versions (source_key, version_no, sha256, payload,"
        " is_active, created_at) VALUES ('episodes.json', 1, 'sh', ?, 1, ?)",
        (json.dumps(meta), _db.utcnow()))
    segs = [{"start": 0.0, "end": 10.0, "text": "acme bright widget"}]
    cur = con.execute(
        "INSERT INTO source_versions (source_key, version_no, sha256, payload,"
        " is_active, created_at) VALUES (?, 1, ?, ?, 1, ?)",
        ("transcript:s1", "sh-s1", json.dumps({"segments": segs}),
         _db.utcnow()))
    tv = cur.lastrowid
    con.execute(
        "INSERT INTO episodes (id, kind, show_title, ep_title, category,"
        " pub_date, fetch_date, transcript_version_id, revision, updated_at)"
        " VALUES ('s1', 'supplied', 'Show One', 'First', 'business',"
        " '2026-03-10', '2026-09-21', ?, 1, ?)", (tv, _db.utcnow()))
    from earwitness.app import citations as _citations
    _citations.publish_source_state(con, kind="episode", record_id="s1",
                                    actor="fixture")
    con.commit()


class WorkerWorkspaceTest(unittest.TestCase):
    """Each test gets a throwaway repo-relative workspace directory."""

    def setUp(self):
        self.workdir = REPO / "test-work-tmp"
        if self.workdir.exists():
            shutil.rmtree(self.workdir)
        self.workdir.mkdir(parents=True)
        self.workspace = "test-work-tmp"

    def tearDown(self):
        if self.workdir.exists():
            shutil.rmtree(self.workdir)

    def _connect(self):
        workspace = _ws.resolve_workspace(self.workspace)
        con = _db.connect(workspace)
        _db.init_schema(con)
        return con


class IngestTest(WorkerWorkspaceTest):
    def setUp(self):
        super().setUp()
        con = self._connect()
        con.close()

    def _write_source(self, name, content: bytes):
        path = self.workdir / name
        path.write_bytes(content)
        return f"{self.workspace}/{name}"

    def test_transcript_ingest_builds_proof_chain(self):
        rel = self._write_source("t1.json", json.dumps({
            "episode_id": "ep-t1", "show_title": "Local",
            "segments": [{"start": 0.0, "end": 30.0,
                          "text": "acme bright widget offer"},
                         {"start": 30.0, "end": 60.0,
                          "text": "more local talk"}]}).encode())
        out = _WORKER._handle_ingest(
            {"id": "j1"}, _args(self.workspace, source_file=rel))
        self.assertFalse(out["empty_ok"])
        self.assertEqual(out["episode_id"], "ep-t1")
        con = self._connect()
        try:
            tv = con.execute("SELECT transcript_version_id FROM episodes"
                             " WHERE id='ep-t1'").fetchone()[0]
            self.assertEqual(tv, out["transcript_version_id"])
            rows = con.execute(
                "SELECT stage, status, source_identity FROM coverage_attestations"
                " WHERE episode_id='ep-t1' ORDER BY id").fetchall()
            stages = [r[0] for r in rows]
            self.assertIn("transcribe", stages)
            self.assertIn("index", stages)
            for stage, status, identity in rows:
                self.assertEqual(status, "success")
                self.assertEqual(identity, tv)
            state = con.execute("SELECT value FROM index_state WHERE key='status'"
                                ).fetchone()
            self.assertIsNotNone(state)
        finally:
            con.close()

    def test_transcript_ingest_requires_processable_content(self):
        rel = self._write_source("empty.json", json.dumps(
            {"episode_id": "ep-x", "segments": []}).encode())
        with self.assertRaises(RuntimeError):
            _WORKER._handle_ingest(
                {"id": "j1"}, _args(self.workspace, source_file=rel))
        rel = self._write_source("junk.json", b"{not json")
        with self.assertRaises(RuntimeError):
            _WORKER._handle_ingest(
                {"id": "j1"}, _args(self.workspace, source_file=rel))

    def test_ingest_rejects_nonlocal_and_missing_files(self):
        with self.assertRaises(RuntimeError):
            _WORKER._handle_ingest({"id": "j1"}, _args(self.workspace))
        with self.assertRaises(RuntimeError):
            _WORKER._handle_ingest(
                {"id": "j1"},
                _args(self.workspace, source_file="../outside.json"))
        with self.assertRaises(RuntimeError):
            _WORKER._handle_ingest(
                {"id": "j1"},
                _args(self.workspace, source_file=f"{self.workspace}/nope.json"))

    @unittest.skipUnless(HAS_FEEDPARSER, FEEDPARSER_REASON)
    def test_feed_ingest_registers_metadata_with_gaps(self):
        rss = (b'<?xml version="1.0"?><rss version="2.0"><channel>'
               b'<title>Local</title>'
               b'<item><guid>ep-a</guid><title>Episode A</title>'
               b'<enclosure url="https://cdn.example/a.mp3" type="audio/mpeg"/>'
               b'</item></channel></rss>')
        rel = self._write_source("feed.xml", rss)
        out = _WORKER._handle_ingest(
            {"id": "j1"}, _args(self.workspace, source_file=rel))
        self.assertEqual(out["entries"], 1)
        self.assertEqual(len(out["registered"]), 1)
        self.assertTrue(out["gaps"])
        self.assertTrue(all("no attestation" in g for g in out["gaps"]))
        con = self._connect()
        try:
            ep = con.execute("SELECT audio_url, transcript_version_id FROM episodes"
                             " WHERE id=?", (out["registered"][0],)).fetchone()
            self.assertTrue(ep[0].startswith("https://cdn.example/"))
            self.assertIsNone(ep[1])
            count = con.execute("SELECT COUNT(*) FROM coverage_attestations"
                                ).fetchone()[0]
            self.assertEqual(count, 0)
        finally:
            con.close()
        # Re-run is quiet: already known, nothing new, still success.
        again = _WORKER._handle_ingest(
            {"id": "j2"}, _args(self.workspace, source_file=rel))
        self.assertTrue(again["empty_ok"])
        self.assertEqual(again["registered"], [])

    @unittest.skipUnless(HAS_FEEDPARSER, FEEDPARSER_REASON)
    def test_feed_without_entries_fails(self):
        rel = self._write_source("bare.xml", b'<?xml version="1.0"?><rss version="2.0">'
                                              b'<channel><title>Empty</title></channel></rss>')
        with self.assertRaises(RuntimeError):
            _WORKER._handle_ingest(
                {"id": "j1"}, _args(self.workspace, source_file=rel))


class MonitorPathTest(WorkerWorkspaceTest):
    def _alert(self, con, **monitor):
        query = {"version": 2, "mode": "mentions", "q": "acme"}
        saved = _customer.save_alert(con, "watch acme", query)
        rev = saved["revision"]
        monitor.setdefault("enabled", True)
        monitor.setdefault("interval_s", 3600)
        monitor.setdefault("recipients", ["ops@example.com"])
        monitor.setdefault("next_due_at", "2000-01-01T00:00:00Z")
        out = _customer.set_alert_monitor(con, saved["id"], monitor, rev,
                                          actor="owner")
        con.commit()
        return saved["id"], out

    def test_quiet_run_with_nothing_due(self):
        con = self._connect()
        try:
            _seed_corpus(con)
        finally:
            con.close()
        out = _WORKER._handle_monitor_check(
            {"id": "j1"}, _args(self.workspace, handler="monitor"))
        self.assertTrue(out["empty_ok"])
        self.assertEqual(out["alerts"], [])

    def test_scheduled_alert_computes_matches_and_delivers(self):
        con = self._connect()
        try:
            _seed_corpus(con)
            aid, _mon = self._alert(con)
            first = _WORKER._handle_monitor_check(
                {"id": "j1"}, _args(self.workspace, handler="monitor"))
            self.assertEqual(first["alerts"], [aid])
            ran = first["ran"][0]
            self.assertEqual(ran["emitted"], 0)  # baseline established silently
            # New matching content appears in the local index.
            cur = con.execute(
                "INSERT INTO source_versions (source_key, version_no, sha256,"
                " payload, is_active, created_at) VALUES (?, 1, ?, ?, 1, ?)",
                ("transcript:s2", "sh-s2",
                 json.dumps({"segments": [{"start": 0.0, "end": 10.0,
                                           "text": "fresh acme launch"}]}),
                 _db.utcnow()))
            tv = cur.lastrowid
            con.execute(
                "INSERT INTO episodes (id, kind, show_title, ep_title, category,"
                " pub_date, fetch_date, transcript_version_id, revision,"
                " updated_at) VALUES ('s2', 'supplied', 'Show Two', 'Second',"
                " 'business', '2026-03-11', '2026-09-21', ?, 1, ?)",
                (tv, _db.utcnow()))
            from earwitness.app import citations as _citations
            _citations.publish_source_state(con, kind="episode",
                                            record_id="s2", actor="fixture")
            con.commit()
            _customer.set_alert_monitor(
                con, aid, {"enabled": True, "interval_s": 3600,
                           "recipients": ["ops@example.com"],
                           "next_due_at": "2000-01-01T00:00:00Z"},
                _customer.get_alert(con, aid)["revision"], actor="owner")
            con.commit()
            second = _WORKER._handle_monitor_check(
                {"id": "j2"}, _args(self.workspace, handler="monitor"))
            ran = second["ran"][0]
            self.assertEqual(ran["emitted"], 1)
            self.assertEqual(ran["queued"]["queued"], 1)
            self.assertEqual(ran["delivered"]["sent"], 1)
            self.assertEqual(ran["delivered"]["provider"], "sandbox")
            states = con.execute("SELECT state FROM notification_outbox").fetchall()
            self.assertTrue(states)
            self.assertTrue(all(r[0] == "sent" for r in states))
            attempts = [tuple(r) for r in con.execute(
                "SELECT provider, state FROM delivery_attempts").fetchall()]
            self.assertEqual(attempts, [("sandbox", "sent")])
        finally:
            con.close()

    def test_manual_alerts_are_never_touched(self):
        con = self._connect()
        try:
            _seed_corpus(con)
            query = {"version": 2, "mode": "mentions", "q": "acme"}
            saved = _customer.save_alert(con, "manual only", query)
            con.commit()
            before = con.execute("SELECT last_attempted_at, last_successful_at,"
                                 " baseline_json FROM customer_alerts"
                                 " WHERE id=?", (saved["id"],)).fetchone()
            out = _WORKER._handle_monitor_check(
                {"id": "j1"}, _args(self.workspace, handler="monitor"))
            self.assertTrue(out["empty_ok"])
            after = con.execute("SELECT last_attempted_at, last_successful_at,"
                                " baseline_json FROM customer_alerts"
                                " WHERE id=?", (saved["id"],)).fetchone()
            self.assertEqual(tuple(after), tuple(before))
        finally:
            con.close()

    def test_all_failing_due_alerts_fail_the_job(self):
        con = self._connect()
        try:
            _seed_corpus(con)
            aid, _mon = self._alert(con)
            con.execute("UPDATE customer_alerts SET query_json='{}'"
                        " WHERE id=?", (aid,))
            con.commit()
            with self.assertRaises(RuntimeError):
                _WORKER._handle_monitor_check(
                    {"id": "j1"}, _args(self.workspace, handler="monitor"))
        finally:
            con.close()


class AutoDispatchTest(WorkerWorkspaceTest):
    def test_auto_refuses_unqualified_stages(self):
        for stage in ("fetch", "transcribe", "screen", "refine", "noop",
                      "frobnicate"):
            with self.assertRaises(RuntimeError, msg=stage):
                _WORKER._auto_handler(stage)
        self.assertIs(_WORKER._auto_handler("monitor"),
                      _WORKER._handle_monitor_check)
        self.assertIs(_WORKER._auto_handler("ingest"), _WORKER._handle_ingest)

    def _submit(self, con, stage, key):
        return _jobs.submit_run(
            con, workspace_id="local",
            stages=[{"job_id": f"{key}-0", "stage": stage,
                     "input_digest": "d", "parents": [],
                     "backend_profile": "any", "cost_estimate": {}}],
            config_digest="c", mode="new_episodes", initiator="test",
            idempotency_key=key)

    def test_auto_monitor_run_end_to_end(self):
        con = self._connect()
        try:
            _seed_corpus(con)
            summary = self._submit(con, "monitor", "k-monitor-1")
            con.commit()
        finally:
            con.close()
        code = _WORKER.cmd_work(_args(self.workspace, profile="any",
                                      handler="auto", run=summary["run_id"]))
        self.assertEqual(code, 0)
        con = self._connect()
        try:
            state = con.execute("SELECT state FROM processing_jobs").fetchone()[0]
            self.assertEqual(state, "succeeded")
        finally:
            con.close()

    def test_auto_fetch_run_fails_with_gap(self):
        con = self._connect()
        try:
            summary = self._submit(con, "fetch", "k-fetch-1")
            con.commit()
        finally:
            con.close()
        code = _WORKER.cmd_work(_args(self.workspace, profile="any",
                                      handler="auto", run=summary["run_id"]))
        self.assertEqual(code, 1)
        con = self._connect()
        try:
            row = con.execute("SELECT state, error FROM job_attempts").fetchone()
            self.assertEqual(row[0], "failed")
            self.assertIn("network acquisition", row[1])
        finally:
            con.close()

    def test_explicit_noop_still_mechanics(self):
        con = self._connect()
        try:
            summary = self._submit(con, "noop", "k-noop-1")
            con.commit()
        finally:
            con.close()
        code = _WORKER.cmd_work(_args(self.workspace, profile="any",
                                      handler="noop", run=summary["run_id"]))
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
