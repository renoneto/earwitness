"""L17: uniform refusals on guest share and invite routes (red-capable).

L8b (D4): an account that is not the named audience or invitee could tell
a real share, invite or workspace from a fabricated one by the status and
message of the refusal. The rule under test: for such a caller every
not-entitled case answers with the exact response the app already gives
for an unknown object (404 NOT_FOUND "unknown object"). The named
audience or invitee keeps the specific messages (revoked, no longer
pending) because they help that person and reveal nothing to others.

The rate-limit tests cover the other half: a caller-supplied workspace id
on a guest or invite path must not spend that workspace's budget before
the caller is authorized.
"""
from __future__ import annotations

import asyncio
import gc
import json
import sqlite3
import time
import os
import unittest
import warnings
from unittest import mock

from earwitness.app import access as _access
from earwitness.app import asgi as _asgi
from earwitness.app import connections as _conn
from earwitness.app import services as _services
from earwitness.app import browser_auth as _ba
from earwitness.app import collaboration as _collab
from tests import backend_fixtures
from tests.test_backend_authority import HostedReaderHttpTest

ISSUER = "https://i.example"
OWNER = f"oidc:{ISSUER}:owner"
CARDS = {"unknown_object": (404, {
    "error": {"code": "NOT_FOUND", "details": {}, "message": "unknown object",
              "status": 404}, "ok": False})}
FAKE_SHARE = "share:ffffffffffff"
FAKE_INVITE = "invite:ffffffffffff"


class _Base(unittest.TestCase):
    def setUp(self):
        self.http = HostedReaderHttpTest()
        self.http.setUp()
        self.addCleanup(self.http.tearDown)
        self.app = self.http.app
        con = self._db()
        try:
            self.carol = f"oidc:{ISSUER}:carol"
            self.dave = f"oidc:{ISSUER}:dave"
            # carol is the named audience of one share per state.
            self.share = _collab.create_share(
                con, "local", "episode", "e1", self.carol, ["ep_title"],
                OWNER)["share_id"]
            self.revoked = _collab.create_share(
                con, "local", "episode", "e1", self.carol, ["ep_title"],
                OWNER)["share_id"]
            con.execute("UPDATE shares SET revoked=1 WHERE id=?", (self.revoked,))
            self.expired = _collab.create_share(
                con, "local", "episode", "e1", self.carol, ["ep_title"],
                OWNER)["share_id"]
            con.execute("UPDATE shares SET expires_at='2000-01-01T00:00:00+00:00'"
                        " WHERE id=?", (self.expired,))
            # carol is invited; dave's invite is already consumed.
            self.invite = _collab.create_invite(
                con, "local", self.carol, "viewer", OWNER)["invite_id"]
            self.consumed = _collab.create_invite(
                con, "local", self.dave, "viewer", OWNER)["invite_id"]
            _collab.accept_invite(con, self.consumed, self.dave)
            # An invitation addressed to an email, accepted by verification.
            self.by_email = _collab.create_invite(
                con, "local", "erin@example.test", "viewer", OWNER)["invite_id"]
        finally:
            con.close()
        self.sessions = {}

    def _db(self):
        con = sqlite3.connect(self.http.db_path, isolation_level=None)
        con.row_factory = sqlite3.Row
        return con

    def session(self, subject, *, active="", email="", verified=False,
                identity=None):
        identity = identity or "uniform-" + subject
        if identity in self.sessions:
            return self.sessions[identity]
        control = self.app.store._connect()
        try:
            control.execute(
                "INSERT INTO browser_sessions(session_hash,issuer,subject,email,"
                "email_verified,csrf_token,active_workspace,created_at,expires_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (_ba._hash_presented(identity), ISSUER, subject, email,
                 bool(verified), "tok123", active, time.time(), time.time() + 3600))
            control.commit()
        finally:
            control.close()
        headers = {"cookie": "ew_session=" + identity, "x-csrf-token": "tok123",
                   "origin": "http://127.0.0.1:8771"}
        self.sessions[identity] = headers
        return headers

    def call(self, path, *, headers, method="GET", query="", body=None):
        messages = []
        raw = json.dumps(body or {}).encode()
        scope = {"type": "http", "method": method, "path": path, "scheme": "http",
                 "headers": [(b"host", b"127.0.0.1:8771"),
                             (b"content-length", str(len(raw)).encode())] +
                            [(k.encode(), v.encode()) for k, v in headers.items()],
                 "query_string": query.encode()}

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}

        async def send(message):
            messages.append(message)

        asyncio.run(self.app(scope, receive, send))
        status = messages[0]["status"]
        payload = b"".join(m.get("body", b"") for m in messages)
        return status, json.loads(payload or b"{}")

    def bare_workspace(self, name="bare"):
        """A registered workspace that never got the collaboration tables."""
        folder = os.path.join(self.http.data_dir, name)
        path = str(backend_fixtures.make_workspace(folder, schema_version=8))
        con = sqlite3.connect(path, isolation_level=None)
        try:
            con.execute("DROP TABLE IF EXISTS invites")  # aux schema unprepared
            self.assertFalse(_collab.has_aux_schema(con))
        finally:
            con.close()
        _conn.register_workspace(self.app.store, name, path, self.http.data_dir)

    def shared(self, route, share, workspace, headers):
        extra = "&episode_id=e1" if route == "transcript" else ""
        path = "/api/v1/shared/" + route
        return self.call(path, headers=headers,
                         query=f"workspace_id={workspace}&share_id={share}{extra}")

    def accept(self, invite, workspace, headers):
        return self.call(f"/api/v1/workspaces/{workspace}/invites/{invite}/accept",
                         headers=headers, method="POST")


