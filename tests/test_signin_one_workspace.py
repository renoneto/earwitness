"""QA7-ONEWS (live QA #7): a person with exactly one admitted workspace
lands straight in it after sign-in, for password and OIDC sign-in alike.

Through the real HostedApp: POST /api/v1/session/password or the OIDC
callback, then GET /api/v1/session with the new cookie. Workspaces are
real registered fixture databases with real memberships; the OIDC
provider is the loopback fixture from test_signin_bounds. Zero or
several admitted workspaces keep the old unbound session (the chooser).
Revoked memberships, pending invitations and retired workspaces do not
count. The binding is the workspace switch's own (same revision step,
same fences): a workspace the switch would refuse is not bound.
"""
from __future__ import annotations

import json
import sqlite3
import time
import unittest
from pathlib import Path
from unittest import mock

from earwitness.app import access as _access
from earwitness.app import admission as _admission
from earwitness.app import browser_auth as _auth
from earwitness.app import collaboration as _collab
from earwitness.app import connections as _conn
from earwitness.app import conversion as _conversion
from earwitness.app import customer as _customer
from earwitness.app import db as _db
from earwitness.app import services as _services
from tests import backend_fixtures
from tests import test_password_signin as _pw
from tests import test_signin_bounds as _sb

OTHER = "oidc:https://other.example:someone"


def _register(store, data_dir: str, name: str, owner: str, *, ready: bool = True,
              is_owner: bool = False, bound: tuple | None = None) -> str:
    """A real workspace owned (membership role owner) by `owner`. Ready
    (the default) is built the way services.collab_create_workspace builds
    one: current schema, then the product-creation readiness marker, so the
    session carries the frontend contract. ready=False is the older fixture
    store, which the app cannot open yet (frontend_contract None).

    is_owner=True registers the operator-owner way (QA7-R3-01): the
    registry entry is_owner=1, and `bound` is the (issuer, subject) the
    operator bound to it. Authority then comes from that binding, not
    from membership."""
    path = _register_files(store, data_dir, name, owner, ready)
    _conn.register_workspace(store, name, path, data_dir, is_owner=is_owner)
    if bound:
        # Straight to the registry row: the loopback provider's issuer is
        # not https, which bind_workspace_account (operator CLI) requires.
        control = store._connect()
        try:
            control.execute("UPDATE workspace_registry SET bound_issuer=?, bound_subject=?"
                            " WHERE workspace_id=?", (bound[0], bound[1], name))
            control.commit()
        finally:
            control.close()
    return path


