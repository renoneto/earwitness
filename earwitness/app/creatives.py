"""P06 recurring creatives and occurrences (workspace-scoped, reversible).

Owned by P05P06. Exact decoded-interval PCM hashes plus normalized transcript
token fingerprints; near-match retrieval is token-shingle + duration-band with
lexical + commercial-field diffs, config stored, always suggestions until
reviewed. No new fingerprint dependency. Confirm/reject/split/merge are
revision-checked with reversible lineage; confirming membership never copies
ad/boundary/alignment/campaign judgments. Rejected pairs are keyed to compared
versions so reprocessing does not re-suggest unchanged pairs.

Occurrences follow plan section 3: separately identified placements with
revisioned observation links; same-placement refetch preserves mapping; two
real repetitions are separate IDs; unresolved changed-DAI correspondence is
ambiguous (no fabricated exact placement total).
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import struct
import uuid

from . import db as _db
from . import ops as _ops

CREATIVE_STATUSES = ("suggested", "confirmed", "split", "merged", "rejected")
CORRESPONDENCE = ("confirmed", "ambiguous", "superseded")

PCM_CONFIG = {"target_rate": 16000, "channels": 1, "bits": 16}
TEXT_CONFIG_VERSION = "text-fp-v1"
SHINGLE_K = 5

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[.'/-][a-z0-9]+)*", re.I)
_DOMAIN_RE = re.compile(r"(?:[\w-]+\.)+(?:com|net|org|io|co|fm)(?:/[\w\-%.~]*)?", re.I)
_CODE_RE = re.compile(r"\b[A-Z0-9][A-Z0-9-]{2,19}\b")
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?%?\b")
_OFFER_RE = re.compile(r"\b\d+\s?(?:%|percent)\s?off\b|\bfree\b|\btrial\b", re.I)


def _now() -> str:
    return _db.utcnow()


def _new_id(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4().hex[:12]}"


def _require_member(ctx, operation: str) -> None:
    if ctx is not None:
        ctx.require_member(operation)
        from .access import require_customer_mutation
        require_customer_mutation(ctx, operation)


def setup_aux_schema(con: sqlite3.Connection) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS creative_fingerprints ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " workspace_id TEXT NOT NULL DEFAULT 'local',"
                " creative_id TEXT, observation_id TEXT, kind TEXT NOT NULL,"
                " value TEXT NOT NULL DEFAULT '', config TEXT NOT NULL DEFAULT '{}',"
                " created_at TEXT NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS creative_match_configs ("
                " digest TEXT PRIMARY KEY, config TEXT NOT NULL DEFAULT '{}',"
                " created_at TEXT NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS rejected_pairs ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " workspace_id TEXT NOT NULL DEFAULT 'local',"
                " observation_a TEXT NOT NULL, observation_b TEXT NOT NULL,"
                " revision_a INTEGER NOT NULL, revision_b INTEGER NOT NULL,"
                " config_digest TEXT NOT NULL DEFAULT '',"
                " reviewer TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,"
                " UNIQUE (observation_a, observation_b, revision_a, revision_b,"
                " config_digest))")
    # The default suggestion config is recorded here, at setup: the
    # suggestion read never writes.
    con.execute("INSERT OR IGNORE INTO creative_match_configs (digest, config, created_at)"
                " VALUES (?, ?, ?)", (match_config_digest(DEFAULT_MATCH_CONFIG),
                                      json.dumps(DEFAULT_MATCH_CONFIG), _now()))


def _ensure_aux(con: sqlite3.Connection) -> None:
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"creative_fingerprints", "creative_match_configs", "rejected_pairs"} <= tables:
        raise _ops.BadInput("creative schema requires explicit setup")


# -- fingerprints ----------------------------------------------------------

def pcm_hash(samples, sample_rate: int, channels: int,
             config: dict | None = None) -> dict:
    """Exact decoded-interval PCM hash with fixed rate/channel conversion.

    Narrow positive identity signal only: no robust-matching claim across
    re-encoding, gain changes, or shifted boundaries. Config is stored with
    the hash so comparisons are auditable.
    """
    cfg = dict(PCM_CONFIG)
    if config:
        cfg.update({k: config[k] for k in PCM_CONFIG if k in config})
    if sample_rate <= 0 or channels <= 0:
        raise _ops.BadInput("sample_rate and channels must be positive")
    vals = [float(v) for v in (samples or [])]
    if not vals:
        raise _ops.BadInput("pcm samples are required for an exact hash")
    if channels > 1:
        mono = [sum(vals[i:i + channels]) / channels
                for i in range(0, len(vals) - channels + 1, channels)]
    else:
        mono = list(vals)
    target = int(cfg["target_rate"])
    if int(sample_rate) != target and len(mono) > 1:
        ratio = target / float(sample_rate)
        new_len = max(1, int(len(mono) * ratio))
        resampled = []
        for i in range(new_len):
            pos = i / ratio
            lo = int(pos)
            hi = min(len(mono) - 1, lo + 1)
            frac = pos - lo
            resampled.append(mono[lo] * (1 - frac) + mono[hi] * frac)
        mono = resampled
    peak = max(1e-9, max(abs(v) for v in mono))
    quantized = [int(max(-32768, min(32767, round(v / peak * 32767)))) for v in mono]
    blob = struct.pack(f"<{len(quantized)}h", *quantized)
    digest = hashlib.sha256(blob).hexdigest()
    return {"sha256": digest, "samples": len(quantized), "config": cfg}


def text_fingerprint(transcript_text: str) -> dict:
    """Normalized transcript token fingerprint.

    Normalization removes punctuation/case for retrieval but preserves
    numbers, codes, domains, and offer terms for the variant comparison.
    Exact text alone is not exact audio.
    """
    text = str(transcript_text or "")
    lowered = text.lower()
    spoken_norm = re.sub(r"\s+dot\s+", ".", lowered)
    spoken_norm = re.sub(r"\s+slash\s+", "/", spoken_norm)
    tokens = _TOKEN_RE.findall(lowered)
    domains = sorted(set(_DOMAIN_RE.findall(spoken_norm)))
    codes = sorted(set(c.upper() for c in _CODE_RE.findall(text)))
    numbers = sorted(set(_NUMBER_RE.findall(lowered)))
    offers = sorted(set(m.lower() for m in _OFFER_RE.findall(lowered)))
    shingles = set()
    for i in range(max(0, len(tokens) - SHINGLE_K + 1)):
        shingles.add(" ".join(tokens[i:i + SHINGLE_K]))
    fp_src = "|".join(sorted(shingles)) if shingles else "|".join(tokens)
    digest = hashlib.sha256(fp_src.encode()).hexdigest() if fp_src else ""
    return {"tokens": tokens, "token_count": len(tokens),
            "shingles": sorted(shingles), "shingle_hash": digest,
            "numbers": numbers, "codes": codes, "domains": domains,
            "offers": offers, "config_version": TEXT_CONFIG_VERSION}


def _duration_of(con: sqlite3.Connection, observation_id: str) -> float | None:
    row = con.execute("SELECT start_s, end_s FROM ad_observations WHERE id=?",
                      (observation_id,)).fetchone()
    if row is None or row["start_s"] is None or row["end_s"] is None:
        return None
    return float(row["end_s"]) - float(row["start_s"])


DEFAULT_MATCH_CONFIG = {"duration_band": 0.2, "min_shingle_overlap": 0.05}


def match_config_digest(config: dict) -> str:
    src = json.dumps(config or {}, sort_keys=True)
    return hashlib.sha256(src.encode()).hexdigest()[:16]


@_db.atomic
def fingerprint_observation(con: sqlite3.Connection, observation_id: str,
                            transcript_text: str | None = None,
                            pcm_samples=None, sample_rate: int = 16000,
                            channels: int = 1, actor: str = "pipeline",
                            ctx=None) -> dict:
    """Store exact PCM and/or text fingerprints for an observation."""
    _require_member(ctx, "match_creatives")
    _ensure_aux(con)
    if con.execute("SELECT id FROM ad_observations WHERE id=?",
                   (observation_id,)).fetchone() is None:
        raise _ops.BadInput("unknown observation")
    out: dict = {"observation_id": observation_id}
    with _db.transaction(con, write=True):
        if transcript_text is not None:
            fp = text_fingerprint(transcript_text)
            con.execute("INSERT INTO creative_fingerprints (workspace_id, observation_id,"
                        " kind, value, config, created_at)"
                        " VALUES ('local', ?, 'text', ?, ?, ?)",
                        (observation_id, json.dumps(fp),
                         json.dumps({"version": TEXT_CONFIG_VERSION}), _now()))
            out["text"] = {"shingle_hash": fp["shingle_hash"],
                           "tokens": fp["token_count"]}
        if pcm_samples is not None:
            exact = pcm_hash(pcm_samples, sample_rate, channels)
            con.execute("INSERT INTO creative_fingerprints (workspace_id, observation_id,"
                        " kind, value, config, created_at)"
                        " VALUES ('local', ?, 'pcm', ?, ?, ?)",
                        (observation_id, exact["sha256"],
                         json.dumps(exact["config"]), _now()))
            out["pcm"] = {"sha256": exact["sha256"]}
        rev = _db.bump_revision(con)
        _db.audit(con, actor, "creative_fingerprint", "observation",
                  observation_id, out, rev)
    out["aggregate_rev"] = rev
    return out


def _observation_text_fp(con: sqlite3.Connection, observation_id: str) -> dict | None:
    _ensure_aux(con)
    row = con.execute("SELECT value FROM creative_fingerprints WHERE observation_id=?"
                      " AND kind='text' ORDER BY id DESC LIMIT 1",
                      (observation_id,)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return None


def _commercial_diff(query_fp: dict, cand_fp: dict) -> dict:
    diff = {}
    for key in ("codes", "domains", "offers", "numbers"):
        q, c = set(query_fp.get(key, [])), set(cand_fp.get(key, []))
        if q != c:
            diff[key] = {"query_only": sorted(q - c), "candidate_only": sorted(c - q)}
    return diff


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def suggest_memberships(con: sqlite3.Connection, observation_id: str,
                        config: dict | None = None, limit: int = 20,
                        ctx=None) -> dict:
    """Bounded near-match retrieval: shingle + duration band, then diff.

    No whole-corpus pairwise: candidates are pre-filtered by duration band
    before any lexical comparison. Results are ALWAYS suggestions
    (confirmed=0) until reviewed. Workspace-scoped only.
    """
    _require_member(ctx, "match_creatives")
    _ensure_aux(con)
    config = dict(config or DEFAULT_MATCH_CONFIG)
    band = float(config.get("duration_band", 0.2))
    min_overlap = float(config.get("min_shingle_overlap", 0.05))
    limit = max(1, min(int(limit), 50))
    qrow = con.execute("SELECT revision FROM ad_observations WHERE id=?",
                       (observation_id,)).fetchone()
    if qrow is None:
        raise _ops.BadInput("unknown observation")
    qfp = _observation_text_fp(con, observation_id)
    if qfp is None:
        raise _ops.BadInput("observation has no text fingerprint; fingerprint first")
    qdur = _duration_of(con, observation_id)
    digest = match_config_digest(config)
    qsh = set(qfp.get("shingles", []))
    rows = con.execute("SELECT id, start_s, end_s, revision FROM ad_observations"
                       " WHERE id!=?", (observation_id,)).fetchall()
    suggestions = []
    for r in rows:
        oid = r["id"]
        if _was_rejected(con, observation_id, qrow["revision"], oid,
                         r["revision"], digest):
            continue
        dur = None
        if r["start_s"] is not None and r["end_s"] is not None:
            dur = float(r["end_s"]) - float(r["start_s"])
        if qdur and dur:
            denom = max(qdur, dur)
            if denom and abs(qdur - dur) / denom > band + 1e-9:
                continue
        cfp = _observation_text_fp(con, oid)
        if cfp is None:
            continue
        overlap = _jaccard(qsh, set(cfp.get("shingles", [])))
        if overlap < min_overlap:
            continue
        tok_overlap = _jaccard(set(qfp.get("tokens", [])), set(cfp.get("tokens", [])))
        score = 0.5 * overlap + 0.5 * tok_overlap
        suggestions.append({"observation_id": oid, "observation_revision": r["revision"],
                            "score": round(score, 4), "shingle_overlap": round(overlap, 4),
                            "commercial_diff": _commercial_diff(qfp, cfp),
                            "method": "suggested", "confirmed": False,
                            "config_digest": digest})
    suggestions.sort(key=lambda s: -s["score"])
    return {"observation_id": observation_id, "config": config,
            "config_digest": digest, "suggestions": suggestions[:limit],
            "note": "suggestions only; review required before membership"}


def _was_rejected(con: sqlite3.Connection, a: str, rev_a: int, b: str,
                  rev_b: int, digest: str) -> bool:
    _ensure_aux(con)
    o1, o2 = sorted([a, b])
    r1, r2 = (rev_a, rev_b) if o1 == a else (rev_b, rev_a)
    return con.execute("SELECT id FROM rejected_pairs WHERE observation_a=?"
                       " AND observation_b=? AND revision_a=? AND revision_b=?"
                       " AND config_digest=?", (o1, o2, r1, r2, digest)).fetchone() is not None


@_db.atomic
def create_creative(con: sqlite3.Connection, observation_ids: list,
                    actor: str = "owner", status: str = "suggested",
                    ctx=None) -> dict:
    """Create a workspace-scoped creative with suggested (unconfirmed) members."""
    _require_member(ctx, "match_creatives")
    _ensure_aux(con)
    if status not in CREATIVE_STATUSES:
        raise _ops.BadInput("unknown creative status")
    if not observation_ids:
        raise _ops.BadInput("a creative requires at least one observation")
    for oid in observation_ids:
        if con.execute("SELECT id FROM ad_observations WHERE id=?", (oid,)).fetchone() is None:
            raise _ops.BadInput(f"unknown observation: {oid}")
    now = _now()
    with _db.transaction(con, write=True):
        cid = _new_id("cr")
        con.execute("INSERT INTO creatives (id, workspace_id, revision, status,"
                    " created_at, updated_at) VALUES (?, 'local', 1, ?, ?, ?)",
                    (cid, status, now, now))
        for oid in observation_ids:
            con.execute("INSERT OR IGNORE INTO creative_memberships (workspace_id,"
                        " creative_id, observation_id, method, confirmed, created_at)"
                        " VALUES ('local', ?, ?, 'suggested', 0, ?)", (cid, oid, now))
        rev = _db.bump_revision(con)
        _db.audit(con, actor, "creative_create", "creative", cid,
                  {"members": list(observation_ids), "status": status}, rev)
    return {"creative_id": cid, "revision": 1, "status": status,
            "aggregate_rev": rev}


@_db.atomic
def confirm_membership(con: sqlite3.Connection, creative_id: str,
                       observation_id: str, expected_revision,
                       reviewer: str = "owner", ctx=None) -> dict:
    """Confirm one membership. Never copies ad/boundary/campaign judgments."""
    _require_member(ctx, "match_creatives")
    if expected_revision is None:
        raise _ops.MissingPrecondition("expected revision is required")
    row = con.execute("SELECT revision FROM creatives WHERE id=?",
                      (creative_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown creative")
    if int(expected_revision) != row["revision"]:
        raise _ops.Conflict(row["revision"])
    mem = con.execute("SELECT id, confirmed FROM creative_memberships WHERE creative_id=?"
                      " AND observation_id=?", (creative_id, observation_id)).fetchone()
    if mem is None:
        raise _ops.BadInput("observation is not a suggested member of this creative")
    before_reviews = con.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    with _db.transaction(con, write=True):
        con.execute("UPDATE creative_memberships SET confirmed=1 WHERE id=?", (mem["id"],))
        con.execute("UPDATE creatives SET revision=revision+1, updated_at=? WHERE id=?",
                    (_now(), creative_id))
        rev = _db.bump_revision(con)
        _db.audit(con, reviewer, "creative_confirm", "creative", creative_id,
                  {"observation_id": observation_id,
                   "judgments_copied": False}, rev)
    after_reviews = con.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    assert after_reviews == before_reviews
    return {"creative_id": creative_id, "revision": row["revision"] + 1,
            "observation_id": observation_id, "confirmed": True,
            "judgments_copied": False, "aggregate_rev": rev}


@_db.atomic
def reject_membership(con: sqlite3.Connection, creative_id: str,
                      observation_id_a: str, observation_id_b: str,
                      expected_revision, reviewer: str = "owner",
                      config_digest: str = "", ctx=None) -> dict:
    """Reject a pair, keyed to compared versions (no re-suggestion)."""
    _require_member(ctx, "match_creatives")
    _ensure_aux(con)
    if expected_revision is None:
        raise _ops.MissingPrecondition("expected revision is required")
    row = con.execute("SELECT revision FROM creatives WHERE id=?",
                      (creative_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown creative")
    if int(expected_revision) != row["revision"]:
        raise _ops.Conflict(row["revision"])
    ra = con.execute("SELECT revision FROM ad_observations WHERE id=?",
                     (observation_id_a,)).fetchone()
    rb = con.execute("SELECT revision FROM ad_observations WHERE id=?",
                     (observation_id_b,)).fetchone()
    if ra is None or rb is None:
        raise _ops.BadInput("unknown observation in rejected pair")
    o1, o2 = sorted([observation_id_a, observation_id_b])
    r1, r2 = (ra["revision"], rb["revision"]) if o1 == observation_id_a else (
        rb["revision"], ra["revision"])
    now = _now()
    with _db.transaction(con, write=True):
        con.execute("INSERT OR IGNORE INTO rejected_pairs (workspace_id, observation_a,"
                    " observation_b, revision_a, revision_b, config_digest, reviewer,"
                    " created_at) VALUES ('local', ?, ?, ?, ?, ?, ?, ?)",
                    (o1, o2, r1, r2, config_digest, reviewer, now))
        con.execute("UPDATE creative_memberships SET confirmed=0 WHERE creative_id=?"
                    " AND observation_id IN (?, ?)", (creative_id, o1, o2))
        con.execute("UPDATE creatives SET revision=revision+1, updated_at=? WHERE id=?",
                    (now, creative_id))
        rev = _db.bump_revision(con)
        _db.audit(con, reviewer, "creative_reject", "creative", creative_id,
                  {"pair": [o1, o2], "revisions": [r1, r2],
                   "config_digest": config_digest}, rev)
    return {"creative_id": creative_id, "revision": row["revision"] + 1,
            "rejected_pair": [o1, o2], "aggregate_rev": rev}


@_db.atomic
def split_creative(con: sqlite3.Connection, creative_id: str, groups: list,
                   expected_revision, actor: str = "owner", ctx=None) -> dict:
    """Split a creative group with reversible lineage (audit-backed)."""
    _require_member(ctx, "match_creatives")
    if expected_revision is None:
        raise _ops.MissingPrecondition("expected revision is required")
    row = con.execute("SELECT revision, status FROM creatives WHERE id=?",
                      (creative_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown creative")
    if int(expected_revision) != row["revision"]:
        raise _ops.Conflict(row["revision"])
    members = [r["observation_id"] for r in con.execute(
        "SELECT observation_id FROM creative_memberships WHERE creative_id=?",
        (creative_id,)).fetchall()]
    flat = [o for g in groups for o in g]
    if sorted(flat) != sorted(members):
        raise _ops.BadInput("split groups must partition current members exactly")
    now = _now()
    with _db.transaction(con, write=True):
        new_ids = []
        for g in groups:
            cid = _new_id("cr")
            con.execute("INSERT INTO creatives (id, workspace_id, revision, status,"
                        " created_at, updated_at) VALUES (?, 'local', 1, 'suggested', ?, ?)",
                        (cid, now, now))
            for oid in g:
                conf = con.execute("SELECT confirmed FROM creative_memberships"
                                   " WHERE creative_id=? AND observation_id=?",
                                   (creative_id, oid)).fetchone()
                con.execute("INSERT INTO creative_memberships (workspace_id, creative_id,"
                            " observation_id, method, confirmed, created_at)"
                            " VALUES ('local', ?, ?, 'split', ?, ?)",
                            (cid, oid, conf["confirmed"] if conf else 0, now))
            new_ids.append(cid)
        con.execute("UPDATE creatives SET status='split', revision=revision+1,"
                    " updated_at=? WHERE id=?", (now, creative_id))
        rev = _db.bump_revision(con)
        _db.audit(con, actor, "creative_split", "creative", creative_id,
                  {"children": new_ids, "groups": groups,
                   "reversible_from": row["revision"]}, rev)
    return {"creative_id": creative_id, "children": new_ids,
            "revision": row["revision"] + 1, "aggregate_rev": rev}


@_db.atomic
def merge_creatives(con: sqlite3.Connection, creative_ids: list,
                    expected_revisions: dict, actor: str = "owner",
                    ctx=None) -> dict:
    """Merge creative groups with reversible lineage."""
    _require_member(ctx, "match_creatives")
    if expected_revisions is None:
        raise _ops.MissingPrecondition("expected revisions are required")
    members: list = []
    for cid in creative_ids:
        row = con.execute("SELECT revision FROM creatives WHERE id=?", (cid,)).fetchone()
        if row is None:
            raise _ops.BadInput(f"unknown creative: {cid}")
        exp = (expected_revisions or {}).get(cid)
        if exp is None:
            raise _ops.MissingPrecondition(f"expected revision required for {cid}")
        if int(exp) != row["revision"]:
            raise _ops.Conflict(row["revision"])
        members += [r["observation_id"] for r in con.execute(
            "SELECT observation_id FROM creative_memberships WHERE creative_id=?",
            (cid,)).fetchall()]
    now = _now()
    with _db.transaction(con, write=True):
        cid = _new_id("cr")
        con.execute("INSERT INTO creatives (id, workspace_id, revision, status,"
                    " created_at, updated_at) VALUES (?, 'local', 1, 'suggested', ?, ?)",
                    (cid, now, now))
        for oid in sorted(set(members)):
            con.execute("INSERT OR IGNORE INTO creative_memberships (workspace_id,"
                        " creative_id, observation_id, method, confirmed, created_at)"
                        " VALUES ('local', ?, ?, 'merged', 0, ?)", (cid, oid, now))
        for old in creative_ids:
            con.execute("UPDATE creatives SET status='merged', revision=revision+1,"
                        " updated_at=? WHERE id=?", (now, old))
        rev = _db.bump_revision(con)
        _db.audit(con, actor, "creative_merge", "creative", cid,
                  {"parents": list(creative_ids)}, rev)
    return {"creative_id": cid, "revision": 1, "parents": list(creative_ids),
            "aggregate_rev": rev}


def creative_service_payload(con: sqlite3.Connection, creative_id: str) -> dict:
    """Server-data plumbing for creative review: data only, native controls."""
    _ensure_aux(con)
    row = con.execute("SELECT * FROM creatives WHERE id=?", (creative_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown creative")
    members = [dict(r) for r in con.execute(
        "SELECT observation_id, method, score, confirmed FROM creative_memberships"
        " WHERE creative_id=? ORDER BY observation_id", (creative_id,)).fetchall()]
    history = [dict(r) for r in con.execute(
        "SELECT at, actor, action, detail, aggregate_rev FROM audit"
        " WHERE target_type='creative' AND target_id=? ORDER BY id", (creative_id,)).fetchall()]
    return {"creative": dict(row), "members": members, "record_kind": "creative",
            "history": history,
            "actions": [
                {"id": "confirm", "label": "Confirm membership",
                 "kind": "native-button", "method": "confirm"},
                {"id": "reject", "label": "Reject pair", "kind": "native-button",
                 "method": "reject"},
                {"id": "split", "label": "Split creative", "kind": "native-button",
                 "method": "split"},
                {"id": "merge", "label": "Merge creatives", "kind": "native-button",
                 "method": "merge"},
            ],
            "display": {"confirm_note": "confirming membership never copies"
                                        " ad/boundary/alignment/campaign judgments"}}


# -- occurrences -----------------------------------------------------------

def _link_basis(basis: str, observation_revision: int) -> str:
    return f"{basis}:obs_rev={int(observation_revision)}"


def _require_link(con: sqlite3.Connection, episode_id: str, observation_id: str,
                  observation_revision) -> None:
    """An occurrence links an existing revision of an observation of its
    own episode (AD-05). Raises BadInput before anything is written."""
    if con.execute("SELECT id FROM episodes WHERE id=?", (episode_id,)).fetchone() is None:
        raise _ops.BadInput("unknown episode")
    obs = con.execute("SELECT episode_id FROM ad_observations WHERE id=?",
                      (observation_id,)).fetchone()
    if obs is None:
        raise _ops.BadInput("unknown observation")
    if obs["episode_id"] != episode_id:
        raise _ops.BadInput("the observation belongs to another episode")
    if isinstance(observation_revision, bool) or not isinstance(observation_revision, int) \
            or con.execute("SELECT 1 FROM observation_revisions WHERE observation_id=?"
                           " AND revision=?", (observation_id, observation_revision)
                           ).fetchone() is None:
        raise _ops.BadInput("unknown observation revision")


def _require_current_revision(con: sqlite3.Connection, observation_id: str,
                              observation_revision: int) -> None:
    """A NEW occurrence links the observation's current revision (AD-05,
    plan 3.10). Observation revisions only, never transcript freshness.
    Callers run inside the write transaction that inserts the link."""
    current = con.execute("SELECT revision FROM ad_observations WHERE id=?",
                          (observation_id,)).fetchone()["revision"]
    if observation_revision != current:
        raise _ops.BadInput(f"stale observation revision {observation_revision}:"
                            f" the observation's current revision is {current}")


@_db.atomic
def create_occurrence(con: sqlite3.Connection, episode_id: str,
                      observation_id: str, observation_revision: int,
                      basis: str = "manual", reviewer: str = "",
                      correspondence: str = "confirmed",
                      actor: str = "owner", ctx=None) -> dict:
    """Separately identify one placement occurrence with a revisioned link.

    When it owns the transaction, @_db.atomic holds the write lock (BEGIN
    IMMEDIATE) from these checks through the insert, so the linked revision
    is current at commit. Inside a caller's deferred transaction it is only a
    SAVEPOINT: a revision committed by another connection after the caller's
    snapshot makes the insert fail with sqlite3.OperationalError ('database
    is locked'), never a stale link, but not this BadInput message."""
    _require_member(ctx, "match_creatives")
    if correspondence not in CORRESPONDENCE:
        raise _ops.BadInput("unknown correspondence state")
    _require_link(con, episode_id, observation_id, observation_revision)
    _require_current_revision(con, observation_id, observation_revision)
    now = _now()
    with _db.transaction(con, write=True):
        occ_id = _new_id("occ")
        con.execute("INSERT INTO occurrences (id, workspace_id, episode_id,"
                    " correspondence, revision, created_at, updated_at)"
                    " VALUES (?, 'local', ?, ?, 1, ?, ?)",
                    (occ_id, episode_id, correspondence, now, now))
        con.execute("INSERT INTO occurrence_observations (occurrence_id, observation_id,"
                    " basis, reviewer) VALUES (?, ?, ?, ?)",
                    (occ_id, observation_id,
                     _link_basis(basis, observation_revision), reviewer))
        rev = _db.bump_revision(con)
        _db.audit(con, actor, "occurrence_create", "occurrence", occ_id,
                  {"episode_id": episode_id, "observation_id": observation_id,
                   "correspondence": correspondence}, rev)
    return {"occurrence_id": occ_id, "revision": 1,
            "correspondence": correspondence, "aggregate_rev": rev}


@_db.atomic
def map_refetch(con: sqlite3.Connection, occurrence_id: str,
                observation_id: str, observation_revision: int,
                media_digest: str | None, source_interval: str | None,
                episode_id: str, same_bytes: bool,
                same_interval: bool, reviewer: str = "",
                actor: str = "owner", ctx=None) -> dict:
    """Same-placement refetch preserves one mapping; changed DAI stays ambiguous.

    Identical media bytes plus the same source interval and episode retain the
    mapping. Anything else is recorded ambiguous — never silently confirmed.
    """
    _require_member(ctx, "match_creatives")
    row = con.execute("SELECT episode_id, correspondence, revision FROM occurrences"
                      " WHERE id=?", (occurrence_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown occurrence")
    _require_link(con, episode_id, observation_id, observation_revision)
    if row["episode_id"] != episode_id:
        # Different logical episode: never reuse the placement identity.
        return mark_ambiguous(con, observation_id, observation_revision,
                              episode_id, "episode mismatch; separate occurrence required",
                              reviewer, actor, ctx=None)
    now = _now()
    with _db.transaction(con, write=True):
        if same_bytes and same_interval:
            con.execute("INSERT OR IGNORE INTO occurrence_observations"
                        " (occurrence_id, observation_id, basis, reviewer)"
                        " VALUES (?, ?, ?, ?)",
                        (occurrence_id, observation_id,
                         _link_basis("refetch-same", observation_revision), reviewer))
            con.execute("UPDATE occurrences SET correspondence='confirmed',"
                        " revision=revision+1, updated_at=? WHERE id=?",
                        (now, occurrence_id))
            rev = _db.bump_revision(con)
            _db.audit(con, actor, "occurrence_refetch", "occurrence", occurrence_id,
                      {"observation_id": observation_id, "media_digest": media_digest,
                       "source_interval": source_interval,
                       "correspondence": "confirmed"}, rev)
            return {"occurrence_id": occurrence_id, "correspondence": "confirmed",
                    "revision": row["revision"] + 1, "aggregate_rev": rev}
        con.execute("INSERT OR IGNORE INTO occurrence_observations"
                    " (occurrence_id, observation_id, basis, reviewer)"
                    " VALUES (?, ?, ?, ?)",
                    (occurrence_id, observation_id,
                     _link_basis("refetch-changed", observation_revision), reviewer))
        con.execute("UPDATE occurrences SET correspondence='ambiguous',"
                    " revision=revision+1, updated_at=? WHERE id=?", (now, occurrence_id))
        rev = _db.bump_revision(con)
        _db.audit(con, actor, "occurrence_refetch", "occurrence", occurrence_id,
                  {"observation_id": observation_id,
                   "correspondence": "ambiguous",
                   "reason": "bytes or interval changed without reviewed correspondence"},
                  rev)
        return {"occurrence_id": occurrence_id, "correspondence": "ambiguous",
                "revision": row["revision"] + 1, "aggregate_rev": rev,
                "note": "unresolved changed-DAI correspondence; no exact total claimed"}


@_db.atomic
def mark_ambiguous(con: sqlite3.Connection, observation_id: str,
                   observation_revision: int, episode_id: str,
                   reason: str, reviewer: str = "",
                   actor: str = "owner", ctx=None) -> dict:
    """Retain source observations with correspondence=ambiguous."""
    _require_member(ctx, "match_creatives")
    _require_link(con, episode_id, observation_id, observation_revision)
    now = _now()
    with _db.transaction(con, write=True):
        occ_id = _new_id("occ")
        con.execute("INSERT INTO occurrences (id, workspace_id, episode_id,"
                    " correspondence, revision, created_at, updated_at)"
                    " VALUES (?, 'local', ?, 'ambiguous', 1, ?, ?)",
                    (occ_id, episode_id, now, now))
        con.execute("INSERT INTO occurrence_observations (occurrence_id, observation_id,"
                    " basis, reviewer) VALUES (?, ?, ?, ?)",
                    (occ_id, observation_id,
                     _link_basis(f"ambiguous:{reason[:120]}", observation_revision),
                     reviewer))
        rev = _db.bump_revision(con)
        _db.audit(con, actor, "occurrence_ambiguous", "occurrence", occ_id,
                  {"observation_id": observation_id, "reason": reason}, rev)
    return {"occurrence_id": occ_id, "correspondence": "ambiguous",
            "revision": 1, "aggregate_rev": rev}


def occurrence_counts(con: sqlite3.Connection, episode_id: str | None = None) -> dict:
    """Snapshot observations + confirmed lower bound + unresolved, separately.

    Returns no fabricated exact placement total while ambiguous links exist:
    ``exact_total`` is None with a reason in that case.
    """
    if episode_id:
        obs = con.execute("SELECT COUNT(*) c FROM ad_observations WHERE episode_id=?",
                          (episode_id,)).fetchone()["c"]
        conf = con.execute("SELECT COUNT(DISTINCT o.id) c FROM occurrences o"
                           " WHERE o.episode_id=? AND o.correspondence='confirmed'",
                           (episode_id,)).fetchone()["c"]
        amb = con.execute("SELECT COUNT(DISTINCT o.id) c FROM occurrences o"
                          " WHERE o.episode_id=? AND o.correspondence='ambiguous'",
                          (episode_id,)).fetchone()["c"]
    else:
        obs = con.execute("SELECT COUNT(*) c FROM ad_observations").fetchone()["c"]
        conf = con.execute("SELECT COUNT(*) c FROM occurrences"
                           " WHERE correspondence='confirmed'").fetchone()["c"]
        amb = con.execute("SELECT COUNT(*) c FROM occurrences"
                          " WHERE correspondence='ambiguous'").fetchone()["c"]
    exact = conf if amb == 0 else None
    return {"snapshot_observations": obs,
            "confirmed_occurrences_lower_bound": conf,
            "ambiguous_unresolved": amb,
            "exact_total": exact,
            "exact_total_reason": ("exact while no ambiguous correspondence" if amb == 0
                                   else "ambiguous correspondence unresolved;"
                                        " confirmed count is a lower bound only")}


def match_creatives_service(ctx, con: sqlite3.Connection, request,
                            idempotency_key: str = "",
                            actor: str = "owner") -> dict:
    """Authorized CreativeMatchRequest dispatcher (contracts.py shape)."""
    from . import contracts as _contracts
    if not isinstance(request, _contracts.CreativeMatchRequest):
        raise _ops.BadInput("CreativeMatchRequest is required")
    _require_member(ctx, "match_creatives")
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise _contracts.ContractError("creation requires an idempotency key",
                                       _contracts.HTTP_BAD_REQUEST)
    _ensure_aux(con)
    out = {"config_digest": match_config_digest({"version": TEXT_CONFIG_VERSION}),
           "suggestions": {}}
    for oid in (request.observation_ids or ()):
        try:
            out["suggestions"][oid] = suggest_memberships(con, oid, ctx=None)
        except _ops.BadInput as exc:
            out["suggestions"][oid] = {"error": str(exc)}
    rev = _db.bump_revision(con)
    _db.audit(con, actor, "creative_match", "creative",
              request.selection_snapshot or "adhoc",
              {"idempotency_key": idempotency_key,
               "observations": list(request.observation_ids or ())}, rev)
    out["aggregate_rev"] = rev
    return out
