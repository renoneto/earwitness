"""Walkthrough decision 1 (2026-10-16), backend: ads, not reads.

An ad is the customer ad reads that overlap on one transcript copy
(ad_breaks.chain), split wherever two different brands are named: walk
each overlap group in order; a read naming no brand joins the current
ad; a read whose named brands share nothing with the current ad's named
brands opens a new ad; one read is never split. "Named" = the spoken
brand keys customers see: a name under review, an unconfirmed name of no
known brand and a web address or code never name a brand; a
machine-suggested name of a known brand does (and stays unconfirmed).
An unsplit group keeps its id; a split piece's id is break_id(episode,
copy, its first evidence id). Every ad dict carries group_id (its overlap
group's id) and group_ad_count (ads in that group).

Wire names stay (ad_breaks, break_total, breaks, ad_break_id, ...); their
values are ads. Compare carries `ads` beside each occurrence count; alert
events carry `ad_id`; alerts `unread_ad_count`; Overview `alert_totals`
`ads`/`unread_ads`; new exports an `ad_id` column and `ad_count`.
Campaign checks count distinct ads; the occurrence -> ad mapping is
pinned, so a regroup stales a frozen report instead of changing silently.
Stored alert events, baselines and frozen report bytes are untouched.

Run: PYTHONPATH=. python -m unittest tests.test_uat_d1
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import unittest
import urllib.parse
from unittest import mock

from earwitness.app import ad_breaks, campaigns, citations, customer, customer_query, db
from tests import test_uat_dec_d1api as d1
from tests.test_customer_campaigns import (add_candidate, add_placement, fresh_db, occ,
                                           placement, seed_campaign, seed_episode,
                                           verify_full_ad)

NOW = "2026-04-01T00:00:00+00:00"


def _r(key, start, end, names=(), tv=1, episode="e1"):
    return dict(d1._read(key, start, end, tv=tv, episode=episode), names=list(names))


def _names(read):
    return set(read.get("names") or ())


def _ads(reads):
    return ad_breaks.split(ad_breaks.chain(reads), _names)


def _keys(ads):
    return [[r["key"] for r in a["reads"]] for a in ads]


def _republish(con):
    con.commit()
    from tests.backend_fixtures import publish_all_records
    publish_all_records(con)
    customer.rebuild_customer_show_map(con)
    con.commit()


class RuleTests(unittest.TestCase):
    """The pure rule: ad_breaks.split over ad_breaks.chain groups."""

    def test_overlapping_reads_naming_different_brands_are_two_ads(self):
        out = _ads([_r("a", 0, 30, ["x"]), _r("b", 20, 50, ["y"])])
        self.assertEqual(_keys(out), [["a"], ["b"]])

    def test_an_unnamed_read_stays_with_the_earlier_ad(self):
        out = _ads([_r("a", 0, 30, ["x"]), _r("u", 10, 40), _r("b", 20, 50, ["y"])])
        self.assertEqual(_keys(out), [["a", "u"], ["b"]])
        out = _ads([_r("u", 0, 30), _r("a", 10, 40, ["x"]), _r("b", 20, 50, ["y"])])
        self.assertEqual(_keys(out), [["u", "a"], ["b"]])

    def test_same_brand_overlapping_reads_are_one_ad(self):
        out = _ads([_r("a", 0, 30, ["x"]), _r("b", 10, 40, ["x"]), _r("c", 20, 50)])
        self.assertEqual(_keys(out), [["a", "b", "c"]])

    def test_one_read_naming_two_brands_is_never_split(self):
        out = _ads([_r("a", 0, 30, ["x", "y"]), _r("b", 10, 20, ["y"])])
        self.assertEqual(_keys(out), [["a", "b"]])
        out = _ads([_r("a", 0, 30, ["x"]), _r("b", 10, 40, ["x", "y"])])
        self.assertEqual(_keys(out), [["a", "b"]], "b shares x with the current ad")

    def test_a_returning_brand_after_another_is_a_third_ad(self):
        # A-B-A (Kachava, other, Kachava): the rule makes three ads.
        out = _ads([_r("a", 0, 30, ["x"]), _r("b", 10, 40, ["y"]), _r("c", 20, 50, ["x"])])
        self.assertEqual(_keys(out), [["a"], ["b"], ["c"]])

    def test_an_unsplit_group_keeps_its_id_and_span(self):
        reads = [_r("a", 0, 30, ["x"]), _r("b", 10, 40, ["x"])]
        group = ad_breaks.chain([dict(r) for r in reads])[0]
        out = _ads(reads)
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0]["id"], out[0]["span"]), (group["id"], [0, 40]))
        self.assertEqual((out[0]["group_id"], out[0]["group_ad_count"]), (group["id"], 1))

    def test_split_pieces_take_their_first_evidence_id_and_own_span(self):
        out = _ads([_r("a", 0, 100, ["x"]), _r("b", 20, 50, ["y"]), _r("c", 30, 60)])
        self.assertEqual(_keys(out), [["a"], ["b", "c"]])
        self.assertEqual(out[0]["id"], d1.bid("e1", 1, "a"))
        self.assertEqual(out[1]["id"], d1.bid("e1", 1, "b"))
        self.assertEqual([a["span"] for a in out], [[0, 100], [20, 60]])
        self.assertEqual({a["group_id"] for a in out}, {d1.bid("e1", 1, "a")})
        self.assertEqual([a["group_ad_count"] for a in out], [2, 2])

    def test_earlier_copy_and_no_copy_behaviour_is_unchanged(self):
        reads = [_r("cur", 0, 30, ["x"], tv=2), _r("old", 10, 20, ["y"], tv=1),
                 _r("old2", 15, 40, ["y"], tv=1), _r("nov", 5, 25, ["z"], tv=None),
                 _r("nov2", 6, 24, ["w"], tv=None), _r("nostart", None, 20, ["q"])]
        before = ad_breaks.chain([dict(r) for r in reads])
        after = _ads(reads)
        self.assertEqual(_keys(after), _keys(before))
        self.assertEqual([a["id"] for a in after], [b["id"] for b in before])
        self.assertTrue(all(a["group_ad_count"] == 1 for a in after))

    def test_order_matches_chain_order(self):
        out = _ads([_r("z", 200, 210), _r("b", 20, 50, ["y"]), _r("a", 0, 30, ["x"])])
        self.assertEqual(_keys(out), [["a"], ["b"], ["z"]])


# -- the brand keys that split, on a seeded workspace --------------------------

class KnownBeta(d1.SeededBase):
    """d1 seed with beta a known brand: r5 (e2) has its beta confirmed by a
    person. r2's machine-suggested beta (B1, beside r1's acme) splits B1
    into an acme ad and a beta ad, as it does on the plain seed (item 5:
    a visible name splits, known or not)."""

    def setUp(self):
        super().setUp()
        self.con.execute("UPDATE brand_assocs SET basis='confirmed' WHERE evidence_id='r5'")
        _republish(self.con)
        self.B["B1b"] = d1.bid("e1", self.tv["cur"], "r2")


class BrandKeyTests(d1.SeededBase):
    def test_a_visible_unknown_machine_suggestion_splits(self):
        # Plain d1 seed: beta is only a machine suggestion of no known brand,
        # yet customers see it on the ad, so it is named and splits B1
        # (item 5, 2026-10-16 22:36: split on exactly the visible names).
        result = self.bundle()["result"]
        self.assertEqual(result["break_total"], 6)
        by_id = {b["id"]: b for b in result["ad_breaks"]}
        b1, b1b = by_id[self.B["B1"]], by_id[d1.bid("e1", self.tv["cur"], "r2")]
        self.assertEqual(b1["brands"], [{"key": "acme", "display": "Acme", "confirmed": True}])
        self.assertEqual(b1b["brands"], [{"key": "beta", "display": "Beta", "confirmed": False}])
        self.assertEqual((b1["read_count"], b1b["read_count"]), (1, 1))
        for ad in (b1, b1b):
            self.assertEqual((ad["group_id"], ad["group_ad_count"]), (self.B["B1"], 2))

    def test_no_ad_lists_brands_from_reads_with_disjoint_visible_names(self):
        # Guard: an ad lists two brands only when one of its reads names
        # both (scope_index: Search, Brands, Compare, alerts, exports) ...
        from earwitness.app import ad_reads
        with db.transaction(self.con, write=False):
            aliases = ad_reads.alias_map(self.con)
            index = ad_breaks.scope_index(self.con)
        for ad in {a["id"]: a for a in index.values()}.values():
            keys = {b["key"] for b in ad["brands"]}
            per_read = [{k for k, _c, a in ad_breaks._read_keys(aliases, r)
                         if a.get("channel") not in ("url", "code")} for r in ad["reads"]]
            self.assertTrue(len(keys) < 2 or any(keys <= n for n in per_read), (ad["id"], keys))
        # ... and on the d1 seed no read names two, so no ad lists two
        # (Search page ads, and the Episode page via breaks_of_matches).
        result = self.bundle()["result"]
        with db.transaction(self.con, write=False):
            ep = customer.episode_reads(self.con, "e1")
        for ad in result["ad_breaks"] + ep["ad_breaks"]:
            self.assertLessEqual(len(ad["brands"]), 1, (ad["id"], ad["brands"]))

    def _known_beta(self):
        self.con.execute("UPDATE brand_assocs SET basis='confirmed' WHERE evidence_id='r5'")
        _republish(self.con)

    def test_known_machine_suggestion_splits_and_stays_unconfirmed(self):
        self._known_beta()
        result = self.bundle()["result"]
        self.assertEqual(result["break_total"], 6)
        by_id = {b["id"]: b for b in result["ad_breaks"]}
        b1, b1b = by_id[self.B["B1"]], by_id[d1.bid("e1", self.tv["cur"], "r2")]
        self.assertEqual(b1["brands"], [{"key": "acme", "display": "Acme", "confirmed": True}])
        self.assertEqual(b1b["brands"], [{"key": "beta", "display": "Beta", "confirmed": False}])
        self.assertEqual((b1["span"], b1b["span"]), ([0.0, 30.0], [20.0, 40.0]))

    def test_web_address_or_code_never_splits(self):
        self._known_beta()
        self.con.execute("UPDATE brand_assocs SET channel='url' WHERE evidence_id='r2'")
        _republish(self.con)
        self.assertEqual(self.bundle()["result"]["break_total"], 5)

    def test_a_name_under_review_never_splits(self):
        self._known_beta()
        real = customer.brand_under_review
        with mock.patch.object(customer, "brand_under_review",
                               side_effect=lambda ev, a: ev.get("id") == "r2" or real(ev, a)):
            result = self.bundle()["result"]
        self.assertEqual(result["break_total"], 5)
        b1 = next(b for b in result["ad_breaks"] if b["id"] == self.B["B1"])
        self.assertEqual([b["key"] for b in b1["brands"]], ["acme"])

    def test_a_confirmed_name_always_splits(self):
        self.con.execute("UPDATE brand_assocs SET basis='confirmed' WHERE evidence_id='r2'")
        _republish(self.con)
        self.assertEqual(self.bundle()["result"]["break_total"], 6)


class SurfaceTests(KnownBeta):
    """Every surface counts ads through the one rule; grouping fields on
    every ad dict."""

    def test_search_counts_ads_and_reads_stay_reads(self):
        result = self.bundle()["result"]
        self.assertEqual((result["total"], result["break_total"], result["break_dated_total"],
                          result["break_undated_total"]), (7, 6, 5, 1))
        items = {i["evidence_id"]: i for i in result["items"]}
        self.assertEqual(items["r1"]["ad_break_id"], self.B["B1"])
        self.assertEqual(items["r2"]["ad_break_id"], self.B["B1b"])
        for b in result["ad_breaks"]:
            if b["id"] in (self.B["B1"], self.B["B1b"]):
                self.assertEqual((b["group_id"], b["group_ad_count"]), (self.B["B1"], 2))
            else:
                self.assertEqual((b["group_id"], b["group_ad_count"]), (b["id"], 1))

    def test_brands_brand_page_show_and_alerts(self):
        rows = {b["key"]: b for b in customer.brands_directory(self.con)}
        self.assertEqual((rows["acme"]["records"], rows["acme"]["ad_breaks"]), (5, 4))
        self.assertEqual((rows["beta"]["records"], rows["beta"]["ad_breaks"]), (2, 2))
        show_id = customer.customer_show_for_episode(self.con, "e1")["show_identity_id"]
        with db.transaction(self.con, write=False):
            detail = customer.show_detail(self.con, show_id)
        eps = {e["id"]: e for e in detail["episodes"]}
        self.assertEqual((eps["e1"]["ads"], eps["e1"]["ad_breaks"]), (6, 5))
        aid = customer.save_alert(self.con, "w", {"version": 2, "mode": "ads"})["id"]
        self.con.commit()
        self.assertEqual(customer.get_alert(self.con, aid)["current_matches"]["breaks"], 6)

    def test_episode_and_record_carry_the_grouping(self):
        with db.transaction(self.con, write=False):
            ep = customer.episode_reads(self.con, "e1")
            rec = customer.record_window(self.con, self.ref("r2"))
        self.assertEqual([b["id"] for b in ep["ad_breaks"]],
                         [self.B["B1"], self.B["B1b"], self.B["B2"], self.B["B3"], self.B["B5"]])
        self.assertEqual(ep["ad_break_counts"], {"current": 4, "earlier": 1, "no_copy": 0})
        self.assertEqual([(b["group_id"], b["group_ad_count"]) for b in ep["ad_breaks"][:3]],
                         [(self.B["B1"], 2), (self.B["B1"], 2), (self.B["B2"], 1)])
        ads = {a["evidence_id"]: a["ad_break_id"] for a in ep["ads"]}
        self.assertEqual((ads["r1"], ads["r2"]), (self.B["B1"], self.B["B1b"]))
        brk = rec["ad_break"]
        self.assertEqual((brk["id"], brk["read_count"], brk["index"]), (self.B["B1b"], 1, 0))
        self.assertEqual((brk["group_id"], brk["group_ad_count"]), (self.B["B1"], 2))


class TopShowOrderTests(unittest.TestCase):
    """R2-10: a brand's top shows rank by ads, then reads."""

    def test_more_ads_ranks_above_more_reads(self):
        def item(ev, show):
            return {"evidence_id": ev, "ref": "ref-" + ev, "status": "finding", "pub_date": "",
                    "show": {"show_identity_id": show, "display_title": show.upper()}}
        items = [item("a1", "s1"), item("a2", "s1"), item("a3", "s1"),
                 item("b1", "s2"), item("b2", "s2")]
        breaks = {"a1": {"id": "A"}, "a2": {"id": "A"}, "a3": {"id": "A"},
                  "b1": {"id": "B"}, "b2": {"id": "C"}}
        con = fresh_db()
        self.addCleanup(con.close)
        rows = customer._brand_top_shows(con, items, breaks=breaks)["top_shows"]
        self.assertEqual([(r["show_id"], r["ad_breaks"], r["reads"]) for r in rows],
                         [("s2", 2, 2), ("s1", 1, 3)])
        # Equal ads: more reads first.
        breaks["b2"] = {"id": "B"}
        rows = customer._brand_top_shows(con, items, breaks=breaks)["top_shows"]
        self.assertEqual([(r["show_id"], r["ad_breaks"], r["reads"]) for r in rows],
                         [("s1", 1, 3), ("s2", 1, 2)])


