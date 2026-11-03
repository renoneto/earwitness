"""Authorized application services: the single entrypoint for browser/API/MCP.

Every operation takes a server-constructed AccessContext (never request
JSON), enforces membership/share-grant authorization, validates strict
contracts, then delegates to the existing ops behavior where it exists.
P01-P13 stage logic is not implemented here: those entrypoints authorize,
validate, and raise NotImplementedError rather than silently succeeding.
"""
from __future__ import annotations

import functools
import inspect
import json
import math
import sqlite3

from . import access as _access
from . import ad_reads as _ad_reads
from . import citations as _citations
from . import collaboration as _collaboration
from . import contracts as _contracts
from . import db as _db
from . import ops as _ops
from . import oplog as _oplog
from . import search as _search

DEFAULT_LIMIT = _search.DEFAULT_LIMIT

# Server-built arguments of a hosted writer; every other argument comes
# from the member's request. `pins` (customer_freeze_report) are the draft
# pins the server showed, posted back and matched exactly against the
# current draft, so no member text reaches storage through them; in a
# workspace that holds a recording they name it.
_SERVER_ARGS = frozenset({"ctx", "con", "entry", "store", "data_dir", "repo",
                          "mailer", "now", "pins", "prepared"})


def _member_writer(fn):
    """A hosted writer: every argument the request supplies is checked by
    _refuse_recording_id before the writer runs, so no argument a writer
    persists is left out (SEC-R10-01). Every hosted writer carries this
    (tests.test_clip_audio_policy.test_every_hosted_writer_is_guarded).
    The call runs in a customer_query.writer_scope: its class is marked
    the writer only when it returns having changed rows (RC-R3-02)."""
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    def guarded(*args, **kwargs):
        from . import customer_query as _query
        with _query.writer_scope():
            try:
                bound = signature.bind(*args, **kwargs)
            except TypeError:
                return fn(*args, **kwargs)  # the writer reports its own bad call
            _refuse_recording_id(*(value for name, value in bound.arguments.items()
                                   if name not in _SERVER_ARGS))
            return fn(*args, **kwargs)
    guarded.member_text_guard = True
    return guarded


def _refuse_copied_ids(con, ids: set, table: str, where: str, params) -> None:
    """A frozen report or export just stored must not copy a recording id
    that sat in unscanned member text: checked before commit, so the
    caller's transaction rolls back (CLIPCORE-R8-N1, SEC-R10-03)."""
    from . import rights_admission as _rights
    for row in con.execute(f'SELECT * FROM "{table}" WHERE {where}', params):
        try:
            text = "\n".join(_rights.stored_text(v) for v in tuple(row))
        except UnicodeDecodeError:
            # A copy stored as bytes that are not UTF-8 fails closed
            # (CLIPCORE-SEC-R14-01).
            raise _ops.BadInput(_rights.RECORDING_COPY_REFUSAL) from None
        if any(i in text for i in ids):
            raise _ops.BadInput(_rights.RECORDING_COPY_REFUSAL)


def _check(ctx: _access.AccessContext, operation: str, scope: str | None = None) -> None:
    ctx.require_operation(operation)
    if scope:
        ctx.require_scope(scope)


def _check_share_or_scope(ctx: _access.AccessContext, con: sqlite3.Connection,
                          operation: str, scope: str,
                          object_type: str, object_id: str) -> dict | None:
    """Grant enforcement for share guests; scope gate for members.

    Guests arrive with a scopeless share ctx, so the scope gate cannot
    authorize them. Allowlisted share reads instead enforce the grant
    (operation allowlist, expiry/revocation, exact object match) via
    check_share_access. Membership paths are unchanged.

    Returns the grant's field allowlist info for share guests (the
    check_share_access result) so callers can project payloads to the
    granted fields; None for members (full payload).
    """
    if ctx.authority_kind == "share":
        return _collaboration.check_share_access(con, ctx, operation,
                                                 object_type, object_id)
    _check(ctx, operation, scope)
    return None


# Identity keys a share guest always receives; every other payload key
# must name a granted field. Citation contents are filtered the same way
# (citation identity keys always kept).
_SHARE_IDENTITY_KEYS = frozenset({"id", "episode_id"})
_SHARE_CITATION_IDENTITY_KEYS = frozenset({"evidence_id", "episode_id"})


def _granted_field_set(grant: dict) -> set:
    """Normalize a grant's field allowlist from either grant shape.

    check_share_access returns parsed ``fields`` (a list); share_state
    carries the raw stored JSON string. Unknown shapes grant nothing.
    """
    fields = grant.get("fields")
    if isinstance(fields, str):
        try:
            fields = json.loads(fields or "[]")
        except (ValueError, TypeError):
            fields = []
    if isinstance(fields, (list, tuple, set)):
        return {f for f in fields if isinstance(f, str)}
    return set()


def _filter_guest_citation(citation: dict, allowed: set) -> dict:
    """Project a nested citation to granted fields + citation identity."""
    keep = set(allowed) | set(_SHARE_CITATION_IDENTITY_KEYS)
    return {k: v for k, v in citation.items() if k in keep}


def _project_search_item_for_guest(item: dict, allowed: set) -> dict:
    """Project one search item to granted fields + identity keys.

    Drops ungranted review judgment/bounds, brand associations, and other
    payload keys; the nested citation is filtered rather than dropped so
    the guest keeps verifiable identity pins for what it may read.
    """
    keep = set(allowed) | set(_SHARE_IDENTITY_KEYS)
    out = {k: v for k, v in item.items() if k in keep or k == "citation"}
    citation = item.get("citation")
    if isinstance(citation, dict):
        out["citation"] = _filter_guest_citation(citation, allowed)
    return out


def _project_evidence_detail_for_guest(detail: dict, allowed: set,
                                       allowed_ids: set) -> dict:
    """Project an evidence payload to granted fields + identity keys.

    ``allowed_ids`` holds the evidence ids covered by the grant: overlap
    entries pointing at out-of-grant evidence are dropped even when the
    overlaps key itself is granted, so neighbors never leak ids/bounds.
    """
    keep = set(allowed) | set(_SHARE_IDENTITY_KEYS)
    out = {k: v for k, v in detail.items() if k in keep or k == "citation"}
    citation = detail.get("citation")
    if isinstance(citation, dict):
        out["citation"] = _filter_guest_citation(citation, allowed)
    if "overlaps" in out:
        out["overlaps"] = [o for o in out["overlaps"]
                           if o.get("id") in allowed_ids]
    return out


def _coerce_offset(offset) -> int:
    """Validate a transcript page offset; clamp negatives to zero.

    Non-integer, boolean, and non-finite offsets are caller errors (400);
    integral floats are accepted. Clamping to the segment total happens
    against the loaded segments in transcript_window.
    """
    if isinstance(offset, bool):
        raise _contracts.ContractError("offset must be a non-negative integer",
                                       _contracts.HTTP_BAD_REQUEST)
    if isinstance(offset, float):
        if not math.isfinite(offset) or offset != int(offset):
            raise _contracts.ContractError(
                "offset must be a non-negative integer",
                _contracts.HTTP_BAD_REQUEST)
        offset = int(offset)
    if not isinstance(offset, int):
        raise _contracts.ContractError("offset must be a non-negative integer",
                                       _contracts.HTTP_BAD_REQUEST)
    return max(0, offset)


def _guest_search_scope(ctx: _access.AccessContext, con: sqlite3.Connection,
                        filt: dict) -> tuple:
    """Pin a share guest's search filter to its granted object.

    Returns (pinned_filter, grant). Guests see only grant-covered objects:
    an episode grant pins episode_id (a conflicting caller value is
    traversal and denied); an evidence grant pins its episode for the
    query and the page is narrowed to the granted id afterwards. Grants
    covering non-evidence objects see nothing here and are denied.
    """
    ctx.require_operation("search_evidence")
    grant = _collaboration.share_state(con, ctx.share_grant_id)
    if grant["state"] != "active":
        raise _access.AuthorizationError(f"share is {grant['state']}", 403)
    filt = _search.validate_filter(filt)
    object_type, object_id = grant["object_type"], grant["object_id"]
    if object_type == "episode":
        if filt.get("episode_id") and filt["episode_id"] != object_id:
            raise _access.AuthorizationError(
                "share grant does not cover this object", 403)
        return ({**filt, "episode_id": object_id}, grant)
    if object_type == "evidence":
        row = con.execute("SELECT episode_id FROM evidence WHERE id=?",
                          (object_id,)).fetchone()
        if row is None:
            raise _access.AuthorizationError(
                "share grant does not cover this object", 403)
        if filt.get("episode_id") and filt["episode_id"] != row["episode_id"]:
            raise _access.AuthorizationError(
                "share grant does not cover this object", 403)
        return ({**filt, "episode_id": row["episode_id"]}, grant)
    raise _access.AuthorizationError(
        "share grant does not cover evidence search", 403)


def _guest_narrow_page(grant: dict | None, page: dict) -> dict:
    """Restrict a guest search page to its grant (object + fields).

    Episode grants need no id narrowing: the pinned filter already scopes
    the query. An evidence grant covers at most one id, so the total is
    exact (1 when seen; 0 when the single page is exhausted, else a lower
    bound while later pages stay unexamined). Every guest item is also
    projected to the grant's field allowlist (identity keys always kept),
    so ungranted judgment/associations/bounds never ride the page.
    Member pages (grant None) pass through untouched.
    """
    if grant is None:
        return page
    allowed = _granted_field_set(grant)
    if grant["object_type"] == "episode":
        page = dict(page)
        page["items"] = [_project_search_item_for_guest(it, allowed)
                         for it in page["items"]]
        page["returned_count"] = len(page["items"])
        return page
    if grant["object_type"] != "evidence":
        return page
    object_id = grant["object_id"]
    page = dict(page)
    page["items"] = [_project_search_item_for_guest(it, allowed)
                     for it in page["items"] if it.get("id") == object_id]
    page["returned_count"] = len(page["items"])
    page["citations"] = [c for c in page.get("citations", [])
                         if c.get("evidence_id") == object_id]
    if page["items"]:
        page["total_matches"] = 1
        page["total_relation"] = "exact"
    elif page.get("next_cursor") is None and not page.get("truncated"):
        page["total_matches"] = 0
        page["total_relation"] = "exact"
    else:
        page["total_matches"] = 0
        page["total_relation"] = "lower_bound"
    return page


def _require_key(idempotency_key: str | None) -> str:
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise _contracts.ContractError("creation requires an idempotency key",
                                       _contracts.HTTP_BAD_REQUEST)
    return idempotency_key


def view_revision(con: sqlite3.Connection, scope: str, *, notes: bool = False,
                  history: str = "active") -> str:
    """Opaque projection-aware revision token for a read scope.

    Single shared derivation for every transport (brief-D carryover): the
    hash covers only caller-visible projection values, so hidden-only
    changes (private notes, watchlist saves, audits) leave the token
    unchanged. Format "vr2-<12hex>" from search.projection_revision.
    """
    return _search.projection_revision(con, {"scope": scope, "notes": notes,
                                             "history": history})


def envelope(ctx: _access.AccessContext, con: sqlite3.Connection, scope: str,
             applied_filter: dict, items: list, total: int,
             relation: str = "exact", cursor: str | None = None,
             coverage: dict | None = None, limitations: tuple = (),
             citations: list | None = None) -> dict:
    return {
        "schema_version": 1,
        "request_id": ctx.request_id,
        "workspace_id": ctx.workspace_id,
        "view_revision": _citations._state_digest("view", ctx.policy_identity,
            [scope, applied_filter, items, total, coverage, citations]),
        "as_of": _db.utcnow(),
        "applied_filter": applied_filter,
        "items": items,
        "returned_count": len(items),
        "total_matches": total,
        "total_relation": relation,
        "next_cursor": cursor,
        "truncated": cursor is not None,
        "truncation_reason": "PAGE_LIMIT" if cursor is not None else None,
        "coverage": coverage or {},
        "limitations": list(limitations),
        "citations": list(citations) if citations is not None else [],
    }


# -- Reader operations (backed by existing ops behavior) ---------------------


def _show_scoped_search(ctx: _access.AccessContext, con: sqlite3.Connection,
                       filt: dict, limit: int, cursor: str | None,
                       grant: dict | None, show_episodes: list) -> dict:
    """Evidence search restricted to a resolved stable show identity.

    show_id has no legacy equivalent, so the legacy converter cannot
    execute it: resolve via search.resolve_show_id first (unknown ids
    reject, keeping strict validation), project the remainder through
    the legacy converter, then restrict the full result set to the
    resolved episodes before paging. Cursor digests bind the show_id.
    """
    from . import indexing as _indexing
    legacy = _search.to_legacy_filter({**filt, "show_id": ""})
    digest = _indexing.filter_digest({**legacy, "per_page": limit,
                                      "show_id": filt["show_id"]})
    data_rev = None
    try:
        data_rev = _indexing.check_fresh(con)
    except _indexing.StaleProjection as exc:
        if cursor is not None:
            raise _contracts.ContractError(str(exc), _contracts.HTTP_CONFLICT)
    except _indexing.IndexUnavailable as exc:
        if cursor is not None:
            raise _contracts.ContractError(str(exc), _contracts.HTTP_BAD_REQUEST)
    offset = 0
    if cursor is not None:
        try:
            offset = _indexing.decode_cursor(ctx.workspace_id, cursor, digest,
                                             data_rev)
        except _indexing.StaleProjection as exc:
            raise _contracts.ContractError(str(exc), _contracts.HTTP_CONFLICT)
    wanted = set(show_episodes)
    scoped: list = []
    collected = 0
    page_num = 1
    brand_hidden = _ad_reads.brand_review_ids(con)
    while True:
        result = _ops.search_evidence(con, {**legacy, "per_page": _ops.PER_PAGE_MAX,
                                            "page": page_num}, brand_hidden)
        scoped.extend(it for it in result["items"]
                      if it.get("episode_id") in wanted)
        collected += len(result["items"])
        if collected >= result["total"] or not result["items"]:
            break
        page_num += 1
    items = scoped[offset:offset + limit]
    next_cursor = None
    if len(scoped) > offset + len(items) and data_rev is not None:
        next_cursor = _indexing.encode_cursor(
            ctx.workspace_id, digest, offset + len(items), data_rev)
    for item in items:
        item["citation"] = {"evidence_id": item["id"], "episode_id": item["episode_id"],
                            "source": item.get("source")}
    return _guest_narrow_page(
        grant, envelope(ctx, con, "research", filt, items, len(scoped),
                        cursor=next_cursor,
                        coverage={"scope": "authorized evidence"},
                        limitations=["CANDIDATES_NOT_UNIQUE_ADS", "NO_MATCH_IS_NOT_ABSENCE",
                                     "AUDIO_ALIGNMENT_UNKNOWN"]))


def _excerpt_ceiling(ctx, con, row, source_key, excerpt_allowed):
    """The excerpt this caller may read for one evidence row (design 5).

    Returns (text, excerpt_allowed, transcript_allowed). text None keeps
    today's rule (the stored excerpt). At excerpt level (the transcript
    source denies the transcript by its own policy row) text is the safe
    excerpt, served only when excerpt and metadata are allowed on both
    sources; otherwise it is "" and the excerpt is refused. A row with no
    resolvable transcript pin whose policy denies the transcript has no
    safe excerpt: text is "". The stored excerpt is never returned there."""
    transcript = _citations._version_provenance(con, row.get("transcript_version_id"))
    transcript_allowed = bool(transcript) and ctx.disclosure_allowed(
        con, transcript["source_key"], "transcript")
    if transcript and not transcript_allowed and \
            _citations.transcript_policy_denied(con, transcript["source_key"]):
        both = excerpt_allowed and ctx.disclosure_allowed(
            con, transcript["source_key"], "metadata") and ctx.disclosure_allowed(
            con, transcript["source_key"], "excerpt")
        safe = _citations.current_safe_excerpt(con, row["id"]) if both else None
        return (safe or {}).get("text") or "", both, transcript_allowed
    if not transcript and _citations.untranscribed_policy_denied(
            con, source_key, row.get("episode_id")):
        return "", excerpt_allowed, transcript_allowed
    return None, excerpt_allowed, transcript_allowed


