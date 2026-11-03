#!/usr/bin/env python3
"""EarWitness owner-review server: local-only transcript labeling backend.

Serves /review/ (static files from review/) and same-origin JSON API
endpoints for bootstrap, transcript retrieval, owner-label save/complete,
and export. Python standard library only. Never fetches network resources.

Usage:
    python3 scripts/review_server.py [--host 127.0.0.1] [--port 8765]
        [--repo-root /path/to/earwitness-validation]
        [--storage-dir labels/next-validation-20260921]
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

EP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
WAVE_ID = "next-validation-20260921"
PROTOCOL_VERSION = "1.0"
AUDIO_PROFILES = {
    "not_reviewed", "regular", "studio_insert", "possible_transition", "uncertain",
}
READ_TYPES = {"host-read", "produced_spot", "uncertain", "organic_mention"}
STATUSES = {"draft", "complete"}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
# Tight allowlist for locally served practice audio: public URL name -> repo path.
# The clip endpoint serves ONLY these names; no arbitrary paths, no URL proxy.
CLIP_ALLOWLIST = {
    "s002e1_shopify_1276s.mp3": "clips/s002e1_shopify_1276s.mp3",
}
AUDIO_EXAMPLES_MANIFEST = "reports/audio-examples.json"
JEV_RUN_PATH = "experiments/jev-ad-triage-20260921/results.json"
JEV_FEEDBACK_PATH = "experiments/jev-ad-triage-20260921/feedback.json"
JEV_THRESHOLD = 0.80
JEV_EXCERPT_CHARS = 600
JEV_VERDICTS = {"correct", "wrong", "unsure"}
JEV_NOTE_CHARS = 500


REPO_ROOT = Path(__file__).resolve().parent.parent


def utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


class Store:
    """Repository-backed state: transcripts, metadata, accepted + owner labels."""

    def __init__(self, repo_root: Path, storage_dir: str):
        self.repo = repo_root
        self.storage = (repo_root / storage_dir).resolve()
        # Refuse to escape the repository.
        if self.storage != repo_root and repo_root not in self.storage.parents:
            raise ValueError("storage dir must live inside the repository")
        self.storage.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.storage / "manifest.json"
        self._transcript_ids: set[str] | None = None
        self._candidates: list | None = None
        # ThreadingHTTPServer serves each request on its own thread against
        # this shared Store; serialize feedback read-modify-write so
        # concurrent verdicts cannot lose each other.
        self._feedback_lock = threading.Lock()

    # -- corpus ---------------------------------------------------------
    def transcript_ids(self) -> list[str]:
        tdir = self.repo / "transcripts"
        ids = sorted(p.stem for p in tdir.glob("*.json") if p.is_file())
        self._transcript_ids = set(ids)
        return ids

    def valid_episode(self, ep_id: str) -> bool:
        if not EP_ID_RE.match(ep_id or ""):
            return False
        if self._transcript_ids is None:
            self.transcript_ids()
        return ep_id in (self._transcript_ids or set())

    def transcript_path(self, ep_id: str) -> Path:
        return self.repo / "transcripts" / (ep_id + ".json")

    def load_transcript(self, ep_id: str):
        return load_json(self.transcript_path(ep_id))

    def episodes_meta(self) -> dict:
        """Metadata keyed by ep_id from data/episodes.json (s* ids)."""
        out: dict = {}
        eps = load_json(self.repo / "data" / "episodes.json", default=[])
        if isinstance(eps, list):
            for e in eps:
                if isinstance(e, dict) and e.get("ep_id"):
                    out[e["ep_id"]] = {
                        "ep_id": e.get("ep_id"),
                        "show_title": e.get("show_title"),
                        "ep_title": e.get("ep_title"),
                        "category": e.get("category"),
                        "fetch_date": e.get("fetch_date"),
                        "audio_url": e.get("audio_url"),
                    }
        return out

    def sample(self) -> dict:
        return load_json(self.repo / "reports" / "next-labeling-sample.json", default={}) or {}

    def audio_examples(self) -> list:
        """Practice-only examples: local clip served by this server, remote
        full episodes streamed by the browser from data/episodes.json URLs.
        No gold labels, no candidates, no confidence anywhere in this path."""
        manifest = load_json(self.repo / AUDIO_EXAMPLES_MANIFEST, default={}) or {}
        meta = self.episodes_meta()
        if self._transcript_ids is None:
            self.transcript_ids()
        tids = self._transcript_ids or set()
        out = []
        for ex in manifest.get("examples", []) or []:
            if not isinstance(ex, dict):
                continue
            ep_id = ex.get("ep_id")
            if ep_id not in tids:
                continue
            try:
                offset = float(ex.get("source_offset_s", 0))
            except (TypeError, ValueError):
                continue
            if ex.get("kind") == "local_clip":
                name = str(ex.get("clip_path") or "").rsplit("/", 1)[-1]
                if name not in CLIP_ALLOWLIST:
                    continue
                src = f"/api/clip/{name}"
            else:
                url = (meta.get(ep_id) or {}).get("audio_url")
                if not url:
                    continue
                src = url
            out.append({
                "id": ex.get("id"),
                "ep_id": ep_id,
                "kind": ex.get("kind"),
                "label": ex.get("label"),
                "src": src,
                "source_offset_s": offset,
            })
        return out

    def episode_audio(self, ep_id: str):
        """Normal-review playback source: the episode's remote full audio_url,
        else None. The bundled 90s local clip is practice-only and is exposed
        solely through the practice example, never as episode audio — cue
        timestamps span the full transcript and would seek outside the clip."""
        url = (self.episodes_meta().get(ep_id) or {}).get("audio_url")
        if url:
            return {"src": url, "source_offset_s": 0.0, "kind": "remote_full"}
        return None

    def accepted_ids(self) -> list[str]:
        ldir = self.repo / "labels"
        return sorted(
            p.stem for p in ldir.glob("*.json")
            if p.is_file() and EP_ID_RE.match(p.stem)
        )

    def accepted_label(self, ep_id: str):
        p = self.repo / "labels" / (ep_id + ".json")
        if not p.is_file():
            return None
        return load_json(p)

    def normalized_candidates(self) -> list:
        """Raw detector output; only ever exposed via the gated cues endpoint."""
        if self._candidates is None:
            rows = load_json(self.repo / "output" / "candidates_normalized.json", default=[])
            self._candidates = rows if isinstance(rows, list) else []
        return self._candidates

    def cues_allowed(self, ep_id: str) -> bool:
        """Detector cues unlock only after independent labeling is locked:
        an accepted root label exists, or the owner label is complete with
        independent_read_confirmed=true."""
        if self.accepted_label(ep_id) is not None:
            return True
        own = self.owner_label(ep_id)
        rev = (own.get("review") or {}) if isinstance(own, dict) else {}
        return (rev.get("status") == "complete"
                and rev.get("independent_read_confirmed") is True)

    def jev_queue(self) -> dict:
        """Read-only Jev triage queue from the fixed run file. Windows at or
        above threshold only; excerpts assembled from local transcripts;
        audio via the normal episode mapping. No owner labels, no detector
        candidates, nothing write-capable."""
        run = load_json(self.repo / JEV_RUN_PATH, default={}) or {}
        meta = self.episodes_meta()
        if self._transcript_ids is None:
            self.transcript_ids()
        tids = self._transcript_ids or set()
        entries = []
        for ep in run.get("episodes", []) or []:
            if not isinstance(ep, dict):
                continue
            ep_id = ep.get("episode_id") or ep.get("ep_id")
            if not self.valid_episode(ep_id or "") or ep_id not in tids:
                continue
            t = self.load_transcript(ep_id) or {}
            segs = t.get("segments", []) if isinstance(t, dict) else []
            m = meta.get(ep_id, {})
            answers = ep.get("answers", {}) if isinstance(ep.get("answers"), dict) else {}
            for wid in sorted(answers):
                w = answers[wid]
                if not isinstance(w, dict):
                    continue
                try:
                    noul = float(w.get("noul", 0))
                    start = float(w.get("start_s", 0))
                    end = float(w.get("end_s", 0))
                except (TypeError, ValueError):
                    continue
                if noul < JEV_THRESHOLD:
                    continue
                excerpt = " ".join(
                    str(s.get("text", "")) for s in segs
                    if isinstance(s, dict)
                    and float(s.get("start", float("inf"))) < end
                    and float(s.get("end", float("-inf"))) > start
                ).strip()[:JEV_EXCERPT_CHARS]
                entries.append({
                    "ep_id": ep_id,
                    "show_title": m.get("show_title") or t.get("show"),
                    "ep_title": m.get("ep_title") or t.get("ep_title"),
                    "category": m.get("category"),
                    "window_id": wid,
                    "start_s": start,
                    "end_s": end,
                    "noul": noul,
                    "excerpt": excerpt,
                    "audio": self.episode_audio(ep_id),
                })
        entries.sort(key=lambda e: (e["ep_id"], e["start_s"]))
        agg = run.get("aggregate", {}) if isinstance(run.get("aggregate"), dict) else {}
        models = run.get("model_returned") or []
        return {
            "run_id": run.get("run_id"),
            "model": models[0] if models else run.get("model_requested"),
            "threshold": JEV_THRESHOLD,
            "summary": {
                "episode_count": agg.get("episode_count"),
                "total_runtime_hours": agg.get("total_runtime_hours"),
                "total_input_cost_usd": agg.get("total_input_cost_usd"),
                "queue_windows": len(entries),
            },
            "entries": entries,
        }

    def jev_feedback_path(self) -> Path:
        return self.repo / JEV_FEEDBACK_PATH

    def jev_feedback(self) -> dict:
        """Saved per-window verdicts. Separate artifact; never part of owner
        labels or export. Returns run/model/threshold, entries, and summary.
        Raises ValueError when the existing store is unreadable."""
        doc = self.jev_feedback_doc()
        entries = doc.get("entries", {})
        if not isinstance(entries, dict):
            raise ValueError("existing feedback entries are malformed")
        clean = {k: v for k, v in entries.items()
                 if isinstance(k, str) and isinstance(v, dict)
                 and v.get("verdict") in JEV_VERDICTS}
        summary = {"correct": 0, "wrong": 0, "unsure": 0}
        for v in clean.values():
            summary[v["verdict"]] += 1
        run = load_json(self.repo / JEV_RUN_PATH, default={}) or {}
        models = run.get("model_returned") or []
        return {
            "run_id": run.get("run_id"),
            "model": models[0] if models else run.get("model_requested"),
            "threshold": JEV_THRESHOLD,
            "entries": clean,
            "summary": summary,
        }

    def jev_feedback_doc(self) -> dict:
        """Raw feedback document. Raises ValueError when an existing file is
        unreadable, so callers never silently overwrite it as empty."""
        path = self.jev_feedback_path()
        if not path.is_file():
            return {}
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"existing feedback store is unreadable: {exc}")
        if not isinstance(doc, dict):
            raise ValueError("existing feedback store is malformed")
        return doc

    def save_jev_feedback(self, ep_id, window_id, verdict, note) -> dict:
        """Validate against the fixed run and persist one verdict atomically,
        preserving all other entries. Raises ValueError on any bad input."""
        for name, value in (("episode_id", ep_id), ("window_id", window_id),
                            ("verdict", verdict)):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        if note is None:
            note = ""
        if not isinstance(note, str):
            raise ValueError("note must be a string")
        if verdict not in JEV_VERDICTS:
            raise ValueError("verdict must be one of correct, wrong, unsure")
        if not self.valid_episode(ep_id):
            raise ValueError("unknown episode")
        run = load_json(self.repo / JEV_RUN_PATH, default={}) or {}
        found = None
        for ep in run.get("episodes", []) or []:
            if isinstance(ep, dict) and (ep.get("episode_id") or ep.get("ep_id")) == ep_id:
                answers = ep.get("answers", {})
                if isinstance(answers, dict):
                    found = answers.get(window_id)
                break
        if not isinstance(found, dict):
            raise ValueError("unknown episode/window in the fixed Jev run")
        try:
            noul = float(found.get("noul", 0))
        except (TypeError, ValueError):
            raise ValueError("window has no valid score")
        if noul < JEV_THRESHOLD:
            raise ValueError("window is below the queue threshold")
        note = note[:JEV_NOTE_CHARS]
        run = load_json(self.repo / JEV_RUN_PATH, default={}) or {}
        models = run.get("model_returned") or []
        with self._feedback_lock:
            doc = self.jev_feedback_doc()
            entries = doc.get("entries", {})
            if not isinstance(entries, dict):
                raise ValueError("existing feedback entries are malformed")
            entries[f"{ep_id}/{window_id}"] = {
                "episode_id": ep_id,
                "window_id": window_id,
                "verdict": verdict,
                "note": note,
                "updated_at": utcnow(),
            }
            doc.update({
                "run_id": run.get("run_id"),
                "model": models[0] if models else run.get("model_requested"),
                "threshold": JEV_THRESHOLD,
                "provenance": ("Human triage verdicts recorded after seeing Jev model "
                               "output. NOT independent blind labels; never part of "
                               "owner-label ground truth or export."),
                "entries": entries,
            })
            atomic_write(self.jev_feedback_path(), doc)
            return self.jev_feedback()

    def detector_cues(self, ep_id: str) -> list:
        """Small sanitized cue list. Spoken and URL attribution stay separate."""
        cues = []
        for c in self.normalized_candidates():
            if not isinstance(c, dict) or c.get("ep_id") != ep_id:
                continue
            cues.append({
                "candidate_id": c.get("candidate_id"),
                "start_s": c.get("start_s"),
                "end_s": c.get("end_s"),
                "brand_spoken": c.get("brand_spoken"),
                "brand_url": c.get("brand_url"),
                "urls": c.get("urls") or [],
                "offer_codes": c.get("offer_codes") or [],
                "anchor": c.get("anchor"),
                "confidence": c.get("confidence"),
            })
        return cues

    # -- owner labels ---------------------------------------------------
    def owner_path(self, ep_id: str) -> Path:
        return self.storage / (ep_id + ".json")

    def owner_label(self, ep_id: str):
        return load_json(self.owner_path(ep_id))

    def owner_ids(self) -> list[str]:
        return sorted(
            p.stem for p in self.storage.glob("*.json")
            if p.is_file() and p.name != "manifest.json" and EP_ID_RE.match(p.stem)
        )

    def manifest(self) -> dict:
        m = load_json(self.manifest_path)
        if not isinstance(m, dict):
            m = {}
        m.setdefault("wave_id", WAVE_ID)
        m.setdefault("protocol_version", PROTOCOL_VERSION)
        m.setdefault("updated_at", utcnow())
        m.setdefault("episodes", {})
        return m

    def write_manifest(self, manifest: dict) -> None:
        manifest["updated_at"] = utcnow()
        atomic_write(self.manifest_path, manifest)

    # -- bootstrap ------------------------------------------------------
    def bootstrap(self) -> dict:
        tids = self.transcript_ids()
        meta = self.episodes_meta()
        sample = self.sample()
        sample_eps: dict = {}
        for e in sample.get("episodes", []) if isinstance(sample, dict) else []:
            if isinstance(e, dict) and e.get("ep_id"):
                sample_eps[e["ep_id"]] = e
        selected = [e for e in sample.get("episodes", []) if isinstance(e, dict)]
        selected_ids = [e["ep_id"] for e in selected if e.get("ep_id")]
        out_of_sample = sample.get("not_selected_currently_unlabeled", []) or []
        accepted = set(self.accepted_ids())
        owners = {ep: self.owner_label(ep) for ep in self.owner_ids()}
        examples = self.audio_examples()
        audio_by_ep = {tid: self.episode_audio(tid) for tid in tids}

        episodes = []
        for tid in tids:
            t = self.load_transcript(tid) or {}
            segs = t.get("segments", []) if isinstance(t, dict) else []
            m = meta.get(tid, {})
            s = sample_eps.get(tid, {})
            acc = self.accepted_label(tid) if tid in accepted else None
            own = owners.get(tid)
            episodes.append({
                "ep_id": tid,
                "show_title": m.get("show_title") or (t.get("show") if isinstance(t, dict) else None),
                "ep_title": m.get("ep_title") or (t.get("ep_title") if isinstance(t, dict) else None),
                "category": m.get("category") or s.get("category") or (acc.get("category") if isinstance(acc, dict) else None),
                "fetch_date": m.get("fetch_date") or (acc.get("fetch_date") if isinstance(acc, dict) else None),
                "duration_min": (t.get("duration_min") if isinstance(t, dict) else None) or s.get("duration_min"),
                "segment_count": len(segs) if isinstance(segs, list) else 0,
                "selected": tid in sample_eps,
                "stratum": s.get("stratum"),
                "accepted_locked": tid in accepted,
                "audio": audio_by_ep.get(tid),
                "has_owner_label": own is not None,
                "owner_status": ((own.get("review") or {}).get("status") if isinstance(own, dict) else None),
                "owner_updated_at": ((own.get("review") or {}).get("updated_at") if isinstance(own, dict) else None),
            })
        return {
            "wave_id": WAVE_ID,
            "protocol_version": PROTOCOL_VERSION,
            "protocol_path": "docs/LABELING-PROTOCOL.md",
            "protocol_reminder": (
                "Read the FULL transcript before labels are compared to detector output. "
                "This tool never shows detector candidates."
            ),
            "selected_ids": selected_ids,
            "out_of_sample_ids": [i for i in out_of_sample if i in set(tids)],
            "episodes": episodes,
            "practice_examples": examples,
            "manifest": self.manifest(),
            "generated_at": utcnow(),
        }


def atomic_write(path: Path, payload: dict) -> None:
    """Write JSON atomically via temp-file + os.replace; keep one .bak."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    if path.is_file():
        bak = path.with_suffix(path.suffix + ".bak")
        try:
            with open(path, "rb") as src, open(bak, "wb") as dst:
                dst.write(src.read())
        except OSError:
            pass  # backup is best-effort; the atomic replace still protects data
    os.replace(tmp, path)