def _register_files(store, data_dir: str, name: str, owner: str, ready: bool) -> str:
    if ready:
        path = Path(data_dir) / (name + ".sqlite3")
        con = sqlite3.connect(str(path), isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            _db.init_schema(con)
            with _db.transaction(con, write=True):
                _collab.create_workspace(con, "local", name, owner)
                _conversion.record_product_creation(con, owner)
        finally:
            con.close()
        return str(path)
    db = backend_fixtures.make_workspace(Path(data_dir) / name, schema_version=8)
    con = sqlite3.connect(str(db), isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        _collab.create_workspace(con, "local", name, owner)
    finally:
        con.close()
    return str(db)


def _on_db(path: str, fn):
    con = sqlite3.connect(path, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        return fn(con)
    finally:
        con.close()


class PasswordSignInOneWorkspaceTest(_pw._Base):
    def setUp(self):
        super().setUp()
        self.data_dir = str(self.work / "data")
        self.add_account()

    def register(self, name, owner=_pw.P, *, ready=True, is_owner=False, bound=None):
        return _register(self.app.store, self.data_dir, name, owner, ready=ready,
                         is_owner=is_owner, bound=bound)

    def register_owned(self, name, *, ready=True, bound=(_pw.BASE, _pw.SUB)):
        """An operator-owner workspace bound to the local account."""
        return self.register(name, OTHER, ready=ready, is_owner=True, bound=bound)

    def pending_invite(self, name):
        db = self.register(name, OTHER)
        _on_db(db, lambda con: _collab.create_invite(con, "local", _pw.P, "viewer", OTHER))

    def revoked_member(self, name):
        db = self.register(name, OTHER)

        def run(con):
            invite = _collab.create_invite(con, "local", _pw.P, "viewer", OTHER)
            _collab.accept_invite(con, invite["invite_id"], _pw.P)
            _collab.revoke_member(con, "local", _pw.P, OTHER)
        _on_db(db, run)

    def audit_rows(self):
        con = self.control()
        try:
            return con.execute("SELECT COUNT(*) FROM agent_audit").fetchone()[0]
        except sqlite3.OperationalError:
            return 0
        finally:
            con.close()

    async def signed_in_session(self):
        status, _raw, data, jar = await self.login()
        self.assertEqual(status, 200, data)
        self.assertEqual(data, {"return_to": "/app/"})
        cookie = jar[_auth.SESSION_COOKIE]
        status, _raw, session, _jar = await _pw.call(
            self.app, "/api/v1/session", cookies={_auth.SESSION_COOKIE: cookie})
        self.assertEqual(status, 200, session)
        return session

    async def test_one_workspace_is_active_at_sign_in(self):
        self.register("solo")
        session = await self.signed_in_session()
        self.assertEqual(session["workspaces"], ["solo"])
        self.assertEqual(session["active_workspace"], "solo")
        # Bound exactly as the switch binds: one switch epoch consumed, and
        # the member context the switch reply would carry.
        self.assertEqual(session["workspace_switch_revision"], 1)
        self.assertEqual(session["role"], "owner")
        self.assertTrue(session["workspace_context"])
        self.assertEqual(session["frontend_contract"], _customer.SESSION_CONTRACT)
        self.assertEqual(session["signin_method"], "password")

    async def test_an_owner_bound_workspace_is_active_at_sign_in(self):
        """QA7-R3-01 P1: the sole workspace is operator-registered
        (is_owner) and bound to this account; no membership row names
        the person. The owner binding alone admits and binds it."""
        self.register_owned("ownd")
        session = await self.signed_in_session()
        self.assertEqual(session["workspaces"], ["ownd"])
        self.assertEqual(session["active_workspace"], "ownd")
        self.assertEqual(session["workspace_switch_revision"], 1)
        self.assertEqual(session["role"], "owner")
        self.assertEqual(session["frontend_contract"], "earwitness/customer-ui/2")

    async def test_an_owner_bound_workspace_not_ready_binds_nothing(self):
        """QA7-R3-01 P1b: owner-bound but not ready for the app: listed,
        nothing bound."""
        self.register_owned("ownd", ready=False)
        session = await self.signed_in_session()
        self.assertEqual(session["active_workspace"], "")
        self.assertEqual(session["workspace_switch_revision"], 0)
        self.assertIsNone(session["frontend_contract"])

    async def test_an_owner_bound_plus_a_membership_workspace_keeps_the_chooser(self):
        """QA7-R3-01 P2: two admitted workspaces (one by binding, one by
        membership) is the chooser."""
        self.register_owned("ownd")
        self.register("member")
        session = await self.signed_in_session()
        self.assertEqual(session["workspaces"], ["member", "ownd"])
        self.assertEqual(session["active_workspace"], "")
        self.assertEqual(session["workspace_switch_revision"], 0)

    async def test_an_owner_database_bound_to_another_principal_ignores_memberships(self):
        """QA7-R3-01 P2b: an owner-registered database bound to someone
        else admits nobody else, even with a live membership row for this
        person in it. Sign-in binds nothing and the switch refuses it."""
        self.register("ownd", _pw.P, is_owner=True, bound=(_pw.BASE, "someone-else"))
        db = str(Path(self.data_dir) / "ownd.sqlite3")
        self.assertTrue(_on_db(db, lambda con: _collab.is_member(con, "local", _pw.P)),
                        "fixture: the person must hold a membership row")
        status, _raw, data, jar = await self.login()
        self.assertEqual(status, 200, data)
        cookies = {_auth.SESSION_COOKIE: jar[_auth.SESSION_COOKIE]}
        status, _raw, session, _jar = await _pw.call(self.app, "/api/v1/session", cookies=cookies)
        self.assertEqual(session["workspaces"], [])
        self.assertEqual(session["active_workspace"], "")
        headers = {"content-type": "application/json", "origin": _pw.BASE,
                   _auth.CSRF_HEADER: session["csrf_token"]}
        status, _raw, refused, _jar = await _pw.call(
            self.app, "/api/v1/session/workspace", "POST",
            {"workspace_id": "ownd", "expected_workspace_switch_revision": 0},
            headers=headers, cookies=cookies)
        self.assertGreaterEqual(status, 400, refused)
        status, _raw, after, _jar = await _pw.call(self.app, "/api/v1/session", cookies=cookies)
        self.assertEqual(after["active_workspace"], "")

    async def test_an_owner_binding_under_another_issuer_admits_nobody(self):
        """QA7-R3-01: the same subject under a different issuer is a
        different principal (the issuer half of the owner check)."""
        self.register("ownd", OTHER, is_owner=True, bound=("https://other.example", _pw.SUB))
        session = await self.signed_in_session()
        self.assertEqual(session["workspaces"], [])
        self.assertEqual(session["active_workspace"], "")

    async def test_a_workspace_not_ready_for_the_app_keeps_the_chooser(self):
        """QA7-R1-03: the sole workspace is not ready for the frontend (no
        frontend_contract, the readiness the browser gate checks): nothing
        is bound, so the person gets today's chooser with Change password,
        not the "needs a different version" page."""
        self.register("solo", ready=False)
        session = await self.signed_in_session()
        self.assertEqual(session["workspaces"], ["solo"])
        self.assertEqual(session["active_workspace"], "")
        self.assertEqual(session["workspace_switch_revision"], 0)
        self.assertIsNone(session["frontend_contract"])

    async def test_a_workspace_the_product_created_opens_usable(self):
        """QA7-R1-04: a workspace made through the product's own create
        (POST /api/v1/workspaces, services.collab_create_workspace) is
        bound at the next sign-in and is usable: the session carries the
        frontend contract the browser gate requires."""
        status, _raw, data, jar = await self.login()
        self.assertEqual(status, 200, data)
        cookies = {_auth.SESSION_COOKIE: jar[_auth.SESSION_COOKIE]}
        status, _raw, first, _jar = await _pw.call(self.app, "/api/v1/session", cookies=cookies)
        self.assertEqual(first["active_workspace"], "")
        headers = {"content-type": "application/json", "origin": _pw.BASE,
                   _auth.CSRF_HEADER: first["csrf_token"]}
        status, _raw, made, _jar = await _pw.call(
            self.app, "/api/v1/workspaces", "POST",
            {"workspace_id": "ws-pilot", "name": "Pilot"}, headers=headers, cookies=cookies)
        self.assertEqual(status, 200, made)
        session = await self.signed_in_session()
        self.assertEqual(session["workspaces"], ["ws-pilot"])
        self.assertEqual(session["active_workspace"], "ws-pilot")
        self.assertEqual(session["role"], "owner")
        self.assertTrue(session["workspace_context"])
        self.assertEqual(session["frontend_contract"], _customer.SESSION_CONTRACT)

    async def test_zero_workspaces_keep_the_unbound_session(self):
        session = await self.signed_in_session()
        self.assertEqual(session["workspaces"], [])
        self.assertEqual(session["active_workspace"], "")
        self.assertEqual(session["workspace_switch_revision"], 0)

    async def test_two_workspaces_keep_the_chooser(self):
        self.register("alpha")
        self.register("beta")
        session = await self.signed_in_session()
        self.assertEqual(session["workspaces"], ["alpha", "beta"])
        self.assertEqual(session["active_workspace"], "")
        self.assertEqual(session["workspace_switch_revision"], 0)

    async def test_a_pending_invitation_does_not_count(self):
        self.register("solo")
        self.pending_invite("invited")
        session = await self.signed_in_session()
        self.assertEqual(session["active_workspace"], "solo")

    async def test_only_a_pending_invitation_binds_nothing(self):
        self.pending_invite("invited")
        session = await self.signed_in_session()
        self.assertEqual(session["active_workspace"], "")

    async def test_a_revoked_membership_does_not_count(self):
        self.register("solo")
        self.revoked_member("former")
        session = await self.signed_in_session()
        self.assertEqual(session["workspaces"], ["solo"])
        self.assertEqual(session["active_workspace"], "solo")

    async def test_only_a_revoked_membership_binds_nothing(self):
        self.revoked_member("former")
        session = await self.signed_in_session()
        self.assertEqual(session["active_workspace"], "")

    async def test_a_retired_workspace_does_not_count(self):
        self.register("solo")
        self.register("old")
        self.admission["retired_workspace_ids"] = ["old"]
        self.write_admission()
        session = await self.signed_in_session()
        self.assertEqual(session["active_workspace"], "solo")

    async def test_only_a_retired_workspace_binds_nothing(self):
        self.register("old")
        self.admission["retired_workspace_ids"] = ["old"]
        self.write_admission()
        session = await self.signed_in_session()
        self.assertEqual(session["active_workspace"], "")

    async def test_a_workspace_the_switch_refuses_is_not_bound(self):
        """The switch's pre-commit fence refuses: sign-in still succeeds,
        and the session stays unbound (the chooser), nothing half-bound."""
        self.register("solo")
        real = _services.fenced_authority_check
        refused = []

        def refuse(*args, **kwargs):
            if kwargs.get("operation") == "session-status" and not refused:
                refused.append(True)
                raise _access.AccessLost("workspace authority changed")
            return real(*args, **kwargs)
        with mock.patch.object(_services, "fenced_authority_check", refuse):
            status, _raw, data, jar = await self.login()
        self.assertEqual(status, 200, data)
        self.assertEqual(refused, [True], "sign-in never ran the switch's fence")
        rows = self.sessions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["active_workspace"], "")
        self.assertEqual(rows[0]["workspace_switch_revision"], 0)
        status, _raw, session, _jar = await _pw.call(
            self.app, "/api/v1/session", cookies={_auth.SESSION_COOKIE: jar[_auth.SESSION_COOKIE]})
        self.assertEqual(status, 200, session)
        self.assertEqual(session["active_workspace"], "")

    async def test_a_failing_count_leaves_sign_in_unbound(self):
        """QA7-R1-01: an error while choosing the workspace (here a locked
        workspace database during the count) leaves sign-in succeeding
        unbound: 200, the new cookie, and a live session the browser holds,
        even when the request carried an older session cookie."""
        self.register("solo")
        status, _raw, data, jar = await self.login()
        self.assertEqual(status, 200, data)
        old = jar[_auth.SESSION_COOKIE]
        with mock.patch.object(self.app, "_authorized_workspaces",
                               side_effect=sqlite3.OperationalError("database is locked")):
            status, _raw, data, jar = await self.login(cookies={_auth.SESSION_COOKIE: old})
        self.assertEqual(status, 200, data)
        cookie = jar.get(_auth.SESSION_COOKIE)
        self.assertTrue(cookie, "sign-in set no session cookie")
        self.assertNotEqual(cookie, old)
        status, _raw, session, _jar = await _pw.call(
            self.app, "/api/v1/session", cookies={_auth.SESSION_COOKIE: cookie})
        self.assertEqual(status, 200, session)
        self.assertTrue(session["authenticated"])
        self.assertEqual(session["active_workspace"], "")
        self.assertEqual(session["workspace_switch_revision"], 0)

    async def test_an_unbound_sign_in_logs_a_warning_with_the_error_type_only(self):
        """signin.workspace_unbound is a warning (as app.warm_failed is)
        and carries the error type only: no email, principal, session or
        workspace ID."""
        self.register("solo")
        with mock.patch.object(self.app, "_authorized_workspaces",
                               side_effect=sqlite3.OperationalError("database is locked")), \
                self.assertLogs("earwitness.ops", level="WARNING") as logs:
            status, _raw, data, _jar = await self.login()
        self.assertEqual(status, 200, data)
        records = [json.loads(line.split(":", 2)[2]) for line in logs.output
                   if "signin.workspace_unbound" in line]
        self.assertEqual(len(records), 1, logs.output)
        record = records[0]
        self.assertEqual(record["level"], "warning")
        self.assertEqual(record["reason"], "OperationalError")
        self.assertEqual(set(record) - {"ts", "level", "event", "request_id", "job_id"},
                         {"reason"})

    async def test_an_unreadable_admission_read_binds_nothing(self):
        """QA7-R1-02: the admission policy is read once per sign-in, after
        the count. When that read cannot load the file (an edit caught half
        way), nothing is bound: one unreadable read never turns several
        workspaces into one."""
        self.register("alpha")
        self.register("beta")
        await self.assert_unreadable_policy_binds_nothing()

    async def test_an_unreadable_admission_read_binds_nothing_for_one(self):
        """The same unreadable read with exactly one workspace: still
        nothing is bound (unreadable is never "nothing retired")."""
        self.register("solo")
        await self.assert_unreadable_policy_binds_nothing()

    async def assert_unreadable_policy_binds_nothing(self):
        real_policy = _admission._policy
        real_count = self.app._authorized_workspaces
        state = {"counted": False, "unreadable": 0}

        def count(*args, **kwargs):
            state["counted"] = True
            return real_count(*args, **kwargs)

        def policy(*args, **kwargs):
            if state["counted"] and not state["unreadable"]:
                state["unreadable"] += 1
                return _admission.UNAVAILABLE, None
            return real_policy(*args, **kwargs)
        with mock.patch.object(self.app, "_authorized_workspaces", count), \
                mock.patch.object(_admission, "_policy", policy):
            status, _raw, data, jar = await self.login()
        self.assertEqual(status, 200, data)
        self.assertEqual(state["unreadable"], 1, "sign-in never read the policy after the count")
        rows = self.sessions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["active_workspace"], "")
        self.assertEqual(rows[0]["workspace_switch_revision"], 0)

    async def test_binding_holds_no_provider_slot(self):
        """QA7-R1-05: the password hash thread has ended before the
        binding, so this attempt's provider slot is free while it runs,
        and freed once only: another attempt's slot stays held."""
        self.register("solo")
        self.app.signin_limiter._provider_in_flight = 1  # another attempt in flight
        held = []
        real = self.app._signin_bind_one_workspace

        async def bind(session_id):
            held.append(self.app.signin_limiter._provider_in_flight)
            return await real(session_id)
        with mock.patch.object(self.app, "_signin_bind_one_workspace", bind):
            session = await self.signed_in_session()
        self.assertEqual(held, [1])
        self.assertEqual(session["active_workspace"], "solo")
        self.assertEqual(self.app.signin_limiter._provider_in_flight, 1)

    async def test_binding_writes_the_same_audit_rows_as_the_switch(self):
        """The switch writes no agent_audit row; neither does the binding."""
        self.register("solo")
        before = self.audit_rows()
        await self.signed_in_session()
        self.assertEqual(self.audit_rows(), before)

    async def test_switch_still_works_after_a_bound_sign_in(self):
        self.register("solo")
        status, _raw, data, jar = await self.login()
        self.assertEqual(status, 200, data)
        cookies = {_auth.SESSION_COOKIE: jar[_auth.SESSION_COOKIE]}
        status, _raw, session, _jar = await _pw.call(self.app, "/api/v1/session", cookies=cookies)
        headers = {"content-type": "application/json", "origin": _pw.BASE,
                   _auth.CSRF_HEADER: session["csrf_token"]}
        status, _raw, switched, _jar = await _pw.call(
            self.app, "/api/v1/session/workspace", "POST",
            {"workspace_id": "solo", "expected_workspace_switch_revision": 1},
            headers=headers, cookies=cookies)
        self.assertEqual(status, 200, switched)
        self.assertEqual(switched["active_workspace"], "solo")
        self.assertEqual(switched["workspace_switch_revision"], 2)


class OidcSignInOneWorkspaceTest(_sb._Base):
    def setUp(self):
        super().setUp()
        self.data_dir = str(self.work / "data")
        self.alice = f"oidc:{self.idp.iss}:alice"

    def register(self, name, owner=None):
        return _register(self.app.store, self.data_dir, name, owner or self.alice)

    async def session_after_sign_in(self):
        status, data, cookies = await self.sign_in("alice")
        self.assertEqual(status, 302, data)
        cookie = cookies[_auth.SESSION_COOKIE]
        status, session, _c = await _sb._call(
            self.app, "/api/v1/session", cookies={_auth.SESSION_COOKIE: cookie})
        self.assertEqual(status, 200, session)
        return session

    async def test_one_workspace_is_active_at_oidc_sign_in(self):
        self.register("solo")
        session = await self.session_after_sign_in()
        self.assertEqual(session["signin_method"], "oidc")
        self.assertEqual(session["active_workspace"], "solo")
        self.assertEqual(session["workspace_switch_revision"], 1)

    async def test_an_owner_bound_workspace_is_active_at_oidc_sign_in(self):
        """QA7-R3-01 O1: the provider principal is the operator-bound
        owner of the sole workspace (no membership row)."""
        _register(self.app.store, self.data_dir, "ownd", "oidc:https://other.example:x",
                  is_owner=True, bound=(self.idp.iss, "alice"))
        session = await self.session_after_sign_in()
        self.assertEqual(session["workspaces"], ["ownd"])
        self.assertEqual(session["active_workspace"], "ownd")
        self.assertEqual(session["workspace_switch_revision"], 1)
        self.assertEqual(session["role"], "owner")
        self.assertEqual(session["frontend_contract"], "earwitness/customer-ui/2")

    async def test_two_workspaces_keep_the_chooser_at_oidc_sign_in(self):
        self.register("alpha")
        self.register("beta")
        session = await self.session_after_sign_in()
        self.assertEqual(session["active_workspace"], "")
        self.assertEqual(session["workspace_switch_revision"], 0)

    async def test_a_failing_count_leaves_oidc_sign_in_unbound(self):
        """QA7-R1-01 for the provider callback: the code is spent, so a
        failure while choosing the workspace must still finish sign-in."""
        self.register("solo")
        with mock.patch.object(self.app, "_authorized_workspaces",
                               side_effect=sqlite3.OperationalError("database is locked")):
            status, data, cookies = await self.sign_in("alice")
        self.assertEqual(status, 302, data)
        cookie = cookies.get(_auth.SESSION_COOKIE)
        self.assertTrue(cookie, "the callback set no session cookie")
        status, session, _c = await _sb._call(
            self.app, "/api/v1/session", cookies={_auth.SESSION_COOKIE: cookie})
        self.assertEqual(status, 200, session)
        self.assertTrue(session["authenticated"])
        self.assertEqual(session["active_workspace"], "")

    async def test_oidc_binding_holds_no_provider_slot(self):
        """QA7-R1-05: discovery and the code exchange have ended before
        the binding, so the shared provider slot is free while it runs."""
        self.register("solo")
        held = []
        real = self.app._signin_bind_one_workspace

        async def bind(session_id):
            held.append(self.app.signin_limiter._provider_in_flight)
            return await real(session_id)
        with mock.patch.object(self.app, "_signin_bind_one_workspace", bind):
            session = await self.session_after_sign_in()
        self.assertEqual(held, [0])
        self.assertEqual(session["active_workspace"], "solo")
        self.assertEqual(self.app.signin_limiter._provider_in_flight, 0)

    async def test_an_owner_binding_to_another_provider_principal_admits_nobody(self):
        """QA7-R3-01: owner bindings with the same subject under another
        issuer, and with another subject under this issuer, admit nobody."""
        other = "oidc:https://other.example:x"
        _register(self.app.store, self.data_dir, "wrongiss", other,
                  is_owner=True, bound=("https://other.example", "alice"))
        _register(self.app.store, self.data_dir, "wrongsub", other,
                  is_owner=True, bound=(self.idp.iss, "bob"))
        session = await self.session_after_sign_in()
        self.assertEqual(session["workspaces"], [])
        self.assertEqual(session["active_workspace"], "")

    async def test_someone_elses_workspace_is_never_bound(self):
        self.register("theirs", owner=f"oidc:{self.idp.iss}:bob")
        session = await self.session_after_sign_in()
        self.assertEqual(session["workspaces"], [])
        self.assertEqual(session["active_workspace"], "")


if __name__ == "__main__":
    unittest.main()
