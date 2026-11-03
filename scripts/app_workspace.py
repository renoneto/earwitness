#!/usr/bin/env python3
"""Local corpus import, status, reindex, consistent backup, and locked restore.

Also the trusted local-operator workflow for the hosted registry:
register-owner records an existing owner workspace database, and
bind-account binds one authenticated (issuer, subject) to it. OIDC
login alone never claims an owner workspace; these subcommands run on
the operator's own machine, never from client JSON (which must never
choose DB paths, external identities, row namespaces, or bindings)."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import ipaddress
import json
import os
import socket
import ssl
import struct
import subprocess
import time
from pathlib import Path
import sqlite3
import sys
import tempfile
import urllib.parse
import codecs
import xml.parsers.expat as _expat

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from earwitness.app import connections as registry
from earwitness.app import db, import_sources as importer, ops

REPO = Path(__file__).resolve().parent.parent
RECOVERY_DIR = REPO / ".earwitness-recovery"

READER_BASE_SCOPES = ["research:read", "transcripts:read"]
OPTIONAL_READ_SCOPES = {"campaigns": "campaigns:read", "watchlists": "watchlists:read",
                        "notes": "notes:read"}
AGENT_CLIENTS = ("codex", "cursor", "claude-desktop")


def resolve_workspace(value):
    path = (REPO / value).resolve()
    if REPO not in path.parents:
        raise ValueError("workspace must be a directory inside the repository")
    return path


def restore(workspace, source, force):
    source = (REPO / source).resolve()
    if REPO not in source.parents or not source.is_file():
        raise ValueError("backup must be a file inside the repository")
    destination = workspace / "workspace.sqlite3"
    if destination.exists() and not force:
        raise ValueError("target exists; restore into a new workspace or explicitly use --force")
    if source == destination:
        raise ValueError("backup and destination must differ")
    with db.workspace_lock(workspace):
        # Also reject a server started before workspace locks were introduced.
        pid = workspace / "server.pid"
        if pid.exists():
            try:
                os.kill(int(pid.read_text()), 0)
            except (ProcessLookupError, ValueError):
                pass
            else:
                raise ValueError("workspace server is running")
        incoming = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        incoming.row_factory = sqlite3.Row  # the conversion status read below
        fd, name = tempfile.mkstemp(prefix="restore-", suffix=".sqlite3", dir=workspace)
        os.close(fd)
        staged = Path(name)
        try:
            check = incoming.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise ValueError("backup failed integrity_check")
            version = incoming.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
            if version != db.SCHEMA_VERSION:
                raise ValueError(f"backup must use schema {db.SCHEMA_VERSION}")
            expected = sqlite3.connect(":memory:", isolation_level=None)
            expected.row_factory = sqlite3.Row
            try:
                db.init_schema(expected)
                required = {r[0] for r in expected.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'evidence_fts%'")}
                required -= set(db.OPTIONAL_TABLES)  # older backups lack them; created below
                tables = {r[0] for r in incoming.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                # A converted workspace has retired this store; whether the
                # backup's own conversion marker allows that is decided last,
                # once the tables the conversion status reads are checked.
                retired = db.RETIRED_SAVED_SEARCH_STORE not in tables
                if retired:
                    required.discard(db.RETIRED_SAVED_SEARCH_STORE)
                if not required <= tables:
                    raise ValueError("backup has missing tables")
                # Optional tables may be absent, but when present they get the same shape checks.
                for table in required | (set(db.OPTIONAL_TABLES) & tables):
                    columns = {tuple(r)[1:6] for r in expected.execute(f"PRAGMA table_info({table})")}
                    actual = {tuple(r)[1:6] for r in incoming.execute(f"PRAGMA table_info({table})")}
                    if not columns <= actual:
                        raise ValueError(f"backup has incompatible columns: {table}")
                    references = {tuple(r)[2:] for r in expected.execute(f"PRAGMA foreign_key_list({table})")}
                    if not references <= {tuple(r)[2:] for r in incoming.execute(f"PRAGMA foreign_key_list({table})")}:
                        raise ValueError(f"backup has missing references: {table}")
                if incoming.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("backup has invalid references")
                if retired and not db.saved_search_store_retired(incoming):
                    raise ValueError("backup has missing tables: customer_saved_searches is"
                                     " absent but the backup's customer conversion is not"
                                     " complete (marker not complete or definitions pending)")
            finally:
                expected.close()
            target = sqlite3.connect(staged)
            try:
                incoming.backup(target)
                db.ensure_evidence_membership(target)
                db.ensure_ask_schema(target)
                db.ensure_episode_review_schema(target)
                from earwitness.pipeline import jobs
                jobs.ensure_tables(target)
                target.commit()
                # The restored file must not present a token any server
                # memoized for the replaced (or an earlier) workspace state.
                target.isolation_level = None
                target.row_factory = sqlite3.Row
                # Derived corpus tables a backup may predate: built from the
                # restored source versions, as upgrade does.
                from earwitness.app import ad_reads, lineage, transcript_index
                ad_reads.ensure_schema(target)
                transcript_index.ensure_schema(target)
                lineage.ensure_schema(target)
                db.ensure_view_state_guards(target, fresh_token=True)
                with db.transaction(target, write=True):
                    prepared = importer.prepare_workspace(target, "restore", REPO)
                target.execute("PRAGMA journal_mode=DELETE")
            finally:
                target.close()
            # Preserve the stopped target and checkpoint its sidecars before replacement.
            if destination.exists():
                previous = sqlite3.connect(destination)
                recovery = workspace / ("before-restore-" + str(os.getpid()) + ".sqlite3")
                if recovery.exists():
                    raise ValueError("recovery backup already exists")
                saved = sqlite3.connect(recovery)
                try:
                    previous.backup(saved)
                    previous.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    previous.execute("PRAGMA journal_mode=DELETE")
                finally:
                    previous.close()
                    saved.close()
                recovery.chmod(0o600)
            os.replace(staged, destination)
            destination.chmod(0o600)
            return {"restored": str(destination), "bytes": destination.stat().st_size,
                    "ad_reads": prepared}
        finally:
            incoming.close()
            staged.unlink(missing_ok=True)


def server_running(workspace):
    """Refuse explicit upgrade/restore while a server holds the workspace."""
    pid = workspace / "server.pid"
    if not pid.exists():
        return False
    try:
        os.kill(int(pid.read_text()), 0)
    except (ProcessLookupError, ValueError):
        return False
    return True


def recovery_ledger():
    """Operator-controlled recovery authority outside any workspace payload."""
    RECOVERY_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(RECOVERY_DIR, 0o700)
    path = RECOVERY_DIR / "ledger.sqlite3"
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE IF NOT EXISTS recovery_authority (workspace_id TEXT PRIMARY KEY,"
                " authority_schema INTEGER NOT NULL, updated_at TEXT NOT NULL,"
                " note TEXT NOT NULL DEFAULT '')")
    con.commit()
    os.chmod(path, 0o600)
    return con


def quarantine_marker(workspace):
    return workspace / "quarantine.json"


def is_quarantined(workspace):
    marker = quarantine_marker(workspace)
    if not marker.exists():
        return None
    try:
        return json.loads(marker.read_text())
    except (OSError, ValueError):
        return {"reason": "unreadable quarantine marker"}


def profile_scopes(allow_campaigns=False, allow_watchlists=False, allow_notes=False):
    scopes = list(READER_BASE_SCOPES)
    if allow_campaigns:
        scopes.append(OPTIONAL_READ_SCOPES["campaigns"])
    if allow_watchlists:
        scopes.append(OPTIONAL_READ_SCOPES["watchlists"])
    if allow_notes:
        scopes.append(OPTIONAL_READ_SCOPES["notes"])
    return scopes


def _backup_destination(workspace, backup_rel, default_name):
    """Recovery backup path: a new separate repository-local file."""
    if backup_rel:
        output = (REPO / backup_rel).resolve()
        if REPO not in output.parents or output == workspace / "workspace.sqlite3":
            raise ValueError("backup destination must be a separate repository-local file")
    else:
        output = workspace / default_name
    if output.exists():
        raise ValueError(f"backup destination exists: {output}; choose a new filename")
    return output


def _legacy_audio_root(value):
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"legacy audio root is not a directory: {root}")
    return root


@contextlib.contextmanager
def _legacy_audio_cleanup():
    """Collects registered workspace copies; deletes them if the block fails."""
    created = []
    try:
        yield created
    except BaseException:
        for path in created:
            Path(path).unlink(missing_ok=True)
        raise


def _register_legacy_audio(con, workspace, root, actor, created):
    from earwitness.app import media
    receipt = media.register_legacy_local_audio(con, workspace, root, actor=actor)
    created.extend(receipt.pop("created_files"))
    return receipt


def upgrade_workspace(workspace, backup_rel=None, allow_campaigns=False,
                      allow_watchlists=False, allow_notes=False, legacy_audio_root=None):
    """Explicit backed-up upgrade to the current schema with fixed identity.

    Requires the workspace server stopped and the workspace lock. Takes a
    recovery backup first, migrates (init_schema covers 3->4->5->6->7->8 with
    history backfill plus customer and retained B1–B5 resource tables; legacy
    campaign mode stays NULL, never a fabricated default), verifies, then
    establishes the fixed owner
    identity, preserves (or seeds) the read profile, and records the
    recovery-authority ledger entry. Already-current
    workspaces report upgraded=False with exit 0.

    legacy_audio_root (optional, an existing directory) registers the old
    app's in-place .pilot-corpus pilot/benchmark audio as pinned workspace
    recordings before the first source publication; the receipt is
    returned as "legacy_audio". It is refused on an already-current
    workspace rather than silently skipped (use register_legacy_audio).
    """
    if legacy_audio_root is not None:
        legacy_audio_root = _legacy_audio_root(legacy_audio_root)
    if server_running(workspace):
        raise ValueError("workspace server is running; stop it before upgrade")
    with db.workspace_lock(workspace):
        con = db.connect(workspace)
        try:
            before_version = con.execute(
                "SELECT COALESCE(MAX(version),0) FROM schema_version").fetchone()[0]
        finally:
            con.close()
        if before_version > db.SCHEMA_VERSION:
            raise ValueError(f"workspace schema v{before_version} is newer than this build"
                             f" (v{db.SCHEMA_VERSION})")
        if before_version == db.SCHEMA_VERSION and legacy_audio_root is not None:
            raise ValueError(f"workspace is already at schema v{before_version}; register"
                             " legacy local audio with the register-legacy-audio command"
                             " before the customer conversion")
        if before_version == db.SCHEMA_VERSION:
            con = db.connect(workspace)
            try:
                verification = db.verify_schema(con)
                from earwitness.app import conversion
                source_prepared = conversion.source_qualification(con)["qualified"]
            finally:
                con.close()
            if verification["problems"]:
                raise ValueError(f"workspace failed verification: {verification['problems']}")
            if source_prepared:
                # Already current, but auxiliary tables/indexes added since
                # (job-ledger p01_job_profiles, evidence_source_membership) are
                # still created and backfilled: idempotent, no backup needed.
                from earwitness.pipeline import jobs
                con = db.connect(workspace)
                try:
                    jobs.ensure_tables(con)
                    db.ensure_evidence_membership(con)
                    con.commit()
                    from earwitness.app import ad_reads
                    ad_reads.ensure_schema(con)
                    db.ensure_view_state_guards(con)
                    with db.transaction(con, write=True):
                        # What a load leaves, from the stored source versions:
                        # one active row per stable key (human work moved onto
                        # it), customer-ad side tables, transcript index.
                        ad_tables = importer.prepare_workspace(con, "upgrade", REPO)
                        receipt = conversion.record_local_readiness(con, "upgrade")
                finally:
                    con.close()
                return {"upgraded": False, "version": before_version,
                        "verification": verification, "ad_reads": ad_tables,
                        "local_ui_readiness": receipt}
        output = _backup_destination(workspace, backup_rel,
                                     f"before-upgrade-{before_version}.sqlite3")
        con = db.connect(workspace)
        try:
            before = snapshot_counts(con)
            result = ops.backup_to(con, output)
            output.chmod(0o600)
        finally:
            con.close()
        con = db.connect(workspace)
        try:
            db.init_schema(con)
            from earwitness.app import citations, customer
            legacy_audio = None
            with _legacy_audio_cleanup() as legacy_files, db.transaction(con, write=True):
                customer.rebuild_customer_show_map(con)
                if legacy_audio_root is not None:
                    # Before the first publication, so the first published
                    # source pins already carry the recording.
                    legacy_audio = _register_legacy_audio(
                        con, workspace, legacy_audio_root, "upgrade", legacy_files)
                for row in con.execute("SELECT id FROM episodes").fetchall():
                    citations.publish_episode_sources(con, row["id"], "upgrade")
            db.ensure_view_state_guards(con)
            with db.transaction(con, write=True):
                ad_tables = importer.prepare_workspace(con, "upgrade", REPO)
            verification = db.verify_schema(con)
            after = snapshot_counts(con)
            if verification["problems"]:
                raise ValueError(f"upgraded workspace failed verification: {verification['problems']}")
            for table, count in before["tables"].items():
                if table in after["tables"] and after["tables"][table] < count:
                    raise ValueError(f"upgrade lost rows in {table}: {count} ->"
                                     f" {after['tables'][table]}")
            existing_profile = con.execute(
                "SELECT value FROM meta WHERE key='agent_profile'").fetchone()
            if existing_profile is not None:
                # Preservation rule (Product-9 §4.3): an established reader
                # profile survives upgrade untouched; CLI flags never widen
                # or reset it. agent_config enforces no-expansion separately.
                profile = json.loads(existing_profile["value"])
            else:
                scopes = profile_scopes(allow_campaigns, allow_watchlists, allow_notes)
                profile = {"profile": "reader", "scopes": scopes,
                           "owner": "owner-1", "workspace_id": "local",
                           "updated_at": db.utcnow()}
                con.execute("INSERT INTO meta (key, value) VALUES ('agent_profile', ?)"
                            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (json.dumps(profile),))
            identity = db.ensure_workspace_identity(con)
            con.commit()
        finally:
            con.close()
        ledger = recovery_ledger()
        try:
            ledger.execute("INSERT INTO recovery_authority (workspace_id, authority_schema,"
                           " updated_at, note) VALUES (?, ?, ?, ?)"
                           " ON CONFLICT(workspace_id) DO UPDATE SET authority_schema=excluded."
                           "authority_schema, updated_at=excluded.updated_at, note=excluded.note",
                           (identity, db.SCHEMA_VERSION, db.utcnow(), "upgrade"))
            ledger.commit()
        finally:
            ledger.close()
        return {"upgraded": True, "backup": str(output), "backup_bytes": result["bytes"],
                "ad_reads": ad_tables,
                "before_version": before_version, "after_version": db.SCHEMA_VERSION,
                "before": before, "after": after, "verification": verification,
                "profile": profile, "workspace_identity": identity,
                "legacy_audio": legacy_audio}


def register_legacy_audio(workspace, legacy_audio_root, backup_rel=None):
    """Register legacy local audio on a current, not yet converted workspace.

    For a workspace upgraded without --legacy-audio-root. Same guards as
    upgrade: server stopped, workspace lock, a recovery backup first, then
    one write transaction. Refused once the customer conversion has run:
    copy the before-upgrade backup over workspace.sqlite3 with SQLite's
    backup API (restore accepts only the current schema) and upgrade with
    the flag instead.
    """
    from earwitness.app import conversion
    root = _legacy_audio_root(legacy_audio_root)
    if not (workspace / "workspace.sqlite3").is_file():
        raise ValueError(f"workspace database does not exist: {workspace / 'workspace.sqlite3'}")
    if server_running(workspace):
        raise ValueError("workspace server is running; stop it before registering legacy audio")
    with db.workspace_lock(workspace):
        con = db.connect(workspace)
        try:
            version = con.execute(
                "SELECT COALESCE(MAX(version),0) FROM schema_version").fetchone()[0]
            if version != db.SCHEMA_VERSION:
                raise ValueError(f"workspace schema is v{version}, not v{db.SCHEMA_VERSION};"
                                 " run upgrade with --legacy-audio-root instead")
            if con.execute("SELECT 1 FROM meta WHERE key=?",
                           (conversion.MARKER_KEY,)).fetchone() is not None:
                raise ValueError("the customer conversion already ran on this workspace;"
                                 " copy the before-upgrade backup over workspace.sqlite3 with"
                                 " SQLite's backup API (restore accepts only schema"
                                 f" {db.SCHEMA_VERSION}), then run upgrade with"
                                 " --legacy-audio-root to register legacy audio")
            verification = db.verify_schema(con)
            if verification["problems"]:
                raise ValueError(f"workspace failed verification: {verification['problems']}")
            output = _backup_destination(workspace, backup_rel,
                                         "before-register-legacy-audio.sqlite3")
            backup = ops.backup_to(con, output)
            output.chmod(0o600)
            with _legacy_audio_cleanup() as created, db.transaction(con, write=True):
                receipt = _register_legacy_audio(con, workspace, root,
                                                 "register-legacy-audio", created)
        finally:
            con.close()
    return {"operation": "register_legacy_audio", "backup": str(output),
            "backup_bytes": backup["bytes"], "legacy_audio": receipt}


def record_alias(workspace, alias, target, dry_run=False, backup_rel=None,
                 actor="operator"):
    """Record `alias` as an explicit alias of `target` (episode_alias).

    Dry run: shared lock, read-only handle, prints the change, writes
    nothing. Run: server stopped, workspace lock, a recovery backup first
    (only when something changes; named for the alias, target and UTC
    time, never overwritten), then one write transaction with its audit
    row. An already recorded alias is a no-op."""
    import re
    from datetime import datetime, timezone
    from earwitness.app import episode_alias
    path = workspace / "workspace.sqlite3"
    if not path.is_file():
        raise ValueError(f"workspace database does not exist: {path}")
    if dry_run:
        with db.workspace_lock(workspace, shared=True):
            con = db.open_existing(path, readonly=True)
            try:
                return episode_alias.record(con, alias, target, actor=actor, dry_run=True)
            finally:
                con.close()
    if server_running(workspace):
        raise ValueError("workspace server is running; stop it before recording an alias")
    with db.workspace_lock(workspace):
        con = db.open_existing(path, readonly=False)
        try:
            with db.transaction(con, write=False):
                planned = episode_alias.plan(con, alias, target)
            if not planned["changed"]:
                return episode_alias.record(con, alias, target, actor=actor)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            names = "-to-".join(re.sub(r"[^\w.-]", "_", v) for v in (alias, target))
            output = _backup_destination(workspace, backup_rel,
                                         f"before-record-alias-{names}-{stamp}.sqlite3")
            ops.backup_to(con, output)
            output.chmod(0o600)
            result = episode_alias.record(con, alias, target, actor=actor)
            result["backup"] = str(output)
            return result
        finally:
            con.close()


def import_coverage(workspace, dry_run=False, run_id=None, actor="operator",
                    backup_rel=None):
    """D-2 legacy coverage import (coverage_import), the record-alias pattern.

    Dry run: shared lock, read-only handle on the existing current-schema
    database, writes nothing. Run: server stopped, workspace lock, a
    recovery backup first (only when something will be recorded), then one
    write transaction with its audit row. A run with nothing to record
    writes nothing.

    Operator note (R3-04): a dry run never changes the database bytes, but
    on a workspace that has no -wal/-shm or workspace.lock files yet it may
    create them (an empty -wal, the -shm index, the lock), as any SQLite
    reader and record-alias do. Opening with immutable=1 instead is unsafe
    while a server runs."""
    from earwitness.app import coverage_import
    path = workspace / "workspace.sqlite3"
    if not path.is_file():
        raise ValueError(f"workspace database does not exist: {path}")
    if dry_run:
        with db.workspace_lock(workspace, shared=True):
            con = db.open_existing(path, readonly=True)
            try:
                return coverage_import.run(con, dry_run=True, run_id=run_id, actor=actor)
            finally:
                con.close()
    if server_running(workspace):
        raise ValueError("workspace server is running; stop it before importing coverage")
    with db.workspace_lock(workspace):
        con = db.open_existing(path, readonly=False)
        try:
            planned = coverage_import.run(con, dry_run=True, run_id=run_id, actor=actor)
            if not planned["would_record_episodes"]:
                return coverage_import.run(con, dry_run=False, run_id=planned["run_id"],
                                           actor=actor)
            output = _backup_destination(workspace, backup_rel,
                                         f"before-import-coverage-{planned['run_id']}.sqlite3")
            ops.backup_to(con, output)
            output.chmod(0o600)
            result = coverage_import.run(con, dry_run=False, run_id=planned["run_id"], actor=actor)
            result["backup"] = str(output)
            return result
        finally:
            con.close()


def import_jev(workspace, corpus, dry_run=False, run_id=None, actor="operator",
               backup_rel=None):
    """R1-06 A: import a Jev re-screen of the current transcript copies
    (jev_rescreen.import_results), the record-alias pattern.

    Dry run: shared lock, read-only handle, writes nothing. Run: server
    stopped, workspace lock, a recovery backup first (only when something
    will be imported), then one write transaction with its audit row. A
    run with nothing to import writes nothing."""
    from earwitness.app import jev_rescreen
    path = workspace / "workspace.sqlite3"
    if not path.is_file():
        raise ValueError(f"workspace database does not exist: {path}")
    corpus = Path(corpus)
    if not (corpus / "jev").is_dir():
        raise ValueError(f"no Jev results under {corpus / 'jev'}")
    if dry_run:
        with db.workspace_lock(workspace, shared=True):
            con = db.open_existing(path, readonly=True)
            try:
                return jev_rescreen.import_results(con, corpus, dry_run=True, run_id=run_id,
                                                   actor=actor)
            finally:
                con.close()
    if server_running(workspace):
        raise ValueError("workspace server is running; stop it before importing Jev results")
    with db.workspace_lock(workspace):
        con = db.open_existing(path, readonly=False)
        try:
            planned = jev_rescreen.import_results(con, corpus, dry_run=True, run_id=run_id,
                                                  actor=actor)
            if not planned["would_import"]:
                # Nothing to import: a real run that writes nothing.
                return jev_rescreen.import_results(con, corpus, dry_run=False,
                                                   run_id=planned["run_id"], actor=actor)
            output = _backup_destination(workspace, backup_rel,
                                         f"before-import-jev-{planned['run_id']}.sqlite3")
            ops.backup_to(con, output)
            output.chmod(0o600)
            result = jev_rescreen.import_results(con, corpus, dry_run=False,
                                                 run_id=planned["run_id"], actor=actor)
            result["backup"] = str(output)
            return result
        finally:
            con.close()


def import_names(workspace, corpus, dry_run=False, run_id=None, actor="operator",
                 backup_rel=None, curate=True):
    """Suggested brand names for the re-screen's jev reads
    (name_import.import_names), the import-jev pattern.

    Dry run: shared lock, read-only handle, writes nothing. Run: server
    stopped, workspace lock, a recovery backup first (only when names or the
    brand curation will write), then one write transaction with its audit
    rows. A run with nothing to write writes nothing. It curates the known
    brand list first; curate=False (--no-curate) leaves that out."""
    from earwitness.app import name_import
    path = workspace / "workspace.sqlite3"
    if not path.is_file():
        raise ValueError(f"workspace database does not exist: {path}")
    corpus = Path(corpus)
    if not (corpus / "names").is_dir():
        raise ValueError(f"no names under {corpus / 'names'}"
                         " (run corpus_run.py name-backfill --name-gliner none first)")
    if dry_run:
        with db.workspace_lock(workspace, shared=True):
            con = db.open_existing(path, readonly=True)
            try:
                return name_import.import_names(con, corpus, dry_run=True, run_id=run_id,
                                                actor=actor, curate=curate)
            finally:
                con.close()
    if server_running(workspace):
        raise ValueError("workspace server is running; stop it before importing names")
    with db.workspace_lock(workspace):
        con = db.open_existing(path, readonly=False)
        try:
            planned = name_import.import_names(con, corpus, dry_run=True, run_id=run_id,
                                               actor=actor, curate=curate)
            if not planned["would_write"] and not (planned.get("curated") or {}).get(
                    "would_write"):
                return name_import.import_names(con, corpus, dry_run=False,
                                                run_id=planned["run_id"], actor=actor,
                                                curate=curate)
            output = _backup_destination(workspace, backup_rel,
                                         f"before-import-names-{planned['run_id']}.sqlite3")
            ops.backup_to(con, output)
            output.chmod(0o600)
            result = name_import.import_names(con, corpus, dry_run=False,
                                              run_id=planned["run_id"], actor=actor,
                                              curate=curate)
            result["backup"] = str(output)
            return result
        finally:
            con.close()


def curate_brands(workspace, corpus=None, dry_run=False, run_id=None, actor="operator",
                  backup_rel=None, add=None):
    """Add brands to the known list (brand_curate), the import-names pattern.

    Without `add`: the rule's curation of the off-list brands the names stage
    heard. With `add` (a dict key, display, approved_by, note): one
    Reno-approved manual entry; no corpus needed.

    Dry run: shared lock, read-only handle, writes nothing. Run: server
    stopped, workspace lock, a recovery backup first (only when something
    will be written), then one write transaction with its audit row."""
    from earwitness.app import brand_curate
    path = workspace / "workspace.sqlite3"
    if not path.is_file():
        raise ValueError(f"workspace database does not exist: {path}")
    if add is None:
        if corpus is None:
            raise ValueError("--corpus is required (or --add to enter one brand)")
        corpus = Path(corpus)
        if not (corpus / "names").is_dir():
            raise ValueError(f"no names under {corpus / 'names'}"
                             " (run corpus_run.py name-backfill --name-gliner none first)")

    def run(con, dry, rid):
        if add is not None:
            return brand_curate.add_manual(con, add["key"], add["display"],
                                           approved_by=add["approved_by"], note=add["note"],
                                           dry_run=dry, run_id=rid, actor=actor)
        return brand_curate.curate_brands(con, corpus, dry_run=dry, run_id=rid, actor=actor)
    if dry_run:
        with db.workspace_lock(workspace, shared=True):
            con = db.open_existing(path, readonly=True)
            try:
                return run(con, True, run_id)
            finally:
                con.close()
    if server_running(workspace):
        raise ValueError("workspace server is running; stop it before curating brands")
    with db.workspace_lock(workspace):
        con = db.open_existing(path, readonly=False)
        try:
            planned = run(con, True, run_id)
            if not planned["would_write"]:
                return run(con, False, planned["run_id"])
            output = _backup_destination(workspace, backup_rel,
                                         f"before-curate-brands-{planned['run_id']}.sqlite3")
            ops.backup_to(con, output)
            output.chmod(0o600)
            result = run(con, False, planned["run_id"])
            result["backup"] = str(output)
            return result
        finally:
            con.close()


def brand_suggest(workspace, corpus, dry_run=False, force=False, run_id=None, max_calls=100,
                  workers=3, agy="agy", model=None, log_dir=None, actor="operator",
                  backup_rel=None):
    """Brand suggestions (brand_suggest): one stored suggestion per off-list
    heard name, never applied until the operator confirms it in Ad review.

    Dry run: shared lock, read-only handle, prints the plan; no call, no
    write. Run: plan on a read-only handle (allowed while the server runs),
    ask agy (calls, or the call cache; no lock held while agy runs), then the
    write: refused while the server runs (the answers are saved in the log
    folder; run again after stopping it, no new calls), else the workspace
    lock, a re-plan, a recovery backup only when something will be stored,
    then one write transaction with its audit row."""
    from earwitness.app import brand_suggest as bs
    path = workspace / "workspace.sqlite3"
    if not path.is_file():
        raise ValueError(f"workspace database does not exist: {path}")
    corpus = Path(corpus)
    if not (corpus / "names").is_dir():
        raise ValueError(f"no names under {corpus / 'names'}"
                         " (run corpus_run.py name-backfill --name-gliner none first)")
    run_id = run_id or bs._stamp()
    log_dir = Path(log_dir).resolve() if log_dir else workspace / "brand-suggest"
    model = model or bs.MODEL
    if dry_run:
        with db.workspace_lock(workspace, shared=True):
            con = db.open_existing(path, readonly=True)
            try:
                return bs.plan(con, corpus, force=force, run_id=run_id)
            finally:
                con.close()

    def asked(plan_):
        return [n for n in plan_["names"] if n["decision"] == "ask"]
    with db.workspace_lock(workspace, shared=True):
        con = db.open_existing(path, readonly=True)
        try:
            planned = bs.plan(con, corpus, force=force, run_id=run_id)
        finally:
            con.close()
    if force and asked(planned) and server_running(workspace):
        raise ValueError("--force asks again with new calls and its answers cannot be saved while"
                         " the workspace server runs: stop the server first (nothing was asked)")
    answers = bs.ask(asked(planned), agy=agy, log_dir=log_dir, run_id=run_id,
                     max_calls=max_calls, workers=workers, model=model, use_cache=not force)
    calls, cached = sum(r["calls"] for r in answers), sum(bool(r["cached"]) for r in answers)
    if server_running(workspace):
        # Never take the write lock under a running server.
        if any(r["ok"] for r in answers):
            raise ValueError(f"answers saved in {log_dir}; stop the server and run again:"
                             " no new calls")
        result = bs.store(None, planned, answers, run_id=run_id, corpus=corpus, actor=actor)
        result["calls"], result["cached"], result["backup"] = calls, cached, None
        return result   # nothing to store: no database access
    with db.workspace_lock(workspace):
        con = db.open_existing(path, readonly=False)
        try:
            plan_ = bs.plan(con, corpus, force=force, run_id=run_id)
            # Only the answers this run has: a forced run stores what it asked, not the
            # older answers of the names it did not reach.
            fresh = {r["name"] for r in answers if r["ok"]}
            results = bs.ask([n for n in asked(plan_) if not force or n["name"] in fresh],
                             agy=agy, log_dir=log_dir, run_id=run_id, max_calls=0, workers=1,
                             model=model, cache_only=True)
            backup = None
            if any(r["ok"] for r in results):
                backup = _backup_destination(workspace, backup_rel,
                                             f"before-brand-suggest-{run_id}.sqlite3")
                ops.backup_to(con, backup)
                backup.chmod(0o600)
            result = bs.store(con, plan_, results, run_id=run_id, corpus=corpus, actor=actor)
            # What this run asked and called (the locked step only reads the cache).
            result["calls"], result["cached"] = calls, cached
            result["backup"] = str(backup) if backup else None
            return result
        finally:
            con.close()


def carry_alert_baselines(workspace, names_run_id, dry_run=False, run_id=None,
                          actor="operator", backup_rel=None):
    """R1-04 A: alert baselines absorb the reads a names run named
    (alert_carry.carry_alert_baselines), the import-names pattern.

    Dry run: shared lock, read-only handle, writes nothing. Run: server
    stopped, workspace lock, a recovery backup first (only when something
    will be written), then one write transaction with its audit row. A run
    with nothing to carry writes nothing."""
    from earwitness.app import alert_carry
    path = workspace / "workspace.sqlite3"
    if not path.is_file():
        raise ValueError(f"workspace database does not exist: {path}")
    if dry_run:
        with db.workspace_lock(workspace, shared=True):
            con = db.open_existing(path, readonly=True)
            try:
                return alert_carry.carry_alert_baselines(con, names_run_id, dry_run=True,
                                                         run_id=run_id, actor=actor)
            finally:
                con.close()
    if server_running(workspace):
        raise ValueError("workspace server is running; stop it before carrying alert baselines")
    with db.workspace_lock(workspace):
        con = db.open_existing(path, readonly=False)
        try:
            planned = alert_carry.carry_alert_baselines(con, names_run_id, dry_run=True,
                                                        run_id=run_id, actor=actor)
            if not planned["would_write"]:
                return planned | {"dry_run": False}
            output = _backup_destination(workspace, backup_rel,
                                         f"before-carry-alert-baselines-{planned['run_id']}.sqlite3")
            ops.backup_to(con, output)
            output.chmod(0o600)
            result = alert_carry.carry_alert_baselines(con, names_run_id, dry_run=False,
                                                       run_id=planned["run_id"], actor=actor)
            result["backup"] = str(output)
            return result
        finally:
            con.close()


def agent_config(workspace, client, profile="reader", allow_campaigns=False,
                 allow_watchlists=False, allow_notes=False):
    """Emit a real non-secret client configuration for an upgraded workspace.

    Refuses incompatible or unupgraded state and quarantined workspaces, and
    never expands permissions beyond the profile established at upgrade time.
    The stdio script path is package D's deliverable; the interpreter,
    workspace, and profile paths emitted here are real.
    """
    if client not in AGENT_CLIENTS:
        raise ValueError(f"client must be one of {list(AGENT_CLIENTS)}")
    if profile != "reader":
        raise ValueError("only the reader profile exists in this contract")
    quarantined = is_quarantined(workspace)
    if quarantined:
        raise ValueError(f"workspace is quarantined ({quarantined.get('reason')});"
                         " reconcile recovery authority first")
    with db.workspace_lock(workspace, shared=True):
        con = db.open_existing(workspace / "workspace.sqlite3", readonly=True)
        try:
            version = con.execute(
                "SELECT COALESCE(MAX(version),0) FROM schema_version").fetchone()[0]
            if version != db.SCHEMA_VERSION:
                raise ValueError(f"workspace schema is v{version}, agent access requires"
                                 f" v{db.SCHEMA_VERSION}; run the upgrade command first")
            row = con.execute("SELECT value FROM meta WHERE key='agent_profile'").fetchone()
        finally:
            con.close()
    if row is None:
        raise ValueError("workspace has no agent profile; re-run the upgrade command")
    stored = json.loads(row["value"])
    requested = profile_scopes(allow_campaigns, allow_watchlists, allow_notes)
    widened = [scope for scope in requested if scope not in stored["scopes"]]
    if widened:
        raise ValueError(f"refusing to expand permissions beyond the upgrade profile: {widened}")
    from earwitness.app import connections
    connections.prepare_local_reader_control(workspace, stored.get("workspace_id") or "local")
    interpreter = sys.executable
    script = str(REPO / "scripts" / "app_mcp.py")
    # The launcher takes explicit opt-in flags, not a profile name: emit
    # exactly the flags for the requested (never widened) scopes so the
    # snippet launches as printed. Package F fixed this: "--profile
    # reader" was never a launcher flag and the snippet failed to start.
    args = [script, "--workspace", str(workspace.resolve())]
    if "campaigns:read" in requested:
        args.append("--allow-campaigns")
    if "watchlists:read" in requested:
        args.append("--allow-watchlists")
    if "notes:read" in requested:
        args.append("--allow-notes")
    servers = {"earwitness": {"command": interpreter, "args": args}}
    if client == "codex":
        args_toml = ", ".join(f'"{a}"' for a in args)
        snippet = ("[mcp_servers.earwitness]\n"
                   f"command = \"{interpreter}\"\nargs = [{args_toml}]\n")
    else:
        snippet = json.dumps({"mcpServers": servers}, indent=2)
    config = {"client": client, "profile": profile, "scopes": requested,
              "workspace": str(workspace.resolve()), "interpreter": interpreter,
              "script": script, "snippet": snippet,
              "note": "no secrets embedded; launcher is scripts/app_mcp.py"}
    if not Path(script).exists():
        print("warning: scripts/app_mcp.py is not installed yet (package D);"
              " configuration paths are otherwise real", file=sys.stderr)
    return config


def restore_quarantined(workspace, backup_rel, force):
    """Restore, then hold quarantine until current recovery authority agrees.

    Delegates the staged verified replacement to restore(); afterwards the
    restored workspace stays quarantined unless the recovery ledger carries
    current authority for exactly this workspace identity. An old backup's
    ledger entry, or authority for another workspace, is insufficient.
    """
    result = restore(workspace, backup_rel, force)
    con = db.connect(workspace)
    try:
        identity = db.workspace_identity(con)
        version = con.execute("SELECT COALESCE(MAX(version),0) FROM schema_version").fetchone()[0]
    finally:
        con.close()
    reason = None
    if not identity:
        reason = "restored workspace has no stable identity"
    else:
        ledger = recovery_ledger()
        try:
            entry = ledger.execute("SELECT authority_schema FROM recovery_authority"
                                   " WHERE workspace_id=?", (identity,)).fetchone()
        finally:
            ledger.close()
        if entry is None:
            reason = f"no current recovery authority for workspace {identity}"
        elif entry[0] < version:
            reason = (f"recovery authority schema v{entry[0]} is older than restored v{version}")
    if reason:
        quarantine_marker(workspace).write_text(json.dumps(
            {"reason": reason, "restored_at": db.utcnow(),
             "backup": backup_rel, "workspace_identity": identity}))
        result["quarantined"] = True
        result["quarantine_reason"] = reason
    else:
        result["quarantined"] = False
    return result


def reconcile_workspace(workspace):
    """Replay current recovery authority and lift quarantine when covered."""
    marker = quarantine_marker(workspace)
    if not marker.exists():
        raise ValueError("workspace is not quarantined")
    if server_running(workspace):
        raise ValueError("workspace server is running; stop it before reconcile")
    with db.workspace_lock(workspace):
        con = db.connect(workspace)
        try:
            identity = db.workspace_identity(con)
            version = con.execute(
                "SELECT COALESCE(MAX(version),0) FROM schema_version").fetchone()[0]
        finally:
            con.close()
        if not identity:
            raise ValueError("workspace has no stable identity; cannot reconcile")
        ledger = recovery_ledger()
        try:
            entry = ledger.execute("SELECT authority_schema FROM recovery_authority"
                                   " WHERE workspace_id=?", (identity,)).fetchone()
        finally:
            ledger.close()
        if entry is None or entry[0] < version:
            raise ValueError("no current recovery authority for this workspace;"
                             " restore stays quarantined")
        marker.unlink()
        return {"reconciled": True, "workspace_identity": identity, "version": version}


def snapshot_counts(con):
    """Row/relation/review/export counts plus source pins for rehearsal evidence."""
    counts = db.table_counts(con)
    pins = [dict(r) for r in con.execute(
        "SELECT source_key, MAX(version_no) AS latest, COUNT(*) AS versions"
        " FROM source_versions GROUP BY source_key ORDER BY source_key").fetchall()] \
        if "source_versions" in counts else []
    review_states = [dict(r) for r in con.execute(
        "SELECT COALESCE(judgment, 'unreviewed') AS judgment, COUNT(*) AS n"
        " FROM evidence LEFT JOIN reviews ON reviews.evidence_id=evidence.id"
        " GROUP BY judgment ORDER BY judgment").fetchall()] \
        if "evidence" in counts else []
    export_count = counts.get("exports", 0)
    return {"tables": counts, "source_pins": pins,
            "review_states": review_states, "exports": export_count}


# Files scanned for leftover V1 saved-search runtime aliases. Tracker
# criteria grammar (customer.parse_query) is the K08 contract surface,
# not an alias, and is intentionally unscanned.
_CUTOVER_RUNTIME_FILES = (
    "earwitness/app/customer.py",
    "earwitness/app/services.py",
    "earwitness/app/asgi.py",
    "scripts/app_server.py",
)
_CUTOVER_BANNED_TOKENS = (
    '"/searches"', "'/searches'", '"/saved-searches"', "'/saved-searches'",
    "def save_search(", "def list_searches(", "def get_search(",
    "def customer_save_search(", "def customer_saved_searches(",
    "customer_saved_searches",
)


def customer_cutover_check(workspace, manifest=None):
    """Pure no-create inventory, reviewed disposition and source/API gate."""
    from earwitness.app import conversion as _conversion
    con = db.open_existing(workspace / "workspace.sqlite3", readonly=True)
    try:
        with db.transaction(con, write=False):
            report = _conversion.status(con)
            source = _conversion.source_qualification(con)
            readiness = _conversion.readiness(con)
            manifest_problem = None
            if manifest is not None:
                try:
                    _conversion.validate_manifest(con, manifest)
                except (ops.BadInput, ops.MissingPrecondition) as exc:
                    manifest_problem = str(exc)
    finally:
        con.close()
    problems = []
    if report["pending_definitions"]:
        problems.append("pending definitions: " + ", ".join(report["pending_definitions"]))
    if manifest_problem:
        problems.append(manifest_problem)
    if not source["qualified"]:
        problems.append("source qualification unavailable")
    if not readiness["ready"]:
        problems.append("source-bound runtime/semantic readiness is incomplete")
    if not report["store_retired"]:
        problems.append("obsolete customer_saved_searches store still present")
    for rel in _CUTOVER_RUNTIME_FILES:
        path = REPO / rel
        try:
            text = path.read_text()
        except OSError:
            problems.append(f"runtime file unreadable: {rel}")
            continue
        for token in _CUTOVER_BANNED_TOKENS:
            if token in text:
                problems.append(f"{rel} still defines {token}")
    ok = not problems
    return {"ok": ok, "problems": problems, "conversion": report, "source": source, "readiness": readiness}


def semantic_convert_workspace(workspace, manifest, backup_rel=None):
    """Separate backed-up semantic operation on an explicitly prepared schema."""
    from earwitness.app import conversion
    if server_running(workspace):
        raise ValueError("workspace server must be stopped before semantic conversion")
    output = (REPO / backup_rel).resolve() if backup_rel else workspace / "before-customer-conversion.sqlite3"
    if REPO not in output.parents or output.exists() or output == workspace / "workspace.sqlite3":
        raise ValueError("semantic backup must be a new separate candidate-local file")
    with db.workspace_lock(workspace):
        con = db.open_existing(workspace / "workspace.sqlite3", readonly=False)
        try:
            conversion.validate_manifest(con, manifest)
            backup = ops.backup_to(con, output)
            output.chmod(0o600)
            with db.transaction(con, write=True):
                semantic = conversion.apply(con, actor="cli", manifest=manifest)
                retirement = conversion.retire_store(con, actor="cli")
            return {"operation": "semantic_conversion", "backup": str(output),
                    "backup_bytes": backup["bytes"], "conversion": semantic,
                    "retirement": retirement, "readiness": conversion.readiness(con)}
        finally:
            con.close()


def migrate_workspace(workspace, backup_rel):
    """Explicit migration path: backup -> migrate -> verify -> restore-verify."""
    output = (REPO / backup_rel).resolve()
    if REPO not in output.parents or output == workspace / "workspace.sqlite3":
        raise ValueError("backup destination must be a separate repository-local file")
    if output.exists():
        raise ValueError("backup destination exists; choose a new filename")
    with db.workspace_lock(workspace, shared=True):
        con = db.connect(workspace)
        try:
            before = snapshot_counts(con)
            before_version = con.execute(
                "SELECT COALESCE(MAX(version),0) FROM schema_version").fetchone()[0]
            result = ops.backup_to(con, output)
            output.chmod(0o600)
        finally:
            con.close()
        # Migrate in place (init_schema keeps its own before-schema-N backup).
        con = db.connect(workspace)
        try:
            db.init_schema(con)
            verification = db.verify_schema(con)
            after = snapshot_counts(con)
        finally:
            con.close()
        if verification["problems"]:
            raise ValueError(f"migrated workspace failed verification: {verification['problems']}")
        for table, count in before["tables"].items():
            if table in after["tables"] and after["tables"][table] < count:
                raise ValueError(f"migration lost rows in {table}: {count} -> {after['tables'][table]}")
        # Restore-verify: the backup opens cleanly and matches pre-migration state.
        check = sqlite3.connect(output.as_uri() + "?mode=ro", uri=True)
        try:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("backup failed integrity_check")
            check_version = check.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
            if check_version != before_version:
                raise ValueError("backup version does not match pre-migration workspace")
            if check.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("backup has invalid references")
        finally:
            check.close()
        return {"backup": str(output), "backup_bytes": result["bytes"],
                "before_version": before_version,
                "after_version": db.SCHEMA_VERSION,
                "before": before, "after": after,
                "verification": verification, "restore_verify": "ok"}


# -- Bounded show-artwork refresh (explicit CLI only) ---------------------------
#
# Owner-approved 2026-09-29: sequential refresh of recorded public feeds
# with a pinned, validated transport. No page request, startup, cache
# miss, or local image read ever invokes this path. Network I/O always
# completes (socket closed) before any SQLite write commits.
#
# Invariants (Astra-approved fetcher contract):
# - Only recorded feed URLs and metadata-selected artwork URLs are read;
#   enclosures and linked pages are never followed.
# - Reject credentials, unsupported schemes/ports, ambient proxies and
#   cookies (raw sockets: neither exists on this path).
# - Resolve and classify EVERY address, including mixed and IPv4-mapped
#   results; any non-global address rejects the whole host (fail closed).
# - Connect to the pinned address (no second DNS lookup) while TLS keeps
#   hostname/SNI/certificate verification against the original host.
# - Every redirect is re-validated; max three; no HTTPS downgrade.
# - 5s connect/read timeouts; 15s wall-clock deadline per request chain
#   including DNS, redirects, and slow trickles; 20-minute whole run cap.
# - Stream caps: 20,000,000-byte feed XML (parsed incrementally, at most
#   ART_FEED_MAX_DEPTH deep, stopping at the first channel image),
#   1,000,000-byte images; unbounded content encoding
#   rejected; JPEG/PNG/WebP only with signature + stdlib dimension parse
#   (<=1400x1400); atomic cache replacement; failures never overwrite
#   last-good bytes; validators never cross targets.


class ArtworkFetchError(Exception):
    """A control rejection with a safe reason (no response bytes kept)."""


ART_CONNECT_TIMEOUT_S = 5.0
ART_READ_TIMEOUT_S = 5.0
ART_CHAIN_DEADLINE_S = 15.0
ART_WHOLE_RUN_S = 20 * 60
ART_MAX_REDIRECTS = 3
ART_MAX_HEADER_BYTES = 65536
# The feed parse (ART-PERF-FIX1, APS-R1-01): expat fed in pieces, no tree.
# Elements nested deeper than ART_FEED_MAX_DEPTH refuse the feed, and so
# does a parse still undecided after ART_FEED_PARSE_S (checked between
# pieces). The DOCTYPE scan and the parse take the feed this many bytes
# at a time.
ART_FEED_MAX_DEPTH = 64
ART_FEED_PARSE_S = 5.0
_FEED_SCAN_PIECE = 1 << 16
_FEED_PARSE_PIECE = 1 << 16
ART_SCHEME_PORTS = {"http": 80, "https": 443}


# getaddrinfo runs in this short-lived child so a blocking resolver
# can be killed on timeout. The child receives only host/port on
# argv (no shell); the child itself refuses to write a serialized
# answer larger than the cap, and the parent re-checks fail-closed.
_DNS_CHILD_MAX_BYTES = 65536
_DNS_CHILD_PY = (
    "import json, socket, sys\n"
    "host, port = sys.argv[1], int(sys.argv[2])\n"
    "out = [[fam, sock[0]] for fam, _st, _pr, _cn, sock in "
    "socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]\n"
    f"data = json.dumps(out)\n"
    f"cap = {_DNS_CHILD_MAX_BYTES}\n"
    "if len(data) > cap:\n"
    "    sys.stderr.write('DNS answer exceeds child output cap')\n"
    "    sys.exit(2)\n"
    "sys.stdout.write(data)"
)


def artwork_default_resolver(host, port, timeout=None):
    """Production DNS for the pinned transport: raw addrinfo, no proxy.

    Resolution runs in a short-lived child process. With a timeout the
    parent kills and reaps the child on expiry (subprocess.run waits
    after killing), so no resolver work continues after the deadline.
    Returns raw addrinfo shaped like socket.getaddrinfo.
    """
    cmd = [sys.executable, "-c", _DNS_CHILD_PY, host, str(port)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ArtworkFetchError(
            f"DNS for {host} exceeded its chain deadline")
    except OSError as exc:
        raise ArtworkFetchError(
            f"DNS lookup failed to start for {host}: {exc}")
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace")[:200]
        raise ArtworkFetchError(
            f"DNS resolution failed for {host}: {err or 'child error'}")
    if len(proc.stdout) > _DNS_CHILD_MAX_BYTES:
        raise ArtworkFetchError(
            f"DNS for {host} returned an oversize answer")
    try:
        pairs = json.loads(proc.stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ArtworkFetchError(
            f"DNS for {host} returned malformed data: {exc}")
    answers = []
    for item in pairs if isinstance(pairs, list) else []:
        try:
            family, ip = int(item[0]), str(item[1])
        except (ValueError, TypeError, IndexError):
            raise ArtworkFetchError(
                f"DNS returned a malformed answer for {host}")
        answers.append((family, socket.SOCK_STREAM, 0, "", (ip, port)))
    return answers


def artwork_classify_ip(ip_str):
    """None when an address is a globally routable fetch target, else a
    rejection reason. Every family is checked, including IPv4-mapped
    IPv6 results (classified by their mapped address)."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return f"unparseable address: {ip_str!r}"
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if not addr.is_global:
        return f"non-global destination: {ip_str}"
    return None