class GuestShareRefusalsTest(_Base):
    ROUTES = ("search", "transcript", "evidence/e1")

    def test_stranger_cannot_tell_real_share_from_fabricated(self):
        bob = self.session("bob")
        for route in self.ROUTES:
            seen = {
                "real share, real workspace": self.shared(route, self.share, "acme", bob),
                "fabricated share, real workspace": self.shared(route, FAKE_SHARE, "acme", bob),
                "real share, fabricated workspace": self.shared(route, self.share, "nope", bob),
                "fabricated both": self.shared(route, FAKE_SHARE, "nope", bob),
                "revoked share": self.shared(route, self.revoked, "acme", bob),
                "expired share": self.shared(route, self.expired, "acme", bob),
            }
            for label, answer in seen.items():
                with self.subTest(route=route, case=label):
                    self.assertEqual(answer, CARDS["unknown_object"])

    def test_member_of_the_workspace_is_a_stranger_to_a_named_share(self):
        # A viewer of the workspace is not the share's audience either.
        con = self._db()
        try:
            member = f"oidc:{ISSUER}:viewer1"
            _collab.accept_invite(con, _collab.create_invite(
                con, "local", member, "viewer", OWNER)["invite_id"], member)
        finally:
            con.close()
        viewer = self.session("viewer1", active="acme")
        for route in self.ROUTES:
            with self.subTest(route=route):
                self.assertEqual(self.shared(route, self.share, "acme", viewer),
                                 CARDS["unknown_object"])
                self.assertEqual(self.shared(route, FAKE_SHARE, "acme", viewer),
                                 CARDS["unknown_object"])

    def test_named_audience_keeps_its_specific_messages(self):
        carol = self.session("carol")
        status, body = self.shared("search", self.share, "acme", carol)
        self.assertEqual(status, 200, body)
        status, body = self.shared("search", self.revoked, "acme", carol)
        self.assertEqual((status, body["error"]["message"]), (403, "share is revoked"))
        status, body = self.shared("search", self.expired, "acme", carol)
        self.assertEqual((status, body["error"]["message"]), (403, "share is expired"))

    def test_named_audience_gets_the_unknown_answer_for_a_fabricated_share(self):
        carol = self.session("carol")
        self.assertEqual(self.shared("search", FAKE_SHARE, "acme", carol),
                         CARDS["unknown_object"])
        self.assertEqual(self.shared("search", self.share, "nope", carol),
                         CARDS["unknown_object"])

    def test_verified_email_audience_is_entitled(self):
        con = self._db()
        try:
            by_email = _collab.create_share(
                con, "local", "episode", "e1", "frank@example.test",
                ["ep_title"], OWNER)["share_id"]
        finally:
            con.close()
        verified = self.session("frank", email="frank@example.test", verified=True)
        status, body = self.shared("search", by_email, "acme", verified)
        self.assertEqual(status, 200, body)
        unverified = self.session("gus", email="frank@example.test", verified=False)
        self.assertEqual(self.shared("search", by_email, "acme", unverified),
                         CARDS["unknown_object"])


