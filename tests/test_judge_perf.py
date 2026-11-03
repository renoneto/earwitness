"""DEMO2 judge rehearsal fixes (PERF brief).

Overview: with several campaigns, Overview never builds a full campaign
draft (about 13 s each on the 1,500-episode sample); it reads what it
needs and keeps the same placement gaps.
Mention search: a query word matches whole words only, so "Meta" no
longer matches "metal" while "Meta's" and "Meta" still match; phrases
still match. Both the indexed and the direct-scan paths.
"""
from __future__ import annotations

import json
import sqlite3
import unittest
from unittest import mock

from earwitness.app import campaigns, customer, customer_contracts, db
from earwitness.app import transcript_index
from tests.test_customer_campaigns import fresh_db, seed_campaign, seed_episode
from tests.test_uat_cui import _show_of, _window


class OverviewBuildsNoDraftTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con, show="Casefile")
        show = _show_of(self.con)
        self.placed = []
        for _ in range(3):
            cid = seed_campaign(self.con)
            _window(self.con, cid, show)
            self.placed.append(cid)
        for name in ("Research one", "Research two"):
            created = campaigns.save_customer_campaign(self.con, {"campaign": {
                "name": name, "mode": "research", "status": "active", "brand": "acme",
                "source_query": None, "message": None, "offer": None, "notes": "",
                "requirement_provenance": None}})
            campaigns.create_target(self.con, created["campaign"]["id"], "episode",
                                    episode_id="ep-1")

    def tearDown(self):
        self.con.close()

    def test_overview_builds_no_campaign_draft(self):
        real = campaigns.build_customer_draft
        with mock.patch.object(campaigns, "build_customer_draft",
                               side_effect=real) as built:
            out = customer.overview(self.con)
        self.assertEqual(len(out["tracked_campaigns"]), 5)
        self.assertEqual(built.call_count, 0,
                         f"Overview built {built.call_count} campaign drafts")
        # The same gaps the campaign page's draft gives: one per placement.
        self.assertEqual(sorted(g["campaign_id"] for g in out["campaign_gaps"]),
                         sorted(self.placed))
        for gap in out["campaign_gaps"]:
            draft = real(self.con, gap["campaign_id"])
            outcome = draft["allocation"]["evaluations"][gap["placement_id"]]["outcome"]
            cov = draft["coverage_by_placement"][gap["placement_id"]]
            self.assertEqual(gap["outcome"], outcome)
            self.assertEqual((gap["linkage"], gap["label"]),
                             customer.placement_linkage_label(cov.get("state"), outcome, cov))



class ResearchCampaignStateTest(unittest.TestCase):
    """DEMO2-R3-01: a research campaign with a target or an observation
    reads "Researching", not "Draft"; one with neither stays "Draft".
    Overview still evaluates placements of "Tracking" campaigns only."""

    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con, show="Casefile")
        self.placed = seed_campaign(self.con)
        _window(self.con, self.placed, _show_of(self.con))
        self.supplied_empty = seed_campaign(self.con)
        self.research_empty = self._research("No targets yet")
        self.research_target = self._research("Has a target")
        campaigns.create_target(self.con, self.research_target, "episode",
                                episode_id="ep-1")
        self.research_obs = self._research("Has an observation")
        target = campaigns.create_target(self.con, self.research_obs, "episode",
                                         episode_id="ep-1")
        self.con.execute("INSERT INTO target_evidence (workspace_id, target_id, ref,"
                         " attached_at) VALUES ('local', ?, 'sr1:x', ?)",
                         (target["id"], db.utcnow()))
        self.con.commit()

    def tearDown(self):
        self.con.close()

    def _research(self, name):
        return campaigns.save_customer_campaign(self.con, {"campaign": {
            "name": name, "mode": "research", "status": "active", "brand": "acme",
            "source_query": None, "message": None, "offer": None, "notes": "",
            "requirement_provenance": None}})["campaign"]["id"]

    def expected(self):
        return {self.placed: "Tracking", self.supplied_empty: "Draft",
                self.research_empty: "Draft", self.research_target: "Researching",
                self.research_obs: "Researching"}

    def test_state_function(self):
        got = {cid: customer.campaign_customer_state(self.con, cid) for cid in self.expected()}
        self.assertEqual(got, self.expected())

    def test_campaigns_list_and_overview_cards(self):
        rows = campaigns.customer_list_summaries(self.con, [
            dict(r) for r in self.con.execute("SELECT * FROM campaigns ORDER BY id")])
        self.assertEqual({r["id"]: r["state"] for r in rows}, self.expected())
        cards = customer.overview(self.con)["tracked_campaigns"]
        self.assertEqual({c["id"]: c["state"] for c in cards}, self.expected())

    def test_archived_research_reads_archived(self):
        rev = self.con.execute("SELECT revision FROM campaigns WHERE id=?",
                               (self.research_target,)).fetchone()["revision"]
        campaigns.save_customer_campaign(self.con, {"campaign": {"status": "archived"}},
                                         self.research_target, rev)
        self.assertEqual(customer.campaign_customer_state(self.con, self.research_target),
                         "Archived")

    def test_overview_evaluates_tracking_campaigns_only(self):
        real = campaigns.placement_outcomes
        with mock.patch.object(campaigns, "placement_outcomes",
                               side_effect=real) as evaluated:
            out = customer.overview(self.con)
        self.assertEqual([c.args[1] for c in evaluated.call_args_list], [self.placed])
        self.assertEqual({g["campaign_id"] for g in out["campaign_gaps"]}, {self.placed})


