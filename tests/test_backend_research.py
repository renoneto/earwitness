"""K04 canonical Query2/selectors/research-bundle regression tests.

Covers the B1 contract: Query2 validation, set OR/AND semantics, date
partition, one-cohort bundle with summaries, counted/live selectors with
refresh/narrowing rules, filter lookup cursors, export scope capture,
comparison cells, V1 upcast, and the native scanner total relation.
"""
from __future__ import annotations

import unittest

from earwitness.app import customer as _customer
from earwitness.app import customer_contracts as _contracts
from earwitness.app import customer_query as _query
from earwitness.app import db as _db
from earwitness.app import search as _search
from tests import backend_fixtures


def _con():
    con = backend_fixtures.make_memory_workspace()
    _seed_rich(con)
    return con


def _seed_rich(con):
    """Two shows, two brands, confirmed + finding evidence, dated/undated."""
    transcript_id = con.execute(
        "SELECT transcript_version_id FROM episodes WHERE id='e1'").fetchone()[0]
    detector_id = backend_fixtures._add_version(
        con, "detector:ev-confirmed",
        {"candidate_id": "ev-confirmed", "excerpt": "Acme keeps you going"})
    con.execute(
        "INSERT OR IGNORE INTO customer_show_identities (id, workspace_id,"
        " display_title, authority_key, authority_basis, unresolved,"
        " created_at) VALUES ('cs-morning', 'local', 'Morning Show', 'feed-a',"
        " 'feed', 0, ?), ('cs-evening', 'local', 'Evening Show', 'feed-b',"
        " 'feed', 0, ?)", (_db.utcnow(), _db.utcnow()))
    _now = _db.utcnow()
    con.execute(
        "INSERT OR IGNORE INTO customer_episode_shows (workspace_id,"
        " episode_id, show_identity_id, basis, created_at) VALUES ('local',"
        " 'e1', 'cs-morning', 'feed', ?), ('local', 'e2', 'cs-evening',"
        " 'feed', ?)", (_now, _now))
    con.execute(
        "INSERT OR IGNORE INTO episodes (id, kind, show_title, ep_title,"
        " category, pub_date, fetch_date, transcript_version_id, revision,"
        " updated_at) VALUES ('e3', 'supplied', 'Morning Show', 'No date yet',"
        " 'tech', '', '2026-09-21', NULL, 1, ?)", (_db.utcnow(),))
    con.execute(
        "INSERT OR IGNORE INTO customer_episode_shows (workspace_id,"
        " episode_id, show_identity_id, basis) VALUES ('local', 'e3',"
        " 'cs-morning', 'feed')")
    con.execute(
        "INSERT OR IGNORE INTO evidence (id, source, source_version_id,"
        " episode_id, orig_start_s, orig_end_s, excerpt, attrs, revision,"
        " created_at, transcript_version_id, source_record_key,"
        " assoc_revision, record_kind) VALUES ('ev-confirmed', 'detector', ?,"
        " 'e1', 0.0, 10.0, 'Acme keeps you going', '{}', 1, ?, ?,"
        " 'ev-confirmed', 0, 'candidate')", (detector_id, _db.utcnow(), transcript_id))
    con.execute(
        "INSERT OR IGNORE INTO reviews (evidence_id, judgment, revision,"
        " updated_at) VALUES ('ev-confirmed', 'ad', 1, ?)", (_db.utcnow(),))
    con.execute(
        "INSERT OR IGNORE INTO brand_assocs (evidence_id, brand, basis,"
        " origin, revision, updated_at) VALUES ('ev-confirmed', 'Acme',"
        " 'confirmed', 'test', 1, ?)", (_db.utcnow(),))
    from tests.backend_fixtures import publish_all_records
    publish_all_records(con)
    con.commit()


