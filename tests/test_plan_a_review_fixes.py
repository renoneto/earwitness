"""Plan A excerpt view, review round 1 fixes (brief PA2).

- Evidence with no transcript version (code HIGH-1) and a missing
  transcript pin (Astra MED-1): the evidence source is still authorized
  (Plan B refuses a denied one), and at excerpt level the row is served
  with no text; neither its text nor any match comes from the stored or
  detector excerpt.
- Alert history at excerpt level keys on the workspace plan, never on the
  credential's transcript scope (Astra MED-2).
- Restricted callers keep today's search projection (code LOW-2).
- The Search page route carries the plan signal (code MED-1, LOW-4).
- Creative detail serves the safe excerpt (code MED-2).
- Alert backfill at excerpt level (code LOW-1, Astra LOW-2) and
  check_alert(ctx=None) (code LOW-3) stay on the pa1 basis.
"""
from __future__ import annotations

import json
import unittest

from earwitness.app import access, agent_contracts as agents, citations, creatives, customer
from earwitness.app import customer_query, db, observations, services
from tests.test_plan_a_fixture import (ENTRY, Workspace, leaked, member, safe_text, strings, token,
                                       transcripts)

SCOPES = ("research:read", "transcripts:read", "notes:read", "watchlists:read",
          "campaigns:read")
PAT = ("research:read", "watchlists:read", "campaigns:read")


def agent(ws, tool, request):
    agents.bind_workspace("local", ws.path)
    ctx = access.AccessContext.create(principal_id="owner-1", principal_kind="owner",
                                      workspace_id="local", credential_kind="reader_bearer",
                                      scopes=SCOPES, db_path=str(ws.path))
    return agents.AGENT_ENTRYPOINTS[tool](ctx, request)


def research(ws, mode, q, ctx=None):
    return services.customer_research_query(ctx or member(), ws.con, ENTRY,
                                            {"query": {"version": 2, "mode": mode, "q": q}})


def result_ids(out):
    return sorted(i.get("evidence_id") or i.get("episode_id") or i.get("ref")
                  for i in out["result"]["items"])


class UntranscribedEvidenceTest(unittest.TestCase):
    """Code HIGH-1: a detector ad on an episode with no transcript."""

    def ws(self, plan):
        ws = Workspace(plan, untranscribed=True)
        self.addCleanup(ws.close)
        return ws

    def test_plan_b_refuses_a_record_whose_evidence_source_is_denied(self):
        ws = self.ws("B")
        for category in ("metadata", "excerpt", "transcript", "audio"):
            ws.row("detector:ad-n1", category, "deny")
        ref = ws.ref("evidence", "ad-n1")
        with self.assertRaises(access.AuthorizationError):
            services.customer_record(member(), ws.con, ENTRY, ref)

    def test_plan_a_search_never_matches_or_shows_the_stored_excerpt(self):
        ws = self.ws("A")
        for mode in ("ads", "episodes"):
            with self.subTest(mode=mode):
                out = research(ws, mode, "qzdetector")
                self.assertEqual(out["result"]["total"], 0)
                self.assertEqual(leaked(out["result"]), [])
        out = research(ws, "ads", "glimmer")
        self.assertEqual(result_ids(out), ["ad-n1"])
        self.assertEqual(leaked(out["result"]), [])
        self.assertFalse(out["result"]["items"][0].get("text"))
        found = services.customer_search(member(), ws.con, ENTRY,
                                         {"version": 2, "mode": "ads", "q": "glimmer"})
        self.assertEqual(leaked(found["items"]), [])

    def test_plan_a_record_is_metadata_with_no_text(self):
        ws = self.ws("A")
        rec = services.customer_record(member(), ws.con, ENTRY,
                                       ws.ref("evidence", "ad-n1"))["record"]
        self.assertEqual(rec.get("disclosure"), "excerpt")
        self.assertIsNone(rec.get("text"))
        self.assertEqual(leaked(rec), [])

    def test_plan_a_agent_tools_carry_no_marker(self):
        ws = self.ws("A")
        self.assertEqual(agent(ws, "search_evidence", {"filter": {"query": "qzdetector"}})["items"],
                         [])
        found = agent(ws, "search_evidence", {"filter": {"query": "glimmer"}})
        self.assertEqual([i["id"] for i in found["items"]], ["ad-n1"])
        self.assertEqual(leaked({k: v for k, v in found.items() if k != "applied_filter"}), [])
        detail = agent(ws, "get_evidence", {"evidence_id": "ad-n1"})
        self.assertEqual(leaked(detail), [])

    def test_plan_a_capture_of_the_row_holds_pa1_locators(self):
        ws = self.ws("A")
        locators = citations.captured_disclosure(ws.con, ws.ref("evidence", "ad-n1"), member())
        texts = [loc for loc in locators if "excerpt" in loc["fields"]]
        self.assertTrue(texts)
        self.assertTrue(all(loc.get("rule") == "pa1" for loc in texts), locators)


