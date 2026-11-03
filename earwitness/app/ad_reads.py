"""Customer ads from corpus loads (validation plan A2, A3, A5).

One customer ad per real read. A workspace loaded from a corpus export with
named reads (the active `named-reads-output` source version) shows, as ads,
its active named reads that are not flagged self-promo. Rule candidates and
Jev windows that overlap a named read are its supporting evidence (linked
by stable record keys in ad_read_links); lone rule candidates and unnamed
Jev windows wait in the operator review list, and an operator who confirms
one (review judgment "ad") makes it a customer ad. Rows that no corpus load
produced (uploads, agent observations, hand-made rows) keep the earlier
rule: every active row that is not rejected is an ad. A workspace without
named reads keeps that rule for every row.

The side tables here are rebuilt on every load from the export (and by
`upgrade` from the stored source versions). They hold only load-time facts:
the current namer suggestion, display spelling, self-promo flag, display
bounds, clip, screening status and the pinned quote text. Review state and
brand associations are read live, so a review or a direct SQL write is
visible at once and nothing here can go stale against human work.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS ad_reads (
  id INTEGER PRIMARY KEY,
  evidence_id TEXT NOT NULL UNIQUE,
  record_key TEXT NOT NULL,
  episode_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  review_list INTEGER NOT NULL DEFAULT 0,
  evidence_revision INTEGER,
  start_s REAL, end_s REAL,
  display_start_s REAL, display_end_s REAL,
  self_promo INTEGER NOT NULL DEFAULT 0,
  needs_review INTEGER NOT NULL DEFAULT 0,
  confidence_score REAL,
  clip_offset_s REAL,
  confidence TEXT,
  brand_suggested TEXT,
  clip_id TEXT,
  self_promo_reason TEXT,
  candidates_json TEXT NOT NULL DEFAULT '[]',
  quote TEXT NOT NULL DEFAULT '',
  search_folded TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_evidence_customer_light
  ON evidence (id, episode_id, source_version_id, transcript_version_id, source_record_key,
               orig_start_s, orig_end_s, revision);
CREATE INDEX IF NOT EXISTS idx_source_states_current
  ON source_states (workspace_id, kind, record_id, publication_order, id);
-- Lookups a reload runs once per record (validation plan A4): rows by
-- stable record key, publications by record, source files by digest.
CREATE INDEX IF NOT EXISTS idx_evidence_record_key ON evidence (source_record_key, episode_id);
CREATE INDEX IF NOT EXISTS idx_source_states_kind_record ON source_states (kind, record_id);
CREATE INDEX IF NOT EXISTS idx_source_files_digest ON source_files (sha256, path);
CREATE INDEX IF NOT EXISTS idx_ad_reads_episode ON ad_reads(episode_id);
CREATE INDEX IF NOT EXISTS idx_ad_reads_key ON ad_reads(record_key);
-- The operator review queue (corrections.queue) reads only these columns
-- of ad_reads, never its quote/search text: a cold queue stays in index pages.
CREATE INDEX IF NOT EXISTS idx_ad_reads_review ON ad_reads(kind, needs_review, self_promo,
  evidence_id, brand_suggested, display_start_s, display_end_s);
CREATE INDEX IF NOT EXISTS idx_ad_reads_named_brand ON ad_reads(kind, self_promo, brand_suggested);
CREATE TABLE IF NOT EXISTS ad_brand_aliases (
  raw_key TEXT PRIMARY KEY,
  canonical_key TEXT NOT NULL,
  display TEXT,
  display_basis TEXT NOT NULL DEFAULT 'none'
);
CREATE INDEX IF NOT EXISTS idx_ad_brand_aliases_canonical ON ad_brand_aliases(canonical_key);
CREATE TABLE IF NOT EXISTS ad_read_links (
  read_key TEXT NOT NULL,
  support_key TEXT NOT NULL,
  PRIMARY KEY (read_key, support_key)
);
CREATE INDEX IF NOT EXISTS idx_ad_read_links_support ON ad_read_links(support_key);
CREATE TABLE IF NOT EXISTS ad_clips (
  id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL,
  rel TEXT NOT NULL,
  path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  start_s REAL NOT NULL,
  end_s REAL NOT NULL,
  transcript_version_id INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ad_clips_episode ON ad_clips(episode_id);
CREATE TABLE IF NOT EXISTS episode_screening (
  episode_id TEXT PRIMARY KEY,
  jev_status TEXT,
  name_status TEXT,
  screen_status TEXT,
  clip_status TEXT,
  pipeline_status TEXT,
  jev_screened INTEGER NOT NULL DEFAULT 0,
  jev_windows INTEGER NOT NULL DEFAULT 0,
  named_reads INTEGER NOT NULL DEFAULT 0,
  self_promo_reads INTEGER NOT NULL DEFAULT 0,
  screened_no_ads INTEGER NOT NULL DEFAULT 0,
  stopped_partway INTEGER NOT NULL DEFAULT 0,
  coverage_tier TEXT NOT NULL DEFAULT 'full'
);
CREATE TABLE IF NOT EXISTS ad_reads_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

TABLES = ("ad_reads", "ad_brand_aliases", "ad_read_links", "ad_clips", "episode_screening",
          "ad_reads_meta")
# ad_reads_meta keys: BUILT is written in the transaction that finishes a
# full build (rebuild), so tables that schema setup created empty, or a
# build that never committed, never count as built. FTS_POPULATED marks the
# ads text index as holding every ad_reads row.
BUILT = "built"
FTS_POPULATED = "fts_populated"
# CLIPGUARD R8: the named reads ad_rules R8 judged clean ("clip_hidden"
# false on the loaded row), as JSON {"evidence_ids": [...]}: an allowlist.
# A read R8 marked, or never judged (rules rows stored before R8, the rules
# off, a read without a transcript), is not on it. Every build writes it. A
# meta row, not a column: the pinned workspace schema stays as it is. Only
# the web's clip guard reads it (clip_guard_on).
CLIP_SHOWN = "clip_shown"
# DETBUILD: the display bounds the ad rules (R6 split, R7 end trim) gave
# named reads, as JSON {"bounds": {evidence_id: [lo, hi]}}; empty when none.
# Written by a build only when some read has bounds, and deleted otherwise,
# so a load with the DET rules off leaves no row (rule_bounds: None). Only
# ad_breaks.ads reads it (rule_bounds), to chain a read by what it shows; the
# pinned workspace schema stays as it is.
DISPLAY_RULE = "display_rule"
# DETBUILD: the rows the ad rules add by R6 (a split part) and R9 (an address
# row) get no clip (ad_rules.R6_SOURCE, R9_SOURCE): every stored clip was cut
# for a whole detected read, so the file a part lies in holds the keeper's ad.
NO_CLIP_SOURCES = ("ad_rules:R6", "ad_rules:R9")
# The same rows for the record media route, which falls back to a span
# lookup when the clip guard is off: the evidence ids of those rows, as JSON
# {"evidence_ids": [...]} sorted. Written by a build only when non-empty and
# deleted otherwise (no_clip). media.clip_for_publication and
# media.excerpt_clip_for play no clip for an id on it.
NO_CLIP = "no_clip"
# The web's clip guard: on, the clip lookups refuse every read that is not
# on the list (every clip when the list is missing or unreadable); off
# (missing or empty), they are the pre-R8 ones and never read the list.
# The web reads it once at start (asgi.create_app -> set_clip_guard); loads
# never read it.
CLIP_GUARD_ENV = "EARWITNESS_AD_RULES_CLIP_GUARD"
_CLIP_GUARD = [False]


def clip_guard_setting(env) -> bool:
    """CLIP_GUARD_ENV in `env`: on is True; off, missing or empty is False;
    any other value raises ValueError (naming the setting, not the value)."""
    value = str(env.get(CLIP_GUARD_ENV) or "").strip().lower()
    if value not in ("", "on", "off"):
        raise ValueError(f"{CLIP_GUARD_ENV} must be on, off or unset")
    return value == "on"


def set_clip_guard(on: bool) -> None:
    """The web's clip guard for this process (create_app, at start)."""
    _CLIP_GUARD[0] = bool(on)


def clip_guard_on() -> bool:
    return _CLIP_GUARD[0]
NAMED_SOURCE_KEY = "named-reads-output"
DISPLAY_BASES = ("brand_list", "transcript_cased", "none")
_CLIP_NAME = re.compile(r"(\d+)-(\d+)\.mp3$")


FTS = "ad_reads_fts"


def ensure_schema(con: sqlite3.Connection) -> None:
    """Create the side tables (empty: not built) and the ads text index. An
    index created after rows exist, or never marked populated, is filled
    from ad_reads here, so it never answers from a partial index."""
    _create_schema(con)
    # Last: a new or rebuilt guarded table gets its view-state guards, or
    # view_state_key stays None and no research read is reused (UPD3-SLOW).
    from . import db as _db
    _db.ensure_view_state_guards(con)


def _create_schema(con: sqlite3.Connection) -> None:
    # Statement by statement: executescript would commit the caller's
    # transaction (a load runs this inside its own).
    for statement in SCHEMA.split(";"):
        if statement.strip():
            con.execute(statement)
    # A side table made before coverage tiers gets the column; the next
    # rebuild fills it from the export.
    if "coverage_tier" not in {r[1] for r in con.execute("PRAGMA table_info(episode_screening)")}:
        con.execute("ALTER TABLE episode_screening"
                    " ADD COLUMN coverage_tier TEXT NOT NULL DEFAULT 'full'")
    # A clip plays only for evidence on the transcript version it was cut
    # against (audio clips design 4.5); a table made before that gets the
    # column, and the next rebuild binds kept rows to their episode's copy.
    if "transcript_version_id" not in {r[1] for r in con.execute("PRAGMA table_info(ad_clips)")}:
        con.execute("ALTER TABLE ad_clips"
                    " ADD COLUMN transcript_version_id INTEGER NOT NULL DEFAULT 0")
    from . import corrections  # the operator's rules (human work, never rebuilt)
    corrections.ensure_schema(con)
    try:  # candidate index for ads text search; absent = direct scan
        con.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS} USING fts5(search_folded,"
                    " content='ad_reads', content_rowid='id', tokenize='trigram case_sensitive 1')")
    except sqlite3.OperationalError:
        return
    if not _meta(con, FTS_POPULATED):
        con.execute(f"INSERT INTO {FTS} ({FTS}) VALUES ('rebuild')")
        _set_meta(con, FTS_POPULATED, "1")


