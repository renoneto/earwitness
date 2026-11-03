"""Customer campaign truth and immutable report tests (Product-9 §4.5).

Positive fixtures are built through persisted authority (observation
proposal/review, occurrence creation, evidence review, attribution,
episode media verification). Bare candidates never promote: unresolved
correspondence, unverified copy/timing, and transcript-only coverage
stay unknown/ineligible with explicit gaps.
"""
from __future__ import annotations

import json
import sqlite3
import unittest

from earwitness.app import campaigns, citations, customer, coverage, db, observations, ops
from earwitness.app import creatives


def seed_full_episode(con, ep_id="ep-1", pub_date="2026-03-10", show="Show",
                      duration_min=60.0):
    """Episode with a full-duration transcript and recorded stage proof.

    Transcript segments genuinely span the whole requested interval, and
    every semantic stage is recorded through the validating attestation
    write path (transcribe/screen/publication/index with source pins).
    This is the complete-coverage counterpart to seed_episode's partial
    transcript, which must stay incomplete.
    """
    ep_no = (con.execute("SELECT COALESCE(MAX(version_no), 0) + 1 FROM source_versions"
                         " WHERE source_key='episodes.json'").fetchone()[0])
    cur = con.execute(
        "INSERT INTO source_versions (source_key, version_no, sha256, payload,"
        " is_active, created_at) VALUES ('episodes.json', ?, ?, ?, 1, ?)",
        (ep_no, "sh-ep", json.dumps([{"ep_id": ep_id, "show_title": show,
                                      "feed_url": "https://show.example/rss"}]),
         db.utcnow()))
    req = float(duration_min) * 60.0
    half = req / 2.0
    segments = [{"start": 0.0, "end": half,
                 "text": "acme bright widget offer WIDGET10"},
                {"start": half, "end": req, "text": "more acme talk"}]
    cur = con.execute(
        "INSERT INTO source_versions (source_key, version_no, sha256, payload,"
        " is_active, created_at) VALUES (?, 1, 'sh', ?, 1, ?)",
        (f"transcript:{ep_id}", json.dumps({"segments": segments}), db.utcnow()))
    tv = cur.lastrowid
    con.execute(
        "INSERT INTO episodes (id, kind, show_title, ep_title, category, pub_date,"
        " duration_min, fetch_date, transcript_version_id, revision, updated_at)"
        " VALUES (?, 'supplied', ?, 'Ep', 'business', ?, ?, '2026-09-21', ?, 1, ?)",
        (ep_id, show, pub_date, duration_min, tv, db.utcnow()))
    pin = f"source-version:{tv}"
    for stage in ("transcribe", "screen", "observation_publication", "index"):
        coverage.record_attestation(
            con, ep_id, stage, (0.0, req), (0.0, req),
            source_pin=pin, producer="test-fixture", actor="owner")
    from tests.backend_fixtures import record_screening
    record_screening(con, ep_id)
    from earwitness.app import citations as _citations
    _citations.publish_source_state(con, kind="episode", record_id=ep_id,
                                    actor="fixture")
    con.commit()
    return tv


def fresh_db():
    from tests.backend_fixtures import make_file_workspace
    return make_file_workspace()


def seed_episode(con, ep_id="ep-1", pub_date="2026-03-10", show="Show",
                 version_no=1):
    cur = con.execute(
        "INSERT INTO source_versions (source_key, version_no, sha256, payload,"
        " is_active, created_at) VALUES ('episodes.json', ?, ?, ?, 1, ?)",
        (version_no, "sh-ep", json.dumps([{"ep_id": ep_id, "show_title": show,
                                           "feed_url": "https://show.example/rss"}]),
         db.utcnow()))
    ep_version = cur.lastrowid
    cur = con.execute(
        "INSERT INTO source_versions (source_key, version_no, sha256, payload,"
        " is_active, created_at) VALUES (?, 1, 'sh', ?, 1, ?)",
        (f"transcript:{ep_id}",
         json.dumps({"segments": [{"start": 0.0, "end": 150.0,
                                    "text": "acme bright widget offer WIDGET10"}]}), db.utcnow()))
    tv = cur.lastrowid
    con.execute(
        "INSERT INTO episodes (id, kind, show_title, ep_title, category, pub_date,"
        " duration_min, fetch_date, transcript_version_id, revision, updated_at)"
        " VALUES (?, 'supplied', ?, 'Ep', 'business', ?, 60.0, '2026-09-21', ?, 1, ?)",
        (ep_id, show, pub_date, tv, db.utcnow()))
    from earwitness.app import citations as _citations
    _citations.publish_source_state(con, kind="episode", record_id=ep_id,
                                    actor="fixture")
    con.commit()
    return ep_version


def seed_campaign(con, brand="Acme"):
    from earwitness.app import briefs
    brief = briefs.create_brief(con, {"title": "Acme provided brief", "provided_text": "bright WIDGET10"})
    ref = {"kind": "supplied_brief", "brief_id": brief["brief_id"], "version": 1}
    created = campaigns.save_customer_campaign(con, {"campaign": {
        "name": "Acme flight", "mode": "supplied_requirements", "brand": brand,
        "message": "bright", "offer": "WIDGET10", "notes": "",
        "requirement_provenance": {"brief_ref": ref}}})
    return created["campaign"]["id"]


def synthetic_brief(con):
    from earwitness.app import briefs
    created = briefs.create_brief(con, {"title": "Synthetic provided brief",
        "provided_text": "Generated fixture requirements; not a real agreement."}, actor="fixture")
    return {"kind": "supplied_brief", "brief_id": created["brief_id"], "version": 1}


def add_placement(con, cid, pid_suffix="p1", requirements=None, expected_count=1,
                  episode_id=None, date_from="2026-03-01", date_to="2026-03-31"):
    projection = json.loads(con.execute("SELECT brief_ref_json FROM campaigns WHERE id=?", (cid,)).fetchone()[0])
    provenance = {"brief_ref": {"kind": "supplied_brief", "brief_id": projection["brief_id"], "version": projection["version"]}}
    if episode_id:
        provenance["source_scope"] = campaigns._capture_episode_scope(con, episode_id)
    saved = campaigns.save_expected_placement(
        con, cid, scope="episode" if episode_id else "show_window",
        show_id="", episode_id=episode_id, date_from=date_from, date_to=date_to,
        timezone_name="UTC", expected_count=expected_count,
        requirements=requirements or {},
        provenance=provenance, priority=0, actor="owner")
    return saved["placement_id"]


