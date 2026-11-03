"""Workspace display names in the session reply and the pending-invites
read route, over the real HostedApp HTTP surface (red-capable)."""
from __future__ import annotations

import sqlite3
import time
import unittest

from earwitness.app import browser_auth as _auth
from earwitness.app import collaboration as _collab
from earwitness.app import customer as _customer
from tests import test_backend_authority as _authority_tests

OWNER = "oidc:https://i.example:owner"


class _Hosted(unittest.TestCase):
    def setUp(self):
        self.fixture = _authority_tests.HostedReaderHttpTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.headers = self.fixture._csrf()

    def _session(self, identity, subject, workspace="acme"):
        control = self.fixture.app.store.auth_connection()
        try:
            now = time.time()
            control.execute(
                "INSERT INTO browser_sessions(session_hash,issuer,subject,email,"
                "email_verified,csrf_token,active_workspace,created_at,expires_at)"
                " VALUES (?,'https://i.example',?,'',0,'tok123',?,?,?)",
                (_auth._hash_presented(identity), subject, workspace, now, now + 3600))
            control.commit()
        finally:
            control.close()
        return {**self.headers, "cookie": f"ew_session={identity}"}

    def _member(self, subject, role):
        con = sqlite3.connect(self.fixture.db_path, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            principal = f"oidc:https://i.example:{subject}"
            invite = _collab.create_invite(con, "local", principal, role, OWNER)
            _collab.accept_invite(con, invite["invite_id"], principal)
        finally:
            con.close()
        return self._session(f"sess-{subject}", subject)


class SessionWorkspaceNamesTest(_Hosted):
    def test_session_names_each_authorized_workspace_and_keeps_ids(self):
        status, _ = self.fixture._call("/api/v1/workspaces",
            body={"workspace_id": "names-b", "name": "Brand Team"}, headers=self.headers)
        self.assertEqual(status, 200)
        outsider = self._session("sess-outsider", "outsider", workspace="")
        status, _ = self.fixture._call("/api/v1/workspaces",
            body={"workspace_id": "secret-c", "name": "Secret Co"}, headers=outsider)
        self.assertEqual(status, 200)
        status, session = self.fixture._call("/api/v1/session", method="GET", headers=self.headers)
        self.assertEqual(status, 200)
        self.assertEqual(session["workspaces"], ["acme", "names-b"])
        self.assertEqual(session["workspace_names"], {"acme": "Acme", "names-b": "Brand Team"})
        # Another account's workspace is neither listed nor named.
        self.assertNotIn("Secret Co", str(session))

    def test_settings_display_name_wins_and_switch_reply_carries_names(self):
        status, prefs = self.fixture._call("/api/v1/customer/preferences", method="GET",
                                           headers=self.headers)
        self.assertEqual(status, 200)
        status, _ = self.fixture._call("/api/v1/customer/preferences", method="PUT",
            body={"workspace_name": "  Acme Media  ",
                  "expected_revision": prefs["preferences"]["revision"]},
            headers=self.headers)
        self.assertEqual(status, 200)
        status, session = self.fixture._call("/api/v1/session", method="GET", headers=self.headers)
        self.assertEqual(session["workspace_names"], {"acme": "Acme Media"})
        status, switched = self.fixture._call("/api/v1/session/workspace",
            body={"workspace_id": "acme",
                  "expected_workspace_switch_revision": session["workspace_switch_revision"]},
            headers=self.headers)
        self.assertEqual(status, 200)
        self.assertEqual(switched["workspace_names"], {"acme": "Acme Media"})

    def test_unnamed_workspace_falls_back_to_empty_for_the_id(self):
        con = sqlite3.connect(":memory:")
        try:
            con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            self.assertEqual(_customer.workspace_display_name(con), "")
            con.execute("CREATE TABLE workspaces (id TEXT PRIMARY KEY, name TEXT, created_at TEXT)")
            con.execute("INSERT INTO workspaces VALUES ('local', '', 'x')")
            self.assertEqual(_customer.workspace_display_name(con), "")
        finally:
            con.close()


class PendingInvitesTest(_Hosted):
    PATH = "/api/v1/workspaces/acme/invites"

    def test_owner_lists_pending_invites_with_delivery_state_and_no_secrets(self):
        # GET is unguarded: no workspace switch revision header is sent.
        status, out = self.fixture._call(self.PATH, method="GET", headers=self.headers)
        self.assertEqual(status, 200, out)
        self.assertEqual([i["member"] for i in out["invites"]], ["newcomer"])
        invite = out["invites"][0]
        # L25: `unresolved` says when the latest send can never be settled.
        self.assertEqual(set(invite), {"id", "member", "role", "created_at", "delivery",
                                       "unresolved"})
        self.assertEqual((invite["role"], invite["delivery"]), ("viewer", "not_queued"))
        status, queued = self.fixture._call(f"{self.PATH}/{invite['id']}/deliver",
                                            body={}, headers=self.headers)
        self.assertEqual(status, 200, queued)
        self.assertFalse(queued["sent"])
        status, out = self.fixture._call(self.PATH, method="GET", headers=self.headers)
        self.assertEqual(out["invites"][0]["delivery"], "pending")
        con = sqlite3.connect(self.fixture.db_path)
        try:
            states = [r[0] for r in con.execute("SELECT state FROM notification_outbox")]
        finally:
            con.close()
        self.assertEqual(states, ["pending"], "delivery only queues; nothing is sent")

    def test_accepted_invites_leave_the_list(self):
        self._member("joiner", "viewer")
        status, out = self.fixture._call(self.PATH, method="GET", headers=self.headers)
        self.assertEqual(status, 200)
        self.assertNotIn("oidc:https://i.example:joiner", [i["member"] for i in out["invites"]])

    def test_only_inviting_roles_and_only_the_displayed_workspace(self):
        for subject, role in (("editor", "editor"), ("viewer", "viewer")):
            with self.subTest(role=role):
                status, out = self.fixture._call(self.PATH, method="GET",
                                                 headers=self._member(subject, role))
                self.assertEqual(status, 403)
                self.assertNotIn("invites", out)
        status, out = self.fixture._call(self.PATH, method="GET", headers={
            "authorization": f"Bearer {self.fixture.reader_token}"})
        self.assertEqual(status, 403)
        self.assertNotIn("invites", out)
        status, out = self.fixture._call("/api/v1/workspaces/other/invites", method="GET",
                                         headers=self.headers)
        self.assertEqual(status, 404)
        self.assertNotIn("invites", out)


if __name__ == "__main__":
    unittest.main()
