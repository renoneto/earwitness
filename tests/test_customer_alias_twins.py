"""Alias twins (UAT 2026-10-15): an explicit source alias (alias_of, e.g.
bench_0 -> s000e0) is the same audio as its canonical episode, so a read
or passage it shares with the canonical episode shows and counts once.

"Twin" is decided only through the recorded alias_of link plus an
overlapping span of the same read kind on the canonical episode, never
by text or title. The copy carrying a human review wins, else the
canonical copy. Alias reads with no twin stay. Alias items carry the
canonical episode's title and date. No stored row changes.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
import time
import unittest
import urllib.parse

from earwitness.app import asgi as _asgi
from earwitness.app import browser_auth as _auth
from earwitness.app import collaboration as _collab
from earwitness.app import connections as _conn
from earwitness.app import customer, customer_contracts, db


def _transcript(con, ep_id, segments):
    cur = con.execute(
        "INSERT INTO source_versions (source_key, version_no, sha256, payload,"
        " is_active, created_at) VALUES (?, 1, ?, ?, 1, ?)",
        (f"transcript:{ep_id}", "sh-" + ep_id, json.dumps({"segments": segments}),
         db.utcnow()))
    return cur.lastrowid


def _evidence(con, ev_id, episode_id, start, end, judgment=None, key=None):
    tv = con.execute("SELECT transcript_version_id FROM episodes WHERE id=?",
                     (episode_id,)).fetchone()[0]
    con.execute(
        "INSERT INTO evidence (id, source, source_version_id, episode_id,"
        " orig_start_s, orig_end_s, excerpt, attrs, revision, created_at,"
        " transcript_version_id, source_record_key, assoc_revision,"
        " record_kind) VALUES (?, 'jev', 1, ?, ?, ?, 'acme offer', '{}', 1, ?,"
        " ?, ?, 0, 'candidate')",
        (ev_id, episode_id, start, end, db.utcnow(), tv,
         key or f"jev:{episode_id}:{ev_id}"))
    if judgment is not None:
        con.execute("INSERT INTO reviews (evidence_id, judgment, revision,"
                    " updated_at) VALUES (?, ?, 1, ?)", (ev_id, judgment, db.utcnow()))
    con.execute("INSERT INTO brand_assocs (evidence_id, brand, basis, origin,"
                " revision, updated_at) VALUES (?, 'acme', 'suggested', 'test', 1, ?)",
                (ev_id, db.utcnow()))


def seed_alias_twins(con):
    """s000e0 (dated 2026-03-10) and its undated alias bench_0.

    Reads: twin pair A (alias copy reviewed), twin pair B (neither
    reviewed), one alias-only read, one canonical-only read.
    Passages matching "acme": canonical 0-10 and 20-30; alias 1-11
    (twin of 0-10) and 40-50 (no twin)."""
    con.execute(
        "INSERT INTO source_versions (source_key, version_no, sha256, payload,"
        " is_active, created_at) VALUES ('episodes.json', 1, 'sh-eps', ?, 1, ?)",
        (json.dumps([{"ep_id": "s000e0", "show_title": "Shop",
                      "feed_url": "https://shop.example/rss"}]), db.utcnow()))
    tv_c = _transcript(con, "s000e0", [
        {"start": 0.0, "end": 10.0, "text": "acme one"},
        {"start": 10.0, "end": 20.0, "text": "plain talk"},
        {"start": 20.0, "end": 30.0, "text": "acme two"}])
    tv_a = _transcript(con, "bench_0", [
        {"start": 1.0, "end": 11.0, "text": "acme one again"},
        {"start": 40.0, "end": 50.0, "text": "acme solo"}])
    now = db.utcnow()
    con.execute(
        "INSERT INTO episodes (id, kind, show_title, ep_title, category, pub_date,"
        " fetch_date, transcript_version_id, revision, updated_at)"
        " VALUES ('s000e0', 'supplied', 'Shop', 'Canonical title', 'business',"
        " '2026-03-10', '2026-09-21', ?, 1, ?)", (tv_c, now))
    con.execute(
        "INSERT INTO episodes (id, kind, alias_of, show_title, ep_title, category,"
        " pub_date, fetch_date, transcript_version_id, revision, updated_at)"
        " VALUES ('bench_0', 'supplied-alias', 's000e0', 'Shop', 'Alias cut',"
        " 'business', '', '2026-09-21', ?, 1, ?)", (tv_a, now))
    _evidence(con, "c-a", "s000e0", 60.0, 120.0)
    _evidence(con, "a-a", "bench_0", 60.0, 120.0, judgment="ad")
    _evidence(con, "c-b", "s000e0", 200.0, 260.0)
    _evidence(con, "a-b", "bench_0", 205.0, 262.0)
    _evidence(con, "a-solo", "bench_0", 400.0, 460.0)
    _evidence(con, "c-solo", "s000e0", 600.0, 660.0)
    con.commit()
    from tests.backend_fixtures import publish_all_records
    publish_all_records(con)
    customer.rebuild_customer_show_map(con)
    con.commit()


def _q(mode, **extra):
    return customer_contracts.upcast_query_v1(dict({"mode": mode}, **extra))[0]


def _ev_ids(items):
    return sorted(i["evidence_id"] for i in items)


class AliasTwinTests(unittest.TestCase):
    def setUp(self):
        from tests.backend_fixtures import make_file_workspace
        self.con = make_file_workspace()
        seed_alias_twins(self.con)

    def tearDown(self):
        self.con.close()

    def test_twin_reads_show_and_count_once_reviewed_copy_wins(self):
        ads = customer.query(self.con, _q("ads"))
        # Pair A: the reviewed alias copy; pair B: the canonical copy;
        # the alias-only read stays.
        self.assertEqual(_ev_ids(ads["items"]), ["a-a", "a-solo", "c-b", "c-solo"])
        self.assertEqual(ads["total"], 4)
        self.assertEqual((ads["dated_total"], ads["undated_total"]), (4, 0))
        # Stored rows are untouched.
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 6)

    def test_brand_counts_count_twins_once(self):
        entry = next(e for e in customer.brands_directory(self.con) if e["key"] == "acme")
        self.assertEqual(entry["records"], 4)
        detail = customer.brand_detail(self.con, "acme")
        self.assertEqual(detail["total"], 4)

    def test_show_page_counts_twins_once_under_the_canonical_episode(self):
        show_id = customer.customer_show_for_episode(self.con, "s000e0")["show_identity_id"]
        detail = customer.show_detail(self.con, show_id)
        self.assertEqual([(e["id"], e["ads"]) for e in detail["episodes"]], [("s000e0", 4)])
        acme = next(s for s in detail["sponsors"] if s["key"] == "acme")
        self.assertEqual((acme["episodes"], acme["ads"], acme["first_seen"]),
                         (1, 4, "2026-03-10"))

    def test_alias_card_carries_canonical_title_and_date(self):
        ads = customer.query(self.con, _q("ads"))
        for item in ads["items"]:
            self.assertEqual(item["pub_date"], "2026-03-10", item["evidence_id"])
            self.assertEqual(item["ep_title"], "Canonical title", item["evidence_id"])

    def test_alias_card_date_with_legacy_pinned_history(self):
        # Older pinned episode history rows carry no kind/alias_of (the
        # owner snapshot's bench_0); the episode row's link still decides.
        self.con.execute("UPDATE episode_history SET kind=NULL, alias_of=NULL"
                         " WHERE episode_id='bench_0'")
        self.con.commit()
        ads = customer.query(self.con, _q("ads"))
        alias = [i for i in ads["items"] if i["episode_id"] == "bench_0"]
        self.assertEqual(len(alias), 2)
        for item in alias:
            self.assertEqual((item["pub_date"], item["ep_title"]),
                             ("2026-03-10", "Canonical title"))

    def test_alias_passages_fold_into_canonical_passages(self):
        mentions = customer.query(self.con, _q("mentions", q="acme"))
        got = sorted((i["episode_id"], i["text"]) for i in mentions["items"])
        self.assertEqual(got, [("bench_0", "acme solo"), ("s000e0", "acme one"),
                               ("s000e0", "acme two")])
        for item in mentions["items"]:
            self.assertEqual(item["pub_date"], "2026-03-10")
        episodes = customer.query(self.con, _q("episodes", q="acme"))
        self.assertEqual([i["episode_id"] for i in episodes["items"]], ["s000e0"])
        self.assertEqual(episodes["items"][0]["matching_passages"], 3)


class AliasTwinHostedLocalTests(unittest.TestCase):
    """The hosted search route and the local owner read agree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = os.path.realpath(os.path.join(self.tmp.name, "data"))
        os.makedirs(self.data_dir)
        self.db_path = os.path.join(self.data_dir, "ws.sqlite3")
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        db.init_schema(con)
        con.execute("INSERT OR REPLACE INTO meta (key, value)"
                    " VALUES ('workspace_id', 'ws-identity-1')")
        con.commit()
        _collab.create_workspace(con, "local", "Acme", "oidc:https://issuer.example:alice")
        seed_alias_twins(con)
        con.close()
        self.app = _asgi.HostedApp(_asgi.HostedConfig(
            data_dir=self.data_dir, grants_db=os.path.join(self.tmp.name, "grants.sqlite3")))
        _conn.register_workspace(self.app.store, "acme", self.db_path, self.data_dir)
        digest = hashlib.sha256(b"ew-browser-v1:sess-alias-1").hexdigest()
        now_s = time.time()
        raw = self.app.store.auth_connection()
        try:
            _auth.ensure_auth_schema(raw)
            raw.execute(
                "INSERT INTO browser_sessions (session_hash, issuer, subject, email,"
                " email_verified, csrf_token, active_workspace, created_at, expires_at,"
                " revoked_at) VALUES (?, 'https://issuer.example', 'alice', '', 0,"
                " 'csrf-alias', 'acme', ?, ?, NULL)", (digest, now_s, now_s + 3600))
            raw.commit()
        finally:
            raw.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _hosted_search(self, query):
        scope = {"type": "http", "method": "GET", "path": "/api/v1/customer/search",
                 "scheme": "http",
                 "headers": [(b"host", b"127.0.0.1:8771"),
                             (b"cookie", b"ew_session=sess-alias-1")],
                 "query_string": ("query=" + urllib.parse.quote(json.dumps(query))).encode()}
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        asyncio.run(self.app(scope, receive, send))
        start = next(m for m in sent if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        self.assertEqual(start["status"], 200, body[:300])
        return json.loads(body.decode())

    def test_hosted_and_local_agree(self):
        query = {"version": 2, "mode": "ads"}
        hosted = self._hosted_search(query)
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        try:
            local = customer.query(con, customer_contracts.normalize_query(query))
        finally:
            con.close()
        view = lambda r: sorted((i["evidence_id"], i["pub_date"]) for i in r["items"])
        self.assertEqual(hosted["total"], 4)
        self.assertEqual(view(hosted), view(local))
        self.assertEqual((hosted["total"], hosted["dated_total"]),
                         (local["total"], local["dated_total"]))


if __name__ == "__main__":
    unittest.main()
