"""Canonical Query2 / ResearchSelector1 contracts (K04).

Pure validation and selector logic for the B1 research contract. No I/O,
no database access: adapters supply the current view revision and workspace
context, and this module decides acceptance, narrowing, refresh and
export-basis equality. Invalid input raises QueryRejected (400-class);
stale or foreign counted scopes raise SelectorConflict (409) or
SelectorUnavailable (403/404-class) so adapters map status exactly.
"""
from __future__ import annotations

from datetime import date

from earwitness.app import search as _search

QUERY_VERSION = 2
SELECTOR_VERSION = 1
MODES = ("ads", "mentions", "episodes")
SORTS = ("newest", "oldest")
STATUSES = ("confirmed", "finding", "mention", "unresolved")
AUDIOS = ("available", "unavailable", "mismatch")
DATE_STATUSES = ("all", "known", "unknown")
SET_FIELDS = ("brands", "shows", "categories", "statuses", "audio")
SET_MAX = 200
IDS_MAX = 200
QUERY_MAX = 200
SUMMARY_VALUES = ("week", "category", "brand", "show", "status")
CLAUSE_KINDS = ("all_results", "summary_cell", "confirmed_occurrences")
COUNT_UNITS = ("distinct_results", "confirmed_occurrences")
PARKED_FIELDS = ("read_styles", "production_timings", "insertion_methods",
                 "placement_positions", "perspective")
QUERY_FIELDS = {"version", "mode", "q", "brands", "shows", "categories",
                "statuses", "audio", "date_status", "from", "to",
                "sort", "page", "ids", "self_promo", "has_clip"}
# Self-promo reads (validation plan A5) are left out of ads by default;
# "include" adds them, "only" lists just them. The default is never
# written into a normalized query, so stored queries keep their bytes.
SELF_PROMO_VALUES = ("exclude", "include", "only")


class QueryRejected(_search.FilterRejected):
    """A Query2/selector request is malformed: adapter maps to 400."""


class ExportTooLarge(QueryRejected):
    """A complete export over customer_query.EXPORT_RESULT_CAP: the hosted
    adapter maps it to 422 EXPORT_TOO_LARGE (other adapters: 400)."""


class SelectorConflict(_search.FilterRejected):
    """Counted scope basis changed: adapter maps to 409 VIEW_CHANGED."""


class SelectorUnavailable(_search.FilterRejected):
    """Scope lost or foreign: adapter maps to non-disclosing 403/404."""


def publication_day(value) -> str | None:
    """Valid publication-date partition key or None for unknown.

    Uses a valid ISO date prefix; malformed/missing dates are unknown.
    No midnight/timezone precision is invented for day-only metadata.
    """
    if not isinstance(value, str):
        return None
    prefix = value[:10]
    try:
        parsed = date.fromisoformat(prefix)
    except ValueError:
        return None
    return parsed.isoformat() if parsed.isoformat() == prefix else None


def _check_set_field(name: str, value) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise QueryRejected(f"{name} must be null, [], or a string array")
    if len(value) > SET_MAX:
        raise QueryRejected(
            f"{name} has too many values (max {SET_MAX}); narrow the selection")
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, str) or not entry:
            raise QueryRejected(f"{name} entries must be nonempty strings")
        if len(entry) > 200:
            raise QueryRejected(f"{name} entries must be at most 200 characters")
        if entry in seen:
            raise QueryRejected(f"{name} has a repeated value: {entry!r}")
        seen.add(entry)
    return list(value)


