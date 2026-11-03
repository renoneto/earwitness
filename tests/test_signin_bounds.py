"""L20 (Astra L8d R1-01): anonymous sign-in must not block the web process.

A local fixture identity provider on 127.0.0.1 (port 0, synthetic users,
an in-test RSA key) can answer normally, hold a reply open, fail, send an
oversized reply, or drip a reply byte by byte. The app is driven with
direct ASGI calls on one event loop, so a blocking provider call would
also block the concurrent /healthz call. No real account, no network
beyond loopback.
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from earwitness.app import asgi as _asgi
from earwitness.app import browser_auth as _auth

DISCOVERY = "/.well-known/openid-configuration"
HOLD_MAX_S = 2.5  # a held reply is released by the test or after this


class _IdP:
    """Fixture OIDC provider. `modes[path]` picks the behaviour:
    "ok" (default), "hold", "fail" (HTTP 500), "throttle" (HTTP 429), "big"
    (about 1 MB of valid JSON) or "drip" (one byte every 50 ms)."""

    def __init__(self):
        from joserfc.jwk import RSAKey
        self._rsa = RSAKey
        self.counts = collections.Counter()
        self.modes = {}
        self.payloads = {}  # path -> fixed JSON reply (malformed replies)
        self.release = threading.Event()
        self.codes = {}
        self.lock = threading.Lock()
        self.rotate("k1")
        idp = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _serve(self, path, payload_fn):
                with idp.lock:
                    idp.counts[path] += 1
                mode = idp.modes.get(path, "ok")
                try:
                    if mode == "hold" and not idp.release.wait(HOLD_MAX_S):
                        idp.release.set()  # later holds pass at once
                    if mode == "fail":
                        return self._reply(500, {"error": "server_error"})
                    if mode == "throttle":
                        return self._reply(429, {"error": "too_many_requests"})
                    payload = (idp.payloads[path] if path in idp.payloads
                               else payload_fn())
                    if mode == "big":
                        payload = {**payload, "pad": "x" * 1_000_000}
                    if mode == "drip":
                        data = json.dumps(payload).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        deadline = time.monotonic() + 3.0
                        for i in range(len(data)):
                            if time.monotonic() > deadline:
                                return
                            self.wfile.write(data[i:i + 1])
                            self.wfile.flush()
                            time.sleep(0.05)
                        return
                    return self._reply(200, payload)
                except (BrokenPipeError, ConnectionResetError):
                    return

            def do_GET(self):
                path = urllib.parse.urlsplit(self.path).path
                if path == DISCOVERY:
                    return self._serve(path, lambda: {
                        "issuer": idp.iss, "jwks_uri": idp.iss + "/jwks",
                        "token_endpoint": idp.iss + "/token",
                        "authorization_endpoint": idp.iss + "/authorize"})
                if path == "/jwks":
                    return self._serve(path, lambda: {"keys": [idp.public]})
                self._reply(404, {})

            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0") or 0)
                fields = {k: v[0] for k, v in urllib.parse.parse_qs(
                    self.rfile.read(n).decode()).items()}
                if self.path != "/token":
                    return self._reply(404, {})
                rec = idp.codes.pop(fields.get("code", ""), None)
                if rec is None:
                    with idp.lock:
                        idp.counts["/token"] += 1
                    return self._reply(400, {"error": "invalid_grant"})
                return self._serve("/token", lambda: {
                    "id_token": idp.mint(rec), "token_type": "Bearer",
                    "access_token": "at-fixture"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.iss = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def rotate(self, kid):
        self.kid = kid
        self.key = self._rsa.generate_key(2048, parameters={"kid": kid,
                                                            "use": "sig"})
        pub = self.key.as_dict(private=False)
        pub["alg"] = "RS256"
        self.public = pub

    def issue_code(self, nonce, subject):
        code = f"code-{len(self.codes)}-{time.monotonic_ns()}"
        self.codes[code] = {"nonce": nonce, "sub": subject}
        return code

    def mint(self, rec):
        from joserfc import jwt
        now = int(time.time())
        token = jwt.encode({"alg": "RS256", "kid": self.kid},
                           {"iss": self.iss, "aud": "cid", "sub": rec["sub"],
                            "nonce": rec["nonce"], "iat": now,
                            "exp": now + 300}, self.key)
        return token.decode("ascii") if isinstance(token, bytes) else token

    def provider_requests(self):
        return sum(self.counts.values())

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


async def _call(app, path, method="GET", body=None, cookies=None, query="",
                client=("198.51.100.7", 40000)):
    raw = json.dumps(body).encode() if body is not None else b""
    headers = [(b"host", b"127.0.0.1:8771")]
    if cookies:
        headers.append((b"cookie", "; ".join(
            f"{k}={v}" for k, v in cookies.items()).encode()))
    if raw:
        headers.append((b"content-length", str(len(raw)).encode()))
    scope = {"type": "http", "method": method, "path": path, "scheme": "http",
             "headers": headers, "query_string": query.encode(),
             "client": client}
    sent = []

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    set_cookies = {}
    for name, value in start.get("headers", []):
        if name == b"set-cookie":
            pair = value.decode().split(";", 1)[0]
            key, _, val = pair.partition("=")
            set_cookies[key.strip()] = val.strip()
    payload = b"".join(m.get("body", b"") for m in sent
                       if m["type"] == "http.response.body")
    try:
        data = json.loads(payload.decode() or "{}")
    except ValueError:
        data = {}
    return start["status"], data, set_cookies


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="ew-l20-"))
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in ("EARWITNESS_ADMISSION_FILE", "EARWITNESS_MODE"):
            os.environ.pop(key, None)
        self.idp = _IdP()
        self.addCleanup(self.idp.close)
        self.now = [1_000_000.0]
        (self.work / "data").mkdir()
        self.app = _asgi.HostedApp(_asgi.HostedConfig(
            data_dir=str(self.work / "data"),
            grants_db=str(self.work / "grants.sqlite3"),
            clock=lambda: self.now[0]))
        self.addCleanup(self.app.close)
        self.app.browser_config_override = _auth.BrowserAuthConfig(
            issuer=self.idp.iss, client_id="cid", audience="aud",
            redirect_uri="http://127.0.0.1:8771/api/v1/session/callback",
            allow_insecure_loopback=True)

    async def login(self, client=("198.51.100.7", 40000)):
        return await _call(self.app, "/api/v1/session/login", "POST",
                           {"return_to": "/app/"}, client=client)

    async def callback(self, login_data, login_cookies, subject="alice"):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(
            login_data["authorize_url"]).query)
        code = self.idp.issue_code(query["nonce"][0], subject)
        return await _call(
            self.app, "/api/v1/session/callback",
            cookies={_auth.TX_COOKIE: login_cookies[_auth.TX_COOKIE]},
            query=urllib.parse.urlencode({"state": query["state"][0],
                                          "code": code}))

    async def sign_in(self, subject="alice"):
        status, data, cookies = await self.login()
        self.assertEqual(status, 200, data)
        return await self.callback(data, cookies, subject)


class HeldProviderDoesNotBlockTest(_Base):
    async def test_held_discovery_does_not_delay_health(self):
        self.idp.modes[DISCOVERY] = "hold"
        started = time.monotonic()
        login = asyncio.ensure_future(self.login())
        await asyncio.sleep(0.2)
        status, _data, _c = await _call(self.app, "/healthz")
        elapsed = time.monotonic() - started
        self.idp.release.set()
        login_status, _d, _c = await login
        self.assertEqual(status, 200)
        self.assertLess(elapsed, 1.0, "a held discovery stalled /healthz")
        self.assertEqual(login_status, 200)

    async def test_held_token_exchange_does_not_delay_health(self):
        status, data, cookies = await self.login()
        self.assertEqual(status, 200, data)
        self.idp.modes["/token"] = "hold"
        started = time.monotonic()
        callback = asyncio.ensure_future(self.callback(data, cookies))
        await asyncio.sleep(0.2)
        status, _data, _c = await _call(self.app, "/healthz")
        elapsed = time.monotonic() - started
        self.idp.release.set()
        cb_status, _d, cb_cookies = await callback
        self.assertEqual(status, 200)
        self.assertLess(elapsed, 1.0, "a held token exchange stalled /healthz")
        self.assertEqual(cb_status, 302)
        self.assertTrue(cb_cookies.get(_auth.SESSION_COOKIE))


class UnboundCallbackTest(_Base):
    async def test_malformed_or_unbound_callbacks_make_no_provider_request(self):
        status, data, cookies = await self.login()
        self.assertEqual(status, 200, data)
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(
            data["authorize_url"]).query)["state"][0]
        # An empty cache: an expired discovery must not be refetched for
        # a callback that is refused anyway.
        self.app.signin_cache = _auth.ProviderCache()
        before = self.idp.provider_requests()
        cases = [
            ("", None, "callback is missing state or code"),
            ("state=" + state, None, "callback is missing state or code"),
            ("code=c1", None, "callback is missing state or code"),
            ("error=access_denied", None, "provider refused sign-in"),
            (urllib.parse.urlencode({"state": "made-up", "code": "c"}),
             {_auth.TX_COOKIE: cookies[_auth.TX_COOKIE]},
             "unknown or expired sign-in request"),
            (urllib.parse.urlencode({"state": state, "code": "c"}),
             {_auth.TX_COOKIE: "someone-else"},
             "sign-in was not initiated by this browser"),
        ]
        for query, jar, message in cases:
            status, body, set_cookies = await _call(
                self.app, "/api/v1/session/callback", cookies=jar, query=query)
            self.assertEqual(status, 400, (query, body))
            self.assertEqual(body["error"]["message"], message)
            self.assertNotIn(_auth.SESSION_COOKIE, set_cookies)
        self.assertEqual(self.idp.provider_requests(), before,
                         "a malformed or unbound callback reached the provider")


class ProviderCacheTest(_Base):
    async def sign_in(self, subject="alice"):
        self.now[0] += 60  # stay inside the per-address sign-in rate
        return await super().sign_in(subject)

    async def test_discovery_and_jwks_are_cached_with_bounded_refresh(self):
        clock = [500.0]
        self.app.signin_cache = _auth.ProviderCache(clock=lambda: clock[0])
        for _ in range(3):
            status, _d, cookies = await self.sign_in()
            self.assertEqual(status, 302)
            self.assertTrue(cookies.get(_auth.SESSION_COOKIE))
        self.assertEqual(self.idp.counts[DISCOVERY], 1)
        self.assertEqual(self.idp.counts["/jwks"], 1)
        clock[0] += _auth.PROVIDER_CACHE_TTL_S + 1
        status, _d, _c = await self.sign_in()
        self.assertEqual(status, 302)
        self.assertEqual(self.idp.counts[DISCOVERY], 2)
        self.assertEqual(self.idp.counts["/jwks"], 2)
        # Key rotation: an unknown kid refreshes the JWKS, but at most once
        # per JWKS_KID_REFRESH_S.
        self.idp.rotate("k2")
        status, _d, cookies = await self.sign_in()
        self.assertEqual(status, 503)
        self.assertNotIn(_auth.SESSION_COOKIE, cookies)
        self.assertEqual(self.idp.counts["/jwks"], 2)
        clock[0] += _auth.JWKS_KID_REFRESH_S + 1
        status, _d, cookies = await self.sign_in()
        self.assertEqual(status, 302)
        self.assertEqual(self.idp.counts["/jwks"], 3)
        status, _d, _c = await self.sign_in()
        self.assertEqual(status, 302)
        self.assertEqual(self.idp.counts["/jwks"], 3)
        self.assertEqual(self.idp.counts[DISCOVERY], 2)


class PreAuthLimitTest(_Base):
    async def test_excess_logins_are_refused_before_provider_io_and_recover(self):
        self.idp.modes[DISCOVERY] = "fail"  # never cached: one fetch per admitted login
        statuses = []
        for _ in range(60):
            status, body, _c = await self.login()
            statuses.append(status)
        refused = statuses.count(429)
        self.assertGreater(refused, 0, "no login was refused")
        self.assertEqual(self.idp.counts[DISCOVERY], len(statuses) - refused)
        self.assertEqual(set(statuses) - {429}, {503})
        # Another address has its own budget.
        status, _b, _c = await self.login(client=("203.0.113.9", 1))
        self.assertEqual(status, 503)
        self.now[0] += 3600
        status, _b, _c = await self.login()
        self.assertEqual(status, 503)

    async def test_excess_callbacks_are_refused_and_recover(self):
        statuses = []
        for _ in range(60):
            status, _b, _c = await _call(self.app, "/api/v1/session/callback")
            statuses.append(status)
        self.assertIn(429, statuses)
        self.assertEqual(set(statuses), {400, 429})
        self.assertEqual(self.idp.provider_requests(), 0)
        self.now[0] += 3600
        status, _b, _c = await _call(self.app, "/api/v1/session/callback")
        self.assertEqual(status, 400)

    async def test_concurrent_sign_ins_are_capped_and_capacity_recovers(self):
        self.idp.modes[DISCOVERY] = "hold"
        same = [asyncio.ensure_future(self.login()) for _ in range(6)]
        many = [asyncio.ensure_future(self.login(client=(f"192.0.2.{i}", 1)))
                for i in range(1, 13)]
        await asyncio.sleep(0.3)
        fetches_while_held = self.idp.counts[DISCOVERY]
        self.idp.release.set()
        results = await asyncio.gather(*same, *many)
        statuses = [r[0] for r in results]
        refused = statuses.count(429)
        self.assertGreater(refused, 0)
        self.assertEqual(fetches_while_held, len(statuses) - refused)
        self.assertLessEqual(fetches_while_held, _asgi.SIGNIN_MAX_CONCURRENT)
        self.assertGreater(statuses[:6].count(429), 0)
        self.idp.release.set()
        self.idp.modes.pop(DISCOVERY)
        self.now[0] += 3600
        status, _b, _c = await self.login()
        self.assertEqual(status, 200)


class ProviderSlotTest(_Base):
    """Requests refused before the provider spend only their own address's
    budget; provider work takes one of the provider slots (review rounds
    1 and 2)."""

    async def bare_callback(self, client):
        status, _b, _c = await _call(self.app, "/api/v1/session/callback",
                                     client=client)
        return status

    async def test_bare_callback_flood_does_not_block_a_fresh_sign_in(self):
        statuses = collections.Counter()
        for i in range(1, 14):
            for _ in range(30):
                statuses[await self.bare_callback((f"10.0.0.{i}", 1))] += 1
        self.assertEqual(self.idp.provider_requests(), 0)
        self.assertEqual(set(statuses), {400, 429})
        status, body, cookies = await self.sign_in()
        self.assertEqual(status, 302, body)
        self.assertTrue(cookies.get(_auth.SESSION_COOKIE))

    async def test_cached_logins_from_many_addresses_do_not_block(self):
        status, _b, _c = await self.login(client=("10.0.1.1", 1))
        self.assertEqual(status, 200)  # discovery is now cached
        for i in range(2, 200):
            status, _b, _c = await self.login(client=(f"10.0.1.{i}", 1))
            self.assertEqual(status, 200)
        self.assertEqual(self.idp.counts[DISCOVERY], 1)
        status, body, cookies = await self.sign_in()
        self.assertEqual(status, 302, body)

    async def test_busy_slots_roll_the_claim_back(self):
        status, data, cookies = await self.login()
        self.assertEqual(status, 200)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(
            data["authorize_url"]).query)
        code = self.idp.issue_code(query["nonce"][0], "alice")
        args = dict(cookies={_auth.TX_COOKIE: cookies[_auth.TX_COOKIE]},
                    query=urllib.parse.urlencode({"state": query["state"][0],
                                                  "code": code}))
        before = self.idp.provider_requests()
        with mock.patch.object(self.app.signin_limiter, "admit_provider",
                               lambda now: 5.0):
            status, _b, set_cookies = await _call(
                self.app, "/api/v1/session/callback", **args)
        self.assertEqual(status, 429)
        self.assertEqual(self.idp.provider_requests(), before)
        status, body, set_cookies = await _call(
            self.app, "/api/v1/session/callback", **args)
        self.assertEqual(status, 302, body)
        self.assertTrue(set_cookies.get(_auth.SESSION_COOKIE))

    async def test_fake_code_pairs_from_many_addresses_do_not_lock_out_a_victim(self):
        """Review round 2, MED-A: self-made claims with made-up codes reach
        the token endpoint but no longer drain a shared budget."""
        status, _b, _c = await self.login(client=("10.0.4.250", 1))
        self.assertEqual(status, 200)  # discovery cached
        refused = collections.Counter()
        for i in range(1, 31):
            for _ in range(5):
                client = (f"10.0.4.{i}", 1)
                status, data, cookies = await self.login(client=client)
                refused[status] += 1
                state = urllib.parse.parse_qs(urllib.parse.urlsplit(
                    data["authorize_url"]).query)["state"][0]
                status, body, _c = await _call(
                    self.app, "/api/v1/session/callback", client=client,
                    cookies={_auth.TX_COOKIE: cookies[_auth.TX_COOKIE]},
                    query=urllib.parse.urlencode({"state": state,
                                                  "code": "made-up"}))
                refused[status] += 1
                self.assertEqual(body["error"]["message"],
                                 "provider refused sign-in")
        self.assertEqual(self.idp.counts["/token"], 150)
        self.assertEqual(set(refused), {200, 400})
        status, body, cookies = await self.sign_in()
        self.assertEqual(status, 302, body)

    async def test_ipv6_addresses_in_one_64_share_a_budget(self):
        statuses = [await self.bare_callback(("2001:db8:1:2::1", 1))
                    for _ in range(_asgi.SIGNIN_BURST)]
        self.assertEqual(set(statuses), {400})
        self.assertEqual(await self.bare_callback(("2001:db8:1:2:ffff::9", 1)),
                         429)
        self.assertEqual(await self.bare_callback(("2001:db8:1:3::1", 1)), 400)
        # IPv4 stays the full address.
        for _ in range(_asgi.SIGNIN_BURST):
            await self.bare_callback(("192.0.2.1", 1))
        self.assertEqual(await self.bare_callback(("192.0.2.1", 1)), 429)
        self.assertEqual(await self.bare_callback(("192.0.2.2", 1)), 400)


class LimiterTableTest(_Base):
    """Review round 1, MED-2: the per-address table is bounded."""

    async def test_table_stays_under_the_cap_and_limits_still_apply(self):
        with mock.patch.object(_asgi, "SIGNIN_MAX_TRACKED", 50, create=True):
            for i in range(400):
                status, _b, _c = await _call(
                    self.app, "/api/v1/session/callback",
                    client=(f"10.3.{i // 250}.{i % 250}", 1))
                self.assertEqual(status, 400)
                self.assertLessEqual(self.app.signin_limiter.tracked(), 50)
            statuses = []
            for _ in range(30):
                status, _b, _c = await _call(
                    self.app, "/api/v1/session/callback",
                    client=("10.3.9.9", 1))
                statuses.append(status)
            self.assertEqual(statuses[0], 400)
            self.assertIn(429, statuses)
            self.assertLessEqual(self.app.signin_limiter.tracked(), 50)

    async def test_prune_scans_stay_rare_when_entries_are_in_flight(self):
        """Review round 2, LOW-B: a table full of in-flight entries is not
        rescanned on every new address."""
        class Counting(dict):
            scans = 0

            def __iter__(self):
                Counting.scans += 1
                return super().__iter__()

        limiter = self.app.signin_limiter
        limiter._addresses = Counting()
        with mock.patch.object(_asgi, "SIGNIN_MAX_TRACKED", 50, create=True):
            for i in range(50):
                self.assertEqual(limiter.admit_address(f"a{i}", 0.0), 0.0)
            Counting.scans = 0
            for i in range(500):  # never released: all stay in flight
                self.assertEqual(limiter.admit_address(f"b{i}", 0.0), 0.0)
        # One prune pass (two scans) per growth of a tenth of the cap.
        self.assertLessEqual(Counting.scans, 2 * (500 // 5) + 2)


class MalformedJwksTest(_Base):
    """Review round 1, LOW-1: a malformed JWKS is a provider failure (503)."""

    async def test_malformed_jwks_answers_503(self):
        for payload in ({"keys": "x"},
                        {"keys": [{"kty": "nope", "kid": "k1"}]},
                        {"nokeys": 1}):
            self.now[0] += 60
            self.app.signin_cache = _auth.ProviderCache()
            self.idp.payloads["/jwks"] = payload
            status, data, cookies = await self.login()
            self.assertEqual(status, 200)
            status, body, set_cookies = await self.callback(data, cookies)
            self.assertEqual(status, 503, (payload, body))
            self.assertEqual(body["error"]["code"], "UNAVAILABLE")
            self.assertNotIn(_auth.SESSION_COOKIE, set_cookies)

    async def test_no_usable_signing_key_answers_503_without_library_text(self):
        """Review round 2, NIT-C."""
        from joserfc.jwk import ECKey
        other = dict(self.idp.public, kid="other")
        # An EC key under the RS256 token's kid passes the key filter and
        # the kid check, then fails as a key-type error inside joserfc.
        wrong_type = ECKey.generate_key("P-256", parameters={
            "kid": self.idp.kid}).as_dict(private=False)
        for payload in ({"keys": [{"kty": "oct", "k": "c2VjcmV0", "kid": "k1"}]},
                        {"keys": [other]}, {"keys": [wrong_type]}):
            self.now[0] += 60
            self.app.signin_cache = _auth.ProviderCache()
            self.idp.payloads["/jwks"] = payload
            status, data, cookies = await self.login()
            self.assertEqual(status, 200)
            status, body, set_cookies = await self.callback(data, cookies)
            self.assertEqual(status, 503, (payload, body))
            self.assertEqual(body["error"]["message"],
                             "sign-in provider is unavailable")
            self.assertNotIn(_auth.SESSION_COOKIE, set_cookies)


class ReplyBoundsTest(_Base):
    async def test_oversized_reply_is_cut_off(self):
        self.idp.modes[DISCOVERY] = "big"
        status, body, _c = await self.login()
        self.assertEqual(status, 503, body)
        self.assertEqual(body["error"]["code"], "UNAVAILABLE")

    async def test_oversized_jwks_is_cut_off(self):
        status, data, cookies = await self.login()
        self.assertEqual(status, 200)
        self.idp.modes["/jwks"] = "big"
        status, body, set_cookies = await self.callback(data, cookies)
        self.assertEqual(status, 503, body)
        self.assertNotIn(_auth.SESSION_COOKIE, set_cookies)

    async def test_slow_reply_is_cut_off_by_the_total_time_bound(self):
        with mock.patch.object(_auth, "PROVIDER_TOTAL_S", 1.0, create=True):
            for mode in ("drip", "hold"):
                self.idp.modes[DISCOVERY] = mode
                started = time.monotonic()
                status, body, _c = await self.login(
                    client=(f"198.51.100.{len(mode)}", 1))
                elapsed = time.monotonic() - started
                self.assertEqual(status, 503, (mode, body))
                self.assertLess(elapsed, 2.0, mode)

    async def test_token_endpoint_throttling_or_failure_answers_503(self):
        """Review round 3, LOW-1: a provider 429 (or 5xx) is the provider
        being unavailable, not this user's code being refused."""
        for mode in ("throttle", "fail"):
            self.now[0] += 60
            self.idp.modes["/token"] = mode
            status, data, cookies = await self.login()
            self.assertEqual(status, 200)
            status, body, set_cookies = await self.callback(data, cookies)
            self.assertEqual(status, 503, (mode, body))
            self.assertEqual(body["error"]["message"],
                             "sign-in provider is unavailable")
            self.assertNotIn(_auth.SESSION_COOKIE, set_cookies)

    async def test_slow_token_reply_is_cut_off(self):
        status, data, cookies = await self.login()
        self.assertEqual(status, 200)
        self.idp.modes["/token"] = "drip"
        with mock.patch.object(_auth, "PROVIDER_TOTAL_S", 1.0, create=True):
            started = time.monotonic()
            status, body, set_cookies = await self.callback(data, cookies)
            elapsed = time.monotonic() - started
        self.assertEqual(status, 503, body)
        self.assertLess(elapsed, 2.0)
        self.assertNotIn(_auth.SESSION_COOKIE, set_cookies)


