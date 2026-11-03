"""P08 coverage-aware competitive intelligence (plan section 13).

Coverage/count definition v1 lives here. Every summary echoes definition
revision, requested scope, selected snapshots, required stages, successful
interval union, exclusions, and review predicate. Snapshot selection picks
the latest requested snapshot per logical episode by fetch-start then
stable fetch ID BEFORE success consideration; never substitutes an older
success for a failed latest. Explicit multi-snapshot mode labels summed
time snapshot_audio_hours.

Units (never converted): searchable-transcript coverage,
automated-observation coverage, covered audio hours, distinct covered
episodes (full-interval only; partial listed separately), reviewed
occurrences (resolved IDs, current non-superseded reviewed_ad in selected
snapshots), brand comparison (reviewed confirmed assoc on selected
channel), proposals/candidates/creatives as separate units.

Comparison views use the DESIGN.md toolbar/table/definition-list
vocabulary with textual status beside results; no trend charts or
pass-fail cards, no spend/reach/market-share invention.
"""
from __future__ import annotations

import hashlib
import math
import sqlite3

from . import db as _db

DEFINITION_REVISION = 1

REQUIRED_STAGES = ("transcription", "screening", "observation_publication", "index")

# Actual pipeline job stages plus publication/index markers. Coverage's
# semantic stages map onto these recorded names; invented stage names are
# rejected at the write path.
ATTESTATION_STAGES = ("fetch", "transcribe", "screen", "refine",
                      "observation_publication", "index")
ATTESTATION_STATUS = ("success", "failed")
STAGE_GROUPS = {"transcription": ("transcribe",),
                "screening": ("screen",),
                "observation_publication": ("observation_publication",),
                "index": ("index",)}
EPS = 1e-6
# Stored durations are one-decimal minutes (a 6 s step), so the true end of
# an episode is known only to within half that step. The one tolerance for
# "reaches the stored duration": the legacy coverage import and the
# placement reader use it only where the requested end IS that rounded
# stored duration; every other comparison keeps EPS.
DURATION_STEP_S = 6.0
DURATION_TOLERANCE_S = DURATION_STEP_S / 2


def duration_tolerance(duration_min) -> float:
    """DURATION_TOLERANCE_S for a stored duration on the 0.1-min grid (a
    rounded duration); EPS for any other (a measured duration, e.g. an
    upload's): only a rounded end is uncertain."""
    if isinstance(duration_min, bool) or not isinstance(duration_min, (int, float)) \
            or not math.isfinite(duration_min) or duration_min <= 0:
        return EPS
    tenths = float(duration_min) * 10.0
    return DURATION_TOLERANCE_S if abs(tenths - round(tenths)) < 1e-6 else EPS


def covers_to_end(intervals, req_start: float, req_end: float, tolerance: float = EPS) -> bool:
    """The intervals cover [req_start, req_end - tolerance] (gaps inside it
    allowed only up to EPS): the tolerance forgives an uncertain end, never
    a gap in the middle."""
    end = req_end - max(tolerance, EPS)
    reach = req_start
    for s, e in sorted(intervals):
        if e <= reach:
            continue
        if s > reach + EPS:
            return False
        reach = e
        if reach >= end:
            return True
    return reach >= end


def _require_writer(ctx, operation: str) -> None:
    """Attestations record production processing proof; readers never mint it.

    A body cannot claim a processing actor: with an AccessContext present,
    workspace membership is required. Bare internal callers pass ctx=None
    with an explicit pipeline/operator actor string.
    """
    if ctx is not None:
        ctx.require_member(operation)
        from .access import require_customer_mutation
        require_customer_mutation(ctx, operation)


