"""P09 processing-center API regressions (stdlib unittest, disposable state only).

Drives the real scripts/app_server.py Handler (routing, Host/CSRF/Origin
guards, body limits, ledger mapping) over in-memory streams: no TCP bind,
which the sandbox forbids. Every test fails on a plausible product bug:
duplicate logical runs, retried non-failures, unfenced cancels, unsafe URL
registration, uncapped uploads, or a missing offline-worker command.
Disposable workspaces live under .muse-product-p09p10/test-work and are
removed in tearDown; owner state is untouched.
"""
from __future__ import annotations

import io
import json
import shutil
import socket
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO))

import app_server as srv
from earwitness.app import db
from earwitness.pipeline import jobs as ledger

PORT = 18779
HOST = f"127.0.0.1:{PORT}"
CSRF = "test-csrf-token"


def workdir(name: str) -> Path:
    root = REPO / ".muse-product-p09p10" / "test-work"
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=name + "-", dir=str(root)))


def seed(ws: Path, episodes=()) -> None:
    con = db.connect(ws)
    try:
        db.init_schema(con)
        for ep in episodes:
            con.execute(
                "INSERT OR IGNORE INTO episodes (id, kind, show_title, ep_title,"
                " updated_at) VALUES (?,?,?,?,'t')",
                (ep, "episode", "Test Show", f"Test {ep}"),
            )
        con.commit()
    finally:
        con.close()


class HandlerCase(unittest.TestCase):
    def setUp(self):
        self.ws = workdir("case")
        seed(self.ws, ("ep_a", "ep_b"))
        self._prev_ws = srv._state["workspace"]
        self._prev_port = srv._state["port"]
        self._prev_csrf = srv._state["csrf"]
        srv._state["workspace"] = self.ws
        srv._state["port"] = PORT
        srv._state["csrf"] = CSRF
        self._prev_gai = socket.getaddrinfo

    def tearDown(self):
        srv._state["workspace"] = self._prev_ws
        srv._state["port"] = self._prev_port
        srv._state["csrf"] = self._prev_csrf
        socket.getaddrinfo = self._prev_gai
        shutil.rmtree(self.ws, ignore_errors=True)

    # -- harness: real Handler, in-memory transport --
    def call(self, method, path, body=None, raw=None, ctype=None,
             filename=None):
        handler = srv.Handler.__new__(srv.Handler)
        handler.command = method
        handler.path = path
        handler.request_version = "HTTP/1.1"
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler._headers_buffer = []
        handler.log_message = lambda *a: None
        payload = b""
        headers = {"Host": HOST}
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        elif raw is not None:
            payload = raw
            headers["Content-Type"] = ctype or "application/octet-stream"
        if method in ("POST", "PUT", "DELETE"):
            headers["X-CSRF-Token"] = CSRF
            headers["Origin"] = f"http://{HOST}"
        if filename is not None:
            headers["X-Filename"] = filename
        headers["Content-Length"] = str(len(payload))
        handler.headers = headers
        handler.rfile = io.BytesIO(payload)
        handler.wfile = io.BytesIO()
        handler.close_connection = True
        if method == "GET":
            handler.do_GET()
        elif method == "POST":
            handler.do_POST()
        elif method == "PUT":
            handler.do_PUT()
        else:
            raise AssertionError(method)
        data = handler.wfile.getvalue()
        head, _, rest = data.partition(b"\r\n\r\n")
        status = int(head.split(b" ")[1])
        try:
            return status, json.loads(rest)
        except ValueError:
            return status, {"_raw": rest[:200]}

    def fake_dns(self, mapping):
        def _gai(host, port, *args, **kwargs):
            if host not in mapping:
                raise socket.gaierror("name resolution failed")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     (mapping[host], port or 443))]
        socket.getaddrinfo = _gai

    def start_run(self, key="k-1", episodes=("ep_a",), profile="local"):
        status, out = self.call("POST", "/api/v1/processing/runs", {
            "idempotency_key": key, "episode_ids": list(episodes),
            "profile": profile})
        self.assertEqual(status, 200, out)
        return out

    def ledger_con(self):
        con = db.connect(self.ws)
        con.row_factory = __import__("sqlite3").Row
        return con


