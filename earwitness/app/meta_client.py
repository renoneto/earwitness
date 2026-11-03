"""Meta model client for Ask: key loader, one stateless chat call, Ask's ledger.

Contract: ASK-CONTRACT section 1. Design: docs/plans/2026-10-23-meta-ask-design.md
sections 6, 7, 10 and 17; docs/hosting/meta-ask.md.

- The key is read from one file (`EARWITNESS_META_KEY_FILE`): a regular file
  owned by this process's user, no group or other bits, one word of printable
  ASCII. It is sent only in the `Authorization` header, over HTTPS, never on a
  redirect. No error, repr or log line carries the key, `Authorization` or the
  request body.
- Spend goes through `AskLedger`, a `JevBudget` with Meta prices and the Ask
  caps: one reservation of `TURN_BOUND_USD` per turn, settled with the summed
  usage of every call. A `HALT` file in the ledger folder stops every
  reservation at once.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import stat
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from earwitness.pipeline import jev_budget as jb

log = logging.getLogger(__name__)

MODEL = "muse-spark-1.3"
BASE_URL = "https://api.meta.ai/v1"
META_PRICING = jb.Pricing(1.25, 4.25, "https://dev.meta.ai/docs/pricing-rate-limits",
                          "2026-10-23")

# One turn's hard bound (base design 6): 160 KB of summed request bodies (bytes
# bound tokens) and 4 x 4,000 + 2 x 8,000 output tokens, plus 25%.
TURN_BODY_BYTES = 160_000
TURN_OUTPUT_TOKENS = 32_000
TURN_BOUND_USD = 0.42

ENV_KEY_FILE = "EARWITNESS_META_KEY_FILE"
DEFAULT_KEY_FILE = "/etc/earwitness/meta-model-api-key"
ENV_BUDGET_DIR = "EARWITNESS_META_BUDGET_DIR"
DEFAULT_BUDGET_DIR = "/srv/earwitness-vol/meta-budget"

MAX_KEY_BYTES = 4096
MAX_REPLY_BYTES = 8 * 1024 * 1024
FORBIDDEN_BODY_KEYS = ("user", "store", "metadata", "safety_identifier")

KEY_MISSING = "META_KEY_MISSING"
KEY_MODE = "META_KEY_MODE"
KEY_OWNER = "META_KEY_OWNER"
KEY_NOT_FILE = "META_KEY_NOT_FILE"
KEY_FORMAT = "META_KEY_FORMAT"

TIMEOUT = "META_TIMEOUT"
NETWORK = "META_NETWORK"
BAD_REPLY = "META_BAD_REPLY"

HALT = "META_HALT"
RECOVERY = "META_RECOVERY"
MONTH_CAP = "META_MONTH_CAP"
TOTAL_CAP = "META_TOTAL_CAP"
DAY_CAP = "META_DAY_CAP"

_TURN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def key_file_path(env=None) -> str:
    env = os.environ if env is None else env
    return env.get(ENV_KEY_FILE) or DEFAULT_KEY_FILE


def budget_dir(env=None) -> str:
    env = os.environ if env is None else env
    return env.get(ENV_BUDGET_DIR) or DEFAULT_BUDGET_DIR


# ---------------------------------------------------------------- key

class MetaKeyError(Exception):
    """The key file cannot be used. The message is the code only."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _valid_key(value) -> bool:
    """One word of printable ASCII (no space, tab, newline or control byte)."""
    return (isinstance(value, str) and 0 < len(value) <= MAX_KEY_BYTES
            and all(33 <= ord(c) <= 126 for c in value))


def _stat_code(st) -> str | None:
    if not stat.S_ISREG(st.st_mode):
        return KEY_NOT_FILE
    if st.st_uid != os.geteuid():
        return KEY_OWNER
    if st.st_mode & 0o077:
        return KEY_MODE
    return None


def _lstat_code(path) -> str:
    try:
        return _stat_code(os.lstat(os.fspath(path))) or KEY_MISSING
    except OSError:
        return KEY_MISSING


