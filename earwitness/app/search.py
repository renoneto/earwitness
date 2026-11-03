"""Strict ResearchFilterV1 validation: reject, never silently broaden.

The legacy browser filter drops unknown enum values and trims over-limit
input so old callers keep working. ResearchFilterV1 is the shared strict
contract for every new consumer (browser/API/MCP after cutover, watchlists,
summaries): unknown fields, invalid enums, NaN/infinity, impossible date
ranges, conflicting selectors, and over-limit values raise FilterRejected
(HTTP 400). Literal substring semantics are preserved: query is a plain
case-folded substring, never SQL/regex/FTS grammar.
"""
from __future__ import annotations

import datetime
import json
import math
import sqlite3

from . import citations as _citations

FILTER_VERSION = 1

ALLOWED_FIELDS = frozenset({
    "query", "sources", "review_states", "brand", "brand_basis",
    "brand_channel", "show_id", "show_query", "episode_id", "category",
    "published_from", "published_to", "include_unknown_dates",
    "source_version_id", "history", "sort", "pipeline", "record_kind",
    "creative_id", "assignment_state", "observation_status",
})

SOURCES = {"jev", "detector", "manual", "pipeline"}
REVIEW_STATES = {"unreviewed", "ad", "not_ad", "unsure"}
BRAND_BASES = {"confirmed", "incidental", "unknown", "suggested"}
BRAND_CHANNELS = {"spoken", "url", "code", "unspecified"}
HISTORIES = {"active", "all"}
SORTS = {"episode", "start", "brand", "published"}
RECORD_KINDS = {"candidate", "observation"}
OBSERVATION_STATUSES = {"proposed", "verified", "superseded", "rejected"}

QUERY_MAX = 200
ID_MAX = 200

DEFAULT_LIMIT = 20
HARD_LIMIT_MAX = 50


class FilterRejected(Exception):
    """A strict-filter violation; adapters map this to HTTP 400."""

    def __init__(self, message: str):
        super().__init__(message)
        self.status = 400


def _reject(message: str) -> FilterRejected:
    return FilterRejected(message)


def _check_str_list(value, name: str, allowed: set, limit: int = 50) -> list:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise _reject(f"{name} must be a string or list of strings")
    if len(value) > limit:
        raise _reject(f"{name} has too many values (max {limit})")
    out = []
    for item in value:
        if not isinstance(item, str):
            raise _reject(f"{name} values must be strings")
        if item not in allowed:
            raise _reject(f"invalid {name} value: {item!r}")
        out.append(item)
    return out


def _check_str(value, name: str, limit: int = ID_MAX) -> str:
    if not isinstance(value, str):
        raise _reject(f"{name} must be a string")
    if len(value) > limit:
        raise _reject(f"{name} exceeds {limit} characters")
    return value


def _check_date(value, name: str) -> str:
    if not isinstance(value, str):
        raise _reject(f"{name} must be an ISO date string")
    try:
        return datetime.date.fromisoformat(value).isoformat()
    except ValueError:
        raise _reject(f"{name} is not a valid ISO date: {value!r}")


