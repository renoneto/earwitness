"""LIMITER-WAIT (QA-full G2-02, DEP7b): a full in-flight cap waits briefly.

When a per-session (per-grant) or per-workspace in-flight cap is full, a
request waits for a slot, FIFO per cap and bounded (LIMIT_WAIT_S, 8 s),
before it answers 429 with Retry-After. Caps stay caps: nothing ever runs
over a cap, and a waiting request holds no slot until it is given one. A
waiting request whose client disconnects leaves the queue; a running one
frees its slot when it ends. The wait queue per cap is bounded too
(LIMIT_QUEUE_MAX); past it the answer is an immediate 429. The workspace
cap is configurable (EARWITNESS_WORKSPACE_MAX_IN_FLIGHT, default 10). The
per-minute rate buckets are unchanged and never wait.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import random
import resource
import selectors
import socket
import sys
import threading
import time
import unittest

from earwitness.app import asgi as _asgi
from earwitness.app import browser_auth as _ba
from earwitness.app import health as _health
from tests.test_uniform_refusals import _Base

GUARD_S = 5.0  # a test that would wait for ever fails instead of hanging


class _Watch:
    """A stand-in for the request's disconnect watch."""

    def __init__(self):
        self.future = None
        self.stopped = 0

    def gone(self):
        if self.future is None:
            self.future = asyncio.get_running_loop().create_future()
        return self.future

    def stop(self):
        self.stopped += 1

    def disconnect(self):
        self.gone().set_result(True)


def run(coro):
    async def guarded():
        async with asyncio.timeout(GUARD_S):
            return await coro
    return asyncio.run(guarded())


class SettingsTest(unittest.TestCase):
    def test_the_wait_bound_is_eight_seconds_and_the_queue_is_bounded(self):
        self.assertEqual(_asgi.LIMIT_WAIT_S, 8.0)
        self.assertEqual(_asgi.RateLimiter().wait_s, 8.0)
        self.assertGreaterEqual(_asgi.LIMIT_QUEUE_MAX, 16)
        self.assertEqual(_asgi.RateLimiter().queue_max, _asgi.LIMIT_QUEUE_MAX)

    def test_rate_buckets_and_session_cap_are_unchanged(self):
        self.assertEqual((_asgi.READS_PER_MINUTE, _asgi.BURST,
                          _asgi.SESSION_BURST, _asgi.WORKSPACE_READS_PER_MINUTE,
                          _asgi.MAX_CONCURRENT_PER_GRANT,
                          _asgi.MAX_CONCURRENT_PER_WORKSPACE),
                         (60, 20, 40, 600, 2, 10))

    def test_workspace_cap_defaults_to_ten(self):
        self.assertEqual(_asgi.WORKSPACE_MAX_IN_FLIGHT_ENV,
                         "EARWITNESS_WORKSPACE_MAX_IN_FLIGHT")
        self.assertEqual(_asgi.config_from_env({}).workspace_max_in_flight, 10)
        self.assertEqual(_asgi.HostedConfig().workspace_max_in_flight, 10)
        app = _asgi.HostedApp(_asgi.HostedConfig())
        self.addCleanup(app.close)
        self.assertEqual(app.limiter._max_ws, 10)
        self.assertEqual(app.limiter._max_grant, 2)

    def test_workspace_cap_set_to_sixteen_is_honoured(self):
        config = _asgi.config_from_env(
            {"EARWITNESS_WORKSPACE_MAX_IN_FLIGHT": "16"})
        self.assertEqual(config.workspace_max_in_flight, 16)
        app = _asgi.HostedApp(config)
        self.addCleanup(app.close)
        self.assertEqual(app.limiter._max_ws, 16)
        self.assertEqual(app.limiter._max_grant, 2)

        async def go():
            for n in range(16):
                self.assertEqual(await app.limiter.admit(
                    f"g{n}", "acme", time.time()), 0.0)
            started = time.monotonic()
            limiter = app.limiter
            limiter.wait_s = 0.2
            self.assertGreater(await limiter.admit("g99", "acme", time.time()), 0)
            self.assertGreaterEqual(time.monotonic() - started, 0.18)
        run(go())

    def test_a_bad_workspace_cap_is_refused_at_start_in_both_modes(self):
        for value in ("0", "-3", "x", "65", "1.5"):
            for mode in ("local-test", "production"):
                with self.subTest(value=value, mode=mode):
                    config = _asgi.config_from_env({
                        "EARWITNESS_MODE": mode,
                        "EARWITNESS_WORKSPACE_MAX_IN_FLIGHT": value})
                    problems = _health.check_config(config)
                    self.assertTrue(any("EARWITNESS_WORKSPACE_MAX_IN_FLIGHT" in p
                                        for p in problems), problems)
        ok = _asgi.config_from_env({"EARWITNESS_MODE": "local-test",
                                    "EARWITNESS_WORKSPACE_MAX_IN_FLIGHT": "16"})
        self.assertFalse(any("EARWITNESS_WORKSPACE_MAX_IN_FLIGHT" in p
                             for p in _health.check_config(ok)))


