"""P02 fast full transcript + focused refinement + P03 backend profiles.

Pure stdlib processing logic. Real model/paid inference runs only through
``run_backend`` under job/budget authorization (server-constructed
``AccessContext`` + ``ExecutionBudget``); search/review read paths never
invoke backends. No English forcing; VAD skips silence only.
"""
from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field

from earwitness.app import access as _access
from earwitness.app import contracts as _contracts

SCHEMA_VERSION = 1
FULL_VERSION_KIND = "full"
REFINEMENT_VERSION_KIND = "refinement"

CONTEXT_S = 15.0
FULL_REFINEMENT_COVERAGE = 0.70

# Read paths must never trigger paid inference.
READ_PATHS = frozenset({
    "search_evidence", "evidence_detail", "transcript_window",
    "research_overview", "brand_detail", "show_detail",
})

REQUIRED_PROFILE_FIELDS = (
    "backend", "backend_version", "model", "weights_digest", "device",
    "precision", "batch_size", "beam_size", "vad", "language_policy",
    "word_timestamp_mode", "thread_limit", "memory_limit_mb",
)

SUPPORTED_DEVICES = frozenset({"cpu", "mlx", "cuda"})


class TranscriptError(ValueError):
    pass


class UnsupportedDeviceError(TranscriptError):
    pass


class AuthorizationMissingError(TranscriptError):
    pass


def _finite(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value):
        raise TranscriptError(f"{name} must be a finite number")
    return float(value)


def validate_segments(segments) -> list:
    if not isinstance(segments, list):
        raise TranscriptError("segments must be a list")
    out = []
    for i, s in enumerate(segments):
        if not isinstance(s, dict):
            raise TranscriptError(f"segment {i} must be a dict")
        start = _finite(s.get("start"), f"segments[{i}].start")
        end = _finite(s.get("end"), f"segments[{i}].end")
        text = s.get("text")
        if not isinstance(text, str):
            raise TranscriptError(f"segments[{i}].text must be a string")
        if start < 0 or end < 0:
            raise TranscriptError(f"segments[{i}] offsets must be >= 0")
        if end < start:
            raise TranscriptError(f"segments[{i}].end < start")
        out.append({"start": start, "end": end, "text": text})
    return out


def validate_language_policy(policy) -> dict:
    """No English forcing: uncertain/multilingual material keeps evidence."""
    if not isinstance(policy, dict):
        raise TranscriptError("language_policy must be a dict")
    mode = policy.get("mode", "auto")
    if mode in ("force_english", "force_en", "en-only"):
        raise TranscriptError("language policy must not force English")
    if mode not in ("auto", "uncertain-keep", "pinned"):
        raise TranscriptError(f"unknown language mode: {mode!r}")
    return dict(policy)


def validate_vad(vad) -> dict:
    """VAD may skip silence within transcription, never decide ads."""
    if not isinstance(vad, dict):
        raise TranscriptError("vad must be a dict")
    if vad.get("decide_ads") or vad.get("ad_decision"):
        raise TranscriptError("VAD must not decide ad presence")
    return dict(vad)


def validate_profile(profile: dict) -> dict:
    if not isinstance(profile, dict):
        raise TranscriptError("backend profile must be a dict")
    missing = [k for k in REQUIRED_PROFILE_FIELDS if k not in profile]
    if missing:
        raise TranscriptError(f"profile missing fields: {missing}")
    device = profile.get("device")
    if device not in SUPPORTED_DEVICES:
        raise UnsupportedDeviceError(
            f"unsupported device: {device!r}; no silent fallback applied")
    validate_language_policy(profile.get("language_policy"))
    validate_vad(profile.get("vad"))
    _finite(profile.get("batch_size"), "batch_size")
    _finite(profile.get("beam_size"), "beam_size")
    return dict(profile)


