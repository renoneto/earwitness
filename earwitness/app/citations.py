"""Source-pinned citations: every cited fact names its exact source version.

A citation carries the source_key/version_no digest identity, episode pin,
character/time references where known, and the fetch_date provenance of the
underlying record. Citations never invent evidence: unknown alignment or
missing pins are stated, not filled in.

History-bound citations (package C) resolve opaque EvidenceRevisionRef and
CampaignRevisionRef locators against full-value revision snapshots. A
reference whose pinned components no longer match retained state returns
REVISION_UNAVAILABLE, never today's values disguised as old ones.
"""
from __future__ import annotations

import base64
import contextlib
import contextvars
import json
import sqlite3
from . import db as _db


class CitationError(Exception):
    pass


class RevisionUnavailable(CitationError):
    """Pinned historical state cannot be reproduced: report the gap."""


# Evaluator identity for the current check-evaluation code. Any change to
# the evaluation logic or its inputs requires a new id; historical checks
# retained under another id resolve only through exact-identity match.
CHECK_EVALUATOR_ID = "ew-check-evaluator/1"


def source_pin(con: sqlite3.Connection, version_id: int | None) -> dict | None:
    if version_id is None:
        return None
    row = con.execute("SELECT id, source_key, version_no, sha256, is_active, created_at"
                      " FROM source_versions WHERE id=?", (version_id,)).fetchone()
    if row is None:
        return None
    return {"source_key": row["source_key"], "version_no": row["version_no"],
            "sha256": row["sha256"], "is_active": bool(row["is_active"]),
            "created_at": row["created_at"]}


def _cited_fetch_date(con: sqlite3.Connection, transcript_version_id,
                      episode_fetch_date) -> dict:
    """The fetch date a citation carries for the version it cites.

    episodes.fetch_date holds the current version's date only. A version
    whose stored payload holds fetch_date_provenance (written by the pilot
    import) cites its own fetch_date and basis; anything else keeps the
    episode value, as before."""
    if transcript_version_id is not None:
        row = con.execute(
            "SELECT json_extract(payload, '$.fetch_date'),"
            " json_extract(payload, '$.fetch_date_provenance.basis')"
            " FROM source_versions WHERE id=? AND json_valid(payload)"
            " AND json_type(payload, '$.fetch_date_provenance')='object'",
            (transcript_version_id,)).fetchone()
        if row is not None and isinstance(row[0], str) and row[0] \
                and row[1] in ("source_file", "received"):
            return {"fetch_date": row[0], "fetch_date_source": "transcript_version",
                    "fetch_date_basis": row[1]}
    return {"fetch_date": episode_fetch_date, "fetch_date_source": "episode"}


def evidence_citation(con: sqlite3.Connection, evidence_id: str) -> dict:
    row = con.execute(
        "SELECT e.id, e.source, e.source_version_id, e.transcript_version_id,"
        " e.episode_id, e.orig_start_s, e.orig_end_s, e.revision,"
        " r.judgment, r.eff_start_s, r.eff_end_s,"
        " ep.fetch_date, ep.transcript_version_id AS ep_version"
        " FROM evidence e LEFT JOIN reviews r ON r.evidence_id=e.id"
        " JOIN episodes ep ON ep.id=e.episode_id WHERE e.id=?",
        (evidence_id,)).fetchone()
    if row is None:
        raise CitationError(f"unknown evidence: {evidence_id!r}")
    pin = source_pin(con, row["source_version_id"])
    reviewed = row["judgment"] is not None
    return {
        "evidence_id": row["id"],
        "episode_id": row["episode_id"],
        "source": row["source"],
        "source_pin": pin,
        "transcript_version_id": row["transcript_version_id"],
        "original_bounds_s": [row["orig_start_s"], row["orig_end_s"]],
        "effective_bounds_s": [row["eff_start_s"], row["eff_end_s"]] if reviewed else None,
        "judgment": row["judgment"],
        **_cited_fetch_date(con, row["transcript_version_id"], row["fetch_date"]),
        "alignment": "unknown",
    }


def transcript_citation(con: sqlite3.Connection, episode_id: str,
                        version_id: int | None = None) -> dict:
    ep = con.execute("SELECT id, fetch_date, transcript_version_id FROM episodes WHERE id=?",
                     (episode_id,)).fetchone()
    if ep is None:
        raise CitationError(f"unknown episode: {episode_id!r}")
    version = version_id if version_id is not None else ep["transcript_version_id"]
    if version is not None and version != ep["transcript_version_id"]:
        owned = con.execute("SELECT 1 FROM episode_history WHERE episode_id=?"
                            " AND transcript_version_id=?",
                            (episode_id, version)).fetchone()
        if owned is None:
            raise CitationError(
                f"transcript version {version} does not belong to episode"
                f" {episode_id!r}")
    pin = source_pin(con, version)
    return {
        "episode_id": episode_id,
        "transcript_version_id": version,
        "source_pin": pin,
        **_cited_fetch_date(con, version, ep["fetch_date"]),
        "alignment": "unknown",
    }


def observation_citation(con: sqlite3.Connection, observation_id: str) -> dict:
    row = con.execute(
        "SELECT o.id, o.episode_id, o.media_artifact_id, o.transcript_version_id,"
        " o.source_version_id, o.status, o.start_s, o.end_s, o.revision,"
        " e.fetch_date FROM ad_observations o"
        " LEFT JOIN episodes e ON e.id=o.episode_id WHERE o.id=?",
        (observation_id,)).fetchone()
    if row is None:
        raise CitationError(f"unknown observation: {observation_id!r}")
    return {
        "observation_id": row["id"],
        "episode_id": row["episode_id"],
        "media_artifact_id": row["media_artifact_id"],
        "transcript_version_id": row["transcript_version_id"],
        "source_pin": source_pin(con, row["source_version_id"]),
        "bounds_s": [row["start_s"], row["end_s"]],
        "status": row["status"],
        "revision": row["revision"],
        **_cited_fetch_date(con, row["transcript_version_id"], row["fetch_date"]),
    }


def transcript_segment(con: sqlite3.Connection, version_id: int,
                       index: int) -> dict | None:
    """One pinned segment by (transcript version, segment index).

    Reads the immutable source_versions payload, never the episode's current
    transcript: replacing a transcript cannot change an old citation's quote.
    """
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise CitationError(f"invalid segment index: {index!r}")
    row = con.execute("SELECT payload FROM source_versions WHERE id=?",
                      (version_id,)).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"])
    except (json.JSONDecodeError, TypeError):
        return None
    segments = payload.get("segments", []) if isinstance(payload, dict) else []
    if index >= len(segments):
        return None
    segment = segments[index]
    return {"segment_index": index, "start": segment.get("start"),
            "end": segment.get("end"), "text": segment.get("text")}


def _encode_ref(prefix: str, body: dict) -> str:
    raw = base64.urlsafe_b64encode(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode())
    return prefix + raw.decode()


def _decode_ref(token: str, prefix: str, fields: tuple) -> dict:
    if not isinstance(token, str) or not token.startswith(prefix):
        raise RevisionUnavailable("unknown revision reference")
    try:
        body = json.loads(base64.urlsafe_b64decode(token[len(prefix):]).decode())
    except (ValueError, UnicodeDecodeError):
        raise RevisionUnavailable("malformed revision reference")
    if not isinstance(body, dict) or body.get("v") != 1 \
            or any(k not in body for k in fields):
        raise RevisionUnavailable("malformed revision reference")
    return body


def _history_tables_present(con: sqlite3.Connection) -> bool:
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return "evidence_history" in tables and "episode_history" in tables


def make_evidence_ref(con: sqlite3.Connection, evidence_id: str) -> str:
    """Bounded opaque locator for the caller's current visible projection."""
    row = con.execute("SELECT e.revision, e.assoc_revision, e.episode_id,"
                      " e.source_version_id, e.transcript_version_id,"
                      " ep.revision AS episode_rev, r.revision AS review_rev FROM evidence e"
                      " LEFT JOIN reviews r ON r.evidence_id=e.id"
                      " JOIN episodes ep ON ep.id=e.episode_id WHERE e.id=?",
                      (evidence_id,)).fetchone()
    if row is None:
        raise CitationError(f"unknown evidence: {evidence_id!r}")
    return _encode_ref("er1.", {"v": 1, "id": evidence_id, "er": row["revision"],
                                "ar": row["assoc_revision"],
                                "rr": row["review_rev"] if row["review_rev"] is not None else 0,
                                "sv": row["source_version_id"], "tv": row["transcript_version_id"],
                                "epr": row["episode_rev"]})


def resolve_evidence_ref(con: sqlite3.Connection, reference: str) -> dict:
    """Resolve an EvidenceRevisionRef to pinned full-value components.

    The reference carries the evidence id plus its pinned component
    revisions. When every pin still matches current state, current values
    are returned; otherwise each component resolves from its history
    snapshot. Raises RevisionUnavailable when any pinned component changed
    without a retained snapshot, or when history tables are absent pre-v5.
    Never mixes old reviews with current bounds.
    """
    body = _decode_ref(reference, "er1.", ("id", "er", "ar", "rr", "sv", "tv"))
    evidence_id = body["id"]
    live = con.execute("SELECT e.*, COALESCE(r.revision, 0) AS review_rev FROM evidence e"
                       " LEFT JOIN reviews r ON r.evidence_id=e.id WHERE e.id=?",
                       (evidence_id,)).fetchone()
    if live is not None and live["revision"] == body["er"] \
            and live["assoc_revision"] == body["ar"] \
            and live["review_rev"] == body["rr"] \
            and live["source_version_id"] == body["sv"] \
            and live["transcript_version_id"] == body["tv"]:
        return _resolve_current(con, evidence_id, live, body)
    if not _history_tables_present(con):
        raise RevisionUnavailable("evidence history unavailable (pre-v5 workspace)")
    snap = con.execute("SELECT * FROM evidence_history WHERE evidence_id=?"
                       " AND evidence_revision=?", (evidence_id, body["er"])).fetchone()
    if snap is None:
        raise RevisionUnavailable("pinned evidence state unavailable")
    source = dict(snap)
    # Associations resolve from their own history by body["ar"]; the evidence
    # snapshot's assoc_revision only records the state when it was taken.
    if source["source_version_id"] != body["sv"] \
            or source["transcript_version_id"] != body["tv"]:
        raise RevisionUnavailable("source pins changed without retained snapshot")
    episode = _resolve_episode(con, source["episode_id"], body)
    review = _resolve_review(con, evidence_id, body)
    assocs = _resolve_assocs(con, evidence_id, body, False)
    return {"evidence_id": evidence_id, "evidence": _history_evidence(source),
            "episode": episode, "review": review, "assocs": assocs,
            "source_pin": source_pin(con, source["source_version_id"]),
            "transcript_version_id": source["transcript_version_id"],
            "interval_text": _pinned_interval_text(con, source)}


