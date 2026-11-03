"""PSURF product surfaces (384/407/408/517-rest/540/604): stdlib unittest.

Disposable workspaces under .muse-product-surf/test-work; real
scripts/app_server.py Handler over in-memory transport where HTTP.
"""
from __future__ import annotations

import io
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO))

import app_server as srv
from earwitness.app import campaigns, creatives, db, monitoring, observations, ops

PORT = 18791
HOST = f"127.0.0.1:{PORT}"
CSRF = "test-psurf-csrf"

SEGS = [
    {"start": 0.0, "end": 15.0, "text": "go to kachava dot com use code SAVE20 for 20 percent off"},
    {"start": 15.0, "end": 30.0, "text": "back to editorial content here"},
]
TEXT_A = "go to kachava dot com use code SAVE20 for 20 percent off today"
TEXT_B = "go to kachava dot com use code SAVE30 for 20 percent off today"


def workdir(name: str) -> Path:
    root = REPO / ".muse-product-surf" / "test-work"
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=name + "-", dir=str(root)))


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


def seed_evidence(con, eid="ev-1", ep="ep-1", brand="kachava", judgment="ad",
                  record_kind="candidate", vid=None):
    con.execute(
        "INSERT OR IGNORE INTO evidence (id, source, source_version_id, episode_id,"
        " orig_start_s, orig_end_s, excerpt, attrs, revision, created_at,"
        " transcript_version_id, record_kind)"
        " VALUES (?, 'jev', ?, ?, 0.0, 15.0, 'kachava excerpt', '{}', 1, ?, ?, ?)",
        (eid, vid, ep, db.utcnow(), vid, record_kind),
    )
    con.execute(
        "INSERT INTO brand_assocs (evidence_id, brand, basis, origin, revision, updated_at)"
        " VALUES (?, ?, 'confirmed', 'test', 1, ?)",
        (eid, brand, db.utcnow()),
    )
    if judgment:
        con.execute(
            "INSERT OR REPLACE INTO reviews (evidence_id, judgment, eff_start_s, eff_end_s,"
            " notes, revision, updated_at) VALUES (?, ?, 0.0, 15.0, '', 1, ?)",
            (eid, judgment, db.utcnow()),
        )
    con.commit()


def propose(con, vid, ep="ep-1", start=0.0, end=15.0):
    return observations.propose_observation(
        con, episode_id=ep, start_s=start, end_s=end,
        bounds_evidence={"segments": SEGS}, fields={"read_type": {"value": "unknown"}},
        attributions=[{"entity": "kachava.com", "raw_phrase": "kachava dot com",
                       "channel": "url", "method": "deterministic", "status": "suggested"}],
        source_pins={"source_version_id": vid, "transcript_version_id": vid},
        actor="pipeline")["observation_id"]


class HandlerCase(unittest.TestCase):
    def setUp(self):
        self.ws = workdir("case")
        con = db.connect(self.ws)
        try:
            db.init_schema(con)
        finally:
            con.close()
        self._prev_ws = srv._state["workspace"]
        self._prev_port = srv._state["port"]
        self._prev_csrf = srv._state["csrf"]
        srv._state["workspace"] = self.ws
        srv._state["port"] = PORT
        srv._state["csrf"] = CSRF

    def tearDown(self):
        srv._state["workspace"] = self._prev_ws
        srv._state["port"] = self._prev_port
        srv._state["csrf"] = self._prev_csrf
        shutil.rmtree(self.ws, ignore_errors=True)

    def call(self, method, path, body=None):
        handler = srv.Handler.__new__(srv.Handler)
        handler.command = method
        handler.path = path
        handler.request_version = "HTTP/1.1"
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler._headers_buffer = []
        handler.log_message = lambda *a: None
        payload = b""
        headers = {"Host": HOST}
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if method in ("POST", "PUT", "DELETE"):
            headers["X-CSRF-Token"] = CSRF
            headers["Origin"] = f"http://{HOST}"
        headers["Content-Length"] = str(len(payload))
        handler.headers = headers
        handler.rfile = io.BytesIO(payload)
        handler.wfile = io.BytesIO()
        handler.close_connection = True
        if method == "GET":
            handler.do_GET()
        elif method == "POST":
            handler.do_POST()
        elif method == "PUT":
            handler.do_PUT()
        else:
            raise AssertionError(method)
        data = handler.wfile.getvalue()
        head, _, rest = data.partition(b"\r\n\r\n")
        status = int(head.split(b" ")[1])
        try:
            return status, json.loads(rest)
        except ValueError:
            return status, {"_raw": rest[:200]}

    def db(self):
        con = db.connect(self.ws)
        con.row_factory = sqlite3.Row
        return con


