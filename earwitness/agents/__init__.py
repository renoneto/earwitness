"""EarWitness local MCP reader: scoped tool catalog + domain handlers (package D).

No MCP SDK import here: this module is pure domain logic over the frozen
agent contracts, so contract/security regressions run on the system Python.
``mcp_server.py`` adds the protocol mapping only.

Sixteen tools in deterministic catalog order. Narrower grants advertise only
permitted tools; guessed calls for hidden tools are denied, never served.
``search``/``fetch`` are the §7 compatibility pair over the same authorized
services; the document composition lives here (D-owned) and the
agent_contracts entrypoints delegate to it, so MCP, HTTP, and in-process
callers share one implementation.
"""
from __future__ import annotations

import json
import uuid

from earwitness.app import access as _access
from earwitness.app import agent_contracts as _contracts
from earwitness.app import citations as _citations
from earwitness.app import ops as _ops
from earwitness.app import search as _search

RESEARCH_READ = _contracts.RESEARCH_READ
TRANSCRIPTS_READ = _contracts.TRANSCRIPTS_READ
WATCHLISTS_READ = _contracts.WATCHLISTS_READ
CAMPAIGNS_READ = _contracts.CAMPAIGNS_READ
NOTES_READ = _contracts.NOTES_READ

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False,
             "idempotentHint": True, "openWorldHint": False}

COMPAT_MAX_RESULTS = 20
FETCH_TEXT_MAX_BYTES = 8 * 1024

_CANONICAL_BASE = "ew-local://"


def canonical_evidence_url(workspace_id: str, evidence_id: str) -> str:
    """Citation identifier for an evidence document. Supports citation; it
    grants no access to the resource."""
    return f"{_CANONICAL_BASE}/w/{workspace_id}/evidence/{evidence_id}"


def canonical_transcript_url(workspace_id: str, version_id: int, seg_index: int,
                             char_start: int, char_end: int) -> str:
    return (f"{_CANONICAL_BASE}/w/{workspace_id}/transcripts/{version_id}"
            f"/segments/{seg_index}/{char_start}-{char_end}")


class ToolFailure(Exception):
    """A typed tool-domain failure with a §13 code and HTTP status."""

    def __init__(self, code: str, message: str, status: int | None = None,
                 details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = dict(details or {})

    def envelope(self) -> dict:
        return {"code": self.code, "message": str(self),
                "status": self.status, "details": self.details}


def _fail_from(exc: Exception, request_id: str) -> ToolFailure:
    status, code, message = _contracts.error_for_agent(exc)
    if code == _contracts.INTERNAL:
        message = f"internal error (request {request_id})"
    # HQ-1: preserve the needs_correction diagnostic across the transport
    # boundary. It describes the caller's own saved filter (field names +
    # reason strings), never workspace content, so it is safe to forward;
    # every other details key stays dropped.
    details: dict = {}
    if code != _contracts.INTERNAL and isinstance(exc, _contracts.AgentError):
        reasons = (exc.details or {}).get("needs_correction")
        if reasons:
            details = {"needs_correction": list(reasons)}
    return ToolFailure(code, message, status, details=details)


def _prop(ptype: str, description: str) -> dict:
    return {"type": ptype, "description": description}


def _obj(description: str) -> dict:
    return {"type": "object", "description": description}


def _schema(properties: dict, required: tuple = ()) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(required), "additionalProperties": False}


_FILTER_DESC = ("Strict ResearchFilterV1 object (literal query text, closed "
                "enums, no SQL/regex/FTS grammar). Unknown fields reject.")
_LIMIT_DESC = "Maximum items to return (upper bound; default 20)."
_CURSOR_DESC = "Opaque continuation cursor from a previous page of the same query."


def _filter_limit_cursor(tool: str) -> dict:
    return _schema({"filter": _obj(_FILTER_DESC), "limit": _prop("integer", _LIMIT_DESC),
                    "cursor": _prop("string", _CURSOR_DESC)})


