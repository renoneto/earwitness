"""Purchase seats in the control store (purchase design 5.1, BY2).

One row per Stripe subscription. A seat ties a buyer's principal P to one
pool workspace of their plan's offer (`ew-a` or `ew-b`) for life:

- `UNIQUE (workspace_id)` over all rows, ended and retired ones included:
  a workspace is held once for life. SQLite lets several NULLs through,
  so refused rows (no workspace) never collide.
- `UNIQUE (principal) WHERE workspace_id IS NOT NULL`: one
  workspace-holding seat per principal for life. Refused rows are
  separate rows with a NULL workspace.
- `state` is one of STATES (a CHECK). A refused row holds no workspace;
  every other row holds one, except a refused row that later ended.

States change only through the window commands (BY3); this module gives
them the table, the read helpers the web process needs (the webhook and
admission) and a small write API that enforces the transitions of 5.1:

    reserved -> recorded -> bound -> active -> ended -> retired
    reserved | recorded | bound | awaiting_account -> needs_refund -> ended
    refused -> ended
    bound -> awaiting_account -> active    (password fallback, 9.5)

`awaiting_account` admits no one, so a fallback seat whose subscription
ends is refunded like the first three (design 5.1).

Only `active` admits (admission.py). The webhook takes the workspace path
only for WEBHOOK_STATES. P is derived from the canonical email by the
password sign-in rule (local_accounts.principal); it is never computed
another way. The table is created with the control store (connections.GrantsStore calls
create_tables), so its schema is pinned from the start; every read still
treats a missing table (an older store) as "no seat".
"""
from __future__ import annotations

import datetime as _dt
import os
import sqlite3

OFFERS = ("ew-a", "ew-b")
STATES = ("reserved", "recorded", "bound", "active", "awaiting_account",
          "needs_refund", "refused", "ended", "retired")
ADMITTING_STATES = frozenset({"active"})
# needs_refund is not a webhook state (BY3 r9): every move into it denies
# the workspace first, and a webhook must never re-allow it.
WEBHOOK_STATES = frozenset({"bound", "active", "awaiting_account"})
TRANSITIONS = {
    "reserved": frozenset({"recorded", "needs_refund"}),
    "recorded": frozenset({"bound", "needs_refund"}),
    "bound": frozenset({"active", "awaiting_account", "needs_refund"}),
    "awaiting_account": frozenset({"active", "needs_refund"}),
    "active": frozenset({"ended"}),
    "needs_refund": frozenset({"ended"}),
    "refused": frozenset({"ended"}),
    "ended": frozenset({"retired"}),
    "retired": frozenset(),
}

_STATE_LIST = ",".join(f"'{s}'" for s in STATES)
_OFFER_LIST = ",".join(f"'{o}'" for o in OFFERS)
# Two statements run one by one with execute(): executescript() would
# commit a caller's open transaction first (Astra BY2-R1-01).
DDL = (f"""
CREATE TABLE IF NOT EXISTS purchase_seats (
  subscription_id TEXT PRIMARY KEY,
  offer TEXT NOT NULL CHECK (offer IN ({_OFFER_LIST})),
  customer_id TEXT NOT NULL,
  principal TEXT NOT NULL,
  email TEXT NOT NULL,
  workspace_id TEXT UNIQUE,
  state TEXT NOT NULL CHECK (state IN ({_STATE_LIST})),
  run_key TEXT NOT NULL,
  state_at TEXT NOT NULL,
  last_error TEXT NOT NULL DEFAULT '',
  credential_delivered_at TEXT,
  CHECK (state <> 'refused' OR workspace_id IS NULL),
  CHECK (workspace_id IS NOT NULL OR state IN ('refused', 'ended')))""",
       """
CREATE UNIQUE INDEX IF NOT EXISTS purchase_seats_one_per_principal
  ON purchase_seats (principal) WHERE workspace_id IS NOT NULL""")


class SeatError(ValueError):
    """A seat write the rules of 5.1 refuse."""


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def ensure_schema(con: sqlite3.Connection) -> None:
    """Create the table inside whatever transaction the caller holds; it
    never commits or ends that transaction."""
    for statement in DDL:
        con.execute(statement)


def _missing_table(exc: sqlite3.OperationalError) -> bool:
    return "no such table" in str(exc)


def _one(con, sql: str, args: tuple):
    try:
        cur = con.execute(sql, args)
        row = cur.fetchone()
    except sqlite3.OperationalError as exc:
        if _missing_table(exc):
            return None
        raise
    if row is None:
        return None
    return dict(zip([d[0] for d in cur.description], tuple(row)))