def normalize_query(raw: dict | None) -> dict:
    """Validate a CustomerQuery2 mapping; raise QueryRejected on violation.

    Set fields are null (Any), [] (explicit exact-empty), or unique
    nonempty strings (OR within, AND across). No unknown or parked
    field is silently dropped: every one is rejected with its name.
    """
    if not isinstance(raw, dict):
        raise QueryRejected("query must be an object")
    if raw.get("version") != QUERY_VERSION:
        raise QueryRejected(f"unsupported query version: {raw.get('version')!r}")
    unknown = set(raw) - QUERY_FIELDS
    if unknown:
        parked = sorted(set(unknown) & set(PARKED_FIELDS))
        if parked:
            raise QueryRejected(
                f"unsupported parked query fields: {parked}")
        raise QueryRejected(f"unknown query fields: {sorted(unknown)}")
    mode = raw.get("mode", "mentions")
    if mode not in MODES:
        raise QueryRejected(f"invalid mode value: {mode!r}")
    q = raw.get("q", "")
    if not isinstance(q, str):
        raise QueryRejected("q must be a string")
    if len(q) > QUERY_MAX:
        raise QueryRejected(f"q exceeds {QUERY_MAX} characters")
    normalized = {"version": QUERY_VERSION, "mode": mode, "q": q}
    for name in SET_FIELDS:
        allowed = {"statuses": STATUSES, "audio": AUDIOS}.get(name)
        values = _check_set_field(name, raw.get(name))
        if values is not None and allowed is not None:
            bad = [v for v in values if v not in allowed]
            if bad:
                raise QueryRejected(f"invalid {name} values: {bad}")
        normalized[name] = values
    date_status = raw.get("date_status", "all")
    if date_status not in DATE_STATUSES:
        raise QueryRejected(f"invalid date_status value: {date_status!r}")
    date_from = raw.get("from")
    date_to = raw.get("to")
    for name, value in (("from", date_from), ("to", date_to)):
        if value is None:
            continue
        if not isinstance(value, str) or publication_day(value) is None \
                or publication_day(value) != value[:10]:
            raise QueryRejected(f"{name} must be a valid ISO date string")
    if date_from and date_to and date_to[:10] < date_from[:10]:
        raise QueryRejected("to must not precede from")
    if date_status == "unknown" and (date_from is not None or date_to is not None):
        raise QueryRejected("date_status 'unknown' requires null date bounds")
    normalized["date_status"] = date_status
    normalized["from"] = date_from
    normalized["to"] = date_to
    sort = raw.get("sort", "newest")
    if sort not in SORTS:
        raise QueryRejected(f"invalid sort value: {sort!r}")
    normalized["sort"] = sort
    page = raw.get("page", 1)
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise QueryRejected("page must be a positive integer")
    normalized["page"] = page
    self_promo = raw.get("self_promo", "exclude")
    if self_promo not in SELF_PROMO_VALUES:
        raise QueryRejected(f"invalid self_promo value: {self_promo!r}")
    if self_promo != "exclude":
        normalized["self_promo"] = self_promo
    # REDESIGN S9: only the ads with a clip the caller can play. Written
    # only when true, so stored queries keep their bytes.
    has_clip = raw.get("has_clip", False)
    if not isinstance(has_clip, bool):
        raise QueryRejected("has_clip must be true or false")
    if has_clip:
        if mode != "ads":
            raise QueryRejected("has_clip applies to ads mode only")
        normalized["has_clip"] = True
    ids = raw.get("ids")
    if ids is None:
        normalized["ids"] = None
    elif ids == "__none__":
        normalized["ids"] = []
    elif isinstance(ids, list):
        if len(ids) > IDS_MAX:
            raise QueryRejected(
                f"ids has too many values (max {IDS_MAX}); use a selector scope")
        for item in ids:
            if not isinstance(item, str) or not item or len(item) > 200:
                raise QueryRejected("ids entries must be nonempty strings")
        if len(set(ids)) != len(ids):
            raise QueryRejected("ids has a repeated value")
        normalized["ids"] = list(ids)
    else:
        raise QueryRejected("ids must be null, [], or a string array")
    return normalized


def _check_clause(clause) -> dict:
    if not isinstance(clause, dict):
        raise QueryRejected("selector clause must be an object")
    kind = clause.get("kind")
    if kind not in CLAUSE_KINDS:
        raise QueryRejected(f"invalid selector clause kind: {kind!r}")
    if kind == "summary_cell":
        group_by = clause.get("group_by")
        key = clause.get("key")
        if group_by not in SUMMARY_VALUES:
            raise QueryRejected(f"invalid summary group_by: {group_by!r}")
        if not isinstance(key, str) or not key:
            raise QueryRejected("summary_cell clause needs a nonempty key")
        return {"kind": kind, "group_by": group_by, "key": key}
    if set(clause) - {"kind"}:
        raise QueryRejected("only summary_cell clauses carry parameters")
    return {"kind": kind}


