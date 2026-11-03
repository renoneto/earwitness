"""Transcript quality checks and repair for the corpus runner.

Two checks run on every transcript at transcribe time (`assess`, stored as
the transcript's "quality" record):

* **Repeat loop**: a phrase of 1-8 words repeated back to back at least 3
  times, covering at least LOOP_MIN_WORDS words. Whisper (tiny) invents
  these over real speech; Parakeet's largest real repeat in the health
  check was 16 words, so 20 sits above real speech.
* **Window language**: a transcribe window whose language was voted
  without the show's language hint, differs from both the show's RSS
  language and the episode's language, and holds at least
  LANGUAGE_MIN_WORDS words (The Moth's 9-minute Welsh window). Needs the
  show language; an episode wholly in another language (an English
  interview on a German show) is not a window failure. A window decoded in
  the hint's language while most of its probes voted another one (a "hint
  conflict": the RSS tag may be wrong, e.g. an English show tagged es) is
  flagged too, with the same word floor.

A flag is repaired (`repair_doc`) by re-transcribing its stretch +-
REPAIR_PAD_S with a stronger backend (Parakeet by default) from the
episode audio, or for done episodes from a kept evidence clip that covers
the whole stretch, and splicing the new text in over the stretch: the
original segments whose midpoint falls inside it are replaced by the new
ones (marked "src": "repair"), and the replaced text is kept in
provenance "repairs".
"""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from earwitness.pipeline import chunked_stt as stt

QUALITY_VERSION = 1
LOOP_MIN_WORDS = 20      # health check: Parakeet max 16; 13.8% of tiny episodes reach 20
LOOP_MAX_PHRASE = 8
LOOP_MIN_REPEATS = 3
LANGUAGE_MIN_WORDS = 100
REPAIR_PAD_S = 30.0
REPAIR_BACKENDS = ("parakeet", "mlx")
REPAIR_CHUNKS = {"parakeet": (120.0, 15.0), "mlx": (600.0, 20.0)}  # (chunk_s, overlap_s)

# Loop tokens: words with their intra-word punctuation (e-commerce, 1,000,
# o'neil's) in any script; CJK characters (no spaces between words) count
# one each.
_CJK = "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_W = rf"[^\W{_CJK}]"
_TOK = re.compile(rf"[{_CJK}]|{_W}+(?:['’\-.,]{_W}+)*")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- language

def language_code(raw) -> str | None:
    """The Whisper language code of an RSS <language> value ("en-US",
    "EN", "de-de" -> "en", "en", "de"), or None when it is not a clear code
    ("English", "", None, an unknown code)."""
    return stt.whisper_language(raw)


# ---------------------------------------------------------------- loops

def _timed_tokens(doc) -> tuple:
    """(tokens, [(start, end) of the item hosting each token]): normalized
    lowercase tokens from the words list when present, else the segments."""
    toks, spans = [], []
    items = doc.get("words") if doc.get("words") else doc.get("segments") or []
    key = "word" if doc.get("words") else "text"
    for it in items:
        for t in _TOK.findall(str(it.get(key, "")).lower()):
            toks.append(t)
            spans.append((float(it["start"]), float(it["end"])))
    return toks, spans