class InviteRefusalsTest(_Base):
    def test_stranger_cannot_tell_real_invite_from_fabricated(self):
        bob = self.session("bob")
        seen = {
            "pending invite for someone else": self.accept(self.invite, "acme", bob),
            "consumed invite": self.accept(self.consumed, "acme", bob),
            "fabricated invite, real workspace": self.accept(FAKE_INVITE, "acme", bob),
            "real invite, fabricated workspace": self.accept(self.invite, "nope", bob),
            "fabricated both": self.accept(FAKE_INVITE, "nope", bob),
            "email invite, no verified email": self.accept(self.by_email, "acme", bob),
        }
        for label, answer in seen.items():
            with self.subTest(case=label):
                self.assertEqual(answer, CARDS["unknown_object"])

    def test_wrong_verified_email_is_a_stranger(self):
        other = self.session("hank", email="hank@example.test", verified=True)
        self.assertEqual(self.accept(self.by_email, "acme", other),
                         CARDS["unknown_object"])

    def test_a_refused_stranger_changes_nothing(self):
        bob = self.session("bob")
        self.accept(self.invite, "acme", bob)
        con = self._db()
        try:
            status = con.execute("SELECT status, member FROM invites WHERE id=?",
                                 (self.invite,)).fetchone()
            members = [r[0] for r in con.execute("SELECT member FROM memberships")]
        finally:
            con.close()
        self.assertEqual((status[0], status[1]), ("created", self.carol))
        self.assertNotIn(f"oidc:{ISSUER}:bob", members)

    def test_invitee_keeps_its_specific_message_and_can_accept(self):
        carol = self.session("carol")
        status, body = self.accept(self.invite, "acme", carol)
        self.assertEqual(status, 200, body)
        status, body = self.accept(self.invite, "acme", carol)
        self.assertEqual((status, body["error"]["message"]),
                         (400, "invite is no longer pending"))

    def test_invitee_by_verified_email_can_accept(self):
        erin = self.session("erin", email="erin@example.test", verified=True)
        status, body = self.accept(self.by_email, "acme", erin)
        self.assertEqual(status, 200, body)
        status, body = self.accept(self.by_email, "acme", erin)
        # The invitation is bound to erin's account, so it is still hers.
        self.assertEqual((status, body["error"]["message"]),
                         (400, "invite is no longer pending"))

    def test_invitee_gets_the_unknown_answer_for_a_fabricated_invite(self):
        carol = self.session("carol")
        self.assertEqual(self.accept(FAKE_INVITE, "acme", carol),
                         CARDS["unknown_object"])
        self.assertEqual(self.accept(self.invite, "nope", carol),
                         CARDS["unknown_object"])
        # Someone else's consumed invite is not the invitee's to learn about.
        self.assertEqual(self.accept(self.consumed, "acme", carol),
                         CARDS["unknown_object"])


