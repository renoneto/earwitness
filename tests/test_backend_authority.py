"""K02 presented-authority regression tests (red-capable).

Reader credentials stay read-only even for owner/editor principals;
expiry/revocation deny at current state; the fresh-authority fence
releases only against a current snapshot; invitation identity comes
from the server session, never the request body.
"""
from __future__ import annotations

import asyncio
import http.client
import importlib.util
import json
import os
import pathlib
import sqlite3
import socket
import tempfile
import time
import threading
import unittest
from types import SimpleNamespace

from earwitness.app import access as _access
from earwitness.app import asgi as _asgi
from earwitness.app import browser_auth as _auth
from earwitness.app import collaboration as _collab
from earwitness.app import connections as _conn
from earwitness.app import customer as _customer
from earwitness.app import customer_contracts
from earwitness.app import db as _db
from earwitness.app import ops as _ops
from earwitness.app import search as _search
from earwitness.app import services as _services
from tests import backend_fixtures


def _registry_fixture(identity="ws-k02-identity"):
    tmp = tempfile.TemporaryDirectory()
    data_dir = os.path.realpath(os.path.join(tmp.name, "data"))
    os.makedirs(data_dir)
    ws_dir = os.path.join(data_dir, "ws1")
    db_path = backend_fixtures.make_workspace(ws_dir, schema_version=8)
    store = _conn.GrantsStore(os.path.join(tmp.name, "grants.sqlite3"))
    _conn.register_workspace(store, "ws1", str(db_path), data_dir)
    return tmp, data_dir, str(db_path), store


