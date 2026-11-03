"""Campaign share-pack HTML pilot (stdlib unittest).

Disposable workspace under /tmp; live .earwitness/ is never touched.
Red-first: on pristine HEAD the campaign HTML is a raw <pre> dump with
local #/review/ links, so the readable-markers test fails; after the
exports.py upgrade it passes. Non-campaign exports keep the old layout.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from earwitness.app import briefs, campaigns, citations, db, exports, ops

SEGS = [
    {"start": 0.0, "end": 15.0, "text": "visit crowncoins dot com enter code Crown today"},
    {"start": 15.0, "end": 30.0, "text": "back to editorial content here"},
]


def seed_episode(con, ep="ep-1"):
    con.execute(
        "INSERT OR IGNORE INTO episodes (id, kind, show_title, ep_title, fetch_date, updated_at)"
        " VALUES (?, 'supplied', 'Test Show', ?, '2026-01-02', '2026-01-02T00:00:00+00:00')",
        (ep, f"Test {ep}"),
    )
    cur = con.execute(
        "INSERT INTO source_versions (source_key, version_no, sha256, payload, is_active, created_at)"
        " VALUES (?, 1, 'abc', ?, 1, ?)",
        (f"transcript:{ep}", json.dumps({"segments": SEGS}), db.utcnow()),
    )
    vid = cur.lastrowid
    con.execute("UPDATE episodes SET transcript_version_id=? WHERE id=?", (vid, ep))
    con.commit()
    return vid


def seed_evidence(con, eid="ev-1", ep="ep-1", vid=None, judgment="ad"):
    con.execute(
        "INSERT OR IGNORE INTO evidence (id, source, source_version_id, episode_id,"
        " orig_start_s, orig_end_s, excerpt, attrs, revision, created_at,"
        " transcript_version_id, record_kind)"
        " VALUES (?, 'jev', ?, ?, 0.0, 15.0, 'crowncoins excerpt', '{}', 1, ?, ?, 'candidate')",
        (eid, vid, ep, db.utcnow(), vid),
    )
    con.execute(
        "INSERT INTO brand_assocs (evidence_id, brand, basis, origin, revision, updated_at)"
        " VALUES (?, 'crowncoins', 'confirmed', 'test', 1, ?)",
        (eid, db.utcnow()),
    )
    if judgment:
        con.execute(
            "INSERT OR REPLACE INTO reviews (evidence_id, judgment, eff_start_s, eff_end_s,"
            " notes, revision, updated_at) VALUES (?, ?, 0.0, 15.0, '', 1, ?)",
            (eid, judgment, db.utcnow()),
        )
    con.commit()


class SharePackCase(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="share-pack-"))
        self.con = db.connect(self.ws)
        self.con.row_factory = sqlite3.Row
        db.init_schema(self.con)

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.ws, ignore_errors=True)

    def _campaign(self, name="CrownCoins <share> pack"):
        con = self.con
        vid = seed_episode(con)
        seed_evidence(con, vid=vid)
        citations.publish_episode_sources(con, "ep-1", "fixture")
        brief = briefs.create_brief(con, {"title": "Provided pack brief", "provided_text": "Crowncoins research requirements."})
        ref = {"kind": "supplied_brief", "brief_id": brief["brief_id"], "version": 1}
        camp = campaigns.save_customer_campaign(con, {"campaign": {"name": name,
            "mode": "supplied_requirements", "requirement_provenance": {"brief_ref": ref}}})["campaign"]
        cid = camp["id"]
        rev = ops.campaign_detail(con, cid)["revision"]
        ops.attach_evidence(con, cid, "ev-1", rev, "owner")
        detail = ops.campaign_detail(con, cid)
        ops.save_check(con, {"campaign_id": cid, "campaign_revision": detail["revision"],
                             "check_type": "confirmed_brand", "params": {"brand": "crowncoins"},
                             "evidence_id": "ev-1"}, None, None, "owner")
        placed = campaigns.save_expected_placement(
            con, cid, scope="show_window", show_id="s1", expected_count=1,
            requirements={}, provenance={"brief_ref": ref})
        pid = placed["placement_id"]
        occ = [{"id": "occ-1", "correspondence": "confirmed", "review_status": "reviewed_ad",
                "advertiser_confirmed": True}]
        evaluation = campaigns.allocate_campaign(
            campaigns.list_expected(con, cid), occ, {"state": "complete"},
            "2026-02-01T00:00:00+00:00")
        campaigns.freeze_evaluation(con, cid, evaluation, {"campaign_revision": 1}, "owner")
        return cid, pid

    def test_campaign_html_is_readable_share_pack(self):
        cid, pid = self._campaign()
        out = exports.build_export(self.con, {}, "html", title="Campaign: CrownCoins",
                                   actor="owner", campaign_id=cid)
        payload = out["payload"]
        for marker in ("Campaign brief", "<table", "Checks and manual assessments",
                       "Expected placements and evaluations", "Limitations",
                       "CrownCoins &lt;share&gt; pack", pid, "occ-1"):
            self.assertIn(marker, payload, marker)
        self.assertNotIn("#/review/", payload)
        self.assertNotIn("<audio", payload.lower())
        self.assertNotIn("<share>", payload)
        self.assertNotIn("<script>", payload)

    def test_non_campaign_html_keeps_current_layout(self):
        vid = seed_episode(self.con)
        seed_evidence(self.con, vid=vid)
        out = exports.build_export(self.con, {}, "html", title="Plain export", actor="owner")
        self.assertIn("<pre>", out["payload"])

    def test_csv_and_json_paths_untouched(self):
        cid, _ = self._campaign()
        csv_out = exports.build_export(self.con, {}, "csv", actor="owner", campaign_id=cid)
        self.assertTrue(csv_out["payload"].startswith("record_type,"))
        json_out = exports.build_export(self.con, {}, "json", actor="owner", campaign_id=cid)
        bundle = json.loads(json_out["payload"])
        self.assertIn("items", bundle)
        self.assertIn("campaign", bundle)
        self.assertNotIn("placements", bundle)

    def test_guided_campaign_flow_present_in_views(self):
        # Operator tools live under app/operator/ since the customer split.
        src = (Path(__file__).resolve().parent.parent / "app" / "operator" / "views.js").read_text()
        for token in ("Guided campaign flow", "Expected placements", "Campaign printable HTML"):
            self.assertIn(token, src)

    def test_occurrence_shortcuts_present_in_views(self):
        src = (Path(__file__).resolve().parent.parent / "app" / "operator" / "views.js").read_text()
        for token in ("build-occs", "eval-now", "occurrences ready",
                      "No attached evidence — attach judged evidence first",
                      "no frozen evaluation yet"):
            self.assertIn(token, src)


if __name__ == "__main__":
    unittest.main()
