#!/usr/bin/env python3
"""Brand-agnostic ad-read detection: find ALL sponsor reads in transcripts.

Reference implementation hardened for T-042 (same anchor/cluster architecture
as the Hatch VM original; not a rewrite):

Fixes applied (see README §4):
  1. Cluster splitting — anchors merge only within a tightened temporal
     window (<=6 segments) AND with a compatible brand name. A cluster
     carries at most one named brand: a different named brand starts a new
     cluster even across unnamed anchors, and a sponsor phrase never joins
     a cluster of another (or no) brand. A same-brand CTA closes the read its
     opener started (joins within --context-after segments).
  2. Brand capture — case-insensitive anchor regexes; raw captures are
     trimmed at trailing-word boundaries and normalized against the shared
     gazetteer (alias normalization + strict fuzzy fallback for STT
     mangling). URL attribution matches the domain label exactly.
  3. Deduplication — deterministic: same-brand candidates in one episode
     whose windows overlap (IoU > 0.3, or one window holds the other's
     anchor) merge; losers recorded in ``dedupe_log``.
  5. Anchor-role windows — sponsor phrases open a read (window runs
     forward); CTA/marker-only clusters usually close a read (window runs
     back --cta-context-before) but may sit mid-read, so every read also
     runs on up to --cta-context-after segments past its last CTA. No
     window crosses into a different-brand neighbouring cluster.
  4. Attribution separation — ``brand_spoken`` (inferred from spoken anchor
     text) is kept separate from ``brand_url`` / ``brand_code`` (derived from
     URL/code tokens). They are never conflated: ``brand`` = brand_spoken.

Output layers (kept separate):
  output/candidates_raw.json        — one record per cluster pre-dedupe
  output/candidates_normalized.json — post-dedupe, gazetteer-normalized

Usage:
  detect_all.py [--data-root D] [--transcript-root T] [--output-root O]
                [--runs-root R] [--run-id ID] [--limit K] [--episodes IDS]
                [--cluster-window N] [--context-before N] [--context-after N]
                [--cta-context-before N] [--cta-context-after N]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from earwitness.config import (
    add_common_args, parse_episode_filter, resolve, resolve_fetch_date,
)
from earwitness.gazetteer import (
    ALIAS_TO_CANONICAL, normalize_brand, trim_trailing_words,
)
from earwitness.logging_utils import RunLogger

# Case-insensitive captures (fix #2). Capture group is deliberately wide;
# trim_trailing_words() + normalize_brand() clean it up downstream.
SPONSOR_RES = [
    (re.compile(r"brought to you by\s+(.{1,60})", re.I), "brought_to_you"),
    (re.compile(r"sponsored by\s+(.{1,60})", re.I), "sponsored_by"),
    (re.compile(r"this episode is sponsored by\s+(.{1,60})", re.I), "sponsored_by"),
    (re.compile(r"today'?s sponsor is\s+(.{1,60})", re.I), "todays_sponsor"),
    (re.compile(r"thanks to\s+(.{1,60}?)\s+for sponsoring", re.I), "thanks_to"),
    (re.compile(r"our sponsors?(?:[\s,]+(?:uh|um|also|again|for|today|this|week|episode))*"
                r"(?:[\s,]+(?:is|are))?[\s,]+(.{1,60})", re.I), "our_sponsor"),
    # Bounded gap: an unbounded lazy ".*?" is quadratic on long segments.
    (re.compile(r"support for [^.]{0,120}? comes from\s+(.{1,60})", re.I), "support_from"),
]
# Anchor roles (fix #5): sponsor phrases open a read; CTA URLs and
# code/offer markers usually close one.
OPENER_KINDS = {kind for _, kind in SPONSOR_RES}
CLOSER_KINDS = {"cta_url", "marker_only"}
FALLBACK_MARKERS = [
    r"use (?:the )?code", r"promo code", r"for \d+% off",
    r"go to ([\w-]+\.)+(com|net|org|io|co|fm)",
]
FALLBACK_RE = re.compile("|".join(FALLBACK_MARKERS), re.I)
CODE_RES = [
    re.compile(r"use (?:the )?code ([A-Za-z0-9]{3,20})", re.I),
    re.compile(r"code ([A-Za-z0-9]{3,20}) (?:for|at|to get)", re.I),
    re.compile(r"promo code ([A-Za-z0-9]{3,20})", re.I),
]
URL_RE = re.compile(
    r"(?:go to|visit|head to|head over to|check out|at)\s+"
    r"((?:(?!at\s)[\w-]+\s+){0,3}?(?:[\w-]+\.)+(?:com|net|org|io|co|fm)(?:(?:/| slash |/)[\w\-%.~]*)?)",
    re.I)
# Contact-address domains are never ad CTA URLs (observed: "at ISGDpodcast
# at gmail.com", "at Bible Brothers podcast at gmail.com").
EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "aol.com", "protonmail.com",
}


def extract_urls(text: str) -> list[str]:
    """CTA URL evidence from text, excluding contact email addresses."""
    out = set()
    for u in URL_RE.findall(text):
        u = normalize_url(u)
        root = u.lower().split("/")[0]
        if root in EMAIL_DOMAINS or any(
                root.endswith("." + d) for d in EMAIL_DOMAINS):
            continue
        out.add(u)
    return sorted(out)
# Exact domain labels (or full hosts) that identify a brand from URL
# evidence (fix #4). Matched against the registrable label only, never as a
# substring (hero.com is not ro.co); the gazetteer aliases are also tried
# exactly. Spoken multi-word domains are compacted ("chat GPT.com").
URL_BRAND_HINTS = {
    "cachava": "kachava", "kacha": "kachava", "khava": "kachava",
    "birdie": "she's birdie", "sheezbirdie": "she's birdie",
    "shesbirdie": "she's birdie",
    "shopify": "shopify", "stripe": "stripe",
    "chamba": "chumba casino", "cocino": "chumba casino",
    "cassino": "chumba casino", "chumba": "chumba casino",
    "talkspace": "talkspace", "stamp": "stamps.com",
    "quince": "quince", "qvc": "qvc", "progressive": "progressive",
    "chatgpt": "chatgpt",
    # CTA-wave validation scope only (observed label/transcript variants).
    "youtube": "youtube premium", "spinquest": "spinquest",
    "talkiatry": "talkiatry", "tachyatry": "talkiatry",
    "ro.co": "ro", "jonesroad": "jones road beauty",
    "thezone": "dazn", "gofundme": "gofundme",
}
STOPWORDS = {"the", "this", "that", "and", "for", "with", "our", "your",
             "their", "a", "an"}


def normalize_url(u: str) -> str:
    u = re.sub(r"\s+slash\s+", "/", u, flags=re.I)
    u = re.sub(r"\s+dot\s+", ".", u, flags=re.I)
    return u.strip("., ")


CLAUSE_START_RE = re.compile(
    r"\s+(?:who|which|they|and\s+(?:they|it|its|we|i|you|their))\b", re.I)


def raw_brand_from_match(text: str, rx: re.Pattern) -> str | None:
    m = rx.search(text)
    if not m:
        return None
    # Take up to the first sentence boundary; brand names precede it.
    chunk = re.split(r"[.!?;:,]", m.group(1), maxsplit=1)[0]
    # A relative clause or "and they/it ..." ends the name.
    chunk = CLAUSE_START_RE.split(chunk, maxsplit=1)[0]
    chunk = trim_trailing_words(chunk).strip(" .,!?-'’")
    if not chunk or chunk.lower() in STOPWORDS or len(chunk) < 2:
        return None
    return chunk


def url_labels(u: str) -> tuple[str, list[str]]:
    """(host, candidate brand labels) for one URL.

    The registrable label is the last dot-label before the TLD; spoken
    leading words are compacted, longest first ("my friend shopify.com" ->
    myfriendshopify, friendshopify, shopify)."""
    host = u.lower().split("/")[0].strip()
    if host.startswith("www."):
        host = host[4:]
    label = host.rsplit(".", 1)[0].rsplit(".", 1)[-1]
    words = label.split()
    return host, [re.sub(r"[\s-]+", "", " ".join(words[k:]))
                  for k in range(len(words))]


# Generic marketing wrappers around a brand label (getquince, drinkkachava,
# progressiveinsurance). Stripped forms are tried only after exact labels.
URL_PREFIXES = ("get", "try", "drink", "meet", "join", "go", "use", "shop", "my", "the")
URL_SUFFIXES = ("app", "hq", "insurance", "official")


def marketing_stripped(label: str) -> list[str]:
    """Label with one marketing prefix and/or suffix removed. The remainder
    must keep 5+ chars, so everyday words (my-mint, get-ro) never match."""
    heads = [label] + [label[len(p):] for p in URL_PREFIXES if label.startswith(p)]
    out = [h[:-len(x)] for h in heads for x in URL_SUFFIXES if h.endswith(x)]
    return [x for x in heads[1:] + out if len(x) >= 5]


def brand_hint_from_url(u: str) -> str | None:
    host, labels = url_labels(u)
    if host in URL_BRAND_HINTS:
        return URL_BRAND_HINTS[host]
    for lab in labels:
        canon = URL_BRAND_HINTS.get(lab) or (
            ALIAS_TO_CANONICAL.get(lab) if len(lab) >= 5 else None)
        if canon:
            return canon
    for lab in (x for lab in labels for x in marketing_stripped(lab)):
        canon = URL_BRAND_HINTS.get(lab) or ALIAS_TO_CANONICAL.get(lab)
        if canon:
            return canon
    return None


def brand_hint_from_urls(urls: list[str]) -> str | None:
    for u in urls:
        hint = brand_hint_from_url(u)
        if hint:
            return hint
    return None


def url_supports_brand(u: str, brand: str | None) -> bool:
    """URL evidence for ``brand``: its hint is that brand, or (unknown
    domain) a label equals the compacted brand name or, for names of 5+
    characters, contains it."""
    if not brand:
        return False
    hint = brand_hint_from_url(u)
    if hint:
        return hint == brand
    compact = re.sub(r"[^a-z0-9]", "", brand)
    _, labels = url_labels(u)
    return bool(compact) and any(
        lab == compact or (len(compact) >= 5 and compact in lab) for lab in labels)


def url_evidence_key(urls: list[str]) -> tuple:
    """Explicit per-anchor URL evidence key for cluster compatibility."""
    hint = brand_hint_from_urls(urls)
    if hint:
        return ("hint", hint)
    if urls:
        return ("dom", tuple(sorted(u.lower().split("/")[0] for u in urls)))
    return ()


def joins_cluster(cl: list[tuple], anc: tuple, cluster_window: int,
                  context_after: int) -> bool:
    """Whether anchor ``anc`` extends cluster ``cl`` (fixes #1, #5).

    Anchor tuple: (segidx, raw, canon, kind, url_evidence_key)."""
    brand = next((x[2] for x in cl if x[2]), None)
    if anc[3] in OPENER_KINDS and anc[2] != brand:
        # A sponsor phrase opens a new read unless same brand. Exception: an
        # adjacent unnamed CTA/code cluster adopts the opener's brand when
        # none of its URLs point elsewhere ("Use code X ... sponsored by Hims").
        hosts = [h for x in cl for h in anchor_hosts(x)]
        return (brand is None and anc[0] - cl[-1][0] <= cluster_window
                and all(url_supports_brand(h, anc[2]) for h in hosts))
    if anc[2] and brand and anc[2] != brand:
        return False  # at most one named brand per cluster
    if brand and not anc[2] and not all(
            urls_support_cluster(h, cl) for h in anchor_hosts(anc)):
        return False  # unnamed link to another domain
    links_brand = anc[2] == brand or (
        anc[3] == "cta_url" and anchor_hosts(anc)
        and all(urls_support_cluster(h, cl) for h in anchor_hosts(anc)))
    last_key = next((x[4] for x in reversed(cl) if x[4]), ())
    if last_key and anc[4] and last_key != anc[4]:
        return False  # URL evidence changes (other domain / other brand)
    if anc[0] - cl[-1][0] <= cluster_window:
        return True
    # A same-brand CTA closes the read its opener started.
    first_opener = next((x[0] for x in cl if x[3] in OPENER_KINDS), None)
    return (first_opener is not None and brand is not None and links_brand
            and anc[3] in CLOSER_KINDS and anc[0] - first_opener <= context_after)


def anchor_hosts(anc: tuple) -> tuple:
    """Hosts of an anchor's unattributed (no brand hint) URL evidence."""
    return anc[4][1] if anc[4] and anc[4][0] == "dom" else ()


def urls_support_cluster(host: str, cl: list[tuple]) -> bool:
    """URL host consistent with the cluster's brand (canonical or as spoken)."""
    names = {x[2] for x in cl if x[2]} | {x[1].lower() for x in cl
                                           if x[2] and x[3] in OPENER_KINDS and x[1]}
    return any(url_supports_brand(host, n) for n in names)


def cluster_bounds(clusters: list[list], ci: int, n_segs: int, *,
                   context_before: int, context_after: int,
                   cta_context_before: int, cta_context_after: int
                   ) -> tuple[int, int]:
    """Segment slice [lo, hi) for cluster ``ci`` by anchor role (fix #5).

    Opened reads run from just before the opener; CTA-only reads run
    backward from the CTA, never into the previous cluster. Forward, a read
    runs on after its last CTA (a link is often given mid-read) up to
    ``cta_context_after`` segments, or ``context_after`` after an opener,
    and never into the next cluster. A same-brand neighbour does not bound
    (overlap is merged by dedupe)."""
    def brand(c):
        return next((x[2] for x in c if x[2]), None)

    def bounds(nb):
        return brand(nb) is None or brand(nb) != brand(cl)

    cl = clusters[ci]
    prev_last, next_first = -1, n_segs
    if ci > 0 and bounds(clusters[ci - 1]):
        prev_last = clusters[ci - 1][-1][0]
    if ci + 1 < len(clusters) and bounds(clusters[ci + 1]):
        next_first = clusters[ci + 1][0][0]
    if cl[0][3] in OPENER_KINDS:
        lo = cl[0][0] - context_before
    else:
        lo = max(cl[0][0] - cta_context_before, prev_last + 1)
    if cl[-1][3] in CLOSER_KINDS:
        hi = cl[-1][0] + 1 + cta_context_after
        if cl[0][3] in OPENER_KINDS:  # the link may sit early in an opened read
            hi = max(hi, cl[0][0] + context_after)
        hi = min(hi, next_first)
    else:
        hi = min(cl[-1][0] + context_after, next_first)
    return max(0, lo), min(n_segs, hi)


CONFIDENCE_RANK = ["low", "medium", "high"]


def window_iou(a: dict, b: dict) -> float:
    a0, a1 = a["start_s"] or 0, a["end_s"] or 0
    b0, b1 = b["start_s"] or 0, b["end_s"] or 0
    inter = max(0.0, min(a1, b1) - max(a0, b0))
    union = max(a1, b1) - min(a0, b0)
    return inter / union if union > 0 else 0.0


def dedupe_episode(records: list[dict], segs: list,
                   dedupe_log: list | None = None) -> list[dict]:
    """Merge same-brand candidates whose windows overlap (fix #3).

    Overlap = IoU > 0.3, or one window contains the other's anchor start.
    The earlier candidate is kept and absorbs the dropped one's window,
    anchors, URLs, codes and confidence; the merge is logged."""
    def anchor_times(r):
        return [segs[i].get("start") or 0 for i in r["provenance"]["anchor_seg_idx"]]

    def holds(r, t):
        return (r["start_s"] or 0) <= t <= (r["end_s"] or 0)

    kept: list[dict] = []
    copies: set[int] = set()  # merged winners are copies; raw records stay intact
    for r in sorted(records, key=lambda x: x["start_s"] or 0):
        winner, iou = None, 0.0
        if r["brand_spoken"]:
            for k in kept:
                if k["brand_spoken"] != r["brand_spoken"]:
                    continue
                iou = window_iou(k, r)
                if (iou > 0.3 or any(holds(k, t) for t in anchor_times(r))
                        or any(holds(r, t) for t in anchor_times(k))):
                    winner = k
                    break
        if winner is None:
            kept.append(r)
            continue
        if id(winner) not in copies:
            merged = dict(winner, provenance=dict(winner["provenance"]))
            kept[kept.index(winner)] = merged
            copies.add(id(merged))
            winner = merged
        winner["start_s"] = min(winner["start_s"] or 0, r["start_s"] or 0)
        winner["end_s"] = max(winner["end_s"] or 0, r["end_s"] or 0)
        winner["urls"] = sorted(set(winner["urls"]) | set(r["urls"]))
        winner["offer_codes"] = sorted(set(winner["offer_codes"]) | set(r["offer_codes"]))
        winner["confidence"] = max(winner["confidence"], r["confidence"],
                                   key=CONFIDENCE_RANK.index)
        prov = winner["provenance"]
        prov["anchor_seg_idx"] = sorted(set(prov["anchor_seg_idx"])
                                        | set(r["provenance"]["anchor_seg_idx"]))
        prov["merged_candidates"] = prov.get("merged_candidates", []) + [r["candidate_id"]]
        if dedupe_log is not None:
            dedupe_log.append({"dropped": r["candidate_id"], "kept": winner["candidate_id"],
                               "key": [r["ep_id"], r["brand_spoken"], "overlap"],
                               "iou": round(iou, 3)})
    return kept


def detect_episode(ep_id: str, segments: list, *, ep_meta: dict | None = None,
                   transcript_name: str | None = None,
                   fetch_date: str | None = None,
                   fetch_date_source: str | None = None,
                   cluster_window: int = 6, context_before: int = 2,
                   context_after: int = 25, cta_context_before: int = 10,
                   cta_context_after: int = 8,
                   raw_out: list | None = None,
                   dedupe_log: list | None = None) -> list[dict]:
    """Detect sponsor-read candidates for ONE episode.

    Returns the normalized (post-dedupe) candidates — the same records the
    CLI writes to candidates_normalized.json. Pre-dedupe records are appended
    to ``raw_out`` and dropped-duplicate entries to ``dedupe_log`` when given.
    """
    ep = ep_meta or {}
    segs = segments
    if fetch_date is None:
        fetch_date, fetch_date_source = resolve_fetch_date(None, ep.get("fetch_date"))
    tname = transcript_name or f"{ep_id}.json"
    records: list[dict] = []
    # (segidx, raw, canon, kind, url_evidence_key)
    anchors: list[tuple] = []
    for i, s in enumerate(segs):
        t = s.get("text", "")
        seg_urls = extract_urls(t)
        seg_urlkey = url_evidence_key(seg_urls)
        hit = False
        for rx, kind in SPONSOR_RES:
            raw = raw_brand_from_match(t, rx)
            if raw:
                anchors.append((i, raw, normalize_brand(raw, anchor_context=True),
                                kind, seg_urlkey))
                hit = True
                break
        if hit:
            continue
        if seg_urls:
            # CTA URL anchor: a recognized brand attribution names
            # brand_spoken from the spoken URL evidence; unknown domains
            # stay unnamed.
            anchors.append((i, seg_urls[0], brand_hint_from_urls(seg_urls),
                            "cta_url", seg_urlkey))
        elif FALLBACK_RE.search(t):
            anchors.append((i, None, None, "marker_only", seg_urlkey))
    # Cluster: merge only if close in time AND brand/URL-compatible.
    clusters: list[list] = []
    for anc in anchors:
        if clusters and joins_cluster(clusters[-1], anc, cluster_window, context_after):
            clusters[-1].append(anc)
        else:
            clusters.append([anc])
    for ci, cl in enumerate(clusters):
        named = [x for x in cl if x[2]]
        brand_spoken = named[0][2] if named else None
        anchor_kind = named[0][3] if named else cl[0][3]
        lo, hi = cluster_bounds(clusters, ci, len(segs),
                                context_before=context_before,
                                context_after=context_after,
                                cta_context_before=cta_context_before,
                                cta_context_after=cta_context_after)
        window = segs[lo:hi]
        # URL/code attribution comes only from the cluster's own
        # anchor span (never the excerpt context); the wider window
        # stays as review excerpt.
        attr_text = " ".join(s.get("text", "")
                             for s in segs[cl[0][0]:cl[-1][0] + 1])
        wtext = " ".join(s.get("text", "") for s in window)
        codes = set()
        for crx in CODE_RES:
            codes.update(crx.findall(attr_text))
        urls = extract_urls(attr_text)
        brand_url = brand_hint_from_urls(urls)
        # Only evidence consistent with the spoken brand raises confidence.
        supported = codes or any(url_supports_brand(u, brand_spoken) for u in urls)
        conf = ("high" if (brand_spoken and supported)
                else ("medium" if brand_spoken else "low"))
        records.append({
            "candidate_id": f"{ep_id}.c{ci}",
            "ep_id": ep_id,
            "show_title": ep.get("show_title", "?"),
            "ep_title": ep.get("ep_title", "?"),
            "pub_date": ep.get("pub_date", "?"),
            "fetch_date": fetch_date,
            "brand": brand_spoken or "(unnamed)",   # spoken attribution only
            "brand_spoken": brand_spoken,
            "brand_raw": named[0][1] if named else None,
            "brand_url": brand_url,                # URL/code-derived, separate (fix #4)
            "brand_code": None,
            "anchor": anchor_kind,
            "confidence": conf,
            "offer_codes": sorted(codes),
            "urls": urls,
            "start_s": window[0].get("start"),
            "end_s": window[-1].get("end"),
            "provenance": {
                "anchor_seg_idx": [x[0] for x in cl],
                "cluster_id": f"{ep_id}.cl{ci}",
                "transcript": tname,
                "fetch_date_source": fetch_date_source,
            },
            "excerpt": re.sub(r"\s+", " ", wtext)[:600],
        })
    if raw_out is not None:
        raw_out.extend(records)
    return dedupe_episode(records, segs, dedupe_log)


def main() -> int:
    p = argparse.ArgumentParser(description="Brand-agnostic ad-read detection (hardened)")
    add_common_args(p)
    p.add_argument("--fetch-date", default=None,
                   help="Explicit fetch date override (else EARWITNESS_FETCH_DATE, "
                        "else episode/transcript metadata, else current UTC date)")
    p.add_argument("--cluster-window", type=int, default=6,
                   help="Max segment gap to merge anchors (tightened; orig 12)")
    p.add_argument("--context-before", type=int, default=2)
    p.add_argument("--context-after", type=int, default=25,
                   help="Max segments an opened read runs forward")
    p.add_argument("--cta-context-before", type=int, default=10,
                   help="Segments a CTA-only read runs backward")
    p.add_argument("--cta-context-after", type=int, default=8,
                   help="Max segments a read runs on after its last CTA "
                        "(stops at the next cluster)")
    a = p.parse_args()

    data_root = resolve("data_root", a.data_root)
    troot = resolve("transcript_root", a.transcript_root)
    oroot = resolve("output_root", a.output_root)
    rroot = resolve("runs_root", a.runs_root)
    oroot.mkdir(parents=True, exist_ok=True)
    run_id = a.run_id or RunLogger.new_run_id("detect-all")
    log = RunLogger(rroot, run_id, "detect_all")

    ep_path = data_root / "episodes.json"
    if not ep_path.exists():
        log.failed(record_id="-", error=f"missing episodes file: {ep_path}")
        print(f"ERROR: missing {ep_path}", file=sys.stderr)
        return 2
    try:
        episodes = {e["ep_id"]: e for e in json.loads(ep_path.read_text())}
    except Exception as ex:
        log.failed(record_id="-", error=f"episodes.json parse: {ex}")
        print(f"ERROR: episodes.json parse: {ex}", file=sys.stderr)
        return 2

    allow = parse_episode_filter(a.episodes)
    tfiles = sorted(glob.glob(str(troot / "*.json")))
    if allow:
        tfiles = [t for t in tfiles if os.path.splitext(os.path.basename(t))[0] in allow]
    if a.limit:
        tfiles = tfiles[:a.limit]
    if not tfiles:
        log.failed(record_id="-", error=f"no transcripts in {troot}")
        print(f"ERROR: no transcripts in {troot}", file=sys.stderr)
        return 2

    raw_records: list[dict] = []
    normalized: list[dict] = []
    dedupe_log: list[dict] = []
    for tp in tfiles:
        ep_id = os.path.splitext(os.path.basename(tp))[0]
        try:
            d = json.loads(open(tp).read())
            segs = d["segments"]
            if not isinstance(segs, list) or not all(isinstance(x, dict) for x in segs):
                raise ValueError("segments must be a list of objects")
        except Exception as ex:
            log.failed(record_id=ep_id, inputs={"transcript": tp}, error=f"parse: {ex}")
            continue
        ep = episodes.get(ep_id, {})
        if not ep:  # benchmark transcripts carry show info inline, no episodes.json entry
            ep = {"show_title": d.get("show", "?"), "ep_title": d.get("ep_title", "?"),
                  "pub_date": d.get("pub_date", "?"), "fetch_date": d.get("fetch_date", "?")}
        fetch_date, fetch_src = resolve_fetch_date(
            a.fetch_date, ep.get("fetch_date"), d.get("fetch_date"))
        n_before = len(raw_records)
        normalized.extend(detect_episode(
            ep_id, segs, ep_meta=ep, transcript_name=os.path.basename(tp),
            fetch_date=fetch_date, fetch_date_source=fetch_src,
            cluster_window=a.cluster_window, context_before=a.context_before,
            context_after=a.context_after,
            cta_context_before=a.cta_context_before,
            cta_context_after=a.cta_context_after, raw_out=raw_records,
            dedupe_log=dedupe_log))
        log.ok(record_id=ep_id, inputs={"transcript": tp},
               outputs={"clusters": len(raw_records) - n_before},
               extra={"fetch_date": fetch_date, "fetch_date_source": fetch_src})

    normalized.sort(key=lambda x: (x["ep_id"], x["start_s"] or 0))

    (oroot / "candidates_raw.json").write_text(json.dumps(raw_records, indent=1))
    (oroot / "candidates_normalized.json").write_text(json.dumps(normalized, indent=1))
    (oroot / "dedupe_log.json").write_text(json.dumps(dedupe_log, indent=1))
    log.ok(record_id="summary",
           outputs={"raw": str(oroot / "candidates_raw.json"),
                    "normalized": str(oroot / "candidates_normalized.json")},
           extra={"raw_count": len(raw_records), "normalized_count": len(normalized),
                  "dropped": len(dedupe_log)})
    print(f"DONE: {len(raw_records)} raw candidates -> {len(normalized)} normalized "
          f"({len(dedupe_log)} deduped) in {len({r['ep_id'] for r in normalized})} episodes")
    for r in normalized:
        extra = []
        if r["offer_codes"]:
            extra.append("code " + "/".join(r["offer_codes"][:2]))
        if r["urls"]:
            extra.append(r["urls"][0])
        url_note = f" [url-brand: {r['brand_url']}]" if r["brand_url"] and r["brand_url"] != r["brand_spoken"] else ""
        print(f"- {r['brand']} on {r['show_title']} [{r['confidence']}] {' | '.join(extra)}{url_note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