def _share_search(ctx, con, filt, grant, limit, cursor):
    import base64
    fields = _granted_field_set(grant)
    if not fields:
        raise _access.AuthorizationError("share grants no content fields", 403)
    dependencies = {
        "sources": {"source"}, "review_states": {"judgment", "review"},
        "brand": {"assocs", "attribution"}, "brand_basis": {"assocs", "attribution"},
        "brand_channel": {"assocs", "attribution"}, "show_id": {"show_title"},
        "show_query": {"show_title"}, "category": {"category"},
        "published_from": {"pub_date"}, "published_to": {"pub_date"},
        "source_version_id": {"source_version_id"},
    }
    for predicate, required in dependencies.items():
        if filt.get(predicate) and not required & fields:
            raise _access.AuthorizationError("share does not grant filter fields", 403)
    if not filt["include_unknown_dates"] and "pub_date" not in fields:
        raise _access.AuthorizationError("share does not grant date filtering", 403)
    if filt["history"] != "active" and "source_version_id" not in fields:
        raise _access.AuthorizationError("share does not grant history filtering", 403)
    order_fields = {"start": {"orig_start_s", "bounds"},
                    "brand": {"assocs", "attribution"}, "published": {"pub_date"}}
    if filt["sort"] in order_fields and not order_fields[filt["sort"]] & fields:
        raise _access.AuthorizationError("share does not grant ordering fields", 403)
    legacy = _search.to_legacy_filter(filt)
    legacy["q"] = ""
    permitted = []
    page = 1
    brand_hidden = _ad_reads.brand_review_ids(con)
    while True:
        result = _ops.search_evidence(con, {**legacy, "page": page, "per_page": _ops.PER_PAGE_MAX},
                                      brand_hidden)
        for raw in result["items"]:
            if grant["object_type"] == "evidence" and raw["id"] != grant["object_id"]:
                continue
            key = _citations.evidence_source_provenance(con, raw)
            if key:
                categories = [category for category in ("metadata", "excerpt", "notes") if ctx.permits(category)]
                ctx.authorize_disclosure(con, key["source_key"], categories)
                # Plan A: the same safe excerpt shared detail serves, before
                # projection and matching; never the stored excerpt.
                text, allowed, _ = _excerpt_ceiling(ctx, con, raw, key["source_key"],
                                                    "excerpt" in categories)
                if text is not None:
                    raw = {k: v for k, v in raw.items() if k != "excerpt"}
                    if allowed:
                        raw["excerpt"] = text
            item = _project_search_item_for_guest(raw, fields)
            if "interval_text" in fields:
                transcript = _citations._version_provenance(con, raw.get("transcript_version_id"))
                if transcript is None:
                    continue
                ctx.authorize_disclosure(con, transcript["source_key"], ["transcript"])
                item["interval_text"] = _ops.evidence_text(con, raw)
            if _search.permitted_evidence_match(item, filt.get("query", ""),
                    "interval_text" in fields, "notes" in fields,
                    item.get("interval_text", ""), item.get("notes", "")):
                permitted.append(item)
        if not result["items"] or page * _ops.PER_PAGE_MAX >= result["total"]:
            break
        page += 1
    if filt["sort"] == "episode" and not fields & {"orig_start_s", "bounds"}:
        permitted.sort(key=lambda item: (item.get("episode_id", ""), item["id"]))
    revision = _citations._state_digest("share", ctx.share_grant_id, permitted)
    offset = 0
    if cursor:
        try:
            token = json.loads(base64.urlsafe_b64decode(cursor).decode())
        except (TypeError, ValueError):
            raise _contracts.ContractError("malformed share cursor", 400)
        if token.get("revision") != revision or token.get("filter") != filt:
            raise _contracts.ContractError("share result changed", 409)
        offset = token.get("offset")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise _contracts.ContractError("malformed share cursor", 400)
    chunk = permitted[offset:offset + limit]
    more = offset + limit < len(permitted)
    next_cursor = base64.urlsafe_b64encode(json.dumps(
        {"revision": revision, "filter": filt, "offset": offset + limit}).encode()).decode() if more else None
    result = envelope(ctx, con, "share", filt, chunk, len(permitted), cursor=next_cursor)
    result["view_revision"] = revision
    return result
def search_evidence(ctx: _access.AccessContext, con: sqlite3.Connection,
                    filt: dict, limit: int = DEFAULT_LIMIT,
                    cursor: str | None = None) -> dict:
    _grant = None
    if ctx.authority_kind == "share":
        filt, _grant = _guest_search_scope(ctx, con, filt)
        return _share_search(ctx, con, filt, _grant, limit, cursor)
    else:
        _check(ctx, "search_evidence", "research:read")
    filt = _search.validate_filter(filt)
    limit = _search.check_limit(limit)
    if filt.get("show_id"):
        show_episodes = _search.resolve_show_id(con, filt["show_id"])
        return _show_scoped_search(ctx, con, filt, limit, cursor, _grant,
                                   show_episodes)
    from . import indexing as _indexing
    legacy = _search.to_legacy_filter(filt)
    brand_hidden = _ad_reads.brand_review_ids(con)
    offset = 0
    try:
        data_rev = _indexing.check_fresh(con)
    except _indexing.StaleProjection as exc:
        if cursor is not None:
            raise _contracts.ContractError(str(exc), _contracts.HTTP_CONFLICT)
        result = _ops.search_evidence(con, {**legacy, "per_page": limit, "page": 1},
                                      brand_hidden)
        items = result["items"][:limit]
        next_cursor = None
        for item in items:
            item["citation"] = {"evidence_id": item["id"],
                                "episode_id": item["episode_id"],
                                "source": item.get("source")}
        return _guest_narrow_page(
            _grant, envelope(ctx, con, "research", filt, items, result["total"],
                             cursor=next_cursor,
                             coverage={"scope": "authorized evidence"},
                             limitations=["CANDIDATES_NOT_UNIQUE_ADS",
                                          "NO_MATCH_IS_NOT_ABSENCE",
                                          "AUDIO_ALIGNMENT_UNKNOWN"]))
    except _indexing.IndexUnavailable as exc:
        if cursor is not None:
            raise _contracts.ContractError(str(exc), _contracts.HTTP_BAD_REQUEST)
        result = _ops.search_evidence(con, {**legacy, "per_page": limit, "page": 1},
                                      brand_hidden)
        items = result["items"][:limit]
        next_cursor = None
        for item in items:
            item["citation"] = {"evidence_id": item["id"],
                                "episode_id": item["episode_id"],
                                "source": item.get("source")}
        return _guest_narrow_page(
            _grant, envelope(ctx, con, "research", filt, items, result["total"],
                             cursor=next_cursor,
                             coverage={"scope": "authorized evidence"},
                             limitations=["CANDIDATES_NOT_UNIQUE_ADS",
                                          "NO_MATCH_IS_NOT_ABSENCE",
                                          "AUDIO_ALIGNMENT_UNKNOWN"]))
    digest = _indexing.filter_digest({**legacy, "per_page": limit})
    if cursor is not None:
        try:
            offset = _indexing.decode_cursor(ctx.workspace_id, cursor, digest,
                                             data_rev)
        except _indexing.StaleProjection as exc:
            raise _contracts.ContractError(str(exc), _contracts.HTTP_CONFLICT)
    try:
        matched = _indexing.drop_brand_only_matches(
            con, _indexing.scoped_match(con, legacy.get("q", ""), _fresh_rev=data_rev),
            legacy.get("q", ""), brand_hidden)
        page = offset // limit + 1 if limit else 1
        result = _indexing.paged_search(con, {**legacy, "per_page": limit,
                                              "page": page}, matched, brand_hidden)
        items = result["items"][:limit]
        next_cursor = None
        if result["total"] > offset + len(items):
            next_cursor = _indexing.encode_cursor(
                ctx.workspace_id, digest, offset + len(items), data_rev)
    except (_indexing.IndexUnavailable, _indexing.StaleProjection):
        if cursor is not None:
            raise _contracts.ContractError("result set changed; refetch without cursor",
                                           _contracts.HTTP_CONFLICT)
        result = _ops.search_evidence(con, {**legacy, "per_page": limit, "page": 1},
                                      brand_hidden)
        items = result["items"][:limit]
        next_cursor = None
    for item in items:
        item["citation"] = {"evidence_id": item["id"], "episode_id": item["episode_id"],
                            "source": item.get("source")}
    return _guest_narrow_page(
        _grant, envelope(ctx, con, "research", filt, items, result["total"],
                         cursor=next_cursor,
                         coverage={"scope": "authorized evidence"},
                         limitations=["CANDIDATES_NOT_UNIQUE_ADS", "NO_MATCH_IS_NOT_ABSENCE",
                                      "AUDIO_ALIGNMENT_UNKNOWN"]))


def evidence_detail(ctx: _access.AccessContext, con: sqlite3.Connection,
                    evidence_id: str) -> dict:
    grant = _check_share_or_scope(ctx, con, "evidence_detail", "research:read",
                                  "evidence", evidence_id)
    if not isinstance(evidence_id, str) or not evidence_id:
        raise _contracts.ContractError("evidence_id must be a non-empty id",
                                       _contracts.HTTP_BAD_REQUEST)
    detail = _ops.evidence_detail(con, evidence_id)
    if detail is None:
        raise KeyError(evidence_id)
    # Customers (members' agents, share guests) never see a guessed brand.
    _ops.hide_brand_guess(detail, _ad_reads.brand_review_ids(con, [evidence_id]))
    detail["citation"] = _citations.evidence_citation(con, evidence_id)
    source = _citations.evidence_source_provenance(con, detail)
    if source is None:
        raise _access.AuthorizationError("source unavailable", 404)
    ctx.authorize_disclosure(con, source["source_key"], ["metadata"])
    excerpt_allowed = ctx.disclosure_allowed(con, source["source_key"], "excerpt")
    notes_allowed = ctx.disclosure_allowed(con, source["source_key"], "notes")
    text, excerpt_allowed, transcript_allowed = _excerpt_ceiling(
        ctx, con, detail, source["source_key"], excerpt_allowed)
    if text is not None:
        detail["excerpt"] = text
    detail["_excerpt_allowed"] = excerpt_allowed
    detail["_notes_allowed"] = notes_allowed
    detail["_transcript_allowed"] = transcript_allowed
    if not excerpt_allowed:
        detail.pop("excerpt", None)
    if not notes_allowed and detail.get("review"):
        detail["review"] = {key: value for key, value in detail["review"].items() if key not in ("notes", "revision")}
    if not (excerpt_allowed and notes_allowed and transcript_allowed):
        detail.pop("citation", None)
    admitted_overlaps = []
    for overlap in detail.get("overlaps", []):
        neighbor = con.execute("SELECT source_version_id, transcript_version_id FROM evidence"
                               " WHERE id=?", (overlap["id"],)).fetchone()
        provenance = _citations.evidence_source_provenance(con, neighbor) if neighbor else None
        if provenance and ctx.disclosure_allowed(con, provenance["source_key"], "metadata"):
            admitted_overlaps.append(overlap)
    detail["overlaps"] = admitted_overlaps
    if grant is not None:
        # The grant check pins the object; the field allowlist pins the
        # payload. Only the granted evidence id is in-grant here, so
        # neighboring overlaps are always out-of-grant and dropped.
        detail = _project_evidence_detail_for_guest(
            detail, _granted_field_set(grant), {evidence_id})
    return detail


def transcript_window(ctx: _access.AccessContext, con: sqlite3.Connection,
                      episode_id: str, version_id=None, offset: int = 0,
                      *, start_s=None, end_s=None) -> dict:
    grant = _check_share_or_scope(ctx, con, "transcript_window", "transcripts:read",
                                  "episode", episode_id)
    if grant is not None and not _granted_field_set(grant) & {"segments", "transcript", "text", "interval_text"}:
        raise _access.AuthorizationError("share does not grant transcript content", 403)
    offset = _coerce_offset(offset)
    segments = _ops.transcript_segments(con, episode_id, version_id or "current")
    citation = _citations.transcript_citation(con, episode_id, version_id)
    if grant is not None:
        citation = _filter_guest_citation(citation, _granted_field_set(grant))
    version = version_id
    if version is None:
        version = con.execute("SELECT transcript_version_id FROM episodes WHERE id=?", (episode_id,)).fetchone()[0]
    source = _citations._version_provenance(con, version)
    if source is not None:
        ctx.authorize_disclosure(con, source["source_key"], ["transcript"])
    if start_s is not None:
        segments = [s for s in segments if s.get("start", float("inf")) < end_s
                    and s.get("end", float("-inf")) > start_s]
    total = len(segments)
    offset = min(offset, total)
    window = segments[offset:offset + 100]
    truncated = total > offset + len(window)
    return {"segments": window, "total_segments": total,
            "truncated": truncated,
            "next_offset": offset + len(window) if truncated else None,
            "citation": citation,
            "view_revision": _citations._state_digest("transcript", episode_id, [segments, citation])}


def research_overview(ctx: _access.AccessContext, con: sqlite3.Connection) -> dict:
    _check(ctx, "research_overview", "research:read")
    out = _ops.overview(con)
    out["view_revision"] = view_revision(con, "research")
    return out


# -- P01-P13 stage entrypoints: authorized + validated, behavior in leaves --


def _stage(operation: str):
    def run(ctx: _access.AccessContext, *args, **kwargs):
        ctx.require_member(operation)
        raise NotImplementedError(f"{operation} stage logic lands in its P-leaf; "
                                  "contract frozen in contracts.py")
    run.__name__ = operation
    return run


submit_processing = _stage("submit_processing")
get_processing_run = _stage("get_processing_run")
cancel_processing = _stage("cancel_processing")
retry_processing = _stage("retry_processing")
resume_processing = _stage("resume_processing")
reconcile_paid_operation = _stage("reconcile_paid_operation")
save_observation = _stage("save_observation")
match_creatives = _stage("match_creatives")
summarize_research = _stage("summarize_research")
save_schedule = _stage("save_schedule")
save_assignment = _stage("save_assignment")
save_comment = _stage("save_comment")
create_share = _stage("create_share")
revoke_share = _stage("revoke_share")
save_expected_placement = _stage("save_expected_placement")
evaluate_placements = _stage("evaluate_placements")
search_transcripts = _stage("stage_search_transcripts")


# -- Hosted customer product (Task 10): authorized customer services --------
#
# Two authority checks on every call: (1) registry routing from the
# authorized external workspace to its exact verified database (unknown
# keys never create files or expose paths); (2) the current principal's
# membership or exact share permission inside that database, applied to
# the database's bound row scope. The loopback owner adapter stays a
# trusted explicit mode; hosted callers always pass through here.


def open_customer_workspace(store, workspace_id: str, data_dir: str,
                            ctx: _access.AccessContext, *, readonly=False, control=None):
    """Resolve + verify + authorize a hosted customer workspace.

    Owner databases additionally require the explicitly bound
    (issuer, subject) account; until bound, hosted access is denied
    while local owner mode keeps working. Returns (con, entry).
    The caller closes con. Unknown workspaces raise NOT_FOUND without
    disclosing paths; denied ones raise FORBIDDEN.
    """
    from . import connections as _conn
    if ctx.connection_id:
        # L14: a bearer grant's member must be admitted before any handler
        # runs, so a handler's own errors (404, 409, 400, size) cannot act
        # as an oracle for a member who is not admitted. Same refusal as
        # a revoked grant. `_current_authority` checks again at commit.
        from . import admission as _admission
        if not _admission.is_principal_admitted(ctx.principal_id or ""):
            raise _conn.GrantError("FORBIDDEN", "grant revoked", 403)
    entry = _conn.resolve_workspace(store, workspace_id, control=control)
    con, entry = _conn.open_registered_db(store, workspace_id, data_dir, readonly=readonly, control=control)
    try:
        principal = ctx.principal_id or ""
        if entry["is_owner"]:
            bound = (entry.get("bound_issuer") or "", entry.get("bound_subject") or "")
            if not bound[0] or not bound[1]:
                raise _access.AccessLost(
                    "owner workspace has no bound hosted account; use local owner mode")
            if principal != f"oidc:{bound[0]}:{bound[1]}" or (
                    ctx.session_binding is not None and ctx.session_binding != bound):
                raise _access.AccessLost(
                    "account is not bound to this workspace")
        else:
            if ctx.authority_kind == "share":
                pass
            elif not _collaboration.is_member(con, entry["row_namespace"], principal):
                raise _access.AccessLost(
                    "workspace membership required")
    except Exception:
        con.close()
        raise
    return con, entry


def _customer_actor(ctx: _access.AccessContext) -> str:
    return ctx.principal_id or "hosted"


_R, _T, _W, _C, _N = ("research:read", "transcripts:read", "watchlists:read",
                      "campaigns:read", "notes:read")