def _finite_seconds(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number of seconds")
    if not math.isfinite(float(value)):
        raise ValueError(f"{name} must be a finite number of seconds")
    return float(value)


def has_attestation_schema(con: sqlite3.Connection) -> bool:
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    return "coverage_attestations" in tables


def _require_attestation_schema(con: sqlite3.Connection) -> None:
    if not has_attestation_schema(con):
        raise RuntimeError("coverage attestation schema is not prepared:"
                           " run setup/explicit upgrade first")


def ensure_attestation_schema(con: sqlite3.Connection) -> None:
    """Setup/mutation-only DDL (K01). No commit; reads never call this."""
    con.execute(
        "CREATE TABLE IF NOT EXISTS coverage_attestations ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " workspace_id TEXT NOT NULL DEFAULT 'local',"
        " episode_id TEXT NOT NULL REFERENCES episodes(id),"
        " source_pin TEXT NOT NULL DEFAULT '',"
        " source_identity INTEGER,"
        " stage TEXT NOT NULL DEFAULT '',"
        " req_start_s REAL NOT NULL DEFAULT 0.0,"
        " req_end_s REAL NOT NULL DEFAULT 0.0,"
        " succ_start_s REAL NOT NULL DEFAULT 0.0,"
        " succ_end_s REAL NOT NULL DEFAULT 0.0,"
        " status TEXT NOT NULL DEFAULT 'success',"
        " producer TEXT NOT NULL DEFAULT '',"
        " producer_rev INTEGER NOT NULL DEFAULT 0,"
        " actor TEXT NOT NULL DEFAULT '',"
        " recorded_at TEXT NOT NULL)")
    cols = {r[1] for r in con.execute("PRAGMA table_info(coverage_attestations)")}
    if "source_identity" not in cols:
        con.execute("ALTER TABLE coverage_attestations ADD COLUMN source_identity INTEGER")


def record_attestation(con: sqlite3.Connection, episode_id: str, stage: str,
                       requested: tuple, successful: tuple,
                       source_pin: str = "", producer: str = "",
                       producer_rev: int = 0, actor: str = "",
                       status: str = "success", ctx=None,
                       recorded_at: str | None = None) -> int:
    """Persist one minimal coverage attestation at a stage transaction.

    Callers are the actual stage/publication transactions (intake,
    screening persist, observation publication, index rebuild), never
    readers. A review, candidate row, or observation window alone is not
    whole-episode proof: only the recorded interval counts, and a later
    failed attestation supersedes earlier success for that stage.

    Each row is bound to the episode's authoritative current
    transcript_version_id at write time. Readers that compute current
    coverage match on that identity, so proof recorded against a
    superseded source never unions into current coverage.
    """
    from . import db as _db
    _require_writer(ctx, "record_coverage")
    _require_attestation_schema(con)
    if not isinstance(episode_id, str) or not episode_id:
        raise ValueError("episode_id must be a nonempty string")
    ep_row = con.execute("SELECT transcript_version_id FROM episodes WHERE id=?",
                         (episode_id,)).fetchone()
    if ep_row is None:
        raise ValueError("unknown episode for coverage attestation")
    source_identity = ep_row[0]
    if stage not in ATTESTATION_STAGES:
        raise ValueError(f"unknown coverage stage: {stage!r}")
    if status not in ATTESTATION_STATUS:
        raise ValueError(f"unknown attestation status: {status!r}")
    try:
        req_start = _finite_seconds(requested[0], "req_start_s")
        req_end = _finite_seconds(requested[1], "req_end_s")
        succ_start = _finite_seconds(successful[0], "succ_start_s")
        succ_end = _finite_seconds(successful[1], "succ_end_s")
    except (IndexError, TypeError):
        raise ValueError("requested/successful must be (start_s, end_s) pairs")
    if not (req_start >= 0 and req_end > req_start):
        raise ValueError("requested interval must satisfy 0 <= start < end")
    if not (succ_start >= req_start - EPS and succ_end <= req_end + EPS
            and succ_end > succ_start):
        raise ValueError("successful interval must lie inside the requested interval")
    if not isinstance(source_pin, str) or not source_pin.strip():
        raise ValueError("source_pin must be a nonempty source/fetch pin")
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("actor must be a nonempty pipeline/operator actor")
    if actor.strip().lower() in ("reader", "guest", "anonymous"):
        raise ValueError("readers cannot record coverage attestations")
    cur = con.execute(
        "INSERT INTO coverage_attestations (workspace_id, episode_id, source_pin,"
        " source_identity, stage, req_start_s, req_end_s, succ_start_s, succ_end_s,"
        " status, producer, producer_rev, actor, recorded_at)"
        " VALUES ('local', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (episode_id, source_pin.strip(), source_identity, stage, req_start, req_end,
         succ_start, succ_end, status, str(producer or "")[:200],
         int(producer_rev or 0), actor.strip(),
         recorded_at or _db.utcnow()))
    return cur.lastrowid


_UNSET: object = object()


