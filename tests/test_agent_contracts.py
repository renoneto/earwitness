"""A-delta agent-contract regressions (stdlib unittest).

Every test fails on a plausible implementation bug: silent filter
broadening, request-body authority forgery, unauthorized field leakage,
lost saved-search meaning, or envelope/cursor contract drift. No mocks,
no source-text inspection; disposable in-memory workspaces only.
"""
from __future__ import annotations

import json
import sqlite3
import unittest

from earwitness.app import access, agent_contracts as ac, db, ops, search, services


def member_ctx(**kwargs):
    args = {"principal_id": "owner-1", "principal_kind": "owner",
            "workspace_id": "test-ws", "credential_kind": "local_reader",
            "scopes": {"research:read", "transcripts:read", "watchlists:read",
                       "campaigns:read", "notes:read"}}
    args.update(kwargs)
    return access.AccessContext.create(**args)


def guest_ctx(**kwargs):
    args = {"principal_id": "guest-1", "principal_kind": "guest",
            "workspace_id": "test-ws", "scopes": {"research:read"},
            "authority_kind": "share", "share_grant_id": "share-1",
            "credential_kind": "browser_session"}
    args.update(kwargs)
    return access.AccessContext.create(**args)


from contextlib import contextmanager
from pathlib import Path
import tempfile


@contextmanager
def bound_workspace(con):
    """Materialize a seeded in-memory DB as a bound disposable workspace dir."""
    with tempfile.TemporaryDirectory() as tmp:
        dest = sqlite3.connect(str(Path(tmp) / "workspace.sqlite3"))
        try:
            con.backup(dest)
        finally:
            dest.close()
        from earwitness.app import connections
        setup = db.open_existing(Path(tmp) / "workspace.sqlite3", readonly=False)
        try:
            setup.execute("INSERT OR REPLACE INTO meta (key,value) VALUES ('agent_profile',?)",
                          (json.dumps({"profile": "reader", "owner": "owner-1",
                                       "workspace_id": "test-ws",
                                       "scopes": sorted(member_ctx().scopes)}),))
            setup.commit()
        finally:
            setup.close()
        connections.prepare_local_reader_control(tmp, "test-ws",
                                                  verification_context="local_fixture")
        ac.bind_workspace("test-ws", tmp)
        try:
            yield tmp
        finally:
            ac._WORKSPACES.pop("test-ws", None)


