"""Follow-up N: the research view revision is computed once per data state.

customer_query._view_revision reuses a revision across requests,
connections and processes only while nothing it depends on has changed:
rows (any write through SQLite bumps the guarded state token), schema,
the caller's context, and the recording file/clock inputs. These tests
pin every reuse against the uncached computation, value and side effects
(ctx.disclosures), and count expensive computations instead of timing
them.
"""
from __future__ import annotations

import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from earwitness.app import citations, conversion, customer, customer_query, db, ops
from earwitness.app import import_sources
from earwitness.app.access import AccessContext
from earwitness.pipeline import artifacts
from tests import backend_fixtures

REPO = Path(__file__).resolve().parent.parent
UNCACHED = customer_query._view_revision_uncached


# Names the authorizer reports as read that are not stored tables, so no
# trigger can guard them: sqlite_master (covered by the schema digest) and
# json_each, the table-valued function a query uses to bind a list of ids
# (conversion.py treats it the same way). Everything else read must be guarded.
NOT_STORED = frozenset({"sqlite_master", "json_each"})


def unguarded_reads(con, read, guarded):
    """Tables the view read that carry no guard. A stored table that happens
    to be named like an exempt name is still reported."""
    stored = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
    return read - guarded - (NOT_STORED - stored)


def owner():
    return AccessContext.create(principal_id="local-owner", principal_kind="owner",
                                workspace_id="local", credential_kind="local_operator")


def alice():
    return AccessContext.create(principal_id="alice", principal_kind="viewer",
                                workspace_id="local", credential_kind="browser_session",
                                policy_identity="local:7")


def bob():
    """Same policy identity as alice, narrower visibility: no transcripts."""
    return AccessContext.create(principal_id="bob", principal_kind="viewer",
                                workspace_id="local", credential_kind="reader_bearer",
                                scopes=("research:read",), policy_identity="local:7")


def guest():
    return AccessContext.create(principal_id="guest@example.com", principal_kind="guest",
                                workspace_id="local", credential_kind="browser_session",
                                authority_kind="share", share_grant_id="grant-1",
                                content_policy={"fields": ["metadata", "excerpt"]},
                                policy_identity="local:7")


CONTEXTS = {"default": lambda: None, "owner": owner, "alice": alice, "bob": bob,
            "guest": guest}


def _outcome(fn, con, make):
    ctx = make()
    try:
        value = ("ok", fn(con, ctx))
    except Exception as exc:  # compared, never swallowed silently
        value = ("raise", type(exc).__name__, str(exc))
    effects = None if ctx is None else (frozenset(ctx.disclosures), dict(ctx.recordings))
    return value, effects


def _reference(con, make):
    if con.in_transaction:
        return _outcome(UNCACHED, con, make)
    with db.transaction(con, write=False):
        return _outcome(UNCACHED, con, make)


def file_workspace():
    """File-backed synthetic workspace with a verified pinned recording on e1."""
    con = backend_fixtures.make_file_workspace()
    con.isolation_level = None
    backend_fixtures.seed_source_rows(con)
    proof = backend_fixtures.register_synthetic_recording(con, "e1")
    root = Path(con.execute("PRAGMA database_list").fetchone()[2]).parent
    return con, root / proof["artifact"]["locator"], proof["artifact"]["id"]


def other_connection(con):
    path = con.execute("PRAGMA database_list").fetchone()[2]
    other = sqlite3.connect(path, isolation_level=None)
    other.row_factory = sqlite3.Row
    return other


class _ViewCase(unittest.TestCase):
    def setUp(self):
        customer_query._VIEW_MEMO.clear()
        self.addCleanup(customer_query._VIEW_MEMO.clear)

    def assertViewsMatch(self, con, contexts=CONTEXTS) -> dict:
        """Every context: the (possibly reused) revision and its effects on
        ctx equal the uncached computation's, on first and repeated calls."""
        out = {}
        for name, make in contexts.items():
            reference = _reference(con, make)
            first = _outcome(customer_query._view_revision, con, make)
            again = _outcome(customer_query._view_revision, con, make)
            self.assertEqual(first, reference, name)
            self.assertEqual(again, reference, name)
            out[name] = reference[0]
        return out

    def assertWriterTracked(self, con, write, *, changes=True) -> None:
        before = self.assertViewsMatch(con)
        write()
        after = self.assertViewsMatch(con)
        if changes:
            self.assertNotEqual(before, after, "fixture write must change the view")


