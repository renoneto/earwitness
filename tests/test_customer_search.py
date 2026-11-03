"""Customer search/projection contract tests (Product-9 §4.1)."""
from __future__ import annotations

import json
import sqlite3
import unittest

from earwitness.app import citations, customer, db, ops, search
from earwitness.app import customer_contracts, customer_query


def fresh_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON;")
    db.init_schema(con)
    return con


def add_version(con, key, payload):
    cur = con.execute(
        "INSERT INTO source_versions (source_key, version_no, sha256, payload,"
        " is_active, created_at) VALUES (?, 1, ?, ?, 1, ?)",
        (key, "sha-" + key, json.dumps(payload), db.utcnow()))
    return cur.lastrowid


EPISODES_META = [
    {"ep_id": "e1", "show_title": "Same Title", "feed_url": "https://feed-a.example/rss",
     "ep_title": "Morning brew", "pub_date": "2026-03-10", "category": "business"},
    {"ep_id": "e2", "show_title": "Same Title", "feed_url": "https://feed-b.example/rss",
     "ep_title": "Evening blend", "pub_date": "2026-03-11", "category": "business"},
    {"ep_id": "e3", "show_title": "Same Title", "feed_url": "https://feed-a.example/rss",
     "ep_title": "Night roast", "pub_date": "2026-03-12", "category": "business"},
    {"ep_id": "e4", "show_title": "Undated Show", "feed_url": "https://feed-c.example/rss",
     "ep_title": "Lost tape", "pub_date": "", "category": "fiction"},
    {"ep_id": "e5", "show_title": "No Provenance", "feed_url": "",
     "ep_title": "Mystery", "pub_date": "2026-03-13", "category": "fiction"},
]

SEGMENTS = {
    "e1": [{"start": 0.0, "end": 10.0, "text": "Welcome to the show"},
           {"start": 10.0, "end": 20.0, "text": "This episode is brought to you by Lumen"},
           {"start": 20.0, "end": 30.0, "text": "Enjoy your café lumen blend"}],
    "e2": [{"start": 0.0, "end": 10.0, "text": "A different feed entirely"},
           {"start": 10.0, "end": 20.0, "text": "Lumen keeps the lights on here"}],
    "e3": [{"start": 0.0, "end": 10.0, "text": "Lumen lumen lumen"},
           {"start": 10.0, "end": 20.0, "text": "Ordinary weather talk"}],
    "e4": [{"start": 0.0, "end": 10.0, "text": "Undated lumen mention"}],
    "e5": [{"start": 0.0, "end": 10.0, "text": "Nothing relevant here"}],
}


def seed(con):
    ep_version = add_version(con, "episodes.json", EPISODES_META)
    for meta in EPISODES_META:
        ep_id = meta["ep_id"]
        tv = add_version(con, f"transcript:{ep_id}",
                         {"segments": SEGMENTS[ep_id]})
        con.execute(
            "INSERT INTO episodes (id, kind, show_title, ep_title, category, pub_date,"
            " fetch_date, transcript_version_id, revision, updated_at)"
            " VALUES (?, 'supplied', ?, ?, ?, ?, '2026-09-21', ?, 1, ?)",
            (ep_id, meta["show_title"], meta["ep_title"], meta["category"],
             meta["pub_date"], tv, db.utcnow()))
    from tests.backend_fixtures import publish_all_records
    publish_all_records(con)
    con.commit()
    return ep_version


def add_evidence(con, ev_id, episode_id, excerpt, judgment=None, brands=(),
                 source_version=1, start=10.0, end=20.0):
    con.execute(
        "INSERT INTO evidence (id, source, source_version_id, episode_id, orig_start_s,"
        " orig_end_s, excerpt, attrs, revision, created_at, transcript_version_id,"
        " source_record_key, assoc_revision, record_kind)"
        " VALUES (?, 'detector', ?, ?, ?, ?, ?, '{}', 1, ?, "
        " (SELECT transcript_version_id FROM episodes WHERE id=?), ?, 0, 'candidate')",
        (ev_id, source_version, episode_id, start, end, excerpt, db.utcnow(),
         episode_id, ev_id))
    if judgment is not None:
        con.execute("INSERT INTO reviews (evidence_id, judgment, revision, updated_at)"
                    " VALUES (?, ?, 1, ?)", (ev_id, judgment, db.utcnow()))
    for brand, basis in brands:
        con.execute("INSERT INTO brand_assocs (evidence_id, brand, basis, origin,"
                    " revision, updated_at) VALUES (?, ?, ?, 'test', 1, ?)",
                    (ev_id, brand, basis, db.utcnow()))
    db.record_evidence_snapshot(con, ev_id, "seed")
    from earwitness.app import citations as _citations
    _citations.publish_source_state(con, kind="evidence", record_id=ev_id,
                                    actor="fixture")
    _citations.publish_source_state(con, kind="episode", record_id=episode_id,
                                    actor="fixture")
    con.commit()


