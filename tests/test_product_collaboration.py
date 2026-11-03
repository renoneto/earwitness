"""P12 product regressions: collaboration (stdlib unittest).

Disposable in-memory workspaces only. Invites never send.
"""
from __future__ import annotations

import sqlite3
import unittest

from earwitness.app import access, citations, collaboration, db
from earwitness.app import ops


def fresh_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON;")
    db.init_schema(con)
    collaboration.ensure_aux(con)
    return con


def member_ctx(member, workspace="ws-1", kind="editor"):
    return access.AccessContext.create(principal_id=member, principal_kind=kind,
                                       workspace_id=workspace,
                                       scopes=("research:read",),
                                       credential_kind="local_operator")


class RolesTest(unittest.TestCase):
    def test_owner_editor_viewer_matrix(self):
        con = fresh_db()
        try:
            collaboration.create_workspace(con, "ws-1", "W", "ann")
            collaboration.create_invite(con, "ws-1", "ed", "editor", "ann")
            collaboration.accept_invite(
                con, con.execute("SELECT id FROM invites").fetchone()["id"], "ed")
            collaboration.create_invite(con, "ws-1", "vi", "viewer", "ann")
            collaboration.accept_invite(
                con, con.execute("SELECT id FROM invites WHERE member='vi'").fetchone()["id"], "vi")
            self.assertTrue(collaboration.check_permission(con, "ws-1", "ann", "sharing"))
            self.assertFalse(collaboration.check_permission(con, "ws-1", "ed", "sharing"))
            self.assertTrue(collaboration.check_permission(con, "ws-1", "ed", "comment"))
            self.assertFalse(collaboration.check_permission(con, "ws-1", "vi", "comment"))
            # Editor processes ONLY with explicit grant.
            self.assertFalse(collaboration.check_permission(con, "ws-1", "ed", "process"))
            collaboration.grant_processing(con, "ws-1", "ed", "ann")
            self.assertTrue(collaboration.check_permission(con, "ws-1", "ed", "process"))
        finally:
            con.close()

    def test_invite_mechanics_never_send(self):
        con = fresh_db()
        try:
            collaboration.create_workspace(con, "ws-1", "W", "ann")
            inv = collaboration.create_invite(con, "ws-1", "bob", "viewer", "ann")
            self.assertFalse(inv["sent"])
        finally:
            con.close()

    def test_accept_invite_actor_must_match_invited_member(self):
        con = fresh_db()
        try:
            collaboration.create_workspace(con, "ws-1", "W", "ann")
            inv = collaboration.create_invite(con, "ws-1", "bob", "viewer", "ann")
            with self.assertRaises(access.AuthorizationError):
                collaboration.accept_invite(con, inv["invite_id"], "mallory")
            self.assertFalse(collaboration.is_member(con, "ws-1", "bob"))
            self.assertFalse(collaboration.is_member(con, "ws-1", "mallory"))
            got = collaboration.accept_invite(con, inv["invite_id"], "bob")
            self.assertEqual(got["member"], "bob")
            self.assertTrue(collaboration.is_member(con, "ws-1", "bob"))
        finally:
            con.close()

    def test_no_password_crypto_fake_signin(self):
        shape = collaboration.oidc_provider_shape()
        self.assertEqual(shape["provider_selection"], "unresolved")
        self.assertEqual(shape["password_crypto"], "not_implemented_by_design")