class GuardTest(_ViewCase):
    def test_every_ordinary_table_is_guarded_and_fts_is_not(self):
        con = backend_fixtures.make_memory_workspace()
        self.addCleanup(con.close)
        rows = db._schema_rows(con)
        guarded = set(db._view_state_guarded_tables(rows))
        tables = {r[1] for r in rows if r[0] == "table"}
        self.assertIn("episodes", guarded)
        self.assertIn("meta", guarded)
        fts = {name + suffix for name in ("evidence_fts", "transcript_segments_fts", "ad_reads_fts")
               for suffix in ("", "_config", "_content", "_data", "_docsize", "_idx")}
        self.assertEqual({t for t in tables - guarded if not t.startswith("sqlite_")},
                         (fts & tables) | (db.VIEW_STATE_UNREAD & tables))
        self.assertIsNotNone(db.view_state_key(con))

    def test_lazily_created_unread_table_keeps_the_key(self):
        con = backend_fixtures.make_memory_workspace()
        self.addCleanup(con.close)
        before = db.view_state_key(con)
        # The DDL campaigns.save_manual_assessment runs on first use.
        con.execute("CREATE TABLE IF NOT EXISTS manual_assessments ("
                    " id INTEGER PRIMARY KEY AUTOINCREMENT, placement_id TEXT NOT NULL,"
                    " created_at TEXT NOT NULL)")
        con.execute("INSERT INTO manual_assessments (placement_id, created_at) VALUES ('p', 'x')")
        after = db.view_state_key(con)
        self.assertIsNotNone(after)
        self.assertNotEqual(after, before)  # the schema digest moved; no guard is missing
        con.execute("INSERT INTO manual_assessments (placement_id, created_at) VALUES ('q', 'x')")
        self.assertEqual(db.view_state_key(con), after)  # unguarded: no invalidation

    def test_unknown_new_table_disables_reuse_until_guarded(self):
        con = backend_fixtures.make_memory_workspace()
        self.addCleanup(con.close)
        con.execute("CREATE TABLE brand_new_feature (x)")
        self.assertIsNone(db.view_state_key(con))
        db.ensure_view_state_guards(con)
        self.assertIsNotNone(db.view_state_key(con))

    def test_view_reads_only_guarded_tables(self):
        """An authorizer records every table the uncached view reads, for
        every context; each must carry guards (FTS and sqlite_* never)."""
        con, _path, _artifact = file_workspace()
        con.execute("INSERT INTO p01p04_retention VALUES (?, '2999-01-01T00:00:00+00:00')",
                    (_artifact,))
        con.execute("UPDATE episodes SET alias_of='e1' WHERE id='e2'")
        read = set()

        def authorizer(action, arg1, _arg2, _db_name, _trigger):
            if action == sqlite3.SQLITE_READ:
                read.add(arg1)
            return sqlite3.SQLITE_OK
        try:
            guarded = set(db._view_state_guarded_tables(db._schema_rows(con)))
            # Research data token path (validation plan A3): the token table
            # plus the recording proof inputs outside it.
            fresh = other_connection(con)
            fresh.set_authorizer(authorizer)
            for make in CONTEXTS.values():
                _outcome(UNCACHED, fresh, make)
            fresh.close()
            self.assertTrue({"research_state", "episodes", "media_artifacts",
                             "p01p04_retention"} <= read)
            self.assertEqual(unguarded_reads(con, read, guarded), set())
            # Rescan path (a research trigger missing): every result input.
            read.clear()
            con.execute('DROP TRIGGER "ew_research_state_insert_reviews"')
            fresh = other_connection(con)
            fresh.set_authorizer(authorizer)
            for make in CONTEXTS.values():
                _outcome(UNCACHED, fresh, make)
            fresh.close()
            self.assertTrue({"episodes", "evidence", "source_states", "media_artifacts",
                             "p01p04_retention", "source_content_policy",
                             "customer_episode_shows"} <= read)
            self.assertEqual(unguarded_reads(con, read, guarded), set())
        finally:
            con.close()

    def test_exempt_names_never_hide_a_real_unguarded_table(self):
        """The exemption covers only the function and sqlite_master: a real
        unguarded table read next to json_each is still reported, and a
        stored table named json_each is reported too."""
        con = backend_fixtures.make_memory_workspace()
        self.addCleanup(con.close)
        guarded = set(db._view_state_guarded_tables(db._schema_rows(con)))
        con.execute("CREATE TABLE brand_new_feature (x)")  # no guard triggers
        read = set()

        def authorizer(action, arg1, _arg2, _db_name, _trigger):
            if action == sqlite3.SQLITE_READ:
                read.add(arg1)
            return sqlite3.SQLITE_OK
        con.set_authorizer(authorizer)
        con.execute("SELECT x FROM brand_new_feature WHERE x IN"
                    " (SELECT value FROM json_each(?))", ('["a"]',)).fetchall()
        con.set_authorizer(None)
        self.assertIn("json_each", read)
        self.assertEqual(unguarded_reads(con, read, guarded), {"brand_new_feature"})
        shadow = sqlite3.connect(":memory:")
        self.addCleanup(shadow.close)
        shadow.execute("CREATE TABLE json_each (value)")
        self.assertEqual(unguarded_reads(shadow, {"json_each", "sqlite_master"}, set()),
                         {"json_each"})

    def test_context_fields_read_are_all_keyed(self):
        """Every AccessContext attribute the computation reads is part of the
        memo identity (or is a method, or the replayed side effects)."""
        con, _path, _artifact = file_workspace()
        read = set()
        original = AccessContext.__getattribute__

        def spy(self, name):
            read.add(name)
            return original(self, name)
        try:
            with mock.patch.object(AccessContext, "__getattribute__", spy):
                for make in CONTEXTS.values():
                    _outcome(UNCACHED, con, make)
        finally:
            con.close()
        keyed = {"policy_identity", "policy_revision", "workspace_id", "principal_id",
                 "principal_kind", "effective_principal", "authority_kind",
                 "share_grant_id", "credential_kind", "scopes", "content_policy"}
        methods = {name for name in read if callable(getattr(AccessContext, name, None))
                   or isinstance(getattr(AccessContext, name, None), property)}
        self.assertEqual(read - keyed - methods - {"disclosures", "recordings"}, set())

    def test_missing_guard_disables_reuse_until_repaired(self):
        con, _path, _artifact = file_workspace()
        try:
            con.execute('DROP TRIGGER "ew_view_state_update_reviews"')
            self.assertIsNone(db.view_state_key(con))
            with mock.patch.object(customer_query, "_view_revision_uncached",
                                   side_effect=UNCACHED) as spy:
                customer_query._view_revision(con, alice())
                customer_query._view_revision(con, alice())
                self.assertEqual(spy.call_count, 2)
                # Untracked while unguarded: still correct, never reused.
                con.execute("UPDATE reviews SET judgment='ad'")
                self.assertViewsMatch(con)
                db.ensure_view_state_guards(con)
                self.assertIsNotNone(db.view_state_key(con))
                spy.reset_mock()
                customer_query._view_revision(con, alice())
                customer_query._view_revision(con, alice())
                self.assertEqual(spy.call_count, 1)
        finally:
            con.close()

    def test_altered_guard_is_not_trusted(self):
        con = backend_fixtures.make_memory_workspace()
        self.addCleanup(con.close)
        con.execute('DROP TRIGGER "ew_view_state_insert_episodes"')
        con.execute('CREATE TRIGGER "ew_view_state_insert_episodes" AFTER INSERT ON'
                    ' "episodes" BEGIN SELECT 1; END')
        self.assertIsNone(db.view_state_key(con))
        db.ensure_view_state_guards(con)
        self.assertIsNotNone(db.view_state_key(con))

    def test_guard_install_issues_a_fresh_token_only_after_a_repair(self):
        con = backend_fixtures.make_memory_workspace()
        self.addCleanup(con.close)
        first = db.view_state_key(con)
        db.ensure_view_state_guards(con)  # nothing to repair: e.g. a worker start
        self.assertEqual(db.view_state_key(con), first)
        db.ensure_view_state_guards(con, fresh_token=True)
        second = db.view_state_key(con)
        self.assertNotEqual(second, first)
        con.execute("DELETE FROM meta WHERE key=?", (db.VIEW_STATE_KEY,))
        self.assertIsNone(db.view_state_key(con))
        db.ensure_view_state_guards(con)
        self.assertNotIn(db.view_state_key(con), (None, first, second))

    def test_writes_to_the_token_row_never_recurse(self):
        con = backend_fixtures.make_memory_workspace()
        self.addCleanup(con.close)
        con.execute("PRAGMA recursive_triggers=ON")
        before = db.view_state_key(con)
        con.execute("UPDATE meta SET value=value WHERE key='fts_available'")
        self.assertNotEqual(db.view_state_key(con), before)


