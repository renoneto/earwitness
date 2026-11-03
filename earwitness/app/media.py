"""Registered local media identities only (stdlib). No proxying or fetching."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

LOGGER = logging.getLogger("earwitness.media")

# Media id -> {rel path inside repo, offset seconds, label}. The bundled clip
# is a 90-second excerpt at transcript offset 1276s, never a full episode.
REGISTERED = {
    "practice-clip-s002e1": {
        "rel": "clips/s002e1_shopify_1276s.mp3",
        "offset_s": 1276.0,
        "mime": "audio/mpeg",
        "label": "Bundled local excerpt (90s at transcript offset 1276s)",
    },
}

# Pilot slugs with local audio scratch in .pilot-corpus/<slug>/latest.{mp3,m4a}.
# Git-ignored, served in place; never moved or copied into tracked dirs.
# Episode upload:pilot-<slug> maps to media id pilot-<slug>.
PILOT_PREFIX = "pilot-"
PILOT_SLUGS = ("acquired", "lennys-podcast", "my-first-million", "tbpn",
               "latent-space")
PILOT_MIMES = {".mp3": "audio/mpeg", ".m4a": "audio/mp4"}

# Episode ids whose audio proved untrustworthy (wrong content served).
# Quarantined ids resolve to no media anywhere: pages show the honest
# unavailable path instead of misleading audio. Reversible: delete the
# entry once a content-verified file is mapped. Currently empty: the
# 2026-09-24 audit cleared every episode (s001e0's "wrong show" proved
# to be a rotating cross-promo ad pod; its stream body is the True
# Crime episode).
AUDIO_QUARANTINED = set()

# 2026-09-24 all-show audit verdicts (head-transcribe vs transcript).
# exact: audio matches the transcript, listening verifies. rotated:
# same show, programmatic prerolls rotated since transcription — ad
# windows (esp. near 0) may play a different creative; body aligns
# approximately. Episodes in neither set are unverified: keep the
# generic live-stream caution.
TRUST_EXACT = frozenset({
    "bench_0", "bench_3",
    "upload:pilot-acquired", "upload:pilot-lennys-podcast",
    "upload:pilot-my-first-million", "upload:pilot-tbpn",
    "upload:pilot-latent-space",
    "s000e0", "s000e1", "s003e0", "s003e1", "s005e0", "s006e1",
})
TRUST_ROTATED = frozenset({
    "s001e0", "s001e1", "s002e0", "s002e1", "s004e0", "s004e1",
    "s006e0", "s007e0", "s008e0", "s008e1", "s009e0", "s009e1",
})


def audio_trust(episode_id: str) -> str:
    """Listen-trust for an episode: exact, rotated, or unknown."""
    if episode_id in TRUST_EXACT:
        return "exact"
    if episode_id in TRUST_ROTATED:
        return "rotated"
    return "unknown"


# Benchmark episodes with local audio scratch in
# .pilot-corpus/bench-audio/bench_<N>.mp3. Git-ignored, served in place;
# never moved or copied into tracked dirs. Episode bench_<N> maps to media
# id bench_<N> explicitly (allowlist, no traversal).
# Only content-verified episodes map: bench_1/2/4 were RSS title-matches
# whose audio proved to be different episode cuts (head-transcribe vs the
# supplied transcripts, 2026-09-24), so they stay on the unavailable path.
BENCH_IDS = ("bench_3",)
BENCH_MIME = "audio/mpeg"


def known_media(media_id: str) -> bool:
    return media_id in REGISTERED


def pilot_media_id(episode_id: str) -> str | None:
    """Media id for a pilot upload episode, else None.

    Only upload:pilot-<slug> with an allowlisted slug maps; every other
    episode id (including other uploads) is untouched.
    """
    if not isinstance(episode_id, str) or not episode_id.startswith("upload:"):
        return None
    slug = episode_id[len("upload:pilot-"):] if episode_id.startswith("upload:pilot-") else None
    if not slug or slug not in PILOT_SLUGS:
        return None
    return PILOT_PREFIX + slug


def resolve_pilot(repo: Path, media_id: str) -> dict | None:
    """Resolve a pilot media id to its .pilot-corpus scratch file in place."""
    if not isinstance(media_id, str) or not media_id.startswith(PILOT_PREFIX):
        return None
    slug = media_id[len(PILOT_PREFIX):]
    if slug not in PILOT_SLUGS:
        return None
    base = (repo / ".pilot-corpus" / slug).resolve()
    if (repo.resolve() not in base.parents and base != repo.resolve()):
        return None
    for ext, mime in PILOT_MIMES.items():
        target = (repo / ".pilot-corpus" / slug / ("latest" + ext)).resolve()
        if base not in target.parents:
            continue
        if not target.is_file():
            continue
        return {"media_id": media_id, "size": target.stat().st_size,
                "offset_s": 0.0, "mime": mime,
                "label": f"Local pilot audio ({slug})",
                "path": target}
    return None


def bench_media_id(episode_id: str) -> str | None:
    """Media id for a benchmark episode with local audio, else None.

    Only content-verified bench_3 maps (to the same id); every other
    episode id (including bench_0, bench_1, bench_2, bench_4 and other
    benchmarks) is untouched.
    """
    if not isinstance(episode_id, str):
        return None
    if episode_id not in BENCH_IDS:
        return None
    return episode_id


def resolve_bench(repo: Path, media_id: str) -> dict | None:
    """Resolve a benchmark media id to its .pilot-corpus/bench-audio file."""
    if not isinstance(media_id, str) or media_id not in BENCH_IDS:
        return None
    base = (repo / ".pilot-corpus" / "bench-audio").resolve()
    if (repo.resolve() not in base.parents and base != repo.resolve()):
        return None
    target = (base / (media_id + ".mp3")).resolve()
    if base not in target.parents:
        return None
    if not target.is_file():
        return None
    return {"media_id": media_id, "size": target.stat().st_size,
            "offset_s": 0.0, "mime": BENCH_MIME,
            "label": f"Local benchmark audio ({media_id})",
            "path": target}


def resolve(repo: Path, media_id: str) -> dict | None:
    """Resolve a registered id to a repo-contained file. Never arbitrary paths."""
    if isinstance(media_id, str) and media_id.startswith(PILOT_PREFIX):
        return resolve_pilot(repo, media_id)
    if isinstance(media_id, str) and media_id in BENCH_IDS:
        return resolve_bench(repo, media_id)
    spec = REGISTERED.get(media_id)
    if spec is None:
        return None
    target = (repo / spec["rel"]).resolve()
    if repo.resolve() not in target.parents:
        return None
    if not target.is_file():
        return None
    return {"media_id": media_id, "size": target.stat().st_size,
            "offset_s": spec["offset_s"], "mime": spec["mime"], "label": spec["label"],
            "path": target}


def media_for_episode(episode: dict, repo=None) -> dict | None:
    """Playback descriptor: registered local clip when mapped, else local
    pilot/benchmark audio when the file is on disk, else the known remote
    URL for browser-only streaming, else explicit unavailability."""
    if (episode.get("id") or "") in AUDIO_QUARANTINED:
        return None
    ep_id = episode.get("id") or ""
    if episode.get("media_id") and episode["media_id"] in REGISTERED:
        spec = REGISTERED[episode["media_id"]]
        return {"kind": "local_clip", "src": f"/media/{episode['media_id']}",
                "source_offset_s": spec["offset_s"], "label": spec["label"],
                "trust": "exact"}
    if repo is not None:
        pilot = pilot_media_id(ep_id)
        if pilot is not None and resolve_pilot(repo, pilot) is not None:
            return {"kind": "local_full", "src": f"/media/{pilot}",
                    "source_offset_s": 0.0,
                    "label": f"Local pilot audio ({pilot[len(PILOT_PREFIX):]})",
                    "trust": "exact"}
        bench = bench_media_id(ep_id)
        if bench is not None and resolve_bench(repo, bench) is not None:
            return {"kind": "local_full", "src": f"/media/{bench}",
                    "source_offset_s": 0.0,
                    "label": f"Local benchmark audio ({bench})",
                    "trust": "exact"}
    if episode.get("audio_url"):
        return {"kind": "remote_full", "src": episode["audio_url"],
                "source_offset_s": 0.0, "label": "Remote episode stream (needs network)",
                "trust": audio_trust(ep_id)}
    return None


# Per-connection identity memo, valid only while the database is unchanged:
# a list scan calls recording_identity for every matched passage, and each
# call decodes the whole archived and pipeline transcripts. Holds the last
# few connections (strong references, so an id is never reused while held).
_IDENTITY_MEMO: dict = {}
_IDENTITY_MEMO_CONNECTIONS = 16
_IDENTITY_MEMO_LOCK = __import__("threading").Lock()


def _identity_memo(con):
    """The memo for con's current database state (cleared on any write)."""
    state = (con.execute("PRAGMA data_version").fetchone()[0], con.total_changes,
             con.in_transaction)
    with _IDENTITY_MEMO_LOCK:
        held = _IDENTITY_MEMO.pop(id(con), None)
        if held is None or held[0] is not con or held[1] != state:
            held = (con, state, {})
        _IDENTITY_MEMO[id(con)] = held  # most recently used last
        while len(_IDENTITY_MEMO) > _IDENTITY_MEMO_CONNECTIONS:
            del _IDENTITY_MEMO[next(iter(_IDENTITY_MEMO))]
    return held[2]