class LimiterWaitTest(unittest.TestCase):
    def limiter(self, **kw):
        kw.setdefault("max_per_workspace", 1)
        kw.setdefault("wait_s", 2.0)
        return _asgi.RateLimiter(**kw)

    def test_a_waiting_request_is_admitted_when_a_slot_frees(self):
        lim = self.limiter()

        async def go():
            self.assertEqual(await lim.admit("a", "ws", time.time()), 0.0)
            waiter = asyncio.ensure_future(lim.admit("b", "ws", time.time()))
            await asyncio.sleep(0.05)
            self.assertFalse(waiter.done())
            self.assertEqual(lim._workspaces["ws"], 1)
            self.assertEqual(lim.waiting(), 1)
            await lim.release("a", "ws")
            self.assertEqual(await waiter, 0.0)
            self.assertEqual(lim._workspaces["ws"], 1)
            self.assertEqual(lim.waiting(), 0)
            await lim.release("b", "ws")
            self.assertEqual(lim._workspaces["ws"], 0)
        run(go())

    def test_a_full_session_cap_waits_too(self):
        lim = self.limiter(max_per_workspace=10)

        async def go():
            for _ in range(2):
                self.assertEqual(await lim.admit("s", "ws", time.time(),
                                                 session=True), 0.0)
            waiter = asyncio.ensure_future(
                lim.admit("s", "ws", time.time(), session=True))
            await asyncio.sleep(0.05)
            self.assertFalse(waiter.done())
            self.assertEqual(lim._grants["s"], 2)
            await lim.release("s", "ws")
            self.assertEqual(await waiter, 0.0)
            self.assertEqual(lim._grants["s"], 2)
        run(go())

    def test_the_wait_times_out_to_a_retry_after(self):
        lim = self.limiter(wait_s=0.3)

        async def go():
            await lim.admit("a", "ws", time.time())
            started = time.monotonic()
            wait = await lim.admit("b", "ws", time.time())
            elapsed = time.monotonic() - started
            self.assertGreater(wait, 0)
            self.assertGreaterEqual(elapsed, 0.28)
            self.assertLess(elapsed, 1.5)
            self.assertEqual(lim.waiting(), 0)
            self.assertEqual(lim._workspaces["ws"], 1)
            await lim.release("a", "ws")
            self.assertEqual(lim._workspaces["ws"], 0)
        run(go())

    def test_admit_workspace_waits_and_times_out_too(self):
        lim = self.limiter(wait_s=0.3)

        async def go():
            self.assertEqual(await lim.admit_workspace("ws", time.time()), 0.0)
            waiter = asyncio.ensure_future(lim.admit_workspace("ws", time.time()))
            await asyncio.sleep(0.05)
            await lim.release_workspace("ws")
            self.assertEqual(await waiter, 0.0)
            started = time.monotonic()
            self.assertGreater(await lim.admit_workspace("ws", time.time()), 0)
            self.assertGreaterEqual(time.monotonic() - started, 0.28)
        run(go())

    def test_a_shared_deadline_bounds_both_stages(self):
        lim = self.limiter(wait_s=5.0)

        async def go():
            await lim.admit("a", "ws", time.time())
            loop = asyncio.get_running_loop()
            started = time.monotonic()
            wait = await lim.admit_workspace("ws", time.time(),
                                             deadline=loop.time() + 0.2)
            self.assertGreater(wait, 0)
            self.assertLess(time.monotonic() - started, 1.0)
        run(go())

    def test_waiters_are_admitted_in_arrival_order(self):
        lim = self.limiter()
        order = []

        async def worker(name):
            self.assertEqual(await lim.admit(name, "ws", time.time()), 0.0)
            order.append(name)
            await asyncio.sleep(0.01)
            await lim.release(name, "ws")

        async def go():
            await lim.admit("holder", "ws", time.time())
            tasks = []
            for name in ("w1", "w2", "w3"):
                tasks.append(asyncio.ensure_future(worker(name)))
                await asyncio.sleep(0.01)
            await lim.release("holder", "ws")
            # A newcomer arriving as the slot frees queues behind them.
            tasks.append(asyncio.ensure_future(worker("w4")))
            await asyncio.gather(*tasks)
            self.assertEqual(order, ["w1", "w2", "w3", "w4"])
        run(go())

    def test_a_full_queue_answers_at_once(self):
        lim = self.limiter(queue_max=2)

        async def go():
            await lim.admit("a", "ws", time.time())
            waiters = [asyncio.ensure_future(lim.admit(f"w{n}", "ws", time.time()))
                       for n in range(2)]
            await asyncio.sleep(0.05)
            started = time.monotonic()
            self.assertGreater(await lim.admit("late", "ws", time.time()), 0)
            self.assertLess(time.monotonic() - started, 0.1)
            self.assertEqual(lim.waiting(), 2)
            await lim.release("a", "ws")
            self.assertEqual(await waiters[0], 0.0)
            await lim.release("w0", "ws")
            self.assertEqual(await waiters[1], 0.0)
        run(go())

    def test_a_disconnect_while_waiting_leaves_the_queue_and_holds_nothing(self):
        lim = self.limiter()

        async def go():
            await lim.admit("a", "ws", time.time())
            watch = _Watch()
            waiter = asyncio.ensure_future(
                lim.admit("b", "ws", time.time(), watch=watch))
            await asyncio.sleep(0.05)
            self.assertEqual(lim.waiting(), 1)
            watch.disconnect()
            with self.assertRaises(_asgi.ClientGone):
                await waiter
            self.assertGreaterEqual(watch.stopped, 1)
            self.assertEqual(lim.waiting(), 0)
            self.assertEqual(lim._grants.get("b", 0), 0)
            self.assertEqual(lim._workspaces["ws"], 1)
            # The freed slot goes back to the pool, not to the gone waiter.
            await lim.release("a", "ws")
            self.assertEqual(lim._workspaces["ws"], 0)
            self.assertEqual(await lim.admit("c", "ws", time.time()), 0.0)
        run(go())

    def test_a_disconnect_while_waiting_on_the_workspace_keeps_its_session_slot_for_the_caller(self):
        lim = self.limiter()

        async def go():
            await lim.admit_workspace("ws", time.time())
            await lim.admit("s", "caller:s", time.time(), session=True)
            watch = _Watch()
            waiter = asyncio.ensure_future(
                lim.admit_workspace("ws", time.time(), watch=watch))
            await asyncio.sleep(0.05)
            watch.disconnect()
            with self.assertRaises(_asgi.ClientGone):
                await waiter
            # It held its session slot before the wait; the caller frees it.
            self.assertEqual(lim._grants["s"], 1)
            self.assertEqual(lim._workspaces["ws"], 1)
            await lim.release("s", "caller:s")
            await lim.release_workspace("ws")
            self.assertEqual((lim._grants["s"], lim._workspaces["ws"]), (0, 0))
        run(go())

    def test_a_cancelled_waiter_leaves_the_queue(self):
        lim = self.limiter()

        async def go():
            await lim.admit("a", "ws", time.time())
            waiter = asyncio.ensure_future(lim.admit("b", "ws", time.time()))
            await asyncio.sleep(0.05)
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            self.assertEqual(lim.waiting(), 0)
            await lim.release("a", "ws")
            self.assertEqual(lim._workspaces["ws"], 0)
        run(go())

    def test_a_waiter_cancelled_as_its_slot_arrives_passes_the_slot_on(self):
        lim = self.limiter()

        async def go():
            await lim.admit("a", "ws", time.time())
            first = asyncio.ensure_future(lim.admit("b", "ws", time.time()))
            await asyncio.sleep(0.02)
            second = asyncio.ensure_future(lim.admit("c", "ws", time.time()))
            await asyncio.sleep(0.02)
            await lim.release("a", "ws")   # handed to b ...
            first.cancel()                  # ... which is cancelled first
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertEqual(await second, 0.0)
            self.assertEqual(lim._workspaces["ws"], 1)
            await lim.release("c", "ws")
            self.assertEqual(lim._workspaces["ws"], 0)
        run(go())

    def test_rate_buckets_never_wait(self):
        lim = _asgi.RateLimiter(reads_per_minute=1, burst=1)

        async def go():
            now = time.time()
            self.assertEqual(await lim.admit("a", "ws", now), 0.0)
            await lim.release("a", "ws")
            started = time.monotonic()
            wait = await lim.admit("a", "ws", now)
            self.assertLess(time.monotonic() - started, 0.1)
            self.assertGreater(wait, 30)
            self.assertEqual(lim.waiting(), 0)
        run(go())

    def test_many_concurrent_requests_never_exceed_either_cap(self):
        lim = _asgi.RateLimiter(max_per_workspace=3, max_per_grant=2,
                                session_burst=10_000, burst=10_000,
                                workspace_per_minute=1_000_000,
                                reads_per_minute=1_000_000,
                                wait_s=4.0, queue_max=1_000,
                                grant_queue_max=1_000, waiters_max=1_000)
        rng = random.Random(7)
        live = {"ws": 0}
        per_grant = {}
        peak = {"ws": 0, "grant": 0}
        outcomes = []

        async def request(n):
            await asyncio.sleep(rng.random() * 0.3)
            grant = f"g{n % 10}"
            wait = await lim.admit(grant, "ws", time.time())
            outcomes.append(wait)
            if wait:
                return
            live["ws"] += 1
            per_grant[grant] = per_grant.get(grant, 0) + 1
            peak["ws"] = max(peak["ws"], live["ws"])
            peak["grant"] = max(peak["grant"], per_grant[grant])
            self.assertLessEqual(live["ws"], 3)
            self.assertLessEqual(per_grant[grant], 2)
            self.assertLessEqual(lim._workspaces["ws"], 3)
            try:
                for _ in range(rng.randint(0, 3)):
                    await asyncio.sleep(0)
                await asyncio.sleep(0.001 + rng.random() * 0.006)
            finally:
                live["ws"] -= 1
                per_grant[grant] -= 1
                await lim.release(grant, "ws")

        async def go():
            await asyncio.gather(*(request(n) for n in range(200)))
        run(go())
        self.assertEqual(outcomes.count(0.0), 200, "every request got a slot")
        self.assertEqual(peak["ws"], 3)
        self.assertLessEqual(peak["grant"], 2)
        self.assertEqual(lim._workspaces["ws"], 0)
        self.assertEqual(sum(lim._grants.values()), 0)
        self.assertEqual(lim.waiting(), 0)