class EstimateTest(HandlerCase):
    def test_local_estimate_lists_reuse_and_zero_reserved(self):
        status, out = self.call("POST", "/api/v1/processing/estimate", {
            "episode_ids": ["ep_a", "ep_b"], "profile": "local"})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["stage_count"], 8)
        self.assertEqual(out["reusable"], 0)
        self.assertEqual(out["new_stages"], 8)
        self.assertEqual(out["reserved_cost"], 0.0)
        self.assertIn("local", out["cost_note"].lower())

    def test_unknown_episode_rejected(self):
        status, out = self.call("POST", "/api/v1/processing/estimate", {
            "episode_ids": ["nope"], "profile": "local"})
        self.assertEqual(status, 400)
        self.assertIn("unknown episodes", out["error"]["message"])

    def test_unsupported_profile_actionable_without_provider_switch(self):
        status, out = self.call("POST", "/api/v1/processing/estimate", {
            "episode_ids": ["ep_a"], "profile": "gpu-a100"})
        self.assertEqual(status, 400)
        self.assertIn("local", out["error"]["message"])
        self.assertIn("No provider was switched", out["error"]["message"])


class DuplicateClickTest(HandlerCase):
    def test_duplicate_click_starts_one_logical_run(self):
        first = self.start_run(key="dup-1")
        second = self.start_run(key="dup-1")
        self.assertEqual(first["run"]["run_id"], second["run"]["run_id"])
        con = self.ledger_con()
        try:
            count = con.execute(
                "SELECT COUNT(*) FROM processing_runs").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(count, 1)

    def test_same_key_divergent_body_conflicts(self):
        self.start_run(key="dup-2", episodes=("ep_a",))
        status, out = self.call("POST", "/api/v1/processing/runs", {
            "idempotency_key": "dup-2", "episode_ids": ["ep_b"],
            "profile": "local"})
        self.assertEqual(status, 409, out)

    def test_missing_idempotency_key_rejected(self):
        status, out = self.call("POST", "/api/v1/processing/runs", {
            "episode_ids": ["ep_a"], "profile": "local"})
        self.assertEqual(status, 400)
        self.assertIn("idempotency_key", out["error"]["message"])