def recording_identity(con, episode: dict, *, pipeline_id=None) -> dict | None:
    """Bind archived transcript, recording digest and explicit global offset."""
    artifact_id = episode.get("media_artifact_id")
    if not artifact_id:
        return None
    memo = _identity_memo(con)
    episode_id = episode.get("episode_id") or episode.get("id")
    # Cheap row fingerprint: a write rolled back inside a still-open
    # transaction leaves the connection state unchanged, not these rows.
    key = (artifact_id, episode.get("transcript_version_id"), episode_id, pipeline_id,
           tuple(map(tuple, con.execute("SELECT id,digest,bytes,duration_s FROM media_artifacts"
                             " WHERE id=?", (artifact_id,)).fetchall())),
           tuple(map(tuple, con.execute("SELECT id,sha256,length(payload) FROM source_versions"
                             " WHERE id=?", (episode.get("transcript_version_id"),)).fetchall())),
           tuple(map(tuple, con.execute("SELECT id,source_offsets,length(segments) FROM transcript_versions"
                             " WHERE episode_id=? AND media_artifact_id=? ORDER BY id",
                             (episode_id, artifact_id)).fetchall())))
    if memo is not None and key in memo:
        cached = memo[key]
        return dict(cached) if cached is not None else None
    identity = _recording_identity(con, episode, artifact_id, pipeline_id)
    if memo is not None:
        memo[key] = dict(identity) if identity is not None else None
    return identity


def _recording_identity(con, episode: dict, artifact_id, pipeline_id) -> dict | None:
    import json
    import math
    artifact = con.execute("SELECT id,digest,bytes,duration_s FROM media_artifacts"
                           " WHERE workspace_id='local' AND id=?", (artifact_id,)).fetchone()
    archived = con.execute("SELECT payload FROM source_versions WHERE id=?",
                           (episode.get("transcript_version_id"),)).fetchone()
    if artifact is None or archived is None:
        return None
    try:
        payload = json.loads(archived["payload"])
        segments = payload if isinstance(payload, list) else payload["segments"]
        duration = artifact["duration_s"]
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) \
                or not math.isfinite(duration) or duration <= 0:
            return None
        offsets = set()
        selected_id = None
        for row in con.execute("SELECT id,segments,source_offsets FROM transcript_versions"
                               " WHERE workspace_id='local' AND episode_id=? AND media_artifact_id=?"
                               " AND (? IS NULL OR id=?) ORDER BY id",
                               (episode.get("episode_id") or episode.get("id"), artifact_id,
                                pipeline_id, pipeline_id)):
            offset = json.loads(row["source_offsets"]).get("global_start_s")
            if not isinstance(offset, (int, float)) or isinstance(offset, bool) \
                    or not math.isfinite(offset) or offset < 0:
                continue
            interval = [segment for segment in segments
                        if segment["start"] < offset + duration and segment["end"] > offset]
            if json.loads(row["segments"]) == interval:
                offsets.add(float(offset))
                if selected_id is None:
                    selected_id = row["id"]
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if len(offsets) != 1:
        return None
    return {"artifact_id": artifact["id"], "digest": artifact["digest"],
            "size_bytes": artifact["bytes"], "duration_s": artifact["duration_s"],
            "source_offset_s": offsets.pop(), "pipeline_transcript_version_id": selected_id}