def validate_filter(raw: dict | None) -> dict:
    """Validate a ResearchFilterV1 mapping; raise FilterRejected on any violation."""
    if not isinstance(raw, dict):
        raise _reject("filter must be an object")
    if raw.get("version") is not None and raw.get("version") != FILTER_VERSION:
        raise _reject(f"unsupported filter version: {raw.get('version')!r}")
    unknown = set(raw) - ALLOWED_FIELDS - {"version"}
    if unknown:
        raise _reject(f"unknown filter fields: {sorted(unknown)}")
    filt: dict = {"version": FILTER_VERSION}
    query = raw.get("query", "")
    if not isinstance(query, str):
        raise _reject("query must be a string")
    if len(query) > QUERY_MAX:
        raise _reject(f"query exceeds {QUERY_MAX} characters")
    filt["query"] = query
    filt["sources"] = _check_str_list(raw.get("sources", []), "sources", SOURCES)
    filt["review_states"] = _check_str_list(raw.get("review_states", []), "review_states",
                                            REVIEW_STATES)
    filt["brand"] = _check_str(raw.get("brand", ""), "brand", 120)
    filt["brand_basis"] = _check_str_list(raw.get("brand_basis", []), "brand_basis", BRAND_BASES)
    filt["brand_channel"] = _check_str_list(raw.get("brand_channel", []), "brand_channel",
                                            BRAND_CHANNELS)
    filt["show_id"] = _check_str(raw.get("show_id", ""), "show_id")
    filt["show_query"] = _check_str(raw.get("show_query", ""), "show_query")
    if filt["show_id"] and filt["show_query"]:
        raise _reject("show_id and show_query conflict; use one selector")
    filt["episode_id"] = _check_str(raw.get("episode_id", ""), "episode_id", 80)
    filt["category"] = _check_str(raw.get("category", ""), "category", 80)
    if raw.get("published_from") is not None and raw.get("published_from") != "":
        filt["published_from"] = _check_date(raw["published_from"], "published_from")
    else:
        filt["published_from"] = ""
    if raw.get("published_to") is not None and raw.get("published_to") != "":
        filt["published_to"] = _check_date(raw["published_to"], "published_to")
    else:
        filt["published_to"] = ""
    if filt["published_from"] and filt["published_to"] \
            and filt["published_to"] < filt["published_from"]:
        raise _reject("published_to must not precede published_from")
    include_unknown = raw.get("include_unknown_dates", True)
    if not isinstance(include_unknown, bool):
        raise _reject("include_unknown_dates must be a boolean")
    filt["include_unknown_dates"] = include_unknown
    filt["source_version_id"] = _check_str(str(raw.get("source_version_id", "")),
                                          "source_version_id", 30)
    history = raw.get("history", "active")
    if history not in HISTORIES:
        raise _reject(f"invalid history value: {history!r}")
    filt["history"] = history
    sort = raw.get("sort", "episode")
    if sort not in SORTS:
        raise _reject(f"invalid sort value: {sort!r}")
    filt["sort"] = sort
    if raw.get("pipeline") is not None and not isinstance(raw.get("pipeline"), bool):
        raise _reject("pipeline must be a boolean")
    filt["pipeline"] = bool(raw.get("pipeline", False))
    if raw.get("record_kind") is not None and raw.get("record_kind") != "":
        if raw["record_kind"] not in RECORD_KINDS:
            raise _reject(f"invalid record_kind value: {raw['record_kind']!r}")
        filt["record_kind"] = raw["record_kind"]
    else:
        filt["record_kind"] = ""
    filt["creative_id"] = _check_str(raw.get("creative_id", ""), "creative_id")
    filt["assignment_state"] = _check_str(raw.get("assignment_state", ""), "assignment_state", 30)
    if raw.get("observation_status") is not None and raw.get("observation_status") != "":
        if isinstance(raw["observation_status"], str):
            statuses = [raw["observation_status"]]
        else:
            statuses = raw["observation_status"]
        filt["observation_status"] = _check_str_list(statuses, "observation_status",
                                                     OBSERVATION_STATUSES)
    else:
        filt["observation_status"] = []
    return filt


def check_numeric(value, name: str) -> float:
    """Reject booleans, non-numbers, and NaN/infinity; return a finite float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value):
        raise _reject(f"{name} must be a finite number")
    return float(value)


def check_limit(limit) -> int:
    """Validate a caller-requested item count against the bounded envelope."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise _reject("limit must be an integer")
    if not 1 <= limit <= HARD_LIMIT_MAX:
        raise _reject(f"limit must be within 1..{HARD_LIMIT_MAX}")
    return limit


def literal_contains(haystack: str, needle: str) -> bool:
    """Literal Unicode substring match with case folding, no grammar."""
    if not needle:
        return True
    return needle.casefold() in haystack.casefold()


def _fold_with_map(text: str) -> tuple[str, list[int]]:
    """Case-fold text keeping the original index behind every folded char.

    Expansion folds (e.g. ß→ss) emit one map entry per folded char, so a
    folded span maps back to exact original offsets instead of corrupting
    them. The needle is folded the same per-character way for consistency.
    """
    folded: list[str] = []
    index_map: list[int] = []
    for i, ch in enumerate(text):
        for piece in ch.casefold():
            folded.append(piece)
            index_map.append(i)
    return ("".join(folded), index_map)


def _literal_spans(folded_hay: str, index_map: list[int],
                   folded_needle: str) -> list[tuple[int, int]]:
    """Non-overlapping literal spans in ORIGINAL coordinates (start, end)."""
    if not folded_needle:
        return []
    spans: list[tuple[int, int]] = []
    start = 0
    size = len(folded_needle)
    while True:
        at = folded_hay.find(folded_needle, start)
        if at < 0:
            return spans
        spans.append((index_map[at], index_map[at + size - 1] + 1))
        start = at + size


def show_identity(episode: dict) -> dict:
    """Stable show identity for one episode row.

    Prefers preserved source identity where the schema carries it; with the
    current columns the distinguishing basis is (kind, normalized title), so
    two feeds sharing kind+title still merge — recorded as a limitation, and
    distinct kinds sharing a title stay separate and explicit (ambiguous).
    Identity format "sh-<12hex>" is deterministic across runs.
    """
    import hashlib
    title = (episode.get("show_title") or "").strip()
    basis = "kind_title"
    key = f"{episode.get('kind') or ''}\0{title.casefold()}"
    show_id = "sh-" + hashlib.sha1(key.encode()).hexdigest()[:12]
    return {"show_id": show_id, "title": title, "basis": basis,
            "ambiguous": False}