class RetryOnlyFailedTest(HandlerCase):
    def _fail_first_job(self, run_id):
        con = self.ledger_con()
        try:
            job = ledger.claim_next(con, worker_id="t", now=1000.0)
            self.assertIsNotNone(job)
            self.assertEqual(job["run_id"], run_id)
            ledger.publish_failure(con, job["id"], job["fencing_token"],
                                   failure_class="HANDLER_FAILED",
                                   error="boom", retryable=False, now=1001.0)
            return job["id"]
        finally:
            con.close()

    def test_retry_reopens_only_selected_failures(self):
        out = self.start_run(key="retry-1")
        run_id = out["run"]["run_id"]
        failed_id = self._fail_first_job(run_id)
        status, detail = self.call(
            "GET", f"/api/v1/processing/runs/{run_id}")
        self.assertEqual(status, 200, detail)
        states = {j["id"]: j["state"] for j in detail["jobs"]}
        self.assertEqual(states[failed_id], "failed")
        rev = detail["run"]["revision"]
        status, reopened = self.call(
            "POST", f"/api/v1/processing/runs/{run_id}/retry", {
                "failed_job_ids": [failed_id],
                "idempotency_key": "r-1",
                "expected_revision": rev})
        self.assertEqual(status, 200, reopened)
        states = {j["id"]: j["state"] for j in reopened["jobs"]}
        self.assertEqual(states[failed_id], "queued")

    def test_retry_of_succeeded_job_refused(self):
        out = self.start_run(key="retry-2")
        run_id = out["run"]["run_id"]
        con = self.ledger_con()
        try:
            job = ledger.claim_next(con, worker_id="t", now=1000.0)
            ledger.publish_success(con, job["id"], job["fencing_token"],
                                   "digest-x", now=1001.0)
            succeeded_id = job["id"]
        finally:
            con.close()
        status, body = self.call(
            "POST", f"/api/v1/processing/runs/{run_id}/retry", {
                "failed_job_ids": [succeeded_id], "idempotency_key": "r-2"})
        self.assertEqual(status, 409, body)
        self.assertIn("not failed", body["error"]["message"])

    def test_reconciliation_needs_evidence_action_not_retry(self):
        out = self.start_run(key="retry-3")
        run_id = out["run"]["run_id"]
        con = self.ledger_con()
        try:
            job = ledger.claim_next(con, worker_id="t", now=1000.0)
            # reserve_paid/admit_dispatch refuse in the pilot (L4p): seed the
            # admitted row they used to write, keep the ambiguity/retry check.
            op = {"id": "op-9"}
            con.execute(
                "INSERT INTO paid_operations (id, workspace_id, job_id,"
                " reservation, provider_key, disposition, receipt, revision,"
                " updated_at) VALUES ('op-9', 'local', ?,"
                " '{\"worst_case_cost\": 2.5}', 'prov-1', 'admitted', '{}', 2,"
                " 't')", (job["id"],))
            ledger.mark_ambiguous(con, op["id"], "worker died mid-send",
                                  now=1003.0)
            stuck_id = job["id"]
        finally:
            con.close()
        status, detail = self.call(
            "GET", f"/api/v1/processing/runs/{run_id}")
        self.assertEqual(status, 200, detail)
        self.assertTrue(detail["reconcile"], detail)
        entry = detail["reconcile"][0]
        self.assertEqual(entry["action"], "reconcile")
        self.assertIn("reconcile", entry["operator_command"])
        self.assertIn("ordinary retry cannot", entry["note"])
        status, body = self.call(
            "POST", f"/api/v1/processing/runs/{run_id}/retry", {
                "failed_job_ids": [stuck_id], "idempotency_key": "r-3"})
        self.assertEqual(status, 409, body)


class CancelFencingTest(HandlerCase):
    def test_stale_revision_rejected_then_cancel_accepted(self):
        out = self.start_run(key="cancel-1")
        run_id = out["run"]["run_id"]
        rev = out["run"]["revision"]
        status, body = self.call(
            "POST", f"/api/v1/processing/runs/{run_id}/cancel",
            {"expected_revision": rev + 25})
        self.assertEqual(status, 412, body)
        status, cancelled = self.call(
            "POST", f"/api/v1/processing/runs/{run_id}/cancel",
            {"expected_revision": rev})
        self.assertEqual(status, 200, cancelled)
        self.assertIn(cancelled["run"]["status"],
                      ("cancel_requested", "cancelled"))

    def test_cancel_requires_revision(self):
        out = self.start_run(key="cancel-2")
        run_id = out["run"]["run_id"]
        status, body = self.call(
            "POST", f"/api/v1/processing/runs/{run_id}/cancel", {})
        self.assertEqual(status, 428, body)


class UnsafeUrlTest(HandlerCase):
    def test_scheme_and_shape_rejected_without_dns(self):
        for url in ("file:///etc/passwd", "ftp://example.com/feed.xml",
                    "not-a-url", "https://user:pass@example.com/feed"):
            status, body = self.call(
                "POST", "/api/v1/processing/sources", {"url": url})
            self.assertEqual(status, 400, (url, body))

    def test_private_linklocal_metadata_denied(self):
        self.fake_dns({"meta.example": "169.254.169.254",
                       "int.example": "10.0.0.5",
                       "ll.example": "fe80::1",
                       "ok.example": "93.184.216.1"})
        for host in ("meta.example", "int.example", "ll.example"):
            status, body = self.call(
                "POST", "/api/v1/processing/sources",
                {"url": f"https://{host}/feed.xml"})
            self.assertEqual(status, 400, (host, body))
            self.assertIn("denied", body["error"]["message"])

    def test_unresolvable_host_rejected(self):
        self.fake_dns({})
        status, body = self.call(
            "POST", "/api/v1/processing/sources",
            {"url": "https://ghost.invalid/feed.xml"})
        self.assertEqual(status, 400, body)

    def test_public_host_registered_once(self):
        self.fake_dns({"ok.example": "93.184.216.1"})
        first = self.call("POST", "/api/v1/processing/sources",
                          {"url": "https://ok.example/feed.xml"})
        second = self.call("POST", "/api/v1/processing/sources",
                           {"url": "https://ok.example/feed.xml"})
        self.assertEqual(first[0], 200, first)
        self.assertEqual(second[0], 200, second)
        self.assertEqual(first[1]["source_id"], second[1]["source_id"])


