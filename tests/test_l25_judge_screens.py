"""L25: the server half of the judges' first screens (red-capable).

1. Invite preflight. The invite landing page needs an entitlement decision
   before it shows any invitation detail or the Accept button. The accept
   POST is a mutation, so it cannot be that check. GET
   /api/v1/workspaces/{ws}/invites/{id}/preflight is a read-only check
   for the invitee: everyone else gets the uniform 404 "unknown object"
   (docs/hosting/uniform-refusals.md), byte for byte the same as accept.
2. The sign-in callback answers a browser navigation with a redirect to a
   plain page, not raw JSON: the admission refusal goes to
   /app/#/access-unavailable, any other failed callback to
   /app/#/signin-failed. API callers keep the JSON answers.
3. The owner's pending invitation list says when a send is unresolved, so
   the panel never infers it from elapsed time.
"""
from __future__ import annotations

import json
import os
import sqlite3
import unittest
import urllib.parse

from earwitness.app import browser_auth as _auth
from earwitness.app import collaboration as _collab
from tests.test_invite_supersede import (CAROL_EMAIL as _CAROL_EMAIL, ISSUER as _ISSUER,
                                         _Supersede as _SupersedeBase)
from tests.test_signin_bounds import _Base as _SigninBase
from tests.test_uniform_refusals import (CARDS, FAKE_INVITE, OWNER,
                                         _Base as _RefusalBase)

UNKNOWN = CARDS["unknown_object"]


class _PreflightBase(_RefusalBase):
    def preflight(self, invite, workspace, headers):
        return self.call(
            f"/api/v1/workspaces/{workspace}/invites/{invite}/preflight",
            headers=headers)

    def snapshot(self):
        con = self._db()
        try:
            return {
                "invites": [tuple(r) for r in con.execute(
                    "SELECT id, member, status FROM invites ORDER BY id")],
                "members": [tuple(r) for r in con.execute(
                    "SELECT member, role, revoked FROM memberships ORDER BY member")],
                "audit": con.execute("SELECT COUNT(*) FROM audit").fetchone()[0],
            }
        finally:
            con.close()


class InvitePreflightRefusalTest(_PreflightBase):
    def test_stranger_gets_the_uniform_answer_for_every_case(self):
        bob = self.session("bob")
        cases = {
            "pending invite for someone else": (self.invite, "acme"),
            "consumed invite": (self.consumed, "acme"),
            "fabricated invite, real workspace": (FAKE_INVITE, "acme"),
            "real invite, fabricated workspace": (self.invite, "nope"),
            "fabricated both": (FAKE_INVITE, "nope"),
            "email invite, no verified email": (self.by_email, "acme"),
        }
        for label, (invite, workspace) in cases.items():
            with self.subTest(case=label):
                self.assertEqual(self.preflight(invite, workspace, bob), UNKNOWN)

    def test_wrong_verified_email_is_a_stranger(self):
        other = self.session("hank", email="hank@example.test", verified=True)
        self.assertEqual(self.preflight(self.by_email, "acme", other), UNKNOWN)

    def test_invitee_gets_the_unknown_answer_for_objects_that_are_not_hers(self):
        carol = self.session("carol")
        self.assertEqual(self.preflight(FAKE_INVITE, "acme", carol), UNKNOWN)
        self.assertEqual(self.preflight(self.invite, "nope", carol), UNKNOWN)
        self.assertEqual(self.preflight(self.consumed, "acme", carol), UNKNOWN)

    def test_workspace_without_collaboration_tables_answers_as_unknown(self):
        self.bare_workspace()
        bob = self.session("bob")
        self.assertEqual(self.preflight(self.invite, "bare", bob), UNKNOWN)
        self.assertEqual(sum(self.app.limiter._grants.values()), 0)
        self.assertEqual(sum(self.app.limiter._workspaces.values()), 0)

    def test_strangers_never_touch_the_workspace_bucket(self):
        bob = self.session("bob")
        for _ in range(3):
            self.preflight(self.invite, "acme", bob)
            self.preflight(FAKE_INVITE, "made-up", bob)
        self.assertNotIn("ws:acme", self.app.limiter._buckets)
        self.assertEqual([k for k in self.app.limiter._buckets
                          if k.startswith("ws:made-up")], [])

    def test_signed_out_caller_gets_the_sign_in_answer(self):
        status, body = self.preflight(self.invite, "acme", {})
        self.assertEqual(status, 401, body)
        self.assertNotIn("invite", json.dumps(body).lower())