# (raw meta value, its parsed set): one tuple, replaced whole and read once
# into a local, so a reader never pairs one value with another's set. Keyed
# by the value itself, so workspaces with different lists never share one.
_SHOWN_PARSED: tuple = (None, None)


def _parse_shown(raw: str):
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError):
        return None
    ids = doc.get("evidence_ids") if isinstance(doc, dict) else None
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        return None
    return frozenset(ids)


def shown_clips(con: sqlite3.Connection):
    """The evidence ids the last build judged clean (CLIP_SHOWN), or None
    when unknown: no side tables, no meta row (a build from before R8) or
    an unreadable one."""
    global _SHOWN_PARSED
    if not _tables_exist(con):
        return None
    raw = _meta(con, CLIP_SHOWN)
    if raw is None:
        return None
    cached = _SHOWN_PARSED
    if cached[0] == raw:
        return cached[1]
    parsed = _parse_shown(raw)
    _SHOWN_PARSED = (raw, parsed)
    return parsed


_NO_CLIP_PARSED: tuple = (None, None)


def no_clip(con: sqlite3.Connection, evidence_id) -> bool:
    """Whether the last build gave this evidence no clip (NO_CLIP): the row
    is an R6 part or an R9 row. False when the list is missing, and also
    when it is unreadable: failing closed would hide every clip, and the
    list only ever takes clips away from rows that never had one."""
    global _NO_CLIP_PARSED
    if not _tables_exist(con):
        return False
    raw = _meta(con, NO_CLIP)
    if raw is None:
        return False
    cached = _NO_CLIP_PARSED
    if cached[0] != raw:
        cached = _NO_CLIP_PARSED = (raw, _parse_shown(raw))
    return cached[1] is not None and evidence_id in cached[1]


def no_clip_source(con: sqlite3.Connection, evidence_id) -> bool:
    """Whether this evidence row is an R6 part or an R9 row by its own
    source (NO_CLIP_SOURCES: the candidate's "source", kept in the row's
    attrs; the evidence.source column only says "detector"), whatever rules
    version is active and whether or not the NO_CLIP list is there. False
    when the row or its attrs cannot be read (no_clip's list then still
    applies)."""
    try:
        row = con.execute("SELECT attrs FROM evidence WHERE id=?", (evidence_id,)).fetchone()
        attrs = json.loads(row[0]) if row is not None else None
    except (sqlite3.Error, ValueError, TypeError):
        return False
    return isinstance(attrs, dict) and attrs.get("source") in NO_CLIP_SOURCES


_BOUNDS_PARSED: tuple = (None, None)


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def _parse_bounds(raw: str):
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError):
        return None
    found = doc.get("bounds") if isinstance(doc, dict) else None
    if not isinstance(found, dict):
        return None
    out = {}
    for key, pair in found.items():
        if not isinstance(pair, list) or len(pair) != 2 or not all(_number(v) for v in pair) \
                or not pair[0] < pair[1]:
            return None
        out[key] = [float(pair[0]), float(pair[1])]
    return out


def rule_bounds(con: sqlite3.Connection):
    """{evidence id: [lo, hi]} the last build gave reads as rule display
    bounds (DISPLAY_RULE), or None when unknown: no side tables, no meta row
    or an unreadable one (no read is then chained by rule bounds). The
    caller must not mutate it."""
    global _BOUNDS_PARSED
    if not _tables_exist(con):
        return None
    raw = _meta(con, DISPLAY_RULE)
    if raw is None:
        return None
    cached = _BOUNDS_PARSED
    if cached[0] == raw:
        return cached[1]
    parsed = _parse_bounds(raw)
    _BOUNDS_PARSED = (raw, parsed)
    return parsed


def _rule_display(read: dict):
    """(lo, hi) a rules row shows (R6 keeper, R7 trim): the numeric display
    bounds of a read with an R6 or R7 entry in its provenance, inside its
    detected span; else None."""
    prov = read.get("provenance") if isinstance(read.get("provenance"), dict) else {}
    entries = prov.get("ad_rules") if isinstance(prov.get("ad_rules"), list) else []
    if not any(isinstance(e, dict) and e.get("rule") in ("R6", "R7") for e in entries):
        return None
    lo, hi = read.get("display_start_s"), read.get("display_end_s")
    start, end = read.get("start_s"), read.get("end_s")
    if not all(_number(v) for v in (lo, hi, start, end)) or not lo < hi \
            or not start <= lo or not hi <= end:
        return None
    return float(lo), float(hi)


def clip_hidden(con: sqlite3.Connection, evidence_id, shown=False) -> bool:
    """Whether the clip guard refuses this evidence's clip (CLIPGUARD R8;
    callers ask only while clip_guard_on()): it is not on the last build's
    clean list (shown_clips): R8 marked its read, never judged it, or it is
    no named read; or the list is missing or unreadable (fail closed).
    Evidence with no ad_reads row takes the state of the reads the build has
    under its record key only when they are on the same transcript copy:
    R8 judged them on that copy, so a publication on an earlier copy (its
    clip cut from the old audio) is refused (CGS-R2-01). `shown`:
    shown_clips(con), when the caller has it."""
    shown = shown_clips(con) if shown is False else shown
    if shown is None:
        return True
    if evidence_id in shown:
        return False
    if con.execute("SELECT 1 FROM ad_reads WHERE evidence_id=?", (evidence_id,)).fetchone():
        return True
    rows = con.execute("SELECT ar.evidence_id FROM evidence e JOIN ad_reads ar"
                       " ON ar.record_key=e.source_record_key AND ar.episode_id=e.episode_id"
                       " JOIN evidence cur ON cur.id=ar.evidence_id"
                       " AND cur.transcript_version_id IS e.transcript_version_id"
                       " WHERE e.id=?", (evidence_id,)).fetchall()
    return not rows or any(eid not in shown for (eid,) in rows)


def episode_tier(row) -> str:
    """The coverage tier an export episode row carries. Only rows of a tier
    other than full have the key, so an older export reads as full."""
    tier = row.get("coverage_tier") if isinstance(row, dict) else None
    return tier if isinstance(tier, str) and tier else "full"


def transcript_only(row, jev_screened) -> bool:
    """A transcript-tier episode with no Jev screening: transcripts only. One
    whose export row carries Jev results (jev_status ok) was screened and
    reads like a full-tier episode."""
    screened = isinstance(row.get("jev_status"), str) and row["jev_status"].startswith("ok") \
        and str(row.get("ep_id")) in jev_screened
    return episode_tier(row) == "transcript" and not screened


def _meta(con: sqlite3.Connection, key: str):
    row = con.execute("SELECT value FROM ad_reads_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _del_meta(con: sqlite3.Connection, key: str) -> None:
    con.execute("DELETE FROM ad_reads_meta WHERE key=?", (key,))


def _set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT INTO ad_reads_meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                " WHERE value IS NOT excluded.value", (key, value))


def _tables_exist(con: sqlite3.Connection) -> bool:
    names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN (%s)"
        % ",".join("?" * len(TABLES)), TABLES)}
    return len(names) == len(TABLES)


def fts_present(con: sqlite3.Connection) -> bool:
    """The ads text index exists and holds every ad_reads row."""
    return con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (FTS,)).fetchone() is not None \
        and _tables_exist(con) and _meta(con, FTS_POPULATED) is not None


def tables_present(con: sqlite3.Connection) -> bool:
    """The side tables exist and a full build committed (BUILT). Tables that
    schema setup (status, backup, reindex, a migration) created empty, or
    an interrupted build, read as absent: callers keep the rule for
    workspaces without them."""
    return _tables_exist(con) and _meta(con, BUILT) is not None


# Evidence attrs that name a read's brand: the namer's guess, its spelling,
# Option A fields and its candidates (provenance). A customer never gets
# them while the brand is under review (brand_review_ids).
BRAND_GUESS_ATTRS = ("brand", "brand_spoken", "brand_raw", "brand_url", "brand_code",
                     "brand_canonical", "canon_reason", "brand_display", "brand_display_basis",
                     "product", "network", "provenance")


def _named_span_class(record_key: str):
    """(episode prefix, second-brand suffix) of a named read's record key
    (named:<ep>.named.<lo>-<hi>[.b-<brand>]); None for any other key."""
    head, sep, tail = (record_key or "").partition(".named.")
    if not sep or not head.startswith("named:"):
        return None
    return head + sep, tail.partition(".")[2]


def _chunks(values, size=500):
    values = list(values)
    return [values[at:at + size] for at in range(0, len(values), size)]