TOOL_DEFS = (
    {"name": "get_workspace_context", "scopes": (),
     "description": ("Selected workspace identity, available scopes and tools, "
                     "coverage, units, limits, connection mode, and caveats. "
                     "Reads only; no side effects."),
     "schema": _schema({})},
    {"name": "search_evidence", "scopes": (RESEARCH_READ,),
     "description": ("Bounded evidence summaries over excerpts, titles, "
                     "associations, and pinned interval text. Results are "
                     "candidates, never verified ads; every hit carries "
                     "match_basis. Reads only; no side effects."),
     "schema": _filter_limit_cursor("search_evidence")},
    {"name": "get_evidence", "scopes": (RESEARCH_READ,),
     "description": ("One evidence record: bounds, attribution channels, review "
                     "provenance, overlaps, source hashes, citation. Review "
                     "notes appear only with notes:read. Reads only."),
     "schema": _schema({"evidence_id": _prop("string", "Opaque evidence id."),
                        "revision": _prop("string", "Optional exact revision "
                                           "reference from search.")})},
    {"name": "search_transcripts", "scopes": (TRANSCRIPTS_READ,),
     "description": ("Whole-word search over timestamped transcript segments "
                     "across the authorized collection, including episodes "
                     "with no detected evidence, as product Search matches: "
                     "every query word in one segment, ignoring case and "
                     "accents; \"quoted\" words match as a phrase. Newest "
                     "episodes first; total_matches counts matching segments "
                     "exactly. No ad judgment is inferred. Reads only."),
     "schema": _schema({"query": _prop("string", "Words to find, max 200 chars."),
                        "episode_id": _prop("string", "Optional episode id."),
                        "show_id": _prop("string", "Optional stable show id."),
                        "show_query": _prop("string", "Optional show title substring."),
                        "published_from": _prop("string", "Optional ISO start date."),
                        "published_to": _prop("string", "Optional ISO end date."),
                        "include_unknown_dates": _prop("boolean", "Include unknown-date episodes."),
                        "source_version_id": _prop("string", "Optional pinned transcript version id."),
                        "limit": _prop("integer", _LIMIT_DESC),
                        "cursor": _prop("string", _CURSOR_DESC)}, ("query",))},
    {"name": "get_transcript_window", "scopes": (TRANSCRIPTS_READ,),
     "description": ("Ordered source segments for a pinned transcript version "
                     "inside an explicit time window. Never substitutes the "
                     "current transcript for the pinned one. Reads only."),
     "schema": _schema({"episode_id": _prop("string", "Episode id."),
                        "transcript_version_id": _prop("integer", "Exact pinned transcript version."),
                        "start_s": _prop("number", "Window start seconds."),
                        "end_s": _prop("number", "Window end seconds (at most 300s span).")},
                       ("episode_id", "transcript_version_id", "start_s", "end_s"))},
    {"name": "list_brands", "scopes": (RESEARCH_READ,),
     "description": ("Brand/channel/basis summaries with explicit evidence-"
                     "record count units. Overlapping groups are not mutually "
                     "exclusive. Reads only."),
     "schema": _schema({"query": _prop("string", "Optional brand substring, max 200 chars."),
                        "filter": _obj(_FILTER_DESC),
                        "limit": _prop("integer", _LIMIT_DESC),
                        "cursor": _prop("string", _CURSOR_DESC)})},
    {"name": "list_shows", "scopes": (RESEARCH_READ,),
     "description": ("Stable workspace show identities and coverage. Titles "
                     "alone never merge unrelated shows; ambiguous identities "
                     "stay explicit. Reads only."),
     "schema": _schema({"query": _prop("string", "Optional title substring, max 200 chars."),
                        "limit": _prop("integer", _LIMIT_DESC),
                        "cursor": _prop("string", _CURSOR_DESC)})},
    {"name": "list_episodes", "scopes": (RESEARCH_READ,),
     "description": ("Episode metadata, transcript availability, canonical "
                     "aliases, known/unknown dates. Reads only."),
     "schema": _schema({"show_id": _prop("string", "Optional stable show id."),
                        "query": _prop("string", "Optional title substring."),
                        "published_from": _prop("string", "Optional ISO start date."),
                        "published_to": _prop("string", "Optional ISO end date."),
                        "include_unknown_dates": _prop("boolean", "Include unknown-date episodes."),
                        "limit": _prop("integer", _LIMIT_DESC),
                        "cursor": _prop("string", _CURSOR_DESC)})},
    {"name": "summarize_evidence", "scopes": (RESEARCH_READ,),
     "description": ("Deterministic aggregates by source/brand/show/review/"
                     "date with exact units and denominators. Never ad "
                     "counts, spend, reach, or impressions. Reads only."),
     "schema": _schema({"filter": _obj(_FILTER_DESC),
                        "group_by": _prop("string", "One of source, brand, show, review, date."),
                        "limit": _prop("integer", _LIMIT_DESC),
                        "cursor": _prop("string", _CURSOR_DESC)}, ("group_by",))},
    {"name": "list_watchlists", "scopes": (WATCHLISTS_READ,),
     "description": ("Saved search names, definitions, and revisions. No live "
                     "alerts are implied. Reads only."),
     "schema": _schema({"limit": _prop("integer", _LIMIT_DESC),
                        "cursor": _prop("string", _CURSOR_DESC)})},
    {"name": "get_watchlist_results", "scopes": (WATCHLISTS_READ, RESEARCH_READ),
     "description": ("One saved search definition with present-corpus matches "
                     "and evaluation time. Definition changes are explicit. "
                     "Reads only."),
     "schema": _schema({"watchlist_id": _prop("integer", "Saved search id."),
                        "expected_definition_revision": _prop("integer", "Optional expected definition revision."),
                        "limit": _prop("integer", _LIMIT_DESC),
                        "cursor": _prop("string", _CURSOR_DESC)}, ("watchlist_id",))},
    {"name": "list_campaigns", "scopes": (CAMPAIGNS_READ,),
     "description": ("Campaign brief summaries, revisions, attachment and "
                     "check counts. Reads only."),
     "schema": _schema({"status": _prop("string", "One of active, archived, all."),
                        "limit": _prop("integer", _LIMIT_DESC),
                        "cursor": _prop("string", _CURSOR_DESC)})},
    {"name": "get_campaign", "scopes": (CAMPAIGNS_READ,),
     "description": ("Campaign brief, evidence, computed checks, separate "
                     "manual assessments, missing prerequisites. Notes gated. "
                     "Reads only."),
     "schema": _schema({"campaign_id": _prop("integer", "Campaign id."),
                        "revision": _prop("integer", "Optional exact revision.")},
                       ("campaign_id",))},
    {"name": "get_source_provenance", "scopes": (RESEARCH_READ,),
     "description": ("Typed source identity, hash, dates, provenance, and "
                     "active/history state for one evidence id or source "
                     "version id (exactly one). Reads only."),
     "schema": _schema({"evidence_id": _prop("string", "Evidence id (omit when using source_version_id)."),
                        "source_version_id": _prop("integer", "Source version id (omit when using evidence_id).")})},
    {"name": "search", "scopes": (),
     "description": ("Compatibility discovery (OpenAI search/fetch profile): "
                     "literal query over authorized evidence-card fields and, "
                     "with transcripts:read, transcript text. At most 20 "
                     "matches in deterministic order; deliberately "
                     "non-exhaustive — use native tools for complete counts. "
                     "Use concise terms, not a full question. Reads only."),
     "schema": _schema({"query": _prop("string", "Literal discovery query, max 200 chars.")},
                       ("query",))},
    {"name": "fetch", "scopes": (),
     "description": ("Compatibility retrieval: one bounded document or chunk "
                     "found by search, with citation URL and metadata. "
                     "Metadata-only episodes, campaigns, watchlists, and "
                     "private notes have no compatibility documents; their "
                     "native tools are required. Reads only."),
     "schema": _schema({"id": _prop("string", "Document or chunk id issued by search.")},
                       ("id",))},
)