def _resolve_current(con, evidence_id: str, live, body: dict) -> dict:
    episode = _resolve_episode(con, live["episode_id"], body)
    review = _resolve_review(con, evidence_id, body)
    assocs = _resolve_assocs(con, evidence_id, body, True)
    return {"evidence_id": evidence_id, "evidence": _history_evidence(dict(live)),
            "episode": episode, "review": review, "assocs": assocs,
            "source_pin": source_pin(con, live["source_version_id"]),
            "transcript_version_id": live["transcript_version_id"],
            "interval_text": _pinned_interval_text(con, dict(live))}


def _history_evidence(source: dict) -> dict:
    """Project a live row or history snapshot onto the stable evidence shape."""
    return {"id": source.get("id", source.get("evidence_id")),
            "source": source.get("source"), "episode_id": source.get("episode_id"),
            "original_bounds_s": [source.get("orig_start_s"), source.get("orig_end_s")],
            "excerpt": source.get("excerpt"),
            "source_version_id": source.get("source_version_id"),
            "transcript_version_id": source.get("transcript_version_id"),
            "revision": source.get("evidence_revision", source.get("revision"))}


def _resolve_episode(con, episode_id: str | None, body: dict) -> dict:
    if "epr" in body:
        row = con.execute("SELECT * FROM episodes WHERE id=? AND revision=?",
                          (episode_id, body["epr"])).fetchone()
        if row is not None:
            return dict(row)
        return _component(con, "episode_history", "episode_id", episode_id,
                          "episode_revision", body["epr"])
    snapshots = con.execute("SELECT * FROM episode_history WHERE episode_id=?"
                            " AND transcript_version_id=?",
                            (episode_id, body["tv"])).fetchall()
    if len(snapshots) != 1:
        raise RevisionUnavailable("legacy episode component is ambiguous or unretained")
    return dict(snapshots[0])


def _resolve_review(con, evidence_id: str, body: dict) -> dict:
    row = con.execute("SELECT * FROM reviews WHERE evidence_id=?", (evidence_id,)).fetchone()
    current_rev = row["revision"] if row else 0
    if current_rev == body["rr"]:
        return dict(row) if row else {"evidence_id": evidence_id, "judgment": None,
                                      "eff_start_s": None, "eff_end_s": None,
                                      "notes": "", "bounds_verified": 0, "revision": 0}
    if not _history_tables_present(con):
        raise RevisionUnavailable("review history unavailable (pre-v5 workspace)")
    snap = con.execute("SELECT * FROM review_history WHERE evidence_id=?"
                       " AND review_revision=?", (evidence_id, body["rr"])).fetchone()
    if snap is None:
        raise RevisionUnavailable("pinned review state unavailable")
    return dict(snap)


def _resolve_assocs(con, evidence_id: str, body: dict, is_current: bool) -> list:
    if is_current:
        current_rev = con.execute("SELECT assoc_revision FROM evidence WHERE id=?",
                                  (evidence_id,)).fetchone()
        if current_rev is not None and current_rev["assoc_revision"] == body["ar"]:
            # PODS: model rows are display-only
            return [dict(a) for a in con.execute(
                "SELECT brand, basis, origin, channel, revision FROM brand_assocs"
                " WHERE evidence_id=? AND basis<>'model' ORDER BY brand, channel",
                (evidence_id,)).fetchall()]
    if not _history_tables_present(con):
        raise RevisionUnavailable("association history unavailable (pre-v5 workspace)")
    snap = con.execute("SELECT assocs_json FROM association_history WHERE evidence_id=?"
                       " AND association_revision=?", (evidence_id, body["ar"])).fetchone()
    if snap is None:
        raise RevisionUnavailable("pinned association state unavailable")
    return [a for a in json.loads(snap["assocs_json"]) if a.get("basis") != "model"]


def _pinned_interval_text(con, source: dict) -> str:
    row = con.execute("SELECT payload FROM source_versions WHERE id=?",
                      (source.get("transcript_version_id"),)).fetchone()
    if row is None:
        return ""
    try:
        payload = json.loads(row["payload"])
    except (json.JSONDecodeError, TypeError):
        return ""
    segments = payload.get("segments", []) if isinstance(payload, dict) else []
    start, end = source.get("orig_start_s"), source.get("orig_end_s")
    if start is None or end is None:
        return ""
    return " ".join(s.get("text", "") for s in segments
                    if isinstance(s, dict) and s.get("start", float("inf")) < end
                    and s.get("end", float("-inf")) > start)