def shows_with_title(con: sqlite3.Connection, title: str) -> list[dict]:
    """All episode rows sharing a normalized title (ambiguity inspection)."""
    needle = (title or "").strip().casefold()
    return [dict(r) for r in
            con.execute("SELECT id, kind, show_title FROM episodes").fetchall()
            if (r["show_title"] or "").strip().casefold() == needle]


def resolve_show_id(con: sqlite3.Connection, show_id: str) -> list[str]:
    """Map a stable show id to episode ids in deterministic order.

    Unknown ids reject (an explicit restriction to a nonexistent scope must
    not broaden); title collisions across kinds stay separate shows.
    """
    if not isinstance(show_id, str) or not show_id.startswith("sh-"):
        raise _reject(f"unknown show: {show_id!r}")
    matched = sorted(r["id"] for r in
                     con.execute("SELECT id, kind, show_title FROM episodes").fetchall()
                     if show_identity(dict(r))["show_id"] == show_id)
    if not matched:
        raise _reject(f"unknown show: {show_id!r}")
    return matched


def projection_revision(con: sqlite3.Connection, spec: dict) -> str:
    """Projection-aware view revision: hash of the caller-visible projection.

    spec: {"scope": "research"|"transcripts", "notes": bool,
           "history": "active"|"all"}. Only visible values feed the hash —
    a notes-only edit leaves a notes=False revision unchanged, while any
    visible change alters it. Counter-free, so private activity that never
    surfaces (watchlist saves, audits) cannot move it. Format "vr2-<12hex>".
    """
    import hashlib
    import json as _json
    scope = spec.get("scope", "research")
    include_notes = bool(spec.get("notes"))
    history = spec.get("history", "active")
    digest = hashlib.sha256()
    digest.update(_json.dumps({"scope": scope, "notes": include_notes,
                               "history": history},
                              sort_keys=True).encode())
    for row in con.execute("SELECT id, kind, show_title, ep_title, category, pub_date,"
                           " transcript_version_id FROM episodes ORDER BY id").fetchall():
        digest.update(repr([row["id"], row["kind"], row["show_title"], row["ep_title"],
                            row["category"], row["pub_date"],
                            row["transcript_version_id"]]).encode())
    for row in con.execute("SELECT id, source, episode_id, orig_start_s, orig_end_s,"
                           " excerpt, source_version_id, transcript_version_id"
                           " FROM evidence ORDER BY id").fetchall():
        digest.update(repr([row["id"], row["source"], row["episode_id"], row["orig_start_s"],
                            row["orig_end_s"], row["excerpt"], row["source_version_id"],
                            row["transcript_version_id"]]).encode())
    for row in con.execute("SELECT evidence_id, judgment, eff_start_s, eff_end_s, notes,"
                           " bounds_verified, revision FROM reviews ORDER BY evidence_id"
                           ).fetchall():
        visible = [row["evidence_id"], row["judgment"], row["eff_start_s"], row["eff_end_s"]]
        if include_notes:
            visible += [row["notes"], row["bounds_verified"], row["revision"]]
        digest.update(repr(visible).encode())
    for row in con.execute("SELECT evidence_id, brand, basis, channel, origin"
                           " FROM brand_assocs ORDER BY evidence_id, brand, channel").fetchall():
        digest.update(repr([row["evidence_id"], row["brand"], row["basis"],
                            row["channel"], row["origin"]]).encode())
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "evidence_source_membership" in tables:
        # Visibility depends on which source versions are active and on memberships.
        digest.update(repr([
            [r[0] for r in con.execute("SELECT id FROM source_versions WHERE is_active=1"
                                       " ORDER BY id")],
            [tuple(r) for r in con.execute("SELECT evidence_id, source_version_id, member FROM"
                                           " evidence_source_membership ORDER BY 1, 2")]]).encode())
    if history == "all":
        for table in ("evidence_history", "episode_history", "review_history",
                      "association_history", "campaign_history"):
            if table in tables:
                count = con.execute(f"SELECT COUNT(*), COALESCE(MAX(ROWID), 0) FROM {table}"
                                    ).fetchone()
                digest.update(repr([table, count[0], count[1]]).encode())
    return "vr2-" + digest.hexdigest()[:12]


def filter_digest_for(filt: dict, query: str, policy_tag: str = "") -> str:
    """Stable digest binding a cursor to its exact authorized query."""
    import hashlib
    import json as _json
    return hashlib.sha256(_json.dumps(
        {"filter": filt, "query": query, "policy": policy_tag},
        sort_keys=True, default=str).encode()).hexdigest()


