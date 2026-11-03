"""ASK-WAYFAIR r3b: scripts/gen_plain_alias_words.py sorts the no-display
alias keys made only of dictionary words by their attributed detected ads."""
import importlib.util
import sqlite3
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "gen_plain_alias_words", Path(__file__).resolve().parents[1] / "scripts" / "gen_plain_alias_words.py")
gen = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gen)

WORDS = {"get", "up", "every", "day", "grow", "therapy", "her", "progressive", "five", "hour",
         "energy", "simply", "safe"}
# raw/canonical key -> attributed detected ads
ROWS = {"get up": 1, "every day": 2, "five-hour energy": 0, "grow therapy": 3, "simply safe": 38,
        "her": 0, "progressive": 3, "squarespace": 9, "simply safer": 0, "store": 5}


def _db():
    con = sqlite3.connect(":memory:")
    con.executescript(
        "CREATE TABLE ad_brand_aliases (raw_key TEXT, canonical_key TEXT, display TEXT);"
        "CREATE TABLE ad_reads (evidence_id TEXT);"
        "CREATE TABLE brand_assocs (evidence_id TEXT, brand TEXT, basis TEXT);")
    for key, n in ROWS.items():
        con.execute("INSERT INTO ad_brand_aliases VALUES (?,?,NULL)", (key, key))
        for i in range(n):
            ev = f"{key}-{i}"
            con.execute("INSERT INTO ad_reads VALUES (?)", (ev,))
            con.execute("INSERT INTO brand_assocs VALUES (?,?,?)", (ev, key, "suggested"))
    # A display row and a rejected attribution count for nothing.
    con.execute("INSERT INTO ad_brand_aliases VALUES ('day up', 'day up', 'Day Up')")
    con.execute("INSERT INTO ad_reads VALUES ('x1')")
    con.execute("INSERT INTO brand_assocs VALUES ('x1', 'get up', 'rejected')")
    return con


class PlainWordList(unittest.TestCase):
    def test_words_holds_only_single_words_with_few_ads(self):
        self.assertEqual(gen.build(_db(), WORDS, 3)["plain"], ["her"])

    def test_capital_only_holds_advertising_words_and_phrases_with_few_ads(self):
        # Owner r3c: a phrase with fewer than 3 ads is capitalised-only.
        self.assertEqual(gen.build(_db(), WORDS, 3)["capital_only"],
                         ["every day", "five-hour energy", "get up", "progressive"])

    def test_a_phrase_with_3_or_more_ads_is_in_neither_list(self):
        # Matched in any case (S9).
        out = gen.build(_db(), WORDS, 3)
        self.assertFalse({"grow therapy", "simply safe"} & set(out["plain"] + out["capital_only"]))

    def test_a_phrase_with_a_non_dictionary_token_stays_checked(self):
        out = gen.build(_db(), WORDS - {"safe"}, 3)
        self.assertNotIn("simply safe", out["plain"] + out["capital_only"])
        self.assertNotIn("simply safer", out["plain"])


if __name__ == "__main__":
    unittest.main()
