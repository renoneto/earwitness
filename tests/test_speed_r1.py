"""SPEED-R1: research query and filter values remembered per class; every
role group warmed at start and after a research change; 4 read threads.

Sign-in measured on the live host (7db427a4): POST /research-query took
~1.9 s on every Search open, uncached; the first read of each role group
took 3.3-3.5 s after the warm-up (it filled the editor class only); the
first filter-values call took 2.4 s (remembered per person and view
state, computed on the event loop); one web worker with 2 read threads.

Contract checked here:

1. The research query is computed once per (database, research data
   state, transcript index state, disclosure class, request): a hit for
   a second editor; a miss for each key part (words, sort, page, mode,
   brand, date, summaries, selector, expected revision, rank). Each
   caller gets its own workspace_context, in the answer, its selector
   and every summary cell's selector; a selector of another context is
   refused as before. Concurrent cold reads compute once.
2. Isolation: workspace (another database), role, plan, agent token;
   share guests and model contexts are refused before any lookup.
3. Invalidation: an import, a review, a correction, a withhold (Plan A
   content policy), a hold, raw SQL from another process (the nightly
   window, a reseed) and an index-only rebuild each move the key; a
   saved-item write does not. Cached always equals fresh.
4. Filter values: once per class and research query state (not per
   person), kept across a saved-item write, moved by an index-only
   rebuild; off the event loop.
5. Warm-up: every role group at start and after a research change, with
   Overview, the default Search page query, the filter rail and tracked
   brands' pages; only the classes in use after a saved-item write;
   app.read_warm per group with the role and no ids.
6. read_threads is 4; the export and readiness pools and the in-flight
   caps are unchanged.

Run: PYTHONPATH=. python -m unittest tests.test_speed_r1
"""
from __future__ import annotations

import copy
import io
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from earwitness.app import access, customer, customer_contracts, customer_query, db, services

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tests import test_uat_dec_d1api as d1  # noqa: E402
from tests.test_read_cache import (MEMBER, OVERVIEW_SEARCH, OWN, WS,  # noqa: E402
                                   ClassIsolationTest, ReadCacheBase, _Counted, fresh,
                                   guest, routes, session, text)
from tests.test_uat_d1 import _republish  # noqa: E402

SEARCH_PAGE = {"query": dict(OVERVIEW_SEARCH), "selector": None, "summaries": ["week"],
               "refresh_selector": False}


def body(**changes):
    out = copy.deepcopy(SEARCH_PAGE)
    for key, value in changes.items():
        if key in ("selector", "summaries", "refresh_selector", "expected_view_revision"):
            out[key] = value
        else:
            out["query"][key] = value
    return out


def rq(ctx, con, wire, entry=MEMBER, **kw):
    """The research query route's service, with a fresh copy of the body
    (a request parses its own)."""
    return services.customer_research_query(ctx, con, entry, copy.deepcopy(wire), **kw)


def contexts(answer) -> set:
    """Every workspace_context an answer names."""
    found = {answer["workspace_context"]}
    if isinstance(answer.get("selector"), dict):
        found.add(answer["selector"]["workspace_context"])
    for cells in (answer.get("summary") or {}).values():
        for cell in cells:
            found.add(cell["selector"]["workspace_context"])
    return found


def own_context(answer, ctx, entry=MEMBER):
    """answer with ctx's context in place of the caller's (to compare two
    callers' answers)."""
    mine = services.customer_view_context(ctx, entry)
    return json.loads(text(answer).replace(answer["workspace_context"], mine))


class _Base(ReadCacheBase):

    def setUp(self):
        super().setUp()
        con = self.con
        self.routes.update({
            "rq": lambda c: rq(c, con, SEARCH_PAGE),
            "rq_list_q": lambda c: rq(c, con, body(q="acme")),
            "rq_mentions": lambda c: rq(c, con, body(mode="mentions", q="words")),
            "rq_episodes": lambda c: rq(c, con, body(mode="episodes", q="words")),
            "filter_brands": lambda c: services.customer_filter_values(c, con, MEMBER, "brands"),
            "filter_shows": lambda c: services.customer_filter_values(c, con, MEMBER, "shows"),
        })

    def bundles(self, delay=0.0):
        return _Counted(customer_query, "research_bundle", delay)

    def assertSameAsFresh(self, ctx, wire, entry=MEMBER, con=None):
        con = con or self.con
        cached = rq(ctx, con, wire, entry)
        again = rq(ctx, con, wire, entry)
        with fresh():
            plain = rq(ctx, con, wire, entry)
        self.assertEqual(text(cached), text(plain))
        self.assertEqual(text(again), text(plain))
        return cached


class ResearchQueryCacheTest(_Base):

    def test_a_second_editor_reuses_the_first_editors_computation(self):
        with self.bundles() as computed:
            a = rq(self.A, self.con, SEARCH_PAGE)
            b = rq(self.B, self.con, SEARCH_PAGE)
        self.assertEqual(computed.calls, 1)
        self.assertEqual(contexts(a), {services.customer_view_context(self.A, MEMBER)})
        self.assertEqual(contexts(b), {services.customer_view_context(self.B, MEMBER)})
        self.assertNotEqual(contexts(a), contexts(b))
        self.assertEqual(own_context(a, self.B), b)
        self.assertTrue(a["summary"]["week"], "no summary cell: the selector check proves nothing")
        for ctx in (self.A, self.B, self.viewer, self.owner):
            self.assertSameAsFresh(ctx, SEARCH_PAGE)

    def test_each_key_part_is_its_own_entry(self):
        first = rq(self.A, self.con, SEARCH_PAGE)
        cell = first["summary"]["week"][0]["selector"]
        variants = {
            "words": body(q="acme"), "sort": body(sort="oldest"), "page": body(page=2),
            "mode mentions": body(mode="mentions", q="words"),
            "mode episodes": body(mode="episodes", q="words"),
            "brand": body(brands=["acme"]), "date": body(**{"from": "2026-01-01"}),
            "summaries": body(summaries=[]), "selector": body(selector=cell),
            "expected revision": body(expected_view_revision=first["revision"]),
        }
        seen = {text(first)}
        for name, wire in variants.items():
            # B presents its own selector (another context's is refused).
            wire_b = body(selector=rq(self.B, self.con, SEARCH_PAGE)["summary"]["week"][0][
                "selector"]) if name == "selector" else wire
            with self.bundles() as computed:
                got = rq(self.A, self.con, wire)
                again = rq(self.B, self.con, wire_b)
            self.assertEqual(computed.calls, 1, name)
            self.assertEqual(own_context(got, self.B), again, name)
            self.assertSameAsFresh(self.A, wire)
            seen.add(text(got))
        with self.bundles() as computed:
            ranked = rq(self.A, self.con, SEARCH_PAGE)
            dated = rq(self.A, self.con, SEARCH_PAGE, rank=False)
        self.assertEqual(computed.calls, 1, "rank is in the key")
        with fresh():
            self.assertEqual(dated, rq(self.A, self.con, SEARCH_PAGE, rank=False))
        self.assertEqual(ranked, first)
        # The variants really differ (else the misses prove nothing).
        self.assertGreaterEqual(len(seen), 8)

    def test_a_selector_round_trip_keeps_each_callers_context(self):
        a = rq(self.A, self.con, SEARCH_PAGE)
        cell = a["summary"]["week"][0]["selector"]
        narrowed = self.assertSameAsFresh(self.A, body(selector=cell))
        self.assertEqual(contexts(narrowed), {services.customer_view_context(self.A, MEMBER)})
        refreshed = self.assertSameAsFresh(self.A, body(selector=cell, refresh_selector=True))
        self.assertEqual(refreshed["selector"]["workspace_context"],
                         services.customer_view_context(self.A, MEMBER))
        # B presenting A's selector is refused, cached or fresh, as before.
        for patch in (mock.MagicMock(), fresh()):
            with patch:
                with self.assertRaises(customer_contracts.SelectorUnavailable):
                    rq(self.B, self.con, body(selector=cell))

    def test_a_stale_expected_revision_is_refused_cached_or_fresh(self):
        rq(self.A, self.con, SEARCH_PAGE)
        for patch in (mock.MagicMock(), fresh()):
            with patch:
                with self.assertRaises(customer_contracts.SelectorConflict):
                    rq(self.A, self.con, body(expected_view_revision="stale"))

    def test_concurrent_cold_reads_compute_once(self):
        editors = [session(f"rush-{n}") for n in range(5)]
        start = threading.Barrier(len(editors))
        out, errors = {}, []

        def run(ctx):
            con = self.open_reader()
            try:
                start.wait()
                out[ctx.principal_id] = own_context(rq(ctx, con, SEARCH_PAGE), self.A)
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
            finally:
                con.close()
        with self.bundles(delay=0.3) as computed:
            threads = [threading.Thread(target=run, args=(c,)) for c in editors]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(errors, [])
        self.assertEqual(computed.calls, 1)
        self.assertEqual(len({text(v) for v in out.values()}), 1)

    def test_the_memo_stays_bounded_per_class(self):
        for page in range(1, customer_query.RESEARCH_QUERY_KEEP + 6):
            rq(self.A, self.con, body(page=page))
        kept = [k for k in customer_query._VIEW_MEMO
                if isinstance(k[2][0], tuple) and k[2][0][0] == "research-query"]
        self.assertEqual(len(kept), customer_query.RESEARCH_QUERY_KEEP)


class IsolationTest(_Base):

    def test_roles_never_share(self):
        for order in ((self.viewer, self.A, self.owner), (self.owner, self.A, self.viewer)):
            customer_query._VIEW_MEMO.clear()
            for ctx in order:
                rq(ctx, self.con, SEARCH_PAGE)
            for ctx in order:
                self.assertSameAsFresh(ctx, SEARCH_PAGE)
        revisions = {rq(c, self.con, SEARCH_PAGE)["revision"]
                     for c in (self.viewer, self.A, self.owner)}
        self.assertEqual(len(revisions), 3)

    def test_plans_never_share(self):
        plan_b = rq(self.A, self.con, SEARCH_PAGE)
        ClassIsolationTest._plan_a(self)
        plan_a = self.assertSameAsFresh(self.A, SEARCH_PAGE)
        self.assertTrue(plan_a["result"].get("excerpt_only"))
        self.assertNotIn("excerpt_only", plan_b["result"])
        self.assertSameAsFresh(self.viewer, SEARCH_PAGE)

    def test_workspaces_never_share(self):
        """Another workspace (its own database file) with one more ad: the
        same person, the same request, never the first workspace's answer."""
        other_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other_dir, True)
        other_path = os.path.join(other_dir, "ws2.sqlite3")
        other = sqlite3.connect(other_path)
        self.con.backup(other)
        other.close()
        other = db.open_existing(other_path, readonly=False)
        self.addCleanup(other.close)
        d1._evidence(other, "r9", "e1", 140.0, 160.0, self.tv["cur"], "acme")
        _republish(other)
        other.commit()
        entry2 = {"is_owner": False, "row_namespace": "local", "workspace_id": "ws2"}
        here = rq(self.A, self.con, SEARCH_PAGE)
        there_ctx = session("editor-a", workspace="ws2")
        there = self.assertSameAsFresh(there_ctx, SEARCH_PAGE, entry2, con=other)
        self.assertNotEqual(there["result"]["total"], here["result"]["total"])
        self.assertEqual(text(rq(self.A, self.con, SEARCH_PAGE)), text(here))

    def test_a_copy_registered_as_another_workspace_never_shares(self):
        """A cp -c copy of the same database under another workspace: the
        same data state, a different answer (its revision)."""
        other_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other_dir, True)
        other_path = os.path.join(other_dir, "ws2.sqlite3")
        other = sqlite3.connect(other_path)
        self.con.backup(other)
        other.close()
        other = db.open_existing(other_path, readonly=False)
        self.addCleanup(other.close)
        other.execute("UPDATE meta SET value='ws-copy' WHERE key='workspace_id'")
        other.commit()
        self.assertEqual(customer_query.research_query_state(other),
                         customer_query.research_query_state(self.con))
        entry2 = {"is_owner": False, "row_namespace": "local", "workspace_id": "ws2"}
        there_ctx = session("editor-a", workspace="ws2")
        here = rq(self.A, self.con, SEARCH_PAGE)
        there = self.assertSameAsFresh(there_ctx, SEARCH_PAGE, entry2, con=other)
        self.assertNotEqual(there["revision"], here["revision"])
        self.assertSameAsFresh(self.A, SEARCH_PAGE)

    def test_an_agent_token_has_its_own_class(self):
        reader = session("editor-a")
        reader.credential_kind = "reader_bearer"
        reader.scopes = frozenset({"customer:read", "research:read"})
        browser = customer_query.disclosure_class(self.con, self.A)[0]
        token = customer_query.disclosure_class(self.con, reader)[0]
        self.assertNotEqual(browser, token)

    def test_share_guests_and_model_contexts_are_refused_before_any_lookup(self):
        rq(self.A, self.con, SEARCH_PAGE)
        services.customer_filter_values(self.A, self.con, MEMBER, "brands")
        model = session("editor-a")
        model.credential_kind = "model_session"
        before = list(customer_query._VIEW_MEMO)
        with _Counted(customer_query, "_memoized") as looked:
            for ctx in (guest(), model):
                for call in (lambda c: rq(c, self.con, SEARCH_PAGE),
                             lambda c: services.customer_filter_values(c, self.con, MEMBER,
                                                                      "brands")):
                    with self.assertRaises(access.AuthorizationError) as caught:
                        call(ctx)
                    self.assertEqual(caught.exception.status, 403)
        self.assertEqual(looked.calls, 0)
        self.assertEqual(list(customer_query._VIEW_MEMO), before)


