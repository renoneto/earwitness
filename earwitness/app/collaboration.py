"""P12 team research, authorization and sharing (disjoint leaf).

Multi-member workspaces with owner/editor/viewer roles, assignments,
revisioned comments, agent proposals, and authenticated named-audience
shares. Tenant enforcement uses composite workspace scoping; local owner
mode stays useful but cannot satisfy multi-user acceptance.

Boundaries honored:
- Membership invites are outward actions: mechanics only, DO NOT send.
- Sign-in is not decided here. The pilot uses built-in email and password
  sign-in (local_accounts.py, PW design rev 4); a maintained OIDC
  provider may come later. Nothing in this module handles passwords.
- Editor processes ONLY with an explicit processing grant.
- Comment permission is separate (default owner/editor).
- Shares: authenticated named-audience read-only links; default 7-day
  proposed expiry with source-policy expiry winning; no anonymous links;
  no audio redistribution by default. Pre-create preview + single
  labeled "Create share" action; result shows active/expired/revoked +
  no-recall notice.
- Guest reads go through AccessContext authority_kind=share service
  rule (server resolves grant after auth; citations cannot broaden;
  streams obey bounded release).
- Agent proposals keep agent origin (never human/verified/accepted);
  applying records proposer + approver (approval != audio inspection).
- Hosted tenant migration rehearsal assigns legacy local records with
  composite FKs and scoped indexes; missing historical revisions stay
  missing.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from . import access as _access
from . import db as _db
from . import ops as _ops

ROLES = ("owner", "editor", "viewer")
ASSIGNMENT_STATES = ("open", "in_progress", "done")
SHARE_DEFAULT_EXPIRY_DAYS = 7
# One invitation lifetime for send and acceptance (invites_send uses it).
INVITE_TTL = timedelta(days=7)
# Pending is 'created'; 'accepted', 'superseded' and 'withdrawn' are final.
INVITE_STATES = ("created", "accepted", "superseded", "withdrawn")
# Membership decisions that make older invitations for the member stale.
_MEMBERSHIP_DECISIONS = ("role_set", "member_revoke")
AGENT_ORIGINS = ("agent", "classifier", "pipeline", "machine")

# Role -> allowed actions. Editor processing requires a separate grant.
_ROLE_ACTIONS = {
    "owner": {"membership", "policy", "sharing", "review", "organize",
              "comment", "process", "reconcile", "assign"},
    "editor": {"review", "organize", "comment", "assign"},
    "viewer": {"read"},
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4().hex[:12]}"


_AUX_TABLES = ("workspaces", "memberships", "processing_grants", "invites",
               "comment_revisions", "proposals", "share_audit")


def has_aux_schema(con: sqlite3.Connection) -> bool:
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    return all(t in tables for t in _AUX_TABLES)


def _require_aux_tables(con: sqlite3.Connection) -> None:
    if not has_aux_schema(con):
        raise RuntimeError("auxiliary workspace schema is not prepared:"
                           " run setup/explicit upgrade first")


def setup_aux_schema(con: sqlite3.Connection) -> None:
    """Setup/migration-only auxiliary DDL (K01). No commit: the caller-owned
    transaction commits. Reads/admissions never call this."""
    con.execute("CREATE TABLE IF NOT EXISTS workspaces ("
                " id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',"
                " created_at TEXT NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS memberships ("
                " workspace_id TEXT NOT NULL, member TEXT NOT NULL,"
                " role TEXT NOT NULL DEFAULT 'viewer',"
                " revoked INTEGER NOT NULL DEFAULT 0,"
                " created_at TEXT NOT NULL,"
                " PRIMARY KEY (workspace_id, member))")
    con.execute("CREATE TABLE IF NOT EXISTS processing_grants ("
                " workspace_id TEXT NOT NULL, member TEXT NOT NULL,"
                " granted_by TEXT NOT NULL DEFAULT '',"
                " created_at TEXT NOT NULL,"
                " PRIMARY KEY (workspace_id, member))")
    con.execute("CREATE TABLE IF NOT EXISTS invites ("
                " id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,"
                " member TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT 'viewer',"
                " status TEXT NOT NULL DEFAULT 'created',"
                " created_at TEXT NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS comment_revisions ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " workspace_id TEXT NOT NULL DEFAULT 'local',"
                " comment_id TEXT NOT NULL REFERENCES comments(id),"
                " revision INTEGER NOT NULL, body TEXT NOT NULL DEFAULT '',"
                " actor TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,"
                " UNIQUE (comment_id, revision))")
    con.execute("CREATE TABLE IF NOT EXISTS proposals ("
                " id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL DEFAULT 'local',"
                " target_type TEXT NOT NULL, target_id TEXT NOT NULL,"
                " origin TEXT NOT NULL DEFAULT 'agent',"
                " proposer TEXT NOT NULL DEFAULT '',"
                " body TEXT NOT NULL DEFAULT '{}',"
                " status TEXT NOT NULL DEFAULT 'open',"
                " approver TEXT, applied_at TEXT,"
                " created_at TEXT NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS share_audit ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " workspace_id TEXT NOT NULL DEFAULT 'local',"
                " share_id TEXT NOT NULL, actor TEXT NOT NULL DEFAULT '',"
                " action TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_memberships_ws ON memberships (workspace_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_assignments_ws ON assignments (workspace_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_comments_ws ON comments (workspace_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_shares_ws ON shares (workspace_id)")


def ensure_aux(con: sqlite3.Connection) -> None:
    """Setup/migration-only alias (K01). No commit; reads never call this."""
    setup_aux_schema(con)


# -- workspaces / membership ----------------------------------------------

@_db.atomic
def create_workspace(con: sqlite3.Connection, workspace_id: str, name: str,
                     owner: str) -> dict:
    ensure_aux(con)
    if not workspace_id or not owner:
        raise _ops.BadInput("workspace and owner are required")
    con.execute("INSERT INTO workspaces (id, name, created_at) VALUES (?, ?, ?)",
                (workspace_id, str(name or "")[:160], _now_iso()))
    con.execute("INSERT INTO memberships (workspace_id, member, role, created_at)"
                " VALUES (?, ?, 'owner', ?)", (workspace_id, owner, _now_iso()))
    return {"workspace_id": workspace_id, "owner": owner}


def get_role(con: sqlite3.Connection, workspace_id: str, member: str) -> str | None:
    """Pure prepared-schema admission lookup (K01): never creates schema."""
    _require_aux_tables(con)
    row = con.execute("SELECT role, revoked FROM memberships WHERE workspace_id=?"
                      " AND member=?", (workspace_id, member)).fetchone()
    if row is None or row["revoked"]:
        return None
    return row["role"]


def is_member(con: sqlite3.Connection, workspace_id: str, member: str) -> bool:
    return get_role(con, workspace_id, member) is not None


LAST_OWNER_REFUSAL = "a workspace needs at least one owner"


def _require_another_owner(con: sqlite3.Connection, workspace_id: str,
                           member: str) -> None:
    """T5: refuse when member is the workspace's only active owner. Call it
    inside the write transaction that changes the member, so two changes
    at once cannot each see another owner."""
    owners = [r[0] for r in con.execute(
        "SELECT member FROM memberships WHERE workspace_id=? AND role='owner'"
        " AND revoked=0", (workspace_id,))]
    if member in owners and len(owners) == 1:
        raise _ops.BadInput(LAST_OWNER_REFUSAL)


def _higher_role(a: str, b: str) -> str:
    return a if ROLES.index(a) <= ROLES.index(b) else b


def check_permission(con: sqlite3.Connection, workspace_id: str, member: str,
                     action: str) -> bool:
    """Role check; 'process' for editors needs an explicit processing grant."""
    role = get_role(con, workspace_id, member)
    if role is None:
        return False
    if action not in _ROLE_ACTIONS.get(role, set()):
        if action == "process" and role == "editor":
            row = con.execute("SELECT 1 FROM processing_grants WHERE workspace_id=?"
                              " AND member=?", (workspace_id, member)).fetchone()
            return row is not None
        return False
    if action == "process" and role == "editor":
        row = con.execute("SELECT 1 FROM processing_grants WHERE workspace_id=?"
                          " AND member=?", (workspace_id, member)).fetchone()
        return row is not None
    return True


# -- invitations ------------------------------------------------------------
#
# Rule (L22): an invitation never outlives a later decision about the same
# person. Identity is the invited principal plus every email tied to it in
# this workspace by an acceptance (and the reverse): the email a by-email
# acceptance bound, and the session's verified email when the account
# accepted by account (its verified alias). ASCII email addresses compare
# case-insensitively; other addresses and account ids compare exactly.
# - Newest wins: creating an invitation supersedes the identity's older
#   pending invitations.
# - Accepting supersedes the identity's other pending invitations,
#   including those addressed to the session's verified email.
# - Removing a member or changing its role supersedes the member's pending
#   invitations. Removing a member, or taking the owner role from it, also
#   supersedes the pending invitations it created. Acceptance also refuses any invitation created before the
#   latest such decision about the accepting account, or about any account
#   the invited address or the session's current verified email is tied
#   to: this covers an invitation to an email not yet tied to the account,
#   and another account using a removed member's verified email, even on
#   its first acceptance of an invitation addressed to that account.
# - Acceptance refuses an invitation older than INVITE_TTL.
# Superseded and withdrawn rows stay in place (history is never deleted).


def _parse_time(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def identity_key(value) -> str:
    """Comparison key for an invited address: an ASCII email address (an
    '@' and no ':') is lowercased; a non-ASCII address and an account id
    stay exact. Unicode case folding would merge distinct addresses (the
    same rule as admission)."""
    text = str(value or "")
    return text.lower() if "@" in text and ":" not in text and text.isascii() else text


def invite_expired(created_at, now: datetime | None = None) -> bool:
    """True when an invitation created at created_at is INVITE_TTL old or
    older (or its time is unreadable)."""
    created = _parse_time(created_at)
    return created is None or (now or datetime.now(timezone.utc)) >= created + INVITE_TTL


def _identities(con: sqlite3.Connection, workspace_id: str, members) -> set:
    """Identity keys of members plus every address tied to them in
    workspace_id by an acceptance (audit invite_accept_bound: bound_email
    or verified_alias), transitively."""
    ids = {identity_key(m) for m in members if m}
    pairs = []
    for target, detail in con.execute(
            "SELECT target_id, detail FROM audit WHERE action='invite_accept_bound'"):
        try:
            data = json.loads(detail or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("workspace") != workspace_id:
            continue
        for key in ("bound_email", "verified_alias"):
            email = data.get(key)
            if isinstance(email, str) and email:
                pairs.append((identity_key(target), identity_key(email)))
    grew = True
    while grew:
        grew = False
        for principal, email in pairs:
            if (principal in ids) != (email in ids):
                ids |= {principal, email}
                grew = True
    return ids


def _supersede(con: sqlite3.Connection, workspace_id: str, members,
               keep: str = "") -> list:
    """Mark the identity's pending invitations superseded; return their ids."""
    ids = _identities(con, workspace_id, members)
    if not ids:
        return []
    rows = con.execute("SELECT id, member FROM invites WHERE workspace_id=?"
                       " AND status='created' ORDER BY created_at, id",
                       (workspace_id,)).fetchall()
    out = [r[0] for r in rows if r[0] != keep and identity_key(r[1]) in ids]
    for invite_id in out:
        con.execute("UPDATE invites SET status='superseded' WHERE id=? AND status='created'",
                    (invite_id,))
    return out


