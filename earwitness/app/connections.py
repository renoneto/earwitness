"""Hosted connection grants: OAuth linkage, consent, status, revocation.

Package E (hosted code only; no live provider, no deploy). One grant
authorizes exactly one workspace. Tokens are opaque random strings stored
as hashes; a token resolves to its server-controlled grant on every
request — never trust a tool's workspace_id, a header, or an actor
string. All timestamps are UTC ISO-8601.

Security defaults (§9): access tokens live 10 minutes; refresh tokens
rotate with reuse detection and a 30-day absolute cap; revoked grants
are never resurrected by refresh.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass

ACCESS_TTL_S = 600
REFRESH_CAP_DAYS = 30
REFRESH_CAP_S = REFRESH_CAP_DAYS * 86400

# Connection states for the Agent-connections surface (§12 hosted journey).
NOT_CONFIGURED = "Not configured"
AWAITING_AUTHORIZATION = "Awaiting authorization"
CONNECTED = "Connected"
EXPIRED = "Expired"
REVOKED = "Revoked"
UNSUPPORTED = "Unsupported"
ACCESS_REMOVED = "Access removed"
UNAVAILABLE = "Unavailable"

ROLE_RANK = {"viewer": 1, "editor": 2, "owner": 3}
PRINCIPAL_FOR_ROLE = {"viewer": "viewer", "editor": "editor", "owner": "owner"}

_GRANTS_DDL = """
CREATE TABLE IF NOT EXISTS connection_grants (
    id TEXT PRIMARY KEY,
    member_id TEXT NOT NULL,
    member_role TEXT NOT NULL,
    client_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
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
    refresh_rotated_at TEXT
);
CREATE TABLE IF NOT EXISTS consent_records (
    grant_id TEXT PRIMARY KEY,
    scopes TEXT NOT NULL,
    content_categories TEXT NOT NULL,
    policy_rev INTEGER NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revocation_ledger (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    grant_id TEXT NOT NULL,
    revoked_at TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS used_refresh_hashes (
    hash TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL,
    used_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    grant_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    object_ids TEXT NOT NULL DEFAULT '[]',
    outcome TEXT NOT NULL,
    latency_ms REAL NOT NULL DEFAULT 0,
    bytes_out INTEGER NOT NULL DEFAULT 0,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_registry (
    workspace_id TEXT PRIMARY KEY,
    db_path TEXT NOT NULL,
    row_namespace TEXT NOT NULL DEFAULT 'local',
    db_identity TEXT NOT NULL DEFAULT '',
    is_owner INTEGER NOT NULL DEFAULT 0,
    bound_issuer TEXT NOT NULL DEFAULT '',
    bound_subject TEXT NOT NULL DEFAULT '',
    registered_by TEXT NOT NULL DEFAULT '',
    registered_at TEXT NOT NULL
);
"""


class GrantError(Exception):
    """Denied hosted access; carries an HTTP status for adapters.

    Unknown grant/token identifiers deny as 404 NOT_FOUND (indistinguishable
    from absent) or 401 for bearer failures per §13; never leak names,
    counts, hashes, or revisions.
    """

    def __init__(self, code: str, message: str, status: int = 403):
        super().__init__(message)
        self.code = code
        self.status = status


class UnknownToken(GrantError):
    """No opaque credential matched; only this case may try provider claims."""


def _now_iso(now: float | None = None) -> str:
    import datetime
    moment = now if now is not None else time.time()
    return datetime.datetime.fromtimestamp(
        moment, datetime.timezone.utc).isoformat()


def _now_ts(now: float | None = None) -> float:
    return now if now is not None else time.time()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


@dataclass
class Grant:
    id: str
    member_id: str
    member_role: str
    client_id: str
    workspace_id: str
    scopes: frozenset
    content_policy: dict
    policy_rev: int
    created_at: str
    expires_at: str
    revoked_at: str | None
    last_used_at: str | None
    refresh_created_at: str | None = None
    refresh_rotated_at: str | None = None
    access_hash: str | None = None


class GrantsStore:
    """SQLite-backed grant/consent/ledger/audit store.

    Opens a connection per call and closes it (never share thread-affine
    handles with SDK workers). Suitable for tests via :memory: or tmp
    files; production uses a dedicated auth database file.
    """

    def __init__(self, path: str):
        from urllib.parse import quote
        self._keeper = None
        if path == ":memory:":
            self.path = "file:ew-control-" + secrets.token_hex(16) + "?mode=memory&cache=shared"
            self._keeper = sqlite3.connect(self.path, uri=True, check_same_thread=False)
            con = self._keeper
        else:
            self.path = str(path)
            con = sqlite3.connect(self.path)
        try:
            con.executescript(_GRANTS_DDL)
            # Pilot rights records (hosted runtime 5.2.1) are part of every
            # control store from the start, so approving one never changes
            # the pinned control schema (L5p N9).
            from .rights_admission import ADDENDUM_DDL as _ADDENDUM_DDL
            from .rights_admission import DDL as _RIGHTS_DDL
            con.executescript(_RIGHTS_DDL)
            # So are the model-processing addenda (Ask design 8.1).
            con.executescript(_ADDENDUM_DDL)
            # So are the purchase tables (purchase design 13, BY3): the
            # window and the webhook never change the pinned schema.
            from .purchases import create_tables as _purchase_tables
            _purchase_tables(con)
            con.commit()
        finally:
            if self._keeper is None:
                con.close()

    def _connect(self) -> sqlite3.Connection:
        from urllib.parse import quote
        uri = self.path if self.path.startswith("file:ew-control-") else \
            "file:" + quote(self.path, safe="/") + "?mode=rw"
        con = sqlite3.connect(uri, uri=True)
        con.row_factory = sqlite3.Row
        return con

    @classmethod
    def open_existing(cls, path):
        """Prepared control storage only: no creation or admission-time DDL."""
        store = cls.__new__(cls)
        store.path = str(path)
        store._keeper = None
        try:
            con = store._connect()
        except sqlite3.Error as exc:
            raise GrantError("INTERNAL", "control storage requires explicit setup", 503) from exc
        try:
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"connection_grants", "workspace_registry", "agent_audit"} <= tables:
                raise GrantError("INTERNAL", "control schema requires explicit setup", 503)
        finally:
            con.close()
        return store

    def auth_connection(self) -> sqlite3.Connection:
        """Server-side connection for browser sessions and the workspace
        registry. Opened and closed per call by the caller."""
        return self._connect()

    def list_registered_workspaces(self, *, control=None) -> list[str]:
        """Registered external workspace identities (no paths)."""
        owned = control is None
        con = control if control is not None else self._connect()
        try:
            return [r["workspace_id"] for r in con.execute(
                "SELECT workspace_id FROM workspace_registry ORDER BY workspace_id"
            ).fetchall()]
        except sqlite3.Error:
            return []
        finally:
            if owned:
                con.close()

    @staticmethod
    def _row_to_grant(row: sqlite3.Row) -> Grant:
        return Grant(
            id=row["id"], member_id=row["member_id"],
            member_role=row["member_role"], client_id=row["client_id"],
            workspace_id=row["workspace_id"],
            scopes=frozenset(json.loads(row["scopes"])),
            content_policy=json.loads(row["content_policy"] or "{}"),
            policy_rev=row["policy_rev"], created_at=row["created_at"],
            expires_at=row["expires_at"], revoked_at=row["revoked_at"],
            last_used_at=row["last_used_at"],
            refresh_created_at=row["refresh_created_at"],
            refresh_rotated_at=row["refresh_rotated_at"], access_hash=row["access_hash"])

    def issue_grant(self, *, member_id: str, member_role: str, client_id: str,
                    workspace_id: str, scopes, content_policy: dict,
                    policy_rev: int = 1,
                    now: float | None = None) -> dict:
        """Create a one-workspace grant with fresh token pair. Returns the
        grant id plus the only copies of the plaintext tokens."""
        if member_role not in ROLE_RANK:
            raise GrantError("INVALID_ARGUMENT", "unknown role", 400)
        moment = _now_ts(now)
        grant_id = "cg_" + secrets.token_urlsafe(18)
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        stamp = _now_iso(moment)
        con = self._connect()
        try:
            con.execute(
                "INSERT INTO connection_grants (id, member_id, member_role,"
                " client_id, workspace_id, scopes, content_policy, policy_rev,"
                " created_at, expires_at, access_hash, refresh_hash,"
                " refresh_created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (grant_id, member_id, member_role, client_id, workspace_id,
                 json.dumps(sorted(set(scopes))), json.dumps(content_policy),
                 policy_rev, stamp,
                 _now_iso(moment + ACCESS_TTL_S), _hash_token(access),
                 _hash_token(refresh), stamp))
            con.commit()
        finally:
            con.close()
        return {"grant_id": grant_id, "access_token": access,
                "refresh_token": refresh, "expires_at": _now_iso(
                    moment + ACCESS_TTL_S)}

    def record_consent(self, grant_id: str, *, scopes,
                       content_categories, policy_rev: int,
                       now: float | None = None, control=None) -> None:
        """With control, runs on the caller's open transaction and never
        commits or closes it (personal tokens, design 4.3)."""
        owned = control is None
        con = self._connect() if owned else control
        try:
            exists = con.execute("SELECT 1 FROM connection_grants WHERE id=?",
                                 (grant_id,)).fetchone()
            if not exists:
                raise GrantError("NOT_FOUND", "unknown grant", 404)
            con.execute("INSERT OR REPLACE INTO consent_records (grant_id,"
                        " scopes, content_categories, policy_rev, recorded_at)"
                        " VALUES (?,?,?,?,?)",
                        (grant_id, json.dumps(sorted(set(scopes))),
                         json.dumps(list(content_categories)), policy_rev,
                         _now_iso(now)))
            if owned:
                con.commit()
        finally:
            if owned:
                con.close()

    def get_grant(self, grant_id: str) -> Grant:
        con = self._connect()
        try:
            row = con.execute("SELECT * FROM connection_grants WHERE id=?",
                              (grant_id,)).fetchone()
            if not row:
                raise GrantError("NOT_FOUND", "unknown grant", 404)
            return self._row_to_grant(row)
        finally:
            con.close()

    def resolve_token(self, access_token: str, *, now=None) -> Grant:
        """Bearer -> grant via hash lookup. Unknown tokens deny as 401
        (§13 invalid token); never distinguish unknown from malformed."""
        if not access_token or not isinstance(access_token, str):
            raise GrantError("UNAUTHORIZED", "missing token", 401)
        if not access_token.isascii():
            # Never a valid opaque token or JWT; deny, never 500.
            raise GrantError("UNAUTHORIZED", "invalid token", 401)
        digest = _hash_token(access_token)
        con = self._connect()
        try:
            row = con.execute("SELECT * FROM connection_grants WHERE "
                              "access_hash=?", (digest,)).fetchone()
            if not row:
                raise UnknownToken("UNAUTHORIZED", "invalid token", 401)
            return authorize_request(self, self._row_to_grant(row), now=now)
        finally:
            con.close()

    def touch_used(self, grant_id: str, now: float | None = None) -> None:
        con = self._connect()
        try:
            con.execute("UPDATE connection_grants SET last_used_at=? WHERE id=?",
                        (_now_iso(now), grant_id))
            con.commit()
        finally:
            con.close()

    def revoke_grant(self, grant_id: str, reason: str,
                     now: float | None = None, control=None) -> None:
        """Revoke + ledger entry. Revoked grants are never resurrected:
        refresh of a revoked grant denies instead of reissuing. Token
        hashes are wiped, so a revoked opaque bearer 401s like an
        unknown token (indistinguishable); the REVOKED state stays
        visible via status and the ledger, never the bearer response.
        With control, runs on the caller's open transaction and never
        commits or closes it."""
        stamp = _now_iso(now)
        owned = control is None
        con = self._connect() if owned else control
        try:
            row = con.execute("SELECT revoked_at FROM connection_grants WHERE "
                              "id=?", (grant_id,)).fetchone()
            if not row:
                raise GrantError("NOT_FOUND", "unknown grant", 404)
            if row["revoked_at"]:
                return
            _revoke_on(con, grant_id, reason, stamp)
            if owned:
                con.commit()
        finally:
            if owned:
                con.close()

    def use_refresh(self, refresh_token: str,
                    now: float | None = None) -> dict:
        """Rotate a refresh token. Reuse of a rotated token revokes the
        grant (reuse detection); refresh past the 30-day cap denies."""
        moment = _now_ts(now)
        digest = _hash_token(refresh_token)
        con = self._connect()
        try:
            reused = con.execute("SELECT grant_id FROM used_refresh_hashes "
                                 "WHERE hash=?", (digest,)).fetchone()
            if reused:
                con.execute("UPDATE connection_grants SET revoked_at=?,"
                            " access_hash=NULL, refresh_hash=NULL WHERE id=?",
                            (_now_iso(moment), reused["grant_id"]))
                con.execute("INSERT INTO revocation_ledger (grant_id,"
                            " revoked_at, reason) VALUES (?,?,?)",
                            (reused["grant_id"], _now_iso(moment),
                             "refresh reuse detected"))
                con.commit()
                raise GrantError("FORBIDDEN", "refresh reuse detected", 403)
            row = con.execute("SELECT * FROM connection_grants WHERE "
                              "refresh_hash=?", (digest,)).fetchone()
            if not row:
                raise GrantError("UNAUTHORIZED", "invalid refresh", 401)
            grant = self._row_to_grant(row)
            if grant.revoked_at:
                raise GrantError("FORBIDDEN", "grant revoked", 403)
            created = _parse_ts(grant.refresh_created_at)
            if moment - created > REFRESH_CAP_S:
                raise GrantError("UNAUTHORIZED", "refresh lifetime exceeded",
                                 401)
            access = secrets.token_urlsafe(32)
            refresh = secrets.token_urlsafe(32)
            con.execute("INSERT INTO used_refresh_hashes (hash, grant_id,"
                        " used_at) VALUES (?,?,?)",
                        (digest, grant.id, _now_iso(moment)))
            con.execute("UPDATE connection_grants SET access_hash=?,"
                        " refresh_hash=?, refresh_rotated_at=?,"
                        " expires_at=? WHERE id=?",
                        (_hash_token(access), _hash_token(refresh),
                         _now_iso(moment), _now_iso(moment + ACCESS_TTL_S),
                         grant.id))
            con.commit()
        finally:
            con.close()
        return {"grant_id": grant.id, "access_token": access,
                "refresh_token": refresh}

    def audit(self, *, request_id: str, grant_id: str, principal_id: str,
              workspace_id: str, operation: str, object_ids=(),
              outcome: str, latency_ms: float = 0, bytes_out: int = 0,
              now: float | None = None, control=None) -> None:
        """Operational metadata only: never tokens, bodies, notes, or terms.
        With control, runs on the caller's open transaction and never
        commits or closes it."""
        owned = control is None
        con = self._connect() if owned else control
        try:
            con.execute(
                "INSERT INTO agent_audit (request_id, grant_id, principal_id,"
                " workspace_id, operation, object_ids, outcome, latency_ms,"
                " bytes_out, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (request_id, grant_id, principal_id, workspace_id, operation,
                 json.dumps([str(o) for o in object_ids][:50]), outcome,
                 latency_ms, bytes_out, _now_iso(now)))
            if owned:
                con.commit()
        finally:
            if owned:
                con.close()


def _revoke_on(con, grant_id: str, reason: str, stamp: str) -> None:
    """Wipe the hashes and write the ledger row, on the caller's connection."""
    con.execute("UPDATE connection_grants SET revoked_at=?,"
                " access_hash=NULL, refresh_hash=NULL WHERE id=?",
                (stamp, grant_id))
    con.execute("INSERT INTO revocation_ledger (grant_id, revoked_at,"
                " reason) VALUES (?,?,?)", (grant_id, stamp, reason))


def _parse_ts(stamp: str) -> float:
    import datetime
    return datetime.datetime.fromisoformat(stamp).timestamp()


# Personal agent tokens (agent access design, revision 3, section 4).
# One connection_grants row each, issued from a browser session for the
# member themself. Read-only scopes only; no refresh; the lifetime is the
# chosen days capped at the pilot end. Astra r1 accepted the 7-day default
# and 14-day maximum as a bounded pilot exception; issue_grant keeps its
# 10-minute lifetime for every other grant.
PERSONAL_TOKEN_PREFIX = "ewt_"
PERSONAL_CLIENT_PREFIX = "pat:"
PERSONAL_TOKEN_SCOPES = ("research:read", "transcripts:read", "watchlists:read",
                         "campaigns:read", "notes:read")
PERSONAL_TOKEN_DEFAULT_SCOPES = ("research:read", "transcripts:read",
                                 "watchlists:read", "campaigns:read")
PERSONAL_TOKEN_DAYS = (1, 7, 14)
PERSONAL_TOKEN_DEFAULT_DAYS = 7
PERSONAL_TOKEN_CAP = 3
PERSONAL_TOKEN_MIN_LEFT_S = 3600
PERSONAL_LABEL_DEFAULT = "Claude Code"


def _personal_label_ok(label) -> bool:
    import re
    return isinstance(label, str) and bool(re.fullmatch(r"[A-Za-z0-9 ._-]{1,40}", label))


def personal_token_expiry(now: float, days: int, pilot_end: str) -> str:
    """min(now + days, pilot end) as an ISO instant. Refused (409) when the
    pilot end is missing, unreadable or less than an hour away."""
    from .rights_admission import parse_deployed_end
    try:
        end = parse_deployed_end(pilot_end).timestamp()
    except ValueError:
        raise GrantError("PILOT_END", "agent tokens need a pilot end date", 409)
    if end - now < PERSONAL_TOKEN_MIN_LEFT_S:
        raise GrantError("PILOT_END", "the pilot ends too soon for a new agent token", 409)
    return _now_iso(min(now + days * 86400, end))


def _personal_rows(control, workspace_id: str, member_id: str | None):
    # Exact, case-sensitive prefix (LIKE ignores ASCII case).
    sql = ("SELECT * FROM connection_grants WHERE workspace_id=?"
           " AND substr(client_id, 1, ?)=?")
    args = [workspace_id, len(PERSONAL_CLIENT_PREFIX), PERSONAL_CLIENT_PREFIX]
    if member_id is not None:
        sql += " AND member_id=?"
        args.append(member_id)
    return control.execute(sql + " ORDER BY created_at DESC, id", args).fetchall()


def count_active_tokens(control, workspace_id: str, member_id: str, now: float) -> int:
    """Unrevoked, unexpired personal tokens of one member in one workspace."""
    return sum(1 for row in _personal_rows(control, workspace_id, member_id)
               if not row["revoked_at"] and _parse_ts(row["expires_at"]) > now)


def issue_personal_token(control, *, member_id: str, member_role: str,
                         workspace_id: str, label: str, scopes,
                         expires_at: str, now: float | None = None) -> dict:
    """Insert one personal-token grant on the caller's open control
    transaction. Only the token's SHA-256 is stored; the plaintext is
    returned once. Never commits or closes control."""
    if member_role not in ROLE_RANK:
        raise GrantError("INVALID_ARGUMENT", "unknown role", 400)
    if not control.in_transaction:
        raise GrantError("INTERNAL", "personal tokens need a control write", 500)
    grant_id = "cg_" + secrets.token_urlsafe(18)
    token = PERSONAL_TOKEN_PREFIX + secrets.token_urlsafe(32)
    control.execute(
        "INSERT INTO connection_grants (id, member_id, member_role, client_id,"
        " workspace_id, scopes, content_policy, policy_rev, created_at,"
        " expires_at, access_hash, refresh_hash, refresh_created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
        (grant_id, member_id, member_role, PERSONAL_CLIENT_PREFIX + label,
         workspace_id, json.dumps(sorted(set(scopes))), json.dumps({}), 1,
         _now_iso(now), expires_at, _hash_token(token)))
    return {"grant_id": grant_id, "token": token, "expires_at": expires_at}


def list_member_grants(control, workspace_id: str, member_id: str | None = None) -> list:
    """Personal tokens in a workspace (one member's, or all with None).
    Never returns a token or a hash. last_used_at is the grant's own
    column, which record_success updates on every successful agent call."""
    out = []
    for row in _personal_rows(control, workspace_id, member_id):
        out.append({"id": row["id"], "member_id": row["member_id"],
                    "label": row["client_id"][len(PERSONAL_CLIENT_PREFIX):],
                    "scopes": sorted(json.loads(row["scopes"])),
                    "created_at": row["created_at"], "expires_at": row["expires_at"],
                    "revoked_at": row["revoked_at"], "last_used_at": row["last_used_at"]})
    return out


def personal_grant(control, workspace_id: str, grant_id: str):
    """One personal-token row in this workspace, or None."""
    if not isinstance(grant_id, str) or not grant_id:
        return None
    return control.execute(
        "SELECT * FROM connection_grants WHERE id=? AND workspace_id=?"
        " AND substr(client_id, 1, ?)=?",
        (grant_id, workspace_id, len(PERSONAL_CLIENT_PREFIX),
         PERSONAL_CLIENT_PREFIX)).fetchone()


PERSONAL_ISSUED_ACTION = "agent_token_issued"


def record_personal_issuance(con, namespace: str, grant_id: str, member_id: str) -> None:
    """Write the token's issuance into the workspace audit, on the
    request's workspace write (AGT-SEC-R2-01). Its audit position is the
    token's membership generation; no revision bump, nothing changes for
    readers."""
    from . import db as _db
    _db.audit(con, member_id, PERSONAL_ISSUED_ACTION, "agent_token", grant_id,
              {"workspace": namespace}, _db.aggregate_revision(con))


def personal_token_current(con, namespace: str, grant_id: str, member_id: str) -> bool:
    """True only when the workspace holds this token's issuance record and
    no removal or role decision for the member was recorded after it. The
    check reads the workspace database alone, which commits membership
    changes before the control store: a lost control commit (hash wipe,
    ledger) can never bring an old token back."""
    from . import collaboration as _collab
    issued = con.execute(
        "SELECT MAX(id) FROM audit WHERE action=? AND target_type='agent_token'"
        " AND target_id=?", (PERSONAL_ISSUED_ACTION, grant_id)).fetchone()[0]
    if issued is None:
        return False
    last = _collab._last_decision_seq(con, namespace, [member_id])
    return last is None or last < issued


def revoke_member_tokens(control, *, workspace_id: str, member_id: str,
                         reason: str, now: float | None = None) -> list:
    """Revoke every unrevoked personal token of one member in one
    workspace, on the caller's open control transaction (membership
    removal or a role change: the loss is permanent, so a re-invite or a
    re-promotion brings no token back). Never commits or closes control."""
    if not control.in_transaction:
        raise GrantError("INTERNAL", "token revocation needs a control write", 500)
    stamp = _now_iso(now)
    revoked = []
    for row in _personal_rows(control, workspace_id, member_id):
        if not row["revoked_at"]:
            _revoke_on(control, row["id"], reason, stamp)
            revoked.append(row["id"])
    return revoked


def authorize_request(store: GrantsStore, grant: Grant, *,
                      required_scopes=(), workspace_id: str | None = None,
                      member_role: str | None = None,
                      policy_rev: int | None = None,
                      now: float | None = None) -> Grant:
    """Per-request grant validation. Stale role/grant/policy deny; the
    grant's workspace wins over any request-supplied workspace id
    (workspace mismatch denies as indistinguishable NOT_FOUND)."""
    moment = _now_ts(now)
    if grant.revoked_at:
        raise GrantError("FORBIDDEN", "grant revoked", 403)
    if moment >= _parse_ts(grant.expires_at):
        raise GrantError("UNAUTHORIZED", "grant expired", 401)
    if workspace_id is not None and workspace_id != grant.workspace_id:
        raise GrantError("NOT_FOUND", "unknown object", 404)
    if member_role is not None and member_role != grant.member_role:
        raise GrantError("FORBIDDEN", "stale role", 403)
    if policy_rev is not None and policy_rev != grant.policy_rev:
        raise GrantError("FORBIDDEN", "stale policy", 403)
    missing = [s for s in required_scopes if s not in grant.scopes]
    if missing:
        raise GrantError("FORBIDDEN", "missing scope", 403)
    return grant


def resolve_claims(store: GrantsStore, claims: dict, *, issuer: str,
                   audience: str, now: float | None = None) -> Grant:
    """Provider-token path, after the signature is verified: validate
    the claims, then load the server-side grant named by the private
    grant claim (``egrant``). The token must belong to the grant: its
    ``oidc:<iss>:<sub>`` principal is the grant's member and its
    ``client_id`` (else ``azp``) is the grant's client. Effective scopes
    are the token's scopes within the grant's; none in common denies.
    The workspace always comes from the grant, never from a request
    field. Every grant failure (unknown, revoked, expired, mismatched,
    malformed) is the same 401 as a bad token, so grant existence and
    state never leak."""
    from dataclasses import replace
    from earwitness.app.browser_auth import principal_identity
    check_claims(claims, issuer=issuer, audience=audience, now=now)
    denied = GrantError("UNAUTHORIZED", "invalid token", 401)
    grant_id = claims.get("egrant")
    if not grant_id or not isinstance(grant_id, str):
        raise denied
    try:
        grant = store.get_grant(grant_id)
    except GrantError:
        raise denied
    if grant.revoked_at or _now_ts(now) >= _parse_ts(grant.expires_at):
        raise denied
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject or \
            principal_identity(claims["iss"], subject) != grant.member_id:
        raise denied
    client = claims["client_id"] if "client_id" in claims else claims.get("azp")
    if not isinstance(client, str) or not client or client != grant.client_id:
        raise denied
    scope_field = claims.get("scope")
    if not isinstance(scope_field, str):
        raise denied
    effective = frozenset(scope_field.split()) & grant.scopes
    if not effective:
        raise denied
    return replace(grant, scopes=effective)


def build_context(grant: Grant, request_id: str):
    """Server-constructed AccessContext from the authorized grant. The
    workspace, principal, scopes, connection, client, and policy revision
    all come from the grant — request bodies contribute business
    arguments only."""
    from earwitness.app import access as _access
    return _access.AccessContext.create(
        principal_id=grant.member_id,
        principal_kind=PRINCIPAL_FOR_ROLE.get(grant.member_role, "viewer"),
        workspace_id=grant.workspace_id, scopes=set(grant.scopes),
        policy_revision=grant.policy_rev, request_id=request_id,
        connection_id=grant.id, client_id=grant.client_id,
        credential_kind="reader_bearer", content_policy=grant.content_policy,
        policy_identity=json.dumps([grant.policy_rev, sorted(grant.scopes),
                                    grant.content_policy], sort_keys=True),
        credential_hash=grant.access_hash)


def status_of(store: GrantsStore, grant_id: str, *,
              capability: str = "ok",
              now: float | None = None) -> str:
    """Hosted connection state for the Agent-connections surface."""
    try:
        grant = store.get_grant(grant_id)
    except GrantError:
        return NOT_CONFIGURED
    moment = _now_ts(now)
    if grant.revoked_at:
        return REVOKED
    if moment >= _parse_ts(grant.expires_at):
        return EXPIRED
    if capability == "unsupported":
        return UNSUPPORTED
    if capability == "removed":
        return ACCESS_REMOVED
    if capability == "down":
        return UNAVAILABLE
    con = store._connect()
    try:
        ctx = build_context(grant, "status")
        rows = con.execute("SELECT object_ids FROM agent_audit WHERE grant_id=?"
                           " AND outcome='success' ORDER BY seq DESC", (grant_id,)).fetchall()
        for row in rows:
            try:
                binding = json.loads(row["object_ids"])
            except (TypeError, ValueError):
                continue
            if binding.get("policy") != ctx.policy_identity or binding.get("credential") != ctx.credential_hash:
                continue
            reg = con.execute("SELECT * FROM workspace_registry WHERE workspace_id=?",
                              (grant.workspace_id,)).fetchone()
            from . import services, db, access
            if reg is None or reg["db_identity"] != binding.get("db_identity") or \
                    binding.get("registry_policy") != services.registry_policy(dict(reg)):
                continue
            product = db.open_existing(reg["db_path"], readonly=True)
            try:
                with db.transaction(product, write=False):
                    ctx.disclosures = {tuple(locator) for locator in binding.get("disclosures", [])}
                    services._workspace_authority(product, ctx, dict(reg))
            except access.AuthorizationError:
                return ACCESS_REMOVED
            finally:
                product.close()
            return CONNECTED
    finally:
        con.close()
    return AWAITING_AUTHORIZATION


# Clock skew for provider-token expiry and not-before. One value, used
# here and by agent_verify.ProviderTokenVerifier.
CLOCK_SKEW_S = 30
TIME_CLAIMS = ("exp", "iat", "nbf")


def finite_time(value) -> bool:
    """A JWT NumericDate: a finite int or float, never bool. NaN and
    Infinity parse from JSON but compare false or unbounded."""
    import math
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def check_claims(claims: dict, *, issuer: str, audience: str,
                 now: float | None = None,
                 required_scopes=()) -> dict:
    """Pure OAuth claim validation (no network): exact issuer, audience,
    expiry (with CLOCK_SKEW_S; exp/iat/nbf must be finite, non-bool
    numbers when present), and scopes. Signature, algorithm,
    ``typ``, not-before and lifetime belong to the provider verifier
    (agent_verify); this function runs only after it. Raises GrantError."""
    moment = _now_ts(now)
    if not all(finite_time(claims[name]) for name in TIME_CLAIMS
               if name in claims):
        raise GrantError("UNAUTHORIZED", "invalid token", 401)
    if claims.get("iss") != issuer:
        raise GrantError("UNAUTHORIZED", "wrong issuer", 401)
    aud = claims.get("aud", [])
    aud = [aud] if isinstance(aud, str) else list(aud)
    if audience not in aud:
        raise GrantError("UNAUTHORIZED", "wrong audience", 401)
    if moment >= float(claims.get("exp", 0)) + CLOCK_SKEW_S:
        raise GrantError("UNAUTHORIZED", "token expired", 401)
    scope_field = claims.get("scope", "")
    have = set(scope_field.split()) if isinstance(scope_field, str) else set()
    missing = [s for s in required_scopes if s not in have]
    if missing:
        raise GrantError("FORBIDDEN", "missing scope", 403)
    return claims


def reauthorize_chunks(authorize, chunks):
    """Yield body chunks while the grant reauthorizes before each one.

    Mid-transfer withdrawal terminates the incomplete response: the
    already-released prefix may finish (§9 in-flight rule); no further
    chunk is released after denial. Denial stops iteration (it is the
    expected terminal signal, not an error); unexpected exceptions
    propagate.
    """
    for chunk in chunks:
        try:
            authorize()
        except GrantError:
            return
        yield chunk


_WORKSPACE_ID_RE = None


def _workspace_id_pattern():
    global _WORKSPACE_ID_RE
    if _WORKSPACE_ID_RE is None:
        import re
        _WORKSPACE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
    return _WORKSPACE_ID_RE


def _contained_db_path(data_dir: str, db_path: str) -> str | None:
    """Canonical DB path strictly inside the server data_dir, or None.

    Both sides resolve through realpath first, so already-canonical
    absolute paths, `..` segments, and symlinks (on either side) are
    judged by where they actually point. The data_dir itself never
    qualifies; only files strictly beneath it do.
    """
    import os
    if not isinstance(db_path, str) or not db_path:
        return None
    real_data = os.path.realpath(data_dir or "")
    if not real_data:
        return None
    real_db = os.path.realpath(db_path)
    if real_db == real_data:
        return None
    if not real_db.startswith(real_data + os.sep):
        return None
    return real_db


def workspace_id_registration(con, workspace_id: str):
    """The registry row for an ID on an open control connection, or None."""
    return con.execute(
        "SELECT workspace_id, db_path, db_identity, is_owner FROM workspace_registry"
        " WHERE workspace_id=?", (workspace_id,)).fetchone()


def register_workspace(store: GrantsStore, workspace_id: str, db_path: str,
                       data_dir: str, row_namespace: str = "local",
                       is_owner: bool = False, operator: str = "", *, control=None,
                       reregister_same_file: bool = False,
                       relocate_same_identity: bool = False) -> dict:
    """Operator-controlled workspace registration (server side only).

    Maps one external workspace identity to one exact verified database
    file and its internal row namespace. The mapping is fixed per
    registered database and never supplied by HTTP JSON: db_path must be
    an existing file strictly under the server data_dir (unknown keys
    create nothing), and only the 'local' row namespace is addressable
    by this build's SQL. Records the database's stable identity for
    verification on every open. Re-registration by the operator replaces
    the entry only when it names the same file and the caller passes
    reregister_same_file=True; that keeps the account binding and cannot
    flip ownership. A registered ID is never pointed at a different file
    and never replaced silently.

    relocate_same_identity is for the local reader setup only (a moved or
    re-spelled folder): it accepts a new db_path when the stored
    db_identity equals the file's verified identity and is_owner is
    unchanged, keeps the binding, and updates db_path. With both flags
    (local setup only) any verified file in the folder is accepted, so a
    moved folder whose file was also replaced still re-registers. The
    hosted create and operator paths never pass relocate_same_identity.
    """
    import os
    if not isinstance(workspace_id, str) or not _workspace_id_pattern().match(workspace_id):
        raise GrantError("INVALID_ARGUMENT", "unknown workspace", 404)
    if row_namespace != "local":
        raise GrantError("INVALID_ARGUMENT", "unsupported row namespace", 400)
    real_db = _contained_db_path(data_dir, db_path)
    if real_db is None:
        raise GrantError("INVALID_ARGUMENT", "unknown workspace", 404)
    if not os.path.isfile(real_db):
        raise GrantError("NOT_FOUND", "unknown workspace", 404)
    owned = control is None
    con = control or store._connect()
    try:
        db = sqlite3.connect(real_db)
        try:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT value FROM meta WHERE key='workspace_id'").fetchone()
            identity = row["value"] if row else ""
        finally:
            db.close()
        if not identity:
            raise GrantError("INVALID_ARGUMENT", "workspace has no verified identity",
                             400)
        if owned:
            con.execute("BEGIN IMMEDIATE")
        elif not con.in_transaction:
            raise GrantError("INTERNAL", "registration needs a control write"
                             " transaction", 500)
        # Check and write share one control write transaction.
        existing = workspace_id_registration(con, workspace_id)
        if existing is None:
            con.execute(
                "INSERT INTO workspace_registry (workspace_id, db_path,"
                " row_namespace, db_identity, is_owner, registered_by, registered_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (workspace_id, real_db, row_namespace, identity,
                 1 if is_owner else 0, str(operator or "")[:120], _now_iso()))
        else:
            same_file = reregister_same_file and existing["db_path"] == real_db
            # Local setup passes both flags: the control store and the data
            # folder are the user's own, so any verified file there is
            # accepted (ownership still cannot change).
            relocated = relocate_same_identity and (
                existing["db_identity"] == identity or reregister_same_file)
            if not (same_file or relocated) \
                    or bool(existing["is_owner"]) != bool(is_owner):
                raise GrantError("CONFLICT", "workspace id already registered", 409)
            # Explicit re-registration: the account binding stays.
            con.execute(
                "UPDATE workspace_registry SET db_path=?, row_namespace=?,"
                " db_identity=?, registered_by=?, registered_at=?"
                " WHERE workspace_id=?",
                (real_db, row_namespace, identity, str(operator or "")[:120],
                 _now_iso(), workspace_id))
        if owned:
            con.commit()
    except BaseException:
        if owned:
            con.rollback()
        raise
    finally:
        if owned:
            con.close()
    return {"workspace_id": workspace_id, "db_path": real_db,
            "row_namespace": row_namespace, "db_identity": identity,
            "is_owner": bool(is_owner)}


def resolve_workspace(store: GrantsStore, workspace_id: str, *, control=None) -> dict:
    """Resolve a registered workspace or raise NOT_FOUND (no path exposed,
    no file created). Misconfigured namespaces fail closed. `paid_use` and
    `paid_use_reason` come from rights_admission.paid_use."""
    owned = control is None
    con = control if control is not None else store._connect()
    try:
        row = con.execute("SELECT workspace_id, db_path, row_namespace,"
                          " db_identity, is_owner, bound_issuer, bound_subject"
                          " FROM workspace_registry WHERE workspace_id=?",
                          (workspace_id,)).fetchone()
        if row is not None:
            # RB-R1-01: whether this workspace may serve paid use, read with
            # the registry row; every paid path checks entry["paid_use"].
            from .rights_admission import paid_use as _paid_use
            paid = _paid_use(con, row["db_identity"])
    finally:
        if owned:
            con.close()
    if row is None:
        raise GrantError("NOT_FOUND", "unknown workspace", 404)
    entry = dict(row)
    entry["paid_use"], entry["paid_use_reason"] = paid
    entry["is_owner"] = bool(entry["is_owner"])
    if entry["row_namespace"] != "local":
        raise GrantError("INTERNAL", "workspace misconfigured", 500)
    return entry


def bind_workspace_account(store: GrantsStore, workspace_id: str, issuer: str,
                           subject: str, operator: str = "") -> dict:
    """Explicit local-operator bind of an authenticated (issuer, subject)
    to a registered workspace after checking it. OIDC login never claims
    a workspace by itself; until bound, hosted access to an owner
    database is denied. Grants no reader scopes and rewrites no agent
    identity."""
    if not isinstance(issuer, str) or not issuer.startswith("https://") \
            or len(issuer) > 500:
        raise GrantError("INVALID_ARGUMENT", "issuer must be an https URL", 400)
    if not isinstance(subject, str) or not subject or len(subject) > 300:
        raise GrantError("INVALID_ARGUMENT", "subject is required", 400)
    entry = resolve_workspace(store, workspace_id)
    con = store._connect()
    try:
        con.execute("UPDATE workspace_registry SET bound_issuer=?, bound_subject=?"
                    " WHERE workspace_id=?",
                    (issuer, subject, workspace_id))
        con.commit()
    finally:
        con.close()
    entry["bound_issuer"] = issuer
    entry["bound_subject"] = subject
    return {"workspace_id": workspace_id, "issuer": issuer,
            "subject": subject, "bound_by": str(operator or "")[:120]}


def open_registered_db(store: GrantsStore, workspace_id: str, data_dir: str, *, readonly=False, control=None):
    """Open the registered database for a workspace: resolve, re-verify
    the path is still under the server data_dir and the file still
    exists, then verify the database's stable identity matches the
    registered one. Never creates files; never reads client paths."""
    import os
    entry = resolve_workspace(store, workspace_id, control=control)
    real_db = _contained_db_path(data_dir, entry["db_path"])
    if real_db is None:
        raise GrantError("NOT_FOUND", "unknown workspace", 404)
    if not os.path.isfile(real_db):
        raise GrantError("NOT_FOUND", "unknown workspace", 404)
    from . import db
    con = db.open_existing(real_db, readonly=readonly)
    try:
        row = con.execute("SELECT value FROM meta WHERE key='workspace_id'").fetchone()
    except sqlite3.Error:
        con.close()
        raise GrantError("NOT_FOUND", "unknown workspace", 404)
    if row is None or row["value"] != entry["db_identity"]:
        con.close()
        raise GrantError("NOT_FOUND", "unknown workspace", 404)
    return con, entry


LOCAL_CONTROL_NAME = "reader-control.sqlite3"


def local_profile_binding(profile, db_identity):
    fields = {key: profile.get(key) for key in (
        "profile", "owner", "workspace_id", "scopes", "content_policy", "revoked", "expires_at")}
    return hashlib.sha256(json.dumps([db_identity, fields], sort_keys=True).encode()).hexdigest()


def _local_profile(con, scopes, *, now=None):
    from . import conversion
    conversion.require_executable(con, "reader_profile", "agent_profile")
    row = con.execute("SELECT value FROM meta WHERE key='agent_profile'").fetchone()
    identity = con.execute("SELECT value FROM meta WHERE key='workspace_id'").fetchone()
    if row is None or identity is None:
        raise GrantError("FORBIDDEN", "prepared reader profile required", 403)
    profile = json.loads(row["value"])
    allowed = {"research:read", "transcripts:read", "watchlists:read", "campaigns:read", "notes:read"}
    declared = set(profile.get("scopes") or [])
    if profile.get("profile") != "reader" or not profile.get("owner") or \
            not declared <= allowed or not set(scopes) <= declared or profile.get("revoked"):
        raise GrantError("FORBIDDEN", "reader profile does not admit this operation", 403)
    expiry = profile.get("expires_at")
    if expiry is not None:
        try:
            expired = _now_ts(now) >= _parse_ts(expiry)
        except (TypeError, ValueError):
            expired = True
        if expired:
            raise GrantError("UNAUTHORIZED", "reader profile expired", 401)
    return profile, identity["value"]


def prepare_local_reader_control(workspace, workspace_id="local", *,
                                 verification_context="local_reader"):
    """Explicit setup using the existing grants/registry/receipt control store."""
    from pathlib import Path
    from . import db
    path = Path(workspace).resolve()
    product = path / "workspace.sqlite3" if path.is_dir() else path
    con = db.open_existing(product, readonly=True)
    try:
        profile, identity = _local_profile(con, ())
    finally:
        con.close()
    if verification_context not in ("local_reader", "local_fixture"):
        raise ValueError("unsupported local verification context")
    control_path = product.parent / LOCAL_CONTROL_NAME
    store = GrantsStore(str(control_path))
    control_path.chmod(0o600)
    control = store._connect()
    try:
        with db.transaction(control, write=True):
            register_workspace(store, workspace_id, str(product), str(product.parent),
                               operator="local-profile-setup", control=control,
                               reregister_same_file=True,
                               relocate_same_identity=True)
            control.execute("INSERT INTO agent_audit (request_id,grant_id,principal_id,workspace_id,"
                            "operation,object_ids,outcome,recorded_at) VALUES (?,?,?,?,?,?,?,?)",
                            (secrets.token_hex(8), "", profile["owner"], workspace_id, "local_reader_setup",
                             json.dumps({"verification_context": verification_context,
                                         "profile_binding": local_profile_binding(profile, identity)}),
                             "prepared", _now_iso()))
    finally:
        control.close()
    return {"control_path": str(control_path), "workspace_id": workspace_id,
            "configured": True, "verified": False}


def local_reader_context(workspace_id, workspace, scopes):
    """Admission is pure and current; a copied launch command is not authority."""
    from pathlib import Path
    from . import db, access
    path = Path(workspace).resolve()
    product = path / "workspace.sqlite3" if path.is_dir() else path
    store = GrantsStore.open_existing(product.parent / LOCAL_CONTROL_NAME)
    con, entry = open_registered_db(store, workspace_id, str(product.parent), readonly=True)
    try:
        with db.transaction(con, write=False):
            profile, identity = _local_profile(con, scopes)
    finally:
        con.close()
    control = store._connect()
    try:
        prepared = control.execute("SELECT object_ids FROM agent_audit WHERE workspace_id=?"
                                   " AND operation='local_reader_setup' AND outcome='prepared'"
                                   " ORDER BY seq DESC LIMIT 1", (workspace_id,)).fetchone()
        if prepared is None:
            raise GrantError("FORBIDDEN", "local reader control setup is incomplete", 403)
        setup = json.loads(prepared["object_ids"])
    finally:
        control.close()
    binding = local_profile_binding(profile, identity)
    if setup.get("profile_binding") != binding:
        raise GrantError("FORBIDDEN", "reader profile changed; explicit control setup required", 403)
    ctx = access.AccessContext.create(
        principal_id=profile["owner"], principal_kind="owner", workspace_id=workspace_id,
        credential_kind="local_reader", scopes=set(scopes),
        content_policy=profile.get("content_policy") or {}, policy_identity=binding,
        db_path=entry["db_path"], db_identity=entry["db_identity"])
    ctx.control_store = store
    from . import services
    ctx.registry_policy = services.registry_policy(entry)
    ctx.verification_context = setup["verification_context"]
    return ctx


def local_reader_status(product_con):
    """Current actual local use evidence; no schemas or product rows are written."""
    from pathlib import Path
    from . import services, db, access
    filename = next((row[2] for row in product_con.execute("PRAGMA database_list") if row[1] == "main"), "")
    if not filename:
        return {"configured": False, "verified": False, "state": "not_configured"}
    path = Path(filename)
    control_path = path.parent / LOCAL_CONTROL_NAME
    if not control_path.is_file():
        return {"configured": False, "verified": False, "state": "not_configured"}
    store = GrantsStore.open_existing(control_path)
    control = store._connect()
    try:
        prepared = control.execute("SELECT workspace_id FROM agent_audit WHERE operation='local_reader_setup'"
                                   " AND outcome='prepared' ORDER BY seq DESC LIMIT 1").fetchone()
        if prepared is None:
            return {"configured": False, "verified": False, "state": "not_configured"}
        try:
            profile, identity = _local_profile(product_con, ())
            ctx = local_reader_context(prepared["workspace_id"], path, profile["scopes"])
        except GrantError:
            return {"configured": True, "verified": False, "state": "revoked"}
        receipts = control.execute("SELECT object_ids,recorded_at FROM agent_audit"
                                   " WHERE workspace_id=? AND principal_id=? AND outcome='success'"
                                   " ORDER BY seq DESC", (ctx.workspace_id, ctx.principal_id)).fetchall()
        for receipt in receipts:
            binding = json.loads(receipt["object_ids"])
            if binding.get("policy") != ctx.policy_identity or binding.get("db_identity") != identity or \
                    binding.get("verification_context") not in ("local_reader", "local_fixture"):
                continue
            ctx.disclosures = {tuple(locator) for locator in binding.get("disclosures", [])}
            try:
                services.fenced_authority_check(store, str(path.parent), ctx,
                                                 operation="local_reader_status", control=control)
            except (GrantError, access.AuthorizationError):
                continue
            context = binding["verification_context"]
            return {"configured": True, "verified": True, "state": context if context == "local_fixture" else "verified",
                    "verification_context": context, "verified_at": receipt["recorded_at"]}
        return {"configured": True, "verified": False, "state": "copied_unverified"}
    finally:
        control.close()
