"""One-off legacy coverage attestation import (decision D-2).

Legacy workspaces predate the stage transactions that record coverage
attestations, so their placements stay "Coverage incomplete" and nothing
backfills them by design. This operator-run import looks only at episodes
whose stored current transcript reaches the stored duration within the
tolerance (TOLERANCE_NOTE: 3 s for 0.1-min grid durations, otherwise
exact; coverage.duration_tolerance), and attests each stage only
from that stage's own stored evidence on the current transcript copy:

- transcribe: the transcript's last segment end;
- screen: the screening run entry that read this same transcript copy
  (same transcript_version_id, segment count and segments digest, as a Jev
  re-screen records them: jev_rescreen), with the episode's screening
  record (full tier, screened, not stopped partway); an episode with no
  screening record (benchmark, upload) only when the entry's windows are
  contiguous over the whole copy with the pinned model (RUN_ENTRY_BASIS);
- index: the search-index version of the current transcript, when it holds
  every segment;
- observation_publication: the published observations on the current copy,
  each at its own bounds (as the observation producer records them). An
  imported Jev re-screen (`import-jev`) already attested its publication
  over the screened windows; that row is left alone like any other proof.

A stage without its own evidence is not attested. Nothing calls this
automatically: the only entry point is `app_workspace.py import-coverage`
(dry run read-only; a real run with the server stopped, under the workspace
lock, after a recovery backup).

Auditable as its own producer: every row carries
`legacy-coverage-import:<run_id>`, producer_rev PRODUCER_REV, the operator
actor and the transcript source pin; one audit row per run carries each
episode's basis (stored duration, transcript end, shortfall, tolerance,
per-stage evidence) and the attestation ids; a run that records nothing
writes nothing, not even an audit row (the record-alias no-op pattern).
Existing proof is never
overridden: a stage that already has a row on the current source is left
alone, and an episode whose latest current-source row for any stage failed
is skipped.
"""
from __future__ import annotations

import datetime
import json

from . import coverage as _coverage
from . import db as _db

PRODUCER = "legacy-coverage-import"
PRODUCER_REV = 5
# Stored durations are rounded to 0.1 min (6 s): the shared tolerance.
DURATION_STEP_S = _coverage.DURATION_STEP_S
TOLERANCE_S = _coverage.DURATION_TOLERANCE_S
TOLERANCE_NOTE = "3 s for 0.1-min grid durations, otherwise exact"
BASIS = ("legacy import: the stored current transcript reaches the stored duration"
         f" (tolerance {TOLERANCE_NOTE}: half the 0.1-min rounding step of a rounded"
         " duration; a measured duration has none); each stage is attested only from"
         " its own stored evidence on that transcript copy")
STAGES = tuple(s for group in _coverage.STAGE_GROUPS.values() for s in group)


def _payload(con, version_id):
    row = con.execute("SELECT payload FROM source_versions WHERE id=?",
                      (version_id,)).fetchone() if version_id is not None else None
    try:
        return json.loads(row["payload"]) if row else None
    except (ValueError, TypeError):
        return None


def _merge(intervals):
    merged = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [tuple(iv) for iv in merged]


def _jev_run(con) -> dict:
    row = con.execute("SELECT payload FROM source_versions WHERE source_key='jev-run'"
                      " AND is_active=1 ORDER BY id DESC LIMIT 1").fetchone()
    try:
        payload = json.loads(row["payload"]) if row else {}
    except (ValueError, TypeError):
        payload = {}
    episodes = payload.get("episodes") if isinstance(payload, dict) else None
    return {str(e.get("episode_id")): e for e in episodes or [] if isinstance(e, dict)}


def _contiguous(intervals, extent: float):
    """None when the intervals cover [0, extent] without a gap, else why not."""
    reach = 0.0
    for s, e in sorted(intervals):
        if s > reach + _coverage.EPS:
            return f"screened windows leave a gap at {reach:.1f}s"
        reach = max(reach, e)
    if reach < extent - _coverage.EPS:
        return f"screened windows reach {reach:.1f}s of the copy's {extent:.1f}s"
    return None


RUN_ENTRY_BASIS = ("run entry (no episode_screening row): the screening run entry read the"
                   " current transcript copy (same transcript_version_id, segment count and"
                   " segments digest), model {model}, its windows contiguous over"
                   " [0, last segment end] (full tier, not stopped partway)")