# Personal agent tokens on customer reads (AGT-SEC-R4-01). Deny by
# default: every operation passed to _require_member declares the scopes
# a personal token needs for it, all of them required; None, or a missing
# entry, refuses the token. Browser sessions are unaffected, and so are
# the OAuth reader grants of the existing connection flow.
READER_BEARER_SCOPES = {
    "customer_search": (_R,),
    "customer_record": (_R,),
    "customer_record_window": (_R, _T),
    "customer_summary": (_R,),
    "customer_brands": (_R,),
    "customer_brand_detail": (_R,),
    "customer_shows": (_R,),
    "customer_show_detail": (_R,),
    "customer_show_artwork": (_R,),
    "customer_episode_reads": (_R,),
    "customer_compare": (_R,),
    "customer_filter_values": (_R,),
    "customer_creative_detail": (_R,),
    "customer_research_query": (_R,),
    "customer_saved_items": (_R, _W),
    "customer_saved_item": (_R, _W),
    "customer_alerts": (_R, _W),
    "customer_alert": (_R, _W),
    "customer_overview": (_R, _W, _C),
    "customer_mistake_read": (_R, _N),
    "customer_note_collection": (_R, _N),
    "customer_campaigns": (_C,),
    "customer_campaign_detail": (_C,),
    "customer_campaign_draft": (_C,),
    "customer_campaign_targets": (_C,),
    "customer_briefs": (_C,),
    "customer_evidence_fit": (_C, _R),
    "customer_reports": (_C, _R),
    "customer_report": (_C, _R),
    "customer_report_html": (_C, _R),
    # No scope reaches these: agents never get audio, member settings,
    # export files or the reader profile.
    "customer_recording": None,
    "customer_preferences": None,
    "customer_reader_status": None,
    "customer_archive": None,
    "customer_export_download": None,
}

# Set by the customer route for a reader bearer: the operations whose
# scope gate passed during the request. A route that answers without one
# is refused (asgi._customer_route).
import contextvars as _contextvars
READER_GATE = _contextvars.ContextVar("reader_gate", default=None)


def is_personal_token(ctx) -> bool:
    from .connections import PERSONAL_CLIENT_PREFIX
    return ctx.credential_kind == "reader_bearer" and \
        (ctx.client_id or "").startswith(PERSONAL_CLIENT_PREFIX)


def _require_member(ctx: _access.AccessContext, con, entry: dict,
                    operation: str) -> None:
    """Membership check, the agent-token scope gate, then the entitlement
    gate (so a caller without access gets 403, never a billing reason).
    A model_session (Ask) never passes: it reads only through the Ask
    dispatcher's agent tools, never through customer reads."""
    if ctx.credential_kind == "model_session":
        raise _access.AuthorizationError("Ask's model credential cannot read this", 403)
    ctx.require_member(operation)
    if is_personal_token(ctx):
        needed = READER_BEARER_SCOPES.get(operation)
        if needed is None:
            raise _access.AuthorizationError("agent tokens cannot read this", 403)
        for scope in needed:
            ctx.require_scope(scope)
        passed = READER_GATE.get()
        if passed is not None:
            passed.append(operation)
    _require_entitlement(con, entry, operation)


def _require_customer_role(con, entry: dict, ctx: _access.AccessContext,
                           action: str, operation: str) -> None:
    """Membership role gate for hosted customer mutations. Owner-bound
    principals on the owner database pass; other workspaces require the
    collaboration role. Guests never pass a mutation gate. Reader
    credentials are read-only even when their principal is owner/editor
    (K02): credential provenance is checked before any role logic."""
    _access.require_customer_mutation(ctx, operation)
    if ctx.authority_kind == "share":
        raise _access.AuthorizationError(
            f"share grants do not allow operation: {operation}", 403)
    if entry["is_owner"]:
        ctx.require_member(operation)
    elif not _collaboration.check_permission(con, entry["row_namespace"],
                                             ctx.principal_id or "", action):
        # Shown to the member as is: product language, no operation name.
        what = "export" if operation == "customer_research_export" else "make this change"
        raise _access.AuthorizationError(
            f"Your role in this workspace can't {what}. Ask an owner for editor access.", 403)
    _require_entitlement(con, entry, operation)
    from . import customer_query as _query
    # READ-CACHE: the writer's class refills first, once the write changed
    # rows (customer_query.writer_scope, RC-R3-02).
    _query.note_writer(con, ctx)


# FR-11 (UX audit B2): a saved item, alert or campaign changes only by the
# member who made it, or by a workspace owner. The maker is the actor of
# the item's creation audit row (the seed writes through these services
# too); the tables keep no creator column.
CREATOR_ONLY_REFUSAL = "Only the person who made this can change it."
_MADE_BY = {  # kind: (table, audit target_type, creation audit action)
    "saved_item": ("customer_saved_items", "customer_saved_item", "customer_item_save"),
    "alert": ("customer_alerts", "customer_alert", "customer_alert_save"),
    "campaign": ("campaigns", "campaign", "campaign_create"),
    "brief": ("provided_briefs", "provided_brief", "brief_create"),
}
_ID_COLUMN = {"brief": "brief_id"}  # every other table keys its rows by id
# An alert edit writes the creation action too; its detail names what
# changed (customer.save_alert), a creation's never does (CO-SEC-R1-02).
_NOT_AN_EDIT = {"alert": " AND json_extract(detail, '$.membership_changed') IS NULL"}
# What a member who did not make a campaign keeps: reading, its reports,
# and Freeze (today's rule: a frozen report is final and adds, not changes).
_OTHERS_CAMPAIGN_ACTIONS = ("read", "stored_reports", "freeze", "reports")


def _makers(con, kind: str, ids) -> dict:
    """{id: actor} of each item's first creation audit row."""
    _table, target_type, action = _MADE_BY[kind]
    ids = sorted({str(i) for i in ids})
    out = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for row in con.execute(
                "SELECT target_id, actor FROM audit WHERE target_type=? AND action=?"
                f" AND target_id IN ({','.join('?' * len(chunk))}){_NOT_AN_EDIT.get(kind, '')}"
                " ORDER BY id DESC",
                (target_type, action, *chunk)):
            out[row[0]] = row[1]  # descending: the first creation row wins
    return out


def _is_workspace_owner(con, entry: dict, ctx) -> bool:
    return bool(entry["is_owner"]) or _collaboration.get_role(
        con, entry["row_namespace"], ctx.principal_id or "") == "owner"


def _change_rule(con, entry: dict, ctx, kind: str, ids):
    """may(id) -> bool for this caller, the one rule the guard and the
    screens share. A local operator keeps rows no member made (the
    personal-note rule in customer_saved). A member whose current role
    cannot change items (a viewer, a demoted maker) may change none."""
    if _is_workspace_owner(con, entry, ctx):
        return lambda _id: True
    if not entry["is_owner"] and not _collaboration.check_permission(
            con, entry["row_namespace"], ctx.principal_id or "", "organize"):
        return lambda _id: False
    makers = _makers(con, kind, ids)
    me = ctx.principal_id

    def may(object_id) -> bool:
        maker = makers.get(str(object_id))
        return (maker is not None and maker == me) or (
            maker is None and ctx.credential_kind == "local_operator")
    return may


def _require_maker(con, entry: dict, ctx, kind: str, object_id) -> None:
    """Refuse a change to an item the caller did not make (403, before any
    write). An id that names no item passes: the writer reports it as
    today."""
    table = _MADE_BY[kind][0]
    if object_id is None or con.execute(
            f'SELECT 1 FROM "{table}" WHERE {_ID_COLUMN.get(kind, "id")}=?',
            (object_id,)).fetchone() is None:
        return
    if not _change_rule(con, entry, ctx, kind, [object_id])(object_id):
        raise _access.AuthorizationError(CREATOR_ONLY_REFUSAL, 403)


def _mark_changeable(con, entry: dict, ctx, kind: str, items) -> list:
    """Copies of items, each telling the screens whether this caller may
    change it (can_change). Never marks the given dicts: a read may hand
    out shared, remembered results."""
    ids = [i["id"] for i in items if isinstance(i, dict) and i.get("id") is not None]
    may = _change_rule(con, entry, ctx, kind, ids)
    makers = _makers(con, kind, ids) if kind == "saved_item" else None

    def marks(object_id) -> dict:
        out = {"can_change": may(object_id)}
        if makers is not None:
            # RB-R1-01: added_by_me, true only for the member who saved it
            # (an owner may change every item, but did not add each one). A
            # yes/no for this caller: never the maker's name, id or a count.
            # A row no member made is the local operator's (_change_rule).
            maker = makers.get(str(object_id))
            out["added_by_me"] = (maker is not None and maker == ctx.principal_id) or (
                maker is None and ctx.credential_kind == "local_operator")
        return out
    return [dict(i, **marks(i["id"])) if isinstance(i, dict) and i.get("id") is not None
            else i for i in items]


def _campaign_actions_for(con, entry: dict, ctx, campaign_id, payload):
    """payload, or a copy with allowed_actions narrowed (and can_change
    false) for a member who did not make the campaign."""
    if isinstance(payload, dict) and isinstance(payload.get("allowed_actions"), list) and \
            not _change_rule(con, entry, ctx, "campaign", [campaign_id])(campaign_id):
        return dict(payload, can_change=False,
                    allowed_actions=[a for a in payload["allowed_actions"]
                                     if a in _OTHERS_CAMPAIGN_ACTIONS])
    return payload


def registry_policy(entry):
    return {key: entry.get(key) for key in (
        "workspace_id", "db_path", "db_identity", "row_namespace",
        "is_owner", "bound_issuer", "bound_subject")}


def _model_session_origin(control, ctx, *, now=None) -> dict:
    """Ask's model_session (base design 4.2): the originating browser
    session row, re-read on every call. Sign-out, expiry, another account,
    a workspace switch, or an account the admission policy no longer
    admits ends the credential at its next call."""
    import time
    from . import admission as _admission
    from . import browser_auth as ba
    scopes = frozenset(ctx.scopes)
    if "research:read" not in scopes or not scopes <= {"research:read", "transcripts:read"}:
        raise _access.AuthorizationError("model session scope changed", 403)
    session = control.execute("SELECT * FROM browser_sessions WHERE session_hash=?",
                              (ctx.session_id,)).fetchone() if ctx.session_id else None
    if session is None or session["revoked_at"] is not None or \
            (time.time() if now is None else now) >= session["expires_at"] or \
            ba.session_principal(dict(session)) != ctx.principal_id:
        raise _access.AccessLost("session unavailable")
    if ctx.session_binding is None or \
            (session["issuer"], session["subject"]) != tuple(ctx.session_binding):
        raise _access.AccessLost("session identity changed")
    if ctx.session_switch_revision is None or \
            session["workspace_switch_revision"] != ctx.session_switch_revision or \
            (session["active_workspace"] or "") != ctx.workspace_id:
        raise _access.AccessLost("workspace switched")
    if _admission.decide(session["issuer"], session["subject"], session["email"],
                         bool(session["email_verified"]), control=control) != _admission.ADMITTED:
        raise _access.AccessLost("session unavailable")
    return dict(session)


def _current_authority(control, ctx, *, now=None):
    from . import connections as conn
    from . import browser_auth as ba
    reg = control.execute("SELECT * FROM workspace_registry WHERE workspace_id=?",
                          (ctx.workspace_id,)).fetchone()
    if reg is None:
        raise conn.GrantError("NOT_FOUND", "unknown workspace", 404)
    entry = dict(reg)
    if ctx.registry_policy is not None and registry_policy(entry) != ctx.registry_policy:
        raise _access.AccessLost("workspace authority binding changed")
    if ctx.db_identity and entry["db_identity"] != ctx.db_identity or \
            ctx.db_path and entry["db_path"] != ctx.db_path:
        raise _access.AccessLost("workspace identity changed")
    if ctx.connection_id:
        row = control.execute("SELECT * FROM connection_grants WHERE id=?",
                              (ctx.connection_id,)).fetchone()
        if row is None:
            raise _access.AuthorizationError("credential unavailable", 403)
        grant = conn.authorize_request(None, conn.GrantsStore._row_to_grant(row),
            workspace_id=ctx.workspace_id, policy_rev=ctx.policy_revision, now=now)
        from . import admission as _admission  # L14: every customer credential
        if not _admission.is_principal_admitted(grant.member_id):
            raise conn.GrantError("FORBIDDEN", "grant revoked", 403)
        # Admitted scopes may be narrower than the grant (a provider token
        # carries the token's scopes within the grant's), never wider and
        # never empty: a grant narrowed after admission still refuses.
        scopes = frozenset(ctx.scopes)
        if grant.member_id != ctx.principal_id or not scopes or \
                not scopes <= grant.scopes or \
                grant.content_policy != ctx.content_policy or grant.access_hash != ctx.credential_hash:
            raise _access.AuthorizationError("credential projection changed", 403)
    if ctx.credential_kind == "browser_session":
        session = control.execute("SELECT * FROM browser_sessions WHERE session_hash=?",
                                  (ctx.session_id,)).fetchone()
        import time
        if session is None or session["revoked_at"] is not None or \
                (time.time() if now is None else now) >= session["expires_at"] or \
                ba.session_principal(dict(session)) != ctx.principal_id:
            raise _access.AccessLost("session unavailable")
        entry["_session"] = dict(session)
        if ctx.session_binding is not None and (
                session["issuer"], session["subject"]) != ctx.session_binding:
            raise _access.AccessLost("session identity changed")
    if ctx.credential_kind == "model_session":
        entry["_session"] = _model_session_origin(control, ctx, now=now)
    if entry["is_owner"] and ctx.authority_kind != "share" and ctx.credential_kind != "local_reader" and ctx.principal_id != \
            f"oidc:{entry['bound_issuer']}:{entry['bound_subject']}":
        raise _access.AccessLost("owner binding changed")
    return entry


def _share_policy(state):
    return {"fields": sorted(_granted_field_set(state)),
            "share": {key: state.get(key) for key in (
                "workspace_id", "object_type", "object_id", "audience", "download", "expires_at", "revision")}}


def _workspace_authority(con, ctx, entry, *, bootstrap=False):
    if ctx.credential_kind == "local_reader":
        from . import connections
        profile, identity = connections._local_profile(con, ctx.scopes)
        if identity != ctx.db_identity or profile["owner"] != ctx.principal_id or \
                connections.local_profile_binding(profile, identity) != ctx.policy_identity:
            raise _access.AuthorizationError("local reader profile changed", 403)
        for source_key, field, exporting in ctx.disclosures:
            ctx.authorize_disclosure(con, source_key, [field], export=exporting)
        ctx.authorize_recording_captures(con)
        return
    if ctx.authority_kind == "share":
        state = _collaboration.share_state(con, ctx.share_grant_id)
        session = entry.get("_session") or {}
        audience = state["audience"]
        if state["state"] != "active" or _share_policy(state) != ctx.content_policy or \
                audience != ctx.principal_id and not (
                    session.get("email_verified") and session.get("email") == audience):
            raise _access.AuthorizationError("share projection or audience changed", 403)
        for source_key, field, exporting in ctx.disclosures:
            ctx.authorize_disclosure(con, source_key, [field], export=exporting)
        ctx.authorize_recording_captures(con)
        return
    role = "owner" if entry["is_owner"] else _collaboration.get_role(
        con, entry["row_namespace"], ctx.principal_id)
    if role is None and not bootstrap:
        raise _access.AccessLost("workspace membership required")
    if ctx.is_reader_credential and role != ctx.principal_kind:
        raise _access.AuthorizationError("reader role binding changed", 403)
    if ctx.admitted_role is not None and role != ctx.admitted_role:
        raise _access.RoleChanged("workspace role changed")
    from . import connections as _conn
    if ctx.is_reader_credential and (ctx.client_id or "").startswith(_conn.PERSONAL_CLIENT_PREFIX) \
            and not _conn.personal_token_current(con, entry["row_namespace"],
                                                 ctx.connection_id or "", ctx.principal_id or ""):
        # AGT-SEC-R2-01: a removal or role decision recorded after issuance.
        raise _access.AuthorizationError("membership changed since the token was issued", 403)
    for source_key, field, exporting in ctx.disclosures:
        ctx.authorize_disclosure(con, source_key, [field], export=exporting)
    ctx.authorize_recording_captures(con)


