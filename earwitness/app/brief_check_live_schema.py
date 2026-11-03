"""LIVE-BRIEF-CHECK (#7): the live brief check's run table
(ws-meta/work/WIN13-FEASIBILITY.md, "New" and "Schema").

One row is one press of the check on one placement by one member: the
pinned brief version it sent, its counts, and its spend. Its checked ads
are model_brief_checks rows (brief_checks.insert_row) with the run's id;
this table holds no brief, transcript or model text.

- state: working, done or failed. A restart marks a working run failed
  (RESTART) and moves its open spend into its spend.
- requirements_sha256: the placement requirements the run sent, hashed, so a
  later edit of them is known (they are not part of a row's pins).
- usd_open: the bounds of calls reserved and not yet settled; usd_spent:
  settled costs, plus the bound of any call whose usage is unknown. The
  per-member day limit counts both. No spend column is ever negative;
  usd_settled is NULL when any call's usage is unknown.
- The partial unique index is the per-placement lock: at most one working
  run per campaign placement.

Created with model_brief_checks by ensure_schema, the one call the host's
STOP step runs (additive; SCHEMA_VERSION unchanged; the workspace schema
digest changes, so the layout's workspace pin moves in the same window).
A fresh workspace does not have it, as it does not have model_brief_checks.
"""
from __future__ import annotations

import sqlite3

from . import brief_checks as _brief_checks
from . import db as _db

TABLE = "brief_check_runs"
STATES = ("working", "done", "failed")

SCHEMA = (
    f"CREATE TABLE IF NOT EXISTS {TABLE} ("
    " id TEXT PRIMARY KEY NOT NULL,"
    " workspace_id TEXT NOT NULL DEFAULT 'local',"
    " member TEXT NOT NULL,"
    " campaign_id INTEGER NOT NULL, placement_id TEXT NOT NULL,"
    " brief_id TEXT NOT NULL, brief_version INTEGER NOT NULL, brief_digest TEXT NOT NULL,"
    " requirements_sha256 TEXT,"
    " state TEXT NOT NULL CHECK (state IN ('working', 'done', 'failed')),"
    " error_code TEXT,"
    " ads INTEGER NOT NULL DEFAULT 0, sent INTEGER NOT NULL DEFAULT 0,"
    " checked INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,"
    " over_cap INTEGER NOT NULL DEFAULT 0, calls INTEGER NOT NULL DEFAULT 0,"
    " usd_open REAL NOT NULL DEFAULT 0 CHECK (usd_open >= 0),"
    " usd_spent REAL NOT NULL DEFAULT 0 CHECK (usd_spent >= 0),"
    " usd_settled REAL CHECK (usd_settled IS NULL OR usd_settled >= 0),"
    " model TEXT NOT NULL DEFAULT '',"
    " rights_level TEXT NOT NULL, record_revision TEXT NOT NULL,"
    " addendum_revision TEXT NOT NULL,"
    " created_at TEXT NOT NULL, finished_at TEXT)",
    f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{TABLE}_working ON {TABLE}"
    " (campaign_id, placement_id) WHERE state='working'",
    f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_member ON {TABLE} (member, created_at)",
)


def ensure_schema(con: sqlite3.Connection) -> None:
    """model_brief_checks (brief_checks.ensure_brief_check_schema), then
    brief_check_runs. The host's STOP step runs this one call in place of
    ensure_brief_check_schema; it runs no other DDL."""
    _brief_checks.ensure_brief_check_schema(con)
    for statement in SCHEMA:
        con.execute(statement)
    # Last: a new or rebuilt guarded table gets its view-state guards, or
    # view_state_key stays None and no research read is reused (UPD3-SLOW).
    _db.ensure_view_state_guards(con)


def present(con: sqlite3.Connection) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (TABLE,)).fetchone() is not None
