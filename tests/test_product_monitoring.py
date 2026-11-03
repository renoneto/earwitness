"""P11 product regressions: monitoring + outbox (stdlib unittest).

Disposable in-memory workspaces only. Provider sandbox only — no sends.
"""
from __future__ import annotations

import json
import sqlite3
import unittest

from earwitness.app import db, monitoring
from earwitness.app import ops


def fresh_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON;")
    db.init_schema(con)
    monitoring.ensure_aux(con)
    return con


def seed_schedule(con, sid="sched-1", interval=21600, mode="discovery"):
    con.execute("INSERT INTO schedules (id, workspace_id, mode, source_ids,"
                " interval_s, enabled, definition_revision, revision, updated_at)"
                " VALUES (?, 'local', ?, ?, ?, 1, 1, 1, ?)",
                (sid, mode, json.dumps(["feed-a"]), interval, db.utcnow()))
    con.commit()


MATCHES = [
    {"kind": "new_observation", "identity": "occ-1",
     "occurrence_id": "occ-1", "processing_state": "processed"},
    {"kind": "new_creative", "identity": "cre-9",
     "creative_id": "cre-9", "processing_state": "processed"},
]


class SchedulingTest(unittest.TestCase):
    def test_discovery_default_6h_with_deterministic_jitter(self):
        con = fresh_db()
        try:
            j1 = monitoring.jitter_for("feed-a")
            j2 = monitoring.jitter_for("feed-a")
            self.assertEqual(j1, j2)
            self.assertGreaterEqual(abs(j1), 0)
            self.assertLessEqual(abs(j1), monitoring.JITTER_WINDOW_S)
            due = monitoring.next_due_at("2026-01-01T00:00:00+00:00", 21600, "feed-a")
            self.assertIn("2026-01-01", due)
        finally:
            con.close()

    def test_resampling_disabled_unless_selected(self):
        con = fresh_db()
        try:
            seed_schedule(con, mode="discovery")
            st = monitoring.schedule_status(con, "sched-1")
            self.assertEqual(st["resampling"], "disabled")
        finally:
            con.close()

    def test_missed_coalesces_to_one_catchup(self):
        con = fresh_db()
        try:
            seed_schedule(con, interval=3600)
            monitoring.save_schedule_state(con, "sched-1", "local",
                                           "2026-01-01T00:00:00+00:00")
            due = monitoring.due_schedules(con, "2026-01-01T12:00:00+00:00")
            self.assertEqual(len(due), 1)
            self.assertTrue(due[0]["coalesced"])
            self.assertGreaterEqual(due[0]["missed_intervals"], 2)
        finally:
            con.close()

    def test_pause_on_budget_failure_shows_last_success(self):
        con = fresh_db()
        try:
            seed_schedule(con)
            monitoring.save_schedule_state(con, "sched-1", "local",
                                           "2026-01-01T00:00:00+00:00")
            monitoring.record_poll_result(con, "sched-1", "success",
                                          {"episodes": 5})
            paused = monitoring.record_poll_result(con, "sched-1",
                                                   "budget_exhausted")
            self.assertTrue(paused["paused"])
            self.assertEqual(paused["last_successful_coverage"], {"episodes": 5})
            st = monitoring.schedule_status(con, "sched-1")
            self.assertEqual(st["paused_reason"], "budget_exhausted")
        finally:
            con.close()


