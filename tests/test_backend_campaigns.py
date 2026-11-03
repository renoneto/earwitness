"""K06 provided briefs, fixed campaign modes, research targets (red-capable).

B3: brief versions with server-captured bytes/digest, exact
SuppliedBriefRef1 resolution, mode-at-creation with no saved-type
change, research targets with captured source scopes, supplied
placements with explicit counts and supported checks only, and
coherent directory/draft projections with legacy review gating.
"""
from __future__ import annotations

import unittest

from earwitness.app import briefs as _briefs
from earwitness.app import campaigns as _camp
from earwitness.app import citations as _citations
from earwitness.app import customer as _customer
from earwitness.app import db as _db
from earwitness.app import ops as _ops
from tests import backend_fixtures


def _con():
    con = backend_fixtures.make_memory_workspace()
    backend_fixtures.publish_sources(con)
    con.commit()
    return con


def _brief(con, actor="owner"):
    return _briefs.create_brief(
        con, {"title": "Provided brief", "provided_text": "Say lumen."},
        actor)


def _research(con):
    return _camp.save_customer_campaign(
        con, {"campaign": {"name": "Brand research", "mode": "research",
                           "status": "active", "brand": "lumen",
                           "source_query": None, "message": None,
                           "offer": None, "notes": "",
                           "requirement_provenance": None}})


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


class BriefTest(unittest.TestCase):
    def test_create_and_version_round_trip(self):
        con = _con()
        try:
            made = _brief(con)
            self.assertEqual(made["version"], 1)
            self.assertTrue(made["digest"])
            second = _briefs.add_version(
                con, made["brief_id"],
                {"title": "Provided brief v2",
                 "provided_text": "Say lumen clearly."}, 1)
            self.assertEqual(second["version"], 2)
            self.assertNotEqual(second["digest"], made["digest"])
            fetched = _briefs.get_version(con, made["brief_id"], 1)
            self.assertEqual(fetched["digest"], made["digest"])
            listed = _briefs.list_briefs(con, q="provided")
            self.assertEqual(listed["total"], 1)
        finally:
            con.close()

    def test_stale_version_conflicts_without_write(self):
        con = _con()
        try:
            made = _brief(con)
            with self.assertRaises(_ops.Conflict):
                _briefs.add_version(
                    con, made["brief_id"],
                    {"title": "Stale", "provided_text": "x"}, 99)
            self.assertEqual(
                _briefs.get_brief(con, made["brief_id"])["current_version"],
                1)
        finally:
            con.close()

    def test_title_limit_and_no_client_authority(self):
        con = _con()
        try:
            with self.assertRaises(_ops.BadInput):
                _briefs.create_brief(
                    con, {"title": "x" * 121, "provided_text": "ok"})
            with self.assertRaises(_ops.BadInput):
                _briefs.create_brief(
                    con, {"title": "T", "provided_text": "ok",
                          "synthetic": True})
            with self.assertRaises(_ops.BadInput):
                _briefs.create_brief(
                    con, {"title": "T", "provided_text": "ok",
                          "extra": 1})
        finally:
            con.close()

    def test_unregistered_ref_needs_review(self):
        con = _con()
        try:
            with self.assertRaises(_ops.BadInput):
                _briefs.resolve_ref(
                    con, {"kind": "supplied_brief",
                          "brief_id": "brief-ghost", "version": 1})
            with self.assertRaises(_ops.BadInput):
                _briefs.resolve_ref(con, "brief-ghost")
        finally:
            con.close()


