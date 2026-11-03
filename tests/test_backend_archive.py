"""K09 research-export capture persistence, archive, and download gates."""
from __future__ import annotations

import hashlib
import json
import unittest

from earwitness.app import customer_contracts as _contracts
from earwitness.app import campaigns as _campaigns
from earwitness.app import customer_query as _query
from earwitness.app import db as _db
from earwitness.app import exports as _exports
from earwitness.app import ops as _ops
from tests import backend_fixtures
from tests.test_backend_research import BASE_Q, _seed_bulk, _seed_rich


def _con():
    con = backend_fixtures.make_memory_workspace()
    _seed_rich(con)
    return con


def _rev(con):
    return _query.research_bundle(
        con, dict(BASE_Q), None, [], False, "ctx-1")["revision"]


def _capture(con, fmt="json", scope="all_matches", query=None, title="T"):
    rev = _rev(con)
    return _query.capture_export(con, dict(query or BASE_Q), None, scope,
                                 fmt, rev, "ctx-1", title=title)






class CapturePersistenceTest(unittest.TestCase):
    def test_capture_persists_bytes_with_digest(self):
        con = _con()
        try:
            out = _capture(con)
            self.assertGreater(out["total"], 0)
            self.assertEqual(out["item_count"], out["total"])
            expect = hashlib.sha256(
                out["payload"].encode("utf-8")).hexdigest()
            self.assertEqual(out["digest"], expect)
            row = con.execute("SELECT * FROM exports WHERE id=?",
                              (out["export_id"],)).fetchone()
            self.assertEqual(row["digest"], expect)
            self.assertEqual(row["payload"], out["payload"])
            self.assertEqual(row["title"], "T")
            pinned = json.loads(row["captured_json"])
            self.assertEqual(pinned["view_revision"], out["view_revision"])
            self.assertEqual(pinned["scope"], "all_matches")
            self.assertEqual(pinned["query"]["q"], "Lumen")
        finally:
            con.close()

    def test_json_payload_round_trips_rows(self):
        con = _con()
        try:
            out = _capture(con)
            body = json.loads(out["payload"])
            self.assertEqual(body["item_count"], out["total"])
            self.assertEqual(len(body["items"]), out["total"])
            self.assertEqual({r["ref"] for r in body["items"]},
                             {r["ref"] for r in out["rows"]})
        finally:
            con.close()

    def test_csv_and_html_render_refs(self):
        con = _con()
        try:
            csv_out = _capture(con, fmt="csv")
            lines = csv_out["payload"].splitlines()
            # M7: the brand leads; the ref is still written, with the IDs.
            self.assertEqual(lines[0].split(",")[0], "brand")
            self.assertIn("ref", lines[0].split(","))
            self.assertEqual(len(lines) - 1, csv_out["total"])
            html_out = _capture(con, fmt="html")
            self.assertIn("<table>", html_out["payload"])
            self.assertIn("T", html_out["payload"])
        finally:
            con.close()

    def test_audio_never_bundled(self):
        rows = [{"ref": "r1", "text": "hello",
                 "audio": "https://cdn.example/a.mp3"}]
        for fmt in ("csv", "json", "html"):
            payload = _exports.render_capture(rows, fmt)
            self.assertNotIn("cdn.example", payload)

    def test_stale_basis_rejected_without_persisting(self):
        con = _con()
        try:
            before = con.execute("SELECT COUNT(*) FROM exports").fetchone()[0]
            with self.assertRaises(_contracts.SelectorConflict):
                _query.capture_export(con, dict(BASE_Q), None, "all_matches",
                                      "json", "vr2-stale", "ctx-1")
            after = con.execute("SELECT COUNT(*) FROM exports").fetchone()[0]
            self.assertEqual(before, after)
        finally:
            con.close()

    def test_selected_scope_pins_ids(self):
        con = _con()
        try:
            _seed_bulk(con, 12)
            rev = _rev(con)
            full = _query.capture_export(con, dict(BASE_Q), None,
                                         "all_matches", "json", rev, "ctx-1")
            refs = [r["ref"] for r in full["rows"][:3]]
            rev2 = _rev(con)
            part = _query.capture_export(
                con, dict(BASE_Q, ids=refs), None, "selected", "json",
                rev2, "ctx-1")
            self.assertEqual(part["total"], len(refs))
            pinned = json.loads(con.execute(
                "SELECT captured_json FROM exports WHERE id=?",
                (part["export_id"],)).fetchone()[0])
            self.assertEqual(pinned["scope"], "selected")
        finally:
            con.close()