def attestation_union(con: sqlite3.Connection, episode_id: str,
                      stages: tuple,
                      source_identity=_UNSET,
                      exclude_producers: tuple = ()) -> tuple[list, str | None]:
    """Union successful intervals for stage names; latest failure wins.

    Returns (union_intervals, failure_reason). When the latest attestation
    across the group failed, earlier success is not reused: the union is
    empty and the reason names the failed stage.

    When source_identity is given, only rows bound to that source
    identity participate: proof recorded against a superseded source is
    never unioned into current coverage, and a failure recorded against
    a superseded source does not fail current coverage. Interval
    holes/overlap merging and the no-fabricated-proof invariants are
    unchanged within the matched set.

    exclude_producers: producer prefixes whose rows do not count here (proof
    whose own evidence is no longer in force, e.g. a re-screen run entry the
    active run no longer holds).
    """
    _require_attestation_schema(con)
    if source_identity is _UNSET:
        rows = [dict(r) for r in con.execute(
            "SELECT stage, succ_start_s, succ_end_s, status, producer FROM coverage_attestations"
            " WHERE workspace_id='local' AND episode_id=? AND stage IN"
            f" ({','.join('?' * len(stages))}) ORDER BY id",
            (episode_id, *stages)).fetchall()]
    else:
        rows = [dict(r) for r in con.execute(
            "SELECT stage, succ_start_s, succ_end_s, status, producer FROM coverage_attestations"
            " WHERE workspace_id='local' AND episode_id=? AND stage IN"
            f" ({','.join('?' * len(stages))}) AND source_identity IS ? ORDER BY id",
            (episode_id, *stages, source_identity)).fetchall()]
    if exclude_producers:
        rows = [r for r in rows if not str(r.get("producer") or "").startswith(exclude_producers)]
    if not rows:
        return [], None
    if rows[-1]["status"] != "success":
        return [], f"latest {rows[-1]['stage']} attestation failed"
    return [(float(r["succ_start_s"]), float(r["succ_end_s"]))
            for r in rows if r["status"] == "success"], None


def has_superseded_attestations(con: sqlite3.Connection, episode_id: str,
                                source_identity) -> bool:
    """True when proof rows bound to an older source exist and nothing is
    bound to the current source.

    Consumer-visible signal that recorded proof predates the current
    source: readers report a stale-source exclusion instead of silently
    showing unknown. Once fresh proof is recorded against the current
    source, the old rows are simply ignored.
    """
    _require_attestation_schema(con)
    row = con.execute(
        "SELECT 1 FROM coverage_attestations"
        " WHERE workspace_id='local' AND episode_id=?"
        " AND NOT (source_identity IS ?)"
        " AND source_identity IS NOT NULL LIMIT 1",
        (episode_id, source_identity)).fetchone()
    if row is None:
        return False
    current = con.execute(
        "SELECT 1 FROM coverage_attestations"
        " WHERE workspace_id='local' AND episode_id=?"
        " AND source_identity IS ? LIMIT 1",
        (episode_id, source_identity)).fetchone()
    return current is None


# DET-PLAN item 6: a transcript that ends at most this far before the
# stored duration still counts as reaching it (trailing music or silence).
FULLY_CHECKED_TOLERANCE_S = 15.0


def _screen_gate(con: sqlite3.Connection, episode_id: str, tv) -> dict:
    """Producers whose proof no longer counts for this copy: a re-screen's
    screen and publication proof while the active Jev run no longer holds
    the entry that read the copy (R4-01)."""
    from . import jev_rescreen as _rescreen
    if _rescreen.run_entry_current(con, episode_id, tv):
        return {}
    return {"screening": (_rescreen.SCREEN_PROOF_PRODUCER,),
            "observation_publication": (_rescreen.PRODUCER + ":",)}


def _has_index_version(con: sqlite3.Connection, tv) -> bool:
    try:
        return con.execute("SELECT 1 FROM transcript_index_versions WHERE version_id=?",
                           (tv,)).fetchone() is not None
    except sqlite3.OperationalError:
        return False


