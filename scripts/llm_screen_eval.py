#!/usr/bin/env python3
"""Offline evaluation of LLM ad-read screening against human labels.

For every labeled episode (--labels-dir, repeatable) whose transcript is
found as <episode_id>.json under a --transcript-root (repeatable), screen
the transcript with earwitness.pipeline.llm_screen, write the reads as
detector-shaped candidates, and score them with scripts/score_labels.py:
protocol and strict metrics, plus location-only recall (a candidate of
ANY brand covers >50% of a labeled read), tokens and cost.

  llm_screen_eval.py --labels-dir L [--labels-dir L2] --transcript-root T
                     [--model claude-haiku-5-5] [--effort low] --out DIR
                     [--batch] [--dry-run]

--dry-run   count windows and estimate tokens/cost; no API call, no key.
--batch     submit every window through the Message Batches API (50% price;
            usually done within an hour) instead of one call per window.

Needs `pip install anthropic` and ANTHROPIC_API_KEY unless --dry-run.
Outputs in --out: candidates_normalized.json, screens/<ep>.json,
labeling-metrics.json (full scorer report), metrics.json (summary).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import score_labels as sl  # noqa: E402
from earwitness.pipeline import llm_screen as ls  # noqa: E402
from earwitness.pipeline.chunked_stt import atomic_write_json  # noqa: E402

DRY_RUN_OUTPUT_TOKENS_PER_WINDOW = 400  # assumption: short JSON + low-effort thinking


def location_recall(labels: list, cands: list, roots: list) -> dict:
    """Labeled (non-uncertain) reads covered >50% by a candidate of any brand."""
    by_ep = {}
    for c in cands:
        by_ep.setdefault(c["ep_id"], []).append((float(c.get("start_s") or 0),
                                                 float(c.get("end_s") or 0)))
    hit = total = 0
    for lab in labels:
        segs = sl.load_segments(lab["episode_id"], roots)
        for r in lab.get("reads", []):
            if r.get("read_type") == "uncertain":
                continue
            span = sl.resolve_span(r, segs)
            if span is None:
                continue
            total += 1
            hit += any(sl.label_coverage(span, c) > 0.5 for c in by_ep.get(lab["episode_id"], []))
    return {"hit": hit, "reads": total, "recall": round(hit / total, 3) if total else None}


def _meta(lab: dict) -> dict:
    return {"show_title": lab.get("show_title", "?"), "ep_title": lab.get("ep_title", "?"),
            "pub_date": lab.get("pub_date", "?"), "fetch_date": None}


def screen_sync(jobs, client, a) -> dict:
    return {ep: ls.screen_transcript(segs, client=client, model=a.model, window_s=a.window_s,
                                     overlap_s=a.overlap_s, effort=a.effort)
            for ep, segs in jobs}


class BatchTimeout(RuntimeError):
    pass


def _batch_failure(res) -> str:
    """Failure reason for a non-succeeded batch result, with the API's
    error type and message when it gave one."""
    if res is None:
        return "batch_missing"
    err = getattr(res, "error", None)
    inner = getattr(err, "error", None) or err
    detail = " ".join(str(x) for x in (getattr(inner, "type", None),
                                       getattr(inner, "message", None)) if x)
    return f"batch_{res.type}" + (f": {detail}" if detail else "")


def screen_batch(jobs, client, a, poll_s: float) -> dict:
    """One Message Batches request per window; results keyed by custom_id."""
    plan, requests = [], []
    for i, (ep, segs) in enumerate(jobs):
        windows = ls.split_windows(segs, a.window_s, a.overlap_s)
        plan.append((ep, segs, windows))
        for k, (lo, hi) in enumerate(windows):
            requests.append({"custom_id": f"e{i}w{k}", "params": ls.build_request(
                segs, lo, hi, model=a.model, effort=a.effort, k=k, n=len(windows))})
    if not requests:
        return {ep: ls.assemble(segs, [], [], model=a.model, effort=a.effort)
                for ep, segs, _ in plan}
    batch = client.messages.batches.create(requests=requests)
    print(f"batch {batch.id}: {len(requests)} requests submitted", flush=True)
    deadline = time.monotonic() + a.batch_timeout_s
    while client.messages.batches.retrieve(batch.id).processing_status != "ended":
        if time.monotonic() >= deadline:
            raise BatchTimeout(f"batch {batch.id} not ended after {a.batch_timeout_s:g}s; "
                               "results stay retrievable for 29 days")
        time.sleep(poll_s)
    results = {r.custom_id: r.result for r in client.messages.batches.results(batch.id)}
    out = {}
    for i, (ep, segs, windows) in enumerate(plan):
        outcomes = []
        for k, (lo, hi) in enumerate(windows):
            res = results.get(f"e{i}w{k}")
            if res is None or res.type != "succeeded":
                outcomes.append(([], _batch_failure(res), ls.add_usage({}, {})))
                continue
            reads, failure = ls.parse_response(res.message, segs, lo, hi, k)
            outcomes.append((reads, failure, ls.usage_of(res.message)))
        out[ep] = ls.assemble(segs, windows, outcomes, model=a.model, effort=a.effort)
    return out


def dry_run(jobs, a) -> int:
    windows = system = user = 0
    for _, segs in jobs:
        e = ls.estimate(segs, a.window_s, a.overlap_s, a.model)
        windows += e["windows"]
        system = e["system_tokens_est"]
        user += e["user_tokens_est"]
    out_tok = windows * DRY_RUN_OUTPUT_TOKENS_PER_WINDOW
    cached = {"input_tokens": user, "output_tokens": out_tok,
              "cache_creation_input_tokens": system if windows else 0,
              "cache_read_input_tokens": system * max(0, windows - 1)}
    uncached = {"input_tokens": user + system * windows, "output_tokens": out_tok}
    print(f"episodes: {len(jobs)}  windows: {windows}  model: {a.model}  effort: {a.effort}")
    print(f"estimated input tokens: {user + system * windows} "
          f"(system prompt ~{system}/window, transcript ~{user} total; "
          f"~{ls.CHARS_PER_TOKEN_EST} chars/token)")
    print(f"assumed output tokens: {out_tok} ({DRY_RUN_OUTPUT_TOKENS_PER_WINDOW}/window)")
    for label, usage in (("system prompt cached", cached), ("no cache hits", uncached)):
        c = ls.cost_usd(usage, a.model)
        if c is None:
            print(f"cost: no price for {a.model} in llm_screen.PRICES_PER_MTOK")
            break
        print(f"estimated cost_usd ({label}): {c:.4f}  batch: "
              f"{ls.cost_usd(usage, a.model, batch=True):.4f}")
    return 0


def main(argv=None, client=None, poll_s: float | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--labels-dir", action="append", required=True)
    p.add_argument("--transcript-root", action="append", required=True)
    p.add_argument("--model", default=ls.DEFAULT_MODEL)
    p.add_argument("--effort", default="low",
                   choices=("low", "medium", "high", "xhigh", "max"))
    p.add_argument("--out", default=None, help="output dir (required unless --dry-run)")
    p.add_argument("--window-s", type=float, default=600.0)
    p.add_argument("--overlap-s", type=float, default=60.0)
    p.add_argument("--batch", action="store_true")
    p.add_argument("--poll-s", type=float, default=60.0, help="batch status poll interval")
    p.add_argument("--batch-timeout-s", type=float, default=86400.0,
                   help="give up polling after this long (exit 3, batch id printed)")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    poll_s = a.poll_s if poll_s is None else poll_s

    labels, jobs, missing = [], [], []
    for lab in sl.load_labels(a.labels_dir):
        segs = sl.load_segments(lab["episode_id"], a.transcript_root)
        if segs is None:
            missing.append(lab["episode_id"])
            continue
        labels.append(lab)
        jobs.append((lab["episode_id"], segs))
    if missing:
        print(f"WARN: no transcript for {len(missing)} labeled episode(s): {missing}",
              file=sys.stderr)
    if a.dry_run:
        return dry_run(jobs, a)
    if not a.out:
        p.error("--out is required unless --dry-run")
    if client is None:
        try:
            client = ls.make_client()
        except ls.ScreenUnavailable as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    warn = ls.price_warning(a.model)
    if warn:
        print(warn, file=sys.stderr)
    out = Path(a.out)
    try:
        results = (screen_batch(jobs, client, a, poll_s) if a.batch
                   else screen_sync(jobs, client, a))
    except BatchTimeout as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # API errors after SDK retries: report, no traceback
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    cands, usage, windows, failed = [], ls.add_usage({}, {}), 0, 0
    meta = {lab["episode_id"]: _meta(lab) for lab in labels}
    for ep, res in sorted(results.items()):
        ls.assign_read_ids(ep, res)
        res["cost_usd"] = ls.cost_usd(res["usage"], a.model, batch=a.batch)
        atomic_write_json(out / "screens" / f"{ep}.json", res)
        cands.extend(ls.llm_candidates(ep, res, meta[ep]))
        ls.add_usage(usage, res["usage"])
        windows += res["windows"]
        failed += len(res["failed_windows"])
    atomic_write_json(out / "candidates_normalized.json", cands)
    report = sl.score(labels, cands, a.transcript_root)
    atomic_write_json(out / "labeling-metrics.json", report)
    keys = ("TP", "FP", "FN", "precision", "recall", "organic_fp")
    cost = ls.cost_usd(usage, a.model, batch=a.batch)
    summary = {
        "model": a.model, "effort": a.effort, "batch": a.batch,
        "prompt_version": ls.PROMPT_VERSION, "episodes": len(jobs),
        "episodes_without_transcript": missing, "windows": windows, "failed_windows": failed,
        "candidates": len(cands), "usage": usage, "total_tokens": sum(usage.values()),
        "cost_usd": cost,
        "protocol": {k: report[k] for k in keys},
        "strict": {k: report["strict"][k] for k in keys},
        "location_recall": location_recall(labels, cands, a.transcript_root),
        "labeled_reads": report["labeled_denominator_reads"],
        "unscorable_label_items": len(report["unscorable_label_items"]),
    }
    atomic_write_json(out / "metrics.json", summary)
    pr, st, loc = summary["protocol"], summary["strict"], summary["location_recall"]
    print(f"{a.model} effort={a.effort}{' batch' if a.batch else ''}: {len(jobs)} episodes, "
          f"{windows} windows ({failed} failed), {len(cands)} candidates, "
          f"{summary['labeled_reads']} labeled reads")
    for name, m in (("protocol", pr), ("strict", st)):
        print(f"{name + ':':<10}TP={m['TP']} FP={m['FP']} FN={m['FN']} "
              f"precision={m['precision']:.3f} recall={m['recall']:.3f}")
    print(f"location-only recall: {loc['hit']}/{loc['reads']} = {loc['recall']}")
    print(f"tokens: {usage}  total={summary['total_tokens']}  cost_usd={cost}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
