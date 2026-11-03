"""Design v3 bugfix regressions (B-1..B-15 from the design v3 screenshot pack).

Each test reproduces one behaviour the capture found, on disposable state
only. Run: PYTHONPATH=. python -m unittest tests.test_dv3_bugfix
"""
from __future__ import annotations

import io
import json
import unittest

from earwitness.app import db as _db
from tests import backend_fixtures


def _local_call(fixture, method, path, body=None, headers=None):
    """The real local Handler over in-memory streams, DELETE included."""
    from tests.test_product_processing_api import srv, HOST, CSRF
    handler = srv.Handler.__new__(srv.Handler)
    handler.command = method
    handler.path = path
    handler.request_version = "HTTP/1.1"
    handler.requestline = f"{method} {path} HTTP/1.1"
    handler._headers_buffer = []
    handler.log_message = lambda *a: None
    payload = json.dumps(body).encode() if body is not None else b""
    hdrs = {"Host": HOST, "Content-Length": str(len(payload))}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    if method != "GET":
        hdrs["X-CSRF-Token"] = CSRF
        hdrs["Origin"] = f"http://{HOST}"
    hdrs.update(headers or {})
    handler.headers = hdrs
    handler.rfile = io.BytesIO(payload)
    handler.wfile = io.BytesIO()
    handler.close_connection = True
    getattr(handler, "do_" + method)()
    head, _, rest = handler.wfile.getvalue().partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), json.loads(rest or b"{}")


class LocalFixture(unittest.TestCase):
    def setUp(self):
        from tests.test_product_processing_api import HandlerCase
        self.fixture = HandlerCase()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        con = _db.open_existing(self.fixture.ws / "workspace.sqlite3", readonly=False)
        try:
            backend_fixtures.seed_source_rows(con)
            backend_fixtures.publish_sources(con)
        finally:
            con.close()

    def call(self, method, path, body=None, headers=None):
        return _local_call(self.fixture, method, path, body, headers)


class B1LocalDeleteReadsBodyRevision(LocalFixture):
    """B-1: the client sends DELETE {expected_revision}; the hosted server
    reads it, so the local server must too (and If-Match keeps working)."""

    def _track(self):
        status, out = self.call("POST", "/api/v1/customer/saved-items",
                                {"type": "bookmark", "target": {"kind": "brand", "id": "lumen"}})
        self.assertEqual(status, 200, out)
        return out["item"]

    def test_delete_with_body_revision_removes_the_item(self):
        item = self._track()
        status, out = self.call("DELETE", "/api/v1/customer/saved-items/" + item["id"],
                                {"expected_revision": item["revision"]})
        self.assertEqual(status, 200, out)
        status, listed = self.call("GET", "/api/v1/customer/saved-items?type=bookmark")
        self.assertEqual(listed["items"], [])

    def test_stale_body_revision_conflicts_and_if_match_still_works(self):
        item = self._track()
        status, out = self.call("DELETE", "/api/v1/customer/saved-items/" + item["id"],
                                {"expected_revision": item["revision"] + 5})
        self.assertEqual(status, 412, out)
        status, out = self.call("DELETE", "/api/v1/customer/saved-items/" + item["id"],
                                headers={"If-Match": str(item["revision"])})
        self.assertEqual(status, 200, out)

    def test_delete_without_any_revision_is_still_refused(self):
        item = self._track()
        status, out = self.call("DELETE", "/api/v1/customer/saved-items/" + item["id"])
        self.assertEqual(status, 428, out)


class B3ShowWindowNeedsAShow(unittest.TestCase):
    """B-3: a supplied show-window placement with no (or an unknown) show
    was saved. A window means nothing without its show: refuse it."""

    def setUp(self):
        from tests.test_backend_campaigns import _brief, _con, _supplied
        self.con = _con()
        self.addCleanup(self.con.close)
        self.brief = _brief(self.con)
        self.camp = _supplied(self.con, self.brief)["campaign"]

    def _body(self, show_id):
        return {"placement": {
            "scope": "show_window", "show_id": show_id, "episode_id": None,
            "date_from": None, "date_to": None, "timezone": "UTC",
            "expected_count": 1, "binding": "eligible",
            "requirements": {"message": "Say lumen"},
            "provenance": {"brief_ref": {"kind": "supplied_brief",
                                         "brief_id": self.brief["brief_id"], "version": 1}}},
            "expected_campaign_revision": self._rev()}

    def _rev(self):
        return self.con.execute("SELECT revision FROM campaigns WHERE id=?",
                                (self.camp["id"],)).fetchone()[0]

    def _show(self):
        from earwitness.app import customer
        return customer.customer_show_for_episode(self.con, "e1")["show_identity_id"]

    def test_missing_or_unknown_show_is_refused(self):
        from earwitness.app import campaigns, ops
        for show_id in ("", None, "cs-nope"):
            with self.assertRaises(ops.BadInput, msg=repr(show_id)):
                campaigns.save_customer_placement(self.con, self.camp["id"], self._body(show_id))
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM expected_placements WHERE campaign_id=?",
            (self.camp["id"],)).fetchone()[0], 0, "nothing was saved")

    def test_a_proven_show_saves_and_an_edit_cannot_clear_it(self):
        from earwitness.app import campaigns, ops
        made = campaigns.save_customer_placement(self.con, self.camp["id"], self._body(self._show()))
        placed = made["placement"]
        self.assertEqual(placed["show_id"], self._show())
        with self.assertRaises(ops.BadInput):
            campaigns.save_customer_placement(
                self.con, self.camp["id"],
                {"placement": {"show_id": ""}, "expected_campaign_revision": self._rev()},
                placed["id"], placed["revision"])

    def test_an_edit_that_keeps_the_stored_show_is_not_rechecked(self):
        """Review F2: the dialog always resends scope and show; an edit of the
        count alone must not fail on a stored show the check would refuse."""
        from earwitness.app import campaigns
        placed = campaigns.save_customer_placement(self.con, self.camp["id"], self._body(self._show()))["placement"]
        self.con.execute("UPDATE expected_placements SET show_id='sh-legacy' WHERE id=?", (placed["id"],))
        out = campaigns.save_customer_placement(
            self.con, self.camp["id"],
            {"placement": {"scope": "show_window", "show_id": "sh-legacy", "expected_count": 2},
             "expected_campaign_revision": self._rev()},
            placed["id"], placed["revision"])
        self.assertEqual((out["placement"]["show_id"], out["placement"]["expected_count"]), ("sh-legacy", 2))