# -- Compare -------------------------------------------------------------------

class CompareTests(d1.SeededBase):
    """r3 and r7 (one ad, B2) and r1 (B1) confirmed. PLAIN-ADS (DEP7):
    Compare counts every acme ad the caller can see in the period (five
    pieces of evidence in four ads), confirmed or not."""

    def setUp(self):
        super().setUp()
        for ev in ("r3", "r7"):
            self.con.execute("INSERT INTO reviews (evidence_id, judgment, revision, updated_at)"
                             " VALUES (?, 'ad', 1, ?)", (ev, db.utcnow()))
            self.con.execute("UPDATE brand_assocs SET basis='confirmed' WHERE evidence_id=?", (ev,))
        _republish(self.con)

    def test_compare_counts_every_ad_of_the_brand(self):
        with db.transaction(self.con, write=False):
            out = customer_query.compare_bundle(
                self.con, ["acme", "beta"], {"from": "2026-03-01", "to": "2026-03-31"},
                {"from": "2026-04-01", "to": "2026-04-30"}, None, None, "ctx-test")
        cells = {c["brand"]: c for c in out["cells"]}
        self.assertEqual((cells["acme"]["a"]["count"], cells["acme"]["a"]["ads"]), (5, 4))
        self.assertEqual((cells["acme"]["b"]["count"], cells["acme"]["b"]["ads"]), (0, 0))
        # beta's one unconfirmed ad counts too (PLAIN-ADS).
        self.assertEqual((cells["beta"]["a"]["count"], cells["beta"]["a"]["ads"]), (1, 1))