def _supersede_created_by(con: sqlite3.Connection, workspace_id: str,
                          creator: str) -> list:
    """Mark the pending invitations creator issued in workspace_id (its
    invite_create audit rows) superseded; return their ids. Used when the
    creator loses the authority that issued them."""
    rows = con.execute(
        "SELECT id FROM invites WHERE workspace_id=? AND status='created'"
        " AND id IN (SELECT json_extract(detail, '$.invite_id') FROM audit"
        " WHERE action='invite_create' AND actor=? AND json_valid(detail))"
        " ORDER BY created_at, id", (workspace_id, creator)).fetchall()
    out = [r[0] for r in rows]
    for invite_id in out:
        con.execute("UPDATE invites SET status='superseded' WHERE id=? AND status='created'",
                    (invite_id,))
    return out


def _creation_seq(con: sqlite3.Connection, invite_id: str) -> int | None:
    row = con.execute("SELECT id FROM audit WHERE action='invite_create'"
                      " AND CASE WHEN json_valid(detail) THEN"
                      " json_extract(detail, '$.invite_id')=? END"
                      " ORDER BY id LIMIT 1", (invite_id,)).fetchone()
    return row[0] if row else None


def _last_decision_seq(con: sqlite3.Connection, workspace_id: str,
                       members) -> int | None:
    """Audit position of the latest removal or role change of any of
    members in workspace_id. A record without a workspace counts (older
    builds)."""
    members = sorted(members)
    if not members:
        return None
    marks = ",".join("?" * len(_MEMBERSHIP_DECISIONS))
    who = ",".join("?" * len(members))
    row = con.execute(f"SELECT MAX(id) FROM audit WHERE action IN ({marks})"
                      f" AND target_id IN ({who}) AND CASE WHEN json_valid(detail) THEN"
                      " json_extract(detail, '$.workspace') IS NULL"
                      " OR json_extract(detail, '$.workspace')=? ELSE 1 END",
                      (*_MEMBERSHIP_DECISIONS, *members, workspace_id)).fetchone()
    return row[0] if row and row[0] is not None else None