class HandoverRaceTest(unittest.TestCase):
    """LW-R1-02: a slot handed over in the same loop step as a timeout,
    disconnect or drained bucket is passed on or freed, never kept by a
    request that does not run (mutants B, D, K)."""

    def test_a_disconnect_in_the_same_step_as_a_handover_passes_the_slot_on(self):
        lim = _asgi.RateLimiter(max_per_workspace=1, wait_s=2.0)

        async def go():
            await lim.admit("a", "ws", time.time())
            watch = _Watch()
            gone = asyncio.ensure_future(
                lim.admit("b", "ws", time.time(), watch=watch))
            await asyncio.sleep(0.02)
            nxt = asyncio.ensure_future(lim.admit("c", "ws", time.time()))
            await asyncio.sleep(0.02)
            await lim.release("a", "ws")   # handed to b ...
            watch.disconnect()              # ... which left in the same step
            with self.assertRaises(_asgi.ClientGone):
                await gone
            self.assertEqual(await nxt, 0.0)   # passed on to c
            self.assertEqual((lim._workspaces["ws"], lim._grants.get("b", 0)),
                             (1, 0))
            await lim.release("c", "ws")
            self.assertEqual((lim._workspaces["ws"], lim.waiting()), (0, 0))
        run(go())

    def test_a_disconnect_with_a_handover_and_nobody_behind_frees_the_slot(self):
        lim = _asgi.RateLimiter(max_per_workspace=1, wait_s=2.0)

        async def go():
            await lim.admit("a", "ws", time.time())
            watch = _Watch()
            gone = asyncio.ensure_future(
                lim.admit("b", "ws", time.time(), watch=watch))
            await asyncio.sleep(0.02)
            await lim.release("a", "ws")
            watch.disconnect()
            with self.assertRaises(_asgi.ClientGone):
                await gone
            self.assertEqual((lim._workspaces["ws"], lim._grants.get("b", 0)),
                             (0, 0))
            self.assertEqual(await lim.admit("c", "ws", time.time()), 0.0)
        run(go())

    def test_a_handover_that_lands_at_the_deadline_is_kept_or_freed_never_lost(self):
        lim = _asgi.RateLimiter(max_per_workspace=1, wait_s=0.1)

        async def go():
            await lim.admit("a", "ws", time.time())
            waiter = asyncio.ensure_future(lim.admit("b", "ws", time.time()))
            await asyncio.sleep(0.02)
            time.sleep(0.15)               # the loop stalls past b's deadline ...
            await lim.release("a", "ws")   # ... and the slot reaches b then
            wait = await waiter
            if wait == 0.0:
                self.assertEqual(lim._workspaces["ws"], 1)
                await lim.release("b", "ws")
            self.assertEqual((lim._workspaces["ws"], sum(lim._grants.values()),
                              lim.waiting()), (0, 0, 0))
        run(go())

    def test_a_bucket_drained_while_waiting_refuses_and_frees_both_slots(self):
        lim = _asgi.RateLimiter(max_per_workspace=1, wait_s=2.0)

        async def go():
            await lim.admit("a", "ws", time.time())
            waiter = asyncio.ensure_future(lim.admit("b", "ws", time.time()))
            await asyncio.sleep(0.02)
            self.assertEqual(lim._grants["b"], 1)   # holds its grant slot
            lim._buckets["ws:ws"]["tokens"] = 0.0     # spent meanwhile
            await lim.release("a", "ws")
            self.assertGreater(await waiter, 0)
            self.assertEqual((lim._workspaces["ws"], lim._grants["b"],
                              lim.waiting()), (0, 0, 0))
        run(go())

    def test_admit_workspace_with_a_drained_bucket_frees_its_slot(self):
        lim = _asgi.RateLimiter(max_per_workspace=1, wait_s=2.0)

        async def go():
            await lim.admit_workspace("ws", time.time())
            waiter = asyncio.ensure_future(lim.admit_workspace("ws", time.time()))
            await asyncio.sleep(0.02)
            lim._buckets["ws:ws"]["tokens"] = 0.0
            await lim.release_workspace("ws")
            self.assertGreater(await waiter, 0)
            self.assertEqual((lim._workspaces["ws"], lim.waiting()), (0, 0))
        run(go())


