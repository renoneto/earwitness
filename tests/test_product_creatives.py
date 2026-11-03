"""P06 product regressions: creatives + occurrences (stdlib unittest).

Disposable in-memory workspaces only. Each test fails on a plausible
implementation bug: cross-workspace leakage, auto-confirmation, copied
judgments, re-suggested rejected pairs, or fabricated placement totals.
"""
from __future__ import annotations

import json
import sqlite3
import unittest

from earwitness.app import contracts, creatives, db, observations
from earwitness.app import ops


def fresh_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON;")
    db.init_schema(con)
    return con


SEGS = [
    {"start": 0.0, "end": 15.0, "text": "go to kachava dot com use code SAVE20 for 20 percent off"},
    {"start": 15.0, "end": 30.0, "text": "back to editorial content here"},
]


def seed(con, ep="ep-1"):
    con.execute("INSERT INTO episodes (id, kind, fetch_date, updated_at)"
                " VALUES (?, 'supplied', '2026-01-02', '2026-01-02T00:00:00+00:00')", (ep,))
    cur = con.execute("INSERT INTO source_versions (source_key, version_no, sha256,"
                      " payload, is_active, created_at) VALUES (?, 1, 'abc', ?, 1, ?)",
                      (f"transcript:{ep}", json.dumps({"segments": SEGS}), db.utcnow()))
    vid = cur.lastrowid
    con.execute("UPDATE episodes SET transcript_version_id=? WHERE id=?", (vid, ep))
    con.commit()
    return vid


def propose(con, vid, ep="ep-1", start=0.0, end=15.0):
    return observations.propose_observation(
        con, episode_id=ep, start_s=start, end_s=end,
        bounds_evidence={"segments": SEGS}, fields={"read_type": {"value": "unknown"}},
        attributions=[{"entity": "kachava.com", "raw_phrase": "kachava dot com",
                       "channel": "url", "method": "deterministic",
                       "status": "suggested"}],
        source_pins={"source_version_id": vid, "transcript_version_id": vid},
        actor="pipeline")["observation_id"]


TEXT_A = "go to kachava dot com use code SAVE20 for 20 percent off today"
TEXT_B = "go to kachava dot com use code SAVE30 for 20 percent off today"


class FingerprintTest(unittest.TestCase):
    def test_exact_pcm_narrow_signal(self):
        h1 = creatives.pcm_hash([0, 1000, -1000, 500], 16000, 1)
        h2 = creatives.pcm_hash([0, 1000, -1000, 500], 16000, 1)
        h3 = creatives.pcm_hash([0, 1000, -1000, 501], 16000, 1)
        self.assertEqual(h1["sha256"], h2["sha256"])
        self.assertNotEqual(h1["sha256"], h3["sha256"])
        self.assertEqual(h1["config"]["target_rate"], 16000)
        self.assertEqual(h1["config"]["channels"], 1)

    def test_text_fp_preserves_commercial_tokens(self):
        fp = creatives.text_fingerprint(TEXT_A)
        self.assertIn("save20", [c.lower() for c in fp["codes"]])
        self.assertTrue(any("kachava" in d for d in fp["domains"]))
        self.assertTrue(fp["numbers"])
        self.assertTrue(fp["offers"])
        self.assertTrue(fp["shingle_hash"])

    def test_suggestion_is_suggestion_only_with_code_diff(self):
        con = fresh_db()
        try:
            vid = seed(con)
            a = propose(con, vid)
            b = propose(con, vid, start=0.0, end=15.0)
            creatives.fingerprint_observation(con, a, transcript_text=TEXT_A)
            creatives.fingerprint_observation(con, b, transcript_text=TEXT_B)
            out = creatives.suggest_memberships(con, a)
            self.assertTrue(out["suggestions"])
            top = out["suggestions"][0]
            self.assertFalse(top["confirmed"])
            self.assertIn("codes", top["commercial_diff"])
            mem = con.execute("SELECT COUNT(*) c FROM creative_memberships"
                              " WHERE confirmed=1").fetchone()["c"]
            self.assertEqual(mem, 0)
        finally:
            con.close()

    def test_duration_band_excludes_distant_candidate(self):
        con = fresh_db()
        try:
            vid = seed(con)
            a = propose(con, vid, start=0.0, end=15.0)
            far = propose(con, vid, start=0.0, end=1.0)
            creatives.fingerprint_observation(con, a, transcript_text=TEXT_A)
            creatives.fingerprint_observation(con, far, transcript_text=TEXT_A)
            out = creatives.suggest_memberships(
                con, a, config={"duration_band": 0.05, "min_shingle_overlap": 0.0})
            ids = [s["observation_id"] for s in out["suggestions"]]
            self.assertNotIn(far, ids)
        finally:
            con.close()

    def test_no_cross_workspace_lookup(self):
        con1, con2 = fresh_db(), fresh_db()
        try:
            vid1, vid2 = seed(con1), seed(con2)
            a = propose(con1, vid1)
            b = propose(con2, vid2)
            creatives.fingerprint_observation(con1, a, transcript_text=TEXT_A)
            creatives.fingerprint_observation(con2, b, transcript_text=TEXT_A)
            out = creatives.suggest_memberships(con1, a)
            ids = [s["observation_id"] for s in out["suggestions"]]
            self.assertNotIn(b, ids)
        finally:
            con1.close()
            con2.close()