class InvalidationTest(_Base):
    """Each write that changes a result moves the key; the value it moves
    is named in docs/hosting/read-cache.md (SPEED-R1)."""

    NAMES = ("rq", "rq_list_q", "rq_mentions", "rq_episodes", "filter_brands", "filter_shows")

    def check(self, write, changes, moves="research"):
        before = {c.principal_id: self.read(c, self.NAMES) for c in (self.A, self.viewer)}
        state = (customer_query.research_view_state(self.con),
                 customer_query._transcript_index_state(self.con))
        write()
        self.con.commit()
        after_state = (customer_query.research_view_state(self.con),
                       customer_query._transcript_index_state(self.con))
        if moves == "research":
            self.assertNotEqual(after_state[0], state[0])
        elif moves == "index":
            self.assertEqual(after_state[0], state[0])
            self.assertNotEqual(after_state[1], state[1])
        for ctx in (self.A, self.B, self.viewer):
            cached = self.read(ctx, self.NAMES)
            plain = self.fresh_read(ctx, self.NAMES)
            for name in self.NAMES:
                self.assertEqual(text(cached[name]), text(plain[name]), name)
        after = self.read(self.A, self.NAMES)
        for name in changes:
            self.assertNotEqual(text(after[name]), text(before[self.A.principal_id][name]),
                                f"{name} did not change: the write proves nothing")

    def test_import(self):
        def write():
            d1._evidence(self.con, "r9", "e1", 140.0, 160.0, self.tv["cur"], "acme")
            _republish(self.con)
        self.check(write, ["rq", "rq_list_q"])

    def test_review(self):
        def write():
            self.con.execute("INSERT INTO reviews (evidence_id, judgment, revision, updated_at)"
                             " VALUES ('r4', 'not_ad', 1, ?)", (db.utcnow(),))
            _republish(self.con)
        self.check(write, ["rq"])

    def test_correction(self):
        def write():
            self.con.execute("UPDATE brand_assocs SET brand='gamma', revision=revision+1"
                             " WHERE evidence_id='r4'")
            _republish(self.con)
        self.check(write, ["rq", "filter_brands"])

    def test_withhold(self):
        """A content policy that withholds transcripts (Plan A)."""
        self.check(lambda: ClassIsolationTest._plan_a(self), ["rq", "rq_mentions"], moves=None)

    def test_a_show_title_change(self):
        def write():
            self.con.execute("UPDATE episodes SET show_title='Held Elsewhere' WHERE id='e1'")
            _republish(self.con)
        self.check(write, ["rq", "filter_shows"])

    def test_another_process_the_nightly_window_or_a_reseed(self):
        def write():
            other = sqlite3.connect(self.path)
            try:
                other.execute("UPDATE brand_assocs SET brand='delta', revision=revision+1"
                              " WHERE evidence_id='r4'")
                other.commit()
            finally:
                other.close()
        self.check(write, ["rq", "filter_brands"])

    def test_an_index_only_rebuild(self):
        from earwitness.app import transcript_index
        with mock.patch.object(customer, "SCAN_SEGMENT_CAP", 1):
            def write():
                versions = [r[0] for r in self.con.execute(
                    "SELECT transcript_version_id FROM episodes")]
                transcript_index.index_versions(self.con, versions)
            self.check(write, ["rq_mentions"], moves="index")

    def test_a_saved_item_write_keeps_every_entry(self):
        for ctx in (self.A, self.viewer):
            self.read(ctx, self.NAMES)
        with self.bundles() as computed, \
                _Counted(customer_query, "_filter_value_entries") as values:
            self.save(self.B, {"type": "query", "title": "B search 2",
                               "query": {"version": 2, "mode": "ads", "brands": ["acme"]}})
            for ctx in (self.A, self.B, self.viewer):
                self.read(ctx, self.NAMES)
        self.assertEqual((computed.calls, values.calls), (0, 0))


class FilterValuesTest(_Base):

    def test_once_per_class_not_per_person(self):
        with _Counted(customer_query, "_filter_value_entries") as computed:
            for ctx in (self.A, self.B, session("editor-c")):
                for field in ("brands", "shows", "categories"):
                    services.customer_filter_values(ctx, self.con, MEMBER, field)
        self.assertEqual(computed.calls, 1)
        for ctx in (self.A, self.viewer, self.owner):
            for field in ("brands", "shows", "categories"):
                cached = services.customer_filter_values(ctx, self.con, MEMBER, field)
                with fresh():
                    plain = services.customer_filter_values(ctx, self.con, MEMBER, field)
                self.assertEqual(cached, plain, field)

    def test_an_index_only_rebuild_moves_the_key(self):
        """The transcript index is outside the data state (db.VIEW_STATE_UNREAD)
        and the episodes match reads it: an index-only rebuild computes the
        values again, and they equal a fresh read."""
        from earwitness.app import transcript_index
        services.customer_filter_values(self.A, self.con, MEMBER, "brands")
        state = customer_query.research_view_state(self.con)
        versions = [r[0] for r in self.con.execute("SELECT transcript_version_id FROM episodes")]
        transcript_index.index_versions(self.con, versions)
        self.con.commit()
        self.assertEqual(customer_query.research_view_state(self.con), state)
        with _Counted(customer_query, "_filter_value_entries") as computed:
            cached = services.customer_filter_values(self.B, self.con, MEMBER, "brands")
        self.assertEqual(computed.calls, 1)
        with fresh():
            self.assertEqual(cached, services.customer_filter_values(self.B, self.con, MEMBER, "brands"))

    def test_a_cursor_still_pages(self):
        first = customer_query.filter_values(self.con, "brands", limit=1, ctx=self.A)
        self.assertTrue(first["next_cursor"])
        second = customer_query.filter_values(self.con, "brands", cursor=first["next_cursor"],
                                              limit=1, ctx=self.B)
        with fresh():
            plain = customer_query.filter_values(self.con, "brands",
                                                 cursor=first["next_cursor"], limit=1, ctx=self.B)
        self.assertEqual(second, plain)

    def test_the_route_runs_off_the_event_loop(self):
        from earwitness.app import asgi
        source = Path(asgi.__file__).read_text()
        route = source[source.index('base == "/filter-values"'):]
        route = route[:route.index("return")]
        self.assertIn("off_loop(_services.customer_filter_values", route)


class _WarmBase(_Base):

    def setUp(self):
        super().setUp()
        from earwitness.app import asgi, connections
        self.data_dir = os.path.dirname(self.path)
        self.store = connections.GrantsStore(os.path.join(self.data_dir, "grants.sqlite3"))
        connections.register_workspace(self.store, WS, self.path, self.data_dir)
        self.warmer = asgi.ReadWarmer(self.store, self.data_dir)
        customer_query._VIEW_MEMO.clear()
        customer_query._CLASS_USED.clear()

    def first_pages(self, ctx):
        from earwitness.app import connections
        con, entry = connections.open_registered_db(self.store, WS, self.data_dir, readonly=True)
        try:
            r = routes(con, entry)
            out = {name: r[name](ctx) for name in
                   ("overview", "search", "saved_bookmarks", "brands", "shows", "alerts")}
            out["rq"] = rq(ctx, con, SEARCH_PAGE, entry)
            for field in ("brands", "shows", "categories"):
                out[field] = services.customer_filter_values(ctx, con, entry, field)
            detail = services.customer_brand_detail(ctx, con, entry, "acme")["brand"]
            ads = detail["drill_down"]["ads"]
            out["brand"] = detail
            out["brand_newest"] = services.customer_search(
                ctx, con, entry, customer_contracts.normalize_query(dict(ads, sort="newest", page=1)))
            out["brand_week"] = services.customer_summary(
                ctx, con, entry, customer_contracts.normalize_query(dict(ads)), "week")
            return out
        finally:
            con.close()

    def counted(self):
        return [_Counted(customer, "_overview_core"), _Counted(customer, "_query"),
                _Counted(customer_query, "research_bundle"),
                _Counted(customer_query, "_filter_value_entries"),
                _Counted(customer, "_brand_detail")]

    def assertWarm(self, kind, name):
        counters = self.counted()
        for c in counters:
            c.__enter__()
        try:
            warm = self.first_pages(session(name, kind))
        finally:
            for c in counters:
                c.__exit__(None, None, None)
        self.assertEqual({c.name: c.calls for c in counters if c.calls}, {}, kind)
        with fresh():
            plain = self.first_pages(session(name, kind))
        self.assertEqual(text(warm), text(plain), kind)
        return warm

    def refilled(self):
        kinds = []
        real = services.warm_class_reads

        def note(ctx, con, entry):
            kinds.append(ctx.principal_kind)
            return real(ctx, con, entry)
        with mock.patch.object(services, "warm_class_reads", note):
            self.warmer.tick()
        return kinds