def build_full_transcript(episode_id: str, media_artifact_id: str,
                          media_digest: str, segments, profile: dict,
                          language: str | None = None, fetch_date: str = "",
                          fetch_date_source: str = "") -> dict:
    if not episode_id or not media_artifact_id or not media_digest:
        raise TranscriptError("episode/media identity is required")
    segs = validate_segments(list(segments))
    prof = validate_profile(profile)
    return {
        "schema_version": SCHEMA_VERSION,
        "version_kind": FULL_VERSION_KIND,
        "version_id": f"full:{episode_id}:{prof['backend']}:{prof['model']}",
        "episode_id": episode_id,
        "media_artifact_id": media_artifact_id,
        "media_digest": media_digest,
        "backend_profile": prof,
        "language": language,  # retained evidence, never forced
        "segments": segs,
        "fetch_date": fetch_date,
        "fetch_date_source": fetch_date_source,
    }


def map_crop_to_original(crop_start_s: float, backend_t: float) -> float:
    """Single original-audio time conversion. Crop-relative backend
    timestamps map once: original = crop_start + backend_t."""
    crop = _finite(crop_start_s, "crop_start_s")
    ts = _finite(backend_t, "backend_t")
    if crop < 0 or ts < 0:
        raise TranscriptError("offsets must be >= 0")
    return crop + ts


def map_segment_to_original(crop_start_s: float, segment: dict) -> dict:
    mapped = dict(segment)
    mapped["start"] = map_crop_to_original(crop_start_s, segment["start"])
    mapped["end"] = map_crop_to_original(crop_start_s, segment["end"])
    return mapped


def build_refinement_transcript(episode_id: str, base_version_id: str,
                                media_artifact_id: str, media_digest: str,
                                crop_start_s: float, crop_end_s: float,
                                backend_segments, profile: dict,
                                fetch_date: str = "",
                                fetch_date_source: str = "") -> dict:
    crop_start = _finite(crop_start_s, "crop_start_s")
    crop_end = _finite(crop_end_s, "crop_end_s")
    if crop_end <= crop_start:
        raise TranscriptError("crop_end_s must exceed crop_start_s")
    raw = validate_segments(list(backend_segments))
    prof = validate_profile(profile)
    # Backend timestamps are crop-relative; convert exactly once.
    mapped = [map_segment_to_original(crop_start, s) for s in raw]
    for s in mapped:
        if s["end"] > crop_end + 1e-6:
            raise TranscriptError("refined segment exceeds crop bounds")
    return {
        "schema_version": SCHEMA_VERSION,
        "version_kind": REFINEMENT_VERSION_KIND,
        "version_id": f"ref:{episode_id}:{crop_start:.2f}-{crop_end:.2f}",
        "episode_id": episode_id,
        "base_version_id": base_version_id,
        "media_artifact_id": media_artifact_id,
        "media_digest": media_digest,
        "crop_start_s": crop_start,
        "crop_end_s": crop_end,
        "backend_timestamps": [ {"start": s["start"], "end": s["end"]}
                                for s in raw ],
        "segments": mapped,
        "backend_profile": prof,
        "fetch_date": fetch_date,
        "fetch_date_source": fetch_date_source,
    }


def apply_refinement_failure(full_payload: dict, error: str) -> dict:
    """Refinement failure leaves a searchable fast transcript + partial flag."""
    if full_payload.get("version_kind") != FULL_VERSION_KIND:
        raise TranscriptError("failure fallback requires the full transcript")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "partial",
        "searchable_version_id": full_payload["version_id"],
        "segments": full_payload["segments"],
        "reason": f"refinement failed: {error}",
        "effective_version_id": full_payload["version_id"],
    }


# -- Backend invocation behind job/budget authorization ----------------------

def require_transcription_authorization(ctx: _access.AccessContext,
                                        budget: _access.ExecutionBudget,
                                        operation: str = "submit_processing") -> None:
    if not isinstance(ctx, _access.AccessContext):
        raise AuthorizationMissingError("server-constructed context required")
    ctx.require_member(operation)
    budget.check(operation)