class WriterTest(_ViewCase):
    """A write through each kind of writer changes the returned revision
    exactly as the uncached computation does."""

    def setUp(self):
        super().setUp()
        self.con, self.recording, self.artifact = file_workspace()
        self.addCleanup(self.con.close)

    def test_operator_review_and_brand_edits(self):
        con = self.con

        def write():
            ops.save_review(con, "fixture-ad", {"judgment": "ad"}, 1)
            revision = con.execute("SELECT assoc_revision FROM evidence WHERE id='fixture-ad'"
                                   ).fetchone()[0]
            ops.set_brand_assocs(con, "fixture-ad", [{"brand": "Lumen", "basis": "confirmed",
                                                      "channel": "spoken"}], revision)
        self.assertWriterTracked(con, write)
        self.assertWriterTracked(con, lambda: ops.clear_review(con, "fixture-ad", 2))

    def test_operator_manual_evidence(self):
        con = self.con
        self.assertWriterTracked(con, lambda: ops.create_manual_evidence(con, {
            "episode_id": "e2", "start_s": 10.0, "end_s": 20.0,
            "origin_note": "follow-up N"}))

    def test_operator_episode_media_edit(self):
        con = self.con
        revision = con.execute("SELECT revision FROM episodes WHERE id='e1'").fetchone()[0]
        self.assertWriterTracked(con, lambda: ops.set_episode_media(
            con, "e1", {"alignment": "mismatch"}, revision))

    def test_hidden_show_toggle(self):
        con = self.con
        # Customer scope keeps hidden shows: the view must not change, and
        # the write must still be honoured exactly.
        self.assertWriterTracked(con, lambda: ops.set_show_hidden(con, "Same Title", True, 0),
                                 changes=False)

    def test_show_mapping_rename_and_rebuild_dispute(self):
        con = self.con
        self.assertWriterTracked(con, lambda: con.execute(
            "UPDATE customer_show_identities SET display_title='Renamed show'"))

        def conflicting_provenance():
            # A newer episodes.json gives e1 a different feed: the rebuild
            # keeps the identity but marks the mapping disputed.
            metas = [{"ep_id": "e1", "show_title": "Same Title",
                      "feed_url": "https://feed-c.example/rss"},
                     {"ep_id": "e2", "show_title": "Same Title",
                      "feed_url": "https://feed-b.example/rss"}]
            backend_fixtures._add_version(con, "episodes.json", metas)
            customer.rebuild_customer_show_map(con)
        self.assertWriterTracked(con, conflicting_provenance)
        self.assertEqual(con.execute("SELECT basis FROM customer_episode_shows"
                                     " WHERE episode_id='e1'").fetchone()[0], "unresolved")

    def test_direct_dispute_and_alias(self):
        con = self.con
        self.assertWriterTracked(con, lambda: con.execute(
            "UPDATE customer_episode_shows SET basis='unresolved' WHERE episode_id='e2'"))
        self.assertWriterTracked(con, lambda: con.execute(
            "UPDATE episodes SET alias_of='e1' WHERE id='e2'"))

    def test_visibility_policy_row(self):
        con = self.con
        self.assertWriterTracked(con, lambda: con.execute(
            "INSERT INTO source_content_policy (workspace_id, source_key, content_category,"
            " agent_disclosure, export_eligible, updated_at)"
            " VALUES ('local', 'transcript:e2', 'transcript', 'deny', 0, ?)",
            (db.utcnow(),)))

    def test_seeding_and_publication(self):
        con = self.con

        def seed():
            tv = backend_fixtures._add_version(con, "transcript:e3", {"segments": [
                {"start": 0.0, "end": 9.0, "text": "Lumen sponsors episode three"}]})
            con.execute("INSERT INTO episodes (id, kind, show_title, ep_title, pub_date,"
                        " fetch_date, transcript_version_id, revision, updated_at)"
                        " VALUES ('e3', 'supplied', 'Third Show', 'Three', '2026-03-12',"
                        " '2026-09-21', ?, 1, ?)", (tv, db.utcnow()))
            customer.rebuild_customer_show_map(con)
            backend_fixtures.publish_all_records(con)
        self.assertWriterTracked(con, seed)

    def test_import_upload_transcript(self):
        con = self.con
        self.assertWriterTracked(con, lambda: import_sources.import_upload_transcript(
            con, {"ep_id": "fresh-upload", "segments": [
                {"start": 0.0, "end": 5.0, "text": "Lumen upload words"}]},
            actor="test", origin_note="follow-up N"))

    def test_conversion_apply(self):
        con = self.con
        self.assertWriterTracked(con, lambda: conversion.apply(con), changes=False)

    def test_recording_file_removed_and_replaced(self):
        con = self.con
        self.assertWriterTracked(con, lambda: self.recording.unlink())
        self.recording.write_bytes(b"not the pinned recording")
        self.assertViewsMatch(con)

    def test_recording_file_bytes_change_in_place(self):
        con = self.con
        self.assertWriterTracked(con, lambda: self.recording.write_bytes(
            self.recording.read_bytes() + b"\0\0"))

    def test_audio_row_allow_then_deny(self):
        """The episode audio row the audio gate reads (CLIPCORE-R3-01) is a
        tracked writer: a stored 'refused' or proved revision never
        outlives a change of that row (VREV-R1-01)."""
        con = self.con
        self.assertWriterTracked(con, lambda: con.execute(
            "INSERT INTO source_content_policy (workspace_id, source_key, content_category,"
            " agent_disclosure, updated_at) VALUES ('local', 'episode:e1', 'audio',"
            " 'allow', 'fixture')"))
        self.assertWriterTracked(con, lambda: con.execute(
            "UPDATE source_content_policy SET agent_disclosure='deny'"
            " WHERE source_key='episode:e1' AND content_category='audio'"))

    def test_transient_probe_failure_is_never_reused(self):
        """A proof that failed transiently (probe timeout, I/O error) is
        retried by every uncached call, so nothing built on it is kept; a
        verified proof is kept while the file signature holds, exactly as
        media._PROOF_CACHE keeps it. A context the audio gate refuses
        (CLIPCORE-R3-01: hosted, no allowing audio row, or a credential that
        never plays audio) does no proof work, so its revision does not
        depend on the failure and may be stored."""
        con = self.con
        from earwitness.app import media
        # e1's audio row allows, so alice (a member's browser session) is
        # gated and allowed: a hosted context that proves the recording.
        con.execute("INSERT INTO source_content_policy (workspace_id, source_key,"
                    " content_category, agent_disclosure, updated_at)"
                    " VALUES ('local', 'episode:e1', 'audio', 'allow', 'fixture')")
        proving = {"default", "owner", "alice"}
        refused = set(CONTEXTS) - proving    # bob: reader bearer; guest: share
        healthy = self.assertViewsMatch(con)
        failures = (mock.patch.object(media, "_recording_duration", return_value=None),
                    mock.patch.object(artifacts, "sha256_file", side_effect=OSError("EMFILE")))
        for failure in failures:
            customer_query._VIEW_MEMO.clear()
            media._PROOF_CACHE.clear()
            with failure, mock.patch.object(customer_query, "_view_revision_uncached",
                                            side_effect=UNCACHED) as spy:
                during = self.assertViewsMatch(con)
                # proving contexts: never stored; refused ones: stored once
                self.assertEqual(spy.call_count, 2 * len(proving) + len(refused))
            for name in proving:
                self.assertNotEqual(during[name], healthy[name], name)
            for name in refused:
                self.assertEqual(during[name], healthy[name], name)
            self.assertEqual(self.assertViewsMatch(con), healthy)

    def test_retention_deadline_rows_and_clock(self):
        con = self.con
        self.assertWriterTracked(con, lambda: con.execute(
            "INSERT INTO p01p04_retention VALUES (?, '2000-01-01T00:00:00+00:00')",
            (self.artifact,)))
        self.assertWriterTracked(con, lambda: con.execute(
            "UPDATE p01p04_retention SET retain_until='2999-01-01T00:00:00+00:00'"))
        with mock.patch.object(artifacts, "utcnow", return_value="3000-01-01T00:00:00+00:00"):
            # No write at all: only the clock crosses the deadline.
            after = self.assertViewsMatch(self.con)
        self.assertNotEqual(after, self.assertViewsMatch(self.con))

    def test_schema_change_without_row_writes(self):
        con = self.con
        con.execute("INSERT INTO p01p04_retention VALUES (?, '2000-01-01T00:00:00+00:00')",
                    (self.artifact,))
        # DROP fires no trigger; the schema digest still moves the key.
        self.assertWriterTracked(con, lambda: con.execute("DROP TABLE p01p04_retention"))

    def test_rolled_back_write(self):
        con = self.con
        before = self.assertViewsMatch(con)
        con.execute("BEGIN IMMEDIATE")
        con.execute("UPDATE reviews SET judgment='not_ad'")
        inside = self.assertViewsMatch(con)
        self.assertNotEqual(before, inside)
        con.execute("ROLLBACK")
        self.assertEqual(self.assertViewsMatch(con), before)

    def test_unrelated_write_stays_correct(self):
        con = self.con
        self.assertWriterTracked(con, lambda: db.audit(con, "t", "noop", "x", "1", {}, 0),
                                 changes=False)


