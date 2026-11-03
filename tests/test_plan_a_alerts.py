"""Plan A excerpt view: retained alert history (design 6, design 9 item 6c).

Under Plan B an alert whose query matches only text after character 600
records a baseline, an event and an unread event. After the change to
Plan A the alert list and detail and Overview show no membership, count
or ref from that history, identical to a fixture without the hidden text.
The first excerpt-level check of the same alert starts a separate `pa1`
baseline from safe matches only and emits nothing from comparing old and
new membership. A new Plan A alert shows positive matches marked `pa1`.
"""
from __future__ import annotations

import json
import unittest

from earwitness.app import services
from tests.test_plan_a_fixture import ENTRY, Workspace, leaked, member, safe_text

HIDDEN = {"version": 2, "mode": "ads", "q": "qzlate"}
STRIP = ("revision", "view_revision", "criteria", "created_at", "updated_at",
         "last_attempted_at", "last_successful_at", "observed_at", "checked_at",
         "latest_observed_at", "at", "aggregate_rev", "resource_revision", "activity")


class AlertHistoryTest(unittest.TestCase):
    def setUp(self):
        self.one = Workspace("B")
        self.two = Workspace("B", variant=True)
        self.addCleanup(self.one.close)
        self.addCleanup(self.two.close)
        self.ids = {}
        for ws in (self.one, self.two):
            self.ids[id(ws)] = services.customer_save_alert(
                member(), ws.con, ENTRY, "Hidden words", HIDDEN, "/searches",
                ["new_finding"])["id"]
            # A new read over the same hidden words: a Plan B check emits an
            # (unread) event for it in the fixture that holds the words.
            ws.add_ad("ad-a6", "ep_d", 98.0, 122.0, "Nimbus", "nimbus again")
            services.customer_check_alert(member(), ws.con, ENTRY, self.ids[id(ws)])

    def alert(self, ws, detail=True):
        if detail:
            return services.customer_alert(member(), ws.con, ENTRY, self.ids[id(ws)])["alert"]
        return services.customer_alerts(member(), ws.con, ENTRY)["alerts"]

    def overview(self, ws):
        return services.customer_overview(member(), ws.con, ENTRY)

    def normal(self, ws, value):
        names = {r["id"]: f"{r['kind']}:{r['record_id']}" for r in ws.con.execute(
            "SELECT id, kind, record_id FROM source_states")}

        def walk(v):
            if isinstance(v, dict):
                return {k: walk(x) for k, x in v.items() if k not in STRIP}
            if isinstance(v, list):
                # Ties order by the random publication ids: compare as sets.
                return sorted((walk(x) for x in v), key=lambda x: json.dumps(x, sort_keys=True))
            if isinstance(v, str) and v.startswith("sr1:"):
                state, _sep, rest = v[4:].partition(":")
                return names.get("sr1:" + state, v) + (":" + rest if rest else "")
            return v
        return walk(value)

    def assertSame(self, read):
        self.assertEqual(self.normal(self.one, read(self.one)),
                         self.normal(self.two, read(self.two)))

    def stored(self, ws):
        return ([tuple(r) for r in ws.con.execute(
                    "SELECT id, kind, logical_identity, before_ref, after_ref, read_state,"
                    " payload_json FROM customer_alert_events ORDER BY id")])

    def test_plan_b_history_exists_only_in_the_fixture_with_hidden_words(self):
        one, two = self.alert(self.one), self.alert(self.two)
        self.assertEqual(one["baseline_count"], 2)  # ad-a5 and the new ad-a6
        self.assertEqual(one["unread_event_count"], 1)
        self.assertEqual(two["baseline_count"], 0)
        self.assertEqual(two["unread_event_count"], 0)

    def test_after_the_change_old_history_is_withheld_and_identical(self):
        before = self.stored(self.one)
        for ws in (self.one, self.two):
            ws.plan("A")
        self.assertSame(lambda ws: self.alert(ws))
        self.assertSame(lambda ws: self.alert(ws, detail=False))
        self.assertSame(self.overview)
        one = self.alert(self.one)
        self.assertEqual(one["events"], [])
        self.assertEqual((one["unread_event_count"], one["unread_ad_count"]), (0, 0))
        self.assertIsNone(one["baseline_count"])
        self.assertTrue(one["history_withheld"])
        # The alert's own query is the member's words (a request echo).
        self.assertEqual(leaked({k: v for k, v in one.items() if k != "query"}), [])
        overview = self.overview(self.one)
        self.assertEqual((overview["recent_events"], overview["alert_totals"]), ([], []))
        self.assertEqual(self.stored(self.one), before)

    def test_the_first_plan_a_check_starts_a_separate_pa1_baseline(self):
        for ws in (self.one, self.two):
            ws.plan("A")
        checks = [services.customer_check_alert(member(), ws.con, ENTRY, self.ids[id(ws)])
                  for ws in (self.one, self.two)]
        self.assertEqual([c["emitted"] for c in checks], [0, 0])
        for ws in (self.one, self.two):
            baseline = json.loads(ws.con.execute(
                "SELECT baseline_json FROM customer_alerts WHERE id=?",
                (self.ids[id(ws)],)).fetchone()[0])
            self.assertEqual(baseline["basis"], "pa1")
            self.assertEqual(baseline["members"], {})
        self.assertSame(lambda ws: self.alert(ws))
        self.assertSame(self.overview)
        self.assertSame(lambda ws: services.customer_check_alert(
            member(), ws.con, ENTRY, self.ids[id(ws)]))
        self.assertSame(lambda ws: self.alert(ws))
        one = self.alert(self.one)
        self.assertEqual(one["baseline_count"], 0)
        self.assertEqual(one["events"], [])

    def test_a_new_plan_a_alert_shows_safe_matches_marked_pa1(self):
        ws = self.one
        ws.plan("A")
        made = services.customer_save_alert(member(), ws.con, ENTRY, "Lumen",
                                            {"version": 2, "mode": "ads", "q": "lumen"},
                                            "/searches", ["new_finding"])
        baseline = json.loads(ws.con.execute("SELECT baseline_json FROM customer_alerts"
                                             " WHERE id=?", (made["id"],)).fetchone()[0])
        self.assertEqual(baseline["basis"], "pa1")
        self.assertEqual(set(baseline["members"]), {"finding:ad-a1"})
        alert = services.customer_alert(member(), ws.con, ENTRY, made["id"])["alert"]
        self.assertEqual(alert["baseline_count"], 1)
        self.assertEqual(alert["current_matches"]["count"], 1)
        self.assertFalse(alert.get("history_withheld"))
        # A new matching read emits a pa1 event whose summary is the safe text.
        ws.add_ad("ad-a7", "ep_a", 30.0, 45.0, "Lumen", "lumen again qzstored")
        out = services.customer_check_alert(member(), ws.con, ENTRY, made["id"])
        self.assertEqual(out["emitted"], 1)
        alert = services.customer_alert(member(), ws.con, ENTRY, made["id"])["alert"]
        self.assertEqual(alert["unread_event_count"], 1)
        self.assertEqual(alert["events"][0]["summary"]["text"], safe_text("ad-a1"))
        self.assertEqual(leaked(alert), [])
        payload = json.loads(ws.con.execute(
            "SELECT payload_json FROM customer_alert_events WHERE alert_id=?",
            (made["id"],)).fetchone()[0])
        self.assertEqual(payload["basis"], "pa1")
        overview = self.overview(ws)
        self.assertEqual([t["alert_id"] for t in overview["alert_totals"]], [made["id"]])


if __name__ == "__main__":
    unittest.main()
