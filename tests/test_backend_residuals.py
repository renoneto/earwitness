"""Bounded residual consumer regressions; synthetic source mechanics only."""
import json
import tempfile
import unittest
from pathlib import Path

from earwitness.app import access, agent_contracts, campaigns, citations, collaboration, conversion, customer, customer_query, customer_saved, db, exports, observations, ops, search, services
from tests import backend_fixtures
from tests.test_backend_allocation import _brief, _supplied, _placement, _revs


class ResidualConsumers(unittest.TestCase):
    def setUp(self):
        self.con = backend_fixtures.make_memory_workspace()
        self.addCleanup(self.con.close)
        backend_fixtures.publish_sources(self.con)
        self.owner = access.AccessContext.create(principal_id="owner", principal_kind="owner",
            workspace_id="local", credential_kind="local_operator")

    def reader(self, scopes=None, policy=None):
        return access.AccessContext.create(principal_id="reader", principal_kind="viewer", workspace_id="local",
            credential_kind="reader_bearer", scopes=scopes or {"research:read", "transcripts:read"}, content_policy=policy)

    def deny(self, key, category):
        self.con.execute("INSERT INTO source_content_policy(source_key,content_category,agent_disclosure,updated_at) VALUES (?,?,'deny',?)",
                         (key, category, db.utcnow()))

    def test_no_content_identity_and_sparse_current_ref(self):
        ref = citations.current_source_ref(self.con, "episode", "e1")
        with self.assertRaises(access.AuthorizationError):
            citations.resolve_source_state(self.con, ref, ctx=self.reader({"campaigns:read"}))
        sparse = customer.record(self.con, "ep:e1", ctx=self.reader({"research:read"}))
        self.assertNotIn("source_ref", sparse)
        ops.set_episode_media(self.con, "e1", {"audio_url": "https://audio.example/new"}, 1)
        self.assertEqual(customer.record(self.con, "ep:e1", ctx=self.reader({"research:read"})), sparse)

    def test_neighbor_denial_and_native_transcript_counts(self):
        sv = backend_fixtures._add_version(self.con, "detector:private", {"origin": "private"})
        self.con.execute("INSERT INTO evidence(id,source,source_version_id,episode_id,orig_start_s,orig_end_s,excerpt,created_at) VALUES ('private','detector',?,'e1',10,20,'secret',?)", (sv, db.utcnow()))
        self.deny("detector:private", "metadata")
        detail = services.evidence_detail(self.reader(), self.con, "fixture-ad")
        self.assertNotIn("private", [row["id"] for row in detail["overlaps"]])
        self.deny("transcript:e2", "transcript")
        result = search.search_transcripts_scan(self.con, search.validate_filter({"query": "Lumen"}), 50, ctx=self.reader())
        self.assertEqual(result["episodes_searched"], 1)
        self.assertEqual({row["episode_id"] for row in result["hits"]}, {"e1"})

    def test_share_predicates_deny_hidden_review_and_order(self):
        collaboration.create_workspace(self.con, "local", "Synthetic", "owner")
        made = collaboration.create_share(self.con, "local", "episode", "e1", "guest", ["excerpt"], "owner")
        ctx = access.AccessContext.create(principal_id="guest", principal_kind="guest", workspace_id="local",
            credential_kind="browser_session", authority_kind="share", share_grant_id=made["share_id"], content_policy={"fields": ["excerpt"]})
        good = services.search_evidence(ctx, self.con, {"query": "Lumen"})
        self.assertEqual(good["total_matches"], 2)
        for filt in ({"review_states": ["ad"]}, {"sort": "published"}, {"brand_channel": ["spoken"]}):
            with self.assertRaises(access.AuthorizationError):
                services.search_evidence(ctx, self.con, filt)
        before = good["items"]
        self.con.execute("UPDATE evidence SET orig_start_s=200 WHERE id='fixture-ad'")
        self.assertEqual(services.search_evidence(ctx, self.con, {"query": "Lumen"})["items"], before)

    def test_summary_brand_and_hidden_undated_cohort(self):
        self.deny("detector:fixture-ad", "metadata")
        ctx = self.reader()
        bundle = customer_query.research_bundle(self.con, {"version": 2, "mode": "mentions", "q": "Lumen"}, None, ["brand"], False, "ctx", ctx=ctx)
        self.assertEqual({row["key"] for row in bundle["summary"]["brand"]}, {"unknown"})
        self.deny("transcript:e2", "metadata")
        self.con.execute("UPDATE episodes SET pub_date='' WHERE id='e2'")
        result = customer_query.research_bundle(self.con, {"version": 2, "mode": "episodes", "from": "2026-01-01"}, None, [], False, "ctx", ctx=ctx)
        self.assertFalse(result["coverage"]["excluded_undated"])

    def test_metadata_only_revision_and_note_local_revision(self):
        ctx = self.reader()
        before = customer_query._view_revision(self.con, ctx)
        self.con.execute("INSERT INTO episodes(id,kind,ep_title,show_title,updated_at) VALUES ('metadata','feed','Only metadata','Metadata show',?)", (db.utcnow(),))
        customer.rebuild_customer_show_map(self.con)
        citations.publish_source_state(self.con, kind="episode", record_id="metadata", actor="fixture")
        after = customer_query._view_revision(self.con, ctx)
        self.assertNotEqual(after, before)
        results = customer_query.research_bundle(self.con, {"version": 2, "mode": "episodes"}, None, [], False, "ctx", ctx=ctx)
        self.assertIn("metadata", {row["episode_id"] for row in results["result"]["items"]})
        ref = citations.current_source_ref(self.con, "evidence", "fixture-ad")
        note = customer_saved.save_note(self.con, ref, "Mine", actor=self.owner.principal_id, ctx=self.owner)
        projected = customer_saved.get_note(self.con, note["id"], ctx=self.owner)
        db.bump_revision(self.con)
        self.assertEqual(customer_saved.get_note(self.con, note["id"], ctx=self.owner), projected)

    def test_bounded_large_unicode_and_current_interval_availability(self):
        blob = "☕" * 30000
        tv = backend_fixtures._add_version(self.con, "transcript:large", {"segments": [{"start": 0, "end": 10, "text": blob}]})
        self.con.execute("INSERT INTO episodes(id,kind,ep_title,transcript_version_id,updated_at) VALUES ('large','feed','Large',?,?)", (tv, db.utcnow()))
        customer.rebuild_customer_show_map(self.con)
        ref = citations.publish_source_state(self.con, kind="episode", record_id="large", actor="fixture")
        window = customer.record_window(self.con, ref, ctx=self.owner)
        self.assertLessEqual(sum(len(chunk["text"].encode()) for chunk in window["window"]["segments"]), 65536)
        self.assertFalse(window["window"]["text_complete"])
        self.assertIsNotNone(window["window"]["next_cursor"])
        bundle = customer_query.research_bundle(self.con, {"version": 2, "mode": "mentions", "q": "brought"}, None, [], False, "ctx", ctx=self.owner)
        interval = bundle["result"]["items"][0]["ref"]
        state, reason = customer_saved._saved_availability(self.con, {"source_kind": "episode", "record_id": "e1", "episode_id": "e1"}, interval)
        self.assertEqual(state, "current")

    def test_selected_empty_is_exact_under_unrelated_partial_scan(self):
        ctx = self.owner
        from unittest.mock import patch
        # A real scan cap, not a mocked result: no segments can be scanned.
        # The cap is an input outside the memo key: no memoized view on either side.
        customer_query._VIEW_MEMO.clear()
        self.addCleanup(customer_query._VIEW_MEMO.clear)
        with patch.object(customer, "SCAN_SEGMENT_CAP", 0, create=False):
            revision = customer_query._view_revision(self.con, ctx)
            out = customer_query.capture_export(self.con, {"version": 2, "mode": "mentions", "ids": []}, None, "selected", "json", revision, ctx=ctx)
        self.assertEqual(out["total"], 0)
        self.assertEqual(exports.load_export_bytes(self.con, out["export_id"], ctx)["item_count"], 0)

    def test_show_window_manifest_denies_coverage_only_source(self):
        camp = campaigns.save_customer_campaign(self.con, {"campaign": {"name": "Research", "mode": "research", "brand": "Lumen"}})["campaign"]
        show = customer.customer_show_for_episode(self.con, "e1")["show_identity_id"]
        campaigns.create_target(self.con, camp["id"], "show_window", show_id=show, expected_campaign_revision=camp["revision"])
        draft = campaigns.build_customer_draft(self.con, camp["id"])
        frozen = campaigns.freeze_customer_report(self.con, camp["id"], draft["campaign"]["revision"], draft["pins"])
        payload = exports.load_export_bytes(self.con, frozen["export_id"])["payload"]
        self.deny("transcript:e1", "metadata")
        for getter in (lambda: campaigns.get_customer_report(self.con, camp["id"], frozen["report_no"]),
                       lambda: campaigns.get_customer_report_html(self.con, camp["id"], frozen["report_no"]),
                       lambda: exports.load_export_bytes(self.con, frozen["export_id"])):
            with self.assertRaises(access.AuthorizationError):
                getter()
        self.assertEqual(self.con.execute("SELECT payload FROM exports WHERE id=?", (frozen["export_id"],)).fetchone()[0], payload)

    def test_move_rejects_foreign_scope_and_touches_actual_revisions(self):
        camp = _supplied(self.con, _brief(self.con))["campaign"]
        first = _placement(self.con, camp["id"], camp["revision"])["placement"]
        rev, units = _revs(self.con, camp["id"])
        self.assertEqual(rev, camp["revision"] + 1)
        second = _placement(self.con, camp["id"], rev, episode_id="e2")["placement"]
        ref = citations.current_source_ref(self.con, "evidence", "fixture-ad")
        rev, _ = _revs(self.con, camp["id"])
        campaigns.save_placement_evidence(self.con, camp["id"], first["id"], ref, rev)
        rev, units = _revs(self.con, camp["id"])
        body = {"action": "move", "ref": ref, "from_target_id": first["id"], "to_target_id": second["id"],
                "expected_campaign_revision": rev, "expected_from_revision": units[first["id"]], "expected_to_revision": units[second["id"]]}
        with self.assertRaises(ops.BadInput):
            campaigns.allocate_placement_evidence(self.con, camp["id"], body)
        self.assertEqual(_revs(self.con, camp["id"]), (rev, units))
        body.update(action="reference", to_target_id=first["id"])
        result = campaigns.allocate_placement_evidence(self.con, camp["id"], body)
        self.assertEqual(result["from_revision"], units[first["id"]] + 1)
        self.assertEqual(_revs(self.con, camp["id"])[0], rev + 1)
        self.assertFalse(result["credit_eligible"])

    def test_campaign_null_mode_attachment_quarantine(self):
        camp = _supplied(self.con, _brief(self.con))["campaign"]
        first = _placement(self.con, camp["id"], camp["revision"])["placement"]
        rev, units = _revs(self.con, camp["id"])
        self.con.execute("UPDATE campaigns SET mode=NULL WHERE id=?", (camp["id"],))
        with self.assertRaises(ops.BadInput):
            campaigns.save_placement_evidence(self.con, camp["id"], first["id"], citations.current_source_ref(self.con, "evidence", "fixture-ad"), rev)
        self.assertEqual(campaigns.placement_attachments(self.con, camp["id"], first["id"]), [])

    def test_stale_manifest_is_atomic_and_memberships_independent(self):
        report = conversion.dry_run(self.con)
        entry = next(row for row in report["definitions"] if row["kind"] == "saved_query")
        self.assertEqual(entry["membership"]["corrected_declared_before"],
                         sorted(row["ref"] for row in customer_query.match_for_tracker(self.con, entry["canonical"]["query"])[0]))
        self.assertEqual(entry["membership"]["corrected_declared_before"], entry["membership"]["after"])
        manifest = {"version": 1, "inventory_digest": report["inventory_digest"], "dispositions": [
            {"key": row["key"], "definition_digest": row["definition_digest"], "action": "converted" if row["disposition"] == "convertible" else "quarantined" if row["disposition"] == "needs_review" else "qualified"}
            for row in report["definitions"]]}
        self.con.execute("UPDATE customer_saved_searches SET query_json='{}' WHERE id=1")
        before = db.aggregate_revision(self.con)
        with self.assertRaises(ops.BadInput):
            conversion.apply(self.con, manifest=manifest)
        self.assertEqual(db.aggregate_revision(self.con), before)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM customer_conversion_lineage").fetchone()[0], 0)

    def test_split_merge_keeps_parent_pins_and_publishes_supersession(self):
        tv = self.con.execute("SELECT transcript_version_id FROM episodes WHERE id='e1'").fetchone()[0]
        self.con.execute("INSERT INTO transcript_versions(id,episode_id,segments,created_at) VALUES(1000,'e1',?,?)",
                         (json.dumps(backend_fixtures.SEGMENTS_E1), db.utcnow()))
        def make():
            return observations.propose_observation(self.con, "e1", 10, 20, bounds_evidence={"excerpt": "Lumen"},
                source_pins={"source_version_id": tv, "transcript_version_id": tv, "pipeline_transcript_version_id": 1000})
        parent = make()
        prior = citations.current_source_ref(self.con, "evidence", parent["evidence_id"])
        replacement = backend_fixtures._add_version(self.con, "transcript:e1", {"segments": [{"start": 0, "end": 30, "text": "Replacement"}]})
        self.con.execute("UPDATE episodes SET transcript_version_id=?,revision=revision+1 WHERE id='e1'", (replacement,))
        db.record_episode_snapshot(self.con, "e1", "fixture")
        citations.publish_episode_sources(self.con, "e1", "fixture")
        split = observations.split_observation(self.con, parent["observation_id"], [{"start_s": 10, "end_s": 15}, {"start_s": 15, "end_s": 20}], 1, rationale="separate", ctx=self.owner)
        current = citations.current_source_ref(self.con, "evidence", parent["evidence_id"])
        self.assertNotEqual(current, prior)
        self.assertEqual(citations.resolve_source_state(self.con, prior)["observation"]["status"], "proposed")
        self.assertEqual(citations.resolve_source_state(self.con, current)["observation"]["status"], "superseded")
        merged = observations.merge_observations(self.con, split["children"], {"start_s": 10, "end_s": 20}, {child: 1 for child in split["children"]}, rationale="together", ctx=self.owner)
        child_ref = citations.current_source_ref(self.con, "evidence", f"pipe:{merged['child']}:r1")
        self.assertEqual(citations.resolve_source_state(self.con, child_ref)["pins"]["transcript_version_id"], tv)
        self.assertIn("brought", citations.resolve_source_state(self.con, child_ref)["text"])
        self.assertEqual(self.con.execute("SELECT transcript_version_id FROM ad_observations WHERE id=?", (merged["child"],)).fetchone()[0], 1000)


    def test_open_window_assesses_only_entered_comparison_fields(self):
        from earwitness.app import coverage
        brief = _brief(self.con)
        camp = _supplied(self.con, brief)["campaign"]
        show = customer.customer_show_for_episode(self.con, "e1")["show_identity_id"]
        made = campaigns.save_customer_placement(self.con, camp["id"], {
            "placement": {"scope": "show_window", "show_id": show, "expected_count": 1,
                          "binding": "eligible", "requirements": {"message": "Say Lumen"},
                          "provenance": {"brief_ref": {"kind": "supplied_brief", "brief_id": brief["brief_id"], "version": 1}}},
            "expected_campaign_revision": camp["revision"]})
        for stage in ("fetch", "transcribe", "screen", "observation_publication", "index"):
            version = self.con.execute("SELECT transcript_version_id FROM episodes WHERE id='e1'").fetchone()[0]
            coverage.record_attestation(self.con, "e1", stage, (0, 30), (0, 30),
                                        source_pin=f"source-version:{version}", producer="fixture", actor="owner")
        self.con.execute("UPDATE episodes SET duration_min=0.5 WHERE id='e1'")  # fully checked needs a duration
        backend_fixtures.record_screening(self.con, "e1")
        draft = campaigns.build_customer_draft(self.con, camp["id"])
        unit = made["placement"]["id"]
        self.assertEqual(draft["allocation"]["evaluations"][unit]["outcome"], "pending")
        self.assertEqual(draft["assessment_scope"]["by_unit"][unit],
                         {"entered_fields": ["message"], "assessed_fields": [], "unknown_fields": ["message"]})

    def test_captured_scope_mismatch_and_intact_report_survive_current_source_loss(self):
        camp = _supplied(self.con, _brief(self.con))["campaign"]
        placement = _placement(self.con, camp["id"], camp["revision"])["placement"]
        draft = campaigns.build_customer_draft(self.con, camp["id"])
        frozen = campaigns.freeze_customer_report(self.con, camp["id"], draft["campaign"]["revision"], draft["pins"])
        original = exports.load_export_bytes(self.con, frozen["export_id"])["payload"]
        version = backend_fixtures._add_version(self.con, "transcript:e1", {"segments": [{"start": 0, "end": 60, "text": "New transcript"}]})
        self.con.execute("UPDATE episodes SET transcript_version_id=?,revision=revision+1 WHERE id='e1'", (version,))
        db.record_episode_snapshot(self.con, "e1", "fixture")
        citations.publish_episode_sources(self.con, "e1", "fixture")
        changed = campaigns.build_customer_draft(self.con, camp["id"])
        self.assertEqual(changed["pins"]["source_scopes"][placement["id"]], draft["pins"]["source_scopes"][placement["id"]])
        cov = changed["coverage_by_placement"][placement["id"]]
        self.assertEqual(cov["state"], "unknown")
        self.assertIn("stale", cov["ambiguity"])
        self.con.execute("PRAGMA foreign_keys=OFF")
        self.con.execute("DELETE FROM source_versions WHERE id=?", (version,))
        report = campaigns.get_customer_report(self.con, camp["id"], frozen["report_no"])
        self.assertTrue(report["stale"])
        self.assertEqual(exports.load_export_bytes(self.con, frozen["export_id"])["payload"], original)
        self.assertEqual(campaigns.get_customer_report_html(self.con, camp["id"], frozen["report_no"])[0], original)


    def test_artwork_bytes_require_show_and_retained_feed_source_admission(self):
        import hashlib, struct, zlib
        def chunk(kind, payload):
            return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))
        png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00")) + chunk(b"IEND", b"")
        show = customer.customer_show_for_episode(self.con, "e1")["show_identity_id"]
        source = self.con.execute("SELECT id FROM source_versions WHERE source_key='episodes.json'").fetchone()[0]
        db.upsert_show_artwork(self.con, show, {"image_bytes": png, "mime": "image/png", "width": 1,
            "height": 1, "sha256": hashlib.sha256(png).hexdigest(), "validated_at": db.utcnow(),
            "status": "ok", "source_version_id": source})
        ctx = self.reader()
        self.assertEqual(customer.show_artwork_bytes(self.con, show, ctx), ("image/png", png))
        self.assertIn(("episodes.json", "metadata", False), ctx.disclosures)
        self.deny("episodes.json", "metadata")
        with self.assertRaises(access.AuthorizationError):
            customer.show_artwork_bytes(self.con, show, self.reader())
        self.assertEqual(self.con.execute("SELECT image_bytes FROM show_artwork WHERE show_identity_id=?", (show,)).fetchone()[0], png)

    def test_native_episode_availability_uses_its_source_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            filename = backend_fixtures.make_workspace(Path(tmp) / "workspace", schema_version=8)
            con = db.open_existing(filename, readonly=False)
            con.execute("INSERT INTO source_content_policy(source_key,content_category,agent_disclosure,updated_at) VALUES ('transcript:e2','transcript','deny',?)", (db.utcnow(),))
            con.close()
            ctx = self.reader()
            ctx.db_path = str(filename)
            result = agent_contracts.list_episodes(ctx, {})
            items = {item["id"]: item for item in result["items"]}
            self.assertTrue(items["e1"]["has_transcript"])
            self.assertFalse(items["e2"]["has_transcript"])
            self.assertIn(("transcript:e1", "transcript", False), ctx.disclosures)


    def test_pinned_full_episode_scope_does_not_define_away_short_transcript_gaps(self):
        from earwitness.app import coverage
        self.con.execute("UPDATE episodes SET duration_min=60,revision=revision+1 WHERE id='e1'")
        db.record_episode_snapshot(self.con, "e1", "fixture")
        citations.publish_episode_sources(self.con, "e1", "fixture")
        scope = campaigns._capture_episode_scope(self.con, "e1")
        self.assertEqual(scope["bounds"], {"start_s": 0.0, "end_s": 3600.0})
        version = self.con.execute("SELECT transcript_version_id FROM episodes WHERE id='e1'").fetchone()[0]
        for stage in ("fetch", "transcribe", "screen", "observation_publication", "index"):
            coverage.record_attestation(self.con, "e1", stage, (0, 3600), (0, 30),
                source_pin=f"source-version:{version}", producer="fixture", actor="owner")
        projected = campaigns._placement_coverage(self.con, {"scope": "episode", "episode_id": "e1", "source_scope": scope})
        self.assertEqual(projected["state"], "incomplete")
        self.assertEqual(projected["missing"], ["e1"])


