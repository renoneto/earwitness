"""Consumer counterexamples for the independent retained backend findings."""
import json
import unittest

from earwitness.app import citations, customer, customer_saved, db, ops
from tests import backend_fixtures


class SourceAuthorityRegression(unittest.TestCase):
    def setUp(self):
        self.con = backend_fixtures.make_memory_workspace()
        backend_fixtures.publish_sources(self.con)
        self.addCleanup(self.con.close)

    def test_reversion_allocates_new_current_publication(self):
        ops.save_review(self.con, 'fixture-ad', {'judgment': 'ad'}, 1)
        a = citations.current_source_ref(self.con, 'evidence', 'fixture-ad')
        ops.save_review(self.con, 'fixture-ad', {'judgment': 'unsure'}, 2)
        b = citations.current_source_ref(self.con, 'evidence', 'fixture-ad')
        ops.save_review(self.con, 'fixture-ad', {'judgment': 'ad'}, 3)
        reverted = citations.current_source_ref(self.con, 'evidence', 'fixture-ad')
        self.assertNotEqual(reverted, a)
        self.assertNotEqual(reverted, b)
        self.assertEqual(citations.resolve_source_state(self.con, reverted)['review']['judgment'], 'ad')

    def test_history_is_authority_not_index_payload(self):
        ref = citations.current_source_ref(self.con, 'evidence', 'fixture-ad')
        ops.save_review(self.con, 'fixture-ad', {'judgment': 'ad'}, 1)
        self.con.execute("DELETE FROM review_history WHERE evidence_id='fixture-ad' AND review_revision=1")
        with self.assertRaises(citations.RevisionUnavailable):
            customer.record(self.con, ref)

    def test_private_note_preserves_relationship_and_content(self):
        ref = citations.current_source_ref(self.con, 'evidence', 'fixture-ad')
        before = customer.record(self.con, ref)
        ops.save_review(self.con, 'fixture-ad', {'notes': 'hidden only'}, 1)
        self.assertEqual(customer.record(self.con, ref), before)

    def test_historical_window_and_capture_use_requested_interval(self):
        ref = citations.current_source_ref(self.con, 'episode', 'e1') + ':w:10.0:20.0'
        ops.set_episode_media(self.con, 'e1', {'audio_url': 'https://example.invalid/new.mp3'}, 1)
        window = customer.record_window(self.con, ref)
        self.assertEqual(window['selected_bounds'], {'start_s': 10.0, 'end_s': 20.0})
        self.assertEqual([s['text'] for s in window['window']['segments']], ['This episode is brought to you by Lumen'])
        captured = customer_saved._capture_excerpt(self.con, ref, None, None)
        self.assertEqual(captured['text'], 'This episode is brought to you by Lumen')
        self.assertEqual(captured['source_ref'], ref)
        self.assertEqual(captured['episode_id'], 'e1')
        with self.assertRaises(customer._search.FilterRejected):
            customer_saved._capture_excerpt(self.con, ref, 0.0, 30.0)

    def test_nested_real_producer_rolls_back_caller(self):
        before = citations.current_source_ref(self.con, 'evidence', 'fixture-ad')
        with self.assertRaises(RuntimeError):
            with db.transaction(self.con, write=True):
                ops.save_review(self.con, 'fixture-ad', {'judgment': 'ad'}, 1)
                raise RuntimeError('caller abort')
        self.assertEqual(citations.current_source_ref(self.con, 'evidence', 'fixture-ad'), before)
        self.assertEqual(self.con.execute("SELECT judgment FROM reviews WHERE evidence_id='fixture-ad'").fetchone()[0], 'finding')

    def test_media_change_publishes_affected_evidence(self):
        ref = citations.current_source_ref(self.con, 'evidence', 'fixture-ad')
        old = customer.record(self.con, ref)
        ops.set_episode_media(self.con, 'e1', {'audio_url': 'https://example.invalid/new.mp3'}, 1)
        current = citations.current_source_ref(self.con, 'evidence', 'fixture-ad')
        self.assertNotEqual(current, ref)
        pinned = customer.record(self.con, ref)
        self.assertEqual(pinned['episode'], old['episode'])
        self.assertEqual(pinned['media'], old['media'])
        self.assertEqual(pinned['relationship'], 'historical')


