"""DEMO2 pass 9 (Sol r3 DEMO2-SOL-R3-01, server side).

1. The session check (session_contract_marker -> conversion.readiness)
   qualified every published source on every request. The qualification
   is now remembered per (workspace database, view state), the key the
   research view-revision memo uses (customer_query._VIEW_MEMO): any row
   write to a guarded table, any schema change, a different database file
   and a new process all miss; the recording file and retention clock
   inputs are re-checked on every reuse. A reuse equals a fresh
   computation exactly.

   Inputs source_qualification reads (pinned by the authorizer test):
   tables source_states, source_versions, evidence, episodes,
   evidence_history, review_history, association_history,
   episode_history, observation_revisions, field_confirmations,
   media_artifacts, transcript_versions, p01p04_retention (and whatever
   artifacts.check_rights reads); outside the database, the pinned
   recording file and the retention clock (media.recording_proof).

2. Search filter values (brands, shows, categories) ran the same
   whole-collection ads and episodes match once per field. The values of
   all three fields are now computed from one match per (database, view
   state, caller context incl. permissions) and remembered under that key.

Counting tests, not timings.
Run: PYTHONPATH=. python -m unittest tests.test_demo2_pass9
"""
from __future__ import annotations

import os
import sqlite3
import unittest
from unittest import mock

from earwitness.app import conversion, customer, customer_query, db, media
from tests import backend_fixtures
from tests.test_followup_n_view_revision import (alice, bob, file_workspace, guest,
                                                 other_connection, owner)


class _Counter:
    def __init__(self, target, name):
        self.calls = 0
        original = getattr(target, name)

        def wrapped(*args, **kwargs):
            self.calls += 1
            return original(*args, **kwargs)
        self.patch = mock.patch.object(target, name, wrapped)

    def __enter__(self):
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()


def _delete_and_restore(con, table):
    """Delete the first row of table and insert it back, in the caller's
    write transaction: the same contents, but a real change on both
    statements (the research state's triggers fire on each)."""
    con.execute(f'CREATE TEMP TABLE restore_row AS SELECT * FROM "{table}"'
                f' WHERE rowid=(SELECT min(rowid) FROM "{table}")')
    con.execute("PRAGMA defer_foreign_keys=ON")
    con.execute(f'DELETE FROM "{table}" WHERE rowid=(SELECT min(rowid) FROM "{table}")')
    con.execute(f'INSERT INTO "{table}" SELECT * FROM restore_row')
    con.execute("DROP TABLE restore_row")


def _fresh_qualification(con):
    """The uncached computation (no memo involved)."""
    with db.transaction(con, write=False):
        with conversion.citations.provenance_memo():
            return conversion._source_qualification(con)


class _MemoCase(unittest.TestCase):
    def setUp(self):
        customer_query._VIEW_MEMO.clear()
        conversion._CLASSIFIED.clear()
        self.addCleanup(customer_query._VIEW_MEMO.clear)
        self.addCleanup(conversion._CLASSIFIED.clear)


