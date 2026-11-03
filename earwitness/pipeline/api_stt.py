"""Hosted speech-to-text (Groq, OpenRouter) for the refine pass and the
transcribe stage, so heavy transcription can run on a provider's API instead
of a local GPU.

`ApiTranscriber` keeps refine's transcriber contract (describe / transcribe
/ close; clip-relative segments; ClipFailed for one clip, SpeechUnavailable
to stop the run) and adds `transcribe_bytes` for full-episode chunks.
`transcribe_episode` decodes an episode with chunked_stt's sequential window
decoder, encodes each window as 16 kHz mono FLAC (under the upload limit)
and stitches the windows with the existing seam resolution.

Provider facts (checked 2026-10-09 against the official docs):

- Groq, POST https://api.groq.com/openai/v1/audio/transcriptions, OpenAI
  multipart upload (file, model, response_format=verbose_json gives segments
  with start/end/text). Upload cap 25 MB free tier / 100 MB dev tier;
  whisper-large-v3-turbo $0.04 per audio hour, minimum billed length 10 s per
  request. Free-tier limits for turbo: 20 RPM, 2K RPD, 7.2K audio-s per hour,
  28.8K audio-s per day. 429 carries retry-after (seconds).
  https://console.groq.com/docs/speech-to-text
  https://console.groq.com/docs/rate-limits
- OpenRouter, POST https://openrouter.ai/api/v1/audio/transcriptions, JSON
  body with base64 `input_audio` {data, format}; response_format
  verbose_json returns segments (start/end/text) when the routed provider
  supports it (a 400 otherwise), plus `usage` {seconds, cost}. Multipart cap
  25 MB, ~60 s upstream processing timeout. openai/whisper-large-v3-turbo
  is listed at $0.04 per audio hour; its minimum billed length is not
  documented, so Groq's 10 s is assumed (it can only overstate cost). A 402
  is fatal unless its limit source is the transient in-flight budget.
  https://openrouter.ai/docs/guides/overview/multimodal/stt
  https://openrouter.ai/docs/api-reference/limits

A response without segments but with text (a text-only model) is mapped to
one segment spanning the whole request audio ("segments_source":
"whole-text"): enough to name a clip's brand, too coarse for span timing.

Stdlib only (urllib; multipart built by hand). The API key is held in a
private attribute and scrubbed from every error message; it is never logged
or written.
"""
from __future__ import annotations

import base64
import http.client
import json
import math
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from earwitness.pipeline import chunked_stt as stt
from earwitness.pipeline.refine import BudgetExhausted, ClipFailed, SpeechUnavailable

PROVIDERS = {
    "groq": {
        "url": "https://api.groq.com/openai/v1/audio/transcriptions",
        "preflight_url": "https://api.groq.com/openai/v1/models",
        "model": "whisper-large-v3-turbo",
        "key_env": "GROQ_API_KEY",
        "upload": "multipart",
        "max_upload_bytes": 25 * 1024 * 1024,  # free tier (dev tier: 100 MB)
        "rpm": 20.0,                           # free tier, whisper-large-v3-turbo
        "docs": "https://console.groq.com/docs/speech-to-text",
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/audio/transcriptions",
        "preflight_url": "https://openrouter.ai/api/v1/key",
        "model": "openai/whisper-large-v3-turbo",
        "key_env": "OPENROUTER_API_KEY",
        "upload": "json-base64",
        "max_upload_bytes": 25 * 1024 * 1024,
        "rpm": 20.0,
        "docs": "https://openrouter.ai/docs/guides/overview/multimodal/stt",
    },
}
API_BACKENDS = tuple(PROVIDERS)

# (provider, model) -> (USD per audio hour, minimum billed seconds per request, source)
PRICES = {
    ("groq", "whisper-large-v3-turbo"): (0.04, 10.0, "https://console.groq.com/docs/speech-to-text"),
    ("groq", "whisper-large-v3"): (0.111, 10.0, "https://console.groq.com/docs/speech-to-text"),
    ("openrouter", "openai/whisper-large-v3-turbo"):
        (0.04, 10.0, "https://openrouter.ai/openai/whisper-large-v3-turbo (10 s minimum assumed)"),
    ("openrouter", "openai/whisper-large-v3"):
        (0.111, 10.0, "https://openrouter.ai/openai/whisper-large-v3 (10 s minimum assumed)"),
}

