"""Gap fill for Parakeet transcripts (optional, `--gap-fill-model`).

Parakeet TDT sometimes leaves out speech where music and talk meet (a
music-join drop; see CONTEXT.md); it shows as a gap of several seconds
between consecutive words. Each gap of at least `min_s` (the episode's
start and end count as word edges) is cut from the audio with a pad on
both sides and re-transcribed with Whisper (mlx-whisper, greedy, word
timestamps). Only Whisper words whose midpoint falls inside the gap and
that pass the guards below are spliced in, marked `"src": "gapfill"` on
both the word and its segment. Parakeet's own words are never changed.

Guards (Whisper invents text over music): a Whisper segment is dropped on
a high no-speech probability, a low average log probability, a high
compression ratio (repetition loops) or a known hallucination phrase
("Thank you for watching"); words without letters or digits ("♪") and
bracketed annotations ("[Music]", "(upbeat music)") are not words; words
repeating the Parakeet words on either side of the gap are trimmed; a
gap is filled only with at least GUARDS["min_words"] words and only when
Whisper detects an allowed language.

Every gap gets a record in stats "gaps" ({start, end, outcome, windows,
words, rejected}), outcome one of: "filled" (words spliced in), "silent"
(below SILENCE_PEAK, not decoded), "rejected" (Whisper heard words in the
gap, every one failed a guard; "rejected" counts the reasons), "no_words"
(decoded, Whisper put no word inside the gap) or "skipped" (the episode was
not gap-filled, see "skipped"). Provenance "gap_fill" is {"enabled": false}
for a Parakeet transcript made without gap fill.

Gaps longer than MAX_WINDOW_S are decoded in windows of at most that
length. An episode whose gaps cover more than MAX_GAP_SHARE of it (and
more than SKIP_MIN_GAP_S in all) is not gap-filled at all, with the reason
recorded: mostly music, silence or a language Parakeet does not hear.

Limits: a show not in English gets no gap fill (Whisper must detect
English). Sung lyrics in a gap are spliced in like speech: Whisper scores
them as confidently as speech (mean word probability 0.93-0.99, avg
logprob about -0.2 on the bake-off's song clips), so no guard separates
them. With turbo the no-speech probability measured 0.0 on every segment,
so that guard rarely fires.
"""
from __future__ import annotations

import os
import re
import time
import traceback

from earwitness.pipeline import chunked_stt as stt

DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"
DEFAULT_MIN_S = 3.0
PAD_S = 1.5             # context each side of a gap; words in the pad are not taken
SILENCE_PEAK = 1e-3     # -60 dBFS: a padded gap this quiet is digital silence, not decoded
MAX_WINDOW_S = 120.0    # longer gaps are decoded in windows (Parakeet's chunk length)
MAX_GAP_SHARE = 0.5     # gaps over this share of the episode ...
SKIP_MIN_GAP_S = 300.0  # ... and over this many seconds in all: skip the episode
TRACEBACK_CHARS = 4000  # tail of a gap-fill error's traceback kept in provenance
NEIGHBOUR_WORDS = 5     # Parakeet words each side compared for duplicates
GUARDS = {
    "no_speech_prob_max": 0.6,
    "avg_logprob_min": -1.0,
    "compression_ratio_max": 2.4,
    "word_probability_min": 0.2,
    "min_words": 2,
    "languages": ("en",),
}
# Normalized (lowercase, letters/digits/apostrophes) text Whisper is known to
# produce over music or silence: a segment containing one of PHRASES, or
# whose whole text is one of WHOLE, is dropped.
PHRASES = ("thank you for watching", "thanks for watching", "thank you so much for watching",
           "please subscribe", "like and subscribe", "subscribe to my channel",
           "don't forget to subscribe", "subtitles by", "subtitled by", "captions by",
           "captioned by", "transcribed by", "translated by", "amaraorg", "amara org",
           "see you in the next video")
WHOLE = ("you", "thank you", "thank you very much", "thanks", "bye", "bye bye", "okay", "oh",
         "so", "yeah", "hmm", "uh", "um", "music", "applause", "laughter")


def guards_record() -> dict:
    """The guard settings as recorded in provenance."""
    return {**GUARDS, "languages": list(GUARDS["languages"]), "phrases": list(PHRASES),
            "whole": list(WHOLE), "neighbour_words": NEIGHBOUR_WORDS,
            "silence_peak": SILENCE_PEAK, "max_window_s": MAX_WINDOW_S,
            "max_gap_share": MAX_GAP_SHARE, "skip_min_gap_s": SKIP_MIN_GAP_S}


def _norm(text: str) -> str:
    """Lowercase; curly apostrophes straightened, hyphens and dashes to
    spaces, other punctuation dropped, whitespace collapsed."""
    text = re.sub(r"[\u2018\u2019\u02bc`]", "'", text.lower())
    text = re.sub(r"[-\u2010-\u2015]", " ", text)
    return " ".join(re.sub(r"[^\w' ]", "", text).split())