class ProtectedConsumerRegression(unittest.TestCase):
    def setUp(self):
        from tests.test_backend_authority import HostedReaderHttpTest
        self.http = HostedReaderHttpTest()
        self.http.setUp()
        self.addCleanup(self.http.tearDown)

    def test_expired_opaque_customer_token_denies(self):
        control = self.http.app.store._connect()
        control.execute("UPDATE connection_grants SET expires_at='2000-01-01T00:00:00+00:00'")
        control.commit()
        control.close()
        status, body = self.http._call('/api/v1/customer/research-query',
            body={'query': {'version': 2, 'mode': 'mentions'}},
            headers={'authorization': 'Bearer ' + self.http.reader_token})
        self.assertEqual(status, 401, body)
        self.assertNotIn('result', body)

    def test_reader_export_and_collaboration_are_readonly(self):
        import sqlite3
        con = sqlite3.connect(self.http.db_path)
        before = con.execute('SELECT COUNT(*) FROM exports').fetchone()[0]
        con.close()
        for path, body in (
            ('/api/v1/customer/research-export', {'query': {'version': 2, 'mode': 'mentions'}}),
            ('/api/v1/shares', {'object_type': 'episode', 'object_id': 'e1', 'audience': 'named', 'fields': ['ep_title']}),
            ('/api/v1/workspaces/acme/invites', {'member': 'new', 'role': 'viewer'})):
            status, result = self.http._call(path, body=body,
                headers={'authorization': 'Bearer ' + self.http.reader_token})
            self.assertEqual(status, 403, result)
        con = sqlite3.connect(self.http.db_path)
        self.assertEqual(con.execute('SELECT COUNT(*) FROM exports').fetchone()[0], before)
        con.close()

    def test_customer_matching_uses_presented_scopes(self):
        issued = self.http.app.store.issue_grant(
            member_id='ed-1', member_role='editor', client_id='restricted',
            workspace_id='acme', scopes=['research:read'], content_policy={})
        request = {'query': {'version': 2, 'mode': 'mentions', 'q': 'different'}}
        partial_status, partial = self.http._call('/api/v1/customer/research-query',
            body=request, headers={'authorization': 'Bearer ' + issued['access_token']})
        full_status, full = self.http._call('/api/v1/customer/research-query',
            body=request, headers={'authorization': 'Bearer ' + self.http.reader_token})
        self.assertEqual((partial_status, full_status), (200, 200), (partial, full))
        self.assertEqual(partial['result']['total'], 0)
        self.assertEqual(full['result']['total'], 1)
        self.assertNotEqual(partial['workspace_context'], full['workspace_context'])

    def test_registered_native_read_creates_no_guessed_database(self):
        from pathlib import Path
        status, body = self.http._call('/api/v1/evidence/search',
            body={'filter': {'query': 'Lumen'}},
            headers={'authorization': 'Bearer ' + self.http.reader_token})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['items'], body)
        self.assertFalse((Path(self.http.data_dir) / 'acme').exists())

    def test_capture_then_committed_revocation_releases_no_data_or_receipt(self):
        import asyncio
        from earwitness.app import services, asgi
        captured = []
        async def scenario():
            entered, resume = asyncio.Event(), asyncio.Event()
            async def handler(ctx, con, entry, via_session, session, send):
                ref = citations.current_source_ref(con, 'evidence', 'fixture-ad')
                result = services.customer_record(ctx, con, entry, ref)
                self.assertIn('Lumen', result['record']['text'])
                entered.set()
                await resume.wait()
                await self.http.app._send(send, *asgi._json_response(200, result))
            async def send(message):
                captured.append(message)
            headers = {'authorization': 'Bearer ' + self.http.reader_token}
            scope = {'type': 'http', 'method': 'GET', 'path': '/api/v1/customer/records/captured'}
            operation = asyncio.create_task(self.http.app._with_workspace(
                scope, None, send, headers, 'GET', handler))
            await entered.wait()
            grant = self.http.app.store.resolve_token(self.http.reader_token)
            self.http.app.store.revoke_grant(grant.id, "independent captured-request revocation")
            resume.set()
            await operation
        asyncio.run(scenario())
        self.assertEqual(captured[0]['status'], 403, captured)
        self.assertNotIn(b'Lumen', b''.join(message.get('body', b'') for message in captured))
        control = self.http.app.store._connect()
        self.assertEqual(control.execute("SELECT COUNT(*) FROM agent_audit WHERE outcome='success'").fetchone()[0], 0)
        control.close()


class RetainedArtifactRegression(unittest.TestCase):
    def setUp(self):
        self.con = backend_fixtures.make_memory_workspace()
        backend_fixtures.publish_sources(self.con)
        self.addCleanup(self.con.close)

    def test_saved_source_loss_retains_bytes_but_policy_loss_denies_all(self):
        from earwitness.app import access
        ref = citations.current_source_ref(self.con, 'evidence', 'fixture-ad')
        item = customer_saved.create_item(self.con,
            {'type': 'excerpt', 'title': 'Captured', 'target': {'ref': ref}},
            workspace_context=None)['item']
        original = self.con.execute('SELECT captured_json FROM customer_saved_items WHERE id=?',
                                    (item['id'],)).fetchone()[0]
        self.con.execute("DELETE FROM reviews WHERE evidence_id='fixture-ad'")
        self.con.execute("DELETE FROM brand_assocs WHERE evidence_id='fixture-ad'")
        self.con.execute("DELETE FROM evidence WHERE id='fixture-ad'")
        self.assertEqual(customer_saved.get_item(self.con, item['id'])['item']['captured']['text'],
                         'This episode is brought to you by Lumen')
        self.con.execute("INSERT INTO source_content_policy (source_key,content_category,agent_disclosure,updated_at) VALUES ('transcript:e1','transcript','deny','fixture')")
        with self.assertRaises(access.AuthorizationError):
            customer_saved.get_item(self.con, item['id'])
        self.assertEqual(self.con.execute('SELECT captured_json FROM customer_saved_items WHERE id=?',
                                         (item['id'],)).fetchone()[0], original)

    def test_exports_reject_unverifiable_digest_and_policy_loss(self):
        from earwitness.app import exports, access
        ref = citations.current_source_ref(self.con, 'evidence', 'fixture-ad')
        stored = exports.store_capture(self.con, [{'ref': ref, 'text': 'captured'}],
            'json', 'all_matches', 'distinct_results', {}, 'Captured')
        original = exports.load_export_bytes(self.con, stored['export_id'])['payload']
        self.con.execute("INSERT INTO source_content_policy (source_key,content_category,agent_disclosure,updated_at) VALUES ('transcript:e1','transcript','deny','fixture')")
        with self.assertRaises(access.AuthorizationError):
            exports.load_export_bytes(self.con, stored['export_id'])
        self.assertEqual(self.con.execute('SELECT payload FROM exports WHERE id=?',
                                         (stored['export_id'],)).fetchone()[0], original)
        self.con.execute("DELETE FROM source_content_policy")
        self.con.execute("UPDATE exports SET digest='' WHERE id=?", (stored['export_id'],))
        with self.assertRaises(exports.DigestMismatch):
            exports.load_export_bytes(self.con, stored['export_id'])


