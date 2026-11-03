#!/usr/bin/env python3
"""Scaffold per-brand review-pack markdown from verifications.json.
Agent-judged sections (checks 3/4, quality notes) are filled in by hand."""
import argparse, json, os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("EARWITNESS_DATA_ROOT", os.path.join(REPO, "data"))
CHECK_NAMES = {
    "1_aired": "Ad aired", "2_code_url": "Offer code / URL correct",
    "3_required_claims": "Required claims present", "4_prohibited_claims": "Prohibited claims absent",
    "5_length": "Read length in range", "6_placement": "Placement correct",
    "7_competitor_adjacent": "No competitor adjacent", "8_pronunciation": "Pronunciation",
    "9_suitability": "Suitability",
}
SYM = {"pass": "✅", "fail": "❌", "uncertain": "❓"}

def main():
    ap = argparse.ArgumentParser(description="Review-pack scaffold (reference, hardened paths)")
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--input", default=None, help="verifications.json path")
    ap.add_argument("--out-dir", default=None, help="review-pack output directory")
    ns = ap.parse_args()
    base = ns.data_root or BASE
    ver_path = ns.input or os.path.join(base, "verifications.json")
    try:
        vs = json.load(open(ver_path))
    except Exception as ex:
        print(f"ERROR: verifications parse ({ver_path}): {ex}")
        raise SystemExit(2)
    briefs = json.load(open(os.path.join(base, "briefs.json")))
    outdir = ns.out_dir or os.path.join(os.path.dirname(base) if os.path.basename(base) == "data" else base, "review-pack")
    os.makedirs(outdir, exist_ok=True)
    by_brand = {}
    for v in vs:
        by_brand.setdefault(v["brand"], []).append(v)
    for brand, items in by_brand.items():
        b = briefs[brand]
        L = [f"# EarWitness validation — {b['brand']}", "",
             "> Synthetic brief (invented for validation — NOT a real brand brief).",
             f"> Reads detected: {len(items)}", ""]
        for v in items:
            L += [f"## {v['show_title']} — {v['ep_title'][:80]}",
                  f"- Episode: {v['ep_id']} | pub {v['pub_date']} | fetched {v['fetch_date']} | overall: **{v['overall']}**",
                  f"- Detected: {v['read_confidence']} confidence, {v['read_type']}",
                  f"- Codes: {v['offer_codes']} | URLs: {v['urls']}", "",
                  "**Checklist**", ""]
            for k in sorted(v["checks"], key=lambda x: int(x[0])):
                c = v["checks"][k]
                L.append(f"- {SYM[c['status']]} **{CHECK_NAMES[k]}** — {c['status']} "
                         f"(conf {c['confidence']:.2f}): {c['evidence']}")
            L += ["", "**Excerpt**", "", f"> {v['excerpt'][:1200]}", "",
                  "**Agent notes (checks 3/4 semantic judgment + quality)**", "",
                  "<!-- FILL BY HAND -->", ""]
        L += ["---", "## What the pipeline could NOT answer (for this brand)", "",
              "<!-- FILL BY HAND -->", ""]
        open(os.path.join(outdir, f"{brand}.md"), "w").write("\n".join(L))
        print(f"wrote {os.path.join(outdir, f'{brand}.md')} ({len(items)} reads)")
    # assessment scaffold
    open(os.path.join(outdir, "ASSESSMENT.md"), "w").write(
        "# EarWitness pipeline assessment (validation run)\n\n"
        "<!-- FILL BY HAND: detection precision, extraction accuracy,\n"
        "which checklist items are answerable, what must change -->\n")

if __name__ == "__main__":
    main()
