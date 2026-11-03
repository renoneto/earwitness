"""Customer persistence/migration/alert tests (Product-9 §4.3–§4.4)."""
from __future__ import annotations

import json
import sqlite3
import unittest

from earwitness.app import customer, customer_saved, db, ops, search
from earwitness.app import customer_contracts, customer_query


def fresh_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON;")
    db.init_schema(con)
    return con


def seed_corpus(con, extra_segments=None):
    meta = [
        {"ep_id": "s1", "show_title": "Show One",
         "feed_url": "https://one.example/rss", "ep_title": "First",
         "pub_date": "2026-03-10", "category": "business"},
        {"ep_id": "s2", "show_title": "Show Two",
         "feed_url": "https://two.example/rss", "ep_title": "Second",
         "pub_date": "2026-03-11", "category": "business"},
    ]
    cur = con.execute(
        "INSERT INTO source_versions (source_key, version_no, sha256, payload,"
        " is_active, created_at) VALUES ('episodes.json', 1, 'sh', ?, 1, ?)",
        (json.dumps(meta), db.utcnow()))
    ep_version = cur.lastrowid
    segments = {"s1": [{"start": 0.0, "end": 10.0, "text": "acme bright widget"}],
                "s2": [{"start": 0.0, "end": 10.0, "text": "quiet weather notes"}]}
    if extra_segments:
        segments.update(extra_segments)
    for ep_id, segs in segments.items():
        cur = con.execute(
            "INSERT INTO source_versions (source_key, version_no, sha256, payload,"
            " is_active, created_at) VALUES (?, 1, ?, ?, 1, ?)",
            (f"transcript:{ep_id}", "sh-" + ep_id, json.dumps({"segments": segs}),
             db.utcnow()))
        tv = cur.lastrowid
        row = next(m for m in meta if m["ep_id"] == ep_id)
        con.execute(
            "INSERT INTO episodes (id, kind, show_title, ep_title, category, pub_date,"
            " fetch_date, transcript_version_id, revision, updated_at)"
            " VALUES (?, 'supplied', ?, ?, ?, ?, '2026-09-21', ?, 1, ?)",
            (ep_id, row["show_title"], row["ep_title"], row["category"],
             row["pub_date"], tv, db.utcnow()))
    from tests.backend_fixtures import publish_all_records
    if db._schema_version(con) >= 8 and db.has_v8_tables(con):
        publish_all_records(con)
    con.commit()
    return ep_version


def add_evidence(con, ev_id, episode_id, excerpt, judgment=None,
                 brands=(), attrs=None):
    con.execute(
        "INSERT INTO evidence (id, source, source_version_id, episode_id, orig_start_s,"
        " orig_end_s, excerpt, attrs, revision, created_at, transcript_version_id,"
        " source_record_key, assoc_revision, record_kind)"
        " VALUES (?, 'detector', 1, ?, 0.0, 10.0, ?, ?, 1, ?, "
        " (SELECT transcript_version_id FROM episodes WHERE id=?), ?, 0, 'candidate')",
        (ev_id, episode_id, excerpt, json.dumps(attrs or {}), db.utcnow(),
         episode_id, ev_id))
    if judgment is not None:
        con.execute("INSERT INTO reviews (evidence_id, judgment, revision, updated_at)"
                    " VALUES (?, ?, 1, ?)", (ev_id, judgment, db.utcnow()))
    for brand, basis in brands:
        con.execute("INSERT INTO brand_assocs (evidence_id, brand, basis, origin,"
                    " revision, updated_at) VALUES (?, ?, ?, 'test', 1, ?)",
                    (ev_id, brand, basis, db.utcnow()))
    db.record_evidence_snapshot(con, ev_id, "seed")
    from earwitness.app import citations as _citations
    if db._schema_version(con) >= 8 and db.has_v8_tables(con):
        _citations.publish_source_state(con, kind="evidence", record_id=ev_id, actor="fixture")
        _citations.publish_source_state(con, kind="episode", record_id=episode_id, actor="fixture")
    con.commit()


def query_acme():
    return {"version": 2, "mode": "mentions", "q": "acme"}