def make_campaign_ref(con: sqlite3.Connection, campaign_id: int) -> str:
    row = con.execute("SELECT revision FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if row is None:
        raise CitationError(f"unknown campaign: {campaign_id!r}")
    pins = [{"evidence_id": r["evidence_id"],
             "evidence_revision": con.execute("SELECT revision FROM evidence WHERE id=?",
                                              (r["evidence_id"],)).fetchone()["revision"]}
            for r in con.execute("SELECT evidence_id FROM campaign_evidence WHERE campaign_id=?",
                                 (campaign_id,)).fetchall()]
    return _encode_ref("cr1.", {"v": 1, "id": campaign_id, "cr": row["revision"],
                                "ev": pins})


def resolve_campaign_ref(con: sqlite3.Connection, reference: str) -> dict:
    """Resolve a CampaignRevisionRef to its retained immutable snapshot.

    Later evidence or campaign changes cannot alter the pinned result; reads
    never manufacture a snapshot. Raises RevisionUnavailable when the pinned
    campaign revision has no retained snapshot.
    """
    body = _decode_ref(reference, "cr1.", ("id", "cr", "ev"))
    if not _history_tables_present(con):
        raise RevisionUnavailable("campaign history unavailable (pre-v5 workspace)")
    row = con.execute("SELECT * FROM campaign_history WHERE campaign_id=?"
                      " AND campaign_revision=?",
                      (body["id"], body["cr"])).fetchone()
    if row is None:
        raise RevisionUnavailable("pinned campaign state unavailable")
    return {"campaign_id": row["campaign_id"], "campaign_revision": row["campaign_revision"],
            "brief": {"name": row["name"], "status": row["status"], "brand": row["brand"],
                      "message": row["message"], "offer": row["offer"],
                      "notes": row["notes"], "target_shows": json.loads(row["target_shows"]),
                      "date_from": row["date_from"], "date_to": row["date_to"]},
            "manual_assessment": {"conclusion": row["manual_conclusion"],
                                  "reason": row["manual_reason"]},
            "evidence_pins": json.loads(row["evidence_pins_json"]),
            "checks": json.loads(row["checks_json"]),
            "history_origin": row["history_origin"]}


def get_historical_check(con: sqlite3.Connection, campaign_id: int, check_id: int,
                         check_revision: int, evaluator_id: str) -> dict:
    """Return a retained computed check result only on exact evaluator match.

    A changed evaluator never silently recomputes under an old citation:
    without a retained row for this (check, revision, evaluator) the lookup
    reports REVISION_UNAVAILABLE.
    """
    if not _history_tables_present(con):
        raise RevisionUnavailable("check history unavailable (pre-v5 workspace)")
    row = con.execute("SELECT result_json, evidence_pins_json FROM check_evaluations"
                      " WHERE campaign_id=? AND check_id=? AND check_revision=?"
                      " AND evaluator_id=?",
                      (campaign_id, check_id, check_revision, evaluator_id)).fetchone()
    if row is None:
        raise RevisionUnavailable("no retained result for this check revision and evaluator")
    return {"result": json.loads(row["result_json"]),
            "evidence_pins": json.loads(row["evidence_pins_json"]),
            "evaluator_id": evaluator_id}


SOURCE_STATE_KINDS = ("evidence", "episode")

_INTERVAL_REF_RE = None


def _interval_ref_pattern():
    import re
    global _INTERVAL_REF_RE
    if _INTERVAL_REF_RE is None:
        _INTERVAL_REF_RE = re.compile(r"^sr1:([0-9a-f]{32})(?::w:(\S+):(\S+))?$")
    return _INTERVAL_REF_RE


def _state_digest(kind: str, record_id: str, projection: dict,
                  *, canonical_out: list | None = None) -> str:
    """SHA-256 of the canonical JSON of {kind, record_id, projection}.
    canonical_out, when given, receives the canonical UTF-8 bytes."""
    import hashlib
    canonical = json.dumps({"kind": kind, "record_id": record_id,
                            "projection": projection},
                           sort_keys=True, ensure_ascii=False).encode("utf-8")
    if canonical_out is not None:
        canonical_out.append(canonical)
    return hashlib.sha256(canonical).hexdigest()


def _projection_span(kind: str, record_id: str, canonical: bytes) -> tuple[int, int] | None:
    """Where the projection's own canonical bytes sit inside the canonical
    bytes of _state_digest(kind, record_id, projection), or None."""
    head = ('{"kind": ' + json.dumps(kind, ensure_ascii=False) + ', "projection": ').encode("utf-8")
    tail = (', "record_id": ' + json.dumps(record_id, ensure_ascii=False) + '}').encode("utf-8")
    if not (canonical.startswith(head) and canonical.endswith(tail)):
        return None
    return len(head), len(canonical) - len(tail)


def _state_digest_of_lists(kind: str, record_id: str, lists: list) -> str:
    """_state_digest(kind, record_id, [l1, l2, ...]) from each list's
    already-canonical UTF-8 bytes, given as (canonical, start, end) slices:
    the canonical JSON of a list of lists is "[" + l1 + ", " + l2 + "]".
    Encoding the parts is equivalent to encoding the whole, so this is
    the same digest without encoding the lists again."""
    import hashlib
    digest = hashlib.sha256()
    digest.update(('{"kind": ' + json.dumps(kind, ensure_ascii=False) + ', "projection": [').encode("utf-8"))
    for index, (canonical, start, end) in enumerate(lists):
        if index:
            digest.update(b", ")
        digest.update(memoryview(canonical)[start:end])
    digest.update(('], "record_id": ' + json.dumps(record_id, ensure_ascii=False) + '}').encode("utf-8"))
    return digest.hexdigest()


def _evidence_projection(con: sqlite3.Connection,
                         evidence_id: str, digests: dict | None = None) -> tuple[dict, dict]:
    """Canonical published evidence projection (K03).

    Returns (content, pins): content feeds the ID digest; pins add the
    bare revision counters used only for current-vs-historical
    comparison at resolution.

    Binds exact applicable evidence/association/review/episode content,
    source/transcript version ownership and public effective/original
    bounds. Private notes, audits and bare revision counters are never
    digest inputs, so they cannot rotate the source ID; counters ride
    alongside as resolution pins only.
    """
    ev = con.execute("SELECT * FROM evidence WHERE id=?",
                     (evidence_id,)).fetchone()
    if ev is None:
        raise CitationError(f"unknown evidence: {evidence_id!r}")
    ev = dict(ev)
    ep = con.execute("SELECT * FROM episodes WHERE id=?",
                     (ev["episode_id"],)).fetchone()
    ep = dict(ep) if ep else None
    rev = con.execute("SELECT * FROM reviews WHERE evidence_id=?",
                      (evidence_id,)).fetchone()
    rev = dict(rev) if rev else None
    # PODS: model rows are display-only
    assocs = [dict(a) for a in con.execute(
        "SELECT brand, basis, origin, channel FROM brand_assocs"
        " WHERE evidence_id=? AND basis<>'model' ORDER BY brand, channel",
        (evidence_id,)).fetchall()]
    reviewed = rev is not None and rev.get("judgment") is not None
    original = [ev.get("orig_start_s"), ev.get("orig_end_s")]
    if reviewed and rev.get("eff_start_s") is not None \
            and rev.get("eff_end_s") is not None:
        effective, basis = [rev["eff_start_s"], rev["eff_end_s"]], \
            "review_effective"
    else:
        effective, basis = list(original), "original"
    content = {
        "source_version_id": ev["source_version_id"],
        "transcript_version_id": ev["transcript_version_id"],
        "episode_id": ev["episode_id"],
        "episode_transcript_version_id": ep["transcript_version_id"]
        if ep else None,
        "review": None if rev is None else {
            "judgment": rev.get("judgment"),
            "bounds_verified": bool(rev.get("bounds_verified", 0))},
        "assocs": assocs,
        "original_bounds": original,
        "effective_bounds": effective,
        "bounds_basis": basis,
        "source": ev["source"],
        "public_episode": _public_episode(ep) if ep else None,
    }
    content["excerpt"] = ev.get("excerpt")
    attrs = json.loads(ev.get("attrs") or "{}")
    content["attrs"] = {key: attrs[key] for key in (
        "read_type", "message", "offer", "code", "observation_id",
        "brand_spoken", "brand_url", "brand_code") if key in attrs}
    pins = {"evidence_revision": ev["revision"],
            "assoc_revision": ev["assoc_revision"],
            "episode_revision": ep["revision"] if ep else None,
            "review_revision": rev["revision"] if rev else 0,
            "source_version_id": ev["source_version_id"],
            "transcript_version_id": ev["transcript_version_id"],
            "episode_id": ev["episode_id"]}
    from . import media
    pins["recording"] = media.recording_identity(con, ep) if ep else None
    content["recording"] = {key: value for key, value in pins["recording"].items()
                            if key != "pipeline_transcript_version_id"} if pins["recording"] else None
    observation_id = attrs.get("observation_id")
    if observation_id:
        observation = con.execute("SELECT id FROM observation_revisions WHERE observation_id=?"
                                  " ORDER BY revision DESC LIMIT 1", (observation_id,)).fetchone()
        if observation is None:
            raise RevisionUnavailable("observation history is unretained")
        pins["observation_revision_id"] = observation["id"]
        pins["confirmation_ids"] = [row["id"] for row in con.execute(
            "SELECT id FROM field_confirmations WHERE observation_id=?"
            " AND superseded_by IS NULL AND cleared=0 ORDER BY field", (observation_id,))]
        observed = _pinned_observation(con, pins)
        content["observation"] = {key: observed[key] for key in ("status", "fields", "attributions")}
        content["observation"]["confirmations"] = {
            field: {key: value for key, value in confirmation.items() if key != "id"}
            for field, confirmation in observed["confirmations"].items()}
    pins.update(_payload_pins(con, pins, digests))
    content["payload_digests"] = {key: pins[key] for key in ("source_payload_digest", "transcript_payload_digest")}
    return content, pins


def _episode_projection(con: sqlite3.Connection,
                        episode_id: str, digests: dict | None = None) -> tuple[dict, dict]:
    ep = con.execute("SELECT * FROM episodes WHERE id=?",
                     (episode_id,)).fetchone()
    if ep is None:
        raise CitationError(f"unknown episode: {episode_id!r}")
    ep = dict(ep)
    content = dict(_public_episode(ep), transcript_version_id=ep["transcript_version_id"])
    pins = {"episode_revision": ep["revision"], "transcript_version_id": ep["transcript_version_id"],
            "episode_id": episode_id}
    from . import media
    pins["recording"] = media.recording_identity(con, ep)
    content["recording"] = {key: value for key, value in pins["recording"].items()
                            if key != "pipeline_transcript_version_id"} if pins["recording"] else None
    pins.update(_payload_pins(con, pins, digests))
    content["payload_digests"] = {key: pins[key] for key in ("source_payload_digest", "transcript_payload_digest")}
    return content, pins


def _payload_pins(con, pins, memo: dict | None = None):
    """Payload digests of the pinned versions. memo is an optional per-pass
    memo of payload digests keyed by version id (an immutable identity)."""
    import hashlib
    digests = {}
    for name, version in (("source_payload_digest", pins.get("source_version_id")),
                          ("transcript_payload_digest", pins.get("transcript_version_id"))):
        if version is None:
            digests[name] = None
            continue
        if memo is not None and version in memo:
            digests[name] = memo[version]
            continue
        row = con.execute("SELECT payload FROM source_versions WHERE id=?", (version,)).fetchone()
        if row is None:
            raise RevisionUnavailable("publication source payload is unretained")
        digests[name] = hashlib.sha256(row["payload"].encode("utf-8")).hexdigest()
        if memo is not None:
            memo[version] = digests[name]
    return digests


def _validate_payload_pins(con, pins, memo: dict | None = None):
    actual = _payload_pins(con, pins, memo)
    if any(key not in pins or pins[key] != digest for key, digest in actual.items()):
        raise RevisionUnavailable("pinned source payload digest is unqualified or mismatched")

@_db.atomic
def publish_source_state(con: sqlite3.Connection, *, kind: str,
                         record_id: str, actor: str, digests: dict | None = None) -> str:
    """Publish the current coherent projection as an immutable source state.

    Part of the producer's caller-owned write transaction: call after
    final component/history updates, before commit. Inserts a new row
    only when the canonical published projection changed; otherwise
    reuses the existing ID. Never repoints an existing ID. Returns the
    opaque SourceRef1 (`sr1:<state_id>`).
    """
    if kind not in SOURCE_STATE_KINDS:
        raise CitationError(f"unknown source kind: {kind!r}")
    from . import db as _db
    if not _db.has_v8_tables(con):
        raise CitationError("source publication requires the v8 schema")
    if kind == "episode" and con.execute("SELECT 1 FROM customer_episode_shows WHERE episode_id=?"
                                         " AND workspace_id='local'", (record_id,)).fetchone() is None:
        from . import customer
        customer.rebuild_customer_show_map(con)
    # digests: an optional payload-digest memo keyed by source version id
    # (an immutable identity) shared by one producer's publications.
    content, pins = _evidence_projection(con, record_id, digests) \
        if kind == "evidence" else _episode_projection(con, record_id, digests)
    digest = _state_digest(kind, record_id, content)
    row = con.execute("SELECT id, projection_digest FROM source_states"
                      " WHERE kind=? AND record_id=?"
                      " ORDER BY publication_order DESC LIMIT 1",
                      (kind, record_id)).fetchone()
    if row is not None and row["projection_digest"] == digest:
        return row["id"]
    # Bounds only ride along a new state (an episode's means parsing its
    # transcript), so an unchanged record costs no parse.
    if kind == "episode":
        segments = _transcript_payload(con, pins["transcript_version_id"])
        span = [min((s["start"] for s in segments), default=0.0),
                max((s["end"] for s in segments), default=0.0)]
        bounds = {"original": span, "effective": span, "basis": "original"}
    else:
        bounds = {"original": content["original_bounds"],
                  "effective": content["effective_bounds"],
                  "basis": content["bounds_basis"]}
    _retain_publication_components(con, kind, record_id, pins, actor)
    import uuid
    state_id = "sr1:" + uuid.uuid4().hex
    order = con.execute("SELECT COALESCE(MAX(publication_order),0)+1"
                        " FROM source_states WHERE workspace_id='local'"
                        " AND kind=? AND record_id=?",
                        (kind, record_id)).fetchone()[0]
    con.execute("INSERT INTO source_states (id, workspace_id, kind,"
                " record_id, publication_order, pins_json, bounds_json,"
                " projection_digest, captured_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (state_id, "local", kind, record_id, order,
                 json.dumps(pins, sort_keys=True),
                 json.dumps(bounds, sort_keys=True), digest,
                 _captured_at()))
    return state_id


def qualify_legacy_source_ref(con, ref):
    import re
    match = re.fullmatch(r"ev:(.+)@v(\d+)", ref)
    transcript = re.fullmatch(r"tx:(.+)@v(\d+):(\d+)", ref)
    if match is None and transcript is None:
        raise RevisionUnavailable("unqualified legacy source reference")
    kind = "evidence" if match else "episode"
    identity, version = (match or transcript).group(1, 2)
    candidates = []
    for row in con.execute("SELECT id, pins_json FROM source_states"
                           " WHERE kind=? AND record_id=?", (kind, identity)):
        pins = json.loads(row["pins_json"])
        if pins.get("source_version_id" if match else "transcript_version_id") == int(version):
            candidates.append(row["id"])
    if len(candidates) != 1:
        raise RevisionUnavailable("legacy source components are ambiguous or unretained")
    # Database only: the caller's own resolution of the qualified ref
    # applies its audio gate; this check runs as the owner, so it must
    # never reach the recording (CLIPCORE-SEC-R3-01).
    resolve_source_state(con, candidates[0], media=False)
    if transcript:
        segment = transcript_segment(con, int(version), int(transcript.group(3)))
        if segment is None:
            raise RevisionUnavailable("legacy interval is unretained")
        return f"{candidates[0]}:w:{segment['start']}:{segment['end']}"
    return candidates[0]


