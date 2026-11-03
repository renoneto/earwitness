#!/usr/bin/env python3
"""ADRULES measurement (not shipped to the app runtime): earwitness.app.ad_rules
on labeled sets and on the demo sample. Read-only on every input; run it on
scratch copies.

Subcommands:

  checked-extra <workspace> <out.json>
      The checked list's extra keys as a loaded workspace's
      customer.brand_checker sees them: corrections.known_keys plus the
      committed advertiser list (display-brands.json is passed separately).

  labels --display D --extra E --scorer DIR --set NAME:EXPORT:TRANSCRIPTS:RAWLABELS:SCORINGLABELS ...
         [--config NAME=RULE:0|1,...] [--display-extra FILE] [--out out.json]
      For each set and config (BEFORE, BEFORE-published, each rule alone,
      DEFAULT = ad_rules.DEFAULT_SWITCHES, ALL, and any --config over the
      defaults): shown ads, not-ad rate (adtag/precision.md's rule on the final
      raw labels), sponsor reads located by a shown ad, visible naming
      precision and recall on sponsors (the 565 benchmark's score_labels
      protocol on the v2 scoring labels; DIR holds score_labels.py from
      round-4 head 28f27ebb), coverage (shown ads with a shown brand).
      BEFORE = the export's named reads as the demo imports them (no
      self-promo marks); BEFORE-published keeps the export's self-promo
      flags (reproduces the published 565 numbers). Pooled rows carry
      Wilson 95% intervals; each config gets the app-rule visible precision
      change against BEFORE by the rule that put the brand on.

  demo-rows --display D --extra E <corpus-root> <out-named-reads.json> [--switch R=0 ...]
      The demo sample's named-reads.json with the rules applied.

  demo-count <scratch-dir> [brand ...]
      Counts on a loaded scratch workspace <scratch-dir>/ws: Search ads and
      reads, "Brand not identified" reads and ads, and the given brands'
      Brands-list records.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earwitness.app import ad_rules as ar  # noqa: E402

NAMED_REL = "experiments/named-reads/output/candidates_normalized.json"
CONFIGS = ["BEFORE-published", "BEFORE", "R1", "R1b", "R2", "R3", "R4", "R5", "DEFAULT", "ALL"]
# --config NAME=SWITCH:0|1,... (over DEFAULT_SWITCHES); DETBUILD configs built in.
EXTRA_CONFIGS: dict = {
    "DET": {r: True for r in ar.DET_RULES},
    "R6+R7": {"R6": True, "R7": True},
    "R9": {"R9": True},
}
CONFIGS += list(EXTRA_CONFIGS)
_ROWS: dict = {}   # (set, config) -> {candidate_id: scored row}, for --diff-log
GATE_CONFIGS = ("DEFAULT", "DET", "R6+R7", "R9")


def wilson(k, n, z=1.96):
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 3), round(min(1.0, c + h), 3)]


def load_checked(display: str, extra: str) -> ar.CheckedList:
    return ar.CheckedList(json.loads(Path(display).read_text()), json.loads(Path(extra).read_text()))


def switches(config: str) -> dict:
    """BEFORE*: no rules; a rule name: that rule alone (sub-switches at their
    defaults); DEFAULT: ad_rules.DEFAULT_SWITCHES; ALL: every rule on (R5
    included; sub-switches at their defaults); else a --config."""
    if config == "DEFAULT":
        return dict(ar.DEFAULT_SWITCHES)
    if config == "ALL":
        return dict.fromkeys(ar.RULES, True)
    if config in EXTRA_CONFIGS:
        return dict(ar.DEFAULT_SWITCHES, **EXTRA_CONFIGS[config])
    return {r: r == config for r in ar.RULES}


def scoring_copy(rows: list) -> list:
    """Rows as scored: start_s, end_s are the display bounds (ad_rules.bounds);
    a row without display bounds is unchanged."""
    out = []
    for r in rows:
        lo, hi = ar.bounds(r)
        out.append(r if (lo, hi) == (r.get("start_s"), r.get("end_s"))
                   else dict(r, start_s=lo, end_s=hi))
    return out


# ---------------------------------------------------------------- shown brand

def shown_brand(row: dict, chk: ar.CheckedList):
    """The brand the app shows a customer (customer._customer_ad_rows):
    none for self-promo (not an ad) or a held read; else the first checked
    of brand_spoken, brand_url, brand_code (ad_rules.PRECEDENCE puts at most
    one there)."""
    if row.get("self_promo") or row.get("needs_review"):
        return None
    for key in ("brand_spoken", "brand_url", "brand_code"):
        if chk.checked(row.get(key)):
            return row[key]
    if not row.get("brand_spoken") and chk.checked(row.get("brand")):
        return row["brand"]
    return None


def bench_brand(row: dict, chk: ar.CheckedList):
    """The 565 benchmark's visible name (before the checked-brand rule): a
    shown checked brand, else the namer's pick; none when held."""
    if row.get("self_promo") or row.get("needs_review"):
        return None
    return shown_brand(row, chk) or row.get("brand_spoken") or row.get("brand")


