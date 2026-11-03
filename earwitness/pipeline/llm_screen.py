"""LLM ad-read screening: one transcript in, normalized ad reads out.

The transcript is split into overlapping time windows; each window is sent
to the Anthropic Messages API with a frozen (prompt-cached) system prompt
and a JSON-schema structured output. Reads found in several windows are
merged. Everything here is pure except the `client.messages.create` call,
so tests run against a fake client.

Screen workers need the official SDK, which is NOT in requirements.lock:

    pip install anthropic

and ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) in the environment.
"""
from __future__ import annotations

import json
import os
import time
import re

from earwitness.gazetteer import normalize_brand

PROMPT_VERSION = "screen-v1"
DEFAULT_MODEL = "claude-haiku-5-5"
MAX_TOKENS = 4000
MERGE_GAP_SEGS = 3
READ_TYPES = ("host-read", "produced-spot", "network-promo", "uncertain")

# USD per million tokens. Cache reads bill at CACHE_READ_MULT x input,
# 5-minute cache writes at CACHE_WRITE_MULT x input. Edit here.
PRICES_PER_MTOK = {
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00},
}
CACHE_READ_MULT = 0.10
CACHE_WRITE_MULT = 1.25
BATCH_DISCOUNT = 0.50  # Message Batches API bills half price
CHARS_PER_TOKEN_EST = 3.5  # dry-run estimate only

USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens",
              "cache_creation_input_tokens")

SYSTEM_PROMPT = """\
You screen podcast transcripts for advertising. Each request contains one window of an \
automatic speech-to-text transcript of a single episode. Every line is one segment in the \
form "[seg_idx] mm:ss text": seg_idx is the segment's index in the whole episode and mm:ss \
is its start time from the beginning of the episode. Report every ad read that appears in \
the window.

WHAT COUNTS AS AN AD READ
An ad read is paid promotion of a third-party product, service, or another show. Report \
all of these:
- Host reads: a host pitches a sponsor in their own voice. They are often casual and \
personal, and many have no "sponsored by" or "brought to you by" marker at all. A personal \
story that leads into the pitch ("I have very strong mattress opinions...") is part of the \
read.
- Produced spots: pre-recorded commercials, usually in another voice. They often begin \
abruptly, with no transition, and have nothing to do with the conversation around them.
- Mid-roll inserts: dynamically inserted ads that cut into the conversation, sometimes in \
the middle of a sentence. The conversation resumes after the ad, often mid-thought.
- Network promos: promotion of another podcast or show, including shows from the same \
network ("If you like this show, check out...", "Listen to X wherever you get your \
podcasts").
- Short sponsor credits: one-line credits such as "Support for this show comes from X" or \
"This episode is brought to you by X", even when no pitch follows.

WHAT DOES NOT COUNT
Do not report:
- The hosts' own products and channels: their merchandise, Patreon or paid membership, \
live tour, newsletter, book, or their own company.
- Editorial discussion of companies or products: news, analysis, reviews, interviews, or a \
guest talking about the company they run or work for.
- Jokes and riffs about brands, including mock sponsor lines ("they'd be a great sponsor", \
"this segment is brought to you by kissing and whatnot").
- Ads that are played or quoted as content being discussed, such as a political ad or a \
commercial the hosts are reacting to.
- Closing credits that thank staff or crew ("this show is brought to you thanks to the \
work of...", "produced by...", "our editor is...").
- Requests to rate, review, subscribe to, follow, or share the current show.

BOUNDARIES
- start_seg is the first segment of the pitch. A read often starts before the brand is \
named ("You know what I can't stand? Doing my taxes."), so start where the promotion \
begins, not where the brand first appears.
- end_seg is the last segment of the read: the final call to action, offer code, URL, or \
legal disclaimer. Do not include the conversation that resumes afterwards.
- Report one entry per brand per contiguous read. Back-to-back reads for different brands \
are separate entries, even inside one ad break.
- Windows overlap with their neighbours, so a read near the start or end of the window may \
be cut off. Report what you see in this window and use the segment indexes shown.

FIELDS
- brand: the advertised brand's real, correctly spelled name, even when the speech-to-text \
garbled it ("quionce" -> "Quince", "better health" in a therapy pitch -> "BetterHelp"). \
For a network promo, the name of the promoted show. Use "unknown" only when the brand \
cannot be identified.
- read_type: "host-read", "produced-spot", "network-promo", or "uncertain" when it is \
unclear whether the passage is a paid ad.
- offer_codes: promo codes and offers exactly as spoken (for example "SAVE20" or "20% \
off"). Empty list if none.
- urls: URLs or vanity paths as spoken, written as a URL ("helixsleep.com/show"). Empty \
list if none.
- confidence: from 0 to 1, how sure you are that this is a paid ad read with these \
boundaries.
- evidence: a short verbatim quote from the read, at most 25 words, that shows it is an ad.

If the window contains no ad reads, return {"reads": []}. Do not invent reads to fill the \
list."""

