"""Memory-bounded chunked transcription for the corpus runner.

faster-whisper's `model.transcribe(path)` builds the log-mel spectrogram
for the whole file at once (peak RSS ~0.27 GiB + ~0.06 GiB per audio
minute), which is what SIGKILLed long episodes. Here audio is decoded by
ONE streaming ffmpeg process into a rolling window buffer, and each
fixed-length window is transcribed separately, so peak memory depends on
`chunk_s`, never on episode duration.

Decoding is sequential rather than `ffmpeg -ss START` per window: on a
DAI-stitched MP3 (segments with different bitrates/channel layouts
concatenated) input seeking lands tens of seconds off, which would shift
every timestamp in that window. A sequential decode is sample-exact.

Language is decided per window by voting over several 30 s probes spread
across the window, so an English pre-roll ad cannot set the language of
the following ten minutes of a non-English show.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path

SAMPLE_RATE = 16000
PROBE_S = 30.0


class TranscriptInvalid(ValueError):
    """A transcript failed validation; it must never count as done."""


def ffmpeg_path() -> str:
    found = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    if not os.path.exists(found):
        raise RuntimeError("ffmpeg not found")
    return found


def iter_windows(path, chunk_s: float, overlap_s: float, ffmpeg: str | None = None,
                 stats: dict | None = None):
    """Yield (offset_s, float32 samples) windows of `chunk_s` seconds.

    Consecutive windows start `chunk_s - overlap_s` apart. Only one window
    (plus one read block) is held in memory. `stats["decoded_s"]` holds the
    total decoded duration once the generator is exhausted.
    """
    import numpy as np
    if not 0 <= overlap_s < chunk_s:
        raise ValueError("require 0 <= overlap_s < chunk_s")
    chunk_n = int(round(chunk_s * SAMPLE_RATE))
    step_n = chunk_n - int(round(overlap_s * SAMPLE_RATE))
    cmd = [ffmpeg or ffmpeg_path(), "-nostdin", "-v", "error", "-i", str(path),
           "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"]
    with tempfile.TemporaryFile() as errf:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf,
                                stdin=subprocess.DEVNULL)
        finished = False
        try:
            buf = np.empty(0, dtype=np.float32)
            consumed = 0      # samples dropped from the front of buf
            yielded_to = 0    # absolute sample index already yielded
            while True:
                need = chunk_n - len(buf)
                data = proc.stdout.read(need * 4)  # blocks until need*4 bytes or EOF
                eof = len(data) < need * 4
                data = data[:len(data) - len(data) % 4]
                if data:
                    buf = np.concatenate([buf, np.frombuffer(data, dtype=np.float32)])
                if consumed + len(buf) > yielded_to:
                    yielded_to = consumed + len(buf)
                    yield consumed / SAMPLE_RATE, buf
                if eof:
                    break
                buf = buf[step_n:].copy()
                consumed += step_n
            finished = True
        finally:
            if not finished and proc.poll() is None:
                proc.kill()
            proc.stdout.close()
            rc = proc.wait()
        if rc != 0:
            errf.seek(0)
            raise RuntimeError(f"ffmpeg exited {rc}: "
                               f"{errf.read()[-500:].decode(errors='replace')}")
    if stats is not None:
        stats["decoded_s"] = yielded_to / SAMPLE_RATE


SEAM_OVERLAP_TOL_S = 0.3   # a kept segment may overlap the previous one by this much
SEAM_MIN_RUN = 3           # shared words that align two windows anywhere in the overlap
SEAM_MIN_ANCHORED = 2      # ... when window k's last words are window k+1's first
SEAM_COVERED = 0.5         # no-text fallback: share of a segment the other side must cover
SEAM_TOUCH_S = 0.3         # an anchored run's copies may merely touch (v1-repaired seams)
SEAM_REPAIR_VERSION = 3
# A decoder may stamp the last words of a window past its real end (Whisper
# pads the final window to 30 s: tiny put an ad's fine print 3.3 s late). Such
# words are pinned to the end of the audio when they start within this slack
# of it; further out the timestamps are an error the validator must see.
TAIL_SLACK_S = 10.0
SEAM_COUNTS = ("removed_segments", "trimmed_segments", "retimed_segments")
_OWNER_TOL_S = 0.006       # stored times are rounded to 0.01 s


def _norm_words(text: str) -> list:
    """(original word, normalized token) pairs; token is '' for pure punctuation."""
    return [(w, re.sub(r"[^\w']", "", w.lower())) for w in text.split()]


def _tokens(text: str) -> list:
    return [t for _, t in _norm_words(text) if t]


def _slice_tokens(text: str, lo: int, hi: int | None = None) -> str:
    """Words of `text` from token `lo` up to token `hi` (exclusive); pure
    punctuation stays attached to the token before it."""
    out, seen = [], 0
    for word, tok in _norm_words(text):
        if tok:
            seen += 1
        idx = seen - 1
        if lo <= idx and (hi is None or idx < hi):
            out.append(word)
    return " ".join(out)


def _stream(segs: list) -> list:
    """[(segment index, index within segment, token, interpolated time)]"""
    out = []
    for p, seg in enumerate(segs):
        toks = _tokens(seg["text"])
        dur = seg["end"] - seg["start"]
        out += [(p, i, t, seg["start"] + (i + 0.5) / len(toks) * dur)
                for i, t in enumerate(toks)]
    return out


def _span(segs, stream, lo, hi):
    hosts = [segs[stream[i][0]] for i in range(lo, hi)]
    return min(s["start"] for s in hosts), max(s["end"] for s in hosts)


def _align(za, zb, ta, tb, max_dt: float, boundary: float):
    """Best run of identical tokens shared by the end of window k (`ta`) and
    the start of window k+1 (`tb`): (i0, j0, length) or None.

    A run must be the same speech, so the segments hosting its two copies
    must overlap in time (anchored runs, where window k's last words are
    window k+1's first, may just touch). Word times are interpolated and
    unreliable inside a segment stretched over silence, so they only gate
    2-word runs. Longest wins; among equal lengths (periodic repetition
    offers runs shifted by one period) the one whose copies agree best in
    time, then the one closest to the seam."""
    A = [x[2] for x in ta]
    B = [x[2] for x in tb]
    best, prev = None, [0] * (len(B) + 1)
    for i in range(len(A)):
        cur = [0] * (len(B) + 1)
        for j in range(len(B)):
            if A[i] == B[j]:
                cur[j + 1] = prev[j] + 1
        for j in range(len(B)):
            L = cur[j + 1]
            if not L or (i + 1 < len(A) and j + 1 < len(B) and A[i + 1] == B[j + 1]):
                continue  # not a maximal run
            i0, j0 = i - L + 1, j - L + 1
            anchored = i + 1 == len(A) and j0 == 0
            if L < (SEAM_MIN_ANCHORED if anchored else SEAM_MIN_RUN):
                continue
            dt = abs(ta[i0][3] - tb[j0][3])
            if L < SEAM_MIN_RUN and dt > max_dt:
                continue
            (a0, a1), (b0, b1) = _span(za, ta, i0, i0 + L), _span(zb, tb, j0, j0 + L)
            if min(a1, b1) - max(a0, b0) <= (-SEAM_TOUCH_S if anchored else 0.05):
                continue
            centre = abs((ta[i0][3] + ta[i][3] + tb[j0][3] + tb[j][3]) / 4 - boundary)
            key = (L, -dt, -centre)
            if best is None or key > best[0]:
                best = (key, i0, j0, L)
        prev = cur
    return best[1:] if best else None


def _choose_cut(ta, tb, i0, j0, L, boundary):
    """Any point inside the shared run is a valid cut. Prefer one that splits
    no segment's text, then the one nearest the seam boundary (each window
    is most reliable away from its edges), then the later one."""
    def at_boundary(stream, n):
        return n == 0 or n == len(stream) or stream[n][1] == 0
    best = None
    for q in range(L + 1):
        keep_a, drop_b = i0 + q, j0 + q
        splits = (not at_boundary(ta, keep_a)) + (not at_boundary(tb, drop_b))
        t = (ta[max(keep_a - 1, 0)][3] + tb[min(drop_b, len(tb) - 1)][3]) / 2
        key = (splits, abs(t - boundary), -q)
        if best is None or key < best[0]:
            best = (key, keep_a, drop_b)
    return best[1], best[2]


def _cut_zone(segs, stream, keep_from: int, keep_to: int, stats) -> list:
    """Keep tokens [keep_from, keep_to) of a zone; segments wholly outside
    are removed, a segment cut through is trimmed and re-timed by its
    share of words."""
    first = {}
    for k, (p, i, _, _) in enumerate(stream):
        first.setdefault(p, k)
    out = []
    for p, seg in enumerate(segs):
        n = len(_tokens(seg["text"]))
        f = first.get(p)
        if n == 0:
            if keep_from == 0 and keep_to == len(stream):
                out.append(seg)
            else:
                stats["removed_segments"] += 1
            continue
        lo, hi = max(keep_from - f, 0), min(keep_to - f, n)
        if hi <= lo:
            stats["removed_segments"] += 1
            continue
        if (lo, hi) != (0, n):
            dur = seg["end"] - seg["start"]
            seg = {**seg, "start": seg["start"] + lo / n * dur,
                   "end": seg["start"] + hi / n * dur,
                   "text": _slice_tokens(seg["text"], lo, hi), "_touched": True}
            stats["trimmed_segments"] += 1
        out.append(seg)
    return out


def _covered(seg, others) -> float:
    dur = max(seg["end"] - seg["start"], 1e-6)
    return sum(max(0.0, min(seg["end"], o["end"]) - max(seg["start"], o["start"]))
               for o in others) / dur


def _resolve_seam(tail: list, head: list, offset: float, overlap_s: float, stats: dict) -> None:
    """Join window k (end of `tail`) and window k+1 (`head`, starting at
    `offset`). Both windows transcribed the overlap [offset, offset +
    overlap_s], so their word streams are aligned on a shared run of words
    and cut once inside it: everything before the cut comes from window k,
    everything after from window k+1, so each word appears once and nothing
    in the overlap is dropped unless the other side has it. Without a text
    alignment, segments are resolved by time. Mutates both lists."""
    boundary = offset + overlap_s / 2
    ia = len(tail)
    while ia > 0 and tail[ia - 1]["end"] > offset - overlap_s / 2:
        ia -= 1
    jb = 0
    while jb < len(head) and head[jb]["start"] < offset + 1.5 * overlap_s:
        jb += 1
    za, zb = tail[ia:], head[:jb]
    ta, tb = _stream(za), _stream(zb)
    found = _align(za, zb, ta, tb, max(overlap_s / 2, 3.0), boundary) if ta and tb else None
    if found:
        keep_a, drop_b = _choose_cut(ta, tb, *found, boundary)
        za = _cut_zone(za, ta, 0, keep_a, stats)
        zb = _cut_zone(zb, tb, drop_b, len(tb), stats)
    else:
        kept_a = []
        for seg in za:
            if (seg["start"] + seg["end"]) / 2 >= boundary and _covered(seg, zb) >= SEAM_COVERED:
                stats["removed_segments"] += 1
            else:
                kept_a.append(seg)
        za = kept_a
        kept_b = []
        for seg in zb:
            if (seg["start"] + seg["end"]) / 2 < boundary and _covered(seg, za) >= SEAM_COVERED:
                stats["removed_segments"] += 1
            else:
                kept_b.append(seg)
        zb = kept_b
    tail[ia:] = za
    head[:jb] = zb
    # No kept segment may overlap what precedes it by more than the tolerance,
    # and starts stay in text order (a clamped segment must not overtake the next).
    prev = max((s["end"] for s in tail), default=None)
    floor = max((s["start"] for s in tail), default=float("-inf"))
    k = 0
    while prev is not None and k < len(head) and (
            head[k]["start"] < prev - SEAM_OVERLAP_TOL_S or head[k]["start"] < floor):
        seg = head[k]
        if not found and seg["end"] <= prev + 0.01:
            del head[k]          # wholly inside time window k already transcribed
            stats["removed_segments"] += 1
            continue
        if not seg.get("_touched"):
            stats["retimed_segments"] += 1
        seg["start"] = prev
        seg["end"] = max(seg["end"], prev + 0.01)
        seg["_touched"] = True
        prev, floor = seg["end"], seg["start"]
        k += 1


def _join_windows(groups: list, overlap_s: float, total_s: float | None,
                  stats: dict | None = None) -> list:
    """`groups`: ordered [(offset, [segments with absolute times])]."""
    stats = stats if stats is not None else {}
    for key in SEAM_COUNTS:
        stats.setdefault(key, 0)
    out: list = []
    for k, (offset, segs) in enumerate(groups):
        head = [dict(s) for s in segs]
        if k:
            _resolve_seam(out, head, offset, overlap_s, stats)
        out.extend(head)
    final = []
    floor = 0.0
    for seg in out:
        start = max(0.0, seg["start"])
        end = min(seg["end"], total_s) if total_s is not None else seg["end"]
        if total_s is not None and total_s < start <= total_s + TAIL_SLACK_S:
            start = max(total_s - 0.01, floor)   # keeps the text, in order, at the end
        floor = max(floor, start)
        start, end = round(start, 2), round(end, 2)
        if end <= start:
            end = round(start + 0.01, 2)
        final.append({"start": start, "end": end, "text": seg["text"],
                      **({"group": seg["group"]} if "group" in seg else {})})
    final.sort(key=lambda s: (s["start"], s["end"]))
    return final


def stitch_chunks(chunks: list, overlap_s: float, total_s: float | None = None,
                  stats: dict | None = None) -> list:
    """Merge per-window segment lists into one absolute timeline.

    `chunks` is an ordered list of {"offset": s, "segments": [...]} with
    window-relative times; consecutive windows overlap by `overlap_s`.
    Every non-empty segment of every window is kept, and each seam is then
    resolved at the word level by `_resolve_seam`: the two transcriptions
    of the overlap are aligned on shared words and cut once, so speech in
    the overlap is neither duplicated nor dropped.
    """
    groups = []
    for chunk in chunks:
        off = float(chunk["offset"])
        segs = []
        for seg in chunk["segments"]:
            text = str(seg.get("text", "")).strip()
            if text:
                segs.append({"start": off + float(seg["start"]),
                             "end": off + float(seg["end"]), "text": text,
                             **({"group": seg["group"]} if "group" in seg else {})})
        groups.append((off, segs))
    return _join_windows(groups, overlap_s, total_s, stats)


def repair_seams(segments: list, offsets: list, overlap_s: float) -> tuple:
    """Re-run seam resolution on a transcript stitched by an older algorithm
    (midpoint ownership, or seam repair v1/v2), given its window offsets. Segments are returned to
    the window that owned them; a segment whose rounded midpoint is within
    rounding of a boundary is tried on both sides and goes where it
    resolves more. Returns (segments, counts); idempotent."""
    bounds = [float(o) + overlap_s / 2 for o in offsets[1:]]
    owner, ambiguous = [], []
    for n, seg in enumerate(segments):
        mid = (seg["start"] + seg["end"]) / 2
        owner.append(sum(mid >= b for b in bounds))
        near = [k for k, b in enumerate(bounds) if abs(mid - b) <= _OWNER_TOL_S]
        if near:
            ambiguous.append((n, near[0]))

    def run(own):
        groups = [(float(o), []) for o in offsets]
        for seg, k in zip(segments, own):
            groups[k][1].append(dict(seg))
        stats: dict = {}
        return _join_windows(groups, overlap_s, None, stats), stats

    best = run(owner)
    for n, k in ambiguous:
        for side in (k, k + 1):
            if side == owner[n]:
                continue
            trial_owner = owner[:n] + [side] + owner[n + 1:]
            trial = run(trial_owner)
            if sum(trial[1].values()) > sum(best[1].values()) or (
                    sum(trial[1].values()) == sum(best[1].values()) and side == k):
                best, owner = trial, trial_owner
    return best


# Whisper's language codes (openai-whisper tokenizer.LANGUAGES), and RSS
# aliases for some of them.
WHISPER_LANGUAGES = frozenset(
    "en zh de es ru ko fr ja pt tr pl ca nl ar sv it id hi fi vi he uk el ms cs ro da hu ta "
    "no th ur hr bg lt la mi ml cy sk te fa lv bn sr az sl kn et mk br eu is hy ne mn bs kk "
    "sq sw gl mr pa si km sn yo so af oc ka be tg sd gu am yi lo uz fo ht ps tk nn mt sa lb "
    "my bo tl mg as tt haw ln ha ba jw su yue".split())
_LANGUAGE_ALIASES = {"nb": "no", "iw": "he", "in": "id", "ji": "yi", "jv": "jw", "fil": "tl"}
# With a show-language hint, a window is decoded in the hint's language
# unless at least HINT_OVERRIDE_SHARE of its probes voted one other language
# at a mean probability of HINT_OVERRIDE_PROBABILITY or more (an English
# interview on a German show: tiny says en at 0.9+; an English show wrongly
# tagged es with one probe on music: 2 of 3 en at 0.9+; The Moth's English
# story voted Welsh at 0.64 / 0.56 and stays English).
HINT_OVERRIDE_PROBABILITY = 0.8
HINT_OVERRIDE_SHARE = 2 / 3


def whisper_language(raw) -> str | None:
    """Whisper code of a language tag ("en-US", "EN", "de-de" -> "en", "en",
    "de"); None unless it is a clear code Whisper knows."""
    if not isinstance(raw, str):
        return None
    m = re.fullmatch(r"([a-z]{2,3})(?:[-_][a-z0-9]{1,8})*", raw.strip().lower())
    if not m:
        return None
    code = _LANGUAGE_ALIASES.get(m.group(1), m.group(1))
    return code if code in WHISPER_LANGUAGES else None


def majority_language(votes) -> tuple:
    """(language, share of probes, mean probability of those probes) for the
    language most probes voted; (None, 0.0, 0.0) without votes."""
    votes = [v for v in votes or () if v.get("language")]
    if not votes:
        return None, 0.0, 0.0
    langs = [v["language"] for v in votes]
    best = max(dict.fromkeys(langs), key=langs.count)
    probs = [float(v.get("probability") or 0.0) for v in votes if v["language"] == best]
    return best, len(probs) / len(votes), sum(probs) / len(probs)


def decide_language(vote: dict | None, hint: str | None) -> tuple:
    """(language to decode with, "vote" | "hint") for one window: the hint,
    unless a confident majority of the probes voted another language."""
    if hint is None:
        return (vote or {}).get("language"), "vote"
    lang, share, prob = majority_language((vote or {}).get("votes"))
    if lang is not None and lang != hint and share >= HINT_OVERRIDE_SHARE - 1e-9 \
            and prob >= HINT_OVERRIDE_PROBABILITY:
        return lang, "vote"
    return hint, "hint"


def vote_language(model, audio, windows: int) -> dict:
    """Pick a window's language by summing detection probabilities over
    `windows` 30 s probes spread evenly across it."""
    n = len(audio)
    probe_n = int(PROBE_S * SAMPLE_RATE)
    windows = max(1, windows)
    if n <= probe_n:
        starts = [0]
    else:
        starts = sorted({int((n - probe_n) * (2 * i + 1) / (2 * windows)) for i in range(windows)})
    score = defaultdict(float)
    votes = []
    for s in starts:
        lang, prob, _ = model.detect_language(audio=audio[s:s + probe_n])
        score[lang] += prob
        votes.append({"at_s": round(s / SAMPLE_RATE, 1), "language": lang,
                      "probability": round(float(prob), 3)})
    best = max(score, key=score.get)
    return {"language": best, "probability": round(score[best] / len(starts), 3),
            "votes": votes}


def load_model(model_name: str, threads: int):
    from faster_whisper import WhisperModel
    kwargs = dict(device="cpu", compute_type="int8", cpu_threads=threads, num_workers=1)
    try:
        return WhisperModel(model_name, local_files_only=True, **kwargs)
    except Exception:
        return WhisperModel(model_name, **kwargs)


MLX_REPOS = {
    "tiny": "mlx-community/whisper-tiny",
    "small": "mlx-community/whisper-small-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
}


def mlx_repo(model_name: str) -> str:
    """Map a faster-whisper style model name to an mlx-community repo;
    a full `org/name` repo id passes through unchanged."""
    if "/" in model_name:
        return model_name
    return MLX_REPOS.get(model_name, f"mlx-community/whisper-{model_name}")


class MlxWhisperModel:
    """Apple-GPU backend exposing the subset of faster-whisper's
    `WhisperModel` API that `transcribe_file` and `vote_language` use, so
    decode, stitching and language voting are shared code."""

    def __init__(self, repo: str):
        import mlx_whisper
        self.mlx_whisper = mlx_whisper
        self.repo = repo
        self._lang = None
        try:  # internal API; without it, mlx_whisper detects per chunk itself
            import mlx.core as mx
            from mlx_whisper.audio import N_FRAMES, N_SAMPLES, log_mel_spectrogram, pad_or_trim
            from mlx_whisper.transcribe import ModelHolder
            self._lang = (mx, N_FRAMES, N_SAMPLES, log_mel_spectrogram, pad_or_trim, ModelHolder)
        except (ImportError, AttributeError):
            pass

    @property
    def can_detect_language(self) -> bool:
        return self._lang is not None

    def detect_language(self, audio):
        """Same probe as mlx_whisper.transcribe's own first-30 s detection."""
        mx, n_frames, n_samples, log_mel, pad_or_trim, holder = self._lang
        model = holder.get_model(self.repo, mx.float16)  # shared with transcribe()
        if not model.is_multilingual:
            return "en", 1.0, None
        mel = log_mel(audio, n_mels=model.dims.n_mels, padding=n_samples)
        _, probs = model.detect_language(pad_or_trim(mel, n_frames, axis=-2).astype(mx.float16))
        best = max(probs, key=probs.get)
        return best, float(probs[best]), probs

    def transcribe(self, audio, *, beam_size=1, language=None, vad_filter=False,
                   condition_on_previous_text=False):
        from types import SimpleNamespace
        if beam_size != 1:
            raise ValueError("mlx-whisper backend supports greedy decoding only (beam 1)")
        result = self.mlx_whisper.transcribe(
            audio, path_or_hf_repo=self.repo, language=language,
            condition_on_previous_text=condition_on_previous_text,
            word_timestamps=False, verbose=None)
        segments = [SimpleNamespace(start=s["start"], end=s["end"], text=s["text"])
                    for s in result["segments"]]
        return segments, SimpleNamespace(language=result.get("language") or language,
                                         language_probability=None)


