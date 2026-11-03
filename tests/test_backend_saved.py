"""K05 durable typed saves + owned personal notes (red-capable).

B2: excerpt/query/bookmark as separate typed resources with server
capture, live-definition query selectors, collection cursors/counts and
revision-checked edits. B4: /mistakes collection/direct reads plus
retained exact-ref personal-note writes that survive source loss.
"""
from __future__ import annotations

import json
import unittest

from earwitness.app import access as _access
from earwitness.app import citations as _citations
from earwitness.app import customer as _customer
from earwitness.app import customer_query as _customer_query
from earwitness.app import customer_saved as _saved
from earwitness.app import db as _db
from earwitness.app import ops as _ops
from earwitness.app import search as _search
from earwitness.app import services as _services
from tests import backend_fixtures


def _con():
    con = backend_fixtures.make_memory_workspace()
    backend_fixtures.publish_sources(con)
    con.execute(
        "INSERT OR IGNORE INTO creatives (id, workspace_id, revision,"
        " status, created_at, updated_at) VALUES ('cr-1', 'local', 1,"
        " 'suggested', ?, ?)", (_db.utcnow(), _db.utcnow()))
    con.commit()
    return con


def _entry():
    return {"row_namespace": "local", "is_owner": False,
            "workspace_id": "ws1"}


BASE_Q = {"version": 2, "mode": "mentions", "q": "lumen", "brands": None,
          "shows": None, "categories": None, "statuses": None, "audio": None,
          "date_status": "all", "from": None, "to": None, "sort": "newest",
          "page": 1, "ids": None}