class ReplayTest(unittest.TestCase):
    """LW-R1-02 (mutant E): what the watch read is handed to the handler."""

    def test_a_whole_empty_body_read_while_waiting_is_replayed(self):
        async def go():
            first = {"type": "http.request", "body": b"", "more_body": False}
            sent = [False]

            async def receive():
                if not sent[0]:
                    sent[0] = True
                    return first
                await asyncio.Event().wait()

            watch = _asgi._Receive(receive)
            gone = watch.gone()
            await asyncio.sleep(0.02)
            self.assertTrue(sent[0])
            self.assertFalse(gone.done())
            watch.stop()
            self.assertEqual(await asyncio.wait_for(watch(), 0.5), first)
        run(go())

    def test_a_disconnect_read_while_waiting_is_replayed(self):
        async def go():
            sent = [False]

            async def receive():   # a transport says it once
                if sent[0]:
                    await asyncio.Event().wait()
                sent[0] = True
                return {"type": "http.disconnect"}

            watch = _asgi._Receive(receive)
            reason = await asyncio.wait_for(watch.gone(), 0.5)
            self.assertIsInstance(reason, _asgi.ClientGone)
            watch.stop()
            self.assertEqual(await asyncio.wait_for(watch(), 0.5),
                             {"type": "http.disconnect"})
        run(go())


