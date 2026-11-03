"""P05 product regressions: complete-ad observations (stdlib unittest).

Disposable in-memory workspaces only. Each test fails on a plausible
implementation bug: spoken/URL conflation, unconditional brand splits,
silent transfer of reviews, lost history, or fabricated precision.
"""
from __future__ import annotations

import json
import sqlite3
import unittest

from earwitness.app import access, contracts, creatives, db, observations
from earwitness.app import ops


def owner_ctx():
    return access.AccessContext.create(
        principal_id="owner-1", principal_kind="owner",
        workspace_id="local", scopes={"research:read"},
        credential_kind="local_operator")


def fresh_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON;")
    db.init_schema(con)
    return con


SEGS = [
    {"start": 0.0, "end": 10.0, "text": "welcome to the show intro music"},
    {"start": 10.0, "end": 20.0, "text": "go to kachava dot com for 20 percent off today"},
    {"start": 20.0, "end": 30.0, "text": "use code SAVE20 at checkout for savings"},
    {"start": 30.0, "end": 40.0, "text": "back to the editorial segment now"},
]


def seed(con, ep="ep-1"):
    con.execute("INSERT INTO episodes (id, kind, fetch_date, updated_at)"
                " VALUES (?, 'supplied', '2026-01-02', '2026-01-02T00:00:00+00:00')", (ep,))
    cur = con.execute("INSERT INTO source_versions (source_key, version_no, sha256,"
                      " payload, is_active, created_at) VALUES (?, 1, 'abc', ?, 1, ?)",
                      (f"transcript:{ep}", json.dumps({"segments": SEGS}),
                       db.utcnow()))
    vid = cur.lastrowid
    con.execute("UPDATE episodes SET transcript_version_id=? WHERE id=?", (vid, ep))
    con.commit()
    return vid


def propose(con, vid, ep="ep-1", start=10.0, end=30.0, **kw):
    args = {"episode_id": ep, "start_s": start, "end_s": end,
            "bounds_evidence": {"segments": SEGS, "excerpt": "kachava offer"},
            "fields": {
                "spoken_brand": {"value": "unknown", "method": "deterministic",
                                 "segment_ref": None, "char_ref": None},
                "offer": {"value": "20 percent off", "method": "deterministic",
                          "segment_ref": 1, "char_ref": None},
                "read_type": {"value": "unknown"},
            },
            "attributions": [{"entity": "kachava.com", "raw_phrase": "kachava dot com",
                              "channel": "url", "method": "deterministic",
                              "status": "suggested"}],
            "source_pins": {"source_version_id": vid,
                            "transcript_version_id": vid,
                            "fetch_date": "2026-01-02",
                            "fetch_date_source": "episode"},
            "actor": "pipeline"}
    args.update(kw)
    return observations.propose_observation(con, **args)


class UrlOnlyAttributionTest(unittest.TestCase):
    def test_url_only_yields_assoc_only_with_spoken_unknown(self):
        con = fresh_db()
        try:
            vid = seed(con)
            out = propose(con, vid)
            self.assertEqual(out["status"], "proposed")
            detail = observations.get_observation_detail(con, out["observation_id"])
            channels = {a["channel"] for a in detail["attributions"]}
            self.assertIn("url", channels)
            self.assertNotIn("spoken", channels)
            self.assertEqual(detail["current_values"]["fields"]["spoken_brand"]["value"],
                             "unknown")
        finally:
            con.close()

    def test_red_capable_spoken_from_url_only_is_rejected(self):
        """RED-CAPABLE: spoken=Kachava inferred from URL alone must fail.

        Before: spoken=Kachava from URL (conflation). After: spoken unknown +
        URL association. This test quotes both paths.
        """
        con = fresh_db()
        try:
            vid = seed(con)
            before_error = None
            try:
                observations.propose_observation(
                    con, episode_id="ep-1", start_s=10.0, end_s=20.0,
                    bounds_evidence={"segments": SEGS},
                    fields={"read_type": {"value": "unknown"}},
                    attributions=[{"entity": "kachava",
                                   "raw_phrase": "kachava dot com",
                                   "channel": "spoken", "method": "deterministic",
                                   "status": "suggested"}],
                    source_pins={"source_version_id": vid,
                                 "transcript_version_id": vid},
                    actor="pipeline")
            except ops.BadInput as exc:
                before_error = str(exc)
            self.assertIsNotNone(before_error,
                                 "spoken attribution without spoken phrase must be rejected")
            self.assertIn("spoken", before_error.lower())
            after = propose(con, vid)
            detail = observations.get_observation_detail(con, after["observation_id"])
            self.assertEqual(detail["current_values"]["fields"]["spoken_brand"]["value"],
                             "unknown")
            print(f"RED-CAPABLE before(spoken-from-URL): rejected: {before_error}")
            print(f"RED-CAPABLE after: spoken="
                  f"{detail['current_values']['fields']['spoken_brand']['value']}"
                  f" + url assoc={[a['entity'] for a in detail['attributions']]}")
        finally:
            con.close()


