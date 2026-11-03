"""Plan A excerpt view: guards for the DEMO2 merge (PA code review r11).

- LOW-1: DEMO2's held counts (`baseline_held_for_brand_review`,
  `baseline_held_ads_for_brand_review`) and `baseline_count` of a baseline
  made under Plan B are null after the change to Plan A, for a member, the
  owner (ctx=None) and a personal token, on the alert list, the alert
  detail and Overview. At Plan B the same counts still show.
- LOW-2: an alert with both unmarked and pa1 events at Plan A: Overview
  counts and shows only the pa1 events, and no ref of an unmarked event
  appears.
"""
from __future__ import annotations

import json
import unittest

from earwitness.app import access, customer, services
from tests.test_plan_a_fixture import ENTRY, Workspace, leaked, member, strings, token

PAT = ("research:read", "watchlists:read", "campaigns:read")
HIDDEN = {"version": 2, "mode": "ads", "q": "qzlate"}
HELD = ("baseline_held_for_brand_review", "baseline_held_ads_for_brand_review")


def pat():
    return token(PAT, client_id="pat:probe")


def refs_in(value) -> list:
    """Every source ref string anywhere in value."""
    return [text for _path, text in strings(value) if "sr1:" in text or "sr1%3A" in text]


class HeldCountsTest(unittest.TestCase):
    """LOW-1 (kills mutant M1: `held_ok` without `not withheld`)."""

    def setUp(self):
        self.ws = Workspace("B")
        self.addCleanup(self.ws.close)
        con = self.ws.con
        self.alert_id = services.customer_save_alert(
            member(), con, ENTRY, "Hidden words", HIDDEN, "/searches", ["new_finding"])["id"]
        self.ws.add_ad("ad-a6", "ep_d", 98.0, 122.0, "Nimbus", "nimbus again")
        services.customer_check_alert(member(), con, ENTRY, self.alert_id)
        # DEMO2 held provenance on the unmarked Plan B baseline: one read held
        # for brand review, with a valid ref every caller here may see.
        baseline = json.loads(con.execute("SELECT baseline_json FROM customer_alerts"
                                          " WHERE id=?", (self.alert_id,)).fetchone()[0])
        self.assertNotIn("basis", baseline)
        baseline.update(held_for_brand_review=1, held_ads_for_brand_review=1,
                        held_refs=[self.ws.ref("evidence", "ad-a1")])
        con.execute("UPDATE customer_alerts SET baseline_json=? WHERE id=?",
                    (json.dumps(baseline), self.alert_id))

    def reads(self, callers=(("member", member), ("owner", None), ("token", pat))):
        """(caller, read, alert DTO) for the list and detail of each caller."""
        con, out = self.ws.con, []
        for name, make in callers:
            if make is None:
                listed = customer.list_alerts(con)
                detail = customer.get_alert(con, self.alert_id)
            else:
                listed = services.customer_alerts(make(), con, ENTRY)["alerts"]
                detail = services.customer_alert(make(), con, ENTRY, self.alert_id)["alert"]
            mine = [a for a in listed if a["id"] == self.alert_id]
            self.assertEqual(len(mine), 1, name)
            out += [(name, "list", mine[0]), (name, "detail", detail)]
        return out

    def overviews(self):
        con = self.ws.con
        return (("member", services.customer_overview(member(), con, ENTRY)),
                ("owner", customer.overview(con)),
                ("token", services.customer_overview(pat(), con, ENTRY)))

    def test_plan_b_control_shows_the_held_counts(self):
        """Member and owner see the stored counts. The personal token has no
        transcript scope, so at Plan B it may not disclose the members (found
        in text after character 600) and its alert reads refuse (403)."""
        for caller, read, alert in self.reads((("member", member), ("owner", None))):
            with self.subTest(caller=caller, read=read):
                self.assertEqual(alert["baseline_count"], 2)
                self.assertEqual([alert[k] for k in HELD], [1, 1])
        for read in (lambda: services.customer_alerts(pat(), self.ws.con, ENTRY),
                     lambda: services.customer_alert(pat(), self.ws.con, ENTRY, self.alert_id)):
            with self.assertRaises(access.AuthorizationError) as raised:
                read()
            self.assertEqual(raised.exception.status, 403)
        for caller, overview in self.overviews():
            with self.subTest(caller=caller, read="overview"):
                self.assertEqual([t["alert_id"] for t in overview["alert_totals"]],
                                 [self.alert_id])

    def test_plan_a_nulls_the_held_counts_and_the_baseline_count(self):
        self.ws.plan("A")
        for caller, read, alert in self.reads():
            with self.subTest(caller=caller, read=read):
                self.assertIs(alert["history_withheld"], True)
                self.assertIsNone(alert["baseline_count"])
                self.assertEqual([alert[k] for k in HELD], [None, None])
                self.assertEqual(alert["events"], [])
                self.assertEqual((alert["unread_event_count"], alert["unread_ad_count"]),
                                 (0, 0))
                self.assertEqual(refs_in({k: v for k, v in alert.items()
                                          if k != "current_matches"}), [])
                self.assertEqual(leaked({k: v for k, v in alert.items()
                                         if k not in ("query", "selector")}), [])
        for caller, overview in self.overviews():
            with self.subTest(caller=caller, read="overview"):
                self.assertEqual((overview["recent_events"], overview["alert_totals"]),
                                 ([], []))
                self.assertEqual(leaked(overview), [])


