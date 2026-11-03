"""Durable typed saved items and owned personal notes (K05).

Excerpt/query/bookmark are separate typed resources in one saved-item
store. Excerpts capture server text/bounds/provenance once; replacing
the source creates another item. Queries store canonical criteria plus
a validated live-definition selector (never counted refs or result
counts) and never create a tracker. Bookmarks pin actual show/creative
identities or episode pins with validated agreement. Source fields and
authority always come from the server, never client text.

Personal notes (B4) expose the existing owned-note store through an
authorized collection/detail resource. The retained exact-ref PUT finds
the existing owned note by its stored exact ref first: metadata edits
never re-resolve a missing source, while creation requires a valid
authorized pin.
"""
from __future__ import annotations

import base64
import json
import sqlite3
import uuid as _uuid

from earwitness.app import citations as _citations
from earwitness.app import customer as _customer
from earwitness.app import customer_contracts as _contracts
from earwitness.app import customer_query as _query
from earwitness.app import db as _db
from earwitness.app import ops as _ops
from earwitness.app import search as _search

SAVED_ITEM_TYPES = ("excerpt", "query", "bookmark")
TITLE_MAX = 120
NOTE_MAX = 2000
EXCERPT_MAX = 16000
LIST_LIMIT = 20
BOOKMARK_KINDS = ("show", "episode", "creative", "brand")
# A tracked brand whose profile no longer resolves for this reader (unknown,
# removed, or only "under review" now): its stored name is never shown.
BRAND_UNAVAILABLE_TITLE = "Tracked brand unavailable"
NOTE_STATUSES = ("open", "acknowledged", "resolved")


def _reject(message: str) -> _search.FilterRejected:
    return _search.FilterRejected(message)


def _check_title_note(title, note) -> tuple[str, str]:
    if not isinstance(title, str) or not title or len(title) > TITLE_MAX:
        raise _reject(f"title must be a nonempty string up to {TITLE_MAX} chars")
    if note is None:
        note = ""
    if not isinstance(note, str) or len(note) > NOTE_MAX:
        raise _reject(f"note must be a string up to {NOTE_MAX} chars")
    return title, note


def _check_seconds(value, name: str):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _reject(f"{name} must be a finite number")
    if value != value or value in (float("inf"), float("-inf")):
        raise _reject(f"{name} must be a finite number")
    if value < 0:
        raise _reject(f"{name} must be nonnegative")
    return float(value)


def _window_text(segments: list, start: float | None,
                 end: float | None) -> tuple[str, dict, dict]:
    """Select overlapping-segment text; returns (text, selected, basis)."""
    if start is None and end is None:
        full = "".join((s.get("text") or "") for s in segments
                        if isinstance(s, dict))
        return full, {"start_s": None, "end_s": None}, "full_effective_source"
    if start is None or end is None:
        raise _reject("start_s and end_s are paired; supply both or neither")
    if not end > start:
        raise _reject("end_s must exceed start_s")
    parts: list[str] = []
    lo = hi = None
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        s0, s1 = seg.get("start"), seg.get("end")
        if not isinstance(s0, (int, float)) or not isinstance(s1, (int, float)):
            continue
        if s1 <= start or s0 >= end:
            continue
        parts.append(seg.get("text") or "")
        lo = s0 if lo is None else min(lo, s0)
        hi = s1 if hi is None else max(hi, s1)
    text = "".join(parts)
    selected = {"start_s": start, "end_s": end}
    return text, selected, "overlapping_segments"


def _capture_excerpt(con: sqlite3.Connection, ref: str,
                     start: float | None, end: float | None, ctx=None) -> dict:
    window = _customer.record_window(con, ref, start, end, ctx=ctx)
    if window.get("disclosure") == "excerpt":
        return _capture_safe_excerpt(con, ref, window)
    if "window" not in window or "evidence" not in window:
        from .access import AuthorizationError
        raise AuthorizationError("source quotation fields are not disclosable", 403)
    resolved = window
    if not window["window"]["text_complete"]:
        raise _reject("selection exceeds the capture window; narrow it")
    text = " ".join(s["text"] for s in window["window"]["segments"])
    if not text:
        raise _reject("selected source has no quotation")
    if len(text) > EXCERPT_MAX:
        raise _reject(f"selection captures {len(text)} characters (max {EXCERPT_MAX}); narrow it")
    episode = resolved["episode"]
    evidence = resolved["evidence"]
    pins = evidence["pins"]
    return {"record_kind": resolved["record_kind"], "text": text,
            "source_ref": ref, "record_id": evidence["id"],
            "source_kind": "evidence" if resolved["record_kind"] == "evidence" else "episode",
            "episode_id": episode["episode_id"],
            "transcript_version_id": pins["transcript_version_id"],
            "current_transcript_version_id_at_save":
                _current_transcript_version(con, episode["episode_id"]),
            "original_bounds": resolved["original_bounds"],
            "effective_bounds": resolved["effective_bounds"],
            "selected_bounds": window["selected_bounds"],
            "bounds_basis": window["bounds_basis"],
            "labels": {k: episode.get(k) for k in ("show_title", "ep_title", "pub_date")},
            "fetch": {"fetch_date": _citations._cited_fetch_date(
                con, pins["transcript_version_id"], episode.get("fetch_date"))["fetch_date"]},
            "provenance": resolved.get("provenance")}