class MigrationTest(unittest.TestCase):
    def test_v5_to_v6_preserves_owner_state(self):
        con = fresh_db()
        try:
            # Return to v5 on an empty workspace, then populate owner state.
            db.migrate_8_to_7(con)
            db.migrate_7_to_6(con)
            db.migrate_6_to_5(con)
            self.assertEqual(
                con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0], 5)
            seed_corpus(con)
            add_evidence(con, "ev-keep", "s1", "keep me", "ad", [("acme", "confirmed")])
            con.execute("INSERT INTO watchlists (name, query, revision, updated_at)"
                        " VALUES ('legacy', '{}', 1, ?)", (db.utcnow(),))
            con.execute("INSERT INTO media_artifacts (id, workspace_id, digest, bytes,"
                        " format, locator, fetch_date, fetch_date_source, created_at)"
                        " VALUES ('m1', 'local', 'sh', 8, 'mp3', '/tmp/a.mp3',"
                        " '2026-09-21', 'test', ?)", (db.utcnow(),))
            profile = {"profile": "reader", "scopes": ["search"],
                       "owner": "owner-1", "workspace_id": "local",
                       "updated_at": db.utcnow()}
            con.execute("INSERT INTO meta (key, value) VALUES ('agent_profile', ?)",
                        (json.dumps(profile),))
            identity = db.ensure_workspace_identity(con)
            con.commit()
            # Upgrade twice: idempotent, values preserved, profile untouched.
            db.migrate_5_to_6(con)
            db.migrate_5_to_6(con)
            db.migrate_6_to_7(con)
            db.migrate_6_to_7(con)
            db.migrate_7_to_8(con)
            db.migrate_7_to_8(con)
            self.assertEqual(
                con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0], 8)
            self.assertEqual(
                con.execute("SELECT excerpt FROM evidence WHERE id='ev-keep'").fetchone()[0],
                "keep me")
            self.assertEqual(
                con.execute("SELECT name FROM watchlists WHERE name='legacy'").fetchone()[0],
                "legacy")
            stored = json.loads(con.execute(
                "SELECT value FROM meta WHERE key='agent_profile'").fetchone()[0])
            self.assertEqual(stored, profile)
            self.assertEqual(db.workspace_identity(con), identity)
            self.assertEqual(db.verify_schema(con)["problems"], [])
            self.assertFalse(con.execute("PRAGMA foreign_key_check").fetchall())
        finally:
            con.close()

    def test_downgrade_refuses_with_customer_data(self):
        con = fresh_db()
        try:
            seed_corpus(con)
            customer.save_alert(con, "s", query_acme())
            with self.assertRaises(RuntimeError):
                db.migrate_6_to_5(con)
        finally:
            con.close()


class AlertLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        seed_corpus(self.con)
        add_evidence(self.con, "ev-a", "s1", "acme launch")

    def tearDown(self):
        self.con.close()

    def test_baseline_then_new_match_emits_once(self):
        saved = customer.save_alert(self.con, "watch acme", query_acme())
        saved = customer.set_alert_lifecycle(self.con, saved["id"],
                                             "active_local", saved["revision"])
        first = customer.check_alert(self.con, saved["id"])
        self.assertTrue(first["ok"])
        self.assertEqual(first["emitted"], 0)  # existing matches are not news
        add_evidence(self.con, "ev-b", "s1", "more acme news")
        second = customer.check_alert(self.con, saved["id"])
        self.assertEqual(second["emitted"], 1)
        third = customer.check_alert(self.con, saved["id"])
        self.assertEqual(third["emitted"], 0)
        events = customer.get_alert(self.con, saved["id"])["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["read_state"], 0)

    def test_review_transition_emits_without_new_text(self):
        saved = customer.save_alert(
            self.con, "watch acme",
            {"version": 2, "mode": "ads", "q": "acme"})
        saved = customer.set_alert_lifecycle(self.con, saved["id"],
                                             "active_local", saved["revision"])
        self.assertEqual(customer.check_alert(self.con, saved["id"])["emitted"], 0)
        ops.save_review(self.con, "ev-a", {"judgment": "ad"}, 0)
        ops.set_brand_assocs(self.con, "ev-a",
            [{"brand": "acme", "basis": "confirmed", "channel": "spoken"}], 0)
        self.assertEqual(customer.check_alert(self.con, saved["id"])["emitted"], 1)

    def test_resegmentation_keeps_identity(self):
        saved = customer.save_alert(self.con, "watch acme", query_acme())
        saved = customer.set_alert_lifecycle(self.con, saved["id"],
                                             "active_local", saved["revision"])
        customer.check_alert(self.con, saved["id"])
        # Same canonical text, new segmentation/version: identity retained.
        tv = self.con.execute(
            "SELECT transcript_version_id FROM episodes WHERE id='s1'").fetchone()[0]
        self.con.execute("UPDATE source_versions SET is_active=0 WHERE id=?", (tv,))
        cur = self.con.execute(
            "INSERT INTO source_versions (source_key, version_no, sha256, payload,"
            " is_active, created_at) VALUES ('transcript:s1', 2, 'sh2', ?, 1, ?)",
            (json.dumps({"segments": [{"start": 0.0, "end": 5.0, "text": "acme bright"},
                                       {"start": 5.0, "end": 10.0, "text": "widget"}]}),
             db.utcnow()))
        self.con.execute("UPDATE episodes SET transcript_version_id=?, revision=revision+1"
                         " WHERE id='s1'", (cur.lastrowid,))
        self.con.commit()
        out = customer.check_alert(self.con, saved["id"])
        kinds = [e["kind"] for e in customer.get_alert(self.con, saved["id"])["events"]]
        self.assertNotIn("new_text", kinds)
        self.assertTrue(out["ok"])

    def test_pause_resume_stop(self):
        saved = customer.save_alert(self.con, "watch acme", query_acme())
        paused = customer.set_alert_lifecycle(
            self.con, saved["id"], "paused", saved["revision"])
        self.assertEqual(paused["lifecycle"], "paused")
        with self.assertRaises(ops.BadInput):
            customer.check_alert(self.con, saved["id"])
        resumed = customer.set_alert_lifecycle(
            self.con, saved["id"], "active_local", paused["revision"])
        add_evidence(self.con, "ev-c", "s1", "acme again")
        out = customer.check_alert(self.con, saved["id"])
        self.assertEqual(out["emitted"], 1)  # one bounded catch-up, no flood
        stopped = customer.set_alert_lifecycle(
            self.con, saved["id"], "stopped", resumed["revision"])
        self.assertEqual(stopped["lifecycle"], "stopped")
        with self.assertRaises(ops.BadInput):
            customer.check_alert(self.con, saved["id"])
        # History survives the stop.
        self.assertTrue(customer.get_alert(self.con, saved["id"])["events"])

    def test_name_edit_keeps_baseline_membership_edit_invalidates(self):
        saved = customer.save_alert(self.con, "watch acme", query_acme())
        saved = customer.set_alert_lifecycle(self.con, saved["id"],
                                             "active_local", saved["revision"])
        customer.check_alert(self.con, saved["id"])
        before = customer.get_alert(self.con, saved["id"])
        self.assertTrue(before["baseline_complete"])
        renamed = customer.save_alert(
            self.con, "renamed", query_acme(), alert_id=saved["id"],
            expected_revision=saved["revision"])
        self.assertEqual(renamed["definition_revision"],
                         before["definition_revision"])
        self.assertTrue(customer.get_alert(
            self.con, saved["id"])["baseline_complete"])
        add_evidence(self.con, "ev-d", "s1", "acme storm")
        redefined = customer.save_alert(
            self.con, "renamed",
            {"version": 2, "mode": "mentions", "q": "storm"},
            alert_id=saved["id"], expected_revision=renamed["revision"])
        self.assertGreater(redefined["definition_revision"],
                           before["definition_revision"])
        # The new definition is invalidated, not silently completed: the
        # prior baseline stays as history with no claimed success.
        invalidated = customer.get_alert(self.con, saved["id"])
        self.assertFalse(invalidated["baseline_complete"])
        self.assertEqual(invalidated["last_successful_definition_revision"],
                         before["definition_revision"])
        # The next successful check establishes the new definition
        # silently: no flood of history.
        self.assertEqual(customer.check_alert(self.con, saved["id"])["emitted"], 0)
        self.assertTrue(customer.get_alert(
            self.con, saved["id"])["baseline_complete"])

    def test_mark_read_only_supplied_ids(self):
        saved = customer.save_alert(self.con, "watch acme", query_acme())
        saved = customer.set_alert_lifecycle(self.con, saved["id"],
                                             "active_local", saved["revision"])
        add_evidence(self.con, "ev-e", "s1", "acme event")
        customer.check_alert(self.con, saved["id"])
        events = customer.get_alert(self.con, saved["id"])["events"]
        self.assertEqual(len(events), 1)
        out = customer.mark_alert_events_read(self.con, saved["id"], [events[0]["id"]])
        self.assertEqual(out["marked"], 1)
        out = customer.mark_alert_events_read(
            self.con, saved["id"], [events[0]["id"], 999999])
        self.assertEqual(out["marked"], 1)  # foreign/concurrent IDs never marked

    def test_backfill_historical_idempotent(self):
        saved = customer.save_alert(self.con, "watch acme", query_acme())
        saved = customer.set_alert_lifecycle(self.con, saved["id"],
                                             "active_local", saved["revision"])
        with self.assertRaises(search.FilterRejected):
            customer.backfill_alert(self.con, saved["id"], confirm=False)
        first = customer.backfill_alert(self.con, saved["id"], confirm=True)
        self.assertTrue(first["historical"])
        self.assertGreater(first["imported"], 0)
        second = customer.backfill_alert(self.con, saved["id"], confirm=True)
        self.assertEqual(second["imported"], 0)
        events = customer.get_alert(self.con, saved["id"])["events"]
        self.assertTrue(all(e["historical"] == 1 for e in events))

    def test_no_external_side_effects(self):
        before_schedules = self.con.execute("SELECT COUNT(*) FROM schedules").fetchone()[0]
        before_outbox = self.con.execute(
            "SELECT COUNT(*) FROM notification_outbox").fetchone()[0]
        profile_before = self.con.execute(
            "SELECT value FROM meta WHERE key='agent_profile'").fetchone()
        saved = customer.save_alert(self.con, "watch acme", query_acme(),
                                    delivery={"email": "a@example.com",
                                              "cadence": "daily"})
        saved = customer.set_alert_lifecycle(self.con, saved["id"],
                                             "active_local", saved["revision"])
        customer.check_alert(self.con, saved["id"])
        customer.backfill_alert(self.con, saved["id"], confirm=True)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM schedules").fetchone()[0],
                         before_schedules)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM notification_outbox").fetchone()[0], before_outbox)
        self.assertEqual(self.con.execute(
            "SELECT value FROM meta WHERE key='agent_profile'").fetchone(),
            profile_before)