class SeedAndRestoreTest(_ViewCase):
    def setUp(self):
        super().setUp()
        from tests.test_seed_identity import _workspace_cli, write_corpus
        self.cli, self.write_corpus = _workspace_cli(), write_corpus
        self.corpus = Path(tempfile.mkdtemp(prefix="follow-up-n-corpus-"))
        self.addCleanup(shutil.rmtree, self.corpus, True)
        self.ws = Path(tempfile.mkdtemp(prefix=".follow-up-n-ws-", dir=REPO))
        self.addCleanup(shutil.rmtree, self.ws, True)

    def test_seed_import_and_reseed(self):
        self.write_corpus(self.corpus)
        con = db.connect(self.ws)
        try:
            db.init_schema(con)
            self.assertWriterTracked(con, lambda: import_sources.seed_all(
                self.corpus, con, "test"))
            self.write_corpus(self.corpus, shift_e2=3.0)
            self.assertWriterTracked(con, lambda: import_sources.seed_all(
                self.corpus, con, "test"))
        finally:
            con.close()

    def test_restore_replaces_the_file_under_a_running_memo(self):
        self.write_corpus(self.corpus)
        con = db.connect(self.ws)
        try:
            db.init_schema(con)
            import_sources.seed_all(self.corpus, con, "test")
            backup = self.ws / "backup.sqlite3"
            ops.backup_to(con, backup)
            ops.save_review(con, con.execute("SELECT id FROM evidence LIMIT 1").fetchone()[0],
                            {"judgment": "not_ad"}, 0)
            changed = self.assertViewsMatch(con)
        finally:
            con.close()
        self.cli.restore(self.ws, str(backup.relative_to(REPO)), True)
        con = db.connect(self.ws)
        try:
            restored = self.assertViewsMatch(con)
            self.assertNotEqual(restored, changed)
        finally:
            con.close()

    def test_restored_copy_of_an_earlier_state_is_never_confused(self):
        """A byte copy carries its token; a restore always issues a new one,
        and a copy diverging by any write gets its own."""
        self.write_corpus(self.corpus)
        con = db.connect(self.ws)
        try:
            db.init_schema(con)
            import_sources.seed_all(self.corpus, con, "test")
            first = db.view_state_key(con)
            backup = self.ws / "backup.sqlite3"
            ops.backup_to(con, backup)
        finally:
            con.close()
        self.cli.restore(self.ws, str(backup.relative_to(REPO)), True)
        con = db.connect(self.ws)
        try:
            self.assertNotEqual(db.view_state_key(con), first)
        finally:
            con.close()


