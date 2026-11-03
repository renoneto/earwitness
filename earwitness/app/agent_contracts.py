"""Agent-access contract layer (A-delta) on top of P00's shared contracts.

P00 owns access.py/contracts.py/search.py/citations.py/services.py. This
module owns ONLY the agent-access delta from docs/agent-access-plan.md
sections 6-9, 13-14: strict request models for all sixteen reader tools,
the standard output envelope with citations, bounds, the section 13 error
vocabulary, opaque signed cursors, and the upgrade-only legacy decoder
entry point for callers. No MCP dependency; stdlib only.

Service signature pattern per section 14:
    fn(context, request, *, budget) -> result
Adapters supply the trusted context and budget; request models carry only
the catalog's business arguments. The workspace connection is resolved
server-side from the context's workspace_id through bind_workspace(),
never from request input. Continuation cursors and projection-aware
revision derivation land in package B; any cursor supplied here is
rejected until B issues them.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import time
from collections import Counter
from pathlib import Path

from . import access as _access
from . import ad_reads as _ad_reads
from . import citations as _citations
from . import contracts as _contracts
from . import db as _db
from . import ops as _ops
from . import search as _search
from . import services as _services

SCHEMA_VERSION = 1

# -- Bounds and continuation (§6 table; server-enforced, adjustable) --------

LIST_DEFAULT = 20
LIST_HARD_MAX = 50
EXCERPT_LIST_MAX = 400
EXCERPT_DETAIL_MAX = 600
TRANSCRIPT_WINDOW_DEFAULT_S = 120
TRANSCRIPT_WINDOW_MAX_S = 300
TRANSCRIPT_WINDOW_MAX_SEGMENTS = 100
RESULT_AIM_BYTES = 16 * 1024
RESULT_HARD_BYTES = 64 * 1024
QUERY_TARGET_S = 5.0
QUERY_TIMEOUT_S = 10.0
CURSOR_TTL_S = 15 * 60
READS_PER_MINUTE = 60
READS_BURST = 20
WORKSPACE_READS_PER_MINUTE = 300
CONCURRENT_PER_CONNECTION = 2
CONCURRENT_PER_WORKSPACE = 10
EXPORT_MAX_ROWS = 2000
EXPORT_MAX_BYTES = 20 * 1024 * 1024
EXPORT_MAX_ACTIVE_JOBS = 2
TEXT_CHUNK_MAX = 2000

LIMITATIONS = {
    "CANDIDATES_NOT_UNIQUE_ADS": "Windows and candidates are not unique ads.",
    "NO_MATCH_IS_NOT_ABSENCE": "No match in the searched collection and filters;"
                                " not proof an ad never ran.",
    "AUDIO_ALIGNMENT_UNKNOWN": "Archived transcript and available audio are not"
                               " verified as aligned.",
    "HISTORICAL_VALIDATION_ONLY": "Historical bounded results, not accuracy claims"
                                  " for new queries.",
}

# -- Error vocabulary (§13; status identical across adapters) ----------------

INVALID_ARGUMENT = "INVALID_ARGUMENT"
UNAUTHENTICATED = "UNAUTHENTICATED"
FORBIDDEN = "FORBIDDEN"
NOT_FOUND = "NOT_FOUND"
PRECONDITION_REQUIRED = "PRECONDITION_REQUIRED"
REVISION_CONFLICT = "REVISION_CONFLICT"
RESULT_SET_CHANGED = "RESULT_SET_CHANGED"
CURSOR_EXPIRED = "CURSOR_EXPIRED"
REVISION_UNAVAILABLE = "REVISION_UNAVAILABLE"
IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
RATE_LIMITED = "RATE_LIMITED"
QUERY_TIMEOUT = "QUERY_TIMEOUT"
RESULT_TOO_LARGE = "RESULT_TOO_LARGE"
OPERATION_UNAVAILABLE = "OPERATION_UNAVAILABLE"
SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
INTERNAL = "INTERNAL"

_ERROR_STATUS = {
    INVALID_ARGUMENT: 400,
    UNAUTHENTICATED: 401,
    FORBIDDEN: 403,
    NOT_FOUND: 404,
    PRECONDITION_REQUIRED: 428,
    REVISION_CONFLICT: 412,
    RESULT_SET_CHANGED: 409,
    CURSOR_EXPIRED: 400,
    REVISION_UNAVAILABLE: 404,
    IDEMPOTENCY_CONFLICT: 409,
    RATE_LIMITED: 429,
    QUERY_TIMEOUT: 503,
    RESULT_TOO_LARGE: 413,
    OPERATION_UNAVAILABLE: 404,
    SOURCE_UNAVAILABLE: 503,
    INTERNAL: 500,
}


class AgentError(Exception):
    """A typed agent-contract failure with a section 13 code and HTTP status."""

    def __init__(self, code: str, message: str, status: int | None = None,
                 details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status = status if status is not None else _ERROR_STATUS.get(code, 500)
        self.details = dict(details or {})

    def envelope(self) -> dict:
        return {"code": self.code, "message": str(self),
                "status": self.status, "details": self.details}


def error_for_agent(exc: Exception) -> tuple[int, str, str]:
    """Map any service/contract exception to (status, code, message).

    P00 mappings are preserved identically; agent-contract failures add
    their section 13 code. Executed tool-domain failures surface with
    isError-style structured codes; authentication stays transport 401/403.
    """
    if isinstance(exc, AgentError):
        return (exc.status, exc.code, str(exc))
    if isinstance(exc, TimeoutError):
        return (_ERROR_STATUS[QUERY_TIMEOUT], QUERY_TIMEOUT, str(exc) or "query timed out")
    status, message = _contracts.error_for(exc)
    code = {
        _contracts.HTTP_BAD_REQUEST: INVALID_ARGUMENT,
        _contracts.HTTP_PRECONDITION_FAILED: REVISION_CONFLICT,
        _contracts.HTTP_PRECONDITION_REQUIRED: PRECONDITION_REQUIRED,
        404: NOT_FOUND,
    }.get(status, INTERNAL if status >= 500 else INVALID_ARGUMENT)
    if isinstance(exc, _access.AuthorizationError):
        code = FORBIDDEN
    return (status, code, message)


# -- Strict request parsing --------------------------------------------------

# Identity/authority material is never a business argument. Any of these in
# a tool request is rejected, never honored or silently dropped.
FORBIDDEN_REQUEST_FIELDS = frozenset({
    "actor", "principal", "principal_id", "principal_kind", "workspace_id",
    "workspace", "user", "user_id", "role", "roles", "scope", "scopes",
    "auth", "token", "grant", "grant_id", "connection", "connection_id",
    "client_id", "subject", "owner", "requester", "on_behalf_of",
})


def _parse_object(raw, tool: str) -> dict:
    if not isinstance(raw, dict):
        raise AgentError(INVALID_ARGUMENT, f"{tool} request must be a JSON object")
    forged = sorted(set(raw) & FORBIDDEN_REQUEST_FIELDS)
    if forged:
        raise AgentError(INVALID_ARGUMENT,
                         f"{tool} must not carry identity/authority fields: {forged}")
    return raw


def _check_unknown(raw: dict, tool: str, allowed: frozenset) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise AgentError(INVALID_ARGUMENT, f"{tool} rejects unknown fields: {unknown}")


def _finite_number(value, name: str, tool: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value):
        raise AgentError(INVALID_ARGUMENT, f"{name} must be a finite number")
    return float(value)


def _opaque_id(value, name: str, tool: str, limit: int = 200) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise AgentError(INVALID_ARGUMENT, f"{name} must be a non-empty opaque id")
    return value


def _int_id(value, name: str, tool: str) -> int:
    if isinstance(value, bool):
        raise AgentError(INVALID_ARGUMENT, f"{name} must be an integer id")
    if isinstance(value, int):
        ident = value
    elif isinstance(value, str) and value.isdigit():
        ident = int(value)
    else:
        raise AgentError(INVALID_ARGUMENT, f"{name} must be an integer id")
    if ident < 1:
        raise AgentError(INVALID_ARGUMENT, f"{name} must be a positive integer id")
    return ident


def _limit(value, tool: str, default: int = LIST_DEFAULT) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise AgentError(INVALID_ARGUMENT, "limit must be an integer")
    if not 1 <= value <= LIST_HARD_MAX:
        raise AgentError(INVALID_ARGUMENT, f"limit must be within 1..{LIST_HARD_MAX}")
    return value


def _text(value, name: str, tool: str, limit: int, required: bool = False) -> str:
    if value is None:
        if required:
            raise AgentError(INVALID_ARGUMENT, f"{name} is required")
        return ""
    if not isinstance(value, str):
        raise AgentError(INVALID_ARGUMENT, f"{name} must be a string")
    if len(value) > limit:
        raise AgentError(INVALID_ARGUMENT, f"{name} exceeds {limit} characters")
    if required and not value:
        raise AgentError(INVALID_ARGUMENT, f"{name} must not be empty")
    return value


def _optional_cursor(value, tool: str, detail: str | None = None) -> None:
    # Tools without cursor pagination reject rather than paginating from an
    # unverifiable position. Package B issues signed cursors for transcript
    # traversal and summary groups; entity lists and single pages stay
    # cursor-free by contract.
    if value is not None:
        raise AgentError(INVALID_ARGUMENT,
                         detail or f"{tool} takes no continuation cursor; resend the"
                         " query without one")


def _optional_text_cursor(value, tool: str) -> None:
    if value is not None:
        raise AgentError(INVALID_ARGUMENT,
                         f"{tool} long-text continuation lands in package B")


def parse_filter(raw, tool: str) -> dict:
    """Strict ResearchFilterV1 for agent requests; rejects, never broadens."""
    obj = _parse_object(raw if raw is not None else {}, tool)
    if not isinstance(obj, dict):
        raise AgentError(INVALID_ARGUMENT, "filter must be an object")
    try:
        return _search.validate_filter(obj)
    except _search.FilterRejected as exc:
        raise AgentError(INVALID_ARGUMENT, str(exc))


def parse_browser_evidence_params(qs: dict, tool: str = "browser.evidence") -> dict:
    """Strict gate for the browser/API evidence list (legacy query params).

    Same restrictive semantics as ResearchFilterV1; the returned mapping
    keeps the legacy ops shape (including page/per_page up to the browser's
    own 200-row bound) so the browser response envelope is unchanged. Any
    invalid restrictive value raises AgentError (HTTP 400), never broadens.
    """
    if not isinstance(qs, dict):
        raise AgentError(INVALID_ARGUMENT, "filter params must be an object")
    for key in ("actor", "principal", "principal_id", "principal_kind",
                "workspace_id", "owner", "scope", "scopes"):
        if key in qs:
            raise AgentError(INVALID_ARGUMENT,
                             f"browser filter must not carry {key!r}")
    allowed = {"q", "source", "review", "brand", "brand_basis", "show",
               "category", "episode", "pub_from", "pub_to",
               "include_unknown_dates", "history", "source_version", "sort",
               "page", "per_page", "version"}
    unknown = sorted(set(qs) - allowed)
    if unknown:
        raise AgentError(INVALID_ARGUMENT, f"browser filter rejects fields: {unknown}")
    legacy = {k: v for k, v in qs.items()
              if k not in ("page", "per_page", "version")}
    raw_unknown = legacy.get("include_unknown_dates", True)
    if isinstance(raw_unknown, str):
        if raw_unknown not in ("true", "false", "1", "0"):
            raise AgentError(INVALID_ARGUMENT,
                             "include_unknown_dates must be a boolean")
        legacy["include_unknown_dates"] = raw_unknown in ("true", "1")
    validated, problems = _search.decode_legacy_filter(legacy)
    if problems:
        raise AgentError(INVALID_ARGUMENT, "; ".join(problems))
    try:
        out = _search.to_legacy_filter(validated)
    except _search.FilterRejected as exc:
        raise AgentError(INVALID_ARGUMENT, str(exc))
    out["page"] = _browser_page(qs.get("page", 1), "page")
    out["per_page"] = _browser_page(qs.get("per_page", 50), "per_page",
                                    1, _ops.PER_PAGE_MAX)
    return out


def _browser_page(value, name: str, low: int = 1, high: int | None = None) -> int:
    if isinstance(value, bool):
        raise AgentError(INVALID_ARGUMENT, f"{name} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise AgentError(INVALID_ARGUMENT, f"{name} must be an integer")
    if number < low or (high is not None and number > high):
        bound = f"{low}..{high}" if high is not None else f">={low}"
        raise AgentError(INVALID_ARGUMENT, f"{name} must be within {bound}")
    return number


# -- Sixteen reader tool contracts (§7) --------------------------------------

RESEARCH_READ = "research:read"
TRANSCRIPTS_READ = "transcripts:read"
WATCHLISTS_READ = "watchlists:read"
CAMPAIGNS_READ = "campaigns:read"
NOTES_READ = "notes:read"

TOOL_SCOPES = {
    "get_workspace_context": (),
    "search_evidence": (RESEARCH_READ,),
    "get_evidence": (RESEARCH_READ,),
    "search_transcripts": (TRANSCRIPTS_READ,),
    "get_transcript_window": (TRANSCRIPTS_READ,),
    "list_brands": (RESEARCH_READ,),
    "list_shows": (RESEARCH_READ,),
    "list_episodes": (RESEARCH_READ,),
    "summarize_evidence": (RESEARCH_READ,),
    "list_watchlists": (WATCHLISTS_READ,),
    "get_watchlist_results": (WATCHLISTS_READ, RESEARCH_READ),
    "list_campaigns": (CAMPAIGNS_READ,),
    "get_campaign": (CAMPAIGNS_READ,),
    "get_source_provenance": (RESEARCH_READ,),
    "search": (),
    "fetch": (),
}

GROUP_BY_VALUES = ("source", "brand", "show", "review", "date")
CAMPAIGN_STATUS_VALUES = ("active", "archived", "all")


def _schema(tool: str, properties: dict, required: tuple = ()) -> dict:
    return {"type": "object", "title": tool, "properties": properties,
            "required": list(required), "additionalProperties": False}


def _id_prop(description: str) -> dict:
    return {"type": "string", "minLength": 1, "maxLength": 200,
            "description": description + " Opaque to callers."}


TOOL_SCHEMAS = {
    "get_workspace_context": _schema("get_workspace_context", {}),
    "search_evidence": _schema("search_evidence", {
        "filter": {"type": "object", "description": "ResearchFilterV1; strict."},
        "limit": {"type": "integer", "minimum": 1, "maximum": LIST_HARD_MAX},
        "cursor": {"type": ["string", "null"],
                   "description": "Opaque continuation; package B issues."},
    }),
    "get_evidence": _schema("get_evidence", {
        "evidence_id": _id_prop("Evidence id."),
        "revision": {"type": ["string", "null"],
                     "description": "Exact revision selector returned by search."},
        "text_cursor": {"type": ["string", "null"]},
    }, ("evidence_id",)),
    "search_transcripts": _schema("search_transcripts", {
        "query": {"type": "string", "minLength": 1, "maxLength": 200},
        "episode_id": _id_prop("Episode id filter."),
        "show_id": _id_prop("Show id filter."),
        "show_query": {"type": "string", "maxLength": 200},
        "published_from": {"type": "string"}, "published_to": {"type": "string"},
        "include_unknown_dates": {"type": "boolean"},
        "source_version_id": {"type": "string", "maxLength": 30},
        "limit": {"type": "integer", "minimum": 1, "maximum": LIST_HARD_MAX},
        "cursor": {"type": ["string", "null"]},
    }, ("query",)),
    "get_transcript_window": _schema("get_transcript_window", {
        "episode_id": _id_prop("Episode id."),
        "transcript_version_id": {"type": "integer", "minimum": 1,
                                  "description": "Exact pinned version; never"
                                                 " defaults to current."},
        "start_s": {"type": "number"}, "end_s": {"type": "number"},
        "cursor": {"type": ["string", "null"]},
    }, ("episode_id", "transcript_version_id", "start_s", "end_s")),
    "list_brands": _schema("list_brands", {
        "query": {"type": "string", "maxLength": 200},
        "filter": {"type": "object", "description": "ResearchFilterV1 scope."},
        "limit": {"type": "integer", "minimum": 1, "maximum": LIST_HARD_MAX},
        "cursor": {"type": ["string", "null"]},
    }),
    "list_shows": _schema("list_shows", {
        "query": {"type": "string", "maxLength": 200},
        "limit": {"type": "integer", "minimum": 1, "maximum": LIST_HARD_MAX},
        "cursor": {"type": ["string", "null"]},
    }),
    "list_episodes": _schema("list_episodes", {
        "show_id": _id_prop("Show id filter."),
        "query": {"type": "string", "maxLength": 200},
        "published_from": {"type": "string"}, "published_to": {"type": "string"},
        "include_unknown_dates": {"type": "boolean"},
        "limit": {"type": "integer", "minimum": 1, "maximum": LIST_HARD_MAX},
        "cursor": {"type": ["string", "null"]},
    }),
    "summarize_evidence": _schema("summarize_evidence", {
        "filter": {"type": "object", "description": "ResearchFilterV1; strict."},
        "group_by": {"type": "string", "enum": list(GROUP_BY_VALUES)},
        "limit": {"type": "integer", "minimum": 1, "maximum": LIST_HARD_MAX},
        "cursor": {"type": ["string", "null"]},
    }, ("group_by",)),
    "list_watchlists": _schema("list_watchlists", {
        "limit": {"type": "integer", "minimum": 1, "maximum": LIST_HARD_MAX},
        "cursor": {"type": ["string", "null"]},
    }),
    "get_watchlist_results": _schema("get_watchlist_results", {
        "watchlist_id": {"type": "integer", "minimum": 1},
        "expected_definition_revision": {"type": ["integer", "null"], "minimum": 1},
        "limit": {"type": "integer", "minimum": 1, "maximum": LIST_HARD_MAX},
        "cursor": {"type": ["string", "null"]},
    }, ("watchlist_id",)),
    "list_campaigns": _schema("list_campaigns", {
        "status": {"type": "string", "enum": list(CAMPAIGN_STATUS_VALUES)},
        "limit": {"type": "integer", "minimum": 1, "maximum": LIST_HARD_MAX},
        "cursor": {"type": ["string", "null"]},
    }),
    "get_campaign": _schema("get_campaign", {
        "campaign_id": {"type": "integer", "minimum": 1},
        "revision": {"type": ["integer", "null"], "minimum": 0},
        "attachment_cursor": {"type": ["string", "null"]},
        "check_cursor": {"type": ["string", "null"]},
        "text_cursor": {"type": ["string", "null"]},
    }, ("campaign_id",)),
    "get_source_provenance": _schema("get_source_provenance", {
        "evidence_id": _id_prop("Evidence id; exactly one of the two ids."),
        "source_version_id": {"type": "integer", "minimum": 1},
    }),
    "search": _schema("search", {
        "query": {"type": "string", "minLength": 1, "maxLength": 200},
    }, ("query",)),
    "fetch": _schema("fetch", {
        "id": _id_prop("Compatibility document/chunk id."),
    }, ("id",)),
}


def _allow(tool: str, raw: dict, allowed: frozenset) -> dict:
    obj = _parse_object(raw, tool)
    _check_unknown(obj, tool, allowed)
    return obj


def parse_get_workspace_context(raw) -> dict:
    _allow("get_workspace_context", raw or {}, frozenset())
    return {}


def parse_search_evidence(raw) -> dict:
    obj = _allow("search_evidence", raw, {"filter", "limit", "cursor"})
    _optional_cursor(obj.get("cursor"), "search_evidence",
                     "evidence search returns a single bounded page; narrow the"
                     " filter instead of continuing")
    return {"filter": parse_filter(obj.get("filter"), "search_evidence"),
            "limit": _limit(obj.get("limit"), "search_evidence")}


def parse_get_evidence(raw) -> dict:
    obj = _allow("get_evidence", raw, {"evidence_id", "revision", "text_cursor"})
    _optional_text_cursor(obj.get("text_cursor"), "get_evidence")
    revision = obj.get("revision")
    if revision is not None and (not isinstance(revision, str) or not revision):
        raise AgentError(INVALID_ARGUMENT, "revision must be an opaque reference")
    return {"evidence_id": _opaque_id(obj.get("evidence_id"), "evidence_id",
                                      "get_evidence"),
            "revision": revision}


def parse_search_transcripts(raw) -> dict:
    obj = _allow("search_transcripts", raw,
                 {"query", "episode_id", "show_id", "show_query", "published_from",
                  "published_to", "include_unknown_dates", "source_version_id",
                  "limit", "cursor"})
    cursor = _parse_opaque_cursor(obj.get("cursor"), "search_transcripts")
    query = _text(obj.get("query"), "query", "search_transcripts", 200, True)
    filt = {"query": query, "history": "active"}
    for key in ("episode_id", "show_id", "show_query", "published_from",
                "published_to", "source_version_id"):
        if obj.get(key) not in (None, ""):
            filt[key] = obj[key]
    if "include_unknown_dates" in obj:
        filt["include_unknown_dates"] = obj["include_unknown_dates"]
    try:
        validated = _search.validate_filter(filt)
    except _search.FilterRejected as exc:
        raise AgentError(INVALID_ARGUMENT, str(exc))
    return {"filter": validated, "limit": _limit(obj.get("limit"),
                                                 "search_transcripts"),
            "cursor": cursor}


def parse_get_transcript_window(raw) -> dict:
    obj = _allow("get_transcript_window", raw,
                 {"episode_id", "transcript_version_id", "start_s", "end_s",
                  "cursor"})
    _optional_cursor(obj.get("cursor"), "get_transcript_window")
    version = obj.get("transcript_version_id")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise AgentError(INVALID_ARGUMENT,
                         "transcript_version_id must be an exact pinned integer")
    start = _finite_number(obj.get("start_s"), "start_s", "get_transcript_window")
    end = _finite_number(obj.get("end_s"), "end_s", "get_transcript_window")
    if start < 0 or end <= start:
        raise AgentError(INVALID_ARGUMENT, "require 0 <= start_s < end_s")
    if end - start > TRANSCRIPT_WINDOW_MAX_S:
        raise AgentError(INVALID_ARGUMENT,
                         f"window must span at most {TRANSCRIPT_WINDOW_MAX_S}s")
    return {"episode_id": _opaque_id(obj.get("episode_id"), "episode_id",
                                     "get_transcript_window"),
            "transcript_version_id": version, "start_s": start, "end_s": end}


def parse_list_brands(raw) -> dict:
    obj = _allow("list_brands", raw, {"query", "filter", "limit", "cursor"})
    _optional_cursor(obj.get("cursor"), "list_brands")
    return {"query": _text(obj.get("query"), "query", "list_brands", 200),
            "filter": parse_filter(obj.get("filter"), "list_brands"),
            "limit": _limit(obj.get("limit"), "list_brands")}


def parse_list_shows(raw) -> dict:
    obj = _allow("list_shows", raw, {"query", "limit", "cursor"})
    _optional_cursor(obj.get("cursor"), "list_shows")
    return {"query": _text(obj.get("query"), "query", "list_shows", 200),
            "limit": _limit(obj.get("limit"), "list_shows")}


def parse_list_episodes(raw) -> dict:
    obj = _allow("list_episodes", raw,
                 {"show_id", "query", "published_from", "published_to",
                  "include_unknown_dates", "limit", "cursor"})
    _optional_cursor(obj.get("cursor"), "list_episodes")
    filt = {"query": "", "history": "active"}
    if obj.get("show_id") not in (None, ""):
        filt["show_id"] = _opaque_id(obj.get("show_id"), "show_id", "list_episodes")
    if obj.get("query") not in (None, ""):
        filt["show_query"] = _text(obj.get("query"), "query", "list_episodes", 200)
    for key in ("published_from", "published_to", "include_unknown_dates"):
        if key in obj:
            filt[key] = obj[key]
    try:
        validated = _search.validate_filter(filt)
    except _search.FilterRejected as exc:
        raise AgentError(INVALID_ARGUMENT, str(exc))
    return {"filter": validated, "limit": _limit(obj.get("limit"), "list_episodes")}


def parse_summarize_evidence(raw) -> dict:
    obj = _allow("summarize_evidence", raw, {"filter", "limit", "cursor",
                                             "group_by"})
    cursor = _parse_opaque_cursor(obj.get("cursor"), "summarize_evidence")
    group_by = obj.get("group_by")
    if group_by not in GROUP_BY_VALUES:
        raise AgentError(INVALID_ARGUMENT,
                         f"group_by must be one of {list(GROUP_BY_VALUES)}")
    return {"filter": parse_filter(obj.get("filter"), "summarize_evidence"),
            "group_by": group_by,
            "limit": _limit(obj.get("limit"), "summarize_evidence"),
            "cursor": cursor}


def parse_list_watchlists(raw) -> dict:
    obj = _allow("list_watchlists", raw, {"limit", "cursor"})
    _optional_cursor(obj.get("cursor"), "list_watchlists")
    return {"limit": _limit(obj.get("limit"), "list_watchlists")}


def parse_get_watchlist_results(raw) -> dict:
    obj = _allow("get_watchlist_results", raw,
                 {"watchlist_id", "expected_definition_revision", "limit",
                  "cursor"})
    _optional_cursor(obj.get("cursor"), "get_watchlist_results")
    expected = obj.get("expected_definition_revision")
    if expected is not None and (isinstance(expected, bool)
                                 or not isinstance(expected, int) or expected < 1):
        raise AgentError(INVALID_ARGUMENT,
                         "expected_definition_revision must be a positive integer")
    return {"watchlist_id": _int_id(obj.get("watchlist_id"), "watchlist_id",
                                    "get_watchlist_results"),
            "expected_definition_revision": expected,
            "limit": _limit(obj.get("limit"), "get_watchlist_results")}


def parse_list_campaigns(raw) -> dict:
    obj = _allow("list_campaigns", raw, {"status", "limit", "cursor"})
    _optional_cursor(obj.get("cursor"), "list_campaigns")
    status = obj.get("status", "all")
    if status not in CAMPAIGN_STATUS_VALUES:
        raise AgentError(INVALID_ARGUMENT,
                         f"status must be one of {list(CAMPAIGN_STATUS_VALUES)}")
    return {"status": status, "limit": _limit(obj.get("limit"), "list_campaigns")}


def parse_get_campaign(raw) -> dict:
    obj = _allow("get_campaign", raw,
                 {"campaign_id", "revision", "attachment_cursor", "check_cursor",
                  "text_cursor"})
    for name in ("attachment_cursor", "check_cursor", "text_cursor"):
        _optional_text_cursor(obj.get(name), "get_campaign")
    revision = obj.get("revision")
    if revision is not None and (isinstance(revision, bool)
                                 or not isinstance(revision, int) or revision < 0):
        raise AgentError(INVALID_ARGUMENT,
                         "revision must be a non-negative integer")
    return {"campaign_id": _int_id(obj.get("campaign_id"), "campaign_id",
                                   "get_campaign"),
            "revision": revision}


def parse_get_source_provenance(raw) -> dict:
    obj = _allow("get_source_provenance", raw, {"evidence_id",
                                                "source_version_id"})
    has_evidence = obj.get("evidence_id") not in (None, "")
    has_version = obj.get("source_version_id") is not None
    if has_evidence == has_version:
        raise AgentError(INVALID_ARGUMENT, "provide exactly one of evidence_id"
                                           " or source_version_id")
    if has_evidence:
        return {"evidence_id": _opaque_id(obj.get("evidence_id"), "evidence_id",
                                          "get_source_provenance")}
    return {"source_version_id": _int_id(obj.get("source_version_id"),
                                         "source_version_id",
                                         "get_source_provenance")}


def parse_search(raw) -> dict:
    obj = _allow("search", raw, {"query"})
    return {"query": _text(obj.get("query"), "query", "search", 200, True)}


def parse_fetch(raw) -> dict:
    obj = _allow("fetch", raw, {"id"})
    return {"id": _opaque_id(obj.get("id"), "id", "fetch")}


REQUEST_PARSERS = {
    "get_workspace_context": parse_get_workspace_context,
    "search_evidence": parse_search_evidence,
    "get_evidence": parse_get_evidence,
    "search_transcripts": parse_search_transcripts,
    "get_transcript_window": parse_get_transcript_window,
    "list_brands": parse_list_brands,
    "list_shows": parse_list_shows,
    "list_episodes": parse_list_episodes,
    "summarize_evidence": parse_summarize_evidence,
    "list_watchlists": parse_list_watchlists,
    "get_watchlist_results": parse_get_watchlist_results,
    "list_campaigns": parse_list_campaigns,
    "get_campaign": parse_get_campaign,
    "get_source_provenance": parse_get_source_provenance,
    "search": parse_search,
    "fetch": parse_fetch,
}


def require_saved_meaning(legacy, owner: str = "saved filter") -> dict:
    """Strict gate for legacy-shaped filters (exports, stored definitions).

    Returns the validated ResearchFilterV1 with identical meaning; raises
    AgentError (HTTP 400) carrying needs_correction reasons instead of
    evaluating broadly."""
    validated, problems = _search.decode_legacy_filter(legacy)
    if problems:
        raise AgentError(INVALID_ARGUMENT,
                         f"{owner} needs_correction: " + "; ".join(problems),
                         details={"needs_correction": problems})
    return validated


# -- Opaque signed cursors (§6; issued and advanced in package B) -------------

def encode_cursor(secret: bytes, payload: dict) -> str:
    """Seal a cursor payload: workspace/grant-policy identity, normalized
    filter digest, authorized-view revision, stable sort key, expiry, page
    size. Package B issues these; the codec lives here so both adapters
    share one validation rule."""
    if not isinstance(secret, bytes) or not secret:
        raise ValueError("cursor secret must be non-empty bytes")
    body = {"v": 1, "exp": int(time.time()) + CURSOR_TTL_S}
    body.update(payload)
    raw = base64.urlsafe_b64encode(json.dumps(body, sort_keys=True).encode())
    sig = hmac.new(secret, raw, hashlib.sha256).hexdigest()
    return (raw.decode() + "." + sig)


def decode_cursor(secret: bytes, token: str, *, now: float | None = None) -> dict:
    """Validate a cursor or raise CURSOR_EXPIRED / INVALID_ARGUMENT.

    Expired-but-well-formed cursors report CURSOR_EXPIRED so callers
    restart the original query; malformed or forged cursors report
    INVALID_ARGUMENT. A change to the authorized projection between pages
    reports RESULT_SET_CHANGED at the service layer, never silent
    skipping or repetition."""
    if not isinstance(secret, bytes) or not secret:
        raise ValueError("cursor secret must be non-empty bytes")
    if not isinstance(token, str) or "." not in token:
        raise AgentError(INVALID_ARGUMENT, "cursor is malformed")
    raw, _, sig = token.rpartition(".")
    expected = hmac.new(secret, raw.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise AgentError(INVALID_ARGUMENT, "cursor is malformed or forged")
    try:
        body = json.loads(base64.urlsafe_b64decode(raw.encode()).decode())
    except (ValueError, UnicodeDecodeError):
        raise AgentError(INVALID_ARGUMENT, "cursor is malformed")
    if not isinstance(body, dict) or body.get("v") != 1:
        raise AgentError(INVALID_ARGUMENT, "cursor is malformed")
    if not isinstance(body.get("exp"), int) \
            or body["exp"] <= (now if now is not None else time.time()):
        raise AgentError(CURSOR_EXPIRED, "cursor expired; restart the original query")
    return body


# -- Byte budget (§6: 16 KiB aim incl. dual MCP representations, 64 KiB hard)

def mcp_wire_bytes(payload: dict) -> int:
    """Actual UTF-8 serialization size including both MCP representations."""
    structured = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return len(structured) * 2


def check_result_fit(payload: dict) -> tuple[bool, int]:
    """Return (fits_hard_cap, wire_bytes) for a candidate result."""
    size = mcp_wire_bytes(payload)
    return (size <= RESULT_HARD_BYTES, size)


# -- Trusted adapter plumbing -------------------------------------------------

_WORKSPACES: dict[str, str] = {}
_CURSOR_SECRETS: dict[str, bytes] = {}


def bind_workspace(workspace_id: str, path: str | Path) -> None:
    """Server-controlled workspace registry: adapter binds at startup."""
    if not isinstance(workspace_id, str) or not workspace_id:
        raise ValueError("workspace_id must be a non-empty string")
    _WORKSPACES[workspace_id] = str(path)
    # Per-workspace cursor secret, generated server-side at bind and never
    # exposed: process restarts invalidate outstanding cursors (clients
    # restart the original query), which the 15-minute TTL makes routine.
    _CURSOR_SECRETS.setdefault(workspace_id, secrets.token_bytes(32))


def _cursor_secret(workspace_id: str) -> bytes:
    try:
        return _CURSOR_SECRETS[workspace_id]
    except KeyError:
        raise AgentError(NOT_FOUND, "unknown workspace")


def _parse_opaque_cursor(value, tool: str) -> str | None:
    """Accept an opaque continuation cursor for package-B paged tools."""
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 2000:
        raise AgentError(INVALID_ARGUMENT, f"{tool} cursor is malformed")
    return value


def _project_revision(con, scope, notes_allowed, history, ctx=None, items=None):
    """The view revision of a scope for ctx. items (research only): the
    scope's records when the caller already collected them
    (_collect_scope_items of validate_filter({"history": history}))."""
    if ctx is None:
        return _search.projection_revision(con, {"scope": scope, "notes": notes_allowed, "history": history})
    if scope == "transcripts":
        content = []
        fingerprints = _transcript_fingerprints(con)
        for ep in con.execute("SELECT id, show_title, ep_title, pub_date, transcript_version_id"
                              " FROM episodes ORDER BY id"):
            source_key = _citations._version_source_key(con, ep["transcript_version_id"])
            if source_key is None:
                continue
            try:
                ctx.authorize_disclosure(con, source_key, ["metadata", "transcript"])
            except _access.AuthorizationError:
                continue
            version_id = ep["transcript_version_id"]
            content.append([ep["id"], ep["show_title"], ep["ep_title"], ep["pub_date"],
                            version_id, fingerprints.get(version_id)
                            or _payload_fingerprint(con, version_id)])
    elif RESEARCH_READ in ctx.scopes:
        if items is None:
            filt = _search.validate_filter({"history": history})
            items = _collect_scope_items(con, filt, _budget(None), ctx.permits("transcript"),
                                         notes_allowed, ctx=ctx)
        content = [_project_evidence_item(con, item, "", ctx=ctx) for item in items]
    else:
        content = []
    return _citations._state_digest("native", ctx.policy_identity, content)


def _transcript_fingerprints(con) -> dict:
    """{version id: content fingerprint} of the indexed transcript
    versions, read from the index alone (AGENT-A-SCOPE): the stored hash,
    the indexed segment count and the row id of the first indexed
    segment. A payload rewrite drops the version's index entry (the
    transcript_index triggers), so such a version is fingerprinted by
    its payload (_payload_fingerprint) until it is indexed again, with
    new segment rows."""
    from . import transcript_index as _tindex
    if not _tindex.available(con):
        return {}
    return {row[0]: ["indexed", row[1], row[2], row[3]] for row in con.execute(
        f"SELECT v.version_id, sv.sha256, v.segments, (SELECT s.id FROM {_tindex.SEGMENTS} s"
        " WHERE s.version_id=v.version_id ORDER BY s.seg_index LIMIT 1)"
        f" FROM {_tindex.VERSIONS} v JOIN source_versions sv ON sv.id=v.version_id")}


def _payload_fingerprint(con, version_id) -> list:
    row = con.execute("SELECT payload FROM source_versions WHERE id=?", (version_id,)).fetchone()
    payload = row[0] if row else None
    return ["payload", hashlib.sha256(str(payload).encode("utf-8", "surrogatepass")).hexdigest()]


def owner_context(workspace_id: str = "local") -> _access.AccessContext:
    """Server-constructed owner context for the trusted local adapter."""
    return _access.AccessContext.create(
        principal_id="owner-1", principal_kind="owner",
        workspace_id=workspace_id, credential_kind="local_operator",
        scopes={RESEARCH_READ, TRANSCRIPTS_READ, WATCHLISTS_READ,
                CAMPAIGNS_READ, NOTES_READ, "exports:create",
                "watchlists:write", "campaigns:write", "proposals:write"})


def _budget(budget: _access.ExecutionBudget | None) -> _access.ExecutionBudget:
    if budget is None:
        budget = _access.ExecutionBudget(deadline_s=QUERY_TIMEOUT_S)
    budget.check("agent request")
    return budget


def _connect(ctx: _access.AccessContext):
    if ctx.db_path:
        path = Path(ctx.db_path)
    else:
        try:
            path = Path(_WORKSPACES[ctx.workspace_id])
        except KeyError:
            raise AgentError(NOT_FOUND, "unknown workspace")
    con = _db.open_existing(path / "workspace.sqlite3" if path.is_dir() else path, readonly=True)
    con.execute("BEGIN")
    return con


def _authorize(ctx: _access.AccessContext, tool: str) -> None:
    # Agent tools require a workspace connection grant (membership
    # authority). Share guests use browser citation routes; guest grants
    # can never create agent access (§9 guest matrix).
    ctx.require_member(tool)
    ctx.require_operation(tool)
    for scope in TOOL_SCOPES[tool]:
        ctx.require_scope(scope)


def _base_envelope(ctx, con, scope: str, applied: dict, items: list,
                   total: int, coverage: dict, limitations: list,
                   citations: list | None = None) -> dict:
    return _services.envelope(ctx, con, scope, applied, items, total,
                              coverage=coverage, limitations=tuple(limitations),
                              citations=citations)


def _bounded_text(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return (text, True)
    return (text[:limit], False)


def _revision_ref(evidence_rev, assoc_rev, review_rev) -> str:
    return f"v1.{evidence_rev}.{assoc_rev}.{review_rev}"


def _parse_revision_ref(reference: str | None) -> tuple | None:
    if reference is None:
        return None
    parts = reference.split(".")
    if len(parts) != 4 or parts[0] != "v1":
        raise AgentError(INVALID_ARGUMENT, "revision is not a known reference")
    try:
        return tuple(int(p) for p in parts[1:])
    except ValueError:
        raise AgentError(INVALID_ARGUMENT, "revision is not a known reference")


def _permitted_basis(item: dict, query: str, transcripts_allowed: bool,
                     notes_allowed: bool, interval: str = "",
                     note: str | None = None) -> str | None:
    """match_basis over permitted fields only (restricted callers).

    Mirrors the shared basis priority (excerpt, interval, association,
    title); a notes-only match reports cross_field, exactly as the shared
    path does for junction matches.
    """
    if not query:
        return None
    needle = query.casefold()
    if needle in (item.get("excerpt") or "").casefold():
        return "excerpt"
    if transcripts_allowed and needle in (interval or "").casefold():
        return "interval_text"
    # PODS: model rows are display-only
    assoc_text = " ".join(a.get("brand", "") for a in item.get("assocs", [])
                          if a.get("basis") != "model")
    if needle in assoc_text.casefold():
        return "association"
    title = " ".join((item.get("show_title") or "", item.get("ep_title") or ""))
    if needle in title.casefold():
        return "title"
    return "cross_field"


def _project_evidence_item(con, item: dict, query: str, basis=None,
                           citation=None, ctx=None) -> dict:
    preview, _ = _bounded_text(item.get("excerpt") or "", EXCERPT_LIST_MAX)
    review_rev = item.get("review_rev")
    if "assoc_revision" in item:  # scope items carry their evidence row
        assoc_rev = item["assoc_revision"]
    else:
        rows = con.execute("SELECT assoc_revision FROM evidence WHERE id=?",
                           (item["id"],)).fetchone()
        assoc_rev = rows["assoc_revision"] if rows is not None else None
    out = {
        "id": item["id"],
        "source": item.get("source"),
        "episode_id": item.get("episode_id"),
        "original_bounds_s": [item.get("orig_start_s"),
                              item.get("orig_end_s")],
        "excerpt_preview": preview,
        "excerpt_complete": len(item.get("excerpt") or "") <= EXCERPT_LIST_MAX,
        "match_basis": basis if basis is not None else _match_basis(con, item, query),
        "review_judgment": item.get("judgment"),
        "attribution": [{"brand": a.get("brand"), "basis": a.get("basis"),
                         "channel": a.get("channel")}
                        # PODS: model rows are display-only
                        for a in item.get("assocs", []) if a.get("basis") != "model"],
        "revision_ref": _revision_ref(item.get("revision"), assoc_rev, review_rev),
        "citation": citation if citation is not None else item.get("citation"),
    }
    if ctx is not None and not (ctx.permits("transcript") and ctx.permits("notes")
            and item.get("_transcript_allowed", True) and item.get("_notes_allowed", True)
            and item.get("_excerpt_allowed", True)):
        out.pop("revision_ref", None)
        out.pop("citation", None)
    if not item.get("_excerpt_allowed", True):
        out.pop("excerpt_preview", None)
        out.pop("excerpt_complete", None)
    return out


def _match_basis(con, item: dict, query: str) -> str | None:
    if not query:
        return None
    needle = query.casefold()
    # PODS: model rows are display-only
    assoc_text = " ".join(a.get("brand", "") for a in item.get("assocs", [])
                          if a.get("basis") != "model")
    if needle in (item.get("excerpt") or "").casefold():
        return "excerpt"
    interval = _ops.evidence_text(con, {"id": item["id"],
                                        "episode_id": item["episode_id"],
                                        "transcript_version_id": item.get(
                                            "transcript_version_id"),
                                        "orig_start_s": item.get("orig_start_s"),
                                        "orig_end_s": item.get("orig_end_s")})
    if needle in interval.casefold():
        return "interval_text"
    if needle in assoc_text.casefold():
        return "association"
    title = " ".join((item.get("show_title") or "", item.get("ep_title") or ""))
    if needle in title.casefold():
        return "title"
    # Junction match across concatenated fields; not a claim about one field.
    return "cross_field"


# -- Named service entrypoints (§14: fn(context, request, *, budget)) ---------

def get_workspace_context(ctx: _access.AccessContext, request: dict, *,
                          budget: _access.ExecutionBudget | None = None) -> dict:
    parse_get_workspace_context(request)
    _authorize(ctx, "get_workspace_context")
    _budget(budget)
    con = _connect(ctx)
    try:
        tools = sorted(tool for tool, scopes in TOOL_SCOPES.items()
                       if all(s in ctx.scopes for s in scopes))
        data = {
            "workspace_id": ctx.workspace_id,
            "available_tools": tools,
            "granted_scopes": sorted(ctx.scopes),
            "units": {"counts": "evidence records, distinct canonical episodes,"
                                " association records, or assisted reviewed"
                                " records; never unique ads, spend, reach,"
                                " or impressions"},
            "limits": {"list_default": LIST_DEFAULT, "list_max": LIST_HARD_MAX,
                       "excerpt_list_max": EXCERPT_LIST_MAX,
                       "excerpt_detail_max": EXCERPT_DETAIL_MAX,
                       "transcript_window_default_s": TRANSCRIPT_WINDOW_DEFAULT_S,
                       "transcript_window_max_s": TRANSCRIPT_WINDOW_MAX_S,
                       "transcript_window_max_segments":
                           TRANSCRIPT_WINDOW_MAX_SEGMENTS,
                       "result_aim_bytes": RESULT_AIM_BYTES,
                       "result_hard_bytes": RESULT_HARD_BYTES,
                       "query_target_s": QUERY_TARGET_S,
                       "query_timeout_s": QUERY_TIMEOUT_S,
                       "cursor_ttl_s": CURSOR_TTL_S},
            "connection": "agent connection; read scopes disclose research to"
                          " the selected agent provider",
        }
        return _base_envelope(ctx, con, "research", {}, [data], 1,
                              coverage={"scope": "workspace context"},
                              limitations=list(LIMITATIONS))
    finally:
        con.close()


def search_evidence(ctx: _access.AccessContext, request: dict, *,
                    budget: _access.ExecutionBudget | None = None) -> dict:
    args = parse_search_evidence(request)
    _authorize(ctx, "search_evidence")
    budget = _budget(budget)
    con = _connect(ctx)
    try:
        filt = args["filter"]
        transcripts_allowed = ctx.permits("transcript")
        notes_allowed = ctx.permits("notes")
        query = filt.get("query", "")
        # Restricted scope or show_id: exact permitted-fields rescan with an
        # exact total, projected onto the same single bounded page. A filter
        # that is the revision's scope plus a query matches that one scope
        # pass instead of collecting it twice.
        scope_filter = _search.validate_filter({"history": filt["history"]})
        scope = None
        if dict(filt, query="") == scope_filter:
            scope = _collect_scope_items(con, scope_filter, budget, transcripts_allowed,
                                         notes_allowed, ctx=ctx)
            items = _match_scope_items(con, scope, query, budget) if query else scope
        else:
            items = _collect_scope_items(con, filt, budget, transcripts_allowed, notes_allowed,
                                         ctx=ctx)
        total = len(items)
        projected = []
        for item in items[:args["limit"]]:
            interval = _ops.evidence_text(con, item) if item.get("_transcript_allowed", transcripts_allowed) else ""
            note = None
            if item.get("_notes_allowed", notes_allowed) and query:
                row = con.execute("SELECT notes FROM reviews WHERE evidence_id=?",
                                  (item["id"],)).fetchone()
                note = row["notes"] if row else ""
            try:
                citation = _citations.evidence_citation(con, item["id"])
            except _citations.CitationError:
                citation = None
            projected.append(_project_evidence_item(con, item, query,
                _permitted_basis(item, query, item.get("_transcript_allowed", transcripts_allowed),
                                 item.get("_notes_allowed", notes_allowed), interval, note),
                citation, ctx=ctx))
        result = _services.envelope(ctx, con, "research", filt, projected, total,
                                    coverage={"scope": "authorized evidence scope"},
                                    limitations=tuple(["CANDIDATES_NOT_UNIQUE_ADS",
                                                       "FIELD_RESTRICTED_RESCAN"]),
                                    citations=[i["citation"] for i in projected
                                               if i.get("citation")])
        result["view_revision"] = _project_revision(con, "research", notes_allowed, filt["history"],
                                                    ctx=ctx, items=scope)
        result["truncated"] = total > len(projected)
        result["truncation_reason"] = "PAGE_LIMIT" if total > len(projected) else None
        return result
    finally:
        con.close()


def get_evidence(ctx: _access.AccessContext, request: dict, *,
                 budget: _access.ExecutionBudget | None = None) -> dict:
    args = parse_get_evidence(request)
    _authorize(ctx, "get_evidence")
    _budget(budget)
    con = _connect(ctx)
    try:
        try:
            detail = _services.evidence_detail(ctx, con, args["evidence_id"])
        except KeyError:
            raise AgentError(NOT_FOUND, "unknown evidence")
        review = detail.get("review") or {}
        current_ref = _revision_ref(detail.get("revision"),
                                    detail.get("assoc_revision"),
                                    review.get("revision"))
        expected = _parse_revision_ref(args["revision"])
        if expected is not None and not ({TRANSCRIPTS_READ, NOTES_READ} <= ctx.scopes):
            raise AgentError(FORBIDDEN, "component revision action requires its complete field scope")
        if expected is not None and expected != (
                detail.get("revision"), detail.get("assoc_revision"),
                review.get("revision")):
            raise AgentError(REVISION_CONFLICT, "stale revision; reread current",
                             details={"current_revision": current_ref})
        excerpt, excerpt_complete = _bounded_text(detail.get("excerpt") or "",
                                                  EXCERPT_DETAIL_MAX)
        # Interval text is transcript content: without transcripts:read the
        # detail path omits it entirely (same rule as the compatibility
        # fetch, which gates the interval quote explicitly). An empty
        # preview reports complete so the omission never raises a
        # truncation flag and never influences matches/counts.
        interval = (_ops.evidence_text(con, detail)
                    if detail.get("_transcript_allowed") else "")
        interval_preview, interval_complete = _bounded_text(interval,
                                                            EXCERPT_DETAIL_MAX)
        notes_allowed = detail.get("_notes_allowed", False)
        review_out = {"judgment": review.get("judgment"),
                      "bounds_verified": bool(review.get("bounds_verified", 0)),
                      "effective_bounds_s": [review.get("eff_start_s"),
                                             review.get("eff_end_s")]}
        if notes_allowed:
            review_out["revision"] = review.get("revision")
            note, note_complete = _bounded_text(str(review.get("notes") or ""),
                                                TEXT_CHUNK_MAX)
            review_out["notes"] = note
            review_out["notes_complete"] = note_complete
        limitations = ["CANDIDATES_NOT_UNIQUE_ADS", "AUDIO_ALIGNMENT_UNKNOWN"]
        if not excerpt_complete or not interval_complete:
            limitations.append("TEXT_TRUNCATED_USE_WINDOW")
        data = {
            "id": detail["id"],
            "source": detail.get("source"),
            "episode_id": detail.get("episode_id"),
            "transcript_version_id": detail.get("transcript_version_id"),
            "source_version_id": detail.get("source_version_id"),
            "revision_ref": current_ref,
            "original_bounds_s": [detail.get("orig_start_s"),
                                  detail.get("orig_end_s")],
            "excerpt_preview": excerpt,
            "excerpt_complete": excerpt_complete,
            "interval_text_preview": interval_preview,
            "interval_text_complete": interval_complete,
            "attribution": [{"brand": a.get("brand"), "basis": a.get("basis"),
                             "channel": a.get("channel"),
                             "origin": a.get("origin")}
                            # PODS: model rows are display-only
                            for a in detail.get("assocs", []) if a.get("basis") != "model"],
            "review": review_out,
            "overlaps": [{"id": o.get("id"), "source": o.get("source"),
                          "bounds_s": [o.get("orig_start_s"),
                                       o.get("orig_end_s")]}
                         for o in detail.get("overlaps", [])],
            "audio": {"available": bool(detail.get("audio_url")
                                        or detail.get("media_id")),
                      "alignment": "unknown",
                      "agent_audio_access": False},
            "fetch_date": (detail.get("citation") or {}).get("fetch_date"),
            "fetch_date_source": (detail.get("citation") or {}).get("fetch_date_source",
                                                                    "episode"),
        }
        if not (detail.get("_transcript_allowed") and notes_allowed and detail.get("_excerpt_allowed")):
            data.pop("revision_ref", None)
        if not detail.get("_transcript_allowed"):
            data.pop("transcript_version_id", None)
            data.pop("source_version_id", None)
            data.pop("interval_text_preview", None)
            data.pop("interval_text_complete", None)
        if not detail.get("_excerpt_allowed"):
            data.pop("excerpt_preview", None)
            data.pop("excerpt_complete", None)
        citation = detail.get("citation")
        return _base_envelope(ctx, con, "research", {}, [data], 1,
                              coverage={"scope": "single evidence record"},
                              limitations=limitations,
                              citations=[citation] if citation else [])
    finally:
        con.close()


def search_transcripts(ctx: _access.AccessContext, request: dict, *,
                       budget: _access.ExecutionBudget | None = None) -> dict:
    args = parse_search_transcripts(request)
    _authorize(ctx, "search_transcripts")
    budget = _budget(budget)
    con = _connect(ctx)
    try:
        filt = args["filter"]
        secret = _cursor_secret(ctx.workspace_id)
        revision = _project_revision(con, "transcripts", False, filt["history"], ctx=ctx)
        digest = _search.filter_digest_for(filt, filt["query"], ctx.policy_identity)
        position = None
        if args["cursor"] is not None:
            body = decode_cursor(secret, args["cursor"])
            if body.get("workspace_id") != ctx.workspace_id:
                raise AgentError(INVALID_ARGUMENT, "cursor belongs to another workspace")
            if body.get("filter_digest") != digest:
                raise AgentError(INVALID_ARGUMENT,
                                 "cursor belongs to a different query; restart it")
            if body.get("view_revision") != revision:
                raise AgentError(RESULT_SET_CHANGED,
                                 "collection changed during traversal; restart the query")
            position = body.get("position")
            if not isinstance(position, dict) or not isinstance(position.get("hit"), int) \
                    or not isinstance(position.get("char_offset"), int) or position["hit"] < 0:
                raise AgentError(INVALID_ARGUMENT, "cursor is malformed")
        try:
            page = _transcript_page(con, ctx, filt, args["limit"], position, budget)
        except _search.FilterRejected as exc:
            raise AgentError(INVALID_ARGUMENT, str(exc))
        next_cursor = None
        if page["next_position"] is not None:
            next_cursor = encode_cursor(secret, {
                "workspace_id": ctx.workspace_id,
                "filter_digest": digest,
                "view_revision": revision,
                "sort_key": "transcript",
                "page_size": args["limit"],
                "position": page["next_position"]})
        limitations = ["AUDIO_ALIGNMENT_UNKNOWN"]
        if page["total_relation"] == "lower_bound":
            limitations.append("SCAN_CAPPED_COUNT_IS_LOWER_BOUND")
        items = page["hits"]
        result = _services.envelope(ctx, con, "transcripts", filt, items,
                                    page["total_matches"],
                                    relation=page["total_relation"],
                                    cursor=next_cursor,
                                    coverage=page["coverage"],
                                    limitations=tuple(limitations),
                                    citations=[i["citation"] for i in items
                                               if i.get("citation")])
        result["view_revision"] = revision
        if page["truncation_reason"]:
            result["truncation_reason"] = page["truncation_reason"]
        return result
    finally:
        con.close()


#: How search_transcripts matches and orders (also in the coverage block).
TRANSCRIPT_MATCHING = ("whole words, accent- and case-insensitive; every query word (and"
                       " every \"quoted phrase\") in one segment: product Search's rule")
TRANSCRIPT_ORDERING = ("newest publication first, undated last (product Search's default"
                       " sort); an episode's segments in transcript order")


def _transcript_page(con, ctx, filt: dict, limit: int, position: dict | None,
                     budget) -> dict:
    """One page of transcript search (AGENT-A-SCOPE): the product's
    index-backed whole-word passage search (customer._passage_groups over
    transcript_index) across every authorized transcript in scope, with
    an exact count of matching segments. position {"hit", "char_offset"}
    resumes after the last window emitted: the hit-th matching segment in
    TRANSCRIPT_ORDERING, from char_offset + 1."""
    from . import customer, customer_contracts
    query = filt.get("query", "")
    terms, phrases = customer.tokenize_query(query)
    if not terms and not phrases:
        raise _search.FilterRejected("transcript query must hold a word")
    episodes, pin = _search.transcript_episodes(con, filt)
    entries, in_scope = [], 0
    for ep in episodes:
        budget.check("transcript search")
        versions = _search.transcript_versions(con, ep["id"], filt["history"], pin)
        if not (versions and ctx.disclosure_allowed(con, f"transcript:{ep['id']}", "metadata")
                and ctx.disclosure_allowed(con, f"transcript:{ep['id']}", "transcript")):
            continue
        in_scope += 1
        for rank, version_id in enumerate(versions):
            source_key = _citations._version_source_key(con, version_id)
            if source_key is None:
                continue
            try:
                ctx.authorize_disclosure(con, source_key, ["metadata", "transcript"])
            except _access.AuthorizationError:
                continue
            entries.append({"id": (ep["id"], version_id), "episode_id": ep["id"],
                            "transcript_version_id": version_id, "rank": rank,
                            "pub_date": ep.get("pub_date") or ""})
    budget.check("transcript search")
    # Authorized above: _passage_groups runs without a context (no title
    # matches are used; only segment text counts). The budget bounds it
    # (AGA-R1-01): SQLite reads are interrupted when it runs out, and the
    # match loop checks it, as the old scan did every 200 segments.
    con.set_progress_handler(
        lambda: budget.cancelled or budget.remaining_s() <= 0, 10000)
    try:
        groups, partial, _reason, _titles, counts = customer._passage_groups(
            con, entries, terms, phrases, check=lambda: budget.check("transcript search"))
    except sqlite3.OperationalError as exc:
        if "interrupted" not in str(exc):
            raise
        budget.check("transcript search")
        raise
    finally:
        con.set_progress_handler(None, 0)
    budget.check("transcript search")
    matched = [e for e in entries if (groups.get(e["id"]) or {}).get("count")]
    dated = [e for e in matched if customer_contracts.publication_day(e["pub_date"])]
    undated = [e for e in matched if not customer_contracts.publication_day(e["pub_date"])]
    dated.sort(key=lambda e: e["rank"])
    dated.sort(key=lambda e: (e["pub_date"], e["episode_id"]), reverse=True)
    undated.sort(key=lambda e: (e["episode_id"], e["rank"]))
    flat = [(e, index) for e in dated + undated for index in sorted(groups[e["id"]]["segments"])]
    total = len(flat)
    indexed = _tindex_indexed(con, [e["transcript_version_id"] for e in entries])
    coverage = {"scope": "authorized transcript collection",
                "episodes_in_scope": in_scope,
                "transcripts_searched": len(groups) if partial else len(entries),
                "transcripts_indexed": sum(1 for e in entries
                                           if e["transcript_version_id"] in indexed),
                "episodes_matched": len({e["episode_id"] for e in matched}),
                "matching": TRANSCRIPT_MATCHING,
                "ordering": TRANSCRIPT_ORDERING}
    folded = [customer.normalize_token(t) for t in terms + phrases]
    hits: list[dict] = []
    memo: dict = {}
    wire_bytes = 0
    truncated, reason, last = False, None, None
    start = position["hit"] if position else 0
    for i in range(start, total):
        entry, index = flat[i]
        budget.check("transcript search")
        version_id = entry["transcript_version_id"]
        pinned = _citations.pinned_transcript(con, version_id, memo)
        seg = pinned.segments[index] if pinned is not None and index < len(pinned.segments) else {}
        text = seg.get("text", "") if isinstance(seg, dict) else ""
        text = text if isinstance(text, str) else ""
        resume = position["char_offset"] + 1 if position and i == start else 0
        for chunk, win_start, win_end, spans, continuation in _search._chunk_segment(
                text, _word_spans(text, folded), resume):
            first = spans[0] if spans else (None, None)
            hit = {"episode_id": entry["episode_id"], "transcript_version_id": version_id,
                   "segment_index": index, "segment_id": seg.get("id", index),
                   "start_s": seg.get("start"), "end_s": seg.get("end"), "text": chunk,
                   "window_start": win_start, "window_end": win_end,
                   "match_spans": [[s, e] for s, e in spans],
                   "char_start": first[0], "char_end": first[1],
                   "text_complete": win_start == 0 and win_end >= len(text),
                   "continuation": continuation, "match_basis": "transcript_segment"}
            size = len(json.dumps(hit)) * 2
            if hits and (len(hits) >= limit or wire_bytes + size > RESULT_HARD_BYTES
                         or wire_bytes >= RESULT_AIM_BYTES):
                truncated = True
                reason = "PAGE_LIMIT" if len(hits) >= limit else "BYTE_BUDGET"
                break
            fetch = con.execute("SELECT fetch_date FROM episodes WHERE id=?",
                                (entry["episode_id"],)).fetchone()
            hit["citation"] = {"episode_id": entry["episode_id"],
                               "transcript_version_id": version_id,
                               "source_pin": _citations.source_pin(con, version_id),
                               "fetch_date": fetch["fetch_date"] if fetch else None}
            hits.append(hit)
            wire_bytes += size
            last = {"hit": i, "char_offset": win_end - 1}
        if truncated:
            break
    return {"hits": hits, "total_matches": total,
            "total_relation": "lower_bound" if partial else "exact",
            "next_position": last if truncated else None,
            "truncation_reason": reason if truncated else
            ("SCAN_CAP" if partial else None),
            "coverage": coverage}


def _tindex_indexed(con, version_ids) -> dict:
    from . import transcript_index as _tindex
    return _tindex.indexed(con, version_ids)


def _word_spans(text: str, folded_tokens: list) -> list:
    """Whole-word spans (customer.word_in) of each folded token in text,
    in original character offsets, sorted."""
    from . import customer
    folded, index_map = [], []
    for i, ch in enumerate(text):
        for piece in customer.normalize_token(ch):
            folded.append(piece)
            index_map.append(i)
    hay = "".join(folded)
    spans = set()
    for token in folded_tokens:
        if not token:
            continue
        for found in customer._word_pattern(token).finditer(hay):
            if found.end() > found.start():
                spans.add((index_map[found.start()], index_map[found.end() - 1] + 1))
    return sorted(spans)


def get_transcript_window(ctx: _access.AccessContext, request: dict, *,
                          budget: _access.ExecutionBudget | None = None) -> dict:
    args = parse_get_transcript_window(request)
    _authorize(ctx, "get_transcript_window")
    _budget(budget)
    con = _connect(ctx)
    try:
        if not con.execute("SELECT 1 FROM source_versions WHERE id=?",
                           (args["transcript_version_id"],)).fetchone():
            raise AgentError(NOT_FOUND, "unknown transcript version")
        try:
            window = _services.transcript_window(ctx, con, args["episode_id"],
                args["transcript_version_id"], start_s=args["start_s"], end_s=args["end_s"])
        except _citations.CitationError:
            raise AgentError(NOT_FOUND, "unknown episode")
        segments = [s for s in window["segments"]
                    if s.get("end", 0) > args["start_s"]
                    and s.get("start", 0) < args["end_s"]]
        kept = segments[:TRANSCRIPT_WINDOW_MAX_SEGMENTS]
        truncated = window["truncated"] or len(segments) > len(kept)
        reason = "WINDOW_LIMIT" if truncated else None
        data = {"episode_id": args["episode_id"],
                "transcript_version_id": args["transcript_version_id"],
                "requested_window_s": [args["start_s"], args["end_s"]],
                "segments": kept,
                "returned_segments": len(kept),
                "truncated": truncated,
                "truncation_reason": reason,
                "next_cursor": None}
        return _base_envelope(ctx, con, "transcripts", {}, [data], 1,
                              coverage={"scope": "bounded transcript window"},
                              limitations=["AUDIO_ALIGNMENT_UNKNOWN"],
                              citations=[window["citation"]])
    finally:
        con.close()


def list_brands(ctx: _access.AccessContext, request: dict, *,
                budget: _access.ExecutionBudget | None = None) -> dict:
    args = parse_list_brands(request)
    _authorize(ctx, "list_brands")
    _budget(budget)
    con = _connect(ctx)
    try:
        scoped = _collect_scope_items(con, args["filter"], _budget(budget), ctx.permits("transcript"),
                                      ctx.permits("notes"), ctx=ctx, excerpts=False)
        needle = args["query"].strip().casefold()
        groups: dict = {}
        # The scoped items' associations (none for a brand under review).
        for item in scoped:
            for row in item.get("assocs") or []:
                # PODS: model rows are display-only
                if row.get("basis") == "model":
                    continue
                if needle and needle not in (row["brand"] or "").casefold():
                    continue
                key = (row["brand"], row["channel"], row["basis"])
                groups.setdefault(key, set()).add(item["id"])
        items = [{"brand": brand, "channel": channel, "basis": basis,
                  "evidence_records": len(ids),
                  "count_unit": "evidence records"}
                 for (brand, channel, basis), ids in sorted(groups.items())]
        return _base_envelope(ctx, con, "research", args["filter"],
                              items[:args["limit"]], len(items),
                              coverage={"scope": "authorized evidence scope"},
                              limitations=["CANDIDATES_NOT_UNIQUE_ADS"])
    finally:
        con.close()


def list_shows(ctx: _access.AccessContext, request: dict, *,
               budget: _access.ExecutionBudget | None = None) -> dict:
    args = parse_list_shows(request)
    _authorize(ctx, "list_shows")
    _budget(budget)
    con = _connect(ctx)
    try:
        needle = args["query"].strip().casefold()
        groups: dict = {}
        episode_sets: dict = {}
        from . import customer
        permitted = _collect_scope_items(con, _search.validate_filter({}), _budget(budget),
                                         ctx.permits("transcript"), ctx.permits("notes"),
                                         ctx=ctx, excerpts=False)
        per_episode = Counter(item["episode_id"] for item in permitted)
        for row in customer.permitted_episodes(con, ctx):
            title = row["show_title"] or ""
            if needle and needle not in title.casefold():
                continue
            identity = _search.show_identity(dict(row))
            key = identity["show_id"]
            entry = groups.setdefault(key, {"show_id": key, "title": identity["title"],
                                            "episodes": 0, "transcript_episodes": 0,
                                            "identity_basis": identity["basis"]})
            entry["episodes"] += 1
            if ctx.permits("transcript"):
                source = _citations._version_source_key(con, row["transcript_version_id"])
                entry["transcript_episodes"] += bool(source is not None and ctx.disclosure_allowed(con, source, "transcript"))
            else:
                entry.pop("transcript_episodes", None)
            episode_sets.setdefault(key, set()).add(row["id"])
        items = _search.annotate_show_ambiguity(
            sorted(groups.values(), key=lambda e: (e["title"], e["show_id"])))
        for entry in items:
            entry["evidence_records"] = sum(per_episode[episode]
                                            for episode in episode_sets[entry["show_id"]])
        page = items[:args["limit"]]
        return _base_envelope(ctx, con, "research", {"query": args["query"]},
                              page, len(items),
                              coverage={"scope": "workspace shows"},
                              limitations=[])
    finally:
        con.close()


def list_episodes(ctx: _access.AccessContext, request: dict, *,
                  budget: _access.ExecutionBudget | None = None) -> dict:
    args = parse_list_episodes(request)
    _authorize(ctx, "list_episodes")
    _budget(budget)
    con = _connect(ctx)
    try:
        from . import customer
        rows = [row for row in customer.permitted_episodes(con, ctx)
                if not args["filter"]["show_query"] or
                args["filter"]["show_query"].casefold() in (row.get("show_title") or "").casefold()]
        evidence = _collect_scope_items(con, _search.validate_filter({}), _budget(budget),
                                       ctx.permits("transcript"), ctx.permits("notes"),
                                       ctx=ctx, excerpts=False)
        per_episode = Counter(item["episode_id"] for item in evidence)
        filt = args["filter"]
        show_ids = None
        if filt.get("show_id"):
            try:
                show_ids = set(_search.resolve_show_id(con, filt["show_id"]))
            except _search.FilterRejected as exc:
                raise AgentError(INVALID_ARGUMENT, str(exc))
        items = []
        for row in rows:
            if show_ids is not None and row["id"] not in show_ids:
                continue
            pub = row.get("pub_date") or ""
            if filt["published_from"] and (not pub or pub < filt["published_from"]):
                continue
            if filt["published_to"] and (not pub or pub > filt["published_to"]):
                continue
            if not filt["include_unknown_dates"] and not pub:
                continue
            items.append({
                "id": row["id"], "show_title": row.get("show_title"),
                "episode_title": row.get("ep_title"),
                "category": row.get("category"), "pub_date": pub,
                "pub_date_known": bool(pub),
                **({"has_transcript": bool(row.get("transcript_version_id")) and
                    ctx.disclosure_allowed(con, f"transcript:{row['id']}", "transcript")}
                   if ctx.permits("transcript") else {}),
                "evidence_records": per_episode[row["id"]],
            })
        return _base_envelope(ctx, con, "research", args["filter"],
                              items[:args["limit"]], len(items),
                              coverage={"scope": "workspace episodes"},
                              limitations=[])
    finally:
        con.close()


class _Rights:
    """One call's per-source disclosure decisions (AGENT-A-SCOPE).

    Every decision is the one _collect_scope_items has always made per
    record, taken once per source key and category (and once per record
    pin triple) instead of once per record: within one read transaction
    a disclosure answer depends only on the source key and category, and
    ctx.disclosures records the same set either way."""

    def __init__(self, con, ctx):
        self.con, self.ctx = con, ctx
        self._keys: dict = {}
        self._allowed: dict = {}
        self._denied: dict = {}
        self._pairs: dict = {}

    def key(self, version_id):
        # The source_key alone (columns before the payload): the same
        # existence and key _version_provenance gives, without reading the
        # payload's overflow pages.
        if version_id not in self._keys:
            self._keys[version_id] = _citations._version_source_key(self.con, version_id)
        return self._keys[version_id]

    def allowed(self, source_key, category) -> bool:
        k = (source_key, category)
        if k not in self._allowed:
            self._allowed[k] = self.ctx.disclosure_allowed(self.con, source_key, category)
        return self._allowed[k]

    def transcript_denied(self, source_key) -> bool:
        if source_key not in self._denied:
            self._denied[source_key] = _citations.transcript_policy_denied(self.con, source_key)
        return self._denied[source_key]

    def decide(self, source_version_id, transcript_version_id, episode_id):
        """None (record not disclosed) or its field flags."""
        k = (source_version_id, transcript_version_id, episode_id)
        if k not in self._pairs:
            self._pairs[k] = self._decide(*k)
        return self._pairs[k]

    def _decide(self, source_version_id, transcript_version_id, episode_id):
        version_key = self.key(source_version_id or transcript_version_id)
        source_key = version_key if version_key is not None else f"episode:{episode_id}"
        if not self.allowed(source_key, "metadata"):
            return None
        excerpt_allowed = self.allowed(source_key, "excerpt")
        transcript_key = self.key(transcript_version_id)
        transcript = transcript_key is not None
        allowed_transcript = transcript and self.allowed(transcript_key, "transcript")
        allowed_notes = self.allowed(source_key, "notes")
        excerpt_level = transcript and not allowed_transcript and \
            self.transcript_denied(transcript_key)
        # Plan A with no pinned transcript: no safe excerpt exists; the
        # stored excerpt is neither text nor a match
        # (citations.untranscribed_policy_denied).
        untranscribed = not transcript and any(
            self.transcript_denied(key) for key in (version_key, f"episode:{episode_id}") if key)
        if excerpt_level:
            # Plan A (design 5): the excerpt needs excerpt on both sources,
            # and the item's text is its safe excerpt only.
            if not (excerpt_allowed and self.allowed(transcript_key, "metadata")
                    and self.allowed(transcript_key, "excerpt")):
                return None
        return {"excerpt_allowed": excerpt_allowed, "transcript": allowed_transcript,
                "notes": allowed_notes, "excerpt_level": excerpt_level,
                "untranscribed": untranscribed}


def _scope_order(filt: dict, brand_hidden) -> tuple[str, list]:
    """ops.search_evidence's ORDER BY for a normalized legacy filter."""
    if filt["sort"] == "brand":
        return _ops.brand_sort_order(brand_hidden)
    return ({"episode": "e.episode_id, e.orig_start_s, e.id",
             "start": "e.orig_start_s, e.id",
             "published": "CASE WHEN NULLIF(ep.pub_date,'') IS NULL THEN 1 ELSE 0 END,"
                          " ep.pub_date, e.id"}[filt["sort"]], [])


