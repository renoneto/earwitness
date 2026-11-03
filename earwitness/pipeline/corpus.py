"""Corpus runner: crash-safe, resumable, memory-bounded RSS -> transcript ->
ad-read detection -> [Jev window screen] -> [brand naming] -> [speaker
diarization] -> [LLM screen] -> evidence clip pipeline over a SEPARATE
corpus directory.

Stages: fetch -> transcribe -> detect -> [jev] -> [name] -> [diarize] ->
[screen] -> clip -> done. Audio is kept until the clip stage has published
the episode's evidence clips; only then is it deleted. `jev`, `name`,
`diarize` and `screen` are optional: each is enabled for the corpus the
first time a worker starts with it in --stages (jev needs TYPESAFE_API_KEY;
name runs locally, with GLiNER when `gliner` is installed; diarize runs
Nemotron-3-Diarization locally in a supervised child under a python with
requirements-diarize.txt; screen needs ANTHROPIC_API_KEY and `pip install
anthropic`), and episodes are routed to it only while a worker running it
has been seen recently; otherwise they skip it. `name` follows only a
successful `jev`; `diarize` follows detect/jev/name whichever ran last, and
a diarize failure never kills an episode (it continues without speakers).

Layout under <corpus>/:
  queue.sqlite3   episode queue, append-only fetch observations, stage runs
  audio-tmp/      fetched audio held until clipping (bounded by backpressure)
  work/           per-attempt child output (never read as a result)
  transcripts/    <episode_id>.json, written atomically after validation
  detections/     <episode_id>.json, written atomically
  jev/            <episode_id>.json, Jev noul score per 60-s window + usage + cost
  names/          <episode_id>.json, named ad spans built from flagged Jev windows
  speakers/       <episode_id>.json, speaker turns from the diarize stage (diarize.py)
  brand-list.json the brand namer's repair list (harvested from transcripts/)
  brand-canon.json optional corpus-wide brand canonicalization map
                  (`corpus_run.py canon --apply`); export applies it to named reads
  screens/        <episode_id>.json, LLM screen reads + usage + cost
  clips/          <episode_id>/<start_ms>-<end_ms>.mp3 + index.json (shape: CLIP_INDEX_DOC;
                  read it with read_clip_index, which accepts every older shape)
  refined/        <episode_id>.json, evidence clips re-transcribed by `refine` (a
                  stronger model); names/<ep>.json is then rewritten from that text
                  ("refined": true, the earlier naming kept under "previous")

The database is the only source of truth for progress: a file on disk never
means "done". Every lease-holder write is fenced on (episode_id, lease_token),
so a worker that lost its lease can never overwrite a reclaimer's state.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from urllib.parse import urlparse

from earwitness.pipeline import acquisition as acq
from earwitness.pipeline import brand_canon
from earwitness.pipeline import brand_namer as bn
from earwitness.pipeline import chunked_stt as stt
from earwitness.pipeline import diarize as dz
from earwitness.pipeline import jev_budget
from earwitness.pipeline import jev_screen
from earwitness.pipeline import llm_screen
from earwitness.pipeline import self_promo
from earwitness.pipeline import shownotes
from earwitness.pipeline import transcript_quality as tq
from earwitness.pipeline.chunked_stt import (
    TranscriptInvalid, atomic_write_json, validate_transcript,
)

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "scripts" / "corpus_run.py"
STAGES = ("fetch", "transcribe", "detect", "jev", "name", "diarize", "screen", "clip")
DEFAULT_STAGES = ("fetch", "transcribe", "detect", "clip")  # jev, name, diarize, screen: opt-in
STAGE_ORDER = {s: i for i, s in enumerate(STAGES)}
NEXT_STAGE = {"fetch": "transcribe", "transcribe": "detect", "detect": "clip",
              "jev": "clip", "name": "clip", "diarize": "clip", "screen": "clip",
              "clip": "done"}
AUDIO_HOLDING_STAGES = ("detect", "jev", "name", "diarize", "screen", "clip")  # kept for clipping
# Older runners look for their own exact CHECK text in the stored schema and
# would otherwise rebuild the table with a CHECK that rejects newer stages.
# SQLite keeps comments in the stored schema, so the text of each older
# CHECK is kept in one: the jev-era CHECK (which itself carries the pre-jev
# text in a /* */ comment) sits in a -- line comment, as /* */ cannot nest.
_JEV_ERA_STAGE_CHECK = (
    "CHECK (stage IN ('fetch','transcribe','detect','jev','screen','clip',"
    "'done','dead')) /* pre-jev: CHECK (stage IN ('fetch','transcribe','detect',"
    "'screen','clip','done','dead')) */")
# The name-era CHECK (itself two lines, the second a -- comment) follows
# a "-- pre-diarize: " line comment, so its exact text stays in the schema.
_NAME_ERA_STAGE_CHECK = ("CHECK (stage IN ('fetch','transcribe','detect','jev','name','screen',"
                         "'clip','done','dead'))\n    -- pre-name: " + _JEV_ERA_STAGE_CHECK + "\n")
STAGE_CHECK = ("CHECK (stage IN ('fetch','transcribe','detect','jev','name','diarize','screen',"
               "'clip','done','dead'))\n    -- pre-diarize: " + _NAME_ERA_STAGE_CHECK)
CLIP_PAD_S = 15.0        # context on each side of a candidate / screen read
CLIP_MERGE_GAP_S = 10.0  # padded windows closer than this become one clip
CLIP_MAX_S = 180.0       # longer merged windows are split into equal parts
JEV_CLIP_PAD_S = 45.0    # context around Jev windows / named spans (refine re-transcribes it)
JEV_CLIP_MAX_S = 240.0   # split cap for merged windows that hold a Jev window / named span
CLIP_BITRATE = "64k"     # mono MP3
CLIP_INDEX_VERSION = 2
CLIP_INDEX_DOC = """clips/<episode_id>/index.json, schema_version 2 (written since 2026-10-10):
  {schema_version: 2, episode_id, status: "ok"|"skipped", reason (null or why skipped),
   created_at, audio_sha256 (the audio the clips were cut from; = the transcript's),
   fetch_observation_id, selection (what the clips were cut around), params {...},
   clips: [{file, start_s, end_s, start_ms, end_ms,
            duration_s (decoded seconds; the window length when the clip cannot be probed),
            bytes, sha256, sources ["rule"|"jev"|"named"|"screen"], jev_window_ids,
            candidate_ids, screen_read_ids, named_span_ids, pad_s}]}
Older indexes (no schema_version) lack selection, and their clips may lack sources,
jev_window_ids, named_span_ids or pad_s, or carry duration_s null; read_clip_index
fills them."""
CLIP_LIST_FIELDS = ("sources", "jev_window_ids", "candidate_ids", "screen_read_ids",
                    "named_span_ids")
SCREEN_WORKER_TTL_S = 600.0  # route to screen/jev/name/diarize only if such a worker was seen
# Diarize child: RSS cap and wall timeout (base + factor x audio seconds).
# Measured on an M1 Pro (mps, 1800-s windows): see requirements-diarize.txt.
DIARIZE_MEM_CAP_MB = 4500.0   # measured peak 2.9 GB (62-min episode, mps, 1800-s windows)
DIARIZE_TIMEOUT_BASE_S = 600.0
DIARIZE_TIMEOUT_FACTOR = 0.25   # 4x real time; measured ~180x on mps, ~70x on cpu
# A worker whose diarize child fails the same way on this many different
# episodes in a row is broken (deps, device, cache), not the episodes: it
# releases the lease uncharged and stops (WorkerFatal) instead of moving the
# corpus on to clip, which deletes the audio, without speakers.
DIARIZE_BREAKER_EPISODES = 3
# A Parakeet worker whose gap fill fails on this many episodes in a row is
# broken (mlx-whisper, weights, GPU): each transcript is still kept (without
# gap fill, gap_fill_status records the error), then the worker stops
# (WorkerFatal) instead of filling the corpus with unfilled transcripts.
GAP_FILL_BREAKER = 3
# A transient database error (sqlite "locked"/"busy") that do_diarize does
# not handle gives the lease back uncharged at most this many times per
# episode over its whole life (counted in stage_runs, so across restarts,
# workers and attempts); after that, and for any other error, the attempt is
# charged. Free releases do not count toward --max-episodes.
DIARIZE_DB_RELEASES = 3
DIARIZE_DB_RELEASE_REASON = "diarize database error: "
DIARIZE_DB_RETRY_S = 1.0   # wait before retrying a locked pre-publish heartbeat once
# A free release after a database lock sets not_before this far ahead
# (doubling with each earlier free release of the episode), so the worker
# that hit the lock does not claim the same episode again at once.
DIARIZE_DB_RELEASE_BACKOFF_S = 30.0
DB_RELEASED = "db_released"   # run_lease result of a free release
# Disk guard (--min-free-gb): below the floor a worker claims no fetch (the
# stage that fills the disk; the others drain audio to clip, which frees
# it). Below DISK_CLIP_ONLY_GB it claims only clip, so a nearly full disk
# does not turn into failed attempts and dead episodes.
DISK_CLIP_ONLY_GB = 1.0
# The ad spans export measures voice_share_outside_ads against (recorded in
# each episode's voice_provenance).
VOICE_AD_SPANS = (f"named spans, Jev windows at >= {jev_screen.DEFAULT_THRESHOLD:g}, "
                  "screen reads")
NO_SCREEN_WORKER = "skipped: no screen worker"
NO_JEV_WORKER = "skipped: no jev worker"
NO_NAME_WORKER = "skipped: no name worker"
NO_JEV_RESULT = "skipped: no jev result"
NO_DIARIZE_WORKER = "skipped: no diarize worker"
# Jev spend cap (jev_budget) refused the request: no Jev result, the episode
# goes on as with no jev worker ("Detection incomplete").
JEV_SKIPPED_BUDGET = "jev_skipped_budget"
JEV_ALERT_COOLDOWN_S = 3 * 3600.0


def jev_budget_alert(corpus, code: str, message: str, log=print) -> None:
    """One line in <corpus>/alerts.log (the fleet's alert file, same format)
    with the budget alert code; codes and counts only."""
    line = f"{utcnow()} {code}: {message}"
    try:
        with open(Path(corpus) / "alerts.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as exc:
        log(f"cannot write alerts.log ({exc})")
    log(f"ALERT {code}: {message}")
# Name stage: 60-s windows of context on each side of a flagged window.
# Measured (named_reads_eval.py): on dev, context 1 lowered protocol recall
# (0.64 -> 0.61) and strict recall (0.43 -> 0.32) by merging back-to-back
# spots, while raising precision (0.62 -> 0.74); recall wins, so 0.
DEFAULT_JEV_CONTEXT_WINDOWS = 0
# Name stage sub-window naming (brand_namer.name_windows sub_window_s: one
# span per distinct brand inside flagged windows). Off: on dev (6 episodes)
# 20-s sub-windows raised protocol P/R 0.62/0.64 -> 0.77/0.86 and strict
# 0.41/0.43 -> 0.52/0.57, but the 12-episode holdout was flat (protocol
# 0.52/0.48 -> 0.50/0.48, strict 0.29/0.26 -> 0.28/0.26). None names whole
# 60-s windows; --name-sub-window-s 20 turns it on.
DEFAULT_NAME_SUB_WINDOW_S = None
BRAND_LIST_NAME = "brand-list.json"
OUTCOMES = ("ok", "error", "oom", "timeout", "lease_expired", "killed")
USER_AGENT = "EarWitnessCorpus/0.1 (research)"
FEED_TIMEOUT_S = 30.0
FEED_DEADLINE_S = 180.0  # whole download; a 62 MB feed took over 30 s on a loaded host
FEED_MAX_BYTES = 128 * 1024 * 1024  # biggest measured 2026-10: Clay Travis 85 MB, The Herd 62 MB
# feedparser holds the whole parsed feed: peak RSS ~18x the feed's bytes
# (1.1 GB for a 62 MB feed, ~1.5 GB for an 85 MB one). Feeds being parsed
# at once share this many bytes; a bigger feed parses alone.
PARSE_BUDGET_BYTES = 40 * 1024 * 1024
MEDIA_MAX_BYTES = 800 * 1024 * 1024
TERMINAL_FETCH_ERRORS = (acq.OverBudget,)  # deterministic: retrying cannot succeed
MEDIA_DEADLINE_S = 1800.0
AUDIO_SUFFIXES = (".mp3", ".m4a", ".aac", ".ogg", ".wav", ".mp4", ".opus", ".flac")
GC_MIN_AGE_S = 2 * 3600
# Coverage tiers (shows.tier, copied to each episode when discover queues it):
# "full" shows run the whole pipeline; "transcript" shows only what their
# stage set allows (default fetch + transcribe: searchable, no ad rows).
# A corpus setting (meta tier_stages:<tier>, `set-tier --stages`) overrides
# the default stage set; None = every stage.
TIERS = ("full", "transcript")
DEFAULT_TIER_STAGES = {"full": None, "transcript": ("fetch", "transcribe")}
# Claim order across tiers (`work --tier-priority`): a later tier's
# episodes are claimed only when none of an earlier tier's are claimable.
TIER_PRIORITIES = {"full": ("full", "transcript"), "transcript": ("transcript", "full"),
                   "none": (None,)}


def buffer_caps(max_buffered, max_buffered_transcript=None) -> dict:
    """{tier: fetch buffer cap}. Without a transcript cap, one cap (key None)
    counts both tiers' audio; with it, each tier's audio counts against its
    own cap only (`work --max-buffered-transcript`). A None cap: no limit."""
    if max_buffered_transcript is None:
        return {None: max_buffered}
    return {"full": max_buffered, "transcript": max_buffered_transcript}


def buffer_cap(value) -> int:
    """argparse type of a fetch buffer cap: 0 or more. 0 pauses fetch for the
    episodes that buffer counts (one tier, or both with the shared cap)."""
    n = int(value)
    if n < 0:
        raise ValueError(f"a fetch buffer cap must be 0 or more, not {n}")
    return n


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS shows (
  feed_url TEXT PRIMARY KEY, title TEXT, category TEXT, source TEXT,
  last_fetch_at TEXT, last_status TEXT, last_error TEXT, language TEXT,
  tier TEXT NOT NULL DEFAULT 'full');
CREATE TABLE IF NOT EXISTS episodes (
  episode_id TEXT PRIMARY KEY, feed_url TEXT NOT NULL, guid TEXT, identity_basis TEXT,
  show_title TEXT, category TEXT, ep_title TEXT, pub_date TEXT, enclosure_url TEXT,
  discovered_at TEXT,
  stage TEXT NOT NULL DEFAULT 'fetch'
    """ + STAGE_CHECK + """,
  state TEXT NOT NULL DEFAULT 'ready' CHECK (state IN ('ready','leased')),
  lease_owner TEXT, lease_token TEXT, lease_until REAL, lease_run_id INTEGER,
  not_before REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT, audio_path TEXT, audio_sha256 TEXT, audio_bytes INTEGER,
  duration_s REAL, fetch_observation_id INTEGER, updated_at TEXT,
  screen_status TEXT, dead_stage TEXT, jev_status TEXT, name_status TEXT,
  diarize_status TEXT, gap_fill_status TEXT, repair_requeued_at TEXT,
  tier TEXT NOT NULL DEFAULT 'full');
CREATE INDEX IF NOT EXISTS episodes_claim ON episodes (stage, state, not_before);
CREATE TABLE IF NOT EXISTS fetch_observations (
  id INTEGER PRIMARY KEY, episode_id TEXT NOT NULL, started_at TEXT, finished_at TEXT,
  enclosure_url TEXT, final_url TEXT, http_status INTEGER, redirect_chain TEXT,
  headers TEXT, bytes INTEGER, sha256 TEXT, duration_s REAL, worker TEXT, host TEXT, verification TEXT);
CREATE INDEX IF NOT EXISTS fetch_obs_episode ON fetch_observations (episode_id, started_at);
CREATE TABLE IF NOT EXISTS stage_runs (
  id INTEGER PRIMARY KEY, episode_id TEXT NOT NULL, stage TEXT NOT NULL,
  attempt INTEGER, worker TEXT, host TEXT, lease_token TEXT,
  started_at TEXT, finished_at TEXT, wall_s REAL, cpu_s REAL, audio_s REAL,
  peak_rss_mb REAL,
  outcome TEXT CHECK (outcome IS NULL OR outcome IN
    ('ok','error','oom','timeout','lease_expired','killed')),
  error TEXT, llm_usage TEXT, cost_usd REAL);
CREATE INDEX IF NOT EXISTS stage_runs_episode ON stage_runs (episode_id, stage);
CREATE TABLE IF NOT EXISTS workers (
  worker_id TEXT PRIMARY KEY, host TEXT, stages TEXT, started_at TEXT, last_seen REAL);
CREATE TABLE IF NOT EXISTS discovery_log (
  id INTEGER PRIMARY KEY, command TEXT NOT NULL, started_at TEXT, finished_at TEXT,
  args TEXT, report TEXT);
CREATE TRIGGER IF NOT EXISTS fetch_observations_append_only
  BEFORE UPDATE ON fetch_observations
  BEGIN SELECT RAISE(ABORT, 'fetch_observations is append-only'); END;
"""


# Columns added after corpora already existed: (table, column, type).
SCHEMA_ADDITIONS = (("fetch_observations", "verification", "TEXT"),
                    ("stage_runs", "llm_usage", "TEXT"),
                    ("stage_runs", "cost_usd", "REAL"),
                    ("episodes", "screen_status", "TEXT"),
                    ("episodes", "dead_stage", "TEXT"),
                    ("episodes", "jev_status", "TEXT"),
                    ("episodes", "name_status", "TEXT"),
                    ("episodes", "diarize_status", "TEXT"),
                    ("episodes", "gap_fill_status", "TEXT"),
                    ("shows", "apple_id", "INTEGER"),
                    ("shows", "language", "TEXT"),
                    ("episodes", "repair_requeued_at", "TEXT"),
                    ("shows", "tier", "TEXT NOT NULL DEFAULT 'full'"),
                    ("episodes", "tier", "TEXT NOT NULL DEFAULT 'full'"))


def ensure_schema(con) -> None:
    """Create missing tables and add later columns to existing corpora.
    Idempotent and safe against concurrent workers (re-checked under the
    write lock). ADD COLUMN leaves the append-only trigger in place."""
    con.executescript(SCHEMA)
    for table, column, kind in SCHEMA_ADDITIONS:
        if column in {r[1] for r in con.execute(f"PRAGMA table_info({table})")}:
            continue
        with _Tx(con):
            if column not in {r[1] for r in con.execute(f"PRAGMA table_info({table})")}:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    _migrate_stage_check(con)
    _ensure_feed_index(con)
    _ensure_tier_objects(con)


def _ensure_tier_objects(con) -> None:
    """The claim index led by tier: a full-first claim never walks the
    transcript tier's backlog. Built like the feed index: the table is read
    first without a lock, so the one-time build holds the write lock only
    briefly."""
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='index'"
                   " AND name='episodes_tier_claim'").fetchone():
        return
    con.execute("SELECT max(length(tier)), max(not_before) FROM episodes").fetchone()
    con.execute("CREATE INDEX IF NOT EXISTS episodes_tier_claim"
                " ON episodes (tier, stage, state, not_before)")


def check_tier(tier) -> str:
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}; use one of {', '.join(TIERS)}")
    return tier


def tier_stages(con, tier: str):
    """The stages a tier's episodes go through (None: every stage)."""
    row = con.execute("SELECT value FROM meta WHERE key=?", (f"tier_stages:{tier}",)).fetchone()
    if row is None:
        return DEFAULT_TIER_STAGES.get(tier)
    stages = json.loads(row[0])
    return None if stages is None else tuple(stages)


def tier_stage_lists(con) -> dict:
    """{tier: its stage list, or None for every stage} (JSON-ready)."""
    return {t: None if (st := tier_stages(con, t)) is None else list(st) for t in TIERS}


def tiers_running(con, stage: str) -> tuple:
    """The tiers whose stage set includes `stage` (backfills select only
    their episodes, so a transcript tier is never Jev-screened or diarized)."""
    return tuple(t for t in TIERS if (st := tier_stages(con, t)) is None or stage in st)


def tier_next(nxt: str, allowed) -> str:
    """Where an episode goes instead of `nxt` when its tier's stage set
    leaves `nxt` out: the next allowed stage in pipeline order, else done.
    Stages only ever move forward past detect, so this keeps every route's
    order; moves back (refetch, retranscribe) stay as they are, since fetch
    and transcribe are in every stage set."""
    if allowed is None or nxt not in STAGE_ORDER or nxt in allowed:
        return nxt
    return next((s for s in STAGES if STAGE_ORDER[s] > STAGE_ORDER[nxt] and s in allowed),
                "done")


def _ensure_feed_index(con) -> None:
    """Index episodes by feed. Corpora made by older code lack it; the
    one-time build holds the write lock while it runs (seconds at 180k
    episodes on a cold disk), so read the table first without any lock and
    let the build work from the page cache."""
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='index'"
                   " AND name='episodes_feed'").fetchone():
        return
    con.execute("SELECT max(length(feed_url)) FROM episodes").fetchone()
    con.execute("CREATE INDEX IF NOT EXISTS episodes_feed ON episodes (feed_url)")


def _episodes_sql(con) -> str:
    return con.execute("SELECT sql FROM sqlite_master WHERE type='table'"
                       " AND name='episodes'").fetchone()[0]


def _migrate_stage_check(con) -> None:
    """Corpora created before the screen/clip stages carry a CHECK that
    rejects them; SQLite cannot alter a CHECK, so rebuild the table (rowids
    and every row kept) under the write lock. Idempotent."""
    if STAGE_CHECK in _episodes_sql(con):
        return
    with _Tx(con):
        if STAGE_CHECK in _episodes_sql(con):
            return
        cols = ",".join(r[1] for r in con.execute("PRAGMA table_info(episodes)"))
        ddl = SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS episodes"):]
        ddl = ddl[:ddl.index(";")].replace("IF NOT EXISTS episodes", "episodes__new")
        con.execute(ddl)
        con.execute(f"INSERT INTO episodes__new (rowid, {cols}) SELECT rowid, {cols}"
                    " FROM episodes")
        con.execute("DROP TABLE episodes")
        con.execute("ALTER TABLE episodes__new RENAME TO episodes")
        con.execute("CREATE INDEX IF NOT EXISTS episodes_claim"
                    " ON episodes (stage, state, not_before)")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _shard_of(episode_id: str, n: int) -> int:
    return int(hashlib.sha256(episode_id.encode()).hexdigest()[:8], 16) % n


def connect(corpus) -> sqlite3.Connection:
    path = Path(corpus) / "queue.sqlite3"
    con = sqlite3.connect(str(path), timeout=15.0, isolation_level=None,
                          check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=15000")
    con.execute("PRAGMA synchronous=FULL")
    con.create_function("shard_of", 2, _shard_of, deterministic=True)
    return con


class _Tx:
    """BEGIN IMMEDIATE ... COMMIT, rolled back on any exception."""

    def __init__(self, con):
        self.con = con

    def __enter__(self):
        self.con.execute("BEGIN IMMEDIATE")
        return self.con

    def __exit__(self, exc_type, *_):
        self.con.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def corpus_dirs(corpus) -> dict:
    root = Path(corpus)
    return {name: root / name for name in ("audio-tmp", "work", "transcripts", "detections",
                                           "jev", "names", "speakers", "screens", "clips",
                                           "refined")}


def log_discovery(con, command: str, started_at: str, args, report) -> None:
    """Append one row to the discovery log: what ran, when, with which
    arguments and counts, so a corpus's contents stay explainable."""
    con.execute("INSERT INTO discovery_log (command, started_at, finished_at, args, report)"
                " VALUES (?,?,?,?,?)", (command, started_at, utcnow(),
                                        json.dumps(args, sort_keys=True, default=str),
                                        json.dumps(report, sort_keys=True)))


def init_corpus(corpus, shows: list, per_show: int, args=None) -> dict:
    started = utcnow()
    root = Path(corpus)
    root.mkdir(parents=True, exist_ok=True)
    for d in corpus_dirs(root).values():
        d.mkdir(exist_ok=True)
    con = connect(root)
    try:
        ensure_schema(con)
        with _Tx(con):
            con.execute("INSERT INTO meta (key, value) VALUES ('per_show', ?)"
                        " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(per_show),))
            for s in shows:
                if not s.get("feed_url"):
                    raise ValueError(f"show lacks feed_url: {s!r}")
                con.execute("INSERT INTO shows (feed_url, title, category, source, apple_id,"
                            " language, tier) VALUES (?, ?, ?, ?, ?, ?, ?)"
                            " ON CONFLICT(feed_url) DO UPDATE"
                            " SET title=excluded.title, category=excluded.category,"
                            " source=excluded.source,"
                            " apple_id=COALESCE(excluded.apple_id, apple_id),"
                            " language=COALESCE(excluded.language, shows.language)",
                            (s["feed_url"], s.get("title"), s.get("category"), s.get("source"),
                             s.get("apple_id"), show_language(s.get("language")),
                             check_tier(s.get("tier") or "full")))
            n = con.execute("SELECT COUNT(*) FROM shows").fetchone()[0]
            report = {"corpus": str(root), "shows": n, "per_show": per_show}
            log_discovery(con, "init", started, args, dict(report, listed=len(shows)))
    finally:
        con.close()
    return report


def feed_key(url: str) -> str:
    """Feed identity for dedupe: scheme, "www.", host case and a trailing
    slash ignored."""
    p = urlparse(url.strip())
    host = (p.hostname or "").lower().removeprefix("www.")
    return host + p.path.rstrip("/") + (f"?{p.query}" if p.query else "")


def _fold_latin(c: str) -> str:
    """'é' -> 'e'; other scripts untouched (kana keep their voicing marks)."""
    d = unicodedata.normalize("NFD", c)
    return d[0] if d[0].isascii() else c


def norm_name(s) -> str:
    """Show-title identity: case, accents, punctuation and a leading "the"
    ignored; letters, combining marks and digits of every script kept
    (styled Unicode letters fold to plain ones)."""
    s = "".join(_fold_latin(c) for c in unicodedata.normalize("NFKC", s or "")).casefold()
    s = "".join(c if unicodedata.category(c)[0] in "LMN" else " "
                for c in s.replace("&", " and "))
    return re.sub(r"^the ", "", " ".join(s.split()))


def add_shows(corpus, shows: list, args=None, tier: str | None = None) -> dict:
    """Add the shows of a show list that the corpus lacks. A show whose
    feed is already there under another spelling (http/https, "www.",
    trailing slash) is skipped (`same_feed`); one whose Apple id is
    already there with another feed (a host move) is reported (`moved`),
    not added, since adding it would queue every episode again. Rows
    without an Apple id (older corpora) first get theirs from the list by
    feed; a show whose title matches such a row (and whose Apple id does
    not contradict it) is reported as `possible_move`, not added. The only
    change to an existing row is filling its empty apple_id. Episodes and
    the corpus meta are never changed, so it is safe beside live workers
    and re-running it changes nothing. An added show's tier is `tier`
    when given, else the list's "tier" tag, else full (`set-tier` changes
    the tier of shows already there)."""
    started = utcnow()
    if not (Path(corpus) / "queue.sqlite3").exists():
        raise FileNotFoundError(f"no corpus at {corpus} (run init first)")
    for s in shows:
        if not s.get("feed_url"):
            raise ValueError(f"show lacks feed_url: {s!r}")
        check_tier(tier or s.get("tier") or "full")
    con = connect(corpus)
    try:
        ensure_schema(con)
        with _Tx(con):
            report = {"added": 0, "present": 0, "apple_id_filled": 0, "same_feed": [],
                      "moved": [], "possible_move": [], "tiers": {}}
            by_key = {}
            for r in con.execute("SELECT feed_url FROM shows ORDER BY feed_url"):
                by_key.setdefault(feed_key(r[0]), r[0])
            for s in shows:  # backfill ids before matching: list order must not matter
                existing = by_key.get(feed_key(s["feed_url"]))
                if existing is not None and s.get("apple_id") is not None:
                    report["apple_id_filled"] += con.execute(
                        "UPDATE shows SET apple_id=? WHERE feed_url=? AND apple_id IS NULL",
                        (s["apple_id"], existing)).rowcount
            by_apple, by_title = {}, {}
            for r in con.execute("SELECT feed_url, apple_id, title FROM shows"
                                 " ORDER BY feed_url"):
                if r[1] is not None:
                    by_apple.setdefault(r[1], r[0])
                if norm_name(r[2]):
                    by_title.setdefault(norm_name(r[2]), []).append((r[0], r[1]))
            for s in shows:
                url, apple_id = s["feed_url"], s.get("apple_id")
                existing = by_key.get(feed_key(url))
                if existing is not None:
                    if existing == url:
                        report["present"] += 1
                    else:
                        report["same_feed"].append({"feed_url": url, "existing": existing})
                    continue
                if apple_id is not None and apple_id in by_apple:
                    report["moved"].append({"title": s.get("title"), "apple_id": apple_id,
                                            "feed_url": url, "existing": by_apple[apple_id]})
                    continue
                same_title = [feed for feed, aid in by_title.get(norm_name(s.get("title")), ())
                              if aid is None or apple_id is None]
                if same_title:
                    report["possible_move"].append({"title": s.get("title"), "apple_id": apple_id,
                                                    "feed_url": url, "existing": same_title[0]})
                    continue
                show_tier = tier or s.get("tier") or "full"
                con.execute("INSERT INTO shows (feed_url, title, category, source, apple_id,"
                            " language, tier) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (url, s.get("title"), s.get("category"), s.get("source"),
                             apple_id, show_language(s.get("language")), show_tier))
                report["added"] += 1
                report["tiers"][show_tier] = report["tiers"].get(show_tier, 0) + 1
                by_key[feed_key(url)] = url
                if apple_id is not None:
                    by_apple[apple_id] = url
                if norm_name(s.get("title")):
                    by_title.setdefault(norm_name(s.get("title")), []).append((url, apple_id))
            report["shows"] = con.execute("SELECT COUNT(*) FROM shows").fetchone()[0]
            log_discovery(con, "add-shows", started, args, report)
    finally:
        con.close()
    return report


def set_tier(corpus, tier: str, *, shows: list | None = None, top: dict | None = None,
             others: str | None = None, stages=None, include_done: bool = False,
             keep_audio: bool = False, dry_run: bool = False, args=None) -> dict:
    """Move shows to `tier`: those of the show list `shows` (matched by
    feed, any spelling), or with `top` ({apple_top, apple_genre_top,
    spotify_top, spotify_category_top}: n, see show_sources.within_top)
    those ranked within the limits, by the list's chart tags or, without a
    list, by the corpus's own (shows.source). `others`: every other show of
    the corpus moves to that tier. `stages` sets the tier's stage set
    (corpus meta tier_stages:<tier>; fetch and transcribe are required).

    An episode not yet done follows its show; a done one keeps the tier it
    was processed under (a show moved to full later keeps its older
    transcript-only episodes as such: `done_kept`), unless `include_done`,
    which re-tiers the done episodes of the selected shows (never of
    `others`): one that lacks the result of a stage its new tier adds (no
    detections, no Jev file and no jev_status, no names file and no
    name_status, no clip index; Jev and name only when enabled on the
    corpus) goes back to the first new stage after transcribe (promoted to
    full: detect; detect, Jev and name work from the transcript, clip skips
    the gone audio; `done_requeued`, per stage `done_requeue_stages`); any
    other is only re-labelled, keeping its evidence and statuses
    (`done_retiered`; export then follows its new tier's stage set). An
    episode waiting at a stage its new tier leaves out moves on at once
    (`episodes_moved_on`; its audio is deleted when that makes it done,
    unless `keep_audio`). `dry_run` writes nothing (a query-only
    connection) and reports the same counts, plus the Jev cost of the
    episodes it would requeue to Jev (`jev_estimate`). Each show is one short
    transaction that reads nothing from episodes (UPDATEs on the feed
    index), so it is safe beside live workers; an episode in flight finishes
    its current stage and then follows its new tier's stage set, and one
    leased but not yet started skips a left-out stage (Worker.run_lease)."""
    from earwitness.pipeline import show_sources as ss
    started = utcnow()
    check_tier(tier)
    if others is not None:
        check_tier(others)
    top = {k: v for k, v in (top or {}).items() if v}
    select = shows is not None or bool(top)
    if others is not None and not select:
        raise ValueError("--others needs a selection (--shows or a chart-rank argument)")
    if stages is not None:
        stages = tuple(stages)
        bad = [st for st in stages if st not in STAGES]
        if bad or not {"fetch", "transcribe"} <= set(stages):
            raise ValueError(f"tier stages must be known stages ({', '.join(STAGES)}) and "
                             f"include fetch and transcribe; got {','.join(stages)}")
    if not select and stages is None:
        raise ValueError("nothing to set: give --shows, a chart-rank argument or --stages")
    if not (Path(corpus) / "queue.sqlite3").exists():
        raise FileNotFoundError(f"no corpus at {corpus} (run init first)")
    con = connect(corpus)
    try:
        if dry_run:
            con.execute("PRAGMA query_only=1")
            if "tier" not in {r[1] for r in con.execute("PRAGMA table_info(episodes)")}:
                raise ValueError("this corpus has no coverage tiers yet: run any command of"
                                 " this version (e.g. status) first, then the dry run")
        else:
            ensure_schema(con)
        report = {"tier": tier, "others": others, "top": top, "selected": 0,
                  "not_in_corpus": [], "shows_changed": 0, "episodes_changed": 0,
                  "episodes_moved_on": 0, "done_kept": 0, "done_requeued": 0,
                  "done_retiered": 0, "done_requeue_stages": {}}
        if dry_run:
            report["dry_run"] = True
        if select:  # chosen before anything is written: an empty selection changes nothing
            current = {}
            for r in con.execute("SELECT feed_url, source, tier FROM shows ORDER BY feed_url"):
                current.setdefault(feed_key(r[0]), (r[0], r[1], r[2]))
            if shows is not None:
                picked = [s for s in shows if not top or ss.within_top(s.get("source"), **top)]
                report["not_in_corpus"] = [s["feed_url"] for s in picked
                                           if feed_key(s["feed_url"]) not in current]
                chosen = {current[feed_key(s["feed_url"])][0] for s in picked
                          if feed_key(s["feed_url"]) in current}
            else:
                if not any(ss.chart_ranks(source) for _, source, _ in current.values()):
                    raise ValueError(
                        "no show of the corpus carries chart-rank tags in its source (like"
                        " 'apple-top#5'), so rank arguments select nothing; pass the ranked"
                        " show list with --shows")
                chosen = {url for url, source, _ in current.values()
                          if ss.within_top(source, **top)}
            if not chosen:
                raise ValueError("the selection matches no show of the corpus; nothing changed"
                                 " (check --shows and the rank arguments)")
            report["selected"] = len(chosen)
        if stages is not None and not dry_run:
            with _Tx(con):
                con.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key)"
                            " DO UPDATE SET value=excluded.value",
                            (f"tier_stages:{tier}", json.dumps([st for st in STAGES
                                                               if st in stages])))
        if select:
            want = {url: tier if url in chosen else others for url, _, _ in current.values()
                    if url in chosen or others is not None}
            had = {url: old for url, _, old in current.values()}
            sets = {t: tier_stages(con, t) for t in TIERS}
            if stages is not None:  # as just written (a dry run writes nothing)
                sets[tier] = tuple(st for st in STAGES if st in stages)
            dirs = corpus_dirs(corpus)
            enabled = {"detect": True, "clip": True, "jev": bool(con.execute(
                "SELECT 1 FROM meta WHERE key='jev_model'").fetchone()), "name": bool(
                con.execute("SELECT 1 FROM meta WHERE key='name_pipeline'").fetchone())}
            jev_todo = []
            for url in sorted(want):  # every show: a discover racing an earlier
                new = want[url]       # set-tier may have queued at the old tier
                plan = _plan_done(con, dirs, url, new, sets, enabled, report, jev_todo) \
                    if include_done and url in chosen else []
                if dry_run:
                    _count_tier_move(con, url, new, sets[new], report)
                    for _, ids, rest in plan:
                        report["done_requeued"] += len(ids)
                        report["done_retiered"] += len(rest)
                else:
                    with _Tx(con):
                        if had[url] != new:
                            con.execute("UPDATE shows SET tier=? WHERE feed_url=?", (new, url))
                        report["episodes_changed"] += con.execute(
                            "UPDATE episodes SET tier=? WHERE feed_url=? AND tier<>?"
                            " AND stage NOT IN ('done','dead')", (new, url, new)).rowcount
                        moved, audio = _route_ready_to_tier(con, url, new, sets[new],
                                                            keep_audio)
                        _apply_done_plan(con, url, new, sets[new], plan, report)
                    report["episodes_moved_on"] += moved
                    _delete_unreferenced_audio(con, audio)
                report["shows_changed"] += had[url] != new
                report["done_kept"] += con.execute(  # a WAL read, outside the transaction
                    "SELECT COUNT(*) FROM episodes WHERE feed_url=? AND stage='done'"
                    " AND tier<>?", (url, new)).fetchone()[0] - (
                    sum(len(ids) + len(rest) for _, ids, rest in plan) if dry_run else 0)
            if dry_run:
                est = jev_estimate(corpus, jev_todo, model=(con.execute(
                    "SELECT value FROM meta WHERE key='jev_model'").fetchone() or
                    [jev_screen.DEFAULT_MODEL])[0])
                report.update(jev_episodes_est=est["episodes"],
                              jev_requests_est=est["requests_est"],
                              jev_cost_usd_est=est["cost_usd_est"])
        report["stages"] = tier_stage_lists(con)
        report["tiers"] = {r[0]: r[1] for r in con.execute(
            "SELECT tier, COUNT(*) FROM shows GROUP BY 1 ORDER BY 1")}
        if not dry_run:
            with _Tx(con):
                log_discovery(con, "set-tier", started, args, report)
    finally:
        con.close()
    return report