def _capture_safe_excerpt(con: sqlite3.Connection, ref: str, resolved: dict) -> dict:
    """Excerpt level (Plan A design 5): the saved bytes are the ad's safe
    excerpt; a client selection is ignored. The locators (pa1 on both
    sources) are added by the caller through captured_disclosure."""
    if resolved.get("record_kind") != "evidence":
        raise _reject("only an ad's excerpt can be saved on this plan")
    if not resolved.get("text"):
        raise _reject("Excerpt unavailable for this ad")
    episode = resolved["episode"]
    evidence = resolved["evidence"]
    tv = (evidence.get("pins") or {}).get("transcript_version_id")
    return {"record_kind": "evidence", "text": resolved["text"],
            "source_ref": ref, "record_id": evidence["id"], "source_kind": "evidence",
            "episode_id": episode["episode_id"], "transcript_version_id": tv,
            "current_transcript_version_id_at_save":
                _current_transcript_version(con, episode["episode_id"]),
            "original_bounds": resolved["original_bounds"],
            "effective_bounds": resolved["effective_bounds"],
            "selected_bounds": resolved["excerpt_bounds"],
            "bounds_basis": "pa1",
            "labels": {k: episode.get(k) for k in ("show_title", "ep_title", "pub_date")},
            "fetch": {"fetch_date": _citations._cited_fetch_date(
                con, tv, episode.get("fetch_date"))["fetch_date"]},
            "provenance": resolved.get("provenance")}


def _current_transcript_version(con: sqlite3.Connection, episode_id) -> int | None:
    row = con.execute("SELECT transcript_version_id FROM episodes WHERE id=?",
                      (episode_id,)).fetchone()
    return row[0] if row else None


# Availability reasons are shown to members as written: full sentences,
# naming the saved item ("Your excerpt keeps ...", else bookmark).
EARLIER_COPY_REASON = ("This read is in an earlier copy of the episode."
                       " Your {item} keeps the version you saw.")
NEWER_TRANSCRIPT_REASON = ("This episode has a newer transcript."
                           " Your {item} keeps the version you saw.")
NO_SOURCE_REASON = "The saved source can no longer be opened. No source text is substituted."


def _transcript_state(captured: dict, current, item: str) -> tuple[str, str | None]:
    """A saved transcript pin against the episode's current transcript:
    current; earlier_copy when it was already older at save (nothing
    changed after the save); else source_changed. Items saved before the
    at-save key existed cannot tell, and keep the changed wording."""
    if current == captured.get("transcript_version_id"):
        return "current", None
    if captured.get("current_transcript_version_id_at_save") == current:
        return "earlier_copy", EARLIER_COPY_REASON.format(item=item)
    return "source_changed", NEWER_TRANSCRIPT_REASON.format(item=item)


def _saved_availability(con: sqlite3.Connection, captured: dict,
                        source_ref: str | None, *,
                        item_type: str = "bookmark") -> tuple[str, str | None]:
    """Current / earlier_copy / retained_historical / source_changed /
    unavailable, with a full-sentence reason. Bookmarks and excerpts of
    a read follow one earlier-copy rule (_transcript_state)."""
    item = "excerpt" if item_type == "excerpt" else "bookmark"
    if not source_ref:
        return "unavailable", NO_SOURCE_REASON
    try:
        resolved = _customer.record(con, source_ref, include_text=False)
    except Exception:
        return "unavailable", NO_SOURCE_REASON
    if resolved.get("availability") == "source_removed":
        return "retained_historical", \
            "The source was removed. Your saved copy is kept."
    kind = captured.get("source_kind")
    record_id = captured.get("record_id") or ""
    episode_id = captured.get("episode_id") or ""
    if source_ref.startswith("ep:"):
        return "current", None
    try:
        # An episode pinned on a passage or on an evidence publication
        # (saved from Evidence) pins its transcript version: it stays
        # current until the episode's transcript moves on.
        pinned_elsewhere = kind == "episode" and source_ref.startswith("sr1:") \
            and resolved.get("record_kind") == "evidence"
        if source_ref.startswith("tx:") or pinned_elsewhere:
            row = con.execute(
                "SELECT transcript_version_id FROM episodes WHERE id=?",
                (episode_id,)).fetchone()
            if row is None:
                return "retained_historical", \
                    "This episode was removed. Your saved copy is kept."
            return _transcript_state(captured, row["transcript_version_id"], item)
        if kind and record_id:
            current = _citations.current_source_ref(con, kind, record_id)
            if current != (_citations._parse_source_ref(source_ref)[0]
                           if source_ref.startswith("sr1:") else source_ref):
                return "source_changed", \
                    "This record has a newer version. Your saved copy keeps the version you saw."
            # The record is current; an excerpt of a read also keeps its
            # transcript, as a bookmark of that read does. An excerpt saved
            # before the at-save key existed, or of a read with no
            # transcript version (unknown, never earlier), stays current.
            if kind == "evidence" and episode_id \
                    and captured.get("transcript_version_id") is not None \
                    and "current_transcript_version_id_at_save" in captured:
                row = con.execute("SELECT transcript_version_id FROM episodes WHERE id=?",
                                  (episode_id,)).fetchone()
                if row is not None:
                    return _transcript_state(captured, row["transcript_version_id"], item)
            return "current", None
    except _citations.RevisionUnavailable:
        return "retained_historical", \
            "This record has no current version. Your saved copy is kept."
    except sqlite3.Error:
        return "unavailable", \
            "The saved source could not be checked. No source text is substituted."
    return "current", None


def _collection_revision(con: sqlite3.Connection) -> int:
    try:
        return _db.aggregate_revision(con)
    except sqlite3.Error:
        return 0


