"""A converted schema-7 tracker's first check reports only real changes.

Schema-7 baselines recorded each member's transition as revision
components with ev: refs; schema-8 compares source publications. The
conversion translates the baseline, so unchanged data emits nothing and a
real change (before or after conversion) still emits.
"""
from __future__ import annotations

import json
import unittest

from earwitness.app import citations, conversion, customer, db, ops
from earwitness.app import customer_contracts as contracts
from tests import backend_fixtures

V1_QUERY = {"version": 1, "mode": "ads", "q": "", "brand": "Lumen", "show": "", "from": "",
            "to": "", "status": "finding", "audio": "", "sort": "newest", "page": 1, "ids": None}


def _v7_transition(con, evidence_id):
    """The schema-7 components, read straight from the rows."""
    ev = con.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
    ep = con.execute("SELECT revision, transcript_version_id FROM episodes WHERE id=?",
                     (ev["episode_id"],)).fetchone()
    review = con.execute("SELECT revision FROM reviews WHERE evidence_id=?", (evidence_id,)).fetchone()
    return {"episode_revision": ep["revision"], "transcript_version": ep["transcript_version_id"],
            "evidence_revision": ev["revision"], "source_version": ev["source_version_id"],
            "assoc_revision": ev["assoc_revision"], "review_revision": review["revision"]}


def _current_members(con):
    query = contracts.upcast_query_v1(V1_QUERY)[0]
    return customer._alert_snapshot(con, query, None, ["new_finding"])["members"]


def _legacy_alert(con, *, transition=None, query=V1_QUERY):
    """Alert 1 as schema 7 stored it: v1 query, legacy member format for
    every current match (fixture-ad gets the given transition)."""
    members = {}
    for logical, now in _current_members(con).items():
        evidence_id = logical[len("finding:"):]
        ev = con.execute("SELECT source_version_id FROM evidence WHERE id=?", (evidence_id,)).fetchone()
        members[logical] = {"kind": "new_finding", "ref": f"ev:{evidence_id}@v{ev[0]}",
                            "transition": _v7_transition(con, evidence_id),
                            "offer": now["offer"], "episode_id": now["episode_id"]}
    assert "finding:fixture-ad" in members
    if transition is not None:
        members["finding:fixture-ad"]["transition"] = transition
    baseline = {"members": members, "episode_text": {},
                "revision": "vr2-legacy", "coverage": {}}
    con.execute("UPDATE customer_alerts SET query_json=?, event_types=?, baseline_json=?,"
                " origin_route='' WHERE id=1",
                (json.dumps(query), json.dumps(["new_finding", "offer_difference"]),
                 json.dumps(baseline, sort_keys=True)))


def _add_finding(con, evidence_id, episode_id="e2"):
    sv = con.execute("SELECT source_version_id FROM evidence WHERE id='fixture-ad'").fetchone()[0]
    con.execute(
        "INSERT INTO evidence (id, source, source_version_id, episode_id, orig_start_s, orig_end_s,"
        " excerpt, attrs, revision, created_at, transcript_version_id, source_record_key,"
        " assoc_revision, record_kind) VALUES (?, 'detector', ?, ?, 10.0, 20.0,"
        " 'Lumen keeps the lights on here', '{}', 1, ?, (SELECT transcript_version_id FROM"
        " episodes WHERE id=?), ?, 0, 'candidate')",
        (evidence_id, sv, episode_id, db.utcnow(), episode_id, evidence_id))
    con.execute("INSERT INTO reviews (evidence_id, judgment, revision, updated_at)"
                " VALUES (?, 'finding', 1, ?)", (evidence_id, db.utcnow()))
    con.execute("INSERT INTO brand_assocs (evidence_id, brand, basis, origin, revision, updated_at)"
                " VALUES (?, 'Lumen', 'spoken', 'test', 1, ?)", (evidence_id, db.utcnow()))
    citations.publish_episode_sources(con, episode_id, "test")


