"""Backend trackers K08: Query2/live definitions, transition table, B5 DTO, receipts."""
from __future__ import annotations

import json
import sqlite3
import unittest

from earwitness.app import customer, customer_query, db, ops, search
from earwitness.app import citations as _citations


def fresh_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON;")
    db.init_schema(con)
    return con


def seed_corpus(con):
    meta = [
        {"ep_id": "s1", "show_title": "Show One",
         "feed_url": "https://one.example/rss", "ep_title": "First",
         "pub_date": "2026-03-10", "category": "business"},
        {"ep_id": "s2", "show_title": "Show Two",
         "feed_url": "https://two.example/rss", "ep_title": "Second",
         "pub_date": "2026-03-11", "category": "business"},
    ]
    con.execute(
        "INSERT INTO source_versions (source_key, version_no, sha256, payload,"
        " is_active, created_at) VALUES ('episodes.json', 1, 'sh', ?, 1, ?)",
        (json.dumps(meta), db.utcnow()))
    segments = {"s1": [{"start": 0.0, "end": 10.0, "text": "acme bright widget"}],
                "s2": [{"start": 0.0, "end": 10.0, "text": "quiet weather notes"}]}
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
        _citations.publish_source_state(con, kind="episode", record_id=ep_id,
                                        actor="fixture")
    con.commit()


def add_evidence(con, ev_id, episode_id, excerpt, attrs=None):
    con.execute(
        "INSERT INTO evidence (id, source, source_version_id, episode_id, orig_start_s,"
        " orig_end_s, excerpt, attrs, revision, created_at, transcript_version_id,"
        " source_record_key, assoc_revision, record_kind)"
        " VALUES (?, 'detector', 1, ?, 0.0, 10.0, ?, ?, 1, ?, "
        " (SELECT transcript_version_id FROM episodes WHERE id=?), ?, 0, 'candidate')",
        (ev_id, episode_id, excerpt, json.dumps(attrs or {}), db.utcnow(),
         episode_id, ev_id))
    db.record_evidence_snapshot(con, ev_id, "seed")
    _citations.publish_source_state(con, kind="evidence", record_id=ev_id,
                                    actor="fixture")
    _citations.publish_source_state(con, kind="episode", record_id=episode_id,
                                    actor="fixture")
    con.commit()


def query_acme():
    return {"version": 2, "mode": "mentions", "q": "acme"}


class CreateTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        seed_corpus(self.con)

    def tearDown(self):
        self.con.close()

    def test_create_active_with_silent_baseline(self):
        saved = customer.save_alert(self.con, "watch acme", query_acme())
        self.assertEqual(saved["lifecycle"], "active_local")
        self.assertTrue(saved["baseline_complete"])
        self.assertFalse(saved.get("setup_required"))
        dto = customer.get_alert(self.con, saved["id"])
        self.assertEqual(dto["query"]["q"], "acme")
        self.assertIsNone(dto["selector"])
        # Existing matches are not news: first check emits nothing.
        out = customer.check_alert(self.con, saved["id"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["emitted"], 0)

    def test_dto_carries_created_at(self):
        # JE-WORDS (UX D48): the Alerts list dates the past matches with the
        # alert's own created_at, on the list and on the alert page.
        saved = customer.save_alert(self.con, "watch acme", query_acme())
        self.con.execute("UPDATE customer_alerts SET created_at=? WHERE id=?",
                         ("2026-10-14T12:00:00+00:00", saved["id"]))
        self.con.commit()
        self.assertEqual(customer.get_alert(self.con, saved["id"])["created_at"],
                         "2026-10-14T12:00:00+00:00")
        listed = [a for a in customer.list_alerts(self.con) if a["id"] == saved["id"]]
        self.assertEqual(listed[0]["created_at"], "2026-10-14T12:00:00+00:00")

    def test_v1_criteria_rejected(self):
        with self.assertRaises(search.FilterRejected):
            customer.save_alert(
                self.con, "bad",
                {"version": 1, "mode": "mentions", "q": "acme"})

    def test_client_verification_fields_denied_without_writes(self):
        before = self.con.execute(
            "SELECT COUNT(*) FROM customer_alerts").fetchone()[0]
        with self.assertRaises(search.FilterRejected):
            customer.save_alert(
                self.con, "bad", query_acme(),
                delivery={"verification_context": "verified",
                          "verified_via": "customer says so"})
        with self.assertRaises(search.FilterRejected):
            customer.save_alert(
                self.con, "bad", query_acme(),
                delivery={"verified_via": "x"})
        after = self.con.execute(
            "SELECT COUNT(*) FROM customer_alerts").fetchone()[0]
        self.assertEqual(after, before)

    def test_live_selector_stored_and_narrowing(self):
        selector = {"version": 1,
                    "base_query": {"version": 2, "mode": "mentions",
                                   "q": "acme"},
                    "clause": {"kind": "all_results"},
                    "count_units": "distinct_results",
                    "scope_mode": "live_definition"}
        saved = customer.save_alert(self.con, "narrow", query_acme(),
                                    selector=selector)
        dto = customer.get_alert(self.con, saved["id"])
        self.assertEqual(dto["selector"]["scope_mode"], "live_definition")
        self.assertEqual(dto["selector"]["clause"],
                         {"kind": "all_results"})

    def test_live_selector_mode_conflict_rejected(self):
        selector = {"version": 1,
                    "base_query": {"version": 2, "mode": "ads",
                                   "q": "acme"},
                    "clause": {"kind": "all_results"},
                    "count_units": "distinct_results",
                    "scope_mode": "live_definition"}
        with self.assertRaises(search.FilterRejected):
            customer.save_alert(self.con, "bad", query_acme(),
                                selector=selector)

    def test_counted_selector_rejected_on_trackers(self):
        selector = {"version": 1,
                    "base_query": {"version": 2, "mode": "mentions",
                                   "q": "acme"},
                    "clause": {"kind": "all_results"},
                    "count_units": "distinct_results",
                    "scope_mode": "counted_cohort",
                    "view_revision": "r", "workspace_context": "c"}
        with self.assertRaises(search.FilterRejected):
            customer.save_alert(self.con, "bad", query_acme(),
                                selector=selector)


class TransitionTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        seed_corpus(self.con)
        add_evidence(self.con, "ev-a", "s1", "acme launch")

    def tearDown(self):
        self.con.close()

    def test_metadata_edit_keeps_definition_and_baseline(self):
        saved = customer.save_alert(self.con, "watch acme", query_acme())
        checked = customer.check_alert(self.con, saved["id"])
        before = customer.get_alert(self.con, saved["id"])
        renamed = customer.save_alert(
            self.con, "renamed", query_acme(), alert_id=saved["id"],
            expected_revision=saved["revision"])
        self.assertEqual(renamed["definition_revision"],
                         before["definition_revision"])
        self.assertEqual(renamed["resource_revision"],
                         checked["resource_revision"])
        after = customer.get_alert(self.con, saved["id"])
        self.assertTrue(after["baseline_complete"])
        self.assertEqual(after["baseline_count"], before["baseline_count"])
        self.assertEqual(after["last_successful_at"],
                         before["last_successful_at"])

    def test_membership_edit_invalidates_until_silent_success(self):
        saved = customer.save_alert(self.con, "watch acme", query_acme())
        customer.check_alert(self.con, saved["id"])
        before = customer.get_alert(self.con, saved["id"])
        edited = customer.save_alert(
            self.con, "watch acme",
            {"version": 2, "mode": "mentions", "q": "widget"},
            alert_id=saved["id"], expected_revision=saved["revision"])
        self.assertEqual(edited["definition_revision"],
                         before["definition_revision"] + 1)
        self.assertFalse(edited["baseline_complete"])
        invalidated = customer.get_alert(self.con, saved["id"])
        self.assertEqual(invalidated["baseline_count"],
                         before["baseline_count"])
        self.assertEqual(invalidated["last_successful_definition_revision"],
                         before["definition_revision"])
        out = customer.check_alert(self.con, saved["id"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["emitted"], 0)
        self.assertTrue(customer.get_alert(
            self.con, saved["id"])["baseline_complete"])

    def test_event_only_edit_keeps_baseline_without_replay(self):
        saved = customer.save_alert(
            self.con, "watch acme", query_acme(),
            event_types=["new_text", "new_finding", "offer_difference"])
        customer.check_alert(self.con, saved["id"])
        before = customer.get_alert(self.con, saved["id"])
        edited = customer.save_alert(
            self.con, "watch acme", query_acme(),
            event_types=["new_text"],
            alert_id=saved["id"], expected_revision=saved["revision"])
        self.assertEqual(edited["definition_revision"],
                         before["definition_revision"] + 1)
        after = customer.get_alert(self.con, saved["id"])
        self.assertTrue(after["baseline_complete"])
        self.assertEqual(after["baseline_count"], before["baseline_count"])
        # No replay: nothing new happened, so the next check stays quiet.
        out = customer.check_alert(self.con, saved["id"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["emitted"], 0)

    def test_failed_check_retains_baseline_and_advances_resource(self):
        saved = customer.save_alert(self.con, "watch acme", query_acme())
        first = customer.check_alert(self.con, saved["id"])
        before = customer.get_alert(self.con, saved["id"])
        cap = customer.SCAN_SEGMENT_CAP
        customer.SCAN_SEGMENT_CAP = 0
        customer_query._VIEW_MEMO.clear()  # the cap is an input outside the memo key
        try:
            out = customer.check_alert(self.con, saved["id"])
        finally:
            customer.SCAN_SEGMENT_CAP = cap
            customer_query._VIEW_MEMO.clear()  # the cap is an input outside the memo key
        self.assertFalse(out["ok"])
        self.assertGreater(out["resource_revision"],
                           first["resource_revision"])
        after = customer.get_alert(self.con, saved["id"])
        self.assertEqual(after["definition_revision"],
                         before["definition_revision"])
        self.assertTrue(after["baseline_complete"])
        self.assertEqual(after["baseline_count"], before["baseline_count"])
        self.assertEqual(after["last_successful_at"],
                         before["last_successful_at"])
        self.assertEqual(after["last_attempt_result"], "error")
        self.assertEqual(after["error"], out["error"])


class OfferOnlyTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        seed_corpus(self.con)
        add_evidence(self.con, "ev-a", "s1", "acme launch",
                     {"offer": "WIDGET10", "code": "W10",
                      "message": "bright"})

    def tearDown(self):
        self.con.close()

    def test_offer_change_emits_without_new_finding_gate(self):
        saved = customer.save_alert(
            self.con, "offers only",
            {"version": 2, "mode": "ads", "q": "acme"},
            event_types=["offer_difference"])
        customer.check_alert(self.con, saved["id"])
        # Publish the observed offer change in the same source/history transaction.
        with db.transaction(self.con, write=True):
            self.con.execute("UPDATE evidence SET attrs=?,revision=revision+1 WHERE id='ev-a'",
                             (json.dumps({"offer": "WIDGET20", "code": "W20",
                                          "message": "bright"}),))
            db.record_evidence_snapshot(self.con, "ev-a", "fixture")
            _citations.publish_source_state(self.con, kind="evidence", record_id="ev-a", actor="fixture")
        out = customer.check_alert(self.con, saved["id"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["emitted"], 1)
        kinds = [e["kind"] for e in
                 customer.get_alert(self.con, saved["id"])["events"]]
        self.assertEqual(kinds, ["offer_difference"])

    def test_new_member_of_unsubscribed_kind_emits_nothing(self):
        saved = customer.save_alert(
            self.con, "offers only",
            {"version": 2, "mode": "ads", "q": "acme"},
            event_types=["offer_difference"])
        customer.check_alert(self.con, saved["id"])
        add_evidence(self.con, "ev-b", "s1", "more acme news")
        out = customer.check_alert(self.con, saved["id"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["emitted"], 0)
        self.assertEqual(
            customer.get_alert(self.con, saved["id"])["events"], [])


class StopPauseTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        seed_corpus(self.con)
        add_evidence(self.con, "ev-a", "s1", "acme launch")

    def tearDown(self):
        self.con.close()

    def test_stop_terminal_but_history_readable(self):
        saved = customer.save_alert(self.con, "watch acme", query_acme())
        customer.check_alert(self.con, saved["id"])
        add_evidence(self.con, "ev-b", "s1", "more acme news")
        ran = customer.check_alert(self.con, saved["id"])
        self.assertEqual(ran["emitted"], 1)
        stopped = customer.set_alert_lifecycle(
            self.con, saved["id"], "stopped", saved["revision"] + 0)
        self.assertEqual(stopped["lifecycle"], "stopped")
        with self.assertRaises(ops.BadInput):
            customer.check_alert(self.con, saved["id"])
        with self.assertRaises(ops.BadInput):
            customer.set_alert_lifecycle(
                self.con, saved["id"], "active_local",
                stopped["revision"])
        # History survives the stop through the B5 DTO.
        dto = customer.get_alert(self.con, saved["id"])
        self.assertEqual(dto["lifecycle"], "stopped")
        self.assertEqual(len(dto["events"]), 1)
        self.assertEqual(dto["unread_event_count"], 1)

    def test_paused_check_refused_resume_compares(self):
        saved = customer.save_alert(self.con, "watch acme", query_acme())
        customer.check_alert(self.con, saved["id"])
        paused = customer.set_alert_lifecycle(
            self.con, saved["id"], "paused", saved["revision"])
        self.assertEqual(paused["lifecycle"], "paused")
        with self.assertRaises(ops.BadInput):
            customer.check_alert(self.con, saved["id"])
        resumed = customer.set_alert_lifecycle(
            self.con, saved["id"], "active_local", paused["revision"])
        self.assertEqual(resumed["lifecycle"], "active_local")
        add_evidence(self.con, "ev-c", "s1", "acme again")
        out = customer.check_alert(self.con, saved["id"])
        self.assertEqual(out["emitted"], 1)


class DtoTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        seed_corpus(self.con)
        add_evidence(self.con, "ev-a", "s1", "acme launch")

    def tearDown(self):
        self.con.close()

    def test_dto_carries_named_fields_never_blobs(self):
        saved = customer.save_alert(self.con, "watch acme", query_acme())
        customer.check_alert(self.con, saved["id"])
        add_evidence(self.con, "ev-b", "s1", "more acme news")
        customer.check_alert(self.con, saved["id"])
        dto = customer.tracker_dto(self.con, saved["id"])
        for key in ("id", "name", "revision", "definition_revision",
                    "query", "selector", "event_types", "lifecycle", "baseline_complete",
                    "baseline_count", "count_units", "last_attempted_at",
                    "last_attempt_result", "error", "last_successful_at",
                    "last_successful_definition_revision", "current_matches",
                    "unread_event_count", "events"):
            self.assertIn(key, dto)
        for blob in ("query_json", "baseline_json", "live_selector_json",
                     "delivery_json", "monitor_json"):
            self.assertNotIn(blob, dto)
        self.assertEqual(dto["query"]["version"], 2)
        self.assertEqual(dto["unread_event_count"], 1)
        event = dto["events"][0]
        for key in ("before_ref", "after_ref", "historical", "read_state"):
            self.assertIn(key, event)
        self.assertFalse(event["historical"])
        self.assertEqual(event["read_state"], 0)
        self.assertEqual(dto["current_matches"]["count"],
                         dto["baseline_count"])
        # Owned read marks; foreign IDs never mark.
        marked = customer.mark_alert_events_read(
            self.con, saved["id"], [event["id"], 999999])
        self.assertEqual(marked["marked"], 1)
        self.assertEqual(customer.get_alert(
            self.con, saved["id"])["unread_event_count"], 0)

    def test_list_returns_dtos(self):
        saved = customer.save_alert(self.con, "watch acme", query_acme())
        rows = customer.list_alerts(self.con)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], saved["id"])
        self.assertIn("baseline_complete", rows[0])
        self.assertNotIn("query_json", rows[0])
        # PERF r2: no current-match counts on the list; the alert keeps them.
        self.assertNotIn("current_matches", rows[0])
        self.assertIn("count", customer.get_alert(self.con, saved["id"])["current_matches"])

    def test_backfill_historical_idempotent_without_delivery(self):
        saved = customer.save_alert(self.con, "watch acme", query_acme())
        first = customer.backfill_alert(self.con, saved["id"], confirm=True)
        self.assertTrue(first["historical"])
        self.assertGreater(first["imported"], 0)
        second = customer.backfill_alert(self.con, saved["id"], confirm=True)
        self.assertEqual(second["imported"], 0)
        events = customer.get_alert(self.con, saved["id"])["events"]
        self.assertTrue(all(e["historical"] for e in events))
        with self.assertRaises(search.FilterRejected):
            customer.backfill_alert(self.con, saved["id"], confirm=False)


class TrackerDefinitionHttpTest(unittest.TestCase):
    def test_event_selection_roundtrip_preserves_baseline_and_prevents_replay(self):
        from tests.test_backend_authority import HostedReaderHttpTest
        http = HostedReaderHttpTest()
        http.setUp()
        self.addCleanup(http.tearDown)
        headers = http._csrf()
        selected = ["new_text", "new_finding"]
        status, saved = http._call(
            "/api/v1/customer/alerts",
            body={"name": "Selected events", "query": {"version": 2, "mode": "mentions", "q": "lumen"},
                  "event_types": selected}, headers=headers)
        self.assertEqual(status, 200)
        aid = saved["id"]
        path = f"/api/v1/customer/alerts/{aid}"

        def read():
            status, payload = http._call(path, method="GET", headers=headers)
            self.assertEqual(status, 200)
            return payload["alert"]

        def baseline():
            con = db.open_existing(http.db_path, readonly=True)
            try:
                row = con.execute("SELECT baseline_json FROM customer_alerts WHERE id=?", (aid,)).fetchone()
                return json.loads(row["baseline_json"])
            finally:
                con.close()

        first = read()
        self.assertEqual(first.get("event_types"), selected)
        self.assertIsInstance(first["event_types"], list)
        self.assertGreater(first["baseline_count"], 0)
        status, checked = http._call(path + "/check", body={}, headers=headers)
        self.assertEqual(status, 200)
        self.assertTrue(checked["ok"])
        self.assertEqual(checked["emitted"], 0)
        before = read()
        membership = baseline()
        status, _ = http._call(
            path, method="PUT",
            body={"name": "Renamed only", "query": before["query"],
                  "selector": before["selector"], "event_types": before["event_types"],
                  "expected_revision": before["revision"]}, headers=headers)
        self.assertEqual(status, 200)
        renamed = read()
        self.assertEqual(renamed["event_types"], selected)
        self.assertEqual(renamed["definition_revision"], before["definition_revision"])
        self.assertEqual(renamed["last_successful_at"], before["last_successful_at"])
        self.assertEqual(baseline(), membership)
        status, _ = http._call(
            path, method="PUT",
            body={"name": renamed["name"], "query": renamed["query"],
                  "selector": renamed["selector"], "event_types": ["new_text"],
                  "expected_revision": renamed["revision"]}, headers=headers)
        self.assertEqual(status, 200)
        changed = read()
        self.assertEqual(changed["event_types"], ["new_text"])
        self.assertEqual(changed["definition_revision"], renamed["definition_revision"] + 1)
        self.assertTrue(changed["baseline_complete"])
        self.assertEqual(changed["last_successful_definition_revision"],
                         renamed["last_successful_definition_revision"])
        self.assertEqual(baseline(), membership)
        status, checked = http._call(path + "/check", body={}, headers=headers)
        self.assertEqual(status, 200)
        self.assertTrue(checked["ok"])
        self.assertEqual(checked["emitted"], 0)
        after = read()
        self.assertEqual(after["events"], before["events"])
        self.assertEqual(baseline()["members"], membership["members"])


if __name__ == "__main__":
    unittest.main()