def _recording_duration(path: Path, format: str) -> float | None:
    import json
    import math
    import subprocess
    import wave
    try:
        if format == "wav":
            with wave.open(str(path), "rb") as recording:
                duration = recording.getnframes() / recording.getframerate()
        else:
            demuxer = {"mp3": "mp3", "m4a": "mov", "mp4": "mov", "aac": "aac",
                       "ogg": "ogg", "flac": "flac"}.get(format)
            if demuxer is None:
                return None
            result = subprocess.run(["ffprobe", "-v", "error", "-protocol_whitelist", "file",
                "-f", demuxer, "-show_entries", "format=duration", "-of", "json", str(path)],
                capture_output=True, timeout=5, check=True)
            duration = float(json.loads(result.stdout)["format"]["duration"])
        return duration if math.isfinite(duration) and duration > 0 else None
    except (OSError, EOFError, wave.Error, ValueError, KeyError,
            subprocess.SubprocessError):
        return None


# Process-local verified-proof cache: (resolved path, digest, format) ->
# (file signature at verification, verified duration or None). A hit skips
# the whole-file sha256 and ffprobe only while the file's signature is
# unchanged; recording_chunk still hashes every byte it serves.
_PROOF_CACHE: dict = {}
_PROOF_CACHE_LIMIT = 256


def _file_signature(path: Path) -> tuple:
    status = path.stat()
    return (status.st_size, status.st_mtime_ns, status.st_ctime_ns, status.st_ino, status.st_dev)


# Outside-the-database inputs of recording_proof, noted while a caller (the
# cross-request view-revision memo, customer_query._view_revision) records
# them: each is a ("file", database, locator, state) or ("clock", deadline,
# passed) entry that proof_inputs_unchanged re-checks before reuse, or
# ("volatile",) when the proof rested on a failure that the next call
# retries (nothing built on it may be reused).
_PROOF_INPUTS = __import__("contextvars").ContextVar("earwitness_proof_inputs", default=None)


def _recording_file_state(database: str, locator, resolved: dict | None = None) -> tuple | None:
    """(resolved root, resolved path, file signature or None when the path
    escapes the root or is not a file), or None when resolving or stat
    fails: everything recording_proof reads about the file before trusting
    its bytes (the bytes themselves are trusted by signature, as in
    _PROOF_CACHE).

    resolved (SPEED-FIX2, screen review SPV-C-01): one check's resolved
    root and directories, so a check of n recordings resolves the root and
    each directory once, not 2 x n paths. The answer is the full resolve's:
    the last part is resolved only when it is a symlink, and a locator
    whose last part is '..' resolves whole."""
    try:
        if resolved is None:
            root = Path(database).resolve().parent
            path = (root / locator).resolve()
        else:
            root = resolved.get(database)
            if root is None:
                root = resolved[database] = Path(database).resolve().parent
            whole = root / locator
            if whole.name in ("", ".", "..") or Path(locator).name == "..":
                path = whole.resolve()
            else:
                parent = resolved.get((database, str(whole.parent)))
                if parent is None:
                    parent = resolved[(database, str(whole.parent))] = whole.parent.resolve()
                path = parent / whole.name
                if path.is_symlink():
                    path = path.resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return (str(root), str(path), None)
        return (str(root), str(path), _file_signature(path))
    except OSError:
        return None


def _note_proof_input(entry: tuple) -> None:
    noted = _PROOF_INPUTS.get()
    if noted is not None:
        noted.append(entry)


def proof_inputs_unchanged(noted) -> bool:
    """True when every noted outside-the-database proof input still reads
    the same: file locations and signatures, and the side of each
    retention deadline the clock is on."""
    from earwitness.pipeline import artifacts
    resolved: dict = {}
    for entry in noted:
        if entry[0] == "file":
            _kind, database, locator, state = entry
            if _recording_file_state(database, locator, resolved) != state:
                return False
        elif entry[0] == "clock":
            _kind, deadline, passed = entry
            if bool(deadline and artifacts.utcnow() > deadline) != passed:
                return False
        else:
            return False
    return True


def recording_proof(con, episode: dict, identity: dict | None) -> dict | None:
    """Verify the pinned recording bytes without exposing its private locator."""
    from earwitness.pipeline import artifacts
    if identity is None or recording_identity(con, episode,
            pipeline_id=identity.get("pipeline_transcript_version_id")) != identity:
        return None
    try:
        # The retention check reads the clock: note which side of the
        # deadline this proof saw, at the one instant check_rights uses.
        now = artifacts.utcnow()
        deadline = artifacts.retain_until(con, identity["artifact_id"])
        _note_proof_input(("clock", deadline, bool(deadline and now > deadline)))
        artifacts.check_rights(con, identity["artifact_id"], "playback", now=now)
        artifact = artifacts.get_media(con, identity["artifact_id"])
        database = next((row[2] for row in con.execute("PRAGMA database_list")
                         if row[1] == "main"), "")
        if not database:
            return None
        state = _recording_file_state(database, artifact["locator"])
        _note_proof_input(("file", database, artifact["locator"], state))
        if state is None or state[2] is None:
            return None
        path, signature = Path(state[1]), state[2]
        if signature[0] != identity["size_bytes"]:
            return None
        key = (str(path), identity["digest"], artifact["format"])
        cached = _PROOF_CACHE.get(key)
        if cached is not None and cached[0] == signature:
            duration = cached[1]
        else:
            if artifacts.sha256_file(path) != identity["digest"]:
                _PROOF_CACHE.pop(key, None)
                return None
            duration = None
        if duration is None:
            duration = _recording_duration(path, artifact["format"])
            if duration is None:
                # A failed probe (timeout, no ffprobe, I/O error) is retried
                # on every call, never kept: no outcome built on it is reused.
                _note_proof_input(("volatile",))
            if _file_signature(path) == signature:  # unchanged while verified
                if len(_PROOF_CACHE) >= _PROOF_CACHE_LIMIT:
                    _PROOF_CACHE.clear()
                _PROOF_CACHE[key] = (signature, duration)
    except (artifacts.ArtifactError, KeyError, OSError) as exc:
        if isinstance(exc, OSError):
            # A failed hash or stat may succeed on the next call.
            _note_proof_input(("volatile",))
        return None
    import math
    if duration is None or not math.isclose(duration, identity["duration_s"], abs_tol=0.1):
        duration = None
    current = con.execute("SELECT alignment,media_artifact_id,transcript_version_id FROM episodes WHERE id=?",
                          (episode.get("episode_id") or episode.get("id"),)).fetchone()
    alignment = episode.get("alignment") or "unknown"
    if current is not None and current["transcript_version_id"] == episode.get("transcript_version_id") \
            and current["media_artifact_id"] in (None, identity["artifact_id"]):
        alignment = current["alignment"] or "unknown"
    return dict(identity, path=path, format=artifact["format"], verified_duration_s=duration,
                alignment=alignment)


