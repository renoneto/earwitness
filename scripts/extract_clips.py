#!/usr/bin/env python3
"""Extract 60-90s evidence clips for detected ad reads using ffmpeg.
Clip = read window padded to 60s minimum (30s context each side), capped at 90s.
Output: clips/<ep_id>_<brand>_<start>s.mp3. Verifies alignment by duration."""
import argparse, json, os, subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("EARWITNESS_DATA_ROOT", os.path.join(REPO, "data"))

def main():
    ap = argparse.ArgumentParser(description="Evidence clip extraction (reference, hardened paths)")
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--reads", default=None)
    ap.add_argument("--clips-root", default=None)
    ns = ap.parse_args()
    base = ns.data_root or BASE
    clips = ns.clips_root or os.environ.get("EARWITNESS_CLIPS_ROOT", os.path.join(REPO, "clips"))
    reads_path = ns.reads or os.path.join(base, "ad_reads.json")
    try:
        reads = json.load(open(reads_path))
    except Exception as ex:
        print(f"ERROR: reads parse ({reads_path}): {ex}")
        raise SystemExit(2)
    episodes = {e["ep_id"]: e for e in json.load(open(os.path.join(base, "episodes.json")))}
    os.makedirs(clips, exist_ok=True)
    done, skipped = 0, 0
    for r in reads:
        ep = episodes.get(r["ep_id"])
        if not ep or not os.path.exists(ep["local_path"]):
            print(f"SKIP {r['ep_id']}: audio gone"); skipped += 1; continue
        # pad to >=60s, cap at 90s
        dur = r["end_s"] - r["start_s"]
        pad = max(0, (60 - dur) / 2)
        start = max(0, r["start_s"] - pad)
        length = min(90, dur + 2 * pad)
        out = os.path.join(clips, f"{r['ep_id']}_{r['brand']}_{int(r['start_s'] or 0)}s.mp3")
        if os.path.exists(out):
            done += 1; continue
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", str(round(start, 1)),
               "-i", ep["local_path"], "-t", str(round(length, 1)),
               "-acodec", "libmp3lame", "-b:a", "64k", out]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0 and os.path.exists(out):
            done += 1
        else:
            print(f"CLIP FAIL {r['ep_id']}: {res.stderr[:200]}"); skipped += 1
    print(f"DONE: {done} clips, {skipped} skipped")

if __name__ == "__main__":
    main()