def to_legacy_filter(filt: dict) -> dict:
    """Project a validated ResearchFilterV1 onto the legacy ops filter shape.

    Only valid for filters without selectors the legacy query path cannot
    execute; raises FilterRejected when a v4 extension (pipeline/record_kind/
    creative_id/observation_status/assignment_state) or a stable show id
    (B owns show identity) has no legacy equivalent. brand_channel and the
    published sort execute on the legacy path, so they project through.
    """
    for key in ("pipeline", "record_kind", "creative_id", "observation_status",
                "assignment_state"):
        if filt.get(key):
            raise _reject(f"{key} has no legacy equivalent; use the v4 query path")
    if filt.get("show_id"):
        raise _reject("show_id has no legacy equivalent yet; stable show "
                      "identity lands in package B")
    return {
        "q": filt["query"],
        "source": list(filt["sources"]) if filt["sources"] else [],
        "review": list(filt["review_states"]),
        "brand": filt["brand"],
        "brand_basis": list(filt["brand_basis"]),
        "brand_channel": list(filt["brand_channel"]),
        "show": filt["show_query"],
        "category": filt["category"],
        "episode": filt["episode_id"],
        "source_version": filt["source_version_id"],
        "pub_from": filt["published_from"],
        "pub_to": filt["published_to"],
        "include_unknown_dates": filt["include_unknown_dates"],
        "history": filt["history"],
        "sort": filt["sort"],
    }


# Legacy saved-search field names, in the shape ops.normalize_filter and the
# stored watchlist rows use. Pagination and envelope keys carry no filter
# meaning and are ignored by the upgrade decoder, never flagged.
_LEGACY_FIELD_MAP = (
    ("q", "query"),
    ("source", "sources"),
    ("review", "review_states"),
    ("brand", "brand"),
    ("brand_basis", "brand_basis"),
    ("brand_channel", "brand_channel"),
    ("show", "show_query"),
    ("category", "category"),
    ("episode", "episode_id"),
    ("pub_from", "published_from"),
    ("pub_to", "published_to"),
    ("source_version", "source_version_id"),
)
_LEGACY_IGNORED_KEYS = frozenset({
    "version", "filter_version", "page", "per_page", "history", "sort",
    "include_unknown_dates",
})


def annotate_show_ambiguity(shows: list[dict]) -> list[dict]:
    """Mark shows whose title maps to more than one stable identity."""
    from collections import Counter
    counts = Counter((s.get("title") or "").strip().casefold() for s in shows)
    for show in shows:
        show["ambiguous"] = counts[(show.get("title") or "").strip().casefold()] > 1
    return shows


# Evidence-scoped restrictions have no meaning over bare transcripts; asking
# for them there rejects instead of silently broadening the collection.
_TRANSCRIPT_REJECTED_FIELDS = ("sources", "review_states", "brand",
                               "brand_basis", "brand_channel")


def transcript_episodes(con: sqlite3.Connection, filt: dict) -> tuple[list[dict], int | None]:
    """Episode scope for transcript search in deterministic episode_id order.

    Applies episode-level V1 restrictions (episode_id, show_query, show_id,
    category, published range + unknown-date rule, transcript version pin,
    history mode). Evidence-only fields reject; they cannot scope a
    transcript collection. Returns (episodes, version_pin): the pin is an
    exact transcript version id (digit string) or None.
    """
    for key in _TRANSCRIPT_REJECTED_FIELDS:
        if filt.get(key):
            raise _reject(f"{key} does not scope transcript search")
    conds = ["1=1"]
    args: list = []
    if filt.get("episode_id"):
        conds.append("id=?")
        args.append(filt["episode_id"])
    if filt.get("show_query"):
        conds.append("instr(lower(show_title), lower(?)) > 0")
        args.append(filt["show_query"])
    if filt.get("category"):
        conds.append("category=?")
        args.append(filt["category"])
    if filt.get("show_id"):
        resolved = resolve_show_id(con, filt["show_id"])
        conds.append("id IN (%s)" % ",".join("?" * len(resolved)) if resolved else "1=0")
        args += resolved
    if filt.get("published_from"):
        conds.append("NULLIF(pub_date,'') IS NOT NULL AND pub_date>=?")
        args.append(filt["published_from"])
    if filt.get("published_to"):
        conds.append("NULLIF(pub_date,'') IS NOT NULL AND pub_date<=?")
        args.append(filt["published_to"])
    if not filt.get("include_unknown_dates", True):
        conds.append("NULLIF(pub_date,'') IS NOT NULL")
    pin: int | None = None
    if filt.get("source_version_id"):
        raw_pin = filt["source_version_id"]
        if not raw_pin.isdigit():
            raise _reject(f"transcript version pin must be an integer id: {raw_pin!r}")
        pin = int(raw_pin)
        key = con.execute("SELECT source_key FROM source_versions WHERE id=?",
                          (pin,)).fetchone()
        if key is None:
            raise _reject(f"unknown transcript version: {raw_pin!r}")
        if not str(key["source_key"]).startswith("transcript:"):
            raise _reject(f"version {raw_pin!r} is not a transcript version")
    rows = con.execute("SELECT id, kind, show_title, category, pub_date,"
                       " transcript_version_id FROM episodes WHERE " + " AND ".join(conds) +
                       " ORDER BY id", args).fetchall()
    return ([dict(r) for r in rows], pin)


