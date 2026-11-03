"""Corpus evaluation behind `corpus_run.py score` and `corpus_run.py
align-labels` (productionised from the 2026-10-10 engine check's scratch
score_export.py and align_labels.py).

score: the named reads of an export (or of corpora, exported to a
temporary dir) against a labels folder, with the project's scorer
(score_labels.score: lenient = the protocol metric, strict;
named_reads_eval.location_recall), with denominators, per transcript
source, and the self-promo flag judged against the labels.

align_labels: labels made on one fetch of an episode moved onto another
fetch's transcript (dynamic ad insertion changes pre-rolls and spots):
text alignment of the two transcripts, each label item shifted by the
median offset of the matched word runs near it; reads whose words are not
in the new transcript are marked absent_in_audio.
"""
from __future__ import annotations

import bisect
import difflib
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import named_reads_eval as nre  # noqa: E402
import score_labels  # noqa: E402
from earwitness.gazetteer import normalize_brand  # noqa: E402
from earwitness.pipeline import corpus as cr  # noqa: E402

# labels note house promos as organic mentions named "<brand> (self-promo)"
SELF_PROMO_MARK = "self-promo"
_TLD_TAIL = re.compile(r"(com|net|org|io|co|fm|ai|app|tv|me)$")


# ---------------------------------------------------------------- score

def transcript_source(doc, refined: bool = False) -> str:
    """The episode's transcript path: tiny / parakeet / <model>, +gapfill
    when Parakeet gap fill ran, +refine when its names came from refined
    text."""
    doc = doc if isinstance(doc, dict) else {}
    prov = doc.get("provenance") or {}
    model = str(doc.get("model") or prov.get("model") or "unknown")
    base = "tiny" if "tiny" in model else "parakeet" if "parakeet" in model else \
        model.rsplit("/", 1)[-1]
    gf = prov.get("gap_fill")
    if isinstance(gf, dict) and not gf.get("error") and gf.get("model"):
        base += "+gapfill"
    return base + ("+refine" if refined else "")


def _ckey(b) -> str:
    k = re.sub(r"[^a-z0-9]", "", (normalize_brand(b) or b or "").lower())
    return _TLD_TAIL.sub("", k) if len(k) > 6 else k


def spacing_insensitive(cands: list, labels: list) -> list:
    """Scoring copy: a candidate whose brand equals a labeled brand of its
    episode after dropping spaces, punctuation and a trailing TLD takes that
    label's spelling (helixsleep = helix sleep, public = public.com)."""
    by = {lab["episode_id"]: [r["brand"] for r in lab.get("reads", [])] for lab in labels}
    out = []
    for c in cands:
        b = c.get("brand_spoken")
        hit = next((lb for lb in by.get(c["ep_id"], []) if b and _ckey(b) == _ckey(lb)), None)
        out.append(dict(c, brand_spoken=hit, brand=hit) if hit and hit != b else c)
    return out


def _metrics(labels, cands, troots) -> dict:
    m = nre.summarize(labels, cands, troots)
    return {k: m[k] for k in ("candidates", "reads", "located", "location_recall",
                              "protocol", "strict")} | {"detail": m["detail"]}


def _self_promo(labels, named, all_detail, troots) -> dict:
    outcome = {d["candidate"]: d["outcome"] for d in all_detail if d.get("candidate")}
    promos = {}
    for lab in labels:
        segs = score_labels.load_segments(lab["episode_id"], troots)
        promos[lab["episode_id"]] = [sp for sp in (score_labels.resolve_span(o, segs) for o in (
            lab.get("organic_mentions") or []) if SELF_PROMO_MARK in str(o.get("brand", "")))
            if sp is not None]
    flagged, missed = [], []
    for c in named:
        span = (float(c["start_s"]), float(c["end_s"]))
        on_promo = any(score_labels.substantial_overlap(span, p)
                       for p in promos.get(c["ep_id"], ()))
        if c.get("self_promo"):
            flagged.append({"ep_id": c["ep_id"], "brand": c.get("brand"),
                            "reason": c.get("self_promo_reason"), "span": list(span),
                            "outcome_if_kept": outcome.get(c["candidate_id"]),
                            "on_labeled_self_promo": on_promo})
        elif on_promo:
            missed.append({"ep_id": c["ep_id"], "brand": c.get("brand"), "span": list(span)})
    return {"flagged": len(flagged),
            "flagged_on_labeled_self_promo": sum(f["on_labeled_self_promo"] for f in flagged),
            "real_ads_removed": sum(f["outcome_if_kept"] == "TP" for f in flagged),
            "missed": len(missed), "labeled_self_promo": sum(len(v) for v in promos.values()),
            "flagged_detail": flagged, "missed_detail": missed}


