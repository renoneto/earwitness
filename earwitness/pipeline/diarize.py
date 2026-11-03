"""Speaker diarization for the corpus runner (optional `diarize` stage).

Model: NVIDIA Nemotron-3-Diarization (open weights `nvidia/Nemotron-3-Diarization`,
OpenMDW license, commercial use allowed; never the evaluation-only "-preview"
repo), run through Hugging Face Transformers (AutoProcessor +
AutoModelForAudioFrameClassification) on the Apple GPU (mps) or the CPU.
Pinned: requirements-diarize.txt and MODEL_REVISION below.

The model runs in a supervised CHILD process (`python diarize.py --audio ...`,
started by corpus.Worker.do_diarize through corpus.supervise: RSS cap,
timeout, SIGKILL -> the stage fails, the episode continues without speakers).
Run diarize workers without an auto-restart loop: the circuit breaker's
exit 3 must stop the worker (scripts/corpus_run.py explains why).

Memory: the model's offline mode holds the whole input, and its peak RSS
grows with input length (measured 2.1 GB at 20 min, 4.3 GB at 62 min, 6.2 GB
at 110 min on mps). So the child decodes audio with one sequential ffmpeg
stream (chunked_stt.iter_windows) and diarizes fixed WINDOW_S windows that
overlap by OVERLAP_S; peak memory depends on the window, not the episode
(5-hour episodes included).

Cross-window speaker identity: the model emits per-window speaker slots
(0..7, in order of arrival) and NO speaker embeddings, so a window's slot 0
is not the previous window's slot 0. `stitch_windows` links the slots of
consecutive windows by how much speech they share inside the overlap (both
windows label the same audio there): a pair links when they co-speak for at
least MATCH_MIN_S seconds and that covers at least MATCH_MIN_SHARE of the
newer slot's overlap speech, best pairs first, one-to-one. Limits:
  - a speaker silent throughout an overlap cannot be linked across it and
    gets a new global label in the next window (a guest who leaves and
    returns 40 minutes later, a host silent while an ad break fills the
    overlap, or a produced spot that plays in the pre-roll and again in the
    post-roll of a long episode, splits into two labels). When a main voice
    of the previous window links to nothing, the window's new labels are
    marked `after_failed_link` and voice_attrs leaves their voice_ad_only
    unknown (None) instead of calling a split host an ad-only voice;
  - an error inside one overlap propagates to every later window;
  - the 8-slot cap applies per window, so an episode may carry more than 8
    global speakers, but a window with more than 8 voices still merges some.
  - results depend on where windows fall. Measured 2026-10-09 on two
    labeled episodes (My First Million 62 min, Kendall Rae 67 min; 13 reads
    with a known voice), voice_ad_only correct on: fixed 1800-s windows
    12/13 (a pre-recorded spot in the 6-min final window merged into a
    host; but a post-roll spot the whole-file pass merged via its 8-slot cap
    was caught); whole-file pass 12/13 (4.3-4.7 GB); equal-length windows
    9/13 (one wrong link carried a spot voice onto a host); a short final
    window joined to the previous one 10/13 (4.6 GB). Fixed windows kept.
Episodes shorter than one window are diarized in one pass, exactly as
measured (diarization-test.md).

Output <corpus>/speakers/<episode_id>.json: turns [{start, end, speaker}],
speakers {label: {seconds, turns, first_s, after_failed_link}}, model
id/revision, device,
window parameters, per-window link report, timing and peak RSS.

The pure functions below (stitching, voice attrs, speaker-labeled
transcripts) import only the stdlib; torch/transformers load only in the
child.
"""
from __future__ import annotations

import argparse
import gc
import json
import math
import os
import resource
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