def transcript_versions(con: sqlite3.Connection, episode_id: str,
                        history: str, pin: int | None = None) -> list[int]:
    """Pinned transcript version ids for one episode, oldest first.

    An exact pin restricts to that version when it belongs to this
    episode's transcript key; episodes the pin does not belong to
    contribute an empty scope (the pin scopes the collection, it never
    aborts the query). Under history=active a superseded pin yields an
    empty scope (it is not current) rather than broadening.
    """
    if pin is not None:
        row = con.execute("SELECT source_key FROM source_versions WHERE id=?",
                          (pin,)).fetchone()
        if row is None or row["source_key"] != f"transcript:{episode_id}":
            return []
        if history == "all":
            return [pin]
        current = con.execute("SELECT transcript_version_id FROM episodes WHERE id=?",
                              (episode_id,)).fetchone()
        return [pin] if current and current["transcript_version_id"] == pin else []
    if history != "all":
        row = con.execute("SELECT transcript_version_id FROM episodes WHERE id=?",
                          (episode_id,)).fetchone()
        return [row["transcript_version_id"]] if row and row["transcript_version_id"] else []
    return [r[0] for r in con.execute(
        "SELECT id FROM source_versions WHERE source_key=? ORDER BY version_no, id",
        (f"transcript:{episode_id}",)).fetchall()]


SEGMENT_TEXT_MAX = 600
SCAN_SEGMENT_CAP = 200000