# -- alert events and Overview ---------------------------------------------------

def _events(con):
    return [tuple(r) for r in con.execute("SELECT * FROM customer_alert_events ORDER BY id")]


class AlertEventTests(d1.SeededBase):
    def setUp(self):
        super().setUp()
        self.aid = customer.save_alert(self.con, "acme",
                                       {"version": 2, "mode": "ads", "brands": ["acme"]})["id"]
        self.con.commit()
        cur = self.tv["cur"]
        d1._evidence(self.con, "r8", "e1", 45.0, 58.0, cur, "acme")     # joins B2
        d1._evidence(self.con, "r9", "e1", 200.0, 230.0, cur, "acme")   # a new ad
        _republish(self.con)
        customer.check_alert(self.con, self.aid)
        self.con.commit()
        self.B9 = d1.bid("e1", cur, "r9")

    def test_check_emits_one_event_per_evidence_record_as_before(self):
        rows = self.con.execute("SELECT kind, logical_identity, read_state FROM"
                                " customer_alert_events WHERE alert_id=? ORDER BY id",
                                (self.aid,)).fetchall()
        self.assertEqual(sorted(tuple(r) for r in rows),
                         [("new_finding", "finding:r8", 0), ("new_finding", "finding:r9", 0)])

    def test_events_carry_their_ad_and_unread_ads_count_once(self):
        d1._evidence(self.con, "r10", "e1", 210.0, 240.0, self.tv["cur"], "acme")  # r9's ad
        _republish(self.con)
        customer.check_alert(self.con, self.aid)
        self.con.commit()
        alert = customer.get_alert(self.con, self.aid)
        ad_of = {e["logical_identity"]: e["ad_id"] for e in alert["events"]}
        self.assertEqual(ad_of, {"finding:r8": self.B["B2"], "finding:r9": self.B9,
                                 "finding:r10": self.B9})
        self.assertEqual((alert["unread_event_count"], alert["unread_ad_count"]), (3, 2))
        ids = [e["id"] for e in alert["events"] if e["logical_identity"] == "finding:r9"]
        customer.mark_alert_events_read(self.con, self.aid, ids)
        self.con.commit()
        alert = customer.get_alert(self.con, self.aid)
        self.assertEqual((alert["unread_event_count"], alert["unread_ad_count"]), (2, 2),
                         "r10 keeps r9's ad unread")

    def _raw_event(self, kind, logical):
        self.con.execute("INSERT INTO customer_alert_events (alert_id, kind, logical_identity,"
                         " observed_at, checked_at) VALUES (?, ?, ?, ?, ?)",
                         (self.aid, kind, logical, db.utcnow(), db.utcnow()))
        self.con.commit()

    def test_out_of_scope_and_non_finding_events(self):
        self._raw_event("new_finding", "finding:gone")
        self._raw_event("offer_difference", "offer:x")
        alert = customer.get_alert(self.con, self.aid)
        ad_of = {e["logical_identity"]: e["ad_id"] for e in alert["events"]}
        self.assertIsNone(ad_of["finding:gone"])
        self.assertIsNone(ad_of["offer:x"])
        # r8, r9: 2 ads; each event with no ad is one of its own.
        self.assertEqual((alert["unread_event_count"], alert["unread_ad_count"]), (4, 4))
        over = customer.overview(self.con)
        total = next(t for t in over["alert_totals"] if t["alert_id"] == self.aid)
        self.assertEqual((total["events"], total["unread"], total["ads"], total["unread_ads"]),
                         (4, 4, 4, 4))
        recent = {e["logical_identity"]: e["ad_id"] for e in over["recent_events"]}
        self.assertEqual(recent, {"finding:r8": self.B["B2"], "finding:r9": self.B9,
                                  "finding:gone": None, "offer:x": None})

    def test_overview_totals_count_ads(self):
        d1._evidence(self.con, "r10", "e1", 210.0, 240.0, self.tv["cur"], "acme")
        _republish(self.con)
        customer.check_alert(self.con, self.aid)
        self.con.commit()
        r8 = [e["id"] for e in customer.get_alert(self.con, self.aid)["events"]
              if e["logical_identity"] == "finding:r8"]
        customer.mark_alert_events_read(self.con, self.aid, r8)
        self.con.commit()
        total = next(t for t in customer.overview(self.con)["alert_totals"]
                     if t["alert_id"] == self.aid)
        self.assertEqual((total["events"], total["unread"], total["ads"], total["unread_ads"]),
                         (3, 2, 2, 1))

    def test_overview_totals_without_listed_events_count_no_ads(self):
        # The totals query keys on the alert's workspace, the event list on
        # the event's: a totals row with no listed events must not raise.
        self.con.execute("UPDATE customer_alert_events SET workspace_id='other' WHERE alert_id=?",
                         (self.aid,))
        self.con.commit()
        total = next(t for t in customer.overview(self.con)["alert_totals"]
                     if t["alert_id"] == self.aid)
        self.assertEqual((total["ads"], total["unread_ads"]), (0, 0))

    def test_page_reads_never_touch_stored_events(self):
        before = _events(self.con)
        baseline = self.con.execute("SELECT baseline_json FROM customer_alerts WHERE id=?",
                                    (self.aid,)).fetchone()[0]
        customer.get_alert(self.con, self.aid)
        customer.list_alerts(self.con)
        customer.overview(self.con)
        self.assertEqual(_events(self.con), before)
        self.assertEqual(self.con.execute("SELECT baseline_json FROM customer_alerts WHERE id=?",
                                          (self.aid,)).fetchone()[0], baseline)
        self.assertNotIn("ad_id", baseline)


