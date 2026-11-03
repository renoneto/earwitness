"""K07 allocation/binding/freeze regression tests (red-capable).

Placement binding (eligible vs citation_only), proof-qualified
attach/move/reference with atomic conflict DTOs, allocator credit caps
and double-count reporting, one-transaction freeze with seal, and
digest-gated stored bytes. Fixtures are synthetic; no corpus is read.
"""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import unittest

from earwitness.app import access as _access
from earwitness.app import briefs as _briefs
from earwitness.app import campaigns as _camp
from earwitness.app import db as _db
from earwitness.app import ops as _ops
from earwitness.app import citations
from tests import backend_fixtures


def _con():
    con = backend_fixtures.make_memory_workspace()
    backend_fixtures.publish_sources(con)
    now = _db.utcnow()
    con.execute(
        "INSERT OR IGNORE INTO customer_show_identities (id, workspace_id,"
        " display_title, authority_key, authority_basis, unresolved,"
        " created_at) VALUES ('cs-a', 'local', 'Show A', 'fa', 'feed', 0, ?)",
        (now,))
    con.execute(
        "INSERT OR IGNORE INTO customer_episode_shows (workspace_id,"
        " episode_id, show_identity_id, basis, created_at) VALUES ('local',"
        " 'e1', 'cs-a', 'feed', ?)", (now,))
    con.commit()
    return con


def _brief(con):
    return _briefs.create_brief(
        con, {"title": "Provided brief", "provided_text": "Say lumen."})


def _supplied(con, brief):
    return _camp.save_customer_campaign(
        con, {"campaign": {"name": "Supplied work",
                           "mode": "supplied_requirements",
                           "status": "active", "brand": "lumen",
                           "source_query": None, "message": None,
                           "offer": None, "notes": "",
                           "requirement_provenance": {
                               "brief_ref": {"kind": "supplied_brief",
                                             "brief_id": brief["brief_id"],
                                             "version": 1}}}})


def _placement(con, cid, crev, expected_count=1, binding="eligible",
               episode_id="e1"):
    body = {"placement": {
        "scope": "episode", "show_id": None, "episode_id": episode_id,
        "date_from": None, "date_to": None, "timezone": "UTC",
        "expected_count": expected_count, "binding": binding,
        "requirements": {}, "provenance": {}},
        "expected_campaign_revision": crev}
    # Supplied placements require a registered brief ref.
    brief = _brief(con)
    body["placement"]["provenance"] = {
        "brief_ref": {"kind": "supplied_brief",
                      "brief_id": brief["brief_id"], "version": 1}}
    return _camp.save_customer_placement(con, cid, body, None, None, crev)


def _revs(con, cid):
    camp = con.execute("SELECT revision FROM campaigns WHERE id=?",
                       (cid,)).fetchone()["revision"]
    units = {r["id"]: r["revision"] for r in con.execute(
        "SELECT id, revision FROM expected_placements"
        " WHERE workspace_id='local' AND campaign_id=?", (cid,)).fetchall()}
    units.update({r["id"]: r["revision"] for r in con.execute(
        "SELECT id, revision FROM campaign_targets"
        " WHERE workspace_id='local' AND campaign_id=?", (cid,)).fetchall()})
    return camp, units


