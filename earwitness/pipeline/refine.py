"""Refine pass: re-transcribe evidence clips with a stronger speech model
and re-run brand naming on the refined text.

Decoupled from the queue: it only touches episodes already at stage
`done`, takes no leases and writes nothing to queue.sqlite3, so it runs
alongside a live fleet. For each such episode that has evidence clips
around Jev windows / named spans (and, with only_tiny, a first-pass
transcript made by a `tiny` model):

1. every clip holding a Jev window or named span is transcribed whole (it
   already carries its context) by a speech child process (`corpus_run.py
   refine-stt`, run under `stt_python`, so mlx-whisper and GLiNER can live
   in different venvs), with a timeout per clip; clip-relative times are
   shifted by the clip's start offset from clips/<ep>/index.json and the
   result is written to refined/<ep>.json after every clip (crash-safe at
   clip granularity, clips are never deleted); the refine guard keeps a
   clip's first-pass text when the refined text has under
   GUARD_MIN_WORD_RATIO of its words (each clip records "refine_guard";
   turbo's text is kept under "discarded_segments");
2. the namer re-names each named span on one timeline that is the refined
   text inside refined clips and the original text elsewhere (episodes
   with a Jev result but no names doc are named afresh), and
   names/<ep>.json is replaced atomically with "refined": true, per-span
   "text_source" (refined / partial / original), the earlier naming under
   "previous", and change counts. export and seed read names/ as before,
   so they pick up refined names with no extra switch.

An episode is finished when its names doc carries the current
REFINE_VERSION and model; reruns skip it. An episode refined at the current
REFINE_VERSION by another model or backend is skipped too unless
`rerefine`; a rerefine records progress in refined/<ep>.<backend>.partial
and replaces refined/<ep>.json only once it completes the episode.
"""
from __future__ import annotations

import json
import queue as queue_mod
import subprocess
import sys
import threading
import time
from pathlib import Path

from earwitness.pipeline import brand_namer as bn
from earwitness.pipeline import corpus as cr
from earwitness.pipeline import jev_screen
from earwitness.pipeline import transcript_quality as tq
from earwitness.pipeline.chunked_stt import atomic_write_json

REFINE_VERSION = 1   # clip transcription (refined/<ep>.json); a bump re-transcribes
NAMING_VERSION = 2   # naming of refined text (names/<ep>.json); a bump re-names, no speech
CONFIDENCE_DROP = 0.10  # acceptance rule (b)
DEFAULT_MODEL = "large-v3-turbo"
DEFAULT_BACKEND = "mlx"
CLIP_TIMEOUT_S = 300.0      # per clip (clips are <= 240 s; turbo ran ~0.2x real time)
STARTUP_TIMEOUT_S = 600.0   # child start + model load
MAX_CLIP_ATTEMPTS = 3       # after this many failed runs a clip is named on original text
MAX_START_FAILURES = 3      # consecutive speech child start failures before the run stops
MAX_CONSECUTIVE_CLIP_FAILURES = 5  # clip failures in a row (any episodes) before it stops
GPU_S_PER_AUDIO_MIN = 11.4  # turbo on an M1 Pro under heavy load (turbo-refine-test.md)
# Refine guard: turbo skipped whole ad reads in ~10% of clips (transcription
# health check), so a clip whose refined text has under this share of the
# first pass's words keeps the first-pass text.
GUARD_MIN_WORD_RATIO = 0.8
REFINED_BRAND_LIST_NAME = "brand-list-refined.json"


class ClipFailed(RuntimeError):
    pass


class ChildStartFailed(ClipFailed):
    """The speech child could not start; not counted as a clip attempt."""


class SpeechUnavailable(RuntimeError):
    """Consecutive speech child start failures: the run stops (exit 3)."""


class BudgetExhausted(RuntimeError):
    """The next request would take a hosted backend past its USD cap: the run
    stops cleanly (what is ready is named; nothing is charged as a failure)."""


def is_tiny(model) -> bool:
    return isinstance(model, str) and "tiny" in model.lower()


def refinable_clips(index) -> list:
    """Clips of a published index that hold a Jev window or named span."""
    if not isinstance(index, dict) or index.get("status") != "ok":
        return []
    return [c for c in index.get("clips") or () if isinstance(c, dict)
            and set(c.get("sources") or ()) & {"jev", "named"}]


def finished(names_doc, model: str, backend: str = DEFAULT_BACKEND) -> bool:
    return isinstance(names_doc, dict) and names_doc.get("refined") is True \
        and names_doc.get("refine_version") == REFINE_VERSION \
        and names_doc.get("refine_naming_version") == NAMING_VERSION \
        and names_doc.get("refine_model") == model \
        and names_doc.get("refine_backend") == backend


# ---------------------------------------------------------------- speech child