CONTENT_TYPES = {"mp3": "audio/mpeg", "flac": "audio/flac", "wav": "audio/wav",
                 "m4a": "audio/mp4", "ogg": "audio/ogg", "webm": "audio/webm"}
CHUNK_FORMAT = "flac"   # full-episode windows: lossless, ~10 MB per 600 s at 16 kHz mono
RETRY_STATUS = (408, 409, 429, 500, 502, 503, 504, 520, 522, 524, 529)
_FATAL_STATUS = (401, 402, 403, 404)
_PERMANENT_STATUS = (400, 413, 415, 422)
_FATAL_TEXT = re.compile(r"insufficient.?(credit|balance|quota)|payment required"
                         r"|invalid.?api.?key", re.I)
_TRANSIENT_402 = re.compile(r"in_flight_budget", re.I)


class ApiError(RuntimeError):
    """An HTTP or network failure (status None = network); never holds the key
    (callers redact before it truncates). `reached`: the request may have
    been processed and billed (5xx, 408, timeouts, non-JSON 200)."""

    def __init__(self, status, body: str = "", retry_after: float | None = None,
                 reached: bool = False):
        self.status, self.body, self.retry_after = status, str(body or "")[:300], retry_after
        self.reached = reached
        super().__init__(f"HTTP {status}: {self.body}" if status else self.body)


class ApiUnavailable(SpeechUnavailable):
    """No key: nothing can run."""


class ApiFatal(SpeechUnavailable):
    """Rejected key, permission, payment or unknown model: stop (exit 3)."""


class RateLimited(SpeechUnavailable):
    """Still rate limited after the retries: stop without charging a clip attempt."""


class Aborted(RuntimeError):
    """A full-episode transcription was stopped between chunks (lease lost, shutdown)."""


def classify_error(exc) -> str:
    """"fatal" (stop the run), "permanent" (this clip/request can never
    succeed), "rate_limited" or "transient" (retried)."""
    status = getattr(exc, "status", None)
    if status == 429:
        return "rate_limited"
    if status == 402 and _TRANSIENT_402.search(str(exc)):
        return "transient"
    if status in _FATAL_STATUS or (status in _PERMANENT_STATUS and _FATAL_TEXT.search(str(exc))):
        return "fatal"
    if status in _PERMANENT_STATUS:
        return "permanent"
    return "transient"


# ---------------------------------------------------------------- cost

def price(provider: str, model: str) -> tuple:
    try:
        return PRICES[(provider, model)]
    except KeyError:
        raise ValueError(f"no price for {provider}/{model}; known: "
                         f"{sorted(f'{p}/{m}' for p, m in PRICES)}") from None


def billed_seconds(audio_s: float, min_s: float) -> float:
    return max(float(audio_s), float(min_s))


def request_cost(audio_s: float, provider: str, model: str) -> dict:
    """List-price cost of one request of `audio_s` seconds."""
    usd_h, min_s, _ = price(provider, model)
    billed = billed_seconds(audio_s, min_s)
    return {"audio_s": round(float(audio_s), 3), "billed_s": round(billed, 3),
            "usd": round(billed / 3600.0 * usd_h, 8), "requests": 1}


def add_costs(costs) -> dict:
    out = {"audio_s": 0.0, "billed_s": 0.0, "usd": 0.0, "requests": 0}
    for c in costs:
        for k in out:
            out[k] += (c or {}).get(k) or 0
    return {k: (v if k == "requests" else round(v, 8 if k == "usd" else 3))
            for k, v in out.items()}


def window_lengths(duration_s: float, chunk_s: float, overlap_s: float) -> list:
    """Lengths of the windows stt.iter_windows yields over `duration_s`."""
    step = chunk_s - overlap_s
    n = 1 + max(0, math.ceil((duration_s - chunk_s) / step - 1e-9))
    return [min(chunk_s, duration_s - k * step) for k in range(n)]


def episode_cost(duration_s: float, provider: str, model: str, *, chunk_s: float,
                 overlap_s: float) -> dict:
    """Estimated cost of a chunked full-episode transcription (overlaps are
    billed twice, every request at least the minimum)."""
    return add_costs(request_cost(n, provider, model)
                     for n in window_lengths(float(duration_s), chunk_s, overlap_s))


LEDGER_SCHEMA = """CREATE TABLE IF NOT EXISTS api_spend (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    owner TEXT, provider TEXT, model TEXT, what TEXT,
    status TEXT NOT NULL CHECK (status IN ('reserved', 'hold', 'charged', 'released')),
    reserved_usd REAL NOT NULL,
    charged_usd REAL)"""
