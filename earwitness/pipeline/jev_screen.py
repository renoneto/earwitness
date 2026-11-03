"""Jev (TypeSafe System One) ad-window screening for the corpus runner.

Each transcript is cut into 60-second windows; Jev rates every window with a
`noul` score in [0, 1] ("does this window contain a clear commercial ad
read?"). Measured on a 12-episode blind holdout (2026-10-08): at noul >= 0.8
it covers 88% of labeled ad reads, 95% of flagged windows overlap a real
read, and it flags about 5% of runtime. It cannot name brands.

The request shape and wording are those of the measured reference client
(.corpus-scale-20261008/jev-holdout/jev_holdout.py, same question as the
2026-09-21 triage run): state {"episode_id", "windows": {id: {start_s, end_s,
transcript}}}, one noul question per window, at most 40 windows per request.
This deliberately differs from detection.build_jev_request (an internal
record shape with a different question that was never measured).
Response validation reuses detection.validate_jev_response.

Stdlib only (urllib). The API key is read from TYPESAFE_API_KEY and never
logged or written anywhere. Every request goes through the spend cap
(jev_budget): the transport cannot be built without a budget handle.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request

from earwitness.pipeline import jev_budget
from earwitness.pipeline.detection import PINNED_JEV_MODEL, validate_jev_response

API_URL = "https://api.typesafe.ai/v1/systemone"
MODELS_URL = "https://api.typesafe.ai/v1/models"
DEFAULT_MODEL = PINNED_JEV_MODEL  # "jev-1.13.0"
QUESTION = ("Does this named window contain a clear commercial ad read or sponsor message, "
            "rather than ordinary editorial conversation or an organic brand mention without "
            "a commercial call to action?")
WINDOW_S = 60.0
MAX_WINDOWS_PER_REQUEST = 40
DEFAULT_THRESHOLD = 0.8
INPUT_USD_PER_MTOK = jev_budget.PRICING.input_usd_per_mtok    # 0.042
OUTPUT_USD_PER_MTOK = jev_budget.PRICING.output_usd_per_mtok  # output tokens are free
PRICING_SOURCE = jev_budget.PRICING.source

# API limits, and the budgets requests are kept under (half: the 4 chars/token
# estimate is rough).
TOKEN_LIMIT_REQUEST = 64_000  # state + all questions
TOKEN_LIMIT_STATE = 32_000    # state + longest question
REQUEST_TOKEN_BUDGET = TOKEN_LIMIT_REQUEST // 2
STATE_TOKEN_BUDGET = TOKEN_LIMIT_STATE // 2
CHARS_PER_TOKEN = 4.0
# Output bound used for a request's maximum charge. The API takes no output
# limit; this pins the per-request token limit. Output is priced at $0, so
# it adds nothing to the maximum unless the output price changes.
MAX_OUTPUT_TOKENS = TOKEN_LIMIT_REQUEST

RETRY_STATUS = (429, 500, 502, 503, 504, 529)
_FATAL_STATUS = (401, 402, 403, 404)
_PERMANENT_STATUS = (400, 413, 422)
_FATAL_TEXT = re.compile(r"credit balance|billing|payment required|insufficient.?credit"
                         r"|quota exceeded", re.I)


class JevError(RuntimeError):
    """An HTTP or network failure talking to Jev (status None = network).
    Its text is a code only ("HTTP 402", "network: TimeoutError"): provider
    response bodies are never persisted or logged. `body` (first 300
    chars) stays in memory for classify_error's fatal-text check."""

    def __init__(self, status, body: str = "", *, reason: str | None = None):
        self.status = status
        self.body = str(body or "")[:300]
        super().__init__(reason or (f"HTTP {status}" if status else "network error"))


class JevAborted(RuntimeError):
    """An episode's screening stopped part-way; `usage` is what was billed."""

    def __init__(self, cause, usage: dict, requests: int):
        super().__init__(f"{type(cause).__name__}: {cause}")
        self.cause, self.usage, self.requests = cause, dict(usage), requests


class JevUnavailable(RuntimeError):
    """No key, or the preflight call failed: do not claim work."""


class JevFatal(RuntimeError):
    """No further Jev request from this process can succeed (key, payment)."""


