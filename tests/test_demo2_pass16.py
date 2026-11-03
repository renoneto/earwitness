"""DEMO2 pass 16 (Astra round 5, DEMO2-SEC-R5-02), hosted HTTP path.

Owner decision: the context token the hosted server returns includes the
caller's workspace role. Every role change changes the token, so the
browser drops reads it kept under the old token (an owner-only invitation
list) and reads them again; the "workspace role changed" refusal stays
unmarked (it is not a lost-access boundary).

The real HostedApp runs over the disposable HostedReaderHttpTest fixture.
The direct database role update stands for an owner changing this member's
role elsewhere.
"""
from __future__ import annotations

import sqlite3
import unittest

from tests import test_backend_authority as _authority_tests

OWNER = "oidc:https://i.example:owner"


class RoleInContextTokenHttpTest(unittest.TestCase):
    def setUp(self):
        self.fx = _authority_tests.HostedReaderHttpTest()
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.headers = self.fx._csrf()

    def _get(self, path):
        return self.fx._call(path, method="GET", headers=self.headers)

    def _set_role(self, role):
        con = sqlite3.connect(self.fx.db_path, timeout=15)
        try:
            changed = con.execute("UPDATE memberships SET role=? WHERE member=?", (role, OWNER)).rowcount
            con.commit()
        finally:
            con.close()
        self.assertEqual(changed, 1, "fixture: the owner's membership row")

    def _session(self):
        status, session = self._get("/api/v1/session")
        self.assertEqual(status, 200, session)
        return session

    def test_control_the_token_is_stable_while_the_role_is(self):
        first, second = self._session(), self._session()
        self.assertEqual(first["workspace_context"], second["workspace_context"])
        status, prefs = self._get("/api/v1/customer/preferences")
        self.assertEqual(status, 200, prefs)
        self.assertEqual(prefs["workspace_context"], first["workspace_context"])

    def _owner_then(self, role):
        before = self._session()
        self.assertEqual(before["role"], "owner", before)
        status, invites = self._get("/api/v1/workspaces/acme/invites")
        self.assertEqual(status, 200, invites)
        self._set_role(role)
        return before

    def test_owner_to_editor_changes_the_token_and_the_preferences_save_carries_it(self):
        before = self._owner_then("editor")
        after = self._session()
        self.assertEqual(after["role"], "editor", after)
        self.assertEqual(after["workspace_switch_revision"], before["workspace_switch_revision"])
        self.assertNotEqual(after["workspace_context"], before["workspace_context"],
                            "the context token stayed the same across owner -> editor")
        status, prefs = self._get("/api/v1/customer/preferences")
        self.assertEqual(status, 200, prefs)
        revision = prefs["preferences"]["revision"]
        status, saved = self.fx._call("/api/v1/customer/preferences", method="PUT",
                                      body={"workspace_name": "Acme renamed", "expected_revision": revision},
                                      headers=self.headers)
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["workspace_context"], after["workspace_context"])
        self.assertNotEqual(saved["workspace_context"], before["workspace_context"],
                            "the preferences save answered under the owner's token")
        # Read fresh, the invitations are refused, and the refusal is a
        # role refusal (unmarked), not lost access.
        status, invites = self._get("/api/v1/workspaces/acme/invites")
        self.assertEqual(status, 403, invites)
        self.assertNotIn("access", invites["error"])

    def test_owner_to_viewer_changes_the_token(self):
        before = self._owner_then("viewer")
        after = self._session()
        self.assertEqual(after["role"], "viewer", after)
        self.assertNotEqual(after["workspace_context"], before["workspace_context"])
        status, invites = self._get("/api/v1/workspaces/acme/invites")
        self.assertEqual(status, 403, invites)
        self.assertNotIn("access", invites["error"])

    def test_every_role_has_its_own_token(self):
        tokens = {}
        for role in ("owner", "editor", "viewer"):
            self._set_role(role)
            tokens[role] = self._session()["workspace_context"]
        self.assertEqual(len(set(tokens.values())), 3, tokens)


if __name__ == "__main__":
    unittest.main()