class ExcerptTest(unittest.TestCase):
    def test_excerpt_captures_pin_and_survives_live_edit(self):
        con = _con()
        try:
            ref = _citations.current_source_ref(con, "evidence",
                                                "fixture-ad")
            out = _saved.create_item(
                con, {"type": "excerpt", "title": "Clip",
                      "target": {"ref": ref, "start_s": 10.0,
                                 "end_s": 20.0}},
                workspace_context="ctx-1")
            captured = out["item"]["captured"]
            self.assertIn("Lumen", captured["text"])
            self.assertEqual(captured["source_ref"], ref)
            self.assertEqual(captured["selected_bounds"],
                             {"start_s": 10.0, "end_s": 20.0})
            self.assertEqual(captured["bounds_basis"],
                             "overlapping_segments")
            con.execute("UPDATE evidence SET excerpt='rewritten live'"
                        " WHERE id='fixture-ad'")
            con.commit()
            same = _saved.get_item(con, out["item"]["id"])
            self.assertEqual(same["item"]["captured"]["text"],
                             captured["text"])
            self.assertEqual(same["item"]["availability"], "current")
            con.execute("UPDATE reviews SET judgment='ad', revision=2"
                        " WHERE evidence_id='fixture-ad'")
            con.execute("UPDATE evidence SET revision=2 WHERE id='fixture-ad'")
            _citations.publish_source_state(
                con, kind="evidence", record_id="fixture-ad",
                actor="fixture")
            con.commit()
            again = _saved.get_item(con, out["item"]["id"])
            self.assertEqual(again["item"]["captured"]["text"],
                             captured["text"])
            self.assertEqual(again["item"]["availability"],
                             "source_changed")
        finally:
            con.close()

    def test_excerpt_unresolvable_target_rejected(self):
        con = _con()
        try:
            with self.assertRaises(_citations.RevisionUnavailable):
                _saved.create_item(
                    con, {"type": "excerpt", "title": "Ghost",
                          "target": {"ref": "sr1:" + "0" * 32}},
                    workspace_context="ctx-1")
        finally:
            con.close()

    def test_excerpt_unpaired_seconds_rejected(self):
        con = _con()
        try:
            ref = _citations.current_source_ref(con, "evidence",
                                                "fixture-ad")
            with self.assertRaises(_search.FilterRejected):
                _saved.create_item(
                    con, {"type": "excerpt", "title": "Bad",
                          "target": {"ref": ref, "start_s": 5.0}},
                    workspace_context="ctx-1")
            with self.assertRaises(_search.FilterRejected):
                _saved.create_item(
                    con, {"type": "excerpt", "title": "Bad",
                          "target": {"ref": ref, "start_s": 20.0,
                                     "end_s": 10.0}},
                    workspace_context="ctx-1")
        finally:
            con.close()

    def test_excerpt_overlarge_selection_rejected_not_truncated(self):
        con = _con()
        try:
            import json as _json
            big = "word " * 4000
            payload = _json.dumps({"segments": [
                {"start": 0.0, "end": 100.0, "text": big}]})
            import hashlib as _hashlib
            digest = _hashlib.sha256(payload.encode()).hexdigest()
            cur = con.execute(
                "INSERT INTO source_versions (source_key, version_no,"
                " sha256, payload, is_active, created_at) VALUES"
                " ('transcript:e-big', 1, ?, ?, 1, ?)",
                (digest, payload, _db.utcnow()))
            con.execute(
                "INSERT INTO episodes (id, kind, show_title, ep_title,"
                " category, pub_date, fetch_date, transcript_version_id,"
                " revision, updated_at) VALUES ('e-big', 'supplied', 'Big',"
                " 'Big ep', 'business', '2026-05-01', '2026-09-21', ?, 1, ?)",
                (cur.lastrowid, _db.utcnow()))
            ref = _citations.publish_source_state(con, kind="episode", record_id="e-big", actor="fixture")
            con.commit()
            with self.assertRaises(_search.FilterRejected):
                _saved.create_item(
                    con, {"type": "excerpt", "title": "Too big",
                          "target": {"ref": ref}},
                    workspace_context="ctx-1")
            self.assertEqual(
                _saved.list_items(con)["total"], 0)
        finally:
            con.close()

    def test_excerpt_source_replacement_creates_new_item(self):
        con = _con()
        try:
            ref = _citations.current_source_ref(con, "evidence",
                                                "fixture-ad")
            out = _saved.create_item(
                con, {"type": "excerpt", "title": "Clip",
                      "target": {"ref": ref}},
                workspace_context="ctx-1")
            other = _citations.current_source_ref(con, "episode", "e1")
            with self.assertRaises(_ops.BadInput):
                _saved.update_item(
                    con, out["item"]["id"],
                    {"target": {"ref": other}}, 1)
            again = _saved.get_item(con, out["item"]["id"])
            self.assertEqual(again["item"]["revision"], 1)
            self.assertEqual(again["item"]["captured"]["source_ref"], ref)
        finally:
            con.close()