# -- exports ---------------------------------------------------------------------

class ExportTests(d1.SeededBase):
    def _export(self, fmt="json", **query):
        with db.transaction(self.con, write=False):
            revision = customer_query._view_revision(self.con)
        with db.transaction(self.con, write=True):
            return customer_query.capture_export(self.con, d1._q(**query), None, "all_matches",
                                                 fmt, revision, "ctx-test")

    def test_new_export_has_an_ad_id_per_evidence_record(self):
        out = self._export()
        self.assertEqual((out["total"], out["item_count"], out["ad_count"]), (7, 7, 6))
        payload = json.loads(out["payload"])
        by_ev = {}
        for item in payload["items"]:
            by_ev[item["evidence_id"]] = item["ad_id"]
        self.assertEqual(by_ev, {e: self.B[b] for e, b in self.member_of.items()})
        csv_out = self._export("csv")
        header = csv_out["payload"].splitlines()[0].split(",")
        self.assertIn("ad_id", header)
        self.assertEqual(len(csv_out["payload"].strip().splitlines()), 8)

    def test_a_stored_capture_keeps_its_bytes(self):
        first = self._export()
        stored = self.con.execute("SELECT payload, digest FROM exports WHERE id=?",
                                  (first["export_id"],)).fetchone()
        self.con.execute("UPDATE brand_assocs SET basis='confirmed' WHERE evidence_id='r2'")
        _republish(self.con)
        self._export()
        again = self.con.execute("SELECT payload, digest FROM exports WHERE id=?",
                                 (first["export_id"],)).fetchone()
        self.assertEqual(tuple(again), tuple(stored))