def _transcript_payload(con, version_id) -> list:
    if version_id is None:
        return []
    row = con.execute("SELECT payload FROM source_versions WHERE id=?",
                      (version_id,)).fetchone()
    if row is None:
        raise RevisionUnavailable("pinned transcript is unretained")
    try:
        payload = json.loads(row["payload"])
        segments = payload["segments"]
        if not isinstance(segments, list):
            raise ValueError()
        return segments
    except (ValueError, TypeError, KeyError):
        raise RevisionUnavailable("pinned transcript is unreadable")


def _retain_publication_components(con, kind, record_id, pins, actor):
    from . import db
    components = [("episode_history", "episode_id", pins["episode_id"],
                   "episode_revision", pins["episode_revision"],
                   db.record_episode_snapshot)]
    if kind == "evidence":
        components.extend([
            ("evidence_history", "evidence_id", record_id,
             "evidence_revision", pins["evidence_revision"], db.record_evidence_snapshot),
            ("review_history", "evidence_id", record_id,
             "review_revision", pins["review_revision"], db.record_review_snapshot),
            ("association_history", "evidence_id", record_id,
             "association_revision", pins["assoc_revision"], db.record_association_snapshot)])
    for table, key, identity, revision_key, revision, retain in components:
        if revision is None:
            raise RevisionUnavailable("publication component is absent")
        if con.execute(f"SELECT 1 FROM {table} WHERE {key}=? AND {revision_key}=?",
                       (identity, revision)).fetchone() is None:
            retain(con, identity, actor)


def publish_episode_sources(con, episode_id, actor, digests: dict | None = None):
    """Publish one episode and every evidence row of it. digests: an
    optional payload-digest memo (by version id) shared across calls."""
    publish_source_state(con, kind="episode", record_id=episode_id, actor=actor, digests=digests)
    for row in con.execute("SELECT id FROM evidence WHERE episode_id=?",
                           (episode_id,)).fetchall():
        publish_source_state(con, kind="evidence", record_id=row["id"], actor=actor,
                             digests=digests)


def _captured_at() -> str:
    import datetime
    return datetime.datetime.now(
        datetime.timezone.utc).isoformat(timespec="seconds")


def current_source_ref(con: sqlite3.Connection, kind: str,
                       record_id: str) -> str:
    """Pure lookup of the latest publication ID for a record (K03)."""
    row = con.execute("SELECT id FROM source_states WHERE workspace_id='local'"
                      " AND kind=? AND record_id=? ORDER BY publication_order"
                      " DESC LIMIT 1", (kind, record_id)).fetchone()
    if row is None:
        raise RevisionUnavailable(
            f"no publication for {kind}:{record_id}")
    return row["id"]


def _parse_source_ref(ref: str) -> tuple[str, tuple | None]:
    """Parse a SourceRef1, including server-issued interval variants."""
    m = _interval_ref_pattern().match(ref or "")
    if not m:
        raise CitationError(f"unrecognized source reference: {ref!r}")
    window = None
    if m.group(2) is not None:
        try:
            start, end = float(m.group(2)), float(m.group(3))
        except (TypeError, ValueError):
            raise CitationError("interval bounds must be finite numbers")
        import math
        if not (math.isfinite(start) and math.isfinite(end)) \
                or not (end > start >= 0):
            raise CitationError(
                "interval bounds must be finite, ordered and nonnegative")
        window = (start, end)
    return "sr1:" + m.group(1), window


def _ctx_val(ctx, key, default=None):
    """Read a context field from an object or a plain mapping."""
    if isinstance(ctx, dict):
        return ctx.get(key, default)
    return getattr(ctx, key, default)


def _sparse_view(ctx) -> bool:
    """Restricted callers get identifier-only projections.

    Share guests and callers without the full research+transcript grant
    must not receive publication-bound identifiers (pins, canonical
    refs, relationship, reasons) that disclose ungranted changes, nor
    captured text without the transcript grant. Only authorized object
    identity and actions are retained.
    """
    if ctx is None:
        return False
    if hasattr(ctx, "permits"):
        return not (ctx.permits("metadata") and ctx.permits("excerpt") and ctx.permits("transcript"))
    if _ctx_val(ctx, "authority_kind", "membership") == "share":
        return True
    scopes = set(_ctx_val(ctx, "scopes", None) or ())
    return not ({"research:read", "transcripts:read"} <= scopes)


def _sparse_projection(full: dict, ctx) -> dict:
    scopes = set(_ctx_val(ctx, "scopes", None) or ())
    sparse = {"kind": full["kind"], "record_id": full["record_id"],
              "actions": ["read"]}
    episode = full.get("episode")
    if isinstance(episode, dict):
        identity = episode.get("episode_id") \
            or (episode.get("public") or {}).get("episode_id")
        if identity:
            sparse["episode"] = {"episode_id": identity}
    if (ctx.permits("transcript") if hasattr(ctx, "permits") else "transcripts:read" in scopes):
        sparse["text"] = full.get("text")
    return sparse


def resolve_source_state(con, ref: str, *, ctx=None, include_text=True, media=True) -> dict:
    """The one customer resolution entry (K03).

    Resolves an immutable publication through the full-value
    component/history authority: exact captured text/components, never a
    latest-state alias. Unknown refs are unavailable/repair, not current
    substitution. Old ambiguous ev/tx/ep inputs are handled by the
    customer record edge, not here. Restricted callers (share guests or
    partial grants) receive the sparse identifier-only projection.
    media=False resolves from the database alone: no recording proof or
    file work, and the media descriptor is {"playback": "unchecked"}.
    """
    if ctx is None:
        ctx = _owner_context()
    state_id, window = _parse_source_ref(ref)
    publication = _resolve_publication(con, state_id, ref, ctx, media=media)
    if publication["level"] == "excerpt":
        # Any issued window is ignored: the text is the fixed safe excerpt.
        return excerpt_projection(con, publication, ref)
    full = _windowed(con, publication, ref, window)
    if include_text:
        full["text"] = _pinned_version_interval_text(
            con, publication["projection"]["transcript_version_id"],
            full["selected_bounds"]["start_s"], full["selected_bounds"]["end_s"])
    if _sparse_view(ctx):
        return _sparse_projection(full, ctx)
    if full["media"].get("recording_id"):
        from urllib.parse import quote
        full["media"]["src"] = f"/api/v1/customer/records/{quote(ref, safe='')}/media"
    return full


# -- Plan A: the safe excerpt (rule pa1) and the disclosure level ---------
# Design docs/plans/2026-10-22-plan-a-excerpt-view.md revision 4, sections
# 3-5. A workspace whose transcript policy denies the transcript (Plan A)
# sees each ad's safe excerpt, derived on every use from the pinned
# transcript; the stored or detector excerpt is never used there.

PA1 = "pa1"
EXCERPT_CONTEXT_S = 20.0
EXCERPT_MAX_CHARS = 600
PLAN_REFUSAL = "not available on your plan"


def safe_excerpt_from_segments(segments, start_s, end_s) -> dict | None:
    """Rule pa1 over one transcript's segments and an ad span [a, b]: the
    timed segments that overlap [a, b], each dropped whole when it reaches
    outside [a - 20 s, b + 20 s], joined in time order and cut at a word
    boundary at or before character 600. None when nothing is kept."""
    if not (_plain_number(start_s) and _plain_number(end_s)):
        return None
    a, b = start_s, end_s
    overlapping = []
    for position, seg in enumerate(segments if isinstance(segments, list) else []):
        if not isinstance(seg, dict):
            continue
        s0, s1, text = seg.get("start"), seg.get("end"), seg.get("text", "")
        if not (_plain_number(s0) and _plain_number(s1)) or not isinstance(text, str):
            continue
        if not (s0 < b and s1 > a):
            continue
        overlapping.append((s0, position, s1, text))
    return _pa1_excerpt(overlapping, a, b)


def _pinned_safe_excerpt(pinned, start_s, end_s) -> dict | None:
    """safe_excerpt_from_segments over a decoded pinned transcript: the
    same answer, through its overlap index once the transcript was asked
    for more than a few windows (PLANA-SEARCH-SPEED: one Plan A agent
    search asks one transcript for every record in it)."""
    if not (_plain_number(start_s) and _plain_number(end_s)):
        return None
    overlapping = pinned.overlapping(start_s, end_s)
    if overlapping is None:
        return safe_excerpt_from_segments(pinned.segments, start_s, end_s)
    return _pa1_excerpt(overlapping, start_s, end_s)


def _pa1_excerpt(overlapping, a, b) -> dict | None:
    """Rule pa1 from the (start, position, end, text) entries of the timed
    segments that overlap [a, b]."""
    kept = [entry for entry in overlapping
            if not (entry[0] < a - EXCERPT_CONTEXT_S or entry[2] > b + EXCERPT_CONTEXT_S)]
    if not kept:
        return None
    kept.sort(key=lambda k: (k[0], k[1]))
    text = " ".join(k[3] for k in kept)
    truncated = len(text) > EXCERPT_MAX_CHARS
    if truncated:
        cut = text[:EXCERPT_MAX_CHARS]
        if not text[EXCERPT_MAX_CHARS].isspace():
            space = max(cut.rfind(c) for c in (" ", "\n", "\t"))
            cut = cut[:space] if space > 0 else ""
        text = cut.rstrip()
    if not text.strip():
        return None
    out = {"text": text, "start_s": kept[0][0], "end_s": max(k[2] for k in kept)}
    if truncated:
        out["truncated"] = True  # display only (the screen marks the cut)
    return out


