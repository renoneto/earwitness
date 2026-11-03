#!/usr/bin/env python3
"""Phase 4: stage-1 ad-read detection over transcripts -> ad_reads.json
Signals: brand aliases, offer-code/URL patterns, ad-read linguistic markers.
Confidence heuristic: brand+code+marker=high; brand+marker or brand+code=medium; marker-only=low."""
import argparse
import json, os, re, difflib, sys, glob

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from earwitness.config import resolve as _resolve

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("EARWITNESS_DATA_ROOT", os.path.join(REPO, "data"))
TDIR_DEFAULT = os.environ.get("EARWITNESS_TRANSCRIPT_ROOT", os.path.join(REPO, "transcripts"))

BRANDS = {
    "betterhelp": ["betterhelp", "better help"],
    "mintmobile": ["mint mobile", "mintmobile"],
    "shopify": ["shopify"],
    "ag1": ["ag1", "athletic greens", "athleticgreens", "drinkag1"],
    "hellofresh": ["hellofresh", "hello fresh"],
}

AD_MARKERS = [
    r"brought to you by", r"sponsored by", r"this episode is sponsored",
    r"today'?s sponsor", r"our sponsor", r"thanks to .* for sponsoring",
    r"use code", r"use the code", r"go to [\w.-]+\.\w+", r"visit [\w.-]+\.\w+",
    r"for \d+% off", r"for \$1", r"free (?:month|meals|trial)",
    r"head to [\w.-]+\.\w+", r"check out [\w.-]+\.\w+",
]

CODE_RES = [
    re.compile(r"use (?:the )?code ([A-Za-z0-9]{3,20})", re.I),
    re.compile(r"code ([A-Za-z0-9]{3,20}) (?:for|at|to get)", re.I),
]
URL_RE = re.compile(r"(?:go to|visit|head to|head over to|check out|at)\s+((?:[\w-]+\.)+(?:com|net|org|io|co|fm)(?:(?:/| slash |/)[\w\-%.~]*)?)", re.I)

def normalize_spoken_url(url):
    """Convert 'shopify.com slash unsolved' -> 'shopify.com/unsolved'."""
    url = re.sub(r"\s+slash\s+", "/", url, flags=re.I)
    url = re.sub(r"\s+dot\s+", ".", url, flags=re.I)
    return url.strip("., ")

def fuzzy_alias_hit(low, aliases):
    """Exact substring hits plus fuzzy fallback for STT-mangled brand names."""
    hits = [a for a in aliases if a in low]
    if hits:
        return hits
    words = set(re.findall(r"[a-z]{4,}", low))
    for a in aliases:
        if any(c in a for c in " ./"):  # only single tokens for fuzzy
            continue
        for w in words:
            if abs(len(w) - len(a)) <= 2 and difflib.SequenceMatcher(None, w, a).ratio() >= 0.8:
                return [f"{a}~{w}"]
    return []

def main():
    ap = argparse.ArgumentParser(description="Brand-specific detector (reference, hardened paths)")
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--transcript-root", default=None)
    ap.add_argument("--out", default=None, help="Output JSON path")
    ap.add_argument("--limit", type=int, default=None)
    ns = ap.parse_args()
    base = str(_resolve("data_root", ns.data_root))
    tdir = str(_resolve("transcript_root", ns.transcript_root))
    episodes = {e["ep_id"]: e for e in json.load(open(os.path.join(base, "episodes.json")))}
    if ns.limit:
        episodes = dict(list(episodes.items())[:ns.limit])
    reads = []
    for ep_id, ep in episodes.items():
        tp = f"{tdir}/{ep_id}.json"
        if not os.path.exists(tp):
            continue
        segs = json.load(open(tp))["segments"]
        full = " ".join(s["text"] for s in segs)
        low = full.lower()
        for brand, aliases in BRANDS.items():
            hits = fuzzy_alias_hit(low, aliases)
            if not hits:
                continue
            # locate brand-mention segments, expand with context
            hit_strs = [h.split("~")[0] for h in hits] + [h.split("~")[-1] for h in hits if "~" in h]
            idxs = [i for i, s in enumerate(segs)
                    if any(a in s["text"].lower() for a in hit_strs)]
            # merge nearby idxs into clusters
            clusters, cur = [], []
            for ix in idxs:
                if cur and ix - cur[-1] <= 6:
                    cur.append(ix)
                else:
                    if cur: clusters.append(cur)
                    cur = [ix]
            if cur: clusters.append(cur)
            for cl in clusters:
                lo, hi = max(0, cl[0]-3), min(len(segs), cl[-1]+4)
                window = segs[lo:hi]
                wtext = " ".join(s["text"] for s in window)
                markers = [m for m in AD_MARKERS if re.search(m, wtext, re.I)]
                codes = set()
                for crx in CODE_RES:
                    codes.update(crx.findall(wtext))
                urls = set(normalize_spoken_url(u) for u in URL_RE.findall(wtext))
                # classify read type
                rtext = wtext.lower()
                read_type = "host-read" if re.search(
                    r"\bi\b|\bwe\b|my |our |i'?ve been", rtext) and markers else (
                    "produced" if markers else "uncertain")
                score = sum([bool(hits), bool(codes or urls), bool(markers)])
                conf = "high" if score == 3 else ("medium" if score == 2 else "low")
                reads.append({
                    "ep_id": ep_id, "show_title": ep["show_title"],
                    "ep_title": ep["ep_title"], "pub_date": ep["pub_date"],
                    "fetch_date": ep["fetch_date"], "brand": brand,
                    "matched_alias": hits[0], "read_type": read_type,
                    "confidence": conf, "offer_codes": sorted(codes),
                    "urls": sorted(urls), "markers": markers[:5],
                    "start_s": window[0]["start"], "end_s": window[-1]["end"],
                    "excerpt": wtext[:1500],
                })
    # dedupe identical brand+episode+start
    seen, uniq = set(), []
    for r in reads:
        k = (r["ep_id"], r["brand"], round(r["start_s"]))
        if k not in seen:
            seen.add(k); uniq.append(r)
    out_path = ns.out or os.path.join(base, "ad_reads.json")
    json.dump(uniq, open(out_path, "w"), indent=1)
    print(f"wrote {out_path}")
    by_brand = {}
    for r in uniq:
        by_brand[r["brand"]] = by_brand.get(r["brand"], 0) + 1
    print(f"DONE: {len(uniq)} candidate reads across {len(set(r['ep_id'] for r in uniq))} episodes")
    print("by brand:", by_brand)
    print("by confidence:", {c: sum(1 for r in uniq if r["confidence"]==c) for c in ("high","medium","low")})

if __name__ == "__main__":
    main()