class MissingPinTest(unittest.TestCase):
    """Astra MED-1: transcript pins removed from a Plan A row."""

    def setUp(self):
        self.ws = Workspace("A")
        self.addCleanup(self.ws.close)

    def drop_pins(self, episode=True):
        con = self.ws.con
        con.execute("BEGIN IMMEDIATE")
        con.execute("UPDATE evidence SET transcript_version_id=NULL, revision=revision+1"
                    " WHERE id='ad-a3'")
        if episode:
            con.execute("UPDATE episodes SET transcript_version_id=NULL, revision=revision+1"
                        " WHERE id='ep_b'")
        citations.publish_episode_sources(con, "ep_b", "probe")
        con.execute("COMMIT")

    def test_research_query_never_matches_or_shows_the_detector_excerpt(self):
        self.drop_pins()
        for mode in ("ads", "episodes"):
            with self.subTest(mode=mode):
                out = research(self.ws, mode, "qzdetector")
                self.assertEqual(out["result"]["total"], 0)
                self.assertEqual(leaked(out["result"]), [])
        out = research(self.ws, "ads", "brightly")
        self.assertEqual(result_ids(out), ["ad-a3"])
        self.assertEqual(leaked(out["result"]), [])

    def test_native_search_and_detail_carry_no_marker(self):
        self.drop_pins()
        self.assertEqual(agent(self.ws, "search_evidence",
                               {"filter": {"query": "qzdetector"}})["items"], [])
        detail = agent(self.ws, "get_evidence", {"evidence_id": "ad-a3"})
        self.assertEqual(leaked(detail), [])

    def test_only_the_evidence_pin_missing(self):
        self.drop_pins(episode=False)
        detail = agent(self.ws, "get_evidence", {"evidence_id": "ad-a3"})
        self.assertEqual(leaked(detail), [])
        out = research(self.ws, "ads", "qzdetector")
        self.assertEqual(out["result"]["total"], 0)
        rec = services.customer_record(member(), self.ws.con, ENTRY,
                                       self.ws.ref("evidence", "ad-a3"))["record"]
        self.assertEqual(leaked(rec), [])


def refs_in(value) -> list:
    """(path, string) of every source ref anywhere in value."""
    return [(path, text) for path, text in strings(value) if "sr1:" in text or "sr1%3A" in text]


class _AlertTwins(unittest.TestCase):
    HIDDEN = {"version": 2, "mode": "ads", "q": "qzlate"}

    def setUp(self):
        self.one = Workspace("B")
        self.two = Workspace("B", variant=True)
        self.addCleanup(self.one.close)
        self.addCleanup(self.two.close)
        self.ids = {}
        for ws in (self.one, self.two):
            self.ids[id(ws)] = services.customer_save_alert(
                member(), ws.con, ENTRY, "Hidden words", self.HIDDEN, "/searches",
                ["new_finding"])["id"]
            ws.add_ad("ad-a6", "ep_d", 98.0, 122.0, "Nimbus", "nimbus again")
            services.customer_check_alert(member(), ws.con, ENTRY, self.ids[id(ws)])


