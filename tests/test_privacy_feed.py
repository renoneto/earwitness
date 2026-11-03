"""PRIVACY-FEED: Overview's activity feed never shows another member's
mistake report (BACKLOG item 1; LEDGER-LEAD3-INV-1; RC-SEC-R1-02).

A mistake report is a personal note: customer_saved._project_note shows it
only to the member who created it (404 to every other member, the owner
included); a local operator also sees notes with no recorded creator. The
feed read the audit log with no actor filter, so every member's Overview
listed "Mistake report saved" with the evidence link of other members'
notes. The feed now keeps a customer_mistake_save row only for its actor,
with the same local-operator rule; no other feed row changes.

PRIVACY-FEED-FIX1: a frozen campaign report never holds a personal note
(PF-SEC-R1-01): not in its JSON, its HTML, its export bytes or its pins,
the freezer's own report included. Staleness ignores notes, including the
note keys stored in reports frozen before the change (PF-SEC-R1-02). A
token without notes:read sees no mistake report in the feed (PF-SEC-R1-03).

Run: PYTHONPATH=. python -m unittest tests.test_privacy_feed
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from earwitness.app import access, campaigns, customer, customer_query, customer_saved, db, services
from earwitness.app.connections import PERSONAL_CLIENT_PREFIX

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tests import test_uat_dec_d1api as d1  # noqa: E402
from tests.test_read_cache import OWN, session  # noqa: E402
from tests.test_uat_d1 import _republish  # noqa: E402

SAVED = "Mistake report saved"


def mistake_rows(overview, ref):
    """Feed rows that name the report's evidence."""
    return [a for a in overview["activity"] if ref in a["href"]]


class HostedFeedTest(d1.SeededBase):
    """Two editors A and B, the owner and a viewer of one workspace."""

    def setUp(self):
        super().setUp()
        _republish(self.con)
        customer_query._VIEW_MEMO.clear()
        self.addCleanup(customer_query._VIEW_MEMO.clear)
        self.A, self.B = session("editor-a"), session("editor-b")
        self.viewer, self.owner = session("viewer-v", "viewer"), session("owner-o", "owner")
        self.ref_b = self.ref("r1")
        services.customer_save_mistake(self.B, self.con, OWN, self.ref_b, "B private reason")
        self.con.commit()

    def overview(self, ctx):
        return services.customer_overview(ctx, self.con, None)

    def test_other_members_never_see_the_report(self):
        for who, ctx in (("editor A", self.A), ("owner", self.owner), ("viewer", self.viewer)):
            out = self.overview(ctx)
            self.assertEqual(mistake_rows(out, self.ref_b), [], who)
            self.assertEqual([a for a in out["activity"] if a["text"] == SAVED], [], who)

    def test_the_author_still_sees_their_report(self):
        rows = mistake_rows(self.overview(self.B), self.ref_b)
        self.assertEqual([(a["text"], a["href"]) for a in rows],
                         [(SAVED, f"#/evidence/{self.ref_b}")])

    def test_each_member_sees_only_their_own(self):
        ref_a = self.ref("r3")
        services.customer_save_mistake(self.A, self.con, OWN, ref_a, "A private reason")
        self.con.commit()
        a, b = self.overview(self.A), self.overview(self.B)
        self.assertEqual([x["href"] for x in a["activity"] if x["text"] == SAVED],
                         [f"#/evidence/{ref_a}"])
        self.assertEqual([x["href"] for x in b["activity"] if x["text"] == SAVED],
                         [f"#/evidence/{self.ref_b}"])

    def test_others_reports_do_not_crowd_out_the_callers_own(self):
        """The feed reads the audit log 40 rows deep: other members' reports
        are not read and dropped, so the caller's own older report stays."""
        ref_a = self.ref("r3")
        for i in range(50):   # A's later saves and edits of their own report
            db.audit(self.con, self.A.principal_id, "customer_mistake_save",
                     "customer_mistake_report", str(100 + i), {"ref": ref_a},
                     db.bump_revision(self.con))
        self.con.commit()
        self.assertEqual(mistake_rows(self.overview(self.B), self.ref_b)[0]["href"],
                         f"#/evidence/{self.ref_b}")

    def test_other_feed_rows_are_unchanged(self):
        """Shared workspace actions (alerts) still show to every member."""
        aid = services.customer_save_alert(
            self.A, self.con, OWN, "acme watch",
            {"version": 2, "mode": "ads", "brands": ["acme"]})["id"]
        self.con.commit()
        for ctx in (self.A, self.B, self.owner, self.viewer):
            self.assertIn(("Alert created", f"#/alert/{aid}"),
                          [(a["text"], a["href"]) for a in self.overview(ctx)["activity"]])

    def test_an_old_report_with_no_actor_is_not_shown_to_members(self):
        db.audit(self.con, "", "customer_mistake_save", "customer_mistake_report", "99",
                 {"ref": self.ref("r4")}, db.bump_revision(self.con))
        self.con.commit()
        for ctx in (self.A, self.B, self.owner, self.viewer):
            self.assertEqual(mistake_rows(self.overview(ctx), self.ref("r4")), [])


    def test_a_token_without_notes_read_sees_no_report_even_its_own(self):
        """PF-SEC-R1-03: _project_note needs notes:read; so does the feed."""
        def token(*extra):
            return access.AccessContext.create(
                principal_id=self.B.principal_id, principal_kind="editor", workspace_id=self.B.workspace_id,
                credential_kind="reader_bearer", client_id=PERSONAL_CLIENT_PREFIX + "b",
                scopes=("research:read", "watchlists:read", "campaigns:read") + extra)
        self.assertEqual([a for a in self.overview(token())["activity"] if a["text"] == SAVED], [])
        self.assertEqual(len(mistake_rows(self.overview(token("notes:read")), self.ref_b)), 1)


