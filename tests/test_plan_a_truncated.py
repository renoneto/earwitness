"""Plan A excerpt view, PA fix round 9 (Muse pa-r1 POLISH-3): the excerpt
projection says when rule pa1 cut the text at 600 characters, with a
`truncated: true` flag. Display only: saved, attached and exported bytes
do not change, and Plan B is untouched.

Red-capable command:
  PYTHONDONTWRITEBYTECODE=1 V/.venv-hosted/bin/python -B -m unittest tests.test_plan_a_truncated
"""
from __future__ import annotations

import json
import unittest

from earwitness.app import campaigns, citations, services
from tests.test_plan_a_artifacts import _Base
from tests.test_plan_a_fixture import ENTRY, member, safe_text


class RuleTest(unittest.TestCase):
    def rule(self, text):
        return citations.safe_excerpt_from_segments([{"start": 0, "end": 10, "text": text}], 1, 9)

    def test_a_cut_text_is_flagged(self):
        out = self.rule(" ".join(f"w{i:04d}" for i in range(200)))  # 999 characters
        self.assertIs(out.get("truncated"), True)
        self.assertLessEqual(len(out["text"]), 600)

    def test_601_characters_are_cut_and_flagged(self):
        out = self.rule("a" * 599 + " b")
        self.assertEqual(out["text"], "a" * 599)
        self.assertIs(out.get("truncated"), True)

    def test_exactly_600_characters_are_not_flagged(self):
        out = self.rule("a" * 598 + " b")
        self.assertEqual(len(out["text"]), 600)
        self.assertNotIn("truncated", out)

    def test_a_short_text_is_not_flagged(self):
        self.assertNotIn("truncated", self.rule("first second"))


class ProjectionTest(_Base):
    def test_the_projection_flags_the_cut_excerpt_only(self):
        cut = citations.resolve_source_state(self.con, self.ref("ad-a5"), ctx=member())
        self.assertEqual(cut["disclosure"], "excerpt")
        self.assertIs(cut.get("truncated"), True)
        self.assertEqual(cut["text"], safe_text("ad-a5"))
        whole = citations.resolve_source_state(self.con, self.ref("ad-a1"), ctx=member())
        self.assertNotIn("truncated", whole)
        # The resolution pass (search rows, exports) carries no flag.
        got = citations.ResolutionPass(self.con, member()).current(self.ref("ad-a5"))
        self.assertNotIn("truncated", got)

    def test_the_record_route_carries_the_flag(self):
        cut = services.customer_record(member(), self.con, ENTRY, self.ref("ad-a5"))["record"]
        self.assertIs(cut.get("truncated"), True)
        self.assertEqual(cut["text"], safe_text("ad-a5"))
        whole = services.customer_record(member(), self.con, ENTRY, self.ref("ad-a1"))["record"]
        self.assertNotIn("truncated", whole)

    def test_saved_and_attached_bytes_do_not_change(self):
        item = self.save_excerpt(self.ref("ad-a5"))
        self.assertEqual(item["captured"]["text"], safe_text("ad-a5"))
        self.assertNotIn("truncated", item["captured"])
        stored = self.con.execute("SELECT captured_json FROM customer_saved_items WHERE id=?",
                                  (item["id"],)).fetchone()[0]
        self.assertNotIn("truncated", stored)
        self.assertNotIn("…", stored)
        captured = campaigns._captured_attachment(self.con, self.ref("ad-a5"), member())
        self.assertNotIn("truncated", captured)
        self.assertEqual(captured["text"], safe_text("ad-a5"))

    def test_exports_carry_no_flag(self):
        out = self.export("json")
        payload = self.download(out["export_id"])["payload"]
        self.assertNotIn("truncated", payload)
        self.assertNotIn("truncated", json.dumps(out["rows"]))


class PlanBControlTest(_Base):
    plan = "B"

    def test_plan_b_has_no_flag(self):
        out = citations.resolve_source_state(self.con, self.ref("ad-a5"), ctx=member())
        self.assertNotIn("disclosure", out)
        self.assertNotIn("truncated", out)
        rec = services.customer_record(member(), self.con, ENTRY, self.ref("ad-a5"))["record"]
        self.assertNotIn("truncated", rec)


if __name__ == "__main__":
    unittest.main()