class QuerySaveTest(unittest.TestCase):
    def test_query_stores_definition_not_counts(self):
        con = _con()
        try:
            out = _saved.create_item(
                con, {"type": "query", "title": "Stored search",
                      "query": dict(BASE_Q)}, workspace_context="ctx-1")
            item = out["item"]
            self.assertEqual(item["query"]["sort"], "newest")
            self.assertEqual(item["query"]["page"], 1)
            self.assertNotIn("refs", item.get("captured", {}))
            self.assertNotIn("total", item.get("captured", {}))
            stored = json.loads(con.execute(
                "SELECT selector_json FROM customer_saved_items WHERE id=?",
                (item["id"],)).fetchone()["selector_json"] or "null")
            self.assertIsNone(stored)
        finally:
            con.close()

    def test_query_with_live_selector_round_trip(self):
        con = _con()
        try:
            live = {"version": 1, "base_query": dict(BASE_Q),
                    "clause": {"kind": "all_results"},
                    "count_units": "distinct_results",
                    "scope_mode": "live_definition",
                    "view_revision": None, "workspace_context": None}
            out = _saved.create_item(
                con, {"type": "query", "title": "Live",
                      "query": dict(BASE_Q), "selector": live},
                workspace_context="ctx-1")
            selector = out["item"]["selector"]
            self.assertEqual(selector["scope_mode"], "live_definition")
            self.assertIsNone(selector["view_revision"])
        finally:
            con.close()

    def test_query_save_creates_no_tracker(self):
        con = _con()
        try:
            before = con.execute(
                "SELECT COUNT(*) n FROM customer_alerts").fetchone()["n"]
            _saved.create_item(
                con, {"type": "query", "title": "No tracker",
                      "query": dict(BASE_Q)}, workspace_context="ctx-1")
            after = con.execute(
                "SELECT COUNT(*) n FROM customer_alerts").fetchone()["n"]
            self.assertEqual(before, after)
        finally:
            con.close()

    def test_query_rename_preserves_definition(self):
        con = _con()
        try:
            out = _saved.create_item(
                con, {"type": "query", "title": "Old",
                      "query": dict(BASE_Q)}, workspace_context="ctx-1")
            edited = _saved.update_item(
                con, out["item"]["id"], {"title": "New"}, 1)
            self.assertEqual(edited["item"]["title"], "New")
            self.assertEqual(edited["item"]["query"]["q"], "lumen")
            self.assertEqual(edited["item"]["revision"], 2)
        finally:
            con.close()


class BookmarkTest(unittest.TestCase):
    def test_show_bookmark_opens_current_profile(self):
        con = _con()
        try:
            show = _customer.customer_show_for_episode(con, "e1")
            out = _saved.create_item(
                con, {"type": "bookmark", "title": "Show",
                      "target": {"kind": "show", "id": show["show_identity_id"]}},
                workspace_context="ctx-1")
            captured = out["item"]["captured"]
            self.assertEqual(captured["opens"], "current_profile")
        finally:
            con.close()

    def test_creative_bookmark_uses_server_identity(self):
        con = _con()
        try:
            out = _saved.create_item(
                con, {"type": "bookmark", "title": "Creative",
                      "target": {"kind": "creative", "id": "cr-1"}},
                workspace_context="ctx-1")
            self.assertEqual(
                out["item"]["captured"]["opens"], "current_profile")
            with self.assertRaises(_ops.BadInput):
                _saved.create_item(
                    con, {"type": "bookmark", "title": "Ghost",
                          "target": {"kind": "creative", "id": "cr-ghost"}},
                    workspace_context="ctx-1")
        finally:
            con.close()

    def test_pinned_episode_validates_agreement(self):
        con = _con()
        try:
            ref = _citations.current_source_ref(con, "episode", "e1")
            ep = con.execute("SELECT transcript_version_id FROM episodes"
                             " WHERE id='e1'").fetchone()
            out = _saved.create_item(
                con, {"type": "bookmark", "title": "Pinned",
                      "target": {"kind": "episode", "id": "e1",
                                 "transcript_version_id":
                                 ep["transcript_version_id"],
                                 "source_ref": ref}},
                workspace_context="ctx-1")
            self.assertEqual(out["item"]["captured"]["opens"],
                             "captured_pin")
            with self.assertRaises(_ops.BadInput):
                _saved.create_item(
                    con, {"type": "bookmark", "title": "Mismatch",
                          "target": {"kind": "episode", "id": "e2",
                                     "transcript_version_id":
                                     ep["transcript_version_id"],
                                     "source_ref": ref}},
                    workspace_context="ctx-1")
        finally:
            con.close()

    def test_metadata_only_episode_opens_current(self):
        con = _con()
        try:
            out = _saved.create_item(
                con, {"type": "bookmark", "title": "Later",
                      "target": {"kind": "episode", "id": "e1"}},
                workspace_context="ctx-1")
            captured = out["item"]["captured"]
            self.assertEqual(captured["opens"], "current_profile")
            self.assertEqual(captured["episode_id"], "e1")
        finally:
            con.close()


