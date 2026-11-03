"""P11 scheduled monitoring and notifications (disjoint leaf).

Extends current watchlists/campaigns; never replaces them. No live
schedules are activated by this module: it computes UTC due times,
coalesced catch-up, baselines, match transitions, and a transactional
outbox with delivery reconciliation. All tables beyond the frozen P00
v4 schema live in aux tables created here; db.py is untouched.

Contracts honored:
- Discovery default 6h + per-source deterministic jitter; resampling
  DISABLED unless explicitly selected.
- UTC next_due_at; a missed schedule coalesces to ONE catch-up run.
- Pause on policy/budget failure shows last successful coverage.
- Watchlist baseline on monitor enable (no corpus flood); backfill is a
  separate explicit action; definition edits -> new revision/baseline.
- Source corrections are not new-placement events; unresolved
  cross-snapshot correspondence yields at most a labeled
  new_snapshot_observation event, never a new-placement claim.
- Outbox entries commit with the match transition under one unique key
  (detect_and_queue does both in a single transaction).
- Provider idempotency where available, else at-least-once with
  documented duplicate risk (never exactly-once). Provider message IDs
  recorded; uncertain responses reconciled before resend.
- In-app + ONE explicitly selected email provider; daily digest default,
  immediate opt-in; NO webhooks. Membership/preferences/rights/
  availability rechecked at send time; email carries minimal metadata +
  authenticated link (no transcript content by default); unsubscribe
  stops delivery.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from . import db as _db
from . import ops as _ops

DISCOVERY_DEFAULT_INTERVAL_S = 21600  # 6h
JITTER_WINDOW_S = 900  # +/-15min deterministic per source
RESAMPLE_DEFAULT_ENABLED = False

EVENT_KINDS = ("new_observation", "new_creative", "changed_offer",
               "review_changed_match", "new_snapshot_observation")
OUTBOX_STATES = ("pending", "queued", "sent", "failed", "suppressed",
                 "uncertain", "cancelled")
POLL_OUTCOMES = ("success", "policy_blocked", "budget_exhausted", "failed")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def ensure_aux(con: sqlite3.Connection) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS monitor_schedule_state ("
                " schedule_id TEXT PRIMARY KEY REFERENCES schedules(id),"
                " workspace_id TEXT NOT NULL DEFAULT 'local',"
                " next_due_at TEXT NOT NULL DEFAULT '',"
                " last_attempt_at TEXT, last_success_at TEXT,"
                " last_coverage TEXT NOT NULL DEFAULT '{}',"
                " paused_reason TEXT NOT NULL DEFAULT '',"
                " updated_at TEXT NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS monitor_baselines ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " workspace_id TEXT NOT NULL DEFAULT 'local',"
                " watchlist_id INTEGER NOT NULL,"
                " definition_revision INTEGER NOT NULL,"
                " identities TEXT NOT NULL DEFAULT '[]',"
                " created_at TEXT NOT NULL,"
                " UNIQUE (workspace_id, watchlist_id, definition_revision))")
    con.execute("CREATE TABLE IF NOT EXISTS monitor_preferences ("
                " workspace_id TEXT NOT NULL DEFAULT 'local',"
                " member TEXT NOT NULL,"
                " digest TEXT NOT NULL DEFAULT 'daily',"
                " immediate INTEGER NOT NULL DEFAULT 0,"
                " unsubscribed INTEGER NOT NULL DEFAULT 0,"
                " PRIMARY KEY (workspace_id, member))")
    con.execute("CREATE TABLE IF NOT EXISTS delivery_attempts ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " workspace_id TEXT NOT NULL DEFAULT 'local',"
                " outbox_id INTEGER NOT NULL REFERENCES notification_outbox(id),"
                " provider TEXT NOT NULL DEFAULT '',"
                " provider_key TEXT NOT NULL DEFAULT '',"
                " provider_message_id TEXT NOT NULL DEFAULT '',"
                " state TEXT NOT NULL DEFAULT 'pending',"
                " detail TEXT NOT NULL DEFAULT '{}',"
                " created_at TEXT NOT NULL)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_delivery_outbox ON delivery_attempts (outbox_id)")
    # Uniqueness guard for (event, recipient) lives in the schema, created
    # BEFORE any insert. Creating it after inserts crashed the first-ever
    # duplicate-recipient call with IntegrityError from CREATE INDEX (full
    # rollback). IF NOT EXISTS keeps this migration-safe on replay.
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_event_recipient"
                " ON notification_outbox (event_id, recipient)")


# -- scheduling -----------------------------------------------------------
def _require_aux(con):
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"monitor_schedule_state", "monitor_baselines", "monitor_preferences", "delivery_attempts"} <= tables:
        raise _ops.BadInput("monitoring schema requires explicit workspace setup")



def jitter_for(source_id: str, interval_s: int = DISCOVERY_DEFAULT_INTERVAL_S) -> int:
    """Deterministic per-source jitter in [-JITTER_WINDOW_S, +JITTER_WINDOW_S]."""
    digest = hashlib.sha256(str(source_id or "").encode()).hexdigest()
    span = 2 * JITTER_WINDOW_S + 1
    return int(digest, 16) % span - JITTER_WINDOW_S


def next_due_at(last_iso: str, interval_s: int, source_id: str = "") -> str:
    base = _parse_utc(last_iso)
    due = base + timedelta(seconds=int(interval_s) + jitter_for(source_id, interval_s))
    return due.isoformat(timespec="seconds")


def compute_initial_due(now_iso: str, interval_s: int, source_id: str = "") -> str:
    return next_due_at(now_iso, interval_s, source_id)


@_db.atomic
def save_schedule_state(con: sqlite3.Connection, schedule_id: str, workspace_id: str,
                        next_due: str) -> dict:
    _require_aux(con)
    con.execute("INSERT INTO monitor_schedule_state (schedule_id, workspace_id,"
                " next_due_at, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(schedule_id) DO UPDATE SET next_due_at=excluded.next_due_at,"
                " updated_at=excluded.updated_at",
                (schedule_id, workspace_id, next_due, _now_iso()))
    return {"schedule_id": schedule_id, "next_due_at": next_due}


def due_schedules(con: sqlite3.Connection, now_iso: str) -> list:
    """Schedules due at `now`; each due schedule appears ONCE (coalesced).

    A schedule that missed N intervals yields one catch-up entry with
    missed_intervals=N, never N entries.
    """
    _require_aux(con)
    now = _parse_utc(now_iso)
    out = []
    for row in con.execute("SELECT s.*, st.next_due_at, st.paused_reason"
                           " FROM schedules s LEFT JOIN monitor_schedule_state st"
                           " ON st.schedule_id=s.id WHERE s.enabled=1"):
        d = dict(row)
        due_raw = d.get("next_due_at") or ""
        if not due_raw:
            continue
        try:
            due = _parse_utc(due_raw)
        except (ValueError, TypeError):
            continue
        if d.get("paused_reason"):
            continue  # paused on policy/budget failure: no new due until resumed
        if due <= now:
            interval = int(d.get("interval_s") or DISCOVERY_DEFAULT_INTERVAL_S)
            missed = max(1, int((now - due).total_seconds() // max(1, interval)) + 1)
            out.append({**d, "coalesced": True, "missed_intervals": missed})
    return out


@_db.atomic
def record_poll_result(con: sqlite3.Connection, schedule_id: str, outcome: str,
                       coverage: dict | None = None) -> dict:
    """Record a poll/run outcome. Policy/budget failures pause the schedule.

    Successful polls advance next_due_at by one interval (coalesced) and
    store last successful coverage. Paused schedules keep showing the last
    successful coverage, not just the last attempt.
    """
    if outcome not in POLL_OUTCOMES:
        raise _ops.BadInput(f"unknown poll outcome: {outcome!r}")
    _require_aux(con)
    row = con.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown schedule")
    sched = dict(row)
    st = con.execute("SELECT * FROM monitor_schedule_state WHERE schedule_id=?",
                     (schedule_id,)).fetchone()
    state = dict(st) if st else {}
    now = _now_iso()
    coverage_json = json.dumps(coverage or {})
    if outcome == "success":
        sources = json.loads(sched.get("source_ids") or "[]")
        first = str((sources or [""])[0])
        due = next_due_at(now, int(sched.get("interval_s") or DISCOVERY_DEFAULT_INTERVAL_S),
                          first)
        con.execute("INSERT INTO monitor_schedule_state (schedule_id, workspace_id,"
                    " next_due_at, last_attempt_at, last_success_at, last_coverage,"
                    " paused_reason, updated_at) VALUES (?, ?, ?, ?, ?, ?, '', ?)"
                    " ON CONFLICT(schedule_id) DO UPDATE SET next_due_at=excluded.next_due_at,"
                    " last_attempt_at=excluded.last_attempt_at,"
                    " last_success_at=excluded.last_success_at,"
                    " last_coverage=excluded.last_coverage, paused_reason='',"
                    " updated_at=excluded.updated_at",
                    (schedule_id, sched.get("workspace_id") or "local", due, now, now,
                     coverage_json, now))
        return {"schedule_id": schedule_id, "paused": False, "next_due_at": due,
                "last_coverage": coverage or {}}
    if outcome in ("policy_blocked", "budget_exhausted"):
        con.execute("INSERT INTO monitor_schedule_state (schedule_id, workspace_id,"
                    " next_due_at, last_attempt_at, updated_at, paused_reason,"
                    " last_coverage) VALUES (?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(schedule_id) DO UPDATE SET"
                    " last_attempt_at=excluded.last_attempt_at,"
                    " paused_reason=excluded.paused_reason,"
                    " updated_at=excluded.updated_at",
                    (schedule_id, sched.get("workspace_id") or "local",
                     state.get("next_due_at") or now, now, now, outcome,
                     state.get("last_coverage") or "{}"))
        last_ok = json.loads(state.get("last_coverage") or "{}")
        return {"schedule_id": schedule_id, "paused": True, "reason": outcome,
                "last_successful_coverage": last_ok}
    # outcome == "failed": pause with backoff like policy/budget outcomes.
    # next_due_at advances one interval past now (no hot retry on a stale
    # past due); the schedule stays paused until an explicit resume.
    # Last successful coverage is preserved, never the failed attempt.
    sources = json.loads(sched.get("source_ids") or "[]")
    first = str((sources or [""])[0])
    due = next_due_at(now, int(sched.get("interval_s") or DISCOVERY_DEFAULT_INTERVAL_S),
                      first)
    con.execute("INSERT INTO monitor_schedule_state (schedule_id, workspace_id,"
                " next_due_at, last_attempt_at, updated_at, paused_reason,"
                " last_coverage) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(schedule_id) DO UPDATE SET"
                " next_due_at=excluded.next_due_at,"
                " last_attempt_at=excluded.last_attempt_at,"
                " paused_reason=excluded.paused_reason,"
                " updated_at=excluded.updated_at",
                (schedule_id, sched.get("workspace_id") or "local",
                 due, now, now, outcome,
                 state.get("last_coverage") or "{}"))
    last_ok = json.loads(state.get("last_coverage") or "{}")
    return {"schedule_id": schedule_id, "paused": True, "reason": outcome,
            "next_due_at": due, "last_successful_coverage": last_ok}


@_db.atomic
def resume_schedule(con: sqlite3.Connection, schedule_id: str) -> dict:
    """Explicit resume after a paused outcome (policy/budget/failed).

    Clears paused_reason and recomputes next_due_at from now, so a
    previously failed schedule rejoins its cadence instead of firing
    a stale catch-up immediately.
    """
    _require_aux(con)
    row = con.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown schedule")
    sched = dict(row)
    now = _now_iso()
    sources = json.loads(sched.get("source_ids") or "[]")
    first = str((sources or [""])[0])
    due = next_due_at(now, int(sched.get("interval_s") or DISCOVERY_DEFAULT_INTERVAL_S),
                      first)
    con.execute("INSERT INTO monitor_schedule_state (schedule_id, workspace_id,"
                " next_due_at, last_attempt_at, updated_at, paused_reason)"
                " VALUES (?, ?, ?, ?, ?, '')"
                " ON CONFLICT(schedule_id) DO UPDATE SET"
                " next_due_at=excluded.next_due_at,"
                " last_attempt_at=excluded.last_attempt_at,"
                " paused_reason='', updated_at=excluded.updated_at",
                (schedule_id, sched.get("workspace_id") or "local",
                 due, now, now))
    return {"schedule_id": schedule_id, "paused": False, "next_due_at": due}


def schedule_status(con: sqlite3.Connection, schedule_id: str) -> dict:
    _require_aux(con)
    row = con.execute("SELECT s.*, st.next_due_at, st.last_attempt_at,"
                      " st.last_success_at, st.last_coverage, st.paused_reason"
                      " FROM schedules s LEFT JOIN monitor_schedule_state st"
                      " ON st.schedule_id=s.id WHERE s.id=?", (schedule_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown schedule")
    d = dict(row)
    try:
        d["last_coverage"] = json.loads(d.get("last_coverage") or "{}")
    except (json.JSONDecodeError, TypeError):
        d["last_coverage"] = {}
    # Resampling disabled unless the schedule explicitly selects it.
    d["resampling"] = "selected" if (d.get("mode") == "resample") else "disabled"
    return d


# -- baselines ------------------------------------------------------------

def _baseline_identities(matches: list) -> list:
    return sorted({str(m.get("identity") or "") for m in (matches or []) if m.get("identity")})


@_db.atomic
def establish_baseline(con: sqlite3.Connection, workspace_id: str, watchlist_id: int,
                       definition_revision: int, current_matches: list) -> dict:
    """Baseline current matching identities at monitor enable. No flood."""
    _require_aux(con)
    ids = _baseline_identities(current_matches)
    con.execute("INSERT INTO monitor_baselines (workspace_id, watchlist_id,"
                " definition_revision, identities, created_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(workspace_id, watchlist_id, definition_revision)"
                " DO UPDATE SET identities=excluded.identities",
                (workspace_id, watchlist_id, int(definition_revision),
                 json.dumps(ids), _now_iso()))
    return {"watchlist_id": watchlist_id, "definition_revision": int(definition_revision),
            "baseline_count": len(ids)}


def get_baseline(con: sqlite3.Connection, workspace_id: str, watchlist_id: int,
                 definition_revision: int) -> list:
    _require_aux(con)
    row = con.execute("SELECT identities FROM monitor_baselines WHERE workspace_id=?"
                      " AND watchlist_id=? AND definition_revision=?",
                      (workspace_id, watchlist_id, int(definition_revision))).fetchone()
    if row is None:
        return []
    try:
        return list(json.loads(row["identities"]))
    except (json.JSONDecodeError, TypeError):
        return []


# -- transitions + transactional outbox -----------------------------------

def _schedule_watchlist_id(schedule_id: str) -> int | None:
    """Watchlist id from the `watchlist-<wid>` schedule convention, else None."""
    head, sep, tail = str(schedule_id or "").partition("-")
    if not sep or head != "watchlist" or not tail.isdigit():
        return None
    return int(tail)


def _event_key(workspace_id: str, definition_revision: int, kind: str,
               identity: str) -> str:
    return "|".join([str(workspace_id), str(int(definition_revision)), str(kind),
                     str(identity)])


def _validate_match(match: dict) -> tuple[str, str]:
    kind = str(match.get("kind") or "")
    identity = str(match.get("identity") or "")
    if kind not in EVENT_KINDS:
        raise _ops.BadInput(f"unknown event kind: {kind!r}")
    if not identity:
        raise _ops.BadInput("match identity is required")
    return kind, identity


def _detect_impl(con: sqlite3.Connection, workspace_id: str,
                 schedule_id: str, definition_revision: int,
                 current_matches: list,
                 membership_check=None) -> dict:
    """Inner detect (no transaction of its own; caller owns the boundary).

    Only identities absent from the baseline become events (no replay of
    history, no flood). Source corrections (same occurrence id, corrected
    fields) are NOT new-placement events: callers must pass them with
    kind 'review_changed_match' only when review actually changed the
    match, otherwise omit them. Unresolved cross-snapshot observations
    must arrive as kind 'new_snapshot_observation'.
    Returns created vs duplicate counts (RED-CAPABLE: repeat poll dupes=0).
    """
    _require_aux(con)
    sched = con.execute("SELECT id FROM schedules WHERE id=?", (schedule_id,)).fetchone()
    if sched is None:
        raise _ops.BadInput("unknown schedule")
    wid = _schedule_watchlist_id(schedule_id)
    if wid is not None:
        baseline = set(get_baseline(con, workspace_id, wid, int(definition_revision)))
    else:
        # Schedule id carries no watchlist link: gate on every baseline
        # recorded for this workspace + definition revision (revision-scoped,
        # so definition edits still emit under the new revision).
        baseline = set()
        for brow in con.execute("SELECT identities FROM monitor_baselines"
                                " WHERE workspace_id=? AND definition_revision=?",
                                (workspace_id, int(definition_revision))).fetchall():
            try:
                baseline.update(json.loads(brow["identities"]))
            except (json.JSONDecodeError, TypeError):
                continue
    known_rows = con.execute("SELECT event_key FROM monitor_events WHERE schedule_id=?",
                             (schedule_id,)).fetchall()
    known = {r["event_key"] for r in known_rows}
    created, duplicates = 0, 0
    created_ids: list = []
    for match in (current_matches or []):
        kind, identity = _validate_match(match)
        if identity in baseline:
            duplicates += 1  # baselined at enable: not a new transition
            continue
        key = _event_key(workspace_id, definition_revision, kind, identity)
        if key in known:
            duplicates += 1
            continue
        detail = {"kind": kind, "identity": identity,
                  "definition_revision": int(definition_revision),
                  "occurrence_id": match.get("occurrence_id"),
                  "creative_id": match.get("creative_id"),
                  "source_pins": match.get("source_pins") or {},
                  "processing_state": match.get("processing_state") or "processed",
                  "ambiguous": bool(match.get("ambiguous", False))}
        if detail["processing_state"] in ("failed", "missing"):
            detail["not_a_no_match"] = True  # failed processing != no matches
        try:
            cur = con.execute("INSERT INTO monitor_events (workspace_id, schedule_id,"
                              " event_key, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                              (workspace_id, schedule_id, key, json.dumps(detail),
                               _now_iso()))
        except sqlite3.IntegrityError:
            duplicates += 1
            continue
        known.add(key)
        created += 1
        _ = cur.lastrowid
        created_ids.append(cur.lastrowid)
    return {"created": created, "duplicates": duplicates,
            "event_ids": created_ids}


@_db.atomic
def detect_match_transitions(con: sqlite3.Connection, workspace_id: str,
                             schedule_id: str, definition_revision: int,
                             current_matches: list,
                             membership_check=None) -> dict:
    """Commit new match transitions atomically (compat entry point).

    Only identities absent from the baseline become events (no replay of
    history, no flood). ... Returns created vs duplicate counts
    (RED-CAPABLE: repeat poll dupes=0).
    """
    _require_aux(con)
    out = _detect_impl(con, workspace_id, schedule_id, int(definition_revision),
                       current_matches, membership_check)
    return {"created": out["created"], "duplicates": out["duplicates"]}


def _commit_impl(con: sqlite3.Connection, workspace_id: str,
                 event_ids: list, recipients: list,
                 membership_check=None,
                 preferences_lookup=None) -> dict:
    """Inner queue (no transaction of its own; caller owns the boundary).

    Recipients are deduped defensively before insert, so a repeated
    recipient yields one row, never an IntegrityError. Delivery stays
    at-least-once with documented duplicate risk (never exactly-once).
    """
    queued, suppressed, skipped = 0, 0, 0
    seen: set = set()
    unique_recipients = []
    for recipient in (recipients or []):
        member = str(recipient)
        if member in seen:
            skipped += 1  # duplicate recipient in one call: one row only
            continue
        seen.add(member)
        unique_recipients.append(member)
    for event_id in (event_ids or []):
        ev = con.execute("SELECT * FROM monitor_events WHERE id=?", (event_id,)).fetchone()
        if ev is None:
            skipped += 1
            continue
        for member in unique_recipients:
            if membership_check is not None and not membership_check(member):
                skipped += 1  # revoked/non-member: nothing queued
                continue
            if preferences_lookup is not None:
                prefs = preferences_lookup(member) or {}
                if prefs.get("unsubscribed"):
                    cur = con.execute("INSERT OR IGNORE INTO notification_outbox"
                                      " (workspace_id, event_id, recipient, state, detail,"
                                      " created_at) VALUES (?, ?, ?, 'suppressed', ?, ?)",
                                      (workspace_id, event_id, member,
                                       json.dumps({"reason": "unsubscribed"}), _now_iso()))
                    suppressed += 1 if cur.rowcount else 0
                    continue
            try:
                con.execute("INSERT INTO notification_outbox"
                            " (workspace_id, event_id, recipient, state, detail,"
                            " created_at) VALUES (?, ?, ?, 'queued', ?, ?)",
                            (workspace_id, event_id, member, json.dumps({}), _now_iso()))
                queued += 1
            except sqlite3.IntegrityError:
                skipped += 1  # unique pair already queued (second layer)
    return {"queued": queued, "suppressed": suppressed, "skipped": skipped}


@_db.atomic
def commit_events_with_outbox(con: sqlite3.Connection, workspace_id: str,
                              event_ids: list, recipients: list,
                              membership_check=None,
                              preferences_lookup=None) -> dict:
    """Queue outbox entries for committed events under one unique key.

    Revoked members receive nothing; unsubscribed members are suppressed
    (visible, not silent). Each (event, recipient) pair is unique.
    Delivery is at-least-once with documented duplicate risk.
    """
    _require_aux(con)
    return _commit_impl(con, workspace_id, event_ids, recipients,
                        membership_check, preferences_lookup)


@_db.atomic
def publish_alert_emissions(con: sqlite3.Connection, workspace_id: str,
                            alert_id: int, emissions: list,
                            recipients: list,
                            membership_check=None,
                            preferences_lookup=None,
                            max_events: int | None = None) -> dict:
    """Bridge scheduled customer-alert emissions into monitor events and
    the notification outbox, bounded by max_events.

    Each emission becomes one monitor_events row under the synthetic
    schedule f"alert:{alert_id}" keyed by its transition key, so replays
    are idempotent by construction; outbox entries carry the same
    (event, recipient) uniqueness. Every emission is recorded as a
    pending publication; at most max_events of the oldest pending ones
    are queued per call, and the rest stay pending for the next call
    (the cap defers, never drops). Delivery itself stays a separate,
    explicitly-invoked step (attempt_delivery with a fixture provider).
    """
    _require_aux(con)
    schedule_id = f"alert:{int(alert_id)}"
    con.execute(
        "INSERT OR IGNORE INTO schedules (id, workspace_id, mode, source_ids,"
        " interval_s, enabled, definition_revision, revision, updated_at)"
        " VALUES (?, ?, 'alert-monitor', '[]', 21600, 1, 1, 1, ?)",
        (schedule_id, workspace_id, _now_iso()))
    skipped = 0
    for emission in emissions or []:
        key = str((emission or {}).get("transition_key") or "")
        if not key:
            skipped += 1
            continue
        con.execute(
            "INSERT OR IGNORE INTO monitor_events (workspace_id, schedule_id,"
            " event_key, detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (workspace_id, schedule_id, key,
             json.dumps({"alert_id": int(alert_id),
                         "kind": (emission or {}).get("kind"),
                         "logical_identity": (emission or {}).get("logical_identity"),
                         "publication": "pending"}),
             _now_iso()))
    pending = ("SELECT id FROM monitor_events WHERE workspace_id=?"
               " AND schedule_id=? AND json_extract(detail, '$.publication')='pending'"
               " ORDER BY id")
    limit = -1 if max_events is None else max(0, int(max_events))
    event_ids = [r["id"] for r in con.execute(pending + " LIMIT ?",
                                              (workspace_id, schedule_id, limit))]
    queued = commit_events_with_outbox(con, workspace_id, event_ids,
                                       recipients or [],
                                       membership_check, preferences_lookup)
    for event_id in event_ids:
        con.execute("UPDATE monitor_events SET detail=json_set(detail,"
                    " '$.publication', 'queued') WHERE id=?", (event_id,))
    deferred = len(con.execute(pending, (workspace_id, schedule_id)).fetchall())
    return {"schedule_id": schedule_id, "events": len(event_ids),
            "deferred": deferred, "skipped": skipped, **queued}


@_db.atomic
def detect_and_queue(con: sqlite3.Connection, workspace_id: str,
                     schedule_id: str, definition_revision: int,
                     current_matches: list, recipients: list,
                     membership_check=None,
                     preferences_lookup=None) -> dict:
    """Detect transitions AND queue outbox entries in ONE transaction.

    A crash between detect and queue is impossible by construction: both
    run under this call's single atomic boundary, so a queue failure
    rolls back the transitions too. The individual functions stay for
    compat; new callers (server detect handler) use this.
    Delivery stays at-least-once with documented duplicate risk.
    """
    _require_aux(con)
    detected = _detect_impl(con, workspace_id, schedule_id,
                            int(definition_revision), current_matches,
                            membership_check)
    queued = _commit_impl(con, workspace_id, detected["event_ids"],
                          recipients, membership_check, preferences_lookup)
    return {"created": detected["created"], "duplicates": detected["duplicates"],
            **queued}


@_db.atomic
def resolve_correspondence(con: sqlite3.Connection, event_id: int,
                           resolution: dict, actor: str = "owner") -> dict:
    """Update an event's correspondence relationship; never replays placement."""
    row = con.execute("SELECT * FROM monitor_events WHERE id=?", (event_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown event")
    detail = json.loads(row["detail"])
    detail["correspondence"] = {"basis": resolution.get("basis"),
                               "reviewer": actor,
                               "at": _now_iso()}
    con.execute("UPDATE monitor_events SET detail=? WHERE id=?",
                (json.dumps(detail), event_id))
    rev = _db.bump_revision(con)
    _db.audit(con, actor, "correspondence_resolve", "monitor_event", str(event_id),
              {"basis": resolution.get("basis")}, rev)
    return {"event_id": event_id, "replayed": False, "aggregate_rev": rev}


# -- preferences / digest / delivery --------------------------------------

def set_preferences(con: sqlite3.Connection, workspace_id: str, member: str,
                    digest: str = "daily", immediate: bool = False) -> dict:
    _require_aux(con)
    if digest not in ("daily", "immediate", "none"):
        raise _ops.BadInput("digest must be daily, immediate, or none")
    with _db.transaction(con, write=True):
        con.execute("INSERT INTO monitor_preferences (workspace_id, member, digest,"
                    " immediate, unsubscribed) VALUES (?, ?, ?, ?, 0)"
                    " ON CONFLICT(workspace_id, member) DO UPDATE SET digest=excluded.digest,"
                    " immediate=excluded.immediate",
                    (workspace_id, member, digest, int(bool(immediate))))
    return {"member": member, "digest": digest, "immediate": bool(immediate)}


def set_unsubscribed(con: sqlite3.Connection, workspace_id: str, member: str,
                     unsubscribed: bool = True) -> dict:
    _require_aux(con)
    with _db.transaction(con, write=True):
        con.execute("INSERT INTO monitor_preferences (workspace_id, member, unsubscribed)"
                    " VALUES (?, ?, ?)"
                    " ON CONFLICT(workspace_id, member) DO UPDATE SET"
                    " unsubscribed=excluded.unsubscribed",
                    (workspace_id, member, int(bool(unsubscribed))))
    return {"member": member, "unsubscribed": bool(unsubscribed)}


def get_preferences(con: sqlite3.Connection, workspace_id: str, member: str) -> dict:
    _require_aux(con)
    row = con.execute("SELECT * FROM monitor_preferences WHERE workspace_id=? AND member=?",
                      (workspace_id, member)).fetchone()
    if row is None:
        return {"member": member, "digest": "daily", "immediate": False,
                "unsubscribed": False}
    d = dict(row)
    return {"member": member, "digest": d.get("digest") or "daily",
            "immediate": bool(d.get("immediate")),
            "unsubscribed": bool(d.get("unsubscribed"))}


def digest_queue(con: sqlite3.Connection, workspace_id: str, member: str) -> list:
    """Pending queued entries for a member (daily digest default)."""
    _require_aux(con)
    return [dict(r) for r in con.execute(
        "SELECT o.*, e.detail AS event_detail FROM notification_outbox o"
        " JOIN monitor_events e ON e.id=o.event_id"
        " WHERE o.workspace_id=? AND o.recipient=? AND o.state='queued'"
        " ORDER BY o.id", (workspace_id, member)).fetchall()]


class SandboxDeliveryProvider:
    """Selected-provider sandbox: in-app + ONE email provider shape.

    No webhooks. Provider idempotency via provider_key where available;
    otherwise at-least-once with documented duplicate risk. Uncertain
    responses must be reconciled before resend (no blind retry).
    """

    name = "sandbox"

    def __init__(self):
        self.sent: list = []
        self.provider_keys: set = set()

    def provider_key(self, outbox_id: int, recipient: str) -> str:
        raw = hashlib.sha256(f"{outbox_id}|{recipient}|{self.name}".encode()).hexdigest()[:24]
        return f"{self.name}-{raw}"

    def deliver(self, entry: dict, email_link: str = "") -> dict:
        """Sandbox delivery: records minimal metadata + link, no transcript."""
        key = self.provider_key(entry["id"], entry["recipient"])
        duplicate_risk = key in self.provider_keys
        self.provider_keys.add(key)
        message_id = f"msg-{uuid.uuid4().hex[:12]}"
        payload = {"to": entry["recipient"], "link": email_link or
                   f"https://app.local/n/{entry['event_id']}?to={entry['recipient']}",
                   "provider_key": key,
                   "duplicate_risk": "at-least-once" if duplicate_risk else "none-observed"}
        self.sent.append(payload)
        return {"message_id": message_id, "payload": payload,
                "duplicate_risk": payload["duplicate_risk"]}


def _refuse_invite_row(row) -> None:
    """Invite emails (kind workspace_invite) carry a stored identity and
    are sent and reconciled only by invites_send. The alert outbox
    functions refuse them before any write."""
    try:
        detail = json.loads(row["detail"] or "{}")
    except (TypeError, ValueError):
        return
    if isinstance(detail, dict) and detail.get("kind") == "workspace_invite":
        raise _ops.BadInput("invite messages are sent and reconciled by"
                            " invites_send, not the alert outbox")


@_db.atomic
def attempt_delivery(con: sqlite3.Connection, outbox_id: int, provider,
                     membership_check=None, preferences_lookup=None,
                     rights_check=None) -> dict:
    """Send one outbox entry after rechecking rights at send time.

    An ambiguous provider reply (ProviderUncertain) is recorded as
    uncertain inside this same transaction and returned, never raised:
    the uncertain record commits in the same write transaction as the
    send, so any sender that re-reads and claims the row under the write
    lock sees it before it could resend."""
    _require_aux(con)
    row = con.execute("SELECT * FROM notification_outbox WHERE id=?", (outbox_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown outbox entry")
    _refuse_invite_row(row)
    entry = dict(row)
    if entry["state"] not in ("queued", "pending", "uncertain"):
        return {"outbox_id": outbox_id, "state": entry["state"], "resent": False}
    member = entry["recipient"]
    if membership_check is not None and not membership_check(member):
        con.execute("UPDATE notification_outbox SET state='cancelled' WHERE id=?",
                    (outbox_id,))
        return {"outbox_id": outbox_id, "state": "cancelled", "resent": False,
                "reason": "membership revoked before send"}
    if preferences_lookup is not None and (preferences_lookup(member) or {}).get("unsubscribed"):
        con.execute("UPDATE notification_outbox SET state='suppressed' WHERE id=?",
                    (outbox_id,))
        return {"outbox_id": outbox_id, "state": "suppressed", "resent": False}
    if rights_check is not None and not rights_check(member, entry["event_id"]):
        con.execute("UPDATE notification_outbox SET state='suppressed', detail=? WHERE id=?",
                    (json.dumps({"reason": "rights revoked before send"}), outbox_id))
        return {"outbox_id": outbox_id, "state": "suppressed", "resent": False}
    if entry["state"] == "uncertain":
        return {"outbox_id": outbox_id, "state": "uncertain", "resent": False,
                "reason": "reconcile uncertain response before resend"}
    try:
        result = provider.deliver(entry)
    except ProviderUncertain as exc:
        con.execute("UPDATE notification_outbox SET state='uncertain', detail=?"
                    " WHERE id=?", (json.dumps({"reason": str(exc)}), outbox_id))
        return {"outbox_id": outbox_id, "state": "uncertain", "resent": False,
                "reason": str(exc)}
    con.execute("UPDATE notification_outbox SET state='sent', detail=? WHERE id=?",
                (json.dumps({"provider": provider.name,
                             "provider_message_id": result["message_id"]}), outbox_id))
    con.execute("INSERT INTO delivery_attempts (workspace_id, outbox_id, provider,"
                " provider_key, provider_message_id, state, detail, created_at)"
                " VALUES (?, ?, ?, ?, ?, 'sent', ?, ?)",
                (entry["workspace_id"], outbox_id, provider.name,
                 result["payload"]["provider_key"], result["message_id"],
                 json.dumps({"duplicate_risk": result["duplicate_risk"]}), _now_iso()))
    return {"outbox_id": outbox_id, "state": "sent", "resent": True,
            "provider_message_id": result["message_id"],
            "duplicate_risk": result["duplicate_risk"]}


@_db.atomic
def mark_uncertain(con: sqlite3.Connection, outbox_id: int, detail: dict) -> dict:
    """Record an ambiguous send. Only a row still awaiting an outcome
    (queued, pending, uncertain) changes; a late call never overwrites a
    newer settled state (sent, failed, suppressed, cancelled)."""
    _require_aux(con)
    cur = con.execute("UPDATE notification_outbox SET state='uncertain', detail=?"
                      " WHERE id=? AND state IN ('queued', 'pending', 'uncertain')",
                      (json.dumps(detail), outbox_id))
    if not cur.rowcount:
        row = con.execute("SELECT state FROM notification_outbox WHERE id=?",
                          (outbox_id,)).fetchone()
        return {"outbox_id": outbox_id, "state": row["state"] if row else None,
                "reason": "settled state is never overwritten"}
    return {"outbox_id": outbox_id, "state": "uncertain"}


class ProviderUncertain(Exception):
    """Ambiguous provider outcome (timeout, 429/5xx, truncated reply):
    the send may or may not have happened. Callers must reconcile
    before any resend; never blind-retry."""


RESEND_EMAILS_URL = "https://api.resend.com/emails"
# Resend documents that idempotency keys are kept for 24 hours
# (https://resend.com/docs/dashboard/emails/idempotency-keys).
RESEND_IDEMPOTENCY_WINDOW_S = 24 * 3600
RESEND_IDEMPOTENCY_KEY_MAX = 256


def _urllib_transport(url: str, headers: dict, body: bytes, timeout: float):
    """POST body to url. Returns (status, reply bytes). Any HTTP status is
    returned; transport errors and short reads raise and the caller
    classifies them as uncertain."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        try:
            reply = exc.read()
        except Exception:  # noqa: BLE001 - an unreadable error body is still that status
            reply = b""
        return exc.code, reply


class ResendDeliveryProvider:
    """Production email transport over the Resend API (explicit config).

    send_prepared() posts fixed request bytes with Resend's documented
    Idempotency-Key transport header and classifies the reply:
    - "sent": a 2xx reply whose body is a JSON object with a message id;
    - "failed": a definite refusal (400, 401, 403, 404, 405, 422): the
      provider did not take the message;
    - "uncertain": everything else, including a missing, invalid or
      truncated 2xx body (R5-01), 409, 429, 5xx and transport errors.
      An uncertain send may or may not have been accepted; the caller
      reconciles it and never sends a new message in its place.
      "replayable" is False only for 409 invalid_idempotent_request (the
      key was already used with other bytes), where a replay cannot help.

    deliver() keeps the alert-outbox contract (same shape as the sandbox
    provider): uncertain raises ProviderUncertain, a refusal raises
    ValueError. It sends no transport idempotency key, so alerts stay
    at-least-once. No sandbox fallback: when Resend is selected it is
    used. transport is injectable for tests; the default uses urllib.
    """

    name = "resend"
    DEFINITE_REFUSALS = frozenset({400, 401, 403, 404, 405, 422})

    def __init__(self, api_key: str, from_addr: str, *, transport=None,
                 timeout: float = 20.0):
        if not api_key or not from_addr:
            raise ValueError("resend delivery requires an API key and sender")
        self.api_key = api_key
        self.from_addr = from_addr
        self.transport = transport or _urllib_transport
        self.timeout = timeout
        self.sent: list = []

    def provider_key(self, outbox_id: int, recipient: str) -> str:
        raw = hashlib.sha256(
            f"{outbox_id}|{recipient}|{self.name}".encode()).hexdigest()[:24]
        return f"{self.name}-{raw}"

    def build_body(self, to: str, subject: str, text: str,
                   email_headers: dict | None = None) -> bytes:
        """Canonical request bytes (sorted keys, ASCII): store them and
        replay them unchanged."""
        payload = {"from": self.from_addr, "to": [to], "subject": subject,
                   "text": text}
        if email_headers:
            payload["headers"] = dict(email_headers)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True).encode("ascii")

    def send_prepared(self, body: bytes, idempotency_key: str = "") -> dict:
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json",
                   "Accept": "application/json"}
        if idempotency_key:
            if len(idempotency_key) > RESEND_IDEMPOTENCY_KEY_MAX:
                raise ValueError("idempotency key is longer than 256 characters")
            headers["Idempotency-Key"] = idempotency_key
        try:
            status, reply = self.transport(RESEND_EMAILS_URL, headers, bytes(body),
                                           self.timeout)
        except Exception as exc:  # noqa: BLE001 - every transport failure is ambiguous
            return {"outcome": "uncertain", "replayable": True,
                    "reason": f"resend transport ambiguous: {type(exc).__name__}"}
        return self._classify(int(status), reply)

    def _classify(self, status: int, reply) -> dict:
        try:
            data = json.loads(bytes(reply or b"").decode("utf-8"))
        except (ValueError, UnicodeDecodeError, TypeError):
            data = None
        if 200 <= status < 300:
            message_id = data.get("id") if isinstance(data, dict) else None
            if isinstance(message_id, str) and message_id.strip():
                return {"outcome": "sent", "message_id": message_id}
            return {"outcome": "uncertain", "replayable": True,
                    "reason": f"resend HTTP {status} reply had no readable message id"}
        name = str(data.get("name") or "") if isinstance(data, dict) else ""
        if status == 409 and name == "invalid_idempotent_request":
            return {"outcome": "uncertain", "replayable": False,
                    "reason": "resend reports this key was used with another payload"}
        if status in self.DEFINITE_REFUSALS:
            return {"outcome": "failed",
                    "reason": f"resend refused the message: HTTP {status} {name}".strip()}
        return {"outcome": "uncertain", "replayable": True,
                "reason": f"resend ambiguous HTTP {status} {name}".strip()}

    def deliver(self, entry: dict, email_link: str = "") -> dict:
        key = self.provider_key(entry["id"], entry["recipient"])
        detail = {}
        try:
            detail = json.loads(entry.get("detail") or "{}")
        except (ValueError, TypeError):
            detail = {}
        if not isinstance(detail, dict):
            detail = {}
        subject = str(detail.get("subject") or "EarWitness notification")
        text = str(detail.get("text") or email_link
                   or f"https://app.local/n/{entry['event_id']}?to={entry['recipient']}")
        body = self.build_body(entry["recipient"], subject, text,
                               {"X-Entity-Ref-ID": key})
        result = self.send_prepared(body)
        if result["outcome"] == "uncertain":
            raise ProviderUncertain(result["reason"])
        if result["outcome"] == "failed":
            raise ValueError(result["reason"])
        record = {"to": entry["recipient"], "provider_key": key,
                  "duplicate_risk": "none-observed"}
        self.sent.append(record)
        return {"message_id": result["message_id"], "payload": record,
                "duplicate_risk": "none-observed"}


def select_delivery_provider(config: dict | None = None,
                             *, allow_sandbox: bool = False):
    """Explicit provider selection. Resend when a key+sender are
    configured; the sandbox shape only when explicitly allowed (local
    tests/dev). Otherwise unavailable — never a silent sandbox success
    presented as live delivery."""
    config = dict(config or {})
    api_key = str(config.get("resend_api_key") or "")
    from_addr = str(config.get("resend_from") or "")
    if api_key and from_addr:
        return ResendDeliveryProvider(api_key, from_addr)
    if allow_sandbox:
        return SandboxDeliveryProvider()
    raise ValueError("no delivery provider configured")


def delivery_provider_config_from_env(env=None) -> dict:
    import os
    src = env if env is not None else os.environ
    return {"resend_api_key": str(src.get("RESEND_API_KEY") or ""),
            "resend_from": str(src.get("EARWITNESS_RESEND_FROM") or "")}


def recover_outbox(con: sqlite3.Connection, outbox_id: int, provider,
                   membership_check=None, preferences_lookup=None,
                   rights_check=None) -> dict:
    """Explicit queued/stream recheck and recovery rules. Terminal
    states (sent, cancelled, suppressed) are no-ops; uncertain entries
    must be reconciled first (never blind-retried); failed entries may
    retry once the cause is addressed; pending/queued entries attempt
    delivery. No new failure classes: outcomes reuse the existing
    sent/cancelled/suppressed/uncertain vocabulary."""
    _require_aux(con)
    row = con.execute("SELECT * FROM notification_outbox WHERE id=?",
                      (outbox_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown outbox entry")
    _refuse_invite_row(row)
    state = row["state"]
    if state in ("sent", "cancelled", "suppressed"):
        return {"outbox_id": outbox_id, "state": state, "resent": False,
                "reason": "terminal state is never re-driven"}
    if state == "uncertain":
        return {"outbox_id": outbox_id, "state": "uncertain", "resent": False,
                "reason": "reconcile the uncertain response before resend"}
    if state == "failed":
        # A failure is only ever recorded by reconciliation of an uncertain
        # send (confirmed failed). Its explicit recovery claims the row
        # (failed -> queued) and sends in the same transaction, so the
        # retry and its outcome commit together: a refused send leaves it
        # failed, an ambiguous one commits as uncertain (attempt_delivery
        # records it before the claim ends). The state and the
        # recorded-send check are re-read under the write lock, so a
        # concurrent recovery that lost the race sends nothing and reports
        # the state it found, and a row with a recorded send is never sent
        # again (at most once).
        with _db.transaction(con, write=True):
            now = con.execute("SELECT state FROM notification_outbox WHERE id=?",
                              (outbox_id,)).fetchone()["state"]
            if now != "failed":
                return {"outbox_id": outbox_id, "state": now, "resent": False,
                        "reason": "no longer failed; not re-driven"}
            if con.execute("SELECT 1 FROM delivery_attempts WHERE outbox_id=?"
                           " AND state='sent' LIMIT 1", (outbox_id,)).fetchone():
                return {"outbox_id": outbox_id, "state": "failed", "resent": False,
                        "reason": "a send is already recorded; never resent"}
            con.execute("UPDATE notification_outbox SET state='queued'"
                        " WHERE id=? AND state='failed'", (outbox_id,))
            return attempt_delivery(con, outbox_id, provider,
                                    membership_check, preferences_lookup,
                                    rights_check)
    return attempt_delivery(con, outbox_id, provider, membership_check,
                            preferences_lookup, rights_check)


def run_due_checks(con: sqlite3.Connection, workspace_id: str, now_iso: str,
                   fetch_matches, recipients: list,
                   membership_check=None, preferences_lookup=None,
                   max_schedules: int = 10) -> dict:
    """Scheduled monitoring action: run each due schedule's check locally.

    fetch_matches(schedule) supplies the current matches explicitly
    (event triggers carry matches; nothing is synthesized). Fetcher
    failure records a failed poll under the existing backoff rules.
    Bounded per run; each due schedule fires once (coalesced).
    """
    _require_aux(con)
    ran: list = []
    for sched in due_schedules(con, now_iso):
        if sched.get("workspace_id") != workspace_id:
            continue
        if len(ran) >= max(1, max_schedules):
            break
        try:
            matches = fetch_matches(sched)
            if not isinstance(matches, list):
                raise ValueError("match fetcher must return a list")
            out = detect_and_queue(
                con, workspace_id, sched["id"],
                int(sched.get("definition_revision") or 1), matches,
                list(recipients or []), membership_check, preferences_lookup)
            poll = record_poll_result(con, sched["id"], "success",
                                      {"checked": len(matches),
                                       "events": len(out.get("event_ids", []))})
            ran.append({"schedule_id": sched["id"], "checked": len(matches),
                        "events": out, "poll": poll})
        except Exception as exc:  # noqa: BLE001 - failed polls are recorded, not raised
            try:
                poll = record_poll_result(con, sched["id"], "failed")
            except Exception:
                poll = {"schedule_id": sched["id"], "paused": True}
            ran.append({"schedule_id": sched["id"], "failed": str(exc),
                        "poll": poll})
    return {"workspace_id": workspace_id, "ran": ran}


@_db.atomic
def reconcile_delivery(con: sqlite3.Connection, outbox_id: int, disposition: str,
                       receipt: dict) -> dict:
    """Reconcile an uncertain send before any resend (confirmed/failed/still-unknown).

    A failed row accepts only confirmed_sent: a record correction of a
    mistaken failure that never sends. Any other settled row is refused."""
    if disposition not in ("confirmed_sent", "confirmed_failed", "still_unknown"):
        raise _ops.BadInput("unknown reconciliation disposition")
    _require_aux(con)
    row = con.execute("SELECT * FROM notification_outbox WHERE id=?", (outbox_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown outbox entry")
    _refuse_invite_row(row)
    correction = row["state"] == "failed" and disposition == "confirmed_sent"
    if row["state"] != "uncertain" and not correction:
        # Only an uncertain send is reconciled; a settled row never moves
        # toward a send, so a sent row can never be recorded as failed and
        # then recovered into a resend. The one correction allowed is a
        # mistaken failure confirmed as sent: a record change, never a send.
        raise _ops.BadInput(f"outbox entry is {row['state']}, not uncertain")
    if disposition == "confirmed_sent":
        con.execute("UPDATE notification_outbox SET state='sent', detail=? WHERE id=?",
                    (json.dumps({"reconciled": True, "receipt": receipt}), outbox_id))
    elif disposition == "confirmed_failed":
        con.execute("UPDATE notification_outbox SET state='failed', detail=? WHERE id=?",
                    (json.dumps({"reconciled": True, "receipt": receipt}), outbox_id))
    return {"outbox_id": outbox_id, "disposition": disposition}
