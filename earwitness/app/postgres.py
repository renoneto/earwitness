"""Hosted PostgreSQL persistence: migrations, rehearsal, RLS, parity.

Package E (hosted code only; no live deploy). Migration definitions are
kept in-file; no migration framework is introduced. Two parts:

1. HOSTED_DDL — new hosted relations (workspaces, memberships, grants,
   consent, revocation ledger, refresh reuse, audit, idempotency, export
   manifests) with composite keys, composite foreign keys, and
   row-level security on a non-bypass runtime role.
2. migrate_core() — one-way copy of one single-workspace SQLite source
   into an empty Postgres database: copy every application table, put
   every row in the target workspace_id (stamped, or remapped when the
   table already carries the column), keep the source's constraints
   workspace-scoped (primary keys, uniques, NOT NULL, defaults, CHECK,
   rowid identity past the source high-water mark), add every foreign
   key as a validated composite (workspace_id, key) reference, enable
   RLS, then reconcile counts, per-FK orphan counts and row digests per
   table. schema_check compares the catalog with the source.
   scripts/app_hosting.py migrate-postgres wraps it (dry run, apply).

Text parity note: transcript/segment text is preserved byte-exact in
UTF-8; all literal folding and Unicode offset math stays in Python
(identical semantics on both backends). Ordering parity uses
COLLATE "C" (byte order, matching SQLite BINARY for text keys); the
application never relies on database case-insensitive matching.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

HOSTED_DDL = """
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memberships (
    workspace_id TEXT NOT NULL REFERENCES workspaces (id),
    principal_id TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    PRIMARY KEY (workspace_id, principal_id)
);
CREATE TABLE IF NOT EXISTS connection_grants (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    member_role TEXT NOT NULL,
    client_id TEXT NOT NULL,
    scopes TEXT NOT NULL,
    content_policy TEXT NOT NULL DEFAULT '{}',
    policy_rev INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    last_used_at TEXT,
    access_hash TEXT,
    refresh_hash TEXT,
    refresh_created_at TEXT,
    refresh_rotated_at TEXT,
    FOREIGN KEY (workspace_id, member_id)
        REFERENCES memberships (workspace_id, principal_id)
);
CREATE TABLE IF NOT EXISTS consent_records (
    grant_id TEXT PRIMARY KEY REFERENCES connection_grants (id),
    scopes TEXT NOT NULL,
    content_categories TEXT NOT NULL,
    policy_rev INTEGER NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revocation_ledger (
    seq BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    grant_id TEXT NOT NULL,
    revoked_at TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS used_refresh_hashes (
    hash TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL REFERENCES connection_grants (id),
    used_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_audit (
    seq BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    request_id TEXT NOT NULL,
    grant_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    object_ids TEXT NOT NULL DEFAULT '[]',
    outcome TEXT NOT NULL,
    latency_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
    bytes_out BIGINT NOT NULL DEFAULT 0,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    workspace_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    key TEXT NOT NULL,
    result TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, principal_id, connection_id, operation, key)
);
CREATE TABLE IF NOT EXISTS export_manifests (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    requester TEXT NOT NULL,
    grant_id TEXT NOT NULL,
    source_pins TEXT NOT NULL,
    field_policy TEXT NOT NULL,
    format TEXT NOT NULL,
    membership TEXT NOT NULL,
    byte_limit BIGINT NOT NULL,
    state TEXT NOT NULL,
    object_ref TEXT,
    expires_at TEXT NOT NULL
);
"""

# Schema v8 mirror (K01, DDL only — no connection/provisioning here).
# Same retained B1–B5 resources as the SQLite _SCHEMA_V8 pin index, with
# composite workspace keys and tenant RLS predicates below.
V8_HOSTED_DDL = """
CREATE TABLE IF NOT EXISTS source_states (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'local',
    kind TEXT NOT NULL CHECK (kind IN ('evidence','episode')),
    record_id TEXT NOT NULL,
    publication_order BIGINT NOT NULL,
    pins_json TEXT NOT NULL,
    bounds_json TEXT NOT NULL,
    projection_digest TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    UNIQUE (workspace_id, kind, record_id, publication_order)
);
CREATE TABLE IF NOT EXISTS customer_saved_items (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'local',
    type TEXT NOT NULL CHECK (type IN ('excerpt','query','bookmark')),
    title TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    revision BIGINT NOT NULL DEFAULT 1,
    target_json TEXT NOT NULL DEFAULT '{}',
    query_json TEXT NOT NULL DEFAULT '{}',
    selector_json TEXT,
    captured_json TEXT NOT NULL DEFAULT '{}',
    saved_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provided_briefs (
    brief_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'local',
    head_revision BIGINT NOT NULL DEFAULT 1,
    current_version BIGINT NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provided_brief_versions (
    workspace_id TEXT NOT NULL DEFAULT 'local',
    brief_id TEXT NOT NULL REFERENCES provided_briefs (brief_id),
    version BIGINT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    provided_text TEXT NOT NULL DEFAULT '',
    digest TEXT NOT NULL DEFAULT '',
    captured_at TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (workspace_id, brief_id, version)
);
CREATE TABLE IF NOT EXISTS campaign_targets (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'local',
    campaign_id BIGINT NOT NULL REFERENCES campaigns (id),
    scope TEXT NOT NULL CHECK (scope IN ('episode','show_window')),
    show_id TEXT,
    episode_id TEXT REFERENCES episodes (id),
    date_from TEXT,
    date_to TEXT,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    note TEXT NOT NULL DEFAULT '',
    source_scope_json TEXT NOT NULL DEFAULT '{}',
    revision BIGINT NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS target_evidence (
    workspace_id TEXT NOT NULL DEFAULT 'local',
    target_id TEXT NOT NULL REFERENCES campaign_targets (id),
    ref TEXT NOT NULL,
    attached_at TEXT NOT NULL,
    attached_by TEXT NOT NULL DEFAULT 'owner',
    PRIMARY KEY (workspace_id, target_id, ref)
);
CREATE TABLE IF NOT EXISTS control_receipts (
    seq BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'local',
    credential_kind TEXT NOT NULL DEFAULT '',
    principal TEXT NOT NULL DEFAULT '',
    operation TEXT NOT NULL DEFAULT '',
    policy_digest TEXT NOT NULL DEFAULT '',
    grant_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS customer_conversion_lineage (
    search_id BIGINT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'local',
    action TEXT NOT NULL CHECK (action IN ('converted','dropped')),
    item_id TEXT,
    original_json TEXT NOT NULL DEFAULT '{}',
    notes_json TEXT NOT NULL DEFAULT '[]',
    actor TEXT NOT NULL DEFAULT '',
    converted_at TEXT NOT NULL
);
"""

V8_TABLES = (
    "source_states", "customer_saved_items", "provided_briefs",
    "provided_brief_versions", "campaign_targets", "target_evidence",
    "control_receipts", "customer_conversion_lineage",
)

# Agent-critical relations gain composite (workspace_id, id) uniqueness
# plus composite foreign keys after the workspace stamp.
COMPOSITE_UNIQUES = (
    ("episodes", "id"),
    ("evidence", "id"),
    ("campaigns", "id"),
    ("watchlists", "id"),
    ("source_versions", "id"),
    ("source_states", "id"),
    ("customer_saved_items", "id"),
    ("provided_briefs", "brief_id"),
    ("campaign_targets", "id"),
)
COMPOSITE_FKS = (
    ("evidence", "episode_id", "episodes", "id"),
    ("reviews", "evidence_id", "evidence", "id"),
    ("brand_assocs", "evidence_id", "evidence", "id"),
    ("campaign_evidence", "campaign_id", "campaigns", "id"),
    ("campaign_evidence", "evidence_id", "evidence", "id"),
    ("campaign_checks", "campaign_id", "campaigns", "id"),
    ("provided_brief_versions", "brief_id", "provided_briefs", "brief_id"),
    ("campaign_targets", "campaign_id", "campaigns", "id"),
    ("campaign_targets", "episode_id", "episodes", "id"),
    ("target_evidence", "target_id", "campaign_targets", "id"),
)

# Agent-critical tables the reconcile must cover. Every copied table is
# digested (see _reconcile); this list is the required minimum.
HASHED_TABLES = ("episodes", "evidence", "reviews", "brand_assocs",
                 "source_versions", "campaigns", "campaign_checks",
                 "watchlists", "exports", "source_states",
                 "customer_saved_items", "provided_briefs",
                 "provided_brief_versions", "campaign_targets",
                 "target_evidence", "customer_conversion_lineage")


def rehearse_v8() -> dict:
    """Offline v8 DDL/rehearsal inventory (K01). No connection, no
    provisioning: parses the mirrored DDL, lists tenant constraints and
    hash coverage for review without touching any database."""
    statements = [s.strip() for s in V8_HOSTED_DDL.split(";") if s.strip()]
    return {"v8_statements": len(statements),
            "v8_tables": list(V8_TABLES),
            "composite_uniques": [list(k) for k in COMPOSITE_UNIQUES],
            "composite_fks": [list(k) for k in COMPOSITE_FKS],
            "hashed_tables": list(HASHED_TABLES),
            "live_connection": False}

_TYPE_MAP = {"INTEGER": "BIGINT", "REAL": "DOUBLE PRECISION",
             "TEXT": "TEXT", "BLOB": "BYTEA", "NUMERIC": "NUMERIC"}


def _pg_columns(sqlite_con: sqlite3.Connection, table: str) -> list:
    cols = []
    for row in sqlite_con.execute(f"PRAGMA table_info({table})"):
        _cid, name, ctype, _notnull, _default, pk = row
        base = (ctype or "TEXT").upper().split("(")[0].strip()
        pgtype = _TYPE_MAP.get(base, "TEXT")
        cols.append((name, pgtype, bool(pk)))
    return cols


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _cols(cols) -> str:
    """Column list; workspace_id unquoted, the composite key form AD-07
    pins: (workspace_id, "key")."""
    return ", ".join("workspace_id" if c == "workspace_id" else f'"{c}"'
                     for c in cols)


def _scope(cols) -> list:
    """Destination key: workspace_id first, then the source key (as is
    when the source key already holds workspace_id)."""
    cols = list(cols)
    return cols if "workspace_id" in cols else ["workspace_id"] + cols


def _name(table: str, cols, suffix: str) -> str:
    """Constraint name within Postgres' 63-byte identifier limit."""
    name = "_".join([table, *cols, suffix])
    if len(name) <= 63:
        return name
    digest = hashlib.sha256(name.encode()).hexdigest()[:8]
    return f"{name[:62 - len(suffix) - 9]}_{digest}_{suffix}"


_NUMBER = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")


def _pg_default(raw: str, pgtype: str):
    """Postgres DEFAULT for a SQLite literal default: '' for NULL (no
    default needed), None when there is no safe equivalent (listed in
    schema_check's not_carried, never dropped silently)."""
    text = raw.strip()
    while text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    upper = text.upper()
    if upper == "NULL":
        return ""
    if upper in ("TRUE", "FALSE"):
        text = "1" if upper == "TRUE" else "0"
    if pgtype == "BYTEA":
        match = re.fullmatch(r"[xX]'([0-9a-fA-F]*)'", text)
        return f"'\\x{match.group(1)}'::bytea" if match else None
    if len(text) >= 2 and text[0] == text[-1] == "'":
        value = text[1:-1].replace("''", "'")
        if pgtype == "TEXT":
            return _quote(value)
        text = value.strip()  # numeric affinity converts numeric text
    if not _NUMBER.fullmatch(text):
        return None
    if pgtype == "TEXT":
        return _quote(text)
    if pgtype == "BIGINT":
        if re.fullmatch(r"[+-]?\d+", text):
            return str(int(text))
        number = float(text)
        return str(int(number)) if number.is_integer() else None
    return text


def _without_comments(sql: str) -> str:
    """The statement with -- and /* */ comments blanked, string literals
    and quoted identifiers kept as written."""
    out, i, n = [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in "'\"`[":
            end = sql.find("]" if ch == "[" else ch, i + 1)
            end = n if end < 0 else end + 1
            out.append(sql[i:end])
            i = end
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end < 0 else end
            out.append(" ")
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end < 0 else end + 2
            out.append(" ")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _definitions(sql: str) -> list:
    """Top-level comma-separated parts of a CREATE TABLE body (comments
    already removed)."""
    parts, depth, current, i, n = [], 0, [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in "'\"`[":
            end = sql.find("]" if ch == "[" else ch, i + 1)
            end = n if end < 0 else end + 1
            if depth:
                current.append(sql[i:end])
            i = end
            continue
        if ch == "(":
            depth += 1
            if depth == 1:
                i += 1
                continue
        elif ch == ")":
            depth -= 1
            if depth == 0:
                parts.append("".join(current))
                break
        if depth == 1 and ch == ",":
            parts.append("".join(current))
            current = []
        elif depth:
            current.append(ch)
        i += 1
    return [p.strip() for p in parts if p.strip()]


_DEFERRED = re.compile(r"(\bNOT\s+)?\bDEFERRABLE\s+INITIALLY\s+DEFERRED\b",
                       re.I)


def _deferred_fk_columns(sql: str) -> list:
    """Child columns of each DEFERRABLE INITIALLY DEFERRED foreign key
    (PRAGMA foreign_key_list does not report deferral)."""
    found = []
    for part in _definitions(_without_comments(sql)):
        match = _DEFERRED.search(part)
        if not match or match.group(1) or not re.search(
                r"\bREFERENCES\b", part, re.I):
            continue
        table_fk = re.match(r"(CONSTRAINT\s+\S+\s+)?FOREIGN\s+KEY\s*\(([^)]*)\)",
                            part, re.I)
        names = table_fk.group(2).split(",") if table_fk else [part.split()[0]]
        found.append([n.strip().strip('"`[]\'') for n in names])
    return found


def _check_clauses(sql: str) -> list:
    """CHECK (...) expressions of a CREATE TABLE statement, skipping
    comments, string literals and quoted identifiers."""
    sql = _without_comments(sql)
    out, i, n = [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in "'\"`[":
            end = sql.find("]" if ch == "[" else ch, i + 1)
            i = n if end < 0 else end + 1
            continue
        if (sql[i:i + 5].upper() == "CHECK"
                and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_"))):
            j = i + 5
            while j < n and sql[j].isspace():
                j += 1
            if j < n and sql[j] == "(":
                depth, k = 0, j
                while k < n:
                    c = sql[k]
                    if c in "'\"":
                        end = sql.find(c, k + 1)
                        k = n if end < 0 else end + 1
                        continue
                    depth += (c == "(") - (c == ")")
                    if depth == 0:
                        break
                    k += 1
                out.append(sql[j + 1:k].strip())
                i = k + 1
                continue
        i += 1
    return out


_FK_ACTIONS = ("NO ACTION", "RESTRICT", "CASCADE")


def _table_shape(lite: sqlite3.Connection, table: str) -> dict:
    """What SQLite enforces for one table, in destination terms: the
    workspace-scoped primary key and uniques, NOT NULL, translated
    defaults, rowid identity with its high-water mark, foreign keys and
    CHECK expressions. Unsupported items go to not_carried (defaults,
    foreign-key actions) or refused (unique indexes preflight refuses)."""
    row = lite.execute("SELECT sql FROM sqlite_master WHERE type = 'table'"
                       " AND name = ?", (table,)).fetchone()
    sql = (row[0] if row else "") or ""
    info = lite.execute(f'PRAGMA table_info("{table}")').fetchall()
    pk = [r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5])]
    shape = {"columns": [], "primary_key": _scope(pk) if pk else [],
             "source_key": pk, "unique": [], "identity": {},
             "foreign_keys": [], "checks": _check_clauses(sql),
             "not_carried": [], "refused": []}
    rowid = (len(pk) == 1 and "WITHOUT ROWID" not in sql.upper() and any(
        r[1] == pk[0] and (r[2] or "").upper() == "INTEGER" for r in info))
    if rowid:
        high = lite.execute(f'SELECT MAX("{pk[0]}") FROM "{table}"'
                            ).fetchone()[0] or 0
        if re.search(r"\bAUTOINCREMENT\b", sql, re.I) and lite.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'"
                ).fetchone():
            seq = lite.execute("SELECT seq FROM sqlite_sequence WHERE name"
                               " = ?", (table,)).fetchone()
            high = max(high, seq[0] if seq else 0)
        shape["identity"][pk[0]] = max(int(high), 0)
    types = {n: t for n, t, _p in _pg_columns(lite, table)}
    for _cid, name, _ctype, notnull, dflt, _pk in info:
        pgtype = types[name]
        default = ""
        if name != "workspace_id" and dflt is not None:
            default = _pg_default(dflt, pgtype)
            if default is None:
                shape["not_carried"].append(
                    {"table": table, "column": name, "kind": "default",
                     "source": dflt})
                default = ""
        shape["columns"].append({"name": name, "type": pgtype,
                                 "not_null": bool(notnull),
                                 "default": default})
    for _seq, index, unique, origin, partial in lite.execute(
            f'PRAGMA index_list("{table}")'):
        if not unique:
            continue
        keys = [r for r in lite.execute(f'PRAGMA index_xinfo("{index}")')
                if r[5]]
        cols = [r[2] for r in keys]
        collations = sorted({r[4] for r in keys
                             if (r[4] or "BINARY").upper() != "BINARY"})
        if collations:
            shape["refused"].append(f"{index} (COLLATE {', '.join(collations)})")
        elif origin == "pk":
            continue
        elif partial or None in cols:
            shape["refused"].append(
                f"{index} ({'partial' if partial else 'expression'})")
        else:
            shape["unique"].append(_scope(cols))
    grouped: dict = {}
    for fid, _seq, ref, frm, to, on_update, on_delete, _match in lite.execute(
            f'PRAGMA foreign_key_list("{table}")'):
        entry = grouped.setdefault(fid, {"ref": ref, "cols": [], "ref_cols": [],
                                         "actions": ""})
        entry["cols"].append(frm)
        entry["ref_cols"].append(to)
        for verb, action in (("DELETE", on_delete), ("UPDATE", on_update)):
            if _seq == 0 and action != "NO ACTION":
                if action in _FK_ACTIONS:
                    entry["actions"] += f" ON {verb} {action}"
                else:
                    shape["not_carried"].append(
                        {"table": table, "column": frm,
                         "kind": "foreign_key_action",
                         "source": f"ON {verb} {action}"})
    for entry in grouped.values():
        if None in entry["ref_cols"]:  # REFERENCES parent: its primary key
            info_sql = 'PRAGMA table_info("%s")' % entry["ref"]
            parent = [r[1] for r in sorted(
                (r for r in lite.execute(info_sql) if r[5]),
                key=lambda r: r[5])]
            entry["ref_cols"] = (parent if len(parent) == len(entry["cols"])
                                 else ["rowid"] * len(entry["cols"]))
    shape["foreign_keys"] = list(grouped.values())
    shape["not_carried"] += [
        {"table": table, "column": ",".join(cols),
         "kind": "foreign_key_deferral",
         "source": "DEFERRABLE INITIALLY DEFERRED"}
        for cols in _deferred_fk_columns(sql)]
    return shape


def _foreign_keys(lite: sqlite3.Connection, tables: list) -> list:
    """Every foreign key the copy creates: the SQLite source's own plus
    COMPOSITE_FKS, workspace-scoped, one per (table, columns)."""
    copied = [t for t in tables if t not in HOSTED_TABLES]
    found: dict = {}
    for table in copied:
        for fk in _table_shape(lite, table)["foreign_keys"]:
            if fk["ref"] in copied:
                found.setdefault((table, tuple(fk["cols"])), {
                    "table": table, **fk})
    for table, col, ref, refcol in COMPOSITE_FKS:
        if (table in copied and ref in copied
                and col in _colnames(lite, table)
                and refcol in _colnames(lite, ref)):
            found.setdefault((table, (col,)), {
                "table": table, "ref": ref, "cols": [col],
                "ref_cols": [refcol], "actions": ""})
    out = []
    for fk in found.values():
        fk["name"] = _name(fk["table"], fk["cols"], "fk")
        fk["scoped"] = _scope(fk["cols"])
        fk["ref_scoped"] = (fk["ref_cols"] if "workspace_id" in fk["cols"]
                            else ["workspace_id"] + fk["ref_cols"])
        out.append(fk)
    return out


def _orphans(cur, fk: dict) -> int:
    present = " AND ".join(f'c."{c}" IS NOT NULL' for c in fk["scoped"])
    match = " AND ".join(f'r."{r}" = c."{c}"' for c, r in
                         zip(fk["scoped"], fk["ref_scoped"]))
    cur.execute(f'SELECT COUNT(*) FROM "{fk["table"]}" AS c WHERE {present}'
                f' AND NOT EXISTS (SELECT 1 FROM "{fk["ref"]}" AS r WHERE'
                f" {match})")
    return cur.fetchone()[0]


def _add_foreign_keys(cur, lite: sqlite3.Connection, tables: list) -> None:
    """Create every foreign key NOT VALID after the copy, each with the
    (workspace_id, key) unique target it needs, then VALIDATE each one
    without orphans. One with orphans stays NOT VALID; schema_check
    lists it (the source did not enforce it either)."""
    shapes = {t: _table_shape(lite, t) for t in tables
              if t not in HOSTED_TABLES}
    for fk in _foreign_keys(lite, tables):
        ref = shapes[fk["ref"]]
        if not _parent_key(ref, fk):
            continue  # parent key not unique in the source: schema_check
        unique = ("_".join([fk["ref"], "ws_unique"]) if set(fk["ref_cols"])
                  == set(ref["source_key"]) else
                  _name(fk["ref"], fk["ref_cols"], "ws_unique"))
        _add_constraint(cur, fk["ref"], unique,
                        f"UNIQUE ({_cols(fk['ref_scoped'])})")
        _add_constraint(
            cur, fk["table"], fk["name"],
            f"FOREIGN KEY ({_cols(fk['scoped'])}) REFERENCES"
            f' "{fk["ref"]}" ({_cols(fk["ref_scoped"])})'
            f"{fk['actions']} NOT VALID")
        if _orphans(cur, fk) == 0:
            cur.execute(f'ALTER TABLE "{fk["table"]}" VALIDATE CONSTRAINT'
                        f' "{fk["name"]}"')


def _parent_key(ref_shape: dict, fk: dict) -> bool:
    """The referenced columns are a key of the parent (PK or unique)."""
    keys = [ref_shape["primary_key"]] + ref_shape["unique"]
    return set(fk["ref_scoped"]) in [set(k) for k in keys]


def schema_check(sqlite_path: str, pg_conn) -> dict:
    """Compare every copied table's catalog with what its SQLite source
    enforces (_table_shape), in the caller's transaction.

    mismatches: schema:<table> when the primary key, a NOT NULL, a
    translated default, a unique key, a CHECK, an identity column (or its
    sequence not past the source high-water mark) or a foreign key is
    missing, or a foreign key without orphans is not validated.
    not_carried: what has no safe Postgres equivalent and is listed
    instead (untranslatable defaults, foreign-key actions, a foreign key
    whose parent columns are not a key, a foreign key left NOT VALID
    because the source already held orphans)."""
    lite = _open_source(sqlite_path)
    try:
        tables = [t for t in _source_tables(lite) if t not in HOSTED_TABLES]
        shapes = {t: _table_shape(lite, t) for t in tables}
        fks = _foreign_keys(lite, tables)
    finally:
        lite.close()
    report: dict = {}
    not_carried: list = []
    with pg_conn.cursor() as cur:
        cur.execute(f"SET LOCAL search_path = {PINNED_PATH}")
        for table in tables:
            shape = shapes[table]
            if not shape["columns"]:
                continue
            not_carried += shape["not_carried"]
            not_carried += [
                {"table": table, "column": ",".join(fk["cols"]),
                 "kind": "foreign_key", "source": f"{fk['ref']} is not copied"}
                for fk in shape["foreign_keys"] if fk["ref"] not in tables]
            problems, summary = _table_schema_problems(cur, table, shape)
            for fk in (f for f in fks if f["table"] == table):
                if not _parent_key(shapes[fk["ref"]], fk):
                    not_carried.append(
                        {"table": table, "column": ",".join(fk["cols"]),
                         "kind": "foreign_key", "source":
                         f"{fk['ref']}({','.join(fk['ref_cols'])}) is not a"
                         " key of the parent"})
                    continue
                cur.execute("SELECT convalidated FROM pg_constraint WHERE"
                            " conrelid = %s::regclass AND conname = %s AND"
                            " contype = 'f'", (f'public."{table}"', fk["name"]))
                row = cur.fetchone()
                if row is None:
                    problems.append(f"foreign key {fk['name']} missing")
                elif not row[0]:
                    orphans = _orphans(cur, fk)
                    if orphans:
                        not_carried.append(
                            {"table": table, "column": ",".join(fk["cols"]),
                             "kind": "foreign_key_not_validated",
                             "source": f"{orphans} rows without a parent"})
                    else:
                        problems.append(f"foreign key {fk['name']} not"
                                        " validated")
                else:
                    summary["foreign_keys"] += 1
            if problems:
                summary["problems"] = problems
            report[table] = summary
    mismatches = sorted(f"schema:{t}" for t, e in report.items()
                        if e.get("problems"))
    not_carried.sort(key=lambda e: (e["table"], e["column"], e["kind"]))
    return {"ok": not mismatches, "mismatches": mismatches,
            "not_carried": not_carried, "tables": report}


def _table_schema_problems(cur, table: str, shape: dict) -> tuple:
    """(problems, summary) for one table's columns, keys and checks."""
    oid = f'public."{table}"'
    cur.execute("SELECT a.attname, a.attnotnull, a.attidentity, d.adbin IS"
                " NOT NULL FROM pg_attribute a LEFT JOIN pg_attrdef d ON"
                " d.adrelid = a.attrelid AND d.adnum = a.attnum WHERE"
                " a.attrelid = %s::regclass AND a.attnum > 0 AND NOT"
                " a.attisdropped", (oid,))
    cols = {r[0]: r[1:] for r in cur.fetchall()}
    problems = []
    not_null = [c["name"] for c in shape["columns"] if c["not_null"]]
    defaults = [c["name"] for c in shape["columns"] if c["default"]]
    for name in sorted(set(not_null + ["workspace_id"])):
        if not cols.get(name, (False,))[0]:
            problems.append(f"{name} is nullable")
    for name in sorted(set(defaults + ["workspace_id"])):
        if not cols.get(name, (False, "", False))[2]:
            problems.append(f"{name} has no default")
    identity = {}
    for col, high in shape["identity"].items():
        if cols.get(col, (False, "", False))[1] != "d":
            problems.append(f"{col} is not an identity column")
            continue
        cur.execute("SELECT pg_get_serial_sequence(%s, %s)", (oid, col))
        cur.execute(f"SELECT last_value, is_called FROM {cur.fetchone()[0]}")
        last, called = cur.fetchone()
        identity[col] = {"high_water": high,
                         "next": last + 1 if called else last}
        if identity[col]["next"] <= high:
            problems.append(f"{col} next id {identity[col]['next']} is not"
                            f" past the source high-water mark {high}")
    cur.execute("SELECT c.contype, ARRAY(SELECT a.attname FROM unnest(c.conkey)"
                " WITH ORDINALITY AS k(num, ord) JOIN pg_attribute a ON"
                " a.attrelid = c.conrelid AND a.attnum = k.num ORDER BY"
                " k.ord) FROM pg_constraint c WHERE c.conrelid = %s::regclass",
                (oid,))
    constraints = cur.fetchall()
    pks = [list(k) for t, k in constraints if t == "p"]
    if pks != ([shape["primary_key"]] if shape["primary_key"] else []):
        problems.append(f"primary key {pks} differs from"
                        f" {shape['primary_key']}")
    keys = [set(k) for t, k in constraints if t in ("p", "u")]
    for cols_ in shape["unique"]:
        if set(cols_) not in keys:
            problems.append(f"unique {cols_} missing")
    checks = sum(1 for t, _k in constraints if t == "c")
    if checks != len(shape["checks"]):
        problems.append(f"{checks} of {len(shape['checks'])} CHECK"
                        " constraints carried")
    summary = {"primary_key": shape["primary_key"], "not_null": len(not_null),
               "defaults": len(defaults), "unique": shape["unique"],
               "checks": checks, "identity": identity, "foreign_keys": 0}
    return problems, summary


def _add_constraint(cur, table: str, name: str, definition: str) -> None:
    cur.execute("SELECT 1 FROM pg_constraint WHERE conname = %s", (name,))
    if cur.fetchone():
        return
    cur.execute(f'ALTER TABLE "{table}" ADD CONSTRAINT {name} {definition}')


def _setting() -> str:
    return "current_setting('app.workspace_id', true)"


def _policy_expr(table: str, stamped: dict) -> str:
    """Predicate the copy creates; runtime_check compares the created
    policy with _expected_policy, not with this function."""
    return _expected_policy(table)


def _expected_policy(table: str) -> str:
    """Row-level isolation predicate per table shape. Tables carrying
    their own workspace_id compare directly; the workspace row matches
    its own id; grant-linked tables resolve through connection_grants."""
    if table == "workspaces":
        return f"id = {_setting()}"
    if table in ("consent_records", "used_refresh_hashes",
                 "revocation_ledger"):
        key = "grant_id"
        return (f"EXISTS (SELECT 1 FROM connection_grants AS g WHERE g.id = "
                f'"{table}"."{key}" AND g.workspace_id = {_setting()})')
    return f"workspace_id = {_setting()}"


class MigrationRefused(Exception):
    """The source or destination is outside what migrate_core supports.

    Raised before any write to the destination."""


# Commit marker owned by scripts/app_hosting.py migrate-postgres. It is
# the only relation tolerated in an otherwise empty destination.
MARKER_TABLE = "ew_migration_state"

# Name resolution for the copy, the policy render and the runtime probe:
# built-ins first, so nothing in public can shadow an operator or
# function. Objects are therefore created as public.<name>.
PINNED_PATH = "pg_catalog, public"
_SYSTEM_NSP = ("n.nspname NOT IN ('pg_catalog', 'information_schema') AND"
               " n.nspname !~ '^pg_(toast|temp)'")

# Relations HOSTED_DDL creates. A SQLite table with the same name is not
# copied; a populated one is refused (no row mapping exists yet).
HOSTED_TABLES = tuple(
    stmt.split("(")[0].split()[-1]
    for stmt in HOSTED_DDL.split(";") if "CREATE TABLE" in stmt)


def _content_hash(rows: list) -> str:
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda r: json.dumps(
            r, ensure_ascii=False, sort_keys=True, default=str)):
        digest.update(json.dumps(row, ensure_ascii=False,
                                 sort_keys=True, default=str).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _open_source(sqlite_path: str) -> sqlite3.Connection:
    """Read-only SQLite handle holding one read snapshot (BEGIN)."""
    uri = Path(sqlite_path).resolve().as_uri() + "?mode=ro"
    lite = sqlite3.connect(uri, uri=True, isolation_level=None)
    lite.row_factory = sqlite3.Row
    lite.execute("BEGIN")
    return lite


def _source_tables(lite: sqlite3.Connection) -> list:
    return [r[0] for r in lite.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND "
        "name NOT LIKE 'sqlite_%' AND name != 'schema_version'")]


def _colnames(lite: sqlite3.Connection, table: str) -> list:
    return [n for n, _t, _p in _pg_columns(lite, table)]


# Internal SQLite full-text structures. Their untyped columns hold
# integers (and blobs) that change type in Postgres, so they are compared
# by row count only. The virtual tables and their _content tables carry
# the searchable text and are compared row for row like any other table.
_FTS_INTERNAL = ("_data", "_idx", "_docsize", "_config")


def _derived_tables(lite: sqlite3.Connection) -> set:
    """Internal shadow tables of SQLite full-text virtual tables."""
    virtual = [r[0] for r in lite.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND"
        " sql LIKE 'CREATE VIRTUAL TABLE%'")]
    derived = set()
    for name in _source_tables(lite):
        if any(name == v + suffix for v in virtual for suffix in _FTS_INTERNAL):
            derived.add(name)
    return derived


def destination_relations(pg_conn) -> list:
    """User relations in the destination database, marker excluded."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT n.nspname || '.' || c.relname FROM pg_class c JOIN"
            " pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname NOT IN"
            " ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE"
            " 'pg_toast%%' AND n.nspname NOT LIKE 'pg_temp%%' AND c.relname"
            " NOT IN (%s, %s) ORDER BY 1",
            (MARKER_TABLE, MARKER_TABLE + "_pkey"))
        return [r[0] for r in cur.fetchall()]


def destination_objects(pg_conn) -> list:
    """Non-relation objects that make a destination non-empty: user
    functions, operators, types (the marker's row types aside), extra
    schemas, default privileges and event triggers."""
    queries = (
        ("function", "SELECT n.nspname || '.' || p.proname || '(' ||"
         " pg_get_function_identity_arguments(p.oid) || ')' FROM pg_proc p"
         f" JOIN pg_namespace n ON n.oid = p.pronamespace WHERE {_SYSTEM_NSP}"),
        ("operator", "SELECT n.nspname || '.' || o.oprname FROM pg_operator o"
         f" JOIN pg_namespace n ON n.oid = o.oprnamespace WHERE {_SYSTEM_NSP}"),
        ("type", "SELECT n.nspname || '.' || t.typname FROM pg_type t JOIN"
         f" pg_namespace n ON n.oid = t.typnamespace WHERE {_SYSTEM_NSP} AND"
         f" t.typname NOT IN ('{MARKER_TABLE}', '_{MARKER_TABLE}')"),
        ("schema", "SELECT n.nspname FROM pg_namespace n WHERE"
         f" {_SYSTEM_NSP} AND n.nspname <> 'public'"),
        ("default privileges", "SELECT d.defaclrole::regrole::text || ' '"
         " || d.defaclobjtype::text FROM pg_default_acl d"),
        ("event trigger", "SELECT evtname FROM pg_event_trigger"),
    )
    found = []
    with pg_conn.cursor() as cur:
        for kind, query in queries:
            cur.execute(query + " ORDER BY 1")
            found += [f"{kind} {r[0]}" for r in cur.fetchall()]
    return found


def event_triggers(pg_conn) -> list:
    """Event triggers in the destination database (they could rewrite
    the DDL this migration runs, policies included)."""
    with pg_conn.cursor() as cur:
        cur.execute("SELECT evtname FROM pg_event_trigger ORDER BY 1")
        return [r[0] for r in cur.fetchall()]


def preflight(lite: sqlite3.Connection, pg_conn) -> dict:
    """Refuse anything but one single-workspace source into an empty
    destination. Returns the source shape; raises MigrationRefused."""
    tables = _source_tables(lite)
    occupied = destination_relations(pg_conn)
    if occupied:
        raise MigrationRefused(
            f"destination is not empty ({len(occupied)} relations, e.g. "
            f"{occupied[0]}); this version only migrates into an empty,"
            " disposable database")
    objects = destination_objects(pg_conn)
    if objects:
        raise MigrationRefused(
            f"destination is not empty ({len(objects)} other objects: "
            f"{', '.join(objects[:6])}); this version only migrates into an"
            " empty, disposable database")
    values: set = set()
    for table in tables:
        if "workspace_id" in _colnames(lite, table):
            values.update(r[0] for r in lite.execute(
                f'SELECT DISTINCT workspace_id FROM "{table}"'))
    if len(values) > 1:
        raise MigrationRefused(
            "source holds rows for several workspaces "
            f"({sorted(map(str, values))}); only a single-workspace source"
            " is supported")
    hosted = [t for t in tables if t in HOSTED_TABLES and lite.execute(
        f'SELECT 1 FROM "{t}" LIMIT 1').fetchone()]
    if hosted:
        raise MigrationRefused(
            f"source tables {hosted} share a name with hosted relations and"
            " hold rows; no row mapping exists for them yet")
    refused, null_keys = [], []
    for table in tables:
        if table in HOSTED_TABLES:
            continue
        shape = _table_shape(lite, table)
        refused += [f"{table}.{index}" for index in shape["refused"]]
        if shape["source_key"]:
            nulls = " OR ".join(f'"{c}" IS NULL' for c in shape["source_key"])
            count = lite.execute(f'SELECT COUNT(*) FROM "{table}" WHERE'
                                 f" {nulls}").fetchone()[0]
            if count:
                null_keys.append(f"{table} ({count})")
    if refused:
        raise MigrationRefused(
            f"source unique keys {refused} are partial, use expressions or"
            " a non-binary collation; this version does not carry them and"
            " will not weaken them")
    if null_keys:
        raise MigrationRefused(
            f"source rows with a NULL primary key column: {null_keys}; a"
            " Postgres primary key cannot hold them")
    return {"tables": tables, "source_workspace_ids": sorted(values)}


def _copy_table(cur, lite: sqlite3.Connection, table: str,
                workspace_id: str) -> None:
    """Create one table with the source's constraints and copy its rows,
    every row in workspace_id.

    Natively scoped tables (already carrying workspace_id) are remapped
    to the target like stamped ones, so row-level security shows them;
    workspace_id is NOT NULL with the target as its default either way.
    Keys and uniques are workspace-scoped (_scope). A rowid key becomes
    an identity column whose sequence continues past the source's
    high-water mark. CHECK expressions Postgres rejects are skipped here
    and reported by schema_check."""
    if table in HOSTED_TABLES:
        return
    shape = _table_shape(lite, table)
    if not shape["columns"]:
        return
    names = [c["name"] for c in shape["columns"]]
    ws_column = f'"workspace_id" TEXT NOT NULL DEFAULT {_quote(workspace_id)}'
    defs = []
    for col in shape["columns"]:
        if col["name"] == "workspace_id":
            defs.append(ws_column)
            continue
        parts = [f'"{col["name"]}"', col["type"]]
        if col["name"] in shape["identity"]:
            parts.append("GENERATED BY DEFAULT AS IDENTITY")
        if col["not_null"]:
            parts.append("NOT NULL")
        if col["default"]:
            parts.append(f"DEFAULT {col['default']}")
        defs.append(" ".join(parts))
    if "workspace_id" not in names:
        defs.append(ws_column)
    if shape["primary_key"]:
        defs.append(f"PRIMARY KEY ({_cols(shape['primary_key'])})")
    defs += [f"UNIQUE ({_cols(cols)})" for cols in shape["unique"]]
    cur.execute(f'CREATE TABLE public."{table}" ({", ".join(defs)})')
    for number, expr in enumerate(shape["checks"], 1):
        cur.execute("SAVEPOINT ew_check")
        try:
            cur.execute(f'ALTER TABLE "{table}" ADD CONSTRAINT'
                        f' "{_name(table, [str(number)], "check")}"'
                        f" CHECK ({expr})")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT ew_check")
        cur.execute("RELEASE SAVEPOINT ew_check")
    data = [n for n in names if n != "workspace_id"]
    rows = [tuple(r[n] for n in data) + (workspace_id,)
            for r in lite.execute(f'SELECT * FROM "{table}"')]
    if rows:
        quoted = ", ".join(f'"{n}"' for n in data + ["workspace_id"])
        placeholders = ", ".join(["%s"] * (len(data) + 1))
        cur.executemany(f'INSERT INTO "{table}" ({quoted}) VALUES'
                        f" ({placeholders})", rows)
    for col, high in shape["identity"].items():
        if high > 0:
            cur.execute("SELECT setval(pg_get_serial_sequence(%s, %s), %s)",
                        (f'public."{table}"', col, high))


def migrate_core(sqlite_path: str, pg_conn, workspace_id: str,
                 owner_id: str = "owner-1", *, commit: bool = True) -> dict:
    """Copy a disposable SQLite workspace into an empty Postgres database
    and reconcile.

    pg_conn is an open psycopg connection (autocommit off). The source
    is read read-only in one snapshot. Refuses (MigrationRefused, before
    any write) a non-empty destination, a multi-workspace source, and
    populated tables that collide with hosted relations. With commit
    True the copy is committed before reconciling; with commit False
    nothing is committed and the caller decides. Returns {table: entry}
    from reconcile().
    """
    lite = _open_source(sqlite_path)
    try:
        with pg_conn.cursor() as cur:
            cur.execute(f"SET LOCAL search_path = {PINNED_PATH}")
        tables = preflight(lite, pg_conn)["tables"]
        with pg_conn.cursor() as cur:
            cur.execute(HOSTED_DDL.replace("CREATE TABLE IF NOT EXISTS ",
                                           "CREATE TABLE IF NOT EXISTS public."))
            cur.execute("INSERT INTO workspaces (id, created_at) VALUES "
                        "(%s, NOW()) ON CONFLICT (id) DO NOTHING",
                        (workspace_id,))
            cur.execute("INSERT INTO memberships (workspace_id, principal_id,"
                        " role, status) VALUES (%s, %s, 'owner', 'active')"
                        " ON CONFLICT DO NOTHING", (workspace_id, owner_id))
            for table in tables:
                _copy_table(cur, lite, table, workspace_id)
            # Every composite FK target needs its (workspace_id, key)
            # unique key, whether workspace_id was stamped here or the
            # table already carries it (provided_briefs keys brief_id).
            for table, idcol in COMPOSITE_UNIQUES:
                if table in tables and idcol in _colnames(lite, table):
                    _add_constraint(
                        cur, table, f"{table}_ws_unique",
                        f'UNIQUE (workspace_id, "{idcol}")')
            _add_foreign_keys(cur, lite, tables)
            cur.execute("DO $$ BEGIN CREATE ROLE earwitness_runtime WITH "
                        "NOBYPASSRLS; EXCEPTION WHEN duplicate_object THEN "
                        "NULL; END $$")
            rls_tables = [t for t in tables if t not in HOSTED_TABLES] + list(
                HOSTED_TABLES)
            stamped = {t: ("workspace_id" in _colnames(lite, t))
                       for t in tables}
            for table in rls_tables:
                cur.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
                cur.execute('DROP POLICY IF EXISTS workspace_isolation ON '
                            f'"{table}"')
                cur.execute(f'CREATE POLICY workspace_isolation ON "{table}"'
                            f' USING ({_policy_expr(table, stamped)})')
                cur.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{table}"'
                            ' TO earwitness_runtime')
        if commit:
            pg_conn.commit()
        return _reconcile(lite, pg_conn, workspace_id, tables)
    finally:
        lite.close()


def _normalize(rows: list) -> list:
    """One value normalization for both sides of the reconcile."""
    import datetime
    import decimal
    clean = []
    for row in rows:
        item = {}
        for key, value in row.items():
            if isinstance(value, (datetime.datetime, datetime.date)):
                value = value.isoformat()
            elif isinstance(value, decimal.Decimal):
                value = str(value)
            elif isinstance(value, (bytes, bytearray, memoryview)):
                value = bytes(value).hex()
            item[key] = value
        clean.append(item)
    return clean


def reconcile(sqlite_path: str, pg_conn, workspace_id: str) -> dict:
    """Compare a migrated destination with its SQLite source."""
    lite = _open_source(sqlite_path)
    try:
        return _reconcile(lite, pg_conn, workspace_id, _source_tables(lite))
    finally:
        lite.close()


def _reconcile(lite: sqlite3.Connection, pg_conn, workspace_id: str,
               tables: list) -> dict:
    """Per copied table: row counts on both sides, digests of the rows in
    the expected destination shape (every row in workspace_id; same
    normalization both sides), and orphan counts per composite FK.
    Internal full-text structures match on row count ("compare":
    "count"); every other table, full-text virtual and _content tables
    included, must match row for row ("compare": "rows").
    Hosted-name tables are not copied and carry no match."""
    report: dict = {}
    derived = _derived_tables(lite)
    with pg_conn.cursor() as cur:
        cur.execute(f"SET LOCAL search_path = {PINNED_PATH}")
        for table in tables:
            source_rows = lite.execute(
                f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            cur.execute(f'SELECT COUNT(*) FROM "{table}"')
            entry: dict = {"rows": cur.fetchone()[0],
                           "source_rows": source_rows}
            if table in HOSTED_TABLES:
                entry["hosted"] = True
                report[table] = entry
                continue
            lite_rows = [{**dict(r), "workspace_id": workspace_id}
                         for r in lite.execute(f'SELECT * FROM "{table}"')]
            entry["sqlite_hash"] = _content_hash(_normalize(lite_rows))
            cur.execute(f'SELECT * FROM "{table}"')
            columns = [d[0] for d in cur.description]
            pg_rows = [dict(zip(columns, r)) for r in cur.fetchall()]
            entry["pg_hash"] = _content_hash(_normalize(pg_rows))
            relations = {}
            for child, col, ref, refcol in COMPOSITE_FKS:
                if child != table or ref not in tables:
                    continue
                if (col not in _colnames(lite, child)
                        or refcol not in _colnames(lite, ref)):
                    continue
                src = lite.execute(
                    f'SELECT COUNT(*) FROM "{child}" AS c WHERE c."{col}" IS'
                    f' NOT NULL AND NOT EXISTS (SELECT 1 FROM "{ref}" AS r'
                    f' WHERE r."{refcol}" = c."{col}")').fetchone()[0]
                cur.execute(
                    f'SELECT COUNT(*) FROM "{child}" AS c WHERE c."{col}" IS'
                    f' NOT NULL AND NOT EXISTS (SELECT 1 FROM "{ref}" AS r'
                    f' WHERE r."{refcol}" = c."{col}" AND r.workspace_id ='
                    f' c.workspace_id)')
                relations[f"{col}->{ref}.{refcol}"] = {
                    "source_orphans": src, "dest_orphans": cur.fetchone()[0]}
            entry["relations"] = relations
            entry["compare"] = "count" if table in derived else "rows"
            entry["match"] = (
                (entry["compare"] == "count"
                 or entry["sqlite_hash"] == entry["pg_hash"])
                and entry["rows"] == source_rows
                and all(r["source_orphans"] == r["dest_orphans"]
                        for r in relations.values()))
            report[table] = entry
    return report


RUNTIME_ROLE = "earwitness_runtime"


def _role_problems(cur) -> list:
    """Every role the runtime role can reach through membership (itself
    included): none may be a superuser, bypass RLS, or own a table."""
    cur.execute(
        "WITH RECURSIVE reach(oid) AS (SELECT oid FROM pg_roles WHERE"
        " rolname = %s UNION SELECT m.roleid FROM pg_auth_members m JOIN"
        " reach r ON m.member = r.oid) SELECT p.oid, p.rolname, p.rolsuper,"
        " p.rolbypassrls FROM reach JOIN pg_roles p ON p.oid = reach.oid"
        " ORDER BY p.rolname", (RUNTIME_ROLE,))
    reach = cur.fetchall()
    cur.execute("SELECT DISTINCT relowner FROM pg_class WHERE relnamespace ="
                " 'public'::regnamespace AND relkind IN ('r','p','v','m','f')")
    owners = {r[0] for r in cur.fetchall()}
    problems = []
    for _oid, name, superuser, bypass in reach:
        if name == RUNTIME_ROLE:
            problems += (["rolsuper"] if superuser else []) + (
                ["rolbypassrls"] if bypass else [])
        else:
            problems += (
                [f"can switch to superuser role {name}"] if superuser else []
            ) + ([f"can switch to BYPASSRLS role {name}"] if bypass else [])
    owned = sorted(name for oid, name, _s, _b in reach if oid in owners)
    if owned:
        problems.append(f"member of table owner {owned}")
    return problems


def _setting_problems(cur) -> list:
    """Saved app.* or search_path defaults for this database, the runtime
    role, their pair, or all roles. SET ROLE never applies role defaults, so the
    catalog is the only place they show."""
    cur.execute(
        "SELECT COALESCE(d.datname, '*'), COALESCE(r.rolname, '*'), cfg FROM"
        " pg_db_role_setting s CROSS JOIN LATERAL unnest(s.setconfig) AS cfg"
        " LEFT JOIN pg_database d ON d.oid = s.setdatabase LEFT JOIN pg_roles"
        " r ON r.oid = s.setrole WHERE s.setdatabase IN (0, (SELECT oid FROM"
        " pg_database WHERE datname = current_database())) AND s.setrole IN"
        " (0, (SELECT oid FROM pg_roles WHERE rolname = %s)) AND (cfg LIKE"
        " 'app.%%' OR split_part(cfg, '=', 1) = 'search_path') ORDER BY 1, 2, 3",
        (RUNTIME_ROLE,))
    return [f"saved setting {cfg.split('=')[0]} (database {db}, role {role})"
            for db, role, cfg in cur.fetchall()]


RUNTIME_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")
_TABLE_PRIVILEGES = RUNTIME_PRIVILEGES + ("TRUNCATE", "REFERENCES", "TRIGGER")


def _grant_problems(cur) -> dict:
    """{table: effective privileges} for every table in public (marker
    aside) where the runtime role (through any membership) does not hold
    exactly SELECT, INSERT, UPDATE, DELETE — the copy's GRANT; RLS does
    not cover TRUNCATE — or where PUBLIC holds any privilege."""
    cur.execute(
        "SELECT c.relname, ARRAY(SELECT p FROM unnest(%s::text[]) AS p WHERE"
        " has_table_privilege(%s, c.oid, p)), ARRAY(SELECT p FROM"
        " unnest(%s::text[]) AS p WHERE has_table_privilege('public', c.oid,"
        " p)) FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace"
        " AND c.relkind IN ('r', 'p') AND c.relname <> %s ORDER BY 1",
        (list(_TABLE_PRIVILEGES), RUNTIME_ROLE, list(_TABLE_PRIVILEGES),
         MARKER_TABLE))
    return {name: {"runtime": list(runtime), "public": list(public)}
            for name, runtime, public in cur.fetchall()
            if sorted(runtime) != sorted(RUNTIME_PRIVILEGES) or public}


def _deparsed_expected(cur, oid: int, table: str):
    """The server's own rendering of _expected_policy(table) for this
    table, from a probe policy rolled back at once; None if invalid."""
    cur.execute("SAVEPOINT ew_policy_probe")
    try:
        cur.execute(f'CREATE POLICY ew_policy_probe ON "{table}" USING'
                    f" ({_expected_policy(table)})")
        cur.execute("SELECT pg_get_expr(polqual, polrelid) FROM pg_policy"
                    " WHERE polrelid = %s AND polname = 'ew_policy_probe'",
                    (oid,))
        return cur.fetchone()[0]
    except Exception:
        return None
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT ew_policy_probe")
        cur.execute("RELEASE SAVEPOINT ew_policy_probe")


def _policy_problems(cur) -> dict:
    """{relation: reason} for every relation in the public schema (hosted
    and empty tables included, marker excluded) that is not a table with
    RLS on and exactly one permissive workspace_isolation policy for all
    commands and PUBLIC, no WITH CHECK, and the expected USING."""
    cur.execute("SELECT c.oid, c.relname, c.relkind, c.relrowsecurity FROM"
                " pg_class c WHERE c.relnamespace = 'public'::regnamespace AND"
                " c.relkind IN ('r','p','v','m','f') AND c.relname <> %s"
                " ORDER BY 2", (MARKER_TABLE,))
    out = {}
    for oid, name, kind, rls in cur.fetchall():
        if kind not in ("r", "p"):
            out[name] = f"relation kind {kind!r} is not row-level secured"
            continue
        if not rls:
            out[name] = "row-level security is off"
            continue
        cur.execute("SELECT polname, polpermissive, polcmd, polroles,"
                    " pg_get_expr(polqual, polrelid), pg_get_expr(polwithcheck,"
                    " polrelid) FROM pg_policy WHERE polrelid = %s ORDER BY 1",
                    (oid,))
        policies = cur.fetchall()
        names = [p[0] for p in policies]
        if names != ["workspace_isolation"]:
            out[name] = f"policies {names}; expected only workspace_isolation"
            continue
        _n, permissive, cmd, roles, qual, check = policies[0]
        expected = _deparsed_expected(cur, oid, name)
        if (not permissive or cmd not in ("*", b"*") or list(roles) != [0]
                or check is not None or expected is None or qual != expected):
            out[name] = "workspace_isolation differs from the expected policy"
    return out


def runtime_check(pg_conn, workspace_id: str, report: dict) -> dict:
    """Prove row-level security holds for the runtime role, inside the
    caller's (uncommitted) transaction. Never alters the role.

    problems (unsafe setup, refused): the role is missing; it, or any
    role it can reach through membership, is a superuser, bypasses RLS
    or owns a table; a saved app.* default exists for this database, the
    role or their pair (search_path included); app.workspace_id is preset
    after SET ROLE; an event trigger exists; on any table the runtime
    role does not hold exactly RUNTIME_PRIVILEGES or PUBLIC holds any.
    Runs under PINNED_PATH.
    mismatches: policy:<relation> from the catalog check over every
    relation in the schema; runtime:<table> when, under SET LOCAL ROLE,
    a copied table does not show its source count in the target
    workspace and 0 rows for another workspace and with no context."""
    tables = sorted(t for t, e in report.items() if not e.get("hosted"))
    problems: list = []
    visibility: dict = {}
    with pg_conn.cursor() as cur:
        cur.execute(f"SET LOCAL search_path = {PINNED_PATH}")
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s",
                    (RUNTIME_ROLE,))
        if cur.fetchone() is None:
            return {"ok": False, "problems": [f"{RUNTIME_ROLE} is missing"],
                    "mismatches": [], "policy": {}, "grants": {},
                    "tables": {}}
        problems += _role_problems(cur)
        problems += _setting_problems(cur)
        grants = _grant_problems(cur)
        if grants:
            problems.append(
                f"runtime privileges differ from {', '.join(RUNTIME_PRIVILEGES)}"
                f" (or PUBLIC holds privileges) on {sorted(grants)[:6]}")
        triggers = event_triggers(pg_conn)
        if triggers:
            problems.append(f"event triggers {triggers}")
        policy = _policy_problems(cur)
        cur.execute(f"SET LOCAL ROLE {RUNTIME_ROLE}")
        cur.execute("SELECT current_setting('app.workspace_id', true)")
        preset = cur.fetchone()[0]
        if preset not in (None, ""):
            problems.append(f"app.workspace_id preset to {preset!r} for the"
                            " runtime session")
            cur.execute("SELECT set_config('app.workspace_id', '', true)")
        for label, value in (("none", None), ("other", workspace_id + "#other"),
                             ("target", workspace_id)):
            if value is not None:
                cur.execute("SELECT set_config('app.workspace_id', %s, true)",
                            (value,))
            for table in tables:
                cur.execute(f'SELECT COUNT(*) FROM "{table}"')
                visibility.setdefault(table, {})[label] = cur.fetchone()[0]
        cur.execute("RESET ROLE")
        cur.execute("SELECT set_config('app.workspace_id', '', true)")
    mismatches = sorted([f"policy:{r}" for r in policy] + [
        f"runtime:{t}" for t in tables if visibility[t] != {
            "target": report[t]["source_rows"], "other": 0, "none": 0}])
    return {"ok": not problems and not mismatches, "problems": problems,
            "mismatches": mismatches, "policy": policy, "grants": grants,
            "tables": {t: {k: visibility[t][k] for k in
                           ("target", "other", "none")} for t in tables}}


def _jsonable(rows: list) -> list:
    import datetime
    import decimal
    clean = []
    for row in rows:
        item = {}
        for key, value in row.items():
            if isinstance(value, (datetime.datetime, datetime.date)):
                value = value.isoformat()
            elif isinstance(value, decimal.Decimal):
                value = str(value)
            elif isinstance(value, memoryview):
                value = bytes(value).hex()
            item[key] = value
        clean.append(item)
    return clean


def pg_fetch_all(pg_conn, table: str, order_by: str = "1") -> list:
    """Load a Postgres table as plain dicts (parity comparisons)."""
    with pg_conn.cursor() as cur:
        cur.execute(f'SELECT * FROM "{table}" ORDER BY {order_by}')
        columns = [d[0] for d in cur.description]
        return _jsonable([dict(zip(columns, r)) for r in cur.fetchall()])


def pg_order_sample(pg_conn, table: str, column: str, limit: int = 50) -> list:
    """Byte-order key sample for collation parity checks."""
    with pg_conn.cursor() as cur:
        cur.execute(f'SELECT x FROM (SELECT DISTINCT "{column}" AS x FROM '
                    f'"{table}" WHERE "{column}" IS NOT NULL) AS distinct_keys'
                    f' ORDER BY x COLLATE "C" LIMIT {limit}')
        return [r[0] for r in cur.fetchall()]
