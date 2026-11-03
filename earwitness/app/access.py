"""Shared authorization boundary: server-constructed access context and budget.

An AccessContext is built by the server/operator entrypoint from the
authenticated session, never from a request body. The constructor requires a
live _ServerToken that request JSON cannot supply, so model-supplied tool
arguments can never declare themselves human, owner, or guest.
"""
from __future__ import annotations

import time
import uuid

MEMBER_PRINCIPALS = {"owner", "editor", "viewer", "worker", "agent"}
AUTHORITY_KINDS = {"membership", "share", "bootstrap"}

# Server-only credential provenance (K02). Membership alone never widens
# the presented credential's restrictions: reader bearers stay read-only
# even when their principal holds an owner/editor role.
CREDENTIAL_KINDS = {"local_operator", "local_reader", "browser_session",
                    "reader_bearer", "model_session"}
# model_session (Ask, base design 4.2): derived from a member's live browser
# session by ask_tools.make_model_session; scopes decide what it reads
# (`permits`), it carries the origin session's id (session_id) and switch
# revision, and services._current_authority re-reads that session on every
# call. Audio and customer mutations refuse it (browser sessions only).

# The only membership checks a model_session passes: the six Ask agent tools
# (ask_tools.ALLOWLIST; their entrypoints call require_member with the tool
# name). Every other membership check refuses it (owner decision 2026-10-26).
MODEL_SESSION_OPERATIONS = frozenset({
    "search_transcripts", "get_transcript_window", "search_evidence",
    "get_evidence", "list_brands", "list_shows",
})

# Share-grant authority is limited to these explicitly listed read operations.
# Anything unlisted stays denied for guests without workspace membership.
SHARE_ALLOWED_OPERATIONS = frozenset({
    "search_evidence",
    "evidence_detail",
    "transcript_window",
    "brand_detail",
    "show_detail",
})


class _ServerToken:
    __slots__ = ()


SERVER = _ServerToken()


class AuthorizationError(Exception):
    """Denied access; carries an HTTP-style status code for adapters."""

    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status


class AccessLost(AuthorizationError):
    """The caller no longer has access to the workspace: membership,
    session or workspace binding is gone. Still 403 FORBIDDEN (other
    clients rely on it); the error body adds "access": "lost" so the
    customer app can tell it from a role or feature refusal."""

    access_lost = True

    def __init__(self, message: str):
        super().__init__(message, 403)


class RoleChanged(AuthorizationError):
    """The caller's workspace role changed after the request was admitted.
    Still 403 FORBIDDEN with the same message; the error body adds
    "reason": "role_changed" so the customer app can tell it apart without
    reading the message. A role change is not lost access."""

    role_changed = True

    def __init__(self, message: str = "workspace role changed"):
        super().__init__(message, 403)


class DisclosureRefused(AuthorizationError):
    """The workspace's source content policy refuses a category for a
    source (authorize_disclosure). Still 403 FORBIDDEN with the same
    message; the error body adds "refusal": "disclosure" so the customer
    app can say so without reading the message (FREEZE r2). own_policy
    (ALERT-DENY FIX2, ADC-R1-04): the source's own policy row refused, as
    opposed to a grant whose source list leaves the source out; server
    side only, never in the body."""

    refusal = "disclosure"

    def __init__(self, message: str = "source disclosure denied", status: int = 403, *,
                 own_policy: bool = False):
        super().__init__(message, status)
        self.own_policy = own_policy