READ_SCHEMA = {
    "type": "object",
    "properties": {
        "reads": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "brand": {"type": "string"},
                    "start_seg": {"type": "integer"},
                    "end_seg": {"type": "integer"},
                    "read_type": {"type": "string", "enum": list(READ_TYPES)},
                    "offer_codes": {"type": "array", "items": {"type": "string"}},
                    "urls": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number"},
                    "evidence": {"type": "string"},
                },
                "required": ["brand", "start_seg", "end_seg", "read_type", "offer_codes",
                             "urls", "confidence", "evidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["reads"],
    "additionalProperties": False,
}


class ScreenUnavailable(RuntimeError):
    """The screen stage cannot run here (no credentials, no SDK, key or
    model rejected)."""


class ScreenAborted(RuntimeError):
    """An API call failed mid-transcript (after the SDK's own retries).
    Carries the usage of the windows that did complete, so it is billed."""

    def __init__(self, cause, usage: dict, windows_done: int, windows: int):
        super().__init__(f"window {windows_done + 1}/{windows}: "
                         f"{type(cause).__name__}: {cause}")
        self.usage, self.windows_done, self.windows = usage, windows_done, windows


_FATAL_NAMES = {"AuthenticationError", "PermissionDeniedError", "NotFoundError"}
_PERMANENT_NAMES = {"BadRequestError", "UnprocessableEntityError", "RequestTooLargeError"}
_FATAL_TEXT = re.compile(r"credit balance|billing|payment required|insufficient.?credit", re.I)


def classify_error(exc) -> str:
    """"fatal": no request from this worker can succeed (bad key, no
    permission, unknown model, out of credit) - stop the worker; "permanent":
    this transcript cannot be screened (bad request) - skip screening it;
    "transient": anything else (429/5xx/network after SDK retries)."""
    if isinstance(exc, ScreenAborted) and exc.__cause__ is not None:
        exc = exc.__cause__
    status = getattr(exc, "status_code", None)
    if type(exc).__name__ in _FATAL_NAMES or status in (401, 402, 403, 404) \
            or _FATAL_TEXT.search(str(exc)):
        return "fatal"
    if type(exc).__name__ in _PERMANENT_NAMES or status in (400, 413, 422):
        return "permanent"
    return "transient"


# ---------------------------------------------------------------- client

def credentials_present(env=None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN"))


def make_client(timeout: float = 120.0, max_retries: int = 3):
    """An Anthropic client, or ScreenUnavailable with the fix in the message.
    The SDK retries 408/409/429/5xx and connection errors itself; the
    timeout keeps one hung call well inside the lease heartbeat ceiling."""
    if not credentials_present():
        raise ScreenUnavailable(
            "screen stage needs ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) in the "
            "environment; none is set")
    try:
        import anthropic
    except ImportError as exc:
        raise ScreenUnavailable("screen stage needs the Anthropic SDK: "
                                "pip install anthropic") from exc
    return anthropic.Anthropic(timeout=timeout, max_retries=max_retries)


def preflight(client, model: str, *, tries: int = 3, retry_sleep_s: float = 5.0) -> None:
    """One cheap authenticated call (models.retrieve) before any claim, so
    a bad key, missing permission or unknown model stops the worker at
    start instead of burning episode attempts. A transient error (network
    blip, 5xx) is retried a few times before giving up."""
    models = getattr(client, "models", None)
    if models is None:
        return
    for attempt in range(tries):
        try:
            models.retrieve(model)
            return
        except Exception as exc:
            if classify_error(exc) == "transient" and attempt + 1 < tries:
                time.sleep(retry_sleep_s)
                continue
            raise ScreenUnavailable(f"screen preflight failed for model {model}: "
                                    f"{type(exc).__name__}: {exc}") from exc


def price_warning(model: str) -> str | None:
    if model in PRICES_PER_MTOK:
        return None
    return (f"WARNING: model {model} has no price in llm_screen.PRICES_PER_MTOK; "
            "cost_usd is recorded as unknown (null), not $0")


# ---------------------------------------------------------------- windows

def fmt_ts(seconds: float) -> str:
    total = int(max(0.0, float(seconds)))
    return f"{total // 60:02d}:{total % 60:02d}"


def split_windows(segments: list, window_s: float = 600.0,
                  overlap_s: float = 60.0) -> list:
    """[(lo, hi)] segment index ranges (hi exclusive) of overlapping time
    windows: window k covers [t0 + k*step, t0 + k*step + window_s)."""
    if not segments:
        return []
    step = max(1.0, window_s - overlap_s)
    t0 = float(segments[0]["start"])
    n = len(segments)
    wins, lo = [], 0
    while lo < n:
        while lo < n and float(segments[lo]["end"]) <= t0:
            lo += 1
        if lo >= n:
            break
        hi = lo
        while hi < n and float(segments[hi]["start"]) < t0 + window_s:
            hi += 1
        hi = max(hi, lo + 1)  # a segment longer than the window still goes
        if not wins or hi > wins[-1][1]:  # skip windows a previous one contains
            wins.append((lo, hi))
        if hi >= n:
            break
        t0 += step
    return wins


def render_window(segments: list, lo: int, hi: int) -> str:
    return "\n".join(f"[{i}] {fmt_ts(segments[i]['start'])} "
                     + re.sub(r"\s+", " ", str(segments[i].get("text") or "")).strip()
                     for i in range(lo, hi))


def build_request(segments: list, lo: int, hi: int, *, model: str, effort: str = "low",
                  k: int = 0, n: int = 1) -> dict:
    """messages.create keyword arguments for one window. The system prompt
    is byte-identical across windows and episodes, so it caches."""
    head = (f"Transcript window {k + 1} of {n}: segments {lo}-{hi - 1}, "
            f"{fmt_ts(segments[lo]['start'])} to {fmt_ts(segments[hi - 1]['end'])}.")
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content":
                      f"{head}\n<transcript>\n{render_window(segments, lo, hi)}\n</transcript>"}],
        "output_config": {"effort": effort,
                          "format": {"type": "json_schema", "schema": READ_SCHEMA}},
    }


