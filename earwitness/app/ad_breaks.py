"""Ads (decision 1, 2026-10-16): the customer unit. An ad is the customer
ad reads of one episode (the canonical episode, after alias folding) and
one transcript copy whose detection spans chain by overlap, split
wherever two different brands are named.

Overlap groups (chain): the group's reads sort by (start, end, ref); a
read joins the current group when its start is before the group's running
maximum end, else it opens a new group. Touching spans (start equal to
the maximum end) are two groups; there is no gap tolerance. A read with
no transcript copy or without both times is a group of its own, and an
earlier-copy read chains only with reads of the same copy.

Brand split (split): walk each group in that order. A read joins the
current ad when it names no brand or shares a brand with it; a read whose
named brands share nothing with the current ad's named brands opens a
new ad. One read is never split. "Named" (split_names) is a spoken brand
key customers see on the ad: a name under review and a web address or
code name nothing; a machine suggestion customers see does, known brand
or not (and stays unconfirmed). An unsplit group keeps its id;
a split piece's id is break_id of its own first read. Every ad carries
group_id (its overlap group's id) and group_ad_count (ads in that group),
the "Ad break" grouping label over different ads that play back to back.

Nothing is merged, hidden or stored. Ads are computed at read time over
the whole customer ad scope the caller can see (scope_index), so an ad's
identity, span and read count never depend on a filter: a filter only
decides which ads appear. Read counts keep counting reads. The wire names
stay "break" (ad_breaks, break_total, ad_break_id, ...); their values are
ads.

The ref an ad sorts ties by and its id hash take is the evidence id, the
one read identity every caller (full or identifier-only) shares.
"""
from __future__ import annotations

import hashlib
import sqlite3


def break_id(episode_id: str, transcript_version_id, first_key: str) -> str:
    """Deterministic break id: the canonical episode, the transcript copy
    and the first read in break order."""
    tv = "none" if transcript_version_id is None else transcript_version_id
    raw = f"{episode_id}|{tv}|{first_key}"
    return "brk:" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def _make(reads: list[dict], max_end) -> dict:
    first = reads[0]
    span = None if first["start"] is None or max_end is None else [first["start"], max_end]
    return {"id": break_id(first["episode_id"], first["transcript_version_id"], first["key"]),
            "episode_id": first["episode_id"],
            "transcript_version_id": first["transcript_version_id"],
            "span": span, "reads": reads}


def chain(reads: list[dict]) -> list[dict]:
    """The rule. reads: dicts with key (evidence id), episode_id (canonical),
    transcript_version_id, start and end (the stored detection span); any
    other keys ride along. Returns breaks {id, episode_id,
    transcript_version_id, span: [start, max_end] or None, reads (in break
    order)}, ordered by episode, copy (no copy last) and span start."""
    groups: dict[tuple, list] = {}
    out = []
    for read in reads:
        if read["transcript_version_id"] is None or read["start"] is None or read["end"] is None:
            out.append(_make([read], read["end"]))
            continue
        groups.setdefault((read["episode_id"], read["transcript_version_id"]), []).append(read)
    for members in groups.values():
        members.sort(key=lambda r: (r["start"], r["end"], r["key"]))
        current, max_end = None, None
        for read in members:
            if current is not None and read["start"] < max_end:
                current.append(read)
                max_end = max(max_end, read["end"])
                continue
            if current is not None:
                out.append(_make(current, max_end))
            current, max_end = [read], read["end"]
        out.append(_make(current, max_end))
    out.sort(key=_sort_key)
    return out


def _sort_key(b: dict) -> tuple:
    return (b["episode_id"], copy_order(b["transcript_version_id"]),
            b["span"] is None, (b["span"] or [0])[0], b["id"])


def split(groups: list[dict], names) -> list[dict]:
    """The brand split over chain() groups. names(read) -> the set of brand
    keys the read names. Returns ads (the chain() shape plus group_id and
    group_ad_count), in chain() order."""
    out = []
    for group in groups:
        pieces, current, named = [], None, set()
        for read in group["reads"]:
            keys = names(read)
            if current is None or (keys and named and keys.isdisjoint(named)):
                current, named = [read], set(keys)
                pieces.append(current)
            else:
                current.append(read)
                named |= keys
        if len(pieces) == 1:
            out.append(dict(group, group_id=group["id"], group_ad_count=1))
            continue
        for reads in pieces:
            ends = [r["end"] for r in reads if r["end"] is not None]
            out.append(dict(_make(reads, max(ends) if ends else None),
                            group_id=group["id"], group_ad_count=len(pieces)))
    out.sort(key=_sort_key)
    return out


def copy_order(transcript_version_id) -> tuple:
    """Sort key of a transcript copy: numeric ids in number order, any
    other id after them as text, no copy last (R1-23)."""
    tv = transcript_version_id
    if tv is None:
        return (2, 0, "")
    if isinstance(tv, (int, float)) and not isinstance(tv, bool):
        return (0, tv, "")
    return (1, 0, str(tv))