_COMMITTED_SQL = ("SELECT COALESCE(SUM(CASE status WHEN 'charged' THEN charged_usd"
                  " WHEN 'released' THEN 0 ELSE reserved_usd END), 0) FROM api_spend")
OVERRUN_FACTOR = 1.25  # a charge this far over its reservation stops further requests


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Budget:
    """A hard USD cap on hosted-STT spend, kept in a ledger table (api_spend)
    so every process sharing the ledger (the corpus's queue.sqlite3) shares
    one budget and a restart resumes from what was spent. Before each request
    its estimated cost is reserved inside an IMMEDIATE transaction; the
    request is refused (BudgetExhausted) when charged + open reservations +
    it would pass the cap. A reservation is settled with what was charged, or
    released when nothing was billed. An unsettled reservation (a crashed
    process) keeps counting: the cap errs high. A hold reserves a whole
    episode up front; its chunks draw it down. Without a ledger path the
    ledger lives in memory (one process). `spent` is this process's charges;
    total() is the ledger's committed spend."""

    def __init__(self, max_usd: float | None, *, ledger=None, owner: str | None = None,
                 provider: str | None = None, model: str | None = None):
        if max_usd is not None and not (math.isfinite(max_usd) and max_usd > 0):
            raise ValueError(f"budget cap must be a finite amount > 0, got {max_usd!r}")
        self.max_usd, self.spent = max_usd, 0.0
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}"
        self.provider, self.model = provider, model
        self._con = sqlite3.connect(str(ledger) if ledger else ":memory:", timeout=30.0,
                                    isolation_level=None, check_same_thread=False)
        self._con.execute("PRAGMA busy_timeout=30000")
        if ledger:
            self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute(LEDGER_SCHEMA)
        self._lock = threading.Lock()
        self._reserved: dict = {}
        self._halt = None

    def total(self) -> float:
        with self._lock:
            return round(float(self._con.execute(_COMMITTED_SQL).fetchone()[0]), 8)

    def _refuse(self, committed: float, usd: float, what: str):
        raise BudgetExhausted(f"budget ${self.max_usd:g} reached: committed ${committed:.4f} "
                              f"(charged + in flight), {what} ~${usd:.4f}")

    def check(self, usd: float, what: str = "next request") -> None:
        """Would `usd` more fit? (No reservation.)"""
        if self._halt:
            raise BudgetExhausted(self._halt)
        committed = self.total()
        if self.max_usd is not None and committed + usd > self.max_usd + 1e-12:
            self._refuse(committed, usd, what)

    def reserve(self, usd: float, what: str, *, parent: int | None = None,
                hold: bool = False) -> int:
        if self._halt:
            raise BudgetExhausted(self._halt)
        with self._lock:
            con = self._con
            con.execute("BEGIN IMMEDIATE")
            try:
                if parent is not None:
                    row = con.execute("SELECT reserved_usd FROM api_spend WHERE id=? AND"
                                      " status='hold'", (parent,)).fetchone()
                    if row:
                        con.execute("UPDATE api_spend SET reserved_usd=reserved_usd-?,"
                                    " updated_at=? WHERE id=?",
                                    (min(usd, row[0]), _now(), parent))
                committed = float(con.execute(_COMMITTED_SQL).fetchone()[0])
                if self.max_usd is not None and committed + usd > self.max_usd + 1e-12:
                    self._refuse(committed, usd, what)
                tok = con.execute(
                    "INSERT INTO api_spend (created_at, updated_at, owner, provider, model, what,"
                    " status, reserved_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (_now(), _now(), self.owner, self.provider, self.model, what[:200],
                     "hold" if hold else "reserved", usd)).lastrowid
                con.execute("COMMIT")
            except BaseException:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
        self._reserved[tok] = usd
        return tok

    def settle(self, tok: int, usd: float) -> None:
        with self._lock:
            self._con.execute("UPDATE api_spend SET status='charged', charged_usd=?,"
                              " updated_at=? WHERE id=?", (usd, _now(), tok))
        self.spent = round(self.spent + usd, 8)
        reserved = self._reserved.pop(tok, usd)
        if usd > reserved * OVERRUN_FACTOR + 1e-6:
            self._halt = (f"a request charged ${usd:.4f}, which exceeded its reservation "
                          f"${reserved:.4f}; stopping before further requests")

    def release(self, tok: int) -> None:
        with self._lock:
            self._con.execute("UPDATE api_spend SET status='released', updated_at=? WHERE id=?"
                              " AND status IN ('reserved', 'hold')", (_now(), tok))
        self._reserved.pop(tok, None)