class MembershipLifecycleTest(unittest.TestCase):
    def test_confirm_never_copies_judgments_and_revision_checked(self):
        con = fresh_db()
        try:
            vid = seed(con)
            a, b = propose(con, vid), propose(con, vid)
            before_reviews = con.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
            cr = creatives.create_creative(con, [a, b])["creative_id"]
            out = creatives.confirm_membership(con, cr, a, 1, reviewer="owner")
            self.assertTrue(out["confirmed"])
            self.assertFalse(out["judgments_copied"])
            after_reviews = con.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
            self.assertEqual(before_reviews, after_reviews)
            with self.assertRaises(ops.Conflict):
                creatives.confirm_membership(con, cr, b, 1, reviewer="owner")
            status, _ = contracts.error_for(ops.Conflict(2))
            self.assertEqual(status, 412)
        finally:
            con.close()

    def test_rejected_pair_not_resuggested_after_reprocess(self):
        con = fresh_db()
        try:
            vid = seed(con)
            a, b = propose(con, vid), propose(con, vid)
            creatives.fingerprint_observation(con, a, transcript_text=TEXT_A)
            creatives.fingerprint_observation(con, b, transcript_text=TEXT_B)
            first = creatives.suggest_memberships(con, a)
            digest = first["config_digest"]
            self.assertTrue(any(s["observation_id"] == b for s in first["suggestions"]))
            cr = creatives.create_creative(con, [a])["creative_id"]
            creatives.reject_membership(con, cr, a, b, 1, reviewer="owner",
                                        config_digest=digest)
            second = creatives.suggest_memberships(con, a)
            self.assertFalse(any(s["observation_id"] == b for s in second["suggestions"]))
        finally:
            con.close()

    def test_split_merge_reversible_lineage(self):
        con = fresh_db()
        try:
            vid = seed(con)
            a, b = propose(con, vid), propose(con, vid)
            cr = creatives.create_creative(con, [a, b])["creative_id"]
            split = creatives.split_creative(con, cr, [[a], [b]], 1, actor="owner")
            self.assertEqual(len(split["children"]), 2)
            merged = creatives.merge_creatives(
                con, split["children"],
                {cid: 1 for cid in split["children"]}, actor="owner")
            self.assertTrue(merged["creative_id"])
            payload = creatives.creative_service_payload(con, merged["creative_id"])
            self.assertTrue(any(x["kind"] == "native-button" for x in payload["actions"]))
        finally:
            con.close()


