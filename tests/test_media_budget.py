"""AUDIO-429: clip audio has its own per-session budget.

A clip's audio is a native <audio> element: its byte-Range GETs on
/api/v1/customer/records/{ref}/media are made by the browser's media
loader, which the app cannot pace. They used to spend the same session
bucket as page reads, so a clip (or WebKit's loader asking again at once
after a 429) emptied it and page reads were refused for seconds.

The rule under test: a browser session's media GETs spend a media bucket
of their own (MEDIA_BURST, MEDIA_PER_MINUTE) and a workspace media bucket
(WORKSPACE_MEDIA_PER_MINUTE), never the page-read buckets, and page reads
never spend the media ones. Both still pass the same in-flight caps. A
refused media request is refused before any workspace is opened, refills
nothing and costs nothing (AUDIO-429-FIX1, A429-SEC-R1-01): WebKit's loader
asks again at once after a refused read, and a refusal that cost a token
kept it refused, and the clip locked, for a minute or more. The refill
rate alone bounds what a session reads. Retry-After is computed after any
charge, so a client that waits it out is admitted on its first retry.
"""
from __future__ import annotations

import asyncio
import time
import unittest
from unittest import mock

from earwitness.app import asgi as _asgi
from earwitness.app import services as _services
from tests.test_uniform_refusals import _Base

GUARD_S = 5.0
T0 = 1_800_000_000.0


def run(coro):
    async def guarded():
        async with asyncio.timeout(GUARD_S):
            return await coro
    return asyncio.run(guarded())


def limiter(**kw):
    kw.setdefault("session_burst", _asgi.SESSION_BURST)
    return _asgi.RateLimiter(**kw)


async def admitted(lim, grant, ws, now, *, media=False, session=True):
    """admit, then release at once when admitted (the in-flight slot is
    not what these tests are about). Returns the admit result."""
    wait = await lim.admit(grant, ws, now, session=session, media=media)
    if wait == 0:
        await lim.release(grant, ws)
    return wait


class MediaBudgetSettingsTest(unittest.TestCase):
    def test_media_budget_is_its_own_and_bounded(self):
        self.assertGreater(_asgi.MEDIA_BURST, 0)
        self.assertGreater(_asgi.MEDIA_PER_MINUTE, 0)
        self.assertGreater(_asgi.WORKSPACE_MEDIA_PER_MINUTE,
                           _asgi.MEDIA_PER_MINUTE)
        # Page-read budgets are unchanged.
        self.assertEqual((_asgi.READS_PER_MINUTE, _asgi.SESSION_BURST,
                          _asgi.WORKSPACE_READS_PER_MINUTE),
                         (60, 40, 600))


