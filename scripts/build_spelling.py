#!/usr/bin/env python3
"""Build the versioned display-spelling map (earwitness/pipeline/spelling.py).

  build_spelling.py --advertisers earwitness/data/advertisers-v1.json
                    [--notes notes-listings.json] [--canon brand-canon.json]
                    --version V --out earwitness/data/spelling-vV.json

Tiers, most trusted first: the advertiser list's names (its canon-tier
entries go to the canon tier), show-notes sponsor names as written
(build_advertisers.notes_items), and canon anchors (build_advertisers.
canon_items: gazetteer canonicals, or forms of 5+ letters spoken as a domain
on 3+ shows and named on 5+). A canon form is kept only when a raw notes
listing or the seed (--seed) writes it exactly so. Input digests are
recorded under "sources".
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_advertisers as ba  # noqa: E402
from earwitness.pipeline import brand_namer as bn  # noqa: E402
from earwitness.pipeline import spelling  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--advertisers", required=True)
    ap.add_argument("--notes")
    ap.add_argument("--canon")
    ap.add_argument("--seed", help="the advertiser seed: a written source for canon forms")
    ap.add_argument("--version", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--built-at", default=None, help="fixed timestamp (reproducible builds)")
    a = ap.parse_args(argv)
    words = bn._load_words()
    adv = json.loads(Path(a.advertisers).read_text(encoding="utf-8"))
    sources = {"advertisers": [e["name"] for e in adv["entries"] if e["tier"] != "canon"],
               "notes": [], "canon": [e["name"] for e in adv["entries"] if e["tier"] == "canon"]}
    inputs = {"advertisers": adv["sha256"]}
    if a.notes:
        # a notes spelling the list keeps as a typo alias ("betterhel") is no display
        aliases = {k for e in adv["entries"] for k in e.get("aliases") or ()}
        sources["notes"] = [x["name"] for x in ba.notes_items(a.notes, words)
                            if spelling.key(x["name"]) not in aliases]
        inputs["notes"] = hashlib.sha256(Path(a.notes).read_bytes()).hexdigest()
    if a.canon:
        sources["canon"] += [x["name"] for x in ba.canon_items(a.canon, 3)]
        inputs["canon"] = hashlib.sha256(Path(a.canon).read_bytes()).hexdigest()
    # a canon form is a heard spelling: kept only when a source writes it so
    # (a raw show-notes listing name or a seed name), never "simply safe"
    written = set()
    if a.notes:
        written |= {str(x.get("name") or "").strip().lower()
                    for x in json.loads(Path(a.notes).read_text(encoding="utf-8"))}
    if a.seed:
        written |= {x["name"].strip().lower() for x in ba.seed_items(a.seed)}
        inputs["seed"] = hashlib.sha256(Path(a.seed).read_bytes()).hexdigest()
    dropped = [n for n in sources["canon"] if n.strip().lower() not in written]
    sources["canon"] = [n for n in sources["canon"] if n.strip().lower() in written]
    doc = spelling.build(sources, built_at=a.built_at or datetime.now(timezone.utc).isoformat(
        timespec="seconds"), version=a.version, inputs=inputs, words=words)
    Path(a.out).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"out": a.out, "entries": len(doc["entries"]), "sha256": doc["sha256"],
                      "canon_not_written": sorted(set(dropped)),
                      "by_tier": {t: sum(e["tier"] == t for e in doc["entries"])
                                  for t in spelling.TIERS}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