class QualificationMemoTest(_MemoCase):
    def setUp(self):
        super().setUp()
        self.con, self.recording, self.artifact = file_workspace()
        self.addCleanup(self.con.close)

    def qualify(self, con=None):
        """(result, computations) for one session-path qualification."""
        con = con or self.con
        with _Counter(conversion, "_source_qualification") as computed:
            with db.transaction(con, write=False):
                result = conversion.source_qualification(con)
        return result, computed.calls

    def assertFresh(self, con=None, *, computes):
        con = con or self.con
        result, calls = self.qualify(con)
        self.assertEqual(result, _fresh_qualification(con))
        if computes is not None:
            self.assertEqual(calls, computes)
        return result

    # -- reuse -----------------------------------------------------------

    def test_second_session_check_qualifies_no_source(self):
        first = self.assertFresh(computes=1)
        self.assertTrue(first["qualified"])
        self.assertFresh(computes=0)
        # The session route's own path.
        with _Counter(conversion, "_source_qualification") as computed:
            with db.transaction(self.con, write=False):
                marker = customer.session_contract_marker(self.con)
        self.assertEqual(computed.calls, 0)
        with db.transaction(self.con, write=False):
            self.assertEqual(marker, customer.SESSION_CONTRACT
                             if conversion.readiness(self.con)["ready"] else None)

    def test_reuse_across_connections_to_the_same_file(self):
        self.assertFresh(computes=1)
        other = other_connection(self.con)
        self.addCleanup(other.close)
        self.assertFresh(other, computes=0)

    def test_reused_result_is_a_copy(self):
        first, _ = self.qualify()
        first["unavailable"].append("tampered")
        first["qualified"] = False
        again, calls = self.qualify()
        self.assertEqual(calls, 0)
        self.assertEqual(again, {"qualified": True, "unavailable": []})

    # -- inputs ------------------------------------------------------------

    def test_qualification_reads_only_guarded_tables(self):
        """Every table the computation reads moves the view state on any
        row write (so the memo key changes)."""
        read = set()

        def authorizer(action, arg1, _arg2, _db_name, _trigger):
            if action == sqlite3.SQLITE_READ:
                read.add(arg1)
            return sqlite3.SQLITE_OK
        fresh = other_connection(self.con)
        fresh.set_authorizer(authorizer)
        try:
            _fresh_qualification(fresh)
        finally:
            fresh.close()
        guarded = set(db._view_state_guarded_tables(db._schema_rows(self.con)))
        self.assertTrue({"source_states", "source_versions", "evidence", "episodes",
                         "evidence_history", "review_history", "association_history",
                         "episode_history", "media_artifacts", "transcript_versions",
                         "p01p04_retention"} <= read, read)
        self.assertEqual(read - guarded - {"sqlite_master"}, set())
        # Read only for pinned observations / holds (not in this fixture).
        self.assertTrue({"observation_revisions", "field_confirmations",
                         "p01p04_holds"} <= guarded)
        self.assertFalse(read & db.VIEW_STATE_UNREAD)

    def test_a_write_to_each_input_table_recomputes(self):
        """A real change to a row of each input table recomputes (the row is
        deleted and put back: the research state moves on both). A rewrite
        to equal values changes no input and is reused (READINESS-CACHE)."""
        tables = ["source_states", "source_versions", "evidence", "episodes",
                  "evidence_history", "review_history", "association_history",
                  "episode_history", "media_artifacts", "transcript_versions"]
        self.assertFresh(computes=1)
        for table in tables:
            with self.subTest(table=table):
                with db.transaction(self.con, write=True):
                    self.con.execute(f'UPDATE "{table}" SET rowid=rowid'
                                     f' WHERE rowid=(SELECT min(rowid) FROM "{table}")')
                self.assertFresh(computes=0)
                with db.transaction(self.con, write=True):
                    _delete_and_restore(self.con, table)
                self.assertFresh(computes=1)
                self.assertFresh(computes=0)
        with db.transaction(self.con, write=True):
            self.con.execute("INSERT INTO p01p04_retention VALUES (?, '2999-01-01T00:00:00+00:00')",
                             (self.artifact,))
        self.assertFresh(computes=1)

    def test_a_changed_source_payload_turns_qualification_off(self):
        self.assertTrue(self.assertFresh(computes=1)["qualified"])
        version = self.con.execute("SELECT transcript_version_id FROM episodes"
                                   " WHERE transcript_version_id IS NOT NULL ORDER BY id").fetchone()[0]
        with db.transaction(self.con, write=True):
            self.con.execute("UPDATE source_versions SET payload=payload || ' ' WHERE id=?",
                             (version,))
        after = self.assertFresh(computes=1)
        self.assertFalse(after["qualified"])

    def test_a_removed_publication_is_reported_at_once(self):
        self.assertFresh(computes=1)
        evidence = self.con.execute("SELECT id FROM evidence ORDER BY id LIMIT 1").fetchone()[0]
        with db.transaction(self.con, write=True):
            self.con.execute("DELETE FROM source_states WHERE kind='evidence' AND record_id=?",
                             (evidence,))
        after = self.assertFresh(computes=1)
        self.assertIn("evidence:" + evidence, after["unavailable"])

    def test_a_rolled_back_write_is_never_reused(self):
        self.assertFresh(computes=1)
        evidence = self.con.execute("SELECT id FROM evidence ORDER BY id LIMIT 1").fetchone()[0]
        self.con.execute("BEGIN IMMEDIATE")
        self.con.execute("DELETE FROM source_states WHERE kind='evidence' AND record_id=?",
                         (evidence,))
        inside = conversion.source_qualification(self.con)
        self.assertIn("evidence:" + evidence, inside["unavailable"])
        self.con.execute("ROLLBACK")
        self.assertTrue(self.assertFresh(computes=None)["qualified"])

    def test_schema_change_recomputes_and_a_missing_guard_never_reuses(self):
        self.assertFresh(computes=1)
        with db.transaction(self.con, write=True):
            self.con.execute("CREATE INDEX pass9_probe ON evidence(id)")
        self.assertFresh(computes=1)
        self.con.execute('DROP TRIGGER "ew_view_state_insert_source_states"')
        self.assertIsNone(db.view_state_key(self.con))
        self.assertFresh(computes=1)
        self.assertFresh(computes=1)

    def test_a_changed_recording_file_recomputes(self):
        self.assertFresh(computes=1)
        stat = os.stat(self.recording)
        os.utime(self.recording, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000_000))
        self.assertFresh(computes=1)
        self.assertFresh(computes=0)

    def test_a_changed_outside_input_recomputes(self):
        self.assertFresh(computes=1)
        with mock.patch.object(media, "proof_inputs_unchanged", return_value=False):
            self.assertFresh(computes=1)

    def test_another_workspace_database_never_shares(self):
        self.assertFresh(computes=1)
        other, _recording, _artifact = file_workspace()
        self.addCleanup(other.close)
        self.assertFresh(other, computes=1)
        self.assertFresh(self.con, computes=0)

    def test_a_new_process_starts_empty(self):
        """The memo lives in module state only; a restarted process (here:
        a cleared memo) computes again."""
        self.assertFresh(computes=1)
        customer_query._VIEW_MEMO.clear()
        self.assertFresh(computes=1)