MODEL_ID = "nvidia/Nemotron-3-Diarization"
MODEL_REVISION = "f667ed73aee57d40cc39428eb768b4fd87a0a29e"  # HF commit, 2026-10-09
DIARIZE_VERSION = 1
WINDOW_S = 1800.0        # 30 min: ~2.5-3 GB peak on mps (scales with window, not episode)
OVERLAP_S = 120.0        # shared audio used to link speakers across windows
MATCH_MIN_S = 2.0        # co-speech seconds needed to link two windows' slots
MATCH_MIN_SHARE = 0.5    # ... and share of the newer slot's overlap speech it covers
SPEECH_THRESHOLD = 0.5   # model probability above which a frame is speech of a slot
SPEAKERS_DIR = "speakers"
# voice_ad_only: the span's dominant speaker has less than this share of its
# speech outside every flagged/named ad span of the episode. Tuned on the
# 2026-10-09 measured set (/tmp/ew-diar/out/analysis.json, 39 reads with a
# known voice): produced/network spots sit at 0.000-0.002 (one at 0.433: a
# voice slot shared with a 20 s news clip), pre-recorded host spots that got
# their own speaker at 0.025, live host reads at 0.821-0.967. Any cut in
# (0.433, 0.821) gives the measured 14/16 not-live-host reads flagged with
# 0/23 live reads flagged; 0.5 leaves margin on both sides. The two misses
# (a voice merged by the 8-slot cap; a solo host's pre-recorded spot) have
# shares like live reads, so no threshold catches them.
VOICE_AD_ONLY_SHARE = 0.5
# A failed link: a main voice of the previous window (at least MAIN_VOICE_S
# of speech in the episode so far) links to no slot of the next window,
# however long it was silent before the seam (a guest talking or a block of
# spots can fill the minutes up to and through the overlap). Labels new in
# that window may be that voice split in two, so their voice_ad_only is
# unknown. Short voices are left out: a split spot or clip voice is still
# mostly inside or outside ads, but a split host fragment can sit mostly
# inside them. Measured 2026-10-10 on 12 episodes (1800/120 windows): main
# voices dropped at 4 seams (Unblinded 1, TBPN 2, s005e0 1), 10 labels
# marked; of 39 labeled reads only one (a live TBPN read) became unknown.
MAIN_VOICE_S = 120.0
# voice_ad_only is unknown for a label with less speech than this (a
# fragment); a 15-s produced spot measured 9.6 s of speech.
MIN_JUDGED_SPEECH_S = 5.0
MIXED_MIN_S = 0.5        # a segment is mixed_speakers when a 2nd speaker covers this much
PREFLIGHT_S = 4.0        # audio diarized by the preflight check


# ---------------------------------------------------------------- intervals

def _merge(intervals) -> list:
    out = []
    for a, b in sorted((float(a), float(b)) for a, b in intervals if b > a):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _inter(a: list, b: list) -> float:
    """Seconds covered by both merged interval lists."""
    i = j = 0
    total = 0.0
    while i < len(a) and j < len(b):
        lo, hi = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if hi > lo:
            total += hi - lo
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return total


def _length(iv: list) -> float:
    return sum(b - a for a, b in iv)


def _clip(iv: list, lo: float, hi: float) -> list:
    return [[max(a, lo), min(b, hi)] for a, b in iv if min(b, hi) > max(a, lo)]


# ---------------------------------------------------------------- stitching