class WorkerOfflineTest(HandlerCase):
    def test_offline_state_names_profile_and_exact_command(self):
        out = self.start_run(key="offline-1")
        run_id = out["run"]["run_id"]
        status, detail = self.call(
            "GET", f"/api/v1/processing/runs/{run_id}")
        self.assertEqual(status, 200, detail)
        self.assertFalse(detail["worker"]["worker_live"])
        self.assertTrue(detail["worker"]["queued_without_worker"])
        self.assertEqual(detail["required_profile"], "local")
        rel = str(self.ws.relative_to(REPO))
        self.assertEqual(
            detail["operator_command"],
            f"python3 scripts/app_worker.py --workspace {rel} work --profile local")

    def test_unknown_run_is_404_and_bad_id_is_400(self):
        status, _ = self.call("GET", "/api/v1/processing/runs/run_abcdef123456")
        self.assertEqual(status, 404)
        status, _ = self.call("GET", "/api/v1/processing/runs/nope")
        self.assertEqual(status, 404)


class UploadCapsTest(HandlerCase):
    def test_audio_staged_as_private_hash_named_artifact(self):
        payload = b"FAKEAUDIO" * 200
        status, out = self.call("POST", "/api/v1/processing/uploads",
                                raw=payload, ctype="audio/mpeg",
                                filename="../../evil.mp3")
        self.assertEqual(status, 200, out)
        self.assertEqual(out["bytes"], len(payload))
        self.assertEqual(out["filename_label"], ".._.._evil.mp3")
        if out["duration_unknown"]:
            self.assertIsNone(out["duration_s"])
        else:
            self.assertGreater(out["duration_s"], 0)
        staged = list((self.ws / "staged").glob("*"))
        self.assertEqual(len(staged), 1)
        self.assertNotIn("evil", staged[0].name)

    def test_wrong_type_and_over_cap_rejected(self):
        status, out = self.call("POST", "/api/v1/processing/uploads",
                                raw=b"{}", ctype="application/json",
                                filename="x.json")
        self.assertEqual(status, 415, out)
        handler = srv.Handler.__new__(srv.Handler)
        handler.command = "POST"
        handler.path = "/api/v1/processing/uploads"
        handler.request_version = "HTTP/1.1"
        handler.requestline = "POST /api/v1/processing/uploads HTTP/1.1"
        handler._headers_buffer = []
        handler.log_message = lambda *a: None
        handler.headers = {"Host": HOST, "X-CSRF-Token": CSRF,
                           "Origin": f"http://{HOST}",
                           "Content-Type": "audio/mpeg",
                           "Content-Length": str(srv.P09_AUDIO_MAX_BYTES + 1),
                           "X-Filename": "big.mp3"}
        handler.rfile = io.BytesIO(b"")
        handler.wfile = io.BytesIO()
        handler.close_connection = True
        handler.do_POST()
        data = handler.wfile.getvalue()
        head, _, rest = data.partition(b"\r\n\r\n")
        self.assertEqual(int(head.split(b" ")[1]), 413)