def brand_review_ids(con: sqlite3.Connection, ids=None, *, unconfirmed: bool = True) -> set:
    """The evidence ids (of `ids`, else of the workspace) whose brand is
    under review for customers: exactly the needs-review reads of the
    operator review queue (corrections.queue): a named read of the last
    load the namer flagged (needs_review), not self-promotion, not judged
    not-an-ad, with no confirmed brand. Customers see such a read as an ad
    with no brand; operators still see the guess.
    The same guess also rides on: the rule candidates and Jev windows that
    support such a read (ad_read_links), and the earlier (retired) rows of
    all of these: rows with the same record key, and retired named rows of
    the episode whose span overlaps a hidden read (a re-name that moved the
    span gave the read a new key), and retired rule candidates and Jev
    windows of the episode overlapping it (a re-detection renumbers them),
    and the retired rows whose evidence_lineage successor is hidden (a read
    and its candidate that moved past the old span). Each stays hidden until
    a person confirms a brand on that row. A workspace without the side tables has
    no queue, so nothing is hidden there.
    unconfirmed=False: also reads a person confirmed since (for a pinned
    publication, whose own associations decide;
    customer.pinned_brand_under_review). With `ids`, only the episodes of
    those rows are read."""
    if not tables_present(con):
        return set()
    episodes = None
    if ids is not None:
        ids = list(ids)
        if not ids:
            return set()
        episodes = set()
        for chunk in _chunks(ids):
            episodes.update(r[0] for r in con.execute(
                "SELECT DISTINCT episode_id FROM evidence WHERE id IN (%s)"
                % ",".join("?" * len(chunk)), chunk))
        if not episodes:
            return set()

    def confirmed(evidence_ids) -> set:
        if not unconfirmed:
            return set()
        out = set()
        for chunk in _chunks(evidence_ids):
            out.update(r[0] for r in con.execute(
                "SELECT DISTINCT evidence_id FROM brand_assocs WHERE basis='confirmed'"
                " AND evidence_id IN (%s)" % ",".join("?" * len(chunk)), chunk))
        return out

    where = ("ar.kind='named' AND ar.needs_review=1 AND ar.self_promo=0"
             " AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.evidence_id=ar.evidence_id"
             " AND r.judgment='not_ad')")
    sql = "SELECT ar.evidence_id, ar.record_key, ar.episode_id, ar.start_s, ar.end_s FROM ad_reads ar"
    flagged = []
    for chunk in ([None] if episodes is None else _chunks(episodes)):
        flagged += con.execute(sql + " WHERE " + where + ("" if chunk is None else
                               " AND ar.episode_id IN (%s)" % ",".join("?" * len(chunk))),
                               chunk or ()).fetchall()
    gone = confirmed([r[0] for r in flagged])
    reads = [r for r in flagged if r[0] not in gone]
    out = {r[0] for r in reads}
    keys = {r[1] for r in reads}
    for chunk in _chunks(list(keys)):
        support = con.execute(
            "SELECT ar.evidence_id, ar.record_key FROM ad_read_links l"
            " JOIN ad_reads ar ON ar.record_key=l.support_key"
            " WHERE l.read_key IN (%s) AND ar.kind<>'named'" % ",".join("?" * len(chunk)),
            chunk).fetchall()
        gone = confirmed([r[0] for r in support])
        for eid, key in support:
            if eid not in gone:
                out.add(eid)
                keys.add(key)
    # Retired rows (no ad_reads row) carrying the same guess.
    spans: dict = {}
    episode_spans: dict = {}  # retired rule candidates / Jev windows (a re-detection renumbers them)
    for _eid, key, episode, lo, hi in reads:
        cls = _named_span_class(key)
        if cls is not None and lo is not None and hi is not None:
            spans.setdefault(cls, []).append((float(lo), float(hi)))
            episode_spans.setdefault(episode, []).append((float(lo), float(hi)))
    cols = ("SELECT e.id, e.source_record_key, e.orig_start_s, e.orig_end_s, e.episode_id FROM evidence e"
            " WHERE NOT EXISTS (SELECT 1 FROM ad_reads x WHERE x.evidence_id=e.id) AND ")
    rows = []
    if ids is not None:
        for chunk in _chunks(set(ids) - out):
            rows += con.execute(cols + "e.id IN (%s)" % ",".join("?" * len(chunk)), chunk).fetchall()
    else:
        for chunk in _chunks(list(keys)):
            rows += con.execute(cols + "e.source_record_key IN (%s)"
                                % ",".join("?" * len(chunk)), chunk).fetchall()
        prefixes = {cls[0]: "/" for cls in spans}
        prefixes.update({f"det:{ep}.": "/" for ep in episode_spans})
        prefixes.update({f"jev:{ep}:": ";" for ep in episode_spans})
        for prefix, after in prefixes.items():
            rows += con.execute(cols + "e.source_record_key >= ? AND e.source_record_key < ?",
                                (prefix, prefix[:-1] + after)).fetchall()
    candidates = {}
    for eid, key, lo, hi, episode in rows:
        if eid in out or lo is None or hi is None:
            if eid not in out and key in keys:
                candidates[eid] = True
            continue
        hit = key in keys
        cls = _named_span_class(key)
        if not hit and cls in spans:
            hit = any(float(lo) < b and a < float(hi) for a, b in spans[cls])
        elif not hit and cls is None and (key or "").startswith(
                (f"det:{episode}.", f"jev:{episode}:")):
            hit = any(float(lo) < b and a < float(hi) for a, b in episode_spans.get(episode, ()))
        if hit:
            candidates[eid] = True
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='evidence_lineage'").fetchone():
        # a moved row whose successor is hidden
        link = ("SELECT l.old_id, l.new_id FROM evidence_lineage l WHERE NOT EXISTS"
                " (SELECT 1 FROM ad_reads x WHERE x.evidence_id=l.old_id) AND ")
        for chunk in _chunks(set(ids) - out if ids is not None else out):
            col = "l.old_id" if ids is not None else "l.new_id"
            for old_id, new_id in con.execute(link + col + " IN (%s)" % ",".join("?" * len(chunk)), chunk):
                if new_id in out:
                    candidates[old_id] = True
    out.update(set(candidates) - confirmed(list(candidates)))
    return out if ids is None else out & set(ids)


def without_brand_guess(attrs):
    """Evidence attrs without the keys that name the brand (BRAND_GUESS_ATTRS)."""
    if not isinstance(attrs, dict):
        return attrs
    return {k: v for k, v in attrs.items() if k not in BRAND_GUESS_ATTRS}


def fold(text) -> str:
    """The customer brand/text key (customer.normalize_token)."""
    from .customer import normalize_token
    return normalize_token(text or "")


def brand_key(text) -> str:
    """Brand key: folded, trimmed, inner whitespace collapsed ("mint  mobile"
    is "mint mobile")."""
    return " ".join(fold(text or "").split())


def register_functions(con: sqlite3.Connection) -> None:
    con.create_function("ew_brand_key", 1, brand_key, deterministic=True)


def named_mode(con: sqlite3.Connection) -> bool:
    """True when the workspace holds an active named-reads load and its
    side tables are built."""
    return con.execute("SELECT 1 FROM source_versions WHERE source_key=? AND is_active=1"
                       " LIMIT 1", (NAMED_SOURCE_KEY,)).fetchone() is not None \
        and tables_present(con)


# -- Load-time build -------------------------------------------------------------

def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    return (a_start is not None and a_end is not None and b_start is not None
            and b_end is not None and a_start < b_end and a_end > b_start)


def _clip_index(export_root: Path | None, episodes: list[dict]) -> dict[str, list[dict]]:
    """Clips listed by the export, by episode: file, interval, sha256.
    The interval comes from clips/<ep>/index.json, else the file name."""
    out: dict[str, list[dict]] = {}
    if export_root is None:
        return out
    for ep in episodes:
        ep_id = str(ep.get("ep_id") or "")
        rels = [r for r in (ep.get("clips") or []) if isinstance(r, str)]
        if not ep_id or not rels:
            continue
        index = {}
        try:
            doc = json.loads((export_root / "clips" / ep_id / "index.json").read_text())
            for entry in doc.get("clips") or []:
                if isinstance(entry, dict) and entry.get("file"):
                    index[f"clips/{ep_id}/{entry['file']}"] = entry
        except (OSError, ValueError, AttributeError):
            index = {}
        for rel in rels:
            entry = index.get(rel) or {}
            start, end = entry.get("start_s"), entry.get("end_s")
            m = _CLIP_NAME.search(rel)
            if not isinstance(start, (int, float)) and m:
                start = int(m.group(1)) / 1000.0
            if not isinstance(end, (int, float)) and m:
                end = int(m.group(2)) / 1000.0
            if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
                continue
            out.setdefault(ep_id, []).append({"rel": rel, "start_s": float(start),
                                              "end_s": float(end),
                                              "sha256": entry.get("sha256")})
    return out


def _verified_clip(export_root: Path, clip: dict) -> dict | None:
    """Register a clip in place: the file must exist and match the export's
    sha256 (index.json). Clips without a listed digest are hashed and kept
    under their own digest; a mismatch is refused."""
    path = (export_root / clip["rel"]).resolve()
    try:
        if export_root.resolve() not in path.parents or not path.is_file():
            return None
        data = path.read_bytes()
    except OSError:
        return None
    digest = hashlib.sha256(data).hexdigest()
    if clip.get("sha256") and clip["sha256"] != digest:
        return None
    return {"path": str(path), "sha256": digest, "bytes": len(data)}


def _pick_all(clips: list[dict], start, end, version) -> list[dict]:
    """The clips on the read's transcript version that hold the whole read.
    A clip that only overlaps a read is never linked to it."""
    if start is None or end is None:
        return []
    return [c for c in clips if c["transcript_version_id"] == version
            and c["start_s"] <= start and end <= c["end_s"]]


def _pick_clip(clips: list[dict], start, end, version) -> dict | None:
    """The shortest of them."""
    containing = _pick_all(clips, start, end, version)
    return min(containing, key=lambda c: (c["end_s"] - c["start_s"], c["rel"])) \
        if containing else None


def export_aliases(named: list) -> dict:
    """raw -> (raw, canonical, basis, display) from the load's named reads
    (after the stoplist): the export's canonical key and display spelling,
    the first read with a spelling winning. A read the stoplist moved to its
    next candidate (`_fell_through`, no canonical) decides only a spelling
    no other read has: the export gives every read heard one way the same
    company, and read order never takes it away."""
    aliases: dict[str, tuple] = {}
    heard = {brand_key(r.get("brand_spoken") or r.get("brand")) for r in named
             if isinstance(r, dict) and not r.get("_fell_through")}
    for read in named:
        if not isinstance(read, dict):
            continue
        raw = brand_key(read.get("brand_spoken") or read.get("brand"))
        if not raw or (read.get("_fell_through") and raw in heard):
            continue
        canonical = brand_key(read.get("brand_canonical")) or raw
        basis = read.get("brand_display_basis")
        display = read.get("brand_display") if isinstance(read.get("brand_display"), str) else None
        if basis not in DISPLAY_BASES or (basis == "none") != (display is None):
            basis, display = ("none", None) if display is None else ("brand_list", display)
        known = aliases.get(raw)
        if known is None or (known[2] == "none" and basis != "none"):
            aliases[raw] = (raw, canonical, basis, display)
    return aliases


