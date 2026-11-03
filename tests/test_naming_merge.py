"""Brand namer span rules from the 2026-10-10 engine check
(.corpus-scale-20261008/engine-validation.md): the "both" merge rule for a
read cut by a window edge (fix 1) and a second brand per span for ad pods
(fix 3), plus their export shape. Offline: no GLiNER, no system word list."""
from __future__ import annotations

import unittest

from earwitness.pipeline import brand_namer as bn
from earwitness.pipeline import refine as rf

NO_WORDS = frozenset()
TALK = "ordinary talk about the news and the weather"


def namer():
    return bn.BrandNamer([], None, words=NO_WORDS)


def episode(texts, noul):
    """One 60-s window per text, one segment each."""
    segs = [{"start": 60.0 * i, "end": 60.0 * i + 60, "text": t} for i, t in enumerate(texts)]
    ws = [{"window_id": f"window_{i:03d}", "start_s": 60.0 * i, "end_s": 60.0 * i + 60,
           "noul": n} for i, n in enumerate(noul)]
    return segs, ws


# A read across the 60/120 edge: its first half names a weak generic word
# (needs review), its second half the sponsor's domain.
SPLIT = [TALK, "thinking about therapy? check out therapy",
         "visit betterhelp dot com slash pod today, betterhelp dot com slash pod", TALK]
# Two confident halves, the second brand among the first half's candidates.
OVERLAP = [TALK, "brought to you by quillow. go to quillow dot com, quillow dot com. "
           "and go to northwind dot com",
           "go to northwind dot com, northwind dot com slash pod. thanks northwind", TALK]
# Back-to-back spots with no shared evidence stay apart.
BACK_TO_BACK = [TALK, "this episode is brought to you by quillow. go to quillow dot com slash "
                "cozy, quillow dot com",
                "today's sponsor is northwind. visit northwind dot com, northwind dot com", TALK]
NOUL4 = [0.05, 0.95, 0.9, 0.05]
# An ad pod: two spots in one 60-s window.
POD = [TALK, "this episode is brought to you by quillow. go to quillow dot com slash cozy, "
       "quillow dot com. also from wayfair. head to wayfair dot com, wayfair dot com for deals",
       TALK]


def spans(texts, noul, **kw):
    segs, ws = episode(texts, noul)
    kw.setdefault("context_windows", 0)
    return bn.name_windows(segs, ws, namer(), **kw)