def mlx_versions() -> dict:
    from importlib import metadata
    out = {}
    for dist in ("mlx-whisper", "mlx"):
        try:
            out[dist.replace("-", "_")] = metadata.version(dist)
        except metadata.PackageNotFoundError:
            out[dist.replace("-", "_")] = None
    return out


PARAKEET_DEFAULT_REPO = "mlx-community/parakeet-tdt-0.6b-v3"
PARAKEET_DTYPE = "bfloat16"
# parakeet-mlx SentenceConfig: without these a sentence (= transcript segment)
# runs across music beds until the next full stop (seen: a 29 s segment of six
# words around a 26.9 s gap), which inflates detect's ad windows.
PARAKEET_SENTENCE = {"silence_gap": 1.5, "max_duration": 30.0}
# Windows whose peak stays below the floor are silence and not decoded:
# Parakeet hallucinates "Yeah."/"Okay." on near-silent audio. Short windows
# hallucinate even at -80 dBFS (about 20 of 48 trials at 0.3-1 s), so they get
# -60 dBFS; long windows -80 dBFS, so quiet speech in a full window is never
# skipped. A window is short only when the whole episode is, or when the
# overlap is under PARAKEET_SHORT_WINDOW_S (the last window is the remainder
# plus the overlap).
PARAKEET_SILENCE_PEAK = 1e-4          # -80 dBFS
PARAKEET_SILENCE_PEAK_SHORT = 1e-3    # -60 dBFS, windows under PARAKEET_SHORT_WINDOW_S
PARAKEET_SHORT_WINDOW_S = 15.0