class OverviewMixedEventsTest(unittest.TestCase):
    """LOW-2 (kills mutant M2: Overview `all_events` without the pa1 filter).

    One alert on "lumen": under Plan B a backfill writes an unmarked past
    event (ad-a1) and a check writes an unmarked new event (ad-a6); under
    Plan A a first check starts the pa1 baseline and a second writes one
    pa1 new event (ad-a7)."""

    def setUp(self):
        self.ws = ws = Workspace("B")
        self.addCleanup(ws.close)
        self.alert_id = services.customer_save_alert(
            member(), ws.con, ENTRY, "Lumen", {"version": 2, "mode": "ads", "q": "lumen"},
            "/searches", ["new_finding"])["id"]
        self.assertEqual(services.customer_alert_backfill(
            member(), ws.con, ENTRY, self.alert_id, True)["imported"], 1)
        ws.add_ad("ad-a6", "ep_a", 30.0, 45.0, "Lumen", "lumen once more")
        self.assertEqual(services.customer_check_alert(
            member(), ws.con, ENTRY, self.alert_id)["emitted"], 1)
        ws.plan("A")
        services.customer_check_alert(member(), ws.con, ENTRY, self.alert_id)
        ws.add_ad("ad-a7", "ep_a", 30.0, 45.0, "Lumen", "lumen again qzstored")
        self.assertEqual(services.customer_check_alert(
            member(), ws.con, ENTRY, self.alert_id)["emitted"], 1)

    def events(self):
        return [dict(r, basis=json.loads(r["payload_json"]).get("basis"))
                for r in self.ws.con.execute(
                    "SELECT id, historical, after_ref, payload_json FROM customer_alert_events"
                    " WHERE alert_id=? ORDER BY id", (self.alert_id,))]

    def test_the_alert_holds_both_unmarked_and_pa1_events(self):
        events = self.events()
        self.assertEqual([(e["basis"], bool(e["historical"])) for e in events],
                         [(None, True), (None, False), ("pa1", False)])

    def test_overview_counts_and_shows_only_the_pa1_events(self):
        events = self.events()
        pa1 = [e for e in events if e["basis"] == "pa1"]
        unmarked_refs = {e["after_ref"] for e in events if e["basis"] is None}
        self.assertTrue(all(unmarked_refs))
        self.assertNotIn(pa1[0]["after_ref"], unmarked_refs)
        callers = (("member", lambda: services.customer_overview(member(), self.ws.con, ENTRY)),
                   ("owner", lambda: customer.overview(self.ws.con)),
                   ("token", lambda: services.customer_overview(pat(), self.ws.con, ENTRY)))
        for caller, read in callers:
            with self.subTest(caller=caller):
                overview = read()
                self.assertEqual([e["id"] for e in overview["recent_events"]],
                                 [pa1[0]["id"]])
                totals = [t for t in overview["alert_totals"]
                          if t["alert_id"] == self.alert_id]
                self.assertEqual(len(totals), 1)
                total = totals[0]
                self.assertEqual((total["events"], total["unread"]), (1, 1))
                self.assertEqual((total["ads"], total["unread_ads"]), (1, 1))
                self.assertEqual((total["new_ads"], total["past_ads"],
                                  total["new_unread_ads"]), (1, 0, 1))
                self.assertEqual(total["latest_new_after_ref"], pa1[0]["after_ref"])
                self.assertEqual(total["latest_after_ref"], pa1[0]["after_ref"])
                self.assertEqual((total["latest_past_kind"], total["latest_past_after_ref"],
                                  total["latest_past_observed_at"],
                                  total["latest_past_status"]), (None, None, None, None))
                found = set(refs_in(overview))
                self.assertEqual(sorted(r for r in unmarked_refs
                                        if any(r in f for f in found)), [])
                self.assertEqual(leaked(overview), [])


if __name__ == "__main__":
    unittest.main()
