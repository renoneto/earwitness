"""P12 product regressions: tenancy isolation (stdlib unittest)."""
from __future__ import annotations

import json
import sqlite3
import unittest

from earwitness.app import access, collaboration, creatives, db, observations


def fresh_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON;")
    db.init_schema(con)
    collaboration.ensure_aux(con)
    return con


_TENANCY_SEGS = [
    {"start": 0.0, "end": 15.0, "text": "go to kachava dot com use code SAVE20 for 20 percent off"},
    {"start": 15.0, "end": 30.0, "text": "back to editorial content here"},
]


def _seed_episode(con, ep="ep-1"):
    con.execute("INSERT INTO episodes (id, kind, fetch_date, updated_at)"
                " VALUES (?, 'supplied', '2026-01-02', '2026-01-02T00:00:00+00:00')", (ep,))
    cur = con.execute("INSERT INTO source_versions (source_key, version_no, sha256,"
                      " payload, is_active, created_at) VALUES (?, 1, 'abc', ?, 1, ?)",
                      (f"transcript:{ep}", json.dumps({"segments": _TENANCY_SEGS}),
                       db.utcnow()))
    vid = cur.lastrowid
    con.execute("UPDATE episodes SET transcript_version_id=? WHERE id=?", (vid, ep))
    con.commit()
    return vid


def _propose_observation(con, vid, start=0.0, end=15.0):
    return observations.propose_observation(
        con, episode_id="ep-1", start_s=start, end_s=end,
        bounds_evidence={"segments": _TENANCY_SEGS},
        fields={"read_type": {"value": "unknown"}},
        attributions=[{"entity": "kachava.com", "raw_phrase": "kachava dot com",
                       "channel": "url", "method": "deterministic",
                       "status": "suggested"}],
        source_pins={"source_version_id": vid, "transcript_version_id": vid},
        actor="pipeline")["observation_id"]


class TenancyIsolationTest(unittest.TestCase):
    def test_two_accounts_cannot_infer_each_other(self):
        con = fresh_db()
        try:
            collaboration.create_workspace(con, "ws-a", "A", "alice")
            collaboration.create_workspace(con, "ws-b", "B", "bob")
            # Membership is workspace-scoped: no cross reads.
            self.assertFalse(collaboration.is_member(con, "ws-a", "bob"))
            self.assertFalse(collaboration.is_member(con, "ws-b", "alice"))
            self.assertFalse(collaboration.check_permission(con, "ws-a", "bob", "read"))
            # Workspace ids are explicit; guessing another workspace's
            # object ids grants nothing without membership.
            self.assertIsNone(collaboration.get_role(con, "ws-a", "bob"))
        finally:
            con.close()

    def test_revoked_user_loses_future_access(self):
        con = fresh_db()
        try:
            collaboration.create_workspace(con, "ws-1", "W", "ann")
            collaboration.create_invite(con, "ws-1", "ed", "editor", "ann")
            collaboration.accept_invite(
                con, con.execute("SELECT id FROM invites").fetchone()["id"], "ed")
            self.assertTrue(collaboration.check_permission(con, "ws-1", "ed", "comment"))
            collaboration.revoke_member(con, "ws-1", "ed", "ann")
            self.assertFalse(collaboration.check_permission(con, "ws-1", "ed", "comment"))
            self.assertFalse(collaboration.check_permission(con, "ws-1", "ed", "review"))
            self.assertFalse(collaboration.is_member(con, "ws-1", "ed"))
        finally:
            con.close()

    def test_revoked_share_loses_future_access_bounded_inflight(self):
        """Revocation stops future reads; in-flight bytes are not recalled."""
        con = fresh_db()
        try:
            collaboration.create_workspace(con, "ws-1", "W", "ann")
            s = collaboration.create_share(con, "ws-1", "evidence", "ev-1",
                                           "guest-a", ["excerpt"], "ann")
            sid = s["share_id"]
            guest = access.AccessContext.create(
                principal_id="guest-a", principal_kind="guest",
                workspace_id="ws-1", authority_kind="share",
                share_grant_id=sid, credential_kind="browser_session")
            self.assertTrue(collaboration.check_share_access(
                con, guest, "evidence_detail", "evidence", "ev-1")["allowed"])
            collaboration.revoke_share(con, sid, "ann", expected_revision=1)
            with self.assertRaises(access.AuthorizationError):
                collaboration.check_share_access(con, guest, "evidence_detail",
                                                 "evidence", "ev-1")
            self.assertIn("not delivered copies",
                          collaboration.share_state(con, sid)["no_recall_notice"])
        finally:
            con.close()

    def test_migration_rehearsal_missing_history_stays_missing(self):
        con = fresh_db()
        try:
            out = collaboration.rehearse_tenant_migration(con)
            self.assertIn("composite_workspace_keys", out)
            self.assertTrue(len(out["composite_workspace_keys"]) >= 1)
            self.assertEqual(out["missing_history"],
                             "missing historical revisions stay missing")
            for table, info in out["moved"].items():
                self.assertEqual(info["historical_revisions_invented"], 0)
        finally:
            con.close()

    def test_guest_cannot_workspace_browse_or_agent_scope(self):
        con = fresh_db()
        try:
            collaboration.create_workspace(con, "ws-1", "W", "ann")
            s = collaboration.create_share(con, "ws-1", "evidence", "ev-1",
                                           "guest-a", ["excerpt"], "ann")
            guest = access.AccessContext.create(
                principal_id="guest-a", principal_kind="guest",
                workspace_id="ws-1", authority_kind="share",
                share_grant_id=s["share_id"], credential_kind="browser_session")
            for op in ("save_observation", "submit_processing", "search_transcripts"):
                with self.assertRaises(access.AuthorizationError):
                    guest.require_operation(op)
        finally:
            con.close()

    def test_guest_suggest_memberships_matches_fingerprint_gate(self):
        """RED-CAPABLE (W3): guest share ctx must not read creative suggestions.

        Before: suggest_memberships returned suggestions for a guest ctx
        while fingerprint_observation on the same ctx raised
        AuthorizationError. After: both raise; ctx=None still works
        (loopback posture unchanged).
        """
        con = fresh_db()
        try:
            vid = _seed_episode(con)
            a = _propose_observation(con, vid)
            b = _propose_observation(con, vid)
            creatives.fingerprint_observation(
                con, a,
                transcript_text="go to kachava dot com use code SAVE20 for 20 percent off today")
            creatives.fingerprint_observation(
                con, b,
                transcript_text="go to kachava dot com use code SAVE30 for 20 percent off today")
            guest = access.AccessContext.create(
                principal_id="guest-a", principal_kind="guest",
                workspace_id="ws-1", authority_kind="share",
                share_grant_id="share-1", credential_kind="browser_session")
            with self.assertRaises(access.AuthorizationError):
                creatives.fingerprint_observation(
                    con, a, transcript_text="guest attempt", ctx=guest)
            with self.assertRaises(access.AuthorizationError):
                creatives.suggest_memberships(con, a, ctx=guest)
            out = creatives.suggest_memberships(con, a, ctx=None)
            self.assertTrue(out["suggestions"])
        finally:
            con.close()


