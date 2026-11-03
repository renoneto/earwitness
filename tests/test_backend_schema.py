"""K01 schema/atomicity/admission regression tests (red-capable).

Real consumer behavior on real connections: nested/trigger-aborted
mutations roll back fully, reads never commit the caller, deferred-FK
commit failures roll back, and admission never creates or upgrades files.
"""
from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from earwitness.app import access, collaboration, db
from tests import backend_fixtures


class TransactionPrimitiveTest(unittest.TestCase):
    def test_nested_mutation_rolls_back_outer(self):
        con = backend_fixtures.make_memory_workspace()
        try:
            with self.assertRaises(RuntimeError):
                with db.transaction(con, write=True):
                    collaboration.create_workspace(con, "ws-nested",
                                                   "Nested", "owner")
                    raise RuntimeError("outer boom")
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM workspaces WHERE id='ws-nested'"
                            ).fetchone()[0], 0)
            self.assertFalse(con.in_transaction)
        finally:
            con.close()

    def test_aborted_share_creation_publishes_no_grant(self):
        con = backend_fixtures.make_memory_workspace()
        try:
            collaboration.create_workspace(con, "local", "Synthetic", "owner")
            con.execute("CREATE TRIGGER abort_share_audit BEFORE INSERT ON"
                        " share_audit BEGIN SELECT RAISE(ABORT,"
                        " 'fixture abort'); END")
            before = db.aggregate_revision(con)
            with self.assertRaises(sqlite3.IntegrityError):
                collaboration.create_share(con, "local", "episode", "e1",
                                           "named-fixture-guest", ["ep_title"],
                                           "owner")
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM shares").fetchone()[0], 0)
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM share_audit").fetchone()[0],
                0)
            self.assertEqual(db.aggregate_revision(con), before)
            self.assertFalse(con.in_transaction)
        finally:
            con.close()

    def test_reads_do_not_commit_caller_transaction(self):
        con = backend_fixtures.make_memory_workspace()
        self.addCleanup(con.close)
        try:
            collaboration.create_workspace(con, "local", "Synthetic", "owner")
            with db.transaction(con, write=True):
                con.execute("INSERT INTO workspaces (id, name, created_at)"
                            " VALUES ('sentinel', 'S', '2026-01-01T00:00:00+00:00')")
                self.assertEqual(
                    collaboration.get_role(con, "local", "owner"), "owner")
                self.assertTrue(con.in_transaction)
                raise RuntimeError("sentinel rollback")
        except RuntimeError:
            pass
        self.assertEqual(
            con.execute("SELECT COUNT(*) FROM workspaces WHERE id='sentinel'"
                        ).fetchone()[0], 0)
        self.assertFalse(con.in_transaction)

    def test_share_state_read_is_pure(self):
        con = backend_fixtures.make_memory_workspace()
        try:
            collaboration.create_workspace(con, "local", "Synthetic", "owner")
            created = collaboration.create_share(con, "local", "episode", "e1",
                                                 "named-fixture-guest",
                                                 ["ep_title"], "owner")
            with db.transaction(con, write=True):
                state = collaboration.share_state(con, created["share_id"])
                self.assertEqual(state["state"], "active")
                self.assertTrue(con.in_transaction)
        finally:
            con.close()

    def test_deferred_fk_commit_failure_rolls_back(self):
        con = backend_fixtures.make_memory_workspace()
        try:
            con.execute("CREATE TABLE k_parent(id TEXT PRIMARY KEY)")
            con.execute("CREATE TABLE k_child(id TEXT PRIMARY KEY,"
                        " pid TEXT REFERENCES k_parent(id)"
                        " DEFERRABLE INITIALLY DEFERRED)")
            with self.assertRaises(sqlite3.IntegrityError):
                with db.transaction(con, write=True):
                    con.execute("INSERT INTO k_child VALUES ('c1','missing')")
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM k_child").fetchone()[0], 0)
            self.assertFalse(con.in_transaction)
        finally:
            con.close()


