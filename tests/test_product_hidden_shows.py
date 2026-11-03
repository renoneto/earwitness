"""Hide H1: reversible show hide with queue exclusion (stdlib unittest).

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
from earwitness.app import db, ops

PORT = 18793
HOST = f"127.0.0.1:{PORT}"
CSRF = "test-hide-h1-csrf"

DE_SHOW = "Der Beispiel Podcast mit Beispiel | Unternehmertum und Marketing"
EN_SHOW = "Example English Show"


def workdir(name: str) -> Path:
    root = REPO / ".muse-product-surf" / "test-work"
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=name + "-", dir=str(root)))


def seed_episode(con, ep, show):
    con.execute(
        "INSERT OR IGNORE INTO episodes (id, kind, show_title, ep_title, fetch_date, updated_at)"
        " VALUES (?, 'supplied', ?, ?, '2026-01-02', '2026-01-02T00:00:00+00:00')",
        (ep, show, f"Title {ep}"),
    )
    cur = con.execute(
        "INSERT INTO source_versions (source_key, version_no, sha256, payload, is_active, created_at)"
        " VALUES (?, 1, 'abc', ?, 1, ?)",
        (f"transcript:{ep}", json.dumps({"segments": []}), db.utcnow()),
    )
    vid = cur.lastrowid
    con.execute("UPDATE episodes SET transcript_version_id=? WHERE id=?", (vid, ep))
    con.commit()
    return vid


def seed_evidence(con, eid, ep, vid):
    con.execute(
        "INSERT OR IGNORE INTO evidence (id, source, episode_id,"
        " orig_start_s, orig_end_s, excerpt, attrs, revision, created_at,"
        " transcript_version_id, record_kind)"
        " VALUES (?, 'jev', ?, 0.0, 15.0, 'excerpt', '{}', 1, ?, ?, 'candidate')",
        (eid, ep, db.utcnow(), vid),
    )
    con.commit()


class HandlerCase(unittest.TestCase):
    def setUp(self):
        self.ws = workdir("hide")
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

    def call(self, method, path, body=None, extra_headers=None):
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
        headers.update(extra_headers or {})
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

    def seed(self):
        con = self.db()
        try:
            vid_de = seed_episode(con, "ep-de-1", DE_SHOW)
            vid_en = seed_episode(con, "ep-en-1", EN_SHOW)
            seed_evidence(con, "ev-de-1", "ep-de-1", vid_de)
            seed_evidence(con, "ev-en-1", "ep-en-1", vid_en)
        finally:
            con.close()


class HideShowTest(HandlerCase):
    def test_hide_removes_from_queue_and_counts_with_notice(self):
        self.seed()
        status, before = self.call("GET", "/api/v1/evidence?per_page=50")
        self.assertEqual(status, 200, before)
        self.assertEqual(before["total"], 2)
        self.assertEqual(before["hidden_excluded"], 0)
        status, out = self.call("PUT", "/api/v1/shows/hide",
                                {"show_title": DE_SHOW, "hidden": True,
                                 "expected_revision": 0})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["revision"], 1)
        self.assertTrue(out["hidden"])
        status, after = self.call("GET", "/api/v1/evidence?per_page=50")
        self.assertEqual(status, 200, after)
        self.assertEqual(after["total"], 1)
        self.assertEqual(after["hidden_excluded"], 1)
        self.assertEqual(after["hidden_shows"], [DE_SHOW])
        self.assertEqual([e["id"] for e in after["items"]], ["ev-en-1"])
        status, overview = self.call("GET", "/api/v1/overview")
        self.assertEqual(status, 200, overview)
        self.assertEqual(overview["hidden_excluded"], 1)
        self.assertEqual(overview["hidden_shows"], [DE_SHOW])
        self.assertEqual(overview["evidence_by_source"], {"jev": 1})
        status, eps = self.call("GET", "/api/v1/episodes")
        self.assertEqual(status, 200, eps)
        by_id = {e["id"]: e for e in eps["episodes"]}
        self.assertTrue(by_id["ep-de-1"]["hidden"])
        self.assertEqual(by_id["ep-de-1"]["show_revision"], 1)
        self.assertEqual(by_id["ep-de-1"]["evidence"], 0)
        self.assertFalse(by_id["ep-en-1"]["hidden"])
        status, ep = self.call("GET", "/api/v1/episodes/ep-de-1")
        self.assertEqual(status, 200, ep)
        self.assertTrue(ep["episode"]["hidden"])
        self.assertEqual(ep["episode"]["show_revision"], 1)
        self.assertEqual(ep["episode"]["evidence_count"], 0)
        status, hidden = self.call("GET", "/api/v1/shows/hidden")
        self.assertEqual(status, 200, hidden)
        self.assertEqual([h["show_title"] for h in hidden["hidden"]], [DE_SHOW])

    def test_unhide_restores_queue_and_counts(self):
        self.seed()
        self.call("PUT", "/api/v1/shows/hide",
                  {"show_title": DE_SHOW, "hidden": True, "expected_revision": 0})
        status, out = self.call("PUT", "/api/v1/shows/hide",
                                {"show_title": DE_SHOW, "hidden": False,
                                 "expected_revision": 1})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["revision"], 2)
        self.assertFalse(out["hidden"])
        status, after = self.call("GET", "/api/v1/evidence?per_page=50")
        self.assertEqual(status, 200, after)
        self.assertEqual(after["total"], 2)
        self.assertEqual(after["hidden_excluded"], 0)
        self.assertEqual(sorted(e["id"] for e in after["items"]),
                         ["ev-de-1", "ev-en-1"])
        status, hidden = self.call("GET", "/api/v1/shows/hidden")
        self.assertEqual(status, 200, hidden)
        self.assertEqual(hidden["hidden"], [])

    def test_revision_conflict_returns_412(self):
        self.seed()
        status, out = self.call("PUT", "/api/v1/shows/hide",
                                {"show_title": DE_SHOW, "hidden": True,
                                 "expected_revision": 0})
        self.assertEqual(status, 200, out)
        status, stale = self.call("PUT", "/api/v1/shows/hide",
                                  {"show_title": DE_SHOW, "hidden": True,
                                   "expected_revision": 0})
        self.assertEqual(status, 412, stale)
        self.assertIn("stale revision", stale["error"]["message"])
        status, missing = self.call("PUT", "/api/v1/shows/hide",
                                    {"show_title": DE_SHOW, "hidden": True})
        self.assertEqual(status, 428, missing)
        status, via_header = self.call(
            "PUT", "/api/v1/shows/hide",
            {"show_title": DE_SHOW, "hidden": False},
            extra_headers={"If-Match": "1"})
        self.assertEqual(status, 200, via_header)
        self.assertEqual(via_header["revision"], 2)

    def test_unknown_show_and_bad_types_rejected(self):
        self.seed()
        status, out = self.call("PUT", "/api/v1/shows/hide",
                                {"show_title": "No Such Show", "hidden": True,
                                 "expected_revision": 0})
        self.assertEqual(status, 400, out)
        status, out = self.call("PUT", "/api/v1/shows/hide",
                                {"show_title": DE_SHOW, "hidden": "yes",
                                 "expected_revision": 0})
        self.assertEqual(status, 400, out)
        status, out = self.call("PUT", "/api/v1/shows/hide",
                                {"show_title": "", "hidden": True,
                                 "expected_revision": 0})
        self.assertEqual(status, 400, out)

    def test_raw_rows_preserved_while_hidden(self):
        self.seed()
        self.call("PUT", "/api/v1/shows/hide",
                  {"show_title": DE_SHOW, "hidden": True, "expected_revision": 0})
        con = self.db()
        try:
            self.assertEqual(
                con.execute("SELECT COUNT(*) c FROM evidence").fetchone()["c"], 2)
            self.assertEqual(
                con.execute("SELECT COUNT(*) c FROM evidence WHERE episode_id='ep-de-1'"
                            ).fetchone()["c"], 1)
            self.assertEqual(
                con.execute("SELECT COUNT(*) c FROM episodes WHERE show_title=?",
                            (DE_SHOW,)).fetchone()["c"], 1)
        finally:
            con.close()

    def test_hidden_episode_page_contract_and_notice_markup(self):
        self.seed()
        self.call("PUT", "/api/v1/shows/hide",
                  {"show_title": DE_SHOW, "hidden": True, "expected_revision": 0})
        status, ep = self.call("GET", "/api/v1/episodes/ep-de-1")
        self.assertEqual(status, 200, ep)
        self.assertTrue(ep["episode"]["hidden"])
        src = (REPO / "app" / "operator" / "views.js").read_text()
        self.assertIn("hidden-show-notice", src)
        self.assertIn("review workbench is unavailable", src)
        self.assertIn("if (ep.hidden)", src)
        self.assertIn("Unhide show", src)
        self.assertIn("Hide show", src)
        self.assertIn("hidden_excluded", src)


if __name__ == "__main__":
    unittest.main()