class AllocationActionTest(unittest.TestCase):
    def _setup(self, con):
        brief = _brief(con)
        camp = _supplied(con, brief)["campaign"]
        first = _placement(con, camp["id"], camp["revision"])["placement"]
        camp = _camp.campaign_projection(
            con, dict(con.execute("SELECT * FROM campaigns WHERE id=?",
                                  (camp["id"],)).fetchone()))
        second = _placement(con, camp["id"],
                            camp["revision"])["placement"]
        return camp, first, second

    def _act(self, con, cid, action, ref, frm, to):
        camp_rev, units = _revs(con, cid)
        return _camp.allocate_placement_evidence(
            con, cid,
            {"action": action, "ref": ref,
             "from_target_id": frm, "to_target_id": to,
             "expected_campaign_revision": camp_rev,
             "expected_from_revision": units[frm],
             "expected_to_revision": units[to]})

    def test_retain_move_reference_round_trip(self):
        con = _con()
        try:
            camp, first, second = self._setup(con)
            ref = backend_fixtures.current_source_ref(con, "evidence", "fixture-ad")
            camp_rev, _units = _revs(con, camp["id"])
            _camp.save_placement_evidence(con, camp["id"], first["id"],
                                          ref, camp_rev, "owner")
            camp_rev, units = _revs(con, camp["id"])
            kept = self._act(con, camp["id"], "retain", ref,
                             first["id"], first["id"])
            self.assertEqual(kept["binding"], "eligible")
            self.assertTrue(kept["credit_eligible"])
            # Retain writes nothing: displayed revisions still current.
            camp_rev2, _units2 = _revs(con, camp["id"])
            self.assertEqual(camp_rev2, camp_rev)
            cited = self._act(con, camp["id"], "reference", ref,
                              first["id"], first["id"])
            self.assertEqual(cited["binding"], "citation_only")
            self.assertFalse(cited["credit_eligible"])
            moved = self._act(con, camp["id"], "move", ref,
                              first["id"], second["id"])
            self.assertEqual(moved["to_target_id"], second["id"])
            self.assertEqual(moved["binding"], "citation_only")
            self.assertEqual(
                _camp.placement_attachments(con, camp["id"], first["id"]),
                [])
            self.assertEqual(
                _camp.placement_attachments(con, camp["id"], second["id"]),
                [ref])
        finally:
            con.close()

    def test_unqualified_ref_writes_nothing(self):
        con = _con()
        try:
            camp, first, _second = self._setup(con)
            camp_rev, units = _revs(con, camp["id"])
            with self.assertRaises(citations.RevisionUnavailable):
                _camp.allocate_placement_evidence(
                    con, camp["id"],
                    {"action": "retain", "ref": "ev:ghost@v1",
                     "from_target_id": first["id"],
                     "to_target_id": first["id"],
                     "expected_campaign_revision": camp_rev,
                     "expected_from_revision": units[first["id"]],
                     "expected_to_revision": units[first["id"]]})
            with self.assertRaises(_ops.BadInput):
                _camp.allocate_placement_evidence(
                    con, camp["id"],
                    {"action": "retain", "ref": "not-a-ref",
                     "from_target_id": first["id"],
                     "to_target_id": first["id"],
                     "expected_campaign_revision": camp_rev,
                     "expected_from_revision": units[first["id"]],
                     "expected_to_revision": units[first["id"]]})
            with self.assertRaises(_ops.BadInput):
                _camp.allocate_placement_evidence(
                    con, camp["id"],
                    {"action": "archive", "ref": backend_fixtures.current_source_ref(con, "evidence", "fixture-ad"),
                     "from_target_id": first["id"],
                     "to_target_id": first["id"],
                     "expected_campaign_revision": camp_rev,
                     "expected_from_revision": units[first["id"]],
                     "expected_to_revision": units[first["id"]]})
            self.assertEqual(
                _camp.placement_attachments(con, camp["id"], first["id"]),
                [])
            row = con.execute("SELECT revision FROM campaigns WHERE id=?",
                              (camp["id"],)).fetchone()
            self.assertEqual(row["revision"], camp_rev)
        finally:
            con.close()

    def test_stale_basis_returns_conflict_without_writes(self):
        con = _con()
        try:
            camp, first, second = self._setup(con)
            ref = backend_fixtures.current_source_ref(con, "evidence", "fixture-ad")
            camp_rev, units = _revs(con, camp["id"])
            _camp.save_placement_evidence(con, camp["id"], first["id"],
                                          ref, camp_rev, "owner")
            camp_rev, units = _revs(con, camp["id"])
            out = _camp.allocate_placement_evidence(
                con, camp["id"],
                {"action": "move", "ref": ref,
                 "from_target_id": first["id"],
                 "to_target_id": second["id"],
                 "expected_campaign_revision": 999,
                 "expected_from_revision": units[first["id"]],
                 "expected_to_revision": units[second["id"]]})
            conflict = out["conflict"]
            self.assertEqual(conflict["action"], "move")
            self.assertEqual(conflict["ref"], ref)
            self.assertEqual(conflict["current"]["campaign_revision"],
                             camp_rev)
            self.assertEqual(conflict["allowed_operations"],
                             ["retain", "move", "reference"])
            self.assertTrue(
                conflict["units"]["from"]["bound"])
            # Nothing moved.
            self.assertEqual(
                _camp.placement_attachments(con, camp["id"], first["id"]),
                [ref])
            self.assertEqual(
                _camp.placement_attachments(con, camp["id"], second["id"]),
                [])
            row = con.execute("SELECT revision FROM campaigns WHERE id=?",
                              (camp["id"],)).fetchone()
            self.assertEqual(row["revision"], camp_rev)
        finally:
            con.close()

    def test_move_missing_source_writes_nothing(self):
        con = _con()
        try:
            camp, first, second = self._setup(con)
            camp_rev, units = _revs(con, camp["id"])
            with self.assertRaises(_ops.BadInput):
                _camp.allocate_placement_evidence(
                    con, camp["id"],
                    {"action": "move", "ref": backend_fixtures.current_source_ref(con, "evidence", "fixture-ad"),
                     "from_target_id": first["id"],
                     "to_target_id": second["id"],
                     "expected_campaign_revision": camp_rev,
                     "expected_from_revision": units[first["id"]],
                     "expected_to_revision": units[second["id"]]})
            row = con.execute("SELECT revision FROM campaigns WHERE id=?",
                              (camp["id"],)).fetchone()
            self.assertEqual(row["revision"], camp_rev)
        finally:
            con.close()


