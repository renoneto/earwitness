"""Plan A excerpt view: captures and retained artifacts (design 5 rows
"saved excerpt creation, campaign attachments", "campaign draft, frozen
report", "research export"; design 6 old artifacts; design 9 items 2, 4,
6, 6b).

New captures at excerpt level store the safe excerpt with `pa1` locators
over both sources; artifacts made before the change hold no `pa1` rule and
are refused (403) without any stored byte changing.
"""
from __future__ import annotations

import json
import unittest

from earwitness.app import access, campaigns, citations, customer_query, db, exports, services
from tests.test_backend_allocation import _brief, _placement, _revs, _supplied
from tests.test_plan_a_fixture import ENTRY, Workspace, leaked, member, safe_text

TEXT_FIELDS = {"excerpt", "transcript"}


class _Base(unittest.TestCase):
    plan = "A"

    def setUp(self):
        self.ws = Workspace(self.plan)
        self.addCleanup(self.ws.close)
        self.con = self.ws.con

    def ref(self, evidence_id):
        return self.ws.ref("evidence", evidence_id)

    def save_excerpt(self, ref, **target):
        body = {"type": "excerpt", "title": "Saved read", "target": {"ref": ref, **target}}
        return services.customer_save_item(member(), self.con, ENTRY, body)["item"]

    def item(self, item_id):
        return services.customer_saved_item(member(), self.con, ENTRY, item_id)

    def export(self, fmt="json", mode="ads", q=""):
        ctx = member()
        body = {"query": {"version": 2, "mode": mode, "q": q}, "scope": "all_matches",
                "format": fmt, "expected_view_revision": customer_query._view_revision(self.con, ctx)}
        return services.customer_research_export(ctx, self.con, ENTRY, body)

    def download(self, export_id):
        return services.customer_export_bytes(member(), self.con, ENTRY, export_id)

    def campaign(self, refs):
        """A supplied-requirements campaign with one ep_a placement holding refs."""
        camp = _supplied(self.con, _brief(self.con))["campaign"]
        unit = _placement(self.con, camp["id"], camp["revision"], episode_id="ep_a")["placement"]
        for ref in refs:
            revision, _ = _revs(self.con, camp["id"])
            services.customer_attach_evidence(member(), self.con, ENTRY, camp["id"], unit["id"],
                                              ref, revision)
        return camp["id"], unit["id"]

    def freeze(self, campaign_id):
        draft = services.customer_campaign_draft(member(), self.con, ENTRY, campaign_id)["draft"]
        return services.customer_freeze_report(member(), self.con, ENTRY, campaign_id,
                                               draft["campaign"]["revision"], draft["pins"])

    def assertPa1(self, locators):
        text = [loc for loc in locators if TEXT_FIELDS & set(loc["fields"])]
        self.assertTrue(text)
        for loc in text:
            self.assertEqual(loc.get("rule"), "pa1", loc)
            self.assertNotIn("transcript", loc["fields"])
            self.assertIn("bounds", loc)

    def assertPlanRefusal(self, call, plan_words=False):
        """403. plan_words: the refusal is the old-artifact rule (excerpt
        locators without pa1), not today's transcript denial."""
        with self.assertRaises(access.AuthorizationError) as raised:
            call()
        self.assertEqual(raised.exception.status, 403)
        if plan_words:
            self.assertIn("not available on your plan", str(raised.exception).lower())

    def stored(self):
        return {"exports": [tuple(r) for r in self.con.execute(
                    "SELECT id, payload, digest, captured_json FROM exports ORDER BY id")],
                "saved": [tuple(r) for r in self.con.execute(
                    "SELECT id, captured_json FROM customer_saved_items ORDER BY id")],
                "reports": [tuple(r) for r in self.con.execute(
                    "SELECT id, report_json, export_digest FROM campaign_reports ORDER BY id")]}