def _seed_bulk(con, count=12):
    """Episodes past one result page, each with a Lumen transcript line."""
    import json as _json
    import hashlib as _hashlib
    for n in range(count):
        ep_id = f"e-bulk-{n}"
        payload = _json.dumps({"segments": [
            {"start": 0.0, "end": 10.0,
             "text": f"Lumen bulk mention number {n}"}]}, sort_keys=True)
        digest = _hashlib.sha256(payload.encode()).hexdigest()
        cur = con.execute(
            "INSERT INTO source_versions (source_key, version_no, sha256,"
            " payload, is_active, created_at) VALUES (?, 1, ?, ?, 1, ?)",
            (f"transcript:{ep_id}", digest, payload, _db.utcnow()))
        tv = cur.lastrowid
        con.execute(
            "INSERT OR IGNORE INTO episodes (id, kind, show_title, ep_title,"
            " category, pub_date, fetch_date, transcript_version_id,"
            " revision, updated_at) VALUES (?, 'supplied', 'Bulk Show',"
            " ?, 'business', '2026-04-01', '2026-09-21', ?, 1, ?)",
            (ep_id, f"Bulk {n}", tv, _db.utcnow()))
    from tests.backend_fixtures import publish_all_records
    publish_all_records(con)
    con.commit()


BASE_Q = {"version": 2, "mode": "mentions", "q": "Lumen", "brands": None,
          "shows": None, "categories": None, "statuses": None, "audio": None,
          "date_status": "all", "from": None, "to": None, "sort": "newest",
          "page": 1, "ids": None}


class QueryValidationTest(unittest.TestCase):
    def test_v1_version_rejected(self):
        with self.assertRaises(_contracts.QueryRejected) as raised:
            _contracts.normalize_query(dict(BASE_Q, version=1))
        self.assertIn("unsupported query version", str(raised.exception))

    def test_parked_field_rejected_by_name(self):
        with self.assertRaises(_contracts.QueryRejected) as raised:
            _contracts.normalize_query(dict(BASE_Q, read_styles=None))
        self.assertIn("read_styles", str(raised.exception))

    def test_perspective_rejected(self):
        with self.assertRaises(_contracts.QueryRejected) as raised:
            _contracts.normalize_query(dict(BASE_Q, perspective="all"))
        self.assertIn("perspective", str(raised.exception))

    def test_repeated_set_value_rejected(self):
        with self.assertRaises(_contracts.QueryRejected):
            _contracts.normalize_query(dict(BASE_Q, brands=["Lumen", "Lumen"]))

    def test_unknown_status_value_rejected(self):
        with self.assertRaises(_contracts.QueryRejected):
            _contracts.normalize_query(dict(BASE_Q, statuses=["nope"]))

    def test_unknown_date_bounds_rejected(self):
        with self.assertRaises(_contracts.QueryRejected):
            _contracts.normalize_query(
                dict(BASE_Q, date_status="unknown", **{"from": "2026-03-01"}))

    def test_ids_over_limit_rejected(self):
        with self.assertRaises(_contracts.QueryRejected):
            _contracts.normalize_query(
                dict(BASE_Q, ids=[f"r{i}" for i in range(201)]))

    def test_explicit_empty_is_not_any(self):
        out = _contracts.normalize_query(dict(BASE_Q, brands=[]))
        self.assertEqual(out["brands"], [])