# ---------------------------------------------------------------- responses

def usage_of(message) -> dict:
    u = getattr(message, "usage", None)
    return {k: int(getattr(u, k, 0) or 0) for k in USAGE_KEYS}


def add_usage(total: dict, more: dict) -> dict:
    for k in USAGE_KEYS:
        total[k] = total.get(k, 0) + int(more.get(k, 0) or 0)
    return total


def _clean_strs(values) -> list:
    return [str(v).strip() for v in values or [] if str(v).strip()]


def parse_response(message, segments: list, lo: int, hi: int, k: int = 0) -> tuple:
    """(reads, failure_reason | None). Seg indexes are clamped to the
    window; reads entirely outside it are dropped."""
    stop = getattr(message, "stop_reason", None)
    if stop in ("refusal", "max_tokens"):
        return [], stop
    text = next((b.text for b in getattr(message, "content", None) or []
                 if getattr(b, "type", None) == "text"), None)
    try:
        items = json.loads(text)["reads"]
        if not isinstance(items, list):
            raise TypeError("reads is not a list")
    except (TypeError, ValueError, KeyError):
        return [], "invalid_json"
    reads = []
    for it in items:
        try:
            a, b = int(it["start_seg"]), int(it["end_seg"])
            brand = str(it.get("brand") or "unknown").strip() or "unknown"
            conf = float(it.get("confidence") or 0.0)
        except (TypeError, ValueError, KeyError):
            continue
        a, b = min(a, b), max(a, b)
        if b < lo or a >= hi:
            continue
        a, b = max(a, lo), min(b, hi - 1)
        read_type = it.get("read_type") if it.get("read_type") in READ_TYPES else "uncertain"
        reads.append({
            "brand": brand, "brand_normalized": normalize_brand(brand) or "unknown",
            "start_seg": a, "end_seg": b,
            "start_s": float(segments[a]["start"]), "end_s": float(segments[b]["end"]),
            "read_type": read_type, "offer_codes": _clean_strs(it.get("offer_codes")),
            "urls": _clean_strs(it.get("urls")), "confidence": min(1.0, max(0.0, conf)),
            "evidence": str(it.get("evidence") or "")[:400], "windows": [k]})
    return reads, None