# ---------------------------------------------------------------- key

def load_key(provider: str, env=None, env_file=None) -> str:
    """The provider key from the environment, else from `env_file` (KEY=VALUE
    lines). Error messages name the variable, never a value."""
    name = PROVIDERS[provider]["key_env"]
    key = ((os.environ if env is None else env).get(name) or "").strip()
    if not key and env_file and Path(env_file).is_file():
        for line in Path(env_file).read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].lstrip()
            k, sep, v = line.partition("=")
            if sep and k.strip() == name:
                key = v.strip().strip("'\"").strip()
    if not key:
        raise ApiUnavailable(f"{name} is not set (environment"
                             + (f" or {env_file}" if env_file else "") + ")")
    return key


# ---------------------------------------------------------------- request bodies

def multipart(fields: list, file_field: str, filename: str, content: bytes,
              content_type: str, boundary: str | None = None) -> tuple:
    """(body, Content-Type header) of a multipart/form-data request."""
    b = boundary or "earwitness" + uuid.uuid4().hex
    filename = re.sub(r'["\r\n\\]', "_", filename)
    parts = [f'--{b}\r\nContent-Disposition: form-data; name="{n}"\r\n\r\n{v}\r\n'.encode()
             for n, v in fields]
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{file_field}"; '
                 f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode()
                 + content + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={b}"


def parse_response(resp, audio_s: float) -> dict:
    """{"segments", "language", "duration", "segments_source"} from a
    verbose_json (or plain json) transcription response."""
    if not isinstance(resp, dict):
        raise ClipFailed("transcription response is not a JSON object")
    dur = resp.get("duration")
    dur = float(dur) if isinstance(dur, (int, float)) and not isinstance(dur, bool) \
        and dur > 0 else None
    segs, out = resp.get("segments"), []
    if isinstance(segs, list) and segs:
        for i, s in enumerate(segs):
            try:
                a, b, text = float(s["start"]), float(s["end"]), str(s.get("text") or "").strip()
            except (KeyError, TypeError, ValueError):
                raise ClipFailed(f"malformed segment {i} in transcription response") from None
            if text and b > a:
                out.append({"start": round(a, 3), "end": round(b, 3), "text": text})
        source = "provider"
    elif str(resp.get("text") or "").strip():
        out = [{"start": 0.0, "end": round(dur or float(audio_s), 3),
                "text": str(resp["text"]).strip()}]
        source = "whole-text"
    else:
        source = "empty"
    return {"segments": out, "language": resp.get("language"), "duration": dur,
            "segments_source": source}


def probe_duration(path, ffprobe: str | None = None) -> float | None:
    exe = ffprobe or shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
    try:
        out = subprocess.run([exe, "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", str(path)], capture_output=True,
                             text=True, timeout=60).stdout.strip()
        return float(out) if out else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


# ---------------------------------------------------------------- transcriber

