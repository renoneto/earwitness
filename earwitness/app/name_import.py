"""Suggested brand names for the re-screen's jev reads (`import-names`).

A jev window says where an ad read sits, not whose it is. The corpus runner's
name stage (`corpus_run.py name-backfill --name-gliner none`, local, no
network) names the flagged windows of a corpus exported from the workspace
(jev_rescreen); this module brings those names in as machine SUGGESTIONS,
without a reseed, in one transaction:

    import_names(con, corpus)     `app_workspace.py import-names`

Only jev reads on the episode's CURRENT transcript copy are candidates, and
only when the corpus's transcript copy and jev result are exactly that copy
(same version id and segments digest, as import-jev requires). A read gets a
name when the names file's span for its window (jev_window_ids) names a
brand that is not flagged for review, is not stoplisted, and is a known
brand (corrections.known_brand, or a brand a person attached or confirmed:
a brand_assocs row that is not detector-origin, or is confirmed; another
read's machine suggestion does not count), judged after the gazetteer's canonical spelling that the detector's
suggestions use ("chat gpt" is "chatgpt"; the audit keeps both): a machine suggestion of an unknown word names nothing, as for
every other read. Everything else is skipped and listed with its reason.

The suggestion is written through the existing machine-association refresh
(import_sources.refresh_machine_assocs): a `brand_assocs` row, origin
detector, basis suggested, channel spoken, plus an import-refresh
association snapshot. It is machine output, not human work (lineage), so a
reseed keeps it and a person's review stays the only way to confirm a name.
A read a person worked on (review, human brand, campaign link, ...) is never
touched. A rerun with nothing new writes nothing, not even an audit row;
a suggestion is never removed by this import.

import-names curates first (brand_curate.curate_brands, `--no-curate` skips
it): an off-list brand whose own ads corroborate it (a matching web address
and the name said, in at least two episodes) joins the known list in the same
transaction, so its reads are named in the same run. A dry run counts the
reads of the brands it would add.
"""
from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path

from earwitness.gazetteer import normalize_brand
from earwitness.pipeline import corpus as _corpus
from earwitness.pipeline import jev_screen as _jev

from . import brand_curate
from . import db as _db

PRODUCER = "names-import"
CHANNEL = "spoken"
TOLERANCE_S = 1.0   # a window and its span's range agree to this many seconds
REASONS = ("episode not usable", "reviewed", "other machine associations", "no span", "span does not cover window", "no brand", "needs review",
           "stoplisted", "off-list")


def _read_json(path: Path):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None


def _window_id(attrs: str, record_key) -> str | None:
    try:
        wid = (json.loads(attrs or "{}") or {}).get("window_id")
    except (TypeError, ValueError, AttributeError):
        wid = None
    if isinstance(wid, str) and wid:
        return wid
    # jev:<episode>:window_NNN or ...@tv<copy>
    tail = str(record_key or "").rsplit(":", 1)[-1].split("@", 1)[0]
    return tail or None


def _episode_check(corpus: Path, ep: str, cur: dict) -> tuple:
    """(names doc, sha256, None) when the corpus names this exact current
    copy; else (None, sha256 or None, reason)."""
    path = corpus / "names" / f"{ep}.json"
    if not path.is_file():
        return None, None, "no names file"
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    doc = _read_json(path)
    if not isinstance(doc, dict) or not isinstance(doc.get("spans"), list):
        return None, sha, "names file is not a names document"
    if doc.get("episode_id") not in (None, ep):
        return None, sha, "names file is for another episode"
    digest = _jev.segments_digest(cur["segments"])
    tdoc = _read_json(corpus / "transcripts" / f"{ep}.json")
    try:
        copy = _corpus._check_workspace_copy(tdoc, ep)
    except Exception as exc:  # TranscriptInvalid, or a missing/garbled file
        return None, sha, f"corpus transcript is not a checked workspace copy: {exc}"
    if copy["transcript_version_id"] != cur["tv"] or copy["segments_sha256"] != digest:
        return None, sha, (f"names read another transcript copy (version"
                           f" {copy['transcript_version_id']}; current copy is {cur['tv']})")
    jev = _read_json(corpus / "jev" / f"{ep}.json")
    if not isinstance(jev, dict) or jev.get("transcript_version_id") != cur["tv"] \
            or jev.get("segments_sha256") != digest:
        return None, sha, "the corpus jev result is not for the current transcript copy"
    return doc, sha, None