SEGMENTS = [
    {"start": 0.0, "end": 10.0, "text": "Heavy metal and a metaphor about metallurgy"},
    {"start": 10.0, "end": 20.0, "text": "Meta's Muse launched this week"},
    {"start": 20.0, "end": 30.0, "text": "Then META announced a new model"},
    {"start": 30.0, "end": 40.0, "text": "A museum trip, pure amusement"},
    {"start": 40.0, "end": 50.0, "text": "Things that amuse us"},
]


def _seed_transcript(con):
    con.execute(
        "INSERT INTO source_versions (source_key, version_no, sha256, payload,"
        " is_active, created_at) VALUES ('episodes.json', 1, 'e', ?, 1, ?)",
        (json.dumps([{"ep_id": "w1", "show_title": "Words",
                      "feed_url": "https://words.example/rss"}]), db.utcnow()))
    tv = con.execute(
        "INSERT INTO source_versions (source_key, version_no, sha256, payload,"
        " is_active, created_at) VALUES ('transcript:w1', 1, 't', ?, 1, ?)",
        (json.dumps({"segments": SEGMENTS}), db.utcnow())).lastrowid
    con.execute(
        "INSERT INTO episodes (id, kind, show_title, ep_title, category, pub_date,"
        " fetch_date, transcript_version_id, revision, updated_at)"
        " VALUES ('w1', 'supplied', 'Words', 'Ep', 'tech', '2026-03-10',"
        " '2026-09-21', ?, 1, ?)", (tv, db.utcnow()))
    from tests.backend_fixtures import publish_all_records
    publish_all_records(con)
    con.commit()
    return tv


class WholeWordMentionSearchTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        db.init_schema(self.con)
        self.tv = _seed_transcript(self.con)

    def tearDown(self):
        self.con.close()

    def texts(self, q):
        criteria = customer_contracts.upcast_query_v1({"mode": "mentions", "q": q})[0]
        result = customer.query(self.con, criteria)
        return sorted(i.get("text") or "" for i in result["items"])

    def check(self):
        meta = self.texts("Meta")
        self.assertFalse([t for t in meta if "metal" in t.lower()], meta)
        self.assertEqual(meta, ["Meta's Muse launched this week",
                                "Then META announced a new model"])
        self.assertEqual(self.texts("Muse"), ["Meta's Muse launched this week"])
        self.assertEqual(self.texts("Meta's"), ["Meta's Muse launched this week"])
        self.assertEqual(self.texts('"Meta\'s Muse"'), ["Meta's Muse launched this week"])
        self.assertEqual(self.texts("metal"),
                         ["Heavy metal and a metaphor about metallurgy"])

    def test_direct_scan_matches_whole_words(self):
        self.assertEqual(transcript_index.indexed(self.con, [self.tv]), {})
        self.check()

    def test_index_matches_whole_words(self):
        transcript_index.index_versions(self.con, [self.tv])
        self.con.commit()
        self.assertEqual(transcript_index.indexed(self.con, [self.tv]), {self.tv: 5})
        self.check()