def _tok(word: str) -> str:
    return re.sub(r"[^\w']", "", word.lower())


def find_gaps(words: list, total_s: float, min_s: float) -> list:
    """[(start, end)] of every stretch of at least `min_s` with no word:
    between consecutive words, before the first and after the last."""
    edges = [0.0] + [x for w in words for x in (w["start"], w["end"])] + [total_s]
    gaps = []
    for i in range(0, len(edges), 2):
        a, b = edges[i], edges[i + 1]
        if b - a >= min_s:
            gaps.append((round(a, 2), round(b, 2)))
    return gaps


def iter_spans(path, spans: list, ffmpeg: str | None = None, block_s: float = 30.0):
    """Yield (index, float32 samples) for each (start, end) span of `path`,
    as soon as the one sequential decode (chunked_stt.iter_windows, sample
    exact) has passed its end. A span past the end of the audio is cut at
    the end."""
    import numpy as np
    sr = stt.SAMPLE_RATE
    bounds = [(int(round(a * sr)), int(round(b * sr))) for a, b in spans]
    order = sorted(range(len(spans)), key=lambda i: bounds[i])
    parts: dict = {}
    nxt = 0
    for offset, audio in stt.iter_windows(path, block_s, 0.0, ffmpeg):
        lo = int(round(offset * sr))
        hi = lo + len(audio)
        while nxt < len(order) and bounds[order[nxt]][0] < hi:
            parts[order[nxt]] = []
            nxt += 1
        for i in list(parts):
            a, b = bounds[i]
            s, e = max(a, lo), min(b, hi)
            if e > s:
                parts[i].append(audio[s - lo:e - lo].copy())
            if b <= hi:
                yield i, _join(parts.pop(i))
    for i in list(parts) + order[nxt:]:
        yield i, _join(parts.pop(i, []))


def _join(pieces):
    import numpy as np
    return np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float32)


def whisper_snapshot(repo: str, revision: str | None = None, *,
                     local_only: bool = False) -> dict:
    """{"repo", "path", "revision"} of an mlx-whisper model: an HF repo id
    (HF cache, downloaded on first use; `local_only` never touches the
    network) or a local directory (revision None)."""
    from pathlib import Path
    if Path(repo).is_dir():
        d = Path(repo).resolve()
        weights = next((d / n for n in ("weights.safetensors", "weights.npz")
                        if (d / n).exists()), None)
        snap = {"repo": repo, "path": str(d), "revision": None, "weights_sha256": None}
        if weights is not None:
            st = os.stat(weights)
            snap.update(weights=str(weights), weights_size=st.st_size,
                        weights_mtime_ns=st.st_mtime_ns,
                        weights_sha256=stt.file_sha256(weights))
        return snap
    from huggingface_hub import snapshot_download
    path = Path(snapshot_download(repo, revision=revision, local_files_only=local_only))
    return {"repo": repo, "path": str(path),
            "revision": path.name if path.parent.name == "snapshots" else revision}


class WhisperGapModel:
    """mlx-whisper loaded from a resolved snapshot (no network when pinned)."""

    def __init__(self, repo: str = DEFAULT_MODEL, pin: dict | None = None):
        import mlx_whisper
        self.mlx_whisper = mlx_whisper
        self.repo = repo
        pinned = pin if pin and pin.get("repo") == repo else None
        if pinned and pinned.get("weights") and os.path.isdir(repo):
            snap = {"path": pinned["path"], "revision": None,
                    "weights_sha256": stt.weights_sha256(pinned["weights"], pinned)}
        else:
            snap = (whisper_snapshot(repo, pinned.get("revision"), local_only=True) if pinned
                    else whisper_snapshot(repo))
        self.path, self.revision = snap["path"], snap["revision"]
        self.weights_sha256 = snap.get("weights_sha256")  # local dirs only
        self.version = getattr(mlx_whisper, "__version__", None) \
            or stt._dist_version("mlx-whisper")

    def transcribe(self, audio) -> dict:
        return self.mlx_whisper.transcribe(
            audio, path_or_hf_repo=self.path, language=None, temperature=0.0,
            word_timestamps=True, condition_on_previous_text=False, verbose=None)


def _segment_reason(seg: dict, text: str):
    g = GUARDS
    if (seg.get("no_speech_prob") or 0.0) > g["no_speech_prob_max"]:
        return "no_speech"
    if (seg.get("avg_logprob") or 0.0) < g["avg_logprob_min"]:
        return "avg_logprob"
    if (seg.get("compression_ratio") or 0.0) > g["compression_ratio_max"]:
        return "compression_ratio"
    norm = _norm(text)
    if norm in WHOLE or any(p in norm for p in PHRASES):
        return "phrase"
    return None