def _occ(oid, **over):
    occ = {"id": oid, "correspondence": "confirmed", "ambiguous": False,
           "review_status": "reviewed_ad", "advertiser_confirmed": True,
           "fields": {}, "verified_fields": [],
           "human_verified_copy": False, "attachment_ref": oid}
    occ.update(over)
    return occ


def _placement_dict(pid, expected=1):
    return {"id": pid, "expected_count": expected, "requirements": {},
            "scope": "show_window", "revision": 1}


class AllocatorTest(unittest.TestCase):
    def test_expected_count_caps_credits(self):
        occs = [_occ("ev:a@v1"), _occ("ev:b@v1")]
        out = _camp.allocate_campaign(
            [_placement_dict("p1", expected=1)], occs,
            {"state": "complete"}, "2026-01-01T00:00:00+00:00", None, None,
            {"p1": occs}, {})
        ev = out["evaluations"]["p1"]
        self.assertEqual(len(out["used"]), 1)
        self.assertEqual(len(ev["eligible_ids"]), 2)

    def test_double_count_reported_not_silent(self):
        shared = _occ("ev:shared@v1")
        out = _camp.allocate_campaign(
            [_placement_dict("p1"), _placement_dict("p2")],
            [shared],
            {"state": "complete"}, "2026-01-01T00:00:00+00:00", None, None,
            {"p1": [shared], "p2": [shared]}, {})
        second = out["evaluations"]["p2"]
        self.assertEqual(out["used"], {"ev:shared@v1": "p1"})
        self.assertEqual(second["eligible_ids"], [])
        self.assertEqual(second["allocated_elsewhere"], ["ev:shared@v1"])

    def test_ineligible_occurrence_never_credits(self):
        bad = _occ("ev:bad@v1", review_status="finding")
        out = _camp.allocate_campaign(
            [_placement_dict("p1")], [bad], {"state": "complete"},
            "2026-01-01T00:00:00+00:00", None, None, {"p1": [bad]}, {})
        ev = out["evaluations"]["p1"]
        self.assertEqual(out["used"], {})
        self.assertEqual(ev["eligible_ids"], [])


class BindingTest(unittest.TestCase):
    def test_citation_only_never_allocates(self):
        con = _con()
        try:
            brief = _brief(con)
            camp = _supplied(con, brief)["campaign"]
            planned = _placement(
                con, camp["id"], camp["revision"],
                binding="citation_only")["placement"]
            self.assertEqual(planned["binding"], "citation_only")
            draft = _camp.build_customer_draft(con, camp["id"])
            # Outside the six-outcome evaluations map: a binding
            # label, never an evaluated outcome.
            self.assertNotIn(planned["id"],
                             draft["allocation"]["evaluations"])
            cited = draft["allocation"]["citations"][planned["id"]]
            self.assertEqual(cited["binding"], "citation_only")
            self.assertNotIn("outcome", cited)
            self.assertEqual(cited["allocated_ids"], [])
        finally:
            con.close()

    def test_binding_change_marks_allocations_stale(self):
        con = _con()
        try:
            brief = _brief(con)
            camp = _supplied(con, brief)["campaign"]
            placed = _placement(con, camp["id"],
                                camp["revision"])["placement"]
            fresh = _camp.placement_allocation_state(con, placed["id"])
            self.assertEqual(fresh["allocation_state"], "fresh")
            _camp.freeze_evaluation(
                con, camp["id"],
                {"evaluations": {placed["id"]: {"outcome": "supported",
                                                "reasons": [],
                                                "eligible_ids": []}}},
                {"campaign_revision": camp["revision"]}, "owner")
            current = _camp.campaign_projection(
                con, dict(con.execute("SELECT * FROM campaigns WHERE id=?",
                                      (camp["id"],)).fetchone()))
            edited = _camp.save_customer_placement(
                con, camp["id"], {"placement": {"binding": "citation_only"}},
                placed["id"], placed["revision"],
                current["revision"])
            self.assertEqual(edited["allocation_state"], "stale")
            self.assertEqual(edited["placement"]["binding"], "citation_only")
        finally:
            con.close()

    def test_missing_binding_rejected(self):
        con = _con()
        try:
            brief = _brief(con)
            camp = _supplied(con, brief)["campaign"]
            body = {"placement": {
                "scope": "episode", "episode_id": "e1",
                "expected_count": 1, "requirements": {},
                "provenance": {"brief_ref": {"kind": "supplied_brief",
                                             "brief_id": brief["brief_id"],
                                             "version": 1}}},
                "expected_campaign_revision": camp["revision"]}
            with self.assertRaises(_ops.BadInput):
                _camp.save_customer_placement(con, camp["id"], body, None,
                                              None, camp["revision"])
        finally:
            con.close()