# Rows between budget checks in _collect_scope_items: a Plan A row costs a
# safe excerpt, so 1,000 rows ran ~1 s past the budget (AGA-R2-01).
SCOPE_CHECK_ROWS = 100


def _collect_scope_items(con, filt_v1, budget, transcripts_allowed,
                         notes_allowed, *, ctx, excerpts=True) -> list:
    """Every matching evidence item, re-checked against permitted fields.

    The same records, in the same order and with the same field flags, as
    walking ops.search_evidence page by page and checking each record
    (the pre-AGENT-A-SCOPE path), read in one indexed pass instead
    (AGENT-A-SCOPE): one SQL statement for the records, one for their
    brand associations, rights decided once per source, and a query
    matched over permitted fields with the transcript index narrowing the
    records whose interval text must be read (_match_scope_items). No
    statement runs per record except for records whose interval text or
    Plan A safe excerpt is needed.

    excerpts=False (counting callers that never show an excerpt, with no
    query): Plan A records carry no excerpt text instead of their safe
    excerpt. show_id resolves to an episode set here: the legacy SQL has
    no show concept (coordinator follow-up for the evidence path).
    """
    filt = dict(filt_v1)
    show_ids = None
    if filt.get("show_id"):
        try:
            show_ids = set(_search.resolve_show_id(con, filt["show_id"]))
        except _search.FilterRejected as exc:
            raise AgentError(INVALID_ARGUMENT, str(exc))
        filt["show_id"] = ""
    try:
        legacy = _search.to_legacy_filter(filt)
    except _search.FilterRejected as exc:
        raise AgentError(INVALID_ARGUMENT, str(exc))
    query = filt_v1.get("query", "")
    legacy["q"] = ""
    budget.check("scope collection")
    # Agents act for customers: a brand under review is no brand here.
    brand_hidden = _ad_reads.brand_review_ids(con)
    lf = _ops.normalize_filter(legacy)
    where, args = _ops._filter_where(lf, exclude_shows=tuple(_ops.hidden_show_titles(con)),
                                     con=con, brand_hidden=brand_hidden)
    order, order_args = _scope_order(lf, brand_hidden)
    assocs: dict = {}
    # PODS: model rows are display-only
    for a in con.execute("SELECT evidence_id, brand, basis, origin, channel FROM brand_assocs"
                         " WHERE basis<>'model' ORDER BY evidence_id, brand, id"):
        assocs.setdefault(a["evidence_id"], []).append(
            {"brand": a["brand"], "basis": a["basis"], "origin": a["origin"],
             "channel": a["channel"]})
    rights = _Rights(con, ctx)
    items = []
    # Plan A: each pinned transcript is decoded once for the whole pass,
    # not once per record (PLANA-SEARCH-SPEED, AGA-R2-01).
    transcripts: dict = {}
    rows = con.execute(
        "SELECT e.*, ep.show_title, ep.ep_title, ep.category, r.judgment,"
        " r.eff_start_s, r.eff_end_s, r.revision AS review_rev"
        " FROM evidence e JOIN episodes ep ON ep.id=e.episode_id"
        f" LEFT JOIN reviews r ON r.evidence_id=e.id WHERE {where} ORDER BY {order}",
        args + order_args)
    for n, row in enumerate(rows):
        if n % SCOPE_CHECK_ROWS == 0:
            budget.check("scope collection")
        if show_ids is not None and row["episode_id"] not in show_ids:
            continue
        flags = rights.decide(row["source_version_id"], row["transcript_version_id"],
                              row["episode_id"])
        if flags is None:
            continue
        item = dict(row)
        item["attrs"] = json.loads(item["attrs"])
        item["assocs"] = [dict(a) for a in assocs.get(item["id"], ())]
        _ops.hide_brand_guess(item, brand_hidden)
        item.update(_excerpt_allowed=flags["excerpt_allowed"],
                    _transcript_allowed=flags["transcript"], _notes_allowed=flags["notes"])
        if flags["excerpt_level"]:
            if excerpts or query:
                safe = _citations.current_safe_excerpt(con, item["id"],
                                                       transcripts=transcripts)
                item["excerpt"] = (safe or {}).get("text") or ""
            else:
                item["excerpt"] = ""
            item["_excerpt_level"] = True
        if flags["untranscribed"]:
            item["excerpt"] = ""
            item["_excerpt_level"] = True
        if not flags["excerpt_allowed"]:
            item["excerpt"] = ""
        items.append(item)
    if query:
        items = _match_scope_items(con, items, query, budget)
    return items


