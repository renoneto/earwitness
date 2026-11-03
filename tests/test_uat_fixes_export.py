"""UAT fix: Search CSV cells are readable text, never Python dict/list repr.
Existing columns keep their names; two columns are added at the end."""
from __future__ import annotations

import csv
import io
import unittest

from earwitness.app import exports

SHOW = {"show_identity_id": "cs-1", "basis": "feed_url", "display_title": "The Show", "authority_basis": "feed_url", "unresolved": 0}
PROV = {"id": 25, "source_key": "jev-run", "version_no": 1, "sha256": "ab" * 32, "created_at": "2026-09-22T06:44:40+00:00"}


def read(brand=None, **kw):
    base = {"kind": "rule", "source_label": "Rule candidate",
            "brand": brand or {"key": None, "display": None, "display_basis": "none", "suggested": None, "confirmed": False},
            "needs_review": False, "self_promo": False, "display_span": [46.4, 120.0], "display_basis": "detected",
            "candidates": [], "display_text": "Visit spinquest.com today. Use code SPIN.",
            "said": {"web_addresses": ["spinquest.com"], "promo_codes": ["SPIN"], "address_brand": "Spinquest",
                     "code_brand": None, "spoken_brand": None}}
    base.update(kw)
    return base


def row(**kw):
    base = {"ref": "sr1:aa", "record_kind": "evidence", "evidence_id": "e1", "episode_id": "ep1", "show": SHOW,
            "show_title": "The Show", "ep_title": "Ep 1", "pub_date": "2026-09-21", "text": "Passage text.",
            "quotation_span": [1320.0, 1368.8], "status": "finding", "review": None,
            "attribution": [{"brand": "spinquest", "basis": "suggested", "channel": "spoken"},
                            {"brand": "spinquest", "basis": "suggested", "channel": "url"}],
            "provenance": PROV, "brand_keys": ["spinquest"], "read": read({"key": "spinquest", "display": "Spinquest", "confirmed": False})}
    base.update(kw)
    return base


def parse(rows):
    out = list(csv.DictReader(io.StringIO(exports.render_capture(rows, "csv"))))
    return out


class ReadableCsv(unittest.TestCase):
    def test_no_python_repr_in_any_cell(self):
        for cells in parse([row(), row(review={"judgment": "ad", "notes": "ok"}, read=None, attribution=[], brand_keys=[])]):
            for key, value in cells.items():
                self.assertNotRegex(value or "", r"[{\[]'|'[a-z_]+': ", f"{key}: {value!r}")

    def test_existing_columns_keep_names_and_plain_values(self):
        cells = parse([row()])[0]
        # M7: show_title, ep_title and pub_date lead as show, episode and
        # published; quotation_span (a second time range) is not written.
        for name in ("ref", "record_kind", "evidence_id", "episode_id", "show", "episode", "published", "text",
                     "kind", "attribution", "provenance", "brand_keys", "read"):
            self.assertIn(name, cells)
        for name in ("show_title", "ep_title", "pub_date", "quotation_span"):
            self.assertNotIn(name, cells)
        # PLAIN-ADS (DEP7): status becomes kind (ad / mention), review is dropped.
        self.assertEqual(cells["kind"], "ad")
        self.assertNotIn("status", cells)
        self.assertNotIn("review", cells)
        self.assertEqual(cells["show"], "The Show")
        self.assertEqual(cells["episode"], "Ep 1")
        self.assertEqual(cells["brand_keys"], "spinquest")
        self.assertEqual(cells["attribution"], "Spinquest (spoken); Spinquest (url)", "named in the read's display spelling")
        self.assertIn("jev-run", cells["provenance"])
        self.assertEqual(cells["published"], "2026-09-21")

    def test_brand_and_time_range_columns(self):
        cells = parse([row()])[0]
        self.assertEqual(cells["brand"], "Spinquest")
        self.assertEqual(cells["time_range"], "0:46–2:00")
        confirmed = parse([row(read=read({"key": "x", "display": "Acme", "confirmed": True}))])[0]
        self.assertEqual(confirmed["brand"], "Acme")

    def test_read_cell_is_a_sentence_with_spoken_and_url_apart(self):
        cells = parse([row()])[0]
        text = cells["read"]
        self.assertIn("Rule candidate", text)
        self.assertIn("; Spinquest;", text)
        self.assertNotIn("confirmed", text)
        self.assertIn("web address spinquest.com (names Spinquest)", text)
        self.assertIn("code SPIN", text)
        self.assertNotIn("spoken brand", text, "an unrecorded spoken brand is not invented")
        self.assertNotIn("Visit spinquest.com today", text, "the passage stays in the text column")

    def test_unknown_stays_unknown(self):
        cells = parse([row(pub_date="", quotation_span=None, read=read(), brand_keys=[], attribution=[], show=None, show_title="")])[0]
        self.assertEqual(cells["brand"], "Brand not identified")
        self.assertEqual(cells["published"], "")
        self.assertNotIn("quotation_span", cells)
        self.assertEqual(cells["show"], "")

    def test_held_brand_stays_hidden_and_needs_review_is_not_said(self):
        # PLAIN-ADS (DEP7): a held ad reads "Brand not identified"; a name
        # the namer flagged is shown as is, with no review words.
        hidden = parse([row(read=read({"key": "secret", "display": "Secret", "under_review": True}))])[0]
        self.assertEqual(hidden["brand"], "Brand not identified")
        self.assertNotIn("Secret", hidden["read"])
        flagged = parse([row(read=read({"key": "k", "display": "Kay", "confirmed": False}, needs_review=True))])[0]
        self.assertEqual(flagged["brand"], "Kay")

    def test_rows_without_a_read_have_empty_brand(self):
        self.assertEqual(parse([{"ref": "r1", "text": "hello"}])[0]["brand"], "")

    def test_formula_guard_and_json_unchanged(self):
        self.assertTrue(parse([row(text="=SUM(A1)")])[0]["text"].startswith("'="))
        import json
        data = json.loads(exports.render_capture([row()], "json"))
        # PLAIN-ADS (DEP7): the show identity's internal unresolved flag is left out.
        self.assertEqual(data["items"][0]["show"], {k: v for k, v in SHOW.items() if k != "unresolved"},
                         "JSON keeps the structured record")


if __name__ == "__main__":
    unittest.main()