def normalize_annotation(raw: dict, kind: str, seg_count: int) -> dict:
    """Validate one annotation; raise ValueError on contract violations."""
    if not isinstance(raw, dict):
        raise ValueError(f"{kind} entries must be objects")
    seg_idx = raw.get("seg_idx", [])
    if not isinstance(seg_idx, list) or not seg_idx or not all(isinstance(i, int) and 0 <= i < seg_count for i in seg_idx):
        raise ValueError(f"{kind} seg_idx must be a non-empty list of valid segment indexes")
    seg_idx = sorted(set(seg_idx))
    start_s = raw.get("start_s")
    end_s = raw.get("end_s")
    if not isinstance(start_s, (int, float)) or not isinstance(end_s, (int, float)) or end_s < start_s:
        raise ValueError(f"{kind} requires numeric start_s/end_s with end_s >= start_s")
    audio = raw.get("audio_profile", "not_reviewed")
    if audio not in AUDIO_PROFILES:
        raise ValueError(f"{kind} audio_profile must be one of {sorted(AUDIO_PROFILES)}")
    text_snapshot = raw.get("text_snapshot", "")
    if not isinstance(text_snapshot, str) or not text_snapshot.strip():
        raise ValueError(f"{kind} requires a non-empty text_snapshot evidence string")
    codes = raw.get("codes", []) or []
    urls = raw.get("urls", []) or []
    if not isinstance(codes, list) or not isinstance(urls, list):
        raise ValueError(f"{kind} codes/urls must be lists")
    out = {
        "seg_idx": seg_idx,
        "start_s": start_s,
        "end_s": end_s,
        "text_snapshot": text_snapshot,
        "codes": [str(c) for c in codes],
        "urls": [str(u) for u in urls],
        "audio_profile": audio,
        "audio_notes": str(raw.get("audio_notes", "") or ""),
        "uncertain": bool(raw.get("uncertain", False)),
    }
    if kind == "read":
        brand = str(raw.get("brand", "") or raw.get("company", "") or "").strip().lower()
        read_type = raw.get("read_type", "uncertain")
        if read_type not in READ_TYPES:
            raise ValueError(f"read read_type must be one of {sorted(READ_TYPES)}")
        # Company is optional only for uncertain/unresolved annotations.
        if not brand and not (read_type == "uncertain" or out["uncertain"]):
            raise ValueError("read brand/company is required unless read_type is uncertain or uncertain=true (do not invent brands)")
        out.update({
            "brand": brand,
            "read_type": read_type,
            "evidence": str(raw.get("evidence", "") or raw.get("claims", "") or ""),
            "context_note": str(raw.get("context_note", "") or ""),
        })
    else:
        brand = str(raw.get("brand", "") or "").strip().lower()
        if not brand and not out["uncertain"]:
            raise ValueError("organic mention brand is required unless uncertain=true")
        out.update({
            "brand": brand,
            "context": str(raw.get("context", "") or raw.get("context_note", "") or ""),
        })
    return out


