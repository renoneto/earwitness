"""Plan A excerpt view: the Saved list carries the plan signal
(PA fix round 10; Muse pa-r1 FIX-3 and POLISH-1 on a cold Saved link).

The saved-items response says excerpt_only from
customer.excerpt_workspace, as the research-query response does, so a
Saved page opened first knows the plan without a Search read.
"""
from __future__ import annotations

import unittest

from earwitness.app import services
from tests.test_plan_a_fixture import ENTRY, Workspace, member


class SavedPlanFlagTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace("B")
        self.addCleanup(self.ws.close)

    def listing(self, **kw):
        return services.customer_saved_items(member(), self.ws.con, ENTRY, **kw)

    def test_plan_a_lists_with_excerpt_only_true(self):
        self.ws.plan("A")
        self.assertIs(self.listing()["excerpt_only"], True)
        self.assertIs(self.listing(item_type="excerpt")["excerpt_only"], True)

    def test_plan_b_lists_with_excerpt_only_false(self):
        self.assertIs(self.listing()["excerpt_only"], False)
        self.assertIs(self.listing(item_type="excerpt")["excerpt_only"], False)


if __name__ == "__main__":
    unittest.main()
