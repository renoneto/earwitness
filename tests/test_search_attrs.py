"""Search matches evidence attrs values, never key names (SEARCH-ATTRS).

Every detector record carries the same attrs keys (import_sources.
candidate_attrs: brand_code, offer_codes, urls, confidence, fetch_date,
...). Matching their JSON let "code", "offer", "url" or "brand" match
every record. The matcher reads attrs values only: strings and numbers,
walked through nested dicts and lists, never bools or None. An offer code
or a URL in attrs still matches.

Paths: search.permitted_evidence_match itself; customer share Search
(services.shared_search); agent search_evidence, whose Plan A records
(excerpt level, transcript denied) take the direct permitted_evidence_match
path in agent_contracts._match_scope_items and whose Plan B records
(transcript allowed) take its permitted_fields_text fast path.
"""
from __future__ import annotations

import json
import unittest

from earwitness.app import access, collaboration, import_sources, search, services
from tests.test_plan_a_agents import agent_ctx, agents
from tests.test_plan_a_fixture import Workspace

KEY_WORDS = ("code", "codes", "offer", "url", "urls", "brand", "confidence", "fetch_date",
             "candidate_id", "anchor", "provenance", "null", "none", "true", "false")
URL = "https://lumenlamps.example/morning-deal"


def detector_attrs(evidence_id: str) -> dict:
    """Detector-shaped attrs; ad-a3 holds an offer code, ad-a1 a URL."""
    return import_sources.candidate_attrs(
        {"candidate_id": f"c-{evidence_id}", "fetch_date": "2026-09-11",
         "confidence": 0.82, "brand_code": None, "brand_url": None,
         "offer_codes": ["HARBOR20"] if evidence_id == "ad-a3" else [],
         "urls": [URL] if evidence_id == "ad-a1" else [],
         "provenance": {"stage": "detector", "nested": [{"tag": "qzdeep"}],
                        "flag": True, "gone": None}},
        "det")


def set_detector_attrs(ws) -> None:
    con = ws.con
    con.execute("BEGIN IMMEDIATE")
    for (ev,) in con.execute("SELECT id FROM evidence").fetchall():
        con.execute("UPDATE evidence SET attrs=? WHERE id=?",
                    (json.dumps(detector_attrs(ev)), ev))
    con.execute("COMMIT")


class MatcherTest(unittest.TestCase):
    def item(self, attrs):
        return {"id": "probe", "show_title": "Probe Show", "ep_title": "Monday",
                "assocs": [{"brand": "Lumen"}], "excerpt": "a short read", "attrs": attrs}

    def test_key_names_never_match(self):
        for attrs in (detector_attrs("ad-a3"), json.dumps(detector_attrs("ad-a3"))):
            for query in KEY_WORDS:
                with self.subTest(query=query, shape=type(attrs).__name__):
                    self.assertFalse(search.permitted_evidence_match(
                        self.item(attrs), query, True, True, "", ""))

    def test_values_still_match(self):
        attrs = detector_attrs("ad-a3")
        attrs["urls"] = [URL]
        for query in ("harbor20", "HARBOR20", "lumenlamps.example/morning", "2026-09-11",
                      "0.82", "c-ad-a3", "qzdeep", "detector"):
            with self.subTest(query=query):
                self.assertTrue(search.permitted_evidence_match(
                    self.item(attrs), query, False, False))

    def test_integers_and_field_order(self):
        text = search.permitted_fields_text(self.item({"b": 7, "a": "first"}))
        self.assertEqual(text, "Probe Show Monday Lumen a short read first 7")


class SharedSearchTest(unittest.TestCase):
    """Customer share Search (services.shared_search) with attrs granted."""

    FIELDS = ["excerpt", "show_title", "attrs"]

    def setUp(self):
        self.ws = Workspace("B")
        self.addCleanup(self.ws.close)
        set_detector_attrs(self.ws)
        con = self.ws.con
        collaboration.ensure_aux(con)
        if not con.execute("SELECT 1 FROM workspaces WHERE id='local'").fetchone():
            collaboration.create_workspace(con, "local", "W", "owner-1")
        self.shares = {ev: collaboration.create_share(con, "local", "evidence", ev, "guest-1",
                                                      self.FIELDS, "owner-1")["share_id"]
                       for ev in ("ad-a1", "ad-a3")}
        con.commit()

    def search(self, evidence_id, query):
        share_id = self.shares[evidence_id]
        state = collaboration.share_state(self.ws.con, share_id)
        ctx = access.AccessContext.create(
            principal_id="guest-1", principal_kind="guest", workspace_id="local",
            authority_kind="share", share_grant_id=share_id, scopes=(),
            credential_kind="browser_session", content_policy=services._share_policy(state))
        out = services.shared_search(ctx, self.ws.con, None, {"query": query})
        return [i["id"] for i in out["items"]]

    def results(self):
        out = {}
        for ev in ("ad-a1", "ad-a3"):
            for query in KEY_WORDS + ("harbor20", "lumenlamps.example/morning"):
                out[(ev, query)] = self.search(ev, query)
        return out

    def check(self, got):
        for (ev, query), found in got.items():
            with self.subTest(evidence=ev, query=query):
                want = [ev] if (ev, query) in {("ad-a3", "harbor20"),
                                               ("ad-a1", "lumenlamps.example/morning")} else []
                self.assertEqual(found, want)

    def test_plan_b_and_plan_a_match_values_only_and_agree(self):
        plan_b = self.results()
        self.check(plan_b)
        self.ws.plan("A")
        plan_a = self.results()
        self.check(plan_a)
        self.assertEqual(plan_a, plan_b)


class AgentSearchTest(unittest.TestCase):
    """Agent search_evidence: Plan A direct path and Plan B fast path."""

    def run_queries(self, plan):
        ws = Workspace(plan)
        self.addCleanup(ws.close)
        set_detector_attrs(ws)
        out = {}
        for query in KEY_WORDS + ("harbor20", "lumenlamps.example/morning"):
            found = agents.AGENT_ENTRYPOINTS["search_evidence"](
                agent_ctx(ws), {"filter": {"query": query}, "limit": 50})
            out[query] = sorted(i["id"] for i in found["items"])
        return out

    def test_plan_a_and_plan_b_match_values_only_and_agree(self):
        results = {plan: self.run_queries(plan) for plan in ("A", "B")}
        for plan, got in results.items():
            for query in KEY_WORDS:
                with self.subTest(plan=plan, query=query):
                    self.assertEqual(got[query], [])
            with self.subTest(plan=plan, query="harbor20"):
                self.assertEqual(got["harbor20"], ["ad-a3"])
            with self.subTest(plan=plan, query="url"):
                self.assertEqual(got["lumenlamps.example/morning"], ["ad-a1"])
        self.assertEqual(results["A"], results["B"])


if __name__ == "__main__":
    unittest.main()