def _loops(toks: list, kmax: int = LOOP_MAX_PHRASE, min_rep: int = LOOP_MIN_REPEATS) -> list:
    """[(first token, last token, k, reps, words)] runs where a k-gram repeats
    back to back at least min_rep times (maximal per start; a k-gram that is
    itself a repetition of a shorter unit is left to that unit), largest
    first, without overlaps. Same method as the transcription health check."""
    ids: dict = {}
    arr = [ids.setdefault(t, len(ids)) for t in toks]
    n, found = len(arr), []
    for k in range(1, kmax + 1):
        i = 0
        while i + k < n:
            if arr[i] != arr[i + k]:
                i += 1
                continue
            j = i
            while j + k < n and arr[j] == arr[j + k]:
                j += 1
            matched = j - i
            reps = matched // k + 1
            phrase = arr[i:i + k]
            if reps >= min_rep and not any(
                    k % d == 0 and phrase == phrase[:d] * (k // d) for d in range(1, k)):
                found.append((i, j + k - 1, k, reps, matched + k))
            i = j + 1
    found.sort(key=lambda f: (-f[4], f[0]))
    kept = []
    for f in found:
        if all(f[1] < g[0] or f[0] > g[1] for g in kept):
            kept.append(f)
    return sorted(kept)


def find_loops(doc, min_words: int = LOOP_MIN_WORDS) -> list:
    """Repeat loops covering at least `min_words` words: [{kind, start_s,
    end_s, words, reps, phrase}], start/end the bounds of the segments (or
    words) hosting the loop's first and last token."""
    toks, spans = _timed_tokens(doc)
    out = []
    for a, b, k, reps, words in _loops(toks):
        if words >= min_words:
            out.append({"kind": "loop", "start_s": round(spans[a][0], 2),
                        "end_s": round(spans[b][1], 2), "words": words, "reps": reps,
                        "phrase": " ".join(toks[a:a + k])})
    return out


def effective_word_count(segments) -> int:
    """Words in `segments` with each repeat loop of LOOP_MIN_WORDS or more
    counted once (its phrase), so invented repetition does not count as
    speech."""
    toks, _ = _timed_tokens({"segments": list(segments or ())})
    excess = sum(words - k for _, _, k, _, words in _loops(toks) if words >= LOOP_MIN_WORDS)
    return len(toks) - excess


# ---------------------------------------------------------------- window language

def _words_between(segments, a: float, b: float) -> int:
    return sum(len(s["text"].split()) for s in segments
               if a <= (s["start"] + s["end"]) / 2 < b)


def language_flags(doc, show_language, min_words: int = LANGUAGE_MIN_WORDS) -> list:
    """Transcribe windows voted into another language than the show's and the
    episode's ("reason": "window_language"), and windows decoded in the
    show-language hint while most probes voted another language
    ("hint_conflict"), each with at least `min_words` words; nothing is
    flagged without a show language."""
    show = language_code(show_language)
    if show is None:
        return []
    episode = language_code(doc.get("language"))
    out = []
    windows = (doc.get("provenance") or {}).get("chunk_languages") or []
    segs = doc.get("segments") or []
    for w in windows:
        lang, reason = w.get("language"), "window_language"
        if w.get("hint"):
            if w.get("decided_by") != "hint":
                continue
            lang, share, _ = stt.majority_language(w.get("votes"))
            if lang is None or lang == w["hint"] or share <= 0.5:
                continue
            reason = "hint_conflict"
        elif not lang or language_code(lang) in (show, episode):
            continue
        a = float(w.get("offset_s") or 0.0)
        b = a + float(w.get("length_s") or 0.0)
        n = _words_between(segs, a, b)
        if n >= min_words:
            out.append({"kind": "language", "start_s": round(a, 2), "end_s": round(b, 2),
                        "words": n, "language": lang, "show_language": show,
                        "reason": reason})
    return out


# ---------------------------------------------------------------- the record

def assess(doc, show_language=None) -> dict:
    """The transcript's quality record: every loop / language flag with
    status "flagged"."""
    flags = sorted(find_loops(doc) + language_flags(doc, show_language),
                   key=lambda f: (f["start_s"], f["end_s"]))
    for i, f in enumerate(flags):
        f.update(id=i, status="flagged")
    return {"version": QUALITY_VERSION, "loop_min_words": LOOP_MIN_WORDS,
            "language_min_words": LANGUAGE_MIN_WORDS,
            "show_language": show_language or None, "flags": flags}


def quality_of(doc, show_language=None) -> dict:
    """The transcript's quality record, stored on `doc`: assessed now when it
    has none of this version; a record made without a show language gets
    its language flags once `show_language` is known."""
    q = doc.get("quality")
    if not (isinstance(q, dict) and q.get("version") == QUALITY_VERSION
            and isinstance(q.get("flags"), list)):
        doc["quality"] = assess(doc, show_language)
        return doc["quality"]
    if show_language and not q.get("show_language"):
        q["show_language"] = show_language
        nxt = max((f.get("id", -1) for f in q["flags"]), default=-1) + 1
        for i, f in enumerate(language_flags(doc, show_language)):
            q["flags"].append({**f, "id": nxt + i, "status": "flagged"})
    return q


def pending(quality) -> list:
    """Flags still to repair (not repaired yet)."""
    return [f for f in (quality or {}).get("flags") or () if f.get("status") != "repaired"]


# ---------------------------------------------------------------- repair

def _bounds(g: dict, duration_s, pad_s: float) -> dict:
    g["a"] = max(0.0, g["lo"] - pad_s)
    g["b"] = g["hi"] + pad_s if not duration_s else min(float(duration_s), g["hi"] + pad_s)
    return g


def _source_key(src: dict):
    return (src["kind"], src.get("path")) if src.get("kind") else None


def plan(flags: list, duration_s: float | None, pad_s: float = REPAIR_PAD_S,
         source_for=None) -> list:
    """Flags grouped for one re-transcription each: flags whose padded
    stretches overlap share one. [{"flags", "lo", "hi", "a", "b"}]: [lo, hi]
    is replaced, [a, b] (+-pad, inside the audio) is decoded. With
    `source_for`, only flags with the same source (the same clip, or the
    episode audio) share a group, and each group carries its "src" (so two
    close loops in different clips are both covered)."""
    groups = []
    for f in sorted(flags, key=lambda f: (f["start_s"], f["end_s"])):
        lo, hi = float(f["start_s"]), float(f["end_s"])
        key = None
        if source_for is not None:
            key = _source_key(source_for(_bounds({"lo": lo, "hi": hi}, duration_s, pad_s)))
        if groups and lo - groups[-1]["hi"] < 2 * pad_s and (
                source_for is None or (key is not None and key == groups[-1]["key"])):
            g = groups[-1]
            g["flags"].append(f)
            g["hi"] = max(g["hi"], hi)
        else:
            groups.append({"flags": [f], "lo": lo, "hi": hi, "key": key})
    for g in groups:
        g.pop("key")
        _bounds(g, duration_s, pad_s)
        if source_for is not None:
            g["src"] = source_for(g)
    return groups


def _mid(s) -> float:
    return (float(s["start"]) + float(s["end"])) / 2


def splice(doc, lo: float, hi: float, segments: list, words: list | None,
           record: dict) -> dict:
    """Replace the transcript's text over [lo, hi): original segments (and
    words, when the transcript has them) whose midpoint falls inside are
    removed and the new ones whose midpoint falls inside take their place,
    marked "src": "repair". New segments with words are first trimmed to
    their words inside the stretch. `record` (backend, source, ...) is
    appended to provenance "repairs" with the removed segments and counts."""
    inside = lambda s: lo <= _mid(s) < hi  # noqa: E731
    removed = [s for s in doc["segments"] if inside(s)]
    new_segs, taken = [], set()
    for s in segments:
        sw = [(i, w) for i, w in enumerate(words or ()) if i not in taken
              and s["start"] - 1e-6 <= w["start"] <= s["end"] + 1e-6]
        taken.update(i for i, _ in sw)  # a word on a shared boundary goes to one segment
        sw = [w for _, w in sw]
        if sw:
            sw = [w for w in sw if inside(w)]
            if sw:
                new_segs.append({"start": sw[0]["start"],
                                 "end": max(sw[-1]["end"], round(sw[0]["start"] + 0.01, 2)),
                                 "text": " ".join(w["word"] for w in sw), "src": "repair"})
        elif words is None and inside(s) and str(s.get("text", "")).strip():
            new_segs.append({"start": s["start"], "end": s["end"],
                             "text": str(s["text"]).strip(), "src": "repair"})
    doc["segments"] = sorted([s for s in doc["segments"] if not inside(s)] + new_segs,
                             key=lambda s: (s["start"], s["end"]))
    added_words = 0
    if isinstance(doc.get("words"), list) and words is not None:
        new_words = [{"start": w["start"], "end": w["end"], "word": w["word"], "src": "repair"}
                     for w in words if inside(w)]
        added_words = len(new_words)
        doc["words"] = sorted([w for w in doc["words"] if not inside(w)] + new_words,
                              key=lambda w: (w["start"], w["end"]))
    rec = {**record, "replaced": [round(lo, 2), round(hi, 2)],
           "removed_segments": removed, "added_segments": len(new_segs),
           "added_words": added_words, "repaired_at": _now()}
    prov = doc.setdefault("provenance", {})
    prov.setdefault("repairs", []).append(rec)
    return rec


def cut_wav(src, start_s: float, length_s: float, out, ffmpeg: str | None = None) -> None:
    """`length_s` of `src` from `start_s` as 16 kHz mono WAV. Output-side
    -ss/-t: decoded from the start and cut on the decoded timeline (the one
    transcription and clipping use)."""
    cmd = [ffmpeg or stt.ffmpeg_path(), "-nostdin", "-v", "error", "-y", "-i", str(src),
           "-ss", f"{max(0.0, start_s):.3f}", "-t", f"{length_s:.3f}", "-ac", "1",
           "-ar", str(stt.SAMPLE_RATE), "-c:a", "pcm_s16le", "-f", "wav", str(out)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg cut failed rc={res.returncode}: {res.stderr[-300:]}")


def audio_source(path):
    """`source_for` over a whole episode audio file."""
    def source_for(group):
        return {"kind": "audio", "path": str(path), "offset": 0.0,
                "a": group["a"], "b": group["b"]}
    return source_for


def clip_source(clip_dir, index, audio_sha256=None):
    """`source_for` over an episode's evidence clips (a normalized index):
    the clip that holds the whole replaced stretch, decoded over the padded
    window cut to the clip. A clip from other audio (sha256 differs) is not
    used. None-kind results say why there is no source."""
    clips = []
    if isinstance(index, dict) and index.get("status") == "ok" and (
            not audio_sha256 or not index.get("audio_sha256")
            or index["audio_sha256"] == audio_sha256):
        clips = [c for c in index.get("clips") or ()
                 if (clip_dir / c["file"]).is_file()]

    def source_for(group):
        lo, hi = group["lo"], group["hi"]
        for c in clips:
            c0, c1 = float(c["start_s"]), float(c["end_s"])
            if c0 <= lo and hi <= c1:
                return {"kind": "clip", "path": str(clip_dir / c["file"]), "file": c["file"],
                        "offset": c0, "a": max(c0, group["a"]), "b": min(c1, group["b"])}
        partial = any(float(c["start_s"]) < hi and float(c["end_s"]) > lo for c in clips)
        return {"kind": None, "coverage": "clip_partial" if partial else "none"}
    return source_for


def repair_doc(doc, *, source_for, transcribe, backend: dict, flags: list | None = None,
               duration_s: float | None = None, pad_s: float = REPAIR_PAD_S,
               ffmpeg: str | None = None, workdir=None, log=None, fatal=()) -> dict:
    """Repair the transcript's pending quality flags (or `flags`) in place.

    `source_for(group)` gives the audio for a planned group ({kind:
    "audio"|"clip", path, offset (path's start on the episode timeline), a,
    b} or {kind: None, coverage}); `transcribe(wav_path)` returns {"segments",
    "words"?} with times relative to the wav (an exception of a `fatal` type
    propagates; any other fails only its group). `backend` (backend, model,
    revision, ...) goes into each repair record. Returns counts; each flag's
    status becomes "repaired" (with its repair index) or "uncovered" (with
    coverage) or "failed" (with the error)."""
    q = doc.get("quality") or {}
    todo = pending(q) if flags is None else flags
    rep = {"flags": len(todo), "repaired": 0, "uncovered": 0, "failed": 0,
           "audio_s": 0.0, "by_source": {}}
    if not todo:
        return rep
    dur = duration_s or (doc.get("provenance") or {}).get("decoded_duration_s")
    for g in plan(todo, dur, pad_s, source_for):
        src = g["src"]
        if not src.get("kind"):
            for f in g["flags"]:
                f.update(status="uncovered", coverage=src.get("coverage", "none"))
            rep["uncovered"] += len(g["flags"])
            continue
        a, b = src["a"], src["b"]
        try:
            with tempfile.TemporaryDirectory(dir=workdir, prefix="repair-") as tmp:
                wav = os.path.join(tmp, "span.wav")
                cut_wav(src["path"], a - src["offset"], b - a, wav, ffmpeg)
                res = transcribe(wav)
        except fatal:
            raise
        except Exception as exc:  # one failed stretch must not cost the rest
            err = f"{type(exc).__name__}: {exc}"[:500]
            for f in g["flags"]:
                f.update(status="failed", error=err)
            rep["failed"] += len(g["flags"])
            if log:
                log(f"repair of {g['lo']:.1f}-{g['hi']:.1f} s failed: {err}")
            continue
        shift = lambda items: [  # noqa: E731
            {**it, "start": round(float(it["start"]) + a, 2),
             "end": round(float(it["end"]) + a, 2)} for it in items or ()]
        words = res.get("words")
        record = {**backend, "flag_ids": [f.get("id") for f in g["flags"]],
                  "kinds": sorted({f["kind"] for f in g["flags"]}),
                  "window": [round(a, 2), round(b, 2)], "source": src["kind"],
                  **({"clip": src["file"]} if src.get("file") else {})}
        splice(doc, g["lo"], g["hi"], shift(res.get("segments")),
               shift(words) if words is not None else None, record)
        idx = len(doc["provenance"]["repairs"]) - 1
        for f in g["flags"]:
            f.update(status="repaired", repair=idx, source=src["kind"])
            f.pop("coverage", None)
            f.pop("error", None)
        rep["repaired"] += len(g["flags"])
        rep["audio_s"] = round(rep["audio_s"] + (b - a), 2)
        rep["by_source"][src["kind"]] = rep["by_source"].get(src["kind"], 0) + len(g["flags"])
    return rep


def transcribe_with(model, backend: str, language_hint: str | None = None):
    """`transcribe(wav)` for repair_doc from an in-process model."""
    chunk_s, overlap_s = REPAIR_CHUNKS[backend]

    def transcribe(wav):
        res = stt.transcribe_file(wav, model=model, chunk_s=chunk_s, overlap_s=overlap_s,
                                  beam=1, lang_windows=0 if backend == "parakeet" else 3,
                                  language_hint=language_hint)
        return {"segments": res["segments"], "words": res.get("words")}
    return transcribe


# ---------------------------------------------------------------- backlog

PARAKEET_X_REALTIME = 10.7  # median speed on the mini (transcription health check)


def _source(prov) -> str:
    """Transcript source for reports: backend, plus the model for Whisper."""
    prov = prov or {}
    backend = prov.get("backend") or "?"
    return f"{backend}:{prov.get('model')}" if "whisper" in backend else backend


class RequeueRefused(Exception):
    """repair-loops --requeue without the live workers it needs."""


REQUEUE_WORKERS = ("detect", "jev", "name")


def requeue_missing_workers(corpus) -> list:
    """Stages a repair requeue needs a live worker for and has none: detect
    always, jev / name when the corpus has them enabled. Without them the
    queue routes past jev / name and overwrites jev_status / name_status
    with "no worker" while the old jev/ and names/ files stay."""
    from earwitness.pipeline import corpus as cr
    q = cr.Queue(corpus)
    try:
        need = {"detect": True, "jev": bool(q.jev_model()),
                "name": q.con.execute("SELECT 1 FROM meta WHERE key='name_pipeline'"
                                      ).fetchone() is not None}
        return [s for s in REQUEUE_WORKERS if need[s] and not q.live_worker(s)]
    finally:
        q.close()


def _read_only(corpus):
    """A connection for the dry run that cannot write (no schema changes, no
    journal-mode switch)."""
    con = sqlite3.connect(str(Path(corpus) / "queue.sqlite3"), timeout=15.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=1")
    return con


def repair_loops(corpus, *, transcriber=None, backend: str = "parakeet", dry_run: bool = False,
                 requeue: bool = False, requeue_anyway: bool = False,
                 max_episodes: int | None = None,
                 ffmpeg: str | None = None, pad_s: float = REPAIR_PAD_S, log=print) -> dict:
    """Repair the quality flags of `done` episodes' transcripts (the
    `repair-loops` command). A transcript without a current quality record
    is assessed first (with its show's stored language). Each flag group is
    re-transcribed by `transcriber` (refine.ChildTranscriber over a
    refine-stt child: .describe(), .transcribe(wav, **opts)) from the
    episode audio when this host still has it, else from the evidence clip
    holding the whole stretch; uncovered flags are counted by coverage.

    A repaired transcript is validated and written inside a BEGIN IMMEDIATE
    transaction that re-checks the row is still done/ready. By default the
    queue is left alone and the touched episodes are listed ("touched") for
    re-naming. With `requeue` the episode goes back to `detect` (only to the
    stages its coverage tier runs: one whose tier stops at transcribe stays
    done, and its missing detections are never "stale"; detect,
    Jev, name, clip then rerun; clip keeps the published clips when the
    audio is gone; the paid screen is never re-run: repair_requeued_at
    makes the screen stage skip it); it is refused (RequeueRefused) unless
    detect, jev and name workers are live (requeue_missing_workers), or
    `requeue_anyway`. A repaired transcript whose detections are older than
    it (a crash between the write and the commit, or an earlier run without
    requeue) is listed under "stale_detections", and requeued with
    `requeue`. `dry_run` counts flags, coverage and the audio to decode on a
    read-only connection, loading no model. Idempotent: repaired flags are
    never redone."""
    from earwitness.pipeline import corpus as cr
    from earwitness.pipeline import refine as rf
    corpus = Path(corpus)
    dirs = cr.corpus_dirs(corpus)
    chunk_s, overlap_s = REPAIR_CHUNKS[backend]
    rep = {"dry_run": dry_run, "backend": backend, "requeue": requeue, "transcripts": 0,
           "unreadable": 0, "flags": {}, "episodes_flagged": 0, "episodes_flagged_by_source": {},
           "flags_pending": 0, "coverage": {"audio": 0, "clip": 0, "clip_partial": 0, "none": 0},
           "coverage_s": {"audio": 0.0, "clip": 0.0}, "episodes_repaired": 0,
           "flags_repaired": 0, "flags_failed": 0, "flags_uncovered": 0, "repaired_audio_s": 0.0,
           "detect_requeued": 0, "left_busy": 0, "invalid": 0, "touched": [],
           "stale_detections": []}
    if requeue and not dry_run and not requeue_anyway:
        missing = requeue_missing_workers(corpus)
        if missing:
            raise RequeueRefused(
                f"--requeue needs live {', '.join(missing)} worker(s) (seen in the last "
                f"{cr.SCREEN_WORKER_TTL_S:.0f} s): without them jev_status / name_status "
                "are overwritten with 'no worker' while the old jev/ and names/ files stay. "
                "Start them first, run without --requeue, or add --requeue-anyway")
    info = None
    con = _read_only(corpus) if dry_run else cr.connect(corpus)
    try:
        if not dry_run:
            cr.ensure_schema(con)
        has_lang = "language" in {r[1] for r in con.execute("PRAGMA table_info(shows)")}
        has_tier = "tier" in {r[1] for r in con.execute("PRAGMA table_info(episodes)")}
        rows = [dict(r) for r in con.execute(
            "SELECT e.episode_id, e.audio_path, e.audio_sha256, e.duration_s, %s"
            " AS show_language, %s AS tier FROM episodes e LEFT JOIN shows s"
            " ON s.feed_url = e.feed_url WHERE e.stage='done' AND e.state='ready'"
            " ORDER BY e.episode_id" % ("s.language" if has_lang else "NULL",
                                        "e.tier" if has_tier else "'full'"))]
        sets = {t: cr.tier_stages(con, t) for t in cr.TIERS}

        def requeue_row(ep_id, to):
            if to == "done":  # its tier stops at transcribe: nothing to rerun
                return
            con.execute("UPDATE episodes SET stage=?, state='ready', attempts=0,"
                        " not_before=0, last_error='repair_loops', repair_requeued_at=?,"
                        " updated_at=? WHERE episode_id=?", (to, cr.utcnow(), cr.utcnow(), ep_id))
            rep["detect_requeued"] += 1

        for row in rows:
            ep_id = row["episode_id"]
            requeue_to = cr.tier_next("detect", sets.get(row["tier"]))
            tpath = dirs["transcripts"] / f"{ep_id}.json"
            doc = cr._read_json(tpath)
            if not isinstance(doc, dict) or not isinstance(doc.get("segments"), list):
                rep["unreadable"] += 1
                continue
            rep["transcripts"] += 1
            det = dirs["detections"] / f"{ep_id}.json"
            if (doc.get("provenance") or {}).get("repairs") and requeue_to != "done" and (
                    not det.exists() or det.stat().st_mtime < tpath.stat().st_mtime):
                rep["stale_detections"].append(ep_id)  # repaired, never re-detected
                if requeue and not dry_run:
                    with cr._Tx(con):
                        cur = con.execute("SELECT stage, state FROM episodes WHERE episode_id=?",
                                          (ep_id,)).fetchone()
                        if (cur["stage"], cur["state"]) == ("done", "ready"):
                            requeue_row(ep_id, requeue_to)
                    continue
            q = quality_of(doc, row["show_language"])
            for f in q["flags"]:
                key = f"{f['kind']}:{f.get('status')}"
                rep["flags"][key] = rep["flags"].get(key, 0) + 1
            todo = pending(q)
            if not q["flags"]:
                continue
            rep["episodes_flagged"] += 1
            src_name = _source(doc.get("provenance"))
            rep["episodes_flagged_by_source"][src_name] = \
                rep["episodes_flagged_by_source"].get(src_name, 0) + 1
            if not todo:
                continue
            rep["flags_pending"] += len(todo)
            prov = doc.get("provenance") or {}
            audio = row.get("audio_path")
            if audio and os.path.exists(audio):
                source_for = audio_source(audio)
            else:
                source_for = clip_source(dirs["clips"] / ep_id,
                                         cr.read_clip_index(dirs["clips"] / ep_id / "index.json"),
                                         prov.get("audio_sha256") or row.get("audio_sha256"))
            dur = prov.get("decoded_duration_s") or row.get("duration_s")
            groups = plan(todo, dur, pad_s, source_for)
            for g in groups:
                src = g["src"]
                kind = src.get("kind") or src.get("coverage", "none")
                rep["coverage"][kind] += len(g["flags"])
                if src.get("kind"):
                    rep["coverage_s"][kind] = round(rep["coverage_s"][kind]
                                                    + src["b"] - src["a"], 2)
            if dry_run:
                continue
            if max_episodes is not None and rep["episodes_repaired"] >= max_episodes:
                continue
            if not any(g["src"].get("kind") for g in groups):
                rep["flags_uncovered"] += len(todo)
                continue
            if info is None:
                info = transcriber.describe()
            hint = stt.whisper_language(row["show_language"]) if backend != "parakeet" else None
            r = repair_doc(
                doc, source_for=source_for, duration_s=dur, pad_s=pad_s, ffmpeg=ffmpeg,
                workdir=dirs["work"], log=log, fatal=(rf.SpeechUnavailable,),
                backend={"backend": info.get("backend"), "model": info.get("model"),
                         **(info.get("versions") or {})},
                transcribe=lambda wav: transcriber.transcribe(
                    wav, chunk_s=chunk_s, overlap_s=overlap_s,
                    lang_windows=0 if backend == "parakeet" else 3, language_hint=hint))
            rep["flags_failed"] += r["failed"]
            rep["flags_uncovered"] += r["uncovered"]
            if not r["repaired"]:
                continue
            q["repair"] = {k: r[k] for k in ("flags", "repaired", "uncovered", "failed")}
            with cr._Tx(con):
                cur = con.execute("SELECT stage, state, duration_s FROM episodes"
                                  " WHERE episode_id=?", (ep_id,)).fetchone()
                if (cur["stage"], cur["state"]) != ("done", "ready"):
                    rep["left_busy"] += 1
                    continue
                try:
                    cr.validate_transcript(doc, cur["duration_s"])
                except cr.TranscriptInvalid as exc:
                    rep["invalid"] += 1
                    log(f"[repair-loops] {ep_id}: repaired transcript invalid, not written: {exc}")
                    continue
                cr.atomic_write_json(tpath, doc)
                if requeue:
                    requeue_row(ep_id, requeue_to)
            rep["episodes_repaired"] += 1
            rep["flags_repaired"] += r["repaired"]
            rep["repaired_audio_s"] = round(rep["repaired_audio_s"] + r["audio_s"], 2)
            rep["touched"].append(ep_id)
            log(f"[repair-loops] {ep_id}: {r['repaired']} flags repaired from "
                f"{','.join(sorted(r['by_source']))} ({r['audio_s']:.0f} s), "
                f"{r['uncovered']} uncovered, {r['failed']} failed")
    finally:
        con.close()
    decode_s = rep["coverage_s"]["audio"] + rep["coverage_s"]["clip"]
    rep["gpu_s_est"] = round(decode_s / PARAKEET_X_REALTIME, 1)
    return rep


def summary_line(rep: dict) -> str:
    cov = rep["coverage"]
    head = (f"repair-loops{' dry run' if rep['dry_run'] else ''}: {rep['transcripts']} "
            f"transcripts, {rep['episodes_flagged']} flagged episodes "
            f"{rep['episodes_flagged_by_source']}, flags {rep['flags']}; pending "
            f"{rep['flags_pending']}: audio {cov['audio']}, clip {cov['clip']}, clip only in "
            f"part {cov['clip_partial']}, no audio {cov['none']} "
            f"({rep['coverage_s']['audio'] + rep['coverage_s']['clip']:.0f} s to decode, "
            f"~{rep['gpu_s_est']} GPU s)")
    if rep["dry_run"]:
        return head
    return (head + f"; repaired {rep['flags_repaired']} flags in {rep['episodes_repaired']} "
            f"episodes ({rep['repaired_audio_s']:.0f} s), failed {rep['flags_failed']}, "
            f"uncovered {rep['flags_uncovered']}, detect requeued {rep['detect_requeued']}, "
            f"left busy {rep['left_busy']}, stale detections {len(rep['stale_detections'])}"
            + ("" if rep["requeue"] else
               "; not requeued: re-name the touched episodes (`rename --episodes`)"
               " or re-run with --requeue"))