def seed_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    db.init_schema(con)
    con.execute("INSERT INTO source_versions (id, source_key, version_no, sha256,"
                " payload, is_active, created_at) VALUES (1, 'transcript:ep-1', 1,"
                " 'abc', ?, 1, '2026-01-02T00:00:00+00:00')",
                (json.dumps({"segments": [
                    {"start": 0.0, "end": 12.0, "text": "welcome, shopify mentioned"},
                    {"start": 12.0, "end": 30.0, "text": "more talk here"}]}),))
    con.execute("INSERT INTO source_versions (id, source_key, version_no, sha256,"
                " payload, is_active, created_at) VALUES (2, 'transcript:ep-2', 1,"
                " 'def', ?, 1, '2026-01-03T00:00:00+00:00')",
                (json.dumps({"segments": [
                    {"start": 0.0, "end": 8.0, "text": "unrelated chatter"}]}),))
    con.execute("INSERT INTO episodes (id, kind, show_title, ep_title, pub_date,"
                " fetch_date, transcript_version_id, updated_at) VALUES ('ep-1',"
                " 'supplied', 'Show A', 'Episode One', '2026-01-02', '2026-01-02',"
                " 1, '2026-01-02T00:00:00+00:00')")
    con.execute("INSERT INTO episodes (id, kind, show_title, ep_title, pub_date,"
                " fetch_date, transcript_version_id, updated_at) VALUES ('ep-2',"
                " 'supplied', 'Show B', 'Episode Two', '', '2026-01-03',"
                " 2, '2026-01-03T00:00:00+00:00')")
    con.execute("INSERT INTO evidence (id, source, episode_id, orig_start_s,"
                " orig_end_s, excerpt, attrs, transcript_version_id, created_at)"
                " VALUES ('ev-1', 'jev', 'ep-1', 0.0, 12.0, 'shopify mention',"
                " '{}', 1, '2026-01-02T00:00:00+00:00')")
    con.execute("INSERT INTO evidence (id, source, episode_id, orig_start_s,"
                " orig_end_s, excerpt, attrs, transcript_version_id, created_at)"
                " VALUES ('ev-2', 'detector', 'ep-2', 0.0, 8.0, 'unrelated bit',"
                " '{}', 2, '2026-01-03T00:00:00+00:00')")
    con.execute("INSERT INTO source_versions (id, source_key, version_no, sha256,"
                " payload, is_active, created_at) VALUES (3, 'jev-run', 1,"
                " '789', '{}', 1, '2026-01-02T00:00:00+00:00')")
    con.execute("UPDATE evidence SET source_version_id=3 WHERE id='ev-1'")
    con.execute("INSERT INTO reviews (evidence_id, judgment, eff_start_s,"
                " eff_end_s, notes, revision, updated_at) VALUES ('ev-1', 'ad',"
                " 0.0, 12.0, 'private-note', 1, '2026-01-04T00:00:00+00:00')")
    con.execute("INSERT INTO brand_assocs (evidence_id, brand, basis, origin,"
                " revision, updated_at, channel) VALUES ('ev-1', 'shopify',"
                " 'confirmed', 'human', 1, '2026-01-04T00:00:00+00:00', 'spoken')")
    con.execute("INSERT INTO brand_assocs (evidence_id, brand, basis, origin,"
                " revision, updated_at, channel) VALUES ('ev-2', 'shopify',"
                " 'suggested', 'human', 1, '2026-01-04T00:00:00+00:00', 'url')")
    con.commit()
    out = ops.save_watchlist(con, {"name": "good",
                                   "query": {"q": "shopify", "source": ["jev"]}},
                             None, None, "owner")
    con.execute("INSERT INTO watchlists (name, query, revision, updated_at)"
                " VALUES ('bad', ?, 1, '2026-01-02T00:00:00+00:00')",
                ('{"source": ["invalid"], "q": "shopify"}',))
    con.execute("INSERT INTO campaigns (id, name, status, brand, target_shows,"
                " message, notes, revision, updated_at) VALUES (1, 'Camp',"
                " 'active', 'shopify', '[]', 'brief text', 'campaign note', 1,"
                " '2026-01-02T00:00:00+00:00')")
    con.execute("INSERT INTO campaign_evidence VALUES (1, 'ev-1')")
    con.execute("INSERT INTO campaign_checks (campaign_id, check_type, evidence_id,"
                " params, updated_at) VALUES (1, 'confirmed_brand', 'ev-1',"
                " '{\"brand\": \"shopify\"}', '2026-01-02T00:00:00+00:00')")
    con.commit()
    from tests.backend_fixtures import publish_all_records
    publish_all_records(con)
    return con, out["id"]