def score_named(export_dir, label_dirs, label_transcripts=None) -> dict:
    """The score report for one export dir (see the module doc)."""
    export_dir = Path(export_dir)
    labels = score_labels.load_labels([str(d) for d in label_dirs])
    eps = {lab["episode_id"] for lab in labels}
    path = export_dir / cr.EXPORT_NAMED_CANDIDATES_REL
    named_all = json.loads(path.read_text()) if path.is_file() else []
    named = [c for c in named_all if c.get("ep_id") in eps]
    troots = [str(t) for t in label_transcripts] if label_transcripts else \
        [str(export_dir / "transcripts")]
    ads = [c for c in named if not c.get("self_promo")]
    conf = [c for c in ads if not c.get("needs_review")]
    variants = {"ads": (labels, ads), "ads_confident": (labels, conf),
                "all_named": (labels, named),
                "ads_spacing_insensitive": (labels, spacing_insensitive(ads, labels))}
    rep = {"export": str(export_dir), "labels": [str(d) for d in label_dirs],
           "labeled_episodes": len(labels),
           "episodes_in_export": sorted(e for e in eps if (export_dir / "transcripts"
                                                            / f"{e}.json").is_file()),
           "episodes_missing_from_export": sorted(e for e in eps if not (
               export_dir / "transcripts" / f"{e}.json").is_file()),
           "reads_labeled": sum(1 for lab in labels for r in lab.get("reads", [])),
           "reads_uncertain_or_absent": sum(1 for lab in labels for r in lab.get("reads", [])
                                            if r.get("read_type") == "uncertain"),
           "named_reads": len(named), "second_brand_reads": sum(
               1 for c in named if c.get("second_brand_of") is not None),
           "variants": {}, "by_source": {}}
    for name, (labs, cands) in variants.items():
        rep["variants"][name] = _metrics(labs, cands, troots)
    rep["self_promo"] = _self_promo(labels, named, rep["variants"]["all_named"]["detail"],
                                    troots)
    refined = {c["ep_id"] for c in named if (c.get("provenance") or {}).get("refine_model")}
    groups: dict = {}
    for lab in labels:
        doc = cr._read_json(export_dir / "transcripts" / f"{lab['episode_id']}.json")
        groups.setdefault(transcript_source(doc, lab["episode_id"] in refined), []).append(lab)
    for src, labs in sorted(groups.items()):
        ids = {lab["episode_id"] for lab in labs}
        m = _metrics(labs, [c for c in ads if c["ep_id"] in ids], troots)
        m.pop("detail")
        rep["by_source"][src] = {"episodes": len(labs), **m}
    return rep


def score(*, export=None, corpora=None, labels, label_transcripts=None) -> dict:
    """score_named on `export`, or on `corpora` exported to a temporary
    dir (removed afterwards)."""
    if export:
        return score_named(export, labels, label_transcripts)
    tmp = tempfile.mkdtemp(prefix="ew-score-")
    try:
        cr.export(list(corpora), tmp)
        size = sum(f.stat().st_size for f in Path(tmp).rglob("*") if f.is_file())
        rep = score_named(tmp, labels, label_transcripts)
        rep["temp_export_bytes"] = size
        rep["export"] = None
        rep["corpora"] = [str(c) for c in corpora]
        return rep
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _row(name, m) -> str:
    p, s = m["protocol"], m["strict"]
    return (f"  {name:<24} {m['candidates']:>5}  {m['located']:>3}/{m['reads']:<3}  "
            f"{p['precision']:.2f} / {p['recall']:.2f} ({p['TP']}/{p['FP']}/{p['FN']})   "
            f"{s['precision']:.2f} / {s['recall']:.2f} ({s['TP']})")