class TypedAvailabilityTest(unittest.TestCase):
    def test_live_definitions_and_current_profiles_need_no_source_pin(self):
        con = _con()
        self.addCleanup(con.close)
        ctx = _access.AccessContext.create(
            principal_id="owner", principal_kind="owner", workspace_id="local",
            credential_kind="local_operator")
        show = _customer.customer_show_for_episode(con, "e1")["show_identity_id"]
        cases = [
            {"type": "query", "title": "Live query", "query": dict(BASE_Q)},
            {"type": "query", "title": "Live selected query", "query": dict(BASE_Q),
             "selector": {"version": 1, "base_query": dict(BASE_Q),
                          "clause": {"kind": "all_results"}, "count_units": "distinct_results",
                          "scope_mode": "live_definition", "view_revision": None,
                          "workspace_context": None}},
            {"type": "bookmark", "title": "Show", "target": {"kind": "show", "id": show}},
            {"type": "bookmark", "title": "Episode", "target": {"kind": "episode", "id": "e1"}},
            {"type": "bookmark", "title": "Creative", "target": {"kind": "creative", "id": "cr-1"}},
        ]
        for body in cases:
            with self.subTest(type=body["type"], title=body["title"]):
                con.execute("PRAGMA query_only=OFF")
                saved = _saved.create_item(con, body, workspace_context="ctx-1", ctx=ctx)["item"]
                before = [tuple(row) for row in con.execute(
                    "SELECT * FROM customer_saved_items ORDER BY id")]
                con.execute("PRAGMA query_only=ON")
                item = _saved.get_item(con, saved["id"], workspace_context="ctx-1", ctx=ctx)["item"]
                self.assertEqual(item["availability"], "current")
                self.assertNotIn("availability_reason", item)
                self.assertFalse((item.get("captured") or {}).get("source_ref"))
                if body["type"] == "query":
                    self.assertEqual(item["query"], saved["query"])
                    self.assertNotIn("captured", item)
                else:
                    self.assertEqual(item["captured"]["opens"], "current_profile")
                self.assertEqual([tuple(row) for row in con.execute(
                    "SELECT * FROM customer_saved_items ORDER BY id")], before)

    def test_current_profile_is_unavailable_when_actual_identity_is_removed(self):
        con = _con()
        self.addCleanup(con.close)
        saved = _saved.create_item(
            con, {"type": "bookmark", "title": "Creative",
                  "target": {"kind": "creative", "id": "cr-1"}},
            workspace_context="ctx-1")["item"]
        self.assertEqual(saved["availability"], "current")
        con.execute("DELETE FROM creatives WHERE id='cr-1'")
        con.commit()
        con.execute("PRAGMA query_only=ON")
        item = _saved.get_item(con, saved["id"])["item"]
        self.assertEqual(item["availability"], "unavailable")
        self.assertEqual(item["target"], saved["target"])

    def test_current_profile_keeps_actual_metadata_denial(self):
        con = _con()
        self.addCleanup(con.close)
        show = _customer.customer_show_for_episode(con, "e1")["show_identity_id"]
        denied = _access.AccessContext.create(
            principal_id="reader", principal_kind="owner", workspace_id="local",
            credential_kind="local_reader", scopes={"research:read"},
            content_policy={"fields": ["excerpt"]})
        for kind, identity in (("show", show), ("episode", "e1")):
            with self.subTest(kind=kind):
                con.execute("PRAGMA query_only=OFF")
                item = _saved.create_item(
                    con, {"type": "bookmark", "title": kind,
                          "target": {"kind": kind, "id": identity}},
                    workspace_context="ctx-1")["item"]
                con.execute("PRAGMA query_only=ON")
                with self.assertRaises(_access.AuthorizationError):
                    _saved.get_item(con, item["id"], ctx=denied)