def artwork_pin_address(host, port, resolver=None, deadline=None,
                        clock=None):
    """Resolve once and pin: every returned address must classify global.

    A mixed answer (one good, one prohibited) rejects the whole host so
    a racing DNS rebinding cannot slip a private target past validation.
    The production resolver runs getaddrinfo in a short-lived child
    bounded by the remaining chain budget (killed and reaped on
    expiry, so nothing continues after the deadline); injected test
    resolvers run directly. The pinned connection still dials the
    vetted IP literal, so no second lookup occurs. Returns (family,
    ip) for the first approved address.
    """
    if resolver is None or resolver is artwork_default_resolver:
        if deadline is not None and clock is not None:
            budget = deadline - clock()
        else:
            budget = ART_CHAIN_DEADLINE_S
        if budget <= 0:
            raise ArtworkFetchError(
                f"DNS for {host} exceeded its chain deadline")
        answers = artwork_default_resolver(host, port, timeout=budget)
    else:
        try:
            answers = resolver(host, port)
        except (socket.gaierror, OSError) as exc:
            raise ArtworkFetchError(f"DNS resolution failed for {host}: {exc}")
    if not answers:
        raise ArtworkFetchError(f"DNS returned no addresses for {host}")
    ips = []
    for entry in answers:
        try:
            family, _socktype, _proto, _canon, sockaddr = entry[:5]
            ips.append((family, sockaddr[0]))
        except (ValueError, TypeError, IndexError):
            raise ArtworkFetchError(f"DNS returned a malformed answer for {host}")
    for _family, ip in ips:
        reason = artwork_classify_ip(ip)
        if reason is not None:
            raise ArtworkFetchError(f"DNS for {host} {reason}")
    return ips[0][0], ips[0][1]