class StrictRejectsTest(unittest.TestCase):
    def test_invalid_restrictive_source_rejects_everywhere(self):
        with self.assertRaises(ac.AgentError):
            ac.parse_search_evidence({"filter": {"sources": ["invalid"]}})
        with self.assertRaises(ac.AgentError):
            ac.parse_browser_evidence_params({"source": ["invalid"]})
        with self.assertRaises(ac.AgentError):
            ac.require_saved_meaning({"source": ["invalid"]}, "export")

    def test_agent_supplied_actor_rejected_on_all_tools(self):
        for tool, parse in ac.REQUEST_PARSERS.items():
            with self.assertRaises(ac.AgentError, msg=tool):
                parse({"actor": "owner"})
        with self.assertRaises(ac.AgentError):
            ac.parse_browser_evidence_params({"actor": "owner"})
        with self.assertRaises(ac.AgentError):
            ac.parse_search_evidence({"filter": {}, "workspace_id": "local"})

    def test_unknown_field_invalid_enum_nan_bad_dates_reject(self):
        with self.assertRaises(ac.AgentError):
            ac.parse_search_evidence({"filter": {"query": "x", "select": "all"}})
        with self.assertRaises(ac.AgentError):
            ac.parse_search_evidence({"filter": {"review_states": ["maybe"]}})
        with self.assertRaises(ac.AgentError):
            ac.parse_get_transcript_window({"episode_id": "ep-1",
                                            "transcript_version_id": 1,
                                            "start_s": float("nan"),
                                            "end_s": 10.0})
        with self.assertRaises(ac.AgentError):
            ac.parse_get_transcript_window({"episode_id": "ep-1",
                                            "transcript_version_id": 1,
                                            "start_s": True, "end_s": 10.0})
        with self.assertRaises(ac.AgentError):
            ac.parse_search_evidence({"filter": {"published_from": "2026-05-01",
                                                 "published_to": "2026-01-01"}})
        with self.assertRaises(ac.AgentError):
            ac.parse_list_brands({"limit": 51})
        with self.assertRaises(ac.AgentError):
            ac.parse_get_watchlist_results({"watchlist_id": "abc"})

    def test_window_and_id_bounds(self):
        with self.assertRaises(ac.AgentError):
            ac.parse_get_transcript_window({"episode_id": "ep-1",
                                            "transcript_version_id": None,
                                            "start_s": 0, "end_s": 10.0})
        with self.assertRaises(ac.AgentError):
            ac.parse_get_transcript_window({"episode_id": "ep-1",
                                            "transcript_version_id": 1,
                                            "start_s": 0, "end_s": 301.0})
        with self.assertRaises(ac.AgentError):
            ac.parse_get_source_provenance({"evidence_id": "ev-1",
                                            "source_version_id": 2})
        with self.assertRaises(ac.AgentError):
            ac.parse_get_source_provenance({})

    def test_supplied_cursor_rejects_until_package_b(self):
        with self.assertRaises(ac.AgentError):
            ac.parse_search_evidence({"filter": {}, "cursor": "opaque"})


class ConformanceTest(unittest.TestCase):
    def test_section6_fields_accepted_with_echo(self):
        filt = ac.parse_filter({
            "query": "Shopify", "sources": ["jev", "detector", "manual"],
            "review_states": ["ad"], "brand": "shopify",
            "brand_basis": ["confirmed"], "brand_channel": ["spoken"],
            "show_query": "Show", "episode_id": "ep-1", "category": "c",
            "published_from": "2026-01-01", "published_to": "2026-02-01",
            "source_version_id": "", "history": "all", "sort": "published",
        }, "probe")
        self.assertTrue(filt["include_unknown_dates"])
        self.assertEqual(filt["sort"], "published")
        self.assertEqual(filt["brand_channel"], ["spoken"])

    def test_show_id_rejects_through_legacy_service_path(self):
        con, _ = seed_db()
        try:
            ctx = member_ctx()
            with self.assertRaises(search.FilterRejected):
                search.to_legacy_filter(search.validate_filter(
                    {"show_id": "sh-1"}))
            # Package B resolved stable show identity for the entity paths:
            # the filter parses, and unknown ids reject at resolution.
            parsed = ac.parse_list_episodes({"show_id": "sh-1"})
            self.assertEqual(parsed["filter"]["show_id"], "sh-1")
            with self.assertRaises(search.FilterRejected):
                search.resolve_show_id(con, "sh-1")
        finally:
            con.close()

    def test_brand_channel_and_published_sort_execute(self):
        con, _ = seed_db()
        try:
            spoken = ops.search_evidence(
                con, {**search.to_legacy_filter(search.validate_filter(
                    {"brand_channel": ["spoken"]})), "page": 1, "per_page": 50})
            self.assertEqual([i["id"] for i in spoken["items"]], ["ev-1"])
            ordered = ops.search_evidence(
                con, {**search.to_legacy_filter(search.validate_filter(
                    {"sort": "published"})), "page": 1, "per_page": 50})
            self.assertEqual([i["id"] for i in ordered["items"]],
                             ["ev-1", "ev-2"])
        finally:
            con.close()

    def test_filter_version_pin(self):
        ok = search.validate_filter({"version": 1, "query": "x"})
        self.assertEqual(ok["query"], "x")
        with self.assertRaises(search.FilterRejected):
            search.validate_filter({"version": 2, "query": "x"})

    def test_source_version_id_preserved_not_dropped(self):
        con, _ = seed_db()
        try:
            filt = search.validate_filter({"source_version_id": "3"})
            legacy = search.to_legacy_filter(filt)
            self.assertEqual(legacy["source_version"], "3")
            res = ops.search_evidence(con, {**legacy, "page": 1,
                                            "per_page": 50})
            self.assertEqual({i["id"] for i in res["items"]}, {"ev-1"})
        finally:
            con.close()