def add_candidate(con, ev_id, episode_id="ep-1", excerpt="acme read",
                  attrs=None, source_version=1, start=0.0, end=10.0):
    """Import-equivalent bare candidate: no review, attribution, or linkage."""
    observation_id = (attrs or {}).get("observation_id")
    if observation_id:
        observation = con.execute("SELECT episode_id,source_version_id FROM ad_observations WHERE id=?",
                                  (observation_id,)).fetchone()
        if observation is None or observation["episode_id"] != episode_id:
            raise ValueError("fixture observation ownership differs")
        source_version = observation["source_version_id"]
    con.execute(
        "INSERT INTO evidence (id, source, source_version_id, episode_id, orig_start_s,"
        " orig_end_s, excerpt, attrs, revision, created_at, transcript_version_id,"
        " source_record_key, assoc_revision, record_kind)"
        " VALUES (?, 'detector', ?, ?, ?, ?, ?, ?, 1, ?, "
        " (SELECT transcript_version_id FROM episodes WHERE id=?), ?, 0, 'candidate')",
        (ev_id, source_version, episode_id, start, end, excerpt,
         json.dumps(attrs or {}), db.utcnow(), episode_id, ev_id))
    from earwitness.app import citations as _citations
    _citations.publish_source_state(con, kind="evidence", record_id=ev_id,
                                    actor="fixture")
    _citations.publish_source_state(con, kind="episode", record_id=episode_id,
                                    actor="fixture")
    con.commit()


def verify_full_ad(con, ev_id, episode_id="ep-1", brand="acme",
                   eff_start=0.0, eff_end=10.0,
                   audio_url="https://x.example/a.mp3", assoc_brand=None):
    """Record every authority a supported occurrence needs: observation
    proposal + human review, persisted occurrence with correspondence,
    evidence review with recorded bounds, confirmed attribution for the
    brand, and verified episode media with audio. assoc_brand overrides
    the attributed brand to test cross-brand rejection."""
    attributed = assoc_brand or brand
    tv = con.execute("SELECT transcript_version_id FROM episodes WHERE id=?",
                     (episode_id,)).fetchone()["transcript_version_id"]
    from tests.backend_fixtures import register_synthetic_recording
    recording = register_synthetic_recording(con, episode_id)
    prop = observations.propose_observation(
        con, episode_id, eff_start, eff_end,
        bounds_evidence={"excerpt": "acme read"},
        fields={"read_type": {"value": "host_read"},
                "spoken_brand": {"value": "acme", "method": "deterministic",
                                 "segment_ref": 0}},
        attributions=[{"entity": attributed, "channel": "spoken",
                       "status": "confirmed", "segment_ref": 0,
                       "method": "review"}],
        source_pins={"source_version_id": tv, "transcript_version_id": tv,
                     "pipeline_transcript_version_id": recording["pipeline_transcript_version_id"],
                     "media_artifact_id": recording["artifact"]["id"], "fetch_date": "2026-09-21"},
        actor="owner")
    obs_id = prop["observation_id"]
    observations.review_observation(con, obs_id, "reviewed_ad", 1, actor="owner")
    creatives.create_occurrence(con, episode_id, obs_id, 2,
                                correspondence="confirmed", actor="owner")
    ops.save_review(con, ev_id,
                    {"judgment": "ad", "eff_start_s": eff_start,
                     "eff_end_s": eff_end, "bounds_verified": True},
                    0, actor="owner")
    ops.set_brand_assocs(con, ev_id,
                         [{"brand": attributed, "basis": "confirmed",
                           "channel": "spoken"}], 0, actor="owner")
    # Link the evidence row to its observation (the propose path creates a
    # sibling projection; here the attached candidate is resolved to it).
    attrs = json.loads(con.execute("SELECT attrs FROM evidence WHERE id=?",
                                   (ev_id,)).fetchone()["attrs"] or "{}")
    attrs["observation_id"] = obs_id
    con.execute("UPDATE evidence SET attrs=?,source_version_id=?,revision=revision+1 WHERE id=?",
                (json.dumps(attrs), tv, ev_id))
    db.record_evidence_snapshot(con, ev_id, "fixture")
    citations.publish_source_state(con, kind="evidence", record_id=ev_id, actor="fixture")
    con.commit()
    return {"observation_id": obs_id, "evidence_id": ev_id}


def occ(oid, **kw):
    d = {"id": oid, "correspondence": "confirmed",
         "review_status": "reviewed_ad", "advertiser_confirmed": True,
         "published_at": "2026-03-10T12:00:00+00:00", "episode_id": "ep-1",
         "source_matched": True, "alignment_verified": True,
         "bounds_verified": True, "full_episode_duration_s": 3600.0,
         "duration_s": 30.0, "position_s": 120.0,
         "fields": {}, "verified_fields": [], "date_precision": "instant"}
    d.update(kw)
    return d


def placement(pid, **kw):
    d = {"id": pid, "scope": "show_window", "show_id": "", "episode_id": None,
         "date_from": "2026-03-01", "date_to": "2026-03-31",
         "timezone": "UTC", "expected_count": 1,
         "requirements": {}, "provenance": {"brief_ref": "b-1"}}
    d.update(kw)
    return d


