"""DEMO2 pass 15: the server marks a refusal that means the caller lost
access to the workspace.

Owner decision: status 403 and code FORBIDDEN stay unchanged (other
clients rely on them); the error body gains one field, "access": "lost",
on every refusal raised because the caller no longer has access to the
workspace (membership, session or workspace binding gone). Role and
feature refusals never carry it. The customer app starts its authority
boundary on 401 or on this marker only.
"""
from __future__ import annotations

import ast
import pathlib
import sqlite3
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from earwitness.app import access as _access
from earwitness.app import asgi as _asgi
from earwitness.app import browser_auth as _auth
from earwitness.app import collaboration as _collab
from earwitness.app import db as _db
from earwitness.app import services as _services
from tests import test_backend_authority as _authority_tests

APP = pathlib.Path(__file__).resolve().parent.parent / "earwitness" / "app"

# Every refusal message that means the caller no longer has access to the
# workspace, by source file. Each must be raised as access.AccessLost (or,
# for the direct JSON answer in asgi, carry "access": "lost").
LOST = {
    "asgi.py": ["workspace is not available to this account", "session unavailable",
                "session state changed", "workspace membership required"],
    "services.py": ["account is not bound to this workspace", "workspace membership required",
                    "workspace authority binding changed", "workspace identity changed",
                    "session unavailable", "session identity changed", "owner binding changed",
                    # Pass 15 verification: an owner workspace whose hosted
                    # account binding was removed, and a session that expired
                    # while a workspace was being created.
                    "owner workspace has no bound hosted account; use local owner mode",
                    "session expired during create", "session expired before create commit"],
    "browser_auth.py": ["session unavailable"],
}
# Refusals of one role or feature: never marked.
NOT_LOST = ["owner manages membership", "reader credentials are read-only",
            "valid CSRF token is required", "workspace role changed"]


def _body(exc):
    app = _asgi.HostedApp.__new__(_asgi.HostedApp)
    status, raw, _headers = app._service_error(exc)
    import json
    return status, json.loads(raw)


class MarkerMappingTest(unittest.TestCase):
    def test_access_lost_keeps_403_forbidden_and_adds_the_marker(self):
        self.assertTrue(issubclass(_access.AccessLost, _access.AuthorizationError))
        status, body = _body(_access.AccessLost("workspace membership required"))
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "FORBIDDEN")
        self.assertEqual(body["error"]["status"], 403)
        self.assertEqual(body["error"]["message"], "workspace membership required")
        self.assertEqual(body["error"]["access"], "lost")

    def test_a_role_refusal_has_no_marker(self):
        status, body = _body(_access.AuthorizationError(
            "Your role in this workspace can't export. Ask an owner for editor access.", 403))
        self.assertEqual((status, body["error"]["code"]), (403, "FORBIDDEN"))
        self.assertNotIn("access", body["error"])


class SourceInventoryTest(unittest.TestCase):
    """Every lost-access refusal site raises AccessLost; no role or feature
    refusal does."""

    def _raises(self, name):
        tree = ast.parse((APP / name).read_text())
        out = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                func = node.func
                callee = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if callee in ("AuthorizationError", "AccessLost", "RoleChanged"):
                    out.append((callee, node.args[0].value, node.lineno))
        return out

    def test_every_lost_access_message_is_raised_as_access_lost(self):
        for name, messages in LOST.items():
            raised = self._raises(name)
            for message in messages:
                with self.subTest(file=name, message=message):
                    sites = [r for r in raised if r[1] == message]
                    if name == "asgi.py" and message == "workspace membership required":
                        continue  # the direct JSON answer: see AsgiDirectAnswerTest
                    self.assertTrue(sites, "no raise site found")
                    self.assertEqual({r[0] for r in sites}, {"AccessLost"}, sites)

    def test_role_and_feature_refusals_are_not_marked(self):
        for name in ("asgi.py", "services.py", "browser_auth.py", "collaboration.py", "access.py"):
            for callee, message, line in self._raises(name):
                if message in NOT_LOST or message.startswith("Your role in this workspace"):
                    with self.subTest(file=name, line=line):
                        # DEMO2 pass 17: the role change is raised as
                        # RoleChanged (marked reason "role_changed"), never
                        # as AccessLost.
                        allowed = {"AuthorizationError", "RoleChanged"} if message == "workspace role changed" \
                            else {"AuthorizationError"}
                        self.assertIn(callee, allowed)