def fenced_authority_check(store, data_dir, ctx, *, operation, now=None,
                           control=None, bootstrap=False):
    """Fresh readonly workspace admission under the caller's control fence."""
    from . import connections as conn
    owned = control is None
    control = control or store._connect()
    try:
        with _db.transaction(control, write=True):
            entry = _current_authority(control, ctx, now=now)
            snap, _entry = conn.open_registered_db(
                store, ctx.workspace_id, data_dir, readonly=True, control=control)
            try:
                with _db.transaction(snap, write=False):
                    _workspace_authority(snap, ctx, entry, bootstrap=bootstrap)
            finally:
                snap.close()
            return {"allowed": True, "workspace_context": customer_view_context(ctx, entry)}
    finally:
        if owned:
            control.close()


def record_success(control, ctx, operation, now=None):
    """An actual successful final-admitted protected operation, not discovery."""
    from . import connections as conn
    if operation == "get_workspace_context" or operation.endswith("/reader-status"):
        return
    binding = json.dumps({"policy": ctx.policy_identity, "db_identity": ctx.db_identity,
                          "credential": ctx.credential_hash,
                          "disclosures": sorted(ctx.disclosures),
                          "verification_context": ctx.verification_context,
                          "registry_policy": ctx.registry_policy})
    control.execute("INSERT INTO agent_audit (request_id, grant_id, principal_id,"
                    " workspace_id, operation, object_ids, outcome, recorded_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (ctx.request_id, ctx.connection_id, ctx.principal_id, ctx.workspace_id,
                     operation, binding, "success", conn._now_iso(now)))
    if ctx.connection_id:
        control.execute("UPDATE connection_grants SET last_used_at=? WHERE id=?",
                        (conn._now_iso(now), ctx.connection_id))


def customer_overview(ctx, con, entry) -> dict:
    from . import customer as _customer
    _require_member(ctx, con, entry, "customer_overview")
    return _customer.overview(con, ctx=ctx)


# Overview's own Search read (app/views.js overviewPage), as the browser
# sends it: the read warm-up fills (READ-CACHE).
OVERVIEW_SEARCH = {"version": 2, "mode": "ads", "q": "", "brands": None, "shows": None,
                   "categories": None, "statuses": None, "audio": None, "date_status": "all",
                   "from": None, "to": None, "sort": "newest", "page": 1, "ids": None}


# The Search page's own research query as the browser sends it when Search
# opens (views.js searchPage: apiBundle(scope, ['week'])); the read warm-up
# fills it (SPEED-R1).
SEARCH_PAGE_BODY = {"query": OVERVIEW_SEARCH, "selector": None, "summaries": ["week"],
                    "refresh_selector": False}
# The filter rail's three lists (views.js filterLookup, 1200 px and up).
FILTER_FIELDS = ("brands", "shows", "categories")
# Tracked brands whose Brand page reads the warm-up fills, at most: their
# 2 searches each, the five-task brands' and Overview's own stay inside one
# class's Search budget (customer.SEARCH_KEEP), so the warm-up never drops
# what it just filled.
WARM_TRACKED_BRANDS = 3
# SPEED-FIX1 (lead decision 2 = b): the five customer tasks' first reads
# (launch-readiness ws-ux/FIVE-TASKS.md), filled for every role group at
# start (the nightly window restarts the process) and after a research
# data change (asgi.ReadWarmer). A brand is its Brand page (the brand, its
# newest and oldest ads, its weekly summary); a search is the research
# query the browser sends (five_task_body): the query's fields over the
# default Search, and its summaries.
FIVE_TASK_READS = (
    # T1: Brands > Quince, See all in Search, or "quince" typed in
    # Overview's search box.
    {"brand": "quince"},
    {"search": {"mode": "ads", "brands": ["quince"]}, "summaries": ["week"]},
    {"search": {"mode": "ads", "q": "quince"}, "summaries": ["week"]},
    # T4: Create alert on the Quince page, then Preview matches.
    {"search": {"mode": "ads", "brands": ["quince"]}, "summaries": []},
    # T3: NordVPN, Proton VPN and Surfshark (Brands, then See all in Search).
    {"brand": "nordvpn"},
    {"search": {"mode": "ads", "brands": ["nordvpn"]}, "summaries": ["week"]},
    {"brand": "proton vpn"},
    {"search": {"mode": "ads", "brands": ["proton vpn"]}, "summaries": ["week"]},
    {"brand": "surfshark"},
    {"search": {"mode": "ads", "brands": ["surfshark"]}, "summaries": ["week"]},
    # T2: the BetterHelp campaign's evidence (its Brand page and Search).
    {"brand": "betterhelp"},
    {"search": {"mode": "ads", "brands": ["betterhelp"]}, "summaries": ["week"]},
)
FIVE_TASK_LIMIT = 16   # reads in FIVE_TASK_READS, at most (tests pin it)
# RS1-MEMO: the seeded campaigns whose drafts the five-task warm-up opens
# (T2: Quince ad check 6, BetterHelp report 7, ShipStation 9; campaign 1
# is the Campaigns page's first row). A draft fills the shared ad rows
# (campaigns._ad_rows_by_episode, once per database state) and its own
# source-query match for the class. A campaign this database lacks is
# skipped; a failure is logged and the warm-up goes on.
WARM_CAMPAIGNS = (1, 6, 7, 9)


def five_task_body(read: dict) -> dict:
    """The research query body the browser sends for one search of
    FIVE_TASK_READS (views.js apiBundle)."""
    return {"query": dict(OVERVIEW_SEARCH, **read["search"]), "selector": None,
            "summaries": list(read["summaries"]), "refresh_selector": False}


def _pinned_research_tags() -> frozenset:
    """The research query keys the warm-up fills (the default Search page
    and the five-task searches): their entries have their own budget.
    Computed once per FIVE_TASK_READS."""
    global _PINNED
    if _PINNED[0] is FIVE_TASK_READS:
        return _PINNED[1]
    from . import customer_query as _query
    found = set()
    for body in [SEARCH_PAGE_BODY] + [five_task_body(r) for r in FIVE_TASK_READS if "search" in r]:
        tag = _query.research_query_tag(body["query"], body["selector"], body["summaries"],
                                        False, "warm")
        if tag is not None:
            found.add(tag[0])
    _PINNED = (FIVE_TASK_READS, frozenset(found))
    return _PINNED[1]


_PINNED: tuple = (None, frozenset())


def warm_class_reads(ctx, con, entry) -> None:
    """Fill the remembered reads a signed-in member's first pages make, for
    ctx's disclosure class (READ-CACHE warm-up): Overview, its Search, the
    saved items, the Brands list, the Shows list and the Alerts list; the
    Search page's research query and its filter rail (SPEED-R1); then each
    tracked brand's Brand page reads (the brand, its newest and oldest ads
    and its weekly summary), at most WARM_TRACKED_BRANDS. Each read runs
    its own gates first, exactly as a request does; the results are
    dropped."""
    from . import customer_contracts as _contracts
    customer_overview(ctx, con, entry)
    customer_search(ctx, con, entry, _contracts.normalize_query(dict(OVERVIEW_SEARCH)))
    saved = customer_saved_items(ctx, con, entry, "bookmark")
    customer_brands(ctx, con, entry, "")
    customer_shows(ctx, con, entry, "")
    customer_alerts(ctx, con, entry)
    customer_research_query(ctx, con, entry, json.loads(json.dumps(SEARCH_PAGE_BODY)))
    for field in FILTER_FIELDS:
        customer_filter_values(ctx, con, entry, field)
    tracked = [item["target"]["id"] for item in saved.get("items") or []
               if (item.get("target") or {}).get("kind") == "brand" and item["target"].get("id")]
    for key in tracked[:WARM_TRACKED_BRANDS]:
        _warm_brand_page(ctx, con, entry, key)


def warm_five_task_reads(ctx, con, entry) -> None:
    """Fill FIVE_TASK_READS for ctx's disclosure class (SPEED-FIX1), each
    read through its own gates, as a request; the results are dropped. A
    brand this class does not see (its page is the 404) is skipped. Its
    requests go on warm lists of their own (customer_query.WARM_TASKS;
    SPEED-FIX4, SF3-01)."""
    from . import customer_query as _query
    token = _query.WARM_TASKS.set(True)
    try:
        for read in FIVE_TASK_READS[:FIVE_TASK_LIMIT]:
            if "brand" in read:
                _warm_brand_page(ctx, con, entry, read["brand"])
            else:
                customer_research_query(ctx, con, entry, five_task_body(read))
        _warm_campaign_drafts(ctx, con, entry)
    finally:
        _query.WARM_TASKS.reset(token)


def _warm_campaign_drafts(ctx, con, entry) -> None:
    """Open each WARM_CAMPAIGNS draft this database holds, as a request
    does (RS1-MEMO); the results are dropped. One that fails is logged
    and skipped: it never stops the warm-up."""
    if not WARM_CAMPAIGNS:
        return
    try:
        have = {r[0] for r in con.execute(
            "SELECT id FROM campaigns WHERE id IN (%s)" % ",".join("?" * len(WARM_CAMPAIGNS)),
            WARM_CAMPAIGNS)}
    except sqlite3.OperationalError:
        return   # no campaigns table: nothing to warm
    for cid in WARM_CAMPAIGNS:
        if cid not in have:
            continue
        try:
            customer_campaign_draft(ctx, con, entry, cid)
        except Exception as exc:  # noqa: BLE001 - logged; the warm-up goes on
            _oplog.log_event("app.read_warm_draft_failed", level="warning",
                             campaign=cid, error=type(exc).__name__,
                             role=getattr(ctx, "principal_kind", None))


def _warm_brand_page(ctx, con, entry, key) -> None:
    """The reads of one Brand page (views.js brandPage): the brand, its
    newest and oldest ads, its weekly summary."""
    from . import customer_contracts as _contracts
    try:
        brand = customer_brand_detail(ctx, con, entry, key)["brand"]
    except (_ops.BadInput, _search.FilterRejected):
        return    # not a brand this class sees: its page is the 404
    ads = ((brand or {}).get("drill_down") or {}).get("ads")
    if not isinstance(ads, dict):
        return
    for sort in ("newest", "oldest"):
        customer_search(ctx, con, entry, _contracts.normalize_query(dict(ads, sort=sort, page=1)))
    customer_summary(ctx, con, entry, _contracts.normalize_query(dict(ads)), "week")


def customer_reader_status(ctx, con, entry) -> dict:
    from . import customer as _customer
    _require_member(ctx, con, entry, "customer_reader_status")
    from . import connections
    out = _customer.reader_status(con)
    if ctx.control_store is not None:
        control = ctx.control_store._connect()
        try:
            ids = [row["id"] for row in control.execute(
                "SELECT id FROM connection_grants WHERE workspace_id=? AND member_id=?",
                (ctx.workspace_id, ctx.principal_id))]
        finally:
            control.close()
        out["connections"] = [{"id": identity,
                               "state": connections.status_of(ctx.control_store, identity)}
                              for identity in ids]
        out["verified"] = any(item["state"] == connections.CONNECTED for item in out["connections"])
    return out


def customer_search(ctx, con, entry, criteria: dict, repo=None) -> dict:
    from . import customer as _customer
    _require_member(ctx, con, entry, "customer_search")
    return _customer.query(con, criteria, repo, ctx=ctx)


def customer_record(ctx, con, entry, ref: str, repo=None) -> dict:
    from . import customer as _customer
    _require_member(ctx, con, entry, "customer_record")
    with _db.transaction(con, write=False):
        return {"record": _customer.record(con, ref, repo, ctx=ctx)}


def customer_recording(ctx, con, entry, ref: str, range_header=None) -> dict:
    from . import media
    # The audio credential first (audio clips design 5.2): a reader token or
    # share guest is refused before the membership and entitlement gate.
    if media.audio_gated(ctx):
        ctx.require_audio_credential()
    # The expected-volume guard before any database or file access, and
    # again after the read, before the buffered response is released
    # (audio clips design 4.7, CLIP-R13-01).
    media.require_import_volume()
    _require_member(ctx, con, entry, "customer_recording")
    served = media.recording_chunk(con, ref, range_header, ctx=ctx)
    media.require_import_volume()
    return served


def customer_record_window(ctx, con, entry, ref: str, start_s=None,
                           end_s=None, cursor: str | None = None,
                           limit: int = 100, repo=None) -> dict:
    from . import customer as _customer
    _require_member(ctx, con, entry, "customer_record_window")
    with _db.transaction(con, write=False):
        out = _customer.record_window(
            con, ref, start_s, end_s, cursor, limit,
            customer_view_context(ctx, entry), ctx=ctx)
    return {"record": out}


def customer_summary(ctx, con, entry, criteria: dict, group_by: str) -> dict:
    from . import customer as _customer
    _require_member(ctx, con, entry, "customer_summary")
    return _customer.summary(con, criteria, group_by, ctx=ctx)


def customer_brands(ctx, con, entry, q: str = "") -> dict:
    from . import customer as _customer
    _require_member(ctx, con, entry, "customer_brands")
    return {"brands": _customer.brands_directory(con, q, ctx=ctx)}


def customer_brand_detail(ctx, con, entry, brand_key: str) -> dict:
    from . import customer as _customer
    _require_member(ctx, con, entry, "customer_brand_detail")
    return {"brand": _customer.brand_detail(con, brand_key, ctx=ctx)}


def customer_shows(ctx, con, entry, q: str = "") -> dict:
    from . import customer as _customer
    _require_member(ctx, con, entry, "customer_shows")
    return {"shows": _customer.customer_shows(con, q, ctx=ctx, ad_counts=True)}


def customer_episode_reads(ctx, con, entry, episode_id: str) -> dict:
    """Episode page ads, self-promo reads and screening state (A6)."""
    from . import customer as _customer
    _require_member(ctx, con, entry, "customer_episode_reads")
    with _db.transaction(con, write=False):
        return {"episode": _customer.episode_reads(con, episode_id, ctx=ctx)}


def customer_show_detail(ctx, con, entry, show_id: str) -> dict:
    from . import customer as _customer
    _require_member(ctx, con, entry, "customer_show_detail")
    return _customer.show_detail(con, show_id, ctx=ctx)


def customer_show_artwork(ctx, con, entry, show_id: str):
    """(mime, bytes) of one show's cover art, or None (ART-PERF: the show
    page's access rule and one row, no show page)."""
    from . import customer as _customer
    _require_member(ctx, con, entry, "customer_show_artwork")
    return _customer.show_artwork_bytes(con, show_id, ctx=ctx)


def customer_compare(ctx, con, entry, brands: list, period_a: dict,
                     period_b: dict | None = None) -> dict:
    from . import customer as _customer
    _require_member(ctx, con, entry, "customer_compare")
    return _customer.compare(con, brands, period_a, period_b)


def customer_view_context(ctx, entry) -> str:
    """Opaque authorized-view token for the active workspace/principal.

    Clients compare it with the displayed context and drop mismatching
    late responses. It is not a path, tenant authority, or credential.
    DEMO2 pass 16: it includes the caller's workspace role, so every role
    change changes it and the browser drops reads kept under the old role.
    """
    import hashlib as _hashlib
    wid = ""
    if isinstance(entry, dict):
        wid = str(entry.get("db_identity") or entry.get("workspace_id")
                  or ctx.workspace_id)
    else:
        wid = str(ctx.workspace_id)
    return _hashlib.sha256(json.dumps(
        [wid, ctx.principal_id, ctx.policy_revision, sorted(ctx.scopes),
         ctx.content_policy, ctx.authority_kind, ctx.share_grant_id,
         getattr(ctx, "admitted_role", None)],
        sort_keys=True).encode("utf-8")).hexdigest()[:32]


def customer_research_query(ctx, con, entry, body: dict, repo=None, *,
                            rank: bool = True) -> dict:
    """rank=False keeps the date order (research_bundle): the DEMO2 seed,
    which picks results by position (SR-R1-01)."""
    from . import customer as _customer
    from . import customer_query as _query
    _require_member(ctx, con, entry, "customer_research_query")
    if not isinstance(body, dict):
        from . import customer_contracts as _contracts
        raise _contracts.QueryRejected("body must be an object")
    summaries = body.get("summaries") or []
    if isinstance(summaries, str):
        summaries = [summaries]
    with _db.transaction(con, write=False):
        out = _query.research_bundle_memo(con, body.get("query"), body.get("selector"), list(summaries),
        body.get("refresh_selector") is True,
        customer_view_context(ctx, entry), repo, ctx=ctx,
        expected_view_revision=body.get("expected_view_revision"), rank=rank,
        pinned=_pinned_research_tags())
        avoid = _customer.recent_avoid_reads(con)
    # VISDATA-AVOID item 1: the Brand page's Recent ads read this route (n6), so it marks
    # the known-bleed ads as customer.query does (RECENT-ADS): a mark only, never a drop,
    # a reorder or a count change. Search draws every item and ignores the mark. New
    # dicts: the memo's result is never changed.
    result = out.get("result")
    if avoid and isinstance(result, dict) and result.get("items"):
        out = dict(out, result=dict(result, items=[
            dict(i, known_bleed=True) if i.get("evidence_id") in avoid else i for i in result["items"]]))
    return {"ok": True, **out}