class AllocatorFixTest(unittest.TestCase):
    def test_valid_reuse_one_pair_third_blocked(self):
        ps = [placement("p1"), placement("p2"), placement("p3")]
        out = campaigns.allocate_campaign(
            ps, [occ("o1")], {"state": "complete"}, "2026-04-01T00:00:00+00:00",
            {"o1": {"placement_id": "p2", "rationale": "Approved p2 reuse only"}})
        self.assertEqual(out["evaluations"]["p1"]["outcome"], "supported")
        self.assertEqual(out["evaluations"]["p2"]["outcome"], "supported")
        self.assertIn("o1", out["evaluations"]["p2"]["eligible_ids"])
        self.assertNotEqual(out["evaluations"]["p3"]["outcome"], "supported")
        self.assertEqual(out["used"], {"o1": "p1"})
        self.assertEqual(out["invalid_overrides"], {})

    def test_ghost_override_never_eligible(self):
        ps = [placement("p1"), placement("p2")]
        out = campaigns.allocate_campaign(
            ps, [occ("o1")], {"state": "complete"}, "2026-04-01T00:00:00+00:00",
            {"ghost": {"placement_id": "p2", "rationale": "x"}})
        self.assertIn("ghost", out["invalid_overrides"])
        for ev in out["evaluations"].values():
            self.assertNotIn("ghost", ev["eligible_ids"])

    def test_ineligible_override_rejected(self):
        bad = occ("o-bad", review_status="unreviewed", advertiser_confirmed=False)
        ps = [placement("p1")]
        out = campaigns.allocate_campaign(
            ps, [bad], {"state": "complete"}, "2026-04-01T00:00:00+00:00",
            {"o-bad": {"placement_id": "p1", "rationale": "please"}})
        self.assertIn("o-bad", out["invalid_overrides"])
        self.assertNotIn("o-bad", out["evaluations"]["p1"]["eligible_ids"])

    def test_unresolved_correspondence_ineligible(self):
        ps = [placement("p1")]
        out = campaigns.allocate_campaign(
            ps, [occ("o-u", correspondence="unresolved")], {"state": "complete"},
            "2026-04-01T00:00:00+00:00")
        self.assertNotIn("o-u", out["evaluations"]["p1"]["eligible_ids"])
        self.assertIn("unresolved correspondence",
                      " ".join(out["evaluations"]["p1"]["reasons"]))

    def test_six_outcomes(self):
        p = placement("p1")
        now = "2026-04-01T00:00:00+00:00"
        supported = campaigns.evaluate_one(p, [occ("o1")], {"state": "complete"}, now)
        self.assertEqual(supported["outcome"], "supported")
        pending = campaigns.evaluate_one(
            placement("p1", date_to="2026-12-31"), [], {"state": "complete"}, now)
        self.assertEqual(pending["outcome"], "pending")
        incomplete = campaigns.evaluate_one(
            p, [], {"state": "incomplete", "missing": ["ep-9"], "failed": []}, now)
        self.assertEqual(incomplete["outcome"], "incomplete_coverage")
        nomatch = campaigns.evaluate_one(p, [], {"state": "complete"}, now)
        self.assertEqual(nomatch["outcome"], "no_observation_in_covered_sources")
        amb = campaigns.evaluate_one(
            p, [occ("o2", correspondence="ambiguous")], {"state": "complete"}, now)
        self.assertEqual(amb["outcome"], "ambiguous")
        contra = campaigns.evaluate_one(
            p, [], {"state": "complete", "contradicted_by": ["ev-x"]}, now)
        self.assertEqual(contra["outcome"], "contradicted")

    def test_omitted_coverage_is_unknown(self):
        ev = campaigns.evaluate_one(placement("p1"), [], {}, "2026-04-01T00:00:00+00:00")
        self.assertEqual(ev["coverage_state"], "unknown")
        self.assertNotEqual(ev["outcome"], "no_observation_in_covered_sources")

    def test_comparison_verified_timing_vs_unknown_copy(self):
        p = placement("p1", requirements={"duration_s": [5, 30], "code": "WIDGET10"})
        verified = occ("o1", timing_verified=True, duration_s=10.0,
                       fields={"code": "WIDGET10"})
        rows = campaigns._compare_requirements(p, verified, "ev:x@v1")
        by_field = {r["field"]: r["state"] for r in rows}
        # Timing verifies through the recorded chain; copy has no authority.
        self.assertEqual(by_field["duration_s"], "supported")
        self.assertEqual(by_field["code"], "unknown")
        self.assertTrue(all(r["evidence_ref"] == "ev:x@v1" for r in rows))
        conflict = occ("o1", timing_verified=True, duration_s=120.0)
        rows = campaigns._compare_requirements(
            placement("p1", requirements={"duration_s": [5, 30]}), conflict, "ev:x@v1")
        self.assertEqual(rows[0]["state"], "mismatch")
        self.assertEqual(rows[0]["observed"], 120.0)
        rows = campaigns._compare_requirements(p, None, None)
        self.assertTrue(all(r["state"] == "unknown" for r in rows))


class DraftReportTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con)
        self.cid = seed_campaign(self.con)

    def tearDown(self):
        self.con.close()

    def test_bare_candidate_never_promotes(self):
        # The coordinator probe: candidate + attrs code, no observation,
        # review linkage, correspondence, or processing proof.
        pid = add_placement(self.con, self.cid, episode_id="ep-1",
                            requirements={"code": "WIDGET10"})
        add_candidate(self.con, "x", attrs={"code": "WIDGET10"})
        rev = self.con.execute("SELECT revision FROM campaigns WHERE id=?",
                               (self.cid,)).fetchone()[0]
        campaigns.save_placement_evidence(self.con, self.cid, pid, citations.current_source_ref(self.con, "evidence", "x"), rev)
        draft = campaigns.build_customer_draft(self.con, self.cid)
        ev = draft["allocation"]["evaluations"][pid]
        self.assertNotEqual(ev["outcome"], "supported")
        self.assertEqual(ev["eligible_ids"], [])
        self.assertNotEqual(
            draft["coverage_by_placement"][pid]["state"], "complete")
        rows = draft["comparisons"][pid]
        code_row = next(r for r in rows if r["field"] == "code")
        self.assertEqual(code_row["state"], "unknown")
        self.assertEqual(code_row["evidence_ref"], citations.current_source_ref(self.con, "evidence", "x"))

    def test_supported_through_recorded_authority(self):
        # Complete coverage needs full-interval recorded stage proof, so
        # this fixture uses the full-duration episode; the partial
        # seed_episode transcript must stay incomplete (see followup tests).
        seed_full_episode(self.con, ep_id="ep-full")
        pid = add_placement(self.con, self.cid, episode_id="ep-full",
                            requirements={"duration_s": [5, 30]})
        add_candidate(self.con, "x", episode_id="ep-full")
        verify_full_ad(self.con, "x", episode_id="ep-full")
        rev = self.con.execute("SELECT revision FROM campaigns WHERE id=?",
                               (self.cid,)).fetchone()[0]
        campaigns.save_placement_evidence(self.con, self.cid, pid, citations.current_source_ref(self.con, "evidence", "x"), rev)
        draft = campaigns.build_customer_draft(self.con, self.cid)
        ev = draft["allocation"]["evaluations"][pid]
        self.assertEqual(ev["outcome"], "supported")
        self.assertEqual(
            draft["coverage_by_placement"][pid]["state"], "complete")
        rows = draft["comparisons"][pid]
        self.assertEqual(rows[0]["state"], "supported")
        self.assertEqual(rows[0]["observed"], 10.0)
        self.assertEqual(rows[0]["evidence_ref"], citations.current_source_ref(self.con, "evidence", "x"))

    def test_verified_mismatch_shows_without_pass(self):
        pid = add_placement(self.con, self.cid, episode_id="ep-1",
                            requirements={"duration_s": [5, 30]})
        add_candidate(self.con, "x")
        verify_full_ad(self.con, "x", eff_start=0.0, eff_end=120.0)
        rev = self.con.execute("SELECT revision FROM campaigns WHERE id=?",
                               (self.cid,)).fetchone()[0]
        campaigns.save_placement_evidence(self.con, self.cid, pid, citations.current_source_ref(self.con, "evidence", "x"), rev)
        draft = campaigns.build_customer_draft(self.con, self.cid)
        ev = draft["allocation"]["evaluations"][pid]
        self.assertNotEqual(ev["outcome"], "supported")
        rows = draft["comparisons"][pid]
        self.assertEqual(rows[0]["state"], "mismatch")
        self.assertEqual(rows[0]["observed"], 120.0)
        self.assertEqual(rows[0]["expected"], [5, 30])
        self.assertEqual(rows[0]["evidence_ref"], citations.current_source_ref(self.con, "evidence", "x"))

    def test_cross_brand_confirmed_assoc_does_not_count(self):
        pid = add_placement(self.con, self.cid, episode_id="ep-1",
                            requirements={"duration_s": [5, 30]})
        add_candidate(self.con, "x")
        verify_full_ad(self.con, "x", assoc_brand="rival")
        rev = self.con.execute("SELECT revision FROM campaigns WHERE id=?",
                               (self.cid,)).fetchone()[0]
        campaigns.save_placement_evidence(self.con, self.cid, pid, citations.current_source_ref(self.con, "evidence", "x"), rev)
        draft = campaigns.build_customer_draft(self.con, self.cid)
        ev = draft["allocation"]["evaluations"][pid]
        self.assertEqual(ev["eligible_ids"], [])

    def test_freeze_captures_evidence_and_a_note_does_not_stale_it(self):
        pid = add_placement(self.con, self.cid, episode_id="ep-1",
                            requirements={"duration_s": [5, 30]})
        add_candidate(self.con, "x")
        verify_full_ad(self.con, "x")
        rev = self.con.execute("SELECT revision FROM campaigns WHERE id=?",
                               (self.cid,)).fetchone()[0]
        ref = citations.current_source_ref(self.con, "evidence", "x")
        out = campaigns.save_placement_evidence(self.con, self.cid, pid, ref, rev)
        rev = out["campaign_revision"]
        draft = campaigns.build_customer_draft(self.con, self.cid)
        frozen = campaigns.freeze_customer_report(
            self.con, self.cid, rev, draft["pins"])
        status = campaigns.get_customer_report(
            self.con, self.cid, frozen["report_no"])
        captured = status["frozen"]["captured"][pid][0]
        self.assertEqual(captured["text"], "acme bright widget offer WIDGET10")
        self.assertEqual(captured["ref"], ref)
        body, _ = campaigns.get_customer_report_html(
            self.con, self.cid, frozen["report_no"])
        self.assertIn("acme bright widget offer WIDGET10", body)
        self.assertNotIn("<script", body)
        # A personal note is not part of a report (PRIVACY-FEED-FIX1).
        customer.save_mistake(self.con, ref, "Source uncertain")
        status2 = campaigns.get_customer_report(
            self.con, self.cid, frozen["report_no"])
        self.assertFalse(status2["stale"])
        self.assertEqual(status2["stale_reasons"], [])
        body2, _ = campaigns.get_customer_report_html(
            self.con, self.cid, frozen["report_no"])
        self.assertEqual(body, body2)
        # Unrelated settings do not stale the report.
        settings = ops.get_settings(self.con)
        ops.save_settings(self.con, {"playback_speed": 1.5}, settings["revision"])
        status3 = campaigns.get_customer_report(
            self.con, self.cid, frozen["report_no"])
        self.assertEqual(sorted(status3["stale_reasons"]),
                         sorted(status2["stale_reasons"]))

    def test_unattached_suggestions_do_not_count(self):
        # Covered-no-match needs genuinely complete coverage: full-duration
        # episode with recorded stage proof, pool suggestion left unattached.
        seed_full_episode(self.con, ep_id="ep-full")
        pid = add_placement(self.con, self.cid, episode_id="ep-full")
        add_candidate(self.con, "ev-pool", episode_id="ep-full")
        verify_full_ad(self.con, "ev-pool", episode_id="ep-full")
        self.con.execute("INSERT INTO campaign_evidence (campaign_id, evidence_id)"
                         " VALUES (?, 'ev-pool')", (self.cid,))
        self.con.commit()
        draft = campaigns.build_customer_draft(self.con, self.cid)
        self.assertIn("ev-pool", draft["suggestions"])
        self.assertEqual(draft["allocation"]["evaluations"][pid]["outcome"],
                         "no_observation_in_covered_sources")

    def test_ambiguous_scope_never_supported(self):
        pid = add_placement(self.con, self.cid)
        add_candidate(self.con, "x")
        verify_full_ad(self.con, "x")
        rev = self.con.execute("SELECT revision FROM campaigns WHERE id=?",
                               (self.cid,)).fetchone()[0]
        with self.assertRaises(ops.BadInput):
            campaigns.save_placement_evidence(self.con, self.cid, pid, citations.current_source_ref(self.con, "evidence", "x"), rev)
        draft = campaigns.build_customer_draft(self.con, self.cid)
        ev = draft["allocation"]["evaluations"][pid]
        self.assertEqual(ev["outcome"], "ambiguous")
        self.assertTrue(draft["coverage_by_placement"][pid].get("ambiguity"))

    def test_wrong_campaign_report_rejected(self):
        pid = add_placement(self.con, self.cid, episode_id="ep-1")
        add_candidate(self.con, "x")
        verify_full_ad(self.con, "x")
        rev = self.con.execute("SELECT revision FROM campaigns WHERE id=?",
                               (self.cid,)).fetchone()[0]
        out = campaigns.save_placement_evidence(self.con, self.cid, pid, citations.current_source_ref(self.con, "evidence", "x"), rev)
        rev = out["campaign_revision"]
        draft = campaigns.build_customer_draft(self.con, self.cid)
        frozen = campaigns.freeze_customer_report(self.con, self.cid, rev, draft["pins"])
        with self.assertRaises(ops.BadInput):
            campaigns.get_customer_report(self.con, self.cid + 999, frozen["report_no"])
        with self.assertRaises(ops.BadInput):
            campaigns.get_customer_report_html(self.con, self.cid, 999)

    def test_failed_freeze_leaves_no_partial(self):
        pid = add_placement(self.con, self.cid, episode_id="ep-1")
        add_candidate(self.con, "x")
        verify_full_ad(self.con, "x")
        rev = self.con.execute("SELECT revision FROM campaigns WHERE id=?",
                               (self.cid,)).fetchone()[0]
        out = campaigns.save_placement_evidence(self.con, self.cid, pid, citations.current_source_ref(self.con, "evidence", "x"), rev)
        rev = out["campaign_revision"]
        before = self.con.execute("SELECT COUNT(*) FROM campaign_reports").fetchone()[0]
        with self.assertRaises(Exception):
            campaigns.freeze_customer_report(self.con, self.cid, rev, {"bogus": "pins"})
        after = self.con.execute("SELECT COUNT(*) FROM campaign_reports").fetchone()[0]
        self.assertEqual(before, after)

    def test_evidence_delete_cannot_cascade_report(self):
        pid = add_placement(self.con, self.cid, episode_id="ep-1")
        add_candidate(self.con, "x")
        verify_full_ad(self.con, "x")
        rev = self.con.execute("SELECT revision FROM campaigns WHERE id=?",
                               (self.cid,)).fetchone()[0]
        ref = citations.current_source_ref(self.con, "evidence", "x")
        out = campaigns.save_placement_evidence(self.con, self.cid, pid, ref, rev)
        rev = out["campaign_revision"]
        draft = campaigns.build_customer_draft(self.con, self.cid)
        frozen = campaigns.freeze_customer_report(self.con, self.cid, rev, draft["pins"])
        body_before, _ = campaigns.get_customer_report_html(
            self.con, self.cid, frozen["report_no"])
        self.con.execute("DELETE FROM brand_assocs WHERE evidence_id='x'")
        self.con.execute("DELETE FROM reviews WHERE evidence_id='x'")
        for table in ("evidence_history", "review_history", "association_history"):
            self.con.execute(f"DELETE FROM {table} WHERE evidence_id='x'")
        self.con.execute("DELETE FROM placement_evidence WHERE ref=?", (ref,))
        self.con.execute("DELETE FROM evidence WHERE id='x'")
        self.con.commit()
        body_after, _ = campaigns.get_customer_report_html(
            self.con, self.cid, frozen["report_no"])
        self.assertEqual(body_before, body_after)
        status = campaigns.get_customer_report(self.con, self.cid, frozen["report_no"])
        self.assertTrue(status["stale"])

    def test_ownership_checks(self):
        pid = add_placement(self.con, self.cid, episode_id="ep-1")
        add_candidate(self.con, "x")
        rev = self.con.execute("SELECT revision FROM campaigns WHERE id=?",
                               (self.cid,)).fetchone()[0]
        with self.assertRaises(ops.BadInput):
            campaigns.save_placement_evidence(self.con, 9999, pid, citations.current_source_ref(self.con, "evidence", "x"), 1)
        with self.assertRaises(ops.BadInput):
            campaigns.save_placement_evidence(self.con, self.cid, "nope", citations.current_source_ref(self.con, "evidence", "x"), rev)
        with self.assertRaises(ops.MissingPrecondition):
            campaigns.save_placement_evidence(self.con, self.cid, pid, citations.current_source_ref(self.con, "evidence", "x"), None)
        with self.assertRaises(ops.Conflict):
            campaigns.save_placement_evidence(self.con, self.cid, pid, citations.current_source_ref(self.con, "evidence", "x"), rev + 99)
        with self.assertRaises(citations.RevisionUnavailable):
            campaigns.save_placement_evidence(self.con, self.cid, pid, "ev:x@v9", rev)