def search_transcripts_scan(con: sqlite3.Connection, filt: dict, limit: int,
                            position: dict | None = None,
                            budget=None, ctx=None) -> dict:
    """One consistent transcript page over the authorized collection.

    Literal Unicode substring matching on the case-folded stream with
    original offsets preserved (expansion folds map back exactly).
    Cross-segment phrases return every covering segment, never a new ad
    interval; no judgment is inferred. Reads run inside one read
    transaction, so counts, items, and the view revision agree.
    position resumes strictly after {"episode_id","transcript_version_id",
    "seg_index","char_offset"}, in the scan's episode -> version -> segment
    -> character order (a position without a version is the pre-AD-06
    form: its segment and offset apply to every version of its episode).
    """
    query = filt.get("query", "")
    if not isinstance(query, str) or not query:
        raise _reject("transcript query must not be empty")
    if limit is not None and (not isinstance(limit, int) or limit < 1):
        raise _reject("limit must be a positive integer")
    if position is not None and (not isinstance(position, dict)
                                 or position.get("episode_id") is None):
        raise _reject("malformed page position")
    folded_needle, _ = _fold_with_map(query)
    episodes, version_pin = transcript_episodes(con, filt)
    if ctx is not None:
        admitted = []
        for episode in episodes:
            versions = transcript_versions(con, episode["id"], filt["history"], version_pin)
            if versions and ctx.disclosure_allowed(con, f"transcript:{episode['id']}", "metadata") \
                    and ctx.disclosure_allowed(con, f"transcript:{episode['id']}", "transcript"):
                admitted.append(episode)
        episodes = admitted
    digest = filter_digest_for(filt, query, getattr(ctx, "policy_identity", ""))
    nested = False
    try:
        con.execute("BEGIN")
    except sqlite3.OperationalError:
        nested = True  # already inside the caller's transaction: still consistent
    try:
        if ctx is None:
            revision = projection_revision(
                con, {"scope": "transcripts", "notes": False, "history": filt["history"]})
        else:
            from . import agent_contracts
            revision = agent_contracts._project_revision(con, "transcripts", False, filt["history"], ctx)
        hits: list[dict] = []
        total = 0
        scanned = 0
        truncated = False
        reason = None
        relation = "exact"
        started = position is None
        pos_ep = (position or {}).get("episode_id")
        pos_seg = (position or {}).get("seg_index", -1)
        pos_char = (position or {}).get("char_offset", -1)
        pos_version = (position or {}).get("transcript_version_id")
        pos_rank = None  # the cursor version's place among its episode's versions
        if position is not None and pos_version is not None:
            order = transcript_versions(con, pos_ep, filt["history"], version_pin)
            if pos_version not in order:
                raise _reject("malformed page position")
            pos_rank = order.index(pos_version)
        last_emitted: dict | None = None
        wire_bytes = 0
        done = False
        for ep in episodes:
            if done:
                break
            if budget is not None:
                budget.check("transcript search")
            for rank, version_id in enumerate(transcript_versions(
                    con, ep["id"], filt["history"], version_pin)):
                if done:
                    break
                at_rank = rank if pos_rank is None else pos_rank
                if ctx is not None:
                    from . import citations, access
                    source = citations._version_provenance(con, version_id)
                    if source is None:
                        continue
                    try:
                        ctx.authorize_disclosure(con, source["source_key"], ["metadata", "transcript"])
                    except access.AuthorizationError:
                        continue
                row = con.execute("SELECT payload FROM source_versions WHERE id=?",
                                  (version_id,)).fetchone()
                if row is None:
                    continue
                try:
                    payload = json.loads(row["payload"])
                except (ValueError, TypeError):
                    continue
                segments = payload.get("segments", []) \
                    if isinstance(payload, dict) else []
                seg_texts: list[str] = []
                stream: list[str] = []
                prov: list = []
                for seg in segments:
                    if not isinstance(seg, dict):
                        seg_texts.append("")
                        continue
                    scanned += 1
                    if scanned % 200 == 0 and budget is not None:
                        budget.check("transcript search")
                    if scanned > SCAN_SEGMENT_CAP:
                        truncated, reason, relation = True, "SCAN_CAP", "lower_bound"
                        done = True
                        break
                    text = seg.get("text", "") or ""
                    if not isinstance(text, str):
                        seg_texts.append("")
                        continue
                    seg_texts.append(text)
                    if stream:
                        stream.append(" ")
                        prov.append(None)
                    folded, index_map = _fold_with_map(text)
                    stream.append(folded)
                    prov.extend((len(seg_texts) - 1, i) for i in index_map)
                joined = "".join(stream)
                # Literal spans in stream coords, grouped by covering segment:
                # a cross-boundary phrase returns every covering segment with
                # its own clipped offsets, never a new ad interval.
                seg_spans: dict[int, list] = {}
                cursor = 0
                while True:
                    at = joined.find(folded_needle, cursor)
                    if at < 0:
                        break
                    end = at + len(folded_needle)
                    head, tail = prov[at], prov[end - 1]
                    cursor = end
                    if head is None or tail is None:
                        continue  # pure separator match: meaningless
                    for cover in range(head[0], tail[0] + 1):
                        start = head[1] if cover == head[0] else 0
                        stop = tail[1] + 1 if cover == tail[0] else len(seg_texts[cover])
                        seg_spans.setdefault(cover, []).append((start, stop))
                for index in sorted(seg_spans):
                    if done:
                        break
                    seg = segments[index] if index < len(segments) else {}
                    if not isinstance(seg, dict):
                        continue
                    text = seg_texts[index]
                    spans = seg_spans[index]
                    total += 1
                    if started:
                        resume_from = 0
                    elif (ep["id"], rank, index) < (pos_ep, at_rank, pos_seg):
                        continue
                    elif (ep["id"], rank, index) == (pos_ep, at_rank, pos_seg):
                        resume_from = pos_char + 1
                    else:
                        resume_from = 0
                    for chunk_text, win_start, win_end, match_spans, continuation \
                            in _chunk_segment(text, spans, resume_from):
                        if not started and (ep["id"], rank, index, win_start) <= (
                                pos_ep, at_rank, pos_seg, pos_char):
                            continue
                        first_span = match_spans[0] if match_spans else (None, None)
                        hit = {
                            "episode_id": ep["id"],
                            "transcript_version_id": version_id,
                            "segment_index": index,
                            "segment_id": seg.get("id", index),
                            "start_s": seg.get("start"),
                            "end_s": seg.get("end"),
                            "text": chunk_text,
                            "window_start": win_start,
                            "window_end": win_end,
                            "match_spans": [[s, e] for s, e in match_spans],
                            "char_start": first_span[0],
                            "char_end": first_span[1],
                            "text_complete": win_start == 0 and win_end >= len(text),
                            "continuation": continuation,
                            "match_basis": "transcript_segment",
                        }
                        size = len(json.dumps(hit)) * 2
                        if hits and (len(hits) >= limit
                                     or wire_bytes + size > 64 * 1024
                                     or wire_bytes >= 16 * 1024):
                            truncated = True
                            reason = "BYTE_BUDGET" if wire_bytes >= 16 * 1024 \
                                or wire_bytes + size > 64 * 1024 else "PAGE_LIMIT"
                            # Page/budget truncation stops the scan: later
                            # episodes are never counted, so the running
                            # total is a literal lower bound, never exact.
                            relation = "lower_bound"
                            done = True
                            break
                        pin = _citations.source_pin(con, version_id)
                        fetch = con.execute("SELECT fetch_date FROM episodes WHERE id=?",
                                            (ep["id"],)).fetchone()
                        hit["citation"] = {
                            "episode_id": ep["id"],
                            "transcript_version_id": version_id,
                            "source_pin": pin,
                            "fetch_date": fetch["fetch_date"] if fetch else None,
                        }
                        hits.append(hit)
                        wire_bytes += size
                        last_emitted = {"episode_id": ep["id"],
                                        "transcript_version_id": version_id,
                                        "seg_index": index, "char_offset": win_end - 1}
        return {"hits": hits, "total_matches": total, "total_relation": relation,
                "next_position": dict(last_emitted) if truncated and last_emitted else None,
                "view_revision": revision, "filter_digest": digest,
                "truncated": truncated, "truncation_reason": reason,
                "episodes_searched": len(episodes), "segments_scanned": scanned}
    finally:
        if not nested:
            con.execute("COMMIT")