_OPENERS = set("([{\"'“‘¿¡$£€#@")  # punctuation that leads a word


def tokens_to_words(tokens) -> list:
    """Parakeet's aligned tokens are sub-word pieces; a piece that starts
    with a space starts a new word, and so does the piece after a space-only
    piece (Parakeet emits " " before digits: " the", " ", "9", "0" is "the",
    "90"). Punctuation never changes a word's times and no word contains
    whitespace: a punctuation piece joins the word before it ("well-",
    "end."), except that a space-led opener (" (", " $") or a piece the next
    word continues directly (" '", "em" is "'em") joins the word after it, as
    does punctuation leading a sentence. So every word carries a token and
    seam stitching never drops punctuation. [{start, end, word}]"""
    words, gap, held = [], None, None  # gap: start of a space-only piece; held: " ("-like piece
    for t in tokens:
        if not t.text.strip():
            gap = t.start if gap is None else gap
            continue
        if re.search(r"\w", t.text):
            continues = not t.text.startswith(" ") and gap is None
            if held is not None and words and not continues \
                    and not set(held["word"]) <= _OPENERS:
                words[-1]["word"] += held["word"]
                held = None
            if held is not None or not words or not continues:
                words.append({"start": t.start if gap is None else gap, "end": t.end,
                              "word": (held["word"] if held else "") + t.text.strip()})
            else:
                words[-1]["word"] += t.text.strip()
                words[-1]["end"] = t.end
            held = None
        elif held is not None:
            held["word"] += t.text.strip()
        elif t.text.startswith(" ") or gap is not None or not words:
            held = {"start": t.start, "end": t.end, "word": t.text.strip()}
        else:
            words[-1]["word"] += t.text.strip()
        gap = None
    if held is not None:
        if words:
            words[-1]["word"] += held["word"]
        else:
            words.append(held)
    return [w for w in words if w["word"]]