def episode_screen_state(con: sqlite3.Connection, episode_ids,
                         screening: dict | None = None) -> dict[str, dict]:
    """The one definition of "screened" and "fully checked" (DET-PLAN
    item 6), per episode id, read from stored facts only:

    - screened: the episode has a current transcript copy; the latest
      screen attestation on that copy succeeded and covers [0, transcript
      end]; a transcript that runs past the stored duration by no more than
      the rounding half-step (DURATION_TOLERANCE_S) needs proof only to the
      stored duration (a load never attests past the requested duration),
      and one that runs further past it needs proof to its own end; and its screening
      record (ad_reads.screening_display_many: the
      episode_screening row, else the run-entry proof) says jev_screened
      or screened_no_ads, not stopped partway. Both facts are required.
    - fully checked: screened, the transcript reaches the stored duration
      within FULLY_CHECKED_TOLERANCE_S, the copy has an index version, and
      no latest fetch or refine attestation on it failed. Missing evidence
      (no stored duration) is never fully checked.

    Each entry: state ("no_transcript" | "not_screened" | "screened" |
    "fully_checked"), screened, fully_checked, transcript_short_s (stored
    duration minus transcript end, at least 0, one decimal; None without a
    transcript or a stored duration) and not_fully_checked (reason codes:
    no_transcript, not_screened, transcript_short, no_duration,
    not_indexed, fetch_failed, refine_failed). Unknown episode ids are
    left out. screening: a screening_display_many result the caller
    already holds for these ids."""
    return _episode_screen_states(con, episode_ids, screening)[0]


_EXTENT_CACHE: dict = {}


def _copy_extent(con, tv) -> float:
    """campaigns._transcript_extent, memoized on the copy's exact payload
    bytes (the parse is most of episode_screen_state's cost)."""
    from .campaigns import _payload_extent
    row = con.execute("SELECT payload FROM source_versions WHERE id=?", (tv,)).fetchone()
    if row is None:
        return 0.0
    text = row[0]
    if not isinstance(text, (str, bytes)):
        return _payload_extent(text)
    raw = text.encode("utf-8", "surrogatepass") if isinstance(text, str) else text
    key = (len(raw), hashlib.blake2b(raw, digest_size=20).digest())
    if key not in _EXTENT_CACHE:
        if len(_EXTENT_CACHE) > 4096:
            _EXTENT_CACHE.clear()
        _EXTENT_CACHE[key] = _payload_extent(text)
    return _EXTENT_CACHE[key]


def _episode_screen_states(con: sqlite3.Connection, episode_ids,
                           screening: dict | None = None) -> tuple[dict, dict]:
    """episode_screen_state's per-episode dict, plus {episode_id: transcript
    extent in seconds} for the episodes that have a transcript."""
    from . import ad_reads as _ad
    _require_attestation_schema(con)
    ids = list(dict.fromkeys(episode_ids))
    if screening is None:
        screening = _ad.screening_display_many(con, ids)
    out: dict[str, dict] = {}
    extents: dict[str, float] = {}
    for eid in ids:
        ep = con.execute("SELECT transcript_version_id, duration_min FROM episodes WHERE id=?",
                         (eid,)).fetchone()
        if ep is None:
            continue
        tv = ep["transcript_version_id"]
        dm = ep["duration_min"]
        duration = (float(dm) * 60.0 if isinstance(dm, (int, float)) and not isinstance(dm, bool)
                    and math.isfinite(dm) and dm > 0 else None)
        if tv is None:
            out[eid] = {"state": "no_transcript", "screened": False, "fully_checked": False,
                        "transcript_short_s": None, "not_fully_checked": ["no_transcript"]}
            if attestation_union(con, eid, ("fetch",), source_identity=None)[1]:
                out[eid]["not_fully_checked"].append("fetch_failed")
            continue
        extent = _copy_extent(con, tv)
        extents[eid] = extent
        union, failure = attestation_union(
            con, eid, STAGE_GROUPS["screening"], source_identity=tv,
            exclude_producers=_screen_gate(con, eid, tv).get("screening", ()))
        rec = screening.get(eid) or {}
        screened = (extent > 0 and not failure and bool(union)
                    and covers_to_end(union, 0.0, extent if duration is None
                                      or extent - duration > DURATION_TOLERANCE_S
                                      else min(extent, duration))
                    and not rec.get("stopped_partway")
                    and bool(rec.get("jev_screened") or rec.get("screened_no_ads")))
        short = None if duration is None else round(max(0.0, duration - extent), 1)
        reasons = []
        if not screened:
            reasons.append("not_screened")
        if duration is None:
            reasons.append("no_duration")
        elif extent < duration - FULLY_CHECKED_TOLERANCE_S - EPS:
            reasons.append("transcript_short")
        if not _has_index_version(con, tv):
            reasons.append("not_indexed")
        if attestation_union(con, eid, ("fetch",), source_identity=tv)[1]:
            reasons.append("fetch_failed")
        if attestation_union(con, eid, ("refine",), source_identity=tv)[1]:
            reasons.append("refine_failed")
        out[eid] = {"state": ("not_screened" if not screened else
                              "screened" if reasons else "fully_checked"),
                    "screened": screened, "fully_checked": screened and not reasons,
                    "transcript_short_s": short, "not_fully_checked": reasons}
    return out, extents