def _span_for(doc: dict, window_id: str):
    for sp in doc["spans"]:
        if isinstance(sp, dict) and window_id in (sp.get("jev_window_ids") or ()):
            return sp
    return None


def import_names(con, corpus, *, dry_run: bool, run_id: str | None = None,
                 actor: str = "operator", curate: bool = True) -> dict:
    """Plan (and unless dry_run, apply) the suggestions from
    `<corpus>/names/*.json` in one transaction. Curates the known-brand list
    first unless `curate` is false. See the module docstring."""
    from . import ad_reads, corrections, jev_rescreen
    from . import import_sources as _src
    corpus = Path(corpus)
    run_id = run_id or "names-" + datetime.datetime.now(
        datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = {"run_id": run_id, "producer": PRODUCER, "dry_run": dry_run, "written": 0,
           "would_write": 0, "earlier_copy_rows": 0, "episodes": [], "decisions": [],
           "names_sha256": {}, "counts": {"suggested": 0, "updated": 0, "unchanged": 0,
                                          "skipped": {r: 0 for r in REASONS}}}
    with _db.transaction(con, write=not dry_run):
        curated: set = set()
        if curate:
            out["curated"] = brand_curate.curate_brands(con, corpus, dry_run=dry_run,
                                                        run_id=run_id, actor=actor)
            curated = {a["brand"] for a in out["curated"]["added"]}
        current = jev_rescreen._current_copies(con)
        vid, _run = jev_rescreen._active_run(con)
        rules = corrections.load(con)
        # Brands a person attached (never another read's machine suggestion).
        # PODS: model rows are display-only
        held = {ad_reads.brand_key(b) for (b,) in con.execute(
            "SELECT DISTINCT brand FROM brand_assocs WHERE basis<>'model'"
            " AND (origin<>'detector' OR basis='confirmed')")}
        known_cache: dict = {}

        def known(key):
            if key not in known_cache:
                known_cache[key] = key in held or key in curated \
                    or corrections.known_brand(con, key, rules)
            return known_cache[key]

        rows = con.execute(
            "SELECT e.id, e.episode_id, e.source_version_id, e.orig_start_s, e.orig_end_s,"
            " e.source_record_key, e.attrs, e.transcript_version_id AS tv FROM evidence e"
            " JOIN episodes ep ON ep.id=e.episode_id WHERE e.source='jev'"
            " ORDER BY e.episode_id, e.orig_start_s, e.id").fetchall()
        by_ep: dict = {}
        for r in rows:
            cur = current.get(r["episode_id"])
            if cur is None or r["tv"] != cur["tv"]:
                out["earlier_copy_rows"] += 1
                continue
            if not jev_rescreen._visible_in(con, r, vid):
                continue
            by_ep.setdefault(r["episode_id"], []).append(r)
        changed = []
        for ep, eps_rows in sorted(by_ep.items()):
            doc, sha, why = _episode_check(corpus, ep, current[ep])
            if sha:
                out["names_sha256"][ep] = sha
            out["episodes"].append({"episode_id": ep, "verdict": "skip" if why else "names",
                                    "reason": why or "", "reads": len(eps_rows),
                                    "names_sha256": sha})
            if why:
                for r in eps_rows:
                    out["decisions"].append({
                        "evidence_id": r["id"], "episode_id": ep,
                        "window_id": _window_id(r["attrs"], r["source_record_key"]),
                        "decision": "skip", "reason": "episode not usable: " + why,
                        "brand": None, "raw_brand": None, "span_id": None})
                    out["counts"]["skipped"]["episode not usable"] += 1
                continue
            for r in eps_rows:
                wid = _window_id(r["attrs"], r["source_record_key"])
                d = {"evidence_id": r["id"], "episode_id": ep, "window_id": wid,
                     "decision": "skip", "reason": "", "brand": None, "raw_brand": None,
                     "span_id": None}
                out["decisions"].append(d)
                sp = _span_for(doc, wid) if wid else None
                if sp is not None:
                    d["span_id"] = sp.get("span_id")
                    d["raw_brand"] = d["brand"] = sp.get("brand")
                if _src._has_row_edits(con, r["id"]):
                    d["reason"] = "reviewed"
                elif sp is None:
                    d["reason"] = "no span"
                elif not (sp.get("start_s") is not None and sp.get("end_s") is not None
                          and sp["start_s"] - TOLERANCE_S <= r["orig_start_s"]
                          and r["orig_end_s"] <= sp["end_s"] + TOLERANCE_S):
                    d["reason"] = "span does not cover window"
                elif not ad_reads.brand_key(sp.get("brand")):
                    d["reason"] = "no brand"
                elif sp.get("needs_review"):
                    d["reason"] = "needs review"
                else:
                    # The key the detector's suggestions use: the gazetteer's
                    # canonical brand ("chat gpt" is "chatgpt"), else as heard.
                    raw = ad_reads.brand_key(sp["brand"])
                    key = ad_reads.brand_key(normalize_brand(raw) or raw)
                    d["raw_brand"], d["brand"] = raw, key
                    if key in rules["stoplist"]:
                        d["reason"] = "stoplisted"
                    elif not known(key):
                        d["reason"] = "off-list"
                if d["reason"]:
                    out["counts"]["skipped"][d["reason"]] += 1
                    continue
                machine: dict = {}
                for b, c in con.execute("SELECT brand, channel FROM brand_assocs WHERE"
                                        " evidence_id=? AND origin='detector' ORDER BY id",
                                        (r["id"],)):
                    machine.setdefault(c, []).append(b)
                if set(machine) - {"spoken", "url", "code"} or any(
                        len(v) > 1 for c, v in machine.items() if c != CHANNEL):
                    d["reason"] = "other machine associations"
                    out["counts"]["skipped"][d["reason"]] += 1
                    continue
                have = machine.get(CHANNEL, [])   # only the spoken channel is the namer's
                if have == [d["brand"]]:
                    d.update(decision="unchanged")
                    out["counts"]["unchanged"] += 1
                    continue
                d["decision"] = "update" if have else "suggest"
                out["would_write"] += 1
                if dry_run:
                    out["counts"]["updated" if have else "suggested"] += 1
                    continue
                # URL and code attributions pass through unchanged: the refresh
                # replaces every machine association of the read.
                incoming = {"brand_spoken": d["brand"]}
                for key, channel in (("brand_url", "url"), ("brand_code", "code")):
                    if machine.get(channel):
                        incoming[key] = machine[channel][0]
                if _src.refresh_machine_assocs(con, r["id"], incoming, actor):
                    changed.append(r["id"])
                    out["counts"]["updated" if have else "suggested"] += 1
                else:
                    d.update(decision="skip", reason="reviewed")
                    out["would_write"] -= 1
                    out["counts"]["skipped"]["reviewed"] += 1
        if changed:
            _src._reindex(con)
            corrections.publish_keeping_trackers(con, changed, actor)
            out["written"] = len(changed)
            rev = _db.bump_revision(con)
            _db.audit(con, actor, "names_import", "workspace", run_id,
                      {"producer": PRODUCER, "corpus": str(corpus), "channel": CHANNEL,
                       "names_sha256": out["names_sha256"], "counts": out["counts"],
                       "earlier_copy_rows": out["earlier_copy_rows"],
                       "episodes": out["episodes"], "decisions": out["decisions"]}, rev)
    return out


def format_report(result: dict) -> str:
    dry = result["dry_run"]
    c = result["counts"]
    cur = result.get("curated")
    lines = [brand_curate.format_report(cur)] if cur and (
        cur["added"] or cur["skipped"] or cur.get("skipped_all")) else []
    lines += [("DRY RUN (writes nothing): " if dry else "")
              + f"producer {result['producer']}, run {result['run_id']}",
              "episode\tverdict\treads\treason"]
    for e in result["episodes"]:
        lines.append(f"{e['episode_id']}\t{e['verdict']}\t{e['reads']}\t{e['reason']}")
    lines.append("read\tdecision\tbrand\treason")
    for d in result["decisions"]:
        heard = f" (heard {d['raw_brand']})" if d.get("raw_brand") not in (None, d["brand"]) else ""
        lines.append(f"{d['evidence_id']}\t{d['decision']}\t{d['brand'] or '-'}{heard}"
                     f"\t{d['reason']}")
    skipped = ", ".join(f"{k} {v}" for k, v in c["skipped"].items() if v) or "none"
    lines.append(f"suggest {c['suggested']}, update {c['updated']}, unchanged {c['unchanged']};"
                 f" skipped: {skipped}; earlier-copy reads not considered:"
                 f" {result['earlier_copy_rows']}")
    lines.append(f"would write {result['would_write']}" if dry
                 else f"written {result['written']} suggestions")
    return "\n".join(lines)