class CollectionTest(unittest.TestCase):
    def _seed(self, con, count=3):
        ref = _citations.current_source_ref(con, "episode", "e1")
        for n in range(count):
            _saved.create_item(
                con, {"type": "bookmark", "title": f"Mark {n}",
                      "target": {"kind": "episode", "id": "e1"}},
                workspace_context="ctx-1")
        return ref

    def test_list_envelope_filter_cursor_counts(self):
        con = _con()
        try:
            self._seed(con, 3)
            first = _saved.list_items(con, limit=2)
            self.assertEqual(first["total"], 3)
            self.assertEqual(first["total_relation"], "exact")
            self.assertIsNotNone(first["next_cursor"])
            self.assertEqual(first["counts"]["bookmark"], 3)
            second = _saved.list_items(con, cursor=first["next_cursor"],
                                       limit=2)
            self.assertIsNone(second["next_cursor"])
            ids = {i["id"] for i in first["items"]} | {
                i["id"] for i in second["items"]}
            self.assertEqual(len(ids), 3)
            filtered = _saved.list_items(con, q="mark 1")
            self.assertEqual(filtered["total"], 1)
            typed = _saved.list_items(con, item_type="query")
            self.assertEqual(typed["total"], 0)
        finally:
            con.close()

    def test_changed_collection_rejects_cursor(self):
        con = _con()
        try:
            self._seed(con, 1)
            first = _saved.list_items(con, limit=1)
            _saved.create_item(
                con, {"type": "bookmark", "title": "New",
                      "target": {"kind": "episode", "id": "e1"}},
                workspace_context="ctx-1")
            with self.assertRaises(_search.FilterRejected):
                _saved.list_items(con, cursor="bogus")
            if first["next_cursor"] is not None:
                with self.assertRaises(_search.FilterRejected):
                    _saved.list_items(con, cursor=first["next_cursor"])
        finally:
            con.close()

    def test_title_limit_and_revision_gates(self):
        con = _con()
        try:
            with self.assertRaises(_search.FilterRejected):
                _saved.create_item(
                    con, {"type": "bookmark", "title": "x" * 121,
                          "target": {"kind": "episode", "id": "e1"}},
                    workspace_context="ctx-1")
            out = _saved.create_item(
                con, {"type": "bookmark", "title": "B",
                      "target": {"kind": "episode", "id": "e1"}},
                workspace_context="ctx-1")
            with self.assertRaises(_ops.MissingPrecondition):
                _saved.update_item(con, out["item"]["id"], {"title": "B2"},
                                   None)
            with self.assertRaises(_ops.Conflict):
                _saved.update_item(con, out["item"]["id"], {"title": "B2"},
                                   99)
            with self.assertRaises(_ops.MissingPrecondition):
                _saved.delete_item(con, out["item"]["id"], None)
            done = _saved.delete_item(con, out["item"]["id"], 1)
            self.assertEqual(done["removed_id"], out["item"]["id"])
            with self.assertRaises(_ops.BadInput):
                _saved.get_item(con, out["item"]["id"])
            self.assertEqual(
                con.execute("SELECT COUNT(*) n FROM episodes").fetchone()["n"],
                2)
        finally:
            con.close()

    def test_reader_cannot_save_item(self):
        con = _con()
        try:
            ctx = _access.AccessContext.create(
                principal_id="ed-1", principal_kind="editor",
                workspace_id="ws1", authority_kind="membership",
                scopes={"research:read", "transcripts:read"},
                credential_kind="reader_bearer")
            with self.assertRaises(_access.AuthorizationError):
                _services.customer_save_item(
                    ctx, con, _entry(),
                    {"type": "bookmark", "title": "B",
                     "target": {"kind": "episode", "id": "e1"}})
        finally:
            con.close()