def safe_excerpt(con, transcript_version_id, start_s, end_s, *,
                 transcripts: dict | None = None) -> dict | None:
    """safe_excerpt_from_segments over a pinned transcript version (None
    when the version or its payload is unretained or unreadable)."""
    if transcript_version_id is None:
        return None
    try:
        pinned = pinned_transcript(con, transcript_version_id, transcripts)
    except (ValueError, TypeError):
        return None
    if pinned is None:
        return None
    return _pinned_safe_excerpt(pinned, start_s, end_s)


def _policy_source(con, version_id) -> str:
    source_key = _version_source_key(con, version_id)
    if source_key is None:
        raise RevisionUnavailable("source policy identity is unretained")
    return source_key


def transcript_policy_denied(con, source_key) -> bool:
    """The source's transcript policy row exists and does not allow it: a
    direct read of the row, never the refusal text."""
    row = con.execute("SELECT agent_disclosure FROM source_content_policy"
                      " WHERE workspace_id='local' AND source_key=?"
                      " AND content_category='transcript'", (source_key,)).fetchone()
    return row is not None and row[0] != "allow"


def untranscribed_policy_denied(con, e_source, episode_id) -> bool:
    """Plan A for a publication with no pinned transcript: the evidence
    source's or the episode's own transcript row denies the transcript.
    Such a row has no safe excerpt (design 3); its stored excerpt is never
    text or a match at excerpt level."""
    return any(transcript_policy_denied(con, key)
               for key in (e_source, f"episode:{episode_id}") if key)


def disclosure_level(con, kind: str, pins: dict, ctx, *, export: bool = False) -> str:
    """"full" or "excerpt" for one publication, as an explicit sequence
    (design 4): metadata and excerpt on the evidence policy source E, the
    same on the pinned transcript source T, then transcript on T. Only a
    transcript refusal by T's own policy row, for a caller whose credential
    permits transcripts, selects "excerpt"; every other refusal propagates.
    Computed per call; nothing is cached or read from the client."""
    from .access import AuthorizationError
    t_version = pins.get("transcript_version_id")
    e_version = _excerpt_policy_version(kind, pins)
    e_source = _policy_source(con, e_version) if e_version is not None else None
    if t_version is None:
        # No pinned transcript: the episode's metadata and, as before the
        # excerpt view, metadata and excerpt on the evidence source. At
        # excerpt level the row is served with no text.
        ctx.authorize_disclosure(con, f"episode:{pins['episode_id']}", ["metadata"])
        if e_source is not None:
            ctx.authorize_disclosure(con, e_source, ["metadata", "excerpt"], export=export)
        if ctx.permits("transcript") and \
                untranscribed_policy_denied(con, e_source, pins["episode_id"]):
            return "excerpt"
        return "full"
    t_source = _policy_source(con, t_version)
    if e_source is not None:
        ctx.authorize_disclosure(con, e_source, ["metadata", "excerpt"], export=export)
    ctx.authorize_disclosure(con, t_source, ["metadata", "excerpt"], export=export)
    try:
        ctx.authorize_disclosure(con, t_source, ["transcript"], export=export)
    except AuthorizationError:
        if ctx.permits("transcript") and transcript_policy_denied(con, t_source):
            return "excerpt"
        raise
    return "full"


def current_safe_excerpt(con, evidence_id, *, transcripts: dict | None = None) -> dict | None:
    """safe_excerpt of an evidence row's current publication (its pinned
    transcript version and effective bounds), or None. transcripts: a
    per-pass memo of decoded transcripts (pinned_transcript)."""
    try:
        ref = current_source_ref(con, "evidence", evidence_id)
    except RevisionUnavailable:
        return None
    row = con.execute("SELECT pins_json, bounds_json FROM source_states WHERE id=?",
                      (ref,)).fetchone()
    pins, bounds = json.loads(row["pins_json"]), json.loads(row["bounds_json"])
    effective = bounds["effective"]
    return safe_excerpt(con, pins.get("transcript_version_id"), effective[0], effective[1],
                        transcripts=transcripts)


def excerpt_projection(con, publication: dict, ref: str, *,
                       transcripts: dict | None = None) -> dict:
    """The excerpt projection of one resolved publication (design 5): the
    record's identity and metadata, `text` = its safe excerpt (or None),
    `excerpt_bounds`, `disclosure: "excerpt"`. Never a window, segments,
    context, interval text, canonical or source ref, or a stored excerpt."""
    body = publication["body"]
    bounds = publication["bounds"]
    pins = publication["projection"]
    effective = bounds["effective"]

    def named(span):
        return {"start_s": span[0], "end_s": span[1]}
    out = {"ref": ref, "kind": publication["kind"], "record_id": publication["record_id"],
           "disclosure": "excerpt", "pins": pins,
           "original_bounds": named(bounds["original"]),
           "effective_bounds": named(effective), "selected_bounds": named(effective),
           "bounds_basis": bounds["basis"], "relationship": publication["relationship"],
           "availability": "available" if publication["available"] else "source_removed",
           "reason": None if publication["relationship"] == "current"
           else "retained historical publication",
           "review": body.get("review"), "assocs": body.get("assocs") or [],
           "provenance": body.get("provenance"), "episode": body["episode"],
           "media": {"playback": "unavailable", "reason": "clip unavailable",
                     "alignment": "unknown", "timing": "unknown"},
           "text": None, "excerpt_bounds": None}
    if publication["kind"] == "evidence":
        out["evidence"] = {k: v for k, v in body["evidence"].items() if k != "excerpt"}
        safe = safe_excerpt(con, pins.get("transcript_version_id"), effective[0], effective[1],
                            transcripts=transcripts)
        if safe is not None:
            out["text"] = safe["text"]
            out["excerpt_bounds"] = {"start_s": safe["start_s"], "end_s": safe["end_s"]}
            if safe.get("truncated"):
                out["truncated"] = True
    return out


def _owner_context():
    from .access import AccessContext
    return AccessContext.create(principal_id="local-owner", principal_kind="owner",
                                workspace_id="local", credential_kind="local_operator")


def _resolve_publication(con, state_id: str, ref: str, ctx, digests: dict | None = None,
                         *, media: bool = True) -> dict:
    """The window-independent part of resolving one publication: authority,
    pinned components and current-vs-historical relationship. ref only
    names the input in the unknown-publication error; digests is an
    optional per-pass payload-digest memo (_payload_pins)."""
    row = con.execute("SELECT * FROM source_states WHERE id=?",
                      (state_id,)).fetchone()
    if row is None:
        raise RevisionUnavailable(f"unknown source publication: {ref!r}")
    state = dict(row)
    if not ctx.permits("metadata"):
        from .access import AuthorizationError
        raise AuthorizationError("source identity disclosure denied", 403)
    projection = json.loads(state["pins_json"])
    level = "full"
    if ctx is not None and hasattr(ctx, "authorize_disclosure") and not _sparse_view(ctx):
        # Plan A (design 4): the explicit two-source sequence, per request.
        level = disclosure_level(con, state["kind"], projection, ctx)
    elif ctx is not None and hasattr(ctx, "authorize_disclosure"):
        if projection.get("transcript_version_id") is None:
            ctx.authorize_disclosure(con, f"episode:{projection['episode_id']}",
                                     ["metadata"])
        for version_id, categories in (
                (_excerpt_policy_version(state["kind"], projection), ["metadata", "excerpt"]),
                (projection.get("transcript_version_id"), ["metadata", "transcript"])):
            if version_id is not None:
                provenance = _version_provenance(con, version_id)
                if provenance is None:
                    raise RevisionUnavailable("source policy identity is unretained")
                ctx.authorize_disclosure(con, provenance["source_key"],
                                         [f for f in categories if ctx.permits(f)])
    _validate_payload_pins(con, projection, digests)
    bounds = json.loads(state["bounds_json"])
    kind, record_id = state["kind"], state["record_id"]
    if kind == "evidence":
        # At excerpt level the recording is never served: no proof work.
        body = _resolve_pinned_evidence(con, record_id, projection, ctx=ctx,
                                        media=media and level == "full")
    else:
        body = _resolve_pinned_episode(con, record_id, projection, ctx=ctx,
                                       media=media and level == "full")
    if kind == "evidence":
        content, _pins = _evidence_projection(con, record_id, digests) \
            if con.execute("SELECT 1 FROM evidence WHERE id=?", (record_id,)).fetchone() \
            else (None, None)
    else:
        content, _pins = _episode_projection(con, record_id, digests) \
            if con.execute("SELECT 1 FROM episodes WHERE id=?", (record_id,)).fetchone() \
            else (None, None)
    relationship = "current" if content is not None and \
        current_source_ref(con, kind, record_id) == state_id and \
        _state_digest(kind, record_id, content) == state["projection_digest"] else "historical"
    return {"kind": kind, "record_id": record_id, "projection": projection,
            "bounds": bounds, "body": body, "relationship": relationship,
            "available": content is not None, "level": level}


def _selected_span(bounds: dict, window) -> list:
    effective = bounds["effective"]
    selected = list(window) if window else effective
    if window and (window[0] < effective[0] or window[1] > effective[1]):
        raise CitationError("issued interval is outside pinned source bounds")
    return selected


def require_window_in_bounds(con, ref: str) -> None:
    """DPC-01: a ref a write stores keeps its issued window inside its
    pinned bounds, at every disclosure level. An excerpt-level read ignores
    the window, so without this a window stored under Plan A (or under a
    source's own transcript deny) would first be checked after a change to
    full level (an upgrade, a lifted deny) and cost every member the
    frontend contract. Raises CitationError, as the full-level read does."""
    state_id, window = _parse_source_ref(ref)
    row = con.execute("SELECT bounds_json FROM source_states WHERE id=?", (state_id,)).fetchone()
    if window and row is not None:
        _selected_span(json.loads(row[0]), window)


def _windowed(con, publication: dict, ref: str, window) -> dict:
    """The resolved view of one ref (a publication, or an issued interval
    inside it), without text."""
    bounds = publication["bounds"]
    kind, record_id = publication["kind"], publication["record_id"]
    selected = _selected_span(bounds, window)
    relationship = publication["relationship"]

    def named(span):
        return {"start_s": span[0], "end_s": span[1]}
    return {"ref": ref, "canonical_source_ref": current_source_ref(con, kind, record_id),
            "kind": kind, "record_id": record_id,
            "original_bounds": named(bounds["original"]),
            "effective_bounds": named(bounds["effective"]),
            "selected_bounds": named(selected),
            "bounds_basis": bounds["basis"], **publication["body"],
            "relationship": relationship,
            "availability": "available" if publication["available"] else "source_removed",
            "reason": None if relationship == "current" else "retained historical publication"}


