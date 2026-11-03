"""UAT 2026-10-15 walkthrough re-run, UAT-100: an archived campaign is read-only.

An archived campaign allows reading, its stored reports and Restore (the
status change back to active), and nothing else. Every campaign, target,
placement, attachment, allocation, note and freeze write is refused with
ops.Archived (409 CAMPAIGN_ARCHIVED on both servers) and writes nothing.
allowed_actions says the same. Restore makes writes allowed again. Frozen
report bytes are untouched (R1-08 keeps Archive / Restore from staling them).
"""
from __future__ import annotations

import sqlite3
import unittest

from earwitness.app import campaigns, citations, ops
from tests.test_customer_campaigns import add_candidate, fresh_db, seed_campaign, seed_episode
from tests import test_uat_cui as _cui
from tests.test_uat_cui import _brief_ref, _rev, _show_of, _window

ARCHIVED = ["read", "stored_reports", "restore"]
ACTIVE = ["read", "edit", "targets", "placements", "freeze", "reports"]
MESSAGE = "This campaign is archived. Restore it to make changes."


def _status(con, cid, status):
    return campaigns.save_customer_campaign(con, {"campaign": {"status": status}}, cid, _rev(con, cid))


def _new_placement(con, cid, show, date_from="2026-03-15", date_to="2026-03-31"):
    return {"placement": {"scope": "show_window", "show_id": show, "date_from": date_from,
                          "date_to": date_to, "timezone": "UTC", "expected_count": 1,
                          "requirements": {}, "provenance": {"brief_ref": _brief_ref(con, cid)},
                          "binding": "eligible"},
            "expected_campaign_revision": _rev(con, cid)}


def _placements(con, cid):
    return con.execute("SELECT COUNT(*) FROM expected_placements WHERE campaign_id=?", (cid,)).fetchone()[0]


class SuppliedArchivedTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con, show="Casefile")
        add_candidate(self.con, "ev-1", "ep-1")
        self.ref = citations.current_source_ref(self.con, "evidence", "ev-1")
        self.show = _show_of(self.con)
        self.cid = seed_campaign(self.con)
        self.pid = _window(self.con, self.cid, self.show)
        campaigns.save_placement_evidence(self.con, self.cid, self.pid, self.ref, _rev(self.con, self.cid))
        draft = campaigns.build_customer_draft(self.con, self.cid)
        self.report_no = campaigns.freeze_customer_report(
            self.con, self.cid, _rev(self.con, self.cid), draft["pins"])["report_no"]
        self.bytes = campaigns.get_customer_report_html(self.con, self.cid, self.report_no)
        _status(self.con, self.cid, "archived")

    def tearDown(self):
        self.con.close()

    def _refused(self, call):
        rev, placements = _rev(self.con, self.cid), _placements(self.con, self.cid)
        with self.assertRaises(ops.Archived) as caught:
            call()
        self.assertEqual(str(caught.exception), MESSAGE)
        self.assertEqual(_rev(self.con, self.cid), rev, "nothing was written")
        self.assertEqual(_placements(self.con, self.cid), placements)

    def _placement_rev(self):
        return self.con.execute("SELECT revision FROM expected_placements WHERE id=?", (self.pid,)).fetchone()[0]

    def test_allowed_actions_say_read_reports_and_restore_only(self):
        row = self.con.execute("SELECT * FROM campaigns WHERE id=?", (self.cid,)).fetchone()
        self.assertEqual(campaigns.campaign_projection(self.con, dict(row))["allowed_actions"], ARCHIVED)
        self.assertEqual(campaigns.build_customer_draft(self.con, self.cid)["allowed_actions"], ARCHIVED)
        _status(self.con, self.cid, "active")
        row = self.con.execute("SELECT * FROM campaigns WHERE id=?", (self.cid,)).fetchone()
        self.assertEqual(campaigns.campaign_projection(self.con, dict(row))["allowed_actions"], ACTIVE)
        self.assertEqual(campaigns.build_customer_draft(self.con, self.cid)["allowed_actions"], ACTIVE)

    def test_campaign_edits_are_refused(self):
        for fields in ({"name": "New name"}, {"notes": "a note"}, {"message": "new words"},
                       {"status": "active", "name": "New name"}):
            self._refused(lambda f=fields: campaigns.save_customer_campaign(
                self.con, {"campaign": f}, self.cid, _rev(self.con, self.cid)))

    def test_placement_writes_are_refused(self):
        self._refused(lambda: campaigns.save_customer_placement(
            self.con, self.cid, _new_placement(self.con, self.cid, self.show)))
        self._refused(lambda: campaigns.save_customer_placement(
            self.con, self.cid, {"placement": {"expected_count": 2},
                                 "expected_campaign_revision": _rev(self.con, self.cid)},
            self.pid, self._placement_rev()))
        self._refused(lambda: campaigns.save_expected_placement(
            self.con, self.cid, scope="show_window", show_id=self.show,
            provenance={"brief_ref": _brief_ref(self.con, self.cid)}))
        self._refused(lambda: campaigns.update_expected_placement(
            self.con, self.cid, self.pid, {"expected_count": 2}, self._placement_rev()))

    def test_attachment_and_allocation_writes_are_refused(self):
        self._refused(lambda: campaigns.remove_placement_evidence(
            self.con, self.cid, self.pid, self.ref, _rev(self.con, self.cid)))
        self._refused(lambda: campaigns.save_placement_evidence(
            self.con, self.cid, self.pid, self.ref, _rev(self.con, self.cid)))
        # Move needs a second placement: StaleOrderArchivedTest covers it.
        for action in ("retain", "reference"):
            self._refused(lambda a=action: campaigns.allocate_placement_evidence(
                self.con, self.cid, {"action": a, "ref": self.ref, "from_target_id": self.pid,
                                     "to_target_id": self.pid,
                                     "expected_campaign_revision": _rev(self.con, self.cid),
                                     "expected_from_revision": self._placement_rev(),
                                     "expected_to_revision": self._placement_rev()}))

    def test_freeze_is_refused(self):
        pins = campaigns.build_customer_draft(self.con, self.cid)["pins"]
        self._refused(lambda: campaigns.freeze_customer_report(self.con, self.cid, _rev(self.con, self.cid), pins))

    def test_reads_reports_and_restore_are_allowed(self):
        campaigns.build_customer_draft(self.con, self.cid)
        listed = campaigns.list_customer_reports(self.con, self.cid)
        self.assertEqual([r["report_no"] for r in listed], [self.report_no])
        self.assertFalse(campaigns.get_customer_report(self.con, self.cid, self.report_no)["stale"])
        self.assertEqual(campaigns.get_customer_report_html(self.con, self.cid, self.report_no), self.bytes)
        self._refused(lambda: _status(self.con, self.cid, "archived"))  # only Restore changes status
        out = _status(self.con, self.cid, "active")
        self.assertEqual(out["campaign"]["status"], "active")
        self.assertFalse(campaigns.get_customer_report(self.con, self.cid, self.report_no)["stale"])
        self.assertEqual(campaigns.get_customer_report_html(self.con, self.cid, self.report_no), self.bytes)

    def test_restore_allows_writes_again(self):
        _status(self.con, self.cid, "active")
        campaigns.save_customer_placement(self.con, self.cid, _new_placement(self.con, self.cid, self.show))
        self.assertEqual(_placements(self.con, self.cid), 2)
        campaigns.remove_placement_evidence(self.con, self.cid, self.pid, self.ref, _rev(self.con, self.cid))
        campaigns.save_placement_evidence(self.con, self.cid, self.pid, self.ref, _rev(self.con, self.cid))
        campaigns.save_customer_campaign(self.con, {"campaign": {"name": "Renamed"}}, self.cid, _rev(self.con, self.cid))
        pins = campaigns.build_customer_draft(self.con, self.cid)["pins"]
        campaigns.freeze_customer_report(self.con, self.cid, _rev(self.con, self.cid), pins)

    def test_a_stale_restore_is_still_a_conflict(self):
        rev = _rev(self.con, self.cid)
        with self.assertRaises(ops.Conflict):
            campaigns.save_customer_campaign(self.con, {"campaign": {"status": "active"}}, self.cid, rev - 1)


