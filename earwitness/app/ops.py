"""Application operations: filters, reviews, watchlists, campaigns, exports."""
from __future__ import annotations

import csv
import html
import io
import json
import math
import sqlite3
import uuid

from . import citations as _citations
from . import db
from . import search as _search

FILTER_VERSION = 1
PER_PAGE_DEFAULT = 50
PER_PAGE_MAX = 200

REVIEW_STATES = {"ad", "not_ad", "unsure"}
BRAND_BASES = {"confirmed", "incidental", "unknown", "suggested"}
CHECK_TYPES = {"confirmed_brand", "literal_phrase", "duration_range", "position_range"}


class Conflict(Exception):
    def __init__(self, current_revision: int):
        super().__init__("stale revision")
        self.current_revision = current_revision


class EpisodeReviewConflict(Conflict):
    """A stale revision, or an undo with no decision, on a judge's episode
    review (FUNC-2 Addendum A): a Conflict that adapters map to 409, where
    other stale revisions map to 412."""


class MissingPrecondition(Exception):
    pass


class BadInput(Exception):
    pass


class NotFound(BadInput):
    """An unknown single entity on a read path (K09 ruling).

    Subclasses BadInput so existing domain catches keep working;
    adapters map it to 404 NOT_FOUND so unknown-entity reads share
    one mapping on both transports.
    """


class Archived(BadInput):
    """A write on an archived campaign (UAT-100). Only reads, stored
    reports and Restore are allowed; adapters map this to 409
    CAMPAIGN_ARCHIVED on both transports."""


class NeedsCorrection(BadInput):
    """A saved filter cannot be evaluated safely and needs owner repair.

    Raised instead of broadly evaluating a definition whose restrictive
    meaning cannot be established. Carries the decoder's reasons so callers
    (browser/API/agent) can show exactly what to fix.
    """

    def __init__(self, reasons):
        reasons = list(reasons) or ["unconvertible saved filter"]
        super().__init__("saved filter needs_correction: " + "; ".join(reasons))
        self.reasons = reasons