class NewCaptureTest(_Base):
    def test_a_saved_excerpt_stores_the_safe_excerpt_with_pa1_locators_on_both_sources(self):
        ref = self.ref("ad-a1")
        item = self.save_excerpt(ref, start_s=30.0, end_s=31.0)
        captured = item["captured"]
        self.assertEqual(captured["text"], safe_text("ad-a1"))
        self.assertPa1(captured["disclosure"])
        sources = {loc["source_key"] for loc in captured["disclosure"]}
        self.assertTrue({"detector:ad-a1", "transcript:ep_a"} <= sources, sources)
        self.assertEqual(leaked(item), [])
        again = self.item(item["id"])
        self.assertEqual(again["item"]["captured"]["text"] if "item" in again
                         else again["captured"]["text"], safe_text("ad-a1"))

    def test_an_excerpt_unavailable_ad_cannot_be_saved_as_an_excerpt(self):
        with self.assertRaises(Exception):
            self.save_excerpt(self.ref("ad-a2"))

    def test_research_exports_hold_safe_excerpts_in_every_format(self):
        for fmt in ("json", "csv", "html"):
            with self.subTest(format=fmt):
                out = self.export(fmt)
                self.assertEqual(out["total"], 5)
                payload = self.download(out["export_id"])["payload"]
                self.assertEqual(leaked(payload), [], fmt)
                self.assertEqual(leaked(out["rows"]), [])
                for row in out["rows"]:
                    # EXCERPT-CAP (D7): a new export cuts the safe excerpt
                    # to about 200 characters.
                    self.assertEqual(row["text"] or None,
                                     exports.cap_excerpt(safe_text(row["evidence_id"])))
                captured = json.loads(self.con.execute(
                    "SELECT captured_json FROM exports WHERE id=?",
                    (out["export_id"],)).fetchone()[0])
                self.assertPa1(captured["disclosure"])
                if fmt == "json":
                    self.assertIn(safe_text("ad-a1"), payload)

    def test_a_mentions_export_exports_nothing(self):
        out = self.export("json", mode="mentions", q="qzstart")
        self.assertEqual(out["total"], 0)

    def test_campaign_draft_and_frozen_report_hold_safe_excerpts(self):
        cid, _unit = self.campaign([self.ref("ad-a1")])
        draft = services.customer_campaign_draft(member(), self.con, ENTRY, cid)
        self.assertEqual(leaked(draft), [])
        frozen = self.freeze(cid)
        report = services.customer_report(member(), self.con, ENTRY, cid, frozen["report_no"])
        self.assertEqual(leaked(report), [])
        self.assertPa1(report["frozen"]["disclosure"])
        captured = [c for group in report["frozen"]["captured"].values() for c in group]
        self.assertEqual([c["text"] for c in captured], [safe_text("ad-a1")])
        html, _digest = campaigns.get_customer_report_html(self.con, cid, frozen["report_no"],
                                                           member())
        self.assertEqual(leaked(html), [])
        self.assertIn("Lumen lamps", html)

    def test_notes_show_their_ref_and_label(self):
        ref = self.ref("ad-a1")
        services.customer_save_mistake(member(), self.con, ENTRY, ref, "my own words")
        notes = services.customer_note_collection(member(), self.con, ENTRY)
        note = notes["notes"][0]
        self.assertEqual(note["ref"], ref)
        self.assertEqual(note["source_label"]["ep_title"], "Monday")
        self.assertEqual(leaked(notes), [])

    def test_creative_and_saved_lists_stay_safe(self):
        self.save_excerpt(self.ref("ad-a3"))
        listing = services.customer_saved_items(member(), self.con, ENTRY)
        self.assertEqual(leaked(listing), [])
        archive = services.customer_archive(member(), self.con, ENTRY)
        self.assertEqual(leaked(archive), [])


