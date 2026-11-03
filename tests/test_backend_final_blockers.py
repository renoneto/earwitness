"""Actual required index rollback, receipt-derived status and displayed-basis parity."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from earwitness.app import access, campaigns, citations, coverage, customer, customer_contracts, customer_query, db, indexing, ops
from tests import backend_fixtures

# The feed producer needs feedparser, which only the worker/ingest CLIs use
# (local runtime, requirements.txt). The hosted service never parses feeds,
# so .venv-hosted skips the feed subtests; .venv runs them.
HAS_FEEDPARSER = importlib.util.find_spec("feedparser") is not None
FEEDPARSER_REASON = ("feedparser not installed: feed ingest is a worker-CLI path on"
                     " the local runtime (requirements.txt), not the hosted service")


def _snapshot(con):
    tables = [row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {table: sorted((tuple(row) for row in con.execute(f'SELECT * FROM "{table}"')),
                          key=repr) for table in tables}


class RequiredIndexRollback(unittest.TestCase):
    def test_required_document_and_attestation_abort_both_ingests(self):
        from scripts import app_worker
        rss = b'<rss version="2.0"><channel><title>Fixture</title><item><guid>owned-new-entry</guid><title>New episode</title></item></channel></rss>'
        transcript = json.dumps({"segments": [{"start": 0, "end": 40, "text": "new matching source"}]}).encode()
        for producer in ("transcript", "feed"):
            for failure in ("document", "attestation"):
                with self.subTest(producer=producer, failure=failure), tempfile.TemporaryDirectory() as temporary:
                    if producer == "feed" and not HAS_FEEDPARSER:
                        self.skipTest(FEEDPARSER_REASON)
                    workspace = Path(temporary) / "workspace"
                    filename = backend_fixtures.make_workspace(workspace, schema_version=8)
                    con = db.open_existing(filename, readonly=False)
                    try:
                        indexing.rebuild_all(con)
                        before = _snapshot(con)
                        target = "transcript_segment_docs" if failure == "document" else "coverage_attestations"
                        condition = "" if failure == "document" else " WHEN NEW.stage='index'"
                        con.execute(f"CREATE TRIGGER actual_required_abort BEFORE INSERT ON {target}{condition} BEGIN SELECT RAISE(ABORT,'required index abort'); END")
                    finally:
                        con.close()
                    with self.assertRaises(indexing.IndexUnavailable):
                        if producer == "transcript":
                            app_worker._ingest_transcript_file(workspace, "fixture", transcript, "fixture.json", "e1", coverage, indexing)
                        else:
                            app_worker._ingest_feed_file(workspace, rss, "fixture.rss", 10)
                    con = db.open_existing(filename, readonly=True)
                    try:
                        after = _snapshot(con)
                        self.assertEqual(after, before)
                        self.assertEqual(indexing.check_fresh(con), db.aggregate_revision(con))
                    finally:
                        con.close()

    def test_unreadable_other_pinned_transcript_aborts_entire_ingest(self):
        from scripts import app_worker
        rss = b'<rss version="2.0"><channel><title>Fixture</title><item><guid>another-source</guid><title>New episode</title></item></channel></rss>'
        transcript = json.dumps({"segments": [{"start": 0, "end": 40, "text": "another source"}]}).encode()
        for producer in ("transcript", "feed"):
            for payload in ("{", "[]", "{}", '{"segments":{}}', None):
                with self.subTest(producer=producer, payload=payload), tempfile.TemporaryDirectory() as temporary:
                    if producer == "feed" and not HAS_FEEDPARSER:
                        self.skipTest(FEEDPARSER_REASON)
                    workspace = Path(temporary) / "workspace"
                    filename = backend_fixtures.make_workspace(workspace, schema_version=8)
                    con = db.open_existing(filename, readonly=False)
                    try:
                        indexing.rebuild_all(con)
                        version = con.execute("SELECT transcript_version_id FROM episodes WHERE id='e2'").fetchone()[0]
                        self.assertIsNotNone(version)
                        if payload is None:
                            con.execute("PRAGMA foreign_keys=OFF")
                            con.execute("DELETE FROM source_versions WHERE id=?", (version,))
                            con.execute("PRAGMA foreign_keys=ON")
                        else:
                            con.execute("UPDATE source_versions SET payload=? WHERE id=?", (payload, version))
                        before = _snapshot(con)
                    finally:
                        con.close()
                    with self.assertRaises(indexing.IndexUnavailable):
                        if producer == "transcript":
                            app_worker._ingest_transcript_file(workspace, "fixture", transcript,
                                                               "another.json", "e1", coverage, indexing)
                        else:
                            app_worker._ingest_feed_file(workspace, rss, "another.rss", 10)
                    con = db.open_existing(filename, readonly=True)
                    try:
                        after = _snapshot(con)
                        self.assertEqual(after, before)
                    finally:
                        con.close()

    def test_null_pin_and_genuine_empty_transcript_remain_valid(self):
        con = backend_fixtures.make_memory_workspace()
        self.addCleanup(con.close)
        with db.transaction(con, write=True):
            con.execute("UPDATE episodes SET transcript_version_id=NULL WHERE id='e2'")
            empty = backend_fixtures._add_version(con, "transcript:honest-empty", {"segments": []})
            con.execute("INSERT INTO episodes(id,kind,transcript_version_id,updated_at)"
                        " VALUES('honest-empty','feed',?,?)", (empty, db.utcnow()))
            citations.publish_episode_sources(con, "honest-empty", "fixture")
        indexing.rebuild_all(con)
        self.assertEqual(indexing.check_fresh(con), db.aggregate_revision(con))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM transcript_segment_docs"
                                     " WHERE episode_id IN ('e2','honest-empty')").fetchone()[0], 0)


class ReceiptDerivedConnectionStatus(unittest.TestCase):
    def test_status_read_does_not_verify_use_and_current_receipt_qualifies(self):
        from tests.test_backend_authority import HostedReaderHttpTest
        from earwitness.app import connections
        http = HostedReaderHttpTest()
        http.setUp()
        self.addCleanup(http.tearDown)
        headers = {"authorization": "Bearer " + http.reader_token}
        status, body = http._call('/api/v1/connections', method='GET', headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(body['connections'][0]['status'], connections.AWAITING_AUTHORIZATION)
        control = http.app.store._connect()
        self.assertEqual(control.execute("SELECT COUNT(*) FROM agent_audit WHERE outcome='success'").fetchone()[0], 0)
        control.close()
        status, result = http._call('/api/v1/evidence/search', body={"filter": {"query": "Lumen"}}, headers=headers)
        self.assertEqual(status, 200)
        self.assertGreater(result['total_matches'], 0)
        status, body = http._call('/api/v1/connections', method='GET', headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(body['connections'][0]['status'], connections.CONNECTED)
        control = http.app.store._connect()
        self.assertEqual(control.execute("SELECT COUNT(*) FROM agent_audit WHERE outcome='success'").fetchone()[0], 1)
        control.close()


class CoherentDisplayedBasis(unittest.TestCase):
    def test_real_new_transcript_publication_conflicts_old_basis_and_admits_fresh(self):
        con = backend_fixtures.make_memory_workspace()
        self.addCleanup(con.close)
        ctx = access.AccessContext.create(principal_id='owner', principal_kind='owner', workspace_id='local', credential_kind='local_operator')
        query = {"version": 2, "mode": "mentions", "q": "Lumen"}
        before = customer_query.research_bundle(con, query, None, [], False, 'display', ctx=ctx)
        tv = backend_fixtures._add_version(con, 'transcript:actual-new', {"segments": [{"start": 0, "end": 10, "text": "Lumen new publication"}]})
        with db.transaction(con, write=True):
            con.execute("INSERT INTO episodes(id,kind,ep_title,transcript_version_id,updated_at) VALUES('actual-new','feed','New',?,?)", (tv, db.utcnow()))
            customer.rebuild_customer_show_map(con)
            citations.publish_episode_sources(con, 'actual-new', 'fixture')
        with self.assertRaises(customer_contracts.SelectorConflict):
            customer_query.research_bundle(con, query, None, [], False, 'display', ctx=ctx, expected_view_revision=before['revision'])
        fresh = customer_query.research_bundle(con, query, None, [], False, 'display', ctx=ctx)
        self.assertEqual(fresh['result']['total'], before['result']['total'] + 1)
        self.assertNotEqual(fresh['revision'], before['revision'])

    def test_missing_episode_source_is_not_malformed_field_or_source_reference(self):
        from tests.test_backend_allocation import _brief, _supplied
        con = backend_fixtures.make_memory_workspace()
        self.addCleanup(con.close)
        camp = _supplied(con, _brief(con))['campaign']
        with self.assertRaises(citations.RevisionUnavailable):
            campaigns.save_customer_placement(con, camp['id'], {'placement': {'scope': 'episode', 'episode_id': 'unknown-source'}, 'expected_campaign_revision': camp['revision']})
        with self.assertRaises(ops.BadInput):
            campaigns._qualify_attachment_ref(con, 'not-a-reference')
        with self.assertRaises(citations.RevisionUnavailable):
            campaigns._qualify_attachment_ref(con, 'sr1:' + '0' * 32)