# -- customer surfaces ------------------------------------------------------

def canonical_map(con: sqlite3.Connection) -> dict[str, str]:
    """Alias episode id -> its canonical episode (one explicit hop, the
    target must exist), as customer._canonical_id folds it."""
    return {r[0]: r[1] for r in con.execute(
        "SELECT a.id, a.alias_of FROM episodes a JOIN episodes t ON t.id=a.alias_of")}


def _current_copies(con: sqlite3.Connection) -> dict[str, object]:
    return {r[0]: r[1] for r in con.execute("SELECT id, transcript_version_id FROM episodes")}


def read_of(m: dict, canonical: dict, **extra) -> dict:
    """One rule input from a customer ad match (customer._match_evidence)
    or a light row's match (row["_m"])."""
    ev = m["ev"]
    brands = []
    for assoc in m.get("assocs") or []:
        # PODS: model rows are display-only
        if assoc.get("brand") and assoc.get("basis") != "model":
            # Confirmed on the read rows' basis (customer read block): a
            # confirmed attribution, whatever the review status (R1-11).
            brands.append((assoc, assoc.get("basis") == "confirmed"))
    return {"key": ev["id"], "episode_id": canonical.get(ev["episode_id"], ev["episode_id"]),
            "actual_episode_id": ev["episode_id"],
            "transcript_version_id": ev.get("transcript_version_id"),
            "start": ev.get("orig_start_s"), "end": ev.get("orig_end_s"),
            "assocs": brands, **extra}


_URL_CHANNELS = ("url", "code")


def _read_keys(aliases: dict, read: dict):
    """(key, confirmed, assoc) per brand association of one rule input, in
    order: the canonical brand keys _decorate shows (a read with Brand
    under review arrives with none)."""
    from . import ad_reads as _ad
    for assoc, confirmed in read["assocs"]:
        key = _ad.assoc_key(aliases, assoc)
        if key:
            yield key, confirmed, assoc


def split_names(con: sqlite3.Connection):
    """names() for split: exactly the brand keys _decorate shows for a read
    (_read_keys; a name under review arrives with none), minus a web
    address or code (channel url/code), which never names one. A machine
    suggestion customers see names its brand, known or not, and stays
    unconfirmed."""
    from . import ad_reads as _ad
    aliases = _ad.alias_map(con)

    def names(read: dict) -> set:
        return {key for key, _confirmed, assoc in _read_keys(aliases, read)
                if assoc.get("channel") not in _URL_CHANNELS}
    return names


def ads(con: sqlite3.Connection, reads: list[dict]) -> list[dict]:
    """The rule: overlap groups, then the brand split (undecorated). A read
    the ad rules gave display bounds (R6 keeper, R7 trim:
    ad_reads.rule_bounds) is chained by those bounds, so a split part that
    lies in the keeper's detected span is an ad of its own."""
    from . import ad_reads as _ad
    bounds = _ad.rule_bounds(con)
    if bounds:
        reads = [dict(r, start=bounds[r["key"]][0], end=bounds[r["key"]][1])
                 if r["key"] in bounds else r for r in reads]
    return split(chain(reads), split_names(con))


def _decorate(con: sqlite3.Connection, breaks: list[dict]) -> list[dict]:
    """Add each break's brands ({key, display, confirmed}: the canonical
    brands its reads name by attribution, in order of first appearance; a
    read with Brand under review names nothing) and whether it sits on its
    episode's current transcript. Names follow the customer display list
    (customer.customer_aliases)."""
    from . import ad_reads as _ad
    from . import customer as _customer
    aliases = _customer.customer_aliases(con)
    displays: dict[str, str] = {}
    copies = _current_copies(con)
    for brk in breaks:
        brands: dict[str, dict] = {}
        for read in brk["reads"]:
            for key, confirmed, _assoc in _read_keys(aliases, read):
                if key not in displays:
                    displays[key] = _ad.brand_display(con, key, aliases)[0]
                slot = brands.setdefault(key, {"key": key, "display": displays[key],
                                               "confirmed": False})
                slot["confirmed"] = slot["confirmed"] or confirmed
        brk["brands"] = list(brands.values())
        tv = brk["transcript_version_id"]
        brk["on_current_transcript"] = tv is not None and \
            copies.get(brk["reads"][0]["actual_episode_id"]) == tv
    return breaks


def breaks_of_matches(con: sqlite3.Connection, matches: list[dict]) -> list[dict]:
    """The decorated breaks of customer ad matches (every read of each
    episode they hold: an episode or show page)."""
    canonical = canonical_map(con)
    return _decorate(con, ads(con, [read_of(m, canonical) for m in matches]))


