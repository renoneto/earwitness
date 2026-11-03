"""K10 retained-definition conversion, explicit disposition and cutover gates."""
from __future__ import annotations

import json
import unittest

from earwitness.app import conversion as _conversion
from earwitness.app import customer_saved as _saved
from earwitness.app import db as _db
from earwitness.app import ops as _ops
from tests import backend_fixtures


def _con():
    return backend_fixtures.make_memory_workspace()


def _add_review_row(con):
    con.execute(
        "INSERT INTO customer_saved_searches (workspace_id, name,"
        " query_json, revision, created_at, updated_at)"
        " VALUES ('local', 'future query', ?, 1, ?, ?)",
        (json.dumps({"version": 2, "mode": "mentions", "q": "lumen", "perspective": "tracked"}),
         _db.utcnow(), _db.utcnow()))
    con.commit()
    return con.execute("SELECT MAX(id) FROM customer_saved_searches").fetchone()[0]


def _tables(con):
    return {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


class DryRunTest(unittest.TestCase):
    def test_dry_run_read_only_and_classifies(self):
        con = _con()
        try:
            review_id = _add_review_row(con)
            before_tables = _tables(con)
            before_rev = _db.aggregate_revision(con)
            report = _conversion.dry_run(con)
            self.assertEqual(_tables(con), before_tables)
            self.assertEqual(_db.aggregate_revision(con), before_rev)
            by_id = {e["id"]: e for e in report["searches"]}
            self.assertEqual(by_id[1]["disposition"], "convertible")
            self.assertEqual(by_id[1]["canonical"]["query"]["version"], 2)
            self.assertEqual(by_id[1]["canonical"]["query"]["q"], "lumen")
            self.assertEqual(json.loads(by_id[1]["original"])["mode"], "mentions")
            self.assertEqual(by_id[review_id]["disposition"], "needs_review")
            self.assertIsNone(by_id[review_id]["canonical"])
            self.assertEqual(report["summary"]["total"], 2)
            self.assertEqual(report["summary"]["convertible"], 1)
            self.assertEqual(report["summary"]["needs_review"], 1)
        finally:
            con.close()

    def test_dry_run_empty_workspace(self):
        con = _con()
        try:
            con.execute("DELETE FROM customer_saved_searches")
            con.commit()
            report = _conversion.dry_run(con)
            self.assertEqual(report["summary"]["total"], 0)
        finally:
            con.close()


class ApplyTest(unittest.TestCase):
    def test_apply_converts_and_pins_lineage(self):
        con = _con()
        try:
            out = _conversion.apply(con)
            self.assertEqual(len(out["migrated"]), 1)
            self.assertEqual(out["summary"]["needs_review"], 0)
            self.assertEqual(out["marker"]["state"], "complete")
            item_id = out["migrated"][0]["item_id"]
            item = _saved.get_item(con, item_id,
                                     workspace_context="ctx-1")["item"]
            self.assertEqual(item["type"], "query")
            self.assertEqual(item["query"]["version"], 2)
            self.assertEqual(item["query"]["q"], "lumen")
            self.assertEqual(item["title"], "legacy query")
            lineage = con.execute(
                "SELECT * FROM customer_conversion_lineage WHERE search_id=1"
                ).fetchone()
            self.assertEqual(lineage["action"], "converted")
            self.assertEqual(lineage["item_id"], item_id)
            original = json.loads(lineage["original_json"])
            self.assertEqual(original, {"mode": "mentions", "q": "lumen"})
            self.assertTrue(json.loads(lineage["notes_json"]))
        finally:
            con.close()

    def test_apply_quarantines_review_definition_without_executable_item(self):
        con = _con()
        try:
            review_id = _add_review_row(con)
            out = _conversion.apply(con)
            self.assertEqual(len(out["migrated"]), 1)
            self.assertEqual(out["marker"]["state"], "complete")
            self.assertEqual(out["marker"]["pending"], 0)
            lineage = con.execute(
                "SELECT action FROM customer_conversion_lineage WHERE search_id=?",
                (review_id,)).fetchone()
            self.assertEqual(lineage["action"], "quarantined")
            items = _saved.list_items(con, item_type="query",
                                      workspace_context="ctx-1")["items"]
            self.assertEqual(len(items), 1)
        finally:
            con.close()

    def test_apply_idempotent(self):
        con = _con()
        try:
            first = _conversion.apply(con)
            second = _conversion.apply(con)
            self.assertEqual(second["migrated"], [])
            self.assertEqual(second["marker"]["state"], "complete")
            items = _saved.list_items(con, item_type="query",
                                      workspace_context="ctx-1")["items"]
            self.assertEqual(len(items), len(first["migrated"]))
        finally:
            con.close()


class DispositionTest(unittest.TestCase):
    def test_drop_preserves_original_without_item(self):
        con = _con()
        try:
            review_id = _add_review_row(con)
            _conversion.apply(con)
            out = _conversion.mark_dropped(con, review_id, reason="superseded")
            self.assertEqual(out["marker"]["state"], "complete")
            lineage = con.execute(
                "SELECT * FROM customer_conversion_lineage WHERE search_id=?",
                (review_id,)).fetchone()
            self.assertEqual(lineage["action"], "dropped")
            self.assertIsNone(lineage["item_id"])
            self.assertIn("version", json.loads(lineage["original_json"]))
            self.assertEqual(len(_saved.list_items(
                con, item_type="query",
                workspace_context="ctx-1")["items"]), 1)
        finally:
            con.close()

    def test_drop_unknown_or_repeated_refused(self):
        con = _con()
        try:
            with self.assertRaises(_ops.BadInput):
                _conversion.mark_dropped(con, 4242)
            _conversion.apply(con)
            with self.assertRaises(_ops.BadInput):
                _conversion.mark_dropped(con, 1)
        finally:
            con.close()


class RetireTest(unittest.TestCase):
    def test_retire_refused_while_pending(self):
        con = _con()
        try:
            _add_review_row(con)
            with self.assertRaises(_ops.MissingPrecondition):
                _conversion.retire_store(con)
            self.assertIn("customer_saved_searches", _tables(con))
        finally:
            con.close()

    def test_retire_drops_store_on_complete_marker(self):
        con = _con()
        try:
            _conversion.apply(con)
            out = _conversion.retire_store(con)
            self.assertTrue(out["retired"])
            self.assertNotIn("customer_saved_searches", _tables(con))
            status = _conversion.status(con)
            self.assertTrue(status["store_retired"])
            self.assertEqual(status["marker"]["state"], "complete")
            self.assertEqual(status["summary"]["total"], 0)
            # Lineage survives retirement: provenance is not discarded.
            original = con.execute(
                "SELECT original_json FROM customer_conversion_lineage WHERE search_id=1").fetchone()[0]
            self.assertEqual(json.loads(original), {"mode": "mentions", "q": "lumen"})
        finally:
            con.close()


class CutoverCheckTest(unittest.TestCase):
    def test_cutover_check_requires_conversion_and_real_runtime_qualification(self):
        from scripts import app_workspace as _ws
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            con = _db.connect(workspace)
            try:
                _db.init_schema(con)
                con.execute(
                    "INSERT INTO customer_saved_searches (workspace_id, name,"
                    " query_json, revision, created_at, updated_at)"
                    " VALUES ('local', 'legacy query',"
                    " '{\"mode\":\"mentions\",\"q\":\"lumen\"}', 1, ?, ?)",
                    (_db.utcnow(), _db.utcnow()))
                con.commit()
            finally:
                con.close()
            blocked = _ws.customer_cutover_check(workspace)
            self.assertFalse(blocked["ok"])
            self.assertTrue(blocked["problems"])
            con = _db.connect(workspace)
            try:
                with _db.transaction(con, write=True):
                    _conversion.apply(con)
                    _conversion.retire_store(con)
            finally:
                con.close()
            result = _ws.customer_cutover_check(workspace)
            self.assertFalse(result["ok"])
            self.assertFalse(result["readiness"]["ready"])
            self.assertEqual(result["conversion"]["pending_definitions"], [])
            self.assertEqual(result["problems"], ["source-bound runtime/semantic readiness is incomplete"])


if __name__ == "__main__":
    unittest.main()