class CrossConnectionTest(_ViewCase):
    def test_cli_write_from_another_process_invalidates(self):
        con, _path, _artifact = file_workspace()
        try:
            before = self.assertViewsMatch(con)
            path = con.execute("PRAGMA database_list").fetchone()[2]
            subprocess.run([sys.executable, "-c",
                            "import sqlite3, sys\n"
                            "c = sqlite3.connect(sys.argv[1])\n"
                            "c.execute(\"UPDATE customer_show_identities SET display_title='CLI'\")\n"
                            "c.execute(\"UPDATE reviews SET judgment='ad'\")\n"
                            "c.commit()\n", path], check=True, env={**os.environ, "PYTHONPATH": ""})
            # The same long-lived connection and a fresh one both see it.
            after = self.assertViewsMatch(con)
            self.assertNotEqual(before, after)
            fresh = other_connection(con)
            try:
                self.assertEqual(self.assertViewsMatch(fresh), after)
            finally:
                fresh.close()
        finally:
            con.close()

    def test_write_on_one_connection_invalidates_another(self):
        con, _path, _artifact = file_workspace()
        reader = other_connection(con)
        try:
            before = self.assertViewsMatch(reader)
            ops.save_review(con, "fixture-ad", {"judgment": "ad"}, 1)
            after = self.assertViewsMatch(reader)
            self.assertNotEqual(before, after)
        finally:
            reader.close()
            con.close()


