"""QW7 copy pass: the campaign linkage label is "Ad found".

A report frozen before the change keeps the "Possible ad" bytes it was
frozen with; its digest still verifies. A report frozen now says "Ad found".
"""
from __future__ import annotations

import hashlib
import unittest
from unittest import mock

from earwitness.app import campaigns, citations, customer
from tests.test_customer_campaigns import (
    add_candidate, add_placement, fresh_db, seed_campaign, seed_full_episode, verify_full_ad)


class FrozenLinkageLabelTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        seed_full_episode(self.con, ep_id="ep-full")
        self.cid = seed_campaign(self.con)

    def tearDown(self):
        self.con.close()

    def _freeze(self):
        pid = add_placement(self.con, self.cid, episode_id="ep-full")
        add_candidate(self.con, "x", episode_id="ep-full")
        verify_full_ad(self.con, "x", episode_id="ep-full")
        rev = self.con.execute("SELECT revision FROM campaigns WHERE id=?",
                               (self.cid,)).fetchone()[0]
        ref = citations.current_source_ref(self.con, "evidence", "x")
        out = campaigns.save_placement_evidence(self.con, self.cid, pid, ref, rev)
        draft = campaigns.build_customer_draft(self.con, self.cid)
        self.assertEqual(draft["allocation"]["evaluations"][pid]["outcome"], "supported")
        return campaigns.freeze_customer_report(
            self.con, self.cid, out["campaign_revision"], draft["pins"])

    def test_report_frozen_before_the_change_keeps_possible_ad(self):
        with mock.patch.object(customer, "LINKAGE_POSSIBLE", "Possible ad"):
            frozen = self._freeze()
            stored, _ = campaigns.get_customer_report_html(
                self.con, self.cid, frozen["report_no"])
        self.assertIn("Possible ad", stored)
        # Label restored: the stored bytes are served unchanged and verify.
        body, digest = campaigns.get_customer_report_html(
            self.con, self.cid, frozen["report_no"])
        self.assertEqual(body, stored)
        self.assertIn("Possible ad", body)
        self.assertNotIn("Ad found;", body)
        self.assertEqual(digest, hashlib.sha256(stored.encode()).hexdigest())

    def test_report_frozen_now_says_ad_found(self):
        frozen = self._freeze()
        body, _ = campaigns.get_customer_report_html(
            self.con, self.cid, frozen["report_no"])
        self.assertIn("Ad found", body)
        self.assertNotIn("Possible ad", body)


if __name__ == "__main__":
    unittest.main()
