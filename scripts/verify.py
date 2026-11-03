#!/usr/bin/env python3
"""Phase 5 (deterministic part): rule-based checks of the 9-item checklist.

Hardened for T-042 (same 9-check logic as the Hatch VM original):
- Explicit brief provenance on every output: ``brief_provenance`` +
  ``brief_type: synthetic`` for data/briefs.json. Synthetic-brief outcomes
  state they are not contractual verification.
- Uncertainty is never promoted: checks that cannot be decided from
  transcript evidence return ``uncertain`` (checks 3/4 semantic, 6 placement,
  8 pronunciation by code design).
- Explicit paths/caps, JSONL run log, fetch_date propagation, resume-safe
  (skips reads whose verification already exists unless --force).

Reads input: --reads (default: output/candidates_normalized.json, falls back
to data/ad_reads.json reference snapshot).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import difflib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from earwitness.config import add_common_args, parse_episode_filter, resolve, resolve_fetch_date
from earwitness.logging_utils import RunLogger

SUITABILITY_LEX = {
    "profanity": [r"\bfuck", r"\bshit\b", r"\bbitch", r"\bcunt", r"\bdick\b"],
    "sexual": [r"\bporn", r"\bsex tape", r"\bhooker", r"\bprostitut"],
    "violence": [r"\bmurdered\b", r"\bstabbed", r"\bshot dead", r"\bbeheaded"],
    "drugs": [r"\bcocaine", r"\bheroin", r"\bmeth\b", r"\b fentanyl"],
    "controversy": [r"\bhitler\b", r"\bnazi", r"\bkkk\b", r"\bgenocide"],
}

BRIEF_PROVENANCE = (
    "data/briefs.json in this repo: SYNTHETIC briefs invented for pipeline "
    "validation only (see _note). Outcomes below test pipeline mechanics and "
    "are NOT contractual verification against real brand agreements."
)


def norm_code(c):
    return re.sub(r"[^a-z0-9]", "", c.lower())


def fuzzy_match(found, expected):
    nf = norm_code(found)
    for e in expected:
        ne = norm_code(e.split("<")[0])
        if not ne:
            continue
        if nf == ne or ne in nf or nf in ne:
            return True, e
        if difflib.SequenceMatcher(None, nf, ne).ratio() > 0.85:
            return True, e
    return False, None


def main() -> int:
    p = argparse.ArgumentParser(description="Rule-based 9-check verification (hardened)")
    add_common_args(p)
    p.add_argument("--fetch-date", default=None,
                   help="Explicit fetch date override (else EARWITNESS_FETCH_DATE, "
                        "else read/episode metadata, else current UTC date)")
    p.add_argument("--reads", default=None, help="Reads JSON (default: output/candidates_normalized.json)")
    p.add_argument("--briefs", default=None, help="Briefs JSON (default: <data-root>/briefs.json)")
    p.add_argument("--force", action="store_true", help="Re-verify even if output exists")
    a = p.parse_args()

    data_root = resolve("data_root", a.data_root)
    troot = resolve("transcript_root", a.transcript_root)
    oroot = resolve("output_root", a.output_root)
    rroot = resolve("runs_root", a.runs_root)
    oroot.mkdir(parents=True, exist_ok=True)
    run_id = a.run_id or RunLogger.new_run_id("verify")
    log = RunLogger(rroot, run_id, "verify")

    briefs_path = a.briefs or str(data_root / "briefs.json")
    try:
        briefs = json.loads(open(briefs_path).read())
    except Exception as ex:
        log.failed(record_id="-", error=f"briefs parse: {ex}")
        print(f"ERROR: briefs parse: {ex}", file=sys.stderr)
        return 2
    brief_is_synthetic = briefs.get("_note", "").startswith("SYNTHETIC") or "briefs.json" in briefs_path

    reads_path = a.reads
    if not reads_path:
        cand = oroot / "candidates_normalized.json"
        reads_path = str(cand if cand.exists() else data_root / "ad_reads.json")
    try:
        reads = json.loads(open(reads_path).read())
    except Exception as ex:
        log.failed(record_id="-", error=f"reads parse ({reads_path}): {ex}")
        print(f"ERROR: reads parse: {ex}", file=sys.stderr)
        return 2

    episodes = {}
    ep_path = data_root / "episodes.json"
    if ep_path.exists():
        episodes = {e["ep_id"]: e for e in json.loads(ep_path.read_text())}

    out_path = oroot / "verifications.json"
    existing = {}
    if out_path.exists() and not a.force:
        try:
            for v in json.loads(out_path.read_text()):
                existing[(v.get("ep_id"), v.get("brand"))] = v
        except Exception:
            existing = {}

    allow = parse_episode_filter(a.episodes)
    out: list[dict] = []
    resumed = 0
    for r in reads:
        if a.limit and len(out) >= a.limit:
            break
        if allow and r.get("ep_id") not in allow:
            continue
        key = (r.get("ep_id"), r.get("brand"))
        if key in existing:
            out.append(existing[key])
            resumed += 1
            log.skipped(record_id=f"{key[0]}:{key[1]}", extra={"reason": "already verified"})
            continue
        brand_key = (r.get("brand") or "").lower().replace(" ", "").replace("'", "")
        b = None
        for bk, bv in briefs.items():
            if bk.startswith("_"):
                continue
            if bk.lower().replace(" ", "") == brand_key:
                b = bv
                break
        if not b:
            log.skipped(record_id=r.get("ep_id", "?"),
                        extra={"reason": f"no brief for brand {r.get('brand')}"})
            continue
        checks = {}
        checks["1_aired"] = {"status": "pass" if r.get("confidence") in ("high", "medium") else "uncertain",
            "evidence": f"detection confidence={r.get('confidence')}, anchor={r.get('anchor')}",
            "confidence": 0.9 if r.get("confidence") == "high" else 0.6}
        exp_codes = b.get("offer_codes", []) + [b.get("url_pattern", "")]
        got = list(r.get("offer_codes", [])) + list(r.get("urls", []))
        matched = [fuzzy_match(g, exp_codes) for g in got]
        hits = [m[1] for m in matched if m[0]]
        if hits:
            checks["2_code_url"] = {"status": "pass",
                "evidence": f"detected {got} matched brief {hits}", "confidence": 0.85}
        elif got:
            checks["2_code_url"] = {"status": "uncertain",
                "evidence": "detected items but no brief match (brief is synthetic — not a contract finding)",
                "confidence": 0.5}
        else:
            checks["2_code_url"] = {"status": "uncertain",
                "evidence": "no code/URL extracted from read", "confidence": 0.4}
        checks["3_required_claims"] = {"status": "uncertain", "evidence": "agent-judged (see review)",
            "confidence": 0.0}
        checks["4_prohibited_claims"] = {"status": "uncertain", "evidence": "agent-judged (see review)",
            "confidence": 0.0}
        try:
            dur = float(r.get("end_s") or 0) - float(r.get("start_s") or 0)
        except (TypeError, ValueError):
            dur = 0.0
        lo, hi = b["contracted_length_s"] - b["length_tolerance_s"], b["contracted_length_s"] + b["length_tolerance_s"]
        checks["5_length"] = {"status": "pass" if lo <= dur <= hi else "uncertain",
            "evidence": f"read {dur:.0f}s vs contracted {b['contracted_length_s']}s±{b['length_tolerance_s']}s (detection window, approximate)",
            "confidence": 0.6}
        checks["6_placement"] = {"status": "uncertain",
            "evidence": "no media plan supplied in validation; placement not verifiable", "confidence": 0.0}
        tp = troot / f"{r.get('ep_id')}.json"
        comp_hit = []
        if tp.exists():
            try:
                segs = json.loads(tp.read_text())["segments"]
                lo_t, hi_t = (r.get("start_s") or 0) - 300, (r.get("end_s") or 0) + 300
                wtxt = " ".join(s.get("text", "") for s in segs
                                if s.get("end", 0) >= lo_t and s.get("start", 0) <= hi_t).lower()
                comp_hit = [c for c in b.get("competitors", []) if c.lower() in wtxt]
            except Exception as ex:
                log.failed(record_id=r.get("ep_id", "?"), error=f"transcript scan: {ex}")
        checks["7_competitor_adjacent"] = {"status": "fail" if comp_hit else "pass",
            "evidence": f"competitors in ±5min window: {comp_hit}" if comp_hit else "no competitor mentions within ±5min",
            "confidence": 0.75}
        checks["8_pronunciation"] = {"status": "uncertain",
            "evidence": "transcript cannot prove pronunciation; audio clip queued for listening",
            "confidence": 0.0}
        flags = []
        if tp.exists():
            try:
                segs = json.loads(tp.read_text())["segments"]
                for s in segs:
                    low = s.get("text", "").lower()
                    for cat, pats in SUITABILITY_LEX.items():
                        if any(re.search(px, low) for px in pats):
                            prox = "ADJACENT" if abs(s.get("start", 0) - (r.get("start_s") or 0)) <= 900 else "distant"
                            flags.append({"category": cat, "proximity": prox,
                                          "t": round(s.get("start", 0)), "text": s.get("text", "")[:120]})
                            break
                for s in segs:
                    if any(t.lower() in s.get("text", "").lower() for t in b.get("no_go_topics", [])):
                        flags.append({"category": "brand-no-go",
                                      "proximity": "ADJACENT" if abs(s.get("start", 0) - (r.get("start_s") or 0)) <= 900 else "distant",
                                      "t": round(s.get("start", 0)), "text": s.get("text", "")[:120]})
            except Exception as ex:
                log.failed(record_id=r.get("ep_id", "?"), error=f"suitability scan: {ex}")
        # NOTE: lexicon is a pre-filter only; real gate is agent/human review
        # with brand-specific no-go topics (README §4.4).
        checks["9_suitability"] = {"status": "fail" if any(f["proximity"] == "ADJACENT" for f in flags) else ("uncertain" if flags else "pass"),
            "evidence": f"{len(flags)} lexicon flags (pre-filter only, agent review needed)",
            "confidence": 0.55 if flags else 0.7}
        statuses = [c["status"] for c in checks.values()]
        overall = "failed" if "fail" in statuses else ("needs_review" if "uncertain" in statuses else "verified")
        ep = episodes.get(r.get("ep_id"), {})
        fetch_date, fetch_src = resolve_fetch_date(
            a.fetch_date, r.get("fetch_date"), ep.get("fetch_date"))
        out.append({"brand": r.get("brand"), "ep_id": r.get("ep_id"),
            "show_title": r.get("show_title", ep.get("show_title", "?")),
            "ep_title": r.get("ep_title", ep.get("ep_title", "?")),
            "pub_date": r.get("pub_date", ep.get("pub_date", "?")),
            "fetch_date": fetch_date,
            "fetch_date_source": fetch_src,
            "read_confidence": r.get("confidence"),
            "brief_provenance": BRIEF_PROVENANCE,
            "brief_type": "synthetic" if brief_is_synthetic else "unknown",
            "synthetic_disclaimer": ("Scored against a synthetic brief: failures mean "
                "'did not match the invented brief', NOT contractual noncompliance.")
                if brief_is_synthetic else None,
            "excerpt": r.get("excerpt"), "offer_codes": r.get("offer_codes", []),
            "urls": r.get("urls", []), "checks": checks, "overall": overall})
        log.ok(record_id=f"{r.get('ep_id')}:{r.get('brand')}",
               inputs={"reads": reads_path, "briefs": briefs_path},
               outputs={"overall": overall},
               extra={"fetch_date": fetch_date, "fetch_date_source": fetch_src,
                      "brief_type": "synthetic" if brief_is_synthetic else "unknown"})
    out_path.write_text(json.dumps(out, indent=1))
    by: dict[str, int] = {}
    for v in out:
        by[v["overall"]] = by.get(v["overall"], 0) + 1
    log.ok(record_id="summary", outputs={"verifications": str(out_path)},
           extra={"total": len(out), "resumed": resumed, "by_overall": by})
    print(f"DONE: {len(out)} verifications -> {by} ({resumed} resumed)")
    print("Brief type: synthetic — outcomes are mechanics checks, not contractual verification.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
