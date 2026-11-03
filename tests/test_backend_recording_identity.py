"""File-backed recording identity, offsets, rights and historical source behavior."""
import hashlib
import json
import tempfile
import unittest
import wave
from pathlib import Path

from earwitness.app import campaigns, citations, db, ops
from earwitness.pipeline import artifacts
from tests import backend_fixtures


class RecordingIdentityRegression(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = backend_fixtures.make_workspace(self.root, schema_version=8)
        self.con = db.open_existing(path, readonly=False)
        self.addCleanup(self.con.close)
        artifacts.ensure_tables(self.con)
        self.audio = self.root / "fixture.wav"
        with wave.open(str(self.audio), "wb") as recording:
            recording.setnchannels(1)
            recording.setsampwidth(2)
            recording.setframerate(1000)
            recording.writeframes(bytes(range(256)) * 234 + bytes(range(96)))
        raw = self.audio.read_bytes()
        self.artifact = artifacts.register_media(self.con,
            digest=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw), duration_s=30,
            format="wav", locator="fixture.wav", fetch_date="2026-10-06",
            fetch_date_source="synthetic-fixture")
        self.con.execute("INSERT INTO transcript_versions(episode_id,media_artifact_id,segments,"
                         "source_offsets,created_at) VALUES ('e1',?,?,?,?)",
                         (self.artifact["id"], json.dumps(backend_fixtures.SEGMENTS_E1),
                          json.dumps({"global_start_s": 0}), db.utcnow()))
        self.con.execute("UPDATE episodes SET duration_min=0.5,revision=revision+1 WHERE id='e1'")
        db.record_episode_snapshot(self.con, "e1", "fixture")

    def bind(self):
        return ops.set_episode_media(self.con, "e1",
            {"media_artifact_id": self.artifact["id"], "alignment": "verified"}, 2)

    def test_matching_recording_qualifies_timing_and_changed_bytes_do_not(self):
        self.bind()
        ops.save_review(self.con, "fixture-ad", {"judgment": "ad", "eff_start_s": 10,
                        "eff_end_s": 20, "bounds_verified": True}, 1)
        ref = citations.current_source_ref(self.con, "evidence", "fixture-ad")
        pinned = citations.resolve_source_state(self.con, ref)
        self.assertEqual(pinned["media"]["recording_digest"], self.artifact["digest"])
        occurrence = campaigns._occurrence_from_evidence(self.con, "fixture-ad", "Lumen", ref)
        self.assertTrue(occurrence["timing_verified"])
        self.assertEqual(occurrence["verified_start_s"], 10)
        self.audio.write_bytes(b"not the pinned recording")
        after = citations.resolve_source_state(self.con, ref)
        self.assertEqual(after["text"], pinned["text"])
        self.assertEqual(after["media"]["playback"], "unavailable")
        self.assertFalse(campaigns._occurrence_from_evidence(
            self.con, "fixture-ad", "Lumen", ref)["timing_verified"])

    def test_foreign_pipeline_ownership_cannot_bind_recording(self):
        self.con.execute("UPDATE transcript_versions SET episode_id='e2'")
        before = tuple(self.con.execute("SELECT revision,media_artifact_id FROM episodes WHERE id='e1'").fetchone())
        with self.assertRaises(ops.BadInput):
            self.bind()
        self.assertEqual(tuple(self.con.execute(
            "SELECT revision,media_artifact_id FROM episodes WHERE id='e1'").fetchone()), before)

    def test_missing_offset_does_not_invent_zero(self):
        self.con.execute("UPDATE transcript_versions SET source_offsets='{}'")
        with self.assertRaises(ops.BadInput):
            self.bind()
        self.assertIsNone(self.con.execute("SELECT media_artifact_id FROM episodes WHERE id='e1'").fetchone()[0])

    def test_rights_expiry_removes_playback_not_historical_text(self):
        self.bind()
        ref = citations.current_source_ref(self.con, "episode", "e1")
        before = citations.resolve_source_state(self.con, ref)
        self.con.execute("UPDATE media_artifacts SET rights_state='expired' WHERE id=?", (self.artifact["id"],))
        after = citations.resolve_source_state(self.con, ref)
        self.assertEqual(after["text"], before["text"])
        self.assertEqual(after["media"]["playback"], "unavailable")

    def test_range_capture_and_fresh_rights_admission_use_distinct_snapshots(self):
        from earwitness.app import access, media
        self.bind()
        ref = citations.current_source_ref(self.con, "episode", "e1")
        ctx = access.AccessContext.create(principal_id="owner", principal_kind="owner",
            workspace_id="local", credential_kind="local_operator")
        with db.transaction(self.con, write=False):
            captured = media.recording_chunk(self.con, ref, "bytes=44-51", ctx=ctx)
            self.assertEqual(captured["payload"], bytes(range(8)))
            self.assertEqual(captured["content_range"], "bytes 44-51/60044")
            writer = db.open_existing(self.root / "workspace.sqlite3", readonly=False)
            try:
                with db.transaction(writer, write=True):
                    writer.execute("UPDATE media_artifacts SET rights_state='expired' WHERE id=?",
                                   (self.artifact["id"],))
            finally:
                writer.close()
            self.assertEqual(self.con.execute("SELECT rights_state FROM media_artifacts WHERE id=?",
                             (self.artifact["id"],)).fetchone()[0], "permitted")
            fresh = db.open_existing(self.root / "workspace.sqlite3", readonly=True)
            try:
                with db.transaction(fresh, write=False):
                    with self.assertRaises(access.AuthorizationError):
                        ctx.authorize_recording_captures(fresh)
            finally:
                fresh.close()

    def test_recorded_duration_cannot_replace_container_duration(self):
        self.con.execute("UPDATE media_artifacts SET duration_s=300")
        self.con.execute("UPDATE episodes SET duration_min=5,revision=revision+1 WHERE id='e1'")
        db.record_episode_snapshot(self.con, "e1", "fixture")
        ops.set_episode_media(self.con, "e1",
            {"media_artifact_id": self.artifact["id"], "alignment": "verified"}, 3)
        ops.save_review(self.con, "fixture-ad", {"judgment": "ad", "eff_start_s": 10,
                        "eff_end_s": 20, "bounds_verified": True}, 1)
        ref = citations.current_source_ref(self.con, "evidence", "fixture-ad")
        source = citations.resolve_source_state(self.con, ref)
        self.assertEqual(source["media"]["timing"], "unknown")
        self.assertIsNone(source["media"]["duration_s"])
        self.assertFalse(campaigns._occurrence_from_evidence(
            self.con, "fixture-ad", "Lumen", ref)["timing_verified"])

    def test_current_mismatch_does_not_restore_historical_timing_credit(self):
        from earwitness.app import media
        self.bind()
        ops.save_review(self.con, "fixture-ad", {"judgment": "ad", "eff_start_s": 10,
                        "eff_end_s": 20, "bounds_verified": True}, 1)
        ref = citations.current_source_ref(self.con, "evidence", "fixture-ad")
        ops.set_episode_media(self.con, "e1", {"alignment": "mismatch"}, 3)
        old = citations.resolve_source_state(self.con, ref)
        self.assertEqual(old["episode"]["public"]["alignment"], "verified")
        self.assertEqual(old["media"]["playback"], "unavailable")
        self.assertFalse(campaigns._occurrence_from_evidence(
            self.con, "fixture-ad", "Lumen", ref)["timing_verified"])
        with self.assertRaises(citations.RevisionUnavailable):
            media.recording_chunk(self.con, ref, "bytes=0-11")

    def test_missing_pinned_pipeline_component_does_not_select_identical_newer_row(self):
        self.bind()
        ref = citations.current_source_ref(self.con, "episode", "e1")
        before = citations.resolve_source_state(self.con, ref)
        pipeline_id = before["pins"]["recording"]["pipeline_transcript_version_id"]
        self.con.execute("INSERT INTO transcript_versions(episode_id,media_artifact_id,segments,"
                         "source_offsets,created_at) VALUES ('e1',?,?,?,?)",
                         (self.artifact["id"], json.dumps(backend_fixtures.SEGMENTS_E1),
                          json.dumps({"global_start_s": 0}), db.utcnow()))
        self.con.execute("DELETE FROM transcript_versions WHERE id=?", (pipeline_id,))
        after = citations.resolve_source_state(self.con, ref)
        self.assertEqual(after["text"], before["text"])
        self.assertEqual(after["media"]["playback"], "unavailable")

    def test_partial_recording_keeps_global_offset_without_proving_full_episode_timing(self):
        with wave.open(str(self.audio), "wb") as recording:
            recording.setnchannels(1)
            recording.setsampwidth(2)
            recording.setframerate(1000)
            recording.writeframes(bytes(range(256)) * 78 + bytes(range(32)))
        raw = self.audio.read_bytes()
        self.artifact = artifacts.register_media(self.con,
            digest=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw), duration_s=10,
            format="wav", locator="fixture.wav")
        self.con.execute("INSERT INTO transcript_versions(episode_id,media_artifact_id,segments,"
                         "source_offsets,created_at) VALUES ('e1',?,?,?,?)",
                         (self.artifact["id"], json.dumps([backend_fixtures.SEGMENTS_E1[1]]),
                          json.dumps({"global_start_s": 10}), db.utcnow()))
        self.bind()
        ops.save_review(self.con, "fixture-ad", {"judgment": "ad", "eff_start_s": 10,
                        "eff_end_s": 20, "bounds_verified": True}, 1)
        ref = citations.current_source_ref(self.con, "evidence", "fixture-ad")
        source = citations.resolve_source_state(self.con, ref)
        self.assertEqual(source["media"]["recording_bounds"], {"start_s": 10, "end_s": 20})
        occurrence = campaigns._occurrence_from_evidence(self.con, "fixture-ad", "Lumen", ref)
        self.assertFalse(occurrence["timing_verified"])
        self.assertIsNone(occurrence["full_episode_duration_s"])