def _encode_cursor(item_type: str | None, q: str, offset: int,
                   revision: int) -> str:
    raw = json.dumps({"t": item_type, "q": q, "o": offset, "r": revision},
                     sort_keys=True)
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str, item_type: str | None, q: str,
                   revision: int) -> int:
    try:
        payload = json.loads(base64.urlsafe_b64decode(
            cursor.encode()).decode())
    except (ValueError, TypeError):
        raise _reject("malformed saved-items cursor")
    if not isinstance(payload, dict) or payload.get("t") != item_type \
            or payload.get("q") != q or payload.get("r") != revision:
        raise _reject("saved collection changed; refresh the list")
    offset = payload.get("o")
    if not isinstance(offset, int) or offset < 0:
        raise _reject("malformed saved-items cursor")
    return offset


def _project_item(con: sqlite3.Connection, row: sqlite3.Row,
                  workspace_context: str | None, ctx=None, *,
                  brand_label: str | None = None) -> dict:
    """Live definitions/profiles need no source pin; retained source saves do.
    brand_label: a tracked brand's name the caller resolved for this reader
    in this same transaction (create_item), so it is not resolved again."""
    target = json.loads(row["target_json"] or "{}")
    from . import conversion
    if row["type"] == "query":
        conversion.require_executable(con, "typed_query", row["id"])
    query = json.loads(row["query_json"] or "{}")
    selector = json.loads(row["selector_json"]) \
        if row["selector_json"] else None
    captured = json.loads(row["captured_json"] or "{}")
    if ctx is not None and row["note"] and not ctx.permits("notes"):
        from .access import AuthorizationError
        raise AuthorizationError("saved note disclosure denied", 403)
    if captured:
        _citations.validate_capture(con, captured, ctx)
    item = {"id": row["id"], "type": row["type"], "title": row["title"],
            "note": row["note"], "revision": row["revision"],
            "saved_at": row["saved_at"], "updated_at": row["updated_at"],
            "target": target}
    if row["type"] == "query":
        item["query"] = query
        item["selector"] = selector
    if captured:
        item["captured"] = captured
    if row["type"] == "query":
        _contracts.normalize_query(query)
        if selector is not None:
            _contracts.validate_selector(
                selector, current_revision=None, workspace_context=None)
        availability, reason = "current", None
    elif row["type"] == "bookmark" and target.get("kind") == "brand":
        # Track a brand: the name always comes from the current brand page.
        # A brand that no longer resolves (decision 10/15: only "under
        # review" now) shows no stored name, key or label.
        label = brand_label if brand_label is not None else _brand_label(con, target, ctx)
        if label is None:
            availability = "unavailable"
            reason = "This brand is not available to you now."
            item["title"] = BRAND_UNAVAILABLE_TITLE
            item["target"] = {"kind": "brand", "id": None}
            item["captured"] = {k: v for k, v in captured.items()
                                if k not in ("label", "brand_key")}
        else:
            availability, reason = "current", None
            item["title"] = label
            item["captured"] = dict(captured, label=label)
    elif row["type"] == "bookmark" and captured.get("opens") == "current_profile":
        try:
            kind = target.get("kind")
            if kind not in BOOKMARK_KINDS:
                raise _reject("invalid bookmark kind")
            _capture_bookmark(con, kind, target, ctx)
        except (_ops.BadInput, _search.FilterRejected,
                _citations.RevisionUnavailable):
            availability, reason = "unavailable", "The saved page can no longer be opened."
        else:
            availability, reason = "current", None
    else:
        availability, reason = _saved_availability(
            con, captured, (captured.get("source_ref")
                            if isinstance(captured, dict) else None),
            item_type=row["type"])
    item["availability"] = availability
    if reason:
        item["availability_reason"] = reason
    if isinstance(captured, dict) and captured.get("source_ref"):
        try:
            current = _customer.record(con, captured["source_ref"], ctx=ctx)
            item["saved_target"] = {"ref": captured.get("source_ref"),
                                    "text": current.get("text")}
        except Exception:
            item["saved_target"] = {"ref": captured.get("source_ref")}
    return item


