#!/usr/bin/env python3
"""Build the versioned advertiser list (earwitness/pipeline/advertisers.py).

Sources, most trusted first:
  --corrections FILE   an owner corrections export (earwitness-corrections/2):
                       new_brands and brand_aliases
  --notes FILE         show-notes sponsor listings ([{name, brand, domains}],
                       e.g. harvested with earwitness/pipeline/shownotes.py);
                       only names the notes write out, 1-4 words
  --seed FILE          curated seed: one "Name | alias | alias" per line,
                       '#' comments (earwitness/data/advertisers-seed.txt)
  --canon FILE         a corpus brand-canon.json: gazetteer canonicals, and
                       forms of 5+ letters spoken as a domain on >=
                       --canon-min-domain-shows shows and named on >= 5

  build_advertisers.py --seed S [--notes N] [--corrections C] [--canon K]
                       --version V --out earwitness/data/advertisers-vV.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from earwitness.pipeline import advertisers as adv  # noqa: E402


def seed_items(path) -> list:
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|") if p.strip()]
        out.append({"name": parts[0], "aliases": parts[1:]})
    return out


NOT_A_SPONSOR = re.compile(r"inquir|advertis|privacy|sponsor|podcast|episode|subscri", re.I)


def notes_items(path, words=frozenset()) -> list:
    """Written sponsor names, 1-3 words. With domains, one must carry the
    name ("drinkag1" for AG1) or the listing is prose ("Meta multi-decade
    study" -> worldlypartners); without, at most 2 words. A domain that
    contains the name becomes an alias. A one-word dictionary name
    ("Read", "Government") needs listings on 2+ feeds. Each item carries
    "feeds", the number of feeds listing it."""
    listings = json.loads(Path(path).read_text(encoding="utf-8"))
    feeds: dict = {}
    for s in listings:
        feeds.setdefault(adv.key(s.get("name") or ""), set()).add(s.get("feed_url"))
    out = []
    for s in listings:
        name = re.sub(r"[\s®™.,;:!-]+$", "", (s.get("name") or "").strip())
        k = adv.key(name)
        if not name or not 1 <= len(name.split()) <= 3 or len(k) < 3:
            continue
        if re.search(r"https?:|www\.|@", name) or NOT_A_SPONSOR.search(name):
            continue
        nf = len(feeds.get(adv.key(s.get("name") or ""), ()))
        if len(name.split()) == 1 and name.lower() in words and nf < 2:
            continue
        doms = [adv.key(d) for d in s.get("domains") or () if adv.key(d)]
        first = adv.key(name.split()[0])
        if doms and not any(k in d or d in k or (len(first) >= 4 and first in d)
                            for d in doms):
            continue
        if not doms and len(name.split()) > 2:
            continue
        out.append({"name": name, "aliases": [d for d in doms if d != k and k in d][:3],
                    "feeds": nf})
    return out


def corrections_items(path) -> list:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    out = [{"name": b.get("display") or b.get("brand"), "aliases": [b.get("brand")]}
           for b in doc.get("new_brands") or ()]
    for a in doc.get("brand_aliases") or ():
        out.append({"name": a.get("display") or a.get("canonical"),
                    "aliases": [a.get("canonical"), a.get("raw")]})
    return out


def canon_items(path, min_domain_shows: int, min_name_shows: int = 5) -> list:
    """Canonical forms with strong support: a gazetteer canonical, or 5+
    letters spoken as a domain on >= min_domain_shows shows and named on
    >= min_name_shows shows. Short or thinly named forms are too often a
    recurring mishearing ("zock", "rins", "usay")."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if "names" in doc:  # a committed anchors extract (canon_anchors_extract)
        return [{"name": n} for n in doc["names"]]
    out = []
    for form, m in (doc.get("map") or {}).items():
        if m.get("reason") != "canonical":
            continue
        sup = m.get("canonical_support") or m.get("support") or {}
        name = m.get("canonical") or form
        if sup.get("gazetteer") or (
                len(adv.key(name)) >= 5
                and (sup.get("domain_shows") or 0) >= min_domain_shows
                and (sup.get("name_shows") or 0) >= min_name_shows):
            out.append({"name": name})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seed", required=True)
    ap.add_argument("--notes")
    ap.add_argument("--corrections")
    ap.add_argument("--canon")
    ap.add_argument("--canon-min-domain-shows", type=int, default=3)
    ap.add_argument("--version", type=int)
    ap.add_argument("--out")
    ap.add_argument("--built-at", default=None, help="fixed timestamp (reproducible builds)")
    ap.add_argument("--extract-canon-to",
                    help="write the canon anchors of --canon (a brand-canon.json) as a small "
                         "committed input, and stop")
    a = ap.parse_args(argv)
    if a.extract_canon_to:
        import hashlib
        names = [x["name"] for x in canon_items(a.canon, a.canon_min_domain_shows)]
        Path(a.extract_canon_to).write_text(json.dumps({
            "source": Path(a.canon).name,
            "sha256": hashlib.sha256(Path(a.canon).read_bytes()).hexdigest(),
            "min_domain_shows": a.canon_min_domain_shows, "min_name_shows": 5,
            "names": sorted(set(names))}, indent=1) + "\n", encoding="utf-8")
        print(json.dumps({"out": a.extract_canon_to, "names": len(set(names))}))
        return 0
    if a.version is None or not a.out:
        ap.error("--version and --out are needed to build")
    from earwitness.pipeline import brand_namer as bn
    words = bn._load_words()
    sources = {"seed": seed_items(a.seed)}
    if a.notes:
        sources["notes"] = notes_items(a.notes, words)
    if a.corrections:
        sources["corrections"] = corrections_items(a.corrections)
    if a.canon:
        sources["canon"] = canon_items(a.canon, a.canon_min_domain_shows)
    doc = adv.build(sources, built_at=a.built_at or datetime.now(timezone.utc).isoformat(
                        timespec="seconds"),
                    version=a.version, words=words)
    Path(a.out).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"out": a.out, "entries": len(doc["entries"]), "sha256": doc["sha256"],
                      "sources": doc["sources"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