class HostedResidualConsumers(unittest.TestCase):
    def setUp(self):
        from tests.test_backend_authority import HostedReaderHttpTest
        self.http = HostedReaderHttpTest()
        self.http.setUp()
        self.addCleanup(self.http.tearDown)

    def test_billing_status_and_exact_readonly_request_handle(self):
        headers = {"authorization": "Bearer " + self.http.reader_token}
        status, body = self.http._call("/api/v1/billing/status", method="GET", headers=headers)
        self.assertEqual(status, 200, body)
        self.assertIn("workspace_context", body)
        import asyncio
        messages = []
        async def run():
            async def handler(ctx, con, entry, via_session, session, send):
                with self.assertRaises(__import__("sqlite3").OperationalError):
                    con.execute("INSERT INTO meta(key,value) VALUES ('forbidden-read-write','1')")
                await self.http.app._send(send, 200, b'{"ok":true}')
            async def send(message):
                messages.append(message)
            await self.http.app._with_workspace({"path": "/api/v1/customer/research-query"}, None, send, headers, "POST", handler)
        asyncio.run(run())
        self.assertEqual(messages[0]["status"], 200)

    def test_audit_async_chunks_reject_changed_actual_policy(self):
        import asyncio
        from unittest.mock import patch
        grant = self.http.app.store.resolve_token(self.http.reader_token)
        con = self.http.app.store._connect()
        con.execute("INSERT INTO agent_audit(request_id,grant_id,principal_id,workspace_id,operation,object_ids,outcome,recorded_at) VALUES ('one',?,?,?,'audit-fixture','{}','failure','fixture')",
                    (grant.id, grant.member_id, grant.workspace_id))
        con.close()
        messages = []
        original = self.http.app._chunked
        async def changed(source):
            async for chunk in original(source):
                control = self.http.app.store._connect()
                control.execute("UPDATE connection_grants SET content_policy=? WHERE id=?", ('{"metadata":"deny"}', grant.id))
                control.close()
                yield chunk
        async def send(message):
            messages.append(message)
        async def run():
            with patch.object(self.http.app, "_chunked", changed):
                await self.http.app._audit_stream({}, None, send, {"authorization": "Bearer " + self.http.reader_token}, grant.id)
        asyncio.run(run())
        self.assertEqual(b"".join(row.get("body", b"") for row in messages), b"")


    def test_session_expiring_during_create_leaves_no_workspace_or_registry(self):
        from earwitness.app import browser_auth
        from unittest.mock import patch
        session = self.http.app._lookup_browser(self.http._csrf())
        ctx = browser_auth.build_session_context(session, "", "viewer", "expiry-create")
        ctx.authority_kind, ctx.scopes = "bootstrap", frozenset({"workspace:create"})
        instant = [session["expires_at"] - 1]
        initialize = db.init_schema
        def expires(con):
            initialize(con)
            instant[0] = session["expires_at"] + 1
        with patch.object(db, "init_schema", expires):
            with self.assertRaises(access.AuthorizationError):
                services.collab_create_workspace(self.http.app.store, self.http.data_dir, ctx, "expired-create", "Expired",
                                                 now=lambda: instant[0])
        self.assertFalse((Path(self.http.data_dir) / "expired-create.sqlite3").exists())
        control = self.http.app.store._connect()
        try:
            self.assertIsNone(control.execute("SELECT * FROM workspace_registry WHERE workspace_id='expired-create'").fetchone())
        finally:
            control.close()