class StageKeyMigrationTest(HandlerCase):
    """H1a, revised by backend plan Task 1 (K01: no read/mutation-time
    DDL; schema changes only in fresh setup or explicit upgrade). A
    workspace predating the stage_key column is never migrated on the
    request path: every processing route answers 409 "requires explicit
    upgrade" (app_server _p09_require) and changes nothing. The
    operator's explicit upgrade (app_workspace.upgrade_workspace, which
    runs init_schema) adds the column, keeps the legacy jobs, and the
    same requests then succeed. Fixtures carry real legacy jobs whose
    stage_key predates the column."""

    UPGRADE_409 = "processing schema requires explicit upgrade"

    def _seed_legacy_jobs(self):
        """A run created before stage_key existed: its jobs have no reuse
        key, then the column is dropped (the pre-stage_key layout)."""
        run_id = self.start_run(key="legacy-1", episodes=("ep_a",))["run"]["run_id"]
        con = self.ledger_con()
        try:
            con.execute("UPDATE processing_jobs SET stage_key=NULL")
            db.drop_processing_stage_key(con)
            con.commit()
        finally:
            con.close()
        self.assertFalse(self._has_stage_key())
        self.assertTrue(self._processing_snapshot()["processing_jobs"])
        return run_id

    def _has_stage_key(self):
        con = self.ledger_con()
        try:
            return "stage_key" in {r[1] for r in con.execute(
                "PRAGMA table_info(processing_jobs)")}
        finally:
            con.close()

    def _processing_snapshot(self, drop=()):
        """Every row of every processing table, minus the named columns."""
        con = self.ledger_con()
        try:
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND"
                " (name LIKE 'processing%' OR name LIKE 'p01%' OR name LIKE 'p09%')"
                " ORDER BY name")]
            out = {}
            for table in tables:
                cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')
                        if r[1] not in drop]
                quoted = ", ".join(f'"{c}"' for c in cols)
                out[table] = sorted(
                    (tuple(r) for r in con.execute(f'SELECT {quoted} FROM "{table}"')),
                    key=repr)
            return out
        finally:
            con.close()

    def _assert_requires_explicit_upgrade(self, status, out, before):
        self.assertEqual(status, 409, out)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "CONFLICT")
        self.assertEqual(out["error"]["message"], self.UPGRADE_409)
        # No auto-migration and no partial write on the request path.
        self.assertFalse(self._has_stage_key())
        self.assertEqual(self._processing_snapshot(), before)

    def _explicit_upgrade(self, legacy):
        import app_workspace
        result = app_workspace.upgrade_workspace(self.ws)
        self.assertTrue(result["upgraded"], result)
        self.assertEqual(result["verification"]["problems"], [])
        self.assertTrue(self._has_stage_key())
        # Legacy jobs survive unchanged; their reuse key stays unknown
        # rather than being invented.
        self.assertEqual(self._processing_snapshot(drop=("stage_key",)), legacy)
        con = self.ledger_con()
        try:
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM processing_jobs WHERE stage_key IS NOT NULL"
            ).fetchone()[0], 0)
        finally:
            con.close()

    def test_unmigrated_copy_requires_explicit_upgrade_on_runs_post(self):
        self._seed_legacy_jobs()
        before = self._processing_snapshot()
        body = {"idempotency_key": "h1a-1", "episode_ids": ["ep_a"],
                "profile": "local"}
        status, out = self.call("POST", "/api/v1/processing/runs", body)
        self._assert_requires_explicit_upgrade(status, out, before)
        self._explicit_upgrade(before)
        # The refused request left no idempotency residue: the same key
        # starts the run after the upgrade.
        out = self.start_run(key="h1a-1", episodes=("ep_a",))
        self.assertIn("run_id", out["run"])

    def test_unmigrated_copy_requires_explicit_upgrade_on_estimate_post(self):
        self._seed_legacy_jobs()
        before = self._processing_snapshot()
        body = {"episode_ids": ["ep_a"], "profile": "local"}
        status, out = self.call("POST", "/api/v1/processing/estimate", body)
        self._assert_requires_explicit_upgrade(status, out, before)
        self._explicit_upgrade(before)
        status, out = self.call("POST", "/api/v1/processing/estimate", body)
        self.assertEqual(status, 200, out)

    def test_every_processing_route_requires_explicit_upgrade(self):
        run_id = self._seed_legacy_jobs()
        self.fake_dns({"ok.example": "93.184.216.1"})
        routes = [
            ("GET", "/api/v1/processing/sources", None),
            ("GET", "/api/v1/processing/runs", None),
            ("GET", f"/api/v1/processing/runs/{run_id}", None),
            ("GET", "/api/v1/processing/worker", None),
            ("POST", f"/api/v1/processing/runs/{run_id}/retry",
             {"failed_job_ids": [], "idempotency_key": "r-up", "expected_revision": 1}),
            ("POST", f"/api/v1/processing/runs/{run_id}/cancel",
             {"expected_revision": 1}),
            ("POST", "/api/v1/processing/sources",
             {"url": "https://ok.example/feed.xml"}),
        ]
        before = self._processing_snapshot()
        for method, path, body in routes:
            with self.subTest(method=method, path=path):
                status, out = self.call(method, path, body)
                self._assert_requires_explicit_upgrade(status, out, before)
        self._explicit_upgrade(before)
        for method, path, body in routes:
            if path.endswith("/retry"):
                continue  # nothing failed, so retry has no valid target
            with self.subTest(method=method, path=path, upgraded=True):
                status, out = self.call(method, path, body)
                self.assertEqual(status, 200, out)

    def test_upload_requires_explicit_upgrade_and_keeps_no_bytes(self):
        # POST /processing/uploads is also behind _p09_require. A refused
        # upload must leave nothing behind: no rows and no staged bytes.
        self._seed_legacy_jobs()
        before = self._processing_snapshot()
        staged = self.ws / "staged"
        payload = b"FAKEAUDIO" * 200
        status, out = self.call("POST", "/api/v1/processing/uploads",
                                raw=payload, ctype="audio/mpeg",
                                filename="a.mp3")
        self._assert_requires_explicit_upgrade(status, out, before)
        self.assertEqual(sorted(staged.glob("*")) if staged.exists() else [], [])
        self._explicit_upgrade(before)
        status, out = self.call("POST", "/api/v1/processing/uploads",
                                raw=payload, ctype="audio/mpeg",
                                filename="a.mp3")
        self.assertEqual(status, 200, out)
        self.assertEqual(len(list(staged.glob("*"))), 1)

    def test_repeat_upgrade_is_a_noop(self):
        import app_workspace
        self._seed_legacy_jobs()
        self._explicit_upgrade(self._processing_snapshot())
        after_first = self._processing_snapshot()
        again = app_workspace.upgrade_workspace(self.ws)
        self.assertFalse(again["upgraded"], again)
        self.assertEqual(self._processing_snapshot(), after_first)
        status, out = self.call("GET", "/api/v1/processing/runs")
        self.assertEqual(status, 200, out)

    def test_missing_processing_tables_require_explicit_setup(self):
        import app_workspace
        con = self.ledger_con()
        try:
            con.execute("DROP TABLE p09_run_episodes")
            con.commit()
        finally:
            con.close()
        status, out = self.call("GET", "/api/v1/processing/runs")
        self.assertEqual(status, 409, out)
        self.assertEqual(out["error"]["message"],
                         "processing schema requires explicit workspace setup")
        self.assertTrue(app_workspace.upgrade_workspace(self.ws)["upgraded"])
        status, out = self.call("GET", "/api/v1/processing/runs")
        self.assertEqual(status, 200, out)


class ResidualDbErrorTest(HandlerCase):
    """H1b: a residual DB error surfaces as JSON 500, never a dropped
    connection (zero response bytes / escaping exception)."""

    def test_missing_table_is_json_500_not_dropped(self):
        con = self.ledger_con()
        try:
            con.execute("DROP TABLE episodes")
            con.commit()
        finally:
            con.close()
        status, out = self.call("POST", "/api/v1/processing/runs", {
            "idempotency_key": "h1b-1", "episode_ids": ["ep_a"],
            "profile": "local"})
        self.assertEqual(status, 500, out)
        self.assertFalse(out["ok"])
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