class WholeWordExcerptTest(unittest.TestCase):
    def test_excerpt_opens_at_whole_word_match(self):
        full = "Heavy metal " + "x " * 100 + "then Meta announced a model " + "y " * 400
        out = customer._match_excerpt(full, "Meta")
        self.assertIsNotNone(out, "the excerpt opened at 'metal', not at 'Meta'")
        at = full.index("Meta announced")
        self.assertTrue(at - 70 <= out["start"] <= at, (at, out["start"]))
        self.assertIn("Meta announced", out["text"])


class AlertListScansNothingTest(unittest.TestCase):
    """Sol DEMO2 r1 finding 06: the Alerts list runs no per-alert scan of
    stored records; the alert page still counts current matches."""

    def setUp(self):
        from tests.test_backend_trackers import fresh_db as tdb, seed_corpus, add_evidence, query_acme
        self.con = tdb()
        seed_corpus(self.con)
        add_evidence(self.con, "ev-a", "s1", "acme launch")
        self.ids = [customer.save_alert(self.con, f"watch {n}", query_acme())["id"]
                    for n in range(3)]

    def tearDown(self):
        self.con.close()

    def test_list_runs_no_snapshot(self):
        real = customer._alert_snapshot
        with mock.patch.object(customer, "_alert_snapshot", side_effect=real) as snap:
            rows = customer.list_alerts(self.con)
        self.assertEqual(snap.call_count, 0, f"the list ran {snap.call_count} scans")
        self.assertEqual([r["id"] for r in rows], self.ids)
        for row in rows:
            self.assertNotIn("current_matches", row)
        with mock.patch.object(customer, "_alert_snapshot", side_effect=real) as snap:
            one = customer.get_alert(self.con, self.ids[0])
        self.assertEqual(snap.call_count, 1)
        self.assertEqual(one["current_matches"]["count"], 1)


class ServerFoldsAccentFormsTest(unittest.TestCase):
    """PERF-R1-05: the server folds text (NFKD, marks dropped) before word
    matching, so composed and decomposed forms match alike and an accent
    never splits a word."""

    def test_nfc_and_nfd_match_alike(self):
        for text in ("The naïve plan", "The naïve plan"):
            folded = customer.normalize_token(text)
            self.assertTrue(customer.word_in(customer.normalize_token("naive"), folded), text)
            self.assertFalse(customer.word_in(customer.normalize_token("nai"), folded), text)


class OverviewRestrictedViewerTest(unittest.TestCase):
    """PERF-R1-04: a viewer without transcript access cannot build the
    campaign draft (AuthorizationError). Overview now reports such a
    placement as outcome "ambiguous" (linkage and label "insufficient");
    before PERF it listed "unknown" with the same label. Never supported,
    never "no match"."""

    def setUp(self):
        from earwitness.app import access
        self.con = fresh_db()
        seed_episode(self.con, show="Casefile")
        self.cid = seed_campaign(self.con)
        self.pid = _window(self.con, self.cid, _show_of(self.con))
        keys = {r[0] for r in self.con.execute("SELECT DISTINCT source_key FROM source_versions")}
        keys |= {f"campaign:{r[0]}" for r in self.con.execute("SELECT id FROM campaigns")}
        keys |= {f"brief:{r[0]}" for r in self.con.execute("SELECT brief_id FROM provided_briefs")}
        self.ctx = access.AccessContext.create(
            principal_id="member", principal_kind="viewer", workspace_id="local",
            credential_kind="reader_bearer", scopes={"research:read", "campaigns:read"},
            content_policy={"sources": sorted(keys)})

    def tearDown(self):
        self.con.close()

    def test_viewer_without_transcripts_sees_insufficient(self):
        from earwitness.app.access import AuthorizationError
        with self.assertRaises(AuthorizationError):
            campaigns.build_customer_draft(self.con, self.cid, ctx=self.ctx)
        gaps = [g for g in customer.overview(self.con, self.ctx)["campaign_gaps"]
                if g["placement_id"] == self.pid]
        self.assertEqual(len(gaps), 1)
        self.assertEqual((gaps[0]["outcome"], gaps[0]["linkage"], gaps[0]["label"]),
                         ("ambiguous", "insufficient", customer.LINKAGE_INSUFFICIENT))


if __name__ == "__main__":
    unittest.main()