def _screen_evidence(con, eid, tv, segments, jev):
    """(intervals, reason, basis) of the screen stage from its own evidence."""
    from . import ad_reads as _ad
    from earwitness.pipeline import jev_screen as _jev
    rec = _ad.screening(con, eid)
    if rec is not None:
        if (rec.get("coverage_tier") or "full") != "full":
            return [], f"screening record tier is {rec.get('coverage_tier')}", None
        if rec.get("stopped_partway"):
            return [], "screening stopped partway", None
        # Tier 'full' is only the column default: the record must say it screened.
        if not (rec.get("jev_screened") or rec.get("screened_no_ads")):
            return [], "screening record says not screened", None
    run = jev.get(eid)
    if run is None:
        return [], "no screening record" if rec is None else \
            "no screening run entry for this episode", None
    # The run entry must name the copy it read: same transcript version,
    # segment count and segments digest. A matching segment count alone
    # can match by chance and is never enough (R1-18).
    run_tv = run.get("transcript_version_id")
    if run_tv is None:
        return [], ("screening run entry does not record which transcript copy it read"
                    f" ({run.get('segment_count')} segments; current copy is version {tv})"), None
    if run_tv != tv:
        return [], (f"screening read another transcript copy (version {run_tv};"
                    f" current copy is {tv})"), None
    if run.get("segment_count") != len(segments):
        return [], (f"screening read another transcript copy ({run.get('segment_count')}"
                    f" segments; current copy has {len(segments)})"), None
    if run.get("segments_sha256") != _jev.segments_digest(segments):
        return [], ("screening read another transcript copy (segments differ; a matching"
                    " segment count alone is never enough)"), None
    windows = []
    for answer in (run.get("answers") or {}).values():
        if isinstance(answer, dict) and isinstance(answer.get("start_s"), (int, float)) \
                and isinstance(answer.get("end_s"), (int, float)) \
                and answer["end_s"] > answer["start_s"] >= 0:
            windows.append((float(answer["start_s"]), float(answer["end_s"])))
    if not windows:
        return [], "screening run has no screened windows", None
    if rec is not None:
        flags = {k: bool(rec.get(k)) for k in ("jev_screened", "screened_no_ads")}
        return _merge(windows), None, f"{SCREEN_BASIS}; record: " + json.dumps(flags)
    # No screening row (an episode outside the corpus export: benchmark,
    # upload): the run entry is the stage's own evidence only when it says
    # it screened the whole copy with the pinned model.
    if run.get("model") != _jev.DEFAULT_MODEL:
        return [], (f"no screening record, and the run entry's model {run.get('model')!r}"
                    f" is not {_jev.DEFAULT_MODEL}"), None
    extent = max((float(s["end"]) for s in segments if isinstance(s, dict)
                  and isinstance(s.get("end"), (int, float))), default=0.0)
    gap = _contiguous(windows, extent)
    if gap:
        return [], f"no screening record, and {gap}", None
    return _merge(windows), None, RUN_ENTRY_BASIS.format(model=run.get("model"))


def run_entry_screening(con, episode_id: str, jev: dict | None = None) -> dict | None:
    """The screening status of an episode with no episode_screening row (a
    benchmark or upload episode), from the evidence that attests its screen
    stage: _transcript_gate's checks, no failed latest screening-stage
    attestation, _screen_evidence's run-entry branch (the entry read the current copy, the pinned model, windows contiguous over
    the whole copy) and run_entry_current (its flagged rows are visible). A row-shaped dict for
    ad_reads._screening_state, or None when the entry does not prove it.
    Display only: nothing is written."""
    from . import jev_rescreen as _rescreen
    jev = _jev_run(con) if jev is None else jev
    entry = jev.get(episode_id)
    ep = con.execute("SELECT id, duration_min, transcript_version_id FROM episodes WHERE id=?",
                     (episode_id,)).fetchone()
    if entry is None or ep is None or ep["transcript_version_id"] is None:
        return None
    # The import's own gate first: a short transcript blocks the screen
    # proof, so it blocks the page claim; and so does a failed latest
    # attestation of the screening stage group, per stage as the coverage
    # proof (a failed latest attestation of another stage does not).
    tv = ep["transcript_version_id"]
    if _transcript_gate(con, ep)["reason"] or _coverage.attestation_union(
            con, episode_id, _coverage.STAGE_GROUPS["screening"], source_identity=tv)[1]:
        return None
    segments = (_payload(con, tv) or {}).get("segments") or []
    if _screen_evidence(con, episode_id, tv, segments, jev)[1] is not None \
            or not _rescreen.run_entry_current(con, episode_id, tv):
        return None
    return {"episode_id": episode_id, "jev_screened": 1, "stopped_partway": 0,
            "screened_no_ads": int(not _rescreen.flagged_record_keys(entry)),
            "coverage_tier": "full", "pipeline_status": None}


SCREEN_BASIS = ("episode_screening coverage_tier='full' AND (jev_screened OR screened_no_ads)"
                " AND NOT stopped_partway, and the screening run entry read the current transcript"
                " copy (same transcript_version_id, segment count and segments digest);"
                " intervals = its screened windows")