class AssignmentsCommentsTest(unittest.TestCase):
    def test_assignment_reassignment_keeps_judgment(self):
        con = fresh_db()
        try:
            collaboration.create_workspace(con, "ws-1", "W", "ann")
            collaboration.create_invite(con, "ws-1", "ed", "editor", "ann")
            collaboration.accept_invite(
                con, con.execute("SELECT id FROM invites").fetchone()["id"], "ed")
            a1 = collaboration.save_assignment(con, "ws-1", "observation", "obs-1",
                                               "ed", "open", "ann")
            a2 = collaboration.save_assignment(con, "ws-1", "observation", "obs-1",
                                               "ed", "in_progress", "ann",
                                               expected_revision=a1["revision"])
            self.assertFalse(a2["judgment_changed"])
            got = collaboration.get_assignment(con, "ws-1", "observation", "obs-1")
            self.assertEqual(got["status"], "in_progress")
        finally:
            con.close()

    def test_comments_revisioned_with_actor_and_pins(self):
        con = fresh_db()
        try:
            collaboration.create_workspace(con, "ws-1", "W", "ann")
            c = collaboration.save_comment(con, "ws-1", "observation", "obs-1",
                                           "first look", "ann",
                                           {"segment": 3})
            e = collaboration.edit_comment(con, c["comment_id"], "second look", "ann",
                                           expected_revision=1)
            self.assertEqual(e["revision"], 2)
            listed = collaboration.list_comments(con, "ws-1", "observation", "obs-1")
            self.assertEqual(listed[0]["actor"], "ann")
            invis = collaboration.delete_comment(con, c["comment_id"], "ann")
            self.assertTrue(invis["tombstone_visible"])
        finally:
            con.close()

    def test_viewer_cannot_comment(self):
        con = fresh_db()
        try:
            collaboration.create_workspace(con, "ws-1", "W", "ann")
            collaboration.create_invite(con, "ws-1", "vi", "viewer", "ann")
            collaboration.accept_invite(
                con, con.execute("SELECT id FROM invites").fetchone()["id"], "vi")
            with self.assertRaises(access.AuthorizationError):
                collaboration.save_comment(con, "ws-1", "observation", "obs-1",
                                           "hi", "vi")
        finally:
            con.close()

    def test_agent_proposal_never_human_verified(self):
        con = fresh_db()
        try:
            collaboration.create_workspace(con, "ws-1", "W", "ann")
            p = collaboration.save_proposal(con, "ws-1", "observation", "obs-1",
                                            "agent-1", {"bounds": [1, 2]})
            self.assertEqual(p["origin"], "agent")
            applied = collaboration.apply_proposal(con, p["proposal_id"], "ann")
            self.assertEqual(applied["origin"], "agent")
            self.assertEqual(applied["proposer"], "agent-1")
            self.assertEqual(applied["approver"], "ann")
            self.assertFalse(applied["human_verified"])
            self.assertFalse(applied["audio_inspected"])
        finally:
            con.close()

    def test_agent_proposal_origin_allowlist(self):
        con = fresh_db()
        try:
            collaboration.create_workspace(con, "ws-1", "W", "ann")
            for good in ("agent", "classifier", "pipeline", "machine"):
                p = collaboration.save_proposal(con, "ws-1", "observation", "obs-1",
                                                "agent-1", {"b": 1}, origin=good)
                self.assertEqual(p["origin"], good)
                applied = collaboration.apply_proposal(con, p["proposal_id"], "ann")
                self.assertEqual(applied["origin"], good)
            for bad in ("owner", "", "human", "verified", "accepted", "root"):
                with self.assertRaises(ops.BadInput):
                    collaboration.save_proposal(con, "ws-1", "observation", "obs-1",
                                                "agent-1", {"b": 1}, origin=bad)
        finally:
            con.close()