def seat(con, subscription_id: str) -> dict | None:
    """The seat row of one subscription, or None. sqlite3.Error other than
    a missing table propagates (a transient fault is not "no seat")."""
    return _one(con, "SELECT * FROM purchase_seats WHERE subscription_id=?",
                (subscription_id,))


def seat_for_workspace(con, workspace_id: str) -> dict | None:
    return _one(con, "SELECT * FROM purchase_seats WHERE workspace_id=?",
                (workspace_id,))


def holding_seat(con, principal: str) -> dict | None:
    """The one workspace-holding seat of a principal (any state), or None."""
    return _one(con, "SELECT * FROM purchase_seats WHERE principal=?"
                " AND workspace_id IS NOT NULL", (principal,))


def principal_for(issuer: str, email: str) -> str:
    """P from the canonical email under the password sign-in rule."""
    from . import local_accounts as _la
    return _la.principal(issuer, email)


def _require_local_issuer(issuer: str, public_base_url: str | None = None) -> None:
    """Seats are written for the local password issuer only: the origin of
    EARWITNESS_PUBLIC_BASE_URL (local_accounts.local_issuer), the source
    the password sign-in route and app_accounts use. A seat for any other
    issuer could be matched by that issuer's identity, so it is refused.
    The window passes `public_base_url` from the app's own settings
    (asgi.config_from_env, BY2 review r2 N2); None reads the environment."""
    from . import local_accounts as _la
    if public_base_url is None:
        public_base_url = os.environ.get("EARWITNESS_PUBLIC_BASE_URL", "")
    try:
        local = _la.local_issuer(public_base_url)
    except ValueError as exc:
        raise SeatError("no local sign-in issuer is configured") from exc
    if issuer != local:
        raise SeatError("a seat is written only for the local sign-in issuer")


def reserve(con, *, subscription_id: str, offer: str, customer_id: str,
            email: str, issuer: str, workspace_id: str, run_key: str,
            now: str | None = None, public_base_url: str | None = None) -> dict:
    """Insert a `reserved` seat holding `workspace_id`. E is stored as its
    canonical spelling and P is derived from it. A second workspace-holding
    seat for P, or a workspace already held, raises SeatError and writes
    nothing (the two UNIQUE rules). `issuer` must be the local sign-in
    issuer. The caller owns the transaction; this never commits it."""
    from . import local_accounts as _la
    if offer not in OFFERS:
        raise SeatError("unknown offer")
    _require_local_issuer(issuer, public_base_url)
    if not workspace_id:
        raise SeatError("a reserved seat holds a workspace")
    try:
        key, _display = _la.canonical(email)
    except _la.EmailError as exc:
        raise SeatError("the buyer email fails the email rule") from exc
    _require_paid_capable(con, workspace_id)
    return _insert(con, subscription_id, offer, customer_id,
                   _la.principal(issuer, key), key, workspace_id, "reserved",
                   run_key, now)


def _require_paid_capable(con, workspace_id: str) -> None:
    """RB1: a seat never holds a workspace whose rights record does not
    allow paid use (rights_admission.paid_use: evaluation-only, edited
    after approval, or unreadable). A workspace with no record, or not
    registered (so no record can name it), is allowed. Reads only, in the
    caller's transaction; a refusal writes nothing."""
    from . import rights_admission as _rights
    try:
        row = con.execute("SELECT db_identity FROM workspace_registry"
                          " WHERE workspace_id=?", (workspace_id,)).fetchone()
    except sqlite3.Error as exc:
        raise SeatError("workspace not paid-capable (registry unreadable)") from exc
    if row is None:
        return
    allowed, reason = _rights.paid_use(con, row[0])
    if not allowed:
        raise SeatError(f"workspace not paid-capable ({reason})")


def refuse(con, *, subscription_id: str, offer: str, customer_id: str,
           email: str, issuer: str, run_key: str, reason: str,
           now: str | None = None, public_base_url: str | None = None) -> dict:
    """Insert a `refused` seat with no workspace. P is derived when the
    email passes the rule, else left empty. `issuer` must be the local
    sign-in issuer. The caller owns the transaction."""
    from . import local_accounts as _la
    if offer not in OFFERS:
        raise SeatError("unknown offer")
    _require_local_issuer(issuer, public_base_url)
    try:
        key, _display = _la.canonical(email)
        principal = _la.principal(issuer, key)
    except _la.EmailError:
        key, principal = str(email or ""), ""
    return _insert(con, subscription_id, offer, customer_id, principal, key,
                   None, "refused", run_key, now, last_error=reason)