def _index_evidence(con, tv, segment_count):
    try:
        row = con.execute("SELECT segments FROM transcript_index_versions WHERE version_id=?",
                          (tv,)).fetchone()
    except Exception:
        row = None
    if row is None:
        return [], "current transcript copy is not indexed"
    span = con.execute("SELECT count(*) n, max(end_s) e FROM transcript_segments"
                       " WHERE version_id=?", (tv,)).fetchone()
    if row["segments"] != segment_count or span["n"] != segment_count or not span["e"]:
        return [], f"index holds {span['n']} of {segment_count} segments"
    return [(0.0, float(span["e"]))], None


def _publication_evidence(con, eid, tv):
    rows = con.execute("SELECT id, start_s, end_s FROM ad_observations WHERE episode_id=?"
                       " AND source_version_id=? ORDER BY start_s, id", (eid, tv)).fetchall()
    intervals = [(float(r["start_s"]), float(r["end_s"])) for r in rows
                 if isinstance(r["start_s"], (int, float)) and isinstance(r["end_s"], (int, float))
                 and r["end_s"] > r["start_s"] >= 0]
    if not intervals:
        return [], "no published observations on the current copy"
    return intervals, None


def _transcript_gate(con, ep) -> dict:
    """The transcript checks that come before any stage's own evidence: a
    transcript with segments that reaches the stored duration (within the
    tolerance). The plan row; its reason is set when the episode is blocked."""
    from .campaigns import _transcript_extent
    tv = ep["transcript_version_id"]
    dm = ep["duration_min"]
    duration = (float(dm) * 60.0 if isinstance(dm, (int, float))
                and not isinstance(dm, bool) and dm > 0 else None)
    extent = _transcript_extent(con, tv)
    row = {"episode_id": ep["id"], "transcript_version_id": tv,
           "stored_duration_min": dm if duration is not None else None,
           "last_segment_end_s": extent if tv is not None else None,
           "known_duration_s": duration,
           "gap_s": (duration - extent) if duration is not None and tv is not None else None,
           "tolerance_s": _coverage.duration_tolerance(dm),
           "verdict": "skip", "reason": "", "stages": {}}
    if tv is None:
        row["reason"] = "no transcript"
        return row
    if extent <= 0:
        row["reason"] = "no transcript segments with an end time"
        return row
    if duration is None:
        row["reason"] = "no known duration"
        return row
    # The rounded-duration tolerance only for a duration on the 0.1-min
    # grid; a measured duration (an upload's) keeps EPS (R4-03).
    tol = row["tolerance_s"]
    if extent < duration - tol:
        row["reason"] = (f"transcript stops short: ends {extent:.2f}s of"
                         f" {duration:.2f}s stored duration (tolerance {tol:g}s)")
    return row


def _plan_gate(con, ep) -> tuple[dict, dict]:
    """The transcript checks, then no failed latest attestation of any stage
    (the import plans every stage). (plan row, latest attestation per stage);
    the row's reason is set when the episode is blocked."""
    row = _transcript_gate(con, ep)
    if row["reason"]:
        return row, {}
    tv = ep["transcript_version_id"]
    existing = con.execute(
        "SELECT stage, status, producer FROM coverage_attestations"
        " WHERE workspace_id='local' AND episode_id=? AND source_identity IS ?"
        " ORDER BY id", (ep["id"], tv)).fetchall()
    latest = {r["stage"]: r for r in existing}
    failed = sorted(s for s, r in latest.items() if r["status"] != "success")
    if failed:
        row["reason"] = ("latest " + ", ".join(failed) + " attestation failed;"
                         " a recorded failure is never overridden")
    return row, latest


def _plan_episode(con, ep, jev) -> dict:
    row, latest = _plan_gate(con, ep)
    if row["reason"]:
        return row
    tv = row["transcript_version_id"]
    extent = row["last_segment_end_s"]
    duration = row["known_duration_s"]
    segments = (_payload(con, tv) or {}).get("segments") or []
    screen = _screen_evidence(con, ep["id"], tv, segments, jev)
    evidence = {
        "transcribe": ([(0.0, extent)], None),
        "screen": screen[:2],
        "index": _index_evidence(con, tv, len(segments)),
        "observation_publication": _publication_evidence(con, ep["id"], tv),
    }
    req_end = max(extent, duration)
    for stage in STAGES:
        if stage in latest:
            row["stages"][stage] = {"verdict": "skip",
                                    "reason": f"already attested ({latest[stage]['producer']})"}
            continue
        intervals, reason = evidence[stage]
        intervals = [(max(0.0, s), min(req_end, e)) for s, e in intervals if min(req_end, e) > max(0.0, s)]
        if reason or not intervals:
            row["stages"][stage] = {"verdict": "skip", "reason": reason or "no interval inside the episode"}
        else:
            row["stages"][stage] = {"verdict": "record", "intervals": intervals}
            if stage == "screen":
                row["stages"][stage]["basis"] = screen[2]
    if any(s["verdict"] == "record" for s in row["stages"].values()):
        row["verdict"] = "record"
    else:
        row["reason"] = "no stage has its own unrecorded evidence"
    return row