def select_snapshots(snapshots: list[dict], multi: bool = False) -> list[dict]:
    """Latest-requested snapshot per episode by fetch-start then fetch ID.

    Success is never considered during selection. Single mode returns one
    snapshot per logical episode; multi mode returns every snapshot and the
    caller labels summed time snapshot_audio_hours.
    """
    by_ep: dict[str, list[dict]] = {}
    for snap in snapshots:
        by_ep.setdefault(snap.get("episode_id", ""), []).append(snap)
    selected = []
    for ep_id in sorted(by_ep):
        ordered = sorted(
            by_ep[ep_id],
            key=lambda s: (s.get("fetch_start", ""), str(s.get("fetch_id", ""))),
        )
        if multi:
            selected.extend(ordered)
        else:
            selected.append(ordered[-1])
    return selected


def _union_hours(intervals: list[tuple[float, float]], duration: float | None) -> float:
    if not intervals:
        return 0.0
    clipped = []
    for start, end in intervals:
        try:
            s, e = float(start), float(end)
        except (TypeError, ValueError):
            continue
        if duration is not None:
            s = max(0.0, min(s, duration))
            e = max(0.0, min(e, duration))
        if e > s:
            clipped.append((s, e))
    clipped.sort()
    merged: list[list[float]] = []
    for s, e in clipped:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return sum(e - s for s, e in merged) / 3600.0


