#!/usr/bin/env python3
"""Benchmark: download 5 episodes, transcribe with faster-whisper small/CPU.
Reports audio-min per wall-hour + realtime factor. Usage: benchmark.py [--model small]"""
import csv, os, sys, time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser
import requests

# httpx (via huggingface_hub) chokes on IPv6 entries in no_proxy; drop them.
for _v in ("no_proxy", "NO_PROXY"):
    os.environ.pop(_v, None)

import argparse as _ap
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("EARWITNESS_DATA_ROOT", os.path.join(REPO, "data"))
UA = "PodcastValidation/1.0 (research project)"
N = int(os.environ.get("EARWITNESS_BENCH_N", "5"))

def ep_dt(e):
    for k in ("published_parsed", "updated_parsed"):
        if e.get(k):
            try: return datetime(*e[k][:6], tzinfo=timezone.utc)
            except Exception: pass
    return datetime.min.replace(tzinfo=timezone.utc)

def audio_url(e):
    for l in e.get("enclosures", []) or []:
        h, t = l.get("href", ""), l.get("type", "")
        if h and ("audio" in t or h.endswith((".mp3", ".m4a", ".aac"))):
            return h
    return None

def main():
    ap = _ap.ArgumentParser(description="Throughput benchmark (reference, hardened paths)")
    ap.add_argument("--model", default="small")
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--audio-root", default=None)
    ap.add_argument("--transcript-root", default=None)
    ap.add_argument("--n", type=int, default=None)
    ns = ap.parse_args()
    model_name = ns.model
    base = ns.data_root or BASE
    aud = ns.audio_root or os.environ.get("EARWITNESS_AUDIO_ROOT", os.path.join(REPO, "audio"))
    tdir = ns.transcript_root or os.environ.get("EARWITNESS_TRANSCRIPT_ROOT", os.path.join(REPO, "transcripts"))
    n = ns.n or N
    shows = list(csv.DictReader(open(os.path.join(base, "shows.csv"))))[:12]  # first 12 -> expect >=5 downloads
    paths = []
    for i, s in enumerate(shows):
        if len(paths) >= n: break
        try:
            d = feedparser.parse(s["feed_url"], agent=UA, request_headers={"User-Agent": UA})
            entries = sorted([e for e in d.get("entries", []) if audio_url(e)],
                             key=ep_dt, reverse=True)
            if not entries: continue
            url = audio_url(entries[0])
            dest = os.path.join(aud, f"bench_{len(paths)}.mp3")
            os.makedirs(aud, exist_ok=True)
            r = requests.get(url, headers={"User-Agent": UA}, stream=True, timeout=60)
            r.raise_for_status()
            with open(dest, "wb") as f:
                for ch in r.iter_content(1 << 20): f.write(ch)
            paths.append((dest, s["title"], entries[0].get("title", "")))
            print(f"dl {len(paths)}/{N}: {s['title'][:50]}", flush=True)
        except Exception as ex:
            print(f"skip {s['title'][:40]}: {str(ex)[:70]}", flush=True)
    print(f"downloaded {len(paths)} episodes", flush=True)
    os.makedirs(tdir, exist_ok=True)

    from faster_whisper import WhisperModel
    t0 = time.time()
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    print(f"model load: {time.time()-t0:.0f}s", flush=True)

    tot_a, tot_w = 0.0, 0.0
    for dest, show, title in paths:
        s = time.time()
        segments, info = model.transcribe(dest, beam_size=5)
        segs = [{"start": round(g.start,2), "end": round(g.end,2), "text": g.text.strip()}
                for g in segments]
        dur = segs[-1]["end"]/60 if segs else 0
        wall = time.time() - s
        rtf = dur*60/wall if wall else 0
        tot_a += dur; tot_w += wall
        print(f"{os.path.basename(dest)}: {dur:.1f} audio-min in {wall/60:.1f} wall-min = {rtf:.2f}x realtime | {show[:40]}", flush=True)
        # save transcript for quality eyeball
        bid = os.path.basename(dest).replace(".mp3", "")
        import json
        json.dump({"show": show, "ep_title": title, "duration_min": round(dur,1),
                   "segments": segs},
                  open(os.path.join(tdir, f"{bid}.json"), "w"))
    x = tot_a*60/tot_w if tot_w else 0
    print(f"BENCHMARK [{model_name}]: {tot_a:.1f} audio-min / {tot_w/3600:.2f} wall-hr "
          f"= {tot_a/(tot_w/3600):.1f} audio-min per wall-hour ({x:.2f}x realtime)")
    print("300 eps/week (~12000 audio-min) needs >= 1.19x realtime sustained on this box")

if __name__ == "__main__":
    main()