_BY_NAME = {d["name"]: d for d in TOOL_DEFS}


def tools_for_scopes(scopes) -> list:
    """Deterministic scoped catalog: a tool is advertised only when every
    required scope is granted. search/fetch additionally require at least one
    of research/transcripts (a document kind must be authorized)."""
    have = set(scopes or ())
    out = []
    for tool in TOOL_DEFS:
        required = set(tool["scopes"])
        if tool["name"] in ("search", "fetch"):
            if not (RESEARCH_READ in have or TRANSCRIPTS_READ in have):
                continue
        elif not required <= have:
            continue
        out.append(tool)
    return out


def _local_context(workspace_id: str, scopes) -> _access.AccessContext:
    from earwitness.app import connections
    try:
        path = _contracts._WORKSPACES[workspace_id]
        return connections.local_reader_context(workspace_id, path, scopes)
    except connections.GrantError as exc:
        raise ToolFailure(_contracts.FORBIDDEN, str(exc))
    except KeyError:
        raise ToolFailure(_contracts.NOT_FOUND, "unknown workspace")


def _native(name: str):
    try:
        return _contracts.AGENT_ENTRYPOINTS[name]
    except KeyError:
        raise ToolFailure(_contracts.INVALID_ARGUMENT, f"unknown tool: {name}")