def customer_filter_values(ctx, con, entry, field: str, q: str = "",
                           cursor: str | None = None,
                           limit: int = 50) -> dict:
    from . import customer_query as _query
    _require_member(ctx, con, entry, "customer_filter_values")
    with _db.transaction(con, write=False):
        out = _query.filter_values(con, field, q or "", cursor, limit, ctx=ctx)
    return {"ok": True, **out}


def customer_compare2(ctx, con, entry, brands: list, period_a: dict | None,
                      period_b: dict | None, query: dict | None,
                      selector: dict | None, repo=None) -> dict:
    from . import customer_query as _query
    _require_member(ctx, con, entry, "customer_compare")
    with _db.transaction(con, write=False):
        out = _query.compare_bundle(con, brands, period_a, period_b, query,
                                    selector,
                                    customer_view_context(ctx, entry), repo, ctx=ctx)
    return {"ok": True, **out}


def customer_creative_detail(ctx, con, entry, creative_id: str) -> dict:
    from . import customer as _customer
    _require_member(ctx, con, entry, "customer_creative_detail")
    return {"creative": _customer.creative_detail(con, creative_id, ctx=ctx)}


def customer_saved_items(ctx, con, entry, item_type=None, q: str = "",
                         cursor: str | None = None,
                         limit: int = 20) -> dict:
    from . import customer_saved as _saved
    _require_member(ctx, con, entry, "customer_saved_items")
    with _db.transaction(con, write=False):
        out = _saved.list_items(con, item_type=item_type, q=q or "",
                                cursor=cursor, limit=limit,
                                workspace_context=customer_view_context(
                                    ctx, entry), ctx=ctx)
        out = dict(out, items=_mark_changeable(con, entry, ctx, "saved_item", out["items"]))
    return {"ok": True, **out}


def customer_saved_item(ctx, con, entry, item_id: str) -> dict:
    from . import customer_saved as _saved
    _require_member(ctx, con, entry, "customer_saved_item")
    with _db.transaction(con, write=False):
        out = _saved.get_item(con, item_id, workspace_context=
                              customer_view_context(ctx, entry), ctx=ctx)
        out = dict(out, item=_mark_changeable(con, entry, ctx, "saved_item", [out["item"]])[0])
    return {"ok": True, **out}


def _refuse_recording_id(*values) -> None:
    """Member input may not carry a recording reference (owner decision,
    CLIPCORE-R7-01 option 2; R8-01/02, SEC-R10-01..03): writers store it in
    scanned columns, frozen reports and exports copy it, and the hosted
    start-up check refuses any copy (rights_admission)."""
    from . import rights_admission as _rights
    if _rights.has_recording_id(list(values)):
        raise _ops.BadInput(_rights.RECORDING_ID_REFUSAL)


def _refuse_stored(con, table: str, columns, where: str, params=()) -> None:
    """The same guard on the member text a writer stored, after its strip,
    cut or lowercasing, read back before commit so the caller's
    transaction rolls back (CLIPCORE-SEC-R11-01, R9-01)."""
    from . import rights_admission as _rights
    if _rights.stored_recording_id(con, table, columns, where, params):
        raise _ops.BadInput(_rights.RECORDING_ID_REFUSAL)


@_member_writer
def customer_save_item(ctx, con, entry, body: dict) -> dict:
    from . import customer_saved as _saved
    _require_customer_role(con, entry, ctx, "organize", "customer_save_item")
    with _db.transaction(con, write=True):
        out = _saved.create_item(con, body, workspace_context=customer_view_context(ctx, entry),
        actor=_customer_actor(ctx), ctx=ctx)
        # Tracking a tracked brand returns the item another member made.
        out = dict(out, item=_mark_changeable(con, entry, ctx, "saved_item", [out["item"]])[0])
    return {"ok": True, **out}


@_member_writer
def customer_edit_item(ctx, con, entry, item_id: str, body: dict,
                       expected_revision=None) -> dict:
    from . import customer_saved as _saved
    _require_customer_role(con, entry, ctx, "organize", "customer_edit_item")
    _require_maker(con, entry, ctx, "saved_item", item_id)
    with _db.transaction(con, write=True):
        out = _saved.update_item(con, item_id, body or {}, expected_revision,
                                 actor=_customer_actor(ctx), ctx=ctx)
    return {"ok": True, **out}


@_member_writer
def customer_delete_item(ctx, con, entry, item_id: str,
                         expected_revision=None) -> dict:
    from . import customer_saved as _saved
    _require_customer_role(con, entry, ctx, "organize", "customer_delete_item")
    _require_maker(con, entry, ctx, "saved_item", item_id)
    with _db.transaction(con, write=True):
        out = _saved.delete_item(con, item_id, expected_revision,
                                 actor=_customer_actor(ctx))
    return {"ok": True, **out}


def customer_note_collection(ctx, con, entry, q: str = "",
                             ref: str | None = None,
                             cursor: str | None = None,
                             limit: int = 20) -> dict:
    from . import customer_saved as _saved
    _require_member(ctx, con, entry, "customer_note_collection")
    with _db.transaction(con, write=False):
        out = _saved.list_notes(con, q=q or "", ref=ref, cursor=cursor,
                                limit=limit, ctx=ctx)
    return {"ok": True, **out}


def customer_note(ctx, con, entry, note_id) -> dict:
    from . import customer_saved as _saved
    _require_member(ctx, con, entry, "customer_note_collection")
    with _db.transaction(con, write=False):
        out = _saved.get_note(con, note_id, ctx=ctx)
    return {"ok": True, **out}


def customer_alerts(ctx, con, entry) -> dict:
    from . import customer as _customer
    _require_member(ctx, con, entry, "customer_alerts")
    with _db.transaction(con, write=False):
        return {"alerts": _mark_changeable(con, entry, ctx, "alert",
                                           _customer.list_alerts(con, ctx=ctx))}


def customer_alert(ctx, con, entry, alert_id: int, summaries: bool = True,
                   page: int = 1) -> dict:
    from . import customer as _customer
    _require_member(ctx, con, entry, "customer_alert")
    alert = _customer.get_alert(con, alert_id, ctx=ctx, summaries=summaries, page=page)
    with _db.transaction(con, write=False):
        return {"alert": _mark_changeable(con, entry, ctx, "alert", [alert])[0]}


@_member_writer
def customer_save_alert(ctx, con, entry, name: str, query: dict,
                        origin_route: str = "", event_types=None,
                        delivery: dict | None = None,
                        selector: dict | None = None, alert_id=None,
                        expected_revision=None) -> dict:
    from . import customer as _customer
    _require_customer_role(con, entry, ctx, "organize", "customer_save_alert")
    _require_maker(con, entry, ctx, "alert", alert_id)
    with _db.transaction(con, write=True):
        out = _customer.save_alert(
            con, name, query, origin_route, event_types, delivery, selector,
            alert_id, expected_revision, _customer_actor(ctx), ctx=ctx)
        _refuse_stored(con, "customer_alerts", ("name",), "id=?", (out["id"],))  # stripped
        return out


@_member_writer
def customer_alert_lifecycle(ctx, con, entry, alert_id: int, lifecycle: str,
                             expected_revision=None) -> dict:
    from . import customer as _customer
    _require_customer_role(con, entry, ctx, "organize", "customer_alert_lifecycle")
    _require_maker(con, entry, ctx, "alert", alert_id)
    return _customer.set_alert_lifecycle(con, alert_id, lifecycle, expected_revision,
                                         _customer_actor(ctx))


@_member_writer
def customer_delete_alert(ctx, con, entry, alert_id: int) -> dict:
    """M8: the alert's maker (or an owner, the Stop rule) deletes it and its
    events in one transaction. The gate reuses the lifecycle operation name
    (billing.KNOWN_OPERATIONS): deleting ends the alert as Stop does."""
    from . import customer as _customer
    _require_customer_role(con, entry, ctx, "organize", "customer_alert_lifecycle")
    _require_maker(con, entry, ctx, "alert", alert_id)
    return _customer.delete_alert(con, alert_id, _customer_actor(ctx))


@_member_writer
def customer_check_alert(ctx, con, entry, alert_id: int) -> dict:
    from . import customer as _customer
    _require_customer_role(con, entry, ctx, "organize", "customer_check_alert")
    return _customer.check_alert(con, alert_id, _customer_actor(ctx), ctx=ctx)


@_member_writer
def customer_alert_read(ctx, con, entry, alert_id: int, event_ids: list, *,
                        workspace_wide: bool = False) -> dict:
    """The caller's own read marks (READ-PP). workspace_wide marks them
    read for everyone; server callers only (the seed's past matches), no
    route passes it."""
    from . import customer as _customer
    _require_customer_role(con, entry, ctx, "organize", "customer_alert_read")
    return _customer.mark_alert_events_read(
        con, alert_id, event_ids, _customer_actor(ctx),
        member=None if workspace_wide else _customer.read_member(ctx), ctx=ctx)


@_member_writer
def customer_alert_backfill(ctx, con, entry, alert_id: int, confirm: bool) -> dict:
    from . import customer as _customer
    _require_customer_role(con, entry, ctx, "organize", "customer_alert_backfill")
    _require_maker(con, entry, ctx, "alert", alert_id)
    return _customer.backfill_alert(con, alert_id, confirm is True,
                                    _customer_actor(ctx), ctx=ctx)


def customer_preferences(ctx, con, entry) -> dict:
    from . import customer as _customer
    _require_member(ctx, con, entry, "customer_preferences")
    # The plan signal, as on the research query: a Settings page opened
    # first knows it reads at excerpt level (Plan A).
    # PREFS-SHARED: the caller's own display preferences, never another
    # member's, and never the stored workspace display name.
    return {"preferences": _customer.get_preferences(
                con, _customer.pref_member(ctx), hosted=True),
            "excerpt_only": _customer.excerpt_workspace(con, ctx)}


@_member_writer
def customer_save_preferences(ctx, con, entry, prefs: dict,
                              expected_revision=None) -> dict:
    from . import customer as _customer
    _require_customer_role(con, entry, ctx, "organize", "customer_save_preferences")
    from . import rights_admission as _rights
    with _db.transaction(con, write=True):
        out = _customer.save_preferences(con, prefs, expected_revision,
                                         _customer_actor(ctx),
                                         _customer.pref_member(ctx))
        # display_name is stored stripped: check what was kept.
        stored = out["preferences"]
        if isinstance(prefs, dict) and _rights.has_recording_id(
                [stored.get(key) for key in prefs]):
            raise _ops.BadInput(_rights.RECORDING_ID_REFUSAL)
        if _customer.pref_member(ctx) is None:
            # No member: the shared values were written; a hosted caller
            # still never sees the stored display name.
            out = dict(out, preferences=_customer.get_preferences(con, None, hosted=True))
        return out


@_member_writer
def customer_save_mistake(ctx, con, entry, ref: str | None, reason: str,
                          status: str = "open", mistake_id=None,
                          expected_revision=None) -> dict:
    from . import customer_saved as _saved
    _require_customer_role(con, entry, ctx, "comment", "customer_save_mistake")
    with _db.transaction(con, write=True):
        out = _saved.save_note(con, ref, reason, status, mistake_id,
                               expected_revision, _customer_actor(ctx), ctx=ctx)
        _refuse_stored(con, "customer_mistake_reports", ("reason",), "id=?",
                       (out["id"],))  # stripped and cut to 2000
        return out


@_member_writer
def customer_report_ad(ctx, con, entry, ref: str, body: dict) -> dict:
    """A member reports an ad as wrong (admin review): members only (no
    share guests, no reader credentials), only on a record the member may
    read; it waits in the operator's queue and changes nothing shown."""
    from . import corrections as _corrections
    _access.require_customer_mutation(ctx, "customer_report_ad")
    if ctx.authority_kind == "share":
        raise _access.AuthorizationError(
            "share grants do not allow operation: customer_report_ad", 403)
    _require_entitlement(con, entry, "customer_report_ad")
    if not isinstance(body, dict) or set(body) - {"reason", "note"}:
        raise _ops.BadInput("a report holds reason and note only")
    with _db.transaction(con, write=True):
        out = _corrections.report(con, ref, body.get("reason"), body.get("note") or "",
                                  ctx=ctx)
        _refuse_stored(con, "ad_reports", ("note",), "id=?",
                       (out["report"]["id"],))  # stored stripped
        return out


def customer_campaigns(ctx, con, entry) -> dict:
    from . import campaigns as _camp
    _require_member(ctx, con, entry, "customer_campaigns")
    with _db.transaction(con, write=False):
        rows = _ops.list_campaigns(con, ctx=ctx)
        return {"campaigns": _mark_changeable(con, entry, ctx, "campaign",
                                              _camp.customer_list_summaries(con, rows))}


def customer_campaign_detail(ctx, con, entry, campaign_id: int) -> dict:
    from . import campaigns as _camp
    _require_member(ctx, con, entry, "customer_campaign_detail")
    d = _ops.campaign_detail(con, campaign_id, ctx=ctx)
    if d is None:
        raise _ops.BadInput("unknown campaign")
    with _db.transaction(con, write=False):
        dto = _camp.campaign_projection(con, d)
        dto = _campaign_actions_for(con, entry, ctx, campaign_id, dto)
    return {"campaign": dto}


@_member_writer
def customer_save_campaign(ctx, con, entry, body: dict, campaign_id=None,
                           expected_revision=None) -> dict:
    from . import campaigns as _camp
    _require_customer_role(con, entry, ctx, "organize", "customer_save_campaign")
    _require_maker(con, entry, ctx, "campaign", campaign_id)
    if campaign_id is not None:
        _citations.authorize_resource(con, "campaign", campaign_id, ctx)
    elif not ctx.permits("campaign"):
        raise _access.AuthorizationError("campaign disclosure denied", 403)
    with _db.transaction(con, write=True):
        out = _camp.save_customer_campaign(con, body, campaign_id,
                                           expected_revision,
                                           _customer_actor(ctx))
        # Text is stored stripped, cut, and (brand) lowercased: check the
        # columns this save wrote.
        fields = body.get("campaign") if isinstance(body, dict) else None
        written = [key for key in ("name", "brand", "message", "offer", "notes")
                   if campaign_id is None or (isinstance(fields, dict) and key in fields)]
        _refuse_stored(con, "campaigns", written, "id=?", (out["campaign"]["id"],))
        _citations.authorize_resource(con, "campaign", out["campaign"]["id"], ctx)
        return out


def customer_campaign_draft(ctx, con, entry, campaign_id: int,
                            brief_check: bool = False) -> dict:
    """brief_check: the `brief_check` switch (WIN-2): model_checks is added
    when it is on and model_brief_checks exists; freeze never reads it."""
    from . import campaigns as _camp
    _require_member(ctx, con, entry, "customer_campaign_draft")
    _citations.authorize_resource(con, "campaign", campaign_id, ctx)
    draft = _camp.build_customer_draft(con, campaign_id, ctx=ctx)
    draft["pins"] = _camp.shown_draft_pins(con, draft["pins"], ctx)
    with _db.transaction(con, write=False):
        draft = _campaign_actions_for(con, entry, ctx, campaign_id, draft)
        if brief_check:
            from . import brief_checks as _brief_checks
            draft = _brief_checks.with_model_checks(con, campaign_id, draft)
    return {"draft": draft}


def customer_evidence_fit(ctx, con, entry, campaign_id: int, ref: str) -> dict:
    """Which placements would accept this exact read (UAT-092), read-only."""
    from . import campaigns as _camp
    _require_member(ctx, con, entry, "customer_evidence_fit")
    _citations.authorize_resource(con, "campaign", campaign_id, ctx)
    if isinstance(ref, str) and ref:
        _citations.captured_disclosure(con, ref, ctx)
    return _camp.placement_evidence_fit(con, campaign_id, ref)