class AdmissionTest(unittest.TestCase):
    def test_readonly_handle_serves_lookups(self):
        import tempfile
        tmpdir = Path(tempfile.mkdtemp(prefix="ew-k01-ro-"))
        db_path = backend_fixtures.make_workspace(tmpdir, schema_version=8)
        con = sqlite3.connect(str(db_path), isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            collaboration.create_workspace(con, "local", "Synthetic", "owner")
        finally:
            con.close()
        ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True,
                             isolation_level=None)
        ro.row_factory = sqlite3.Row
        try:
            self.assertEqual(
                ro.execute("SELECT role FROM memberships WHERE workspace_id='local'"
                           " AND member='owner'").fetchone()[0], "owner")
        finally:
            ro.close()

    def test_open_existing_absent_path_creates_nothing(self):
        import tempfile
        missing = Path(tempfile.mkdtemp(prefix="ew-k01-missing-")) / "nope.sqlite3"
        with self.assertRaises(FileNotFoundError):
            db.open_existing(missing, readonly=True)
        self.assertFalse(missing.exists())
        with self.assertRaises(FileNotFoundError):
            db.open_existing(missing, readonly=False)
        self.assertFalse(missing.exists())

    def test_open_existing_moved_before_connect_creates_no_replacement(self):
        import shutil
        import sqlite3 as _sqlite3
        import tempfile
        from unittest import mock as _mock
        for readonly in (True, False):
            tmpdir = Path(tempfile.mkdtemp(prefix="ew-k01-race-"))
            db_path = backend_fixtures.make_workspace(tmpdir, schema_version=8)
            before = db_path.read_bytes()
            moved = tmpdir / "workspace.sqlite3.moved"
            real_connect = _sqlite3.connect
            calls = []

            def racing_connect(*args, **kwargs):
                calls.append((args, kwargs))
                if not moved.exists():
                    shutil.move(str(db_path), str(moved))
                return real_connect(*args, **kwargs)

            with _mock.patch.object(_sqlite3, "connect",
                                    side_effect=racing_connect):
                with self.assertRaises(FileNotFoundError):
                    db.open_existing(db_path, readonly=readonly)
            # No empty replacement at the admitted path; the moved
            # original is retained byte-identical.
            self.assertFalse(db_path.exists())
            self.assertEqual(moved.read_bytes(), before)
            self.assertTrue(calls)

    def test_open_existing_v7_refuses_without_upgrade(self):
        v7_path = backend_fixtures.fresh_v7_file()
        with self.assertRaises(RuntimeError):
            db.open_existing(v7_path, readonly=True)
        con = sqlite3.connect(str(v7_path))
        try:
            version = con.execute("SELECT MAX(version) FROM schema_version"
                                  ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(version, 7)

    def test_pure_lookup_refuses_unprepared_schema(self):
        con = sqlite3.connect(":memory:", isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            with self.assertRaises(RuntimeError):
                collaboration.get_role(con, "local", "owner")
        finally:
            con.close()

    def test_occupied_fixture_target_refuses_without_modification(self):
        import tempfile
        tmpdir = Path(tempfile.mkdtemp(prefix="ew-k01-occupied-"))
        db_path = backend_fixtures.make_workspace(tmpdir, schema_version=8)
        con = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            con.execute("INSERT INTO meta (key, value) VALUES"
                        " ('coordinator-sentinel', 'keep')")
            con.commit()
        finally:
            con.close()
        before = db_path.stat().st_size
        with self.assertRaises(FileExistsError):
            backend_fixtures.make_workspace(tmpdir, schema_version=8)
        self.assertEqual(db_path.stat().st_size, before)
        con = sqlite3.connect(str(db_path))
        try:
            kept = con.execute("SELECT value FROM meta WHERE key="
                               "'coordinator-sentinel'").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(kept, "keep")

    def test_unsupported_fixture_version_raises(self):
        import tempfile
        tmpdir = Path(tempfile.mkdtemp(prefix="ew-k01-badver-"))
        with self.assertRaises(ValueError):
            backend_fixtures.make_workspace(tmpdir, schema_version=6)
        self.assertFalse((tmpdir / "workspace.sqlite3").exists())

    def test_readonly_open_survives_special_characters_in_path(self):
        import tempfile
        quoted = Path(tempfile.mkdtemp(prefix="ew-k01-")) / "quoted#scope?view"
        db_path = backend_fixtures.make_workspace(quoted, schema_version=8)
        con = db.open_existing(db_path, readonly=True)
        try:
            opened = con.execute("PRAGMA database_list").fetchone()
            self.assertEqual(Path(opened[2]).resolve(), db_path.resolve())
            self.assertGreater(
                con.execute("SELECT COUNT(*) FROM episodes").fetchone()[0], 0)
        finally:
            con.close()
        # WAL sidecars of the same database are expected; any other
        # database file would prove the URI opened/created elsewhere.
        dbs = sorted(p.name for p in quoted.iterdir()
                     if p.suffix in (".sqlite3", ".db") and
                     not p.name.endswith(("-shm", "-wal")))
        self.assertEqual(dbs, ["workspace.sqlite3"])


class MigrationTest(unittest.TestCase):
    def test_v7_to_v8_preserves_rows_and_keeps_legacy_mode_null(self):
        v7_path = backend_fixtures.fresh_v7_file()
        con = sqlite3.connect(str(v7_path), isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            before_ep = con.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
            self.assertGreater(before_ep, 0)
            db.migrate_7_to_8(con)
            self.assertEqual(db._schema_version(con), 8)
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM episodes").fetchone()[0],
                before_ep)
            self.assertTrue(db.has_v8_tables(con))
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM campaigns WHERE mode IS NOT"
                            " NULL").fetchone()[0], 0)
            self.assertEqual(db.verify_schema(con)["problems"], [])
        finally:
            con.close()

    def test_down_rehearsal_refuses_populated_v8(self):
        con = backend_fixtures.make_memory_workspace()
        try:
            backend_fixtures.publish_sources(con)
            with self.assertRaises(RuntimeError):
                db.migrate_8_to_7(con)
            self.assertEqual(db._schema_version(con), 8)
        finally:
            con.close()

    def test_down_rehearsal_refuses_real_v8_authorship(self):
        con = backend_fixtures.make_memory_workspace()
        try:
            con.execute(
                "INSERT INTO campaigns (name, status, mode, revision,"
                " updated_at) VALUES ('R', 'active', 'research', 1, ?)",
                (db.utcnow(),))
            with self.assertRaises(RuntimeError):
                db.migrate_8_to_7(con)
            self.assertEqual(db._schema_version(con), 8)
        finally:
            con.close()

    def test_down_rehearsal_allows_empty_v8(self):
        import tempfile
        tmpdir = Path(tempfile.mkdtemp(prefix="ew-k01-down-"))
        # Truly empty v8: schema only, no seeded rows or publications.
        db_path = tmpdir / "workspace.sqlite3"
        seed_con = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            backend_fixtures._prepare(seed_con)
            seed_con.commit()
        finally:
            seed_con.close()
        con = sqlite3.connect(str(db_path), isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            db.migrate_8_to_7(con)
            self.assertEqual(db._schema_version(con), 7)
        finally:
            con.close()

    def test_fixture_reader_profile_uses_real_grant_scopes(self):
        con = backend_fixtures.make_memory_workspace()
        try:
            profile = json.loads(con.execute(
                "SELECT value FROM meta WHERE key='agent_profile'"
                ).fetchone()[0])
            self.assertEqual(profile["profile"], "reader")
            self.assertEqual(profile["scopes"],
                             ["research:read", "transcripts:read"])
            ctx = access.AccessContext.create(
                principal_id="reader-1", principal_kind="viewer",
                workspace_id="local", scopes=set(profile["scopes"]),
                credential_kind="reader_bearer")
            ctx.require_scope("research:read")
            ctx.require_scope("transcripts:read")
        finally:
            con.close()

    def test_literal_v7_fixture_carries_no_v8_objects(self):
        v7_path = backend_fixtures.fresh_v7_file()
        con = sqlite3.connect(str(v7_path), isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            self.assertEqual(db._schema_version(con), 7)
            self.assertFalse(db.has_v8_tables(con))
            cols = {r["name"] for r in con.execute("PRAGMA table_info(campaigns)")}
            self.assertNotIn("mode", cols)
            self.assertGreater(
                con.execute("SELECT COUNT(*) FROM episodes").fetchone()[0], 0)
            profile = con.execute("SELECT value FROM meta WHERE key="
                                  "'agent_profile'").fetchone()[0]
            self.assertIn("reader", profile)
            digest = con.execute("SELECT export_digest FROM campaign_reports"
                                 " WHERE id=1").fetchone()[0]
            payload = con.execute("SELECT payload FROM exports WHERE id=1"
                                  ).fetchone()[0]
            import hashlib
            self.assertEqual(digest, hashlib.sha256(
                payload.encode("utf-8")).hexdigest())
        finally:
            con.close()

    def test_upgrade_refuses_open_caller_transaction(self):
        v7_path = backend_fixtures.fresh_v7_file()
        con = sqlite3.connect(str(v7_path), isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute("INSERT INTO meta (key, value) VALUES"
                        " ('caller-sentinel', 'hold')")
            with self.assertRaises(RuntimeError):
                db.migrate_7_to_8(con)
            self.assertTrue(con.in_transaction)
            kept = con.execute("SELECT value FROM meta WHERE key="
                               "'caller-sentinel'").fetchone()[0]
            self.assertEqual(kept, "hold")
            self.assertEqual(db._schema_version(con), 7)
            con.rollback()
            self.assertIsNone(con.execute("SELECT value FROM meta WHERE key="
                                          "'caller-sentinel'").fetchone())
            self.assertEqual(db._schema_version(con), 7)
            db.migrate_7_to_8(con)
            self.assertEqual(db._schema_version(con), 8)
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