def artwork_split_url(url):
    """Validate scheme/credentials/port without any network access."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise ArtworkFetchError(f"unparseable URL: {exc}")
    scheme = (parts.scheme or "").lower()
    if scheme not in ART_SCHEME_PORTS:
        raise ArtworkFetchError(f"unsupported URL scheme: {scheme or '(none)'!r}")
    if parts.username or parts.password or "@" in (parts.netloc or ""):
        raise ArtworkFetchError("URL credentials are rejected")
    host = parts.hostname or ""
    if not host:
        raise ArtworkFetchError("URL has no host")
    try:
        port = parts.port
    except ValueError:
        raise ArtworkFetchError("unsupported port in URL")
    if port is None:
        port = ART_SCHEME_PORTS[scheme]
    elif port != ART_SCHEME_PORTS[scheme]:
        raise ArtworkFetchError(f"unsupported port: {port}")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return scheme, host, port, path


def artwork_open_pinned(scheme, host, port, family, ip, timeout):
    """Connect to the pinned address; TLS verifies host/SNI/cert.

    The socket connects to the vetted IP literal, so no second DNS
    lookup occurs. HTTPS wraps with the default verifying context and
    the original hostname for SNI and certificate validation. Connect
    and TLS handshake share the one passed budget: the handshake
    timeout is recomputed from the remaining time after connect, so
    both phases together never exceed it. Failures close the socket.
    """
    raw = socket.socket(family, socket.SOCK_STREAM)
    raw.settimeout(timeout)
    start = time.monotonic()
    try:
        try:
            raw.connect((ip, port))
        except (socket.timeout, TimeoutError) as exc:
            raise ArtworkFetchError(f"connection timed out: {exc}")
        if scheme == "https":
            raw.settimeout(_phase_timeout(start + timeout,
                                          time.monotonic,
                                          ART_READ_TIMEOUT_S))
            try:
                ctx = ssl.create_default_context()
                return ctx.wrap_socket(raw, server_hostname=host)
            except (socket.timeout, TimeoutError, ssl.SSLError) as exc:
                raise ArtworkFetchError(f"TLS handshake failed: {exc}")
        return raw
    except Exception:
        try:
            raw.close()
        except OSError:
            pass
        raise


def _check_deadline(clock, deadline):
    """Fail closed the moment the chain deadline is reached.

    An exhausted budget is expired: equality raises rather than
    arming a zero socket timeout for a further blocking call.
    """
    if clock() >= deadline:
        raise ArtworkFetchError("request chain exceeded its wall-clock deadline")


def _phase_timeout(deadline, clock, ceiling):
    """One blocking phase's socket timeout from the remaining budget.

    The per-I/O ceiling applies only while budget remains; expiry
    raises before any further wait. Pure in deadline/clock/ceiling,
    so fake clocks assert it deterministically.
    """
    _check_deadline(clock, deadline)
    return min(ceiling, deadline - clock())


def _chain_io_timeout(sock, deadline, clock):
    """Bound one blocking socket wait by the remaining chain budget.

    The 5-second per-I/O ceiling applies only while chain time
    remains; expiry raises before any further wait. Sockets close in
    the caller's finally block, so a timeout leaves nothing active.
    """
    sock.settimeout(_phase_timeout(deadline, clock, ART_READ_TIMEOUT_S))


def _recv_headers(sock, deadline, clock):
    """Read until the header terminator, bounded by the header cap.

    Reads incrementally so a large body is never accumulated as if it
    were headers; bytes after the terminator are returned as the body
    head. Header framing must complete within ART_MAX_HEADER_BYTES
    under the existing total deadline.
    """
    buf = bytearray()
    while True:
        _chain_io_timeout(sock, deadline, clock)
        marker = buf.find(b"\r\n\r\n")
        if marker >= 0:
            if marker + 4 > ART_MAX_HEADER_BYTES:
                raise ArtworkFetchError("response headers exceed the header cap")
            return bytes(buf[:marker]), bytes(buf[marker + 4:])
        if len(buf) > ART_MAX_HEADER_BYTES + 4:
            raise ArtworkFetchError("response headers exceed the header cap")
        try:
            piece = sock.recv(8192)
        except socket.timeout as exc:
            raise ArtworkFetchError(f"read timed out: {exc}")
        except OSError as exc:
            raise ArtworkFetchError(f"connection failed mid-headers: {exc}")
        if not piece:
            raise ArtworkFetchError("connection closed mid-headers")
        buf += piece
        _check_deadline(clock, deadline)


def _recv_exact(sock, count, cap, deadline, clock, timeout):
    """Read exactly count bytes (or to close) bounded by cap/deadline."""
    chunks, total = [], 0
    remaining = count
    while remaining is None or remaining > 0:
        _chain_io_timeout(sock, deadline, clock)
        try:
            piece = sock.recv(min(65536, remaining) if remaining is not None else 65536)
        except socket.timeout as exc:
            raise ArtworkFetchError(f"read timed out: {exc}")
        except OSError as exc:
            raise ArtworkFetchError(f"connection failed mid-body: {exc}")
        if not piece:
            break
        total += len(piece)
        if total > cap:
            raise ArtworkFetchError("response body exceeds the streamed cap")
        chunks.append(piece)
        if remaining is not None:
            remaining -= len(piece)
        _check_deadline(clock, deadline)
    return b"".join(chunks)


def artwork_http_get(url, headers=None, cap=db.ARTWORK_MAX_FEED_BYTES,
                     chain_deadline=None, resolver=None, opener=None,
                     clock=None):
    """One HTTP/1.1 GET over the pinned transport. No redirects followed.

    Returns (status, headers, body). Raises ArtworkFetchError on any
    control violation, timeout, deadline, oversize, or bad framing.
    `opener(scheme, host, port, family, ip, timeout)` is injectable for
    controlled tests; production uses artwork_open_pinned.
    """
    clock = clock or time.monotonic
    resolver = resolver or artwork_default_resolver
    opener = opener or artwork_open_pinned
    deadline = chain_deadline or (clock() + ART_CHAIN_DEADLINE_S)
    scheme, host, port, path = artwork_split_url(url)
    family, ip = artwork_pin_address(host, port, resolver, deadline, clock)
    request_lines = [f"GET {path} HTTP/1.1", f"Host: {host}",
                     "Connection: close", "Accept-Encoding: identity",
                     "Accept: */*", "User-Agent: EarWitness-artwork/1"]
    for key, value in (headers or {}).items():
        request_lines.append(f"{key}: {value}")
    try:
        request = ("\r\n".join(request_lines) + "\r\n\r\n").encode("ascii")
    except UnicodeEncodeError:
        # Recorded URLs stay byte-exact: an unescaped non-ASCII target
        # is a per-show failure, never a silent rewrite or a batch abort.
        raise ArtworkFetchError(
            f"unsupported non-ASCII request target for {host}:"
            " recorded URLs are used byte-exact and never re-encoded")
    # Connect shares the remaining chain budget: the per-I/O ceiling
    # applies only while chain time remains (TLS then recomputes its
    # own share inside the opener).
    connect_timeout = _phase_timeout(deadline, clock, ART_READ_TIMEOUT_S)
    try:
        sock = opener(scheme, host, port, family, ip, connect_timeout)
    except ArtworkFetchError:
        raise
    except (socket.timeout, TimeoutError) as exc:
        raise ArtworkFetchError(f"connection timed out: {exc}")
    except OSError as exc:
        raise ArtworkFetchError(f"connection failed: {exc}")
    try:
        _chain_io_timeout(sock, deadline, clock)
        try:
            sock.sendall(request)
        except (socket.timeout, TimeoutError) as exc:
            raise ArtworkFetchError(f"send timed out: {exc}")
        except OSError as exc:
            raise ArtworkFetchError(f"send failed: {exc}")
        # Headers stream incrementally under the header cap; the body
        # then streams under its own independent cap below.
        raw_head, tail = _recv_headers(sock, deadline, clock)
        lines = raw_head.decode("latin-1").split("\r\n")
        try:
            _version, status, _reason = lines[0].split(" ", 2)
            status = int(status)
        except (ValueError, IndexError):
            raise ArtworkFetchError("malformed HTTP status line")
        resp_headers = {}
        content_lengths = []
        transfer_encodings = []
        for line in lines[1:]:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip().lower()
            if key == "content-length":
                content_lengths.append(value.strip())
            elif key == "transfer-encoding":
                transfer_encodings.append(value.strip())
            elif key not in resp_headers:
                resp_headers[key] = value.strip()
        if len(content_lengths) > 1:
            raise ArtworkFetchError("duplicate content-length rejected")
        if transfer_encodings and content_lengths:
            raise ArtworkFetchError(
                "transfer-encoding with content-length rejected")
        if len(transfer_encodings) > 1:
            raise ArtworkFetchError("duplicate transfer-encoding rejected")
        encoding = resp_headers.get("content-encoding", "").lower()
        if encoding and encoding != "identity":
            raise ArtworkFetchError(
                f"unbounded content encoding rejected: {encoding}")
        if transfer_encodings:
            te = transfer_encodings[0].strip().lower()
            if te != "chunked":
                raise ArtworkFetchError(
                    f"unsupported transfer encoding: {te or '(empty)'}")
            body = _recv_chunked(sock, tail, cap, deadline, clock)
            _check_deadline(clock, deadline)
            return status, resp_headers, body
        length = content_lengths[0] if content_lengths else None
        if length is not None:
            digits = length.strip()
            # HTTP Content-Length grammar is ASCII decimal digits only:
            # int() would also accept a leading +/-, whitespace, or
            # underscores, so validate before converting. The decimal
            # is then bounded against the body cap as a string, so a
            # thousands-digit declaration can never reach int().
            if not digits or any(ch not in "0123456789" for ch in digits):
                raise ArtworkFetchError("malformed content-length")
            cap_digits = str(cap)
            if (len(digits) > len(cap_digits)
                    or (len(digits) == len(cap_digits) and digits > cap_digits)):
                raise ArtworkFetchError("response body exceeds the streamed cap")
            total = int(digits)
            if len(tail) > total:
                raise ArtworkFetchError(
                    "response body exceeds declared content-length")
            body = tail + _recv_exact(sock, total - len(tail), cap, deadline,
                                      clock, ART_READ_TIMEOUT_S)
            if len(body) != total:
                raise ArtworkFetchError("connection closed mid-body")
            _check_deadline(clock, deadline)
            return status, resp_headers, body
        # Close-delimited bodies charge the header tail against the
        # same cap the reader enforces for the remainder.
        if len(tail) > cap:
            raise ArtworkFetchError("response body exceeds the streamed cap")
        body = tail + _recv_exact(sock, None, cap - len(tail), deadline,
                                  clock, ART_READ_TIMEOUT_S)
        _check_deadline(clock, deadline)
        return status, resp_headers, body
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _recv_chunked(sock, tail, cap, deadline, clock):
    """Bounded chunked-body reader (chunk extensions ignored).

    Linear in the body (APS-R1-07): the buffer is read through an offset
    and compacted only when a socket read appends to it, and the chunks
    are joined once at the end."""
    buf, pos, parts, total = bytearray(tail), 0, [], 0

    def fill():
        nonlocal buf, pos
        _chain_io_timeout(sock, deadline, clock)
        try:
            piece = sock.recv(65536)
        except socket.timeout as exc:
            raise ArtworkFetchError(f"read timed out: {exc}")
        except OSError as exc:
            raise ArtworkFetchError(f"connection failed mid-body: {exc}")
        if not piece:
            raise ArtworkFetchError("connection closed mid-chunk")
        if pos:
            del buf[:pos]
            pos = 0
        buf += piece
        if len(buf) > cap + ART_MAX_HEADER_BYTES:
            raise ArtworkFetchError("response body exceeds the streamed cap")
        _check_deadline(clock, deadline)

    while True:
        _chain_io_timeout(sock, deadline, clock)
        while (end := buf.find(b"\r\n", pos)) < 0:
            fill()
        line = bytes(buf[pos:end])
        try:
            size = int(line.decode("ascii").split(";", 1)[0].strip(), 16)
        except ValueError:
            raise ArtworkFetchError("malformed chunk framing")
        if size < 0 or total + size > cap:
            raise ArtworkFetchError("response body exceeds the streamed cap")
        pos = end + 2
        while len(buf) - pos < size + 2:
            fill()
        parts.append(bytes(buf[pos:pos + size]))
        total += size
        if buf[pos + size:pos + size + 2] != b"\r\n":
            raise ArtworkFetchError("malformed chunk framing")
        pos += size + 2
        if size == 0:
            return b"".join(parts)


def artwork_fetch_chain(url, validators=None, cap=db.ARTWORK_MAX_FEED_BYTES,
                        resolver=None, opener=None, clock=None,
                        allow_304=False, chain_deadline=None):
    """GET with validated redirects. Returns (final_url, status, headers, body).

    Each redirect target is re-validated (scheme, credentials, port, DNS
    classification); HTTPS never downgrades to HTTP; at most three hops.
    304 is returned only when the caller sent validators for this exact
    target. Every hop shares one absolute chain deadline: the caller's
    chain_deadline when given (e.g. the whole-run budget), else 15s.
    """
    clock = clock or time.monotonic
    deadline = chain_deadline if chain_deadline is not None else clock() + ART_CHAIN_DEADLINE_S
    current, first_scheme = url, artwork_split_url(url)[0]
    hops = 0
    while True:
        if clock() > deadline:
            raise ArtworkFetchError("request chain exceeded its wall-clock deadline")
        status, headers, body = artwork_http_get(
            current, headers=validators if hops == 0 else None, cap=cap,
            chain_deadline=deadline, resolver=resolver, opener=opener,
            clock=clock)
        if status == 304:
            if not (allow_304 and hops == 0):
                raise ArtworkFetchError("unexpected 304 without validators")
            return current, status, headers, b""
        if status in (301, 302, 303, 307, 308):
            if hops >= ART_MAX_REDIRECTS:
                raise ArtworkFetchError("too many redirects")
            location = (headers.get("location") or "").strip()
            if not location:
                raise ArtworkFetchError("redirect without a location")
            target = urllib.parse.urljoin(current, location)
            scheme = artwork_split_url(target)[0]
            if first_scheme == "https" and scheme != "https":
                raise ArtworkFetchError("HTTPS downgrade rejected")
            first_scheme = scheme
            current, hops = target, hops + 1
            continue
        if status != 200:
            raise ArtworkFetchError(f"unexpected HTTP status: {status}")
        return current, status, headers, body


_ITUNES_IMAGE_TAG = "{http://www.itunes.com/dtds/podcast-1.0.dtd}image"


def _feed_text_for_scan(feed_bytes):
    """Check feed bytes so DTD scanning sees every accepted encoding.

    Raw-byte scanning misses declarations in UTF-16 (NUL-separated),
    which expat would still parse. Byte-order-marked UTF-16 is decoded
    and scanned as text; NUL bytes without a BOM or undecodable bytes
    fail closed as unsupported encodings. The whole feed is decoded and
    scanned, _FEED_SCAN_PIECE bytes at a time, so memory stays bounded by
    one piece whatever the feed's size (APS-R1-01)."""
    data = feed_bytes if isinstance(feed_bytes, bytes) else bytes(feed_bytes)
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    elif b"\x00" in data:
        raise ArtworkFetchError("unsupported feed encoding")
    else:
        encoding = "utf-8"
    decoder = codecs.getincrementaldecoder(encoding)()
    carry = ""
    for start in range(0, max(len(data), 1), _FEED_SCAN_PIECE):
        piece = data[start:start + _FEED_SCAN_PIECE]
        try:
            text = decoder.decode(piece, final=start + _FEED_SCAN_PIECE >= len(data))
        except (UnicodeDecodeError, ValueError):
            raise ArtworkFetchError("feed bytes are not decodable text")
        window = carry + text
        if "<!doctype" in window.lower():
            raise ArtworkFetchError("feed DTD/entity declarations are rejected")
        carry = window[-8:]


