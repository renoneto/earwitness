"""Pod naming with Jev choice questions (earwitness/pipeline/pod_naming.py):
pieces, candidate pool, request shape, strict response validation, spans
from per-piece choices, the response cache and budget, the pod pass over a
names doc, review reasons, respelling and show-notes sponsors. Offline: a
fake Jev transport, no GLiNER, no system word list."""
from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from earwitness.pipeline import brand_namer as bn
from earwitness.pipeline import pod_naming as pod

NO_WORDS = frozenset()
TALK = "ordinary talk about the news and the weather today"


def namer():
    return bn.BrandNamer([], None, words=NO_WORDS)


def segs_of(texts, step=10.0, t0=0.0):
    return [{"start": t0 + step * i, "end": t0 + step * (i + 1), "text": t}
            for i, t in enumerate(texts)]


def answer(choice, opts, p=0.9):
    rest = [o for o in opts if o != choice]
    probs = {o: (1 - p) / len(rest) for o in rest}
    probs[choice] = p
    return {"type": "choice", "choice": choice, "confidence": p, "probabilities": probs}


class FakeJev:
    """Answers each piece with `pick(piece text, options)`."""

    def __init__(self, pick):
        self.pick, self.bodies = pick, []

    def __call__(self, body):
        self.bodies.append(body)
        out = {}
        for qid, q in body["questions"].items():
            opts = list(q["criteria"])
            text = body["state"]["pieces"][qid]["transcript"]
            out[qid] = answer(self.pick(text, opts), opts)
        return {"model": "jev-1.13.0", "answers": out,
                "usage": {"input_tokens": 1000, "output_tokens": 10}}


def by_domain(text, opts):
    """The option whose name the piece says as a domain, else none."""
    for o in opts:
        if o not in (pod.NONE, pod.OTHER) and f"{bn.compact(o)} dot com" in text:
            return o
    return pod.NONE


class PiecesTest(unittest.TestCase):
    def test_pieces_cover_the_run_at_segment_edges(self):
        segs = segs_of(["a"] * 9, step=5.0, t0=60.0)
        ps = pod.pieces(segs, 60.0, 105.0, 15.0)
        self.assertEqual(ps[0][0], 60.0)
        self.assertEqual(ps[-1][1], 105.0)
        for (a, b), (c, d) in zip(ps, ps[1:]):
            self.assertEqual(b, c)
        self.assertEqual(ps, [(60.0, 75.0), (75.0, 90.0), (90.0, 105.0)])

    def test_opener_starts_a_piece(self):
        segs = segs_of(["talk", "talk", "this episode is brought to you by acme", "talk"],
                       step=5.0)
        self.assertIn(10.0, [a for a, _ in pod.pieces(segs, 0.0, 20.0, 15.0)])

    def test_short_tail_joins_previous_piece(self):
        segs = segs_of(["a"] * 4, step=5.0)  # 0-20 s, size 15: tail 15-20 is < 5 s? = 5
        ps = pod.pieces(segs + [{"start": 20.0, "end": 22.0, "text": "b"}], 0.0, 22.0, 15.0)
        self.assertEqual(ps[-1][1], 22.0)
        self.assertTrue(all(b - a >= 5.0 for a, b in ps))

    def test_short_tail_of_two_groups_joins(self):
        # blind2 one-shot crash: exactly two groups with a short tail raised
        # IndexError (groups[-2] += groups.pop())
        segs = [{"start": 0.0, "end": 20.0, "text": "a"}, {"start": 20.0, "end": 23.0,
                                                           "text": "b"}]
        self.assertEqual(pod.pieces(segs, 0.0, 23.0, 20.0), [(0.0, 23.0)])

    def test_no_segments_is_one_piece(self):
        self.assertEqual(pod.pieces([], 0.0, 60.0), [(0.0, 60.0)])


