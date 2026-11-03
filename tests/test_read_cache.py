"""READ-CACHE: heavy aggregate reads cached per data state x disclosure class.

Six sessions in one workspace waited ~21-22 s for Overview: the eight
aggregate reads (Overview, Search, saved items, Brands, brand detail,
summary, Shows, Alerts list) were either not remembered at all or
remembered per user, so every editor paid a cold compute. They are now
remembered once per (database, data state, read, disclosure class) and
computed under a principal-free class context (design:
launch-readiness ws-app/read-cache-design.md; LEAD DECISION 2).

Contract checked here:

1. Classes never share: owner, editor and viewer, and Plan A vs Plan B,
   each get their own class's result, cached or fresh; the plan is in the
   class even if the data state did not move. Share guests are refused
   before any lookup and fill nothing (F8).
2. Every write path invalidates: after each write, every cached read
   equals a fresh one (in-process, another connection, raw SQL, schema).
3. Warm-up fills the browser classes at start and refills after an
   import, so the next caller computes nothing.
4. The memo stays bounded: at most 8 classes per database, the global
   count cap holds across many workspaces and classes.
5. Cached equals fresh over random callers and plans (F5), and the class
   compute is principal-free.
Field audit (F1-F9): private notes stay private; workspace-shared items
(tracked brands, bookmarks, saved items and their notes) are identical
for two editors, cached and fresh, in either order and concurrently;
alert read marks and counts, the saved-items revision and cursor stay
per caller (READ-PP: laid over the class body after the cache);
Overview recomputes when the UTC day changes; no cached body carries a
principal, session or actor.

Run: PYTHONPATH=. python -m unittest tests.test_read_cache
"""
from __future__ import annotations

import contextlib
import datetime
import json
import os
import random
import sqlite3
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from earwitness.app import access, browser_auth, campaigns, citations, customer
from earwitness.app import customer_contracts, customer_query, customer_saved, db, services

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tests import test_uat_dec_d1api as d1  # noqa: E402
from tests.test_uat_d1 import _republish  # noqa: E402

WS = "ws1"
# Writes go through the owner-bound workspace gate (entry["is_owner"]).
OWN = {"is_owner": True, "row_namespace": "local", "workspace_id": WS}
# Reads go through a member workspace's registry entry, as a request and the
# warm-up pass one (connections.register_workspace's default).
MEMBER = {"is_owner": False, "row_namespace": "local", "workspace_id": WS}
# Overview's own Search read (views.js overviewPage).
OVERVIEW_SEARCH = {"version": 2, "mode": "ads", "q": "", "brands": None, "shows": None,
                   "categories": None, "statuses": None, "audio": None, "date_status": "all",
                   "from": None, "to": None, "sort": "newest", "page": 1, "ids": None}
# Per-caller fields of a cached route (field audit: only saved items).
PER_USER = {"saved": ("collection_revision", "next_cursor"),
            "saved_bookmarks": ("collection_revision", "next_cursor")}
# FR-11: whether this caller may change an item (saved items, alerts) is per
# caller, marked on copies after the shared read; so is whether this caller
# added a saved item (RB-R1-01, added_by_me).
PER_CALLER_MARKS = ("can_change", "added_by_me")


def session(name, kind="editor", workspace=WS):
    """A browser session context, built as the hosted request path builds it."""
    ctx = browser_auth.build_session_context(
        {"issuer": "https://issuer.example", "subject": name, "session_hash": f"sess-{name}"},
        workspace, kind, f"req-{name}",
        scopes=("customer:read",) if kind == "viewer" else ("customer:read", "customer:write"))
    ctx.admitted_role = kind
    return ctx


def guest():
    return access.AccessContext.create(principal_id="g1", principal_kind="guest", workspace_id=WS,
                                       scopes={"research:read"}, authority_kind="share",
                                       share_grant_id="s1", credential_kind="browser_session")


def fresh():
    """Every memo computes afresh (the code before READ-CACHE for these reads)."""
    return mock.patch.object(customer_query, "_ctx_memo_identity", return_value=None)


def routes(con, entry=MEMBER):
    query = customer_contracts.normalize_query(OVERVIEW_SEARCH)
    return {
        "overview": lambda c: services.customer_overview(c, con, entry),
        "search": lambda c: services.customer_search(c, con, entry, dict(query)),
        "saved": lambda c: services.customer_saved_items(c, con, entry),
        "saved_bookmarks": lambda c: services.customer_saved_items(c, con, entry, "bookmark"),
        "brands": lambda c: services.customer_brands(c, con, entry, ""),
        "brand": lambda c: services.customer_brand_detail(c, con, entry, "acme"),
        "summary": lambda c: services.customer_summary(
            c, con, entry, d1._q(brands=["acme"]), "week"),
        "shows": lambda c: services.customer_shows(c, con, entry, ""),
        "alerts": lambda c: services.customer_alerts(c, con, entry),
    }


def unmarked(body):
    """body without the per-caller marks (can_change, added_by_me), at any depth."""
    if isinstance(body, dict):
        return {k: unmarked(v) for k, v in body.items() if k not in PER_CALLER_MARKS}
    if isinstance(body, list):
        return [unmarked(v) for v in body]
    return body


def without(name, body):
    return unmarked({k: v for k, v in body.items() if k not in PER_USER.get(name, ())})


def text(body) -> str:
    return json.dumps(body, sort_keys=True, default=str)


class _Counted:
    """Count calls of module.name while still running it (optionally slowed)."""

    def __init__(self, module, name, delay=0.0):
        self.module, self.name, self.delay = module, name, delay
        self.calls = 0
        self._lock = threading.Lock()

    def __enter__(self):
        real = getattr(self.module, self.name)

        def wrapper(*args, **kwargs):
            with self._lock:
                self.calls += 1
            if self.delay:
                time.sleep(self.delay)
            return real(*args, **kwargs)
        self._patch = mock.patch.object(self.module, self.name, wrapper)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False