def call_tool(workspace_id: str, scopes, name: str, arguments: dict | None,
              *, budget=None, context=None) -> dict:
    """Run one tool against the bound workspace. Returns the result payload.

    Raises ToolFailure with a §13 code for domain failures (including
    guessed calls for tools outside the caller's catalog and oversized
    results). Never leaks stacks, paths, SQL, or private payloads.
    """
    tool = _BY_NAME.get(name)
    if tool is None:
        raise ToolFailure(_contracts.INVALID_ARGUMENT, f"unknown tool: {name}")
    advertised = {t["name"] for t in tools_for_scopes(scopes)}
    if name not in advertised:
        missing = [s for s in tool["scopes"] if s not in set(scopes or ())]
        if name in ("search", "fetch"):
            missing = [RESEARCH_READ]
        raise ToolFailure(_contracts.FORBIDDEN,
                          f"tool {name} is not in this grant; consent to "
                          f"{', '.join(missing) or 'a research scope'} first")
    ctx = context or _local_context(workspace_id, scopes)
    try:
        if name == "search":
            payload = compat_search(ctx, dict(arguments or {}), budget=budget)
        elif name == "fetch":
            payload = compat_fetch(ctx, dict(arguments or {}), budget=budget)
        else:
            payload = _native(name)(ctx, dict(arguments or {}), budget=budget)
    except ToolFailure:
        raise
    except Exception as exc:
        raise _fail_from(exc, ctx.request_id)
    fits, _ = _contracts.check_result_fit(payload)
    if not fits:
        raise ToolFailure(_contracts.RESULT_TOO_LARGE,
                          f"tool {name} result exceeds the 64KiB wire cap; "
                          "narrow the query or page further")
    if ctx.credential_kind == "local_reader":
        from earwitness.app import services, db
        from pathlib import Path
        control = ctx.control_store._connect()
        try:
            with db.transaction(control, write=True):
                services.fenced_authority_check(
                    ctx.control_store, str(Path(ctx.db_path).parent), ctx,
                    operation=name, control=control)
                if name != "get_workspace_context":
                    services.record_success(control, ctx, name)
        except Exception as exc:
            raise _fail_from(exc, ctx.request_id)
        finally:
            control.close()
    if name != "get_workspace_context":
        ctx.completed_operations.add(name)
    return payload


# -- §7 compatibility pair (D-owned document composition) --------------------
#
# The single compatibility implementation lives here and calls the same
# authorized services (native search_evidence/search_transcripts/get_evidence
# plus ops/search/citations reads). The agent_contracts search/fetch
# entrypoints delegate here (carryover wiring: no frozen stubs); the MCP
# transport calls these builders directly. Native calls enforce their own
# scopes, so a document kind is reachable only with its scope.

_EVIDENCE_DOC_KIND = "evidence"
_TRANSCRIPT_DOC_KIND = "transcript_chunk"


def _evidence_doc(workspace_id: str, item: dict) -> dict:
    excerpt = item.get("excerpt_preview") or ""
    title = excerpt[:120] if len(excerpt) <= 120 else excerpt[:117] + "..."
    return {"id": f"ev:{item['id']}",
            "title": title or f"Evidence {item['id']}",
            "url": canonical_evidence_url(workspace_id, item["id"])}