def refuse_paid_on_read(path: str, wants_paid: bool) -> None:
    if path in READ_PATHS and wants_paid:
        raise _contracts.ContractError(
            f"no paid inference from read path: {path}",
            _contracts.HTTP_BAD_REQUEST)


def run_backend(profile: dict, audio_ref: dict, ctx: _access.AccessContext,
                budget: _access.ExecutionBudget, backend_fn,
                operation: str = "submit_processing",
                read_path: str | None = None) -> dict:
    """Invoke a transcription backend only under explicit authorization.

    ``backend_fn`` is an injected callable (tests use fakes); real model
    downloads/paid inference additionally require owner authorization
    recorded by the caller. Search/review paths are refused outright.
    """
    if read_path is not None:
        refuse_paid_on_read(read_path, True)
    require_transcription_authorization(ctx, budget, operation)
    prof = validate_profile(profile)
    if not isinstance(audio_ref, dict) or not audio_ref.get("media_artifact_id"):
        raise TranscriptError("audio_ref with media_artifact_id required")
    return backend_fn(prof, audio_ref)


# -- One model load per worker ------------------------------------------------

@dataclass
class ModelResidency:
    """Tracks the single loaded model per worker process."""
    loaded_profile_id: str | None = None
    load_events: list = field(default_factory=list)

    def ensure_loaded(self, profile: dict, load_fn=None,
                      now_s: float | None = None) -> dict:
        prof = validate_profile(profile)
        pid = f"{prof['backend']}/{prof['backend_version']}/{prof['model']}"
        if self.loaded_profile_id == pid:
            return {"reused": True, "profile_id": pid}
        started = time.monotonic()
        if load_fn is not None:
            load_fn(prof)
        elapsed = (time.monotonic() - started) if now_s is None else now_s
        if self.loaded_profile_id is not None:
            self.load_events.append({"event": "unload",
                                     "profile_id": self.loaded_profile_id})
        self.loaded_profile_id = pid
        event = {"event": "load", "profile_id": pid,
                 "load_time_s": elapsed}
        self.load_events.append(event)
        return {"reused": False, "profile_id": pid, "load_time_s": elapsed}


# -- Refinement span selection -------------------------------------------------

def union_spans(candidates, context_s: float = CONTEXT_S,
                media_duration_s: float | None = None) -> list:
    spans = []
    for c in candidates:
        start = _finite(c["start_s"], "candidate.start_s")
        end = _finite(c["end_s"], "candidate.end_s")
        if end < start:
            raise TranscriptError("candidate end precedes start")
        lo = max(0.0, start - context_s)
        hi = end + context_s
        if media_duration_s is not None:
            dur = _finite(media_duration_s, "media_duration_s")
            hi = min(hi, dur)
        spans.append({"start": lo, "end": hi,
                      "member_ids": [c.get("candidate_id")]})
    return spans


def join_overlapping(spans) -> list:
    ordered = sorted(spans, key=lambda s: (s["start"], s["end"]))
    out = []
    for s in ordered:
        if out and s["start"] <= out[-1]["end"]:
            out[-1]["end"] = max(out[-1]["end"], s["end"])
            out[-1]["member_ids"].extend(s["member_ids"])
        else:
            out.append({"start": s["start"], "end": s["end"],
                        "member_ids": list(s["member_ids"])})
    return out