class OccurrenceIdentityTest(unittest.TestCase):
    """Persisted occurrence identity (§4.5): one linked occurrence attached
    through several refs is one delivery; missing/unverified copy is
    unknown, never a mismatch; explicit reuse overrides reach the draft."""

    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con)
        self.cid = seed_campaign(self.con)

    def tearDown(self):
        self.con.close()

    def _attach(self, pid, *refs):
        # Each attachment advances the owning campaign revision, so the
        # current revision is re-read per mutation; stale writes must 412.
        for ref in refs:
            rev = self.con.execute("SELECT revision FROM campaigns WHERE id=?",
                                   (self.cid,)).fetchone()[0]
            campaigns.save_placement_evidence(self.con, self.cid, pid, ref, rev)

    def _two_refs_one_occurrence(self, expected_count=2):
        pid = add_placement(self.con, self.cid, expected_count=expected_count,
                            episode_id="ep-1")
        add_candidate(self.con, "x")
        link = verify_full_ad(self.con, "x")
        add_candidate(self.con, "y",
                      attrs={"observation_id": link["observation_id"]})
        ops.save_review(self.con, "y",
                        {"judgment": "ad", "eff_start_s": 0.0, "eff_end_s": 10.0,
                         "bounds_verified": True}, 0, actor="owner")
        ops.set_brand_assocs(self.con, "y",
                             [{"brand": "acme", "basis": "confirmed",
                               "channel": "spoken"}], 0, actor="owner")
        self._attach(pid, citations.current_source_ref(self.con, "evidence", "x"), citations.current_source_ref(self.con, "evidence", "y"))
        return pid, link["observation_id"]

    def test_same_occurrence_twice_is_one_delivery(self):
        pid, _ = self._two_refs_one_occurrence(expected_count=2)
        n = self.con.execute("SELECT count(*) FROM occurrences").fetchone()[0]
        self.assertEqual(n, 1)
        draft = campaigns.build_customer_draft(self.con, self.cid)
        ev = draft["allocation"]["evaluations"][pid]
        # One resolved identity, counted once: two expected deliveries from
        # one occurrence can never read supported.
        self.assertEqual(len(ev["eligible_ids"]), 1)
        self.assertEqual(ev["eligible_ids"],
                         draft["eligible_by_placement"][pid])
        self.assertNotEqual(ev["outcome"], "supported")
        self.assertTrue(any("counts once" in r for r in ev["reasons"]))

    def test_multiple_linked_occurrences_stay_ambiguous(self):
        pid, obs_id = self._two_refs_one_occurrence(expected_count=1)
        creatives.create_occurrence(self.con, "ep-1", obs_id, 2,
                                    correspondence="confirmed", actor="owner")
        draft = campaigns.build_customer_draft(self.con, self.cid)
        ev = draft["allocation"]["evaluations"][pid]
        self.assertEqual(ev["eligible_ids"], [])
        self.assertEqual(ev["outcome"], "ambiguous")
        self.assertTrue(any("correspondence ambiguous" in r
                            for r in ev["reasons"]))

    def test_absent_copy_is_unknown_not_mismatch(self):
        ok, gaps = campaigns._occurrence_eligible(
            occ("o1"), placement("p1", requirements={"code": "X"}))
        self.assertFalse(ok)
        self.assertEqual(gaps, ["code: unverified required field is unknown"])

    def test_unverified_observed_copy_is_unknown_not_mismatch(self):
        o = occ("o1", fields={"code": "X"})
        ok, gaps = campaigns._occurrence_eligible(
            o, placement("p1", requirements={"code": "X"}))
        self.assertFalse(ok)
        self.assertEqual(gaps, ["code: unverified required field is unknown"])

    def test_verified_copy_conflict_is_mismatch(self):
        o = occ("o1", fields={"code": "Y"}, verified_fields=["code"])
        ok, gaps = campaigns._occurrence_eligible(
            o, placement("p1", requirements={"code": "X"}))
        self.assertFalse(ok)
        self.assertEqual(gaps, ["code: mismatch"])

    def test_verified_copy_match_is_eligible(self):
        o = occ("o1", fields={"code": "X"}, verified_fields=["code"])
        ok, gaps = campaigns._occurrence_eligible(
            o, placement("p1", requirements={"code": "X"}))
        self.assertTrue(ok)
        self.assertEqual(gaps, [])

    def test_draft_honors_explicit_reuse_override(self):
        p1 = add_placement(self.con, self.cid, pid_suffix="p1",
                           expected_count=1, episode_id="ep-1")
        p2 = add_placement(self.con, self.cid, pid_suffix="p2",
                           expected_count=1, episode_id="ep-1")
        add_candidate(self.con, "x")
        verify_full_ad(self.con, "x")
        self._attach(p1, citations.current_source_ref(self.con, "evidence", "x"))
        self._attach(p2, citations.current_source_ref(self.con, "evidence", "x"))
        plain = campaigns.build_customer_draft(self.con, self.cid)
        evals = plain["allocation"]["evaluations"]
        # One occurrence, two placements: exactly one wins by stable order.
        winners = [pid for pid in (p1, p2)
                   if evals[pid]["outcome"] == "supported"]
        losers = [pid for pid in (p1, p2)
                  if evals[pid]["outcome"] != "supported"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)
        winner, loser = winners[0], losers[0]
        oid = evals[winner]["eligible_ids"][0]
        explicit = {oid: {"placement_id": loser,
                          "rationale": "Owner-approved encore"}}
        draft = campaigns.build_customer_draft(self.con, self.cid, explicit)
        self.assertEqual(
            draft["allocation"]["evaluations"][loser]["outcome"], "supported")
        self.assertIn(
            oid, draft["allocation"]["evaluations"][loser]["eligible_ids"])
        # The original reservation is retained: reuse counts without moving.
        self.assertEqual(draft["allocation"]["used"], {oid: winner})
        self.assertEqual(draft["allocation"]["invalid_overrides"], {})

    def test_placement_body_rejects_envelope_revision(self):
        # Envelope/domain separation: expected_revision travels outside
        # the placement body; the domain never accepts it as a field,
        # while a valid edit through the envelope revision succeeds.
        pid = add_placement(self.con, self.cid, expected_count=1,
                            episode_id="ep-1")
        rev = self.con.execute("SELECT revision FROM expected_placements"
                               " WHERE id=?", (pid,)).fetchone()[0]
        with self.assertRaises(ops.BadInput):
            campaigns.update_expected_placement(
                self.con, self.cid, pid,
                {"requirements": {"offer": "Updated free trial"},
                 "expected_revision": rev}, rev, actor="owner")
        out = campaigns.update_expected_placement(
            self.con, self.cid, pid,
            {"requirements": {"offer": "Updated free trial"}}, rev,
            actor="owner")
        self.assertEqual(out["placement_id"], pid)
        row = self.con.execute("SELECT requirements FROM expected_placements"
                               " WHERE id=?", (pid,)).fetchone()[0]
        self.assertEqual(json.loads(row)["offer"], "Updated free trial")

    def test_draft_reports_invalid_reuse_override(self):
        p1 = add_placement(self.con, self.cid, expected_count=1,
                           episode_id="ep-1")
        add_candidate(self.con, "x")
        verify_full_ad(self.con, "x")
        self._attach(p1, citations.current_source_ref(self.con, "evidence", "x"))
        plain = campaigns.build_customer_draft(self.con, self.cid)
        oid = plain["allocation"]["evaluations"][p1]["eligible_ids"][0]
        draft = campaigns.build_customer_draft(
            self.con, self.cid, {oid: {"placement_id": p1, "rationale": ""}})
        self.assertIn(oid, draft["allocation"]["invalid_overrides"])


