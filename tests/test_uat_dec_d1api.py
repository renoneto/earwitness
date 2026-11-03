"""Decision D-1 (2026-10-16): ad breaks, backend.

An ad break is the customer ad reads of one episode (the canonical
episode, after alias folding) and one transcript copy whose detection
spans chain by overlap: sorted by (start, end, ref), a read joins the
current break when its start is before the break's running maximum end.
Touching spans are two breaks; a read with no transcript copy or no times
is a break of its own; earlier-copy reads chain only with their own copy.
Breaks are computed over the whole customer ad scope the caller sees, so
a filter only decides which breaks appear. Read counts (`total`,
`records`, `count`, `reads`) keep counting reads; nothing is stored.

Run: PYTHONPATH=. python -m unittest tests.test_uat_dec_d1api
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from earwitness.app import ad_breaks
from earwitness.app import customer, customer_contracts, customer_query, db

T0 = "2026-03-10"


def _read(key, start, end, tv=1, episode="e1"):
    return {"key": key, "episode_id": episode, "transcript_version_id": tv,
            "start": start, "end": end}


def _keys(breaks):
    return [[r["key"] for r in b["reads"]] for b in breaks]


class RuleTests(unittest.TestCase):
    """The pure rule, ad_breaks.chain."""

    def test_overlap_chains_through_the_running_max_end(self):
        # c does not overlap b, but starts before a's end: one break.
        out = ad_breaks.chain([_read("a", 0, 100), _read("b", 10, 20), _read("c", 50, 60)])
        self.assertEqual(_keys(out), [["a", "b", "c"]])
        self.assertEqual(out[0]["span"], [0, 100])
        out = ad_breaks.chain([_read("a", 0, 10), _read("b", 5, 20), _read("c", 15, 30)])
        self.assertEqual(_keys(out), [["a", "b", "c"]])
        self.assertEqual(out[0]["span"], [0, 30])

    def test_touching_spans_are_two_breaks(self):
        out = ad_breaks.chain([_read("a", 0, 10), _read("b", 10, 20)])
        self.assertEqual(_keys(out), [["a"], ["b"]])
        self.assertEqual([b["span"] for b in out], [[0, 10], [10, 20]])

    def test_no_gap_tolerance(self):
        out = ad_breaks.chain([_read("a", 0, 10), _read("b", 10.5, 20)])
        self.assertEqual(_keys(out), [["a"], ["b"]])

    def test_sort_order_is_start_end_ref(self):
        reads = [_read("z", 5, 9), _read("b", 0, 30), _read("a", 0, 30), _read("c", 0, 12)]
        out = ad_breaks.chain(list(reversed(reads)))
        self.assertEqual(_keys(out), [["c", "a", "b", "z"]])
        self.assertEqual(ad_breaks.chain(reads)[0]["id"], out[0]["id"])

    def test_earlier_copy_never_joins_the_current_copy(self):
        out = ad_breaks.chain([_read("cur", 0, 30, tv=2), _read("old", 10, 20, tv=1),
                               _read("old2", 15, 40, tv=1)])
        self.assertEqual(sorted(_keys(out)), [["cur"], ["old", "old2"]])
        self.assertEqual({b["transcript_version_id"] for b in out}, {1, 2})

    def test_versionless_or_timeless_reads_stand_alone(self):
        out = ad_breaks.chain([_read("a", 0, 30), _read("nov", 5, 25, tv=None),
                               _read("nov2", 6, 24, tv=None), _read("nostart", None, 20),
                               _read("noend", 3, None)])
        self.assertEqual(sorted(_keys(out)), [["a"], ["noend"], ["nostart"], ["nov"], ["nov2"]])
        spans = {b["reads"][0]["key"]: b["span"] for b in out}
        self.assertEqual(spans["nov"], [5, 25])
        self.assertIsNone(spans["nostart"])
        self.assertIsNone(spans["noend"])

    def test_episodes_never_share_a_break(self):
        out = ad_breaks.chain([_read("a", 0, 30), _read("b", 10, 20, episode="e2")])
        self.assertEqual(len(out), 2)

    def test_break_id_is_deterministic(self):
        out = ad_breaks.chain([_read("a", 0, 30), _read("b", 10, 40)])
        want = "brk:" + hashlib.sha256(b"e1|1|a").hexdigest()[:16]
        self.assertEqual(out[0]["id"], want)
        solo = ad_breaks.chain([_read("x", 1, 2, tv=None)])
        self.assertEqual(solo[0]["id"], "brk:" + hashlib.sha256(b"e1|none|x").hexdigest()[:16])
        self.assertEqual(ad_breaks.chain([_read("b", 10, 40), _read("a", 0, 30)])[0]["id"], want)


# -- seeded workspace -------------------------------------------------------

def _transcript(con, key, version_no, segments, active=1):
    cur = con.execute(
        "INSERT INTO source_versions (source_key, version_no, sha256, payload,"
        " is_active, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (key, version_no, f"sh-{key}-{version_no}", json.dumps({"segments": segments}),
         active, db.utcnow()))
    return cur.lastrowid


def _evidence(con, ev_id, episode_id, start, end, tv, brand=None, confirmed=False,
              key=None, attrs=None):
    con.execute(
        "INSERT INTO evidence (id, source, source_version_id, episode_id,"
        " orig_start_s, orig_end_s, excerpt, attrs, revision, created_at,"
        " transcript_version_id, source_record_key, assoc_revision,"
        " record_kind) VALUES (?, 'jev', 1, ?, ?, ?, 'an offer', ?, 1, ?,"
        " ?, ?, 0, 'candidate')",
        (ev_id, episode_id, start, end, json.dumps(attrs or {}), db.utcnow(), tv,
         key or f"jev:{episode_id}:{ev_id}"))
    if confirmed:
        con.execute("INSERT INTO reviews (evidence_id, judgment, revision,"
                    " updated_at) VALUES (?, 'ad', 1, ?)", (ev_id, db.utcnow()))
    if brand:
        con.execute("INSERT INTO brand_assocs (evidence_id, brand, basis, origin,"
                    " revision, updated_at) VALUES (?, ?, ?, 'test', 1, ?)",
                    (ev_id, brand, "confirmed" if confirmed else "suggested", db.utcnow()))


SEG = [{"start": float(s), "end": float(s + 10), "text": f"words {s}"} for s in range(0, 200, 10)]


def seed_breaks(con):
    """e1 (dated, one show with e2) has a current and an earlier transcript;
    e2 is undated. Reads (start-end, brand):

      e1 current: r1 0-30 acme (confirmed) + r2 20-40 beta  -> B1 (r1) + B1b (r2):
                  one overlap group split by its two visible brands (decision 1)
                  r3 40-60 acme + r7 50-55 acme             -> B2 (touches B1)
                  r4 100-130 acme                           -> B3
                  p1 55-70 self-promotion (never in a break)
      e1 earlier: r6 10-25 acme                             -> B5
      e2:         r5 0-30 beta                              -> B4
    """
    con.execute(
        "INSERT INTO source_versions (source_key, version_no, sha256, payload,"
        " is_active, created_at) VALUES ('episodes.json', 1, 'sh-eps', ?, 1, ?)",
        (json.dumps([{"ep_id": "e1", "show_title": "Shop", "feed_url": "https://shop.example/rss"},
                     {"ep_id": "e2", "show_title": "Shop", "feed_url": "https://shop.example/rss"}]),
         db.utcnow()))
    old = _transcript(con, "transcript:e1", 1, SEG)
    cur = _transcript(con, "transcript:e1", 2, SEG)
    tv2 = _transcript(con, "transcript:e2", 1, SEG)
    now = db.utcnow()
    for ep, date, tv in (("e1", T0, cur), ("e2", "", tv2)):
        con.execute(
            "INSERT INTO episodes (id, kind, show_title, ep_title, category, pub_date,"
            " fetch_date, transcript_version_id, revision, updated_at)"
            " VALUES (?, 'supplied', 'Shop', ?, 'business', ?, '2026-09-21', ?, 1, ?)",
            (ep, f"Title {ep}", date, tv, now))
    _evidence(con, "r1", "e1", 0.0, 30.0, cur, "acme", confirmed=True)
    _evidence(con, "r2", "e1", 20.0, 40.0, cur, "beta")
    _evidence(con, "r3", "e1", 40.0, 60.0, cur, "acme")
    _evidence(con, "r7", "e1", 50.0, 55.0, cur, "acme")
    _evidence(con, "r4", "e1", 100.0, 130.0, cur, "acme")
    _evidence(con, "p1", "e1", 55.0, 70.0, cur, "shop", key="named:e1:p1",
              attrs={"self_promo": True})
    _evidence(con, "r6", "e1", 10.0, 25.0, old, "acme")
    _evidence(con, "r5", "e2", 0.0, 30.0, tv2, "beta")
    con.commit()
    from tests.backend_fixtures import publish_all_records
    publish_all_records(con)
    customer.rebuild_customer_show_map(con)
    con.commit()
    return {"old": old, "cur": cur, "tv2": tv2}


def bid(episode, tv, first):
    return "brk:" + hashlib.sha256(f"{episode}|{tv}|{first}".encode()).hexdigest()[:16]


def _q(**extra):
    return customer_contracts.normalize_query(dict({"version": 2, "mode": "ads"}, **extra))


class SeededBase(unittest.TestCase):
    def setUp(self):
        from tests.backend_fixtures import make_file_workspace
        self.con = make_file_workspace()
        self.tv = seed_breaks(self.con)
        cur, old, tv2 = self.tv["cur"], self.tv["old"], self.tv["tv2"]
        self.B = {"B1": bid("e1", cur, "r1"), "B1b": bid("e1", cur, "r2"),
                  "B2": bid("e1", cur, "r3"),
                  "B3": bid("e1", cur, "r4"), "B4": bid("e2", tv2, "r5"),
                  "B5": bid("e1", old, "r6")}
        self.member_of = {"r1": "B1", "r2": "B1b", "r3": "B2", "r7": "B2", "r4": "B3",
                          "r5": "B4", "r6": "B5"}

    def tearDown(self):
        self.con.close()

    def bundle(self, summaries=(), selector=None, **query):
        with db.transaction(self.con, write=False):
            return customer_query.research_bundle(self.con, _q(**query), selector,
                                                  list(summaries), False, "ctx-test")

    def ref(self, ev_id):
        return customer._current_publication_map(self.con, "evidence", [ev_id])[ev_id]


class SearchAdsTests(SeededBase):
    def test_scope_is_what_the_seed_says(self):
        result = self.bundle()["result"]
        self.assertEqual(sorted(i["evidence_id"] for i in result["items"]),
                         ["r1", "r2", "r3", "r4", "r5", "r6", "r7"])

    def test_read_totals_unchanged_and_break_totals_added(self):
        result = self.bundle()["result"]
        self.assertEqual((result["total"], result["dated_total"], result["undated_total"]), (7, 6, 1))
        self.assertEqual((result["break_total"], result["break_dated_total"],
                          result["break_undated_total"]), (6, 5, 1))
        self.assertEqual(result["per_page"], customer_query.PER_PAGE)

    def test_every_item_carries_its_break_and_breaks_describe_the_page(self):
        result = self.bundle()["result"]
        for item in result["items"]:
            self.assertEqual(item["ad_break_id"], self.B[self.member_of[item["evidence_id"]]],
                             item["evidence_id"])
        by_id = {b["id"]: b for b in result["ad_breaks"]}
        self.assertEqual(set(by_id), set(self.B.values()))
        b1 = by_id[self.B["B1"]]
        self.assertEqual(b1["episode_id"], "e1")
        self.assertEqual(b1["transcript_version_id"], self.tv["cur"])
        self.assertIs(b1["on_current_transcript"], True)
        self.assertEqual(b1["span"], [0.0, 30.0])
        self.assertEqual((b1["read_count"], b1["matching_read_count"]), (1, 1))
        self.assertEqual(b1["refs"], [self.ref("r1")])
        self.assertEqual(b1["brands"], [{"key": "acme", "display": "Acme", "confirmed": True}])
        self.assertEqual(b1["pub_date"], T0)
        b1b = by_id[self.B["B1b"]]
        self.assertEqual((b1b["span"], b1b["read_count"], b1b["refs"]), ([20.0, 40.0], 1, [self.ref("r2")]))
        self.assertEqual(b1b["brands"], [{"key": "beta", "display": "Beta", "confirmed": False}])
        for ad in (b1, b1b):
            self.assertEqual((ad["group_id"], ad["group_ad_count"]), (self.B["B1"], 2))
        b5 = by_id[self.B["B5"]]
        self.assertIs(b5["on_current_transcript"], False)
        self.assertEqual(b5["span"], [10.0, 25.0])
        self.assertEqual(by_id[self.B["B4"]]["pub_date"], "")
        # Page order: by each break's first matching read; reads contiguous.
        order = [i["ad_break_id"] for i in result["items"]]
        seen = []
        for b in order:
            if not seen or seen[-1] != b:
                self.assertNotIn(b, seen, "a break's reads are contiguous")
                seen.append(b)
        self.assertEqual(seen, [b["id"] for b in result["ad_breaks"]])
        for b in result["ad_breaks"]:
            self.assertEqual(b["refs"], [i["ref"] for i in result["items"]
                                         if i["ad_break_id"] == b["id"]])

    def test_self_promotion_never_joins_a_break(self):
        result = self.bundle()["result"]
        b2 = next(b for b in result["ad_breaks"] if b["id"] == self.B["B2"])
        self.assertEqual((b2["span"], b2["read_count"]), ([40.0, 60.0], 2))
        promo = self.bundle(self_promo="include")["result"]
        p1 = next(i for i in promo["items"] if i["evidence_id"] == "p1")
        self.assertIsNone(p1["ad_break_id"])
        self.assertEqual((promo["total"], promo["break_total"]), (8, 6))

    def test_filter_never_changes_break_identity(self):
        result = self.bundle(brands=["beta"])["result"]
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["break_total"], 2)
        b1b = next(b for b in result["ad_breaks"] if b["id"] == self.B["B1b"])
        self.assertEqual((b1b["read_count"], b1b["matching_read_count"]), (1, 1))
        self.assertEqual(b1b["span"], [20.0, 40.0])
        self.assertEqual(b1b["refs"], [self.ref("r2")])
        self.assertEqual([b["key"] for b in b1b["brands"]], ["beta"])
        # The split keeps its overlap group whatever the filter.
        self.assertEqual((b1b["group_id"], b1b["group_ad_count"]), (self.B["B1"], 2))
        ids = self.bundle(ids=[self.ref("r7")])["result"]
        self.assertEqual([b["id"] for b in ids["ad_breaks"]], [self.B["B2"]])
        self.assertEqual(ids["ad_breaks"][0]["read_count"], 2)
        # Partial match (R2-07): r8 names no brand and overlaps r5, so it
        # joins r5's ad B4. The beta filter matches one of B4's two reads;
        # B4 keeps its id, span and size.
        _evidence(self.con, "r8", "e2", 20.0, 45.0, self.tv["tv2"])
        self.con.commit()
        from tests.backend_fixtures import publish_all_records
        publish_all_records(self.con)
        customer.rebuild_customer_show_map(self.con)
        self.con.commit()
        whole = next(b for b in self.bundle()["result"]["ad_breaks"] if b["id"] == self.B["B4"])
        self.assertEqual((whole["read_count"], whole["matching_read_count"]), (2, 2))
        result = self.bundle(brands=["beta"])["result"]
        self.assertEqual((result["total"], result["break_total"]), (2, 2))
        b4 = next(b for b in result["ad_breaks"] if b["id"] == self.B["B4"])
        self.assertEqual((b4["read_count"], b4["matching_read_count"]), (2, 1))
        self.assertEqual(b4["span"], whole["span"])
        self.assertEqual(b4["span"], [0.0, 45.0])
        self.assertEqual(b4["refs"], [self.ref("r5")])

    def test_pagination_is_by_break(self):
        with mock.patch.object(customer_query, "PER_PAGE", 2):
            pages = [self.bundle(page=n)["result"] for n in (1, 2, 3, 4)]
        self.assertEqual([len(p["ad_breaks"]) for p in pages], [2, 2, 2, 0])
        self.assertEqual(pages[3]["items"], [])
        for p in pages:
            self.assertEqual((p["total"], p["break_total"], p["per_page"]), (7, 6, 2))
            self.assertEqual({i["ad_break_id"] for i in p["items"]},
                             {b["id"] for b in p["ad_breaks"]})
        got = [i["evidence_id"] for p in pages for i in p["items"]]
        self.assertEqual(sorted(got), ["r1", "r2", "r3", "r4", "r5", "r6", "r7"])
        breaks = [b["id"] for p in pages for b in p["ad_breaks"]]
        self.assertEqual(sorted(breaks), sorted(self.B.values()))
        # The undated break sorts last, as its read does.
        self.assertEqual(breaks[-1], self.B["B4"])

    def test_selection_ids_across_pages_still_count_reads(self):
        # Three reads in two ads (r3 + r7 are one ad): the counts differ.
        refs = [self.ref(e) for e in ("r3", "r7", "r5")]
        with mock.patch.object(customer_query, "PER_PAGE", 1):
            first = self.bundle(ids=refs)["result"]
            second = self.bundle(ids=refs, page=2)["result"]
            third = self.bundle(ids=refs, page=3)["result"]
        self.assertEqual((first["total"], first["break_total"]), (3, 2))
        self.assertEqual(third["items"], [])
        self.assertEqual(sorted(i["evidence_id"] for i in first["items"] + second["items"]
                                + third["items"]),
                         ["r3", "r5", "r7"])

    def test_summary_cells_add_breaks_and_keep_read_counts(self):
        bundle = self.bundle(summaries=["brand", "week", "status", "show"])
        brand = {c["key"]: c for c in bundle["summary"]["brand"]}
        self.assertEqual((brand["acme"]["count"], brand["acme"]["breaks"]), (5, 4))
        self.assertEqual((brand["beta"]["count"], brand["beta"]["breaks"]), (2, 2))
        week = {c["key"]: c for c in bundle["summary"]["week"]}
        self.assertEqual((week["2026-03-09"]["count"], week["2026-03-09"]["breaks"]), (6, 5))
        self.assertEqual((week["unknown"]["count"], week["unknown"]["breaks"]), (1, 1))
        status = {c["key"]: c for c in bundle["summary"]["status"]}
        self.assertEqual((status["confirmed"]["count"], status["confirmed"]["breaks"]), (1, 1))
        self.assertEqual((status["finding"]["count"], status["finding"]["breaks"]), (6, 5))
        # A cell's selector still selects its reads.
        cell = brand["acme"]
        scoped = self.bundle(selector=cell["selector"])["result"]
        self.assertEqual((scoped["total"], scoped["break_total"]), (5, 4))

    def test_other_modes_have_no_break_fields(self):
        for mode in ("mentions", "episodes"):
            with db.transaction(self.con, write=False):
                bundle = customer_query.research_bundle(
                    self.con, customer_contracts.normalize_query(
                        {"version": 2, "mode": mode, "q": "words"}),
                    None, ["brand"], False, "ctx-test")
            self.assertNotIn("ad_breaks", bundle["result"], mode)
            self.assertNotIn("break_total", bundle["result"], mode)
            for item in bundle["result"]["items"]:
                self.assertNotIn("ad_break_id", item)
            for cell in bundle["summary"]["brand"]:
                self.assertNotIn("breaks", cell)

    def test_export_still_captures_every_read(self):
        with db.transaction(self.con, write=False):
            revision = customer_query._view_revision(self.con)
        with db.transaction(self.con, write=True):
            out = customer_query.capture_export(self.con, _q(), None, "all_matches", "json",
                                                revision, "ctx-test")
        self.assertEqual(out["total"], 7)

    def test_get_search_route_carries_the_fields(self):
        out = customer.query(self.con, _q())
        self.assertEqual((out["total"], out["break_total"]), (7, 6))
        self.assertEqual(len(out["ad_breaks"]), 6)


class BrandAndShowTests(SeededBase):
    def test_directory_counts_breaks_beside_reads(self):
        rows = {b["key"]: b for b in customer.brands_directory(self.con)}
        self.assertEqual((rows["acme"]["records"], rows["acme"]["ad_breaks"]), (5, 4))
        self.assertEqual((rows["beta"]["records"], rows["beta"]["ad_breaks"]), (2, 2))
        self.assertNotIn("shop", rows)  # self-promotion counts toward no brand

    def test_brand_detail_counts_breaks_whole_and_per_show(self):
        detail = customer.brand_detail(self.con, "acme")
        self.assertEqual((detail["total"], detail["ad_breaks"]), (5, 4))
        top = detail["top_shows"][0]
        self.assertEqual((top["reads"], top["ad_breaks"]), (5, 4))
        beta = customer.brand_detail(self.con, "beta")
        self.assertEqual((beta["total"], beta["ad_breaks"]), (2, 2))

    def test_show_sponsors_count_breaks_beside_reads(self):
        show_id = customer.customer_show_for_episode(self.con, "e1")["show_identity_id"]
        detail = customer.show_detail(self.con, show_id)
        rows = {s["key"]: s for s in detail["sponsors"]}
        self.assertEqual((rows["acme"]["ads"], rows["acme"]["ad_breaks"]), (5, 4))
        self.assertEqual((rows["beta"]["ads"], rows["beta"]["ad_breaks"]), (2, 2))


class AlertTests(SeededBase):
    def _alert(self, query):
        aid = customer.save_alert(self.con, "watch", query)["id"]
        self.con.commit()
        return aid

    def test_ads_alert_counts_breaks_beside_reads(self):
        aid = self._alert({"version": 2, "mode": "ads", "brands": ["acme"]})
        current = customer.get_alert(self.con, aid)["current_matches"]
        self.assertEqual((current["count"], current["breaks"]), (5, 4))
        # PERF r2: the list carries no counts (no per-alert scan).
        listed = next(a for a in customer.list_alerts(self.con) if a["id"] == aid)
        self.assertNotIn("current_matches", listed)

    def test_mentions_alert_counts_breaks_of_its_ad_reads(self):
        aid = self._alert({"version": 2, "mode": "mentions", "q": "offer"})
        current = customer.get_alert(self.con, aid)["current_matches"]
        self.assertEqual(current["tracked_by_kind"].get("new_finding"), 7)
        self.assertEqual(current["breaks"], 6)

    def test_baseline_stores_no_break_fields(self):
        aid = self._alert({"version": 2, "mode": "ads"})
        stored = self.con.execute("SELECT baseline_json FROM customer_alerts WHERE id=?",
                                  (aid,)).fetchone()[0]
        self.assertNotIn("break", stored)


class EpisodeAndRecordTests(SeededBase):
    def episode(self, ep="e1"):
        with db.transaction(self.con, write=False):
            return customer.episode_reads(self.con, ep)

    def test_episode_breaks_current_then_earlier(self):
        ep = self.episode()
        self.assertEqual([b["id"] for b in ep["ad_breaks"]],
                         [self.B["B1"], self.B["B1b"], self.B["B2"], self.B["B3"], self.B["B5"]])
        self.assertEqual(ep["ad_break_counts"], {"current": 4, "earlier": 1, "no_copy": 0})
        b1, b1b = ep["ad_breaks"][0], ep["ad_breaks"][1]
        self.assertEqual((b1["refs"], b1b["refs"]), (["r1"], ["r2"]))
        self.assertEqual((b1["read_count"], b1["span"], b1["on_current_transcript"]),
                         (1, [0.0, 30.0], True))
        self.assertEqual((b1b["read_count"], b1b["span"]), (1, [20.0, 40.0]))
        self.assertEqual(([b["key"] for b in b1["brands"]], [b["key"] for b in b1b["brands"]]),
                         (["acme"], ["beta"]))
        self.assertIs(ep["ad_breaks"][4]["on_current_transcript"], False)
        for ad in ep["ads"]:
            self.assertEqual(ad["ad_break_id"], self.B[self.member_of[ad["evidence_id"]]])
        self.assertEqual([p["evidence_id"] for p in ep["self_promo"]], ["p1"])
        self.assertNotIn("ad_break_id", ep["self_promo"][0])

    def test_episode_and_search_agree(self):
        search = {b["id"]: b for b in self.bundle()["result"]["ad_breaks"]}
        for b in self.episode()["ad_breaks"] + self.episode("e2")["ad_breaks"]:
            self.assertEqual((b["read_count"], b["span"]),
                             (search[b["id"]]["read_count"], search[b["id"]]["span"]))

    def window(self, ev_id):
        with db.transaction(self.con, write=False):
            return customer.record_window(self.con, self.ref(ev_id))

    def test_record_carries_its_break(self):
        rec = self.window("r7")
        self.assertEqual(rec["ad_break"]["id"], self.B["B2"])
        self.assertEqual((rec["ad_break"]["read_count"], rec["ad_break"]["index"]), (2, 1))
        self.assertEqual(rec["ad_break"]["span"], [40.0, 60.0])
        self.assertEqual(rec["ad_break"]["refs"], [self.ref("r3"), self.ref("r7")])
        self.assertEqual(rec["ad_break"]["evidence_ids"], ["r3", "r7"])
        self.assertIs(rec["ad_break"]["on_current_transcript"], True)
        self.assertEqual(rec["ad_break"]["brands"],
                         [{"key": "acme", "display": "Acme", "confirmed": False}])
        self.assertEqual(self.window("r6")["ad_break"]["on_current_transcript"], False)

    def test_self_promotion_record_has_no_break(self):
        self.assertIsNone(self.window("p1")["ad_break"])


class AliasTests(unittest.TestCase):
    """bench_0-style alias: its reads belong to the canonical episode's breaks."""

    def setUp(self):
        from tests.backend_fixtures import make_file_workspace
        from tests.test_customer_alias_twins import seed_alias_twins
        self.con = make_file_workspace()
        seed_alias_twins(self.con)
        self.tv_a = self.con.execute("SELECT transcript_version_id FROM episodes"
                                     " WHERE id='bench_0'").fetchone()[0]

    def tearDown(self):
        self.con.close()

    def test_alias_reads_break_under_the_canonical_episode(self):
        with db.transaction(self.con, write=False):
            result = customer_query.research_bundle(self.con, _q(), None, [], False, "")["result"]
        self.assertEqual((result["total"], result["break_total"]), (4, 4))
        alias = [b for b in result["ad_breaks"] if b["transcript_version_id"] == self.tv_a]
        self.assertEqual(len(alias), 2)
        for b in alias:
            self.assertEqual(b["episode_id"], "s000e0")
        self.assertIn(bid("s000e0", self.tv_a, "a-a"), {b["id"] for b in alias})
        with db.transaction(self.con, write=False):
            page = customer.episode_reads(self.con, "bench_0")
        self.assertIn(bid("s000e0", self.tv_a, "a-a"), {b["id"] for b in page["ad_breaks"]})