def with_transcript_spelling(entry: tuple, texts) -> tuple:
    """An alias row without a known spelling takes the one its read
    transcripts give (transcript_spelling), if any."""
    if entry[2] != "none":
        return entry
    cased = transcript_spelling(entry[0], texts)
    return (entry[0], entry[1], "transcript_cased", cased) if cased else entry


def named_columns(read: dict, rules: dict) -> tuple:
    """The rule-dependent ad_reads columns of one named read, after the
    stoplist and self-promo rules: brand_suggested, self_promo,
    self_promo_reason, needs_review, candidates_json. A load and every
    operator rule (corrections) write exactly these."""
    raw = brand_key(read.get("brand_spoken") or read.get("brand"))
    provenance = read.get("provenance") if isinstance(read.get("provenance"), dict) else {}
    candidates = provenance.get("candidates") if isinstance(provenance.get("candidates"), list) else []
    # An alias rule is the operator naming the read's brand, never for a
    # read that only fell through to that spelling.
    needs = int(bool(read.get("needs_review")) and (bool(read.get("_fell_through"))
                                                    or raw not in rules["alias"]))
    return (raw or None, int(bool(read.get("self_promo"))), read.get("self_promo_reason"),
            needs, json.dumps(candidates[:10]))


def det_brand(c: dict, rules: dict):
    """A rule candidate's brand pick: none once stoplisted."""
    raw = brand_key(c.get("brand_spoken"))
    return (raw or None) if raw not in rules["stoplist"] else None


# Parsed active source payloads, keyed by database file and source version
# id (an immutable identity): an operator rule re-reads the namer output
# without parsing it again. One entry; replaced when the version changes.
_PAYLOAD_MEMO: dict = {}


def loaded_named(con: sqlite3.Connection):
    """The named reads the active load used: the ad rules' rows when that
    load applied them (import_sources.AD_RULES_SOURCE_KEY), else the
    named-reads file as stored. None without a named-reads load."""
    from .import_sources import AD_RULES_SOURCE_KEY
    rows = active_payload(con, AD_RULES_SOURCE_KEY)
    return rows if isinstance(rows, list) else active_payload(con, NAMED_SOURCE_KEY)


def active_payload(con: sqlite3.Connection, key: str):
    row = con.execute("SELECT id FROM source_versions WHERE source_key=? AND is_active=1"
                      " ORDER BY id DESC LIMIT 1", (key,)).fetchone()
    if row is None:
        return None
    path = con.execute("PRAGMA database_list").fetchone()[2]
    memo_key = (path, key, row[0])
    if memo_key not in _PAYLOAD_MEMO:
        try:
            value = json.loads(con.execute("SELECT payload FROM source_versions WHERE id=?",
                                           (row[0],)).fetchone()[0])
        except (ValueError, TypeError):
            value = None
        for old in [k for k in _PAYLOAD_MEMO if k[:2] == memo_key[:2]]:
            del _PAYLOAD_MEMO[old]
        _PAYLOAD_MEMO[memo_key] = value
    return _PAYLOAD_MEMO[memo_key]


# Per payload list (the memoized one above): each named read's raw key and
# candidate keys, so a rule does not fold every read's spellings again.
_KEYS_MEMO: dict = {}


def _named_keys(named: list) -> list:
    hit = _KEYS_MEMO.get(id(named))
    if hit is None or hit[0] is not named:
        keys = []
        for read in named:
            if not isinstance(read, dict):
                keys.append(("", frozenset()))
                continue
            prov = read.get("provenance") if isinstance(read.get("provenance"), dict) else {}
            keys.append((brand_key(read.get("brand_spoken") or read.get("brand")),
                         frozenset(brand_key(c.get("brand")) for c in prov.get("candidates") or []
                                   if isinstance(c, dict))))
        _KEYS_MEMO.clear()
        hit = _KEYS_MEMO[id(named)] = (named, keys)
    return hit[1]


_INDEX_MEMO: dict = {}


def _named_index(named: list) -> dict:
    """candidate_id -> position in the memoized payload list."""
    hit = _INDEX_MEMO.get(id(named))
    if hit is None or hit[0] is not named:
        _INDEX_MEMO.clear()
        hit = _INDEX_MEMO[id(named)] = (named, {
            r.get("candidate_id"): i for i, r in enumerate(named) if isinstance(r, dict)})
    return hit[1]


def refresh_read_rules(con: sqlite3.Connection, evidence_id: str) -> bool:
    """One named read's rule columns (brand pick, self-promo, needs review,
    candidates) as a load gives them now: after a person reviewed it or
    cleared the review (a self-promo rule never stands over a reviewed
    read). Same transforms as refresh_rule_facts, on this read only;
    returns whether its row changed."""
    from . import corrections
    row = con.execute("SELECT record_key, episode_id, brand_suggested, self_promo,"
                      " self_promo_reason, needs_review, candidates_json FROM ad_reads"
                      " WHERE evidence_id=? AND kind='named'", (evidence_id,)).fetchone()
    named = loaded_named(con) if row is not None else None
    if not isinstance(named, list):
        return False
    at = _named_index(named).get(str(row["record_key"])[len("named:"):])
    if at is None:
        return False
    rules = corrections.load(con)
    title = con.execute("SELECT show_title FROM episodes WHERE id=?",
                        (str(named[at].get("ep_id")),)).fetchone()

    def canon(raw):
        hit = con.execute("SELECT canonical_key FROM ad_brand_aliases WHERE raw_key=?",
                          (brand_key(raw),)).fetchone()
        return hit[0] if hit else brand_key(raw)
    reviewed = evidence_id in corrections.reviewed_ids_of(con, [evidence_id])
    read = corrections.self_promo_reads(
        corrections.stoplist_reads([named[at]], rules), rules,
        {str(named[at].get("ep_id")): title[0] if title else None}, canon,
        skip={row["record_key"]} if reviewed else ())[0]
    cols = named_columns(read, rules)
    if tuple(row)[2:] == cols:
        return False
    con.execute("UPDATE ad_reads SET brand_suggested=?, self_promo=?, self_promo_reason=?,"
                " needs_review=?, candidates_json=? WHERE evidence_id=?", (*cols, evidence_id))
    return True


def refresh_rule_facts(con: sqlite3.Connection, actor: str, *, words=(), brands=()) -> dict:
    """Apply the current operator rules to the reads one rule touches,
    exactly as a load would (same transforms, same columns, same attrs and
    machine brands), so the next unchanged load changes nothing. Touched:
    named reads heard as (or with a candidate) in `words`, or whose brand
    (canonical) is in `brands`; rule candidates heard as `words`; the alias
    rows of every spelling involved. Rows a person worked on keep their
    attrs and brands (as on a load). Republished reads keep their tracker
    baseline membership (corrections.publish_keeping_trackers). Returns
    {"named": [(evidence_id, before, after) brand picks of changed reads],
    "promo": [evidence ids newly self-promo], "published": n}."""
    from . import corrections, import_sources
    words, brands = set(words), set(brands)
    named = loaded_named(con) or []
    named = named if isinstance(named, list) else []
    episodes = active_payload(con, "episodes.json") or []
    rules = corrections.load(con)
    trans = corrections.stoplist_reads(named, rules)
    aliases = corrections.alias_rows(rules, export_aliases(trans))
    canon = lambda raw: (aliases.get(brand_key(raw)) or (None, brand_key(raw)))[1]  # noqa: E731
    ids = dict(con.execute("SELECT record_key, evidence_id FROM ad_reads WHERE kind='named'"))
    reviewed = corrections.reviewed_ids(con)
    shows = {str(ep.get("ep_id")): ep.get("show_title") for ep in episodes if isinstance(ep, dict)}
    final = corrections.self_promo_reads(trans, rules, shows, canon,
                                         skip={k for k, e in ids.items() if e in reviewed})

    keys = _named_keys(named)

    def raw_of(i):  # the read's raw key now (a stoplisted pick moved on)
        now = final[i]
        return keys[i][0] if now is named[i] or not isinstance(now, dict) else \
            brand_key(now.get("brand_spoken") or now.get("brand"))

    def hit(i):
        orig, now = keys[i][0], raw_of(i)
        return bool(words & ({orig, now} | keys[i][1])) or bool(
            brands and brands & {canon(orig), canon(now)})
    sel = [i for i, orig in enumerate(named) if isinstance(orig, dict) and hit(i)]
    raws = set(words) | {r for i in sel for r in (keys[i][0], raw_of(i)) if r}
    # Alias rows of every spelling involved, as the load builds them.
    heard: dict[str, list] = {}
    keyed = {}
    for i, read in enumerate(final):
        if isinstance(read, dict) and raw_of(i) in raws:
            eid = ids.get(f"named:{read.get('candidate_id')}")
            if eid:
                keyed[eid] = raw_of(i)
    eids = list(keyed)
    for at in range(0, len(eids), 500):
        chunk = eids[at:at + 500]
        for eid, text in con.execute("SELECT evidence_id, quote FROM ad_reads WHERE evidence_id IN (%s)"
                                     % ",".join("?" * len(chunk)), chunk):
            heard.setdefault(keyed[eid], []).append(text or "")
    for raw in raws:
        want = aliases.get(raw)
        if want is not None:
            want = with_transcript_spelling(want, heard.get(raw) or ())
        have = con.execute("SELECT raw_key, canonical_key, display_basis, display FROM ad_brand_aliases"
                           " WHERE raw_key=?", (raw,)).fetchone()
        if want is None and have is not None:
            con.execute("DELETE FROM ad_brand_aliases WHERE raw_key=?", (raw,))
        elif want is not None and (have is None or tuple(have) != tuple(want)):
            con.execute("INSERT OR REPLACE INTO ad_brand_aliases (raw_key, canonical_key,"
                        " display_basis, display) VALUES (?,?,?,?)", want)
    out = {"named": [], "promo": [], "published": 0}
    published = []
    for i in sel:
        eid = ids.get(f"named:{named[i].get('candidate_id')}")
        if eid is None:
            continue
        cols = named_columns(final[i], rules)
        have = con.execute("SELECT brand_suggested, self_promo, self_promo_reason, needs_review,"
                           " candidates_json FROM ad_reads WHERE evidence_id=?", (eid,)).fetchone()
        if tuple(have) != cols:
            con.execute("UPDATE ad_reads SET brand_suggested=?, self_promo=?, self_promo_reason=?,"
                        " needs_review=?, candidates_json=? WHERE evidence_id=?", (*cols, eid))
            if have[0] != cols[0]:
                out["named"].append((eid, have[0], cols[0]))
            if cols[1] and not have[1]:
                out["promo"].append(eid)
        if final[i].get("_fell_through"):
            con.execute("INSERT OR IGNORE INTO ad_fallthrough (evidence_id) VALUES (?)", (eid,))
        else:
            con.execute("DELETE FROM ad_fallthrough WHERE evidence_id=?", (eid,))
        if import_sources._refresh_named_row(
                con, eid, import_sources.candidate_attrs(trans[i], "named"),
                str(trans[i].get("excerpt", ""))[:2000], actor):
            con.execute("UPDATE ad_reads SET evidence_revision=(SELECT revision FROM evidence"
                        " WHERE id=?) WHERE evidence_id=?", (eid, eid))
            published.append(eid)
    if words:
        det = active_payload(con, "detector-output") or []
        det_ids = dict(con.execute("SELECT record_key, evidence_id FROM ad_reads WHERE kind='rule'"))
        for c in det if isinstance(det, list) else []:
            if isinstance(c, dict) and brand_key(c.get("brand_spoken")) in words:
                eid = det_ids.get(f"det:{c.get('candidate_id')}")
                if eid:
                    con.execute("UPDATE ad_reads SET brand_suggested=? WHERE evidence_id=?"
                                " AND brand_suggested IS NOT ?", (det_brand(c, rules), eid,
                                                                  det_brand(c, rules)))
    corrections.publish_keeping_trackers(con, published, actor)
    out["published"] = len(published)
    return out