class CampaignModeTest(unittest.TestCase):
    def test_create_requires_mode(self):
        con = _con()
        try:
            before = con.execute(
                "SELECT COUNT(*) n FROM campaigns").fetchone()["n"]
            with self.assertRaises(_ops.BadInput):
                _camp.save_customer_campaign(
                    con, {"campaign": {"name": "Typeless"}})
            after = con.execute(
                "SELECT COUNT(*) n FROM campaigns").fetchone()["n"]
            self.assertEqual(before, after)
        finally:
            con.close()

    def test_research_create_has_no_provenance(self):
        con = _con()
        try:
            out = _research(con)
            camp = out["campaign"]
            self.assertEqual(camp["mode"], "research")
            self.assertIsNone(camp["requirement_provenance"]
                              .get("brief_id") if camp["requirement_provenance"]
                              else None)
            with self.assertRaises(_ops.BadInput):
                _camp.save_customer_campaign(
                    con, {"campaign": {"name": "Bad research",
                                       "mode": "research",
                                       "requirement_provenance":
                                       {"brief_ref": {}}}})
        finally:
            con.close()

    def test_supplied_create_resolves_registered_ref(self):
        con = _con()
        try:
            brief = _brief(con)
            with self.assertRaises(_ops.BadInput):
                _camp.save_customer_campaign(
                    con, {"campaign": {"name": "No ref",
                                       "mode": "supplied_requirements"}})
            out = _supplied(con, brief)
            prov = out["campaign"]["requirement_provenance"]
            self.assertEqual(prov["brief_id"], brief["brief_id"])
            self.assertEqual(prov["digest"], brief["digest"])
            self.assertEqual(prov["source_label"], "Provided brief")
        finally:
            con.close()

    def test_saved_mode_change_rejected_without_writes(self):
        con = _con()
        try:
            out = _research(con)
            cid, rev = out["campaign"]["id"], out["campaign"]["revision"]
            with self.assertRaises(_ops.BadInput):
                _camp.save_customer_campaign(
                    con, {"campaign": {"mode": "supplied_requirements",
                                       "notes": "sneaky"}}, cid, rev)
            row = con.execute("SELECT mode, revision, notes FROM campaigns"
                              " WHERE id=?", (cid,)).fetchone()
            self.assertEqual(
                (row["mode"], row["revision"], row["notes"]),
                ("research", rev, ""))
        finally:
            con.close()

    def test_normal_edit_advances_revision(self):
        con = _con()
        try:
            out = _research(con)
            cid, rev = out["campaign"]["id"], out["campaign"]["revision"]
            edited = _camp.save_customer_campaign(
                con, {"campaign": {"notes": "week one"}}, cid, rev)
            self.assertEqual(
                edited["campaign"]["revision"], rev + 1)
            self.assertEqual(edited["campaign"]["notes"], "week one")
            with self.assertRaises(_ops.Conflict):
                _camp.save_customer_campaign(
                    con, {"campaign": {"notes": "stale"}}, cid, rev)
            with self.assertRaises(_ops.MissingPrecondition):
                _camp.save_customer_campaign(
                    con, {"campaign": {"notes": "nope"}}, cid, None)
        finally:
            con.close()

    def test_legacy_campaign_is_read_only(self):
        con = _con()
        try:
            old = _ops.save_campaign(con, {"name": "Legacy"}, None, None,
                                     "owner")
            dto = _camp.campaign_projection(
                con, dict(con.execute("SELECT * FROM campaigns WHERE id=?",
                                      (old["id"],)).fetchone()))
            self.assertIsNone(dto["mode"])
            self.assertEqual(dto["availability"],
                             "legacy_review_required")
            self.assertEqual(dto["allowed_actions"],
                             ["read", "stored_reports"])
            with self.assertRaises(_ops.BadInput):
                _camp.save_customer_campaign(
                    con, {"campaign": {"mode": "research"}}, old["id"], 1)
        finally:
            con.close()


class TargetTest(unittest.TestCase):
    def test_episode_target_captures_source_scope(self):
        con = _con()
        try:
            out = _research(con)
            cid = out["campaign"]["id"]
            target = _camp.create_target(con, cid, "episode",
                                        episode_id="e1")
            scope = target["source_scope"]
            self.assertTrue(scope["source_ref"].startswith("sr1:"))
            self.assertGreater(scope["bounds"]["end_s"],
                               scope["bounds"]["start_s"])
            self.assertEqual(scope["bounds_basis"],
                             "full_effective_source")
        finally:
            con.close()

    def test_target_note_edit_preserves_scope(self):
        con = _con()
        try:
            out = _research(con)
            cid = out["campaign"]["id"]
            target = _camp.create_target(con, cid, "episode",
                                        episode_id="e1")
            edited = _camp.update_target_note(
                con, cid, target["id"], "watch this", 1)
            self.assertEqual(
                edited["target"]["source_scope"],
                target["source_scope"])
        finally:
            con.close()

    def test_show_window_requires_proven_show(self):
        con = _con()
        try:
            out = _research(con)
            cid = out["campaign"]["id"]
            show = _customer.customer_show_for_episode(con, "e1")
            good = _camp.create_target(con, cid, "show_window",
                                       show_id=show["show_identity_id"])
            self.assertEqual(good["scope"], "show_window")
            with self.assertRaises(_ops.BadInput):
                _camp.create_target(con, cid, "show_window",
                                    show_id="cs-ghost")
        finally:
            con.close()

    def test_targets_rejected_on_supplied_campaign(self):
        con = _con()
        try:
            brief = _brief(con)
            out = _supplied(con, brief)
            with self.assertRaises(_ops.BadInput):
                _camp.create_target(con, out["campaign"]["id"], "episode",
                                    episode_id="e1")
        finally:
            con.close()