class PrincipalTest(_ViewCase):
    def test_different_visibility_same_policy_identity(self):
        con, _path, _artifact = file_workspace()
        try:
            self.assertEqual(alice().policy_identity, bob().policy_identity)
            for order in (("alice", "bob"), ("bob", "alice")):
                customer_query._VIEW_MEMO.clear()
                views = self.assertViewsMatch(con, {name: CONTEXTS[name] for name in order})
                self.assertNotEqual(views["alice"], views["bob"])
            # The revision reads no source content (research data token plus
            # visibility), so it discloses nothing for admission to re-check.
            a, b = alice(), bob()
            customer_query._view_revision(con, a)
            customer_query._view_revision(con, b)
            self.assertEqual((a.disclosures, b.disclosures), (set(), set()))
        finally:
            con.close()

    def test_principals_never_share_an_entry(self):
        con, _path, _artifact = file_workspace()
        try:
            carol = lambda: AccessContext.create(  # noqa: E731
                principal_id="carol", principal_kind="viewer", workspace_id="local",
                credential_kind="browser_session", policy_identity="local:7")
            with mock.patch.object(customer_query, "_view_revision_uncached",
                                   side_effect=UNCACHED) as spy:
                customer_query._view_revision(con, alice())
                customer_query._view_revision(con, carol())
                self.assertEqual(spy.call_count, 2)
            # A context whose policy is narrowed after construction is keyed
            # by its current policy, not by its (unchanged) policy identity.
            narrowed = {"alice": alice, "narrowed": lambda: _with_sources(alice(), ["transcript:e2"])}
            views = self.assertViewsMatch(con, narrowed)
            self.assertNotEqual(views["alice"], views["narrowed"])
        finally:
            con.close()