class InviteDeliverRefusalsTest(_Base):
    def deliver(self, invite, workspace, headers):
        return self.call(f"/api/v1/workspaces/{workspace}/invites/{invite}/deliver",
                         headers=headers, method="POST")

    def test_outsider_and_member_cannot_tell_real_from_fabricated(self):
        con = self._db()
        try:
            member = f"oidc:{ISSUER}:viewer2"
            _collab.accept_invite(con, _collab.create_invite(
                con, "local", member, "viewer", OWNER)["invite_id"], member)
        finally:
            con.close()
        for who, session in (("outsider", self.session("bob", active="")),
                             ("viewer", self.session("viewer2", active="acme"))):
            answers = {
                "real pending": self.deliver(self.invite, "acme", session),
                "consumed": self.deliver(self.consumed, "acme", session),
                "fabricated": self.deliver(FAKE_INVITE, "acme", session),
                "fabricated workspace": self.deliver(self.invite, "nope", session),
            }
            first = next(iter(answers.values()))
            for label, answer in answers.items():
                with self.subTest(who=who, case=label):
                    self.assertEqual(answer, first)

    def test_owner_keeps_its_specific_messages(self):
        status, body = self.http._call(
            f"/api/v1/workspaces/acme/invites/{self.consumed}/deliver",
            headers=self.http._csrf())
        self.assertEqual((status, body["error"]["message"]),
                         (400, "invite is no longer pending"))
        status, body = self.http._call(
            f"/api/v1/workspaces/acme/invites/{FAKE_INVITE}/deliver",
            headers=self.http._csrf())
        self.assertEqual((status, body["error"]["message"]), (400, "unknown invite"))


class CsrfBeforeLookupTest(_Base):
    """CSRF and Origin are checked before any workspace is resolved, so a
    bad token cannot tell a real workspace from a made-up one."""

    def variants(self, headers):
        no_token = {k: v for k, v in headers.items() if k != "x-csrf-token"}
        return {"bad token": {**headers, "x-csrf-token": "wrong"},
                "no token": no_token,
                "bad origin": {**headers, "origin": "http://evil.example"}}

    def test_invite_accept_answers_alike_for_real_and_made_up_workspaces(self):
        for label, headers in self.variants(self.session("bob")).items():
            real = self.accept(self.invite, "acme", headers)
            made_up = self.accept(self.invite, "nope", headers)
            with self.subTest(case=label):
                self.assertEqual(real, made_up)
                self.assertEqual(real[0], 403)
                self.assertEqual(real[1]["error"]["message"],
                                 "valid CSRF token is required")

    def test_ordinary_member_post_answers_alike_for_real_and_made_up_workspaces(self):
        real_session = self.session("bob", active="acme")
        fake_session = self.session("bob", active="nope", identity="uniform-bob2")
        for label in ("bad token", "no token", "bad origin"):
            answers = []
            for headers in (real_session, fake_session):
                bad = self.variants(headers)[label]
                answers.append(self.call("/api/v1/customer/saved-items",
                                         headers=bad, method="POST",
                                         body={"type": "query"}))
            with self.subTest(case=label):
                self.assertEqual(answers[0], answers[1])
                self.assertEqual(answers[0][0], 403)

    def test_a_valid_token_still_works(self):
        status, body = self.accept(self.invite, "acme", self.session("carol"))
        self.assertEqual(status, 200, body)


