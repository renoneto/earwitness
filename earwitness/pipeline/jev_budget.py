"""Hard spend cap for every Jev call made with the pipeline's key.

Design: docs/plans/2026-10-21-fresh-data-design.md section 10.

- The ledger (SQLite, default ~/ew-corpus-worker/jev-budget.sqlite3) is
  append-only: triggers refuse UPDATE and DELETE, and each row carries the
  digest of the row before it. A settlement is a new row naming the same
  reservation id.
- The pin file (default ~/.config/earwitness-jev-budget.pin) holds the row
  count and head digest; it is replaced atomically after every commit.
- Before each HTTP request, one BEGIN IMMEDIATE transaction sums the LA day
  and LA month (settled actuals, open reservations and unknown rows at their
  maximum) and reserves the request's maximum charge only if both stay at or
  under the caps ($1.00 a day, $15.00 a month by default).
- Every open checks the ledger against the pin, the chain and the clock. Any
  failure is recovery mode: nothing is sent until `recover` installs a new
  ledger whose first rows mark the current day and month exhausted.

Amounts are kept as integer nano-USD so cap comparisons are exact. Nothing
here sees the API key or a response body.
"""
from __future__ import annotations

import fcntl
import glob
import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_LEDGER = "~/ew-corpus-worker/jev-budget.sqlite3"
DEFAULT_PIN = "~/.config/earwitness-jev-budget.pin"
ENV_LEDGER = "EW_JEV_BUDGET_LEDGER"
ENV_PIN = "EW_JEV_BUDGET_PIN"
DAY_CAP_USD = 1.00
MONTH_CAP_USD = 15.00
MARGIN = Decimal("1.25")
PERIOD_TZ = ZoneInfo("America/Los_Angeles")
GENESIS = "0" * 64
NANO = Decimal(1_000_000_000)

AT_CAP = "JEV_AT_CAP"
RECOVERY = "JEV_BUDGET_RECOVERY"
UNPRICED = "JEV_UNPRICED"
BOUND_EXCEEDED = "JEV_BOUND_EXCEEDED"
SETTLE_FAILED = "JEV_BUDGET_SETTLE_FAILED"


@dataclass(frozen=True)
class Pricing:
    input_usd_per_mtok: float | None
    output_usd_per_mtok: float | None
    source: str
    pinned_on: str


# Carried from jev_screen's pinned constants (output tokens are free); not
# re-read from the provider when pinned.
PRICING = Pricing(0.042, 0.0, "https://docs.typesafe.ai/models", "2026-10-21")

SCHEMA = """
CREATE TABLE entries (
  seq INTEGER PRIMARY KEY,
  reservation_id TEXT NOT NULL,
  purpose TEXT NOT NULL,
  episode TEXT,
  max_nusd INTEGER NOT NULL CHECK (max_nusd >= 0),
  state TEXT NOT NULL CHECK (state IN ('reserved','settled','unknown','exhausted')),
  actual_nusd INTEGER,
  la_day TEXT NOT NULL,
  la_month TEXT NOT NULL,
  scope TEXT,
  note TEXT,
  created_at TEXT NOT NULL,
  prev_digest TEXT NOT NULL,
  digest TEXT NOT NULL);
CREATE INDEX entries_reservation ON entries (reservation_id, seq);
CREATE INDEX entries_month ON entries (la_month, reservation_id, seq);
CREATE INDEX entries_created ON entries (created_at);
CREATE TRIGGER entries_no_update BEFORE UPDATE ON entries
  BEGIN SELECT RAISE(ABORT, 'jev budget ledger is append-only'); END;
CREATE TRIGGER entries_no_delete BEFORE DELETE ON entries
  BEGIN SELECT RAISE(ABORT, 'jev budget ledger is append-only'); END;
"""
FIELDS = ("seq", "reservation_id", "purpose", "episode", "max_nusd", "state", "actual_nusd",
          "la_day", "la_month", "scope", "note", "created_at")


class BudgetError(RuntimeError):
    """An operator command cannot proceed (init over an existing ledger...)."""