class PersonalNoteTest(unittest.TestCase):
    def test_note_create_needs_valid_pin(self):
        con = _con()
        try:
            with self.assertRaises(_ops.BadInput):
                _saved.save_note(con, "ev:ghost@v1", "wrong span")
        finally:
            con.close()

    def test_note_edit_after_source_loss_without_resolution(self):
        con = _con()
        try:
            ref = _citations.current_source_ref(con, "episode", "e1")
            saved = _saved.save_note(con, ref, "check this")
            con.execute("DELETE FROM source_states WHERE id=?", (ref,))
            con.commit()
            edited = _saved.save_note(con, ref, "still mine", "open",
                                      saved["id"], saved["revision"])
            self.assertEqual(edited["revision"], saved["revision"] + 1)
            detail = _saved.get_note(con, saved["id"])
            self.assertEqual(detail["note"]["reason"], "still mine")
            self.assertEqual(detail["note"]["note_status"], "open")
            self.assertEqual(detail["note"]["delivery_context"],
                             "local_only")
        finally:
            con.close()

    def test_note_denied_source_hides_text(self):
        con = _con()
        try:
            ref = _citations.current_source_ref(con, "evidence",
                                                "fixture-ad")
            saved = _saved.save_note(con, ref, "span off")
            con.execute("DELETE FROM source_states WHERE id=?",
                        (ref,))
            con.commit()
            detail = _saved.get_note(con, saved["id"])
            self.assertEqual(detail["note"]["reason"], "span off")
            self.assertNotIn("captured_text", detail["note"])
        finally:
            con.close()

    def test_stale_and_foreign_note_denied(self):
        con = _con()
        try:
            ref = _citations.current_source_ref(con, "episode", "e1")
            saved = _saved.save_note(con, ref, "mine")
            with self.assertRaises(_ops.Conflict):
                _saved.save_note(con, ref, "hijack", "open", saved["id"],
                                 99)
            other = _citations.current_source_ref(con, "episode", "e2")
            with self.assertRaises(_ops.BadInput):
                _saved.save_note(con, other, "moved", "open", saved["id"],
                                 saved["revision"])
        finally:
            con.close()

    def test_note_collection_and_direct_read(self):
        con = _con()
        try:
            ref = _citations.current_source_ref(con, "episode", "e1")
            saved = _saved.save_note(con, ref, "first note")
            coll = _saved.list_notes(con, ref=ref)
            self.assertEqual(coll["total"], 1)
            self.assertEqual(coll["notes"][0]["id"], saved["id"])
            queried = _saved.list_notes(con, q="first")
            self.assertEqual(queried["total"], 1)
            direct = _saved.get_note(con, saved["id"])
            self.assertEqual(direct["note"]["ref"], ref)
        finally:
            con.close()