class CampaignConsumerRegression(unittest.TestCase):
    def setUp(self):
        from tests.test_backend_allocation import _con
        self.con = _con()
        self.addCleanup(self.con.close)


    def test_empty_retained_transcript_freezes_unknown_scope_without_inventing_text(self):
        from earwitness.app import campaigns
        from tests.test_backend_allocation import _brief, _supplied, _placement
        tv = backend_fixtures._add_version(self.con, "transcript:empty", {"segments": []})
        self.con.execute("INSERT INTO episodes(id,kind,show_title,ep_title,transcript_version_id,"
                         "revision,updated_at) VALUES ('empty','supplied','Empty','No transcript',?,1,?)",
                         (tv, db.utcnow()))
        db.record_episode_snapshot(self.con, "empty", "fixture")
        scope_ref = citations.publish_source_state(self.con, kind="episode", record_id="empty", actor="fixture")
        camp = _supplied(self.con, _brief(self.con))["campaign"]
        unit = _placement(self.con, camp["id"], camp["revision"], episode_id="empty")["placement"]
        draft = campaigns.build_customer_draft(self.con, camp["id"])
        frozen = campaigns.freeze_customer_report(self.con, camp["id"], draft["campaign"]["revision"], draft["pins"])
        model = campaigns.get_customer_report(self.con, camp["id"], frozen["report_no"])["frozen"]
        self.assertEqual(model["scope_sources"][unit["id"]]["ref"], scope_ref)
        self.assertEqual(model["scope_sources"][unit["id"]]["text"], "")
        self.assertEqual(model["evaluations"][unit["id"]]["outcome"], "ambiguous")
        self.assertEqual(model["coverage_by_placement"][unit["id"]]["state"], "unknown")
        self.assertEqual(model["allocation"]["used"], {})

    def test_metadata_only_scope_freezes_without_quote_and_obeys_retained_policy(self):
        from earwitness.app import campaigns, access
        from tests.test_backend_allocation import _brief, _supplied, _placement
        self.con.execute("INSERT INTO episodes(id,kind,show_title,ep_title,revision,updated_at)"
                         " VALUES ('metadata-only','supplied','Unknown','Metadata only',1,?)", (db.utcnow(),))
        ref = citations.publish_source_state(self.con, kind="episode", record_id="metadata-only", actor="fixture")
        camp = _supplied(self.con, _brief(self.con))["campaign"]
        unit = _placement(self.con, camp["id"], camp["revision"], episode_id="metadata-only")["placement"]
        draft = campaigns.build_customer_draft(self.con, camp["id"])
        frozen = campaigns.freeze_customer_report(self.con, camp["id"], draft["campaign"]["revision"], draft["pins"])
        model = campaigns.get_customer_report(self.con, camp["id"], frozen["report_no"])["frozen"]
        captured = model["scope_sources"][unit["id"]]
        self.assertEqual(captured["ref"], ref)
        self.assertEqual(captured["capture_scope"], "metadata_only")
        self.assertIsNone(captured["text"])
        self.assertEqual(model["coverage_by_placement"][unit["id"]]["state"], "unknown")
        self.assertEqual(model["allocation"]["used"], {})
        self.con.execute("INSERT INTO source_content_policy(source_key,content_category,agent_disclosure,updated_at)"
                         " VALUES ('episode:metadata-only','metadata','deny','fixture')")
        with self.assertRaises(access.AuthorizationError):
            campaigns.get_customer_report(self.con, camp["id"], frozen["report_no"])
        with self.assertRaises(access.AuthorizationError):
            citations.resolve_source_state(self.con, ref)

    def test_research_freeze_captures_targets_query_and_exact_source_scope(self):
        from earwitness.app import campaigns
        camp = campaigns.save_customer_campaign(self.con, {"campaign": {
            "name": "Research", "mode": "research", "brand": "Lumen",
            "source_query": {"version": 2, "mode": "mentions", "q": "Lumen"}}})["campaign"]
        target = campaigns.create_target(self.con, camp["id"], "episode", episode_id="e1",
            actor="owner", expected_campaign_revision=camp["revision"])
        ref = citations.current_source_ref(self.con, "evidence", "fixture-ad")
        revision = self.con.execute("SELECT revision FROM campaigns WHERE id=?", (camp["id"],)).fetchone()[0]
        campaigns.attach_target_evidence(self.con, camp["id"], target["id"], ref, revision)
        draft = campaigns.build_customer_draft(self.con, camp["id"])
        frozen = campaigns.freeze_customer_report(self.con, camp["id"], draft["campaign"]["revision"], draft["pins"])
        report = campaigns.get_customer_report(self.con, camp["id"], frozen["report_no"])["frozen"]
        self.assertEqual(report["mode"], "research")
        self.assertEqual(report["targets"][0]["id"], target["id"])
        self.assertEqual(report["captured"][target["id"]][0]["ref"], ref)
        self.assertTrue(report["research_scope"]["refs"])
        self.assertEqual(report["scope_sources"][target["id"]]["episode"]["episode_id"], "e1")
        self.assertEqual(report["evaluations"], {})
        self.assertIsNone(report["assessment_scope"])
        body, _digest = campaigns.get_customer_report_html(self.con, camp["id"], frozen["report_no"])
        self.assertIn("This episode is brought to you by Lumen", body)
        self.assertIn(target["id"], body)

    def test_reference_binding_has_zero_actual_credit_and_remains_captured(self):
        from earwitness.app import campaigns, observations, creatives
        from tests.test_backend_allocation import _brief, _supplied, _placement, _revs
        tv = self.con.execute("SELECT transcript_version_id FROM episodes WHERE id='e1'").fetchone()[0]
        proposal = observations.propose_observation(self.con, "e1", 10.0, 20.0,
            bounds_evidence={"excerpt": "Lumen"}, fields={},
            source_pins={"source_version_id": tv, "transcript_version_id": tv}, actor="owner")
        observations.review_observation(self.con, proposal["observation_id"], "reviewed_ad", 1)
        creatives.create_occurrence(self.con, "e1", proposal["observation_id"], 2, correspondence="confirmed")
        evidence_id = proposal["evidence_id"]
        ops.save_review(self.con, evidence_id, {"judgment": "ad"}, 0)
        ops.set_brand_assocs(self.con, evidence_id,
            [{"brand": "Lumen", "basis": "confirmed", "channel": "spoken"}], 0)
        ref = citations.current_source_ref(self.con, "evidence", evidence_id)
        camp = _supplied(self.con, _brief(self.con))["campaign"]
        unit = _placement(self.con, camp["id"], camp["revision"])["placement"]
        revision, _ = _revs(self.con, camp["id"])
        campaigns.save_placement_evidence(self.con, camp["id"], unit["id"], ref, revision)
        before = campaigns.build_customer_draft(self.con, camp["id"])
        self.assertEqual(len(before["allocation"]["used"]), 1, before["allocation"])
        revision, units = _revs(self.con, camp["id"])
        campaigns.allocate_placement_evidence(self.con, camp["id"], {
            "action": "reference", "ref": ref, "from_target_id": unit["id"], "to_target_id": unit["id"],
            "expected_campaign_revision": revision, "expected_from_revision": units[unit["id"]],
            "expected_to_revision": units[unit["id"]]})
        after = campaigns.build_customer_draft(self.con, camp["id"])
        self.assertEqual(after["allocation"]["used"], {})
        self.assertEqual(after["pins"]["attachments"][unit["id"]], [ref])
        frozen = campaigns.freeze_customer_report(self.con, camp["id"], after["campaign"]["revision"], after["pins"])
        model = campaigns.get_customer_report(self.con, camp["id"], frozen["report_no"])["frozen"]
        self.assertEqual(model["captured"][unit["id"]][0]["ref"], ref)
        self.assertTrue(model["assessment_scope"]["unassessed_brief_conditions"])
        self.assertEqual(model["allocation"]["used"], {})

    def test_open_ranges_and_source_scope_containment(self):
        from earwitness.app import campaigns
        checked = campaigns._operator_requirements(campaigns._check_requirements(
            {"duration_s": {"min": None, "max": 15}, "position_s": {"min": 5, "max": None}}))
        occurrence = {"correspondence": "confirmed", "review_status": "reviewed_ad",
            "advertiser_confirmed": True, "source_matched": True, "alignment_verified": True,
            "bounds_verified": True, "full_episode_duration_s": 30, "duration_s": 10, "position_s": 10}
        self.assertEqual(campaigns._occurrence_eligible(occurrence, {"requirements": checked}), (True, []))
        source_scope = campaigns._capture_episode_scope(self.con, "e1")
        ref = citations.current_source_ref(self.con, "evidence", "fixture-ad")
        self.assertTrue(campaigns._target_allows_ref(self.con,
            {"scope": "episode", "episode_id": "e1", "source_scope": source_scope}, ref))
        self.assertFalse(campaigns._target_allows_ref(self.con,
            {"scope": "episode", "episode_id": "e2", "source_scope": source_scope}, ref))

    def test_report_manifest_and_every_getter_reject_corruption_or_policy_loss(self):
        from earwitness.app import campaigns, exports, access
        from tests.test_backend_allocation import _brief, _supplied, _placement
        camp = _supplied(self.con, _brief(self.con))["campaign"]
        _placement(self.con, camp["id"], camp["revision"])
        draft = campaigns.build_customer_draft(self.con, camp["id"])
        frozen = campaigns.freeze_customer_report(self.con, camp["id"], draft["campaign"]["revision"], draft["pins"])
        getters = [
            lambda: campaigns.get_customer_report(self.con, camp["id"], frozen["report_no"]),
            lambda: campaigns.get_customer_report_html(self.con, camp["id"], frozen["report_no"]),
            lambda: exports.load_export_bytes(self.con, frozen["export_id"])]
        original = exports.load_export_bytes(self.con, frozen["export_id"])["payload"]
        self.con.execute("INSERT INTO source_content_policy (source_key,content_category,agent_disclosure,updated_at)"
                         " VALUES ('transcript:e1','transcript','deny','fixture')")
        for getter in getters:
            with self.assertRaises(access.AuthorizationError):
                getter()
        self.assertEqual(self.con.execute("SELECT payload FROM exports WHERE id=?", (frozen["export_id"],)).fetchone()[0], original)
        self.con.execute("DELETE FROM source_content_policy")
        self.con.execute("UPDATE campaign_reports SET report_json='{}' WHERE export_id=?", (frozen["export_id"],))
        for getter in getters:
            with self.assertRaises(exports.DigestMismatch):
                getter()


