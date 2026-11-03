"""R1-06 option A: re-screen each episode's current transcript copy with Jev.

Legacy screening read older transcript copies, so no screening result is
tied to the copy a placement reads today. This module exports the current
copies so the corpus runner can screen exactly them:

    export_current_copies(con, out)   read-only on the workspace; writes
        <out>/transcripts/<ep>.json   the copy's segments verbatim plus
                                      `workspace_copy` (which copy it is)
        <out>/queue.sqlite3           one `done` corpus row per exported copy
        <out>/manifest.json           per episode: id, transcript_version_id,
                                      segment count, digest, fetch_date and
                                      its provenance

then `corpus_run.py jev-backfill --corpus <out>` (dry run first) screens
them; each result records the copy it read (corpus._check_workspace_copy).
The app never calls TypeSafe: screening is an operator-run corpus step.

    import_results(con, corpus)       `app_workspace.py import-jev`: brings
        the results into the workspace without a reseed, one transaction.

A result is imported only when it read the episode's current copy: same
transcript_version_id, segment count AND segments digest (a matching
segment count alone is never enough). It becomes part of a new `jev-run`
source version (the old run's entries for episodes without a matching
result stay verbatim; every row active in the old version stays active,
same id and copy: a re-screen never hides a read); each window at the
seed threshold becomes a `jev`
evidence row on the current copy, unnamed and unreviewed (an unchanged
window on an unchanged copy keeps its row and its review, as a seed
reload does). The import is the publication of every window's result for
that copy, so in the same transaction it attests observation_publication
over the screened windows (producer jev-rescreen:<run_id>). Screening
itself is attested by `import-coverage` from the run entry. A rerun with
nothing new writes nothing, not even an audit row.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import math
from pathlib import Path

from earwitness.pipeline import corpus as _corpus
from earwitness.pipeline import jev_screen as _jev
from earwitness.pipeline.chunked_stt import atomic_write_json

from . import db as _db

EXPORT_KIND = "earwitness-workspace-transcript-copies"
FEED = "workspace-export"
PRODUCER = "jev-rescreen"
PRODUCER_REV = 1
# The coverage import's screen proof comes from the active run's entry.
SCREEN_PROOF_PRODUCER = "legacy-coverage-import:"
RESCREEN_STATUS = "workspace_rescreen"


class ExportError(RuntimeError):
    pass


def _workspace_path(con) -> str | None:
    row = con.execute("PRAGMA database_list").fetchone()
    return row[2] if row and row[2] else None


def _plan(con) -> tuple[list, list]:
    """(exportable copies, skipped {episode_id, reason}) from the stored
    current transcript copies."""
    rows, skipped = [], []
    for ep in con.execute(
            "SELECT e.id, e.kind, e.alias_of, e.show_title, e.ep_title, e.duration_min,"
            " e.fetch_date, e.transcript_version_id, sv.source_key, sv.version_no,"
            " sv.sha256, sv.payload FROM episodes e LEFT JOIN source_versions sv"
            " ON sv.id=e.transcript_version_id ORDER BY e.id"):
        if ep["transcript_version_id"] is None or ep["payload"] is None:
            skipped.append({"episode_id": ep["id"], "reason": "no transcript copy"})
            continue
        try:
            payload = json.loads(ep["payload"])
        except (TypeError, ValueError):
            skipped.append({"episode_id": ep["id"], "reason": "transcript copy is not JSON"})
            continue
        segments = payload.get("segments") if isinstance(payload, dict) else None
        if not isinstance(segments, list) or not segments:
            skipped.append({"episode_id": ep["id"], "reason": "transcript copy has no segments"})
            continue
        rows.append((ep, payload, segments))
    return rows, skipped


def export_current_copies(con, out) -> dict:
    """Export every episode's current transcript copy for Jev screening.
    Reads the workspace only. Idempotent: an unchanged copy is rewritten
    byte-identical and keeps its corpus row and any Jev result; a changed
    copy whose old export already has a Jev result is refused (ExportError)
    instead of silently pairing a result with a copy it never read."""
    out = Path(out)
    dirs = _corpus.corpus_dirs(out)
    with _db.transaction(con, write=False):
        rows, skipped = _plan(con)
    docs, entries = {}, []
    for ep, payload, segments in rows:
        copy = {"episode_id": ep["id"], "transcript_version_id": ep["transcript_version_id"],
                "source_key": ep["source_key"], "version_no": ep["version_no"],
                "source_sha256": ep["sha256"], "segment_count": len(segments),
                "segments_sha256": _jev.segments_digest(segments)}
        docs[ep["id"]] = {"ep_id": ep["id"], "duration_min": ep["duration_min"],
                          "workspace_copy": copy, "segments": segments}
        entries.append({**copy, "kind": ep["kind"], "alias_of": ep["alias_of"],
                        "duration_min": ep["duration_min"],
                        "last_segment_end_s": max(float(s.get("end") or 0) for s in segments
                                                  if isinstance(s, dict)),
                        "episode_fetch_date": ep["fetch_date"],
                        "episode_fetch_date_source": ("episodes.fetch_date" if ep["fetch_date"]
                                                      else "unrecorded"),
                        "transcript_fetch_date": payload.get("fetch_date"),
                        "transcript_fetch_date_source": (payload.get("fetch_date_source")
                                                         or ("transcript payload fetch_date"
                                                             if payload.get("fetch_date")
                                                             else "unrecorded"))})
    for ep_id, doc in docs.items():
        path = dirs["transcripts"] / f"{ep_id}.json"
        if path.exists() and (dirs["jev"] / f"{ep_id}.json").exists():
            try:
                same = json.loads(path.read_text("utf-8")) == doc
            except ValueError:
                same = False
            if not same:
                raise ExportError(f"{ep_id}: current copy differs from the exported copy"
                                  " that already has a Jev result; export into a new folder")
    dirs["transcripts"].mkdir(parents=True, exist_ok=True)
    for ep_id, doc in docs.items():
        path = dirs["transcripts"] / f"{ep_id}.json"
        if not (path.exists() and path.read_bytes() == json.dumps(doc).encode("utf-8")):
            atomic_write_json(path, doc)
    qcon = _corpus.connect(out)
    try:
        _corpus.ensure_schema(qcon)
        now = _corpus.utcnow()
        with _corpus._Tx(qcon):
            for ep, _payload, _segments in rows:
                dm = ep["duration_min"]
                qcon.execute(
                    "INSERT OR IGNORE INTO episodes (episode_id, feed_url, show_title, ep_title,"
                    " stage, duration_s, tier, discovered_at, updated_at)"
                    " VALUES (?, ?, ?, ?, 'done', ?, 'full', ?, ?)",
                    (ep["id"], FEED, ep["show_title"], ep["ep_title"],
                     float(dm) * 60.0 if isinstance(dm, (int, float)) and dm > 0 else None,
                     now, now))
    finally:
        qcon.close()
    exported_at = _db.utcnow()
    manifest = {"kind": EXPORT_KIND, "workspace": _workspace_path(con),
                "exported_at": exported_at, "fetch_date": exported_at[:10],
                "fetch_date_source": "export: read from the workspace's stored current"
                                     " transcript copies at exported_at",
                "screen_with": "scripts/corpus_run.py jev-backfill --corpus <this folder>"
                               " (--dry-run first)",
                "model": _jev.DEFAULT_MODEL, "episodes": entries, "skipped": skipped}
    atomic_write_json(out / "manifest.json", manifest)
    return {"exported": len(entries), "segments": sum(e["segment_count"] for e in entries),
            "skipped": skipped, "out": str(out)}


# ---------------------------------------------------------------- import

def _current_copies(con) -> dict:
    out = {}
    for row in con.execute(
            "SELECT e.id, e.duration_min, e.transcript_version_id, sv.sha256, sv.payload"
            " FROM episodes e JOIN source_versions sv ON sv.id=e.transcript_version_id"):
        try:
            segments = json.loads(row["payload"]).get("segments") or []
        except (TypeError, ValueError, AttributeError):
            segments = []
        out[row["id"]] = {"tv": row["transcript_version_id"], "sha256": row["sha256"],
                          "segments": segments, "duration_min": row["duration_min"]}
    return out


def _active_run(con):
    row = con.execute("SELECT id, payload FROM source_versions WHERE source_key='jev-run'"
                      " AND is_active=1 ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None, {}
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        payload = {}
    return row["id"], payload if isinstance(payload, dict) else {}


def _windows_problem(doc, extent: float):
    ws = doc.get("windows")
    if not isinstance(ws, list) or not ws:
        return "result has no windows"
    reach = 0.0
    for w in sorted((w for w in ws if isinstance(w, dict)), key=lambda w: w.get("start_s", -1)):
        vals = [w.get(k) for k in ("start_s", "end_s", "noul")]
        if not isinstance(w.get("window_id"), str) or not all(
                isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                for v in vals) or not (0.0 <= w["noul"] <= 1.0) or w["end_s"] <= w["start_s"]:
            return "result has a malformed window"
        if w["start_s"] > reach + 1e-6:
            return f"windows leave a gap at {reach:.1f}s"
        reach = max(reach, float(w["end_s"]))
    if len(ws) != sum(isinstance(w, dict) for w in ws) or reach < extent - 1e-6:
        return f"windows reach {reach:.1f}s of the copy's {extent:.1f}s"
    return None


def _plan_import(con, corpus: Path, threshold: float) -> dict:
    current = _current_copies(con)
    old_vid, old_run = _active_run(con)
    old_entries = {str(e.get("episode_id") or e.get("ep_id")): e
                   for e in old_run.get("episodes") or [] if isinstance(e, dict)}
    rows = []
    for path in sorted((corpus / "jev").glob("*.json")):
        try:
            doc = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            rows.append({"episode_id": path.stem, "verdict": "skip",
                         "reason": "result is not readable JSON"})
            continue
        ep = str(doc.get("episode_id") or path.stem)
        row = {"episode_id": ep, "verdict": "skip", "reason": "",
               "transcript_version_id": doc.get("transcript_version_id"),
               "segment_count": doc.get("segment_count"),
               "segments_sha256": doc.get("segments_sha256"),
               "screened_at": doc.get("screened_at"), "model": doc.get("model")}
        rows.append(row)
        cur = current.get(ep)
        if cur is None:
            row["reason"] = "no such episode with a current transcript copy"
            continue
        row["current_transcript_version_id"] = cur["tv"]
        tv = doc.get("transcript_version_id")
        if isinstance(tv, bool) or not isinstance(tv, int):
            row["reason"] = "result does not record which transcript copy it read"
            continue
        if tv != cur["tv"]:
            row["reason"] = (f"result read another transcript copy (version {tv};"
                             f" current copy is {cur['tv']})")
            continue
        if doc.get("segment_count") != len(cur["segments"]):
            row["reason"] = (f"result read another transcript copy ({doc.get('segment_count')}"
                             f" segments; current copy has {len(cur['segments'])})")
            continue
        if doc.get("segments_sha256") != _jev.segments_digest(cur["segments"]):
            row["reason"] = ("segments differ from the current copy (a matching segment"
                             " count alone is never enough)")
            continue
        if doc.get("model") != _jev.DEFAULT_MODEL:
            row["reason"] = f"result model {doc.get('model')!r} is not {_jev.DEFAULT_MODEL}"
            continue
        extent = max((float(s["end"]) for s in cur["segments"]
                      if isinstance(s, dict) and isinstance(s.get("end"), (int, float))),
                     default=0.0)
        problem = _windows_problem(doc, extent)
        if problem:
            row["reason"] = problem
            continue
        old = old_entries.get(ep) or {}
        if (old.get("transcript_version_id"), old.get("segments_sha256"),
                old.get("screened_at")) == (tv, doc["segments_sha256"], doc.get("screened_at")):
            row["reason"] = f"already imported (run {old.get('run_id')})"
            continue
        row.update(verdict="import", extent_s=extent, windows=len(doc["windows"]),
                   flagged_windows=sum(1 for w in doc["windows"] if w["noul"] >= threshold),
                   _doc=doc, _copy=cur)
    imported = {r["episode_id"] for r in rows if r["verdict"] == "import"}
    already = sorted(r["episode_id"] for r in rows
                     if r["verdict"] == "skip" and r["reason"].startswith("already imported"))
    return {"episodes": rows, "old_version_id": old_vid, "old_run": old_run,
            "already": already,
            "kept": sorted(ep for ep in old_entries if ep not in imported and ep not in already)}


def _src_visible(con, evidence_id: str, version_id) -> bool:
    """Whether an evidence row is active in a source version (the seed's
    visibility rule: own pin or membership)."""
    from . import import_sources as _src
    row = con.execute("SELECT id, source_version_id FROM evidence WHERE id=?",
                      (evidence_id,)).fetchone()
    return row is not None and _src._is_visible(con, row, version_id)


def _spans(doc, extent: float) -> list:
    """The screened windows clipped to [0, extent], merged."""
    spans = []
    for w in sorted(doc["windows"], key=lambda w: w["start_s"]):
        s, e = float(w["start_s"]), min(float(w["end_s"]), extent)
        if e <= s:
            continue
        if spans and s <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], e)
        else:
            spans.append([s, e])
    return spans


def _visible_in(con, row, version_id) -> bool:
    from . import import_sources as _src
    if not _db.has_evidence_membership(con):
        return row["source_version_id"] == version_id
    return _src._is_visible(con, row, version_id)


def _excerpt(segments, start, end) -> str:
    return " ".join(str(s.get("text", "")) for s in segments
                    if isinstance(s, dict) and s.get("start", float("inf")) < end
                    and s.get("end", float("-inf")) > start).strip()[:600]


def import_results(con, corpus, *, dry_run: bool, run_id: str | None = None,
                   actor: str = "operator") -> dict:
    """Plan (and unless dry_run, apply) the import of `<corpus>/jev/*.json`
    in one transaction. See the module docstring."""
    from . import ad_reads, citations, lineage
    from . import coverage as _coverage
    from . import import_sources as _src
    corpus = Path(corpus)
    run_id = run_id or "rescreen-" + datetime.datetime.now(
        datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    producer = f"{PRODUCER}:{run_id}"
    threshold = _src.JEV_THRESHOLD
    out = {"run_id": run_id, "producer": producer, "dry_run": dry_run, "recorded": 0,
           "created_evidence": 0, "reused_evidence": 0, "attestations": 0,
           "jev_run_version_id": None}
    with _db.transaction(con, write=not dry_run):
        plan = _plan_import(con, corpus, threshold)
        todo = [r for r in plan["episodes"] if r["verdict"] == "import"]
        out.update(previous_version_id=plan["old_version_id"], kept_entries=plan["kept"],
                   already_imported=plan["already"])
        if todo:
            # Each flagged window's record key and the rows it already is
            # (same copy, same span). A window key is positional; where an
            # earlier copy's row holds the plain key, the new row takes
            # `<window>@tv<copy>`, so the earlier-copy read is never taken
            # for replaced (customer.legacy_ad_where) and stays shown.
            decided, new_entries = {}, {}
            for r in todo:
                ep, doc, cur = r["episode_id"], r["_doc"], r["_copy"]
                decided[ep] = []
                renamed = {}
                for w in sorted(doc["windows"], key=lambda w: w["start_s"]):
                    if w["noul"] < threshold:
                        continue
                    start, end = float(w["start_s"]), float(w["end_s"])
                    plain = f"jev:{ep}:{w['window_id']}"
                    keys = (plain, f"{plain}@tv{cur['tv']}")
                    matches = [m for m in con.execute(
                        "SELECT e.id, e.source_version_id, e.orig_start_s, e.orig_end_s,"
                        " e.source_record_key FROM evidence e LEFT JOIN reviews rv"
                        " ON rv.evidence_id=e.id WHERE e.source_record_key IN (?, ?)"
                        " AND e.episode_id=? AND e.transcript_version_id IS ?"
                        " ORDER BY rv.evidence_id IS NULL, e.source_version_id DESC,"
                        " e.created_at DESC", (*keys, ep, cur["tv"])).fetchall()
                        # Same window and start; ends equal once both are
                        # clipped to the copy's end (an earlier run's last
                        # window may run past it; R5-03).
                        if m["orig_start_s"] == start
                        and min(float(m["orig_end_s"]), r["extent_s"]) == min(end, r["extent_s"])]
                    if matches:
                        key = matches[0]["source_record_key"]
                    elif con.execute("SELECT 1 FROM evidence WHERE source_record_key=? AND"
                                     " episode_id=?", (plain, ep)).fetchone():
                        key = keys[1]
                    else:
                        key = plain
                    decided[ep].append((w, key, matches))
                    renamed[w["window_id"]] = key[len(f"jev:{ep}:"):]
                entry = _corpus._jev_episode(ep, doc, (cur["duration_min"] or 0) * 60.0 or None)
                entry["answers"] = {renamed.get(wid, wid): a for wid, a in entry["answers"].items()}
                new_entries[ep] = {**entry, "run_id": run_id}
            out["created_evidence"] = sum(1 for d in decided.values() for _w, _k, m in d if not m)
            out["reused_evidence"] = sum(1 for d in decided.values() for _w, _k, m in d if m)
            out["attestations"] = sum(len(_spans(r["_doc"], r["extent_s"])) for r in todo)
            old_vid = plan["old_version_id"]
            out["kept_rows"] = 0 if old_vid is None else sum(
                1 for row in con.execute("SELECT id, source_version_id FROM evidence"
                                         " WHERE source='jev'").fetchall()
                if _visible_in(con, row, old_vid))
        if not dry_run and todo:
            _db.ensure_evidence_membership(con)
            _coverage._require_attestation_schema(con)
            human = lineage.human_work_ids(con)
            out.update(created_evidence=0, reused_evidence=0, attestations=0)
            old_by_ep = {str(e.get("episode_id") or e.get("ep_id")): e
                         for e in plan["old_run"].get("episodes") or [] if isinstance(e, dict)}
            entries = [old_by_ep[ep] for ep in plan["kept"]] + list(new_entries.values())
            payload = {k: v for k, v in _corpus.jev_results_doc([]).items() if k != "episodes"}
            payload.update(run_id=run_id, status="workspace_rescreen",
                           previous_version_id=plan["old_version_id"],
                           episodes=sorted(entries, key=lambda e: str(e.get("episode_id"))))
            blob = json.dumps(payload)
            version_no = con.execute("SELECT COALESCE(MAX(version_no),0)+1 FROM source_versions"
                                     " WHERE source_key='jev-run'").fetchone()[0]
            con.execute("UPDATE source_versions SET is_active=0 WHERE source_key='jev-run'"
                        " AND is_active=1")
            vid = con.execute(
                "INSERT INTO source_versions (source_key, version_no, sha256, payload,"
                " is_active, created_at) VALUES ('jev-run', ?, ?, ?, 1, ?)",
                (version_no, hashlib.sha256(blob.encode("utf-8")).hexdigest(), blob,
                 _db.utcnow())).lastrowid
            out["jev_run_version_id"] = vid
            # Every row active in the old run version stays active in the
            # new one, with its id and its copy: a re-screen never hides a
            # read (earlier-copy reads stay earlier-copy reads; saved items,
            # campaign links and alerts may cite them).
            kept_rows = 0
            if plan["old_version_id"] is not None:
                for row in con.execute("SELECT id, source_version_id FROM evidence"
                                       " WHERE source='jev'").fetchall():
                    if _src._is_visible(con, row, plan["old_version_id"]):
                        _src._set_visible(con, row, vid, True)
                        kept_rows += 1
            out["kept_rows"] = kept_rows
            created, audit_eps = [], []
            for r in todo:
                ep, doc, cur = r["episode_id"], r["_doc"], r["_copy"]
                ids = {"created": [], "reused": []}
                for w, key, matches in decided[ep]:
                    start, end = float(w["start_s"]), float(w["end_s"])
                    content = _src._named_content(key, ep, start, end)
                    if matches:
                        ids["reused"].append(_src._set_membership(con, matches, vid, human)["id"])
                        continue
                    eid = "jev:" + _src.sha256_bytes(json.dumps([content, cur["sha256"]]).encode())
                    attrs = {"window_id": w["window_id"], "noul": float(w["noul"]),
                             "model": doc.get("model"),
                             "raw_window": {"noul": w["noul"], "start_s": w["start_s"],
                                            "end_s": w["end_s"]}, "run_id": run_id}
                    con.execute(
                        "INSERT INTO evidence (id, source, source_version_id, episode_id,"
                        " orig_start_s, orig_end_s, excerpt, attrs, revision, created_at,"
                        " transcript_version_id, source_record_key)"
                        " VALUES (?, 'jev', ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                        (eid, vid, ep, start, end, _excerpt(cur["segments"], start, end),
                         json.dumps(attrs), _db.utcnow(), cur["tv"], key))
                    ids["created"].append(eid)
                created += ids["created"]
                out["created_evidence"] += len(ids["created"])
                out["reused_evidence"] += len(ids["reused"])
                audit_eps.append({"episode_id": ep, "transcript_version_id": cur["tv"],
                                  "segment_count": r["segment_count"],
                                  "segments_sha256": r["segments_sha256"],
                                  "screened_at": r["screened_at"], "windows": r["windows"],
                                  "flagged_windows": r["flagged_windows"], "evidence": ids})
            _src._reindex(con)
            if _db.has_v8_tables(con):
                digests: dict = {}
                for eid in created:
                    citations.publish_source_state(con, kind="evidence", record_id=eid,
                                                   actor=actor, digests=digests)
            out["ad_reads"] = ad_reads.rebuild_from_workspace(con)
            # The publication of every window's result for the current copy.
            for r, entry in zip(todo, audit_eps):
                ep, cur, doc = r["episode_id"], r["_copy"], r["_doc"]
                latest = con.execute(
                    "SELECT status FROM coverage_attestations WHERE workspace_id='local'"
                    " AND episode_id=? AND stage='observation_publication'"
                    " AND source_identity IS ? ORDER BY id DESC LIMIT 1",
                    (ep, cur["tv"])).fetchone()
                if latest is not None and latest["status"] != "success":
                    entry["attestation_ids"] = []
                    entry["attestation_skipped"] = "latest observation_publication failed"
                    continue
                dm = cur["duration_min"]
                req_end = max(r["extent_s"], float(dm) * 60.0 if isinstance(dm, (int, float))
                              and not isinstance(dm, bool) and dm > 0 else 0.0)
                spans = _spans(doc, r["extent_s"])
                entry["attestation_ids"] = [_coverage.record_attestation(
                    con, ep, "observation_publication", (0.0, req_end), (s, e),
                    source_pin=f"source-version:{cur['tv']}", producer=producer,
                    producer_rev=PRODUCER_REV, actor=actor) for s, e in spans]
                out["attestations"] += len(entry["attestation_ids"])
            out["recorded"] = len(todo)
            rev = _db.bump_revision(con)
            _db.audit(con, actor, "jev_import", "workspace", run_id,
                      {"producer": producer, "producer_rev": PRODUCER_REV,
                       "corpus": str(corpus), "jev_run_version_id": vid,
                       "previous_version_id": plan["old_version_id"],
                       "kept_entries": plan["kept"], "kept_rows": kept_rows,
                       "threshold": threshold,
                       "episodes": audit_eps}, rev)
    out["episodes"] = [{k: v for k, v in r.items() if not k.startswith("_")}
                       for r in plan["episodes"]]
    out["would_import"] = sum(r["verdict"] == "import" for r in plan["episodes"])
    return out


def format_report(result: dict) -> str:
    dry = result["dry_run"]
    lines = [("DRY RUN (writes nothing): " if dry else "")
             + f"producer {result['producer']}; previous jev-run version"
             f" {result.get('previous_version_id')}",
             "episode\tverdict\ttranscript_version\tsegments\twindows\tflagged"]
    for r in result["episodes"]:
        lines.append("\t".join(str(x) for x in (
            r["episode_id"], r["verdict"] + (": " + r["reason"] if r.get("reason") else ""),
            r.get("transcript_version_id"), r.get("segment_count"), r.get("windows", "-"),
            r.get("flagged_windows", "-"))))
    if result.get("already_imported"):
        lines.append(f"already imported: {', '.join(result['already_imported'])}")
    lines.append("kept active-run entries (no importable result for the episode): "
                 f"{', '.join(result['kept_entries']) or '-'}")
    if dry:
        lines.append(f"results {len(result['episodes'])}; would import {result['would_import']}"
                     f" episodes: would create {result['created_evidence']} new evidence rows,"
                     f" reuse {result['reused_evidence']}, keep {result.get('kept_rows', 0)}"
                     f" earlier rows active, record {result['attestations']}"
                     " observation_publication attestations; the new jev-run version is"
                     " created on the real run")
    else:
        lines.append(f"results {len(result['episodes'])}; to import {result['would_import']};"
                     f" recorded {result['recorded']} episodes, {result['created_evidence']} new"
                     f" evidence rows, {result['reused_evidence']} reused,"
                     f" {result['attestations']} observation_publication attestations;"
                     f" {result.get('kept_rows', 0)} earlier rows kept active;"
                     f" jev-run version {result['jev_run_version_id'] or 'unchanged'}")
    return "\n".join(lines)


# ---------------------------------------------------------------- proof gate

_ENTRY_CACHE: dict = {}
_DIGEST_CACHE: dict = {}


def _cached(cache: dict, key, make):
    if key not in cache:
        if len(cache) > 512:
            cache.clear()
        cache[key] = make()
    return cache[key]


def flagged_record_keys(entry: dict) -> tuple:
    """The record keys of a run entry's flagged windows (noul at or above
    the import threshold): the rows its screening publishes."""
    from . import import_sources as _src
    ep = str(entry.get("episode_id"))
    return tuple(sorted(f"jev:{ep}:{wid}" for wid, a in (entry.get("answers") or {}).items()
                        if isinstance(a, dict)
                        and float(a.get("noul") or 0) >= _src.JEV_THRESHOLD))


def run_entry_current(con, episode_id: str, transcript_version_id) -> bool:
    """True while the active jev-run holds an entry for this episode that
    read this transcript copy (version id, segment count and segments
    digest) and whose flagged windows' rows are visible in that run (R5-04).
    Proof derived from such an entry (the run-entry screen proof, the
    re-screen's publication proof) counts only then (R4-01)."""
    if transcript_version_id is None:
        return False
    row = con.execute("SELECT id, sha256 FROM source_versions WHERE"
                      " source_key='jev-run' AND is_active=1 ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return False

    def entries():
        try:
            payload = json.loads(con.execute(
                "SELECT payload FROM source_versions WHERE id=?", (row["id"],)).fetchone()[0])
        except (TypeError, ValueError):
            return {}
        out = {}
        for e in (payload.get("episodes") or []):
            if not isinstance(e, dict) or e.get("transcript_version_id") is None:
                continue
            ep = str(e.get("episode_id"))
            flagged = flagged_record_keys(e)
            out[ep] = (e.get("transcript_version_id"), e.get("segment_count"),
                       e.get("segments_sha256"), flagged)
        return out
    entry = _cached(_ENTRY_CACHE, (row["id"], row["sha256"]), entries).get(episode_id)
    if entry is None or entry[0] != transcript_version_id:
        return False
    # The entry's published rows must be visible in the active run (R5-04):
    # one query over its flagged windows' record keys.
    if entry[3] and _db.has_evidence_membership(con):
        shown = con.execute(
            "SELECT count(DISTINCT e.source_record_key) FROM evidence e"
            " LEFT JOIN evidence_source_membership m ON m.evidence_id=e.id"
            " AND m.source_version_id=? WHERE e.source='jev' AND e.episode_id=?"
            " AND e.transcript_version_id IS ? AND e.source_record_key IN (%s)"
            " AND COALESCE(m.member, e.source_version_id=?)=1" % ",".join("?" * len(entry[3])),
            (row["id"], episode_id, transcript_version_id, *entry[3], row["id"])).fetchone()[0]
        if shown != len(entry[3]):
            return False
    cur = con.execute("SELECT sha256, payload FROM source_versions WHERE id=?",
                      (transcript_version_id,)).fetchone()
    if cur is None:
        return False

    def digest():
        try:
            segments = json.loads(cur["payload"]).get("segments") or []
        except (TypeError, ValueError, AttributeError):
            return None
        return len(segments), _jev.segments_digest(segments)
    return _cached(_DIGEST_CACHE, (transcript_version_id, cur["sha256"]), digest) == entry[1:3]


# ---------------------------------------------------------------- reseed

def carry_forward(con, actor: str = "seed", rebuild: bool = True) -> dict:
    """After a seed (in its transaction): keep the latest imported re-screen
    (R4-01). The seed reactivates the run file it loaded; the re-screen's
    entries for episodes whose current copy the seed left unchanged (same
    version id, segment count, digest) are carried into the active run
    verbatim, and every row the re-screen version showed stays active
    (same ids, same `@tv` keys), so a reseed never hides a re-screen read.
    An entry whose copy changed is not carried: proof from it stops
    counting (run_entry_current). Idempotent: an identical carried version
    is reactivated, never duplicated. rebuild=False leaves the ad_reads
    rebuild to the caller (seed: it carries before lineage.reconcile and
    rebuilds after its own load). Returns a report."""
    from . import ad_reads
    from . import import_sources as _src
    rescreen = None
    for row in con.execute("SELECT id, payload FROM source_versions WHERE source_key='jev-run'"
                           " ORDER BY id DESC"):
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("status") == RESCREEN_STATUS:
            rescreen = (row["id"], payload)
            break
    active_id, active = _active_run(con)
    if rescreen is None or active_id is None or active_id == rescreen[0]:
        return {"carried": 0}
    # Each episode's current copy, decoded only for a re-screen entry that
    # names it, one at a time (IMPORTMEM-R1-01): never every transcript.
    pins = dict(con.execute("SELECT e.id, e.transcript_version_id FROM episodes e"
                            " JOIN source_versions sv ON sv.id=e.transcript_version_id"))
    carried = {}
    for e in rescreen[1].get("episodes") or []:
        ep = str(e.get("episode_id"))
        tv = pins.get(ep)
        if not (isinstance(e, dict) and e.get("run_id") and tv is not None
                and e.get("transcript_version_id") == tv):
            continue
        segments = ad_reads._stored_segments(con, tv)
        if (e.get("segment_count") == len(segments)
                and e.get("segments_sha256") == _jev.segments_digest(segments)):
            carried[ep] = e
        del segments
    if active.get("status") == RESCREEN_STATUS:
        return {"carried": 0}  # already a carried / re-screen version
    entries = [e for e in active.get("episodes") or [] if isinstance(e, dict)
               and str(e.get("episode_id") or e.get("ep_id")) not in carried]
    payload = {**{k: v for k, v in rescreen[1].items() if k != "episodes"},
               "carried_onto_version_id": active_id,
               "episodes": sorted(entries + list(carried.values()),
                                  key=lambda e: str(e.get("episode_id") or e.get("ep_id")))}
    blob = json.dumps(payload)
    sha = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    same = con.execute("SELECT id FROM source_versions WHERE source_key='jev-run' AND sha256=?",
                       (sha,)).fetchone()
    con.execute("UPDATE source_versions SET is_active=0 WHERE source_key='jev-run'"
                " AND is_active=1")
    if same:
        vid = same["id"]
        con.execute("UPDATE source_versions SET is_active=1 WHERE id=?", (vid,))
    else:
        version_no = con.execute("SELECT COALESCE(MAX(version_no),0)+1 FROM source_versions"
                                 " WHERE source_key='jev-run'").fetchone()[0]
        vid = con.execute("INSERT INTO source_versions (source_key, version_no, sha256, payload,"
                          " is_active, created_at) VALUES ('jev-run', ?, ?, ?, 1, ?)",
                          (version_no, sha, blob, _db.utcnow())).lastrowid
    _db.ensure_evidence_membership(con)
    kept = 0
    for row in con.execute("SELECT id, source_version_id FROM evidence WHERE source='jev'"
                           ).fetchall():
        if _src._is_visible(con, row, active_id) or _src._is_visible(con, row, rescreen[0]):
            if not _src._is_visible(con, row, vid):
                _src._set_visible(con, row, vid, True)
            kept += 1
    rebuilt = ad_reads.rebuild_from_workspace(con) if rebuild else None
    report = {"carried": len(carried), "episodes": sorted(carried), "jev_run_version_id": vid,
              "from_version_id": rescreen[0], "onto_version_id": active_id,
              "rows_active": kept, "reused_version": bool(same), "ad_reads": rebuilt}
    rev = _db.bump_revision(con)
    _db.audit(con, actor, "jev_carry_forward", "workspace", str(vid), report, rev)
    return report