class NoteMetadataRouteHttpTest(unittest.TestCase):
    def _state(self, path):
        from tests.test_backend_final_blockers import _snapshot
        con = _db.open_existing(path, readonly=True)
        try:
            return _snapshot(con)
        finally:
            con.close()

    def _exercise(self, call, path, note_id, foreign_id, creation_ref):
        from urllib.parse import quote
        route = f"/api/v1/customer/mistakes/{note_id}"
        status, detail = call("GET", route)
        self.assertEqual(status, 200)
        self.assertNotIn("ref", detail["note"])
        before = self._state(path)
        con = _db.open_existing(path, readonly=True)
        try:
            original = dict(con.execute("SELECT * FROM customer_mistake_reports WHERE id=?",
                                        (note_id,)).fetchone())
        finally:
            con.close()
        status, updated = call("PUT", route, {
            "reason": "Personal metadata only", "status": "acknowledged",
            "expected_revision": detail["note"]["revision"]})
        self.assertEqual(status, 200)
        self.assertEqual(updated["revision"], detail["note"]["revision"] + 1)
        after = self._state(path)
        for table in before:
            if table not in ("customer_mistake_reports", "audit", "meta"):
                self.assertEqual(after[table], before[table], table)
        con = _db.open_existing(path, readonly=True)
        try:
            stored = dict(con.execute("SELECT * FROM customer_mistake_reports WHERE id=?",
                                      (note_id,)).fetchone())
        finally:
            con.close()
        for field in ("ref", "workspace_id", "created_by", "created_at"):
            self.assertEqual(stored[field], original[field], field)
        status, changed = call("GET", route)
        self.assertEqual(status, 200)
        self.assertNotIn("ref", changed["note"])
        self.assertEqual(changed["note"]["reason"], "Personal metadata only")
        self.assertEqual(changed["note"]["note_status"], "acknowledged")
        for extra, revision, expected in (
            ({}, detail["note"]["revision"], 412),
            ({"ref": "invented"}, updated["revision"], 400),
            ({"mistake_id": foreign_id}, updated["revision"], 400),
            ({"actor": "other"}, updated["revision"], 400),
            ({"verification_context": "approved"}, updated["revision"], 400),
            ({"judgment": "ad"}, updated["revision"], 400),
        ):
            with self.subTest(extra=extra, revision=revision):
                before = self._state(path)
                status, _ = call("PUT", route, {
                    "reason": "Must not write", "status": "resolved",
                    "expected_revision": revision, **extra})
                self.assertEqual(status, expected)
                self.assertEqual(self._state(path), before)
        before = self._state(path)
        status, _ = call("PUT", route, {"reason": "Missing basis", "status": "open"})
        self.assertEqual(status, 428)
        self.assertEqual(self._state(path), before)
        before = self._state(path)
        status, _ = call("PUT", f"/api/v1/customer/mistakes/{foreign_id}", {
            "reason": "Not mine", "status": "open", "expected_revision": 1})
        self.assertEqual(status, 404)
        self.assertEqual(self._state(path), before)
        creation = "/api/v1/customer/records/" + quote(creation_ref, safe="") + "/mistake"
        status, created = call("POST", creation, {"reason": "New personal note", "status": "open"})
        self.assertEqual(status, 200)
        self.assertNotEqual(created["id"], note_id)
        before = self._state(path)
        status, _ = call("PUT", creation, {"reason": "Obsolete path", "expected_revision": 1})
        self.assertEqual(status, 404)
        self.assertEqual(self._state(path), before)
        self.assertEqual(stored["revision"], updated["revision"])

    def test_local_identified_metadata_edit_without_available_source(self):
        from tests.test_product_processing_api import HandlerCase
        fixture = HandlerCase()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        path = fixture.ws / "workspace.sqlite3"
        con = _db.open_existing(path, readonly=False)
        try:
            backend_fixtures.seed_source_rows(con)
            refs = backend_fixtures.publish_sources(con)
            foreign = _saved.save_note(con, refs["e1"], "Foreign", actor="other")
        finally:
            con.close()
        self._exercise(lambda method, route, body=None: fixture.call(method, route, body),
                       path, 1, foreign["id"], refs["e2"])

    def test_hosted_owned_metadata_edit_without_source_disclosure(self):
        from tests.test_backend_authority import HostedReaderHttpTest
        fixture = HostedReaderHttpTest()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        con = _db.open_existing(fixture.db_path, readonly=False)
        try:
            refs = backend_fixtures.publish_sources(con)
            owned = _saved.save_note(con, refs["e1"], "Own",
                                     actor="oidc:https://i.example:owner")
            foreign = _saved.save_note(con, refs["e1"], "Foreign", actor="other")
            key = con.execute("SELECT source_key FROM source_versions WHERE id="
                              "(SELECT transcript_version_id FROM episodes WHERE id='e1')").fetchone()[0]
            con.execute("INSERT OR REPLACE INTO source_content_policy"
                        " (workspace_id,source_key,content_category,agent_disclosure,updated_at)"
                        " VALUES ('local',?,'metadata','deny',?)", (key, _db.utcnow()))
        finally:
            con.close()
        headers = fixture._csrf()
        self._exercise(lambda method, route, body=None: fixture._call(
            route, method=method, body=body, headers=headers),
            fixture.db_path, owned["id"], foreign["id"], refs["e2"])
        before = self._state(fixture.db_path)
        status, _ = fixture._call(
            f"/api/v1/customer/mistakes/{owned['id']}", method="PUT",
            body={"reason": "Reader cannot write", "status": "open", "expected_revision": 2},
            headers={"authorization": "Bearer " + fixture.reader_token})
        self.assertEqual(status, 403)
        self.assertEqual(self._state(fixture.db_path), before)