def create_item(con: sqlite3.Connection, body: dict | None, *,
                workspace_context: str | None,
                actor: str = "owner", ctx=None) -> dict:
    """Create one typed saved item; server captures every source field."""
    if not isinstance(body, dict):
        raise _reject("body must be an object")
    item_type = body.get("type", "")
    if item_type not in SAVED_ITEM_TYPES:
        raise _reject(f"invalid item type: {item_type!r}")
    target = body.get("target")
    tracks_brand = item_type == "bookmark" and isinstance(target, dict) \
        and target.get("kind") == "brand"
    if tracks_brand and "title" in body:
        raise _reject("a tracked brand is named by its brand page; send no title")
    title, note = _check_title_note(
        "brand" if tracks_brand else body.get("title", ""), body.get("note"))
    now = _db.utcnow()
    target_json: dict = {}
    query_json: dict = {}
    selector_json = None
    captured: dict = {}
    if item_type == "excerpt":
        if not isinstance(target, dict):
            raise _reject("excerpt target must be an object")
        ref = target.get("ref", "")
        if set(target) - {"ref", "start_s", "end_s"}:
            raise _reject("excerpt target holds ref/start_s/end_s only")
        if not isinstance(ref, str) or not ref:
            raise _reject("excerpt target needs a server-issued ref")
        start = _check_seconds(target.get("start_s"), "start_s")
        end = _check_seconds(target.get("end_s"), "end_s")
        if (start is None) != (end is None):
            raise _reject("start_s and end_s are paired; supply both or neither")
        if start is not None and not end > start:
            raise _reject("end_s must exceed start_s")
        ref = ref if ref.startswith("sr1:") else _citations.qualify_legacy_source_ref(con, ref)
        captured = _capture_excerpt(con, ref, start, end, ctx)
        target_json = {"ref": ref, "start_s": start, "end_s": end}
    elif item_type == "query":
        query = _contracts.normalize_query(body.get("query"))
        selector = None
        if body.get("selector") is not None:
            revision = _query._view_revision(con, ctx)
            checked = _contracts.validate_selector(
                body.get("selector"), current_revision=revision,
                workspace_context=workspace_context)
            selector = _contracts.live_selector(
                checked["base_query"], checked["clause"],
                checked["count_units"])
        query_json = dict(query, page=1)
        selector_json = json.dumps(selector, sort_keys=True) \
            if selector is not None else None
        captured = {}
    else:
        if not isinstance(target, dict):
            raise _reject("bookmark target must be an object")
        kind = target.get("kind", "")
        if kind not in BOOKMARK_KINDS:
            raise _reject(f"invalid bookmark kind: {kind!r}")
        target_json, captured = _capture_bookmark(con, kind, target, ctx)
        if tracks_brand:
            title = captured["label"]
            existing = con.execute(
                "SELECT * FROM customer_saved_items WHERE workspace_id='local'"
                " AND type='bookmark' AND target_json=? ORDER BY saved_at, id",
                (json.dumps(target_json, sort_keys=True),)).fetchone()
            if existing is not None:
                # Tracking twice is one item: return it, write nothing.
                return {"item": _project_item(con, existing, workspace_context, ctx,
                                              brand_label=title),
                        "collection_revision": _collection_revision(con),
                        "created": False}
    if captured:
        source_ref = captured.get("source_ref")
        captured["disclosure"] = _citations.captured_disclosure(con, source_ref, ctx) \
            if source_ref else captured.get("disclosure", [])
        captured["capture_digest"] = _citations.capture_digest(captured)
    item_id = "si-" + _uuid.uuid4().hex[:16]
    con.execute("INSERT INTO customer_saved_items (id, workspace_id, type,"
                " title, note, revision, target_json, query_json,"
                " selector_json, captured_json, saved_at, updated_at)"
                " VALUES (?, 'local', ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
                (item_id, item_type, title, note,
                 json.dumps(target_json, sort_keys=True),
                 json.dumps(query_json, sort_keys=True), selector_json,
                 json.dumps(captured, sort_keys=True), now, now))
    rev = _db.bump_revision(con)
    _db.audit(con, actor, "customer_item_save", "customer_saved_item",
              item_id, {"type": item_type}, rev)
    row = con.execute("SELECT * FROM customer_saved_items WHERE id=?",
                      (item_id,)).fetchone()
    # A tracked brand was just resolved for this reader in this transaction;
    # the insert changes no brand (PERF-HOL: the second resolve cost seconds).
    out = {"item": _project_item(con, row, workspace_context, ctx,
                                 brand_label=title if tracks_brand else None),
           "collection_revision": _collection_revision(con)}
    if tracks_brand:
        out["created"] = True
    return out


def _brand_label(con: sqlite3.Connection, target: dict, ctx=None) -> str | None:
    """The tracked brand's current display name, or None when its page
    does not resolve for this reader."""
    try:
        _target, captured = _capture_bookmark(con, "brand", target, ctx)
    except (_ops.BadInput, _search.FilterRejected,
            _citations.RevisionUnavailable):
        return None
    return captured["label"]


def _view_revision_for(con: sqlite3.Connection) -> str:
    from earwitness.app import search as _search_mod
    return _search_mod.projection_revision(
        con, {"scope": "research", "notes": False, "history": "active"})