def classify_error(exc) -> str:
    """"fatal": stop the worker (bad key, permission, payment, unknown
    model/route); "permanent": this episode cannot be screened (bad or too
    large request); "transient": anything else (429/5xx after retries,
    network, malformed answers)."""
    if isinstance(exc, JevAborted):
        exc = exc.cause
    status = getattr(exc, "status", None)
    if status == 429:  # rate limits are transient whatever their message says
        return "transient"
    if status in _FATAL_STATUS or _FATAL_TEXT.search(str(exc)) \
            or _FATAL_TEXT.search(getattr(exc, "body", "") or ""):
        return "fatal"
    if status in _PERMANENT_STATUS:
        return "permanent"
    return "transient"


# ---------------------------------------------------------------- requests

def windows(segments, window_s: float = WINDOW_S) -> list:
    """Fixed windows over [0, last segment end], as the reference client."""
    if not segments:
        return []
    end = max(s["end"] for s in segments)
    out, t, i = [], 0.0, 0
    while t < end:
        text = " ".join(s["text"].strip() for s in segments
                        if s["start"] < t + window_s and s["end"] > t)
        out.append({"window_id": f"window_{i:03d}", "start_s": t,
                    "end_s": min(t + window_s, end), "text": text})
        t += window_s
        i += 1
    return out


def build_request(chunk: list, *, model: str = DEFAULT_MODEL, episode_id=None) -> dict:
    state = {"episode_id": episode_id} if episode_id is not None else {}
    state["windows"] = {w["window_id"]: {"start_s": w["start_s"], "end_s": w["end_s"],
                                         "transcript": w["text"]} for w in chunk}
    questions = {w["window_id"]: {
        "type": "noul",
        "instructions": f"Window {w['window_id']} ({w['start_s']:.0f}s-{w['end_s']:.0f}s): "
                        f"{QUESTION}"} for w in chunk}
    return {"model": model, "state": state, "questions": questions}


def _chars(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False))


def estimate_tokens(body: dict) -> int:
    return math.ceil(_chars(body) / CHARS_PER_TOKEN)


def _fits(body: dict) -> bool:
    longest = max((_chars(q) for q in body["questions"].values()), default=0)
    return (estimate_tokens(body) <= REQUEST_TOKEN_BUDGET
            and math.ceil((_chars(body["state"]) + longest) / CHARS_PER_TOKEN)
            <= STATE_TOKEN_BUDGET)