_W5_SEGS = '{"segments": [{"start": 0.0, "end": 5.0, "text": "hello"}]}'


def _w5_seed(con):
    """Workspace + two episodes/evidence + three shares. Returns ids."""
    collaboration.create_workspace(con, "ws-1", "W", "ann")
    vids = {}
    for ep in ("ep-1", "ep-2"):
        con.execute("INSERT INTO episodes (id, kind, fetch_date, updated_at)"
                    " VALUES (?, 'supplied', '2026-01-02',"
                    " '2026-01-02T00:00:00+00:00')", (ep,))
        cur = con.execute(
            "INSERT INTO source_versions (source_key, version_no, sha256,"
            " payload, is_active, created_at) VALUES (?, 1, 'abc', ?, 1,"
            " '2026-01-02T00:00:00+00:00')",
            (f"transcript:{ep}", _W5_SEGS))
        vids[ep] = cur.lastrowid
        con.execute("UPDATE episodes SET transcript_version_id=? WHERE id=?",
                    (cur.lastrowid, ep))
    con.execute("INSERT INTO evidence (id, source, episode_id, excerpt,"
                " transcript_version_id, created_at)"
                " VALUES ('ev-1', 'manual', 'ep-1', 'alpha', ?,"
                " '2026-01-02T00:00:00+00:00')", (vids["ep-1"],))
    con.execute("INSERT INTO evidence (id, source, episode_id, excerpt,"
                " transcript_version_id, created_at)"
                " VALUES ('ev-2', 'manual', 'ep-2', 'beta', ?,"
                " '2026-01-02T00:00:00+00:00')", (vids["ep-2"],))
    con.commit()
    ev_share = collaboration.create_share(con, "ws-1", "evidence", "ev-1",
                                          "guest-a", ["excerpt"], "ann")
    ep_share = collaboration.create_share(con, "ws-1", "episode", "ep-1",
                                          "guest-b", ["excerpt"], "ann")
    exp_share = collaboration.create_share(
        con, "ws-1", "evidence", "ev-1", "guest-c", ["excerpt"], "ann",
        requested_expiry="2020-01-01T00:00:00+00:00")
    return ev_share["share_id"], ep_share["share_id"], exp_share["share_id"]