NOTE = "qzprivate A note text"


class ReportNotesTest(unittest.TestCase):
    """PF-SEC-R1-01/02: editors A and B, the owner and a viewer; A writes a
    note on attached evidence and freezes a report."""

    def setUp(self):
        from tests.test_backend_allocation import _brief, _placement, _revs, _supplied
        from tests.test_plan_a_fixture import ENTRY, Workspace, member
        self.ws = Workspace("A")
        self.addCleanup(self.ws.close)
        self.con, self.entry = self.ws.con, ENTRY
        self.A = member(principal_id="editor-a", principal_kind="editor")
        self.B = member(principal_id="editor-b", principal_kind="editor")
        self.owner = member(principal_id="owner-1", principal_kind="owner")
        self.viewer = member(principal_id="viewer-v", principal_kind="viewer")
        self.readers = (("A", self.A), ("B", self.B), ("owner", self.owner), ("viewer", self.viewer))
        camp = _supplied(self.con, _brief(self.con))["campaign"]
        unit = _placement(self.con, camp["id"], camp["revision"], episode_id="ep_a")["placement"]
        self.cid, self.ref = camp["id"], self.ws.ref("evidence", "ad-a1")
        revision, _ = _revs(self.con, self.cid)
        services.customer_attach_evidence(self.A, self.con, ENTRY, self.cid, unit["id"],
                                          self.ref, revision)

    def note(self, ctx, text=NOTE, **edit):
        return services.customer_save_mistake(ctx, self.con, self.entry, self.ref, text, **edit)

    def freeze(self, ctx):
        draft = services.customer_campaign_draft(ctx, self.con, self.entry, self.cid)["draft"]
        return services.customer_freeze_report(ctx, self.con, self.entry, self.cid,
                                               draft["campaign"]["revision"], draft["pins"])

    def report(self, ctx, no):
        return services.customer_report(ctx, self.con, self.entry, self.cid, no)

    def listed(self, ctx, no):
        return [r for r in services.customer_reports(ctx, self.con, self.entry, self.cid)["reports"]
                if r["report_no"] == no][0]

    def assertNotStale(self, no):
        for who, ctx in self.readers:
            for shown in (self.report(ctx, no), self.listed(ctx, no)):
                self.assertFalse(shown["stale"], (who, shown.get("stale_reasons")))
                self.assertEqual(shown.get("stale_reasons") or [], [], who)

    def test_no_report_holds_the_freezers_note(self):
        self.note(self.A)
        frozen = self.freeze(self.A)
        for who, ctx in self.readers:
            out = self.report(ctx, frozen["report_no"])
            self.assertNotIn("qzprivate", json.dumps(out), who)
            self.assertNotIn("notes", out["frozen"], who)
            self.assertNotIn("notes", out["frozen"]["pins"], who)
            for state in out["frozen"]["pins"]["attachment_state"].values():
                self.assertNotIn("note_versions", state, who)
            html, _ = campaigns.get_customer_report_html(self.con, self.cid, frozen["report_no"], ctx)
            self.assertNotIn("qzprivate", html, who)
            payload = services.customer_export_bytes(ctx, self.con, self.entry,
                                                     frozen["export_id"])["payload"]
            self.assertNotIn("qzprivate", payload if isinstance(payload, str) else payload.decode(), who)
        stored = self.con.execute("SELECT pins_json, report_json FROM campaign_reports"
                                  " WHERE campaign_id=?", (self.cid,)).fetchone()
        self.assertNotIn("qzprivate", stored["report_json"])
        self.assertNotIn("note_versions", stored["pins_json"])
        self.assertNotIn('"notes"', stored["pins_json"])

    def test_a_note_never_stales_a_report(self):
        mine = self.note(self.A)
        frozen = self.freeze(self.A)
        self.assertNotStale(frozen["report_no"])
        self.note(self.A, "qzprivate edited", mistake_id=mine["id"],
                  expected_revision=mine["revision"])
        self.note(self.B, "qzprivate B note")
        self.assertNotStale(frozen["report_no"])

    def test_a_report_stored_before_the_change_is_not_stale_from_its_note_keys(self):
        """Pins frozen before PRIVACY-FEED-FIX1 hold "notes" and each
        attachment's "note_versions" (the freezer's own notes)."""
        mine = self.note(self.A)
        frozen = self.freeze(self.A)
        row = self.con.execute("SELECT id, pins_json FROM campaign_reports WHERE campaign_id=?",
                               (self.cid,)).fetchone()
        pins = json.loads(row["pins_json"])
        versions = [[mine["id"], mine["revision"]]]
        pins["notes"] = {self.ref: versions}
        for state in pins["attachment_state"].values():
            state["note_versions"] = versions
        self.con.execute("UPDATE campaign_reports SET pins_json=? WHERE id=?",
                         (json.dumps(pins), row["id"]))
        self.assertNotStale(frozen["report_no"])