def stitch_windows(windows: list) -> dict:
    """Join per-window diarization into one episode timeline.

    `windows`: [{"offset_s", "dur_s", "segments": [(start, end, slot), ...]}]
    in time order, segment times relative to the window. Consecutive windows
    overlap; each overlap is cut at its midpoint (earlier window's turns
    before it, later window's after), and slots are linked across it as
    described in the module docstring. Returns {"turns": [{start, end,
    speaker}], "speakers": {...}, "links": [...per window]} with labels
    S0, S1, ... in order of first speech."""
    turns, links = [], []
    prev = None          # (abs segments by global id, window end)
    next_id = 0
    unsure = set()       # global ids created at a seam where a speaking label went unlinked
    for w in windows:
        off, dur = float(w["offset_s"]), float(w["dur_s"])
        segs = [(off + float(a), off + float(b), int(k)) for a, b, k in w["segments"]
                if float(b) > float(a)]
        slots = sorted({k for _, _, k in segs}, key=lambda k: min(a for a, _, s in segs if s == k))
        mapping, report = {}, {"offset_s": round(off, 3), "dur_s": round(dur, 3),
                               "slots": len(slots), "linked": {}, "new": [], "dropped": []}
        cut = off
        if prev is not None:
            p_segs, p_end = prev
            lo, hi = off, min(p_end, off + dur)
            cut = (lo + hi) / 2 if hi > lo else off
            mine = {k: _clip(_merge((a, b) for a, b, s in segs if s == k), lo, hi) for k in slots}
            theirs = {}
            for a, b, g in p_segs:
                theirs.setdefault(g, []).append((a, b))
            theirs = {g: _clip(_merge(iv), lo, hi) for g, iv in theirs.items()}
            pairs = sorted(((_inter(mine[k], theirs[g]), k, g) for k in slots for g in theirs),
                           key=lambda x: (-x[0], x[1], x[2]))
            used = set()
            for both, k, g in pairs:
                if k in mapping or g in used or both < MATCH_MIN_S \
                        or both < MATCH_MIN_SHARE * _length(mine[k]):
                    continue
                mapping[k] = g
                used.add(g)
                report["linked"][str(k)] = {"to": g, "co_speech_s": round(both, 2)}
            # A failed link (see MAIN_VOICE_S): any new label here may be that voice.
            so_far = {}
            for a, b, g in turns:
                so_far.setdefault(g, []).append((a, b))
            report["dropped"] = sorted(
                g for g in theirs if g not in used
                and _length(_merge(so_far.get(g, ()))) >= MAIN_VOICE_S)
            turns = [(a, min(b, cut), g) for a, b, g in turns if a < cut]
        for k in slots:
            if k not in mapping:
                mapping[k] = next_id
                report["new"].append(k)
                if report["dropped"]:
                    unsure.add(next_id)
                next_id += 1
        mine_abs = [(a, b, mapping[k]) for a, b, k in segs]
        turns += [(max(a, cut), b, g) for a, b, g in mine_abs if b > cut]
        links.append(report)
        prev = (mine_abs, off + dur)
    turns = [(a, b, g) for a, b, g in turns if b - a > 1e-6]
    first = {}
    for a, _, g in sorted(turns):
        first.setdefault(g, a)
    label = {g: f"S{i}" for i, g in enumerate(sorted(first, key=lambda g: (first[g], g)))}
    out = [{"start": round(a, 2), "end": round(b, 2), "speaker": label[g]}
           for a, b, g in sorted(turns, key=lambda t: (t[0], t[1], label[t[2]]))]
    for r in links:
        r["linked"] = {k: {**v, "to": label.get(v["to"])} for k, v in r["linked"].items()}
        r["dropped"] = [label[g] for g in r["dropped"] if g in label]
    stats = speaker_stats(out)
    unsure_labels = {label[g] for g in unsure if g in label}
    for lab, st in stats.items():
        st["after_failed_link"] = lab in unsure_labels
    return {"turns": out, "speakers": stats, "links": links}


def speaker_stats(turns: list) -> dict:
    by = {}
    for t in turns:
        by.setdefault(t["speaker"], []).append((t["start"], t["end"]))
    return {s: {"seconds": round(_length(_merge(iv)), 2), "turns": len(iv),
                "first_s": round(min(a for a, _ in iv), 2)}
            for s, iv in sorted(by.items(), key=lambda x: int(x[0][1:]))}


def is_current(doc, audio_sha256, **setup) -> bool:
    """`doc` was made by the pinned model revision from this audio (and has
    the given setup values, e.g. window_s, overlap_s, device)."""
    return isinstance(doc, dict) and doc.get("revision") == MODEL_REVISION \
        and doc.get("audio_sha256") == (audio_sha256 or None) \
        and all(doc.get(k) == v for k, v in setup.items())


def validate_speakers(doc, episode_id: str, duration_s=None) -> None:
    """Raise ValueError unless `doc` is a usable speakers/<ep>.json."""
    if not isinstance(doc, dict) or doc.get("episode_id") != episode_id:
        raise ValueError("not this episode's speakers doc")
    if doc.get("model") != MODEL_ID or not isinstance(doc.get("turns"), list):
        raise ValueError("missing model or turns")
    limit = float(duration_s) * 1.05 + 30.0 if duration_s else math.inf
    for t in doc["turns"]:
        if not isinstance(t, dict):
            raise ValueError(f"bad turn {t!r}"[:200])
        a, b, s = t.get("start"), t.get("end"), t.get("speaker")
        if not (_finite(a) and _finite(b)) or not 0 <= a <= b <= limit \
                or not isinstance(s, str):
            raise ValueError(f"bad turn {t!r}"[:200])