class ContradictedTest(unittest.TestCase):
    """A known verified requirement conflict is contradicted under the
    existing precedence, never no-observation, through the real draft."""

    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con)
        self.cid = seed_campaign(self.con)

    def tearDown(self):
        self.con.close()

    def test_verified_timing_conflict_is_contradicted(self):
        # Contradiction is only reachable on complete coverage: the
        # partial seed_episode transcript can never produce it.
        seed_full_episode(self.con, ep_id="ep-full")
        add_candidate(self.con, "mismatch", episode_id="ep-full")
        verify_full_ad(self.con, "mismatch", episode_id="ep-full")
        pid = add_placement(self.con, self.cid, expected_count=1,
                            episode_id="ep-full",
                            requirements={"duration_s": [20, 30]})
        rev = self.con.execute("SELECT revision FROM campaigns WHERE id=?",
                               (self.cid,)).fetchone()[0]
        campaigns.save_placement_evidence(self.con, self.cid, pid, citations.current_source_ref(self.con, "evidence", "mismatch"), rev, actor="owner")
        draft = campaigns.build_customer_draft(self.con, self.cid)
        ev = draft["allocation"]["evaluations"][pid]
        rows = draft["comparisons"][pid]
        duration = next(r for r in rows if r["field"] == "duration_s")
        self.assertEqual(duration["observed"], 10.0)
        self.assertEqual(duration["state"], "mismatch")
        self.assertEqual(
            draft["coverage_by_placement"][pid].get("state"), "complete")
        self.assertEqual(ev["outcome"], "contradicted")
        self.assertTrue(any("contradicted by verified evidence" in r
                            for r in ev["reasons"]))

    def test_unknown_timing_never_contradicts(self):
        # Same shape but unverified bounds: unknown stays unknown, never
        # promoted into a contradiction.
        add_candidate(self.con, "bare")
        ops.save_review(self.con, "bare", {"judgment": "ad"}, 0)
        ops.set_brand_assocs(self.con, "bare",
            [{"brand": "acme", "basis": "confirmed", "channel": "spoken"}], 0)
        pid = add_placement(self.con, self.cid, expected_count=1,
                            episode_id="ep-1",
                            requirements={"duration_s": [20, 30]})
        rev = self.con.execute("SELECT revision FROM campaigns WHERE id=?",
                               (self.cid,)).fetchone()[0]
        campaigns.save_placement_evidence(self.con, self.cid, pid, citations.current_source_ref(self.con, "evidence", "bare"), rev, actor="owner")
        draft = campaigns.build_customer_draft(self.con, self.cid)
        ev = draft["allocation"]["evaluations"][pid]
        self.assertNotEqual(ev["outcome"], "contradicted")