class ResolutionPass:
    """Resolve many refs of one pure read pass, each publication once.

    A server-issued interval ref differs from its publication only by a
    window, so the publication's authority, pinned components and
    relationship are resolved once per state id, and each pinned
    transcript is decoded once per version id. Both are immutable
    identities, and the pass lives only for one caller-owned read over one
    connection with one ctx: callers must not write while it is alive.
    current() returns exactly what resolve_source_state(con, ref, ctx=ctx)
    would for the fields result binding reads; a failure raises at the
    same ref with the same error.
    """

    def __init__(self, con, ctx=None, *, transcripts: dict | None = None):
        self.con = con
        self.ctx = ctx if ctx is not None else _owner_context()
        if _sparse_view(self.ctx):
            raise ValueError("a resolution pass serves full views; sparse callers resolve per ref")
        self._publications: dict[str, dict] = {}
        self._canonical: dict[tuple, str] = {}
        self._digests: dict = {}
        self.transcripts: dict = transcripts if transcripts is not None else {}

    def _publication(self, state_id: str, ref: str) -> dict:
        publication = self._publications.get(state_id)
        if publication is None:
            publication = _resolve_publication(self.con, state_id, ref, self.ctx, self._digests)
            self._publications[state_id] = publication
        return publication

    def view(self, ref: str) -> dict:
        """resolve_source_state(con, ref, ctx=ctx, include_text=False) from
        the pass's publication (REDESIGN S4: a Search item's clip block, as
        its record has it). Its media block is fresh per call; read the
        rest only."""
        state_id, window = _parse_source_ref(ref)
        publication = self._publication(state_id, ref)
        if publication["level"] == "excerpt":
            return excerpt_projection(self.con, publication, ref, transcripts=self.transcripts)
        full = _windowed(self.con, publication, ref, window)
        full["media"] = dict(full.get("media") or {})
        if full["media"].get("recording_id"):
            from urllib.parse import quote
            full["media"]["src"] = f"/api/v1/customer/records/{quote(ref, safe='')}/media"
        return full

    def current(self, ref: str) -> dict:
        """relationship, text, provenance and episode identity of one ref.
        Nested values are fresh per call."""
        state_id, window = _parse_source_ref(ref)
        publication = self._publication(state_id, ref)
        if publication["level"] == "excerpt":
            # Never the interval text (design 4): the pin's safe excerpt.
            projected = excerpt_projection(self.con, publication, ref,
                                           transcripts=self.transcripts)
            provenance = publication["body"].get("provenance")
            episode = publication["body"]["episode"]
            return {"relationship": publication["relationship"], "text": projected["text"],
                    "excerpt_bounds": projected["excerpt_bounds"], "disclosure": "excerpt",
                    "provenance": dict(provenance) if provenance is not None else None,
                    "episode": {"episode_id": episode["episode_id"],
                                "public": dict(episode["public"])}}
        bounds = publication["bounds"]
        selected = _selected_span(bounds, window)
        # resolve_source_state reads the canonical ref after the window check;
        # a missing one raises here exactly as it would there.
        record = (publication["kind"], publication["record_id"])
        if record not in self._canonical:
            self._canonical[record] = current_source_ref(self.con, *record)
        # resolve_source_state then builds its full view: the same reads of
        # the bounds, in the same order, fail the same way here.
        original = bounds["original"]
        original[0], original[1]
        effective = bounds["effective"]
        effective[0], effective[1]
        selected[0], selected[1]
        bounds["basis"]
        text = _pinned_version_interval_text(
            self.con, publication["projection"]["transcript_version_id"],
            selected[0], selected[1], transcripts=self.transcripts)
        body = publication["body"]
        provenance = body.get("provenance")
        episode = body["episode"]
        return {"relationship": publication["relationship"], "text": text,
                # The window the text joins (resolve_source_state's
                # selected_bounds) and its pinned transcript.
                "selected_bounds": {"start_s": selected[0], "end_s": selected[1]},
                "transcript_version_id": publication["projection"]["transcript_version_id"],
                "provenance": dict(provenance) if provenance is not None else None,
                "episode": {"episode_id": episode["episode_id"],
                            "public": dict(episode["public"])}}


def _pinned_version_interval_text(con: sqlite3.Connection,
                                   transcript_version_id,
                                   start, end, *, transcripts: dict | None = None) -> str | None:
    """Overlapping-segment text from the pinned transcript payload.

    Records the overlapping-segment basis, never exact word/audio timing.
    Returns None when the pinned payload is unretained. transcripts, when
    given, is a per-pass memo: each pinned version is fetched and decoded
    once for that pass.
    """
    if transcript_version_id is None:
        return None
    pinned = pinned_transcript(con, transcript_version_id, transcripts)
    if pinned is None:
        return None
    if start is None or end is None:
        return None
    return pinned.text(start, end)


def pinned_transcript(con, transcript_version_id, transcripts: dict | None = None):
    """The decoded pinned transcript of one version, or None when its payload
    is missing or fails JSON decoding (JSONDecodeError/TypeError). Any other
    decode error (e.g. ValueError for an over-long integer, UnicodeDecodeError
    for undecodable bytes) propagates and is never kept, so each caller
    applies its own rule to it. transcripts is an optional per-pass memo
    keyed by version id (an immutable identity); without it every call
    decodes."""
    if transcripts is not None and transcript_version_id in transcripts:
        return transcripts[transcript_version_id]
    pinned = _load_pinned_transcript(con, transcript_version_id)
    if transcripts is not None:
        transcripts[transcript_version_id] = pinned
    return pinned


def _load_pinned_transcript(con, transcript_version_id):
    row = con.execute("SELECT payload FROM source_versions WHERE id=?",
                      (transcript_version_id,)).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"])
    except (json.JSONDecodeError, TypeError):
        return None
    return _PinnedTranscript(payload.get("segments", []) if isinstance(payload, dict) else [])


def _plain_number(value) -> bool:
    return type(value) in (int, float) and value == value  # not NaN


class _PinnedTranscript:
    """One decoded pinned transcript and its overlap index.

    text(start, end) is the space-joined text of the dict segments with
    start < end and end > start, in stored order. A transcript asked for
    more than a few text windows, or for any safe excerpt window
    (overlapping), gets an index, when every dict segment carries
    plain numeric (non-NaN) bounds and string-or-absent text: segments
    sorted by start, plus the running maximum of their ends, bound the
    candidates exactly, and the stored-order filter then applies to those
    only. Every other case takes the plain scan, with its exact behaviour.
    """

    __slots__ = ("segments", "_scans", "_starts", "_max_ends", "_order")

    # Text windows answered by the plain scan before the index is built.
    # overlapping() builds it on its first window: rule pa1's plain scan
    # (safe_excerpt_from_segments) costs more than the build (NOGAP-PF-FIX2:
    # 8,685 such scans were about three quarters of a Plan A search on the
    # DET copy).
    INDEX_AFTER = 8

    def __init__(self, segments):
        self.segments = segments
        self._scans = 0
        self._order = None

    def _scan(self, start, end) -> str:
        return " ".join(s.get("text", "") for s in self.segments
                        if isinstance(s, dict)
                        and s.get("start", float("inf")) < end
                        and s.get("end", float("-inf")) > start)

    def _build_index(self) -> bool:
        """Index the segments; False when they do not qualify."""
        if not isinstance(self.segments, list):
            return False
        entries = []
        for position, seg in enumerate(self.segments):
            if not isinstance(seg, dict):
                continue
            if "start" not in seg or "end" not in seg:
                return False
            seg_start, seg_end = seg["start"], seg["end"]
            text = seg.get("text", "")
            if not (_plain_number(seg_start) and _plain_number(seg_end)) or not isinstance(text, str):
                return False
            entries.append((seg_start, position, seg_end, text))
        entries.sort(key=lambda entry: (entry[0], entry[1]))
        max_ends, running = [], None
        for entry in entries:
            running = entry[2] if running is None or entry[2] > running else running
            max_ends.append(running)
        self._starts = [entry[0] for entry in entries]
        self._max_ends = max_ends
        self._order = entries
        return True

    def overlapping(self, start, end):
        """The (start, position, end, text) entries of the dict segments
        that overlap [start, end] (start < end and end > start), through
        the index (built on the first call); None when the plain scan must
        answer: the segments do not qualify. start and end must be plain
        numbers."""
        if self._order is None:
            if self._scans is None:
                return None
            if not self._build_index():
                self._scans = None  # never qualifies: always the plain scan
                return None
        import bisect
        upper = bisect.bisect_left(self._starts, end)
        lower = bisect.bisect_right(self._max_ends, start, 0, upper)
        return [entry for entry in self._order[lower:upper] if entry[2] > start]

    def text(self, start, end) -> str:
        if self._order is None:
            if self._scans is None or self._scans < self.INDEX_AFTER \
                    or not (_plain_number(start) and _plain_number(end)):
                if self._scans is not None:
                    self._scans += 1
                return self._scan(start, end)
            if not self._build_index():
                self._scans = None  # never qualifies: always the plain scan
                return self._scan(start, end)
        if not (_plain_number(start) and _plain_number(end)):
            return self._scan(start, end)
        import bisect
        # Segments whose start is < end form a prefix of the start order;
        # before the first running maximum end > start, no end exceeds start.
        upper = bisect.bisect_left(self._starts, end)
        lower = bisect.bisect_right(self._max_ends, start, 0, upper)
        hits = sorted((position, text) for _s, position, seg_end, text
                      in self._order[lower:upper] if seg_end > start)
        return " ".join(text for _position, text in hits)


def _component(con, table, key, identity, revision_key, revision):
    row = con.execute(f"SELECT * FROM {table} WHERE {key}=? AND {revision_key}=?",
                      (identity, revision)).fetchone()
    if row is None:
        raise RevisionUnavailable(f"pinned {table} component is unretained")
    return dict(row)