class PlacementTest(unittest.TestCase):
    def _placement_body(self, brief, **over):
        body = {"placement": {
            "scope": "episode", "show_id": None, "episode_id": "e1",
            "date_from": None, "date_to": None, "timezone": "UTC",
            "expected_count": 1, "binding": "eligible",
            "requirements": {"message": "Say lumen", "offer": None,
                             "code": None, "duration_s": None,
                             "position_s": None},
            "provenance": {"brief_ref": {"kind": "supplied_brief",
                                         "brief_id": brief["brief_id"],
                                         "version": 1}}}}
        for key, value in over.items():
            body["placement"][key] = value
        return body

    def test_supplied_placement_persists_with_provenance(self):
        con = _con()
        try:
            brief = _brief(con)
            out = _supplied(con, brief)
            camp = out["campaign"]
            body = self._placement_body(brief)
            placed = _camp.save_customer_placement(
                con, camp["id"], body, None, None, camp["revision"])
            prov = placed["placement"]["provenance"]
            self.assertEqual(prov["brief_id"], brief["brief_id"])
            self.assertTrue(
                placed["placement"]["source_scope"]["source_ref"]
                .startswith("sr1:"))
        finally:
            con.close()

    def test_missing_count_rejected(self):
        con = _con()
        try:
            brief = _brief(con)
            out = _supplied(con, brief)
            camp = out["campaign"]
            body = self._placement_body(brief, expected_count=None)
            with self.assertRaises(_ops.BadInput):
                _camp.save_customer_placement(
                    con, camp["id"], body, None, None, camp["revision"])
            self.assertEqual(
                con.execute("SELECT COUNT(*) n FROM expected_placements"
                            ).fetchone()["n"], 0)
        finally:
            con.close()

    def test_parked_keys_rejected_without_mutation(self):
        con = _con()
        try:
            brief = _brief(con)
            out = _supplied(con, brief)
            camp = out["campaign"]
            for parked in ({"read_style": None},
                           {"production_timing": "live"}):
                reqs = {"message": None, "offer": None, "code": None,
                        "duration_s": None, "position_s": None}
                reqs.update(parked)
                body = self._placement_body(brief, requirements=reqs)
                with self.assertRaises(_ops.BadInput):
                    _camp.save_customer_placement(
                        con, camp["id"], body, None, None, camp["revision"])
            self.assertEqual(
                con.execute("SELECT COUNT(*) n FROM expected_placements"
                            ).fetchone()["n"], 0)
        finally:
            con.close()

    def test_bad_range_and_foreign_brief_rejected(self):
        con = _con()
        try:
            brief = _brief(con)
            out = _supplied(con, brief)
            camp = out["campaign"]
            body = self._placement_body(
                brief, requirements={"duration_s": {"min": 5, "max": 2}})
            with self.assertRaises(_ops.BadInput):
                _camp.save_customer_placement(
                    con, camp["id"], body, None, None, camp["revision"])
            ghost = self._placement_body(brief)
            ghost["placement"]["provenance"] = {
                "brief_ref": {"kind": "supplied_brief",
                              "brief_id": "brief-ghost", "version": 1}}
            with self.assertRaises(_ops.BadInput):
                _camp.save_customer_placement(
                    con, camp["id"], ghost, None, None, camp["revision"])
        finally:
            con.close()

    def test_stale_parent_revision_conflicts(self):
        con = _con()
        try:
            brief = _brief(con)
            out = _supplied(con, brief)
            camp = out["campaign"]
            body = self._placement_body(brief)
            with self.assertRaises(_ops.Conflict):
                _camp.save_customer_placement(
                    con, camp["id"], body, None, None, 999)
        finally:
            con.close()

    def test_placements_rejected_on_research_campaign(self):
        con = _con()
        try:
            out = _research(con)
            camp = out["campaign"]
            body = {"placement": {"scope": "show_window",
                                  "expected_count": 1, "requirements": {},
                                  "provenance": {}},
                    "expected_campaign_revision": camp["revision"]}
            with self.assertRaises(_ops.BadInput):
                _camp.save_customer_placement(
                    con, camp["id"], body, None, None, camp["revision"])
        finally:
            con.close()

    def test_draft_carries_mode_provenance_targets(self):
        con = _con()
        try:
            brief = _brief(con)
            out = _supplied(con, brief)
            draft = _camp.build_customer_draft(con, out["campaign"]["id"])
            self.assertEqual(draft["mode"], "supplied_requirements")
            self.assertEqual(
                draft["requirement_provenance"]["brief_id"],
                brief["brief_id"])
            self.assertIn("brief_versions", draft["pins"])
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