def selection_decision(candidates, media_duration_s: float,
                       context_s: float = CONTEXT_S) -> dict:
    """Union ±15s clipped to duration; join overlaps; >=70% union → one
    full refinement pass with recorded decision."""
    duration = _finite(media_duration_s, "media_duration_s")
    if duration <= 0:
        raise TranscriptError("media_duration_s must be positive")
    union = join_overlapping(
        union_spans(list(candidates), context_s, duration))
    covered = sum(s["end"] - s["start"] for s in union)
    coverage = covered / duration
    if coverage >= FULL_REFINEMENT_COVERAGE:
        return {"mode": "full_pass",
                "compute_spans": [{"start": 0.0, "end": duration,
                                   "member_ids": [c.get("candidate_id")
                                                  for c in candidates]}],
                "coverage": coverage,
                "basis": (f"union coverage {coverage:.3f} >= "
                          f"{FULL_REFINEMENT_COVERAGE:.2f}; single full "
                          "refinement pass avoids repeated crops")}
    return {"mode": "crops", "compute_spans": union, "coverage": coverage,
            "basis": (f"union coverage {coverage:.3f} below "
                      f"{FULL_REFINEMENT_COVERAGE:.2f}; bounded crop passes")}


def sample_below_threshold(regions, approved_run: dict | None) -> dict:
    """Below-threshold sampling only inside approved runs with recorded basis."""
    if not approved_run or not approved_run.get("approved"):
        raise _contracts.ContractError(
            "below-threshold sampling requires an approved run",
            _contracts.HTTP_PRECONDITION_REQUIRED)
    basis = approved_run.get("sampling_basis", "")
    if not basis:
        raise _contracts.ContractError(
            "approved run must record a sampling basis",
            _contracts.HTTP_BAD_REQUEST)
    return {"sampled": list(regions), "basis": basis,
            "run_id": approved_run.get("run_id", "")}


# -- Effective transcript rule ---------------------------------------------------

def format_match(version_id: str, seg_idx: int, char_start: int,
                 char_end: int, orig_start_s: float,
                 orig_end_s: float) -> dict:
    return {"version_id": version_id, "seg_idx": int(seg_idx),
            "char_start": int(char_start), "char_end": int(char_end),
            "orig_start_s": _finite(orig_start_s, "orig_start_s"),
            "orig_end_s": _finite(orig_end_s, "orig_end_s")}


def select_effective_version(full_version: dict, refinements: list,
                             interval: dict,
                             requested_version_id: str | None = None) -> dict:
    """Default reads/search use the pinned FULL version; a refinement is
    selectable explicitly and only when its crop fully contains the
    interval with every supporting segment inside the crop. Crop-edge or
    ambiguous intervals use full + refinement as alternative. Alternatives
    are never concatenated."""
    if full_version.get("version_kind") != FULL_VERSION_KIND:
        raise TranscriptError("full_version must be the pinned full transcript")
    start = _finite(interval["start_s"], "interval.start_s")
    end = _finite(interval["end_s"], "interval.end_s")
    supporting = interval.get("supporting_segments", [])
    if requested_version_id is None:
        return {"effective_version_id": full_version["version_id"],
                "reason": "default pinned full version",
                "alternatives": [r["version_id"] for r in refinements]}
    matches = [r for r in refinements
               if r.get("version_id") == requested_version_id]
    if not matches:
        raise TranscriptError(f"unknown refinement: {requested_version_id}")
    chosen = matches[0]
    fully_inside = (chosen["crop_start_s"] <= start
                    and end <= chosen["crop_end_s"])
    segs_inside = all(chosen["crop_start_s"] <= s["start"]
                      and s["end"] <= chosen["crop_end_s"]
                      for s in supporting) if supporting else fully_inside
    if fully_inside and segs_inside:
        return {"effective_version_id": chosen["version_id"],
                "reason": "explicit refinement fully contains interval",
                "alternatives": [full_version["version_id"]]}
    return {"effective_version_id": full_version["version_id"],
            "reason": "crop-edge/ambiguous: full version with "
                      "refinement as alternative",
            "alternatives": [chosen["version_id"]]}