def _stored_segments(con: sqlite3.Connection, version_id) -> list:
    """The segments of one stored source version ([] when unreadable)."""
    row = con.execute("SELECT payload FROM source_versions WHERE id=?",
                      (version_id,)).fetchone()
    try:
        return (json.loads(row["payload"]).get("segments") or []) if row else []
    except (TypeError, ValueError, AttributeError):
        return []


class StoredSegments:
    """episode id -> the segments of its stored transcript copy, decoded on
    each get (rebuild's `segments`): a load never holds every transcript
    at once. `versions`: episode id -> source version id."""

    def __init__(self, con: sqlite3.Connection, versions: dict):
        self._con, self._versions = con, versions

    def get(self, episode_id, default=None):
        version_id = self._versions.get(episode_id)
        if version_id is None:
            return default
        return _stored_segments(self._con, version_id)


def rebuild(con: sqlite3.Connection, *, episodes: list[dict], named: list[dict],
            det: list[dict], jev_windows: list[dict], segments,
            record_ids: dict, export_root: Path | None, jev_screened: set,
            clips: list[dict] | None = None) -> dict:
    """Rebuild every side table from one load, in the caller's transaction.

    clips: clip inputs a pilot import admitted ({id, episode_id, rel, path,
    sha256, bytes, start_s, end_s, transcript_sha256}); each is bound to the
    transcript version its request admitted (the source version with that
    file digest), never to a newer copy the load keeps current; 0 (never
    plays) when there is none. Without an export root no folder is scanned
    and registered clips are kept as they are, their binding included:
    imports only add clips.

    episodes/named/det: the export's episode rows, named reads and rule
    candidates; jev_windows: [{key, episode, start, end}] at the seed
    threshold; segments: transcript segments by episode id (the loaded
    transcripts; anything with get(episode id), e.g. StoredSegments); record_ids: stable record key -> evidence id of this
    load's rows; jev_screened: episode ids with a Jev result."""
    from . import corrections
    from .citations import _PinnedTranscript
    ensure_schema(con)
    # The operator's rules (corrections module): the stoplist gives a
    # read the namer's next pick, alias rules sit over the export's
    # aliases, self-promo rules mark reads; the same as they did at once.
    rules = corrections.load(con)
    named = corrections.stoplist_reads(named, rules)
    # Every table is synced to this load row by row (_sync): an unchanged
    # reload writes nothing here.
    # Raw -> canonical brand alias table (brand_spoken stays evidence identity).
    aliases = corrections.alias_rows(rules, export_aliases(named))
    shows = {str(ep.get("ep_id")): ep.get("show_title") for ep in episodes if isinstance(ep, dict)}
    # Self-promo rules never stand over a read a person reviewed.
    reviewed = corrections.reviewed_ids(con)
    named = corrections.self_promo_reads(
        named, rules, shows, lambda raw: (aliases.get(brand_key(raw)) or (None, brand_key(raw)))[1],
        skip={k for k, eid in record_ids.items() if eid in reviewed})
    fell_through = []
    # Supporting links: the export's keys, else interval overlap.
    det_by_ep: dict[str, list] = {}
    for c in det:
        det_by_ep.setdefault(str(c.get("ep_id")), []).append(c)
    jev_by_ep: dict[str, list] = {}
    for w in jev_windows:
        jev_by_ep.setdefault(str(w["episode"]), []).append(w)
    known_keys = set(record_ids)
    links: list[tuple] = []
    display_bounds: dict[str, tuple] = {}
    for read in named:
        key = f"named:{read['candidate_id']}"
        ep, start, end = str(read.get("ep_id")), read.get("start_s"), read.get("end_s")
        supporting = read.get("supporting_keys")
        if isinstance(supporting, list):
            keys = [k for k in supporting if isinstance(k, str) and k in known_keys]
        else:
            keys = [f"det:{c['candidate_id']}" for c in det_by_ep.get(ep, [])
                    if _overlaps(start, end, c.get("start_s"), c.get("end_s"))]
            keys += [w["key"] for w in jev_by_ep.get(ep, [])
                     if _overlaps(start, end, w["start"], w["end"])]
        links += [(key, k) for k in dict.fromkeys(keys)]
        tight = [c for c in det_by_ep.get(ep, []) if f"det:{c['candidate_id']}" in keys
                 and _overlaps(start, end, c.get("start_s"), c.get("end_s"))]
        if tight and start is not None and end is not None:
            lo = max(float(start), min(float(c["start_s"]) for c in tight))
            hi = min(float(end), max(float(c["end_s"]) for c in tight))
            display_bounds[key] = (lo, hi) if hi > lo else (float(start), float(end))
    links = list(dict.fromkeys(links))
    _sync(con, "ad_read_links", ("read_key", "support_key"), links)
    supported = {k for _read, k in links}
    # Clips: registered in place, sha256-checked. Without an export root
    # (upgrade/restore where the export is not found) the registered clips
    # are kept, never dropped.
    # Each clip is bound to a transcript version. A kept row keeps its own,
    # 0 included: an unbound row (one from before the column) is never
    # bound by a later load. A scanned local clip takes its episode's copy;
    # an admitted clip takes the version its request admitted.
    pins = dict(con.execute("SELECT id, transcript_version_id FROM episodes"))
    clips_by_ep: dict[str, list] = {}
    clip_rows = {}

    def add_clip(row: tuple) -> None:
        clip_rows[row[0]] = row
        clips_by_ep.setdefault(row[1], [])
        clips_by_ep[row[1]] = [c for c in clips_by_ep[row[1]] if c["id"] != row[0]] + [
            {"id": row[0], "rel": row[2], "start_s": row[6], "end_s": row[7],
             "transcript_version_id": row[8]}]
    if export_root is None:
        for row in con.execute("SELECT id, episode_id, rel, path, sha256, bytes, start_s, end_s,"
                               " transcript_version_id FROM ad_clips"):
            add_clip(tuple(row))
    for ep_id, found in _clip_index(export_root, episodes).items():
        for clip in found:
            verified = _verified_clip(export_root, clip)
            if verified is None:
                continue
            add_clip((f"clip:{clip['rel']}", ep_id, clip["rel"], verified["path"],
                      verified["sha256"], verified["bytes"], clip["start_s"], clip["end_s"],
                      pins.get(ep_id) or 0))
    for clip in clips or ():
        admitted = con.execute("SELECT id FROM source_versions WHERE source_key=? AND sha256=?"
                               " ORDER BY id DESC LIMIT 1",
                               (f"transcript:{clip['episode_id']}",
                                clip.get("transcript_sha256"))).fetchone()
        add_clip((clip["id"], clip["episode_id"], clip["rel"], clip["path"], clip["sha256"],
                  clip["bytes"], clip["start_s"], clip["end_s"], admitted[0] if admitted else 0))
    _sync(con, "ad_clips", ("id", "episode_id", "rel", "path", "sha256", "bytes", "start_s",
                            "end_s", "transcript_version_id"), clip_rows.values())
    # Per-read rows.
    transcripts: dict = {}
    # A read on an earlier copy is quoted from that copy (the one its
    # evidence row names), never from the episode's current transcript.
    copy_of: dict = {}
    ids = sorted(set(record_ids.values()))
    for at in range(0, len(ids), 500):
        chunk = ids[at:at + 500]
        copy_of.update(con.execute("SELECT id, transcript_version_id FROM evidence WHERE id IN"
                                   " (%s)" % ",".join("?" * len(chunk)), chunk).fetchall())

    def quote(ep, start, end, evidence_id=None):
        if start is None or end is None:
            return ""
        copy = copy_of.get(evidence_id)
        key = ("copy", copy) if copy is not None and copy != pins.get(ep) else ep
        if key not in transcripts:
            # One transcript at a time: reads come grouped by episode, so
            # each is decoded about once per kind (IMPORTMEM).
            transcripts.clear()
            transcripts[key] = _PinnedTranscript(
                _stored_segments(con, copy) if key != ep else segments.get(ep) or [])
            transcripts[key]._scans = _PinnedTranscript.INDEX_AFTER  # index at once
        return transcripts[key].text(start, end)

    rows = []
    shown: list = []   # CLIPGUARD R8: CLIP_SHOWN, the reads judged clean
    no_clip_ids: list = []   # DETBUILD: NO_CLIP, the R6 parts and R9 rows
    # Verdicts count only from the rules' own rows (their version active),
    # and only when an R8-era load stamped that version: a named-reads
    # file's rows never bring one (CGS-R2-02), nor rules rows a release
    # before R8 stored (CGS-R3-02).
    from .import_sources import AD_RULES_R8_META, AD_RULES_SOURCE_KEY
    judged = con.execute("SELECT 1 FROM source_versions sv JOIN meta m ON m.key=?"
                         " AND m.value=CAST(sv.id AS TEXT) WHERE sv.source_key=?"
                         " AND sv.is_active=1", (AD_RULES_R8_META, AD_RULES_SOURCE_KEY)
                         ).fetchone() is not None
    # DETBUILD: display bounds count only from the rules' own rows (their
    # version active), never from a named-reads file row.
    ruled = con.execute("SELECT 1 FROM source_versions WHERE source_key=? AND is_active=1",
                        (AD_RULES_SOURCE_KEY,)).fetchone() is not None
    rule_shown: dict = {}
    named_count: dict[str, int] = {}
    promo_count: dict[str, int] = {}
    heard: dict[str, list] = {}  # raw key -> the read texts it was heard in
    for read in named:
        key = f"named:{read['candidate_id']}"
        evidence_id = record_ids.get(key)
        if evidence_id is None:
            continue
        ep, start, end = str(read.get("ep_id")), read.get("start_s"), read.get("end_s")
        promo = bool(read.get("self_promo"))
        (promo_count if promo else named_count)[ep] = \
            (promo_count if promo else named_count).get(ep, 0) + 1
        clip, clip_offset = None, None
        link = read.get("clip")
        # A clip links to a read on its own transcript version that it holds
        # whole; the export's contract link names which one.
        version = copy_of.get(evidence_id) or pins.get(ep)
        if ruled and read.get("source") in NO_CLIP_SOURCES:
            # DETBUILD: no clip was cut for an R6 part or an R9 row
            no_clip_ids.append(evidence_id)
        elif isinstance(link, dict) and isinstance(link.get("file"), str):
            clip = next((c for c in _pick_all(clips_by_ep.get(ep, []), start, end, version)
                         if c["rel"] == link["file"]), None)
        elif "clip" not in read:
            clip = _pick_clip(clips_by_ep.get(ep, []), start, end, version)
        if clip is not None and start is not None:
            clip_offset = max(0.0, float(start) - clip["start_s"])
        provenance = read.get("provenance") if isinstance(read.get("provenance"), dict) else {}
        candidates = provenance.get("candidates") if isinstance(provenance.get("candidates"), list) else []
        text = quote(ep, start, end, evidence_id)
        heard.setdefault(brand_key(read.get("brand_spoken") or read.get("brand")), []).append(text[:2000])
        if read.get("_fell_through"):
            fell_through.append((evidence_id,))
        if judged and read.get("clip_hidden") is False:
            shown.append(evidence_id)
        lo, hi = display_bounds.get(key, (start, end))
        base_lo = lo
        rule = _rule_display(read) if ruled else None
        if rule is not None and _number(base_lo):
            # DETBUILD R7/R6 start (GATE3 FAIL 1, The Nerve Wayfair): a rule
            # moves a shown end, never a shown start earlier than the start
            # the read shows without the rules. No span left: no rule display.
            rule = (max(rule[0], float(base_lo)), rule[1])
            if not rule[0] < rule[1]:
                rule = None
        search = fold(" ".join([str(read.get("excerpt") or ""), text]))
        shown_text = text
        if rule is not None:
            # What the rules show: its quote and its search text are that
            # part only. Clip links above stay on the detected span.
            lo, hi = rule
            shown_text = quote(ep, lo, hi, evidence_id)
            search = fold(shown_text)
            rule_shown[evidence_id] = [lo, hi]
        score = read.get("confidence_score")
        suggested, promo_flag, promo_reason, needs, candidates_json = named_columns(read, rules)
        rows.append((evidence_id, key, ep, "named", 0, start, end, lo, hi,
                     suggested, promo_flag, promo_reason, needs,
                     read.get("confidence"),
                     float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else None,
                     candidates_json, shown_text[:2000], search,
                     clip["id"] if clip else None, clip_offset))
    _sync(con, "ad_fallthrough", ("evidence_id",), fell_through)
    # A spelling the transcripts give a raw key when the export gives none.
    for raw, entry in list(aliases.items()):
        aliases[raw] = with_transcript_spelling(entry, heard.get(raw) or ())
    _sync(con, "ad_brand_aliases", ("raw_key", "canonical_key", "display_basis", "display"),
          aliases.values())
    for c in det:
        key = f"det:{c['candidate_id']}"
        evidence_id = record_ids.get(key)
        if evidence_id is None:
            continue
        ep, start, end = str(c.get("ep_id")), c.get("start_s"), c.get("end_s")
        text = quote(ep, start, end, evidence_id)
        rows.append((evidence_id, key, ep, "rule", int(key not in supported), start, end,
                     start, end, det_brand(c, rules), 0, None, 0,
                     c.get("confidence"), None, "[]", text[:2000],
                     fold(" ".join([str(c.get("excerpt") or ""), text])), None, None))
    for w in jev_windows:
        evidence_id = record_ids.get(w["key"])
        if evidence_id is None:
            continue
        text = quote(str(w["episode"]), w["start"], w["end"], evidence_id)
        rows.append((evidence_id, w["key"], str(w["episode"]), "jev", int(w["key"] not in supported),
                     w["start"], w["end"], w["start"], w["end"], None, 0, None, 0, None, None,
                     "[]", text[:2000], fold(" ".join([w.get("excerpt") or "", text])), None, None))
    revisions = {}
    ids = [r[0] for r in rows]
    for at in range(0, len(ids), 500):
        chunk = ids[at:at + 500]
        revisions.update(con.execute("SELECT id, revision FROM evidence WHERE id IN (%s)"
                                     % ",".join("?" * len(chunk)), chunk).fetchall())
    _sync_reads(con, [(*r, revisions.get(r[0])) for r in rows])
    # Screening status per episode.
    windows: dict[str, int] = {}
    for w in jev_windows:
        windows[str(w["episode"])] = windows.get(str(w["episode"]), 0) + 1
    screening = []
    for ep in episodes:
        ep_id = str(ep.get("ep_id") or "")
        if not ep_id:
            continue
        jev_status = ep.get("jev_status")
        tier = episode_tier(ep)
        screened = isinstance(jev_status, str) and jev_status.startswith("ok") \
            and ep_id in jev_screened
        name_status = ep.get("name_status")
        name_ok = name_status is None or (isinstance(name_status, str)
                                          and name_status.startswith(("ok", "done")))
        pipeline = ep.get("pipeline_status")
        no_ads = screened and name_ok and not windows.get(ep_id) and not named_count.get(ep_id)
        screening.append((ep_id, jev_status, name_status, ep.get("screen_status"),
                          ep.get("clip_status"), pipeline, int(ep_id in jev_screened),
                          windows.get(ep_id, 0), named_count.get(ep_id, 0),
                          promo_count.get(ep_id, 0), int(no_ads),
                          int(isinstance(pipeline, str) and pipeline.startswith("dead")), tier))
    _sync(con, "episode_screening", ("episode_id", "jev_status", "name_status", "screen_status",
                                     "clip_status", "pipeline_status", "jev_screened", "jev_windows",
                                     "named_reads", "self_promo_reads", "screened_no_ads",
                                     "stopped_partway", "coverage_tier"), screening)
    _set_meta(con, CLIP_SHOWN, json.dumps({"evidence_ids": sorted(set(shown))},
                                          separators=(",", ":")))
    if rule_shown:
        _set_meta(con, DISPLAY_RULE, json.dumps({"bounds": rule_shown}, sort_keys=True,
                                                separators=(",", ":")))
    else:
        _del_meta(con, DISPLAY_RULE)
    if no_clip_ids:
        _set_meta(con, NO_CLIP, json.dumps({"evidence_ids": sorted(set(no_clip_ids))},
                                           separators=(",", ":")))
    else:
        _del_meta(con, NO_CLIP)
    _set_meta(con, BUILT, "1")
    return {"aliases": len(aliases), "links": len(links), "reads": len(rows),
            "clips": sum(len(v) for v in clips_by_ep.values()),
            "screened_no_ads": sum(r[10] for r in screening)}