def _add_membership(db_path, owner="owner-1", member="ed-1", role="editor"):
    con = sqlite3.connect(db_path, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        _collab.create_workspace(con, "local", "K02", owner)
        if member != owner:
            invite = _collab.create_invite(con, "local", member, role, owner)
            _collab.accept_invite(con, invite["invite_id"], member)
    finally:
        con.close()


def _browser_ctx(principal, workspace="ws1"):
    return _access.AccessContext.create(
        principal_id=principal, principal_kind="owner",
        workspace_id=workspace, authority_kind="membership",
        credential_kind="browser_session")


def _reader_ctx(principal, workspace="ws1", kind="reader_bearer"):
    return _access.AccessContext.create(
        principal_id=principal, principal_kind="editor",
        workspace_id=workspace, authority_kind="membership",
        scopes={"research:read", "transcripts:read"},
        credential_kind=kind)


class CredentialProvenanceTest(unittest.TestCase):
    def test_missing_credential_kind_rejected(self):
        with self.assertRaises(ValueError):
            _access.AccessContext.create(
                principal_id="owner-1", principal_kind="owner",
                workspace_id="local")

    def test_unknown_credential_kind_rejected(self):
        with self.assertRaises(ValueError):
            _access.AccessContext.create(
                principal_id="owner-1", principal_kind="owner",
                workspace_id="local", credential_kind="token")

    def test_reader_bearer_editor_denied_mutation(self):
        tmp, _data, db_path, _store = _registry_fixture()
        try:
            _add_membership(db_path)
            entry = {"row_namespace": "local", "is_owner": False,
                     "workspace_id": "ws1"}
            for kind in ("reader_bearer", "local_reader"):
                ctx = _reader_ctx("ed-1", kind=kind)
                con = sqlite3.connect(db_path, isolation_level=None)
                con.row_factory = sqlite3.Row
                try:
                    with self.assertRaises(_access.AuthorizationError) as raised:
                        _services.customer_save_alert(
                            ctx, con, entry, "probe",
                            {"version": 2, "mode": "mentions",
                             "q": "lumen"})
                    self.assertEqual(raised.exception.status, 403)
                finally:
                    con.close()
            con = sqlite3.connect(db_path)
            try:
                saved = con.execute(
                    "SELECT COUNT(*) FROM customer_saved_searches").fetchone()[0]
            finally:
                con.close()
            self.assertEqual(saved, 1)  # only the K01 legacy row
        finally:
            tmp.cleanup()

    def test_browser_session_mutation_succeeds(self):
        tmp, _data, db_path, _store = _registry_fixture()
        try:
            _add_membership(db_path, owner="owner-1", member="owner-1")
            entry = {"row_namespace": "local", "is_owner": False,
                     "workspace_id": "ws1"}
            ctx = _browser_ctx("owner-1")
            con = sqlite3.connect(db_path, isolation_level=None)
            con.row_factory = sqlite3.Row
            try:
                out = _services.customer_save_alert(
                    ctx, con, entry, "probe",
                    {"version": 2, "mode": "mentions", "q": "lumen"})
                self.assertEqual(out["revision"], 1)
                self.assertIn("id", out)
                self.assertEqual(out["lifecycle"], "active_local")
            finally:
                con.close()
        finally:
            tmp.cleanup()

    def test_reader_cannot_accept_invite(self):
        tmp, _data, db_path, _store = _registry_fixture()
        try:
            _add_membership(db_path)
            entry = {"row_namespace": "local", "is_owner": False,
                     "workspace_id": "ws1"}
            ctx = _reader_ctx("ed-1")
            con = sqlite3.connect(db_path, isolation_level=None)
            con.row_factory = sqlite3.Row
            try:
                with self.assertRaises(_access.AuthorizationError):
                    _services.collab_invite_accept(ctx, con, entry,
                                                   "invite-0", "")
            finally:
                con.close()
        finally:
            tmp.cleanup()

    def test_forged_invite_email_cannot_elevate(self):
        tmp, _data, db_path, _store = _registry_fixture()
        try:
            _add_membership(db_path)
            entry = {"row_namespace": "local", "is_owner": False,
                     "workspace_id": "ws1"}
            con = sqlite3.connect(db_path, isolation_level=None)
            con.row_factory = sqlite3.Row
            try:
                invite = _collab.create_invite(con, "local", "victim",
                                               "owner", "owner-1")
                attacker = _browser_ctx("attacker")
                # No session email: a wrong principal is denied even when a
                # forged address is claimed elsewhere. Adapters reject the
                # client key outright (HTTP test below); the service only
                # trusts the session-derived value it is given.
                with self.assertRaises(_access.AuthorizationError):
                    _services.collab_invite_accept(
                        attacker, con, entry, invite["invite_id"], "")
                self.assertIsNone(_collab.get_role(con, "local", "attacker"))
            finally:
                con.close()
        finally:
            tmp.cleanup()


class StrictBodyTest(unittest.TestCase):
    def test_duplicate_keys_rejected(self):
        with self.assertRaises(ValueError):
            _asgi._parse_json_body(b'{"a": 1, "a": 2}')

    def test_nonfinite_constants_rejected(self):
        for raw in (b'{"a": NaN}', b'{"a": Infinity}', b'{"a": -Infinity}'):
            with self.assertRaises(ValueError):
                _asgi._parse_json_body(raw)

    def test_valid_object_accepted(self):
        self.assertEqual(_asgi._parse_json_body(b'{"a": 1}'), {"a": 1})

    def test_reader_export_and_workspace_create_denied(self):
        tmp, _data, db_path, store = _registry_fixture()
        try:
            _add_membership(db_path)
            entry = {"row_namespace": "local", "is_owner": False,
                     "workspace_id": "ws1"}
            ctx = _reader_ctx("ed-1")
            con = sqlite3.connect(db_path, isolation_level=None)
            con.row_factory = sqlite3.Row
            try:
                with self.assertRaises(_access.AuthorizationError):
                    _services.customer_research_export(
                        ctx, con, entry,
                        {"query": customer_contracts.upcast_query_v1({"mode": "mentions", "q": "lumen"})[0]})
                with self.assertRaises(_access.AuthorizationError):
                    _services.collab_create_workspace(
                        store, "/tmp", ctx, "ws-new", "New")
            finally:
                con.close()
        finally:
            tmp.cleanup()


class GrantLifetimeTest(unittest.TestCase):
    def test_expired_grant_denied(self):
        tmp, _data, _db_path, store = _registry_fixture()
        try:
            issued = store.issue_grant(
                member_id="ed-1", member_role="editor", client_id="c1",
                workspace_id="ws1", scopes=["research:read"],
                content_policy={})
            con = store._connect()
            try:
                con.execute("UPDATE connection_grants SET expires_at=? WHERE id=?",
                            ("2000-01-01T00:00:00+00:00", issued["grant_id"]))
                con.commit()
            finally:
                con.close()
            grant = store.get_grant(issued["grant_id"])
            with self.assertRaises(_conn.GrantError) as raised:
                _conn.authorize_request(store, grant)
            self.assertEqual(raised.exception.status, 401)
        finally:
            tmp.cleanup()

    def test_revoked_grant_denied(self):
        tmp, _data, _db_path, store = _registry_fixture()
        try:
            issued = store.issue_grant(
                member_id="ed-1", member_role="editor", client_id="c1",
                workspace_id="ws1", scopes=["research:read"],
                content_policy={})
            store.revoke_grant(issued["grant_id"], "test revoke")
            grant = store.get_grant(issued["grant_id"])
            with self.assertRaises(_conn.GrantError) as raised:
                _conn.authorize_request(store, grant)
            self.assertEqual(raised.exception.status, 403)
        finally:
            tmp.cleanup()




class ErrorMappingTest(unittest.TestCase):
    def _mapped_status(self, exc):
        return _asgi.HostedApp._service_error(
            SimpleNamespace(config=None), exc)[0]

    def test_conflict_maps_412(self):
        self.assertEqual(self._mapped_status(_ops.Conflict(3)), 412)

    def test_missing_precondition_maps_428(self):
        self.assertEqual(
            self._mapped_status(_ops.MissingPrecondition("rev")), 428)

    def test_bad_input_maps_400(self):
        self.assertEqual(self._mapped_status(_ops.BadInput("bad")), 400)

    def test_filter_rejected_maps_400(self):
        self.assertEqual(
            self._mapped_status(_search.FilterRejected("nope")), 400)


class NamedShareTest(unittest.TestCase):
    def test_wrong_audience_denied(self):
        tmp, data_dir, db_path, store = _registry_fixture()
        try:
            _add_membership(db_path)
            con = sqlite3.connect(db_path, isolation_level=None)
            con.row_factory = sqlite3.Row
            try:
                created = _collab.create_share(
                    con, "local", "episode", "e1", "guest-a",
                    ["ep_title"], "owner-1")
            finally:
                con.close()
            session = {"issuer": "https://i.example", "subject": "intruder",
                       "email": "", "email_verified": 0}
            # L17: a caller who is not the audience gets the unknown-object
            # answer, the same as for a made-up share.
            with self.assertRaises(_ops.NotFound):
                _services.open_shared_workspace(
                    store, "ws1", data_dir, session,
                    created["share_id"])
        finally:
            tmp.cleanup()


class HostedReaderHttpTest(unittest.TestCase):
    """Actual HostedApp HTTP: reader-bearer mutations denied before
    writes; forged invite identity rejected as invalid input."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = os.path.realpath(os.path.join(self.tmp.name, "data"))
        os.makedirs(self.data_dir)
        ws_dir = os.path.join(self.data_dir, "ws1")
        self.db_path = str(backend_fixtures.make_workspace(
            ws_dir, schema_version=8))
        self.app = _asgi.HostedApp(_asgi.HostedConfig(
            data_dir=self.data_dir,
            grants_db=os.path.join(self.tmp.name, "grants.sqlite3")))
        _conn.register_workspace(self.app.store, "acme", self.db_path,
                                 self.data_dir)
        con = sqlite3.connect(self.db_path, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            _collab.create_workspace(
                con, "local", "Acme", "oidc:https://i.example:owner")
            invite = _collab.create_invite(con, "local", "ed-1", "editor",
                                           "oidc:https://i.example:owner")
            _collab.accept_invite(con, invite["invite_id"], "ed-1")
            _collab.create_invite(con, "local", "newcomer", "viewer",
                                  "oidc:https://i.example:owner")
            con.commit()
        finally:
            con.close()
        issued = self.app.store.issue_grant(
            member_id="ed-1", member_role="editor", client_id="c1",
            workspace_id="acme", scopes=["research:read", "transcripts:read"],
            content_policy={})
        self.reader_token = issued["access_token"]
        session_id = "sess-k02-1"
        digest = __import__("hashlib").sha256(
            f"ew-browser-v1:{session_id}".encode()).hexdigest()
        now = time.time()
        raw = self.app.store.auth_connection()
        try:
            _auth.ensure_auth_schema(raw)
            raw.execute(
                "INSERT INTO browser_sessions (session_hash, issuer, subject,"
                " email, email_verified, csrf_token, active_workspace,"
                " created_at, expires_at, revoked_at)"
                " VALUES (?, 'https://i.example', 'owner', 'owner@x.example',"
                " 1, 'tok123', 'acme', ?, ?, NULL)",
                (digest, now, now + 3600))
            raw.commit()
        finally:
            raw.close()
        self.session_id = session_id

    def tearDown(self):
        self.tmp.cleanup()

    def _call(self, path, method="POST", body=None, headers=None, *, workspace_revision=True,
              raw_headers=(), query=""):
        headers = dict(headers or {})
        if workspace_revision and method in ("POST", "PUT", "DELETE") and \
                not path.startswith("/api/v1/session/") and "cookie" in headers and \
                not any(key.lower() == "x-earwitness-workspace-switch-revision" for key in headers):
            status, session = self._call("/api/v1/session", method="GET", headers=headers)
            if status == 200 and session.get("authenticated"):
                headers["x-earwitness-workspace-switch-revision"] = str(session["workspace_switch_revision"])
        raw_body = json.dumps(body or {}).encode()
        hdrs = [(b"host", b"127.0.0.1:8771"),
                (b"content-length", str(len(raw_body)).encode())]
        for key, value in (headers or {}).items():
            hdrs.append((key.encode(), value.encode()))
        hdrs.extend(raw_headers)
        scope = {"type": "http", "method": method, "path": path,
                 "scheme": "http", "headers": hdrs, "query_string": query.encode()}
        sent = []

        async def receive():
            return {"type": "http.request", "body": raw_body,
                    "more_body": False}

        async def send(message):
            sent.append(message)

        asyncio.run(self.app(scope, receive, send))
        status = next(m["status"] for m in sent
                      if m["type"] == "http.response.start")
        payload = b"".join(m.get("body", b"") for m in sent
                           if m["type"] == "http.response.body")
        try:
            data = json.loads(payload.decode() or "{}")
        except ValueError:
            data = {}
        return status, data

    def _csrf(self):
        return {"x-csrf-token": "tok123",
                "origin": "http://127.0.0.1:8771/",
                "cookie": f"ew_session={self.session_id}"}

    def test_reader_bearer_save_denied_before_write(self):
        status, _data = self._call(
            "/api/v1/customer/saved-items",
            body={"type": "query", "title": "reader probe",
                  "query": {"mode": "mentions", "q": "lumen"}},
            headers={"authorization": f"Bearer {self.reader_token}"})
        self.assertEqual(status, 403)
        con = sqlite3.connect(self.db_path)
        try:
            names = [r[0] for r in con.execute(
                "SELECT title FROM customer_saved_items").fetchall()]
        finally:
            con.close()
        self.assertNotIn("reader probe", names)

    def test_forged_invite_email_rejected(self):
        con = sqlite3.connect(self.db_path)
        try:
            row = con.execute("SELECT id FROM invites WHERE member='newcomer'"
                              ).fetchone()
        finally:
            con.close()
        status, _data = self._call(
            f"/api/v1/workspaces/acme/invites/{row[0]}/accept",
            body={"verified_email": "newcomer"},
            headers=self._csrf())
        self.assertEqual(status, 400)

    def test_session_and_customer_context_tokens_match_admitted_owner_and_viewer(self):
        con = sqlite3.connect(self.db_path, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            principal = "oidc:https://i.example:viewer"
            invite = _collab.create_invite(
                con, "local", principal, "viewer", "oidc:https://i.example:owner")
            _collab.accept_invite(con, invite["invite_id"], principal)
        finally:
            con.close()
        viewer_session = "sess-context-viewer"
        control = self.app.store.auth_connection()
        try:
            now = time.time()
            for subject, identity in (("viewer", viewer_session), ("outsider", "sess-context-outsider")):
                control.execute(
                    "INSERT INTO browser_sessions(session_hash,issuer,subject,email,"
                    "email_verified,csrf_token,active_workspace,created_at,expires_at)"
                    " VALUES (?,'https://i.example',?,'',0,'tok123','acme',?,?)",
                    (_auth._hash_presented(identity), subject, now, now + 3600))
            control.commit()
        finally:
            control.close()
        contexts = {}
        for role, identity in (("owner", self.session_id), ("viewer", viewer_session)):
            with self.subTest(role=role):
                headers = {"cookie": f"ew_session={identity}"}
                session_status, session = self._call(
                    "/api/v1/session", method="GET", headers=headers)
                customer_status, overview = self._call(
                    "/api/v1/customer/overview", method="GET", headers=headers)
                self.assertEqual(session_status, 200)
                self.assertEqual(customer_status, 200)
                self.assertTrue(session["authenticated"])
                self.assertIsInstance(session["workspace_context"], str)
                contexts[role] = session["workspace_context"]
                self.assertEqual(session["workspace_context"], overview["workspace_context"])
        self.assertNotEqual(contexts["owner"], contexts["viewer"])
        headers = {"cookie": "ew_session=sess-context-outsider"}
        status, account = self._call("/api/v1/session", method="GET", headers=headers)
        self.assertEqual(status, 200)
        self.assertTrue(account["authenticated"])
        self.assertIsNone(account["workspace_context"])
        status, _ = self._call("/api/v1/customer/overview", method="GET", headers=headers)
        self.assertEqual(status, 403)

    def test_workspace_switch_returns_fresh_canonical_session_context(self):
        headers = self._csrf()
        other_session = "sess-switch-other"
        control = self.app.store.auth_connection()
        try:
            now = time.time()
            control.execute(
                "INSERT INTO browser_sessions(session_hash,issuer,subject,email,"
                "email_verified,csrf_token,active_workspace,created_at,expires_at)"
                " VALUES (?,'https://i.example','other-owner','',0,'tok123','',?,?)",
                (_auth._hash_presented(other_session), now, now + 3600))
            control.commit()
        finally:
            control.close()
        status, _ = self._call(
            "/api/v1/workspaces", body={"workspace_id": "context-b", "name": "Empty B"},
            headers=headers)
        self.assertEqual(status, 200)
        status, _ = self._call(
            "/api/v1/workspaces", body={"workspace_id": "nonmember-c", "name": "Empty C"},
            headers={**headers, "cookie": f"ew_session={other_session}"})
        self.assertEqual(status, 200)
        status, before = self._call("/api/v1/session", method="GET", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(before["active_workspace"], "acme")
        for target, csrf in (("unknown", "tok123"), ("nonmember-c", "tok123"),
                             ("context-b", "invalid")):
            with self.subTest(target=target, csrf=csrf):
                status, _ = self._call(
                    "/api/v1/session/workspace", body={"workspace_id": target,
                    "expected_workspace_switch_revision": before["workspace_switch_revision"]},
                    headers={**headers, "x-csrf-token": csrf})
                self.assertEqual(status, 403)
                status, unchanged = self._call("/api/v1/session", method="GET", headers=headers)
                self.assertEqual(status, 200)
                self.assertEqual(unchanged["active_workspace"], before["active_workspace"])
                self.assertEqual(unchanged["workspace_context"], before["workspace_context"])
        status, switched = self._call(
            "/api/v1/session/workspace", body={"workspace_id": "context-b",
            "expected_workspace_switch_revision": before["workspace_switch_revision"]}, headers=headers)
        self.assertEqual(status, 200)
        self.assertTrue(switched["ok"])
        self.assertIsInstance(switched.get("workspace_context"), str)
        self.assertIn("frontend_contract", switched)
        self.assertNotEqual(switched["workspace_context"], before["workspace_context"])
        status, after = self._call("/api/v1/session", method="GET", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual({key: switched[key] for key in after}, after)
        self.assertEqual(after["active_workspace"], "context-b")
        status, overview = self._call("/api/v1/customer/overview", method="GET", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(overview["workspace_context"], switched["workspace_context"])


class SessionSwitchOrderingTest(unittest.TestCase):
    def setUp(self):
        self.http = HostedReaderHttpTest()
        self.http.setUp()
        self.addCleanup(self.http.tearDown)
        self.headers = self.http._csrf()
        for name in ("ordered-b", "ordered-c"):
            status, _ = self.http._call("/api/v1/workspaces",
                body={"workspace_id": name, "name": name}, headers=self.headers)
            self.assertEqual(status, 200)

    def session(self):
        status, data = self.http._call("/api/v1/session", method="GET", headers=self.headers)
        self.assertEqual(status, 200)
        return data

    def switch(self, target, revision):
        return self.http._call("/api/v1/session/workspace",
            body={"workspace_id": target, "expected_workspace_switch_revision": revision},
            headers=self.headers)

    async def async_switch(self, target, revision, receive_gate=None):
        messages = []
        payload = json.dumps({"workspace_id": target,
                              "expected_workspace_switch_revision": revision}).encode()
        scope = {"type": "http", "method": "POST", "path": "/api/v1/session/workspace",
                 "scheme": "http", "query_string": b"",
                 "headers": [(b"host", b"127.0.0.1:8771"),
                             (b"content-length", str(len(payload)).encode())] +
                            [(key.encode(), value.encode()) for key, value in self.headers.items()]}

        async def receive():
            if receive_gate is not None:
                await receive_gate()
            return {"type": "http.request", "body": payload, "more_body": False}

        async def send(message):
            messages.append(message)

        await self.http.app(scope, receive, send)
        return (next(message["status"] for message in messages
                     if message["type"] == "http.response.start"),
                json.loads(b"".join(message.get("body", b"") for message in messages
                                   if message["type"] == "http.response.body")))

    def test_required_revision_rejects_missing_bool_and_invalid_without_mutation(self):
        before = self.session()
        for body, expected in (({"workspace_id": "ordered-b"}, 428),
                ({"workspace_id": "ordered-b", "expected_workspace_switch_revision": True}, 400),
                ({"workspace_id": "ordered-b", "expected_workspace_switch_revision": -1}, 400),
                ({"workspace_id": "ordered-b", "expected_workspace_switch_revision": 0.0}, 400)):
            with self.subTest(body=body):
                status, _ = self.http._call("/api/v1/session/workspace", body=body, headers=self.headers)
                self.assertEqual(status, expected)
                self.assertEqual(self.session(), before)

    def test_competing_requests_have_exactly_one_committed_selection(self):
        revision = self.session()["workspace_switch_revision"]

        async def race():
            ready = asyncio.Event()
            arrived = 0

            async def gate():
                nonlocal arrived
                arrived += 1
                if arrived == 2:
                    ready.set()
                await ready.wait()

            return await asyncio.gather(self.async_switch("ordered-b", revision, gate),
                                        self.async_switch("ordered-c", revision, gate))

        results = asyncio.run(race())
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        winner = next(data for status, data in results if status == 200)
        rejected = next(data for status, data in results if status == 409)
        current = self.session()
        self.assertEqual(current["workspace_switch_revision"], revision + 1)
        self.assertEqual(winner["workspace_switch_revision"], revision + 1)
        self.assertEqual(rejected["error"]["details"]["workspace_switch_revision"], revision + 1)
        self.assertEqual(current["active_workspace"], winner["active_workspace"])

    def test_aba_and_same_workspace_consume_revision_without_lifetime_reset(self):
        original = self.session()
        revision = original["workspace_switch_revision"]
        for target in ("ordered-c", original["active_workspace"], original["active_workspace"]):
            status, selected = self.switch(target, revision)
            self.assertEqual(status, 200)
            revision += 1
            self.assertEqual(selected["workspace_switch_revision"], revision)
        before = self.session()
        status, error = self.switch("ordered-b", original["workspace_switch_revision"])
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["details"]["workspace_switch_revision"], revision)
        self.assertEqual(self.session(), before)
        control = self.http.app.store.auth_connection()
        try:
            row = control.execute("SELECT created_at,expires_at,workspace_switch_revision"
                                  " FROM browser_sessions WHERE session_hash=?",
                                  (_auth._hash_presented(self.http.session_id),)).fetchone()
            _auth.ensure_auth_schema(control)
            after = control.execute("SELECT created_at,expires_at,workspace_switch_revision"
                                    " FROM browser_sessions WHERE session_hash=?",
                                    (_auth._hash_presented(self.http.session_id),)).fetchone()
            self.assertEqual(tuple(after), tuple(row))
        finally:
            control.close()

    def test_delayed_old_request_cannot_commit_after_successful_retry_and_get(self):
        revision = self.session()["workspace_switch_revision"]

        async def scenario():
            waiting, release = asyncio.Event(), asyncio.Event()

            async def hold():
                waiting.set()
                await release.wait()

            old = asyncio.create_task(self.async_switch("ordered-b", revision, hold))
            await waiting.wait()
            accepted = await self.async_switch("ordered-c", revision)
            self.assertEqual(accepted[0], 200)
            observed = await asyncio.to_thread(self.session)
            self.assertEqual(observed["active_workspace"], "ordered-c")
            release.set()
            rejected = await old
            self.assertEqual(rejected[0], 409)
            return observed

        observed = asyncio.run(scenario())
        self.assertEqual(self.session(), observed)

    def test_unchosen_authenticated_session_exposes_zero_revision(self):
        control = self.http.app.store.auth_connection()
        try:
            now = time.time()
            control.execute("INSERT INTO browser_sessions(session_hash,issuer,subject,csrf_token,"
                            "created_at,expires_at) VALUES(?,'https://i.example','owner','tok123',?,?)",
                            (_auth._hash_presented("sess-unchosen"), now, now + 3600))
            control.commit()
        finally:
            control.close()
        status, session = self.http._call("/api/v1/session", method="GET",
            headers={**self.headers, "cookie": "ew_session=sess-unchosen"})
        self.assertEqual(status, 200)
        self.assertEqual(session["active_workspace"], "")
        self.assertIs(type(session["workspace_switch_revision"]), int)
        self.assertEqual(session["workspace_switch_revision"], 0)

    def test_explicit_old_schema_preparation_and_lookup_never_repairs(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        self.addCleanup(con.close)
        con.execute("CREATE TABLE browser_sessions(session_hash TEXT PRIMARY KEY,issuer TEXT NOT NULL,"
                    "subject TEXT NOT NULL,email TEXT NOT NULL DEFAULT '',email_verified INTEGER NOT NULL DEFAULT 0,"
                    "csrf_token TEXT NOT NULL,active_workspace TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL,"
                    "expires_at REAL NOT NULL,revoked_at REAL)")
        con.execute("INSERT INTO browser_sessions(session_hash,issuer,subject,csrf_token,created_at,expires_at)"
                    " VALUES(?,'https://i.example','owner','token',0,100)",
                    (_auth._hash_presented("old-session"),))
        con.commit()
        before = list(con.iterdump())
        with self.assertRaises(sqlite3.OperationalError):
            _auth.lookup_session(con, "old-session", now=1)
        self.assertEqual(list(con.iterdump()), before)
        _auth.ensure_auth_schema(con)
        con.commit()
        self.assertEqual(_auth.lookup_session(con, "old-session", now=1)["workspace_switch_revision"], 0)

    def test_revocation_during_request_body_admission_never_mutates_selection(self):
        revision = self.session()["workspace_switch_revision"]

        async def scenario():
            waiting, release = asyncio.Event(), asyncio.Event()

            async def hold():
                waiting.set()
                await release.wait()

            pending = asyncio.create_task(self.async_switch("ordered-b", revision, hold))
            await waiting.wait()
            control = self.http.app.store.auth_connection()
            try:
                control.execute("UPDATE browser_sessions SET revoked_at=? WHERE session_hash=?",
                                (time.time(), _auth._hash_presented(self.http.session_id)))
                control.commit()
            finally:
                control.close()
            release.set()
            rejected = await pending
            # The body is read before authentication (so no lock waits on
            # an upload): a revocation during the upload is refused at
            # admission as signed out.
            self.assertEqual(rejected[0], 401)

        asyncio.run(scenario())
        control = self.http.app.store.auth_connection()
        try:
            row = control.execute("SELECT active_workspace,workspace_switch_revision"
                                  " FROM browser_sessions WHERE session_hash=?",
                                  (_auth._hash_presented(self.http.session_id),)).fetchone()
            self.assertEqual(tuple(row), ("acme", revision))
        finally:
            control.close()


class SessionMutationOrderingTest(unittest.TestCase):
    HEADER = "x-earwitness-workspace-switch-revision"

    def setUp(self):
        self.fixture = HostedReaderHttpTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.headers = self.fixture._csrf()
        status, _ = self.fixture._call("/api/v1/workspaces",
            body={"workspace_id": "mutation-b", "name": "Empty B"}, headers=self.headers)
        self.assertEqual(status, 200)

    def test_reload_old_form_epoch_denies_preferences_in_new_workspace(self):
        status, displayed = self.fixture._call("/api/v1/session", method="GET", headers=self.headers)
        self.assertEqual(status, 200)
        status, old_prefs = self.fixture._call("/api/v1/customer/preferences", method="GET", headers=self.headers)
        self.assertEqual(status, 200)
        revision = displayed["workspace_switch_revision"]
        status, _ = self.fixture._call("/api/v1/session/workspace",
            body={"workspace_id": "mutation-b", "expected_workspace_switch_revision": revision},
            headers=self.headers)
        self.assertEqual(status, 200)
        status, current = self.fixture._call("/api/v1/customer/preferences", method="GET", headers=self.headers)
        self.assertEqual(status, 200)
        self.assertEqual(current["preferences"]["revision"], old_prefs["preferences"]["revision"])
        status, error = self.fixture._call("/api/v1/customer/preferences", method="PUT",
            body={"workspace_name": "Wrong target", "expected_revision": old_prefs["preferences"]["revision"]},
            headers={**self.headers, self.HEADER: str(revision)})
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["details"]["workspace_switch_revision"], revision + 1)
        status, after = self.fixture._call("/api/v1/customer/preferences", method="GET", headers=self.headers)
        self.assertEqual(status, 200)
        self.assertEqual(after, current)

    def test_missing_and_malformed_epoch_are_nonmutating(self):
        status, before = self.fixture._call("/api/v1/customer/preferences", method="GET", headers=self.headers)
        self.assertEqual(status, 200)
        for value, expected in ((None, 428), ("true", 400), ("-1", 400), ("+0", 400),
                                ("0.0", 400), ("0,0", 400), ("١", 400), ("", 400),
                                (" 0", 400), ("00", 400), ("0\n", 400), ("9" * 19, 400),
                                ("9" * 5000, 400)):
            with self.subTest(value=value):
                self.fixture.app.limiter = _asgi.RateLimiter()
                headers = dict(self.headers)
                if value is not None:
                    headers[self.HEADER] = value
                stores = self._stores()
                status, _ = self.fixture._call("/api/v1/customer/preferences", method="PUT",
                    body={"workspace_name": "Forbidden", "expected_revision": before["preferences"]["revision"]},
                    headers=headers, workspace_revision=False)
                self.assertEqual(status, expected)
                self.assertEqual(self._stores(), stores)
                status, after = self.fixture._call("/api/v1/customer/preferences", method="GET", headers=self.headers)
                self.assertEqual(status, 200)
                self.assertEqual(after["preferences"], before["preferences"])

    def test_duplicate_epoch_headers_are_rejected_even_when_each_is_current(self):
        status, displayed = self.fixture._call("/api/v1/session", method="GET", headers=self.headers)
        self.assertEqual(status, 200)
        current = str(displayed["workspace_switch_revision"]).encode()
        stale = str(displayed["workspace_switch_revision"] - 1).encode()
        for raw in ([(self.HEADER.encode(), current), (self.HEADER.encode(), current)],
                    [(self.HEADER.encode(), stale), (b"X-EarWitness-Workspace-Switch-Revision", current)],
                    [(self.HEADER.encode(), current), (self.HEADER.encode(), stale)]):
            with self.subTest(raw=raw):
                stores = self._stores()
                status, error = self.fixture._call("/api/v1/customer/preferences", method="PUT",
                    body={"workspace_name": "Duplicated", "expected_revision": 0},
                    headers=self.headers, workspace_revision=False, raw_headers=raw)
                self.assertEqual(status, 400)
                self.assertEqual(error["error"]["code"], "INVALID_ARGUMENT")
                self.assertEqual(self._stores(), stores)

    def test_aba_stale_epoch_denies_customer_member_and_billing_writes(self):
        status, displayed = self.fixture._call("/api/v1/session", method="GET", headers=self.headers)
        self.assertEqual(status, 200)
        revision = displayed["workspace_switch_revision"]
        for target, basis in (("mutation-b", revision), ("acme", revision + 1)):
            status, _ = self.fixture._call("/api/v1/session/workspace",
                body={"workspace_id": target, "expected_workspace_switch_revision": basis},
                headers=self.headers)
            self.assertEqual(status, 200)
        status, current = self.fixture._call("/api/v1/session", method="GET", headers=self.headers)
        self.assertEqual((current["active_workspace"], current["workspace_switch_revision"]),
                         ("acme", revision + 2))
        stale = {**self.headers, self.HEADER: str(revision)}
        for path, method, body in (
                ("/api/v1/customer/preferences", "PUT",
                 {"workspace_name": "ABA", "expected_revision": 0}),
                ("/api/v1/workspaces/acme/invites", "POST",
                 {"member": "aba-member", "role": "viewer"}),
                ("/api/v1/billing/checkout", "POST",
                 {"price_id": "price_x", "success_url": "http://127.0.0.1:8771/ok",
                  "cancel_url": "http://127.0.0.1:8771/cancel"}),
                ("/api/v1/shares", "POST", {}),
                ("/api/v1/workspaces/acme/members/ed-1", "PUT", {"role": "viewer"}),
                ("/api/v1/workspaces/acme/members/ed-1", "DELETE", None),
                ("/api/v1/workspaces/acme/invites/any/deliver", "POST", {})):
            with self.subTest(path=path):
                stores = self._stores()
                status, error = self.fixture._call(path, method=method, body=body, headers=stale)
                self.assertEqual(status, 409)
                self.assertEqual(error["error"]["code"], "WORKSPACE_SWITCH_CONFLICT")
                self.assertEqual(error["workspace_switch_revision"], revision + 2)
                self.assertEqual(self._stores(), stores)

    def test_non_utf8_header_byte_is_rejected_without_writes(self):
        stores = self._stores()
        status, error = self.fixture._call("/api/v1/customer/preferences", method="PUT",
            body={"workspace_name": "Latin", "expected_revision": 0}, headers=self.headers,
            workspace_revision=False, raw_headers=[(self.HEADER.encode(), b"\xb9")])
        self.assertEqual(status, 400)
        self.assertEqual(error["error"]["code"], "INVALID_ARGUMENT")
        self.assertEqual(self._stores(), stores)

    def test_research_query_exemption_is_exact_not_a_member_suffix(self):
        member = "oidc:https://i.example:team/research-query"
        con = _db.open_existing(self.fixture.db_path, readonly=False)
        try:
            invite = _collab.create_invite(con, "local", member, "viewer",
                                           "oidc:https://i.example:owner")
            _collab.accept_invite(con, invite["invite_id"], member)
            con.commit()
        finally:
            con.close()
        path = "/api/v1/workspaces/acme/members/" + member
        status, displayed = self.fixture._call("/api/v1/session", method="GET", headers=self.headers)
        revision = displayed["workspace_switch_revision"]
        for headers, expected in ((self.headers, 428),
                                  ({**self.headers, self.HEADER: str(revision + 7)}, 409)):
            for method in ("PUT", "DELETE"):
                with self.subTest(method=method, expected=expected):
                    stores = self._stores()
                    status, _ = self.fixture._call(path, method=method, body={"role": "editor"},
                                                   headers=headers, workspace_revision=False)
                    self.assertEqual(status, expected)
                    self.assertEqual(self._stores(), stores)
        con = _db.open_existing(self.fixture.db_path, readonly=True)
        try:
            aggregate = _db.aggregate_revision(con)
        finally:
            con.close()
        status, changed = self.fixture._call(path, method="PUT",
            body={"role": "editor", "expected_revision": aggregate},
            headers={**self.headers, self.HEADER: str(revision)})
        self.assertEqual(status, 200, changed)
        self.assertEqual(changed["member"], member)

    def test_switch_committed_after_lookup_conflicts_with_lookup_revision(self):
        self._switch_after_lookup(lambda revision: revision)

    def test_switch_committed_after_lookup_conflicts_even_with_next_revision(self):
        self._switch_after_lookup(lambda revision: revision + 1)

    def test_same_workspace_reselection_after_lookup_conflicts_with_lookup_revision(self):
        self._switch_after_lookup(lambda revision: revision, target="acme")

    def _switch_after_lookup(self, presented, target="mutation-b"):
        from unittest import mock
        from earwitness.app import services as _services
        status, displayed = self.fixture._call("/api/v1/session", method="GET", headers=self.headers)
        revision = displayed["workspace_switch_revision"]
        status, prefs = self.fixture._call("/api/v1/customer/preferences", method="GET", headers=self.headers)
        original = _services.open_customer_workspace
        raced = []

        def switch_after_lookup(store, workspace_id, *args, **kwargs):
            if not raced and kwargs.get("readonly") is False:
                raced.append(workspace_id)
                control = store.auth_connection()
                try:
                    control.execute("BEGIN IMMEDIATE")
                    _auth.set_active_workspace(control, self.fixture.session_id, target, revision)
                    control.commit()
                finally:
                    control.close()
            return original(store, workspace_id, *args, **kwargs)

        stores = self._stores()
        with mock.patch.object(_services, "open_customer_workspace", switch_after_lookup):
            status, error = self.fixture._call("/api/v1/customer/preferences", method="PUT",
                body={"workspace_name": "Raced into A", "expected_revision": prefs["preferences"]["revision"]},
                headers={**self.headers, self.HEADER: str(presented(revision))})
        self.assertEqual(raced, ["acme"])
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "WORKSPACE_SWITCH_CONFLICT")
        self.assertEqual(error["workspace_switch_revision"], revision + 1)
        after = self._stores()
        for path in stores:
            if not path.endswith("grants.sqlite3"):
                self.assertEqual(after[path], stores[path], path)
        status, current = self.fixture._call("/api/v1/session", method="GET", headers=self.headers)
        self.assertEqual((current["active_workspace"], current["workspace_switch_revision"]),
                         (target, revision + 1))

    def test_bearer_write_never_consults_the_browser_epoch(self):
        status, error = self.fixture._call("/api/v1/customer/preferences", method="PUT",
            body={"workspace_name": "Bearer", "expected_revision": 0},
            headers={"authorization": f"Bearer {self.fixture.reader_token}"})
        self.assertEqual(status, 403)
        self.assertNotEqual(error["error"]["code"], "PRECONDITION_REQUIRED")

    def test_unknown_guest_share_releases_its_rate_slots(self):
        for _ in range(4):
            status, error = self.fixture._call("/api/v1/shared/search", method="GET",
                headers=self.headers, query="share_id=bogus&workspace_id=acme")
            self.assertEqual(status, 404, error)
        status, _ = self.fixture._call("/api/v1/customer/preferences", method="GET", headers=self.headers)
        self.assertEqual(status, 200)

    def test_invite_accept_keeps_its_named_authority_without_epoch(self):
        con = sqlite3.connect(self.fixture.db_path)
        try:
            invite = con.execute("SELECT id FROM invites WHERE member='newcomer'").fetchone()[0]
        finally:
            con.close()
        status, error = self.fixture._call(f"/api/v1/workspaces/acme/invites/{invite}/accept",
            body={"verified_email": "newcomer"}, headers=self.headers, workspace_revision=False)
        self.assertEqual(status, 400)
        self.assertNotEqual(error["error"]["code"], "PRECONDITION_REQUIRED")

    def _stores(self):
        dump = {}
        for path in sorted(pathlib.Path(self.fixture.tmp.name).rglob("*.sqlite3")):
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                tables = [r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
                dump[str(path)] = {t: sorted(map(repr, con.execute(f'SELECT * FROM "{t}"')))
                                   for t in tables}
            finally:
                con.close()
        return dump

    def test_slow_upload_never_holds_the_control_lock(self):
        import asyncio, json as _json, time as _time
        fx = self.fixture
        fx.app.limiter = _asgi.RateLimiter()
        status, session = fx._call("/api/v1/session", method="GET", headers=self.headers)
        self.assertEqual(status, 200)
        status, prefs = fx._call("/api/v1/customer/preferences", method="GET", headers=self.headers)
        self.assertEqual(status, 200)

        async def request(path, method, body, headers, delay=0.0):
            raw = _json.dumps(body).encode() if body is not None else b""
            hdrs = [(b"host", b"127.0.0.1:8771"), (b"content-length", str(len(raw)).encode())]
            hdrs += [(k.encode(), v.encode()) for k, v in headers.items()]
            sent = []

            async def receive():
                await asyncio.sleep(delay)  # a slow client upload
                return {"type": "http.request", "body": raw, "more_body": False}

            async def send(message):
                sent.append(message)
            started = _time.monotonic()
            await fx.app({"type": "http", "method": method, "path": path, "scheme": "http",
                          "headers": hdrs, "query_string": b""}, receive, send)
            return (next(m["status"] for m in sent if m["type"] == "http.response.start"),
                    _time.monotonic() - started)

        async def other():
            await asyncio.sleep(0.2)
            return await request("/api/v1/session", "GET", None, self.headers)

        async def both():
            return await asyncio.gather(
                request("/api/v1/customer/preferences", "PUT",
                        {"workspace_name": "Slow upload",
                         "expected_revision": prefs["preferences"]["revision"]},
                        {**self.headers, self.HEADER: str(session["workspace_switch_revision"])},
                        delay=1.5),
                other())
        (slow_status, _), (other_status, other_elapsed) = asyncio.run(both())
        self.assertEqual(slow_status, 200)
        self.assertEqual(other_status, 200)
        self.assertLess(other_elapsed, 1.0, "a concurrent request waited on the upload")
        status, error = fx._call("/api/v1/customer/preferences", method="PUT",
            body={"workspace_name": "x" * (_asgi.MAX_BODY_BYTES + 1)},
            headers={**self.headers, self.HEADER: str(session["workspace_switch_revision"])})
        self.assertEqual(status, 413, error)

    def test_provider_call_runs_after_the_control_lock_is_released(self):
        from unittest import mock
        from earwitness.app import billing as _billing
        from earwitness.app import services as _services
        status, session = self.fixture._call("/api/v1/session", method="GET", headers=self.headers)
        self.assertEqual(status, 200)
        # In-app checkout is off by default since BY1 (design 7.3); this test
        # is about lock ordering, so it turns the route on explicitly.
        config = _billing.BillingConfig(secret_key="sk_test_x", webhook_secret="whsec_x",
                                        success_url="/ok", cancel_url="/cancel",
                                        prices={"price_pro": "Pro"},
                                        inapp_checkout=True)
        seen = {}

        def provider(*_args, **_kwargs):
            con = self.fixture.app.store._connect()
            try:
                con.execute("PRAGMA busy_timeout=0")
                con.execute("BEGIN IMMEDIATE")
                con.rollback()
                seen["lock_free"] = True
            except sqlite3.OperationalError:
                seen["lock_free"] = False
            finally:
                con.close()
            return {"id": "cs_1", "url": "https://pay.example/c1", "checkout_url": "https://pay.example/c1"}
        with mock.patch.object(_services, "_billing_config", return_value=config), \
                mock.patch.object(_billing, "create_checkout_session", side_effect=provider):
            status, out = self.fixture._call("/api/v1/billing/checkout", method="POST",
                body={"price_id": "price_pro"},
                headers={**self.headers, self.HEADER: str(session["workspace_switch_revision"])})
        self.assertEqual(status, 200, out)
        self.assertEqual(seen, {"lock_free": True})

    def test_valid_epoch_mutation_commits_before_later_switch(self):
        status, displayed = self.fixture._call("/api/v1/session", method="GET", headers=self.headers)
        self.assertEqual(status, 200)
        status, before = self.fixture._call("/api/v1/customer/preferences", method="GET", headers=self.headers)
        self.assertEqual(status, 200)
        revision = displayed["workspace_switch_revision"]
        status, saved = self.fixture._call("/api/v1/customer/preferences", method="PUT",
            body={"workspace_name": "Saved in A", "expected_revision": before["preferences"]["revision"]},
            headers={**self.headers, self.HEADER: str(revision)})
        self.assertEqual(status, 200)
        self.assertEqual(saved["preferences"]["workspace_name"], "Saved in A")
        status, _ = self.fixture._call("/api/v1/session/workspace",
            body={"workspace_id": "mutation-b", "expected_workspace_switch_revision": revision},
            headers=self.headers)
        self.assertEqual(status, 200)
        status, b = self.fixture._call("/api/v1/customer/preferences", method="GET", headers=self.headers)
        self.assertEqual(status, 200)
        self.assertNotEqual(b["preferences"].get("workspace_name"), "Saved in A")


@unittest.skipUnless(importlib.util.find_spec("uvicorn"), "needs uvicorn")
class HostedMemberUrlHttpTest(unittest.TestCase):
    def test_real_tcp_decoding_preserves_member_identity_and_guards(self):
        import uvicorn
        from urllib.parse import quote, unquote

        fixture = HostedReaderHttpTest()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        subject = "routing%2Fviewer%25"
        member = "oidc:https://i.example:" + subject
        decoy = unquote(member)
        con = _db.open_existing(fixture.db_path, readonly=False)
        try:
            for identity, role in ((member, "viewer"), (decoy, "editor")):
                invite = _collab.create_invite(
                    con, "local", identity, role, "oidc:https://i.example:owner")
                _collab.accept_invite(con, invite["invite_id"], identity)
        finally:
            con.close()
        viewer_session = "sess-routing-viewer"
        control = fixture.app.store.auth_connection()
        try:
            now = time.time()
            control.execute(
                "INSERT INTO browser_sessions(session_hash,issuer,subject,email,"
                "email_verified,csrf_token,active_workspace,created_at,expires_at)"
                " VALUES (?,'https://i.example',?,'',0,'tok123','acme',?,?)",
                (_auth._hash_presented(viewer_session), subject, now, now + 3600))
            control.commit()
        finally:
            control.close()
        observed = []

        async def app(scope, receive, send):
            if (b"host", f"127.0.0.1:{port}".encode()) in scope["headers"]:   # not outside probes
                observed.append((scope["path"], scope["raw_path"]))
            await fixture.app(scope, receive, send)

        ready = threading.Event()

        class ReadyServer(uvicorn.Server):
            async def startup(self, sockets=None):
                await super().startup(sockets=sockets)
                ready.set()

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        fixture.app.config.public_base_url = origin
        fixture.app.config.allowed_origins = (origin,)
        server = ReadyServer(uvicorn.Config(app, host="127.0.0.1", port=port,
                                            http="h11", lifespan="off", log_level="critical"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)

        def stop():
            server.should_exit = True
            thread.join(10)
            self.assertFalse(thread.is_alive())

        thread.start()
        self.addCleanup(stop)
        self.assertTrue(ready.wait(10), "owned ephemeral ASGI server did not start")

        def request(method, path, body=None, session=fixture.session_id, extra=None):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            headers = {"cookie": f"ew_session={session}", "origin": origin,
                       "x-csrf-token": "tok123", **(extra or {})}
            if method != "GET" and "x-earwitness-workspace-switch-revision" not in headers:
                status, current = request("GET", "/api/v1/session", session=session)
                assert status == 200 and current["authenticated"], current
                headers["x-earwitness-workspace-switch-revision"] = str(current["workspace_switch_revision"])
            try:
                connection.request(method, path, body=json.dumps(body or {}), headers=headers)
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        def state():
            con = _db.open_existing(fixture.db_path, readonly=True)
            try:
                return list(con.iterdump())
            finally:
                con.close()

        encoded = quote(member, safe="")
        path = "/api/v1/workspaces/acme/members/" + encoded
        status, listed = request("GET", "/api/v1/workspaces/acme/members")
        self.assertEqual(status, 200)
        status, session = request("GET", "/api/v1/session")
        current = session["workspace_switch_revision"]
        for extra, expected in (({"x-earwitness-workspace-switch-revision": str(current + 1)}, 409),
                                ({"x-earwitness-workspace-switch-revision": ""}, 400)):
            with self.subTest(epoch=extra, expected=expected):
                before = state()
                self.assertEqual(request("PUT", path, {"role": "editor"}, extra=extra)[0], expected)
                self.assertEqual(state(), before)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.request("DELETE", path, body="{}", headers={
                "cookie": f"ew_session={fixture.session_id}", "origin": origin, "x-csrf-token": "tok123"})
            missing = connection.getresponse()
            missing.read()
        finally:
            connection.close()
        self.assertEqual(missing.status, 428)
        self.assertIn(member, [row["member"] for row in listed["members"]])
        for method, target, body, session, extra, expected in (
            ("DELETE", "/api/v1/workspaces/foreign/members/" + encoded, None,
             fixture.session_id, None, 404),
            ("PUT", path, {"role": "editor"}, fixture.session_id, {"if-match": "invalid"}, 400),
            ("DELETE", path, None, fixture.session_id, {"x-csrf-token": "invalid"}, 403),
            ("DELETE", path, None, viewer_session, None, 403),
        ):
            with self.subTest(method=method, path=target, expected=expected):
                before = state()
                self.assertEqual(request(method, target, body, session, extra)[0], expected)
                self.assertEqual(state(), before)
        con = _db.open_existing(fixture.db_path, readonly=True)
        try:
            revision = _db.aggregate_revision(con)
        finally:
            con.close()
        status, changed = request("PUT", path, {"role": "editor", "expected_revision": revision})
        self.assertEqual(status, 200)
        self.assertEqual(changed["member"], member)
        stray = http.client.HTTPConnection("127.0.0.1", port, timeout=10)  # an outside probe
        try:
            stray.request("GET", "/", headers={"host": f"localhost:{port}"})
            stray.getresponse().read()
        finally:
            stray.close()
        self.assertEqual(observed[-1], ("/api/v1/workspaces/acme/members/" + member,
                                        path.encode("ascii")))
        status, removed = request("DELETE", path)
        self.assertEqual(status, 200)
        self.assertEqual(removed["member"], member)
        status, listed = request("GET", "/api/v1/workspaces/acme/members")
        self.assertEqual(status, 200)
        members = {row["member"]: row for row in listed["members"]}
        self.assertEqual(members[member]["role"], "editor")
        self.assertEqual(members[member]["revoked"], 1)
        self.assertEqual(members[decoy]["role"], "editor")
        self.assertEqual(members[decoy]["revoked"], 0)
        self.assertEqual(members["oidc:https://i.example:owner"]["revoked"], 0)


if __name__ == "__main__":
    unittest.main()