class PoolTest(unittest.TestCase):
    def test_span_brands_first_then_candidates_then_extras_deduped(self):
        spans = [{"brand": "quillow", "candidates": [{"brand": "quillow", "score": 9.0}],
                  "second_brands": [{"brand": "wayfair"}]}]
        pieces = [{"candidates": [{"brand": "northwind", "score": 2.0},
                                  {"brand": "Quillow", "score": 1.0}]}]
        pool = pod.candidate_pool(spans, pieces, extra=["Wayfair", "Blissy"])
        self.assertEqual(pool, ["quillow", "wayfair", "northwind", "Blissy"])

    def test_names_sharing_a_word_stay_apart(self):
        pool = pod.candidate_pool([], [{"candidates": [{"brand": "amazon", "score": 3.0},
                                                       {"brand": "amazon music", "score": 2.0},
                                                       {"brand": "Amazon", "score": 1.0}]}])
        self.assertEqual(pool, ["amazon", "amazon music"])

    def test_reserved_names_never_offered(self):
        pool = pod.candidate_pool([], [{"candidates": [{"brand": "none", "score": 5},
                                                       {"brand": "other", "score": 5}]}])
        self.assertEqual(pool, [])

    def test_cap_keeps_best(self):
        pieces = [{"candidates": [{"brand": f"brand{i:02d}x", "score": float(i)}
                                  for i in range(40)]}]
        pool = pod.candidate_pool([], pieces, max_n=5)
        self.assertEqual(len(pool), 5)
        self.assertEqual(pool[0], "brand39x")


class RequestAndValidationTest(unittest.TestCase):
    def setUp(self):
        self.body = pod.build_request("ep", [(0.0, 15.0), (15.0, 30.0)], ["t1", "t2"],
                                      ["acme", "zeta"])

    def test_request_shape(self):
        b = self.body
        self.assertEqual(set(b["questions"]), {"p00", "p01"})
        q = b["questions"]["p00"]
        self.assertEqual(q["type"], "choice")
        self.assertEqual(list(q["criteria"]), ["acme", "zeta", pod.NONE, pod.OTHER])
        self.assertEqual(b["state"]["pieces"]["p01"]["transcript"], "t2")

    def test_request_key_is_stable(self):
        again = pod.build_request("ep", [(0.0, 15.0), (15.0, 30.0)], ["t1", "t2"],
                                  ["acme", "zeta"])
        self.assertEqual(pod.request_key(self.body), pod.request_key(again))

    def good(self):
        opts = list(self.body["questions"]["p00"]["criteria"])
        return {"answers": {"p00": answer("acme", opts), "p01": answer(pod.NONE, opts)}}

    def test_parse_good(self):
        out = pod.parse_choices(self.body, self.good())
        self.assertEqual(out["p00"]["choice"], "acme")
        self.assertAlmostEqual(out["p00"]["p"], 0.9)

    def test_rejects_bad_answers(self):
        cases = []
        r = self.good(); del r["answers"]["p01"]; cases.append(r)
        r = self.good(); r["answers"]["p00"]["choice"] = "unlisted"; cases.append(r)
        r = self.good(); r["answers"]["p00"]["probabilities"]["acme"] = math.nan; cases.append(r)
        r = self.good(); r["answers"]["p00"]["probabilities"]["acme"] = 3.0; cases.append(r)
        r = self.good(); del r["answers"]["p00"]["probabilities"]["zeta"]; cases.append(r)
        r = self.good(); r["answers"]["p00"]["confidence"] = None; cases.append(r)
        r = self.good(); r["answers"]["p00"]["type"] = "noul"; cases.append(r)
        cases.append({"answers": []})
        cases.append(None)
        for bad in cases:
            with self.assertRaises(ValueError):
                pod.parse_choices(self.body, bad)


class SpansFromChoicesTest(unittest.TestCase):
    PS = [(0.0, 15.0), (15.0, 30.0), (30.0, 45.0), (45.0, 60.0), (60.0, 75.0)]

    def ch(self, labels):
        return [{"choice": c, "p": 0.9} for c in labels]

    def test_two_spots_split_and_none_joins(self):
        out = pod.spans_from_choices(self.PS, self.ch(["a", "a", "none", "b", "b"]))
        self.assertEqual([(lab, a, b) for lab, a, b, _ in out],
                         [("a", 0.0, 30.0), ("b", 30.0, 75.0)])

    def test_bridge_one_gap(self):
        out = pod.spans_from_choices(self.PS, self.ch(["a", "other", "a", "b", "b"]))
        self.assertEqual([lab for lab, *_ in out], ["a", "b"])
        self.assertEqual(out[0][1:3], (0.0, 45.0))

    def test_drop_policy_leaves_none_time_out(self):
        out = pod.spans_from_choices(self.PS, self.ch(["none", "a", "a", "none", "none"]),
                                     none_policy="drop")
        self.assertEqual([(lab, a, b) for lab, a, b, _ in out], [("a", 15.0, 45.0)])

    def test_join_policy_run_edges(self):
        out = pod.spans_from_choices(self.PS, self.ch(["none", "a", "a", "none", "none"]))
        self.assertEqual([(lab, a, b) for lab, a, b, _ in out], [("a", 0.0, 75.0)])

    def test_other_is_its_own_span(self):
        out = pod.spans_from_choices(self.PS, self.ch(["a", "a", "other", "other", "none"]))
        self.assertEqual([lab for lab, *_ in out], ["a", "other"])

    def test_no_listed_brand(self):
        self.assertEqual(pod.spans_from_choices(self.PS, self.ch(["none", "other"] * 2
                                                                 + ["none"])), [])

    def test_bad_policy(self):
        with self.assertRaises(ValueError):
            pod.spans_from_choices(self.PS, self.ch(["a"] * 5), none_policy="x")