class _FeedDone(Exception):
    """The channel image is decided: stop parsing."""


def artwork_parse_feed_image(feed_bytes):
    """First metadata-selected artwork URL from feed XML, or None.

    Reads only show-level RSS channel image metadata (a channel
    image/url child) or correctly namespaced iTunes channel image
    metadata (itunes:image href). Item-level nodes and foreign
    namespaces are never inspected. Rejects DOCTYPE in any accepted
    encoding before parsing, so no DTD/entity resolution can run,
    and never follows enclosures or linked pages.

    Incremental (ART-PERF-FIX1, APS-R1-01): expat is fed the feed in
    _FEED_PARSE_PIECE pieces and builds no tree; only the depth and the
    first channel's image fields are kept, so memory does not grow with
    the feed. Nesting past ART_FEED_MAX_DEPTH refuses the feed. Parsing
    stops once the channel image is decided: the first itunes:image with
    an href, or the end of the first channel. The rules are the tree
    parse's: the first channel child of the root; its first itunes:image
    wins; else the text of the first url child of its first image.
    """
    _feed_text_for_scan(feed_bytes)
    itunes_tag = _ITUNES_IMAGE_TAG[1:].replace("}", " ")
    limit = ART_FEED_MAX_DEPTH
    depth = 0
    # channel/image: None (not seen yet), True (open now), False (over).
    channel = image = itunes = url = None
    in_url = False
    text = []

    def finish_url():
        nonlocal in_url, url
        in_url = False
        parser.CharacterDataHandler = None
        url = "".join(text)
        if itunes is not None:
            raise _FeedDone()

    def start(name, attrs):
        nonlocal depth, channel, image, itunes, in_url
        depth += 1
        if depth > 4:                     # below the channel's fields
            if depth > limit:
                raise ArtworkFetchError(
                    f"feed XML nests deeper than {limit} elements")
            if in_url:
                finish_url()              # url.text ends at its first child
            return
        if depth == 2:
            if name == "channel" and channel is None:
                channel = True
        elif depth == 3:
            if channel is not True:
                return
            if name == itunes_tag and itunes is None:
                itunes = (dict(zip(attrs[::2], attrs[1::2])).get("href")
                          or "").strip()
                if itunes or url is not None:
                    raise _FeedDone()
            elif name == "image" and image is None:
                image = True
        elif depth == 4 and image is True and name == "url" and url is None:
            in_url = True
            parser.CharacterDataHandler = text.append

    def end(_name):
        nonlocal depth, image
        depth -= 1
        if depth >= 3:
            if in_url:
                finish_url()              # the url element itself ends
            return
        if depth == 2:
            if image is True:
                image = False             # the first image is over
        elif depth == 1:
            if channel is True:
                raise _FeedDone()         # the first channel is over
        elif depth == 0:
            raise _FeedDone()             # the root is over

    def refuse_dtd(*_args):
        raise ArtworkFetchError("feed DTD/entity declarations are rejected")

    parser = _expat.ParserCreate(namespace_separator=" ")
    parser.ordered_attributes = True
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.StartDoctypeDeclHandler = refuse_dtd
    parser.EntityDeclHandler = refuse_dtd
    data = feed_bytes if isinstance(feed_bytes, bytes) else bytes(feed_bytes)
    stop = time.monotonic() + ART_FEED_PARSE_S
    try:
        for offset in range(0, len(data), _FEED_PARSE_PIECE):
            parser.Parse(data[offset:offset + _FEED_PARSE_PIECE], False)
            if time.monotonic() > stop:
                raise ArtworkFetchError(
                    f"feed parse exceeded {ART_FEED_PARSE_S:g} s")
        parser.Parse(b"", True)
    except _FeedDone:
        pass
    except _expat.ExpatError as exc:
        raise ArtworkFetchError(f"feed XML does not parse: {exc}")
    if itunes:
        return itunes
    url = (url or "").strip()
    return url or None