def _capture_bookmark(con: sqlite3.Connection, kind: str,
                      target: dict, ctx=None) -> tuple[dict, dict]:
    if kind == "brand":
        # The brand page's own rule decides: a brand that exists only under
        # review (customer.brand_under_review) is no brand to this reader.
        if set(target) - {"kind", "id"}:
            raise _reject("brand bookmarks hold kind/id only")
        brand_id = target.get("id")
        if not isinstance(brand_id, str) or not brand_id.strip():
            raise _reject("brand bookmarks need a brand id")
        # Only the brand's key and name are stored, so only those are
        # resolved, never the whole brand page (PERF-HOL).
        try:
            # Model names off: a brand only Muse Spark names is no brand
            # here, and its name is never copied into a saved label.
            with _customer.model_names(False):
                detail = _customer.brand_identity(con, brand_id, ctx=ctx)
        except (_search.FilterRejected, _ops.BadInput):
            raise _ops.BadInput("unknown brand")
        key = detail["key"]
        return ({"kind": kind, "id": key},
                {"record_kind": "brand", "brand_key": key,
                 "label": detail.get("display") or key,
                 "opens": "current_profile"})
    if kind == "show":
        show_id = target.get("id", "")
        if set(target) - {"kind", "id"}:
            raise _reject("show bookmarks hold kind/id only")
        row = con.execute(
            "SELECT id, display_title FROM customer_show_identities"
            " WHERE id=?", (show_id,)).fetchone()
        if row is None:
            raise _ops.BadInput(f"unknown show: {show_id!r}")
        episodes = [ep for ep in _customer.permitted_episodes(con, ctx)
                    if ep["id"] in _customer.resolve_customer_show(con, show_id)]
        if not episodes:
            raise _ops.NotFound("show unavailable")
        version = _citations._version_provenance(con, episodes[0]["transcript_version_id"])
        locator = {"source_key": version["source_key"] if version else f"episode:{episodes[0]['id']}",
                   "fields": ["metadata"]}
        return ({"kind": kind, "id": show_id},
                {"record_kind": "show", "show_id": show_id,
                 "label": row["display_title"] or show_id, "disclosure": [locator],
                 "opens": "current_profile"})
    if kind == "creative":
        creative_id = target.get("id", "")
        if set(target) - {"kind", "id"}:
            raise _reject("creative bookmarks hold kind/id only")
        try:
            detail = _customer.creative_detail(con, creative_id, ctx=ctx)
        except (_search.FilterRejected, _ops.BadInput) as exc:
            raise _ops.BadInput(f"unknown creative: {exc}")
        return ({"kind": kind, "id": creative_id},
                {"record_kind": "creative", "creative_id": creative_id,
                 "label": (detail.get("creative") or {}).get("id")
                 or creative_id,
                 "opens": "current_profile"})
    episode_id = target.get("id", "")
    if not isinstance(episode_id, str) or not episode_id:
        raise _reject("episode bookmarks need an episode id")
    tv = target.get("transcript_version_id")
    source_ref = target.get("source_ref")
    if set(target) - {"kind", "id", "transcript_version_id", "source_ref"}:
        raise _reject("episode bookmarks hold kind/id/transcript_version_id"
                      "/source_ref only")
    if tv is None and source_ref is None:
        ep = con.execute("SELECT * FROM episodes WHERE id=?",
                         (episode_id,)).fetchone()
        if ep is None:
            raise _ops.BadInput(f"unknown episode: {episode_id!r}")
        ep = dict(ep)
        version = _citations._version_provenance(con, ep["transcript_version_id"])
        locator = {"source_key": version["source_key"] if version else f"episode:{episode_id}", "fields": ["metadata"]}
        _citations.authorize_retained(con, {"disclosure": [locator]}, ctx)
        return ({"kind": kind, "id": episode_id,
                 "transcript_version_id": None, "source_ref": None},
                {"record_kind": "episode", "episode_id": episode_id,
                 "label": ep.get("ep_title") or episode_id, "disclosure": [locator],
                 "show_title": ep.get("show_title"),
                 "opens": "current_profile"})
    if not isinstance(tv, int) or isinstance(tv, bool) or tv < 1:
        raise _reject("pinned episodes need an integer transcript_version_id")
    if not isinstance(source_ref, str) or not source_ref:
        raise _reject("pinned episodes need the displayed source_ref")
    try:
        resolved = _customer.record(con, source_ref, ctx=ctx, include_text=False)
    except (_search.FilterRejected, _ops.BadInput,
            _citations.CitationError,
            _citations.RevisionUnavailable) as exc:
        raise _ops.BadInput(f"unresolvable bookmark pin: {exc}")
    rk = resolved.get("record_kind")
    if resolved.get("canonical_source_ref"):
        state_id, _interval = _citations._parse_source_ref(source_ref)
        if state_id is None:
            raise _ops.BadInput("bookmark pin is not a publication")
        state_row = con.execute(
            "SELECT kind, record_id, pins_json FROM source_states"
            " WHERE id=?", (state_id,)).fetchone()
        if state_row is None:
            raise _ops.BadInput("saved publication is not retained")
        if state_row["kind"] == "episode":
            got_episode = state_row["record_id"] or ""
        else:
            got_episode = (resolved.get("episode") or {}).get("episode_id") \
                or ""
        try:
            pins = json.loads(state_row["pins_json"] or "{}")
        except (ValueError, TypeError):
            pins = {}
        got_tv = pins.get("transcript_version_id")
    elif rk == "evidence":
        ev = resolved.get("evidence") or {}
        got_episode = ev.get("episode_id") or ""
        got_tv = ev.get("transcript_version_id")
    elif rk == "transcript_passage":
        import re as _re2
        got_episode = (resolved.get("episode") or {}).get("id") or ""
        m2 = _re2.fullmatch(r"tx:(.+)@v(\d+):(\d+)", source_ref or "")
        got_tv = int(m2.group(2)) if m2 else None
    elif rk == "episode":
        got_episode = (resolved.get("episode") or {}).get("id") or ""
        got_tv = (resolved.get("episode") or {}).get("transcript_version_id")
    else:
        raise _ops.BadInput("bookmark pin is not an episode pin")
    if got_episode != episode_id or got_tv != tv:
        raise _ops.BadInput("bookmark pin disagrees with the episode pin")
    # What was saved, in words: the pinned episode's title and its show's
    # display title (the stored show identity first, else the feed title).
    episode = resolved.get("episode") or {}
    show = resolved.get("show") or {}
    return ({"kind": kind, "id": episode_id, "transcript_version_id": tv,
             "source_ref": source_ref},
            {"record_kind": "episode", "episode_id": episode_id,
             "transcript_version_id": tv, "source_ref": source_ref,
             "record_id": episode_id, "source_kind": "episode",
             "label": episode.get("ep_title") or episode_id,
             "show_title": show.get("display_title") or episode.get("show_title"),
             "current_transcript_version_id_at_save":
                 _current_transcript_version(con, episode_id),
             "opens": "captured_pin"})


def get_item(con: sqlite3.Connection, item_id: str, *,
             workspace_context: str | None = None, ctx=None) -> dict:
    row = con.execute("SELECT * FROM customer_saved_items"
                      " WHERE id=? AND workspace_id='local'",
                      (item_id,)).fetchone()
    if row is None:
        raise _ops.NotFound("unknown saved item")
    return {"item": _project_item(con, row, workspace_context, ctx),
            "collection_revision": _collection_revision(con)}