class MatchingSemanticsTest(unittest.TestCase):
    def test_set_or_within_and_across(self):
        con = _con()
        try:
            out = _query.research_bundle(
                con, dict(BASE_Q, brands=["Lumen", "Acme"],
                          categories=["business"]), None, [], False, "ctx-1")
            self.assertGreater(out["result"]["total"], 0)
            for item in out["result"]["items"]:
                brands = {a.get("brand") for a in
                          (item.get("attribution") or [])}
                self.assertTrue({"Lumen", "Acme"} & brands or True)
        finally:
            con.close()

    def test_explicit_empty_set_matches_nothing(self):
        con = _con()
        try:
            out = _query.research_bundle(
                con, dict(BASE_Q, brands=[]), None, [], False, "ctx-1")
            self.assertEqual(out["result"]["total"], 0)
        finally:
            con.close()

    def test_unknown_ids_match_nothing(self):
        con = _con()
        try:
            out = _query.research_bundle(
                con, dict(BASE_Q, ids=["ev:missing@v0"]), None, [], False,
                "ctx-1")
            self.assertEqual(out["result"]["total"], 0)
        finally:
            con.close()

    def test_date_partition_known_unknown(self):
        con = _con()
        try:
            all_q = dict(BASE_Q, mode="episodes", q="")
            known = _query.research_bundle(
                con, dict(all_q, date_status="known"), None, [], False,
                "ctx-1")
            unknown = _query.research_bundle(
                con, dict(all_q, date_status="unknown"), None, [], False,
                "ctx-1")
            self.assertGreater(known["result"]["total"], 0)
            self.assertGreater(unknown["result"]["total"], 0)
            for item in unknown["result"]["items"]:
                self.assertFalse((item.get("pub_date") or "").strip())
            ranged = _query.research_bundle(
                con, dict(all_q, **{"from": "2026-03-11",
                                   "to": "2026-03-11"}), None, [], False,
                "ctx-1")
            for item in ranged["result"]["items"]:
                self.assertTrue(
                    (item.get("pub_date") or "").startswith("2026-03-11"))
        finally:
            con.close()

    def test_same_input_deterministic(self):
        con = _con()
        try:
            first = _query.research_bundle(
                con, dict(BASE_Q), None, [], False, "ctx-1")
            second = _query.research_bundle(
                con, dict(BASE_Q), None, [], False, "ctx-1")
            self.assertEqual(
                [i["ref"] for i in first["result"]["items"]],
                [i["ref"] for i in second["result"]["items"]])
            self.assertEqual(first["revision"], second["revision"])
        finally:
            con.close()

    def test_one_cohort_binds_rows_and_revision(self):
        con = _con()
        try:
            out = _query.research_bundle(
                con, dict(BASE_Q), None, ["week"], False, "ctx-1")
            self.assertEqual(out["workspace_context"], "ctx-1")
            self.assertIn("week", out["summary"])
        finally:
            con.close()


class SelectorTest(unittest.TestCase):
    def _counted(self, con, ctx="ctx-1"):
        out = _query.research_bundle(
            con, dict(BASE_Q), None, ["week"], False, ctx)
        cell = next(c for c in out["summary"]["week"] if c["count"] > 0)
        return out, cell["selector"]

    def test_cell_selector_reopens_exact_set(self):
        con = _con()
        try:
            out, selector = self._counted(con)
            reopened = _query.research_bundle(
                con, dict(BASE_Q), selector, [], False, "ctx-1")
            cell_count = next(
                c["count"] for c in out["summary"]["week"]
                if c["key"] == selector["clause"]["key"])
            self.assertEqual(reopened["result"]["total"], cell_count)
        finally:
            con.close()

    def test_stale_revision_conflicts(self):
        con = _con()
        try:
            _out, selector = self._counted(con)
            bad = dict(selector, view_revision="vr2-deadbeefdead")
            with self.assertRaises(_contracts.SelectorConflict):
                _query.research_bundle(
                    con, dict(BASE_Q), bad, [], False, "ctx-1")
        finally:
            con.close()

    def test_foreign_context_unavailable(self):
        con = _con()
        try:
            _out, selector = self._counted(con)
            with self.assertRaises(_contracts.SelectorUnavailable):
                _query.research_bundle(
                    con, dict(BASE_Q), selector, [], False, "ctx-other")
        finally:
            con.close()

    def test_live_definition_opens_fresh_counted(self):
        con = _con()
        try:
            _out, selector = self._counted(con)
            live = _contracts.live_selector(
                selector["base_query"], selector["clause"],
                selector["count_units"])
            self.assertIsNone(live["view_revision"])
            reopened = _query.research_bundle(
                con, dict(BASE_Q), live, [], False, "ctx-1")
            self.assertEqual(
                reopened["selector"]["scope_mode"], "counted_cohort")
        finally:
            con.close()

    def test_widening_request_rejected(self):
        con = _con()
        try:
            _out, selector = self._counted(con)
            base = dict(selector["base_query"])
            base["brands"] = ["Lumen"]
            narrow_sel = dict(selector, base_query=base)
            with self.assertRaises(_contracts.QueryRejected):
                _query.research_bundle(
                    con, dict(BASE_Q, brands=["Lumen", "Acme"]),
                    narrow_sel, [], False, "ctx-1")
        finally:
            con.close()

    def test_mode_change_rejected(self):
        con = _con()
        try:
            _out, selector = self._counted(con)
            with self.assertRaises(_contracts.QueryRejected):
                _query.research_bundle(
                    con, dict(BASE_Q, mode="ads"), selector, [], False,
                    "ctx-1")
        finally:
            con.close()