def _match_scope_items(con, items: list, query: str, budget) -> list:
    """The items search.permitted_evidence_match keeps for query, in
    order, reading interval text only where it can decide the match.

    The match text is "<fields> <interval> <notes>" (fields:
    search.permitted_fields_text; interval only with transcript
    permission, notes only with notes permission and when nonempty). A
    match inside the fields or the notes needs no interval. Any other
    match of a needle with no leading, trailing or inner space at a field
    seam must touch the interval text, so for an ASCII needle the
    record's interval text is read (and the match decided exactly) only
    when its fields end with a needle prefix before a space, its notes
    start with a needle suffix after a space, its transcript version is
    not indexed, or an indexed segment over its interval holds the
    needle's longest space-free piece (transcript_index; folded segment
    text holds every ASCII piece its case-folded text holds). A non-ASCII
    needle reads every permitted interval, as before.
    """
    needle = query.casefold()
    notes: dict = {}
    if any(i["_notes_allowed"] for i in items):
        notes = {r[0]: r[1] for r in con.execute("SELECT evidence_id, notes FROM reviews")}
    keep: set = set()
    pending: list = []
    for item in items:
        note = notes.get(item["id"], "") if item["_notes_allowed"] else None
        item_note = note if note else None
        if not item["_transcript_allowed"]:
            if _search.permitted_evidence_match(item, query, False, item["_notes_allowed"],
                                                "", note):
                keep.add(item["id"])
            continue
        fields = _search.permitted_fields_text(item).casefold()
        if needle in fields or (item_note and needle in item_note.casefold()):
            keep.add(item["id"])
            continue
        pending.append((item, note, fields))
    budget.check("scope match")
    candidates = _interval_candidates(con, pending, needle)
    cache: dict = {}
    for n, (item, note, _fields) in enumerate(pending):
        if item["id"] not in candidates:
            continue
        if n % 200 == 0:
            budget.check("scope match")
        interval = _ops.evidence_text(con, item, cache)
        if _search.permitted_evidence_match(item, query, True, item["_notes_allowed"],
                                            interval, note):
            keep.add(item["id"])
    return [item for item in items if item["id"] in keep]