class ReportTest(_Base):
    """A Plan A member can report a wrong ad from its card (corrections.report
    accepts the server's excerpt-level projection, not only full records)."""

    def test_an_excerpt_level_member_reports_a_wrong_ad(self):
        from earwitness.app import corrections
        out = corrections.report(self.con, self.ref("ad-a1"), "wrong_brand", "It is Ring",
                                 ctx=member())
        self.assertEqual(leaked(out), [])
        row = self.con.execute("SELECT reason, evidence_id FROM ad_reports").fetchone()
        self.assertEqual(tuple(row), ("wrong_brand", "ad-a1"))


    def test_a_restricted_sparse_projection_still_cannot_report(self):
        from earwitness.app import corrections
        from tests.test_plan_a_fixture import token
        for plan in ("A", "B"):
            with self.subTest(plan=plan):
                self.ws.plan(plan)
                with self.assertRaises(access.AuthorizationError) as raised:
                    corrections.report(self.con, self.ref("ad-a1"), "not_an_ad",
                                       ctx=token(["research:read"]))
                self.assertEqual(raised.exception.status, 404)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM ad_reports").fetchone()[0], 0)

class RevocationTest(_Base):
    def test_revoking_excerpt_on_either_source_makes_the_capture_unavailable(self):
        for key in ("transcript:ep_a", "detector:ad-a1"):
            with self.subTest(source=key):
                self.ws.plan("A")
                item = self.save_excerpt(self.ref("ad-a1"))
                out = self.export("json", q="lumen")
                self.ws.row(key, "excerpt", "deny")
                with self.assertRaises(access.AuthorizationError):
                    self.item(item["id"])
                with self.assertRaises(access.AuthorizationError):
                    self.download(out["export_id"])
                self.ws.plan("A")
                self.ws.row(key, "excerpt", "allow", 0)
                with self.assertRaises(access.AuthorizationError):
                    self.download(out["export_id"])
                self.ws.plan("A")
                self.item(item["id"])
                self.download(out["export_id"])