class InvitePreflightInviteeTest(_PreflightBase):
    def test_pending_invitee_learns_the_workspace_and_role(self):
        carol = self.session("carol")
        status, body = self.preflight(self.invite, "acme", carol)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["invitation"], {
            "state": "pending", "role": "viewer", "workspace_name": "Acme"})

    def test_invitee_by_verified_email_is_entitled(self):
        erin = self.session("erin", email="erin@example.test", verified=True)
        status, body = self.preflight(self.by_email, "acme", erin)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["invitation"]["state"], "pending")

    def test_accepted_invite_is_no_longer_pending_for_its_invitee(self):
        carol = self.session("carol")
        self.assertEqual(self.accept(self.invite, "acme", carol)[0], 200)
        status, body = self.preflight(self.invite, "acme", carol)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["invitation"], {"state": "not_pending"})

    def test_withdrawn_invite_is_no_longer_pending(self):
        con = self._db()
        try:
            _collab.withdraw_invite(con, "local", self.invite, OWNER)
        finally:
            con.close()
        status, body = self.preflight(self.invite, "acme", self.session("carol"))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["invitation"], {"state": "not_pending"})

    def test_expired_invite_says_expired(self):
        con = self._db()
        try:
            con.execute("UPDATE invites SET created_at='2000-01-01T00:00:00+00:00'"
                        " WHERE id=?", (self.invite,))
        finally:
            con.close()
        status, body = self.preflight(self.invite, "acme", self.session("carol"))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["invitation"], {"state": "expired"})

    def test_preflight_changes_nothing(self):
        before = self.snapshot()
        for who in ("carol", "bob"):
            self.preflight(self.invite, "acme", self.session(who))
        self.preflight(self.by_email, "acme", self.session(
            "erin", email="erin@example.test", verified=True))
        self.assertEqual(self.snapshot(), before)

    def test_preflight_is_charged_to_the_caller_only(self):
        self.preflight(self.invite, "acme", self.session("carol"))
        self.assertNotIn("ws:acme", self.app.limiter._buckets)
        self.assertEqual(self.app.limiter._workspaces.get("acme", 0), 0)

    def test_accept_still_works_after_a_preflight(self):
        carol = self.session("carol")
        self.assertEqual(self.preflight(self.invite, "acme", carol)[0], 200)
        status, body = self.accept(self.invite, "acme", carol)
        self.assertEqual(status, 200, body)


class PreflightCurrentVerifiedEmailTest(_SupersedeBase):
    """L25 review HIGH: preflight must refuse exactly as accept does in the
    L22b R2-01 case. Account A (carol) joined with verified E; the owner
    invited account B directly; A is removed or downgraded; B's session
    also verifies E. Accept refuses the older invitation, so preflight must
    not answer pending with the workspace name and role."""

    ALT = f"oidc:{_ISSUER}:carol-alt"

    def setUp(self):
        super().setUp()
        carol = self.session("carol", email=_CAROL_EMAIL, verified=True)
        self.assertEqual(self.accept(self.invite, "acme", carol)[0], 200)
        self.older = self.make_invite(self.ALT, "owner")
        self.alt = self.session("carol-alt", email=_CAROL_EMAIL, verified=True,
                                identity="alt-verified")

    def preflight(self, invite, headers):
        return self.call(f"/api/v1/workspaces/acme/invites/{invite}/preflight",
                         headers=headers)

    def test_after_removal_preflight_is_not_pending(self):
        self.remove(self.carol)
        status, body = self.preflight(self.older, self.alt)
        self.assertEqual((status, body.get("invitation")), (200, {"state": "not_pending"}))
        self.assertEqual(self.accept(self.older, "acme", self.alt)[0], 400)

    def test_after_downgrade_preflight_is_not_pending(self):
        self.set_role(self.carol, "editor")
        status, body = self.preflight(self.older, self.alt)
        self.assertEqual((status, body.get("invitation")), (200, {"state": "not_pending"}))

    def test_unrelated_verified_email_still_sees_pending(self):
        self.remove(self.carol)
        other = self.session("carol-alt", email="dana@example.test", verified=True,
                             identity="alt-dana")
        status, body = self.preflight(self.older, other)
        self.assertEqual((status, body["invitation"]["state"]), (200, "pending"))