# ---------------------------------------------------------------- voice attrs

def _label_key(s: str):
    """Speaker labels in numeric order (S2 before S10)."""
    return (0, int(s[1:]), s) if s[1:].isdigit() else (1, 0, s)


def _dominant(cover: dict):
    """The speaker with the most seconds; ties go to the lowest label."""
    return max(sorted(cover, key=_label_key), key=lambda s: cover[s], default=None)


def _by_speaker(doc) -> dict:
    by = {}
    for t in (doc or {}).get("turns") or ():
        by.setdefault(t["speaker"], []).append((t["start"], t["end"]))
    return {s: _merge(iv) for s, iv in by.items()}


def voice_attrs(doc, spans: list, ad_spans: list,
                threshold: float = VOICE_AD_ONLY_SHARE) -> list:
    """Per span ({start_s, end_s}): the speaker with the most speech inside
    it (`dominant_speaker`), the share of that speaker's episode speech
    heard outside every ad span (`voice_share_outside_ads`), and
    `voice_ad_only` (share < threshold). All None when no speech falls in
    the span. `voice_ad_only` is also None (unknown, not False) when the
    speaker's label was created at a seam where another label went
    unlinked (`after_failed_link`: it may be an earlier voice split in two)
    or has under MIN_JUDGED_SPEECH_S of speech. Labels are per episode (S0
    in one episode is not S0 in another)."""
    by = _by_speaker(doc)
    stats = (doc or {}).get("speakers") if isinstance((doc or {}).get("speakers"), dict) else {}
    ads = _merge((s["start_s"], s["end_s"]) for s in ad_spans
                 if _finite(s.get("start_s")) and _finite(s.get("end_s")))
    out = []
    for sp in spans:
        a, b = sp.get("start_s"), sp.get("end_s")
        cover = {s: _inter(iv, [[a, b]]) for s, iv in by.items()} \
            if _finite(a) and _finite(b) and b > a else {}
        dom = _dominant(cover)
        if dom is None or cover[dom] <= 0:
            out.append({"dominant_speaker": None, "voice_share_outside_ads": None,
                        "voice_ad_only": None})
            continue
        total = _length(by[dom])
        share = (total - _inter(by[dom], ads)) / total if total else 0.0
        unsure = total < MIN_JUDGED_SPEECH_S or \
            bool(isinstance(stats.get(dom), dict) and stats[dom].get("after_failed_link"))
        out.append({"dominant_speaker": dom, "voice_share_outside_ads": round(share, 3),
                    "voice_ad_only": None if unsure else share < threshold})
    return out


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


# ---------------------------------------------------------------- labeled transcript

def _word(w):
    """(start, end, text) of a word in either stored shape, or None."""
    a, b = w.get("s", w.get("start")), w.get("e", w.get("end"))
    t = w.get("w", w.get("word"))
    return (float(a), float(max(b, a)), t) if _finite(a) and _finite(b) and isinstance(t, str) \
        else None


def _speaker_at(by: dict, a: float, b: float, min_s: float = 0.0):
    """(dominant speaker over [a, b], {speaker: seconds}) by overlap."""
    if b - a < 0.02:
        a, b = a - 0.01, b + 0.01
    cover = {s: _inter(iv, [[a, b]]) for s, iv in by.items()}
    cover = {s: v for s, v in cover.items() if v > min_s}
    return _dominant(cover), cover