def plan_requests(ws: list, *, model: str = DEFAULT_MODEL, episode_id=None,
                  max_windows: int = MAX_WINDOWS_PER_REQUEST) -> tuple:
    """(request bodies, truncated window ids). Batches of up to
    `max_windows`, shrunk so every request stays under the token budgets; a
    window too large on its own has its transcript cut until it fits."""
    truncated = []
    for w in ws:
        while not _fits(build_request([w], model=model, episode_id=episode_id)):
            w["text"] = w["text"][:len(w["text"]) // 2]
            if w["window_id"] not in truncated:
                truncated.append(w["window_id"])
    batches, cur = [], []
    for w in ws:
        trial = cur + [w]
        if cur and (len(trial) > max_windows
                    or not _fits(build_request(trial, model=model, episode_id=episode_id))):
            batches.append(cur)
            trial = [w]
        cur = trial
    if cur:
        batches.append(cur)
    return [build_request(b, model=model, episode_id=episode_id) for b in batches], truncated


def parse_answers(body: dict, response) -> dict:
    """{window_id: noul} after strict validation (every requested window
    answered, noul a finite number in [0, 1])."""
    if not isinstance(response, dict):
        raise ValueError("jev response is not an object")
    request = {"windows": [{"window_id": wid, "start_s": w["start_s"], "end_s": w["end_s"]}
                           for wid, w in body["state"]["windows"].items()]}
    validate_jev_response(request, response)
    return {wid: float(response["answers"][wid]["noul"]) for wid in body["questions"]}


def add_usage(total: dict, response) -> dict:
    u = (response or {}).get("usage") if isinstance(response, dict) else None
    for k in ("input_tokens", "output_tokens"):
        v = (u or {}).get(k)
        total[k] = total.get(k, 0) + (int(v) if isinstance(v, (int, float)) else 0)
    return total


def cost_usd(usage: dict, model: str | None = None) -> float:
    u = usage or {}
    return round((u.get("input_tokens") or 0) * INPUT_USD_PER_MTOK / 1e6
                 + (u.get("output_tokens") or 0) * OUTPUT_USD_PER_MTOK / 1e6, 10)


def segments_digest(segments) -> str:
    """sha256 of the segments as canonical JSON: which transcript copy a
    result read (the importer recomputes it from the stored copy)."""
    import hashlib
    return hashlib.sha256(json.dumps(segments, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def screen_episode(segments, *, transport, model: str = DEFAULT_MODEL,
                   window_s: float = WINDOW_S,
                   max_windows_per_request: int = MAX_WINDOWS_PER_REQUEST,
                   episode_id=None) -> dict:
    """Score every window of one transcript. Raises JevAborted (carrying the
    usage billed so far) on any failure. The result records the copy it
    read: segment_count and segments_sha256."""
    ws = windows(segments, window_s)
    bodies, truncated = plan_requests(ws, model=model, episode_id=episode_id,
                                      max_windows=max_windows_per_request)
    usage = {"input_tokens": 0, "output_tokens": 0}
    scores, returned, n = {}, None, 0
    for body in bodies:
        try:
            resp = transport(body)
            n += 1
            add_usage(usage, resp)
            scores.update(parse_answers(body, resp))
        except Exception as exc:
            raise JevAborted(exc, usage, n) from exc
        returned = resp.get("model") or returned
    return {"model": returned or model, "model_requested": model, "question": QUESTION,
            "window_s": window_s,
            "windows": [{"window_id": w["window_id"], "start_s": w["start_s"],
                         "end_s": w["end_s"], "noul": scores[w["window_id"]]} for w in ws],
            "usage": usage, "cost_usd": cost_usd(usage, model), "requests": n,
            "truncated_windows": truncated, "segment_count": len(segments),
            "segments_sha256": segments_digest(segments)}


def estimate(segments, *, model: str = DEFAULT_MODEL, window_s: float = WINDOW_S,
             episode_id=None) -> dict:
    """Dry-run: requests and estimated input tokens/cost, no network."""
    bodies, _ = plan_requests(windows(segments, window_s), model=model, episode_id=episode_id)
    tokens = sum(estimate_tokens(b) for b in bodies)
    return {"requests": len(bodies), "input_tokens": tokens,
            "cost_usd": cost_usd({"input_tokens": tokens})}


# ---------------------------------------------------------------- transport

class UrllibTransport:
    """POSTs a request body to Jev; retries 429/5xx and network errors with
    bounded exponential backoff; optional pacing (min_interval_s between
    requests). Every POST attempt first reserves its maximum charge with
    `budget` (jev_budget.JevBudget) and is settled or kept unknown after;
    a refused reservation raises jev_budget.JevBudgetRefused and nothing is
    sent. Errors carry status + a body snippet, never the key."""

    def __init__(self, key: str, timeout: float = 60, *, budget, purpose: str = "screen",
                 url: str = API_URL, models_url: str = MODELS_URL, retries: int = 4,
                 backoff_s: float = 1.0, max_backoff_s: float = 30.0,
                 min_interval_s: float = 0.0, opener=None, sleep=time.sleep,
                 clock=time.monotonic, max_output_tokens: int | None = MAX_OUTPUT_TOKENS,
                 alert=None):
        if budget is None:
            raise TypeError("UrllibTransport needs a Jev budget handle")
        self._key = key
        self.budget, self.purpose, self.max_output_tokens = budget, purpose, max_output_tokens
        self.timeout, self.url, self.models_url = timeout, url, models_url
        self.retries, self.backoff_s, self.max_backoff_s = retries, backoff_s, max_backoff_s
        self.min_interval_s = min_interval_s
        self.opener = opener or urllib.request.urlopen
        self.sleep, self.clock = sleep, clock
        self.alert = alert or _stderr_alert   # alert(code, message): codes, no bodies
        self._last = float("-inf")

    def __repr__(self):
        return f"UrllibTransport(url={self.url!r}, timeout={self.timeout})"

    def _pace(self):
        if self.min_interval_s > 0:
            wait = self._last + self.min_interval_s - self.clock()
            if wait > 0:
                self.sleep(wait)
            self._last = self.clock()

    def _open(self, req):
        try:
            with self.opener(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                body = e.read()[:300].decode(errors="replace")
            except Exception:
                body = ""
            raise JevError(e.code, body) from None
        except (urllib.error.URLError, OSError) as e:  # network, timeout
            raise JevError(None, f"{type(e).__name__}: {e}",
                           reason=f"network: {type(e).__name__}") from None

    def _close(self, res, resp) -> None:
        """Settle at the response's charge, else keep the maximum (unknown).
        A ledger write that fails leaves the row reserved, also counted at
        its maximum, and raises a JEV_BUDGET_SETTLE_FAILED alert; a charge
        above the reserved maximum raises JEV_BOUND_EXCEEDED."""
        charge = response_charge(resp)
        try:
            if charge is None:
                self.budget.mark_unknown(res)
                return
            code = self.budget.settle(res, charge)
        except Exception as exc:
            self.alert(jev_budget.SETTLE_FAILED, f"{self.purpose}: ledger write after a Jev "
                       f"request failed ({type(exc).__name__}); the reservation stays counted "
                       "at its maximum")
            return
        if code:
            self.alert(code, f"{self.purpose}: a Jev response charged more than its reserved "
                       "maximum; Jev is closed for the rest of that LA month (review the price and bound)")

    def __call__(self, body: dict) -> dict:
        data = json.dumps(body).encode()
        state = body.get("state") if isinstance(body, dict) else None
        episode = state.get("episode_id") if isinstance(state, dict) else None
        for attempt in range(self.retries + 1):
            self._pace()
            res = self.budget.reserve(purpose=self.purpose, episode=episode,
                                      body_bytes=len(data),
                                      output_tokens_max=self.max_output_tokens)
            req = urllib.request.Request(self.url, data=data, method="POST", headers={
                "Authorization": f"Bearer {self._key}", "Content-Type": "application/json"})
            try:
                resp = self._open(req)
            except JevError as exc:
                self._close(res, None)
                if (exc.status is not None and exc.status not in RETRY_STATUS) \
                        or attempt == self.retries:
                    raise
            except BaseException:
                self._close(res, None)
                raise
            else:
                self._close(res, resp)
                return resp
            self.sleep(min(self.max_backoff_s, self.backoff_s * 2 ** attempt))
        raise AssertionError("unreachable")

    def preflight(self) -> None:
        """One cheap authenticated GET /v1/models (no tokens billed, so no
        reservation)."""
        self._open(urllib.request.Request(self.models_url, method="GET", headers={
            "Authorization": f"Bearer {self._key}"}))


def _stderr_alert(code: str, message: str) -> None:
    print(f"ALERT {code}: {message}", file=sys.stderr, flush=True)


def response_charge(resp) -> float | None:
    """The charge a response reports (cost_usd of its usage, the figure
    stage_runs.cost_usd records), or None when it reports no usage."""
    u = resp.get("usage") if isinstance(resp, dict) else None
    tokens = (u or {}).get("input_tokens") if isinstance(u, dict) else None
    if not isinstance(tokens, (int, float)) or isinstance(tokens, bool) or tokens < 0:
        return None
    return cost_usd(add_usage({}, resp))


def urllib_transport(key: str, timeout: float = 60, **kw) -> UrllibTransport:
    return UrllibTransport(key, timeout, **kw)


def transport_from_env(env=None, *, budget=None, **kw) -> UrllibTransport:
    """The Jev transport for the pipeline's key, with the budget handle from
    EW_JEV_BUDGET_LEDGER / EW_JEV_BUDGET_PIN (defaults in jev_budget)."""
    env = os.environ if env is None else env
    key = (env.get("TYPESAFE_API_KEY") or "").strip()
    if not key:
        raise JevUnavailable("TYPESAFE_API_KEY is not set; the jev stage needs a TypeSafe key")
    return urllib_transport(key, budget=budget or jev_budget.JevBudget.from_env(env), **kw)


def preflight(transport, *, tries: int = 3, retry_sleep_s: float = 5.0) -> None:
    """Check the key before claiming work; transient errors are retried."""
    check = getattr(transport, "preflight", None)
    if check is None:
        return
    for attempt in range(tries):
        try:
            check()
            return
        except Exception as exc:
            if classify_error(exc) == "transient" and attempt + 1 < tries:
                time.sleep(retry_sleep_s)
                continue
            raise JevUnavailable(f"jev preflight failed: {type(exc).__name__}: {exc}") from exc
