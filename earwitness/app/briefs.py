"""Server-registered provided briefs (K06): immutable SuppliedBriefRef1.

A brief is an immutable versioned server record of provided text. The
server captures bytes/hash/version/capture time and constructs
actor/workspace; old versions are preserved and never substituted.
No external fetch, document parser, automatic requirement extraction
or model call exists: no requirement extraction runs in the app; a
pre-run Muse Spark check may be imported for named campaigns (WIN-2).
Client `synthetic`/`contractual` flags are
rejected as authority inputs; fixture qualification is a server-known
property of the recorded capture actor, never a production input.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import uuid as _uuid

from . import db as _db
from . import ops as _ops

TITLE_MAX = 120
TEXT_MAX = 2_000_000
LIST_LIMIT = 20
BRIEF_KIND = "supplied_brief"


def _digest(title: str, provided_text: str) -> str:
    return hashlib.sha256(json.dumps(
        {"title": title, "text": provided_text},
        sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _check_body(title, provided_text) -> tuple[str, str]:
    if not isinstance(title, str) or not title or len(title) > TITLE_MAX:
        raise _ops.BadInput(
            f"brief title must be a nonempty string up to {TITLE_MAX} chars")
    if not isinstance(provided_text, str) or not provided_text.strip():
        raise _ops.BadInput("provided_text must be nonempty text")
    if len(provided_text) > TEXT_MAX:
        raise _ops.BadInput(
            f"provided_text exceeds the {TEXT_MAX}-byte request cap")
    return title, provided_text


def _check_no_client_authority(body: dict) -> None:
    for flag in ("synthetic", "contractual", "brief_type"):
        if flag in body:
            raise _ops.BadInput(
                f"brief {flag} is server-known authority, not a client input")


@_db.atomic
def create_brief(con: sqlite3.Connection, body: dict | None,
                 actor: str = "owner", ctx=None) -> dict:
    """Register a brief with its first immutable version."""
    from . import access, citations
    if ctx is not None and not ctx.permits("campaign"):
        raise access.AuthorizationError("provided-brief disclosure denied", 403)
    if not isinstance(body, dict):
        raise _ops.BadInput("brief body must be an object")
    if set(body) - {"title", "provided_text"}:
        raise _ops.BadInput(
            f"unknown brief fields: {sorted(set(body) - {'title', 'provided_text'})}")
    _check_no_client_authority(body)
    title, provided_text = _check_body(body.get("title", ""),
                                       body.get("provided_text", ""))
    brief_id = "brief-" + _uuid.uuid4().hex[:16]
    now = _db.utcnow()
    digest = _digest(title, provided_text)
    con.execute("INSERT INTO provided_briefs (brief_id, workspace_id,"
                " head_revision, current_version, created_at, updated_at)"
                " VALUES (?, 'local', 1, 1, ?, ?)", (brief_id, now, now))
    con.execute("INSERT INTO provided_brief_versions (workspace_id, brief_id,"
                " version, title, provided_text, digest, captured_at, actor)"
                " VALUES ('local', ?, 1, ?, ?, ?, ?, ?)",
                (brief_id, title, provided_text, digest, now, actor))
    citations.authorize_resource(con, "brief", brief_id, ctx)
    rev = _db.bump_revision(con)
    _db.audit(con, actor, "brief_create", "provided_brief", brief_id,
              {"version": 1, "digest": digest}, rev)
    return {"brief_id": brief_id, "version": 1, "digest": digest,
            "aggregate_rev": rev}


@_db.atomic
def add_version(con: sqlite3.Connection, brief_id: str, body: dict | None,
                expected_revision, actor: str = "owner", ctx=None) -> dict:
    """Append an immutable version; the displayed head revision is checked."""
    from . import citations
    citations.authorize_resource(con, "brief", brief_id, ctx)
    if not isinstance(body, dict):
        raise _ops.BadInput("brief version body must be an object")
    if set(body) - {"title", "provided_text"}:
        raise _ops.BadInput("unknown brief version fields")
    _check_no_client_authority(body)
    if expected_revision is None:
        raise _ops.MissingPrecondition("expected revision is required")
    row = con.execute("SELECT head_revision, current_version FROM provided_briefs"
                      " WHERE brief_id=? AND workspace_id='local'",
                      (brief_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown brief")
    if int(expected_revision) != row["head_revision"]:
        raise _ops.Conflict(row["head_revision"])
    title, provided_text = _check_body(body.get("title", ""),
                                       body.get("provided_text", ""))
    version = row["current_version"] + 1
    now = _db.utcnow()
    digest = _digest(title, provided_text)
    con.execute("INSERT INTO provided_brief_versions (workspace_id, brief_id,"
                " version, title, provided_text, digest, captured_at, actor)"
                " VALUES ('local', ?, ?, ?, ?, ?, ?, ?)",
                (brief_id, version, title, provided_text, digest, now,
                 actor))
    con.execute("UPDATE provided_briefs SET head_revision=head_revision+1,"
                " current_version=?, updated_at=? WHERE brief_id=?"
                " AND workspace_id='local'", (version, now, brief_id))
    rev = _db.bump_revision(con)
    _db.audit(con, actor, "brief_version", "provided_brief", brief_id,
              {"version": version, "digest": digest}, rev)
    return {"brief_id": brief_id, "version": version, "digest": digest,
            "aggregate_rev": rev}


def get_brief(con: sqlite3.Connection, brief_id: str, ctx=None) -> dict:
    from . import citations
    citations.authorize_resource(con, "brief", brief_id, ctx)
    row = con.execute("SELECT * FROM provided_briefs WHERE brief_id=?"
                      " AND workspace_id='local'", (brief_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown brief")
    versions = [dict(r) for r in con.execute(
        "SELECT brief_id, version, title, digest, captured_at, actor"
        " FROM provided_brief_versions WHERE workspace_id='local'"
        " AND brief_id=? ORDER BY version", (brief_id,)).fetchall()]
    return {"brief_id": row["brief_id"],
            "head_revision": row["head_revision"],
            "current_version": row["current_version"],
            "versions": versions}


def get_version(con: sqlite3.Connection, brief_id: str, version, ctx=None) -> dict:
    from . import citations
    citations.authorize_resource(con, "brief", brief_id, ctx)
    try:
        version = int(version)
    except (TypeError, ValueError):
        raise _ops.BadInput("brief version must be an integer")
    row = con.execute("SELECT * FROM provided_brief_versions"
                      " WHERE workspace_id='local' AND brief_id=? AND version=?",
                      (brief_id, version)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown brief version")
    if _digest(row["title"], row["provided_text"]) != row["digest"]:
        raise _ops.BadInput("stored brief digest mismatch")
    return dict(row)


def _collection_revision(con: sqlite3.Connection) -> int:
    try:
        return _db.aggregate_revision(con)
    except sqlite3.Error:
        return 0


def list_briefs(con: sqlite3.Connection, q: str = "",
                cursor: str | None = None, limit: int = LIST_LIMIT, ctx=None) -> dict:
    from . import access, citations, customer_contracts
    if ctx is not None and not ctx.permits("campaign"):
        raise access.AuthorizationError("provided-brief disclosure denied", 403)
    if not isinstance(q, str):
        raise _ops.BadInput("q must be a string")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= LIST_LIMIT:
        raise _ops.BadInput(f"limit must be 1..{LIST_LIMIT}")
    needle = q.strip().casefold()
    items = []
    for row in con.execute(
            "SELECT b.brief_id,b.head_revision,b.current_version,v.title,v.digest,v.captured_at"
            " FROM provided_briefs b JOIN provided_brief_versions v"
            " ON v.workspace_id=b.workspace_id AND v.brief_id=b.brief_id"
            " AND v.version=b.current_version WHERE b.workspace_id='local'"
            " ORDER BY v.captured_at,b.brief_id"):
        if not citations.resource_allowed(con, "brief", row["brief_id"], ctx):
            continue
        if needle and needle not in row["title"].casefold():
            continue
        items.append(dict(row))
    revision = hashlib.sha256(json.dumps(
        [getattr(ctx, "policy_identity", None), items],
        sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    offset = 0
    if cursor is not None:
        try:
            payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        except (ValueError, TypeError, AttributeError):
            raise _ops.BadInput("malformed brief cursor")
        if not isinstance(payload, dict) or payload.get("q") != q:
            raise _ops.BadInput("brief cursor belongs to another filter")
        if payload.get("r") != revision:
            raise customer_contracts.SelectorConflict("brief collection changed; refresh the list")
        offset = payload.get("o")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise _ops.BadInput("malformed brief cursor")
    total = len(items)
    next_cursor = None
    if offset + limit < total:
        next_cursor = base64.urlsafe_b64encode(json.dumps(
            {"q": q, "o": offset + limit, "r": revision},
            sort_keys=True).encode()).decode()
    return {"items": items[offset:offset + limit], "total": total,
            "total_relation": "exact", "next_cursor": next_cursor,
            "collection_revision": revision}


def resolve_ref(con: sqlite3.Connection, ref) -> dict:
    """Authorize an exact SuppliedBriefRef1 and project its authority.

    The ref selects an explicitly registered brief version; the server
    never substitutes the latest version. Missing/foreign/stale refs
    fail before any write. A provided document is not a verified real
    agreement. Legacy unregistered strings need explicit review.
    """
    if not isinstance(ref, dict):
        raise _ops.BadInput("brief_ref must be a SuppliedBriefRef1 object")
    if set(ref) != {"kind", "brief_id", "version"}:
        raise _ops.BadInput("brief_ref holds kind/brief_id/version only")
    if ref.get("kind") != BRIEF_KIND:
        raise _ops.BadInput(f"brief_ref kind must be {BRIEF_KIND!r}")
    brief_id, version = ref.get("brief_id"), ref.get("version")
    if not isinstance(brief_id, str) or not brief_id:
        raise _ops.BadInput("brief_ref brief_id must be a nonempty string")
    if not isinstance(version, int) or isinstance(version, bool) \
            or version < 1:
        raise _ops.BadInput("brief_ref version must be a positive integer")
    row = get_version(con, brief_id, version)
    return {"brief_id": brief_id, "version": version,
            "digest": row["digest"], "source_label": row["title"],
            "source_kind": "provided_text", "captured_at": row["captured_at"],
            "qualification": {"synthetic": row["actor"] == "fixture"}}