class LocalOperatorFeedTest(d1.SeededBase):
    """The single local operator (scripts/app_server.py): unchanged."""

    def setUp(self):
        super().setUp()
        _republish(self.con)
        ident = self.con.execute("SELECT value FROM meta WHERE key='workspace_id'").fetchone()
        self.ctx = access.AccessContext.create(
            principal_id="owner", principal_kind="owner", workspace_id="local",
            credential_kind="local_operator", db_identity=ident[0] if ident else None)
        self.ref_1 = self.ref("r1")
        with db.transaction(self.con, write=True):
            customer_saved.save_note(self.con, self.ref_1, "operator reason", "open",
                                     None, None, "owner")

    def test_operator_sees_their_report(self):
        rows = mistake_rows(customer.overview(self.con, ctx=self.ctx), self.ref_1)
        self.assertEqual([(a["text"], a["href"]) for a in rows],
                         [(SAVED, f"#/evidence/{self.ref_1}")])

    def test_operator_sees_a_report_with_no_actor(self):
        """As _project_note: a local operator owns notes with no creator."""
        ref = self.ref("r3")
        db.audit(self.con, "", "customer_mistake_save", "customer_mistake_report", "99",
                 {"ref": ref}, db.bump_revision(self.con))
        self.con.commit()
        self.assertEqual(len(mistake_rows(customer.overview(self.con, ctx=self.ctx), ref)), 1)

    def test_no_caller_is_unchanged(self):
        rows = mistake_rows(customer.overview(self.con), self.ref_1)
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