class ConversionConsumerRegression(unittest.TestCase):
    def setUp(self):
        self.con = backend_fixtures.make_memory_workspace()
        self.addCleanup(self.con.close)

    def test_all_class_disposition_preserves_history_and_retires_verified_schema(self):
        from earwitness.app import conversion
        before = {table: [tuple(row) for row in self.con.execute(f"SELECT * FROM {table}")]
                  for table in ("source_versions", "reviews", "brand_assocs", "campaign_reports",
                                "customer_alert_events")}
        baseline = self.con.execute("SELECT baseline_json,monitor_json,definition_revision FROM customer_alerts WHERE id=1").fetchone()
        profile = self.con.execute("SELECT value FROM meta WHERE key='agent_profile'").fetchone()[0]
        result = conversion.apply(self.con)
        self.assertEqual(result["pending_definitions"], [], result)
        kinds = {entry["kind"] for entry in result["definitions"]}
        self.assertTrue({"saved_query", "tracker", "origin", "reader_profile", "campaign", "artifact", "source_pin"} <= kinds)
        self.assertEqual(result["marker"]["state"], "complete")
        for table, rows in before.items():
            self.assertEqual([tuple(row) for row in self.con.execute(f"SELECT * FROM {table}")], rows, table)
        self.assertEqual(tuple(self.con.execute("SELECT baseline_json,monitor_json,definition_revision FROM customer_alerts WHERE id=1").fetchone()), tuple(baseline))
        self.assertEqual(self.con.execute("SELECT value FROM meta WHERE key='agent_profile'").fetchone()[0], profile)
        migrated = result["migrated"][0]["item_id"]
        self.assertEqual(customer_saved.get_item(self.con, migrated)["item"]["query"]["q"], "lumen")
        with self.assertRaises(ops.BadInput):
            conversion.require_executable(self.con, "campaign", 1)
        conversion.retire_store(self.con)
        self.assertNotIn("customer_saved_searches", {r[0] for r in self.con.execute("SELECT name FROM sqlite_master WHERE type='table'")})
        self.assertEqual(db.verify_schema(self.con)["problems"], [])
        self.assertIsNone(customer.session_contract_marker(self.con))
        self.con.execute("INSERT INTO meta(key,value) VALUES('customer_cutover', '{\"disposition\":\"converted\"}')")
        self.assertIsNone(customer.session_contract_marker(self.con))
        second = conversion.apply(self.con)
        self.assertEqual(second["migrated"], [])
        self.assertEqual(second["pending_definitions"], [])

    def test_unresolved_definitions_are_nonexecutable_without_rebaseline(self):
        from earwitness.app import conversion
        self.con.execute("UPDATE customer_alerts SET query_json='{\"version\":2,\"mode\":\"mentions\",\"read_styles\":[]}' WHERE id=1")
        before = tuple(self.con.execute("SELECT baseline_json,monitor_json,definition_revision FROM customer_alerts WHERE id=1").fetchone())
        result = conversion.apply(self.con)
        tracker = next(entry for entry in result["definitions"] if entry["kind"] == "tracker")
        self.assertEqual(tracker["disposition"], "quarantined")
        with self.assertRaises(ops.BadInput):
            customer.check_alert(self.con, 1)
        with self.assertRaises(ops.BadInput):
            customer.save_alert(self.con, "rewrite", {"version": 2, "mode": "mentions"}, alert_id=1, expected_revision=1)
        self.assertEqual(tuple(self.con.execute("SELECT baseline_json,monitor_json,definition_revision FROM customer_alerts WHERE id=1").fetchone()), before)

    def test_source_pin_conversion_keeps_original_lineage_and_is_idempotent(self):
        from earwitness.app import conversion
        sv = self.con.execute("SELECT source_version_id FROM evidence WHERE id='fixture-ad'").fetchone()[0]
        old_ref = f"ev:fixture-ad@v{sv}"
        self.con.execute("UPDATE target_evidence SET ref=?", (old_ref,))
        result = conversion.apply(self.con)
        current = self.con.execute("SELECT ref FROM target_evidence").fetchone()[0]
        self.assertTrue(current.startswith("sr1:"))
        self.assertEqual(citations.resolve_source_state(self.con, current)["record_id"], "fixture-ad")
        lineage = self.con.execute("SELECT original_json FROM customer_conversion_lineage WHERE definition_key LIKE 'source_pin:target_evidence/%'").fetchone()[0]
        self.assertEqual(lineage, old_ref)
        self.assertEqual(conversion.apply(self.con)["pending_definitions"], [])