def _w5_guest(share_id, scopes=()):
    return access.AccessContext.create(
        principal_id="guest-a", principal_kind="guest",
        workspace_id="ws-1", authority_kind="share",
        share_grant_id=share_id, scopes=scopes,
        credential_kind="browser_session")


def _w5_member():
    return access.AccessContext.create(
        principal_id="ann", principal_kind="owner",
        workspace_id="ws-1", scopes={"research:read", "transcripts:read"},
        credential_kind="local_operator")


class ShareGuestServiceReadsTest(unittest.TestCase):
    """RED-CAPABLE (W5): services reads enforce the share grant object.

    Before: evidence_detail enforced only the scope gate, so a scopeless
    guest was denied the allowlisted op entirely while a scope-holding
    guest read ANY evidence id with no grant-object comparison. After:
    allowlisted share reads enforce the grant (object match, expiry) via
    check_share_access instead of the scope gate; member paths unchanged.
    """

    def test_evidence_detail_enforces_grant_object(self):
        from earwitness.app import services
        con = fresh_db()
        try:
            ev_share, _, _ = _w5_seed(con)
            # Brief failing input: scope-holding guest reads ev-2 (must raise).
            scoped = _w5_guest(ev_share, scopes=("research:read",))
            with self.assertRaises(access.AuthorizationError):
                services.evidence_detail(scoped, con, "ev-2")
            # Locked posture: scopeless guest reads granted object, not other.
            guest = _w5_guest(ev_share)
            self.assertEqual(
                services.evidence_detail(guest, con, "ev-1")["id"], "ev-1")
            with self.assertRaises(access.AuthorizationError):
                services.evidence_detail(guest, con, "ev-2")
        finally:
            con.close()

    def test_evidence_detail_expired_grant_denied(self):
        from earwitness.app import services
        con = fresh_db()
        try:
            _, _, exp_share = _w5_seed(con)
            self.assertEqual(
                collaboration.share_state(con, exp_share)["state"], "expired")
            scoped = _w5_guest(exp_share, scopes=("research:read",))
            with self.assertRaises(access.AuthorizationError):
                services.evidence_detail(scoped, con, "ev-1")
            with self.assertRaises(access.AuthorizationError):
                services.evidence_detail(_w5_guest(exp_share), con, "ev-1")
        finally:
            con.close()

    def test_transcript_window_enforces_grant_object(self):
        from earwitness.app import services
        con = fresh_db()
        try:
            ev_share, excerpt_only_share, _ = _w5_seed(con)
            # Transcript content needs an explicit content-field grant
            # (services.transcript_window): an excerpt-only episode share
            # reaches the granted object but not its transcript.
            with self.assertRaisesRegex(access.AuthorizationError,
                                        "share does not grant transcript content"):
                services.transcript_window(_w5_guest(excerpt_only_share), con, "ep-1")
            ep_share = collaboration.create_share(
                con, "ws-1", "episode", "ep-1", "guest-b",
                ["excerpt", "segments"], "ann")["share_id"]
            scoped = _w5_guest(ep_share, scopes=("transcripts:read",))
            with self.assertRaises(access.AuthorizationError):
                services.transcript_window(scoped, con, "ep-2")
            ep_guest = _w5_guest(ep_share)
            window = services.transcript_window(ep_guest, con, "ep-1")
            self.assertTrue(window["segments"])
            with self.assertRaises(access.AuthorizationError):
                services.transcript_window(ep_guest, con, "ep-2")
            # Evidence grant does not traverse to the parent transcript.
            with self.assertRaises(access.AuthorizationError):
                services.transcript_window(_w5_guest(ev_share), con, "ep-1")
            # Member path unchanged.
            self.assertTrue(
                services.transcript_window(_w5_member(), con, "ep-2")["segments"])
        finally:
            con.close()

    def test_search_scoped_to_grant(self):
        from earwitness.app import services
        con = fresh_db()
        try:
            ev_share, ep_share, exp_share = _w5_seed(con)
            scoped = _w5_guest(ev_share, scopes=("research:read",))
            page = services.search_evidence(scoped, con, {"query": ""})
            self.assertEqual([i["id"] for i in page["items"]], ["ev-1"])
            page = services.search_evidence(_w5_guest(ev_share), con, {"query": ""})
            self.assertEqual([i["id"] for i in page["items"]], ["ev-1"])
            self.assertEqual(page["total_matches"], 1)
            page = services.search_evidence(_w5_guest(ep_share), con, {"query": ""})
            self.assertEqual([i["id"] for i in page["items"]], ["ev-1"])
            # Conflicting episode selector is traversal: denied.
            with self.assertRaises(access.AuthorizationError):
                services.search_evidence(_w5_guest(ep_share), con,
                                         {"query": "", "episode_id": "ep-2"})
            with self.assertRaises(access.AuthorizationError):
                services.search_evidence(_w5_guest(exp_share), con, {"query": ""})
            # Member path unchanged: both objects visible.
            page = services.search_evidence(_w5_member(), con, {"query": ""})
            self.assertEqual(sorted(i["id"] for i in page["items"]),
                             ["ev-1", "ev-2"])
            self.assertEqual(page["total_matches"], 2)
        finally:
            con.close()