def format_score(rep: dict) -> str:
    sp = rep["self_promo"]
    lines = [f"labels: {rep['labeled_episodes']} episodes ({len(rep['episodes_in_export'])} in "
             f"the export), {rep['reads_labeled']} reads of which "
             f"{rep['reads_uncertain_or_absent']} uncertain/absent (not scored); "
             f"{rep['named_reads']} named reads ({rep['second_brand_reads']} second brands)",
             f"  {'variant':<24} {'cands':>5}  located  lenient P / R (TP/FP/FN)   "
             "strict P / R (TP)"]
    missing = rep.get("episodes_missing_from_export") or []
    if missing:
        lines.insert(1, f"WARNING: {len(missing)} labeled episodes are not in the export: "
                     + ", ".join(missing[:20]) + (" ..." if len(missing) > 20 else "")
                     + " (their reads count as missed)")
    if rep.get("temp_export_bytes") is not None:
        lines.insert(0, f"temporary export: {rep['temp_export_bytes'] / 1e6:.1f} MB "
                        "(removed after scoring)")
    lines += [_row(k, m) for k, m in rep["variants"].items()]
    lines.append("by transcript source (ads):")
    lines += [_row(f"{k} ({m['episodes']} eps)", m) for k, m in rep["by_source"].items()]
    lines.append(f"self-promo: {sp['flagged']} flagged ({sp['flagged_on_labeled_self_promo']} on "
                 f"a labeled self-promo, {sp['real_ads_removed']} real ads removed); "
                 f"{sp['missed']} named reads on a labeled self-promo not flagged")
    return "\n".join(lines)


# ---------------------------------------------------------------- align

def _words(segs) -> list:
    out = []
    for s in segs:
        toks = [t for t in (re.sub(r"[^a-z0-9']", "", w.lower())
                            for w in str(s.get("text", "")).split()) if t]
        for i, t in enumerate(toks):
            out.append((t, s["start"] + (i + 0.5) / len(toks) * (s["end"] - s["start"])))
    return out


LOCAL_WINDOW_S = 90.0     # a label is first looked for this close to its own time
LOCAL_MIN_SHARE = 0.2     # share of its words that must agree on a local offset
LOCAL_AGREE_S = 5.0       # matched words this close to the median offset agree on it


def _audio_sha(*docs) -> str | None:
    """The audio sha256 a label doc or a transcript doc records (top level or
    in its provenance/meta), else None."""
    for d in docs:
        if not isinstance(d, dict):
            continue
        for src in (d, d.get("provenance"), d.get("meta")):
            if isinstance(src, dict) and src.get("audio_sha256"):
                return str(src["audio_sha256"]).lower()
    return None


def read_sha256sums(path) -> dict:
    """{file stem: sha256} of a `sha256sum` listing (`<sha>  <ep>.mp3`)."""
    out = {}
    for line in Path(path).read_text().splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            out[Path(parts[1].strip().lstrip("*")).stem] = parts[0].lower()
    return out