class RateLimitBucketTest(_Base):
    """A caller-supplied workspace id must not spend that workspace's budget."""

    def setUp(self):
        super().setUp()
        # Small workspace budget so a few requests exhaust it.
        self.app.limiter = _asgi.RateLimiter(
            workspace_per_minute=6, session_burst=40)

    def owner_read(self):
        return self.call("/api/v1/customer/preferences", headers={
            "cookie": "ew_session=sess-k02-1", "x-csrf-token": "tok123",
            "origin": "http://127.0.0.1:8771"})[0]

    def test_guest_requests_for_a_victim_workspace_leave_its_budget_alone(self):
        bob = self.session("bob")
        for _ in range(12):
            self.shared("search", FAKE_SHARE, "acme", bob)
        self.assertEqual(self.owner_read(), 200)

    def test_invite_accepts_for_a_victim_workspace_leave_its_budget_alone(self):
        bob = self.session("bob")
        for _ in range(12):
            self.accept(FAKE_INVITE, "acme", bob)
        self.assertEqual(self.owner_read(), 200)

    def test_fabricated_workspace_ids_do_not_grow_workspace_buckets(self):
        bob = self.session("bob")
        for n in range(8):
            self.shared("search", FAKE_SHARE, f"made-up-{n}", bob)
            self.accept(FAKE_INVITE, f"made-up-{n}", bob)
        self.assertEqual([k for k in self.app.limiter._buckets
                          if k.startswith("ws:made-up")], [])

    def test_the_caller_is_still_limited_on_these_paths(self):
        bob = self.session("bob")
        statuses = [self.shared("search", FAKE_SHARE, "acme", bob)[0]
                    for _ in range(12)]
        self.assertIn(429, statuses)
        self.assertEqual(statuses[0], 404)

    def test_the_owner_is_still_limited_on_the_workspace_budget(self):
        owner = {"cookie": "ew_session=sess-k02-1", "x-csrf-token": "tok123",
                 "origin": "http://127.0.0.1:8771"}
        statuses = [self.call("/api/v1/customer/preferences", headers=owner)[0]
                    for _ in range(12)]
        self.assertEqual(statuses[0], 200)
        self.assertIn(429, statuses)

    def test_a_named_audience_is_capped_by_the_workspace_budget(self):
        statuses = []
        for n in range(30):
            carol = self.session("carol", identity=f"uniform-carol-{n % 5}")
            statuses.append(self.shared("search", self.share, "acme", carol)[0])
        self.assertIn(429, statuses)
        self.assertLessEqual(statuses.count(200), 7)

    def test_a_named_audience_takes_and_frees_a_workspace_concurrency_slot(self):
        seen = []
        original = _services.shared_search

        def spy(*args, **kwargs):
            seen.append(self.app.limiter._workspaces.get("acme", 0))
            return original(*args, **kwargs)

        carol = self.session("carol")
        with mock.patch.object(_services, "shared_search", spy):
            status, body = self.shared("search", self.share, "acme", carol)
        self.assertEqual(status, 200, body)
        self.assertEqual(seen, [1])
        self.assertEqual(self.app.limiter._workspaces.get("acme", 0), 0)
        self.assertEqual(self.app.limiter._grants.get(
            next(k for k in self.app.limiter._grants), 0), 0)

    def test_strangers_never_touch_the_workspace_bucket_or_gate(self):
        bob = self.session("bob")
        self.shared("search", self.share, "acme", bob)
        self.accept(self.invite, "acme", bob)
        self.assertNotIn("ws:acme", self.app.limiter._buckets)
        self.assertEqual(self.app.limiter._workspaces.get("acme", 0), 0)

    def test_a_former_member_does_not_spend_the_workspace_budget(self):
        con = self._db()
        try:
            member = f"oidc:{ISSUER}:former"
            _collab.accept_invite(con, _collab.create_invite(
                con, "local", member, "viewer", OWNER)["invite_id"], member)
            con.execute("UPDATE memberships SET revoked=1 WHERE member=?", (member,))
        finally:
            con.close()
        former = self.session("former", active="acme")
        statuses = [self.call("/api/v1/customer/preferences", headers=former)[0]
                    for _ in range(12)]
        # Refused as a non-member, then limited as a caller (its own bucket).
        self.assertEqual(statuses[:5], [403] * 5)
        self.assertNotIn("ws:acme", self.app.limiter._buckets)
        self.assertEqual(self.owner_read(), 200)

    def test_a_member_is_charged_to_the_workspace_after_the_role_is_known(self):
        statuses = [self.owner_read() for _ in range(12)]
        self.assertEqual(statuses[0], 200)
        self.assertIn(429, statuses)
        self.assertEqual(self.app.limiter._workspaces.get("acme", 0), 0)