def build_owner_label(store: Store, ep_id: str, body: dict) -> dict:
    t = store.load_transcript(ep_id)
    if not isinstance(t, dict) or not isinstance(t.get("segments"), list):
        raise ValueError("unknown or malformed transcript")
    segs = t["segments"]
    label = body.get("label", body)
    if not isinstance(label, dict):
        raise ValueError("label must be an object")
    reads = [normalize_annotation(r, "read", len(segs)) for r in label.get("reads", []) or []]
    organics = [normalize_annotation(o, "organic", len(segs)) for o in label.get("organic_mentions", []) or []]
    unresolved = label.get("unresolved", []) or []
    if not isinstance(unresolved, list):
        raise ValueError("unresolved must be a list")

    meta = store.episodes_meta().get(ep_id, {})
    sample_eps = {e.get("ep_id"): e for e in (store.sample().get("episodes", []) or []) if isinstance(e, dict)}
    prev = store.owner_label(ep_id) or {}
    prev_review = prev.get("review", {}) if isinstance(prev, dict) else {}
    status = (label.get("review") or {}).get("status", prev_review.get("status", "draft")) if isinstance(label.get("review"), dict) else prev_review.get("status", "draft")
    if status not in STATUSES:
        raise ValueError("review.status must be draft or complete")
    confirmed = bool((label.get("review") or {}).get("independent_read_confirmed", prev_review.get("independent_read_confirmed", False))) if isinstance(label.get("review"), dict) else bool(prev_review.get("independent_read_confirmed", False))
    if status == "complete" and not confirmed:
        raise ValueError("complete status requires independent_read_confirmed=true (full transcript read)")

    labeled_by = str(label.get("labeled_by") or prev.get("labeled_by") or "owner-session-human")
    today = datetime.date.today().isoformat()
    return {
        "episode_id": ep_id,
        "category": label.get("category") or meta.get("category") or sample_eps.get(ep_id, {}).get("category"),
        "show_title": label.get("show_title") or meta.get("show_title") or (t.get("show") if isinstance(t, dict) else None),
        "ep_title": label.get("ep_title") or meta.get("ep_title") or (t.get("ep_title") if isinstance(t, dict) else None),
        "fetch_date": label.get("fetch_date") or meta.get("fetch_date"),
        "label_provenance": label.get("label_provenance") or (
            "Independent owner transcript read (owner review UI). Full transcript read before any detector comparison; no detector candidates shown."
        ),
        "labeled_by": labeled_by,
        "labeled_date": label.get("labeled_date") or prev.get("labeled_date") or today,
        "notes": str(label.get("notes", "") or ""),
        "reads": reads,
        "organic_mentions": organics,
        "unresolved": unresolved,
        "review": {
            "wave_id": WAVE_ID,
            "protocol_version": PROTOCOL_VERSION,
            "status": status,
            "independent_read_confirmed": confirmed,
            "transcript_sha256": sha256_file(store.transcript_path(ep_id)),
            "segment_count": len(segs),
            "updated_at": utcnow(),
            "acoustic_note": "Acoustic observations are manual listener notes. Audio playback is available when a source exists, but notes are not transcript proof.",
        },
    }