class ParseQueryTest(unittest.TestCase):
    def test_empty_selection(self):
        con = fresh_db()
        try:
            seed(con)
            empty = customer_contracts.upcast_query_v1({"mode": "ads", "ids": "__none__"})[0]
            self.assertEqual(customer.query(con, empty)["total"], 0)
        finally:
            con.close()

    def test_explicit_empty_ids_stays_exact_empty(self):
        # Absent-vs-empty contract: an explicit empty id array is an
        # exact-empty selection even when records match; only an absent
        # key is unrestricted.
        con = fresh_db()
        try:
            seed(con)
            open_criteria = customer_contracts.upcast_query_v1({"mode": "mentions", "q": "lumen"})[0]
            self.assertGreater(customer.query(con, open_criteria)["total"], 0)
            empty = customer_contracts.upcast_query_v1({"mode": "mentions", "q": "lumen", "ids": []})[0]
            result = customer.query(con, empty)
            self.assertEqual(result["total"], 0)
            self.assertEqual(result["items"], [])
            self.assertEqual(result["total_relation"], "exact")
        finally:
            con.close()

    def test_invalid_date_rejected(self):
        with self.assertRaises(search.FilterRejected):
            customer_contracts.upcast_query_v1({"mode": "episodes", "from": "2026-02-30"})[0]

    def test_unknown_field_rejected(self):
        with self.assertRaises(search.FilterRejected):
            customer_contracts.upcast_query_v1({"mode": "mentions", "unsupported": "value"})[0]

    def test_unknown_mode_rejected(self):
        with self.assertRaises(search.FilterRejected):
            customer_contracts.upcast_query_v1({"mode": "nope"})[0]


class ShowMappingTest(unittest.TestCase):
    def test_same_title_different_feeds_stay_separate(self):
        con = fresh_db()
        try:
            seed(con)
            customer.ensure_customer_show_map(con)
            s1 = customer.customer_show_for_episode(con, "e1")
            s2 = customer.customer_show_for_episode(con, "e2")
            s3 = customer.customer_show_for_episode(con, "e3")
            self.assertNotEqual(s1["show_identity_id"], s2["show_identity_id"])
            self.assertEqual(s1["show_identity_id"], s3["show_identity_id"])
            self.assertEqual(s1["basis"], "feed_url")
        finally:
            con.close()

    def test_unresolved_provenance_labeled(self):
        con = fresh_db()
        try:
            seed(con)
            customer.ensure_customer_show_map(con)
            s5 = customer.customer_show_for_episode(con, "e5")
            self.assertEqual(s5["basis"], "unresolved")
            self.assertEqual(s5["unresolved"], 1)
        finally:
            con.close()

    def test_stable_across_title_edit(self):
        con = fresh_db()
        try:
            seed(con)
            customer.ensure_customer_show_map(con)
            before = customer.customer_show_for_episode(con, "e1")["show_identity_id"]
            con.execute("UPDATE episodes SET show_title='Same Title!!' WHERE id='e1'")
            con.commit()
            customer.ensure_customer_show_map(con)
            after = customer.customer_show_for_episode(con, "e1")["show_identity_id"]
            self.assertEqual(before, after)
        finally:
            con.close()

    def test_agent_ids_unchanged(self):
        con = fresh_db()
        try:
            seed(con)
            before = search.show_identity({"kind": "supplied", "show_title": "Same Title"})
            customer.ensure_customer_show_map(con)
            after = search.show_identity({"kind": "supplied", "show_title": "Same Title"})
            self.assertEqual(before, after)
        finally:
            con.close()


class MatchingTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        seed(self.con)
        add_evidence(self.con, "ev-1", "e1", "Lumen ad read here", "ad",
                     [("lumen", "confirmed")])
        for i in range(11):
            add_evidence(self.con, f"ev-bulk-{i}", "e3", f"Lumen bulk mention {i}")

    def tearDown(self):
        self.con.close()

    def test_and_before_pagination(self):
        # D-1: Ads pages by ad break; distinct spans make 11 breaks, so
        # the eleventh still reaches page 2.
        for i in range(11):
            add_evidence(self.con, f"ev-spread-{i}", "e3", f"Lumen spread mention {i}",
                         start=100.0 + 30 * i, end=110.0 + 30 * i)
        criteria = customer_contracts.upcast_query_v1({"mode": "ads", "q": "lumen spread"})[0]
        result = customer.query(self.con, criteria)
        self.assertEqual(result["total"], 11)
        self.assertEqual(result["break_total"], 11)
        self.assertEqual(len(result["items"]), 10)
        criteria2 = dict(customer_contracts.upcast_query_v1({"mode": "ads", "q": "lumen spread"})[0], page=2)
        page2 = customer.query(self.con, criteria2)
        self.assertEqual(len(page2["items"]), 1)

    def test_one_break_pages_as_one_unit(self):
        # D-1: the 11 bulk reads share one span in one episode, so they
        # are one ad break: page 1 holds all of them, page 2 none.
        criteria = customer_contracts.upcast_query_v1({"mode": "ads", "q": "lumen bulk"})[0]
        result = customer.query(self.con, criteria)
        self.assertEqual(result["total"], 11)
        self.assertEqual(result["break_total"], 1)
        self.assertEqual(len(result["items"]), 11)
        criteria2 = dict(customer_contracts.upcast_query_v1({"mode": "ads", "q": "lumen bulk"})[0], page=2)
        page2 = customer.query(self.con, criteria2)
        self.assertEqual(len(page2["items"]), 0)

    def test_phrase_and_accent(self):
        criteria = customer_contracts.upcast_query_v1({"mode": "mentions", "q": '"brought to you by" café'})[0]
        result = customer.query(self.con, criteria)
        # AND semantics: one segment must carry both the phrase and café.
        self.assertEqual(result["total"], 0)
        criteria = customer_contracts.upcast_query_v1({"mode": "mentions", "q": '"brought to you by"'})[0]
        result = customer.query(self.con, criteria)
        self.assertTrue(result["total"] >= 1)
        criteria = customer_contracts.upcast_query_v1({"mode": "mentions", "q": "cafe"})[0]
        result = customer.query(self.con, criteria)
        self.assertTrue(result["total"] >= 1)

    def test_match_basis_labels(self):
        criteria = customer_contracts.upcast_query_v1({"mode": "ads", "q": "Morning"})[0]
        result = customer.query(self.con, criteria)
        self.assertTrue(result["total"] >= 1)
        self.assertTrue(all(i["match_basis"] == "title" for i in result["items"]))

    def test_status_filters(self):
        confirmed = customer.query(
            self.con, customer_contracts.upcast_query_v1({"mode": "ads", "status": "confirmed"})[0])
        self.assertEqual(confirmed["total"], 1)
        finding = customer.query(
            self.con, customer_contracts.upcast_query_v1({"mode": "ads", "status": "finding"})[0])
        self.assertEqual(finding["total"], 11)

    def test_episodes_distinct_with_counts(self):
        criteria = customer_contracts.upcast_query_v1({"mode": "episodes", "q": "lumen"})[0]
        result = customer.query(self.con, criteria)
        ep_ids = [i["episode_id"] for i in result["items"]]
        self.assertEqual(len(ep_ids), len(set(ep_ids)))
        for item in result["items"]:
            self.assertIn("matching_passages", item)
            self.assertIn(item["match_basis"], ("title", "passage", "mixed"))

    def test_date_scope_excludes_undated(self):
        criteria = customer_contracts.upcast_query_v1({"mode": "mentions", "q": "lumen", "from": "2026-03-01", "to": "2026-03-31"})[0]
        result = customer.query(self.con, criteria)
        self.assertTrue(result["coverage"]["excluded_undated"])
        self.assertTrue(all((i.get("pub_date") or "") != "" for i in result["items"]))

    def test_exact_ids_and_intersection(self):
        all_criteria = customer_contracts.upcast_query_v1({"mode": "ads", "q": "lumen bulk"})[0]
        full = customer.query(self.con, all_criteria)
        self.assertEqual(full["total"], 11)
        one_ref = full["items"][0]["ref"]
        picked = customer.query(
            self.con, customer_contracts.upcast_query_v1({"mode": "ads", "ids": [one_ref]})[0])
        self.assertEqual(picked["total"], 1)
        # Unknown IDs never widen the set.
        narrow = customer.query(
            self.con, customer_contracts.upcast_query_v1({"mode": "ads", "q": "lumen bulk", "ids": [one_ref, "ev:ghost@v9"]})[0])
        self.assertEqual(narrow["total"], 1)

    def test_summary_and_export_agree(self):
        criteria = customer_contracts.upcast_query_v1({"mode": "ads", "q": "lumen bulk"})[0]
        summ = customer.summary(self.con, criteria, "status")
        self.assertEqual(sum(s["count"] for s in summ["cells"]), 11)
        export = customer_query.capture_export(self.con, criteria, None, "all_matches", "csv",
                                               customer_query._view_revision(self.con))
        row = self.con.execute("SELECT payload FROM exports WHERE id=?",
                               (export["export_id"],)).fetchone()
        lines = row["payload"].strip().splitlines()
        self.assertEqual(len(lines), 12)  # header + all matching pages

    def test_record_resolution(self):
        full = customer.query(
            self.con, customer_contracts.upcast_query_v1({"mode": "ads", "q": "Lumen ad read"})[0])
        ref = full["items"][0]["ref"]
        detail = customer.record(self.con, ref)
        self.assertEqual(detail["status"], "confirmed")
        # A pin addressing an unknown publication is unavailable/repair,
        # never a silent substitution: flip one state-id character.
        tampered = "sr1:" + ("0" if ref[4] != "0" else "1") + ref[5:]
        with self.assertRaises(Exception):
            customer.record(self.con, tampered)

    def test_query_never_commits_caller_transaction(self):
        con = fresh_db()
        try:
            seed(con)
            con.execute("BEGIN IMMEDIATE")
            con.execute("UPDATE episodes SET ep_title='UNCOMMITTED' WHERE id='e1'")
            customer.query(con, customer_contracts.upcast_query_v1({"mode": "episodes"})[0])
            self.assertTrue(con.in_transaction)
            con.rollback()
            title = con.execute("SELECT ep_title FROM episodes WHERE id='e1'").fetchone()[0]
            self.assertEqual(title, "Morning brew")
        finally:
            con.close()

    def test_forged_transcript_reference_rejected(self):
        con = fresh_db()
        try:
            seed(con)
            e2_version = con.execute(
                "SELECT transcript_version_id FROM episodes WHERE id='e2'").fetchone()[0]
            with self.assertRaises(Exception):
                customer.record(con, f"tx:e1@v{e2_version}:0")
            # Unrelated versions leave no side effects.
            self.assertEqual(con.execute("SELECT COUNT(*) FROM customer_mistake_reports").fetchone()[0], 0)
        finally:
            con.close()

    def test_historical_transcript_reference_resolves(self):
        con = fresh_db()
        try:
            seed(con)
            old_version = con.execute(
                "SELECT transcript_version_id FROM episodes WHERE id='e1'").fetchone()[0]
            cur = con.execute(
                "INSERT INTO source_versions (source_key, version_no, sha256, payload,"
                " is_active, created_at) VALUES ('transcript:e1', 2, 'sh2', ?, 1, ?)",
                (json.dumps({"segments": [{"start": 0.0, "end": 5.0,
                                            "text": "Revised morning brew"}]}), db.utcnow()))
            con.execute("UPDATE episodes SET transcript_version_id=?, revision=revision+1"
                        " WHERE id='e1'", (cur.lastrowid,))
            con.commit()
            detail = customer.record(con, f"tx:e1@v{old_version}:2")
            # Record replies name the episode by episode_id (service contract).
            self.assertEqual(detail["episode"]["episode_id"], "e1")
            self.assertIn("lumen", detail["text"].lower())
        finally:
            con.close()

    def test_rejected_evidence_excluded_from_ads(self):
        con = fresh_db()
        try:
            seed(con)
            add_evidence(con, "ev-neg", "e1", "not an advertisement", "not_ad",
                         [("lumen", "confirmed")])
            result = customer.query(con, customer_contracts.upcast_query_v1({"mode": "ads"})[0])
            refs = [i["ref"] for i in result["items"]]
            self.assertTrue(all("ev-neg" not in r for r in refs))
        finally:
            con.close()

    def test_title_only_episode_counts(self):
        con = fresh_db()
        try:
            seed(con)
            result = customer.query(
                con, customer_contracts.upcast_query_v1({"mode": "episodes", "q": "Evening blend"})[0])
            self.assertEqual(result["total"], 1)
            item = result["items"][0]
            self.assertEqual(item["episode_id"], "e2")
            self.assertEqual(item["match_basis"], "title")
            self.assertEqual(item["matching_passages"], 0)
            self.assertEqual(item["indexed_passages"], 2)
            # Title metadata without transcript/evidence stays discoverable.
            con.execute("INSERT INTO episodes (id, kind, show_title, ep_title, category,"
                        " pub_date, fetch_date, revision, updated_at)"
                        " VALUES ('ghost', 'supplied', 'Ghost Show', 'Evening blend tales',"
                        " 'fiction', '2026-03-14', '2026-09-21', 1, ?)", (db.utcnow(),))
            from earwitness.app import citations as _citations
            _citations.publish_source_state(con, kind="episode", record_id="ghost",
                                            actor="fixture")
            con.commit()
            result = customer.query(
                con, customer_contracts.upcast_query_v1({"mode": "episodes", "q": "Evening blend"})[0])
            ep_ids = {i["episode_id"] for i in result["items"]}
            self.assertIn("ghost", ep_ids)
            ghost = next(i for i in result["items"] if i["episode_id"] == "ghost")
            self.assertEqual(ghost["matching_passages"], 0)
        finally:
            con.close()

    def test_episodes_brand_predicates(self):
        con = fresh_db()
        try:
            seed(con)
            add_evidence(con, "ev-brand", "e1", "Lumen ad read", "ad",
                         [("lumen", "confirmed")])
            add_evidence(con, "ev-other", "e3", "Lumenx bulk", None,
                         [("lumenx", "suggested")])
            positive = customer.query(
                con, customer_contracts.upcast_query_v1({"mode": "episodes", "brand": "lumen"})[0])
            self.assertEqual({i["episode_id"] for i in positive["items"]}, {"e1"})
            negative = customer.query(
                con, customer_contracts.upcast_query_v1({"mode": "episodes", "brand": "lumenx"})[0])
            self.assertEqual({i["episode_id"] for i in negative["items"]}, {"e3"})
            # Exact keys: 'lumen' never substring-expands into 'lumenx'.
            for item in positive["items"]:
                self.assertNotEqual(item["episode_id"], "e3")
        finally:
            con.close()