def artwork_parse_dimensions(mime, data):
    """Strict stdlib dimension parse for JPEG/PNG/WebP. Fails closed."""
    mime = (mime or "").lower()
    if mime == "image/png":
        if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
            raise ArtworkFetchError("PNG signature mismatch")
        length, kind = struct.unpack(">I4s", data[8:16])
        if kind != b"IHDR" or length != 13:
            raise ArtworkFetchError("PNG structure mismatch")
        width, height = struct.unpack(">II", data[16:24])
    elif mime == "image/jpeg":
        if len(data) < 4 or data[:2] != b"\xff\xd8":
            raise ArtworkFetchError("JPEG signature mismatch")
        width = height = 0
        pos = 2
        while pos + 4 <= len(data):
            if data[pos] != 0xFF:
                raise ArtworkFetchError("JPEG structure mismatch")
            marker = data[pos + 1]
            pos += 2
            if marker == 0xD9 or (0xD0 <= marker <= 0xD7):
                continue  # EOI / restart markers carry no length
            if marker == 0x01 or (0xD8 <= marker <= 0xDA and marker != 0xD8):
                if marker == 0xDA:
                    break  # start of scan: dimensions must precede it
                continue
            if pos + 2 > len(data):
                raise ArtworkFetchError("JPEG structure mismatch")
            seg_len = struct.unpack(">H", data[pos:pos + 2])[0]
            if seg_len < 2 or pos + seg_len > len(data):
                raise ArtworkFetchError("JPEG structure mismatch")
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                if seg_len < 7:
                    raise ArtworkFetchError("JPEG structure mismatch")
                height, width = struct.unpack(">HH", data[pos + 3:pos + 7])
                break
            pos += seg_len
        if not width or not height:
            raise ArtworkFetchError("JPEG dimensions not found")
    elif mime == "image/webp":
        if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
            raise ArtworkFetchError("WebP signature mismatch")
        width = height = 0
        pos = 12
        while pos + 8 <= len(data):
            kind, size = struct.unpack("<4sI", data[pos:pos + 8])
            chunk = data[pos + 8:pos + 8 + size]
            if len(chunk) < size:
                raise ArtworkFetchError("WebP structure mismatch")
            if kind == b"VP8 " and len(chunk) >= 10 \
                    and chunk[3:6] == b"\x9d\x01\x2a":
                width = struct.unpack("<H", chunk[6:8])[0] & 0x3FFF
                height = struct.unpack("<H", chunk[8:10])[0] & 0x3FFF
                break
            if kind == b"VP8L" and len(chunk) >= 5 and chunk[0] == 0x2F:
                bits = struct.unpack("<I", chunk[1:5])[0]
                width = (bits & 0x3FFF) + 1
                height = ((bits >> 14) & 0x3FFF) + 1
                break
            if kind == b"VP8X" and len(chunk) >= 10:
                width = struct.unpack("<I", chunk[4:7] + b"\x00")[0] + 1
                height = struct.unpack("<I", chunk[7:10] + b"\x00")[0] + 1
                break
            pos += 8 + size + (size % 2)
        if not width or not height:
            raise ArtworkFetchError("WebP dimensions not found")
    else:
        raise ArtworkFetchError(f"unsupported image MIME: {mime!r}")
    if width <= 0 or height <= 0:
        raise ArtworkFetchError("image dimensions not found")
    if width > db.ARTWORK_MAX_DIM or height > db.ARTWORK_MAX_DIM:
        raise ArtworkFetchError(
            f"image dimensions {width}x{height} exceed"
            f" {db.ARTWORK_MAX_DIM}x{db.ARTWORK_MAX_DIM}")
    return width, height