class SameIdsAcrossEntrypointsTest(unittest.TestCase):
    def test_same_filter_same_ids(self):
        con, _ = seed_db()
        try:
            with bound_workspace(con):
                ctx = member_ctx()
                agent_page = ac.search_evidence(
                    ctx, {"filter": {"query": "shopify"}, "limit": 20})
                agent_ids = [i["id"] for i in agent_page["items"]]
                svc = services.search_evidence(ctx, con, {"query": "shopify"},
                                               20)
                svc_ids = [i["id"] for i in svc["items"]]
                direct = ops.search_evidence(con, {"q": "shopify", "page": 1,
                                                  "per_page": 50})
                direct_ids = [i["id"] for i in direct["items"]]
                gated = ops.search_evidence(
                    con, ac.parse_browser_evidence_params({"q": "shopify"}))
                gated_ids = [i["id"] for i in gated["items"]]
                self.assertEqual(agent_ids, ["ev-1", "ev-2"])
                self.assertEqual(svc_ids, agent_ids)
                self.assertEqual(direct_ids, agent_ids)
                self.assertEqual(gated_ids, agent_ids)
        finally:
            con.close()


class UnauthorizedFieldsTest(unittest.TestCase):
    def test_evidence_projection_hides_internals(self):
        con, _ = seed_db()
        try:
            con.execute("UPDATE evidence SET attrs=?, revision=revision+1 WHERE id='ev-1'",
                        ('{"internal": true}',))
            db.record_evidence_snapshot(con, "ev-1", "fixture-owner")
            con.commit()
            from tests.backend_fixtures import publish_all_records
            publish_all_records(con)
            with bound_workspace(con):
                ctx = member_ctx(scopes={"research:read"})
                page = ac.search_evidence(ctx, {"filter": {}, "limit": 20})
                item = page["items"][0]
                for forbidden in ("attrs", "audio_url", "media_id", "payload",
                                  "notes"):
                    self.assertNotIn(forbidden, json.dumps(item))
                detail = ac.get_evidence(ctx, {"evidence_id": "ev-1"})
                data = detail["items"][0]
                self.assertNotIn("notes", json.dumps(data["review"]))
                self.assertNotIn("audio_url", json.dumps(data))
        finally:
            con.close()

    def test_notes_visible_only_with_scope(self):
        con, _ = seed_db()
        try:
            with bound_workspace(con):
                noted = ac.get_evidence(member_ctx(), {"evidence_id": "ev-1"})
                self.assertEqual(noted["items"][0]["review"]["notes"],
                                 "private-note")
                plain = ac.get_evidence(member_ctx(scopes={"research:read"}),
                                        {"evidence_id": "ev-1"})
                self.assertNotIn("notes", plain["items"][0]["review"])
                campaign = ac.get_campaign(
                    member_ctx(scopes={"campaigns:read", "research:read"}),
                    {"campaign_id": 1})
                attachment = campaign["items"][0]["attachments"][0]
                self.assertNotIn("notes", attachment["review"])
                self.assertEqual(campaign["items"][0]["brief"]["notes"],
                                 "campaign note")
        finally:
            con.close()

    def test_guest_cannot_use_agent_tools(self):
        con, _ = seed_db()
        try:
            with bound_workspace(con):
                with self.assertRaises(access.AuthorizationError):
                    ac.search_evidence(guest_ctx(), {"filter": {}})
                with self.assertRaises(access.AuthorizationError):
                    ac.get_workspace_context(guest_ctx(), {})
        finally:
            con.close()