class MergeRuleTest(unittest.TestCase):
    def test_default_rule_is_both(self):
        self.assertEqual(bn.DEFAULT_MERGE_RULE, "both")

    def test_fixture_preconditions(self):
        n = namer()
        weak = n.name_span(SPLIT[1])
        self.assertTrue(weak["needs_review"])
        self.assertEqual(weak["brand"], "therapy")
        a, b = n.name_span(OVERLAP[1]), n.name_span(OVERLAP[2])
        self.assertFalse(a["needs_review"] or b["needs_review"])
        self.assertIn("northwind", [c["brand"] for c in a["candidates"]])

    def test_deployed_rule_splits_a_read_cut_by_a_window_edge(self):
        got = [(s["start_s"], s["end_s"], s["brand"])
               for s in spans(SPLIT, NOUL4, merge_rule="deployed")]
        self.assertEqual(got, [(60.0, 120.0, "therapy"), (120.0, 180.0, "betterhelp")])

    def test_needs_review_neighbour_joins_and_the_confident_window_names_it(self):
        sp = spans(SPLIT, NOUL4)
        self.assertEqual([(s["start_s"], s["end_s"], s["brand"], s["jev_window_ids"])
                          for s in sp],
                         [(60.0, 180.0, "betterhelp", ["window_001", "window_002"])])
        self.assertFalse(sp[0]["needs_review"])
        self.assertEqual(sp[0]["method"], "cta")

    def test_confident_different_brands_never_join_through_candidates(self):
        # both halves are confident: one in the other's candidates is not enough
        self.assertEqual([(s["start_s"], s["end_s"], s["brand"]) for s in spans(OVERLAP, NOUL4)],
                         [(60.0, 120.0, "quillow"), (120.0, 180.0, "northwind")])

    def test_confident_back_to_back_spots_stay_apart(self):
        self.assertEqual([s["brand"] for s in spans(BACK_TO_BACK, NOUL4)],
                         ["quillow", "northwind"])

    def test_unknown_rule_is_refused(self):
        with self.assertRaises(ValueError):
            spans(SPLIT, NOUL4, merge_rule="always")

    def test_name_episode_records_the_rule(self):
        segs, ws = episode(SPLIT, NOUL4)
        doc = bn.name_episode("e", segs, {"windows": ws}, namer(), context_windows=0)
        self.assertEqual((doc["merge_rule"], doc["second_brand_min_score"]),
                         ("both", bn.SECOND_BRAND_MIN_SCORE))
        dep = bn.name_episode("e", segs, {"windows": ws}, namer(), context_windows=0,
                              merge_rule="deployed", second_brand_min_score=None)
        self.assertEqual((dep["merge_rule"], dep["second_brand_min_score"]), ("deployed", None))
        self.assertEqual(len(dep["spans"]), 2)

    def test_rename_span_takes_the_most_confident_window(self):
        segs, ws = episode(OVERLAP, NOUL4)
        span = {"span_id": "60000-180000", "start_s": 60.0, "end_s": 180.0, "brand": "x",
                "jev_window_ids": ["window_001", "window_002"]}
        got = bn.rename_span(span, segs, ws, namer())
        self.assertEqual((got["brand"], got["span_id"]), ("quillow", "60000-180000"))
        # windows reversed in confidence: the more confident second window wins
        segs2, ws2 = episode([TALK, SPLIT[1], SPLIT[2], TALK], NOUL4)
        self.assertEqual(bn.rename_span(span, segs2, ws2, namer())["brand"], "betterhelp")
        self.assertEqual(bn.rename_span(span, segs2, ws2, namer(),
                                        merge_rule="deployed")["brand"], "therapy")


class SecondBrandTest(unittest.TestCase):
    def test_pod_window_gets_a_second_brand_with_its_own_span(self):
        sp = spans(POD, [0.05, 0.95, 0.05])
        self.assertEqual(len(sp), 1)
        self.assertEqual(sp[0]["brand"], "quillow")
        sb = sp[0]["second_brands"]
        self.assertEqual([(s["brand"], s["start_s"], s["end_s"], s["jev_window_ids"])
                          for s in sb], [("wayfair", 60.0, 120.0, ["window_001"])])
        self.assertGreaterEqual(sb[0]["score"], bn.SECOND_BRAND_MIN_SCORE)
        self.assertIn("domain", sb[0]["evidence"])

    def test_a_neighbouring_spans_brand_can_be_a_second_brand(self):
        # a pod spot crossing a window edge: the next window names it confidently,
        # this window speaks its domain too
        texts = [TALK, "brought to you by kachava. go to kachava dot com slash pod, kachava "
                 "dot com, kachava dot com. and go to northwind dot com, northwind dot com",
                 read("northwind", 1), TALK]
        sp = spans(texts, NOUL4)
        self.assertEqual([(s["start_s"], s["end_s"], s["brand"]) for s in sp],
                         [(60.0, 120.0, "kachava"), (120.0, 180.0, "northwind")])
        self.assertEqual([(s["brand"], s["start_s"], s["end_s"])
                          for s in sp[0]["second_brands"]], [("northwind", 60.0, 120.0)])

    def test_second_brand_span_is_the_windows_that_speak_its_domain(self):
        texts = [TALK, "brought to you by quillow. go to quillow dot com, quillow dot com",
                 "more from quillow, quillow dot com slash cozy, quillow dot com, quillow dot "
                 "com. and go to northwind dot com, northwind dot com", TALK]
        sp = spans(texts, NOUL4)
        self.assertEqual(len(sp), 1)
        self.assertEqual([(s["brand"], s["start_s"], s["end_s"]) for s in sp[0]["second_brands"]],
                         [("northwind", 120.0, 180.0)])

    def test_no_second_brand_below_the_score_or_off(self):
        self.assertEqual(spans(POD, [0.05, 0.95, 0.05], second_brand_min_score=50.0)[0]
                         ["second_brands"], [])
        self.assertEqual(spans(POD, [0.05, 0.95, 0.05], second_brand_min_score=None)[0]
                         ["second_brands"], [])
        self.assertEqual(spans(BACK_TO_BACK, NOUL4)[0]["second_brands"], [])

    def test_sound_alike_runner_up_is_not_a_second_brand(self):
        text = ("brought to you by quillow. go to quillow dot com, quillow dot com. "
                "or quilow dot com")
        self.assertEqual(spans([TALK, text, TALK], [0.05, 0.95, 0.05])[0]["second_brands"], [])

    def test_export_shape_second_read_and_backward_compatible_primary(self):
        segs, ws = episode(POD, [0.05, 0.95, 0.05])
        doc = bn.name_episode("ep_x", segs, {"windows": ws}, namer(), context_windows=0)
        cands = bn.named_candidates("ep_x", doc, segs, {"show_title": "S"})
        self.assertEqual([c["candidate_id"] for c in cands],
                         ["ep_x.named.60000-120000", "ep_x.named.60000-120000.b-wayfair"])
        prim, sec = cands
        self.assertNotIn("second_brand_of", prim)
        self.assertEqual((sec["brand"], sec["brand_spoken"], sec["brand_raw"],
                          sec["second_brand_of"], sec["start_s"], sec["end_s"], sec["source"]),
                         ("wayfair", "wayfair", "wayfair", "quillow", 60.0, 120.0, "jev+namer"))
        self.assertEqual(sec["needs_review"], prim["needs_review"])
        self.assertEqual(sec["provenance"]["second_brand"]["of"], "quillow")
        self.assertIn("wayfair dot com", sec["excerpt"])
        # a names doc from before the fix (no second_brands) exports as before
        old = dict(doc, spans=[{k: v for k, v in s.items() if k != "second_brands"}
                               for s in doc["spans"]])
        self.assertEqual(bn.named_candidates("ep_x", old, segs, {"show_title": "S"}), [prim])
        self.assertEqual(len(bn.named_candidates("ep_x", doc, segs, confident_only=True)), 2)


