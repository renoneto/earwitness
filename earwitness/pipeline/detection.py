"""P02 Jev screening + deterministic candidates (new product path).

The historical ``scripts/detect_all.py`` contract is read-only reference.
This module reuses its anchor/cluster pattern for deterministic candidates
but repairs attribution for NEW runs: ``brand_spoken`` is never populated
from URL evidence — URL-only evidence yields spoken unknown plus a URL
association. Old raw payloads are preserved untouched (never rewritten).

Jev screening issues ONE whole-transcript request per episode (never one
request per window), pins model + question/window schema, persists raw
scores + provider metadata, validates returned windows strictly, and
treats ``noul >= 0.80`` as a provisional queue setting only — never a
calibrated probability. Detector source evidence stays separate from
Jev windows. Paid inference runs only under job/budget authorization;
read paths never trigger it.
"""
from __future__ import annotations

import math
import re

from earwitness.app import access as _access
from earwitness.app import contracts as _contracts

from . import transcription as _t

PINNED_JEV_MODEL = "jev-1.13.0"
JEV_WINDOW_S = 60.0
JEV_QUESTION = ("For each timestamped 60-second transcript window, "
                "rate ad-read likelihood as noul in [0,1]. "
                "One structured state per episode.")
JEV_WINDOW_SCHEMA = "jev.window.v1"
PROVISIONAL_NOUL_THRESHOLD = 0.80

CTA_FIX_NOTE = ("NEW runs only: cta_url anchors keep brand_spoken=None "
                "with a url-channel association; legacy payloads that "
                "named brand_spoken from URL hints are preserved as-is.")

LEGACY_PAYLOAD_MARKERS = frozenset({"candidates_raw.json",
                                    "candidates_normalized.json"})


class DetectionError(ValueError):
    pass


class WindowValidationError(DetectionError):
    pass


def _finite(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value):
        raise DetectionError(f"{name} must be a finite number")
    return float(value)


# -- Deterministic candidates (CTA-URL channel fix for new runs) ---------------

_DOT = r"(?:\.|\s+dot\s+)"
_URL_RE = re.compile(
    r"(?:go to|visit|head to|head over to|check out|at)\s+"
    r"((?:(?!at\s)[\w-]+\s+){0,3}?(?:[\w-]+"
    + _DOT + r")+"
    r"(?:com|net|org|io|co|fm)(?:(?:/| slash |/)[\w\-%.~]*)?)", re.I)
_CODE_RES = [
    re.compile(r"use (?:the )?code ([A-Za-z0-9]{3,20})", re.I),
    re.compile(r"promo code ([A-Za-z0-9]{3,20})", re.I),
]
_SPONSOR_RES = [
    (re.compile(r"brought to you by\s+(.{1,60})", re.I), "brought_to_you"),
    (re.compile(r"sponsored by\s+(.{1,60})", re.I), "sponsored_by"),
    (re.compile(r"today'?s sponsor is\s+(.{1,60})", re.I), "todays_sponsor"),
    (re.compile(r"thanks to\s+(.{1,60}?)\s+for sponsoring", re.I), "thanks_to"),
]
_EMAIL_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
                  "icloud.com", "aol.com", "protonmail.com"}


def _normalize_url(u: str) -> str:
    u = re.sub(r"\s+slash\s+", "/", u, flags=re.I)
    u = re.sub(r"\s+dot\s+", ".", u, flags=re.I)
    return u.strip("., ")


def extract_urls(text: str) -> list:
    out = set()
    for u in _URL_RE.findall(text):
        u = _normalize_url(u)
        root = u.lower().split("/")[0]
        if root in _EMAIL_DOMAINS:
            continue
        out.add(u)
    return sorted(out)


def brand_hint_from_url(url: str) -> str | None:
    low = url.lower().split("/")[0]
    return low or None


def _spoken_anchor(text: str):
    for rx, kind in _SPONSOR_RES:
        m = rx.search(text)
        if m:
            chunk = re.split(r"[.!?;:,]", m.group(1), maxsplit=1)[0].strip()
            if len(chunk) >= 2:
                return chunk, kind
    return None, None


def deterministic_candidates(episode_id: str, segments,
                             context_before: int = 2,
                             context_after: int = 25) -> list:
    """Deterministic CTA/anchor candidates over the full transcript.

    CTA-URL fix: a URL-only anchor produces ``brand_spoken=None`` with a
    ``url``-channel association — never spoken attribution from URL text.
    """
    segs = _t.validate_segments(list(segments))
    anchors = []
    for i, s in enumerate(segs):
        text = s["text"]
        raw, kind = _spoken_anchor(text)
        if raw:
            anchors.append({"seg_idx": i, "brand_spoken": raw,
                            "anchor_kind": kind, "channel": "spoken",
                            "urls": [], "codes": []})
            continue
        urls = extract_urls(text)
        if urls:
            anchors.append({"seg_idx": i, "brand_spoken": None,
                            "anchor_kind": "cta_url", "channel": "url",
                            "urls": urls, "codes": []})
            continue
        codes = [m for rx in _CODE_RES for m in rx.findall(text)]
        if codes or re.search(r"promo code|for \d+% off", text, re.I):
            anchors.append({"seg_idx": i, "brand_spoken": None,
                            "anchor_kind": "marker_only", "channel": "marker",
                            "urls": [], "codes": codes})
    out = []
    for ci, anc in enumerate(anchors):
        lo = max(0, anc["seg_idx"] - context_before)
        hi = min(len(segs), anc["seg_idx"] + context_after)
        window = segs[lo:hi]
        attr_text = segs[anc["seg_idx"]]["text"]
        urls = anc["urls"] or extract_urls(attr_text)
        codes = anc["codes"] or [m for rx in _CODE_RES
                                 for m in rx.findall(attr_text)]
        out.append({
            "candidate_id": f"{episode_id}.c{ci}",
            "ep_id": episode_id,
            "brand_spoken": anc["brand_spoken"],  # None for URL-only: unknown
            "brand": anc["brand_spoken"] or "(unnamed)",
            "anchor": anc["anchor_kind"],
            "channel": anc["channel"],
            "offer_codes": sorted(set(codes)),
            "urls": urls,
            "url_association": urls[0] if urls and anc["brand_spoken"] is None
            else None,
            "start_s": window[0]["start"],
            "end_s": window[-1]["end"],
            "anchor_seg_idx": [anc["seg_idx"]],
        })
    return out


