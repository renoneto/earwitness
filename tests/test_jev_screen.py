"""Jev ad-window screening client (offline: fake transports and openers)."""
from __future__ import annotations

import io
import json
import math
import shutil
import tempfile
import unittest
import urllib.error
from pathlib import Path

from earwitness.pipeline import jev_budget
from earwitness.pipeline import jev_screen as js

REF_Q = ("Does this named window contain a clear commercial ad read or sponsor message, rather "
         "than ordinary editorial conversation or an organic brand mention without a commercial "
         "call to action?")


def segs(n_minutes, text="we talk about the news today", per_min=6):
    step = 60.0 / per_min
    return [{"start": i * step, "end": i * step + step, "text": f" {text} {i} "}
            for i in range(int(n_minutes * per_min))]


def fake_transport(score=lambda w: 0.02, calls=None, model="jev-1.13.0"):
    """Scores each window from its transcript; usage = request chars / 4."""
    def call(body):
        if calls is not None:
            calls.append(body)
        answers = {wid: {"type": "noul", "noul": score(w)}
                   for wid, w in body["state"]["windows"].items()}
        return {"model": model, "answers": answers,
                "usage": {"input_tokens": len(json.dumps(body)) // 4, "output_tokens": 0}}
    return call


def sponsor_score(w):
    return 0.95 if "sponsor" in w["transcript"] else 0.02


class WindowsAndWordingTest(unittest.TestCase):
    def test_windows_match_reference_client(self):
        s = [{"start": 0.0, "end": 50.0, "text": " hello "},
             {"start": 50.0, "end": 70.0, "text": "across"},
             {"start": 70.0, "end": 130.5, "text": " tail "}]
        w = js.windows(s)
        self.assertEqual([(x["window_id"], x["start_s"], x["end_s"], x["text"]) for x in w], [
            ("window_000", 0.0, 60.0, "hello across"),
            ("window_001", 60.0, 120.0, "across tail"),
            ("window_002", 120.0, 130.5, "tail")])

    def test_request_shape_and_wording_identical_to_reference(self):
        calls = []
        js.screen_episode(segs(2), transport=fake_transport(calls=calls), episode_id="ep_x")
        self.assertEqual(len(calls), 1)
        body = calls[0]
        self.assertEqual(set(body), {"model", "state", "questions"})
        self.assertEqual(body["model"], "jev-1.13.0")
        self.assertEqual(js.QUESTION, REF_Q)
        self.assertEqual(body["state"]["episode_id"], "ep_x")
        self.assertEqual(body["state"]["windows"]["window_001"],
                         {"start_s": 60.0, "end_s": 120.0,
                          "transcript": body["state"]["windows"]["window_001"]["transcript"]})
        self.assertEqual(body["questions"]["window_001"],
                         {"type": "noul", "instructions": f"Window window_001 (60s-120s): {REF_Q}"})
        self.assertEqual(list(body["questions"]), ["window_000", "window_001"])


class SplittingTest(unittest.TestCase):
    def test_batches_of_40_like_the_reference(self):
        calls = []
        res = js.screen_episode(segs(100), transport=fake_transport(calls=calls), episode_id="e")
        self.assertEqual([len(b["questions"]) for b in calls], [40, 40, 20])
        self.assertEqual(res["requests"], 3)
        self.assertEqual(len(res["windows"]), 100)

    def test_large_windows_shrink_the_batch_under_token_limits(self):
        wordy = "sponsor " * 1500  # ~12k chars per minute
        calls = []
        js.screen_episode(segs(30, wordy, per_min=1), transport=fake_transport(calls=calls),
                          episode_id="e")
        self.assertGreater(len(calls), 1)
        self.assertEqual(sum(len(b["questions"]) for b in calls), 30)
        for b in calls:
            total = js.estimate_tokens(b)
            longest = max(len(json.dumps(q)) for q in b["questions"].values())
            state_q = math.ceil((len(json.dumps(b["state"])) + longest) / js.CHARS_PER_TOKEN)
            self.assertLessEqual(total, js.REQUEST_TOKEN_BUDGET)
            self.assertLessEqual(state_q, js.STATE_TOKEN_BUDGET)
            self.assertLess(js.REQUEST_TOKEN_BUDGET, js.TOKEN_LIMIT_REQUEST)
            self.assertLess(js.STATE_TOKEN_BUDGET, js.TOKEN_LIMIT_STATE)

    def test_a_single_oversized_window_is_truncated_not_sent_whole(self):
        calls = []
        res = js.screen_episode([{"start": 0.0, "end": 60.0, "text": "x" * 200_000}],
                                transport=fake_transport(calls=calls), episode_id="e")
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(js.estimate_tokens(calls[0]), js.REQUEST_TOKEN_BUDGET)
        self.assertEqual(res["truncated_windows"], ["window_000"])

    def test_empty_transcript_makes_no_request(self):
        calls = []
        res = js.screen_episode([], transport=fake_transport(calls=calls), episode_id="e")
        self.assertEqual((calls, res["windows"], res["requests"], res["cost_usd"]), ([], [], 0, 0.0))


class ParsingTest(unittest.TestCase):
    def test_result_shape_usage_and_cost(self):
        s = segs(3)
        s[7]["text"] = "this episode is brought to you by our sponsor"
        res = js.screen_episode(s, transport=fake_transport(sponsor_score), episode_id="e")
        self.assertEqual(res["model"], "jev-1.13.0")
        self.assertEqual([(w["window_id"], w["start_s"], w["end_s"], w["noul"])
                          for w in res["windows"]],
                         [("window_000", 0.0, 60.0, 0.02), ("window_001", 60.0, 120.0, 0.95),
                          ("window_002", 120.0, 180.0, 0.02)])
        self.assertEqual(set(res["usage"]), {"input_tokens", "output_tokens"})
        self.assertAlmostEqual(res["cost_usd"], res["usage"]["input_tokens"] * 0.042 / 1e6)

    def test_cost_math(self):
        self.assertAlmostEqual(js.cost_usd({"input_tokens": 1_000_000, "output_tokens": 5000}),
                               0.042)
        self.assertEqual(js.cost_usd({}), 0.0)
        self.assertAlmostEqual(js.cost_usd({"input_tokens": 19_000}), 0.000798)

    def test_out_of_range_or_missing_noul_is_rejected(self):
        for bad in (1.2, -0.1, float("nan"), "0.9", True, None):
            with self.subTest(bad=bad):
                with self.assertRaises(js.JevAborted) as cm:
                    js.screen_episode(segs(2), transport=fake_transport(lambda w: bad),
                                      episode_id="e")
                self.assertEqual(js.classify_error(cm.exception), "transient")

        def drop(body):
            r = fake_transport()(body)
            r["answers"].pop("window_000")
            return r
        with self.assertRaises(js.JevAborted):
            js.screen_episode(segs(2), transport=drop, episode_id="e")

    def test_partial_usage_survives_a_failure(self):
        n = {"k": 0}
        ok = fake_transport()

        def flaky(body):
            n["k"] += 1
            if n["k"] == 2:
                raise js.JevError(503, "overloaded")
            return ok(body)
        with self.assertRaises(js.JevAborted) as cm:
            js.screen_episode(segs(100), transport=flaky, episode_id="e")
        self.assertGreater(cm.exception.usage["input_tokens"], 0)
        self.assertEqual(cm.exception.requests, 1)


class ErrorClassificationTest(unittest.TestCase):
    def test_classes(self):
        for status, kind in ((401, "fatal"), (402, "fatal"), (403, "fatal"), (404, "fatal"),
                             (400, "permanent"), (413, "permanent"), (422, "permanent"),
                             (429, "transient"), (500, "transient"), (503, "transient")):
            self.assertEqual(js.classify_error(js.JevError(status, "x")), kind, status)
        self.assertEqual(js.classify_error(js.JevError(400, "insufficient credit balance")),
                         "fatal")
        self.assertEqual(js.classify_error(ConnectionError("reset")), "transient")
        # A rate limit stays transient even when its message mentions a quota.
        self.assertEqual(js.classify_error(js.JevError(429, "token quota exceeded")),
                         "transient")
        wrapped = js.JevAborted(js.JevError(401, "bad key"), {}, 0)
        wrapped.__cause__ = wrapped.cause
        self.assertEqual(js.classify_error(wrapped), "fatal")


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def http_error(code, body=b'{"detail": "nope"}'):
    return urllib.error.HTTPError("https://api.typesafe.ai/v1/systemone", code, "err", {},
                                  io.BytesIO(body))


class TransportTest(unittest.TestCase):
    def setUp(self):  # a fresh spend-cap ledger: every POST reserves first
        tmp = Path(tempfile.mkdtemp(prefix="jev-transport-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        jev_budget.init(tmp / "ledger.sqlite3", tmp / "ledger.pin", first_activation=True)
        self.budget = jev_budget.JevBudget(tmp / "ledger.sqlite3", tmp / "ledger.pin")

    def opener(self, script, seen):
        def open_(req, timeout=None):
            seen.append(req)
            step = script.pop(0)
            if isinstance(step, Exception):
                raise step
            return FakeResponse(json.dumps(step).encode())
        return open_

    def test_posts_bearer_json_and_retries_429_and_5xx(self):
        seen, sleeps = [], []
        ok = {"model": "jev-1.13.0", "answers": {}, "usage": {"input_tokens": 1}}
        t = js.urllib_transport("sk-test", budget=self.budget, timeout=7, opener=self.opener(
            [http_error(429), http_error(503), ok], seen), sleep=sleeps.append)
        self.assertEqual(t({"model": "jev-1.13.0"}), ok)
        self.assertEqual(len(seen), 3)
        req = seen[0]
        self.assertEqual(req.full_url, js.API_URL)
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer sk-test")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(all(0 < s <= t.max_backoff_s for s in sleeps))

    def test_retries_are_bounded_and_4xx_is_not_retried(self):
        seen = []
        t = js.urllib_transport("k", budget=self.budget, opener=self.opener([http_error(503)] * 10, seen),
                                sleep=lambda s: None)
        with self.assertRaises(js.JevError) as cm:
            t({})
        self.assertEqual((cm.exception.status, len(seen)), (503, t.retries + 1))
        seen = []
        t = js.urllib_transport("k", budget=self.budget, opener=self.opener([http_error(401)], seen),
                                sleep=lambda s: None)
        with self.assertRaises(js.JevError) as cm:
            t({})
        self.assertEqual((cm.exception.status, len(seen)), (401, 1))

    def test_key_never_appears_in_errors(self):
        t = js.urllib_transport("sk-secret-123", budget=self.budget, opener=self.opener([http_error(401)], []),
                                sleep=lambda s: None)
        with self.assertRaises(js.JevError) as cm:
            t({})
        self.assertNotIn("sk-secret-123", str(cm.exception) + repr(cm.exception))

    def test_rate_limit_spaces_requests(self):
        now = [0.0]
        sleeps = []

        def sleep(s):
            sleeps.append(s)
            now[0] += s
        ok = {"answers": {}}
        t = js.urllib_transport("k", budget=self.budget, opener=self.opener([ok] * 3, []), sleep=sleep,
                                min_interval_s=0.1, clock=lambda: now[0])
        for _ in range(3):
            t({})
        self.assertAlmostEqual(sum(sleeps), 0.2)

    def test_preflight_gets_models_and_retries_transient(self):
        seen = []
        t = js.urllib_transport("k", budget=self.budget, opener=self.opener(
            [ConnectionError("blip"), {"models": []}], seen), sleep=lambda s: None)
        js.preflight(t, retry_sleep_s=0)
        self.assertEqual([(r.get_method(), r.full_url) for r in seen],
                         [("GET", js.MODELS_URL)] * 2)
        t = js.urllib_transport("k", budget=self.budget, opener=self.opener([http_error(401)], []),
                                sleep=lambda s: None)
        with self.assertRaises(js.JevUnavailable):
            js.preflight(t, retry_sleep_s=0)

    def test_transport_from_env_requires_key(self):
        with self.assertRaises(js.JevUnavailable) as cm:
            js.transport_from_env({})
        self.assertIn("TYPESAFE_API_KEY", str(cm.exception))
        self.assertEqual(js.transport_from_env({"TYPESAFE_API_KEY": "k"}).timeout, 60)


if __name__ == "__main__":
    unittest.main()