def _resolve_pinned_evidence(con, evidence_id, pins, ctx=None, media=True):
    ev = _component(con, "evidence_history", "evidence_id", evidence_id,
                    "evidence_revision", pins["evidence_revision"])
    if ev["source_version_id"] != pins["source_version_id"] or \
            ev["transcript_version_id"] != pins["transcript_version_id"] or \
            ev["episode_id"] != pins["episode_id"]:
        raise RevisionUnavailable("publication version ownership differs")
    review = _component(con, "review_history", "evidence_id", evidence_id,
                        "review_revision", pins["review_revision"])
    assoc = _component(con, "association_history", "evidence_id", evidence_id,
                       "association_revision", pins["assoc_revision"])
    ep = _component(con, "episode_history", "episode_id", pins["episode_id"],
                    "episode_revision", pins["episode_revision"])
    return {"pins": pins, "evidence": _history_evidence(ev),
            "review": {k: review[k] for k in ("judgment", "bounds_verified")},
            # PODS: model rows are display-only
            "assocs": [{k: a.get(k) for k in ("brand", "basis", "origin", "channel")}
                       for a in json.loads(assoc["assocs_json"])
                       if a.get("basis") != "model"],
            "provenance": _version_provenance(con, pins["source_version_id"]),
            "episode": {"episode_id": pins["episode_id"], "public": _public_episode(ep)},
            "observation": _pinned_observation(con, pins),
            "media": _pinned_media_descriptor(con, ep, pins, ctx) if media
            else {"playback": "unchecked"}}



def _pinned_observation(con, pins):
    revision_id = pins.get("observation_revision_id")
    if revision_id is None:
        return None
    row = con.execute("SELECT observation_id,revision,values_json,source_pins"
                      " FROM observation_revisions WHERE id=?", (revision_id,)).fetchone()
    if row is None:
        raise RevisionUnavailable("pinned observation history is unretained")
    recorded = json.loads(row["values_json"])
    source = json.loads(row["source_pins"])
    if source.get("source_version_id") != pins["source_version_id"] or \
            source.get("transcript_version_id") != pins["transcript_version_id"]:
        raise RevisionUnavailable("pinned observation version ownership differs")
    confirmations = {}
    for identity in pins.get("confirmation_ids", []):
        confirmation = con.execute("SELECT * FROM field_confirmations WHERE id=?", (identity,)).fetchone()
        if confirmation is None or confirmation["observation_id"] != row["observation_id"] or \
                confirmation["source_version_id"] != pins["source_version_id"] or \
                confirmation["transcript_version_id"] != source.get("pipeline_transcript_version_id"):
            raise RevisionUnavailable("pinned confirmation ownership differs")
        confirmations[confirmation["field"]] = {key: confirmation[key] for key in (
            "id", "value", "source_version_id", "transcript_version_id",
            "segment_ref", "char_start", "char_end", "audio_ref", "actor")}
    return {"observation_id": row["observation_id"], "revision": row["revision"],
            "status": recorded.get("status"), "fields": recorded.get("fields") or {},
            "attributions": recorded.get("attributions") or [],
            "confirmations": confirmations}

def transcript_facts(con, transcript_version_id) -> dict | None:
    """Length and fetch day of one stored transcript version, never its
    text: duration_s is its last segment's end; fetch_date is the payload's
    fetch_date, else the day it was transcribed, else None, and
    fetch_date_basis says which ("fetch_date" / "transcribed_at" / None).
    None when the version or its payload is unretained."""
    if transcript_version_id is None:
        return None
    row = con.execute(
        "SELECT json_extract(payload, '$.segments[#-1].end'), json_extract(payload, '$.fetch_date'),"
        " json_extract(payload, '$.transcribed_at') FROM source_versions"
        " WHERE id=? AND json_valid(payload)", (transcript_version_id,)).fetchone()
    if row is None:
        return None
    end, fetched, transcribed = row
    basis = "fetch_date" if isinstance(fetched, str) and fetched else \
        "transcribed_at" if isinstance(transcribed, str) and transcribed else None
    day = fetched if basis == "fetch_date" else transcribed[:10] if basis else None
    return {"duration_s": float(end) if isinstance(end, (int, float)) else None,
            "fetch_date": day, "fetch_date_basis": basis}


def _public_episode(ep):
    return {k: ep.get(k) for k in ("show_title", "ep_title", "category", "pub_date",
                                  "fetch_date", "audio_url", "media_id", "alignment",
                                  "kind", "alias_of", "duration_min", "media_artifact_id")}


def _resolve_pinned_episode(con, episode_id, pins, ctx=None, media=True):
    ep = _component(con, "episode_history", "episode_id", episode_id,
                    "episode_revision", pins["episode_revision"])
    if ep["transcript_version_id"] != pins["transcript_version_id"]:
        raise RevisionUnavailable("publication transcript ownership differs")
    return {"pins": pins, "review": None, "assocs": [],
            "provenance": _version_provenance(con, pins["transcript_version_id"]),
            "episode": {"episode_id": episode_id, "public": _public_episode(ep)},
            "media": _pinned_media_descriptor(con, ep, pins, ctx) if media
            else {"playback": "unchecked"}}


# A per-pass memo of _version_provenance (provenance_memo): created_at sits
# after the payload in source_versions, so each lookup walks the payload's
# overflow pages; a pass over every publication (source_qualification) asks
# for the same few versions tens of thousands of times. It holds
# _version_provenance rows under the version id and _version_source_key
# results under ("source_key", id); the tuple keys never collide with ids.
_PROVENANCE_MEMO: contextvars.ContextVar = contextvars.ContextVar(
    "ew_version_provenance_memo", default=None)


@contextlib.contextmanager
def provenance_memo():
    """Memo _version_provenance and _version_source_key for the duration
    of one read pass (one snapshot: the rows it reads are immutable within
    it). Entries are keyed by version id only, so one block reads one
    connection: a second connection (another database) inside it would
    share them (ALS-R4-01)."""
    token = _PROVENANCE_MEMO.set({})
    try:
        yield
    finally:
        _PROVENANCE_MEMO.reset(token)


def _version_provenance(con: sqlite3.Connection,
                        version_id) -> dict | None:
    if version_id is None:
        return None
    memo = _PROVENANCE_MEMO.get()
    if memo is not None and version_id in memo:
        hit = memo[version_id]
        return dict(hit) if hit is not None else None
    row = con.execute("SELECT id, source_key, version_no, sha256,"
                      " created_at FROM source_versions WHERE id=?",
                      (version_id,)).fetchone()
    out = dict(row) if row else None
    if memo is not None:
        memo[version_id] = out
    return dict(out) if out is not None else None


def _version_source_key(con: sqlite3.Connection, version_id) -> str | None:
    """The source_key of one version, or None when it is unretained. Reads
    only columns stored before the payload, so it never walks the payload's
    overflow pages (the Alerts list asks for every cited version); memoized
    under provenance_memo like _version_provenance.

    Existence and source_key only: it must stay equivalent to
    _version_provenance for policy lookups (None exactly when that returns
    None, else its source_key)."""
    if version_id is None:
        return None
    memo = _PROVENANCE_MEMO.get()
    key = ("source_key", version_id)
    if memo is not None:
        if version_id in memo:
            hit = memo[version_id]
            return hit["source_key"] if hit is not None else None
        if key in memo:
            return memo[key]
    row = con.execute("SELECT source_key FROM source_versions WHERE id=?",
                      (version_id,)).fetchone()
    out = row[0] if row else None
    if memo is not None:
        memo[key] = out
    return out


def _excerpt_policy_version(kind: str, pins: dict):
    """Source version whose policy governs a publication's excerpt: an
    evidence pin's source version, or for manual evidence (none) the
    transcript version it was cut from, as evidence_source_provenance."""
    if kind == "evidence" and pins.get("source_version_id") is None:
        return pins.get("transcript_version_id")
    return pins.get("source_version_id")


def evidence_source_provenance(con: sqlite3.Connection, ev) -> dict | None:
    """Policy source of one evidence row. Manual evidence has no source
    version: the transcript version it was cut from stands in. Never
    backfilled, so publication pins and digests stay unchanged."""
    return _version_provenance(con, ev["source_version_id"]
                               if ev["source_version_id"] is not None
                               else ev["transcript_version_id"])


def _pinned_media_descriptor(con: sqlite3.Connection, ep: dict, pins: dict, ctx=None) -> dict:
    """Media identity comes from the same retained episode component. A
    context the audio policy refuses (audio clips design 5.2) gets an
    unavailable descriptor before any recording proof or file work; the
    text stays as its own permissions allow."""
    from . import media as _media
    from .access import AuthorizationError
    ep = dict(ep, id=ep.get("episode_id") or ep.get("id"))
    if _media.audio_gated(ctx):
        try:
            ctx.authorize_audio(con, ep["id"])
        except AuthorizationError:
            return {"playback": "unavailable", "reason": "audio not available",
                    "alignment": "unknown", "timing": "unknown"}
    proof = _media.recording_proof(con, ep, pins.get("recording"))
    if proof is not None:
        if proof["alignment"] == "mismatch":
            return {"playback": "unavailable", "reason": "recording alignment mismatch",
                    "alignment": "mismatch", "timing": "unknown"}
        verified = proof["alignment"] == "verified" and proof["verified_duration_s"] is not None
        return {"playback": "local", "recording_id": proof["artifact_id"],
                "recording_digest": proof["digest"], "duration_s": proof["verified_duration_s"],
                "source_offset_s": proof["source_offset_s"],
                "recording_bounds": {"start_s": proof["source_offset_s"],
                    "end_s": proof["source_offset_s"] + proof["verified_duration_s"]
                    if proof["verified_duration_s"] is not None else None},
                "label": "Pinned local recording",
                "alignment": "verified" if verified else proof["alignment"],
                "timing": "verified" if verified else "unknown"}
    descriptor = _media.media_for_episode(ep, repo=None)
    if descriptor is None:
        return {"playback": "unavailable",
                "reason": "no authorized recording proof",
                "alignment": ep.get("alignment") or "unknown",
                "timing": "unknown"}
    if descriptor.get("kind") == "remote_full":
        return {"playback": "remote",
                "src": descriptor.get("src"),
                "label": descriptor.get("label"),
                "alignment": ep.get("alignment") or "unknown",
                "timing": "unknown"}
    return {"playback": "unavailable", "reason": "registered media lacks pinned recording proof",
            "alignment": ep.get("alignment") or "unknown", "timing": "unknown"}