def merge_reads(reads: list, gap_segs: int = MERGE_GAP_SEGS) -> list:
    """Same normalized brand and overlapping or adjacent seg ranges (gap <=
    gap_segs) -> one read: union range, max confidence (its evidence and
    read_type win), union of codes, URLs and windows."""
    merged = []
    for r in sorted(reads, key=lambda r: (r["brand_normalized"], r["start_seg"], r["end_seg"])):
        prev = merged[-1] if merged else None
        if prev and prev["brand_normalized"] == r["brand_normalized"] \
                and r["start_seg"] - prev["end_seg"] <= gap_segs:
            if r["confidence"] > prev["confidence"]:
                for key in ("brand", "confidence", "evidence", "read_type"):
                    prev[key] = r[key]
            if r["end_seg"] > prev["end_seg"]:
                prev["end_seg"], prev["end_s"] = r["end_seg"], r["end_s"]
            for key in ("offer_codes", "urls", "windows"):
                prev[key] = sorted(set(prev[key]) | set(r[key]), key=str)
            continue
        merged.append({**r, **{key: list(r[key]) for key in ("offer_codes", "urls", "windows")}})
    merged.sort(key=lambda r: (r["start_seg"], r["end_seg"], r["brand_normalized"]))
    return merged


def assemble(segments: list, windows: list, outcomes: list, *, model: str,
             effort: str) -> dict:
    """Combine per-window (reads, failure, usage) into the screen result."""
    reads, failed, usage = [], [], add_usage({}, {})
    for k, ((lo, hi), (rs, failure, u)) in enumerate(zip(windows, outcomes)):
        add_usage(usage, u)
        reads.extend(rs)
        if failure:
            failed.append({"window": k, "seg_range": [lo, hi - 1], "reason": failure})
    return {"reads": merge_reads(reads), "usage": usage, "model": model,
            "prompt_version": PROMPT_VERSION, "effort": effort, "windows": len(windows),
            "failed_windows": failed}


def screen_transcript(segments: list, *, client, model: str, window_s: float = 600.0,
                      overlap_s: float = 60.0, effort: str = "low") -> dict:
    """Screen one transcript. Returns {"reads", "usage", "model",
    "prompt_version", "effort", "windows", "failed_windows"}. A refusal,
    max_tokens stop or unparseable output fails that window only; an API
    error (after the SDK's own retries) raises ScreenAborted carrying the
    usage of the windows already done."""
    windows = split_windows(segments, window_s, overlap_s)
    outcomes = []
    for k, (lo, hi) in enumerate(windows):
        try:
            msg = client.messages.create(**build_request(segments, lo, hi, model=model,
                                                         effort=effort, k=k, n=len(windows)))
        except Exception as exc:
            usage = add_usage({}, {})
            for _, _, u in outcomes:
                add_usage(usage, u)
            raise ScreenAborted(exc, usage, k, len(windows)) from exc
        rs, failure = parse_response(msg, segments, lo, hi, k)
        outcomes.append((rs, failure, usage_of(msg)))
    return assemble(segments, windows, outcomes, model=model, effort=effort)