class PersonalTokenHistoryTest(_AlertTwins):
    """Astra MED-2: a token without transcript scope after the change."""

    def pat(self):
        return token(PAT, client_id="pat:probe")

    def test_the_token_is_a_personal_token(self):
        self.assertTrue(services.is_personal_token(self.pat()))

    def test_overview_events_and_totals_withhold_unmarked_history(self):
        for ws in (self.one, self.two):
            ws.plan("A")
        for ws in (self.one, self.two):
            with self.subTest(fixture="hidden" if ws is self.one else "twin"):
                overview = services.customer_overview(self.pat(), ws.con, ENTRY)
                self.assertEqual(overview["recent_events"], [])
                self.assertEqual(overview["alert_totals"], [])
                self.assertTrue(customer.excerpt_plan(ws.con))
                self.assertFalse(customer.excerpt_workspace(ws.con, self.pat()))

    def test_alert_list_and_detail_withhold_unmarked_history(self):
        """Code LOW-2, Astra NOTE-1: the HIDDEN alert has no current match
        in Plan A, so the token's list and detail reads succeed; the
        history filter keys on the plan, not on the credential. The list
        runs no scan and carries no current_matches (DEMO2 70dd2587)."""
        for ws in (self.one, self.two):
            ws.plan("A")
        for ws in (self.one, self.two):
            with self.subTest(fixture="hidden" if ws is self.one else "twin"):
                alert_id = self.ids[id(ws)]
                listed = [a for a in services.customer_alerts(self.pat(), ws.con, ENTRY)["alerts"]
                          if a["id"] == alert_id]
                detail = services.customer_alert(self.pat(), ws.con, ENTRY, alert_id)["alert"]
                self.assertNotIn("current_matches", listed[0])
                self.assertEqual(refs_in(listed[0]), [])
                self.assertEqual(detail["current_matches"]["refs"], [])
                for alert in (listed[0], detail):
                    self.assertIs(alert["history_withheld"], True)
                    self.assertIsNone(alert["baseline_count"])
                    self.assertEqual(alert["events"], [])
                    self.assertEqual(alert["unread_event_count"], 0)
                    # The alert's own query echoes its words; nothing else may.
                    self.assertEqual(leaked({k: v for k, v in alert.items()
                                             if k not in ("query", "selector")}), [])

    def test_plan_b_token_still_sees_its_history(self):
        overview = services.customer_overview(self.pat(), self.one.con, ENTRY)
        self.assertEqual(len(overview["alert_totals"]), 1)


class SparseTokenAlertTest(unittest.TestCase):
    """Code LOW-1: a sparse personal token reading an alert with a current
    match gets the missing-ref refusal (403) on the detail, never a KeyError
    (500). The list runs no scan, so it answers with no current_matches and
    no ref (DEMO2 70dd2587)."""

    def pat(self):
        return token(PAT, client_id="pat:probe")

    def assertRefused(self, ws, alert_id):
        listed = [a for a in services.customer_alerts(self.pat(), ws.con, ENTRY)["alerts"]
                  if a["id"] == alert_id]
        self.assertEqual(len(listed), 1)
        self.assertNotIn("current_matches", listed[0])
        self.assertEqual(refs_in(listed[0]), [])
        self.assertEqual(leaked({k: v for k, v in listed[0].items()
                                 if k not in ("query", "selector")}), [])
        with self.assertRaises(access.AuthorizationError) as raised:
            services.customer_alert(self.pat(), ws.con, ENTRY, alert_id)
        self.assertEqual(raised.exception.status, 403)

    def test_an_empty_baseline_alert_with_a_new_match(self):
        for plan in ("A", "B"):
            with self.subTest(plan=plan):
                ws = Workspace(plan)
                self.addCleanup(ws.close)
                made = services.customer_save_alert(
                    member(), ws.con, ENTRY, "Glowzz", {"version": 2, "mode": "ads", "q": "glowzz"},
                    "/searches", ["new_finding"])
                ws.add_ad("ad-a6", "ep_b", 22.0, 30.0, "Glowzz", "glowzz read")
                self.assertTrue(services.customer_alert(member(), ws.con, ENTRY, made["id"])
                                ["alert"]["current_matches"]["count"])
                self.assertRefused(ws, made["id"])

    def test_a_plan_b_alert_read_after_the_change_to_plan_a(self):
        ws = Workspace("B")
        self.addCleanup(ws.close)
        made = services.customer_save_alert(
            member(), ws.con, ENTRY, "Nimbus", {"version": 2, "mode": "ads", "q": "nimbus"},
            "/searches", ["new_finding"])
        ws.plan("A")
        self.assertRefused(ws, made["id"])