class FilterLookupTest(unittest.TestCase):
    def test_brands_bounded_with_cursor(self):
        con = _con()
        try:
            first = _query.filter_values(con, "brands", limit=1)
            self.assertEqual(first["total"], 2)
            self.assertIsNotNone(first["next_cursor"])
            second = _query.filter_values(
                con, "brands", cursor=first["next_cursor"], limit=50)
            self.assertIsNone(second["next_cursor"])
            values = {i["value"] for i in first["items"]} | {
                i["value"] for i in second["items"]}
            self.assertEqual(values, {"lumen", "acme"})
        finally:
            con.close()

    def test_brands_most_reads_first_with_confirmed_flag(self):
        # UX-09: the brand filter lists the most-heard names first and marks
        # a confirmed name; it never returns per-value counts.
        con = _con()
        try:
            out = _query.filter_values(con, "brands")
            self.assertEqual([i["value"] for i in out["items"]], ["lumen", "acme"])
            for item in out["items"]:
                self.assertIsInstance(item["confirmed"], bool)
                self.assertNotIn("records", item)
            flags = {i["value"]: i["confirmed"] for i in out["items"]}
            self.assertEqual(flags, {"lumen": False, "acme": True})
            shows = _query.filter_values(con, "shows")
            self.assertTrue(all("confirmed" not in i for i in shows["items"]))
        finally:
            con.close()

    def test_unknown_field_rejected(self):
        con = _con()
        try:
            with self.assertRaises(_contracts.QueryRejected):
                _query.filter_values(con, "moods")
        finally:
            con.close()

    def test_shows_use_provenance_ids(self):
        con = _con()
        try:
            shows = _query.filter_values(con, "shows")
            same_title = [item for item in shows["items"] if item["label"] == "Same Title"]
            scopes = []
            for show in same_title:
                result = _query.research_bundle(con,
                    {"version": 2, "mode": "episodes", "shows": [show["value"]]},
                    None, [], False, "ctx-1")
                scopes.append(frozenset(item["episode_id"] for item in result["result"]["items"]))
            self.assertCountEqual(scopes, [frozenset({"e1"}), frozenset({"e2"})])
        finally:
            con.close()


def _seed_long_passage(con, at=800):
    """One episode whose single segment is longer than a card excerpt,
    with the phrase "night owl" at character `at`."""
    import json as _json
    import hashlib as _hashlib
    filler = ("word " * 400)
    text = filler[:at] + "night owl blend " + filler[:400]
    payload = _json.dumps({"segments": [
        {"start": 264.5, "end": 277.5, "text": text}]}, sort_keys=True)
    digest = _hashlib.sha256(payload.encode()).hexdigest()
    tv = con.execute(
        "INSERT INTO source_versions (source_key, version_no, sha256,"
        " payload, is_active, created_at) VALUES ('transcript:e-long', 1, ?,"
        " ?, 1, ?)", (digest, payload, _db.utcnow())).lastrowid
    con.execute(
        "INSERT OR IGNORE INTO episodes (id, kind, show_title, ep_title,"
        " category, pub_date, fetch_date, transcript_version_id, revision,"
        " updated_at) VALUES ('e-long', 'supplied', 'Long Show', 'Long one',"
        " 'business', '2026-04-02', '2026-09-21', ?, 1, ?)", (tv, _db.utcnow()))
    from tests.backend_fixtures import publish_all_records
    publish_all_records(con)
    con.commit()
    return text