class WorkerAtomicConsumers(unittest.TestCase):
    def test_ingest_publication_abort_rolls_back_and_reopen_is_exact(self):
        from scripts import app_worker
        from earwitness.app import coverage, indexing
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            filename = backend_fixtures.make_workspace(workspace, schema_version=8)
            con = db.open_existing(filename, readonly=False)
            before = {table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in
                      ("source_versions", "source_states", "episode_history", "coverage_attestations", "audit")}
            old = citations.current_source_ref(con, "episode", "e1")
            con.execute("CREATE TRIGGER abort_worker_publication BEFORE INSERT ON source_states BEGIN SELECT RAISE(ABORT,'worker publication abort'); END")
            con.close()
            data = json.dumps({"segments": [{"start": 0, "end": 40, "text": "replacement"}]}).encode()
            with self.assertRaises(sqlite3.IntegrityError):
                app_worker._ingest_transcript_file(workspace, "fixture-worker", data, "fixture.json", "e1", coverage, indexing)
            con = db.open_existing(filename, readonly=False)
            self.assertEqual(citations.current_source_ref(con, "episode", "e1"), old)
            self.assertEqual({table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before}, before)
            con.execute("DROP TRIGGER abort_worker_publication")
            con.close()
            made = app_worker._ingest_transcript_file(workspace, "fixture-worker", data, "fixture.json", "e1", coverage, indexing)
            con = db.open_existing(filename, readonly=True)
            try:
                fresh = citations.current_source_ref(con, "episode", "e1")
                self.assertNotEqual(fresh, old)
                self.assertEqual(citations.resolve_source_state(con, fresh)["text"], "replacement")
                self.assertIn("Lumen", citations.resolve_source_state(con, old)["text"])
                self.assertEqual(con.execute("SELECT transcript_version_id FROM episodes WHERE id='e1'").fetchone()[0], made["transcript_version_id"])
                self.assertEqual(indexing.check_fresh(con), db.aggregate_revision(con))
            finally:
                con.close()