def labeled_transcript(doc, segments: list) -> dict:
    """Speaker-labeled transcript: segments with word timestamps are split
    into word-level turns (each word takes the speaker with the most speech
    over its span, else the previous word's); segments without words keep
    one speaker (most overlap) and `mixed_speakers` when another speaker
    covers at least MIXED_MIN_S of them. Consecutive word-level pieces of
    one speaker merge into one turn."""
    by = _by_speaker(doc)
    out, words_n, seg_n = [], 0, 0
    for seg in segments or ():
        words = [x for x in (_word(w) for w in seg.get("words") or ()
                             if isinstance(w, dict)) if x]
        if words:
            seg_dom = _speaker_at(by, seg["start"], seg["end"])[0]
            prev = None
            for a, b, text in words:
                spk = _speaker_at(by, a, b)[0] or prev or seg_dom
                prev = spk
                words_n += 1
                last = out[-1] if out else None
                if last and last["level"] == "word" and last["speaker"] == spk:
                    last["end"] = round(b, 2)
                    last["text"] += text
                else:
                    out.append({"start": round(a, 2), "end": round(b, 2), "speaker": spk,
                                "text": text, "level": "word"})
            continue
        dom, cover = _speaker_at(by, seg["start"], seg["end"])
        seg_n += 1
        out.append({"start": seg["start"], "end": seg["end"], "speaker": dom,
                    "text": seg.get("text", ""), "level": "segment",
                    "mixed_speakers": sum(1 for v in cover.values() if v >= MIXED_MIN_S) > 1})
    for t in out:
        t["text"] = t["text"].strip()
    return {"level": "word" if words_n and not seg_n else "segment" if seg_n and not words_n
            else "mixed" if out else None,
            "words": words_n, "segments_without_words": seg_n, "turns": out}


# ---------------------------------------------------------------- child

def _parent_watchdog():
    """Exit when the supervising worker dies (macOS has no PDEATHSIG)."""
    parent = os.getppid()

    def watch():
        while True:
            time.sleep(1.0)
            if os.getppid() != parent:
                os._exit(70)
    threading.Thread(target=watch, daemon=True).start()


def _maxrss_mb() -> float:
    div = 1024 * 1024 if sys.platform == "darwin" else 1024
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / div


class NemotronDiarizer:
    """The model, loaded once per child; `__call__(samples)` diarizes one
    16 kHz mono float32 window and returns [(start, end, slot)]."""

    def __init__(self, device: str, revision: str = MODEL_REVISION, threads: int = 4,
                 local_files_only: bool = True):
        if device == "mps":
            os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        os.environ.setdefault("OMP_NUM_THREADS", str(threads))
        import torch
        import transformers
        from transformers import AutoModelForAudioFrameClassification, AutoProcessor
        torch.set_num_threads(max(1, threads))
        if device == "mps" and not torch.backends.mps.is_available():
            raise RuntimeError("device mps requested but torch.backends.mps is unavailable")
        self.torch, self.device = torch, device
        # The child never downloads: the pinned weights must already be in
        # the Hugging Face cache (preflight checks it).
        self.proc = AutoProcessor.from_pretrained(MODEL_ID, revision=revision,
                                                  local_files_only=local_files_only)
        self.model = AutoModelForAudioFrameClassification.from_pretrained(
            MODEL_ID, revision=revision, dtype=torch.float32,
            local_files_only=local_files_only).to(device).eval()
        self.info = {"torch": torch.__version__, "transformers": transformers.__version__,
                     "dtype": "float32", "mode": "offline", "threads": threads,
                     "speech_threshold": SPEECH_THRESHOLD}

    def __call__(self, samples) -> list:
        torch = self.torch
        inputs = self.proc(samples, sampling_rate=16000).to(self.device, dtype=torch.float32)
        with torch.inference_mode():
            logits = self.model(**inputs).logits
        logits = logits.float().cpu()
        mask = inputs["attention_mask"].cpu() if "attention_mask" in inputs else None
        segs = self.proc.extract_speaker_dict(logits, mask, threshold=SPEECH_THRESHOLD)[0]
        del inputs, logits
        gc.collect()
        if self.device == "mps":
            torch.mps.empty_cache()
        return [(float(s["Start"]), float(s["End"]), int(s["Speaker"])) for s in segs]