class CurrentAdmissionRegression(unittest.TestCase):
    def setUp(self):
        from tests.test_backend_authority import HostedReaderHttpTest
        self.http = HostedReaderHttpTest()
        self.http.setUp()
        self.addCleanup(self.http.tearDown)

    def _browser_subject(self, subject, *, active=None, verified_email=""):
        import time
        from earwitness.app import browser_auth
        identity = "session-" + subject
        control = self.http.app.store._connect()
        try:
            control.execute("INSERT INTO browser_sessions(session_hash,issuer,subject,email,email_verified,csrf_token,"
                "active_workspace,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (browser_auth._hash_presented(identity), "https://i.example", subject, verified_email,
                 bool(verified_email), "tok123", active or "", time.time(), time.time() + 3600))
            control.commit()
        finally:
            control.close()
        return {"cookie": "ew_session=" + identity, "x-csrf-token": "tok123",
                "origin": "http://127.0.0.1:8771"}

    def _call(self, path, *, headers, method="GET", body=None, query=""):
        import asyncio
        messages = []
        raw = json.dumps(body or {}).encode()
        scope = {"type": "http", "method": method, "path": path, "scheme": "http",
                 "headers": [(b"host", b"127.0.0.1:8771"), (b"content-length", str(len(raw)).encode())] +
                            [(key.encode(), value.encode()) for key, value in headers.items()],
                 "query_string": query.encode()}
        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}
        async def send(message):
            messages.append(message)
        asyncio.run(self.http.app(scope, receive, send))
        return messages[0]["status"], json.loads(b"".join(m.get("body", b"") for m in messages))

    def test_no_member_create_invite_and_exact_named_share_entry(self):
        import sqlite3
        from earwitness.app import collaboration, browser_auth
        headers = self._browser_subject("newcomer")
        status, created = self._call("/api/v1/workspaces", headers=headers, method="POST",
            body={"workspace_id": "new-workspace", "name": "New workspace"})
        self.assertEqual(status, 200, created)
        con = sqlite3.connect(self.http.db_path, isolation_level=None)
        con.row_factory = sqlite3.Row
        owner = "oidc:https://i.example:owner"
        invited = "oidc:https://i.example:newcomer"
        invite = collaboration.create_invite(con, "local", invited, "viewer", owner)
        share = collaboration.create_share(con, "local", "episode", "e1",
            "oidc:https://i.example:guest", ["ep_title"], owner)
        con.close()
        status, accepted = self._call(f"/api/v1/workspaces/acme/invites/{invite['invite_id']}/accept",
            method="POST", headers=headers)
        self.assertEqual(status, 200, accepted)
        guest = self._browser_subject("guest")
        status, title = self._call("/api/v1/shared/search", headers=guest,
            query="workspace_id=acme&share_id=" + share["share_id"] + "&filter=%7B%22query%22%3A%22Morning%22%7D")
        self.assertEqual(status, 200, title)
        self.assertNotIn("excerpt", json.dumps(title))
        status, ordinary = self._call("/api/v1/customer/brands", headers=guest)
        self.assertEqual(status, 409, ordinary)
        status, denied = self._call("/api/v1/shared/transcript", headers=guest,
            query="workspace_id=acme&share_id=" + share["share_id"] + "&episode_id=e1")
        self.assertEqual(status, 403, denied)

    def _capture_race(self, change, *, browser=False):
        import asyncio
        from earwitness.app import services, asgi
        messages = []
        async def scenario():
            captured, release = asyncio.Event(), asyncio.Event()
            async def handler(ctx, con, entry, via_session, session, send):
                ref = citations.current_source_ref(con, "evidence", "fixture-ad")
                result = services.customer_record(ctx, con, entry, ref)
                captured.set()
                await release.wait()
                await self.http.app._send(send, *asgi._json_response(200, result))
            async def send(message):
                messages.append(message)
            headers = self.http._csrf() if browser else {"authorization": "Bearer " + self.http.reader_token}
            request = asyncio.create_task(self.http.app._with_workspace(
                {"type": "http", "method": "GET", "path": "/api/v1/customer/records/captured"},
                None, send, headers, "GET", handler))
            await captured.wait()
            change()
            release.set()
            await request
        asyncio.run(scenario())
        self.assertIn(messages[0]["status"], (401, 403), messages)
        self.assertNotIn(b"Lumen", b"".join(message.get("body", b"") for message in messages))
        con = self.http.app.store._connect()
        self.assertEqual(con.execute("SELECT COUNT(*) FROM agent_audit WHERE outcome='success'").fetchone()[0], 0)
        con.close()

    def test_fresh_source_policy_after_real_captured_snapshot(self):
        import sqlite3
        def change():
            con = sqlite3.connect(self.http.db_path, isolation_level=None)
            con.execute("INSERT INTO source_content_policy(source_key,content_category,agent_disclosure,updated_at)"
                        " VALUES('transcript:e1','transcript','deny','fixture')")
            con.close()
        self._capture_race(change)

    def test_fresh_role_after_real_captured_snapshot(self):
        import sqlite3
        from earwitness.app import collaboration
        def change():
            con = sqlite3.connect(self.http.db_path, isolation_level=None)
            con.row_factory = sqlite3.Row
            collaboration.set_role(con, "local", "ed-1", "viewer", "oidc:https://i.example:owner")
            con.close()
        self._capture_race(change)

    def test_fresh_session_expiry_after_real_captured_snapshot(self):
        def change():
            con = self.http.app.store._connect()
            con.execute("UPDATE browser_sessions SET expires_at=0")
            con.commit()
            con.close()
        self._capture_race(change, browser=True)

    def test_mutation_failure_never_releases_success_or_commits_capture(self):
        import asyncio, sqlite3
        from earwitness.app import asgi, services
        messages = []
        async def handler(ctx, con, entry, via_session, session, send):
            ref = citations.current_source_ref(con, "evidence", "fixture-ad")
            services.customer_save_item(ctx, con, entry,
                {"type": "excerpt", "title": "No commit", "target": {"ref": ref}})
            self.assertEqual(con.execute("SELECT COUNT(*) FROM customer_saved_items WHERE title='No commit'").fetchone()[0], 1)
            con.execute("INSERT INTO source_content_policy(source_key,content_category,agent_disclosure,updated_at)"
                        " VALUES('transcript:e1','transcript','deny','fixture')")
            await self.http.app._send(send, *asgi._json_response(200, {"ok": True}))
        async def send(message):
            messages.append(message)
        status, current = self.http._call("/api/v1/session", method="GET", headers=self.http._csrf())
        self.assertEqual(status, 200)
        asyncio.run(self.http.app._with_workspace(
            {"type": "http", "method": "POST", "path": "/api/v1/customer/saved-items", "scheme": "http"},
            None, send, {**self.http._csrf(), "host": "127.0.0.1:8771",
                         "x-earwitness-workspace-switch-revision": str(current["workspace_switch_revision"])},
            "POST", handler))
        self.assertEqual(messages[0]["status"], 403, messages)
        con = sqlite3.connect(self.http.db_path)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM customer_saved_items WHERE title='No commit'").fetchone()[0], 0)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM source_content_policy").fetchone()[0], 0)
        con.close()