def read(brand, k):
    d = brand.replace(" ", "")
    return (f"this episode is brought to you by {brand}. go to {d} dot com slash pod" if k == 0
            else f"that's {d} dot com slash pod, {d} dot com")


SIX = ["athletic greens", "nordvpn", "quillow", "northwind", "wayfair", "betterhelp"]
UNSURE = ["check out therapy", "check out comfort", "check out savings", "check out sleep"]


class ChainTest(unittest.TestCase):
    """Review of 897e9f2: "both" chained a whole pod into one span."""

    def test_fixture_preconditions(self):
        n = namer()
        for t in UNSURE:
            self.assertTrue(n.name_span(t)["needs_review"], t)
        for b in SIX:
            self.assertFalse(n.name_span(read(b, 0))["needs_review"], b)

    def test_six_confident_brands_in_a_row_stay_six_spans(self):
        texts = [read(b, k) for b in SIX for k in (0, 1)]
        for cw in (0, 1):
            sp = spans(texts, [0.95] * 12, context_windows=cw)
            self.assertEqual([(s["start_s"], s["end_s"]) for s in sp],
                             [(120.0 * i, 120.0 * i + 120) for i in range(6)], cw)
            self.assertEqual([bn.compact(s["brand"]) for s in sp],
                             [bn.compact(b) for b in SIX], cw)
        self.assertEqual([s["second_brands"] for s in spans(texts, [0.95] * 12)], [[]] * 6)

    def test_unsure_window_never_bridges_two_confident_brands(self):
        texts = [TALK, read("quillow", 0), UNSURE[0], read("northwind", 0), TALK]
        sp = spans(texts, [0.05, 0.95, 0.95, 0.95, 0.05])
        self.assertEqual([(s["start_s"], s["end_s"], s["brand"]) for s in sp],
                         [(60.0, 180.0, "quillow"), (180.0, 240.0, "northwind")])

    def test_unsure_run_is_capped(self):
        texts = [TALK, read("quillow", 0)] + UNSURE + [TALK]
        noul = [0.05] + [0.95] * 5 + [0.05]
        self.assertEqual(bn.MAX_SPAN_WINDOWS, 3)
        self.assertEqual([(s["start_s"], s["end_s"]) for s in spans(texts, noul)],
                         [(60.0, 240.0), (240.0, 360.0)])
        self.assertEqual([(s["start_s"], s["end_s"])
                          for s in spans(texts, noul, max_span_windows=2)],
                         [(60.0, 180.0), (180.0, 300.0), (300.0, 360.0)])
        self.assertEqual([(s["start_s"], s["end_s"])
                          for s in spans(texts, noul, max_span_windows=None)],
                         [(60.0, 360.0)])

    def test_a_long_read_of_one_brand_is_not_capped(self):
        texts = [TALK] + [read("quillow", k % 2) for k in range(5)] + [TALK]
        sp = spans(texts, [0.05] + [0.95] * 5 + [0.05])
        self.assertEqual([(s["start_s"], s["end_s"], s["brand"]) for s in sp],
                         [(60.0, 360.0, "quillow")])

    def test_unnamed_windows_do_not_count_toward_the_cap(self):
        self.assertIsNone(namer().name_span(TALK)["brand"])
        texts = [TALK, TALK, TALK, read("quillow", 0)]
        for rule in ("both", "deployed"):
            self.assertEqual([(s["start_s"], s["end_s"], s["brand"])
                              for s in spans(texts, [0.95] * 4, merge_rule=rule)],
                             [(0.0, 240.0, "quillow")], rule)

    def test_unnamed_windows_inside_one_read_keep_it_one_span(self):
        texts = [read("quillow", 0)] + [TALK] * 4 + [read("quillow", 1)]
        self.assertEqual([(s["start_s"], s["end_s"], s["brand"])
                          for s in spans(texts, [0.95] * 6)], [(0.0, 360.0, "quillow")])

    def test_deployed_rule_is_unchanged_by_the_cap(self):
        texts = [TALK, read("quillow", 0)] + UNSURE + [TALK]
        noul = [0.05] + [0.95] * 5 + [0.05]
        self.assertEqual(len(spans(texts, noul, merge_rule="deployed")), 5)

    def test_name_episode_records_the_cap(self):
        segs, ws = episode(SPLIT, NOUL4)
        doc = bn.name_episode("e", segs, {"windows": ws}, namer(), context_windows=0,
                              max_span_windows=2)
        self.assertEqual(doc["max_span_windows"], 2)