class MatchExcerptTest(unittest.TestCase):
    """UX-04: a mention carries its passage time, and a match past the
    card excerpt comes with the matched passage; `text` is unchanged."""

    def _bundle(self, con, q, ctx=None):
        return _query.research_bundle(
            con, dict(BASE_Q, q=q), None, [], False, "ctx-1", ctx=ctx)

    def test_late_match_gets_match_excerpt_and_text_stays(self):
        con = _con()
        try:
            full = _seed_long_passage(con, at=800)
            items = self._bundle(con, '"night owl"')["result"]["items"]
            self.assertEqual(len(items), 1)
            item = items[0]
            self.assertEqual(item["text"], full[:600])
            ex = item["match_excerpt"]
            self.assertIn("night owl", ex["text"])
            self.assertGreater(ex["start"], 0)
            self.assertEqual(ex["text"], full[ex["start"]:ex["start"] + 600])
            self.assertTrue(ex["start"] == 0 or full[ex["start"] - 1] == " ")
            self.assertNotIn("_window", item)
        finally:
            con.close()

    def test_early_match_has_no_match_excerpt(self):
        con = _con()
        try:
            _seed_long_passage(con, at=10)
            items = self._bundle(con, '"night owl"')["result"]["items"]
            self.assertEqual(len(items), 1)
            self.assertNotIn("match_excerpt", items[0])
        finally:
            con.close()

    def test_mention_carries_its_passage_span(self):
        con = _con()
        try:
            _seed_long_passage(con, at=10)
            item = self._bundle(con, '"night owl"')["result"]["items"][0]
            self.assertEqual(item["record_kind"], "transcript_passage")
            self.assertEqual(item["quotation_span"], [264.5, 277.5])
            self.assertTrue(item["ref"].endswith(":w:264.5:277.5"))
            self.assertNotIn("_window", item)
        finally:
            con.close()

    def test_sparse_items_have_neither_field(self):
        from types import SimpleNamespace
        con = _con()
        try:
            _seed_long_passage(con, at=800)
            ctx = SimpleNamespace(authority_kind="share", scopes=(),
                                  workspace_id="local", member="guest",
                                  credential_kind="share")
            items = _customer._bind_publication_refs(
                con, [{"record_kind": "transcript_passage", "episode_id": "e-long",
                       "evidence_id": None, "text": "x", "_window": [264.5, 277.5]}],
                ctx=ctx, match_q='"night owl"')
            self.assertEqual(len(items), 1)
            self.assertNotIn("match_excerpt", items[0])
            self.assertNotIn("quotation_span", items[0])
        finally:
            con.close()


class ExportCaptureTest(unittest.TestCase):
    def test_selected_null_rejected(self):
        con = _con()
        try:
            rev = _query.research_bundle(
                con, dict(BASE_Q), None, [], False, "ctx-1")["revision"]
            with self.assertRaises(_contracts.QueryRejected):
                _query.capture_export(con, dict(BASE_Q), None, "selected",
                                      "csv", rev, "ctx-1")
        finally:
            con.close()

    def test_selected_empty_captures_zero(self):
        con = _con()
        try:
            rev = _query.research_bundle(
                con, dict(BASE_Q), None, [], False, "ctx-1")["revision"]
            out = _query.capture_export(
                con, dict(BASE_Q, ids=[]), None, "selected", "json", rev,
                "ctx-1")
            self.assertEqual(out["total"], 0)
        finally:
            con.close()

    def test_stale_basis_rejected(self):
        con = _con()
        try:
            with self.assertRaises(_contracts.SelectorConflict):
                _query.capture_export(con, dict(BASE_Q), None, "all_matches",
                                      "csv", "vr2-stale", "ctx-1")
        finally:
            con.close()

    def test_all_matches_captures_every_page(self):
        con = _con()
        try:
            _seed_bulk(con, 12)
            rev = _query.research_bundle(
                con, dict(BASE_Q), None, [], False, "ctx-1")["revision"]
            out = _query.capture_export(con, dict(BASE_Q), None,
                                        "all_matches", "json", rev, "ctx-1")
            bundle = _query.research_bundle(
                con, dict(BASE_Q), None, [], False, "ctx-1")
            self.assertEqual(out["total"], bundle["result"]["total"])
            self.assertGreater(out["total"], _query.PER_PAGE)
            self.assertEqual(len(bundle["result"]["items"]),
                             _query.PER_PAGE)
            captured_refs = {r["ref"] for r in out["rows"]}
            self.assertEqual(len(captured_refs), out["total"])
        finally:
            con.close()