class BoundaryContinuityTest(unittest.TestCase):
    def test_adjacent_cachava_birdie_is_split_cue_not_law(self):
        r = observations.inspect_commercial_continuity(
            ["cachava dot com deal", "sheez birdie personal safety"],
            ["kachava", "she's birdie"], ["cta_url", "cta_url"])
        self.assertEqual(r["decision"], "split")
        self.assertIn("cue", r["rationale"].lower())

    def test_cobranded_comparison_kept_together_with_rationale(self):
        r = observations.inspect_commercial_continuity(
            ["comparing cachava versus birdie for our listeners"],
            ["kachava", "she's birdie"], ["cta_url", "cta_url"])
        self.assertEqual(r["decision"], "keep")
        self.assertTrue(r["rationale"])

    def test_marker_only_is_uncertain_never_unconditional(self):
        r = observations.inspect_commercial_continuity(
            ["use the code at checkout"], [], ["marker_only"])
        self.assertEqual(r["decision"], "uncertain")

    def test_segment_crossing_keeps_uncertainty_without_word_times(self):
        out = observations.validate_bounds_against_segments(15.0, 25.0, SEGS)
        self.assertTrue(out["uncertain"])
        exact = observations.validate_bounds_against_segments(
            10.0, 30.0, SEGS, word_timestamps=True)
        self.assertFalse(exact["uncertain"])

    def test_classifier_without_cited_text_rejected(self):
        con = fresh_db()
        try:
            vid = seed(con)
            with self.assertRaises(ops.BadInput):
                observations.validate_field_refs(con, vid, {
                    "spoken_brand": {"value": "kachava", "method": "classifier:v1",
                                     "segment_ref": None}})
            with self.assertRaises(ops.BadInput):
                observations.validate_field_refs(con, vid, {
                    "offer": {"value": "x", "method": "deterministic",
                              "segment_ref": 99}})
        finally:
            con.close()


class SplitMergeLineageTest(unittest.TestCase):
    def test_split_preserves_history_invalidates_without_transfer(self):
        con = fresh_db()
        try:
            vid = seed(con)
            parent = propose(con, vid)["observation_id"]
            # Downstream relations pinned to the parent.
            creatives._ensure_aux(con)
            cr = creatives.create_creative(con, [parent])["creative_id"]
            creatives.create_occurrence(con, "ep-1", parent, 1)
            prev = observations.preview_split(
                con, parent, [{"start_s": 10.0, "end_s": 20.0},
                              {"start_s": 20.0, "end_s": 30.0}])
            self.assertIn("creative_memberships", prev["affected"])
            self.assertTrue(prev["affected"]["creative_memberships"])
            out = observations.split_observation(
                con, parent, [{"start_s": 10.0, "end_s": 20.0},
                              {"start_s": 20.0, "end_s": 30.0}],
                expected_revision=1, rationale="supported split cue", actor="owner",
                ctx=owner_ctx())
            self.assertEqual(len(out["children"]), 2)
            hist = observations.revision_history(con, parent)
            self.assertEqual(len(hist), 2)
            self.assertEqual(hist[-1]["values"]["status"], "superseded")
            for cid in out["children"]:
                mem = con.execute("SELECT COUNT(*) c FROM creative_memberships"
                                  " WHERE creative_id=? AND observation_id=?",
                                  (cr, cid)).fetchone()["c"]
                self.assertEqual(mem, 0, "no silent transfer of creative membership")
            self.assertIn("observation_id", out["invalidation"])
        finally:
            con.close()

    def test_merge_atomic_with_revision_conflict(self):
        con = fresh_db()
        try:
            vid = seed(con)
            a = propose(con, vid, start=10.0, end=20.0)["observation_id"]
            b = propose(con, vid, start=20.0, end=30.0)["observation_id"]
            with self.assertRaises(ops.Conflict):
                observations.merge_observations(
                    con, [a, b], {"start_s": 10.0, "end_s": 30.0},
                    {a: 1, b: 999}, rationale="merge", ctx=owner_ctx())
            status, _ = contracts.error_for(ops.Conflict(1))
            self.assertEqual(status, 412)
            out = observations.merge_observations(
                con, [a, b], {"start_s": 10.0, "end_s": 30.0},
                {a: 1, b: 1}, rationale="adjacent same-sponsor merge",
                ctx=owner_ctx())
            self.assertTrue(out["child"])
            for oid in (a, b):
                st = con.execute("SELECT status FROM ad_observations WHERE id=?",
                                 (oid,)).fetchone()["status"]
                self.assertEqual(st, "superseded")
        finally:
            con.close()

    def test_machine_cannot_review_human_can(self):
        con = fresh_db()
        try:
            vid = seed(con)
            oid = propose(con, vid)["observation_id"]
            with self.assertRaises(ops.BadInput):
                observations.review_observation(con, oid, "reviewed_ad", 1,
                                                actor="pipeline", ctx=owner_ctx())
            out = observations.review_observation(con, oid, "reviewed_ad", 1,
                                                  actor="owner", ctx=owner_ctx())
            self.assertEqual(out["status"], "reviewed_ad")
            with self.assertRaises(ops.Conflict):
                observations.review_observation(con, oid, "unsure", 1, actor="owner",
                                                ctx=owner_ctx())
        finally:
            con.close()