def artwork_validate_image(mime, data):
    """Signature/dimension validation. Returns (mime, w, h, sha256).

    The type comes from the bytes' magic numbers, never from the header
    `mime`: a header that disagrees with the bytes is ignored, and bytes
    that are not JPEG, PNG or WebP are rejected whatever it says.
    """
    header = (mime or "").split(";", 1)[0].strip().lower()
    if not data:
        raise ArtworkFetchError("empty image body")
    mime = db.artwork_sniff_mime(data)
    if mime is None:
        raise ArtworkFetchError(
            f"unsupported image bytes (header {header[:60]!r})")
    if len(data) > db.ARTWORK_MAX_IMAGE_BYTES:
        raise ArtworkFetchError("image exceeds the 1 MiB cap")
    width, height = artwork_parse_dimensions(mime, data)
    digest = hashlib.sha256(data).hexdigest()
    return mime, width, height, digest


def artwork_recorded_feeds(con):
    """Distinct recorded feed URLs from the episodes authority payload."""
    feeds = set()
    for row in con.execute(
            "SELECT payload FROM source_versions WHERE source_key='episodes.json'"
            " ORDER BY version_no DESC, id DESC LIMIT 1").fetchall():
        try:
            payload = json.loads(row["payload"])
        except (ValueError, TypeError):
            continue
        rows = payload if isinstance(payload, list) else payload.get("episodes", [])
        if not isinstance(rows, list):
            continue
        for entry in rows:
            if not isinstance(entry, dict):
                continue
            url = (entry.get("feed_url") or "").strip()
            if url:
                feeds.add(url)
    return sorted(feeds)