def _sync(con: sqlite3.Connection, table: str, columns: tuple, rows) -> None:
    """Make `table` hold exactly `rows` (tuples in `columns` order),
    deleting and inserting only rows that differ."""
    wanted = {tuple(r) for r in rows}
    cols = ", ".join(columns)
    have = {tuple(r) for r in con.execute(f"SELECT {cols} FROM {table}")}
    gone = have - wanted
    if gone:
        where = " AND ".join(f"{c} IS ?" for c in columns)
        con.executemany(f"DELETE FROM {table} WHERE {where}", list(gone))
    added = wanted - have
    if added:
        con.executemany(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({','.join('?' * len(columns))})",
                        list(added))


_READ_COLUMNS = ("evidence_id", "record_key", "episode_id", "kind", "review_list", "start_s",
                 "end_s", "display_start_s", "display_end_s", "brand_suggested", "self_promo",
                 "self_promo_reason", "needs_review", "confidence", "confidence_score",
                 "candidates_json", "quote", "search_folded", "clip_id", "clip_offset_s",
                 "evidence_revision")


# CLIP-STOP-FIX4 (LEDGER-LEAD3-CS-5): a read's last display span, kept when
# a build drops its ad_reads row (a later load retired the evidence row), as
# JSON [start, end] under RETIRED_DISPLAY + evidence id. The media route caps
# the retired row's refs there (customer.read_display_span); with none kept,
# a hosted request gets no clip. A meta row, not a column, as CLIP_SHOWN.
RETIRED_DISPLAY = "retired_display:"