class AppWaitTest(_Base):
    """Through the ASGI app: a browser session's read waits for a slot."""

    OWNER = {"cookie": "ew_session=sess-k02-1", "x-csrf-token": "tok123",
             "origin": "http://127.0.0.1:8771"}

    def setUp(self):
        super().setUp()
        self.app.limiter = _asgi.RateLimiter(
            session_burst=_asgi.SESSION_BURST, max_per_workspace=1, wait_s=2.0)

    def scope(self, path="/api/v1/customer/preferences"):
        return {"type": "http", "method": "GET", "path": path, "scheme": "http",
                "headers": [(b"host", b"127.0.0.1:8771")] +
                           [(k.encode(), v.encode()) for k, v in self.OWNER.items()],
                "query_string": b""}

    def start(self, *, hold=None, gone=None):
        """Start one request; hold (an Event) keeps its reply unsent and
        so its slot taken; gone (an Event) disconnects the client."""
        messages = []
        delivered = [False]

        async def receive():
            if not delivered[0]:
                delivered[0] = True
                return {"type": "http.request", "body": b"", "more_body": False}
            if gone is None:
                await asyncio.Event().wait()
            await gone.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if hold is not None:
                await hold.wait()
            messages.append(message)

        task = asyncio.ensure_future(self.app(self.scope(), receive, send))
        return task, messages

    @staticmethod
    def status(messages):
        return messages[0]["status"] if messages else None

    def test_a_read_waits_for_the_workspace_slot_instead_of_429(self):
        async def go():
            hold = asyncio.Event()
            first, first_msgs = self.start(hold=hold)
            await asyncio.sleep(0.2)
            self.assertEqual(self.app.limiter._workspaces.get("acme"), 1)
            second, second_msgs = self.start()
            await asyncio.sleep(0.2)
            self.assertFalse(second.done())
            hold.set()
            await asyncio.gather(first, second)
            self.assertEqual(self.status(first_msgs), 200)
            self.assertEqual(self.status(second_msgs), 200)
            self.assertEqual(self.app.limiter._workspaces.get("acme"), 0)
            self.assertEqual(sum(self.app.limiter._grants.values()), 0)
        run(go())

    def test_a_read_that_waits_too_long_gets_429_with_retry_after(self):
        self.app.limiter.wait_s = 0.3

        async def go():
            hold = asyncio.Event()
            first, _ = self.start(hold=hold)
            await asyncio.sleep(0.2)
            started = time.monotonic()
            second, second_msgs = self.start()
            await second
            self.assertGreaterEqual(time.monotonic() - started, 0.28)
            self.assertEqual(self.status(second_msgs), 429)
            headers = dict(second_msgs[0]["headers"])
            self.assertEqual(headers[b"retry-after"], b"1")
            body = json.loads(b"".join(m.get("body", b"") for m in second_msgs))
            self.assertEqual(body["error"]["code"], "RATE_LIMITED")
            hold.set()
            await first
            self.assertEqual(self.app.limiter._workspaces.get("acme"), 0)
            self.assertEqual(sum(self.app.limiter._grants.values()), 0)
        run(go())

    def test_a_client_that_leaves_while_waiting_gets_nothing_and_holds_nothing(self):
        async def go():
            hold = asyncio.Event()
            first, _ = self.start(hold=hold)
            await asyncio.sleep(0.2)
            gone = asyncio.Event()
            second, second_msgs = self.start(gone=gone)
            await asyncio.sleep(0.2)
            self.assertEqual(self.app.limiter.waiting(), 1)
            gone.set()
            await second
            self.assertEqual(second_msgs, [])
            self.assertEqual(self.app.limiter.waiting(), 0)
            # Its session slot is freed; only the first request holds one.
            self.assertEqual(sum(self.app.limiter._grants.values()), 1)
            hold.set()
            await first
            self.assertEqual(self.app.limiter._workspaces.get("acme"), 0)
            self.assertEqual(sum(self.app.limiter._grants.values()), 0)
        run(go())

    def test_a_running_request_that_disconnects_frees_its_slot(self):
        async def go():
            hold = asyncio.Event()
            first, _ = self.start(hold=hold)
            await asyncio.sleep(0.2)
            second, second_msgs = self.start()
            await asyncio.sleep(0.1)
            first.cancel()   # the server drops the running request
            with self.assertRaises(asyncio.CancelledError):
                await first
            await second
            self.assertEqual(self.status(second_msgs), 200)
            self.assertEqual(self.app.limiter._workspaces.get("acme"), 0)
            self.assertEqual(sum(self.app.limiter._grants.values()), 0)
        run(go())

    def test_a_running_request_whose_send_fails_frees_its_slot(self):
        async def go():
            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                raise OSError("client went away")

            with self.assertRaises(OSError):
                await self.app(self.scope(), receive, send)
            self.assertEqual(self.app.limiter._workspaces.get("acme"), 0)
            self.assertEqual(sum(self.app.limiter._grants.values()), 0)
        run(go())


class WorkspaceStageWaitTest(_Base):
    """LW-R1-02 (mutants G, J): the WORKSPACE stage is the one that waits.
    Workspace cap 3, per-session cap 2, three sessions: the session and
    caller stages have room, the workspace is full."""

    scope = AppWaitTest.scope
    start = AppWaitTest.start
    status = staticmethod(AppWaitTest.status)

    def setUp(self):
        super().setUp()
        self.app.limiter = _asgi.RateLimiter(
            session_burst=_asgi.SESSION_BURST, max_per_workspace=3, wait_s=2.0)
        for name in ("a", "b", "c"):
            self.session("owner", active="acme", email="owner@x.example",
                         verified=True, identity=f"ws-stage-{name}")

    def start_as(self, name, **kw):
        self.OWNER = self.sessions[f"ws-stage-{name}"]
        return self.start(**kw)

    def fill(self):
        """a runs 2, b runs 1: the workspace (cap 3) is full."""
        holds = [asyncio.Event() for _ in range(3)]
        tasks = [self.start_as(n, hold=h)[0] for n, h in zip("aab", holds)]
        return holds, tasks

    def session_slots(self, name):
        throttle = "session:" + _ba._hash_presented(f"ws-stage-{name}")
        lim = self.app.limiter
        return (lim._grants.get(throttle, 0),
                lim._workspaces.get("caller:" + throttle, 0))

    def test_a_read_waits_at_the_workspace_stage_and_is_admitted(self):
        async def go():
            holds, tasks = self.fill()
            await asyncio.sleep(0.2)
            lim = self.app.limiter
            self.assertEqual(lim._workspaces.get("acme"), 3)
            waiter, msgs = self.start_as("c")
            await asyncio.sleep(0.2)
            self.assertEqual(list(lim._queues), [("workspace", "acme")])
            self.assertEqual(self.session_slots("c"), (1, 1))
            holds[2].set()
            await asyncio.gather(tasks[2], waiter)
            self.assertEqual(self.status(msgs), 200)
            for h in holds:
                h.set()
            await asyncio.gather(*tasks)
            self.assertEqual((lim._workspaces.get("acme"),
                              sum(lim._grants.values()), lim.waiting()), (0, 0, 0))
        run(go())

    def test_a_client_that_leaves_at_the_workspace_stage_frees_its_session_slots(self):
        async def go():
            holds, tasks = self.fill()
            await asyncio.sleep(0.2)
            gone = asyncio.Event()
            waiter, msgs = self.start_as("c", gone=gone)
            await asyncio.sleep(0.2)
            lim = self.app.limiter
            self.assertEqual(list(lim._queues), [("workspace", "acme")])
            self.assertEqual(self.session_slots("c"), (1, 1))
            gone.set()
            await waiter
            self.assertEqual(msgs, [])
            self.assertEqual(self.session_slots("c"), (0, 0))
            self.assertEqual((lim._workspaces.get("acme"), lim.waiting()), (3, 0))
            for h in holds:
                h.set()
            await asyncio.gather(*tasks)
            self.assertEqual((lim._workspaces.get("acme"),
                              sum(lim._grants.values())), (0, 0))
        run(go())

    def test_one_deadline_covers_the_session_and_workspace_stages(self):
        self.app.limiter.wait_s = 1.0

        async def go():
            lim = self.app.limiter
            a_holds = [asyncio.Event(), asyncio.Event()]
            a_tasks = [self.start_as("a", hold=h)[0] for h in a_holds]
            c_hold = asyncio.Event()
            c_first = self.start_as("c", hold=c_hold)[0]
            await asyncio.sleep(0.15)
            self.assertEqual(lim._workspaces.get("acme"), 3)
            r2_hold = asyncio.Event()
            r2, r2_msgs = self.start_as("c", hold=r2_hold)  # workspace stage
            await asyncio.sleep(0.05)
            started = time.monotonic()
            r3, r3_msgs = self.start_as("c")                # session stage
            await asyncio.sleep(0.5)
            # c's first read ends: its session slot goes to r3, its
            # workspace slot to r2; r3 then waits for the workspace.
            c_hold.set()
            await c_first
            await r3
            elapsed = time.monotonic() - started
            self.assertEqual(self.status(r3_msgs), 429)
            self.assertGreaterEqual(elapsed, 0.9)
            self.assertLess(elapsed, 1.3)   # not 0.5 + a fresh 1.0
            r2_hold.set()
            for h in a_holds:
                h.set()
            await asyncio.gather(r2, *a_tasks)
            self.assertEqual(self.status(r2_msgs), 200)
            self.assertEqual((lim._workspaces.get("acme"),
                              sum(lim._grants.values()), lim.waiting()), (0, 0, 0))
        run(go())