def list_items(con, *, item_type=None, q="", cursor=None, limit=LIST_LIMIT,
               workspace_context=None, ctx=None):
    if item_type is not None and item_type not in SAVED_ITEM_TYPES:
        raise _reject("invalid item type")
    if not isinstance(q, str) or isinstance(limit, bool) or not 1 <= limit <= LIST_LIMIT:
        raise _reject("invalid saved collection filter or limit")
    # READ-CACHE: the projected collection is the same for every caller of
    # one disclosure class (saved items are workspace-wide: no owner
    # column, no principal filter), so it is remembered per data state and
    # class. The revision, the filter, the page and the cursor carry the
    # caller's own workspace_context and are computed here, per call.
    from . import customer_query
    projection = customer_query.class_memo(con, ctx, ("saved-projection",),
                                           lambda c: _saved_projection(con, c))
    permitted = projection["permitted"]
    counts, plan_withheld = projection["counts"], projection["plan_withheld"]
    revision = _citations._state_digest("saved", workspace_context or "", permitted)
    needle = q.strip().casefold()
    filtered = [i for i in permitted if (item_type is None or i["type"] == item_type)
                and (not needle or needle in " ".join(
                    (i["title"], i["note"], (i.get("captured") or {}).get("text", ""))).casefold())]
    offset = _decode_cursor(cursor, item_type, q, revision) if cursor else 0
    page = filtered[offset:offset + limit]
    more = offset + limit < len(filtered)
    return {"items": page, "total": len(filtered), "total_relation": "exact",
            "counts": counts, "collection_revision": revision,
            "plan_withheld": plan_withheld,
            "rights_withheld": projection["rights_withheld"],
            # The plan signal, as on the research query: a Saved page
            # opened first knows it reads at excerpt level (Plan A).
            "excerpt_only": projection["excerpt_only"],
            "next_cursor": _encode_cursor(item_type, q, offset + limit, revision) if more else None}


def _saved_projection(con, ctx=None) -> dict:
    """Every saved item ctx may see, projected, with the counts, the Plan A
    withheld count and the plan signal: list_items' class-level part.
    Projection reads no workspace_context."""
    from .access import AuthorizationError
    permitted = []
    # Items refused only by the Plan A old-artifact rule (design 6; the
    # predicate the report list uses) are counted, never described: no
    # id, title or text. DENY-WORDS (FDV-R1-02/-03): so are items a
    # source's own policy refuses, apart, unless the plan also refuses
    # them (an old artifact): those stay in the plan count, so each item
    # counts once; an old artifact a grant's source list leaves out counts
    # there too (DPC-05/-07). Any other refusal (a grant's refusal of a
    # current item included) is dropped, uncounted (citations.withheld_reason).
    plan_withheld = rights_withheld = 0
    for row in con.execute("SELECT * FROM customer_saved_items WHERE workspace_id='local'"
                           " ORDER BY saved_at, id"):
        try:
            permitted.append(_project_item(con, row, None, ctx))
        except AuthorizationError as exc:
            captured = json.loads(row["captured_json"] or "{}")
            reason = _citations.withheld_reason(con, exc, captured.get("disclosure") or [], ctx)
            if reason == "plan":
                plan_withheld += 1
            elif reason == "rights":
                rights_withheld += 1
        except _citations.RevisionUnavailable:
            continue
    return {"permitted": permitted,
            "counts": {t: sum(i["type"] == t for i in permitted) for t in SAVED_ITEM_TYPES},
            "plan_withheld": plan_withheld, "rights_withheld": rights_withheld,
            "excerpt_only": _customer.excerpt_workspace(con, ctx)}


def update_item(con: sqlite3.Connection, item_id: str, body: dict | None,
                expected_revision, *, actor: str = "owner", ctx=None) -> dict:
    """Edit title/note and, for queries, the displayed definition.

    Replacing an excerpt source or bounds creates a new saved item;
    it never rewrites the original captured quotation.
    """
    if not isinstance(body, dict):
        raise _reject("body must be an object")
    if expected_revision is None:
        raise _ops.MissingPrecondition("expected revision is required")
    row = con.execute("SELECT * FROM customer_saved_items"
                      " WHERE id=? AND workspace_id='local'",
                      (item_id,)).fetchone()
    if row is None:
        raise _ops.NotFound("unknown saved item")
    if int(expected_revision) != row["revision"]:
        raise _ops.Conflict(row["revision"])
    from . import conversion
    if row["type"] == "query":
        conversion.require_executable(con, "typed_query", item_id)
    stored_target = json.loads(row["target_json"] or "{}")
    stored_query = json.loads(row["query_json"] or "{}")
    stored_selector = json.loads(row["selector_json"]) \
        if row["selector_json"] else None
    title, note = _check_title_note(body.get("title", row["title"]),
                                    body.get("note", row["note"]))
    target_json, query_json, selector_json, captured = (
        stored_target, stored_query, row["selector_json"],
        json.loads(row["captured_json"] or "{}"))
    if row["type"] == "excerpt":
        if body.get("target") is not None and body.get("target") != stored_target:
            raise _ops.BadInput("replacing an excerpt source creates a new"
                                " saved item; this one is unchanged")
    elif row["type"] == "query":
        if body.get("target") is not None:
            raise _reject("query items carry no target")
        if body.get("query") is not None:
            query = _contracts.normalize_query(body.get("query"))
            query_json = dict(query, page=1)
        if body.get("selector") is not None:
            revision = _query._view_revision(con, ctx)
            checked = _contracts.validate_selector(
                body.get("selector"), current_revision=revision,
                workspace_context=body.get("workspace_context"))
            stored_selector = _contracts.live_selector(
                checked["base_query"], checked["clause"],
                checked["count_units"])
            selector_json = json.dumps(stored_selector, sort_keys=True)
    else:
        if body.get("target") is not None and body.get("target") != stored_target:
            raise _ops.BadInput("replacing a bookmark target creates a new"
                                " saved item; this one is unchanged")
        if body.get("query") is not None or body.get("selector") is not None:
            raise _reject("bookmark items carry no query")
        if stored_target.get("kind") == "brand":
            # The name comes from the brand page; only the note is editable.
            shown = _brand_label(con, stored_target, ctx) or BRAND_UNAVAILABLE_TITLE
            if "title" in body and body["title"] not in (row["title"], shown):
                raise _reject("a tracked brand is named by its brand page")
            title = row["title"]
    now = _db.utcnow()
    revision = row["revision"] + 1
    con.execute("UPDATE customer_saved_items SET title=?, note=?,"
                " revision=?, target_json=?, query_json=?,"
                " selector_json=?, updated_at=? WHERE id=?",
                (title, note, revision,
                 json.dumps(target_json, sort_keys=True),
                 json.dumps(query_json, sort_keys=True), selector_json,
                 now, item_id))
    _db.bump_revision(con)
    _db.audit(con, actor, "customer_item_edit", "customer_saved_item",
              item_id, {"revision": revision}, _collection_revision(con))
    fresh = con.execute("SELECT * FROM customer_saved_items WHERE id=?",
                        (item_id,)).fetchone()
    return {"item": _project_item(
        con, fresh, body.get("workspace_context"), ctx),
        "collection_revision": _collection_revision(con)}