def _fresh_values(con, field, ctx_factory, **kwargs):
    customer_query._VIEW_MEMO.clear()
    ctx = ctx_factory()
    with db.transaction(con, write=False):
        out = customer_query.filter_values(con, field, ctx=ctx, **kwargs)
    customer_query._VIEW_MEMO.clear()
    return out, (None if ctx is None else (frozenset(ctx.disclosures), dict(ctx.recordings)))


class FilterValuesShareOneMatchTest(_MemoCase):
    FIELDS = ("brands", "shows", "categories")
    CONTEXTS = {"default": lambda: None, "owner": owner, "alice": alice, "bob": bob,
                "guest": guest}

    def setUp(self):
        super().setUp()
        from tests.test_backend_research import _con
        self.con = _con()
        self.addCleanup(self.con.close)

    def values(self, field, ctx=None, **kwargs):
        with db.transaction(self.con, write=False):
            return customer_query.filter_values(self.con, field, ctx=ctx, **kwargs)

    def test_three_fields_run_one_ads_and_one_episodes_match(self):
        with _Counter(customer, "_light_match") as matched:
            for field in self.FIELDS:
                self.values(field)
        self.assertEqual(matched.calls, 2, "one ads and one episodes match for all three fields")
        with _Counter(customer, "_light_match") as matched:
            for field in self.FIELDS:
                self.values(field)
        self.assertEqual(matched.calls, 0)

    def test_every_field_query_and_page_equals_a_fresh_computation(self):
        expected = {}
        for name, make in self.CONTEXTS.items():
            for field in self.FIELDS:
                for kwargs in ({}, {"q": "e"}, {"limit": 1}):
                    expected[(name, field, repr(kwargs))] = _fresh_values(
                        self.con, field, make, **kwargs)
        for name, make in self.CONTEXTS.items():
            for field in self.FIELDS:
                for kwargs in ({}, {"q": "e"}, {"limit": 1}):
                    for _ in range(2):
                        ctx = make()
                        out = self.values(field, ctx, **kwargs)
                        effects = None if ctx is None else (frozenset(ctx.disclosures),
                                                            dict(ctx.recordings))
                        self.assertEqual((out, effects), expected[(name, field, repr(kwargs))],
                                         (name, field, kwargs))

    def test_viewer_permissions_are_part_of_the_key(self):
        with _Counter(customer, "_light_match") as matched:
            self.values("brands", alice())
            self.values("brands", alice())
            self.assertEqual(matched.calls, 2)
            narrower = self.values("brands", bob())   # no transcript scope
            self.assertEqual(matched.calls, 4)
            self.values("shows", guest())             # share guest, field allowlist
            self.assertEqual(matched.calls, 6)
        self.assertEqual(narrower, _fresh_values(self.con, "brands", bob)[0])

    def test_values_read_only_guarded_tables(self):
        """Every table the values computation reads moves the view state on
        any row write, so a write always misses the memo."""
        con, _recording, _artifact = file_workspace()
        self.addCleanup(con.close)
        read = set()

        def authorizer(action, arg1, _arg2, _db_name, _trigger):
            if action == sqlite3.SQLITE_READ:
                read.add(arg1)
            return sqlite3.SQLITE_OK
        fresh = other_connection(con)
        fresh.set_authorizer(authorizer)
        try:
            for make in self.CONTEXTS.values():
                with db.transaction(fresh, write=False):
                    customer_query._filter_value_entries(fresh, make())
        finally:
            fresh.close()
        guarded = set(db._view_state_guarded_tables(db._schema_rows(con)))
        self.assertTrue({"episodes", "evidence", "source_states"} <= read, read)
        # json_each is a table-valued function; the transcript index's
        # version table is unguarded and keyed by its own digest (below).
        self.assertEqual(read - guarded - {"sqlite_master", "json_each"},
                         {"transcript_index_versions"} & read)

    def test_a_transcript_index_change_recomputes(self):
        from earwitness.app import transcript_index
        con, _recording, _artifact = file_workspace()
        self.addCleanup(con.close)
        self.assertTrue(transcript_index.available(con))
        version = con.execute("SELECT transcript_version_id FROM episodes"
                              " WHERE transcript_version_id IS NOT NULL").fetchone()[0]
        with db.transaction(con, write=True):
            transcript_index.index_versions(con, [version])
        state = db.view_state_key(con)
        with _Counter(customer, "_light_match") as matched:
            customer_query.filter_values(con, "shows")
            customer_query.filter_values(con, "shows")
        self.assertEqual(matched.calls, 2)
        con.execute("DELETE FROM transcript_index_versions WHERE version_id=?", (version,))
        self.assertEqual(db.view_state_key(con), state, "the index table is unguarded")
        with _Counter(customer, "_light_match") as matched:
            out = customer_query.filter_values(con, "shows")
        self.assertEqual(matched.calls, 2)
        self.assertEqual(out, _fresh_values(con, "shows", lambda: None)[0])

    def test_a_write_recomputes_and_never_serves_old_values(self):
        before = self.values("categories")
        self.assertEqual({i["value"] for i in before["items"]}, {"business", "tech"})
        with db.transaction(self.con, write=True):
            self.con.execute("UPDATE episodes SET category='sports' WHERE category='tech'")
        with _Counter(customer, "_light_match") as matched:
            after = self.values("categories")
        self.assertEqual(matched.calls, 2)
        self.assertEqual({i["value"] for i in after["items"]}, {"business", "sports"})
        self.assertEqual(after, _fresh_values(self.con, "categories", lambda: None)[0])
        self.assertNotEqual(after["revision"], before["revision"])


