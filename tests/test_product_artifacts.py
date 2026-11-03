"""P04 media/retention lifecycle regressions (stdlib unittest).

Covers source-pinned clip extraction without truncation, offset
validation, rights/deadline gates, bounded per-chunk release with
reauthorization, the authoritative deletion ledger outside snapshot
rollback, backup manifests, and explicitly authorized purge. No real
media downloads, no retention deletion of owner state.
"""
from __future__ import annotations

import hashlib
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace

from earwitness.app import db
from earwitness.pipeline import artifacts as art

REPO = Path(__file__).resolve().parent.parent
FIXTURE_ROOT = REPO / ".muse-product-p01p04" / "artifact-fixtures"


def fresh():
    # Configured like production (explicit autocommit): transaction() then
    # really issues BEGIN IMMEDIATE/COMMIT instead of nesting savepoints
    # inside an implicit transaction the test never commits.
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON;")
    db.init_schema(con)
    art.ensure_tables(con)
    return con


def tmpdir(testcase, name: str) -> Path:
    path = FIXTURE_ROOT / name
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_file():
            child.unlink(missing_ok=True)
    return path


def register(con, media_root: Path, name="ep.mp3", size=2048,
             rights_state="permitted", retain_until=None, digest=None):
    blob = media_root / name
    seed = (name + "-content-").encode()
    blob.write_bytes((seed * (size // len(seed) + 1))[:size])
    digest = digest or hashlib.sha256(blob.read_bytes()).hexdigest()
    return art.register_media(
        con, "local", digest=digest, size_bytes=size, duration_s=300.0,
        format="mp3", locator=name, rights_state=rights_state,
        retain_until=retain_until, fetch_date="2026-09-22",
        fetch_date_source="test-fixture")


class ClipExtractionTest(unittest.TestCase):
    def fake_runner(self, commands):
        def run(cmd, **kw):
            commands.append(cmd)
            dest = Path(cmd[-1])
            dest.write_bytes(b"clip-bytes")
            return SimpleNamespace(returncode=0, stderr="")
        return run

    def test_long_ad_interval_kept_intact_with_correct_offsets(self):
        media = tmpdir(self, "clips") / "src.mp3"
        media.write_bytes(b"0" * 1024)
        out = media.parent / "clip.mp3"
        commands = []
        # A >90s ad: the historical 90s cap must NOT apply to new evidence.
        result = art.extract_clip(media, out, 10.0, 200.0, context_s=5.0,
                                  media_duration_s=600.0,
                                  runner=self.fake_runner(commands))
        self.assertEqual(result["clip_start_s"], 5.0)
        self.assertEqual(result["clip_length_s"], 200.0)
        cmd = commands[0]
        self.assertIn("-ss", cmd)
        self.assertIn("5.0", cmd)
        self.assertIn("200.0", cmd)
        self.assertNotIn("90", cmd)
        self.assertTrue(out.is_file())

    def test_offset_validation_at_container_level(self):
        media = tmpdir(self, "offsets") / "src.mp3"
        media.write_bytes(b"0" * 64)
        out = media.parent / "clip.mp3"
        commands = []
        runner = self.fake_runner(commands)
        with self.assertRaises(art.OffsetError):
            art.extract_clip(media, out, -1.0, 10.0, runner=runner)
        with self.assertRaises(art.OffsetError):
            art.extract_clip(media, out, 20.0, 20.0, runner=runner)
        with self.assertRaises(art.OffsetError):
            art.extract_clip(media, out, 10.0, 500.0, media_duration_s=300.0,
                             runner=runner)
        with self.assertRaises(art.OffsetError):
            art.extract_clip(media, out, 400.0, 410.0, media_duration_s=300.0,
                             runner=runner)
        self.assertEqual(commands, [])

    def test_failed_clip_does_not_delete_required_media(self):
        media = tmpdir(self, "clipfail") / "src.mp3"
        media.write_bytes(b"0" * 64)
        out = media.parent / "clip.mp3"

        def bad_runner(cmd, **kw):
            return SimpleNamespace(returncode=1, stderr="boom")
        with self.assertRaises(art.ClipFailed):
            art.extract_clip(media, out, 1.0, 5.0, runner=bad_runner)
        self.assertTrue(media.is_file())


class RightsGateTest(unittest.TestCase):
    def test_gates_at_enqueue_execution_playback_export_delete(self):
        con = fresh()
        media_root = tmpdir(self, "rights")
        row = register(con, media_root)
        for op in ("enqueue", "execute", "playback", "export", "delete"):
            art.check_rights(con, row["id"], op)  # permitted: no raise.

    def test_expired_source_stops_stages_with_source_expired(self):
        con = fresh()
        media_root = tmpdir(self, "expired")
        row = register(con, media_root, name="old.mp3",
                       retain_until="2000-01-01T00:00:00+00:00")
        with self.assertRaises(art.SourceExpired):
            art.check_rights(con, row["id"], "execute")

    def test_unknown_rights_denies_export_and_share(self):
        con = fresh()
        media_root = tmpdir(self, "unknown")
        row = register(con, media_root, name="u.mp3", rights_state="unknown")
        art.check_rights(con, row["id"], "playback")
        with self.assertRaises(art.RightsDenied):
            art.check_rights(con, row["id"], "export")

    def test_erasure_overrides_holds(self):
        con = fresh()
        media_root = tmpdir(self, "override")
        recovery = tmpdir(self, "recovery-override")
        row = register(con, media_root, name="e.mp3")
        art.acquire_hold(con, row["id"], "job-1", "transcription pending")
        ledger = art.ledger_connect(recovery, "local")
        art.record_erasure(ledger, "local", row["id"], "rights withdrawn", "op")
        with self.assertRaises(art.RightsDenied):
            art.check_rights(con, row["id"], "playback",
                             ledger=ledger, workspace_id="local")


class BoundedReleaseTest(unittest.TestCase):
    def test_chunks_reauthorized_and_range_headers_accurate(self):
        media_root = tmpdir(self, "release")
        blob = media_root / "big.bin"
        blob.write_bytes(b"x" * (3 << 20))
        total = blob.stat().st_size
        calls = []

        def authorize(offset, length):
            calls.append((offset, length))

        data = b"".join(art.bounded_release(blob, 0, total, authorize))
        self.assertEqual(len(data), total)
        self.assertEqual(calls, [(0, 1 << 20), (1 << 20, 1 << 20), (2 << 20, 1 << 20)])
        header = art.content_range_header(1 << 20, 2 << 20, total)
        self.assertEqual(header, f"bytes {1 << 20}-{2 * (1 << 20) - 1}/{total}")

    def test_mid_transfer_revocation_is_incomplete_never_clean(self):
        media_root = tmpdir(self, "revoked")
        blob = media_root / "big.bin"
        blob.write_bytes(b"y" * (2 << 20))

        def authorize(offset, length):
            if offset > 0:
                raise art.Revoked("revoked during transfer")

        stream = art.bounded_release(blob, 0, blob.stat().st_size, authorize)
        first = next(stream)
        self.assertEqual(len(first), 1 << 20)
        with self.assertRaises(art.Revoked):
            list(stream)

    def test_overlong_chunk_refused_not_trimmed(self):
        media_root = tmpdir(self, "chunkcap")
        blob = media_root / "b.bin"
        blob.write_bytes(b"z" * 16)
        with self.assertRaises(art.ArtifactError):
            art.read_chunk(blob, 0, art.CHUNK_BYTES + 1)


class DeletionLedgerTest(unittest.TestCase):
    def test_erasure_denies_old_links_and_replays_into_workspace(self):
        con = fresh()
        media_root = tmpdir(self, "ledger")
        recovery = tmpdir(self, "recovery")
        row = register(con, media_root, name="gone.mp3")
        ledger = art.ledger_connect(recovery, "local")
        art.record_erasure(ledger, "local", row["id"], "owner request", "owner-1")
        self.assertTrue(art.is_erased(ledger, "local", row["id"]))
        with self.assertRaises(art.RightsDenied):
            art.check_rights(con, row["id"], "playback",
                             ledger=ledger, workspace_id="local")
        applied = art.apply_ledger_to_workspace(con, ledger, "local")
        self.assertIn(row["id"], applied["applied"])
        # Even without the ledger handle, the replayed state denies release.
        with self.assertRaises(art.RightsDenied):
            art.check_rights(con, row["id"], "playback")

    def test_restore_without_current_authority_stays_quarantined(self):
        with self.assertRaises(art.Quarantined):
            art.restore_with_ledger("ws.sqlite3", "backup.sqlite3", None, "local")


class ManifestPurgeTest(unittest.TestCase):
    def test_missing_permitted_artifacts_explicit_never_silent(self):
        con = fresh()
        media_root = tmpdir(self, "manifest")
        present = register(con, media_root, name="here.mp3")
        absent = register(con, media_root, name="gone.mp3")
        (media_root / "gone.mp3").unlink()
        manifest = art.backup_manifest(con, "local", media_root)
        self.assertFalse(manifest["complete"])
        self.assertIn(absent["id"], manifest["missing"])
        self.assertNotIn(present["id"], manifest["missing"])
        check = art.verify_manifest(manifest, media_root)
        self.assertFalse(check["complete"])

    def test_purge_preview_and_explicit_authorization(self):
        con = fresh()
        media_root = tmpdir(self, "purge")
        recovery = tmpdir(self, "recovery-purge")
        held = register(con, media_root, name="held.mp3")
        art.acquire_hold(con, held["id"], "job-9", "clip pending")
        doomed = register(con, media_root, name="doomed.mp3",
                          rights_state="expired")
        kept = register(con, media_root, name="kept.mp3")
        ledger = art.ledger_connect(recovery, "local")
        preview = art.purge_preview(con, ledger, "local", media_root)
        self.assertIn(doomed["id"], preview["eligible"])
        self.assertIn(held["id"], preview["held"])
        self.assertIn(kept["id"], preview["retained"])
        # purge refuses inside an open outer transaction (artifacts.purge:
        # "irreversible purge requires its own outer commit boundary").
        con.execute("BEGIN")
        self.assertTrue(con.in_transaction)
        with self.assertRaisesRegex(art.ArtifactError, "outer commit boundary"):
            art.purge(con, ledger, "local", media_root, [doomed["id"]],
                      {"authorized": True, "operator": "op", "reason": "expired"})
        self.assertTrue((media_root / "doomed.mp3").exists())
        con.commit()
        with self.assertRaises(art.UnauthorizedPurge):
            art.purge(con, ledger, "local", media_root, [doomed["id"]], {})
        with self.assertRaises(art.UnauthorizedPurge):
            art.purge(con, ledger, "local", media_root, [held["id"]],
                      {"authorized": True, "operator": "op", "reason": "x"})
        result = art.purge(con, ledger, "local", media_root, [doomed["id"]],
                           {"authorized": True, "operator": "op", "reason": "expired"})
        self.assertEqual(result["purged"], [doomed["id"]])
        self.assertFalse((media_root / "doomed.mp3").exists())
        with self.assertRaises(KeyError):
            art.get_media(con, doomed["id"])
        # Empty selection purges nothing; there is no purge-everything mode.
        self.assertEqual(art.purge(con, ledger, "local", media_root, [],
                                   {"authorized": True, "operator": "op",
                                    "reason": "x"})["purged"], [])


if __name__ == "__main__":
    unittest.main()