class OccurrenceTest(unittest.TestCase):
    def test_same_creative_twice_is_two_occurrences(self):
        con = fresh_db()
        try:
            vid = seed(con)
            a, b = propose(con, vid), propose(con, vid)
            o1 = creatives.create_occurrence(con, "ep-1", a, 1)["occurrence_id"]
            o2 = creatives.create_occurrence(con, "ep-1", b, 1)["occurrence_id"]
            self.assertNotEqual(o1, o2)
        finally:
            con.close()

    def test_refetch_same_mapping_vs_changed_ambiguous(self):
        con = fresh_db()
        try:
            vid = seed(con)
            a = propose(con, vid)
            occ = creatives.create_occurrence(con, "ep-1", a, 1)["occurrence_id"]
            same = creatives.map_refetch(con, occ, a, 1, media_digest="d1",
                                         source_interval="s1", episode_id="ep-1",
                                         same_bytes=True, same_interval=True)
            self.assertEqual(same["correspondence"], "confirmed")
            changed = creatives.map_refetch(con, occ, a, 1, media_digest="d2",
                                            source_interval="s2", episode_id="ep-1",
                                            same_bytes=False, same_interval=True)
            self.assertEqual(changed["correspondence"], "ambiguous")
            counts = creatives.occurrence_counts(con, "ep-1")
            self.assertIsNone(counts["exact_total"])
            self.assertGreaterEqual(counts["ambiguous_unresolved"], 1)
            self.assertGreaterEqual(counts["confirmed_occurrences_lower_bound"], 0)
            print(f"occurrence counts: snapshot={counts['snapshot_observations']}"
                  f" confirmed_lb={counts['confirmed_occurrences_lower_bound']}"
                  f" ambiguous={counts['ambiguous_unresolved']} exact={counts['exact_total']}")
        finally:
            con.close()

    def test_services_dispatch_and_idempotency_key_required(self):
        con = fresh_db()
        try:
            vid = seed(con)
            a = propose(con, vid)
            creatives.fingerprint_observation(con, a, transcript_text=TEXT_A)
            req = contracts.CreativeMatchRequest(observation_ids=(a,))
            with self.assertRaises(contracts.ContractError):
                creatives.match_creatives_service(None, con, req, idempotency_key="")
            out = creatives.match_creatives_service(None, con, req,
                                                    idempotency_key="k-1")
            self.assertIn(a, out["suggestions"])
            chg = contracts.ObservationChange(operation="revise",
                                              input_revision_ids=(1,),
                                              rationale="r")
            with self.assertRaises(contracts.ContractError):
                observations.save_observation_service(None, con, chg,
                                                      expected_revision=1,
                                                      idempotency_key="")
        finally:
            con.close()


class CustomerCreativeDetailTest(unittest.TestCase):
    def test_member_projects_observation_episode_and_evidence(self):
        from earwitness.app import customer
        con = fresh_db()
        try:
            vid = seed(con)
            oid = propose(con, vid)
            cid = creatives.create_creative(con, [oid])["creative_id"]
            con.execute(
                "INSERT INTO evidence (id, source, source_version_id, episode_id,"
                " orig_start_s, orig_end_s, excerpt, attrs, revision, created_at,"
                " transcript_version_id, source_record_key, assoc_revision,"
                " record_kind) VALUES ('ev-1', 'detector', 1, 'ep-1', 0.0, 15.0,"
                " 'kachava offer', ?, 1, ?, ?, 'ev-1', 0, 'candidate')",
                (json.dumps({"observation_id": oid}), db.utcnow(), vid))
            db.record_evidence_snapshot(con, "ev-1", "fixture")
            from earwitness.app import citations as _ci
            _ci.publish_source_state(con, kind="evidence", record_id="ev-1", actor="fixture")
            con.commit()
            detail = customer.creative_detail(con, cid)
            self.assertEqual(detail["member_count"], 1)
            (m,) = detail["members"]
            self.assertEqual(m["observation_id"], oid)
            self.assertFalse(m.get("confirmed"))
            self.assertEqual((m["observation"] or {})["episode_id"], "ep-1")
            self.assertEqual((m["episode"] or {})["id"], "ep-1")
            refs = m["evidence_refs"]
            # The propose path keeps its own linked sibling projection
            # alongside the attached candidate; every linked ref must be
            # exact and none invented.
            # Refs are SourceRef1 publication ids (sr1:), not legacy
            # "ev:<id>@v<n>" strings: each must be its evidence row's exact
            # current publication.
            self.assertGreaterEqual(len(refs), 1)
            self.assertIn("ev-1", {r["evidence_id"] for r in refs})
            for r in refs:
                self.assertTrue(r["ref"].startswith("sr1:"), r)
                self.assertEqual(
                    r["ref"], _ci.current_source_ref(con, "evidence", r["evidence_id"]))
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