class B5AlertKeepsItsOriginatingSearch(unittest.TestCase):
    """B-5: an alert made from a search stores origin_route, but the alert
    reply never carried it, so "Return to originating search" never showed."""

    def setUp(self):
        from tests.test_backend_trackers import add_evidence, fresh_db, seed_corpus
        self.con = fresh_db()
        self.addCleanup(self.con.close)
        seed_corpus(self.con)
        add_evidence(self.con, "ev-a", "s1", "acme launch")

    def test_dto_returns_the_stored_origin_route(self):
        from earwitness.app import customer
        from tests.test_backend_trackers import query_acme
        route = "#/search?mode=mentions&q=acme"
        saved = customer.save_alert(self.con, "watch acme", query_acme(), origin_route=route)
        self.assertEqual(customer.tracker_dto(self.con, saved["id"])["origin_route"], route)
        plain = customer.save_alert(self.con, "no origin", query_acme())
        self.assertEqual(customer.tracker_dto(self.con, plain["id"])["origin_route"], "")


class B15EvidenceSearchParsesEachTranscriptOnce(unittest.TestCase):
    """B-15: the operator evidence text search re-read and re-parsed the
    whole transcript JSON once per evidence row (4,503 parses of 288
    transcripts on ws-golden), plus one brand query per row."""

    def setUp(self):
        from tests.test_backend_trackers import add_evidence, fresh_db, seed_corpus
        self.con = fresh_db()
        self.addCleanup(self.con.close)
        seed_corpus(self.con)
        for n in range(6):
            add_evidence(self.con, f"ev-{n}", "s1", "acme launch" if n % 2 else "other words")

    def test_one_payload_read_per_transcript_version_and_same_results(self):
        from earwitness.app import ops
        versions = self.con.execute(
            "SELECT COUNT(DISTINCT transcript_version_id) FROM evidence").fetchone()[0]
        rows = self.con.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
        self.assertGreater(rows, versions, "the fixture shares transcripts across rows")
        seen = []
        self.con.set_trace_callback(seen.append)
        try:
            out = ops.search_evidence(self.con, {"q": "acme", "per_page": 200})
        finally:
            self.con.set_trace_callback(None)
        payload_reads = [q for q in seen if "SELECT payload FROM source_versions" in q]
        brand_reads = [q for q in seen if "FROM brand_assocs WHERE evidence_id=" in q]
        self.assertLessEqual(len(payload_reads), versions, payload_reads)
        self.assertLessEqual(len(brand_reads), out["total"],
                             "brand text is read in one pass, not per scanned row")
        # The fixture transcript says "acme bright widget": every row matches
        # on transcript text; excerpt-only words still narrow the result.
        self.assertEqual(out["total"], 6)
        narrow = ops.search_evidence(self.con, {"q": "launch", "per_page": 200})
        self.assertEqual(sorted(i["id"] for i in narrow["items"]), ["ev-1", "ev-3", "ev-5"])


class B6EpisodeBookmarkFromEvidenceIsCurrent(unittest.TestCase):
    """B-6 follow-up: an episode bookmark pinned on an evidence publication
    (the Evidence page's button) read "Changed since saved" at once: the
    pin was compared with the episode's publication id, which an evidence
    publication never equals. It pins a transcript version."""

    def setUp(self):
        from tests.test_backend_saved import _con
        self.con = _con()
        self.addCleanup(self.con.close)

    def _tv(self, ref):
        from earwitness.app import citations
        state, _ = citations._parse_source_ref(ref)
        row = self.con.execute("SELECT pins_json FROM source_states WHERE id=?", (state,)).fetchone()
        return json.loads(row["pins_json"])["transcript_version_id"]

    def test_evidence_pinned_episode_bookmark_is_current_until_the_transcript_moves(self):
        from earwitness.app import citations, customer_saved
        ref = citations.current_source_ref(self.con, "evidence", "fixture-ad")
        tv = self._tv(ref)
        out = customer_saved.create_item(self.con, {
            "type": "bookmark", "title": "From evidence",
            "target": {"kind": "episode", "id": "e1", "transcript_version_id": tv, "source_ref": ref}},
            workspace_context="ctx-1")["item"]
        self.assertEqual((out["availability"], out.get("availability_reason")), ("current", None))
        key = self.con.execute("SELECT source_key FROM source_versions WHERE id=?", (tv,)).fetchone()[0]
        newer = backend_fixtures._add_version(self.con, key, {"segments": [{"start": 0.0, "end": 5.0, "text": "re-transcribed"}]})
        self.con.execute("UPDATE episodes SET transcript_version_id=? WHERE id='e1'", (newer,))
        again = customer_saved.get_item(self.con, out["id"])["item"]
        self.assertEqual(again["availability"], "source_changed", again.get("availability_reason"))


if __name__ == "__main__":
    unittest.main()