class AlertSetupStateTest(unittest.TestCase):
    """Partial-scan alert retention: setup/error state, no baseline (§4.4)."""
    def setUp(self):
        self.con = fresh_db()
        seed_corpus(self.con)
        add_evidence(self.con, "ev-a", "s1", "acme launch")
        self._cap = customer.SCAN_SEGMENT_CAP

    def tearDown(self):
        customer.SCAN_SEGMENT_CAP = self._cap
        customer_query._VIEW_MEMO.clear()  # the cap is an input outside the memo key
        self.con.close()

    def test_partial_create_retains_definition_with_setup_state(self):
        customer.SCAN_SEGMENT_CAP = 1
        customer_query._VIEW_MEMO.clear()  # the cap is an input outside the memo key
        saved = customer.save_alert(self.con, "watch acme", query_acme())
        resumed = customer.set_alert_lifecycle(self.con, saved["id"],
                                               "active_local", saved["revision"])
        self.assertTrue(saved.get("setup_required"))
        self.assertEqual(len(customer.list_alerts(self.con)), 1)
        alert = customer.get_alert(self.con, saved["id"])
        self.assertIsNone(alert["last_successful_at"])
        self.assertFalse(alert["baseline_complete"])
        self.assertEqual(alert["baseline_count"], 0)
        self.assertEqual(alert["last_attempt_result"], "setup")
        self.assertEqual(alert["events"], [])
        # Recovery: the first complete scan establishes the baseline
        # silently — matches already present are not news.
        customer.SCAN_SEGMENT_CAP = self._cap
        customer_query._VIEW_MEMO.clear()  # the cap is an input outside the memo key
        out = customer.check_alert(self.con, saved["id"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["emitted"], 0)
        self.assertIsNotNone(
            customer.get_alert(self.con, saved["id"])["last_successful_at"])

    def test_failed_check_after_edit_retains_baseline_and_history(self):
        saved = customer.save_alert(self.con, "watch acme", query_acme())
        first = customer.check_alert(self.con, saved["id"])
        before = customer.get_alert(self.con, saved["id"])
        self.assertTrue(before["baseline_complete"])
        # A membership edit persists the new definition invalidated; the
        # prior baseline stays as history with no claimed success.
        edited = customer.save_alert(
            self.con, "watch acme",
            {"version": 2, "mode": "mentions", "q": "acme storm"},
            alert_id=saved["id"], expected_revision=saved["revision"])
        self.assertGreater(edited["definition_revision"],
                           before["definition_revision"])
        self.assertFalse(edited["baseline_complete"])
        # The failed attempt retains baseline/history and the historical
        # success definition while advancing the resource revision.
        customer.SCAN_SEGMENT_CAP = 1
        customer_query._VIEW_MEMO.clear()  # the cap is an input outside the memo key
        out = customer.check_alert(self.con, saved["id"])
        customer.SCAN_SEGMENT_CAP = self._cap
        customer_query._VIEW_MEMO.clear()  # the cap is an input outside the memo key
        self.assertFalse(out["ok"])
        self.assertGreater(out["resource_revision"],
                           first["resource_revision"])
        after = customer.get_alert(self.con, saved["id"])
        self.assertEqual(after["definition_revision"],
                         edited["definition_revision"])
        self.assertFalse(after["baseline_complete"])
        self.assertEqual(after["baseline_count"], before["baseline_count"])
        self.assertEqual(after["last_successful_at"],
                         before["last_successful_at"])
        self.assertEqual(after["last_successful_definition_revision"],
                         before["definition_revision"])
        self.assertEqual(after["error"], out["error"])
        self.assertEqual(after["last_attempt_result"], "error")
        # Recovery establishes the new definition silently.
        recovered = customer.check_alert(self.con, saved["id"])
        self.assertTrue(recovered["ok"])
        self.assertEqual(recovered["emitted"], 0)
        self.assertTrue(customer.get_alert(
            self.con, saved["id"])["baseline_complete"])

    def test_pause_resume_preserves_setup_state(self):
        customer.SCAN_SEGMENT_CAP = 1
        customer_query._VIEW_MEMO.clear()  # the cap is an input outside the memo key
        saved = customer.save_alert(self.con, "watch acme", query_acme())
        self.assertTrue(saved.get("setup_required"))
        paused = customer.set_alert_lifecycle(
            self.con, saved["id"], "paused", saved["revision"])
        resumed = customer.set_alert_lifecycle(
            self.con, saved["id"], "active_local", paused["revision"])
        self.assertEqual(resumed["lifecycle"], "active_local")
        alert = customer.get_alert(self.con, saved["id"])
        self.assertIsNone(alert["last_successful_at"])
        self.assertFalse(alert["baseline_complete"])
        self.assertEqual(alert["last_attempt_result"], "setup")
        self.assertEqual(alert["events"], [])
        # The first successful check still establishes the baseline
        # silently: pause/resume never bypasses it.
        customer.SCAN_SEGMENT_CAP = self._cap
        customer_query._VIEW_MEMO.clear()  # the cap is an input outside the memo key
        out = customer.check_alert(self.con, saved["id"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["emitted"], 0)

    def test_backfill_refused_without_completed_baseline(self):
        customer.SCAN_SEGMENT_CAP = 1
        customer_query._VIEW_MEMO.clear()  # the cap is an input outside the memo key
        saved = customer.save_alert(self.con, "watch acme", query_acme())
        with self.assertRaises(ops.BadInput):
            customer.backfill_alert(self.con, saved["id"], confirm=True)
        self.assertEqual(
            customer.get_alert(self.con, saved["id"])["events"], [])

    def test_mode_event_choices_enforced(self):
        with self.assertRaises(search.FilterRejected):
            customer.save_alert(
                self.con, "bad",
                {"version": 2, "mode": "ads", "q": "acme"},
                event_types=["new_episode"])
        with self.assertRaises(search.FilterRejected):
            customer.save_alert(
                self.con, "bad",
                {"version": 2, "mode": "episodes", "q": ""},
                event_types=["new_text"])
        saved = customer.save_alert(
            self.con, "ads watch",
            {"version": 2, "mode": "ads", "q": "acme"})
        dto = customer.get_alert(self.con, saved["id"])
        self.assertEqual(dto["query"]["mode"], "ads")
        self.assertEqual(dto["baseline_count"], dto["current_matches"]["count"])


class MistakeNoteTest(unittest.TestCase):
    def test_note_pinned_and_review_untouched(self):
        con = fresh_db()
        try:
            seed_corpus(con)
            add_evidence(con, "ev-m", "s1", "acme claim", "ad", [("acme", "confirmed")])
            add_evidence(con, "ev-m2", "s1", "acme second", "ad", [("acme", "confirmed")])
            full = customer.query(con, customer_contracts.upcast_query_v1({"mode": "ads", "q": "acme"})[0])
            refs = sorted(i["ref"] for i in full["items"])
            ref, other = refs[0], refs[1]
            saved = customer.save_mistake(con, ref, "wrong span")
            self.assertEqual(saved["revision"], 1)
            review = con.execute("SELECT judgment, revision FROM reviews"
                                 " WHERE evidence_id='ev-m'").fetchone()
            self.assertEqual(review["judgment"], "ad")
            self.assertEqual(review["revision"], 1)
            # Moving a note to new text is rejected; history stays reachable.
            with self.assertRaises(ops.BadInput):
                customer.save_mistake(con, other, "move", mistake_id=saved["id"],
                                      expected_revision=1)
            from earwitness.app import customer_saved
            notes = customer_saved.list_notes(con, ref=ref)["notes"]
            self.assertEqual([note["id"] for note in notes], [saved["id"]])
            self.assertEqual(notes[0]["ref"], ref)
        finally:
            con.close()


class PreferencesTest(unittest.TestCase):
    def test_proposals_never_change_grants(self):
        con = fresh_db()
        try:
            seed_corpus(con)
            profile = {"profile": "reader", "scopes": ["search"],
                       "owner": "owner-1", "workspace_id": "local",
                       "updated_at": db.utcnow()}
            con.execute("INSERT INTO meta (key, value) VALUES ('agent_profile', ?)",
                        (json.dumps(profile),))
            con.commit()
            settings = ops.get_settings(con)
            saved = customer.save_preferences(
                con, {"workspace_name": "Research", "share_audience": "named_reviewers",
                      "reader_scope": ["search"]}, settings["revision"])
            self.assertIn("proposal", saved["preferences"]["_proposal_notice"])
            stored = json.loads(con.execute(
                "SELECT value FROM meta WHERE key='agent_profile'").fetchone()[0])
            self.assertEqual(stored, profile)
            with self.assertRaises(search.FilterRejected):
                customer.save_preferences(con, {"share_audience": "world"},
                                          saved["revision"])
        finally:
            con.close()


class PreferenceEnvelopeTest(unittest.TestCase):
    """Envelope/domain separation: the revision travels outside the
    preference body; the domain never accepts it as a preference key."""

    def test_revision_key_is_not_a_preference(self):
        con = fresh_db()
        try:
            seed_corpus(con)
            settings = ops.get_settings(con)
            with self.assertRaises(search.FilterRejected):
                customer.save_preferences(
                    con, {"workspace_name": "Research",
                          "expected_revision": settings["revision"]},
                    settings["revision"])
            saved = customer.save_preferences(
                con, {"workspace_name": "Research"}, settings["revision"])
            self.assertEqual(saved["preferences"]["workspace_name"], "Research")
            self.assertNotIn("expected_revision", saved["preferences"])
        finally:
            con.close()

    def test_empty_sort_stays_rejected_by_domain(self):
        # JSON saves normalize empty optionals in the adapter; the domain
        # validator itself stays strict.
        with self.assertRaises(search.FilterRejected):
            customer_contracts.upcast_query_v1({"mode": "ads", "q": "shopify", "sort": ""})[0]

    def test_stale_settings_write_conflicts_and_preserves(self):
        con = fresh_db()
        try:
            seed_corpus(con)
            first = customer.get_preferences(con)
            # Another page saves first.
            customer.save_preferences(
                con, {"workspace_name": "Concurrent acceptance name"},
                first["revision"])
            with self.assertRaises(ops.Conflict):
                customer.save_preferences(
                    con, {"workspace_name": "Preserved failure draft"},
                    first["revision"])
            stored = customer.get_preferences(con)
            self.assertEqual(stored["workspace_name"],
                             "Concurrent acceptance name")
        finally:
            con.close()


class MediaTruthTest(unittest.TestCase):
    """Customer audio truth: remote streams are reported, never players;
    only exact local media yields a player descriptor."""

    def setUp(self):
        from tests.backend_fixtures import make_file_workspace
        self.con = make_file_workspace()
        seed_corpus(self.con)
        add_evidence(self.con, "ev-a", "s1", "acme launch")

    def tearDown(self):
        self.con.close()

    def current_record(self):
        from earwitness.app import citations
        return customer.record(self.con, citations.current_source_ref(self.con, "evidence", "ev-a"))

    def test_remote_stream_is_reported_never_a_player(self):
        ops.set_episode_media(self.con, "s1", {"audio_url": "https://cdn.example/ep.mp3"}, 1)
        media = self.current_record()["media"]
        self.assertEqual(media["playback"], "remote")
        self.assertEqual(media["timing"], "unknown")
        self.assertNotIn("recording_id", media)
        rows = customer.query(self.con, {"version": 2, "mode": "ads", "q": "acme"})["items"]
        row = next(item for item in rows if item["evidence_id"] == "ev-a")
        self.assertEqual(row["audio"], "available")
        self.assertFalse(row["audio_local"])

    def test_registered_clip_yields_local_player(self):
        from tests.backend_fixtures import register_synthetic_recording
        recording = register_synthetic_recording(self.con, "s1", offset_s=3, duration_s=5)
        ops.save_review(self.con, "ev-a", {"judgment": "ad", "eff_start_s": 3, "eff_end_s": 8,
            "bounds_verified": True}, 0)
        media = self.current_record()["media"]
        self.assertEqual(media["playback"], "local")
        self.assertEqual(media["recording_id"], recording["artifact"]["id"])
        self.assertEqual(media["recording_digest"], recording["artifact"]["digest"])
        self.assertEqual(media["source_offset_s"], 3)
        self.assertEqual(media["duration_s"], 5)

    def test_mismatch_has_no_player(self):
        ops.set_episode_media(self.con, "s1",
            {"alignment": "mismatch", "audio_url": "https://cdn.example/ep.mp3"}, 1)
        media = self.current_record()["media"]
        self.assertNotEqual(media["playback"], "local")
        self.assertNotIn("recording_id", media)
        self.assertEqual(media.get("timing", "unknown"), "unknown")

    def test_media_edit_without_mismatch_is_not_mismatch(self):
        ops.set_episode_media(self.con, "s1",
            {"alignment": "verified", "audio_url": "https://cdn.example/ep.mp3"}, 1)
        media = self.current_record()["media"]
        self.assertEqual(media["playback"], "remote")
        self.assertEqual(media["timing"], "unknown")


class ActivityFeedTest(unittest.TestCase):
    """Overview activity speaks customer wording with destination links;
    raw audit actions and ids never surface."""

    def setUp(self):
        self.con = fresh_db()
        seed_corpus(self.con)
        add_evidence(self.con, "ev-a", "s1", "acme launch")

    def tearDown(self):
        self.con.close()

    def test_alert_actions_translate_with_links(self):
        saved = customer.save_alert(self.con, "watch acme", query_acme())
        customer.set_alert_lifecycle(self.con, saved["id"], "paused",
                                     saved["revision"])
        feed = customer.overview(self.con)["activity"]
        texts = [a["text"] for a in feed]
        self.assertIn("Alert created", texts)
        self.assertIn("Alert paused", texts)
        for a in feed:
            self.assertTrue(a["href"].startswith("#/"))
            self.assertNotIn("customer_", a["text"])
            self.assertNotIn("customer_", a["href"])

    def test_operational_entries_stay_out(self):
        customer_saved.create_item(
            self.con, {"type": "query", "title": "shopify",
                       "query": {"version": 2, "mode": "mentions",
                                 "q": "shopify", "brands": None,
                                 "shows": None, "categories": None,
                                 "statuses": None, "audio": None,
                                 "date_status": "all", "from": None,
                                 "to": None, "sort": "newest", "page": 1,
                                 "ids": None}},
            workspace_context="ctx-test")
        customer.save_alert(self.con, "watch acme", query_acme())
        feed = customer.overview(self.con)["activity"]
        self.assertTrue(all("export" not in a["text"].lower() for a in feed))
        self.assertTrue(all("saved search" not in a["text"].lower()
                            for a in feed))

    def test_gap_carries_scope_label_not_raw_id(self):
        from earwitness.app import campaigns, briefs
        brief = briefs.create_brief(self.con, {"title": "Acme flight", "provided_text": "One entered placement."})
        ref = {"kind": "supplied_brief", "brief_id": brief["brief_id"], "version": 1}
        created = campaigns.save_customer_campaign(self.con, {"campaign": {"name": "Acme flight",
            "mode": "supplied_requirements", "brand": "Acme", "requirement_provenance": {"brief_ref": ref}}})
        cid = created["campaign"]["id"]
        show_id = customer.customer_show_for_episode(self.con, "s1")["show_identity_id"]
        placed = campaigns.save_expected_placement(self.con, cid, scope="show_window", show_id=show_id,
            date_from="2026-03-01", date_to="2026-03-31", expected_count=1, provenance={"brief_ref": ref})
        gaps = customer.overview(self.con)["campaign_gaps"]
        self.assertTrue(gaps)
        gap = next(gap for gap in gaps if gap["placement_id"] == placed["placement_id"])
        self.assertIn("Show One", gap["placement_label"])
        self.assertEqual(gap["placement_href"], f"#/campaign/{cid}/placement/{placed['placement_id']}")

    def test_unregistered_legacy_brief_is_preserved_readonly(self):
        from earwitness.app import campaigns
        cur = self.con.execute("INSERT INTO campaigns(name,status,brand,revision,updated_at)"
            " VALUES('Legacy','active','Acme',1,?)", (db.utcnow(),))
        cid = cur.lastrowid
        campaigns.save_expected_placement(self.con, cid, expected_count=1,
            provenance={"brief_ref": "unregistered-original"})
        before = self.con.execute("SELECT provenance FROM expected_placements WHERE campaign_id=?", (cid,)).fetchone()[0]
        projection = campaigns.campaign_projection(self.con, dict(self.con.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()))
        self.assertEqual(projection["availability"], "legacy_review_required")
        self.assertIsNone(projection["mode"])
        self.assertEqual(projection["allowed_actions"], ["read", "stored_reports"])
        with self.assertRaises(ops.BadInput):
            campaigns.build_customer_draft(self.con, cid)
        self.assertEqual(self.con.execute("SELECT provenance FROM expected_placements WHERE campaign_id=?", (cid,)).fetchone()[0], before)


if __name__ == "__main__":
    unittest.main()
