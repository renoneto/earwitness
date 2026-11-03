#!/usr/bin/env python3
"""Phase 1b: verify candidate feeds parse and have episodes within last 60 days -> shows.csv
Threaded for speed; early-stops at TARGET verified shows."""
import argparse, csv, json, os, socket, threading, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import feedparser

socket.setdefaulttimeout(25)
CUTOFF = datetime.now(timezone.utc) - timedelta(days=60)
UA = "PodcastValidation/1.0 (research project)"
TARGET, WORKERS = 150, 6

def newest_date(feed_url):
    try:
        d = feedparser.parse(feed_url, agent=UA,
                             request_headers={"User-Agent": UA})
    except Exception as ex:
        return None, f"parse-error: {str(ex)[:60]}"
    if d.get("bozo") and not d.get("entries"):
        return None, f"bozo: {str(d.get('bozo_exception'))[:60]}"
    if not d.get("entries"):
        return None, "no-entries"
    newest = None
    for e in d["entries"]:
        dt = None
        for key in ("published_parsed", "updated_parsed"):
            if e.get(key):
                try:
                    dt = datetime(*e[key][:6], tzinfo=timezone.utc); break
                except Exception:
                    pass
        if dt is None and e.get("published"):
            try:
                dt = parsedate_to_datetime(e["published"])
                if dt and dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except Exception:
                pass
        if dt and (newest is None or dt > newest):
            newest = dt
    return (newest, None) if newest else (None, "no-dates")

def main():
    ap = argparse.ArgumentParser(description="Feed verification (reference, hardened paths)")
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--target", type=int, default=None)
    ns = ap.parse_args()
    base = ns.data_root or os.environ.get("EARWITNESS_DATA_ROOT") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    global TARGET
    if ns.target:
        TARGET = ns.target
    cands = json.load(open(os.path.join(base, "candidates.json")))
    items = list(cands.items())
    rows, skipped, lock = [], [], threading.Lock()
    checked = [0]

    def check(item):
        feed, meta = item
        with lock:
            if len(rows) >= TARGET:
                return
        newest, err = newest_date(feed)
        with lock:
            checked[0] += 1
            n = checked[0]
            if err:
                skipped.append((feed, meta["title"], err))
            elif newest < CUTOFF:
                skipped.append((feed, meta["title"], f"stale ({newest.date()})"))
            elif len(rows) < TARGET:
                rows.append({"title": meta["title"], "feed_url": feed,
                             "category": meta["category"],
                             "latest_episode_date": newest.date().isoformat()})
            if n % 50 == 0:
                print(f"checked {n}/{len(items)} -> {len(rows)} active", flush=True)
        time.sleep(0.15)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(check, items))
    rows.sort(key=lambda r: r["latest_episode_date"], reverse=True)
    rows = rows[:TARGET]
    with open(os.path.join(base, "shows.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["title", "feed_url", "category", "latest_episode_date"])
        w.writeheader(); w.writerows(rows)
    with open(os.path.join(base, "skipped_feeds.log"), "w") as f:
        for feed, title, why in skipped:
            f.write(f"{why}\t{title}\t{feed}\n")
    print(f"DONE: {len(rows)} active shows -> shows.csv, {len(skipped)} skipped, {checked[0]} checked")

if __name__ == "__main__":
    main()