class RestrictedCallerTest(unittest.TestCase):
    """Code LOW-2: a credential without transcript scope keeps today's rule."""

    def test_metadata_only_credential_keeps_its_search_rows(self):
        counts = {}
        for plan in ("A", "B"):
            ws = Workspace(plan)
            self.addCleanup(ws.close)
            ctx = token(["transcripts:read"])
            self.assertFalse(ctx.permits("excerpt"))
            out = research(ws, "ads", "", ctx=ctx)
            counts[plan] = result_ids(out)
        self.assertEqual(counts["A"], counts["B"])
        self.assertTrue(counts["A"])

    def test_a_credential_that_refuses_the_excerpt_gets_no_text(self):
        """Code MED-1 (mutant N2): a transcripts:read-only token keeps its
        Plan A rows with no text, and never matches on the safe excerpt."""
        ws = Workspace("A")
        self.addCleanup(ws.close)
        ctx = token(["transcripts:read"])
        self.assertFalse(ctx.permits("excerpt"))
        out = research(ws, "ads", "", ctx=ctx)
        self.assertTrue(out["result"]["items"])
        safe = {safe_text(ev) for ev in ("ad-a1", "ad-a3", "ad-a5")}
        for item in out["result"]["items"]:
            with self.subTest(item=item.get("evidence_id")):
                self.assertFalse(item.get("text"))
                self.assertFalse(item.get("interval"))
                self.assertEqual([s for s in safe if s in json.dumps(item)], [])
        self.assertEqual(leaked(out["result"]), [])
        found = research(ws, "ads", "sponsored", ctx=ctx)
        self.assertEqual(found["result"]["items"], [])
        self.assertEqual(found["result"]["total"], 0)

    def test_a_research_token_never_gets_the_stored_excerpt(self):
        """Keeping restricted rows must not hand a research-scoped token
        (no transcript scope) the stored or detector excerpt, as text or as
        a match: it reads the safe excerpt."""
        ws = Workspace("A")
        self.addCleanup(ws.close)
        ctx = token(["research:read"], client_id="pat:probe")
        for q in ("qzdetector", "qzlate", "qzstored"):
            with self.subTest(q=q):
                self.assertEqual(research(ws, "ads", q, ctx=ctx)["result"]["total"], 0)
        out = research(ws, "ads", "", ctx=ctx)
        self.assertEqual(result_ids(out), ["ad-a1", "ad-a2", "ad-a3", "ad-a4", "ad-a5"])
        self.assertEqual(leaked(out["result"]), [])


class SearchPlanSignalTest(unittest.TestCase):
    """Code MED-1, LOW-4, Astra LOW-1: the route the Search page uses."""

    def test_plan_a_research_query_says_excerpt_only_even_with_no_results(self):
        ws = Workspace("A")
        self.addCleanup(ws.close)
        out = research(ws, "ads", "nothingmatcheszz")
        self.assertEqual(out["result"]["total"], 0)
        self.assertIs(out["result"].get("excerpt_only"), True)
        mentions = research(ws, "mentions", "")
        self.assertIs(mentions["result"].get("plan_limited"), True)
        self.assertIs(mentions["result"].get("excerpt_only"), True)
        self.assertEqual(mentions["result"]["items"], [])
        self.assertEqual(mentions["result"]["total"], 0)

    def test_plan_b_research_query_has_no_plan_flag(self):
        ws = Workspace("B")
        self.addCleanup(ws.close)
        for mode in ("ads", "mentions", "episodes"):
            out = research(ws, mode, "")
            self.assertNotIn("excerpt_only", out["result"])
            self.assertNotIn("plan_limited", out["result"])

    def test_a_plan_a_mentions_export_says_plan_limited(self):
        ws = Workspace("A")
        self.addCleanup(ws.close)
        ctx = member()
        body = {"query": {"version": 2, "mode": "mentions", "q": ""}, "scope": "all_matches",
                "format": "json",
                "expected_view_revision": customer_query._view_revision(ws.con, ctx)}
        out = services.customer_research_export(ctx, ws.con, ENTRY, body)
        self.assertEqual(out["total"], 0)
        self.assertIs(out.get("plan_limited"), True)


class CreativeDetailTest(unittest.TestCase):
    """Code MED-2: a creative linked to an ad whose stored excerpt holds markers."""

    def test_creative_detail_serves_the_safe_excerpt(self):
        ws = Workspace("A")
        self.addCleanup(ws.close)
        con = ws.con
        segments = transcripts()["ep_b"]
        oid = observations.propose_observation(
            con, episode_id="ep_b", start_s=10.0, end_s=22.0,
            bounds_evidence={"segments": segments},
            fields={"read_type": {"value": "unknown"}}, attributions=[],
            source_pins={"source_version_id": ws.sv["ad-a3"],
                         "transcript_version_id": ws.tv["ep_b"]},
            actor="pipeline")["observation_id"]
        con.execute("BEGIN IMMEDIATE")
        con.execute("UPDATE evidence SET attrs=?, revision=revision+1 WHERE id='ad-a3'",
                    (json.dumps({"observation_id": oid}),))
        citations.publish_source_state(con, kind="evidence", record_id="ad-a3", actor="probe")
        con.execute("COMMIT")
        cid = creatives.create_creative(con, [oid])["creative_id"]
        detail = services.customer_creative_detail(member(), con, ENTRY, cid)["creative"]
        refs = [r for m in detail["members"] for r in m.get("evidence_refs") or []
                if r["evidence_id"] == "ad-a3"]
        self.assertEqual(len(refs), 1)
        self.assertEqual(leaked(detail), [])
        self.assertIn(refs[0]["excerpt"], ((safe_text("ad-a3") or "")[:240], ""))


