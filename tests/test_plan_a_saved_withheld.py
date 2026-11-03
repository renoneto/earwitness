"""Plan A excerpt view: the Saved list counts plan-withheld items
(PA fix round 8; Sol pa-r1 BLOCKER-1 remainder).

The Saved list drops items it cannot authorize. An item refused only by
the Plan A old-artifact rule (citations.plan_refusal, the predicate the
report list uses) is counted in plan_withheld, with no id, title or text.
Any other refusal is dropped and not counted, as before, except a
source's own deny (DENY-WORDS, UX 5e799bded): rights_withheld counts it,
and an item the plan also refuses stays in plan_withheld, counted once
(FDV-R1-02/-03; tests.test_plan_a_plan_b_parity.DenyWordsCountsTest).
"""
from __future__ import annotations

import json
import unittest

from earwitness.app import services
from tests.test_plan_a_fixture import ENTRY, Workspace, member

TITLE = "Old saved read zq7"


class _Base(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace("B")
        self.addCleanup(self.ws.close)
        self.con = self.ws.con
        self.item = services.customer_save_item(member(), self.con, ENTRY, {
            "type": "excerpt", "title": TITLE,
            "target": {"ref": self.ws.ref("evidence", "ad-a1")}})["item"]

    def listing(self, **kw):
        return services.customer_saved_items(member(), self.con, ENTRY, **kw)

    def assertNoTrace(self, out):
        raw = json.dumps(out)
        self.assertNotIn(self.item["id"], raw)
        self.assertNotIn(TITLE, raw)
        text = (self.item.get("captured") or {}).get("text")
        if text:
            self.assertNotIn(text, raw)


class PlanWithheldTest(_Base):
    def test_b_then_a_counts_the_old_excerpt_and_returns_no_item_fields(self):
        self.ws.plan("A")
        out = self.listing()
        self.assertEqual(out["plan_withheld"], 1)
        self.assertEqual(out["items"], [])
        self.assertEqual(out["total"], 0)
        self.assertNoTrace(out)
        for item_type in ("excerpt", "bookmark"):
            self.assertEqual(self.listing(item_type=item_type)["plan_withheld"], 1)

    def test_a_deny_on_an_old_item_keeps_it_in_the_plan_count_once(self):
        # FDV-R1-03 (UX 5e799bded): the plan also refuses this old item, so
        # the line the customer saw before stays; it is not counted again.
        self.ws.plan("A")
        self.ws.row("detector:ad-a1", "metadata", "deny")
        out = self.listing()
        self.assertEqual(out["items"], [])
        self.assertEqual((out.get("plan_withheld"), out.get("rights_withheld")), (1, 0))
        self.assertNoTrace(out)

    def test_plan_b_lists_the_item_and_counts_nothing(self):
        out = self.listing()
        self.assertEqual([i["id"] for i in out["items"]], [self.item["id"]])
        self.assertFalse(out.get("plan_withheld"))

    def test_plan_b_non_plan_refusal_is_not_counted(self):
        self.ws.row("detector:ad-a1", "excerpt", "deny")
        out = self.listing()
        self.assertEqual(out["items"], [])
        self.assertFalse(out.get("plan_withheld"))
        # FDV-R1-02: counted as a deny instead, with no item fields.
        self.assertEqual(out.get("rights_withheld"), 1)
        self.assertNoTrace(out)


if __name__ == "__main__":
    unittest.main()