class AllocationConflictRetentionTest(unittest.TestCase):
    """A shared identity allocated to an earlier placement keeps its
    authoritative conflict on the later placement: contradicted with
    mismatch reasons, never a false no-observation."""

    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con)
        self.cid = seed_campaign(self.con)

    def tearDown(self):
        self.con.close()

    def _attach(self, pid, *refs):
        # Each attachment advances the owning campaign revision, so the
        # current revision is re-read per mutation; stale writes must 412.
        for ref in refs:
            rev = self.con.execute("SELECT revision FROM campaigns WHERE id=?",
                                   (self.cid,)).fetchone()[0]
            campaigns.save_placement_evidence(self.con, self.cid, pid, ref, rev)

    def test_shared_identity_contradiction_survives_allocation(self):
        # The contradicted placement needs complete coverage: full-duration
        # episode with recorded stage proof.
        seed_full_episode(self.con, ep_id="ep-full")
        add_candidate(self.con, "shared", episode_id="ep-full")
        verify_full_ad(self.con, "shared", episode_id="ep-full")  # observed duration 10
        pid_ok = add_placement(
            self.con, self.cid, episode_id="ep-full", date_to="2026-03-15",
            requirements={"duration_s": [5, 15]})
        pid_bad = add_placement(
            self.con, self.cid, episode_id="ep-full", date_to="2026-03-31",
            requirements={"duration_s": [20, 30]})
        self._attach(pid_ok, citations.current_source_ref(self.con, "evidence", "shared"))
        self._attach(pid_bad, citations.current_source_ref(self.con, "evidence", "shared"))
        draft = campaigns.build_customer_draft(self.con, self.cid)
        ok = draft["allocation"]["evaluations"][pid_ok]
        bad = draft["allocation"]["evaluations"][pid_bad]
        self.assertEqual(ok["outcome"], "supported")
        self.assertEqual(len(ok["eligible_ids"]), 1)
        self.assertEqual(bad["outcome"], "contradicted")
        self.assertEqual(bad["eligible_ids"], [])
        self.assertTrue(any("outside [20, 30]" in r for r in bad["reasons"]))
        self.assertTrue(any("contradicted by verified evidence" in r
                            for r in bad["reasons"]))
        (shared_oid,) = ok["eligible_ids"]
        self.assertIn(shared_oid, bad.get("allocated_elsewhere") or [])
        rows = draft["comparisons"][pid_bad]
        duration = next(r for r in rows if r["field"] == "duration_s")
        self.assertEqual(duration["state"], "mismatch")