class ApiTranscriber:
    """Transcribes audio through a provider's API. Retries 429/5xx/network
    errors with bounded backoff, honoring Retry-After (up to
    max_retry_after_s); paces requests to `rpm`; checks every request against
    `budget` before sending it; reports cost per request."""

    def __init__(self, provider: str, *, key: str, model: str | None = None,
                 budget: Budget | None = None, timeout_s: float = 120.0, retries: int = 4,
                 backoff_s: float = 2.0, max_backoff_s: float = 60.0,
                 max_retry_after_s: float = 600.0, rpm: float | None = None,
                 language: str | None = None, url: str | None = None,
                 preflight_url: str | None = None, opener=None, sleep=time.sleep,
                 clock=time.monotonic, ffprobe: str | None = None, log=print):
        if provider not in PROVIDERS:
            raise ValueError(f"unknown provider {provider!r}; use {API_BACKENDS}")
        p = PROVIDERS[provider]
        self.provider, self.model = provider, model or p["model"]
        price(provider, self.model)  # unknown model: fail before any request
        self._key = key
        self.url, self.preflight_url = url or p["url"], preflight_url or p["preflight_url"]
        self.budget = budget if budget is not None else Budget(None)
        self.timeout_s, self.retries, self.backoff_s = timeout_s, retries, backoff_s
        self.max_backoff_s, self.max_retry_after_s = max_backoff_s, max_retry_after_s
        rpm = p["rpm"] if rpm is None else rpm
        self.min_interval_s = 60.0 / rpm if rpm and rpm > 0 else 0.0
        self.language, self.opener = language, opener or urllib.request.urlopen
        self.sleep, self.clock, self.ffprobe, self.log = sleep, clock, ffprobe, log
        self._last = float("-inf")
        self._checked = False
        self.requests = 0

    def __repr__(self):
        return f"ApiTranscriber(provider={self.provider!r}, model={self.model!r})"

    # -- contract
    def describe(self) -> dict:
        if not self._checked:
            self.preflight()
        usd_h, min_s, source = price(self.provider, self.model)
        return {"model": self.model, "backend": f"{self.provider}-api",
                "versions": {"provider": self.provider, "endpoint": self.url,
                             "api_model": self.model, "usd_per_audio_hour": usd_h,
                             "min_billed_s": min_s, "pricing_source": source}}

    def transcribe(self, path) -> dict:
        """One clip file -> {"segments" (clip-relative), "language", "wall_s",
        "decoded_s", "cost", "segments_source"}."""
        path = Path(path)
        data = path.read_bytes()
        audio_s = probe_duration(path, self.ffprobe) or len(data) * 8 / 64000.0
        return self.transcribe_bytes(data, path.suffix.lstrip(".").lower() or "mp3", audio_s,
                                     name=path.name)

    def close(self):
        pass

    # -- requests
    def estimate(self, audio_s: float) -> dict:
        return request_cost(math.ceil(audio_s * 100) / 100, self.provider, self.model)

    def _charge_for(self, resp, audio_s: float) -> float:
        """List price on the longer of the provider's duration and our measured
        audio, or the provider-reported cost when that is higher."""
        dur = resp.get("duration") if isinstance(resp, dict) else None
        ok = isinstance(dur, (int, float)) and not isinstance(dur, bool) and dur > 0
        usd = request_cost(max(dur, audio_s) if ok else audio_s, self.provider,
                           self.model)["usd"]
        reported = _reported_cost(resp)
        return max(usd, reported) if reported is not None else usd

    def transcribe_bytes(self, data: bytes, fmt: str, audio_s: float, *,
                         name: str = "audio", hold: int | None = None) -> dict:
        limit = PROVIDERS[self.provider]["max_upload_bytes"]
        if len(data) > limit:
            raise ClipFailed(f"{name}: {len(data)} bytes is over the {self.provider} upload "
                             f"limit ({limit})")
        est = self.estimate(audio_s)
        body, ctype = self._body(data, fmt, name)
        t0 = self.clock()
        try:
            resp, charged, n = self._post(body, ctype, est["usd"], f"{name} ({audio_s:.1f} s)",
                                          hold, lambda r: self._charge_for(r, audio_s))
        except ApiError as exc:
            kind = classify_error(exc)
            msg = f"{self.provider} {kind}: {self._redact(str(exc))}"
            if kind == "fatal":
                raise ApiFatal(msg) from None
            if kind == "rate_limited":
                raise RateLimited(msg) from None
            raise ClipFailed(msg) from None
        parsed = parse_response(resp, audio_s)  # already charged: a bad answer is billed too
        cost = request_cost(max(parsed["duration"] or 0.0, audio_s), self.provider, self.model)
        cost["usd_list"], cost["usd"], cost["requests"] = cost["usd"], round(charged, 8), n
        if _reported_cost(resp) is not None:
            cost["usd_reported"] = _reported_cost(resp)
        return {"segments": parsed["segments"], "language": parsed["language"],
                "decoded_s": parsed["duration"] or round(audio_s, 3),
                "wall_s": round(self.clock() - t0, 3), "cost": cost,
                "segments_source": parsed["segments_source"]}

    def _body(self, data: bytes, fmt: str, name: str) -> tuple:
        if PROVIDERS[self.provider]["upload"] == "multipart":
            fields = [("model", self.model), ("response_format", "verbose_json"),
                      ("timestamp_granularities[]", "segment"), ("temperature", "0")]
            if self.language:
                fields.append(("language", self.language))
            return multipart(fields, "file", name, data,
                             CONTENT_TYPES.get(fmt, "application/octet-stream"))
        body = {"model": self.model, "response_format": "verbose_json",
                "timestamp_granularities": ["segment"], "temperature": 0,
                "input_audio": {"data": base64.b64encode(data).decode("ascii"), "format": fmt}}
        if self.language:
            body["language"] = self.language
        return json.dumps(body).encode(), "application/json"

    def _redact(self, text: str) -> str:
        return text.replace(self._key, "[redacted]") if self._key else text

    def _pace(self):
        if self.min_interval_s > 0:
            wait = self._last + self.min_interval_s - self.clock()
            if wait > 0:
                self.sleep(wait)
            self._last = self.clock()

    def _request(self, req) -> dict:
        try:
            with self.opener(req, timeout=self.timeout_s) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            try:
                body = e.read(65536).decode(errors="replace")
            except Exception:
                body = ""
            finally:
                e.close()
            ra = e.headers.get("Retry-After") if e.headers is not None else None
            try:
                ra = float(ra) if ra is not None else None
            except ValueError:
                ra = None
            raise ApiError(e.code, self._redact(body), ra,
                           reached=e.code >= 500 or e.code == 408) from None
        except http.client.HTTPException as e:  # e.g. IncompleteRead: sent, maybe billed
            raise ApiError(None, self._redact(f"{type(e).__name__}: {e}"), reached=True) from None
        except (urllib.error.URLError, OSError) as e:  # network, timeout, ssl
            reason = getattr(e, "reason", e)
            raise ApiError(None, self._redact(f"{type(e).__name__}: {e}"), reached=not isinstance(
                reason, (ConnectionRefusedError, socket.gaierror))) from None
        try:
            return json.loads(raw)
        except ValueError:
            raise ApiError(None, "response is not JSON: " + self._redact(
                raw[:65536].decode(errors="replace")), reached=True) from None

    def _headers(self, ctype: str | None = None) -> dict:
        h = {"Authorization": f"Bearer {self._key}", "User-Agent": "EarWitness/0.1 (research)"}
        if ctype:
            h["Content-Type"] = ctype
        return h

    def _post(self, body: bytes, ctype: str, est_usd: float, what: str, hold, charge_for):
        """(response, USD charged over all attempts, attempts charged). Every
        attempt is reserved before it is sent; an attempt that may have been
        processed (a response, 5xx, 408, timeout) is charged, a rejected one
        (4xx) or one that never connected is released."""
        charged, n = 0.0, 0
        for attempt in range(self.retries + 1):
            self._pace()
            tok = self.budget.reserve(est_usd, what, parent=hold)
            req = urllib.request.Request(self.url, data=body, method="POST",
                                         headers=self._headers(ctype))
            self.requests += 1
            try:
                resp = self._request(req)
            except ApiError as exc:
                if exc.reached:
                    self.budget.settle(tok, est_usd)
                    charged, n = charged + est_usd, n + 1
                else:
                    self.budget.release(tok)
                if classify_error(exc) in ("fatal", "permanent") or attempt == self.retries:
                    raise
                wait = exc.retry_after if exc.retry_after is not None \
                    else min(self.max_backoff_s, self.backoff_s * 2 ** attempt)
                if wait > self.max_retry_after_s:
                    raise
                self.log(f"[api-stt] {self.provider} {exc.status or 'network'} error, "
                         f"retry {attempt + 1}/{self.retries} in {wait:g}s")
                self.sleep(wait)
                continue
            usd = charge_for(resp)
            self.budget.settle(tok, usd)
            return resp, charged + usd, n + 1
        raise AssertionError("unreachable")

    def preflight(self) -> None:
        """One authenticated GET (models / key info; nothing billed). A
        rejected key raises ApiFatal; other failures are left to the first
        real request."""
        try:
            self._request(urllib.request.Request(self.preflight_url, method="GET",
                                                 headers=self._headers()))
        except ApiError as exc:
            if classify_error(exc) == "fatal":
                raise ApiFatal(f"{self.provider} preflight: {self._redact(str(exc))}") from None
            self.log(f"[api-stt] {self.provider} preflight inconclusive: "
                     f"{self._redact(str(exc))}")
        self._checked = True