def align_episode(lab: dict, lsegs: list, nsegs: list, min_insert_s: float = 15.0,
                  same_audio: bool = False, local_window_s: float = LOCAL_WINDOW_S) -> tuple:
    """(labels on the new timeline, audio-matched labels, report) for one
    episode: label transcript `lsegs`, new transcript `nsegs`.

    `same_audio`: the labels were made on this very audio (equal checksums),
    so every time is kept and no text alignment runs.

    Otherwise each item is first looked for in the new transcript within
    `local_window_s` of its own time; only when that finds it is the item
    moved by that local offset. A global alignment of a transcript whose
    spot repeats (the same ad pod every ~15 min) can pair a read with a later
    repeat, so the global offset is the fallback, not the first choice."""
    a, b = ([], []) if same_audio else (_words(lsegs), _words(nsegs))
    sm = difflib.SequenceMatcher(None, [x[0] for x in a], [x[0] for x in b], autojunk=False)
    blocks_all = sm.get_matching_blocks()
    mb = [m for m in blocks_all if m.size >= 6]
    matched = [False] * len(a)   # label words in a matched run of >= 3
    for m in blocks_all:
        if m.size >= 3:
            for k in range(m.a, m.a + m.size):
                matched[k] = True
    pts = sorted((a[m.a + k][1], b[m.b + k][1] - a[m.a + k][1])
                 for m in mb for k in (0, m.size - 1))

    def delta_at(t, win=120.0):
        near = [d for x, d in pts if abs(x - t) <= win]
        if not near:
            near = [min(pts, key=lambda p: abs(p[0] - t))[1]] if pts else [0.0]
        return statistics.median(near)

    atimes, btimes = [x[1] for x in a], [x[1] for x in b]

    def local_match(lo, hi):
        """(offset, share of the label words that agree on it) of the label
        words in [lo, hi] found in the new words within local_window_s of
        them, or None. Matched runs of >= 2 words vote; words whose offset
        is within LOCAL_AGREE_S of the median offset count as agreeing, so
        scattered chance matches of common words do not."""
        i0, i1 = bisect.bisect_left(atimes, lo), bisect.bisect_right(atimes, hi)
        j0 = bisect.bisect_left(btimes, lo - local_window_s)
        j1 = bisect.bisect_right(btimes, hi + local_window_s)
        if i1 - i0 < 3 or j1 <= j0:
            return None
        m = difflib.SequenceMatcher(None, [x[0] for x in a[i0:i1]],
                                    [x[0] for x in b[j0:j1]], autojunk=False)
        offs = [btimes[j0 + blk.b + k] - atimes[i0 + blk.a + k]
                for blk in m.get_matching_blocks() if blk.size >= 2 for k in range(blk.size)]
        if not offs:
            return None
        med = statistics.median(offs)
        agree = [o for o in offs if abs(o - med) <= LOCAL_AGREE_S]
        share = len(agree) / (i1 - i0)
        if len(agree) < 4 or share < LOCAL_MIN_SHARE:
            return None
        return statistics.median(agree), share

    nend = nsegs[-1]["end"] if nsegs else 0.0
    lend = lsegs[-1]["end"] if lsegs else 0.0
    blocks = [(a[m.a][1], a[m.a + m.size - 1][1], b[m.b][1], b[m.b + m.size - 1][1])
              for m in mb]
    inserted = []
    if blocks and blocks[0][2] - blocks[0][0] > min_insert_s:
        inserted.append((0.0, blocks[0][2]))
    for p, q in zip(blocks, blocks[1:]):
        if (q[2] - p[3]) - (q[0] - p[1]) > min_insert_s:
            inserted.append((p[3], q[2]))
    if blocks and (nend - blocks[-1][3]) - (lend - blocks[-1][1]) > min_insert_s:
        inserted.append((blocks[-1][3], nend))
    # stretches of the new audio whose words match nothing in the label
    # transcript (replaced or inserted spots; also long music or silence)
    nmatched = sorted({b[m.b + k][1] for m in blocks_all if m.size >= 3 for k in range(m.size)})
    edges = [0.0] + nmatched + [nend]
    unmatched = [] if same_audio else [(x, y) for x, y in zip(edges, edges[1:])
                                       if y - x > min_insert_s]

    def shift(item):
        sp = score_labels.resolve_span(item, lsegs)
        if sp is None:
            return None, None, False
        out = {k: v for k, v in item.items() if k != "seg_idx"}
        if same_audio:
            out.update(start_s=round(sp[0], 2), end_s=round(sp[1], 2),
                       label_start_s=sp[0], label_end_s=sp[1])
            return out, None, True
        inside = [k for k, (_, t) in enumerate(a) if sp[0] <= t <= sp[1]]
        loc = local_match(*sp)
        if loc is not None:
            d0 = d1 = loc[0]
            cov = loc[1]
        else:
            d0, d1 = delta_at(sp[0]), delta_at(sp[1])
            cov = sum(matched[k] for k in inside) / len(inside) if inside else None
        out.update(start_s=round(sp[0] + d0, 2), end_s=round(sp[1] + d1, 2),
                   label_start_s=sp[0], label_end_s=sp[1])
        return out, cov, loc is not None

    new = {k: v for k, v in lab.items() if k not in ("reads", "organic_mentions", "unresolved")}
    new.update(reads=[], organic_mentions=[], unresolved=[])
    rinfo, unplaced = [], []
    for r in lab.get("reads", []):
        o, cov, located = shift(r)
        if o is None:
            unplaced.append(r.get("brand"))
            continue
        o["text_aligned_share"] = None if cov is None else round(cov, 2)
        ins = sum(max(0.0, min(y, o["end_s"]) - max(x, o["start_s"])) for x, y in unmatched)
        if same_audio:
            o["absent_in_audio"] = bool(r.get("absent_in_audio"))
        else:   # found near its own time = present, however little of it matched
            o["absent_in_audio"] = bool(not located and cov is not None and cov < 0.3
                                        and ins > 0.5 * (o["end_s"] - o["start_s"]))
        new["reads"].append(o)
        rinfo.append({"brand": r.get("brand"), "read_type": r.get("read_type"),
                      "label": [o["label_start_s"], o["label_end_s"]],
                      "audio": [o["start_s"], o["end_s"]], "aligned_share": o["text_aligned_share"],
                      "absent_in_audio": o["absent_in_audio"]})
    for kind in ("organic_mentions", "unresolved"):
        for it in lab.get(kind, []):
            o, _, _ = shift(it)
            if o is not None:
                new[kind].append(o)
    matched_doc = json.loads(json.dumps(new))
    for r in matched_doc["reads"]:
        if r["absent_in_audio"]:
            r["read_type_label"], r["read_type"] = r.get("read_type"), "uncertain"
    matched_doc["unresolved"] += [{"start_s": round(x, 2), "end_s": round(y, 2),
                                   "note": "audio-not-in-labeled-download"}
                                  for x, y in unmatched]
    report = {"same_audio": same_audio, "label_len_s": lend, "audio_len_s": nend, "blocks": len(mb),
              "median_offset_s": (0.0 if same_audio else
                                  round(statistics.median(d for _, d in pts), 1) if pts else None),
              "inserted": [[round(x, 1), round(y, 1)] for x, y in inserted],
              "unmatched": [[round(x, 1), round(y, 1)] for x, y in unmatched],
              "reads": rinfo, "reads_absent": sum(r["absent_in_audio"] for r in rinfo),
              "reads_unplaced": unplaced}
    return new, matched_doc, report