class SummaryOrderTest(unittest.TestCase):
    def test_week_cells_read_chronologically_unknown_last(self):
        con = fresh_db()
        try:
            seed(con)
            # A later week with MORE matches still sorts after March:
            # chronology wins over count for week cells.
            tv = add_version(
                con, "transcript:e9",
                {"segments": [{"start": 0.0, "end": 10.0,
                               "text": "lumen one"},
                              {"start": 10.0, "end": 20.0,
                               "text": "lumen two"},
                              {"start": 20.0, "end": 30.0,
                               "text": "lumen three"},
                              {"start": 30.0, "end": 40.0,
                               "text": "lumen four"},
                              {"start": 40.0, "end": 50.0,
                               "text": "lumen five"}]})
            con.execute(
                "INSERT INTO episodes (id, kind, show_title, ep_title, category,"
                " pub_date, fetch_date, transcript_version_id, revision,"
                " updated_at) VALUES ('e9', 'supplied', 'Late Show', 'April',"
                " 'business', '2026-04-07', '2026-09-21', ?, 1, ?)",
                (tv, db.utcnow()))
            from earwitness.app import citations as _citations
            _citations.publish_source_state(con, kind="episode", record_id="e9",
                                            actor="fixture")
            con.commit()
            out = customer.summary(
                con, customer_contracts.upcast_query_v1({"mode": "mentions", "q": "lumen"})[0],
                "week")
            keys = [c["key"] for c in out["cells"]]
            self.assertEqual(keys, ["2026-03-09", "2026-04-06", "unknown"])
            april = next(c for c in out["cells"] if c["key"] == "2026-04-06")
            # Summary cells carry their actual date interval as
            # interval{from,to} (service contract, research summary cells).
            self.assertEqual(april["interval"],
                             {"from": "2026-04-06", "to": "2026-04-12"})
        finally:
            con.close()