class SecondBrandCandidatesTest(unittest.TestCase):
    def test_a_runner_up_ranked_above_the_span_brand_is_considered(self):
        w = {"window_id": "w0", "start_s": 0.0, "end_s": 60.0}
        cands = [{"brand": "wayfair", "score": 12.0, "evidence": ["domain"]},
                 {"brand": "quillow", "score": 10.0, "evidence": ["domain"]}]
        res = {"candidates": cands}
        got = bn._second_brands("quillow", cands, [(w, res)], 7.0)
        self.assertEqual([(g["brand"], g["rank"]) for g in got], [("wayfair", 1)])


class RefineKeepsRuleTest(unittest.TestCase):
    def test_refine_rename_passes_the_rule_and_carries_second_brands(self):
        segs, ws = episode(SPLIT, NOUL4)
        jev = {"windows": ws}
        base = bn.name_episode("e", segs, jev, namer(), context_windows=0,
                               merge_rule="deployed")
        self.assertEqual(len(base["spans"]), 2)
        refined = {"backend": "mlx", "clips": [{"start_s": 60.0, "end_s": 120.0, "segments": [
            {"start": 60.0, "end": 120.0, "text": POD[1]}]}]}
        doc = rf.rename("e", base, jev, segs, refined, namer(), model="large-v3-turbo",
                        threshold=0.8, context_windows=0)
        # ranges are the deployed naming's; only the span brands follow "both"
        self.assertEqual((doc["merge_rule"], doc["span_brand_rule"]), ("deployed", "both"))
        first = doc["spans"][0]
        self.assertEqual((first["brand"], [s["brand"] for s in first["second_brands"]]),
                         ("quillow", ["wayfair"]))


if __name__ == "__main__":
    unittest.main()