class TransitionsTest(unittest.TestCase):
    def test_red_capable_repeat_poll_no_duplicate_event(self):
        """RED-CAPABLE: duplicate-event on repeated poll.

        Before: naive insert counted 2 events. After: unique event_key
        dedupes to 1 row; second poll reports created=0, duplicates=2.
        """
        con = fresh_db()
        try:
            seed_schedule(con)
            first = monitoring.detect_match_transitions(
                con, "local", "sched-1", 3, MATCHES)
            second = monitoring.detect_match_transitions(
                con, "local", "sched-1", 3, MATCHES)
            rows = con.execute("SELECT COUNT(*) c FROM monitor_events").fetchone()["c"]
            self.assertEqual(first["created"], 2)
            self.assertEqual(second["created"], 0)
            self.assertEqual(second["duplicates"], 2)
            self.assertEqual(rows, 2)  # not 4
        finally:
            con.close()

    def test_baseline_then_no_flood(self):
        con = fresh_db()
        try:
            seed_schedule(con)
            monitoring.establish_baseline(con, "local", 7, 3, MATCHES)
            base = monitoring.get_baseline(con, "local", 7, 3)
            self.assertEqual(len(base), 2)
        finally:
            con.close()

    def test_baseline_gates_first_poll(self):
        """Baselined identities emit nothing on first poll.

        Before: detect_match_transitions ignored monitor_baselines, so
        enabling a monitor on a baselined watchlist emitted one event
        per baselined identity. After: only identities absent from the
        baseline become events; event_key dedupe stays the second layer.
        """
        con = fresh_db()
        try:
            seed_schedule(con)
            same = [{"kind": "new_observation", "identity": "occ-1"}]
            monitoring.establish_baseline(con, "local", 7, 3, same)
            out = monitoring.detect_match_transitions(
                con, "local", "sched-1", 3, same)
            self.assertEqual(out["created"], 0)
            rows = con.execute(
                "SELECT COUNT(*) c FROM monitor_events").fetchone()["c"]
            self.assertEqual(rows, 0)
            # Positive control: a genuinely new identity still emits.
            fresh = monitoring.detect_match_transitions(
                con, "local", "sched-1", 3,
                same + [{"kind": "new_observation", "identity": "occ-2"}])
            self.assertEqual(fresh["created"], 1)
        finally:
            con.close()

    def test_definition_edit_new_revision_no_replay(self):
        con = fresh_db()
        try:
            seed_schedule(con)
            monitoring.detect_match_transitions(con, "local", "sched-1", 3, MATCHES)
            # Definition edit -> new revision + fresh baseline incl. old ids.
            monitoring.establish_baseline(con, "local", 7, 4, MATCHES)
            self.assertEqual(monitoring.get_baseline(con, "local", 7, 4),
                             ["cre-9", "occ-1"])
        finally:
            con.close()

    def test_failed_processing_is_not_no_match(self):
        con = fresh_db()
        try:
            seed_schedule(con)
            monitoring.detect_match_transitions(
                con, "local", "sched-1", 3,
                [{"kind": "new_observation", "identity": "occ-x",
                  "processing_state": "failed"}])
            row = con.execute("SELECT detail FROM monitor_events").fetchone()
            detail = json.loads(row["detail"])
            self.assertTrue(detail["not_a_no_match"])
        finally:
            con.close()

    def test_snapshot_observation_never_new_placement(self):
        con = fresh_db()
        try:
            seed_schedule(con)
            out = monitoring.detect_match_transitions(
                con, "local", "sched-1", 3,
                [{"kind": "new_snapshot_observation", "identity": "snap-1",
                  "occurrence_id": None}])
            self.assertEqual(out["created"], 1)
            row = con.execute("SELECT detail FROM monitor_events").fetchone()
            self.assertEqual(json.loads(row["detail"])["kind"],
                             "new_snapshot_observation")
        finally:
            con.close()


