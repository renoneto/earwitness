"""K01 synthetic fixture foundation (candidate-only, no owner/corpus rows).

All payloads are generated in-memory with genuine SHA-256 hashes over
canonical JSON. No .earwitness state, corpus seed CLI, or recovery
authority is touched. Later source/resource task fixtures invoke actual
domain operations on top of these rows, never canned response dicts.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

from earwitness.app import db
from earwitness.app import collaboration as collaboration


def _digest(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


SEGMENTS_E1 = [
    {"start": 0.0, "end": 10.0, "text": "Welcome to the show"},
    {"start": 10.0, "end": 20.0, "text": "This episode is brought to you by Lumen"},
    {"start": 20.0, "end": 30.0, "text": "Enjoy your cafe lumen blend"},
]


def _add_version(con, key, payload) -> int:
    blob = json.dumps(payload, sort_keys=True)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    cur = con.execute(
        "INSERT INTO source_versions (source_key, version_no, sha256, payload,"
        " is_active, created_at) VALUES (?,"
        " (SELECT COALESCE(MAX(version_no),0)+1 FROM source_versions WHERE source_key=?), ?, ?, 1, ?)",
        (key, key, digest, blob, db.utcnow()),
    )
    return cur.lastrowid


def seed_source_rows(con) -> dict:
    """Insert deterministic synthetic source rows with real payload hashes.

    Same-title feeds (e1/e2 on distinct feeds), metadata/source ownership,
    plus one legacy profile/query/alert/note/report row each. Runs inside
    the caller's transaction when one is held, otherwise commits once.
    """
    own = not con.in_transaction
    if own:
        con.execute("BEGIN IMMEDIATE")
    try:
        metas = [
            {"ep_id": "e1", "show_title": "Same Title",
             "feed_url": "https://feed-a.example/rss",
             "ep_title": "Morning brew", "pub_date": "2026-03-10",
             "category": "business"},
            {"ep_id": "e2", "show_title": "Same Title",
             "feed_url": "https://feed-b.example/rss",
             "ep_title": "Evening blend", "pub_date": "2026-03-11",
             "category": "business"},
        ]
        _add_version(con, "episodes.json", metas)
        tv_ids = {}
        for meta in metas:
            segs = SEGMENTS_E1 if meta["ep_id"] == "e1" else [
                {"start": 0.0, "end": 10.0, "text": "A different feed entirely"},
                {"start": 10.0, "end": 20.0, "text": "Lumen keeps the lights on here"},
            ]
            tv = _add_version(con, f"transcript:{meta['ep_id']}", {"segments": segs})
            tv_ids[meta["ep_id"]] = tv
            con.execute(
                "INSERT OR IGNORE INTO episodes (id, kind, show_title, ep_title,"
                " category, pub_date, fetch_date, transcript_version_id,"
                " revision, updated_at) VALUES (?, 'supplied', ?, ?, ?, ?,"
                " '2026-09-21', ?, 1, ?)",
                (meta["ep_id"], meta["show_title"], meta["ep_title"],
                 meta["category"], meta["pub_date"], tv, db.utcnow()))
        sv = _add_version(con, "detector:fixture-ad", {"origin": "synthetic"})
        con.execute(
            "INSERT OR IGNORE INTO evidence (id, source, source_version_id,"
            " episode_id, orig_start_s, orig_end_s, excerpt, attrs, revision,"
            " created_at, transcript_version_id, source_record_key,"
            " assoc_revision, record_kind) VALUES ('fixture-ad', 'detector', ?,"
            " 'e1', 10.0, 20.0, 'This episode is brought to you by Lumen',"
            " '{}', 1, ?, (SELECT transcript_version_id FROM episodes WHERE"
            " id='e1'), 'fixture-ad', 0, 'candidate')",
            (sv, db.utcnow()))
        con.execute(
            "INSERT OR IGNORE INTO reviews (evidence_id, judgment, revision,"
            " updated_at) VALUES ('fixture-ad', 'finding', 1, ?)",
            (db.utcnow(),))
        con.execute(
            "INSERT OR IGNORE INTO brand_assocs (evidence_id, brand, basis,"
            " origin, revision, updated_at) VALUES ('fixture-ad', 'Lumen',"
            " 'spoken', 'test', 1, ?)",
            (db.utcnow(),))
        # Deterministic legacy rows: genuine reader profile, saved query,
        # alert, personal note, and retained report/byte metadata under
        # the actual export/report storage (never canned service DTOs).
        profile = {"profile": "reader",
                   "scopes": ["research:read", "transcripts:read"],
                   "owner": "owner-1", "workspace_id": "local",
                   "updated_at": "2026-01-01T00:00:00+00:00"}
        con.execute("INSERT OR IGNORE INTO meta (key, value) VALUES"
                    " ('agent_profile', ?)", (json.dumps(profile),))
        con.execute(
            "INSERT OR IGNORE INTO customer_saved_searches (id, workspace_id,"
            " name, query_json, revision, created_at, updated_at) VALUES (1,"
            " 'local', 'legacy query', '{\"mode\":\"mentions\",\"q\":\"lumen\"}',"
            " 1, ?, ?)", (db.utcnow(), db.utcnow()))
        con.execute(
            "INSERT OR IGNORE INTO customer_alerts (id, workspace_id, name,"
            " query_json, definition_revision, origin_route, event_types,"
            " delivery_json, lifecycle, revision, baseline_json, monitor_json,"
            " created_at, updated_at) VALUES (1, 'local', 'legacy alert',"
            " '{\"mode\":\"mentions\",\"q\":\"lumen\"}', 1, '/searches', '[]',"
            " '{}', 'active_local', 1, '{}', '{}', ?, ?)",
            (db.utcnow(), db.utcnow()))
        con.execute(
            "INSERT OR IGNORE INTO customer_mistake_reports (id, workspace_id,"
            " ref, reason, status, revision, created_at, updated_at)"
            " VALUES (1, 'local', 'tx:e1', 'legacy note', 'open', 1, ?, ?)",
            (db.utcnow(), db.utcnow()))
        report_bytes = b"<html><body>legacy report</body></html>"
        report_digest = hashlib.sha256(report_bytes).hexdigest()
        con.execute(
            "INSERT OR IGNORE INTO campaigns (id, name, status, brand,"
            " revision, updated_at) VALUES (1, 'legacy campaign', 'active',"
            " 'Lumen', 1, ?)", (db.utcnow(),))
        con.execute(
            "INSERT OR IGNORE INTO exports (id, created_at, format, filter,"
            " aggregate_rev, item_count, payload) VALUES (1, ?, 'html', '{}',"
            " 0, 1, ?)", (db.utcnow(), report_bytes.decode("utf-8")))
        con.execute(
            "INSERT OR IGNORE INTO campaign_reports (id, workspace_id,"
            " campaign_id, report_no, campaign_revision, pins_json,"
            " report_json, export_id, export_digest, renderer_version,"
            " created_at) VALUES (1, 'local', 1, 1, 1, '{}', '{}', 1, ?,"
            " 'legacy', ?)", (report_digest, db.utcnow()))
        _seed_extended_map(con)
        # Producers publish on write; synthetic seeds mirror them. The
        # literal-v7 path has no v8 tables and must stay v8-object free.
        if db.has_v8_tables(con):
            publish_all_records(con)
        if own:
            con.commit()
    except Exception:
        if own:
            con.rollback()
        raise
    return {"episodes": ["e1", "e2"], "evidence": ["fixture-ad"]}


def _seed_extended_map(con) -> None:
    """Foundation rows for the wider acceptance fixture map.

    A double-count/conflict evidence pair, one brief chain, and one
    research target with a citation binding on the legacy campaign.
    Saved collections stay per-scenario (producers) so shared
    collection totals keep their exact-count assertions. Raw
    deterministic rows in the seed idiom; scenario state is built
    through domain producers on top. Column-tolerant: tables absent
    from older schemas are skipped.
    """
    tables = {r["name"] for r in
              con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    now = db.utcnow()
    if "evidence" in tables:
        con.execute(
            "INSERT OR IGNORE INTO evidence (id, source, source_version_id,"
            " episode_id, orig_start_s, orig_end_s, excerpt, attrs, revision,"
            " created_at, transcript_version_id, source_record_key,"
            " assoc_revision, record_kind) VALUES ('fixture-ad-dup',"
            " 'detector', (SELECT source_version_id FROM evidence WHERE"
            " id='fixture-ad'), 'e1', 10.0, 20.0,"
            " 'Lumen keeps the morning bright', '{}', 1, ?,"
            " (SELECT transcript_version_id FROM episodes WHERE id='e1'),"
            " 'fixture-ad-dup', 0, 'candidate')", (now,))
        con.execute(
            "INSERT OR IGNORE INTO reviews (evidence_id, judgment, revision,"
            " updated_at) VALUES ('fixture-ad-dup', 'finding', 1, ?)", (now,))
        con.execute(
            "INSERT OR IGNORE INTO brand_assocs (evidence_id, brand, basis,"
            " origin, revision, updated_at) VALUES ('fixture-ad-dup',"
            " 'Lumen', 'spoken', 'test', 1, ?)", (now,))
    if "provided_briefs" in tables:
        con.execute(
            "INSERT OR IGNORE INTO provided_briefs (brief_id, workspace_id,"
            " head_revision, current_version, created_at, updated_at)"
            " VALUES ('brief-fixture', 'local', 1, 1, ?, ?)", (now, now))
        brief_digest = _digest({"title": "Fixture brief",
                                "provided_text": "Say lumen clearly."})
        con.execute(
            "INSERT OR IGNORE INTO provided_brief_versions (workspace_id,"
            " brief_id, version, title, provided_text, digest, captured_at,"
            " actor) VALUES ('local', 'brief-fixture', 1, 'Fixture brief',"
            " 'Say lumen clearly.', ?, ?, 'fixture')",
            (brief_digest, now))
    if "campaign_targets" in tables and "target_evidence" in tables:
        con.execute(
            "INSERT OR IGNORE INTO campaign_targets (id, workspace_id,"
            " campaign_id, scope, episode_id, note, source_scope_json,"
            " revision, created_at, updated_at) VALUES ('target-fixture',"
            " 'local', 1, 'episode', 'e1', 'Fixture target', '{}', 1, ?,"
            " ?)", (now, now))
        con.execute(
            "INSERT OR IGNORE INTO target_evidence (workspace_id, target_id,"
            " ref, attached_at, attached_by) VALUES ('local',"
            " 'target-fixture', 'ev:fixture-ad@v1', ?, 'fixture')", (now,))


def _prepare(con) -> sqlite3.Connection:
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON;")
    db.init_schema(con)
    return con


def make_memory_workspace() -> sqlite3.Connection:
    """Prepared schema, autocommit SQLite, synthetic e1, no membership."""
    con = sqlite3.connect(":memory:", isolation_level=None)
    _prepare(con)
    seed_source_rows(con)
    return con


def _init_v7_literal(con) -> None:
    """Build a genuine v7 workspace from the captured source-v7 DDL.

    Executes tests/fixtures/backend-v7-schema.sql directly, independent
    of the new implementation, then stamps version markers and minimal
    meta rows. No v8 objects are created.
    """
    ddl_path = Path(__file__).parent / "fixtures" / "backend-v7-schema.sql"
    con.executescript(ddl_path.read_text(encoding="utf-8"))
    for version in (3, 4, 5, 6, 7):
        con.execute("INSERT OR IGNORE INTO schema_version VALUES (?, ?)",
                    (version, db.utcnow()))
    if con.execute("SELECT value FROM meta WHERE key='aggregate_rev'").fetchone() is None:
        con.execute("INSERT INTO meta (key, value) VALUES ('aggregate_rev', '0')")
    assert db._schema_version(con) == 7
    assert not db.has_v8_tables(con)


def make_workspace(path, *, schema_version) -> Path:
    """Create a fresh file workspace at path with the requested schema.

    Refuses any occupied target without modifying its files or rows: a
    fresh-fixture producer never unlinks an existing database and never
    infers ownership from its basename. schema_version 7 builds a
    genuine v7 workspace from the captured literal DDL; 8 builds the
    current schema. Anything else raises. No workspace membership is
    created. Returns the DB file path.
    """
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    db_path = target / "workspace.sqlite3"
    if db_path.exists():
        raise FileExistsError(f"fixture target is occupied: {db_path}")
    if schema_version == 7:
        con = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys=ON;")
            _init_v7_literal(con)
            seed_source_rows(con)
            con.commit()
        except Exception:
            con.close()
            db_path.unlink()
            raise
        else:
            con.close()
    elif schema_version == 8:
        con = sqlite3.connect(str(db_path), isolation_level=None)
        con.execute("PRAGMA journal_mode=WAL")
        try:
            _prepare(con)
            seed_source_rows(con)
        finally:
            con.close()
    else:
        raise ValueError(f"unsupported fixture schema_version: {schema_version!r}")
    db_path.chmod(0o600)
    return db_path


def publish_sources(con) -> dict:
    """Publish fixture sources through the real producer hooks (K03).

    Uses citations.publish_source_state on actual domain rows, never
    canned response dictionaries or direct-table bodies.
    """
    from earwitness.app import citations as _citations
    refs = {}
    own = not con.in_transaction
    if own:
        con.execute("BEGIN IMMEDIATE")
    try:
        for ev_id in ("fixture-ad",):
            refs[ev_id] = _citations.publish_source_state(
                con, kind="evidence", record_id=ev_id, actor="fixture")
        for ep_id in ("e1", "e2"):
            refs[ep_id] = _citations.publish_source_state(
                con, kind="episode", record_id=ep_id, actor="fixture")
        if own:
            con.commit()
    except Exception:
        if own:
            con.rollback()
        raise
    return refs


def publish_all_records(con) -> dict:
    """Publish every episode/evidence row through the real producer hook.

    Runtime producers publish on write (import_sources, ops review/episode
    paths); synthetic seeds mirror them so every matchable record owns a
    server-issued SourceRef1. Returns {(kind, record_id): ref}.
    """
    from earwitness.app import citations as _citations
    from earwitness.app import customer
    customer.rebuild_customer_show_map(con)
    out = {}
    for (ep_id,) in con.execute("SELECT id FROM episodes").fetchall():
        out[("episode", ep_id)] = _citations.publish_source_state(
            con, kind="episode", record_id=ep_id, actor="fixture")
    for (ev_id,) in con.execute("SELECT id FROM evidence").fetchall():
        out[("evidence", ev_id)] = _citations.publish_source_state(
            con, kind="evidence", record_id=ev_id, actor="fixture")
    return out


def current_source_ref(con, kind, record_id) -> str:
    row = con.execute(
        "SELECT id FROM source_states WHERE workspace_id='local' AND kind=?"
        " AND record_id=? ORDER BY publication_order DESC LIMIT 1",
        (kind, record_id)).fetchone()
    assert row is not None, f"no publication for {kind}:{record_id}"
    return row[0]


def customer_context() -> SimpleNamespace:
    """Trusted browser/member context for K01–K03 resolver tests."""
    return SimpleNamespace(credential_kind="browser_session",
                           member="owner", workspace_id="local",
                           authority_kind="member")


def fresh_v7_file() -> Path:
    """Fresh on-disk v7 workspace for upgrade/rehearsal tests."""
    tmpdir = Path(tempfile.mkdtemp(prefix="ew-v7-"))
    return make_workspace(tmpdir, schema_version=7)


class _FileFixtureConnection(sqlite3.Connection):
    def close(self):
        try:
            super().close()
        finally:
            self.fixture_directory.cleanup()


def make_file_workspace():
    """Prepared file-backed fixture; closing its actual connection removes owned files."""
    directory = tempfile.TemporaryDirectory(prefix="ew-recording-fixture-")
    filename = Path(directory.name) / "workspace.sqlite3"
    con = sqlite3.connect(filename, factory=_FileFixtureConnection)
    con.fixture_directory = directory
    return _prepare(con)


def register_synthetic_recording(con, episode_id, *, offset_s=0.0, duration_s=None):
    """Generate contained WAV bytes and bind real archive/pipeline/container/offset proof."""
    import uuid
    import wave
    from earwitness.app import citations, ops
    from earwitness.pipeline import artifacts
    root = Path(con.execute("PRAGMA database_list").fetchone()[2]).parent
    if not con.execute("PRAGMA database_list").fetchone()[2]:
        raise ValueError("recording fixture requires an owned file-backed workspace")
    episode = con.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    segments = citations._transcript_payload(con, episode["transcript_version_id"])
    full_duration = float(episode["duration_min"]) * 60 if episode["duration_min"] else \
        max((segment["end"] for segment in segments), default=0)
    duration = full_duration - offset_s if duration_s is None else duration_s
    filename = "fixture-" + uuid.uuid4().hex + ".wav"
    with wave.open(str(root / filename), "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(10)
        recording.writeframes(b"\0\0" * round(duration * 10))
    payload = (root / filename).read_bytes()
    artifact = artifacts.register_media(con, digest=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload), duration_s=duration, format="wav", locator=filename,
        fetch_date="2026-10-06", fetch_date_source="synthetic-fixture")
    selected = [segment for segment in segments
                if segment["end"] > offset_s and segment["start"] < offset_s + duration]
    cur = con.execute("INSERT INTO transcript_versions(episode_id,media_artifact_id,segments,source_offsets,created_at)"
                      " VALUES(?,?,?,?,?)", (episode_id, artifact["id"], json.dumps(selected),
                      json.dumps({"global_start_s": offset_s}), db.utcnow()))
    if episode["duration_min"] is None:
        con.execute("UPDATE episodes SET duration_min=?,revision=revision+1 WHERE id=?",
                    (full_duration / 60, episode_id))
        db.record_episode_snapshot(con, episode_id, "fixture")
    revision = con.execute("SELECT revision FROM episodes WHERE id=?", (episode_id,)).fetchone()[0]
    ops.set_episode_media(con, episode_id, {"media_artifact_id": artifact["id"], "alignment": "verified"}, revision)
    return {"artifact": artifact, "pipeline_transcript_version_id": cur.lastrowid,
            "transcript_version_id": episode["transcript_version_id"]}


TRANSCRIPT_TIER_EP = "ep_fx5"
TRANSCRIPT_TIER_WORD = "marmalade"


def add_transcript_tier_episode(export: Path, *, jev: str = "none", tier: bool = True) -> None:
    """Add one transcript-tier episode (ep_fx5) to a copy of the export-contract
    fixture, the way the corpus export writes it: `coverage_tier: "transcript"`,
    a transcript, and no detector, Jev, named or clip rows. `jev="full"` also
    gives it jev_status ok and a Jev result (a tier whose stage set runs Jev);
    `jev="status_only"` gives the status without a result; `tier=False` writes
    the same row as an older export would, without the key."""
    export = Path(export)
    path = export / "data" / "episodes.json"
    episodes = json.loads(path.read_text())
    row = {"ep_id": TRANSCRIPT_TIER_EP, "show_title": "Fixture Show C", "category": "business",
           "feed_url": "https://feed-c.example/rss", "ep_title": "Transcript only",
           "guid": f"guid-{TRANSCRIPT_TIER_EP}", "pub_date": "2026-10-05",
           "audio_url": f"https://audio.example/{TRANSCRIPT_TIER_EP}.mp3", "duration_min": 5.0,
           "fetch_date": "2026-10-09", "clip_status": None, "clip_skip_reason": None, "clips": [],
           "screen_status": None, "jev_status": "ok" if jev != "none" else None,
           "name_status": None, "pipeline_status": "done"}
    if tier:
        row["coverage_tier"] = "transcript"
    episodes.append(row)
    path.write_text(json.dumps(episodes))
    segments = [{"start": 0.0, "end": 30.0, "text": f"We talk about {TRANSCRIPT_TIER_WORD} jars."},
                {"start": 30.0, "end": 60.0, "text": "Nothing else happens."}]
    (export / "transcripts" / f"{TRANSCRIPT_TIER_EP}.json").write_text(json.dumps(
        {"ep_id": TRANSCRIPT_TIER_EP, "model": "tiny", "language": "en", "duration_min": 5.0,
         "segments": segments}))
    if jev == "full":
        jev_file = export / "experiments/jev-ad-triage-20260921/results.json"
        run = json.loads(jev_file.read_text())
        run["episodes"].append({"episode_id": TRANSCRIPT_TIER_EP, "model": "jev-1.13.0",
                                "answers": {"window_000": {"noul": 0.05, "start_s": 0.0,
                                                           "end_s": 60.0}}})
        jev_file.write_text(json.dumps(run))


def record_screening(con, episode_id, *, no_ads=False, index=True):
    """The facts beside a screen attestation that make an episode screened
    and fully checked (coverage.episode_screen_state, DET-PLAN item 6): its
    screening record (full tier, screened, not stopped partway) in a built
    ad_reads workspace, and an index version of its current copy."""
    from earwitness.app import ad_reads, transcript_index
    con.execute("INSERT OR REPLACE INTO episode_screening (episode_id, jev_screened,"
                " stopped_partway, coverage_tier, screened_no_ads) VALUES (?, ?, 0, 'full', ?)",
                (episode_id, 0 if no_ads else 1, 1 if no_ads else 0))
    ad_reads._set_meta(con, ad_reads.BUILT, "1")
    tv = con.execute("SELECT transcript_version_id FROM episodes WHERE id=?",
                     (episode_id,)).fetchone()[0]
    if index and tv is not None:
        transcript_index.index_versions(con, [tv])
    con.commit()