def _reported_cost(resp):
    usage = resp.get("usage") if isinstance(resp, dict) else None
    cost = usage.get("cost") if isinstance(usage, dict) else None
    return round(float(cost), 8) if isinstance(cost, (int, float)) \
        and not isinstance(cost, bool) and cost >= 0 else None


def from_args(provider: str, *, model=None, env_file=None, max_cost_usd=None, rpm=None,
              timeout_s: float = 120.0, ledger=None, owner=None, language=None, env=None,
              log=print) -> ApiTranscriber:
    """A transcriber for a paid run: refuses to start without a USD cap.
    `ledger`: the sqlite file whose api_spend table holds the shared budget
    (the corpus's queue.sqlite3)."""
    if max_cost_usd is None:
        raise ApiUnavailable(f"--max-cost-usd is required for the paid {provider} backend")
    if not (math.isfinite(max_cost_usd) and max_cost_usd > 0):
        raise ApiUnavailable(f"--max-cost-usd must be a finite amount > 0, got {max_cost_usd!r}")
    model = model or PROVIDERS[provider]["model"]
    return ApiTranscriber(provider, key=load_key(provider, env=env, env_file=env_file),
                          model=model, rpm=rpm, timeout_s=timeout_s, language=language,
                          budget=Budget(max_cost_usd, ledger=ledger, owner=owner,
                                        provider=provider, model=model), log=log)


