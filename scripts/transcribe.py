#!/usr/bin/env python3
"""Phase 3b: transcribe episodes with faster-whisper (CPU), save segments, discard audio.
Usage: transcribe.py [--benchmark N] [--model small] [--limit K]"""
import json, os, sys, time
from datetime import datetime, timezone

# httpx (via huggingface_hub) chokes on IPv6 entries in no_proxy; drop them.
for _v in ("no_proxy", "NO_PROXY"):
    os.environ.pop(_v, None)

import argparse as _ap
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from earwitness.config import resolve as _resolve
from earwitness.retention import retention_from_args as _retention

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_DEFAULT = os.environ.get("EARWITNESS_DATA_ROOT", os.path.join(REPO, "data"))

def main():
    ap = _ap.ArgumentParser(description="Transcribe episodes (reference, hardened paths)")
    ap.add_argument("--model", default="small")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--benchmark", type=int, default=0)
    ap.add_argument("--keep-audio", action="store_true")
    ap.add_argument("--retain-audio", action="store_true")
    ap.add_argument("--discard-audio", action="store_true")
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--transcript-root", default=None)
    ap.add_argument("--audio-root", default=None)
    ns = ap.parse_args()
    args, model_name, limit, benchmark_n = sys.argv[1:], ns.model, ns.limit, ns.benchmark
    # Retention default: discard audio after transcription (safe local default).
    # --keep-audio / --retain-audio / EARWITNESS_RETAIN_AUDIO=1 retains it.
    keep_audio = bool(_retention(ns.keep_audio or ns.retain_audio, True)["retain_audio"])
    BASE = str(_resolve("data_root", ns.data_root))
    TDIR = str(_resolve("transcript_root", ns.transcript_root))
    AUD = str(_resolve("audio_root", ns.audio_root))

    from faster_whisper import WhisperModel
    t0 = time.time()
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    print(f"model {model_name} loaded in {time.time()-t0:.1f}s", flush=True)

    episodes = json.load(open(f"{BASE}/episodes.json"))
    if benchmark_n:
        episodes = episodes[:benchmark_n]
    if limit:
        episodes = episodes[:limit]
    os.makedirs(TDIR, exist_ok=True)

    total_audio_min, total_wall_s = 0.0, 0.0
    done = 0
    for ep in episodes:
        path = ep["local_path"]
        if not os.path.exists(path):
            continue
        out = os.path.join(TDIR, f"{ep['ep_id']}.json")
        if os.path.exists(out):
            done += 1; continue
        s = time.time()
        try:
            segments, info = model.transcribe(path, beam_size=5)
            segs = [{"start": round(sg.start, 2), "end": round(sg.end, 2),
                     "text": sg.text.strip()} for sg in segments]
            dur_min = (segs[-1]["end"] / 60) if segs else 0
            json.dump({"ep_id": ep["ep_id"], "model": model_name,
                       "transcribed_at": datetime.now(timezone.utc).isoformat(),
                       "duration_min": round(dur_min, 1),
                       "language": info.language,
                       "segments": segs}, open(out, "w"))
            wall = time.time() - s
            total_audio_min += dur_min; total_wall_s += wall
            rtf = dur_min * 60 / wall if wall else 0
            print(f"{ep['ep_id']}: {dur_min:.1f} audio-min in {wall/60:.1f} wall-min "
                  f"({rtf:.2f}x realtime)", flush=True)
            done += 1
            if not keep_audio:
                os.remove(path)  # transcribe-and-discard (keep for clip extraction if flagged)
        except Exception as ex:
            print(f"{ep['ep_id']}: TRANSCRIBE FAIL {ex}", flush=True)
    if total_wall_s:
        x = total_audio_min * 60 / total_wall_s
        print(f"BENCHMARK: {total_audio_min:.1f} audio-min / {total_wall_s/3600:.2f} wall-hr "
              f"= {total_audio_min/(total_wall_s/3600):.1f} audio-min per wall-hr ({x:.2f}x realtime)")
        print(f"300 eps/week (~12000 audio-min) needs >=1.19x realtime sustained")
    print(f"DONE: {done} transcripts")

if __name__ == "__main__":
    main()