class LimitReasonTest(unittest.TestCase):
    """LW-SEC-R1-05: every limiter refusal names its reason (no caller
    identifiers): bucket, queue_full, cap (the process waiter limit) or
    timeout."""

    def test_each_refusal_names_its_reason(self):
        async def go():
            lim = _asgi.RateLimiter(max_per_workspace=1, wait_s=0.1, queue_max=1)
            await lim.admit("a", "ws", time.time())
            waiter = asyncio.ensure_future(lim.admit("b", "ws", time.time()))
            await asyncio.sleep(0.02)
            self.assertGreater(await lim.admit("c", "ws", time.time()), 0)
            self.assertEqual(_asgi._LIMIT_REASON.get(), "queue_full")
            self.assertGreater(await waiter, 0)
            self.assertGreater(await lim.admit("d", "ws", time.time()), 0)
            self.assertEqual(_asgi._LIMIT_REASON.get(), "timeout")
            lim.waiters_max = 0
            self.assertGreater(await lim.admit("e", "ws", time.time()), 0)
            self.assertEqual(_asgi._LIMIT_REASON.get(), "cap")
            dry = _asgi.RateLimiter(reads_per_minute=1, burst=1)
            await dry.admit("f", "ws", time.time())
            await dry.release("f", "ws")
            self.assertGreater(await dry.admit("f", "ws", time.time()), 0)
            self.assertEqual(_asgi._LIMIT_REASON.get(), "bucket")
        run(go())


class LimitLogTest(_Base):
    """LW-SEC-R1-05 through the app: request.complete carries limit=<reason>
    for a limiter 429 and for a client that left while waiting (status 0,
    nothing sent). LW-R1-04: the bearer 429 carries Retry-After too."""

    scope = AppWaitTest.scope
    start = AppWaitTest.start
    status = staticmethod(AppWaitTest.status)
    OWNER = AppWaitTest.OWNER

    def setUp(self):
        super().setUp()
        self.app.limiter = _asgi.RateLimiter(
            session_burst=_asgi.SESSION_BURST, max_per_workspace=1, wait_s=0.3)

    @staticmethod
    def completes(logs):
        found = []
        for line in logs.output:
            record = json.loads(line.split(":", 2)[2])
            if record["event"] == "request.complete":
                found.append(record)
        return found

    def test_a_timed_out_wait_and_a_client_that_left_are_logged_with_a_reason(self):
        async def go():
            hold = asyncio.Event()
            first, _ = self.start(hold=hold)
            await asyncio.sleep(0.1)
            timed_out, msgs = self.start()
            await timed_out
            self.assertEqual(self.status(msgs), 429)
            gone = asyncio.Event()
            left, left_msgs = self.start(gone=gone)
            await asyncio.sleep(0.1)
            gone.set()
            await left
            self.assertEqual(left_msgs, [])
            hold.set()
            await first

        with self.assertLogs("earwitness.ops", level="INFO") as logs:
            run(go())
        done = self.completes(logs)
        self.assertEqual([(r["status"], r.get("limit")) for r in done],
                         [(429, "timeout"), (0, "client_gone"), (200, None)])
        for record in done:
            self.assertNotIn("sess-k02-1", json.dumps(record))

    def test_the_bearer_429_has_retry_after(self):
        self.app.limiter._workspaces["acme"] = 1   # the workspace is full
        self.app.limiter.wait_s = 0.1
        messages = []

        async def go():
            scope = {"type": "http", "method": "GET",
                     "path": "/api/v1/customer/preferences", "scheme": "http",
                     "query_string": b"",
                     "headers": [(b"host", b"127.0.0.1:8771"),
                                 (b"authorization",
                                  f"Bearer {self.http.reader_token}".encode())]}

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                messages.append(message)

            await self.app(scope, receive, send)

        with self.assertLogs("earwitness.ops", level="INFO") as logs:
            run(go())
        self.assertEqual(messages[0]["status"], 429)
        self.assertEqual(dict(messages[0]["headers"])[b"retry-after"], b"1")
        self.assertEqual(self.completes(logs)[-1].get("limit"), "timeout")