def share_scoped_citation(con: sqlite3.Connection, share_id: str,
                          citation: dict, allowed_fields: set) -> dict:
    """Apply a share grant's field allowlist to a citation.

    The share view, nested citations, and downloads all pass through here so
    a grant cannot leak fields outside its audience/object/version/fields
    scope. The grant must be active: unknown, expired, or revoked grants
    raise CitationError.
    """
    from . import collaboration as _collaboration
    from . import ops as _ops
    try:
        grant = _collaboration.share_state(con, share_id)
    except _ops.BadInput as exc:
        raise CitationError(f"unknown share: {share_id!r}") from exc
    if grant["state"] != "active":
        raise CitationError(f"share grant is {grant['state']}")
    return {k: v for k, v in citation.items() if k in allowed_fields}


def captured_disclosure(con, ref, ctx=None, *, export=False):
    """Retain policy locators, never copied policy or current source aliases.

    At excerpt level (Plan A, design 4) the locators name both contributing
    sources with [metadata, excerpt], the rule `pa1` and the excerpt bounds:
    what a capture made from the excerpt projection holds. Otherwise the
    locators are today's (transcript on the transcript source)."""
    if not ref.startswith("sr1:"):
        ref = qualify_legacy_source_ref(con, ref)
    state_id, _window = _parse_source_ref(ref)
    row = con.execute("SELECT kind, pins_json, bounds_json FROM source_states WHERE id=?",
                      (state_id,)).fetchone()
    if row is None:
        raise RevisionUnavailable("capture publication is unretained")
    if ctx is None:
        ctx = _owner_context()
    if hasattr(ctx, "authorize_disclosure") and not _sparse_view(ctx) and \
            disclosure_level(con, row["kind"], json.loads(row["pins_json"]), ctx,
                             export=export) == "excerpt":
        disclosures = pa1_disclosure(con, row["kind"], json.loads(row["pins_json"]),
                                     json.loads(row["bounds_json"]))
        authorize_retained(con, {"disclosure": disclosures}, ctx, export=export)
        return disclosures
    return full_disclosure(con, ref, ctx, export=export)


def pa1_disclosure(con, kind: str, pins: dict, bounds: dict) -> list:
    """The locators of a capture made at excerpt level: both sources, each
    [metadata, excerpt] with rule pa1 and the bounds of the safe excerpt
    the capture holds (None when it holds none). An episode publication
    holds metadata only. With no pinned transcript the episode's metadata
    stands for T, and the capture holds no excerpt."""
    t_version = pins.get("transcript_version_id")
    if t_version is None:
        episode = {"source_key": f"episode:{pins['episode_id']}", "fields": ["metadata"]}
        e_version = _excerpt_policy_version(kind, pins) if kind == "evidence" else None
        if e_version is None:
            return [episode]
        return [episode, {"source_key": _policy_source(con, e_version),
                          "fields": ["metadata", "excerpt"], "rule": PA1, "bounds": None}]
    t_source = _policy_source(con, t_version)
    if kind != "evidence":
        return [{"source_key": t_source, "fields": ["metadata"]}]
    effective = bounds["effective"]
    safe = safe_excerpt(con, t_version, effective[0], effective[1])
    span = [safe["start_s"], safe["end_s"]] if safe else None
    return [{"source_key": key, "fields": ["metadata", "excerpt"], "rule": PA1, "bounds": span}
            for key in (_policy_source(con, _excerpt_policy_version(kind, pins)), t_source)]


def full_disclosure(con, ref, ctx=None, *, export=False):
    """Today's locators of a full capture (the transcript category on the
    transcript source), authorized: a transcript capture still needs
    transcript rights."""
    if not ref.startswith("sr1:"):
        ref = qualify_legacy_source_ref(con, ref)
    state_id, _window = _parse_source_ref(ref)
    row = con.execute("SELECT kind, pins_json FROM source_states WHERE id=?", (state_id,)).fetchone()
    if row is None:
        raise RevisionUnavailable("capture publication is unretained")
    pins = json.loads(row["pins_json"])
    disclosures = []
    if pins.get("transcript_version_id") is None:
        disclosures.append({"source_key": f"episode:{pins['episode_id']}", "fields": ["metadata"]})
    for version, fields in ((_excerpt_policy_version(row["kind"], pins), ["metadata", "excerpt"]),
                            (pins.get("transcript_version_id"), ["metadata", "transcript"])):
        if version is None:
            continue
        source_key = _version_source_key(con, version)
        if source_key is None:
            raise RevisionUnavailable("capture policy identity is unretained")
        disclosures.append({"source_key": source_key, "fields": fields})
    authorize_retained(con, {"disclosure": disclosures}, ctx, export=export)
    return disclosures


def authorize_retained(con, captured, ctx=None, *, export=False):
    from . import access
    if ctx is None:
        ctx = access.AccessContext.create(principal_id="local-owner", principal_kind="owner",
                                         workspace_id="local", credential_kind="local_operator")
    disclosures = captured.get("disclosure")
    if not isinstance(disclosures, list):
        raise RevisionUnavailable("retained disclosure provenance is unqualified")
    for locator in disclosures:
        ctx.authorize_disclosure(con, locator["source_key"], locator["fields"], export=export)
        # Old artifacts (design 6): where the transcript is a policy denial,
        # retained excerpt text is served only when the pa1 rule made it.
        if "excerpt" in locator["fields"] and locator.get("rule") != PA1 \
                and transcript_policy_denied(con, locator["source_key"]):
            raise access.AuthorizationError(PLAN_REFUSAL, 403)


def _old_artifact(con, locator) -> bool:
    """A text locator made before excerpt level (design 6): excerpt or
    transcript fields, no pa1 rule, on a source whose transcript is a
    policy denial."""
    return bool({"excerpt", "transcript"} & set(locator.get("fields", ()))) \
        and locator.get("rule") != PA1 and transcript_policy_denied(con, locator["source_key"])


def plan_refusal(con, disclosures: list, ctx=None, *, export=False) -> bool:
    """Whether these retained locators are refused only because they are
    old artifacts: refused as they are, allowed once each old-artifact
    locator is read as Plan A would hold it (no transcript field, the pa1
    rule). Every other right on that source still counts. Decided from the
    record, never the refusal text. Records nothing on ctx."""
    def check(locators):
        return lambda probe: authorize_retained(con, {"disclosure": locators}, probe, export=export)
    kept = [{**loc, "fields": [f for f in loc["fields"] if f != "transcript"], "rule": PA1}
            if _old_artifact(con, loc) else loc for loc in disclosures]
    if kept == disclosures or export_permitted(ctx, check(disclosures)):
        return False
    return export_permitted(ctx, check(kept))


def withheld_reason(con, exc, disclosures: list, ctx=None) -> str | None:
    """Why a listing withholds an artifact whose read refused with exc,
    each artifact counted once (UX DENY-WORDS (3)): "plan" when the plan
    refuses it (plan_refusal), or when a deny or a grant refuses it and
    one of its locators is an old artifact, which the plan refuses either
    way (DPC-04/05); else "rights" for a source's own deny, "access" for
    a grant's source list, None for any other refusal. Records nothing."""
    from .access import DisclosureRefused
    if plan_refusal(con, disclosures, ctx):
        return "plan"
    if not isinstance(exc, DisclosureRefused):
        return None
    if any(isinstance(loc, dict) and _old_artifact(con, loc) for loc in disclosures):
        return "plan"
    return "rights" if exc.own_policy else "access"


def export_permitted(ctx, check) -> bool:
    """Whether check(ctx) passes the export-permission recheck, for a
    listing that only shows the answer. The probe's disclosures are not
    kept on ctx: the listing stays a read for later rechecks and audit
    receipts (review R2-01)."""
    from . import access
    kept = None if ctx is None else (ctx.disclosures, ctx.recordings)
    if kept is not None:
        ctx.disclosures, ctx.recordings = set(), {}
    try:
        check(ctx)
        return True
    except access.AuthorizationError:
        return False
    finally:
        if kept is not None:
            ctx.disclosures, ctx.recordings = kept


def resource_disclosure(kind, record_id):
    """Stable retained policy identity for a workspace research resource."""
    category = {"campaign": "campaign", "brief": "campaign",
                "watchlist": "watchlist"}[kind]
    return {"source_key": f"{kind}:{record_id}", "fields": [category]}


def authorize_resource(con, kind, record_id, ctx=None, *, export=False):
    locator = resource_disclosure(kind, record_id)
    authorize_retained(con, {"disclosure": [locator]}, ctx, export=export)
    return locator


def resource_allowed(con, kind, record_id, ctx=None):
    from .access import AuthorizationError
    try:
        authorize_resource(con, kind, record_id, ctx)
    except AuthorizationError:
        return False
    return True


def capture_digest(captured):
    import hashlib
    content = {k: v for k, v in captured.items() if k != "capture_digest"}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_capture(con, captured, ctx=None, *, export=False):
    """Integrity plus disclosure recheck of a retained capture. export=True
    when its bytes are being released (download), so the current export
    permission applies, not only read permission."""
    if not captured.get("capture_digest") or captured["capture_digest"] != capture_digest(captured):
        raise RevisionUnavailable("retained capture digest is unqualified or corrupt")
    authorize_retained(con, captured, ctx, export=export)
    # Stored locators may predate a policy rule (a manual evidence capture
    # retained no excerpt locator): an sr1 evidence capture also passes the
    # live check. Nothing stored changes, so no digest changes.
    ref = captured.get("source_ref") or captured.get("ref")
    if captured.get("source_kind", captured.get("kind")) == "evidence" \
            and isinstance(ref, str) and ref.startswith("sr1:"):
        # The live check asks for the category the capture holds: a pa1
        # excerpt capture needs excerpt rights on both sources only.
        if any(isinstance(loc, dict) and loc.get("rule") == PA1
               for loc in captured.get("disclosure") or []):
            state_id, _window = _parse_source_ref(ref)
            row = con.execute("SELECT kind, pins_json, bounds_json FROM source_states WHERE id=?",
                              (state_id,)).fetchone()
            if row is None:
                raise RevisionUnavailable("capture publication is unretained")
            authorize_retained(con, {"disclosure": pa1_disclosure(
                con, row["kind"], json.loads(row["pins_json"]), json.loads(row["bounds_json"]))},
                ctx, export=export)
        else:
            full_disclosure(con, ref, ctx, export=export)