class EnvelopeCursorErrorTest(unittest.TestCase):
    def test_envelope_shape_and_citations(self):
        con, _ = seed_db()
        try:
            with bound_workspace(con):
                page = ac.search_evidence(member_ctx(),
                                          {"filter": {"query": "shopify"}})
                for key in ("schema_version", "request_id", "workspace_id",
                            "view_revision", "as_of", "applied_filter", "items",
                            "returned_count", "total_matches", "total_relation",
                            "next_cursor", "truncated", "coverage", "limitations",
                            "citations"):
                    self.assertIn(key, page, key)
                self.assertEqual(page["workspace_id"], "test-ws")
                self.assertEqual(page["applied_filter"]["query"], "shopify")
                self.assertTrue(page["view_revision"])
                self.assertEqual(len(page["citations"]), len(page["items"]))
                self.assertIn("CANDIDATES_NOT_UNIQUE_ADS", page["limitations"])
        finally:
            con.close()

    def test_error_vocabulary_maps_identically(self):
        status, code, _ = ac.error_for_agent(search.FilterRejected("bad"))
        self.assertEqual((status, code), (400, ac.INVALID_ARGUMENT))
        status, code, _ = ac.error_for_agent(
            access.AuthorizationError("denied", 403))
        self.assertEqual((status, code), (403, ac.FORBIDDEN))
        status, code, _ = ac.error_for_agent(ops.Conflict(3))
        self.assertEqual((status, code), (412, ac.REVISION_CONFLICT))
        status, code, _ = ac.error_for_agent(ops.MissingPrecondition("rev"))
        self.assertEqual((status, code), (428, ac.PRECONDITION_REQUIRED))
        status, code, _ = ac.error_for_agent(KeyError("nope"))
        self.assertEqual((status, code), (404, ac.NOT_FOUND))
        status, code, _ = ac.error_for_agent(TimeoutError("slow"))
        self.assertEqual((status, code), (503, ac.QUERY_TIMEOUT))
        err = ac.AgentError(ac.RESULT_SET_CHANGED, "changed")
        self.assertEqual((err.status, err.code), (409, ac.RESULT_SET_CHANGED))
        self.assertEqual(ac.AgentError(ac.RESULT_TOO_LARGE, "big").status, 413)
        self.assertEqual(ac.AgentError(ac.RATE_LIMITED, "fast").status, 429)

    def test_cursor_round_trip_expiry_and_forgery(self):
        secret = b"test-secret-16byte"
        token = ac.encode_cursor(secret, {"workspace_id": "w",
                                          "filter_digest": "abc",
                                          "view_revision": "v1-x",
                                          "sort_key": "episode", "page_size": 20})
        body = ac.decode_cursor(secret, token)
        self.assertEqual(body["workspace_id"], "w")
        self.assertEqual(body["sort_key"], "episode")
        with self.assertRaises(ac.AgentError) as expired:
            ac.decode_cursor(secret, token, now=body["exp"] + 1)
        self.assertEqual(expired.exception.code, ac.CURSOR_EXPIRED)
        with self.assertRaises(ac.AgentError) as forged:
            ac.decode_cursor(secret, token[:-2] + ("00" if token[-2:] != "00"
                                                   else "11"))
        self.assertEqual(forged.exception.code, ac.INVALID_ARGUMENT)
        with self.assertRaises(ac.AgentError):
            ac.decode_cursor(secret, "not-a-cursor")

    def test_tool_schemas_are_closed_objects(self):
        self.assertEqual(set(ac.TOOL_SCHEMAS), set(ac.REQUEST_PARSERS))
        self.assertEqual(set(ac.AGENT_ENTRYPOINTS), set(ac.REQUEST_PARSERS))
        for tool, schema in ac.TOOL_SCHEMAS.items():
            self.assertFalse(schema["additionalProperties"], tool)
            self.assertEqual(schema["type"], "object", tool)