class FreezeSealTest(unittest.TestCase):
    def test_freeze_is_atomic_with_verifiable_digest(self):
        con = _con()
        try:
            brief = _brief(con)
            camp = _supplied(con, brief)["campaign"]
            draft = _camp.build_customer_draft(con, camp["id"])
            frozen = _camp.freeze_customer_report(
                con, camp["id"], camp["revision"], draft["pins"], "owner")
            self.assertTrue(frozen["freeze_digest"])
            recomputed = hashlib.sha256(json.dumps(
                {"campaign_id": camp["id"],
                 "report_no": frozen["report_no"],
                 "digest": frozen["digest"], "pins": draft["pins"],
                 "aggregate_rev": frozen["aggregate_rev"],
                 "actor": "owner",
                 "captured_at": con.execute(
                     "SELECT created_at FROM campaign_reports"
                     " WHERE campaign_id=? AND report_no=?",
                     (camp["id"], frozen["report_no"])).fetchone()
                 ["created_at"]},
                sort_keys=True).encode()).hexdigest()
            self.assertEqual(recomputed, frozen["freeze_digest"])
            body, digest = _camp.get_customer_report_html(
                con, camp["id"], frozen["report_no"])
            self.assertEqual(
                hashlib.sha256(body.encode()).hexdigest(), digest)
        finally:
            con.close()

    def test_stale_freeze_writes_nothing(self):
        con = _con()
        try:
            brief = _brief(con)
            camp = _supplied(con, brief)["campaign"]
            before = con.execute(
                "SELECT COUNT(*) FROM campaign_reports").fetchone()[0]
            with self.assertRaises(_ops.Conflict):
                _camp.freeze_customer_report(con, camp["id"], 999, {},
                                             "owner")
            after = con.execute(
                "SELECT COUNT(*) FROM campaign_reports").fetchone()[0]
            self.assertEqual(before, after)
        finally:
            con.close()

    def test_corrupt_bytes_fail_digest(self):
        con = _con()
        try:
            brief = _brief(con)
            camp = _supplied(con, brief)["campaign"]
            draft = _camp.build_customer_draft(con, camp["id"])
            frozen = _camp.freeze_customer_report(
                con, camp["id"], camp["revision"], draft["pins"], "owner")
            con.execute("UPDATE exports SET payload='tampered' WHERE id=?",
                        (frozen["export_id"],))
            with self.assertRaises(_ops.BadInput):
                _camp.get_customer_report_html(con, camp["id"],
                                               frozen["report_no"])
        finally:
            con.close()