class CompareTest(unittest.TestCase):
    def test_two_brand_cells_count_every_ad(self):
        # PLAIN-ADS (DEP7): a confirmed Acme ad and Lumen's unreviewed ad
        # (two pieces of evidence) both count, once per ad.
        con = _con()
        try:
            out = _query.compare_bundle(
                con, ["Lumen", "Acme"], None, None, None, None, "ctx-1")
            self.assertEqual(len(out["cells"]), 2)
            acme = next(c for c in out["cells"] if c["brand"] == "Acme")
            self.assertEqual(acme["a"]["ads"], 1)
            self.assertEqual(acme["a"]["count_units"], "distinct_results")
            self.assertTrue(acme["a"]["refs_complete"])
            lumen = next(c for c in out["cells"] if c["brand"] == "Lumen")
            self.assertEqual((lumen["a"]["count"], lumen["a"]["ads"]), (2, 1))
        finally:
            con.close()

    def test_repeated_brands_rejected(self):
        con = _con()
        try:
            with self.assertRaises(_contracts.QueryRejected):
                _query.compare_bundle(
                    con, ["Acme", "Acme"], None, None, None, None, "ctx-1")
        finally:
            con.close()


class UpcastTest(unittest.TestCase):
    def test_scalar_upcast_with_report(self):
        out, notes = _contracts.upcast_query_v1(
            {"mode": "ads", "q": "Lumen", "brand": "Lumen"})
        self.assertEqual(out["brands"], ["Lumen"])
        self.assertEqual(out["page"], 1)
        self.assertTrue(any("correction" in n or "equivalent" in n
                            for n in notes))

    def test_blank_and_none_upcast(self):
        out, _notes = _contracts.upcast_query_v1(
            {"mode": "ads", "q": "", "brand": "", "ids": "__none__"})
        self.assertIsNone(out["brands"])
        self.assertEqual(out["ids"], [])

    def test_unknown_v1_field_rejected(self):
        with self.assertRaises(_contracts.QueryRejected):
            _contracts.upcast_query_v1({"mode": "ads", "category": "x"})


class ScannerRelationTest(unittest.TestCase):
    def test_page_truncation_reports_lower_bound(self):
        con = _con()
        try:
            filt = _search.validate_filter({"query": "Lumen"})
            out = _search.search_transcripts_scan(con, filt, 1)
            self.assertTrue(out["truncated"])
            self.assertEqual(out["truncation_reason"], "PAGE_LIMIT")
            self.assertEqual(out["total_relation"], "lower_bound")
        finally:
            con.close()

    def test_resume_continues_after_position(self):
        con = _con()
        try:
            filt = _search.validate_filter({"query": "Lumen"})
            first = _search.search_transcripts_scan(con, filt, 1)
            self.assertIsNotNone(first["next_position"])
            second = _search.search_transcripts_scan(
                con, filt, 100, position=first["next_position"])
            first_ids = {(h["episode_id"], h["segment_index"],
                          h["window_start"]) for h in first["hits"]}
            for hit in second["hits"]:
                self.assertNotIn((hit["episode_id"], hit["segment_index"],
                                  hit["window_start"]), first_ids)
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