@_member_writer
def customer_save_placement(ctx, con, entry, campaign_id: int, body: dict,
                            placement_id: str | None = None,
                            expected_revision=None,
                            expected_campaign_revision=None) -> dict:
    from . import campaigns as _camp
    _require_customer_role(con, entry, ctx, "organize", "customer_save_placement")
    _require_maker(con, entry, ctx, "campaign", campaign_id)
    _citations.authorize_resource(con, "campaign", campaign_id, ctx)
    placement = body.get("placement", body or {})
    if placement_id is None and placement.get("scope") == "episode":
        _citations.captured_disclosure(con, _citations.current_source_ref(
            con, "episode", placement.get("episode_id")), ctx)
    with _db.transaction(con, write=True):
        return _camp.save_customer_placement(
            con, campaign_id, body, placement_id, expected_revision,
            expected_campaign_revision, _customer_actor(ctx))


def customer_briefs(ctx, con, entry, q: str = "", cursor: str | None = None,
                    limit: int = 20) -> dict:
    from . import briefs as _briefs
    _require_member(ctx, con, entry, "customer_briefs")
    with _db.transaction(con, write=False):
        out = _briefs.list_briefs(con, q or "", cursor, limit, ctx=ctx)
    return {"ok": True, **out}


def customer_brief_version(ctx, con, entry, brief_id: str,
                           version) -> dict:
    from . import briefs as _briefs
    _require_member(ctx, con, entry, "customer_briefs")
    with _db.transaction(con, write=False):
        out = _briefs.get_version(con, brief_id, version, ctx=ctx)
    return {"ok": True, "brief_version": out}


@_member_writer
def customer_save_brief(ctx, con, entry, body: dict, brief_id=None,
                        expected_revision=None) -> dict:
    from . import briefs as _briefs
    _require_customer_role(con, entry, ctx, "organize", "customer_save_brief")
    _require_maker(con, entry, ctx, "brief", brief_id)
    with _db.transaction(con, write=True):
        if brief_id is None:
            out = _briefs.create_brief(con, body or {},
                                       _customer_actor(ctx), ctx=ctx)
        else:
            out = _briefs.add_version(con, brief_id, body or {},
                                      expected_revision,
                                      _customer_actor(ctx), ctx=ctx)
    return {"ok": True, **out}


@_member_writer
def customer_allocate(ctx, con, entry, campaign_id: int, body: dict,
                      expected_campaign_revision=None) -> dict:
    from . import campaigns as _camp
    _require_customer_role(con, entry, ctx, "organize", "customer_allocate")
    _require_maker(con, entry, ctx, "campaign", campaign_id)
    _citations.authorize_resource(con, "campaign", campaign_id, ctx)
    _citations.captured_disclosure(con, (body or {}).get("ref", ""), ctx)
    with _db.transaction(con, write=True):
        out = _camp.allocate_placement_evidence(
            con, campaign_id, body or {}, expected_campaign_revision,
            _customer_actor(ctx))
    if isinstance(out, dict) and "conflict" in out:
        return out
    return {"ok": True, **out}


def customer_campaign_targets(ctx, con, entry, campaign_id: int) -> dict:
    from . import campaigns as _camp
    _require_member(ctx, con, entry, "customer_campaign_targets")
    _citations.authorize_resource(con, "campaign", campaign_id, ctx)
    with _db.transaction(con, write=False):
        out = _camp.list_targets(con, campaign_id)
    return {"ok": True, "targets": out}


@_member_writer
def customer_save_target(ctx, con, entry, campaign_id: int, body: dict,
                         target_id: str | None = None,
                         expected_revision=None) -> dict:
    from . import campaigns as _camp
    _require_customer_role(con, entry, ctx, "organize", "customer_save_target")
    _require_maker(con, entry, ctx, "campaign", campaign_id)
    _citations.authorize_resource(con, "campaign", campaign_id, ctx)
    target = body.get("target", body or {})
    if target_id is None and target.get("scope", "episode") == "episode":
        _citations.captured_disclosure(con, _citations.current_source_ref(
            con, "episode", target.get("episode_id")), ctx)
    with _db.transaction(con, write=True):
        if target_id is None:
            target = body.get("target", body or {})
            out = _camp.create_target(
                con, campaign_id, target.get("scope", "episode"),
                target.get("show_id"), target.get("episode_id"),
                target.get("date_from"), target.get("date_to"),
                target.get("timezone", "UTC"), target.get("note", ""),
                _customer_actor(ctx),
                (body or {}).get("expected_campaign_revision"))
        else:
            out = _camp.update_target_note(
                con, campaign_id, target_id, (body.get("target", body or {})
                                             ).get("note", ""),
                expected_revision, _customer_actor(ctx))
        return {"ok": True, **out}


@_member_writer
def customer_delete_target(ctx, con, entry, campaign_id: int,
                           target_id: str) -> dict:
    from . import campaigns as _camp
    _require_customer_role(con, entry, ctx, "organize",
                           "customer_delete_target")
    _require_maker(con, entry, ctx, "campaign", campaign_id)
    _citations.authorize_resource(con, "campaign", campaign_id, ctx)
    with _db.transaction(con, write=True):
        out = _camp.delete_target(con, campaign_id, target_id,
                                  _customer_actor(ctx))
    return {"ok": True, **out}


@_member_writer
def customer_attach_evidence(ctx, con, entry, campaign_id: int,
                             placement_id: str, ref: str,
                             expected_revision=None) -> dict:
    from . import campaigns as _camp
    _require_customer_role(con, entry, ctx, "organize", "customer_attach_evidence")
    _require_maker(con, entry, ctx, "campaign", campaign_id)
    _citations.authorize_resource(con, "campaign", campaign_id, ctx)
    _citations.captured_disclosure(con, ref, ctx)
    return _camp.save_placement_evidence(con, campaign_id, placement_id, ref,
                                         expected_revision,
                                         _customer_actor(ctx))


@_member_writer
def customer_review_episode(ctx, con, entry, campaign_id: int, placement_id: str,
                            episode_id: str, evidence_id: str, decision: str,
                            expected_revision=None, code=None, shown_code=None) -> dict:
    """A judge's confirm, reject or undo on one read of one episode of a
    placement (FUNC-2 Addendum A): the owner or any editor, not only the
    campaign's maker; a viewer is refused."""
    from . import campaigns as _camp
    _require_customer_role(con, entry, ctx, "organize", "customer_review_episode")
    _citations.authorize_resource(con, "campaign", campaign_id, ctx)
    return _camp.save_episode_review(con, campaign_id, placement_id, episode_id,
                                     evidence_id, decision, expected_revision,
                                     code=code, actor=_customer_actor(ctx), ctx=ctx,
                                     shown_code=shown_code)


def _require_target_evidence_writer(ctx, con, entry, campaign_id: int) -> None:
    """Who may add an observation to a research target, and so remove it
    (G2-01): the one permission check for both, never a second one. Since
    FR-11 that is the campaign's maker or an owner."""
    _require_customer_role(con, entry, ctx, "organize",
                           "customer_attach_target_evidence")
    _require_maker(con, entry, ctx, "campaign", campaign_id)
    _citations.authorize_resource(con, "campaign", campaign_id, ctx)


@_member_writer
def customer_attach_target_evidence(ctx, con, entry, campaign_id: int,
                                    target_id: str, ref: str,
                                    expected_revision=None) -> dict:
    """A research observation: an editor keeps a passage on a target."""
    from . import campaigns as _camp
    _require_target_evidence_writer(ctx, con, entry, campaign_id)
    _citations.captured_disclosure(con, ref, ctx)
    with _db.transaction(con, write=True):
        return _camp.attach_target_evidence(con, campaign_id, target_id, ref,
                                            expected_revision,
                                            _customer_actor(ctx))


@_member_writer
def customer_detach_target_evidence(ctx, con, entry, campaign_id: int,
                                    target_id: str, ref: str,
                                    expected_revision=None) -> dict:
    """Remove one observation from a research target (G2-01): the link
    only, the evidence stays. Undo is the attach above."""
    from . import campaigns as _camp
    _require_target_evidence_writer(ctx, con, entry, campaign_id)
    with _db.transaction(con, write=True):
        return _camp.detach_target_evidence(con, campaign_id, target_id, ref,
                                            expected_revision,
                                            _customer_actor(ctx))


@_member_writer
def customer_detach_evidence(ctx, con, entry, campaign_id: int,
                             placement_id: str, ref: str,
                             expected_revision=None) -> dict:
    from . import campaigns as _camp
    _require_customer_role(con, entry, ctx, "organize", "customer_detach_evidence")
    _require_maker(con, entry, ctx, "campaign", campaign_id)
    _citations.authorize_resource(con, "campaign", campaign_id, ctx)
    return _camp.remove_placement_evidence(con, campaign_id, placement_id, ref,
                                           expected_revision,
                                           _customer_actor(ctx))


def customer_reports(ctx, con, entry, campaign_id: int) -> dict:
    from . import campaigns as _camp
    _require_member(ctx, con, entry, "customer_reports")
    _citations.authorize_resource(con, "campaign", campaign_id, ctx)
    return {"reports": _camp.list_customer_reports(con, campaign_id, ctx=ctx)}


def customer_report(ctx, con, entry, campaign_id: int, report_no: int) -> dict:
    from . import campaigns as _camp
    _require_member(ctx, con, entry, "customer_report")
    _citations.authorize_resource(con, "campaign", campaign_id, ctx)
    return _camp.get_customer_report(con, campaign_id, report_no, ctx=ctx)


@_member_writer
def customer_freeze_report(ctx, con, entry, campaign_id: int,
                           expected_revision, pins: dict) -> dict:
    from . import campaigns as _camp
    _require_customer_role(con, entry, ctx, "organize", "customer_freeze_report")
    _citations.authorize_resource(con, "campaign", campaign_id, ctx, export=True)
    from . import rights_admission as _rights
    with _db.transaction(con, write=True):
        typed = _rights.member_recording_ids(con)
        out = _camp.freeze_customer_report(con, campaign_id, expected_revision,
                                           pins, _customer_actor(ctx), ctx=ctx)
        # Campaign, note and brief text stored before the writer guard, or
        # by a local writer, is copied here (CLIPCORE-R8-N1, SEC-R10-03).
        if typed:
            for table, column in (("exports", "id"), ("campaign_reports", "export_id")):
                _refuse_copied_ids(con, typed, table, f"{column}=?", (out["export_id"],))
    return out




@_member_writer
def customer_research_export(ctx, con, entry, body: dict, repo=None, prepared=None) -> dict:
    """prepared: prepare_research_export's result for this request (the
    hosted route runs it off the event loop first); else prepared here."""
    from . import customer_contracts as _contracts
    from . import customer_query as _query
    _require_customer_role(con, entry, ctx, "organize", "customer_research_export")
    if not isinstance(body, dict):
        raise _contracts.QueryRejected("body must be an object")
    with _db.transaction(con, write=True):
        if prepared is None:
            prepared = _prepare_export(ctx, con, entry, body, repo)
        out = _query.store_export(con, prepared, actor=_customer_actor(ctx), ctx=ctx)
        # The stored title is cut to 160 characters: check what was kept.
        _refuse_stored(con, "exports", ("title",), "id=?", (out["export_id"],))
    return {"ok": True, **out}


def _prepare_export(ctx, con, entry, body: dict, repo=None) -> dict:
    from . import customer_query as _query
    return _query.prepare_export(
        con, body.get("query"), body.get("selector"),
        body.get("scope", "all_matches"), body.get("format", "json"),
        body.get("expected_view_revision", ""),
        customer_view_context(ctx, entry), repo,
        title=str(body.get("title") or "EarWitness research export")[:160], ctx=ctx)


def readonly_snapshot(store, data_dir, ctx, entry, fn):
    """fn(ro) on a read-only connection of its own to the request's
    database, in one read snapshot, writing nothing. For work that runs in
    a worker thread (asgi offload and off-loop reads), so it holds no lock."""
    from . import connections as _conn
    ro, reopened = _conn.open_registered_db(store, ctx.workspace_id, data_dir, readonly=True)
    try:
        # The registry may have been re-pointed since the request opened
        # its workspace: read only from that same database.
        if reopened["db_identity"] != entry["db_identity"]:
            raise _conn.GrantError("NOT_FOUND", "unknown workspace", 404)
        with _db.transaction(ro, write=False):
            return fn(ro)
    finally:
        ro.close()


@_member_writer
def prepare_research_export(store, data_dir, ctx, entry, body: dict) -> dict:
    """The export's match and render on a read-only snapshot
    (readonly_snapshot); customer_research_export stores the result only
    if the view is unchanged and every check passes again."""
    from . import customer_contracts as _contracts

    def prepare(ro):
        _require_customer_role(ro, entry, ctx, "organize", "customer_research_export")
        if not isinstance(body, dict):
            raise _contracts.QueryRejected("body must be an object")
        return _prepare_export(ctx, ro, entry, body)
    return readonly_snapshot(store, data_dir, ctx, entry, prepare)


@_member_writer
def prepare_brand_bookmark(store, data_dir, ctx, entry, body) -> None:
    """PERF-HOL: resolve a brand bookmark's brand (customer.brand_identity)
    on a read-only snapshot in a worker thread, after the same role check
    as customer_save_item. It returns nothing: the result is remembered per
    data state, so the save, which runs every check again, reuses it when
    nothing changed meanwhile and resolves it again otherwise. A brand that
    does not resolve is left to the save to refuse."""
    from . import customer as _customer
    target = body.get("target") if isinstance(body, dict) else None
    if not isinstance(target, dict) or body.get("type") != "bookmark" \
            or target.get("kind") != "brand":
        return

    def prepare(ro):
        _require_customer_role(ro, entry, ctx, "organize", "customer_save_item")
        try:
            # Model names off, as in the save, so its lookup finds this result.
            with _customer.model_names(False):
                _customer.brand_identity(ro, target.get("id"), ctx=ctx)
        except (_ops.BadInput, _search.FilterRejected):
            pass
    readonly_snapshot(store, data_dir, ctx, entry, prepare)


def customer_archive(ctx, con, entry, kind: str | None = None,
                     campaign_id=None, q: str = "",
                     cursor: str | None = None, limit: int = 20) -> dict:
    from . import exports as _exports
    _require_member(ctx, con, entry, "customer_archive")
    with _db.transaction(con, write=False):
        return _exports.archive_collection(con, kind=kind, campaign_id=campaign_id, q=q, cursor=cursor,
        limit=limit, ctx=ctx)


def customer_export_bytes(ctx, con, entry, export_id) -> dict:
    from . import exports as _exports
    _require_member(ctx, con, entry, "customer_export_download")
    with _db.transaction(con, write=False):
        return {"ok": True, **_exports.load_export_bytes(con, export_id, ctx=ctx)}


# -- Hosted collaboration (Task 12): workspaces, invites, shares -----------
#
# Membership management requires the manage permission (owner by
# default); invitations never send on write (delivery is an explicit
# gated outbox action); share guests stay inside the exact guest
# allowlist with named-audience binding, expiry, revocation, and
# download restrictions rechecked per fetch.