class MissingCollaborationTablesTest(_Base):
    """A workspace without the collaboration tables answers as unknown and
    frees the session's slots; it never 500s or leaks a concurrency slot."""

    def test_guest_and_invite_paths_answer_as_unknown_and_keep_working(self):
        self.bare_workspace()
        bob = self.session("bob")
        for _ in range(5):
            for answer in (self.shared("search", self.share, "bare", bob),
                           self.accept(self.invite, "bare", bob)):
                self.assertEqual(answer, CARDS["unknown_object"])
        self.assertEqual(sum(self.app.limiter._grants.values()), 0)
        self.assertEqual(sum(self.app.limiter._workspaces.values()), 0)

    def test_the_answer_matches_a_made_up_workspace(self):
        self.bare_workspace()
        bob = self.session("bob")
        self.assertEqual(self.shared("search", self.share, "bare", bob),
                         self.shared("search", self.share, "nope", bob))


class ConnectionsCloseOnEveryExitTest(_Base):
    """Every exit from the workspace wrapper closes the database it opened
    and frees the limiter slots, including a schema error after the open."""

    def drop_memberships(self):
        con = self._db()
        try:
            con.execute("DROP TABLE memberships")
        finally:
            con.close()

    def record_opens(self, name):
        opened = []
        target = _conn if name == "open_registered_db" else _services
        original = getattr(target, name)

        def wrapper(*args, **kwargs):
            result = original(*args, **kwargs)
            opened.append(result[0])
            return result

        patcher = mock.patch.object(target, name, wrapper)
        patcher.start()
        self.addCleanup(patcher.stop)
        return opened

    def assertClosed(self, opened):
        self.assertTrue(opened)
        for con in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                con.execute("SELECT 1")

    def assertNothingHeld(self):
        self.assertEqual(sum(self.app.limiter._grants.values()), 0)
        self.assertEqual(sum(self.app.limiter._workspaces.values()), 0)

    def test_invite_accept_closes_the_database_after_a_schema_error(self):
        self.drop_memberships()
        opened = self.record_opens("open_registered_db")
        bob = self.session("bob")
        gc.collect()  # earlier tests' garbage must not count here
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            answer = self.accept(self.invite, "acme", bob)
            gc.collect()
        self.assertEqual(answer, CARDS["unknown_object"])
        self.assertClosed(opened)
        self.assertEqual([str(w.message) for w in caught
                          if "database" in str(w.message)], [])
        self.assertNothingHeld()

    def test_member_path_closes_the_database_after_a_schema_error(self):
        # The role lookup fails on the connection the wrapper just opened.
        opened = self.record_opens("open_customer_workspace")
        original = _collab.get_role

        def failing(con, *args, **kwargs):
            if opened and con is opened[-1]:
                raise sqlite3.OperationalError("no such table: memberships")
            return original(con, *args, **kwargs)

        owner = {"cookie": "ew_session=sess-k02-1", "x-csrf-token": "tok123",
                 "origin": "http://127.0.0.1:8771"}
        gc.collect()  # earlier tests' garbage must not count here
        with mock.patch.object(_collab, "get_role", failing), \
                warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            status, _ = self.call("/api/v1/customer/preferences", headers=owner)
            gc.collect()
        self.assertEqual(status, 500)
        self.assertClosed(opened)
        self.assertEqual([str(w.message) for w in caught
                          if "database" in str(w.message)], [])
        self.assertNothingHeld()

    def test_an_authorization_error_after_the_open_closes_the_database(self):
        opened = self.record_opens("open_customer_workspace")
        original = _collab.get_role

        def refusing(con, *args, **kwargs):
            if opened and con is opened[-1]:
                raise _access.AuthorizationError("refused after open", 403)
            return original(con, *args, **kwargs)

        owner = {"cookie": "ew_session=sess-k02-1", "x-csrf-token": "tok123",
                 "origin": "http://127.0.0.1:8771"}
        with mock.patch.object(_collab, "get_role", refusing):
            status, _ = self.call("/api/v1/customer/preferences", headers=owner)
        self.assertEqual(status, 403)
        self.assertClosed(opened)
        self.assertNothingHeld()

    def test_a_failure_after_authorization_closes_and_frees(self):
        opened = self.record_opens("open_customer_workspace")
        owner = {"cookie": "ew_session=sess-k02-1", "x-csrf-token": "tok123",
                 "origin": "http://127.0.0.1:8771"}
        with mock.patch.object(_services, "registry_policy",
                               side_effect=RuntimeError("boom")):
            status, _ = self.call("/api/v1/customer/preferences", headers=owner)
        self.assertEqual(status, 500)
        self.assertClosed(opened)
        self.assertNothingHeld()
        # The next request is not starved by a leaked slot.
        self.assertEqual(self.call("/api/v1/customer/preferences",
                                   headers=owner)[0], 200)