class EmbeddedCheckConsumers(unittest.TestCase):
    def setUp(self):
        self.con = backend_fixtures.make_memory_workspace()
        self.addCleanup(self.con.close)
        backend_fixtures.publish_sources(self.con)
        ops.save_review(self.con, "fixture-ad", {"judgment": "ad", "bounds_verified": True,
            "eff_start_s": 10, "eff_end_s": 20}, 1)

    def test_hidden_review_notes_do_not_change_permitted_check_projection(self):
        ctx = access.AccessContext.create(principal_id="reader", principal_kind="viewer", workspace_id="local",
            credential_kind="reader_bearer", scopes={"research:read", "transcripts:read", "campaigns:read"})
        check = {"evidence_id": "fixture-ad", "check_type": "literal_phrase", "params": {"phrase": "Lumen"}}
        before = ops.evaluate_check(self.con, check, ctx=ctx)
        self.assertEqual(before["outcome"], "pass")
        self.assertNotIn("review_revision", before)
        ops.save_review(self.con, "fixture-ad", {"notes": "Private operator note"}, 2)
        self.assertEqual(ops.evaluate_check(self.con, check, ctx=ctx), before)

    def test_remote_url_alignment_never_qualifies_embedded_timing_check(self):
        ops.set_episode_media(self.con, "e1", {"audio_url": "https://audio.example/remote", "alignment": "verified"}, 1)
        result = ops.evaluate_check(self.con, {"evidence_id": "fixture-ad", "check_type": "duration_range",
            "params": {"min_s": 10, "max_s": 10}})
        self.assertEqual(result["outcome"], "unknown")