def refresh_show_artwork(workspace, limit=None, resolver=None, opener=None,
                         clock=None):
    """Explicit sequential artwork refresh over recorded feeds only.

    No request occurs for unrecorded URLs; each show resolves its own
    feed, selects artwork from parsed metadata, validates, and commits
    atomically after its sockets close. Per-show failures are recorded
    on the cache row; the run summary reports them. The 20-minute whole
    command limit stops the run with truncated=True. Holds only the
    shared workspace lock, so the refresh runs while the local app
    server (which holds the shared lock for its lifetime) keeps
    serving reads; no SQLite write transaction is held during requests.
    """
    from earwitness.app import customer as _customer
    clock = clock or time.monotonic
    started = clock()
    workspace = Path(workspace)
    with db.workspace_lock(workspace, shared=True):
        con = db.connect(workspace)
        try:
            version = con.execute(
                "SELECT COALESCE(MAX(version),0) FROM schema_version").fetchone()[0]
            if version != db.SCHEMA_VERSION:
                raise ValueError(
                    f"workspace schema is v{version}; artwork refresh requires"
                    f" v{db.SCHEMA_VERSION}")
            recorded = set(artwork_recorded_feeds(con))
            _customer.rebuild_customer_show_map(con)
            authority = con.execute(
                "SELECT MAX(id) FROM source_versions"
                " WHERE source_key='episodes.json'").fetchone()[0]
            shows = con.execute(
                "SELECT id, display_title, authority_key, authority_basis"
                " FROM customer_show_identities WHERE workspace_id='local'"
                " AND unresolved=0 AND authority_basis='feed_url'"
                " ORDER BY display_title").fetchall()
            summary = {"shows": 0, "ok": 0, "unchanged": 0, "failed": 0,
                       "errors": [], "truncated": False}
            run_deadline = started + ART_WHOLE_RUN_S
            for row in shows:
                if clock() >= run_deadline:
                    summary["truncated"] = True
                    break
                if limit is not None and summary["shows"] >= limit:
                    break
                show_id = row["id"]
                feed_url = (row["authority_key"] or "").strip()
                if not feed_url or feed_url not in recorded:
                    continue
                summary["shows"] += 1
                try:
                    _refresh_one_show(con, show_id, feed_url, authority,
                                      resolver, opener, clock, summary,
                                      run_deadline=run_deadline)
                except ArtworkFetchError as exc:
                    summary["failed"] += 1
                    summary["errors"].append(
                        {"show": show_id, "error": str(exc)})
                    db.record_artwork_attempt(con, show_id, str(exc), feed_url)
                    if clock() >= run_deadline:
                        summary["truncated"] = True
                except (sqlite3.Error, OSError, MemoryError) as exc:
                    # One show's local failure (out of memory included)
                    # fails that show only; the run carries on.
                    summary["failed"] += 1
                    summary["errors"].append(
                        {"show": show_id, "error": "local failure: "
                         + (str(exc) or "out of memory")})
                    if clock() >= run_deadline:
                        summary["truncated"] = True
            return summary
        finally:
            con.close()


def _chain_cap(clock, run_deadline):
    """One chain's absolute deadline under the whole-run budget."""
    chain = clock() + ART_CHAIN_DEADLINE_S
    if run_deadline is None:
        return chain
    return min(chain, run_deadline)


