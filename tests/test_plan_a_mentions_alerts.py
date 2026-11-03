"""Plan A excerpt view, PA-FU6: All mentions alerts at excerpt level.

At Plan A a NEW mentions alert is refused (400, details reason plan, nothing
written), and so is changing an existing alert to mentions. An existing
mentions alert stays editable and keeps its mode. Its check says
`plan_limited: true`; Plan B and ads/episodes alerts carry no such field.
"""
from __future__ import annotations

import json
import unittest

from earwitness.app import access, customer, services
from tests.test_plan_a_fixture import ENTRY, Workspace, member

MENTIONS = {"version": 2, "mode": "mentions", "q": "lumen"}
ADS = {"version": 2, "mode": "ads", "q": "lumen"}
EPISODES = {"version": 2, "mode": "episodes", "q": "lumen"}
MESSAGE = "All mentions is not included in your plan."


class _Base(unittest.TestCase):
    plan = "A"

    def setUp(self):
        self.ws = Workspace(self.plan)
        self.addCleanup(self.ws.close)
        self.con = self.ws.con

    def save(self, name, query, **kw):
        return services.customer_save_alert(member(), self.con, ENTRY, name, query,
                                            "/searches", None, **kw)

    def check(self, aid):
        return services.customer_check_alert(member(), self.con, ENTRY, aid)

    def rows(self):
        return [(r[0], r[1], json.loads(r[2]).get("mode")) for r in self.con.execute(
            "SELECT id, name, query_json FROM customer_alerts ORDER BY id")]

    def counts(self):
        return tuple(self.con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                     for t in ("customer_alerts", "customer_alert_events", "audit"))

    def assertPlanRefusal(self, raised):
        exc = raised.exception
        self.assertIsInstance(exc, access.AuthorizationError)
        self.assertEqual(exc.status, 400)
        self.assertEqual(getattr(exc, "details", None), {"reason": "plan"})
        self.assertEqual(str(exc), MESSAGE)


class CreateAtPlanATest(_Base):
    def test_a_new_mentions_alert_is_refused_and_nothing_is_written(self):
        before = self.counts()
        with self.assertRaises(Exception) as raised:
            self.save("New mentions", MENTIONS)
        self.assertPlanRefusal(raised)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.rows(), [])

    def test_the_domain_save_refuses_before_any_write(self):
        # The refusal comes before the alert snapshot and the insert: the
        # snapshot is never built (code review r2: save_alert's transaction
        # would otherwise hide a refusal raised after the insert).
        from unittest import mock
        before = self.counts()
        with mock.patch.object(customer, "_alert_snapshot",
                               side_effect=AssertionError("snapshot built before the refusal")), \
                self.assertRaises(Exception) as raised:
            customer.save_alert(self.con, "New mentions", MENTIONS, "/searches",
                                ctx=member())
        self.assertPlanRefusal(raised)
        self.assertEqual(self.counts(), before)

    def test_an_ads_alert_is_still_created_with_no_plan_limited_check(self):
        aid = self.save("Lumen ads", ADS)["id"]
        self.assertEqual(self.rows(), [(aid, "Lumen ads", "ads")])
        out = self.check(aid)
        self.assertTrue(out["ok"])
        self.assertNotIn("plan_limited", out)
        self.assertNotIn("plan_limited", self.check(aid))

    def test_an_episodes_alert_check_has_no_plan_limited(self):
        aid = self.save("Lumen episodes", EPISODES)["id"]
        self.assertNotIn("plan_limited", self.check(aid))

    def test_changing_an_ads_alert_to_mentions_is_refused(self):
        made = self.save("Lumen ads", ADS)
        before = self.counts()
        with self.assertRaises(Exception) as raised:
            self.save("Lumen ads", MENTIONS, alert_id=made["id"],
                      expected_revision=made["revision"])
        self.assertPlanRefusal(raised)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.rows(), [(made["id"], "Lumen ads", "ads")])


class ExistingMentionsAlertTest(_Base):
    plan = "B"

    def setUp(self):
        super().setUp()
        self.made = self.save("Lumen words", MENTIONS)
        self.aid = self.made["id"]

    def test_plan_b_creates_a_mentions_alert_and_its_check_has_no_plan_limited(self):
        self.assertEqual(self.rows(), [(self.aid, "Lumen words", "mentions")])
        out = self.check(self.aid)
        self.assertTrue(out["ok"])
        self.assertNotIn("plan_limited", out)

    def test_a_rename_at_plan_a_is_kept_and_the_mode_stays_mentions(self):
        self.ws.plan("A")
        out = self.save("Lumen words renamed", MENTIONS, alert_id=self.aid,
                        expected_revision=self.made["revision"])
        self.assertEqual(out["id"], self.aid)
        self.assertEqual(self.rows(), [(self.aid, "Lumen words renamed", "mentions")])

    def test_a_query_edit_that_keeps_mentions_is_kept_at_plan_a(self):
        self.ws.plan("A")
        query = {**MENTIONS, "q": "brightly"}
        self.save("Lumen words", query, alert_id=self.aid,
                  expected_revision=self.made["revision"])
        stored = json.loads(self.con.execute(
            "SELECT query_json FROM customer_alerts WHERE id=?", (self.aid,)).fetchone()[0])
        self.assertEqual((stored["mode"], stored["q"]), ("mentions", "brightly"))

    def test_the_check_at_plan_a_says_plan_limited(self):
        self.ws.plan("A")
        out = self.check(self.aid)
        self.assertIs(out.get("plan_limited"), True)
        self.assertIn("emitted", out)
        # A second check (baseline already established) says so too.
        self.assertIs(self.check(self.aid).get("plan_limited"), True)

    def test_the_check_back_at_plan_b_has_no_plan_limited(self):
        self.ws.plan("A")
        self.check(self.aid)
        self.ws.plan("B")
        self.assertNotIn("plan_limited", self.check(self.aid))


    def test_a_setup_error_check_at_plan_a_says_plan_limited(self):
        # Code review r1 LOW-1: the setup-error return keeps plan_limited.
        from unittest import mock
        from earwitness.app import ops
        self.ws.plan("A")
        with mock.patch.object(customer, "_alert_snapshot",
                               side_effect=ops.BadInput("setup failed")):
            out = self.check(self.aid)
        self.assertFalse(out["ok"])
        self.assertIs(out.get("plan_limited"), True)


class SaveAlertAtomicTest(_Base):
    """Code review r1 HIGH-1: a direct save_alert keeps its own transaction."""
    plan = "B"

    def test_a_failure_after_the_insert_leaves_no_alert(self):
        from unittest import mock
        from earwitness.app import db
        with mock.patch.object(db, "audit", side_effect=RuntimeError("after insert")):
            with self.assertRaises(RuntimeError):
                customer.save_alert(self.con, "Lumen", ADS)
        self.assertEqual(self.rows(), [])

if __name__ == "__main__":
    unittest.main()