def stage_counts(episodes: list) -> dict:
    """Per stage: qualifying episodes, episodes/rows to record, skip reasons."""
    out = {}
    qualifying = [r for r in episodes if r["stages"]]
    for stage in STAGES:
        reasons: dict = {}
        rec_eps = rec_rows = 0
        for r in qualifying:
            s = r["stages"].get(stage) or {}
            if s.get("verdict") == "record":
                rec_eps += 1
                rec_rows += len(s["intervals"])
            else:
                key = s.get("reason", "")
                key = key.split(" (")[0] if key.startswith(("screening read another", "already attested")) else key
                reasons[key] = reasons.get(key, 0) + 1
        out[stage] = {"qualifying": len(qualifying), "record_episodes": rec_eps,
                      "record_rows": rec_rows, "skipped": reasons}
    return out


def run(con, *, dry_run: bool, run_id: str | None = None,
        actor: str = "operator") -> dict:
    """Plan (and unless dry_run, record) the import in one transaction."""
    run_id = run_id or "cov-" + datetime.datetime.now(
        datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    producer = f"{PRODUCER}:{run_id}"
    recorded = []
    with _db.transaction(con, write=not dry_run):
        _coverage._require_attestation_schema(con)
        jev = _jev_run(con)
        episodes = [_plan_episode(con, ep, jev) for ep in con.execute(
            "SELECT id, duration_min, transcript_version_id FROM episodes ORDER BY id")]
        if not dry_run:
            for row in episodes:
                if row["verdict"] != "record":
                    continue
                req = (0.0, max(row["last_segment_end_s"], row["known_duration_s"]))
                ids = {}
                for stage, s in row["stages"].items():
                    if s["verdict"] != "record":
                        continue
                    ids[stage] = [_coverage.record_attestation(
                        con, row["episode_id"], stage, req, iv,
                        source_pin=f"source-version:{row['transcript_version_id']}",
                        producer=producer, producer_rev=PRODUCER_REV, actor=actor)
                        for iv in s["intervals"]]
                recorded.append({k: row[k] for k in (
                    "episode_id", "transcript_version_id", "stored_duration_min",
                    "known_duration_s", "last_segment_end_s", "gap_s", "tolerance_s",
                    "stages")} | {"attestation_ids": ids})
            if recorded:
                rev = _db.bump_revision(con)
                _db.audit(con, actor, "coverage_import", "workspace", run_id,
                          {"producer": producer, "producer_rev": PRODUCER_REV,
                           "basis": BASIS, "tolerance_s": TOLERANCE_NOTE,
                           "episodes": recorded}, rev)
    return {"run_id": run_id, "producer": producer, "dry_run": dry_run,
            "basis": BASIS, "tolerance_s": TOLERANCE_NOTE,
            "would_record_episodes": sum(r["verdict"] == "record" for r in episodes),
            "recorded_episodes": len(recorded),
            "recorded_attestations": sum(len(v) for r in recorded
                                         for v in r["attestation_ids"].values()),
            "stage_counts": stage_counts(episodes),
            "episodes": episodes}


def format_report(result: dict) -> str:
    def num(v):
        return "-" if v is None else f"{v:.2f}"
    lines = [("DRY RUN (writes nothing): " if result["dry_run"] else "")
             + f"producer {result['producer']}; basis: {result['basis']}",
             "episode\ttranscript_version\tlast_segment_end_s\tknown_duration_s\tgap_s\tverdict\t"
             + "\t".join(STAGES)]
    for r in result["episodes"]:
        cells = []
        for stage in STAGES:
            s = r["stages"].get(stage)
            cells.append("-" if s is None else
                         f"record x{len(s['intervals'])}" if s["verdict"] == "record"
                         else "skip: " + s["reason"])
        lines.append("\t".join([r["episode_id"], str(r["transcript_version_id"]),
                                num(r["last_segment_end_s"]), num(r["known_duration_s"]),
                                num(r["gap_s"]), r["verdict"] + (": " + r["reason"] if r["reason"] else "")]
                               + cells))
    lines.append("per stage (episodes passing the transcript gate):")
    for stage, c in result["stage_counts"].items():
        lines.append(f"  {stage}: qualifying {c['qualifying']}; record {c['record_episodes']}"
                     f" episodes / {c['record_rows']} rows; skipped {json.dumps(c['skipped'])}")
    lines.append(f"episodes {len(result['episodes'])}; with something to record"
                 f" {result['would_record_episodes']}; recorded"
                 f" {result['recorded_attestations']} attestations for"
                 f" {result['recorded_episodes']} episodes")
    return "\n".join(lines)