class DownloadGateTest(unittest.TestCase):
    def test_load_verifies_digest(self):
        con = _con()
        try:
            out = _capture(con)
            loaded = _exports.load_export_bytes(con, out["export_id"])
            self.assertEqual(loaded["payload"], out["payload"])
            self.assertEqual(loaded["digest"], out["digest"])
            self.assertTrue(
                loaded["content_type"].startswith("application/json"))
        finally:
            con.close()


    def test_tampered_bytes_refused(self):
        con = _con()
        try:
            out = _capture(con)
            con.execute("UPDATE exports SET payload=payload || 'x' WHERE id=?",
                        (out["export_id"],))
            with self.assertRaises(_exports.DigestMismatch):
                _exports.load_export_bytes(con, out["export_id"])
        finally:
            con.close()

    def test_unknown_export_reads_unknown(self):
        con = _con()
        try:
            with self.assertRaises(_ops.BadInput):
                _exports.load_export_bytes(con, 9999)
        finally:
            con.close()


class ArchiveTest(unittest.TestCase):
    def test_archive_collection_shape_filters_and_cursor(self):
        con = _con()
        try:
            first = _capture(con, title="first")
            second = _capture(con, fmt="csv", title="second")
            campaign = _campaigns.save_customer_campaign(con, {"campaign": {
                "name": "Stored research", "mode": "research", "brand": "Lumen"}})["campaign"]
            _campaigns.create_target(con, campaign["id"], "episode", episode_id="e1",
                                     expected_campaign_revision=campaign["revision"])
            draft = _campaigns.build_customer_draft(con, campaign["id"])
            _campaigns.freeze_customer_report(con, campaign["id"],
                draft["campaign"]["revision"], draft["pins"])
            page = _exports.archive_collection(con)
            self.assertIn("archive_revision", page)
            self.assertEqual(page["total_relation"], "exact")
            self.assertGreaterEqual(page["total"], 2)
            kinds = {e["kind"] for e in page["items"]}
            self.assertEqual(kinds, {"export", "report"})
            report = next(e for e in page["items"]
                          if e["kind"] == "report")
            for key in ("campaign_id", "report_no", "digest",
                        "download_path"):
                self.assertIn(key, report)
            self.assertTrue(report["download_path"].startswith(
                "/customer/campaigns/"))
            by_id = {e["id"]: e for e in page["items"] if e["kind"] == "export"}
            self.assertEqual(by_id[first["export_id"]]["title"], "first")
            self.assertEqual(by_id[second["export_id"]]["format"], "csv")
            self.assertEqual(by_id[second["export_id"]]["digest"],
                             second["digest"])
            for entry in page["items"]:
                self.assertNotIn("payload", entry)
                self.assertNotIn("captured_json", entry)
                for key in ("id", "kind", "title", "captured_at", "pins",
                            "count", "count_units", "format", "digest",
                            "download_path", "stale", "stale_reasons",
                            "limitations"):
                    self.assertIn(key, entry)
                if entry["kind"] == "export":
                    self.assertEqual(
                        entry["download_path"],
                        f"/customer/exports/{entry['id']}")
            # Literal title filter narrows; kind filter matches.
            narrowed = _exports.archive_collection(con, q="second")
            self.assertEqual(narrowed["total"], 1)
            self.assertEqual(narrowed["items"][0]["title"], "second")
            reports = _exports.archive_collection(con, kind="report")
            self.assertTrue(reports["total"] >= 1)
            self.assertEqual(
                {e["kind"] for e in reports["items"]}, {"report"})
            by_campaign = _exports.archive_collection(
                con, kind="report", campaign_id=1)
            self.assertTrue(all(
                e["campaign_id"] == 1 for e in by_campaign["items"]))
            # Changed-cohort cursor reads as refresh denial, never mixing.
            third = _capture(con, title="third")
            two = _exports.archive_collection(con, limit=1)
            self.assertIsNotNone(two["next_cursor"])
            second_page = _exports.archive_collection(
                con, limit=1, cursor=two["next_cursor"])
            self.assertEqual(second_page["total"], two["total"])
            with self.assertRaises(_ops.BadInput):
                _exports.archive_collection(
                    con, q="first", cursor=two["next_cursor"])
            with self.assertRaises(_ops.BadInput):
                _exports.archive_collection(con, kind="bogus")
        finally:
            con.close()




if __name__ == "__main__":
    unittest.main()