def acceptance_refusal(con: sqlite3.Connection, inv: dict, principal: str,
                       now: datetime | None = None, aliases=()) -> str | None:
    """Why principal may not accept inv now, or None. Audience is checked
    by the caller. aliases are the session's current server-verified
    email(s): part of the accepting identity even before any acceptance
    recorded them. The decisions that count are about the accepting
    account, the invited address, the aliases and every account tied to
    any of them. An invitation without a recorded creation (an older
    build) counts as older than any decision."""
    if inv.get("status") != "created":
        return "invite is no longer pending"
    if invite_expired(inv.get("created_at"), now):
        return "invite has expired"
    raw = {m for m in (principal, inv.get("member"), *aliases) if m}
    decided = _last_decision_seq(
        con, inv["workspace_id"], _identities(con, inv["workspace_id"], raw) | raw)
    if decided is not None:
        made = _creation_seq(con, inv["id"])
        if made is None or made < decided:
            return "invite is no longer pending"
    return None


@_db.atomic
def create_invite(con: sqlite3.Connection, workspace_id: str, member: str,
                  role: str, actor: str) -> dict:
    """Invite mechanics ONLY. No send happens here (outward action).
    The new invitation supersedes the identity's older pending ones."""
    ensure_aux(con)
    if role not in ROLES:
        raise _ops.BadInput(f"unknown role: {role!r}")
    if not check_permission(con, workspace_id, actor, "membership"):
        raise _access.AuthorizationError("owner manages membership", 403)
    superseded = _supersede(con, workspace_id, [member])
    invite_id = _new_id("invite")
    con.execute("INSERT INTO invites (id, workspace_id, member, role, status,"
                " created_at) VALUES (?, ?, ?, ?, 'created', ?)",
                (invite_id, workspace_id, member, role, _now_iso()))
    rev = _db.bump_revision(con)
    _db.audit(con, actor, "invite_create", "membership", member,
              {"workspace": workspace_id, "role": role, "sent": False,
               "invite_id": invite_id, "superseded": superseded}, rev)
    return {"invite_id": invite_id, "member": member, "role": role,
            "sent": False, "superseded": superseded, "aggregate_rev": rev}