# -- campaigns (plan step 2.6) -----------------------------------------------------

class EvaluateTests(unittest.TestCase):
    """evaluate_one / allocate_campaign count distinct ads (occurrence ad_id);
    an occurrence with no ad_id is an ad of its own (as before)."""

    def test_two_pieces_of_evidence_of_one_ad_count_once(self):
        p = placement("p1", expected_count=2, date_to="2026-03-31")
        cov = {"state": "complete"}
        one_ad = [occ("o1", ad_id="brk:a"), occ("o2", ad_id="brk:a")]
        out = campaigns.evaluate_one(p, one_ad, cov, NOW)
        self.assertNotEqual(out["outcome"], "supported")
        self.assertEqual(out["eligible_ids"], ["o1", "o2"])
        two_ads = [occ("o1", ad_id="brk:a"), occ("o2", ad_id="brk:b")]
        self.assertEqual(campaigns.evaluate_one(p, two_ads, cov, NOW)["outcome"], "supported")
        legacy = [occ("o1"), occ("o2")]
        self.assertEqual(campaigns.evaluate_one(p, legacy, cov, NOW)["outcome"], "supported")

    def test_allocation_claims_every_piece_of_a_counted_ad(self):
        ps = [placement("p1"), placement("p2")]
        occs = [occ("o1", ad_id="brk:a"), occ("o2", ad_id="brk:a"), occ("o3", ad_id="brk:b")]
        out = campaigns.allocate_campaign(ps, occs, {"state": "complete"}, NOW)
        self.assertEqual(out["used"], {"o1": "p1", "o2": "p1", "o3": "p2"})
        self.assertEqual(out["evaluations"]["p2"]["outcome"], "supported")
        self.assertEqual(out["evaluations"]["p2"]["eligible_ids"], ["o3"])

    def test_one_ad_never_supports_two_placements(self):
        ps = [placement("p1"), placement("p2")]
        occs = [occ("o1", ad_id="brk:a"), occ("o2", ad_id="brk:a")]
        out = campaigns.allocate_campaign(ps, occs, {"state": "complete"}, NOW)
        self.assertEqual(out["evaluations"]["p1"]["outcome"], "supported")
        self.assertNotEqual(out["evaluations"]["p2"]["outcome"], "supported")