# Done-episode evidence per stage (set-tier --include-done): a file, or a status
# column that any outcome of the stage sets (ok, failed, skipped).
DONE_EVIDENCE = {"detect": ("detections", None), "jev": ("jev", "jev_status"),
                 "name": ("names", "name_status"), "clip": ("clips", None)}


def _plan_done(con, dirs, url, new, sets, enabled, report, jev_todo) -> list:
    """[(old tier, episode ids to requeue, ids to re-label)] for the show's
    done episodes of other tiers (a WAL read, outside any transaction). An
    episode is requeued only when it lacks the result of a stage its new
    tier adds (enabled stages with evidence, DONE_EVIDENCE); one that has
    every result keeps it and is only re-labelled."""
    plan = []
    for old in TIERS:
        if old == new:
            continue
        added = [st for st in DONE_EVIDENCE if enabled[st] and st in (sets[new] or STAGES)
                 and st not in (sets[old] or STAGES)]
        rows = con.execute("SELECT episode_id, jev_status, name_status FROM episodes"
                           " WHERE feed_url=? AND stage='done' AND tier=? ORDER BY episode_id",
                           (url, old)).fetchall()
        requeue, relabel = [], []
        for r in rows:
            ep_id, lacks = r[0], []
            for st in added:
                folder, column = DONE_EVIDENCE[st]
                path = dirs[folder] / (f"{ep_id}/index.json" if st == "clip" else f"{ep_id}.json")
                if not path.exists() and (column is None or r[column] is None):
                    lacks.append(st)
            (requeue if lacks else relabel).append(ep_id)
            for st in lacks:
                report["done_requeue_stages"][st] = report["done_requeue_stages"].get(st, 0) + 1
            if "jev" in lacks:
                jev_todo.append(ep_id)
        if rows:
            plan.append((old, requeue, relabel))
    return plan


def _apply_done_plan(con, url, new, allowed, plan, report) -> None:
    """Inside the caller's transaction; each UPDATE re-checks the row is
    still done at its old tier (by feed and id)."""
    for old, requeue, relabel in plan:
        for ids, requeued in ((requeue, True), (relabel, False)):
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                where = (" WHERE feed_url=? AND stage='done' AND tier=? AND episode_id IN (%s)"
                         % ",".join("?" * len(chunk)))
                if requeued:
                    n = con.execute("UPDATE episodes SET tier=?, stage=?, attempts=0,"
                                    " not_before=0, last_error='set-tier --include-done',"
                                    " updated_at=?" + where,
                                    (new, tier_next("detect", allowed), utcnow(), url, old,
                                     *chunk)).rowcount
                else:
                    n = con.execute("UPDATE episodes SET tier=?, updated_at=?" + where,
                                    (new, utcnow(), url, old, *chunk)).rowcount
                report["done_requeued" if requeued else "done_retiered"] += n


def _count_tier_move(con, url, new, allowed, report) -> None:
    """A dry run's counts for one show (reads only)."""
    report["episodes_changed"] += con.execute(
        "SELECT COUNT(*) FROM episodes WHERE feed_url=? AND tier<>?"
        " AND stage NOT IN ('done','dead')", (url, new)).fetchone()[0]
    out = [] if allowed is None else [s for s in STAGES if s not in allowed]
    if out:
        report["episodes_moved_on"] += con.execute(
            "SELECT COUNT(*) FROM episodes WHERE feed_url=? AND state='ready' AND stage IN (%s)"
            % ",".join("?" * len(out)), (url, *out)).fetchone()[0]


def _route_ready_to_tier(con, feed_url: str, tier: str, allowed, keep_audio: bool = False
                         ) -> tuple:
    """Inside the caller's transaction: the show's ready rows waiting at a
    stage `tier` leaves out move on (tier_next, as a worker's completion
    routes them), so no worker claims them there; leased rows are left to
    their worker (Worker.run_lease skips the stage). UPDATEs on the feed
    index only. `allowed`: the tier's stage set. Returns (rows moved, audio
    paths of the rows now done, to delete once committed; none with
    `keep_audio`, as workers run with --keep-audio)."""
    out = [] if allowed is None else [s for s in STAGES if s not in allowed]
    if not out:
        return 0, []
    case = " ".join(f"WHEN '{s}' THEN '{tier_next(s, allowed)}'" for s in out)
    moved = con.execute(
        f"UPDATE episodes SET stage=CASE stage {case} END, attempts=0, not_before=0,"
        f" updated_at=? WHERE feed_url=? AND tier=? AND state='ready'"
        f" AND stage IN ({','.join('?' * len(out))}) RETURNING episode_id, stage, audio_path",
        (utcnow(), feed_url, tier, *out)).fetchall()
    done = [] if keep_audio else [r for r in moved if r[1] == "done" and r[2]]
    if done:  # nothing left needs the audio
        con.execute("UPDATE episodes SET audio_path=NULL WHERE feed_url=? AND episode_id IN (%s)"
                    % ",".join("?" * len(done)), (feed_url, *(r[0] for r in done)))
    return len(moved), [r[2] for r in done]


def _delete_unreferenced_audio(con, paths) -> None:
    for path in paths:
        if not con.execute("SELECT COUNT(*) FROM episodes WHERE audio_path=?",
                           (path,)).fetchone()[0]:
            Path(path).unlink(missing_ok=True)


# ---------------------------------------------------------------- discovery

def episode_id_for(feed_url: str, entry: dict) -> tuple[str, dict]:
    key = acq.episode_identity_key(feed_url, entry)
    canon = json.dumps(key, sort_keys=True, separators=(",", ":"))
    return "ep_" + hashlib.sha256(canon.encode()).hexdigest()[:16], key


class HttpStatusError(RuntimeError):
    """A non-200 answer; the message stays "HTTP <status>"."""

    def __init__(self, status: int, retry_after=None):
        super().__init__(f"HTTP {status}")
        self.status, self.retry_after = status, retry_after


def http_get_capped(url: str, timeout_s: float = FEED_TIMEOUT_S,
                    max_bytes: int = FEED_MAX_BYTES,
                    deadline_s: float = FEED_DEADLINE_S) -> bytes:
    """GET a feed: `timeout_s` per connect / read, `deadline_s` for the
    whole download, at most `max_bytes`."""
    import requests
    deadline = time.monotonic() + deadline_s
    with requests.get(url, headers={"User-Agent": USER_AGENT}, stream=True,
                      timeout=timeout_s, allow_redirects=True) as resp:
        if resp.status_code != 200:
            raise HttpStatusError(resp.status_code,
                                  (getattr(resp, "headers", None) or {}).get("Retry-After"))
        body = bytearray()
        for chunk in resp.iter_content(1 << 16):
            body.extend(chunk)
            if len(body) > max_bytes:
                raise RuntimeError(f"feed exceeds {max_bytes} bytes")
            if time.monotonic() > deadline:
                raise RuntimeError(f"feed fetch exceeded {deadline_s}s")
        return bytes(body)


class ByteBudget:
    """Bytes shared by the feeds being parsed at once. A request over the
    whole budget waits until nothing else holds any, then runs alone."""

    def __init__(self, total: int):
        self.total, self.used = total, 0
        self._cond = threading.Condition()

    def acquire(self, n: int) -> int:
        n = min(n, self.total)
        with self._cond:
            self._cond.wait_for(lambda: self.used + n <= self.total)
            self.used += n
        return n

    def release(self, n: int) -> None:
        with self._cond:
            self.used -= n
            self._cond.notify_all()


PARSE_BUDGET = ByteBudget(PARSE_BUDGET_BYTES)


def parse_feed_entries_bounded(body: bytes, feed_url: str = "") -> list:
    """acq.parse_feed_entries within PARSE_BUDGET, so parallel discovery
    stays bounded: feedparser builds the whole document (peak RSS ~18x the
    feed), so concurrent parses peak at ~18x max(budget, biggest feed).
    Downloaded bodies wait here for the budget, so add up to workers x the
    feed cap (FEED_MAX_BYTES) of raw bytes waiting. A
    streaming parser would have to re-create feedparser's entry fields that
    episode ids are derived from."""
    held = PARSE_BUDGET.acquire(len(body))
    try:
        return acq.parse_feed_entries(body, feed_url)
    finally:
        PARSE_BUDGET.release(held)


def utc_today():
    return datetime.now(timezone.utc).date()


def parse_feed_bounded(body: bytes, feed_url: str = "") -> tuple:
    """(entries, channel language) of a feed, parsed within PARSE_BUDGET like
    parse_feed_entries_bounded. The language is the channel's RSS
    <language>, normalized by show_language (None when absent)."""
    held = PARSE_BUDGET.acquire(len(body))
    try:
        parsed = acq.parse_feed(body, feed_url)
        return (list(parsed.get("entries", [])),
                show_language((parsed.get("feed") or {}).get("language")))
    finally:
        PARSE_BUDGET.release(held)


def show_language(raw) -> str | None:
    """A show's language as stored: the RSS <language> value trimmed and
    lowercased ("en-US" -> "en-us"); None when empty. Its Whisper code is
    chunked_stt.whisper_language()."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip().lower()[:35]


def language_allowed(raw, languages) -> bool | None:
    """True / False for a stored show language against `languages` (Whisper
    codes, e.g. {"en"}); None when the show's language is unknown or not a
    clear code."""
    code = stt.whisper_language(raw)
    return None if code is None else code in languages


def select_entries(entries: list, per_show: int | None, since=None, until=None) -> list:
    """The `per_show` most recent entries that carry an audio enclosure
    (all of them when per_show is None). With `since` / `until` (UTC dates,
    inclusive) only dated entries in that window count."""
    with_audio = [e for e in entries if acq.entry_audio_url(e)]
    if since is not None or until is not None:
        def inside(e):
            dt = acq.entry_datetime(e)
            if dt is None:
                return False
            day = dt.date()  # UTC (feedparser normalises), as stored in episodes.pub_date
            return (since is None or day >= since) and (until is None or day <= until)
        with_audio = [e for e in with_audio if inside(e)]
    floor = datetime.min.replace(tzinfo=timezone.utc)
    with_audio.sort(key=lambda e: acq.entry_datetime(e) or floor, reverse=True)
    return with_audio if per_show is None else with_audio[:per_show]


def discover(corpus, per_show: int | None = None, workers: int = 8,
             fetch_feed=None, *, since=None, until=None, keep_current: bool = False,
             new_shows: bool = False, feed_max_bytes: int = FEED_MAX_BYTES,
             languages=None, tier: str | None = None, args=None) -> dict:
    """Fetch every feed in parallel and enqueue episodes.

    Default: each show's `per_show` newest audio episodes (per_show from the
    corpus meta when None). `since` / `until` (dates): the audio episodes in
    that window, at most per_show when given. `keep_current`: only episodes
    dated on or after the show's newest queued episode (UTC dates; queued
    episodes dated in the future do not count) and not queued yet, at most
    per_show when given; a show with no dated episode gets the default
    selection.
    `new_shows`: only shows with no episode yet.
    `tier`: only shows of that coverage tier (e.g. keep-current runs on a
    different schedule per tier).
    `languages` (Whisper codes, e.g. {"en"}): shows whose RSS language is
    another one enqueue nothing (counted under "skipped_language"); shows
    with no clear language are kept and counted under "language_unknown".
    Every fetched feed's channel language is stored on its show row
    (per_show 0 refreshes show rows without enqueuing anything).

    Only ever inserts (INSERT OR IGNORE on a content-derived id), one short
    transaction per show that holds no reads of the episodes table, so it
    is idempotent and safe beside live workers. A failing feed or a
    database error on one show is recorded on its show row and never stops
    the others. Each run is appended to the discovery log.
    """
    started = utcnow()
    if keep_current and (since is not None or until is not None):
        raise ValueError("keep_current cannot be combined with since/until")
    if tier is not None:
        check_tier(tier)
    fetch_feed = fetch_feed or (lambda url: http_get_capped(url, max_bytes=feed_max_bytes))
    window = since is not None or until is not None
    con = connect(corpus)
    try:
        ensure_schema(con)
        row = con.execute("SELECT value FROM meta WHERE key='per_show'").fetchone()
        corpus_per_show = int(row[0]) if row else 2
        cap = per_show if (per_show is not None or window or keep_current) else corpus_per_show
        today = utc_today()
        # newest queued date per show, future dates (trailers, typos) left out
        queued = {r[0]: (r[1], r[2]) for r in con.execute(
            "SELECT feed_url, COUNT(*), MAX(CASE WHEN pub_date <= ? THEN pub_date END)"
            " FROM episodes GROUP BY feed_url", (today.isoformat(),))}
        shows = [dict(r) for r in con.execute("SELECT * FROM shows ORDER BY feed_url")]
        listed = len(shows)
        # Show notes go to files (shownotes/<ep>.json) for episodes already
        # queued (by id, else guid) or being enqueued; read here, no lock.
        known: dict = {}
        for r in con.execute("SELECT feed_url, episode_id, guid FROM episodes"):
            k = known.setdefault(r[0], {"ids": set(), "guids": {}})
            k["ids"].add(r[1])
            if r[2]:
                k["guids"].setdefault(r[2], r[1])
        notes_counts = {"written": 0, "unchanged": 0, "empty": 0, "errors": 0}
        notes_lock = threading.Lock()

        def store_notes(url, entries, picked_ids=()):
            try:
                c = shownotes.store_feed_notes(corpus, url, entries, known.get(url) or {},
                                               picked_ids, utcnow(), episode_id_for)
            except Exception:  # never fails the show
                c = {"errors": 1}
            with notes_lock:
                for key, n in c.items():
                    notes_counts[key] += n

        if new_shows:
            shows = [s for s in shows if s["feed_url"] not in queued]
        if tier is not None:
            shows = [s for s in shows if s["tier"] == tier]

        def newest_day(feed_url):
            day = queued.get(feed_url, (0, None))[1]
            return datetime.fromisoformat(day).date() if keep_current and day else None

        def fetch(show):
            """Fetch, parse and pick in the pool thread; only the picked
            episodes' rows leave it, so parsed feeds are freed at once."""
            rows, error, day = [], None, None
            try:
                url = show["feed_url"]
                entries, lang = parse_feed_bounded(fetch_feed(url), url)
                lang = lang or show.get("language")
                if languages is not None and language_allowed(lang, languages) is False:
                    store_notes(url, entries)
                    return show, rows, None, None, lang
                day = newest_day(url)
                if day is not None:
                    picked = select_entries(entries, None, since=day)
                elif window:
                    picked = select_entries(entries, cap, since=since, until=until)
                else:
                    picked = select_entries(entries, corpus_per_show if cap is None else cap)
                store_notes(url, entries, [episode_id_for(url, e)[0] for e in picked])
                del entries
                for entry in picked:
                    ep_id, key = episode_id_for(url, entry)
                    dt = acq.entry_datetime(entry)
                    rows.append((ep_id, (entry.get("id") or entry.get("guid") or "").strip()
                                 or None, key["basis"], entry.get("title"),
                                 dt.date().isoformat() if dt else None,
                                 acq.entry_audio_url(entry)))
                return show, rows, None, day, lang
            except Exception as exc:  # recorded per show, never fatal
                error = f"{type(exc).__name__}: {exc}"[:500]
                return show, rows, error, day, None

        report = {"mode": "keep-current" if keep_current else "window" if window else "newest",
                  "per_show": cap, "since": since and since.isoformat(),
                  "until": until and until.isoformat(), "new_shows_only": new_shows,
                  "shows_listed": listed, "shows": len(shows), "feeds_ok": 0,
                  "feeds_failed": 0, "episodes_new": 0, "episodes_seen": 0}
        if languages is not None:
            report.update(languages=sorted(languages), skipped_language={},
                          language_unknown=0)
        if tier is not None:
            report["tier"] = tier
        now = utcnow()

        def record(show, rows, error, day, lang):
            if day is not None:  # keep current: drop queued ones (a WAL read, no lock), cap
                have = {r[0] for r in con.execute(
                    "SELECT episode_id FROM episodes WHERE feed_url=?", (show["feed_url"],))}
                rows = [r for r in rows if r[0] not in have]
                rows = rows if cap is None else rows[:cap]
            new = seen = 0
            with _Tx(con):
                try:
                    for ep_id, guid, basis, title, pub_date, enclosure in rows:
                        new += con.execute(
                            "INSERT OR IGNORE INTO episodes (episode_id, feed_url, guid,"
                            " identity_basis, show_title, category, ep_title, pub_date,"
                            " enclosure_url, discovered_at, updated_at, tier)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?,"
                            " COALESCE((SELECT tier FROM shows WHERE feed_url=?), ?))",
                            (ep_id, show["feed_url"], guid, basis, show["title"],
                             show["category"], title, pub_date, enclosure, now, now,
                             show["feed_url"], show["tier"])).rowcount
                        seen += 1
                except Exception as exc:  # recorded on the show, as before
                    error = f"{type(exc).__name__}: {exc}"[:500]
                con.execute("UPDATE shows SET last_fetch_at=?, last_status=?, last_error=?,"
                            " language=COALESCE(?, language) WHERE feed_url=?",
                            (now, "ok" if error is None else "error", error, lang,
                             show["feed_url"]))
            return new, seen, error

        try:
            with ThreadPoolExecutor(max(1, workers)) as pool:
                for show, rows, error, day, lang in pool.map(fetch, shows):
                    if languages is not None and error is None:  # before record()
                        ok = language_allowed(lang, languages)
                        if ok is False:
                            code = stt.whisper_language(lang)
                            report["skipped_language"][code] = \
                                report["skipped_language"].get(code, 0) + 1
                        report["language_unknown"] += ok is None
                    try:
                        new, seen, error = record(show, rows, error, day, lang)
                    except sqlite3.Error as exc:  # e.g. locked past busy_timeout
                        new = seen = 0
                        error = f"{type(exc).__name__}: {exc}"[:500]
                        report["db_errors"] = report.get("db_errors", 0) + 1
                        try:
                            with _Tx(con):
                                con.execute("UPDATE shows SET last_fetch_at=?, last_status="
                                            "'error', last_error=? WHERE feed_url=?",
                                            (now, error, show["feed_url"]))
                        except sqlite3.Error:
                            pass
                    report["episodes_new"] += new
                    report["episodes_seen"] += seen
                    report["feeds_ok" if error is None else "feeds_failed"] += 1
        except BaseException as exc:
            report["aborted"] = f"{type(exc).__name__}: {exc}"[:500]
            raise
        finally:
            report["shownotes"] = notes_counts
            try:
                with _Tx(con):
                    log_discovery(con, "discover", started, args, report)
            except sqlite3.Error:
                pass
        return report
    finally:
        con.close()


# ---------------------------------------------------------------- queue

@dataclass
class Lease:
    episode_id: str
    token: str
    stage: str
    attempt: int
    run_id: int
    row: dict
    started: float = field(default_factory=time.monotonic)


class LeaseLost(Exception):
    """This worker no longer holds the lease; its result is discarded."""



API_STT_BACKENDS = ("groq", "openrouter")  # hosted transcription (api_stt.PROVIDERS)


def first_attempt_only_stages(cfg) -> tuple:
    """A non-default (GPU) transcription backend takes only first attempts;
    a retry after a timeout or crash goes to a faster-whisper CPU worker.
    A fleet therefore needs at least one CPU transcribe worker, or retries wait."""
    return ("transcribe",) if getattr(cfg, "backend", "faster-whisper") != "faster-whisper" else ()


def drain_uncounted_retries(cfg) -> tuple:
    """First-attempt-only stages whose retries a worker need not wait for
    when deciding it is drained: those it runs as its only stage. A worker
    with any other stage keeps counting them, since the retried episode can
    come back to it: forward once another worker finishes the retry (detect,
    clip, ...), or back to fetch when the retry finds its audio missing. The
    fetch-back happens to a transcribe-only worker's retries too; that is
    harmless, since it does not run fetch: whichever worker refetches hands
    the episode back to transcribe, and the queue routes it from there."""
    return tuple(st for st in first_attempt_only_stages(cfg) if set(cfg.stages) == {st})

class WorkerFatal(Exception):
    """No further work can succeed on this worker (e.g. the API key was
    rejected); the lease was released without charging an attempt."""


class SttUnavailable(Exception):
    """The local GPU transcribe backend cannot run here (preflight, before
    claiming anything)."""


PARAKEET_PREFLIGHT_TIMEOUT_S = 3600.0  # a first run downloads ~2.5 GB of weights


def parakeet_preflight(python: str, model: str, revision: str | None = None,
                       timeout_s: float = PARAKEET_PREFLIGHT_TIMEOUT_S,
                       gap_fill_model: str | None = None,
                       gap_fill_revision: str | None = None) -> dict:
    """Run `corpus_run.py parakeet-preflight` under the transcribe python:
    it must exist, import parakeet_mlx and resolve `model` (at `revision`,
    else the latest). Returns the pin (revision + weights digest) every
    transcribe child of this worker loads offline. Across workers and
    restarts the corpus holds them to the model of its first preflight
    (Worker._hold_parakeet_model). With `gap_fill_model` the pin also
    carries that mlx-whisper model resolved (at `gap_fill_revision`, else
    the latest) as "gap_fill"."""
    cmd = [python, str(SCRIPT), "parakeet-preflight", "--model", model]
    if revision:
        cmd += ["--revision", revision]
    if gap_fill_model:
        cmd += ["--gap-fill-model", gap_fill_model]
        if gap_fill_revision:
            cmd += ["--gap-fill-revision", gap_fill_revision]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SttUnavailable(f"parakeet preflight could not run {python!r} ({exc}); "
                             "pass --stt-python <python of a venv with parakeet-mlx>") from None
    lines = (res.stdout or "").strip().splitlines()
    if res.returncode != 0 or not lines:
        why = ((res.stderr or "").strip().splitlines() or [f"rc={res.returncode}"])[-1]
        raise SttUnavailable(f"parakeet preflight failed under {python}: {why}")
    try:
        return json.loads(lines[-1])
    except ValueError:
        raise SttUnavailable(f"parakeet preflight under {python} printed no pin: "
                             f"{lines[-1][:200]}") from None


def _same_gap_fill(held, mine) -> bool:
    """Same gap fill: both off, or same repo and min gap, and the same
    revision / weights digest where both records have one."""
    if not held or not mine:
        return not held and not mine
    if held.get("repo") != mine.get("repo") \
            or float(held.get("min_s") or 0) != float(mine.get("min_s") or 0):
        return False
    return all(held[f] == mine[f] for f in ("revision", "weights_sha256")
               if held.get(f) and mine.get(f))


def _describe_gap_fill(gf) -> str:
    if not gf:
        return "off"
    return (f"on ({gf.get('repo')} at {gf.get('revision') or gf.get('weights_sha256') or '?'}, "
            f"min gap {gf.get('min_s')} s)")


def gap_fill_summary(prov) -> str | None:
    """episodes.gap_fill_status for a transcript's provenance "gap_fill"
    ('{"enabled": false}' for a Parakeet transcript made without it)."""
    if not isinstance(prov, dict):
        return None
    if prov.get("enabled") is False:
        return json.dumps({"enabled": False})
    keys = ("gaps_found", "gaps_filled", "words_added", "error", "skipped")
    return json.dumps({k: prov[k] for k in keys if k in prov}, sort_keys=True)


def gap_fill_totals(con) -> dict:
    """Totals over episodes.gap_fill_status (transcripts made with gap fill)."""
    if "gap_fill_status" not in {r[1] for r in con.execute("PRAGMA table_info(episodes)")}:
        return _gap_fill_sum(())
    return _gap_fill_sum(r[0] for r in con.execute(
        "SELECT gap_fill_status FROM episodes WHERE gap_fill_status IS NOT NULL"))


