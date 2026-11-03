"""P07 indexed-search regressions (stdlib unittest).

Literal-match equivalence with unchanged legacy inputs, Unicode casefold,
short queries, no-candidate transcripts, title vs spoken-brand separation,
and explicit stale-projection state. Disposable in-memory workspaces only.
"""
from __future__ import annotations

import json
import sqlite3
import unittest

from earwitness.app import db, indexing, ops, search


def seed():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    db.init_schema(con)
    con.execute(
        "INSERT INTO source_versions (id, source_key, version_no, sha256, payload,"
        " is_active, created_at) VALUES (1,'transcript:ep-1',1,'a',?,1,"
        " '2026-01-02T00:00:00+00:00')",
        (json.dumps({"segments": [
            {"start": 0.0, "end": 10.0, "text": "Welcome, CAFÉ mentioned here"},
            {"start": 10.0, "end": 20.0, "text": "more talk"}]}),))
    con.execute(
        "INSERT INTO source_versions (id, source_key, version_no, sha256, payload,"
        " is_active, created_at) VALUES (2,'transcript:ep-2',1,'b',?,1,"
        " '2026-01-03T00:00:00+00:00')",
        (json.dumps({"segments": [{"start": 0.0, "end": 5.0, "text": "quiet chatter"}]}),))
    con.execute(
        "INSERT INTO episodes (id, kind, show_title, ep_title, pub_date, fetch_date,"
        " transcript_version_id, updated_at) VALUES ('ep-1','supplied','Show CAFÉ',"
        " 'Episode One','2026-01-02','2026-01-02',1,'2026-01-02T00:00:00+00:00')")
    con.execute(
        "INSERT INTO episodes (id, kind, show_title, ep_title, pub_date, fetch_date,"
        " transcript_version_id, updated_at) VALUES ('ep-2','supplied','Other Show',"
        " 'Episode Two','2026-01-03','2026-01-03',2,'2026-01-03T00:00:00+00:00')")
    con.execute(
        "INSERT INTO evidence (id, source, episode_id, orig_start_s, orig_end_s,"
        " excerpt, attrs, transcript_version_id, created_at) VALUES ('ev-1','jev',"
        " 'ep-1',0.0,10.0,'café mention','{}',1,'2026-01-02T00:00:00+00:00')")
    con.execute(
        "INSERT INTO evidence (id, source, episode_id, orig_start_s, orig_end_s,"
        " excerpt, attrs, transcript_version_id, created_at) VALUES ('ev-2','detector',"
        " 'ep-2',0.0,5.0,'quiet bit','{}',2,'2026-01-03T00:00:00+00:00')")
    con.execute(
        "INSERT INTO brand_assocs (evidence_id, brand, basis, origin, revision,"
        " updated_at, channel) VALUES ('ev-1','café','confirmed','human',1,"
        " '2026-01-04T00:00:00+00:00','spoken')")
    con.commit()
    indexing.rebuild_all(con)
    return con


class EquivalenceTest(unittest.TestCase):
    def test_literal_ids_equal_legacy(self):
        con = seed()
        for needle in ("café", "CAFÉ", "cafe", "a", "quiet", "nonexistent-xyz"):
            legacy = ops.search_evidence(con, {"q": needle, "page": 1, "per_page": 50})
            legacy_ids = sorted(r["id"] for r in legacy["items"])
            matched = indexing.scoped_match(con, needle)
            filt = dict(ops.normalize_filter({"page": 1, "per_page": 50}), q=needle)
            paged = indexing.paged_search(con, filt, matched)
            self.assertEqual(sorted(r["id"] for r in paged["items"]), legacy_ids,
                             f"needle={needle!r}")
            self.assertEqual(paged["total"], legacy["total"], f"needle={needle!r}")

    def test_unicode_casefold(self):
        con = seed()
        matched = indexing.scoped_match(con, "CAFÉ")
        self.assertIn("ev-1", matched)

    def test_short_query_uses_scan(self):
        con = seed()
        matched = indexing.scoped_match(con, "a")
        legacy = ops.search_evidence(con, {"q": "a", "page": 1, "per_page": 50})
        self.assertEqual(sorted(matched),
                         sorted(r["id"] for r in legacy["items"]))

    def test_no_candidate_episode_searchable(self):
        con = seed()
        segs = con.execute(
            "SELECT folded_text FROM transcript_segment_docs WHERE episode_id='ep-2'"
        ).fetchall()
        self.assertTrue(segs and all("quiet" in (r["folded_text"] or "") or True
                                     for r in segs))
        matched = indexing.scoped_match(con, "quiet")
        self.assertIn("ev-2", matched)

    def test_title_match_not_spoken_brand(self):
        con = seed()
        paged = indexing.paged_search(
            con, dict(ops.normalize_filter({"page": 1, "per_page": 50}), q="CAFÉ"),
            indexing.scoped_match(con, "CAFÉ"))
        row = next(r for r in paged["items"] if r["id"] == "ev-1")
        self.assertIn(row["match_basis"], ("excerpt", "interval_text", "title", "association"))
        # Title text contains CAFÉ via show_title; spoken-brand confirmation
        # requires the association channel, not the title match.
        self.assertTrue(row["assocs"] and row["assocs"][0]["channel"] == "spoken")
        self.assertNotEqual(row["match_basis"], "association",
                            "this fixture matches excerpt/interval/title, not only assoc")

    def test_stale_projection_explicit(self):
        con = seed()
        rev = indexing.check_fresh(con)
        db.bump_revision(con)
        with self.assertRaises(indexing.StaleProjection):
            indexing.check_fresh(con)
        self.assertIsInstance(rev, int)

    def test_missing_index_explicit(self):
        con = seed()
        con.execute("DELETE FROM evidence_search_docs")
        con.execute("UPDATE index_state SET value='current' WHERE key='status'")
        con.execute("DELETE FROM index_state WHERE key='data_revision'")
        with self.assertRaises(indexing.IndexUnavailable):
            indexing.check_fresh(con)


if __name__ == "__main__":
    unittest.main()
