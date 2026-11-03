"""Legacy review reasons are plain product sentences, never raw Python errors."""
import json
import unittest

from earwitness.app import campaigns, conversion, customer, db, services
from tests import backend_fixtures

FREE_TEXT = "the placement's brief is free text, not a registered brief"
RAW_FRAGMENTS = ("string indices", "Expecting value", "object has no attribute",
                 "KeyError", "TypeError", "ValueError", "'brief_id'", "'version'",
                 "'digest'", "not subscriptable", "NoneType")


class LegacyReasons(unittest.TestCase):
    def setUp(self):
        self.con = backend_fixtures.make_memory_workspace()
        self.addCleanup(self.con.close)

    def _campaign(self, *, mode=None, brief_ref_json="{}", notes="Owner notes"):
        cur = self.con.execute(
            "INSERT INTO campaigns (name,status,brand,notes,mode,brief_ref_json,updated_at)"
            " VALUES ('Legacy Q3','active','acme',?,?,?,?)",
            (notes, mode, brief_ref_json, db.utcnow()))
        return cur.lastrowid

    def _placement(self, campaign_id, provenance, requirements=None, pid="p-legacy"):
        if requirements is None:
            requirements = json.dumps({"message": "x", "priority": 0})
        if not isinstance(provenance, str):
            provenance = json.dumps(provenance)
        self.con.execute(
            "INSERT INTO expected_placements (id,campaign_id,show_id,requirements,provenance,updated_at)"
            " VALUES (?,?,?,?,?,?)", (pid, campaign_id, "s1", requirements, provenance, db.utcnow()))
        return pid

    def _entry(self, key):
        report = conversion.dry_run(self.con)
        return next(e for e in report["definitions"] if e["key"] == key)

    def _camp(self, cid):
        return dict(self.con.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone())

    def _assert_clean(self, reasons):
        for reason in reasons:
            for fragment in RAW_FRAGMENTS:
                self.assertNotIn(fragment, reason)

    def _expect_requirements(self, provenance, sentence, requirements=None):
        cid = self._campaign()
        self._n = getattr(self, "_n", 0) + 1
        pid = self._placement(cid, provenance, requirements, pid=f"p-legacy-{self._n}")
        entry = self._entry(f"requirements:{pid}")
        self.assertEqual(entry["disposition"], "needs_review")
        self.assertEqual(entry["notes"], [sentence])
        camp = self._camp(cid)
        self.assertTrue(customer.is_customer_campaign(self.con, cid))
        dto = campaigns.campaign_projection(self.con, camp)
        self.assertEqual(dto["availability"], "legacy_review_required")
        self.assertIn(sentence, dto["reasons"])
        self._assert_clean(dto["reasons"])
        for row in campaigns.customer_list_summaries(self.con, [camp]):
            self.assertEqual(row["availability"], "legacy_review_required")
        # VL-10: a legacy row is read-only, never "Tracking".
        self.assertEqual(customer.campaign_customer_state(self.con, cid), "Read-only")
        with self.assertRaises(Exception) as caught:
            campaigns.build_customer_draft(self.con, cid)
        self._assert_clean([str(caught.exception)])
        self.assertIn(sentence, str(caught.exception))

    def test_free_text_brief_ref(self):
        self._expect_requirements({"brief_ref": "Q3 brief from sales"}, FREE_TEXT)

    def test_missing_or_empty_brief_ref(self):
        none = "the placement has no registered brief"
        self._expect_requirements({}, none)
        self._expect_requirements({"brief_ref": None}, none)
        self._expect_requirements({"brief_ref": ""}, none)

    def test_incomplete_brief_ref_dict(self):
        self._expect_requirements({"brief_ref": {"brief_id": "b1"}},
                                  "the placement's brief reference is incomplete")

    def test_unreadable_provenance_and_requirements(self):
        for raw in ("not json at all", "[1, 2]", '"text"'):
            self._expect_requirements(raw, "the placement's stored provenance is unreadable")
        self._expect_requirements({"brief_ref": "x"}, "the placement's stored requirements are unreadable",
                                  requirements="not json")
        self._expect_requirements({"brief_ref": "x"}, "the placement's stored requirements are unreadable",
                                  requirements="[1]")

    def test_undecodable_provenance_keeps_list_and_detail_working(self):
        cid = self._campaign()
        self._placement(cid, "Q3 brief from sales", requirements="also not json")
        camp = self._camp(cid)
        placements = campaigns.list_expected(self.con, cid)
        self.assertEqual((placements[0]["requirements"], placements[0]["provenance"]), ({}, {}))
        rows = campaigns.customer_list_summaries(self.con, [camp])
        self.assertEqual(rows[0]["availability"], "legacy_review_required")
        self.assertEqual(customer.customer_placement_cards(self.con, cid)[0]["placement_id"], "p-legacy")
        detail = campaigns.campaign_projection(self.con, camp)
        self.assertEqual(detail["availability"], "legacy_review_required")
        everything = json.dumps([rows, detail], default=str)
        for fragment in RAW_FRAGMENTS:
            self.assertNotIn(fragment, everything)
        self.assertIn("the placement's stored requirements are unreadable", detail["reasons"])

    def test_brief_reference_with_wrong_types_is_incomplete(self):
        good = {"brief_id": "b1", "version": 1, "digest": "d"}
        bad = ({"brief_id": {"x": 1}}, {"brief_id": ""}, {"brief_id": ["b"]}, {"digest": None},
               {"digest": 5}, {"version": {"x": 1}}, {"version": True}, {"version": None},
               {"version": "1.5"}, {"version": [1]}, {"version": ""})
        for change in bad:
            ref = {**good, **change}
            sentence = "the placement's brief reference is incomplete"
            self._expect_requirements({"brief_ref": ref}, sentence)
            cid = self._campaign(mode="supplied_requirements", brief_ref_json=json.dumps(ref))
            entry = self._entry(f"campaign:{cid}")
            self.assertEqual(entry["notes"], ["the campaign's brief reference is incomplete"], ref)
            dto = campaigns.campaign_projection(self.con, self._camp(cid))
            self.assertEqual(dto["availability"], "legacy_review_required")

    def test_repeated_placement_problems_are_listed_once(self):
        cid = self._campaign()
        for n in range(3):
            self._placement(cid, {"brief_ref": "Q3 brief from sales"}, pid=f"p-dup-{n}")
        problems = campaigns._definition_problems(self.con, self._camp(cid))
        self.assertEqual(problems.count(FREE_TEXT), 1)
        self.assertEqual(len(problems), len(set(problems)))
        self.assertEqual(campaigns.campaign_projection(self.con, self._camp(cid))["reasons"].count(FREE_TEXT), 1)

    def test_campaign_kind_brief_problems(self):
        cases = (("Q3 brief from sales", "the campaign's brief is free text, not a registered brief"),
                 ("not json {", "the campaign's brief is free text, not a registered brief"),
                 (json.dumps({"brief_id": "b1"}), "the campaign's brief reference is incomplete"),
                 (json.dumps([1]), "the campaign's brief reference is incomplete"))
        for raw, sentence in cases:
            cid = self._campaign(mode="supplied_requirements", brief_ref_json=raw)
            entry = self._entry(f"campaign:{cid}")
            self.assertEqual(entry["disposition"], "needs_review", raw)
            self.assertEqual(entry["notes"], [sentence], raw)
            dto = campaigns.campaign_projection(self.con, self._camp(cid))
            self.assertEqual(dto["availability"], "legacy_review_required")
            self.assertIn(sentence, dto["reasons"])
            self._assert_clean(dto["reasons"])

    def test_raw_exception_text_never_becomes_a_note(self):
        entry = conversion._classify(self.con, {"kind": "native_filter", "original": "{not json"})
        self.assertEqual(entry["disposition"], "needs_review")
        self._assert_clean(entry["notes"])
        self.assertNotIn("line 1 column", entry["notes"][0])
        entry = conversion._classify(self.con, {"kind": "tracker", "original": {"query": "{x", "selector": None}})
        self.assertEqual(entry["notes"], ["legacy tracker definition is unreadable"])
        entry = conversion._classify(self.con, {"kind": "reader_profile", "original": "oops"})
        self.assertEqual(entry["notes"], ["legacy reader profile is unreadable"])

    def test_domain_messages_are_kept(self):
        entry = conversion._classify(self.con, {"kind": "origin", "original": "/searches/1"})
        self.assertEqual(entry["notes"], ["retired customer origin is historical only; no runtime replay"])