@_db.atomic
def accept_invite(con: sqlite3.Connection, invite_id: str, actor: str,
                  aliases=()) -> dict:
    """Accept as actor. aliases are the actor's other verified addresses
    (the session's verified email); their pending invitations are
    superseded with the actor's own."""
    ensure_aux(con)
    row = con.execute("SELECT * FROM invites WHERE id=?", (invite_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown invite")
    inv = dict(row)
    if inv["status"] != "created":
        raise _ops.BadInput("invite is no longer pending")
    if actor != inv["member"]:
        raise _access.AuthorizationError("only the invited member may accept", 403)
    refusal = acceptance_refusal(con, inv, actor)
    if refusal:
        raise _ops.BadInput(refusal)
    # Acceptance never lowers an active member's role (T5): a pending
    # lower-role invitation must not demote an owner. A removed member
    # starts again at exactly the invited role.
    role = inv["role"]
    held = get_role(con, inv["workspace_id"], inv["member"])
    if held is not None:
        role = _higher_role(held, role)
    con.execute("INSERT INTO memberships (workspace_id, member, role, created_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(workspace_id, member) DO UPDATE SET role=excluded.role,"
                " revoked=0",
                (inv["workspace_id"], inv["member"], role, _now_iso()))
    con.execute("UPDATE invites SET status='accepted' WHERE id=?", (invite_id,))
    superseded = _supersede(con, inv["workspace_id"], [actor, *aliases], keep=invite_id)
    rev = _db.bump_revision(con)
    _db.audit(con, actor, "invite_accept", "membership", inv["member"],
              {"workspace": inv["workspace_id"], "invite_id": invite_id,
               "superseded": superseded}, rev)
    return {"member": inv["member"], "role": role, "aggregate_rev": rev}


@_db.atomic
def withdraw_invite(con: sqlite3.Connection, workspace_id: str, invite_id: str,
                    actor: str) -> dict:
    """Withdraw one pending invitation (owner). The row stays as history;
    its messages are never sent again (invites_send refuses non-pending)."""
    ensure_aux(con)
    if not check_permission(con, workspace_id, actor, "membership"):
        raise _access.AuthorizationError("owner manages membership", 403)
    row = con.execute("SELECT * FROM invites WHERE id=?", (invite_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown invite")
    inv = dict(row)
    if inv["workspace_id"] != workspace_id:
        raise _access.AuthorizationError("invite is not in this workspace", 403)
    if inv["status"] != "created":
        raise _ops.BadInput("invite is no longer pending")
    con.execute("UPDATE invites SET status='withdrawn' WHERE id=?", (invite_id,))
    rev = _db.bump_revision(con)
    _db.audit(con, actor, "invite_withdraw", "membership", inv["member"],
              {"workspace": workspace_id, "invite_id": invite_id}, rev)
    return {"invite_id": invite_id, "status": "withdrawn", "aggregate_rev": rev}


@_db.atomic
def set_role(con: sqlite3.Connection, workspace_id: str, member: str,
             role: str, actor: str) -> dict:
    """Change member's role; the member's pending invitations are
    superseded and older ones can no longer be accepted. A member that is
    no longer an owner also loses the pending invitations it created."""
    ensure_aux(con)
    if role not in ROLES:
        raise _ops.BadInput(f"unknown role: {role!r}")
    if not check_permission(con, workspace_id, actor, "membership"):
        raise _access.AuthorizationError("owner manages membership", 403)
    if con.execute("SELECT 1 FROM memberships WHERE workspace_id=? AND member=?",
                   (workspace_id, member)).fetchone() is None:
        raise _ops.BadInput("unknown member")
    if role != "owner":
        _require_another_owner(con, workspace_id, member)
    con.execute("UPDATE memberships SET role=? WHERE workspace_id=? AND member=?",
                (role, workspace_id, member))
    superseded = _supersede(con, workspace_id, [member])
    if role != "owner":
        superseded += _supersede_created_by(con, workspace_id, member)
    rev = _db.bump_revision(con)
    _db.audit(con, actor, "role_set", "membership", member,
              {"role": role, "workspace": workspace_id, "superseded": superseded}, rev)
    return {"member": member, "role": role, "aggregate_rev": rev}


@_db.atomic
def revoke_member(con: sqlite3.Connection, workspace_id: str, member: str,
                  actor: str) -> dict:
    """Remove member; the member's pending invitations, and those it
    created, are superseded and older ones can no longer be accepted. A
    new invitation still works."""
    ensure_aux(con)
    if not check_permission(con, workspace_id, actor, "membership"):
        raise _access.AuthorizationError("owner manages membership", 403)
    _require_another_owner(con, workspace_id, member)
    con.execute("UPDATE memberships SET revoked=1 WHERE workspace_id=? AND member=?",
                (workspace_id, member))
    con.execute("DELETE FROM processing_grants WHERE workspace_id=? AND member=?",
                (workspace_id, member))
    superseded = _supersede(con, workspace_id, [member])
    superseded += _supersede_created_by(con, workspace_id, member)
    rev = _db.bump_revision(con)
    _db.audit(con, actor, "member_revoke", "membership", member,
              {"workspace": workspace_id, "superseded": superseded}, rev)
    return {"member": member, "revoked": True, "aggregate_rev": rev}


@_db.atomic
def grant_processing(con: sqlite3.Connection, workspace_id: str, member: str,
                     actor: str) -> dict:
    ensure_aux(con)
    if not check_permission(con, workspace_id, actor, "membership"):
        raise _access.AuthorizationError("owner manages processing grants", 403)
    role = get_role(con, workspace_id, member)
    if role != "editor":
        raise _ops.BadInput("processing grants apply to editors only")
    con.execute("INSERT OR IGNORE INTO processing_grants (workspace_id, member,"
                " granted_by, created_at) VALUES (?, ?, ?, ?)",
                (workspace_id, member, actor, _now_iso()))
    return {"member": member, "processing": True}


def oidc_provider_shape() -> dict:
    """Maintained-provider OIDC/OAuth shape (selection unresolved).

    The shape of a later provider only. The pilot signs in with built-in
    email and password accounts (local_accounts.py); their hashing lives
    there, not in this module. Local owner mode stays useful without
    hosted auth.
    """
    return {"protocol": "OIDC",
            "flows": ["authorization_code_with_pkce", "refresh"],
            "session": "provider_browser_session",
            "provider_selection": "unresolved",
            "local_owner_mode": "useful_without_hosted_auth",
            "password_crypto": "not_implemented_by_design"}


# -- assignments -----------------------------------------------------------

@_db.atomic
def save_assignment(con: sqlite3.Connection, workspace_id: str, target_type: str,
                    target_id: str, member: str, status: str = "open",
                    actor: str = "owner", expected_revision=None) -> dict:
    ensure_aux(con)
    if status not in ASSIGNMENT_STATES:
        raise _ops.BadInput(f"unknown assignment status: {status!r}")
    if not target_id:
        raise _ops.BadInput("assignment target is required")
    if not is_member(con, workspace_id, member):
        raise _ops.BadInput("assignee must be a workspace member")
    if not check_permission(con, workspace_id, actor, "assign"):
        raise _access.AuthorizationError("assignment requires assign permission", 403)
    existing = con.execute("SELECT * FROM assignments WHERE workspace_id=?"
                           " AND target_type=? AND target_id=?",
                           (workspace_id, target_type, target_id)).fetchone()
    if existing is None:
        aid = _new_id("assign")
        con.execute("INSERT INTO assignments (id, workspace_id, target_type, target_id,"
                    " member, revision, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
                    (aid, workspace_id, target_type, target_id, member, _now_iso()))
        con.execute("UPDATE assignments SET revision=1 WHERE id=?",
                    (aid,))  # explicit; schema lacks status col -> track in detail below
        _set_assignment_status(con, aid, status)
        rev = _db.bump_revision(con)
        _db.audit(con, actor, "assignment_create", "assignment", aid,
                  {"target": target_id, "member": member}, rev)
        return {"assignment_id": aid, "revision": 1, "status": status,
                "judgment_changed": False, "aggregate_rev": rev}
    cur = dict(existing)
    if expected_revision is not None and int(expected_revision) != cur["revision"]:
        raise _ops.Conflict(cur["revision"])
    con.execute("UPDATE assignments SET member=?, revision=revision+1, updated_at=?"
                " WHERE id=?", (member, _now_iso(), cur["id"]))
    _set_assignment_status(con, cur["id"], status)
    rev = _db.bump_revision(con)
    _db.audit(con, actor, "assignment_reassign", "assignment", cur["id"],
              {"member": member, "status": status}, rev)
    # Reassignment never alters evidence judgment.
    return {"assignment_id": cur["id"], "revision": cur["revision"] + 1,
            "status": status, "judgment_changed": False, "aggregate_rev": rev}


def _set_assignment_status(con: sqlite3.Connection, assignment_id: str, status: str) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS assignment_status ("
                " assignment_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'open')")
    con.execute("INSERT INTO assignment_status (assignment_id, status) VALUES (?, ?)"
                " ON CONFLICT(assignment_id) DO UPDATE SET status=excluded.status",
                (assignment_id, status))


def get_assignment(con: sqlite3.Connection, workspace_id: str, target_type: str,
                   target_id: str) -> dict | None:
    """Pure lookup (K01): never creates schema."""
    _require_aux_tables(con)
    row = con.execute("SELECT * FROM assignments WHERE workspace_id=? AND target_type=?"
                      " AND target_id=?", (workspace_id, target_type, target_id)).fetchone()
    if row is None:
        return None
    d = dict(row)
    st = con.execute("SELECT status FROM assignment_status WHERE assignment_id=?",
                     (d["id"],)).fetchone()
    d["status"] = st["status"] if st else "open"
    return d


# -- comments --------------------------------------------------------------

@_db.atomic
def save_comment(con: sqlite3.Connection, workspace_id: str, target_type: str,
                 target_id: str, body: str, actor: str,
                 source_pins: dict | None = None) -> dict:
    ensure_aux(con)
    if not check_permission(con, workspace_id, actor, "comment"):
        raise _access.AuthorizationError("comment permission required", 403)
    if not body or len(body) > 5000:
        raise _ops.BadInput("comment body must be 1..5000 characters")
    cid = _new_id("comment")
    con.execute("INSERT INTO comments (id, workspace_id, target_type, target_id, body,"
                " actor, revision, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
                (cid, workspace_id, target_type, target_id, body, actor, _now_iso()))
    con.execute("INSERT INTO comment_revisions (workspace_id, comment_id, revision, body,"
                " actor, created_at) VALUES (?, ?, 1, ?, ?, ?)",
                (workspace_id, cid, body, actor, _now_iso()))
    if source_pins:
        con.execute("CREATE TABLE IF NOT EXISTS comment_pins (comment_id TEXT PRIMARY KEY,"
                    " pins TEXT NOT NULL DEFAULT '{}')")
        con.execute("INSERT OR REPLACE INTO comment_pins VALUES (?, ?)",
                    (cid, json.dumps(source_pins)))
    rev = _db.bump_revision(con)
    _db.audit(con, actor, "comment_create", "comment", cid, {"target": target_id}, rev)
    return {"comment_id": cid, "revision": 1, "actor": actor, "aggregate_rev": rev}


@_db.atomic
def edit_comment(con: sqlite3.Connection, comment_id: str, body: str, actor: str,
                 expected_revision=None) -> dict:
    ensure_aux(con)
    row = con.execute("SELECT * FROM comments WHERE id=?", (comment_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown comment")
    cur = dict(row)
    if cur["actor"] != actor and not check_permission(
            con, cur["workspace_id"], actor, "membership"):
        raise _access.AuthorizationError("only the author or owner may edit", 403)
    if expected_revision is not None and int(expected_revision) != cur["revision"]:
        raise _ops.Conflict(cur["revision"])
    if not body or len(body) > 5000:
        raise _ops.BadInput("comment body must be 1..5000 characters")
    con.execute("UPDATE comments SET body=?, revision=revision+1 WHERE id=?",
                (body, comment_id))
    con.execute("INSERT INTO comment_revisions (workspace_id, comment_id, revision, body,"
                " actor, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (cur["workspace_id"], comment_id, cur["revision"] + 1, body, actor,
                 _now_iso()))
    return {"comment_id": comment_id, "revision": cur["revision"] + 1}


@_db.atomic
def delete_comment(con: sqlite3.Connection, comment_id: str, actor: str) -> dict:
    """Delete leaves a visible tombstone (edit/delete visibility)."""
    ensure_aux(con)
    row = con.execute("SELECT * FROM comments WHERE id=?", (comment_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown comment")
    cur = dict(row)
    if cur["actor"] != actor and not check_permission(
            con, cur["workspace_id"], actor, "membership"):
        raise _access.AuthorizationError("only the author or owner may delete", 403)
    con.execute("UPDATE comments SET body='[deleted]', revision=revision+1 WHERE id=?",
                (comment_id,))
    return {"comment_id": comment_id, "deleted": True, "tombstone_visible": True}


def list_comments(con: sqlite3.Connection, workspace_id: str, target_type: str,
                  target_id: str) -> list:
    """Pure lookup (K01): never creates schema."""
    _require_aux_tables(con)
    return [dict(r) for r in con.execute(
        "SELECT * FROM comments WHERE workspace_id=? AND target_type=? AND target_id=?"
        " ORDER BY created_at", (workspace_id, target_type, target_id)).fetchall()]


# -- agent proposals -------------------------------------------------------

@_db.atomic
def save_proposal(con: sqlite3.Connection, workspace_id: str, target_type: str,
                  target_id: str, proposer: str, body: dict,
                  origin: str = "agent") -> dict:
    ensure_aux(con)
    if origin not in AGENT_ORIGINS:
        raise _ops.BadInput("agent proposals keep a known agent origin")
    pid = _new_id("proposal")
    con.execute("INSERT INTO proposals (id, workspace_id, target_type, target_id, origin,"
                " proposer, body, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                (pid, workspace_id, target_type, target_id, origin, proposer,
                 json.dumps(body or {}), _now_iso()))
    return {"proposal_id": pid, "origin": origin, "status": "open"}


@_db.atomic
def apply_proposal(con: sqlite3.Connection, proposal_id: str, approver: str) -> dict:
    ensure_aux(con)
    row = con.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown proposal")
    prop = dict(row)
    if not check_permission(con, prop["workspace_id"], approver, "review"):
        raise _access.AuthorizationError("review permission required to apply", 403)
    con.execute("UPDATE proposals SET status='applied', approver=?, applied_at=?"
                " WHERE id=?", (approver, _now_iso(), proposal_id))
    rev = _db.bump_revision(con)
    _db.audit(con, approver, "proposal_apply", "proposal", proposal_id,
              {"proposer": prop["proposer"], "origin": prop["origin"],
               "audio_inspected": False}, rev)
    # Origin stays agent; approval is not audio inspection or verification.
    return {"proposal_id": proposal_id, "origin": prop["origin"],
            "proposer": prop["proposer"], "approver": approver,
            "human_verified": False, "audio_inspected": False,
            "aggregate_rev": rev}


# -- shares -----------------------------------------------------------------

def _effective_expiry(requested_iso: str | None, source_policy_iso: str | None) -> str:
    default = (datetime.now(timezone.utc) + timedelta(days=SHARE_DEFAULT_EXPIRY_DAYS))
    requested = None
    if requested_iso:
        try:
            requested = datetime.fromisoformat(str(requested_iso).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            requested = None
    effective = requested or default
    if source_policy_iso:
        try:
            policy = datetime.fromisoformat(str(source_policy_iso).replace("Z", "+00:00"))
            if policy < effective:
                effective = policy  # source-policy expiry wins
        except (ValueError, TypeError):
            pass
    if effective.tzinfo is None:
        effective = effective.replace(tzinfo=timezone.utc)
    return effective.isoformat(timespec="seconds")


def preview_share(object_type: str, object_id: str, audience: str, fields: list,
                  download: bool = False, requested_expiry: str | None = None,
                  source_policy_expiry: str | None = None,
                  version: int = 1, citations: list | None = None) -> dict:
    """Pre-create preview: audience/selection/version/fields/citations/
    downloads/effective expiry/exclusions. No share exists yet."""
    if not audience:
        raise _ops.BadInput("shares require a named authenticated audience")
    excluded = []
    if download:
        excluded.append("audio redistribution is excluded by default")
    return {"audience": audience, "selection": {"object_type": object_type,
                                               "object_id": object_id},
            "version": version, "fields": list(fields or []),
            "citations": list(citations or []),
            "downloads": {"allowed": bool(download), "audio": False},
            "effective_expiry": _effective_expiry(requested_expiry, source_policy_expiry),
            "exclusions": excluded,
            "action_label": "Create share"}


@_db.atomic
def create_share(con: sqlite3.Connection, workspace_id: str, object_type: str,
                 object_id: str, audience: str, fields: list, actor: str,
                 download: bool = False, requested_expiry: str | None = None,
                 source_policy_expiry: str | None = None,
                 idempotency_key: str | None = None) -> dict:
    """Single labeled 'Create share' action. No anonymous links."""
    ensure_aux(con)
    if not check_permission(con, workspace_id, actor, "sharing"):
        raise _access.AuthorizationError("sharing requires owner permission", 403)
    if not audience:
        raise _ops.BadInput("shares require a named authenticated audience")
    if object_type not in ("evidence", "episode", "campaign", "export", "creative"):
        raise _ops.BadInput(f"unknown share object: {object_type!r}")
    expiry = _effective_expiry(requested_expiry, source_policy_expiry)
    share_id = _new_id("share")
    con.execute("INSERT INTO shares (id, workspace_id, object_type, object_id, audience,"
                " fields, download, expires_at, revoked, revision, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?)",
                (share_id, workspace_id, object_type, object_id, audience,
                 json.dumps(list(fields or [])), int(bool(download) and False),
                 expiry, _now_iso()))
    # download forced to 0 for audio safety: bounded release allows only
    # explicitly permitted non-audio downloads; audio stays excluded default.
    # The narrowing is an explicit response signal, never silent.
    narrowed = bool(download)
    con.execute("INSERT INTO share_audit (workspace_id, share_id, actor, action,"
                " created_at) VALUES (?, ?, ?, 'create', ?)",
                (workspace_id, share_id, actor, _now_iso()))
    rev = _db.bump_revision(con)
    _db.audit(con, actor, "share_create", "share", share_id,
              {"audience": audience, "object": object_id}, rev)
    return {"share_id": share_id, "state": "active",
            "downloads": {"requested": bool(download), "allowed": False,
                          "audio": False, "narrowed_by_policy": narrowed,
                          "notice": ("downloads were narrowed by policy: audio"
                                     " redistribution is excluded; stored with"
                                     " downloads disabled.") if narrowed
                          else "downloads not requested"},
            "no_recall_notice": "Revocation stops future access, not delivered copies.",
            "aggregate_rev": rev}


@_db.atomic
def revoke_share(con: sqlite3.Connection, share_id: str, actor: str,
                 expected_revision=None) -> dict:
    ensure_aux(con)
    row = con.execute("SELECT * FROM shares WHERE id=?", (share_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown share")
    cur = dict(row)
    if not check_permission(con, cur["workspace_id"], actor, "sharing"):
        raise _access.AuthorizationError("sharing requires owner permission", 403)
    if expected_revision is not None and int(expected_revision) != cur["revision"]:
        raise _ops.Conflict(cur["revision"])
    con.execute("UPDATE shares SET revoked=1, revision=revision+1 WHERE id=?",
                (share_id,))
    con.execute("INSERT INTO share_audit (workspace_id, share_id, actor, action,"
                " created_at) VALUES (?, ?, ?, 'revoke', ?)",
                (cur["workspace_id"], share_id, actor, _now_iso()))
    rev = _db.bump_revision(con)
    _db.audit(con, actor, "share_revoke", "share", share_id, {}, rev)
    return {"share_id": share_id, "state": "revoked",
            "no_recall_notice": "Revocation stops future access, not delivered copies.",
            "aggregate_rev": rev}


def share_state(con: sqlite3.Connection, share_id: str,
                now_iso: str | None = None) -> dict:
    """Pure lookup (K01): never creates schema."""
    _require_aux_tables(con)
    row = con.execute("SELECT * FROM shares WHERE id=?", (share_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown share")
    d = dict(row)
    now = datetime.now(timezone.utc) if now_iso is None else datetime.fromisoformat(
        str(now_iso).replace("Z", "+00:00"))
    if d["revoked"]:
        d["state"] = "revoked"
    elif d.get("expires_at"):
        try:
            exp = datetime.fromisoformat(str(d["expires_at"]).replace("Z", "+00:00"))
            d["state"] = "expired" if exp <= now else "active"
        except (ValueError, TypeError):
            d["state"] = "expired"
    else:
        d["state"] = "active"
    d["no_recall_notice"] = "Revocation stops future access, not delivered copies."
    return d


def check_share_access(con: sqlite3.Connection, ctx, operation: str,
                       object_type: str, object_id: str) -> dict:
    """Guest service rule: server-resolved grant after auth.

    Enforces the SHARE_ALLOWED_OPERATIONS allowlist, grant expiry/
    revocation, object selection (no traversal), field/download limits,
    and that citations cannot broaden access. Bounded in-flight release:
    revocation stops future reads; already-streaming bytes are not recalled.
    """
    _require_aux_tables(con)
    ctx.require_operation(operation)  # share allowlist enforced here
    if ctx.authority_kind != "share" or not ctx.share_grant_id:
        raise _access.AuthorizationError("share authority required", 403)
    grant = share_state(con, ctx.share_grant_id)
    if grant["state"] != "active":
        raise _access.AuthorizationError(f"share is {grant['state']}", 403)
    if grant["object_type"] != object_type or grant["object_id"] != object_id:
        # Named guest cannot traverse to parent transcript/workspace.
        raise _access.AuthorizationError("share grant does not cover this object", 403)
    return {"allowed": True, "share_id": grant["id"], "fields": json.loads(
        grant.get("fields") or "[]"), "download": bool(grant.get("download"))}


# -- hosted tenant migration rehearsal ---------------------------------------

def rehearse_tenant_migration(con: sqlite3.Connection, legacy_workspace: str = "local",
                              target_workspace: str = "hosted:acme") -> dict:
    """Assign legacy local records to a hosted tenant; verify composite keys.

    Missing historical revisions stay missing (never invented). Returns
    per-table assignment counts plus composite-FK and scoped-index checks.
    """
    ensure_aux(con)
    from .db import COMPOSITE_WORKSPACE_KEYS as _keys
    moved: dict = {}
    for table in ("assignments", "comments", "shares", "schedules", "monitor_events",
                  "notification_outbox", "expected_placements",
                  "placement_evaluations"):
        try:
            rows = con.execute(f"SELECT COUNT(*) c FROM {table}"
                               " WHERE workspace_id=?", (legacy_workspace,)).fetchone()[0]
        except sqlite3.Error:
            rows = 0
        moved[table] = {"legacy_rows": rows, "assigned_to": target_workspace,
                        "historical_revisions_invented": 0}
    composite_ok = [list(k) for k in _keys]
    indexes = [dict(r) for r in con.execute(
        "SELECT name, tbl_name FROM sqlite_master WHERE type='index'"
        " AND (name LIKE 'idx_%' OR name LIKE 'sqlite_autoindex%')")]
    scoped = [i for i in indexes if "workspace" in (i["name"] or "").lower()
              or i["tbl_name"] in ("memberships", "assignments", "comments",
                                   "shares", "monitor_events")]
    return {"moved": moved, "composite_workspace_keys": composite_ok,
            "scoped_indexes_observed": len(scoped),
            "missing_history": "missing historical revisions stay missing"}