def new_revision_on_version_change(pins: dict, old_version_id: str,
                                   new_version_id: str) -> dict:
    """Changing the effective version creates a new revision; existing
    review/citation pins never move."""
    if old_version_id == new_version_id:
        raise TranscriptError("version unchanged; no new revision")
    return {"revision_id": f"rev-{uuid.uuid4().hex[:12]}",
            "old_version_id": old_version_id,
            "new_version_id": new_version_id,
            "review_pins": dict(pins),
            "citation_pins": dict(pins)}


# -- P03 qualification -----------------------------------------------------------

QUALIFICATION_SHEET_FIELDS = (
    "source_hashes", "source_categories", "reference_provenance",
    "required_names", "required_codes", "required_domains",
    "normalization", "error_limits", "timestamp_tolerance_s",
    "resource_caps",
)


def freeze_qualification_sheet(sheet: dict) -> dict:
    missing = [k for k in QUALIFICATION_SHEET_FIELDS if k not in sheet]
    if missing:
        raise TranscriptError(
            f"qualification sheet missing: {missing}")
    if not sheet["source_hashes"]:
        raise TranscriptError("qualification sheet needs approved source hashes")
    tol = _finite(sheet["timestamp_tolerance_s"], "timestamp_tolerance_s")
    if tol <= 0:
        raise TranscriptError("timestamp tolerance must be positive")
    frozen = dict(sheet)
    frozen["frozen"] = True
    return frozen


def run_comparison(sheet: dict | None, profiles: list,
                   measurements: dict, approved_media: dict | None = None,
                   selected_profile_id: str | None = None) -> dict:
    """Bounded comparison on APPROVED media only. Without approved media
    hashes + explicit profile selection, or without a trustworthy
    reference, report throughput-only and leave quality unqualified —
    never invent a winner."""
    if sheet is None or not sheet.get("frozen"):
        raise TranscriptError("freeze the qualification sheet first")
    if not approved_media or not approved_media.get("hashes"):
        return {"result": "blocked",
                "reason": "media selection authorization absent; "
                          "qualification blocked",
                "measurements": dict(measurements)}
    if not selected_profile_id:
        raise TranscriptError("explicit profile selection required")
    usable = [p for p in profiles
              if p.get("profile_id") == selected_profile_id]
    if not usable:
        raise TranscriptError("selected profile not in candidate set")
    reference = approved_media.get("reference_provenance")
    if not reference:
        return {"result": "throughput-only",
                "reason": "no trustworthy inspected reference; quality "
                          "selection unqualified",
                "measurements": dict(measurements)}
    tol = _finite(sheet["timestamp_tolerance_s"], "timestamp_tolerance_s")
    limits = sheet.get("error_limits", {})
    try:
        mem_cap = _finite((sheet.get("resource_caps") or {}).get("peak_rss_mb"),
                          "resource_caps.peak_rss_mb")
    except TranscriptError:
        return {"result": "throughput-only",
                "reason": "sheet carries no resource caps; quality "
                          "selection unqualified",
                "measurements": dict(measurements)}
    contenders = []
    for p in profiles:
        m = measurements.get(p.get("profile_id"), {})
        detail_errors = m.get("critical_detail_errors", float("inf"))
        timing_errors = m.get("timestamp_errors_s", [float("inf")])
        worst_timing = max(timing_errors) if timing_errors else float("inf")
        if detail_errors <= limits.get("critical_detail", 0) \
                and worst_timing <= tol \
                and m.get("peak_rss_mb", float("inf")) <= mem_cap:
            contenders.append((m.get("wall_time_s", float("inf")),
                               p.get("profile_id")))
    if not contenders:
        return {"result": "throughput-only",
                "reason": "no profile met the critical-detail/timing "
                          "contract; no winner invented",
                "measurements": dict(measurements)}
    contenders.sort()
    winner = contenders[0][1]
    alternatives = [pid for _, pid in contenders[1:]]
    return {"result": "qualified", "selected_profile_id": winner,
            "non_dominated_alternatives": alternatives,
            "measurements": dict(measurements)}