class TidyTest(unittest.TestCase):
    def ch(self, pairs):
        return [{"choice": c, "p": p} for c, p in pairs]

    def test_bridge_any_one_piece(self):
        c = self.ch([("a", .9), ("tool", .9), ("a", .9), ("b", .9), ("x", .9), ("y", .9)])
        self.assertEqual(pod.tidy_labels(c), ["a", "tool", "a", "b", "x", "y"])
        self.assertEqual(pod.tidy_labels(c, ("bridge_any",)),
                         ["a", "a", "a", "b", "x", "y"])

    def test_merge_same_brand_spellings(self):
        c = self.ch([("helix", .5), ("helix", .5), ("helixsleep", .9), ("quince", .9)])
        self.assertEqual(pod.tidy_labels(c, ("merge_same",)),
                         ["helix", "helix", "helix", "quince"])
        c = self.ch([("helix", .3), ("helixsleep", .9), ("helixsleep", .9)])
        self.assertEqual(pod.tidy_labels(c, ("merge_same",)), ["helixsleep"] * 3)

    def test_absorb_unsure_into_sure_neighbour(self):
        c = self.ch([("therapy", .4), ("therapy", .5), ("betterhelp", .9), ("betterhelp", .95),
                     ("betterhelp", .9), ("none", .9)])
        self.assertEqual(pod.tidy_labels(c, ("absorb_unsure",)),
                         ["betterhelp"] * 5 + ["none"])
        # a sure short spot is never absorbed; an unsure one alone stays
        c = self.ch([("a", .9), ("b", .9), ("a", .9), ("a", .9)])
        self.assertEqual(pod.tidy_labels(c, ("absorb_unsure",)), ["a", "b", "a", "a"])
        c = self.ch([("none", .9), ("b", .3), ("none", .9)])
        self.assertEqual(pod.tidy_labels(c, ("absorb_unsure",)), ["none", "b", "none"])

    def test_unknown_rule(self):
        with self.assertRaises(ValueError):
            pod.tidy_labels(self.ch([("a", .9)]), ("nope",))


class CachedAskTest(unittest.TestCase):
    def test_second_call_is_free_and_persisted(self):
        with tempfile.TemporaryDirectory() as d:
            jev = FakeJev(lambda t, o: o[0])
            ask = pod.CachedAsk(d, jev)
            body = pod.build_request("ep", [(0.0, 15.0)], ["hi"], ["acme"])
            r1 = ask("ep", body)
            r2 = ask("ep", body)
            self.assertEqual(r1, r2)
            self.assertEqual((ask.calls, ask.cached), (1, 1))
            self.assertGreater(ask.report()["cost_usd"], 0)
            again = pod.CachedAsk(d, None, offline=True)
            self.assertEqual(again("ep", body), r1)
            self.assertEqual(len(jev.bodies), 1)
            self.assertTrue((Path(d) / pod.POD_DIR / "ep.json").is_file())

    def test_offline_miss_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            ask = pod.CachedAsk(d, None, offline=True)
            self.assertIsNone(ask("ep", pod.build_request("ep", [(0.0, 1.0)], ["x"], ["a"])))

    def test_budget_stops_before_paying(self):
        with tempfile.TemporaryDirectory() as d:
            jev = FakeJev(lambda t, o: o[0])
            ask = pod.CachedAsk(d, jev, max_usd=0.0)
            with self.assertRaises(pod.PodJevBudget):
                ask("ep", pod.build_request("ep", [(0.0, 1.0)], ["x"], ["a"]))
            self.assertEqual(jev.bodies, [])


