"""PERF-BRAND-ALERT (live QA #3, #4): Brand page and Alert page speed, and
paged alert matches.

On the 1,500-episode sample with the full DEMO2 seed the Brand page (Quince)
took about 11 s and the Alert page about 9 s. Causes (cProfile, logs in
ws-app/logs/perf-brand-alert/):

- Brand page: its three reads (brand detail, Search Ads, the weekly summary)
  each rebuilt the whole-collection ad index (ad_breaks.scope_index,
  1.2-1.4 s each), and nothing was remembered between requests (the brand
  detail and the summary each rescan the brand's ads, 0.7-1.2 s).
- Alert page: get_alert resolved a summary for every event (up to 200; 92
  for Quince, about 3.8 s) and was not remembered between requests.

Contract checked here:

1. One data state builds the whole-scope ad index once for every reader
   (view-state memo); the weekly summary and the brand detail are
   remembered per data state; a write recomputes them.
2. An alert's events are paged: summaries only for one page of
   ALERT_EVENT_SUMMARY_LIMIT events in the page order (unread first, then
   newest), `event_page` says which (page, per_page, total, event_ids);
   every event stays listed (counts unchanged). A page past the last reads
   the last page.
3. get_alert is remembered per data state, alert and page; a write
   (mark read) recomputes it.
4. Equality: remembered and paged results equal the old results on the
   seed fixture, for the owner and for a member (same disclosures).
5. HTTP: GET /customer/alerts/{id}?page=N pages; a bad page is a 400.

Run: PYTHONPATH=. python -m unittest tests.test_perf_brand_alert
"""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

from earwitness.app import ad_breaks, citations, customer, customer_query, db
from earwitness.app.access import AccessContext

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tests import test_uat_dec_d1api as d1  # noqa: E402
from tests.test_uat_d1 import _republish  # noqa: E402

ADS_ACME = {"version": 2, "mode": "ads", "brands": ["acme"]}


def owner():
    return AccessContext.create(principal_id="local-owner", principal_kind="owner",
                                workspace_id="local", credential_kind="local_operator")


def editor():
    return AccessContext.create(principal_id="m1", principal_kind="editor",
                                workspace_id="local", credential_kind="browser_session")


def share_guest():
    return AccessContext.create(principal_id="g1", principal_kind="guest", workspace_id="local",
                                scopes={"research:read"}, authority_kind="share",
                                share_grant_id="s1", credential_kind="browser_session")


class _Counted:
    """Count calls of module.name while still running it."""

    def __init__(self, module, name):
        self.module, self.name = module, name
        self.calls = 0

    def __enter__(self):
        real = getattr(self.module, self.name)

        def wrapper(*args, **kwargs):
            self.calls += 1
            return real(*args, **kwargs)
        self._patch = mock.patch.object(self.module, self.name, wrapper)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False


def _no_memo():
    """Every view-state memo computes afresh (the code before this change
    remembered none of these reads)."""
    return mock.patch.object(customer_query, "_ctx_memo_identity", return_value=None)