def _insert(con, subscription_id, offer, customer_id, principal, email,
            workspace_id, state, run_key, now, *, last_error="") -> dict:
    ensure_schema(con)
    try:
        con.execute(
            "INSERT INTO purchase_seats (subscription_id, offer, customer_id,"
            " principal, email, workspace_id, state, run_key, state_at,"
            " last_error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (subscription_id, offer, customer_id, principal, email,
             workspace_id, state, run_key, now or _now_iso(), last_error))
    except sqlite3.IntegrityError as exc:
        raise SeatError(f"seat refused by a table rule: {exc}") from exc
    return seat(con, subscription_id)


def transition(con, subscription_id: str, to_state: str, *, run_key: str,
               last_error: str = "", now: str | None = None) -> dict:
    """Move one seat along an allowed edge of 5.1; SeatError otherwise.
    The caller owns the transaction."""
    if to_state not in STATES:
        raise SeatError("unknown seat state")
    row = seat(con, subscription_id)
    if row is None:
        raise SeatError("no such seat")
    if to_state not in TRANSITIONS[row["state"]]:
        raise SeatError(f"seat cannot go from {row['state']} to {to_state}")
    if to_state == "retired" and row["workspace_id"] is None:
        raise SeatError("only a workspace-holding seat is retired")
    con.execute("UPDATE purchase_seats SET state=?, run_key=?, state_at=?,"
                " last_error=? WHERE subscription_id=? AND state=?",
                (to_state, run_key, now or _now_iso(), last_error,
                 subscription_id, row["state"]))
    return seat(con, subscription_id)


# -- The purchase tables of the control store (BY3; design 4.6, 5.3, 9.5, 13)
#
# connections.GrantsStore creates them with every control store, the way it
# creates the rights table, so MANIFEST.json pins one control schema and no
# window or web write ever changes it (preserve compares schemas). The
# window only checks that they exist; it never creates them.

CANCEL_REQUESTS_DDL = """
CREATE TABLE IF NOT EXISTS purchase_cancel_requests (
  subscription_id TEXT PRIMARY KEY,
  target_end INTEGER NOT NULL CHECK (target_end > 0),
  requested_at TEXT NOT NULL,
  done_at TEXT,
  outcome TEXT NOT NULL DEFAULT ''
    CHECK (outcome IN ('', 'applied', 'late', 'terminal')),
  CHECK ((done_at IS NULL) = (outcome = '')))"""

JOURNAL_DDL = """
CREATE TABLE IF NOT EXISTS purchase_journal (
  id INTEGER PRIMARY KEY,
  run_key TEXT NOT NULL,
  subscription_id TEXT NOT NULL,
  step TEXT NOT NULL,
  outcome TEXT NOT NULL,
  committed_at TEXT NOT NULL)"""