def _with_sources(ctx, sources):
    ctx.content_policy["sources"] = sources
    return ctx


class CountingTest(_ViewCase):
    """The expensive path runs once per data state (counted, never timed)."""

    def test_once_per_state_across_connections_and_contexts(self):
        con, _path, _artifact = file_workspace()
        try:
            with mock.patch.object(customer_query, "_view_revision_uncached",
                                   side_effect=UNCACHED) as spy, \
                    mock.patch.object(customer, "_run_match", wraps=customer._run_match) as scans:
                for _ in range(4):
                    request = other_connection(con)
                    try:
                        customer_query._view_revision(request, alice())
                        customer_query._view_revision(request, bob())
                    finally:
                        request.close()
                self.assertEqual(spy.call_count, 2)  # one per context
                self.assertEqual(scans.call_count, 0)  # a research data token, no rescan
                ops.save_review(con, "fixture-ad", {"judgment": "ad"}, 1)
                for _ in range(3):
                    customer_query._view_revision(con, alice())
                self.assertEqual(spy.call_count, 3)
                self.assertEqual(scans.call_count, 0)
        finally:
            con.close()

    def test_search_and_alert_requests_reuse_the_revision(self):
        con, _path, _artifact = file_workspace()
        try:
            criteria = {"version": 2, "mode": "ads", "brands": ["Lumen"]}
            with mock.patch.object(customer_query, "_view_revision_uncached",
                                   side_effect=UNCACHED) as spy:
                customer.query(con, criteria, ctx=alice())
                customer.query(con, dict(criteria, page=2), ctx=alice())
                customer.list_alerts(con, ctx=alice())
                customer.list_alerts(con, ctx=alice())
                self.assertEqual(spy.call_count, 1)
        finally:
            con.close()


