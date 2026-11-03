#!/usr/bin/env python3
"""Phase 1: build list of 100-150 active ad-heavy podcasts from Apple charts + iTunes search."""
import argparse, csv, json, os, time, sys
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.parse import urlencode

UA = {"User-Agent": "PodcastValidation/1.0 (research project; contact via workspace)"}
GENRES = {
    "comedy": "1303",
    "business": "1321",
    "truecrime": "1488",
    "health": "1512",
    "sports": "1545",
}
SEARCH_TERMS = {
    "comedy": "comedy podcast",
    "business": "marketing business podcast",
    "truecrime": "true crime podcast",
    "health": "fitness health podcast",
    "sports": "sports podcast",
    "finance": "investing finance podcast",
}

def get(url, timeout=25):
    req = Request(url, headers=UA)
    with urlopen(req, timeout=timeout) as r:
        return r.read()

def chart_ids(genre_id, limit=100):
    url = f"https://itunes.apple.com/us/rss/toppodcasts/limit={limit}/genre={genre_id}/json"
    data = json.loads(get(url))
    ids = []
    for e in data["feed"].get("entry", []):
        try:
            ids.append(e["id"]["attributes"]["im:id"])
        except KeyError:
            pass
    return ids

def lookup_feeds(ids):
    """Batch lookup -> {collectionId: (title, feedUrl)}"""
    out = {}
    for i in range(0, len(ids), 150):
        chunk = ids[i:i+150]
        url = "https://itunes.apple.com/lookup?" + urlencode({"id": ",".join(chunk)})
        try:
            data = json.loads(get(url))
        except Exception as ex:
            print(f"lookup failed: {ex}", file=sys.stderr)
            continue
        for r in data.get("results", []):
            if r.get("wrapperType") == "collection" and r.get("feedUrl"):
                out[str(r["collectionId"])] = (r.get("collectionName", ""), r["feedUrl"])
        time.sleep(0.5)
    return out

def search_feeds(term, limit=200):
    url = "https://itunes.apple.com/search?" + urlencode(
        {"term": term, "entity": "podcast", "limit": limit, "country": "US"})
    try:
        data = json.loads(get(url))
    except Exception as ex:
        print(f"search failed for {term}: {ex}", file=sys.stderr)
        return {}
    out = {}
    for r in data.get("results", []):
        if r.get("feedUrl"):
            out[str(r.get("collectionId", r.get("trackId")))] = (
                r.get("collectionName", ""), r["feedUrl"])
    return out

def main():
    shows = {}  # feed_url -> dict(title, category)
    # 1. charts
    for cat, gid in GENRES.items():
        try:
            ids = chart_ids(gid)
            print(f"chart {cat}: {len(ids)} ids")
        except Exception as ex:
            print(f"chart {cat} failed: {ex}", file=sys.stderr)
            continue
        feeds = lookup_feeds(ids)
        print(f"chart {cat}: {len(feeds)} feeds resolved")
        for cid, (title, feed) in feeds.items():
            if feed not in shows:
                shows[feed] = {"title": title, "category": cat}
        time.sleep(1)
    # 2. search supplement
    for cat, term in SEARCH_TERMS.items():
        feeds = search_feeds(term)
        added = 0
        for cid, (title, feed) in feeds.items():
            if feed not in shows:
                shows[feed] = {"title": title, "category": cat}
                added += 1
        print(f"search {cat}: +{added}")
        time.sleep(1)
    print(f"TOTAL candidate feeds: {len(shows)}")
    ap = argparse.ArgumentParser(description="Feed discovery (reference, hardened paths)")
    ap.add_argument("--data-root", default=None)
    out = ap.parse_args().data_root or os.environ.get("EARWITNESS_DATA_ROOT") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    dest = os.path.join(out, "candidates.json")
    json.dump(shows, open(dest, "w"), indent=1)
    print(f"wrote {dest}")

if __name__ == "__main__":
    main()