class WatchlistGateTest(unittest.TestCase):
    def test_valid_legacy_meaning_preserved(self):
        con, _ = seed_db()
        try:
            direct = ops.search_evidence(con, {"q": "shopify", "source": ["jev"],
                                               "page": 1, "per_page": 1})
            out = ops.evaluate_watchlist(con, 1)
            self.assertEqual(out["match_count"], direct["total"])
        finally:
            con.close()

    def test_malformed_saved_search_refuses_with_reasons(self):
        con, _ = seed_db()
        try:
            with self.assertRaises(ops.NeedsCorrection) as caught:
                ops.evaluate_watchlist(con, 2)
            self.assertTrue(caught.exception.reasons)
            with self.assertRaises(ac.AgentError) as agent:
                with bound_workspace(con):
                    ac.get_watchlist_results(
                        member_ctx(), {"watchlist_id": 2})
            self.assertIn("needs_correction", str(agent.exception))
        finally:
            con.close()

    def test_save_refuses_malformed_query(self):
        con, _ = seed_db()
        try:
            with self.assertRaises(ops.BadInput):
                ops.save_watchlist(con, {"name": "x",
                                         "query": {"source": ["invalid"]}},
                                   None, None, "owner")
            kept = ops.save_watchlist(con, {"name": "y",
                                            "query": {"q": "shopify"}},
                                      None, None, "owner")
            self.assertEqual(kept["revision"], 1)
        finally:
            con.close()

    def test_definition_revision_conflict(self):
        con, good_id = seed_db()
        try:
            with bound_workspace(con):
                res = ac.get_watchlist_results(
                    member_ctx(), {"watchlist_id": good_id,
                                   "expected_definition_revision": 1})
                self.assertEqual(res["items"][0]["match_ids"], ["ev-1"])
                with self.assertRaises(ac.AgentError) as stale:
                    ac.get_watchlist_results(
                        member_ctx(), {"watchlist_id": good_id,
                                       "expected_definition_revision": 99})
                self.assertEqual(stale.exception.code, ac.REVISION_CONFLICT)
        finally:
            con.close()