class PropertyTest(_ViewCase):
    """Randomized write sequences: every reuse equals the uncached value."""

    def _ops(self, con, recording, artifact, clock):
        def review():
            current = con.execute("SELECT revision FROM reviews WHERE evidence_id='fixture-ad'"
                                  ).fetchone()
            ops.save_review(con, "fixture-ad",
                            {"judgment": self.rng.choice(["ad", "not_ad", "unsure"])},
                            current[0] if current else 0)

        def assocs():
            revision = con.execute("SELECT assoc_revision FROM evidence WHERE id='fixture-ad'"
                                   ).fetchone()[0]
            ops.set_brand_assocs(con, "fixture-ad", [{"brand": self.rng.choice(["Lumen", "Acme"]),
                                                      "basis": self.rng.choice(
                                                          ["confirmed", "suggested"])}], revision)

        def pub_date():
            con.execute("UPDATE episodes SET pub_date=? WHERE id=?",
                        (self.rng.choice(["2026-01-01", "2026-05-05", ""]),
                         self.rng.choice(["e1", "e2"])))

        def alias():
            con.execute("UPDATE episodes SET alias_of=? WHERE id='e2'",
                        (self.rng.choice(["e1", None]),))

        def policy():
            if self.rng.random() < 0.5:
                con.execute("DELETE FROM source_content_policy WHERE history_origin='follow-up-n'")
            else:
                con.execute("INSERT OR REPLACE INTO source_content_policy (workspace_id,"
                            " source_key, content_category, agent_disclosure, export_eligible,"
                            " history_origin, updated_at) VALUES ('local', ?, ?, 'deny', 0,"
                            " 'follow-up-n', ?)",
                            (self.rng.choice(["transcript:e1", "transcript:e2",
                                              "detector:fixture-ad"]),
                             self.rng.choice(["metadata", "excerpt", "transcript"]),
                             db.utcnow()))

        def show():
            if self.rng.random() < 0.5:
                con.execute("UPDATE customer_show_identities SET display_title=?",
                            (self.rng.choice(["One", "Two"]),))
            else:
                con.execute("UPDATE customer_episode_shows SET basis=? WHERE episode_id='e1'",
                            (self.rng.choice(["unresolved", "feed_url"]),))

        def unrelated():
            db.audit(con, "t", "noop", "x", "1", {}, 0)

        def rolled_back():
            con.execute("BEGIN IMMEDIATE")
            con.execute("UPDATE reviews SET judgment='ad'")
            con.execute("ROLLBACK")

        def recording_file():
            if recording.exists():
                self.saved = recording.read_bytes()
                recording.unlink()
            else:
                recording.write_bytes(self.saved)

        def retention():
            choice = self.rng.choice(["past", "future", "none"])
            con.execute("DELETE FROM p01p04_retention WHERE artifact_id=?", (artifact,))
            if choice != "none":
                con.execute("INSERT INTO p01p04_retention VALUES (?, ?)", (artifact,
                            "2000-01-01T00:00:00+00:00" if choice == "past"
                            else "2500-01-01T00:00:00+00:00"))

        def clock_jump():
            clock[0] = self.rng.choice(["2026-10-09T00:00:00+00:00",
                                        "2600-01-01T00:00:00+00:00"])
        return [review, assocs, pub_date, alias, policy, show, unrelated, rolled_back,
                recording_file, retention, clock_jump]

    def test_randomized_writes_match_uncached(self):
        for seed in (11, 23, 42):
            with self.subTest(seed=seed):
                customer_query._VIEW_MEMO.clear()
                self.rng = random.Random(seed)
                con, recording, artifact = file_workspace()
                clock = ["2026-10-09T00:00:00+00:00"]
                try:
                    with mock.patch.object(artifacts, "utcnow", lambda: clock[0]):
                        steps = self._ops(con, recording, artifact, clock)
                        seen = set()
                        for _ in range(30):
                            step = self.rng.choice(steps)
                            step()
                            seen.add(json.dumps(self.assertViewsMatch(con), sort_keys=True))
                        self.assertGreater(len(seen), 3)  # the writes really moved the view
                finally:
                    con.close()


if __name__ == "__main__":
    unittest.main()