class AccessContext:
    """Who acts, in which workspace, under which authority.

    Construct only via AccessContext.create() in a trusted entrypoint.
    Direct construction without the server token raises TypeError, which is
    what makes request-body forgery structurally impossible.
    """

    __slots__ = ("principal_id", "principal_kind", "workspace_id",
                 "connection_id", "client_id", "scopes", "policy_revision",
                 "request_id", "authority_kind", "share_grant_id",
                 "credential_kind", "effective_principal", "policy_identity",
                 "content_policy", "session_id", "db_path", "db_identity",
                 "disclosures", "recordings", "admitted_role", "completed_operations",
                 "credential_hash", "control_store", "verification_context",
                 "registry_policy", "session_binding", "audio_captures",
                 "session_switch_revision")

    def __init__(self, *, _server, principal_id: str, principal_kind: str,
                 workspace_id: str, scopes=(), policy_revision: int = 0,
                 request_id: str | None = None, connection_id: str = "",
                 client_id: str = "", authority_kind: str = "membership",
                 share_grant_id: str | None = None, credential_kind: str = "",
                 effective_principal: str | None = None,
                 policy_identity: str | None = None, content_policy=None,
                 session_id: str = "", db_path: str = "", db_identity: str = "",
                 credential_hash: str | None = None):
        if not isinstance(_server, _ServerToken):
            raise TypeError("AccessContext is server-constructed only; "
                            "use AccessContext.create() in a trusted entrypoint")
        if principal_kind not in MEMBER_PRINCIPALS | {"guest"}:
            raise ValueError(f"unknown principal_kind: {principal_kind!r}")
        if authority_kind not in AUTHORITY_KINDS:
            raise ValueError(f"unknown authority_kind: {authority_kind!r}")
        if authority_kind == "share" and not share_grant_id:
            raise ValueError("share authority requires a share_grant_id")
        if credential_kind not in CREDENTIAL_KINDS:
            raise ValueError(
                "explicit credential_kind is required: "
                f"{sorted(CREDENTIAL_KINDS)}")
        if credential_kind == "model_session" and not session_id:
            raise ValueError("a model_session requires its origin browser session")
        self.principal_id = principal_id
        self.principal_kind = principal_kind
        self.workspace_id = workspace_id
        self.connection_id = connection_id
        self.client_id = client_id
        self.scopes = frozenset(scopes)
        self.policy_revision = policy_revision
        self.request_id = request_id or uuid.uuid4().hex[:16]
        self.authority_kind = authority_kind
        self.share_grant_id = share_grant_id
        self.credential_kind = credential_kind
        self.effective_principal = (effective_principal if effective_principal
                                    is not None else principal_id)
        self.policy_identity = (policy_identity if policy_identity is not None
                                else f"{workspace_id}:{policy_revision}")
        self.content_policy = dict(content_policy or {})
        self.session_id = session_id
        self.db_path = db_path
        self.db_identity = db_identity
        self.disclosures = set()
        self.recordings = {}
        self.admitted_role = None
        self.completed_operations = set()
        self.credential_hash = credential_hash
        self.control_store = None
        self.verification_context = "hosted"
        self.registry_policy = None
        self.session_binding = None
        # Episodes whose audio this request served: the final workspace
        # check authorizes each again (authorize_recording_captures).
        self.audio_captures = set()
        # model_session only: the origin session's workspace_switch_revision
        # when the credential was made; any later switch refuses it.
        self.session_switch_revision = None

    def permits(self, category: str) -> bool:
        scope = {"transcript": "transcripts:read", "notes": "notes:read",
                 "metadata": "research:read", "excerpt": "research:read",
                 "campaign": "campaigns:read", "watchlist": "watchlists:read"}[category]
        if self.content_policy.get(category) in ("deny", False):
            return False
        fields = self.content_policy.get("fields")
        if fields is not None:
            allowed = set(fields)
            if self.is_share_guest:
                aliases = {"metadata": {"metadata", "show_title", "ep_title", "pub_date", "category", "fetch_date",
                                        "assocs", "judgment", "review", "attribution", "bounds",
                                        "orig_start_s", "orig_end_s", "source", "audio"},
                           "excerpt": {"excerpt", "text"},
                           "transcript": {"transcript", "segments", "interval_text", "text"},
                           "notes": {"notes"}}
                if not aliases[category] & allowed:
                    return False
            elif category not in allowed:
                return False
        return self.credential_kind in ("browser_session", "local_operator") or scope in self.scopes or \
            (category == "metadata" and "transcripts:read" in self.scopes)

    def authorize_disclosure(self, con, source_key: str, categories, *, export=False):
        sources = self.content_policy.get("sources")
        if sources is not None and source_key not in sources:
            raise DisclosureRefused("source disclosure denied", 403)
        for category in categories:
            if not self.permits(category):
                raise AuthorizationError("field disclosure denied", 403)
            row = con.execute("SELECT agent_disclosure, export_eligible"
                              " FROM source_content_policy WHERE workspace_id='local'"
                              " AND source_key=? AND content_category=?",
                              (source_key, category)).fetchone()
            if row and (row["agent_disclosure"] != "allow" or
                        (export and not row["export_eligible"])):
                raise DisclosureRefused("source disclosure denied", 403, own_policy=True)
            self.disclosures.add((source_key, category, export))

    def authorize_recording_captures(self, con):
        from . import citations
        # Audio served by this request: the audio row must still allow.
        for episode_id in sorted(self.audio_captures):
            self.authorize_audio(con, episode_id)
        for ref, identity in self.recordings.items():
            try:
                resolved = citations.resolve_source_state(con, ref, ctx=self)
            except citations.CitationError as exc:
                raise AuthorizationError("captured recording is unavailable", 403) from exc
            media = resolved.get("media") or {}
            if media.get("recording_id") != identity["artifact_id"] \
                    or media.get("recording_digest") != identity["digest"]:
                raise AuthorizationError("captured recording authority changed", 403)

    def require_audio_credential(self) -> None:
        """Audio is served only to a member's signed-in browser session
        (audio clips design 5.2). Reads nothing: no query, no file."""
        if self.credential_kind != "browser_session" or self.is_share_guest:
            raise AuthorizationError("audio is served only to a member's signed-in"
                                     " browser session", 403)

    def authorize_audio(self, con, episode_id: str, *, capture: bool = False) -> None:
        """The credential first, before any read; then the episode's audio
        row must exist and allow (a missing row refuses). capture: the
        request serves this audio, so the final check authorizes it again."""
        self.require_audio_credential()
        row = con.execute("SELECT agent_disclosure FROM source_content_policy"
                          " WHERE workspace_id='local' AND source_key=?"
                          " AND content_category='audio'", (f"episode:{episode_id}",)).fetchone()
        if row is None or row[0] != "allow":
            raise AuthorizationError("audio disclosure denied", 403)
        if capture:
            self.audio_captures.add(episode_id)

    def disclosure_allowed(self, con, source_key, category):
        try:
            self.authorize_disclosure(con, source_key, [category])
        except AuthorizationError:
            return False
        return True

    @classmethod
    def create(cls, *, principal_id: str, principal_kind: str,
               workspace_id: str, **kwargs) -> "AccessContext":
        return cls(_server=SERVER, principal_id=principal_id,
                   principal_kind=principal_kind, workspace_id=workspace_id,
                   **kwargs)

    @classmethod
    def from_request_body(cls, body: dict):
        """Always refused: request bodies cannot mint authority."""
        raise TypeError("AccessContext cannot be constructed from a request body")

    @property
    def is_share_guest(self) -> bool:
        return self.authority_kind == "share" or self.principal_kind == "guest"

    def require_member(self, operation: str) -> None:
        if self.credential_kind == "model_session" and operation not in MODEL_SESSION_OPERATIONS:
            raise AuthorizationError("Ask's model credential cannot use this", 403)
        if self.authority_kind != "membership" or self.principal_kind == "guest":
            raise AuthorizationError(f"operation requires workspace membership: {operation}", 403)

    def require_operation(self, operation: str) -> None:
        """Enforce the share-grant allowlist for guests; members pass."""
        if self.is_share_guest and operation not in SHARE_ALLOWED_OPERATIONS:
            raise AuthorizationError(f"share grant does not allow operation: {operation}", 403)

    def require_scope(self, scope: str) -> None:
        if scope not in self.scopes:
            raise AuthorizationError(f"missing scope: {scope}", 403)

    @property
    def is_reader_credential(self) -> bool:
        """Reader bearers and local readers are read-only: membership in an
        owner/editor role never widens them into writers."""
        return self.credential_kind in ("reader_bearer", "local_reader")


def require_customer_mutation(ctx: "AccessContext", operation: str) -> None:
    """Gate for member-owned customer resource mutations (K02).

    Workspace creation and exact invitation acceptance use separate
    operation-specific authenticated admission, not this member helper.
    Named-share reads use their audience/grant projection, never a
    fabricated member context. Reader credentials cannot create/accept
    even when their principal is owner/editor.
    """
    if ctx.credential_kind not in ("browser_session", "local_operator"):
        raise AuthorizationError(
            "reader credentials are read-only", 403)
    ctx.require_member(operation)


class ExecutionBudget:
    """One monotonic deadline plus cooperative cancellation per request."""

    def __init__(self, deadline_s: float = 10.0):
        if not deadline_s > 0:
            raise ValueError("deadline must be positive")
        self.deadline_s = deadline_s
        self._start = time.monotonic()
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def remaining_s(self) -> float:
        return max(0.0, self.deadline_s - (time.monotonic() - self._start))

    def check(self, what: str = "operation") -> None:
        if self._cancelled:
            raise TimeoutError(f"{what} cancelled")
        if self.remaining_s() <= 0:
            raise TimeoutError(f"{what} exceeded its {self.deadline_s}s budget")