class ConvertedTrackerFirstCheckTest(unittest.TestCase):
    def setUp(self):
        self.con = backend_fixtures.make_memory_workspace()
        _legacy_alert(self.con)

    def tearDown(self):
        self.con.close()

    def _baseline(self):
        return json.loads(self.con.execute(
            "SELECT baseline_json FROM customer_alerts WHERE id=1").fetchone()[0])

    def test_unchanged_data_emits_no_event_on_first_check(self):
        out = conversion.apply(self.con)
        row = self.con.execute("SELECT query_json FROM customer_alerts WHERE id=1").fetchone()
        self.assertEqual(json.loads(row[0])["version"], 2)
        check = customer.check_alert(self.con, 1)
        self.assertTrue(check["ok"])
        self.assertEqual(check["emitted"], 0, check.get("emitted_events"))
        current = backend_fixtures.current_source_ref(self.con, "evidence", "fixture-ad")
        member = self._baseline()["members"]["finding:fixture-ad"]
        self.assertEqual(member["ref"], current)
        self.assertEqual(member["transition"], {"source_ref": current})
        record = next(r for r in out["baseline_translations"] if r["alert_id"] == 1)
        self.assertEqual(record["state"], "translated")
        self.assertEqual(record["adopted"], sorted(_current_members(self.con)))
        self.assertEqual(record["changed_since_baseline"], [])
        lineage = self.con.execute("SELECT notes_json, membership_json FROM customer_conversion_lineage"
                                   " WHERE definition_key='tracker:1'").fetchone()
        self.assertEqual(json.loads(lineage["membership_json"])["baseline_translation"], record)
        self.assertTrue(any("baseline translated" in note for note in json.loads(lineage["notes_json"])))

    def test_translation_keeps_membership_offer_and_is_idempotent(self):
        before = self._baseline()
        conversion.apply(self.con)
        after = self._baseline()
        self.assertEqual(set(after["members"]), set(before["members"]))
        self.assertEqual(after["members"]["finding:fixture-ad"]["offer"],
                         before["members"]["finding:fixture-ad"]["offer"])
        again = conversion.apply(self.con)
        self.assertEqual(again["baseline_translations"], [])
        self.assertEqual(self._baseline(), after)

    def test_change_before_conversion_still_emits(self):
        stale = dict(_v7_transition(self.con, "fixture-ad"), evidence_revision=0)
        _legacy_alert(self.con, transition=stale)
        out = conversion.apply(self.con)
        record = next(r for r in out["baseline_translations"] if r["alert_id"] == 1)
        self.assertEqual(record["changed_since_baseline"], ["finding:fixture-ad"])
        check = customer.check_alert(self.con, 1)
        self.assertEqual(check["emitted"], 1)
        self.assertEqual(check["emitted_events"][0]["logical_identity"], "finding:fixture-ad")
        self.assertEqual(customer.check_alert(self.con, 1)["emitted"], 0)

    def test_new_finding_after_conversion_emits(self):
        conversion.apply(self.con)
        self.assertEqual(customer.check_alert(self.con, 1)["emitted"], 0)
        _add_finding(self.con, "fixture-ad-2")
        check = customer.check_alert(self.con, 1)
        self.assertEqual([e["logical_identity"] for e in check["emitted_events"]],
                         ["finding:fixture-ad-2"])
        self.assertEqual(check["emitted_events"][0]["kind"], "new_finding")

    def test_publication_change_after_conversion_emits(self):
        conversion.apply(self.con)
        before = backend_fixtures.current_source_ref(self.con, "evidence", "fixture-ad")
        self.con.execute("UPDATE evidence SET orig_end_s=19.0, revision=revision+1 WHERE id='fixture-ad'")
        if db.has_history_tables(self.con):
            db.record_evidence_snapshot(self.con, "fixture-ad", "test")
        citations.publish_episode_sources(self.con, "e1", "test")
        self.assertNotEqual(backend_fixtures.current_source_ref(self.con, "evidence", "fixture-ad"), before)
        check = customer.check_alert(self.con, 1)
        self.assertEqual([e["logical_identity"] for e in check["emitted_events"]],
                         ["finding:fixture-ad"])

    def test_check_refuses_untranslated_schema7_baseline(self):
        _legacy_alert(self.con, query=dict(contracts.upcast_query_v1(V1_QUERY)[0], page=1))
        stored = self._baseline()
        with self.assertRaises(ops.BadInput) as raised:
            customer.check_alert(self.con, 1)
        self.assertIn("predates source publications", str(raised.exception))
        self.assertEqual(self._baseline(), stored)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM customer_alert_events").fetchone()[0], 0)