def summarize_scope(snapshots: list[dict], reviews: list[dict] | None = None,
                    assocs: list[dict] | None = None,
                    scope: dict | None = None,
                    multi_snapshot: bool = False) -> dict:
    """Scoped coverage summary per definition v1.

    snapshots: each with episode_id, fetch_id, fetch_start, requested
      interval (req_start, req_end), successful intervals list,
      duration_s (measured media duration or None), stages dict
      {transcription/screening/observation_publication/index: bool},
      has_transcript bool, language (None when missing/unsupported),
      optional tolerance_s (default EPS) for the full-interval check.
    reviews: each with occurrence_id/episode_id, judgment, superseded bool,
      snapshot/fetch selection key (fetch_id or episode_id).
    assocs: each with occurrence_id, brand, basis, channel, status.
    """
    scope = dict(scope or {})
    selected = select_snapshots(snapshots, multi=multi_snapshot)
    selected_keys = {(s.get("episode_id"), s.get("fetch_id")) for s in selected}
    exclusions: list[str] = []
    # Searchable = published full-transcript intervals; screening failure
    # does not remove searchable text: count the requested interval when
    # a transcript exists even if screening failed.
    searchable_hours = 0.0
    observed_hours = 0.0
    complete_episodes = []
    partial_episodes = []
    for snap in selected:
        duration = snap.get("duration_s")
        req = (snap.get("req_start", 0.0), snap.get("req_end", duration or 0.0))
        success = list(snap.get("successful_intervals") or [])
        stages = snap.get("stages") or {}
        if snap.get("language") in ("unknown", "unsupported"):
            exclusions.append(
                f"{snap.get('episode_id')}: unsupported/unknown language excluded"
                " from qualified observation coverage")
        if snap.get("fetch_failed"):
            exclusions.append(f"{snap.get('episode_id')}: latest fetch failed")
        if snap.get("has_transcript"):
            searchable_hours += _union_hours([req], duration)
        else:
            exclusions.append(f"{snap.get('episode_id')}: no transcript")
        full_ok = (
            stages.get("transcription") and stages.get("screening")
            and stages.get("observation_publication") and stages.get("index")
        )
        if full_ok and success:
            observed_hours += _union_hours(success, duration)
            req_len = max(0.0, float(req[1]) - float(req[0]))
            cov_len = _union_hours(success, duration) * 3600.0
            # tolerance_s: the reader's shared duration tolerance where the
            # request ends at the rounded stored duration (forgiven at the
            # end only, never as a gap in the middle); EPS otherwise.
            tol = float(snap.get("tolerance_s", EPS))
            whole = (covers_to_end(success, float(req[0]), float(req[1]), tol)
                     if tol > EPS else cov_len >= req_len - 1e-6)
            if req_len and whole:
                complete_episodes.append(snap.get("episode_id"))
            else:
                partial_episodes.append(snap.get("episode_id"))
        else:
            if not full_ok:
                exclusions.append(
                    f"{snap.get('episode_id')}: incomplete stage"
                    f" ({','.join(k for k in REQUIRED_STAGES if not stages.get(k))})")
            partial_episodes.append(snap.get("episode_id")) if success else None
    # Deduplicate + sort episode lists.
    complete_episodes = sorted(set(complete_episodes))
    partial_episodes = sorted(
        set(partial_episodes) - set(complete_episodes))
    reviews = list(reviews or [])
    assocs = list(assocs or [])
    reviewed_occurrences: list[str] = []
    for rev in reviews:
        if rev.get("judgment") != "reviewed_ad":
            continue
        if rev.get("superseded"):
            continue
        key = (rev.get("episode_id"), rev.get("fetch_id"))
        if selected_keys and key not in selected_keys and "fetch_id" in rev:
            continue
        oid = rev.get("occurrence_id") or rev.get("id")
        if oid:
            reviewed_occurrences.append(str(oid))
    reviewed_occurrences = sorted(set(reviewed_occurrences))
    label = "snapshot_audio_hours" if multi_snapshot else "covered_audio_hours"
    return {
        "definition_revision": DEFINITION_REVISION,
        "requested_scope": scope,
        "selected_snapshots": [
            {"episode_id": s.get("episode_id"), "fetch_id": s.get("fetch_id"),
             "fetch_start": s.get("fetch_start")} for s in selected
        ],
        "required_stages": list(REQUIRED_STAGES),
        "successful_interval_union_h": round(observed_hours, 6),
        label: round(
            sum(_union_hours(list(s.get("successful_intervals") or []),
                             s.get("duration_s")) for s in selected), 6)
        if multi_snapshot else round(observed_hours, 6),
        "searchable_transcript_hours": round(searchable_hours, 6),
        "automated_observation_hours": round(observed_hours, 6),
        "covered_audio_hours": round(observed_hours, 6),
        "distinct_covered_episodes": list(complete_episodes),
        "partial_episodes": list(partial_episodes),
        "reviewed_occurrences": reviewed_occurrences,
        "exclusions": sorted(set(exclusions)),
        "review_predicate": "judgment=reviewed_ad AND superseded=false"
                            " AND snapshot in selected",
        "incomplete": not complete_episodes,
    }


def compare_groups(observations: list[dict], group_by: str,
                   unit: str = "reviewed_occurrences",
                   include_proposals: bool = False) -> dict:
    """Brand/category/show comparison with explicit count unit.

    Default comparative unit is reviewed occurrences; proposals are visibly
    separated when included. Multi-brand records disclose non-additivity.
    Unknown dates/brand/category are separate buckets.
    """
    if unit not in ("reviewed_occurrences", "proposals", "candidates",
                    "creatives", "episodes", "audio_hours"):
        raise ValueError(f"unknown count unit: {unit}")
    groups: dict[str, list[dict]] = {}
    multi_brand = False
    for obs in observations:
        key = obs.get(group_by) or f"unknown_{group_by}"
        if not obs.get(group_by):
            key = f"unknown_{group_by}"
        groups.setdefault(str(key), []).append(obs)
        brands = obs.get("brands") or ([obs.get("brand")] if obs.get("brand") else [])
        if len([b for b in brands if b]) > 1:
            multi_brand = True
    reviewed = [o for o in observations if o.get("judgment") == "reviewed_ad"
                and not o.get("superseded")]
    proposals = [o for o in observations if o.get("status") == "proposed"
                 or o.get("judgment") in (None, "proposed")]
    table = []
    for name in sorted(groups):
        rows = groups[name]
        r = [o for o in rows if o.get("judgment") == "reviewed_ad"
             and not o.get("superseded")]
        first = min((o.get("observed_at", "") or "" for o in rows if o.get("observed_at")), default="")
        last = max((o.get("observed_at", "") or "" for o in rows if o.get("observed_at")), default="")
        entry: dict = {
            "group": name, "reviewed_occurrences": len(r),
            "first_observed_workspace": first or "unknown",
            "last_observed_workspace": last or "unknown",
            "status": "complete" if r else "incomplete",
        }
        if include_proposals:
            entry["proposals_separate"] = len(
                [o for o in rows if o.get("status") == "proposed"])
        table.append(entry)
    return {
        "definition_revision": DEFINITION_REVISION,
        "group_by": group_by,
        "unit": unit,
        "groups": table,
        "totals": {unit: len(reviewed) if unit == "reviewed_occurrences" else len(observations),
                   "proposals_separate": len(proposals) if include_proposals else 0},
        "non_additive": multi_brand,
        "caveats": (["multi-brand records appear in multiple groups; summed counts"
                     " are not unique-ad totals"] if multi_brand else [])
        + ["first/last observed are workspace-first, never first-aired",
           "unknown dates/brand/category are separate buckets",
           "fetch-history is a separately labeled view",
           "incomplete periods stay incomplete"],
    }