class AlertDurabilityTest(unittest.TestCase):
    def _alert(self, con):
        query = {"version": 2, "mode": "mentions", "q": "Lumen"}
        return _customer.save_alert(con, "News", query, origin_route="/x",
                                    event_types=["new_text"])

    def test_save_stamps_selector_and_success(self):
        con = _con()
        try:
            out = self._alert(con)
            self.assertEqual(out["lifecycle"], "active_local")
            row = con.execute("SELECT query_version, live_selector_json,"
                              " last_attempt_result, last_error,"
                              " resource_revision FROM customer_alerts"
                              " WHERE id=?", (out["id"],)).fetchone()
            self.assertEqual(row[0], 2)  # canonical Query2
            selector = json.loads(row[1])
            self.assertEqual(selector, {})
            self.assertEqual(row[2], "success")
            self.assertEqual(row[3], "")
            self.assertEqual(row[4], 1)
            dto = _customer.get_alert(con, out["id"])
            self.assertEqual(dto["query"]["mode"], "mentions")
            self.assertIsNone(dto["selector"])
            self.assertTrue(dto["baseline_complete"])
        finally:
            con.close()

    def test_failed_check_records_error_durably(self):
        con = _con()
        try:
            out = self._alert(con)
            out = _customer.set_alert_lifecycle(con, out["id"], "active_local",
                                               out["revision"])
            cap = _customer.SCAN_SEGMENT_CAP
            _customer.SCAN_SEGMENT_CAP = 0
            _customer_query._VIEW_MEMO.clear()  # the cap is an input outside the memo key
            try:
                result = _customer.check_alert(con, out["id"])
            finally:
                _customer.SCAN_SEGMENT_CAP = cap
                _customer_query._VIEW_MEMO.clear()  # the cap is an input outside the memo key
            self.assertFalse(result["ok"])
            row = con.execute("SELECT last_attempt_result, last_error"
                              " FROM customer_alerts WHERE id=?",
                              (out["id"],)).fetchone()
            self.assertEqual(row[0], "error")
            self.assertTrue(row[1])
        finally:
            con.close()

    def test_paused_alert_check_refused(self):
        con = _con()
        try:
            out = self._alert(con)
            _customer.set_alert_lifecycle(con, out["id"], "paused", 1)
            with self.assertRaises(_ops.BadInput):
                _customer.check_alert(con, out["id"])
        finally:
            con.close()

    def test_successful_check_advances_and_events_carry_source(self):
        con = _con()
        try:
            out = self._alert(con)
            out = _customer.set_alert_lifecycle(con, out["id"], "active_local",
                                               out["revision"])
            result = _customer.check_alert(con, out["id"])
            self.assertTrue(result["ok"])
            row = con.execute("SELECT last_attempt_result, last_error,"
                              " resource_revision FROM customer_alerts"
                              " WHERE id=?", (out["id"],)).fetchone()
            self.assertEqual(row[0], "success")
            self.assertEqual(row[1], "")
            self.assertEqual(row[2], 2)  # create stamp + check stamp
            events = con.execute("SELECT source_event_json FROM"
                                 " customer_alert_events WHERE alert_id=?",
                                 (out["id"],)).fetchall()
            for (payload,) in events:
                saved = json.loads(payload)
                self.assertIn("transition_key", saved)
                self.assertIn("definition_revision", saved)
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