class BrowserPinTest(unittest.TestCase):
    def _displayed(self, con, ctx=None):
        camp = _supplied(con, _brief(con))["campaign"]
        placed = _placement(con, camp["id"], camp["revision"])
        pid = placed["placement"]["id"]
        revision = _revs(con, camp["id"])[0]
        draft = _camp.build_customer_draft(con, camp["id"], ctx=ctx)
        result = subprocess.run(
            ["node", "-e", 'process.stdout.write(JSON.stringify(JSON.parse(require("fs").readFileSync(0,"utf8"))))'],
            input=json.dumps(draft["pins"]), text=True, capture_output=True, check=True)
        return camp["id"], pid, revision, draft["pins"], json.loads(result.stdout)

    def test_browser_roundtrip_freezes_and_changed_basis_still_writes_nothing(self):
        con = _con()
        self.addCleanup(con.close)
        cid, pid, revision, displayed, browser = self._displayed(con)
        self.assertIsInstance(displayed["source_scopes"][pid]["bounds"]["start_s"], float)
        self.assertIs(type(browser["source_scopes"][pid]["bounds"]["start_s"]), int)
        frozen = _camp.freeze_customer_report(con, cid, revision, browser, "owner")
        html, digest = _camp.get_customer_report_html(con, cid, frozen["report_no"])
        requests = []
        changed_number = copy.deepcopy(browser)
        changed_number["source_scopes"][pid]["bounds"]["end_s"] += 1
        requests.append(("number", revision, changed_number))
        false_zero = copy.deepcopy(browser)
        self.assertEqual(false_zero["source_scopes"][pid]["bounds"]["start_s"], 0)
        false_zero["source_scopes"][pid]["bounds"]["start_s"] = False
        requests.append(("false-versus-zero", revision, false_zero))
        true_one = copy.deepcopy(browser)
        self.assertEqual(true_one["placements"][pid], 1)
        true_one["placements"][pid] = True
        requests.append(("true-versus-one", revision, true_one))
        missing = copy.deepcopy(browser)
        del missing["coverage_sources"]
        requests.append(("missing-key", revision, missing))
        extra = copy.deepcopy(browser)
        extra["unexpected"] = None
        requests.append(("extra-key", revision, extra))
        changed_list = copy.deepcopy(browser)
        changed_list["attachments"][pid].append(browser["coverage_sources"]["e1"])
        requests.append(("list", revision, changed_list))
        changed_source = copy.deepcopy(browser)
        changed_source["coverage_sources"]["e1"] = "sr1:" + "0" * 32
        requests.append(("source", revision, changed_source))
        requests.append(("stale-revision", revision - 1, browser))
        for name, basis, pins in requests:
            with self.subTest(name=name):
                before = list(con.iterdump())
                with self.assertRaises(_ops.Conflict):
                    _camp.freeze_customer_report(con, cid, basis, pins, "owner")
                self.assertEqual(list(con.iterdump()), before)
        episode_revision = con.execute("SELECT revision FROM episodes WHERE id='e1'").fetchone()[0]
        _ops.set_episode_media(con, "e1", {"audio_url": "https://example.test/new-source.mp3"},
                               episode_revision, "owner")
        before = list(con.iterdump())
        with self.assertRaises(_ops.Conflict):
            _camp.freeze_customer_report(con, cid, revision, browser, "owner")
        self.assertEqual(list(con.iterdump()), before)
        self.assertEqual(_camp.get_customer_report_html(con, cid, frozen["report_no"]), (html, digest))

    def test_actual_source_policy_loss_denies_freeze_and_preserves_stored_bytes(self):
        con = _con()
        self.addCleanup(con.close)
        ctx = _access.AccessContext.create(
            principal_id="owner", principal_kind="owner", workspace_id="local",
            credential_kind="local_operator")
        cid, _pid, revision, _displayed, browser = self._displayed(con, ctx)
        frozen = _camp.freeze_customer_report(con, cid, revision, browser, "owner", ctx)
        source_key = con.execute(
            "SELECT source_key FROM source_versions WHERE id="
            "(SELECT transcript_version_id FROM episodes WHERE id='e1')").fetchone()[0]
        con.execute("INSERT OR REPLACE INTO source_content_policy"
                    " (workspace_id,source_key,content_category,agent_disclosure,updated_at)"
                    " VALUES ('local',?,'metadata','deny',?)", (source_key, _db.utcnow()))
        con.commit()
        before = list(con.iterdump())
        with self.assertRaises(_access.AuthorizationError):
            _camp.freeze_customer_report(con, cid, revision, browser, "owner", ctx)
        with self.assertRaises(_access.AuthorizationError):
            _camp.get_customer_report_html(con, cid, frozen["report_no"], ctx)
        self.assertEqual(list(con.iterdump()), before)

    def test_json_pin_boolean_types_and_list_order_remain_distinct(self):
        for current, expected in (
            ({"flag": True}, {"flag": 1}),
            ({"flag": False}, {"flag": 0.0}),
            ({"items": [True, 0]}, {"items": [1.0, False]}),
            ({"items": [0, 1]}, {"items": [1, 0]}),
            ({"revision": 1}, {"revision": "1"}),
        ):
            with self.subTest(current=current, expected=expected):
                self.assertFalse(_camp._pins_match(current, expected))


if __name__ == "__main__":
    unittest.main()