def creative_history(memberships: list[dict]) -> dict:
    """Confirmed creative histories with first/last observed + drill-down."""
    creatives: dict[str, list[dict]] = {}
    for mem in memberships:
        creatives.setdefault(str(mem.get("creative_id", "unknown")), []).append(mem)
    out = []
    for cid in sorted(creatives):
        rows = creatives[cid]
        confirmed = [r for r in rows if r.get("confirmed")]
        times = sorted(r.get("observed_at", "") or "" for r in rows if r.get("observed_at"))
        out.append({
            "creative_id": cid,
            "confirmed_occurrences": len(confirmed),
            "suggested_only": len(confirmed) == 0,
            "first_observed_workspace": times[0] if times else "unknown",
            "last_observed_workspace": times[-1] if times else "unknown",
            "observations": [r.get("observation_id") for r in rows],
        })
    return {"definition_revision": DEFINITION_REVISION, "creatives": out}


def changed_fields(observations: list[dict], field: str) -> dict:
    """Changed offers/codes view: distinct values per occurrence over time."""
    by_occ: dict[str, list[dict]] = {}
    for obs in observations:
        by_occ.setdefault(str(obs.get("occurrence_id", "unknown")), []).append(obs)
    changed = []
    for occ, rows in sorted(by_occ.items()):
        values = []
        for row in sorted(rows, key=lambda r: str(r.get("observed_at", ""))):
            val = row.get(field)
            if val and (not values or values[-1] != val):
                values.append(val)
        if len(values) > 1:
            changed.append({"occurrence_id": occ, "field": field,
                            "values": values,
                            "observations": [r.get("id") for r in rows]})
    new_relationships = sorted({
        str(o.get("brand")) for o in observations
        if o.get("is_new_sponsor") and o.get("brand")
    })
    return {"definition_revision": DEFINITION_REVISION, "field": field,
            "changed": changed, "new_sponsor_relationships": new_relationships}


def save_comparison(con: sqlite3.Connection, name: str, definition: dict,
                    data_revision: int, source_revision: str | None = None) -> dict:
    con.execute(
        "CREATE TABLE IF NOT EXISTS saved_comparisons ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " name TEXT NOT NULL, definition TEXT NOT NULL,"
        " data_revision INTEGER NOT NULL, source_revision TEXT NOT NULL DEFAULT '',"
        " caveats TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL)"
    )
    import datetime
    caveats = ["coverage definition v1; incomplete periods stay incomplete",
               "no spend/reach/market-share inferred"]
    with _db.transaction(con, write=True):
        cur = con.execute(
            "INSERT INTO saved_comparisons"
            " (name, definition, data_revision, source_revision, caveats, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (name, __import__("json").dumps(definition), int(data_revision),
             str(source_revision or ""),
             __import__("json").dumps(caveats),
             datetime.datetime.now(datetime.timezone.utc).isoformat()),
        )
        return {"id": cur.lastrowid, "name": name,
                "data_revision": int(data_revision),
                "source_revision": str(source_revision or ""),
                "caveats": caveats}
