"""B1: benchmark episode bench_3 plays local .pilot-corpus audio.

Mapping: episode bench_3 -> /media/bench_3, served in place from
.pilot-corpus/bench-audio/bench_3.mp3 with byte-range support.
Explicit allowlist (content-verified only: bench_1/2/4 title-matches
proved to be wrong episode cuts, so they stay unavailable), no
traversal; missing file keeps the existing unavailable path. All
other episodes keep byte-identical behavior.
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
from earwitness.app import db, media as media_mod

PORT = 18794
HOST = f"127.0.0.1:{PORT}"
CSRF = "test-bench-audio-csrf"

STUB_AUDIO = b"ID3" + bytes(range(256)) * 16  # 4099 fake MP3 bytes

REMOTE_URL = "https://audio.example-cdn.net/ep123.mp3?source=feed"


def seed_bench_episode(con, ep_id, audio_url=None):
    con.execute(
        "INSERT OR IGNORE INTO episodes (id, kind, show_title, ep_title,"
        " audio_url, fetch_date, updated_at)"
        " VALUES (?, 'benchmark-unresolved', 'Bench Show', ?, ?, '2026-09-23',"
        " '2026-09-23T00:00:00+00:00')",
        (ep_id, f"Test {ep_id}", audio_url))
    con.commit()


class BenchAudioTest(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="bench-audio-ws-"))
        con = db.connect(self.ws)
        try:
            db.init_schema(con)
        finally:
            con.close()
        # Stub repo carrying the content-verified bench_3 scratch file;
        # bench_1/2/4 are deliberately unmapped (wrong-content title
        # matches), bench_0 was never mapped: all stay unavailable.
        self.stub_repo = Path(tempfile.mkdtemp(prefix="bench-audio-repo-"))
        audio_dir = self.stub_repo / ".pilot-corpus" / "bench-audio"
        audio_dir.mkdir(parents=True)
        (audio_dir / "bench_3.mp3").write_bytes(STUB_AUDIO)
        self._prev_repo = srv.REPO
        self._prev_ws = srv._state["workspace"]
        self._prev_port = srv._state["port"]
        self._prev_csrf = srv._state["csrf"]
        srv.REPO = self.stub_repo
        srv._state["workspace"] = self.ws
        srv._state["port"] = PORT
        srv._state["csrf"] = CSRF

    def tearDown(self):
        srv.REPO = self._prev_repo
        srv._state["workspace"] = self._prev_ws
        srv._state["port"] = self._prev_port
        srv._state["csrf"] = self._prev_csrf
        shutil.rmtree(self.ws, ignore_errors=True)
        shutil.rmtree(self.stub_repo, ignore_errors=True)

    def raw_get(self, path, extra_headers=None):
        handler = srv.Handler.__new__(srv.Handler)
        handler.command = "GET"
        handler.path = path
        handler.request_version = "HTTP/1.1"
        handler.requestline = f"GET {path} HTTP/1.1"
        handler.log_message = lambda *a: None
        headers = {"Host": HOST, "Content-Length": "0"}
        headers.update(extra_headers or {})
        handler.headers = headers
        handler.rfile = io.BytesIO(b"")
        handler.wfile = io.BytesIO()
        handler.close_connection = True
        handler.do_GET()
        data = handler.wfile.getvalue()
        head, _, body = data.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        status = int(lines[0].split(b" ")[1])
        parsed = {}
        for line in lines[1:]:
            key, _, value = line.partition(b":")
            parsed[key.decode().strip().lower()] = value.decode().strip()
        return status, parsed, body

    # -- mapping ------------------------------------------------------
    def test_bench_media_id_allowlist(self):
        self.assertEqual(media_mod.bench_media_id("bench_3"), "bench_3")
        for ep in ("bench_0", "bench_1", "bench_2", "bench_4", "bench_5",
                   "bench_9", "s000e0", "upload:pilot-acquired",
                   "bench_1/../x", "bench-1", "bench-3", None):
            self.assertIsNone(media_mod.bench_media_id(ep))

    def test_resolve_bench_present_and_missing(self):
        spec = media_mod.resolve(self.stub_repo, "bench_3")
        self.assertIsNotNone(spec)
        self.assertEqual(spec["size"], len(STUB_AUDIO))
        self.assertEqual(spec["mime"], "audio/mpeg")
        # Deliberately unmapped ids never resolve even with a scratch file.
        for ep in ("bench_1", "bench_2", "bench_4"):
            (self.stub_repo / ".pilot-corpus" / "bench-audio" / f"{ep}.mp3").write_bytes(STUB_AUDIO)
            self.assertIsNone(media_mod.resolve(self.stub_repo, ep))
        # Non-allowlisted ids never resolve as bench audio.
        self.assertIsNone(media_mod.resolve(self.stub_repo, "bench_0"))
        self.assertIsNone(media_mod.resolve(self.stub_repo, "bench_9"))
        self.assertIsNone(media_mod.resolve(self.stub_repo, "bench_1/../x"))

    # -- episode payload ----------------------------------------------
    def test_bench_episode_payload_carries_playable_audio_url(self):
        con = db.connect(self.ws)
        try:
            seed_bench_episode(con, "bench_3")
        finally:
            con.close()
        status, _, body = self.raw_get("/api/v1/episodes/bench_3")
        self.assertEqual(status, 200)
        ep = json.loads(body)["episode"]
        self.assertEqual(ep["audio_url"], "/media/bench_3")
        self.assertEqual(ep["media"]["kind"], "local_full")
        self.assertEqual(ep["media"]["src"], "/media/bench_3")

    def test_unmapped_bench_stays_unavailable(self):
        con = db.connect(self.ws)
        try:
            seed_bench_episode(con, "bench_1")
            seed_bench_episode(con, "bench_2")
        finally:
            con.close()
        for ep_id in ("bench_1", "bench_2"):
            status, _, body = self.raw_get(f"/api/v1/episodes/{ep_id}")
            self.assertEqual(status, 200)
            ep = json.loads(body)["episode"]
            self.assertIsNone(ep["audio_url"])
            self.assertIsNone(ep["media"])

    def test_non_bench_episodes_unchanged(self):
        con = db.connect(self.ws)
        try:
            seed_bench_episode(con, "bench_0", REMOTE_URL)
            seed_bench_episode(con, "s000e0")
        finally:
            con.close()
        status, _, body = self.raw_get("/api/v1/episodes/bench_0")
        self.assertEqual(status, 200)
        ep = json.loads(body)["episode"]
        self.assertEqual(ep["audio_url"], REMOTE_URL)
        self.assertEqual(ep["media"]["kind"], "remote_full")
        self.assertEqual(ep["media"]["src"], REMOTE_URL)
        status, _, body = self.raw_get("/api/v1/episodes/s000e0")
        self.assertEqual(status, 200)
        ep = json.loads(body)["episode"]
        self.assertIsNone(ep["audio_url"])
        self.assertIsNone(ep["media"])

    # -- media bytes + ranges ------------------------------------------
    def test_bench_media_full_and_byte_range(self):
        status, headers, body = self.raw_get("/media/bench_3")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("content-type"), "audio/mpeg")
        self.assertEqual(body, STUB_AUDIO)
        status, headers, body = self.raw_get(
            "/media/bench_3", {"Range": "bytes=0-99"})
        self.assertEqual(status, 206)
        self.assertEqual(headers.get("content-range"),
                         f"bytes 0-99/{len(STUB_AUDIO)}")
        self.assertEqual(body, STUB_AUDIO[:100])

    def test_unknown_bench_media_is_404(self):
        status, _, _ = self.raw_get("/media/bench_0")
        self.assertEqual(status, 404)
        status, _, _ = self.raw_get("/media/bench_1")
        self.assertEqual(status, 404)
        status, _, _ = self.raw_get("/media/bench_2")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