# A 60-s flagged window holding two produced spots, each saying its domain.
POD_TEXTS = ["this episode is brought to you by quillow.", "go to quillow dot com slash cozy",
             "quillow dot com for cozy pillows.", "and now a word from northwind.",
             "visit northwind dot com, northwind dot com", "northwind dot com today."]


def pod_episode():
    segs = segs_of([TALK] * 6 + POD_TEXTS + [TALK] * 6)
    ws = [{"window_id": f"window_{i:03d}", "start_s": 60.0 * i, "end_s": 60.0 * i + 60,
           "noul": n} for i, n in enumerate([0.05, 0.95, 0.05])]
    return segs, ws


class PodRenameTest(unittest.TestCase):
    def test_pod_window_becomes_two_spans(self):
        segs, ws = pod_episode()
        n = namer()
        doc = bn.name_episode("ep", segs, {"windows": ws}, n, context_windows=0)
        self.assertEqual(len(doc["spans"]), 1)  # one span, one brand (+ maybe a second)
        with tempfile.TemporaryDirectory() as d:
            ask = pod.CachedAsk(d, FakeJev(by_domain))
            new, rep = pod.pod_rename(doc, segs, {"windows": ws}, n, ask, piece_s=10.0)
        brands = [(sp["brand"], sp["start_s"], sp["end_s"]) for sp in new["spans"]]
        self.assertEqual([b for b, *_ in brands], ["quillow", "northwind"])
        self.assertEqual(brands[0][1], 60.0)
        self.assertEqual(brands[-1][2], 120.0)
        self.assertTrue(all(sp["pod"] and sp["method"] == "jev-choice"
                            and sp["second_brands"] == [] for sp in new["spans"]))
        self.assertEqual(rep["renamed"], 1)
        self.assertEqual(new["pod_jev"]["renamed"], 1)

    def test_run_kept_when_jev_names_nothing(self):
        segs, ws = pod_episode()
        n = namer()
        doc = bn.name_episode("ep", segs, {"windows": ws}, n, context_windows=0)
        with tempfile.TemporaryDirectory() as d:
            ask = pod.CachedAsk(d, FakeJev(lambda t, o: pod.NONE))
            new, rep = pod.pod_rename(doc, segs, {"windows": ws}, n, ask)
        self.assertEqual(new["spans"], doc["spans"])
        self.assertEqual(rep["kept"], 1)

    def test_offline_without_cache_keeps_spans(self):
        segs, ws = pod_episode()
        n = namer()
        doc = bn.name_episode("ep", segs, {"windows": ws}, n, context_windows=0)
        with tempfile.TemporaryDirectory() as d:
            new, _ = pod.pod_rename(doc, segs, {"windows": ws}, n,
                                    pod.CachedAsk(d, None, offline=True))
        self.assertEqual(new["spans"], doc["spans"])

    def test_spans_outside_runs_untouched(self):
        segs, ws = pod_episode()
        ws[1]["noul"] = 0.1
        n = namer()
        doc = {"episode_id": "ep", "jev_threshold": 0.8,
               "spans": [{"span_id": "x", "start_s": 0.0, "end_s": 60.0, "brand": "keep"}]}
        with tempfile.TemporaryDirectory() as d:
            new, rep = pod.pod_rename(doc, segs, {"windows": ws}, n,
                                      pod.CachedAsk(d, FakeJev(by_domain)))
        self.assertEqual(new["spans"], doc["spans"])
        self.assertEqual(rep["runs"], 0)


class ReviewTest(unittest.TestCase):
    def test_reasons(self):
        local = {"brand": "zeta", "candidates": [{"brand": "zeta"}]}
        why = pod.review_flags("buy acme now", "acme", 0.4, local, duration_s=5.0,
                               brand_list_keys={"zeta"})
        self.assertEqual(why, ["jev_unsure", "local_disagrees", "short", "off_list"])
        why = pod.review_flags("go to acme dot com", "acme", 0.95,
                               {"brand": "acme", "candidates": [{"brand": "acme"}]},
                               duration_s=30.0, brand_list_keys={"acme"})
        self.assertEqual(why, [])

    def test_not_spoken(self):
        why = pod.review_flags("unrelated words", "acme", 0.95, {"brand": None,
                                                                 "candidates": []},
                               duration_s=30.0)
        self.assertEqual(why, ["not_spoken"])