class MediaBucketTest(unittest.TestCase):
    def test_media_past_its_burst_leaves_page_reads_admitted(self):
        lim = limiter()

        async def go():
            for _ in range(_asgi.MEDIA_BURST):
                self.assertEqual(await admitted(lim, "s", "c", T0, media=True), 0.0)
            self.assertGreater(await admitted(lim, "s", "c", T0, media=True), 0)
            for _ in range(_asgi.SESSION_BURST):
                self.assertEqual(await admitted(lim, "s", "c", T0), 0.0)
        run(go())

    def test_page_reads_past_their_burst_leave_media_admitted(self):
        lim = limiter()

        async def go():
            for _ in range(_asgi.SESSION_BURST):
                self.assertEqual(await admitted(lim, "s", "c", T0), 0.0)
            self.assertGreater(await admitted(lim, "s", "c", T0), 0)
            for _ in range(_asgi.MEDIA_BURST):
                self.assertEqual(await admitted(lim, "s", "c", T0, media=True), 0.0)
        run(go())

    def test_media_is_refused_past_its_own_burst_with_a_retry_after(self):
        lim = limiter()

        async def go():
            for _ in range(_asgi.MEDIA_BURST):
                self.assertEqual(await admitted(lim, "s", "c", T0, media=True), 0.0)
            wait = await admitted(lim, "s", "c", T0, media=True)
            self.assertGreater(wait, 0)
            self.assertLessEqual(wait, 60.0 / _asgi.MEDIA_PER_MINUTE * 2 + 1e-9)
            # It refills at the media rate: one token after 60/rate s.
            later = T0 + 60.0 / _asgi.MEDIA_PER_MINUTE * 3
            self.assertEqual(await admitted(lim, "s", "c", later, media=True), 0.0)
        run(go())

    def test_media_holds_the_session_in_flight_slots(self):
        lim = limiter(wait_s=0.2)

        async def go():
            for _ in range(_asgi.MAX_CONCURRENT_PER_GRANT):
                self.assertEqual(await lim.admit("s", "c", T0, session=True,
                                                 media=True), 0.0)
            # A page read now waits for a session slot and times out.
            self.assertGreater(await lim.admit("s", "c", T0, session=True), 0)
            self.assertEqual(lim._grants["s"], _asgi.MAX_CONCURRENT_PER_GRANT)
        run(go())

    def test_refusals_cost_nothing_and_never_refill(self):
        lim = limiter()
        per = 60.0 / _asgi.MEDIA_PER_MINUTE

        async def go():
            for _ in range(_asgi.MEDIA_BURST):
                await admitted(lim, "s", "c", T0, media=True)
            waits = [await admitted(lim, "s", "c", T0, media=True)
                     for _ in range(5 * _asgi.MEDIA_BURST)]
            # Every refusal waits for the same next token: none cost one,
            # none refilled one.
            self.assertEqual(set(waits), {per})
            self.assertEqual(lim._buckets["media:s"]["tokens"], 0.0)
            # It refills at the media rate, not the page rate.
            self.assertGreater(await admitted(
                lim, "s", "c", T0 + per * 0.95, media=True), 0)
            self.assertEqual(await admitted(
                lim, "s", "c", T0 + per + 1e-3, media=True), 0.0)
            # Page reads never paid for any of it.
            self.assertEqual(await admitted(lim, "s", "c", T0), 0.0)
        run(go())

    def test_a_refusal_flood_admits_at_most_burst_plus_the_refill(self):
        # The security review's flood: 1,390 media reads a second for 20 s
        # from one session. Admitted: at most one burst plus 20 s of refill,
        # and the flood never locks the session out of that refill.
        lim = limiter()
        rate, seconds = 1390, 20

        async def go():
            n = 0
            for i in range(rate * seconds):
                if await admitted(lim, "s", "c", T0 + i / rate, media=True) == 0:
                    n += 1
            return n
        n = run(go())
        bound = _asgi.MEDIA_BURST + seconds * _asgi.MEDIA_PER_MINUTE / 60.0
        self.assertLessEqual(n, bound)
        self.assertGreaterEqual(n, bound - 1)

    def test_a_client_that_obeys_retry_after_is_admitted_on_its_first_retry(self):
        lim = limiter()

        async def go():
            now = T0
            for _ in range(_asgi.MEDIA_BURST):
                await admitted(lim, "s", "c", now, media=True)
            for _ in range(5):
                wait = await admitted(lim, "s", "c", now, media=True)
                self.assertGreater(wait, 0)
                now += max(1, int(wait + 0.999)) + 0.05   # the header, +50 ms
                self.assertEqual(await admitted(lim, "s", "c", now, media=True), 0.0)
        run(go())

    def test_a_media_refusal_after_a_slot_wait_costs_nothing(self):
        lim = limiter(max_per_workspace=1, wait_s=2.0)

        async def go():
            self.assertEqual(await lim.admit("a", "c", T0, session=True,
                                             media=True), 0.0)
            waiter = asyncio.ensure_future(
                lim.admit("b", "c", T0, session=True, media=True))
            await asyncio.sleep(0.02)
            lim._buckets["media:b"]["tokens"] = 0.0   # spent meanwhile
            await lim.release("a", "c")
            self.assertGreater(await waiter, 0)
            self.assertEqual(lim._buckets["media:b"]["tokens"], 0.0)
            self.assertEqual((lim._workspaces["c"], lim._grants["b"]), (0, 0))
        run(go())

    def test_a_workspace_media_refusal_costs_nothing(self):
        lim = limiter()

        async def go():
            for _ in range(_asgi.WORKSPACE_MEDIA_PER_MINUTE):
                self.assertEqual(await lim.admit_workspace("acme", T0, media=True), 0.0)
                await lim.release_workspace("acme")
            for _ in range(50):
                self.assertGreater(await lim.admit_workspace("acme", T0, media=True), 0)
            self.assertEqual(lim._buckets["wsmedia:acme"]["tokens"], 0.0)
        run(go())

    def test_the_workspace_media_bound_holds_across_sessions(self):
        lim = limiter()
        cap = _asgi.WORKSPACE_MEDIA_PER_MINUTE

        async def go():
            n = 0
            sessions = cap // _asgi.MEDIA_BURST + 2
            refused = 0
            for s in range(sessions):
                for _ in range(_asgi.MEDIA_BURST):
                    self.assertEqual(await admitted(lim, f"s{s}", f"c{s}", T0,
                                                    media=True), 0.0)
                    wait = await lim.admit_workspace("acme", T0, media=True)
                    if wait == 0:
                        n += 1
                        await lim.release_workspace("acme")
                    else:
                        refused += 1
            self.assertEqual(n, cap)
            self.assertGreater(refused, 0)
            # Page reads of the workspace are not touched by it.
            self.assertEqual(await lim.admit_workspace("acme", T0), 0.0)
        run(go())

    def test_workspace_page_reads_do_not_drain_workspace_media(self):
        lim = limiter()

        async def go():
            for _ in range(_asgi.WORKSPACE_READS_PER_MINUTE):
                self.assertEqual(await lim.admit_workspace("acme", T0), 0.0)
                await lim.release_workspace("acme")
            self.assertGreater(await lim.admit_workspace("acme", T0), 0)
            self.assertEqual(await lim.admit_workspace("acme", T0, media=True), 0.0)
        run(go())


