#!/usr/bin/env python3
"""Build the versioned show-title gazetteer (earwitness/pipeline/promos.py).

  build_show_titles.py --lists shows-3000.json shows-main-20261011.json ...
                       [--identity <corpus>/show-identity ...]
                       --extract-to earwitness/data/inputs/show-sources-vV.json
  build_show_titles.py --sources earwitness/data/inputs/show-sources-vV.json
                       --version V --out earwitness/data/show-titles-vV.json

The first form writes the committed input (every show's title, feed URL and
publishers, with each source file's sha256); the second builds the
gazetteer from it, so the gazetteer rebuilds from the repo alone.

--lists: show lists ({"shows": [{"title", "feed_url"}], "dropped": [{"title"}]}):
every show and every dropped chart title. --identity: show-identity caches
(self_promo.py): the feed's title and its publishers (authors, owners) as
networks. Each input file's sha256 is recorded under "sources", so the same
inputs give the same gazetteer digest.
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

from earwitness.pipeline import promos  # noqa: E402


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def list_shows(path) -> list:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    shows = doc.get("shows") if isinstance(doc, dict) else doc
    out = [{"title": s.get("title"), "feed_url": s.get("feed_url")}
           for s in shows or () if isinstance(s, dict) and s.get("title")]
    if isinstance(doc, dict):
        out += [{"title": s.get("title")} for s in doc.get("dropped") or ()
                if isinstance(s, dict) and s.get("title")]
    return out


def identity_shows(cache_dir) -> list:
    out = []
    for p in sorted(Path(cache_dir).glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("status") == "ok" and d.get("title"):
            out.append({"title": d["title"], "feed_url": d.get("feed_url"),
                        "networks": list(d.get("authors") or ()) + list(d.get("owners") or ())})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--lists", nargs="+", default=[])
    ap.add_argument("--identity", nargs="*", default=[])
    ap.add_argument("--sources", help="a committed input written by --extract-to")
    ap.add_argument("--extract-to", help="write the input (shows + source digests) and stop")
    ap.add_argument("--version", type=int)
    ap.add_argument("--out")
    ap.add_argument("--built-at", default=None, help="fixed timestamp (reproducible builds)")
    a = ap.parse_args(argv)
    shows, sources = [], {}
    if a.sources:
        src = json.loads(Path(a.sources).read_text(encoding="utf-8"))
        shows, sources = src["shows"], dict(src["sources"])
        sources["input"] = _sha(Path(a.sources))
    for p in a.lists:
        sources[Path(p).name] = _sha(Path(p))
        shows += list_shows(p)
    for d in a.identity:
        files = sorted(Path(d).glob("*.json"))
        h = hashlib.sha256(b"".join(f.name.encode() + _sha(f).encode() for f in files))
        sources[f"identity:{Path(d).resolve().parent.name}/{Path(d).name}"] = h.hexdigest()
        shows += identity_shows(d)
    if a.extract_to:
        Path(a.extract_to).write_text(json.dumps({"sources": sources, "shows": shows},
                                                 indent=0, ensure_ascii=False) + "\n",
                                      encoding="utf-8")
        print(json.dumps({"out": a.extract_to, "shows": len(shows)}))
        return 0
    if a.version is None or not a.out:
        ap.error("--version and --out are needed to build")
    doc = promos.build(shows, built_at=a.built_at or datetime.now(timezone.utc).isoformat(
        timespec="seconds"), version=a.version, sources=sources)
    Path(a.out).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"out": a.out, "entries": len(doc["entries"]), "sha256": doc["sha256"],
                      "shows_in": len(shows)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
