#!/usr/bin/env python3
"""Score hardened detector candidates against human labels (T-042).

Comparison rule (docs/LABELING-PROTOCOL.md; also reports/labeling-report.md):
- Brands on both sides are gazetteer-normalized.
- Protocol metric (primary): TP = candidate with the same brand covering
  >50% of the labeled read (label coverage = overlap / read length).
- Strict metric: protocol TP AND the candidate window is at most 3x the
  read length or IoU >= 0.3 (a 90 s window cannot claim a 20 s read).
- FP = candidate with no matching read, INCLUDING organic FPs (a candidate
  substantially overlapping a labeled organic-mention span). Precision =
  TP / (TP + FP); organic_fp_rate = organic FPs / total detections
  (excluded included); organic_fp_rate_scored = organic FPs / (TP + FP).
- FN = labeled read with no matching candidate.
- Excluded (neither TP nor FP, counted separately): candidates
  substantially overlapping a label's ``unresolved`` span (overlap > 50% of
  the candidate or of the span), and candidates matching a read whose
  read_type is ``uncertain``. Uncertain reads are not in the denominator.
- A label document with ``"coverage": "partial"`` (the ad review's
  labeled examples, corpus_run.py corrections-labels) labels only some
  reads of its episode: only candidates substantially overlapping one of
  its labeled spans (reads, organic mentions, unresolved) are scored.
- Spans given only as ``seg_idx`` are resolved to times via the episode
  transcript (--transcript-root, default <repo>/transcripts). A label item
  that cannot be resolved fails the run unless --allow-unscorable.

Usage: score_labels.py [--labels-dir L ...] [--candidates C] [--out-dir O]
                       [--transcript-root T ...] [--allow-unscorable]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from earwitness.gazetteer import normalize_brand

FIVE_BRANDS = {"betterhelp", "mint mobile", "shopify", "ag1", "hellofresh"}
STRICT_MAX_LEN_RATIO = 3.0
STRICT_MIN_IOU = 0.3


def inter(a0, a1, b0, b1) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def label_coverage(read: tuple, cand: tuple) -> float:
    """Protocol base: share of the labeled read covered by the candidate."""
    length = read[1] - read[0]
    return inter(*read, *cand) / length if length > 0 else 0.0


def iou(a: tuple, b: tuple) -> float:
    union = max(a[1], b[1]) - min(a[0], b[0])
    return inter(*a, *b) / union if union > 0 else 0.0


def substantial_overlap(cand: tuple, span: tuple) -> bool:
    """Overlap > 50% of the candidate or > 50% of the labeled span."""
    ov = inter(*cand, *span)
    return ov > 0.5 * (cand[1] - cand[0]) or ov > 0.5 * (span[1] - span[0])


def protocol_ok(read: tuple, cand: tuple) -> bool:
    return label_coverage(read, cand) > 0.5


def strict_ok(read: tuple, cand: tuple) -> bool:
    return protocol_ok(read, cand) and (
        cand[1] - cand[0] <= STRICT_MAX_LEN_RATIO * (read[1] - read[0])
        or iou(read, cand) >= STRICT_MIN_IOU)


def load_segments(ep: str, roots: list[str]) -> list | None:
    for root in roots:
        path = os.path.join(root, f"{ep}.json")
        if os.path.exists(path):
            return json.load(open(path)).get("segments", [])
    return None


def resolve_span(item: dict, segs: list | None) -> tuple | None:
    """(start, end) from explicit times, else from seg_idx via the transcript."""
    if item.get("start_s") is not None and item.get("end_s") is not None:
        return float(item["start_s"]), float(item["end_s"])
    idx = item.get("seg_idx") or []
    if segs is None or not idx or not all(0 <= i < len(segs) for i in idx):
        return None
    return (float(min(segs[i]["start"] for i in idx)),
            float(max(segs[i]["end"] for i in idx)))


def _metric(reads, cands, ok, organic, unresolved, uncertain, ep, detail):
    """Greedy one-to-one matching (best IoU per read) under ``ok``."""
    out = {"TP": 0, "FP": 0, "FN": 0, "organic_fp": 0,
           "excluded": {"unresolved": 0, "uncertain": 0}, "hits": []}
    used: set[str] = set()
    for r in reads:
        pool = [c for c in cands if c["id"] not in used and c["brand"] == r["brand"]
                and r["brand"] and ok(r["span"], c["span"])]
        hit = max(pool, key=lambda c: iou(r["span"], c["span"]), default=None)
        if hit:
            used.add(hit["id"])
            out["TP"] += 1
            detail.append({"episode": ep, "outcome": "TP", "brand": r["raw_brand"],
                           "candidate": hit["id"]})
        else:
            out["FN"] += 1
            detail.append({"episode": ep, "outcome": "FN", "brand": r["raw_brand"],
                           "window": list(r["span"])})
        out["hits"].append(bool(hit))
    for c in cands:
        if c["id"] in used:
            continue
        if any(c["brand"] and c["brand"] == u["brand"] and protocol_ok(u["span"], c["span"])
               for u in uncertain):
            out["excluded"]["uncertain"] += 1
            outcome = "excluded-uncertain"
        elif any(substantial_overlap(c["span"], s) for s in unresolved):
            out["excluded"]["unresolved"] += 1
            outcome = "excluded-unresolved"
        elif any(substantial_overlap(c["span"], s) for s in organic):
            out["FP"] += 1
            out["organic_fp"] += 1
            outcome = "FP-organic"
        else:
            out["FP"] += 1
            outcome = "FP"
        detail.append({"episode": ep, "outcome": outcome, "brand": c["raw_brand"],
                       "candidate": c["id"]})
    return out


def _summary(m: dict) -> dict:
    tp, fp, fn = m["TP"], m["FP"], m["FN"]
    scored = tp + fp
    total = scored + sum(m["excluded"].values())
    return {"TP": tp, "FP": fp, "FN": fn,
            "precision": round(tp / scored, 3) if scored else 0.0,
            "recall": round(tp / (tp + fn), 3) if (tp + fn) else 0.0,
            "organic_fp": m["organic_fp"],
            "total_detections": total,
            "scored_detections": scored,
            # Protocol: organic FPs / total detections (excluded included).
            "organic_fp_rate": round(m["organic_fp"] / total, 3) if total else 0.0,
            "organic_fp_rate_scored": round(m["organic_fp"] / scored, 3) if scored else 0.0,
            "excluded_candidates": m["excluded"]}


def score(labels: list[dict], cands: list[dict], transcript_roots: list[str]) -> dict:
    """Score candidates against label documents (one per episode)."""
    by_ep: dict[str, list] = {}
    for c in cands:
        by_ep.setdefault(c["ep_id"], []).append(c)
    totals = {k: {"TP": 0, "FP": 0, "FN": 0, "organic_fp": 0,
                  "excluded": {"unresolved": 0, "uncertain": 0}}
              for k in ("protocol", "strict")}
    details: dict[str, list] = {"protocol": [], "strict": []}
    n_reads = n_uncertain = n_unresolved = n_organic = 0
    unscorable: list[dict] = []
    fb_tp = fb_fn = 0

    for lab in labels:
        ep = lab["episode_id"]
        segs = load_segments(ep, transcript_roots)

        def spans(items, kind):
            out = []
            for it in items:
                sp = resolve_span(it, segs)
                if sp is None:
                    unscorable.append({"episode": ep, "kind": kind,
                                       "brand": it.get("brand"), "seg_idx": it.get("seg_idx")})
                else:
                    out.append((it, sp))
            return out

        reads, uncertain = [], []
        for r, sp in spans(lab.get("reads", []), "read"):
            rec = {"brand": normalize_brand(r.get("brand")), "raw_brand": r.get("brand"),
                   "span": sp}
            (uncertain if r.get("read_type") == "uncertain" else reads).append(rec)
        organic = [sp for _, sp in spans(lab.get("organic_mentions", []), "organic")]
        unresolved = [sp for _, sp in spans(lab.get("unresolved", []), "unresolved")]
        ep_cands = [{"id": c["candidate_id"], "raw_brand": c.get("brand"),
                     "brand": normalize_brand(c.get("brand_spoken")),
                     "span": (float(c.get("start_s") or 0), float(c.get("end_s") or 0))}
                    for c in by_ep.get(ep, [])]
        if lab.get("coverage") == "partial":
            labeled = [r["span"] for r in reads + uncertain] + organic + unresolved
            ep_cands = [c for c in ep_cands
                        if any(substantial_overlap(c["span"], sp) for sp in labeled)]
        n_reads += len(reads)
        n_uncertain += len(uncertain)
        n_unresolved += len(lab.get("unresolved", []))
        n_organic += len(lab.get("organic_mentions", []))
        for name, ok in (("protocol", protocol_ok), ("strict", strict_ok)):
            m = _metric(reads, ep_cands, ok, organic, unresolved, uncertain, ep,
                        details[name])
            t = totals[name]
            for k in ("TP", "FP", "FN", "organic_fp"):
                t[k] += m[k]
            for k in t["excluded"]:
                t["excluded"][k] += m["excluded"][k]
            if name == "protocol":
                for r, hit in zip(reads, m["hits"]):
                    if r["brand"] in FIVE_BRANDS:
                        fb_tp += hit
                        fb_fn += not hit

    proto, strict = _summary(totals["protocol"]), _summary(totals["strict"])
    strict["rule"] = (f"protocol TP AND (candidate length <= {STRICT_MAX_LEN_RATIO:g}x read "
                      f"length OR IoU >= {STRICT_MIN_IOU})")
    strict["detail"] = details["strict"]
    return {
        "comparison_rule": (
            "protocol (primary): TP = same normalized brand + candidate covers >50% of the "
            "labeled read; FP = candidate with no such read, organic FPs included; FN = read "
            "with no such candidate. Candidates overlapping unresolved spans or matching "
            "uncertain reads are excluded; uncertain reads are not in the denominator."),
        "metric": "protocol",
        "labeled_episodes": sorted(lab["episode_id"] for lab in labels),
        "labeled_denominator_reads": n_reads,
        **proto,
        "uncertain_reads": n_uncertain,
        "organic_mentions_labeled": n_organic,
        "unresolved": n_unresolved,
        "unscorable_label_items": unscorable,
        "strict": strict,
        "five_brand_subset": {"TP": fb_tp, "FN": fb_fn,
                              "recall": round(fb_tp / (fb_tp + fb_fn), 3) if (fb_tp + fb_fn) else None,
                              "note": "LABELING-PROTOCOL narrow denominator (5 test brands); "
                                      "tiny in-sample count, uninformative alone — primary "
                                      "metrics are the overall ones."},
        "detail": details["protocol"],
    }


def load_labels(dirs: list[str]) -> list[dict]:
    labels: dict[str, dict] = {}
    for ldir in dirs:
        for lf in sorted(glob.glob(os.path.join(ldir, "*.json"))):
            if lf.endswith(".scored.json"):
                continue
            lab = json.load(open(lf))
            if "episode_id" not in lab:
                continue
            if lab["episode_id"] in labels:
                print(f"WARN: duplicate label for {lab['episode_id']} in {lf}; keeping first",
                      file=sys.stderr)
                continue
            labels[lab["episode_id"]] = lab
    return list(labels.values())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Score candidates vs human labels")
    ap.add_argument("--labels-dir", action="append", default=None,
                    help="Label directory (repeatable)")
    ap.add_argument("--candidates", default=None)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--transcript-root", action="append", default=None,
                    help="Transcript directory for seg_idx-only spans (repeatable)")
    ap.add_argument("--allow-unscorable", action="store_true",
                    help="Score even if some label items cannot be resolved to times "
                         "(they leave the denominator; listed in the report)")
    ns = ap.parse_args(argv)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ldirs = ns.labels_dir or [os.path.join(repo, "labels")]
    troots = ns.transcript_root or [os.path.join(repo, "transcripts")]
    cpath = ns.candidates or os.path.join(repo, "output", "candidates_normalized.json")
    odir = ns.out_dir or os.path.join(repo, "reports")
    os.makedirs(odir, exist_ok=True)

    report = score(load_labels(ldirs), json.load(open(cpath)), troots)
    bad = report["unscorable_label_items"]
    if bad and not ns.allow_unscorable:
        print(f"ERROR: {len(bad)} label item(s) have no times and no resolvable "
              f"transcript (roots: {troots}); first: {bad[0]}. Pass --transcript-root "
              f"or --allow-unscorable.", file=sys.stderr)
        return 2
    with open(os.path.join(odir, "labeling-metrics.json"), "w") as f:
        json.dump(report, f, indent=1)
    s = report["strict"]
    print(f"DONE: {report['labeled_denominator_reads']} labeled reads in "
          f"{len(report['labeled_episodes'])} episodes "
          f"(uncertain excluded: {report['uncertain_reads']})")
    print(f"protocol: TP={report['TP']} FP={report['FP']} FN={report['FN']} "
          f"precision={report['precision']:.3f} recall={report['recall']:.3f} "
          f"organic_fp={report['organic_fp']} organic_fp_rate={report['organic_fp_rate']:.3f} "
          f"(of {report['total_detections']} total detections; "
          f"{report['organic_fp_rate_scored']:.3f} of scored)")
    print(f"strict:   TP={s['TP']} FP={s['FP']} FN={s['FN']} "
          f"precision={s['precision']:.3f} recall={s['recall']:.3f}")
    print(f"excluded candidates: {report['excluded_candidates']}  "
          f"unresolved spans={report['unresolved']}  "
          f"unscorable label items={len(report['unscorable_label_items'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