class NativeDisclosureRegression(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from earwitness.app import agent_contracts, connections
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name) / "native"
        self.path = backend_fixtures.make_workspace(self.workspace, schema_version=8)
        connections.prepare_local_reader_control(self.workspace, verification_context="local_fixture")
        agent_contracts.bind_workspace("local", self.workspace)
        self.scopes = {"research:read", "transcripts:read"}

    def test_actual_local_use_receipt_is_pure_and_profile_bound(self):
        from earwitness import agents
        from earwitness.app import connections
        con = db.open_existing(self.path, readonly=True)
        before = con.total_changes, db.aggregate_revision(con)
        self.assertFalse(connections.local_reader_status(con)["verified"])
        agents.call_tool("local", self.scopes, "get_workspace_context", {})
        self.assertFalse(connections.local_reader_status(con)["verified"])
        agents.call_tool("local", self.scopes, "search_evidence", {"filter": {"query": "Lumen"}})
        self.assertEqual(connections.local_reader_status(con)["state"], "local_fixture")
        self.assertEqual((con.total_changes, db.aggregate_revision(con)), before)
        con.close()
        writer = db.open_existing(self.path, readonly=False)
        profile = json.loads(writer.execute("SELECT value FROM meta WHERE key='agent_profile'").fetchone()[0])
        profile["revoked"] = True
        writer.execute("UPDATE meta SET value=? WHERE key='agent_profile'", (json.dumps(profile),))
        writer.close()
        con = db.open_existing(self.path, readonly=True)
        self.assertFalse(connections.local_reader_status(con)["verified"])
        con.close()

    def test_native_hidden_notes_and_source_policy_do_not_leak_through_alternate_paths(self):
        from earwitness.app import agent_contracts, connections, access
        con = db.open_existing(self.path, readonly=False)
        ops.save_review(con, "fixture-ad", {"notes": "secret-only-native-needle"}, 1)
        ctx = connections.local_reader_context("local", self.workspace, {"research:read"})
        before = agent_contracts.get_evidence(ctx, {"evidence_id": "fixture-ad"})["items"][0]
        self.assertNotIn("revision", before["review"])
        self.assertNotIn("revision_ref", before)
        brands = agent_contracts.list_brands(ctx, {"filter": {"query": "secret-only-native-needle"}})
        self.assertEqual(brands["total_matches"], 0)
        ops.save_review(con, "fixture-ad", {"notes": "another private note"}, 2)
        after = agent_contracts.get_evidence(ctx, {"evidence_id": "fixture-ad"})["items"][0]
        self.assertEqual(before, after)
        con.execute("INSERT INTO source_content_policy(source_key,content_category,agent_disclosure,updated_at)"
                    " VALUES('transcript:e1','transcript','deny','fixture')")
        ctx = connections.local_reader_context("local", self.workspace, self.scopes)
        result = agent_contracts.search_transcripts(ctx, {"query": "brought to you"})
        self.assertEqual(result["total_matches"], 0)
        tv = con.execute("SELECT transcript_version_id FROM episodes WHERE id='e1'").fetchone()[0]
        with self.assertRaises(access.AuthorizationError):
            agent_contracts.get_transcript_window(ctx, {"episode_id": "e1", "transcript_version_id": tv,
                                                      "start_s": 10, "end_s": 20})
        con.close()