class RecordKindAndCountsTest(HandlerCase):
    def test_evidence_api_returns_record_kind_consumed_by_badges(self):
        con = self.db()
        try:
            vid = seed_episode(con)
            seed_evidence(con, eid="ev-1", vid=vid, record_kind="candidate")
            seed_evidence(con, eid="ev-2", vid=vid, record_kind="observation")
        finally:
            con.close()
        status, out = self.call("GET", "/api/v1/evidence?per_page=50")
        self.assertEqual(status, 200, out)
        kinds = {e["id"]: e.get("record_kind") for e in out["items"]}
        self.assertEqual(kinds.get("ev-1"), "candidate")
        self.assertEqual(kinds.get("ev-2"), "observation")
        src = (REPO / "app" / "operator" / "views.js").read_text()
        self.assertIn("recordKindBadge", src)
        self.assertIn("record_kind", src)
        self.assertIn("Effective transcript version", src)
        self.assertIn("Refined transcript alternative", src)
        self.assertIn("Effective bounds", src)

    def test_brand_counts_units_reviewed_default_proposals_separated(self):
        con = self.db()
        try:
            vid = seed_episode(con)
            seed_evidence(con, eid="ev-1", vid=vid, brand="kachava", judgment="ad")
            seed_evidence(con, eid="ev-2", vid=vid, brand="kachava", judgment=None)
        finally:
            con.close()
        status, out = self.call("GET", "/api/v1/brands/kachava/counts?review=reviewed")
        self.assertEqual(status, 200, out)
        self.assertEqual(out["review_filter"], "reviewed")
        self.assertEqual(out["creatives"]["unit"], "confirmed creative IDs")
        self.assertEqual(out["occurrences"]["unit"], "resolved occurrence IDs")
        self.assertIn("separated", out["proposals"]["unit"])
        self.assertIn("never summed", out["note"])
        self.assertEqual(out["reviewed_associations"], 1)
        self.assertEqual(out["brand_associations"], 2)
        status2, out2 = self.call("GET", "/api/v1/brands/kachava/counts?review=all")
        self.assertEqual(status2, 200, out2)
        self.assertEqual(out2["reviewed_associations"], 1)
        self.assertEqual(out2["brand_associations"], 2)
        src = (REPO / "app" / "operator" / "views.js").read_text()
        self.assertIn("confirmed creative IDs", src)
        self.assertIn("resolved occurrence IDs", src)
        self.assertIn("never summed", src)

    def test_creative_payload_record_kind_and_explore_columns(self):
        con = self.db()
        try:
            vid = seed_episode(con)
            a = propose(con, vid)
            b = propose(con, vid)
            creatives.fingerprint_observation(con, a, transcript_text=TEXT_A)
            creatives.fingerprint_observation(con, b, transcript_text=TEXT_B)
            cid = creatives.create_creative(con, [a, b])["creative_id"]
        finally:
            con.close()
        status, out = self.call("GET", f"/api/v1/creatives/{cid}")
        self.assertEqual(status, 200, out)
        self.assertEqual(out["record_kind"], "creative")
        src = (REPO / "app" / "operator" / "views.js").read_text()
        self.assertIn("Creatives (confirmed creative IDs)", src)
        self.assertIn("Occurrences (resolved occurrence IDs)", src)