class PendingListUnresolvedTest(_PreflightBase):
    def owner(self):
        return self.http._call("/api/v1/workspaces/acme/invites", method="GET",
                               headers=self.http._csrf())

    def add_message(self, state, detail):
        con = self._db()
        try:
            con.execute("INSERT INTO notification_outbox (workspace_id, event_id,"
                        " recipient, state, detail, created_at)"
                        " VALUES ('local', NULL, ?, ?, ?, '2026-10-20T00:00:00+00:00')",
                        (self.carol, state, json.dumps({
                            "kind": "workspace_invite", "invite_id": self.invite,
                            **detail})))
        finally:
            con.close()

    def row(self):
        status, body = self.owner()
        self.assertEqual(status, 200, body)
        return next(r for r in body["invites"] if r["id"] == self.invite)

    def test_unsent_invite_is_not_unresolved(self):
        self.assertEqual(self.row()["delivery"], "not_queued")
        self.assertIs(self.row()["unresolved"], False)

    def test_uncertain_send_is_not_unresolved_until_marked(self):
        self.add_message("uncertain", {"status_reason": "provider timed out"})
        self.assertEqual((self.row()["delivery"], self.row()["unresolved"]),
                         ("uncertain", False))

    def test_marked_send_is_unresolved(self):
        self.add_message("uncertain", {"unresolved": True,
                                       "status_reason": "past the replay window"})
        self.assertEqual((self.row()["delivery"], self.row()["unresolved"]),
                         ("uncertain", True))


async def _nav(app, path, *, cookies=None, query="", navigate=True):
    """A top-level browser navigation (Accept: text/html, Sec-Fetch-Mode:
    navigate), or a plain API call when navigate is False."""
    headers = [(b"host", b"127.0.0.1:8771")]
    if navigate:
        headers += [(b"accept", b"text/html,application/xhtml+xml,*/*;q=0.8"),
                    (b"sec-fetch-mode", b"navigate")]
    if cookies:
        headers.append((b"cookie", "; ".join(
            f"{k}={v}" for k, v in cookies.items()).encode()))
    scope = {"type": "http", "method": "GET", "path": path, "scheme": "http",
             "headers": headers, "query_string": query.encode(),
             "client": ("198.51.100.7", 40000)}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    head = {}
    cookies_set = {}
    for name, value in start.get("headers", []):
        head.setdefault(name.decode(), value.decode())
        if name == b"set-cookie":
            key, _, val = value.decode().split(";", 1)[0].partition("=")
            cookies_set[key.strip()] = val.strip()
    body = b"".join(m.get("body", b"") for m in sent
                    if m["type"] == "http.response.body")
    return start["status"], head, body, cookies_set


class CallbackBrowserPageTest(_SigninBase):
    def admit_only_alice(self):
        path = self.work / "admission.json"
        path.write_text(json.dumps({
            "status": "approved",
            "principals": [f"oidc:{self.idp.iss}:alice"],
            "verified_emails": [], "max_accounts": 5, "max_workspaces": 5}))
        os.environ["EARWITNESS_ADMISSION_FILE"] = str(path)

    async def navigate_callback(self, subject, navigate=True):
        status, data, cookies = await self.login()
        self.assertEqual(status, 200, data)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(
            data["authorize_url"]).query)
        code = self.idp.issue_code(query["nonce"][0], subject)
        return await _nav(
            self.app, "/api/v1/session/callback",
            cookies={_auth.TX_COOKIE: cookies[_auth.TX_COOKIE]},
            query=urllib.parse.urlencode({"state": query["state"][0],
                                          "code": code}),
            navigate=navigate)

    async def test_refused_browser_lands_on_the_access_page(self):
        self.admit_only_alice()
        status, head, body, cookies = await self.navigate_callback("mallory")
        self.assertEqual(status, 302, body)
        self.assertEqual(head.get("location"), "/app/#/access-unavailable")
        self.assertNotIn(_auth.SESSION_COOKIE, cookies)
        self.assertNotIn(b"admitted", body)
        self.assertNotIn(b"mallory", body)

    async def test_api_caller_keeps_the_json_refusal(self):
        self.admit_only_alice()
        status, head, body, cookies = await self.navigate_callback(
            "mallory", navigate=False)
        self.assertEqual(status, 403, body)
        self.assertEqual(json.loads(body)["error"]["message"],
                         "this account is not admitted to the pilot")
        self.assertNotIn("location", head)

    async def test_other_callback_failures_land_on_the_sign_in_page(self):
        for query in ("", "error=access_denied",
                      urllib.parse.urlencode({"state": "made-up", "code": "c"})):
            with self.subTest(query=query):
                status, head, body, cookies = await _nav(
                    self.app, "/api/v1/session/callback", query=query)
                self.assertEqual(status, 302, body)
                self.assertEqual(head.get("location"), "/app/#/signin-failed")
                self.assertNotIn(_auth.SESSION_COOKIE, cookies)

    async def test_admitted_browser_still_returns_to_its_page(self):
        self.admit_only_alice()
        status, head, body, cookies = await self.navigate_callback("alice")
        self.assertEqual(status, 302, body)
        self.assertEqual(head.get("location"), "/app/")
        self.assertTrue(cookies.get(_auth.SESSION_COOKIE))


if __name__ == "__main__":
    unittest.main()
