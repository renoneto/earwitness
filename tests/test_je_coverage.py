"""JE-COVERAGE: the coverage payload counts Screened episodes (DET-PLAN item
6, coverage.episode_screen_state) and the shows with one, beside the
totals. Overview's coverage block and each /customer/shows row carry them;
Search sums the rows. Transcript-only and stopped-partway episodes are not
Screened; a show whose only episode is not Screened is not counted.
"""
from __future__ import annotations

import unittest

from earwitness.app import coverage, customer
from tests.test_customer_campaigns import fresh_db
from tests.test_screened_state import screened_episode
from tests.test_uat_dec_cov import add_screening, seed_legacy_episode


def one_show(con, ep_ids):
    """Map ep_ids to the first one's show (the producer's mapping rows)."""
    sid = con.execute("SELECT show_identity_id FROM customer_episode_shows"
                      " WHERE episode_id=?", (ep_ids[0],)).fetchone()[0]
    for e in ep_ids[1:]:
        con.execute("UPDATE customer_episode_shows SET show_identity_id=? WHERE episode_id=?",
                    (sid, e))
    con.commit()


class ScreenedCoverage(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        self.addCleanup(self.con.close)
        con = self.con
        # Show A: one screened, one stopped partway. Show B: screened, and
        # screened with no ads. Show C: transcript only (never screened).
        # Show D: no transcript copy.
        screened_episode(con, "a1")
        screened_episode(con, "a2", stopped=1)
        screened_episode(con, "b1")
        screened_episode(con, "b2", screened=0, no_ads=1)
        tv = seed_legacy_episode(con, "c1", 60.0, 3600.0)
        add_screening(con, "c1", 3600.0, tier="transcript", screened=0)
        seed_legacy_episode(con, "d1", 60.0, 3600.0, with_transcript=False)
        one_show(con, ["a1", "a2"])
        one_show(con, ["b1", "b2"])
        self.assertIsNotNone(tv)

    def test_overview_counts_screened_not_total(self):
        cov = customer.overview(self.con)["coverage"]
        self.assertEqual(cov["episodes"], 6)
        self.assertEqual(cov["shows"], 4)
        self.assertEqual(cov["screened_episodes"], 3)
        self.assertEqual(cov["screened_shows"], 2)

    def test_counts_equal_the_one_definition(self):
        ids = ["a1", "a2", "b1", "b2", "c1", "d1"]
        states = coverage.episode_screen_state(self.con, ids)
        self.assertEqual({e for e, s in states.items() if s["screened"]},
                         set(customer.screened_episode_ids(self.con)))
        self.assertEqual({e: s["state"] for e, s in states.items() if not s["screened"]},
                         {"a2": "not_screened", "c1": "not_screened", "d1": "no_transcript"})

    def test_shows_rows_carry_screened_and_sum_to_overview(self):
        shows = customer.customer_shows(self.con)
        self.assertEqual(sorted((s["episodes"], s["screened_episodes"]) for s in shows),
                         [(1, 0), (1, 0), (2, 1), (2, 2)])
        cov = customer.overview(self.con)["coverage"]
        self.assertEqual(sum(s["screened_episodes"] for s in shows), cov["screened_episodes"])
        self.assertEqual(sum(1 for s in shows if s["screened_episodes"]), cov["screened_shows"])

    def test_stopped_partway_alone_drops_its_show(self):
        self.con.execute("UPDATE episode_screening SET stopped_partway=1 WHERE episode_id='a1'")
        self.con.commit()
        cov = customer.overview(self.con)["coverage"]
        self.assertEqual((cov["screened_episodes"], cov["screened_shows"]), (2, 1))

    def test_all_screened_equals_total(self):
        con = fresh_db()
        self.addCleanup(con.close)
        screened_episode(con, "x1")
        screened_episode(con, "x2")
        cov = customer.overview(con)["coverage"]
        self.assertEqual((cov["screened_episodes"], cov["screened_shows"]),
                         (cov["episodes"], cov["shows"]))

    def test_totals_and_fields_kept(self):
        cov = customer.overview(self.con)["coverage"]
        self.assertEqual(set(cov), {"episodes", "episodes_with_transcript", "shows",
                                    "earliest_episode", "screened_episodes", "screened_shows"})
        self.assertEqual(cov["episodes_with_transcript"], 5)

    def test_no_attestation_schema_sends_no_screened_counts(self):
        self.con.execute("DROP TABLE coverage_attestations")
        self.con.commit()
        cov = customer.overview(self.con)["coverage"]
        self.assertNotIn("screened_episodes", cov)
        self.assertNotIn("screened_shows", cov)
        self.assertTrue(all("screened_episodes" not in s for s in customer.customer_shows(self.con)))


if __name__ == "__main__":
    unittest.main()