class CampaignDraftTests(unittest.TestCase):
    """Two verified pieces of evidence of one ad on ep-1, both attached."""

    def setUp(self):
        self.con = fresh_db()
        self.addCleanup(self.con.close)
        seed_episode(self.con)
        self.cid = seed_campaign(self.con)

    def _attach(self, pid, ev_id):
        rev = self.con.execute("SELECT revision FROM campaigns WHERE id=?", (self.cid,)).fetchone()[0]
        campaigns.save_placement_evidence(
            self.con, self.cid, pid, citations.current_source_ref(self.con, "evidence", ev_id), rev)

    def _two_pieces(self, expected=2, second=(5.0, 15.0)):
        pid = add_placement(self.con, self.cid, episode_id="ep-1", expected_count=expected)
        add_candidate(self.con, "x", start=0.0, end=10.0)
        add_candidate(self.con, "y", start=second[0], end=second[1])
        verify_full_ad(self.con, "x")
        verify_full_ad(self.con, "y")
        self._attach(pid, "x")
        self._attach(pid, "y")
        return pid

    def test_draft_counts_ads_and_pins_the_mapping(self):
        pid = self._two_pieces()
        d = campaigns.build_customer_draft(self.con, self.cid)
        ev = d["allocation"]["evaluations"][pid]
        self.assertEqual(len(ev["eligible_ids"]), 2)
        self.assertNotEqual(ev["outcome"], "supported")
        groups = d["pins"]["ad_groups"][pid]
        self.assertEqual(len(groups), 2)
        self.assertEqual(len(set(groups.values())), 1)
        self.assertTrue(next(iter(groups.values())).startswith("brk:"))

    def test_separate_ads_still_support(self):
        pid = self._two_pieces(second=(50.0, 60.0))
        d = campaigns.build_customer_draft(self.con, self.cid)
        self.assertEqual(d["allocation"]["evaluations"][pid]["outcome"], "supported")
        self.assertEqual(len(set(d["pins"]["ad_groups"][pid].values())), 2)

    def test_a_regroup_stales_the_frozen_report(self):
        pid = self._two_pieces(expected=1)
        d = campaigns.build_customer_draft(self.con, self.cid)
        self.assertEqual(d["allocation"]["evaluations"][pid]["outcome"], "supported")
        rev = self.con.execute("SELECT revision FROM campaigns WHERE id=?", (self.cid,)).fetchone()[0]
        frozen = campaigns.freeze_customer_report(self.con, self.cid, rev, d["pins"])
        body = campaigns.get_customer_report_html(self.con, self.cid, frozen["report_no"])
        report = campaigns.list_customer_reports(self.con, self.cid)[0]
        self.assertFalse(report["stale"], report["stale_reasons"])
        # y moves apart: the two pieces become two ads (a regroup).
        self.con.execute("UPDATE evidence SET orig_start_s=50.0, orig_end_s=60.0 WHERE id='y'")
        self.con.commit()
        report = campaigns.list_customer_reports(self.con, self.cid)[0]
        self.assertTrue(report["stale"])
        self.assertIn("ad_groups", report["stale_reasons"])
        self.assertEqual(campaigns.get_customer_report_html(self.con, self.cid,
                                                            frozen["report_no"]), body)

    def test_pins_frozen_without_the_mapping_are_not_stale_for_it(self):
        current = {"ad_groups": {"p1": {"o1": "brk:a"}}}
        self.assertNotIn("ad_groups", campaigns._stale_reasons(current, {"placements": {}}))
        self.assertIn("ad_groups", campaigns._stale_reasons(
            current, {"ad_groups": {"p1": {"o1": "brk:b"}}}))