class LocalOutputRegression(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from scripts import app_server
        self.server = app_server
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name) / "local"
        self.path = backend_fixtures.make_workspace(self.workspace, schema_version=8)
        previous = app_server._state["workspace"]
        app_server._state["workspace"] = self.workspace
        self.addCleanup(app_server._state.__setitem__, "workspace", previous)

    def _handler(self, kind):
        import io
        handler = kind.__new__(kind)
        handler.wfile = io.BytesIO()
        handler.request_version = "HTTP/1.1"
        handler.requestline = "GET /api/v1/customer/records/source HTTP/1.1"
        handler.path = "/api/v1/customer/records/source"
        handler.command = "GET"
        return handler

    def test_captured_loopback_bytes_are_withheld_after_independent_source_denial(self):
        import sqlite3
        con = db.open_existing(self.path, readonly=True)
        ref = citations.current_source_ref(con, "evidence", "fixture-ad")
        con.close()
        path = self.path
        class BarrierHandler(self.server.Handler):
            def _route_get(handler, route, query):
                super()._route_get(route, query)
                handler.actual_capture = b"Lumen" in handler.wfile.getvalue()
                authority = sqlite3.connect(path, isolation_level=None)
                authority.execute("INSERT INTO source_content_policy(source_key,content_category,agent_disclosure,updated_at)"
                                  " VALUES('transcript:e1','transcript','deny','fixture')")
                authority.close()
        handler = self._handler(BarrierHandler)
        transport = handler.wfile
        with self.assertRaises(self.server.AppError) as denied:
            handler._protected_dispatch(handler._route_get, "/customer/records/" + ref, {}, write=False)
        self.assertEqual(denied.exception.status, 403)
        self.assertTrue(handler.actual_capture)
        self.assertEqual(transport.getvalue(), b"")

    def test_loopback_capture_and_outer_failure_roll_back_before_success_bytes(self):
        from earwitness.app import customer_saved
        class DenyingHandler(self.server.Handler):
            def _fresh_local_admission(handler, ctx):
                handler._product_connection.execute(
                    "INSERT INTO source_content_policy(source_key,content_category,agent_disclosure,updated_at)"
                    " VALUES('transcript:e1','transcript','deny','fixture')")
                return super()._fresh_local_admission(ctx)
        handler = self._handler(DenyingHandler)
        transport = handler.wfile
        def route():
            con = handler._request_db()
            ref = citations.current_source_ref(con, "evidence", "fixture-ad")
            item = handler._customer_errors(customer_saved.create_item, con,
                {"type": "excerpt", "title": "Rollback quote", "target": {"ref": ref}}, workspace_context=None)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM customer_saved_items WHERE title='Rollback quote'").fetchone()[0], 1)
            handler._send_json({"ok": True, **item})
        with self.assertRaises(self.server.AppError) as denied:
            handler._protected_dispatch(route, write=True)
        self.assertEqual(denied.exception.status, 403)
        self.assertEqual(transport.getvalue(), b"")
        con = db.open_existing(self.path, readonly=True)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM customer_saved_items WHERE title='Rollback quote'").fetchone()[0], 0)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM source_content_policy").fetchone()[0], 0)
        con.close()


