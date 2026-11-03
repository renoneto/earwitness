"""Full-text index over the segments of active transcripts (validation plan A3).

Mention and episode search read candidate segments from an FTS5 trigram
index over folded segment text (customer.normalize_token) and verify every
candidate with the whole-word rule (customer.word_in), so results stay
exact. A transcript version is indexed once (its payload is immutable by
id); a trigger drops the index entries of a version whose payload row is
rewritten or deleted, and search scans such a version directly until the
next load re-indexes it. Workspaces or versions without an index entry take
the direct scan, so nothing depends on the index being present.

Derived data only: the tables are in db.VIEW_STATE_UNREAD (bulk, rebuilt
from source_versions) and the research view never reads them.
"""
from __future__ import annotations

import json
import sqlite3

SEGMENTS = "transcript_segments"
VERSIONS = "transcript_index_versions"
FTS = "transcript_segments_fts"
TRIGGER_UPDATE = "ew_transcript_index_stale_update"
TRIGGER_DELETE = "ew_transcript_index_stale_delete"


def ensure_schema(con: sqlite3.Connection) -> bool:
    """Create the index tables; False when FTS5 trigram is unavailable
    (search then always scans directly)."""
    con.execute(f"CREATE TABLE IF NOT EXISTS {SEGMENTS} ("
                " id INTEGER PRIMARY KEY,"
                " version_id INTEGER NOT NULL,"
                " seg_index INTEGER NOT NULL,"
                " start_s REAL, end_s REAL,"
                " wref TEXT NOT NULL DEFAULT '',"
                " folded TEXT NOT NULL DEFAULT '',"
                " UNIQUE (version_id, seg_index))")
    con.execute(f"CREATE TABLE IF NOT EXISTS {VERSIONS} ("
                " version_id INTEGER PRIMARY KEY,"
                " segments INTEGER NOT NULL)")
    con.execute(f"CREATE TRIGGER IF NOT EXISTS {TRIGGER_UPDATE} AFTER UPDATE OF payload"
                f" ON source_versions BEGIN DELETE FROM {VERSIONS} WHERE version_id=OLD.id;"
                " END")
    con.execute(f"CREATE TRIGGER IF NOT EXISTS {TRIGGER_DELETE} AFTER DELETE"
                f" ON source_versions BEGIN DELETE FROM {VERSIONS} WHERE version_id=OLD.id;"
                " END")
    try:
        con.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS} USING fts5("
                    f"folded, content='{SEGMENTS}', content_rowid='id',"
                    " tokenize='trigram case_sensitive 1')")
    except sqlite3.OperationalError:
        return False
    return True


def available(con: sqlite3.Connection) -> bool:
    names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE name IN (?,?,?)", (SEGMENTS, VERSIONS, FTS))}
    return len(names) == 3


def window_ref(start, end) -> str:
    """The interval suffix customer._interval_ref_for_item appends to an
    episode publication ref for one segment ('' when it appends none)."""
    import math
    if not (isinstance(start, (int, float)) and isinstance(end, (int, float))) \
            or isinstance(start, bool) or isinstance(end, bool):
        return ""
    if not (math.isfinite(start) and math.isfinite(end)) or not (end > start >= 0):
        return ""
    return f":w:{start}:{end}"