class BrandPageReads(d1.SeededBase):
    """The Brand page's reads of brand acme on the d1 seed."""

    maxDiff = None

    def setUp(self):
        super().setUp()
        _republish(self.con)
        customer_query._VIEW_MEMO.clear()
        self.addCleanup(customer_query._VIEW_MEMO.clear)

    def _page_reads(self, ctx=None):
        detail = customer.brand_detail(self.con, "acme", ctx=ctx)
        with db.transaction(self.con, write=False):
            search = customer_query.research_bundle(
                self.con, dict(ADS_ACME), None, [], False, "ctx-test", ctx=ctx)
        week = customer.summary(self.con, dict(ADS_ACME), "week", ctx=ctx)
        return {"detail": detail, "search": search, "week": week}

    def test_one_state_builds_the_whole_scope_ad_index_once(self):
        with _Counted(ad_breaks, "index_of_rows") as built:
            self._page_reads()
            self._page_reads()
        self.assertEqual(built.calls, 1,
                         f"the Brand page reads built the ad index {built.calls} times")

    def test_the_weekly_summary_is_remembered_until_a_write(self):
        first = customer.summary(self.con, dict(ADS_ACME), "week")
        with _Counted(customer_query, "research_bundle") as ran:
            again = customer.summary(self.con, dict(ADS_ACME), "week")
        self.assertEqual(ran.calls, 0, "the second identical summary ran again")
        self.assertEqual(again, first)
        d1._evidence(self.con, "r10", "e1", 300.0, 330.0, self.tv["cur"], "acme")
        _republish(self.con)
        with _Counted(customer_query, "research_bundle") as ran:
            after = customer.summary(self.con, dict(ADS_ACME), "week")
        self.assertEqual(ran.calls, 1, "a write did not recompute the summary")
        self.assertEqual(after["total"], first["total"] + 1)

    def test_the_brand_detail_is_remembered_until_a_write(self):
        first = customer.brand_detail(self.con, "acme")
        with _Counted(customer_query, "_match_query2_light") as ran:
            again = customer.brand_detail(self.con, "acme")
            beta = customer.brand_detail(self.con, "beta")
        self.assertEqual(ran.calls, 1, "the second identical brand detail ran again")
        self.assertEqual(again, first)
        self.assertNotEqual(beta["key"], first["key"])
        d1._evidence(self.con, "r10", "e1", 300.0, 330.0, self.tv["cur"], "acme")
        _republish(self.con)
        after = customer.brand_detail(self.con, "acme")
        self.assertEqual(after["detections"], first["detections"] + 1)

    def test_each_criteria_and_grouping_keeps_its_own_summary(self):
        reads = [(dict(ADS_ACME), "week"), (dict(ADS_ACME), "show"),
                 ({"version": 2, "mode": "ads"}, "week"),
                 ({"version": 2, "mode": "ads", "brands": ["beta"]}, "week")]
        with _no_memo():
            fresh = [customer.summary(self.con, q, g) for q, g in reads]
        self.assertEqual(len({json.dumps(f, sort_keys=True) for f in fresh}), len(reads))
        for _round in range(2):  # computed, then remembered
            got = [customer.summary(self.con, q, g) for q, g in reads]
            self.assertEqual(got, fresh)

    def test_remembered_reads_equal_fresh_reads(self):
        # PBA-R2-04: the default owner, an owner, an editor and a share guest.
        for make in (lambda: None, owner, editor, share_guest):
            with _no_memo():
                ctx = make()
                fresh = self._page_reads(ctx)
                fresh_disclosures = sorted(map(list, ctx.disclosures)) if ctx else None
            customer_query._VIEW_MEMO.clear()
            for _round in range(2):  # computed, then remembered
                ctx = make()
                got = self._page_reads(ctx)
                self.assertEqual(json.dumps(got, sort_keys=True),
                                 json.dumps(fresh, sort_keys=True))
                if ctx is not None:
                    self.assertEqual(sorted(map(list, ctx.disclosures)), fresh_disclosures)


class _AlertEvents(d1.SeededBase):
    """An ads alert on acme with 45 events: its baseline check, then
    history events citing the seed's publications."""

    maxDiff = None
    N = 45

    def setUp(self):
        super().setUp()
        _republish(self.con)
        customer_query._VIEW_MEMO.clear()
        self.addCleanup(customer_query._VIEW_MEMO.clear)
        self.aid = customer.save_alert(self.con, "acme", dict(ADS_ACME))["id"]
        self.con.commit()
        refs = [self.ref(r) for r in ("r1", "r3", "r4", "r5", "r7")]
        for i in range(self.N):
            self.con.execute(
                "INSERT INTO customer_alert_events (workspace_id, alert_id, kind,"
                " logical_identity, after_ref, observed_at, checked_at, read_state)"
                " VALUES ('local', ?, 'new_finding', ?, ?, ?, ?, ?)",
                (self.aid, f"hist:{i}", refs[i % len(refs)],
                 f"2026-10-{1 + i % 20:02d}T00:00:{i:02d}Z", "2026-10-21T00:00:00Z",
                 1 if i % 3 == 0 else 0))
        self.con.commit()