def _chunk_segment(text: str, spans: list[tuple[int, int]],
                   resume_from: int) -> list[tuple[str, int, int, list, bool]]:
    """Split one matching segment into ≤SEGMENT_TEXT_MAX windows.

    The first window covers the first match at/after resume_from (with a
    100-char lead); later windows continue the same segment id/timestamps.
    Returns (chunk_text, window_start, window_end, match_spans, continuation)
    with match spans in absolute original coordinates.
    """
    size = len(text)
    windows: list[tuple[int, int]] = []
    first = next((i for i, (s, _) in enumerate(spans) if s >= resume_from), None)
    if first is None:
        # Resume point sits past every match: continue the tail verbatim.
        if resume_from < size:
            windows.append((resume_from, min(size, resume_from + SEGMENT_TEXT_MAX)))
    else:
        start = max(resume_from, spans[first][0] - 100)
        end = min(size, start + SEGMENT_TEXT_MAX)
        if end < spans[first][1]:
            end = spans[first][1]
        windows.append((start, end))
        while end < size:
            nxt = min(size, end + SEGMENT_TEXT_MAX)
            windows.append((end, nxt))
            end = nxt
    out = []
    for n, (ws, we) in enumerate(windows):
        spans_in = [(s, e) for s, e in spans if s < we and e > ws and e > resume_from]
        out.append((text[ws:we], ws, we, spans_in, n > 0 or resume_from > 0))
    return out


def permitted_evidence_match(item: dict, query: str, transcripts_allowed: bool,
                             notes_allowed: bool, interval_text: str = "",
                             note_text: str | None = None) -> bool:
    """Literal re-match over permitted fields only (field-permission-before-
    matching): case-folded substring over show/episode titles, association
    brands, excerpt, evidence attrs values (never key names), plus interval
    text only with transcripts permission and review notes only with notes
    permission. Empty query matches everything."""
    if not query:
        return True
    parts = [permitted_fields_text(item)]
    if transcripts_allowed:
        parts.append(interval_text or "")
    if notes_allowed and note_text:
        parts.append(note_text)
    return query.casefold() in " ".join(parts).casefold()


def permitted_fields_text(item: dict) -> str:
    """The fields permitted_evidence_match always reads, joined in its
    order: show/episode titles, association brands, excerpt, evidence
    attrs values: strings and numbers, nested, in key order; never key
    names, bools or None. (Ops items carry attrs parsed; accept both.)"""
    attrs = item.get("attrs")
    if isinstance(attrs, str):
        try:
            attrs = json.loads(attrs or "{}")
        except (ValueError, TypeError):
            attrs = {}
    if not isinstance(attrs, dict):
        attrs = {}
    # PODS: model rows are display-only
    return " ".join([item.get("show_title") or "", item.get("ep_title") or "",
                     " ".join(a.get("brand", "") for a in item.get("assocs", [])
                              if a.get("basis") != "model"),
                     item.get("excerpt") or "", " ".join(_attr_values(attrs))])


def _attr_values(value) -> list:
    if isinstance(value, dict):
        return [s for k in sorted(value) for s in _attr_values(value[k])]
    if isinstance(value, list):
        return [s for v in value for s in _attr_values(v)]
    return [] if isinstance(value, bool) or value is None else [str(value)]