def _peak_rss() -> int:
    """This process's peak resident size in bytes."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


class BodyWhileWaitingTest(_Base):
    """LW-R1-01 / LW-SEC-R1-01: the disconnect watch keeps no body bytes.

    A route whose body is not read before routing (GET, for one) may still
    carry one. While such a request waits for a slot, the watch reads at
    most one message of it, keeps none of its bytes, and the request is
    refused with the too-large answer; the rest stays unread, so the
    transport's flow control holds it back."""

    OWNER = {"cookie": "ew_session=sess-k02-1", "x-csrf-token": "tok123",
             "origin": "http://127.0.0.1:8771"}
    CHUNK = 64 * 1024

    def setUp(self):
        super().setUp()
        self.app.limiter = _asgi.RateLimiter(
            session_burst=_asgi.SESSION_BURST, wait_s=2.0)

    def request(self, receive, *, method="GET", hold=None, headers=(),
                path="/api/v1/customer/preferences"):
        messages = []

        async def send(message):
            if hold is not None:
                await hold.wait()
            messages.append(message)

        scope = {"type": "http", "method": method, "path": path,
                 "scheme": "http", "query_string": b"",
                 "headers": [(b"host", b"127.0.0.1:8771")] +
                 [(k.encode(), v.encode()) for k, v in self.OWNER.items()] +
                 list(headers)}
        return asyncio.ensure_future(self.app(scope, receive, send)), messages

    @staticmethod
    def idle():
        delivered = [False]

        async def receive():
            if not delivered[0]:
                delivered[0] = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.Event().wait()
        return receive

    def stream(self, total, pulls):
        """A body of `total` bytes in fresh 64 KiB chunks, then silence."""
        sent = [0]

        async def receive():
            if sent[0] >= total:
                await asyncio.Event().wait()
            pulls[0] += 1
            await asyncio.sleep(0)
            size = min(self.CHUNK, total - sent[0])
            sent[0] += size
            return {"type": "http.request", "body": b"x" * size,
                    "more_body": sent[0] < total}
        return receive

    def test_the_watch_keeps_no_body_bytes_and_reports_the_body(self):
        pulls = [0]

        async def go():
            watch = _asgi._Receive(self.stream(200 * 1024 * 1024, pulls))
            reason = await asyncio.wait_for(watch.gone(), 1.0)
            self.assertIsInstance(reason, _asgi.BodyWhileWaiting)
            self.assertIsInstance(reason, _asgi.ClientGone)
            self.assertEqual(pulls[0], 1)
            self.assertEqual(sum(len(m.get("body", b"")) for m in watch._kept), 0)
            watch.stop()
        run(go())

    def test_a_streamed_get_body_while_waiting_is_refused_and_not_kept(self):
        total = 200 * 1024 * 1024
        pulls = [0]

        async def go():
            hold = asyncio.Event()
            held = [self.request(self.idle(), hold=hold) for _ in range(2)]
            await asyncio.sleep(0.2)
            self.assertEqual(sum(self.app.limiter._grants.values()), 2)
            before = _peak_rss()
            task, messages = self.request(
                self.stream(total, pulls),
                headers=[(b"content-length", str(total).encode())])
            await asyncio.wait_for(task, 1.5)   # refused, not timed out
            grown = _peak_rss() - before
            self.assertLess(grown, 5 * 1024 * 1024, grown)
            self.assertLessEqual(pulls[0], 2)
            self.assertEqual(messages[0]["status"], 413)
            headers = dict(messages[0]["headers"])
            self.assertEqual(headers[b"connection"], b"close")
            body = json.loads(b"".join(m.get("body", b"") for m in messages))
            self.assertEqual(body["error"]["code"], "INVALID_ARGUMENT")
            self.assertEqual(self.app.limiter.waiting(), 0)
            self.assertEqual(sum(self.app.limiter._grants.values()), 2)
            hold.set()
            await asyncio.gather(*(t for t, _ in held))
            self.assertEqual(sum(self.app.limiter._grants.values()), 0)
            self.assertEqual(sum(self.app.limiter._workspaces.values()), 0)
        run(go())

    def test_a_post_over_two_megabytes_is_refused_as_before(self):
        async def go():
            for declared in (True, False):
                pulls = [0]
                total = 3_000_000
                headers = [(b"content-length", str(total).encode())] if declared else []
                task, messages = self.request(
                    self.stream(total, pulls), method="POST", headers=headers)
                await asyncio.wait_for(task, 2.0)
                self.assertEqual(messages[0]["status"], 413)
                self.assertLessEqual(pulls[0], _asgi.MAX_BODY_BYTES // self.CHUNK + 1)
                if declared:
                    self.assertEqual(pulls[0], 0)
            self.assertEqual(sum(self.app.limiter._grants.values()), 0)
        run(go())


class WaiterLimitTest(unittest.TestCase):
    """LW-SEC-R1-02: waiting is bounded per session and process-wide, so
    waits can never use up the server's connections."""

    def test_the_bounds(self):
        self.assertEqual(_asgi.LIMIT_QUEUE_MAX, 32)          # per workspace
        self.assertEqual(_asgi.LIMIT_GRANT_QUEUE_MAX, 4)     # per session/grant
        self.assertEqual(_asgi.LIMIT_WAITERS_MAX, 64)        # process-wide
        lim = _asgi.RateLimiter()
        self.assertEqual((lim.queue_max, lim.grant_queue_max, lim.waiters_max),
                         (32, 4, 64))

    def test_a_session_queues_at_most_four_more_than_its_cap(self):
        lim = _asgi.RateLimiter(wait_s=2.0)

        async def go():
            for _ in range(2):
                self.assertEqual(await lim.admit("s", "ws", time.time(),
                                                 session=True), 0.0)
            waiters = [asyncio.ensure_future(
                lim.admit("s", "ws", time.time(), session=True)) for _ in range(4)]
            await asyncio.sleep(0.05)
            self.assertEqual(lim.waiting(), 4)
            started = time.monotonic()
            self.assertGreater(await lim.admit("s", "ws", time.time(),
                                               session=True), 0)
            self.assertLess(time.monotonic() - started, 0.1)
            self.assertEqual(lim.waiting(), 4)
            for _ in range(6):
                await lim.release("s", "ws")
            self.assertEqual(await asyncio.gather(*waiters), [0.0] * 4)
            self.assertEqual((lim._grants["s"], lim.waiting()), (0, 0))
        run(go())

    def test_the_process_waiter_limit_holds_across_sessions(self):
        lim = _asgi.RateLimiter(max_per_workspace=1, wait_s=2.0)

        async def go():
            for n in range(100):
                self.assertEqual(await lim.admit(f"a{n}", f"w{n}", time.time()), 0.0)
            started = time.monotonic()
            tries = [asyncio.ensure_future(lim.admit(f"a{n}", f"w{n}", time.time()))
                     for n in range(100)]
            await asyncio.sleep(0.1)
            self.assertEqual(lim.waiting(), 64)
            refused = [t for t in tries if t.done()]
            self.assertEqual(len(refused), 36)
            self.assertTrue(all(t.result() > 0 for t in refused))
            self.assertLess(time.monotonic() - started, 0.5)
            # A judge whose workspace has room still gets a slot at once ...
            self.assertEqual(await lim.admit("judge", "judges", time.time()), 0.0)
            # ... and one whose workspace is full gets a prompt refusal.
            started = time.monotonic()
            self.assertGreater(await lim.admit("judge2", "w0", time.time()), 0)
            self.assertLess(time.monotonic() - started, 0.1)
            for n in range(100):
                await lim.release(f"a{n}", f"w{n}")
            await asyncio.gather(*tries)
            self.assertEqual(lim.waiting(), 0)
        run(go())

    def test_the_process_limit_follows_uvicorn_limit_concurrency(self):
        cases = (({}, [], 64),
                 ({}, ["uvicorn", "--limit-concurrency", "256"], 64),
                 ({}, ["uvicorn", "--limit-concurrency=40"], 10),
                 ({"UVICORN_LIMIT_CONCURRENCY": "16"}, [], 4),
                 ({"UVICORN_LIMIT_CONCURRENCY": "2"}, [], 1),
                 ({"UVICORN_LIMIT_CONCURRENCY": "x"}, ["--limit-concurrency"], 64))
        for env, argv, expected in cases:
            with self.subTest(env=env, argv=argv):
                config = _asgi.config_from_env(env, argv=argv)
                self.assertEqual(config.limit_waiters, expected)
                app = _asgi.HostedApp(config)
                self.addCleanup(app.close)
                self.assertEqual(app.limiter.waiters_max, expected)


@unittest.skipUnless(importlib.util.find_spec("uvicorn"), "needs uvicorn")
class RealUvicornWaiterLimitTest(_Base):
    """Over a real socket: waiting requests from many sessions cannot hold
    uvicorn's --limit-concurrency; a judge gets an answer, never 503."""

    LIMIT = 16  # uvicorn limit_concurrency; the waiter limit is a quarter

    def serve(self):
        import uvicorn
        server = uvicorn.Server(uvicorn.Config(
            self.app, host="127.0.0.1", port=0, lifespan="off",
            log_level="warning", access_log=False,
            limit_concurrency=self.LIMIT))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(setattr, server, "should_exit", True)
        deadline = time.monotonic() + 5
        while not server.started:
            self.assertLess(time.monotonic(), deadline, "no start")
            time.sleep(0.02)
        return server.servers[0].sockets[0].getsockname()[1]

    def open(self, port, cookie):
        sock = socket.create_connection(("127.0.0.1", port), 3)
        self.addCleanup(sock.close)
        sock.sendall(b"GET /api/v1/customer/preferences HTTP/1.1\r\n"
                     b"Host: 127.0.0.1:8771\r\nCookie: ew_session=" +
                     cookie.encode() + b"\r\n\r\n")
        return sock

    @staticmethod
    def answers(socks, within):
        """Status codes that arrive within `within` seconds, by socket."""
        found = {}
        sel = selectors.DefaultSelector()
        for sock in socks:
            sel.register(sock, selectors.EVENT_READ)
        end = time.monotonic() + within
        while len(found) < len(socks) and time.monotonic() < end:
            for key, _ in sel.select(max(0.0, end - time.monotonic())):
                data = key.fileobj.recv(4096)
                found[key.fileobj] = int(data.split(b" ")[1]) if data else 0
                sel.unregister(key.fileobj)
        sel.close()
        return found

    def test_waiters_cannot_crowd_a_judge_out_with_503(self):
        self.app.limiter = _asgi.RateLimiter(
            session_burst=_asgi.SESSION_BURST, max_per_workspace=2,
            wait_s=6.0, waiters_max=self.LIMIT // 4)
        port = self.serve()
        # The judges' workspace is full: its slots are held throughout.
        self.app.limiter._workspaces["acme"] = 2
        room = self.LIMIT // 4
        # Two waves of 4 other sessions, 2 requests each. A refused client
        # closes and comes back; only the waiters keep their connections.
        # Without the process limit the second wave finds every
        # connection taken (503) instead of a prompt 429.
        for wave in range(2):
            socks = []
            for n in range(4):
                identity = f"crowd-{wave}-{n}"
                self.session("owner", active="acme", email="owner@x.example",
                             verified=True, identity=identity)
                socks += [self.open(port, identity) for _ in range(2)]
            got = self.answers(socks, 1.0)
            expected = 8 - room if wave == 0 else 8
            self.assertEqual(sorted(got.values()), [429] * expected, got)
            for sock in got:
                sock.close()
            time.sleep(0.2)
            self.assertEqual(self.app.limiter.waiting(), room)
        self.session("owner", active="acme", email="owner@x.example",
                     verified=True, identity="judge-new")
        started = time.monotonic()
        judge = self.open(port, "judge-new")
        self.assertEqual(self.answers([judge], 1.0), {judge: 429})
        self.assertLess(time.monotonic() - started, 1.0)


if __name__ == "__main__":
    unittest.main()