def is_legacy_payload(path: str) -> bool:
    return any(marker in path for marker in LEGACY_PAYLOAD_MARKERS)


# -- Jev whole-transcript operation ----------------------------------------------

def windows_for_transcript(segments, window_s: float = JEV_WINDOW_S) -> list:
    segs = _t.validate_segments(list(segments))
    if not segs:
        return []
    duration = segs[-1]["end"]
    windows = []
    start = 0.0
    idx = 0
    while start < duration:
        end = min(start + window_s, duration)
        text = " ".join(s["text"] for s in segs
                        if s["start"] < end and s["end"] > start)
        windows.append({"window_id": f"window_{idx:03d}",
                        "start_s": start, "end_s": end, "text": text})
        start = end
        idx += 1
    return windows


def build_jev_request(episode_id: str, segments,
                      window_s: float = JEV_WINDOW_S) -> dict:
    """One bounded request for the whole transcript — never one request
    per window."""
    windows = windows_for_transcript(segments, window_s)
    return {
        "schema": JEV_WINDOW_SCHEMA,
        "model": PINNED_JEV_MODEL,
        "question": JEV_QUESTION,
        "episode_id": episode_id,
        "window_seconds": window_s,
        "windows": windows,  # single payload carrying all windows
    }


def validate_jev_response(request: dict, response: dict) -> dict:
    expected = request.get("windows", [])
    answers = response.get("answers")
    if not isinstance(answers, dict):
        raise WindowValidationError("response answers must be a dict")
    if set(answers) != {w["window_id"] for w in expected}:
        raise WindowValidationError(
            f"returned windows {sorted(answers)} do not match requested "
            f"{[w['window_id'] for w in expected]}")
    for w in expected:
        ans = answers[w["window_id"]]
        if not isinstance(ans, dict):
            raise WindowValidationError(f"{w['window_id']} must be a dict")
        noul = ans.get("noul")
        _finite(noul, f"{w['window_id']}.noul")
        if not 0.0 <= float(noul) <= 1.0:
            raise WindowValidationError(
                f"{w['window_id']}.noul out of [0,1]")
        if abs(float(ans.get("start_s", w["start_s"])) - w["start_s"]) > 1e-6 \
                or abs(float(ans.get("end_s", w["end_s"])) - w["end_s"]) > 1e-6:
            raise WindowValidationError(
                f"{w['window_id']} bounds do not match request")
    return {"validated_windows": len(expected),
            "model": response.get("model", PINNED_JEV_MODEL)}


def persist_jev_record(episode_id: str, request: dict, response: dict,
                       provider_metadata: dict) -> dict:
    """Persist raw scores + provider metadata (never just the queue)."""
    validation = validate_jev_response(request, response)
    return {
        "schema_version": 1,
        "episode_id": episode_id,
        "model": response.get("model", PINNED_JEV_MODEL),
        "question": request.get("question"),
        "window_schema": request.get("schema"),
        "raw_scores": {wid: float(ans["noul"])
                       for wid, ans in response["answers"].items()},
        "provider_metadata": dict(provider_metadata),
        "validation": validation,
    }


def provisional_queue(record: dict,
                      threshold: float = PROVISIONAL_NOUL_THRESHOLD) -> dict:
    """``noul >= 0.80`` is a provisional queue setting, never a calibrated
    ad probability."""
    queued = sorted(wid for wid, score in record["raw_scores"].items()
                    if score >= threshold)
    return {"window_ids": queued, "threshold": threshold,
            "basis": "provisional queue setting; not a calibrated "
                     "probability of an ad"}


def combine_screening(deterministic: list, jev_record: dict,
                      jev_queue: dict, user_requested_regions: list) -> dict:
    """Detector source evidence stays separate from Jev windows; explicit
    user-requested regions ride along even below threshold."""
    below = [r for r in user_requested_regions]
    return {
        "deterministic_candidates": list(deterministic),
        "jev_windows": {"record": jev_record, "queue": jev_queue},
        "user_requested_regions": below,
        "note": "deterministic source evidence and Jev windows are "
                "separate inputs to refinement selection",
    }


def run_jev_screening(episode_id: str, segments, ctx: _access.AccessContext,
                      budget: _access.ExecutionBudget, jev_fn,
                      provider_metadata: dict | None = None,
                      operation: str = "submit_processing",
                      read_path: str | None = None,
                      user_requested_regions: list | None = None) -> dict:
    """Bounded repeatable Jev operation: one authorized whole-transcript
    call with strict validation. Read paths are refused paid inference."""
    if read_path is not None:
        _t.refuse_paid_on_read(read_path, True)
    if not isinstance(ctx, _access.AccessContext):
        raise DetectionError("server-constructed context required")
    ctx.require_member(operation)
    budget.check(operation)
    request = build_jev_request(episode_id, segments)
    response = jev_fn(request)  # single provider call for all windows
    record = persist_jev_record(episode_id, request, response,
                                provider_metadata or {})
    queue = provisional_queue(record)
    det = deterministic_candidates(episode_id, segments)
    return combine_screening(det, record, queue,
                             user_requested_regions or [])