class ChildTranscriber:
    """Transcribes clip files in a long-lived child process (one model load
    per run): one JSON line per request on its stdin, one JSON line per
    answer on its stdout. A clip that overruns `clip_timeout_s`, a broken
    pipe or a garbled answer kills the child (ClipFailed); a child found
    dead is restarted before the next clip. A failed start raises
    ChildStartFailed; `max_start_failures` in a row (or a failed first
    start, in describe) raise SpeechUnavailable."""

    def __init__(self, cmd: list, *, clip_timeout_s: float = CLIP_TIMEOUT_S,
                 startup_timeout_s: float = STARTUP_TIMEOUT_S,
                 max_start_failures: int = MAX_START_FAILURES, log=print):
        self.cmd, self.clip_timeout_s, self.startup_timeout_s = cmd, clip_timeout_s, \
            startup_timeout_s
        self.max_start_failures, self.start_failures = max_start_failures, 0
        self.log = log
        self.proc, self.lines, self.info = None, None, None

    def _start(self):
        try:
            self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, text=True,
                                         bufsize=1)  # stderr: inherited
            self.lines = queue_mod.Queue()
            threading.Thread(target=self._pump, args=(self.proc, self.lines),
                             daemon=True).start()
            ready = self._read(self.startup_timeout_s, "start")
            if not ready.get("ready"):
                raise ClipFailed(f"speech child failed to start: {ready.get('error') or ready}")
        except (ClipFailed, OSError) as exc:
            self.close()
            self.start_failures += 1
            if self.start_failures >= self.max_start_failures:
                raise SpeechUnavailable(f"{self.start_failures} consecutive speech child "
                                        f"start failures; last: {exc}") from exc
            raise ChildStartFailed(str(exc)) from exc
        self.start_failures = 0
        self.info = {k: ready.get(k) for k in ("model", "backend", "versions")}

    def _ensure(self):
        if self.proc is not None and self.proc.poll() is not None:
            self.close()  # died between clips (e.g. OOM-killed): start afresh
        if self.proc is None:
            self._start()

    @staticmethod
    def _pump(proc, lines):
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    def _read(self, timeout_s: float, what: str) -> dict:
        try:
            line = self.lines.get(timeout=timeout_s)
        except queue_mod.Empty:
            self.close()
            raise ClipFailed(f"timeout: speech child gave no {what} answer in {timeout_s:g}s")
        if line is None:
            rc = self.proc.wait()
            self.close()
            raise ClipFailed(f"speech child exited rc={rc} during {what}")
        try:
            ans = json.loads(line)
            if not isinstance(ans, dict):
                raise ValueError("not a JSON object")
        except ValueError as exc:
            self.close()
            raise ClipFailed(f"speech child sent a bad {what} answer ({exc}): "
                             f"{line.strip()[:200]!r}") from exc
        return ans

    def describe(self) -> dict:
        if self.proc is None:
            try:
                self._start()
            except ChildStartFailed as exc:  # nothing to run on
                raise SpeechUnavailable(f"speech child failed its first start: {exc}") from exc
        return self.info

    def transcribe(self, path, **opts) -> dict:
        """{"segments": clip-relative [{start, end, text}], "language", "wall_s"}
        (+ "words" from a word-timestamp backend). `opts` (chunk_s, overlap_s,
        lang_windows, language_hint) go to the child's transcribe_file."""
        self._ensure()
        try:
            self.proc.stdin.write(json.dumps({"path": str(path), **opts}) + "\n")
            self.proc.stdin.flush()
        except OSError as exc:  # BrokenPipeError: the child stopped reading
            self.close()
            raise ClipFailed(f"speech child pipe broke: {type(exc).__name__}: {exc}") from exc
        ans = self._read(self.clip_timeout_s, "clip")
        if "error" in ans:
            raise ClipFailed(ans["error"])
        return ans

    def close(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait()
            for pipe in (self.proc.stdin, self.proc.stdout):
                try:
                    pipe.close()
                except OSError:  # a dead child's stdin may refuse its final flush
                    pass
            self.proc = None


def load_stt_model(model_name: str, backend: str, threads: int = 4, pin: dict | None = None):
    """(model, info) for the refine-stt child, reusing chunked_stt's backends.
    "parakeet" (repair-loops) loads `pin`'s revision offline when given."""
    from earwitness.pipeline import chunked_stt as stt
    if backend == "parakeet":
        model = stt.ParakeetModel(model_name, pin=pin)
        return model, {"model": model_name, "backend": "parakeet-mlx",
                       "versions": {"parakeet_mlx": model.version,
                                    "mlx": stt.mlx_versions()["mlx"], "repo": model.repo,
                                    "revision": model.revision,
                                    "weights_sha256": model.weights_sha256,
                                    "dtype": model.dtype}}
    if backend == "mlx":
        repo = stt.mlx_repo(model_name)
        model = stt.MlxWhisperModel(repo)
        return model, {"model": model_name, "backend": "mlx-whisper",
                       "versions": {**stt.mlx_versions(), "repo": repo, "dtype": "float16"}}
    import ctranslate2
    import faster_whisper
    return stt.load_model(model_name, threads), {
        "model": model_name, "backend": "faster-whisper",
        "versions": {"faster_whisper": faster_whisper.__version__,
                     "ctranslate2": ctranslate2.__version__, "compute_type": "int8"}}


def serve_stt(model, info: dict, inp, out) -> None:
    """The refine-stt child loop: announce readiness, then transcribe each
    requested clip whole (greedy, the backend's own language detection)."""
    from earwitness.pipeline import chunked_stt as stt
    out.write(json.dumps({"ready": True, **info}) + "\n")
    out.flush()
    for line in inp:
        if not line.strip():
            continue
        t0 = time.monotonic()
        try:
            req = json.loads(line)
            res = stt.transcribe_file(req["path"], model=model,
                                      chunk_s=float(req.get("chunk_s") or 600.0),
                                      overlap_s=float(req.get("overlap_s") or 20.0),
                                      beam=1, lang_windows=int(req.get("lang_windows") or 0),
                                      language_hint=req.get("language_hint"))
            ans = {"segments": res["segments"], "language": res["language"],
                   "decoded_s": res["decoded_duration_s"],
                   "wall_s": round(time.monotonic() - t0, 3)}
            if "words" in res:
                ans["words"] = res["words"]
        except Exception as exc:  # one bad clip must not end the run
            ans = {"error": f"{type(exc).__name__}: {exc}"[:500]}
        out.write(json.dumps(ans) + "\n")
        out.flush()


# ---------------------------------------------------------------- naming

def _to_episode_time(segments: list, clip: dict) -> list:
    off, end = float(clip["start_s"]), float(clip["end_s"])
    out = []
    for s in segments:
        text = str(s.get("text", "")).strip()
        a = round(min(off + float(s["start"]), end), 2)
        b = round(min(off + float(s["end"]), end), 2)
        if text and b > a:
            out.append({"start": a, "end": b, "text": text})
    return out


def first_pass_in(segments: list, clip: dict) -> list:
    """First-pass segments a refined clip replaces (refined_segments' rule:
    midpoint inside the clip)."""
    a, b = float(clip["start_s"]), float(clip["end_s"])
    return [dict(s) for s in segments or () if a <= (s["start"] + s["end"]) / 2 < b]


def guard(first_pass: list, refined: list) -> dict:
    """The refine guard's decision for one clip: "kept_first_pass" when the
    refined text has under GUARD_MIN_WORD_RATIO of the first pass's words
    (repeat loops in the first pass count once), else "refined"."""
    fp = tq.effective_word_count(first_pass)
    rf_words = tq.effective_word_count(refined)
    ratio = round(rf_words / fp, 3) if fp else None
    keep = ratio is not None and ratio < GUARD_MIN_WORD_RATIO
    return {"decision": "kept_first_pass" if keep else "refined", "first_pass_words": fp,
            "refined_words": rf_words, "ratio": ratio, "min_ratio": GUARD_MIN_WORD_RATIO}


kept_first_pass = cr.refine_kept_first_pass


def _covered(lo: float, hi: float, ranges: list) -> float:
    """Share of [lo, hi] covered by the union of `ranges`."""
    if hi <= lo:
        return 0.0
    got, cur = 0.0, lo
    for a, b in sorted(ranges):
        a, b = max(a, cur), min(b, hi)
        if b > a:
            got += b - a
            cur = b
    return got / (hi - lo)


def _text_source(span: dict, ranges: list) -> str:
    share = _covered(float(span["start_s"]), float(span["end_s"]), ranges)
    return "refined" if share >= 0.999 else "partial" if share > 0 else "original"


def _key(brand):
    return bn.compact(bn.normalize_brand(brand) or brand) if brand else None


_GAZETTEER_KEYS = frozenset(bn.compact(c) for c in bn.GAZETTEER)
_NAME_FIELDS = ("brand", "candidates", "confidence", "needs_review", "method",
                "second_brands")


def _supported(span: dict) -> bool:
    """The span's brand is a curated (gazetteer) name, or its winning
    candidate was spoken as a domain or matches a domain heard in the
    corpus (brand-list)."""
    k = _key(span.get("brand"))
    if k is None:
        return False
    if k in _GAZETTEER_KEYS:
        return True
    cand = next((c for c in span.get("candidates") or () if _key(c.get("brand")) == k), None)
    return cand is not None and any(e in ("domain", "brand-list")
                                    for e in cand.get("evidence") or ())


def acceptance(new: dict, old: dict) -> str | None:
    """Why the previous name is kept over the refined one, or None to
    accept the refined name: (a) "needs_review": the refined name needs
    review and the previous did not; (b) "confidence_drop": the brands
    differ and refined confidence is more than CONFIDENCE_DROP below the
    previous; (c) "generic": the brands differ and the refined name has no
    domain or gazetteer support while the previous had it."""
    if new.get("needs_review") and not old.get("needs_review"):
        return "needs_review"
    differ = _key(new.get("brand")) != _key(old.get("brand"))
    if differ and (new.get("confidence") or 0.0) < (old.get("confidence") or 0.0) \
            - CONFIDENCE_DROP:
        return "confidence_drop"
    if differ and not _supported(new) and _supported(old):
        return "generic"
    return None


POD_RECORDS = ("pod_jev", "pod_config", "cue_spans", "promo_shows", "display_spelling")


def _pod_made(span: dict) -> bool:
    """A span the pod pass or a round-3 cue pass made (renaming.rename_corpus)."""
    return bool(span.get("pod")) or span.get("method") in ("tag", "promo")


def rename(ep_id: str, names_doc, jev_doc, segments: list, refined_doc: dict, namer, *,
           model: str, threshold: float, context_windows: int,
           merge_rule: str = bn.DEFAULT_MERGE_RULE,
           second_brand_min_score: float | None = bn.SECOND_BRAND_MIN_SCORE,
           max_span_windows: int | None = bn.MAX_SPAN_WINDOWS,
           sub_window_s: float | None = None) -> dict:
    """The refined names/<ep>.json document. Spans keep the previous
    naming's ranges, so the doc records that naming's merge_rule (and
    max_span_windows; "deployed" for names from before the rule was
    recorded; and its sub_window_s) and, as span_brand_rule, the
    `merge_rule` that picked each span's brand (bn.rename_span). Without
    previous spans the refined text is named afresh under `merge_rule` and
    `sub_window_s`."""
    previous = names_doc.get("previous") if isinstance(names_doc, dict) \
        and names_doc.get("refined") else names_doc
    merged = cr.refined_segments(segments, refined_doc)
    ranges = [(c["start_s"], c["end_s"]) for c in refined_doc.get("clips") or ()
              if not kept_first_pass(c)]
    windows = bn.jev_windows(jev_doc) or []
    prev_spans = cr.named_spans(previous)
    keep = bn.protected_spans(namer, ep_id)   # a person confirmed it: no operator rules
    if prev_spans is not None:
        cw = previous.get("context_windows", context_windows)
        # pod pieces, tag and promo spans keep their own range and name:
        # re-named over their whole Jev windows, neighbours would all take
        # one brand (rename --pod-jev names them; rerun it on the refined text)
        spans = [dict(sp) if _pod_made(sp) else
                 bn.rename_span(sp, merged, windows,
                                bn.without_rules(namer) if sp.get("span_id") in keep else namer,
                                context_windows=cw,
                                merge_rule=merge_rule,
                                second_brand_min_score=second_brand_min_score)
                 for sp in prev_spans]
        head = {k: previous.get(k) for k in ("episode_id", "jev_model", "jev_threshold",
                                              "context_windows")}
        rules = {"merge_rule": previous.get("merge_rule", "deployed"),
                "max_span_windows": previous.get("max_span_windows"),
                "span_brand_rule": merge_rule}
    else:
        doc = bn.name_episode(ep_id, merged, jev_doc, namer, threshold=threshold,
                              context_windows=context_windows, merge_rule=merge_rule,
                              second_brand_min_score=second_brand_min_score,
                              max_span_windows=max_span_windows, sub_window_s=sub_window_s)
        spans, head = doc.pop("spans"), {k: doc[k] for k in (
            "episode_id", "jev_model", "jev_threshold", "context_windows")}
        rules = {"merge_rule": merge_rule, "max_span_windows": max_span_windows,
                "span_brand_rule": merge_rule}
    sw = (previous or {}).get("sub_window_s") if prev_spans is not None else sub_window_s
    if sw:  # recorded only when on, as name_episode does
        head["sub_window_s"] = sw
    by_id = {sp.get("span_id"): sp for sp in prev_spans or ()}
    corr = getattr(namer, "corrections", None)
    # a previous name is kept only as the operator's rules have it
    fixed = {k: sp if corr is None or k in keep else corr.fix_name(sp)
             for k, sp in by_id.items()}
    changes = {"names_changed": 0, "names_unchanged": 0, "names_new": 0,
               "accepted_refined": 0, "kept_previous": 0, "kept_needs_review": 0,
               "kept_confidence_drop": 0, "kept_generic": 0,
               "confidence_up": 0, "confidence_down": 0, "confidence_same": 0,
               "confidence_delta_sum": 0.0}
    for sp in spans:
        sp["text_source"] = _text_source(sp, ranges)
        old = by_id.get(sp.get("span_id"))
        rule = None if old is None else (
            "pod" if _pod_made(old) else acceptance(sp, fixed[sp.get("span_id")]))
        sp["accepted"], sp["rule"] = ("refined", None) if rule is None else ("previous", rule)
        if rule is None:
            changes["accepted_refined"] += 1
        else:
            sp["refined_candidate"] = {k: sp[k] for k in _NAME_FIELDS}
            sp.update({k: fixed[sp.get("span_id")].get(k) for k in _NAME_FIELDS})
            changes["kept_previous"] += 1
            changes[f"kept_{rule}"] = changes.get(f"kept_{rule}", 0) + 1
        if old is None:
            changes["names_new"] += 1
            continue
        sp["previous_brand"], sp["previous_confidence"] = old.get("brand"), old.get("confidence")
        changes["names_changed" if _key(old.get("brand")) != _key(sp["brand"])
                else "names_unchanged"] += 1
        delta = (sp["confidence"] or 0.0) - (old.get("confidence") or 0.0)
        changes["confidence_delta_sum"] += delta
        changes["confidence_up" if delta > 1e-9 else "confidence_down" if delta < -1e-9
                else "confidence_same"] += 1
    changes["confidence_delta_sum"] = round(changes["confidence_delta_sum"], 3)
    return {**head, "episode_id": ep_id, **rules,
            "second_brand_min_score": second_brand_min_score, **bn.describe(namer, ep_id),
            "spans": spans,
            "named_at": cr.utcnow(), "refined": True, "refine_version": REFINE_VERSION,
            "refine_naming_version": NAMING_VERSION,
            "refine_model": model, "refine_backend": refined_doc.get("backend"),
            "changes": changes, "previous": previous,
            # the pod / round-3 records stay with the names they made
            **{k: previous[k] for k in POD_RECORDS if prev_spans is not None and k in previous}}


# ---------------------------------------------------------------- the pass

def _refined_other(dirs, ep_id: str, names, model: str, backend: str) -> bool:
    """Refined at this REFINE_VERSION by another model/backend: its names doc
    says so, or its refined/<ep>.json holds clips of that run."""
    if isinstance(names, dict) and names.get("refined") is True \
            and names.get("refine_version") == REFINE_VERSION \
            and (names.get("refine_model"), names.get("refine_backend")) != (model, backend):
        return True
    ref = cr._read_json(dirs["refined"] / f"{ep_id}.json")
    return isinstance(ref, dict) and ref.get("refine_version") == REFINE_VERSION \
        and bool(ref.get("clips")) and (ref.get("model"), ref.get("backend")) != (model, backend)


def candidates(corpus, *, model: str, backend: str = DEFAULT_BACKEND,
               only_tiny: bool = True, rerefine: bool = False) -> tuple:
    """(todo [(ep_id, clips)], skipped counts) over done episodes. Episodes
    refined at this version by another model/backend are skipped unless
    `rerefine`."""
    corpus = Path(corpus)
    dirs = cr.corpus_dirs(corpus)
    con = cr.connect(corpus)
    try:
        cr.ensure_schema(con)
        ids = [r[0] for r in con.execute("SELECT episode_id FROM episodes WHERE stage='done'"
                                         " ORDER BY episode_id")]
    finally:
        con.close()
    todo, skipped = [], {"finished": 0, "refined_other": 0, "no_clips": 0, "no_spans": 0,
                         "not_tiny": 0, "no_transcript": 0, "clips_exhausted": 0}
    for ep_id in ids:
        names = cr._read_json(dirs["names"] / f"{ep_id}.json")
        if finished(names, model, backend):
            skipped["finished"] += 1
            continue
        if not rerefine and _refined_other(dirs, ep_id, names, model, backend):
            skipped["refined_other"] += 1
            continue
        clips = refinable_clips(cr.read_clip_index(dirs["clips"] / ep_id / "index.json"))
        clips = [c for c in clips if (dirs["clips"] / ep_id / c["file"]).is_file()]
        if not clips:
            skipped["no_clips"] += 1
            continue
        if cr.named_spans(names) is None and \
                bn.jev_windows(cr._read_json(dirs["jev"] / f"{ep_id}.json")) is None:
            skipped["no_spans"] += 1
            continue
        tr = cr._read_json(dirs["transcripts"] / f"{ep_id}.json")
        if not isinstance(tr, dict) or not isinstance(tr.get("segments"), list):
            skipped["no_transcript"] += 1
            continue
        if only_tiny and not is_tiny(tr.get("model")
                                     or (tr.get("provenance") or {}).get("model")):
            skipped["not_tiny"] += 1
            continue
        if _exhausted(_prior_refined(dirs, ep_id, model, backend), clips):
            skipped["clips_exhausted"] += 1
            continue
        todo.append((ep_id, clips))
    return todo, skipped


def refined_timelines(corpus):
    """(episode_id, segments) for every transcript in the corpus: the
    refined timeline where refined/<ep>.json exists, else the original. The
    refine namer's repair list is harvested from these, so a first-pass
    garble ("wafer dot com") does not pull a refined name back to itself."""
    dirs = cr.corpus_dirs(corpus)
    for path in sorted(dirs["transcripts"].glob("*.json")):
        doc = cr._read_json(path)
        segs = doc.get("segments") if isinstance(doc, dict) else None
        if isinstance(segs, list):
            yield path.stem, cr.refined_segments(
                segs, cr._read_json(dirs["refined"] / path.name))


def trusted_text(corpus):
    """(episode_id, segments) from a stronger speech model: refined clip
    text, and whole transcripts not made by a tiny model."""
    dirs = cr.corpus_dirs(corpus)
    for path in sorted(dirs["transcripts"].glob("*.json")):
        doc = cr._read_json(path)
        if not isinstance(doc, dict) or not isinstance(doc.get("segments"), list):
            continue
        ref = cr._read_json(dirs["refined"] / path.name)
        segs = [s for c in (ref or {}).get("clips") or () if isinstance(c, dict)
                and not kept_first_pass(c) for s in c.get("segments") or ()]
        if not is_tiny(doc.get("model") or (doc.get("provenance") or {}).get("model")):
            segs = doc["segments"]
        if segs:
            yield path.stem, segs


def build_refine_namer(corpus, *, gliner: str = "small", brand_list=None,
                       threads: int | None = None, log=print,
                       corrections=None) -> bn.BrandNamer:
    """The name stage's namer, with its repair list harvested from the
    refined timelines, spellings heard in trusted_text heading their groups
    (kept in <corpus>/brand-list-refined.json; the fleet's brand-list.json
    is left alone)."""
    return cr.build_namer(corpus, gliner=gliner, brand_list=brand_list, threads=threads,
                          log=log, harvest_episodes=refined_timelines(corpus),
                          harvest_trusted=trusted_text(corpus),
                          list_name=REFINED_BRAND_LIST_NAME, corrections=corrections)


def _matches(doc, model: str, backend: str) -> bool:
    return isinstance(doc, dict) and doc.get("refine_version") == REFINE_VERSION \
        and doc.get("model") == model and doc.get("backend") == backend


def _progress_path(dirs, ep_id: str, model: str, backend: str) -> Path:
    """Where this run records clip progress: refined/<ep>.json when it is
    absent or this run's own (version, model, backend); otherwise a sidecar
    refined/<ep>.<backend>.partial, so another run's file is replaced only
    when this run completes the episode."""
    main = dirs["refined"] / f"{ep_id}.json"
    if not main.exists() or _matches(cr._read_json(main), model, backend):
        return main
    return dirs["refined"] / f"{ep_id}.{backend}.partial"


def _prior_refined(dirs, ep_id: str, model: str, backend: str) -> dict:
    """This run's earlier progress (refined/<ep>.json or its sidecar) when it
    is at this version, model and backend; else {}."""
    prior = cr._read_json(_progress_path(dirs, ep_id, model, backend))
    return prior if _matches(prior, model, backend) else {}


def _clip_state(prior: dict, clips: list) -> tuple:
    """({file: refined clip entry}, {file: failed entry}) from a prior
    refined doc, for the episode's current clips (a re-cut clip, with a new
    sha256, needs a new transcription)."""
    sha = {c["file"]: c.get("sha256") for c in clips}
    done = {c["clip"]: c for c in prior.get("clips") or () if isinstance(c, dict)
            and c.get("clip") in sha and c.get("sha256") == sha[c["clip"]]}
    failed = {f["clip"]: f for f in prior.get("failed") or () if isinstance(f, dict)
              and f.get("clip") in sha and f["clip"] not in done}
    return done, failed


def _exhausted(prior: dict, clips: list) -> bool:
    """No clip refined and every clip failed MAX_CLIP_ATTEMPTS times."""
    done, failed = _clip_state(prior, clips)
    return not done and all((failed.get(c["file"]) or {}).get("attempts", 0)
                            >= MAX_CLIP_ATTEMPTS for c in clips)



class _LazyInfo:
    """transcriber.describe() on first use: describing a real child starts it,
    and a re-naming-only pass must not need the speech model at all."""

    def __init__(self, transcriber):
        self.transcriber, self.value = transcriber, None

    def get(self, key, default=None):
        if self.value is None:
            self.value = self.transcriber.describe()
        return self.value.get(key, default)

def _transcribe_episode(dirs, ep_id: str, clips: list, transcriber, info: dict, model: str,
                        backend: str, rep: dict, log, use_guard: bool = True) -> dict | None:
    """Transcribe the episode's refinable clips into refined/<ep>.json. Every
    write holds all clips refined so far (this run or earlier, at this
    version, model and backend; those are reused) and every failure count,
    so an abort loses nothing. A clip that failed MAX_CLIP_ATTEMPTS times
    across runs is skipped (its spans are named on original text); a speech
    child start failure is not a clip attempt. MAX_CONSECUTIVE_CLIP_FAILURES
    clip failures in a row (counted across episodes in rep) raise
    SpeechUnavailable, as do repeated start failures. Returns the refined
    doc, or None while a failed clip has attempts left or no clip is refined
    (such an episode is not named)."""
    main = dirs["refined"] / f"{ep_id}.json"
    out_path = _progress_path(dirs, ep_id, model, backend)
    done, failed = _clip_state(_prior_refined(dirs, ep_id, model, backend), clips)
    first = (cr._read_json(dirs["transcripts"] / f"{ep_id}.json") or {}).get("segments") or []

    def doc():
        out = {"episode_id": ep_id, "refine_version": REFINE_VERSION, "model": model,
               "backend": backend,
               "clips": [done[c["file"]] for c in clips if c["file"] in done],
               "failed": [failed[c["file"]] for c in clips if c["file"] in failed]}
        costed = [c for c in out["clips"] if c.get("cost")]
        costs = [c["cost"] for c in costed]
        if costs:  # hosted backend: provenance + cost of every refined clip
            out["api"] = {k: (costed[-1].get("versions") or {}).get(k)
                          for k in ("provider", "endpoint", "api_model", "usd_per_audio_hour",
                                    "min_billed_s", "pricing_source")}
            out["cost"] = {k: round(sum(c.get(k) or 0 for c in costs), 8 if k == "usd" else 3)
                           for k in ("audio_s", "billed_s", "usd", "requests")}
        return out

    for c in clips:
        if c["file"] in done:
            rep["clips_reused"] += 1
            continue
        attempts = (failed.get(c["file"]) or {}).get("attempts", 0)
        if attempts >= MAX_CLIP_ATTEMPTS:
            continue
        try:
            info.get("model")  # first real clip: start the child; a failed first start is SpeechUnavailable
            ans = transcriber.transcribe(dirs["clips"] / ep_id / c["file"])
        except ChildStartFailed as exc:
            failed[c["file"]] = {"clip": c["file"], "attempts": attempts,
                                 "error": f"start: {exc}"[:500]}
            log(f"[refine] {ep_id} {c['file']}: speech child failed to start: {exc}")
        except ClipFailed as exc:
            failed[c["file"]] = {"clip": c["file"], "attempts": attempts + 1,
                                 "error": str(exc)[:500]}
            rep["clips_failed"] += 1
            rep["consecutive_clip_failures"] = rep.get("consecutive_clip_failures", 0) + 1
            log(f"[refine] {ep_id} {c['file']} FAILED: {exc}")
        else:
            failed.pop(c["file"], None)
            segs = _to_episode_time(ans.get("segments") or [], c)
            fp = first_pass_in(first, c)
            decision = guard(fp, segs)
            if not use_guard:  # record what the guard would do, keep the refined text
                decision.update(decision="off",
                                would_keep_first_pass=decision["decision"] == "kept_first_pass")
            done[c["file"]] = {
                "clip": c["file"], "start_s": c["start_s"], "end_s": c["end_s"],
                "sha256": c.get("sha256"), "segments": segs,
                "language": ans.get("language"), "wall_s": ans.get("wall_s"),
                "model": info.get("model", model), "backend": backend,
                "versions": {**(info.get("versions") or {}), "engine": info.get("backend")},
                "refine_guard": decision}
            if decision["decision"] == "kept_first_pass":  # the clip keeps first-pass text
                done[c["file"]].update(segments=fp, discarded_segments=segs)
                rep["clips_kept_first_pass"] = rep.get("clips_kept_first_pass", 0) + 1
                log(f"[refine] {ep_id} {c['file']}: refined text has {decision['refined_words']}"
                    f" words vs {decision['first_pass_words']} first-pass; kept first pass")
            if ans.get("cost"):
                done[c["file"]].update(cost=ans["cost"],
                                       segments_source=ans.get("segments_source"))
                rep["cost_usd"] = round(rep.get("cost_usd", 0.0) + ans["cost"]["usd"], 8)
            rep["clips_transcribed"] += 1
            rep["consecutive_clip_failures"] = 0
            rep["stt_wall_s"] = round(rep["stt_wall_s"] + (ans.get("wall_s") or 0.0), 3)
        atomic_write_json(out_path, {**doc(), "updated_at": cr.utcnow()})
        if rep.get("consecutive_clip_failures", 0) >= MAX_CONSECUTIVE_CLIP_FAILURES:
            raise SpeechUnavailable(f"{rep['consecutive_clip_failures']} consecutive clip "
                                    "failures")
    out = doc()
    if not out["clips"] or any(f["attempts"] < MAX_CLIP_ATTEMPTS for f in out["failed"]):
        return None  # retried next run; names left as they are
    if out_path != main:  # completed: now it may replace another run's file
        atomic_write_json(main, {**out, "updated_at": cr.utcnow()})
        out_path.unlink(missing_ok=True)
    return out


def refine(corpus, *, transcriber, namer, model: str = DEFAULT_MODEL,
           backend: str = DEFAULT_BACKEND, only_tiny: bool = True, max_episodes: int | None = None, dry_run: bool = False,
           threshold: float = jev_screen.DEFAULT_THRESHOLD,
           context_windows: int = cr.DEFAULT_JEV_CONTEXT_WINDOWS, cost_estimate=None,
           rerefine: bool = False, guard: bool = True,
           sub_window_s: float | None = cr.DEFAULT_NAME_SUB_WINDOW_S, log=print) -> dict:
    """Run the refine pass; returns the run report (see module docstring).
    All selected episodes are transcribed first, then named: `namer` is a
    BrandNamer or a zero-argument factory, called once after transcription
    (so its repair list can include the refined text). `cost_estimate`
    (clip seconds -> USD, hosted backends) adds the dry run's cost estimate.
    BudgetExhausted from the transcriber stops transcription cleanly; what
    is ready is still named. `guard=False` turns the refine guard off (the
    API eval scores the engine's raw text); each clip still records what the
    guard would have done."""
    corpus = Path(corpus)
    dirs = cr.corpus_dirs(corpus)
    if not dry_run:
        bn.check_names_lock(dirs["names"])  # before any speech is spent
    todo, skipped = candidates(corpus, model=model, backend=backend, only_tiny=only_tiny,
                               rerefine=rerefine)
    if max_episodes is not None:
        todo = todo[:max_episodes]
    audio_s = sum(c["end_s"] - c["start_s"] for _, clips in todo for c in clips)
    rep = {"dry_run": dry_run, "model": model, "backend": backend, "only_tiny": only_tiny,
           "episodes": len(todo), "clips": sum(len(c) for _, c in todo),
           "audio_s": round(audio_s, 1), "skipped": skipped}
    if dry_run:
        rep["stt_s_est"] = round(audio_s / 60 * GPU_S_PER_AUDIO_MIN, 1)
        if cost_estimate is not None:
            rep["cost_usd_est"] = round(sum(cost_estimate(c["end_s"] - c["start_s"])
                                            for _, clips in todo for c in clips), 4)
        return rep
    rep.update({"refined": 0, "failed": 0, "left_busy": 0, "changed_meanwhile": 0,
                "clips_transcribed": 0,
                "clips_reused": 0, "clips_failed": 0, "clips_kept_first_pass": 0,
                "stt_wall_s": 0.0, "spans": 0,
                "spans_text_refined": 0, "names_changed": 0, "names_unchanged": 0,
                "names_new": 0, "accepted_refined": 0, "kept_previous": 0,
                "kept_needs_review": 0, "kept_confidence_drop": 0, "kept_generic": 0,
                "confidence_up": 0, "confidence_down": 0,
                "confidence_same": 0, "confidence_delta_sum": 0.0, "cost_usd": 0.0})
    info = _LazyInfo(transcriber) if todo else {}  # describe() starts the child: only on first real clip
    ready = []
    for i, (ep_id, clips) in enumerate(todo, 1):
        try:
            doc = _transcribe_episode(dirs, ep_id, clips, transcriber, info, model, backend,
                                      rep, log, use_guard=guard)
        except SpeechUnavailable as exc:  # name what is ready, then stop (exit 3)
            rep["aborted"] = str(exc)[:500]
            rep["failed"] += 1
            log(f"[refine] STOPPING after {ep_id}: {exc}")
            break
        except BudgetExhausted as exc:  # clips so far are kept; name what is ready
            rep["budget_stopped"] = str(exc)[:500]
            log(f"[refine] budget reached at {ep_id}: {exc}")
            break
        if doc is None:
            rep["failed"] += 1
        else:
            ready.append((ep_id, doc))
        log(f"[refine] transcribed {i}/{len(todo)} {ep_id}: {len(clips)} clips"
            + ("" if doc is not None else " (a clip failed; retried next run)"))
    if hasattr(transcriber, "close"):
        transcriber.close()  # free the speech model before the namer loads
    if ready and callable(namer):
        namer = namer()
    for i, (ep_id, doc) in enumerate(ready, 1):
        path = dirs["names"] / f"{ep_id}.json"
        raw = path.read_bytes() if path.is_file() else None
        try:
            names = _parse(raw)
            tr = cr._read_json(dirs["transcripts"] / f"{ep_id}.json")
            new = rename(ep_id, names, cr._read_json(dirs["jev"] / f"{ep_id}.json"),
                         tr["segments"], doc, namer, model=model, threshold=threshold,
                         context_windows=context_windows, sub_window_s=sub_window_s)
        except Exception as exc:
            rep["failed"] += 1
            log(f"[refine] {ep_id} naming FAILED: {type(exc).__name__}: {exc}")
            continue
        if not _still_done(corpus, ep_id):  # requeued (e.g. repair-seams) meanwhile
            rep["left_busy"] += 1
            continue
        if not replace_if_unchanged(path, raw, new):  # e.g. a `rename` meanwhile
            rep["changed_meanwhile"] += 1
            log(f"[refine] {ep_id}: names file changed meanwhile; left alone")
            continue
        rep["refined"] += 1
        rep["spans"] += len(new["spans"])
        rep["spans_text_refined"] += sum(sp["text_source"] == "refined" for sp in new["spans"])
        for k, v in new["changes"].items():
            rep[k] = round(rep.get(k, 0) + v, 3)
        log(f"[refine] named {i}/{len(ready)} {ep_id}: " + (", ".join(
            f"{sp.get('previous_brand') or '-'}->{sp['brand'] or '?'}"
            f"({sp['text_source'][0]})" for sp in new["spans"]) or "no spans"))
    n = rep["names_changed"] + rep["names_unchanged"]
    rep["confidence_delta_mean"] = round(rep.pop("confidence_delta_sum") / n, 3) if n else None
    return rep


def _parse(raw: bytes | None):
    """As cr._read_json, on bytes already read."""
    try:
        return json.loads(raw) if raw is not None else None
    except ValueError:
        return None


def replace_if_unchanged(path, expected: bytes | None, doc: dict) -> bool:
    """Write `doc` to `path` (atomically) only if the file still holds
    `expected` (None: still absent). The check and the write run under the
    names write lock (bn.names_lock) every names writer takes, so no writer
    is overwritten after this one read the file. False: changed meanwhile,
    left alone."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with bn.names_lock(path.parent):
        if (path.read_bytes() if path.is_file() else None) != expected:
            return False
        atomic_write_json(path, doc)
        return True


def _still_done(corpus, ep_id: str) -> bool:
    con = cr.connect(corpus)
    try:
        row = con.execute("SELECT stage FROM episodes WHERE episode_id=?", (ep_id,)).fetchone()
    finally:
        con.close()
    return row is not None and row[0] == "done"


def stt_command(stt_python: str | None, model: str, backend: str, threads: int = 4) -> list:
    return [stt_python or sys.executable, str(cr.SCRIPT), "refine-stt", "--model", model,
            "--backend", backend, "--threads", str(threads)]


def summary_line(rep: dict) -> str:
    if rep.get("dry_run"):
        return (f"refine dry run: {rep['episodes']} episodes, {rep['clips']} clips, "
                f"{rep['audio_s']} audio-s, ~{rep['stt_s_est']} stt-s at "
                f"{GPU_S_PER_AUDIO_MIN} s/audio-min"
                + (f", ~${rep['cost_usd_est']} at list price" if "cost_usd_est" in rep else "")
                + f"; skipped {rep['skipped']}")
    return (f"refine: {rep['refined']} episodes refined, {rep['failed']} failed, "
            f"{rep['left_busy']} requeued meanwhile, {rep['changed_meanwhile']} names changed "
            f"meanwhile; clips {rep['clips_transcribed']} "
            f"transcribed, {rep['clips_reused']} reused, {rep['clips_failed']} failed, "
            f"{rep.get('clips_kept_first_pass', 0)} kept first pass (refine guard), "
            f"{rep['stt_wall_s']} stt-s; spans {rep['spans']} "
            f"({rep['spans_text_refined']} on refined text): names changed "
            f"{rep['names_changed']}, unchanged {rep['names_unchanged']}, new "
            f"{rep['names_new']}; accepted {rep['accepted_refined']}, kept previous "
            f"{rep['kept_previous']} (needs_review {rep['kept_needs_review']}, "
            f"confidence_drop {rep['kept_confidence_drop']}, generic {rep['kept_generic']}); "
            f"confidence up {rep['confidence_up']}, down "
            f"{rep['confidence_down']}, same {rep['confidence_same']}, mean delta "
            f"{rep['confidence_delta_mean']}; skipped {rep['skipped']}"
            + (f"; cost ${rep['cost_usd']}" if rep.get("cost_usd") else "")
            + (f"; BUDGET: {rep['budget_stopped']}" if rep.get("budget_stopped") else "")
            + (f"; STOPPED: {rep['aborted']}" if rep.get("aborted") else ""))