@_member_writer
def collab_create_workspace(store, data_dir, ctx, workspace_id, name, *, now=None):
    """Exact authenticated create admission; no active membership is required."""
    import os
    import time
    clock = now if callable(now) else time.time if now is None else lambda: now
    from . import connections, browser_auth
    if ctx.credential_kind != "browser_session" or ctx.authority_kind != "bootstrap" \
            or ctx.scopes != frozenset({"workspace:create"}):
        raise _access.AuthorizationError("authenticated create admission required", 403)
    if not isinstance(workspace_id, str) or not connections._workspace_id_pattern().fullmatch(workspace_id) \
            or workspace_id == "local":
        raise _ops.BadInput("invalid workspace id")
    filename = os.path.join(os.path.realpath(data_dir), workspace_id + ".sqlite3")
    control = store._connect()
    created = False
    try:
        with _db.transaction(control, write=True):
            session = control.execute("SELECT * FROM browser_sessions WHERE session_hash=?",
                                      (ctx.session_id,)).fetchone()
            if session is None or session["revoked_at"] is not None or \
                    clock() >= session["expires_at"] or \
                    browser_auth.session_principal(dict(session)) != ctx.principal_id:
                raise _access.AccessLost("session unavailable")
            from . import admission as _admission  # L14 admission gate
            _admission.require_admitted(session["issuer"], session["subject"],
                                        session["email"],
                                        bool(session["email_verified"]))
            # A retired ID (deleted workspace, N11) is never created again.
            if _admission.is_workspace_retired(workspace_id):
                raise _access.AuthorizationError("workspace id unavailable", 409)
            # An existing ID is refused first and uniformly (owner or
            # customer, cap full or not), in this same write transaction,
            # so concurrent creates of one ID admit exactly one.
            if connections.workspace_id_registration(control, workspace_id) is not None:
                raise _access.AuthorizationError("workspace id unavailable", 409)
            # An existing file for this name (registered under another ID,
            # or an orphan) gets the same answer; nothing is unlinked.
            if os.path.lexists(filename):
                raise _access.AuthorizationError("workspace id unavailable", 409)
            # Pilot cap: counted in this control write transaction, so
            # concurrent creates serialise on the same lock and the check
            # and the registration cannot interleave. Refused before any
            # file exists.
            _admission.check_workspace_capacity(control.execute(
                "SELECT COUNT(*) FROM workspace_registry").fetchone()[0])
            try:
                fd = os.open(filename, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                raise _access.AuthorizationError("workspace id unavailable", 409)
            os.close(fd)
            created = True
            con = sqlite3.connect(filename, isolation_level=None)
            con.row_factory = sqlite3.Row
            try:
                _db.init_schema(con)
                with _db.transaction(con, write=True):
                    _collaboration.create_workspace(con, "local", name or workspace_id, ctx.principal_id)
                    # The name is stored cut to 160 characters.
                    _refuse_stored(con, "workspaces", ("name",), "id='local'")
                    # L18: ready by construction (docs/hosting/workspace-readiness.md).
                    from . import conversion as _conversion
                    _conversion.record_product_creation(con, ctx.principal_id)
                    if clock() >= session["expires_at"]:
                        raise _access.AccessLost("session expired during create")
                connections.register_workspace(store, workspace_id, filename, data_dir,
                                               operator=ctx.principal_id, control=control)
                if clock() >= session["expires_at"]:
                    raise _access.AccessLost("session expired before create commit")
            finally:
                con.close()
        return {"workspace_id": workspace_id, "name": name or workspace_id, "role": "owner"}
    except BaseException:
        if created:
            os.unlink(filename)
        raise
    finally:
        control.close()


def open_shared_workspace(store, workspace_id: str, data_dir: str,
                          session: dict, share_id: str):
    """Resolve a share guest: registry routing plus named-audience
    binding against the authenticated session, then an exact-state
    check (revoked/expired denied). Returns (con, entry, guest_ctx).
    Forged IDs, other workspaces' shares, and non-audience accounts
    fail closed without disclosing paths."""
    from . import browser_auth as _ba
    from . import connections as _conn
    entry = _conn.resolve_workspace(store, workspace_id)
    con, entry = _conn.open_registered_db(store, workspace_id, data_dir, readonly=True)
    try:
        principal = _ba.session_principal(session)
        # Uniform refusal: a caller who is not the named audience gets the
        # unknown-object answer whether the share is real, revoked,
        # expired, in another workspace or made up. Only the audience
        # learns the share's state.
        try:
            state = _collaboration.share_state(con, share_id)
        except _ops.BadInput:
            raise _ops.NotFound("unknown object") from None
        audience = state.get("audience") or ""
        email = session.get("email") or ""
        if state.get("workspace_id") != entry["row_namespace"] or (
                principal != audience and not (
                    session.get("email_verified") and email
                    and email == audience)):
            raise _ops.NotFound("unknown object")
        if state.get("state") != "active":
            raise _access.AuthorizationError(
                f"share is {state.get('state')}", 403)
        guest = _access.AccessContext.create(
            request_id=None, authority_kind="share",
            principal_id=principal, principal_kind="guest",
            workspace_id=workspace_id,
            scopes=(), share_grant_id=share_id,
            credential_kind="browser_session",
            session_id=session.get("session_hash", ""),
            content_policy=_share_policy(state))
    except Exception:
        con.close()
        raise
    return con, entry, guest


def _visible_member_emails(ctx, con, namespace: str) -> dict:
    """The bound emails this ctx may read. Another member's email is
    visible only where pending invitations are (collab_invites: a
    member-session credential with the "membership" permission); any
    principal always sees its own. Otherwise names fall back to the
    principal's subject in the UI."""
    emails = _member_emails(con, namespace)
    me = ctx.principal_id or ""
    try:
        _access.require_customer_mutation(ctx, "collab_member_emails")
        manages = _collaboration.check_permission(con, namespace, me, "membership")
    except _access.AuthorizationError:
        manages = False
    return emails if manages else {k: v for k, v in emails.items() if k == me}


def _member_emails(con, namespace: str) -> dict:
    """principal -> email bound by a verified invite acceptance in this
    workspace (latest wins). Missing audit table or bad JSON is tolerated."""
    out: dict = {}
    try:
        rows = con.execute(
            "SELECT target_id, detail FROM audit WHERE"
            " action='invite_accept_bound' ORDER BY id").fetchall()
    except sqlite3.Error:
        return out
    for row in rows:
        try:
            detail = json.loads(row["detail"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(detail, dict) or detail.get("workspace") != namespace:
            continue
        email = detail.get("bound_email")
        if isinstance(email, str) and email:
            out[row["target_id"]] = email
    return out


def collab_members(ctx, con, entry) -> dict:
    ctx.require_member("collab_members")
    rows = [dict(r) for r in con.execute(
        "SELECT member, role, revoked, created_at FROM memberships"
        " WHERE workspace_id=? ORDER BY member",
        (entry["row_namespace"],)).fetchall()]
    emails = _visible_member_emails(ctx, con, entry["row_namespace"])
    for row in rows:
        row["email"] = emails.get(row["member"], "")
        row["display"] = _member_display(con, row["member"], row["email"])
    return {"members": rows}


def _member_display(con, member: str, email: str) -> str:
    """JE-SETTINGS: what Settings shows for a member, never the raw id: the
    member's own display name, else the local part of the email this caller
    may see, else "Member". The id stays in `member` for controls."""
    from . import customer as _customer
    try:
        name = _customer._member_prefs(con, member)[0].get("display_name")
    except sqlite3.Error:
        name = None
    if isinstance(name, str) and name.strip():
        return name.strip()
    return (email or "").split("@", 1)[0].strip() or "Member"


@_member_writer
def collab_invite_create(ctx, con, entry, member: str, role: str) -> dict:
    _access.require_customer_mutation(ctx, "collab_invite_create")
    if not _collaboration.check_permission(con, entry["row_namespace"],
                                           ctx.principal_id or "", "membership"):
        raise _access.AuthorizationError("owner manages membership", 403)
    return _collaboration.create_invite(con, entry["row_namespace"], member,
                                        role, ctx.principal_id or "hosted")


@_member_writer
def collab_invites(ctx, con, entry) -> dict:
    """Pending invitations of this workspace, for the roles that may invite.

    Pending means created, not accepted, superseded or withdrawn, and
    younger than INVITE_TTL (an expired invite is omitted). Delivery is
    the latest outbox state for the invite ("not_queued" when never
    queued). Invites carry no token; nothing secret is returned."""
    _access.require_customer_mutation(ctx, "collab_invites")
    if not _collaboration.check_permission(con, entry["row_namespace"],
                                           ctx.principal_id or "", "membership"):
        raise _access.AuthorizationError("owner manages membership", 403)
    rows = con.execute(
        "SELECT i.id, i.member, i.role, i.created_at, m.state AS delivery,"
        " CASE WHEN json_valid(m.detail) THEN"
        "   json_extract(m.detail, '$.unresolved') END AS unresolved"
        " FROM invites i LEFT JOIN notification_outbox m ON m.id ="
        " (SELECT o.id FROM notification_outbox o"
        "   WHERE o.workspace_id=i.workspace_id"
        "   AND CASE WHEN json_valid(o.detail) THEN"
        "     json_extract(o.detail, '$.kind')='workspace_invite'"
        "     AND json_extract(o.detail, '$.invite_id')=i.id END"
        "   ORDER BY o.id DESC LIMIT 1)"
        " WHERE i.workspace_id=? AND i.status='created'"
        " ORDER BY i.created_at, i.id", (entry["row_namespace"],)).fetchall()
    # unresolved: recovery marked the latest message as one that can never
    # be settled or re-sent (invites.md). The panel shows it from this
    # field only, never from elapsed time.
    return {"invites": [{"id": r["id"], "member": r["member"], "role": r["role"],
                         "created_at": r["created_at"],
                         "delivery": r["delivery"] or "not_queued",
                         "unresolved": r["delivery"] == "uncertain"
                         and r["unresolved"] == 1} for r in rows
                        if not _collaboration.invite_expired(r["created_at"])]}


@_member_writer
def collab_invite_deliver(ctx, con, entry, invite_id: str) -> dict:
    """Explicit gated invite delivery: enqueue into the notification
    outbox only. No send happens during membership writes; delivery is
    a separate explicit send action (attempt_delivery)."""
    _access.require_customer_mutation(ctx, "collab_invite_deliver")
    if not _collaboration.check_permission(con, entry["row_namespace"],
                                           ctx.principal_id or "", "membership"):
        raise _access.AuthorizationError("owner manages membership", 403)
    row = con.execute("SELECT * FROM invites WHERE id=?", (invite_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown invite")
    inv = dict(row)
    if inv.get("workspace_id") != entry["row_namespace"]:
        raise _access.AuthorizationError("invite is not in this workspace", 403)
    if inv.get("status") != "created":
        raise _ops.BadInput("invite is no longer pending")
    cur = con.execute("INSERT INTO notification_outbox (workspace_id, event_id,"
                      " recipient, state, detail, created_at)"
                      " VALUES (?, NULL, ?, 'pending', ?, ?)",
                      (entry["row_namespace"], inv["member"],
                       json.dumps({"kind": "workspace_invite",
                                   "invite_id": invite_id,
                                   "role": inv["role"]}),
                       _db.utcnow()))
    return {"invite_id": invite_id, "queued_outbox_id": cur.lastrowid,
            "sent": False}


@_member_writer
def collab_invite_send(ctx, con, entry, invite_id: str, mailer):
    """Explicit "Send invite" (L3), step 1: check and claim one invitation
    email. Returns an invites_send.PendingSend: when needs_transmit, the
    caller runs transmit() (the provider call only, thread-safe) and then
    finish(con, outcome); otherwise result is the answer.

    Validity and the sender's authority are checked at send time inside
    invites_send.prepare_send. con must not be in a transaction: the claim
    commits before the provider call. Status is queued, sent, failed or
    uncertain; nothing promises exactly-once delivery."""
    from . import invites_send as _invites_send
    _access.require_customer_mutation(ctx, "collab_invite_send")
    # A lapsed workspace sends no email. Only a caller who may manage
    # membership sees the billing refusal; anyone else still gets the 403
    # from prepare_send.
    if _collaboration.check_permission(con, entry["row_namespace"],
                                       ctx.principal_id or "", "membership"):
        _require_entitlement(con, entry, "collab_invite_send")
    return _invites_send.prepare_send(
        con, workspace_id=entry["row_namespace"],
        hosted_workspace_id=entry["workspace_id"], invite_id=invite_id,
        actor=ctx.principal_id or "", mailer=mailer)


def _invitee_invite(ctx, con, entry, invite_id: str, verified_email: str):
    """The invitation, when this browser session is its invitee, and
    whether it matched by account. Shared by accept and its read-only
    preflight, so both refuse exactly alike."""
    if ctx.credential_kind not in ("browser_session", "local_operator"):
        raise _access.AuthorizationError(
            "invitation acceptance requires a browser session", 403)
    if ctx.credential_kind == "browser_session" and (
            ctx.authority_kind != "bootstrap" or ctx.scopes != frozenset({"invite:accept"})):
        raise _access.AuthorizationError("exact invitation admission required", 403)
    principal = ctx.principal_id or ""
    row = con.execute("SELECT * FROM invites WHERE id=?", (invite_id,)).fetchone()
    inv = dict(row) if row is not None else {}
    # Uniform refusal: only the invitee (by account, or by verified email
    # for a named invitation) may learn anything about an invitation. A
    # made-up, foreign, consumed or someone else's invite all answer as an
    # unknown object.
    by_account = principal == inv.get("member")
    by_email = bool(verified_email) and _collaboration.identity_key(
        verified_email) == _collaboration.identity_key(inv.get("member"))
    if not inv or inv.get("workspace_id") != entry["row_namespace"] \
            or not (by_account or by_email):
        raise _ops.NotFound("unknown object")
    return inv, by_account


def collab_invite_preflight(ctx, con, entry, invite_id: str,
                            verified_email: str = "") -> dict:
    """Read-only entitlement check for the invite landing page (L25).

    Same refusal as accept: anyone but the invitee gets 404 "unknown
    object". The invitee learns whether accepting would go through now,
    and, only while it would, the workspace name and the role. Writes
    nothing; the accept POST stays the only way to accept."""
    inv, _by_account = _invitee_invite(ctx, con, entry, invite_id, verified_email)
    # The same identity accept checks, the session's verified email
    # included (L22b R2-01), so preflight never promises what accept refuses.
    refusal = _collaboration.acceptance_refusal(
        con, inv, ctx.principal_id or "",
        aliases=(verified_email,) if verified_email else ())
    if refusal == "invite has expired":
        return {"invitation": {"state": "expired"}}
    if refusal:
        return {"invitation": {"state": "not_pending"}}
    from . import customer as _customer
    name = _customer.workspace_display_name(con, entry["row_namespace"])
    return {"invitation": {"state": "pending", "role": inv["role"],
                           "workspace_name": name or entry["workspace_id"]}}


@_member_writer
def collab_invite_accept(ctx, con, entry, invite_id: str,
                         verified_email: str = "", *, control=None, now=None) -> dict:
    """Accept as the authenticated principal. A verified email may bind
    an explicit named invitation addressed to it; nothing else merges
    accounts or grants membership from a claim.

    verified_email must be the server-verified session email: adapters
    reject a client-supplied value before calling. Invitation acceptance
    uses operation-specific browser-session admission (K02): reader
    credentials cannot accept even for an owner/editor principal.
    """
    from . import db as _db
    principal = ctx.principal_id or ""
    inv, by_account = _invitee_invite(ctx, con, entry, invite_id, verified_email)
    # Pending, unexpired and newer than any removal or role change of this
    # account, of the invited address, or of an account the session's
    # verified email is tied to (collaboration's invitation rule; R2-01).
    # Re-read under the write lock and checked before binding, so a
    # refused acceptance binds nothing.
    with _db.transaction(con, write=True):
        current = con.execute("SELECT * FROM invites WHERE id=?",
                              (invite_id,)).fetchone()
        if current is None or current["member"] != inv["member"]:
            raise _ops.BadInput("invite is no longer pending")
        inv = dict(current)
        refusal = _collaboration.acceptance_refusal(
            con, inv, principal,
            aliases=(verified_email,) if verified_email else ())
        if refusal:
            raise _ops.BadInput(refusal)
        if by_account:
            audit_detail = {"workspace": inv["workspace_id"]}
            if verified_email:
                # Not bound (the members list shows bound emails only), but
                # part of the account's identity: a later removal or role
                # change supersedes invitations to this address too.
                audit_detail["verified_alias"] = verified_email
        else:
            con.execute("UPDATE invites SET member=? WHERE id=?",
                        (principal, invite_id))
            audit_detail = {"workspace": inv["workspace_id"],
                            "bound_email": verified_email}
        held = _collaboration.get_role(con, entry["row_namespace"], principal)
        out = _collaboration.accept_invite(
            con, invite_id, principal,
            aliases=(verified_email,) if verified_email else ())
        _db.audit(con, principal, "invite_accept_bound", "membership",
                  principal, audit_detail, _db.bump_revision(con))
    if control is not None and held != out["role"]:
        # A promotion or a re-admission ends any personal token still live
        # in control (AGT-R2-01), and records the revocation a failed
        # earlier control commit lost (AGT-SEC-R2-01).
        from . import connections as _conn
        _conn.revoke_member_tokens(
            control, workspace_id=ctx.workspace_id, member_id=principal,
            reason="member readmitted" if held is None else "role changed", now=now)
    return out


@_member_writer
def collab_invite_withdraw(ctx, con, entry, invite_id: str) -> dict:
    """Withdraw one pending invitation (owner). Refusals follow deliver:
    the permission check runs before any lookup."""
    _access.require_customer_mutation(ctx, "collab_invite_withdraw")
    if not _collaboration.check_permission(con, entry["row_namespace"],
                                           ctx.principal_id or "", "membership"):
        raise _access.AuthorizationError("owner manages membership", 403)
    return _collaboration.withdraw_invite(con, entry["row_namespace"], invite_id,
                                          ctx.principal_id or "hosted")


@_member_writer
def collab_role_change(ctx, con, entry, member: str, role: str,
                       expected_revision=None, *, control=None, now=None) -> dict:
    """With control (the request's control write transaction), the role
    decision also revokes the member's personal agent tokens for good. The
    workspace record alone already refuses them (AGT-SEC-R2-01); this
    wipes the hashes and writes the ledger."""
    _access.require_customer_mutation(ctx, "collab_role_change")
    if not _collaboration.check_permission(con, entry["row_namespace"],
                                           ctx.principal_id or "", "membership"):
        raise _access.AuthorizationError("owner manages membership", 403)
    out = _collaboration.set_role(con, entry["row_namespace"], member,
                                  role, ctx.principal_id or "hosted")
    if control is not None:
        from . import connections as _conn
        _conn.revoke_member_tokens(control, workspace_id=ctx.workspace_id,
                                   member_id=member, reason="role changed", now=now)
    return out


@_member_writer
def collab_member_remove(ctx, con, entry, member: str, *, control=None, now=None) -> dict:
    """With control, also revokes the member's personal agent tokens for good."""
    _access.require_customer_mutation(ctx, "collab_member_remove")
    if not _collaboration.check_permission(con, entry["row_namespace"],
                                           ctx.principal_id or "", "membership"):
        raise _access.AuthorizationError("owner manages membership", 403)
    out = _collaboration.revoke_member(con, entry["row_namespace"], member,
                                       ctx.principal_id or "hosted")
    if control is not None:
        from . import connections as _conn
        _conn.revoke_member_tokens(control, workspace_id=ctx.workspace_id,
                                   member_id=member, reason="member removed", now=now)
    return out


def _share_object_id(con, object_type: str, object_id) -> str:
    """The id a share stores. Settings > Shares tells users to paste the ID
    at the end of the item's page address, which for evidence and
    episodes is an `sr1:` publication ref; guest reads match the internal
    record id. Resolve the ref here, so the share a user makes by
    following the hint works. A ref that does not resolve, or names the
    other kind of record, is refused with a 400, never stored."""
    if not isinstance(object_id, str) or not object_id.startswith("sr1:"):
        return object_id
    noun = {"evidence": "an evidence item", "episode": "an episode"}.get(object_type)
    if noun is None:
        raise _ops.BadInput(
            f"an sr1: page-address ID cannot be shared as a {object_type}")
    try:
        state_id, _window = _citations._parse_source_ref(object_id)
    except _citations.CitationError:
        raise _ops.BadInput(
            "the sr1: ID is not a valid page-address ID") from None
    row = con.execute("SELECT kind, record_id FROM source_states WHERE id=?",
                      (state_id,)).fetchone()
    if row is None or row[0] != object_type:
        raise _ops.BadInput(f"the sr1: ID does not name {noun} in this workspace")
    return row[1]


@_member_writer
def collab_share_create(ctx, con, entry, object_type: str, object_id: str,
                        audience: str, fields: list, download: bool = False,
                        requested_expiry=None,
                        source_policy_expiry=None) -> dict:
    _access.require_customer_mutation(ctx, "collab_share_create")
    if not _collaboration.check_permission(con, entry["row_namespace"],
                                           ctx.principal_id or "", "sharing"):
        # Refused before the ref is looked up: no probing for refs.
        raise _access.AuthorizationError("sharing requires owner permission", 403)
    object_id = _share_object_id(con, object_type, object_id)
    return _collaboration.create_share(
        con, entry["row_namespace"], object_type, object_id, audience,
        fields or [], ctx.principal_id or "hosted", download,
        requested_expiry, source_policy_expiry)


def collab_share_state(ctx, con, entry, share_id: str) -> dict:
    ctx.require_member("collab_share_state")
    state = _collaboration.share_state(con, share_id)
    if state.get("workspace_id") != entry["row_namespace"]:
        raise _access.AuthorizationError("share is not in this workspace", 403)
    return {"share": state}


def _in_rows(con, sql: str, ids) -> list:
    """Rows for `sql` (one IN placeholder list) over ids, in bounded batches."""
    ids = sorted({str(i) for i in ids})
    out = []
    for start in range(0, len(ids), 500):
        batch = ids[start:start + 500]
        out.extend(con.execute(sql % ",".join("?" * len(batch)), batch).fetchall())
    return out


def _share_object_titles(con, ctx, rows) -> dict:
    """(object_type, object_id) -> readable title for the listed shares
    only: one bounded IN read per kind, never the whole episode table.
    Evidence/episode titles pass the same metadata check as
    permitted_episodes; anything unknown or withheld is ''."""
    from . import customer as _customer
    want = lambda kind: {r["object_id"] for r in rows if r["object_type"] == kind}
    titles: dict = {}
    try:
        ev_episode = {r["id"]: r["episode_id"] for r in _in_rows(
            con, "SELECT id, episode_id FROM evidence WHERE id IN (%s)", want("evidence"))}
        episode_ids = want("episode") | set(ev_episode.values())
        ep_title = {r["id"]: r["ep_title"] or "" for r in
                    _customer.permitted_episodes_by_id(con, ctx, episode_ids)}
        for ev_id, ep_id in ev_episode.items():
            titles[("evidence", ev_id)] = ep_title.get(ep_id, "")
        for ep_id in want("episode"):
            titles[("episode", ep_id)] = ep_title.get(ep_id, "")
        # Campaign names pass the same gate as every campaign read
        # (ops.list_campaigns): category permitted and this resource
        # allowed. Otherwise the title stays '' (a generic label).
        if ctx is None or ctx.permits("campaign"):
            for r in _in_rows(con, "SELECT id, name FROM campaigns WHERE id IN (%s)", want("campaign")):
                if _citations.resource_allowed(con, "campaign", r["id"], ctx):
                    titles[("campaign", str(r["id"]))] = r["name"] or ""
    except (sqlite3.Error, _access.AuthorizationError):
        return {}
    return titles


def collab_shares(ctx, con, entry) -> dict:
    ctx.require_member("collab_shares")
    rows = [dict(r) for r in con.execute(
        "SELECT id, object_type, object_id, audience, expires_at, revoked,"
        " revision FROM shares WHERE workspace_id=? ORDER BY id",
        (entry["row_namespace"],)).fetchall()]
    emails = _visible_member_emails(ctx, con, entry["row_namespace"])
    # Titles reveal content (episode and campaign names); a workspace that
    # is not entitled still lists its shares, without them.
    from . import billing as _billing
    try:
        _require_entitlement(con, entry, "collab_share_titles")
        titles = _share_object_titles(con, ctx, rows)
    except _billing.EntitlementRequired:
        titles = {}
    for row in rows:
        row["audience_email"] = emails.get(row["audience"], "")
        row["object_title"] = titles.get((row["object_type"], str(row["object_id"])), "")
    return {"shares": rows}


@_member_writer
def collab_share_revoke(ctx, con, entry, share_id: str,
                        expected_revision=None) -> dict:
    _access.require_customer_mutation(ctx, "collab_share_revoke")
    return _collaboration.revoke_share(con, share_id,
                                       ctx.principal_id or "hosted",
                                       expected_revision)


def shared_search(ctx, con, entry, filt: dict, limit: int = 50,
                  cursor: str | None = None) -> dict:
    """Share-guest evidence search with field projection; members are
    refused here (they use the member search path)."""
    if ctx.authority_kind != "share":
        raise _access.AuthorizationError("share authority required", 403)
    _require_entitlement(con, entry, "shared_search")
    return search_evidence(ctx, con, filt, limit, cursor)


def shared_evidence(ctx, con, entry, evidence_id: str) -> dict:
    if ctx.authority_kind != "share":
        raise _access.AuthorizationError("share authority required", 403)
    _require_entitlement(con, entry, "shared_evidence")
    try:
        return evidence_detail(ctx, con, evidence_id)
    except KeyError as exc:
        # A share naming an id that holds no evidence is unreadable, not a
        # server fault. Only evidence_detail's own missing-record error
        # (KeyError of exactly this id) maps; any other KeyError is a bug
        # and stays a logged 500.
        if exc.args != (evidence_id,):
            raise
        raise _ops.NotFound("unknown evidence") from None


def shared_transcript(ctx, con, entry, episode_id: str, offset: int = 0) -> dict:
    if ctx.authority_kind != "share":
        raise _access.AuthorizationError("share authority required", 403)
    _require_entitlement(con, entry, "shared_transcript")
    return transcript_window(ctx, con, episode_id, None, offset)


# -- Hosted billing (Task 13): Stripe adapters + server entitlement ------


class BillingConfigError(RuntimeError):
    """The billing settings are invalid. create_app refuses to start on
    them, so this is only reachable if the environment changed after start.
    The message is generic on purpose: it reaches customers as a 500."""


def _billing_config():
    from . import billing as _billing
    try:
        return _billing.config_from_env()
    except ValueError as exc:
        raise BillingConfigError("billing configuration is invalid") from exc


def _require_entitlement(con, entry, operation: str) -> None:
    """Server-side entitlement gate for one customer operation. Off unless
    billing configuration turns it on. When on, every operation is gated
    except the one explicit (UNAPPROVED) exempt list in the billing
    configuration, so a missing or misspelled name fails closed. A refusal
    is a named 402, never an empty success. Callers run it after their
    membership and role checks. A comped workspace (its exact external ID
    in EARWITNESS_BILLING_COMPED_WORKSPACES) is never refused."""
    from . import billing as _billing
    config = _billing_config()
    if not config.entitlement_required:
        return
    if str(entry.get("workspace_id") or "") in config.comped_workspaces:
        return
    if operation not in (config.ungated_operations or ()) \
            and entry.get("paid_use") is not True:
        # RB-R1-01: an evaluation-only (or unreadable) rights record is never
        # paid, whatever the stored entitlement says.
        raise _billing.EntitlementRequired(
            operation, entry.get("paid_use_reason") or _rights_unreadable(), "blocked")
    _billing.require_entitlement(con, entry["row_namespace"], operation, config,
                                 tenant=_billing_identity(entry))


def _billing_identity(entry) -> str:
    """The workspace's provider-facing billing identity (never the shared
    row namespace). '' when the entry has no verified identity."""
    from . import billing as _billing
    return _billing.workspace_billing_identity(
        str(entry.get("workspace_id") or ""), str(entry.get("db_identity") or ""))


def billing_status(ctx, con, entry, *, seat: bool = False) -> dict:
    """The Billing section's read (design 4.5, 4.7; BY1 review M1).

    - A comped workspace reports {"state": "comped", "allowed": true}: the
      gate never refuses it and there is nothing to buy.
    - `seat` is true when a purchase seat holds this workspace (the caller
      reads the control store); the page then offers "Manage billing".
    - `checkout` mirrors EARWITNESS_BILLING_INAPP_CHECKOUT, so the page
      hides the Upgrade form while in-app checkout is off.
    - `past_due` inside grace carries `grace_ends_at` (epoch seconds) for
      the banner."""
    import time as _time
    from . import billing as _billing
    ctx.require_member("billing_status")
    config = _billing_config()
    catalog = [{"price_id": pid,
                "amount_cents": (meta or {}).get("amount_cents"),
                "interval": (meta or {}).get("interval"),
                "trial_days": (meta or {}).get("trial_days", 0)}
               for pid, meta in (config.prices or {}).items()]
    comped = str(entry.get("workspace_id") or "") in config.comped_workspaces
    paid_use = entry.get("paid_use") is True
    if comped:
        entitlement = {"state": "comped", "allowed": True}
    elif not paid_use:
        # RB1: an evaluation-only (or unreadable) rights record is never
        # paid, whatever the stored entitlement says.
        entitlement = {"state": "blocked", "allowed": False,
                       "reason": entry.get("paid_use_reason") or _rights_unreadable()}
    else:
        now = int(_time.time())
        entitlement = _billing.entitlement_status(
            con, entry["row_namespace"], config, now=now,
            tenant=_billing_identity(entry))
        if entitlement.get("state") == "past_due" \
                and entitlement.get("grace_remaining_s") is not None:
            ends = now + int(entitlement["grace_remaining_s"])
            cancel_at = int(entitlement.get("cancel_at") or 0)
            entitlement["grace_ends_at"] = min(ends, cancel_at) if cancel_at else ends
    # Checkout and the portal refuse a workspace without paid use (RB1).
    return {"entitlement": entitlement, "configured": config.configured,
            "prices": catalog, "checkout": bool(config.inapp_checkout) and paid_use,
            "seat": bool(seat) and not comped and paid_use, "comped": comped}


def _rights_unreadable() -> str:
    from .rights_admission import PAID_USE_UNREADABLE
    return PAID_USE_UNREADABLE


def _workspace_customer_id(con, entry) -> str:
    """Server-recorded provider customer verified for this workspace's
    billing identity ('' when none). A binding or billing record that no
    delivery verified for it is refused, never guessed: an operator
    reconciles it (docs/hosting/billing.md)."""
    from . import billing as _billing
    try:
        return _billing.tenant_customer(con, entry["row_namespace"],
                                        _billing_identity(entry))
    except _billing.TenantMismatch as exc:
        raise _ops.BadInput("billing for this workspace is not verified;"
                            " an operator must reconcile it") from exc
    except (sqlite3.Error, ValueError):
        return ""


@_member_writer
def billing_checkout(ctx, con, entry, price_id: str, success_url: str = "",
                     cancel_url: str = "", trial_days: int = 0):
    """Role check and customer lookup run here, on the caller's thread
    (the sqlite connection is thread-bound). Returns the provider call,
    which touches no database, so the caller can run it off the loop."""
    import functools
    from . import billing as _billing
    _require_customer_role(con, entry, ctx, "organize", "billing_checkout")
    config = _billing_config()
    if not config.configured:
        raise _ops.BadInput("billing is not configured")
    identity = _billing_identity(entry)
    if not identity:
        raise _ops.BadInput("billing identity is unavailable for this workspace")
    customer_id = _workspace_customer_id(con, entry)
    success_url = success_url or config.success_url
    cancel_url = cancel_url or config.cancel_url
    # Caller input is validated before anything is reserved, so a bad
    # request can never consume the one trial.
    try:
        _, _, offered = _billing.validate_checkout(
            config, price_id=price_id, success_url=success_url,
            cancel_url=cancel_url, trial_days=trial_days or 0)
    except ValueError as exc:
        raise _ops.BadInput(str(exc)) from exc
    allow_trial, reservation_id = False, ""
    if offered:
        reservation = _billing.reserve_trial(
            con, entry["row_namespace"], customer_id, config)
        allow_trial = reservation["granted"]
        reservation_id = reservation["reservation_id"]
    return functools.partial(
        _billing.create_checkout_session, config,
        customer_id=customer_id or "", price_id=price_id,
        workspace_id=identity,
        success_url=success_url, cancel_url=cancel_url,
        trial_days=trial_days or 0, allow_trial=allow_trial,
        trial_reservation_id=reservation_id)


@_member_writer
def billing_portal(ctx, con, entry, return_url: str = ""):
    """Like billing_checkout: database reads here, provider call returned."""
    import functools
    from . import billing as _billing
    _require_customer_role(con, entry, ctx, "organize", "billing_portal")
    config = _billing_config()
    if not config.configured:
        raise _ops.BadInput("billing is not configured")
    customer_id = _workspace_customer_id(con, entry)
    if not customer_id:
        raise _ops.BadInput("no billing customer on file for this workspace")
    return functools.partial(
        _billing.create_portal_session, config, customer_id=customer_id,
        return_url=return_url or config.success_url)


def billing_webhook(config, con, entry, payload: bytes,
                    signature: str, provider=None) -> dict:
    """Server-side webhook delivery: verifies the live signature, then
    reconciles against the current provider resource. Production callers
    leave provider unset for live verification; tests inject the local
    protocol fixture. A delivery verified for the workspace's billing
    identity returns its journaled outcome dict, including refusals such as
    'customer_mismatch' or 'unpriced' (the route answers 200, so the
    provider does not retry). One not verified for that identity raises
    billing.TenantMismatch with nothing written (the route answers the
    unknown-id 404, so the provider retries)."""
    from . import billing as _billing
    return _billing.handle_webhook(config, con, entry["row_namespace"],
                                   payload, signature, provider=provider,
                                   tenant=_billing_identity(entry))