def _interval_candidates(con, pending: list, needle: str) -> set:
    """Ids of the pending (item, note, folded fields) whose interval text
    may hold a match (_match_scope_items)."""
    from . import transcript_index as _tindex
    if not pending:
        return set()
    pieces = [p for p in needle.split(" ") if p]
    if not needle.isascii() or not pieces:
        return {item["id"] for item, _note, _fields in pending}
    out: set = set()
    by_version: dict = {}
    seams = [k for k, ch in enumerate(needle) if ch == " "]
    for item, note, fields in pending:
        note_cf = note.casefold() if note else ""
        if any(fields.endswith(needle[:k]) for k in seams) or \
                (note and any(note_cf.startswith(needle[k + 1:]) for k in seams)):
            out.add(item["id"])
            continue
        by_version.setdefault(item.get("transcript_version_id"), []).append(item)
    indexed = _tindex.indexed(con, [v for v in by_version if v is not None])
    for version, group in by_version.items():
        if version not in indexed:
            out.update(item["id"] for item in group)
    if not indexed:
        return out
    piece = max(pieces, key=len)
    versions = json.dumps(sorted(indexed))
    if len(piece) >= 3:
        segments = con.execute(
            f"SELECT s.version_id, s.start_s, s.end_s FROM {_tindex.FTS} f"
            f" JOIN {_tindex.SEGMENTS} s ON s.id=f.rowid WHERE {_tindex.FTS} MATCH ?"
            " AND s.version_id IN (SELECT value FROM json_each(?))",
            ('"' + piece.replace('"', '""') + '"', versions))
    else:
        segments = con.execute(
            f"SELECT version_id, start_s, end_s FROM {_tindex.SEGMENTS}"
            " WHERE version_id IN (SELECT value FROM json_each(?)) AND instr(folded, ?) > 0",
            (versions, piece))
    bounds: dict = {}
    for version in indexed:
        for item in by_version.get(version, ()):
            start = item["eff_start_s"] if item.get("eff_start_s") is not None else item.get("orig_start_s")
            end = item["eff_end_s"] if item.get("eff_end_s") is not None else item.get("orig_end_s")
            if start is not None and end is not None:  # else no interval text
                bounds.setdefault(version, []).append((item["id"], start, end))
    for version, seg_start, seg_end in segments:
        for item_id, start, end in bounds.get(version, ()):
            if seg_start is None or seg_end is None or (seg_start < end and seg_end > start):
                out.add(item_id)
    return out