def validate_selector(raw: dict | None, *, current_revision: str | None,
                      workspace_context: str | None, refresh: bool = False) -> dict:
    """Validate a ResearchSelector1 against the current authorized cohort.

    counted_cohort requires the selector view_revision to equal the
    current authorized-view revision and workspace_context to match the
    session; mismatch raises SelectorConflict (changed dataset) or
    SelectorUnavailable (foreign/lost scope). No mismatched rows are
    ever returned: callers only proceed on success.
    """
    if not isinstance(raw, dict):
        raise QueryRejected("selector must be an object or null")
    if raw.get("version") != SELECTOR_VERSION:
        raise QueryRejected(
            f"unsupported selector version: {raw.get('version')!r}")
    base = normalize_query(raw.get("base_query"))
    clause = _check_clause(raw.get("clause"))
    count_units = raw.get("count_units", "distinct_results")
    if count_units not in COUNT_UNITS:
        raise QueryRejected(f"invalid count_units: {count_units!r}")
    if clause["kind"] == "confirmed_occurrences" \
            and count_units != "confirmed_occurrences":
        raise QueryRejected("confirmed_occurrences clauses use confirmed_occurrences units")
    scope_mode = raw.get("scope_mode")
    if scope_mode not in ("counted_cohort", "live_definition"):
        raise QueryRejected(f"invalid scope_mode: {scope_mode!r}")
    view_revision = raw.get("view_revision")
    context = raw.get("workspace_context")
    if scope_mode == "live_definition":
        if view_revision is not None or context is not None:
            raise QueryRejected("live definitions carry no counted revision")
    else:
        if not isinstance(view_revision, str) or not view_revision:
            raise SelectorUnavailable("selector scope is unavailable")
        if not isinstance(context, str) or not context:
            raise SelectorUnavailable("selector scope is unavailable")
        if context != workspace_context:
            raise SelectorUnavailable("selector scope is unavailable")
        if view_revision != current_revision and not refresh:
            raise SelectorConflict("selector view changed; refresh required")
    return {"version": SELECTOR_VERSION, "base_query": base,
            "clause": clause, "count_units": count_units,
            "scope_mode": scope_mode, "view_revision": view_revision,
            "workspace_context": context}


def live_selector(base_query: dict, clause: dict,
                 count_units: str = "distinct_results") -> dict:
    """Build the live-definition (uncounted) form for saved queries/trackers."""
    base = normalize_query(base_query)
    checked = _check_clause(clause)
    if count_units not in COUNT_UNITS:
        raise QueryRejected(f"invalid count_units: {count_units!r}")
    return {"version": SELECTOR_VERSION, "base_query": base,
            "clause": checked, "count_units": count_units,
            "scope_mode": "live_definition", "view_revision": None,
            "workspace_context": None}


# What each self_promo value admits: regular reads, self-promo reads.
_SELF_PROMO_SCOPE = {"exclude": frozenset({"regular"}), "include": frozenset({"regular", "promo"}),
                     "only": frozenset({"promo"})}


def narrow_for_request(base: dict, request: dict) -> dict:
    """Intersect a request query with a selector base: narrow, never widen.

    Request page/sort control presentation only. A request set value must
    be null (keep base) or a subset of a non-null base; a non-empty
    request against an exact-empty base widens and is rejected. A
    differing result mode is rejected until the user leaves the cell.
    """
    if request["mode"] != base["mode"]:
        raise QueryRejected("request mode differs from the selector scope")
    merged = dict(base)
    for name in SET_FIELDS + ("ids",):
        wanted = request[name]
        if wanted is None:
            continue
        current = base[name]
        if current is None:
            merged[name] = wanted
            continue
        if wanted == [] or set(wanted) <= set(current):
            merged[name] = wanted
            continue
        raise QueryRejected(
            f"request {name} widens the selector scope")
    # Text predicates intersect: both term sets must match (AND).
    merged["q"] = (base["q"] + " " + request["q"]).strip()
    if request["date_status"] != base["date_status"] \
            or request["from"] != base["from"] \
            or request["to"] != base["to"]:
        raise QueryRejected("request date scope differs from the selector scope")
    # Self-promo scope intersects too. A request without it (normalized
    # "exclude" is indistinguishable from absent) keeps the base; a request
    # disjoint from the base ("only" against "exclude") is rejected, like a
    # differing date scope.
    wanted = request.get("self_promo")
    if wanted is not None:
        scope = _SELF_PROMO_SCOPE[base.get("self_promo", "exclude")] & _SELF_PROMO_SCOPE[wanted]
        if not scope:
            raise QueryRejected("request self_promo contradicts the selector scope")
        else:
            narrowed = next(k for k, v in _SELF_PROMO_SCOPE.items() if v == scope)
            merged.pop("self_promo", None)
            if narrowed != "exclude":
                merged["self_promo"] = narrowed
    if request.get("has_clip"):
        # REDESIGN S9: a clip filter only narrows.
        merged["has_clip"] = True
    merged["sort"] = request["sort"]
    merged["page"] = request["page"]
    return merged