class CompareTest(unittest.TestCase):
    """Compare counts every ad of the brand the caller can see (PLAIN-ADS,
    DEP7): rejected evidence never counts, an unsure (unresolved) ad does,
    and two pieces of evidence of one ad count once. Each cell's selector
    renders exactly the counted refs."""

    def setUp(self):
        self.con = fresh_db()
        seed(self.con)

    def tearDown(self):
        self.con.close()

    def _confirm(self, ev_id, episode_id, brand="acme", judgment="ad",
                 basis="confirmed", obs_id=None):
        add_evidence(self.con, ev_id, episode_id, f"{brand} read",
                     judgment, [(brand, basis)])
        if obs_id is not None:
            self._ensure_observation(obs_id, ev_id)
            # Link the evidence through the producer idiom: attrs change,
            # revision bump, history snapshot, republication.
            self.con.execute("UPDATE evidence SET attrs=?, revision=revision+1"
                             " WHERE id=?",
                             (json.dumps({"observation_id": obs_id}), ev_id))
            db.record_evidence_snapshot(self.con, ev_id, "fixture")
            citations.publish_source_state(self.con, kind="evidence",
                                           record_id=ev_id, actor="fixture")
            self.con.commit()

    def _ensure_observation(self, obs_id, ev_id):
        """A reviewed observation with retained history, pinned to the same
        source/transcript versions as the evidence that cites it (the rows
        observations.propose_observation writes). Publication resolves the
        observation through observation_revisions."""
        if self.con.execute("SELECT 1 FROM ad_observations WHERE id=?",
                            (obs_id,)).fetchone():
            return
        ev = self.con.execute(
            "SELECT episode_id, source_version_id, transcript_version_id,"
            " orig_start_s, orig_end_s FROM evidence WHERE id=?", (ev_id,)).fetchone()
        now = db.utcnow()
        self.con.execute(
            "INSERT INTO ad_observations (id, workspace_id, episode_id,"
            " source_version_id, status, start_s, end_s,"
            " revision, created_at, updated_at)"
            " VALUES (?, 'local', ?, ?, 'reviewed_ad', ?, ?, 1, ?, ?)",
            (obs_id, ev["episode_id"], ev["source_version_id"],
             ev["orig_start_s"], ev["orig_end_s"], now, now))
        self.con.execute(
            "INSERT INTO observation_revisions (workspace_id, observation_id,"
            " revision, values_json, source_pins, actor, created_at)"
            " VALUES ('local', ?, 1, ?, ?, 'fixture', ?)",
            (obs_id,
             json.dumps({"status": "reviewed_ad", "fields": {},
                         "start_s": ev["orig_start_s"], "end_s": ev["orig_end_s"]}),
             json.dumps({"source_version_id": ev["source_version_id"],
                         "transcript_version_id": ev["transcript_version_id"]}),
             now))

    def _link_occurrence(self, occ_id, episode_id, obs_id,
                         correspondence="confirmed"):
        self.con.execute(
            "INSERT INTO occurrences (id, workspace_id, episode_id,"
            " correspondence, revision, created_at, updated_at)"
            " VALUES (?, 'local', ?, ?, 1, ?, ?)",
            (occ_id, episode_id, correspondence, db.utcnow(), db.utcnow()))
        self.con.execute(
            "INSERT INTO occurrence_observations (occurrence_id,"
            " observation_id, basis, reviewer) VALUES (?, ?, 'test', '')",
            (occ_id, obs_id))
        self.con.commit()

    CONTEXT = "ctx-compare-test"

    def _cell(self, brand="acme", period=None):
        # Both transports call compare_bundle with the request's workspace
        # context (app_server /customer/compare, asgi customer_compare2).
        # Counted ResearchSelector1 cells are bound to that context, so
        # only selectors from this path can be reopened.
        period = period or {"from": "2026-03-09", "to": "2026-03-15"}
        with db.transaction(self.con, write=False):
            out = customer_query.compare_bundle(
                self.con, [brand], period, None, None, None, self.CONTEXT)
        self.assertEqual(len(out["cells"]), 1)
        self.assertEqual(out["workspace_context"], self.CONTEXT)
        return out["cells"][0]["a"], out

    def _reopen(self, selector):
        """Render a counted cell selector the way POST /research-query does."""
        with db.transaction(self.con, write=False):
            out = customer_query.research_bundle(
                self.con, selector["base_query"], selector, [], False,
                self.CONTEXT)
        self.assertEqual(out["selector"]["scope_mode"], "counted_cohort")
        return out["result"]["items"]

    def test_zero_fixture_renders_zero(self):
        cell, out = self._cell()
        self.assertEqual(cell["count"], 0)
        self.assertEqual(cell["refs"], [])
        self.assertEqual(out["shared_scale"], 0)
        # Zero stays zero and is not reported as absence: the cell carries
        # only matched_in_scope (service-contract comparison wire), which
        # the UI renders as "absence not established" (views.js).
        self.assertEqual(cell["coverage"], {"matched_in_scope": 0})
        self.assertEqual(cell["count_units"], "distinct_results")
        self.assertEqual(cell["ads"], 0)

    def test_positive_counts_once(self):
        self._confirm("ev-p1", "e1")
        cell, _ = self._cell()
        self.assertEqual(cell["count"], 1)
        self.assertEqual(len(cell["refs"]), 1)
        # The cell selector renders exactly the counted refs.
        shown = self._reopen(cell["selector"])
        self.assertEqual({i["ref"] for i in shown}, set(cell["refs"]))

    def test_rejected_excluded_unresolved_counted(self):
        self._confirm("ev-r1", "e1", judgment="not_ad")
        self._confirm("ev-u1", "e2", judgment="unsure")
        cell, _ = self._cell()
        self.assertEqual(cell["ads"], 1)
        self.assertEqual(len(cell["refs"]), 1)
        shown = self._reopen(cell["selector"])
        self.assertEqual({i["evidence_id"] for i in shown}, {"ev-u1"})

    def test_duplicate_refs_count_once(self):
        self._confirm("ev-d1", "e1", obs_id="obs-dup")
        self._confirm("ev-d2", "e1", obs_id="obs-dup")
        self._link_occurrence("occ-dup", "e1", "obs-dup")
        cell, _ = self._cell()
        self.assertEqual(cell["ads"], 1)
        self.assertEqual(len(cell["refs"]), 2)
        shown = self._reopen(cell["selector"])
        self.assertEqual({i["ref"] for i in shown}, set(cell["refs"]))

    def test_unlinked_confirmed_counts_as_own_identity(self):
        self._confirm("ev-solo", "e1")
        cell, _ = self._cell()
        self.assertEqual(cell["count"], 1)

    def test_empty_period_is_unknown_not_absence(self):
        cell, _ = self._cell(
            period={"from": "2026-05-01", "to": "2026-05-07"})
        self.assertEqual(cell["count"], 0)
        self.assertEqual(cell["refs"], [])
        self.assertEqual(cell["coverage"], {"matched_in_scope": 0})

    def test_shared_scale_covers_both_periods(self):
        self._confirm("ev-p1", "e1")
        out = customer.compare(
            self.con, ["acme"],
            {"from": "2026-03-09", "to": "2026-03-15"},
            {"from": "2026-03-16", "to": "2026-03-22"})
        counts = [out["cells"][0]["a"]["count"],
                  out["cells"][0]["b"]["count"]]
        self.assertEqual(counts, [1, 0])
        self.assertEqual(out["shared_scale"], 1)


class ShowDetailTest(unittest.TestCase):
    def test_title_identity_and_coverage_not_raw_id(self):
        con = fresh_db()
        try:
            seed(con)
            customer.ensure_customer_show_map(con)
            show_id = customer.customer_show_for_episode(
                con, "e1")["show_identity_id"]
            detail = customer.show_detail(con, show_id)
            self.assertEqual(detail["display_title"], "Same Title")
            self.assertEqual(detail["show_id"], show_id)
            self.assertEqual({e["id"] for e in detail["episodes"]},
                             {"e1", "e3"})
            self.assertEqual(detail["coverage"]["episodes"], 2)
            self.assertEqual(
                detail["coverage"]["episodes_with_transcript"], 2)
        finally:
            con.close()

    def test_unknown_show_rejected(self):
        con = fresh_db()
        try:
            seed(con)
            # A malformed locator is invalid input; a well-formed show id
            # that does not exist is not found (customer.show_detail).
            with self.assertRaises(search.FilterRejected):
                customer.show_detail(con, "not-a-show-locator")
            with self.assertRaisesRegex(ops.NotFound, "show unavailable"):
                customer.show_detail(con, "cs-does-not-exist")
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