class WarmTest(_WarmBase):

    def test_start_fills_every_role_group(self):
        self.assertEqual(self.refilled(), ["editor", "owner", "viewer"])
        for kind in ("editor", "owner", "viewer"):
            self.assertWarm(kind, f"first-{kind}")

    def test_a_research_change_refills_every_role_group(self):
        """The nightly window (or a reseed) imports while no one is in use."""
        self.warmer.tick()
        customer_query._CLASS_USED.clear()
        before = self.first_pages(session("before", "viewer"))["rq"]["result"]["total"]
        customer_query._CLASS_USED.clear()
        d1._evidence(self.con, "r9", "e1", 140.0, 160.0, self.tv["cur"], "acme")
        _republish(self.con)
        self.con.commit()
        self.assertEqual(self.refilled(), ["editor", "owner", "viewer"])
        for kind in ("editor", "owner", "viewer"):
            warm = self.assertWarm(kind, f"after-{kind}")
            self.assertEqual(warm["rq"]["result"]["total"], before + 1)

    def test_a_saved_item_write_refills_only_the_classes_in_use(self):
        self.warmer.tick()
        customer_query._CLASS_USED.clear()
        self.first_pages(session("judge-1"))
        self.save(self.A, {"type": "query", "title": "A search",
                           "query": {"version": 2, "mode": "ads", "brands": ["beta"]}})
        self.assertEqual(self.refilled(), ["editor"])

    def test_read_warm_logs_each_group_with_its_role_and_no_ids(self):
        from earwitness.app import oplog
        lines = []
        with mock.patch.object(oplog, "log_event",
                               lambda event, **fields: lines.append((event, fields))):
            self.warmer.tick()
        warm = [fields for event, fields in lines if event == "app.read_warm"]
        self.assertEqual([f["role"] for f in warm], ["editor", "owner", "viewer"])
        tasks = [fields for event, fields in lines if event == "app.read_warm_tasks"]
        self.assertEqual([f["role"] for f in tasks], ["editor", "owner", "viewer"])
        for fields in warm + tasks:
            self.assertEqual(set(fields), {"workspace", "role", "seconds", "cpu_seconds"})
            self.assertEqual(fields["workspace"], WS)

    def test_the_warm_up_never_drops_what_it_filled(self):
        """12 tracked brands: Overview's Search is still remembered after the
        warm-up (the tracked-brand searches fit the class's Search budget)."""
        brands = sum(1 for read in services.FIVE_TASK_READS if "brand" in read)
        self.assertLessEqual(2 * (services.WARM_TRACKED_BRANDS + brands) + 1, customer.SEARCH_KEEP)
        keys = [f"brand-{n}" for n in range(12)]
        many = {"ok": True, "items": [{"type": "bookmark", "target": {"kind": "brand", "id": k}}
                                      for k in keys]}
        real = services.customer_saved_items

        def detail(ctx, con, entry, key):
            return {"brand": {"drill_down": {"ads": dict(OVERVIEW_SEARCH, brands=[key])}}}
        with mock.patch.object(services, "customer_saved_items",
                               lambda *a, **k: many if a[3:] == ("bookmark",) else real(*a, **k)), \
                mock.patch.object(services, "customer_brand_detail", detail):
            self.warmer.tick()
        from earwitness.app import connections
        con, entry = connections.open_registered_db(self.store, WS, self.data_dir, readonly=True)
        try:
            with _Counted(customer, "_query") as search:
                routes(con, entry)["search"](session("judge-after", "editor"))
        finally:
            con.close()
        self.assertEqual(search.calls, 0)

    def test_a_tracked_brand_that_is_gone_does_not_fail_the_class(self):
        """A tracked brand this class no longer sees (its page is the 404)."""
        from earwitness.app import oplog, ops
        lines = []
        with mock.patch.object(oplog, "log_event",
                               lambda event, **fields: lines.append((event, fields))), \
                mock.patch.object(services, "customer_brand_detail",
                                  side_effect=ops.BadInput("unknown brand")) as detail:
            self.warmer.tick()
        self.assertTrue(detail.called)
        self.assertNotIn("app.read_warm_failed", [event for event, _f in lines])


class ReadThreadsTest(unittest.TestCase):

    def test_four_read_threads_and_unchanged_caps(self):
        from earwitness.app import asgi
        app = asgi.HostedApp(asgi.HostedConfig(data_dir=tempfile.mkdtemp()))
        self.addCleanup(app.close)
        for pool in (app.export_pool, app.session_marker_pool, app.read_pool):
            self.addCleanup(pool.shutdown)
        self.assertEqual(app.read_pool._max_workers, 4)
        self.assertEqual(app.export_pool._max_workers, 2)
        self.assertEqual(app.session_marker_pool._max_workers, 2)
        self.assertEqual(asgi.MAX_CONCURRENT_PER_GRANT, 2)
        self.assertEqual(app.limiter._max_grant, 2)
        self.assertEqual(app.limiter._max_ws, 10)



# -- SPEED-FIX1 --------------------------------------------------------------
# Code review r1 (SPC-R1-01..08) and security review r1 (SPS-R1-01..04).

def agent_token(name, scopes, policy=None):
    """A personal agent token of an editor (reader_bearer), built as the
    agent token gate builds it: two of these differ only in the fields
    given here."""
    from earwitness.app import connections
    ctx = access.AccessContext.create(
        principal_id="tok-" + name, principal_kind="editor", workspace_id=WS, scopes=scopes,
        credential_kind="reader_bearer", client_id=connections.PERSONAL_CLIENT_PREFIX + name,
        content_policy=policy or {}, request_id="req-tok-" + name)
    ctx.admitted_role = "editor"
    return ctx


def research_keys():
    return [k for k in customer_query._VIEW_MEMO
            if isinstance(k[2][0], tuple) and str(k[2][0][0]).startswith("research-query")]


class RecordingInputsTest(unittest.TestCase):
    """SPC-R1-01, SPC-R1-08: the research query's answer rests on every
    episode's recording file and retention deadline (its revision), not
    only the shown page's. A change on an episode outside the results
    moves the cached answer, so a chart cell and Export with the revision
    on screen work after one reload."""

    Q = {"version": 2, "mode": "episodes", "q": "Evening"}

    def setUp(self):
        from tests.test_followup_n_view_revision import file_workspace, owner
        customer_query._VIEW_MEMO.clear()
        self.addCleanup(customer_query._VIEW_MEMO.clear)
        self.con, self.recording, self.artifact = file_workspace()
        self.addCleanup(self.con.close)
        self.owner = owner

    def bundle(self, memo=True, selector=None):
        fn = customer_query.research_bundle_memo if memo else customer_query.research_bundle
        with db.transaction(self.con, write=False):
            return fn(self.con, self.Q, selector, ["week"], False, "ctx-abc", None,
                      ctx=self.owner())

    def export(self, revision):
        with db.transaction(self.con, write=False):
            return customer_query.prepare_export(self.con, self.Q, None, "all_matches", "csv",
                                                 revision, "ctx-abc", ctx=self.owner())

    def assertReloadWorks(self, before):
        shown = self.bundle()
        with fresh():
            plain = self.bundle(memo=False)
        self.assertNotEqual(plain["revision"], before["revision"],
                            "the change did not move the revision: the test proves nothing")
        self.assertEqual(text(shown), text(plain))
        self.export(shown["revision"])
        cell = shown["summary"]["week"][0]["selector"]
        self.assertEqual(text(self.bundle(selector=cell)),
                         text(self.bundle(memo=False, selector=cell)))

    def test_a_recording_file_outside_the_results_moves_the_answer(self):
        before = self.bundle()
        shown = {item.get("episode_id") for item in before["result"]["items"]}
        self.assertNotIn("e1", shown, "e1 (the recording) must be outside the results")
        self.assertTrue(before["summary"]["week"], "no chart cell: the cell check proves nothing")
        self.export(before["revision"])
        self.recording.unlink()
        self.assertReloadWorks(before)

    def test_a_retention_deadline_passing_moves_the_answer(self):
        from earwitness.pipeline import artifacts
        self.con.execute("INSERT INTO p01p04_retention VALUES (?, '2999-01-01T00:00:00+00:00')",
                         (self.artifact,))
        before = self.bundle()
        self.assertEqual(text(before), text(self.bundle()))
        # No write at all: only the clock crosses the deadline.
        with mock.patch.object(artifacts, "utcnow", return_value="3000-01-01T00:00:00+00:00"):
            self.assertReloadWorks(before)

    def test_a_nested_memo_passes_its_inputs_up_on_a_hit_and_on_a_fill(self):
        from earwitness.app import media
        con = self.con
        inner = lambda: customer_query._view_revision(con, self.owner())  # noqa: E731
        with db.transaction(con, write=False):
            inner()   # the nested entry exists: the next use is a hit
            for kind in ("hit", "fill"):
                if kind == "fill":
                    customer_query._VIEW_MEMO.clear()
                noted: list = []
                token = media._PROOF_INPUTS.set(noted)
                try:
                    inner()
                finally:
                    media._PROOF_INPUTS.reset(token)
                self.assertIn("file", [entry[0] for entry in noted], kind)
                self.assertIn("clock", [entry[0] for entry in noted], kind)


