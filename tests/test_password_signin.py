"""PW3: built-in email and password sign-in (POST /api/v1/session/password).

Design: V/docs/plans/2026-10-21-pilot-password-signin.md rev 4, sections
2 (T1-T10, T13), 3.2, 5.1, 5.4, 7.1 and 7.2. The app is driven with
direct ASGI calls on one event loop; accounts are synthetic rows in a
scratch control store; no service, provider or network is used.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import shutil
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from earwitness.app import admission as _admission
from earwitness.app import asgi as _asgi
from earwitness.app import browser_auth as _auth
from earwitness.app import health as _health
from earwitness.app import local_accounts as la

BASE = "https://pilot.example"
HOST = "pilot.example"
E = "Judge@Example.com"
PW = "ABCDE-FGHJK-LMNPQ-RSTUV"
P = la.principal(BASE, E)
SUB = la.subject(BASE, "judge@example.com")
LOGIN = "/api/v1/session/password"
FAILED = {"ok": False, "error": {"code": "SIGNIN_FAILED",
                                 "message": "email or password is incorrect"}}
JSON_HEADERS = {"content-type": "application/json", "origin": BASE,
                "sec-fetch-site": "same-origin"}


async def call(app, path, method="GET", body=None, *, raw=None, headers=None,
               cookies=None, client=("198.51.100.7", 40000)):
    """One ASGI request; returns (status, body bytes, parsed body, set-cookies)."""
    if raw is None:
        raw = json.dumps(body).encode() if body is not None else b""
    hdrs = [(b"host", HOST.encode())]
    for name, value in (headers or {}).items():
        hdrs.append((name.encode(), value.encode()))
    if cookies:
        hdrs.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    if raw:
        hdrs.append((b"content-length", str(len(raw)).encode()))
    scope = {"type": "http", "method": method, "path": path, "scheme": "https",
             "headers": hdrs, "query_string": b"", "client": client}
    sent = []

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    jar = {}
    for name, value in start.get("headers", []):
        if name == b"set-cookie":
            key, _, val = value.decode().split(";", 1)[0].partition("=")
            jar[key.strip()] = val.strip()
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    try:
        data = json.loads(payload.decode() or "{}")
    except ValueError:
        data = {}
    return start["status"], payload, data, jar


_REAL_VERIFY = la.verify


class PausedVerify:
    """Replaces local_accounts.verify: hashes for real, then waits until
    released, so a test can change state between hashing and the fence."""

    def __init__(self):
        self.hashed = threading.Event()
        self.release = threading.Event()
        self._real = la.verify

    def __call__(self, stored, password):
        out = self._real(stored, password)
        self.hashed.set()
        self.release.wait(10)
        return out

    async def wait_hashed(self, task=None, limit=20.0):
        """Wait until the worker hashed; fail at once if the request ended
        without hashing (or after `limit` seconds)."""
        deadline = time.monotonic() + limit
        while not self.hashed.is_set():
            if (task is not None and task.done()) or time.monotonic() > deadline:
                self.release.set()
                raise AssertionError("the request never reached the hash")
            await asyncio.sleep(0.01)


class _Base(unittest.IsolatedAsyncioTestCase):
    signin_local = True

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="ew-pw3-"))
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in list(os.environ):
            if key.startswith("EARWITNESS_"):
                os.environ.pop(key)
        self.admission = {"status": "approved", "principals": [P],
                          "removed_principals": []}
        self.admission_file = self.work / "admission.json"
        self.write_admission()
        os.environ["EARWITNESS_ADMISSION_FILE"] = str(self.admission_file)
        (self.work / "data").mkdir()
        self.grants = str(self.work / "grants.sqlite3")
        self.app = _asgi.HostedApp(_asgi.HostedConfig(
            mode="local-test", data_dir=str(self.work / "data"), grants_db=self.grants,
            public_base_url=BASE + "/", allowed_origins=(BASE,),
            signin_local=self.signin_local))
        self.addCleanup(self.app.close)

    def write_admission(self):
        self.admission_file.write_text(json.dumps(self.admission))

    def control(self):
        con = sqlite3.connect(self.grants)
        con.row_factory = sqlite3.Row
        return con

    def add_account(self, email=E, password=PW, *, stored=...):
        con = self.control()
        try:
            value = la.hash_password(password) if stored is ... else stored
            out = la.insert_account(con, BASE, email, value, time.time())
            con.commit()
            return out
        finally:
            con.close()

    def sessions(self, subject=SUB):
        con = self.control()
        try:
            return [dict(r) for r in con.execute(
                "SELECT * FROM browser_sessions WHERE subject=?", (subject,))]
        finally:
            con.close()

    def reset(self, subject=SUB):
        """What app_accounts reset-password commits: a new hash and every
        session revoked, in one transaction."""
        con = self.control()
        try:
            now = time.time()
            con.execute("UPDATE local_accounts SET password_hash=?, password_set_at=?"
                        " WHERE issuer=? AND subject=?",
                        (la.hash_password("reset-" + PW), now, BASE, subject))
            con.execute("UPDATE browser_sessions SET revoked_at=? WHERE subject=?"
                        " AND revoked_at IS NULL", (now, subject))
            con.commit()
        finally:
            con.close()

    async def login(self, email=E, password=PW, *, headers=None, client=("198.51.100.7", 40000),
                    cookies=None, return_to="/app/"):
        body = {"email": email, "password": password, "return_to": return_to}
        return await call(self.app, LOGIN, "POST", body,
                          headers=JSON_HEADERS if headers is None else headers,
                          client=client, cookies=cookies)


class OffByDefaultTest(_Base):
    signin_local = False

    async def test_local_signin_is_off_unless_configured(self):
        self.add_account()
        status, _raw, data, jar = await self.login()
        self.assertEqual(status, 503)
        self.assertEqual(data["error"]["message"], "browser sign-in is not configured")
        self.assertNotIn(_auth.SESSION_COOKIE, jar)
        status, _raw, data, _jar = await call(self.app, "/api/v1/session")
        self.assertEqual(status, 401)
        self.assertEqual(data["signin"]["methods"], [])
        self.assertFalse(data["signin"]["available"])

    def test_config_from_env_reads_the_switch(self):
        self.assertFalse(_asgi.config_from_env({}).signin_local)
        self.assertFalse(_asgi.config_from_env({"EARWITNESS_SIGNIN_LOCAL": "off"}).signin_local)
        self.assertTrue(_asgi.config_from_env({"EARWITNESS_SIGNIN_LOCAL": "on"}).signin_local)


class SignInTest(_Base):
    async def test_status_names_the_password_method(self):
        status, _raw, data, _jar = await call(self.app, "/api/v1/session")
        self.assertEqual(status, 401)
        self.assertEqual(data["signin"]["methods"], ["password"])
        self.assertTrue(data["signin"]["available"])
        self.assertIsNone(data["signin"]["login_url"])

    async def test_success_opens_a_session_for_the_derived_principal(self):
        self.add_account()
        status, _raw, data, jar = await self.login(email="  judge@EXAMPLE.com ")
        self.assertEqual(status, 200, data)
        self.assertEqual(data, {"return_to": "/app/"})
        cookie = jar[_auth.SESSION_COOKIE]
        status, _raw, data, _jar = await call(
            self.app, "/api/v1/session", cookies={_auth.SESSION_COOKIE: cookie})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["principal"], P)
        self.assertTrue(data["email_verified"])
        self.assertEqual(data["signin_method"], "password")
        self.assertNotIn("signin", data)  # the browser reads "signin" as signed out
        # Sol FIX-1: the account page names the account's email, never the
        # acct- subject; the session payload carries it.
        self.assertEqual(data["email"], E)
        [row] = self.sessions()
        self.assertEqual((row["issuer"], row["email"], row["email_verified"]), (BASE, E, 1))

    async def test_a_provider_session_payload_is_unchanged(self):
        """Sol FIX-1 touches only local sessions: a provider (OIDC) session
        payload still carries no email key."""
        issuer, sub = "https://idp.example", "u1"
        self.admission["principals"].append(f"oidc:{issuer}:{sub}")
        self.write_admission()
        cookie = "provider-session-cookie-" + "x" * 40
        con = self.control()
        try:
            con.execute("INSERT INTO browser_sessions (session_hash, issuer, subject, email,"
                        " email_verified, csrf_token, created_at, expires_at)"
                        " VALUES (?, ?, ?, 'u1@idp.example', 1, ?, ?, ?)",
                        (_auth._hash_presented(cookie), issuer, sub, "c" * 43,
                         time.time(), time.time() + 3600))
            con.commit()
        finally:
            con.close()
        status, _raw, data, _jar = await call(self.app, "/api/v1/session",
                                              cookies={_auth.SESSION_COOKIE: cookie})
        self.assertEqual(status, 200, data)
        self.assertEqual((data["signin_method"], data["subject"]), ("oidc", sub))
        self.assertNotIn("email", data)

    async def test_signin_revokes_the_cookies_previous_session(self):
        self.add_account()
        _s, _r, _d, first = await self.login()
        status, _r, _d, second = await self.login(
            cookies={_auth.SESSION_COOKIE: first[_auth.SESSION_COOKIE]})
        self.assertEqual(status, 200)
        rows = {r["session_hash"]: r for r in self.sessions()}
        old = rows[_auth._hash_presented(first[_auth.SESSION_COOKIE])]
        new = rows[_auth._hash_presented(second[_auth.SESSION_COOKIE])]
        self.assertIsNotNone(old["revoked_at"])
        self.assertIsNone(new["revoked_at"])


class IdenticalFailureTest(_Base):
    async def test_unknown_malformed_wrong_and_missing_password_answer_the_same(self):
        self.add_account()
        self.add_account("nopass@example.com", stored=None)
        self.add_account("badhash@example.com", stored="bcrypt$abc")
        cases = [("unknown@example.com", PW), ("a:b@example.com", PW),
                 ("two@@example.com", PW), (E, PW + "x"),
                 ("nopass@example.com", PW), ("badhash@example.com", PW), (17, PW)]
        bodies = set()
        for i, (email, password) in enumerate(cases):
            with self.subTest(email=email):
                calls = []
                real = hashlib.scrypt

                def counting(*a, **kw):
                    calls.append(kw)
                    return real(*a, **kw)
                with mock.patch.object(la.hashlib, "scrypt", counting):
                    status, raw, data, jar = await self.login(
                        email, password, client=(f"198.51.100.{10 + i}", 40000))
                self.assertEqual(status, 401, data)
                self.assertEqual(len(calls), 1, "every failure runs exactly one scrypt-v1")
                self.assertEqual(calls[0]["n"], la.SCRYPT_N)
                self.assertNotIn(_auth.SESSION_COOKIE, jar)
                bodies.add(raw)
        self.assertEqual(len(bodies), 1)
        self.assertEqual(json.loads(bodies.pop()), {"ok": False, "error": {
            **FAILED["error"], "status": 401, "details": {}}})

    async def test_failure_log_holds_no_email_password_or_account(self):
        self.add_account()
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("earwitness.ops")
        logger.addHandler(handler)
        old_level = logger.level
        logger.setLevel(logging.INFO)
        try:
            status, *_ = await self.login(E, "wrong-" + PW)
            self.assertEqual(status, 401)
            status, *_ = await self.login("unknown@example.com", "wrong-" + PW)
            self.assertEqual(status, 401)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
        lines = [json.loads(line) for line in stream.getvalue().splitlines()]
        failed = [r for r in lines if r["event"] == "signin.password_failed"]
        self.assertEqual(len(failed), 2)
        for record in failed:
            self.assertEqual(record["address"], "signin:198.51.100.7")
        text = stream.getvalue().lower()
        for secret in ("judge@example.com", "unknown@example.com", PW.lower(), SUB, "acct-"):
            self.assertNotIn(secret, text)

    async def test_an_exception_while_hashing_logs_no_field_value(self):
        self.add_account()
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("earwitness.ops")
        logger.addHandler(handler)
        old_level = logger.level
        logger.setLevel(logging.INFO)

        def boom(stored, password):
            raise RuntimeError(f"failed for {password}")
        try:
            with mock.patch.object(la, "verify", boom):
                status, _raw, data, _jar = await self.login()
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
        self.assertEqual(status, 500)
        text = stream.getvalue().lower()
        self.assertIn("request.error", text)
        for secret in ("judge@example.com", PW.lower(), "failed for"):
            self.assertNotIn(secret, text)
        self.assertEqual(self.app.signin_limiter._provider_in_flight, 0)


class RequestChecksTest(_Base):
    async def test_header_checks_refuse_before_any_hash(self):
        self.add_account()
        with mock.patch.object(la, "verify", side_effect=AssertionError("hashed")):
            for name, headers in [
                    ("no content type", {"origin": BASE}),
                    ("form type", {**JSON_HEADERS, "content-type": "application/x-www-form-urlencoded"}),
                    ("text type", {**JSON_HEADERS, "content-type": "text/plain"}),
                    ("cross-site fetch", {**JSON_HEADERS, "sec-fetch-site": "cross-site"}),
                    ("same-site fetch", {**JSON_HEADERS, "sec-fetch-site": "same-site"}),
                    ("null origin", {**JSON_HEADERS, "origin": "null"})]:
                with self.subTest(name):
                    status, _raw, data, jar = await self.login(headers=headers)
                    self.assertEqual(status, 403, data)
                    self.assertNotIn(_auth.SESSION_COOKIE, jar)
        # Without Origin or Sec-Fetch-Site (a non-browser client), JSON passes.
        status, _raw, data, _jar = await self.login(
            headers={"content-type": "application/json; charset=utf-8"})
        self.assertEqual(status, 200, data)

    async def test_foreign_origin_is_refused_even_with_no_allowlist(self):
        self.add_account()
        status, *_ = await self.login(headers={**JSON_HEADERS, "origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.app.config.allowed_origins = ()
        with mock.patch.object(la, "verify", side_effect=AssertionError("hashed")):
            status, *_ = await self.login(headers={**JSON_HEADERS, "origin": "https://evil.example"})
            self.assertEqual(status, 403)
            status, *_ = await self.login(headers=JSON_HEADERS)
            self.assertEqual(status, 403)

    async def test_body_checks_answer_400_before_any_hash(self):
        self.add_account()
        big = json.dumps({"email": E, "password": PW, "return_to": "/", "pad": "x" * 4096}).encode()
        with mock.patch.object(la, "verify", side_effect=AssertionError("hashed")):
            status, *_ = await call(self.app, LOGIN, "POST", raw=big, headers=JSON_HEADERS)
            self.assertEqual(status, 400)
            for name, password, return_to in [
                    ("empty password", "", "/"), ("long password", "é" * 513, "/"),
                    ("not a string", 12345678901234, "/"), ("missing", None, "/"),
                    ("absolute return", PW, "https://evil.example/"),
                    ("protocol-relative return", PW, "//evil.example/")]:
                with self.subTest(name):
                    status, _raw, data, _jar = await self.login(password=password, return_to=return_to)
                    self.assertEqual(status, 400, data)
            status, *_ = await call(self.app, LOGIN, "POST", raw=b"[1]", headers=JSON_HEADERS)
            self.assertEqual(status, 400)
        # 1024 bytes is still accepted (and simply wrong).
        status, *_ = await self.login(password="x" * 1024)
        self.assertEqual(status, 401)


class PairLimitRouteTest(_Base):
    async def test_five_failures_block_the_pair_but_not_other_addresses(self):
        self.add_account()
        for _ in range(5):
            status, *_ = await self.login(password="wrong-" + PW)
            self.assertEqual(status, 401)
        with mock.patch.object(la, "verify", side_effect=AssertionError("hashed")):
            status, _raw, data, jar = await self.login()
        self.assertEqual(status, 429, data)
        self.assertNotIn(_auth.SESSION_COOKIE, jar)
        status, *_ = await self.login(client=("203.0.113.9", 1))
        self.assertEqual(status, 200)
        # The same address with another email is a different pair.
        status, *_ = await self.login("unknown@example.com", PW)
        self.assertEqual(status, 401)

    async def test_success_resets_the_failure_count(self):
        self.add_account()
        for _ in range(4):
            status, *_ = await self.login(password="wrong-" + PW)
            self.assertEqual(status, 401)
        status, *_ = await self.login()
        self.assertEqual(status, 200)
        for _ in range(4):
            status, *_ = await self.login(password="wrong-" + PW)
            self.assertEqual(status, 401)
        status, *_ = await self.login()
        self.assertEqual(status, 200)

    async def test_hash_slot_refusal_releases_the_pair_once(self):
        self.add_account()
        limiter = self.app.signin_limiter
        limiter._provider_in_flight = _asgi.SIGNIN_MAX_CONCURRENT
        for _ in range(8):
            status, *_ = await self.login()
            self.assertEqual(status, 429)
        self.assertEqual(self.app.pair_limiter.tracked(), 0)
        limiter._provider_in_flight = 0
        status, *_ = await self.login()
        self.assertEqual(status, 200)

    async def test_cancelled_request_holds_its_slot_and_pair_until_the_hash_ends(self):
        self.add_account()
        paused = PausedVerify()
        with mock.patch.object(la, "verify", paused):
            task = asyncio.ensure_future(self.login())
            await paused.wait_hashed(task)
            task.cancel()
            await asyncio.sleep(0.05)
            self.assertEqual(self.app.signin_limiter._provider_in_flight, 1)
            [entry] = self.app.pair_limiter._entries.values()
            self.assertEqual(entry[2], 1)
            paused.release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.app.signin_limiter._provider_in_flight, 0)
        self.assertEqual(entry[2], 0)
        self.assertEqual(self.sessions(), [])


class PairLimiterTest(unittest.TestCase):
    def setUp(self):
        self.lim = _asgi.PairLimiter()
        self.t = 1000.0

    def key(self, n=0, address="a"):
        return self.lim.key(f"user{n}@example.com", address)

    def fail_attempts(self, key, times=1):
        for _ in range(times):
            handle, _wait = self.lim.reserve(key, self.t)
            self.assertIsNotNone(handle)
            self.lim.complete(handle, False, self.t)

    def test_keys_are_keyed_by_a_process_secret(self):
        other = _asgi.PairLimiter()
        self.assertNotEqual(self.lim.key("e@x", "a"), other.key("e@x", "a"))
        self.assertEqual(self.lim.key("e@x", "a"), self.lim.key("e@x", "a"))
        self.assertNotEqual(self.lim.key("e@x", "a"), self.lim.key("e@x", "b"))

    def test_many_unknown_emails_are_each_admitted(self):
        for n in range(200):
            handle, _w = self.lim.reserve(self.key(n), self.t)
            self.assertIsNotNone(handle)
            self.lim.complete(handle, False, self.t)
        self.assertEqual(self.lim.tracked(), 200)

    def test_limit_counts_reservations_in_flight_with_no_overshoot(self):
        key = self.key()
        held = [self.lim.reserve(key, self.t)[0] for _ in range(5)]
        self.assertTrue(all(held))
        handle, wait = self.lim.reserve(key, self.t)
        self.assertIsNone(handle)
        self.assertGreater(wait, 0)
        self.fail_attempts(self.key(1))  # other keys are unaffected
        for h in held[:3]:
            self.lim.complete(h, False, self.t)
        self.assertIsNone(self.lim.reserve(key, self.t)[0])

    def test_window_ends_after_fifteen_minutes(self):
        key = self.key()
        self.fail_attempts(key, 5)
        self.assertIsNone(self.lim.reserve(key, self.t + 899)[0])
        self.t += 900
        self.assertIsNotNone(self.lim.reserve(key, self.t)[0])

    def test_success_and_failure_overlapping_on_one_pair(self):
        key = self.key()
        self.fail_attempts(key, 3)
        a = self.lim.reserve(key, self.t)[0]
        b = self.lim.reserve(key, self.t)[0]
        self.lim.complete(a, True, self.t)
        self.lim.complete(b, False, self.t)  # finishes after the success: still counts
        self.assertEqual(self.lim._entries[key][0], 1)
        # Completing twice changes nothing.
        self.lim.complete(b, False, self.t)
        self.lim.complete(a, True, self.t)
        self.assertEqual((self.lim._entries[key][0], self.lim._entries[key][2]), (1, 0))

    def test_entry_with_a_reservation_open_is_reused_never_deleted(self):
        key = self.key()
        self.fail_attempts(key, 4)
        held = self.lim.reserve(key, self.t)[0]
        entry = self.lim._entries[key]
        self.t += 2000  # its window ended, but a reservation is open
        self.lim.prune(self.t)
        self.assertIs(self.lim._entries[key], entry)
        again = self.lim.reserve(key, self.t)[0]
        self.assertIsNotNone(again)
        self.assertIs(self.lim._entries[key], entry)
        self.lim.complete(held, False, self.t)
        self.lim.complete(again, False, self.t)
        self.assertEqual(entry[2], 0)
        self.assertEqual(entry[0], 2)  # the window restarted with these failures

    def test_full_table_refuses_new_keys_but_never_evicts_live_entries(self):
        lim = _asgi.PairLimiter(cap=50)
        live = [lim.key(f"u{n}", "a") for n in range(50)]
        for key in live:
            h = lim.reserve(key, self.t)[0]
            lim.complete(h, False, self.t)
        at_limit = live[0]
        for _ in range(4):
            lim.complete(lim.reserve(at_limit, self.t)[0], False, self.t)
        self.assertEqual(lim._entries[at_limit][0], 5)
        newcomer = lim.key("new", "a")
        handle, wait = lim.reserve(newcomer, self.t)
        self.assertIsNone(handle)
        self.assertGreater(wait, 0)
        self.assertEqual(lim.tracked(), 50)
        self.assertIsNone(lim.reserve(at_limit, self.t)[0])  # still blocked: not evicted
        self.assertIsNotNone(lim.reserve(live[1], self.t)[0])  # existing keys still work
        # Expired entries are pruned first when a new key arrives at the cap.
        handle, _w = lim.reserve(newcomer, self.t + 900)
        self.assertIsNotNone(handle)
        self.assertIn(live[1], lim._entries)  # in flight: kept

    def test_release_without_outcome_happens_once(self):
        key = self.key()
        self.fail_attempts(key, 2)
        h = self.lim.reserve(key, self.t)[0]
        self.lim.complete(h, None, self.t)
        self.lim.complete(h, None, self.t)
        self.lim.complete(h, False, self.t)
        self.assertEqual(self.lim._entries[key][0], 2)
        self.assertEqual(self.lim._entries[key][2], 0)


class FenceTest(_Base):
    async def test_login_against_reset_creates_no_session(self):
        self.add_account()
        paused = PausedVerify()
        with mock.patch.object(la, "verify", paused):
            task = asyncio.ensure_future(self.login())
            await paused.wait_hashed(task)
            self.reset()
            paused.release.set()
            status, _raw, data, jar = await task
        self.assertEqual(status, 401, data)
        self.assertNotIn(_auth.SESSION_COOKIE, jar)
        self.assertEqual(self.sessions(), [])

    async def test_login_against_exclusion_creates_no_session(self):
        self.add_account()
        paused = PausedVerify()
        with mock.patch.object(la, "verify", paused):
            task = asyncio.ensure_future(self.login())
            await paused.wait_hashed(task)
            self.admission["removed_principals"].append(P)
            self.write_admission()
            paused.release.set()
            status, _raw, data, jar = await task
        self.assertEqual(status, 403, data)
        self.assertEqual(data["error"]["message"], _admission.REFUSAL)
        self.assertNotIn(_auth.SESSION_COOKIE, jar)
        self.assertEqual(self.sessions(), [])

    async def test_a_valid_password_gets_the_admission_403_after_exclusion(self):
        self.add_account()
        _s, _r, _d, first = await self.login()
        self.admission["principals"].remove(P)
        self.admission["removed_principals"].append(P)
        self.write_admission()
        status, _raw, data, jar = await self.login(
            cookies={_auth.SESSION_COOKIE: first[_auth.SESSION_COOKIE]})
        self.assertEqual(status, 403, data)
        self.assertEqual(data["error"]["message"], _admission.REFUSAL)
        self.assertNotIn(_auth.SESSION_COOKIE, jar)
        con = self.control()
        try:
            row = con.execute("SELECT password_hash FROM local_accounts").fetchone()
        finally:
            con.close()
        self.assertTrue(la.verify(row["password_hash"], PW))  # the hash remains
        # Nothing was written: the refused attempt did not revoke the old cookie's row.
        [old] = self.sessions()
        self.assertIsNone(old["revoked_at"])

    async def test_an_unadmitted_account_with_a_valid_password_gets_no_session(self):
        self.add_account("stranger@example.com")
        status, _raw, data, jar = await self.login("stranger@example.com")
        self.assertEqual(status, 403, data)
        self.assertNotIn(_auth.SESSION_COOKIE, jar)


class StartupTest(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="ew-pw3s-"))
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.grants = str(self.work / "grants.sqlite3")
        con = sqlite3.connect(self.grants)
        con.row_factory = sqlite3.Row
        _auth.ensure_auth_schema(con)
        con.commit()
        con.close()

    def config(self, **kw):
        base = dict(mode="production", grants_db=self.grants, public_base_url=BASE,
                    allowed_hosts=(HOST,), allowed_origins=(BASE,), signin_local=True,
                    issuer="")
        base.update(kw)
        return _asgi.HostedConfig(**base)

    def row(self, issuer=BASE, email=E, stored=None):
        con = sqlite3.connect(self.grants)
        try:
            la.insert_account(con, issuer, email,
                              la.hash_password(PW) if stored is None else stored, 1.0)
            con.commit()
        finally:
            con.close()

    def problems(self, **kw):
        return _health.local_signin_problems(self.config(**kw))

    def test_a_clean_configuration_has_no_local_signin_problem(self):
        self.row()
        self.assertEqual(self.problems(), [])
        self.assertEqual(self.problems(signin_local=False), [])

    def test_oauth_issuer_equal_to_the_local_issuer_is_refused(self):
        for issuer in (BASE, BASE + "/"):
            self.assertTrue(any("EARWITNESS_OAUTH_ISSUER" in p for p in self.problems(issuer=issuer)))
        self.assertEqual(self.problems(issuer="https://accounts.google.com"), [])
        # Off, but a row exists: still refused (T10).
        self.row()
        self.assertTrue(any("EARWITNESS_OAUTH_ISSUER" in p
                            for p in self.problems(signin_local=False, issuer=BASE)))

    def test_rows_under_another_issuer_are_refused(self):
        self.row(issuer="https://old.example")
        self.assertTrue(any("issuer" in p for p in self.problems()))
        self.assertTrue(any("issuer" in p for p in self.problems(signin_local=False)))
        self.assertTrue(any("issuer" in p for p in self.problems(mode="local-test")))

    def test_production_needs_an_https_allowed_public_origin(self):
        self.assertTrue(any("https" in p for p in self.problems(
            public_base_url="http://pilot.example", allowed_origins=())))
        self.assertTrue(any("EARWITNESS_ALLOWED_HOSTS" in p for p in self.problems(
            allowed_hosts=("other.example",))))
        self.assertTrue(self.problems(public_base_url=""))
        # Off with no rows: nothing to check.
        self.assertEqual(self.problems(signin_local=False, public_base_url=""), [])

    def test_a_malformed_hash_makes_production_refuse(self):
        self.row(stored="bcrypt$abc")
        self.assertTrue(any("hash" in p for p in self.problems()))
        self.assertFalse(any("hash" in p for p in self.problems(mode="local-test")))

    def test_check_config_includes_the_local_signin_problems(self):
        problems = _health.check_config(self.config(issuer=BASE))
        self.assertTrue(any("EARWITNESS_OAUTH_ISSUER" in p for p in problems))
        problems = _health.check_config(self.config(mode="local-test", issuer=BASE))
        self.assertTrue(any("EARWITNESS_OAUTH_ISSUER" in p for p in problems))


CHANGE = "/api/v1/session/password/change"
NEW_PW = "a fresh chosen password"


class ChangePasswordTest(_Base):
    """PW4: POST /api/v1/session/password/change (design 5.3, 5.4, T2)."""

    async def signed_in(self, client=("198.51.100.7", 40000)):
        status, _raw, data, jar = await self.login(client=client)
        self.assertEqual(status, 200, data)
        cookie = jar[_auth.SESSION_COOKIE]
        status, _raw, data, _jar = await call(self.app, "/api/v1/session",
                                              cookies={_auth.SESSION_COOKIE: cookie})
        self.assertEqual(status, 200, data)
        return cookie, data["csrf_token"]

    async def change(self, cookie, csrf, current=PW, new=NEW_PW, client=("198.51.100.7", 40000)):
        headers = {"content-type": "application/json", "origin": BASE}
        if csrf is not None:
            headers["x-csrf-token"] = csrf
        return await call(self.app, CHANGE, "POST",
                          {"current_password": current, "new_password": new},
                          headers=headers, client=client,
                          cookies={_auth.SESSION_COOKIE: cookie} if cookie else None)

    def stored_hash(self):
        con = self.control()
        try:
            return con.execute("SELECT password_hash FROM local_accounts WHERE subject=?",
                               (SUB,)).fetchone()["password_hash"]
        finally:
            con.close()

    async def test_change_keeps_the_caller_and_signs_out_other_devices(self):
        self.add_account()
        mine, csrf = await self.signed_in()
        other, _c = await self.signed_in(client=("203.0.113.4", 1))
        status, _raw, data, _jar = await self.change(mine, csrf)
        self.assertEqual(status, 200, data)
        self.assertEqual(data, {"ok": True})
        stored = self.stored_hash()
        self.assertTrue(la.verify(stored, NEW_PW))
        self.assertFalse(la.verify(stored, PW))
        status, *_ = await call(self.app, "/api/v1/session", cookies={_auth.SESSION_COOKIE: mine})
        self.assertEqual(status, 200)
        status, *_ = await call(self.app, "/api/v1/session", cookies={_auth.SESSION_COOKIE: other})
        self.assertEqual(status, 401)
        status, *_ = await self.login(password=NEW_PW, client=("203.0.113.5", 1))
        self.assertEqual(status, 200)
        status, *_ = await self.login(password=PW, client=("203.0.113.6", 1))
        self.assertEqual(status, 401)

    async def test_change_needs_a_live_local_session_and_its_csrf_token(self):
        self.add_account()
        mine, csrf = await self.signed_in()
        with mock.patch.object(la, "verify", side_effect=AssertionError("hashed")):
            status, _raw, data, _jar = await self.change(None, None)
            self.assertEqual(status, 401, data)
            status, *_ = await self.change(mine, None)
            self.assertEqual(status, 403)
            status, *_ = await self.change(mine, "x" * 43)
            self.assertEqual(status, 403)
            # A session from another issuer (an OIDC account) has no local password.
            con = self.control()
            try:
                con.execute("INSERT INTO browser_sessions (session_hash, issuer, subject, email,"
                            " email_verified, csrf_token, created_at, expires_at)"
                            " VALUES (?, 'https://issuer.example', 'u1', 'u1@example.com', 1, ?, ?, ?)",
                            (_auth._hash_presented("oidc-session"), "t" * 43, time.time(),
                             time.time() + 3600))
                con.commit()
            finally:
                con.close()
            self.admission["principals"].append("oidc:https://issuer.example:u1")
            self.write_admission()
            status, _raw, data, _jar = await self.change("oidc-session", "t" * 43)
            self.assertEqual(status, 403, data)
            self.app.config.signin_local = False
            status, *_ = await self.change(mine, csrf)
            self.assertEqual(status, 503)
        self.assertTrue(la.verify(self.stored_hash(), PW))

    async def test_new_password_bounds_are_checked_before_any_hash(self):
        self.add_account()
        mine, csrf = await self.signed_in()
        with mock.patch.object(la, "verify", side_effect=AssertionError("hashed")):
            for name, current, new in [
                    ("eleven characters", PW, "x" * 11), ("129 characters", PW, "x" * 129),
                    ("same as current", PW, PW), ("same as email", PW, "JUDGE@example.com"),
                    ("not a string", PW, 123456789012345), ("missing current", "", NEW_PW),
                    ("current too long", "x" * 1025, NEW_PW)]:
                with self.subTest(name):
                    status, _raw, data, _jar = await self.change(mine, csrf, current, new)
                    self.assertEqual(status, 400, data)
        # 12 and 128 characters (512 UTF-8 bytes) are accepted.
        status, _raw, data, _jar = await self.change(mine, csrf, PW, "y" * 12)
        self.assertEqual(status, 200, data)
        status, _raw, data, _jar = await self.change(mine, csrf, "y" * 12, "\U0001F600" * 128)
        self.assertEqual(status, 200, data)

    async def test_wrong_current_password_changes_nothing_and_counts(self):
        self.add_account()
        mine, csrf = await self.signed_in()
        before = self.stored_hash()
        for _ in range(4):
            status, _raw, data, _jar = await self.change(mine, csrf, "wrong-" + PW)
            self.assertEqual(status, 401, data)
            self.assertEqual(data["error"]["code"], "PASSWORD_INCORRECT")
            self.assertNotIn("signin", data)
        self.assertEqual(self.stored_hash(), before)
        # The sign-in success reset the pair; four failures plus this one fill it.
        status, *_ = await self.change(mine, csrf, "wrong-" + PW)
        self.assertEqual(status, 401)
        status, *_ = await self.change(mine, csrf)
        self.assertEqual(status, 429)
        status, *_ = await call(self.app, "/api/v1/session", cookies={_auth.SESSION_COOKIE: mine})
        self.assertEqual(status, 200)

    async def test_change_against_reset_keeps_the_newer_hash(self):
        self.add_account()
        mine, csrf = await self.signed_in()
        paused = PausedVerify()
        with mock.patch.object(la, "verify", paused):
            task = asyncio.ensure_future(self.change(mine, csrf))
            await paused.wait_hashed(task)
            self.reset()
            paused.release.set()
            status, _raw, data, _jar = await task
        self.assertEqual(status, 409, data)
        self.assertEqual(data["error"]["message"],
                         "Your password was changed elsewhere. Sign in again.")
        stored = self.stored_hash()
        self.assertTrue(la.verify(stored, "reset-" + PW))
        self.assertFalse(la.verify(stored, NEW_PW))

    async def test_change_against_exclusion_is_refused_and_keeps_the_hash(self):
        """Review LOW-2: admission is removed while the change is hashing.
        The fence refuses with the admission 403 (not 409) and the stored
        hash is unchanged."""
        self.add_account()
        mine, csrf = await self.signed_in()
        before = self.stored_hash()
        paused = PausedVerify()
        with mock.patch.object(la, "verify", paused):
            task = asyncio.ensure_future(self.change(mine, csrf))
            await paused.wait_hashed(task)
            self.admission["removed_principals"].append(P)
            self.write_admission()
            paused.release.set()
            status, _raw, data, _jar = await task
        self.assertEqual(status, 403, data)
        self.assertEqual(data["error"]["message"], _admission.REFUSAL)
        self.assertEqual(self.stored_hash(), before)

    async def test_change_against_a_concurrent_hash_change_replaces_nothing(self):
        self.add_account()
        mine, csrf = await self.signed_in()
        newer = la.hash_password("newer-" + PW)
        paused = PausedVerify()
        with mock.patch.object(la, "verify", paused):
            task = asyncio.ensure_future(self.change(mine, csrf))
            await paused.wait_hashed(task)
            con = self.control()
            con.execute("UPDATE local_accounts SET password_hash=? WHERE subject=?", (newer, SUB))
            con.commit()
            con.close()
            paused.release.set()
            status, _raw, data, _jar = await task
        self.assertEqual(status, 409, data)
        self.assertEqual(self.stored_hash(), newer)
        [row] = [r for r in self.sessions() if r["session_hash"] == _auth._hash_presented(mine)]
        self.assertIsNone(row["revoked_at"])

    async def test_change_after_the_session_ended_replaces_nothing(self):
        """The fence re-reads the initiating session: signed out (here, in
        another tab) while hashing, the hash stays as it was."""
        self.add_account()
        mine, csrf = await self.signed_in()
        before = self.stored_hash()
        paused = PausedVerify()
        with mock.patch.object(la, "verify", paused):
            task = asyncio.ensure_future(self.change(mine, csrf))
            await paused.wait_hashed(task)
            con = self.control()
            con.execute("UPDATE browser_sessions SET revoked_at=? WHERE session_hash=?",
                        (time.time(), _auth._hash_presented(mine)))
            con.commit()
            con.close()
            paused.release.set()
            status, _raw, data, _jar = await task
        self.assertEqual(status, 409, data)
        self.assertEqual(self.stored_hash(), before)

    async def test_one_slot_is_held_for_both_hashes(self):
        self.add_account()
        mine, csrf = await self.signed_in()
        seen = []
        real = hashlib.scrypt

        def counting(*a, **kw):
            seen.append(self.app.signin_limiter._provider_in_flight)
            return real(*a, **kw)
        with mock.patch.object(la.hashlib, "scrypt", counting):
            status, *_ = await self.change(mine, csrf)
        self.assertEqual(status, 200)
        self.assertEqual(seen, [1, 1])
        self.assertEqual(self.app.signin_limiter._provider_in_flight, 0)

    async def test_an_exception_while_hashing_logs_no_field_value(self):
        self.add_account()
        mine, csrf = await self.signed_in()
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("earwitness.ops")
        logger.addHandler(handler)
        old_level = logger.level
        logger.setLevel(logging.INFO)

        def boom(password):
            raise RuntimeError(f"could not hash {password}")
        try:
            with mock.patch.object(la, "hash_password", boom):
                status, *_ = await self.change(mine, csrf)
            status_wrong, *_ = await self.change(mine, csrf, "wrong-" + PW)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
        self.assertEqual(status, 500)
        self.assertEqual(status_wrong, 401)
        text = stream.getvalue().lower()
        self.assertIn("request.error", text)
        for secret in (NEW_PW, PW.lower(), "could not hash", "judge@example.com", SUB):
            self.assertNotIn(secret, text)
        self.assertTrue(la.verify(self.stored_hash(), PW))
        self.assertEqual(self.app.signin_limiter._provider_in_flight, 0)


async def stream(app, path, chunks, *, headers, length=None, cookies=None,
                 client=("198.51.100.7", 40000), on_receive=None):
    """One ASGI request whose body arrives in `chunks`; returns (status,
    the number of receive() calls the app made)."""
    hdrs = [(b"host", HOST.encode())]
    for name, value in headers.items():
        hdrs.append((name.encode(), value.encode()))
    if cookies:
        hdrs.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    if length is not None:
        hdrs.append((b"content-length", str(length).encode()))
    scope = {"type": "http", "method": "POST", "path": path, "scheme": "https",
             "headers": hdrs, "query_string": b"", "client": client}
    calls, sent = [], []

    async def receive():
        if on_receive is not None:
            on_receive()
        calls.append(1)
        n = len(calls)
        if n > len(chunks):
            return {"type": "http.disconnect"}
        return {"type": "http.request", "body": chunks[n - 1], "more_body": n < len(chunks)}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], len(calls)


class StreamedBodyTest(_Base):
    """Astra PWB-R1-01: both password routes bound the actual read at
    PASSWORD_BODY_MAX (4 KiB), and refuse cheaply before reading any body."""

    signed_in = ChangePasswordTest.signed_in

    def setUp(self):
        super().setUp()
        self.add_account()
        hashed = mock.patch.object(la, "verify", side_effect=AssertionError("hashed"))
        hashed.start()
        self.addCleanup(hashed.stop)

    async def routes(self):
        with mock.patch.object(la, "verify", _REAL_VERIFY):
            cookie, csrf = await self.signed_in()
        change = {"content-type": "application/json", "origin": BASE, "x-csrf-token": csrf}
        return [(LOGIN, JSON_HEADERS, None), (CHANGE, change, {_auth.SESSION_COOKIE: cookie})]

    async def test_declared_oversize_is_refused_without_reading(self):
        for path, headers, cookies in await self.routes():
            with self.subTest(path):
                status, calls = await stream(self.app, path, [b"x" * 8192], headers=headers,
                                             length=8192, cookies=cookies)
                self.assertEqual((status, calls), (400, 0))

    async def test_chunked_oversize_stops_at_the_first_chunk_past_4_kib(self):
        for path, headers, cookies in await self.routes():
            for name, length, chunks in [
                    ("no length", None, [b"x" * 8192, b""]),
                    ("no length, small chunks", None, [b"x" * 3000, b"x" * 3000, b"x" * 3000, b""]),
                    ("understated length", 100, [b"x" * 3000, b"x" * 3000, b""])]:
                with self.subTest(path=path, case=name):
                    status, calls = await stream(self.app, path, chunks, headers=headers,
                                                 length=length, cookies=cookies)
                    self.assertEqual(status, 400)
                    self.assertEqual(calls, 1 if len(chunks[0]) > 4096 else 2)

    async def test_refused_attempts_read_no_body(self):
        big = [b"x" * 3000, b"x" * 3000, b""]
        # Local sign-in off, header checks, and an L20 refusal: no read.
        status, calls = await stream(self.app, LOGIN, big, headers={**JSON_HEADERS, "content-type": "text/plain"})
        self.assertEqual((status, calls), (403, 0))
        with mock.patch.object(self.app.signin_limiter, "admit_address", return_value=30.0):
            status, calls = await stream(self.app, LOGIN, big, headers=JSON_HEADERS)
            self.assertEqual((status, calls), (429, 0))
        status, calls = await stream(self.app, CHANGE, big, headers={"content-type": "application/json", "origin": BASE})
        self.assertEqual((status, calls), (401, 0))
        self.app.config.signin_local = False
        status, calls = await stream(self.app, LOGIN, big, headers=JSON_HEADERS)
        self.assertEqual((status, calls), (503, 0))

    async def test_no_hash_slot_or_pair_is_held_while_the_body_uploads(self):
        seen = []
        probe = lambda: seen.append((self.app.signin_limiter._provider_in_flight,  # noqa: E731
                                     self.app.pair_limiter.tracked()))
        with mock.patch.object(la, "verify", _REAL_VERIFY):
            status, calls = await stream(self.app, LOGIN, [b'{"email": "a@b.c", ', b'"password": "x"}'],
                                         headers=JSON_HEADERS, on_receive=probe)
        self.assertEqual((status, calls), (401, 2))
        self.assertEqual(seen, [(0, 0), (0, 0)])


class TimingTest(_Base):
    """T4: the work is equal by construction (one scrypt-v1 each); the
    timing is measured, not proven. The spread is printed for the report;
    the loose assertion only shows the unknown paths really hash (the
    exact one-hash count is IdenticalFailureTest)."""

    async def test_timing_spread_is_measured(self):
        self.add_account()
        self.add_account("nopass@example.com", stored=None)
        self.add_account("badhash@example.com", stored="bcrypt$abc")
        cases = {"unknown": ("unknown@example.com", PW),
                 "malformed_email": ("a:b@example.com", PW),
                 "wrong_password": (E, "wrong-" + PW),
                 "no_password": ("nopass@example.com", PW),
                 "malformed_hash": ("badhash@example.com", PW)}
        rounds = 8
        times = {name: [] for name in cases}
        for r in range(rounds):
            for i, (name, (email, password)) in enumerate(cases.items()):
                self.app.pair_limiter = _asgi.PairLimiter()
                self.app.signin_limiter = _asgi.SigninLimiter()
                started = time.perf_counter()
                status, *_ = await self.login(email, password, client=(f"198.51.100.{i}", r))
                times[name].append(time.perf_counter() - started)
                self.assertEqual(status, 401)
        medians = {name: statistics.median(v) * 1000 for name, v in times.items()}
        spread = max(medians.values()) - min(medians.values())
        report = {"rounds": rounds, "median_ms": {k: round(v, 2) for k, v in medians.items()},
                  "spread_ms": round(spread, 2),
                  "spread_pct_of_max": round(100 * spread / max(medians.values()), 1)}
        print("\nPW3 timing " + json.dumps(report, sort_keys=True), file=sys.stderr)
        out = os.environ.get("EW_PW3_TIMING_OUT")
        if out:
            Path(out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        self.assertGreater(min(medians.values()), 0.25 * max(medians.values()), report)


if __name__ == "__main__":
    unittest.main()
