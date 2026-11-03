"""P07 indexed search without changed semantics (plan section 12).

Materialized searchable text per evidence revision with match_basis and
source pins. Local literal matching moves into SQLite over materialized
folded text with instr (preserves token semantics, removes JSON parse +
N+1). FTS5 trigram candidate index ONLY when supported + measured useful,
always with exact folded-substring verification. Short-query and
unsupported-tokenizer paths fall back to the materialized scan under a
real deadline (never Python transcript reconstruction).

Ownership: P07P08 leaf. Consumes P00 tables (db.py schema untouched) and
the single shared strict filter (search.py). Default placement for
materialization SQL is here (and coverage.py), called from services;
P00 shared files are not edited by this module.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import time
from . import db as _db

DEFINITION_REVISION = 1
CURSOR_TTL_S = 15 * 60
STATEMENT_BUDGET = 8

MATCH_BASES = ("excerpt", "interval_text", "title", "association", "transcript_segment")


class IndexUnavailable(Exception):
    """Materialized index absent or corrupt; caller must surface rebuild state."""

    def __init__(self, message="search index unavailable; rebuild required"):
        super().__init__(message)
        self.code = "INDEX_UNAVAILABLE"


class StaleProjection(Exception):
    """Projection changed since cursor/selection; caller maps to RESULT_SET_CHANGED."""

    def __init__(self, message="projection changed; refetch without cursor"):
        super().__init__(message)
        self.code = "RESULT_SET_CHANGED"


def ensure_schema(con: sqlite3.Connection) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS evidence_search_docs ("
        " evidence_id TEXT PRIMARY KEY,"
        " folded_text TEXT NOT NULL DEFAULT '',"
        " match_basis TEXT NOT NULL DEFAULT '{}',"
        " source_pins TEXT NOT NULL DEFAULT '{}',"
        " data_rev INTEGER NOT NULL DEFAULT 0)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS transcript_segment_docs ("
        " episode_id TEXT NOT NULL,"
        " seg_index INTEGER NOT NULL,"
        " folded_text TEXT NOT NULL DEFAULT '',"
        " start_s REAL, end_s REAL,"
        " version_id INTEGER,"
        " PRIMARY KEY (episode_id, seg_index))"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS index_state ("
        " key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_search_docs_folded"
        " ON evidence_search_docs(folded_text)"
    )


def _fold(text: str) -> str:
    return (text or "").casefold()


def _aggregate_rev(con: sqlite3.Connection) -> int:
    try:
        from . import db as _db
        return int(_db.aggregate_revision(con))
    except Exception:
        row = con.execute(
            "SELECT value FROM meta WHERE key='aggregate_rev'"
        ).fetchone()
        try:
            return int(row["value"]) if row else 0
        except (TypeError, ValueError):
            return 0


def _index_state(con: sqlite3.Connection) -> tuple[int | None, str]:
    rows = con.execute(
        "SELECT key, value FROM index_state WHERE key IN ('data_revision','status')"
    ).fetchall()
    state = {r["key"]: r["value"] for r in rows}
    try:
        rev = int(state["data_revision"]) if "data_revision" in state else None
    except (TypeError, ValueError):
        rev = None
    return rev, state.get("status", "")


def _evidence_folded_text(con, ev_id, excerpt, show_title, ep_title,
                          assoc_text, interval_text) -> tuple[str, dict, dict]:
    folded = " ".join([
        _fold(excerpt), _fold(interval_text), _fold(show_title),
        _fold(ep_title), _fold(assoc_text),
    ])
    basis = {}
    if excerpt:
        basis["excerpt"] = _fold(excerpt)
    if interval_text:
        basis["interval_text"] = _fold(interval_text)
    if show_title or ep_title:
        basis["title"] = _fold(" ".join([show_title or "", ep_title or ""]))
    if assoc_text:
        basis["association"] = _fold(assoc_text)
    pins = {"evidence_id": ev_id}
    return folded, basis, pins


def _interval_text(con, episode_id, transcript_version_id,
                   start, end) -> str:
    if start is None or end is None:
        return ""
    try:
        from . import citations
        segments = citations._transcript_payload(con, transcript_version_id)
    except Exception as exc:
        raise IndexUnavailable("required transcript interval could not be read") from exc
    return " ".join(
        s.get("text", "") for s in segments
        if isinstance(s, dict)
        and s.get("start", 0) < end and s.get("end", 0) > start
    )


@_db.atomic
def rebuild_all(con: sqlite3.Connection) -> dict:
    """Publish complete projections atomically; bulk path marks stale first."""
    ensure_schema(con)
    con.execute(
        "INSERT OR REPLACE INTO index_state (key, value) VALUES ('status','rebuilding')"
    )
    rev = _aggregate_rev(con)
    with _db.transaction(con, write=True):
        con.execute("DELETE FROM evidence_search_docs")
        con.execute("DELETE FROM transcript_segment_docs")
        try:
            ev_rows = con.execute(
                "SELECT e.id, e.excerpt, e.episode_id, e.orig_start_s, e.orig_end_s,"
                " e.transcript_version_id, e.source_version_id,"
                " ep.show_title, ep.ep_title"
                " FROM evidence e JOIN episodes ep ON ep.id=e.episode_id"
            ).fetchall()
        except sqlite3.Error as exc:
            raise IndexUnavailable(f"index source unreadable: {exc}")
        assoc_map: dict[str, str] = {}
        try:
            # PODS: model rows are display-only
            for row in con.execute(
                "SELECT evidence_id, brand FROM brand_assocs WHERE basis<>'model'"
            ).fetchall():
                assoc_map.setdefault(row["evidence_id"], "")
                assoc_map[row["evidence_id"]] += " " + (row["brand"] or "")
        except sqlite3.Error as exc:
            raise IndexUnavailable("required attribution source could not be read") from exc
        for row in ev_rows:
            ev_id = row["id"]
            review = con.execute(
                "SELECT eff_start_s, eff_end_s FROM reviews WHERE evidence_id=?",
                (ev_id,),
            ).fetchone()
            start = review["eff_start_s"] if review and review["eff_start_s"] is not None \
                else row["orig_start_s"]
            end = review["eff_end_s"] if review and review["eff_end_s"] is not None \
                else row["orig_end_s"]
            interval = _interval_text(
                con, row["episode_id"], row["transcript_version_id"], start, end)
            folded, basis, pins = _evidence_folded_text(
                con, ev_id, row["excerpt"] or "", row["show_title"] or "",
                row["ep_title"] or "", (assoc_map.get(ev_id) or "").strip(), interval)
            pins["source_version_id"] = row["source_version_id"]
            pins["transcript_version_id"] = row["transcript_version_id"]
            con.execute(
                "INSERT INTO evidence_search_docs"
                " (evidence_id, folded_text, match_basis, source_pins, data_rev)"
                " VALUES (?,?,?,?,?)",
                (ev_id, folded, json.dumps(basis), json.dumps(pins), rev),
            )
        try:
            from . import citations
            ep_rows = con.execute(
                "SELECT id, transcript_version_id FROM episodes"
            ).fetchall()
            indexed_spans: dict[str, list] = {}
            for ep in ep_rows:
                try:
                    segments = citations._transcript_payload(con, ep["transcript_version_id"])
                except Exception as exc:
                    raise IndexUnavailable("required transcript source could not be read") from exc
                for idx, seg in enumerate(segments):
                    if not isinstance(seg, dict):
                        continue
                    con.execute(
                        "INSERT INTO transcript_segment_docs"
                        " (episode_id, seg_index, folded_text, start_s, end_s, version_id)"
                        " VALUES (?,?,?,?,?,?)",
                        (ep["id"], idx, _fold(seg.get("text", "")),
                         seg.get("start"), seg.get("end"),
                         ep["transcript_version_id"]),
                    )
                    try:
                        span = indexed_spans.setdefault(ep["id"], [None, None])
                        start, end = float(seg.get("start")), float(seg.get("end"))
                        if end > start >= 0:
                            span[0] = start if span[0] is None else min(span[0], start)
                            span[1] = end if span[1] is None else max(span[1], end)
                    except (TypeError, ValueError):
                        continue
            from . import coverage as _coverage
            for ep_id, (start, end) in indexed_spans.items():
                if start is None or end is None or end <= start:
                    continue
                ep_tv = next((r["transcript_version_id"] for r in ep_rows
                              if r["id"] == ep_id), None)
                _coverage.record_attestation(
                    con, ep_id, "index", (start, end), (start, end),
                    source_pin=(f"source-version:{ep_tv}"
                                if ep_tv is not None else f"index-data-rev:{rev}"),
                    producer="index-rebuild", producer_rev=int(rev or 0),
                    actor="index-rebuild")
        except sqlite3.Error as exc:
            raise IndexUnavailable("required transcript document or index attestation failed") from exc
        con.execute(
            "INSERT OR REPLACE INTO index_state (key, value) VALUES ('data_revision',?)",
            (str(rev),),
        )
        con.execute(
            "INSERT OR REPLACE INTO index_state (key, value) VALUES ('status','current')"
        )
    return {"data_revision": rev, "evidence_docs": len(ev_rows)}


def check_fresh(con: sqlite3.Connection) -> int:
    """Raise explicit rebuild/availability state; never return empty results."""
    try:
        indexed = con.execute("SELECT COUNT(*) c FROM evidence_search_docs").fetchone()["c"]
    except sqlite3.Error as exc:
        if "no such table" in str(exc).lower():
            ensure_schema(con)
            raise IndexUnavailable("search index not published; run rebuild")
        raise IndexUnavailable(f"search index unreadable: {exc}")
    rev, status = _index_state(con)
    if rev is None or status != "current":
        raise IndexUnavailable("search index not published; run rebuild")
    current = _aggregate_rev(con)
    if rev != current:
        raise StaleProjection(
            f"projection changed (indexed={rev} current={current}); refetch")
    if indexed == 0:
        # Empty corpus is valid only when evidence itself is empty.
        try:
            total = con.execute("SELECT COUNT(*) c FROM evidence").fetchone()["c"]
        except sqlite3.Error as exc:
            raise IndexUnavailable(f"search index unreadable: {exc}")
        if total:
            raise IndexUnavailable("search index empty with evidence present; rebuild")
    return rev


@_db.atomic
def update_on_mutation(con: sqlite3.Connection, evidence_id: str) -> None:
    """Transactional single-doc refresh after review/assoc mutations."""
    ensure_schema(con)
    with _db.transaction(con, write=True):
        row = con.execute(
            "SELECT e.id, e.excerpt, e.episode_id, e.orig_start_s, e.orig_end_s,"
            " e.transcript_version_id, e.source_version_id,"
            " ep.show_title, ep.ep_title"
            " FROM evidence e JOIN episodes ep ON ep.id=e.episode_id"
            " WHERE e.id=?",
            (evidence_id,),
        ).fetchone()
        if row is None:
            con.execute("DELETE FROM evidence_search_docs WHERE evidence_id=?",
                        (evidence_id,))
            return
        # PODS: model rows are display-only
        assoc_text = " ".join(
            r["brand"] for r in con.execute(
                "SELECT brand FROM brand_assocs WHERE evidence_id=? AND basis<>'model'",
                (evidence_id,)))
        review = con.execute(
            "SELECT eff_start_s, eff_end_s FROM reviews WHERE evidence_id=?",
            (evidence_id,),
        ).fetchone()
        start = review["eff_start_s"] if review and review["eff_start_s"] is not None \
            else row["orig_start_s"]
        end = review["eff_end_s"] if review and review["eff_end_s"] is not None \
            else row["orig_end_s"]
        interval = _interval_text(
            con, row["episode_id"], row["transcript_version_id"], start, end)
        folded, basis, pins = _evidence_folded_text(
            con, evidence_id, row["excerpt"] or "", row["show_title"] or "",
            row["ep_title"] or "", assoc_text.strip(), interval)
        pins["source_version_id"] = row["source_version_id"]
        pins["transcript_version_id"] = row["transcript_version_id"]
        rev = _aggregate_rev(con)
        con.execute(
            "INSERT OR REPLACE INTO evidence_search_docs"
            " (evidence_id, folded_text, match_basis, source_pins, data_rev)"
            " VALUES (?,?,?,?,?)",
            (evidence_id, folded, json.dumps(basis), json.dumps(pins), rev),
        )
        con.execute(
            "INSERT OR REPLACE INTO index_state (key, value) VALUES ('data_revision',?)",
            (str(rev),),
        )


def match_basis_for(con: sqlite3.Connection, evidence_id: str, needle_folded: str) -> str:
    row = con.execute(
        "SELECT match_basis FROM evidence_search_docs WHERE evidence_id=?",
        (evidence_id,),
    ).fetchone()
    if row is None:
        return "excerpt"
    try:
        basis = json.loads(row["match_basis"])
    except (json.JSONDecodeError, TypeError):
        return "excerpt"
    for key in ("excerpt", "interval_text", "title", "association"):
        text = basis.get(key, "")
        if needle_folded and needle_folded in text:
            return key
    return "excerpt"


def _try_trigram(con: sqlite3.Connection) -> bool:
    try:
        con.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS evidence_trigram"
            " USING fts5(evidence_id UNINDEXED, text, tokenize='trigram')"
        )
        return True
    except sqlite3.OperationalError:
        return False


def scoped_match(con: sqlite3.Connection, needle: str, deadline_s: float = 5.0,
                 use_trigram: bool = False, _fresh_rev: int | None = None) -> list[str]:
    """Scoped DB matching over materialized folded text with instr.

    FTS5 trigram is a candidate prefilter ONLY when supported; every
    candidate is verified with exact folded-substring instr. Short queries
    and unsupported tokenizers fall back to the materialized scan under a
    real deadline (never Python transcript reconstruction).
    """
    rev = _fresh_rev if _fresh_rev is not None else check_fresh(con)
    needle_folded = _fold(needle)
    start = time.monotonic()
    if not needle_folded:
        rows = con.execute("SELECT evidence_id FROM evidence_search_docs").fetchall()
        return [r["evidence_id"] for r in rows]
    candidates: list[str] | None = None
    if use_trigram and len(needle_folded) >= 3 and _try_trigram(con):
        try:
            con.execute("DELETE FROM evidence_trigram")
            for row in con.execute(
                "SELECT evidence_id, folded_text FROM evidence_search_docs"
            ).fetchall():
                con.execute(
                    "INSERT INTO evidence_trigram (evidence_id, text) VALUES (?,?)",
                    (row["evidence_id"], row["folded_text"]),
                )
            q = '"' + needle_folded.replace('"', '""') + '"'
            candidates = [
                r["evidence_id"] for r in con.execute(
                    "SELECT evidence_id FROM evidence_trigram WHERE text MATCH ?", (q,)
                ).fetchall()
            ]
        except sqlite3.Error:
            candidates = None
    if candidates is not None:
        out = []
        wanted = set(candidates)
        for row in con.execute(
            "SELECT evidence_id, folded_text FROM evidence_search_docs"
            " WHERE evidence_id IN (%s)" % ",".join("?" * len(wanted)) if wanted else
            "SELECT evidence_id, folded_text FROM evidence_search_docs WHERE 0",
            list(wanted),
        ).fetchall() if wanted else []:
            if time.monotonic() - start > deadline_s:
                raise TimeoutError("materialized scan exceeded deadline")
            if needle_folded in (row["folded_text"] or ""):
                out.append(row["evidence_id"])
        return out
    out = []
    for row in con.execute(
        "SELECT evidence_id FROM evidence_search_docs"
        " WHERE instr(folded_text, ?) > 0", (needle_folded,)
    ).fetchall():
        if time.monotonic() - start > deadline_s:
            raise TimeoutError("materialized scan exceeded deadline")
        out.append(row["evidence_id"])
    return out


def drop_brand_only_matches(con: sqlite3.Connection, matched_ids: list[str],
                            needle: str, brand_hidden) -> list[str]:
    """scoped_match ids minus brand_hidden ids (brands under review) that
    match only through their brand associations, not their excerpt,
    interval text or titles. Customer callers only."""
    needle_folded = _fold(needle)
    suspect = [i for i in matched_ids if i in (brand_hidden or ())]
    if not needle_folded or not suspect:
        return matched_ids
    dropped = set(suspect)
    for at in range(0, len(suspect), 500):
        chunk = suspect[at:at + 500]
        for row in con.execute("SELECT evidence_id, match_basis FROM evidence_search_docs"
                               " WHERE evidence_id IN (%s)" % ",".join("?" * len(chunk)),
                               chunk):
            try:
                basis = json.loads(row["match_basis"] or "{}")
            except (TypeError, ValueError):
                basis = {}
            if any(needle_folded in (basis.get(k) or "")
                   for k in ("excerpt", "interval_text", "title")):
                dropped.discard(row["evidence_id"])
    return [i for i in matched_ids if i not in dropped]


def paged_search(con: sqlite3.Connection, legacy_filt: dict,
                 matched_ids: list[str] | None, brand_hidden=None) -> dict:
    """Filtered COUNT + page over P00 evidence using shared ops WHERE.

    Reuses ops._filter_where for identical workspace/rights/version filter
    semantics; literal text restriction comes from materialized matched_ids
    (already exact folded-substring verified). Batched assoc retrieval keeps
    warm evidence pages within the statement budget. brand_hidden is the
    customer opt-in of ops.search_evidence (brands under review).
    """
    from . import ops as _ops
    filt = _ops.normalize_filter({**legacy_filt, "q": ""})
    where, args = _ops._filter_where(
        filt, exclude_shows=tuple(_ops.hidden_show_titles(con)), con=con,
        brand_hidden=brand_hidden)
    base = ("FROM evidence e JOIN episodes ep ON ep.id=e.episode_id"
            " LEFT JOIN reviews r ON r.evidence_id=e.id")
    extra = ""
    if matched_ids is not None:
        if not matched_ids:
            return {"filter": filt, "total": 0, "page": filt["page"],
                    "per_page": filt["per_page"], "items": []}
        extra = " AND e.id IN (%s)" % ",".join("?" * len(matched_ids))
        args = list(args) + list(matched_ids)
    total = con.execute(
        f"SELECT COUNT(*) c {base} WHERE {where}{extra}", args).fetchone()["c"]
    order_args: list = []
    if filt["sort"] == "brand":
        order, order_args = _ops.brand_sort_order(brand_hidden)
    else:
        order = {"episode": "e.episode_id, e.orig_start_s, e.id",
                 "start": "e.orig_start_s, e.id",
                 "published": "CASE WHEN NULLIF(ep.pub_date,'') IS NULL THEN 1 ELSE 0 END,"
                              " ep.pub_date, e.id"}[filt["sort"]]
    offset = (filt["page"] - 1) * filt["per_page"]
    rows = con.execute(
        f"SELECT e.*, ep.show_title, ep.ep_title, ep.category, r.judgment,"
        f" r.eff_start_s, r.eff_end_s, r.revision AS review_rev {base}"
        f" WHERE {where}{extra} ORDER BY {order} LIMIT ? OFFSET ?",
        list(args) + order_args + [filt["per_page"], offset]).fetchall()
    ids = [r["id"] for r in rows]
    assoc_map: dict[str, list] = {i: [] for i in ids}
    if ids:
        for a in con.execute(
            # PODS: model rows are display-only
            "SELECT evidence_id, brand, basis, origin, channel FROM brand_assocs"
            " WHERE evidence_id IN (%s) AND basis<>'model' ORDER BY brand" % ",".join("?" * len(ids)),
            ids,
        ).fetchall():
            assoc_map.setdefault(a["evidence_id"], []).append(dict(a))
    doc_map: dict[str, dict] = {}
    if ids:
        for doc in con.execute(
            "SELECT evidence_id, match_basis, source_pins FROM evidence_search_docs"
            " WHERE evidence_id IN (%s)" % ",".join("?" * len(ids)),
            ids,
        ).fetchall():
            doc_map[doc["evidence_id"]] = dict(doc)
    needle_folded = _fold(legacy_filt.get("q", ""))
    items = []
    for row in rows:
        d = dict(row)
        try:
            import json as _json
            d["attrs"] = _json.loads(d["attrs"]) if isinstance(d["attrs"], str) else d["attrs"]
        except (ValueError, TypeError):
            d["attrs"] = {}
        d["assocs"] = assoc_map.get(d["id"], [])
        basis = {}
        pins: dict = {}
        doc = doc_map.get(d["id"])
        if doc:
            try:
                import json as _json2
                basis = _json2.loads(doc["match_basis"]) if doc["match_basis"] else {}
                pins = _json2.loads(doc["source_pins"]) if doc["source_pins"] else {}
            except (ValueError, TypeError):
                basis, pins = {}, {}
        d["match_basis"] = next(
            (k for k in ("excerpt", "interval_text", "title", "association")
             if needle_folded and needle_folded in (basis.get(k) or "")),
            "excerpt",
        )
        d["source_pins"] = pins
        if brand_hidden and d["id"] in brand_hidden:
            basis.pop("association", None)
            d["match_basis"] = next(
                (k for k in ("excerpt", "interval_text", "title")
                 if needle_folded and needle_folded in (basis.get(k) or "")), "excerpt")
        items.append(_ops.hide_brand_guess(d, brand_hidden))
    return {"filter": filt, "total": total, "page": filt["page"],
            "per_page": filt["per_page"], "items": items}


# -- Signed policy-bound cursors (navigation, not authorization) -------------

def _cursor_key(workspace_id: str) -> bytes:
    return hashlib.sha256(f"p07-cursor:{workspace_id}".encode()).digest()


def encode_cursor(workspace_id: str, filter_digest: str, offset: int,
                  data_revision: int) -> str:
    payload = {
        "v": 1, "f": filter_digest, "o": int(offset),
        "r": int(data_revision), "e": int(time.time()) + CURSOR_TTL_S,
    }
    raw = json.dumps(payload, sort_keys=True).encode()
    sig = hmac.new(_cursor_key(workspace_id), raw, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(raw).decode() + "." + sig


def decode_cursor(workspace_id: str, cursor: str, filter_digest: str,
                  data_revision: int) -> int:
    try:
        raw_b64, sig = cursor.rsplit(".", 1)
        raw = base64.urlsafe_b64decode(raw_b64.encode())
    except (ValueError, TypeError):
        raise StaleProjection("invalid cursor; refetch without cursor")
    expect = hmac.new(_cursor_key(workspace_id), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, sig):
        raise StaleProjection("cursor signature mismatch; refetch")
    try:
        payload = json.loads(raw.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise StaleProjection("invalid cursor; refetch without cursor")
    if payload.get("e", 0) < time.time():
        raise StaleProjection("cursor expired; refetch without cursor")
    if payload.get("f") != filter_digest or payload.get("r") != int(data_revision):
        raise StaleProjection(
            "result set changed since cursor was issued; refetch")
    return int(payload.get("o", 0))


def filter_digest(filt: dict) -> str:
    return hashlib.sha256(
        json.dumps(filt, sort_keys=True, default=str).encode()).hexdigest()[:24]