class CustomerListRegression(unittest.TestCase):
    """Customer campaign directory rows and report history lists."""

    def setUp(self):
        from tests.test_backend_allocation import _con
        self.con = _con()
        self.addCleanup(self.con.close)
        self.entry = {"row_namespace": "local", "workspace_id": "local", "is_owner": True}

    def _owner(self):
        from earwitness.app import access
        return access.AccessContext.create(principal_id="local-owner", principal_kind="owner",
            workspace_id="local", credential_kind="local_operator")

    def test_directory_rows_carry_counts_state_and_availability_without_draft_builds(self):
        from unittest import mock
        from earwitness.app import campaigns, services
        from tests.test_backend_allocation import _brief, _supplied, _placement
        supplied = _supplied(self.con, _brief(self.con))["campaign"]
        first = _placement(self.con, supplied["id"], supplied["revision"])
        _placement(self.con, supplied["id"], first["campaign_revision"], episode_id="e2")
        research = campaigns.save_customer_campaign(self.con, {"campaign": {
            "name": "Research work", "mode": "research", "brand": "Lumen"}})["campaign"]
        campaigns.create_target(self.con, research["id"], "episode", episode_id="e1",
                                expected_campaign_revision=research["revision"])
        legacy = self.con.execute(
            "INSERT INTO campaigns (name, status, mode, updated_at) VALUES ('Old stored', 'active', NULL, ?)",
            (db.utcnow(),)).lastrowid
        self.con.commit()
        with mock.patch.object(campaigns, "build_customer_draft",
                               side_effect=AssertionError("no draft build for a directory row")):
            rows = {row["id"]: row for row in
                    services.customer_campaigns(self._owner(), self.con, self.entry)["campaigns"]}
        self.assertEqual((rows[supplied["id"]]["placement_count"], rows[supplied["id"]]["target_count"]), (2, 0))
        self.assertEqual(rows[supplied["id"]]["state"], "Tracking")
        self.assertEqual(rows[supplied["id"]]["availability"], "active")
        self.assertEqual(rows[supplied["id"]]["mode"], "supplied_requirements")
        self.assertEqual((rows[research["id"]]["placement_count"], rows[research["id"]]["target_count"]), (0, 1))
        self.assertEqual(rows[research["id"]]["state"], "Researching")  # DEMO2-R3-01
        self.assertEqual(rows[research["id"]]["availability"], "active")
        self.assertIsNone(rows[legacy]["mode"])
        self.assertEqual(rows[legacy]["availability"], "legacy_review_required")
        # Backward compatible: the stored columns are still there.
        self.assertEqual(rows[legacy]["name"], "Old stored")
        self.assertIn("revision", rows[supplied["id"]])
        self.assertFalse(self.con.in_transaction, "the read transaction is closed")

    def test_report_list_keeps_every_report_and_marks_an_unverifiable_one(self):
        from earwitness.app import campaigns, exports
        from tests.test_backend_allocation import _brief, _supplied, _placement
        camp = _supplied(self.con, _brief(self.con))["campaign"]
        _placement(self.con, camp["id"], camp["revision"])
        draft = campaigns.build_customer_draft(self.con, camp["id"])
        bad = campaigns.freeze_customer_report(self.con, camp["id"], draft["campaign"]["revision"], draft["pins"])
        draft = campaigns.build_customer_draft(self.con, camp["id"])
        good = campaigns.freeze_customer_report(self.con, camp["id"], draft["campaign"]["revision"], draft["pins"])
        self.con.execute("UPDATE exports SET payload = payload || ' ' WHERE id=?", (bad["export_id"],))
        self.con.commit()
        listed = {row["report_no"]: row for row in campaigns.list_customer_reports(self.con, camp["id"])}
        self.assertEqual(sorted(listed), [bad["report_no"], good["report_no"]])
        self.assertIs(listed[bad["report_no"]]["available"], False)
        self.assertEqual(listed[bad["report_no"]]["unavailable_reason"], "unverifiable")
        self.assertIs(listed[good["report_no"]]["available"], True)
        self.assertNotIn("unavailable_reason", listed[good["report_no"]])
        for row in listed.values():
            self.assertFalse({"payload", "frozen", "report"} & set(row), "no report bytes in the list")
        # Opening the unverifiable report still refuses; the good one opens.
        for getter in (campaigns.get_customer_report, campaigns.get_customer_report_html):
            with self.assertRaises(exports.DigestMismatch):
                getter(self.con, camp["id"], bad["report_no"])
        self.assertIn("frozen", campaigns.get_customer_report(self.con, camp["id"], good["report_no"]))