class ReadCacheBase(d1.SeededBase):
    """The d1 seed plus one acme alert with an unread new match, and two
    editors A and B with their own tracked brand, saved item (with a note)
    and private personal note on the same record."""

    maxDiff = None

    def setUp(self):
        super().setUp()
        _republish(self.con)
        customer_query._VIEW_MEMO.clear()
        self.addCleanup(customer_query._VIEW_MEMO.clear)
        self.aid = customer.save_alert(self.con, "acme watch",
                                       {"version": 2, "mode": "ads", "brands": ["acme"]})["id"]
        self.con.commit()
        d1._evidence(self.con, "r8", "e1", 45.0, 58.0, self.tv["cur"], "acme")
        _republish(self.con)
        customer.check_alert(self.con, self.aid)
        self.con.commit()
        self.A, self.B = session("editor-a"), session("editor-b")
        self.viewer, self.owner = session("viewer-v", "viewer"), session("owner-o", "owner")
        self.members(self.con, (self.A, self.B, self.viewer, self.owner))
        self.note_ref = self.ref("r1")
        self.save(self.A, {"type": "bookmark", "target": {"kind": "brand", "id": "acme"}})
        self.save(self.A, {"type": "excerpt", "title": "Clip", "note": "A's note text",
                           "target": {"ref": self.note_ref}})
        self.save(self.B, {"type": "bookmark", "target": {"kind": "brand", "id": "beta"}})
        self.save(self.B, {"type": "query", "title": "Beta ads", "note": "B's query note",
                           "query": {"version": 2, "mode": "ads", "brands": ["beta"]}})
        self.note_a = services.customer_save_mistake(self.A, self.con, OWN, self.note_ref,
                                                     "A private reason")["id"]
        self.note_b = services.customer_save_mistake(self.B, self.con, OWN, self.note_ref,
                                                     "B private reason")["id"]
        self.con.commit()
        self.routes = routes(self.con)
        self.path = self.con.execute("PRAGMA database_list").fetchone()[2]

    @staticmethod
    def members(con, ctxs):
        """Each session a member of the workspace in its role (FR-11: the
        owner role may change every item)."""
        from earwitness.app import collaboration
        collaboration.ensure_aux(con)
        con.executemany("INSERT OR REPLACE INTO memberships (workspace_id, member, role,"
                        " created_at) VALUES ('local', ?, ?, ?)",
                        [(c.principal_id, c.admitted_role, db.utcnow()) for c in ctxs])
        con.commit()

    def save(self, ctx, body):
        out = services.customer_save_item(ctx, self.con, OWN, body)
        self.con.commit()
        return out["item"]

    def read(self, ctx, names=None):
        return {name: fn(ctx) for name, fn in self.routes.items()
                if names is None or name in names}

    def fresh_read(self, ctx, names=None):
        with fresh():
            return self.read(ctx, names)

    def assertCachedEqualsFresh(self, *ctxs, names=None):
        for ctx in ctxs:
            cached = self.read(ctx, names)
            again = self.read(ctx, names)   # a warm hit, too
            plain = self.fresh_read(ctx, names)
            for name in cached:
                self.assertEqual(cached[name], plain[name], f"{name} cached != fresh for"
                                 f" {ctx.principal_kind} {ctx.principal_id}")
                self.assertEqual(again[name], plain[name], f"{name} warm hit != fresh")

    def open_reader(self):
        return db.open_existing(self.path, readonly=True)


class ClassIsolationTest(ReadCacheBase):
    """Phase 2 (1): two roles and Plan A vs Plan B in one workspace."""

    def test_roles_never_get_another_classes_result(self):
        for order in ((self.viewer, self.A, self.owner), (self.owner, self.A, self.viewer)):
            customer_query._VIEW_MEMO.clear()
            for ctx in order:
                self.read(ctx)
            self.assertCachedEqualsFresh(*order)
        # The classes really differ (else this test proves nothing).
        viewer, editor, owner = (self.read(c)["search"]["revision"]
                                 for c in (self.viewer, self.A, self.owner))
        self.assertEqual(len({viewer, editor, owner}), 3)

    def _plan_a(self, con=None):
        (con or self.con).execute(
            "INSERT INTO source_content_policy (workspace_id, source_key, content_category,"
            " agent_disclosure, export_eligible, updated_at) VALUES"
            " ('local', 'transcript:e1', 'transcript', 'deny', 0, ?)", (db.utcnow(),))
        (con or self.con).commit()

    def test_plan_a_and_plan_b_never_share(self):
        plan_b = self.read(self.A)
        self._plan_a()
        plan_a = self.read(self.A)
        self.assertCachedEqualsFresh(self.A, self.B, self.viewer)
        self.assertNotEqual(plan_a["search"], plan_b["search"])
        self.assertTrue(plan_a["search"].get("excerpt_only"))

    def test_the_plan_is_in_the_class_even_if_the_data_state_did_not_move(self):
        """Belt and braces: the plan is derived from guarded rows, so the data
        state moves with it; the class keys it explicitly as well."""
        with mock.patch.object(db, "view_state_key", return_value="frozen"):
            self.read(self.A)
            self._plan_a()
            cached = self.read(self.A)
        with fresh():
            plain = self.read(self.A)
        for name in ("overview", "search", "alerts", "saved"):
            self.assertEqual(cached[name], plain[name], name)

    def test_share_guests_are_refused_before_any_lookup(self):
        """F8: all eight reads refuse a share guest; nothing is filled."""
        self.read(self.A)
        before = list(customer_query._VIEW_MEMO)
        with _Counted(customer_query, "_memoized") as looked:
            for name, fn in self.routes.items():
                with self.assertRaises(access.AuthorizationError, msg=name) as caught:
                    fn(guest())
                self.assertEqual(caught.exception.status, 403)
        self.assertEqual(looked.calls, 0)
        self.assertEqual(list(customer_query._VIEW_MEMO), before)


