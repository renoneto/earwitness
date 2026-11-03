"""Operation-count guards for the session/switch read path.

A workspace switch and a session read each project the frontend contract
marker, whose readiness pass classifies every saved query and tracker.
These tests count scans and readiness passes so the cost stays bounded
by distinct definitions, not definitions x view-revision rescans.
"""
import contextlib
import json
import unittest
from unittest import mock

from earwitness.app import citations, conversion, customer, customer_query, customer_saved, db
from tests import backend_fixtures


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


class ReadinessScanCountTest(unittest.TestCase):
    def setUp(self):
        self.con = backend_fixtures.make_memory_workspace()
        backend_fixtures.publish_sources(self.con)
        self.addCleanup(self.con.close)

    def _save_query(self, q):
        customer_saved.create_item(self.con, {
            "type": "query", "title": f"q {q}",
            "query": {"version": 2, "mode": "mentions", "q": q}},
            workspace_context=None)

    def _readiness_counts(self):
        with db.transaction(self.con, write=False):
            with _Counter(customer, "_run_match") as scans, \
                    _Counter(customer_query, "_view_revision") as revisions:
                state = conversion.readiness(self.con)
        self.assertIn("ready", state)
        return scans.calls, revisions.calls

    def _dry_run_counts(self):
        with db.transaction(self.con, write=False):
            with _Counter(customer, "_run_match") as scans, \
                    _Counter(customer_query, "_view_revision") as revisions:
                conversion.dry_run(self.con)
        return scans.calls, revisions.calls

    def test_dry_run_scans_each_distinct_query_once_and_readiness_skips_native_queries(self):
        # The fixture's legacy v1 saved query and tracker scan on their own path.
        fixture_scans, revisions = self._readiness_counts()
        self.assertEqual(revisions, 1)
        dry_fixture_scans, _ = self._dry_run_counts()
        for q in ("alpha", "alpha", "alpha", "alpha", "beta"):
            self._save_query(q)
        with db.transaction(self.con, write=False):
            report = conversion.dry_run(self.con)
        typed = [e for e in report["definitions"] if e["kind"] == "typed_query"]
        self.assertEqual(len(typed), 5)
        self.assertTrue(all(e["disposition"] == "qualified" for e in typed), typed)
        # dry_run classifies all five Query2 definitions over two distinct
        # queries: one scan each, and one view revision per pass. Unshared
        # this was 5 x (2 + 3) = 25.
        scans, revisions = self._dry_run_counts()
        self.assertEqual(revisions, 1)
        self.assertEqual(scans - dry_fixture_scans, 2)
        # Readiness never classifies Query2 saved queries (no conversion
        # applies), and saving them changes no input of the legacy fixture
        # definitions' classification: they are reused (READINESS-CACHE).
        scans, revisions = self._readiness_counts()
        self.assertEqual((scans, revisions), (0, 0))
        # A new process classifies only the legacy fixture definitions.
        customer_query._VIEW_MEMO.clear()
        conversion._CLASSIFIED.clear()
        scans, revisions = self._readiness_counts()
        self.assertEqual(revisions, 1)
        self.assertEqual(scans - fixture_scans, 0)

    def test_readiness_runs_each_distinct_owner_view_match_once(self):
        # The view revision's unfiltered scans repeat ones the Query2
        # definitions and legacy v1 path need: one pass scans each once.
        for q in ("", "alpha"):
            self._save_query(q)
        with db.transaction(self.con, write=False):
            expected = self._unshared_dry_run()
            scanned = []
            original = customer._episode_scope

            def counted(con, criteria, *args, **kwargs):
                scanned.append(json.dumps(criteria, sort_keys=True))
                return original(con, criteria, *args, **kwargs)
            # The unshared reference run memoized the view revision across
            # passes; forget it so this pass must compute (and share) it.
            customer_query._VIEW_MEMO.clear()
            with mock.patch.object(customer, "_episode_scope", counted):
                self.assertIn("ready", conversion.readiness(self.con))
            actual = conversion.dry_run(self.con)
        self.assertEqual(actual, expected)
        # The owner-view revision is a research data token digest (A3): it
        # adds no scan of its own; each definition's own match runs once.
        self.assertEqual(len(scanned), len(set(scanned)), scanned)

    def _unshared_dry_run(self):
        with mock.patch.object(conversion.query, "shared_scans",
                               lambda con: contextlib.nullcontext()):
            return conversion.dry_run(self.con)

    def test_view_revision_failure_classifies_every_query_definition(self):
        # A published transcript segment whose text is not a string breaks
        # the owner-view projection; every query/tracker definition must
        # still classify needs_review, exactly as per-definition revisions did.
        self.con.execute("BEGIN IMMEDIATE")
        version = backend_fixtures._add_version(self.con, "transcript:bad", {"segments": [
            {"start": 0.0, "end": 1.0, "text": "lumen"},
            {"start": 1.0, "end": 2.0, "text": 12345}]})
        self.con.execute(
            "INSERT INTO episodes (id, kind, show_title, ep_title, pub_date,"
            " transcript_version_id, revision, updated_at)"
            " VALUES ('bad','supplied','Bad','Bad','2026-03-12',?,1,?)",
            (version, db.utcnow()))
        citations.publish_source_state(self.con, kind="episode", record_id="bad", actor="fixture")
        self.con.commit()
        for q in ("alpha", "alpha", "beta"):
            self._save_query(q)
        with db.transaction(self.con, write=False):
            # The revision is a research data token digest (A3): the bad
            # segment no longer breaks it; shared and unshared passes still
            # classify every definition alike.
            customer_query._view_revision(self.con)
            expected = self._unshared_dry_run()
            actual = conversion.dry_run(self.con)
        self.assertEqual(actual, expected)
        queries = [e for e in actual["definitions"]
                   if e["kind"] in ("saved_query", "typed_query", "tracker")]
        self.assertGreaterEqual(len(queries), 5)

    def test_shared_scans_do_not_leak_outside_the_pass(self):
        self._save_query("lumen")
        with db.transaction(self.con, write=False):
            conversion.dry_run(self.con)
            with _Counter(customer, "_run_match") as scans:
                query = customer_query._contracts.normalize_query(
                    {"version": 2, "mode": "mentions", "q": "lumen"})
                customer_query._match_query2(self.con, query)
                customer_query._match_query2(self.con, query)
        self.assertEqual(scans.calls, 2)