def summarize_groups(items: list[dict], group_by: str, limit: int,
                     episode_lookup=None) -> dict:
    """Deterministic aggregates with exact units and denominators.

    items: evidence dicts (ops shape) already restricted to the authorized
    scope; deduplicated by id. group_by: source|brand|show|review|date.
    Counts are evidence records and never unique ads, spend, reach, or
    impressions. Overlapping brand groups each count their members: groups
    are explicitly non-exclusive, never summed as mutually exclusive.
    Groups sort by count desc, key asc; capped at limit with total_groups.
    """
    if group_by not in ("source", "brand", "show", "review", "date"):
        raise _reject(f"group_by must be source|brand|show|review|date")
    if not isinstance(limit, int) or limit < 1:
        raise _reject("limit must be a positive integer")
    seen: dict[str, dict] = {}
    for item in items:
        if item.get("id") in seen:
            continue
        seen[item["id"]] = item
    distinct = list(seen.values())
    groups: dict[str, dict] = {}

    def add(key: dict, label: str, evidence_id: str):
        name = json.dumps(key, sort_keys=True)
        entry = groups.get(name)
        if entry is None:
            entry = groups[name] = {"key": key, "label": label, "count": 0,
                                    "unit": "evidence records",
                                    "evidence_ids": []}
        entry["count"] += 1
        entry["evidence_ids"].append(evidence_id)

    for item in distinct:
        if group_by == "source":
            add({"source": item.get("source")}, str(item.get("source")), item["id"])
        elif group_by == "review":
            judgment = item.get("judgment") or "unreviewed"
            add({"review": judgment}, judgment, item["id"])
        elif group_by == "brand":
            # PODS: model rows are display-only
            assocs = [a for a in (item.get("assocs", []) or []) if a.get("basis") != "model"]
            if not assocs:
                add({"brand": None, "channel": None, "basis": None},
                    "unattributed", item["id"])
            for assoc in assocs:
                key = {"brand": assoc.get("brand"), "channel": assoc.get("channel"),
                       "basis": assoc.get("basis")}
                add(key, f"{assoc.get('brand')} [{assoc.get('channel')}/"
                         f"{assoc.get('basis')}]", item["id"])
        elif group_by == "show":
            info = episode_lookup(item.get("episode_id")) if episode_lookup else None
            if info is None:
                add({"show_id": None}, "unmapped", item["id"])
            else:
                identity = show_identity(info)
                add({"show_id": identity["show_id"]}, identity["title"] or "untitled",
                    item["id"])
        elif group_by == "date":
            info = episode_lookup(item.get("episode_id")) if episode_lookup else None
            pub = (info or {}).get("pub_date") or ""
            bucket = pub[:7] if pub else "unknown"
            add({"month": bucket}, bucket, item["id"])
    ordered = sorted(groups.values(),
                     key=lambda g: (-g["count"], json.dumps(g["key"], sort_keys=True)))
    for entry in ordered:
        del entry["evidence_ids"]
    return {"group_by": group_by, "groups": ordered[:limit],
            "total_groups": len(ordered), "truncated": len(ordered) > limit,
            "denominator": {"distinct_evidence": len(distinct),
                            "unit": "evidence records"},
            "exclusive": False}


def decode_legacy_filter(legacy: dict | None) -> tuple[dict, list]:
    """Upgrade-only decoder: legacy saved-search meaning to ResearchFilterV1.

    Returns (validated_v1_filter, problems). A clean decode preserves the
    legacy meaning exactly; any unknown field or invalid value is reported
    in problems (a needs_correction diagnosis) instead of being dropped or
    broadened. Callers must refuse evaluation while problems is non-empty.
    Package C applies this in the backed-up upgrade; live callers use it to
    gate save/evaluate paths.
    """
    if not isinstance(legacy, dict):
        return ({}, ["saved filter must be an object"])
    known = {old for old, _ in _LEGACY_FIELD_MAP} | _LEGACY_IGNORED_KEYS
    unknown = sorted(set(legacy) - known)
    problems = [f"unknown saved-filter field: {name!r}" for name in unknown]
    candidate: dict = {}
    for old, new in _LEGACY_FIELD_MAP:
        if old in legacy:
            candidate[new] = legacy[old]
    for key in ("history", "sort", "include_unknown_dates"):
        if key in legacy:
            candidate[key] = legacy[key]
    if "include_unknown_dates" not in candidate:
        candidate["include_unknown_dates"] = True
    if "history" not in candidate:
        candidate["history"] = "active"
    if "sort" not in candidate:
        candidate["sort"] = "episode"
    try:
        validated = validate_filter(candidate)
    except FilterRejected as exc:
        problems.append(str(exc))
        return ({}, problems)
    if problems:
        return ({}, problems)
    return (validated, [])