class StaleOrderArchivedTest(unittest.TestCase):
    """A stale displayed revision on an archived campaign gets the conflict
    reply, as on an active one; a current revision gets CAMPAIGN_ARCHIVED."""

    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con, show="Casefile")
        add_candidate(self.con, "ev-1", "ep-1")
        self.ref = citations.current_source_ref(self.con, "evidence", "ev-1")
        show = _show_of(self.con)
        self.cid = seed_campaign(self.con)
        self.pid = _window(self.con, self.cid, show)
        self.pid2 = _window(self.con, self.cid, show, "2026-03-05", "2026-03-20")
        campaigns.save_placement_evidence(self.con, self.cid, self.pid, self.ref, _rev(self.con, self.cid))
        _status(self.con, self.cid, "archived")

    def tearDown(self):
        self.con.close()

    def _prev(self, pid):
        return self.con.execute("SELECT revision FROM expected_placements WHERE id=?", (pid,)).fetchone()[0]

    def _body(self, action, crev=0, frev=0, trev=0):
        to = self.pid2 if action == "move" else self.pid
        return {"action": action, "ref": self.ref, "from_target_id": self.pid, "to_target_id": to,
                "expected_campaign_revision": _rev(self.con, self.cid) + crev,
                "expected_from_revision": self._prev(self.pid) + frev,
                "expected_to_revision": self._prev(to) + trev}

    def test_stale_allocation_gets_the_conflict_reply(self):
        for action in ("retain", "reference", "move"):
            for stale in ({"crev": -1}, {"frev": -1}) + (({"trev": -1},) if action == "move" else ()):
                rev = _rev(self.con, self.cid)
                out = campaigns.allocate_placement_evidence(self.con, self.cid, self._body(action, **stale))
                self.assertIn("conflict", out, (action, stale))
                self.assertEqual(out["conflict"]["current"]["campaign_revision"], rev)
                self.assertEqual(_rev(self.con, self.cid), rev, "nothing was written")

    def test_current_allocation_gets_campaign_archived(self):
        for action in ("retain", "reference", "move"):
            with self.assertRaises(ops.Archived, msg=action):
                campaigns.allocate_placement_evidence(self.con, self.cid, self._body(action))

    def test_stale_freeze_gets_the_conflict(self):
        pins = campaigns.build_customer_draft(self.con, self.cid)["pins"]
        with self.assertRaises(ops.Conflict):
            campaigns.freeze_customer_report(self.con, self.cid, _rev(self.con, self.cid) - 1, pins)
        with self.assertRaises(ops.Conflict):
            campaigns.freeze_customer_report(self.con, self.cid, _rev(self.con, self.cid),
                                             {**pins, "attachments": {}})
        with self.assertRaises(ops.Archived):
            campaigns.freeze_customer_report(self.con, self.cid, _rev(self.con, self.cid), pins)

    def test_stale_placement_edit_gets_the_conflict(self):
        body = {"placement": {"expected_count": 2}, "expected_campaign_revision": _rev(self.con, self.cid)}
        with self.assertRaises(ops.Conflict):
            campaigns.save_customer_placement(self.con, self.cid, body, self.pid, self._prev(self.pid) - 1)
        with self.assertRaises(ops.Archived):
            campaigns.save_customer_placement(self.con, self.cid, body, self.pid, self._prev(self.pid))


class ResearchArchivedTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        seed_episode(self.con, show="Casefile")
        add_candidate(self.con, "ev-1", "ep-1")
        self.ref = citations.current_source_ref(self.con, "evidence", "ev-1")
        self.show = _show_of(self.con)
        self.cid = campaigns.save_customer_campaign(self.con, {"campaign": {
            "name": "Research", "mode": "research"}})["campaign"]["id"]
        self.tid = campaigns.create_target(self.con, self.cid, "show_window", show_id=self.show)["id"]
        campaigns.attach_target_evidence(self.con, self.cid, self.tid, self.ref, _rev(self.con, self.cid))
        _status(self.con, self.cid, "archived")

    def tearDown(self):
        self.con.close()

    def test_target_writes_are_refused(self):
        rev = _rev(self.con, self.cid)
        trev = self.con.execute("SELECT revision FROM campaign_targets WHERE id=?", (self.tid,)).fetchone()[0]
        calls = [
            lambda: campaigns.create_target(self.con, self.cid, "show_window", show_id=self.show),
            lambda: campaigns.update_target_note(self.con, self.cid, self.tid, "note", trev),
            lambda: campaigns.delete_target(self.con, self.cid, self.tid),
            lambda: campaigns.detach_target_evidence(self.con, self.cid, self.tid, self.ref, rev),
            lambda: campaigns.attach_target_evidence(self.con, self.cid, self.tid, self.ref, rev),
        ]
        for call in calls:
            with self.assertRaises(ops.Archived):
                call()
        self.assertEqual(_rev(self.con, self.cid), rev)
        self.assertEqual(len(campaigns.list_targets(self.con, self.cid)), 1)

    def test_allowed_actions_and_restore(self):
        self.assertEqual(campaigns.build_customer_draft(self.con, self.cid)["allowed_actions"], ARCHIVED)
        _status(self.con, self.cid, "active")
        trev = self.con.execute("SELECT revision FROM campaign_targets WHERE id=?", (self.tid,)).fetchone()[0]
        campaigns.update_target_note(self.con, self.cid, self.tid, "note", trev)
        campaigns.create_target(self.con, self.cid, "show_window", show_id=self.show)