class LegacyNotes(unittest.TestCase):
    setUp = LegacyReasons.setUp
    _campaign = LegacyReasons._campaign
    _placement = LegacyReasons._placement
    _camp = LegacyReasons._camp

    def test_legacy_dto_keeps_notes_read_only(self):
        cid = self._campaign(notes="Q3 plan: <b>call</b> the buyer")
        self._placement(cid, {"brief_ref": "Q3 brief from sales"})
        camp = self._camp(cid)
        dto = campaigns.campaign_projection(self.con, camp)
        self.assertEqual(dto["availability"], "legacy_review_required")
        self.assertEqual(dto["notes"], "Q3 plan: <b>call</b> the buyer")
        self.assertEqual(dto["allowed_actions"], ["read", "stored_reports"])
        self.assertNotIn("edit", dto["allowed_actions"])
        self.assertIsNone(dto["requirement_provenance"])
        empty = self._campaign(notes="")
        self._placement(empty, {"brief_ref": "x"}, pid="p-empty")
        self.assertEqual(campaigns.campaign_projection(self.con, self._camp(empty))["notes"], "")
        # The list rows are whole campaign rows, so they already carry notes.
        self.assertEqual(campaigns.customer_list_summaries(self.con, [camp])[0]["notes"],
                         "Q3 plan: <b>call</b> the buyer")


if __name__ == "__main__":
    unittest.main()