class Handler(BaseHTTPRequestHandler):
    store: Store
    review_dir: Path
    server_version = "EarWitnessReview/1.0"

    def log_message(self, fmt, *args):  # quieter stderr logging
        sys.stderr.write("review_server: " + fmt % args + "\n")

    # -- helpers ------------------------------------------------------
    def send_json(self, payload, status=HTTPStatus.OK, download_name: str | None = None):
        body = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: HTTPStatus, message: str):
        self.send_json({"ok": False, "error": message}, status=status)

    def read_json_body(self, limit_bytes: int = 2_000_000):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > limit_bytes:
            raise ValueError("missing or oversized request body")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    # -- routing ------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        try:
            if path in ("/review", "/review/"):
                return self.serve_static("index.html", "text/html; charset=utf-8")
            if path.startswith("/review/"):
                name = path[len("/review/"):]
                if name in ("app.js", "jev.js", "jev.html", "styles.css"):
                    if name.endswith(".js"):
                        ctype = "text/javascript; charset=utf-8"
                    elif name.endswith(".html"):
                        ctype = "text/html; charset=utf-8"
                    else:
                        ctype = "text/css; charset=utf-8"
                    return self.serve_static(name, ctype)
                return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
            if path == "/api/bootstrap":
                return self.send_json({"ok": True, **self.store.bootstrap()})
            if path == "/api/transcript":
                return self.handle_transcript(qs)
            if path == "/api/owner-label":
                return self.handle_owner_label(qs)
            if path == "/api/accepted-label":
                return self.handle_accepted_label(qs)
            if path == "/api/export":
                return self.handle_export()
            if path == "/api/episode-export":
                return self.handle_episode_export(qs)
            if path == "/api/detector-cues":
                return self.handle_detector_cues(qs)
            if path == "/api/jev-queue":
                return self.send_json({"ok": True, **self.store.jev_queue()})
            if path == "/api/jev-feedback":
                return self.handle_jev_feedback_get()
            if path.startswith("/api/clip/"):
                return self.handle_clip(path[len("/api/clip/"):])
            return self.send_error_json(HTTPStatus.NOT_FOUND, "unknown endpoint")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/save":
                return self.handle_save()
            if parsed.path == "/api/complete":
                return self.handle_complete()
            if parsed.path == "/api/jev-feedback":
                return self.handle_jev_feedback()
            return self.send_error_json(HTTPStatus.NOT_FOUND, "unknown endpoint")
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- handlers -----------------------------------------------------
    def serve_static(self, name: str, ctype: str):
        target = (self.review_dir / name).resolve()
        if self.review_dir not in target.parents and target != self.review_dir:
            return self.send_error_json(HTTPStatus.FORBIDDEN, "forbidden")
        try:
            data = target.read_bytes()
        except FileNotFoundError:
            return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def require_episode(self, qs) -> str | None:
        ep_id = (qs.get("ep_id") or qs.get("ep") or [""])[0]
        if not self.store.valid_episode(ep_id):
            self.send_error_json(HTTPStatus.BAD_REQUEST, "unknown or invalid ep_id")
            return None
        return ep_id

    def handle_transcript(self, qs):
        ep_id = self.require_episode(qs)
        if ep_id is None:
            return
        t = self.store.load_transcript(ep_id)
        if not isinstance(t, dict):
            return self.send_error_json(HTTPStatus.NOT_FOUND, "transcript not found")
        self.send_json({"ok": True, "ep_id": ep_id, "transcript": t,
                        "transcript_sha256": sha256_file(self.store.transcript_path(ep_id))})

    def handle_owner_label(self, qs):
        ep_id = self.require_episode(qs)
        if ep_id is None:
            return
        self.send_json({"ok": True, "ep_id": ep_id, "label": self.store.owner_label(ep_id)})

    def handle_accepted_label(self, qs):
        ep_id = self.require_episode(qs)
        if ep_id is None:
            return
        label = self.store.accepted_label(ep_id)
        if label is None:
            return self.send_json({"ok": True, "ep_id": ep_id, "label": None})
        self.send_json({"ok": True, "ep_id": ep_id, "label": label, "read_only": True})

    def handle_save(self):
        try:
            body = self.read_json_body()
        except ValueError as exc:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "invalid JSON body")
        ep_id = body.get("episode_id") or body.get("ep_id")
        if not self.store.valid_episode(ep_id or ""):
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "unknown or invalid episode_id")
        try:
            label = build_owner_label(self.store, ep_id, body)
        except ValueError as exc:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        try:
            atomic_write(self.store.owner_path(ep_id), label)
        except OSError as exc:
            return self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"persist failed: {exc}")
        manifest = self.store.manifest()
        manifest["episodes"][ep_id] = {
            "status": label["review"]["status"],
            "reads": len(label["reads"]),
            "organic_mentions": len(label["organic_mentions"]),
            "unresolved": len(label["unresolved"]),
            "transcript_sha256": label["review"]["transcript_sha256"],
            "updated_at": label["review"]["updated_at"],
        }
        manifest.pop("last_error", None)
        try:
            self.store.write_manifest(manifest)
        except OSError as exc:
            return self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"manifest update failed: {exc}")
        self.send_json({"ok": True, "ep_id": ep_id, "label": label,
                        "updated_at": label["review"]["updated_at"]})

    def handle_complete(self):
        try:
            body = self.read_json_body()
        except ValueError as exc:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "invalid JSON body")
        ep_id = body.get("episode_id") or body.get("ep_id")
        if not self.store.valid_episode(ep_id or ""):
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "unknown or invalid episode_id")
        if body.get("affirmation") is not True and body.get("independent_read_confirmed") is not True:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "completion requires affirmation=true that the full transcript was read")
        base = self.store.owner_label(ep_id) or {}
        merged = dict(base)
        merged["review"] = {**(base.get("review") if isinstance(base.get("review"), dict) else {}),
                            "status": "complete", "independent_read_confirmed": True}
        for key in ("labeled_by", "notes", "category", "fetch_date"):
            if body.get(key) is not None:
                merged[key] = body[key]
        try:
            label = build_owner_label(self.store, ep_id, {"episode_id": ep_id, "label": merged, **merged})
        except ValueError as exc:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        try:
            atomic_write(self.store.owner_path(ep_id), label)
        except OSError as exc:
            return self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"persist failed: {exc}")
        manifest = self.store.manifest()
        manifest["episodes"][ep_id] = {
            "status": "complete",
            "reads": len(label["reads"]),
            "organic_mentions": len(label["organic_mentions"]),
            "unresolved": len(label["unresolved"]),
            "transcript_sha256": label["review"]["transcript_sha256"],
            "updated_at": label["review"]["updated_at"],
        }
        try:
            self.store.write_manifest(manifest)
        except OSError as exc:
            return self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"manifest update failed: {exc}")
        self.send_json({"ok": True, "ep_id": ep_id, "label": label})

    def handle_export(self):
        manifest = self.store.manifest()
        labels = {ep: self.store.owner_label(ep) for ep in self.store.owner_ids()}
        self.send_json({
            "ok": True,
            "wave_id": WAVE_ID,
            "protocol_version": PROTOCOL_VERSION,
            "exported_at": utcnow(),
            "manifest": manifest,
            "labels": labels,
        }, download_name=f"{WAVE_ID}-owner-labels.json")

    def handle_episode_export(self, qs):
        ep_id = self.require_episode(qs)
        if ep_id is None:
            return
        self.send_json({"ok": True, "ep_id": ep_id, "label": self.store.owner_label(ep_id)},
                       download_name=f"{ep_id}-owner-label.json")

    def handle_jev_feedback_get(self):
        try:
            doc = self.store.jev_feedback()
        except ValueError as exc:
            return self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
        self.send_json({"ok": True, **doc})

    def handle_jev_feedback(self):
        try:
            body = self.read_json_body()
        except ValueError as exc:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "invalid JSON body")
        if not isinstance(body, dict):
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "invalid feedback body")
        try:
            doc = self.store.save_jev_feedback(
                body.get("episode_id"), body.get("window_id"),
                body.get("verdict"), body.get("note", ""))
        except ValueError as exc:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except OSError as exc:
            return self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"persist failed: {exc}")
        self.send_json({"ok": True, **doc})

    def handle_detector_cues(self, qs):
        ep_id = self.require_episode(qs)
        if ep_id is None:
            return
        if not self.store.cues_allowed(ep_id):
            return self.send_error_json(
                HTTPStatus.CONFLICT,
                "detector cues unlock only after independent labeling is locked "
                "(complete status with full-transcript affirmation) or an accepted "
                "label exists; no cues returned")
        self.send_json({"ok": True, "ep_id": ep_id,
                        "cues": self.store.detector_cues(ep_id)})

    def handle_clip(self, name: str):
        """Serve one allowlisted local practice clip with byte-range support.
        The name must match CLIP_ALLOWLIST exactly; anything else is 404.
        Never a filesystem browser, never a URL proxy."""
        rel = CLIP_ALLOWLIST.get(name)
        if rel is None or "/" in name or "\\" in name:
            return self.send_error_json(HTTPStatus.NOT_FOUND, "unknown clip")
        target = (self.store.repo / rel).resolve()
        if self.store.repo not in target.parents:
            return self.send_error_json(HTTPStatus.NOT_FOUND, "unknown clip")
        try:
            size = target.stat().st_size
        except OSError:
            return self.send_error_json(HTTPStatus.NOT_FOUND, "clip unavailable")
        start, end = 0, size - 1
        status = HTTPStatus.OK
        range_hdr = (self.headers.get("Range") or "").strip()
        if range_hdr:
            m = re.match(r"bytes=(\d*)-(\d*)$", range_hdr)
            if not m or (not m.group(1) and not m.group(2)):
                return self.send_error_json(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                                            "invalid Range")
            if not m.group(1):
                start, end = max(0, size - int(m.group(2))), size - 1
            else:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            if start >= size or end < start:
                return self.send_error_json(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                                            "range out of bounds")
            end = min(end, size - 1)
            status = HTTPStatus.PARTIAL_CONTENT
        try:
            with open(target, "rb") as fh:
                fh.seek(start)
                data = fh.read(end - start + 1)
        except OSError:
            return self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "clip read failed")
        self.send_response(status)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(data)))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        self.wfile.write(data)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="EarWitness owner-review server: serves review/ UI and same-origin "
                    "labeling API on localhost. Stdlib only; makes no network calls.",
        epilog="Example: python3 scripts/review_server.py --port 8765",
    )
    p.add_argument("--host", default="127.0.0.1",
                   help="Bind address (default 127.0.0.1; keep loopback for local-only use).")
    p.add_argument("--port", type=int, default=8765, help="Bind port (default 8765).")
    p.add_argument("--repo-root", default=str(REPO_ROOT),
                   help="Repository root (default: parent of scripts/).")
    p.add_argument("--storage-dir", default=f"labels/{WAVE_ID}",
                   help="Owner-label storage dir relative to repo root "
                        f"(default labels/{WAVE_ID}). Test runs may point this at a "
                        "disposable directory; canonical data stays in labels/.")
    return p


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.host not in LOCAL_HOSTS:
        print("error: --host must be a loopback address (127.0.0.1, localhost, or ::1)", file=sys.stderr)
        return 2
    repo_root = Path(args.repo_root).resolve()
    if not (repo_root / "transcripts").is_dir():
        print(f"error: {repo_root} does not look like the earwitness-validation repo "
              "(missing transcripts/)", file=sys.stderr)
        return 2
    try:
        store = Store(repo_root, args.storage_dir)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    Handler.store = store
    Handler.review_dir = (repo_root / "review").resolve()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"EarWitness review UI: http://{args.host}:{server.server_port}/review/")
    print(f"Repo: {repo_root}  storage: {store.storage}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