class RespellTest(unittest.TestCase):
    def test_spoken_spacing_is_no_written_spelling(self):
        # review finding 8: the episode's spacing alone never respells
        self.assertEqual(pod.respell("applecard", "get the apple card today"),
                         ("applecard", None))
        self.assertEqual(pod.respell("applecard", "", written=["Apple Card"]),
                         ("apple card", "written"))

    def test_sponsor_spelling_wins(self):
        self.assertEqual(pod.respell("blissey", "blissey dot com", ["Blissy"]),
                         ("blissy", "sponsor"))
        self.assertEqual(pod.respell("goodpracticetherapy", "", ["good practice therapy"]),
                         ("good practice therapy", "sponsor"))

    def test_written_sponsor_name_wins_on_spacing(self):
        self.assertEqual(pod.respell("crowd strike", "", ["CrowdStrike"]),
                         ("crowdstrike", "sponsor"))
        self.assertEqual(pod.respell("helixsleep", "", ["Helix Sleep"]),
                         ("helix sleep", "sponsor"))

    def test_respace_needs_real_words(self):
        self.assertEqual(pod.respell("codex", "write code x faster"), ("codex", None))

    def test_guards(self):
        # short names never change; distinct brands are not merged
        self.assertEqual(pod.respell("ro", "r o"), ("ro", None))
        self.assertEqual(pod.respell("hers", "for hers", ["forhers"]), ("hers", None))
        self.assertEqual(pod.respell("quince", "", ["quip"]), ("quince", None))
        self.assertEqual(pod.respell("northwind", "no spacing here"), ("northwind", None))
        self.assertEqual(pod.respell(None, "x"), (None, None))

    def test_respell_doc_records_original(self):
        doc = {"spans": [{"brand": "applecard", "second_brands": [{"brand": "helixsleep"}]}]}
        segs = [{"start": 0, "end": 5, "text": "Apple Card and Helix Sleep"}]
        from earwitness.pipeline import advertisers as adv
        a = adv.Advertisers(adv.build({"seed": [{"name": "Apple Card"}, {"name": "Helix Sleep"}]},
                                      built_at="x", version=1))
        new, ch = pod.respell_doc(doc, segs, advertisers=a)
        self.assertEqual(new["spans"][0]["brand"], "apple card")
        self.assertEqual(new["spans"][0]["brand_spelled_from"], "applecard")
        self.assertEqual(new["spans"][0]["second_brands"][0]["brand"], "helix sleep")
        self.assertEqual(len(ch), 2)
        self.assertEqual(doc["spans"][0]["brand"], "applecard")  # input not mutated


class SponsorsTest(unittest.TestCase):
    def test_reads_names_and_tolerates_missing(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(pod.sponsors_of(d, "ep"), [])
            (Path(d) / "shownotes-reads").mkdir()
            (Path(d) / "shownotes-reads" / "ep.json").write_text(json.dumps({"sponsors": [
                {"name": "Gusto", "brand": "gusto"}, {"name": None, "brand": "functionhealth"},
                {"name": "", "brand": ""}, "junk"]}))
            self.assertEqual(pod.sponsors_of(d, "ep"), ["Gusto", "functionhealth"])
            # a compact listing key is not a spelling target
            self.assertEqual(pod.sponsors_of(d, "ep", written_only=True), ["Gusto"])


if __name__ == "__main__":
    unittest.main()


class OtherNextToItsBrandTest(unittest.TestCase):
    """Review follow-up (finding 4's duplicate neighbours): a piece Jev
    calls "other" whose own name is the brand of the piece next to it joins
    that piece's span; it never makes a second span of the same brand."""

    def test_other_piece_joins_its_same_brand_neighbour(self):
        segs, ws = pod_episode()
        n = namer()
        doc = bn.name_episode("ep", segs, {"windows": ws}, n, context_windows=0)

        def pick(text, opts):
            # quillow pieces: the first by name, the next as "other"
            if "quillow" in text:
                pick.n = getattr(pick, "n", 0) + 1
                return "quillow" if pick.n == 1 else pod.OTHER
            return by_domain(text, opts)

        with tempfile.TemporaryDirectory() as d:
            new, _ = pod.pod_rename(doc, segs, {"windows": ws}, n,
                                    pod.CachedAsk(d, FakeJev(pick)), piece_s=10.0)
        brands = [sp["brand"] for sp in new["spans"]]
        self.assertEqual(brands, ["quillow", "northwind"])