class LocalArchivedRoutesTest(_cui.LocalRoutesTest):
    """scripts/app_server.py: 409 CAMPAIGN_ARCHIVED, reports readable, Restore works."""

    PORT = 18798
    HOST = f"127.0.0.1:{PORT}"

    def setUp(self):
        super().setUp()
        draft = campaigns.build_customer_draft(self.con, self.cid)
        self.report_no = campaigns.freeze_customer_report(
            self.con, self.cid, _rev(self.con, self.cid), draft["pins"])["report_no"]
        _status(self.con, self.cid, "archived")
        self.con.commit()

    def test_list_detail_and_draft_carry_names(self):
        pass

    def test_archive_and_restore_through_the_route(self):
        pass

    def test_archived_campaign_refuses_writes_and_restore_reopens_it(self):
        base = f"/api/v1/customer/campaigns/{self.cid}"
        status, data = self._req("GET", base)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["campaign"]["allowed_actions"], ARCHIVED)
        status, data = self._req("POST", base + "/placements", _new_placement(self.con, self.cid, self.show))
        self.assertEqual(status, 409, data)
        self.assertEqual(data["error"]["code"], "CAMPAIGN_ARCHIVED")
        self.assertEqual(data["error"]["message"], MESSAGE)
        status, data = self._req("PUT", base, {"campaign": {"name": "x"}, "expected_revision": _rev(self.con, self.cid)})
        self.assertEqual(status, 409, data)
        self.assertEqual(_placements(self.con, self.cid), 1)
        for path in ("/reports", f"/reports/{self.report_no}"):
            status, data = self._req("GET", base + path)
            self.assertEqual(status, 200, data)
        status, data = self._req("PUT", base, {"campaign": {"status": "active"}, "expected_revision": _rev(self.con, self.cid)})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["campaign"]["allowed_actions"], ACTIVE)
        status, data = self._req("POST", base + "/placements", _new_placement(self.con, self.cid, self.show))
        self.assertEqual(status, 200, data)
        self.assertEqual(_placements(self.con, self.cid), 2)


class HostedArchivedRoutesTest(unittest.TestCase):
    """The hosted customer routes refuse the same writes the same way."""

    def setUp(self):
        from tests.test_backend_http import HostedHttpTest
        self._harness = HostedHttpTest
        HostedHttpTest.setUp(self)
        self.con = sqlite3.connect(self.db_path, isolation_level=None)
        self.con.row_factory = sqlite3.Row
        seed_episode(self.con, ep_id="ep-h", show="Casefile", version_no=99)
        self.cid = seed_campaign(self.con, brand="kachava")
        self.show = _show_of(self.con, "ep-h")
        _window(self.con, self.cid, self.show, "2026-03-01", "2026-03-31")
        draft = campaigns.build_customer_draft(self.con, self.cid)
        self.report_no = campaigns.freeze_customer_report(
            self.con, self.cid, _rev(self.con, self.cid), draft["pins"])["report_no"]
        _status(self.con, self.cid, "archived")

    def tearDown(self):
        self.con.close()
        self._harness.tearDown(self)

    def _call(self, *args, **kwargs):
        return self._harness._call(self, *args, **kwargs)

    def test_archived_campaign_refuses_writes_and_restore_reopens_it(self):
        base = f"/api/v1/customer/campaigns/{self.cid}"
        status, _h, data = self._call(base)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["campaign"]["allowed_actions"], ARCHIVED)
        status, _h, data = self._call(base + "/placements", "POST", _new_placement(self.con, self.cid, self.show))
        self.assertEqual(status, 409, data)
        self.assertEqual(data["error"]["code"], "CAMPAIGN_ARCHIVED")
        self.assertEqual(data["error"]["message"], MESSAGE)
        status, _h, data = self._call(base, "PUT", {"campaign": {"name": "x"}, "expected_revision": _rev(self.con, self.cid)})
        self.assertEqual(status, 409, data)
        self.assertEqual(_placements(self.con, self.cid), 1)
        for path in ("/reports", f"/reports/{self.report_no}"):
            status, _h, data = self._call(base + path)
            self.assertEqual(status, 200, data)
        status, _h, data = self._call(base, "PUT", {"campaign": {"status": "active"}, "expected_revision": _rev(self.con, self.cid)})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["campaign"]["allowed_actions"], ACTIVE)
        status, _h, data = self._call(base + "/placements", "POST", _new_placement(self.con, self.cid, self.show))
        self.assertEqual(status, 200, data)
        self.assertEqual(_placements(self.con, self.cid), 2)


if __name__ == "__main__":
    unittest.main()
