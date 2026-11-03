"""Display data for the customer UI: member/share emails and titles,
stable reason codes on campaign evaluations, and Retry-After on the
browser-session 429 (follow-up D)."""
from __future__ import annotations

import asyncio
import copy
import json
import pickle
import sqlite3
import time
import unittest

from earwitness.app import asgi as _asgi
from earwitness.app import campaigns, citations, db
from earwitness.app import collaboration as _collab
from earwitness.app import services as _services
from tests import test_customer_campaigns as cc
from tests import test_followup_names_invites as ni

OWNER = "oidc:https://i.example:owner"
MEMBERS = "/api/v1/workspaces/acme/members"


class _Db(ni._Hosted):
    def _con(self):
        con = sqlite3.connect(self.fixture.db_path, isolation_level=None)
        con.row_factory = sqlite3.Row
        self.addCleanup(con.close)
        return con


class MemberEmailTest(_Db):
    def _bind(self, con, principal, email, workspace="local"):
        db.audit(con, principal, "invite_accept_bound", "membership", principal,
                 {"workspace": workspace, "bound_email": email}, 1)

    def test_members_carry_bound_email_latest_wins_else_empty(self):
        con = self._con()
        self._bind(con, "ed-1", "old@x.example")
        self._bind(con, "ed-1", "ed@x.example")
        self._bind(con, OWNER, "elsewhere@x.example", workspace="other-ns")
        status, out = self.fixture._call(MEMBERS, method="GET", headers=self.headers)
        self.assertEqual(status, 200, out)
        emails = {m["member"]: m["email"] for m in out["members"]}
        self.assertEqual(emails, {"ed-1": "ed@x.example", OWNER: ""})
        self.assertEqual(set(out["members"][0]),
                         {"member", "role", "revoked", "created_at", "email", "display"})

    def test_missing_audit_table_yields_empty_email(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        self.assertEqual(_services._member_emails(con, "local"), {})

    def test_bad_json_and_empty_email_rows_are_skipped(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute("CREATE TABLE audit (id INTEGER PRIMARY KEY, action TEXT,"
                    " target_id TEXT, detail TEXT)")
        con.executemany("INSERT INTO audit (action, target_id, detail) VALUES"
                        " ('invite_accept_bound', ?, ?)", [
                            ("a", "not json"),
                            ("b", json.dumps({"workspace": "local", "bound_email": ""})),
                            ("c", json.dumps({"workspace": "local", "bound_email": "c@x"}))])
        self.assertEqual(_services._member_emails(con, "local"), {"c": "c@x"})


class _Ctx:
    """Owner-shaped stub: every gate passes except denied metadata keys."""
    principal_id = OWNER
    credential_kind = "browser_session"

    def __init__(self, denied=()):
        self.denied = set(denied)

    def require_member(self, _op):
        return None

    def permits(self, _category):
        return True

    def authorize_disclosure(self, *_a, **_k):
        return None

    def disclosure_allowed(self, con, key, _field):
        return key not in self.denied


class ShareDisplayTest(_Db):
    def _shares(self, con, ctx=None):
        return _services.collab_shares(
            ctx or _Ctx(), con, {"row_namespace": "local"})["shares"]

    def test_shares_carry_audience_email_and_object_title(self):
        con = self._con()
        db.audit(con, "guest-1", "invite_accept_bound", "membership", "guest-1",
                 {"workspace": "local", "bound_email": "guest@x.example"}, 1)
        by_obj = {}
        for kind, obj, aud in (("evidence", "fixture-ad", "guest-1"),
                               ("episode", "e2", "someone@x.example"),
                               ("campaign", "1", "guest-1"),
                               ("evidence", "no-such", "guest-1"),
                               ("export", "x", "guest-1")):
            out = _collab.create_share(con, "local", kind, obj, aud, [], OWNER)
            by_obj[obj] = out["share_id"] if "share_id" in out else out["id"]
        rows = {r["object_id"]: r for r in self._shares(con)}
        self.assertEqual(rows["fixture-ad"]["object_title"], "Morning brew")
        self.assertEqual(rows["e2"]["object_title"], "Evening blend")
        self.assertEqual(rows["1"]["object_title"], "legacy campaign")
        self.assertEqual(rows["no-such"]["object_title"], "")
        self.assertEqual(rows["x"]["object_title"], "")
        self.assertEqual(rows["fixture-ad"]["audience_email"], "guest@x.example")
        self.assertEqual(rows["e2"]["audience_email"], "")
        self.assertIn("audience", rows["e2"])

    def test_title_withheld_for_episode_not_permitted_for_this_context(self):
        con = self._con()
        from earwitness.app import customer
        allowed = {r["id"] for r in customer.permitted_episodes(con, _Ctx())}
        self.assertEqual(allowed, {"e1", "e2"})
        keys = {}

        class Probe(_Ctx):
            def disclosure_allowed(self, con, key, field):
                keys.setdefault(key, None)
                return True
        customer.permitted_episodes(con, Probe())
        denied = [k for k in keys if k.endswith("e2") or "e2" in k]
        _collab.create_share(con, "local", "episode", "e2", "g", [], OWNER)
        _collab.create_share(con, "local", "evidence", "fixture-ad", "g", [], OWNER)
        rows = {r["object_id"]: r for r in self._shares(con, _Ctx(denied=denied))}
        self.assertEqual(rows["e2"]["object_title"], "")
        self.assertEqual(rows["fixture-ad"]["object_title"], "Morning brew")

    def test_titles_read_only_the_episodes_the_listed_shares_reference(self):
        # A large workspace: Settings must not walk every episode to
        # title two shares. Count metadata checks and full-table reads.
        con = self._con()
        con.executemany("INSERT INTO episodes (id, kind, ep_title, updated_at) VALUES (?, 'episode', ?, '2026-01-01')",
                        [(f"bulk-{i}", f"Bulk {i}") for i in range(300)])
        _collab.create_share(con, "local", "episode", "e2", "g", [], OWNER)
        _collab.create_share(con, "local", "evidence", "fixture-ad", "g", [], OWNER)
        checked = []

        class Counting(_Ctx):
            def disclosure_allowed(self, con, key, field):
                checked.append(key)
                return True
        statements = []
        con.set_trace_callback(statements.append)
        try:
            rows = {r["object_id"]: r for r in self._shares(con, Counting())}
        finally:
            con.set_trace_callback(None)
        self.assertEqual(rows["e2"]["object_title"], "Evening blend")
        self.assertEqual(rows["fixture-ad"]["object_title"], "Morning brew")
        self.assertLessEqual(len(checked), 2, f"metadata checks: {len(checked)}")
        full_scans = [q for q in statements
                      if "FROM episodes" in q and "WHERE" not in q.split("FROM episodes", 1)[1]]
        self.assertEqual(full_scans, [], "no whole-table episode read")


VIEWER = "oidc:https://i.example:u-viewer"


def _real_ctx(**kw):
    from earwitness.app import access as A
    return A.AccessContext.create(principal_id=VIEWER, principal_kind="viewer",
                                  workspace_id="local", **kw)


class DisclosureLeakTest(_Db):
    """Review blockers on d15ef41: display fields never disclose past the
    gates the underlying reads use."""
    ENTRY = {"row_namespace": "local", "workspace_id": "w"}

    def test_campaign_title_needs_the_campaign_read_gate(self):
        from earwitness.app import access as A, ops
        con = self._con()
        _collab.create_share(con, "local", "campaign", "1", "oidc:https://i.example:u-guest", [], OWNER)
        for label, ctx in (("reader bearer, research:read only",
                            _real_ctx(credential_kind="reader_bearer", scopes=("research:read",))),
                           ("browser session, content_policy campaign=deny",
                            _real_ctx(credential_kind="browser_session", content_policy={"campaign": "deny"}))):
            with self.subTest(label):
                with self.assertRaises(A.AuthorizationError):
                    ops.campaign_detail(con, 1, ctx)
                rows = [r for r in _services.collab_shares(ctx, con, self.ENTRY)["shares"]
                        if r["object_type"] == "campaign"]
                self.assertEqual([r["object_title"] for r in rows], [""], label)
        allowed = _real_ctx(credential_kind="browser_session")
        rows = [r for r in _services.collab_shares(allowed, con, self.ENTRY)["shares"]
                if r["object_type"] == "campaign"]
        self.assertEqual([r["object_title"] for r in rows], ["legacy campaign"], "a permitted reader still sees it")

    def test_member_emails_are_for_membership_managers_and_oneself(self):
        from earwitness.app import access as A
        con = self._con()
        new = "oidc:https://i.example:u-newhire"
        inv = _collab.create_invite(con, "local", "newhire@corp.example", "editor", OWNER)
        acc = A.AccessContext.create(principal_id=new, principal_kind="viewer", workspace_id="local",
                                     credential_kind="browser_session", authority_kind="bootstrap",
                                     scopes=("invite:accept",))
        _services.collab_invite_accept(acc, con, self.ENTRY, inv.get("invite_id") or inv.get("id"),
                                       verified_email="newhire@corp.example")
        _collab.create_share(con, "local", "evidence", "fixture-ad", new, [], OWNER)
        emails = lambda ctx: ({m["member"]: m["email"] for m in _services.collab_members(ctx, con, self.ENTRY)["members"] if m["email"]},
                              {r["audience"]: r["audience_email"] for r in _services.collab_shares(ctx, con, self.ENTRY)["shares"] if r["audience_email"]})
        for label, ctx in (("viewer browser session", _real_ctx(credential_kind="browser_session")),
                           ("viewer reader bearer", _real_ctx(credential_kind="reader_bearer", scopes=("research:read",)))):
            with self.subTest(label):
                self.assertEqual(emails(ctx), ({}, {}), label)
        own = A.AccessContext.create(principal_id=new, principal_kind="viewer", workspace_id="local",
                                     credential_kind="browser_session")
        self.assertEqual(emails(own)[0], {new: "newhire@corp.example"}, "a member sees their own email")
        owner = A.AccessContext.create(principal_id=OWNER, principal_kind="owner", workspace_id="local",
                                       credential_kind="browser_session")
        self.assertEqual(emails(owner), ({new: "newhire@corp.example"}, {new: "newhire@corp.example"}),
                         "the membership manager sees it, as in pending invitations")
        owner_reader = A.AccessContext.create(principal_id=OWNER, principal_kind="owner", workspace_id="local",
                                              credential_kind="reader_bearer", scopes=("research:read",))
        self.assertEqual(emails(owner_reader), ({}, {}), "reader credentials never see others' emails")


class ReasonTypeTest(unittest.TestCase):
    def test_reason_is_a_plain_string_with_code(self):
        r = campaigns.Reason("message: mismatch", "requirement_mismatch", "message")
        self.assertEqual(json.dumps(r), json.dumps("message: mismatch"))
        self.assertEqual(json.dumps({"a": [r]}), json.dumps({"a": ["message: mismatch"]}))
        self.assertEqual(r, "message: mismatch")
        self.assertEqual(hash(r), hash("message: mismatch"))
        for clone in (copy.deepcopy(r), pickle.loads(pickle.dumps(r)), copy.copy(r)):
            self.assertEqual((clone, clone.code, clone.field),
                             ("message: mismatch", "requirement_mismatch", "message"))
        self.assertEqual(campaigns.Reason("x").code, "other")
        # A browser round trip returns plain strings; pins must still match.
        self.assertTrue(campaigns._pins_match({"r": [r]}, {"r": ["message: mismatch"]}))
        self.assertFalse(campaigns._pins_match({"r": [r]}, {"r": ["other text"]}))
        self.assertEqual(campaigns.reason_code_rows(["plain", r]),
                         [{"code": "other", "field": None},
                          {"code": "requirement_mismatch", "field": "message"}])


class EvaluationCodesTest(unittest.TestCase):
    NOW = "2026-04-01T00:00:00+00:00"

    def _codes(self, ev):
        return [(r.code, r.field) for r in ev["reasons"]]

    def test_requirement_mismatch_and_unverified_codes(self):
        p = cc.placement("p1", requirements={"message": "buy", "offer": "X",
                                             "duration_s": [1, 5]})
        o = cc.occ("o1", fields={"message": "other"}, verified_fields=["message"])
        ev = campaigns.evaluate_one(p, [o], {"state": "complete"}, self.NOW)
        codes = self._codes(ev)
        self.assertIn(("requirement_mismatch", "message"), codes)
        self.assertIn(("requirement_unverified", "offer"), codes)
        self.assertIn(("requirement_mismatch", "duration_s"), codes)
        self.assertTrue(all(r.startswith("o1: ") for r in ev["reasons"]))

    def test_gap_codes_and_check_codes(self):
        o = cc.occ("o1", correspondence="ambiguous", review_status="x",
                   advertiser_confirmed=False)
        ok, gaps = campaigns._occurrence_eligible(
            o, cc.placement("p1"), {"a": "unknown", "b": "fail"})
        self.assertEqual([(g.code, g.field) for g in gaps], [
            ("correspondence_ambiguous", None), ("not_reviewed_ad", None),
            ("attribution_unconfirmed", None), ("check_unknown", "a"),
            ("check_failed", "b")])

    def test_coverage_and_count_codes(self):
        p = cc.placement("p1", date_to="2026-03-15")
        ev = campaigns.evaluate_one(
            p, [], {"state": "partial", "missing": ["e9"], "failed": []}, self.NOW)
        self.assertEqual(ev["outcome"], "incomplete_coverage")
        self.assertEqual([c[0] for c in self._codes(ev)],
                         ["missing_transcripts", "no_failed_fetches"])
        ev = campaigns.evaluate_one(
            cc.placement("p1", date_to="2026-03-15", expected_count=2),
            [cc.occ("o1")], {"state": "complete"}, self.NOW)
        self.assertEqual(ev["outcome"], "ambiguous")
        self.assertEqual(ev["reasons"][0].code, "insufficient_count")
        self.assertEqual(ev["reasons"][0], campaigns.INSUFFICIENT_SUPPORTED_COUNT)

    def test_date_uncertain_keeps_code_through_occurrence_prefix(self):
        ev = campaigns.evaluate_one(
            cc.placement("p1"), [cc.occ("o1", date_precision="uncertain")],
            {"state": "complete"}, self.NOW)
        self.assertEqual(ev["outcome"], "ambiguous")
        self.assertEqual(ev["reasons"][0], "o1: uncertain date precision")
        self.assertEqual(ev["reasons"][0].code, "date_uncertain")

    def test_contradicted_and_allocation_competition_codes(self):
        p = cc.placement("p1", requirements={"message": "buy"})
        o = cc.occ("o1", evidence_id="ev1", fields={"message": "other"},
                   verified_fields=["message"])
        ev = campaigns.evaluate_one(p, [o], {"state": "complete"}, self.NOW)
        self.assertEqual(ev["outcome"], "contradicted")
        self.assertEqual(self._codes(ev)[0], ("contradicted", None))
        self.assertIn(("requirement_mismatch", "message"), self._codes(ev))
        # Allocation: o1 goes to p1; p2 sees it as competition.
        out = campaigns.allocate_campaign(
            [cc.placement("p1", date_to="2026-03-15"),
             cc.placement("p2", date_to="2026-03-31")],
            [cc.occ("o1")], {"state": "complete"}, self.NOW)
        self.assertEqual(out["evaluations"]["p1"]["outcome"], "supported")
        comp = out["evaluations"]["p2"]
        self.assertEqual(comp["outcome"], "ambiguous")
        self.assertEqual(comp["reasons"][0].code, "allocation_competition")
        self.assertTrue(comp["reasons"][0].startswith("allocation competition: "))


class DraftReasonCodesTest(unittest.TestCase):
    def setUp(self):
        self.con = cc.fresh_db()
        cc.seed_episode(self.con)
        self.cid = cc.seed_campaign(self.con)
        self.addCleanup(self.con.close)

    def test_draft_reason_codes_parallel_the_reasons_outside_the_pins(self):
        con = self.con
        cc.seed_full_episode(con, ep_id="ep-full")
        cc.add_candidate(con, "shared", episode_id="ep-full")
        cc.verify_full_ad(con, "shared", episode_id="ep-full")
        pid_ok = cc.add_placement(con, self.cid, episode_id="ep-full",
                                  date_to="2026-03-15",
                                  requirements={"duration_s": [5, 15]})
        pid_bad = cc.add_placement(con, self.cid, episode_id="ep-full",
                                   date_to="2026-03-31",
                                   requirements={"duration_s": [20, 30]})
        for pid in (pid_ok, pid_bad):
            rev = con.execute("SELECT revision FROM campaigns WHERE id=?",
                              (self.cid,)).fetchone()[0]
            campaigns.save_placement_evidence(
                con, self.cid, pid,
                citations.current_source_ref(con, "evidence", "shared"), rev)
        draft = campaigns.build_customer_draft(con, self.cid)
        evaluations = draft["allocation"]["evaluations"]
        codes = draft["reason_codes"]["evaluations"]
        self.assertEqual(set(codes), {pid_ok, pid_bad})
        for pid, ev in evaluations.items():
            self.assertEqual(len(codes[pid]), len(ev["reasons"]))
        bad = [(c["code"], c["field"]) for c in codes[pid_bad]]
        self.assertEqual(bad[0], ("contradicted", None))
        self.assertIn(("requirement_mismatch", "duration_s"), bad)
        self.assertIn(("allocation_retained", None), bad)
        self.assertEqual(draft["reason_codes"]["citations"], {})
        # Frozen-report staleness compares pins/allocation: no new keys there.
        self.assertNotIn("reason_codes", draft["pins"])
        self.assertNotIn("reason_codes", draft["pins"]["allocation"])
        self.assertNotIn("reason_codes", json.dumps(draft["allocation"]))
        self.assertNotIn("code", json.dumps(evaluations[pid_bad]))


class RetryAfterTest(_Db):
    def test_browser_session_429_carries_retry_after(self):
        self.fixture.app.limiter = _asgi.RateLimiter(reads_per_minute=1, burst=1)

        def call():
            hdrs = [(b"host", b"127.0.0.1:8771"), (b"content-length", b"2")]
            hdrs += [(k.encode(), v.encode()) for k, v in self.headers.items()]
            sent = []

            async def receive():
                return {"type": "http.request", "body": b"{}", "more_body": False}

            async def send(message):
                sent.append(message)
            asyncio.run(self.fixture.app(
                {"type": "http", "method": "GET", "path": MEMBERS, "scheme": "http",
                 "headers": hdrs, "query_string": b""}, receive, send))
            start = next(m for m in sent if m["type"] == "http.response.start")
            return start["status"], dict(start["headers"])

        status, _ = call()
        self.assertEqual(status, 200)
        status, headers = call()
        self.assertEqual(status, 429)
        retry = int(headers[b"retry-after"] if b"retry-after" in headers
                    else headers[b"Retry-After"])
        self.assertGreaterEqual(retry, 1)


class SettingsReadBudgetTest(_Db):
    """final17: Settings open, then save + reload twice, all at one instant.
    /session sits outside the read bucket; every other call spends one token."""
    SETTINGS = ("/api/v1/customer/preferences", MEMBERS, "/api/v1/workspaces/acme/invites",
                "/api/v1/shares", "/api/v1/billing/status")

    def setUp(self):
        super().setUp()
        self.now = time.time()  # frozen: no refill between calls
        self.fixture.app.config.clock = lambda: self.now
        self.statuses = []

    def _get(self, path):
        status, out = self.fixture._call(path, method="GET", headers=self.headers)
        self.statuses.append((path, status))
        if path == self.SETTINGS[0]:
            self.revision = out["preferences"]["revision"]

    def _save(self, field, value):
        status, _ = self.fixture._call(self.SETTINGS[0], method="PUT",
            body={field: value, "expected_revision": self.revision}, headers=self.headers)
        self.statuses.append(("PUT preferences", status))
        self._get(self.SETTINGS[0])  # the in-place re-render re-reads preferences only

    def test_open_and_two_save_reload_cycles_are_never_rate_limited(self):
        for path in self.SETTINGS:  # open
            self._get(path)
        for field, value in (("workspace_name", "Acme 2"), ("share_audience", "named_reviewers")):
            self._save(field, value)
            for path in self.SETTINGS + ("/api/v1/customer/overview",):  # reload + chrome
                self._get(path)
        self.assertEqual(len(self.statuses), 21)
        self.assertEqual([c for c in self.statuses if c[1] == 429], [])
        self.assertIn(("PUT preferences", 200), self.statuses)

    def test_session_burst_then_the_sustained_rate_still_limit(self):
        admitted = 0
        while self.fixture._call(MEMBERS, method="GET", headers=self.headers)[0] != 429:
            admitted += 1
            self.assertLessEqual(admitted, 100)
        self.assertEqual(admitted, _asgi.SESSION_BURST)
        self.now += 60.0 / _asgi.SESSION_READS_PER_MINUTE  # one more read per refill
        self.assertEqual(self.fixture._call(MEMBERS, method="GET", headers=self.headers)[0], 200)
        self.assertEqual(self.fixture._call(MEMBERS, method="GET", headers=self.headers)[0], 429)


if __name__ == "__main__":
    unittest.main()