def clip_for_publication(con, resolved: dict) -> dict | None:
    """The registered evidence clip of an evidence publication, or None: a
    read's clip, played through the record media route. The read's linked
    clip (ad_reads); for a publication on an earlier transcript version, a
    clip on that version that holds the publication's span whole. The
    caller checks the clip's version against the publication's pin. With
    the clip guard on (CLIPGUARD R8): None for a read the guard refuses
    (ad_reads.clip_hidden), and a clip found by span must keep to the read's
    padded window (_clip_qualifies)."""
    if resolved.get("kind") != "evidence":
        return None
    from . import ad_reads
    if ad_reads.no_clip_source(con, resolved["record_id"]) or (
            ad_reads.tables_present(con) and ad_reads.no_clip(con, resolved["record_id"])):
        return None   # DETBUILD: an R6 part or an R9 row has no clip, guard or not
    if ad_reads.clip_guard_on() and ad_reads.tables_present(con) \
            and ad_reads.clip_hidden(con, resolved["record_id"]):
        return None
    clip = ad_reads.clip_for(con, resolved["record_id"])
    version = (resolved.get("pins") or {}).get("transcript_version_id")
    if clip is not None and clip["transcript_version_id"] == version:
        return clip
    span = resolved.get("original_bounds") or {}
    start, end = span.get("start_s"), span.get("end_s")
    if version is None or start is None or end is None or not ad_reads.tables_present(con):
        return clip
    episode_id = resolved["episode"]["episode_id"]
    if ad_reads.clip_guard_on():
        # CLIPGUARD R8: only a clip that keeps to this read's own padded
        # window (the one R8 judged), never another read's clip file.
        for found in con.execute("SELECT * FROM ad_clips WHERE episode_id=? AND"
                                 " transcript_version_id=? AND start_s<=? AND end_s>=?"
                                 " ORDER BY end_s-start_s, rel", (episode_id, version, start, end)):
            if _clip_qualifies(dict(found), episode_id, version, start, end):
                return dict(found, clip_offset_s=start - found["start_s"])
        return clip
    row = con.execute("SELECT * FROM ad_clips WHERE episode_id=? AND transcript_version_id=?"
                      " AND start_s<=? AND end_s>=? ORDER BY end_s-start_s, rel LIMIT 1",
                      (episode_id, version, start, end)).fetchone()
    return dict(row, clip_offset_s=start - row["start_s"]) if row else clip


# Plan A (design 7): at excerpt level only a registered clip of the pinned
# ad that keeps to the documented padding plays: it starts at most 5 s
# before the ad and ends at most 3 s after it (0.5 s rounding tolerance),
# lasts at most the accepted 99 s and is on the pin's transcript version.
CLIP_BEFORE_S = 5.0
CLIP_AFTER_S = 3.0
CLIP_TOLERANCE_S = 0.5
CLIP_MAX_S = 99.0


def _clip_qualifies(clip: dict, episode_id, version, start, end) -> bool:
    lo, hi = clip.get("start_s"), clip.get("end_s")
    if clip.get("episode_id") != episode_id or clip.get("transcript_version_id") != version:
        return False
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
               for v in (lo, hi, start, end)):
        return False
    return (lo >= start - CLIP_BEFORE_S - CLIP_TOLERANCE_S
            and hi <= end + CLIP_AFTER_S + CLIP_TOLERANCE_S
            and hi - lo <= CLIP_MAX_S and lo < end and hi > start)


def excerpt_clip_for(con, evidence_id, episode_id, version, start, end) -> dict | None:
    """The registered clip an excerpt-level read may play, or None: the
    read's linked clip, else the tightest clip on the same episode and
    transcript version, either only when it keeps to the padding rule.
    Never the recording, never a clip cut on demand. With the clip guard on
    (CLIPGUARD R8), None for a read the guard refuses (ad_reads.clip_hidden),
    and a clip must also keep to the read's original bounds (the window R8
    judged), so reviewed bounds wider than the read never reach another
    read's clip file (CGS-R3-01)."""
    from . import ad_reads
    guard = ad_reads.clip_guard_on()
    if version is None or not ad_reads.tables_present(con) or ad_reads.no_clip(
            con, evidence_id) or ad_reads.no_clip_source(con, evidence_id) or (
            guard and ad_reads.clip_hidden(con, evidence_id)):
        return None   # DETBUILD: no_clip / no_clip_source, an R6 part or an R9 row
    judged = None
    if guard:
        judged = con.execute("SELECT orig_start_s, orig_end_s FROM evidence WHERE id=?",
                             (evidence_id,)).fetchone()
        if judged is None:
            return None
    linked = ad_reads.clip_for(con, evidence_id)
    candidates = [linked] if linked is not None else []
    candidates += [dict(row) for row in con.execute(
        "SELECT * FROM ad_clips WHERE episode_id=? AND transcript_version_id=?"
        " ORDER BY end_s-start_s, rel", (episode_id, version))]
    for clip in candidates:
        if _clip_qualifies(clip, episode_id, version, start, end) and (
                judged is None or _clip_qualifies(clip, episode_id, version, judged[0],
                                                  judged[1])):
            return dict(clip, clip_offset_s=max(0.0, start - clip["start_s"]))
    return None


def excerpt_clip(con, resolved: dict) -> dict | None:
    """excerpt_clip_for one resolved evidence publication (its pins and
    effective bounds)."""
    if resolved.get("kind") != "evidence":
        return None
    effective = resolved.get("effective_bounds") or {}
    return excerpt_clip_for(con, resolved["record_id"], resolved["episode"]["episode_id"],
                            (resolved.get("pins") or {}).get("transcript_version_id"),
                            effective.get("start_s"), effective.get("end_s"))