def _drop_annotations(words: list) -> list:
    """Words that contain a letter or digit, outside closed [...] / (...)
    annotations ("[Music]", "(upbeat music)"). An opener never closed in
    the segment ("(and ...") is an ordinary word."""
    def opens(t):
        return t[:1] in ("[", "(")

    def closes(t):
        return re.search(r"[\])]\W*$", t) is not None
    out, i = [], 0
    while i < len(words):
        t = words[i]["word"]
        if opens(t):
            j = next((k for k in range(i, len(words)) if closes(words[k]["word"])), None)
            if j is not None:
                i = j + 1
                continue
        if re.search(r"\w", t):
            out.append(words[i])
        i += 1
    return out


def _overlap(left: list, right: list) -> int:
    """Longest k <= NEIGHBOUR_WORDS with left's last k tokens == right's first k."""
    for k in range(min(NEIGHBOUR_WORDS, len(left), len(right)), 0, -1):
        if [_tok(t) for t in left[-k:]] == [_tok(t) for t in right[:k]]:
            return k
    return 0


def select_words(result: dict, gap: tuple, offset: float, before: list, after: list,
                 rejected: dict | None = None) -> list:
    """Whisper `result` for audio starting at `offset` -> [[word dicts] per
    kept Whisper segment] inside `gap`, times absolute and clamped into the
    gap. `before`/`after`: Parakeet word texts on each side. Reasons for
    dropping anything that had words inside the gap are counted in
    `rejected`."""
    rejected = rejected if rejected is not None else {}

    def reject(reason):
        rejected[reason] = rejected.get(reason, 0) + 1
        return []
    g0, g1 = gap
    lang = result.get("language")
    groups, passed = [], False
    for seg in result.get("segments") or ():
        inside = []
        for w in seg.get("words") or ():
            a, b = offset + float(w["start"]), offset + float(w["end"])
            if g0 < (a + b) / 2 < g1:
                inside.append({**w, "word": w["word"].strip(), "start": a, "end": b})
        if not inside:
            continue
        if lang not in GUARDS["languages"]:
            return reject("language")
        reason = _segment_reason(seg, " ".join(w["word"] for w in inside))
        if reason:
            reject(reason)
            continue
        passed = True
        kept = [w for w in _drop_annotations(inside)
                if float(w.get("probability", 1.0)) >= GUARDS["word_probability_min"]]
        if kept:
            groups.append(kept)
    flat = [w for grp in groups for w in grp]
    lead = _overlap(before, [w["word"] for w in flat])
    tail = _overlap([w["word"] for w in flat[lead:]], after)
    keep = {id(w) for w in flat[lead:len(flat) - tail]}
    if len(keep) < GUARDS["min_words"]:
        return reject("too_few_words") if passed else []
    out = []
    for grp in groups:
        words = []
        for w in grp:
            if id(w) not in keep:
                continue
            start = min(max(round(w["start"], 2), g0), g1)
            end = min(max(round(w["end"], 2), start), g1)
            words.append({"start": start, "end": end, "word": w["word"]})
        if words:
            out.append(words)
    return out


def windows(gap: tuple, max_s: float = MAX_WINDOW_S) -> list:
    """`gap` cut into equal consecutive windows of at most `max_s`."""
    import math
    g0, g1 = gap
    n = max(1, math.ceil((g1 - g0) / max_s - 1e-9))
    step = (g1 - g0) / n
    return [(round(g0 + k * step, 2), round(g1 if k == n - 1 else g0 + (k + 1) * step, 2))
            for k in range(n)]


