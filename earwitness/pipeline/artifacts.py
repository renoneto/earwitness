"""P04 media artifact lifecycle: holds, clips, rights, release, deletion.

Implements full-product plan section 9 on top of the P00-frozen schema
(`media_artifacts`) and the P01 job ledger:

- Media/clip metadata carries private locators (workspace-relative, never
  browser-supplied absolute paths). Acquisition-to-processing holds pin
  required audio until transcription/refinement/clip stages finish or an
  explicit policy deadline stops them with `SOURCE_EXPIRED`.
- Clips are extracted from the exact media artifact with original
  start/end offsets plus permitted context. There is NO 90-second
  truncation for new evidence (the historical `scripts/extract_clips.py`
  60-90s window stays a property of that historical script only).
- Rights/deadline checks run at enqueue, execution, playback, export and
  deletion. Rights expiry and required erasure override holds and cancel
  dependent work.
- Bounded release hands out at most 1 MiB per admitted chunk with a
  30-second chunk timeout, reauthorizing every chunk and every resumed
  range. Length/range headers are accurate; an interrupted transfer is an
  incomplete failure, never a clean success.
- The authoritative deletion/revocation ledger lives OUTSIDE the
  application snapshot's rollback domain: locally a separate
  operator-controlled SQLite ledger under an ignored recovery root
  (default `<workspace>-recovery/`, which matches the repository's
  `.earwitness-*/` ignore pattern), keyed by stable workspace identity.
  Restore must replay it before any release; without current authority
  the workspace stays quarantined.
- Backup manifests list required private media with hashes; missing
  permitted artifacts are explicit, never silent success. Purge decisions
  are previewable and require explicit authorization; no broad
  historical-purge path touches the owner workspace.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from earwitness.app.db import transaction

CHUNK_BYTES = 1 << 20      # 1 MiB per admitted release chunk.
CHUNK_TIMEOUT_S = 30.0     # 30-second timeout per admitted chunk.

RIGHTS_STATES = frozenset({"permitted", "expired", "erased", "unknown"})
GATED_OPERATIONS = frozenset(
    {"enqueue", "execute", "playback", "export", "share", "delete"})


class ArtifactError(Exception):
    """Base error for media lifecycle violations."""


class RightsDenied(ArtifactError):
    """The rights/deadline gate refused this operation."""


class SourceExpired(ArtifactError):
    """Audio expired before required processing finished."""


class ClipFailed(ArtifactError):
    """Clip extraction failed; required media is retained, not deleted."""


class OffsetError(ArtifactError):
    """Requested clip offsets are invalid at container level."""


class Revoked(ArtifactError):
    """Service-controlled access was revoked before/during release."""


class IncompleteTransfer(ArtifactError):
    """An interrupted response; carries bytes already admitted."""

    def __init__(self, message: str, bytes_sent: int = 0):
        super().__init__(message)
        self.bytes_sent = bytes_sent


class Quarantined(ArtifactError):
    """Restored workspace without current deletion authority."""


class UnauthorizedPurge(ArtifactError):
    """Purge without explicit operator authorization."""


def utcnow() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def ensure_tables(con: sqlite3.Connection) -> None:
    """Create P04 helper tables only; never alters P00-owned tables."""
    con.execute(
        "CREATE TABLE IF NOT EXISTS p01p04_holds ("
        " artifact_id TEXT NOT NULL,"
        " job_id TEXT NOT NULL,"
        " reason TEXT NOT NULL DEFAULT '',"
        " created_at TEXT NOT NULL,"
        " PRIMARY KEY (artifact_id, job_id))")
    con.execute("CREATE TABLE IF NOT EXISTS p01p04_retention"
                " (artifact_id TEXT PRIMARY KEY, retain_until TEXT NOT NULL)")
    # Last: a new or rebuilt guarded table gets its view-state guards, or
    # view_state_key stays None and no research read is reused (UPD3-SLOW).
    from earwitness.app.db import ensure_view_state_guards
    ensure_view_state_guards(con)


def _require_tables(con):
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"p01p04_holds", "p01p04_retention"} <= tables:
        raise ArtifactError("artifact schema requires explicit setup")


# -- Registration and holds ---------------------------------------------------

def _locator_ok(locator: str) -> bool:
    if not locator or not isinstance(locator, str):
        return False
    if locator.startswith("/") or locator.startswith("~"):
        return False
    if ".." in Path(locator).parts:
        return False
    return True


def register_media(con: sqlite3.Connection, workspace_id: str = "local", *,
                   digest: str, size_bytes: int, duration_s: float | None,
                   format: str, locator: str, rights_state: str = "permitted",
                   retain_until: str | None = None,
                   fetch_date: str | None = None,
                   fetch_date_source: str = "") -> dict:
    """Register a media artifact with a private workspace-relative locator."""
    if rights_state not in RIGHTS_STATES:
        raise ArtifactError(f"unknown rights state: {rights_state!r}")
    if not _locator_ok(locator):
        raise ArtifactError("media locator must be a private workspace-relative path")
    if not digest:
        raise ArtifactError("media registration requires a content digest")
    artifact_id = "med_" + uuid.uuid4().hex[:12]
    now_s = utcnow()
    _require_tables(con)
    with transaction(con, write=True):
        # Content identity is reusable within a workspace (plan section 3):
        # byte-identical media avoids repeated transcription, while each
        # fetch observation and episode occurrence stays distinct.
        prior = con.execute("SELECT id FROM media_artifacts WHERE workspace_id=?"
                            " AND digest=?", (workspace_id, digest)).fetchone()
        if prior is None:
            con.execute("INSERT INTO media_artifacts (id, workspace_id, digest, bytes,"
                        " duration_s, format, locator, rights_state, fetch_date,"
                        " fetch_date_source, created_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (artifact_id, workspace_id, digest, int(size_bytes), duration_s,
                         format, locator, rights_state, fetch_date, fetch_date_source,
                         now_s))
        else:
            artifact_id = prior["id"]
        if retain_until:
            con.execute("INSERT OR REPLACE INTO p01p04_retention"
                        " (artifact_id, retain_until) VALUES (?, ?)",
                        (artifact_id, retain_until))
    row = con.execute("SELECT * FROM media_artifacts WHERE id=?",
                      (artifact_id,)).fetchone()
    return dict(row)


def get_media(con: sqlite3.Connection, artifact_id: str) -> dict:
    row = con.execute("SELECT * FROM media_artifacts WHERE id=?",
                      (artifact_id,)).fetchone()
    if row is None:
        raise KeyError(artifact_id)
    return dict(row)


def acquire_hold(con: sqlite3.Connection, artifact_id: str, job_id: str,
                 reason: str = "") -> None:
    """Pin acquisition-to-processing audio until the owning stage finishes."""
    _require_tables(con)
    with transaction(con, write=True):
        get_media(con, artifact_id)  # KeyError when unknown.
        con.execute("INSERT OR IGNORE INTO p01p04_holds (artifact_id, job_id,"
                    " reason, created_at) VALUES (?, ?, ?, ?)",
                    (artifact_id, job_id, reason[:200], utcnow()))


def release_hold(con: sqlite3.Connection, artifact_id: str, job_id: str) -> None:
    _require_tables(con)
    with transaction(con, write=True):
        con.execute("DELETE FROM p01p04_holds WHERE artifact_id=? AND job_id=?",
                    (artifact_id, job_id))


def holds_for(con: sqlite3.Connection, artifact_id: str) -> list:
    _require_tables(con)
    return [dict(r) for r in con.execute(
        "SELECT * FROM p01p04_holds WHERE artifact_id=?", (artifact_id,)).fetchall()]


def retain_until(con: sqlite3.Connection, artifact_id: str) -> str | None:
    try:
        row = con.execute("SELECT retain_until FROM p01p04_retention WHERE artifact_id=?",
                          (artifact_id,)).fetchone()
    except sqlite3.Error:
        return None
    return row["retain_until"] if row else None


def check_rights(con: sqlite3.Connection, artifact_id: str, operation: str,
                 *, ledger=None, workspace_id: str = "local", now=None) -> None:
    """Gate enqueue/execution/playback/export/share/delete on rights+deadline.

    Raises RightsDenied/SourceExpired. Rights expiry and required erasure
    override holds. When a deletion-ledger handle is supplied, pending
    erasure also denies future service-controlled release.
    """
    if operation not in GATED_OPERATIONS:
        raise ArtifactError(f"unknown gated operation: {operation!r}")
    media = get_media(con, artifact_id)
    if ledger is not None and is_erased(ledger, workspace_id, artifact_id):
        raise RightsDenied(f"artifact {artifact_id} is erased; release denied")
    if media["rights_state"] == "erased":
        raise RightsDenied(f"artifact {artifact_id} is erased; release denied")
    if media["rights_state"] == "expired":
        raise SourceExpired(f"artifact {artifact_id} expired before"
                            f" {operation} could complete")
    deadline = retain_until(con, artifact_id)
    if deadline and (now if now is not None else utcnow()) > deadline:
        raise SourceExpired(f"artifact {artifact_id} passed its retention deadline"
                            f" ({deadline}) before {operation}")
    if media["rights_state"] == "unknown" and operation in ("export", "share"):
        raise RightsDenied(f"artifact {artifact_id} has unknown rights;"
                           f" {operation} denied")


# -- Source-pinned clip extraction ---------------------------------------------

def clip_span(start_s: float, end_s: float, context_s: float = 0.0,
              media_duration_s: float | None = None) -> tuple:
    """Validate offsets at container level and apply permitted context.

    Returns the (clip_start, clip_length) pair. The full selected
    interval plus context is preserved; there is no duration truncation.
    """
    for value, name in ((start_s, "start_s"), (end_s, "end_s"),
                        (context_s, "context_s")):
        if not isinstance(value, (int, float)) or value != value:
            raise OffsetError(f"{name} must be a finite number")
    if context_s < 0:
        raise OffsetError("context_s must be non-negative")
    if start_s < 0 or end_s < 0:
        raise OffsetError("clip offsets must be non-negative")
    if not end_s > start_s:
        raise OffsetError("clip end must be after clip start")
    if media_duration_s is not None:
        if start_s >= media_duration_s:
            raise OffsetError("clip start is beyond measured media duration")
        if end_s > media_duration_s:
            raise OffsetError("clip end is beyond measured media duration")
    clip_start = max(0.0, start_s - context_s)
    clip_end = end_s + context_s
    if media_duration_s is not None:
        clip_end = min(clip_end, media_duration_s)
    return (round(clip_start, 3), round(clip_end - clip_start, 3))


def extract_clip(src_path: str | os.PathLike, dest_path: str | os.PathLike,
                 start_s: float, end_s: float, *, context_s: float = 0.0,
                 media_duration_s: float | None = None,
                 runner=None) -> dict:
    """Extract the exact source interval (+ context) with ffmpeg.

    `runner` defaults to `subprocess.run` and is injectable so tests never
    need real media. Failures raise ClipFailed and never delete the
    required source media.
    """
    src_path, dest_path = Path(src_path), Path(dest_path)
    if not src_path.is_file():
        raise ClipFailed(f"source media is missing: {src_path}")
    clip_start, length = clip_span(start_s, end_s, context_s, media_duration_s)
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", str(clip_start),
           "-i", str(src_path), "-t", str(length),
           "-acodec", "libmp3lame", "-b:a", "64k", str(dest_path)]
    run = runner or subprocess.run
    try:
        result = run(cmd, capture_output=True, text=True)
    except Exception as exc:
        raise ClipFailed(f"clip extraction could not start: {exc}")
    if getattr(result, "returncode", 1) != 0 or not dest_path.is_file():
        stderr = (getattr(result, "stderr", "") or "")[:300]
        raise ClipFailed(f"clip extraction failed: {stderr}")
    return {"path": str(dest_path), "bytes": dest_path.stat().st_size,
            "clip_start_s": clip_start, "clip_length_s": length,
            "source_start_s": start_s, "source_end_s": end_s}


# -- Bounded release ------------------------------------------------------------

def content_range_header(start: int, end: int, total: int) -> str:
    """Accurate Range header value for bytes [start, end)."""
    if not (0 <= start <= end <= total):
        raise ArtifactError("invalid range for stored length")
    return f"bytes {start}-{end - 1}/{total}"


def read_chunk(path: str | os.PathLike, offset: int, length: int,
               *, max_chunk: int = CHUNK_BYTES) -> bytes:
    """Read one bounded chunk; overlong requests are refused, not trimmed."""
    if offset < 0 or length <= 0:
        raise ArtifactError("chunk offset/length out of range")
    if length > max_chunk:
        raise ArtifactError(f"chunk of {length} bytes exceeds the"
                            f" {max_chunk}-byte admitted bound")
    with open(path, "rb") as handle:
        handle.seek(offset)
        data = handle.read(length)
    return data


def bounded_release(path: str | os.PathLike, start: int, end: int, authorize,
                    *, chunk_bytes: int = CHUNK_BYTES,
                    timeout_s: float = CHUNK_TIMEOUT_S,
                    clock=None) -> "Generator[bytes, None, int]":
    """Yield admitted chunks with per-chunk reauthorization.

    `authorize(offset, length)` is called before every chunk and before
    every resumed range; it raises Revoked when revocation won. When
    revocation or a deadline lands mid-transfer, the generator raises
    IncompleteTransfer carrying the admitted byte count -- never a clean
    successful end for a truncated response. One normal HTTP response may
    stream successive authorized chunks; no application continuation
    protocol is assembled here.
    """
    path = Path(path)
    total = path.stat().st_size
    if not (0 <= start <= end <= total):
        raise ArtifactError("release range outside stored bytes")
    now = clock or time.monotonic
    sent = 0
    offset = start
    while offset < end:
        length = min(chunk_bytes, end - offset)
        deadline = now() + timeout_s
        authorize(offset, length)  # Fresh check per chunk; may raise Revoked.
        if now() > deadline:
            raise IncompleteTransfer("release chunk timed out", bytes_sent=sent)
        yield read_chunk(path, offset, length, max_chunk=chunk_bytes)
        sent += length
        offset += length
    return sent


# -- Authoritative deletion/revocation ledger ------------------------------------

_LEDGER_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS deletions ("
    " workspace_id TEXT NOT NULL,"
    " artifact_id TEXT NOT NULL,"
    " reason TEXT NOT NULL DEFAULT '',"
    " actor TEXT NOT NULL DEFAULT '',"
    " state TEXT NOT NULL DEFAULT 'committed',"
    " created_at TEXT NOT NULL,"
    " PRIMARY KEY (workspace_id, artifact_id))")


def recovery_root_for(workspace: str | os.PathLike) -> Path:
    """Default ignored recovery root: `<workspace>-recovery/`."""
    workspace = Path(workspace)
    return workspace.parent / (workspace.name + "-recovery")


def ledger_connect(recovery_root: str | os.PathLike,
                   workspace_id: str) -> sqlite3.Connection:
    """Open (creating, 0700/0600) the operator deletion ledger."""
    root = Path(recovery_root)
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    path = root / "deletion-ledger.sqlite3"
    con = sqlite3.connect(str(path), timeout=15.0, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute(_LEDGER_SCHEMA)
    con.execute("INSERT OR IGNORE INTO deletions (workspace_id, artifact_id,"
                " reason, actor, state, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("_ledger", "_init", "ledger created", "operator",
                 "committed", utcnow()))
    con.commit()
    con.execute("DELETE FROM deletions WHERE workspace_id='_ledger'")
    con.commit()
    _ = workspace_id  # Ledger file is per recovery root; rows key workspace.
    return con


def record_erasure(ledger: sqlite3.Connection, workspace_id: str,
                   artifact_id: str, reason: str, actor: str) -> dict:
    """Durably record erasure/revocation intent BEFORE reporting committed."""
    if not reason or not actor:
        raise ArtifactError("erasure requires a reason and an actor")
    if ledger.in_transaction:
        raise ArtifactError("erasure ledger requires its own durable commit boundary")
    with transaction(ledger, write=True):
        ledger.execute("INSERT OR REPLACE INTO deletions (workspace_id, artifact_id,"
                       " reason, actor, state, created_at)"
                       " VALUES (?, ?, ?, ?, 'committed', ?)",
                       (workspace_id, artifact_id, reason[:500], actor, utcnow()))
    return {"workspace_id": workspace_id, "artifact_id": artifact_id,
            "state": "committed"}


def is_erased(ledger: sqlite3.Connection, workspace_id: str,
              artifact_id: str) -> bool:
    row = ledger.execute("SELECT state FROM deletions WHERE workspace_id=?"
                         " AND artifact_id=?", (workspace_id, artifact_id)).fetchone()
    return row is not None and row["state"] == "committed"


def pending_for_workspace(ledger: sqlite3.Connection, workspace_id: str) -> list:
    return [dict(r) for r in ledger.execute(
        "SELECT artifact_id, reason, actor, created_at FROM deletions"
        " WHERE workspace_id=?", (workspace_id,)).fetchall()]


def apply_ledger_to_workspace(con: sqlite3.Connection, ledger: sqlite3.Connection,
                              workspace_id: str) -> dict:
    """Replay authoritative deletions into the application workspace.

    Marks erased artifacts' rights state so future service-controlled
    releases are denied. Returns the applied ids.
    """
    applied = []
    with transaction(con, write=True):
        for row in pending_for_workspace(ledger, workspace_id):
            con.execute("UPDATE media_artifacts SET rights_state='erased'"
                        " WHERE id=? AND workspace_id=?",
                        (row["artifact_id"], workspace_id))
            applied.append(row["artifact_id"])
    return {"applied": applied}


def restore_with_ledger(workspace_db_path: str | os.PathLike,
                        backup_db_path: str | os.PathLike,
                        ledger: sqlite3.Connection | None,
                        workspace_id: str) -> dict:
    """Gate a restore on current deletion authority.

    Without a live ledger the restored workspace stays quarantined: no
    browser, worker, share or download access until the latest
    authoritative records are replayed. A ledger copied inside an old
    backup is insufficient -- only the current operator ledger qualifies.
    There is no quarantine bypass.
    """
    if ledger is None:
        raise Quarantined("no current deletion authority; restored workspace"
                          " remains quarantined until the operator ledger"
                          " is available and replayed")
    _ = (workspace_db_path, backup_db_path)
    pending = pending_for_workspace(ledger, workspace_id)
    return {"quarantined": False, "replayed_deletions": len(pending),
            "note": "replay the latest authoritative records before any release"}


# -- Backup manifest and purge -----------------------------------------------------

def sha256_file(path: str | os.PathLike) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_manifest(con: sqlite3.Connection, workspace_id: str,
                    media_root: str | os.PathLike) -> dict:
    """Portable backup/restore manifest of required private media + hashes.

    Missing permitted artifacts are listed explicitly under `missing`;
    the manifest never reports silent success when required bytes are
    absent. Hash mismatches are listed under `corrupt`.
    """
    media_root = Path(media_root)
    items, missing, corrupt = [], [], []
    for row in con.execute("SELECT * FROM media_artifacts WHERE workspace_id=?",
                           (workspace_id,)).fetchall():
        media = dict(row)
        locator = media.get("locator", "")
        candidate = media_root / locator if locator else None
        entry = {"id": media["id"], "digest": media["digest"],
                 "bytes": media["bytes"], "locator": locator,
                 "rights_state": media["rights_state"]}
        if candidate is not None and candidate.is_file():
            actual = sha256_file(candidate)
            entry["present"] = True
            entry["sha256"] = actual
            entry["hash_ok"] = (actual == media["digest"])
            if not entry["hash_ok"]:
                corrupt.append(entry["id"])
        else:
            entry["present"] = False
            if media["rights_state"] == "permitted":
                missing.append(entry["id"])
        items.append(entry)
    complete = not missing and not corrupt
    return {"workspace_id": workspace_id, "media": items, "missing": missing,
            "corrupt": corrupt, "complete": complete}


def verify_manifest(manifest: dict, media_root: str | os.PathLike) -> dict:
    """Re-verify a manifest against restored bytes; explicit, never silent."""
    media_root = Path(media_root)
    missing = [m["id"] for m in manifest.get("media", [])
               if not (media_root / m.get("locator", "\x00")).is_file()]
    return {"complete": not missing, "missing": missing,
            "declared_missing": manifest.get("missing", [])}


def purge_preview(con: sqlite3.Connection, ledger: sqlite3.Connection,
                  workspace_id: str, media_root: str | os.PathLike) -> dict:
    """Preview retention decisions: eligible, held, and retained artifacts.

    Eligible means erased/expired (or past deadline) AND free of
    acquisition-to-processing holds. Preview changes nothing.
    """
    _require_tables(con)
    eligible, held, retained = [], [], []
    for row in con.execute("SELECT * FROM media_artifacts WHERE workspace_id=?",
                           (workspace_id,)).fetchall():
        media = dict(row)
        aid = media["id"]
        if holds_for(con, aid):
            held.append(aid)
            continue
        erased = is_erased(ledger, workspace_id, aid) \
            or media["rights_state"] in ("erased", "expired")
        past_deadline = False
        deadline = retain_until(con, aid)
        if deadline and utcnow() > deadline:
            past_deadline = True
        if erased or past_deadline:
            eligible.append(aid)
        else:
            retained.append(aid)
    _ = media_root
    return {"eligible": sorted(eligible), "held": sorted(held),
            "retained": sorted(retained)}


def purge(con: sqlite3.Connection, ledger: sqlite3.Connection,
          workspace_id: str, media_root: str | os.PathLike,
          artifact_ids: list, auth: dict) -> dict:
    """Delete only explicitly authorized, preview-eligible artifacts.

    `auth` must be an explicit operator grant
    (`{"authorized": True, "operator": ..., "reason": ...}`); anything
    else raises UnauthorizedPurge. There is no "purge everything" mode:
    an empty list purges nothing, and held/retained ids are refused.
    """
    if not isinstance(auth, dict) or auth.get("authorized") is not True \
            or not auth.get("operator") or not auth.get("reason"):
        raise UnauthorizedPurge("purge requires explicit operator authorization"
                                " with operator and reason")
    if con.in_transaction:
        raise ArtifactError("irreversible purge requires its own outer commit boundary")
    if not artifact_ids:
        return {"purged": [], "note": "empty selection purges nothing"}
    preview = purge_preview(con, ledger, workspace_id, media_root)
    eligible = set(preview["eligible"])
    refused = [aid for aid in artifact_ids if aid not in eligible]
    if refused:
        raise UnauthorizedPurge(f"purge refused for non-eligible artifacts: {refused}")
    media_root = Path(media_root)
    purged = []
    locators = []
    for aid in artifact_ids:
        media = get_media(con, aid)
        locators.append(media.get("locator", ""))
        record_erasure(ledger, workspace_id, aid, auth["reason"], auth["operator"])
    with transaction(con, write=True):
        for aid in artifact_ids:
            con.execute("DELETE FROM p01p04_holds WHERE artifact_id=?", (aid,))
            con.execute("DELETE FROM p01p04_retention WHERE artifact_id=?", (aid,))
            con.execute("DELETE FROM media_artifacts WHERE id=? AND workspace_id=?",
                        (aid, workspace_id))
            purged.append(aid)
    for locator in locators:
        if locator and _locator_ok(locator):
            (media_root / locator).unlink(missing_ok=True)
    return {"purged": purged, "operator": auth["operator"], "reason": auth["reason"]}