class DowngradeTest(_Base):
    """Design 9 item 6: artifacts made under Plan B are refused after the
    change to Plan A, unchanged; new ones made under Plan A are served."""
    plan = "B"

    def test_plan_b_artifacts_are_refused_after_the_change_and_new_ones_are_served(self):
        item = self.save_excerpt(self.ref("ad-a1"))
        out = self.export("json")
        self.assertIn("qzstored", self.download(out["export_id"])["payload"])
        cid, _unit = self.campaign([self.ref("ad-a1")])
        # A frozen report needs transcript export: made while the record
        # still allowed it, as a pilot workspace's was.
        self.con.execute("UPDATE source_content_policy SET export_eligible=1"
                         " WHERE content_category='transcript'")
        frozen = self.freeze(cid)
        self.ws.plan("B")
        before = self.stored()
        self.ws.plan("A")
        self.assertPlanRefusal(lambda: self.item(item["id"]))
        self.assertPlanRefusal(lambda: self.download(out["export_id"]), plan_words=True)
        self.assertPlanRefusal(lambda: exports.load_export_bytes(
            self.con, out["export_id"], member(), export=False), plan_words=True)
        self.assertPlanRefusal(lambda: services.customer_report(
            member(), self.con, ENTRY, cid, frozen["report_no"]))
        self.assertPlanRefusal(lambda: campaigns.get_customer_report_html(
            self.con, cid, frozen["report_no"], member()))
        listing = services.customer_saved_items(member(), self.con, ENTRY)
        self.assertEqual(leaked(listing), [])
        self.assertEqual(leaked(services.customer_archive(member(), self.con, ENTRY)), [])
        self.assertEqual(self.stored(), before)
        # New ones under Plan A are served.
        new_item = self.save_excerpt(self.ref("ad-a1"))
        self.assertEqual(self.item(new_item["id"])["item"]["captured"]["text"]
                         if "item" in self.item(new_item["id"])
                         else self.item(new_item["id"])["captured"]["text"], safe_text("ad-a1"))
        new_export = self.export("json")
        self.assertEqual(leaked(self.download(new_export["export_id"])), [])
        draft = services.customer_campaign_draft(member(), self.con, ENTRY, cid)
        self.assertEqual(leaked(draft), [])
        refrozen = self.freeze(cid)
        report = services.customer_report(member(), self.con, ENTRY, cid, refrozen["report_no"])
        self.assertEqual(leaked(report), [])
        # Code review r4 LOW-1: the report list keeps both rows; the old one
        # carries the plan refusal, the new one is exportable.
        rows = {r["report_no"]: r for r in services.customer_reports(
            member(), self.con, ENTRY, cid)["reports"]}
        old, new = rows[frozen["report_no"]], rows[refrozen["report_no"]]
        self.assertEqual((old["available"], old["exportable"], old.get("export_refusal")),
                         (True, False, "plan"))
        self.assertTrue(new["exportable"])
        with self.assertRaises(access.AuthorizationError) as raised:
            services.customer_report(member(), self.con, ENTRY, cid, frozen["report_no"])
        self.assertEqual(getattr(raised.exception, "details", None), {"reason": "plan"})

    def _old_report(self, refs):
        """A report frozen under Plan B while the record allowed transcript
        export, as a pilot workspace's was."""
        cid, unit = self.campaign(refs)
        self.con.execute("UPDATE source_content_policy SET export_eligible=1"
                         " WHERE content_category='transcript'")
        return cid, unit, self.freeze(cid)

    def test_an_old_report_with_no_attached_evidence_is_listed_with_the_plan_refusal(self):
        # Code review r5 LOW-1: its first refused locator is a transcript
        # locator ("source disclosure denied"), not the excerpt rule.
        cid, _unit, frozen = self._old_report([])
        self.ws.plan("A")
        rows = services.customer_reports(member(), self.con, ENTRY, cid)["reports"]
        self.assertEqual([(r["report_no"], r["available"], r["exportable"],
                           r.get("export_refusal")) for r in rows],
                         [(frozen["report_no"], True, False, "plan")])
        with self.assertRaises(access.AuthorizationError) as raised:
            services.customer_report(member(), self.con, ENTRY, cid, frozen["report_no"])
        self.assertEqual(raised.exception.status, 403)
        self.assertEqual(getattr(raised.exception, "details", None), {"reason": "plan"})

    def test_an_old_report_with_a_source_revoked_outright_is_not_a_plan_refusal(self):
        # Code review r5 NOTE-1: the old-artifact rule refuses ad-a2's
        # locator first (it comes first in the record); ad-a1's source, no
        # longer in the campaign, then has its metadata revoked while its
        # transcript row is no denial. That later refusal is not the plan's.
        cid, unit, frozen = self._old_report([self.ref("ad-a1"), self.ref("ad-a2")])
        for ev in ("ad-a1", "ad-a2"):
            revision, _ = _revs(self.con, cid)
            services.customer_detach_evidence(member(), self.con, ENTRY, cid, unit,
                                              self.ref(ev), revision)
        self.ws.plan("A")
        self.ws.row("detector:ad-a1", "transcript", "allow")
        self.ws.row("detector:ad-a1", "metadata", "deny")
        try:
            rows = services.customer_reports(member(), self.con, ENTRY, cid)["reports"]
        except access.AuthorizationError:
            rows = None
        # Row 211 (ALERT-DENY): a report its source's own row denies is left
        # out of the history; listed, it is never a plan refusal.
        if rows:
            self.assertNotEqual(rows[0].get("export_refusal"), "plan", rows)
        with self.assertRaises(access.AuthorizationError) as raised:
            services.customer_report(member(), self.con, ENTRY, cid, frozen["report_no"])
        self.assertNotEqual(getattr(raised.exception, "details", None), {"reason": "plan"})

    def _assert_not_plan(self, cid, report_no):
        try:
            rows = services.customer_reports(member(), self.con, ENTRY, cid)["reports"]
        except access.AuthorizationError:
            rows = None
        # Row 211 (ALERT-DENY): a report its source's own row denies is left
        # out of the history; listed, it is never a plan refusal.
        for row in [r for r in rows or () if r["report_no"] == report_no]:
            self.assertNotEqual(row.get("export_refusal"), "plan", rows)
        with self.assertRaises(access.AuthorizationError) as raised:
            services.customer_report(member(), self.con, ENTRY, cid, report_no)
        self.assertNotEqual(getattr(raised.exception, "details", None), {"reason": "plan"})

    def test_a_rights_withdrawal_on_an_old_artifact_source_is_not_a_plan_refusal(self):
        # Code review r6 LOW-1 scenario B: ad-a2's own excerpt right is
        # withdrawn after the change; its transcript row stays as Plan A set
        # it, so its locator is still an old artifact.
        cid, unit, frozen = self._old_report([self.ref("ad-a1"), self.ref("ad-a2")])
        revision, _ = _revs(self.con, cid)
        services.customer_detach_evidence(member(), self.con, ENTRY, cid, unit,
                                          self.ref("ad-a2"), revision)
        self.ws.plan("A")
        for category in ("excerpt", "metadata"):
            with self.subTest(denied=category):
                self.ws.plan("A")
                self.ws.row("detector:ad-a2", category, "deny")
                self._assert_not_plan(cid, frozen["report_no"])

    def test_metadata_withdrawn_on_an_attached_old_source_is_not_a_plan_refusal(self):
        # Code review r6 LOW-1, second instance.
        cid, _unit, frozen = self._old_report([self.ref("ad-a1")])
        self.ws.plan("A")
        self.ws.row("detector:ad-a1", "metadata", "deny")
        self._assert_not_plan(cid, frozen["report_no"])

    def test_the_plan_classifier_keeps_every_other_right_on_an_old_locator(self):
        # Astra r5 MED-1 matrix: an old [metadata, excerpt] locator on a
        # Plan A source is a plan refusal only while nothing else refuses it.
        self.ws.plan("A")
        old = [{"source_key": "detector:ad-a2", "fields": ["metadata", "excerpt"]}]
        self.assertTrue(citations.plan_refusal(self.con, old, member()))
        self.assertTrue(citations.plan_refusal(self.con, old, member(), export=True))
        others = {
            "source allowlist": lambda: (member(content_policy={"sources": ["detector:ad-a1"]}),
                                         False),
            "caller lacks excerpt": lambda: (member(content_policy={"excerpt": "deny"}), False),
            "metadata denied": lambda: (self.ws.row("detector:ad-a2", "metadata", "deny")
                                        or member(), False),
            "excerpt denied": lambda: (self.ws.row("detector:ad-a2", "excerpt", "deny")
                                       or member(), False),
            "excerpt not export eligible": lambda: (self.ws.row(
                "detector:ad-a2", "excerpt", "allow", export=0) or member(), True),
        }
        for name, make in others.items():
            with self.subTest(name):
                self.ws.plan("A")
                ctx, export = make()
                before = set(ctx.disclosures)
                self.assertFalse(citations.plan_refusal(self.con, old, ctx, export=export))
                self.assertEqual(set(ctx.disclosures), before)

    def test_a_source_outside_the_callers_allowlist_is_not_listed_as_plan(self):
        # Astra r5 MED-1: a caller whose source allowlist omits the detached
        # source gets a refusal, not a row carrying that source's reference.
        cid, unit, frozen = self._old_report([self.ref("ad-a1"), self.ref("ad-a2")])
        revision, _ = _revs(self.con, cid)
        services.customer_detach_evidence(member(), self.con, ENTRY, cid, unit,
                                          self.ref("ad-a2"), revision)
        self.ws.plan("A")
        # Code review r7 F-1: allow every source an unrestricted list reads
        # except ad-a2, so the call reaches the plan classifier.
        probe = member()
        services.customer_reports(probe, self.con, ENTRY, cid)
        keys = sorted({k for k, _c, _e in probe.disclosures} - {"detector:ad-a2"})
        ctx = member(content_policy={"sources": keys})
        with self.assertRaises(access.AuthorizationError):
            services.customer_reports(ctx, self.con, ENTRY, cid)

    def test_plan_b_keeps_its_exports_and_transcript_captures(self):
        item = self.save_excerpt(self.ref("ad-a1"))
        self.assertIn("transcript", {f for loc in item["captured"]["disclosure"]
                                     for f in loc["fields"]})
        out = self.export("json")
        self.download(out["export_id"])
        self.item(item["id"])


if __name__ == "__main__":
    unittest.main()