def _transcript_doc(workspace_id: str, hit: dict) -> dict:
    version = hit["transcript_version_id"]
    seg = hit["segment_index"]
    start, end = hit["window_start"], hit["window_end"]
    quote = (hit.get("text") or "")[:120]
    return {"id": f"tr:{version}:{seg}:{start}-{end}",
            "title": (f"Transcript {hit['episode_id']} segment {seg} "
                      f"({hit.get('start_s')}-{hit.get('end_s')}s): {quote}"),
            "url": canonical_transcript_url(workspace_id, version, seg,
                                            start, end)}


def compat_search(ctx, request: dict, *, budget=None) -> dict:
    """Literal compatibility discovery: at most 20 matches, deterministic
    order (evidence docs in search order, then transcript chunks in scan
    order), deliberately non-exhaustive."""
    args = _contracts.parse_search(request)
    research = RESEARCH_READ in ctx.scopes
    transcripts = TRANSCRIPTS_READ in ctx.scopes
    if not (research or transcripts):
        raise _contracts.AgentError(
            _contracts.FORBIDDEN,
            "search needs research:read or transcripts:read consent first")
    budget = _access.ExecutionBudget(deadline_s=10.0) if budget is None else budget
    results: list[dict] = []
    if research:
        page = _contracts.search_evidence(
            ctx, {"filter": {"query": args["query"]}, "limit": 20},
            budget=budget)
        for item in page.get("items", []):
            results.append(_evidence_doc(ctx.workspace_id, item))
            if len(results) >= COMPAT_MAX_RESULTS:
                break
    if transcripts and len(results) < COMPAT_MAX_RESULTS:
        page = _contracts.search_transcripts(
            ctx, {"query": args["query"], "limit": 20}, budget=budget)
        for hit in page.get("items", []):
            results.append(_transcript_doc(ctx.workspace_id, hit))
            if len(results) >= COMPAT_MAX_RESULTS:
                break
    return {"results": results}


def _fetch_evidence_doc(ctx, evidence_id: str, budget) -> dict:
    try:
        page = _contracts.get_evidence(ctx, {"evidence_id": evidence_id},
                                       budget=budget)
    except _contracts.AgentError:
        raise
    except Exception as exc:
        raise _fail_from(exc, ctx.request_id)
    data = (page.get("items") or [{}])[0]
    review = data.get("review") or {}
    lines = [
        f"Evidence {data.get('id')} — status: "
        f"{review.get('judgment') or 'unreviewed'} (a candidate, not a verified ad).",
        f"Source: {data.get('source')} · Episode: {data.get('episode_id')} · "
        f"Fetched: {data.get('fetch_date')} (source: {data.get('fetch_date_source')}).",
        "Attribution: " + ("; ".join(
            f"{a.get('brand')} ({a.get('channel')}, {a.get('basis')})"
            # PODS: model rows are display-only
            for a in data.get("attribution", []) if a.get("basis") != "model") or "none recorded") + ".",
        f"Excerpt: {data.get('excerpt_preview') or ''}",
    ]
    if TRANSCRIPTS_READ in ctx.scopes and data.get("interval_text_preview"):
        lines.append(f"Transcript interval {data.get('original_bounds_s')}: "
                     f"\"{data.get('interval_text_preview')}\"")
    lines.append("Coverage: single authorized evidence record. Limitations: "
                 "CANDIDATES_NOT_UNIQUE_ADS (not a verified ad), "
                 "AUDIO_ALIGNMENT_UNKNOWN (transcript and audio not verified aligned).")
    text = "\n".join(lines)
    if len(text.encode("utf-8")) > FETCH_TEXT_MAX_BYTES:
        raise ToolFailure(_contracts.INTERNAL,
                          "compatibility evidence document exceeds 8KiB")
    url = canonical_evidence_url(ctx.workspace_id, data.get("id"))
    return {"id": f"ev:{data.get('id')}", "title": (data.get("excerpt_preview") or "")[:120],
            "text": text, "url": url,
            "metadata": {"kind": _EVIDENCE_DOC_KIND,
                         "evidence_id": data.get("id"),
                         **({"revision_ref": data["revision_ref"]} if "revision_ref" in data else {}),
                         "source": data.get("source"),
                         "fetch_date": data.get("fetch_date"),
                         "judgment": review.get("judgment"),
                         "attribution": data.get("attribution"),
                         "limitations": ["CANDIDATES_NOT_UNIQUE_ADS",
                                         "AUDIO_ALIGNMENT_UNKNOWN"]}}