JOURNAL_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS purchase_journal_run ON purchase_journal (run_key)"""

CREDENTIAL_COLUMN = "credential_delivered_at"
WINDOW_TABLES = ("purchase_seats", "purchase_cancel_requests", "purchase_journal",
                 "billing_deliveries")


def missing_tables(con) -> list:
    """The purchase tables (and the seat column) a control store lacks."""
    names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    missing = [t for t in WINDOW_TABLES if t not in names]
    if "purchase_seats" in names:
        cols = {r[1] for r in con.execute("PRAGMA table_info(purchase_seats)")}
        if CREDENTIAL_COLUMN not in cols:
            missing.append(f"purchase_seats.{CREDENTIAL_COLUMN}")
    return missing


def create_tables(con) -> None:
    """Create every purchase table in the caller's transaction (never
    commits). Called by connections.GrantsStore for every control store."""
    from . import billing as _billing
    ensure_schema(con)
    con.execute(CANCEL_REQUESTS_DDL)
    con.execute(JOURNAL_DDL)
    con.execute(JOURNAL_INDEX_DDL)
    con.execute(_billing._DELIVERIES_DDL)



def _rows(con, sql: str, args: tuple = ()) -> list:
    try:
        cur = con.execute(sql, args)
        rows = cur.fetchall()
    except sqlite3.OperationalError as exc:
        if _missing_table(exc):
            return []
        raise
    names = [d[0] for d in cur.description]
    return [dict(zip(names, tuple(r))) for r in rows]


def all_seats(con) -> list:
    """Every seat row, oldest first (a missing table is no seat)."""
    return _rows(con, "SELECT * FROM purchase_seats ORDER BY state_at, subscription_id")


def journal(con, run_key: str, subscription_id: str, step: str, outcome: str,
            now: str | None = None) -> None:
    """One committed-write evidence row (design 5.3 restart safety). The
    caller writes it in the same transaction as the write it describes."""
    con.execute("INSERT INTO purchase_journal (run_key, subscription_id, step,"
                " outcome, committed_at) VALUES (?, ?, ?, ?, ?)",
                (run_key, subscription_id or "", step, outcome, now or _now_iso()))


def journal_rows(con, run_key: str) -> list:
    return _rows(con, "SELECT * FROM purchase_journal WHERE run_key=? ORDER BY id",
                 (run_key,))


def last_sync_ok(con) -> str | None:
    """committed_at of the latest fully successful daily reconciliation."""
    rows = _rows(con, "SELECT MAX(committed_at) AS at FROM purchase_journal"
                      " WHERE step='sync' AND outcome='ok'")
    return rows[0]["at"] if rows else None


def set_last_error(con, subscription_id: str, last_error: str, *, run_key: str,
                   now: str | None = None) -> None:
    """Note why a row stopped, without changing its state."""
    con.execute("UPDATE purchase_seats SET last_error=?, run_key=?, state_at=?"
                " WHERE subscription_id=?",
                (last_error[:200], run_key, now or _now_iso(), subscription_id))


# -- Cancel requests (design 4.6) -------------------------------------------

def cancel_request(con, subscription_id: str) -> dict | None:
    return _one(con, "SELECT * FROM purchase_cancel_requests WHERE subscription_id=?",
                (subscription_id,))


def queue_cancel_request(con, subscription_id: str, target_end: int,
                         now: str | None = None) -> dict:
    """Store the request, keeping the earliest target_end. A request that
    was already applied or found late stays as it is. Never creates a
    table: a store without it is refused (SeatError)."""
    if "purchase_cancel_requests" in missing_tables(con):
        raise SeatError("the control store has no purchase tables; re-pin as DEPLOY")
    con.execute("INSERT INTO purchase_cancel_requests (subscription_id, target_end,"
                " requested_at) VALUES (?, ?, ?) ON CONFLICT(subscription_id) DO UPDATE"
                " SET target_end=MIN(target_end, excluded.target_end)"
                " WHERE done_at IS NULL",
                (subscription_id, int(target_end), now or _now_iso()))
    return cancel_request(con, subscription_id)


def close_cancel_request(con, subscription_id: str, outcome: str,
                         now: str | None = None) -> None:
    con.execute("UPDATE purchase_cancel_requests SET done_at=?, outcome=?"
                " WHERE subscription_id=? AND done_at IS NULL",
                (now or _now_iso(), outcome, subscription_id))


def set_cancel_outcome(con, subscription_id: str, outcome: str,
                       now: str | None = None) -> None:
    """Correct a closed request's outcome (a request wrongly closed as
    terminal while its subscription still lives, BY3 r4)."""
    con.execute("UPDATE purchase_cancel_requests SET done_at=?, outcome=?"
                " WHERE subscription_id=?", (now or _now_iso(), outcome, subscription_id))


def queued_cancel_requests(con) -> list:
    return _rows(con, "SELECT * FROM purchase_cancel_requests WHERE done_at IS NULL"
                      " ORDER BY subscription_id")


def late_cancel_requests(con) -> list:
    return _rows(con, "SELECT * FROM purchase_cancel_requests WHERE outcome='late'"
                      " ORDER BY subscription_id")


# -- Password fallback (design 9.5) -----------------------------------------

def fallback_reset_allowed(row: dict | None) -> bool:
    """PW's checked reset may run for a purchase seat only while its
    credential is not delivered: `awaiting_account`, or `active` with no
    `credential_delivered_at`. Once delivered, the branch is closed and a
    reset follows PW runbook AC like any account."""
    if not row or not row.get("workspace_id"):
        return False
    if row.get("state") == "awaiting_account":
        return True
    return row.get("state") == "active" and not row.get(CREDENTIAL_COLUMN)


def mark_delivered(con, subscription_id: str, now: str | None = None) -> bool:
    """Write only credential_delivered_at (online). False when the seat is
    not an undelivered active seat."""
    cur = con.execute(f"UPDATE purchase_seats SET {CREDENTIAL_COLUMN}=?"
                      f" WHERE subscription_id=? AND state='active'"
                      f" AND {CREDENTIAL_COLUMN} IS NULL",
                      (now or _now_iso(), subscription_id))
    return cur.rowcount == 1