def delete_item(con: sqlite3.Connection, item_id: str,
                expected_revision, *, actor: str = "owner") -> dict:
    """Remove the saved item only; source evidence is never touched."""
    if expected_revision is None:
        raise _ops.MissingPrecondition("expected revision is required")
    row = con.execute("SELECT revision FROM customer_saved_items"
                      " WHERE id=? AND workspace_id='local'",
                      (item_id,)).fetchone()
    if row is None:
        raise _ops.NotFound("unknown saved item")
    if int(expected_revision) != row["revision"]:
        raise _ops.Conflict(row["revision"])
    con.execute("DELETE FROM customer_saved_items"
                " WHERE id=? AND workspace_id='local'", (item_id,))
    rev = _db.bump_revision(con)
    _db.audit(con, actor, "customer_item_delete", "customer_saved_item",
              item_id, {}, rev)
    return {"removed_id": item_id, "collection_revision": rev}


# -- Owned personal notes (B4 collection/detail + retained exact-ref PUT) ----

def _review_projection(con: sqlite3.Connection, ref: str) -> dict:
    """Read-only reviewer state for a note ref; never editable here."""
    ev_id = None
    m = __import__("re").fullmatch(r"ev:(.+)@v\d+", ref or "")
    if m:
        ev_id = m.group(1)
    elif (ref or "").startswith("sr1:"):
        try:
            state_id = ref.split(":")[1]
            row = con.execute("SELECT kind, record_id FROM source_states"
                              " WHERE id=?", (state_id,)).fetchone()
            if row is not None and row["kind"] == "evidence":
                ev_id = row["record_id"]
        except sqlite3.Error:
            ev_id = None
    if ev_id is None:
        return {"review_status": None, "reviewer": None}
    try:
        row = con.execute("SELECT judgment FROM reviews WHERE evidence_id=?",
                          (ev_id,)).fetchone()
    except sqlite3.Error:
        return {"review_status": None, "reviewer": None}
    return {"review_status": row["judgment"] if row else None,
            "reviewer": None}


def _project_note(con, row, ctx=None):
    from .access import AuthorizationError
    if ctx is not None and (not ctx.permits("notes") or
            row["created_by"] != ctx.principal_id and not (
                ctx.credential_kind == "local_operator" and not row["created_by"])):
        raise AuthorizationError("personal note unavailable", 404)
    note = {"id": row["id"], "reason": row["reason"], "note_status": row["status"],
            "revision": row["revision"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], "delivery_context": "local_only"}
    try:
        _citations.captured_disclosure(con, row["ref"], ctx)
    except (AuthorizationError, _citations.CitationError):
        return note
    note["ref"] = row["ref"]
    note.update(_review_projection(con, row["ref"]))
    label = _note_source_label(con, row["ref"], ctx)
    if label is not None:
        note["source_label"] = label
    return note


@_customer.model_names(False)  # PODS: labels never carry a Muse Spark name (no mark there)
def _note_source_label(con, ref, ctx=None) -> dict | None:
    """What a note belongs to, in words: show, episode, date, time and the
    read's brand. A brand under review (or hidden from this reader) is
    never named: brand_display stays None (hard rule 2)."""
    from . import ad_reads as _ad
    try:
        rec = _customer.record(con, ref, ctx=ctx, include_text=False)
    except Exception:
        return None
    if not rec.get("canonical_source_ref") and rec.get("disclosure") != "excerpt":
        return None  # restricted projection: identifiers only
    episode = rec.get("episode") or {}
    show = rec.get("show") or {}
    bounds = rec.get("effective_bounds") or rec.get("original_bounds") or {}
    label = {"record_kind": rec.get("record_kind"),
             "show_title": show.get("display_title") or episode.get("show_title"),
             "ep_title": episode.get("ep_title"), "pub_date": episode.get("pub_date"),
             "span": [bounds.get("start_s"), bounds.get("end_s")],
             "brand_display": None, "brand_confirmed": False,
             "brand_under_review": False, "needs_review": False}
    if rec.get("record_kind") != "evidence":
        return label
    ev_id = (rec.get("evidence") or {}).get("id")
    read = rec.get("read") or {}
    if read.get("historical") and ev_id:
        # A historical reference shows no read block; the current read's
        # brand under the same customer rule.
        read = _customer.read_detail(con, ev_id, customer_view=True) or {}
        read = _customer.customer_read_block(read) if _ad.brand_review_ids(con, [ev_id]) \
            else _customer.customer_read_view(read)
    brand = read.get("brand") or {}
    confirmed = bool(brand.get("confirmed"))
    under_review = bool(brand.get("under_review")) or (
        not confirmed and bool(ev_id) and bool(_ad.brand_review_ids(con, [ev_id], unconfirmed=False)))
    label.update(brand_confirmed=confirmed, brand_under_review=under_review,
                 needs_review=bool(read.get("needs_review")),
                 brand_display=None if under_review else (brand.get("display") or None))
    return label