def diarize_file(audio, infer, *, window_s: float = WINDOW_S, overlap_s: float = OVERLAP_S,
                 ffmpeg: str | None = None, log=None) -> dict:
    """Decode `audio` sequentially and diarize it window by window with
    `infer(samples) -> [(start, end, slot)]`; returns stitch_windows(...)
    plus decoded duration and inference seconds."""
    from earwitness.pipeline import chunked_stt as stt
    windows, stats, infer_s = [], {}, 0.0
    for off, samples in stt.iter_windows(audio, window_s, overlap_s, ffmpeg=ffmpeg, stats=stats):
        dur = len(samples) / stt.SAMPLE_RATE
        t0 = time.monotonic()
        segs = infer(samples)
        infer_s += time.monotonic() - t0
        windows.append({"offset_s": off, "dur_s": dur, "segments": segs})
        if log:
            log(f"window @{off:.0f}s ({dur:.0f}s): {len(segs)} segments, "
                f"{len({k for *_, k in segs})} slots, maxrss_mb={_maxrss_mb():.0f}")
    res = stitch_windows(windows)
    res["decoded_duration_s"] = round(stats.get("decoded_s", 0.0), 3)
    res["infer_s"] = round(infer_s, 3)
    return res


def run_child(a, infer=None) -> int:
    t0 = time.monotonic()
    if infer is None:
        infer = NemotronDiarizer(a.device, a.revision, a.threads)
    info = getattr(infer, "info", {})
    load_s = time.monotonic() - t0
    res = diarize_file(a.audio, infer, window_s=a.window_s, overlap_s=a.overlap_s,
                       ffmpeg=a.ffmpeg, log=lambda m: print(m, flush=True))
    decoded = res.pop("decoded_duration_s")
    wall = time.monotonic() - t0
    doc = {"episode_id": a.episode_id, "version": DIARIZE_VERSION, "model": MODEL_ID,
           "revision": a.revision, "device": a.device,
           "diarized_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "window_s": a.window_s, "overlap_s": a.overlap_s,
           "match": {"min_s": MATCH_MIN_S, "min_share": MATCH_MIN_SHARE},
           "decoded_duration_s": decoded, "audio_sha256": a.audio_sha256 or None,
           "turns": res["turns"], "speakers": res["speakers"], "windows": res["links"],
           "runtime": {**info, "load_s": round(load_s, 3), "infer_s": res["infer_s"],
                       "wall_s": round(wall, 3),
                       "x_realtime": round(decoded / res["infer_s"], 1) if res["infer_s"] else None,
                       "peak_rss_mb": round(_maxrss_mb(), 1)}}
    from earwitness.pipeline.chunked_stt import atomic_write_json
    atomic_write_json(a.out, doc)
    return 0


def preflight(a) -> int:
    """Load the pinned weights from the local cache (never downloads) on the
    device and diarize PREFLIGHT_S of quiet noise; print one JSON line."""
    try:
        import numpy as np
        t0 = time.monotonic()
        infer = NemotronDiarizer(a.device, a.revision, a.threads, local_files_only=True)
        samples = (np.random.default_rng(0).standard_normal(int(PREFLIGHT_S * 16000))
                   * 0.01).astype(np.float32)
        segs = infer(samples)
        if not isinstance(segs, list):
            raise RuntimeError(f"inference returned {type(segs).__name__}, not a list")
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]}))
        return 2
    print(json.dumps({"ok": True, **getattr(infer, "info", {}), "device": a.device,
                      "revision": a.revision, "preflight_s": round(time.monotonic() - t0, 2),
                      "peak_rss_mb": round(_maxrss_mb(), 1)}))
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="diarize one episode (child of the corpus worker)")
    p.add_argument("--preflight", action="store_true")
    p.add_argument("--audio")
    p.add_argument("--out")
    p.add_argument("--episode-id")
    p.add_argument("--device", choices=("mps", "cpu"), default="mps")
    p.add_argument("--revision", default=MODEL_REVISION)
    p.add_argument("--window-s", type=float, default=WINDOW_S)
    p.add_argument("--overlap-s", type=float, default=OVERLAP_S)
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--ffmpeg", default=None)
    p.add_argument("--audio-sha256", default="")
    return p.parse_args(argv)


def main(argv=None) -> int:
    a = parse_args(argv)
    if a.preflight:
        return preflight(a)
    _parent_watchdog()
    return run_child(a)


if __name__ == "__main__":
    # Run as a script by the worker (possibly under a separate venv): import
    # the repo package, never sibling modules as top-level names.
    sys.path[0] = str(Path(__file__).resolve().parent.parent.parent)
    raise SystemExit(main())