if __name__ == "__main__":
    unittest.main()


class CsrfReasonTest(_Base):
    """K6 (RA-R3-02): every refused CSRF token carries the stable marker
    error.reason "csrf_invalid", so the customer app never matches the
    English message. Status, code and message stay as they were, and the
    answer stays the same for a real and a made-up workspace."""

    WANT = {"code": "FORBIDDEN", "message": "valid CSRF token is required",
            "status": 403, "details": {}, "reason": "csrf_invalid"}

    def bad(self, subject, **kw):
        return {**self.session(subject, **kw), "x-csrf-token": "wrong"}

    def test_every_session_route_marks_the_csrf_refusal(self):
        cases = {
            "customer write": ("/api/v1/customer/campaigns", {"campaign": {"name": "x"}}),
            "logout": ("/api/v1/session/logout", {}),
            "workspace switch": ("/api/v1/session/workspace", {"workspace_id": "acme"}),
            "workspace create": ("/api/v1/workspaces", {"workspace_id": "new1", "name": "N"}),
            "password change": ("/api/v1/session/password/change", {}),
        }
        for label, (path, body) in cases.items():
            status, out = self.call(path, headers=self.bad("bob", active="acme"),
                                    method="POST", body=body)
            with self.subTest(case=label):
                if label == "password change" and status == 503:
                    continue  # password sign-in not configured in this fixture
                self.assertEqual(status, 403, out)
                self.assertEqual(out["error"], self.WANT)

    def test_the_raised_csrf_refusal_maps_to_the_marker(self):
        status, body = _body_of(_asgi._csrf_error())
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], self.WANT)

    def test_the_marker_is_alike_for_real_and_made_up_workspaces(self):
        real = self.call("/api/v1/customer/saved-items", headers=self.bad("bob", active="acme"),
                         method="POST", body={"type": "query"})
        fake = self.call("/api/v1/customer/saved-items",
                         headers=self.bad("bob", active="nope", identity="uniform-bob2"),
                         method="POST", body={"type": "query"})
        self.assertEqual(real, fake)
        self.assertEqual(real[1]["error"]["reason"], "csrf_invalid")

    def test_no_csrf_refusal_bypasses_the_marker(self):
        # The password routes answer 503 in this fixture; every CSRF refusal in
        # asgi.py goes through _csrf_refused / _csrf_error, which hold the only
        # copy of the message.
        import inspect
        src = inspect.getsource(_asgi)
        self.assertEqual(src.count('"valid CSRF token is required"'), 1)
        self.assertEqual(src.count("_csrf_refused()") + src.count("_csrf_error()"), 8)

    def test_other_403s_carry_no_csrf_marker(self):
        status, body = _body_of(_access.AuthorizationError("reader credentials are read-only", 403))
        self.assertEqual(status, 403)
        self.assertNotIn("reason", body["error"])


def _body_of(exc):
    app = _asgi.HostedApp.__new__(_asgi.HostedApp)
    status, raw, _headers = app._service_error(exc)
    return status, json.loads(raw)