class ReadPathTest(unittest.TestCase):
    def test_candidate_vs_observation_and_effective_text(self):
        con = fresh_db()
        try:
            vid = seed(con)
            con.execute("INSERT INTO evidence (id, source, episode_id, excerpt, created_at)"
                        " VALUES ('cand-1', 'detector', 'ep-1', 'raw window', ?)",
                        (db.utcnow(),))
            con.commit()
            oid = propose(con, vid)["observation_id"]
            proj = con.execute("SELECT id FROM evidence WHERE record_kind='observation'"
                               " LIMIT 1").fetchone()["id"]
            cand = observations.describe_record(con, "cand-1")
            obs = observations.describe_record(con, proj)
            self.assertTrue(cand["is_candidate"])
            self.assertTrue(obs["is_observation"])
            self.assertEqual(obs["observation"]["observation_id"], oid)
            eff = observations.effective_text(con, oid)
            self.assertTrue(eff["effective_text"])
            self.assertIn("full version", eff["reason"])
            payload = observations.observation_review_payload(con, oid)
            self.assertEqual(payload["record_kind"], "observation")
            self.assertTrue(any(a["kind"] == "native-button" for a in payload["actions"]))
            row = observations.observation_export_row(con, oid)
            self.assertEqual(row["fetch_date_source"], "episode")
        finally:
            con.close()


class IdempotencyLookbackTest(unittest.TestCase):
    def test_retry_resolves_original_after_fifty_subsequent_rows(self):
        """RED-CAPABLE (W4): keyed idempotency lookup over the full table.

        Before: _check_idempotency scanned only the newest 50 audit rows,
        so a retry with a previously used (key, digest) pair after 50+
        subsequent proposes minted a duplicate observation. After: the
        lookup is keyed by (idempotency_key, digest) over the full table
        and the retry returns the original with duplicate=True.
        """
        con = fresh_db()
        try:
            vid = seed(con)
            first = propose(con, vid, idempotency_key="K")
            target = first["observation_id"]
            digest = json.loads(con.execute(
                "SELECT detail FROM audit WHERE action='observation_propose'"
                " AND target_type='observation' ORDER BY id ASC LIMIT 1"
            ).fetchone()["detail"])["digest"]
            for i in range(54):
                db.audit(con, "pipeline", "observation_propose", "observation",
                         f"obs-gone-{i}",
                         {"revision": 1, "digest": digest, "idempotency_key": "K",
                          "observation_id": f"obs-gone-{i}"}, i + 2)
            con.commit()
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM audit"
                            " WHERE action='observation_propose'"
                            " AND target_type='observation'").fetchone()[0], 55)
            self.assertEqual(observations._check_idempotency(con, "K", digest),
                             target)
            retry = propose(con, vid, idempotency_key="K")
            self.assertTrue(retry.get("duplicate", False))
            self.assertEqual(retry["observation_id"], target)
        finally:
            con.close()


class EpisodeGuardTest(unittest.TestCase):
    """F4-N3: the dead `if ... and False` guard is gone; unknown episodes
    still raise BadInput (pinned or unpinned fetch_date)."""

    def _attempt(self, con, pins):
        return observations.propose_observation(
            con, episode_id="ep-nope", start_s=10.0, end_s=20.0,
            bounds_evidence={"segments": SEGS},
            fields={"read_type": {"value": "unknown"}},
            attributions=[], source_pins=pins, actor="pipeline")

    def test_unknown_episode_rejected_without_fetch_date_pin(self):
        con = fresh_db()
        try:
            seed(con)
            with self.assertRaises(ops.BadInput) as caught:
                self._attempt(con, {"source_version_id": 1,
                                    "transcript_version_id": 1})
            self.assertIn("unknown episode", str(caught.exception))
        finally:
            con.close()

    def test_unknown_episode_rejected_with_fetch_date_pin(self):
        con = fresh_db()
        try:
            seed(con)
            with self.assertRaises(ops.BadInput) as caught:
                self._attempt(con, {"fetch_date": "2026-01-02",
                                    "fetch_date_source": "episode"})
            self.assertIn("unknown episode", str(caught.exception))
        finally:
            con.close()