class JevBudgetRefused(RuntimeError):
    """No Jev request may be sent now; `code` is the alert code. A budget
    with another `code_prefix` passes its own code (META_AT_CAP...)."""
    code = AT_CAP

    def __init__(self, message: str, reason: str | None = None, *, code: str | None = None):
        if code is not None:
            self.code = code
        super().__init__(f"{self.code}: {message}")
        self.reason = reason


class BudgetExhausted(JevBudgetRefused):
    """`cap`: the cap that refused, DAY_CAP, MONTH_CAP or TOTAL_CAP."""
    code = AT_CAP

    def __init__(self, message: str, reason: str | None = None, *, code: str | None = None,
                 cap: str | None = None):
        super().__init__(message, reason, code=code)
        self.cap = cap


class BudgetRecovery(JevBudgetRefused):
    code = RECOVERY


class NotSendable(JevBudgetRefused):
    """Unknown price or no output bound: the maximum charge is unknown."""
    code = UNPRICED


@dataclass(frozen=True)
class Reservation:
    id: str
    purpose: str
    episode: str | None
    max_nusd: int
    la_day: str
    la_month: str

    @property
    def max_usd(self) -> float:
        return self.max_nusd / 1e9


# ---------------------------------------------------------------- amounts

def _nusd(usd) -> int:
    return int((Decimal(str(usd)) * NANO).to_integral_value(rounding=ROUND_CEILING))


def max_charge_nusd(body_bytes: int, output_tokens_max, pricing: Pricing = PRICING) -> int:
    """Upper bound of one request's charge: the body's UTF-8 byte length
    (tokens never exceed bytes) times the input price, plus the output
    bound times the output price, plus 25%."""
    if pricing.input_usd_per_mtok is None or pricing.output_usd_per_mtok is None:
        raise NotSendable("no pinned Jev price; request not sent")
    if output_tokens_max is None:
        raise NotSendable("request has no output token bound; not sent")
    if int(body_bytes) < 0 or int(output_tokens_max) < 0:
        raise NotSendable("negative request size")
    usd = (Decimal(int(body_bytes)) * Decimal(str(pricing.input_usd_per_mtok))
           + Decimal(int(output_tokens_max)) * Decimal(str(pricing.output_usd_per_mtok))
           ) / Decimal(1_000_000) * MARGIN
    return int((usd * NANO).to_integral_value(rounding=ROUND_CEILING))


def max_charge_usd(body_bytes: int, output_tokens_max, pricing: Pricing = PRICING) -> float:
    return max_charge_nusd(body_bytes, output_tokens_max, pricing) / 1e9


def periods(t: float) -> tuple:
    d = datetime.fromtimestamp(t, PERIOD_TZ)
    return d.strftime("%Y-%m-%d"), d.strftime("%Y-%m")


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="microseconds")