class _Http(unittest.TestCase):
    def setUp(self):
        self.fx = _authority_tests.HostedReaderHttpTest()
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.headers = self.fx._csrf()

    def _revoke_owner(self):
        con = sqlite3.connect(self.fx.db_path, timeout=15)
        try:
            con.execute("UPDATE memberships SET revoked=1 WHERE member=?",
                        ("oidc:https://i.example:owner",))
            con.commit()
        finally:
            con.close()


class LostMembershipHttpTest(_Http):
    def test_control_a_member_reads(self):
        status, out = self.fx._call("/api/v1/customer/brands", method="GET", headers=self.headers)
        self.assertEqual(status, 200, out)

    def test_a_revoked_member_gets_the_marker(self):
        self._revoke_owner()
        status, out = self.fx._call("/api/v1/customer/brands", method="GET", headers=self.headers)
        self.assertEqual(status, 403, out)
        self.assertEqual(out["error"]["code"], "FORBIDDEN")
        self.assertEqual(out["error"].get("access"), "lost", out)

    def test_membership_revoked_after_admission_gets_the_marker(self):
        real = _services._current_authority

        def revoking(*args, **kwargs):
            self._revoke_owner()
            return real(*args, **kwargs)

        with mock.patch.object(_services, "_current_authority", side_effect=revoking):
            status, out = self.fx._call("/api/v1/customer/saved-items", method="POST",
                                        body={"type": "query", "title": "t",
                                              "payload": {"query": {"version": 2, "mode": "mentions", "q": "x"}}},
                                        headers=self.headers)
        self.assertEqual(status, 403, out)
        self.assertEqual(out["error"].get("access"), "lost", out)


class AsgiDirectAnswerTest(_Http):
    """asgi.py: a session whose role lookup finds no membership."""

    def test_no_role_answer_carries_the_marker(self):
        with mock.patch.object(_collab, "get_role", return_value=None), \
                mock.patch.object(_collab, "is_member", return_value=True):
            status, out = self.fx._call("/api/v1/customer/brands", method="GET", headers=self.headers)
        self.assertEqual(status, 403, out)
        self.assertEqual(out["error"]["message"], "workspace membership required")
        self.assertEqual(out["error"].get("access"), "lost", out)


class WorkspaceSwitchTest(_Http):
    def _session_revision(self):
        status, session = self.fx._call("/api/v1/session", method="GET", headers=self.headers)
        self.assertEqual(status, 200, session)
        return session["workspace_switch_revision"]

    def test_switch_to_a_workspace_not_available_carries_the_marker(self):
        status, out = self.fx._call("/api/v1/session/workspace",
                                    body={"workspace_id": "elsewhere",
                                          "expected_workspace_switch_revision": self._session_revision()},
                                    headers=self.headers)
        self.assertEqual(status, 403, out)
        self.assertEqual(out["error"]["message"], "workspace is not available to this account")
        self.assertEqual(out["error"].get("access"), "lost", out)

    def test_access_gone_before_the_switch_answer_carries_the_marker(self):
        rev = self._session_revision()
        real = _asgi.HostedApp._authorized_workspaces
        calls = {"n": 0}

        def second_time_gone(app, session, **kwargs):
            calls["n"] += 1
            return [] if calls["n"] >= 2 else real(app, session, **kwargs)

        with mock.patch.object(_asgi.HostedApp, "_authorized_workspaces", second_time_gone):
            status, out = self.fx._call("/api/v1/session/workspace",
                                        body={"workspace_id": "acme",
                                              "expected_workspace_switch_revision": rev},
                                        headers=self.headers)
        self.assertEqual(status, 403, out)
        self.assertEqual(out["error"]["message"], "workspace is not available to this account")
        self.assertEqual(out["error"].get("access"), "lost", out)