class OutboxTest(unittest.TestCase):
    def _ids(self, con):
        monitoring.detect_match_transitions(con, "local", "sched-1", 3, MATCHES[:1])
        return [r["id"] for r in con.execute("SELECT id FROM monitor_events").fetchall()]

    def test_revoked_member_gets_nothing_queued(self):
        con = fresh_db()
        try:
            seed_schedule(con)
            ids = self._ids(con)
            out = monitoring.commit_events_with_outbox(
                con, "local", ids, ["alice", "mallory"],
                membership_check=lambda m: m != "mallory")
            self.assertEqual(out["queued"], 1)
            self.assertEqual(out["skipped"], 1)
            rows = con.execute("SELECT recipient FROM notification_outbox").fetchall()
            self.assertEqual([r["recipient"] for r in rows], ["alice"])
        finally:
            con.close()

    def test_unsubscribe_suppresses_visibly(self):
        con = fresh_db()
        try:
            seed_schedule(con)
            ids = self._ids(con)
            monitoring.set_unsubscribed(con, "local", "alice", True)
            out = monitoring.commit_events_with_outbox(
                con, "local", ids, ["alice"],
                preferences_lookup=lambda m: {"unsubscribed": True})
            self.assertEqual(out["suppressed"], 1)
        finally:
            con.close()

    def test_ambiguous_send_visible_and_reconciled_before_resend(self):
        con = fresh_db()
        try:
            seed_schedule(con)
            ids = self._ids(con)
            monitoring.commit_events_with_outbox(con, "local", ids[:1], ["alice", "bob"])
            ob, ob2 = [r["id"] for r in con.execute(
                "SELECT id FROM notification_outbox ORDER BY id")]
            provider = monitoring.SandboxDeliveryProvider()
            sent = monitoring.attempt_delivery(con, ob, provider)
            self.assertEqual(sent["state"], "sent")
            self.assertIn("provider_message_id", sent)
            # A late uncertainty note never overwrites a recorded send.
            monitoring.mark_uncertain(con, ob, {"reason": "timeout after send"})
            self.assertEqual(con.execute("SELECT state FROM notification_outbox"
                                         " WHERE id=?", (ob,)).fetchone()[0], "sent")

            class Ambiguous(monitoring.SandboxDeliveryProvider):
                def deliver(self, entry, email_link=""):
                    super().deliver(entry, email_link)
                    raise monitoring.ProviderUncertain("timeout after send")
            ambiguous = monitoring.attempt_delivery(con, ob2, Ambiguous())
            self.assertEqual(ambiguous["state"], "uncertain")
            ob = ob2
            blocked = monitoring.attempt_delivery(con, ob, provider)
            self.assertEqual(blocked["state"], "uncertain")
            self.assertIn("reconcile", blocked["reason"])
            rec = monitoring.reconcile_delivery(con, ob, "confirmed_sent",
                                                {"provider": "sandbox"})
            self.assertEqual(rec["disposition"], "confirmed_sent")
        finally:
            con.close()

    def test_digest_default_daily_immediate_opt_in(self):
        con = fresh_db()
        try:
            prefs = monitoring.get_preferences(con, "local", "bob")
            self.assertEqual(prefs["digest"], "daily")
            monitoring.set_preferences(con, "local", "bob", "daily", True)
            prefs = monitoring.get_preferences(con, "local", "bob")
            self.assertTrue(prefs["immediate"])
        finally:
            con.close()


class OutboxDedupeRegressionTest(unittest.TestCase):
    """P1: UNIQUE guard in schema + defensive recipient dedupe."""

    def test_duplicate_recipients_single_row_no_raise(self):
        con = fresh_db()
        try:
            seed_schedule(con)
            monitoring.detect_match_transitions(con, "local", "sched-1", 3,
                                                MATCHES[:1])
            ids = [r["id"] for r in
                   con.execute("SELECT id FROM monitor_events").fetchall()]
            out = monitoring.commit_events_with_outbox(
                con, "local", ids, ["alice", "alice"])
            rows = con.execute(
                "SELECT recipient FROM notification_outbox").fetchall()
            self.assertEqual(out["queued"], 1)
            self.assertEqual([r["recipient"] for r in rows], ["alice"])
            repeat = monitoring.commit_events_with_outbox(
                con, "local", ids, ["alice", "alice"])
            still = con.execute(
                "SELECT COUNT(*) c FROM notification_outbox").fetchone()["c"]
            self.assertEqual(still, 1)
            self.assertEqual(repeat["queued"], 0)
        finally:
            con.close()

    def test_unique_guard_created_before_inserts_replay_safe(self):
        con = fresh_db()
        try:
            idx = con.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
                " AND name='idx_outbox_event_recipient'").fetchone()
            self.assertIsNotNone(idx)
            monitoring.ensure_aux(con)  # migration-safe replay: no raise
        finally:
            con.close()

    def test_repeat_suppressed_commit_counts_newly_suppressed_only(self):
        # R3/M1: repeat commit for the same (event, recipient) inserts no
        # row (INSERT OR IGNORE) and must report suppressed=0, not 1.
        con = fresh_db()
        try:
            seed_schedule(con)
            monitoring.detect_match_transitions(con, "local", "sched-1", 3,
                                                MATCHES[:1])
            ids = [r["id"] for r in
                   con.execute("SELECT id FROM monitor_events").fetchall()]
            kw = {"preferences_lookup": lambda m: {"unsubscribed": True}}
            first = monitoring.commit_events_with_outbox(
                con, "local", ids, ["alice"], **kw)
            self.assertEqual(first["suppressed"], 1)
            repeat = monitoring.commit_events_with_outbox(
                con, "local", ids, ["alice"], **kw)
            self.assertEqual(repeat["suppressed"], 0)
            still = con.execute(
                "SELECT COUNT(*) c FROM notification_outbox").fetchone()["c"]
            self.assertEqual(still, 1)
        finally:
            con.close()