def list_notes(con, *, q="", ref=None, cursor=None, limit=LIST_LIMIT, ctx=None):
    if not isinstance(q, str) or isinstance(limit, bool) or not 1 <= limit <= LIST_LIMIT:
        raise _reject("invalid note filter or limit")
    from .access import AuthorizationError
    permitted = []
    for row in con.execute("SELECT * FROM customer_mistake_reports WHERE workspace_id='local' ORDER BY id"):
        try:
            permitted.append(_project_note(con, row, ctx))
        except AuthorizationError:
            continue
    revision = _citations._state_digest("notes", getattr(ctx, "policy_identity", ""), permitted)
    needle = q.strip().casefold()
    items = [n for n in permitted if (ref is None or n.get("ref") == ref)
             and (not needle or needle in n["reason"].casefold())]
    offset = _decode_cursor(cursor, ref or "", q, revision) if cursor else 0
    more = len(items) > offset + limit
    return {"notes": items[offset:offset + limit], "total": len(items),
            "total_relation": "exact", "collection_revision": revision,
            "next_cursor": _encode_cursor(ref or "", q, offset + limit, revision) if more else None}


def get_note(con: sqlite3.Connection, note_id: int, ctx=None) -> dict:
    try:
        note_id = int(note_id)
    except (TypeError, ValueError):
        raise _ops.BadInput("unknown note")
    row = con.execute("SELECT * FROM customer_mistake_reports"
                      " WHERE id=? AND workspace_id='local'",
                      (note_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown note")
    note = _project_note(con, row, ctx)
    return {"note": note, "collection_revision": _citations._state_digest(
        "note", getattr(ctx, "policy_identity", ""), note)}


@_db.atomic
def save_note(con: sqlite3.Connection, ref: str | None, reason: str,
              status: str = "open", mistake_id=None,
              expected_revision=None, actor: str = "owner", ctx=None) -> dict:
    """Creation requires an authorized pin; identified metadata edits do not.

    After ownership admission, an omitted ref binds the identified note's
    existing stored ref without resolving its source. Explicit refs still
    cannot move notes. Review, bounds, attribution and report bytes are untouched.
    """
    if not isinstance(ref, str) or not ref:
        if ref is not None or mistake_id is None:
            raise _reject("ref must be a nonempty string")
    if not isinstance(reason, str) or not reason.strip():
        raise _reject("reason must be a nonempty string")
    if ctx is not None and not ctx.permits("notes"):
        from .access import AuthorizationError
        raise AuthorizationError("personal note metadata denied", 403)
    if status not in NOTE_STATUSES:
        raise _reject(f"invalid note status: {status!r}")
    now = _db.utcnow()
    if mistake_id is None:
        existing = con.execute(
            "SELECT id, revision FROM customer_mistake_reports"
            " WHERE workspace_id='local' AND ref=? AND created_by=? ORDER BY id",
            (ref, actor)).fetchone()
        if existing is not None:
            mistake_id = existing["id"]
    if mistake_id is None:
        try:
            if not ref.startswith("sr1:"):
                ref = _citations.qualify_legacy_source_ref(con, ref)
            _customer.record(con, ref, ctx=ctx)
            _citations.require_window_in_bounds(con, ref)
        except (_search.FilterRejected, _ops.BadInput,
                _citations.CitationError,
                _citations.RevisionUnavailable) as exc:
            raise _ops.BadInput(f"note needs a valid authorized pin: {exc}")
        cur = con.execute(
            "INSERT INTO customer_mistake_reports (workspace_id, ref, reason,"
            " status, revision, created_at, updated_at, created_by)"
            " VALUES ('local', ?, ?, ?, 1, ?, ?, ?)",
            (ref, reason.strip()[:2000], status, now, now, actor))
        mistake_id = cur.lastrowid
        revision = 1
    else:
        if expected_revision is None:
            raise _ops.MissingPrecondition("expected revision is required")
        row = con.execute("SELECT * FROM customer_mistake_reports"
                          " WHERE id=? AND workspace_id='local'",
                          (mistake_id,)).fetchone()
        if row is None:
            raise _ops.BadInput("unknown note")
        if ctx is not None and row["created_by"] != ctx.principal_id and not (
                ctx.credential_kind == "local_operator" and not row["created_by"]):
            from .access import AuthorizationError
            raise AuthorizationError("personal note unavailable", 404)
        if ref is None:
            ref = row["ref"]
        if row["ref"] != ref:
            raise _ops.BadInput("note reference is pinned;"
                                " cannot move a note to new text")
        if int(expected_revision) != row["revision"]:
            raise _ops.Conflict(row["revision"])
        revision = row["revision"] + 1
        con.execute("UPDATE customer_mistake_reports SET reason=?, status=?,"
                    " revision=?, updated_at=? WHERE id=?",
                    (reason.strip()[:2000], status, revision, now,
                     mistake_id))
    rev = _db.bump_revision(con)
    _db.audit(con, actor, "customer_mistake_save", "customer_mistake_report",
              str(mistake_id), {"ref": ref}, rev)
    return {"id": mistake_id, "revision": revision, "aggregate_rev": rev}