class CurrentAuthorityTest(_Http):
    """services._current_authority: session and workspace binding gone."""

    def _ctx(self, **over):
        import hashlib
        digest = hashlib.sha256(f"ew-browser-v1:{self.fx.session_id}".encode()).hexdigest()
        base = dict(workspace_id="acme", registry_policy=None, db_identity=None, db_path=None,
                    connection_id=None, credential_kind="browser_session", session_id=digest,
                    principal_id="oidc:https://i.example:owner", session_binding=None,
                    authority_kind="membership")
        base.update(over)
        return SimpleNamespace(**base)

    def _check(self, ctx, message):
        control = self.fx.app.store.auth_connection()
        try:
            with self.assertRaises(_access.AuthorizationError) as caught:
                _services._current_authority(control, ctx)
        finally:
            control.close()
        self.assertEqual(str(caught.exception), message)
        self.assertIsInstance(caught.exception, _access.AccessLost)
        self.assertEqual(_body(caught.exception)[1]["error"].get("access"), "lost")

    def test_control_the_current_session_passes(self):
        control = self.fx.app.store.auth_connection()
        try:
            _services._current_authority(control, self._ctx())
        finally:
            control.close()

    def test_revoked_session(self):
        control = self.fx.app.store.auth_connection()
        try:
            control.execute("UPDATE browser_sessions SET revoked_at=?", (time.time(),))
            control.commit()
        finally:
            control.close()
        self._check(self._ctx(), "session unavailable")

    def test_session_identity_changed(self):
        self._check(self._ctx(session_binding=("https://i.example", "someone-else")), "session identity changed")

    def test_workspace_binding_changed(self):
        self._check(self._ctx(registry_policy={"workspace_id": "moved"}), "workspace authority binding changed")

    def test_workspace_identity_changed(self):
        self._check(self._ctx(db_identity="another-database"), "workspace identity changed")


class WorkspaceAuthorityTest(_Http):
    def test_no_role_at_the_fence_is_marked(self):
        con = sqlite3.connect(self.fx.db_path)
        try:
            ctx = SimpleNamespace(credential_kind="browser_session", authority_kind="membership",
                                  principal_id="oidc:https://i.example:gone")
            with self.assertRaises(_access.AccessLost):
                _services._workspace_authority(con, ctx, {"is_owner": False, "row_namespace": "local"})
        finally:
            con.close()


class BrowserAuthTest(_Http):
    def test_switch_on_a_missing_session_is_marked(self):
        control = self.fx.app.store.auth_connection()
        try:
            with self.assertRaises(_access.AccessLost):
                with _db.transaction(control, write=True):
                    _auth.set_active_workspace(control, "no-such-session", "acme", 0)
        finally:
            control.close()


class OwnerBindingRemovedTest(unittest.TestCase):
    """services.open_customer_workspace: the owner workspace's hosted
    account binding was removed, so the account that used it lost access."""

    def test_unbound_owner_workspace_is_marked(self):
        from earwitness.app import connections as _conn
        closed = []
        con = SimpleNamespace(close=lambda: closed.append(True))
        entry = {"is_owner": True, "bound_issuer": "", "bound_subject": ""}
        ctx = SimpleNamespace(connection_id=None, principal_id="oidc:https://i.example:owner",
                              session_binding=None, authority_kind="membership")
        with mock.patch.object(_conn, "resolve_workspace", return_value=entry), \
                mock.patch.object(_conn, "open_registered_db", return_value=(con, entry)):
            with self.assertRaises(_access.AuthorizationError) as caught:
                _services.open_customer_workspace(None, "acme", "/nowhere", ctx)
        self.assertEqual(closed, [True])
        self.assertIsInstance(caught.exception, _access.AccessLost)
        status, body = _body(caught.exception)
        self.assertEqual((status, body["error"]["code"]), (403, "FORBIDDEN"))
        self.assertEqual(body["error"].get("access"), "lost", body)


class RoleRefusalTest(_Http):
    def test_the_role_sentence_is_not_marked(self):
        ctx = SimpleNamespace(authority_kind="membership", principal_id="oidc:https://i.example:viewer")
        with mock.patch.object(_access, "require_customer_mutation"), \
                mock.patch.object(_collab, "check_permission", return_value=False):
            with self.assertRaises(_access.AuthorizationError) as caught:
                _services._require_customer_role(None, {"is_owner": False, "row_namespace": "local"}, ctx,
                                                 "export", "customer_research_export")
        self.assertNotIsInstance(caught.exception, _access.AccessLost)
        self.assertTrue(str(caught.exception).startswith("Your role in this workspace can't export"))
        self.assertNotIn("access", _body(caught.exception)[1]["error"])

    def test_owner_only_collaboration_is_not_marked(self):
        con = sqlite3.connect(self.fx.db_path, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            with self.assertRaises(_access.AuthorizationError) as caught:
                _collab.create_invite(con, "local", "someone", "viewer", "ed-1")
        finally:
            con.close()
        self.assertEqual(str(caught.exception), "owner manages membership")
        self.assertNotIsInstance(caught.exception, _access.AccessLost)
        self.assertNotIn("access", _body(caught.exception)[1]["error"])


if __name__ == "__main__":
    unittest.main()