# The round-4 analysis's spelling-tolerant variant (analyze4.py `tolerant`):
# a name equal to an overlapping labeled read's brand up to spacing,
# punctuation, a TLD, a parenthetical, or being a >= 5-letter prefix or
# suffix of it takes that label's spelling ("shane co" = "shane company").
_TLD_END = re.compile(r"(com|net|org|io|co|fm|ai|app|tv|me)$")


def _k(b):
    s = re.sub(r"[^a-z0-9]", "", re.sub(r"\(.*?\)", "", (b or "").lower()))
    return _TLD_END.sub("", s) if len(s) > 6 else s


def _same(c, lab):
    a, b = _k(c), _k(lab)
    return bool(a and b) and (a == b or (len(a) >= 5 and (b.startswith(a) or b.endswith(a))))


def tolerant(cands: list, labels: list) -> list:
    eps = {d["episode_id"]: d for d in labels}
    out = []
    for c in cands:
        hit = next((r["brand"] for r in eps[c["ep_id"]]["reads"]
                    if _inter(r["start_s"], r["end_s"], c["start_s"], c["end_s"]) > 0
                    and _same(c.get("brand_spoken"), r["brand"])), None)
        out.append(dict(c, brand_spoken=hit) if hit else c)
    return out


# ---------------------------------------------------------------- not-ad rule