class LeafCtxEnforcementTest(unittest.TestCase):
    """F4-N4: split/merge/revise leaves reject ctx=None; the dispatcher
    threads the server's owner ctx through so local flows keep working."""

    def _split_args(self):
        return [{"start_s": 10.0, "end_s": 20.0},
                {"start_s": 20.0, "end_s": 30.0}]

    def test_leaves_reject_none_ctx(self):
        con = fresh_db()
        try:
            vid = seed(con)
            a = propose(con, vid, start=10.0, end=20.0)["observation_id"]
            b = propose(con, vid, start=20.0, end=30.0)["observation_id"]
            c = propose(con, vid, start=10.0, end=30.0)["observation_id"]
            with self.assertRaises(ops.BadInput):
                observations.split_observation(
                    con, a, self._split_args(), expected_revision=1,
                    rationale="probe", actor="owner", ctx=None)
            with self.assertRaises(ops.BadInput):
                observations.merge_observations(
                    con, [a, b], {"start_s": 10.0, "end_s": 30.0},
                    {a: 1, b: 1}, rationale="probe", ctx=None)
            with self.assertRaises(ops.BadInput):
                observations.revise_observation(
                    con, c, None, expected_revision=1, rationale="probe",
                    actor="owner", idempotency_key="k-probe", ctx=None)
        finally:
            con.close()

    def test_owner_ctx_flows_keep_working(self):
        con = fresh_db()
        try:
            vid = seed(con)
            a = propose(con, vid, start=10.0, end=20.0)["observation_id"]
            out = observations.split_observation(
                con, a, self._split_args(), expected_revision=1,
                rationale="supported split cue", actor="owner", ctx=owner_ctx())
            self.assertEqual(len(out["children"]), 2)
            b = propose(con, vid, start=10.0, end=20.0)["observation_id"]
            c = propose(con, vid, start=20.0, end=30.0)["observation_id"]
            merged = observations.merge_observations(
                con, [b, c], {"start_s": 10.0, "end_s": 30.0},
                {b: 1, c: 1}, rationale="adjacent same-sponsor merge",
                ctx=owner_ctx())
            self.assertTrue(merged["child"])
            d = propose(con, vid)["observation_id"]
            revised = observations.revise_observation(
                con, d, None, expected_revision=1, rationale="refine",
                actor="owner", idempotency_key="k-rev", ctx=owner_ctx())
            self.assertEqual(revised["revision"], 2)
        finally:
            con.close()

    def test_dispatcher_threads_owner_ctx(self):
        con = fresh_db()
        try:
            vid = seed(con)
            first = propose(con, vid)
            rid = con.execute("SELECT id FROM observation_revisions"
                              " WHERE observation_id=?",
                              (first["observation_id"],)).fetchone()["id"]
            chg = contracts.ObservationChange(
                operation="split", input_revision_ids=(rid,), rationale="r")
            with self.assertRaises(ops.BadInput):
                observations.save_observation_service(
                    None, con, chg, outputs=self._split_args(),
                    idempotency_key="k-n4-none")
            out = observations.save_observation_service(
                owner_ctx(), con, chg, outputs=self._split_args(),
                idempotency_key="k-n4-owner")
            self.assertEqual(len(out["children"]), 2)
        finally:
            con.close()


class EvidenceProjectionPinTest(unittest.TestCase):
    def test_proposed_evidence_carries_transcript_pin_and_searches(self):
        """RED-CAPABLE: proposed evidence must stay visible in /evidence search.

        Before: the evidence projection omitted transcript_version_id, so the
        pinned-episode search clause (e.transcript_version_id IS
        ep.transcript_version_id) dropped every proposed row. After: the
        projection carries the FK-safe source_versions pin and q matches it.
        """
        con = fresh_db()
        try:
            vid = seed(con)
            out = propose(con, vid)
            erow = con.execute("SELECT transcript_version_id FROM evidence WHERE id=?",
                               (out["evidence_id"],)).fetchone()
            self.assertEqual(erow["transcript_version_id"], vid)
            res = ops.search_evidence(con, {"q": "kachava", "page": 1, "per_page": 50})
            self.assertIn(out["evidence_id"], [i["id"] for i in res["items"]])
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