def retired_display(con: sqlite3.Connection, evidence_id) -> list | None:
    """The display span a build kept for an evidence row it dropped from
    ad_reads (RETIRED_DISPLAY), or None."""
    raw = _meta(con, RETIRED_DISPLAY + str(evidence_id))
    try:
        span = json.loads(raw) if raw is not None else None
    except ValueError:
        return None
    return span if isinstance(span, list) and len(span) == 2 else None


def _sync_reads(con: sqlite3.Connection, rows: list[tuple]) -> None:
    """_sync for ad_reads and its full-text index (external content: an
    entry is removed with the values it was indexed with; ensure_schema
    has made the index whole first)."""
    fts = fts_present(con)
    cols = ", ".join(_READ_COLUMNS)
    have = {r[1]: r for r in con.execute(f"SELECT id, {cols} FROM ad_reads")}
    wanted = {r[0]: tuple(r) for r in rows}
    lo_at, hi_at = (_READ_COLUMNS.index(c) + 1 for c in ("display_start_s", "display_end_s"))
    for evidence_id, old in have.items():
        new = wanted.get(evidence_id)
        if new is not None and tuple(old[1:]) == new:
            continue
        if new is None:
            # The row leaves the side tables (retired): keep its last
            # display span for its refs (RETIRED_DISPLAY), or none.
            if old[lo_at] is None or old[hi_at] is None:
                con.execute("DELETE FROM ad_reads_meta WHERE key=?",
                            (RETIRED_DISPLAY + evidence_id,))
            else:
                _set_meta(con, RETIRED_DISPLAY + evidence_id, json.dumps([old[lo_at], old[hi_at]]))
        if fts:
            con.execute(f"INSERT INTO {FTS} ({FTS}, rowid, search_folded) VALUES ('delete', ?, ?)",
                        (old[0], old[_READ_COLUMNS.index("search_folded") + 1]))
        con.execute("DELETE FROM ad_reads WHERE id=?", (old[0],))
    for evidence_id, new in wanted.items():
        old = have.get(evidence_id)
        if old is not None and tuple(old[1:]) == new:
            continue
        rowid = con.execute(f"INSERT INTO ad_reads ({cols}) VALUES ({','.join('?' * len(new))})",
                            new).lastrowid
        if fts:
            con.execute(f"INSERT INTO {FTS} (rowid, search_folded) VALUES (?, ?)",
                        (rowid, new[_READ_COLUMNS.index("search_folded")]))


def rebuild_from_workspace(con: sqlite3.Connection, repo: Path | None = None) -> dict:
    """Rebuild the side tables from the active stored source versions
    (`upgrade` on a workspace loaded before these tables existed). Rows are
    found by their stable record keys; no evidence row, review,
    association, campaign link or citation is written."""
    from . import import_sources

    def payload(key):
        row = con.execute("SELECT payload FROM source_versions WHERE source_key=? AND is_active=1"
                          " ORDER BY id DESC LIMIT 1", (key,)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["payload"])
        except (ValueError, TypeError):
            return None
    episodes = payload("episodes.json") or []
    named = loaded_named(con) or []
    det = payload("detector-output") or []
    run = payload("jev-run") or {}
    # The active row of each stable key (lineage.reconcile leaves one).
    record_ids, excerpts, jev_rows = {}, {}, {}
    where, args = active_where(con)
    for row in con.execute("SELECT e.id, e.source_record_key, e.excerpt, e.source,"
                           " e.episode_id, e.orig_start_s, e.orig_end_s FROM evidence e"
                           f" JOIN episodes ep ON ep.id=e.episode_id WHERE {where}"
                           " ORDER BY e.created_at, e.id", args):
        if row["source_record_key"]:
            record_ids[row["source_record_key"]] = row["id"]
            excerpts[row["source_record_key"]] = row["excerpt"]
            if row["source"] == "jev":
                jev_rows[row["source_record_key"]] = row
    windows, screened = [], set()
    for ep in (run.get("episodes") or []) if isinstance(run, dict) else []:
        ep_id = ep.get("episode_id") or ep.get("ep_id")
        if ep.get("answers") is not None:
            screened.add(str(ep_id))
        for wid, w in sorted((ep.get("answers") or {}).items()):
            try:
                if float(w.get("noul", 0)) < import_sources.JEV_THRESHOLD:
                    continue
                # The window's stored excerpt, as the load indexed it.
                windows.append({"key": f"jev:{ep_id}:{wid}", "episode": ep_id,
                                "start": float(w["start_s"]), "end": float(w["end_s"]),
                                "excerpt": excerpts.get(f"jev:{ep_id}:{wid}") or ""})
            except (TypeError, ValueError, KeyError):
                continue
    # Every other active Jev row is a read too (a window a re-screen no
    # longer flags stays active and counted: it keeps its read facts and
    # full-text entry, R4-04).
    listed = {w["key"] for w in windows}
    for key, row in jev_rows.items():
        if key in listed or not isinstance(row["orig_start_s"], (int, float)) \
                or not isinstance(row["orig_end_s"], (int, float)):
            continue
        windows.append({"key": key, "episode": row["episode_id"],
                        "start": float(row["orig_start_s"]), "end": float(row["orig_end_s"]),
                        "excerpt": row["excerpt"] or ""})
    # Each episode's pinned copy, decoded when a read quotes it.
    segments = StoredSegments(con, dict(con.execute(
        "SELECT ep.id, sv.id FROM episodes ep JOIN source_versions sv"
        " ON sv.id=ep.transcript_version_id")))
    # The export root: where the last load's episodes.json was (a repo-layout
    # seed records it relative to the repo). Unknown root: the registered
    # clips stay as they are (rebuild keeps them).
    # Only a local repo layout has an export root to scan: a pilot host
    # (EARWITNESS_PILOT_LAYOUT set) or a call without a repo never scans, so
    # clips arrive only through the pilot import (audio clips design 4.5).
    root = None
    row = con.execute("SELECT path FROM source_files WHERE kind='episodes_json'"
                      " ORDER BY id DESC LIMIT 1").fetchone()
    if repo is None or os.environ.get("EARWITNESS_PILOT_LAYOUT"):
        row = None
    if row is not None:
        path = Path(row["path"])
        if not path.is_absolute() and repo is not None:
            path = Path(repo) / path
        if path.is_absolute() and path.is_file():
            root = path.parent.parent
    return rebuild(con, episodes=episodes if isinstance(episodes, list) else [],
                   named=named if isinstance(named, list) else [],
                   det=det if isinstance(det, list) else [], jev_windows=windows,
                   segments=segments, record_ids=record_ids, export_root=root,
                   jev_screened=screened)


# -- Reads -----------------------------------------------------------------------

class Aliases(dict):
    """alias_map's result; rule_raws: raw keys an operator alias rule maps."""
    rule_raws: frozenset = frozenset()


def alias_map(con: sqlite3.Connection) -> dict[str, tuple]:
    """raw key -> (canonical key, display, display basis); {} without tables."""
    out = Aliases()
    if not tables_present(con):
        return out
    out.update((r[0], (r[1], r[2], r[3])) for r in con.execute(
        "SELECT raw_key, canonical_key, display, display_basis FROM ad_brand_aliases"))
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ad_rules'").fetchone():
        out.rule_raws = frozenset(r[0] for r in con.execute(
            "SELECT key FROM ad_rules WHERE kind='alias'"))
    return out


def canonical(aliases: dict, raw) -> str:
    key = brand_key(raw)
    hit = aliases.get(key)
    return hit[0] if hit else key


def assoc_key(aliases: dict, assoc: dict) -> str:
    """The brand key of one brand association: through the alias table,
    except that a confirmed brand (a person's) is never remapped by an
    operator alias rule."""
    key = brand_key(assoc.get("brand"))
    if assoc.get("basis") == "confirmed" and key in getattr(aliases, "rule_raws", ()):
        return key
    return canonical(aliases, assoc.get("brand"))


RAW_TOKEN = "raw:"  # match_keys/wanted_keys: a spelling an operator alias maps away


def wanted_keys(aliases: dict, brands) -> set:
    """What a brand filter for `brands` matches (against match_keys): each
    one's canonical key; but the raw spelling of an operator alias matches
    only itself (the reads heard as it and the reads a person confirmed
    under it), never the alias's target brand. With the customer display
    list (customer.customer_aliases) a listed key or display name matches
    the key of its display name."""
    out = set()
    shown = getattr(aliases, "display_filter", None) or {}
    for brand in brands:
        key = brand_key(brand)
        if key in getattr(aliases, "rule_raws", ()):
            out.add(RAW_TOKEN + key)
        elif key in shown:
            out.add(shown[key])
        elif key:
            out.add(canonical(aliases, brand))
    out.discard("")
    return out


def match_keys(keys, heard=None) -> set:
    """A read's brand keys as wanted_keys matches them: its keys, and as
    spellings its keys and the spelling it was heard as."""
    out = set(keys)
    out.update(RAW_TOKEN + k for k in keys if k)
    if brand_key(heard):
        out.add(RAW_TOKEN + brand_key(heard))
    return out


def item_heard(item: dict):
    """The spelling a result item was heard as (its read block), unless a
    person confirmed its brand: the confirmation wins (a read heard as
    "shopify" and confirmed as Wix is only Wix)."""
    brand = (item.get("read") or {}).get("brand") or {}
    return None if brand.get("confirmed") else brand.get("suggested")