class WaitingCountsAds(unittest.TestCase):
    """_waiting_for_review counts ads: overlapping waiting reads are one."""

    def setUp(self):
        from tests import test_uat_dec_cov as cov
        self.cov = cov
        self.con = fresh_db()
        self.addCleanup(self.con.close)
        tv = cov.seed_legacy_episode(self.con, "ep-1", 60.0, 3600.0)
        from earwitness.app import coverage
        for stage in cov.ALL_STAGES:
            coverage.record_attestation(self.con, "ep-1", stage, (0.0, 3600.0), (0.0, 3600.0),
                                        source_pin=f"source-version:{tv}",
                                        producer="test-fixture", actor="owner")
        self.con.commit()

    def waiting(self):
        return campaigns._waiting_for_review(self.con, ["ep-1"], "Acme", None)

    def test_overlapping_waiting_reads_are_one_ad(self):
        add_candidate(self.con, "det:1", episode_id="ep-1", start=100.0, end=160.0)
        add_candidate(self.con, "det:2", episode_id="ep-1", start=120.0, end=180.0)
        self.assertEqual(self.waiting(), 1)
        add_candidate(self.con, "det:3", episode_id="ep-1", start=300.0, end=360.0)
        self.assertEqual(self.waiting(), 2)

    def test_a_waiting_read_inside_a_settled_ad_makes_it_wait(self):
        add_candidate(self.con, "det:1", episode_id="ep-1", start=100.0, end=160.0)
        self.cov.review(self.con, "det:1", "ad")
        self.cov.set_brand(self.con, "det:1", "Acme", basis="confirmed")
        self.assertEqual(self.waiting(), 0)
        add_candidate(self.con, "det:2", episode_id="ep-1", start=120.0, end=180.0)
        self.assertEqual(self.waiting(), 1)

    def test_a_read_overlapping_two_reads_joins_them_into_one_ad(self):
        # det:unnamed and det:acme do not overlap: two ads.
        add_candidate(self.con, "det:unnamed", episode_id="ep-1", start=100.0, end=160.0)
        add_candidate(self.con, "det:acme", episode_id="ep-1", start=200.0, end=260.0)
        self.cov.set_brand(self.con, "det:acme", "ACME")
        self.assertEqual(self.waiting(), 2)
        # Observations at 0-1800 on the same copy overlap both; their web
        # addresses never split, so all four reads are one ad.
        self.cov.add_observation_read(self.con, "ep-1", attributions=[("Progressive", "suggested")])
        self.cov.add_observation_read(self.con, "ep-1", attributions=[("acme", "suggested")])
        self.assertEqual(self.waiting(), 1)

    def test_a_read_naming_only_another_brand_adds_no_ad(self):
        add_candidate(self.con, "det:unnamed", episode_id="ep-1", start=100.0, end=160.0)
        self.cov.add_observation_read(self.con, "ep-1", attributions=[("Progressive", "suggested")],
                                      start=400.0, end=460.0)
        self.assertEqual(self.waiting(), 1)
        self.cov.add_observation_read(self.con, "ep-1", attributions=[("acme", "suggested")],
                                      start=600.0, end=660.0)
        self.assertEqual(self.waiting(), 2)


# -- frozen report /4 vocabulary ---------------------------------------------------

VOCAB = re.compile(r"\b(reads?|detections?|ad breaks?|pods?|spots?)\b", re.I)


class ReportFourVocabulary(unittest.TestCase):
    TECH = "<h2>Technical record</h2>"

    def test_verdict_lines_use_ad_vocabulary(self):
        def frozen(linkages):
            placements = [{"id": f"p{i}", "scope": "episode", "episode_id": "ep-1"}
                          for i in range(len(linkages))]
            covs, evs = {}, {}
            outcome = {"possible": "supported", "none": "no_observation_in_covered_sources",
                       "unreviewed": "unreviewed_detections_in_scope"}
            for p, kind in zip(placements, linkages):
                covs[p["id"]] = {"state": "complete", "waiting_for_review": 2}
                evs[p["id"]] = {"outcome": outcome[kind]}
            return {"placements": placements, "coverage_by_placement": covs,
                    "evaluations": evs, "allocation": {"citations": {}}}
        line = campaigns._report_verdict(frozen(["possible", "none", "unreviewed"]))
        self.assertEqual(line, "2 of 3 counted placements have a result: 1 with an ad found and 1 with"
                               " no matching ad in the available transcript; 1 with ads waiting"
                               " for review.")
        self.assertEqual(campaigns._report_verdict(frozen(["unreviewed"])),
                         "0 of 1 counted placement has a result yet — 1 with ads waiting for review.")
        html = campaigns._report_summary_html(dict(frozen(["possible", "none"]), episode_titles={}),
                                              str)
        self.assertIn("Ad found;", html)
        self.assertIn("No matching ad in the available transcript;", html)
        self.assertIsNone(VOCAB.search(html), html)

    def test_customer_text_of_a_new_report_has_no_read_nouns(self):
        con = fresh_db()
        self.addCleanup(con.close)
        seed_episode(con)
        cid = seed_campaign(con)
        pid = add_placement(con, cid, episode_id="ep-1")
        add_candidate(con, "x")
        verify_full_ad(con, "x")
        rev = con.execute("SELECT revision FROM campaigns WHERE id=?", (cid,)).fetchone()[0]
        campaigns.save_placement_evidence(con, cid, pid,
                                          citations.current_source_ref(con, "evidence", "x"), rev)
        d = campaigns.build_customer_draft(con, cid)
        rev = con.execute("SELECT revision FROM campaigns WHERE id=?", (cid,)).fetchone()[0]
        frozen = campaigns.freeze_customer_report(con, cid, rev, d["pins"])
        body = campaigns.get_customer_report_html(con, cid, frozen["report_no"])[0]
        human = re.sub(r"<[^>]+>", " ", body[:body.index(self.TECH)])
        human = human.replace("acme read", "")  # the captured passage quotes the source
        self.assertIsNone(VOCAB.search(human), VOCAB.search(human) and human)


# -- hosted and local routes ---------------------------------------------------------