def _finite(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise BadInput(f"{name} must be a finite number")
    return float(value)


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def normalize_filter(raw: dict | None) -> dict:
    if not isinstance(raw, dict):
        raise BadInput("filter must be an object")
    per = raw.get("per_page", PER_PAGE_DEFAULT)
    try:
        per = int(per)
    except (TypeError, ValueError):
        per = PER_PAGE_DEFAULT
    per = max(1, min(PER_PAGE_MAX, per))
    try:
        page = max(1, int(raw.get("page", 1)))
    except (TypeError, ValueError):
        page = 1

    def strlist(key):
        val = raw.get(key, [])
        if isinstance(val, str):
            val = [val]
        if not isinstance(val, list):
            raise BadInput(f"{key} must be a string or list")
        return [str(v) for v in val if isinstance(v, (str, int, float))][:50]

    filt = {
        "version": FILTER_VERSION,
        "q": str(raw.get("q", ""))[:200],
        "source": [s for s in strlist("source") if s in ("jev", "detector", "manual", "pipeline")],
        "review": [s for s in strlist("review")
                   if s in ("unreviewed", "ad", "not_ad", "unsure")],
        "brand": str(raw.get("brand", ""))[:120],
        "brand_basis": [s for s in strlist("brand_basis") if s in BRAND_BASES],
        "brand_channel": [s for s in strlist("brand_channel")
                          if s in ("spoken", "url", "code", "unspecified")],
        "show": str(raw.get("show", ""))[:200],
        "category": str(raw.get("category", ""))[:80],
        "source_version": str(raw.get("source_version", ""))[:30],
        "history": str(raw.get("history", "active")) if raw.get("history") in ("active", "all") else "active",
        "episode": str(raw.get("episode", ""))[:80],
        "pub_from": str(raw.get("pub_from", ""))[:10],
        "pub_to": str(raw.get("pub_to", ""))[:10],
        "include_unknown_dates": raw.get("include_unknown_dates", True) not in (False, "false", "0", 0),
        "sort": raw.get("sort") if raw.get("sort") in ("episode", "start", "brand", "published") else "episode",
        "page": page,
        "per_page": per,
    }
    return filt


HIDDEN_SHOWS_META_KEY = "hidden_shows"


def get_hidden_shows(con: sqlite3.Connection) -> dict:
    """All show-hide records keyed by exact show_title.

    Stored in the workspace meta table (no schema change): each value is
    ``{"hidden": bool, "revision": int, "updated_at": str}``. Unhide keeps
    the record with ``hidden: False`` so per-show revision conflicts stay
    meaningful across hide/unhide cycles. Raw evidence/episode rows are
    never touched by hiding.
    """
    row = con.execute("SELECT value FROM meta WHERE key=?",
                      (HIDDEN_SHOWS_META_KEY,)).fetchone()
    if row is None:
        return {}
    try:
        data = json.loads(row["value"])
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def hidden_show_titles(con: sqlite3.Connection) -> list:
    return sorted(t for t, rec in get_hidden_shows(con).items()
                  if isinstance(rec, dict) and rec.get("hidden") is True)


@db.atomic
def set_show_hidden(con: sqlite3.Connection, show_title: str, hidden: bool,
                    expected_revision, actor: str = "owner") -> dict:
    if not isinstance(show_title, str) or not show_title.strip():
        raise BadInput("show_title is required")
    if not isinstance(hidden, bool):
        raise BadInput("hidden must be a boolean")
    if expected_revision is None:
        raise MissingPrecondition("expected revision is required")
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
        raise BadInput("expected_revision must be an integer")
    title = show_title.strip()
    if con.execute("SELECT 1 FROM episodes WHERE show_title=?",
                   (title,)).fetchone() is None:
        raise BadInput("unknown show")
    records = get_hidden_shows(con)
    current = records.get(title, {}).get("revision", 0) \
        if isinstance(records.get(title), dict) else 0
    if int(expected_revision) != current:
        raise Conflict(current)
    now = db.utcnow()
    records[title] = {"hidden": hidden, "revision": current + 1,
                      "updated_at": now}
    with db.transaction(con, write=True):
        con.execute("INSERT INTO meta (key, value) VALUES (?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (HIDDEN_SHOWS_META_KEY, json.dumps(records)))
        rev = db.bump_revision(con)
        db.audit(con, actor, "show_unhide" if not hidden else "show_hide",
                 "show", title, {"hidden": hidden}, rev)
    return {"show_title": title, "hidden": hidden,
            "revision": current + 1, "aggregate_rev": rev}


_NOT_BRAND_HIDDEN = "e.id NOT IN (SELECT value FROM json_each(?))"


def _filter_where(filt: dict, exclude_shows: tuple = (),
                  only_shows: tuple = (), con=None,
                  brand_hidden=None) -> tuple[str, list]:
    """brand_hidden (customer callers only): evidence ids whose brand is
    under review (ad_reads.brand_review_ids); a brand, brand basis or brand
    channel predicate never matches them. None (operators) changes nothing."""
    conds = ["1=1"]
    args: list = []
    if exclude_shows:
        conds.append("ep.show_title NOT IN (%s)" % ",".join("?" * len(exclude_shows)))
        args += list(exclude_shows)
    if only_shows:
        conds.append("ep.show_title IN (%s)" % ",".join("?" * len(only_shows)))
        args += list(only_shows)
    # A row belongs to a source version through its own pin or an importer
    # membership (member=1); member=0 retires a duplicate from that version.
    # Workspaces made before the membership table existed have no memberships.
    member = ("EXISTS (SELECT 1 FROM evidence_source_membership m WHERE m.evidence_id=e.id"
              " AND m.source_version_id=sv.id AND m.member=%d)"
              if con is None or db.has_evidence_membership(con) else "%d<0")
    if filt["source_version"]:
        conds.append("EXISTS (SELECT 1 FROM source_versions sv WHERE sv.id=?"
                     " AND ((sv.id=e.source_version_id AND NOT " + member % 0 + ") OR " + member % 1 + "))")
        args.append(filt["source_version"])
    elif filt["history"] != "all":
        # Active: the row's own pinned version is active and not retired from
        # it (member=0), or an active version lists it (member=1). Written as
        # lookups by the row's own keys, not a scan of every active version.
        if con is None or db.has_evidence_membership(con):
            conds.append(
                "(e.source_version_id IS NULL"
                " OR (EXISTS (SELECT 1 FROM source_versions sv WHERE sv.id=e.source_version_id"
                " AND sv.is_active=1) AND NOT EXISTS (SELECT 1 FROM evidence_source_membership m"
                " WHERE m.evidence_id=e.id AND m.source_version_id=e.source_version_id"
                " AND m.member=0))"
                " OR EXISTS (SELECT 1 FROM evidence_source_membership m JOIN source_versions sv"
                " ON sv.id=m.source_version_id WHERE m.evidence_id=e.id AND m.member=1"
                " AND sv.is_active=1))")
        else:
            conds.append("(e.source_version_id IS NULL OR EXISTS (SELECT 1 FROM source_versions sv"
                         " WHERE sv.id=e.source_version_id AND sv.is_active=1))")
        if filt["history"] != "active_version":  # else any transcript (legacy ads)
            conds.append("e.transcript_version_id IS ep.transcript_version_id")
    if filt["source"]:
        conds.append("e.source IN (%s)" % ",".join("?" * len(filt["source"])))
        args += filt["source"]
    if filt["review"]:
        parts = []
        if "unreviewed" in filt["review"]:
            parts.append("r.judgment IS NULL")
        judged = [s for s in filt["review"] if s != "unreviewed"]
        if judged:
            parts.append("r.judgment IN (%s)" % ",".join("?" * len(judged)))
            args += judged
        conds.append("(" + " OR ".join(parts) + ")")
    channels = filt.get("brand_channel") or []
    channel_sql = (" AND a.channel IN (%s)" % ",".join("?" * len(channels))) if channels else ""
    if filt["brand"]:
        # PODS: model rows are display-only
        conds.append("EXISTS (SELECT 1 FROM brand_assocs a WHERE a.evidence_id=e.id"
                     " AND a.basis<>'model' AND a.brand=?%s%s)" % (" AND a.basis IN (%s)" % ",".join("?" * len(filt["brand_basis"]))
                                              if filt["brand_basis"] else "",
                                              channel_sql))
        args.append(filt["brand"].strip().lower())
        args += filt["brand_basis"]
        args += channels
        if brand_hidden is not None:
            conds.append(_NOT_BRAND_HIDDEN)
            args.append(json.dumps(sorted(brand_hidden)))
    elif filt["brand_basis"] or channels:
        clauses = []
        if filt["brand_basis"]:
            clauses.append("a.basis IN (%s)" % ",".join("?" * len(filt["brand_basis"])))
            args += filt["brand_basis"]
        if channels:
            clauses.append("a.channel IN (%s)" % ",".join("?" * len(channels)))
            args += channels
        conds.append("EXISTS (SELECT 1 FROM brand_assocs a WHERE a.evidence_id=e.id"
                     " AND a.basis<>'model' AND " + " AND ".join(clauses) + ")")
        if brand_hidden is not None:
            conds.append(_NOT_BRAND_HIDDEN)
            args.append(json.dumps(sorted(brand_hidden)))
    if filt["show"]:
        conds.append("ep.show_title LIKE ? ESCAPE '\\'")
        args.append("%" + _like_escape(filt["show"]) + "%")
    if filt["category"]:
        conds.append("ep.category=?")
        args.append(filt["category"])
    if filt["episode"]:
        conds.append("e.episode_id=?")
        args.append(filt["episode"])
    if filt["pub_from"]:
        conds.append("(NULLIF(ep.pub_date,'') IS NULL OR ep.pub_date>=?)")
        args.append(filt["pub_from"])
    if filt["pub_to"]:
        conds.append("(NULLIF(ep.pub_date,'') IS NULL OR ep.pub_date<=?)")
        args.append(filt["pub_to"])
    if not filt["include_unknown_dates"]:
        conds.append("NULLIF(ep.pub_date,'') IS NOT NULL")
    return " AND ".join(conds), args


def brand_sort_order(brand_hidden=None) -> tuple[str, list]:
    """The brand sort key; a brand_hidden id (customer callers) sorts as
    a read with no brand."""
    # PODS: model rows are display-only
    key = "(SELECT MIN(brand) FROM brand_assocs WHERE evidence_id=e.id AND basis<>'model')"
    if brand_hidden is None:
        return key + ", e.id", []
    return ("CASE WHEN " + _NOT_BRAND_HIDDEN + " THEN " + key + " END, e.id",
            [json.dumps(sorted(brand_hidden))])


def hide_brand_guess(item: dict, brand_hidden) -> dict:
    """A search/detail item as customers see it when its brand is under
    review: no brand associations and no brand attrs."""
    if brand_hidden and item.get("id") in brand_hidden:
        from .ad_reads import without_brand_guess as strip
        item["assocs"] = []
        item["attrs"] = strip(item.get("attrs"))
    return item


def search_evidence(con: sqlite3.Connection, filt: dict, brand_hidden=None) -> dict:
    """brand_hidden (customer callers only, ad_reads.brand_review_ids):
    those reads show no brand, and no brand predicate, brand text match or
    brand sort uses their guess. None (operators) changes nothing."""
    filt = normalize_filter(filt)
    hidden = hidden_show_titles(con)
    where, args = _filter_where(filt, exclude_shows=tuple(hidden), con=con,
                                brand_hidden=brand_hidden)
    base = ("FROM evidence e JOIN episodes ep ON ep.id=e.episode_id"
            " LEFT JOIN reviews r ON r.evidence_id=e.id")
    search_ids = None
    if filt["q"]:
        # Literal substring search is deterministic at this bounded corpus size.
        # Search full pinned interval text and original excerpt, never FTS grammar.
        needle = filt["q"].casefold()
        search_ids = []
        # One pass over the brands and one parse per transcript version,
        # not one of each per scanned row (B-15).
        brands: dict = {}
        # PODS: model rows are display-only
        for a in con.execute("SELECT evidence_id, brand FROM brand_assocs"
                             " WHERE basis<>'model' ORDER BY id"):
            brands.setdefault(a["evidence_id"], []).append(a["brand"])
        segments: dict = {}
        for row in con.execute("SELECT e.*, ep.show_title, ep.ep_title FROM evidence e"
                               " JOIN episodes ep ON ep.id=e.episode_id"):
            assoc_text = "" if brand_hidden and row["id"] in brand_hidden else " ".join(
                brands.get(row["id"], ()))
            text = " ".join((row["show_title"] or "", row["ep_title"] or "", assoc_text,
                             row["excerpt"] or "", evidence_text(con, dict(row), segments)))
            if needle in text.casefold():
                search_ids.append(row["id"])
        if not search_ids:
            return {"filter": filt, "total": 0, "page": filt["page"],
                    "per_page": filt["per_page"], "items": [],
                    "hidden_excluded": 0, "hidden_shows": hidden}
        where += " AND e.id IN (%s)" % ",".join("?" * len(search_ids))
        args += search_ids
    total = con.execute(f"SELECT COUNT(*) c {base} WHERE {where}", args).fetchone()["c"]
    hidden_excluded = 0
    if hidden:
        hidden_where, hidden_args = _filter_where(
            filt, only_shows=tuple(hidden), con=con, brand_hidden=brand_hidden)
        if filt["q"] and search_ids:
            hidden_where += " AND e.id IN (%s)" % ",".join("?" * len(search_ids))
            hidden_args += search_ids
        hidden_excluded = con.execute(
            f"SELECT COUNT(*) c {base} WHERE {hidden_where}",
            hidden_args).fetchone()["c"]
    order_args: list = []
    if filt["sort"] == "brand":
        order, order_args = brand_sort_order(brand_hidden)
    else:
        order = {"episode": "e.episode_id, e.orig_start_s, e.id",
                 "start": "e.orig_start_s, e.id",
                 "published": "CASE WHEN NULLIF(ep.pub_date,'') IS NULL THEN 1 ELSE 0 END,"
                              " ep.pub_date, e.id"}[filt["sort"]]
    offset = (filt["page"] - 1) * filt["per_page"]
    rows = con.execute(
        f"SELECT e.*, ep.show_title, ep.ep_title, ep.category, r.judgment,"
        f" r.eff_start_s, r.eff_end_s, r.revision AS review_rev {base}"
        f" WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
        args + order_args + [filt["per_page"], offset]).fetchall()
    items = []
    for row in rows:
        d = dict(row)
        d["attrs"] = json.loads(d["attrs"])
        # PODS: model rows are display-only
        d["assocs"] = [dict(a) for a in con.execute(
            "SELECT brand, basis, origin, channel FROM brand_assocs WHERE evidence_id=?"
            " AND basis<>'model' ORDER BY brand",
            (d["id"],)).fetchall()]
        items.append(hide_brand_guess(d, brand_hidden))
    return {"filter": filt, "total": total, "page": filt["page"],
            "per_page": filt["per_page"], "items": items,
            "hidden_excluded": hidden_excluded, "hidden_shows": hidden}


def evidence_detail(con: sqlite3.Connection, evidence_id: str) -> dict | None:
    row = con.execute("SELECT e.*, ep.show_title, ep.ep_title, ep.category, ep.audio_url,"
                      " ep.media_id, ep.duration_min FROM evidence e"
                      " JOIN episodes ep ON ep.id=e.episode_id WHERE e.id=?",
                      (evidence_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["attrs"] = json.loads(d["attrs"])
    d["provenance"] = source_provenance(con, d.get("source_version_id"))
    d["transcript_provenance"] = source_provenance(con, d.get("transcript_version_id"))
    d["review"] = dict(con.execute("SELECT * FROM reviews WHERE evidence_id=?",
                                   (evidence_id,)).fetchone() or {})
    # PODS: model rows are display-only
    d["assocs"] = [dict(a) for a in con.execute(
        "SELECT * FROM brand_assocs WHERE evidence_id=? AND basis<>'model' ORDER BY brand",
        (evidence_id,)).fetchall()]
    d["overlaps"] = [dict(o) for o in con.execute(
        "SELECT id, source, orig_start_s, orig_end_s FROM evidence"
        " WHERE episode_id=? AND id!=? AND orig_start_s IS NOT NULL"
        " AND orig_start_s < COALESCE(?, 1e18) AND orig_end_s > COALESCE(?, -1)",
        (d["episode_id"], evidence_id, d["orig_end_s"], d["orig_start_s"])).fetchall()]
    return d


def source_provenance(con, version_id):
    row = con.execute("SELECT id, source_key, version_no, sha256, is_active, created_at"
                      " FROM source_versions WHERE id=?", (version_id,)).fetchone()
    return dict(row) if row else None


def evidence_text(con, evidence, segments_cache: dict | None = None):
    """The pinned interval's transcript text. segments_cache (one call's
    scratch dict) keeps each parsed transcript for rows that share it."""
    review = con.execute("SELECT eff_start_s, eff_end_s FROM reviews WHERE evidence_id=?",
                         (evidence["id"],)).fetchone()
    start = review["eff_start_s"] if review and review["eff_start_s"] is not None else evidence["orig_start_s"]
    end = review["eff_end_s"] if review and review["eff_end_s"] is not None else evidence["orig_end_s"]
    if start is None or end is None:
        return ""
    key = (evidence["episode_id"], evidence.get("transcript_version_id"))
    if segments_cache is None:
        segments = transcript_segments(con, *key)
    elif key in segments_cache:
        segments = segments_cache[key]
    else:
        segments = segments_cache[key] = transcript_segments(con, *key)
    return " ".join(s["text"] for s in segments if s["start"] < end and s["end"] > start)


def transcript_segments(con: sqlite3.Connection, episode_id: str, version_id="current") -> list:
    current = version_id == "current"
    if current:
        ep = con.execute("SELECT transcript_version_id FROM episodes WHERE id=?", (episode_id,)).fetchone()
        version_id = ep["transcript_version_id"] if ep else None
    row = con.execute("SELECT payload FROM source_versions WHERE id=?", (version_id,)).fetchone()
    if row is None:
        alias = con.execute("SELECT alias_of FROM episodes WHERE id=?", (episode_id,)).fetchone() if current else None
        if alias and alias["alias_of"]:
            return transcript_segments(con, alias["alias_of"])
        return []
    try:
        payload = json.loads(row["payload"])
    except json.JSONDecodeError:
        return []
    return payload.get("segments", []) if isinstance(payload, dict) else []


@db.atomic
def save_review(con: sqlite3.Connection, evidence_id: str, body: dict,
                expected_revision, actor: str = "owner", *,
                history_origin: str = "mutation") -> dict:
    if expected_revision is None:
        raise MissingPrecondition("expected revision is required")
    row = con.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
    if row is None:
        raise BadInput("unknown evidence")
    cur = con.execute("SELECT revision FROM reviews WHERE evidence_id=?", (evidence_id,)).fetchone()
    current = cur["revision"] if cur else 0
    if int(expected_revision) != current:
        raise Conflict(current)
    judgment = body.get("judgment")
    if "judgment" in body and judgment is not None and (not isinstance(judgment, str) or judgment not in REVIEW_STATES):
        raise BadInput("judgment must be ad, not_ad, or unsure")
    eff_start = body.get("eff_start_s", "absent")
    eff_end = body.get("eff_end_s", "absent")
    notes = body.get("notes", None)
    with db.transaction(con, write=True):
        prev = dict(con.execute("SELECT * FROM reviews WHERE evidence_id=?",
                                (evidence_id,)).fetchone() or {})
        start = prev.get("eff_start_s")
        end = prev.get("eff_end_s")
        if eff_start != "absent":
            start = None if eff_start is None else _finite(eff_start, "eff_start_s")
        if eff_end != "absent":
            end = None if eff_end is None else _finite(eff_end, "eff_end_s")
        actual_start = start if start is not None else row["orig_start_s"]
        actual_end = end if end is not None else row["orig_end_s"]
        if actual_start is not None and actual_start < 0:
            raise BadInput("effective start must be >= 0")
        if actual_end is not None and actual_start is not None and actual_end <= actual_start:
            raise BadInput("effective end must exceed effective start")
        segments = transcript_segments(con, row["episode_id"], row["transcript_version_id"])
        if start is not None or end is not None:
            extent = max((s["end"] for s in segments), default=0)
            if not segments or any(v is not None and (v < 0 or v > extent) for v in (actual_start, actual_end)):
                raise BadInput("review bounds must lie within the pinned transcript")
        if "judgment" not in body:
            judgment = prev.get("judgment")
        if notes is None:
            notes = prev.get("notes", "")
        con.execute("INSERT INTO reviews (evidence_id, judgment, eff_start_s, eff_end_s,"
                    " notes, revision, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(evidence_id) DO UPDATE SET judgment=excluded.judgment,"
                    " eff_start_s=excluded.eff_start_s, eff_end_s=excluded.eff_end_s,"
                    " notes=excluded.notes, revision=reviews.revision+1, updated_at=excluded.updated_at",
                    (evidence_id, judgment, start, end, str(notes), current + 1, db.utcnow()))
        verified = body.get("bounds_verified", bool(prev.get("bounds_verified", 0)))
        if not isinstance(verified, bool):
            raise BadInput("bounds_verified must be boolean")
        if verified and (start is None or end is None):
            raise BadInput("verified bounds require both start and end")
        con.execute("UPDATE reviews SET bounds_verified=? WHERE evidence_id=?",
                    (int(verified), evidence_id))
        if db.has_history_tables(con):
            db.record_review_snapshot(con, evidence_id, actor, origin=history_origin)
        if db.has_v8_tables(con):
            _citations.publish_source_state(con, kind="evidence",
                                            record_id=evidence_id,
                                            actor=actor)
        from . import corrections
        corrections.after_review(con, evidence_id, actor)
        rev = db.bump_revision(con)
        db.audit(con, actor, "review_save", "evidence", evidence_id,
                 {"judgment": judgment, "revision": current + 1}, rev)
    return {"evidence_id": evidence_id, "revision": current + 1, "aggregate_rev": rev}


@db.atomic
def clear_review(con: sqlite3.Connection, evidence_id: str, expected_revision,
                 actor: str = "owner") -> dict:
    current = con.execute("SELECT revision FROM reviews WHERE evidence_id=?", (evidence_id,)).fetchone()
    revision = current["revision"] if current else 0
    if expected_revision is None:
        raise MissingPrecondition("expected revision is required")
    if int(expected_revision) != revision:
        raise Conflict(revision)
    if current is None:
        raise BadInput("no judgment to clear")
    con.execute("UPDATE reviews SET judgment=NULL, revision=revision+1, updated_at=?"
                " WHERE evidence_id=?", (db.utcnow(), evidence_id))
    if db.has_history_tables(con):
        db.record_review_snapshot(con, evidence_id, actor)
    if db.has_v8_tables(con):
        _citations.publish_source_state(con, kind="evidence",
                                        record_id=evidence_id, actor=actor)
    from . import corrections
    corrections.after_review(con, evidence_id, actor)
    rev = db.bump_revision(con)
    db.audit(con, actor, "review_clear", "evidence", evidence_id, {}, rev)
    return {"evidence_id": evidence_id, "revision": revision + 1, "aggregate_rev": rev}


def _require_current(con: sqlite3.Connection, evidence_id: str) -> None:
    """Brand changes go to the current version of a read: a replaced row's
    change would never reach the read customers see. The rule is the
    customers' own row rule (customer.customer_row_where): with named
    reads, the current source version and transcript; without, also a row
    pinned to an older transcript (customers see it as an ad), never a row
    lineage moved onto another or one a current-transcript copy of the same
    read replaced (customer.legacy_ad_where). Nothing is added here."""
    from .customer import customer_row_where
    where, args = customer_row_where(con)
    if con.execute("SELECT 1 FROM evidence e JOIN episodes ep ON ep.id=e.episode_id"
                   f" WHERE e.id=? AND {where}", [evidence_id, *args]).fetchone() is None:
        raise BadInput("this is an old version of the read; change the current read")


@db.atomic
def set_brand_assocs(con: sqlite3.Connection, evidence_id: str, assocs: list,
                     expected_revision, actor: str = "owner", *,
                     history_origin: str = "mutation", kept: list = ()) -> dict:
    """Replace a read's brand associations. `kept` rows (brand, basis,
    origin, channel) are existing associations written back unchanged."""
    if expected_revision is None:
        raise MissingPrecondition("expected revision is required")
    if con.execute("SELECT id FROM evidence WHERE id=?", (evidence_id,)).fetchone() is None:
        raise BadInput("unknown evidence")
    _require_current(con, evidence_id)
    if not isinstance(assocs, list) or len(assocs) > 50:
        raise BadInput("assocs must be a list of at most 50 associations")
    seen = set()
    cleaned = []
    # Origin follows the trusted context actor, never a hardcoded claim:
    # agent connections record origin="agent"; the local browser owner keeps
    # the historical "human" default. (Seeded detector rows keep whatever the
    # import path wrote; changing those defaults is a product decision.)
    origin = "agent" if str(actor or "").startswith("agent:") else "human"
    for a in assocs or []:
        if not isinstance(a, dict) or not isinstance(a.get("brand"), str) or not a["brand"].strip():
            raise BadInput("each association needs a brand")
        basis = a.get("basis", "unknown")
        if not isinstance(basis, str) or basis not in BRAND_BASES:
            raise BadInput("basis must be confirmed, incidental, unknown, or suggested")
        brand = str(a["brand"]).strip().lower()
        channel = a.get("channel", "unspecified")
        if channel not in ("spoken", "url", "code", "unspecified"):
            raise BadInput("channel must be spoken, url, code, or unspecified")
        if (brand, channel) in seen:
            raise BadInput("each brand/channel pair may appear only once")
        seen.add((brand, channel))
        cleaned.append((brand, basis, origin, channel))
    for brand, basis, kept_origin, channel in kept:
        # PODS: model rows are display-only (never rewritten by an operator edit)
        if basis == "model":
            continue
        if (brand, channel) not in seen:
            seen.add((brand, channel))
            cleaned.append((brand, basis, kept_origin, channel))
    with db.transaction(con, write=True):
        cur = con.execute("SELECT assoc_revision FROM evidence WHERE id=?", (evidence_id,)).fetchone()
        if int(expected_revision) != cur["assoc_revision"]:
            raise Conflict(cur["assoc_revision"])
        con.execute("UPDATE evidence SET assoc_revision=assoc_revision+1 WHERE id=?", (evidence_id,))
        con.execute("DELETE FROM brand_assocs WHERE evidence_id=? AND basis<>'model'",
                    (evidence_id,))
        for brand, basis, origin, channel in cleaned:
            con.execute("INSERT INTO brand_assocs (evidence_id, brand, basis, origin,"
                        " revision, updated_at, channel) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (evidence_id, brand, basis, origin, int(expected_revision) + 1, db.utcnow(), channel))
        if db.has_history_tables(con):
            db.record_association_snapshot(con, evidence_id, actor, origin=history_origin)
        if db.has_v8_tables(con):
            _citations.publish_source_state(con, kind="evidence",
                                            record_id=evidence_id,
                                            actor=actor)
        from . import corrections
        corrections.after_review(con, evidence_id, actor)
        rev = db.bump_revision(con)
        db.audit(con, actor, "assocs_set", "evidence", evidence_id,
                 {"count": len(cleaned)}, rev)
    return {"evidence_id": evidence_id, "revision": int(expected_revision) + 1,
            "aggregate_rev": rev}


@db.atomic
def choose_brand(con: sqlite3.Connection, evidence_id: str, brand: str,
                 expected_revision, actor: str = "owner") -> dict:
    """Operator brand choice (validation plan A6): one of the namer's other
    brand candidates becomes the read's confirmed brand. The machine
    suggestion goes, and so does any brand a person or agent set to one of
    the read's candidates (an earlier choice); other brands a person or
    agent added stay as they were (basis and origin). The choice is an
    association revision (history origin "brand-choice") plus a
    "brand_choice" audit entry naming the brand it replaced."""
    from . import ad_reads as _ad
    from . import customer as _customer
    if expected_revision is None:
        raise MissingPrecondition("expected revision is required")
    read = _customer.read_detail(con, evidence_id)
    if read is None:
        raise BadInput("unknown evidence")
    _require_current(con, evidence_id)
    if not isinstance(brand, str) or not brand.strip():
        raise BadInput("brand is required")
    aliases = _ad.alias_map(con)
    wanted = _ad.canonical(aliases, brand)
    listed = [str(c.get("brand") or "") for c in read.get("candidates") or []
              if isinstance(c, dict)]
    rank = next((i + 1 for i, c in enumerate(listed) if _ad.canonical(aliases, c) == wanted), None)
    if rank is None:
        raise BadInput("brand is not one of this read's candidates")
    chosen = listed[rank - 1].strip().lower()
    candidates = {_ad.canonical(aliases, c) for c in listed}
    # PODS: model rows are display-only
    kept = [tuple(a) for a in con.execute("SELECT brand, basis, origin, channel FROM brand_assocs"
                                          " WHERE evidence_id=? AND basis<>'model' ORDER BY id",
                                          (evidence_id,))
            if a["origin"] != "detector" and _ad.canonical(aliases, a["brand"]) not in candidates]
    out = set_brand_assocs(con, evidence_id,
                           [{"brand": chosen, "basis": "confirmed", "channel": "spoken"}],
                           expected_revision, actor, history_origin="brand-choice", kept=kept)
    db.audit(con, actor, "brand_choice", "evidence", evidence_id,
             {"brand": chosen, "previous": (read.get("brand") or {}).get("key"),
              "candidate_rank": rank}, out["aggregate_rev"])
    return out


@db.atomic
def create_manual_evidence(con: sqlite3.Connection, body: dict, actor: str = "owner") -> dict:
    ep_id = body.get("episode_id")
    ep = con.execute("SELECT transcript_version_id FROM episodes WHERE id=?", (ep_id,)).fetchone()
    if ep is None:
        raise BadInput("unknown episode")
    start = _finite(body.get("start_s"), "start_s")
    end = _finite(body.get("end_s"), "end_s")
    if start < 0 or end <= start:
        raise BadInput("require 0 <= start < end")
    transcript_id = body.get("transcript_version_id", ep["transcript_version_id"])
    if not con.execute("SELECT 1 FROM source_versions WHERE id=? AND source_key=?",
                       (transcript_id, f"transcript:{ep_id}")).fetchone():
        raise BadInput("manual evidence requires a transcript version for this episode")
    origin = str(body.get("origin_note", ""))[:500]
    if not origin:
        raise BadInput("origin_note is required for manual evidence")
    segs = transcript_segments(con, ep_id, transcript_id)
    if not segs or end > max(s["end"] for s in segs):
        raise BadInput("manual bounds must lie within the transcript")
    excerpt = " ".join(str(s.get("text", "")) for s in segs
                       if s.get("start", float("inf")) < end
                       and s.get("end", float("-inf")) > start).strip()[:600]
    ev_id = "man:" + uuid.uuid4().hex[:12]
    with db.transaction(con, write=True):
        con.execute("INSERT INTO evidence (id, source, episode_id, orig_start_s, orig_end_s,"
                    " excerpt, attrs, revision, created_at) VALUES (?, 'manual', ?, ?, ?, ?, ?, 1, ?)",
                    (ev_id, ep_id, start, end, excerpt,
                     json.dumps({"origin_note": origin}), db.utcnow()))
        con.execute("UPDATE evidence SET transcript_version_id=? WHERE id=?", (transcript_id, ev_id))
        if db.has_history_tables(con):
            db.record_evidence_snapshot(con, ev_id, actor)
        if db.has_v8_tables(con):
            _citations.publish_source_state(con, kind="evidence",
                                            record_id=ev_id, actor=actor)
        rev = db.bump_revision(con)
        db.audit(con, actor, "evidence_create", "evidence", ev_id,
                 {"episode": ep_id, "start": start, "end": end}, rev)
    return {"evidence_id": ev_id, "aggregate_rev": rev}


def overview(con: sqlite3.Connection) -> dict:
    hidden = hidden_show_titles(con)
    where, args = _filter_where(normalize_filter({}),
                                exclude_shows=tuple(hidden), con=con)
    active = (" FROM evidence e JOIN episodes ep ON ep.id=e.episode_id"
              " LEFT JOIN reviews r ON r.evidence_id=e.id WHERE " + where)
    counts = {r["source"]: r["c"] for r in
              con.execute("SELECT e.source, COUNT(*) c" + active + " GROUP BY e.source", args)}
    rev = {r["judgment"] or "unreviewed": r["c"] for r in
           con.execute("SELECT r.judgment, COUNT(*) c" + active + " GROUP BY r.judgment", args)}
    hidden_excluded = 0
    if hidden:
        hidden_where, hidden_args = _filter_where(
            normalize_filter({}), only_shows=tuple(hidden), con=con)
        hidden_excluded = con.execute(
            "SELECT COUNT(*) c FROM evidence e JOIN episodes ep ON ep.id=e.episode_id"
            " LEFT JOIN reviews r ON r.evidence_id=e.id WHERE " + hidden_where,
            hidden_args).fetchone()["c"]
    return {
        "hidden_excluded": hidden_excluded,
        "hidden_shows": hidden,
        "metadata_records": con.execute(
            "SELECT COUNT(*) c FROM episodes WHERE kind='supplied'").fetchone()["c"],
        "transcript_files": con.execute("SELECT COUNT(DISTINCT path) c FROM source_files"
                                        " WHERE kind='transcript'").fetchone()["c"],
        "canonical_transcript_episodes": con.execute(
            "SELECT COUNT(DISTINCT COALESCE(alias_of, id)) c FROM episodes"
            " WHERE transcript_version_id IS NOT NULL").fetchone()["c"],
        "transcript_hours": round((con.execute(
            "SELECT COALESCE(SUM(duration_min),0) s FROM episodes"
            " WHERE transcript_version_id IS NOT NULL AND alias_of IS NULL").fetchone()["s"] or 0) / 60, 2),
        "evidence_by_source": counts,
        "review_states": rev,
        "import_state": [dict(r) for r in con.execute(
            "SELECT id, source_key, version_no, sha256, is_active, created_at FROM source_versions ORDER BY source_key").fetchall()],
        "aggregate_rev": db.aggregate_revision(con),
    }


def brands_index(con: sqlite3.Connection, q: str = "") -> list:
    like = "%" + _like_escape(q.strip().lower()) + "%" if q.strip() else None
    where, args = _filter_where(normalize_filter({}),
                                exclude_shows=tuple(hidden_show_titles(con)), con=con)
    sql = ("SELECT brand, COUNT(DISTINCT e.id) c,"
           " COUNT(DISTINCT CASE WHEN r.judgment='ad' THEN e.id END) ads"
           # PODS: model rows are display-only
           " FROM brand_assocs a JOIN evidence e ON e.id=a.evidence_id AND a.basis<>'model'"
           " JOIN episodes ep ON ep.id=e.episode_id"
           " LEFT JOIN reviews r ON r.evidence_id=e.id WHERE " + where)
    if like:
        sql += " AND a.brand LIKE ? ESCAPE '\\'"
        args.append(like)
    sql += " GROUP BY brand ORDER BY c DESC LIMIT 200"
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def shows_index(con: sqlite3.Connection, q: str = "") -> list:
    where, args = _filter_where(normalize_filter({}),
                                exclude_shows=tuple(hidden_show_titles(con)), con=con)
    rows = con.execute(
        "SELECT ep.id, ep.show_title, ep.ep_title, ep.category, ep.pub_date,"
        " ep.transcript_version_id IS NOT NULL AS has_transcript,"
        " COUNT(DISTINCT e.id) AS evidence FROM episodes ep"
        " LEFT JOIN evidence e ON e.episode_id=ep.id AND " + where +
        " GROUP BY ep.id ORDER BY ep.show_title, ep.id", args).fetchall()
    needle = q.strip().casefold()
    records = get_hidden_shows(con)
    out = []
    for row in rows:
        if needle and needle not in " ".join(
                [row["show_title"] or "", row["ep_title"] or ""] +
                [s["text"] for s in transcript_segments(con, row["id"])]).casefold():
            continue
        d = dict(row)
        rec = records.get(d.get("show_title") or "")
        d["hidden"] = bool(isinstance(rec, dict) and rec.get("hidden") is True)
        d["show_revision"] = int(rec.get("revision", 0)) if isinstance(rec, dict) else 0
        out.append(d)
    return out


# -- watchlists ---------------------------------------------------------
def list_watchlists(con: sqlite3.Connection) -> list:
    return [dict(r) for r in con.execute("SELECT * FROM watchlists ORDER BY name").fetchall()]


@db.atomic
def save_watchlist(con: sqlite3.Connection, body: dict, watch_id=None,
                   expected_revision=None, actor: str = "owner") -> dict:
    if not isinstance(body.get("name"), str) or not body["name"].strip():
        raise BadInput("watchlist name is required")
    name = body["name"].strip()[:120]
    _, problems = _search.decode_legacy_filter(body.get("query", {}))
    if problems:
        raise NeedsCorrection(problems)
    query = normalize_filter(body.get("query", {}))
    with db.transaction(con, write=True):
        if watch_id is None:
            cur = con.execute("INSERT INTO watchlists (name, query, revision, updated_at)"
                              " VALUES (?, ?, 1, ?)", (name, json.dumps(query), db.utcnow()))
            wid = cur.lastrowid
            rev = db.bump_revision(con)
            db.audit(con, actor, "watchlist_create", "watchlist", str(wid), {"name": name}, rev)
            return {"id": wid, "revision": 1, "aggregate_rev": rev}
        if expected_revision is None:
            raise MissingPrecondition("expected revision is required")
        cur = con.execute("SELECT revision FROM watchlists WHERE id=?", (watch_id,)).fetchone()
        if cur is None:
            raise BadInput("unknown watchlist")
        if int(expected_revision) != cur["revision"]:
            raise Conflict(cur["revision"])
        con.execute("UPDATE watchlists SET name=?, query=?, revision=revision+1, updated_at=?"
                    " WHERE id=?", (name, json.dumps(query), db.utcnow(), watch_id))
        rev = db.bump_revision(con)
        db.audit(con, actor, "watchlist_update", "watchlist", str(watch_id), {}, rev)
        return {"id": watch_id, "revision": cur["revision"] + 1, "aggregate_rev": rev}


@db.atomic
def delete_watchlist(con: sqlite3.Connection, watch_id: int, expected_revision=None,
                     actor: str = "owner") -> dict:
    with db.transaction(con, write=True):
        if con.execute("SELECT id FROM watchlists WHERE id=?", (watch_id,)).fetchone() is None:
            raise BadInput("unknown watchlist")
        _expect(con, "watchlists", watch_id, expected_revision)
        con.execute("DELETE FROM watchlists WHERE id=?", (watch_id,))
        rev = db.bump_revision(con)
        db.audit(con, actor, "watchlist_delete", "watchlist", str(watch_id), {}, rev)
    return {"aggregate_rev": rev}


def evaluate_watchlist(con: sqlite3.Connection, watch_id: int) -> dict:
    import time
    row = con.execute("SELECT * FROM watchlists WHERE id=?", (watch_id,)).fetchone()
    if row is None:
        raise BadInput("unknown watchlist")
    try:
        stored = json.loads(row["query"])
    except (json.JSONDecodeError, TypeError):
        stored = None
    filt_v1, problems = _search.decode_legacy_filter(stored)
    if problems:
        raise NeedsCorrection(problems)
    start = time.monotonic()
    result = search_evidence(con, {**_search.to_legacy_filter(filt_v1),
                                   "page": 1, "per_page": 1})
    return {"id": watch_id, "name": row["name"], "match_count": result["total"],
            "evaluated_at": db.utcnow(), "eval_ms": round((time.monotonic() - start) * 1000, 1)}


# -- campaigns ----------------------------------------------------------
def list_campaigns(con: sqlite3.Connection, ctx=None) -> list:
    from . import access, citations
    if ctx is not None and not ctx.permits("campaign"):
        raise access.AuthorizationError("campaign disclosure denied", 403)
    rows = con.execute("SELECT * FROM campaigns ORDER BY status, name").fetchall()
    return [dict(row) for row in rows if ctx is None or
            citations.resource_allowed(con, "campaign", row["id"], ctx)]


def campaign_detail(con: sqlite3.Connection, campaign_id: int, ctx=None) -> dict | None:
    if ctx is not None:
        from . import citations
        citations.authorize_resource(con, "campaign", campaign_id, ctx)
    row = con.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["target_shows"] = json.loads(d["target_shows"])
    from . import citations, access
    d["evidence"] = []
    for linked in con.execute("SELECT evidence_id FROM campaign_evidence WHERE campaign_id=?", (campaign_id,)):
        ref = citations.current_source_ref(con, "evidence", linked["evidence_id"])
        try:
            resolved = citations.resolve_source_state(con, ref, ctx=ctx)
        except access.AuthorizationError:
            continue
        if "pins" in resolved:
            d["evidence"].append({"id": resolved["record_id"], "episode_id": resolved["episode"]["episode_id"],
                                  "orig_start_s": resolved["original_bounds"]["start_s"],
                                  "orig_end_s": resolved["original_bounds"]["end_s"],
                                  "excerpt": resolved.get("text")})
        else:
            d["evidence"].append({"id": resolved["record_id"]})
    d["checks"] = []
    for row in con.execute("SELECT * FROM campaign_checks WHERE campaign_id=? ORDER BY id", (campaign_id,)):
        check = dict(row)
        if check.get("evidence_id"):
            ref = citations.current_source_ref(con, "evidence", check["evidence_id"])
            try:
                resolved = citations.resolve_source_state(con, ref, ctx=ctx)
            except access.AuthorizationError:
                continue
            if "pins" not in resolved:
                continue
        check["params"] = json.loads(check["params"])
        try:
            check["result"] = evaluate_check(con, check, ctx=ctx)
        except access.AuthorizationError:
            continue
        d["checks"].append(check)
    return d


@db.atomic
def save_campaign(con: sqlite3.Connection, body: dict, campaign_id=None,
                  expected_revision=None, actor: str = "owner") -> dict:
    name = str(body.get("name") or "").strip()[:160]
    if (campaign_id is None or "name" in body) and not name:
        raise BadInput("campaign name is required")
    if not isinstance(body.get("target_shows", []), list) or any(
            not isinstance(s, str) for s in body.get("target_shows", [])):
        raise BadInput("target_shows must be a list of strings")
    fields = {
        "name": name,
        "status": body.get("status", "active") if body.get("status") in ("active", "archived") else "active",
        "brand": (str(body.get("brand", "")).strip().lower() or None) if body.get("brand") is not None else None,
        "date_from": body.get("date_from"), "date_to": body.get("date_to"),
        "target_shows": json.dumps(body.get("target_shows", [])),
        "message": str(body.get("message") or "")[:2000],
        "offer": str(body.get("offer") or "")[:1000],
        "notes": str(body.get("notes") or "")[:4000],
        "manual_conclusion": body.get("manual_conclusion")
        if body.get("manual_conclusion") in ("supported", "not_supported", "inconclusive") else None,
        "manual_reason": str(body.get("manual_reason") or "")[:2000],
    }
    with db.transaction(con, write=True):
        if campaign_id is None:
            cur = con.execute("INSERT INTO campaigns (name, status, brand, date_from, date_to,"
                              " target_shows, message, offer, notes, manual_conclusion,"
                              " manual_reason, revision, updated_at)"
                              " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                              (*fields.values(), db.utcnow()))
            cid = cur.lastrowid
            if db.has_history_tables(con):
                db.record_campaign_snapshot(con, cid, actor)
            rev = db.bump_revision(con)
            db.audit(con, actor, "campaign_create", "campaign", str(cid), {"name": name}, rev)
            return {"id": cid, "revision": 1, "aggregate_rev": rev}
        if expected_revision is None:
            raise MissingPrecondition("expected revision is required")
        cur = con.execute("SELECT revision, status FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if cur is None:
            raise BadInput("unknown campaign")
        if int(expected_revision) != cur["revision"]:
            raise Conflict(cur["revision"])
        known = ("name", "status", "brand", "date_from", "date_to", "target_shows",
                 "message", "offer", "notes", "manual_conclusion", "manual_reason")
        # An archived campaign takes Restore only (decision 16), as on the
        # customer route; a stale revision gets the conflict first.
        if not ({key for key in known if key in body} == {"status"} and body["status"] == "active"):
            from . import campaigns
            campaigns._refuse_archived(con, dict(cur))
        sets, args = [], []
        for key in known:
            # Explicit null clears; absent leaves unchanged.
            if key in body:
                sets.append(f"{key}=?")
                args.append(fields[key])
        if not sets:
            raise BadInput("no known campaign fields to update")
        con.execute(f"UPDATE campaigns SET {', '.join(sets)}, revision=revision+1,"
                    " updated_at=? WHERE id=?", args + [db.utcnow(), campaign_id])
        if db.has_history_tables(con):
            db.record_campaign_snapshot(con, campaign_id, actor)
        rev = db.bump_revision(con)
        db.audit(con, actor, "campaign_update", "campaign", str(campaign_id), {}, rev)
        return {"id": campaign_id, "revision": cur["revision"] + 1, "aggregate_rev": rev}


def _expect(con, table, identity, expected):
    if expected is None:
        raise MissingPrecondition("expected revision is required")
    row = con.execute(f"SELECT * FROM {table} WHERE id=?", (identity,)).fetchone()
    if row is None:
        raise BadInput(f"unknown {table} record")
    if isinstance(expected, bool) or int(expected) != row["revision"]:
        raise Conflict(row["revision"])
    return dict(row)


def _campaign_changed(con, campaign_id, actor, action, detail):
    con.execute("UPDATE campaigns SET revision=revision+1, updated_at=? WHERE id=?",
                (db.utcnow(), campaign_id))
    if db.has_history_tables(con):
        db.record_campaign_snapshot(con, campaign_id, actor)
    rev = db.bump_revision(con)
    db.audit(con, actor, action, "campaign", str(campaign_id), detail, rev)
    revision = con.execute("SELECT revision FROM campaigns WHERE id=?", (campaign_id,)).fetchone()[0]
    return {"aggregate_rev": rev, "campaign_revision": revision}


@db.atomic
def attach_evidence(con, campaign_id, evidence_id, expected_revision=None, actor="owner"):
    from . import campaigns
    campaigns._campaign_exists(con, campaign_id)
    campaigns._refuse_archived(con, _expect(con, "campaigns", campaign_id, expected_revision))
    if not con.execute("SELECT 1 FROM evidence WHERE id=?", (evidence_id,)).fetchone():
        raise BadInput("unknown evidence")
    con.execute("INSERT OR IGNORE INTO campaign_evidence VALUES (?, ?)", (campaign_id, evidence_id))
    return _campaign_changed(con, campaign_id, actor, "campaign_attach", {"evidence": evidence_id})


@db.atomic
def detach_evidence(con, campaign_id, evidence_id, expected_revision=None, actor="owner"):
    from . import campaigns
    campaigns._campaign_exists(con, campaign_id)
    campaigns._refuse_archived(con, _expect(con, "campaigns", campaign_id, expected_revision))
    con.execute("UPDATE campaign_checks SET evidence_id=NULL, revision=revision+1, updated_at=?"
                " WHERE campaign_id=? AND evidence_id=?", (db.utcnow(), campaign_id, evidence_id))
    con.execute("DELETE FROM campaign_evidence WHERE campaign_id=? AND evidence_id=?",
                (campaign_id, evidence_id))
    return _campaign_changed(con, campaign_id, actor, "campaign_detach", {"evidence": evidence_id})


def _check_params(ctype, params):
    if ctype not in CHECK_TYPES or not isinstance(params, dict):
        raise BadInput("choose a supported check type and parameters")
    if ctype in ("confirmed_brand", "literal_phrase"):
        key = "brand" if ctype == "confirmed_brand" else "phrase"
        if not isinstance(params.get(key), str) or not params[key].strip():
            raise BadInput(f"{key} is required")
        if len(params[key]) > 1000:
            raise BadInput(f"{key} exceeds 1000 characters")
        return {key: params[key].strip()}
    lo, hi = _finite(params.get("min_s"), "min_s"), _finite(params.get("max_s"), "max_s")
    if lo < 0 or hi < lo:
        raise BadInput("require 0 <= min_s <= max_s")
    return {"min_s": lo, "max_s": hi}


@db.atomic
def save_check(con, body, check_id=None, expected_revision=None, actor="owner"):
    previous = _expect(con, "campaign_checks", check_id, expected_revision) if check_id else {}
    campaign_id = previous.get("campaign_id", body.get("campaign_id"))
    from . import campaigns
    campaigns._refuse_archived(con, _expect(con, "campaigns", campaign_id, body.get("campaign_revision")))
    ctype = body.get("check_type", previous.get("check_type"))
    params = _check_params(ctype, body.get("params", json.loads(previous.get("params", "{}"))))
    evidence_id = body.get("evidence_id", previous.get("evidence_id"))
    if evidence_id is not None and not con.execute(
            "SELECT 1 FROM campaign_evidence WHERE campaign_id=? AND evidence_id=?",
            (campaign_id, evidence_id)).fetchone():
        raise BadInput("check evidence must be attached to this campaign")
    manual = body.get("manual_verdict", previous.get("manual_verdict"))
    if manual not in (None, "pass", "fail", "unknown"):
        raise BadInput("manual_verdict must be pass, fail, unknown, or null")
    reason = str(body.get("manual_reason", previous.get("manual_reason", "")))[:2000]
    if manual and not reason.strip():
        raise BadInput("a manual assessment requires a reason")
    values = (ctype, evidence_id, json.dumps(params), manual, reason, db.utcnow())
    if check_id:
        con.execute("UPDATE campaign_checks SET check_type=?, evidence_id=?, params=?,"
                    " manual_verdict=?, manual_reason=?, updated_at=?, revision=revision+1 WHERE id=?",
                    (*values, check_id))
    else:
        check_id = con.execute("INSERT INTO campaign_checks (check_type,evidence_id,params,"
                               "manual_verdict,manual_reason,updated_at,campaign_id)"
                               " VALUES (?,?,?,?,?,?,?)", (*values, campaign_id)).lastrowid
    out = _campaign_changed(con, campaign_id, actor, "check_save", {"check_id": check_id})
    if db.has_history_tables(con):
        fresh = con.execute("SELECT * FROM campaign_checks WHERE id=?", (check_id,)).fetchone()
        if fresh is not None:
            stored = dict(fresh)
            try:
                stored["params"] = json.loads(stored.get("params") or "{}")
            except (ValueError, TypeError):
                stored["params"] = {}
            computed = evaluate_check(con, stored)
            pins = []
            if stored.get("evidence_id"):
                ev_rev = con.execute("SELECT revision FROM evidence WHERE id=?",
                                     (stored["evidence_id"],)).fetchone()
                if ev_rev is not None:
                    pins.append({"evidence_id": stored["evidence_id"],
                                 "evidence_revision": ev_rev["revision"]})
            db.retain_check_evaluation(con, campaign_id, check_id, stored["revision"],
                                       _citations.CHECK_EVALUATOR_ID, pins, computed)
    return {**out, "id": check_id, "revision": previous.get("revision", 0) + 1}


def evaluate_check(con, check, ctx=None):
    ev_id, ctype, params = check.get("evidence_id"), check["check_type"], check["params"]
    ev = evidence_detail(con, ev_id) if ev_id else None
    from . import access, citations
    if ctx is None:
        ctx = access.AccessContext.create(principal_id="local-owner", principal_kind="owner",
            workspace_id="local", credential_kind="local_operator")
    notes_allowed = False
    if ev:
        source = citations.evidence_source_provenance(con, ev)
        if source is None:
            raise access.AuthorizationError("check source authority unavailable", 404)
        ctx.authorize_disclosure(con, source["source_key"], ["metadata"])
        notes_allowed = ctx.disclosure_allowed(con, source["source_key"], "notes")
    def result(outcome, reason):
        return {"outcome": outcome, "reason": reason, "evidence": ev_id,
                **({"review_revision": (ev or {}).get("review", {}).get("revision")} if notes_allowed else {}),
                "association_revision": (ev or {}).get("assoc_revision"),
                "source_version_id": (ev or {}).get("source_version_id"),
                "transcript_version_id": (ev or {}).get("transcript_version_id")}
    if not ev:
        return result("unknown", "No evidence linked; absence of evidence is not absence.")
    review = ev["review"]
    if review.get("judgment") != "ad":
        return result("unknown", "Evidence has not been reviewed as an ad.")
    if ctype == "confirmed_brand":
        brand = params.get("brand", "").strip().lower()
        assocs = [a for a in ev["assocs"] if a["brand"] == brand]
        if any(a["basis"] == "confirmed" for a in assocs):
            return result("pass", f"{brand} explicitly confirmed by a reviewer.")
        if any(a["basis"] == "incidental" for a in assocs):
            return result("fail", f"{brand} explicitly classified as incidental.")
        return result("unknown", "Brand attribution is missing or unconfirmed.")
    if ctype == "literal_phrase":
        transcript = citations._version_provenance(con, ev.get("transcript_version_id"))
        if transcript is None:
            raise access.AuthorizationError("check transcript authority unavailable", 404)
        ctx.authorize_disclosure(con, transcript["source_key"], ["metadata", "transcript"])
        text = evidence_text(con, ev)
        if not text:
            return result("unknown", "No complete interval text is available.")
        found = params.get("phrase", "").casefold() in text.casefold()
        if found:
            return result("pass", "Literal phrase appears in the pinned transcript interval; not an audio claim.")
        if not review.get("bounds_verified"):
            return result("unknown", "Phrase absent from proposed interval; boundaries remain unverified.")
        return result("fail", "Literal phrase is absent from the reviewed transcript interval; not an audio claim.")
    if ctype in ("duration_range", "position_range"):
        if not review.get("bounds_verified"):
            return result("unknown", "Boundaries have not been explicitly verified.")
        from . import campaigns
        ref = citations.current_source_ref(con, "evidence", ev_id)
        occurrence = campaigns._occurrence_from_evidence(con, ev_id, "", ref, ctx)
        if not occurrence["timing_verified"]:
            return result("unknown", "No matching full recorded source qualifies timing.")
        start, end = review.get("eff_start_s"), review.get("eff_end_s")
        value = end - start if ctype == "duration_range" else start
        outcome = "pass" if params["min_s"] <= value <= params["max_s"] else "fail"
        return result(outcome, f"Reviewer-verified value {value:.1f}s; alignment explicitly verified.")
    return result("unknown", "Unsupported check type.")


# -- settings / media / backup ------------------------------------------
def get_settings(con: sqlite3.Connection) -> dict:
    row = con.execute("SELECT value FROM meta WHERE key='settings'").fetchone()
    base = {"playback_speed": 1.0, "page_density": "comfortable", "revision": 0}
    if row:
        try:
            base.update(json.loads(row["value"]))
        except json.JSONDecodeError:
            pass
    return base


@db.atomic
def save_settings(con: sqlite3.Connection, body: dict, expected_revision, actor: str = "owner") -> dict:
    current = get_settings(con)
    if expected_revision is None:
        raise MissingPrecondition("expected revision is required")
    if int(expected_revision) != current["revision"]:
        raise Conflict(current["revision"])
    if "playback_speed" in body:
        try:
            speed = float(body["playback_speed"])
        except (TypeError, ValueError):
            raise BadInput("playback_speed must be a number")
        if not 0.5 <= speed <= 2.0:
            raise BadInput("playback_speed must be between 0.5 and 2.0")
        current["playback_speed"] = speed
    if "page_density" in body:
        if body["page_density"] not in ("comfortable", "compact"):
            raise BadInput("page_density must be comfortable or compact")
        current["page_density"] = body["page_density"]
    current["revision"] += 1
    with db.transaction(con, write=True):
        con.execute("INSERT INTO meta (key, value) VALUES ('settings', ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(current),))
        rev = db.bump_revision(con)
        db.audit(con, actor, "settings_save", "settings", "settings", current, rev)
    return {"settings": current, "revision": current["revision"], "aggregate_rev": rev}


@db.atomic
def set_episode_media(con: sqlite3.Connection, episode_id: str, body: dict,
                      expected_revision, actor: str = "owner") -> dict:
    if expected_revision is None:
        raise MissingPrecondition("expected revision is required")
    cur = con.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if cur is None:
        raise BadInput("unknown episode")
    if int(expected_revision) != cur["revision"]:
        raise Conflict(cur["revision"])
    audio_url = body.get("audio_url", "absent")
    media_id = body.get("media_id", "absent")
    artifact_id = body.get("media_artifact_id", "absent")
    with db.transaction(con, write=True):
        alignment = body.get("alignment", "unknown")
        if alignment not in ("unknown", "verified", "mismatch"):
            raise BadInput("alignment must be unknown, verified, or mismatch")
        con.execute("UPDATE episodes SET media_edited=1, alignment=? WHERE id=?", (alignment, episode_id))
        if audio_url != "absent":
            if audio_url is not None and (not isinstance(audio_url, str) or not audio_url.startswith("https://")):
                raise BadInput("audio_url must be an HTTPS URL or null")
            con.execute("UPDATE episodes SET audio_url=? WHERE id=?", (audio_url, episode_id))
        if media_id != "absent":
            if media_id is not None:
                from . import media as media_mod
                if not isinstance(media_id, str) or not media_mod.known_media(media_id):
                    raise BadInput("unknown registered media id")
            con.execute("UPDATE episodes SET media_id=? WHERE id=?", (media_id, episode_id))
        if artifact_id != "absent":
            if artifact_id is not None:
                if not isinstance(artifact_id, str):
                    raise BadInput("media_artifact_id must be a registered id or null")
                from . import media as media_mod
                candidate = dict(cur, media_artifact_id=artifact_id, alignment=alignment)
                identity = media_mod.recording_identity(con, candidate)
                if media_mod.recording_proof(con, candidate, identity) is None:
                    raise BadInput("recording must match this episode's retained transcript and local bytes")
            con.execute("UPDATE episodes SET media_artifact_id=? WHERE id=?", (artifact_id, episode_id))
        con.execute("UPDATE episodes SET revision=revision+1, updated_at=? WHERE id=?",
                    (db.utcnow(), episode_id))
        if db.has_history_tables(con):
            db.record_episode_snapshot(con, episode_id, actor)
        if db.has_v8_tables(con):
            _citations.publish_episode_sources(con, episode_id, actor)
        rev = db.bump_revision(con)
        db.audit(con, actor, "episode_media", "episode", episode_id, {}, rev)
    return {"episode_id": episode_id, "revision": cur["revision"] + 1, "aggregate_rev": rev}


def backup_to(con: sqlite3.Connection, dest: Path) -> dict:
    import sqlite3 as _sql
    dest.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(str(dest))
    try:
        con.backup(target)
        revision = int(target.execute("SELECT value FROM meta WHERE key='aggregate_rev'").fetchone()[0])
    finally:
        target.close()
    return {"path": str(dest), "bytes": dest.stat().st_size, "aggregate_rev": revision}


def aggregate_rev(con: sqlite3.Connection) -> int:
    return db.aggregate_revision(con)