# CLIP-STOP (lead decision APP-11 (1)): a clip's bytes end at its read's
# display span. The cap is the end of the MP3 frame that holds the span end
# plus CLIP_STOP_TOLERANCE_S, never past the file end; the capped prefix is
# served as the whole file (design: launch-readiness CLIP-STOP-DESIGN.md).
CLIP_STOP_TOLERANCE_S = 0.5
# Layer III bit rates (kbps) by bit-rate index: MPEG-1, then MPEG-2 and 2.5.
_MP3_KBPS = ((0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
             (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160))
_MP3_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def _mp3_frame(data: bytes, pos: int) -> tuple | None:
    """(bytes, samples, sample rate) of the Layer III frame header at pos,
    or None when pos holds no valid one."""
    if pos + 4 > len(data) or data[pos] != 0xFF or data[pos + 1] & 0xE0 != 0xE0:
        return None
    version, layer = (data[pos + 1] >> 3) & 3, (data[pos + 1] >> 1) & 3
    rate_index, padding = (data[pos + 2] >> 2) & 3, (data[pos + 2] >> 1) & 1
    kbps_index = data[pos + 2] >> 4
    if version == 1 or layer != 1 or rate_index == 3 or kbps_index in (0, 15):
        return None
    kbps = _MP3_KBPS[version != 3][kbps_index]
    rate = _MP3_RATES[version][rate_index]
    samples = 1152 if version == 3 else 576
    return samples // 8 * kbps * 1000 // rate + padding, samples, rate


def _mp3_info_frame(data: bytes, pos: int) -> bool:
    """A Xing/Info (or VBRI) header frame: it carries no audio time."""
    mono = data[pos + 3] >> 6 == 3
    side = (17 if mono else 32) if (data[pos + 1] >> 3) & 3 == 3 else (9 if mono else 17)
    return data[pos + 4 + side:pos + 8 + side] in (b"Xing", b"Info") \
        or data[pos + 36:pos + 40] == b"VBRI"


def _id3v2_end(data: bytes) -> int:
    """The byte after a leading ID3v2 tag (and its footer); 0 without one."""
    if data[:3] != b"ID3" or len(data) < 10:
        return 0
    return 10 + ((data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14
                 | (data[8] & 0x7F) << 7 | data[9] & 0x7F) + (10 if data[5] & 0x10 else 0)


def clip_byte_cap(data: bytes, end_s) -> int:
    """How many leading bytes of an MP3 clip reach file time end_s: the end
    of the frame that holds it, by walking the frame headers (CBR or VBR, a
    leading ID3v2 tag, a trailing ID3v1 tag and a Xing/Info/VBRI frame kept
    but not timed). The whole file when end_s is None or past the last
    frame; also when a byte that is not a frame comes first, or the walk
    ends before end_s anywhere but at the last frame's end (each logged
    once); never past the file end."""
    size = len(data)
    if end_s is None:
        return size
    pos = _id3v2_end(data)
    audio_end = size - 128 if data[-128:-125] == b"TAG" else size
    at, first = 0.0, True
    while pos + 4 <= audio_end:
        frame = _mp3_frame(data, pos)
        if frame is None:
            LOGGER.warning("clip byte cap: no MP3 frame at byte %d of %d; the whole clip"
                           " is served", pos, size)
            return size
        length, samples, rate = frame
        if not (first and _mp3_info_frame(data, pos)):
            if at + samples / rate > end_s:
                return min(size, pos + length)
            at += samples / rate
        first = False
        pos += length
    if first or pos != audio_end:
        LOGGER.warning("clip byte cap: the MP3 frames end at byte %d of %d before %.3f s;"
                       " the whole clip is served", pos, size, end_s)
    return size


def _span_end(span, clip=None) -> float | None:
    """The end of a [start, end] span the players accept (EWUI.playSpan):
    finite numbers, start >= 0, end > start; else None. With a clip, also
    None for a span the clip holds none of (it ends at or before the clip
    start, or starts at or after its end): no span (CS-R4-02)."""
    import math
    if not isinstance(span, (list, tuple)) or len(span) < 2:
        return None
    lo, hi = span[0], span[1]
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
               for v in (lo, hi)):
        return None
    if clip is not None and (hi <= clip["start_s"] or lo >= clip["end_s"]):
        return None
    return float(hi) if 0 <= lo < hi else None


# CLIP-START (audit Problem 5) and LEADIN (LEDGER-LEAD3-GATE3-1): a hosted
# clip's bytes start at its read's lead-in, the first frame that starts at
# or after `span start - lead-in` (never before the clip start). The lead-in
# is the players' 3 s (app.js clipStart), or less when a shown read of
# another brand ends inside it (customer.read_lead_in, LEADIN-FILTER);
# design: CLIP-START-DESIGN.md.
CLIP_LEAD_IN_S = 3.0


def clip_byte_start(data: bytes, start_s) -> int | None:
    """The byte where an MP3 clip's first audio frame that starts at or
    after file time start_s begins, by walking the frame headers (a leading
    ID3v2 tag and a Xing/Info/VBRI frame are not audio, so they are left
    out). 0 when start_s is None (the local app), or at most 0 and the
    file starts with a frame; the end of the frames when no frame starts
    at or after start_s (nothing is served, CST-R1-03). None, logged once,
    when a byte that is not a frame comes first, or no frame at all (a
    frameless file gets no clip at any start, CLIP-START-FIX1)."""
    if start_s is None:
        return 0
    size = len(data)
    pos = _id3v2_end(data)
    audio_end = size - 128 if data[-128:-125] == b"TAG" else size
    at, first, audio, frame = 0.0, True, False, None
    while pos + 4 <= audio_end:
        frame = _mp3_frame(data, pos)
        if frame is None:
            break
        if start_s <= 0:
            return 0
        length, samples, rate = frame
        if not (first and _mp3_info_frame(data, pos)):
            if at >= start_s - 1e-6:
                return pos
            audio = True
            at += samples / rate
        first = False
        pos += length
    if frame is None or not audio:
        LOGGER.warning("clip byte start: no MP3 frame at byte %d of %d before %.3f s;"
                       " no clip is served", pos, size, start_s)
        return None
    return pos


def play_window(con, evidence_id, span, clip=None, *, memo=None) -> tuple | None:
    """(from, to) in episode time a read's clip plays: from its lead-in
    start (span start - customer.read_lead_in, never before the clip start)
    to its span end. None when the span is not valid (_span_end). The one
    rule for the media route's bytes and the record payload's timeline.
    memo: read_lead_in's, shared by one page's rows."""
    end = _span_end(span, clip)
    if end is None:
        return None
    from . import customer
    start = float(span[0])
    lo = start - customer.read_lead_in(con, evidence_id, start, memo)
    return (max(lo, float(clip["start_s"])) if clip is not None else lo), end


def clip_play_window(con, resolved: dict, clip=None, *, memo=None) -> tuple | None:
    """play_window of a publication's read, on its display span
    (customer.read_display_span, as clip_span_end); None without one.
    memo: play_window's, shared by one page's rows (Search items)."""
    if resolved.get("kind") != "evidence":
        return None
    from . import customer
    return play_window(con, resolved["record_id"],
                       customer.read_display_span(con, resolved["record_id"]), clip, memo=memo)


def clip_span_end(con, resolved: dict, clip=None) -> float | None:
    """The episode time a publication's clip plays to, as the players
    compute it (EWUI.playSpan(read, current)): an evidence publication's
    read display_span (customer.read_display_span, the record payload's
    read block), on every ref of the read, current or historical
    (LEDGER-LEAD3-CS-3); a row a later load retired has its own last one
    (CS-5). None when there is no valid span (none kept, the row gone, an
    invalid span, one the clip holds none of) or no evidence: never the
    detected or stored bounds. A hosted request then gets no clip
    (recording_chunk)."""
    if resolved.get("kind") != "evidence":
        return None
    from . import customer
    return _span_end(customer.read_display_span(con, resolved["record_id"]), clip)


def _hosted_window(con, resolved: dict, clip: dict, refused: Exception) -> tuple:
    """clip_play_window for an audio-gated request, which gets no clip
    without one (`refused`, the answer the route gives a guard-hidden read):
    logged once, numbers only (LEDGER-LEAD3-CS-5)."""
    window = clip_play_window(con, resolved, clip)
    if window is None:
        LOGGER.warning("clip byte cap: the read has no valid display span in the clip; no clip"
                       " (start %.3f s, %d bytes) is served to a hosted request",
                       clip["start_s"], clip["bytes"])
        raise refused
    return window


def _clip_chunk(clip: dict, range_header: str | None, span_end_s=None, *,
                play_from_s=None) -> dict:
    """Release at most one MiB of a registered clip, referenced in place;
    its bytes must still match the export's sha256. With span_end_s (the
    read's span end in episode time, clip_span_end) only the bytes up to
    clip_byte_cap at span_end_s + CLIP_STOP_TOLERANCE_S exist (CLIP-STOP);
    with play_from_s (the play window's start, play_window) only those from
    clip_byte_start at it (CLIP-START). Every Range form, the 200 path and
    Content-Range see the bytes between as the whole file. Only an
    audio-gated request passes them (recording_chunk)."""
    import hashlib
    import re
    from . import citations, ops
    path = Path(clip["path"])
    try:
        data = path.read_bytes()
    except OSError:
        raise citations.RevisionUnavailable("evidence clip is unavailable")
    if len(data) != clip["bytes"] or hashlib.sha256(data).hexdigest() != clip["sha256"]:
        raise citations.RevisionUnavailable("evidence clip bytes changed")
    cap = clip_byte_cap(data, None if span_end_s is None
                        else span_end_s - clip["start_s"] + CLIP_STOP_TOLERANCE_S)
    first = clip_byte_start(data, None if play_from_s is None
                            else play_from_s - clip["start_s"])
    if first is None:
        raise citations.RevisionUnavailable("evidence clip is unavailable")
    data = data[first:cap]
    size = len(data)
    start, end = 0, size - 1
    if range_header:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
        if match is None or not any(match.groups()):
            raise ops.BadInput("invalid recording byte range")
        if match[1]:
            start = int(match[1])
            end = int(match[2]) if match[2] else end
        else:
            suffix = int(match[2])
            start = max(0, size - suffix) if suffix else size
        if start >= size or end < start:
            raise ops.BadInput("recording byte range is outside the file")
    elif size > 1 << 20:
        raise ops.BadInput("large recording reads require a byte Range")
    end = min(end, size - 1, start + (1 << 20) - 1)
    return {"payload": data[start:end + 1], "mime": "audio/mpeg",
            "status": 206 if range_header else 200,
            "content_range": f"bytes {start}-{end}/{size}" if range_header else None}


def audio_gated(ctx) -> bool:
    """The audio policy applies to every context but the local app's (none,
    or the local operator), which no hosted route builds."""
    return ctx is not None and ctx.credential_kind != "local_operator"


def _publication_episode(con, ref: str) -> str:
    """The episode a publication pins, from its stored pins alone: a
    database read, no media descriptor and no file."""
    import json
    from . import citations
    state_id, _window = citations._parse_source_ref(ref)
    row = con.execute("SELECT pins_json FROM source_states WHERE id=?", (state_id,)).fetchone()
    if row is None:
        raise citations.RevisionUnavailable(f"unknown source publication: {ref!r}")
    return json.loads(row[0])["episode_id"]


def recording_chunk(con, ref: str, range_header: str | None = None, *, ctx=None) -> dict:
    """Release at most one MiB of an authorized pinned recording."""
    import hashlib
    import re
    from . import citations, ops
    from .access import AuthorizationError
    # Audio policy (audio clips design 5.2): a hosted credential other than
    # a member's browser session is refused before any query or file open;
    # then the publication's episode audio row must allow, read from its
    # stored pins before resolution (which proves pinned recordings on
    # disk). The local app (no context, or the local operator) is unchanged.
    audio_gate = audio_gated(ctx)
    if audio_gate:
        ctx.require_audio_credential()
        episode_id = _publication_episode(con, ref)
        ctx.authorize_audio(con, episode_id, capture=True)
    resolved = citations.resolve_source_state(con, ref, ctx=ctx)
    # CLIP-STOP and CLIP-START cut a clip only where the audio policy gates
    # (every hosted request); the local app keeps the whole clip for its Ad
    # review page (lead decision CS-2).
    if resolved.get("disclosure") == "excerpt":
        # Excerpt level (design 7): a qualifying registered clip only.
        clip = excerpt_clip(con, resolved)
        if clip is None:
            raise AuthorizationError("clip unavailable", 403)
        window = _hosted_window(con, resolved, clip, AuthorizationError(
            "clip unavailable", 403)) if audio_gate else (None, None)
        return _clip_chunk(clip, range_header, window[1], play_from_s=window[0])
    if "pins" not in resolved:
        raise AuthorizationError("recording disclosure denied", 403)
    # Safety check: resolution reads the same stored pins, so the episodes
    # agree for every stored publication; kept to fail closed if they ever
    # diverge (no test can reach it).
    if audio_gate and resolved["episode"]["episode_id"] != episode_id:
        raise AuthorizationError("recording disclosure denied", 403)
    episode = dict(resolved["episode"]["public"], id=resolved["episode"]["episode_id"],
                   transcript_version_id=resolved["pins"]["transcript_version_id"])
    proof = recording_proof(con, episode, resolved["pins"].get("recording"))
    if proof is None or proof["alignment"] == "mismatch":
        # A read's registered evidence clip plays when the episode has no
        # pinned recording (validation plan A5).
        clip = clip_for_publication(con, resolved)
        if clip is not None:
            # A clip plays only for evidence pinned to the transcript
            # version it was cut against (design 4.5).
            if clip["transcript_version_id"] != resolved["pins"]["transcript_version_id"]:
                raise AuthorizationError("the clip belongs to another transcript version", 403)
            window = _hosted_window(con, resolved, clip, citations.RevisionUnavailable(
                "pinned recording is unavailable")) if audio_gate else (None, None)
            return _clip_chunk(clip, range_header, window[1], play_from_s=window[0])
        raise citations.RevisionUnavailable("pinned recording is unavailable")
    size = proof["size_bytes"]
    start, end = 0, size - 1
    if range_header:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
        if match is None or not any(match.groups()):
            raise ops.BadInput("invalid recording byte range")
        if match[1]:
            start = int(match[1])
            end = int(match[2]) if match[2] else end
        else:
            suffix = int(match[2])
            start = max(0, size - suffix) if suffix else size
        if start >= size or end < start:
            raise ops.BadInput("recording byte range is outside the file")
    elif size > 1 << 20:
        raise ops.BadInput("large recording reads require a byte Range")
    end = min(end, size - 1, start + (1 << 20) - 1)
    digest, captured, offset = hashlib.sha256(), bytearray(), 0
    with proof["path"].open("rb") as recording:
        for block in iter(lambda: recording.read(1 << 20), b""):
            digest.update(block)
            if offset <= end and offset + len(block) > start:
                captured.extend(block[max(0, start - offset):min(len(block), end + 1 - offset)])
            offset += len(block)
    if offset != size or digest.hexdigest() != proof["digest"]:
        raise citations.RevisionUnavailable("recording bytes changed during capture")
    if ctx is not None:
        ctx.recordings[ref] = resolved["pins"]["recording"]
    mime = {"wav": "audio/wav", "mp3": "audio/mpeg", "m4a": "audio/mp4",
            "mp4": "audio/mp4", "aac": "audio/aac", "ogg": "audio/ogg",
            "flac": "audio/flac"}.get(proof["format"], "application/octet-stream")
    return {"payload": bytes(captured), "mime": mime, "status": 206 if range_header else 200,
            "content_range": f"bytes {start}-{end}/{size}" if range_header else None}


def register_legacy_local_audio(con, workspace_dir: Path, legacy_root: Path,
                                actor: str = "upgrade") -> dict:
    """Bind the old app's in-place pilot/benchmark audio as pinned recordings.

    Explicit upgrade-time step: candidates are episodes on the old app's
    exact allowlists (pilot_media_id / bench_media_id) with no recording
    yet; files resolve only under the explicit legacy_root (no default, no
    repository fallback). Each file is copied by content digest into
    workspace_dir/media (recording proof only accepts workspace files),
    registered with unknown rights (playback allowed, export/share denied),
    paired with the archived transcript over the recording interval at
    explicit offset 0, and bound through ops.set_episode_media, which
    verifies the proof, snapshots history, republishes and audits.

    Alignment authority is the 2026-09-24 listen audit: "verified" only
    for TRUST_EXACT episodes, otherwise "unknown".

    A recording that ends more than 1 s before the archived transcript's
    last segment is skipped as a wrong or truncated file.

    Runs inside the caller's transaction (each episode in its own
    savepoint). A missing file, unreadable duration or refused proof is a
    skipped entry, never an exception. Any other exception deletes the
    copies written so far and re-raises; the caller must roll back.
    created_files lists the workspace copies this call wrote and left
    referenced, so a caller whose outer transaction later rolls back can
    delete them.
    """
    import json
    import os
    import shutil
    import tempfile
    from earwitness.pipeline import artifacts
    from . import db, ops
    legacy_root = Path(legacy_root).resolve()
    workspace_dir = Path(workspace_dir)
    media_dir = workspace_dir / "media"
    registered, skipped, created = [], [], []
    rows = con.execute("SELECT id, revision, transcript_version_id FROM episodes"
                       " WHERE media_artifact_id IS NULL ORDER BY id").fetchall()
    target, made = None, False
    try:
        for row in rows:
            made = False
            episode_id = row["id"]
            pilot, bench = pilot_media_id(episode_id), bench_media_id(episode_id)
            if pilot is None and bench is None:
                continue
            found = resolve_pilot(legacy_root, pilot) if pilot is not None \
                else resolve_bench(legacy_root, bench)
            if found is None:
                skipped.append({"episode_id": episode_id, "reason": "legacy audio file not found"})
                continue
            source = found["path"]
            archived = con.execute("SELECT payload FROM source_versions WHERE id=?",
                                   (row["transcript_version_id"],)).fetchone()
            try:
                payload = json.loads(archived["payload"]) if archived is not None else None
                segments = payload if isinstance(payload, list) else payload["segments"]
                transcript_end = max((float(segment["end"]) for segment in segments), default=0.0)
            except (ValueError, KeyError, TypeError):
                segments = None
            if segments is None:
                skipped.append({"episode_id": episode_id, "reason": "no archived transcript"})
                continue
            ext = source.suffix.lower()
            fmt = ext.lstrip(".")
            digest, size = artifacts.sha256_file(source), source.stat().st_size
            locator = f"media/{digest}{ext}"
            target = workspace_dir / locator
            media_dir.mkdir(mode=0o700, exist_ok=True)
            os.chmod(media_dir, 0o700)
            if target.exists():
                if target.stat().st_size != size or artifacts.sha256_file(target) != digest:
                    skipped.append({"episode_id": episode_id,
                                    "reason": "existing workspace media copy differs from source"})
                    continue
            else:
                handle, temp = tempfile.mkstemp(dir=media_dir, prefix=".legacy-", suffix=ext)
                try:
                    os.fchmod(handle, 0o600)
                    with os.fdopen(handle, "wb") as out, source.open("rb") as src:
                        shutil.copyfileobj(src, out)
                    if os.path.getsize(temp) != size or artifacts.sha256_file(temp) != digest:
                        raise OSError(f"legacy audio copy does not match its source: {source}")
                    os.replace(temp, target)
                except BaseException:
                    Path(temp).unlink(missing_ok=True)
                    raise
                made = True
            duration = _recording_duration(target, fmt)
            reason = None
            if duration is None:
                reason = "recording duration unreadable"
            elif transcript_end > duration + 1.0:
                reason = "recording is shorter than its archived transcript"
            else:
                try:
                    with db.transaction(con, write=True):
                        artifact = artifacts.register_media(
                            con, digest=digest, size_bytes=size, duration_s=duration, format=fmt,
                            locator=locator, rights_state="unknown", fetch_date=None,
                            fetch_date_source="legacy-local-audio:"
                                              f"{source.relative_to(legacy_root).as_posix()}")
                        interval = [segment for segment in segments
                                    if segment["start"] < 0.0 + duration and segment["end"] > 0.0]
                        cur = con.execute(
                            "INSERT INTO transcript_versions (episode_id, media_artifact_id,"
                            " backend, segments, source_offsets, created_at)"
                            " VALUES (?, ?, 'legacy-local-audio', ?, ?, ?)",
                            (episode_id, artifact["id"], json.dumps(interval),
                             json.dumps({"global_start_s": 0.0}), db.utcnow()))
                        alignment = "verified" if audio_trust(episode_id) == "exact" else "unknown"
                        ops.set_episode_media(con, episode_id,
                                              {"media_artifact_id": artifact["id"],
                                               "alignment": alignment},
                                              expected_revision=row["revision"], actor=actor)
                except (ops.BadInput, ops.Conflict, artifacts.ArtifactError, KeyError,
                        TypeError) as exc:
                    reason = f"recording binding refused: {exc}"
            if reason is not None:
                skipped.append({"episode_id": episode_id, "reason": reason})
                if made and con.execute("SELECT 1 FROM media_artifacts WHERE locator=?",
                                        (locator,)).fetchone() is None:
                    target.unlink(missing_ok=True)
                continue
            if made and artifact["locator"] != locator and con.execute(
                    "SELECT 1 FROM media_artifacts WHERE locator=?", (locator,)).fetchone() is None:
                target.unlink(missing_ok=True)  # an existing artifact owns these bytes
            elif made:
                created.append(str(target))
            registered.append({"episode_id": episode_id,
                               "source": source.relative_to(legacy_root).as_posix(),
                               "bytes": size, "sha256": digest, "duration_s": duration,
                               "artifact_id": artifact["id"], "locator": artifact["locator"],
                               "alignment": alignment,
                               "pipeline_transcript_version_id": cur.lastrowid,
                               # A recording much longer than its transcript
                               # (e.g. inserted ads) is visible here for review.
                               "transcript_end_s": transcript_end,
                               "episode_revision_before": row["revision"],
                               "episode_revision_after": con.execute(
                                   "SELECT revision FROM episodes WHERE id=?",
                                   (episode_id,)).fetchone()[0]})
    except BaseException:
        # Unexpected failure: the caller's transaction rolls the rows back,
        # so no copy written here may survive.
        for path in created + ([str(target)] if made and target is not None else []):
            Path(path).unlink(missing_ok=True)
        raise
    if registered:
        # The binding is a recorded episode revision made by this migration,
        # not an owner change: conversion reads this record so a schema-7
        # tracker baseline does not report it as news.
        bound = legacy_audio_registrations(con)
        bound.update({entry["episode_id"]: {key: entry[key] for key in (
            "episode_revision_before", "episode_revision_after", "artifact_id", "sha256")}
            for entry in registered})
        con.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key)"
                    " DO UPDATE SET value=excluded.value",
                    (LEGACY_AUDIO_KEY, json.dumps(bound, sort_keys=True)))
    return {"legacy_root": str(legacy_root), "registered": registered,
            "skipped": skipped, "created_files": created}


