"""S3: exact-transcript alias episodes share the target's remote enclosure URL.

bench_0 is byte-identical to s000e0, whose supplied metadata carries the
enclosure URL. The bench_0 episode payload must stream that remote
URL (browser-only; the server never fetches it). Unaliased benchmark
episodes keep audio_url None: uncertain stays uncertain.
No mocks and no supplied corpus: the real seed plan runs over a minimal
owned synthetic importer root built in setUpClass.

Serve-time fallback: rows seeded before the seed-time inheritance (live
workspaces with bench_0 audio_url null) resolve the target stream in the
episode-detail path when the alias row itself carries no audio.
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO))

import app_server as srv
from earwitness.app import db
from earwitness.app import import_sources as src
from earwitness.app import media as media_mod

TARGET_URL = "https://audio.example-cdn.net/ep123.mp3?source=feed"


def _synthetic_alias_root(root: Path) -> Path:
    """Minimal owned importer root: one supplied episode with an
    enclosure URL, one byte-identical transcript (alias) and one
    different transcript (unresolved benchmark)."""
    shared = [{"start": 0.0, "end": 12.0, "text": "Lumen keeps us on"},
              {"start": 12.0, "end": 24.0, "text": "the air tonight"}]
    other = [{"start": 0.0, "end": 12.0, "text": "A different show"},
             {"start": 12.0, "end": 24.0, "text": "with other words"}]
    (root / "data").mkdir(parents=True)
    (root / "transcripts").mkdir(parents=True)
    (root / "data/episodes.json").write_text(json.dumps([{
        "ep_id": "s000e0", "show_title": "Synthetic Show",
        "ep_title": "Synthetic Ep", "pub_date": "2026-09-21",
        "audio_url": TARGET_URL, "categories": ["News"]}]))
    (root / "transcripts/s000e0.json").write_text(
        json.dumps({"segments": shared}))
    (root / "transcripts/bench_0.json").write_text(
        json.dumps({"segments": shared}))
    (root / "transcripts/bench_1.json").write_text(
        json.dumps({"segments": other}))
    jev_dir = root / "experiments/jev-ad-triage-20260921"
    det_dir = root / "experiments/next-validation-detector/output"
    jev_dir.mkdir(parents=True)
    det_dir.mkdir(parents=True)
    (jev_dir / "results.json").write_text(json.dumps({"episodes": []}))
    (det_dir / "candidates_normalized.json").write_text("[]")
    return root


class AliasAudioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="alias-audio-root-")
        cls.plan = src._build_seed_plan(
            _synthetic_alias_root(Path(cls._tmp.name)))
        cls.by_id = {e["id"]: e for e in cls.plan["episodes"]}

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_bench_0_inherits_s000e0_enclosure_url(self):
        bench = self.by_id["bench_0"]
        target = self.by_id["s000e0"]
        self.assertEqual(bench["alias_of"], "s000e0")
        self.assertEqual(bench["kind"], "supplied-alias")
        self.assertIsNotNone(target["audio_url"])
        self.assertTrue(target["audio_url"].startswith("https://"))
        self.assertEqual(bench["audio_url"], target["audio_url"])

    def test_bench_0_media_descriptor_streams_remote(self):
        media = media_mod.media_for_episode(self.by_id["bench_0"])
        self.assertIsNotNone(media)
        self.assertEqual(media["kind"], "remote_full")
        self.assertEqual(media["src"], self.by_id["s000e0"]["audio_url"])

    def test_unaliased_benchmark_stays_without_audio(self):
        bench = self.by_id["bench_1"]
        self.assertIsNone(bench.get("alias_of"))
        self.assertEqual(bench["kind"], "benchmark-unresolved")
        self.assertIsNone(bench["audio_url"])
        self.assertIsNone(media_mod.media_for_episode(bench))


class ServeAliasFallbackTest(unittest.TestCase):
    """Episode-detail path on a pre-inheritance workspace: the alias row
    itself has audio_url null (as live bench_0 rows do today)."""

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="alias-audio-ws-"))
        con = db.connect(self.ws)
        try:
            db.init_schema(con)
            con.execute(
                "INSERT INTO episodes (id, kind, show_title, ep_title,"
                " audio_url, updated_at) VALUES"
                " ('s000e0', 'supplied', 'Show', 'Ep', ?, '2026-09-23T00:00:00+00:00')",
                (TARGET_URL,))
            con.execute(
                "INSERT INTO episodes (id, kind, alias_of, show_title, ep_title,"
                " audio_url, updated_at) VALUES"
                " ('bench_0', 'supplied-alias', 's000e0', 'Show', 'Ep', NULL,"
                " '2026-09-23T00:00:00+00:00')")
            con.execute(
                "INSERT INTO episodes (id, kind, show_title, ep_title,"
                " audio_url, updated_at) VALUES"
                " ('bench_1', 'benchmark-unresolved', 'Show', 'Ep', NULL,"
                " '2026-09-23T00:00:00+00:00')")
            con.execute(
                "INSERT INTO episodes (id, kind, alias_of, show_title, ep_title,"
                " audio_url, updated_at) VALUES"
                " ('bench_9', 'supplied-alias', 'missing-target', 'Show', 'Ep', NULL,"
                " '2026-09-23T00:00:00+00:00')")
            con.commit()
        finally:
            con.close()
        # Hermetic repo without bench/pilot scratch: the "without audio"
        # assertions must not depend on ambient git-ignored
        # .pilot-corpus files (B1 maps bench_1..4 to local files when
        # present; absence keeps the unavailable path).
        self.stub_repo = Path(tempfile.mkdtemp(prefix="alias-audio-repo-"))
        self._prev_repo = srv.REPO
        self._prev_ws = srv._state["workspace"]
        self._prev_port = srv._state["port"]
        srv.REPO = self.stub_repo
        srv._state["workspace"] = self.ws
        srv._state["port"] = 18831

    def tearDown(self):
        srv.REPO = self._prev_repo
        srv._state["workspace"] = self._prev_ws
        srv._state["port"] = self._prev_port
        shutil.rmtree(self.ws, ignore_errors=True)
        shutil.rmtree(self.stub_repo, ignore_errors=True)

    def raw_get(self, path):
        handler = srv.Handler.__new__(srv.Handler)
        handler.command = "GET"
        handler.path = path
        handler.request_version = "HTTP/1.1"
        handler.requestline = f"GET {path} HTTP/1.1"
        handler.log_message = lambda *a: None
        handler.headers = {"Host": "127.0.0.1:18831", "Content-Length": "0"}
        handler.rfile = io.BytesIO(b"")
        handler.wfile = io.BytesIO()
        handler.close_connection = True
        handler.do_GET()
        data = handler.wfile.getvalue()
        _, _, body = data.partition(b"\r\n\r\n")
        status = int(data.split(b"\r\n")[0].split(b" ")[1])
        return status, json.loads(body)

    def test_aliased_episode_with_null_own_audio_serves_target_stream(self):
        status, body = self.raw_get("/api/v1/episodes/bench_0")
        self.assertEqual(status, 200)
        ep = body["episode"]
        self.assertEqual(ep["alias_of"], "s000e0")
        self.assertEqual(ep["audio_url"], TARGET_URL)
        self.assertEqual(ep["media"]["kind"], "remote_full")
        self.assertEqual(ep["media"]["src"], TARGET_URL)

    def test_target_episode_unchanged(self):
        status, body = self.raw_get("/api/v1/episodes/s000e0")
        self.assertEqual(status, 200)
        ep = body["episode"]
        self.assertIsNone(ep["alias_of"])
        self.assertEqual(ep["audio_url"], TARGET_URL)

    def test_non_alias_without_audio_unchanged(self):
        status, body = self.raw_get("/api/v1/episodes/bench_1")
        self.assertEqual(status, 200)
        ep = body["episode"]
        self.assertIsNone(ep["audio_url"])
        self.assertIsNone(ep["media"])

    def test_alias_with_missing_target_stays_without_audio(self):
        status, body = self.raw_get("/api/v1/episodes/bench_9")
        self.assertEqual(status, 200)
        ep = body["episode"]
        self.assertIsNone(ep["audio_url"])
        self.assertIsNone(ep["media"])


class SeedOverrunAttestationTest(unittest.TestCase):
    """Seed coverage stays inside the requested interval.

    Supplied metadata rounds durations to one-decimal minutes while a
    transcript segment can end slightly past that mark. Before: seed
    recorded successful (0, extent) against requested (0, duration)
    and record_attestation rejected the overrun, failing the whole
    documented seed. After: success is bounded to the request; source
    content, pins, and duration_min are retained unchanged."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="seed-overrun-repo-"))
        (self.repo / "data").mkdir()
        (self.repo / "transcripts").mkdir()
        jev_dir = self.repo / "experiments/jev-ad-triage-20260921"
        det_dir = (self.repo
                   / "experiments/next-validation-detector/output")
        jev_dir.mkdir(parents=True)
        det_dir.mkdir(parents=True)
        (self.repo / "data/episodes.json").write_text(json.dumps([{
            "ep_id": "tiny-e0", "show_title": "Tiny Show",
            "ep_title": "Tiny Ep", "duration_min": 22.8}]))
        (self.repo / "transcripts/tiny-e0.json").write_text(json.dumps({
            "segments": [{"text": "hello world", "start": 0.0,
                          "end": 1368.8}]}))
        (jev_dir / "results.json").write_text(json.dumps({}))
        (det_dir / "candidates_normalized.json").write_text(json.dumps([{
            "candidate_id": "c1", "ep_id": "tiny-e0", "start_s": 10.0,
            "end_s": 20.0, "excerpt": "hello"}]))
        self.ws = Path(tempfile.mkdtemp(prefix="seed-overrun-ws-"))

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_seed_bounds_success_to_known_duration(self):
        con = db.connect(self.ws)
        try:
            db.init_schema(con)
            report = src.seed_all(self.repo, con, "test")
        finally:
            con.close()
        self.assertGreater(report["episodes"], 0)
        con = db.connect(self.ws)
        try:
            dur = con.execute("SELECT duration_min FROM episodes"
                              " WHERE id='tiny-e0'").fetchone()[0]
            self.assertEqual(dur, 22.8)
            payload = con.execute(
                "SELECT payload FROM source_versions WHERE source_key=?"
                " AND is_active=1", ("transcript:tiny-e0",)).fetchone()[0]
            segments = json.loads(payload)["segments"]
            self.assertEqual(segments[0]["end"], 1368.8)
            rows = con.execute(
                "SELECT stage, req_start_s, req_end_s, succ_start_s,"
                " succ_end_s, source_pin FROM coverage_attestations"
                " WHERE episode_id='tiny-e0'").fetchall()
        finally:
            con.close()
        stages = {r[0]: r for r in rows}
        self.assertEqual(set(stages), {"transcribe", "screen"})
        for stage, row in stages.items():
            _req_start, req_end, _succ_start, succ_end, _pin = row[1:]
            self.assertEqual(req_end, 22.8 * 60.0)
            self.assertLessEqual(succ_end, req_end)
            self.assertGreater(succ_end, 0)


if __name__ == "__main__":
    unittest.main()