class CreativeRoundTripTest(HandlerCase):
    def _two_obs_creative(self):
        con = self.db()
        try:
            vid = seed_episode(con)
            a = propose(con, vid)
            b = propose(con, vid)
            creatives.fingerprint_observation(con, a, transcript_text=TEXT_A)
            creatives.fingerprint_observation(con, b, transcript_text=TEXT_B)
            out = creatives.create_creative(con, [a, b])
            return out["creative_id"], a, b
        finally:
            con.close()

    def test_suggest_shows_offer_code_diffs_unconfirmed(self):
        cid, a, b = self._two_obs_creative()
        status, out = self.call("GET", f"/api/v1/creatives/suggest?observation_id={a}")
        self.assertEqual(status, 200, out)
        self.assertTrue(out["suggestions"])
        top = out["suggestions"][0]
        self.assertFalse(top["confirmed"])
        self.assertIn("codes", top["commercial_diff"])

    def test_confirm_reject_split_merge_round_trip_with_stale_412(self):
        cid, a, b = self._two_obs_creative()
        # confirm a at rev 1
        status, out = self.call("POST", f"/api/v1/creatives/{cid}/confirm",
                                {"observation_id": a, "expected_revision": 1})
        self.assertEqual(status, 200, out)
        self.assertFalse(out["judgments_copied"])
        # stale confirm of b at rev 1 -> 412, membership unchanged
        status2, out2 = self.call("POST", f"/api/v1/creatives/{cid}/confirm",
                                  {"observation_id": b, "expected_revision": 1})
        self.assertEqual(status2, 412, out2)
        con = self.db()
        try:
            row = con.execute("SELECT confirmed FROM creative_memberships"
                              " WHERE creative_id=? AND observation_id=?", (cid, b)).fetchone()
            self.assertEqual(row["confirmed"], 0)
            rev = con.execute("SELECT revision FROM creatives WHERE id=?", (cid,)).fetchone()["revision"]
            self.assertEqual(rev, 2)
        finally:
            con.close()
        # reject pair at current rev
        status3, out3 = self.call("POST", f"/api/v1/creatives/{cid}/reject",
                                  {"observation_id_a": a, "observation_id_b": b,
                                   "expected_revision": 2})
        self.assertEqual(status3, 200, out3)
        # split into two groups
        status4, out4 = self.call("POST", "/api/v1/creatives/split",
                                  {"creative_id": cid, "groups": [[a], [b]],
                                   "expected_revision": out3["revision"]})
        self.assertEqual(status4, 200, out4)
        self.assertEqual(len(out4["children"]), 2)
        # stale split -> 412
        status5, _ = self.call("POST", "/api/v1/creatives/split",
                               {"creative_id": cid, "groups": [[a], [b]],
                                "expected_revision": out3["revision"]})
        self.assertEqual(status5, 412)
        # merge children back
        children = out4["children"]
        con = self.db()
        try:
            revs = {c: con.execute("SELECT revision FROM creatives WHERE id=?", (c,)).fetchone()["revision"]
                    for c in children}
        finally:
            con.close()
        status6, out6 = self.call("POST", "/api/v1/creatives/merge",
                                  {"creative_ids": children, "expected_revisions": revs})
        self.assertEqual(status6, 200, out6)
        self.assertIn("parents", out6)

    def test_red_capable_stale_confirm_rejected_membership_unchanged(self):
        cid, a, b = self._two_obs_creative()
        con = self.db()
        try:
            before = con.execute("SELECT observation_id, confirmed FROM creative_memberships"
                                 " WHERE creative_id=? ORDER BY observation_id", (cid,)).fetchall()
            before_txt = json.dumps([dict(r) for r in before], sort_keys=True)
        finally:
            con.close()
        s1, _ = self.call("POST", f"/api/v1/creatives/{cid}/confirm",
                          {"observation_id": a, "expected_revision": 1})
        self.assertEqual(s1, 200)
        s2, _ = self.call("POST", f"/api/v1/creatives/{cid}/confirm",
                          {"observation_id": b, "expected_revision": 1})
        self.assertEqual(s2, 412, "RED-CAPABLE: stale creative confirm must be rejected 412")
        con = self.db()
        try:
            after = con.execute("SELECT observation_id, confirmed FROM creative_memberships"
                                " WHERE creative_id=? ORDER BY observation_id", (cid,)).fetchall()
            after_rows = [dict(r) for r in after]
            b_row = [r for r in after_rows if r["observation_id"] == b][0]
            self.assertEqual(b_row["confirmed"], 0, f"before={before_txt} after={after_rows}")
        finally:
            con.close()