def file_sha256(path) -> str:
    with open(path, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def parakeet_snapshot(repo: str, revision: str | None = None, *,
                      local_only: bool = False) -> dict:
    """{"path": model dir, "revision": HF commit or None, "weights": its
    model.safetensors} for an HF repo id (HF cache, downloaded on first use;
    offline the cached copy) or a local directory (revision None).
    `local_only` never touches the network (children of a pinned worker)."""
    if Path(repo).is_dir():
        d = Path(repo).resolve()
        return {"path": str(d), "revision": None, "weights": str(d / "model.safetensors")}
    from huggingface_hub import hf_hub_download
    kw = {"revision": revision, "local_files_only": local_only}
    hf_hub_download(repo, "config.json", **kw)
    weights = Path(hf_hub_download(repo, "model.safetensors", **kw))
    snap = weights.parent
    return {"path": str(snap),
            "revision": snap.name if snap.parent.name == "snapshots" else revision,
            "weights": str(weights)}


def parakeet_pin(repo: str, revision: str | None = None) -> dict:
    """Resolve a Parakeet model once per worker (its preflight): snapshot,
    revision (`revision`, else the latest), and the weights' sha256 with the
    file stat it was taken at. Children load exactly this revision, offline,
    and reuse the digest."""
    snap = parakeet_snapshot(repo, revision)
    st = os.stat(snap["weights"])
    return {"repo": repo, **snap, "weights_sha256": file_sha256(snap["weights"]),
            "weights_size": st.st_size, "weights_mtime_ns": st.st_mtime_ns}


def weights_sha256(path, pin: dict | None = None) -> str:
    """sha256 of a weights file. Hashed, not read off the HF cache blob name:
    under huggingface_hub 2.x the per-repo blob is a symlink to a shared
    Xet-named blob, so realpath's name is not the file digest. A pin's digest
    is reused while the file at that path keeps its size and mtime."""
    st = os.stat(path)
    if pin and pin.get("weights_sha256") and (
            pin.get("weights"), pin.get("weights_size"), pin.get("weights_mtime_ns")) == (
            str(path), st.st_size, st.st_mtime_ns):
        return pin["weights_sha256"]
    return file_sha256(path)


class ParakeetModel:
    """NVIDIA Parakeet TDT on the Apple GPU (parakeet-mlx), exposing the
    subset of faster-whisper's `WhisperModel` API that `transcribe_file`
    uses, so decode, windowing and seam stitching are shared code. Each
    window is one `generate` call (what parakeet-mlx's own chunked
    transcribe does per chunk); its native token timestamps become
    word timestamps. No language detection: transcripts carry language
    None. `pin` (parakeet_pin, from the worker's preflight) fixes the
    revision and the weights digest; without it the model resolves as
    usual and the weights are hashed here."""
    can_detect_language = False
    word_timestamps = True  # transcribe_file derives segments from stitched words

    def __init__(self, repo: str = PARAKEET_DEFAULT_REPO, dtype: str = PARAKEET_DTYPE,
                 pin: dict | None = None):
        import mlx.core as mx
        import parakeet_mlx
        from parakeet_mlx.audio import get_logmel
        self.mx, self.get_logmel = mx, get_logmel
        self.repo, self.dtype = repo, dtype
        if pin and pin.get("repo") != repo:
            pin = None
        snap = (parakeet_snapshot(repo, pin.get("revision"), local_only=True) if pin
                else parakeet_snapshot(repo))
        self.revision = snap["revision"]
        self.weights_sha256 = weights_sha256(snap["weights"], pin)
        self.decoding_config = parakeet_mlx.DecodingConfig(
            sentence=parakeet_mlx.SentenceConfig(**PARAKEET_SENTENCE))
        self.model = parakeet_mlx.from_pretrained(snap["path"], dtype=getattr(mx, dtype))
        self.version = getattr(parakeet_mlx, "__version__", None) or _dist_version("parakeet-mlx")

    def transcribe(self, audio, *, beam_size=1, language=None, vad_filter=False,
                   condition_on_previous_text=False):
        from types import SimpleNamespace
        if beam_size != 1:
            raise ValueError("parakeet backend supports greedy decoding only (beam 1)")
        import numpy as np
        info = SimpleNamespace(language=None, language_probability=None)
        floor = PARAKEET_SILENCE_PEAK_SHORT if len(audio) < PARAKEET_SHORT_WINDOW_S * SAMPLE_RATE \
            else PARAKEET_SILENCE_PEAK
        if len(audio) < self.model.preprocessor_config.hop_length \
                or float(np.max(np.abs(audio))) < floor:
            return [], info
        mel = self.get_logmel(self.mx.array(audio), self.model.preprocessor_config)
        result = self.model.generate(mel, decoding_config=self.decoding_config)[0]
        self.mx.clear_cache()  # freed window buffers back to the OS between windows
        sentences = []
        for s in result.sentences:
            if sentences and not re.search(r"\w", s.text):
                # a punctuation-only sentence (a stray "." alone in silence,
                # split off by silence_gap) belongs to the sentence before it
                sentences[-1]["text"] += s.text
                sentences[-1]["tokens"] += list(s.tokens)
            else:
                sentences.append({"start": s.start, "end": s.end, "text": s.text,
                                  "tokens": list(s.tokens)})
        segments = [SimpleNamespace(start=s["start"], end=s["end"], text=s["text"],
                                    words=[SimpleNamespace(**w)
                                           for w in tokens_to_words(s["tokens"])])
                    for s in sentences]
        return segments, info


def _dist_version(dist: str):
    from importlib import metadata
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return None


def transcribe_file(path, *, model, chunk_s: float = 600.0, overlap_s: float = 20.0,
                    beam: int = 1, lang_windows: int = 3,
                    condition_on_previous_text: bool = False,
                    ffmpeg: str | None = None, progress=None,
                    language_hint: str | None = None) -> dict:
    """Transcribe `path` window by window; returns segments + per-window
    languages + the decoded duration. A backend with word timestamps
    (`model.word_timestamps`, Parakeet) also yields "words" (always, [] when
    nothing was said): words are seam-stitched and the segments are rebuilt
    from the surviving words, so both agree through every seam.

    `language_hint` (a Whisper code, e.g. the show's RSS language): each
    window decodes in it unless a confident majority of its probes voted
    another language (decide_language); its chunk_languages entry then records
    "hint", "decided_by" and the vote as "detected". Backends without
    language detection ignore it."""
    hint = whisper_language(language_hint) if getattr(model, "can_detect_language", True) \
        else None
    chunks = []
    word_chunks = []
    languages = []
    stats: dict = {}
    for k, (offset, audio) in enumerate(iter_windows(path, chunk_s, overlap_s, ffmpeg, stats)):
        vote = lang_windows > 0 and getattr(model, "can_detect_language", True)
        lang = vote_language(model, audio, lang_windows) if vote else None
        decided = lang["language"] if lang else None
        if hint is not None:
            decided, by = decide_language(lang, hint)
            lang = {**(lang or {"votes": [], "probability": None}), "language": decided,
                    "detected": (lang or {}).get("language"), "hint": hint, "decided_by": by}
        segments, info = model.transcribe(
            audio, beam_size=beam, language=decided,
            vad_filter=False, condition_on_previous_text=condition_on_previous_text)
        segs, words = [], []
        for j, s in enumerate(segments):  # faster-whisper yields a one-shot generator
            segs.append({"start": s.start, "end": s.end, "text": s.text})
            words += [{"start": w.start, "end": w.end, "text": w.word, "group": (k, j)}
                      for w in getattr(s, "words", None) or ()]
        word_chunks.append({"offset": offset, "segments": words})
        if lang is None:
            prob = info.language_probability
            lang = {"language": info.language, "votes": [],
                    "probability": None if prob is None else round(float(prob), 3)}
        languages.append({"chunk": k, "offset_s": round(offset, 2),
                          "length_s": round(len(audio) / SAMPLE_RATE, 2), **lang})
        chunks.append({"offset": offset, "segments": segs})
        if progress:
            progress(k, offset, len(audio) / SAMPLE_RATE)
    decoded = stats.get("decoded_s", 0.0)
    weight = defaultdict(float)
    for c in languages:
        weight[c["language"]] += c["length_s"]
    out = {"chunk_languages": languages,
           "language": max(weight, key=weight.get) if weight else None,
           "decoded_duration_s": round(decoded, 3)}
    if getattr(model, "word_timestamps", False):
        # Each word is its own one-token segment, so the seam alignment runs
        # on exact word times; a segment is then its window segment's words
        # that survived the seam (one cut decides both).
        words = stitch_chunks(word_chunks, overlap_s, decoded)
        out["words"] = [{"start": w["start"], "end": w["end"], "word": w["text"]}
                        for w in words]
        out["segments"] = words_to_segments(words)
    else:
        out["segments"] = stitch_chunks(chunks, overlap_s, decoded)
    return out


def words_to_segments(words: list) -> list:
    """Consecutive stitched words of one window segment ("group") -> one
    segment spanning them."""
    segs = []
    for w in words:
        if segs and segs[-1]["group"] == w["group"]:
            segs[-1]["end"] = w["end"]
            segs[-1]["text"] += " " + w["text"]
        else:
            segs.append({"group": w["group"], "start": w["start"], "end": w["end"],
                         "text": w["text"]})
    return [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in segs]


def duration_tolerance_s(probed_s: float) -> float:
    return max(0.02 * probed_s, 5.0)


def validate_transcript(doc, probed_duration_s: float | None) -> None:
    """Raise TranscriptInvalid unless `doc` is a complete transcript."""
    if not isinstance(doc, dict) or not isinstance(doc.get("segments"), list):
        raise TranscriptInvalid("transcript lacks a segments list")
    for key in ("ep_id", "model", "transcribed_at", "duration_min"):
        if key not in doc:
            raise TranscriptInvalid(f"transcript lacks {key}")
    decoded = (doc.get("provenance") or {}).get("decoded_duration_s")
    if not isinstance(decoded, (int, float)) or decoded <= 0:
        raise TranscriptInvalid("transcript lacks a positive decoded duration")
    if probed_duration_s:
        if abs(decoded - probed_duration_s) > duration_tolerance_s(probed_duration_s):
            raise TranscriptInvalid(f"decoded {decoded:.1f}s vs probed {probed_duration_s:.1f}s")
    prev = -1.0
    for i, s in enumerate(doc["segments"]):
        if not isinstance(s, dict) or not isinstance(s.get("text"), str):
            raise TranscriptInvalid(f"segment {i} malformed")
        start, end = s.get("start"), s.get("end")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (start, end)):
            raise TranscriptInvalid(f"segment {i} bounds not numeric")
        if start < prev:
            raise TranscriptInvalid(f"segment {i} out of order")
        if start < 0 or end <= start or end > decoded + 1.0:
            raise TranscriptInvalid(f"segment {i} bounds outside 0..{decoded:.1f}s")
        prev = start
    if "words" in doc:
        _validate_words(doc["words"], decoded)


def _validate_words(words, decoded: float) -> None:
    if not isinstance(words, list):
        raise TranscriptInvalid("words is not a list")
    prev = -1.0
    for i, w in enumerate(words):
        if not isinstance(w, dict) or not isinstance(w.get("word"), str):
            raise TranscriptInvalid(f"word {i} malformed")
        start, end = w.get("start"), w.get("end")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (start, end)):
            raise TranscriptInvalid(f"word {i} bounds not numeric")
        if start < prev:
            raise TranscriptInvalid(f"word {i} out of order")
        if start < 0 or end < start or end > decoded + 1.0:
            raise TranscriptInvalid(f"word {i} bounds outside 0..{decoded:.1f}s")
        prev = start


def atomic_write_json(path, obj) -> None:
    """Temp file in the same directory, fsync, os.replace, fsync dir."""
    from earwitness.pipeline.acquisition import _fsync_dir
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(obj, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)