class LegacyAudioUpgradeThenConversionTest(unittest.TestCase):
    """The upgrade's own legacy-audio binding bumps the episode revision;
    a schema-7 baseline taken before the upgrade must not report it."""

    def setUp(self):
        import sqlite3
        import tempfile
        from pathlib import Path
        from unittest import mock
        from tests import test_followup_k_audio as audio
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.cli = audio.load_workspace_cli()
        self.enterContext(mock.patch.object(self.cli, "RECOVERY_DIR", self.tmp / "recovery"))
        self.workspace = self.tmp / "ws"
        self.db_path = backend_fixtures.make_workspace(self.workspace, schema_version=7)
        con = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            con.execute("BEGIN IMMEDIATE")
            audio.add_episode(con, "upload:pilot-acquired", audio.LEGACY_SEGMENTS)
            con.commit()
        finally:
            con.close()
        self.legacy = self.tmp / "legacy-repo"
        target = self.legacy / ".pilot-corpus/acquired/latest.mp3"
        target.parent.mkdir(parents=True)
        target.write_bytes(audio.mp3_bytes(1200, 0))
        self.query = {"version": 1, "mode": "mentions", "q": "quillmark", "brand": "", "show": "",
                      "from": "", "to": "", "status": "", "audio": "", "sort": "newest",
                      "page": 1, "ids": None}

    def _upgrade_with_schema7_baseline(self, revision_shift=0):
        result = self.cli.upgrade_workspace(self.workspace, legacy_audio_root=self.legacy)
        entry = result["legacy_audio"]["registered"][0]
        self.assertEqual(entry["episode_revision_after"], entry["episode_revision_before"] + 1)
        con = db.open_existing(self.db_path, readonly=False)
        self.addCleanup(con.close)
        live = customer._alert_snapshot(con, contracts.upcast_query_v1(self.query)[0], None,
                                        ["new_text"])["members"]
        self.assertTrue(live)
        members = {}
        for logical, now in live.items():
            ep = con.execute("SELECT transcript_version_id FROM episodes WHERE id=?",
                             (now["episode_id"],)).fetchone()
            members[logical] = {"kind": now["kind"], "ref": f"tx:{now['episode_id']}@v{ep[0]}:0",
                                "transition": {"episode_revision": entry["episode_revision_before"]
                                               + revision_shift,
                                               "transcript_version": ep[0]},
                                "offer": now["offer"], "episode_id": now["episode_id"]}
        baseline = {"members": members, "episode_text": {}, "revision": "vr2-legacy", "coverage": {}}
        con.execute("UPDATE customer_alerts SET query_json=?, event_types=?, baseline_json=?,"
                    " origin_route='' WHERE id=1",
                    (json.dumps(self.query), json.dumps(["new_text"]), json.dumps(baseline)))
        return con, sorted(members)

    def test_first_check_after_upgrade_and_conversion_emits_nothing(self):
        con, logicals = self._upgrade_with_schema7_baseline()
        out = conversion.apply(con)
        record = next(r for r in out["baseline_translations"] if r["alert_id"] == 1)
        self.assertEqual(record["adopted"], logicals)
        self.assertEqual(record["legacy_audio_revision_mapped"], logicals)
        check = customer.check_alert(con, 1)
        self.assertEqual(check["emitted"], 0, check.get("emitted_events"))

    def test_other_episode_revision_change_still_emits(self):
        con, logicals = self._upgrade_with_schema7_baseline(revision_shift=-1)
        out = conversion.apply(con)
        record = next(r for r in out["baseline_translations"] if r["alert_id"] == 1)
        self.assertEqual(record["changed_since_baseline"], logicals)
        self.assertEqual(customer.check_alert(con, 1)["emitted"], len(logicals))


if __name__ == "__main__":
    unittest.main()
