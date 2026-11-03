"""Meaning alerts: the three workspace tables and takedown (design
docs/plans/2026-10-26-meaning-alerts.md section 2).

Word alerts keep the Query2 tracker tables (customer_alerts); meaning
alerts never write them. A saved alert is a sentence and its search words;
a run is one terms call, one preview or one Check now; a match is one
passage the model judged `match` or `uncertain` (a `no` is counted on the
run, never stored). A member keeps only the latest preview: earlier ones
lose their matches at once, and their rows (which hold no member text) go
once their Los Angeles day has passed (meaning.start_preview).

Takedown (base design 8.4): an episode that leaves the load takes every
match citing it, in the caller's transaction (delete_citing). A run whose
matches all went reads "These results are no longer available".
"""
from __future__ import annotations

import json
import sqlite3

TABLES = ("meaning_alerts", "meaning_runs", "meaning_matches")
LIFECYCLES = ("active", "paused", "stopped")
RUN_KINDS = ("preview", "check", "terms", "ask_tone")
RUN_STATES = ("working", "done", "failed")
VERDICTS = ("match", "uncertain")

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meaning_alerts ("
    " id TEXT PRIMARY KEY, created_by TEXT NOT NULL,"
    " sentence TEXT NOT NULL CHECK (length(sentence) BETWEEN 1 AND 200),"
    " terms_json TEXT NOT NULL CHECK (json_valid(terms_json)),"
    " shows_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(shows_json)),"
    " brand TEXT,"
    " lifecycle TEXT NOT NULL DEFAULT 'active'"
    " CHECK (lifecycle IN ('active', 'paused', 'stopped')),"
    " check_cursor TEXT, rights_level TEXT NOT NULL, record_revision TEXT NOT NULL,"
    " addendum_revision TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,"
    " created_at TEXT NOT NULL, last_checked_at TEXT)",
    "CREATE TABLE IF NOT EXISTS meaning_runs ("
    " id TEXT PRIMARY KEY, alert_id TEXT, member TEXT NOT NULL,"
    " kind TEXT NOT NULL CHECK (kind IN ('preview', 'check', 'terms', 'ask_tone')),"
    " state TEXT NOT NULL CHECK (state IN ('working', 'done', 'failed')),"
    " superseded INTEGER NOT NULL DEFAULT 0,"
    " candidates INTEGER NOT NULL DEFAULT 0, checked INTEGER NOT NULL DEFAULT 0,"
    " not_checked INTEGER NOT NULL DEFAULT 0, over_cap INTEGER NOT NULL DEFAULT 0,"
    " flagged INTEGER NOT NULL DEFAULT 0, uncertain INTEGER NOT NULL DEFAULT 0,"
    " batches INTEGER NOT NULL DEFAULT 0,"
    " usd_open REAL NOT NULL DEFAULT 0, usd_spent REAL NOT NULL DEFAULT 0, usd_settled REAL,"
    " model TEXT NOT NULL DEFAULT '', error_code TEXT,"
    " window_from TEXT, window_to TEXT, window_episodes INTEGER NOT NULL DEFAULT 0,"
    " cursor_to TEXT, definition_sha TEXT,"
    " rights_level TEXT NOT NULL, record_revision TEXT NOT NULL,"
    " addendum_revision TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT)",
    "CREATE INDEX IF NOT EXISTS idx_meaning_runs_alert ON meaning_runs (alert_id)",
    "CREATE INDEX IF NOT EXISTS idx_meaning_runs_member ON meaning_runs (member, created_at)",
    "CREATE TABLE IF NOT EXISTS meaning_matches ("
    " id TEXT PRIMARY KEY, run_id TEXT NOT NULL, alert_id TEXT, pid TEXT NOT NULL,"
    " episode_id TEXT NOT NULL, version_id TEXT, evidence_id TEXT,"
    " start_s REAL NOT NULL, end_s REAL NOT NULL,"
    " kind TEXT NOT NULL CHECK (kind IN ('transcript_passage', 'detected_ad')),"
    " in_ad INTEGER NOT NULL DEFAULT 0, held INTEGER NOT NULL DEFAULT 0,"
    " brands_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(brands_json)),"
    " show TEXT NOT NULL, episode_title TEXT NOT NULL, pub_date TEXT NOT NULL,"
    " fetch_date TEXT NOT NULL, fetch_source TEXT NOT NULL,"
    " verdict TEXT NOT NULL CHECK (verdict IN ('match', 'uncertain')),"
    " reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 140),"
    " passage_text TEXT NOT NULL CHECK (length(CAST(passage_text AS BLOB)) <= 1024),"
    " UNIQUE (run_id, pid))",
    "CREATE INDEX IF NOT EXISTS idx_meaning_matches_episode ON meaning_matches (episode_id)",
    "CREATE INDEX IF NOT EXISTS idx_meaning_matches_alert ON meaning_matches (alert_id)",
)


def ensure_schema(con: sqlite3.Connection) -> None:
    """Create the meaning-alert tables (additive; part of the pinned
    workspace schema)."""
    for statement in SCHEMA:
        con.execute(statement)
    # Last: a new or rebuilt guarded table gets its view-state guards, or
    # view_state_key stays None and no research read is reused (UPD3-SLOW).
    from . import db as _db
    _db.ensure_view_state_guards(con)


def present(con: sqlite3.Connection) -> bool:
    return con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN"
                       " (?, ?, ?)", TABLES).fetchone()[0] == len(TABLES)


def delete_citing(con: sqlite3.Connection, episode_ids) -> int:
    """Takedown, in the caller's transaction: delete every meaning match
    that cites one of `episode_ids`. Returns the number deleted; 0 when the
    list is empty or the workspace has no meaning_matches table (a backup
    made before it)."""
    ids = sorted({str(e) for e in episode_ids})
    if not ids or con.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                              " AND name='meaning_matches'").fetchone() is None:
        return 0
    return con.execute("DELETE FROM meaning_matches WHERE episode_id IN"
                       " (SELECT value FROM json_each(?))", (json.dumps(ids),)).rowcount