class ServiceBehaviorTest(unittest.TestCase):
    def test_workspace_context_lists_scoped_tools(self):
        con, _ = seed_db()
        try:
            with bound_workspace(con):
                full = ac.get_workspace_context(member_ctx(), {})
                tools = full["items"][0]["available_tools"]
                self.assertIn("search_evidence", tools)
                self.assertIn("get_watchlist_results", tools)
                narrow = ac.get_workspace_context(
                    member_ctx(scopes={"research:read"}), {})
                self.assertNotIn("get_watchlist_results",
                                 narrow["items"][0]["available_tools"])
        finally:
            con.close()

    def test_evidence_revision_round_trip_and_conflict(self):
        con, _ = seed_db()
        try:
            with bound_workspace(con):
                ctx = member_ctx()
                found = ac.search_evidence(ctx, {"filter": {"query": "shopify"}})
                ref = found["items"][0]["revision_ref"]
                detail = ac.get_evidence(ctx, {"evidence_id": "ev-1",
                                               "revision": ref})
                self.assertEqual(detail["items"][0]["revision_ref"], ref)
                with self.assertRaises(ac.AgentError) as stale:
                    ac.get_evidence(ctx, {"evidence_id": "ev-1",
                                          "revision": "v1.0.0.0"})
                self.assertEqual(stale.exception.code, ac.REVISION_CONFLICT)
                with self.assertRaises(ac.AgentError):
                    ac.get_evidence(ctx, {"evidence_id": "nope"})
        finally:
            con.close()

    def test_transcript_window_requires_exact_pin(self):
        con, _ = seed_db()
        try:
            with bound_workspace(con):
                ctx = member_ctx()
                window = ac.get_transcript_window(
                    ctx, {"episode_id": "ep-1", "transcript_version_id": 1,
                          "start_s": 0, "end_s": 120})
                self.assertEqual(window["items"][0]["returned_segments"], 2)
                narrow = ac.get_transcript_window(
                    ctx, {"episode_id": "ep-1", "transcript_version_id": 1,
                          "start_s": 13, "end_s": 120})
                self.assertEqual(narrow["items"][0]["returned_segments"], 1)
        finally:
            con.close()

    def test_brands_shows_episodes_lists(self):
        con, _ = seed_db()
        try:
            with bound_workspace(con):
                ctx = member_ctx()
                brands = ac.list_brands(ctx, {"query": "shopify", "filter": {}})
                by_key = {(b["brand"], b["channel"], b["basis"]): b
                          for b in brands["items"]}
                self.assertEqual(by_key[("shopify", "spoken", "confirmed")]
                                 ["evidence_records"], 1)
                scoped = ac.list_brands(ctx, {"query": "",
                                              "filter": {"sources": ["jev"]}})
                self.assertEqual(len(scoped["items"]), 1)
                shows = ac.list_shows(ctx, {"query": "show"})
                self.assertEqual(shows["total_matches"], 2)
                episodes = ac.list_episodes(
                    ctx, {"published_from": "2026-01-01",
                          "published_to": "2026-01-02"})
                self.assertEqual([e["id"] for e in episodes["items"]], ["ep-1"])
        finally:
            con.close()

    def test_campaign_gating_and_provenance(self):
        con, _ = seed_db()
        try:
            with bound_workspace(con):
                campaigns = ac.list_campaigns(member_ctx(), {"status": "all"})
                self.assertEqual(campaigns["items"][0]["attachment_count"], 1)
                full = ac.get_campaign(member_ctx(), {"campaign_id": 1})
                self.assertEqual(full["items"][0]["checks"][0]["result"]
                                 ["outcome"], "pass")
                scoped = ac.get_campaign(
                    member_ctx(scopes={"campaigns:read"}), {"campaign_id": 1})
                self.assertEqual(scoped["items"][0]["attachments"], [])
                self.assertIn("INCOMPLETE_SUPPORT",
                              scoped["limitations"])
                by_evidence = ac.get_source_provenance(
                    member_ctx(), {"evidence_id": "ev-1"})
                self.assertEqual(by_evidence["items"][0]["source"], "jev")
                by_version = ac.get_source_provenance(
                    member_ctx(), {"source_version_id": 1})
                self.assertEqual(by_version["items"][0]["source_pin"]
                                 ["source_key"], "transcript:ep-1")
        finally:
            con.close()

    def test_unbuilt_behaviors_raise_not_implemented(self):
        con, _ = seed_db()
        try:
            with bound_workspace(con):
                ctx = member_ctx()
                # Package B landed transcript search and summaries: they now
                # return bounded results instead of raising.
                found = ac.search_transcripts(ctx, {"query": "shopify"})
                self.assertGreaterEqual(found["total_matches"], 1)
                summary = ac.summarize_evidence(ctx, {"group_by": "brand"})
                self.assertGreaterEqual(
                    summary["items"][0]["denominator"]["distinct_evidence"], 1)
                # Carryover wiring (package F): search/fetch delegate to the
                # single shared compatibility builders instead of raising.
                found_compat = ac.search(ctx, {"query": "shopify"})
                self.assertIn("results", found_compat)
                with self.assertRaises(ac.AgentError) as rejected:
                    ac.fetch(ctx, {"id": "doc-1"})
                self.assertEqual(rejected.exception.code,
                                 ac.INVALID_ARGUMENT)
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