class FieldAuditTest(ReadCacheBase):
    """F1-F6 and F9, in the orders A then B, B then A, and concurrently."""

    def _orders(self):
        """Yield (label, {ctx name: route bodies}) for each read order, the
        cache cleared before each."""
        for label, first, second in (("A then B", self.A, self.B), ("B then A", self.B, self.A)):
            customer_query._VIEW_MEMO.clear()
            out = {first.principal_id: self.read(first)}
            out[second.principal_id] = self.read(second)
            yield label, out
        customer_query._VIEW_MEMO.clear()
        out, errors = {}, []

        def run(ctx):
            con = self.open_reader()
            try:
                out[ctx.principal_id] = {name: fn(ctx) for name, fn in routes(con).items()}
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
            finally:
                con.close()
        threads = [threading.Thread(target=run, args=(c,)) for c in (self.A, self.B)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        yield "concurrent", out
        with fresh():
            yield "fresh", {c.principal_id: self.read(c) for c in (self.A, self.B)}

    def test_f1_private_notes_stay_private(self):
        for label, out in self._orders():
            for me, other, other_reason in ((self.A, self.B, "B private reason"),
                                            (self.B, self.A, "A private reason")):
                for name, body in out[me.principal_id].items():
                    blob = text(body)
                    self.assertNotIn(other_reason, blob, f"{label}: {name}")
                    self.assertNotIn(other.principal_id, blob, f"{label}: {name}")
                    self.assertNotIn('"mistake_id"', blob, f"{label}: {name}")
        for me, mine in ((self.A, self.note_a), (self.B, self.note_b)):
            for _ in range(2):
                notes = services.customer_note_collection(me, self.con, None)["notes"]
                self.assertEqual([n["id"] for n in notes], [mine])
        tags = {str(key[2]) for key in customer_query._VIEW_MEMO}
        self.assertFalse(any("note" in t or "mistake" in t for t in tags), tags)

    def test_f2_shared_items_are_identical_and_a_write_shows_for_both(self):
        for label, out in self._orders():
            a, b = out[self.A.principal_id], out[self.B.principal_id]
            for name in a:
                self.assertEqual(self.shared(name, a[name]), self.shared(name, b[name]),
                                 f"{label}: {name}")
            titles = sorted(i["title"] for i in a["saved"]["items"])
            self.assertEqual(titles, ["Acme", "Beta", "Beta ads", "Clip"], label)
            notes = sorted(i["note"] for i in a["saved"]["items"] if i["note"])
            self.assertEqual(notes, ["A's note text", "B's query note"], label)
        self.read(self.B, ["saved"])
        self.save(self.A, {"type": "excerpt", "title": "Second clip", "note": "A again",
                           "target": {"ref": self.ref("r3")}})
        after = self.read(self.B, ["saved"])["saved"]
        self.assertIn("Second clip", [i["title"] for i in after["items"]])
        self.assertEqual(after, self.fresh_read(self.B, ["saved"])["saved"])

    def test_f3_saved_revision_and_cursor_stay_per_caller(self):
        def page(ctx, cursor=None):
            return services.customer_saved_items(ctx, self.con, MEMBER, None, "", cursor, 1)
        for label, cached in (("cached", True), ("fresh", False)):
            with contextlib.nullcontext() if cached else fresh():
                first_a, first_b = page(self.A), page(self.B)
                with fresh():
                    plain_a = page(self.A)
                self.assertEqual(first_a["collection_revision"], plain_a["collection_revision"], label)
                self.assertNotEqual(first_a["collection_revision"], first_b["collection_revision"],
                                    label)
                second = page(self.A, first_a["next_cursor"])
                self.assertEqual(len(second["items"]), 1, label)
                self.assertNotEqual(second["items"][0]["id"], first_a["items"][0]["id"], label)
                with self.assertRaisesRegex(Exception, "saved collection changed", msg=label):
                    page(self.B, first_a["next_cursor"])

    def test_f4_read_marks_are_each_callers_own_and_never_stale(self):
        """READ-PP: A's mark is A's only; B's cached read is B's own."""
        def unread(ctx):
            body = self.read(ctx, ["overview", "alerts"])
            total = next(t for t in body["overview"]["alert_totals"] if t["alert_id"] == self.aid)
            alert = next(a for a in body["alerts"]["alerts"] if a["id"] == self.aid)
            return ((total["unread"], total["unread_ads"], total["new_unread_ads"]),
                    (alert["unread_event_count"], alert["unread_ad_count"]), body)
        before_b = unread(self.B)
        self.assertEqual(before_b[0][0], 1)
        self.assertEqual(before_b[1][0], 1)
        events = [e["id"] for e in customer.get_alert(self.con, self.aid)["events"]
                  if not e["read_state"]]
        services.customer_alert_read(self.A, self.con, OWN, self.aid, events)
        self.con.commit()
        after_b, after_a = unread(self.B), unread(self.A)
        self.assertEqual(after_b[0], (1, 1, 1))
        self.assertEqual(after_b[1], (1, 1))
        self.assertEqual(after_a[0], (0, 0, 0))
        self.assertEqual(after_a[1], (0, 0))
        self.assertEqual(after_b[2], self.fresh_read(self.B, ["overview", "alerts"]))
        self.assertEqual(after_a[2], self.fresh_read(self.A, ["overview", "alerts"]))

    @staticmethod
    def shared(name, body):
        """The body without its per-caller fields: saved items' (PER_USER)
        and Overview's activity feed, read uncached for each caller
        (PRIVACY-FEED: a mistake report's save shows to its actor only)."""
        out = without(name, body)
        return {k: v for k, v in out.items() if k != "activity"} if name == "overview" else out

    def test_f5_the_class_compute_is_principal_free(self):
        class_ctx = session("class-free")
        class_ctx.principal_id = class_ctx.effective_principal = "class:editor-test"
        theirs, mine = self.read(class_ctx), self.read(self.A)
        with fresh():
            plain = self.read(class_ctx)
        for name in mine:
            self.assertEqual(self.shared(name, theirs[name]), self.shared(name, mine[name]),
                             name)
            self.assertEqual(theirs[name], plain[name], name)

    def test_f5_cached_equals_fresh_over_random_callers_and_plans(self):
        rng = random.Random(20261025)
        for round_no in range(2):
            callers = [session(f"rand-{rng.randrange(10**9)}", rng.choice(["owner", "editor",
                                                                             "viewer"]))
                       for _ in range(4)]
            for ctx in callers:
                cached, again = self.read(ctx), self.read(ctx)
                ctx_fresh = session(ctx.principal_id[len("oidc:https://issuer.example:"):],
                                    ctx.principal_kind)
                plain = self.fresh_read(ctx_fresh)
                self.assertEqual(cached, plain, f"round {round_no} {ctx.principal_kind}")
                self.assertEqual(again, plain)
                # The same disclosures are registered on the caller's context.
                self.assertEqual(ctx.disclosures, ctx_fresh.disclosures)
            if round_no == 0:
                ClassIsolationTest._plan_a(self)

    def test_f6_saved_revision_is_the_callers_own(self):
        for ctx in (self.A, self.B, self.viewer):
            body = self.read(ctx, ["saved"])["saved"]
            want = citations._state_digest("saved", services.customer_view_context(ctx, MEMBER),
                                           unmarked(body["items"]))
            self.assertEqual(body["collection_revision"], want)

    def test_f9_no_cached_body_names_a_principal_session_or_actor(self):
        ctxs = (self.owner, self.A, self.B, self.viewer)
        for ctx in ctxs:
            self.read(ctx)
        needles = set()
        for ctx in ctxs:
            needles |= {ctx.principal_id, ctx.session_id, services._customer_actor(ctx),
                        services.customer_view_context(ctx, None)}
        needles |= {"editor-a", "editor-b", "viewer-v", "owner-o", "class:", "issuer.example"}
        for ctx in ctxs:
            for name, body in self.read(ctx).items():
                blob = text(body)
                for needle in needles:
                    self.assertNotIn(needle, blob, f"{ctx.principal_kind} {name}")


    def test_f10_can_change_is_each_callers_own_on_a_shared_read(self):
        """FR-11 on the cache: A and B share one editor-class read, yet each
        is told only its own items may change; the owner may change all, the
        viewer none. The alert was made by the local seed ("owner"), so no
        member made it. In every order, cached and fresh."""
        made = {self.A.principal_id: {"Acme", "Clip"}, self.B.principal_id: {"Beta", "Beta ads"}}

        def check(label, ctx, body):
            mine = made.get(ctx.principal_id, set())
            is_owner = ctx is self.owner
            for name in ("saved", "saved_bookmarks"):
                got = {i["title"]: i["can_change"] for i in body[name]["items"]}
                self.assertEqual(got, {t: is_owner or t in mine for t in got},
                                 f"{label}: {name} {ctx.principal_id}")
                # RB-R1-01: added_by_me is each caller's own too (the owner added none).
                got = {i["title"]: i["added_by_me"] for i in body[name]["items"]}
                self.assertEqual(got, {t: t in mine for t in got},
                                 f"{label}: {name} added_by_me {ctx.principal_id}")
            got = {a["id"]: a["can_change"] for a in body["alerts"]["alerts"]}
            self.assertEqual(got, {self.aid: is_owner}, f"{label}: alerts {ctx.principal_id}")
        for label, out in self._orders():
            for ctx in (self.A, self.B):
                check(label, ctx, out[ctx.principal_id])
        customer_query._VIEW_MEMO.clear()
        bodies = [(ctx, self.read(ctx)) for ctx in (self.A, self.owner, self.B, self.viewer, self.A)]
        for ctx, body in bodies:   # checked after every read: no later read re-marks
            check("one shared memo", ctx, body)


class OverviewDayTest(unittest.TestCase):
    """F7: a Tracking placement ending today is pending until midnight UTC;
    a cached Overview recomputes when the day changes."""

    def setUp(self):
        from tests.test_customer_campaigns import fresh_db, seed_campaign, seed_episode
        from tests.test_uat_cui import _show_of, _window
        customer_query._VIEW_MEMO.clear()
        self.addCleanup(customer_query._VIEW_MEMO.clear)
        self.con = fresh_db()
        self.addCleanup(self.con.close)
        seed_episode(self.con, show="Casefile")
        cid = seed_campaign(self.con)
        self.today = db.utcnow()[:10]
        _window(self.con, cid, _show_of(self.con), date_from="2026-01-01", date_to=self.today)
        self.con.commit()
        # Complete coverage, so only the window decides pending vs not seen.
        covered = {"state": "complete", "missing": [], "failed": [], "episodes": ["ep-1"]}
        patch = mock.patch.object(campaigns, "_placement_coverage", return_value=covered)
        patch.start()
        self.addCleanup(patch.stop)

    def test_f7_the_day_changes_overview(self):
        ctx = session("editor-day")
        before = services.customer_overview(ctx, self.con, None)["campaign_gaps"]
        self.assertEqual([g["outcome"] for g in before], ["pending"])
        tomorrow = (datetime.date.fromisoformat(self.today)
                    + datetime.timedelta(days=1)).isoformat() + "T00:00:01Z"
        with mock.patch.object(db, "utcnow", return_value=tomorrow):
            after = services.customer_overview(ctx, self.con, None)["campaign_gaps"]
            with fresh():
                plain = services.customer_overview(session("editor-day"), self.con,
                                                   None)["campaign_gaps"]
        self.assertEqual([g["outcome"] for g in after], ["no_observation_in_covered_sources"])
        self.assertEqual(after, plain)


class InvalidationTest(ReadCacheBase):
    """Phase 2 (2): each write path, then every cached read equals a fresh one."""

    def check(self, write, changes):
        ctxs = (self.A, self.viewer)
        before = {c.principal_id: self.read(c) for c in ctxs}
        write()
        self.con.commit()
        self.assertCachedEqualsFresh(self.A, self.B, self.viewer)
        after = self.read(self.A)
        for name in changes:
            self.assertNotEqual(after[name], before[self.A.principal_id][name],
                                f"{name} did not change: the write proves nothing")

    def test_import_new_evidence(self):
        def write():
            d1._evidence(self.con, "r9", "e1", 140.0, 160.0, self.tv["cur"], "acme")
            _republish(self.con)
        self.check(write, ["search", "brands", "brand", "summary"])

    def test_review_confirms_an_ad(self):
        def write():
            self.con.execute("INSERT INTO reviews (evidence_id, judgment, revision, updated_at)"
                             " VALUES ('r4', 'ad', 1, ?)", (db.utcnow(),))
            _republish(self.con)
        self.check(write, ["brand"])

    def test_attribution_correction(self):
        def write():
            self.con.execute("UPDATE brand_assocs SET brand='gamma', revision=revision+1"
                             " WHERE evidence_id='r4'")
            _republish(self.con)
        self.check(write, ["brands"])

    def test_plan_change(self):
        self.check(lambda: ClassIsolationTest._plan_a(self), ["search"])

    def test_show_rename(self):
        def write():
            self.con.execute("UPDATE customer_show_identities SET display_title='Shop Renamed'")
        self.check(write, ["shows"])

    def test_campaign_create(self):
        def write():
            created = campaigns.save_customer_campaign(self.con, {"campaign": {
                "name": "Research one", "mode": "research", "status": "active", "brand": "acme",
                "source_query": None, "message": None, "offer": None, "notes": "",
                "requirement_provenance": None}})
            campaigns.create_target(self.con, created["campaign"]["id"], "episode",
                                    episode_id="e1")
        self.check(write, ["overview"])

    def test_saved_item_create_edit_delete(self):
        item = {}

        def create():
            item.update(self.save(self.B, {"type": "excerpt", "title": "B's clip",
                                           "target": {"ref": self.ref("r4")}}))
        self.check(create, ["saved"])

        def edit():
            services.customer_edit_item(self.A, self.con, OWN, item["id"],
                                        {"note": "edited by A"}, item["revision"])
        self.check(edit, ["saved"])

        def delete():
            current = next(i for i in self.read(self.A, ["saved"])["saved"]["items"]
                           if i["id"] == item["id"])
            services.customer_delete_item(self.B, self.con, OWN, item["id"], current["revision"])
        self.check(delete, ["saved"])

    def test_alert_save_lifecycle_check_and_read_mark(self):
        def save():
            services.customer_save_alert(self.A, self.con, OWN, "beta watch",
                                         {"version": 2, "mode": "ads", "brands": ["beta"]})
        self.check(save, ["alerts"])

        def lifecycle():
            revision = next(a for a in self.read(self.A, ["alerts"])["alerts"]["alerts"]
                            if a["id"] == self.aid)["revision"]
            services.customer_alert_lifecycle(self.A, self.con, OWN, self.aid, "paused", revision)
        self.check(lifecycle, ["alerts", "overview"])

        # READ-PP: a read mark is the caller's own, so A's mark changes A's
        # reads (and B's stay B's, cached equal to fresh).
        def read_mark():
            services.customer_alert_read(self.A, self.con, OWN, self.aid, [
                e["id"] for e in customer.get_alert(self.con, self.aid)["events"]])
        self.check(read_mark, ["alerts", "overview"])

    def test_alert_check_records_new_events(self):
        other = customer.save_alert(self.con, "beta watch",
                                    {"version": 2, "mode": "ads", "brands": ["beta"]})["id"]
        self.con.commit()

        def check():
            d1._evidence(self.con, "r10", "e2", 100.0, 130.0, self.tv["tv2"], "beta")
            _republish(self.con)
            services.customer_check_alert(self.A, self.con, OWN, other)
        self.check(check, ["alerts", "overview"])

    def test_another_process_writes_with_raw_sql(self):
        def write():
            other = sqlite3.connect(self.path)
            try:
                other.execute("UPDATE customer_show_identities SET display_title='Elsewhere'")
                other.execute("UPDATE customer_alert_events SET read_state=1")
                other.commit()
            finally:
                other.close()
        self.check(write, ["shows", "alerts"])

    def test_schema_change_stops_reuse_until_repaired(self):
        self.read(self.A)
        self.con.execute("CREATE TABLE unguarded_new (x)")
        self.con.execute("UPDATE customer_show_identities SET display_title='Without guards'")
        self.con.commit()
        self.assertIsNone(db.view_state_key(self.con))
        self.assertCachedEqualsFresh(self.A)
        db.ensure_view_state_guards(self.con)
        self.con.commit()
        self.assertCachedEqualsFresh(self.A, self.viewer)


class TranscriptIndexKeyTest(ReadCacheBase):
    """RC-R1-03: mention and episode searches read the transcript index
    (db.VIEW_STATE_UNREAD), so an index-only write (a rebuild with no
    guarded row) must move their key; ads searches do not read it."""

    def search(self, ctx, mode):
        query = customer_contracts.normalize_query({"version": 2, "mode": mode, "q": "words"})
        return services.customer_search(ctx, self.con, None, query)

    def test_rc_r1_03_an_index_only_write_moves_the_search_key(self):
        from earwitness.app import transcript_index
        # Unindexed transcripts are scanned up to the cap; indexed ones
        # answer exactly, so indexing changes the result.
        with mock.patch.object(customer, "SCAN_SEGMENT_CAP", 1):
            before = {mode: self.search(self.A, mode) for mode in ("mentions", "episodes")}
            self.assertEqual(before["mentions"]["truncation_reason"], "SCAN_CAP")
            state = db.view_state_key(self.con)
            versions = [r[0] for r in self.con.execute("SELECT transcript_version_id FROM episodes")]
            transcript_index.index_versions(self.con, versions)
            self.con.commit()
            self.assertEqual(db.view_state_key(self.con), state)   # index-only
            for mode in ("mentions", "episodes"):
                cached = self.search(self.A, mode)
                with fresh():
                    plain = self.search(self.A, mode)
                self.assertEqual(cached, plain, mode)
                self.assertNotEqual(plain, before[mode], mode)
            self.assertIsNone(plain["truncation_reason"])
            # Another editor of the class gets the same, now remembered, result.
            self.assertEqual(self.search(self.B, "mentions"), self.search(self.A, "mentions"))

    def summary(self, ctx, mode):
        query = customer_contracts.normalize_query({"version": 2, "mode": mode, "q": "words"})
        return services.customer_summary(ctx, self.con, None, query, "week")

    def test_rc_r2_03_an_index_only_write_moves_the_summary_key(self):
        """RC-R2-03: /summary in mention or episode mode reads the index
        too (research_bundle), so its key moves on an index-only write."""
        from earwitness.app import transcript_index
        with mock.patch.object(customer, "SCAN_SEGMENT_CAP", 1):
            before = {mode: self.summary(self.A, mode) for mode in ("mentions", "episodes")}
            self.assertEqual(before["mentions"]["truncation_reason"], "SCAN_CAP")
            state = db.view_state_key(self.con)
            versions = [r[0] for r in self.con.execute("SELECT transcript_version_id FROM episodes")]
            transcript_index.index_versions(self.con, versions)
            self.con.commit()
            self.assertEqual(db.view_state_key(self.con), state)   # index-only
            for mode in ("mentions", "episodes"):
                cached = self.summary(self.A, mode)
                with fresh():
                    plain = self.summary(self.A, mode)
                self.assertEqual(cached, plain, mode)
                self.assertNotEqual(plain, before[mode], mode)
            self.assertIsNone(plain["truncation_reason"])
            self.assertEqual(self.summary(self.B, "mentions"), self.summary(self.A, "mentions"))


class SingleFlightAndBoundTest(ReadCacheBase):
    """Phase 2 (4) and the single-flight rule."""

    def test_six_editors_compute_each_read_once(self):
        editors = [session(f"judge-{n}") for n in range(6)]
        with _Counted(customer, "_overview_core") as overview, \
                _Counted(customer, "_brands_directory") as brands, \
                _Counted(customer_saved, "_saved_projection") as saved:
            for ctx in editors:
                self.read(ctx, ["overview", "brands", "saved_bookmarks"])
        self.assertEqual((overview.calls, brands.calls, saved.calls), (1, 1, 1))

    def test_concurrent_cold_misses_fill_once(self):
        editors = [session(f"rush-{n}") for n in range(4)]
        start = threading.Barrier(len(editors))
        out, errors = {}, []

        def run(ctx):
            con = self.open_reader()
            try:
                start.wait()
                out[ctx.principal_id] = services.customer_overview(ctx, con, None)
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
            finally:
                con.close()
        with _Counted(customer, "_overview_core", delay=0.3) as computed:
            threads = [threading.Thread(target=run, args=(c,)) for c in editors]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(errors, [])
        self.assertEqual(computed.calls, 1)
        self.assertEqual(len({text(v) for v in out.values()}), 1)

    def test_rc_r1_02_a_failed_shared_compute_fails_every_waiter(self):
        """RC-R1-02: when the one shared computation raises, every caller
        waiting on it gets that error (no waiter computes again), nothing
        is remembered, and the next read computes afresh."""
        editors = [session(f"fail-{n}") for n in range(4)]
        start = threading.Barrier(len(editors))
        looked, computes, out, errors = [0], [0], {}, {}
        lock = threading.Lock()
        real_hit, real_core = customer_query._memo_hit, customer._overview_core

        def hit(key, ctx):
            with lock:
                looked[0] += 1
            return real_hit(key, ctx)

        def failing(*args, **kwargs):
            with lock:
                computes[0] += 1
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:   # every caller has looked and waits
                with lock:
                    if looked[0] >= len(editors):
                        break
                time.sleep(0.02)
            time.sleep(0.3)
            raise RuntimeError("shared compute failed")

        def run(ctx):
            con = self.open_reader()
            try:
                start.wait()
                out[ctx.principal_id] = services.customer_overview(ctx, con, None)
            except Exception as exc:  # noqa: BLE001 - checked below
                errors[ctx.principal_id] = exc
            finally:
                con.close()
        with mock.patch.object(customer_query, "_memo_hit", hit), \
                mock.patch.object(customer, "_overview_core", failing):
            threads = [threading.Thread(target=run, args=(c,)) for c in editors]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(out, {})
        self.assertEqual(sorted(errors), sorted(c.principal_id for c in editors))
        for exc in errors.values():
            self.assertIsInstance(exc, RuntimeError)
            self.assertEqual(str(exc), "shared compute failed")
        self.assertEqual(computes[0], 1)
        self.assertFalse([k for k in customer_query._VIEW_MEMO if k[2][0] == ("overview-core",)])
        self.assertEqual(customer_query._INFLIGHT, {})
        with _Counted(customer, "_overview_core") as computed:
            again = services.customer_overview(editors[0], self.con, None)
        self.assertEqual(computed.calls, 1)
        self.assertIs(customer._overview_core, real_core)
        with fresh():
            self.assertEqual(again, services.customer_overview(editors[0], self.con, None))

    def test_search_keeps_the_8_queries_last_used(self):
        """RC-R1-08: Search keeps customer.SEARCH_KEEP entries per class (8,
        20 since SPEED-FIX1); one more query evicts the least recently used
        one, not the oldest stored."""
        keep = customer.SEARCH_KEEP

        def search(n):
            query = customer_contracts.normalize_query(dict(OVERVIEW_SEARCH, q=f"offer{n}"))
            return services.customer_search(self.A, self.con, None, query)
        for n in range(keep):
            search(n)
        search(0)          # used again: query 1 is now the least recently used
        search(keep)       # one query more
        for n, computes in ((0, 0), (keep, 0), (2, 0), (1, 1)):
            with _Counted(customer, "_query") as computed:
                search(n)
            self.assertEqual(computed.calls, computes, f"query {n}")

    def test_bound_holds_under_many_workspaces_and_classes(self):
        from tests.backend_fixtures import make_file_workspace
        cons = [self.con]
        for _ in range(2):
            con = make_file_workspace()
            self.addCleanup(con.close)
            d1.seed_breaks(con)
            cons.append(con)
        for con in cons:
            for n in range(12):
                ctx = session(f"policy-{n}")
                ctx.policy_revision, ctx.policy_identity = n, f"{WS}:{n}"
                services.customer_shows(ctx, con, None)
                services.customer_brands(ctx, con, None)
        classes: dict = {}
        for key in customer_query._VIEW_MEMO:
            digest = customer_query._class_of(key[2])
            if digest is not None:
                classes.setdefault(key[0], set()).add(digest)
        self.assertEqual(len(classes), 3)
        for database, seen in classes.items():
            self.assertLessEqual(len(seen), customer_query._CLASS_LIMIT, database)
        self.assertLessEqual(len(customer_query._VIEW_MEMO), customer_query._VIEW_MEMO_LIMIT)
        # The most recent class is still served from the memo.
        with _Counted(customer, "_customer_shows") as computed:
            services.customer_shows(ctx, cons[-1], None)
        self.assertEqual(computed.calls, 0)


class WarmUpTest(ReadCacheBase):
    """Phase 2 (3): warm-up at start, invalidation and refill after an import."""

    def setUp(self):
        super().setUp()
        from earwitness.app import asgi, connections
        self.data_dir = os.path.dirname(self.path)
        self.store = connections.GrantsStore(os.path.join(self.data_dir, "grants.sqlite3"))
        connections.register_workspace(self.store, WS, self.path, self.data_dir)
        self.warmer = asgi.ReadWarmer(self.store, self.data_dir)
        customer_query._VIEW_MEMO.clear()
        if hasattr(customer_query, "_CLASS_USED"):
            customer_query._CLASS_USED.clear()

    def overview_reads(self, ctx):
        # As a request opens it (the registered path, resolved).
        from earwitness.app import connections
        con, entry = connections.open_registered_db(self.store, WS, self.data_dir, readonly=True)
        try:
            r = routes(con, entry)
            return {name: r[name](ctx) for name in
                    ("overview", "search", "saved_bookmarks", "brands", "shows", "alerts")}
        finally:
            con.close()

    def refilled(self):
        """One warm-up check; the principal kinds of the classes it filled."""
        kinds = []
        real = services.warm_class_reads

        def note(ctx, con, entry):
            kinds.append(ctx.principal_kind)
            return real(ctx, con, entry)
        with mock.patch.object(services, "warm_class_reads", note):
            self.warmer.tick()
        return sorted(kinds)

    def test_start_fills_and_an_import_refills(self):
        """At start every role group is filled (SPEED-R1; before, the editor
        class only); each is refilled after an import."""
        self.assertEqual(self.refilled(), ["editor", "owner", "viewer"])
        self.assertEqual(self.warmer.tick(), [])   # nothing changed: nothing filled
        for kind, computes in (("editor", 0), ("owner", 0), ("viewer", 0)):
            with _Counted(customer, "_overview_core") as overview, \
                    _Counted(customer, "_query") as search, \
                    _Counted(customer_saved, "_saved_projection") as saved:
                warm = self.overview_reads(session(f"first-{kind}", kind))
            self.assertEqual((overview.calls, search.calls, saved.calls), (computes,) * 3, kind)
            with fresh():
                self.assertEqual(warm, self.overview_reads(session(f"first-{kind}", kind)))
        total = warm["search"]["total"]
        d1._evidence(self.con, "r9", "e1", 140.0, 160.0, self.tv["cur"], "acme")
        _republish(self.con)    # an import, as pilot_import commits it
        self.assertEqual(self.refilled(), ["editor", "owner", "viewer"])
        for kind in ("editor", "viewer"):
            with _Counted(customer, "_overview_core") as overview, \
                    _Counted(customer, "_query") as search:
                after = self.overview_reads(session(f"after-import-{kind}", kind))
            self.assertEqual((overview.calls, search.calls), (0, 0), kind)
            self.assertEqual(after["search"]["total"], total + 1)
            with fresh():
                self.assertEqual(after, self.overview_reads(session(f"after-import-{kind}", kind)))

    def test_rc_r1_01_a_write_refills_only_the_classes_in_use(self):
        """RC-R1-01: after one editor tracks a brand only the classes in
        use (a live session read them in the last 30 minutes, RC-R2-02)
        are filled again: the judges' editor class, not the owner and
        viewer classes nobody uses."""
        beta = next(i for i in self.fresh_read(self.B, ["saved"])["saved"]["items"]
                    if i["type"] == "bookmark" and i["target"].get("id") == "beta")
        services.customer_delete_item(self.B, self.con, OWN, beta["id"], beta["revision"])
        self.con.commit()
        # Start: every role group (SPEED-R1).
        self.assertEqual(self.refilled(), ["editor", "owner", "viewer"])
        for n in range(3):
            self.overview_reads(session(f"judge-{n}"))
        tracked = self.save(self.A, {"type": "bookmark", "target": {"kind": "brand", "id": "beta"}})
        self.assertNotEqual(tracked["id"], beta["id"])   # a real write, not the old bookmark
        self.assertEqual(self.refilled(), ["editor"])
        with _Counted(customer, "_overview_core") as overview, \
                _Counted(customer_saved, "_saved_projection") as saved:
            after = self.overview_reads(session("judge-0"))
        self.assertEqual((overview.calls, saved.calls), (0, 0))
        with fresh():
            self.assertEqual(after, self.overview_reads(session("judge-0")))
        # A viewer reads once: the viewer class is in use until its next refill.
        self.overview_reads(session("viewer-1", "viewer"))
        self.overview_reads(session("judge-1"))
        self.note_write("B", 1)
        self.assertEqual(self.refilled(), ["editor", "viewer"])
        # Nobody read since that refill, but both classes were read in the
        # last 30 minutes (RC-R2-02): the next write refills them again,
        # and the next read is a hit that equals a fresh one.
        self.note_write("A", 2)
        self.assertEqual(self.refilled(), ["editor", "viewer"])
        with _Counted(customer, "_overview_core") as overview:
            warm = self.overview_reads(session("judge-2"))
        self.assertEqual(overview.calls, 0)
        with fresh():
            self.assertEqual(warm, self.overview_reads(session("judge-2")))

    def test_rc_r2_02_a_class_stays_in_use_for_30_minutes(self):
        """RC-R2-02: a class is in use while a live session of it read a
        remembered route in the last 30 minutes; a refill does not use the
        mark up. Two data changes with no read between them: the second
        still refills the class, and the first judge back reads warm."""
        now = [5000.0]
        with mock.patch.object(customer_query, "_clock", lambda: now[0]):
            self.warmer.tick()
            self.use("viewer", "editor")
            self.note_write("A", 1)
            self.assertEqual(self.refilled(), ["editor", "viewer"])
            now[0] += 60
            self.note_write("A", 2)    # no read since the refill
            self.assertEqual(self.refilled(), ["editor", "viewer"])
            for kind in ("editor", "viewer"):
                with _Counted(customer, "_overview_core") as overview, \
                        _Counted(customer_saved, "_saved_projection") as saved:
                    back = self.overview_reads(session(f"back-{kind}", kind))
                self.assertEqual((overview.calls, saved.calls), (0, 0), kind)
                with fresh():
                    self.assertEqual(back, self.overview_reads(session(f"back-{kind}", kind)))
            # 30 minutes after its last read the viewer class drops out; the
            # editor class read 10 minutes ago stays.
            now[0] += 20 * 60
            self.use("editor")
            now[0] += 10 * 60 + 1
            self.note_write("B", 3)
            self.assertEqual(self.refilled(), ["editor"])

    def test_rc_r2_04_a_read_during_a_refill_keeps_the_class_in_use(self):
        """RC-R2-04 (the code r2 probe): a judge's read that lands during
        the refill keeps the class in use for the next change."""
        self.warmer.tick()
        self.use("editor")
        self.note_write("A", 1)
        real = services.warm_class_reads

        def fill(ctx, con, entry):
            out = real(ctx, con, entry)
            self.overview_reads(session("judge-during"))   # a live read mid-refill
            return out
        with mock.patch.object(services, "warm_class_reads", fill):
            self.warmer.tick()
        self.note_write("A", 2)
        self.assertEqual(self.refilled(), ["editor"])

    def refill_order(self):
        """One warm-up check; the principal kinds it filled, in order."""
        kinds = []
        real = services.warm_class_reads

        def note(ctx, con, entry):
            kinds.append(ctx.principal_kind)
            return real(ctx, con, entry)
        with mock.patch.object(services, "warm_class_reads", note):
            self.warmer.tick()
        return kinds

    def test_rc_scr_r1_01_the_writers_class_refills_first(self):
        """RC-SCR-R1-01: after a write the writer's class refills first,
        then the other classes in use, most recently used first, even when
        an owner and a viewer read after the writer did."""
        self.warmer.tick()
        self.use("editor", "viewer", "owner")    # owner read last
        self.note_write("A", 1)                  # an editor writes
        self.use("viewer", "owner")              # they reload first
        self.assertEqual(self.refill_order(), ["editor", "owner", "viewer"])
        # An owner's write puts the owner class first; the rest by last use.
        self.save(self.owner, {"type": "query", "title": "owner search",
                               "query": {"version": 2, "mode": "ads", "brands": ["acme"]}})
        self.use("editor")
        self.assertEqual(self.refill_order(), ["owner", "editor", "viewer"])

    def note_write(self, who, n):
        """A saved-item write (a member saving a search), as a judge makes it."""
        self.save(getattr(self, who), {"type": "query", "title": f"{who} search {n}",
                                       "query": {"version": 2, "mode": "ads", "brands": ["beta"]}})

    def test_a_failing_workspace_does_not_stop_the_others(self):
        now = [1000.0]
        self.warmer.clock = lambda: now[0]
        with mock.patch.object(services, "warm_class_reads", side_effect=RuntimeError("boom")):
            self.assertEqual(self.warmer.tick(), [])
        now[0] += 3600   # past any back-off
        self.assertEqual(self.warmer.tick(), [WS])

    def failing(self, kinds, exc):
        """warm_class_reads raising exc for the given class kinds; the kinds tried."""
        tried = []
        real = services.warm_class_reads

        def fill(ctx, con, entry):
            tried.append(ctx.principal_kind)
            if ctx.principal_kind in kinds:
                raise exc
            return real(ctx, con, entry)
        return tried, mock.patch.object(services, "warm_class_reads", fill)

    def use(self, *kinds):
        for kind in kinds:
            self.overview_reads(session(f"user-{kind}", kind))

    def test_rc_r1_04_one_failing_class_does_not_skip_the_others(self):
        self.warmer.tick()
        self.use("viewer", "editor", "owner")
        self.note_write("A", 1)    # the writer's class (editor) is refilled first
        tried, patch = self.failing({"editor"}, RuntimeError("boom"))
        with patch:
            self.warmer.tick()
        self.assertEqual(tried, ["editor", "owner", "viewer"])
        # The classes after the failing one were filled.
        for kind in ("owner", "viewer"):
            with _Counted(customer, "_overview_core") as overview:
                self.overview_reads(session(f"after-{kind}", kind))
            self.assertEqual(overview.calls, 0, kind)

    def test_rc_r1_04_a_gate_refusal_is_nothing_to_warm(self):
        """A class its gate refuses (403, or 402 when billing is on) is not
        retried until the data changes again."""
        from earwitness.app import billing
        now = [1000.0]
        self.warmer.clock = lambda: now[0]
        self.warmer.tick()
        for exc in (access.AuthorizationError("no", 403),
                    billing.EntitlementRequired("customer_overview", "no active entitlement")):
            self.use("viewer", "editor")
            self.note_write("A", len(exc.args[0]))
            tried, patch = self.failing({"viewer"}, exc)
            with patch:
                self.assertEqual(self.warmer.tick(), [WS])
                for _ in range(5):
                    now[0] += 3600
                    self.assertEqual(self.warmer.tick(), [])
            self.assertEqual(tried, ["editor", "viewer"], type(exc).__name__)

    def test_rc_r1_04_failed_retries_back_off(self):
        """A failing class is retried with a growing delay (interval x 2^n,
        capped), not on every check; the next success resets it."""
        now = [1000.0]
        self.warmer.clock = lambda: now[0]
        self.warmer.tick()
        self.use("editor")
        self.note_write("A", 1)
        tried, patch = self.failing({"editor"}, RuntimeError("boom"))
        attempts = []
        with patch:
            for second in range(0, 61, 2):   # a check every 2 s for a minute
                now[0] = 1000.0 + second
                before = len(tried)
                self.warmer.tick()
                if len(tried) > before:
                    attempts.append(second)
        self.assertEqual(attempts, [0, 4, 12, 28, 60])
        self.assertGreater(self.warmer.retry_cap_s, 0)
        now[0] += 3600
        self.assertEqual(self.warmer.tick(), [WS])   # retried, filled; back-off reset
        self.note_write("A", 2)
        self.use("editor")
        now[0] += 2
        self.assertEqual(self.warmer.tick(), [WS])


    def test_rc_r1_05_stop_joins_the_warm_up_thread(self):
        """HostedApp.close() stops the warmer: stop() waits for the thread,
        so no fill runs on after shutdown."""
        entered = threading.Event()

        def slow(workspace_id):
            entered.set()
            time.sleep(0.5)
            return False, False
        with mock.patch.object(self.warmer, "_fill", slow):
            self.warmer.start()
            self.assertTrue(entered.wait(30))
            self.warmer.stop()
            self.assertFalse(self.warmer._thread.is_alive())

    def test_rc_r1_05_test_apps_default_warm_up_off(self):
        from earwitness.app import asgi
        # A config built from an explicit mapping (tests, scripts) has no warm-up
        # unless it asks for one; the server's own environment defaults to 2 s.
        self.assertEqual(asgi.config_from_env({}).read_warm_s, 0.0)
        self.assertEqual(asgi.config_from_env({"EARWITNESS_DATA_DIR": self.data_dir}).read_warm_s,
                         0.0)
        self.assertEqual(asgi.config_from_env({"EARWITNESS_READ_WARM_S": "3"}).read_warm_s, 3.0)
        with mock.patch.dict(os.environ):
            os.environ.pop("EARWITNESS_READ_WARM_S", None)
            self.assertEqual(asgi.config_from_env().read_warm_s, 2.0)
            os.environ["EARWITNESS_READ_WARM_S"] = "0"
            self.assertEqual(asgi.config_from_env().read_warm_s, 0.0)

if __name__ == "__main__":
    unittest.main()