def fill_gaps(path, words: list, segments: list, total_s: float, model, *,
              min_s: float = DEFAULT_MIN_S, pad_s: float = PAD_S,
              ffmpeg: str | None = None) -> tuple:
    """(words, segments, stats) with guarded Whisper words spliced into
    every gap of at least `min_s`; the inputs are not modified."""
    import bisect
    t0 = time.monotonic()
    gaps = find_gaps(words, total_s, min_s)
    stats = {"gaps_found": len(gaps), "gaps_silent": 0, "gaps_decoded": 0, "gaps_filled": 0,
             "words_added": 0, "windows": 0, "decoded_audio_s": 0.0, "rejected": {}}
    stats["gaps"] = [{"start": a, "end": b, "outcome": None, "windows": 0, "words": 0}
                     for a, b in gaps]
    gap_s = sum(b - a for a, b in gaps)
    if total_s and gap_s > MAX_GAP_SHARE * total_s and gap_s > SKIP_MIN_GAP_S:
        stats["skipped"] = (f"gaps cover {gap_s / total_s:.0%} of the audio ({gap_s:.0f} s), "
                            f"over {MAX_GAP_SHARE:.0%}: mostly music, silence or a language "
                            "Parakeet does not hear")
        for rec in stats["gaps"]:
            rec["outcome"] = "skipped"
        stats["wall_s"] = round(time.monotonic() - t0, 3)
        return list(words), list(segments), stats
    wins = [(g, w, w == ws[-1]) for g, gap in enumerate(gaps) for ws in [windows(gap)]
            for w in ws]
    stats["windows"] = len(wins)
    spans = [(max(0.0, w0 - pad_s), min(total_s, w1 + pad_s)) for _, (w0, w1), _ in wins]
    starts = [w["start"] for w in words]
    new_words, new_segs = [], []
    state = {}  # gap index -> {"decoded", "filled", "prev": accepted words of the last window}
    for i, audio in iter_spans(path, spans, ffmpeg) if wins else ():
        g, win, last = wins[i]
        st = state.setdefault(g, {"decoded": False, "filled": False, "prev": None,
                                  "rejected": {}})
        stats["gaps"][g]["windows"] += 1
        if len(audio) and float(abs(audio).max()) >= SILENCE_PEAK:
            st["decoded"] = True
            stats["decoded_audio_s"] += len(audio) / stt.SAMPLE_RATE
            k = bisect.bisect_left(starts, gaps[g][1])  # first word after the gap
            before = ([w["word"] for w in words[max(0, k - NEIGHBOUR_WORDS):k]]
                      if st["prev"] is None else st["prev"])
            after = [w["word"] for w in words[k:k + NEIGHBOUR_WORDS]] if last else []
            offset = int(round(spans[i][0] * stt.SAMPLE_RATE)) / stt.SAMPLE_RATE
            groups = select_words(model.transcribe(audio), win, offset, before, after,
                                  st["rejected"])
            flat = [w["word"] for grp in groups for w in grp]
            st["prev"] = flat[-NEIGHBOUR_WORDS:]
            st["filled"] = st["filled"] or bool(groups)
            for grp in groups:
                stats["words_added"] += len(grp)
                stats["gaps"][g]["words"] += len(grp)
                new_words += [{**w, "src": "gapfill"} for w in grp]
                end = grp[-1]["end"] if grp[-1]["end"] > grp[0]["start"] \
                    else round(grp[0]["start"] + 0.01, 2)
                new_segs.append({"start": grp[0]["start"], "end": end,
                                 "text": " ".join(w["word"] for w in grp), "src": "gapfill"})
        else:
            st["prev"] = []
        if last:
            stats["gaps_decoded" if st["decoded"] else "gaps_silent"] += 1
            stats["gaps_filled"] += st["filled"]
            rec = stats["gaps"][g]
            rec["outcome"] = ("filled" if st["filled"] else "silent" if not st["decoded"]
                              else "rejected" if st["rejected"] else "no_words")
            if st["rejected"]:
                rec["rejected"] = dict(st["rejected"])
            for reason, n in st["rejected"].items():
                stats["rejected"][reason] = stats["rejected"].get(reason, 0) + n
    stats["decoded_audio_s"] = round(stats["decoded_audio_s"], 2)
    stats["wall_s"] = round(time.monotonic() - t0, 3)
    if not new_words:
        return list(words), list(segments), stats
    return (sorted(words + new_words, key=lambda w: w["start"]),
            sorted(segments + new_segs, key=lambda s: (s["start"], s["end"])), stats)


def apply(path, result: dict, repo: str, *, min_s: float = DEFAULT_MIN_S,
          pin: dict | None = None, ffmpeg: str | None = None, log=print) -> dict:
    """Gap-fill a transcribe_file `result` (with "words") in place; returns
    the provenance record. Any failure leaves `result` as Parakeet made it
    and is recorded as "error": gap fill never costs the transcript."""
    prov = {"enabled": True, "model": repo, "min_gap_s": min_s, "pad_s": PAD_S,
            "guards": guards_record()}
    try:
        import mlx.core as mx
        mx.reset_peak_memory()
        model = WhisperGapModel(repo, pin)
        prov.update(revision=model.revision, mlx_whisper=model.version)
        if model.weights_sha256:
            prov["weights_sha256"] = model.weights_sha256
        words, segments, stats = fill_gaps(
            path, result["words"], result["segments"], result["decoded_duration_s"], model,
            min_s=min_s, ffmpeg=ffmpeg)
        prov.update(stats)
        prov["mlx_peak_memory_mb"] = round(mx.get_peak_memory() / 2 ** 20, 1)
        result["words"], result["segments"] = words, segments
    except Exception as exc:
        tb = traceback.format_exc()
        log(f"gap fill failed, transcript kept without it: {tb}")
        prov["error"] = f"{type(exc).__name__}: {exc}"[:500]
        prov["traceback"] = tb[-TRACEBACK_CHARS:]
    return prov
