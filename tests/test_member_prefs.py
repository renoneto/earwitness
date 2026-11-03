"""PREFS-SHARED: each member's Settings display preferences are their own.

Two judges (editors A and B), the owner and a viewer share one workspace.
The five Settings display fields (display_name, time_zone, search_tab,
results_view, default_range) were stored once per workspace in
meta.settings, so A's typed name showed on B's Settings page and A's time
zone or default view changed everyone's (claims-audit Problem 2). Each
member's values now live in their own member_preferences_save
audit rows (no new table; design: launch-readiness
ws-app/PREFS-DESIGN.md). The shared values stay for the local operator;
the shared display name never shows to a hosted caller, and the other
four shared values stay the default for a member who has not set theirs.

Run: PYTHONPATH=. python -m unittest tests.test_member_prefs
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from earwitness.app import access, collaboration, customer, db, ops, search, services

from tests import test_backend_http as http
from tests import test_read_cache as rc

MEMBER = {"is_owner": False, "row_namespace": "local", "workspace_id": rc.WS}
A_PREFS = {"display_name": "Alice Judge", "time_zone": "Asia/Tokyo",
           "search_tab": "episodes", "results_view": "list", "default_range": "90d"}


class MemberPrefsBase(rc.ReadCacheBase):

    def setUp(self):
        super().setUp()
        collaboration.ensure_aux(self.con)
        self.con.executemany("INSERT OR REPLACE INTO memberships (workspace_id, member, role,"
                             " created_at) VALUES ('local', ?, ?, ?)",
                             [(c.principal_id, c.admitted_role, db.utcnow())
                              for c in (self.A, self.B, self.owner, self.viewer)])
        self.con.commit()
        self.others = (self.B, self.owner, self.viewer)

    def prefs(self, ctx):
        return services.customer_preferences(ctx, self.con, MEMBER)["preferences"]

    def put(self, ctx, body, revision=None):
        if revision is None:
            revision = self.prefs(ctx)["revision"]
        out = services.customer_save_preferences(ctx, self.con, MEMBER, dict(body), revision)
        self.con.commit()
        return out

    def everything(self, ctx) -> str:
        """Every read a member makes, as one text: the preferences and the
        cached aggregate reads (Overview with its activity, Search, saved
        items, Brands, brand, summary, Shows, Alerts)."""
        bodies = {"preferences": services.customer_preferences(ctx, self.con, MEMBER)}
        bodies.update(self.read(ctx))
        return json.dumps(bodies, sort_keys=True, default=str)


class OwnValuesTest(MemberPrefsBase):

    def test_a_name_and_zone_are_a_own(self):
        self.put(self.A, A_PREFS)
        mine = self.prefs(self.A)
        for key, value in A_PREFS.items():
            self.assertEqual(mine[key], value, key)
        for ctx in self.others:
            seen = self.prefs(ctx)
            self.assertNotIn("display_name", seen, ctx.principal_id)
            for key in ("time_zone", "search_tab", "results_view", "default_range"):
                self.assertNotIn(key, seen, (ctx.principal_id, key))

    def test_no_other_response_carries_a_values(self):
        before = {c.principal_id: self.everything(c) for c in self.others}
        self.put(self.A, A_PREFS)
        for ctx in self.others:
            after = self.everything(ctx)
            self.assertNotIn("Alice Judge", after, ctx.principal_id)
            self.assertNotIn("Asia/Tokyo", after, ctx.principal_id)
            self.assertNotIn("Preferences updated", after, ctx.principal_id)
            self.assertEqual(json.loads(after)["overview"]["activity"],
                             json.loads(before[ctx.principal_id])["overview"]["activity"])

    def test_many_own_saves_never_crowd_out_the_activity_feed(self):
        """Overview activity reads the newest 40 customer rows: a member's
        own saves (never listed) must not use them up for everyone."""
        before = {c.principal_id: self.fresh_read(c, ["overview"])["overview"]["activity"]
                  for c in (self.A, *self.others)}
        self.assertTrue(before[self.B.principal_id])
        for n in range(41):
            self.put(self.A, {"display_name": f"Alice {n}"})
        for ctx in (self.A, *self.others):
            self.assertEqual(self.fresh_read(ctx, ["overview"])["overview"]["activity"],
                             before[ctx.principal_id], ctx.principal_id)

    def test_b_sets_their_own_and_a_is_unchanged(self):
        self.put(self.A, A_PREFS)
        self.put(self.B, {"display_name": "Bea", "time_zone": "Europe/Paris",
                          "results_view": "cards"})
        a, b = self.prefs(self.A), self.prefs(self.B)
        self.assertEqual((a["display_name"], a["time_zone"], a["results_view"]),
                         ("Alice Judge", "Asia/Tokyo", "list"))
        self.assertEqual((b["display_name"], b["time_zone"], b["results_view"]),
                         ("Bea", "Europe/Paris", "cards"))
        self.assertNotIn("search_tab", b)

    def test_a_partial_save_keeps_a_earlier_values(self):
        self.put(self.A, A_PREFS)
        self.put(self.A, {"time_zone": ""})
        mine = self.prefs(self.A)
        self.assertEqual((mine["display_name"], mine["time_zone"], mine["default_range"]),
                         ("Alice Judge", "", "90d"))

    def test_the_put_answer_is_the_callers_own_view(self):
        self.put(self.B, {"display_name": "Bea"})
        out = self.put(self.A, A_PREFS)
        self.assertEqual(out["preferences"]["display_name"], "Alice Judge")
        self.assertNotIn("Bea", json.dumps(out))
        self.assertEqual(out["revision"], self.prefs(self.A)["revision"])

    def test_member_rows_hold_only_the_five_values_and_no_principal(self):
        self.put(self.A, dict(A_PREFS, workspace_name="Judges"))
        rows = self.con.execute("SELECT actor, target_type, target_id, detail FROM audit"
                                " WHERE action='member_preferences_save'").fetchall()
        self.assertEqual(len(rows), 1)
        actor, target_type, target_id, detail = rows[0]
        self.assertEqual((actor, target_type, target_id),
                         (self.A.principal_id, "settings", "member"))
        self.assertEqual(json.loads(detail), A_PREFS)
        self.assertNotIn(self.A.principal_id, detail)
        # The workspace name in the same request stays shared.
        self.assertEqual(customer.get_preferences(self.con)["workspace_name"], "Judges")
        for ctx in self.others:
            self.assertEqual(self.prefs(ctx)["workspace_name"], "Judges")

    def test_shared_values_are_not_written_by_a_member(self):
        before = ops.get_settings(self.con)
        self.put(self.A, A_PREFS)
        self.assertEqual(ops.get_settings(self.con), before)


class UpgradeTest(MemberPrefsBase):
    """Values stored in meta.settings before PREFS-SHARED."""

    def setUp(self):
        super().setUp()
        rev = customer.get_preferences(self.con)["revision"]
        customer.save_preferences(self.con, {"display_name": "Operator Name",
                                             "time_zone": "Europe/Lisbon",
                                             "results_view": "list"}, rev)
        self.con.commit()

    def test_the_shared_display_name_never_shows_to_a_hosted_member(self):
        for ctx in (self.A, *self.others):
            seen = self.prefs(ctx)
            self.assertNotIn("display_name", seen, ctx.principal_id)
            self.assertNotIn("Operator Name", self.everything(ctx), ctx.principal_id)

    def test_the_other_shared_values_stay_the_default(self):
        for ctx in (self.A, *self.others):
            seen = self.prefs(ctx)
            self.assertEqual((seen["time_zone"], seen["results_view"]),
                             ("Europe/Lisbon", "list"), ctx.principal_id)
        self.put(self.A, {"results_view": "cards"})
        self.assertEqual(self.prefs(self.A)["results_view"], "cards")
        self.assertEqual(self.prefs(self.A)["time_zone"], "Europe/Lisbon")
        self.assertEqual(self.prefs(self.B)["results_view"], "list")

    def test_the_local_operator_keeps_today(self):
        local = customer.get_preferences(self.con)
        self.assertEqual(local["display_name"], "Operator Name")
        self.put(self.A, A_PREFS)
        local = customer.get_preferences(self.con)
        self.assertEqual((local["display_name"], local["time_zone"]),
                         ("Operator Name", "Europe/Lisbon"))
        customer.save_preferences(self.con, {"display_name": "Op 2"}, local["revision"])
        self.con.commit()
        self.assertEqual(customer.get_preferences(self.con)["display_name"], "Op 2")
        self.assertEqual(self.prefs(self.A)["display_name"], "Alice Judge")
        self.assertNotIn("display_name", self.prefs(self.B))

    def test_a_hosted_context_with_no_member_sees_no_shared_name(self):
        for ctx in (None, SimpleNamespace(authority_kind="membership", principal_id=""),
                    SimpleNamespace(authority_kind="membership", principal_id="class:editor"),
                    SimpleNamespace(authority_kind="share", principal_id="g1")):
            self.assertIsNone(customer.pref_member(ctx))
        self.assertEqual(customer.pref_member(self.A), self.A.principal_id)
        self.assertNotIn("display_name",
                         customer.get_preferences(self.con, None, hosted=True))


    def test_the_services_give_a_no_member_context_no_shared_name(self):
        """PREFS-R1-01: services.customer_preferences reads hosted, and a
        save by a caller with no member (a class: or empty principal)
        answers the hosted re-read: neither gives the shared display name.
        No real caller reaches them today (class: contexts serve only the
        cached aggregate reads)."""
        owner_entry = dict(MEMBER, is_owner=True)
        for principal in ("class:" + "0" * 16, ""):
            ctx = access.AccessContext.create(
                principal_id=principal, principal_kind="editor", workspace_id=rc.WS,
                scopes={"customer:read", "customer:write"}, authority_kind="membership",
                credential_kind="browser_session")
            ctx.admitted_role = "editor"
            self.assertIsNone(customer.pref_member(ctx), principal)
            seen = services.customer_preferences(ctx, self.con, owner_entry)["preferences"]
            self.assertNotIn("display_name", seen, principal)
            out = services.customer_save_preferences(
                ctx, self.con, owner_entry, {"results_view": "cards"}, seen["revision"])
            self.con.commit()
            self.assertNotIn("display_name", out["preferences"], principal)
            self.assertNotIn("Operator Name", json.dumps(out, default=str), principal)

class RevisionTest(MemberPrefsBase):

    def test_another_member_save_never_conflicts(self):
        a_rev = self.prefs(self.A)["revision"]
        self.put(self.B, {"display_name": "Bea"})
        self.put(self.A, A_PREFS, a_rev)
        self.assertEqual(self.prefs(self.A)["display_name"], "Alice Judge")

    def test_a_stale_own_form_conflicts(self):
        rev = self.prefs(self.A)["revision"]
        self.put(self.A, {"display_name": "Alice"}, rev)
        with self.assertRaises(ops.Conflict):
            self.put(self.A, {"display_name": "Other tab"}, rev)
        self.assertEqual(self.prefs(self.A)["display_name"], "Alice")

    def test_a_workspace_name_save_moves_everyone_and_keeps_both_forms_working(self):
        self.put(self.A, {"display_name": "Alice"})
        a_rev = self.prefs(self.A)["revision"]
        self.put(self.owner, {"workspace_name": "Judges"})
        with self.assertRaises(ops.Conflict):
            self.put(self.A, {"display_name": "Stale"}, a_rev)
        out = self.put(self.A, {"display_name": "Alice 2"})
        self.put(self.A, {"workspace_name": "Judges 2"}, out["revision"])
        self.assertEqual(self.prefs(self.B)["workspace_name"], "Judges 2")

    def test_a_member_save_does_not_bump_the_aggregate_revision(self):
        before = db.aggregate_revision(self.con)
        self.put(self.A, A_PREFS)
        self.assertEqual(db.aggregate_revision(self.con), before)


class WhoMayWriteTest(MemberPrefsBase):

    def test_a_viewer_cannot_write_and_nothing_is_stored(self):
        self.put(self.A, A_PREFS)
        with self.assertRaises(access.AuthorizationError):
            self.put(self.viewer, {"display_name": "Viewer"})
        self.assertEqual(self.prefs(self.A)["display_name"], "Alice Judge")
        self.assertNotIn("display_name", self.prefs(self.viewer))
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM audit WHERE action='member_preferences_save'"
            " AND actor=?", (self.viewer.principal_id,)).fetchone()[0], 0)

    def test_no_request_names_another_member(self):
        for body in ({"member": self.B.principal_id, "display_name": "X"},
                     {"principal": self.B.principal_id, "display_name": "X"}):
            with self.assertRaises(search.FilterRejected):
                self.put(self.A, body)
        self.assertNotIn("display_name", self.prefs(self.B))

    def test_removed_then_re_added_member(self):
        self.put(self.A, A_PREFS)
        collaboration.revoke_member(self.con, "local", self.A.principal_id,
                                    self.owner.principal_id)
        self.con.commit()
        with self.assertRaises(access.AuthorizationError):
            self.put(self.A, {"display_name": "While removed"})
        self.assertNotIn("display_name", self.prefs(self.B))
        self.con.execute("UPDATE memberships SET revoked=0 WHERE member=?",
                         (self.A.principal_id,))
        self.con.commit()
        self.assertEqual(self.prefs(self.A)["display_name"], "Alice Judge")
        self.assertNotIn("display_name", self.prefs(self.B))

    def test_removed_then_re_invited_member(self):
        """The real way back: the owner invites the same account again and
        it accepts."""
        self.put(self.A, A_PREFS)
        collaboration.revoke_member(self.con, "local", self.A.principal_id,
                                    self.owner.principal_id)
        self.con.commit()
        invite = collaboration.create_invite(self.con, "local", self.A.principal_id,
                                             "editor", self.owner.principal_id)
        collaboration.accept_invite(self.con, invite["invite_id"], self.A.principal_id)
        self.con.commit()
        self.assertEqual(self.prefs(self.A)["display_name"], "Alice Judge")
        for ctx in self.others:
            self.assertNotIn("Alice Judge", self.everything(ctx), ctx.principal_id)

    def test_pseudonymised_purge_drops_the_members_values(self):
        """acct_attribution.sql:7 (decision 4, pseudonymisation) renames
        the actor; the values no longer belong to the member."""
        self.put(self.A, A_PREFS)
        self.con.execute("UPDATE audit SET actor = 'removed:' || ? WHERE actor = ?",
                         ("req-1", self.A.principal_id))
        self.con.commit()
        self.assertNotIn("display_name", customer.get_preferences(
            self.con, self.A.principal_id, hosted=True))


class ExportTest(MemberPrefsBase):

    def test_another_members_export_carries_none_of_a_values(self):
        from earwitness.app import customer_query, exports
        self.put(self.A, A_PREFS)
        for ctx in (self.B, self.owner):
            with db.transaction(self.con, write=False):
                view = customer_query._view_revision(self.con, ctx)
            out = services.customer_research_export(
                ctx, self.con, MEMBER, {"query": {"version": 2, "mode": "ads",
                                                  "brands": ["acme"]},
                                        "expected_view_revision": view})
            self.con.commit()
            body = exports.load_export_bytes(self.con, out["export_id"], ctx)
            text = json.dumps(body, default=lambda b: b.decode("utf-8", "replace")
                              if isinstance(b, bytes) else str(b))
            self.assertNotIn("Alice Judge", text, ctx.principal_id)
            self.assertNotIn("Asia/Tokyo", text, ctx.principal_id)


class HttpTest(unittest.TestCase):
    """Over the hosted HTTP app: owner, an editor and a viewer. The owner's
    own values never reach the others, and nothing can be revalidated:
    no ETag, no 304, private no-store per cookie."""

    def setUp(self):
        import sqlite3
        http.HostedHttpTest.setUp(self)
        self.addCleanup(http.HostedHttpTest.tearDown, self)
        self._editor_id = "sess-prefs-editor"
        http._register_session(self.app, self._editor_id, "u-editor", csrf="tok-viewer")
        con = sqlite3.connect(self.db_path, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            invite = collaboration.create_invite(con, "local", "oidc:https://i.example:u-editor",
                                                 "editor", "oidc:https://i.example:u-owner")
            collaboration.accept_invite(con, invite["invite_id"],
                                        "oidc:https://i.example:u-editor")
            con.commit()
        finally:
            con.close()

    _call = http.HostedHttpTest._call

    def get(self, session, extra=()):
        import asyncio
        hdrs = [(b"host", b"127.0.0.1:8771"), (b"cookie", f"ew_session={session}".encode()),
                *extra]
        scope = {"type": "http", "method": "GET", "path": "/api/v1/customer/preferences",
                 "scheme": "http", "headers": hdrs, "query_string": b""}
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        asyncio.run(self.app(scope, receive, send))
        start = next(m for m in sent if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        return start["status"], {k.decode().lower(): v.decode()
                                 for k, v in start.get("headers", [])}, body.decode()

    def test_the_owners_values_reach_no_one_else_and_nothing_revalidates(self):
        status, _, before = self.get(self._session_id)
        self.assertEqual(status, 200, before)
        revision = json.loads(before)["preferences"]["revision"]
        status, _, out = self._call("/api/v1/customer/preferences", "PUT",
                                    {"display_name": "Olga Owner", "time_zone": "Asia/Tokyo"},
                                    match=revision)
        self.assertEqual(status, 200, out)
        status, headers, mine = self.get(self._session_id)
        self.assertIn("Olga Owner", mine)
        for session in (self._editor_id, self._viewer_id):
            for extra in ((), ((b"if-none-match", f'"{revision}"'.encode()),
                               (b"if-modified-since", b"Mon, 26 Oct 2026 00:00:00 GMT"))):
                status, headers, seen = self.get(session, extra)
                self.assertEqual(status, 200, (session, seen))
                self.assertNotIn("Olga Owner", seen, session)
                self.assertNotIn("Asia/Tokyo", seen, session)
                self.assertNotIn("display_name", json.loads(seen)["preferences"], session)
                self.assertNotIn("etag", headers, session)
                self.assertNotIn("last-modified", headers, session)
                self.assertIn("no-store", headers.get("cache-control", ""), session)
                self.assertIn("cookie", headers.get("vary", "").lower(), session)


if __name__ == "__main__":
    unittest.main()