class PlacementEvalTest(HandlerCase):
    def _campaign(self):
        con = self.db()
        try:
            cur = con.execute("INSERT INTO campaigns (name, revision, updated_at) VALUES ('c', 1, ?)",
                              (db.utcnow(),))
            return cur.lastrowid
        finally:
            con.close()

    def test_insufficient_after_close_is_ambiguous_count(self):
        cid = self._campaign()
        status, out = self.call("POST", f"/api/v1/campaigns/{cid}/placements",
                                {"scope": "show_window", "show_id": "s1",
                                 "date_from": "2026-01-01", "date_to": "2026-01-31",
                                 "timezone": "UTC", "expected_count": 2,
                                 "requirements": {}, "provenance": {"synthetic": True}})
        self.assertEqual(status, 200, out)
        occ = [{"id": "occ-1", "correspondence": "confirmed", "review_status": "reviewed_ad",
                "advertiser_confirmed": True, "published_at": "2026-01-10T00:00:00+00:00"}]
        s2, e2 = self.call("POST", f"/api/v1/campaigns/{cid}/evaluate",
                           {"occurrences": occ, "coverage": {"state": "complete"},
                            "now_iso": "2026-02-01T00:00:00+00:00"})
        self.assertEqual(s2, 200, e2)
        ev = next(iter(e2["evaluations"].values()))
        self.assertEqual(ev["outcome"], "ambiguous")
        self.assertIn(campaigns.INSUFFICIENT_SUPPORTED_COUNT, ev["reasons"])

    def test_supported_and_manual_assessment_separate_and_history(self):
        cid = self._campaign()
        s0, p0 = self.call("POST", f"/api/v1/campaigns/{cid}/placements",
                           {"scope": "show_window", "show_id": "s1",
                            "expected_count": 1, "requirements": {},
                            "provenance": {"synthetic": True}})
        self.assertEqual(s0, 200, p0)
        pid = p0["placement_id"]
        occ = [{"id": "occ-1", "correspondence": "confirmed", "review_status": "reviewed_ad",
                "advertiser_confirmed": True}]
        s1, e1 = self.call("POST", f"/api/v1/campaigns/{cid}/evaluate",
                           {"occurrences": occ, "coverage": {"state": "complete"},
                            "now_iso": "2026-01-01T00:00:00+00:00"})
        self.assertEqual(s1, 200, e1)
        s2, m2 = self.call("POST", f"/api/v1/campaigns/{cid}/placements/{pid}/assess",
                           {"author": "owner", "verdict": "supported",
                            "reason": "listened and verified", "evidence": []})
        self.assertEqual(s2, 200, m2)
        s3, f3 = self.call("POST", f"/api/v1/campaigns/{cid}/evaluations/freeze",
                           {"evaluation": e1, "pins": {"campaign_revision": 1}})
        self.assertEqual(s3, 200, f3)
        s4, h4 = self.call("GET", f"/api/v1/campaigns/{cid}/placements/{pid}/history")
        self.assertEqual(s4, 200, h4)
        self.assertTrue(h4["history"])
        src = (REPO / "app" / "operator" / "views.js").read_text()
        for token in ("Expected placements", "six-state", "Manual assessment",
                      "Frozen evaluation history", "synthetic"):
            self.assertIn(token, src)


class MonitorTest(HandlerCase):
    def _watchlist(self):
        con = self.db()
        try:
            return ops.save_watchlist(con, {"name": "w", "query": {"q": "kachava"}}, None, None)["id"]
        finally:
            con.close()

    def test_enable_baseline_then_no_flood_and_preferences(self):
        wid = self._watchlist()
        s1, o1 = self.call("POST", f"/api/v1/monitoring/watchlists/{wid}/enable",
                           {"current_matches": [{"identity": "occ-1"}], "definition_revision": 1})
        self.assertEqual(s1, 200, o1)
        self.assertEqual(o1["baseline_count"], 1)
        self.assertTrue(o1["enabled"])
        s2, o2 = self.call("POST", f"/api/v1/monitoring/watchlists/{wid}/backfill",
                           {"confirm": True,
                            "current_matches": [{"kind": "new_observation", "identity": "occ-2"}],
                            "definition_revision": 1})
        self.assertEqual(s2, 200, o2)
        self.assertEqual(o2["created"], 1)
        s3, o3 = self.call("POST", f"/api/v1/monitoring/watchlists/{wid}/backfill",
                           {"confirm": True,
                            "current_matches": [{"kind": "new_observation", "identity": "occ-2"}],
                            "definition_revision": 1})
        self.assertEqual(s3, 200, o3)
        self.assertEqual(o3["duplicates"], 1)
        s4, o4 = self.call("POST", "/api/v1/monitoring/preferences",
                           {"member": "owner", "digest": "daily", "immediate": False})
        self.assertEqual(s4, 200, o4)
        s5, o5 = self.call("GET", "/api/v1/monitoring/preferences?member=owner")
        self.assertEqual(s5, 200, o5)
        self.assertEqual(o5["preferences"]["digest"], "daily")
        s6, o6 = self.call("POST", "/api/v1/monitoring/preferences/unsubscribe",
                           {"member": "owner", "unsubscribed": True})
        self.assertEqual(s6, 200, o6)
        s7, o7 = self.call("GET", "/api/v1/monitoring/preferences?member=owner")
        self.assertTrue(o7["preferences"]["unsubscribed"])
        # freshness display: last success vs attempt + paused state shape
        con = self.db()
        try:
            sid = f"watchlist-{wid}"
            monitoring.record_poll_result(con, sid, "success", {"episodes": 1})
            monitoring.record_poll_result(con, sid, "budget_exhausted")
        finally:
            con.close()
        s8, o8 = self.call("GET", f"/api/v1/monitoring/schedules/{sid}/status")
        self.assertEqual(s8, 200, o8)
        self.assertIn("last_success_at", o8["schedule"])
        self.assertIn("last_attempt_at", o8["schedule"])
        self.assertTrue(o8["schedule"]["paused_reason"])
        src = (REPO / "app" / "operator" / "views.js").read_text()
        for token in ("Enable monitor", "Backfill", "last success", "Digest preferences", "Unsubscribe"):
            self.assertIn(token, src)

    def test_backfill_requires_explicit_confirm(self):
        wid = self._watchlist()
        self.call("POST", f"/api/v1/monitoring/watchlists/{wid}/enable",
                  {"current_matches": [], "definition_revision": 1})
        status, _ = self.call("POST", f"/api/v1/monitoring/watchlists/{wid}/backfill",
                              {"current_matches": [{"kind": "new_observation", "identity": "x"}]})
        self.assertEqual(status, 400)


