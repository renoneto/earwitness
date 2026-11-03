"""T2-EXPORT: a CSV or JSON export row of an ad under an excerpt-only
export rule carries the same excerpt as the frozen report for the same
pinned state: the stored excerpt cut to the pinned reviewed bounds when
they narrow the read (campaigns._reviewed_excerpt via
exports._pinned_excerpt), else the stored excerpt byte for byte.

Fixture and shaping as tests/test_t2_excerpt.py (episode e1: 0-10
WELCOME, 10-20 AD, 20-30 CAFE; fixture-ad).
"""
from __future__ import annotations

import csv
import io
import json
import unittest
from unittest import mock

from earwitness.app import citations, exports
from tests import test_t2_excerpt as t2
from tests.test_t2_excerpt import AD, CAFE, STORED, WELCOME


class ExportExcerptTest(unittest.TestCase):

    setUp = t2.ReviewedExcerptTest.setUp
    shape = t2.ReviewedExcerptTest.shape
    frozen_text = t2.ReviewedExcerptTest.frozen_text

    def exported(self):
        """{fmt: the fixture-ad row's text} of a CSV and a JSON export
        under the excerpt-only rule. The row offers wide transcript text,
        which an excerpt-only row never keeps."""
        ref = citations.current_source_ref(self.con, "evidence", "fixture-ad")
        out = {}
        for fmt in ("csv", "json"):
            row = {"ref": ref, "record_kind": "evidence", "evidence_id": "fixture-ad",
                   "text": " ".join((WELCOME, AD, CAFE)), "context": WELCOME}
            with mock.patch.object(exports, "export_locators", t2._excerpt_only):
                payload = exports.prepare_capture(
                    self.con, [row], fmt, "all_matches", "distinct_results",
                    {"query": {}}, "T2 export", ctx=citations._owner_context())["payload"]
            if fmt == "csv":
                rows = list(csv.DictReader(io.StringIO(payload)))
                out[fmt] = next(r["text"] for r in rows if r["evidence_id"] == "fixture-ad")
            else:
                out[fmt] = json.loads(payload)["items"][0]["text"]
        return out

    def test_c9_garten_no_other_brand_text_in_csv_or_json(self):
        # c9 ShipStation: the stored excerpt opens on the ad before it.
        self.shape((0.0, 20.0), WELCOME + " " + AD, eff=(10.0, 20.0))
        for fmt, text in self.exported().items():
            with self.subTest(fmt=fmt):
                self.assertEqual(text, AD)
                self.assertNotIn("Welcome", text)

    def test_narrowed_end_drops_the_ad_after_it(self):
        self.shape((10.0, 30.0), AD + " " + CAFE, eff=(10.0, 20.0))
        for fmt, text in self.exported().items():
            with self.subTest(fmt=fmt):
                self.assertEqual(text, AD)

    def test_export_row_equals_the_frozen_report_for_a_narrowed_read(self):
        for orig, stored, eff in (((0.0, 20.0), WELCOME + " " + AD, (10.0, 20.0)),
                                  ((0.0, 20.0), WELCOME + " " + AD, (10.0, 30.0)),
                                  ((0.0, 30.0), WELCOME + " " + AD + " " + CAFE, (12.0, 18.0))):
            with self.subTest(orig=orig, eff=eff):
                self.setUp()
                self.shape(orig, stored, eff=eff)
                frozen = self.frozen_text()
                self.assertNotEqual(frozen, stored)
                self.assertEqual(self.exported(), {"csv": frozen, "json": frozen})

    def test_an_unnarrowed_read_keeps_its_stored_excerpt_byte_for_byte(self):
        for eff in (None, (10.0, 20.0), (0.0, 30.0), (20.0, 30.0)):
            with self.subTest(eff=eff):
                self.setUp()
                self.shape((10.0, 20.0), STORED, eff=eff)
                self.assertEqual(self.exported(), {"csv": STORED, "json": STORED})
                self.assertEqual(self.frozen_text(), STORED)

    def test_segments_without_finite_ordered_timings_never_reach_an_export(self):
        from earwitness.app import observations
        self.shape((0.0, 20.0), WELCOME + " " + AD, eff=(10.0, 20.0))
        good = {"start": 10.0, "end": 20.0, "text": AD}
        bad = [{"text": "LEAK"}, {"start": 12.0, "end": None, "text": "LEAK"},
               {"start": 12.0, "end": 0, "text": "LEAK"},
               {"start": "13", "end": "14", "text": "LEAK"},
               {"start": float("nan"), "end": 14.0, "text": "LEAK"},
               {"start": 12.0, "end": float("inf"), "text": "LEAK"},
               {"start": True, "end": 14.0, "text": "LEAK"},
               {"start": 14.0, "end": 13.0, "text": "LEAK"}]
        for segment in bad:
            with self.subTest(segment=segment), \
                    mock.patch.object(observations, "_segments_of",
                                      lambda con, vid, seg=segment: [seg, good]):
                self.assertEqual(self.exported(), {"csv": AD, "json": AD})

    def test_malformed_pinned_bounds_keep_the_stored_excerpt_in_exports(self):
        # Reversed, non-finite, null or 3-element pinned bounds never cut
        # (and so never widen): the stored excerpt, byte for byte.
        nan, inf = float("nan"), float("inf")
        self.shape((0.0, 20.0), STORED, eff=(10.0, 20.0))
        state_id = citations._parse_source_ref(
            citations.current_source_ref(self.con, "evidence", "fixture-ad"))[0]
        for original, effective in (([0.0, 20.0], [18.0, 12.0]), ([20.0, 0.0], [10.0, 15.0]),
                                    ([0.0, 20.0], [nan, 15.0]), ([nan, 20.0], [10.0, 15.0]),
                                    ([0.0, inf], [10.0, 15.0]), ([0.0, 20.0], [None, 15.0]),
                                    ([0.0, 20.0, 30.0], [10.0, 15.0])):
            bounds = json.dumps({"original": original, "effective": effective})
            with self.subTest(original=original, effective=effective), \
                    mock.patch.object(exports, "_publication", lambda con, ref, b=bounds: dict(
                        con.execute("SELECT kind, record_id, pins_json FROM source_states"
                                    " WHERE id=?", (state_id,)).fetchone(), bounds_json=b)):
                self.assertEqual(exports._pinned_excerpt(self.con, "unused"), STORED)


if __name__ == "__main__":
    unittest.main()
