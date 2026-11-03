#!/usr/bin/env python3
"""Hosted speech-to-text evaluation: cost, wall time and, where blind
judgments exist, naming accuracy of a Groq/OpenRouter transcription.

  api_stt_eval.py --corpus DIR [--episodes ep_a,ep_b | --limit N]
                  [--mode clips|full] [--backend groq|openrouter] [--model M]
                  [--env-file PATH] [--max-cost-usd 1.0] [--judge-dir DIR]
                  [--work DIR] [--name-gliner small|none] [--fetch-missing]
                  [--dry-run] [--out report.json]

clips: the selected episodes (queue rows, transcripts, jev, names, clips) are
copied into a scratch corpus under --work (the source corpus is never
written) and the refine pass runs there on the API backend, so the result is
exactly what `corpus_run.py refine --backend ...` would write.

full: whole episodes are transcribed with api_stt.transcribe_episode (audio
from the queue's audio_path, or with --fetch-missing the enclosure
re-downloaded into --work), and every named span of the source names doc is
re-named on the new transcript. Re-downloaded audio whose sha256 differs
from the original fetch (dynamic ad insertion moves ads) is costed but not
scored.

Scoring: a judged span (judgments.json, type other than "unclear"; id
"<episode>#<span index>") counts as correct when the name leniently matches
(named_reads_eval.lenient_ok) the judge's brand or one of its `also`
brands. Reported side by side: the first-pass name, the source corpus's
current name, and the API name before ("api_raw") and after ("api_final")
refine's acceptance rule. --limit picks judged episodes first.

--max-cost-usd caps the total spend recorded in <work>/api-spend.sqlite3,
so repeated runs with the same --work share one budget. Exit 3 on a
rejected key / payment / persistent rate limit; a budget stop is reported,
not an error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from earwitness.pipeline import acquisition as acq  # noqa: E402
from earwitness.pipeline import api_stt  # noqa: E402
from earwitness.pipeline import brand_namer as bn  # noqa: E402
from earwitness.pipeline import corpus as cr  # noqa: E402
from earwitness.pipeline import refine as rf  # noqa: E402
from earwitness.pipeline.chunked_stt import atomic_write_json  # noqa: E402
from named_reads_eval import lenient_ok  # noqa: E402

DEFAULT_JUDGE_DIR = cr.REPO / ".corpus-scale-20261008" / "refine-judge"
COLUMNS = ("first_pass", "source_current", "api_raw", "api_final")


def load_judged(judge_dir) -> dict:
    """{episode_id: {span index: judgment}} for judged spans not "unclear"."""
    path = Path(judge_dir) / "judgments.json"
    if not path.is_file():
        return {}
    out: dict = {}
    for j in json.loads(path.read_text()):
        if j.get("type") == "unclear" or not j.get("brand"):
            continue
        ep, _, idx = j["id"].partition("#")
        out.setdefault(ep, {})[int(idx)] = j
    return out


def correct(name, judgment) -> bool:
    return bool(name) and any(lenient_ok(name, b) for b in
                              [judgment["brand"], *(judgment.get("also") or ())] if b)


def _ro(corpus) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{Path(corpus) / 'queue.sqlite3'}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def source_rows(corpus) -> dict:
    con = _ro(corpus)
    try:
        return {r["episode_id"]: dict(r) for r in
                con.execute("SELECT * FROM episodes WHERE stage='done' ORDER BY episode_id")}
    finally:
        con.close()


def has_input(corpus, ep: str, row: dict, mode: str, fetch: bool) -> bool:
    if mode == "clips":
        return bool(rf.refinable_clips(cr._read_json(Path(corpus) / "clips" / ep / "index.json")))
    return bool(row.get("audio_path") and Path(row["audio_path"]).is_file()) or (
        fetch and bool(row.get("enclosure_url")))


def select(corpus, rows: dict, judged: dict, a) -> list:
    if a.episodes:
        eps = [e.strip() for e in a.episodes.split(",") if e.strip()]
        missing = [e for e in eps if e not in rows]
        if missing:
            raise SystemExit(f"not done in {corpus}: {missing}")
        return eps
    order = sorted(rows, key=lambda e: (e not in judged, e))
    eps = [e for e in order if has_input(corpus, e, rows[e], a.mode, a.fetch_missing)]
    return eps[:a.limit] if a.limit else eps


def scratch_corpus(src, work: Path, eps: list, rows: dict) -> Path:
    """A corpus holding only `eps` (copied, so refine can write freely)."""
    dst = work / "corpus"
    if dst.exists():
        shutil.rmtree(dst)
    sdirs, ddirs = cr.corpus_dirs(src), cr.corpus_dirs(dst)
    for d in ddirs.values():
        d.mkdir(parents=True, exist_ok=True)
    con = cr.connect(dst)
    try:
        cr.ensure_schema(con)
        cols = {r[1] for r in con.execute("PRAGMA table_info(episodes)")}
        for ep in eps:
            row = {k: v for k, v in rows[ep].items() if k in cols}
            con.execute("INSERT INTO episodes (%s) VALUES (%s)"
                        % (",".join(row), ",".join("?" * len(row))), list(row.values()))
    finally:
        con.close()
    for ep in eps:
        for sub in ("transcripts", "jev", "names", "detections"):
            f = sdirs[sub] / f"{ep}.json"
            if f.is_file():
                shutil.copy2(f, ddirs[sub] / f.name)
        if (sdirs["clips"] / ep).is_dir():
            shutil.copytree(sdirs["clips"] / ep, ddirs["clips"] / ep)
    return dst


def brand_list_path(corpus, override):
    if override:
        return override
    for name in (rf.REFINED_BRAND_LIST_NAME, cr.BRAND_LIST_NAME):
        if (Path(corpus) / name).is_file():
            return str(Path(corpus) / name)
    return None


def _first_pass(doc):
    prev = doc.get("previous") if isinstance(doc, dict) and doc.get("refined") else doc
    return cr.named_spans(prev) or []


def _at(values, i):
    return values[i] if values is not None and i < len(values) else None


def score(eps: list, judged: dict, names: dict) -> dict:
    """names: {ep: {column: [brand per span index]}}"""
    acc = {"spans": 0, **{c: 0 for c in COLUMNS}, "api_spans": 0, "rows": []}
    for ep in eps:
        for idx, j in sorted((judged.get(ep) or {}).items()):
            got = {c: _at((names.get(ep) or {}).get(c), idx) for c in COLUMNS}
            acc["spans"] += 1
            has_api = (names.get(ep) or {}).get("api_final") is not None
            acc["api_spans"] += has_api
            row = {"id": f"{ep}#{idx}", "judge": j["brand"], **got}
            for c in COLUMNS:
                ok = correct(got[c], j)
                acc[c] += ok
                row[c + "_ok"] = ok
            acc["rows"].append(row)
    for c in COLUMNS:
        n = acc["api_spans"] if c.startswith("api") else acc["spans"]
        acc[c + "_pct"] = round(100 * acc[c] / n, 1) if n else None
    return acc


def source_names(corpus, ep) -> dict:
    doc = cr._read_json(Path(corpus) / "names" / f"{ep}.json")
    return {"first_pass": [s.get("brand") for s in _first_pass(doc)],
            "source_current": [s.get("brand") for s in cr.named_spans(doc) or []]}


# ---------------------------------------------------------------- modes

def run_clips(a, corpus, eps, rows, work, transcriber, log) -> dict:
    scratch = scratch_corpus(corpus, work, eps, rows)
    blist = brand_list_path(corpus, a.brand_list)
    est = lambda s: api_stt.request_cost(s, a.backend, a.model)["usd"]  # noqa: E731
    t0 = time.monotonic()
    rep = rf.refine(scratch, transcriber=transcriber, model=a.model, backend=a.backend,
                    only_tiny=False, rerefine=True, dry_run=a.dry_run, cost_estimate=est,
                    guard=False, log=log,  # "api_raw" scores the API's own text
                    namer=lambda: rf.build_refine_namer(scratch, gliner=a.name_gliner,
                                                        brand_list=blist, log=log))
    wall = round(time.monotonic() - t0, 1)
    names = {}
    for ep in eps:
        names[ep] = source_names(corpus, ep)
        doc = cr._read_json(scratch / "names" / f"{ep}.json")
        if isinstance(doc, dict) and doc.get("refine_backend") == a.backend:
            spans = cr.named_spans(doc) or []
            names[ep]["api_final"] = [s.get("brand") for s in spans]
            names[ep]["api_raw"] = [(s.get("refined_candidate") or s).get("brand")
                                    for s in spans]
    per_ep = []
    for ep in eps:
        ref = cr._read_json(scratch / "refined" / f"{ep}.json") or {}
        per_ep.append({"episode_id": ep, "clips": len(ref.get("clips") or ()),
                       "failed": len(ref.get("failed") or ()), "cost": ref.get("cost")})
    return {"wall_s": wall, "refine": {k: v for k, v in rep.items() if not isinstance(v, list)},
            "cost_usd": rep.get("cost_usd", 0.0), "cost_usd_est": rep.get("cost_usd_est"),
            "per_episode": per_ep, "names": names, "scratch": str(scratch)}


def _audio_for(a, ep, row, work, log):
    """(path, sha matches the original fetch) or (None, False)."""
    if row.get("audio_path") and Path(row["audio_path"]).is_file():
        return row["audio_path"], True
    if not a.fetch_missing or not row.get("enclosure_url"):
        return None, False
    dest = work / "audio"
    dest.mkdir(parents=True, exist_ok=True)
    have = next(dest.glob(f"{ep}.*"), None)
    if have is None:
        log(f"[eval] fetching {ep}")
        obs = acq.download_media(row["enclosure_url"], dest_dir=dest, name=f"{ep}.mp3",
                                 fetcher=acq.requests_fetcher(), max_bytes=cr.MEDIA_MAX_BYTES,
                                 timeout_s=60.0, deadline_s=cr.MEDIA_DEADLINE_S)
        have = Path(obs["path"])
    sha = hashlib.sha256(have.read_bytes()).hexdigest()
    return str(have), sha == row.get("audio_sha256")


def run_full(a, corpus, eps, rows, work, transcriber, log) -> dict:
    per_ep, names, total_wall = [], {}, 0.0
    if a.dry_run:
        for ep in eps:
            dur = rows[ep].get("duration_s") or 0.0
            per_ep.append({"episode_id": ep, "duration_s": dur, "cost_est": api_stt.episode_cost(
                dur, a.backend, a.model, chunk_s=a.chunk_s, overlap_s=a.overlap_s)})
        return {"per_episode": per_ep, "names": {ep: source_names(corpus, ep) for ep in eps},
                "wall_s": 0.0, "cost_usd": 0.0,
                "cost_usd_est": round(sum(p["cost_est"]["usd"] for p in per_ep), 6)}
    blist_path = brand_list_path(corpus, a.brand_list)
    blist = json.loads(Path(blist_path).read_text()) if blist_path else []
    namer = bn.BrandNamer(blist, None if a.name_gliner == "none" else a.name_gliner)
    out_dir = work / "transcripts"
    out_dir.mkdir(parents=True, exist_ok=True)
    stopped = None
    for ep in eps:
        row = rows[ep]
        audio, same = _audio_for(a, ep, row, work, log)
        if audio is None:
            per_ep.append({"episode_id": ep, "skipped": "no audio"})
            continue
        t0 = time.monotonic()
        try:
            res = api_stt.transcribe_episode(audio, transcriber, chunk_s=a.chunk_s,
                                             overlap_s=a.overlap_s,
                                             duration_s=row.get("duration_s") if same else None)
        except rf.BudgetExhausted as exc:
            stopped = str(exc)
            log(f"[eval] budget reached at {ep}: {exc}")
            break
        except rf.ClipFailed as exc:
            per_ep.append({"episode_id": ep, "error": str(exc)[:300]})
            continue
        wall = round(time.monotonic() - t0, 1)
        total_wall += wall
        atomic_write_json(out_dir / f"{ep}.json", {"ep_id": ep, "model": transcriber.model,
                                                   "segments": res["segments"],
                                                   "provenance": {"cost": res["cost"]}})
        per_ep.append({"episode_id": ep, "wall_s": wall, "audio_s": res["decoded_duration_s"],
                       "segments": len(res["segments"]), "cost": res["cost"],
                       "same_audio": same})
        names[ep] = source_names(corpus, ep)
        if same:
            doc = cr._read_json(Path(corpus) / "names" / f"{ep}.json")
            windows = bn.jev_windows(cr._read_json(Path(corpus) / "jev" / f"{ep}.json")) or []
            prev = doc.get("previous") if isinstance(doc, dict) and doc.get("refined") else doc
            cw = (prev or {}).get("context_windows", cr.DEFAULT_JEV_CONTEXT_WINDOWS)
            new = [bn.rename_span(sp, res["segments"], windows, namer, context_windows=cw)
                   for sp in _first_pass(doc)]
            names[ep]["api_final"] = names[ep]["api_raw"] = [s.get("brand") for s in new]
    out = {"per_episode": per_ep, "names": names, "wall_s": round(total_wall, 1),
           "cost_usd": round(transcriber.budget.spent, 6)}
    if stopped:
        out["budget_stopped"] = stopped
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--corpus", required=True)
    p.add_argument("--episodes", default=None, help="comma list of episode ids")
    p.add_argument("--limit", type=int, default=None, help="first N eligible (judged first)")
    p.add_argument("--mode", choices=("clips", "full"), default="clips")
    p.add_argument("--backend", choices=api_stt.API_BACKENDS, default="groq")
    p.add_argument("--model", default=None, help="default: the provider's turbo")
    p.add_argument("--env-file", default=str(cr.REPO / ".env"))
    p.add_argument("--max-cost-usd", type=float, default=1.0)
    p.add_argument("--api-rpm", type=float, default=None)
    p.add_argument("--api-language", default=None, help="ISO-639-1 hint (default: auto)")
    p.add_argument("--judge-dir", default=str(DEFAULT_JUDGE_DIR))
    p.add_argument("--work", default=None, help="scratch dir (default: a new temp dir)")
    p.add_argument("--name-gliner", choices=("small", "none"), default="small")
    p.add_argument("--brand-list", default=None,
                   help="repair list (default: the source corpus's brand-list-refined.json)")
    p.add_argument("--chunk-s", type=float, default=600.0)
    p.add_argument("--overlap-s", type=float, default=20.0)
    p.add_argument("--fetch-missing", action="store_true",
                   help="full mode: re-download audio that is not on disk")
    p.add_argument("--dry-run", action="store_true", help="estimate cost; no requests")
    p.add_argument("--out", default=None, help="also write the report JSON here")
    a = p.parse_args(argv)
    a.model = a.model or api_stt.PROVIDERS[a.backend]["model"]

    def log(m):
        print(m, file=sys.stderr, flush=True)

    corpus = Path(a.corpus)
    judged = load_judged(a.judge_dir)
    rows = source_rows(corpus)
    eps = select(corpus, rows, judged, a)
    work = Path(a.work or tempfile.mkdtemp(prefix="api-stt-eval-"))
    work.mkdir(parents=True, exist_ok=True)
    transcriber = None
    if not a.dry_run:
        try:
            transcriber = api_stt.from_args(a.backend, model=a.model, env_file=a.env_file,
                                            max_cost_usd=a.max_cost_usd, rpm=a.api_rpm,
                                            language=a.api_language,
                                            ledger=work / "api-spend.sqlite3", owner="eval",
                                            log=log)
        except api_stt.ApiUnavailable as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    try:
        run = (run_clips if a.mode == "clips" else run_full)(a, corpus, eps, rows, work,
                                                             transcriber, log)
    except rf.SpeechUnavailable as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 3
    acc = score(eps, judged, run.pop("names"))
    rep = {"mode": a.mode, "backend": a.backend, "model": a.model, "corpus": str(corpus),
           "episodes": len(eps), "judged_episodes": sum(e in judged for e in eps),
           "max_cost_usd": a.max_cost_usd, "dry_run": a.dry_run, **run, "accuracy": acc}
    if transcriber is not None:
        rep["requests"] = transcriber.requests
    if a.out:
        Path(a.out).write_text(json.dumps(rep, indent=1))
    print(json.dumps({k: v for k, v in rep.items() if k != "accuracy"}
                     | {"accuracy": {k: v for k, v in acc.items() if k != "rows"}}), flush=True)
    stop = rep.get("budget_stopped") or (rep.get("refine") or {}).get("budget_stopped")
    print(f"{a.mode} via {a.backend}/{a.model}: {len(eps)} episodes, cost ${rep['cost_usd']}"
          + (f" (est ${rep['cost_usd_est']})" if rep.get("cost_usd_est") is not None else "")
          + f", wall {rep['wall_s']}s; judged spans {acc['spans']}: first pass "
          f"{acc['first_pass_pct']}%, source now {acc['source_current_pct']}%, API raw "
          f"{acc['api_raw_pct']}%, API final {acc['api_final_pct']}% (of {acc['api_spans']})"
          + (f"; BUDGET: {stop}" if stop else ""), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
