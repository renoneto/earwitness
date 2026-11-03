"""Plan A excerpt view, PA fix round 14 (Muse pa-r2): plan signals a
cold page reads, and the cut mark on episode ad rows.

- GET /customer/preferences carries excerpt_only (FIX-3: a cold Settings
  page gates "Open Search on").
- GET /customer/shows/<id> carries excerpt_only (FIX-4: a cold show page
  says transcripts are not included in the plan).
- GET /customer/episodes/<id> ad rows carry truncated: true when rule pa1
  cut the excerpt at 600 characters (POLISH-2). Display only.

Red-capable command:
  PYTHONDONTWRITEBYTECODE=1 V/.venv-hosted/bin/python -B -m unittest tests.test_plan_a_pa14_flags
"""
from __future__ import annotations

import unittest

from earwitness.app import services
from tests.test_plan_a_fixture import ENTRY, Workspace, member, safe_text


class _Base(unittest.TestCase):
    plan = "A"

    def setUp(self):
        self.ws = Workspace("B")
        self.addCleanup(self.ws.close)
        if self.plan == "A":
            self.ws.plan("A")

    def prefs(self):
        return services.customer_preferences(member(), self.ws.con, ENTRY)

    def shows(self):
        shows = services.customer_shows(member(), self.ws.con, ENTRY)["shows"]
        self.assertTrue(shows)
        return [services.customer_show_detail(member(), self.ws.con, ENTRY, s["id"]) for s in shows]

    def episode(self, ep):
        return services.customer_episode_reads(member(), self.ws.con, ENTRY, ep)["episode"]


class PlanATest(_Base):
    def test_preferences_carry_excerpt_only(self):
        self.assertIs(self.prefs()["excerpt_only"], True)

    def test_show_detail_carries_excerpt_only(self):
        for out in self.shows():
            self.assertIs(out["excerpt_only"], True)

    def test_a_cut_episode_ad_row_is_flagged(self):
        ads = {a["evidence_id"]: a for a in self.episode("ep_d")["ads"]}
        self.assertEqual(ads["ad-a5"]["text"], safe_text("ad-a5"))
        self.assertIs(ads["ad-a5"].get("truncated"), True)

    def test_an_uncut_episode_ad_row_is_not_flagged(self):
        for ad in self.episode("ep_a")["ads"]:
            self.assertNotIn("truncated", ad)


class PlanBControlTest(_Base):
    plan = "B"

    def test_preferences_say_not_excerpt(self):
        self.assertIs(self.prefs()["excerpt_only"], False)

    def test_show_detail_says_not_excerpt(self):
        for out in self.shows():
            self.assertIs(out["excerpt_only"], False)

    def test_plan_b_episode_rows_have_no_flag(self):
        for ep in ("ep_a", "ep_d"):
            for ad in self.episode(ep)["ads"]:
                self.assertNotIn("truncated", ad)


if __name__ == "__main__":
    unittest.main()
