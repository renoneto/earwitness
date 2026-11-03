"""UAT fix 1: a read with an unknown publication date sorts last for both
"Newest first" and "Oldest first". "Unknown" means the same thing the
date_status filter means: no valid ISO date prefix (empty or malformed)."""
from __future__ import annotations

import unittest

from earwitness.app import customer


def row(ref, pub):
    return {"ref": ref, "sort_ref": ref, "pub_date": pub}


ROWS = [
    row("a", "unknown"), row("b", "2026-10-09"), row("c", ""),
    row("d", "2026-06-18"), row("e", "Thu, 09 Oct 2026 10:00:00 GMT"),
    row("f", "2026-10-09T08:00:00Z"), row("g", "2026-13-45"), row("h", None),
]
UNKNOWN = {"a", "c", "e", "g", "h"}


class UnknownDatesSortLast(unittest.TestCase):
    def check(self, order, sort):
        refs = [r["ref"] for r in order(list(ROWS), sort)]
        self.assertEqual(sorted(refs), sorted(r["ref"] for r in ROWS), "no read is dropped")
        known = [r for r in refs if r not in UNKNOWN]
        self.assertEqual(set(refs[:len(known)]), set(known), f"{sort}: known dates come first")
        self.assertEqual(set(refs[len(known):]), UNKNOWN, f"{sort}: unknown dates come last")
        return refs

    def test_light_rows_newest(self):
        refs = self.check(customer._order_light, "newest")
        self.assertEqual(refs[:3], ["f", "b", "d"])

    def test_light_rows_oldest(self):
        refs = self.check(customer._order_light, "oldest")
        self.assertEqual(refs[:3], ["d", "b", "f"])

    def test_items_newest(self):
        self.assertEqual(self.check(customer._order_items, "newest")[:3], ["f", "b", "d"])

    def test_items_oldest(self):
        self.assertEqual(self.check(customer._order_items, "oldest")[:3], ["d", "b", "f"])


if __name__ == "__main__":
    unittest.main()