class SwitchReadinessCountTest(unittest.TestCase):
    def setUp(self):
        from tests.test_backend_authority import SessionSwitchOrderingTest
        self.case = SessionSwitchOrderingTest()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_switch_and_session_read_each_run_one_readiness_pass(self):
        revision = self.case.session()["workspace_switch_revision"]
        with _Counter(conversion, "readiness") as passes:
            status, switched = self.case.switch("ordered-b", revision)
        self.assertEqual(status, 200, switched)
        self.assertIn("frontend_contract", switched)
        self.assertEqual(passes.calls, 1)
        with _Counter(conversion, "readiness") as passes:
            after = self.case.session()
        self.assertEqual(passes.calls, 1)
        self.assertEqual({key: switched[key] for key in after}, after)

    def test_readiness_failure_rolls_the_switch_back(self):
        before = self.case.session()

        def broken(con):
            raise ValueError("malformed conversion marker")
        with mock.patch.object(conversion, "readiness", broken):
            try:
                status, _ = self.case.switch("ordered-b", before["workspace_switch_revision"])
            except ValueError:
                status = None
        self.assertNotEqual(status, 200)
        after = self.case.session()
        self.assertEqual(after["active_workspace"], before["active_workspace"])
        self.assertEqual(after["workspace_switch_revision"], before["workspace_switch_revision"])


if __name__ == "__main__":
    unittest.main()