def _digest(prev: str, row: dict) -> str:
    blob = json.dumps([prev] + [row[k] for k in FIELDS], separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


# ---------------------------------------------------------------- files

def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_pin_file(pin: Path, rows: int, head: str) -> None:
    tmp = pin.with_name(f".{pin.name}.tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps({"version": 1, "rows": rows, "head": head}))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, pin)
    _fsync_dir(pin.parent)


def _read_pin(pin: Path) -> tuple:
    if not pin.exists():
        raise BudgetRecovery("budget pin file is missing", "PIN_MISSING")
    try:
        doc = json.loads(pin.read_text(encoding="utf-8"))
        rows, head = doc["rows"], doc["head"]
        if not isinstance(rows, int) or isinstance(rows, bool) or rows < 0 \
                or not isinstance(head, str) or len(head) != 64:
            raise ValueError("bad pin")
    except (OSError, ValueError, KeyError, TypeError):
        raise BudgetRecovery("budget pin file is unreadable", "PIN_UNREADABLE") from None
    return rows, head


def _lock_path(pin: Path) -> Path:
    return pin.with_name(f".{pin.name}.lock")


@contextmanager
def _pin_lock(pin: Path, timeout_s: float | None = None):
    """The one writer lock: every ledger commit and its pin update, and
    `recover`, run under it. timeout_s None waits; else TimeoutError."""
    try:
        fh = open(_lock_path(pin), "a")
    except OSError:
        raise BudgetRecovery("budget pin file is missing", "PIN_MISSING") from None
    try:
        if timeout_s is None:
            fcntl.flock(fh, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + timeout_s
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("budget lock is held") from None
                    time.sleep(0.05)
        yield
    finally:
        fh.close()


def _create_ledger(ledger: Path) -> sqlite3.Connection:
    ledger.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ledger, isolation_level=None)
    con.executescript(SCHEMA)
    return con


# ---------------------------------------------------------------- authority

class JevBudget:
    """The budget handle a Jev transport is built with."""

    def __init__(self, ledger=DEFAULT_LEDGER, pin=DEFAULT_PIN, *,
                 day_cap_usd: float = DAY_CAP_USD, month_cap_usd: float = MONTH_CAP_USD,
                 pricing: Pricing = PRICING, clock=time.time, busy_timeout_s: float = 60.0,
                 code_prefix: str = "JEV", total_cap_usd: float | None = None):
        """code_prefix: refusal codes read `<prefix>_AT_CAP`... (default JEV).
        total_cap_usd: optional lifetime cap, settled plus open amounts since
        the ledger's first row; None (default) is no lifetime cap."""
        self.ledger = Path(ledger).expanduser()
        self.pin = Path(pin).expanduser()
        self.day_cap_nusd, self.month_cap_nusd = _nusd(day_cap_usd), _nusd(month_cap_usd)
        self.total_cap_nusd = None if total_cap_usd is None else _nusd(total_cap_usd)
        self.code_prefix = str(code_prefix)
        self.pricing, self.clock, self.busy_timeout_s = pricing, clock, busy_timeout_s
        self._verified = None   # (ledger file identity, rows, head digest) checked here
        # Set by meta_client.AskLedger only: a reservation is closed once,
        # checked inside the writer transaction. Off for Jev (unchanged).
        self._close_once = False

    def _code(self, code: str) -> str:
        """JEV_AT_CAP -> <prefix>_AT_CAP."""
        return self.code_prefix + code[len("JEV"):]

    def _recode(self, exc: JevBudgetRefused) -> None:
        if self.code_prefix != "JEV":
            exc.code = self._code(type(exc).code)
            exc.args = (f"{exc.code}: {str(exc).partition(': ')[2]}",)

    def _require_open(self, con, res: Reservation) -> None:
        if not self._close_once:
            return
        # The latest row must be the reservation itself, as the caller names
        # it: a rebuilt Reservation with another maximum or period is refused.
        row = con.execute("SELECT state, purpose, max_nusd, la_day, la_month FROM entries"
                          " WHERE reservation_id=? ORDER BY seq DESC LIMIT 1",
                          (res.id,)).fetchone()
        if row is None or tuple(row) != ("reserved", res.purpose, res.max_nusd, res.la_day,
                                         res.la_month):
            raise ValueError("reservation is not open")

    @classmethod
    def from_env(cls, env=None, **kw) -> "JevBudget":
        env = os.environ if env is None else env
        return cls(env.get(ENV_LEDGER) or DEFAULT_LEDGER, env.get(ENV_PIN) or DEFAULT_PIN, **kw)

    def __repr__(self):
        return f"JevBudget(ledger={str(self.ledger)!r}, pin={str(self.pin)!r})"

    # -- open and check
    def _connect(self) -> sqlite3.Connection:
        if not self.ledger.is_file():
            raise BudgetRecovery("budget ledger is missing", "LEDGER_MISSING")
        try:
            con = sqlite3.connect(self.ledger.resolve().as_uri() + "?mode=rw", uri=True,
                                  timeout=self.busy_timeout_s, isolation_level=None)
            con.execute("SELECT 1 FROM entries LIMIT 1").fetchall()
        except sqlite3.Error:
            raise BudgetRecovery("budget ledger is unreadable", "LEDGER_UNREADABLE") from None
        return con

    def _check(self, con, now: float | None) -> tuple:
        """(row count, head digest) after the open checks; BudgetRecovery
        otherwise. The chain is verified in full once per handle and ledger
        file, then from the last verified row on (rows are append-only, so a
        verified prefix whose head digest is unchanged stays verified)."""
        pin_rows, pin_head = _read_pin(self.pin)
        try:
            st = os.stat(self.ledger)
            ident = (st.st_dev, st.st_ino)
            n, prev = 0, GENESIS
            if self._verified and self._verified[0] == ident:
                _, vn, vd = self._verified
                row = con.execute("SELECT digest FROM entries WHERE seq=?", (vn,)).fetchone()
                if vn == 0 or (row is not None and row[0] == vd):
                    n, prev = vn, vd
            for r in con.execute("SELECT %s, prev_digest, digest FROM entries WHERE seq > ?"
                                 " ORDER BY seq" % ",".join(FIELDS), (n,)):
                row = dict(zip(FIELDS, r[:len(FIELDS)]))
                n += 1
                if row["seq"] != n or r[-2] != prev or _digest(prev, row) != r[-1]:
                    self._verified = None
                    raise BudgetRecovery("budget ledger chain is broken", "CHAIN_BROKEN")
                prev = r[-1]
            at_pin = GENESIS if pin_rows == 0 else (con.execute(
                "SELECT digest FROM entries WHERE seq=?", (pin_rows,)).fetchone() or [None])[0]
            latest = con.execute("SELECT MAX(created_at) FROM entries").fetchone()[0]
        except (OSError, sqlite3.Error):
            raise BudgetRecovery("budget ledger is unreadable", "LEDGER_UNREADABLE") from None
        self._verified = (ident, n, prev)
        if n < pin_rows:
            raise BudgetRecovery("budget ledger is older than its pin", "LEDGER_BEHIND_PIN")
        if at_pin != pin_head:
            raise BudgetRecovery("budget ledger does not match its pin", "PIN_MISMATCH")
        if now is not None and latest is not None and latest > _iso(now):
            raise BudgetRecovery("budget ledger has rows dated after the clock", "FUTURE_ROW")
        return n, prev

    def _totals(self, con, day: str, month: str) -> dict:
        out = {k: {"nusd": 0, "reserved": 0, "settled": 0, "unknown": 0, "exhausted": False}
               for k in ("day", "month")}
        purposes: dict = {}
        for state, max_n, actual, la_day, purpose, scope in con.execute(
                "SELECT e.state, e.max_nusd, e.actual_nusd, e.la_day, e.purpose, e.scope"
                " FROM entries e WHERE e.seq IN (SELECT MAX(seq) FROM entries WHERE la_month=?"
                " GROUP BY reservation_id)", (month,)):
            periods_hit = ["month"] + (["day"] if la_day == day else [])
            if state == "exhausted":
                if scope == "month":
                    out["month"]["exhausted"] = out["day"]["exhausted"] = True
                elif scope == "day" and la_day == day:
                    out["day"]["exhausted"] = True
                continue
            amount = actual if state == "settled" else max_n
            purposes[purpose] = purposes.get(purpose, 0) + 1
            for p in periods_hit:
                out[p]["nusd"] += amount
                out[p][state] += 1
        out["by_purpose"] = purposes
        if self.total_cap_nusd is not None:
            out["total"] = self._lifetime(con)
        return out

    @staticmethod
    def _lifetime(con) -> dict:
        """Settled actuals plus open maxima over every row of the ledger, and
        whether a lifetime mark (scope `total`) was written."""
        t = {"nusd": 0, "reserved": 0, "settled": 0, "unknown": 0, "exhausted": False}
        for state, max_n, actual, scope in con.execute(
                "SELECT e.state, e.max_nusd, e.actual_nusd, e.scope FROM entries e"
                " WHERE e.seq IN (SELECT MAX(seq) FROM entries GROUP BY reservation_id)"):
            if state == "exhausted":
                t["exhausted"] = t["exhausted"] or scope == "total"
                continue
            t["nusd"] += actual if state == "settled" else max_n
            t[state] += 1
        return t

    def _append(self, con, head: tuple, **row) -> tuple:
        n, prev = head
        row = {"seq": n + 1, "episode": None, "actual_nusd": None, "scope": None,
               "note": None, **row}
        digest = _digest(prev, row)
        con.execute("INSERT INTO entries (%s, prev_digest, digest) VALUES (%s)"
                    % (",".join(FIELDS), ",".join("?" * (len(FIELDS) + 2))),
                    [row[k] for k in FIELDS] + [prev, digest])
        return n + 1, digest

    def _commit_rows(self, decide) -> tuple:
        """Append the rows `decide(con, day, month)` returns, in one
        BEGIN IMMEDIATE transaction under the writer lock, then pin the new
        head. The clock is read only once the lock is held, so no other
        writer can commit a later created_at before this one checks it."""
        try:
            return self._commit_rows_locked(decide)
        except JevBudgetRefused as exc:
            self._recode(exc)
            raise

    def _commit_rows_locked(self, decide) -> tuple:
        with _pin_lock(self.pin):
            con = self._connect()
            try:
                self._check(con, None)   # verify the bulk of the chain first
                con.execute("BEGIN IMMEDIATE")
                try:
                    now = self.clock()
                    head = self._check(con, now)
                    day, month = periods(now)
                    for row in decide(con, day, month):
                        head = self._append(con, head, created_at=_iso(now), **row)
                    con.execute("COMMIT")
                except BaseException:
                    con.execute("ROLLBACK")
                    raise
            finally:
                con.close()
            _write_pin_file(self.pin, *head)
        return head

    # -- requests
    def reserve(self, *, purpose: str, episode, body_bytes: int,
                output_tokens_max) -> Reservation:
        """Reserve one request's maximum charge, or raise JevBudgetRefused
        (nothing may be sent)."""
        try:
            max_n = max_charge_nusd(body_bytes, output_tokens_max, self.pricing)
        except JevBudgetRefused as exc:
            self._recode(exc)
            raise
        rid, got = uuid.uuid4().hex, {}

        def decide(con, day, month):
            t = self._totals(con, day, month)
            # One refusal reason, month first, then lifetime, then day.
            if t["month"]["exhausted"]:
                raise BudgetExhausted(f"Jev budget for {day} marked exhausted", cap="MONTH_CAP")
            if t["month"]["nusd"] + max_n > self.month_cap_nusd:
                raise BudgetExhausted(f"Jev month cap reached for {month}", cap="MONTH_CAP")
            if self.total_cap_nusd is not None:
                if t["total"]["exhausted"]:
                    raise BudgetExhausted("lifetime cap marked reached", cap="TOTAL_CAP")
                if t["total"]["nusd"] + max_n > self.total_cap_nusd:
                    raise BudgetExhausted("lifetime cap reached", cap="TOTAL_CAP")
            if t["day"]["exhausted"]:
                raise BudgetExhausted(f"Jev budget for {day} marked exhausted", cap="DAY_CAP")
            if t["day"]["nusd"] + max_n > self.day_cap_nusd:
                raise BudgetExhausted(f"Jev day cap reached for {day}", cap="DAY_CAP")
            got.update(day=day, month=month)
            return [{"reservation_id": rid, "purpose": str(purpose),
                     "episode": None if episode is None else str(episode),
                     "max_nusd": max_n, "state": "reserved", "la_day": day,
                     "la_month": month}]
        self._commit_rows(decide)
        return Reservation(rid, str(purpose), episode, max_n, got["day"], got["month"])

    def _closing_row(self, res: Reservation, state: str, actual_nusd=None) -> dict:
        return {"reservation_id": res.id, "purpose": res.purpose,
                "episode": None if res.episode is None else str(res.episode),
                "max_nusd": res.max_nusd, "state": state, "actual_nusd": actual_nusd,
                "la_day": res.la_day, "la_month": res.la_month}

    def settle(self, res: Reservation, actual_usd: float) -> str | None:
        """A known charge (from the response's usage) replaces the maximum.
        A charge above the reserved maximum is recorded as it is, and the
        reservation's LA day and month, and the settle-time day and month
        when different, are marked exhausted (a wrong price or bound must not
        repeat), and with a lifetime cap the lifetime too; returns
        <prefix>_BOUND_EXCEEDED then (the caller alerts), else None."""
        actual_n = _nusd(max(0.0, float(actual_usd)))
        over = actual_n > res.max_nusd

        def decide(con, day, month):
            self._require_open(con, res)
            rows = [self._closing_row(res, "settled", actual_n)]
            if not over:
                return rows
            marks = [(res.la_day, res.la_month)]
            if (day, month) != marks[0]:      # settled in a later day or month: close it too
                marks.append((day, month))
            for la_day, la_month in marks:
                for scope in ("day", "month"):
                    rows.append({"reservation_id": uuid.uuid4().hex, "purpose": "bound",
                                 "max_nusd": 0, "state": "exhausted", "la_day": la_day,
                                 "la_month": la_month, "scope": scope,
                                 "note": f"{BOUND_EXCEEDED}: reservation {res.id} charged "
                                         f"{actual_n} n$ over its maximum {res.max_nusd} n$"})
            if self.total_cap_nusd is not None:
                rows.append(_total_mark(day, month, f"{BOUND_EXCEEDED}: reservation {res.id}"))
            return rows
        self._commit_rows(decide)
        return self._code(BOUND_EXCEEDED) if over else None

    def mark_unknown(self, res: Reservation) -> None:
        """Sent, outcome unknown (timeout, lost response, error): the
        maximum stays counted for the period."""
        def decide(con, day, month):
            self._require_open(con, res)
            return [self._closing_row(res, "unknown")]
        self._commit_rows(decide)

    # -- reporting
    def status(self) -> dict:
        """Mode and day/month totals; codes and amounts only."""
        out = {"ledger": str(self.ledger), "pin": str(self.pin),
               "caps_usd": {"day": self.day_cap_nusd / 1e9, "month": self.month_cap_nusd / 1e9},
               "pricing": {"input_usd_per_mtok": self.pricing.input_usd_per_mtok,
                           "output_usd_per_mtok": self.pricing.output_usd_per_mtok,
                           "source": self.pricing.source, "pinned_on": self.pricing.pinned_on},
               "period_tz": str(PERIOD_TZ)}
        try:
            con = self._connect()
            try:
                con.execute("BEGIN")
                con.execute("SELECT COUNT(*) FROM entries").fetchone()  # snapshot first,
                now = self.clock()                                       # then the clock
                day, month = periods(now)
                rows, _ = self._check(con, now)
                t = self._totals(con, day, month)
            finally:
                con.close()
        except BudgetRecovery as exc:
            return {"mode": "recovery", "code": self._code(type(exc).code), "reason": exc.reason,
                    **out}
        periods_out = {}
        for k, label in (("day", day), ("month", month)):
            v = t[k]
            periods_out[k] = {"period": label, "committed_usd": v["nusd"] / 1e9,
                              "settled": v["settled"], "reserved": v["reserved"],
                              "unknown": v["unknown"], "exhausted": v["exhausted"]}
        if self.total_cap_nusd is not None:
            v = t["total"]
            out["caps_usd"]["total"] = self.total_cap_nusd / 1e9
            periods_out["total"] = {"committed_usd": v["nusd"] / 1e9, "settled": v["settled"],
                                    "reserved": v["reserved"], "unknown": v["unknown"],
                                    "exhausted": v["exhausted"]}
        return {"mode": "normal", "rows": rows, **out, **periods_out,
                "by_purpose": t["by_purpose"]}


# ---------------------------------------------------------------- operator commands

def init(ledger=DEFAULT_LEDGER, pin=DEFAULT_PIN, *, first_activation: bool = False,
         clock=time.time) -> dict:
    """A fresh ledger and pin, only at first activation (design 10.4) and
    only when neither exists nor was ever replaced."""
    if not first_activation:
        raise BudgetError("init creates the ledger only at first activation; pass "
                          "--first-activation (after a loss or restore use `jev-budget recover`)")
    ledger, pin = Path(ledger).expanduser(), Path(pin).expanduser()
    for p in (ledger, pin):
        if p.exists():
            raise BudgetError(f"{p} exists; init only creates a first ledger "
                              "(after a loss or restore use `jev-budget recover`)")
        if p.parent.is_dir() and any(p.parent.glob(glob.escape(p.name) + ".replaced-*")):
            raise BudgetError(f"replaced copies of {p} exist, so this is not a first "
                              "activation; use `jev-budget recover`")
    con = _create_ledger(ledger)
    con.close()
    pin.parent.mkdir(parents=True, exist_ok=True)
    _write_pin_file(pin, 0, GENESIS)
    return {"ledger": str(ledger), "pin": str(pin), "rows": 0}


def _total_mark(day: str, month: str, note: str) -> dict:
    """The lifetime-cap mark: only a budget with total_cap_usd reads it."""
    return {"reservation_id": uuid.uuid4().hex, "purpose": "total", "max_nusd": 0,
            "state": "exhausted", "la_day": day, "la_month": month, "scope": "total",
            "note": note[:500]}


def recover(ledger=DEFAULT_LEDGER, pin=DEFAULT_PIN, reason: str = "", *,
            clock=time.time, lock_timeout_s: float = 10.0) -> dict:
    """Section 10.3: move the old ledger and pin aside (never deleted) and
    start a new ledger whose first rows mark the current LA day and month
    exhausted, so nothing is sent until the next month. Runs under the
    writer lock; refuses when a worker holds it (stop the jev workers
    first). A SQLite journal beside the old ledger is moved with it and
    listed in `journals_moved`. It also marks the lifetime cap reached, read
    only by a budget with total_cap_usd (Jev has none, so Jev is unchanged)."""
    reason = (reason or "").strip()
    if not reason:
        raise BudgetError("recover needs --reason")
    ledger, pin = Path(ledger).expanduser(), Path(pin).expanduser()
    pin.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _pin_lock(pin, lock_timeout_s):
            return _recover_locked(ledger, pin, reason, clock)
    except TimeoutError:
        raise BudgetError("the Jev budget lock is held by a running process: stop the jev "
                          "workers (and jev-backfill / rename) before recover") from None


def _recover_locked(ledger: Path, pin: Path, reason: str, clock) -> dict:
    now = clock()
    day, month = periods(now)
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    moved, journals = {}, []
    for key, p in (("ledger", ledger), ("pin", pin)):
        moved[key] = None
        for side in (p, *(p.with_name(p.name + s) for s in ("-journal", "-wal", "-shm"))):
            if side.exists():
                dest = side.with_name(f"{side.name}.replaced-{stamp}")
                os.rename(side, dest)
                if side == p:
                    moved[key] = str(dest)
                else:
                    journals.append(str(dest))
    con = _create_ledger(ledger)
    head = (0, GENESIS)
    try:
        con.execute("BEGIN IMMEDIATE")
        auth = JevBudget(ledger, pin, clock=clock)
        for scope in ("day", "month"):
            head = auth._append(con, head, reservation_id=uuid.uuid4().hex, purpose="recover",
                                max_nusd=0, state="exhausted", la_day=day, la_month=month,
                                scope=scope, note=f"recover: {reason}"[:500],
                                created_at=_iso(now))
        head = auth._append(con, head, created_at=_iso(now),
                            **_total_mark(day, month, f"recover: {reason}"))
        con.execute("COMMIT")
    finally:
        con.close()
    _write_pin_file(pin, *head)
    return {"ledger": str(ledger), "pin": str(pin), "rows": head[0], "moved_aside": moved,
            "journals_moved": journals, "exhausted": {"day": day, "month": month}}