def _episode_lookup(con):
    def lookup(episode_id):
        row = con.execute("SELECT id, kind, show_title, pub_date FROM episodes"
                          " WHERE id=?", (episode_id,)).fetchone()
        return dict(row) if row else None
    return lookup


def summarize_evidence(ctx: _access.AccessContext, request: dict, *,
                       budget: _access.ExecutionBudget | None = None) -> dict:
    args = parse_summarize_evidence(request)
    _authorize(ctx, "summarize_evidence")
    budget = _budget(budget)
    con = _connect(ctx)
    try:
        transcripts_allowed = ctx.permits("transcript")
        notes_allowed = ctx.permits("notes")
        items = _collect_scope_items(con, args["filter"], budget, transcripts_allowed, notes_allowed, ctx=ctx)
        summary = _search.summarize_groups(items, args["group_by"], LIST_HARD_MAX,
                                           _episode_lookup(con))
        offset = 0
        if args["cursor"] is not None:
            body = decode_cursor(_cursor_secret(ctx.workspace_id), args["cursor"])
            if body.get("workspace_id") != ctx.workspace_id:
                raise AgentError(INVALID_ARGUMENT, "cursor belongs to another workspace")
            if body.get("filter_digest") != _search.filter_digest_for(
                    {**args["filter"], "group_by": args["group_by"]},
                    args["filter"]["query"]):
                raise AgentError(INVALID_ARGUMENT,
                                 "cursor belongs to a different query; restart it")
            current_revision = _project_revision(con, "research", notes_allowed, args["filter"]["history"], ctx=ctx)
            if body.get("view_revision") != current_revision:
                raise AgentError(RESULT_SET_CHANGED,
                                 "collection changed during traversal; restart the query")
            offset = (body.get("position") or {}).get("group_offset", 0)
            if not isinstance(offset, int) or offset < 0:
                raise AgentError(INVALID_ARGUMENT, "cursor is malformed")
        groups = summary["groups"][offset:offset + args["limit"]]
        revision = _project_revision(con, "research", notes_allowed, args["filter"]["history"], ctx=ctx)
        next_cursor = None
        if offset + args["limit"] < len(summary["groups"]):
            next_cursor = encode_cursor(_cursor_secret(ctx.workspace_id), {
                "workspace_id": ctx.workspace_id,
                "filter_digest": _search.filter_digest_for(
                    {**args["filter"], "group_by": args["group_by"]},
                    args["filter"]["query"]),
                "view_revision": revision,
                "sort_key": "summarize:" + args["group_by"],
                "page_size": args["limit"],
                "position": {"group_offset": offset + args["limit"]}})
        data = {**summary, "groups": groups,
                "returned_groups": len(groups), "group_offset": offset}
        result = _base_envelope(ctx, con, "research",
                                {**args["filter"], "group_by": args["group_by"]},
                                [data], 1,
                                coverage={"scope": "authorized evidence scope"},
                                limitations=["CANDIDATES_NOT_UNIQUE_ADS",
                                             "NO_MATCH_IS_NOT_ABSENCE",
                                             "COUNTS_ARE_EVIDENCE_RECORDS"])
        result["view_revision"] = revision
        result["next_cursor"] = next_cursor
        result["truncated"] = next_cursor is not None
        result["truncation_reason"] = "PAGE_LIMIT" if next_cursor else None
        return result
    finally:
        con.close()


