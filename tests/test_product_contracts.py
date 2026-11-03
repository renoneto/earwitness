"""P00 product-contract regressions (stdlib unittest).

Every test fails on a plausible implementation bug: silent filter
broadening, request-body authority forgery, guest overreach, or lossy
migration. No mocks, no source-text inspection.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from earwitness.app import access, citations, contracts, db, search, services


def member_ctx(**kwargs):
    args = {"principal_id": "owner-1", "principal_kind": "owner",
            "workspace_id": "local", "credential_kind": "local_operator",
            "scopes": {"research:read", "transcripts:read"}}
    args.update(kwargs)
    return access.AccessContext.create(**args)


def guest_ctx(**kwargs):
    args = {"principal_id": "guest-1", "principal_kind": "guest",
            "workspace_id": "local", "scopes": {"research:read"},
            "authority_kind": "share", "share_grant_id": "share-1",
            "credential_kind": "browser_session"}
    args.update(kwargs)
    return access.AccessContext.create(**args)


def fresh_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON;")
    db.init_schema(con)
    return con


class StrictFilterTest(unittest.TestCase):
    def test_unknown_field_rejected_not_dropped(self):
        with self.assertRaises(search.FilterRejected):
            search.validate_filter({"query": "shopify", "select": "everything"})

    def test_invalid_enum_rejected_not_broadened(self):
        with self.assertRaises(search.FilterRejected):
            search.validate_filter({"sources": ["invalid"]})
        with self.assertRaises(search.FilterRejected):
            search.validate_filter({"review_states": ["maybe"]})
        with self.assertRaises(search.FilterRejected):
            search.validate_filter({"history": "sometimes"})

    def test_impossible_date_range_rejected(self):
        with self.assertRaises(search.FilterRejected):
            search.validate_filter({"published_from": "2026-05-01",
                                    "published_to": "2026-01-01"})

    def test_bad_date_rejected(self):
        with self.assertRaises(search.FilterRejected):
            search.validate_filter({"published_from": "not-a-date"})

    def test_non_boolean_flag_rejected(self):
        with self.assertRaises(search.FilterRejected):
            search.validate_filter({"include_unknown_dates": "yes"})
        with self.assertRaises(search.FilterRejected):
            search.validate_filter({"pipeline": "true"})

    def test_nan_and_bool_numbers_rejected(self):
        with self.assertRaises(search.FilterRejected):
            search.check_numeric(float("nan"), "max_cost")
        with self.assertRaises(search.FilterRejected):
            search.check_numeric(float("inf"), "max_cost")
        with self.assertRaises(search.FilterRejected):
            search.check_numeric(True, "max_cost")

    def test_conflicting_selectors_rejected(self):
        with self.assertRaises(search.FilterRejected):
            search.validate_filter({"show_id": "abc", "show_query": "abc"})

    def test_over_limit_values_rejected(self):
        with self.assertRaises(search.FilterRejected):
            search.validate_filter({"query": "x" * 201})
        with self.assertRaises(search.FilterRejected):
            search.check_limit(51)
        with self.assertRaises(search.FilterRejected):
            search.check_limit(0)

    def test_valid_filter_passes_with_echo(self):
        filt = search.validate_filter({"query": "Shopify", "sources": ["jev"],
                                       "history": "active"})
        self.assertEqual(filt["query"], "Shopify")
        self.assertEqual(filt["sources"], ["jev"])
        self.assertTrue(filt["include_unknown_dates"])

    def test_literal_substring_preserved(self):
        # Grammar characters are literal text, not patterns.
        self.assertTrue(search.literal_contains("save 20% today", "20%"))
        self.assertFalse(search.literal_contains("save 20 today", "20%"))
        self.assertFalse(search.literal_contains("ax", ".*"))
        self.assertTrue(search.literal_contains("SHOPIFY store", "shopify"))

    def test_legacy_filter_drops_nothing_silently(self):
        # v4-only selectors cannot ride the legacy path.
        filt = search.validate_filter({"record_kind": "observation"})
        with self.assertRaises(search.FilterRejected):
            search.to_legacy_filter(filt)


class AccessContextTest(unittest.TestCase):
    def test_request_body_cannot_mint_context(self):
        body = {"principal_id": "owner-1", "principal_kind": "owner",
                "workspace_id": "local", "scopes": ["research:read"]}
        with self.assertRaises(TypeError):
            access.AccessContext.from_request_body(body)

    def test_direct_construction_without_server_token_refused(self):
        with self.assertRaises(TypeError):
            access.AccessContext(principal_id="owner-1", principal_kind="owner",
                                 workspace_id="local")

    def test_server_created_context_works(self):
        ctx = member_ctx()
        self.assertEqual(ctx.authority_kind, "membership")
        self.assertIn("research:read", ctx.scopes)

    def test_share_authority_requires_grant(self):
        with self.assertRaises(ValueError):
            access.AccessContext.create(principal_id="g", principal_kind="guest",
                                        workspace_id="local", authority_kind="share",
                                        credential_kind="browser_session")

    def test_budget_cancel_and_expiry(self):
        budget = access.ExecutionBudget(deadline_s=30.0)
        budget.check("probe")
        budget.cancel()
        with self.assertRaises(TimeoutError):
            budget.check("probe")
        spent = access.ExecutionBudget(deadline_s=0.0001)
        import time
        time.sleep(0.001)
        with self.assertRaises(TimeoutError):
            spent.check("probe")


class ShareGrantTest(unittest.TestCase):
    def test_guest_denied_unlisted_operation(self):
        ctx = guest_ctx()
        with self.assertRaises(access.AuthorizationError) as caught:
            ctx.require_operation("save_observation")
        self.assertEqual(caught.exception.status, 403)

    def test_guest_denied_workspace_traversal(self):
        ctx = guest_ctx()
        for op in ("create_share", "submit_processing", "research_overview"):
            with self.assertRaises(access.AuthorizationError, msg=op):
                ctx.require_operation(op)

    def test_guest_allowed_listed_read(self):
        ctx = guest_ctx()
        ctx.require_operation("search_evidence")  # must not raise

    def test_guest_stage_write_denied_through_service(self):
        ctx = guest_ctx(scopes={"research:read"})
        con = fresh_db()
        try:
            with self.assertRaises(access.AuthorizationError):
                services.save_observation(ctx, con)
        finally:
            con.close()

    def test_member_search_runs_on_empty_workspace(self):
        ctx = member_ctx()
        con = fresh_db()
        try:
            page = services.search_evidence(ctx, con, {"query": "shopify"})
            self.assertEqual(page["total_matches"], 0)
            self.assertEqual(page["applied_filter"]["query"], "shopify")
            self.assertTrue(page["view_revision"])
        finally:
            con.close()

    def test_error_mapping(self):
        status, _ = contracts.error_for(search.FilterRejected("bad"))
        self.assertEqual(status, 400)
        from earwitness.app import ops
        status, _ = contracts.error_for(ops.Conflict(3))
        self.assertEqual(status, 412)
        status, _ = contracts.error_for(ops.MissingPrecondition("rev"))
        self.assertEqual(status, 428)
        status, _ = contracts.error_for(KeyError("nope"))
        self.assertEqual(status, 404)


def _seed_shows(con):
    """Two episodes sharing kind+title (one stable show) + one outsider."""
    for ep in ("ep-A", "ep-B"):
        con.execute("INSERT INTO episodes (id, kind, show_title, fetch_date, updated_at)"
                    " VALUES (?, 'supplied', 'Morning Show', '2026-01-02',"
                    " '2026-01-02T00:00:00+00:00')", (ep,))
        con.execute("INSERT INTO evidence (id, source, episode_id, excerpt, created_at)"
                    " VALUES (?, 'manual', ?, ?, '2026-01-02T00:00:00+00:00')",
                    (f"ev-{ep}", ep, f"mention in {ep}"))
    con.execute("INSERT INTO episodes (id, kind, show_title, fetch_date, updated_at)"
                " VALUES ('ep-C', 'supplied', 'Other Show', '2026-01-02',"
                " '2026-01-02T00:00:00+00:00')")
    con.execute("INSERT INTO evidence (id, source, episode_id, excerpt, created_at)"
                " VALUES ('ev-ep-C', 'manual', 'ep-C', 'other mention',"
                " '2026-01-02T00:00:00+00:00')")
    con.commit()
    row = con.execute("SELECT id, kind, show_title FROM episodes"
                      " WHERE id='ep-A'").fetchone()
    return search.show_identity(dict(row))["show_id"]


class ShowIdServingTest(unittest.TestCase):
    """F4-N1: a valid show_id filter serves show-scoped results (not FilterRejected)."""

    def test_valid_show_id_returns_only_show_evidence(self):
        con = fresh_db()
        try:
            sid = _seed_shows(con)
            page = services.search_evidence(member_ctx(), con,
                                            {"query": "", "show_id": sid})
            self.assertEqual(sorted(i["id"] for i in page["items"]),
                             ["ev-ep-A", "ev-ep-B"])
            self.assertEqual(page["total_matches"], 2)
            self.assertEqual(page["applied_filter"]["show_id"], sid)
        finally:
            con.close()

    def test_unknown_show_id_stays_rejected(self):
        con = fresh_db()
        try:
            _seed_shows(con)
            with self.assertRaises(search.FilterRejected):
                services.search_evidence(member_ctx(), con,
                                         {"query": "", "show_id": "sh-deadbeef0000"})
            status, _ = contracts.error_for(
                search.FilterRejected("unknown show: 'sh-deadbeef0000'"))
            self.assertEqual(status, 400)
        finally:
            con.close()

    def test_show_id_pages_through_cursor(self):
        from earwitness.app import indexing as _indexing
        con = fresh_db()
        try:
            sid = _seed_shows(con)
            _indexing.rebuild_all(con)
            first = services.search_evidence(member_ctx(), con,
                                             {"query": "", "show_id": sid}, limit=1)
            self.assertEqual(len(first["items"]), 1)
            self.assertEqual(first["total_matches"], 2)
            self.assertIsNotNone(first["next_cursor"])
            second = services.search_evidence(member_ctx(), con,
                                              {"query": "", "show_id": sid}, limit=1,
                                              cursor=first["next_cursor"])
            self.assertEqual(len(second["items"]), 1)
            self.assertNotEqual(first["items"][0]["id"], second["items"][0]["id"])
            self.assertIsNone(second["next_cursor"])
        finally:
            con.close()


class TranscriptWindowTruncationTest(unittest.TestCase):
    """F4-N2: transcript_window clips at 100 segments with total + flag + cursor."""

    def _seed_segments(self, con, n, ep="ep-1"):
        import json as _json
        segs = [{"start": float(i), "end": float(i + 1),
                 "text": f"segment {i} text here"} for i in range(n)]
        con.execute("INSERT INTO episodes (id, kind, fetch_date, updated_at)"
                    " VALUES (?, 'supplied', '2026-01-02', '2026-01-02T00:00:00+00:00')",
                    (ep,))
        cur = con.execute("INSERT INTO source_versions (source_key, version_no, sha256,"
                          " payload, is_active, created_at)"
                          " VALUES (?, 1, 'abc', ?, 1, ?)",
                          (f"transcript:{ep}", _json.dumps({"segments": segs}),
                           db.utcnow()))
        con.execute("UPDATE episodes SET transcript_version_id=? WHERE id=?",
                    (cur.lastrowid, ep))
        con.commit()

    def test_overlong_episode_reports_truncation(self):
        con = fresh_db()
        try:
            self._seed_segments(con, 150)
            window = services.transcript_window(member_ctx(), con, "ep-1")
            self.assertEqual(len(window["segments"]), 100)
            self.assertEqual(window["total_segments"], 150)
            self.assertTrue(window["truncated"])
            self.assertEqual(window["next_offset"], 100)
        finally:
            con.close()

    def test_short_episode_not_truncated(self):
        con = fresh_db()
        try:
            self._seed_segments(con, 3)
            window = services.transcript_window(member_ctx(), con, "ep-1")
            self.assertEqual(len(window["segments"]), 3)
            self.assertEqual(window["total_segments"], 3)
            self.assertFalse(window["truncated"])
            self.assertIsNone(window["next_offset"])
        finally:
            con.close()


class MigrationRoundTripTest(unittest.TestCase):
    def _seed_legacy(self, con):
        con.execute("INSERT INTO episodes (id, kind, fetch_date, updated_at)"
                    " VALUES ('ep-1', 'supplied', '2026-01-02', '2026-01-02T00:00:00+00:00')")
        con.execute("INSERT INTO evidence (id, source, episode_id, excerpt, created_at)"
                    " VALUES ('ev-1', 'manual', 'ep-1', 'shopify mention',"
                    " '2026-01-02T00:00:00+00:00')")
        con.execute("INSERT INTO reviews (evidence_id, judgment, updated_at)"
                    " VALUES ('ev-1', 'ad', '2026-01-03T00:00:00+00:00')")
        con.commit()

    def test_up_down_up_round_trip_preserves_legacy_rows(self):
        con = fresh_db()
        try:
            self._seed_legacy(con)
            before = db.table_counts(con)
            self.assertEqual(db._schema_version(con), 8)
            self.assertIn("ad_observations", before)
            self.assertIn("show_artwork", before)
            db.migrate_8_to_7(con)
            db.migrate_7_to_6(con)
            db.migrate_6_to_5(con)
            db.migrate_5_to_4(con)
            db.migrate_down(con)
            self.assertEqual(db._schema_version(con), 3)
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("ad_observations", tables)
            self.assertNotIn("show_artwork", tables)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM evidence").fetchone()[0],
                             before["evidence"])
            self.assertEqual(con.execute("SELECT COUNT(*) FROM reviews").fetchone()[0],
                             before["reviews"])
            self.assertEqual(con.execute("SELECT judgment FROM reviews"
                                         " WHERE evidence_id='ev-1'").fetchone()[0], "ad")
            cols = {r["name"] for r in con.execute("PRAGMA table_info(evidence)")}
            self.assertNotIn("record_kind", cols)
            db.migrate_up(con)
            db.migrate_4_to_5(con)
            db.migrate_5_to_6(con)
            db.migrate_6_to_7(con)
            db.migrate_7_to_8(con)
            self.assertEqual(db._schema_version(con), 8)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM evidence").fetchone()[0],
                             before["evidence"])
            self.assertEqual(con.execute("SELECT record_kind FROM evidence"
                                         " WHERE id='ev-1'").fetchone()[0], "candidate")
            self.assertEqual(db.verify_schema(con)["problems"], [])
        finally:
            con.close()

    def test_down_migration_refuses_with_extension_data(self):
        con = fresh_db()
        try:
            self._seed_legacy(con)
            con.execute("INSERT INTO processing_runs (id, created_at, updated_at)"
                        " VALUES ('run-1', '2026-01-02T00:00:00+00:00',"
                        " '2026-01-02T00:00:00+00:00')")
            con.commit()
            with self.assertRaises(RuntimeError):
                db.migrate_down(con)
            # Failed downgrade leaves the v8 workspace intact.
            self.assertEqual(db._schema_version(con), 8)
        finally:
            con.close()

    def test_init_schema_idempotent_on_v4_workspace(self):
        # Server boot and status call init_schema on every start; a migrated
        # workspace must open without rebuild errors or row loss.
        con = fresh_db()
        try:
            self._seed_legacy(con)
            db.init_schema(con)
            db.init_schema(con)
            self.assertEqual(db._schema_version(con), 8)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 1)
            self.assertEqual(db.verify_schema(con)["problems"], [])
        finally:
            con.close()

    def test_record_kind_constraint_enforced(self):
        con = fresh_db()
        try:
            self._seed_legacy(con)
            with self.assertRaises(Exception):
                with con:
                    con.execute("UPDATE evidence SET record_kind='ad' WHERE id='ev-1'")
        finally:
            con.close()

    def test_rehearsal_on_disposable_file_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workspace.sqlite3"
            src = sqlite3.connect(str(path))
            src.row_factory = sqlite3.Row
            db.init_schema(src)
            self._seed_legacy(src)
            src.close()
            work = sqlite3.connect(str(path))
            work.row_factory = sqlite3.Row
            try:
                before = db.table_counts(work)
                db.migrate_8_to_7(work)
                db.migrate_7_to_6(work)
                db.migrate_6_to_5(work)
                db.migrate_5_to_4(work)
                db.migrate_down(work)
                db.migrate_up(work)
                db.migrate_4_to_5(work)
                db.migrate_5_to_6(work)
                db.migrate_6_to_7(work)
                db.migrate_7_to_8(work)
                after = db.table_counts(work)
                self.assertEqual(before["evidence"], after["evidence"])
                self.assertEqual(db.verify_schema(work)["problems"], [])
            finally:
                work.close()


class TranscriptWindowOffsetTest(unittest.TestCase):
    """R3-1 minor: transcript_window next_offset is consumable via offset.

    Before: the window advertised next_offset=100 but accepted no offset
    argument, so no caller could page. After: offset (default 0,
    behavior-preserving) pages finite-clamped windows with an updated
    cursor; member and guest authorization paths are unchanged.
    """

    def _seed_segments(self, con, n, ep="ep-1"):
        import json as _json
        segs = [{"start": float(i), "end": float(i + 1),
                 "text": f"segment {i} text here"} for i in range(n)]
        con.execute("INSERT INTO episodes (id, kind, fetch_date, updated_at)"
                    " VALUES (?, 'supplied', '2026-01-02', '2026-01-02T00:00:00+00:00')",
                    (ep,))
        cur = con.execute("INSERT INTO source_versions (source_key, version_no, sha256,"
                          " payload, is_active, created_at)"
                          " VALUES (?, 1, 'abc', ?, 1, ?)",
                          (f"transcript:{ep}", _json.dumps({"segments": segs}),
                           db.utcnow()))
        con.execute("UPDATE episodes SET transcript_version_id=? WHERE id=?",
                    (cur.lastrowid, ep))
        con.commit()

    def test_offset_pages_second_window(self):
        con = fresh_db()
        try:
            self._seed_segments(con, 150)
            first = services.transcript_window(member_ctx(), con, "ep-1")
            self.assertEqual(first["next_offset"], 100)
            second = services.transcript_window(member_ctx(), con, "ep-1",
                                                offset=100)
            self.assertEqual(len(second["segments"]), 50)
            self.assertEqual(second["segments"][0]["text"],
                             "segment 100 text here")
            self.assertEqual(second["total_segments"], 150)
            self.assertFalse(second["truncated"])
            self.assertIsNone(second["next_offset"])
        finally:
            con.close()

    def test_offset_cursor_chains_across_windows(self):
        con = fresh_db()
        try:
            self._seed_segments(con, 250)
            w0 = services.transcript_window(member_ctx(), con, "ep-1")
            self.assertEqual(w0["next_offset"], 100)
            w1 = services.transcript_window(member_ctx(), con, "ep-1",
                                            offset=w0["next_offset"])
            self.assertEqual(len(w1["segments"]), 100)
            self.assertEqual(w1["next_offset"], 200)
            w2 = services.transcript_window(member_ctx(), con, "ep-1",
                                            offset=w1["next_offset"])
            self.assertEqual(len(w2["segments"]), 50)
            self.assertIsNone(w2["next_offset"])
        finally:
            con.close()

    def test_default_offset_zero_is_behavior_preserving(self):
        con = fresh_db()
        try:
            self._seed_segments(con, 150)
            default = services.transcript_window(member_ctx(), con, "ep-1")
            explicit = services.transcript_window(member_ctx(), con, "ep-1",
                                                  offset=0)
            self.assertEqual(default["segments"], explicit["segments"])
            self.assertEqual(default["next_offset"], 100)
            negative = services.transcript_window(member_ctx(), con, "ep-1",
                                                  offset=-5)
            self.assertEqual(negative["segments"], default["segments"])
            past_end = services.transcript_window(member_ctx(), con, "ep-1",
                                                  offset=9999)
            self.assertEqual(past_end["segments"], [])
            self.assertFalse(past_end["truncated"])
            self.assertIsNone(past_end["next_offset"])
        finally:
            con.close()

    def test_non_finite_offset_rejected(self):
        con = fresh_db()
        try:
            self._seed_segments(con, 150)
            for bad in (True, "100", 1.5, float("inf"), float("nan"), None):
                with self.assertRaises(contracts.ContractError, msg=repr(bad)):
                    services.transcript_window(member_ctx(), con, "ep-1",
                                               offset=bad)
            integral = services.transcript_window(member_ctx(), con, "ep-1",
                                                  offset=100.0)
            self.assertEqual(len(integral["segments"]), 50)
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
