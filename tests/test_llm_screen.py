"""LLM ad-read screening (earwitness/pipeline/llm_screen.py) and the offline
evaluation script (scripts/llm_screen_eval.py). Offline: every Anthropic
call goes to a fake client; no network, no API key.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from earwitness.pipeline import llm_screen as ls

REPO = Path(__file__).resolve().parent.parent


def fake_message(reads=None, stop_reason="end_turn", usage=(1000, 100, 0, 0), text=None):
    body = text if text is not None else json.dumps({"reads": reads or []})
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="thinking", thinking=""),
                 SimpleNamespace(type="text", text=body)],
        usage=SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1],
                              cache_read_input_tokens=usage[2],
                              cache_creation_input_tokens=usage[3]))


def window_lines(kw):
    """(seg_idx, text) for every rendered transcript line in a request."""
    content = kw["messages"][0]["content"]
    text = content if isinstance(content, str) else "".join(b["text"] for b in content)
    return [(int(m.group(1)), m.group(3)) for m in
            re.finditer(r"^\[(\d+)\] (\d+:\d\d) (.*)$", text, re.M)]


def keyword_responder(brands):
    """Reports a read for each run of lines mentioning a keyword in `brands`
    (keyword -> brand name the model would return)."""
    def respond(kw):
        reads = []
        lines = window_lines(kw)
        for key, brand in brands.items():
            idx = [i for i, t in lines if key in t.lower()]
            if idx:
                reads.append({"brand": brand, "start_seg": min(idx), "end_seg": max(idx),
                              "read_type": "host-read", "offer_codes": ["SAVE"],
                              "urls": [f"{key}.com/show"], "confidence": 0.9,
                              "evidence": "use code SAVE"})
        return fake_message(reads, usage=(800, 120, 700, 0))
    return respond


class FakeBatches:
    def __init__(self, messages):
        self.messages = messages
        self.created = None
        self.polls = 0

    def create(self, requests):
        self.created = list(requests)
        return SimpleNamespace(id="msgbatch_1", processing_status="in_progress")

    def retrieve(self, batch_id):
        self.polls += 1
        return SimpleNamespace(id=batch_id,
                               processing_status="ended" if self.polls >= 2 else "in_progress")

    def results(self, batch_id):
        out = []
        for req in self.created:
            msg = self.messages.responder(req["params"])
            out.append(SimpleNamespace(custom_id=req["custom_id"],
                                       result=SimpleNamespace(type="succeeded", message=msg)))
        return iter(reversed(out))  # any order: must be keyed by custom_id


class FakeMessages:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []
        self.batches = FakeBatches(self)

    def create(self, **kw):
        self.calls.append(kw)
        out = self.responder(kw)
        return out(kw) if callable(out) else out


class FakeClient:
    def __init__(self, responder):
        self.messages = FakeMessages(responder)


def segments(n, step=10.0, texts=None):
    texts = texts or {}
    return [{"start": i * step, "end": i * step + step, "text": texts.get(i, f"talk {i}")}
            for i in range(n)]


class WindowTest(unittest.TestCase):
    def test_split_covers_every_segment_with_overlap(self):
        segs = segments(150)  # 1500 s
        wins = ls.split_windows(segs, window_s=600, overlap_s=60)
        self.assertEqual(len(wins), 3)
        self.assertEqual(wins[0][0], 0)
        self.assertEqual(wins[-1][1], 150)
        covered = set()
        for lo, hi in wins:
            covered.update(range(lo, hi))
            self.assertLessEqual(segs[hi - 1]["start"] - segs[lo]["start"], 600)
        self.assertEqual(covered, set(range(150)))
        for (a0, a1), (b0, b1) in zip(wins, wins[1:]):
            self.assertGreater(a1, b0)  # consecutive windows share segments
            self.assertAlmostEqual(segs[a1 - 1]["end"] - segs[b0]["start"], 60, delta=10)

    def test_short_and_empty_transcripts(self):
        self.assertEqual(ls.split_windows(segments(5)), [(0, 5)])
        self.assertEqual(ls.split_windows([]), [])

    def test_render_lines(self):
        segs = [{"start": 125.4, "end": 130, "text": " hello  there "},
                {"start": 7503.0, "end": 7510, "text": "late"}]
        self.assertEqual(ls.render_window(segs, 0, 2),
                         "[0] 02:05 hello there\n[1] 125:03 late")


class ScreenTranscriptTest(unittest.TestCase):
    def test_structured_output_becomes_reads_with_seconds(self):
        segs = segments(30, texts={10: "this week's sponsor is quionce",
                                   11: "quionce makes linen sheets",
                                   12: "go to quionce dot com"})
        client = FakeClient(keyword_responder({"quionce": "Quince"}))
        res = ls.screen_transcript(segs, client=client, model="claude-haiku-5-5")
        self.assertEqual(len(res["reads"]), 1)
        r = res["reads"][0]
        self.assertEqual((r["brand"], r["brand_normalized"]), ("Quince", "quince"))
        self.assertEqual((r["start_seg"], r["end_seg"], r["start_s"], r["end_s"]),
                         (10, 12, 100.0, 130.0))
        self.assertEqual((r["read_type"], r["offer_codes"], r["confidence"]),
                         ("host-read", ["SAVE"], 0.9))
        self.assertEqual(res["model"], "claude-haiku-5-5")
        self.assertEqual(res["prompt_version"], ls.PROMPT_VERSION)
        self.assertEqual(res["usage"], {"input_tokens": 800, "output_tokens": 120,
                                        "cache_read_input_tokens": 700,
                                        "cache_creation_input_tokens": 0})
        kw = client.messages.calls[0]
        self.assertEqual(kw["model"], "claude-haiku-5-5")
        self.assertEqual(kw["max_tokens"], 4000)
        self.assertEqual(kw["output_config"]["effort"], "low")
        self.assertEqual(kw["output_config"]["format"],
                         {"type": "json_schema", "schema": ls.READ_SCHEMA})
        self.assertEqual(kw["system"], [{"type": "text", "text": ls.SYSTEM_PROMPT,
                                         "cache_control": {"type": "ephemeral"}}])
        for banned in ("temperature", "top_p", "top_k", "output_format"):
            self.assertNotIn(banned, kw)
        self.assertEqual([i for i, _ in window_lines(kw)], list(range(30)))

    def test_system_prompt_is_frozen_across_windows(self):
        client = FakeClient(keyword_responder({}))
        ls.screen_transcript(segments(200), client=client, model="claude-haiku-5-5",
                             effort="medium")
        systems = {json.dumps(c["system"]) for c in client.messages.calls}
        self.assertEqual(len(client.messages.calls), 4)
        self.assertEqual(len(systems), 1)
        self.assertEqual({c["output_config"]["effort"] for c in client.messages.calls},
                         {"medium"})
        self.assertGreater(len(ls.SYSTEM_PROMPT), 2500)  # >512-token cacheable prefix

    def test_reads_merge_across_overlapping_windows(self):
        # Segments 55..62 sit in the overlap of windows 1 and 2.
        texts = {i: "betterhelp online therapy" for i in range(55, 63)}
        texts.update({63: "athletic greens ag one", 64: "athletic greens ag one"})
        segs = segments(120, texts=texts)
        conf = iter([0.6, 0.95, 0.5, 0.5])

        def respond(kw):
            lines = window_lines(kw)
            reads = []
            bh = [i for i, t in lines if "betterhelp" in t]
            if bh:
                reads.append({"brand": "Better Help", "start_seg": bh[0], "end_seg": bh[-1],
                              "read_type": "host-read", "offer_codes": [], "urls": [],
                              "confidence": next(conf), "evidence": "betterhelp"})
            ag = [i for i, t in lines if "ag one" in t]
            if ag:
                reads.append({"brand": "AG1", "start_seg": ag[0], "end_seg": ag[-1],
                              "read_type": "produced-spot", "offer_codes": [], "urls": [],
                              "confidence": 0.8, "evidence": "ag one"})
            return fake_message(reads)
        res = ls.screen_transcript(segs, client=FakeClient(respond), model="claude-haiku-5-5")
        brands = [(r["brand_normalized"], r["start_seg"], r["end_seg"]) for r in res["reads"]]
        self.assertEqual(brands, [("betterhelp", 55, 62), ("ag1", 63, 64)])
        self.assertEqual(res["reads"][0]["confidence"], 0.95)

    def test_merge_rule(self):
        def read(brand, a, b, c=0.5):
            return {"brand": brand, "brand_normalized": brand, "start_seg": a, "end_seg": b,
                    "start_s": a * 10.0, "end_s": b * 10.0 + 10, "confidence": c, "read_type": "host-read", "offer_codes": [], "urls": [],
                    "evidence": "", "windows": [0]}
        merged = ls.merge_reads([read("x", 10, 12), read("x", 15, 18, 0.9),   # gap 3 -> one
                                 read("x", 30, 31), read("x", 35, 36),        # gap 4 -> two
                                 read("y", 18, 20)])                          # other brand
        self.assertEqual([(r["brand_normalized"], r["start_seg"], r["end_seg"], r["confidence"])
                          for r in merged],
                         [("x", 10, 18, 0.9), ("y", 18, 20, 0.5), ("x", 30, 31, 0.5),
                          ("x", 35, 36, 0.5)])

    def test_refusal_and_max_tokens_fail_the_window_only(self):
        segs = segments(200, texts={5: "mint mobile deal"})
        outcomes = iter(["ok", "refusal", "max_tokens", "ok"])

        def respond(kw):
            kind = next(outcomes)
            if kind == "ok":
                return keyword_responder({"mint mobile": "Mint Mobile"})(kw)
            return fake_message(stop_reason=kind, text="{\"reads\": [", usage=(500, 4000, 0, 0))
        res = ls.screen_transcript(segs, client=FakeClient(respond), model="claude-haiku-5-5")
        self.assertEqual([r["brand_normalized"] for r in res["reads"]], ["mint mobile"])
        self.assertEqual([(f["window"], f["reason"]) for f in res["failed_windows"]],
                         [(1, "refusal"), (2, "max_tokens")])
        self.assertEqual(res["windows"], 4)
        self.assertEqual(res["usage"]["output_tokens"], 120 + 4000 + 4000 + 120)

    def test_out_of_window_indexes_are_clamped_or_dropped(self):
        segs = segments(10)

        def respond(kw):
            return fake_message([
                {"brand": "A", "start_seg": 8, "end_seg": 40, "read_type": "uncertain",
                 "offer_codes": [], "urls": [], "confidence": 1.7, "evidence": ""},
                {"brand": "B", "start_seg": 50, "end_seg": 60, "read_type": "host-read",
                 "offer_codes": [], "urls": [], "confidence": 0.5, "evidence": ""},
                {"brand": "C", "start_seg": 4, "end_seg": 2, "read_type": "host-read",
                 "offer_codes": [], "urls": [], "confidence": 0.5, "evidence": ""}])
        res = ls.screen_transcript(segs, client=FakeClient(respond), model="claude-haiku-5-5")
        got = sorted((r["brand"], r["start_seg"], r["end_seg"], r["confidence"])
                     for r in res["reads"])
        self.assertEqual(got, [("A", 8, 9, 1.0), ("C", 2, 4, 0.5)])

    def test_unparseable_text_fails_the_window(self):
        res = ls.screen_transcript(segments(3), client=FakeClient(
            lambda kw: fake_message(text="not json")), model="claude-haiku-5-5")
        self.assertEqual(res["reads"], [])
        self.assertEqual(res["failed_windows"][0]["reason"], "invalid_json")


class CostAndCandidatesTest(unittest.TestCase):
    def test_cost_from_price_table(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000,
                 "cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000}
        self.assertAlmostEqual(ls.cost_usd(usage, "claude-haiku-5-5"), 0.735)
        self.assertAlmostEqual(ls.cost_usd(usage, "claude-haiku-5-5", batch=True), 0.3675)
        self.assertAlmostEqual(ls.cost_usd(usage, "claude-sonnet-5-5"), 2 + 10 + 0.2 + 2.5)
        self.assertIsNone(ls.cost_usd(usage, "some-other-model"))
        self.assertEqual(ls.cost_usd({}, "claude-haiku-5-5"), 0.0)

    def test_llm_candidates_match_rule_candidate_shape(self):
        doc = {"model": "claude-haiku-5-5", "prompt_version": ls.PROMPT_VERSION, "reads": [
            {"read_id": "ep_x.llm0", "brand": "Better Help", "brand_normalized": "betterhelp",
             "start_seg": 3, "end_seg": 5, "start_s": 30.0, "end_s": 60.0,
             "read_type": "host-read", "offer_codes": ["SAVE"], "urls": ["betterhelp.com/x"],
             "confidence": 0.92, "evidence": "betterhelp dot com"},
            {"read_id": "ep_x.llm1", "brand": "unknown", "brand_normalized": "unknown",
             "start_seg": 9, "end_seg": 9, "start_s": 90.0, "end_s": 100.0,
             "read_type": "produced-spot", "offer_codes": [], "urls": [],
             "confidence": 0.3, "evidence": "call now"}]}
        cands = ls.llm_candidates("ep_x", doc, {"show_title": "S", "ep_title": "E",
                                                "pub_date": "2026-10-01", "fetch_date": None})
        rule_keys = {"candidate_id", "ep_id", "show_title", "ep_title", "pub_date", "fetch_date",
                     "brand", "brand_spoken", "brand_raw", "brand_url", "brand_code", "anchor",
                     "confidence", "offer_codes", "urls", "start_s", "end_s", "provenance",
                     "excerpt"}
        for c in cands:
            self.assertTrue(rule_keys <= set(c), rule_keys - set(c))
            self.assertEqual(c["source"], "llm-screen")
        self.assertEqual([c["candidate_id"] for c in cands], ["ep_x.llm0", "ep_x.llm1"])
        self.assertEqual((cands[0]["brand_spoken"], cands[0]["brand_raw"], cands[0]["confidence"]),
                         ("betterhelp", "Better Help", "high"))
        self.assertEqual((cands[1]["brand_spoken"], cands[1]["brand"]), (None, "(unnamed)"))

    def test_missing_credentials_fail_fast(self):
        self.assertFalse(ls.credentials_present({}))
        self.assertTrue(ls.credentials_present({"ANTHROPIC_AUTH_TOKEN": "t"}))
        self.assertFalse(ls.credentials_present({"ANTHROPIC_API_KEY": ""}))
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ls.ScreenUnavailable) as cm:
                ls.make_client()
        self.assertIn("ANTHROPIC_API_KEY", str(cm.exception))


def load_eval():
    spec = importlib.util.spec_from_file_location("llm_screen_eval",
                                                  REPO / "scripts" / "llm_screen_eval.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class EvalBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="llm-eval-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        texts = {20: "this show is sponsored by mint mobile", 21: "mint mobile unlimited plan",
                 22: "mint mobile dot com slash show",
                 60: "factor meals are ready to eat", 61: "factor dot com use code show",
                 90: "great sponsor joke about kissing and whatnot"}
        segs = segments(100, step=5.0, texts=texts)  # 500 s: one window
        (self.tmp / "tr").mkdir()
        (self.tmp / "tr" / "ep_e.json").write_text(json.dumps({"ep_id": "ep_e", "segments": segs}))
        (self.tmp / "labels").mkdir()
        (self.tmp / "labels" / "ep_e.json").write_text(json.dumps({
            "episode_id": "ep_e",
            "reads": [{"brand": "mint mobile", "seg_idx": [20, 21, 22], "read_type": "host-read"},
                      {"brand": "factor", "seg_idx": [60, 61], "read_type": "host-read"},
                      {"brand": "hims", "seg_idx": [80], "read_type": "uncertain"}],
            "organic_mentions": [{"brand": "whatnot", "seg_idx": [90]}]}))
        # Mint Mobile found and named; Factor located but named wrong.
        self.responder = keyword_responder({"mint mobile": "Mint Mobile", "factor": "unknown"})
        self.mod = load_eval()

    def run_eval(self, *extra, client=None):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self.mod.main(["--labels-dir", str(self.tmp / "labels"), "--transcript-root",
                                str(self.tmp / "tr"), "--out", str(self.tmp / "out"), *extra],
                               client=client, poll_s=0)
        return rc, buf.getvalue()


class EvalScriptTest(EvalBase):
    def test_end_to_end_with_fake_client(self):
        client = FakeClient(self.responder)
        rc, out = self.run_eval("--model", "claude-haiku-5-5", "--effort", "low", client=client)
        self.assertEqual(rc, 0, out)
        cands = json.loads((self.tmp / "out" / "candidates_normalized.json").read_text())
        self.assertEqual({c["source"] for c in cands}, {"llm-screen"})
        self.assertEqual(sorted(c["brand_spoken"] or "-" for c in cands), ["-", "mint mobile"])
        m = json.loads((self.tmp / "out" / "metrics.json").read_text())
        self.assertEqual((m["protocol"]["TP"], m["protocol"]["FN"], m["protocol"]["FP"]), (1, 1, 1))
        self.assertEqual(m["strict"]["TP"], 1)
        self.assertEqual(m["location_recall"], {"hit": 2, "reads": 2, "recall": 1.0})
        self.assertEqual(m["usage"]["input_tokens"], 800)
        self.assertGreater(m["cost_usd"], 0)
        for needle in ("protocol:", "strict:", "location-only recall: 2/2", "cost_usd"):
            self.assertIn(needle, out)
        self.assertTrue((self.tmp / "out" / "screens" / "ep_e.json").exists())

    def test_batch_mode_keys_results_by_custom_id(self):
        client = FakeClient(self.responder)
        rc, out = self.run_eval("--batch", client=client)
        self.assertEqual(rc, 0, out)
        self.assertEqual(client.messages.calls, [])  # nothing went through messages.create
        reqs = client.messages.batches.created
        self.assertEqual(len(reqs), 1)
        self.assertTrue(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", reqs[0]["custom_id"]))
        self.assertEqual(reqs[0]["params"]["output_config"]["format"]["type"], "json_schema")
        m = json.loads((self.tmp / "out" / "metrics.json").read_text())
        self.assertEqual(m["protocol"]["TP"], 1)
        self.assertTrue(m["batch"])
        self.assertAlmostEqual(m["cost_usd"],
                               ls.cost_usd(m["usage"], "claude-haiku-5-5", batch=True))

    def test_dry_run_makes_no_calls(self):
        rc, out = self.run_eval("--dry-run", "--model", "claude-sonnet-5-5")
        self.assertEqual(rc, 0, out)
        self.assertIn("windows: 1", out)
        self.assertIn("estimated input tokens", out)
        self.assertFalse((self.tmp / "out" / "candidates_normalized.json").exists())

    def test_missing_key_is_a_clear_error(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                rc, _ = self.run_eval()
        self.assertEqual(rc, 2)
        self.assertIn("ANTHROPIC_API_KEY", buf.getvalue())


if __name__ == "__main__":
    unittest.main()


class FakeAuthenticationError(Exception):
    status_code = 401


FakeAuthenticationError.__name__ = "AuthenticationError"


class FakeBadRequestError(Exception):
    status_code = 400


FakeBadRequestError.__name__ = "BadRequestError"


class FakeOverloadedError(Exception):
    status_code = 529


class ReviewFixesScreenTest(unittest.TestCase):
    def test_classify_errors(self):
        self.assertEqual(ls.classify_error(FakeAuthenticationError("bad key")), "fatal")
        self.assertEqual(ls.classify_error(FakeBadRequestError(
            "Your credit balance is too low to access the Anthropic API")), "fatal")
        self.assertEqual(ls.classify_error(FakeBadRequestError("prompt is too long")), "permanent")
        self.assertEqual(ls.classify_error(FakeOverloadedError("overloaded")), "transient")
        self.assertEqual(ls.classify_error(ConnectionError("reset")), "transient")

    def test_partial_usage_survives_a_failing_window(self):
        n = {"k": 0}
        ok = keyword_responder({})

        def flaky(kw):
            n["k"] += 1
            if n["k"] == 3:
                raise FakeOverloadedError("529 overloaded")
            return ok(kw)
        with self.assertRaises(ls.ScreenAborted) as cm:
            ls.screen_transcript(segments(200), client=FakeClient(flaky), model="claude-haiku-5-5")
        err = cm.exception
        self.assertIsInstance(err.__cause__, FakeOverloadedError)
        self.assertEqual((err.windows_done, err.windows), (2, 4))
        self.assertEqual(err.usage["input_tokens"], 1600)
        self.assertEqual(ls.classify_error(err), "transient")

    def test_client_has_timeout_and_bounded_retries(self):
        seen = {}

        class FakeSDK:
            class Anthropic:
                def __init__(self, **kw):
                    seen.update(kw)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), \
                mock.patch.dict("sys.modules", {"anthropic": FakeSDK}):
            ls.make_client()
        self.assertEqual((seen["timeout"], seen["max_retries"]), (120.0, 3))

    def test_preflight_checks_key_and_model(self):
        calls = []

        class Models:
            def retrieve(self, model):
                calls.append(model)
                if model == "bad":
                    raise FakeAuthenticationError("invalid x-api-key")
        client = SimpleNamespace(models=Models())
        ls.preflight(client, "claude-haiku-5-5")
        self.assertEqual(calls, ["claude-haiku-5-5"])
        with self.assertRaises(ls.ScreenUnavailable) as cm:
            ls.preflight(client, "bad")
        self.assertIn("invalid x-api-key", str(cm.exception))
        ls.preflight(SimpleNamespace(), "x")  # clients without .models are not checked

    def test_unpriced_model_warns(self):
        self.assertIsNone(ls.cost_usd({"input_tokens": 5}, "claude-new-model"))
        self.assertIn("claude-new-model", ls.price_warning("claude-new-model"))
        self.assertIsNone(ls.price_warning("claude-haiku-5-5"))


class ReviewFixesEvalTest(EvalBase):
    def test_batch_error_detail_is_kept(self):
        client = FakeClient(self.responder)

        def results(batch_id):
            req = client.messages.batches.created[0]
            err = SimpleNamespace(type="error", error=SimpleNamespace(
                type="invalid_request_error", message="prompt is too long"))
            return iter([SimpleNamespace(custom_id=req["custom_id"], result=SimpleNamespace(
                type="errored", error=err))])
        client.messages.batches.results = results
        rc, out = self.run_eval("--batch", client=client)
        self.assertEqual(rc, 0, out)
        screen = json.loads((self.tmp / "out" / "screens" / "ep_e.json").read_text())
        self.assertIn("invalid_request_error", screen["failed_windows"][0]["reason"])
        self.assertIn("prompt is too long", screen["failed_windows"][0]["reason"])

    def test_batch_poll_timeout_reports_the_batch_id(self):
        client = FakeClient(self.responder)
        client.messages.batches.retrieve = lambda bid: SimpleNamespace(
            id=bid, processing_status="in_progress")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc, out = self.run_eval("--batch", "--batch-timeout-s", "0", client=client)
        self.assertEqual(rc, 3)
        self.assertIn("msgbatch_1", buf.getvalue())

    def test_batch_create_error_is_reported(self):
        client = FakeClient(self.responder)

        def boom(requests):
            raise FakeAuthenticationError("invalid x-api-key")
        client.messages.batches.create = boom
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc, _ = self.run_eval("--batch", client=client)
        self.assertEqual(rc, 2)
        self.assertIn("invalid x-api-key", buf.getvalue())

    def test_unpriced_model_warns_instead_of_zero(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc, out = self.run_eval("--model", "claude-new-model",
                                    client=FakeClient(self.responder))
        self.assertEqual(rc, 0)
        self.assertIn("claude-new-model", buf.getvalue())
        self.assertIsNone(json.loads((self.tmp / "out" / "metrics.json").read_text())["cost_usd"])