def list_watchlists(ctx: _access.AccessContext, request: dict, *,
                    budget: _access.ExecutionBudget | None = None) -> dict:
    args = parse_list_watchlists(request)
    _authorize(ctx, "list_watchlists")
    _budget(budget)
    con = _connect(ctx)
    try:
        if not ctx.permits("watchlist"):
            raise AgentError(FORBIDDEN, "watchlist disclosure denied")
        rows = [row for row in _ops.list_watchlists(con)
                if _citations.resource_allowed(con, "watchlist", row["id"], ctx)]
        items = [{"id": row["id"], "name": row["name"],
                  "definition_revision": row["revision"],
                  "definition": json.loads(row["query"])}
                 for row in rows]
        result = _base_envelope(ctx, con, "research", {}, items[:args["limit"]],
                                len(items), coverage={"scope": "saved searches"},
                                limitations=[])
        result["view_revision"] = _citations._state_digest(
            "watchlists", ctx.policy_identity, {"items": items})
        return result
    finally:
        con.close()


def get_watchlist_results(ctx: _access.AccessContext, request: dict, *,
                          budget: _access.ExecutionBudget | None = None) -> dict:
    args = parse_get_watchlist_results(request)
    _authorize(ctx, "get_watchlist_results")
    _budget(budget)
    con = _connect(ctx)
    try:
        row = con.execute("SELECT * FROM watchlists WHERE id=?",
                          (args["watchlist_id"],)).fetchone()
        if row is None:
            raise AgentError(NOT_FOUND, "unknown watchlist")
        _citations.authorize_resource(con, "watchlist", row["id"], ctx)
        from . import conversion
        conversion.require_executable(con, "native_filter", args["watchlist_id"])
        expected = args["expected_definition_revision"]
        if expected is not None and expected != row["revision"]:
            raise AgentError(REVISION_CONFLICT, "stale definition revision",
                             details={"current_definition_revision":
                                      row["revision"]})
        try:
            stored = json.loads(row["query"])
        except (TypeError, ValueError) as exc:
            raise AgentError(INVALID_ARGUMENT, "watchlist needs_correction",
                             details={"needs_correction": ["saved filter is not valid JSON"]}) from exc
        filt_v1 = require_saved_meaning(stored, "watchlist")
        items = _collect_scope_items(con, filt_v1, _budget(budget), ctx.permits("transcript"),
                                     ctx.permits("notes"), ctx=ctx, excerpts=False)
        data = {"id": row["id"], "name": row["name"],
                "definition_revision": row["revision"], "definition": stored,
                "match_count": len(items),
                "match_ids": [i["id"] for i in items[:args["limit"]]],
                "evaluated_at": _db.utcnow()}
        result = _base_envelope(ctx, con, "research", {"watchlist_id":
                              args["watchlist_id"]}, [data], 1,
                              coverage={"scope": "saved search evaluation"},
                              limitations=["CANDIDATES_NOT_UNIQUE_ADS",
                                           "NO_MATCH_IS_NOT_ABSENCE"])
        result["view_revision"] = _citations._state_digest(
            "watchlist", ctx.policy_identity,
            {"id": row["id"], "definition": stored,
             "revision": row["revision"], "matches": [item["id"] for item in items]})
        return result
    finally:
        con.close()


