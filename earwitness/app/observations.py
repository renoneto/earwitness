"""P05 complete-ad observations and attribution (review-safe, lineage-preserving).

Owned by P05P06. Uses the frozen P00 schema (ad_observations,
observation_revisions, observation_attributions, evidence projections) without
modifying db.py. Machine output creates ``proposed`` only; human review writes
``reviewed_ad`` / ``reviewed_not_ad`` / ``unsure`` through distinct calls.
Split/merge are transactional over pinned parents with revision checks and
deliberate reassessment of downstream relations.

No new dependencies. Read paths distinguish candidates from observations and
original from effective text. Service plumbing here returns plain data for
server routes; no DOM changes (full review UI is P09/P10).
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid

from . import citations as _citations
from . import db as _db
from . import ops as _ops

OBS_STATUSES = ("proposed", "reviewed_ad", "reviewed_not_ad", "unsure", "superseded")
HUMAN_JUDGMENTS = ("reviewed_ad", "reviewed_not_ad", "unsure")
READ_TYPES = ("host_read", "produced", "unknown")
ATTR_CHANNELS = ("spoken", "url", "code", "unspecified")
ATTR_STATUSES = ("suggested", "confirmed", "rejected")
MACHINE_ACTORS = ("pipeline", "machine", "agent", "classifier")

COMPARISON_CUES = (
    "compare", "comparison", "versus", " vs ", "co-brand", "cobrand",
    "both", "also brings", "compared",
)

_URL_RE = re.compile(r"(?:[\w-]+\.)+(?:com|net|org|io|co|fm)(?:/[\w\-%.~]*)?", re.I)
_CODE_RE = re.compile(r"\b[A-Z0-9]{3,20}\b")
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?%?\b")


def _now() -> str:
    return _db.utcnow()


def _new_id(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4().hex[:12]}"


def _is_machine(actor: str) -> bool:
    return str(actor or "").strip().lower() in MACHINE_ACTORS


def _attribution_status(a: dict, machine: bool) -> str:
    """Validated status for a fresh attribution; machine-created ones stay suggested."""
    st = a.get("status", "suggested")
    if st not in ATTR_STATUSES:
        raise _ops.BadInput("attribution status must be suggested, confirmed, or rejected")
    if machine and st != "suggested":
        raise _ops.BadInput("machine-created attributions must be suggested")
    return st


def _require_member(ctx, operation: str) -> None:
    if ctx is not None:
        ctx.require_member(operation)
        from .access import require_customer_mutation
        require_customer_mutation(ctx, operation)


def _finite(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _ops.BadInput(f"{name} must be a finite number")
    import math
    if not math.isfinite(value):
        raise _ops.BadInput(f"{name} must be a finite number")
    return float(value)


def _segments_of(con: sqlite3.Connection, source_version_id) -> list:
    """Pinned source segments for validation (never synthesized)."""
    if source_version_id is None:
        return []
    row = con.execute("SELECT payload FROM source_versions WHERE id=?",
                      (source_version_id,)).fetchone()
    if row is None:
        return []
    try:
        payload = json.loads(row["payload"])
    except (json.JSONDecodeError, TypeError):
        return []
    segs = payload.get("segments", []) if isinstance(payload, dict) else []
    return segs if isinstance(segs, list) else []


def _fk_or_null(con: sqlite3.Connection, table: str, row_id) -> object:
    """FK-safe pin: keep the id only when the referenced row exists."""
    if row_id is None:
        return None
    try:
        rid = int(row_id)
    except (TypeError, ValueError):
        return None
    row = con.execute(f"SELECT id FROM {table} WHERE id=?", (rid,)).fetchone()
    return rid if row is not None else None


def _evidence_transcript_pin(con: sqlite3.Connection, episode_id) -> object:
    """FK-safe transcript pin for evidence projections.

    ``evidence.transcript_version_id`` references ``source_versions`` (the
    live transcript identity), so guard the pin against that table; a missing
    pin stays NULL exactly as before.
    """
    row = con.execute("SELECT transcript_version_id FROM episodes WHERE id=?",
                      (episode_id,)).fetchone()
    return _fk_or_null(con, "source_versions",
                       row["transcript_version_id"] if row else None)


def _episode_fetch(con: sqlite3.Connection, episode_id: str) -> tuple:
    row = con.execute("SELECT fetch_date FROM episodes WHERE id=?",
                      (episode_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown episode")
    return row["fetch_date"]


def inspect_commercial_continuity(window_texts: list, brands: list,
                                  anchor_kinds: list | None = None) -> dict:
    """Adjacent-window boundary proposal with commercial-continuity inspection.

    Distinct brands are a split CUE, not a law: comparative/co-branded copy
    mentioning multiple brands stays together with rationale. Anything else
    ambiguous returns ``uncertain`` — never an unconditional merge/split.
    """
    texts = [str(t or "") for t in (window_texts or [])]
    canon = [str(b or "").strip().lower() for b in (brands or []) if str(b or "").strip()]
    distinct = sorted(set(canon))
    joined = " ".join(texts).lower()
    cues = [c for c in COMPARISON_CUES if c in joined]
    kinds = [str(k or "") for k in (anchor_kinds or [])]
    marker_only = bool(kinds) and all(k == "marker_only" for k in kinds)
    if len(distinct) >= 2 and cues:
        return {"decision": "keep",
                "rationale": ("multiple brands with comparative/co-branded cues "
                              f"({', '.join(cues)}); kept together for review"),
                "distinct_brands": distinct}
    if len(distinct) >= 2:
        return {"decision": "split",
                "rationale": f"distinct brands {distinct} with no comparative cues; split cue",
                "distinct_brands": distinct}
    if marker_only:
        return {"decision": "uncertain",
                "rationale": "marker-only anchors without named brands; uncertain",
                "distinct_brands": distinct}
    if len(texts) >= 2 and not distinct:
        return {"decision": "uncertain",
                "rationale": "adjacent positive windows without brand separation; uncertain",
                "distinct_brands": distinct}
    return {"decision": "keep",
            "rationale": "single continuous commercial span",
            "distinct_brands": distinct}


def validate_field_refs(con: sqlite3.Connection, source_version_id,
                        fields: dict) -> None:
    """Deterministic validation of per-field segment/character references.

    Every proposed field with a segment ref must cite pinned text that exists;
    out-of-range refs and classifier proposals without cited text raise
    BadInput. No subsegment precision without word timestamps.
    """
    segs = _segments_of(con, source_version_id)
    if not isinstance(fields, dict):
        raise _ops.BadInput("fields must be an object")
    for name, spec in fields.items():
        if not isinstance(spec, dict):
            raise _ops.BadInput(f"field {name!r} must be an object")
        seg_ref = spec.get("segment_ref")
        char_ref = spec.get("char_ref")
        method = str(spec.get("method", ""))
        value = str(spec.get("value", ""))
        if seg_ref is None:
            if method.startswith("classifier") and value:
                raise _ops.BadInput(
                    f"classifier field {name!r} proposes without cited pinned text")
            continue
        if not isinstance(seg_ref, int) or not 0 <= seg_ref < len(segs):
            raise _ops.BadInput(f"field {name!r} segment_ref out of range")
        seg_text = str(segs[seg_ref].get("text", ""))
        if char_ref is not None:
            if (not isinstance(char_ref, (list, tuple)) or len(char_ref) != 2):
                raise _ops.BadInput(f"field {name!r} char_ref must be [start, end]")
            a, b = char_ref
            if not isinstance(a, int) or not isinstance(b, int) \
                    or not 0 <= a < b <= len(seg_text):
                raise _ops.BadInput(f"field {name!r} char_ref out of range")
            cited = seg_text[a:b]
            if value and value not in seg_text and value not in cited:
                raise _ops.BadInput(
                    f"field {name!r} value not found in cited pinned text")
        elif value and value not in seg_text:
            # Cited segment must contain the proposed literal (case-sensitive
            # pinned text; normalization is a suggestion, not evidence).
            if value.lower() not in seg_text.lower():
                raise _ops.BadInput(
                    f"field {name!r} value not found in cited pinned text")
        if spec.get("subsegment") and not spec.get("word_timestamps"):
            raise _ops.BadInput(
                f"field {name!r} claims subsegment precision without word timestamps")


def validate_bounds_against_segments(start_s: float, end_s: float,
                                     segments: list,
                                     word_timestamps: bool = False) -> dict:
    """Boundary check: segment-crossing bounds keep segment uncertainty.

    Subsegment precision is accepted only with word timestamps; otherwise
    bounds snap-report uncertainty instead of fabricating precision.
    """
    start_s = _finite(start_s, "start_s")
    end_s = _finite(end_s, "end_s")
    if end_s <= start_s:
        raise _ops.BadInput("require start_s < end_s")
    if not segments:
        return {"start_s": start_s, "end_s": end_s, "uncertain": True,
                "reason": "no pinned segments; segment-level uncertainty"}
    extent = max(float(s.get("end", 0) or 0) for s in segments)
    if start_s < 0 or end_s > extent + 1e-6:
        raise _ops.BadInput("bounds must lie within the pinned transcript")
    inner = False
    for s in segments:
        a, b = float(s.get("start", 0) or 0), float(s.get("end", 0) or 0)
        if a < start_s < b or a < end_s < b:
            inner = True
    if inner and not word_timestamps:
        return {"start_s": start_s, "end_s": end_s, "uncertain": True,
                "reason": "bound crosses a segment without word timestamps; "
                          "segment-level uncertainty retained"}
    return {"start_s": start_s, "end_s": end_s, "uncertain": False, "reason": ""}


def _check_idempotency(con: sqlite3.Connection, key: str, digest: str):
    if not key:
        return None
    row = con.execute("SELECT detail FROM audit WHERE action='observation_propose'"
                      " AND target_type='observation'"
                      " AND json_extract(detail, '$.idempotency_key')=?"
                      " AND json_extract(detail, '$.digest')=?"
                      " ORDER BY id DESC", (key, digest)).fetchall()
    for r in row:
        try:
            detail = json.loads(r["detail"])
        except (json.JSONDecodeError, TypeError):
            continue
        if detail.get("idempotency_key") == key and detail.get("digest") == digest:
            obs_id = detail.get("observation_id")
            if obs_id and con.execute("SELECT id FROM ad_observations WHERE id=?",
                                      (obs_id,)).fetchone():
                return obs_id
    return None


@_db.atomic
def propose_observation(con: sqlite3.Connection, episode_id: str,
                        start_s: float, end_s: float,
                        bounds_evidence: dict | None = None,
                        fields: dict | None = None,
                        attributions: list | None = None,
                        source_pins: dict | None = None,
                        effective_transcript_version_id=None,
                        idempotency_key: str = "",
                        actor: str = "pipeline",
                        status: str = "proposed",
                        ctx=None) -> dict:
    """Create a machine-proposed complete-ad observation (proposed ONLY).

    URL-only evidence yields URL associations with spoken unknown; a spoken
    brand without spoken-phrase evidence is rejected (no conflation).
    """
    _require_member(ctx, "save_observation")
    if status != "proposed":
        raise _ops.BadInput("machine output creates proposed observations only")
    _episode_fetch(con, episode_id)  # validate episode exists; raises BadInput
    fields = dict(fields or {})
    attributions = list(attributions or [])
    source_pins = dict(source_pins or {})
    bounds_evidence = dict(bounds_evidence or {})
    start_s = _finite(start_s, "start_s")
    end_s = _finite(end_s, "end_s")
    if end_s <= start_s:
        raise _ops.BadInput("require start_s < end_s")
    read_type = fields.get("read_type", {}).get("value", "unknown") \
        if isinstance(fields.get("read_type"), dict) else "unknown"
    if read_type not in READ_TYPES:
        raise _ops.BadInput("read_type must be host_read, produced, or unknown")
    source_version_id = source_pins.get("source_version_id")
    # Archive versions and pipeline transcript versions are separate namespaces.
    archive_transcript_id = source_pins.get("transcript_version_id")
    if archive_transcript_id is None and source_version_id is not None:
        source = con.execute("SELECT source_key FROM source_versions WHERE id=?",
                             (source_version_id,)).fetchone()
        if source is not None and source["source_key"] == f"transcript:{episode_id}":
            archive_transcript_id = source_version_id
    if archive_transcript_id is not None:
        source = con.execute("SELECT source_key FROM source_versions WHERE id=?",
                             (archive_transcript_id,)).fetchone()
        if source is None or source["source_key"] != f"transcript:{episode_id}":
            raise _ops.BadInput("archived transcript does not belong to the observation episode")
    pipeline_transcript_id = source_pins.get("pipeline_transcript_version_id")
    if pipeline_transcript_id is not None:
        pipeline = con.execute("SELECT episode_id FROM transcript_versions WHERE id=?",
                               (pipeline_transcript_id,)).fetchone()
        if pipeline is None or pipeline["episode_id"] != episode_id:
            raise _ops.BadInput("pipeline transcript does not belong to the observation episode")
    source_pins["transcript_version_id"] = archive_transcript_id
    source_pins["pipeline_transcript_version_id"] = pipeline_transcript_id
    segments = _segments_of(con, source_version_id)
    seg_list = bounds_evidence.get("segments", segments)
    bounds = validate_bounds_against_segments(
        start_s, end_s, seg_list,
        word_timestamps=bool(bounds_evidence.get("word_timestamps")))
    validate_field_refs(con, source_version_id, fields)
    # Channel separation: spoken claims need spoken evidence.
    spoken = [a for a in attributions if a.get("channel") == "spoken"
              and str(a.get("entity", "")).strip()]
    for a in attributions:
        _attribution_status(a, _is_machine(actor))
        ch = a.get("channel", "unspecified")
        if ch not in ATTR_CHANNELS:
            raise _ops.BadInput("attribution channel must be spoken, url, code, or unspecified")
        if a.get("segment_ref") is not None:
            ref = a["segment_ref"]
            if not isinstance(ref, int) or not 0 <= ref < len(segments):
                raise _ops.BadInput("attribution segment_ref out of range")
    if spoken:
        sb = fields.get("spoken_brand")
        if not isinstance(sb, dict) or not str(sb.get("value", "")).strip() \
                or str(sb.get("value", "")).strip().lower() == "unknown":
            raise _ops.BadInput("spoken attribution requires a spoken_brand field with"
                                " cited pinned text; URL evidence alone is not spoken"
                                " evidence (use channel=url with spoken unknown)")
    # URL-only Kachava pattern: spoken stays unknown, URL association kept.
    url_attribs = [a for a in attributions if a.get("channel") == "url"]
    if url_attribs and not spoken:
        fields.setdefault("spoken_brand",
                          {"value": "unknown", "method": "deterministic",
                           "segment_ref": None, "char_ref": None})
    digest_src = json.dumps({"ep": episode_id, "s": start_s, "e": end_s,
                             "fields": fields, "attrs": attributions,
                             "pins": source_pins}, sort_keys=True)
    digest = hashlib.sha256(digest_src.encode()).hexdigest()[:16]
    existing = _check_idempotency(con, idempotency_key, digest)
    if existing:
        row = con.execute("SELECT id, revision, status FROM ad_observations WHERE id=?",
                          (existing,)).fetchone()
        return {"observation_id": existing, "revision": row["revision"],
                "status": row["status"], "duplicate": True,
                "aggregate_rev": _db.aggregate_revision(con)}
    fetch_date = source_pins.get("fetch_date")
    if fetch_date is None:
        fetch_date = _episode_fetch(con, episode_id)
    fetch_src = source_pins.get("fetch_date_source", "episode")
    obs_id = _new_id("obs")
    now = _now()
    transcript_version_id = pipeline_transcript_id
    media_artifact_id = _fk_or_null(con, "media_artifacts",
                                    source_pins.get("media_artifact_id"))
    if source_version_id is not None:
        source_version_id = _fk_or_null(con, "source_versions", source_version_id)
    with _db.transaction(con, write=True):
        con.execute("INSERT INTO ad_observations (id, workspace_id, episode_id,"
                    " media_artifact_id, transcript_version_id, source_version_id,"
                    " status, start_s, end_s, revision, created_at, updated_at)"
                    " VALUES (?, 'local', ?, ?, ?, ?, 'proposed', ?, ?, 1, ?, ?)",
                    (obs_id, episode_id, media_artifact_id, transcript_version_id,
                     source_version_id, bounds["start_s"], bounds["end_s"], now, now))
        values = {"start_s": bounds["start_s"], "end_s": bounds["end_s"],
                  "original_bounds_s": bounds_evidence.get("original_bounds_s",
                                                            [bounds["start_s"], bounds["end_s"]]),
                  "effective_bounds_s": [bounds["start_s"], bounds["end_s"]],
                  "bounds_uncertain": bounds["uncertain"],
                  "bounds_reason": bounds["reason"],
                  "fields": fields, "status": "proposed",
                  "effective_transcript_version_id": effective_transcript_version_id,
                  "fetch_date": fetch_date, "fetch_date_source": fetch_src,
                  "lineage": {"operation": "propose", "parents": []}}
        pins = dict(source_pins)
        pins["fetch_date"] = fetch_date
        pins["fetch_date_source"] = fetch_src
        if idempotency_key:
            pins["idempotency_key"] = idempotency_key
        cur = con.execute("INSERT INTO observation_revisions (workspace_id, observation_id,"
                          " revision, values_json, source_pins, actor, created_at)"
                          " VALUES ('local', ?, 1, ?, ?, ?, ?)",
                          (obs_id, json.dumps(values), json.dumps(pins), actor, now))
        rev_id = cur.lastrowid
        for a in attributions:
            st = _attribution_status(a, _is_machine(actor))
            con.execute("INSERT INTO observation_attributions (workspace_id, observation_id,"
                        " entity, raw_phrase, channel, method, status, created_at)"
                        " VALUES ('local', ?, ?, ?, ?, ?, ?, ?)",
                        (obs_id, str(a.get("entity", ""))[:200],
                         str(a.get("raw_phrase", ""))[:500],
                         a.get("channel", "unspecified"),
                         str(a.get("method", ""))[:120], st, now))
        ev_id = f"pipe:{obs_id}:r1"
        evidence_tv = archive_transcript_id
        con.execute("INSERT INTO evidence (id, source, source_version_id, episode_id,"
                    " orig_start_s, orig_end_s, excerpt, attrs, revision, created_at,"
                    " transcript_version_id, record_kind, observation_revision_id)"
                    " VALUES (?, 'pipeline', ?, ?, ?, ?, ?, ?, 1, ?, ?, 'observation', ?)",
                    (ev_id, source_version_id, episode_id, bounds["start_s"],
                     bounds["end_s"], str(bounds_evidence.get("excerpt", ""))[:600],
                     json.dumps({"observation_id": obs_id}), now, evidence_tv, rev_id))
        rev = _db.bump_revision(con)
        _db.audit(con, actor, "observation_propose", "observation", obs_id,
                  {"revision": 1, "digest": digest,
                   "idempotency_key": idempotency_key or None,
                   "observation_id": obs_id}, rev)
        from . import coverage as _coverage
        _pub_pin = (f"source-version:{source_version_id}"
                    if source_version_id is not None
                    else (f"source-version:{evidence_tv}"
                          if evidence_tv is not None else ""))
        if _pub_pin:
            _coverage.record_attestation(
                con, episode_id, "observation_publication",
                (float(bounds["start_s"]), float(bounds["end_s"])),
                (float(bounds["start_s"]), float(bounds["end_s"])),
                source_pin=_pub_pin, producer=f"observation:{obs_id}",
                producer_rev=1, actor=actor)
        if _db.has_v8_tables(con):
            from . import citations
            citations.publish_source_state(con, kind="evidence", record_id=ev_id, actor=actor)
    return {"observation_id": obs_id, "revision": 1, "status": "proposed",
            "observation_revision_id": rev_id, "evidence_id": ev_id,
            "aggregate_rev": rev, "bounds_uncertain": bounds["uncertain"]}


@_db.atomic
def review_observation(con: sqlite3.Connection, observation_id: str,
                       judgment: str, expected_revision,
                       actor: str = "owner", notes: str = "",
                       ctx=None) -> dict:
    """Human review of an observation. Machine actors are rejected."""
    _require_member(ctx, "save_observation")
    if expected_revision is None:
        raise _ops.MissingPrecondition("expected revision is required")
    if judgment not in HUMAN_JUDGMENTS:
        raise _ops.BadInput("judgment must be reviewed_ad, reviewed_not_ad, or unsure")
    if _is_machine(actor):
        raise _ops.BadInput("machine actors cannot write human review judgments")
    row = con.execute("SELECT revision, status, episode_id FROM ad_observations WHERE id=?",
                      (observation_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown observation")
    if int(expected_revision) != row["revision"]:
        raise _ops.Conflict(row["revision"])
    with _db.transaction(con, write=True):
        prev = con.execute("SELECT values_json, source_pins FROM observation_revisions"
                           " WHERE observation_id=? AND revision=?",
                           (observation_id, row["revision"])).fetchone()
        values = json.loads(prev["values_json"]) if prev else {}
        values = dict(values)
        values["status"] = judgment
        values["review"] = {"judgment": judgment, "notes": str(notes)[:1000],
                            "actor": actor}
        new_rev = row["revision"] + 1
        con.execute("INSERT INTO observation_revisions (workspace_id, observation_id,"
                    " revision, values_json, source_pins, actor, created_at)"
                    " VALUES ('local', ?, ?, ?, ?, ?, ?)",
                    (observation_id, new_rev, json.dumps(values),
                     prev["source_pins"] if prev else "{}", actor, _now()))
        con.execute("UPDATE ad_observations SET status=?, revision=?, updated_at=?"
                    " WHERE id=?", (judgment, new_rev, _now(), observation_id))
        _publish_observation_sources(con, observation_id, actor)
        rev = _db.bump_revision(con)
        _db.audit(con, actor, "observation_review", "observation", observation_id,
                  {"judgment": judgment, "revision": new_rev}, rev)
    return {"observation_id": observation_id, "revision": new_rev,
            "status": judgment, "aggregate_rev": rev}


def _publish_observation_sources(con, observation_id, actor):
    """Publish every exact evidence projection after operator history changes."""
    if not _db.has_v8_tables(con):
        return
    from . import citations
    for row in con.execute("SELECT id,attrs FROM evidence").fetchall():
        if json.loads(row["attrs"] or "{}").get("observation_id") == observation_id:
            citations.publish_source_state(con, kind="evidence", record_id=row["id"], actor=actor)


CONFIRMABLE_FIELDS = ("read_type", "message", "offer", "code")


def ensure_confirmation_schema(con: sqlite3.Connection) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS field_confirmations ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " workspace_id TEXT NOT NULL DEFAULT 'local',"
        " observation_id TEXT NOT NULL REFERENCES ad_observations(id),"
        " field TEXT NOT NULL DEFAULT '',"
        " value TEXT NOT NULL DEFAULT '',"
        " cleared INTEGER NOT NULL DEFAULT 0,"
        " observation_revision INTEGER NOT NULL DEFAULT 0,"
        " source_version_id INTEGER,"
        " transcript_version_id INTEGER,"
        " segment_ref INTEGER,"
        " char_start INTEGER,"
        " char_end INTEGER,"
        " audio_ref TEXT NOT NULL DEFAULT '',"
        " actor TEXT NOT NULL DEFAULT '',"
        " superseded_by INTEGER,"
        " created_at TEXT NOT NULL)")


def _require_confirmation_schema(con):
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                   " AND name='field_confirmations'").fetchone() is None:
        raise _ops.BadInput("field confirmation schema requires explicit setup")


def _confirmation_pin(con: sqlite3.Connection, observation_id: str) -> dict:
    row = con.execute("SELECT revision, source_version_id, transcript_version_id,"
                      " status, superseded_by FROM ad_observations WHERE id=?",
                      (observation_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown observation")
    return dict(row)


def current_field_confirmations(con: sqlite3.Connection,
                                observation_id: str) -> dict:
    """Latest authoritative confirmation per field for one observation.

    A confirmation is current only while it is the newest record for its
    field (not superseded, not cleared), its bound source pins still
    match the observation's current pins, its principal is human, and
    its cited evidence is structurally valid against the currently
    bound source (an existing segment with in-range characters, or
    audio that still resolves to the observation's episode). Source
    replacement, observation supersession, a newer confirm/clear, a
    machine principal, or dangling evidence keeps history but loses
    authority. Split/merge children never inherit these rows.
    """
    _require_confirmation_schema(con)
    try:
        pin = _confirmation_pin(con, observation_id)
    except _ops.BadInput:
        return {}
    if pin["superseded_by"]:
        return {}
    out: dict = {}
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM field_confirmations WHERE workspace_id='local'"
        " AND observation_id=? ORDER BY id", (observation_id,)).fetchall()]
    latest: dict[str, dict] = {}
    for r in rows:
        if r["superseded_by"] is not None:
            continue
        latest[r["field"]] = r
    for field, r in latest.items():
        if r["cleared"]:
            continue
        if r["observation_revision"] is None:
            continue
        if (r["source_version_id"] != pin["source_version_id"]
                or r["transcript_version_id"] != pin["transcript_version_id"]):
            continue
        if _is_machine(r["actor"]):
            continue
        if not _confirmation_evidence_current(con, observation_id, r):
            continue
        out[field] = {"id": r["id"], "value": r["value"], "actor": r["actor"],
                      "reviewed_at": r["created_at"],
                      "observation_revision": r["observation_revision"],
                      "source_version_id": r["source_version_id"],
                      "transcript_version_id": r["transcript_version_id"],
                      "segment_ref": r["segment_ref"],
                      "char_start": r["char_start"], "char_end": r["char_end"],
                      "audio_ref": r["audio_ref"]}
    return out


def _episode_audio_locators(con: sqlite3.Connection,
                              episode_id: str) -> set:
    """Exact current media locators (audio URL and/or media ID) for an
    episode. Empty when the episode is unknown or carries no audio."""
    ep = con.execute("SELECT audio_url, media_id FROM episodes WHERE id=?",
                     (episode_id,)).fetchone()
    if ep is None:
        return set()
    return {str(v) for v in (ep[0], ep[1]) if str(v or "").strip()}


def _confirmation_evidence_current(con: sqlite3.Connection, observation_id: str,
                                   row: dict) -> bool:
    """Structural evidence validity against the currently bound source.

    A segment citation must name an existing segment (with in-range
    characters when given) of the bound source version; an audio
    citation must name the exact current episode media asset (audio
    URL or media ID), not merely note that some audio exists. A
    rotated or removed episode asset therefore ends audio authority
    without rewriting history. No literal-substring matching:
    copy-field authority comes from the human principal plus a
    structurally valid citation, never from whether the value text
    appears in the transcript (fields like read_type never appear
    literally).
    """
    seg = row.get("segment_ref")
    if seg is not None:
        try:
            seg_idx = int(seg)
        except (TypeError, ValueError):
            return False
        segs = _segments_of(con, row.get("source_version_id"))
        if not 0 <= seg_idx < len(segs):
            return False
        a, b = row.get("char_start"), row.get("char_end")
        if a is not None or b is not None:
            seg_obj = segs[seg_idx] if isinstance(segs[seg_idx], dict) else {}
            seg_text = str(seg_obj.get("text", ""))
            if not (isinstance(a, int) and isinstance(b, int)
                    and 0 <= a < b <= len(seg_text)):
                return False
        return True
    ref = str(row.get("audio_ref") or "").strip()
    if not ref:
        return False
    obs = con.execute("SELECT episode_id FROM ad_observations WHERE id=?",
                      (observation_id,)).fetchone()
    if obs is None:
        return False
    return ref in _episode_audio_locators(con, obs[0])


@_db.atomic
def confirm_observation_field(con: sqlite3.Connection, observation_id: str,
                              field: str, value: str, expected_revision,
                              evidence: dict | None = None,
                              actor: str = "", ctx=None) -> dict:
    """Human confirm/correct of one copy field on an observation.

    Binds the exact field value, the current observation revision, the
    current source pins, and segment/character or audio evidence to the
    human principal. Machine actors cannot mint human confirmation;
    membership revocation and stale revisions invalidate the write.
    Prior confirmations for the field are superseded, never rewritten.
    """
    _require_member(ctx, "confirm_observation_field")
    _require_confirmation_schema(con)
    if field not in CONFIRMABLE_FIELDS:
        raise _ops.BadInput(f"field must be one of {list(CONFIRMABLE_FIELDS)}")
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise _ops.BadInput("confirmation value must be a nonempty string (max 500)")
    value = value.strip()
    if _is_machine(actor) or not actor.strip():
        raise _ops.BadInput("machine actors cannot write human field confirmations;"
                            " a human principal is required")
    if expected_revision is None:
        raise _ops.MissingPrecondition("expected revision is required")
    pin = _confirmation_pin(con, observation_id)
    if pin["superseded_by"]:
        raise _ops.BadInput("observation is superseded; confirm on the current record")
    if int(expected_revision) != pin["revision"]:
        raise _ops.Conflict(pin["revision"])
    evidence = dict(evidence or {})
    segment_ref = evidence.get("segment_ref")
    char_ref = evidence.get("char_ref")
    audio_ref = str(evidence.get("audio_ref") or "")[:300]
    if segment_ref is not None:
        segs = _segments_of(con, pin["source_version_id"])
        if not isinstance(segment_ref, int) or not 0 <= segment_ref < len(segs):
            raise _ops.BadInput("confirmation segment_ref out of range")
        if char_ref is not None:
            if (not isinstance(char_ref, (list, tuple)) or len(char_ref) != 2):
                raise _ops.BadInput("confirmation char_ref must be [start, end]")
            a, b = char_ref
            seg_len = len(str(segs[segment_ref].get("text", "")))
            if (not isinstance(a, int) or not isinstance(b, int)
                    or not (0 <= a < b <= seg_len)):
                raise _ops.BadInput("confirmation char_ref outside cited segment text")
    elif char_ref is not None:
        raise _ops.BadInput("char_ref requires a segment_ref")
    if segment_ref is None and not audio_ref:
        raise _ops.BadInput("confirmation requires segment/character or audio evidence")
    if audio_ref:
        obs_ep = con.execute("SELECT episode_id FROM ad_observations WHERE id=?",
                             (observation_id,)).fetchone()
        locators = _episode_audio_locators(
            con, obs_ep[0] if obs_ep else "")
        if audio_ref.strip() not in locators:
            raise _ops.BadInput("confirmation audio_ref does not resolve to"
                                " the current episode media asset")
    char_start, char_end = (char_ref if isinstance(char_ref, (list, tuple)) else (None, None))
    with _db.transaction(con, write=True):
        prior = [dict(r) for r in con.execute(
            "SELECT id FROM field_confirmations WHERE workspace_id='local'"
            " AND observation_id=? AND field=? AND superseded_by IS NULL",
            (observation_id, field)).fetchall()]
        cur = con.execute(
            "INSERT INTO field_confirmations (workspace_id, observation_id, field,"
            " value, cleared, observation_revision, source_version_id,"
            " transcript_version_id, segment_ref, char_start, char_end, audio_ref,"
            " actor, created_at) VALUES ('local', ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (observation_id, field, value, pin["revision"],
             pin["source_version_id"], pin["transcript_version_id"], segment_ref,
             char_start, char_end, audio_ref, actor.strip(), _now()))
        new_id = cur.lastrowid
        for p in prior:
            con.execute("UPDATE field_confirmations SET superseded_by=? WHERE id=?",
                        (new_id, p["id"]))
        values = {"field_confirmation": {"field": field, "value": value,
                                         "evidence": {"segment_ref": segment_ref,
                                                      "char_ref": char_ref,
                                                      "audio_ref": audio_ref},
                                         "actor": actor.strip()}}
        new_rev = pin["revision"] + 1
        prev = con.execute("SELECT source_pins,values_json FROM observation_revisions"
                           " WHERE observation_id=? AND revision=?",
                           (observation_id, pin["revision"])).fetchone()
        retained = json.loads(prev["values_json"]) if prev else {}
        values = dict(retained, **values)
        con.execute("INSERT INTO observation_revisions (workspace_id, observation_id,"
                    " revision, values_json, source_pins, actor, created_at)"
                    " VALUES ('local', ?, ?, ?, ?, ?, ?)",
                    (observation_id, new_rev, json.dumps(values),
                     prev["source_pins"] if prev else "{}", actor.strip(), _now()))
        con.execute("UPDATE ad_observations SET revision=?, updated_at=? WHERE id=?",
                    (new_rev, _now(), observation_id))
        _publish_observation_sources(con, observation_id, actor)
        rev = _db.bump_revision(con)
        _db.audit(con, actor.strip(), "observation_field_confirm", "observation",
                  observation_id, {"field": field, "revision": new_rev}, rev)
    return {"observation_id": observation_id, "field": field, "value": value,
            "revision": new_rev, "aggregate_rev": rev}


@_db.atomic
def clear_observation_field(con: sqlite3.Connection, observation_id: str,
                            field: str, expected_revision,
                            actor: str = "", ctx=None) -> dict:
    """Human revoke of a field confirmation; history is kept, authority ends."""
    _require_member(ctx, "clear_observation_field")
    _require_confirmation_schema(con)
    if field not in CONFIRMABLE_FIELDS:
        raise _ops.BadInput(f"field must be one of {list(CONFIRMABLE_FIELDS)}")
    if _is_machine(actor) or not actor.strip():
        raise _ops.BadInput("machine actors cannot revoke human field confirmations")
    if expected_revision is None:
        raise _ops.MissingPrecondition("expected revision is required")
    pin = _confirmation_pin(con, observation_id)
    if pin["superseded_by"]:
        raise _ops.BadInput("observation is superseded; clear on the current record")
    if int(expected_revision) != pin["revision"]:
        raise _ops.Conflict(pin["revision"])
    with _db.transaction(con, write=True):
        prior = [dict(r) for r in con.execute(
            "SELECT id FROM field_confirmations WHERE workspace_id='local'"
            " AND observation_id=? AND field=? AND superseded_by IS NULL",
            (observation_id, field)).fetchall()]
        if not prior:
            raise _ops.BadInput("no current confirmation to clear for this field")
        cur = con.execute(
            "INSERT INTO field_confirmations (workspace_id, observation_id, field,"
            " value, cleared, observation_revision, source_version_id,"
            " transcript_version_id, actor, created_at)"
            " VALUES ('local', ?, ?, '', 1, ?, ?, ?, ?, ?)",
            (observation_id, field, pin["revision"], pin["source_version_id"],
             pin["transcript_version_id"], actor.strip(), _now()))
        new_id = cur.lastrowid
        for p in prior:
            con.execute("UPDATE field_confirmations SET superseded_by=? WHERE id=?",
                        (new_id, p["id"]))
        new_rev = pin["revision"] + 1
        prev = con.execute("SELECT source_pins,values_json FROM observation_revisions"
                           " WHERE observation_id=? AND revision=?",
                           (observation_id, pin["revision"])).fetchone()
        con.execute("INSERT INTO observation_revisions (workspace_id, observation_id,"
                    " revision, values_json, source_pins, actor, created_at)"
                    " VALUES ('local', ?, ?, ?, ?, ?, ?)",
                    (observation_id, new_rev,
                     json.dumps(dict(json.loads(prev["values_json"]) if prev else {},
                                     field_confirmation_cleared={"field": field})),
                     prev["source_pins"] if prev else "{}", actor.strip(), _now()))
        con.execute("UPDATE ad_observations SET revision=?, updated_at=? WHERE id=?",
                    (new_rev, _now(), observation_id))
        _publish_observation_sources(con, observation_id, actor)
        rev = _db.bump_revision(con)
        _db.audit(con, actor.strip(), "observation_field_clear", "observation",
                  observation_id, {"field": field, "revision": new_rev}, rev)
    return {"observation_id": observation_id, "field": field, "cleared": True,
            "revision": new_rev, "aggregate_rev": rev}


def observation_detail(con: sqlite3.Connection, observation_id: str) -> dict:
    """Operator read: observation row, proposed fields, and current
    per-field human confirmations with their bound revisions and evidence."""
    _require_confirmation_schema(con)
    row = con.execute("SELECT * FROM ad_observations WHERE id=?",
                      (observation_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown observation")
    obs = dict(row)
    rev = con.execute("SELECT values_json, source_pins FROM observation_revisions"
                      " WHERE observation_id=? ORDER BY revision DESC LIMIT 1",
                      (observation_id,)).fetchone()
    values = json.loads(rev["values_json"]) if rev else {}
    pins = json.loads(rev["source_pins"]) if rev else {}
    fields = {k: (v.get("value") if isinstance(v, dict) else v)
              for k, v in (values.get("fields") or {}).items()}
    return {"observation": obs, "fields": fields,
            "status": obs.get("status"), "revision": obs.get("revision"),
            "source_pins": pins,
            "confirmations": current_field_confirmations(con, observation_id)}


def affected_relations(con: sqlite3.Connection, observation_id: str) -> dict:
    """Downstream relations requiring deliberate reassessment after changes."""
    proj = [dict(r) for r in con.execute(
        "SELECT id, record_kind FROM evidence WHERE observation_revision_id IN"
        " (SELECT id FROM observation_revisions WHERE observation_id=?)"
        " OR attrs LIKE ?", (observation_id, f"%{observation_id}%")).fetchall()]
    members = [dict(r) for r in con.execute(
        "SELECT creative_id, confirmed, method FROM creative_memberships"
        " WHERE observation_id=?", (observation_id,)).fetchall()]
    occs = [dict(r) for r in con.execute(
        "SELECT occurrence_id, basis FROM occurrence_observations"
        " WHERE observation_id=?", (observation_id,)).fetchall()]
    ev_ids = [p["id"] for p in proj]
    campaigns = []
    reviews = []
    if ev_ids:
        q = ",".join("?" * len(ev_ids))
        campaigns = [dict(r) for r in con.execute(
            f"SELECT campaign_id, evidence_id FROM campaign_evidence WHERE evidence_id IN ({q})",
            ev_ids).fetchall()]
        reviews = [dict(r) for r in con.execute(
            f"SELECT evidence_id, judgment FROM reviews WHERE evidence_id IN ({q})",
            ev_ids).fetchall()]
    clip_verified = [dict(r) for r in con.execute(
        "SELECT id, clip_sha256 FROM ad_observations WHERE id=? AND clip_sha256 IS NOT NULL",
        (observation_id,)).fetchall()]
    return {"evidence_projections": proj, "creative_memberships": members,
            "occurrence_links": occs, "campaign_support": campaigns,
            "clip_verification": clip_verified, "review_states": reviews}


def _lineage_source_pins(con, observation_id):
    row = con.execute("SELECT episode_id,source_version_id,transcript_version_id,media_artifact_id"
                      " FROM ad_observations WHERE id=?", (observation_id,)).fetchone()
    history = con.execute("SELECT source_pins FROM observation_revisions WHERE observation_id=?"
                          " ORDER BY revision DESC LIMIT 1", (observation_id,)).fetchone()
    if row is None or history is None:
        raise _ops.BadInput("parent source authority is unretained")
    pins = json.loads(history["source_pins"])
    archive = pins.get("transcript_version_id")
    source = pins.get("source_version_id")
    pipeline = pins.get("pipeline_transcript_version_id")
    if pins.get("episode_id", row["episode_id"]) != row["episode_id"] or source != row["source_version_id"] \
            or pipeline != row["transcript_version_id"] or pins.get("media_artifact_id") != row["media_artifact_id"]:
        raise _ops.BadInput("parent source authority differs from retained history")
    for version in (source, archive):
        owner = con.execute("SELECT source_key FROM source_versions WHERE id=?", (version,)).fetchone()
        if owner is None or owner["source_key"] != f"transcript:{row['episode_id']}":
            raise _ops.BadInput("parent archived source belongs to another episode")
    if pipeline is not None:
        owner = con.execute("SELECT episode_id FROM transcript_versions WHERE id=?", (pipeline,)).fetchone()
        if owner is None or owner["episode_id"] != row["episode_id"]:
            raise _ops.BadInput("parent pipeline source belongs to another episode")
    pins["episode_id"] = row["episode_id"]
    return pins


def preview_split(con: sqlite3.Connection, observation_id: str,
                  outputs: list) -> dict:
    """Non-mutating split preview with affected relations."""
    row = con.execute("SELECT revision, status FROM ad_observations WHERE id=?",
                      (observation_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown observation")
    if not outputs or len(outputs) > 10:
        raise _ops.BadInput("split requires 1..10 output intervals")
    for o in outputs:
        _finite(o.get("start_s"), "start_s")
        _finite(o.get("end_s"), "end_s")
        if o["end_s"] <= o["start_s"]:
            raise _ops.BadInput("require start_s < end_s in every split output")
    return {"operation": "split", "parent": observation_id,
            "parent_revision": row["revision"], "parent_status": row["status"],
            "outputs": outputs,
            "affected": affected_relations(con, observation_id),
            "warning": "split preserves parent history; reviews, clip verification,"
                       " creative membership, and campaign support are NOT transferred"
                       " and require deliberate reassessment"}


@_db.atomic
def split_observation(con: sqlite3.Connection, observation_id: str,
                      outputs: list, expected_revision,
                      rationale: str = "", actor: str = "owner",
                      idempotency_key: str = "", ctx=None) -> dict:
    """Transactional split: parent superseded, children proposed, no silent transfer."""
    if ctx is None:
        raise _ops.BadInput("split_observation requires a valid member ctx")
    _require_member(ctx, "save_observation")
    if expected_revision is None:
        raise _ops.MissingPrecondition("expected revision is required")
    preview = preview_split(con, observation_id, outputs)
    row = con.execute("SELECT revision, episode_id, transcript_version_id,"
                      " source_version_id, media_artifact_id FROM ad_observations WHERE id=?",
                      (observation_id,)).fetchone()
    if int(expected_revision) != row["revision"]:
        raise _ops.Conflict(row["revision"])
    if not rationale.strip():
        raise _ops.BadInput("split requires a rationale")
    now = _now()
    with _db.transaction(con, write=True):
        parent_rev = con.execute("SELECT values_json, source_pins FROM observation_revisions"
                                 " WHERE observation_id=? AND revision=?",
                                 (observation_id, row["revision"])).fetchone()
        if parent_rev is None:
            raise _ops.BadInput("parent source authority is unretained")
        pins = _lineage_source_pins(con, observation_id)
        child_ids = []
        for i, o in enumerate(outputs):
            cid = _new_id("obs")
            fields = o.get("fields", {})
            validate_field_refs(con, row["source_version_id"], fields)
            con.execute("INSERT INTO ad_observations (id, workspace_id, episode_id,"
                        " media_artifact_id, transcript_version_id, source_version_id,"
                        " status, start_s, end_s, revision, created_at, updated_at)"
                        " VALUES (?, 'local', ?, ?, ?, ?, 'proposed', ?, ?, 1, ?, ?)",
                        (cid, row["episode_id"], row["media_artifact_id"],
                         row["transcript_version_id"], row["source_version_id"],
                         float(o["start_s"]), float(o["end_s"]), now, now))
            values = {"start_s": float(o["start_s"]), "end_s": float(o["end_s"]),
                      "original_bounds_s": [float(o["start_s"]), float(o["end_s"])],
                      "effective_bounds_s": [float(o["start_s"]), float(o["end_s"])],
                      "fields": fields, "status": "proposed",
                      "lineage": {"operation": "split", "parents": [observation_id],
                                  "parent_revision": row["revision"], "rationale": rationale}}
            pins = dict(pins)
            cur = con.execute("INSERT INTO observation_revisions (workspace_id, observation_id,"
                              " revision, values_json, source_pins, actor, created_at)"
                              " VALUES ('local', ?, 1, ?, ?, ?, ?)",
                              (cid, json.dumps(values), json.dumps(pins), actor, now))
            for a in (o.get("attributions") or []):
                con.execute("INSERT INTO observation_attributions (workspace_id,"
                            " observation_id, entity, raw_phrase, channel, method, status,"
                            " created_at) VALUES ('local', ?, ?, ?, ?, ?, ?, ?)",
                            (cid, str(a.get("entity", ""))[:200],
                             str(a.get("raw_phrase", ""))[:500],
                             a.get("channel", "unspecified"),
                             str(a.get("method", ""))[:120],
                             _attribution_status(a, _is_machine(actor)), now))
            ev_id = f"pipe:{cid}:r1"
            con.execute("INSERT INTO evidence (id, source, source_version_id, episode_id,"
                        " orig_start_s, orig_end_s, excerpt, attrs, revision, created_at,"
                        " transcript_version_id, record_kind, observation_revision_id)"
                        " VALUES (?, 'pipeline', ?, ?, ?, ?, ?, ?, 1, ?, ?, 'observation', ?)",
                        (ev_id, row["source_version_id"], row["episode_id"],
                         float(o["start_s"]), float(o["end_s"]),
                         str(o.get("excerpt", ""))[:600],
                         json.dumps({"observation_id": cid}), now,
                         pins["transcript_version_id"],
                         cur.lastrowid))
            child_ids.append(cid)
        new_rev = row["revision"] + 1
        con.execute("UPDATE ad_observations SET status='superseded', revision=?,"
                    " superseded_by=?, updated_at=? WHERE id=?",
                    (new_rev, json.dumps(child_ids), now, observation_id))
        sup_values = json.loads(parent_rev["values_json"]) if parent_rev else {}
        sup_values = dict(sup_values)
        sup_values["status"] = "superseded"
        sup_values["lineage"] = {"operation": "split", "children": child_ids,
                                 "rationale": rationale}
        con.execute("INSERT INTO observation_revisions (workspace_id, observation_id,"
                    " revision, values_json, source_pins, actor, created_at)"
                    " VALUES ('local', ?, ?, ?, ?, ?, ?)",
                    (observation_id, new_rev, json.dumps(sup_values),
                     parent_rev["source_pins"] if parent_rev else "{}", actor, now))
        marker = enqueue_projection_invalidation(con, observation_id,
                                                 f"split into {len(child_ids)} children",
                                                 actor)
        for affected in [observation_id, *child_ids]:
            _publish_observation_sources(con, affected, actor)
        rev = _db.bump_revision(con)
        _db.audit(con, actor, "observation_split", "observation", observation_id,
                  {"children": child_ids, "revision": new_rev,
                   "idempotency_key": idempotency_key or None,
                   "affected": preview["affected"]}, rev)
    return {"parent": observation_id, "revision": new_rev, "children": child_ids,
            "affected": preview["affected"], "invalidation": marker,
            "aggregate_rev": rev}


def preview_merge(con: sqlite3.Connection, observation_ids: list,
                  output: dict) -> dict:
    """Non-mutating merge preview with affected relations."""
    if not observation_ids or len(observation_ids) > 10:
        raise _ops.BadInput("merge requires 1..10 parent observations")
    parents = []
    for oid in observation_ids:
        row = con.execute("SELECT id, revision, status, episode_id FROM ad_observations"
                          " WHERE id=?", (oid,)).fetchone()
        if row is None:
            raise _ops.BadInput(f"unknown observation: {oid}")
        parents.append(dict(row))
    eps = {p["episode_id"] for p in parents}
    if len(eps) != 1:
        raise _ops.BadInput("merge parents must share one episode")
    _finite(output.get("start_s"), "start_s")
    _finite(output.get("end_s"), "end_s")
    affected = {oid: affected_relations(con, oid) for oid in observation_ids}
    return {"operation": "merge", "parents": parents, "output": output,
            "affected": affected,
            "warning": "merge preserves parent history; reviews, clip verification,"
                       " creative membership, and campaign support are NOT transferred"
                       " and require deliberate reassessment"}


@_db.atomic
def merge_observations(con: sqlite3.Connection, observation_ids: list,
                       output: dict, expected_revisions: dict,
                       rationale: str = "", actor: str = "owner",
                       idempotency_key: str = "", ctx=None) -> dict:
    """Transactional merge: parents superseded, one child proposed."""
    if ctx is None:
        raise _ops.BadInput("merge_observations requires a valid member ctx")
    _require_member(ctx, "save_observation")
    if expected_revisions is None:
        raise _ops.MissingPrecondition("expected revisions are required")
    preview = preview_merge(con, observation_ids, output)
    for p in preview["parents"]:
        exp = (expected_revisions or {}).get(p["id"])
        if exp is None:
            raise _ops.MissingPrecondition(f"expected revision required for {p['id']}")
        if int(exp) != p["revision"]:
            raise _ops.Conflict(p["revision"])
    if not rationale.strip():
        raise _ops.BadInput("merge requires a rationale")
    first = con.execute("SELECT episode_id, transcript_version_id, source_version_id,"
                        " media_artifact_id FROM ad_observations WHERE id=?",
                        (observation_ids[0],)).fetchone()
    pins = _lineage_source_pins(con, observation_ids[0])
    for parent_id in observation_ids[1:]:
        other = _lineage_source_pins(con, parent_id)
        if any(other.get(key) != pins.get(key) for key in ("episode_id", "source_version_id",
               "transcript_version_id", "pipeline_transcript_version_id", "media_artifact_id")):
            raise _ops.BadInput("merge parents have different pinned source authority")
    validate_field_refs(con, first["source_version_id"], output.get("fields", {}))
    now = _now()
    with _db.transaction(con, write=True):
        cid = _new_id("obs")
        con.execute("INSERT INTO ad_observations (id, workspace_id, episode_id,"
                    " media_artifact_id, transcript_version_id, source_version_id,"
                    " status, start_s, end_s, revision, created_at, updated_at)"
                    " VALUES (?, 'local', ?, ?, ?, ?, 'proposed', ?, ?, 1, ?, ?)",
                    (cid, first["episode_id"], first["media_artifact_id"],
                     first["transcript_version_id"], first["source_version_id"],
                     float(output["start_s"]), float(output["end_s"]), now, now))
        values = {"start_s": float(output["start_s"]), "end_s": float(output["end_s"]),
                  "original_bounds_s": [float(output["start_s"]), float(output["end_s"])],
                  "effective_bounds_s": [float(output["start_s"]), float(output["end_s"])],
                  "fields": output.get("fields", {}), "status": "proposed",
                  "lineage": {"operation": "merge", "parents": list(observation_ids),
                              "rationale": rationale}}
        pins = dict(pins)
        cur = con.execute("INSERT INTO observation_revisions (workspace_id, observation_id,"
                          " revision, values_json, source_pins, actor, created_at)"
                          " VALUES ('local', ?, 1, ?, ?, ?, ?)",
                          (cid, json.dumps(values), json.dumps(pins), actor, now))
        for a in (output.get("attributions") or []):
            con.execute("INSERT INTO observation_attributions (workspace_id, observation_id,"
                        " entity, raw_phrase, channel, method, status, created_at)"
                        " VALUES ('local', ?, ?, ?, ?, ?, ?, ?)",
                        (cid, str(a.get("entity", ""))[:200],
                         str(a.get("raw_phrase", ""))[:500],
                         a.get("channel", "unspecified"),
                         str(a.get("method", ""))[:120],
                         _attribution_status(a, _is_machine(actor)), now))
        ev_id = f"pipe:{cid}:r1"
        con.execute("INSERT INTO evidence (id, source, source_version_id, episode_id,"
                    " orig_start_s, orig_end_s, excerpt, attrs, revision, created_at,"
                    " transcript_version_id, record_kind, observation_revision_id)"
                    " VALUES (?, 'pipeline', ?, ?, ?, ?, ?, ?, 1, ?, ?, 'observation', ?)",
                    (ev_id, first["source_version_id"], first["episode_id"],
                     float(output["start_s"]), float(output["end_s"]),
                     str(output.get("excerpt", ""))[:600],
                     json.dumps({"observation_id": cid}), now,
                     pins["transcript_version_id"],
                     cur.lastrowid))
        markers = []
        for oid in observation_ids:
            prow = con.execute("SELECT revision, values_json, source_pins FROM"
                               " observation_revisions WHERE observation_id=?"
                               " ORDER BY revision DESC LIMIT 1", (oid,)).fetchone()
            new_rev = con.execute("SELECT revision FROM ad_observations WHERE id=?",
                                  (oid,)).fetchone()["revision"] + 1
            con.execute("UPDATE ad_observations SET status='superseded', revision=?,"
                        " superseded_by=?, updated_at=? WHERE id=?",
                        (new_rev, json.dumps([cid]), now, oid))
            vals = json.loads(prow["values_json"]) if prow else {}
            vals = dict(vals)
            vals["status"] = "superseded"
            vals["lineage"] = {"operation": "merge", "children": [cid],
                               "rationale": rationale}
            con.execute("INSERT INTO observation_revisions (workspace_id, observation_id,"
                        " revision, values_json, source_pins, actor, created_at)"
                        " VALUES ('local', ?, ?, ?, ?, ?, ?)",
                        (oid, new_rev, json.dumps(vals),
                         prow["source_pins"] if prow else "{}", actor, now))
            markers.append(enqueue_projection_invalidation(
                con, oid, f"merged into {cid}", actor))
        for affected in [*observation_ids, cid]:
            _publish_observation_sources(con, affected, actor)
        rev = _db.bump_revision(con)
        _db.audit(con, actor, "observation_merge", "observation", cid,
                  {"parents": list(observation_ids),
                   "idempotency_key": idempotency_key or None,
                   "affected": preview["affected"]}, rev)
    return {"child": cid, "revision": 1, "parents": list(observation_ids),
            "affected": preview["affected"], "invalidations": markers,
            "aggregate_rev": rev}


def enqueue_projection_invalidation(con: sqlite3.Connection, observation_id: str,
                                    reason: str, actor: str = "owner") -> dict:
    """Idempotent projection-invalidation marker (audit-backed enqueue)."""
    marker = {"observation_id": observation_id, "reason": reason,
              "stale_as_of": _now()}
    rev = _db.aggregate_revision(con)
    _db.audit(con, actor, "projection_invalidate", "observation", observation_id,
              marker, rev)
    return marker


def revision_history(con: sqlite3.Connection, observation_id: str) -> list:
    """Complete-value immutable revision history (reachable from review)."""
    rows = con.execute("SELECT revision, values_json, source_pins, actor, created_at"
                       " FROM observation_revisions WHERE observation_id=?"
                       " ORDER BY revision",
                       (observation_id,)).fetchall()
    out = []
    for r in rows:
        out.append({"revision": r["revision"], "values": json.loads(r["values_json"]),
                    "source_pins": json.loads(r["source_pins"]), "actor": r["actor"],
                    "created_at": r["created_at"]})
    return out


def get_observation_detail(con: sqlite3.Connection, observation_id: str) -> dict:
    """Original + effective bounds together; source/suggested/reviewed distinct."""
    row = con.execute("SELECT * FROM ad_observations WHERE id=?",
                      (observation_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown observation")
    d = dict(row)
    rev = con.execute("SELECT values_json, source_pins, actor FROM observation_revisions"
                      " WHERE observation_id=? ORDER BY revision DESC LIMIT 1",
                      (observation_id,)).fetchone()
    values = json.loads(rev["values_json"]) if rev else {}
    pins = json.loads(rev["source_pins"]) if rev else {}
    attributions = [dict(a) for a in con.execute(
        "SELECT entity, raw_phrase, channel, method, status FROM observation_attributions"
        " WHERE observation_id=? ORDER BY channel, entity", (observation_id,)).fetchall()]
    try:
        citation = _citations.observation_citation(con, observation_id)
    except _citations.CitationError:
        citation = {"observation_id": observation_id}
    return {"observation": d, "current_values": values, "source_pins": pins,
            "original_bounds_s": values.get("original_bounds_s",
                                            [d["start_s"], d["end_s"]]),
            "effective_bounds_s": values.get("effective_bounds_s",
                                             [d["start_s"], d["end_s"]]),
            "source_values": {"bounds_s": values.get("original_bounds_s"),
                              "pins": pins},
            "suggested_values": {"bounds_s": values.get("effective_bounds_s"),
                                 "fields": values.get("fields", {}),
                                 "attributions": [a for a in attributions
                                                  if a["status"] == "suggested"]},
            "reviewed_values": values.get("review", {}),
            "attributions": attributions,
            "revision_history": revision_history(con, observation_id),
            "affected_relations": affected_relations(con, observation_id),
            "citation": citation}


def observation_review_payload(con: sqlite3.Connection,
                               observation_id: str) -> dict:
    """Server-data plumbing for Review/Library: data only, native controls."""
    detail = get_observation_detail(con, observation_id)
    return {**detail,
            "record_kind": "observation",
            "actions": [
                {"id": "confirm-ad", "label": "Confirm ad", "kind": "native-button",
                 "method": "review", "params": {"judgment": "reviewed_ad"}},
                {"id": "confirm-not-ad", "label": "Confirm not ad",
                 "kind": "native-button", "method": "review",
                 "params": {"judgment": "reviewed_not_ad"}},
                {"id": "mark-unsure", "label": "Mark unsure", "kind": "native-button",
                 "method": "review", "params": {"judgment": "unsure"}},
                {"id": "split", "label": "Split observation", "kind": "native-button",
                 "method": "preview_split"},
                {"id": "merge", "label": "Merge observations", "kind": "native-button",
                 "method": "preview_merge"},
            ],
            "display": {"candidates_vs_observations":
                        "observations are complete-ad proposals (source=pipeline);"
                        " candidates are raw detector windows",
                        "history": "revision history reachable via revision_history"}}


def observation_export_row(con: sqlite3.Connection, observation_id: str) -> dict:
    """Export surfacing with candidate/observation + original/effective columns."""
    detail = get_observation_detail(con, observation_id)
    obs = detail["observation"]
    return {"record_kind": "observation", "observation_id": obs["id"],
            "episode_id": obs["episode_id"], "status": obs["status"],
            "revision": obs["revision"],
            "original_bounds_s": detail["original_bounds_s"],
            "effective_bounds_s": detail["effective_bounds_s"],
            "fields": detail["current_values"].get("fields", {}),
            "attributions": detail["attributions"],
            "fetch_date": detail["current_values"].get("fetch_date"),
            "fetch_date_source": detail["current_values"].get("fetch_date_source"),
            "citation": detail["citation"]}


def describe_record(con: sqlite3.Connection, evidence_id: str) -> dict:
    """Candidate vs observation read path over the shared evidence table."""
    row = con.execute("SELECT id, source, record_kind, observation_revision_id,"
                      " episode_id, orig_start_s, orig_end_s FROM evidence WHERE id=?",
                      (evidence_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown evidence")
    d = dict(row)
    kind = d["record_kind"] or "candidate"
    out = {"evidence_id": d["id"], "source": d["source"],
           "record_kind": kind, "episode_id": d["episode_id"],
           "is_candidate": kind == "candidate",
           "is_observation": kind == "observation"}
    if kind == "observation" and d["observation_revision_id"] is not None:
        rev = con.execute("SELECT observation_id, revision FROM observation_revisions"
                          " WHERE id=?", (d["observation_revision_id"],)).fetchone()
        out["observation"] = dict(rev) if rev else None
    return out


def effective_text(con: sqlite3.Connection, observation_id: str) -> dict:
    """Original vs refined text via the effective-transcript-version rule.

    Prefers the selected refinement only when its crop fully contains the
    proposed interval and every supporting segment fits within the crop;
    otherwise uses the full version with a visible reason. Never splices
    alternatives into a phrase no source contains.
    """
    detail = get_observation_detail(con, observation_id)
    values = detail["current_values"]
    pins = detail["source_pins"]
    bounds = detail["effective_bounds_s"]
    eff_version = values.get("effective_transcript_version_id")
    full_version = pins.get("transcript_version_id")
    start, end = bounds[0], bounds[1]

    episode_id = detail["observation"]["episode_id"]

    def _archive_text(version_id):
        # The archive pin lives only in source_versions (AD-02).
        if version_id is None:
            return [], None
        srow = con.execute("SELECT payload FROM source_versions WHERE id=? AND source_key=?",
                           (version_id, f"transcript:{episode_id}")).fetchone()
        if srow is None:
            return [], None
        try:
            payload = json.loads(srow["payload"])
        except (json.JSONDecodeError, TypeError):
            return [], None
        segs = payload.get("segments", []) if isinstance(payload, dict) else []
        return segs, {"transcript_version": version_id, "kind": "full"}

    def _refinement_text(version_id):
        # A refinement lives only in transcript_versions and must belong to
        # the observation's episode (AD-02).
        trow = con.execute("SELECT segments, source_offsets FROM transcript_versions"
                           " WHERE id=? AND episode_id=?",
                           (version_id, episode_id)).fetchone()
        if trow is None:
            return [], None
        try:
            segs = json.loads(trow["segments"])
        except (json.JSONDecodeError, TypeError):
            segs = []
        return segs, {"transcript_version": version_id, "kind": "refinement",
                      "offsets": trow["source_offsets"]}

    full_segs, full_meta = _archive_text(full_version)
    full_text = " ".join(s.get("text", "") for s in full_segs
                         if s.get("start", 1e18) < (end or 1e18)
                         and s.get("end", -1) > (start or -1))
    if eff_version is None:
        return {"observation_id": observation_id,
                "original_text": full_text, "effective_text": full_text,
                "effective_version_id": full_version,
                "reason": "full version selected (no refinement override)",
                "alternative": None}
    ref_segs, ref_meta = _refinement_text(eff_version)
    if ref_meta is None:
        return {"observation_id": observation_id,
                "original_text": full_text, "effective_text": full_text,
                "effective_version_id": full_version,
                "reason": "full version selected (refinement is not a transcript"
                          " of this episode)",
                "alternative": None}
    crop = (ref_meta or {})
    try:
        offsets = json.loads(crop.get("offsets", "{}")) if isinstance(
            crop.get("offsets"), str) else (crop.get("offsets") or {})
    except (json.JSONDecodeError, TypeError):
        offsets = {}
    crop_start, crop_end = offsets.get("crop_start_s"), offsets.get("crop_end_s")
    contains = (crop_start is not None and crop_end is not None
                and crop_start <= (start or 0) and (end or 0) <= crop_end)
    if contains and ref_segs:
        ref_text = " ".join(s.get("text", "") for s in ref_segs)
        return {"observation_id": observation_id, "original_text": full_text,
                "effective_text": ref_text, "effective_version_id": eff_version,
                "reason": "refinement crop fully contains the interval",
                "alternative": {"version_id": full_version, "text": full_text}}
    return {"observation_id": observation_id, "original_text": full_text,
            "effective_text": full_text, "effective_version_id": full_version,
            "reason": "crop missing or does not fully contain the interval;"
                      " refinement shown as alternative only",
            "alternative": {"version_id": eff_version,
                            "text": " ".join(s.get("text", "") for s in ref_segs)}}


def save_observation_service(ctx, con: sqlite3.Connection, request,
                             expected_revision=None,
                             expected_revisions: dict | None = None,
                             output: dict | None = None,
                             outputs: list | None = None,
                             rationale: str = "",
                             idempotency_key: str = "",
                             actor: str = "owner") -> dict:
    """Authorized ObservationChange dispatcher (contracts.py shape, no new files).

    ``revise`` writes a new proposed revision over pinned parents; ``split``
    and ``merge`` delegate to the transactional operations above. Multi-output
    commits are atomic; revision conflicts raise (mapped to 412 upstream).
    """
    from . import contracts as _contracts
    if not isinstance(request, _contracts.ObservationChange):
        raise _ops.BadInput("ObservationChange request is required")
    _require_member(ctx, "save_observation")
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise _contracts.ContractError("creation requires an idempotency key",
                                       _contracts.HTTP_BAD_REQUEST)
    op = request.operation
    if op == "split":
        if len(request.input_revision_ids) != 1:
            raise _ops.BadInput("split takes exactly one parent revision")
        parent = _parent_of_revision(con, request.input_revision_ids[0])
        return split_observation(con, parent, list(outputs or []),
                                 (expected_revision if expected_revision is not None
                                  else _current_revision(con, parent)),
                                 rationale=rationale or request.rationale,
                                 actor=actor, idempotency_key=idempotency_key, ctx=ctx)
    if op == "merge":
        parents = [_parent_of_revision(con, rid) for rid in request.input_revision_ids]
        revs = dict(expected_revisions or {})
        for oid in parents:
            revs.setdefault(oid, _current_revision(con, oid))
        return merge_observations(con, parents, dict(output or {}), revs,
                                  rationale=rationale or request.rationale,
                                  actor=actor, idempotency_key=idempotency_key, ctx=ctx)
    if op == "revise":
        if len(request.input_revision_ids) != 1:
            raise _ops.BadInput("revise takes exactly one input revision")
        return revise_observation(con, _parent_of_revision(con, request.input_revision_ids[0]),
                                  request, expected_revision, rationale, actor,
                                  idempotency_key, ctx=ctx)
    raise _ops.BadInput(f"unknown observation operation: {op!r}")


def _parent_of_revision(con: sqlite3.Connection, revision_id) -> str:
    try:
        rid = int(revision_id)
    except (TypeError, ValueError):
        raise _ops.BadInput("input revision ids must reference observation revisions")
    row = con.execute("SELECT observation_id FROM observation_revisions WHERE id=?",
                      (rid,)).fetchone()
    if row is None:
        raise _ops.BadInput(f"unknown observation revision: {revision_id!r}")
    return row["observation_id"]


def _current_revision(con: sqlite3.Connection, observation_id: str) -> int:
    row = con.execute("SELECT revision FROM ad_observations WHERE id=?",
                      (observation_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown observation")
    return row["revision"]


@_db.atomic
def revise_observation(con: sqlite3.Connection, observation_id: str,
                       request=None, expected_revision=None,
                       rationale: str = "", actor: str = "owner",
                       idempotency_key: str = "", ctx=None) -> dict:
    """Human revision over pinned parents: new proposed revision, history kept."""
    if ctx is None:
        raise _ops.BadInput("revise_observation requires a valid member ctx")
    _require_member(ctx, "save_observation")
    if expected_revision is None:
        raise _ops.MissingPrecondition("expected revision is required")
    row = con.execute("SELECT revision, status, source_version_id FROM ad_observations"
                      " WHERE id=?", (observation_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown observation")
    if int(expected_revision) != row["revision"]:
        raise _ops.Conflict(row["revision"])
    intervals = list((request.output_intervals if request is not None else []) or [])
    if len(intervals) > 1:
        raise _ops.BadInput("revise writes one interval; use split for many outputs")
    with _db.transaction(con, write=True):
        prev = con.execute("SELECT values_json, source_pins FROM observation_revisions"
                           " WHERE observation_id=? AND revision=?",
                           (observation_id, row["revision"])).fetchone()
        values = json.loads(prev["values_json"]) if prev else {}
        values = dict(values)
        if intervals:
            start, end = float(intervals[0][0]), float(intervals[0][1])
            if end <= start:
                raise _ops.BadInput("require start_s < end_s")
            values["start_s"] = start
            values["end_s"] = end
            values["effective_bounds_s"] = [start, end]
            con.execute("UPDATE ad_observations SET start_s=?, end_s=?, updated_at=?"
                        " WHERE id=?", (start, end, _now(), observation_id))
        values["lineage"] = {"operation": "revise", "parents": [observation_id],
                             "rationale": rationale or (request.rationale if request else "")}
        new_rev = row["revision"] + 1
        con.execute("INSERT INTO observation_revisions (workspace_id, observation_id,"
                    " revision, values_json, source_pins, actor, created_at)"
                    " VALUES ('local', ?, ?, ?, ?, ?, ?)",
                    (observation_id, new_rev, json.dumps(values),
                     prev["source_pins"] if prev else "{}", actor, _now()))
        con.execute("UPDATE ad_observations SET revision=?, updated_at=? WHERE id=?",
                    (new_rev, _now(), observation_id))
        marker = enqueue_projection_invalidation(con, observation_id, "revise", actor)
        rev = _db.bump_revision(con)
        _db.audit(con, actor, "observation_revise", "observation", observation_id,
                  {"revision": new_rev,
                   "idempotency_key": idempotency_key or None}, rev)
    return {"observation_id": observation_id, "revision": new_rev,
            "status": row["status"], "invalidation": marker, "aggregate_rev": rev}