class AlertPaging(_AlertEvents):
    """Paging and remembering get_alert."""

    def _order(self, events):
        # Unread first, then newest first; a tie keeps the lower id first
        # (PBA-R1-07: the order the alert page always drew).
        ordered = sorted(events, key=lambda e: e["id"])
        ordered.sort(key=lambda e: e["observed_at"] or "", reverse=True)
        ordered.sort(key=lambda e: bool(e["read_state"]))
        return [e["id"] for e in ordered]

    def test_tied_events_page_in_id_order(self):
        """PBA-R1-07: events of one check share observed_at; the page
        order breaks the tie by the lower id first, as the alert page
        has always drawn tied rows."""
        self.con.execute("UPDATE customer_alert_events SET observed_at='2026-10-21T00:00:00Z'"
                         " WHERE alert_id=?", (self.aid,))
        self.con.commit()
        dto = customer.get_alert(self.con, self.aid)
        unread = sorted(e["id"] for e in dto["events"] if not e["read_state"])
        self.assertEqual(dto["event_page"]["event_ids"],
                         unread[:customer.ALERT_EVENT_SUMMARY_LIMIT])

    def test_a_page_resolves_only_its_own_summaries(self):
        with _Counted(customer, "_event_summary") as resolved:
            customer.get_alert(self.con, self.aid)
        self.assertLessEqual(resolved.calls, customer.ALERT_EVENT_SUMMARY_LIMIT)
        self.assertLessEqual(customer.ALERT_EVENT_SUMMARY_LIMIT, 25)

    def test_pages_partition_the_events_in_page_order(self):
        size = customer.ALERT_EVENT_SUMMARY_LIMIT
        first = customer.get_alert(self.con, self.aid)
        total = len(first["events"])
        self.assertEqual(total, self.N)
        order = self._order(first["events"])
        pages = -(-total // size)
        seen = []
        for page in range(1, pages + 1):
            dto = customer.get_alert(self.con, self.aid, page=page)
            ep = dto["event_page"]
            self.assertEqual((ep["page"], ep["per_page"], ep["total"]), (page, size, total))
            self.assertEqual(ep["event_ids"], order[(page - 1) * size:page * size])
            self.assertEqual([e["id"] for e in dto["events"]], sorted(order),
                             "every event stays listed, in id order")
            with_summary = {e["id"] for e in dto["events"] if e["summary"] is not None}
            self.assertEqual(with_summary, set(ep["event_ids"]))
            for key in ("unread_event_count", "unread_ad_count", "baseline_count",
                        "current_matches"):
                self.assertEqual(dto[key], first[key], key)
            seen += ep["event_ids"]
        self.assertEqual(seen, order)
        past = customer.get_alert(self.con, self.aid, page=pages + 5)
        self.assertEqual(past["event_page"]["page"], pages)
        self.assertEqual(past["event_page"]["event_ids"], order[(pages - 1) * size:])

    def _ads_of_three(self):
        """Every 3 history events (hist:3k..3k+2) are one ad: 15 ads."""
        real = customer._with_ad_ids

        def with_ads(con, events, ctx=None, index=None):
            out = real(con, events, ctx, index)
            for event in out:
                logical = event["logical_identity"] or ""
                if logical.startswith("hist:"):
                    event["ad_id"] = f"ad{int(logical[5:]) // 3}"
            return out
        return mock.patch.object(customer, "_with_ad_ids", with_ads)

    def test_pages_hold_whole_ads_and_count_changes(self):
        """PBA-R1-02: the page unit is the header's change (one ad, or one
        event with no ad). 45 events in 15 ads: event_page.total is 15,
        no ad splits across pages, and the episode count covers every
        event on every page."""
        with self._ads_of_three(), _no_memo(), \
                mock.patch.object(customer, "ALERT_EVENT_SUMMARY_LIMIT", 200):
            old = customer.get_alert(self.con, self.aid)
        ad_of = {e["id"]: e["ad_id"] for e in old["events"]}
        self.assertEqual(len(set(ad_of.values())), 15)
        episodes = {e["summary"]["episode_id"] for e in old["events"] if e["summary"]}
        self.assertGreater(len(episodes), 1)
        customer_query._VIEW_MEMO.clear()
        with self._ads_of_three():
            one = customer.get_alert(self.con, self.aid)["event_page"]
        self.assertEqual((one["page"], one["total"], len(one["event_ids"])), (1, 15, self.N))
        customer_query._VIEW_MEMO.clear()
        size = 4
        seen, seen_ads = [], set()
        with self._ads_of_three(), mock.patch.object(customer, "ALERT_EVENT_SUMMARY_LIMIT", size):
            for page in range(1, 5):
                dto = customer.get_alert(self.con, self.aid, page=page)
                ep = dto["event_page"]
                self.assertEqual((ep["page"], ep["per_page"], ep["total"]), (page, size, 15))
                ads = {ad_of[i] for i in ep["event_ids"]}
                self.assertEqual(len(ads), min(size, 15 - size * (page - 1)), page)
                self.assertFalse(ads & seen_ads, f"an ad split across pages (page {page})")
                self.assertEqual(set(ep["event_ids"]), {i for i, a in ad_of.items() if a in ads})
                self.assertEqual({e["id"] for e in dto["events"] if e["summary"] is not None},
                                 set(ep["event_ids"]))
                self.assertEqual(ep["episodes"], len(episodes), f"page {page} episode count")
                self.assertIs(ep["grouped"], True)
                seen += ep["event_ids"]
                seen_ads |= ads
        self.assertEqual(sorted(seen), sorted(ad_of))

    def test_event_page_names_the_pages_of_the_latest_check(self):
        """PBA-R1-06: the latest check's matches are read, so the unread
        history fills page 1; event_page.latest_check_pages names the
        pages that hold them (the alert page links the first)."""
        at = customer.get_alert(self.con, self.aid)["last_successful_at"]
        self.con.execute("UPDATE customer_alert_events SET checked_at=? WHERE alert_id=?"
                         " AND read_state=1 AND CAST(substr(logical_identity, 6) AS INTEGER) < 12",
                         (at, self.aid))
        self.con.commit()
        latest = {r[0] for r in self.con.execute(
            "SELECT id FROM customer_alert_events WHERE alert_id=? AND checked_at=?", (self.aid, at))}
        self.assertEqual(len(latest), 4)
        pages = -(-self.N // customer.ALERT_EVENT_SUMMARY_LIMIT)
        eps = [customer.get_alert(self.con, self.aid, page=p)["event_page"]
               for p in range(1, pages + 1)]
        expect = [ep["page"] for ep in eps if latest & set(ep["event_ids"])]
        self.assertNotIn(1, expect)
        for ep in eps:
            self.assertEqual(ep["latest_check_pages"], expect, ep["page"])
        # A check under earlier settings has no latest-check section.
        self.con.execute("UPDATE customer_alerts SET definition_revision=2 WHERE id=?", (self.aid,))
        self.con.commit()
        self.assertEqual(customer.get_alert(self.con, self.aid)["event_page"]["latest_check_pages"], [])

    def test_light_read_lists_every_event_without_a_page(self):
        dto = customer.get_alert(self.con, self.aid, summaries=False)
        self.assertEqual(len(dto["events"]), self.N)
        self.assertNotIn("event_page", dto)
        self.assertTrue(all("summary" not in e for e in dto["events"]))

    def test_get_alert_is_remembered_until_a_write(self):
        first = customer.get_alert(self.con, self.aid, page=2)
        with _Counted(customer, "tracker_dto") as ran:
            again = customer.get_alert(self.con, self.aid, page=2)
        self.assertEqual(ran.calls, 0, "the second identical alert read ran again")
        self.assertEqual(again, first)
        unread = [e["id"] for e in first["events"] if not e["read_state"]][:3]
        customer.mark_alert_events_read(self.con, self.aid, unread)
        self.con.commit()
        with _Counted(customer, "tracker_dto") as ran:
            after = customer.get_alert(self.con, self.aid, page=2)
        self.assertEqual(ran.calls, 1, "a write did not recompute the alert")
        self.assertEqual(after["unread_event_count"], first["unread_event_count"] - 3)

    def test_a_cold_read_resolves_only_its_page(self):
        """PBA-R2-01: the episode count over every event reads the source
        rows tracker_dto already authorized; only the page's events fully
        resolve a publication."""
        with _Counted(citations, "_resolve_publication") as light:
            customer.get_alert(self.con, self.aid, summaries=False)
        customer_query._VIEW_MEMO.clear()
        with _Counted(citations, "_resolve_publication") as full:
            dto = customer.get_alert(self.con, self.aid)
        self.assertGreater(self.N, customer.ALERT_EVENT_SUMMARY_LIMIT)
        self.assertLessEqual(full.calls - light.calls, len(dto["event_page"]["event_ids"]))

    def test_the_memo_keys_on_the_resolved_page_and_stays_small(self):
        """PBA-R1-04: a page past the last reuses the last page's entry.
        PBA-R2-02: each alert and caller keeps the pages last shown, at
        most ALERT_PAGE_ENTRIES, least recently used out, so Previous and
        Next stay remembered and stepping through ?page= cannot crowd
        other reads out of the shared memo. PBA-R2-03: with the alert
        (with and without summaries) that is at most ALERT_PAGE_ENTRIES
        + 2 entries."""
        pages = -(-self.N // customer.ALERT_EVENT_SUMMARY_LIMIT)
        last = customer.get_alert(self.con, self.aid, page=pages)
        with _Counted(customer, "tracker_dto") as ran, \
                _Counted(customer, "_event_summary") as resolved:
            for page in (pages + 1, 999, 999999):
                self.assertEqual(customer.get_alert(self.con, self.aid, page=page), last)
        self.assertEqual((ran.calls, resolved.calls), (0, 0),
                         "a page past the last computed the last page again")
        customer_query._VIEW_MEMO.clear()
        size = 5
        with mock.patch.object(customer, "ALERT_EVENT_SUMMARY_LIMIT", size):
            pages = -(-self.N // size)
            self.assertGreater(pages, 6)
            read = lambda page: customer.get_alert(self.con, self.aid, page=page)  # noqa: E731
            read(1), read(2)
            with _Counted(customer, "_event_summary") as resolved:
                for page in (1, 2, 1, 2):
                    read(page)
            self.assertEqual(resolved.calls, 0, "Previous and Next computed a page again")
            keep = customer.ALERT_PAGE_ENTRIES
            self.assertEqual(keep, 4)
            read(3), read(4), read(1)  # most recent last: 2, 3, 4, 1
            with _Counted(customer, "_event_summary") as resolved:
                read(5)  # evicts page 2, the least recently used
                read(1), read(3), read(4)
            self.assertEqual(resolved.calls, size, "a remembered page computed again")
            with _Counted(customer, "_event_summary") as resolved:
                read(2)
            self.assertEqual(resolved.calls, size, "the least recently used page was kept")
            customer_query._view_revision(self.con, None)
            before = len(customer_query._VIEW_MEMO)
            with _Counted(customer, "tracker_dto") as ran:
                for page in list(range(1, pages + 1)) + [50, 7000, 2]:
                    read(page)
            customer.get_alert(self.con, self.aid, summaries=False)
            self.assertEqual(ran.calls, 0, "changing page rebuilt the whole alert")
        # The current matches (PERF-HOL) are kept per definition, not per
        # alert page: one entry, outside get_alert's budget.
        alert_entries = [k for k in customer_query._VIEW_MEMO
                         if isinstance(k[2][0], tuple) and str(k[2][0][0]).startswith("alert")
                         and k[2][0][0] != "alert-matches"]
        self.assertEqual(sum(1 for k in customer_query._VIEW_MEMO if isinstance(k[2][0], tuple)
                             and k[2][0][0] == "alert-matches"), 1)
        page_entries = [k for k in alert_entries if k[2][0][0] == "alert-page"]
        self.assertEqual(len(page_entries), keep, page_entries)
        self.assertEqual(len(alert_entries), keep + 2, alert_entries)
        self.assertLessEqual(len(customer_query._VIEW_MEMO), before + 1)

    def test_paged_reads_equal_the_unpaged_read(self):
        """Every field equals the old read (summaries for up to 200 events);
        an event's summary is the old one on its page and None elsewhere.
        PBA-R3-02: the default owner, an owner, an editor and a share guest."""
        for make in (lambda: None, owner, editor, share_guest):
            with _no_memo(), mock.patch.object(customer, "ALERT_EVENT_SUMMARY_LIMIT", 200):
                ctx = make()
                old = customer.get_alert(self.con, self.aid, ctx=ctx)
                old_disclosures = sorted(map(list, ctx.disclosures)) if ctx else None
            old.pop("event_page", None)
            old_summary = {e["id"]: e["summary"] for e in old["events"]}
            self.assertTrue(all(s is not None for s in old_summary.values()))
            pages = -(-self.N // customer.ALERT_EVENT_SUMMARY_LIMIT)
            customer_query._VIEW_MEMO.clear()
            for page in list(range(1, pages + 1)) * 2:  # computed, then remembered
                ctx = make()
                got = copy.deepcopy(customer.get_alert(self.con, self.aid, ctx=ctx, page=page))
                event_page = got.pop("event_page")
                on_page = set(event_page["event_ids"])
                # PBA-R1-02: episodes over every event equal the old read's.
                self.assertEqual(event_page["episodes"], len(
                    {s["episode_id"] for s in old_summary.values() if s and s["episode_id"]}))
                expect = copy.deepcopy(old)
                for event in expect["events"]:
                    if event["id"] not in on_page:
                        event["summary"] = None
                self.assertEqual(json.dumps(got, sort_keys=True), json.dumps(expect, sort_keys=True))
                if ctx is not None:
                    disclosed = sorted(map(list, ctx.disclosures))
                    self.assertTrue(set(map(tuple, disclosed)) <= set(map(tuple, old_disclosures)))


class LocalAlertPage(_AlertEvents):
    """GET /api/v1/customer/alerts/{id}?page=N on the local server
    (scripts/app_server.py) pages as the hosted route does (PBA-R1-01)."""

    CSRF = "test-local-csrf"
    PORT = 18794

    def setUp(self):
        super().setUp()
        scripts = str(Path(__file__).resolve().parent.parent / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import app_server as srv
        self.srv = srv
        self.addCleanup(srv._state.update, dict(srv._state))
        root = Path(self.con.execute("PRAGMA database_list").fetchone()[2]).parent
        srv._state.update(workspace=root, port=self.PORT, csrf=self.CSRF)

    def get(self, query=""):
        import io
        path = f"/api/v1/customer/alerts/{self.aid}" + (f"?{query}" if query else "")
        handler = self.srv.Handler.__new__(self.srv.Handler)
        handler.command, handler.path = "GET", path
        handler.request_version = "HTTP/1.1"
        handler.requestline = f"GET {path} HTTP/1.1"
        handler._headers_buffer = []
        handler.log_message = lambda *a: None
        handler.headers = {"Host": f"127.0.0.1:{self.PORT}", "Content-Length": "0"}
        handler.rfile, handler.wfile = io.BytesIO(b""), io.BytesIO()
        handler.close_connection = True
        handler.do_GET()
        head, _, body = handler.wfile.getvalue().partition(b"\r\n\r\n")
        return int(head.split(b" ")[1]), json.loads(body.decode())

    def test_the_local_server_passes_the_page(self):
        status, one = self.get()
        self.assertEqual(status, 200, one)
        pages = -(-one["alert"]["event_page"]["total"] // one["alert"]["event_page"]["per_page"])
        self.assertGreater(pages, 1)
        seen = []
        for page in range(1, pages + 1):
            status, out = self.get(f"page={page}")
            self.assertEqual(status, 200, out)
            ep = out["alert"]["event_page"]
            self.assertEqual(ep["page"], page)
            self.assertEqual(ep, customer.get_alert(self.con, self.aid, page=page)["event_page"])
            seen += ep["event_ids"]
        self.assertEqual(sorted(seen), sorted(e["id"] for e in one["alert"]["events"]))
        for bad in ("page=0", "page=-1", "page=x"):
            status, out = self.get(bad)
            self.assertEqual(status, 400, (bad, out))

    def test_a_page_past_the_last_is_the_last_page(self):
        """PBA-R1-05: any whole number past the last page reads the last
        page; only a non-integer or a number below 1 is refused."""
        _, one = self.get()
        ep = one["alert"]["event_page"]
        last = -(-ep["total"] // ep["per_page"])
        for big in ("1000000", "99999999999999999999999", "0004"):
            status, out = self.get(f"page={big}")
            self.assertEqual(status, 200, (big, out))
            self.assertEqual(out["alert"]["event_page"]["page"], last, big)
        for bad in ("page=00", "page=1.5", "page=%2B2", "page=2x"):
            status, out = self.get(bad)
            self.assertEqual(status, 400, (bad, out))


class AlertPageHttp(unittest.TestCase):
    """GET /api/v1/customer/alerts/{id}?page=N on the hosted app."""

    def setUp(self):
        from tests.test_backend_http import HostedHttpTest
        self._harness = HostedHttpTest("test_tracker_birth_active_local_silent_baseline")
        self._harness.setUp()
        self.addCleanup(self._harness.tearDown)
        self._call = self._harness._call
        customer_query._VIEW_MEMO.clear()
        self.addCleanup(customer_query._VIEW_MEMO.clear)

    def test_page_parameter_pages_and_a_bad_page_is_refused(self):
        import sqlite3
        from tests.test_backend_http import Q2
        _, _, made = self._call("/api/v1/customer/alerts", "POST",
                                {"name": "paged", "query": dict(Q2)})
        aid = made["id"]
        con = sqlite3.connect(self._harness.db_path, isolation_level=None)
        try:
            ref = con.execute("SELECT id FROM source_states WHERE workspace_id='local'"
                              " ORDER BY publication_order DESC LIMIT 1").fetchone()[0]
            for i in range(30):
                con.execute("INSERT INTO customer_alert_events (workspace_id, alert_id, kind,"
                            " logical_identity, after_ref, observed_at, checked_at)"
                            " VALUES ('local', ?, 'new_text', ?, ?, ?, 'now')",
                            (aid, f"p{i}", ref, f"2026-10-10T00:00:{i:02d}Z"))
        finally:
            con.close()
        size = customer.ALERT_EVENT_SUMMARY_LIMIT
        status, _, one = self._call(f"/api/v1/customer/alerts/{aid}")
        self.assertEqual(status, 200, one)
        status, _, two = self._call(f"/api/v1/customer/alerts/{aid}", query="page=2")
        self.assertEqual(status, 200, two)
        p1, p2 = one["alert"]["event_page"], two["alert"]["event_page"]
        self.assertEqual((p1["page"], p1["per_page"], p1["total"]), (1, size, 30))
        self.assertEqual((p2["page"], p2["total"]), (2, 30))
        self.assertEqual(len(p1["event_ids"]) + len(p2["event_ids"]), min(30, 2 * size))
        self.assertFalse(set(p1["event_ids"]) & set(p2["event_ids"]))
        for bad in ("page=0", "page=-1", "page=x", "page=1.5", "page=00"):
            status, _, out = self._call(f"/api/v1/customer/alerts/{aid}", query=bad)
            self.assertEqual(status, 400, (bad, out))
        # PBA-R1-05: a page past the last reads the last page.
        last = -(-p1["total"] // size)
        for big in ("1000000", "99999999999999999999999"):
            status, _, out = self._call(f"/api/v1/customer/alerts/{aid}", query="page=" + big)
            self.assertEqual(status, 200, (big, out))
            self.assertEqual(out["alert"]["event_page"]["page"], last, big)


if __name__ == "__main__":
    unittest.main()