def _refresh_one_show(con, show_id, feed_url, authority, resolver, opener,
                      clock, summary, run_deadline=None):
    """Fetch feed, select artwork, validate image, commit atomically.

    Feed and artwork chains each run under the whole-run deadline, and
    no image work starts after run expiry: expiry truncates the run
    with the show recorded as failed. An unchanged feed still
    revalidates the selected cached artwork URL through its own
    request: feed metadata alone never validates cached image bytes.
    Sockets close in the chain finally block, so a timeout leaves
    nothing active.
    """
    cached = con.execute("SELECT * FROM show_artwork"
                         " WHERE workspace_id='local' AND show_identity_id=?",
                         (show_id,)).fetchone()
    cached = dict(cached) if cached else {}
    feed_validators = {}
    if cached.get("feed_url") == feed_url and cached.get("feed_etag"):
        feed_validators["If-None-Match"] = cached["feed_etag"]
    if cached.get("feed_url") == feed_url and cached.get("feed_last_modified"):
        feed_validators["If-Modified-Since"] = cached["feed_last_modified"]
    final_feed, status, headers, body = artwork_fetch_chain(
        feed_url, validators=feed_validators or None,
        cap=db.ARTWORK_MAX_FEED_BYTES, resolver=resolver, opener=opener,
        clock=clock, allow_304=bool(feed_validators),
        chain_deadline=_chain_cap(clock, run_deadline))
    feed_not_modified = (status == 304)
    if feed_not_modified:
        # The feed is unchanged, but unchanged feed metadata alone
        # never validates cached image bytes: the selected cached
        # artwork URL still revalidates through its own request below.
        image_url = cached.get("artwork_url") or None
    else:
        image_url = artwork_parse_feed_image(body)
    if not image_url:
        raise ArtworkFetchError("feed carries no artwork metadata")
    # No separate run-expiry check is needed here: both chains below
    # run under the run-capped deadline, and the chain entry check
    # refuses any post-expiry resolve or request, including image work.
    _scheme, _host, _port, _path = artwork_split_url(image_url)
    # Validators bind to the exact URL that returned them: a redirect
    # target's ETag/Last-Modified is never persisted under (or resent
    # to) the original URL. Non-redirected chains keep exact equality,
    # so their validators persist as before. Feed and artwork targets
    # keep separate validators (never crossed).
    same_feed = (final_feed == feed_url)
    if feed_not_modified:
        # A 304 carries no representation: keep the cached feed
        # validators unless the response itself updates them, so a
        # validator-less 304 never wipes the stored revalidation state.
        if same_feed:
            feed_etag = (headers.get("etag", "")
                         or cached.get("feed_etag", ""))
            feed_last_modified = (headers.get("last-modified", "")
                                  or cached.get("feed_last_modified", ""))
        else:
            feed_etag = ""
            feed_last_modified = ""
    else:
        feed_etag = headers.get("etag", "") if same_feed else ""
        feed_last_modified = headers.get("last-modified", "") if same_feed else ""
    art_validators = {}
    if cached.get("artwork_url") == image_url and cached.get("image_bytes"):
        if cached.get("art_etag"):
            art_validators["If-None-Match"] = cached["art_etag"]
        if cached.get("art_last_modified"):
            art_validators["If-Modified-Since"] = cached["art_last_modified"]
    final_art, status, headers, body = artwork_fetch_chain(
        image_url, validators=art_validators or None,
        cap=db.ARTWORK_MAX_IMAGE_BYTES, resolver=resolver, opener=opener,
        clock=clock, allow_304=bool(art_validators),
        chain_deadline=_chain_cap(clock, run_deadline))
    now = db.utcnow()
    if status == 304:
        db.upsert_show_artwork(con, show_id, {
            **_cache_keep(cached), "fetched_at": now, "validated_at": now})
        summary["unchanged"] += 1
        return
    mime = (headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    mime, width, height, digest = artwork_validate_image(mime, body)
    same_art = (final_art == image_url)
    db.upsert_show_artwork(con, show_id, {
        "source_version_id": authority, "feed_url": feed_url,
        "artwork_url": image_url, "image_bytes": body, "mime": mime,
        "width": width, "height": height, "sha256": digest,
        "feed_etag": feed_etag,
        "feed_last_modified": feed_last_modified,
        "art_etag": headers.get("etag", "") if same_art else "",
        "art_last_modified": headers.get("last-modified", "") if same_art else "",
        "fetched_at": now, "validated_at": now, "status": "ok",
        "last_error": "", "attempted_at": now})
    summary["ok"] += 1


def _cache_keep(cached):
    """Carry retained representation fields across a 304 revalidation."""
    return {"source_version_id": cached.get("source_version_id"),
            "feed_url": cached.get("feed_url", ""),
            "artwork_url": cached.get("artwork_url", ""),
            "image_bytes": cached.get("image_bytes"),
            "mime": cached.get("mime", ""),
            "width": cached.get("width", 0), "height": cached.get("height", 0),
            "sha256": cached.get("sha256", ""),
            "feed_etag": cached.get("feed_etag", ""),
            "feed_last_modified": cached.get("feed_last_modified", ""),
            "art_etag": cached.get("art_etag", ""),
            "art_last_modified": cached.get("art_last_modified", ""),
            "status": "ok", "last_error": "",
            "attempted_at": cached.get("attempted_at")}


def hosted_registry_command(args):
    """Trusted local-operator register/bind against the grants registry.

    register-owner records one existing owner workspace database (the
    file must already exist under the server data_dir with a verified
    workspace identity); bind-account then binds exactly one
    authenticated (issuer, subject) to it. Either step failing leaves
    hosted owner access denied."""
    store = registry.GrantsStore(args.grants_db)
    if args.command == "register-owner":
        result = registry.register_workspace(
            store, args.workspace_id, args.db_path, args.data_dir,
            "local", True, args.operator or "")
    else:
        result = registry.bind_workspace_account(
            store, args.workspace_id, args.issuer, args.subject,
            operator=args.operator or "")
    print(json.dumps(result, indent=2))
    return 0


def corrections_command(con, args) -> dict:
    from earwitness.app import corrections
    if args.action == "export":
        if not args.out:
            raise ValueError("corrections export needs --out")
        output = Path(args.out).resolve()
        if output.exists():
            raise ValueError("output exists; choose a new filename")
        with db.transaction(con, write=False):
            doc = corrections.export(con)
        output.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        return {"out": str(output), **{k: len(v) for k, v in doc.items() if isinstance(v, list)}}
    if not args.shows:
        raise ValueError("corrections ranks needs --shows")
    shows = json.loads(Path(args.shows).read_text())
    shows = shows.get("shows") if isinstance(shows, dict) else shows
    if not isinstance(shows, list):
        raise ValueError("shows file must hold a list of shows")
    with db.transaction(con, write=True):
        return {"ranked": corrections.set_show_ranks(con, shows)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=".earwitness")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("seed", "status", "reindex", "backup", "restore", "migrate", "verify",
                    "upgrade", "agent-config", "reconcile",
                    "register-owner", "bind-account", "refresh-show-artwork",
                    "customer-cutover-check", "register-legacy-audio", "corrections",
                    "record-alias", "import-coverage", "import-jev", "import-names",
                    "curate-brands", "brand-suggest", "carry-alert-baselines"):
        p = sub.add_parser(command)
        p.add_argument("--workspace", default=argparse.SUPPRESS)
        if command == "seed":
            p.add_argument("--source-root", default=None,
                           help="directory with the repo layout to seed from (default: the repository)")
            p.add_argument("--episodes", default=None)
            p.add_argument("--transcripts", default=None)
            p.add_argument("--candidates", default=None)
            p.add_argument("--jev-results", default=None)
            p.add_argument("--no-jev", action="store_true",
                           help="treat the Jev run as empty instead of requiring its results file")
            p.add_argument("--named-reads", default=None,
                           help="named Jev reads (corpus export; default: the source root's "
                                "experiments/named-reads/output/candidates_normalized.json if present)")
            p.add_argument("--no-named-reads", action="store_true",
                           help="do not import named Jev reads")
        if command == "corrections":
            # The operator's ad-review rules: `export` writes the corrections
            # file the pipeline reads (SETUP.md); `ranks` stores show chart
            # ranks from a corpus shows list (review queue impact).
            p.add_argument("action", choices=["export", "ranks"])
            p.add_argument("--out", help="export: the new JSON file to write")
            p.add_argument("--shows", help="ranks: a corpus_run --shows JSON file")
        if command == "record-alias":
            # Operator data change: record an episode as an explicit alias
            # of the same audio's canonical episode (episode_alias).
            p.add_argument("--alias", required=True, help="the duplicate episode id")
            p.add_argument("--target", required=True, help="the canonical episode id")
            p.add_argument("--dry-run", action="store_true",
                           help="print the change and write nothing")
            p.add_argument("--backup-out", default=None)
            p.add_argument("--actor", default="operator",
                           help="who records the alias (audit and history actor)")
        if command == "import-coverage":
            # D-2: one-off legacy coverage attestation import (operator-run).
            p.add_argument("--dry-run", action="store_true",
                           help="print what would be recorded; the database bytes stay"
                                " unchanged (empty -wal/-shm and workspace.lock files may be"
                                " created)")
            p.add_argument("--run-id", default=None)
            p.add_argument("--actor", default="operator")
            p.add_argument("--backup-out", default=None)
        if command == "import-jev":
            # R1-06 A: import a Jev re-screen of the current transcript
            # copies (jev_rescreen; operator-run, no TypeSafe call here).
            p.add_argument("--corpus", required=True,
                           help="the re-screen corpus folder (jev/<episode>.json results)")
            p.add_argument("--dry-run", action="store_true",
                           help="print what would be imported; write nothing")
            p.add_argument("--run-id", default=None)
            p.add_argument("--actor", default="operator")
            p.add_argument("--backup-out", default=None)
        if command == "import-names":
            # Post-decisions item 4: suggested brand names for the re-screen's
            # jev reads from a corpus names stage (name_import; operator-run,
            # no network here).
            p.add_argument("--corpus", required=True,
                           help="the re-screen corpus folder (names/<episode>.json results)")
            p.add_argument("--dry-run", action="store_true",
                           help="print what would be suggested; write nothing")
            p.add_argument("--run-id", default=None)
            p.add_argument("--actor", default="operator")
            p.add_argument("--backup-out", default=None)
            p.add_argument("--no-curate", action="store_true",
                           help="do not curate the known brand list first")
        if command == "curate-brands":
            # Automated curation of the known-brand list (brand_curate;
            # operator-run, no network here), or one Reno-approved entry.
            p.add_argument("--corpus", default=None,
                           help="the corpus folder with names/<episode>.json (not needed with --add)")
            p.add_argument("--dry-run", action="store_true",
                           help="print what would be added; write nothing")
            p.add_argument("--run-id", default=None)
            p.add_argument("--actor", default="operator")
            p.add_argument("--backup-out", default=None)
            p.add_argument("--add", default=None, metavar="KEY",
                           help="add this brand by hand (Reno-approved), with --display and --approved-by")
            p.add_argument("--display", default=None)
            p.add_argument("--approved-by", default=None)
            p.add_argument("--note", default="")
        if command == "brand-suggest":
            # One suggestion per off-list heard name (brand_suggest; operator-run:
            # it calls `agy`, which spends a paid model call per new name).
            p.add_argument("--corpus", required=True,
                           help="the corpus folder with names/<episode>.json")
            p.add_argument("--dry-run", action="store_true",
                           help="print the plan; no call, no write")
            p.add_argument("--force", action="store_true",
                           help="ask again, with new calls (the call cache is skipped), for every"
                                " name whose suggestion is not confirmed")
            p.add_argument("--run-id", default=None)
            p.add_argument("--max-calls", type=int, default=100,
                           help="at most this many agy calls in one run, retries included")
            p.add_argument("--workers", type=int, default=3)
            p.add_argument("--agy", default="agy", help="the agy command to run")
            p.add_argument("--model", default=None)
            p.add_argument("--log-dir", default=None,
                           help="calls.jsonl and raw streams (default <workspace>/brand-suggest)")
            p.add_argument("--actor", default="operator")
            p.add_argument("--backup-out", default=None)
        if command == "carry-alert-baselines":
            # R1-04 A: publish-time step after import-names: the reads that
            # names run named join the alert baselines (not news).
            p.add_argument("--names-run-id", required=True,
                           help="the import-names --run-id whose named reads to carry")
            p.add_argument("--dry-run", action="store_true",
                           help="print what would be carried; write nothing")
            p.add_argument("--run-id", default=None)
            p.add_argument("--actor", default="operator")
            p.add_argument("--backup-out", default=None)
        if command == "backup":
            p.add_argument("--out", required=True)
        if command == "migrate":
            p.add_argument("--backup-out", required=True)
        if command in ("upgrade", "customer-cutover-check"):
            p.add_argument("--manifest")
        if command == "customer-cutover-check":
            p.add_argument("--out")
        if command == "restore":
            p.add_argument("--backup", required=True)
            p.add_argument("--force", action="store_true")
        if command == "refresh-show-artwork":
            p.add_argument("--limit", type=int, default=None)
        if command == "upgrade":
            p.add_argument("--backup-out", default=None)
            p.add_argument("--allow-campaigns", action="store_true")
            p.add_argument("--allow-watchlists", action="store_true")
            p.add_argument("--allow-notes", action="store_true")
            p.add_argument("--legacy-audio-root", default=None)
        if command == "register-legacy-audio":
            p.add_argument("--legacy-audio-root", required=True)
            p.add_argument("--backup-out", default=None)
        if command == "agent-config":
            p.add_argument("--client", required=True,
                           choices=["codex", "cursor", "claude-desktop"])
            p.add_argument("--profile", default="reader", choices=["reader"])
            p.add_argument("--allow-campaigns", action="store_true")
            p.add_argument("--allow-watchlists", action="store_true")
            p.add_argument("--allow-notes", action="store_true")
        if command == "register-owner":
            p.add_argument("--grants-db", required=True)
            p.add_argument("--data-dir", required=True)
            p.add_argument("--workspace-id", required=True)
            p.add_argument("--db-path", required=True)
            p.add_argument("--operator", default="")
        if command == "bind-account":
            p.add_argument("--grants-db", required=True)
            p.add_argument("--workspace-id", required=True)
            p.add_argument("--issuer", required=True)
            p.add_argument("--subject", required=True)
            p.add_argument("--operator", default="")
    args = parser.parse_args(argv)
    try:
        if args.command in ("register-owner", "bind-account"):
            return hosted_registry_command(args)
        workspace = resolve_workspace(args.workspace)
        if args.command == "restore":
            result = restore_quarantined(workspace, args.backup, args.force)
        elif args.command == "upgrade":
            result = upgrade_workspace(workspace, args.backup_out, args.allow_campaigns,
                                       args.allow_watchlists, args.allow_notes,
                                       legacy_audio_root=args.legacy_audio_root)
            if args.manifest:
                manifest = json.loads((REPO / args.manifest).read_text())
                semantic = semantic_convert_workspace(workspace, manifest)
                result = {"structural": result, "semantic": semantic}
        elif args.command == "record-alias":
            result = record_alias(workspace, args.alias, args.target, args.dry_run,
                                  args.backup_out, args.actor)
        elif args.command == "import-coverage":
            from earwitness.app import coverage_import
            report = import_coverage(workspace, args.dry_run, args.run_id, args.actor,
                                     args.backup_out)
            print(coverage_import.format_report(report))
            if report.get("backup"):
                print(f"recovery backup: {report['backup']}")
            return 0
        elif args.command == "import-jev":
            from earwitness.app import jev_rescreen
            report = import_jev(workspace, args.corpus, args.dry_run, args.run_id, args.actor,
                                args.backup_out)
            print(jev_rescreen.format_report(report))
            if report.get("backup"):
                print(f"recovery backup: {report['backup']}")
            return 0
        elif args.command == "import-names":
            from earwitness.app import name_import
            report = import_names(workspace, args.corpus, args.dry_run, args.run_id, args.actor,
                                  args.backup_out, curate=not args.no_curate)
            print(name_import.format_report(report))
            if report.get("backup"):
                print(f"recovery backup: {report['backup']}")
            return 0
        elif args.command == "curate-brands":
            from earwitness.app import brand_curate
            add = None
            if args.add is not None:
                if not args.display or not args.approved_by:
                    raise ValueError("--add needs --display and --approved-by")
                add = {"key": args.add, "display": args.display,
                       "approved_by": args.approved_by, "note": args.note}
            report = curate_brands(workspace, args.corpus, args.dry_run, args.run_id,
                                   args.actor, args.backup_out, add=add)
            print(brand_curate.format_report(report))
            if report.get("backup"):
                print(f"recovery backup: {report['backup']}")
            return 0
        elif args.command == "brand-suggest":
            from earwitness.app import brand_suggest as bs
            report = brand_suggest(workspace, args.corpus, args.dry_run, args.force, args.run_id,
                                   args.max_calls, args.workers, args.agy, args.model,
                                   args.log_dir, args.actor, args.backup_out)
            print(bs.format_report(report))
            if not args.dry_run:
                print(f"recovery backup: {report.get('backup') or 'none'}")
            return 0
        elif args.command == "carry-alert-baselines":
            from earwitness.app import alert_carry
            report = carry_alert_baselines(workspace, args.names_run_id, args.dry_run,
                                           args.run_id, args.actor, args.backup_out)
            print(alert_carry.format_report(report))
            if report.get("backup"):
                print(f"recovery backup: {report['backup']}")
            return 0
        elif args.command == "register-legacy-audio":
            result = register_legacy_audio(workspace, args.legacy_audio_root, args.backup_out)
        elif args.command == "agent-config":
            result = agent_config(workspace, args.client, args.profile,
                                  args.allow_campaigns, args.allow_watchlists,
                                  args.allow_notes)
        elif args.command == "reconcile":
            result = reconcile_workspace(workspace)
        elif args.command == "refresh-show-artwork":
            result = refresh_show_artwork(workspace, limit=args.limit)
        elif args.command == "migrate":
            return print(json.dumps(migrate_workspace(workspace, args.backup_out), indent=2)) or 0
        elif args.command == "verify":
            with db.workspace_lock(workspace, shared=True):
                con = db.connect(workspace)
                try:
                    result = db.verify_schema(con)
                    result["counts"] = snapshot_counts(con)["tables"]
                finally:
                    con.close()
            print(json.dumps(result, indent=2))
            return 0 if not result["problems"] else 1
        elif args.command == "customer-cutover-check":
            manifest = json.loads((REPO / args.manifest).read_text()) if args.manifest else None
            result = customer_cutover_check(workspace, manifest)
            if args.out:
                output = (REPO / args.out).resolve()
                if REPO not in output.parents or output.exists():
                    raise ValueError("receipt must be a new candidate-local file")
                output.write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result, indent=2))
            return 0 if result["ok"] else 1
        else:
            with db.workspace_lock(workspace, shared=True):
                con = db.connect(workspace)
                try:
                    db.init_schema(con)
                    if args.command == "seed":
                        # One large write transaction: a bigger page cache keeps
                        # it from spilling pages to the WAL again and again.
                        con.execute("PRAGMA cache_size=-262144")
                        root = Path(args.source_root).resolve() if args.source_root else REPO
                        explicit = (args.source_root, args.episodes, args.transcripts,
                                    args.candidates, args.jev_results, args.no_jev,
                                    args.named_reads, args.no_named_reads)
                        result = importer.seed_all(root, con, "cli", importer.seed_sources(
                            root, args.episodes, args.transcripts, args.candidates,
                            args.jev_results, args.no_jev, args.named_reads,
                            args.no_named_reads) if any(explicit) else None)
                        from earwitness.app import conversion as _conversion
                        with db.transaction(con, write=True):
                            result["local_ui_readiness"] = _conversion.record_local_readiness(con, "seed")
                    elif args.command == "corrections":
                        result = corrections_command(con, args)
                    elif args.command == "status":
                        result = ops.overview(con)
                    elif args.command == "backup":
                        output = (REPO / args.out).resolve()
                        if REPO not in output.parents or output == workspace / "workspace.sqlite3":
                            raise ValueError("backup destination must be a separate repository-local file")
                        if output.exists():
                            raise ValueError("backup destination exists; choose a new filename")
                        result = ops.backup_to(con, output)
                        output.chmod(0o600)
                    else:
                        with con:
                            con.execute("BEGIN IMMEDIATE")
                            importer._reindex(con)
                            revision = db.bump_revision(con)
                            db.audit(con, "cli", "reindex", "workspace", "reindex", {}, revision)
                        result = {"aggregate_rev": revision}
                finally:
                    con.close()
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, RuntimeError, sqlite3.Error, importer.ImportError, OSError,
            registry.GrantError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