class SharesTest(unittest.TestCase):
    def _setup(self, con):
        collaboration.create_workspace(con, "ws-1", "W", "ann")
        prev = collaboration.preview_share("evidence", "ev-1", "guest-a",
                                           ["excerpt"], False)
        self.assertEqual(prev["action_label"], "Create share")
        s = collaboration.create_share(con, "ws-1", "evidence", "ev-1", "guest-a",
                                       ["excerpt"], "ann")
        return s["share_id"]

    def test_red_capable_guest_traversal_denied(self):
        """RED-CAPABLE: guest traversal.

        Before: named guest could read parent transcript/workspace.
        After: selected share reads; parent transcript + workspace
        browse + agent scopes denied by the same service authorization.
        """
        con = fresh_db()
        try:
            sid = self._setup(con)
            guest = access.AccessContext.create(
                principal_id="guest-a", principal_kind="guest",
                workspace_id="ws-1", authority_kind="share",
                share_grant_id=sid, credential_kind="browser_session")
            ok = collaboration.check_share_access(con, guest, "evidence_detail",
                                                  "evidence", "ev-1")
            self.assertTrue(ok["allowed"])
            with self.assertRaises(access.AuthorizationError):
                collaboration.check_share_access(con, guest, "transcript_window",
                                                 "episode", "ep-1")
            with self.assertRaises(access.AuthorizationError):
                guest.require_operation("submit_processing")
        finally:
            con.close()

    def test_share_defaults_and_revocation(self):
        con = fresh_db()
        try:
            sid = self._setup(con)
            st = collaboration.share_state(con, sid)
            self.assertEqual(st["state"], "active")
            self.assertIn("not delivered copies", st["no_recall_notice"])
            collaboration.revoke_share(con, sid, "ann", expected_revision=1)
            st = collaboration.share_state(con, sid)
            self.assertEqual(st["state"], "revoked")
            guest = access.AccessContext.create(
                principal_id="guest-a", principal_kind="guest",
                workspace_id="ws-1", authority_kind="share",
                share_grant_id=sid, credential_kind="browser_session")
            with self.assertRaises(access.AuthorizationError):
                collaboration.check_share_access(con, guest, "evidence_detail",
                                                 "evidence", "ev-1")
        finally:
            con.close()

    def test_create_share_download_narrowed_by_policy_is_explicit(self):
        con = fresh_db()
        try:
            collaboration.create_workspace(con, "ws-1", "W", "ann")
            resp = collaboration.create_share(con, "ws-1", "evidence", "ev-1",
                                              "guest-a", ["excerpt"], "ann",
                                              download=True)
            stored = con.execute("SELECT download FROM shares WHERE id=?",
                                 (resp["share_id"],)).fetchone()["download"]
            self.assertEqual(stored, 0)
            self.assertTrue(resp["downloads"]["requested"])
            self.assertFalse(resp["downloads"]["allowed"])
            self.assertFalse(resp["downloads"]["audio"])
            self.assertTrue(resp["downloads"]["narrowed_by_policy"])
            self.assertIn("narrowed by policy", resp["downloads"]["notice"])
        finally:
            con.close()

    def test_share_scoped_citation_denies_expired_revoked_unknown(self):
        con = fresh_db()
        try:
            collaboration.create_workspace(con, "ws-1", "W", "ann")
            live = collaboration.create_share(con, "ws-1", "evidence", "ev-1",
                                              "guest-a", ["excerpt"], "ann")
            out = citations.share_scoped_citation(
                con, live["share_id"], {"excerpt": "x", "secret": "y"}, {"excerpt"})
            self.assertEqual(out, {"excerpt": "x"})
            old = collaboration.create_share(
                con, "ws-1", "evidence", "ev-1", "guest-a", ["excerpt"], "ann",
                requested_expiry="2020-01-01T00:00:00+00:00")
            self.assertEqual(
                collaboration.share_state(con, old["share_id"])["state"], "expired")
            with self.assertRaises(citations.CitationError):
                citations.share_scoped_citation(
                    con, old["share_id"], {"excerpt": "x"}, {"excerpt"})
            collaboration.revoke_share(con, live["share_id"], "ann")
            with self.assertRaises(citations.CitationError):
                citations.share_scoped_citation(
                    con, live["share_id"], {"excerpt": "x"}, {"excerpt"})
            with self.assertRaises(citations.CitationError):
                citations.share_scoped_citation(
                    con, "share:missing", {"excerpt": "x"}, {"excerpt"})
        finally:
            con.close()

    def test_no_anonymous_share(self):
        con = fresh_db()
        try:
            collaboration.create_workspace(con, "ws-1", "W", "ann")
            with self.assertRaises(ops.BadInput):
                collaboration.create_share(con, "ws-1", "evidence", "ev-1", "",
                                           ["excerpt"], "ann")
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
