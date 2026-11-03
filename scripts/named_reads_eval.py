#!/usr/bin/env python3
"""End-to-end evaluation of named Jev reads (Jev windows -> brand namer ->
candidates), scored with score_labels.py, plus dev-only threshold tuning.

  run   Real Jev outputs + transcripts + labels -> named-read candidates per
        configuration (--context-windows x --thresholds x --sub-window-s,
        0 = whole-window naming), scored with
        score_labels.score (protocol + strict), location recall (labeled
        reads covered > 50% by any candidate, any brand), for all spans and
        for confident spans only (needs_review false), and named reads per
        episode (the precision risk of splitting). Writes
        <out-dir>/<config>/candidates_normalized.json and report.json.
  tune  The bake-off's threshold protocol on the DEV set only: each labeled
        read named on its tight window (read +/- 10 s) and on its 60-s grid
        cell, plus N random non-ad 60-s windows; per source (domain/CTA,
        GLiNER) the raw-score threshold maximizing mean top-1 lenient
        (tight, 60 s) minus half the non-ad naming rate.
  canon Corpus-wide brand canonicalization (brand_canon.py), measured: a
        `run` config's candidates scored before/after canonicalization with
        a canon map built from a corpus export's named candidates and
        transcripts (--canon-export) with each evaluated episode's own
        export spans and transcript left out (its evaluated candidates stand
        in, so its raw brands are map keys), plus the export's own distinct
        confident brands before/after, its merges (with a seeded random
        sample for hand review) and the similar pairs the support rule
        rejected. Writes <out-dir>/canon-report.json and brand-canon.json.

  show-identity  Fetch (once per show, cached in --show-identity) the RSS
        feed of every labeled episode's show for the self-promo filter.

`run --self-promo` marks named reads with the self-promo filter
(earwitness/pipeline/self_promo.py) from cached show identities (feed URLs
from --feeds), with brand recurrence counted over the evaluated episodes
plus the --self-promo-export corpus's named reads (other episodes of the
same show); it scores the filtered reads as `all` / `confident`, the
unfiltered ones as `unfiltered` / `unfiltered_confident`, and reports what
the filter removed: removed_tp (correct ad names lost), removed_on_ad
(removed reads covering > 50% of a labeled ad read = real ads wrongly
called self-promo) and removed_organic_fp. Self-promo reads are out of the
ad metrics (`all` / `confident`).

The repair brand list is harvested from --brand-corpus transcripts with the
evaluated episode left out (leave-one-episode-out), so an episode's own
transcript never feeds its own repair list. Never tune on the holdout.

Presets (--split): holdout = the 12-episode blind set (2026-10-08 Jev
outputs, final transcripts); dev = the 6 dev labels with the 2026-09-21
Jev triage run.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from earwitness.gazetteer import normalize_brand  # noqa: E402
from earwitness.pipeline import brand_canon as bc  # noqa: E402
from earwitness.pipeline import brand_namer as bn  # noqa: E402
from earwitness.pipeline import self_promo as sp  # noqa: E402

import score_labels  # noqa: E402

VAL = "/Users/renoneto/projects/earwitness-validation"
CS = f"{VAL}/.corpus-scale-20261008"
PRESETS = {
    "holdout": {"jev_dir": f"{CS}/jev-holdout", "jev_results": None,
                "transcripts": "/tmp/ew-final-all/tr",
                "labels": [f"{CS}/labels-blind", f"{CS}/labels-blind-pilot"],
                "brand_corpus": [f"{CS}/export/transcripts"],
                "feeds": [f"{VAL}/data/episodes.json", f"{CS}/export/data/episodes.json",
                          *sorted(glob.glob(f"{VAL}/.pilot-corpus/*/sidecar.json"))],
                "self_promo_export": f"{CS}/export"},
    "dev": {"jev_dir": None, "jev_results": f"{VAL}/experiments/jev-ad-triage-20260921/results.json",
            "transcripts": f"{VAL}/transcripts", "labels": [f"{VAL}/labels"],
            "brand_corpus": [f"{CS}/export/transcripts"],
            "feeds": [f"{VAL}/data/episodes.json"], "self_promo_export": f"{CS}/export"},
}


# ---------------------------------------------------------------- inputs

def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _dump(obj, path) -> None:
    Path(path).write_text(json.dumps(obj, indent=1), encoding="utf-8")


def load_jev(jev_dir=None, jev_results=None) -> dict:
    """{episode_id: Jev doc} from a dir of per-episode results or one
    triage-run results.json ({"episodes": [...]})."""
    out = {}
    if jev_dir:
        for f in sorted(glob.glob(os.path.join(jev_dir, "*.json"))):
            doc = _load(f)
            if bn.jev_windows(doc) is not None:
                out[doc.get("episode_id") or Path(f).stem] = doc
    if jev_results:
        for ep in _load(jev_results).get("episodes") or []:
            if bn.jev_windows(ep) is not None:
                out[ep.get("episode_id") or ep.get("ep_id")] = ep
    return out


def brand_corpus_paths(dirs, exclude: str | None = None) -> list:
    return sorted(p for d in dirs for p in glob.glob(os.path.join(d, "*.json"))
                  if Path(p).stem != exclude)


class Namers:
    """One BrandNamer per leave-one-out brand list (GLiNER loaded once)."""

    def __init__(self, brand_dirs, gliner):
        self.dirs, self.gliner = brand_dirs, None if gliner == "none" else gliner
        self.corpus_eps = {Path(p).stem for p in brand_corpus_paths(brand_dirs)}
        self._cache = {}

    def for_episode(self, ep: str) -> bn.BrandNamer:
        key = ep if ep in self.corpus_eps else None
        if key not in self._cache:
            blist = bn.harvest_brand_list(brand_corpus_paths(self.dirs, exclude=key))
            self._cache[key] = bn.BrandNamer(blist, self.gliner, threads=4)
        return self._cache[key]


def segments(transcripts: str, ep: str) -> list:
    return _load(os.path.join(transcripts, f"{ep}.json"))["segments"]


# ---------------------------------------------------------------- self-promo

def load_feed_map(paths) -> dict:
    """{episode_id: feed_url} from episodes.json lists (ep_id / episode_id)
    and pilot sidecar.json files (episode id "pilot-<dir>"); first wins."""
    out: dict = {}
    for path in paths or ():
        if not Path(path).is_file():
            continue
        doc = _load(path)
        rows = doc if isinstance(doc, list) else [
            dict(doc, ep_id=f"pilot-{Path(path).parent.name}")]
        for e in rows:
            ep = e.get("ep_id") or e.get("episode_id")
            if ep and e.get("feed_url"):
                out.setdefault(ep, e["feed_url"])
    return out


def export_named(export_dir) -> tuple:
    """(named candidates, {ep: feed_url}) of a corpus export; empty when absent."""
    root = Path(export_dir) if export_dir else None
    if not root or not (root / NAMED_REL).is_file():
        return [], {}
    return _load(root / NAMED_REL), load_feed_map([root / "data" / "episodes.json"])


class SelfPromo:
    def __init__(self, a):
        self.feeds = load_feed_map(a.feeds)
        self.cache = a.show_identity
        self.export, self.export_feeds = export_named(a.self_promo_export)

    def mark(self, cands, docs, transcripts, known_brands=None) -> list:
        evaluated = set(docs)
        feed = self.feeds.get
        items = [(feed(ep), ep, None) for ep in docs]
        items += [(feed(c["ep_id"]), c["ep_id"], sp.candidate_brand(c)) for c in cands]
        items += [(self.export_feeds.get(c["ep_id"]), c["ep_id"], sp.candidate_brand(c))
                  for c in self.export if c["ep_id"] not in evaluated]
        rec = sp.show_recurrence([i for i in items if i[0]])
        segs = {ep: segments(transcripts, ep) for ep in docs}
        return sp.mark(cands, texts=[sp.span_text(segs[c["ep_id"]], c["start_s"], c["end_s"])
                                     for c in cands],
                       identities=[sp.load_identity(self.cache, feed(c["ep_id"])) for c in cands],
                       recurrence=rec, feeds=[feed(c["ep_id"]) for c in cands],
                       known_brands=known_brands)


def removal_report(labels, marked, unfiltered: dict, troots) -> dict:
    """What the filter removed, judged by the unfiltered scoring."""
    outcome = {d["candidate"]: d["outcome"] for d in unfiltered["detail"] if d.get("candidate")}
    reads = {}
    for lab in labels:
        segs = score_labels.load_segments(lab["episode_id"], troots)
        reads[lab["episode_id"]] = [sp_ for sp_ in (score_labels.resolve_span(r, segs)
                                                    for r in lab.get("reads", [])
                                                    if r.get("read_type") != "uncertain") if sp_]
    removed = []
    for c in marked:
        if not c.get("self_promo"):
            continue
        span = (float(c["start_s"]), float(c["end_s"]))
        removed.append({"candidate": c["candidate_id"], "brand": c["brand"],
                        "reason": c["self_promo_reason"],
                        "outcome": outcome.get(c["candidate_id"]),
                        "on_ad": any(score_labels.protocol_ok(r, span)
                                     for r in reads.get(c["ep_id"], []))})
    return {"removed": len(removed),
            "removed_tp": sum(r["outcome"] == "TP" for r in removed),
            "removed_on_ad": sum(r["on_ad"] for r in removed),
            "removed_organic_fp": sum(r["outcome"] == "FP-organic" for r in removed),
            "removed_fp": sum(r["outcome"] == "FP" for r in removed),
            "reasons": [r["reason"] for r in removed], "detail": removed}


def cmd_show_identity(a) -> int:
    from earwitness.pipeline import corpus as cr
    feeds = load_feed_map(a.feeds)
    eps = [lab["episode_id"] for lab in score_labels.load_labels(a.labels_dir)]
    missing = sorted(ep for ep in eps if ep not in feeds)
    rep = sp.build_identities([feeds[ep] for ep in eps if ep in feeds], a.show_identity,
                              cr.http_get_capped, max_fetches=a.max_fetches, refresh=a.refresh)
    print(json.dumps({"episodes": len(eps), "episodes_without_feed": missing, **rep}))
    return 0


# ---------------------------------------------------------------- run

def location_recall(labels: list, cands: list, troots: list) -> dict:
    by_ep: dict = {}
    for c in cands:
        by_ep.setdefault(c["ep_id"], []).append((float(c["start_s"]), float(c["end_s"])))
    n = hit = 0
    for lab in labels:
        segs = score_labels.load_segments(lab["episode_id"], troots)
        for r in lab.get("reads", []):
            if r.get("read_type") == "uncertain":
                continue
            span = score_labels.resolve_span(r, segs)
            if span is None:
                continue
            n += 1
            hit += any(score_labels.protocol_ok(span, c) for c in by_ep.get(lab["episode_id"], []))
    return {"reads": n, "located": hit, "location_recall": round(hit / n, 3) if n else 0.0}


def summarize(labels, cands, troots, episodes: int | None = None) -> dict:
    rep = score_labels.score(labels, cands, troots)
    loc = location_recall(labels, cands, troots)
    keep = ("TP", "FP", "FN", "precision", "recall", "organic_fp")
    episodes = episodes or len(labels)
    return {"candidates": len(cands),
            "reads_per_episode": round(len(cands) / episodes, 2) if episodes else 0.0,
            "protocol": {k: rep[k] for k in keep},
            "strict": {k: rep["strict"][k] for k in keep},
            "location_recall": loc["location_recall"], "located": loc["located"],
            "reads": loc["reads"],
            "named_given_located": round(rep["TP"] / loc["located"], 3) if loc["located"] else 0.0,
            "detail": rep["detail"]}


def cmd_run(a) -> int:
    labels = score_labels.load_labels(a.labels_dir)
    jev = load_jev(a.jev_dir, a.jev_results)
    namers = Namers(a.brand_corpus, a.gliner)
    troots = [a.transcripts]
    promo = SelfPromo(a) if a.self_promo else None
    missing = sorted(lab["episode_id"] for lab in labels if lab["episode_id"] not in jev)
    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {"split": a.split_name, "episodes": len(labels), "episodes_without_jev": missing,
              "gliner": a.gliner, "thresholds_used": dict(bn.DEV_THRESHOLDS),
              "note": "namer thresholds were tuned on DEV only; holdout is reported once "
                      "per configuration", "configs": []}
    configs = [(ctx, thr, sw or None) for ctx in a.context_windows for thr in a.thresholds
               for sw in a.sub_window_s]
    for ctx, thr, sw in configs:
        name = f"ctx{ctx}_thr{thr:g}" + (f"_sub{sw:g}" if sw else "")
        cands, docs = [], {}
        for lab in labels:
            ep = lab["episode_id"]
            if ep not in jev:
                continue
            segs = segments(a.transcripts, ep)
            namer = namers.for_episode(ep)
            doc = bn.name_episode(ep, segs, jev[ep], namer, threshold=thr,
                                  context_windows=ctx, sub_window_s=sw)
            docs[ep] = doc
            cands.extend(bn.named_candidates(ep, doc, segs))
        entry_extra = {}
        if promo:
            known = sp.known_brand_keys(
                [{"brand": b} for ep in docs for b in namers.for_episode(ep).entries.values()])
            marked = promo.mark(cands, docs, a.transcripts, known)
            unf = summarize(labels, cands, troots, len(docs))
            entry_extra = {
                "unfiltered": unf,
                "unfiltered_confident": summarize(
                    labels, [c for c in cands if not c["needs_review"]], troots, len(docs)),
                "self_promo": removal_report(labels, marked, unf, troots)}
            cands = [c for c in marked if not c["self_promo"]]
            _dump(marked, out_dir / f"{name}_self_promo_marked.json")
        conf = [c for c in cands if not c["needs_review"]]
        cfg_dir = out_dir / name
        cfg_dir.mkdir(exist_ok=True)
        _dump(cands, cfg_dir / "candidates_normalized.json")
        _dump(docs, cfg_dir / "names.json")
        entry = {"config": name, "context_windows": ctx, "jev_threshold": thr,
                 "sub_window_s": sw,
                 "pipelines": sorted({d["pipeline"] for d in docs.values()}),
                 "all": summarize(labels, cands, troots, len(docs)),
                 "confident": summarize(labels, conf, troots, len(docs)), **entry_extra}
        report["configs"].append(entry)
    if a.baseline_candidates:
        report["baseline"] = summarize(labels, _load(a.baseline_candidates), troots)
    _dump(report, out_dir / "report.json")
    print(format_table(report))
    return 0


def _row(name, m) -> str:
    p, s = m["protocol"], m["strict"]
    return (f"| {name} | {m['candidates']} | {p['precision']:.2f} / {p['recall']:.2f} "
            f"| {s['precision']:.2f} / {s['recall']:.2f} | {m['location_recall']:.2f} "
            f"| {m['named_given_located']:.2f} | {m['reads_per_episode']:.2f} |")


def format_table(report: dict) -> str:
    lines = [f"split: {report['split']}  episodes: {report['episodes']}  gliner: "
             f"{report['gliner']}  namer thresholds (dev): {report['thresholds_used']}",
             "| config | cands | protocol P / R | strict P / R | location recall "
             "| named / located | reads / episode |", "|---|---|---|---|---|---|---|"]
    if "baseline" in report:
        lines.append(_row("rules baseline", report["baseline"]))
    for c in report["configs"]:
        if "unfiltered" in c:
            lines.append(_row(f"{c['config']} unfiltered", c["unfiltered"]))
            lines.append(_row(f"{c['config']} unfiltered confident", c["unfiltered_confident"]))
        lines.append(_row(f"{c['config']} all", c["all"]))
        lines.append(_row(f"{c['config']} confident", c["confident"]))
        if "self_promo" in c:
            s = c["self_promo"]
            lines.append(f"self-promo removed {s['removed']}: organic FP {s['removed_organic_fp']},"
                         f" other FP {s['removed_fp']}, TP {s['removed_tp']}, on a labeled ad "
                         f"{s['removed_on_ad']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- tune (dev only)

def _compact(s):
    return bn.compact(s or "")


def lenient_ok(pred: str, label: str) -> bool:
    """The bake-off's lenient match: normalized equality, compact equality,
    containment of >= 4 characters, or Jaro-Winkler >= 0.9."""
    if normalize_brand(pred) == normalize_brand(label):
        return True
    a, b = _compact(pred), _compact(label)
    if not a or not b:
        return False
    if a == b or (len(min(a, b, key=len)) >= 4 and (a in b or b in a)):
        return True
    return bn.jaro_winkler(a, b) >= 0.9


def _window(segs, lo, hi) -> str:
    return " ".join(s["text"].strip() for s in segs if s["end"] > lo and s["start"] < hi)


def tune_items(labels, transcripts, n_neg: int, seed: int = 7) -> list:
    """Labeled reads (tight and 60-s grid windows) + random non-ad windows,
    built as the bake-off built them."""
    items, spans, segmap = [], {}, {}
    for lab in labels:
        ep = lab["episode_id"]
        segs = segmap[ep] = segments(transcripts, ep)
        spans[ep] = []
        for r in lab.get("reads", []):
            sp = score_labels.resolve_span(r, segs)
            if sp is None:
                continue
            spans[ep].append(sp)
            if r.get("read_type") == "uncertain":
                continue
            mid = (sp[0] + sp[1]) / 2
            lo = (mid // 60) * 60
            for win, text in (("tight", _window(segs, sp[0] - 10, sp[1] + 10)),
                              ("jev60", _window(segs, lo, lo + 60))):
                items.append({"ep": ep, "kind": "read", "win": win, "label": r["brand"],
                              "text": text})
    rng, eps, negs, tries = random.Random(seed), sorted(spans), [], 0
    while len(negs) < n_neg and tries < 10000:
        tries += 1
        ep = rng.choice(eps)
        segs = segmap[ep]
        lo = rng.uniform(0, max(0, segs[-1]["end"] - 60))
        if any(lo < e + 10 and lo + 60 > s - 10 for s, e in spans[ep]):
            continue
        if any(abs(o["lo"] - lo) < 60 and o["ep"] == ep for o in negs):
            continue
        text = _window(segs, lo, lo + 60)
        if len(text.split()) < 30:
            continue
        negs.append({"ep": ep, "kind": "neg", "win": "jev60", "label": None, "lo": lo,
                     "text": text})
    return items + negs


def _metrics(rows, items, tau, win):
    reads = [(r, it) for r, it in zip(rows, items) if it["kind"] == "read" and it["win"] == win]
    negs = [r for r, it in zip(rows, items) if it["kind"] == "neg"]
    ok = sum(1 for r, it in reads if r and r[0][1] >= tau and lenient_ok(r[0][0], it["label"]))
    fn = sum(1 for r in negs if r and r[0][1] >= tau)
    return ok / len(reads) if reads else 0.0, fn / len(negs) if negs else 0.0


def tune_threshold(rows, items) -> tuple:
    """The bake-off's rule: lowest-first scan over observed top scores; a
    threshold replaces the best only when it improves the objective by more
    than 0.02."""
    best = (-9.0, 0.0)
    for tau in sorted({0.0} | {round(r[0][1], 3) for r in rows if r}):
        t1_t, fn_t = _metrics(rows, items, tau, "tight")
        t1_j, _ = _metrics(rows, items, tau, "jev60")
        obj = (t1_t + t1_j) / 2 - 0.5 * fn_t
        if obj > best[0] + 0.02:
            best = (obj, tau)
    return best[1], round(best[0], 3)


def cmd_tune(a) -> int:
    labels = score_labels.load_labels(a.labels_dir)
    items = tune_items(labels, a.transcripts, a.negatives)
    namers = Namers(a.brand_corpus, a.gliner)
    words = namers.for_episode(items[0]["ep"]).words if items else frozenset()
    rows = {"cta": [[(c, s) for c, s, _ in bn.rank_cta(it["text"], words)] for it in items]}
    namer = namers.for_episode(items[0]["ep"]) if items else None
    if namer is not None and namer.gliner is not None:
        rows["gliner"] = [[(c, s) for c, s, _ in bn.rank_gliner(it["text"], namer.gliner)]
                          for it in items]
    out = {"split": a.split_name, "reads": sum(1 for it in items if it["kind"] == "read"
                                               and it["win"] == "tight"),
           "negatives": sum(1 for it in items if it["kind"] == "neg"), "thresholds": {},
           "objective": {}}
    for src, rws in rows.items():
        out["thresholds"][src], out["objective"][src] = tune_threshold(rws, items)
    print(json.dumps(out))
    return 0


# ---------------------------------------------------------------- canon

NAMED_REL = "experiments/named-reads/output/candidates_normalized.json"


def export_inputs(export_dir) -> tuple:
    """(named candidates, transcript paths, {ep: show}) of a corpus export."""
    root = Path(export_dir)
    shows = {e["ep_id"]: e.get("show_title") for e in _load(root / "data" / "episodes.json")}
    return (_load(root / NAMED_REL), sorted(glob.glob(str(root / "transcripts" / "*.json"))),
            shows)


def _raw(c: dict) -> dict:
    """The candidate as named: brand / brand_spoken from brand_raw (an
    export may already carry a canonical brand), no canonical fields."""
    key = bc.brand_key(c.get("brand_raw")) if c.get("brand_raw") else None
    out = {k: v for k, v in c.items() if k not in ("brand_canonical", "canon_reason")}
    return dict(out, brand=key or None, brand_spoken=key or None) if "brand_raw" in c else out


def _canonical(c: dict) -> dict:
    """Scoring copy: brand / brand_spoken = brand_canonical when present."""
    b = c.get("brand_canonical")
    return dict(c, brand=b, brand_spoken=b) if b else c


def _distinct(cands, confident=True) -> int:
    return len({c["brand"] for c in cands if c.get("brand_spoken")
                and not (confident and c.get("needs_review"))})


def cmd_canon(a) -> int:
    labels = score_labels.load_labels(a.labels_dir)
    troots = [a.transcripts]
    cands = [_raw(c) for c in _load(a.candidates)]
    spans, paths, shows = export_inputs(a.canon_export)
    spans = [_raw(s) for s in spans]
    words = bc.load_common_words()
    after, changes = [], []
    for ep in sorted({c["ep_id"] for c in cands}):
        # The export's own spans and transcript of `ep` are left out; the
        # evaluated candidates of `ep` stand in for them (as in production,
        # where the map covers every named episode), so its raw brands are
        # map keys with only their own support.
        mine = [c for c in cands if c["ep_id"] == ep]
        own = [{"episode_id": ep, "show": ep, "brand_raw": c.get("brand_raw")} for c in mine]
        doc = bc.build_canon([s for s in spans if s["ep_id"] != ep] + own, None,
                             bc.TranscriptIndex(paths, shows, exclude={ep}), common_words=words)
        mine = bc.apply_canon(mine, doc)
        after.extend(_canonical(c) for c in mine)
        changes += [{"ep_id": ep, "from": c["brand_spoken"], "to": c["brand_canonical"],
                     "reason": c["canon_reason"], "needs_review": c["needs_review"]}
                    for c in mine if c.get("brand_canonical")]
    keep = ("protocol", "strict", "candidates")
    holdout = {}
    for name, sel in (("all", lambda c: True), ("confident", lambda c: not c["needs_review"])):
        b = summarize(labels, [c for c in cands if sel(c)], troots)
        f = summarize(labels, [c for c in after if sel(c)], troots)
        holdout[name] = {"before": {k: b[k] for k in keep}, "after": {k: f[k] for k in keep}}
    full = bc.build_canon(spans, None, bc.TranscriptIndex(paths, shows), common_words=words)
    applied = [_canonical(c) for c in bc.apply_canon(spans, full)]
    merged = sorted(k for k, v in full["map"].items() if v["canonical"] != k)
    sample = sorted(random.Random(a.seed).sample(merged, min(a.sample, len(merged))))
    report = {"candidates": a.candidates, "canon_export": a.canon_export,
              "holdout": holdout, "holdout_changes": changes,
              "corpus": {"spans": len(spans), "episodes": len({s["ep_id"] for s in spans}),
                         "distinct_confident_before": _distinct(spans),
                         "distinct_confident_after": _distinct(applied),
                         "distinct_all_before": _distinct(spans, False),
                         "distinct_all_after": _distinct(applied, False),
                         "spans_changed": sum(1 for x, y in zip(spans, applied)
                                              if x["brand"] != y["brand"]),
                         "confident_spans_changed": sum(
                             1 for x, y in zip(spans, applied)
                             if x["brand"] != y["brand"] and not x["needs_review"]),
                         **bc.summarize(full, top=a.sample)},
              "sample": [dict(raw=k, **{f: full["map"][k][f] for f in ("canonical", "reason")})
                         for k in sample],
              "rejected": full["rejected"]}
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    _dump(report, out / "canon-report.json")
    _dump(full, out / "brand-canon.json")
    print(format_canon(report))
    return 0


def format_canon(r: dict) -> str:
    lines = ["| holdout | protocol P / R before | after | strict P / R before | after |",
             "|---|---|---|---|---|"]
    for name, m in r["holdout"].items():
        b, f = m["before"], m["after"]
        lines.append(f"| {name} | {b['protocol']['precision']:.2f} / {b['protocol']['recall']:.2f}"
                     f" | {f['protocol']['precision']:.2f} / {f['protocol']['recall']:.2f}"
                     f" | {b['strict']['precision']:.2f} / {b['strict']['recall']:.2f}"
                     f" | {f['strict']['precision']:.2f} / {f['strict']['recall']:.2f} |")
    lines.append(f"holdout brand changes: {r['holdout_changes']}")
    c = r["corpus"]
    lines.append(f"corpus: {c['spans']} spans / {c['episodes']} episodes; distinct confident "
                 f"brands {c['distinct_confident_before']} -> {c['distinct_confident_after']}; "
                 f"all {c['distinct_all_before']} -> {c['distinct_all_after']}; spans changed "
                 f"{c['spans_changed']} (confident {c['confident_spans_changed']}); merged keys "
                 f"{c['merged_keys']}; rejected pairs {c['rejected']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- cli

def _floats(v):
    return [float(x) for x in v.split(",") if x.strip()]


def _ints(v):
    return [int(x) for x in v.split(",") if x.strip()]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("canon")
    s.add_argument("--split", choices=sorted(PRESETS), default=None)
    s.add_argument("--split-name", default=None)
    s.add_argument("--transcripts", default=None)
    s.add_argument("--labels-dir", action="append", default=None)
    s.add_argument("--candidates", required=True,
                   help="a run config's candidates_normalized.json")
    s.add_argument("--canon-export", default=f"{CS}/export",
                   help="corpus export whose named candidates + transcripts build the map")
    s.add_argument("--out-dir", required=True)
    s.add_argument("--sample", type=int, default=30)
    s.add_argument("--seed", type=int, default=7)
    s = sub.add_parser("show-identity")
    s.add_argument("--split", choices=sorted(PRESETS), default=None)
    s.add_argument("--split-name", default=None)
    s.add_argument("--transcripts", default=None)
    s.add_argument("--labels-dir", action="append", default=None)
    s.add_argument("--feeds", action="append", default=None)
    s.add_argument("--show-identity", required=True, help="identity cache dir")
    s.add_argument("--max-fetches", type=int, default=sp.MAX_FETCHES)
    s.add_argument("--refresh", action="store_true", help="refetch every cached feed")
    for name in ("run", "tune"):
        s = sub.add_parser(name)
        s.add_argument("--split", choices=sorted(PRESETS), default=None,
                       help="preset paths (explicit flags override)")
        s.add_argument("--split-name", default=None)
        s.add_argument("--transcripts", default=None)
        s.add_argument("--labels-dir", action="append", default=None)
        s.add_argument("--brand-corpus", action="append", default=None,
                       help="transcript dir(s) the repair brand list is harvested from")
        s.add_argument("--gliner", choices=("small", "none"), default="small")
        if name == "run":
            s.add_argument("--jev-dir", default=None)
            s.add_argument("--jev-results", default=None)
            s.add_argument("--context-windows", type=_ints, default=[0, 1])
            s.add_argument("--thresholds", type=_floats, default=[0.6, 0.7, 0.8])
            s.add_argument("--sub-window-s", type=_floats, default=[0.0],
                           help="sub-window naming lengths to sweep (0 = whole windows)")
            s.add_argument("--out-dir", required=True)
            s.add_argument("--baseline-candidates", default=None,
                           help="also score these candidates (e.g. the rules detector)")
            s.add_argument("--self-promo", action="store_true",
                           help="apply the self-promo filter (needs --show-identity)")
            s.add_argument("--show-identity", default=None, help="identity cache dir")
            s.add_argument("--feeds", action="append", default=None,
                           help="episodes.json / sidecar.json files mapping episodes to feeds")
            s.add_argument("--self-promo-export", default=None,
                           help="corpus export whose named reads add brand recurrence")
        else:
            s.add_argument("--negatives", type=int, default=20)
    a = p.parse_args(argv)
    pre = PRESETS.get(a.split) or {}
    a.split_name = a.split_name or a.split or "custom"
    a.transcripts = a.transcripts or pre.get("transcripts")
    a.labels_dir = a.labels_dir or pre.get("labels")
    if a.command == "canon":
        return cmd_canon(a)
    if a.command in ("show-identity", "run"):
        a.feeds = a.feeds or pre.get("feeds")
    if a.command == "show-identity":
        return cmd_show_identity(a)
    a.brand_corpus = a.brand_corpus or pre.get("brand_corpus")
    if a.command == "tune" and a.split == "holdout":
        p.error("tune runs on the dev set only")
    if a.command == "run":
        if not (a.jev_dir or a.jev_results):
            a.jev_dir, a.jev_results = pre.get("jev_dir"), pre.get("jev_results")
        a.self_promo_export = a.self_promo_export or pre.get("self_promo_export")
        if a.self_promo and not a.show_identity:
            p.error("--self-promo needs --show-identity")
        return cmd_run(a)
    return cmd_tune(a)


if __name__ == "__main__":
    raise SystemExit(main())