def load_key(path) -> str:
    """The key from `path`, or MetaKeyError. The file is opened without
    following a symlink and checked on the open descriptor, so a swap between
    the check and the read is not possible."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) \
        | getattr(os, "O_CLOEXEC", 0)
    # Every MetaKeyError is raised outside an `except` block, so it carries no
    # exception context at all.
    code = None
    try:
        fd = os.open(os.fspath(path), flags)
    except OSError as exc:
        # ELOOP (a symlink, refused by O_NOFOLLOW), EACCES (often another
        # owner's file): classify from lstat, which never follows a link.
        code = KEY_MISSING if isinstance(exc, FileNotFoundError) else _lstat_code(path)
    if code:
        raise MetaKeyError(code)
    data = b""
    try:
        code = _stat_code(os.fstat(fd))
        while code is None and len(data) <= MAX_KEY_BYTES + 1:
            chunk = os.read(fd, MAX_KEY_BYTES + 2 - len(data))
            if not chunk:
                break
            data += chunk
    except OSError:
        code = KEY_MISSING
    finally:
        os.close(fd)
    if code:
        raise MetaKeyError(code)
    if data.endswith(b"\n"):
        data = data[:-1]
    # Checked on bytes, so no decode error (which would hold the bytes) is ever raised.
    if not data or len(data) > MAX_KEY_BYTES or any(b < 33 or b > 126 for b in data):
        raise MetaKeyError(KEY_FORMAT)
    return data.decode("ascii")


# ---------------------------------------------------------------- client

class MetaCallError(Exception):
    """A chat call failed. `sent` is True once request bytes may have left
    this process (the charge is then unknown). The message is the code only."""

    def __init__(self, code: str, *, sent: bool = True, retry_after: float | None = None):
        super().__init__(code)
        self.code, self.sent, self.retry_after = code, sent, retry_after


@dataclass(frozen=True)
class MetaReply:
    status: int
    message: dict = field(repr=False)
    usage: dict
    request_bytes: int
    model: str | None = None      # the reply's "model" when it is a string


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: it would resend the request elsewhere. The
    3xx reaches the caller as an HTTP error."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _header_dict(headers) -> dict:
    try:
        return {str(k): str(v) for k, v in (headers or {}).items()}
    except Exception:
        return {}


def urllib_transport(url: str, headers: dict, data: bytes, timeout_s: float) -> tuple:
    """POST `data` to `url`; (status, headers, body). Non-2xx replies are
    returned, not raised. `Authorization` is an unredirected header."""
    req = urllib.request.Request(url, data=data, method="POST")
    for name, value in headers.items():
        if name.lower() == "authorization":
            req.add_unredirected_header(name, value)
        else:
            req.add_header(name, value)
    try:
        with _OPENER.open(req, timeout=timeout_s) as resp:
            return resp.status, _header_dict(resp.headers), resp.read(MAX_REPLY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(MAX_REPLY_BYTES + 1) if exc.fp is not None else b""
        except Exception:
            body = b""
        finally:
            exc.close()
        return exc.code, _header_dict(exc.headers), body


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    return isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError)


def _retry_after(headers: dict) -> float | None:
    for k, v in headers.items():
        if k.lower() == "retry-after":
            try:
                value = float(str(v).strip())
            except ValueError:
                return None
            return value if 0 <= value < 86400 else None
    return None


def _tokens(usage, name: str) -> int:
    value = usage.get(name) if isinstance(usage, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"usage {name} is not a whole number of tokens")
    return value


def _cost(usage) -> Decimal:
    prompt, completion = _tokens(usage, "prompt_tokens"), _tokens(usage, "completion_tokens")
    return (Decimal(prompt) * Decimal(str(META_PRICING.input_usd_per_mtok))
            + Decimal(completion) * Decimal(str(META_PRICING.output_usd_per_mtok))
            ) / Decimal(1_000_000)


def cost_usd(usage: dict) -> float:
    """Billed cost of one call: prompt and completion tokens at the pinned
    prices (reasoning tokens are inside completion; cached input is charged
    at the full input price, which only overstates)."""
    return float(_cost(usage))


class MetaClient:
    """One stateless `chat/completions` call per `chat`."""

    def __init__(self, key: str, *, base_url: str = BASE_URL, transport=None):
        if not _valid_key(key):
            raise MetaKeyError(KEY_FORMAT)
        if not isinstance(base_url, str) or not base_url.startswith("https://"):
            raise ValueError("base_url must be https")
        self._key = key
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._transport = transport or urllib_transport

    def __repr__(self):
        return f"MetaClient(url={self._url!r})"

    __str__ = __repr__

    def chat(self, body: dict, *, timeout_s: float) -> MetaReply:
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) \
                or not 0 < timeout_s < math.inf:
            raise ValueError("timeout_s must be a positive number of seconds")
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        bad = [k for k in FORBIDDEN_BODY_KEYS if k in body]
        if bad:
            raise ValueError(f"request body has a forbidden field: {bad[0]}")
        data = None
        try:
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            pass    # a UnicodeEncodeError holds the whole body: never re-raised
        if data is None:
            raise ValueError("request body is not JSON that encodes as UTF-8")
        headers = {"Authorization": f"Bearer {self._key}",
                   "Content-Type": "application/json", "Accept": "application/json"}
        # Every MetaCallError is raised outside an `except` block, so no
        # exception context (a reply body, a transport's text) rides along.
        failed = None
        try:
            status, reply_headers, raw = self._transport(self._url, headers, data, timeout_s)
        except Exception as exc:  # noqa: BLE001 - any failure after the send started
            failed = TIMEOUT if _is_timeout(exc) else NETWORK
        try:
            if failed:
                raise MetaCallError(failed)
            reply_headers = _header_dict(reply_headers)
            if not isinstance(status, int) or not 200 <= status < 300:
                raise MetaCallError(f"META_HTTP_{int(status) if isinstance(status, int) else 0}",
                                    retry_after=_retry_after(reply_headers))
            message, usage, model = self._parse(raw)
        except MetaCallError as exc:
            log.warning("meta chat failed: code=%s sent=%s request_bytes=%d",
                        exc.code, exc.sent, len(data))
            raise
        return MetaReply(status, message, usage, len(data), model)

    @staticmethod
    def _parse(raw) -> tuple:
        message = usage = model = None
        try:
            if isinstance(raw, (bytes, bytearray)) and len(raw) <= MAX_REPLY_BYTES:
                doc = json.loads(raw)
                message, usage = doc["choices"][0]["message"], doc["usage"]
                _cost(usage)
                model = doc.get("model")
        except Exception:  # noqa: BLE001 - the reply text never reaches the error
            message = None
        if not isinstance(message, dict) or not isinstance(usage, dict):
            raise MetaCallError(BAD_REPLY)
        return message, usage, model if isinstance(model, str) else None


# ---------------------------------------------------------------- ledger

class MetaBudgetRefused(Exception):
    """No Ask turn may be sent now; `code` is one of the META_* availability codes."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