class MediaThroughTheAppTest(_Base):
    """Through the ASGI app with one browser session."""

    def setUp(self):
        super().setUp()
        self.app.limiter = _asgi.RateLimiter(session_burst=_asgi.SESSION_BURST)
        self.now = T0
        self.app.config.clock = lambda: self.now
        self.cookie = {"cookie": f"ew_session={self.http.session_id}"}

    def get(self, path):
        return self.call(path)[0]

    def call(self, path, range_=None):
        """GET through the app: (status, Retry-After or None)."""
        hdrs = [(b"host", b"127.0.0.1:8771"),
                (b"cookie", self.cookie["cookie"].encode())]
        if range_:
            hdrs.append((b"range", range_.encode()))
        scope = {"type": "http", "method": "GET", "path": path,
                 "scheme": "http", "headers": hdrs, "query_string": b""}
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        asyncio.run(self.app(scope, receive, send))
        start = next(m for m in sent if m["type"] == "http.response.start")
        after = dict(start.get("headers", [])).get(b"retry-after")
        return start["status"], None if after is None else int(after)

    def workspace_bucket(self, media):
        """The session's workspace bucket (page or media), made if new."""
        lim = self.app.limiter
        prefix = "wsmedia:" if media else "ws:"
        self.get(self.MEDIA if media else self.PAGE)
        names = [k for k in lim._buckets if k.startswith(prefix)
                 and not k.startswith(prefix + "caller:")]
        if not names:   # never charged there: make it, to report the miss
            return lim._ws_bucket("missing", self.now, media)
        self.assertEqual(len(names), 1)
        return lim._buckets[names[0]]

    MEDIA = "/api/v1/customer/records/sr1%3Aabc/media"
    PAGE = "/api/v1/customer/preferences"

    def test_a_media_burst_past_its_budget_leaves_page_reads_answered(self):
        statuses = [self.get(self.MEDIA) for _ in range(_asgi.MEDIA_BURST + 10)]
        self.assertNotIn(429, statuses[:_asgi.MEDIA_BURST])
        self.assertEqual(statuses[_asgi.MEDIA_BURST:], [429] * 10)
        self.assertNotEqual(self.get(self.PAGE), 429)

    def test_page_reads_past_their_budget_leave_a_clip_read_admitted(self):
        statuses = [self.get(self.PAGE) for _ in range(_asgi.SESSION_BURST + 3)]
        self.assertEqual(statuses[-3:], [429] * 3)
        self.assertNotEqual(self.get(self.MEDIA), 429)

    def test_a_refused_media_read_opens_no_workspace(self):
        for _ in range(_asgi.MEDIA_BURST):
            self.get(self.MEDIA)
        with mock.patch.object(_services, "open_customer_workspace",
                               side_effect=AssertionError("opened")) as opened:
            statuses = [self.get(self.MEDIA) for _ in range(20)]
        self.assertEqual(statuses, [429] * 20)
        self.assertEqual(opened.call_count, 0)

    def test_one_token_left_the_probe_plays_and_a_refusal_flood_ends_at_the_next_token(self):
        # WebKit reads a clip as bytes=0-1, then the rest. With one token
        # left the probe is admitted, the rest refused, and the loader asks
        # again at once, hundreds of times a second (A429-SEC-R1-01).
        for _ in range(_asgi.MEDIA_BURST - 1):
            self.get(self.MEDIA)
        self.assertNotEqual(self.call(self.MEDIA, "bytes=0-1")[0], 429)
        flood = [self.call(self.MEDIA, "bytes=0-") for _ in range(500)]
        self.assertEqual({status for status, _ in flood}, {429})
        per = 60.0 / _asgi.MEDIA_PER_MINUTE
        self.assertLessEqual(max(after for _, after in flood), per)
        # The next token, not a minute or more later: the clip plays.
        self.now += per + 0.05
        self.assertNotEqual(self.call(self.MEDIA, "bytes=0-")[0], 429)

    def test_a_client_that_obeys_retry_after_is_admitted_on_its_first_retry(self):
        for _ in range(_asgi.MEDIA_BURST):
            self.get(self.MEDIA)
        for _ in range(5):
            status, after = self.call(self.MEDIA)
            self.assertEqual(status, 429)
            self.now += after + 0.05
            self.assertNotEqual(self.get(self.MEDIA), 429)

    def test_a_workspace_refusal_waits_for_the_session_token_it_spent(self):
        # The session stage spends the session's last media token; the
        # workspace stage then refuses. Its Retry-After must cover the
        # session's next token too, or the obeying retry is refused.
        ws = self.workspace_bucket(media=True)
        for _ in range(_asgi.MEDIA_BURST - 2):
            self.get(self.MEDIA)
        ws["tokens"] = 0.9           # the workspace's next token in 0.02 s
        status, after = self.call(self.MEDIA)
        self.assertEqual(status, 429)
        self.now += after + 0.05
        self.assertNotEqual(self.get(self.MEDIA), 429)

    def test_the_workspace_stage_charges_a_clip_read_to_the_workspace_media_bucket(self):
        # A429-R1-02: the app passes media= to admit_workspace.
        self.workspace_bucket(media=False)["tokens"] = 0.0
        self.workspace_bucket(media=True)
        self.assertEqual(self.get(self.PAGE), 429)
        self.assertNotEqual(self.get(self.MEDIA), 429)

    def test_the_workspace_stage_charges_a_page_read_to_the_page_bucket(self):
        self.workspace_bucket(media=False)
        self.workspace_bucket(media=True)["tokens"] = 0.0
        self.assertEqual(self.get(self.MEDIA), 429)
        self.assertNotEqual(self.get(self.PAGE), 429)

    def test_a_workspace_refusal_spends_exactly_one_session_token(self):
        # A429-R2-01: the refusal keeps the session token admit spent (no
        # refund), and grant_wait's refill must not clamp either session
        # bucket below its own burst. The clock is frozen: nothing refills.
        lim = self.app.limiter

        def tokens():
            return {media: lim._buckets[next(
                k for k in lim._buckets
                if k.startswith("media:session:" if media else "session:"))]
                ["tokens"] for media in (False, True)}

        self.workspace_bucket(media=True)
        for media in (False, True):
            self.workspace_bucket(media=media)["tokens"] = 0.0
            before = tokens()
            self.assertEqual(self.get(self.MEDIA if media else self.PAGE), 429)
            want = dict(before)
            want[media] -= 1.0
            self.assertEqual(tokens(), want, "media" if media else "page")


if __name__ == "__main__":
    unittest.main()