def _fetch_transcript_doc(ctx, rest: str, budget) -> dict:
    if TRANSCRIPTS_READ not in ctx.scopes:
        raise _contracts.AgentError(
            _contracts.FORBIDDEN,
            "transcript documents need transcripts:read consent first")
    try:
        version_s, seg_s, span = rest.split(":")
        version, seg_idx = int(version_s), int(seg_s)
        start_s, end_s = span.split("-")
        char_start, char_end = int(start_s), int(end_s)
        if version < 1 or seg_idx < 0 or not 0 <= char_start < char_end:
            raise ValueError("bounds")
    except (ValueError, AttributeError):
        raise _contracts.AgentError(
            _contracts.INVALID_ARGUMENT,
            "unknown document id; transcript chunks look like "
            "tr:<version>:<segment>:<start>-<end>")
    con = _contracts._connect(ctx)
    try:
        pin = _citations.source_pin(con, version)
        key = (pin or {}).get("source_key") or ""
        if not key.startswith("transcript:"):
            raise _contracts.AgentError(_contracts.NOT_FOUND, "unknown document")
        ctx.authorize_disclosure(con, key, ["metadata", "transcript"])
        episode_id = key[len("transcript:"):]
        seg = _citations.transcript_segment(con, version, seg_idx)
        if seg is None:
            raise _contracts.AgentError(_contracts.NOT_FOUND, "unknown document")
    finally:
        con.close()
    text = seg.get("text") or ""
    if char_end > len(text):
        raise _contracts.AgentError(_contracts.NOT_FOUND, "unknown document")
    quote = text[char_start:char_end]
    start_s, end_s = seg.get("start"), seg.get("end")
    url = canonical_transcript_url(ctx.workspace_id, version, seg_idx,
                                   char_start, char_end)
    body = (f"Transcript {episode_id} segment {seg_idx} "
            f"({start_s}-{end_s}s), characters {char_start}-{char_end} "
            f"of transcript version {version}: \"{quote}\"\n"
            "Coverage: one authorized pinned transcript chunk. Limitations: "
            "AUDIO_ALIGNMENT_UNKNOWN (transcript and audio not verified aligned).")
    if len(body.encode("utf-8")) > FETCH_TEXT_MAX_BYTES:
        raise ToolFailure(_contracts.INTERNAL,
                          "compatibility transcript document exceeds 8KiB")
    return {"id": f"tr:{version}:{seg_idx}:{char_start}-{char_end}",
            "title": f"Transcript {episode_id} segment {seg_idx}",
            "text": body, "url": url,
            "metadata": {"kind": _TRANSCRIPT_DOC_KIND,
                         "episode_id": episode_id,
                         "transcript_version_id": version,
                         "segment_index": seg_idx,
                         "char_range": [char_start, char_end],
                         "time_range_s": [start_s, end_s],
                         "source_pin": pin,
                         "limitations": ["AUDIO_ALIGNMENT_UNKNOWN"]}}


def compat_fetch(ctx, request: dict, *, budget=None) -> dict:
    """One bounded compatibility document (≤8KiB) for a search-issued id."""
    args = _contracts.parse_fetch(request)
    doc_id = args["id"]
    budget = _access.ExecutionBudget(deadline_s=10.0) if budget is None else budget
    if doc_id.startswith("ev:") and doc_id.count(":") == 1 and len(doc_id) > 3:
        return _fetch_evidence_doc(ctx, doc_id[3:], budget)
    if doc_id.startswith("tr:"):
        return _fetch_transcript_doc(ctx, doc_id[3:], budget)
    raise _contracts.AgentError(
        _contracts.INVALID_ARGUMENT,
        "unknown document id; metadata-only episodes, campaigns, watchlists, "
        "and private notes have no compatibility documents — use their "
        "native tools")
