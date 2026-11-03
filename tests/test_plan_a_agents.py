"""Plan A excerpt view: agent paths with the agent switch forced on
(design 5 last row, design 9 items 2 and 3). Agent tools are off at
launch (FEATURE_DISABLED is an HTTP gate); these tests call the native
entrypoints directly, so the switch does not hide anything here.

At excerpt level the native tools match and show the safe excerpt only,
and the transcript window refuses.
"""
from __future__ import annotations

import unittest

from earwitness.app import access, agent_contracts as agents
from tests.test_plan_a_fixture import Workspace, leaked, safe_text

SCOPES = ("research:read", "transcripts:read", "notes:read", "watchlists:read",
          "campaigns:read")


def agent_ctx(ws, scopes=SCOPES):
    agents.bind_workspace("local", ws.path)  # the compatibility builders look it up
    return access.AccessContext.create(principal_id="owner-1", principal_kind="owner",
                                       workspace_id="local", credential_kind="reader_bearer",
                                       scopes=scopes, db_path=str(ws.path))


def ids(result):
    return sorted(item["id"] for item in result["items"])


class AgentToolsTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace("A")
        self.addCleanup(self.ws.close)

    def call(self, tool, request, ws=None):
        return agents.AGENT_ENTRYPOINTS[tool](agent_ctx(ws or self.ws), request)

    def test_search_evidence_matches_and_shows_the_safe_excerpt_only(self):
        expect = {"lumen": ["ad-a1"], "zebra": ["ad-a1"], "qzbound": [], "qzlate": [],
                  "qzoversized": [], "qzdetector": [], "qzstored": [], "qzstart": []}
        for q, want in expect.items():
            with self.subTest(q=q):
                out = self.call("search_evidence", {"filter": {"query": q}})
                self.assertEqual(ids(out), want)
                body = {k: v for k, v in out.items() if k != "applied_filter"}
                self.assertEqual(leaked(body), [])
                for item in out["items"]:
                    self.assertEqual(item["excerpt_preview"],
                                     (safe_text(item["id"]) or "")[:400])

    def test_get_evidence_gives_the_safe_excerpt_and_no_interval(self):
        for ev in ("ad-a1", "ad-a2", "ad-a3", "ad-a5"):
            with self.subTest(evidence=ev):
                out = self.call("get_evidence", {"evidence_id": ev})
                data = out["items"][0]
                self.assertEqual(leaked(out), [])
                self.assertEqual(data.get("excerpt_preview") or None, safe_text(ev))
                self.assertNotIn("interval_text_preview", data)

    def test_the_transcript_window_refuses(self):
        with self.assertRaises(Exception):
            self.call("get_transcript_window", {"episode_id": "ep_a",
                                                "transcript_version_id": self.ws.tv["ep_a"],
                                                "start_s": 0.0, "end_s": 60.0})

    def test_summaries_and_compatibility_tools_stay_safe(self):
        out = self.call("summarize_evidence", {"filter": {"query": "lumen"}, "group_by": "brand"})
        self.assertEqual(leaked(out), [])
        for q in ("lumen", "qzlate"):
            found = self.call("search", {"query": q})
            self.assertEqual(leaked({k: v for k, v in found.items()
                                     if k not in ("query", "applied_filter")}), [])
        fetched = self.call("fetch", {"id": "ev:ad-a1"})
        self.assertEqual(leaked(fetched), [])

    def test_agent_answers_do_not_depend_on_hidden_text(self):
        twin = Workspace("A", variant=True)
        self.addCleanup(twin.close)
        for q in ("qzlate", "qzother", "qzoversized", "qzoverother", "nimbus", "closing words"):
            with self.subTest(q=q):
                a = self.call("search_evidence", {"filter": {"query": q}})
                b = self.call("search_evidence", {"filter": {"query": q}}, twin)
                self.assertEqual([(i["id"], i["match_basis"], i.get("excerpt_preview"))
                                  for i in a["items"]],
                                 [(i["id"], i["match_basis"], i.get("excerpt_preview"))
                                  for i in b["items"]])
                self.assertEqual(a["total_matches"], b["total_matches"])


if __name__ == "__main__":
    unittest.main()