class SyntheticPlacementTruthTest(unittest.TestCase):
    """Explicit synthetic placement provenance reaches the draft and the
    frozen report truth; synthetic evidence is never contractual."""

    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con)
        self.cid = seed_campaign(self.con)

    def tearDown(self):
        self.con.close()

    def test_synthetic_placements_propagate_to_draft_and_report(self):
        saved = campaigns.save_expected_placement(
            self.con, self.cid, scope="episode", episode_id="ep-1",
            date_from="2026-03-01", date_to="2026-03-31",
            timezone_name="UTC", expected_count=1,
            requirements={"duration_s": [5, 30]},
            provenance={"brief_ref": synthetic_brief(self.con)},
            priority=0, actor="owner")
        pid = saved["placement_id"]
        self.assertTrue(saved["synthetic"])
        add_candidate(self.con, "x")
        verify_full_ad(self.con, "x")
        rev = self.con.execute("SELECT revision FROM campaigns WHERE id=?",
                               (self.cid,)).fetchone()[0]
        out = campaigns.save_placement_evidence(self.con, self.cid, pid, citations.current_source_ref(self.con, "evidence", "x"), rev)
        rev = out["campaign_revision"]
        draft = campaigns.build_customer_draft(self.con, self.cid)
        self.assertFalse(draft["synthetic"])
        self.assertEqual(draft["synthetic_placements"], [pid])
        frozen = campaigns.freeze_customer_report(
            self.con, self.cid, rev, draft["pins"])
        status = campaigns.get_customer_report(
            self.con, self.cid, frozen["report_no"])
        self.assertEqual(status["frozen"]["synthetic_placements"], [pid])
        unit = next(unit for unit in status["frozen"]["placements"] if unit["id"] == pid)
        self.assertTrue(unit["provenance"]["brief_ref"]["qualification"]["synthetic"])
        self.assertFalse(unit["provenance"]["contractual"])

    def test_new_freeze_retains_source_labels_and_stored_digest(self):
        pid = add_placement(self.con, self.cid, episode_id="ep-1",
                            requirements={"duration_s": [5, 30]})
        add_candidate(self.con, "x")
        verify_full_ad(self.con, "x")
        rev = self.con.execute("SELECT revision FROM campaigns WHERE id=?",
                               (self.cid,)).fetchone()[0]
        out = campaigns.save_placement_evidence(self.con, self.cid, pid, citations.current_source_ref(self.con, "evidence", "x"), rev)
        rev = out["campaign_revision"]
        draft = campaigns.build_customer_draft(self.con, self.cid)
        frozen = campaigns.freeze_customer_report(
            self.con, self.cid, rev, draft["pins"])
        status = campaigns.get_customer_report(
            self.con, self.cid, frozen["report_no"])
        # Labels captured into the new frozen payload from the draft.
        self.assertEqual(status["frozen"]["episode_titles"].get("ep-1"), "Ep")
        body, digest = campaigns.get_customer_report_html(
            self.con, self.cid, frozen["report_no"])
        self.assertIn("Episode Ep", body)  # readable label, not a raw id
        self.assertIn("Ep", body)
        self.assertIn("supported", body)
        self.assertNotIn("<script", body)
        import hashlib
        self.assertEqual(digest, hashlib.sha256(body.encode()).hexdigest())

    def test_old_frozen_bytes_immutable(self):
        pid = add_placement(self.con, self.cid, episode_id="ep-1",
                            requirements={"duration_s": [5, 30]})
        add_candidate(self.con, "x")
        verify_full_ad(self.con, "x")
        rev = self.con.execute("SELECT revision FROM campaigns WHERE id=?",
                               (self.cid,)).fetchone()[0]
        out = campaigns.save_placement_evidence(self.con, self.cid, pid, citations.current_source_ref(self.con, "evidence", "x"), rev)
        rev = out["campaign_revision"]
        draft = campaigns.build_customer_draft(self.con, self.cid)
        frozen = campaigns.freeze_customer_report(
            self.con, self.cid, rev, draft["pins"])
        before_payload, before_digest = campaigns.get_customer_report_html(
            self.con, self.cid, frozen["report_no"])
        campaigns.save_customer_campaign(self.con, {"campaign": {"name": "Edited after capture"}},
                                         self.cid, rev)
        # Current campaign edits and subsequent reads never rewrite captured bytes.
        campaigns.get_customer_report(
            self.con, self.cid, frozen["report_no"])
        after_payload, after_digest = campaigns.get_customer_report_html(
            self.con, self.cid, frozen["report_no"])
        self.assertEqual(after_payload, before_payload)
        self.assertEqual(after_digest, before_digest)


class DisplayedBasisTest(unittest.TestCase):
    """Stale confirmations never overwrite: a freeze prepared from a
    displayed draft that another writer superseded 412s with no report."""

    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con)
        self.cid = seed_campaign(self.con)

    def tearDown(self):
        self.con.close()

    def test_stale_freeze_conflicts_without_report(self):
        pid = add_placement(self.con, self.cid, expected_count=1,
                            episode_id="ep-1")
        add_candidate(self.con, "x")
        verify_full_ad(self.con, "x")
        rev = self.con.execute("SELECT revision FROM campaigns WHERE id=?",
                               (self.cid,)).fetchone()[0]
        campaigns.save_placement_evidence(self.con, self.cid, pid, citations.current_source_ref(self.con, "evidence", "x"), rev, actor="owner")
        shown = campaigns.build_customer_draft(self.con, self.cid)
        # Another writer attaches before this confirmation submits.
        add_candidate(self.con, "y")
        rev2 = self.con.execute("SELECT revision FROM campaigns WHERE id=?",
                                (self.cid,)).fetchone()[0]
        campaigns.save_placement_evidence(self.con, self.cid, pid, citations.current_source_ref(self.con, "evidence", "y"), rev2, actor="owner")
        before = self.con.execute(
            "SELECT COUNT(*) FROM campaign_reports").fetchone()[0]
        with self.assertRaises(ops.Conflict):
            campaigns.freeze_customer_report(self.con, self.cid, rev,
                                             shown["pins"], actor="owner")
        after = self.con.execute(
            "SELECT COUNT(*) FROM campaign_reports").fetchone()[0]
        self.assertEqual(before, after)


class CoverageLabelTest(unittest.TestCase):
    """Coverage gaps read as episode titles with exact ids alongside;
    labels never enter the coverage digest."""

    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con)
        self.cid = seed_campaign(self.con)

    def tearDown(self):
        self.con.close()

    def test_gap_reasons_use_titles_with_ids_alongside(self):
        ev = campaigns.evaluate_one(
            placement("p1"),
            [],
            {"state": "incomplete", "missing": ["ep-1"], "failed": [],
             "episode_labels": {"ep-1": "Spring Launch"}},
            "2026-09-28T00:00:00+00:00")
        self.assertEqual(ev["outcome"], "incomplete_coverage")
        self.assertEqual(ev["reasons"][:2],
                         ["missing transcripts: ['Spring Launch']",
                          "failed fetches: []"])
        self.assertEqual(ev["coverage_gap_ids"],
                         {"missing": ["ep-1"], "failed": []})

    def test_gap_reasons_fall_back_to_ids_without_labels(self):
        ev = campaigns.evaluate_one(
            placement("p1"), [],
            {"state": "incomplete", "missing": ["ep-1"], "failed": []},
            "2026-09-28T00:00:00+00:00")
        self.assertIn("missing transcripts: ['ep-1']", ev["reasons"])

    def test_draft_maps_episode_titles(self):
        pid = add_placement(self.con, self.cid, expected_count=1,
                            episode_id="ep-1")
        add_candidate(self.con, "x")
        self._attach(pid, citations.current_source_ref(self.con, "evidence", "x"))
        draft = campaigns.build_customer_draft(self.con, self.cid)
        self.assertEqual(draft["episode_titles"].get("ep-1"), "Ep")

    def _attach(self, pid, *refs):
        # Each attachment advances the owning campaign revision, so the
        # current revision is re-read per mutation; stale writes must 412.
        for ref in refs:
            rev = self.con.execute("SELECT revision FROM campaigns WHERE id=?",
                                   (self.cid,)).fetchone()[0]
            campaigns.save_placement_evidence(self.con, self.cid, pid, ref, rev)


if __name__ == "__main__":
    unittest.main()