_REASON_CODES = {"MONTH_CAP": MONTH_CAP, "TOTAL_CAP": TOTAL_CAP, "DAY_CAP": DAY_CAP}


class AskLedger:
    """Ask's spend ledger: a JevBudget with Meta prices, the Ask caps and a
    lifetime cap, in `dir_path` (`ask.sqlite3`, `ask.pin`, `HALT`)."""

    LEDGER, PIN, HALT_FILE = "ask.sqlite3", "ask.pin", "HALT"
    PURPOSE = "ask"

    def __init__(self, dir_path, budget: jb.JevBudget):
        self.dir = Path(dir_path)
        self.budget = budget

    @classmethod
    def open(cls, dir_path, *, day_cap_usd: float = 10.0, month_cap_usd: float = 60.0,
             total_cap_usd: float = 100.0, clock=time.time) -> "AskLedger":
        d = Path(dir_path)
        budget = jb.JevBudget(d / cls.LEDGER, d / cls.PIN, day_cap_usd=day_cap_usd,
                              month_cap_usd=month_cap_usd, total_cap_usd=total_cap_usd,
                              pricing=META_PRICING, clock=clock, code_prefix="META")
        # A reservation closes once (checked under the writer lock): a second
        # close, say settle_zero after mark_unknown, raises ValueError.
        budget._close_once = True
        return cls(d, budget)

    @classmethod
    def init(cls, dir_path, *, clock=time.time) -> dict:
        """First activation only (jev_budget.init refuses over any existing ledger)."""
        d = Path(dir_path)
        return jb.init(d / cls.LEDGER, d / cls.PIN, first_activation=True, clock=clock)

    @classmethod
    def recover(cls, dir_path, reason: str, *, clock=time.time) -> dict:
        """jev_budget.recover: day, month and lifetime marked reached (design 7.3)."""
        d = Path(dir_path)
        return jb.recover(d / cls.LEDGER, d / cls.PIN, reason, clock=clock)

    def halted(self) -> bool:
        return os.path.lexists(self.dir / self.HALT_FILE)

    def availability(self) -> tuple:
        """(True, None) when one turn's reservation would fit now; else
        (False, code). Reads only."""
        if self.halted():
            return False, HALT
        try:
            st = self.budget.status()
        except (OSError, sqlite3.Error):
            st = {"mode": "unreadable"}
        if st["mode"] != "normal":
            return False, RECOVERY
        b = self.budget
        bound = jb.max_charge_nusd(TURN_BODY_BYTES, TURN_OUTPUT_TOKENS, META_PRICING)
        for key, code, cap in (("month", MONTH_CAP, b.month_cap_nusd),
                               ("total", TOTAL_CAP, b.total_cap_nusd),
                               ("day", DAY_CAP, b.day_cap_nusd)):
            if cap is None or key not in st:
                continue
            p = st[key]
            # committed_usd is an exact n$ count divided by 1e9: round() restores it.
            if p["exhausted"] or round(p["committed_usd"] * 1e9) + bound > cap:
                return False, code
        return True, None

    def reserve_turn(self, turn_id: str) -> jb.Reservation:
        if not isinstance(turn_id, str) or not _TURN_ID.fullmatch(turn_id):
            raise ValueError("turn_id must be a plain id")
        if self.halted():
            raise MetaBudgetRefused(HALT)
        try:
            res = self.budget.reserve(purpose=self.PURPOSE, episode=turn_id,
                                      body_bytes=TURN_BODY_BYTES,
                                      output_tokens_max=TURN_OUTPUT_TOKENS)
        except jb.BudgetExhausted as exc:
            raise MetaBudgetRefused(_REASON_CODES.get(exc.cap, MONTH_CAP)) from None
        except (jb.JevBudgetRefused, OSError, sqlite3.Error):
            # Recovery, or a ledger or pin that cannot be written: nothing is sent.
            raise MetaBudgetRefused(RECOVERY) from None
        if self.halted():
            # HALT appeared while reserve waited on the writer lock.
            try:
                self.settle_zero(res)
            except Exception:  # noqa: BLE001 - then it stays counted at its maximum
                pass
            raise MetaBudgetRefused(HALT)
        return res

    def settle(self, reservation: jb.Reservation, usages: list) -> str | None:
        """Settle with the summed cost of every call's usage. Returns
        META_BOUND_EXCEEDED when that is above the reservation (the day, month
        and lifetime are then marked reached), else None."""
        if not usages:
            raise ValueError("settle needs the usage of every call; use settle_zero")
        total = sum((_cost(u) for u in usages), Decimal(0))
        return self.budget.settle(reservation, float(total))

    def mark_unknown(self, reservation: jb.Reservation) -> None:
        self.budget.mark_unknown(reservation)

    def settle_zero(self, reservation: jb.Reservation) -> None:
        """Cancelled before the first send: nothing was charged."""
        self.budget.settle(reservation, 0.0)