def align_labels(label_dirs, label_transcripts, transcripts, out,
                 min_insert_s: float = 15.0, label_audio_sums=None) -> dict:
    """Align every label (first dir wins a duplicate episode) whose episode
    has both transcripts. When the label's audio sha256 (the label doc, else
    its transcript, else `label_audio_sums`, a sha256sum listing of the
    labeled audio) equals the audio sha256 the new transcript records, the
    label times are kept and no alignment runs. Missing or unequal
    checksums align on text. Writes <out>/labels/, <out>/labels-matched/
    (absent reads made uncertain, unmatched new audio unresolved) and
    <out>/alignment.json. Returns the alignment report."""
    out = Path(out)
    (out / "labels").mkdir(parents=True, exist_ok=True)
    (out / "labels-matched").mkdir(parents=True, exist_ok=True)
    rep = {"episodes": {}, "skipped": []}
    sums = read_sha256sums(label_audio_sums) if label_audio_sums else {}
    for lab in score_labels.load_labels([str(d) for d in label_dirs]):
        ep = lab["episode_id"]
        lt = cr._read_json(Path(label_transcripts) / f"{ep}.json")
        nt = cr._read_json(Path(transcripts) / f"{ep}.json")
        if not (isinstance(lt, dict) and isinstance(nt, dict)):
            rep["skipped"].append(ep)
            continue
        lsha = _audio_sha(lab, lt) or sums.get(ep)
        same = bool(lsha) and lsha == _audio_sha(nt)
        new, matched, r = align_episode(lab, lt.get("segments") or [], nt.get("segments") or [],
                                        min_insert_s, same_audio=same)
        (out / "labels" / f"{ep}.json").write_text(json.dumps(new, indent=1))
        (out / "labels-matched" / f"{ep}.json").write_text(json.dumps(matched, indent=1))
        rep["episodes"][ep] = r
    rep["reads"] = sum(len(r["reads"]) for r in rep["episodes"].values())
    rep["reads_absent"] = sum(r["reads_absent"] for r in rep["episodes"].values())
    (out / "alignment.json").write_text(json.dumps(rep, indent=1))
    return rep


def format_alignment(rep: dict) -> str:
    same = sorted(ep for ep, r in rep["episodes"].items() if r.get("same_audio"))
    lines = [f"aligned {len(rep['episodes'])} episodes: {rep['reads']} reads, "
             f"{rep['reads_absent']} absent from the new audio; skipped (a transcript "
             f"missing): {rep['skipped'] or '-'}"]
    if same:
        lines.append(f"  identical audio (checksums match), labels kept as they are: "
                     f"{', '.join(same)}")
    for ep, r in sorted(rep["episodes"].items()):
        absent = [x["brand"] for x in r["reads"] if x["absent_in_audio"]]
        lines.append(f"  {ep}: offset {r['median_offset_s']} s, {len(r['reads'])} reads"
                     + (f", absent: {', '.join(absent)}" if absent else "")
                     + (f", unmatched new audio {r['unmatched']}" if r["unmatched"] else ""))
    return "\n".join(lines)

