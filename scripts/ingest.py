#!/usr/bin/env python3
"""Phase 3: download 2 most recent episodes per verified show -> episodes.json + audio/"""
import csv, json, os, time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser
import requests

import argparse as _ap, sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from earwitness.config import resolve as _resolve

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("EARWITNESS_DATA_ROOT", os.path.join(REPO, "data"))
UA = "PodcastValidation/1.0 (research project)"
N_PER_SHOW = int(os.environ.get("EARWITNESS_N_PER_SHOW", "2"))
MAX_SHOWS = int(os.environ.get("EARWITNESS_MAX_SHOWS", "40"))  # validation corpus cap; raise for full run

def ep_datetime(e):
    for key in ("published_parsed", "updated_parsed"):
        if e.get(key):
            try:
                return datetime(*e[key][:6], tzinfo=timezone.utc)
            except Exception:
                pass
    if e.get("published"):
        try:
            dt = parsedate_to_datetime(e["published"])
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return None

def audio_url(e):
    for link in e.get("enclosures", []) or []:
        href = link.get("href", "")
        typ = link.get("type", "")
        if href and ("audio" in typ or href.endswith((".mp3", ".m4a", ".aac", ".ogg", ".wav"))):
            return href
    for link in e.get("links", []) or []:
        href = link.get("href", "")
        if href and href.endswith((".mp3", ".m4a", ".aac")):
            return href
    return None

def main():
    ap = _ap.ArgumentParser(description="Episode ingest (reference, hardened paths)")
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--audio-root", default=None)
    ap.add_argument("--max-shows", type=int, default=None)
    ap.add_argument("--n-per-show", type=int, default=None)
    ns = ap.parse_args()
    base = str(_resolve("data_root", ns.data_root))
    aud = str(_resolve("audio_root", ns.audio_root))
    max_shows = ns.max_shows or MAX_SHOWS
    n_per_show = ns.n_per_show or N_PER_SHOW
    os.makedirs(aud, exist_ok=True)
    shows = list(csv.DictReader(open(os.path.join(base, "shows.csv"))))[:max_shows]
    episodes, fails = [], []
    fetch_date = datetime.now(timezone.utc).date().isoformat()
    for si, s in enumerate(shows):
        try:
            d = feedparser.parse(s["feed_url"], agent=UA,
                                 request_headers={"User-Agent": UA})
        except Exception as ex:
            fails.append((s["title"], f"feed-parse: {ex}")); continue
        entries = [e for e in d.get("entries", []) if audio_url(e)]
        entries.sort(key=lambda e: ep_datetime(e) or datetime.min.replace(tzinfo=timezone.utc),
                     reverse=True)
        got = 0
        for e in entries[:n_per_show]:
            url = audio_url(e)
            ep_id = f"s{si:03d}e{got}"
            dest = os.path.join(aud, f"{ep_id}.mp3")
            try:
                r = requests.get(url, headers={"User-Agent": UA}, stream=True, timeout=60)
                r.raise_for_status()
                size = 0
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk); size += len(chunk)
                episodes.append({
                    "ep_id": ep_id, "show_title": s["title"], "category": s["category"],
                    "feed_url": s["feed_url"], "ep_title": e.get("title", ""),
                    "guid": e.get("id", e.get("guid", "")),
                    "pub_date": (ep_datetime(e).date().isoformat() if ep_datetime(e) else ""),
                    "audio_url": url, "local_path": dest, "bytes": size,
                    "fetch_date": fetch_date,  # DAI caveat: ad heard = as of this date
                })
                got += 1
            except Exception as ex:
                fails.append((s["title"], f"download: {url[:80]}: {ex}"))
            time.sleep(0.5)
        if (si + 1) % 25 == 0:
            print(f"ingested {si+1}/{len(shows)} shows -> {len(episodes)} episodes", flush=True)
        if (si + 1) % 10 == 0:
            json.dump(episodes, open(os.path.join(base, "episodes.json"), "w"), indent=1)
        time.sleep(0.3)
    json.dump(episodes, open(os.path.join(base, "episodes.json"), "w"), indent=1)
    with open(os.path.join(base, "ingest_fails.log"), "w") as f:
        for t, w in fails:
            f.write(f"{t}\t{w}\n")
    print(f"DONE: {len(episodes)} episodes, {len(fails)} failures")

if __name__ == "__main__":
    main()