class RouteTests(d1.RouteTests):
    """The new fields on the hosted ASGI and local app_server routes."""
    PORT = 18799
    HOST = f"127.0.0.1:{PORT}"
    test_alerts = test_brands_and_brand_detail = test_episode = None
    test_record = test_search = test_show_sponsors = None

    def setUp(self):
        super().setUp()
        import sqlite3
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        try:
            for ev in ("r3", "r7"):
                con.execute("INSERT INTO reviews (evidence_id, judgment, revision, updated_at)"
                            " VALUES (?, 'ad', 1, ?)", (ev, db.utcnow()))
                con.execute("UPDATE brand_assocs SET basis='confirmed' WHERE evidence_id=?", (ev,))
            _republish(con)
            customer.backfill_alert(con, self.alert_id, confirm=True)
            con.commit()
        finally:
            con.close()

    def hosted_post(self, path, body):
        raw = json.dumps(body).encode()
        scope = {"type": "http", "method": "POST", "path": "/api/v1/customer/" + path,
                 "scheme": "http",
                 "headers": [(b"host", b"127.0.0.1:8771"), (b"cookie", b"ew_session=sess-d1api"),
                             (b"x-csrf-token", b"csrf-d1"), (b"content-type", b"application/json"),
                             (b"x-earwitness-workspace-switch-revision", b"0"),
                             (b"content-length", str(len(raw)).encode())],
                 "query_string": b""}
        sent = []
        chunks = [{"type": "http.request", "body": raw, "more_body": False}]

        async def receive():
            return chunks.pop(0) if chunks else {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        asyncio.run(self.app(scope, receive, send))
        start = next(m for m in sent if m["type"] == "http.response.start")
        out = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        self.assertEqual(start["status"], 200, out[:300])
        return json.loads(out.decode())

    def local_post(self, path, body):
        raw = json.dumps(body).encode()
        handler = self.srv.Handler.__new__(self.srv.Handler)
        full = "/api/v1/customer/" + path
        handler.command, handler.path = "POST", full
        handler.request_version = "HTTP/1.1"
        handler.requestline = f"POST {full} HTTP/1.1"
        handler._headers_buffer = []
        handler.log_message = lambda *a: None
        handler.headers = {"Host": self.HOST, "Content-Length": str(len(raw)),
                           "Content-Type": "application/json", "X-CSRF-Token": "csrf-local",
                           "Origin": "http://" + self.HOST}
        handler.rfile, handler.wfile = io.BytesIO(raw), io.BytesIO()
        handler.close_connection = True
        handler.do_POST()
        head, _, rest = handler.wfile.getvalue().partition(b"\r\n\r\n")
        self.assertEqual(int(head.split(b" ")[1]), 200, rest[:300])
        return json.loads(rest.decode())

    def test_compare(self):
        qs = "brands=" + urllib.parse.quote(json.dumps(["acme"]))
        hosted, local = self.both("compare", qs)
        for out in (hosted, local):
            a = out["cells"][0]["a"]
            self.assertEqual((a["count"], a["ads"]), (5, 4))

    def test_alert_events_and_overview(self):
        hosted, local = self.both(f"alerts/{self.alert_id}")
        for out in (hosted, local):
            alert = out["alert"]
            self.assertEqual((alert["unread_event_count"], alert["unread_ad_count"]), (5, 4))
            self.assertEqual({e["logical_identity"]: e["ad_id"] for e in alert["events"]}["finding:r7"],
                             d1.bid("e1", self.srv_tv(), "r3"))
        self.assertEqual([e["ad_id"] for e in hosted["alert"]["events"]],
                         [e["ad_id"] for e in local["alert"]["events"]])
        hosted, local = self.both("overview")
        for out in (hosted, local):
            total = next(t for t in out["alert_totals"] if t["alert_id"] == self.alert_id)
            self.assertEqual((total["events"], total["ads"], total["unread_ads"]), (5, 4, 4))
            self.assertTrue(all("ad_id" in e for e in out["recent_events"]))
        self.assertEqual([e["ad_id"] for e in hosted["recent_events"]],
                         [e["ad_id"] for e in local["recent_events"]])

    def test_brands_count_confirmed_ads(self):
        # R2-05: r1, r3 and r7 are confirmed acme reads in two ads (B1;
        # B2 = r3 + r7): `confirmed` stays per read, `confirmed_ads` is ads.
        hosted, local = self.both("brands")
        for out in (hosted, local):
            rows = {b["key"]: b for b in out["brands"]}
            self.assertEqual((rows["acme"]["ad_breaks"], rows["acme"]["confirmed"],
                              rows["acme"]["confirmed_ads"]), (4, 3, 2))
            self.assertEqual((rows["beta"]["confirmed"], rows["beta"]["confirmed_ads"]), (0, 0))
        self.assertEqual(hosted["brands"], local["brands"])
        hosted, local = self.both("brands/acme")
        for out in (hosted, local):
            top = out["brand"]["top_shows"][0]
            self.assertEqual((top["reads"], top["ad_breaks"], top["confirmed"],
                              top["confirmed_ads"]), (5, 4, 3, 2))
        self.assertEqual(hosted["brand"]["top_shows"], local["brand"]["top_shows"])

    def srv_tv(self):
        import sqlite3
        con = sqlite3.connect(self.db_path)
        try:
            return con.execute("SELECT transcript_version_id FROM episodes WHERE id='e1'").fetchone()[0]
        finally:
            con.close()

    def test_export(self):
        for post in (self.hosted_post, self.local_post):
            q = {"version": 2, "mode": "ads"}
            rq = post("research-query", {"query": q, "selector": None, "summaries": []})
            out = post("research-export", {"query": q, "selector": None, "scope": "all_matches",
                                           "format": "json",
                                           "expected_view_revision": rq["revision"]})
            self.assertEqual((out["item_count"], out["ad_count"]), (7, 6), post.__name__)


if __name__ == "__main__":
    unittest.main()