def brand_display(con: sqlite3.Connection, key: str, aliases: dict | None = None) -> tuple:
    """(display spelling, basis) for one canonical key: the export's (or
    the operator's) spelling, else the curated customer label, else the
    spelling the transcripts give the key itself, else the key in title
    case marked "none" (no known spelling). Another heard spelling's
    transcript casing never names the company ("Comcast Business" is not
    the display of `comcast`). The key itself stays the lowercase
    matcher. The customer display list (customer.customer_aliases:
    display_names) names its keys first."""
    from .customer import BRAND_DISPLAY_NAMES
    aliases = alias_map(con) if aliases is None else aliases
    listed_name = (getattr(aliases, "display_names", None) or {}).get(key)
    if listed_name:
        return listed_name, "curated"
    known = [(v[1], v[2]) for v in aliases.values() if v[0] == key and v[1]]
    listed = sorted(k for k in known if k[1] != "transcript_cased")
    if listed:
        return listed[0]
    if key in BRAND_DISPLAY_NAMES:
        return BRAND_DISPLAY_NAMES[key], "curated"
    if con is not None and con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ad_rules'").fetchone():
        row = con.execute("SELECT value FROM ad_rules WHERE kind='brand' AND key=?",
                          (key,)).fetchone()
        rule = json.loads(row[0] or "{}") if row else {}
        text = rule.get("display")
        if rule.get("origin") in ("auto", "manual") and isinstance(text, str) \
                and text != text.lower():
            return text, "transcript_cased" if rule["origin"] == "auto" else "brand_list"
    own = sorted(d for d, _b in known if fold(d) == key)
    if own:
        return own[0], "transcript_cased"
    return title_case(key), "none"


def _informative(form: str) -> bool:
    """A transcript spelling worth showing: a capital past the first letter
    (BetterHelp, QVC) or every word capitalized (Capella University)."""
    words = [w for w in re.split(r"[\s\-]+", form) if w]
    return any(c.isupper() for c in form[1:]) and (
        any(c.isupper() for w in words for c in w[1:]) or all(w[:1].isupper() or not w[:1].isalpha()
                                                              for w in words))


def transcript_spelling(raw: str, texts) -> str | None:
    """How the read transcripts spell a raw brand key when that spelling
    carries case (BetterHelp, Capella University): the most common
    capitalized form, if its case says more than a sentence start, else
    None."""
    tokens = re.findall(r"[a-z0-9&']+", raw or "")
    if not tokens:
        return None
    pattern = re.compile(r"(?<![A-Za-z0-9])" + r"[\s\-.]?".join(re.escape(t) for t in tokens)
                         + r"(?![A-Za-z0-9])", re.IGNORECASE)
    seen: dict[str, int] = {}
    for text in texts:
        for m in pattern.finditer(text or ""):
            form = m.group(0)
            if fold(form).replace(" ", "") == raw.replace(" ", "") or fold(form) == raw:
                seen[form] = seen.get(form, 0) + 1
    # Every capitalized form votes ("Wayfair" outvotes a stray "WayFair");
    # the winner shows only if its case says something.
    votes = [f for f in seen if f[:1].isupper() or f[:1].isdigit()]
    if not votes:
        return None
    best = max(sorted(votes), key=lambda f: seen[f])
    return best if _informative(best) else None


def title_case(key: str) -> str:
    """A readable fallback for a brand key with no known spelling: words
    capitalized; short vowel-less words and short words with digits or "&"
    in capitals (NPR, AG1, AT&T); web addresses as heard."""
    out = []
    for word in (key or "").split():
        if "." in word or "/" in word:
            out.append(word)
        elif "&" in word or (any(c.isdigit() for c in word) and len(word) <= 4 and word[0].isalpha()) \
                or (len(word) <= 3 and word.isalpha() and not re.search(r"[aeiouy]", word)):
            out.append(word.upper())
        else:
            out.append(word[:1].upper() + word[1:])
    return " ".join(out)


def review_list(con: sqlite3.Connection, *, limit: int = 100, offset: int = 0) -> dict:
    """Operator review list: active lone rule candidates and unnamed Jev
    windows of the current load that no review has decided yet."""
    if not tables_present(con):
        return {"items": [], "total": 0}
    where, args = active_where(con)
    base = (f" FROM ad_reads ar JOIN evidence e ON e.id=ar.evidence_id"
            f" JOIN episodes ep ON ep.id=e.episode_id LEFT JOIN reviews r ON r.evidence_id=e.id"
            f" WHERE ar.review_list=1 AND r.judgment IS NULL AND {where}")
    total = con.execute("SELECT COUNT(*)" + base, args).fetchone()[0]
    rows = con.execute("SELECT ar.evidence_id, ar.record_key, ar.kind, ar.episode_id, ar.start_s,"
                       " ar.end_s, ar.brand_suggested, ar.quote, ep.show_title, ep.ep_title,"
                       " ep.pub_date" + base + " ORDER BY ep.pub_date DESC, ar.episode_id,"
                       " ar.start_s LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
    return {"items": [dict(r, quote=(r["quote"] or "")[:600]) for r in rows], "total": total}


def active_where(con: sqlite3.Connection, current_transcript: bool = True) -> tuple[str, list]:
    """The operator list's active-row rule (ops._filter_where): current
    source version (own pin or importer membership) and, unless
    current_transcript is False, current transcript.
    Aliases: e = evidence, ep = episodes."""
    from . import ops
    filt = {"source_version": None,
            "history": "active" if current_transcript else "active_version",
            "source": [], "review": [],
            "brand": "", "brand_basis": [], "brand_channel": [], "show": "", "category": "",
            "episode": "", "pub_from": "", "pub_to": "", "include_unknown_dates": True}
    return ops._filter_where(filt, con=con)


def clip_for(con: sqlite3.Connection, evidence_id: str, *, known=None) -> dict | None:
    """The read's linked clip. With the clip guard on (CLIPGUARD R8; the
    hosted web sets it at start, the local app never does, so its operator
    review page always sees the guard off), for every caller (the media
    lookups, has_clip, episode_reads): None for a read the guard refuses
    (clip_hidden), and for a
    link to a clip outside the read's own padded window (the window R8
    judged; media._clip_qualifies), such as the clip of another read that
    holds this one. `known`: (tables_present(con), shown_clips(con)), when
    the caller has them once per request (CG-R2-03)."""
    present, shown = known if known is not None else (tables_present(con), False)
    if not present:
        return None
    guard = clip_guard_on()
    if guard and clip_hidden(con, evidence_id, shown):
        return None
    row = con.execute("SELECT c.*, ar.clip_offset_s, ar.start_s AS read_start_s,"
                      " ar.end_s AS read_end_s FROM ad_reads ar JOIN ad_clips c"
                      " ON c.id=ar.clip_id WHERE ar.evidence_id=?", (evidence_id,)).fetchone()
    if row is None:
        return None
    clip = dict(row)
    start, end = clip.pop("read_start_s"), clip.pop("read_end_s")
    if guard:
        from .media import _clip_qualifies
        if not _clip_qualifies(clip, clip["episode_id"], clip["transcript_version_id"],
                               start, end):
            return None
    return clip


def screening(con: sqlite3.Connection, episode_id: str) -> dict | None:
    if not tables_present(con):
        return None
    row = con.execute("SELECT * FROM episode_screening WHERE episode_id=?", (episode_id,)).fetchone()
    return None if row is None else _screening_state(row)


def screening_many(con: sqlite3.Connection, episode_ids) -> dict[str, dict]:
    """screening() for many episodes in chunked reads: id -> state (an
    episode without a screening row has no entry)."""
    if not tables_present(con):
        return {}
    ids = list(episode_ids)
    out: dict[str, dict] = {}
    for at in range(0, len(ids), 500):
        chunk = ids[at:at + 500]
        for row in con.execute("SELECT * FROM episode_screening WHERE episode_id IN (%s)"
                               % ",".join("?" * len(chunk)), chunk):
            out[row["episode_id"]] = _screening_state(row)
    return out


def screening_display_many(con: sqlite3.Connection, episode_ids,
                           listed: dict | None = None,
                           run_cache: dict | None = None) -> dict[str, dict]:
    """screening_many() for the episode and show pages: an episode with no
    episode_screening row (a benchmark or upload episode) shows the state
    its active jev-run entry proves (coverage_import.run_entry_screening,
    the rule that attests its screen stage), in the same wording. A row
    keeps its own state; an entry that does not prove the current copy
    adds nothing. listed: episode id -> ads the page lists; a run entry that
    flags no window says "no ads found" only for an episode listing none.
    run_cache: a dict scoped to one read pass (a draft's reads_cache); the
    parsed jev run is kept there so it is parsed once per pass."""
    from . import coverage_import as _cov
    ids = list(episode_ids)
    out = screening_many(con, ids)
    missing = [i for i in ids if i not in out]
    if missing and tables_present(con):
        if run_cache is None:
            jev = _cov._jev_run(con)
        else:
            if "jev_run" not in run_cache:
                run_cache["jev_run"] = _cov._jev_run(con)
            jev = run_cache["jev_run"]
        for eid in missing:
            row = _cov.run_entry_screening(con, eid, jev) if eid in jev else None
            if row is not None:
                if (listed or {}).get(eid):
                    row["screened_no_ads"] = 0
                out[eid] = _screening_state(row)
    return out


def _screening_state(row) -> dict:
    out = dict(row)
    for key in ("jev_screened", "screened_no_ads", "stopped_partway"):
        out[key] = bool(out[key])
    if out["stopped_partway"]:
        out["label"] = "Stopped partway (" + str(out["pipeline_status"]).split(":", 1)[-1] + ")"
    elif out["screened_no_ads"]:
        out["label"] = "Screened, no ads found"
    elif out["jev_screened"]:
        out["label"] = "Screened"
    elif out.get("coverage_tier") == "transcript":
        out["label"] = "Not screened for ads (transcript only)"
    else:
        out["label"] = "Not screened"
    return out
