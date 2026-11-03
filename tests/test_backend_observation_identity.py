"""Archived transcript and pipeline transcript identities never alias."""
import unittest

from earwitness.app import campaigns, citations, db, observations, ops
from tests import backend_fixtures


class ObservationIdentityRegression(unittest.TestCase):
    def setUp(self):
        self.con = backend_fixtures.make_memory_workspace()
        backend_fixtures.publish_sources(self.con)
        self.addCleanup(self.con.close)
        self.archive_id = self.con.execute(
            "SELECT transcript_version_id FROM episodes WHERE id='e1'").fetchone()[0]

    def propose(self, **extra):
        return observations.propose_observation(self.con, "e1", 10.0, 20.0,
            bounds_evidence={"excerpt": "This episode is brought to you by Lumen"},
            source_pins={"source_version_id": self.archive_id,
                         "transcript_version_id": self.archive_id, **extra})

    def test_confirmation_binds_archived_payload_without_inventing_pipeline_id(self):
        made = self.propose()
        confirmed = observations.confirm_observation_field(self.con, made["observation_id"],
            "message", "This episode is brought to you by Lumen", 1,
            evidence={"segment_ref": 1}, actor="fixture-reviewer")
        ref = citations.current_source_ref(self.con, "evidence", made["evidence_id"])
        pinned = citations.resolve_source_state(self.con, ref)
        self.assertEqual(pinned["observation"]["confirmations"]["message"]["value"],
                         "This episode is brought to you by Lumen")
        self.assertEqual(pinned["pins"]["transcript_version_id"], self.archive_id)
        row = self.con.execute("SELECT transcript_version_id FROM ad_observations WHERE id=?",
                               (made["observation_id"],)).fetchone()
        self.assertIsNone(row[0])
        self.assertEqual(confirmed["revision"], 2)

    def test_foreign_pipeline_id_cannot_alias_valid_archived_transcript(self):
        self.con.execute("INSERT INTO transcript_versions(id,episode_id,segments,created_at)"
                         " VALUES (?,'e2','[]',?)", (self.archive_id, db.utcnow()))
        before = self.con.execute("SELECT COUNT(*) FROM ad_observations").fetchone()[0]
        with self.assertRaises(ops.BadInput):
            self.propose(pipeline_transcript_version_id=self.archive_id)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM ad_observations").fetchone()[0], before)

    def test_historical_read_does_not_reinstate_revoked_ad_judgment(self):
        made = self.propose()
        ops.save_review(self.con, made["evidence_id"], {"judgment": "ad"}, 0)
        ref = citations.current_source_ref(self.con, "evidence", made["evidence_id"])
        ops.save_review(self.con, made["evidence_id"], {"judgment": "not_ad"}, 1)
        pinned = citations.resolve_source_state(self.con, ref)
        occurrence = campaigns._occurrence_from_evidence(
            self.con, made["evidence_id"], "Lumen", ref)
        self.assertEqual(pinned["review"]["judgment"], "ad")
        self.assertEqual(occurrence["review_status"], "reviewed_not_ad")

    def test_historical_copy_keeps_value_but_cleared_confirmation_loses_credit(self):
        made = self.propose()
        observations.confirm_observation_field(self.con, made["observation_id"],
            "message", "This episode is brought to you by Lumen", 1,
            evidence={"segment_ref": 1}, actor="fixture-reviewer")
        ref = citations.current_source_ref(self.con, "evidence", made["evidence_id"])
        observations.clear_observation_field(self.con, made["observation_id"],
            "message", 2, actor="fixture-reviewer")
        occurrence = campaigns._occurrence_from_evidence(
            self.con, made["evidence_id"], "Lumen", ref)
        self.assertEqual(occurrence["fields"]["message"],
                         "This episode is brought to you by Lumen")
        self.assertNotIn("message", occurrence["verified_fields"])

    def test_remote_url_and_alignment_flag_do_not_prove_recording_timing(self):
        self.con.execute("UPDATE episodes SET duration_min=1,revision=revision+1 WHERE id='e1'")
        db.record_episode_snapshot(self.con, "e1", "fixture")
        ops.set_episode_media(self.con, "e1",
            {"audio_url": "https://audio.example/rotatable.mp3", "alignment": "verified"}, 2)
        ops.save_review(self.con, "fixture-ad",
            {"judgment": "ad", "eff_start_s": 10, "eff_end_s": 20, "bounds_verified": True}, 1)
        ref = citations.current_source_ref(self.con, "evidence", "fixture-ad")
        occurrence = campaigns._occurrence_from_evidence(self.con, "fixture-ad", "Lumen", ref)
        self.assertFalse(occurrence["timing_verified"])
        self.assertIsNone(occurrence["verified_start_s"])

    def test_current_human_confirmation_qualifies_pinned_copy(self):
        made = self.propose()
        observations.confirm_observation_field(self.con, made["observation_id"],
            "message", "This episode is brought to you by Lumen", 1,
            evidence={"segment_ref": 1}, actor="fixture-reviewer")
        ref = citations.current_source_ref(self.con, "evidence", made["evidence_id"])
        occurrence = campaigns._occurrence_from_evidence(
            self.con, made["evidence_id"], "Lumen", ref)
        self.assertEqual(occurrence["verified_fields"], ["message"])
        self.assertTrue(occurrence["human_verified_copy"])

    def test_human_evidence_ad_review_qualifies_proposed_observation(self):
        made = self.propose()
        ops.save_review(self.con, made["evidence_id"], {"judgment": "ad"}, 0)
        ref = citations.current_source_ref(self.con, "evidence", made["evidence_id"])
        occurrence = campaigns._occurrence_from_evidence(
            self.con, made["evidence_id"], "Lumen", ref)
        self.assertEqual(occurrence["review_status"], "reviewed_ad")

    def test_entered_copy_conflict_does_not_require_unentered_classification_confirmation(self):
        made = observations.propose_observation(self.con, "e1", 10, 20,
            fields={"read_type": {"value": "unknown"}},
            bounds_evidence={"excerpt": "This episode is brought to you by Lumen"},
            source_pins={"source_version_id": self.archive_id, "transcript_version_id": self.archive_id})
        observations.confirm_observation_field(self.con, made["observation_id"],
            "message", "This episode is brought to you by Lumen", 1,
            evidence={"segment_ref": 1}, actor="fixture-reviewer")
        ops.save_review(self.con, made["evidence_id"], {"judgment": "ad"}, 0)
        ops.set_brand_assocs(self.con, made["evidence_id"],
            [{"brand": "Lumen", "basis": "confirmed", "channel": "spoken"}], 0)
        from earwitness.app import creatives
        creatives.create_occurrence(self.con, "e1", made["observation_id"], 2, correspondence="confirmed")
        ref = citations.current_source_ref(self.con, "evidence", made["evidence_id"])
        occurrence = campaigns._occurrence_from_evidence(self.con, made["evidence_id"], "Lumen", ref)
        self.assertFalse(occurrence["human_verified_copy"])
        self.assertTrue(campaigns._verified_requirement_conflict(
            occurrence, {"requirements": {"message": "Different required copy"}}))
        self.assertFalse(campaigns._verified_requirement_conflict(
            occurrence, {"requirements": {"offer": "An unconfirmed offer"}}))
