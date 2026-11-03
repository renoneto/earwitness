"""M11-PLAYER-FIX2 (lead decision LEDGER-LEAD3-M11-2): what a refused clip
read costs the server.

M11-SCR-R2-01 ships as a known issue (M11-WEBKIT): a media 429 after a
seek into audio not yet loaded makes headless WebKit's media loader ask
again, about 170 times in 0.6 s, before the player stops; with an empty
refusal body (a browser stub) it asked 250-700 times a second until the
reader left the page. Every ask is refused. These tests pin what that
flood may cost, and that the media 429 keeps its body:

1. A refused media read (the media budget's 429, and the audio policy's
   refusal before any workspace: the import volume guard) does no
   workspace work: no workspace database is opened, and the only database
   work is the one read that finds the browser session (it has to be
   known before its budget can be). It writes nothing.
2. A flood of refused media reads from one reader does not use up that
   reader's budget for other requests: Search, Overview and alerts still
   answer 200 during and right after it, on the same session (the
   per-session RateLimiter and the media budget, docs/hosting/request-limits.md).
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import unittest
import urllib.parse
from unittest import mock

from earwitness.app import asgi as _asgi
from earwitness.app import connections as _conn
from earwitness.app import media as _media
from earwitness.app import services as _services
from tests.test_uniform_refusals import _Base

MEDIA = "/api/v1/customer/records/sr1%3Aabc/media"
# The Search page's read: one Query2 query (as tests/pilot_drill_content.py).
Q2 = {"version": 2, "mode": "ads", "q": "lumen", "brands": None, "shows": None,
      "categories": None, "statuses": None, "audio": None, "date_status": "all",
      "from": None, "to": None, "sort": "newest", "page": 1, "ids": None}
PAGES = {"search": ("/api/v1/customer/search", "query=" + urllib.parse.quote(json.dumps(Q2))),
         "overview": ("/api/v1/customer/overview", ""), "alerts": ("/api/v1/customer/alerts", "")}
FLOOD = 5000


class _OneSession(_Base):
    """The ASGI app with one browser session and its own limiter."""

    def setUp(self):
        super().setUp()
        self.app.limiter = _asgi.RateLimiter(session_burst=_asgi.SESSION_BURST)
        # The real time: Search checks the session's expiry on the app's clock.
        self.now = time.time()
        self.app.config.clock = lambda: self.now
        self.cookie = f"ew_session={self.http.session_id}".encode()

    async def acall(self, path, range_=None, query=""):
        """GET through the app: (status, Retry-After or None)."""
        hdrs = [(b"host", b"127.0.0.1:8771"), (b"cookie", self.cookie)]
        if range_:
            hdrs.append((b"range", range_.encode()))
        scope = {"type": "http", "method": "GET", "path": path,
                 "scheme": "http", "headers": hdrs, "query_string": query.encode()}
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await self.app(scope, receive, send)
        self.last_body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        start = next(m for m in sent if m["type"] == "http.response.start")
        after = dict(start.get("headers", [])).get(b"retry-after")
        return start["status"], None if after is None else int(after)

    def call(self, path, range_=None, query=""):
        return asyncio.run(self.acall(path, range_, query))

    def spend_media_budget(self):
        statuses = [self.call(MEDIA) for _ in range(_asgi.MEDIA_BURST)]
        self.assertNotIn(429, statuses)
        self.assertEqual(self.call(MEDIA)[0], 429, "fixture: the media budget is spent")


class _DbWork:
    """Every sqlite3 connection opened and every statement run while it is
    entered, and the workspace entry points made to fail if called."""

    def __init__(self, test):
        self.test = test
        self.opened = []
        self.statements = []

    def __enter__(self):
        real = sqlite3.connect

        def connect(*args, **kw):
            con = real(*args, **kw)
            self.opened.append(str(args[0] if args else kw.get("database")))
            con.set_trace_callback(self.statements.append)
            return con

        self.patches = [mock.patch.object(sqlite3, "connect", connect)] + [
            mock.patch.object(owner, name, wraps=getattr(owner, name)) for owner, name in (
                (_services, "open_customer_workspace"), (_services, "open_shared_workspace"),
                (_conn, "open_registered_db"))]
        self.entries = [p.start() for p in self.patches][1:]
        return self

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()
        return False

    def only_the_session_lookup(self, requests):
        """Each refused request opened one connection (the session lookup)
        and ran one statement there: the browser_sessions read."""
        t = self.test
        t.assertEqual([e.call_count for e in self.entries], [0, 0, 0], "a workspace entry point ran")
        t.assertEqual(len(self.opened), requests, self.opened)
        t.assertEqual(len(self.statements), requests, self.statements)
        for sql in self.statements:
            t.assertRegex(" ".join(sql.split()), r"^SELECT .* FROM browser_sessions WHERE session_hash='[0-9a-f]{64}'$")
            t.assertNotRegex(sql, r"(?i)\b(insert|update|delete|replace|begin)\b")
        t.assertNotIn(t.http.db_path, " ".join(self.opened), "the workspace database was opened")


class RefusedMediaReadDbWorkTest(_OneSession):
    def test_a_media_budget_refusal_opens_no_workspace_and_reads_only_the_session(self):
        self.spend_media_budget()
        with _DbWork(self) as work:
            statuses = [self.call(MEDIA, "bytes=393216-544182")[0] for _ in range(20)]
        self.assertEqual(statuses, [429] * 20)
        work.only_the_session_lookup(20)

    def test_a_media_budget_refusal_keeps_its_json_body(self):
        """M11-WEBKIT: WebKit reads the 429 body as audio and moves its
        range on by the body's size, so the decoder fails and the player
        stops after one burst. A refusal with an empty body kept the range
        still and the loop endless (m11-webkit-verify)."""
        self.spend_media_budget()
        self.assertEqual(self.call(MEDIA, "bytes=393216-544182")[0], 429)
        self.assertGreater(len(self.last_body), 0, "the media 429 has no body")
        self.assertEqual(json.loads(self.last_body)["error"]["code"], "RATE_LIMITED")

    def test_an_audio_policy_refusal_opens_no_workspace_and_reads_only_the_session(self):
        # The import volume guard refuses audio before any workspace is opened.
        with mock.patch.object(_media, "import_volume_code", return_value="VOLUME_ABSENT"):
            with _DbWork(self) as work:
                statuses = [self.call(MEDIA, "bytes=0-")[0] for _ in range(20)]
        self.assertEqual(set(statuses), {statuses[0]})
        self.assertNotIn(statuses[0], (200, 206, 429), "fixture: refused by the audio policy")
        work.only_the_session_lookup(20)
        # The refusal spent no media token.
        self.assertNotEqual(self.call(MEDIA)[0], 429)

    def test_the_counter_sees_an_admitted_read_open_the_workspace(self):
        # Positive control: the same counter catches workspace work.
        with _DbWork(self) as work:
            self.call(MEDIA)
        self.assertEqual(work.entries[0].call_count, 1)
        self.assertIn(self.http.db_path, " ".join(work.opened))
        with self.assertRaises(AssertionError):
            work.only_the_session_lookup(1)


class RefusedMediaFloodTest(_OneSession):
    def test_a_flood_of_refused_media_reads_leaves_search_overview_and_alerts_answering(self):
        for name, (path, query) in PAGES.items():
            self.assertEqual(self.call(path, query=query)[0], 200, f"fixture: {name} answers before the flood")
        self.spend_media_budget()

        async def flood():
            seen = {"media": {}, **{name: [] for name in PAGES}}
            for i in range(FLOOD):
                # A short loop: 5,000 asks over 5 s (WebKit asked 250-700 a second).
                self.now += 5.0 / FLOOD
                status, _ = await self.acall(MEDIA, "bytes=393216-544182")
                seen["media"][status] = seen["media"].get(status, 0) + 1
                if i % 500 == 499:   # during the flood
                    for name, (path, query) in PAGES.items():
                        seen[name].append((await self.acall(path, query=query))[0])
            return seen

        seen = asyncio.run(flood())
        # The refill (one every 2 s) may admit a read or two in 5 s; the rest are refused.
        self.assertLessEqual(FLOOD - seen["media"].get(429, 0), 5 / (60 / _asgi.MEDIA_PER_MINUTE) + 1, seen["media"])
        for name, (path, query) in PAGES.items():
            self.assertEqual(seen[name], [200] * (FLOOD // 500), f"{name} during the flood")
            self.assertEqual(self.call(path, query=query)[0], 200, f"{name} right after the flood")


if __name__ == "__main__":
    unittest.main()