def assign_read_ids(ep_id: str, result: dict) -> dict:
    for n, r in enumerate(result["reads"]):
        r["read_id"] = f"{ep_id}.llm{n}"
    return result


# ---------------------------------------------------------------- cost

def cost_usd(usage: dict, model: str, batch: bool = False) -> float | None:
    price = PRICES_PER_MTOK.get(model)
    if price is None:
        return None
    u = {k: int((usage or {}).get(k, 0) or 0) for k in USAGE_KEYS}
    usd = (u["input_tokens"] * price["input"]
           + u["output_tokens"] * price["output"]
           + u["cache_read_input_tokens"] * price["input"] * CACHE_READ_MULT
           + u["cache_creation_input_tokens"] * price["input"] * CACHE_WRITE_MULT) / 1e6
    return round(usd * (BATCH_DISCOUNT if batch else 1.0), 8)


def estimate(segments: list, window_s: float = 600.0, overlap_s: float = 60.0,
             model: str = DEFAULT_MODEL) -> dict:
    """Window count and a character-based input-token estimate (no API)."""
    windows = split_windows(segments, window_s, overlap_s)
    user = sum(len(build_request(segments, lo, hi, model=model, k=k, n=len(windows))
                   ["messages"][0]["content"]) for k, (lo, hi) in enumerate(windows))
    return {"windows": len(windows),
            "system_tokens_est": round(len(SYSTEM_PROMPT) / CHARS_PER_TOKEN_EST),
            "user_tokens_est": round(user / CHARS_PER_TOKEN_EST)}


# ---------------------------------------------------------------- candidates

def _confidence_bucket(score: float) -> str:
    return "high" if score >= 0.8 else "medium" if score >= 0.5 else "low"


def llm_candidates(ep_id: str, screen_doc: dict, meta: dict | None = None) -> list:
    """Screen reads as detector-shaped candidate records ("source":
    "llm-screen"), so score_labels.py and the seed importer can read them."""
    meta = meta or {}
    out = []
    for n, r in enumerate(screen_doc.get("reads") or []):
        brand = r.get("brand")
        spoken = None if (brand or "unknown").strip().lower() == "unknown" \
            else normalize_brand(brand)
        out.append({
            "candidate_id": r.get("read_id") or f"{ep_id}.llm{n}",
            "ep_id": ep_id,
            "show_title": meta.get("show_title", "?"),
            "ep_title": meta.get("ep_title", "?"),
            "pub_date": meta.get("pub_date", "?"),
            "fetch_date": meta.get("fetch_date"),
            "brand": spoken or "(unnamed)",
            "brand_spoken": spoken,
            "brand_raw": brand,
            "brand_url": None,
            "brand_code": None,
            "anchor": f"llm:{r.get('read_type')}",
            "confidence": _confidence_bucket(float(r.get("confidence") or 0)),
            "confidence_score": r.get("confidence"),
            "offer_codes": list(r.get("offer_codes") or []),
            "urls": list(r.get("urls") or []),
            "start_s": r.get("start_s"),
            "end_s": r.get("end_s"),
            "provenance": {"model": screen_doc.get("model"),
                           "prompt_version": screen_doc.get("prompt_version"),
                           "read_type": r.get("read_type"),
                           "seg_range": [r.get("start_seg"), r.get("end_seg")],
                           "windows": r.get("windows")},
            "excerpt": r.get("evidence") or "",
            "source": "llm-screen",
        })
    return out