class WarmScopeTest(_WarmBase):
    """SPS-R1-01 (= SPC-R1-06): every role group refills at start and when
    the research data or the transcript index changed; any other write
    (a campaign, a brief, a comment, a share, an invite) refills only the
    classes in use, as at t (RC-R1-01). The five-task reads fill with
    every role group only."""

    def judge_write(self, write):
        self.warmer.tick()
        customer_query._CLASS_USED.clear()
        self.first_pages(session("judge-1"))
        before = customer_query.research_view_state(self.con)
        write(session("judge-1"))
        self.con.commit()
        self.assertNotEqual(customer_query.research_view_state(self.con), before,
                            "the write did not move the research view state: proves nothing")
        with _Counted(services, "warm_five_task_reads") as tasks:
            kinds = self.refilled()
        return kinds, tasks.calls

    def test_an_editors_campaign_refills_only_the_classes_in_use(self):
        kinds, tasks = self.judge_write(lambda c: services.customer_save_campaign(
            c, self.con, OWN, {"campaign": {"mode": "research", "name": "warm probe"}}))
        self.assertEqual((kinds, tasks), (["editor"], 0))

    def test_an_editors_brief_refills_only_the_classes_in_use(self):
        kinds, tasks = self.judge_write(lambda c: services.customer_save_brief(
            c, self.con, OWN, {"title": "b", "provided_text": "warm probe"}))
        self.assertEqual((kinds, tasks), (["editor"], 0))

    def test_an_index_only_rebuild_refills_every_role_group(self):
        from earwitness.app import transcript_index
        self.warmer.tick()
        customer_query._CLASS_USED.clear()
        versions = [r[0] for r in self.con.execute("SELECT transcript_version_id FROM episodes")]
        transcript_index.index_versions(self.con, versions)
        self.con.commit()
        self.assertEqual(self.refilled(), ["editor", "owner", "viewer"])

    def test_start_fills_the_five_task_reads_of_every_role_group(self):
        tasks = ({"search": {"mode": "ads", "brands": ["acme"]}, "summaries": ["week"]},
                 {"search": {"mode": "ads", "q": "acme"}, "summaries": ["week"]},
                 {"search": {"mode": "ads", "brands": ["acme"]}, "summaries": []},
                 {"brand": "beta"}, {"brand": "no-such-brand"})
        with mock.patch.object(services, "FIVE_TASK_READS", tasks):
            self.warmer.tick()
            for kind in ("editor", "owner", "viewer"):
                ctx = session(f"task-{kind}", kind)
                with self.bundles() as computed, _Counted(customer, "_brand_detail") as brand:
                    got = [rq(ctx, self.con, services.five_task_body(t)) for t in tasks if "search" in t]
                    page = services.customer_brand_detail(ctx, self.con, MEMBER, "beta")
                self.assertEqual((computed.calls, brand.calls), (0, 0), kind)
                with fresh():
                    plain = [rq(ctx, self.con, services.five_task_body(t)) for t in tasks if "search" in t]
                    self.assertEqual(page, services.customer_brand_detail(ctx, self.con, MEMBER, "beta"))
                self.assertEqual(text(got), text(plain), kind)

    def test_the_five_task_list_names_the_five_tasks(self):
        """R/ws-ux/FIVE-TASKS.md: T1 Quince (Brand page, See all in Search,
        typed in Overview's box), T3 NordVPN, Proton VPN and Surfshark, T4
        the Quince alert's Preview matches, T2 BetterHelp; bounded."""
        reads = services.FIVE_TASK_READS
        self.assertLessEqual(len(reads), services.FIVE_TASK_LIMIT)
        brands = {t["brand"] for t in reads if "brand" in t}
        self.assertEqual(brands, {"quince", "nordvpn", "proton vpn", "surfshark", "betterhelp"})
        bodies = [services.five_task_body(t) for t in reads if "search" in t]
        wanted = [({"mode": "ads", "brands": [b]}, ["week"]) for b in sorted(brands)] + [
            ({"mode": "ads", "q": "quince"}, ["week"]), ({"mode": "ads", "brands": ["quince"]}, [])]
        for query, summaries in wanted:
            want = customer_contracts.normalize_query(dict(OVERVIEW_SEARCH, **query))
            self.assertIn((want, summaries), [(customer_contracts.normalize_query(b["query"]),
                                               b["summaries"]) for b in bodies], query)
        for body_ in bodies:   # the browser's own shape (views.js apiBundle)
            self.assertEqual(set(body_), {"query", "selector", "summaries", "refresh_selector"})

    def test_warmed_searches_keep_their_place_when_a_class_runs_many_others(self):
        tasks = ({"search": {"mode": "ads", "brands": ["acme"]}, "summaries": ["week"]},)
        with mock.patch.object(services, "FIVE_TASK_READS", tasks):
            self.warmer.tick()
            for page in range(1, customer_query.RESEARCH_QUERY_KEEP + 4):
                rq(self.A, self.con, body(q="other", page=page))
            with self.bundles() as computed:
                rq(self.A, self.con, SEARCH_PAGE)
                rq(self.A, self.con, services.five_task_body(tasks[0]))
        self.assertEqual(computed.calls, 0)

    def test_the_cap_holds_every_warmed_class(self):
        """SPC-R1-02: _CLASS_LIMIT classes of warm-up and judge reads fit
        the global cap (CLASS_ENTRIES each, measured on DET)."""
        self.assertGreaterEqual(customer_query._VIEW_MEMO_LIMIT,
                                customer_query._CLASS_LIMIT * customer_query.CLASS_ENTRIES)
        self.warmer.tick()
        per_class: dict = {}
        for key in customer_query._VIEW_MEMO:
            digest = customer_query._class_of(key[2])
            if digest is not None and digest != "warm":   # the warm-up's own revisions
                per_class[digest] = per_class.get(digest, 0) + 1
        self.assertEqual(len(per_class), 3)
        self.assertLessEqual(max(per_class.values()), customer_query.CLASS_ENTRIES)
        self.assertLessEqual(len(customer_query._VIEW_MEMO), 3 * customer_query.CLASS_ENTRIES)

    def test_a_busy_class_fits_class_entries(self):
        """SPC-R1-02, SPEED-FIX2 (SPC-C-01): a class with every budget full
        fits CLASS_ENTRIES, so the global cap holds _CLASS_LIMIT of them:
        Search's own and the warm-up's searches (Overview's and each warmed
        Brand page's 2), the research queries' own and the warmed ones,
        brand details, weekly summaries and brand identities (32 each,
        customer.brand_detail, summary and _brand_identity), and its single
        reads (11 on DET with lr-app-nogap-perf merged: two revisions,
        Overview, saved items, Shows, Alerts, filter values, Brands, the ad
        index, Compare, a draft). Measured on DET with real Brand pages:
        135 (logs/speed-r1/fix2/cap-merged.json). SPEED-FIX3 (SC2-01): plus
        the warmed Brand pages' own brand details and weekly summaries
        (1 each per warmed brand, customer.BRAND_WARM_KEEP). SPEED-FIX4
        (SF3-03): the warm budgets counted full (SEARCH_WARM_KEEP,
        BRAND_WARM_KEEP twice), not one fill's requests: tracked-brand
        changes fill them."""
        five = sum(1 for r in services.FIVE_TASK_READS if "brand" in r)
        tracked = services.WARM_TRACKED_BRANDS
        # One fill's requests fit their pass's half of each warm budget.
        self.assertLessEqual(max(1 + 2 * tracked, 2 * five), customer.SEARCH_WARM_KEEP // 2)
        self.assertLessEqual(max(tracked, five), customer.BRAND_WARM_KEEP // 2)
        busy = (customer.SEARCH_KEEP + customer.SEARCH_WARM_KEEP
                + customer_query.RESEARCH_QUERY_KEEP + len(services._pinned_research_tags())
                + 3 * 32 + 2 * customer.BRAND_WARM_KEEP + 11)
        self.assertLessEqual(busy, customer_query.CLASS_ENTRIES)
        self.assertGreaterEqual(customer_query._VIEW_MEMO_LIMIT,
                                customer_query._CLASS_LIMIT * customer_query.CLASS_ENTRIES)


class SelectorKeyTest(_Base):
    """SPS-R1-02 (= SPC-R1-03): the key holds the validated, normalised
    selector; unknown fields are dropped before keying; a request whose
    key is over RESEARCH_QUERY_KEY_LIMIT computes afresh and keeps
    nothing; duplicate summaries are one request."""

    def test_padded_selectors_neither_pin_memory_nor_evict_the_warmed_default(self):
        cell = rq(self.A, self.con, SEARCH_PAGE)["summary"]["week"][0]["selector"]
        with self.bundles() as computed:
            answers = [rq(self.A, self.con, body(selector=dict(cell, padding=str(n) * 200_000)))
                       for n in range(20)]
            rq(self.A, self.con, SEARCH_PAGE)
        self.assertEqual(computed.calls, 1, "one cell request, the default a hit")
        with fresh():
            plain = rq(self.A, self.con, body(selector=cell))
        for answer in answers:
            self.assertEqual(text(answer), text(plain))
        held = sum(len(k[2][0][1]) for k in research_keys())
        self.assertLess(held, 2 * customer_query.RESEARCH_QUERY_KEY_LIMIT)

    def test_an_oversized_request_computes_afresh_and_keeps_nothing(self):
        rq(self.A, self.con, SEARCH_PAGE)
        cell = rq(self.A, self.con, SEARCH_PAGE)["summary"]["week"][0]["selector"]
        big = dict(cell, clause=dict(cell["clause"], key="k" * 100_000))
        before = set(customer_query._VIEW_MEMO)
        for _ in range(3):
            with self.assertRaises(customer_contracts.SelectorConflict):
                rq(self.A, self.con, body(selector=big, refresh_selector=False,
                                          expected_view_revision="x" * 100_000))
            got = rq(self.A, self.con, body(selector=dict(cell, view_revision="v" * 100_000),
                                           refresh_selector=True))
            with fresh():
                self.assertEqual(text(got), text(rq(self.A, self.con, body(
                    selector=dict(cell, view_revision="v" * 100_000), refresh_selector=True))))
        added = set(customer_query._VIEW_MEMO) - before
        # Only the caller's own revision (one per person, as at t): nothing
        # keyed on the request.
        self.assertEqual([k for k in added if k in research_keys()], [])
        for key in added:
            self.assertLess(len(repr(key)), 1000)
        for key in research_keys():
            self.assertLessEqual(len(key[2][0][1]), customer_query.RESEARCH_QUERY_KEY_LIMIT)

    def test_duplicate_summaries_are_one_request(self):
        once = rq(self.A, self.con, SEARCH_PAGE)
        with self.bundles() as computed:
            many = rq(self.A, self.con, body(summaries=["week"] * 50))
        self.assertEqual(computed.calls, 0)
        with fresh():
            self.assertEqual(text(many), text(rq(self.A, self.con, body(summaries=["week"] * 50))))
        self.assertEqual(text(many), text(once))

    def test_a_rejected_selector_is_rejected_as_before(self):
        cell = rq(self.A, self.con, SEARCH_PAGE)["summary"]["week"][0]["selector"]
        for bad in (dict(cell, version=9), dict(cell, clause={"kind": "nope"}),
                    dict(cell, base_query={"version": 2, "nope": 1}), "not a selector"):
            for patch in (mock.MagicMock(), fresh()):
                with patch:
                    with self.assertRaises(customer_contracts.QueryRejected):
                        rq(self.A, self.con, body(selector=bad))


class LateFillTest(_Base):
    """SPC-R1-05: a fill that read an older snapshot (a long warm-up)
    and finishes after a request stored the newer state's entry never
    replaces it."""

    def test_a_late_fill_of_an_older_snapshot_keeps_the_newer_entry(self):
        old = self.open_reader()
        self.addCleanup(old.close)
        with db.transaction(old, write=False):
            customer_query.research_query_state(old)   # the old snapshot starts here
            d1._evidence(self.con, "r9", "e1", 140.0, 160.0, self.tv["cur"], "acme")
            _republish(self.con)
            self.con.commit()
            newer = rq(self.B, self.con, SEARCH_PAGE)
            older = rq(self.A, old, SEARCH_PAGE)          # finishes last
        self.assertNotEqual(older["result"]["total"], newer["result"]["total"])
        with self.bundles() as computed:
            again = rq(self.A, self.con, SEARCH_PAGE)
        self.assertEqual(computed.calls, 0, "the newer entry was replaced by the older one")
        self.assertEqual(own_context(newer, self.A), again)

    def test_the_reviewers_probe(self):
        """tools/probe_race.py of the code review: S2 stored by a request,
        then a fill of S1 (not the database's state) finishes."""
        con = self.open_reader()
        self.addCleanup(con.close)
        database = con.execute("PRAGMA database_list").fetchone()[2]
        identity = (("research-query", "{}"), ("class", "d" * 32))
        new_key, old_key = (database, "S2", identity), (database, "S1", identity)
        customer_query._fill(con, None, database, "S2", new_key, identity,
                             lambda: "answer at S2", 16, None)
        customer_query._fill(con, None, database, "S1", old_key, identity,
                             lambda: "answer at S1", 16, None)
        self.assertIn(new_key, customer_query._VIEW_MEMO)
        self.assertNotIn(old_key, customer_query._VIEW_MEMO)

    def test_a_marked_state_of_the_current_data_still_replaces_an_older_entry(self):
        """A memo state may carry a mark over the data state (a tuple whose
        first item is it: PODS's model-names view); it is the current one."""
        con = self.open_reader()
        self.addCleanup(con.close)
        database = con.execute("PRAGMA database_list").fetchone()[2]
        with db.transaction(con, write=False):
            now = db.view_state_key(con)
        identity = (("marked", "{}"), ("class", "e" * 32))
        old_key, new_key = (database, "OLD", identity), (database, (now, "mark"), identity)
        customer_query._fill(con, None, database, "OLD", old_key, identity, lambda: "old", 16, None)
        customer_query._fill_with(None, con, None, database, (now, "mark"), new_key, identity,
                                  lambda: "new", 16, None)
        self.assertIn(new_key, customer_query._VIEW_MEMO)
        self.assertNotIn(old_key, customer_query._VIEW_MEMO)

    def test_a_fill_of_the_current_state_still_replaces_an_older_entry(self):
        rq(self.A, self.con, SEARCH_PAGE)
        d1._evidence(self.con, "r9", "e1", 140.0, 160.0, self.tv["cur"], "acme")
        _republish(self.con)
        self.con.commit()
        rq(self.A, self.con, SEARCH_PAGE)
        states = {k[1] for k in research_keys()}
        self.assertEqual(states, {customer_query.research_query_state(self.con)})


class TokenIsolationTest(_Base):
    """SPS-R1-03: two agent tokens that differ only in scopes, or only in
    content policy, never share a research query or filter values."""

    WIRES = {"mentions": body(mode="mentions", q="words"),
             "episodes": body(mode="episodes", q="words"), "ads": SEARCH_PAGE}

    def pair(self, first, second):
        for order in ((first, second), (second, first)):
            customer_query._VIEW_MEMO.clear()
            for ctx in order:
                for wire in self.WIRES.values():
                    rq(ctx, self.con, wire)
                for field in ("brands", "shows", "categories"):
                    services.customer_filter_values(ctx, self.con, MEMBER, field)
            for ctx in order:
                for name, wire in self.WIRES.items():
                    self.assertSameAsFresh(ctx, wire)
                for field in ("brands", "shows", "categories"):
                    cached = services.customer_filter_values(ctx, self.con, MEMBER, field)
                    with fresh():
                        plain = services.customer_filter_values(ctx, self.con, MEMBER, field)
                    self.assertEqual(cached, plain, field)
        a, b = (rq(c, self.con, self.WIRES["mentions"]) for c in (first, second))
        self.assertNotEqual(a["result"], b["result"], "the tokens see the same: proves nothing")

    def test_scopes_only(self):
        self.pair(agent_token("a", ("research:read",)),
                  agent_token("b", ("research:read", "transcripts:read")))

    def test_content_policy_only_transcript_deny(self):
        both = ("research:read", "transcripts:read")
        self.pair(agent_token("a", both, {"transcript": "deny"}), agent_token("b", both))

    def test_content_policy_only_excerpt_deny(self):
        both = ("research:read", "transcripts:read")
        self.pair(agent_token("a", both, {"excerpt": "deny"}), agent_token("b", both))

    def test_content_policy_only_a_sources_list(self):
        both = ("research:read", "transcripts:read")
        keys = sorted(r[0] for r in self.con.execute("SELECT DISTINCT source_key FROM source_versions"))
        self.assertGreater(len(keys), 1)
        self.pair(agent_token("a", both, {"sources": keys[: len(keys) // 2]}),
                  agent_token("b", both, {"sources": keys}))



# -- SPEED-FIX2 ---------------------------------------------------------------

def raw_bundle(ctx, con, wire, entry=MEMBER):
    """The research query route answered by research_bundle on the raw
    request (the code before the research-query memo), its error or its
    answer."""
    def plain(c, q, s, summ, refresh, context, repo=None, ctx=None,
              expected_view_revision=None, rank=True, pinned=frozenset()):
        return customer_query.research_bundle(c, q, s, summ, refresh, context, repo, ctx=ctx,
                                              expected_view_revision=expected_view_revision,
                                              rank=rank)
    with mock.patch.object(customer_query, "research_bundle_memo", plain):
        return outcome(lambda: rq(ctx, con, wire, entry))


def outcome(call):
    try:
        return ("ok", text(call()))
    except Exception as exc:  # noqa: BLE001 - compared below
        return ("error", type(exc).__name__, str(exc))


class KnownSelectorFieldTest(_Base):
    """SPC-C-02 + SPS-C-02: every field validate_selector reads reaches the
    remembered answer: a counted selector with each known field set to a
    non-default value answers as research_bundle does on the raw selector,
    cached and again; the selector's view_revision is in the key."""

    def selectors(self):
        cell = rq(self.A, self.con, SEARCH_PAGE)["summary"]["week"][0]["selector"]
        self.assertEqual(cell["clause"]["kind"], "summary_cell")
        live = dict(cell, scope_mode="live_definition", view_revision=None, workspace_context=None)
        return {
            "summary cell": cell,
            "summary cell, occurrence units": dict(cell, count_units="confirmed_occurrences"),
            "confirmed occurrences": dict(cell, clause={"kind": "confirmed_occurrences"},
                                          count_units="confirmed_occurrences"),
            "all results": dict(cell, clause={"kind": "all_results"}),
            "live definition": live,
            "live definition, occurrence units": dict(
                live, clause={"kind": "confirmed_occurrences"}, count_units="confirmed_occurrences"),
            "occurrence clause, result units": dict(cell, clause={"kind": "confirmed_occurrences"}),
            "bad units": dict(cell, count_units="nope"),
            "bad scope": dict(cell, scope_mode="nope"),
            "live with a revision": dict(live, view_revision=cell["view_revision"]),
        }

    def test_each_known_field_answers_as_the_raw_selector_does(self):
        seen = set()
        for name, selector in self.selectors().items():
            for refresh in (False, True):
                wire = body(selector=selector, refresh_selector=refresh)
                plain = raw_bundle(self.A, self.con, wire)
                self.assertEqual(outcome(lambda: rq(self.A, self.con, wire)), plain, name)
                self.assertEqual(outcome(lambda: rq(self.A, self.con, wire)), plain, name + " (hit)")
                seen.add(plain)
        # The fields really change the answer (else equality proves nothing).
        self.assertGreaterEqual(len([s for s in seen if s[0] == "ok"]), 4)
        self.assertGreaterEqual(len([s for s in seen if s[0] == "error"]), 3)

    def test_the_selectors_view_revision_is_in_the_key(self):
        """SPS-C-02 (the security review's guard test): a counted selector
        with an older view_revision is refused after the current
        selector's answer is remembered, cached or fresh, for A and for B
        with B's own context."""
        cell = rq(self.A, self.con, SEARCH_PAGE)["summary"]["week"][0]["selector"]
        current = rq(self.A, self.con, body(selector=cell))          # remembered now
        with fresh():
            self.assertEqual(text(current), text(rq(self.A, self.con, body(selector=cell))))
        stale = dict(cell, view_revision="an-older-revision")
        for patch in (mock.MagicMock(), fresh()):
            with patch:
                with self.assertRaises(customer_contracts.SelectorConflict):
                    rq(self.A, self.con, body(selector=stale))
                with self.assertRaises(customer_contracts.SelectorConflict):
                    rq(self.B, self.con, body(selector=dict(
                        stale, workspace_context=services.customer_view_context(self.B, MEMBER))))
        # Refreshed, the two revisions are two requests (the key names it).
        tags = {customer_query.research_query_tag(
            OVERVIEW_SEARCH, sel, ["week"], True, cell["workspace_context"])[0] for sel in (cell, stale)}
        self.assertEqual(len(tags), 2)


class WarmedBrandPagesTest(_WarmBase):
    """SPC-C-01 (+ SPS-C-01's eviction): the Search entries the warm-up
    fills (Overview's search and each warmed Brand page's newest and
    oldest lists) have their own budget in the class: a judge opening
    other Brand pages (or searching unknown brands) never drops them."""

    TASKS = ({"brand": "acme"}, {"brand": "beta"})

    def brand_page(self, ctx, con, key):
        detail = services.customer_brand_detail(ctx, con, MEMBER, key)["brand"]
        ads = detail["drill_down"]["ads"]
        return [detail] + [services.customer_search(
            ctx, con, MEMBER, customer_contracts.normalize_query(dict(ads, sort=s, page=1)))
            for s in ("newest", "oldest")] + [services.customer_summary(
                ctx, con, MEMBER, customer_contracts.normalize_query(dict(ads)), "week")]

    def test_other_brand_pages_never_drop_the_warmed_ones(self):
        with mock.patch.object(services, "FIVE_TASK_READS", self.TASKS):
            self.warmer.tick()
        judge = session("judge-walk", "editor")
        # 2 x SEARCH_KEEP other Brand pages' lists (unknown brands: each a
        # Search entry, as a real Brand page's newest and oldest are).
        for n in range(customer.SEARCH_KEEP):
            for sort in ("newest", "oldest"):
                services.customer_search(judge, self.con, MEMBER, customer_contracts.normalize_query(
                    dict(OVERVIEW_SEARCH, brands=[f"other-{n}"], sort=sort)))
        counters = [_Counted(customer, "_query"), _Counted(customer, "_brand_detail"),
                    _Counted(customer_query, "research_bundle")]
        for c in counters:
            c.__enter__()
        try:
            pages = [self.brand_page(judge, self.con, t["brand"]) for t in self.TASKS]
            search = routes(self.con)["search"](judge)
        finally:
            for c in counters:
                c.__exit__(None, None, None)
        self.assertEqual({c.name: c.calls for c in counters if c.calls}, {})
        with fresh():
            self.assertEqual(text(pages), text([self.brand_page(judge, self.con, t["brand"])
                                                for t in self.TASKS]))
            self.assertEqual(text(search), text(routes(self.con)["search"](judge)))

    def test_the_warmed_budget_is_bounded_and_only_the_warm_up_fills_it(self):
        """A judge's own searches never land in the warmed budget, and the
        warmed budget never grows past SEARCH_WARM_KEEP per class."""
        with mock.patch.object(services, "FIVE_TASK_READS", self.TASKS):
            self.warmer.tick()
        judge = session("judge-own", "editor")
        for n in range(5):
            services.customer_search(judge, self.con, MEMBER, customer_contracts.normalize_query(
                dict(OVERVIEW_SEARCH, brands=[f"own-{n}"])))
        warmed = [k for k in customer_query._VIEW_MEMO
                  if isinstance(k[2][0], tuple) and k[2][0][0] == "search-warm"]
        self.assertTrue(warmed)
        self.assertNotIn("own-", repr(warmed))
        per_class: dict = {}
        for k in warmed:
            per_class[k[2][1]] = per_class.get(k[2][1], 0) + 1
        self.assertLessEqual(max(per_class.values()), customer.SEARCH_WARM_KEEP)


    def seed_brands(self, count, prefix="other"):
        """count more real brands (one ad each), so each has a Brand page."""
        names = [f"{prefix}-{n:02d}" for n in range(count)]
        for n, brand in enumerate(names):
            d1._evidence(self.con, f"{prefix}{n}", "e1", 140.0 + 0.5 * n, 140.4 + 0.5 * n,
                         self.tv["cur"], brand)
        _republish(self.con)
        self.con.commit()
        return names

    def test_other_real_brand_pages_never_drop_the_warmed_pages(self):
        """SC2-01 (SPEED-FIX3): a warmed Brand page is 4 entries (the brand,
        newest, oldest, weekly summary). After a judge opens 40 other REAL
        Brand pages in the same class (more than the class's 32 brand
        details and 32 weekly summaries, and 2 x SEARCH_KEEP lists), every
        warmed Brand page (the five-task ones and the tracked one) and
        Overview's search are still hits, equal to fresh."""
        others = self.seed_brands(40)
        with mock.patch.object(services, "FIVE_TASK_READS", self.TASKS):
            self.warmer.tick()
        judge = session("judge-walk", "editor")
        for brand in others:
            self.brand_page(judge, self.con, brand)
        digest = customer_query.disclosure_class(self.con, judge)[0]
        own = {}
        for k in customer_query._VIEW_MEMO:
            if isinstance(k[2][0], tuple) and k[2][1] == ("class", digest):
                own[k[2][0][0]] = own.get(k[2][0][0], 0) + 1
        # The walk filled the class's own budgets past their caps.
        self.assertEqual((own.get("brand-detail"), own.get("summary"), own.get("search")),
                         (32, 32, customer.SEARCH_KEEP), own)
        warmed = [t["brand"] for t in self.TASKS] + ["acme"]   # acme: editor A's tracked brand
        counters = [_Counted(customer, "_query"), _Counted(customer, "_brand_detail"),
                    _Counted(customer_query, "research_bundle")]
        for c in counters:
            c.__enter__()
        try:
            pages = [self.brand_page(judge, self.con, key) for key in warmed]
            search = routes(self.con)["search"](judge)
        finally:
            for c in counters:
                c.__exit__(None, None, None)
        self.assertEqual({c.name: c.calls for c in counters if c.calls}, {})
        with fresh():
            self.assertEqual(text(pages), text([self.brand_page(judge, self.con, key)
                                                for key in warmed]))
            self.assertEqual(text(search), text(routes(self.con)["search"](judge)))

    def listed(self, group, ctx=None):
        """Every request the warm-up's lists hold for one group (of ctx's
        class, or of every class)."""
        digest = ctx and customer_query.disclosure_class(self.con, ctx)[0]
        found = set()
        for name, lists in customer_query._WARM_TAGS.items():
            if name[0] == self.path and digest in (None, name[1]):
                for (g, _pass), made in lists.items():
                    if g == group:
                        found.update(made)
        return found

    def own_counts(self, ctx):
        digest = customer_query.disclosure_class(self.con, ctx)[0]
        own: dict = {}
        for k in customer_query._VIEW_MEMO:
            if isinstance(k[2][0], tuple) and k[2][1] == ("class", digest):
                own[k[2][0][0]] = own.get(k[2][0][0], 0) + 1
        return own

    def churn(self, new):
        """A tracked-brand change per brand of new (seeded before the
        warm-up), each a real Track of the brand and
        Untrack of the oldest tracked one (by its maker), each followed by a
        warm-up check (asgi.ReadWarmer.tick): a saved-item write moves no
        research data, so each check is a first-pages fill of the writers'
        class (the 3 oldest tracked brands' pages), never a five-task pass.
        The tracked brands, oldest first, after the last change."""
        tracked = [(item["id"], item["target"]["id"])
                   for item in services.customer_saved_items(self.A, self.con, MEMBER, "bookmark")["items"]
                   if (item.get("target") or {}).get("kind") == "brand"]
        owner = {item_id: self.A if brand == "acme" else self.B for item_id, brand in tracked}
        for n, brand in enumerate(new):
            who = self.A if n % 2 else self.B
            item = self.save(who, {"type": "bookmark", "target": {"kind": "brand", "id": brand}})
            owner[item["id"]] = who
            tracked.append((item["id"], brand))
            oldest = tracked.pop(0)[0]
            revision = self.con.execute("SELECT revision FROM customer_saved_items WHERE id=?",
                                        (oldest,)).fetchone()[0]
            services.customer_delete_item(owner[oldest], self.con, OWN, oldest, revision)
            self.con.commit()
            self.warmer.tick()
            # SF3-02: Overview's search, made by every fill, is moved to
            # newest, so no fill trims it.
            self.assertIn(("search", json.dumps(customer_contracts.normalize_query(
                dict(OVERVIEW_SEARCH)), sort_keys=True)), self.listed("search", self.A), n)
        return [brand for _id, brand in tracked]

    def test_tracked_brand_churn_never_drops_the_five_task_pages(self):
        """SF3-01, SF3-02 (SPEED-FIX4): 12 changes to the 3 oldest tracked
        brands between five-task passes (real Track/Untrack writes, each
        refilled by the warm-up) never take the five-task Brand pages off
        the warm lists, and the requests every fill makes again (Overview's
        search, the tracked brands' pages) stay listed (moved to newest).
        Then a judge's 40 other real Brand pages fill the class's own
        budgets, and every one of those warmed pages is still a hit, equal
        to fresh (the reviewer's churn probe: Quince 13.2 s, NordVPN
        4.8-5.4 s computed in full at 18389b4f)."""
        # Every brand seeded before the warm-up: seeding is a research
        # change, which a Track is not.
        tasks = tuple({"brand": b} for b in self.seed_brands(2, "task"))
        new, others = self.seed_brands(12, "churn"), self.seed_brands(40)
        with mock.patch.object(services, "FIVE_TASK_READS", tasks):
            self.warmer.tick()
            tracked = self.churn(new)
        judge = session("judge-churn", "editor")
        for brand in others:
            self.brand_page(judge, self.con, brand)
        own = self.own_counts(judge)
        self.assertEqual((own.get("brand-detail"), own.get("summary"), own.get("search")),
                         (32, 32, customer.SEARCH_KEEP), own)
        warmed = [t["brand"] for t in tasks] + tracked[:services.WARM_TRACKED_BRANDS]
        counters = [_Counted(customer, "_query"), _Counted(customer, "_brand_detail"),
                    _Counted(customer_query, "research_bundle")]
        for c in counters:
            c.__enter__()
        try:
            pages = [self.brand_page(judge, self.con, key) for key in warmed]
            owner_pages = [self.brand_page(self.owner, self.con, t["brand"]) for t in tasks]
            search = routes(self.con)["search"](judge)
        finally:
            for c in counters:
                c.__exit__(None, None, None)
        self.assertEqual({c.name: c.calls for c in counters if c.calls}, {})
        with fresh():
            self.assertEqual(text(pages), text([self.brand_page(judge, self.con, key)
                                                for key in warmed]))
            self.assertEqual(text(owner_pages), text([self.brand_page(self.owner, self.con, t["brand"])
                                                      for t in tasks]))
            self.assertEqual(text(search), text(routes(self.con)["search"](judge)))
        overview = customer_contracts.normalize_query(dict(OVERVIEW_SEARCH))
        self.assertIn(("search", json.dumps(overview, sort_keys=True)), self.listed("search", judge))
        for key in warmed:
            self.assertIn(("brand-detail", key), self.listed("brand-detail", judge), key)

    def computes(self, read):
        """(value of read(), the Brand-page computes it made)."""
        counters = [_Counted(customer, "_query"), _Counted(customer, "_brand_detail"),
                    _Counted(customer_query, "research_bundle")]
        for c in counters:
            c.__enter__()
        try:
            value = read()
        finally:
            for c in counters:
                c.__exit__(None, None, None)
        return value, {c.name: c.calls for c in counters if c.calls}

    def test_a_trimmed_page_keeps_its_current_entry_not_an_older_one(self):
        """SF4-01 (SPEED-FIX5): a judge opens a Brand page (ordinary
        entries), it is then tracked and warmed, the research data changes
        (refilled at the new state), and it is untracked and trimmed off
        the lists while its older ordinary entries are still in the
        class's budgets. The trim keeps the current (warm) entry, so the
        next open is a hit, equal to fresh."""
        tasks = tuple({"brand": b} for b in self.seed_brands(2, "task"))
        (page,), new = self.seed_brands(1, "page"), self.seed_brands(12, "churn")
        judge = session("judge-state", "editor")
        with mock.patch.object(services, "FIVE_TASK_READS", tasks):
            self.warmer.tick()
            self.brand_page(judge, self.con, page)
            self.churn([page])
            self.assertIn(("brand-detail", page), self.listed("brand-detail", judge))
            self.seed_brands(1, "research-change")
            self.warmer.tick()
            self.churn(new)
        self.assertNotIn(("brand-detail", page), self.listed("brand-detail", judge))
        got, made = self.computes(lambda: self.brand_page(judge, self.con, page))
        self.assertEqual(made, {})
        with fresh():
            self.assertEqual(text(got), text(self.brand_page(judge, self.con, page)))

    def test_a_trim_burst_never_drops_a_judges_recent_pages(self):
        """SF4-02 (SPEED-FIX5): a judge opens Brand pages until the class's
        Search budget is full, then 10 tracked-brand changes with no reads
        between each trim a page off the warm lists. A trimmed request's
        entries go in as the oldest of their ordinary budget, so the
        judge's last 5 Brand pages stay hits (re-check: 4.9-6.4 s each)."""
        tasks = tuple({"brand": b} for b in self.seed_brands(2, "task"))
        mine, new = self.seed_brands(10, "mine"), self.seed_brands(10, "churn")
        judge = session("judge-burst", "editor")
        with mock.patch.object(services, "FIVE_TASK_READS", tasks):
            self.warmer.tick()
            for brand in mine:
                self.brand_page(judge, self.con, brand)
            self.assertEqual(self.own_counts(judge).get("search"), customer.SEARCH_KEEP)
            self.churn(new)
        got, made = self.computes(lambda: [self.brand_page(judge, self.con, b) for b in mine[-5:]])
        self.assertEqual(made, {})
        with fresh():
            self.assertEqual(text(got), text([self.brand_page(judge, self.con, b)
                                              for b in mine[-5:]]))
        self.assertLessEqual(self.own_counts(judge).get("search", 0), customer.SEARCH_KEEP)

    def test_a_read_racing_a_trim_stores_where_the_next_open_looks(self):
        """SF4-03 (SPEED-FIX5): a live read computing a listed request
        while the warm-up trims it off its list (the re-check's hook)
        stores the value in the group's ordinary budget, not the warm
        one: the next open is a hit. SF5-01 (SPEED-FIX6): it stores with
        the ordinary budget (32), not the warm one (12): the judge's 20
        other brand details opened before the race stay hits."""
        others = self.seed_brands(20)
        with mock.patch.object(services, "FIVE_TASK_READS", self.TASKS):
            self.warmer.tick()
        judge = session("judge-race", "editor")
        for brand in others:
            services.customer_brand_detail(judge, self.con, MEMBER, brand)
        digest = customer_query.disclosure_class(self.con, judge)[0]
        target = next(key for key in ("acme", "beta")
                      if ("brand-detail", key) in self.listed("brand-detail", judge))
        twin = (("brand-detail-warm", target), ("class", digest))
        with customer_query._VIEW_MEMO_LOCK:
            for k in [k for k in customer_query._VIEW_MEMO if k[2] == twin]:
                del customer_query._VIEW_MEMO[k]
        real = customer._brand_detail
        fired = []

        def racing(con, key, ctx):
            value = real(con, key, ctx)
            if key == target and not fired:
                fired.append(1)
                for passed in (True, False):
                    token = customer_query.WARM_TASKS.set(passed)
                    try:
                        for n in range(customer.BRAND_WARM_KEEP):
                            customer_query._warm_tag(con, ("brand-detail", f"zz-race-{n}"), digest,
                                                     False, customer.BRAND_WARM_KEEP, 32)
                    finally:
                        customer_query.WARM_TASKS.reset(token)
            return value
        with mock.patch.object(customer, "_brand_detail", racing):
            services.customer_brand_detail(judge, self.con, MEMBER, target)
        self.assertTrue(fired)
        self.assertNotIn(("brand-detail", target), self.listed("brand-detail", judge))
        self.assertEqual([k for k in customer_query._VIEW_MEMO if k[2] == twin], [])
        got, made = self.computes(
            lambda: services.customer_brand_detail(judge, self.con, MEMBER, target))
        self.assertEqual(made, {})
        with fresh():
            self.assertEqual(text(got), text(services.customer_brand_detail(
                judge, self.con, MEMBER, target)))
        self.assertGreater(self.own_counts(judge).get("brand-detail", 0), customer.BRAND_WARM_KEEP)
        got, made = self.computes(lambda: [services.customer_brand_detail(
            judge, self.con, MEMBER, brand) for brand in others])
        self.assertEqual(made, {})
        with fresh():
            self.assertEqual(text(got), text([services.customer_brand_detail(
                judge, self.con, MEMBER, brand) for brand in others]))

    def test_a_reader_waiting_on_a_read_racing_a_trim_takes_its_value(self):
        """SF5-02 (SPEED-FIX6): a second live read of the same request
        (same class, another connection and thread) waits on the racing
        read's single flight under the warm key. The racing read stores
        under the ordinary key (SF4-03); the woken waiter finds it there
        and does not compute again (the re-check's 2-thread probe: two
        _brand_detail calls)."""
        with mock.patch.object(services, "FIVE_TASK_READS", self.TASKS):
            self.warmer.tick()
        judge, judge_b = session("judge-race", "editor"), session("judge-race-b", "editor")
        digest = customer_query.disclosure_class(self.con, judge)[0]
        self.assertEqual(digest, customer_query.disclosure_class(self.con, judge_b)[0])
        target = next(key for key in ("acme", "beta")
                      if ("brand-detail", key) in self.listed("brand-detail", judge))
        twin = (("brand-detail-warm", target), ("class", digest))
        with customer_query._VIEW_MEMO_LOCK:
            for k in [k for k in customer_query._VIEW_MEMO if k[2] == twin]:
                del customer_query._VIEW_MEMO[k]
        real, real_wait = customer._brand_detail, customer_query._Flight.wait
        calls, out, waiting = [], {}, threading.Event()

        def wait(flight, timeout=None):
            waiting.set()
            return real_wait(flight, timeout)

        def read_b():
            con_b = db.open_existing(self.path, readonly=True)
            try:
                out["b"] = services.customer_brand_detail(judge_b, con_b, MEMBER, target)
            except Exception as exc:  # noqa: BLE001
                out["error"] = exc
            finally:
                con_b.close()

        def racing(con, key, ctx):
            if key == target:
                calls.append(threading.current_thread().name)
            if key == target and "thread" not in out:
                out["thread"] = threading.Thread(target=read_b, name="B")
                out["thread"].start()
                self.assertTrue(waiting.wait(30))   # B waits on this read's flight
                value = real(con, key, ctx)
                for passed in (True, False):
                    token = customer_query.WARM_TASKS.set(passed)
                    try:
                        for n in range(customer.BRAND_WARM_KEEP):
                            customer_query._warm_tag(con, ("brand-detail", f"zz-race-{n}"), digest,
                                                     False, customer.BRAND_WARM_KEEP, 32)
                    finally:
                        customer_query.WARM_TASKS.reset(token)
                return value
            return real(con, key, ctx)
        with mock.patch.object(customer, "_brand_detail", racing), \
                mock.patch.object(customer_query._Flight, "wait", wait):
            got = services.customer_brand_detail(judge, self.con, MEMBER, target)
            out["thread"].join(60)
        self.assertNotIn("error", out)
        self.assertNotIn(("brand-detail", target), self.listed("brand-detail", judge))
        self.assertEqual(calls, ["MainThread"])
        self.assertEqual(text(out["b"]), text(got))
        with fresh():
            self.assertEqual(text(got), text(services.customer_brand_detail(
                judge, self.con, MEMBER, target)))

    def test_a_page_trimmed_off_one_list_stays_warm_on_the_other(self):
        """SF4-04 (SPEED-FIX5): a five-task Brand page that is also tracked
        (Quince on DET) and is then untracked and trimmed off the
        first-pages list stays warm through the five-task list: still a
        hit after a judge's 40 other Brand pages fill the class's ordinary
        budgets."""
        tasks = tuple({"brand": b} for b in self.seed_brands(2, "task"))
        both = tasks[0]["brand"]
        new, others = self.seed_brands(12, "churn"), self.seed_brands(40)
        judge = session("judge-both", "editor")
        digest = customer_query.disclosure_class(self.con, judge)[0]
        with mock.patch.object(services, "FIVE_TASK_READS", tasks):
            self.warmer.tick()
            self.churn([both])
            lists = customer_query._WARM_TAGS[(self.path, digest)]
            self.assertIn(("brand-detail", both), lists[("brand-detail", "pages")])
            self.churn(new)
        lists = customer_query._WARM_TAGS[(self.path, digest)]
        self.assertNotIn(("brand-detail", both), lists[("brand-detail", "pages")])
        self.assertIn(("brand-detail", both), lists[("brand-detail", "tasks")])
        for brand in others:
            self.brand_page(judge, self.con, brand)
        got, made = self.computes(lambda: self.brand_page(judge, self.con, both))
        self.assertEqual(made, {})
        with fresh():
            self.assertEqual(text(got), text(self.brand_page(judge, self.con, both)))

    def test_the_warm_lists_keep_at_most_class_limit_classes(self):
        """SF3-01 (SPEED-FIX4): the warm lists are per class, at most
        _CLASS_LIMIT classes per database, the least recently warmed
        dropped with its warm entries; a live read adds no list."""
        limit = customer_query._CLASS_LIMIT
        twin = ("search-warm", "x")
        for n in range(limit + 2):
            customer_query._VIEW_MEMO[(self.path, "s", (twin, ("class", f"d{n}")))] = (
                None, frozenset(), {}, ())
            customer_query._warm_tag(self.con, ("search", "x"), f"d{n}", False, 24, 20)
        self.assertEqual(customer_query._warm_tag(self.con, ("search", "x"), "live", True, 24, 20),
                         (("search", "x"), 20))
        kept = [f"d{n}" for n in range(2, limit + 2)]
        self.assertEqual([name[1] for name in customer_query._WARM_TAGS if name[0] == self.path],
                         kept)
        self.assertEqual(sorted(k[2][1][1] for k in customer_query._VIEW_MEMO
                                if k[0] == self.path and k[2][0] == twin), kept)

    def test_the_warm_request_lists_stay_bounded(self):
        """SC2-04, SF3-01: each warm request list is trimmed to its budget:
        warming more five-task Brand pages than the budgets hold keeps at
        most SEARCH_WARM_KEEP searches and BRAND_WARM_KEEP brand details and
        weekly summaries listed per group, and the warm entries of a
        request trimmed off its list go back to its group's ordinary budget
        (bounded by it), where a judge's next open finds them: a hit, not a
        full compute. Overview's search, made by every first-pages fill,
        stays listed."""
        others = self.seed_brands(16, "many")
        tasks = tuple({"brand": b} for b in others)
        judge = session("judge-late", "editor")
        # The judge's class's own Search budget full first: the trimmed
        # requests' entries that move there must keep it.
        for n in range(customer.SEARCH_KEEP):
            services.customer_search(judge, self.con, MEMBER, customer_contracts.normalize_query(
                dict(OVERVIEW_SEARCH, brands=[f"own-{n}"])))
        with mock.patch.object(services, "FIVE_TASK_READS", tasks), \
                mock.patch.object(services, "FIVE_TASK_LIMIT", len(tasks)):
            self.warmer.tick()
        made = {group: self.listed(group) for group in ("search", "brand-detail", "summary")}
        trimmed = [b for b in others if ("brand-detail", b) not in made["brand-detail"]]
        self.assertTrue(trimmed)
        # The judge's own searches stay hits: the trimmed requests' entries
        # went in as the oldest of the full Search budget and were dropped
        # first (SF4-02). The newest trimmed page's brand detail (its
        # ordinary budget not full) and the newest five-task page: hits,
        # equal to fresh.
        with _Counted(customer, "_query") as own_lists:
            for n in range(customer.SEARCH_KEEP):
                services.customer_search(judge, self.con, MEMBER, customer_contracts.normalize_query(
                    dict(OVERVIEW_SEARCH, brands=[f"own-{n}"])))
        self.assertEqual(own_lists.calls, 0)
        with _Counted(customer, "_brand_detail") as brand, _Counted(customer, "_query") as lists:
            detail = services.customer_brand_detail(judge, self.con, MEMBER, trimmed[-1])
            page = self.brand_page(judge, self.con, others[-1])
        self.assertEqual((brand.calls, lists.calls), (0, 0))
        with fresh():
            self.assertEqual(text([detail, page]), text([
                services.customer_brand_detail(judge, self.con, MEMBER, trimmed[-1]),
                self.brand_page(judge, self.con, others[-1])]))
        self.assertLessEqual(len(made["search"]), customer.SEARCH_WARM_KEEP)
        self.assertLessEqual(len(made["brand-detail"]), customer.BRAND_WARM_KEEP)
        self.assertLessEqual(len(made["summary"]), customer.BRAND_WARM_KEEP)
        overview = customer_contracts.normalize_query(dict(OVERVIEW_SEARCH))
        self.assertIn(("search", json.dumps(overview, sort_keys=True)), made["search"])
        # Nothing of a trimmed request is left in its class's warm budget,
        # and each class's budgets hold.
        mine = {name[1]: lists for name, lists in customer_query._WARM_TAGS.items()
                if name[0] == self.path}
        self.assertEqual(len(mine), 3)
        for k in customer_query._VIEW_MEMO:
            if isinstance(k[2][0], tuple) and k[2][0][0] in ("search-warm", "brand-detail-warm",
                                                             "summary-warm"):
                group = k[2][0][0][:-len("-warm")]
                lists = mine[k[2][1][1]]
                self.assertTrue(any((group,) + k[2][0][1:] in lists.get((group, p), ())
                                    for p in ("tasks", "pages")), k[2][0])
        own = self.own_counts(judge)
        for group, keep in (("search", customer.SEARCH_KEEP), ("brand-detail", 32),
                            ("summary", 32), ("search-warm", customer.SEARCH_WARM_KEEP),
                            ("brand-detail-warm", customer.BRAND_WARM_KEEP),
                            ("summary-warm", customer.BRAND_WARM_KEEP)):
            self.assertLessEqual(own.get(group, 0), keep, group)


class HeldDisclosuresTest(_Base):
    """SC2-05 (SPEED-FIX3): a memo entry holds its disclosure set, which
    on DET is about 2,700 tuples whatever the value. Equal sets are held
    once, and a disclosure tuple once across different sets; a hit still
    replays exactly the disclosures a fresh computation adds."""

    def reads(self, ctx):
        out = [services.customer_brand_detail(ctx, self.con, MEMBER, key) for key in ("acme", "beta")]
        out += [customer.brand_identity(self.con, key, ctx) for key in ("acme", "beta")]
        out.append(rq(ctx, self.con, SEARCH_PAGE))
        out.append(self.routes["overview"](ctx))
        out.append(services.customer_search(ctx, self.con, MEMBER,
                                            customer_contracts.normalize_query(dict(OVERVIEW_SEARCH))))
        return out

    def test_equal_sets_and_equal_tuples_are_held_once(self):
        self.reads(self.A)
        sets = [v[1] for v in customer_query._VIEW_MEMO.values() if v[1]]
        objects: dict = {}
        for held in sets:
            objects.setdefault(held, set()).add(id(held))
        self.assertLess(len(objects), len(sets), "no two entries with equal sets: proves nothing")
        self.assertEqual([len(ids) for ids in objects.values() if len(ids) > 1], [])
        items: dict = {}
        for held in objects:
            for item in held:
                items.setdefault(item, set()).add(id(item))
        self.assertGreater(sum(len(held) for held in objects), len(items),
                           "no tuple in two different sets: proves nothing")
        self.assertEqual([item for item, ids in items.items() if len(ids) > 1], [])

    def test_a_hit_replays_the_fresh_disclosures(self):
        self.reads(session("warm-first"))
        hit = session("judge-hit")
        got = self.reads(hit)
        with fresh():
            plain = session("judge-hit")
            want = self.reads(plain)
        self.assertEqual(text(got), text(want))
        self.assertTrue(plain.disclosures)
        self.assertEqual(hit.disclosures, plain.disclosures)


class SearchKeyBoundTest(_Base):
    """SPS-C-01: a Search request whose key is over SEARCH_KEY_LIMIT (a
    maximal filter of unknown brands, categories and ids) is computed
    afresh and kept nowhere."""

    def test_a_maximal_filter_search_keeps_nothing(self):
        big = customer_contracts.normalize_query(dict(
            OVERVIEW_SEARCH, brands=[f"{n:03d}" + "b" * 196 for n in range(200)],
            categories=[f"{n:03d}" + "c" * 196 for n in range(200)]))
        before = set(customer_query._VIEW_MEMO)
        with _Counted(customer, "_query") as computed:
            got = [services.customer_search(self.A, self.con, MEMBER, dict(big)) for _ in range(3)]
        self.assertEqual(computed.calls, 3)
        added = set(customer_query._VIEW_MEMO) - before
        self.assertEqual([k for k in added if "search" in repr(k[2][0])[:20]], [])
        for key in added:
            self.assertLess(len(repr(key)), customer.SEARCH_KEY_LIMIT)
        with fresh():
            self.assertEqual(text(got[0]), text(services.customer_search(self.A, self.con, MEMBER, dict(big))))

    def test_an_ordinary_search_is_still_remembered(self):
        small = customer_contracts.normalize_query(dict(OVERVIEW_SEARCH, brands=["acme"]))
        with _Counted(customer, "_query") as computed:
            for _ in range(3):
                services.customer_search(self.A, self.con, MEMBER, dict(small))
        self.assertEqual(computed.calls, 1)


class FiveTaskCadenceTest(_WarmBase):
    """SPC-C-03 and SPS-C-04: the five-task pass runs at start and when the
    transcript index changed at once; after any other research data change
    (an owner's ad review, an import) it waits until the research data has
    been quiet for TASKS_QUIET_S (at most TASKS_MAX_WAIT_S), so a burst of
    reviews runs it once; and it fills only role groups the class cut kept."""

    def setUp(self):
        super().setUp()
        self.now = [1000.0]
        self.warmer.clock = lambda: self.now[0]
        self.reviews = 0

    def tick(self):
        kinds, tasks = [], []
        real_first, real_tasks = services.warm_class_reads, services.warm_five_task_reads

        def first(ctx, con, entry):
            kinds.append(ctx.principal_kind)
            return real_first(ctx, con, entry)

        def five(ctx, con, entry):
            tasks.append(ctx.principal_kind)
            return real_tasks(ctx, con, entry)
        with mock.patch.object(services, "warm_class_reads", first), \
                mock.patch.object(services, "warm_five_task_reads", five), \
                mock.patch.object(services, "FIVE_TASK_READS", ({"brand": "acme"},)):
            self.warmer.tick()
        return kinds, tasks

    def review(self):
        """A research row write of the owner-review kind."""
        self.reviews += 1
        d1._evidence(self.con, f"rv{self.reviews}", "e1", 200.0 + 10 * self.reviews,
                     205.0 + 10 * self.reviews, self.tv["cur"], "acme")
        _republish(self.con)
        self.con.commit()

    def test_start_runs_the_five_task_pass_at_once(self):
        self.assertEqual(self.tick(), (["editor", "owner", "viewer"], ["editor", "owner", "viewer"]))
        self.assertEqual(self.tick(), ([], []))

    def test_a_burst_of_reviews_runs_the_five_task_pass_once_when_quiet(self):
        self.tick()
        customer_query._CLASS_USED.clear()
        for _ in range(3):
            self.review()
            self.now[0] += 10
            kinds, tasks = self.tick()
            self.assertEqual((kinds, tasks), (["editor", "owner", "viewer"], []))
        self.now[0] += self.warmer.TASKS_QUIET_S - 1
        self.assertEqual(self.tick(), ([], []))
        self.now[0] += 2
        self.assertEqual(self.tick(), ([], ["editor", "owner", "viewer"]))
        self.now[0] += 1000
        self.assertEqual(self.tick(), ([], []))
        # The deferred pass filled the current state: every five-task read
        # is a hit again, equal to fresh.
        ctx = session("after-burst", "viewer")
        with _Counted(customer, "_brand_detail") as brand:
            page = services.customer_brand_detail(ctx, self.con, MEMBER, "acme")
        self.assertEqual(brand.calls, 0)
        with fresh():
            self.assertEqual(page, services.customer_brand_detail(ctx, self.con, MEMBER, "acme"))

    def test_steady_reviews_run_it_at_most_once_per_max_wait(self):
        self.tick()
        customer_query._CLASS_USED.clear()
        passes = 0
        self.warmer.TASKS_MAX_WAIT_S = 4 * self.warmer.TASKS_QUIET_S   # fewer refills to run
        step = self.warmer.TASKS_QUIET_S / 2
        for _ in range(int(2 * self.warmer.TASKS_MAX_WAIT_S / step)):
            self.review()
            self.now[0] += step
            passes += len(self.tick()[1]) // 3
        self.assertIn(passes, (1, 2))

    def test_an_index_change_runs_it_at_once(self):
        from earwitness.app import transcript_index
        self.tick()
        customer_query._CLASS_USED.clear()
        versions = [r[0] for r in self.con.execute("SELECT transcript_version_id FROM episodes")]
        transcript_index.index_versions(self.con, versions)
        self.con.commit()
        self.assertEqual(self.tick(), (["editor", "owner", "viewer"], ["editor", "owner", "viewer"]))

    def test_without_research_triggers_the_pass_runs_at_start_only(self):
        """SC2-02: when the research state cannot be read (the research
        triggers are missing), the five-task pass runs at start only: a
        later view-state write refills the first pages, never the
        five-task reads, on that tick or any later one."""
        with mock.patch.object(customer_query, "research_data_state", lambda con: None):
            self.assertEqual(self.tick(), (["editor", "owner", "viewer"], ["editor", "owner", "viewer"]))
            customer_query._CLASS_USED.clear()
            self.review()
            self.now[0] += 1
            self.assertEqual(self.tick(), (["editor", "owner", "viewer"], []))
            for _ in range(4):
                self.now[0] += self.warmer.TASKS_MAX_WAIT_S
                self.assertEqual(self.tick(), ([], []))

    def test_the_five_task_pass_fills_only_role_groups_the_cut_kept(self):
        """SPC-C-03: with fewer class slots than role groups, the role
        groups cut from the first pages get no five-task reads either."""
        with mock.patch.object(customer_query, "_CLASS_LIMIT", 2):
            self.assertEqual(self.tick(), (["editor", "owner"], ["editor", "owner"]))


class ScreenFindingsTest(_WarmBase):
    """SPEED-FIX2 step 8 (screen review SPV-C-01..03)."""

    def test_a_utc_day_change_refills_every_role_groups_overview(self):
        """SPV-C-03: Overview keys on the UTC day; at 00:00Z every role
        group refills, not only the classes in use (none overnight)."""
        day1, day2 = "2026-10-29T23:58:00+00:00", "2026-10-30T00:01:00+00:00"
        with mock.patch.object(db, "utcnow", lambda: day1):
            self.warmer.tick()
        customer_query._CLASS_USED.clear()   # nobody read in the last 30 min
        with mock.patch.object(db, "utcnow", lambda: day2):
            with _Counted(services, "warm_five_task_reads") as tasks:
                kinds = self.refilled()
            self.assertEqual((kinds, tasks.calls), (["editor", "owner", "viewer"], 0))
            from earwitness.app import connections
            con, entry = connections.open_registered_db(self.store, WS, self.data_dir, readonly=True)
            try:
                for kind in ("editor", "owner", "viewer"):
                    ctx = session(f"day2-{kind}", kind)
                    with _Counted(customer, "_overview_core") as overview:
                        got = routes(con, entry)["overview"](ctx)
                    self.assertEqual(overview.calls, 0, kind)
                    with fresh():
                        self.assertEqual(text(got), text(routes(con, entry)["overview"](ctx)), kind)
            finally:
                con.close()
            self.assertEqual(self.refilled(), [], "the same day again refills nothing")

    def test_a_recording_check_resolves_each_directory_once(self):
        """SPV-C-01: a hit's check of n recordings resolves the media root
        and each directory once, not 2 x n paths, and reads the same
        states as the full resolve: plain files, a missing file, a
        symlinked file, a symlinked directory, a path that escapes the
        root, '..' and '.' locators."""
        from pathlib import Path
        from earwitness.app import media
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        database = str(root / "workspace.sqlite3")
        Path(database).write_bytes(b"")
        locators = []
        for d in range(4):
            (root / "media" / f"{d:02x}").mkdir(parents=True)
            for n in range(50):
                name = f"media/{d:02x}/{n:04d}.mp3"
                (root / name).write_bytes(b"x" * (n + 1))
                locators.append(name)
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "o.mp3").write_bytes(b"o")
        (root / "media" / "00" / "link.mp3").symlink_to(root / "media" / "01" / "0001.mp3")
        (root / "media" / "out.mp3").symlink_to(outside / "o.mp3")
        (root / "media" / "dirlink").symlink_to(root / "media" / "02")
        odd = ["media/00/link.mp3", "media/out.mp3", "media/dirlink/0003.mp3", "media/00/missing.mp3",
               "media/00/..", "media/00/.", "../escape.mp3", "media/00/../01/0002.mp3", "media/nodir/x.mp3"]
        noted = [("file", database, loc, media._recording_file_state(database, loc))
                 for loc in locators + odd]
        self.assertTrue(media.proof_inputs_unchanged(noted))
        real = Path.resolve
        calls = []

        def counted(self_, *a, **k):
            calls.append(self_)
            return real(self_, *a, **k)
        with mock.patch.object(Path, "resolve", counted):
            self.assertTrue(media.proof_inputs_unchanged(noted[:len(locators)]))
        self.assertLessEqual(len(calls), 1 + 4, "the root once and each directory once")
        # Every change is still seen: a rewritten file, a removed file, a
        # file swapped for a symlink, a directory swapped for a symlink.
        (root / locators[5]).write_bytes(b"changed and longer")
        self.assertFalse(media.proof_inputs_unchanged(noted[:len(locators)]))
        noted = [("file", database, loc, media._recording_file_state(database, loc))
                 for loc in locators + odd]
        (root / locators[7]).unlink()
        (root / locators[7]).symlink_to(outside / "o.mp3")
        self.assertFalse(media.proof_inputs_unchanged(noted[7:8]))
        shutil.move(str(root / "media" / "03"), str(root / "media" / "03-real"))
        (root / "media" / "03").symlink_to(outside)
        self.assertFalse(media.proof_inputs_unchanged([e for e in noted if e[2].startswith("media/03/")][:1]))
        for entry in noted:
            self.assertEqual(media.proof_inputs_unchanged([entry]),
                             media._recording_file_state(database, entry[2]) == entry[3], entry[2])


class PoolTimesTest(unittest.TestCase):

    def test_request_complete_splits_pool_wait_from_run(self):
        """SPV-C-02: each pool call adds its queue wait and its run time to
        the request's request.complete (pool_wait_ms, pool_run_ms)."""
        import asyncio
        import time as _time
        from concurrent.futures import ThreadPoolExecutor
        from earwitness.app import asgi
        pool = ThreadPoolExecutor(1)
        self.addCleanup(pool.shutdown)
        # SPEED-FIX3: the blocker holds the one thread until 0.15 s after
        # the second call is queued (no sleep race under machine load).
        started, release = threading.Event(), threading.Event()

        def hold():
            started.set()
            release.wait(30)

        async def go():
            times = asgi._POOL_TIMES.set(asgi._pool_times())
            try:
                blocker = asyncio.ensure_future(asgi.HostedApp._in_pool(pool, hold))
                while not started.is_set():
                    await asyncio.sleep(0.005)
                second = asyncio.ensure_future(asgi.HostedApp._in_pool(pool, _time.sleep, 0.1))
                await asyncio.sleep(0)   # the second call is queued behind the blocker
                timer = threading.Timer(0.15, release.set)
                timer.start()
                await second
                await blocker
                return asgi._POOL_TIMES.get()
            finally:
                release.set()
                asgi._POOL_TIMES.reset(times)
        got = asyncio.run(go())
        self.assertGreaterEqual(got["wait_s"], 0.15)
        self.assertGreaterEqual(got["run_s"], 0.25)
        self.assertEqual(got["calls"], 2)
        self.assertEqual(set(asgi._pool_fields(got)), {"pool_wait_ms", "pool_run_ms", "pool_calls"})
        self.assertEqual(asgi._pool_fields(asgi._pool_times()), {})


def _limiter_app_base():
    from tests import test_limiter_wait
    return test_limiter_wait._Base


class PoolLogTest(_limiter_app_base()):
    """SPV-C-02 through the app: a read that runs on the read pool logs
    its pool wait and run time on request.complete; one that runs no pool
    work logs none."""

    def request(self, path):
        import asyncio
        from tests import test_limiter_wait
        messages, delivered = [], [False]

        async def receive():
            if not delivered[0]:
                delivered[0] = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.Event().wait()

        async def send(message):
            messages.append(message)
        with self.assertLogs("earwitness.ops", level="INFO") as logs:
            asyncio.run(self.app(test_limiter_wait.AppWaitTest.scope(self, path), receive, send))
        done = [json.loads(line.split(":", 2)[2]) for line in logs.output]
        return [r for r in done if r["event"] == "request.complete"]

    OWNER = {"cookie": "ew_session=sess-k02-1", "x-csrf-token": "tok123",
             "origin": "http://127.0.0.1:8771"}

    def test_a_pool_read_logs_its_wait_and_run(self):
        (overview,) = self.request("/api/v1/customer/overview")
        self.assertEqual(overview["status"], 200)
        self.assertEqual(overview["pool_calls"], 1)
        self.assertGreater(overview["pool_run_ms"], 0)
        self.assertGreaterEqual(overview["pool_wait_ms"], 0)
        (prefs,) = self.request("/api/v1/customer/preferences")
        self.assertNotIn("pool_calls", prefs)


if __name__ == "__main__":
    unittest.main()