# ---------------------------------------------------------------- full episodes

def encode_window(samples, fmt: str = CHUNK_FORMAT, ffmpeg: str | None = None) -> bytes:
    """float32 16 kHz mono samples -> FLAC (or 64 kbps MP3) bytes."""
    codec = ["-c:a", "flac"] if fmt == "flac" else ["-c:a", "libmp3lame", "-b:a", "64k"]
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / f"w.{fmt}"
        proc = subprocess.run([ffmpeg or stt.ffmpeg_path(), "-nostdin", "-v", "error",
                               "-f", "f32le", "-ar", str(stt.SAMPLE_RATE), "-ac", "1",
                               "-i", "pipe:0", *codec, "-ac", "1", str(out)],
                              input=samples.tobytes(), capture_output=True, timeout=600)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg encode exited {proc.returncode}: "
                               f"{proc.stderr[-300:].decode(errors='replace')}")
        return out.read_bytes()


def transcribe_episode(path, transcriber: ApiTranscriber, *, chunk_s: float = 600.0,
                       overlap_s: float = 20.0, fmt: str = CHUNK_FORMAT,
                       ffmpeg: str | None = None, duration_s: float | None = None,
                       abort=None, progress=None) -> dict:
    """Chunked API transcription of a whole episode: sequential window decode,
    one request per window, seam stitching. With `duration_s`, the whole
    episode's estimated cost is checked against the budget before the first
    request, so a run never stops part-way through an episode on budget."""
    hold = None
    if duration_s:  # hold the whole episode's estimate: no stop part-way on budget
        est = episode_cost(duration_s, transcriber.provider, transcriber.model,
                           chunk_s=chunk_s, overlap_s=overlap_s)
        hold = transcriber.budget.reserve(est["usd"], f"episode {Path(path).name} "
                                          f"({duration_s:.0f} s, {est['requests']} requests)",
                                          hold=True)
    chunks, languages, costs, sources, stats = [], [], [], set(), {}
    try:
        for k, (offset, audio) in enumerate(stt.iter_windows(path, chunk_s, overlap_s, ffmpeg,
                                                             stats)):
            if abort is not None and (why := abort()):
                raise Aborted(why)
            n_s = len(audio) / stt.SAMPLE_RATE
            ans = transcriber.transcribe_bytes(encode_window(audio, fmt, ffmpeg), fmt, n_s,
                                               name=f"chunk{k:03d}.{fmt}", hold=hold)
            chunks.append({"offset": offset, "segments": ans["segments"]})
            languages.append({"chunk": k, "offset_s": round(offset, 2),
                              "length_s": round(n_s, 2), "language": ans.get("language"),
                              "probability": None, "votes": []})
            costs.append(ans["cost"])
            sources.add(ans.get("segments_source"))
            if progress:
                progress(k, offset, n_s)
    finally:
        if hold is not None:
            transcriber.budget.release(hold)
    decoded = stats.get("decoded_s", 0.0)
    weight: dict = {}
    for c in languages:
        weight[c["language"]] = weight.get(c["language"], 0.0) + c["length_s"]
    return {"segments": stt.stitch_chunks(chunks, overlap_s, decoded),
            "chunk_languages": languages,
            "language": max(weight, key=weight.get) if weight else None,
            "decoded_duration_s": round(decoded, 3), "cost": add_costs(costs),
            "segments_source": sorted(s for s in sources if s)}