class AlertBackfillTest(_AlertTwins):
    """Code LOW-1, Astra LOW-2."""

    def baseline(self, ws, alert_id):
        return json.loads(ws.con.execute("SELECT baseline_json FROM customer_alerts WHERE id=?",
                                         (alert_id,)).fetchone()[0])

    def test_a_plan_a_backfill_is_stamped_pa1_and_visible(self):
        ws = self.one
        ws.plan("A")
        made = services.customer_save_alert(member(), ws.con, ENTRY, "Lumen",
                                            {"version": 2, "mode": "ads", "q": "lumen"},
                                            "/searches", ["new_finding"])
        out = services.customer_alert_backfill(member(), ws.con, ENTRY, made["id"], True)
        self.assertEqual(out["imported"], 1)
        alert = services.customer_alert(member(), ws.con, ENTRY, made["id"])["alert"]
        self.assertEqual([e["basis"] for e in alert["events"]], ["pa1"])
        self.assertEqual(leaked(alert["events"]), [])
        row = ws.con.execute("SELECT logical_identity, transition_key, payload_json"
                             " FROM customer_alert_events WHERE alert_id=?",
                             (made["id"],)).fetchone()
        self.assertEqual(json.loads(row["payload_json"]).get("basis"), "pa1")
        stored = dict(ws.con.execute("SELECT * FROM customer_alerts WHERE id=?",
                                     (made["id"],)).fetchone())
        info = self.baseline(ws, made["id"])["members"][row["logical_identity"]]
        self.assertEqual(row["transition_key"], customer._transition_key(
            stored, row["logical_identity"], info, 0, basis="pa1"))
        again = services.customer_alert_backfill(member(), ws.con, ENTRY, made["id"], True)
        self.assertEqual(again["imported"], 0)

    def test_a_plan_a_backfill_needs_a_pa1_baseline(self):
        ws = self.one
        alert_id = self.ids[id(ws)]
        before = ws.con.execute("SELECT COUNT(*) FROM customer_alert_events").fetchone()[0]
        ws.plan("A")
        with self.assertRaises(Exception) as raised:
            services.customer_alert_backfill(member(), ws.con, ENTRY, alert_id, True)
        self.assertIn("baseline", str(raised.exception).lower())
        self.assertEqual(ws.con.execute("SELECT COUNT(*) FROM customer_alert_events")
                         .fetchone()[0], before)


class OwnerContextCheckTest(unittest.TestCase):
    """Code LOW-3: check_alert(ctx=None) matches at the owner's level."""

    def test_a_ctx_none_check_never_matches_hidden_text(self):
        ws = Workspace("A")
        self.addCleanup(ws.close)
        made = services.customer_save_alert(member(), ws.con, ENTRY, "Hidden",
                                            {"version": 2, "mode": "ads", "q": "qzlate"},
                                            "/searches", ["new_finding"])
        ws.add_ad("ad-a6", "ep_d", 98.0, 122.0, "Nimbus", "nimbus again")
        out = customer.check_alert(ws.con, made["id"])
        self.assertEqual(out["emitted"], 0)
        baseline = json.loads(ws.con.execute("SELECT baseline_json FROM customer_alerts"
                                             " WHERE id=?", (made["id"],)).fetchone()[0])
        self.assertEqual(baseline["basis"], "pa1")
        self.assertEqual(baseline["members"], {})

    def test_the_matcher_without_a_context_uses_the_owner_level(self):
        ws = Workspace("A")
        self.addCleanup(ws.close)
        episodes = {"ep_a", "ep_b", "ep_c", "ep_d"}
        for q in ("qzlate", "qzstored", "qzdetector", "qzoversized"):
            with self.subTest(q=q):
                matched, _ = customer._match_evidence(
                    ws.con, episodes, {"brand": "", "self_promo": "include"}, [q], [], ctx=None)
                self.assertEqual([m["ev"]["id"] for m in matched], [])
        matched, _ = customer._match_evidence(
            ws.con, episodes, {"brand": "", "self_promo": "include"}, ["lumen"], [], ctx=None)
        self.assertEqual([m["ev"]["id"] for m in matched], ["ad-a1"])


if __name__ == "__main__":
    unittest.main()