def _inter(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def _unc(r):
    return str(r.get("confidence", "")).lower() == "uncertain" or r.get("ad_status") == "uncertain" \
        or r.get("uncertain") is True


def notad_class(c: dict, lab: dict) -> str:
    """adtag/scripts/precision-notad.py's classes, verbatim in logic."""
    a0, a1 = c["start_s"], c["end_s"]
    reads = [r for r in lab.get("reads", []) if r.get("start_s") is not None]
    sure = [r for r in reads if not _unc(r)]
    if sum(_inter(a0, a1, r["start_s"], r["end_s"]) for r in sure) > 0:
        return "ad"
    if any(_inter(a0, a1, r["start_s"], r["end_s"]) > 0 for r in reads if _unc(r)) or \
            any(u.get("start_s") is not None and _inter(a0, a1, u["start_s"], u["end_s"]) > 0
                for u in lab.get("unresolved", [])):
        return "uncertain"
    org = [o for o in lab.get("organic_mentions", []) if o.get("start_s") is not None
           and _inter(a0, a1, o["start_s"], o["end_s"]) > 0]
    if any("self-promo" in (o.get("brand") or "").lower() for o in org):
        return "self-promo"
    return "editorial/organic" if org else "nothing labeled"


# ---------------------------------------------------------------- breakdown

BRAND_RULES = ("R1b", "R2", "R3", "R4", "R5")


def attribution(c: dict, lab: dict) -> dict:
    """Which rule put the shown brand on a scored row ("namer" when none
    did), and what the labels hold under it (for the FP sample)."""
    by = [e["rule"] for e in (c.get("provenance") or {}).get("ad_rules") or []
          if e["rule"] in BRAND_RULES]
    a0, a1 = c["start_s"], c["end_s"]
    under = [f"read {r.get('brand')}" for r in lab.get("reads", [])
             if r.get("start_s") is not None and _inter(a0, a1, r["start_s"], r["end_s"]) > 0]
    under += [f"organic {o.get('brand')}" for o in lab.get("organic_mentions", [])
              if o.get("start_s") is not None and _inter(a0, a1, o["start_s"], o["end_s"]) > 0]
    return {"by": by[-1] if by else "namer", "brand": c["brand_spoken"], "ep": c["ep_id"],
            "span": [round(a0), round(a1)], "under": under or ["nothing labeled"]}


def breakdown(results: list, config: str, base: str = "BEFORE") -> dict:
    """App-rule visible precision, `config` against `base`, by the rule that
    put the brand on: TP and FP (strict and spelling-tolerant) added, plus
    TP and FP lost (rows `config` no longer scores as such), with every
    added FP for sampling."""
    out: dict = {}

    def slot(rule):
        return out.setdefault(rule, {"TP+": 0, "FP+": 0, "tolFP+": 0, "TP-": 0, "FP-": 0,
                                     "fps": []})
    for res in results:
        b, c = res["configs"][base], res["configs"][config]
        bo, co = b["app"]["outcomes"], c["app"]["outcomes"]
        bt, ct = b["app"]["tol_outcomes"], c["app"]["tol_outcomes"]
        for cid, o in co.items():
            a = c["attr"][cid]
            if o == "TP" and bo.get(cid) != "TP":
                slot(a["by"])["TP+"] += 1
            if o.startswith("FP") and not str(bo.get(cid, "")).startswith("FP"):
                slot(a["by"])["FP+"] += 1
                slot(a["by"])["fps"].append(dict(a, set=res["set"]))
            if ct.get(cid, "").startswith("FP") and not str(bt.get(cid, "")).startswith("FP"):
                slot(a["by"])["tolFP+"] += 1
        for cid, o in bo.items():
            if o == "TP" and co.get(cid) != "TP":
                slot("lost")["TP-"] += 1
            if o.startswith("FP") and not str(co.get(cid, "")).startswith("FP"):
                slot("lost")["FP-"] += 1
    return out


def breakdown_md(results: list, config: str, sample: int = 8) -> str:
    bd = breakdown(results, config)
    lines = [f"| Rule (who put the brand on) | TP added | FP added | FP added, tolerant | "
             f"TP lost | FP gone |", "|---|---|---|---|---|---|"]
    for rule in sorted(bd):
        d = bd[rule]
        lines.append(f"| {rule} | {d['TP+']} | {d['FP+']} | {d['tolFP+']} | {d['TP-']} | "
                     f"{d['FP-']} |")
    for rule in sorted(bd):
        fps = bd[rule]["fps"]
        if not fps:
            continue
        lines.append(f"\n{rule}, sample of {min(sample, len(fps))} of {len(fps)} added FPs:\n")
        step = max(1, len(fps) // sample)
        for a in fps[::step][:sample]:
            lines.append(f"- {a['set']} {a['ep']} {a['span'][0]}-{a['span'][1]} s: shows "
                         f"\"{a['brand']}\"; labels: {'; '.join(a['under'])}")
    return "\n".join(lines)


# ---------------------------------------------------------------- one set

def _labels(d: str) -> list:
    return [json.loads(p.read_text()) for p in sorted(Path(d).glob("*.json"))]


def measure_set(name, export, transcripts_dir, raw_dir, scoring_dir, chk, sl) -> dict:
    raw = {d["episode_id"]: d for d in _labels(raw_dir)}
    scoring = _labels(scoring_dir)
    eps = {d["episode_id"] for d in scoring}
    rows_pub = [r for r in json.loads((Path(export) / NAMED_REL).read_text()) if r["ep_id"] in eps]
    rows_demo = [{k: v for k, v in r.items() if k not in ("self_promo", "self_promo_reason")}
                 for r in rows_pub]
    episodes = {e["ep_id"]: e for e in json.loads((Path(export) / "data/episodes.json").read_text())}
    trs = {}
    for ep in sorted(eps):
        p = Path(transcripts_dir) / f"{ep}.json"
        if p.exists():
            doc = json.loads(p.read_text())
            meta = episodes.get(ep) or {}
            trs[ep] = dict(doc, ep_id=ep, show_title=doc.get("show_title") or meta.get("show_title"),
                           ep_title=doc.get("ep_title") or meta.get("ep_title"),
                           pub_date=doc.get("pub_date") or meta.get("pub_date"),
                           fetch_date=doc.get("fetch_date") or meta.get("fetch_date"),
                           fetch_date_source=doc.get("fetch_date_source")
                           or ({"from": "episode row"} if meta.get("fetch_date") else None))
    sponsors = sl.labels_of_kind(scoring, "sponsors")
    scoring_by_ep = {d["episode_id"]: d for d in scoring}
    sponsor_reads = [(d["episode_id"], r) for d in sponsors for r in d["reads"]
                     if r.get("read_type") != "uncertain"]
    out = {"set": name, "episodes": len(eps), "transcripts": len(trs), "configs": {}}
    for config in CONFIGS:
        if config == "BEFORE-published":
            rows = rows_pub
        elif config == "BEFORE":
            rows = rows_demo
        else:
            # the importer does not load a row R10 marks as the same ad
            rows = [r for r in scoring_copy(ar.apply(rows_demo, trs, chk,
                                                     switches=switches(config)))
                    if not r.get("same_ad_as")]
        shown = [r for r in rows if not r.get("self_promo")]
        _ROWS[(name, config)] = {r["candidate_id"]: r for r in rows if r.get("candidate_id")}
        classes: dict = {}
        for c in shown:
            k = notad_class(c, raw[c["ep_id"]])
            classes[k] = classes.get(k, 0) + 1
        not_ad = len(shown) - classes.get("ad", 0) - classes.get("uncertain", 0)
        located = sum(1 for ep, r in sponsor_reads if any(
            c["ep_id"] == ep and _inter(c["start_s"], c["end_s"], r["start_s"], r["end_s"]) > 0
            for c in shown))
        res = {"rows": len(rows), "shown": len(shown), "new_rows": sum(
            1 for r in rows if r.get("source") == ar.R1B_SOURCE), "classes": classes,
            "not_ad": not_ad, "located": [located, len(sponsor_reads)],
            "rules": {}}
        for r in rows:
            for e in (r.get("provenance") or {}).get("ad_rules") or []:
                res["rules"][e["rule"]] = res["rules"].get(e["rule"], 0) + 1
        res["shown_brands"] = {r["candidate_id"]: shown_brand(r, chk) for r in shown
                               if shown_brand(r, chk)}
        parts = [r for r in shown if r.get("source") == ar.R6_SOURCE]
        entries = [(r, e) for r in rows for e in (r.get("provenance") or {}).get("ad_rules") or []]
        res["det"] = {
            "r6_parts": len([r for r in rows if r.get("source") == ar.R6_SOURCE]),
            "r6_parts_named": sum(1 for r in parts if shown_brand(r, chk)),
            "r7_trims": sum(1 for r, e in entries if e["rule"] == "R7"),
            "r9_names": sum(1 for r, e in entries if e["rule"] == "R9"
                            and r.get("source") != ar.R9_SOURCE),
            "r9_new_rows": sum(1 for r in rows if r.get("source") == ar.R9_SOURCE),
            "unnamed_rows": sum(1 for r in shown if r.get("source") != ar.R6_SOURCE
                                and not shown_brand(r, chk)),
            "rows_rest": sum(1 for r in shown if r.get("source") != ar.R6_SOURCE),
            "unnamed_parts": sum(1 for r in parts if not shown_brand(r, chk)),
            "parts_shown": len(parts)}
        for metric, fn in (("app", shown_brand), ("bench", bench_brand)):
            cands = []
            for r in shown:
                b = fn(r, chk)
                if b:
                    cands.append(dict(r, brand_spoken=b, brand=b))
            kinds = sl.candidate_kinds(scoring, cands, [])
            mine = [c for c in cands if kinds[c["candidate_id"]] == "sponsors"]
            sc = sl.score(sponsors, mine, [])
            tol = sl.score(sponsors, tolerant(mine, scoring), [])
            res[metric] = {"named": len(cands), "TP": sc["TP"], "FP": sc["FP"],
                           "reads": sc["labeled_denominator_reads"], "tolTP": tol["TP"],
                           "tolFP": tol["FP"],
                           "outcomes": {d["candidate"]: d["outcome"] for d in sc["detail"]
                                        if "candidate" in d},
                           "tol_outcomes": {d["candidate"]: d["outcome"] for d in tol["detail"]
                                            if "candidate" in d}}
            if metric == "app":
                res["attr"] = {c["candidate_id"]: attribution(c, scoring_by_ep[c["ep_id"]])
                               for c in mine}
        res["coverage"] = [res["app"]["named"], len(shown)]
        out["configs"][config] = res
    dflt = out["configs"]["DEFAULT"]["shown_brands"]
    for res in out["configs"].values():
        sb = res["shown_brands"]
        res["named_lost"] = sum(1 for cid, b in dflt.items() if sb.get(cid) != b)
    return out


def pool(results: list, config: str) -> dict:
    tot = {"shown": 0, "not_ad": 0, "located": [0, 0], "new_rows": 0, "named_lost": 0,
           "det": dict.fromkeys(("r6_parts", "r6_parts_named", "r7_trims", "r9_names",
                                 "r9_new_rows", "unnamed_rows", "rows_rest", "unnamed_parts",
                                 "parts_shown"), 0),
           "app": {"named": 0, "TP": 0, "FP": 0, "reads": 0, "tolTP": 0, "tolFP": 0},
           "bench": {"named": 0, "TP": 0, "FP": 0, "reads": 0, "tolTP": 0, "tolFP": 0},
           "coverage": [0, 0]}
    for res in results:
        c = res["configs"][config]
        for k in ("shown", "not_ad", "new_rows", "named_lost"):
            tot[k] += c[k]
        for k in tot["det"]:
            tot["det"][k] += c["det"][k]
        for k in ("located", "coverage"):
            tot[k] = [tot[k][0] + c[k][0], tot[k][1] + c[k][1]]
        for m in ("app", "bench"):
            for k in tot[m]:
                tot[m][k] += c[m][k]
    return tot


def _f(k, n):
    return f"{k / n:.3f} ({k}/{n})" if n else "-"


def row_md(label: str, c: dict, ci: bool = False) -> str:
    na = _f(c["not_ad"], c["shown"])
    if ci and c["shown"]:
        lo, hi = wilson(c["not_ad"], c["shown"])
        na += f" [{lo:.3f}-{hi:.3f}]"
    app, bench = c["app"], c["bench"]

    def p(k, n):
        out = _f(k, n)
        if ci and n:
            lo, hi = wilson(k, n)
            out += f" [{lo:.3f}-{hi:.3f}]"
        return out
    return (f"| {label} | {c['shown']} | {c['new_rows']} | {na} | {_f(*c['located'])} | "
            f"{p(app['TP'], app['TP'] + app['FP'])} | "
            f"{p(app['tolTP'], app['tolTP'] + app['tolFP'])} | {_f(app['TP'], app['reads'])} | "
            f"{p(bench['TP'], bench['TP'] + bench['FP'])} | {_f(*c['coverage'])} |")


HEADER = ("| Config | Shown ads | New (R1b) | Not-ad rate | Sponsor reads located | "
          "Visible P, app (checked brands) | same, spelling-tolerant | Named R, app | "
          "Visible P, benchmark rule | Coverage (shown ads with a brand) |\n"
          "|---|---|---|---|---|---|---|---|---|---|")


GATE_HEADER = ("| Config | App visible P | spelling-tolerant P | Benchmark rule P | Located recall | "
               "Coverage | Named reads | Named lost | R6 parts (named) | R7 trims | R9 names | "
               "R9 new rows | Unnamed share, rows excl. parts | Unnamed share, parts |\n"
               "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")


def gate_row(label: str, c: dict) -> str:
    def p(k, n):
        out = _f(k, n)
        if n:
            lo, hi = wilson(k, n)
            out += f" [{lo:.3f}-{hi:.3f}]"
        return out
    app, bench, d = c["app"], c["bench"], c["det"]
    return (f"| {label} | {p(app['TP'], app['TP'] + app['FP'])} | "
            f"{p(app['tolTP'], app['tolTP'] + app['tolFP'])} | "
            f"{p(bench['TP'], bench['TP'] + bench['FP'])} | {p(*c['located'])} | "
            f"{p(*c['coverage'])} | {app['named']} | {c['named_lost']} | "
            f"{d['r6_parts']} ({d['r6_parts_named']}) | {d['r7_trims']} | {d['r9_names']} | "
            f"{d['r9_new_rows']} | {_f(d['unnamed_rows'], d['rows_rest'])} | "
            f"{_f(d['unnamed_parts'], d['parts_shown'])} |")


def diff_log(results: list, base: str, config: str) -> str:
    """Every row whose TP/FP status (app rule, strict and tolerant) differs
    between `base` and `config`, with its rule entries, brand, bounds, labels."""
    out = [f"# TP/FP status changes, {base} -> {config}", ""]
    n = 0
    for res in results:
        b, c = res["configs"][base], res["configs"][config]
        for kind, key in (("strict", "outcomes"), ("tolerant", "tol_outcomes")):
            bo, co = b["app"][key], c["app"][key]
            for cid in sorted(set(bo) | set(co)):
                if bo.get(cid) == co.get(cid):
                    continue
                n += 1
                row = _ROWS[(res["set"], config)].get(cid) or _ROWS[(res["set"], base)].get(cid) or {}
                rules = [(e["rule"], e.get("route")) for e in (row.get("provenance") or {}).get("ad_rules") or []]
                att = c["attr"].get(cid) or b["attr"].get(cid) or {}
                out.append(f"- [{kind}] {res['set']} {row.get('ep_id')} {cid}: {base} {bo.get(cid)} -> "
                           f"{config} {co.get(cid)}; rules {rules}; source {row.get('source')}; "
                           f"brand {att.get('brand')!r}; start/end {row.get('start_s')}-{row.get('end_s')} "
                           f"(display {row.get('display_start_s')}-{row.get('display_end_s')}); "
                           f"labels: {'; '.join(att.get('under') or [])}")
    out.append(f"\n{n} status changes")
    return "\n".join(out)


def merged_display(path: str, extra_path) -> dict:
    doc = json.loads(Path(path).read_text())
    if not extra_path:
        return doc
    more = json.loads(Path(extra_path).read_text())
    out = dict(doc)
    for part in ("brands", "aliases"):
        out[part] = {**(more.get(part) or {}), **(doc.get(part) or {})}
    return out


def cmd_labels(a) -> None:
    for spec in a.config or ():
        name, sw = spec.split("=", 1)
        EXTRA_CONFIGS[name] = {k: v != "0" for k, v in (x.split(":") for x in sw.split(","))}
        if name not in CONFIGS:
            CONFIGS.append(name)
    sys.path.insert(0, a.scorer)
    import score_labels as sl  # noqa: E402  (round-4 head 28f27ebb copy)
    chk = ar.CheckedList(merged_display(a.display, a.display_extra), json.loads(Path(a.extra).read_text()))
    results = []
    for spec in a.set:
        name, export, trs, raw, scoring = spec.split(":")
        results.append(measure_set(name, export, trs, raw, scoring, chk, sl))
    lines = []
    for res in results:
        lines += [f"\n### {res['set']} ({res['episodes']} episodes)\n", HEADER]
        lines += [row_md(cfg, res["configs"][cfg]) for cfg in CONFIGS]
    pooled = "Pooled " + ", ".join(r["set"] for r in results)
    groups = [(pooled, results)]
    b234 = [r for r in results if r["set"] in ("blind2", "blind3", "blind4")]
    if len(b234) == 3:
        groups.append(("blind2-4 (the 565 benchmark pool)", b234))
    for title, group in groups:
        lines += [f"\n### {title}\n", HEADER]
        lines += [row_md(cfg, pool(group, cfg), ci=True) for cfg in CONFIGS]
    sets = [(r["set"], r) for r in results]
    lines += ["\n## DETBUILD gate tables (display-bound scoring)"]
    for title, group in groups:
        lines += [f"\n### Gate: {title}\n", GATE_HEADER]
        lines += [gate_row(cfg, pool(group, cfg)) for cfg in GATE_CONFIGS]
    for name, res in sets:
        lines += [f"\n### Gate: {name}\n", GATE_HEADER]
        lines += [gate_row(cfg, res["configs"][cfg]) for cfg in GATE_CONFIGS]
    for cfg in CONFIGS:
        if cfg not in ("BEFORE-published", "BEFORE"):
            lines += [f"\n### App-rule visible precision change by rule, {cfg} vs BEFORE "
                      f"({pooled})\n", breakdown_md(results, cfg)]
    text = "\n".join(lines)
    print(text)
    if a.diff_log:
        Path(a.diff_log).write_text(diff_log(results, "DEFAULT", "DET") + "\n")
    if a.out:
        Path(a.out).write_text(json.dumps({"sets": results, "markdown": text}, indent=1))


# ---------------------------------------------------------------- demo

def cmd_demo_rows(a) -> None:
    chk = load_checked(a.display, a.extra)
    root = Path(a.root)
    rows = json.loads((root / "named-reads.json").read_text())
    trs = {}
    for p in sorted((root / "transcripts").glob("*.json")):
        doc = json.loads(p.read_text())
        trs[doc.get("ep_id") or p.stem] = doc
    sw = {}
    for s in a.switch or ():
        k, v = s.split("=")
        sw[k] = v not in ("0", "off", "false")
    out = [r for r in ar.apply(rows, trs, chk, switches=sw)
           if not r.get("same_ad_as")]   # the importer does not load a marked row
    Path(a.out).write_text(json.dumps(out))
    counts: dict = {}
    for r in out:
        for e in (r.get("provenance") or {}).get("ad_rules") or []:
            counts[e["rule"]] = counts.get(e["rule"], 0) + 1
    print(json.dumps({"rows_in": len(rows), "rows_out": len(out), "rule_entries": counts,
                      "self_promo": sum(1 for r in out if r.get("self_promo")),
                      "held": sum(1 for r in out if r.get("needs_review")),
                      "brand_url": sum(1 for r in out if r.get("brand_url")),
                      "brand_code": sum(1 for r in out if r.get("brand_code")),
                      "new_r1b": sum(1 for r in out if r.get("source") == ar.R1B_SOURCE),
                      "switches": sw}))


def cmd_checked_extra(a) -> None:
    from earwitness.app import corrections, db
    con = db.connect(Path(a.workspace))
    with db.transaction(con, write=False):
        keys = set(corrections.known_keys(con))
    adv = json.loads((Path(ar.__file__).resolve().parents[1] / "data/advertisers-v2.json").read_text())
    for e in adv.get("entries") or []:
        for k in (e.get("key"), e.get("name"), *(e.get("aliases") or [])):
            if isinstance(k, str) and k:
                keys.add(k)
    Path(a.out).write_text(json.dumps(sorted(keys)))
    print(len(keys))


def cmd_demo_count(a) -> None:
    from earwitness.app import ad_breaks, customer, customer_query, db
    con = db.connect(Path(a.scratch) / "ws")
    q = {"version": 2, "mode": "ads", "q": "", "brands": None, "shows": None, "categories": None,
         "statuses": None, "audio": None, "date_status": "all", "from": None, "to": None,
         "sort": "newest", "page": 1, "ids": None}
    with db.transaction(con, write=False), customer.model_names(False):  # PODS: model rows are display-only
        rows = customer._customer_ad_rows(con)
        bni = {r["id"] for r in rows if not r["_brand_keys"]}
        ads = {}
        for _eid, ad in ad_breaks.scope_index(con).items():
            ads.setdefault(ad["id"], ad)
        bni_ads = sum(1 for ad in ads.values() if all(rd["key"] in bni for rd in ad["reads"]))
        sr = customer_query.research_bundle(con, q, None, [], False, "")["result"]
        directory = {b["key"]: (b["display"], b["records"]) for b in customer.brands_directory(con)}
    out = {"search_ads": sr["break_total"], "search_reads": sr["total"], "reads": len(rows),
           "bni_reads": len(bni), "bni_read_share": round(len(bni) / len(rows), 4),
           "ads_in_index": len(ads), "bni_ads": bni_ads,
           "bni_ad_share": round(bni_ads / len(ads), 4) if ads else None,
           "r1b_reads": sum(1 for r in rows if ".adrules-r1b." in (r["source_record_key"] or "")),
           "brands_list": len(directory),
           "brands": {k: directory.get(k) for k in a.brands}}
    print(json.dumps(out, indent=1))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    x = sub.add_parser("checked-extra")
    x.add_argument("workspace")
    x.add_argument("out")
    x = sub.add_parser("labels")
    x.add_argument("--display", required=True)
    x.add_argument("--extra", required=True)
    x.add_argument("--scorer", required=True)
    x.add_argument("--set", action="append", required=True)
    x.add_argument("--config", action="append", help="NAME=SWITCH:0|1,... over the defaults")
    x.add_argument("--diff-log", help="write the DEFAULT -> DET TP/FP status changes here")
    x.add_argument("--display-extra", help="a second display list merged under --display (base wins)")
    x.add_argument("--out")
    x = sub.add_parser("demo-rows")
    x.add_argument("--display", required=True)
    x.add_argument("--extra", required=True)
    x.add_argument("--switch", action="append")
    x.add_argument("root")
    x.add_argument("out")
    x = sub.add_parser("demo-count")
    x.add_argument("scratch")
    x.add_argument("brands", nargs="*")
    a = p.parse_args(argv)
    {"labels": cmd_labels, "demo-rows": cmd_demo_rows, "checked-extra": cmd_checked_extra,
     "demo-count": cmd_demo_count}[a.cmd](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