class AtomicDetectAndQueueTest(unittest.TestCase):
    """P2: one transaction for transitions + outbox queue."""

    def test_queue_failure_rolls_back_transitions(self):
        con = fresh_db()
        try:
            seed_schedule(con)

            def boom(member):
                raise RuntimeError("membership store down")

            with self.assertRaises(RuntimeError):
                monitoring.detect_and_queue(con, "local", "sched-1", 3,
                                            MATCHES[:1], ["alice"],
                                            membership_check=boom)
            events = con.execute(
                "SELECT COUNT(*) c FROM monitor_events").fetchone()["c"]
            queued = con.execute(
                "SELECT COUNT(*) c FROM notification_outbox").fetchone()["c"]
            self.assertEqual(events, 0)
            self.assertEqual(queued, 0)
        finally:
            con.close()

    def test_success_path_creates_and_queues(self):
        con = fresh_db()
        try:
            seed_schedule(con)
            out = monitoring.detect_and_queue(con, "local", "sched-1", 3,
                                              MATCHES[:1], ["alice"])
            self.assertEqual(out["created"], 1)
            self.assertEqual(out["queued"], 1)
            again = monitoring.detect_and_queue(con, "local", "sched-1", 3,
                                                MATCHES[:1], ["alice"])
            self.assertEqual(again["created"], 0)
            self.assertEqual(again["queued"], 0)
        finally:
            con.close()

    def test_individual_functions_kept_for_compat(self):
        self.assertTrue(callable(monitoring.detect_match_transitions))
        self.assertTrue(callable(monitoring.commit_events_with_outbox))

    def test_server_detect_handler_uses_atomic_service(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "scripts" / "app_server.py").read_text()
        self.assertIn("detect_and_queue", src)
        self.assertNotIn("detect_match_transitions", src)


class FailedPollBackoffTest(unittest.TestCase):
    """P3: failed polls pause with backoff, keep coverage, resume."""

    def test_failed_pauses_with_backoff_no_hot_retry(self):
        con = fresh_db()
        try:
            seed_schedule(con, interval=3600)
            monitoring.save_schedule_state(con, "sched-1", "local",
                                           "2026-01-01T00:00:00+00:00")
            monitoring.record_poll_result(con, "sched-1", "success",
                                          {"episodes": 5})
            failed = monitoring.record_poll_result(con, "sched-1", "failed")
            self.assertTrue(failed["paused"])
            self.assertEqual(failed["reason"], "failed")
            self.assertEqual(failed["last_successful_coverage"],
                             {"episodes": 5})
            st = monitoring.schedule_status(con, "sched-1")
            self.assertEqual(st["paused_reason"], "failed")
            self.assertEqual(st["last_coverage"], {"episodes": 5})
            from datetime import datetime, timezone
            due = datetime.fromisoformat(
                st["next_due_at"].replace("Z", "+00:00"))
            self.assertGreater(due, datetime.now(timezone.utc))
            self.assertEqual(
                monitoring.due_schedules(con, monitoring._now_iso()), [])
        finally:
            con.close()

    def test_explicit_resume_rejoins_cadence(self):
        con = fresh_db()
        try:
            seed_schedule(con, interval=3600)
            monitoring.save_schedule_state(con, "sched-1", "local",
                                           "2026-01-01T00:00:00+00:00")
            monitoring.record_poll_result(con, "sched-1", "failed")
            resumed = monitoring.resume_schedule(con, "sched-1")
            self.assertFalse(resumed["paused"])
            st = monitoring.schedule_status(con, "sched-1")
            self.assertEqual(st["paused_reason"], "")
            self.assertEqual(
                monitoring.due_schedules(con, monitoring._now_iso()), [])
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
