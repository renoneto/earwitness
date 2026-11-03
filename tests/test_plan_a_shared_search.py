"""Plan A excerpt view: shared search (brief PA3, Astra build r2 MED-1).

A share made under Plan B and read after the change to Plan A. Shared
search must project and match the same policy-aware safe excerpt that
shared detail serves: never the stored or detector excerpt, never a
stored-text predicate, and no excerpt where the transcript source refuses
it, the transcript pin is missing, or the row has no transcript. The
grant's object and field restrictions stay; Plan B sharing is unchanged.
"""
from __future__ import annotations

import unittest

from earwitness.app import access, collaboration, services
from tests.test_plan_a_fixture import Workspace, leaked, safe_text

FIELDS = ["excerpt", "show_title"]


class SharedSearchTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace("B", untranscribed=True)
        self.addCleanup(self.ws.close)
        con = self.ws.con
        collaboration.ensure_aux(con)
        if not con.execute("SELECT 1 FROM workspaces WHERE id='local'").fetchone():
            collaboration.create_workspace(con, "local", "W", "owner-1")
        self.shares = {ev: collaboration.create_share(con, "local", "evidence", ev, "guest-1",
                                                      FIELDS, "owner-1")["share_id"]
                       for ev in ("ad-a3", "ad-n1")}
        con.commit()

    def guest(self, evidence_id):
        share_id = self.shares[evidence_id]
        state = collaboration.share_state(self.ws.con, share_id)
        return access.AccessContext.create(
            principal_id="guest-1", principal_kind="guest", workspace_id="local",
            authority_kind="share", share_grant_id=share_id, scopes=(),
            credential_kind="browser_session", content_policy=services._share_policy(state))

    def search(self, ctx, query):
        return services.shared_search(ctx, self.ws.con, None, {"query": query})

    def detail(self, ctx, evidence_id):
        return services.shared_evidence(ctx, self.ws.con, None, evidence_id)

    def replay(self, ctx):
        """The final disclosure check the transport runs after a read."""
        for source_key, field, exporting in sorted(ctx.disclosures):
            ctx.authorize_disclosure(self.ws.con, source_key, [field], export=exporting)

    # -- Plan B unchanged ------------------------------------------------
    def test_plan_b_share_search_is_unchanged(self):
        guest = self.guest("ad-a3")
        out = self.search(guest, "qzdetector")
        self.assertEqual([i["id"] for i in out["items"]], ["ad-a3"])
        self.assertEqual(out["items"][0]["excerpt"],
                         "qzdetector qzstored Brightly sponsorship read")

    # -- after the change to Plan A --------------------------------------
    def test_a_hidden_text_query_matches_nothing(self):
        self.ws.plan("A")
        for query in ("qzdetector", "qzstored", "sponsorship"):
            with self.subTest(query=query):
                guest = self.guest("ad-a3")
                out = self.search(guest, query)
                self.assertEqual(out["items"], [])
                self.assertEqual(out["total_matches"], 0)
                self.replay(guest)

    def test_a_safe_text_query_matches_with_the_safe_text(self):
        self.ws.plan("A")
        for query in ("sponsored", ""):
            with self.subTest(query=query):
                guest = self.guest("ad-a3")
                out = self.search(guest, query)
                self.assertEqual([i["id"] for i in out["items"]], ["ad-a3"])
                self.assertEqual(out["items"][0]["excerpt"], safe_text("ad-a3"))
                self.assertEqual(leaked(out["items"]), [])
                # Both contributing sources were authorized for the text,
                # so the final check sees the transcript source too.
                self.assertIn(("transcript:ep_b", "excerpt", False), guest.disclosures)
                self.replay(guest)

    def test_shared_search_and_shared_detail_serve_the_same_text(self):
        self.ws.plan("A")
        guest = self.guest("ad-a3")
        found = self.search(guest, "")["items"][0]
        detail = self.detail(self.guest("ad-a3"), "ad-a3")
        self.assertEqual(found.get("excerpt"), detail.get("excerpt"))
        self.assertEqual(found.get("excerpt"), safe_text("ad-a3"))

    def test_the_final_check_refuses_after_the_transcript_excerpt_is_denied(self):
        self.ws.plan("A")
        guest = self.guest("ad-a3")
        self.search(guest, "")
        self.ws.row("transcript:ep_b", "excerpt", "deny")
        with self.assertRaises(access.AuthorizationError):
            self.replay(guest)

    def test_a_transcript_denial_gives_no_excerpt(self):
        for category in ("excerpt", "metadata"):
            with self.subTest(denied=category):
                self.ws.plan("A")
                self.ws.row("transcript:ep_b", category, "deny")
                guest = self.guest("ad-a3")
                out = self.search(guest, "")
                self.assertEqual([i["id"] for i in out["items"]], ["ad-a3"])
                self.assertFalse(out["items"][0].get("excerpt"))
                self.assertEqual(leaked(out["items"]), [])
                for query in ("qzdetector", "sponsored"):
                    self.assertEqual(self.search(self.guest("ad-a3"), query)["items"], [])
                detail = self.detail(self.guest("ad-a3"), "ad-a3")
                self.assertEqual(out["items"][0].get("excerpt") or "",
                                 detail.get("excerpt") or "")
                self.replay(guest)

    def test_the_no_transcript_row_has_no_excerpt_and_no_stored_match(self):
        self.ws.plan("A")
        for query in ("qzdetector", "qzstored", "glimmer read"):
            with self.subTest(query=query):
                self.assertEqual(self.search(self.guest("ad-n1"), query)["items"], [])
        guest = self.guest("ad-n1")
        out = self.search(guest, "late show")
        self.assertEqual([i["id"] for i in out["items"]], ["ad-n1"])
        self.assertFalse(out["items"][0].get("excerpt"))
        self.assertEqual(leaked(out), [])
        self.replay(guest)
        detail = self.detail(self.guest("ad-n1"), "ad-n1")
        self.assertEqual(out["items"][0].get("excerpt") or "", detail.get("excerpt") or "")

    def test_a_missing_transcript_pin_gives_no_excerpt(self):
        self.ws.plan("A")
        con = self.ws.con
        from earwitness.app import citations
        con.execute("BEGIN IMMEDIATE")
        con.execute("UPDATE evidence SET transcript_version_id=NULL, revision=revision+1"
                    " WHERE id='ad-a3'")
        citations.publish_episode_sources(con, "ep_b", "probe")
        con.execute("COMMIT")
        for query in ("qzdetector", "sponsorship"):
            self.assertEqual(self.search(self.guest("ad-a3"), query)["items"], [])
        # The unpinned row may drop out of the evidence search itself (it
        # does on the red base too); any row served carries no text.
        out = self.search(self.guest("ad-a3"), "")
        for item in out["items"]:
            self.assertFalse(item.get("excerpt"))
        self.assertEqual(leaked(out), [])
        self.assertEqual(leaked(self.detail(self.guest("ad-a3"), "ad-a3")), [])

    def test_the_grant_still_pins_object_and_fields(self):
        self.ws.plan("A")
        out = self.search(self.guest("ad-a3"), "")
        item = out["items"][0]
        self.assertNotIn("assocs", item)
        self.assertNotIn("interval_text", item)
        self.assertNotIn("ad-n1", [i["id"] for i in out["items"]])


if __name__ == "__main__":
    unittest.main()