# -- hosted and local routes ------------------------------------------------

class RouteTests(unittest.TestCase):
    """The hosted ASGI routes and the local app_server routes return the
    same break fields."""
    PORT = 18797
    HOST = f"127.0.0.1:{PORT}"

    def setUp(self):
        from earwitness.app import asgi as _asgi
        from earwitness.app import browser_auth as _auth
        from earwitness.app import collaboration as _collab
        from earwitness.app import connections as _conn
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = os.path.realpath(os.path.join(self.tmp.name, "data"))
        os.makedirs(self.data_dir)
        self.db_path = os.path.join(self.data_dir, "workspace.sqlite3")
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        db.init_schema(con)
        con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('workspace_id', 'ws-d1api')")
        con.commit()
        _collab.create_workspace(con, "local", "Acme", "oidc:https://issuer.example:alice")
        seed_breaks(con)
        aid = customer.save_alert(con, "acme", {"version": 2, "mode": "ads", "brands": ["acme"]})["id"]
        con.commit()
        self.alert_id = aid
        self.r7 = customer._current_publication_map(con, "evidence", ["r7"])["r7"]
        self.show_id = customer.customer_show_for_episode(con, "e1")["show_identity_id"]
        con.close()
        self.app = _asgi.HostedApp(_asgi.HostedConfig(
            data_dir=self.data_dir, grants_db=os.path.join(self.tmp.name, "grants.sqlite3")))
        _conn.register_workspace(self.app.store, "acme", self.db_path, self.data_dir)
        digest = hashlib.sha256(b"ew-browser-v1:sess-d1api").hexdigest()
        now_s = time.time()
        raw = self.app.store.auth_connection()
        try:
            _auth.ensure_auth_schema(raw)
            raw.execute(
                "INSERT INTO browser_sessions (session_hash, issuer, subject, email,"
                " email_verified, csrf_token, active_workspace, created_at, expires_at,"
                " revoked_at) VALUES (?, 'https://issuer.example', 'alice', '', 0,"
                " 'csrf-d1', 'acme', ?, ?, NULL)", (digest, now_s, now_s + 3600))
            raw.commit()
        finally:
            raw.close()
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from scripts import app_server as srv
        self.srv = srv
        self._prev = {k: srv._state.get(k) for k in ("workspace", "port", "csrf")}
        srv._state.update({"workspace": Path(self.data_dir), "port": self.PORT, "csrf": "csrf-local"})

    def tearDown(self):
        self.srv._state.update(self._prev)
        self.tmp.cleanup()

    def hosted(self, path, qs=""):
        scope = {"type": "http", "method": "GET", "path": "/api/v1/customer/" + path,
                 "scheme": "http",
                 "headers": [(b"host", b"127.0.0.1:8771"), (b"cookie", b"ew_session=sess-d1api")],
                 "query_string": qs.encode()}
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        asyncio.run(self.app(scope, receive, send))
        start = next(m for m in sent if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        self.assertEqual(start["status"], 200, body[:300])
        return json.loads(body.decode())

    def local(self, path, qs=""):
        handler = self.srv.Handler.__new__(self.srv.Handler)
        full = "/api/v1/customer/" + path + ("?" + qs if qs else "")
        handler.command, handler.path = "GET", full
        handler.request_version = "HTTP/1.1"
        handler.requestline = f"GET {full} HTTP/1.1"
        handler._headers_buffer = []
        handler.log_message = lambda *a: None
        handler.headers = {"Host": self.HOST, "Content-Length": "0"}
        handler.rfile, handler.wfile = io.BytesIO(b""), io.BytesIO()
        handler.close_connection = True
        handler.do_GET()
        head, _, rest = handler.wfile.getvalue().partition(b"\r\n\r\n")
        self.assertEqual(int(head.split(b" ")[1]), 200, rest[:300])
        return json.loads(rest.decode())

    def both(self, path, qs=""):
        return self.hosted(path, qs), self.local(path, qs)

    def test_search(self):
        qs = "query=" + urllib.parse.quote(json.dumps({"version": 2, "mode": "ads"}))
        for out in self.both("search", qs):
            self.assertEqual((out["total"], out["break_total"], out["break_dated_total"],
                              out["break_undated_total"]), (7, 6, 5, 1))
            self.assertEqual(len(out["ad_breaks"]), 6)
            self.assertTrue(all(i["ad_break_id"] for i in out["items"]))
        hosted, local = self.both("search", qs)
        self.assertEqual(hosted["ad_breaks"], local["ad_breaks"])

    def test_brands_and_brand_detail(self):
        for out in self.both("brands"):
            acme = next(b for b in out["brands"] if b["key"] == "acme")
            self.assertEqual((acme["records"], acme["ad_breaks"]), (5, 4))
        for out in self.both("brands/acme"):
            self.assertEqual((out["brand"]["total"], out["brand"]["ad_breaks"]), (5, 4))
            self.assertEqual(out["brand"]["top_shows"][0]["ad_breaks"], 4)

    def test_show_sponsors(self):
        for out in self.both("shows/" + urllib.parse.quote(self.show_id)):
            acme = next(s for s in out["sponsors"] if s["key"] == "acme")
            self.assertEqual((acme["ads"], acme["ad_breaks"]), (5, 4))

    def test_alerts(self):
        # PERF r2: the list never scans for current matches (no list
        # consumer reads them); the alert page still counts them.
        hosted, local = self.both("alerts")
        for out in (hosted, local):
            listed = next(a for a in out["alerts"] if a["id"] == self.alert_id)
            self.assertNotIn("current_matches", listed)
        for out in self.both(f"alerts/{self.alert_id}"):
            self.assertEqual(out["alert"]["current_matches"]["count"], 5)
            self.assertEqual(out["alert"]["current_matches"]["breaks"], 4)

    def test_episode(self):
        hosted, local = self.both("episodes/e1")
        for out in (hosted, local):
            self.assertEqual(out["episode"]["ad_break_counts"],
                             {"current": 4, "earlier": 1, "no_copy": 0})
        self.assertEqual(hosted["episode"]["ad_breaks"], local["episode"]["ad_breaks"])

    def test_record(self):
        hosted, local = self.both("records/" + urllib.parse.quote(self.r7, safe=""))
        for out in (hosted, local):
            self.assertEqual((out["record"]["ad_break"]["read_count"],
                              out["record"]["ad_break"]["index"]), (2, 1))
        self.assertEqual(hosted["record"]["ad_break"], local["record"]["ad_break"])


if __name__ == "__main__":
    unittest.main()