class ReportListBuildsNoDraftWithoutReportsTest(unittest.TestCase):
    """GET /campaigns/<id>/reports built the whole current draft (2.4-5.3 s
    on the 1,500 sample) to mark stale reports, even when the campaign has
    none. It builds the draft only when there is a report to compare."""

    def setUp(self):
        from tests.test_backend_allocation import _con
        self.con = _con()
        self.addCleanup(self.con.close)

    def test_no_reports_no_draft_and_reports_unchanged(self):
        from earwitness.app import campaigns
        from tests.test_backend_allocation import _brief, _placement, _supplied
        camp = _supplied(self.con, _brief(self.con))["campaign"]
        _placement(self.con, camp["id"], camp["revision"])
        with _Counter(campaigns, "build_customer_draft") as built:
            self.assertEqual(campaigns.list_customer_reports(self.con, camp["id"]), [])
        self.assertEqual(built.calls, 0)
        draft = campaigns.build_customer_draft(self.con, camp["id"])
        campaigns.freeze_customer_report(self.con, camp["id"], draft["campaign"]["revision"],
                                         draft["pins"])
        with _Counter(campaigns, "build_customer_draft") as built:
            listed = campaigns.list_customer_reports(self.con, camp["id"])
        self.assertGreater(built.calls, 0, "a listed report is still compared with the draft")
        self.assertEqual(len(listed), 1)
        self.assertFalse(listed[0]["stale"], listed[0])
        _placement(self.con, camp["id"], campaigns.build_customer_draft(
            self.con, camp["id"])["campaign"]["revision"])
        listed = campaigns.list_customer_reports(self.con, camp["id"])
        self.assertTrue(listed[0]["stale"], "a changed campaign still marks the report stale")

if __name__ == "__main__":
    unittest.main()
