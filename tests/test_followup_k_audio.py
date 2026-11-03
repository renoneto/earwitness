"""Follow-up K: owner-data cutover audio regressions.

The v7->v8 rehearsal lost the retained 2026-09-24 audit trust labels on
customer surfaces, and local pilot/benchmark audio that the old app
served in place from .pilot-corpus had no registered recording.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from earwitness.app import customer, customer_contracts, db
from tests import backend_fixtures

TRUST_SEGMENTS = [
    {"start": 0.0, "end": 10.0, "text": "Welcome back to the program"},
    {"start": 10.0, "end": 20.0, "text": "Today we talk about zephyrine lamps"},
]


def add_episode(con, ep_id, segments, *, audio_url=None, title=None):
    """Archived transcript + episode row + publication, mirroring seed_source_rows."""
    tv = backend_fixtures._add_version(con, f"transcript:{ep_id}", {"segments": segments})
    con.execute(
        "INSERT INTO episodes (id, kind, show_title, ep_title, category, pub_date,"
        " fetch_date, transcript_version_id, audio_url, revision, updated_at)"
        " VALUES (?, 'supplied', 'Trust Show', ?, 'business', '2026-03-12',"
        " '2026-09-21', ?, ?, 1, ?)",
        (ep_id, title or f"Episode {ep_id}", tv, audio_url, db.utcnow()))
    return tv


def episode_items(con, q):
    items = customer.query(con, customer_contracts.normalize_query(
        {"version": 2, "mode": "episodes", "q": q}))["items"]
    return {item["episode_id"]: item for item in items}


class AuditTrustLabelTest(unittest.TestCase):
    """Remote episodes keep their retained audit verdict as the label,
    never a player or verified timing."""

    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = backend_fixtures.make_workspace(root, schema_version=8)
        self.con = db.open_existing(path, readonly=False)
        self.addCleanup(self.con.close)
        with db.transaction(self.con, write=True):
            for ep_id in ("s002e0", "s003e0", "zz-unaudited"):
                add_episode(self.con, ep_id, TRUST_SEGMENTS,
                            audio_url=f"https://audio.example-cdn.net/{ep_id}.mp3")
            backend_fixtures.publish_all_records(self.con)

    def test_search_items_carry_audit_trust_without_player(self):
        items = episode_items(self.con, "zephyrine")
        expected = {"s002e0": "rotated", "s003e0": "exact", "zz-unaudited": "unknown"}
        for ep_id, trust in expected.items():
            with self.subTest(episode=ep_id):
                self.assertIn(ep_id, items)
                self.assertEqual(items[ep_id]["audio"], "available")
                self.assertEqual(items[ep_id]["audio_trust"], trust)
                self.assertFalse(items[ep_id]["audio_local"])

    def test_media_payload_has_no_player_for_remote_audit(self):
        for ep_id in ("s002e0", "s003e0", "zz-unaudited"):
            with self.subTest(episode=ep_id):
                ep = dict(self.con.execute("SELECT * FROM episodes WHERE id=?", (ep_id,)).fetchone())
                self.assertIsNone(customer._media_payload(ep, None, self.con)["player"])

    def test_record_media_trust_follows_audit_without_local_playback(self):
        rotated = customer.record(self.con, "ep:s002e0")["media"]
        self.assertEqual(rotated["trust"], "rotated")
        self.assertNotEqual(rotated["playback"], "local")
        self.assertEqual(rotated.get("timing", "unknown"), "unknown")
        exact = customer.record(self.con, "ep:s003e0")["media"]
        self.assertEqual(exact["trust"], "exact")
        self.assertNotEqual(exact["playback"], "local")
        self.assertEqual(exact.get("timing", "unknown"), "unknown")
        self.assertEqual(customer.record(self.con, "ep:zz-unaudited")["media"]["trust"], "unknown")

    def test_mismatch_alignment_overrides_audit(self):
        self.con.execute("UPDATE episodes SET alignment='mismatch' WHERE id='s003e0'")
        ep = dict(self.con.execute("SELECT * FROM episodes WHERE id='s003e0'").fetchone())
        payload = customer._media_payload(ep, None, self.con)
        self.assertEqual(payload["trust"], "mismatch")
        self.assertIsNone(payload["player"])



# -- Legacy local audio registration during the v7 upgrade -------------------

import hashlib  # noqa: E402
import json  # noqa: E402
import importlib.util  # noqa: E402
import sqlite3  # noqa: E402
from urllib.parse import quote  # noqa: E402
import stat  # noqa: E402
from unittest import mock  # noqa: E402

from earwitness.app import citations, media  # noqa: E402

LEGACY_SEGMENTS = [
    {"start": 0.0, "end": 5.0, "text": "Welcome to the legacy pilot"},
    {"start": 5.0, "end": 12.0, "text": "This hour is sponsored by quillmark pens"},
]


def mp3_bytes(frames: int, fill: int) -> bytes:
    """Valid MPEG-1 Layer III frames (128 kbps, 44.1 kHz, 417 bytes each)."""
    return (bytes([0xFF, 0xFB, 0x90, 0x64]) + bytes([fill]) * 413) * frames


def load_workspace_cli():
    path = Path(__file__).resolve().parent.parent / "scripts" / "app_workspace.py"
    spec = importlib.util.spec_from_file_location("app_workspace_cli_k", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LegacyLocalAudioUpgradeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.cli = load_workspace_cli()
        self.enterContext(mock.patch.object(self.cli, "RECOVERY_DIR", self.tmp / "recovery"))
        self.workspace = self.tmp / "ws"
        self.db_path = backend_fixtures.make_workspace(self.workspace, schema_version=7)
        con = sqlite3.connect(self.db_path, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            con.execute("BEGIN IMMEDIATE")
            for ep_id in ("upload:pilot-acquired", "bench_3", "upload:pilot-tbpn"):
                add_episode(con, ep_id, LEGACY_SEGMENTS)
            con.commit()
        finally:
            con.close()
        self.legacy = self.tmp / "legacy-repo"
        self.sources = {
            "upload:pilot-acquired": (".pilot-corpus/acquired/latest.mp3", mp3_bytes(1200, 0)),
            "bench_3": (".pilot-corpus/bench-audio/bench_3.mp3", mp3_bytes(1300, 0x55)),
        }
        for rel, payload in self.sources.values():
            target = self.legacy / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)

    def open(self):
        con = db.open_existing(self.db_path, readonly=False)
        self.addCleanup(con.close)
        return con

    def test_upgrade_registers_legacy_local_audio_as_pinned_recordings(self):
        result = self.cli.upgrade_workspace(self.workspace, legacy_audio_root=self.legacy)
        self.assertTrue(result["upgraded"])
        receipt = result["legacy_audio"]
        self.assertEqual(receipt["legacy_root"], str(self.legacy))
        registered = {entry["episode_id"]: entry for entry in receipt["registered"]}
        self.assertEqual(set(registered), set(self.sources))
        self.assertIn("upload:pilot-tbpn", {entry["episode_id"] for entry in receipt["skipped"]})
        con = self.open()
        self.assertEqual(stat.S_IMODE((self.workspace / "media").stat().st_mode), 0o700)
        for ep_id, (rel, payload) in self.sources.items():
            with self.subTest(episode=ep_id):
                digest = hashlib.sha256(payload).hexdigest()
                entry = registered[ep_id]
                self.assertEqual((entry["source"], entry["bytes"], entry["sha256"]),
                                 (rel, len(payload), digest))
                self.assertEqual(entry["transcript_end_s"], 12.0)
                self.assertEqual((self.legacy / rel).read_bytes(), payload)
                row = con.execute("SELECT media_artifact_id, alignment FROM episodes WHERE id=?",
                                  (ep_id,)).fetchone()
                self.assertEqual(row["media_artifact_id"], entry["artifact_id"])
                self.assertEqual(row["alignment"], "verified")
                artifact = con.execute("SELECT * FROM media_artifacts WHERE id=?",
                                       (row["media_artifact_id"],)).fetchone()
                self.assertEqual((artifact["digest"], artifact["bytes"]), (digest, len(payload)))
                self.assertTrue(artifact["locator"].startswith("media/"))
                self.assertEqual(artifact["rights_state"], "unknown")
                copy = self.workspace / artifact["locator"]
                self.assertEqual(copy.read_bytes(), payload)
                self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o600)
                ref = citations.current_source_ref(con, "episode", ep_id)
                self.assertEqual(media.recording_chunk(con, ref, "bytes=0-")["payload"],
                                 payload[:1 << 20])
                self.assertEqual(media.recording_chunk(con, ref, "bytes=1000-4999")["payload"],
                                 payload[1000:5000])
                record = customer.record(con, "ep:" + ep_id)["media"]
                self.assertEqual(record["playback"], "local")
                self.assertEqual(record["trust"], "exact")
        items = episode_items(con, "quillmark")
        for ep_id in self.sources:
            with self.subTest(item=ep_id):
                self.assertTrue(items[ep_id]["audio_local"])
                self.assertEqual(items[ep_id]["audio_trust"], "exact")
        self.assertFalse(items["upload:pilot-tbpn"]["audio_local"])

    def test_episode_record_media_ref_is_served_and_ep_ref_is_refused(self):
        # Follow-up M: the episode page reads the record by `ep:<id>` and
        # must build its audio URL from a ref the media route accepts.
        self.cli.upgrade_workspace(self.workspace, legacy_audio_root=self.legacy)
        con = self.open()
        for ep_id, (_rel, payload) in self.sources.items():
            with self.subTest(episode=ep_id):
                record = customer.record_window(con, "ep:" + ep_id)
                self.assertEqual(record["ref"], "ep:" + ep_id)
                self.assertEqual(record["media"]["playback"], "local")
                ref = record["source_ref"]
                self.assertTrue(ref.startswith("sr1:"))
                self.assertEqual(ref, record["canonical_source_ref"])
                self.assertEqual(record["media"]["src"],
                                 "/api/v1/customer/records/" + quote(ref, safe="") + "/media")
                served = media.recording_chunk(con, ref, "bytes=0-")
                self.assertIn(served["status"], (200, 206))
                self.assertEqual(served["payload"], payload[:1 << 20])
                with self.assertRaises(citations.CitationError):
                    media.recording_chunk(con, "ep:" + ep_id)

    def test_upgrade_without_flag_registers_nothing(self):
        result = self.cli.upgrade_workspace(self.workspace)
        self.assertTrue(result["upgraded"])
        self.assertIsNone(result["legacy_audio"])
        con = self.open()
        self.assertEqual(con.execute("SELECT COUNT(*) FROM media_artifacts").fetchone()[0], 0)
        self.assertFalse((self.workspace / "media").exists())

    def test_current_schema_workspace_refuses_legacy_flag(self):
        current = self.tmp / "ws-current"
        backend_fixtures.make_workspace(current, schema_version=8)
        with self.assertRaisesRegex(ValueError, "register-legacy-audio command"):
            self.cli.upgrade_workspace(current, legacy_audio_root=self.legacy)

    def test_missing_legacy_root_is_refused(self):
        with self.assertRaises(ValueError):
            self.cli.upgrade_workspace(self.workspace, legacy_audio_root=self.tmp / "absent")
        con = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0], 7)
        finally:
            con.close()

    def test_cli_passes_legacy_audio_root(self):
        with mock.patch.object(self.cli, "upgrade_workspace",
                               return_value={"upgraded": True}) as upgrade, \
                mock.patch.object(self.cli, "resolve_workspace", return_value=self.workspace), \
                mock.patch("builtins.print"):
            code = self.cli.main(["--workspace", str(self.workspace), "upgrade",
                                  "--legacy-audio-root", str(self.legacy)])
        self.assertEqual(code, 0)
        self.assertEqual(upgrade.call_args.kwargs["legacy_audio_root"], str(self.legacy))


class LegacyAudioRollbackTest(unittest.TestCase):
    setUp = LegacyLocalAudioUpgradeTest.setUp
    open = LegacyLocalAudioUpgradeTest.open
    """A failure after registration rolls back the rows and deletes the copies."""

    def test_failed_upgrade_block_deletes_created_copies(self):
        from earwitness.app import citations as _citations
        with mock.patch.object(_citations, "publish_episode_sources",
                               side_effect=RuntimeError("publish failed")):
            with self.assertRaisesRegex(RuntimeError, "publish failed"):
                self.cli.upgrade_workspace(self.workspace, legacy_audio_root=self.legacy)
        self.assertEqual([p for p in (self.workspace / "media").iterdir()], [])
        con = self.open()
        self.assertEqual(con.execute("SELECT COUNT(*) FROM media_artifacts").fetchone()[0], 0)


# -- Recording proof cache: search must not re-hash/re-probe per item --------

import os  # noqa: E402
import shutil  # noqa: E402

from earwitness.pipeline import artifacts as _artifacts  # noqa: E402

MANY_SEGMENTS = [{"start": float(i), "end": float(i + 1), "text": f"quillmark pens passage {i}"}
                 for i in range(300)]


@unittest.skipUnless(shutil.which("ffprobe"), "ffprobe is required for MP3 recordings")
class RecordingProofCacheTest(unittest.TestCase):
    """One registered legacy MP3 recording with 300 matching passages."""

    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.cli = load_workspace_cli()
        self.enterContext(mock.patch.object(self.cli, "RECOVERY_DIR", self.tmp / "recovery"))
        self.workspace = self.tmp / "ws"
        self.db_path = backend_fixtures.make_workspace(self.workspace, schema_version=7)
        con = sqlite3.connect(self.db_path, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            con.execute("BEGIN IMMEDIATE")
            add_episode(con, "upload:pilot-acquired", MANY_SEGMENTS)
            con.commit()
        finally:
            con.close()
        self.legacy = self.tmp / "legacy-repo"
        source = self.legacy / ".pilot-corpus/acquired/latest.mp3"
        source.parent.mkdir(parents=True)
        source.write_bytes(mp3_bytes(12000, 0x11))  # ~313 s, ~5 MB
        result = self.cli.upgrade_workspace(self.workspace, legacy_audio_root=self.legacy)
        self.assertEqual(len(result["legacy_audio"]["registered"]), 1)
        self.con = db.open_existing(self.db_path, readonly=False)
        self.addCleanup(self.con.close)
        self.episode = dict(self.con.execute(
            "SELECT * FROM episodes WHERE id='upload:pilot-acquired'").fetchone())
        artifact = self.con.execute("SELECT locator FROM media_artifacts").fetchone()
        self.recording = self.workspace / artifact["locator"]
        media._PROOF_CACHE.clear()

    def mentions(self):
        return customer.query(self.con, customer_contracts.normalize_query(
            {"version": 2, "mode": "mentions", "q": "quillmark"}))

    def player(self):
        return customer._media_payload(self.episode, None, self.con)["player"]

    def test_repeated_payloads_and_queries_hash_the_recording_once(self):
        calls = []
        real = _artifacts.sha256_file

        def counting(path, *args, **kwargs):
            if Path(path).resolve() == self.recording:
                calls.append(path)
            return real(path, *args, **kwargs)

        with mock.patch.object(_artifacts, "sha256_file", side_effect=counting):
            for _ in range(5):
                self.assertIsNotNone(self.player())
            first, again = self.mentions(), self.mentions()
        self.assertGreaterEqual(first["total"], 300)
        self.assertTrue(all(item["audio_local"] for item in first["items"] + again["items"]))
        self.assertEqual(len(calls), 1)

    def test_changed_bytes_same_size_and_restored_mtime_fail_the_next_proof(self):
        self.assertIsNotNone(self.player())
        before = self.recording.stat()
        payload = bytearray(self.recording.read_bytes())
        payload[-1] ^= 0xFF
        self.recording.write_bytes(bytes(payload))
        os.utime(self.recording, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.recording.stat().st_size, before.st_size)
        self.assertIsNone(self.player())
        self.assertFalse(any(item["audio_local"] for item in self.mentions()["items"]))

    def test_changed_bytes_same_size_with_bumped_mtime_fail_the_next_proof(self):
        self.assertIsNotNone(self.player())
        before = self.recording.stat()
        payload = bytearray(self.recording.read_bytes())
        payload[0] ^= 0xFF
        self.recording.write_bytes(bytes(payload))
        os.utime(self.recording, ns=(before.st_atime_ns, before.st_mtime_ns + 10**9))
        self.assertIsNone(self.player())

    def test_changed_size_fails_the_next_proof(self):
        self.assertIsNotNone(self.player())
        with self.recording.open("ab") as handle:
            handle.write(mp3_bytes(1, 0x11))
        self.assertIsNone(self.player())

    def test_restored_bytes_prove_again(self):
        original = self.recording.read_bytes()
        self.assertIsNotNone(self.player())
        self.recording.write_bytes(original[:-1] + bytes([original[-1] ^ 0xFF]))
        self.assertIsNone(self.player())
        self.recording.write_bytes(original)
        self.assertIsNotNone(self.player())

    def test_search_with_hundreds_of_passages_stays_fast_after_first_call(self):
        self.assertGreaterEqual(self.mentions()["total"], 300)
        # The second identical search proves no recording again: the cost
        # is the identity memo, not a wall-clock bound a loaded machine fails.
        with mock.patch.object(media, "_recording_identity",
                               wraps=media._recording_identity) as proved:
            result = self.mentions()
        self.assertGreaterEqual(result["total"], 300)
        self.assertTrue(all(item["audio_local"] for item in result["items"]))
        self.assertEqual(proved.call_count, 0)


# -- register-legacy-audio: an upgraded v8 workspace before the conversion ---

class RegisterLegacyAudioCommandTest(unittest.TestCase):
    setUp_v7 = LegacyLocalAudioUpgradeTest.setUp
    open = LegacyLocalAudioUpgradeTest.open

    def setUp(self):
        self.setUp_v7()
        self.assertIsNone(self.cli.upgrade_workspace(self.workspace)["legacy_audio"])

    def test_registers_on_upgraded_unconverted_workspace(self):
        with mock.patch.object(self.cli, "resolve_workspace", return_value=self.workspace), \
                mock.patch("builtins.print") as printed:
            code = self.cli.main(["--workspace", "ws", "register-legacy-audio",
                                  "--legacy-audio-root", str(self.legacy)])
        self.assertEqual(code, 0)
        result = json.loads(printed.call_args.args[0])
        self.assertEqual(result["operation"], "register_legacy_audio")
        backup = Path(result["backup"])
        self.assertEqual(backup, self.workspace / "before-register-legacy-audio.sqlite3")
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        receipt = result["legacy_audio"]
        self.assertNotIn("created_files", receipt)
        self.assertEqual({entry["episode_id"] for entry in receipt["registered"]},
                         set(self.sources))
        self.assertIn("upload:pilot-tbpn", {entry["episode_id"] for entry in receipt["skipped"]})
        con = self.open()
        for ep_id, (_rel, payload) in self.sources.items():
            with self.subTest(episode=ep_id):
                self.assertEqual(customer.record(con, "ep:" + ep_id)["media"]["playback"], "local")
                ref = citations.current_source_ref(con, "episode", ep_id)
                self.assertEqual(media.recording_chunk(con, ref, "bytes=0-99")["payload"],
                                 payload[:100])
        audit = con.execute("SELECT COUNT(*) FROM audit WHERE actor='register-legacy-audio'"
                            ).fetchone()[0]
        self.assertGreater(audit, 0)
        with self.assertRaisesRegex(ValueError, "backup destination exists"):
            self.cli.register_legacy_audio(self.workspace, self.legacy)

    def test_refused_after_customer_conversion(self):
        from earwitness.app import conversion
        con = sqlite3.connect(self.db_path)
        try:
            con.execute("INSERT INTO meta (key, value) VALUES (?, '{}')", (conversion.MARKER_KEY,))
            con.commit()
        finally:
            con.close()
        with self.assertRaisesRegex(ValueError, "conversion already ran.*before-upgrade backup"):
            self.cli.register_legacy_audio(self.workspace, self.legacy)
        self.assertFalse((self.workspace / "before-register-legacy-audio.sqlite3").exists())
        self.assertFalse((self.workspace / "media").exists())

    def test_refused_without_workspace_database_or_root_directory(self):
        empty = self.tmp / "ws-empty"
        with self.assertRaisesRegex(ValueError, "workspace database does not exist"):
            self.cli.register_legacy_audio(empty, self.legacy)
        self.assertFalse(empty.exists())
        with self.assertRaisesRegex(ValueError, "not a directory"):
            self.cli.register_legacy_audio(self.workspace, self.tmp / "absent")
        stray = self.tmp / "file.mp3"
        stray.write_bytes(b"x")
        with self.assertRaisesRegex(ValueError, "not a directory"):
            self.cli.register_legacy_audio(self.workspace, stray)
        self.assertFalse((self.workspace / "before-register-legacy-audio.sqlite3").exists())

    def test_refused_on_v7_workspace(self):
        v7 = self.tmp / "ws-v7"
        backend_fixtures.make_workspace(v7, schema_version=7)
        with self.assertRaisesRegex(ValueError, "run upgrade with --legacy-audio-root"):
            self.cli.register_legacy_audio(v7, self.legacy)

    def test_failure_deletes_created_copies_and_rolls_back(self):
        real = self.cli._register_legacy_audio

        def then_fail(*args, **kwargs):
            real(*args, **kwargs)
            raise RuntimeError("late failure")

        with mock.patch.object(self.cli, "_register_legacy_audio", side_effect=then_fail):
            with self.assertRaisesRegex(RuntimeError, "late failure"):
                self.cli.register_legacy_audio(self.workspace, self.legacy)
        self.assertEqual(list((self.workspace / "media").iterdir()), [])
        con = self.open()
        self.assertEqual(con.execute("SELECT COUNT(*) FROM media_artifacts").fetchone()[0], 0)


# -- Registration sanity: the recording must cover its archived transcript ---

class LegacyAudioCoverageTest(unittest.TestCase):
    setUp_v7 = LegacyLocalAudioUpgradeTest.setUp

    def setUp(self):
        self.setUp_v7()  # both sources are ~31.3 s of MP3 frames
        con = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            con.execute("BEGIN IMMEDIATE")
            for ep_id, end in (("upload:pilot-acquired", 100.0), ("bench_3", 32.0)):
                tv = con.execute("SELECT transcript_version_id FROM episodes WHERE id=?",
                                 (ep_id,)).fetchone()[0]
                segments = LEGACY_SEGMENTS + [{"start": 12.0, "end": end, "text": "closing"}]
                con.execute("UPDATE source_versions SET payload=? WHERE id=?",
                            (json.dumps({"segments": segments}), tv))
            con.commit()
        finally:
            con.close()

    def test_recording_shorter_than_transcript_is_skipped(self):
        receipt = self.cli.upgrade_workspace(self.workspace,
                                             legacy_audio_root=self.legacy)["legacy_audio"]
        skipped = {entry["episode_id"]: entry["reason"] for entry in receipt["skipped"]}
        self.assertEqual(skipped["upload:pilot-acquired"],
                         "recording is shorter than its archived transcript")
        self.assertEqual([entry["episode_id"] for entry in receipt["registered"]], ["bench_3"])
        con = db.open_existing(self.db_path, readonly=False)
        self.addCleanup(con.close)
        row = con.execute("SELECT media_artifact_id FROM episodes"
                          " WHERE id='upload:pilot-acquired'").fetchone()
        self.assertIsNone(row["media_artifact_id"])
        acquired = hashlib.sha256(self.sources["upload:pilot-acquired"][1]).hexdigest()
        self.assertFalse((self.workspace / "media" / f"{acquired}.mp3").exists())
        self.assertEqual(con.execute("SELECT COUNT(*) FROM media_artifacts").fetchone()[0], 1)


class RecordingIdentityMemoTest(unittest.TestCase):
    """A list scan decodes each recording's transcripts once per database
    state; any write makes the next identity recompute."""

    def setUp(self):
        self.con = backend_fixtures.make_file_workspace()
        self.addCleanup(self.con.close)
        backend_fixtures.seed_source_rows(self.con)
        backend_fixtures.register_synthetic_recording(self.con, "e1")
        if self.con.in_transaction:
            self.con.commit()
        self.episode = dict(self.con.execute("SELECT * FROM episodes WHERE id='e1'").fetchone())

    def test_unchanged_database_computes_identity_once(self):
        real = media._recording_identity
        with mock.patch.object(media, "_recording_identity", side_effect=real) as counted:
            first = media.recording_identity(self.con, self.episode)
            for _ in range(20):
                self.assertEqual(media.recording_identity(self.con, self.episode), first)
                customer._media_payload(self.episode, None, self.con)
        self.assertIsNotNone(first)
        # Once for the episode, once for its proof's explicit pipeline row.
        self.assertEqual(counted.call_count, 2)

    def test_any_write_recomputes_identity(self):
        self.assertIsNotNone(media.recording_identity(self.con, self.episode))
        with self.con:
            self.con.execute("UPDATE transcript_versions SET source_offsets='{}'"
                             " WHERE episode_id='e1'")
        self.assertIsNone(media.recording_identity(self.con, self.episode))
        self.assertIsNone(customer._media_payload(self.episode, None, self.con)["player"])

    def test_other_connection_commit_recomputes_identity(self):
        import sqlite3 as _sqlite3
        self.assertIsNotNone(media.recording_identity(self.con, self.episode))
        path = self.con.execute("PRAGMA database_list").fetchone()[2]
        other = _sqlite3.connect(path)
        self.addCleanup(other.close)
        with other:
            other.execute("UPDATE transcript_versions SET source_offsets='{}' WHERE episode_id='e1'")
        self.assertIsNone(media.recording_identity(self.con, self.episode))


class RecordingIdentityMemoRollbackTest(RecordingIdentityMemoTest):
    def test_rolled_back_binding_inside_open_transaction_is_not_reused(self):
        self.con.execute("BEGIN IMMEDIATE")
        self.con.execute("SAVEPOINT k")
        self.assertIsNotNone(media.recording_identity(self.con, self.episode))
        self.con.execute("DELETE FROM transcript_versions WHERE episode_id='e1'")
        self.assertIsNone(media.recording_identity(self.con, self.episode))
        self.con.execute("ROLLBACK TO k")
        self.assertIsNotNone(media.recording_identity(self.con, self.episode))
        self.con.rollback()


if __name__ == "__main__":
    unittest.main()