LEGACY_AUDIO_KEY = "legacy_audio_registration"


def legacy_audio_registrations(con) -> dict:
    """Episodes whose legacy local audio the upgrade bound, with the
    episode revision before and after that binding."""
    import json
    row = con.execute("SELECT value FROM meta WHERE key=?", (LEGACY_AUDIO_KEY,)).fetchone()
    try:
        value = json.loads(row[0]) if row else {}
    except (ValueError, TypeError):
        value = {}
    return value if isinstance(value, dict) else {}


# ------------------------------------------------ expected-volume guard
# Audio clips design 4.7 (revision 13, CLIP-R13-01): on a pilot host
# (EARWITNESS_PILOT_LAYOUT set) the clip inputs live on the import volume.
# The media route runs scripts/volume_guard.py's check after the
# credential-first refusal and before any database or file access, and
# again after the read before the buffered response is released.

def _volume_guard():
    module = sys.modules.get("scripts.volume_guard")
    if module is None:
        try:
            from scripts import volume_guard as module
        except ImportError:   # the app runs without the checkout root on sys.path
            import importlib.util
            path = Path(__file__).resolve().parents[2] / "scripts" / "volume_guard.py"
            spec = importlib.util.spec_from_file_location("scripts.volume_guard", path)
            module = importlib.util.module_from_spec(spec)
            sys.modules["scripts.volume_guard"] = module
            spec.loader.exec_module(module)
    return module


def import_volume_code(environ=None) -> str | None:
    """None when this host has no pilot layout or its import volume passes
    the guard; else VOLUME_ABSENT or VOLUME_WRONG."""
    layout = (os.environ if environ is None else environ).get("EARWITNESS_PILOT_LAYOUT")
    if not layout:
        return None
    guard = _volume_guard()
    try:
        guard.check(layout)
    except guard.VolumeRejected as exc:
        if guard.UNCHECKABLE in str(exc):   # an unexpected error: keep its reason (CLIP4-R3-02)
            LOGGER.warning("import volume guard: %s", exc)
        return exc.code
    return None


def require_import_volume() -> None:
    """The route's existing "unavailable" refusal when the guard fails."""
    from . import citations
    if import_volume_code() is not None:
        raise citations.RevisionUnavailable("recording is unavailable")