class ShareGuestFieldProjectionTest(unittest.TestCase):
    """RED-CAPABLE (R3-1 major): guest payloads honor the field allowlist.

    Before: a guest granted one evidence id with fields=['excerpt']
    received the full ops payload (review judgment, brand assocs,
    provenance, overlaps disclosing neighboring out-of-grant evidence),
    and search items carried judgment/assocs/bounds unfiltered. After:
    guest payloads project to granted fields (identity keys always kept),
    overlaps filter to in-grant evidence only, member paths unchanged.
    """

    def _seed(self, con):
        ev_share, ep_share, _ = _w5_seed(con)
        con.execute("INSERT INTO reviews (evidence_id, judgment, updated_at)"
                    " VALUES ('ev-1', 'ad', '2026-01-03T00:00:00+00:00')")
        con.execute("INSERT INTO brand_assocs (evidence_id, brand, basis,"
                    " origin, revision, updated_at)"
                    " VALUES ('ev-1', 'kachava', 'x', 'agent', 1,"
                    " '2026-01-03T00:00:00+00:00')")
        vid = con.execute("SELECT transcript_version_id v FROM episodes"
                          " WHERE id='ep-1'").fetchone()["v"]
        # Field projection is the subject here, so ev-1 cites its
        # transcript as source. The null-source case (cluster M, a product
        # bug fixed separately) stays covered by
        # ShareGuestServiceReadsTest.test_evidence_detail_enforces_grant_object.
        con.execute("UPDATE evidence SET orig_start_s=0.0, orig_end_s=10.0,"
                    " source_version_id=? WHERE id='ev-1'", (vid,))
        con.execute("INSERT INTO evidence (id, source, episode_id, excerpt,"
                    " transcript_version_id, created_at, orig_start_s,"
                    " orig_end_s) VALUES ('ev-9', 'manual', 'ep-1', 'neighbor',"
                    " ?, '2026-01-02T00:00:00+00:00', 5.0, 15.0)", (vid,))
        con.commit()
        return ev_share, ep_share

    def test_evidence_detail_projects_to_granted_fields(self):
        from earwitness.app import services
        con = fresh_db()
        try:
            ev_share, _ = self._seed(con)
            detail = services.evidence_detail(_w5_guest(ev_share), con, "ev-1")
            self.assertEqual(detail["id"], "ev-1")
            self.assertEqual(detail["excerpt"], "alpha")
            for leaked in ("review", "assocs", "overlaps", "provenance",
                           "transcript_provenance", "judgment"):
                self.assertNotIn(leaked, detail)
            self.assertNotIn("judgment", detail["citation"])
            self.assertEqual(detail["citation"]["evidence_id"], "ev-1")
            # Member path unchanged: full payload with review judgment.
            full = services.evidence_detail(_w5_member(), con, "ev-1")
            self.assertEqual(full["review"]["judgment"], "ad")
            self.assertTrue(full["assocs"])
        finally:
            con.close()

    def test_evidence_detail_overlaps_filtered_to_grant_even_when_granted(self):
        from earwitness.app import collaboration as _collab
        from earwitness.app import services
        con = fresh_db()
        try:
            self._seed(con)
            s = _collab.create_share(con, "ws-1", "evidence", "ev-1",
                                     "guest-b", ["excerpt", "overlaps"], "ann")
            detail = services.evidence_detail(_w5_guest(s["share_id"]), con,
                                              "ev-1")
            # ev-9 overlaps ev-1 but is outside this single-object grant.
            self.assertEqual(detail["overlaps"], [])
        finally:
            con.close()

    def test_search_items_project_to_granted_fields(self):
        from earwitness.app import services
        con = fresh_db()
        try:
            ev_share, ep_share = self._seed(con)
            for sid in (ev_share, ep_share):
                page = services.search_evidence(_w5_guest(sid), con,
                                                {"query": ""})
                self.assertTrue(page["items"])
                for item in page["items"]:
                    self.assertIn(item["id"], ("ev-1", "ev-9"))
                    for leaked in ("judgment", "assocs", "review"):
                        self.assertNotIn(leaked, item)
            # Member path unchanged: judgment still served.
            page = services.search_evidence(_w5_member(), con, {"query": ""})
            self.assertTrue(any("judgment" in i for i in page["items"]))
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