class SignInStillWorksTest(_Base):
    async def test_valid_sign_in_yields_a_session(self):
        status, body, cookies = await self.sign_in()
        self.assertEqual(status, 302, body)
        self.assertEqual(body["return_to"], "/app/")
        self.assertTrue(cookies.get(_auth.SESSION_COOKIE))
        self.assertEqual(cookies.get(_auth.TX_COOKIE), "")
        status, me, _c = await _call(
            self.app, "/api/v1/session",
            cookies={_auth.SESSION_COOKIE: cookies[_auth.SESSION_COOKIE]})
        self.assertEqual(status, 200, me)

    async def test_non_admitted_account_is_still_refused(self):
        path = self.work / "admission.json"
        path.write_text(json.dumps({
            "status": "approved",
            "principals": [f"oidc:{self.idp.iss}:alice"],
            "verified_emails": [], "max_accounts": 5, "max_workspaces": 5}))
        os.environ["EARWITNESS_ADMISSION_FILE"] = str(path)
        status, body, cookies = await self.sign_in("mallory")
        self.assertEqual(status, 403, body)
        self.assertEqual(body["error"]["message"],
                         "this account is not admitted to the pilot")
        self.assertNotIn(_auth.SESSION_COOKIE, cookies)
        status, body, cookies = await self.sign_in("alice")
        self.assertEqual(status, 302, body)
        self.assertTrue(cookies.get(_auth.SESSION_COOKIE))

    async def test_foreign_cookie_refusal_leaves_the_real_browser_able_to_finish(self):
        """Review round 1, LOW-2: a refused claim is rolled back, so the
        initiating browser can still finish its own sign-in."""
        status, data, cookies = await self.login()
        self.assertEqual(status, 200)
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(
            data["authorize_url"]).query)["state"][0]
        status, body, set_cookies = await _call(
            self.app, "/api/v1/session/callback",
            cookies={_auth.TX_COOKIE: "someone-else"},
            query=urllib.parse.urlencode({"state": state, "code": "c"}))
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["message"],
                         "sign-in was not initiated by this browser")
        status, body, set_cookies = await self.callback(data, cookies)
        self.assertEqual(status, 302, body)
        self.assertTrue(set_cookies.get(_auth.SESSION_COOKIE))

    async def test_invalid_return_route_still_refused(self):
        status, body, _c = await _call(self.app, "/api/v1/session/login",
                                       "POST", {"return_to": "//evil.example"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["message"], "invalid return route")
        self.assertEqual(self.idp.provider_requests(), 0)


class SigninAddressTest(unittest.TestCase):
    """Review round 3, NIT-2: the per-address limit key."""

    def test_address_keys(self):
        key = _asgi._signin_address
        self.assertEqual(key("::ffff:1.2.3.4"), "1.2.3.4")
        self.assertEqual(key("::ffff:1.2.3.4%eth0"), "1.2.3.4")
        self.assertEqual(key("fe80::1%eth0"), "fe80::/64")
        self.assertEqual(key("2001:db8:1:2::1"), key("2001:db8:1:2:ffff::9"))
        self.assertNotEqual(key("2001:db8:1:2::1"), key("2001:db8:1:3::1"))
        self.assertEqual(key("192.0.2.1"), "192.0.2.1")
        self.assertNotEqual(key("192.0.2.1"), key("192.0.2.2"))
        self.assertNotEqual(key("::ffff:192.0.2.1"), key("192.0.2.2"))
        self.assertEqual(key("not-an-ip"), "not-an-ip")
        self.assertEqual(key(""), "unknown")
        self.assertEqual(key(None), "unknown")


if __name__ == "__main__":
    unittest.main()