def list_campaigns(ctx: _access.AccessContext, request: dict, *,
                   budget: _access.ExecutionBudget | None = None) -> dict:
    args = parse_list_campaigns(request)
    _authorize(ctx, "list_campaigns")
    _budget(budget)
    con = _connect(ctx)
    try:
        items = []
        for campaign in _ops.list_campaigns(con, ctx=ctx):
            if args["status"] != "all" and campaign["status"] != args["status"]:
                continue
            items.append({
                "id": campaign["id"], "name": campaign["name"],
                "status": campaign["status"], "revision": campaign["revision"],
                "brand": campaign.get("brand"),
                "attachment_count": con.execute(
                    "SELECT COUNT(*) c FROM campaign_evidence WHERE campaign_id=?",
                    (campaign["id"],)).fetchone()["c"],
                "check_count": con.execute(
                    "SELECT COUNT(*) c FROM campaign_checks WHERE campaign_id=?",
                    (campaign["id"],)).fetchone()["c"],
            })
        result = _base_envelope(ctx, con, "research", {"status": args["status"]},
                               items[:args["limit"]], len(items),
                               coverage={"scope": "campaigns"}, limitations=[])
        result["view_revision"] = _citations._state_digest(
            "campaigns", ctx.policy_identity, {"items": items})
        return result
    finally:
        con.close()