def _gap_fill_sum(raws) -> dict:
    out = {"episodes": 0, "errors": 0, "skipped": 0, "gaps_found": 0, "gaps_filled": 0,
           "words_added": 0, "off": 0}
    for raw in raws:
        if not raw:
            continue
        try:
            g = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(g, dict):
            continue
        if g.get("enabled") is False:  # Parakeet transcript made without gap fill
            out["off"] += 1
            continue
        out["episodes"] += 1
        out["errors"] += bool(g.get("error"))
        out["skipped"] += bool(g.get("skipped"))
        for k in ("gaps_found", "gaps_filled", "words_added"):
            out[k] += int(g.get(k) or 0)
    return out


class Queue:
    def __init__(self, corpus, clock=time.time, backoff_base_s: float = 60.0):
        self.corpus = Path(corpus)
        self.con = connect(self.corpus)
        ensure_schema(self.con)
        self.clock = clock
        self.backoff_base_s = backoff_base_s
        self.host = socket.gethostname()
        self._doomed = []
        self.keep_audio = False   # set by the Worker (--keep-audio): tier-skips keep audio too

    def close(self):
        self.con.close()

    def backoff(self, attempts: int) -> float:
        return min(self.backoff_base_s * 4 ** max(0, attempts - 1), 6 * 3600)

    @staticmethod
    def _shard_sql(shard):
        if not shard or shard[1] <= 1:
            return "", ()
        return " AND shard_of(episode_id, ?) = ?", (shard[1], shard[0])

    def _reclaim_expired(self, con, now: float, max_attempts: int) -> int:
        rows = con.execute("SELECT episode_id, stage, attempts, lease_run_id, lease_owner,"
                           " audio_path FROM episodes WHERE state='leased' AND lease_until < ?",
                           (now,)).fetchall()
        for r in rows:
            attempts = r["attempts"] + 1
            dead = attempts >= max_attempts
            err = f"lease_expired (owner {r['lease_owner']})"
            con.execute("UPDATE stage_runs SET outcome='lease_expired', finished_at=?,"
                        " error=? WHERE id=? AND outcome IS NULL",
                        (utcnow(), err, r["lease_run_id"]))
            if dead and r["stage"] == "diarize":  # never kills: on without speakers
                nxt, screen_status = self.screen_route(con)
                con.execute("UPDATE episodes SET state='ready', lease_owner=NULL,"
                            " lease_token=NULL, lease_until=NULL, lease_run_id=NULL,"
                            " attempts=0, stage=?, last_error=?, not_before=0, updated_at=?,"
                            " diarize_status='failed: lease_expired', screen_status=?"
                            " WHERE episode_id=?",
                            (nxt, err, utcnow(), screen_status, r["episode_id"]))
                continue
            con.execute("UPDATE episodes SET state='ready', lease_owner=NULL,"
                        " lease_token=NULL, lease_until=NULL, lease_run_id=NULL,"
                        " attempts=?, stage=?, last_error=?, not_before=?, updated_at=?"
                        " WHERE episode_id=?",
                        (attempts, "dead" if dead else r["stage"],
                         "max_attempts_exceeded: " + err if dead else err,
                         now + self.backoff(attempts), utcnow(), r["episode_id"]))
            if dead:
                self._mark_dead(con, r["episode_id"], r["stage"], r["audio_path"])
        return len(rows)

    def _mark_dead(self, con, episode_id: str, stage: str, audio_path) -> None:
        """Remember where it died; a dead episode never holds audio (sha256
        and the fetch observation stay). The file goes after commit."""
        frees = stage in ("transcribe", *AUDIO_HOLDING_STAGES)
        con.execute("UPDATE episodes SET dead_stage=?" + (", audio_path=NULL" if frees else "")
                    + " WHERE episode_id=?", (stage, episode_id))
        if frees and audio_path:
            self._doomed.append(audio_path)

    def _purge_doomed(self) -> None:
        doomed, self._doomed = self._doomed, []
        _delete_unreferenced_audio(self.con, doomed)

    def buffered(self, con=None, shard=None, tier=None) -> int:
        """Episodes holding audio on disk (transcribe, and detect/screen/clip
        while audio is kept for clipping) or fetching it right now; of one
        tier only when `tier` is given."""
        sql, args = self._shard_sql(shard)
        # The tier goes inside each OR branch, so SQLite seeks (tier, stage)
        # on episodes_tier_claim instead of walking all of the tier's rows.
        t, targs = ("tier=? AND ", (tier,) * 3) if tier is not None else ("", ())
        held = ",".join(f"'{s}'" for s in AUDIO_HOLDING_STAGES)
        return (con or self.con).execute(
            f"SELECT COUNT(*) FROM episodes WHERE (({t}stage='transcribe') OR"
            f" ({t}stage IN ({held}) AND audio_path IS NOT NULL) OR"
            f" ({t}stage='fetch' AND state='leased'))" + sql, (*targs, *args)).fetchone()[0]

    def buffer_use(self, max_buffered, max_buffered_transcript=None, con=None,
                   shard=None) -> dict:
        """{tier: (buffered, cap)} for each fetch buffer (buffer_caps)."""
        return {t: (self.buffered(con, shard, t), cap)
                for t, cap in buffer_caps(max_buffered, max_buffered_transcript).items()}

    def screen_model(self) -> str | None:
        """The corpus routes detect -> screen once this is set."""
        row = self.con.execute("SELECT value FROM meta WHERE key='screen_model'").fetchone()
        return row[0] if row else None

    def enable_screen(self, model: str) -> None:
        with _Tx(self.con) as con:
            con.execute("INSERT INTO meta (key, value) VALUES ('screen_model', ?)"
                        " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (model,))

    def disable_screen(self) -> None:
        with _Tx(self.con) as con:
            con.execute("DELETE FROM meta WHERE key='screen_model'")

    def jev_model(self, con=None) -> str | None:
        """The corpus routes detect -> jev once this is set."""
        row = (con or self.con).execute(
            "SELECT value FROM meta WHERE key='jev_model'").fetchone()
        return row[0] if row else None

    def enable_jev(self, model: str) -> None:
        with _Tx(self.con) as con:
            con.execute("INSERT INTO meta (key, value) VALUES ('jev_model', ?)"
                        " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (model,))

    def enable_name(self, pipeline: str) -> None:
        with _Tx(self.con) as con:
            con.execute("INSERT INTO meta (key, value) VALUES ('name_pipeline', ?)"
                        " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (pipeline,))

    def enable_diarize(self, model: str) -> None:
        with _Tx(self.con) as con:
            con.execute("INSERT INTO meta (key, value) VALUES ('diarize_model', ?)"
                        " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (model,))

    def parakeet_model(self) -> dict | None:
        """The model the corpus's Parakeet transcripts come from: {repo,
        revision, weights_sha256} of its first preflight (meta parakeet_model)."""
        row = self.con.execute("SELECT value FROM meta WHERE key='parakeet_model'").fetchone()
        return json.loads(row[0]) if row else None

    def record_parakeet_model(self, record: dict, *, overwrite: bool = False) -> dict:
        """Atomically record `record` as the corpus's Parakeet model unless one
        is already recorded (or `overwrite`); returns the record now held."""
        with _Tx(self.con) as con:
            con.execute("INSERT INTO meta (key, value) VALUES ('parakeet_model', ?)"
                        " ON CONFLICT(key) DO " + ("UPDATE SET value=excluded.value"
                                                    if overwrite else "NOTHING"),
                        (json.dumps(record, sort_keys=True),))
            row = con.execute("SELECT value FROM meta WHERE key='parakeet_model'").fetchone()
        return json.loads(row[0])

    def set_parakeet_gap_fill(self, gap_fill: dict | None) -> dict:
        """Atomically replace the gap-fill part of the held Parakeet record
        (the Parakeet model itself stays held); returns the record now held."""
        with _Tx(self.con) as con:
            row = con.execute("SELECT value FROM meta WHERE key='parakeet_model'").fetchone()
            record = {**json.loads(row[0]), "gap_fill": gap_fill}
            con.execute("UPDATE meta SET value=? WHERE key='parakeet_model'",
                        (json.dumps(record, sort_keys=True),))
        return record

    def seed_parakeet_model(self, repo: str) -> dict | None:
        """A corpus without the parakeet_model record (older code) is held to
        the model it already used: a legacy `parakeet_model:<repo>` key (value:
        an HF revision, or a local dir's weights sha256), else the provenance of
        its first Parakeet transcript. Transcripts from before the weights-digest
        fix (no "sentence" in provenance) recorded a cache blob name, not the
        digest: only their revision is trusted (75a8837-only transcripts had the
        true digest without "sentence"; indistinguishable, and 75a8837 was
        immediately followed by the commit adding "sentence"). A candidate left
        with neither revision nor digest is skipped. Returns the record now held."""
        rows = self.con.execute("SELECT key, value FROM meta WHERE key LIKE 'parakeet_model:%'"
                                " ORDER BY key").fetchall()
        rows.sort(key=lambda r: r[0] != f"parakeet_model:{repo}")
        if rows:
            key, value = rows[0]
            digest = re.fullmatch(r"[0-9a-f]{64}", value) is not None
            return self.record_parakeet_model({
                "repo": key.split(":", 1)[1], "revision": None if digest else value,
                "weights_sha256": value if digest else None})
        for path in sorted((self.corpus / "transcripts").glob("*.json")):
            try:
                raw = path.read_bytes()
                if b"parakeet" not in raw:  # cheap: most transcripts are not Parakeet's
                    continue
                prov = json.loads(raw).get("provenance")
            except (OSError, ValueError, AttributeError):
                continue
            if not isinstance(prov, dict) or prov.get("backend") != "parakeet-mlx":
                continue
            record = {"repo": prov.get("repo"), "revision": prov.get("revision"),
                      "weights_sha256": prov.get("weights_sha256") if "sentence" in prov
                      else None}
            if record["revision"] or record["weights_sha256"]:  # else: no identity left
                return self.record_parakeet_model(record)
        return None

    def retries_waiting(self, stages, shard=None) -> int:
        """Ready episodes waiting on a retry of one of `stages`."""
        if not stages:
            return 0
        sql, args = self._shard_sql(shard)
        return self.con.execute(
            "SELECT COUNT(*) FROM episodes WHERE state='ready' AND attempts > 0"
            " AND stage IN (%s)%s" % (",".join("?" * len(stages)), sql),
            (*stages, *args)).fetchone()[0]

    def show_language(self, feed_url: str) -> str | None:
        """The show's stored RSS language (None when unknown)."""
        row = self.con.execute("SELECT language FROM shows WHERE feed_url=?",
                               (feed_url,)).fetchone()
        return row[0] if row else None

    def touch_worker(self, worker_id: str, stages, con=None) -> None:
        """Liveness: detect routes to screen only while a screen worker is seen."""
        (con or self.con).execute(
            "INSERT INTO workers (worker_id, host, stages, started_at, last_seen)"
            " VALUES (?,?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET host=excluded.host,"
            " stages=excluded.stages, last_seen=excluded.last_seen",
            (worker_id, self.host, ",".join(stages), utcnow(), self.clock()))

    def live_worker(self, stage: str, con=None) -> bool:
        return (con or self.con).execute(
            "SELECT COUNT(*) FROM workers WHERE ','||stages||',' LIKE ?"
            " AND last_seen >= ?", (f"%,{stage},%", self.clock() - SCREEN_WORKER_TTL_S)
        ).fetchone()[0] > 0

    def live_screen_worker(self, con=None) -> bool:
        return self.live_worker("screen", con)

    def screen_route(self, con=None) -> tuple:
        """(next stage after detect/jev, screen_status to record or None)."""
        if not (con or self.con).execute(
                "SELECT 1 FROM meta WHERE key='screen_model'").fetchone():
            return "clip", None
        if not self.live_worker("screen", con):
            return "clip", NO_SCREEN_WORKER
        return "screen", None

    def diarize_route(self, con=None) -> tuple:
        """(next stage after detect/jev/name, status updates): diarize while
        a diarize worker is live, else the screen route; diarize_status
        records a skip (and is only touched once diarize was enabled)."""
        updates = {}
        if (con or self.con).execute(
                "SELECT 1 FROM meta WHERE key='diarize_model'").fetchone():
            if self.live_worker("diarize", con):
                return "diarize", {"diarize_status": None, "screen_status": None}
            updates["diarize_status"] = NO_DIARIZE_WORKER
        nxt, screen_status = self.screen_route(con)
        return nxt, {**updates, "screen_status": screen_status}

    def jev_route(self, con=None) -> tuple:
        """(next stage after a successful jev, status updates): name while a
        name worker is live, else the diarize route with name_status
        recording the skip."""
        name_status = None
        if (con or self.con).execute(
                "SELECT 1 FROM meta WHERE key='name_pipeline'").fetchone():
            if self.live_worker("name", con):
                return "name", {"name_status": None}
            name_status = NO_NAME_WORKER
        nxt, updates = self.diarize_route(con)
        return nxt, {"name_status": name_status, **updates}

    def detect_route(self) -> tuple:
        """(next stage after detect, status updates): jev while a jev worker
        is live, else the diarize route with jev_status recording the skip."""
        jev_status = None
        if self.jev_model():
            if self.live_worker("jev"):
                return "jev", {"jev_status": None, "screen_status": None}
            jev_status = NO_JEV_WORKER
        nxt, updates = self.diarize_route()
        return nxt, {"jev_status": jev_status, **updates}

    def stage_counts(self, shard=None) -> dict:
        sql, args = self._shard_sql(shard)
        return {r[0]: r[1] for r in self.con.execute(
            "SELECT stage, COUNT(*) FROM episodes WHERE stage NOT IN ('done','dead')%s"
            " GROUP BY 1" % sql, args)}

    def claim(self, worker: str, stages, *, shard=None, max_attempts: int = 3,
              lease_s: float = 300.0, max_buffered: int | None = None,
              first_attempt_only=(), tier_priority: str = "full",
              max_buffered_transcript: int | None = None) -> Lease | None:
        """Lease the next ready episode at one of `stages`. Tiers are tried
        in `tier_priority` order (TIER_PRIORITIES), each on the tier-led
        index, so a later tier's episode is leased only when no earlier
        tier's is claimable; within a tier, later stage first, then rowid order
        at fetch (a due retry is not starved by the backlog; not_before spaces
        the retries) and attempts-then-rowid at the other stages (the diarize
        breaker sees distinct episodes). Fetch is held while the fetch buffer is
        full: one shared buffer, or one per tier with
        `max_buffered_transcript` (buffer_caps)."""
        if tier_priority not in TIER_PRIORITIES:
            raise ValueError(f"unknown tier priority {tier_priority!r}; use one of "
                             f"{', '.join(TIER_PRIORITIES)}")
        stages = [s for s in STAGES if s in set(stages)]
        now = self.clock()
        with _Tx(self.con) as con:
            self._reclaim_expired(con, now, max_attempts)
            if not self.live_worker("jev", con):  # nobody will jev-screen these
                nxt, upd = self.diarize_route(con)
                self._move_orphans(con, "jev", nxt, {"jev_status": NO_JEV_WORKER, **upd})
            if not self.live_worker("name", con):  # nobody will name these
                nxt, upd = self.diarize_route(con)
                self._move_orphans(con, "name", nxt, {"name_status": NO_NAME_WORKER, **upd})
            if not self.live_worker("diarize", con):  # nobody will diarize these
                nxt, screen_status = self.screen_route(con)
                self._move_orphans(con, "diarize", nxt, {"diarize_status": NO_DIARIZE_WORKER,
                                                         "screen_status": screen_status})
            if not self.live_screen_worker(con):  # nobody will screen these
                con.execute("UPDATE episodes SET stage='clip', screen_status=?, attempts=0,"
                            " not_before=0, updated_at=? WHERE stage='screen' AND state='ready'",
                            (NO_SCREEN_WORKER, utcnow()))
            allowed = list(stages)
            capped = set() if "fetch" not in allowed else {  # tiers whose buffer is full
                t for t, cap in buffer_caps(max_buffered, max_buffered_transcript).items()
                if cap is not None and self.buffered(con, shard, t) >= cap}
            if None in capped or set(TIERS) <= capped:
                allowed.remove("fetch")
                capped = set()
            sql, args = self._shard_sql(shard)
            first_only = [st for st in first_attempt_only if st in allowed]
            if first_only:  # retries of these stages are left to other workers
                sql += " AND NOT (stage IN (%s) AND attempts > 0)" % ",".join("?" * len(first_only))
                args = (*args, *first_only)
            row = None
            for tier in TIER_PRIORITIES[tier_priority] if allowed else ():
                # INDEXED BY: ANALYZE stats can otherwise steer a tier's claim to
                # episodes_claim or a full scan, walking the other tier's backlog.
                # ensure_schema (Queue.__init__) always builds the index.
                tsql, targs = ("INDEXED BY episodes_tier_claim WHERE tier=? AND ", (tier,)) \
                    if tier else ("WHERE ", ())
                tier_allowed, tier_sql, tier_args = allowed, sql, args
                if tier in capped:      # this tier's fetch buffer is full
                    tier_allowed = [st for st in allowed if st != "fetch"]
                    if not tier_allowed:
                        continue
                elif tier is None and capped:   # no tier order: hold those tiers' fetch
                    tier_sql = sql + " AND NOT (stage='fetch' AND tier IN (%s))" \
                        % ",".join("?" * len(capped))
                    tier_args = (*args, *sorted(capped))
                row = con.execute(
                    "SELECT * FROM episodes %sstate='ready' AND stage IN (%s)"
                    " AND not_before <= ?%s ORDER BY CASE stage WHEN 'clip' THEN 0"
                    " WHEN 'screen' THEN 1 WHEN 'diarize' THEN 2 WHEN 'name' THEN 3"
                    " WHEN 'jev' THEN 4 WHEN 'detect' THEN 5 WHEN 'transcribe' THEN 6 ELSE 7 END,"
                    " CASE WHEN stage='fetch' THEN 0 ELSE attempts END, rowid"
                    " LIMIT 1"
                    % (tsql, ",".join("?" * len(tier_allowed)), tier_sql),
                    (*targs, *tier_allowed, now, *tier_args)).fetchone()
                if row is not None:
                    break
            if row is not None:
                token = secrets.token_hex(16)
                run = con.execute(
                    "INSERT INTO stage_runs (episode_id, stage, attempt, worker, host,"
                    " lease_token, started_at) VALUES (?,?,?,?,?,?,?)",
                    (row["episode_id"], row["stage"], row["attempts"] + 1, worker, self.host,
                     token, utcnow()))
                con.execute("UPDATE episodes SET state='leased', lease_owner=?, lease_token=?,"
                            " lease_until=?, lease_run_id=?, updated_at=? WHERE episode_id=?",
                            (worker, token, now + lease_s, run.lastrowid, utcnow(),
                             row["episode_id"]))
        self._purge_doomed()
        if row is None:
            return None
        return Lease(row["episode_id"], token, row["stage"], row["attempts"] + 1,
                     run.lastrowid, dict(row))

    @staticmethod
    def _move_orphans(con, stage: str, nxt: str, updates: dict) -> None:
        """Ready rows at `stage` move to `nxt` with fresh attempts."""
        sets = {"stage": nxt, **updates, "attempts": 0, "not_before": 0, "updated_at": utcnow()}
        con.execute("UPDATE episodes SET " + ",".join(f"{k}=?" for k in sets)
                    + " WHERE stage=? AND state='ready'", (*sets.values(), stage))

    def heartbeat(self, lease: Lease, lease_s: float, con=None) -> bool:
        cur = (con or self.con).execute(
            "UPDATE episodes SET lease_until=? WHERE episode_id=? AND lease_token=?"
            " AND state='leased'", (self.clock() + lease_s, lease.episode_id, lease.token))
        return cur.rowcount == 1

    def _close_run(self, con, lease, outcome, error, metrics):
        m = metrics or {}
        con.execute("UPDATE stage_runs SET outcome=?, error=?, finished_at=?, wall_s=?,"
                    " cpu_s=?, audio_s=?, peak_rss_mb=?, llm_usage=?, cost_usd=? WHERE id=?",
                    (outcome, error, utcnow(), m.get("wall_s"), m.get("cpu_s"),
                     m.get("audio_s"), m.get("peak_rss_mb"), m.get("llm_usage"),
                     m.get("cost_usd"), lease.run_id))

    def complete(self, lease: Lease, *, updates: dict | None = None,
                 metrics: dict | None = None, observation: dict | None = None,
                 next_stage: str | None = None, outcome: str = "ok",
                 error: str | None = None, carry_attempts: bool = False) -> bool:
        """Advance the episode (fenced). Returns False when the lease was lost.

        `observation` (a fetch observation) is appended even when the lease
        was lost: it records a fetch that really happened.
        """
        nxt = next_stage or NEXT_STAGE[lease.stage]
        updates = dict(updates or {})
        doomed = None
        with _Tx(self.con) as con:
            routed = self._tier_route(con, lease.episode_id, nxt)
            if routed != nxt:  # the tier's stage set leaves nxt out
                nxt = routed
                if nxt == "done" and not self.keep_audio:  # nothing left needs the audio
                    doomed = updates.get("audio_path", lease.row.get("audio_path"))
                    updates["audio_path"] = None
            if observation is not None:
                cols = list(observation)
                cur = con.execute("INSERT INTO fetch_observations (%s) VALUES (%s)"
                                  % (",".join(cols), ",".join("?" * len(cols))),
                                  [observation[c] for c in cols])
                updates["fetch_observation_id"] = cur.lastrowid
            sets = {"stage": nxt, "state": "ready", "lease_owner": None, "lease_token": None,
                    "lease_until": None, "lease_run_id": None, "not_before": 0,
                    "attempts": lease.attempt if carry_attempts else 0,
                    "last_error": error, "updated_at": utcnow(), **updates}
            cur = con.execute("UPDATE episodes SET " + ",".join(f"{k}=?" for k in sets)
                              + " WHERE episode_id=? AND lease_token=? AND state='leased'",
                              [*sets.values(), lease.episode_id, lease.token])
            if cur.rowcount != 1:
                return False
            self._close_run(con, lease, outcome, error, metrics)
            if doomed:
                self._doomed.append(doomed)
        self._purge_doomed()
        return True

    def tier_runs(self, episode_id: str, stage: str) -> bool:
        """Whether the episode's tier (read now) runs `stage`."""
        row = self.con.execute("SELECT tier FROM episodes WHERE episode_id=?",
                               (episode_id,)).fetchone()
        allowed = tier_stages(self.con, row[0]) if row else None
        return allowed is None or stage in allowed

    def _tier_route(self, con, episode_id: str, nxt: str) -> str:
        """`nxt`, or where the episode's tier (read now: set-tier may have
        moved its show during the lease) sends it instead (tier_next)."""
        row = con.execute("SELECT tier FROM episodes WHERE episode_id=?",
                          (episode_id,)).fetchone()
        return tier_next(nxt, tier_stages(con, row[0])) if row else nxt

    def fail(self, lease: Lease, outcome: str, error: str, *, max_attempts: int = 3,
             metrics: dict | None = None, terminal: bool = False) -> bool:
        error = (error or outcome)[:1000]
        with _Tx(self.con) as con:
            dead = terminal or lease.attempt >= max_attempts
            audio = con.execute("SELECT audio_path FROM episodes WHERE episode_id=?",
                                (lease.episode_id,)).fetchone()
            cur = con.execute(
                "UPDATE episodes SET state='ready', lease_owner=NULL, lease_token=NULL,"
                " lease_until=NULL, lease_run_id=NULL, attempts=?, stage=?, last_error=?,"
                " not_before=?, updated_at=? WHERE episode_id=? AND lease_token=?"
                " AND state='leased'",
                (lease.attempt, "dead" if dead else lease.stage,
                 ("terminal: " if terminal else "max_attempts_exceeded: " if dead else "")
                 + f"{outcome}: {error}",
                 self.clock() + self.backoff(lease.attempt), utcnow(),
                 lease.episode_id, lease.token))
            if cur.rowcount != 1:
                return False
            if dead:
                self._mark_dead(con, lease.episode_id, lease.stage, audio and audio[0])
            self._close_run(con, lease, outcome, error, metrics)
        self._purge_doomed()
        return True

    def mark_dead(self, episode_ids, reason: str, *, dry_run: bool = False) -> dict:
        """Retire ready episodes at the fetch stage to dead, keeping dead_stage.
        Fetch-stage ready rows only: live rows at any other stage are listed
        under `not_fetch` (their audio is never touched), leased rows under
        `leased`, done/dead rows under `not_live`, unknown ids under
        `unknown`. `dry_run` writes nothing. Undo: requeue_dead, which sends
        the row back to a fresh fetch."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("mark_dead needs a non-empty reason")
        reason = reason[:500]
        started = utcnow()
        ids = list(dict.fromkeys(episode_ids))
        rep = {"dry_run": dry_run, "marked": [], "would_mark": [], "leased": [],
               "not_live": [], "not_fetch": [], "unknown": []}
        with _Tx(self.con) as con:
            for ep_id in ids:
                row = con.execute("SELECT stage, state, attempts, last_error, audio_path"
                                  " FROM episodes WHERE episode_id=?", (ep_id,)).fetchone()
                if row is None:
                    rep["unknown"].append(ep_id)
                elif row["state"] == "leased":
                    rep["leased"].append(ep_id)
                elif row["stage"] in ("done", "dead"):
                    rep["not_live"].append(ep_id)
                elif row["stage"] != "fetch":
                    rep["not_fetch"].append(ep_id)
                elif dry_run:
                    rep["would_mark"].append({"episode_id": ep_id, "stage": row["stage"],
                                              "attempts": row["attempts"],
                                              "last_error": row["last_error"]})
                else:
                    cur = con.execute(
                        "UPDATE episodes SET stage='dead', state='ready', not_before=0,"
                        " updated_at=?, last_error='marked_dead: ' || ? || ' (was ' || stage"
                        " || '): ' || COALESCE(last_error, '') WHERE episode_id=?"
                        " AND state='ready' AND stage='fetch'",
                        (utcnow(), reason, ep_id))
                    if cur.rowcount == 1:
                        self._mark_dead(con, ep_id, row["stage"], row["audio_path"])
                        rep["marked"].append(ep_id)
                    else:
                        # Unreachable under BEGIN IMMEDIATE (the row was just read
                        # as ready); kept so every id lands in exactly one bucket.
                        rep["leased"].append(ep_id)
            if not dry_run:
                log_discovery(con, "mark-dead", started,
                              {"episodes": ids, "reason": reason}, rep)
        self._purge_doomed()
        return rep

    def diarize_db_releases(self, episode_id: str) -> int:
        """Free (uncharged) diarize releases after database errors, all time."""
        return self.con.execute(
            "SELECT COUNT(*) FROM stage_runs WHERE episode_id=? AND stage='diarize'"
            " AND outcome='killed' AND error LIKE ?",
            (episode_id, DIARIZE_DB_RELEASE_REASON + "%")).fetchone()[0]

    def release(self, lease: Lease, reason: str = "worker_shutdown",
                metrics: dict | None = None, delay_s: float = 0.0) -> bool:
        """Give the lease back without charging an attempt; with `delay_s`
        nobody claims it again for that many seconds."""
        with _Tx(self.con) as con:
            cur = con.execute("UPDATE episodes SET state='ready', lease_owner=NULL,"
                              " lease_token=NULL, lease_until=NULL, lease_run_id=NULL,"
                              " updated_at=?" + (", not_before=?" if delay_s > 0 else "")
                              + " WHERE episode_id=? AND lease_token=? AND state='leased'",
                              (utcnow(), *((self.clock() + delay_s,) if delay_s > 0 else ()),
                               lease.episode_id, lease.token))
            if cur.rowcount != 1:
                return False
            self._close_run(con, lease, "killed", reason, metrics)
        return True

    def remaining(self, stages, shard=None, skip_retries_of=()) -> int:
        """Episodes that could still reach one of `stages` (not done/dead),
        leaving out episodes retrying a `skip_retries_of` stage
        (drain_uncounted_retries)."""
        upto = max(STAGE_ORDER[s] for s in stages)
        names = [s for s in STAGES if STAGE_ORDER[s] <= upto]
        sql, args = self._shard_sql(shard)
        if skip_retries_of:
            sql += " AND NOT (stage IN (%s) AND attempts > 0)" % ",".join(
                "?" * len(skip_retries_of))
            args = (*args, *skip_retries_of)
        return self.con.execute("SELECT COUNT(*) FROM episodes WHERE stage IN (%s)%s"
                                % (",".join("?" * len(names)), sql),
                                (*names, *args)).fetchone()[0]


class Heartbeat(threading.Thread):
    """Extends a lease every lease_s/3 on its own connection until stopped or
    the per-stage ceiling passes; sets `lost` when fencing rejects it."""

    def __init__(self, queue: Queue, lease: Lease, lease_s: float, ceiling_s: float,
                 touch=None):
        super().__init__(daemon=True)
        self.queue, self.lease, self.lease_s, self.ceiling_s = queue, lease, lease_s, ceiling_s
        self.touch = touch  # worker liveness, kept fresh during long leases
        self.halt = threading.Event()
        self.lost = threading.Event()

    def run(self):
        con = connect(self.queue.corpus)
        t0 = time.monotonic()
        try:
            while not self.halt.wait(max(0.05, self.lease_s / 3)):
                if time.monotonic() - t0 > self.ceiling_s:
                    return
                try:
                    if not self.queue.heartbeat(self.lease, self.lease_s, con):
                        self.lost.set()
                        return
                    if self.touch:
                        self.touch(con)
                except sqlite3.Error:
                    continue
        finally:
            con.close()

    def stop(self):
        self.halt.set()
        self.join(timeout=5)


# ---------------------------------------------------------------- child supervision

def rss_mb(pid: int) -> float:
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        return int(out) / 1024 if out else 0.0
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def supervise(cmd: list, *, mem_cap_mb: float, timeout_s: float, poll_s: float = 1.0,
              abort=None, log_path=None) -> dict:
    """Run `cmd` in its own process group; SIGKILL it on RSS > cap, timeout,
    or `abort()` returning a reason. Returns outcome + resource metrics."""
    log = open(log_path, "wb") if log_path else subprocess.DEVNULL
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
    finally:
        if log_path:
            log.close()
    peak, reason, status, ru = 0.0, None, None, None
    try:
        while True:
            pid, status, ru = os.wait4(proc.pid, os.WNOHANG)
            if pid:
                break
            peak = max(peak, rss_mb(proc.pid))
            if reason is None:
                if peak > mem_cap_mb:
                    reason = "oom"
                elif time.monotonic() - t0 > timeout_s:
                    reason = "timeout"
                elif abort and (why := abort()):
                    reason = why
                if reason:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    _, status, ru = os.wait4(proc.pid, 0)
                    break
            time.sleep(poll_s)
    except BaseException:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            os.wait4(proc.pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
        raise
    proc.returncode = rc = os.waitstatus_to_exitcode(status)
    # ru_maxrss is bytes on macOS, KiB on Linux; it catches peaks between polls.
    div = 1024 * 1024 if sys.platform == "darwin" else 1024
    peak = max(peak, ru.ru_maxrss / div if ru else 0.0)
    if reason is None:
        if rc == 0:
            reason = "ok"
        elif rc in (-signal.SIGKILL, 137):
            reason = "oom" if peak >= 0.9 * mem_cap_mb else "killed"
        elif rc < 0:
            reason = "killed"
        else:
            reason = "error"
    tail = ""
    if log_path and reason != "ok":
        try:
            tail = Path(log_path).read_bytes()[-800:].decode(errors="replace")
        except OSError:
            pass
    return {"outcome": reason, "returncode": rc, "peak_rss_mb": round(peak, 1),
            "wall_s": round(time.monotonic() - t0, 3),
            "cpu_s": round(ru.ru_utime + ru.ru_stime, 3) if ru else None, "log_tail": tail}


# ---------------------------------------------------------------- worker

YIELD_FLAG = "--yield-while-pgrep"


def yield_match(pattern: str) -> list:
    """Command lines of running processes that match `pattern`, an extended
    regex searched in the full command line (as `pgrep -f`). Command lines
    holding YIELD_FLAG are left out: the fleet's own supervisor, wrappers and
    workers carry the pattern as that flag's value. A failing `ps` raises."""
    out = subprocess.run(["ps", "-axww", "-o", "command="], capture_output=True,
                         text=True, check=True).stdout
    rx = re.compile(pattern)
    return [cmd for cmd in (line.strip() for line in out.splitlines())
            if rx.search(cmd) and YIELD_FLAG not in cmd]


_detect_mod = None


def _detect_all():
    global _detect_mod
    if _detect_mod is None:
        spec = importlib.util.spec_from_file_location(
            "ew_detect_all", REPO / "scripts" / "detect_all.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _detect_mod = mod
    return _detect_mod


@dataclass
class WorkerConfig:
    worker_id: str
    stages: tuple = DEFAULT_STAGES
    model: str = "tiny"
    beam: int = 1
    threads: int = 2
    chunk_s: float = 600.0
    overlap_s: float = 20.0
    lang_windows: int = 3
    mem_cap_mb: float = 3500.0
    max_attempts: int = 3
    lease_s: float = 300.0
    max_buffered: int = 6
    max_buffered_transcript: int | None = None  # own transcript-tier buffer (buffer_caps)
    stop_at: float | None = None
    max_episodes: int | None = None
    shard: tuple | None = None
    keep_audio: bool = False
    timeout_base_s: float = 300.0
    timeout_factor: float = 1.5
    poll_s: float = 1.0
    idle_poll_s: float = 10.0
    exit_when_drained: bool = True
    backend: str = "faster-whisper"
    stt_python: str | None = None  # python for the transcribe child (default: this one)
    allow_model_change: bool = False  # parakeet: accept a revision other than the corpus's
    gap_fill_model: str | None = None  # parakeet: mlx-whisper repo re-transcribing word gaps
    gap_fill_min_s: float = 3.0        # parakeet gap fill: shortest gap re-transcribed
    allow_gap_fill_change: bool = False  # parakeet: switch the corpus's held gap fill
    screen_model: str = llm_screen.DEFAULT_MODEL
    screen_effort: str = "low"
    screen_window_s: float = 600.0
    screen_overlap_s: float = 60.0
    jev_model: str = jev_screen.DEFAULT_MODEL
    jev_threshold: float = jev_screen.DEFAULT_THRESHOLD  # clip/name windows at/above
    jev_context_windows: int = DEFAULT_JEV_CONTEXT_WINDOWS  # name stage text context
    name_sub_window_s: float | None = DEFAULT_NAME_SUB_WINDOW_S  # None: whole 60-s windows
    name_gliner: str = "small"   # "none": domain/CTA extraction + repair only
    name_threads: int = 2        # torch threads for GLiNER
    brand_list: str | None = None  # JSON from harvest_brand_list; default: build from corpus
    jev_clip_pad_s: float = JEV_CLIP_PAD_S  # clip pad around jev/named spans (others: CLIP_PAD_S)
    jev_clip_max_s: float = JEV_CLIP_MAX_S  # split cap for clips holding a jev/named span
    stall_log_s: float = 300.0  # idle with work left: log stage counts this often
    min_free_gb: float | None = None  # disk guard floor on the corpus volume (None: off)
    max_cost_usd: float | None = None  # hosted transcribe backends: USD cap for this worker
    api_env_file: str | None = None    # hosted backends: .env holding the provider key
    api_rpm: float | None = None       # hosted backends: requests per minute (default: provider)
    api_timeout_s: float = 300.0       # hosted backends: per-request timeout
    api_language: str | None = None    # hosted backends: ISO-639-1 hint (default: auto)
    diarize_device: str = "mps"        # diarize child: mps (Apple GPU) or cpu
    diarize_mem_cap_mb: float = DIARIZE_MEM_CAP_MB  # diarize child RSS cap
    diarize_python: str | None = None  # python with requirements-diarize.txt (default: this one)
    diarize_window_s: float = dz.WINDOW_S
    diarize_overlap_s: float = dz.OVERLAP_S
    language_hint: bool = True          # Whisper: decode in the show's RSS language when known
    repair_backend: str | None = None   # transcribe: re-transcribe quality flags (parakeet|mlx)
    repair_model: str | None = None     # default: Parakeet v3 / large-v3-turbo
    repair_python: str | None = None    # python for the repair child (default: stt_python)
    tier_priority: str = "full"         # claim order across coverage tiers (TIER_PRIORITIES)
    yield_while_pgrep: str | None = None  # claim nothing while a process matches (yield_match)
    yield_poll_s: float = 60.0          # re-check interval while yielding


class Worker:
    def __init__(self, corpus, config: WorkerConfig, *, queue: Queue | None = None,
                 fetcher=None, transcribe_cmd: list | None = None, log=print,
                 llm_client=None, ffmpeg: str | None = None, jev_transport=None,
                 namer=None, api_transcriber=None, diarize_cmd: list | None = None,
                 parakeet_pin: dict | None = None, repair_cmd: list | None = None,
                 repair_pin: dict | None = None):
        self.corpus = Path(corpus)
        self.cfg = config
        self.log = log
        self.api, self.budget_stopped = None, None
        if "transcribe" in config.stages and config.backend in API_STT_BACKENDS:
            from earwitness.pipeline import api_stt  # ApiUnavailable / ApiFatal: before claiming
            if api_transcriber is None:
                api_transcriber = api_stt.from_args(
                    config.backend, model=config.model, env_file=config.api_env_file,
                    max_cost_usd=config.max_cost_usd, rpm=config.api_rpm,
                    timeout_s=config.api_timeout_s, language=config.api_language,
                    ledger=self.corpus / "queue.sqlite3", owner=config.worker_id,
                    log=lambda m: log(f"[{config.worker_id}] {m}"))
            api_transcriber.describe()
            self.api = api_transcriber
        self.parakeet_pin = parakeet_pin
        if "name" in config.stages and namer is None:
            namer = build_namer(self.corpus, gliner=config.name_gliner,
                                brand_list=config.brand_list, threads=config.name_threads,
                                log=lambda m: log(f"[{config.worker_id}] {m}"))
        self.namer = namer
        if "jev" in config.stages:  # JevUnavailable: fail before claiming
            if jev_transport is None:
                jev_transport = jev_screen.transport_from_env(
                    purpose="screen", alert=lambda code, msg: jev_budget_alert(
                        self.corpus, code, f"{config.worker_id}: {msg}", log))
            jev_screen.preflight(jev_transport)
        self.jev_transport = jev_transport
        if "screen" in config.stages:  # ScreenUnavailable: fail before claiming
            if llm_client is None:
                llm_client = llm_screen.make_client()
            llm_screen.preflight(llm_client, config.screen_model)
            if warn := llm_screen.price_warning(config.screen_model):
                log(f"[{config.worker_id}] {warn}")
        self.llm_client = llm_client
        self.diarize_cmd = diarize_cmd or [config.diarize_python or sys.executable,
                                           str(Path(dz.__file__).resolve())]
        if "diarize" in config.stages:  # DiarizeUnavailable: fail before claiming
            diarize_preflight(self.diarize_cmd, config.diarize_device,
                              mem_cap_mb=config.diarize_mem_cap_mb)
        self.ffmpeg = ffmpeg or stt.ffmpeg_path()
        self.queue = queue or Queue(self.corpus)
        self.queue.keep_audio = config.keep_audio
        if "transcribe" in config.stages and config.backend == "parakeet" \
                or self.parakeet_pin:  # SttUnavailable: before claiming
            self._hold_parakeet_model()
        if "screen" in config.stages:
            self.queue.enable_screen(config.screen_model)
        if "jev" in config.stages:
            self.queue.enable_jev(config.jev_model)
        if "name" in config.stages:
            self.queue.enable_name(namer.pipeline)
        if "diarize" in config.stages:
            self.queue.enable_diarize(f"{dz.MODEL_ID}@{dz.MODEL_REVISION}")
        self.touch()
        self.fatal = None
        self._diarize_streak = (None, set())   # (failure class, episode ids)
        self._gap_fill_errors = 0  # consecutive transcripts whose gap fill failed
        self._last_stall_log = float("-inf")
        self._jev_alerted: dict = {}           # budget alert code -> monotonic time
        self.dirs = corpus_dirs(self.corpus)
        for d in self.dirs.values():
            d.mkdir(parents=True, exist_ok=True)
        self.fetcher = fetcher
        self.transcribe_cmd = transcribe_cmd or [config.stt_python or sys.executable,
                                                  str(SCRIPT), "transcribe-one"]
        repair_python = config.repair_python or config.stt_python or sys.executable
        self.repair_cmd = repair_cmd or [repair_python, str(SCRIPT), "repair-one"]
        self.repair_pin = repair_pin
        if "transcribe" in config.stages and config.repair_backend == "parakeet" \
                and repair_pin is None:  # SttUnavailable: before claiming
            self.repair_pin = parakeet_preflight(repair_python, self.repair_model())
        self.stop = threading.Event()      # stop claiming; release long work
        self.processed = 0
        self.disk_free = lambda: shutil.disk_usage(self.corpus).free
        self._disk_low = None   # stages held back by the disk guard, None when space is fine
        self._yielding = False  # a --yield-while-pgrep process ran at the last check
        self._yield_check_failed = False
        self._last_disk_log = float("-inf")
        self._disk_check_failed = False

    def _hold_parakeet_model(self) -> None:
        """Preflight (unless a pin was given) and hold the corpus to one
        Parakeet model: the first preflight records {repo, revision,
        weights_sha256}; later ones resolve that revision, and a worker that
        would load other weights is refused before its preflight can download
        anything. Same weights (equal revision and sha256) at another local
        path are the same model. --allow-model-change resolves the latest and
        records it. The record is written atomically after the preflight, so
        of two first workers racing through their preflights only one model
        is recorded and the other worker is refused.

        Gap fill is held the same way: the record's "gap_fill" is {repo,
        revision, weights_sha256, min_s} or None (off). A record without the
        field (older code) means gap fill was off so far. Turning it on or
        off, or another Whisper model, revision or min gap, is refused unless
        --allow-gap-fill-change (gap fill only; the Parakeet model stays held)
        or --allow-model-change."""
        c = self.cfg
        held = self.queue.parakeet_model() or self.queue.seed_parakeet_model(c.model)
        keep = held is not None and not c.allow_model_change
        gf_switch = c.allow_model_change or c.allow_gap_fill_change
        want_gf = ({"repo": c.gap_fill_model, "min_s": float(c.gap_fill_min_s)}
                   if c.gap_fill_model else None)

        def refuse(found: str):
            raise SttUnavailable(
                f"this corpus's Parakeet transcripts come from {held['repo']} at "
                f"{held.get('revision') or held.get('weights_sha256')}, but this worker "
                f"{found}; pass --allow-model-change to switch (the corpus then mixes models)")

        def refuse_gf(theirs, mine):
            raise SttUnavailable(
                f"this corpus's Parakeet transcripts were made with gap fill "
                f"{_describe_gap_fill(theirs)}, but this worker would run gap fill "
                f"{_describe_gap_fill(mine)}; pass --allow-gap-fill-change to switch gap fill "
                "(the Parakeet model stays held; the corpus then mixes transcripts made "
                "with different gap fill)")
        if held is not None and not gf_switch \
                and not _same_gap_fill(held.get("gap_fill"), want_gf):
            refuse_gf(held.get("gap_fill"), want_gf)
        if self.parakeet_pin is None:
            if keep and held.get("revision") and c.model != held["repo"]:
                refuse(f"would load {c.model}")
            if keep and not held.get("revision") and not Path(c.model).is_dir():
                refuse(f"would load {c.model}")
            held_gf = (held or {}).get("gap_fill") or {}
            gf_rev = held_gf.get("revision") if (
                want_gf and held_gf.get("repo") == want_gf["repo"]
                and not c.allow_model_change) else None
            self.parakeet_pin = parakeet_preflight(
                c.stt_python or sys.executable, c.model,
                held.get("revision") if keep else None, gap_fill_model=c.gap_fill_model,
                gap_fill_revision=gf_rev)
        pin = self.parakeet_pin
        mine = {"repo": pin.get("repo") or c.model, "revision": pin.get("revision"),
                "weights_sha256": pin.get("weights_sha256")}
        if want_gf:
            pin_gf = pin.get("gap_fill") or {}
            mine["gap_fill"] = {**want_gf, "revision": pin_gf.get("revision"),
                                "weights_sha256": pin_gf.get("weights_sha256")}
        else:
            mine["gap_fill"] = None
        if not (mine["revision"] or mine["weights_sha256"]):
            return
        held = self.queue.record_parakeet_model(mine, overwrite=c.allow_model_change)
        # A seeded record may lack a field (legacy key, pre-fix provenance):
        # compare the fields it has.
        fields = [f for f in ("revision", "weights_sha256") if held.get(f)]
        if any(held[f] != mine[f] for f in fields):
            refuse(f"resolved {mine['repo']} at {mine['revision'] or mine['weights_sha256']}")
        if not _same_gap_fill(held.get("gap_fill"), mine["gap_fill"]):
            if not gf_switch:
                refuse_gf(held.get("gap_fill"), mine["gap_fill"])
            self.queue.set_parakeet_gap_fill(mine["gap_fill"])

    def repair_model(self) -> str:
        c = self.cfg
        return c.repair_model or (stt.PARAKEET_DEFAULT_REPO if c.repair_backend == "parakeet"
                                  else "large-v3-turbo")

    def repair_transcript(self, lease: Lease, hb: Heartbeat, audio: str, out: Path,
                          doc: dict, show_lang: str | None) -> dict:
        """Run the repair child over the transcript's quality flags (audio still
        here) and leave the result in `out`. Returns the repaired, validated
        transcript, or `doc` with the failure in quality "repair_error" (the
        transcript is never lost to a failed repair), or None when the worker
        is shutting down."""
        c, row = self.cfg, lease.row
        fixed = out.with_name(out.name + ".repaired")
        log_path = out.with_name(out.name + ".repair.log")
        cmd = [*self.repair_cmd, "--audio", audio, "--transcript", str(out),
               "--out", str(fixed), "--backend", c.repair_backend, "--model", self.repair_model()]
        if self.repair_pin:
            cmd += ["--parakeet-pin", json.dumps(self.repair_pin, sort_keys=True)]
        if show_lang:
            cmd += ["--show-language", show_lang]
        audio_s = sum(g["b"] - g["a"] for g in tq.plan(tq.pending(doc.get("quality")),
                                                       row.get("duration_s")))
        try:
            res = supervise(cmd, mem_cap_mb=c.mem_cap_mb,
                            timeout_s=c.timeout_base_s + c.timeout_factor * audio_s,
                            poll_s=c.poll_s, log_path=log_path,
                            abort=lambda: "lease_lost" if hb.lost.is_set()
                            else "shutdown" if self.stop.is_set() else None)
            if res["outcome"] == "lease_lost":
                raise LeaseLost("lease lost while repairing")
            if res["outcome"] == "shutdown":
                return None
            why = None
            if res["outcome"] != "ok":
                why = f"{res['outcome']} rc={res['returncode']} {res['log_tail'][-300:]}"
            else:
                try:
                    new = json.loads(fixed.read_text(encoding="utf-8"))
                    validate_transcript(new, row.get("duration_s"))
                    os.replace(fixed, out)
                    return new
                except (OSError, ValueError, TranscriptInvalid) as exc:
                    why = f"invalid repaired transcript: {exc}"
            doc.setdefault("quality", {})["repair_error"] = why[:500]
            self.log(f"[{c.worker_id}] REPAIR FAILED on {lease.episode_id} (transcript kept "
                     f"unrepaired): {why[:300]}")
            atomic_write_json(out, doc)
            return doc
        finally:
            fixed.unlink(missing_ok=True)
            log_path.unlink(missing_ok=True)

    def note_gap_fill(self, episode_id: str, prov: dict | None) -> None:
        """After a transcript is published: log a gap-fill error loudly and
        stop the worker (WorkerFatal) after GAP_FILL_BREAKER in a row."""
        if not prov or not prov.get("error"):
            self._gap_fill_errors = 0
            return
        self._gap_fill_errors += 1
        self.log(f"[{self.cfg.worker_id}] GAP FILL ERROR on {episode_id} "
                 f"({self._gap_fill_errors} in a row; transcript kept without gap fill): "
                 f"{prov['error']}")
        if self._gap_fill_errors >= GAP_FILL_BREAKER:
            raise WorkerFatal(f"gap fill failed on {self._gap_fill_errors} episodes in a row, "
                              f"last {episode_id}: {prov['error'][:300]}")

    def claimable_stages(self) -> tuple:
        """The configured stages, less those the disk guard holds back:
        below --min-free-gb no fetch, below DISK_CLIP_ONLY_GB only clip.
        Free space is what statvfs reports; on APFS it leaves out purgeable
        space (Finder's "available" can be larger).
        Logs when the guard engages (repeated every stall_log_s) and when
        space recovers."""
        c = self.cfg
        if not c.min_free_gb:
            return c.stages
        try:
            free_gb = self.disk_free() / 2 ** 30
        except OSError as exc:
            if not self._disk_check_failed:
                self.log(f"[{c.worker_id}] disk check failed ({exc}); guard skipped until "
                         "it works again")
            self._disk_check_failed = True
            return c.stages
        self._disk_check_failed = False
        if free_gb >= c.min_free_gb:
            if self._disk_low is not None:
                self.log(f"[{c.worker_id}] disk space recovered: {free_gb:.1f} GB free; "
                         "claiming all stages again")
                self._disk_low = None
            return c.stages
        clip_only = free_gb < min(DISK_CLIP_ONLY_GB, c.min_free_gb)
        stages = tuple(s for s in c.stages if (s == "clip" if clip_only else s != "fetch"))
        held = tuple(s for s in c.stages if s not in stages)
        now = time.monotonic()
        if held != self._disk_low or now - self._last_disk_log >= c.stall_log_s:
            self._last_disk_log = now
            if held:
                self.log(f"[{c.worker_id}] DISK LOW: {free_gb:.1f} GB free on {self.corpus} "
                         f"(floor {c.min_free_gb:g} GB): not claiming {','.join(held)}")
        self._disk_low = held or None
        return stages

    def yielding(self) -> bool:
        """True while a process matches --yield-while-pgrep (a release build):
        the worker then claims nothing new. Logs each change once. A failing
        check is logged once and does not hold the worker."""
        c = self.cfg
        if not c.yield_while_pgrep:
            return False
        try:
            hits = yield_match(c.yield_while_pgrep)
            self._yield_check_failed = False
        except (OSError, subprocess.SubprocessError) as exc:
            if not self._yield_check_failed:
                self.log(f"[{c.worker_id}] yield check failed: {exc}; claiming as usual")
            self._yield_check_failed = True
            hits = []
        if hits and not self._yielding:
            self.log(f"[{c.worker_id}] YIELD: release build active ({hits[0][:160]}); "
                     f"claiming nothing, re-checking every {c.yield_poll_s:g} s")
        elif self._yielding and not hits:
            self.log(f"[{c.worker_id}] YIELD over: no process matches "
                     f"{c.yield_while_pgrep!r}; claiming again")
        self._yielding = bool(hits)
        return self._yielding

    def touch(self, con=None):
        try:
            self.queue.touch_worker(self.cfg.worker_id, self.cfg.stages, con)
        except sqlite3.Error:
            pass

    def log_idle(self) -> None:
        """Idle with episodes left: say why, loudly and repeatedly."""
        c = self.cfg
        now = time.monotonic()
        if now - self._last_stall_log < c.stall_log_s:
            return
        self._last_stall_log = now
        counts = self.queue.stage_counts(c.shard)
        use = self.queue.buffer_use(c.max_buffered, c.max_buffered_transcript, shard=c.shard)
        blocked = "fetch" in c.stages and any(
            cap is not None and n >= cap for n, cap in use.values())
        buffered = (f"buffered={use[None][0]}/{use[None][1]}" if None in use else
                    "buffered " + " ".join(f"{t}={n}/{cap}" for t, (n, cap) in use.items()))
        held = {s: counts[s] for s in AUDIO_HOLDING_STAGES if counts.get(s)}
        first_only = first_attempt_only_stages(c)
        retries = self.queue.retries_waiting(
            [s for s in first_only if s in c.stages], c.shard)
        self.log(f"[{c.worker_id}] {'STALLED' if blocked else 'idle'}: nothing claimable for "
                 f"stages {','.join(c.stages)}; {buffered}; "
                 "queue " + " ".join(f"{s}={counts[s]}" for s in STAGES if counts.get(s))
                 + (f"; audio held at {held} needs a worker with `clip` in --stages"
                    if blocked and held else "")
                 + (f"; {retries} {','.join(first_only)} retr{'y waits' if retries == 1 else 'ies wait'}"
                    f" for a faster-whisper worker (--backend {c.backend} takes first attempts"
                    " only)" if retries else ""))

    # -- signals
    def install_signal_handlers(self):
        def handler(signum, _frame):
            if self.stop.is_set():
                raise KeyboardInterrupt
            self.log(f"[{self.cfg.worker_id}] signal {signum}: stop claiming, "
                     "finishing/releasing current lease")
            self.stop.set()
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)

    # -- loop
    def run(self) -> dict:
        c = self.cfg
        gc_corpus(self.corpus, self.queue.con)
        while not self.stop.is_set():
            if c.stop_at is not None and time.time() >= c.stop_at:
                self.log(f"[{c.worker_id}] stop-at reached")
                break
            if c.max_episodes is not None and self.processed >= c.max_episodes:
                break
            if self.yielding():   # in-flight work is done: nothing new while a build runs
                self.touch()
                self.stop.wait(c.yield_poll_s)
                continue
            try:
                lease = self.queue.claim(c.worker_id, self.claimable_stages(), shard=c.shard,
                                         max_attempts=c.max_attempts, lease_s=c.lease_s,
                                         max_buffered=c.max_buffered,
                                         max_buffered_transcript=c.max_buffered_transcript,
                                         first_attempt_only=first_attempt_only_stages(c),
                                         tier_priority=c.tier_priority)
            except sqlite3.Error as exc:
                self.log(f"[{c.worker_id}] claim failed: {exc}")
                self.stop.wait(c.idle_poll_s)
                continue
            if lease is None:
                left = self.queue.remaining(c.stages, c.shard)
                uncounted = drain_uncounted_retries(c)
                retries = left - self.queue.remaining(c.stages, c.shard, skip_retries_of=uncounted) \
                    if uncounted and left else 0
                if left - retries == 0 and c.exit_when_drained:
                    self.log(f"[{c.worker_id}] queue drained" + (
                        f" ({retries} {','.join(uncounted)} retr"
                        f"{'y' if retries == 1 else 'ies'} left to other workers)"
                        if retries else ""))
                    break
                if left:  # work left (--wait-forever: retries only, too): say why idle
                    self.log_idle()
                self.touch()
                self.stop.wait(c.idle_poll_s)
                continue
            self._last_stall_log = float("-inf")
            self.processed += 1
            try:
                if self.run_lease(lease) == DB_RELEASED:
                    self.processed -= 1   # a free release is not an episode handled
            except WorkerFatal as exc:
                self.fatal = str(exc)
                self.log(f"[{c.worker_id}] FATAL, worker stopping: {exc}")
                break
            self.touch()
        out = {"worker": c.worker_id, "processed": self.processed}
        if self.api is not None:
            out["cost_usd"] = round(self.api.budget.spent, 6)
            out["corpus_spend_usd"] = round(self.api.budget.total(), 6)
        if self.budget_stopped:
            out["budget_exhausted"] = self.budget_stopped
        if self.fatal:
            out["fatal"] = self.fatal
        return out

    def ceiling_for(self, lease: Lease) -> float:
        if lease.stage == "fetch":
            return MEDIA_DEADLINE_S + 600
        if lease.stage == "transcribe":
            return self.transcribe_timeout(lease.row.get("duration_s")) + 300
        if lease.stage == "clip":
            return self.clip_timeout(lease.row.get("duration_s")) + 300
        if lease.stage == "screen":
            return 3600.0
        if lease.stage == "diarize":
            return self.diarize_timeout(lease.row.get("duration_s")) + 300
        return 1800.0

    def diarize_timeout(self, duration_s) -> float:
        return diarize_timeout(duration_s)

    def clip_timeout(self, duration_s) -> float:
        return 300.0 + 0.5 * float(duration_s or 3600)

    def transcribe_timeout(self, duration_s) -> float:
        return self.cfg.timeout_base_s + self.cfg.timeout_factor * float(duration_s or 3600)

    def run_lease(self, lease: Lease) -> str:
        c = self.cfg
        if not self.queue.tier_runs(lease.episode_id, lease.stage):
            # Its show changed tier after the claim: never run the stage (a paid
            # Jev call, say); completing at the same stage routes it on (tier_next).
            self.log(f"[{c.worker_id}] {lease.stage} {lease.episode_id} skipped: its"
                     " coverage tier leaves the stage out")
            if not self.queue.complete(lease, next_stage=lease.stage,
                                       error=f"skipped: coverage tier leaves {lease.stage} out"):
                return "lease_lost"
            return "skipped"
        hb = Heartbeat(self.queue, lease, c.lease_s, self.ceiling_for(lease), touch=self.touch)
        hb.start()
        self.log(f"[{c.worker_id}] {lease.stage} {lease.episode_id} attempt {lease.attempt}")
        try:
            handler = getattr(self, "do_" + lease.stage)
            result = handler(lease, hb)
        except KeyboardInterrupt:
            hb.stop()
            self.queue.release(lease, "worker_interrupted")
            raise
        except WorkerFatal:
            raise
        except LeaseLost as exc:
            result = "lease_lost"
            self.log(f"[{c.worker_id}] lease lost on {lease.episode_id}: {exc}")
        except Exception as exc:
            hb.stop()
            metrics = {"wall_s": round(time.monotonic() - lease.started, 3)}
            if lease.stage == "diarize":
                result = self.diarize_escaped(lease, exc, metrics)
            else:
                ok = self.queue.fail(
                    lease, "error", f"{type(exc).__name__}: {exc}",
                    max_attempts=c.max_attempts, metrics=metrics,
                    terminal=lease.stage == "fetch" and isinstance(exc, TERMINAL_FETCH_ERRORS))
                result = "error" if ok else "lease_lost"
            self.log(f"[{c.worker_id}] {lease.stage} {lease.episode_id} failed: {exc}")
        finally:
            hb.stop()
        return result

    # -- stages
    def do_fetch(self, lease: Lease, hb: Heartbeat) -> str:
        row = lease.row
        url = row["enclosure_url"]
        suffix = Path(urlparse(url).path).suffix.lower()
        suffix = suffix if suffix in AUDIO_SUFFIXES else ".mp3"
        durations = []
        t0, cpu0 = time.monotonic(), time.process_time()

        def verify(path):  # download_media records the returned duration
            durations.append(acq.probe_audio_duration(path))
            return durations[-1]

        fetcher = self.fetcher or acq.requests_fetcher()

        def download(target, deadline_s):
            return acq.download_media(
                target, dest_dir=self.dirs["audio-tmp"], name=lease.episode_id + suffix,
                fetcher=fetcher, max_bytes=MEDIA_MAX_BYTES, timeout_s=60.0,
                deadline_s=deadline_s, content_addressed=True, verify_fn=verify)

        fallback = None
        try:
            obs = download(url, MEDIA_DEADLINE_S)
        except acq.BadRedirectHost as exc:
            # A tracking prefix redirected to a dead host: one retry at the
            # media URL embedded in the enclosure (allowlisted media CDN only).
            inner = acq.embedded_media_url(url)
            if inner is None:
                raise
            fallback = {"requested_url": inner, "failed_host": exc.host,
                        "reason": exc.reason, "failed_url": exc.url}
            self.log(f"[{self.cfg.worker_id}] fetch {lease.episode_id}: {exc};"
                     f" retrying once at {inner!r}")
            obs = download(inner, max(1.0, MEDIA_DEADLINE_S - (time.monotonic() - t0)))
        duration = durations[-1]
        verification = {k: obs.get(k) for k in (
            "length_verified", "probe_duration_s", "content_encoding")}
        if fallback:
            verification["fallback"] = fallback
        observation = {
            "episode_id": lease.episode_id, "started_at": obs["fetch_started_at"],
            "finished_at": obs["fetch_ended_at"], "enclosure_url": url,
            "final_url": obs["final_url"], "http_status": obs["http_status"],
            "redirect_chain": json.dumps(obs["redirect_chain"]),
            "headers": json.dumps(obs["response_headers"], sort_keys=True),
            "bytes": obs["bytes"], "sha256": obs["sha256"],
            "duration_s": round(duration, 3), "worker": self.cfg.worker_id,
            "host": self.queue.host,
            "verification": json.dumps(verification, sort_keys=True)}
        ok = self.queue.complete(
            lease, observation=observation,
            updates={"audio_path": obs["path"], "audio_sha256": obs["sha256"],
                     "audio_bytes": obs["bytes"], "duration_s": round(duration, 3)},
            metrics={"wall_s": round(time.monotonic() - t0, 3),
                     "cpu_s": round(time.process_time() - cpu0, 3),
                     "audio_s": round(duration, 3)})
        if not ok:
            raise LeaseLost("fetch completion fenced out")
        return "ok"

    def do_transcribe(self, lease: Lease, hb: Heartbeat) -> str:
        c, row = self.cfg, lease.row
        audio = row.get("audio_path")
        if not audio or not os.path.exists(audio):
            # Audio is host-local and may be gone (crash after delete, other
            # host): refetch, carrying the attempt so this cannot ping-pong.
            if lease.attempt >= c.max_attempts:
                self.queue.fail(lease, "error", "audio_missing", max_attempts=c.max_attempts)
            elif not self.queue.complete(lease, next_stage="fetch", outcome="error",
                                         error="audio_missing", carry_attempts=True,
                                         updates={"audio_path": None}):
                raise LeaseLost("requeue fenced out")
            return "error"
        if c.backend in API_STT_BACKENDS:
            return self.transcribe_api(lease, hb, audio)
        work = self.dirs["work"]
        out = work / f"{lease.episode_id}.{lease.token}.json"
        log_path = work / f"{lease.episode_id}.{lease.token}.log"
        cmd = [*self.transcribe_cmd, "--audio", audio, "--out", str(out),
               "--episode-id", lease.episode_id, "--model", c.model, "--beam", str(c.beam),
               "--threads", str(c.threads), "--chunk-s", str(c.chunk_s),
               "--overlap-s", str(c.overlap_s), "--lang-windows", str(c.lang_windows),
               "--probed-duration", str(row.get("duration_s") or 0),
               "--audio-sha256", row.get("audio_sha256") or "",
               "--audio-bytes", str(row.get("audio_bytes") or 0),
               "--fetch-observation-id", str(row.get("fetch_observation_id") or "")]
        if c.backend != "faster-whisper":  # default command stays byte-identical
            cmd += ["--backend", c.backend]
        if c.backend == "parakeet" and self.parakeet_pin:
            cmd += ["--parakeet-pin", json.dumps(self.parakeet_pin, sort_keys=True)]
        if c.backend == "parakeet" and c.gap_fill_model:
            cmd += ["--gap-fill-model", c.gap_fill_model,
                    "--gap-fill-min-s", str(c.gap_fill_min_s)]
        show_lang = self.queue.show_language(row.get("feed_url"))
        if show_lang:  # default command stays byte-identical without one
            cmd += ["--show-language", show_lang]
            if not c.language_hint:
                cmd += ["--no-language-hint"]

        def abort():
            if hb.lost.is_set():
                return "lease_lost"
            if self.stop.is_set():
                return "shutdown"
            return None

        try:
            res = supervise(cmd, mem_cap_mb=c.mem_cap_mb,
                            timeout_s=self.transcribe_timeout(row.get("duration_s")),
                            poll_s=c.poll_s, abort=abort, log_path=log_path)
            metrics = {k: res[k] for k in ("wall_s", "cpu_s", "peak_rss_mb")}
            metrics["audio_s"] = row.get("duration_s")
            outcome = res["outcome"]
            if outcome == "lease_lost":
                raise LeaseLost("lease lost while transcribing")
            if outcome == "shutdown":
                hb.stop()
                self.queue.release(lease, "worker_shutdown", metrics)
                return "released"
            if outcome != "ok":
                hb.stop()
                self.queue.fail(lease, outcome, f"rc={res['returncode']} "
                                f"peak_rss_mb={res['peak_rss_mb']} {res['log_tail'][-400:]}",
                                max_attempts=c.max_attempts, metrics=metrics)
                return outcome
            try:
                doc = json.loads(out.read_text(encoding="utf-8"))
                validate_transcript(doc, row.get("duration_s"))
            except (OSError, ValueError, TranscriptInvalid) as exc:
                hb.stop()
                self.queue.fail(lease, "error", f"invalid transcript: {exc}",
                                max_attempts=c.max_attempts, metrics=metrics)
                return "error"
            if c.repair_backend and tq.pending(doc.get("quality")):
                doc = None if self.stop.is_set() else \
                    self.repair_transcript(lease, hb, audio, out, doc, show_lang)
                if doc is None:
                    hb.stop()
                    self.queue.release(lease, "worker_shutdown", metrics)
                    return "released"
            hb.stop()
            if not self.queue.heartbeat(lease, c.lease_s):
                raise LeaseLost("lease lost before publishing transcript")
            final = self.dirs["transcripts"] / f"{lease.episode_id}.json"
            os.replace(out, final)
            acq._fsync_dir(final.parent)
            # Audio stays (audio_path kept): the clip stage cuts evidence from it.
            gf_prov = (doc.get("provenance") or {}).get("gap_fill")
            updates = {"gap_fill_status": gap_fill_summary(gf_prov)} if gf_prov else None
            if not self.queue.complete(lease, metrics=metrics, updates=updates):
                raise LeaseLost("transcribe completion fenced out")
            self.note_gap_fill(lease.episode_id, gf_prov)
            return "ok"
        finally:
            out.unlink(missing_ok=True)
            log_path.unlink(missing_ok=True)

    def transcribe_api(self, lease: Lease, hb: Heartbeat, audio: str) -> str:
        """The transcribe stage on a hosted API, in this process: chunked
        upload + seam stitching (api_stt.transcribe_episode). The episode's
        estimated cost is checked against the worker's budget before the
        first request; on the cap the lease is released uncharged and the
        worker stops claiming. A rejected key / payment / persistent rate
        limit releases the lease and stops the worker (exit 3); a rejected
        request (400/413) or a transient failure after retries charges an
        attempt."""
        from earwitness.pipeline import api_stt
        from earwitness.pipeline.refine import BudgetExhausted, SpeechUnavailable
        c, row, api, ep_id = self.cfg, lease.row, self.api, lease.episode_id
        t0, spent0 = time.monotonic(), api.budget.spent

        def metrics():
            return {"wall_s": round(time.monotonic() - t0, 3), "audio_s": row.get("duration_s"),
                    "cost_usd": round(api.budget.spent - spent0, 8)}

        def abort():
            if hb.lost.is_set():
                return "lease_lost"
            return "shutdown" if self.stop.is_set() else None

        try:
            res = api_stt.transcribe_episode(audio, api, chunk_s=c.chunk_s,
                                             overlap_s=c.overlap_s, ffmpeg=self.ffmpeg,
                                             duration_s=row.get("duration_s"), abort=abort)
        except api_stt.Aborted as exc:
            if str(exc) == "lease_lost":
                raise LeaseLost("lease lost while transcribing") from None
            hb.stop()
            self.queue.release(lease, "worker_shutdown", metrics())
            return "released"
        except BudgetExhausted as exc:
            hb.stop()
            self.queue.release(lease, f"budget: {exc}"[:1000], metrics())
            self.budget_stopped = str(exc)
            self.stop.set()
            self.log(f"[{c.worker_id}] budget reached, stopping: {exc}")
            return "released"
        except SpeechUnavailable as exc:  # ApiFatal, RateLimited
            hb.stop()
            self.queue.release(lease, f"fatal transcribe error: {exc}"[:1000], metrics())
            raise WorkerFatal(f"transcribe API error on {ep_id}: {exc}") from None
        except Exception as exc:  # ClipFailed (rejected / transient after retries), ffmpeg
            hb.stop()
            self.queue.fail(lease, "error", f"api transcribe: {type(exc).__name__}: {exc}",
                            max_attempts=c.max_attempts, metrics=metrics())
            return "error"
        decoded = res["decoded_duration_s"]
        usd_h, min_s, source = api_stt.price(api.provider, api.model)
        doc = {
            "ep_id": ep_id, "model": api.model, "transcribed_at": utcnow(),
            "duration_min": round(decoded / 60, 2), "language": res["language"],
            "segments": res["segments"],
            "provenance": {
                "backend": f"{api.provider}-api", "provider": api.provider,
                "endpoint": api.url, "model": api.model,
                "pricing": {"usd_per_audio_hour": usd_h, "min_billed_s": min_s,
                            "source": source},
                "cost": res["cost"], "segments_source": res["segments_source"],
                "condition_on_previous_text": False, "vad_filter": False,
                "chunk_s": c.chunk_s, "overlap_s": c.overlap_s,
                "chunk_format": f"{api_stt.CHUNK_FORMAT} {stt.SAMPLE_RATE} Hz mono",
                "seam_dedupe": stt.SEAM_REPAIR_VERSION,
                "decoder": "ffmpeg sequential f32le 16k mono",
                "audio_sha256": row.get("audio_sha256"), "audio_bytes": row.get("audio_bytes"),
                "probed_duration_s": row.get("duration_s"), "decoded_duration_s": decoded,
                "fetch_observation_id": row.get("fetch_observation_id"),
                "chunk_languages": res["chunk_languages"],
                "wall_s": round(time.monotonic() - t0, 3), "host": socket.gethostname()}}
        doc["quality"] = tq.assess(doc, self.queue.show_language(row.get("feed_url")))
        try:
            validate_transcript(doc, row.get("duration_s"))
        except TranscriptInvalid as exc:
            hb.stop()
            self.queue.fail(lease, "error", f"invalid transcript: {exc}",
                            max_attempts=c.max_attempts, metrics=metrics())
            return "error"
        hb.stop()
        if not self.queue.heartbeat(lease, c.lease_s):
            raise LeaseLost("lease lost before publishing transcript")
        atomic_write_json(self.dirs["transcripts"] / f"{ep_id}.json", doc)
        if not self.queue.complete(lease, metrics=metrics()):
            raise LeaseLost("transcribe completion fenced out")
        return "ok"

    def delete_audio_if_unreferenced(self, path: str) -> bool:
        refs = self.queue.con.execute("SELECT COUNT(*) FROM episodes WHERE audio_path=?",
                                      (path,)).fetchone()[0]
        if refs:
            return False
        Path(path).unlink(missing_ok=True)
        return True

    def load_transcript(self, lease: Lease):
        """The validated transcript, or None after sending the episode back
        to transcribe (attempts carried, so this cannot ping-pong)."""
        tpath = self.dirs["transcripts"] / f"{lease.episode_id}.json"
        try:
            doc = json.loads(tpath.read_text(encoding="utf-8"))
            validate_transcript(doc, lease.row.get("duration_s"))
            return doc
        except (OSError, ValueError, TranscriptInvalid) as exc:
            if not self.queue.complete(lease, next_stage="transcribe", outcome="error",
                                       error=f"transcript_unusable: {exc}"[:500],
                                       carry_attempts=True):
                raise LeaseLost("requeue fenced out")
            return None

    def do_detect(self, lease: Lease, hb: Heartbeat) -> str:
        row = lease.row
        t0, cpu0 = time.monotonic(), time.process_time()
        doc = self.load_transcript(lease)
        if doc is None:
            return "error"
        obs = self.queue.con.execute("SELECT finished_at FROM fetch_observations WHERE id=?",
                                     (row.get("fetch_observation_id"),)).fetchone()
        fetch_date = (obs["finished_at"] or "")[:10] if obs else None
        cands = _detect_all().detect_episode(
            lease.episode_id, doc["segments"],
            ep_meta={"show_title": row.get("show_title"), "ep_title": row.get("ep_title"),
                     "pub_date": row.get("pub_date"), "fetch_date": fetch_date},
            transcript_name=f"{lease.episode_id}.json", fetch_date=fetch_date,
            fetch_date_source="fetch_observation" if fetch_date else "fallback:processing-date")
        atomic_write_json(self.dirs["detections"] / f"{lease.episode_id}.json", cands)
        nxt, updates = self.queue.detect_route()
        if not self.queue.complete(lease, next_stage=nxt, updates=updates, metrics={
                "wall_s": round(time.monotonic() - t0, 3),
                "cpu_s": round(time.process_time() - cpu0, 3),
                "audio_s": row.get("duration_s")}):
            raise LeaseLost("detect completion fenced out")
        return "ok"

    def do_jev(self, lease: Lease, hb: Heartbeat) -> str:
        c, ep_id = self.cfg, lease.episode_id
        t0, cpu0 = time.monotonic(), time.process_time()
        doc = self.load_transcript(lease)
        if doc is None:
            return "error"
        try:
            res = jev_screen.screen_episode(doc["segments"], transport=self.jev_transport,
                                            model=c.jev_model, episode_id=ep_id)
        except Exception as exc:
            usage = getattr(exc, "usage", None) or {"input_tokens": 0, "output_tokens": 0}
            nxt, upd = self.queue.diarize_route()
            refused = getattr(exc, "cause", exc)
            if isinstance(refused, jev_budget.JevBudgetRefused):
                return self.jev_skipped_budget(lease, hb, refused, t0, usage, nxt, upd)
            return self.api_stage_failed(
                lease, hb, exc, t0, kind=jev_screen.classify_error(exc), usage=usage,
                cost=jev_screen.cost_usd(usage), status_col="jev_status", next_stage=nxt,
                updates=upd)
        res = {"episode_id": ep_id, "screened_at": utcnow(), **res}
        if hb.lost.is_set() or not self.queue.heartbeat(lease, c.lease_s):
            raise LeaseLost("lease lost before publishing jev windows")
        atomic_write_json(self.dirs["jev"] / f"{ep_id}.json", res)
        nxt, updates = self.queue.jev_route()
        if not self.queue.complete(lease, next_stage=nxt, updates={
                "jev_status": "ok", **updates}, metrics={
                "wall_s": round(time.monotonic() - t0, 3),
                "cpu_s": round(time.process_time() - cpu0, 3),
                "audio_s": lease.row.get("duration_s"),
                "llm_usage": json.dumps(res["usage"], sort_keys=True),
                "cost_usd": res["cost_usd"]}):
            raise LeaseLost("jev completion fenced out")
        return "ok"

    def jev_skipped_budget(self, lease: Lease, hb: Heartbeat, refused, t0: float,
                           usage: dict, nxt: str, upd: dict) -> str:
        """The spend cap refused a Jev request: no Jev result, jev_status
        jev_skipped_budget, and the episode moves on as with no jev worker.
        Tokens billed before the refusal stay on the stage run."""
        hb.stop()
        if not self.queue.complete(lease, next_stage=nxt, updates={
                **upd, "jev_status": JEV_SKIPPED_BUDGET}, metrics={
                "wall_s": round(time.monotonic() - t0, 3),
                "audio_s": lease.row.get("duration_s"),
                "llm_usage": json.dumps(usage, sort_keys=True),
                "cost_usd": jev_screen.cost_usd(usage)}):
            raise LeaseLost("jev completion fenced out")
        code = refused.code
        self.log(f"[{self.cfg.worker_id}] jev {lease.episode_id} skipped: {code}"
                 f" ({refused.reason or 'cap'})")
        now = time.monotonic()
        if now - self._jev_alerted.get(code, float("-inf")) >= JEV_ALERT_COOLDOWN_S:
            self._jev_alerted[code] = now
            jev_budget_alert(self.corpus, code, f"{self.cfg.worker_id}: Jev sends nothing"
                             f" ({refused.reason or 'spend cap reached'}); episodes go on with"
                             f" jev_status {JEV_SKIPPED_BUDGET}", self.log)
        return "skipped"

    def do_name(self, lease: Lease, hb: Heartbeat) -> str:
        """Name the brands of the Jev-flagged spans (local CPU work). Never
        kills an episode: errors retry, and the last attempt moves on with
        name_status 'failed: ...'."""
        c, ep_id = self.cfg, lease.episode_id
        t0, cpu0 = time.monotonic(), time.process_time()
        doc = self.load_transcript(lease)
        if doc is None:
            return "error"
        nxt, upd = self.queue.diarize_route()
        jev = _read_json(self.dirs["jev"] / f"{ep_id}.json")
        if bn.jev_windows(jev) is None:
            if not self.queue.complete(lease, next_stage=nxt, updates={
                    "name_status": NO_JEV_RESULT, **upd}):
                raise LeaseLost("name completion fenced out")
            return "skipped"
        try:
            res = bn.name_episode(ep_id, doc["segments"], jev, self.namer,
                                  threshold=c.jev_threshold,
                                  context_windows=c.jev_context_windows,
                                  sub_window_s=c.name_sub_window_s)
        except Exception as exc:
            return self.api_stage_failed(lease, hb, exc, t0, kind="transient", usage=None,
                                         cost=None, status_col="name_status", next_stage=nxt,
                                         updates=upd)
        res = {**res, "named_at": utcnow()}
        if hb.lost.is_set() or not self.queue.heartbeat(lease, c.lease_s):
            raise LeaseLost("lease lost before publishing names")
        with bn.names_lock(self.dirs["names"]):
            atomic_write_json(self.dirs["names"] / f"{ep_id}.json", res)
        if not self.queue.complete(lease, next_stage=nxt, updates={
                "name_status": "ok", **upd}, metrics={
                "wall_s": round(time.monotonic() - t0, 3),
                "cpu_s": round(time.process_time() - cpu0, 3),
                "audio_s": lease.row.get("duration_s")}):
            raise LeaseLost("name completion fenced out")
        return "ok"

    def diarize_command(self, lease: Lease, out: Path) -> list:
        c = self.cfg
        return diarize_command(self.diarize_cmd, lease.row, lease.episode_id, out,
                               device=c.diarize_device, window_s=c.diarize_window_s,
                               overlap_s=c.diarize_overlap_s, threads=max(c.threads, 4),
                               ffmpeg=self.ffmpeg)

    def do_diarize(self, lease: Lease, hb: Heartbeat) -> str:
        """Speaker turns for the episode (speakers/<ep>.json) from a
        supervised child (RSS cap, timeout). Never kills an episode: an OOM
        or timeout moves on at once (a rerun would hit the same wall), other
        child failures retry and the last attempt moves on; either way with
        diarize_status 'failed: ...' and no speakers. Database errors (and
        anything raised outside the child's handling) go to diarize_escaped
        and never count toward the circuit breaker."""
        c, row, ep_id = self.cfg, lease.row, lease.episode_id
        t0 = time.monotonic()
        nxt, screen_status = self.queue.screen_route()
        metrics = {"audio_s": row.get("duration_s")}

        def move_on(status, outcome="ok", error=None):
            metrics.setdefault("wall_s", round(time.monotonic() - t0, 3))
            hb.stop()
            if not self.queue.complete(lease, next_stage=nxt, metrics=metrics, outcome=outcome,
                                       error=error, updates={"diarize_status": status[:500],
                                                             "screen_status": screen_status}):
                raise LeaseLost("diarize completion fenced out")
            return "ok" if outcome == "ok" else "error"

        final = self.dirs["speakers"] / f"{ep_id}.json"
        prior = _read_json(final)
        if dz.is_current(prior, row.get("audio_sha256"), window_s=c.diarize_window_s,
                         overlap_s=c.diarize_overlap_s, device=c.diarize_device):
            try:  # already diarized (backfill, or a crash after publishing)
                dz.validate_speakers(prior, ep_id, row.get("duration_s"))
                return move_on("ok (existing)")
            except ValueError:
                pass
        audio = row.get("audio_path")
        if not audio or not os.path.exists(audio):
            return move_on("skipped: audio_missing")
        work = self.dirs["work"]
        out = work / f"{ep_id}.{lease.token}.speakers.json"
        log_path = work / f"{ep_id}.{lease.token}.diarize.log"
        try:
            res = supervise(self.diarize_command(lease, out), mem_cap_mb=c.diarize_mem_cap_mb,
                            timeout_s=self.diarize_timeout(row.get("duration_s")),
                            poll_s=c.poll_s, log_path=log_path,
                            abort=lambda: "lease_lost" if hb.lost.is_set()
                            else "shutdown" if self.stop.is_set() else None)
            metrics.update({k: res[k] for k in ("wall_s", "cpu_s", "peak_rss_mb")})
            outcome = res["outcome"]
            if outcome == "lease_lost":
                raise LeaseLost("lease lost while diarizing")
            if outcome == "shutdown":
                hb.stop()
                self.queue.release(lease, "worker_shutdown", metrics)
                return "released"
            why = None
            if outcome != "ok":
                why = (f"{outcome}: rc={res['returncode']} peak_rss_mb={res['peak_rss_mb']} "
                       f"{res['log_tail'][-300:]}")
                kind = f"error rc={res['returncode']}" if outcome == "error" else outcome
            else:
                try:
                    doc = json.loads(out.read_text(encoding="utf-8"))
                    dz.validate_speakers(doc, ep_id, row.get("duration_s"))
                except (OSError, ValueError) as exc:
                    outcome, why = "error", f"error: invalid speakers doc: {exc}"
                    kind = "invalid_output"
            if why is not None:
                self.diarize_breaker(lease, hb, kind, why, metrics)
                if outcome in ("error", "killed") and lease.attempt < c.max_attempts:
                    hb.stop()
                    self.queue.fail(lease, outcome, why, max_attempts=c.max_attempts,
                                    metrics=metrics)
                    return outcome
                return move_on(f"failed: {why}", outcome=outcome, error=why[:1000])
            hb.stop()
            try:
                alive = self.queue.heartbeat(lease, c.lease_s)
            except sqlite3.OperationalError:   # keep the result: one retry
                self.stop.wait(DIARIZE_DB_RETRY_S)
                alive = self.queue.heartbeat(lease, c.lease_s)
            if not alive:
                raise LeaseLost("lease lost before publishing speakers")
            os.replace(out, final)
            acq._fsync_dir(final.parent)
            self._diarize_streak = (None, set())
            return move_on("ok")
        except (LeaseLost, WorkerFatal, sqlite3.Error):
            raise   # a database error is not the child's: diarize_escaped handles it
        except Exception as exc:  # e.g. the diarize python vanished: still never dead
            why = f"error: {type(exc).__name__}: {exc}"
            self.diarize_breaker(lease, hb, f"exception {type(exc).__name__}", why, metrics)
            if lease.attempt < c.max_attempts:
                hb.stop()
                self.queue.fail(lease, "error", why, max_attempts=c.max_attempts,
                                metrics=metrics)
                return "error"
            return move_on(f"failed: {why}", outcome="error", error=why[:1000])
        finally:
            out.unlink(missing_ok=True)
            log_path.unlink(missing_ok=True)

    def diarize_escaped(self, lease: Lease, exc: Exception, metrics: dict) -> str:
        """An error that do_diarize does not handle itself: any database
        error (routing, heartbeat, fail, completing a skip, reuse or move-on)
        and anything raised outside the child's handling. Never marks the
        episode dead and never loops:
          - sqlite "locked"/"busy" gives the lease back uncharged while the
            episode has had fewer than DIARIZE_DB_RELEASES such releases in
            its whole life (stage_runs); the result DB_RELEASED keeps it out
            of --max-episodes;
          - otherwise the attempt is charged: before the last attempt it is
            a normal retry (fail, never dead there); on the last attempt the
            episode moves on along the screen route with diarize_status
            'failed: ...' and no speakers. If routing raises a database
            error the lease is left to expire (reclaim routes it in its own
            transaction); another routing error goes to clip with
            screen_status 'skipped: routing failed: ...';
          - if recording any of this raises (database or not), the lease is
            left to expire, and reclaim retries diarize or, at the last
            attempt, moves the episode on (never dead). Each step is fenced:
            at most max_attempts charged runs plus DIARIZE_DB_RELEASES free
            ones per episode."""
        why = f"{type(exc).__name__}: {exc}"
        msg = str(exc).lower()
        transient = isinstance(exc, sqlite3.OperationalError) \
            and ("locked" in msg or "busy" in msg)
        try:
            freed = self.queue.diarize_db_releases(lease.episode_id) if transient else None
            if transient and freed < DIARIZE_DB_RELEASES:
                ok = self.queue.release(lease, f"{DIARIZE_DB_RELEASE_REASON}{why}"[:1000],
                                        metrics, delay_s=DIARIZE_DB_RELEASE_BACKOFF_S * 2 ** freed)
                return DB_RELEASED if ok else "lease_lost"
            if lease.attempt < self.cfg.max_attempts:
                ok = self.queue.fail(lease, "error", why, max_attempts=self.cfg.max_attempts,
                                     metrics=metrics)
                return "error" if ok else "lease_lost"
            try:
                nxt, screen_status = self.queue.screen_route()
            except sqlite3.Error:
                raise
            except Exception as route_exc:
                nxt, screen_status = "clip", \
                    f"skipped: routing failed: {type(route_exc).__name__}: {route_exc}"[:500]
            ok = self.queue.complete(lease, next_stage=nxt, metrics=metrics, outcome="error",
                                     error=why[:1000],
                                     updates={"diarize_status": f"failed: {why}"[:500],
                                              "screen_status": screen_status})
            return "error" if ok else "lease_lost"
        except Exception as again:
            self.log(f"[{self.cfg.worker_id}] diarize {lease.episode_id}: could not record "
                     f"the failure ({type(again).__name__}: {again}); the lease expires and "
                     "reclaim moves it on")
            return "lease_lost"

    def diarize_breaker(self, lease: Lease, hb: Heartbeat, kind: str, why: str,
                        metrics: dict) -> None:
        """Count a diarize child failure; on DIARIZE_BREAKER_EPISODES
        different episodes failing in a row with the same `kind`, release
        the lease uncharged and raise WorkerFatal."""
        last, eps = self._diarize_streak
        eps = (eps if kind == last else set()) | {lease.episode_id}
        self._diarize_streak = (kind, eps)
        if len(eps) < DIARIZE_BREAKER_EPISODES:
            return
        hb.stop()
        reason = f"fatal diarize error: {why}"[:1000]
        try:
            self.queue.release(lease, reason, metrics)
        except sqlite3.Error:   # a lock must not swallow the trip: retry once, then trip
            self.stop.wait(DIARIZE_DB_RETRY_S)
            try:
                self.queue.release(lease, reason, metrics)
            except sqlite3.Error as exc:
                self.log(f"[{self.cfg.worker_id}] could not release {lease.episode_id} "
                         f"({exc}); the lease expires and reclaim retries it")
        raise WorkerFatal(f"diarize child failed ({kind}) on {len(eps)} episodes in a row, "
                          f"last {lease.episode_id}: {why[:300]}")

    def do_screen(self, lease: Lease, hb: Heartbeat) -> str:
        c, ep_id = self.cfg, lease.episode_id
        t0, cpu0 = time.monotonic(), time.process_time()
        if lease.row.get("repair_requeued_at"):  # repair-loops requeue: no paid re-screen
            if not self.queue.complete(lease, updates={
                    "screen_status": "skipped: transcript repaired (repair-loops); the paid"
                                     " screen is not re-run",
                    "repair_requeued_at": None},
                    metrics={"wall_s": round(time.monotonic() - t0, 3)}):
                raise LeaseLost("screen completion fenced out")
            return "ok"
        doc = self.load_transcript(lease)
        if doc is None:
            return "error"
        try:
            res = llm_screen.screen_transcript(
                doc["segments"], client=self.llm_client, model=c.screen_model,
                window_s=c.screen_window_s, overlap_s=c.screen_overlap_s,
                effort=c.screen_effort)
        except Exception as exc:
            return self.screen_failed(lease, hb, exc, t0)
        llm_screen.assign_read_ids(ep_id, res)
        res.update(episode_id=ep_id, screened_at=utcnow(),
                   cost_usd=llm_screen.cost_usd(res["usage"], c.screen_model))
        if hb.lost.is_set() or not self.queue.heartbeat(lease, c.lease_s):
            raise LeaseLost("lease lost before publishing screen")
        atomic_write_json(self.dirs["screens"] / f"{ep_id}.json", res)
        failed = len(res["failed_windows"])
        if not self.queue.complete(lease, updates={
                "screen_status": f"ok ({failed} windows failed)" if failed else "ok"}, metrics={
                "wall_s": round(time.monotonic() - t0, 3),
                "cpu_s": round(time.process_time() - cpu0, 3),
                "audio_s": lease.row.get("duration_s"),
                "llm_usage": json.dumps(res["usage"], sort_keys=True),
                "cost_usd": res["cost_usd"]}):
            raise LeaseLost("screen completion fenced out")
        return "ok"

    def screen_failed(self, lease: Lease, hb: Heartbeat, exc, t0: float) -> str:
        usage = getattr(exc, "usage", None) or llm_screen.add_usage({}, {})
        return self.api_stage_failed(
            lease, hb, exc, t0, kind=llm_screen.classify_error(exc), usage=usage,
            cost=llm_screen.cost_usd(usage, self.cfg.screen_model), status_col="screen_status")

    def api_stage_failed(self, lease: Lease, hb: Heartbeat, exc, t0: float, *, kind: str,
                         usage: dict, cost, status_col: str, next_stage: str | None = None,
                         updates: dict | None = None) -> str:
        """A screening stage (jev, name, screen) never kills an episode: fatal
        errors (key, permission, credit, model) release the lease uncharged
        and stop the worker; transient ones retry; a permanent error or the
        last transient attempt moves on with `status_col` 'failed: ...'.
        Tokens spent before the failure are billed on the stage run either way."""
        c, stage = self.cfg, lease.stage
        metrics = {"wall_s": round(time.monotonic() - t0, 3),
                   "audio_s": lease.row.get("duration_s"),
                   "llm_usage": None if usage is None else json.dumps(usage, sort_keys=True),
                   "cost_usd": cost}
        why = f"{type(exc).__name__}: {exc}"
        hb.stop()
        if kind == "fatal":
            self.queue.release(lease, f"fatal {stage} error: {why}"[:1000], metrics)
            raise WorkerFatal(f"{stage} API error on {lease.episode_id}: {why}")
        if kind == "transient" and lease.attempt < c.max_attempts:
            self.queue.fail(lease, "error", f"{stage} {kind}: {why}",
                            max_attempts=c.max_attempts, metrics=metrics)
            return "error"
        status = f"failed: {kind}: {why}"[:500]
        if not self.queue.complete(lease, next_stage=next_stage,
                                   updates={**(updates or {}), status_col: status},
                                   metrics=metrics, outcome="error", error=status):
            raise LeaseLost(f"{stage} completion fenced out")
        return "error"

    # -- clip
    def clip_sources(self, ep_id: str) -> tuple:
        """(spans clips are cut around, selection). With named spans (the
        name stage): those spans; else with a Jev result: Jev windows at
        noul >= jev_threshold. Either way plus rule candidates that overlap
        one of them or have confidence "high". With neither: every rule
        candidate. Screen reads are always included."""
        spans = named_spans(_read_json(self.dirs["names"] / f"{ep_id}.json"))
        if spans is not None:
            flagged, kind = spans, "named+rules"
            out = [{"id": s.get("span_id"), "kind": "named", "start_s": s.get("start_s"),
                    "end_s": s.get("end_s"), "jev_ids": list(s.get("jev_window_ids") or [])}
                   for s in spans]
        else:
            flagged = jev_flagged(_read_json(self.dirs["jev"] / f"{ep_id}.json"),
                                  self.cfg.jev_threshold)
            kind = "jev+rules"
            out = [{"id": w["window_id"], "kind": "jev", "start_s": w["start_s"],
                    "end_s": w["end_s"]} for w in flagged or ()]
        for c in _read_json(self.dirs["detections"] / f"{ep_id}.json") or []:
            if flagged is not None and c.get("confidence") != "high" \
                    and not any(_overlaps(c, w) for w in flagged):
                continue
            out.append({"id": c.get("candidate_id"), "kind": "candidate",
                        "start_s": c.get("start_s"), "end_s": c.get("end_s")})
        for r in (_read_json(self.dirs["screens"] / f"{ep_id}.json") or {}).get("reads") or []:
            out.append({"id": r.get("read_id"), "kind": "screen",
                        "start_s": r.get("start_s"), "end_s": r.get("end_s")})
        return out, "rules (no jev result)" if flagged is None else kind

    def publish_clip_index(self, lease: Lease, clips: list, status: str = "ok",
                           reason: str | None = None, selection: str | None = None) -> None:
        row, d = lease.row, self.dirs["clips"] / lease.episode_id
        keep = {c["file"] for c in clips} | {"index.json"}
        atomic_write_json(d / "index.json", {
            "schema_version": CLIP_INDEX_VERSION,
            "episode_id": lease.episode_id, "status": status, "reason": reason,
            "created_at": utcnow(), "audio_sha256": row.get("audio_sha256"),
            "fetch_observation_id": row.get("fetch_observation_id"),
            "selection": selection,
            "params": {"jev_threshold": self.cfg.jev_threshold, "pad_s": CLIP_PAD_S,
                       "jev_pad_s": self.cfg.jev_clip_pad_s, "merge_gap_s": CLIP_MERGE_GAP_S,
                       "max_s": CLIP_MAX_S, "jev_max_s": self.cfg.jev_clip_max_s,
                       "codec": "libmp3lame", "bitrate": CLIP_BITRATE,
                       "channels": 1,
                       "seek": "output-side -ss/-t on the decoded stream (frame-accurate: "
                               "measured within one MP3 frame, ~26 ms, after a DAI splice)"},
            "clips": clips})
        for p in d.iterdir():  # only after the new index is durable
            if p.name not in keep:
                p.unlink(missing_ok=True)

    def finish_clip(self, lease: Lease, audio, metrics: dict) -> str:
        """Clips are durable: advance to done, then delete the audio."""
        keep = self.cfg.keep_audio and audio and os.path.exists(audio)
        if not self.queue.complete(lease, updates={} if keep else {"audio_path": None},
                                   metrics=metrics):
            raise LeaseLost("clip completion fenced out")
        if audio and not keep:
            self.delete_audio_if_unreferenced(audio)
        return "ok"

    def do_clip(self, lease: Lease, hb: Heartbeat) -> str:
        c, row, ep_id = self.cfg, lease.row, lease.episode_id
        t0 = time.monotonic()
        audio = row.get("audio_path")
        d = self.dirs["clips"] / ep_id
        metrics = {"audio_s": row.get("duration_s")}
        if not audio or not os.path.exists(audio):
            # Audio deleted by an older run or gone with another host: keep a
            # previously published clip set, else record why there is none.
            prior = _read_json(d / "index.json")
            if not (isinstance(prior, dict) and prior.get("status") == "ok"):
                self.publish_clip_index(lease, [], "skipped", "audio_missing")
            metrics["wall_s"] = round(time.monotonic() - t0, 3)
            return self.finish_clip(lease, audio, metrics)
        sources, selection = self.clip_sources(ep_id)
        windows = clip_windows(sources, row.get("duration_s"), jev_pad_s=c.jev_clip_pad_s,
                               jev_max_s=c.jev_clip_max_s)
        d.mkdir(parents=True, exist_ok=True)
        for p in d.glob(".*.tmp"):
            p.unlink(missing_ok=True)
        outputs = []
        for w in windows:
            w["start_ms"], w["end_ms"] = round(w["start_s"] * 1000), round(w["end_s"] * 1000)
            w["file"] = f"{w['start_ms']}-{w['end_ms']}.mp3"
            outputs.append((w, d / f".{w['file']}.{lease.token}.tmp"))
        log_path = self.dirs["work"] / f"{ep_id}.{lease.token}.clip.log"
        try:
            if outputs:
                res = supervise(
                    clip_command(self.ffmpeg, audio, [(w["start_s"], w["end_s"] - w["start_s"], p)
                                                      for w, p in outputs]),
                    mem_cap_mb=c.mem_cap_mb, timeout_s=self.clip_timeout(row.get("duration_s")),
                    poll_s=c.poll_s, log_path=log_path,
                    abort=lambda: "lease_lost" if hb.lost.is_set()
                    else "shutdown" if self.stop.is_set() else None)
                metrics.update({k: res[k] for k in ("wall_s", "cpu_s", "peak_rss_mb")})
                if res["outcome"] == "lease_lost":
                    raise LeaseLost("lease lost while clipping")
                if res["outcome"] == "shutdown":
                    hb.stop()
                    self.queue.release(lease, "worker_shutdown", metrics)
                    return "released"
                missing = [p.name for _, p in outputs if not p.exists()]
                if res["outcome"] != "ok" or missing:
                    why = (f"{res['outcome']} rc={res['returncode']} "
                           f"{res['log_tail'][-300:]}" if res["outcome"] != "ok"
                           else f"ffmpeg wrote no {missing[0]}")
                    hb.stop()
                    if lease.attempt < c.max_attempts:
                        self.queue.fail(lease, "error" if res["outcome"] == "ok"
                                        else res["outcome"], why,
                                        max_attempts=c.max_attempts, metrics=metrics)
                        return "error"
                    # Out of attempts: keep transcript + detections; keep clips
                    # from an earlier good run, else record why there are none.
                    prior = _read_json(d / "index.json")
                    if not (isinstance(prior, dict) and prior.get("status") == "ok"):
                        self.publish_clip_index(lease, [], "skipped",
                                                f"clip_failed: {why}"[:500])
                    metrics["wall_s"] = round(time.monotonic() - t0, 3)
                    return self.finish_clip(lease, audio, metrics)
            clips = []
            for w, tmp in outputs:
                with open(tmp, "rb") as fh:
                    os.fsync(fh.fileno())
                    digest = hashlib.sha256(fh.read()).hexdigest()
                clips.append({
                    "file": w["file"], "start_s": w["start_s"], "end_s": w["end_s"],
                    "start_ms": w["start_ms"], "end_ms": w["end_ms"],
                    "duration_s": _clip_seconds(tmp, self.ffmpeg,
                                                w["end_s"] - w["start_s"]),
                    "bytes": tmp.stat().st_size, "sha256": digest,
                    "sources": w["sources"], "jev_window_ids": w["jev_window_ids"],
                    "candidate_ids": w["candidate_ids"], "screen_read_ids": w["screen_read_ids"],
                    "named_span_ids": w["named_span_ids"], "pad_s": w["pad_s"]})
            hb.stop()
            if not self.queue.heartbeat(lease, c.lease_s):
                raise LeaseLost("lease lost before publishing clips")
            for w, tmp in outputs:
                os.replace(tmp, d / w["file"])
            self.publish_clip_index(lease, clips, selection=selection)
            metrics["wall_s"] = round(time.monotonic() - t0, 3)
            return self.finish_clip(lease, audio, metrics)
        finally:
            for _, tmp in outputs:
                tmp.unlink(missing_ok=True)
            log_path.unlink(missing_ok=True)


def diarize_command(base: list, row: dict, episode_id: str, out, *, device: str,
                    window_s: float, overlap_s: float, threads: int, ffmpeg: str) -> list:
    return [*base, "--audio", row["audio_path"], "--out", str(out), "--episode-id", episode_id,
            "--device", device, "--revision", dz.MODEL_REVISION, "--window-s", str(window_s),
            "--overlap-s", str(overlap_s), "--threads", str(threads), "--ffmpeg", ffmpeg,
            "--audio-sha256", row.get("audio_sha256") or ""]


def diarize_timeout(duration_s) -> float:
    return DIARIZE_TIMEOUT_BASE_S + DIARIZE_TIMEOUT_FACTOR * float(duration_s or 3600)


class DiarizeUnavailable(RuntimeError):
    """The diarize python cannot run the model (missing deps, no device)."""


def diarize_preflight(cmd: list, device: str, mem_cap_mb: float = DIARIZE_MEM_CAP_MB,
                      timeout_s: float = 600.0) -> dict:
    """Run `<cmd> --preflight --device D` once under the child's RSS cap;
    DiarizeUnavailable unless it loads the pinned weights from the local
    Hugging Face cache on `device` and diarizes a few seconds of audio."""
    fd, log_path = tempfile.mkstemp(prefix="diarize-preflight-", suffix=".log")
    os.close(fd)
    try:
        res = supervise([*cmd, "--preflight", "--device", device], mem_cap_mb=mem_cap_mb,
                        timeout_s=timeout_s, poll_s=0.2, log_path=log_path)
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise DiarizeUnavailable(f"diarize preflight failed: {exc}") from None
    finally:
        Path(log_path).unlink(missing_ok=True)
    info = {}
    for line in reversed(text.strip().splitlines()):
        try:
            info = json.loads(line)
            break
        except ValueError:
            continue
    if not isinstance(info, dict) or res["outcome"] != "ok" or not info.get("ok"):
        why = (info.get("error") if isinstance(info, dict) else None) \
            or f"{res['outcome']}: rc={res['returncode']} peak_rss_mb={res['peak_rss_mb']} " \
               f"{text[-300:]}"
        raise DiarizeUnavailable(
            f"diarize preflight failed ({cmd[0]}): {why}; install requirements-diarize.txt "
            f"into that python (or pass --diarize-python) and cache the weights: hf download "
            f"{dz.MODEL_ID} --revision {dz.MODEL_REVISION}")
    return info


def _decoded_seconds(path):
    try:
        return round(acq.probe_audio_duration(path), 3)
    except Exception:  # an empty cut past the end still publishes, unmeasured
        return None


def _clip_seconds(path, ffmpeg: str, window_s: float) -> float:
    """A cut clip's decoded seconds: PyAV's decode probe, else an ffmpeg
    decode (a python without PyAV), else the window length."""
    measured = _decoded_seconds(path)
    if measured is None:
        try:
            stats: dict = {}
            for _ in stt.iter_windows(path, max(window_s, 1.0) + 60.0, 0.0, ffmpeg, stats):
                pass
            measured = round(stats["decoded_s"], 3) if "decoded_s" in stats else None
        except Exception:
            measured = None
    return measured if measured is not None else round(window_s, 3)


def read_clip_index(path):
    """A clips/<ep>/index.json in the current shape (CLIP_INDEX_DOC), whatever
    shape it was written in; None when unreadable. Missing id lists become
    [], missing sources are taken from the ids present (the oldest indexes
    predate Jev, so "rule" / "screen"), a missing pad_s from params, a null
    duration_s from end_s - start_s."""
    doc = _read_json(path)
    if not isinstance(doc, dict):
        return None
    doc = dict(doc)
    doc.setdefault("schema_version", 1)
    doc.setdefault("selection", None)
    pad = (doc.get("params") or {}).get("pad_s", CLIP_PAD_S)
    clips = []
    for c in doc.get("clips") or ():
        if not isinstance(c, dict) or _num(c.get("start_s")) is None \
                or _num(c.get("end_s")) is None or not c.get("file"):
            continue
        c = dict(c)
        for k in CLIP_LIST_FIELDS:
            if not isinstance(c.get(k), list):
                c[k] = [] if k != "sources" else sorted(
                    label for label, ids in (("rule", "candidate_ids"),
                                             ("screen", "screen_read_ids"),
                                             ("jev", "jev_window_ids"),
                                             ("named", "named_span_ids")) if c.get(ids))
        c.setdefault("pad_s", pad)
        if _num(c.get("duration_s")) is None:
            c["duration_s"] = round(float(c["end_s"]) - float(c["start_s"]), 3)
        clips.append(c)
    doc["clips"] = clips
    return doc


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _num(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value) else None


def _overlaps(a: dict, b: dict) -> bool:
    a0, a1, b0, b1 = (_num(x) for x in (a.get("start_s"), a.get("end_s"),
                                        b.get("start_s"), b.get("end_s")))
    return None not in (a0, a1, b0, b1) and a0 < b1 and a1 > b0


def named_spans(doc):
    """The spans of a names/<ep>.json doc; None when `doc` is not one."""
    if not isinstance(doc, dict) or not isinstance(doc.get("spans"), list):
        return None
    return [s for s in doc["spans"] if isinstance(s, dict)]


def jev_flagged(doc, threshold: float = jev_screen.DEFAULT_THRESHOLD):
    """Windows of a jev/<ep>.json doc at noul >= threshold; None when `doc`
    is not a Jev result."""
    if not isinstance(doc, dict) or not isinstance(doc.get("windows"), list):
        return None
    return [w for w in doc["windows"] if isinstance(w, dict)
            and (_num(w.get("noul")) or 0.0) >= threshold]


SOURCE_LABELS = {"candidate": "rule", "jev": "jev", "screen": "screen", "named": "named"}
JEV_KINDS = ("jev", "named")


def clip_windows(sources: list, duration_s: float | None = None, pad_s: float = CLIP_PAD_S,
                 merge_gap_s: float = CLIP_MERGE_GAP_S, max_s: float = CLIP_MAX_S,
                 jev_pad_s: float | None = None, jev_max_s: float | None = None) -> list:
    """Clip windows around `sources` ({id, kind: candidate|jev|screen|named,
    start_s, end_s}; named spans also carry their jev_ids): each span padded
    by `pad_s` (`jev_pad_s` for jev windows and named spans; clamped to [0,
    duration]), padded windows closer than `merge_gap_s` merged, merged
    windows longer than `max_s` (`jev_max_s` when one holds a jev/named
    span) split into equal parts. Each window lists the source ids whose
    padded span overlaps it, which kinds of source those are, and the
    largest pad among them."""
    jev_pad_s = pad_s if jev_pad_s is None else jev_pad_s
    jev_max_s = max_s if jev_max_s is None else jev_max_s
    items = []
    for s in sources:
        try:
            a, b = float(s["start_s"]), float(s["end_s"])
        except (TypeError, ValueError, KeyError):
            continue
        if not (math.isfinite(a) and math.isfinite(b)) or b < a:
            continue
        pad = jev_pad_s if s.get("kind") in JEV_KINDS else pad_s
        lo, hi = max(0.0, a - pad), b + pad
        if duration_s:
            hi = min(hi, float(duration_s))
        if hi > lo:
            items.append((lo, hi, s, pad))
    items.sort(key=lambda x: (x[0], x[1]))
    merged = []
    for lo, hi, s, pad in items:
        if merged and lo - merged[-1][1] < merge_gap_s:
            merged[-1][1] = max(merged[-1][1], hi)
            merged[-1][2].append((lo, hi, s, pad))
        else:
            merged.append([lo, hi, [(lo, hi, s, pad)]])
    out = []
    for lo, hi, members in merged:
        cap = jev_max_s if any(s.get("kind") in JEV_KINDS for _, _, s, _ in members) else max_s
        n = max(1, math.ceil((hi - lo) / cap - 1e-9))
        for i in range(n):
            a = lo + (hi - lo) * i / n
            b = hi if i == n - 1 else lo + (hi - lo) * (i + 1) / n
            hit = [(s, pad) for m0, m1, s, pad in members if m0 < b and m1 > a]
            pad_used = max(pad for _, pad in hit)
            hit = [s for s, _ in hit]
            out.append({"start_s": round(a, 3), "end_s": round(b, 3), "pad_s": pad_used,
                        "sources": sorted({SOURCE_LABELS.get(s.get("kind"), "rule")
                                           for s in hit}),
                        "jev_window_ids": list(dict.fromkeys(
                            i for s in hit for i in (
                                [s["id"]] if s.get("kind") == "jev" else s.get("jev_ids") or []))),
                        "candidate_ids": list(dict.fromkeys(
                            s["id"] for s in hit
                            if s.get("kind") not in ("screen", "jev", "named"))),
                        "screen_read_ids": list(dict.fromkeys(
                            s["id"] for s in hit if s.get("kind") == "screen")),
                        "named_span_ids": list(dict.fromkeys(
                            s["id"] for s in hit if s.get("kind") == "named"))})
    return out


def clip_command(ffmpeg: str, audio, outputs: list) -> list:
    """One decode pass, one mono MP3 per (start_s, length_s, path). -ss/-t
    are OUTPUT options: ffmpeg decodes from the start and cuts the decoded
    stream, so offsets follow the same decoded timeline transcription uses.
    Frame-accurate, not sample-accurate: after a DAI splice the measured
    offset is a constant ~26 ms (one MP3 frame). Input-side -ss seeks by
    bitrate/TOC estimate and has landed tens of seconds off on DAI files."""
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-y", "-i", str(audio)]
    for start, length, path in outputs:
        cmd += ["-map", "0:a:0", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-ac", "1",
                "-c:a", "libmp3lame", "-b:a", CLIP_BITRATE, "-map_metadata", "-1",
                "-f", "mp3", str(path)]
    return cmd


def requeue_dead(corpus, episodes=None) -> dict:
    """Send dead episodes (all, or those of `episodes`) back to fetch as
    fresh rows, e.g. after the code that killed them is fixed. A dead
    episode holds no audio, so it is fetched again. The earlier attempts
    stay in stage_runs and fetch_observations; last_error keeps the cause.
    A requeued row takes its show's current coverage tier (set-tier leaves
    dead rows alone). Only rows at stage dead move; a second call changes
    nothing."""
    started = utcnow()
    con = connect(corpus)
    try:
        ensure_schema(con)
        rep = {"requeued": [], "not_dead": [], "unknown": []}
        with _Tx(con):
            stage = dict(con.execute("SELECT episode_id, stage FROM episodes").fetchall())
            ids = sorted(e for e, s in stage.items() if s == "dead") if episodes is None \
                else list(dict.fromkeys(episodes))
            for ep_id in ids:
                if ep_id not in stage:
                    rep["unknown"].append(ep_id)
                    continue
                if stage[ep_id] != "dead":
                    rep["not_dead"].append(ep_id)
                    continue
                con.execute(
                    "UPDATE episodes SET stage='fetch', state='ready', attempts=0,"
                    " not_before=0, lease_owner=NULL, lease_token=NULL, lease_until=NULL,"
                    " lease_run_id=NULL, audio_path=NULL, dead_stage=NULL, screen_status=NULL,"
                    " jev_status=NULL, name_status=NULL, diarize_status=NULL,"
                    " gap_fill_status=NULL, repair_requeued_at=NULL,"
                    " tier=COALESCE((SELECT tier FROM shows WHERE feed_url=episodes.feed_url),"
                    " tier),"
                    " last_error='requeued_dead (was ' || COALESCE(dead_stage, '?') || '): '"
                    " || COALESCE(last_error, ''), updated_at=? WHERE episode_id=?"
                    " AND stage='dead'", (utcnow(), ep_id))
                rep["requeued"].append(ep_id)
            log_discovery(con, "requeue-dead", started, {"episodes": episodes}, rep)
        return rep
    finally:
        con.close()


def mark_dead(corpus, episodes, reason, dry_run=False) -> dict:
    """Queue.mark_dead on `corpus`; requeue_dead brings the rows back."""
    q = Queue(corpus)
    try:
        return q.mark_dead(episodes, reason, dry_run=dry_run)
    finally:
        q.close()


def repair_seams(corpus, dry_run: bool = False, below_version: int | None = None) -> dict:
    """De-duplicate chunk seams in published transcripts of `done` episodes
    and send them back to `detect`.

    Only stage=done rows are touched, and each write happens inside a
    BEGIN IMMEDIATE transaction that re-checks the row, so no worker can
    claim or publish the episode meanwhile (done rows are never leased).
    Idempotent per algorithm version: transcripts stitched or repaired by
    the current SEAM_REPAIR_VERSION are left alone; older ones (midpoint
    stitching, or repair v1) are repaired once more. A repaired transcript
    whose detections are older than it (crash between the file write and
    the commit) is sent back to detect again. `below_version` limits repair
    to transcripts whose seam version is below it (1 = midpoint-stitched only);
    the stale-detections requeue above still applies to every repaired
    transcript, since it is crash recovery and never rewrites text.
    A repaired episode goes back only to the stages its coverage tier runs
    (tier_next from detect): one whose tier stops at transcribe keeps its
    repaired transcript and stays done.
    """
    dirs = corpus_dirs(corpus)
    rep = {"episodes_scanned": 0, "episodes_repaired": 0, "detect_requeued": 0,
           "skipped": 0, **{k: 0 for k in stt.SEAM_COUNTS}, "dry_run": dry_run}
    con = connect(corpus)
    try:
        ensure_schema(con)
        sets = {tr: tier_stages(con, tr) for tr in TIERS}
        rows = con.execute("SELECT episode_id, tier FROM episodes WHERE stage='done'"
                           " AND state='ready' ORDER BY episode_id").fetchall()
        for ep_id, tier in rows:
            requeue_to = tier_next("detect", sets.get(tier))
            tpath = dirs["transcripts"] / f"{ep_id}.json"
            det = dirs["detections"] / f"{ep_id}.json"
            try:
                doc = json.loads(tpath.read_text(encoding="utf-8"))
                prov = doc.get("provenance") or {}
                offsets = [c["offset_s"] for c in prov.get("chunk_languages") or []]
                overlap = float(prov.get("overlap_s") or 0)
            except (OSError, ValueError, KeyError, TypeError):
                rep["skipped"] += 1
                continue
            rep["episodes_scanned"] += 1
            counts = {k: 0 for k in stt.SEAM_COUNTS}
            done_version = max(int(prov.get("seam_dedupe") or 0),
                               int((prov.get("seam_repair") or {}).get("version") or 0))
            target = stt.SEAM_REPAIR_VERSION if below_version is None else min(
                below_version, stt.SEAM_REPAIR_VERSION)
            todo = len(offsets) > 1 and done_version < target
            if todo:
                fixed, counts = stt.repair_seams(doc["segments"], offsets, overlap)
            changed = todo and fixed != doc["segments"]
            stale = "seam_repair" in prov and requeue_to != "done" and (
                not det.exists() or det.stat().st_mtime < tpath.stat().st_mtime)
            if not (changed or stale):
                continue
            if changed:
                prior = prov.get("seam_repair")
                prov["seam_repair"] = {"version": stt.SEAM_REPAIR_VERSION, **counts}
                if prior:
                    prov["seam_repair"]["previous"] = prior
                doc["segments"] = fixed
                if "words" in doc:  # stitched by the old cut; re-transcribe to rebuild
                    prov["seam_repair"]["words_dropped"] = len(doc.pop("words") or [])
            if dry_run:
                rep["episodes_repaired"] += changed
                rep["detect_requeued"] += requeue_to != "done"
                for k in stt.SEAM_COUNTS:
                    rep[k] += counts[k]
                continue
            with _Tx(con):
                row = con.execute("SELECT stage, state, duration_s, tier FROM episodes"
                                  " WHERE episode_id=?", (ep_id,)).fetchone()
                if (row["stage"], row["state"], row["tier"]) != ("done", "ready", tier):
                    rep["skipped"] += 1
                    continue
                if changed:
                    try:
                        validate_transcript(doc, row["duration_s"])
                    except TranscriptInvalid:
                        rep["skipped"] += 1
                        continue
                    atomic_write_json(tpath, doc)
                if requeue_to != "done":
                    con.execute("UPDATE episodes SET stage=?, state='ready', attempts=0,"
                                " not_before=0, last_error='seam_repair', updated_at=?"
                                " WHERE episode_id=?", (requeue_to, utcnow(), ep_id))
            rep["episodes_repaired"] += changed
            rep["detect_requeued"] += requeue_to != "done"
            for k in stt.SEAM_COUNTS:
                rep[k] += counts[k]
    finally:
        con.close()
    return rep


def gc_corpus(corpus, con, min_age_s: float = GC_MIN_AGE_S) -> int:
    """Remove old unreferenced audio, stale .part files and orphaned child
    output. Young files are left alone: they may belong to a live peer."""
    removed = 0
    cutoff = time.time() - min_age_s
    referenced = {r[0] for r in con.execute(
        "SELECT audio_path FROM episodes WHERE audio_path IS NOT NULL")}
    dirs = corpus_dirs(corpus)
    for d in (dirs["audio-tmp"], dirs["work"]):
        for p in d.iterdir() if d.exists() else ():
            try:
                if p.is_file() and str(p) not in referenced and p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                pass
    return removed


# ---------------------------------------------------------------- status

def _pct(values, q):
    if not values:
        return None
    vals = sorted(values)
    return round(vals[min(len(vals) - 1, int(q * (len(vals) - 1) + 0.5))], 3)


def _dir_bytes(path: Path) -> int:
    total = 0
    for p in path.rglob("*") if path.exists() else ():
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _length_verified(verification):
    try:
        value = json.loads(verification).get("length_verified")
    except (TypeError, ValueError, AttributeError):
        return None
    return value if isinstance(value, bool) else None


def clip_totals(corpus) -> dict:
    """From the published clips/<ep>/index.json files (bytes from disk)."""
    out = {"episodes_indexed": 0, "episodes_with_clips": 0, "clips": 0, "bytes": 0,
           "skipped": {}}
    root = corpus_dirs(corpus)["clips"]
    for idx_path in sorted(root.glob("*/index.json")) if root.exists() else ():
        idx = _read_json(idx_path)
        if not isinstance(idx, dict):
            continue
        out["episodes_indexed"] += 1
        if idx.get("status") != "ok":
            reason = (idx.get("reason") or "unknown").split(":")[0]
            out["skipped"][reason] = out["skipped"].get(reason, 0) + 1
            continue
        clips = idx.get("clips") or []
        out["episodes_with_clips"] += bool(clips)
        out["clips"] += len(clips)
        for c in clips:
            try:
                out["bytes"] += (idx_path.parent / c["file"]).stat().st_size
            except (OSError, KeyError):
                pass
    return out


def screen_totals(corpus, con) -> dict:
    """Reads from screens/*.json; tokens and cost from every screen stage
    run, failed ones included (their partial usage is billed too)."""
    out = {"episodes_screened": 0, "reads": 0, "failed_windows": 0,
           **{k: 0 for k in llm_screen.USAGE_KEYS}, "total_tokens": 0, "cost_usd": 0.0,
           "unpriced_runs": 0, "status": {}}
    root = corpus_dirs(corpus)["screens"]
    for path in sorted(root.glob("*.json")) if root.exists() else ():
        doc = _read_json(path)
        if not isinstance(doc, dict):
            continue
        out["episodes_screened"] += 1
        out["reads"] += len(doc.get("reads") or [])
        out["failed_windows"] += len(doc.get("failed_windows") or [])
    for usage, cost in con.execute("SELECT llm_usage, cost_usd FROM stage_runs"
                                   " WHERE stage='screen' AND llm_usage IS NOT NULL"):
        u = _read_json_text(usage) or {}
        for k in llm_screen.USAGE_KEYS:
            out[k] += int(u.get(k) or 0)
        if cost is None:
            out["unpriced_runs"] += any(u.get(k) for k in llm_screen.USAGE_KEYS)
        else:
            out["cost_usd"] += cost
    for st, n in con.execute("SELECT screen_status, COUNT(*) FROM episodes"
                             " WHERE screen_status IS NOT NULL GROUP BY 1"):
        key = "failed" if st.startswith("failed") else st.split(" (")[0]
        out["status"][key] = out["status"].get(key, 0) + n
    out["total_tokens"] = sum(out[k] for k in llm_screen.USAGE_KEYS)
    out["cost_usd"] = round(out["cost_usd"], 6)
    return out


def jev_totals(corpus, con, threshold: float = jev_screen.DEFAULT_THRESHOLD) -> dict:
    """Windows from jev/*.json; tokens and cost from every jev stage run
    (worker and backfill, failed ones included)."""
    out = {"threshold": threshold, "episodes_screened": 0, "windows": 0, "windows_flagged": 0,
           "screened_s": 0.0, "flagged_s": 0.0, "runtime_flagged_share": None,
           "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "status": {}}
    root = corpus_dirs(corpus)["jev"]
    for path in sorted(root.glob("*.json")) if root.exists() else ():
        doc = _read_json(path)
        flagged = jev_flagged(doc, threshold)
        if flagged is None:
            continue
        out["episodes_screened"] += 1
        for w in doc["windows"]:
            span = (_num(w.get("end_s")) or 0.0) - (_num(w.get("start_s")) or 0.0)
            out["windows"] += 1
            out["screened_s"] += span
            if w in flagged:
                out["windows_flagged"] += 1
                out["flagged_s"] += span
    for usage, cost in con.execute("SELECT llm_usage, cost_usd FROM stage_runs"
                                   " WHERE stage='jev' AND llm_usage IS NOT NULL"):
        u = _read_json_text(usage) or {}
        out["input_tokens"] += int(u.get("input_tokens") or 0)
        out["output_tokens"] += int(u.get("output_tokens") or 0)
        out["cost_usd"] += cost or 0.0
    for st, n in con.execute("SELECT jev_status, COUNT(*) FROM episodes"
                             " WHERE jev_status IS NOT NULL GROUP BY 1"):
        key = "failed" if st.startswith("failed") else st.split(" (")[0]
        out["status"][key] = out["status"].get(key, 0) + n
    if out["screened_s"]:
        out["runtime_flagged_share"] = out["flagged_s"] / out["screened_s"]
    out["screened_s"], out["flagged_s"] = round(out["screened_s"], 1), round(out["flagged_s"], 1)
    out["cost_usd"] = round(out["cost_usd"], 8)  # ~$0.0008/episode
    return out


def name_totals(corpus, con) -> dict:
    """Spans from names/*.json; episode name_status counts."""
    out = {"episodes_named": 0, "spans": 0, "spans_named": 0, "spans_needs_review": 0,
           "pipelines": {}, "status": {}}
    root = corpus_dirs(corpus)["names"]
    for path in sorted(root.glob("*.json")) if root.exists() else ():
        doc = _read_json(path)
        spans = named_spans(doc)
        if spans is None:
            continue
        out["episodes_named"] += 1
        p = doc.get("pipeline") or "?"
        out["pipelines"][p] = out["pipelines"].get(p, 0) + 1
        out["spans"] += len(spans)
        out["spans_named"] += sum(1 for sp in spans if sp.get("brand"))
        out["spans_needs_review"] += sum(1 for sp in spans if sp.get("needs_review"))
    for st, n in con.execute("SELECT name_status, COUNT(*) FROM episodes"
                             " WHERE name_status IS NOT NULL GROUP BY 1"):
        key = "failed" if st.startswith("failed") else st.split(" (")[0]
        out["status"][key] = out["status"].get(key, 0) + n
    return out


def diarize_totals(corpus, con) -> dict:
    """Speaker docs from speakers/*.json; episode diarize_status counts;
    child peak RSS and speed from ok diarize stage runs."""
    out = {"episodes_diarized": 0, "audio_s": 0.0, "speakers_median": None, "status": {},
           "peak_rss_mb_max": None, "realtime_factor": None}
    root = corpus_dirs(corpus)["speakers"]
    counts = []
    for path in sorted(root.glob("*.json")) if root.exists() else ():
        doc = _read_json(path)
        if not isinstance(doc, dict) or not isinstance(doc.get("turns"), list):
            continue
        out["episodes_diarized"] += 1
        out["audio_s"] += _num(doc.get("decoded_duration_s")) or 0.0
        counts.append(len(doc.get("speakers") or {}))
    out["speakers_median"] = median(counts) if counts else None
    runs = con.execute("SELECT wall_s, audio_s, peak_rss_mb FROM stage_runs WHERE"
                       " stage='diarize' AND outcome='ok' AND peak_rss_mb IS NOT NULL").fetchall()
    if runs:
        out["peak_rss_mb_max"] = max(r[2] for r in runs)
        wall = sum(r[0] or 0 for r in runs)
        out["realtime_factor"] = round(sum(r[1] or 0 for r in runs) / wall, 1) if wall else None
    for st, n in con.execute("SELECT diarize_status, COUNT(*) FROM episodes"
                             " WHERE diarize_status IS NOT NULL GROUP BY 1"):
        key = "failed" if st.startswith("failed") else st.split(" (")[0]
        out["status"][key] = out["status"].get(key, 0) + n
    out["audio_s"] = round(out["audio_s"], 1)
    return out


def refine_kept_first_pass(clip) -> bool:
    """A refined clip whose refine guard kept the first-pass text."""
    return isinstance(clip, dict) and \
        (clip.get("refine_guard") or {}).get("decision") == "kept_first_pass"


def refined_segments(segments: list, refined_doc) -> list:
    """One episode timeline: inside each refined clip's range the refined
    segments, elsewhere the original ones (an original segment is replaced
    when its midpoint falls inside a refined range). A clip whose refine
    guard kept the first pass is skipped: the live transcript shows through
    (it may have been repaired since the clip was refined)."""
    clips = [c for c in (refined_doc or {}).get("clips") or ()
             if isinstance(c, dict) and isinstance(c.get("segments"), list)
             and not refine_kept_first_pass(c)]
    if not clips:
        return list(segments or ())
    ranges = [(c["start_s"], c["end_s"]) for c in clips]
    keep = [s for s in segments or ()
            if not any(a <= (s["start"] + s["end"]) / 2 < b for a, b in ranges)]
    return sorted(keep + [s for c in clips for s in c["segments"]],
                  key=lambda s: (s["start"], s["end"]))


def refine_totals(corpus) -> dict:
    """From refined/*.json (re-transcribed clips) and the names/*.json docs
    the refine pass rewrote."""
    out = {"episodes_refined": 0, "clips": 0, "clips_failed": 0, "audio_s": 0.0,
           "wall_s": 0.0, "models": {}, "episodes_renamed": 0, "spans": 0,
           "spans_text_refined": 0, "names_changed": 0, "names_unchanged": 0, "names_new": 0,
           "accepted_refined": 0, "kept_previous": 0, "confidence_up": 0,
           "confidence_down": 0}
    dirs = corpus_dirs(corpus)
    for path in sorted(dirs["refined"].glob("*.json")) if dirs["refined"].exists() else ():
        doc = _read_json(path)
        if not isinstance(doc, dict) or not isinstance(doc.get("clips"), list):
            continue
        out["episodes_refined"] += 1
        out["clips"] += len(doc["clips"])
        out["clips_failed"] += len(doc.get("failed") or [])
        m = doc.get("model") or "?"
        out["models"][m] = out["models"].get(m, 0) + 1
        for c in doc["clips"]:
            out["audio_s"] += (_num(c.get("end_s")) or 0.0) - (_num(c.get("start_s")) or 0.0)
            out["wall_s"] += _num(c.get("wall_s")) or 0.0
    for path in sorted(dirs["names"].glob("*.json")) if dirs["names"].exists() else ():
        doc = _read_json(path)
        spans = named_spans(doc)
        if spans is None or not doc.get("refined"):
            continue
        out["episodes_renamed"] += 1
        out["spans"] += len(spans)
        for k, v in (doc.get("changes") or {}).items():
            if k in out and isinstance(v, int):
                out[k] += v
        out["spans_text_refined"] += sum(1 for sp in spans if sp.get("text_source") == "refined")
    out["audio_s"], out["wall_s"] = round(out["audio_s"], 1), round(out["wall_s"], 1)
    return out


def _read_json_text(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def status(corpus) -> dict:
    con = connect(corpus)
    try:
        ensure_schema(con)
        counts = {}
        for r in con.execute("SELECT stage, state, COUNT(*) n FROM episodes GROUP BY 1, 2"):
            counts.setdefault(r["stage"], {})[r["state"]] = r["n"]
        tier_stage = tier_stage_lists(con)
        tiers = {}
        for r in con.execute("SELECT tier, COUNT(*) n FROM shows GROUP BY 1"):
            tiers[r["tier"]] = {"shows": r["n"], "episodes": {}, "stages": tier_stage.get(r["tier"])}
        for r in con.execute("SELECT tier, stage, COUNT(*) n FROM episodes GROUP BY 1, 2"):
            tiers.setdefault(r["tier"], {"shows": 0, "episodes": {},
                                         "stages": tier_stage.get(r["tier"])}
                             )["episodes"][r["stage"]] = r["n"]
        dead = [dict(r) for r in con.execute(
            "SELECT episode_id, show_title, ep_title, attempts, last_error FROM episodes"
            " WHERE stage='dead' ORDER BY updated_at")]
        outcomes = {}
        for r in con.execute("SELECT stage, COALESCE(outcome, 'running') o, COUNT(*) n"
                             " FROM stage_runs GROUP BY 1, 2"):
            outcomes.setdefault(r["stage"], {})[r["o"]] = r["n"]
        per_stage = {}
        for stage in STAGES:
            walls = [r[0] for r in con.execute(
                "SELECT wall_s FROM stage_runs WHERE stage=? AND outcome='ok'"
                " AND wall_s IS NOT NULL", (stage,))]
            per_stage[stage] = {"ok_runs": len(walls), "wall_s_median": _pct(walls, 0.5),
                                "wall_s_p90": _pct(walls, 0.9)}
        tr = con.execute("SELECT wall_s, cpu_s, audio_s, peak_rss_mb FROM stage_runs WHERE"
                         " stage='transcribe' AND outcome='ok' AND audio_s > 0").fetchall()
        audio = sum(r["audio_s"] for r in tr)
        wall = sum(r["wall_s"] or 0 for r in tr)
        cpu = sum(r["cpu_s"] or 0 for r in tr)
        rss = [r["peak_rss_mb"] for r in con.execute(
            "SELECT peak_rss_mb FROM stage_runs WHERE stage='transcribe'"
            " AND peak_rss_mb IS NOT NULL")]
        shows = {r["last_status"] or "never": r["n"] for r in con.execute(
            "SELECT last_status, COUNT(*) n FROM shows GROUP BY 1")}
        show_errors = [dict(r) for r in con.execute(
            "SELECT title, feed_url, last_error FROM shows WHERE last_status='error'")]
        n_obs = con.execute("SELECT COUNT(*) FROM fetch_observations").fetchone()[0]
        last = con.execute("SELECT command, finished_at, report FROM discovery_log"
                           " ORDER BY id DESC LIMIT 1").fetchone()
        discovery = {"runs": con.execute("SELECT COUNT(*) FROM discovery_log").fetchone()[0],
                     "last": dict(last, report=json.loads(last["report"] or "null"))
                     if last else None}
        screen = screen_totals(corpus, con)
        jev = jev_totals(corpus, con)
        names = name_totals(corpus, con)
        diarize = diarize_totals(corpus, con)
        refine = refine_totals(corpus)
        gap_fill = gap_fill_totals(con)
        workers = [dict(r) for r in con.execute(
            "SELECT worker_id, host, stages, last_seen FROM workers ORDER BY worker_id")]
        verif = {"verified": 0, "unverified": 0, "unknown": 0}
        for (v,) in con.execute("SELECT o.verification FROM episodes e JOIN fetch_observations o"
                                " ON o.id = e.fetch_observation_id"):
            lv = _length_verified(v)
            verif["unknown" if lv is None else "verified" if lv else "unverified"] += 1
    finally:
        con.close()
    dirs = corpus_dirs(corpus)
    return {
        "episodes": counts, "total_episodes": sum(sum(v.values()) for v in counts.values()),
        "shows": shows, "show_errors": show_errors, "fetch_observations": n_obs,
        "discovery": discovery,
        "fetch_verification": verif,
        "dead": dead, "outcomes": outcomes, "per_stage": per_stage, "tiers": tiers,
        "transcription": {
            "episodes": len(tr), "audio_s": round(audio, 1), "wall_s": round(wall, 1),
            "realtime_factor": round(audio / wall, 2) if wall else None,
            "realtime_factor_median": _pct([r["audio_s"] / r["wall_s"] for r in tr
                                            if r["wall_s"]], 0.5),
            "cpu_s_per_audio_s": round(cpu / audio, 4) if audio else None,
            "peak_rss_mb": {"min": _pct(rss, 0), "median": _pct(rss, 0.5),
                            "p90": _pct(rss, 0.9), "max": _pct(rss, 1.0)}},
        "disk_bytes": {name: _dir_bytes(dirs[name])
                       for name in ("audio-tmp", "transcripts", "detections", "jev",
                                    "names", "speakers", "screens", "clips", "refined",
                                    "work")},
        "clips": clip_totals(corpus),
        "jev": jev,
        "names": names,
        "diarize": diarize,
        "refine": refine,
        "gap_fill": gap_fill,
        "screen": screen,
        "workers": workers,
    }


def format_status(s: dict) -> str:
    lines = [f"episodes: {s['total_episodes']}  shows: {s['shows']}  "
             f"fetch_observations: {s['fetch_observations']}"]
    d = s.get("discovery") or {}
    if d.get("last"):
        rep = d["last"]["report"] or {}
        lines.append(f"discovery log: {d['runs']} runs; last {d['last']['command']} at "
                     f"{d['last']['finished_at']}"
                     + (f" (+{rep['episodes_new']} episodes)" if "episodes_new" in rep else ""))
    v = s["fetch_verification"]
    lines.append(f"fetched audio: length verified: {v['verified']}  unverified: "
                 f"{v['unverified']}  unknown: {v['unknown']}")
    for stage in (*STAGES, "done", "dead"):
        if stage in s["episodes"]:
            lines.append(f"  {stage:<10} " + "  ".join(
                f"{k}={v}" for k, v in sorted(s["episodes"][stage].items())))
    for tier, t in sorted((s.get("tiers") or {}).items()):
        eps = t["episodes"]
        lines.append(f"tier {tier}: {t['shows']} shows, {sum(eps.values())} episodes ("
                     + " ".join(f"{st}={eps[st]}" for st in (*STAGES, "done", "dead")
                                if st in eps)
                     + f"); stages {','.join(t['stages']) if t['stages'] else 'all'}")
    lines.append("stage runs:")
    for stage, outs in s["outcomes"].items():
        ps = s["per_stage"].get(stage, {})
        lines.append(f"  {stage:<10} " + " ".join(f"{k}={v}" for k, v in sorted(outs.items()))
                     + f"  wall_s median={ps.get('wall_s_median')} p90={ps.get('wall_s_p90')}")
    t = s["transcription"]
    lines.append(f"transcription: {t['episodes']} eps, {t['audio_s']} audio-s in "
                 f"{t['wall_s']} wall-s, realtime x{t['realtime_factor']} "
                 f"(median x{t['realtime_factor_median']}), "
                 f"cpu-s/audio-s {t['cpu_s_per_audio_s']}, peak RSS MB {t['peak_rss_mb']}")
    cl = s["clips"]
    lines.append(f"clips: {cl['clips']} clips, {cl['bytes']} bytes, in "
                 f"{cl['episodes_with_clips']} episodes ({cl['episodes_indexed']} indexed; "
                 f"skipped: {cl['skipped'] or 0})")
    j = s["jev"]
    share = j["runtime_flagged_share"]
    lines.append(f"jev: {j['episodes_screened']} episodes screened, {j['windows_flagged']}/"
                 f"{j['windows']} windows flagged at noul>={j['threshold']} "
                 f"({'-' if share is None else f'{share:.1%}'} of runtime), "
                 f"{j['input_tokens']} input tokens, cost_usd {j['cost_usd']}; "
                 f"episode jev_status {j['status'] or '-'}")
    n = s["names"]
    lines.append(f"names: {n['episodes_named']} episodes, {n['spans']} ad spans "
                 f"({n['spans_named']} named, {n['spans_needs_review']} need review); "
                 f"pipelines {n['pipelines'] or '-'}; episode name_status {n['status'] or '-'}")
    d = s["diarize"]
    lines.append(f"diarize: {d['episodes_diarized']} episodes, {d['audio_s']} audio-s, "
                 f"median {d['speakers_median']} speakers, realtime x{d['realtime_factor']}, "
                 f"child peak RSS MB {d['peak_rss_mb_max']}; episode diarize_status "
                 f"{d['status'] or '-'}")
    r = s["refine"]
    lines.append(f"refine: {r['episodes_refined']} episodes, {r['clips']} clips "
                 f"({r['clips_failed']} failed), {r['audio_s']} audio-s in {r['wall_s']} "
                 f"stt-s, models {r['models'] or '-'}; {r['episodes_renamed']} episodes "
                 f"renamed, {r['spans']} spans ({r['spans_text_refined']} on refined text): "
                 f"{r['names_changed']} names changed, {r['names_unchanged']} unchanged, "
                 f"{r['names_new']} new; accepted {r['accepted_refined']}, kept previous "
                 f"{r['kept_previous']}; confidence up {r['confidence_up']}, "
                 f"down {r['confidence_down']}")
    g = s.get("gap_fill") or {}
    if g.get("episodes"):
        lines.append(f"gap fill: {g['episodes']} transcripts, {g['gaps_found']} gaps found, "
                     f"{g['gaps_filled']} filled, {g['words_added']} words added; "
                     f"{g['skipped']} skipped, {g['errors']} errors")
        if g["errors"]:
            lines.append(f"WARNING: gap fill failed on {g['errors']} transcripts (kept without "
                         "it; see provenance gap_fill.error / traceback)")
    sc = s["screen"]
    lines.append(f"screen: {sc['episodes_screened']} episodes, {sc['reads']} reads, "
                 f"{sc['failed_windows']} failed windows, {sc['total_tokens']} tokens, "
                 f"cost_usd {sc['cost_usd']}; episode screen_status {sc['status'] or '-'}")
    if sc["unpriced_runs"]:
        lines.append(f"WARNING: {sc['unpriced_runs']} screen runs used a model missing from "
                     "llm_screen.PRICES_PER_MTOK; cost_usd above excludes them")
    lines.extend(skipped_stage_warnings(s))
    lines.append("disk bytes: " + "  ".join(f"{k}={v}" for k, v in s["disk_bytes"].items()))
    for d in s["dead"]:
        lines.append(f"  DEAD {d['episode_id']} ({d['show_title']}): {d['last_error']}")
    for e in s["show_errors"]:
        lines.append(f"  FEED ERROR {e['title']}: {e['last_error']}")
    return "\n".join(lines)


def skipped_stage_warnings(s: dict) -> list:
    """WARNING lines for episodes that went past an enabled jev / name /
    diarize stage because no worker of it was live (routing needs a worker
    seen in the last SCREEN_WORKER_TTL_S): a run that starts stages one at a
    time, or a stopped worker, silently measures a shorter chain."""
    out = []
    for stage, totals, marker, fix in (
            ("jev", s["jev"], NO_JEV_WORKER, "jev-backfill, then name-backfill"),
            ("name", s["names"], NO_NAME_WORKER, "name-backfill"),
            ("diarize", s["diarize"], NO_DIARIZE_WORKER,
             "diarize-backfill while the audio is kept")):
        n = (totals.get("status") or {}).get(marker)
        if n:
            out.append(f"WARNING: {n} episodes skipped {stage} ({marker}): run its worker "
                       f"alongside the others, or fill them in with {fix}")
    return out


# ---------------------------------------------------------------- export

EXPORT_CANDIDATES_REL = "experiments/next-validation-detector/output/candidates_normalized.json"
EXPORT_LLM_CANDIDATES_REL = "experiments/llm-screen/output/candidates_normalized.json"
# Named Jev spans (the name stage) as candidates; `app_workspace.py seed`
# imports them as their own source (import_sources.NAMED_OUT_REL).
EXPORT_NAMED_CANDIDATES_REL = "experiments/named-reads/output/candidates_normalized.json"
# The path and shape `app_workspace.py seed` reads Jev windows from
# (import_sources.JEV_RUN_REL).
EXPORT_JEV_RESULTS_REL = "experiments/jev-ad-triage-20260921/results.json"


def jev_results_doc(episodes: list) -> dict:
    """A seed-importable Jev run (the 2026-09-21 triage run's shape) from
    jev/<ep>.json docs. No timestamps at the top level: an unchanged export
    is byte-identical, so a re-seed adds no new source version."""
    models = sorted({e["model"] for e in episodes if e.get("model")})
    return {
        "run_id": "corpus-jev", "status": "corpus_jev_screen",
        "model_requested": jev_screen.DEFAULT_MODEL,
        "model_returned": ",".join(models) or jev_screen.DEFAULT_MODEL,
        "pricing_source": jev_screen.PRICING_SOURCE,
        "input_price_usd_per_million_tokens": jev_screen.INPUT_USD_PER_MTOK,
        "output_price_usd_per_million_tokens": jev_screen.OUTPUT_USD_PER_MTOK,
        "window_seconds": jev_screen.WINDOW_S, "question": jev_screen.QUESTION,
        "state_contract": "per episode: timestamped 60-second transcript windows, at most "
                          f"{jev_screen.MAX_WINDOWS_PER_REQUEST} per request; no detector "
                          "output or labels supplied",
        "seed_threshold": jev_screen.DEFAULT_THRESHOLD,
        "episodes": sorted(episodes, key=lambda e: e["episode_id"])}


def _speakers_doc(dirs, ep_id: str, duration_s, audio_sha256):
    """(the episode's validated speakers/<ep>.json or None, problem or None):
    a file that exists but is unusable is a problem ("invalid"), as is one
    from another model revision or other audio ("stale"), not a crash."""
    path = dirs["speakers"] / f"{ep_id}.json"
    if not path.exists():
        return None, None
    doc = _read_json(path)
    try:
        dz.validate_speakers(doc, ep_id, duration_s)
    except ValueError as exc:
        return None, f"invalid: {exc}"
    if not dz.is_current(doc, audio_sha256):
        return None, "stale: other model revision or audio"
    return doc, None


def _voice_provenance(speakers: dict) -> dict:
    """What produced an episode's voice attrs (one per episode, not per read)."""
    return {**{k: speakers.get(k) for k in ("model", "revision", "device", "window_s",
                                            "overlap_s")},
            "voice_ad_only_below": dz.VOICE_AD_ONLY_SHARE,
            "min_judged_speech_s": dz.MIN_JUDGED_SPEECH_S, "ad_spans": VOICE_AD_SPANS}


# Which transcript copy a Jev result read (transcript_version_id only for a
# workspace copy, see _check_workspace_copy).
JEV_COPY_KEYS = ("segment_count", "segments_sha256", "transcript_version_id")


def _jev_episode(ep_id: str, doc: dict, duration_s) -> dict:
    return {"episode_id": ep_id, "model": doc.get("model"),
            "duration_min": round(duration_s / 60, 2) if duration_s else None,
            "window_count": len(doc["windows"]), "usage": doc.get("usage"),
            "input_cost_usd": doc.get("cost_usd"), "screened_at": doc.get("screened_at"),
            **{k: doc[k] for k in JEV_COPY_KEYS if k in doc},
            "answers": {w["window_id"]: {"noul": w["noul"], "start_s": w["start_s"],
                                         "end_s": w["end_s"]} for w in doc["windows"]}}


def _link_or_copy(src, dst) -> None:
    import shutil
    Path(dst).unlink(missing_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def show_identity(corpus, *, fetch_feed=None, max_fetches: int = self_promo.MAX_FETCHES,
                  refresh: bool = False, corrections=None) -> dict:
    """Cache each show's own web identity from its public RSS feed in
    <corpus>/show-identity/ (one request per show; failures retried after
    a day, older identity versions refetched, `refresh` refetches all).

    With `corrections` (corrections.Corrections), also report what the
    operator's self-promo rules cover in this corpus: per rule, the shows
    whose title contains its network text (the export marks their reads
    of the rule's brand self-promo, identity or not); a rule that covers
    no show is listed in self_promo_rules_unmatched (a typo, or a show
    this corpus does not hold)."""
    con = connect(corpus)
    try:
        ensure_schema(con)
        urls = [r[0] for r in con.execute(
            "SELECT feed_url FROM shows UNION SELECT DISTINCT feed_url FROM episodes"
            " ORDER BY 1")]
        titles = sorted({r[0] for r in con.execute(
            "SELECT title FROM shows UNION SELECT DISTINCT show_title FROM episodes")
            if r[0]}) if corrections is not None else []
    finally:
        con.close()
    rep = self_promo.build_identities(urls, Path(corpus) / self_promo.IDENTITY_DIR,
                                      fetch_feed or http_get_capped, max_fetches=max_fetches,
                                      refresh=refresh)
    out = {"shows": len(urls), **rep}
    if corrections is not None:
        rules = corrections.self_promo_shows(titles)
        out.update(corrections=corrections.provenance(), self_promo_rules=rules,
                   self_promo_rules_unmatched=[r for r in rules if not r["shows"]])
    return out


def _own_network_promo(c: dict):
    """The network of a named read marked as a promo for its show's own
    network (promos.name_promos, self_promo_own_network), else None."""
    promo = c.get("promo") or {}
    return promo.get("own_network") if promo.get("self_promo") else None


def _mark_self_promo(corpora, named_cands, texts, feeds, named_eps) -> list:
    """Self-promo fields on the named candidates of shows with a cached
    identity (first corpus that has one); other shows' candidates are
    returned unchanged."""
    idents = {}
    for f in set(feeds):
        idents[f] = next((d for d in (self_promo.load_identity(
            Path(c) / self_promo.IDENTITY_DIR, f) for c in corpora) if d), None)
    if not any(idents.values()):
        return named_cands
    lists = [_read_json(Path(c) / BRAND_LIST_NAME) for c in corpora]
    known = self_promo.known_brand_keys(
        [e for lst in lists if isinstance(lst, list) for e in lst])
    recurrence = self_promo.show_recurrence(
        [(f, ep, None) for f, ep in named_eps]
        + [(f, c["ep_id"], self_promo.candidate_brand(c)) for c, f in zip(named_cands, feeds)])
    out = []
    for c, text, f in zip(named_cands, texts, feeds):
        if idents.get(f) is None:
            out.append(c)
        else:
            out.extend(self_promo.mark([c], texts=[text], identities=[idents[f]],
                                       recurrence=recurrence, feeds=[f], known_brands=known))
    return out


def shownotes_pass(corpora) -> dict:
    """Match every named read of the exported episodes (done, or dead after
    transcription) against its episode's show notes (shownotes/<ep>.json,
    stored by `discover`) and write shownotes-reads/<ep>.json in the
    episode's corpus (the first corpus holding the episode, as export).
    Reads are built as export builds them (refined text, the first
    corpus's brand-canon.json). Read-only on the queue; a result file is
    rewritten only when it changes, so re-running it changes nothing. An
    episode that fails (bad notes, full disk) is counted in `errors` and
    skipped; the others still go."""
    corpora = [Path(c) for c in ([corpora] if isinstance(corpora, (str, Path)) else corpora)]
    canon_path = next((c / brand_canon.CANON_NAME for c in corpora
                       if (c / brand_canon.CANON_NAME).is_file()), None)
    canon = brand_canon.load_canon(canon_path) if canon_path else None
    rep = {"corpora": len(corpora), "episodes": 0, "episodes_with_notes": 0,
           "episodes_with_listings": 0, "listings": 0,
           "reads": {s: 0 for s in shownotes.STATUSES}, "matches": {},
           "clears_review": 0, "written": 0, "unchanged": 0, "errors": 0}
    seen, idents = set(), {}
    for corpus in corpora:
        dirs = corpus_dirs(corpus)
        con = connect(corpus)
        try:
            rows = [dict(r) for r in con.execute(
                "SELECT episode_id, feed_url, show_title, ep_title, pub_date FROM episodes"
                " WHERE stage='done' OR (stage='dead' AND dead_stage IN"
                " ('detect','jev','name','diarize','screen','clip')) ORDER BY episode_id")]
        finally:
            con.close()
        for r in rows:
            ep_id = r["episode_id"]
            names = _read_json(dirs["names"] / f"{ep_id}.json")
            if ep_id in seen or named_spans(names) is None:
                continue
            seen.add(ep_id)
            segs = (_read_json(dirs["transcripts"] / f"{ep_id}.json") or {}).get("segments") or []
            if names.get("refined"):
                segs = refined_segments(segs, _read_json(dirs["refined"] / f"{ep_id}.json"))
            cands = bn.named_candidates(ep_id, names, segs, {
                "show_title": r["show_title"], "ep_title": r["ep_title"],
                "pub_date": r["pub_date"]})
            if canon:
                cands = brand_canon.apply_canon(cands, canon)
            feed = r["feed_url"]
            if feed not in idents:
                idents[feed] = next((d for d in (self_promo.load_identity(
                    Path(c) / self_promo.IDENTITY_DIR, feed) for c in corpora) if d), None)
            notes = shownotes.load_notes(corpus, ep_id)
            try:
                doc = shownotes.episode_result(ep_id, cands, notes, idents[feed],
                                               r["show_title"])
                rep["written" if shownotes.write_result(corpus, doc) else "unchanged"] += 1
            except Exception as e:  # one bad notes file or a full disk: the rest still go
                rep["errors"] += 1
                rep.setdefault("error_episodes", []).append(
                    f"{ep_id}: {type(e).__name__}: {e}"[:200])
                continue
            rep["episodes"] += 1
            rep["episodes_with_notes"] += notes is not None
            rep["episodes_with_listings"] += bool(doc["sponsors"])
            rep["listings"] += len(doc["sponsors"] or ())
            for x in doc["reads"].values():
                rep["reads"][x["status"]] += 1
                if x["match"]:
                    rep["matches"][x["match"]] = rep["matches"].get(x["match"], 0) + 1
                rep["clears_review"] += bool(x["clears_review"])
    return rep


def export(corpora, out, corrections=None, *, exclude=frozenset(), media=True) -> dict:
    """Write a seed-importable source root from DONE episodes only.

    `corpora` is one corpus dir or several (e.g. one per Mac, each running
    its own --shard); ids are content-derived, so shards merge cleanly and a
    duplicate id keeps the first corpus's copy. Named reads take the first
    corpus's brand-canon.json when one exists: a read whose brand the map
    moves gets brand_canonical + canon_reason; brand, brand_spoken and
    brand_raw stay as named (they are evidence identity on seed). The map
    is applied even when stale (built from other names/transcripts than
    these corpora now hold): brand_canon_stale reports it.

    Named reads of shows with a cached show identity (`show_identity`) get
    the display attrs self_promo / self_promo_reason (every span stays in
    the named reads; seed keeps them out of evidence identity).

    After a `shownotes` pass (shownotes-reads/ in any corpus), named reads
    the pass saw with the same brand and span carry shownotes_status (and,
    when listed, shownotes_match / shownotes_brand / shownotes_evidence);
    a correction is only shownotes_brand (never brand_canonical), and no
    match changes needs_review. Without a pass the export is unchanged.

    Episodes with speaker turns (speakers/<ep>.json, the diarize stage) give
    their named reads the display attrs dominant_speaker /
    voice_share_outside_ads / voice_ad_only (diarize.voice_attrs; the ad
    spans are the episode's named spans, Jev windows at the default
    threshold and screen reads), and get a speaker-labeled transcript in
    speaker-transcripts/<ep>.json (word-level where the transcript carries
    word timestamps, else segment-level with mixed_speakers). Each such
    episode row carries `voice_provenance` (model, revision, device,
    windows, threshold, ad-span definition). A speakers file that fails
    validation is skipped and listed in the report's `speakers_invalid`;
    one from another model revision or other audio in `speakers_stale`.
    With no speakers anywhere (stage off) the export is the same as before
    the diarize stage: diarize keys appear only when set.

    An episode exports the ad rows only of the stages its tier's stage set
    runs (episodes.tier, tier_stages): detector rows with detect, Jev rows
    and jev_status with jev, named rows with name, screen rows and
    screen_status with screen, speakers with diarize, clips with clip. The
    default transcript tier runs none of them: its episodes export their
    transcript only, for mention search, which means "not screened for
    ads". A row of any tier but full carries `coverage_tier: <tier>`;
    full-tier rows carry no such key.

    `corrections` (corrections.Corrections; None = the export exactly as
    without): after the canon map, the operator's alias rules give heard
    spellings their canonical (brand_canonical, canon_reason
    "corrections:alias"; brand_spoken unchanged), the self-promo rules mark
    reads (self_promo_reason "network:<network>"), and reads the operator
    marked not an ad are left out (matched by record key, or by episode
    and overlapping span when a re-name moved them). The report's
    `corrections` lists what was dropped, moved and unmatched, and whether
    names docs or the canon map were built with other rules (stale).

    `exclude` (episode IDs) leaves those episodes out, as if not done.
    `media=False` writes no clip files and no speaker-transcripts files;
    every record (clip paths and speaker_transcript included) stays as
    with media.
    """
    import shutil
    corpora = [Path(c) for c in ([corpora] if isinstance(corpora, (str, Path)) else corpora)]
    out = Path(out)
    tdir = out / "transcripts"
    tdir.mkdir(parents=True, exist_ok=True)
    cdir = out / "clips"
    sdir = out / "speaker-transcripts"
    episodes, candidates, llm_cands, jev_eps, keep = [], [], [], [], set()
    n_voice, spk_keep, spk_bad, spk_stale = 0, set(), [], []
    named_cands, named_texts, named_feeds, named_eps = [], [], [], []
    n_clips = n_transcript_only = names_stale = 0
    gf_raws = []
    for corpus in corpora:
        dirs = corpus_dirs(corpus)
        con = connect(corpus)
        try:
            ensure_schema(con)
            tier_sets = {t: tier_stages(con, t) for t in TIERS}
            rows = [dict(r) for r in con.execute(
                "SELECT e.*, o.finished_at AS fetched_at, o.final_url AS obs_final_url,"
                " o.host AS fetch_host, o.verification AS fetch_verification"
                " FROM episodes e LEFT JOIN fetch_observations o"
                " ON o.id = e.fetch_observation_id WHERE e.stage='done'"
                " OR (e.stage='dead' AND e.dead_stage IN"
                " ('detect','jev','name','diarize','screen','clip'))"
                " ORDER BY e.episode_id")]
        finally:
            con.close()
        for r in rows:
            ep_id = r["episode_id"]
            dst = tdir / f"{ep_id}.json"
            if dst.name in keep or ep_id in exclude:
                continue
            src = dirs["transcripts"] / f"{ep_id}.json"
            # Ad rows only of the stages the episode's tier runs, even when a
            # mid-flight move left other stages' files behind.
            ran = tier_sets.get(r.get("tier"))
            runs = (lambda st: True) if ran is None else ran.__contains__
            if r["stage"] == "dead":  # died after transcription: export what exists
                if not src.is_file():
                    continue
                if runs("detect"):
                    candidates.extend(_read_json(dirs["detections"] / f"{ep_id}.json") or [])
            elif runs("detect"):
                candidates.extend(json.loads(
                    (dirs["detections"] / f"{ep_id}.json").read_text()))
            _link_or_copy(src, dst)
            keep.add(dst.name)
            gf_raws.append(r.get("gap_fill_status"))
            fetch_date = (r["fetched_at"] or "")[:10] or None
            jev = _read_json(dirs["jev"] / f"{ep_id}.json") if runs("jev") else None
            if jev_flagged(jev) is not None:
                jev_eps.append(_jev_episode(ep_id, jev, r["duration_s"]))
            meta = {"show_title": r["show_title"], "ep_title": r["ep_title"],
                    "pub_date": r["pub_date"], "fetch_date": fetch_date}
            screen = _read_json(dirs["screens"] / f"{ep_id}.json") if runs("screen") else None
            if isinstance(screen, dict):
                llm_cands.extend(llm_screen.llm_candidates(ep_id, screen, meta))
            names = _read_json(dirs["names"] / f"{ep_id}.json") if runs("name") else None
            speakers, problem = _speakers_doc(dirs, ep_id, r["duration_s"], r["audio_sha256"]) \
                if runs("diarize") else (None, None)
            if problem:
                (spk_stale if problem.startswith("stale") else spk_bad).append(ep_id)
            spk_path = None
            if speakers is not None:
                spk_path = f"speaker-transcripts/{ep_id}.json"
                if media:
                    atomic_write_json(out / spk_path, {
                    "episode_id": ep_id, "model": speakers["model"],
                    "revision": speakers.get("revision"), "speakers": speakers.get("speakers"),
                    **dz.labeled_transcript(speakers, (_read_json(dst) or {}).get("segments"))})
                    spk_keep.add(f"{ep_id}.json")
            if named_spans(names) is not None:
                segs = (_read_json(dst) or {}).get("segments") or []
                if names.get("refined"):  # excerpts from the text the names came from
                    segs = refined_segments(segs, _read_json(dirs["refined"] / f"{ep_id}.json"))
                mine = bn.named_candidates(ep_id, names, segs, meta)
                if corrections is not None and corrections.names_stale(names, ep_id):
                    names_stale += 1
                if speakers is not None:
                    ads = named_spans(names) + (jev_flagged(jev) or []) + [
                        {"start_s": x.get("start_s"), "end_s": x.get("end_s")}
                        for x in ((screen or {}).get("reads") or []) if isinstance(x, dict)]
                    for c, attrs in zip(mine, dz.voice_attrs(speakers, mine, ads)):
                        c.update(attrs)
                        n_voice += attrs["dominant_speaker"] is not None
                named_cands.extend(mine)
                named_texts += [self_promo.span_text(segs, c["start_s"], c["end_s"]) for c in mine]
                named_feeds += [r["feed_url"]] * len(mine)
                named_eps.append((r["feed_url"], ep_id))
            shutil.rmtree(cdir / ep_id, ignore_errors=True)
            idx = _read_json(dirs["clips"] / ep_id / "index.json") if runs("clip") else None
            clip_paths = []
            if isinstance(idx, dict):
                if media:
                    (cdir / ep_id).mkdir(parents=True)
                for c in idx.get("clips") or []:
                    src = dirs["clips"] / ep_id / c["file"]
                    if src.is_file():
                        if media:
                            _link_or_copy(src, cdir / ep_id / c["file"])
                        clip_paths.append(f"clips/{ep_id}/{c['file']}")
                if media:
                    _link_or_copy(dirs["clips"] / ep_id / "index.json",
                                  cdir / ep_id / "index.json")
            n_clips += len(clip_paths)
            ep = {
                "ep_id": ep_id, "show_title": r["show_title"], "category": r["category"],
                "feed_url": r["feed_url"], "ep_title": r["ep_title"], "guid": r["guid"],
                "pub_date": r["pub_date"], "audio_url": r["enclosure_url"],
                "duration_min": round(r["duration_s"] / 60, 2) if r["duration_s"] else None,
                "fetch_date": (r["fetched_at"] or "")[:10] or None,
                "fetch_observation_id": r["fetch_observation_id"],
                "fetch_host": r["fetch_host"], "fetched_at": r["fetched_at"],
                "final_url": r["obs_final_url"], "audio_sha256": r["audio_sha256"],
                "audio_bytes": r["audio_bytes"],
                "length_verified": _length_verified(r["fetch_verification"]),
                "clip_status": idx.get("status") if isinstance(idx, dict) else None,
                "clip_skip_reason": idx.get("reason") if isinstance(idx, dict) else None,
                "clips": clip_paths,
                "screen_status": r["screen_status"] if runs("screen") else None,
                "jev_status": r["jev_status"] if runs("jev") else None,
                "pipeline_status": "done" if r["stage"] == "done" else f"dead:{r['dead_stage']}"}
            if r.get("tier", "full") != "full":  # full-tier rows stay as before
                ep["coverage_tier"] = r["tier"]
                n_transcript_only += 1
            # Diarize keys only when set: with the stage off the export is unchanged.
            if r["diarize_status"] is not None:
                ep["diarize_status"] = r["diarize_status"]
            if speakers is not None:
                ep.update(speaker_transcript=spk_path, voice_provenance=_voice_provenance(speakers))
            episodes.append(ep)
    for stale in tdir.glob("*.json"):
        if stale.name not in keep:
            stale.unlink()
    for stale in cdir.iterdir() if cdir.exists() else ():
        if stale.is_dir() and f"{stale.name}.json" not in keep:
            shutil.rmtree(stale, ignore_errors=True)
    for stale in sdir.glob("*.json") if sdir.exists() else ():
        if stale.name not in spk_keep:
            stale.unlink()
    episodes.sort(key=lambda e: e["ep_id"])
    candidates.sort(key=lambda c: (c["ep_id"], c.get("start_s") or 0))
    llm_cands.sort(key=lambda c: (c["ep_id"], c.get("start_s") or 0))
    named_cands = _mark_self_promo(corpora, named_cands, named_texts, named_feeds, named_eps)
    # a promo for the show's own network, marked so at rename
    # (--promo-own-network-self-promo): self-promo as the operator rules mark it
    named_cands = self_promo.apply_rules(named_cands, _own_network_promo)
    named_cands.sort(key=lambda c: (c["ep_id"], c.get("start_s") or 0))
    canon_path = next((c / brand_canon.CANON_NAME for c in corpora
                       if (c / brand_canon.CANON_NAME).is_file()), None)
    canon = brand_canon.load_canon(canon_path) if canon_path else None
    n_canon, stale = 0, None
    if canon:
        named_cands = brand_canon.apply_canon(named_cands, canon)
        n_canon = sum(1 for c in named_cands if c.get("brand_canonical"))
        stale = brand_canon.is_stale(canon, corpora)
    sn_rep = None
    if any((c / shownotes.READS_DIR).is_dir() for c in corpora):  # `shownotes` pass ran
        named_cands, sn_rep = shownotes.apply_to_candidates(corpora, named_cands)
    corr_rep = None
    if corrections is not None:
        named_cands, n_stopped = corrections.stop_kept_spans(named_cands)
        named_cands = corrections.apply_aliases(named_cands, canon)
        named_cands = self_promo.apply_rules(named_cands, corrections.self_promo_network)
        named_cands, corr_rep = corrections.drop_not_ads(named_cands)
        n_canon = sum(1 for c in named_cands if c.get("brand_canonical"))
        corr_rep = {**corrections.provenance(), **corr_rep,
                    "aliased": sum(1 for c in named_cands
                                   if "corrections:alias" in str(c.get("canon_reason") or "")),
                    # reads nobody confirmed in a span kept for a confirmed read
                    "stopped_in_kept_spans": n_stopped,
                    "self_promo_by_rule": sum(1 for c in named_cands if str(
                        c.get("self_promo_reason") or "").startswith("network:")),
                    # named before these rules (or without any): re-name to apply
                    # the namer's rules (stoplist, new brands)
                    "names_stale": names_stale,
                    "canon_stale": None if not canon else corrections.canon_stale(canon)}
    atomic_write_json(out / "data" / "episodes.json", episodes)
    atomic_write_json(out / EXPORT_CANDIDATES_REL, candidates)
    atomic_write_json(out / EXPORT_LLM_CANDIDATES_REL, llm_cands)
    if jev_eps:
        atomic_write_json(out / EXPORT_JEV_RESULTS_REL, jev_results_doc(jev_eps))
    else:  # seed then needs --no-jev, as before; never a stale run
        (out / EXPORT_JEV_RESULTS_REL).unlink(missing_ok=True)
    n_promo = sum(1 for c in named_cands if c.get("self_promo"))
    if named_cands:
        atomic_write_json(out / EXPORT_NAMED_CANDIDATES_REL, named_cands)
    else:  # seed skips named reads when the file is absent; never a stale one
        (out / EXPORT_NAMED_CANDIDATES_REL).unlink(missing_ok=True)
    gf_sum = _gap_fill_sum(gf_raws)
    return {"out": str(out), "corpora": len(corpora), "episodes": len(episodes),
            "transcripts": len(keep), "candidates": len(candidates),
            "llm_candidates": len(llm_cands), "jev_episodes": len(jev_eps),
            "named_candidates": len(named_cands), "named_self_promo": n_promo,
            "clips": n_clips,
            "brand_canon": str(canon_path) if canon else None, "named_canonicalized": n_canon,
            "brand_canon_stale": stale,
            **({"corrections": corr_rep} if corr_rep is not None else {}),
            **{k: v for k, v in (("gap_fill", gf_sum if gf_sum["episodes"] else None),
                                 ("shownotes", sn_rep),
                                 ("named_voice_attrs", n_voice),
                                 ("transcript_only_episodes", n_transcript_only),
                                 ("speaker_transcripts", len(spk_keep)),
                                 ("speakers_invalid", sorted(spk_bad)),
                                 ("speakers_stale", sorted(spk_stale))) if v}}


# ---------------------------------------------------------------- jev backfill

def jev_estimate(corpus, episode_ids, model: str = jev_screen.DEFAULT_MODEL) -> dict:
    """Requests, input tokens and cost of Jev-screening these episodes'
    transcripts (jev-backfill --dry-run, set-tier --dry-run)."""
    tdir = corpus_dirs(corpus)["transcripts"]
    rep = {"episodes": len(episode_ids), "requests_est": 0, "input_tokens_est": 0}
    for ep_id in episode_ids:
        doc = _read_json(tdir / f"{ep_id}.json") or {}
        est = jev_screen.estimate(doc.get("segments") or [], model=model, episode_id=ep_id)
        rep["requests_est"] += est["requests"]
        rep["input_tokens_est"] += est["input_tokens"]
    rep["cost_usd_est"] = jev_screen.cost_usd({"input_tokens": rep["input_tokens_est"]})
    return rep


def _check_workspace_copy(doc: dict, ep_id: str) -> dict:
    """A transcript exported from an app workspace (jev_rescreen): its
    completeness was checked when the workspace loaded it, and it carries no
    STT run fields, so check the segment rules and that it is still the
    exported copy (episode, version id, digest). Returns workspace_copy."""
    copy = doc.get("workspace_copy")
    segments = doc.get("segments")
    if not isinstance(copy, dict) or copy.get("episode_id") != ep_id:
        raise TranscriptInvalid("workspace copy is for another episode")
    tv = copy.get("transcript_version_id")
    if isinstance(tv, bool) or not isinstance(tv, int):
        raise TranscriptInvalid("workspace copy lacks its transcript_version_id")
    if not isinstance(segments, list) or not segments:
        raise TranscriptInvalid("workspace copy has no segments")
    prev = -1.0
    for i, s in enumerate(segments):
        if not isinstance(s, dict) or not isinstance(s.get("text"), str):
            raise TranscriptInvalid(f"segment {i} malformed")
        start, end = s.get("start"), s.get("end")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                   and math.isfinite(v) for v in (start, end)):
            raise TranscriptInvalid(f"segment {i} bounds not numeric")
        if start < 0 or end <= start or start < prev:
            raise TranscriptInvalid(f"segment {i} bounds out of order")
        prev = start
    if copy.get("segment_count") != len(segments) \
            or copy.get("segments_sha256") != jev_screen.segments_digest(segments):
        raise TranscriptInvalid("workspace copy changed since export (segments differ)")
    return copy


def jev_backfill(corpus, *, transport=None, threshold: float = jev_screen.DEFAULT_THRESHOLD,
                 dry_run: bool = False, model: str = jev_screen.DEFAULT_MODEL,
                 log=print) -> dict:
    """Run Jev over the transcripts of `done` episodes that have no Jev
    result (no audio, no clips). A workspace-exported transcript
    (workspace_copy, jev_rescreen.export_current_copies) is checked by
    _check_workspace_copy and its result records its transcript_version_id.
    Idempotent and crash-safe: jev/<ep>.json
    is written atomically and is what marks an episode as screened, so a
    rerun after a crash picks up exactly the rest. Permanent failures are
    recorded (jev_status) and skipped on reruns; a fatal error (key,
    payment) raises JevFatal. Each attempt is a stage_runs row (stage jev,
    worker jev-backfill) carrying tokens and cost, so `status` totals it."""
    corpus = Path(corpus)
    dirs = corpus_dirs(corpus)
    con = connect(corpus)
    host = socket.gethostname()
    try:
        ensure_schema(con)
        tiers = tiers_running(con, "jev")
        rows = con.execute("SELECT episode_id, duration_s FROM episodes WHERE stage='done'"
                           " AND COALESCE(jev_status, '') NOT LIKE 'failed: permanent%%'"
                           " AND tier IN (%s) ORDER BY episode_id" % ",".join("?" * len(tiers)),
                           tiers).fetchall()
        todo = [r for r in rows if not (dirs["jev"] / f"{r[0]}.json").exists()
                and (dirs["transcripts"] / f"{r[0]}.json").exists()]
        if dry_run:
            return {"dry_run": True, **jev_estimate(corpus, [r[0] for r in todo], model=model)}
        dirs["jev"].mkdir(exist_ok=True)
        rep = {"dry_run": False, "episodes": len(todo), "screened": 0, "failed": 0,
               "windows": 0, "windows_flagged": 0, "input_tokens": 0, "cost_usd": 0.0}

        def record(ep_id, started, t0, outcome, usage, error=None, jev_status=None):
            cost = jev_screen.cost_usd(usage)
            with _Tx(con):
                con.execute("INSERT INTO stage_runs (episode_id, stage, attempt, worker, host,"
                            " started_at, finished_at, wall_s, outcome, error, llm_usage,"
                            " cost_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (ep_id, "jev", 1, "jev-backfill", host, started, utcnow(),
                             round(time.monotonic() - t0, 3), outcome, error,
                             json.dumps(usage, sort_keys=True), cost))
                if jev_status is not None:
                    con.execute("UPDATE episodes SET jev_status=?, updated_at=? WHERE"
                                " episode_id=? AND stage='done'", (jev_status, utcnow(), ep_id))
            rep["input_tokens"] += usage.get("input_tokens") or 0
            rep["cost_usd"] = round(rep["cost_usd"] + cost, 8)

        for i, (ep_id, duration_s) in enumerate(todo, 1):
            out = dirs["jev"] / f"{ep_id}.json"
            if out.exists():  # another backfill got here first
                continue
            started, t0 = utcnow(), time.monotonic()
            try:
                doc = json.loads((dirs["transcripts"] / f"{ep_id}.json").read_text("utf-8"))
                copy = None
                if "workspace_copy" in doc:
                    copy = _check_workspace_copy(doc, ep_id)
                else:
                    validate_transcript(doc, duration_s)
                res = jev_screen.screen_episode(doc["segments"], transport=transport,
                                                model=model, episode_id=ep_id)
                if copy is not None:
                    res["transcript_version_id"] = copy["transcript_version_id"]
            except Exception as exc:
                kind = jev_screen.classify_error(exc)
                usage = getattr(exc, "usage", None) or {"input_tokens": 0, "output_tokens": 0}
                why = f"{type(exc).__name__}: {exc}"[:500]
                refused = getattr(exc, "cause", exc)
                if isinstance(refused, jev_budget.JevBudgetRefused):
                    # spend cap: stop; the rest waits for the next period's run
                    record(ep_id, started, t0, "error", usage, why)
                    rep["budget_refused"] = refused.code
                    jev_budget_alert(corpus, refused.code, f"jev-backfill stopped after "
                                     f"{rep['screened']} episode(s) "
                                     f"({refused.reason or 'spend cap reached'})", log)
                    break
                record(ep_id, started, t0, "error", usage, why,
                       None if kind == "fatal" else f"failed: {kind}: {why}"[:500])
                if kind == "fatal":
                    raise jev_screen.JevFatal(f"jev API error on {ep_id}: {why}") from exc
                rep["failed"] += 1
                log(f"[jev-backfill] {i}/{len(todo)} {ep_id} FAILED ({kind}): {why}")
                continue
            res = {"episode_id": ep_id, "screened_at": utcnow(), **res}
            atomic_write_json(out, res)
            record(ep_id, started, t0, "ok", res["usage"], jev_status="ok (backfill)")
            flagged = len(jev_flagged(res, threshold))
            rep["screened"] += 1
            rep["windows"] += len(res["windows"])
            rep["windows_flagged"] += flagged
            log(f"[jev-backfill] {i}/{len(todo)} {ep_id}: {flagged}/{len(res['windows'])} "
                f"windows at noul>={threshold}; cost so far ${rep['cost_usd']:.6f}")
        return rep
    finally:
        con.close()


# ---------------------------------------------------------------- brand naming

def build_namer(corpus, *, gliner: str = "small", brand_list=None, threads: int | None = None,
                log=print, harvest_episodes=None, harvest_trusted=None,
                list_name: str = BRAND_LIST_NAME, corrections=None) -> bn.BrandNamer:
    """The name stage's BrandNamer. The repair list is `brand_list` (JSON
    from harvest_brand_list) or, by default, harvested from every transcript
    in the corpus now (or from `harvest_episodes`, (episode_id, segments)
    pairs) and written to <corpus>/<list_name> (rebuilt at each start, so it
    grows with the corpus). gliner="none" runs extraction + repair only;
    when GLiNER is asked for but cannot load, that is logged and recorded in
    every names doc. Either way the list is also kept by digest in
    <corpus>/brand-lists/ (bn.archive_brand_list), the digest each names
    doc records. `corrections` (corrections.Corrections): the operator's
    rules join the namer (BrandNamer), never the archived list."""
    corpus = Path(corpus)
    if brand_list:
        blist = json.loads(Path(brand_list).read_text(encoding="utf-8"))
    else:
        t0 = time.monotonic()
        blist = bn.harvest_brand_list(sorted(corpus_dirs(corpus)["transcripts"].glob("*.json")),
                                      episodes=harvest_episodes, trusted=harvest_trusted)
        atomic_write_json(corpus / list_name, blist)
        log(f"brand list: {len(blist)} entries harvested in {time.monotonic() - t0:.1f}s")
    bn.archive_brand_list(corpus, blist)
    namer = bn.BrandNamer(blist, None if gliner == "none" else gliner, threads=threads,
                          corrections=corrections)
    if gliner != "none" and namer.gliner is None:
        log(f"WARNING: GLiNER-{gliner} unavailable ({namer.gliner_error}); naming with "
            "domain/CTA extraction + repair only")
    return namer


def name_backfill(corpus, *, namer, threshold: float = jev_screen.DEFAULT_THRESHOLD,
                  context_windows: int = DEFAULT_JEV_CONTEXT_WINDOWS,
                  sub_window_s: float | None = DEFAULT_NAME_SUB_WINDOW_S, log=print) -> dict:
    """Name the Jev-flagged spans of `done` episodes that have a Jev result
    but no names/<ep>.json (transcripts only: no audio, no clips).
    Idempotent and crash-safe: names/<ep>.json is written atomically and is
    what marks an episode as named, so a rerun after a crash picks up
    exactly the rest. Each episode is a stage_runs row (stage name, worker
    name-backfill); failures are recorded in name_status and retried on the
    next run."""
    corpus = Path(corpus)
    dirs = corpus_dirs(corpus)
    bn.check_names_lock(dirs["names"])
    con = connect(corpus)
    host = socket.gethostname()
    try:
        ensure_schema(con)
        rows = con.execute("SELECT episode_id, duration_s FROM episodes WHERE stage='done'"
                           " ORDER BY episode_id").fetchall()
        todo = [r for r in rows if not (dirs["names"] / f"{r[0]}.json").exists()
                and (dirs["jev"] / f"{r[0]}.json").exists()
                and (dirs["transcripts"] / f"{r[0]}.json").exists()]
        dirs["names"].mkdir(exist_ok=True)
        rep = {"episodes": len(todo), "named": 0, "failed": 0, "spans": 0, "spans_named": 0,
               "spans_needs_review": 0, "pipeline": namer.pipeline,
               "sub_window_s": sub_window_s}

        def record(ep_id, started, t0, cpu0, outcome, error, name_status):
            with _Tx(con):
                con.execute("INSERT INTO stage_runs (episode_id, stage, attempt, worker, host,"
                            " started_at, finished_at, wall_s, cpu_s, outcome, error)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (ep_id, "name", 1, "name-backfill", host, started, utcnow(),
                             round(time.monotonic() - t0, 3),
                             round(time.process_time() - cpu0, 3), outcome, error))
                con.execute("UPDATE episodes SET name_status=?, updated_at=? WHERE"
                            " episode_id=? AND stage='done'", (name_status, utcnow(), ep_id))

        for i, (ep_id, duration_s) in enumerate(todo, 1):
            out = dirs["names"] / f"{ep_id}.json"
            if out.exists():  # another backfill got here first
                continue
            started, t0, cpu0 = utcnow(), time.monotonic(), time.process_time()
            try:
                doc = json.loads((dirs["transcripts"] / f"{ep_id}.json").read_text("utf-8"))
                if "workspace_copy" in doc:
                    # A workspace export (jev_rescreen): the same copy check as
                    # jev_backfill, and never GLiNER (no model load, no network).
                    if namer.gliner is not None:
                        raise ValueError("a workspace copy is named without GLiNER"
                                         " (--name-gliner none)")
                    _check_workspace_copy(doc, ep_id)
                else:
                    validate_transcript(doc, duration_s)
                jev = json.loads((dirs["jev"] / f"{ep_id}.json").read_text("utf-8"))
                res = bn.name_episode(ep_id, doc["segments"], jev, namer, threshold=threshold,
                                      context_windows=context_windows,
                                      sub_window_s=sub_window_s)
                if res is None:
                    raise ValueError("jev/<ep>.json is not a Jev result")
            except Exception as exc:
                why = f"{type(exc).__name__}: {exc}"[:500]
                record(ep_id, started, t0, cpu0, "error", why, f"failed: {why}"[:500])
                rep["failed"] += 1
                log(f"[name-backfill] {i}/{len(todo)} {ep_id} FAILED: {why}")
                continue
            res = {**res, "named_at": utcnow()}
            with bn.names_lock(dirs["names"]):
                raced = out.exists()  # another backfill or a rename got here first
                if not raced:
                    atomic_write_json(out, res)
            if raced:
                continue
            record(ep_id, started, t0, cpu0, "ok", None, "ok (backfill)")
            spans = res["spans"]
            rep["named"] += 1
            rep["spans"] += len(spans)
            rep["spans_named"] += sum(1 for sp in spans if sp["brand"])
            rep["spans_needs_review"] += sum(1 for sp in spans if sp["needs_review"])
            log(f"[name-backfill] {i}/{len(todo)} {ep_id}: " + (", ".join(
                f"{sp['brand'] or '?'}{'?' if sp['needs_review'] else ''}" for sp in spans)
                or "no flagged windows"))
        return rep
    finally:
        con.close()


# ---------------------------------------------------------------- diarize backfill

def _speakers_current(dirs, row) -> bool:
    """The episode has a valid speakers file from the pinned revision and its audio."""
    doc = _read_json(dirs["speakers"] / f"{row['episode_id']}.json")
    try:
        dz.validate_speakers(doc, row["episode_id"], row["duration_s"])
    except ValueError:
        return False
    return dz.is_current(doc, row["audio_sha256"])


def diarize_backfill(corpus, *, cmd: list, device: str = "mps",
                     mem_cap_mb: float = DIARIZE_MEM_CAP_MB, window_s: float = dz.WINDOW_S,
                     overlap_s: float = dz.OVERLAP_S, threads: int = 4,
                     ffmpeg: str | None = None, max_episodes: int | None = None,
                     log=print) -> dict:
    """Diarize the episodes that still hold their full audio on this host
    and have no current speakers/<ep>.json (missing, invalid, or made by
    another model revision or from other audio): any stage but dead (done
    episodes keep audio only under --keep-audio). Episodes whose audio is
    gone are counted (`no_audio`) and left alone: their evidence clips (a
    few minutes cut around ad spans) cannot tell whether a voice is heard
    elsewhere in the episode, so episode-level speaker identity needs the
    full audio.

    Takes no leases: the child only reads the audio and speakers/<ep>.json
    is written atomically (a worker reaching the diarize stage later reuses
    it). Idempotent: a rerun picks up exactly the episodes still missing.
    Each episode is a stage_runs row (stage diarize, worker
    diarize-backfill); diarize_status is set on rows nobody holds."""
    corpus = Path(corpus)
    dirs = corpus_dirs(corpus)
    ffmpeg = ffmpeg or stt.ffmpeg_path()
    con = connect(corpus)
    host = socket.gethostname()
    try:
        ensure_schema(con)
        tiers = tiers_running(con, "diarize")
        rows = [dict(r) for r in con.execute(
            "SELECT episode_id, audio_path, audio_sha256, duration_s FROM episodes"
            " WHERE stage NOT IN ('dead', 'fetch') AND tier IN (%s) ORDER BY episode_id"
            % ",".join("?" * len(tiers)), tiers)]
        todo = [r for r in rows if not _speakers_current(dirs, r)]
        have = [r for r in todo if r["audio_path"] and os.path.exists(r["audio_path"])]
        rep = {"episodes_without_speakers": len(todo), "no_audio": len(todo) - len(have),
               "diarized": 0, "failed": 0, "audio_s": 0.0, "wall_s": 0.0,
               "peak_rss_mb": 0.0}
        if rep["no_audio"]:
            log(f"[diarize-backfill] {rep['no_audio']} episodes without speakers have no audio "
                "on this host; clips are too short for episode-level speaker identity, so "
                "they are skipped")
        dirs["speakers"].mkdir(exist_ok=True)
        for i, r in enumerate(have[:max_episodes] if max_episodes else have, 1):
            ep_id = r["episode_id"]
            final = dirs["speakers"] / f"{ep_id}.json"
            if _speakers_current(dirs, r):   # a worker diarized it meanwhile
                continue
            out = dirs["work"] / f"{ep_id}.backfill-{os.getpid()}.speakers.json"
            log_path = out.with_suffix(".log")
            started = utcnow()
            try:
                res = supervise(diarize_command(cmd, r, ep_id, out, device=device,
                                                window_s=window_s, overlap_s=overlap_s,
                                                threads=threads, ffmpeg=ffmpeg),
                                mem_cap_mb=mem_cap_mb, timeout_s=diarize_timeout(r["duration_s"]),
                                log_path=log_path)
                why = None
                if res["outcome"] != "ok":
                    why = (f"{res['outcome']}: rc={res['returncode']} "
                           f"peak_rss_mb={res['peak_rss_mb']} {res['log_tail'][-300:]}")
                else:
                    try:
                        doc = json.loads(out.read_text(encoding="utf-8"))
                        dz.validate_speakers(doc, ep_id, r["duration_s"])
                        os.replace(out, final)
                        acq._fsync_dir(final.parent)
                    except (OSError, ValueError) as exc:
                        why = f"error: invalid speakers doc: {exc}"
            finally:
                out.unlink(missing_ok=True)
                log_path.unlink(missing_ok=True)
            status = "ok (backfill)" if why is None else f"failed: {why}"[:500]
            with _Tx(con):
                con.execute("INSERT INTO stage_runs (episode_id, stage, attempt, worker, host,"
                            " started_at, finished_at, wall_s, cpu_s, audio_s, peak_rss_mb,"
                            " outcome, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (ep_id, "diarize", 1, "diarize-backfill", host, started, utcnow(),
                             res["wall_s"], res["cpu_s"], r["duration_s"], res["peak_rss_mb"],
                             "ok" if why is None else
                             (res["outcome"] if res["outcome"] != "ok" else "error"), why))
                con.execute("UPDATE episodes SET diarize_status=?, updated_at=? WHERE"
                            " episode_id=? AND state='ready'", (status, utcnow(), ep_id))
            rep["wall_s"] += res["wall_s"]
            rep["peak_rss_mb"] = max(rep["peak_rss_mb"], res["peak_rss_mb"])
            if why is None:
                rep["diarized"] += 1
                rep["audio_s"] += r["duration_s"] or 0.0
                log(f"[diarize-backfill] {i}/{len(have)} {ep_id}: {len(doc['speakers'])} "
                    f"speakers in {res['wall_s']:.0f}s, peak RSS {res['peak_rss_mb']:.0f} MB")
            else:
                rep["failed"] += 1
                log(f"[diarize-backfill] {i}/{len(have)} {ep_id} FAILED: {why[:300]}")
        rep["audio_s"], rep["wall_s"] = round(rep["audio_s"], 1), round(rep["wall_s"], 1)
        return rep
    finally:
        con.close()