def upcast_query_v1(raw: dict | None) -> tuple[dict, list[str]]:
    """Pure one-time V1-to-Query2 upcast with an equivalence/correction report.

    Returns (query2, notes). Notes distinguish unchanged_equivalent from
    documented_correctness_correction entries; unknown V1 shapes raise
    QueryRejected instead of broadening.
    """
    if not isinstance(raw, dict):
        raise QueryRejected("query must be an object")
    notes: list[str] = []
    version = raw.get("version", 1)
    if version != 1:
        raise QueryRejected(f"unsupported query version: {version!r}")
    unknown = set(raw) - {"version", "mode", "q", "brand", "show", "from",
                          "to", "status", "audio", "sort", "page", "ids"}
    if unknown:
        raise QueryRejected(f"unknown query fields: {sorted(unknown)}")
    mode = raw.get("mode", "ads")
    if mode not in MODES:
        raise QueryRejected(f"invalid mode value: {mode!r}")

    def _one_set(name: str, value, limit: int) -> list[str] | None:
        if value is None or value == "":
            notes.append(f"{name}: blank scalar upcast to null (Any)")
            return None
        if not isinstance(value, str):
            raise QueryRejected(f"legacy {name} must be a string")
        if len(value) > limit:
            raise QueryRejected(f"legacy {name} exceeds {limit} characters")
        return [value]

    brands = _one_set("brand", raw.get("brand", ""), 120)
    shows = _one_set("show", raw.get("show", ""), 64)
    status = raw.get("status", "")
    statuses = None if status in (None, "") else [status]
    if statuses is not None and status not in STATUSES:
        raise QueryRejected(f"invalid status value: {status!r}")
    audio = raw.get("audio", "")
    audios = None if audio in (None, "") else [audio]
    if audios is not None and audio not in AUDIOS:
        raise QueryRejected(f"invalid audio value: {audio!r}")
    date_from = raw.get("from", "") or None
    date_to = raw.get("to", "") or None
    for name, value in (("from", date_from), ("to", date_to)):
        if value is not None and (
                not isinstance(value, str) or publication_day(value) != value[:10]):
            raise QueryRejected(f"legacy {name} is not a valid ISO date: {value!r}")
    ids = raw.get("ids")
    if ids is None:
        ids_out = None
    elif ids == "" or ids == "__none__" or ids == []:
        notes.append("ids: legacy exact-empty upcast to []")
        ids_out = []
    elif isinstance(ids, list):
        ids_out = list(ids)
    else:
        raise QueryRejected("legacy ids must be absent, __none__, or a list")
    query = {"version": QUERY_VERSION, "mode": mode, "q": raw.get("q", ""),
             "brands": brands, "shows": shows, "categories": None,
             "statuses": statuses, "audio": audios, "date_status": "all",
             "from": date_from, "to": date_to,
             "sort": raw.get("sort", "newest"), "page": 1, "ids": ids_out}
    if raw.get("page", 1) != 1:
        notes.append("page: reopening starts at page 1")
    out = normalize_query(query)
    if not notes:
        notes.append("unchanged_equivalent: scalar semantics preserved")
    else:
        notes.append("documented_correctness_correction: see entries above")
    return out, notes
