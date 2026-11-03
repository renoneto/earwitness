"""Unqualified legacy definitions remain preserved, readable and non-executable."""
import json
import unittest

from earwitness.app import briefs, campaigns, db, ops
from tests import backend_fixtures
from tests.test_backend_allocation import _brief, _placement, _supplied


class LegacyProvenanceConsumers(unittest.TestCase):
    def setUp(self):
        self.con = backend_fixtures.make_memory_workspace()
        self.addCleanup(self.con.close)
        self.campaign = _supplied(self.con, _brief(self.con))["campaign"]

    def test_incomplete_and_string_unit_provenance_is_readonly_not_blessed(self):
        unit = _placement(self.con, self.campaign["id"], self.campaign["revision"])["placement"]
        valid = json.loads(self.con.execute("SELECT provenance FROM expected_placements WHERE id=?", (unit["id"],)).fetchone()[0])
        for invalid in ("original-unregistered", {"brief_id": valid["brief_ref"]["brief_id"], "version": 1}):
            malformed = json.dumps({"brief_ref": invalid})
            self.con.execute("UPDATE expected_placements SET provenance=? WHERE id=?", (malformed, unit["id"]))
            parent = dict(self.con.execute("SELECT * FROM campaigns WHERE id=?", (self.campaign["id"],)).fetchone())
            projected = campaigns.campaign_projection(self.con, parent)
            self.assertEqual(projected["mode"], "supplied_requirements")
            self.assertEqual(projected["availability"], "legacy_review_required")
            self.assertEqual(projected["allowed_actions"], ["read", "stored_reports"])
            self.assertIsNone(projected["requirement_provenance"])
            with self.assertRaises(ops.BadInput):
                campaigns.build_customer_draft(self.con, parent["id"])
            with self.assertRaises(ops.BadInput):
                campaigns.allocate_placement_evidence(self.con, parent["id"], {"action": "reference"}, parent["revision"])
            self.assertEqual(self.con.execute("SELECT provenance FROM expected_placements WHERE id=?", (unit["id"],)).fetchone()[0], malformed)

    def test_input_locator_resolves_real_projection_but_incomplete_stored_projection_rejects(self):
        made = briefs.create_brief(self.con, {"title": "Chosen source", "provided_text": "Entered requirements"})
        locator = {"kind": "supplied_brief", "brief_id": made["brief_id"], "version": 1}
        projection = campaigns.validate_provenance(self.con, {"brief_ref": locator})
        self.assertEqual(projection["brief_ref"]["digest"], made["digest"])
        with self.assertRaises(ops.BadInput):
            campaigns.validate_provenance(self.con, {"brief_ref": {"brief_id": made["brief_id"], "version": 1}})