def index_of_rows(con: sqlite3.Connection, rows: list[dict]) -> dict[str, dict]:
    """Evidence id -> decorated break, over the ads light rows of the
    whole customer ad scope (customer._light_match). Each read keeps
    has_clip: whether the caller can play its clip (the row's read block;
    Plan A by the excerpt clip rule)."""
    canonical = canonical_map(con)
    reads = [read_of(row["_m"], canonical, pub_date=row.get("pub_date") or "",
                     has_clip=bool((row.get("read") or {}).get("has_clip")))
             for row in rows if row.get("record_kind") == "evidence" and row.get("_m")]
    index: dict[str, dict] = {}
    for brk in _decorate(con, ads(con, reads)):
        for read in brk["reads"]:
            index[read["key"]] = brk
    return index


def scope_index(con: sqlite3.Connection, ctx=None) -> dict[str, dict]:
    """index_of_rows over the whole customer ad scope ctx can see (the
    rows of an unfiltered Search Ads), computed once per shared_scans or
    shared_view_revisions pass for one ctx (the pass memo
    customer_query.shared_view_revision uses), and once per data state
    and disclosure class across requests (customer_query.class_memo:
    the Brand page's three reads used to build it three times,
    PERF-BRAND-ALERT). Callers must not mutate the index."""
    from . import customer as _customer
    from . import customer_contracts as _contracts
    from . import customer_query as _query
    shared = _query._SHARED_SCANS.get()
    memo = shared[1] if shared is not None and shared[0] is con else None
    key = ("ad_breaks", None if ctx is None else id(ctx))
    if _customer.model_names_on():
        key = ("model-names",) + key  # PODS: never shared with the plain view
    if memo is not None:
        entry = memo.get(key)
        if entry is not None and entry[1] is ctx:
            return entry[0]

    def compute(c):
        criteria = _query._v1_criteria(_contracts.normalize_query({"version": 2, "mode": "ads"}))
        rows = _customer._light_match(con, criteria, c,
                                      transcripts=_query.pass_transcripts(con))["rows"]
        return index_of_rows(con, rows)
    # READ-CACHE: one index per disclosure class, not per caller (about
    # 1.8 MB pickled on the host-size sample).
    index = _query.class_memo(con, ctx, ("ad-breaks-scope",), compute, default=True,
                              state=_query.research_view_state)
    if memo is not None:
        memo[key] = (index, ctx)
    return index


def index_for_reads(con: sqlite3.Connection, ctx, evidence_ids) -> dict[str, dict]:
    """scope_index restricted to the canonical episodes of the given reads:
    an ad is the reads of one canonical episode, so each of these reads
    gets the same ad as in scope_index, without scanning the whole customer
    ad scope (the Alerts list and an alert's page, DEMO2 pass 5). A read
    outside the scope is in neither index."""
    from . import customer as _customer
    from . import customer_contracts as _contracts
    from . import customer_query as _query
    ids = sorted({str(e) for e in evidence_ids if e})
    if not ids:
        return {}
    canonical = canonical_map(con)
    episodes: set[str] = set()
    for start in range(0, len(ids), 500):
        batch = ids[start:start + 500]
        for row in con.execute("SELECT episode_id FROM evidence WHERE id IN (%s)"
                               % ",".join("?" * len(batch)), batch):
            # Both ids: an alias whose target the viewer may not see is a
            # canonical episode of its own in the scope (DEMO2-R5-01).
            episodes.add(row[0])
            episodes.add(canonical.get(row[0], row[0]))
    if not episodes:
        return {}
    criteria = _query._v1_criteria(_contracts.normalize_query({"version": 2, "mode": "ads"}))
    rows = _customer._light_match(con, criteria, ctx, episodes=episodes,
                                  transcripts=_query.pass_transcripts(con))["rows"]
    return index_of_rows(con, rows)


def index_of_ad_rows(con: sqlite3.Connection, rows: list[dict]) -> dict[str, dict]:
    """Evidence id -> ad (undecorated) over compact ad rows shaped like
    customer._customer_ad_rows (orig_start_s/orig_end_s, `_assocs`; a
    Brand under review row has none): the campaign checks' one pass."""
    canonical = canonical_map(con)
    reads = [read_of({"ev": row, "assocs": row.get("_assocs") or []}, canonical) for row in rows]
    return {read["key"]: ad for ad in ads(con, reads) for read in ad["reads"]}


def count(index: dict[str, dict], evidence_ids) -> int:
    """Distinct ads among the given reads (a read outside the customer
    ad scope, e.g. self-promotion, belongs to none)."""
    return len({index[e]["id"] for e in evidence_ids if e in index})


def evidence_id(item: dict) -> str | None:
    if item.get("record_kind") != "evidence":
        return None
    return item.get("evidence_id") or (item.get("_m") or {}).get("ev", {}).get("id")