def get_campaign(ctx: _access.AccessContext, request: dict, *,
                 budget: _access.ExecutionBudget | None = None) -> dict:
    args = parse_get_campaign(request)
    _authorize(ctx, "get_campaign")
    _budget(budget)
    con = _connect(ctx)
    try:
        detail = _ops.campaign_detail(con, args["campaign_id"], ctx=ctx)
        if detail is None:
            raise AgentError(NOT_FOUND, "unknown campaign")
        if args["revision"] is not None and args["revision"] != detail["revision"]:
            raise AgentError(REVISION_CONFLICT, "stale campaign revision",
                             details={"current_revision": detail["revision"]})
        research_allowed = RESEARCH_READ in ctx.scopes
        notes_allowed = ctx.permits("notes")
        limitations = []
        if research_allowed:
            attachments = []
            for evidence in detail["evidence"][:LIST_HARD_MAX]:
                full = _services.evidence_detail(ctx, con, evidence["id"])
                review = dict(full.get("review") or {})
                # Evidence review notes need notes:read; without it they
                # (and their length metadata) are omitted entirely. The
                # campaign brief and manual assessment rationale below stay
                # campaign fields, visible with campaigns:read.
                if not full.get("_notes_allowed"):
                    review.pop("notes", None)
                    review.pop("revision", None)
                attachments.append({
                    "id": evidence["id"], "episode_id": evidence.get("episode_id"),
                    "original_bounds_s": [evidence.get("orig_start_s"),
                                          evidence.get("orig_end_s")],
                    **({"excerpt_preview": (full.get("excerpt") or "")[:EXCERPT_LIST_MAX]}
                       if full.get("_excerpt_allowed") else {}),
                    "review": review,
                })
            checks = []
            for check in detail["checks"]:
                result = dict(check.get("result") or {})
                checks.append({**check, "result": result})
            if len(detail["evidence"]) > LIST_HARD_MAX:
                limitations.append("ATTACHMENTS_TRUNCATED")
        else:
            attachments = []
            checks = [{**c, "result": {"outcome": "unknown",
                                       "reason": "supporting evidence is not"
                                                 " in the authorized scope"}}
                      for c in detail["checks"]]
            limitations.append("INCOMPLETE_SUPPORT")
        data = {
            "id": detail["id"], "name": detail["name"],
            "status": detail["status"], "revision": detail["revision"],
            "brand": detail.get("brand"),
            "brief": {"message": detail.get("message"),
                      "offer": detail.get("offer"),
                      "notes": detail.get("notes"),
                      "target_shows": detail.get("target_shows"),
                      "date_from": detail.get("date_from"),
                      "date_to": detail.get("date_to")},
            "manual_assessment": {
                "conclusion": detail.get("manual_conclusion"),
                "reason": detail.get("manual_reason")},
            "attachment_count": len(attachments),
            "attachments": attachments,
            "checks": checks,
        }
        result = _base_envelope(ctx, con, "research", {"campaign_id":
                               args["campaign_id"]}, [data], 1,
                               coverage={"scope": "single campaign"},
                               limitations=limitations)
        result["view_revision"] = _citations._state_digest(
            "campaign", ctx.policy_identity, {"campaign": data})
        return result
    finally:
        con.close()


def get_source_provenance(ctx: _access.AccessContext, request: dict, *,
                          budget: _access.ExecutionBudget | None = None) -> dict:
    args = parse_get_source_provenance(request)
    _authorize(ctx, "get_source_provenance")
    _budget(budget)
    con = _connect(ctx)
    try:
        if "evidence_id" in args:
            try:
                citation = _citations.evidence_citation(con, args["evidence_id"])
            except _citations.CitationError:
                raise AgentError(NOT_FOUND, "unknown evidence")
            data = {"for_evidence": args["evidence_id"],
                    "source": citation.get("source"),
                    "source_pin": citation.get("source_pin"),
                    "transcript_version_id": citation.get("transcript_version_id"),
                    "fetch_date": citation.get("fetch_date"),
                    "fetch_date_source": citation.get("fetch_date_source")}
        else:
            pin = _citations.source_pin(con, args["source_version_id"])
            if pin is None:
                raise AgentError(NOT_FOUND, "unknown source version")
            data = {"for_source_version": args["source_version_id"],
                    "source_pin": pin}
        pin = data.get("source_pin") or {}
        key = pin.get("source_key")
        if not key:
            raise AgentError(NOT_FOUND, "source provenance unavailable")
        ctx.authorize_disclosure(con, key, ["metadata"])
        category = "transcript" if key.startswith("transcript:") else "excerpt"
        if not ctx.disclosure_allowed(con, key, category):
            data.pop("source_pin", None)
            data.pop("transcript_version_id", None)
        return _base_envelope(ctx, con, "research", args, [data], 1,
                              coverage={"scope": "source provenance"},
                              limitations=[])
    finally:
        con.close()


def search(ctx: _access.AccessContext, request: dict, *,
           budget: _access.ExecutionBudget | None = None) -> dict:
    """Compatibility discovery via the single shared implementation.

    Delegates to the compatibility builders in :mod:`earwitness.agents`
    (same authorized services as every native tool); the MCP transport
    calls the same builders. The import is deferred because that module
    imports this one. Entry authorization stays here: membership plus at
    least one document scope, then the builders re-check their own
    document kinds.
    """
    parse_search(request)
    ctx.require_member("search")
    if RESEARCH_READ not in ctx.scopes and TRANSCRIPTS_READ not in ctx.scopes:
        ctx.require_scope(RESEARCH_READ)
    budget = _budget(budget)
    from earwitness.agents import compat_search
    return compat_search(ctx, request, budget=budget)


def fetch(ctx: _access.AccessContext, request: dict, *,
          budget: _access.ExecutionBudget | None = None) -> dict:
    """Compatibility retrieval via the single shared implementation.

    Same delegation contract as :func:`search`: one implementation in
    the compatibility builders, shared by MCP, HTTP, and in-process
    callers of :data:`AGENT_ENTRYPOINTS`.
    """
    parse_fetch(request)
    ctx.require_member("fetch")
    if RESEARCH_READ not in ctx.scopes and TRANSCRIPTS_READ not in ctx.scopes:
        ctx.require_scope(RESEARCH_READ)
    budget = _budget(budget)
    from earwitness.agents import compat_fetch
    return compat_fetch(ctx, request, budget=budget)


AGENT_ENTRYPOINTS = {
    "get_workspace_context": get_workspace_context,
    "search_evidence": search_evidence,
    "get_evidence": get_evidence,
    "search_transcripts": search_transcripts,
    "get_transcript_window": get_transcript_window,
    "list_brands": list_brands,
    "list_shows": list_shows,
    "list_episodes": list_episodes,
    "summarize_evidence": summarize_evidence,
    "list_watchlists": list_watchlists,
    "get_watchlist_results": get_watchlist_results,
    "list_campaigns": list_campaigns,
    "get_campaign": get_campaign,
    "get_source_provenance": get_source_provenance,
    "search": search,
    "fetch": fetch,
}