def index_versions(con: sqlite3.Connection, version_ids) -> dict:
    """Index the given transcript versions that are not indexed yet and
    drop index entries of versions no episode pins any more. Joins the
    caller's transaction."""
    from .customer import normalize_token
    if not available(con) and not ensure_schema(con):
        return {"indexed": 0, "segments": 0, "dropped": 0}
    wanted = {int(v) for v in version_ids if v is not None}
    done = {r[0] for r in con.execute(f"SELECT version_id FROM {VERSIONS}")}
    pinned = {r[0] for r in con.execute(
        "SELECT DISTINCT transcript_version_id FROM episodes WHERE transcript_version_id IS NOT NULL")}
    # New rows take ids above every row present now, dropped ones included,
    # so a re-indexed version never gets its old ids back and its index
    # fingerprint (agent_contracts._transcript_fingerprints) changes.
    floor = con.execute(f"SELECT COALESCE(MAX(id), 0) FROM {SEGMENTS}").fetchone()[0] + 1
    # Rows of versions no longer indexed (payload rewritten, or unpinned).
    stale = [r[0] for r in con.execute(
        f"SELECT DISTINCT version_id FROM {SEGMENTS}")
        if r[0] not in done or r[0] not in pinned]
    dropped = 0
    for version in stale:
        con.execute(f"INSERT INTO {FTS} ({FTS}, rowid, folded) SELECT 'delete', id, folded"
                    f" FROM {SEGMENTS} WHERE version_id=?", (version,))
        con.execute(f"DELETE FROM {SEGMENTS} WHERE version_id=?", (version,))
        con.execute(f"DELETE FROM {VERSIONS} WHERE version_id=?", (version,))
        dropped += 1
    total = 0
    count = 0
    for version in sorted(wanted - (done - set(stale))):
        row = con.execute("SELECT payload FROM source_versions WHERE id=?", (version,)).fetchone()
        if row is None:
            continue
        try:
            payload = json.loads(row["payload"])
        except (ValueError, TypeError):
            continue
        segments = payload.get("segments") if isinstance(payload, dict) else None
        if not isinstance(segments, list):
            continue
        rows = []
        for idx, seg in enumerate(segments):
            if not isinstance(seg, dict):
                continue
            text = seg.get("text", "") or ""
            if not isinstance(text, str):
                text = ""
            start, end = seg.get("start"), seg.get("end")
            rows.append((version, idx,
                         float(start) if isinstance(start, (int, float)) and not isinstance(start, bool) else None,
                         float(end) if isinstance(end, (int, float)) and not isinstance(end, bool) else None,
                         window_ref(start, end), normalize_token(text)))
        first = max(floor, con.execute(
            f"SELECT COALESCE(MAX(id), 0) FROM {SEGMENTS}").fetchone()[0] + 1)
        con.executemany(f"INSERT INTO {SEGMENTS} (id, version_id, seg_index, start_s, end_s, wref,"
                        " folded) VALUES (?,?,?,?,?,?,?)",
                        [(first + i, *r) for i, r in enumerate(rows)])
        con.execute(f"INSERT INTO {FTS} (rowid, folded) SELECT id, folded FROM {SEGMENTS}"
                    " WHERE version_id=?", (version,))
        con.execute(f"INSERT OR REPLACE INTO {VERSIONS} (version_id, segments) VALUES (?,?)",
                    (version, len(rows)))
        total += len(rows)
        count += 1
    return {"indexed": count, "segments": total, "dropped": dropped}


def indexed(con: sqlite3.Connection, version_ids) -> dict[int, int]:
    """version id -> segment count, for the given versions that are indexed."""
    if not available(con):
        return {}
    ids = sorted({int(v) for v in version_ids if v is not None})
    if not ids:
        return {}
    return {r[0]: r[1] for r in con.execute(
        f"SELECT version_id, segments FROM {VERSIONS} WHERE version_id IN"
        " (SELECT value FROM json_each(?))", (json.dumps(ids),))}


def _match_expr(folded_tokens: list[str]) -> str | None:
    """FTS5 trigram expression that every match satisfies (a superset):
    each folded token of 3+ characters as a phrase. None when no token is
    long enough to use the index."""
    long_tokens = [t for t in folded_tokens if len(t) >= 3]
    if not long_tokens:
        return None
    return " AND ".join('"' + t.replace('"', '""') + '"' for t in long_tokens)


def candidate_segments(con: sqlite3.Connection, version_ids, folded_tokens: list[str]):
    """(version_id, seg_index, start_s, end_s, wref, folded) of indexed segments that
    may contain every token, or None when the index cannot narrow the
    search (no token of 3+ characters). The caller verifies each candidate
    against the segment text."""
    expr = _match_expr(folded_tokens)
    if expr is None:
        return None
    ids = sorted({int(v) for v in version_ids if v is not None})
    if not ids:
        return []
    return con.execute(
        f"SELECT s.version_id, s.seg_index, s.start_s, s.end_s, s.wref, s.folded FROM {FTS} f"
        f" JOIN {SEGMENTS} s ON s.id=f.rowid WHERE {FTS} MATCH ?"
        " AND s.version_id IN (SELECT value FROM json_each(?))"
        " ORDER BY s.version_id, s.seg_index", (expr, json.dumps(ids))).fetchall()


def segment_rows(con: sqlite3.Connection, version_id: int, offset: int, limit: int):
    """Indexed segments of one version ordered by their interval ref."""
    return con.execute(f"SELECT version_id, seg_index, start_s, end_s, wref FROM {SEGMENTS}"
                       " WHERE version_id=? ORDER BY wref, seg_index LIMIT ? OFFSET ?",
                       (version_id, limit, offset)).fetchall()