class AdditiveRoutesRejectInvalidTest(HandlerCase):
    def test_invalid_inputs_are_400_not_broadening(self):
        con = self.db()
        try:
            vid = seed_episode(con)
            a = propose(con, vid)
            b = propose(con, vid)
            cid = creatives.create_creative(con, [a, b])["creative_id"]
            camp = con.execute("INSERT INTO campaigns (name, revision, updated_at)"
                               " VALUES ('c', 1, ?) RETURNING id", (db.utcnow(),)).fetchone()["id"]
            wl = ops.save_watchlist(con, {"name": "w", "query": {}}, None, None)["id"]
        finally:
            con.close()
        cases = [
            ("GET", "/api/v1/creatives/suggest", 400),
            ("GET", "/api/v1/creatives/suggest?observation_id=!!!", 400),
            ("GET", "/api/v1/brands/kachava/counts?review=sometimes", 400),
            ("GET", "/api/v1/monitoring/preferences", 400),
            ("POST", "/api/v1/creatives", 400),
            ("POST", f"/api/v1/creatives/{cid}/confirm", 400),
            ("POST", "/api/v1/creatives/split", 400),
            ("POST", "/api/v1/creatives/merge", 400),
            ("POST", f"/api/v1/monitoring/watchlists/{wl}/backfill", 400),
            ("POST", "/api/v1/monitoring/preferences", 400),
            ("POST", f"/api/v1/campaigns/{camp}/placements", 400),
            ("POST", f"/api/v1/campaigns/{camp}/evaluate", 400),
        ]
        bodies = {
            "/api/v1/creatives": {"observation_ids": []},
            f"/api/v1/creatives/{cid}/confirm": {"observation_id": ""},
            "/api/v1/creatives/split": {"creative_id": cid, "groups": []},
            "/api/v1/creatives/merge": {"creative_ids": [cid], "expected_revisions": {}},
            f"/api/v1/monitoring/watchlists/{wl}/backfill": {"current_matches": []},
            "/api/v1/monitoring/preferences": {"member": "", "digest": "sometimes"},
            f"/api/v1/campaigns/{camp}/placements": {"scope": "nope"},
            f"/api/v1/campaigns/{camp}/evaluate": {"occurrences": [], "coverage": {}},
        }
        for method, path, expected in cases:
            if method == "GET":
                status, _ = self.call(method, path)
            else:
                status, _ = self.call(method, path, bodies[path])
            self.assertEqual(status, expected, f"{method} {path}")


class ReviewSurfaceProofTest(unittest.TestCase):
    def test_517_rest_and_384_strings_render(self):
        src = (REPO / "app" / "operator" / "views.js").read_text()
        self.assertIn("atEnd", src)
        self.assertIn("End of queue. No further result in this queue.", src)
        self.assertIn("queue-position", src)
        self.assertIn("App.generation", src)
        self.assertIn("Audio unavailable", src)
        self.assertIn("No media", src)
        self.assertIn("outside the bundled clip", src)
        self.assertIn("Saving", src)
        self.assertIn("412", src)
        self.assertIn("Draft retained", src)


if __name__ == "__main__":
    unittest.main()
