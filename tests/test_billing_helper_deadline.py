"""BY1: the bounded Stripe helper (design 8.5; billing R2-02, BUY-R3-07).

Rules under test (local fixture servers on 127.0.0.1 only; an audit hook
refuses every other socket connect while these tests run):
  * One 10 s deadline, started before any network step, enforced by a
    timer that shuts down and closes the socket the call currently owns.
    A server that stalls accepting (full backlog), in the TLS handshake,
    before the status line, dripping headers, dripping the body, or
    sending a huge error body ends the call within 10.5 s, and the
    calling thread ends.
  * A deadline that passed before a socket was published is seen right
    after publishing: no connect happens.
  * Replies are capped at 256 KiB and error bodies at 16 KiB.
  * Stripe-Version goes on every call; Idempotency-Key only when asked.
  * DNS resolution is NOT bounded by the helper (the named gap): a
    blocking resolver keeps the call running past the deadline.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from earwitness.app import billing as _billing

# -- network guard ----------------------------------------------------------

ALLOWED_PORTS: set = set()
CONNECTS: list = []
REFUSED: list = []
GUARD = {"on": 0}


def _audit(event, args):
    if event != "socket.connect" or not GUARD["on"]:
        return
    address = args[1] if len(args) > 1 else None
    if isinstance(address, tuple) and len(address) >= 2 \
            and address[0] == "127.0.0.1" and address[1] in ALLOWED_PORTS:
        CONNECTS.append(address)
        return
    REFUSED.append(address)
    raise ConnectionRefusedError(f"test network guard refused {address!r}")


sys.addaudithook(_audit)


class Guarded(unittest.TestCase):
    """Every socket connect outside the fixture servers is refused."""

    def setUp(self):
        GUARD["on"] += 1
        self.addCleanup(self._unguard)
        self._refused_before = len(REFUSED)

    def _unguard(self):
        GUARD["on"] -= 1

    def assert_no_refused_connects(self):
        self.assertEqual(REFUSED[self._refused_before:], [])


# -- fixture TLS certificate (trusted only by the test context) -------------

CERT: dict = {}


def setUpModule():
    if not shutil.which("openssl"):
        return
    tmp = tempfile.mkdtemp(prefix="ew-by1-tls-")
    cert, key = os.path.join(tmp, "cert.pem"), os.path.join(tmp, "key.pem")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", key, "-out", cert, "-days", "1", "-subj", "/CN=127.0.0.1",
         "-addext", "subjectAltName=IP:127.0.0.1"],
        check=True, capture_output=True)
    CERT.update(dir=tmp, cert=cert, key=key)
    atexit.register(shutil.rmtree, tmp, True)


def tearDownModule():
    if CERT.get("dir"):
        shutil.rmtree(CERT["dir"], ignore_errors=True)
    # Forget the deleted files, so a later module's need_tls makes new ones.
    CERT.clear()


def need_tls(test):
    if not CERT:
        setUpModule()  # another module's test may need the fixture first
    if not CERT:
        test.skipTest("openssl is not available to make the fixture certificate")


def client_context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=CERT["cert"])


def _server_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT["cert"], CERT["key"])
    return ctx


# -- fixture servers ---------------------------------------------------------

class Server:
    """A local fixture server. `behaviour(conn, server)` answers one
    request after the TLS handshake; 'tls_stall' never handshakes and
    'backlog' never accepts."""

    def __init__(self, behaviour, *, tls=True):
        self.behaviour = behaviour
        self.tls = tls
        self.stop = threading.Event()
        self.requests: list = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        ALLOWED_PORTS.add(self.port)
        self.fillers: list = []
        if behaviour == "backlog":
            self.sock.listen(1)
            self._fill_backlog()
            return
        self.sock.listen(16)
        self.sock.settimeout(0.1)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _fill_backlog(self):
        for _ in range(1024):
            filler = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            filler.settimeout(0.3)
            try:
                filler.connect(("127.0.0.1", self.port))
            except (TimeoutError, socket.timeout):
                filler.close()
                return
            except OSError:
                filler.close()
                return
            self.fillers.append(filler)
        raise AssertionError("could not fill the listen backlog")

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.port}/v1"

    def _serve(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,),
                             daemon=True).start()

    def _handle(self, conn):
        try:
            if self.behaviour == "tls_stall":
                self.stop.wait(30)
                return
            if self.tls:
                conn = _server_context().wrap_socket(conn, server_side=True)
            conn.settimeout(15)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                head += chunk
            raw_head, _, rest = head.partition(b"\r\n\r\n")
            lines = raw_head.decode("latin-1").split("\r\n")
            headers = {}
            for line in lines[1:]:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
            if headers.get("host") != f"127.0.0.1:{self.port}":
                conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n"
                             b"Connection: close\r\n\r\n")
                return                              # an outside probe: not recorded
            length = int(headers.get("content-length") or 0)
            while len(rest) < length:
                rest += conn.recv(4096)
            self.requests.append({"line": lines[0], "headers": headers,
                                  "body": rest})
            self.behaviour(conn, self)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        self.stop.set()
        for filler in self.fillers:
            filler.close()
        self.sock.close()
        ALLOWED_PORTS.discard(self.port)


def _stall_status(conn, server):
    server.stop.wait(30)


def _drip_headers(conn, server):
    conn.sendall(b"HTTP/1.1 200 OK\r\n")
    while not server.stop.wait(0.5):
        conn.sendall(b"X")


def _drip_body(conn, server):
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                 b"Content-Length: 1000000\r\n\r\n{")
    while not server.stop.wait(0.5):
        conn.sendall(b" ")


def _huge_error(conn, server):
    conn.sendall(b"HTTP/1.1 500 Internal Server Error\r\n"
                 b"Content-Type: application/json\r\n"
                 b"Content-Length: 50000000\r\n\r\n")
    block = b"x" * 65536
    while not server.stop.is_set():
        conn.sendall(block)


def _reply(status=200, body=b'{"id": "sub_1", "object": "subscription"}'):
    def behaviour(conn, server):
        conn.sendall(f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n"
                     f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                     .encode() + body)
    return behaviour


class HelperStallTest(Guarded):
    """Each stall phase ends within 10.5 s on the real 10 s deadline. The
    six cases run at once, so the test takes about ten seconds."""

    def test_every_stall_phase_times_out_and_the_thread_ends(self):
        need_tls(self)
        cases = {"accepting (full backlog)": "backlog",
                 "TLS handshake": "tls_stall",
                 "status line": _stall_status,
                 "dripping headers": _drip_headers,
                 "dripping body": _drip_body,
                 "huge error body": _huge_error}
        servers = {name: Server(b) for name, b in cases.items()}
        for server in servers.values():
            self.addCleanup(server.close)
        results: dict = {}

        def run(name, server):
            started = time.monotonic()
            try:
                got = _billing.bounded_request(
                    "GET", server.url + "/subscriptions/x",
                    ssl_context=client_context())
                results[name] = (got, time.monotonic() - started)
            except Exception as exc:  # noqa: BLE001 - recorded and asserted
                results[name] = (exc, time.monotonic() - started)

        threads = {name: threading.Thread(target=run, args=(name, s), daemon=True)
                   for name, s in servers.items()}
        for thread in threads.values():
            thread.start()
        for name, thread in threads.items():
            thread.join(12.0)
            with self.subTest(name):
                self.assertFalse(thread.is_alive(), f"{name}: thread still running")
                outcome, elapsed = results[name]
                self.assertLessEqual(elapsed, 10.5, name)
                if name == "huge error body":
                    # Read only up to the error cap, then answered at once.
                    status, data = outcome
                    self.assertEqual(status, 500)
                    self.assertLessEqual(len(data), _billing.PROVIDER_MAX_ERROR_BYTES)
                    self.assertLess(elapsed, 5.0, name)
                else:
                    self.assertIsInstance(
                        outcome, _billing.ProviderTimeout,
                        f"{name}: {outcome!r} cause {getattr(outcome, '__cause__', None)!r}"
                        f" after {elapsed:.2f}s")
                    if name != "accepting (full backlog)":
                        # macOS gives up on a dropped SYN by itself (about
                        # 8 s); the timer cut of connect is shown below.
                        self.assertGreaterEqual(elapsed, 9.0, name)
        self.assert_no_refused_connects()

    def test_the_timer_cuts_a_blocked_connect(self):
        need_tls(self)
        server = Server("backlog")
        self.addCleanup(server.close)
        started = time.monotonic()
        with self.assertRaises(_billing.ProviderTimeout) as caught:
            _billing.bounded_request("GET", server.url + "/x", deadline_s=2.0,
                                     ssl_context=client_context())
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 1.9)
        self.assertLess(elapsed, 2.5)
        # Cut by the timer (socket closed under connect), not an OS timeout.
        self.assertNotIsInstance(caught.exception.__cause__, TimeoutError)
        self.assert_no_refused_connects()

    def test_deadline_constant_is_ten_seconds(self):
        self.assertEqual(_billing.PROVIDER_DEADLINE_S, 10.0)
        self.assertEqual(_billing.PROVIDER_MAX_REPLY_BYTES, 256 * 1024)
        self.assertEqual(_billing.PROVIDER_MAX_ERROR_BYTES, 16 * 1024)


class HelperBoundsTest(Guarded):
    def _server(self, behaviour):
        need_tls(self)
        server = Server(behaviour)
        self.addCleanup(server.close)
        return server

    def test_over_size_reply_is_refused(self):
        server = self._server(_reply(body=b"[" + b"0," * (140 * 1024) + b"0]"))
        with self.assertRaises(_billing.ProviderTooLarge):
            _billing.bounded_request("GET", server.url + "/subscriptions/x",
                                     ssl_context=client_context())

    def test_reply_at_the_cap_is_returned(self):
        body = b"x" * _billing.PROVIDER_MAX_REPLY_BYTES
        server = self._server(_reply(body=body))
        status, data = _billing.bounded_request(
            "GET", server.url + "/x", ssl_context=client_context())
        self.assertEqual((status, len(data)), (200, len(body)))

    def test_error_body_is_capped(self):
        server = self._server(_reply(status=402, body=b"e" * (40 * 1024)))
        status, data = _billing.bounded_request(
            "GET", server.url + "/x", ssl_context=client_context())
        self.assertEqual(status, 402)
        self.assertLessEqual(len(data), _billing.PROVIDER_MAX_ERROR_BYTES)

    def test_stripe_version_on_every_call_and_idempotency_key_on_request(self):
        server = self._server(_reply())
        _billing.bounded_request("GET", server.url + "/a",
                                 ssl_context=client_context())
        _billing.bounded_request("POST", server.url + "/b", body=b"a=1",
                                 idempotency_key="key-1",
                                 ssl_context=client_context())
        first, second = server.requests
        for req in (first, second):
            self.assertEqual(req["headers"].get("stripe-version"),
                             _billing.STRIPE_VERSION)
        self.assertNotIn("idempotency-key", first["headers"])
        self.assertEqual(second["headers"].get("idempotency-key"), "key-1")
        self.assertEqual(_billing.STRIPE_VERSION, "2026-09-30.endive")

    def test_stripe_request_uses_the_helper_with_auth_and_version(self):
        server = self._server(_reply(body=json.dumps(
            {"id": "sub_1", "object": "subscription", "status": "active",
             "customer": "cus_1"}).encode()))
        config = _billing.BillingConfig(secret_key="sk_fixture",
                                        webhook_secret="whsec",
                                        success_url="/s", cancel_url="/c")
        with mock.patch.object(_billing, "STRIPE_API", server.url), \
                mock.patch.object(_billing, "_tls_context", client_context):
            got = _billing._stripe_request(config, "GET", "/subscriptions/sub_1", {})
        self.assertEqual(got["id"], "sub_1")
        req = server.requests[0]
        self.assertTrue(req["line"].startswith("GET /v1/subscriptions/sub_1 "))
        self.assertTrue(req["headers"]["authorization"].startswith("Basic "))
        self.assertEqual(req["headers"]["stripe-version"], _billing.STRIPE_VERSION)

    def test_a_request_for_another_host_is_not_recorded(self):
        server = self._server(_reply())
        with socket.create_connection(("127.0.0.1", server.port), timeout=10) as raw, \
                client_context().wrap_socket(raw, server_hostname="127.0.0.1") as tls:
            tls.sendall(f"POST /v1/x HTTP/1.1\r\nHost: localhost:{server.port}\r\n"
                        f"Content-Length: 100\r\nConnection: close\r\n\r\n".encode())
            # the body never comes: the 404 must not wait for it
            self.assertTrue(tls.recv(4096).startswith(b"HTTP/1.1 404"))
        self.assertEqual(server.requests, [])

    def test_no_bare_urlopen_left_in_billing(self):
        import inspect
        self.assertNotIn("urlopen", inspect.getsource(_billing))


class HelperDnsGapTest(Guarded):
    def test_deadline_seen_right_after_publishing_means_no_connect(self):
        need_tls(self)
        server = Server(_reply())
        self.addCleanup(server.close)

        def slow_resolve(host, port):
            time.sleep(0.4)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     ("127.0.0.1", port))]

        before = len(CONNECTS)
        started = time.monotonic()
        with mock.patch.object(_billing, "_resolve", slow_resolve):
            with self.assertRaises(_billing.ProviderTimeout):
                _billing.bounded_request("GET", server.url + "/x",
                                         deadline_s=0.1,
                                         ssl_context=client_context())
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(CONNECTS[before:], [], "connected after the deadline")
        self.assertEqual(server.requests, [])

    def test_a_blocking_resolver_is_not_bounded_by_the_helper(self):
        # The named remaining gap (G2): getaddrinfo cannot be aborted, so
        # the call outlives its deadline while the resolver blocks. The web
        # process bounds this with its two slots (test_billing_webhook_single).
        release = threading.Event()

        def stuck_resolve(host, port):
            release.wait(5)
            raise OSError("resolver stub gave up")

        done = threading.Event()
        outcome = {}

        def run():
            try:
                _billing.bounded_request("GET", "https://api.stripe.invalid/v1/x",
                                         deadline_s=0.2)
            except Exception as exc:  # noqa: BLE001
                outcome["exc"] = exc
            done.set()

        with mock.patch.object(_billing, "_resolve", stuck_resolve):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            self.assertFalse(done.wait(0.6), "the helper claimed to bound DNS")
            release.set()
            thread.join(5)
        self.assertIsInstance(outcome.get("exc"), _billing.ProviderError)
        self.assert_no_refused_connects()


class FixtureCertLifecycleTest(unittest.TestCase):
    """The module teardown deletes the fixture certificate. A later module
    that calls need_tls must get a fresh one, not the deleted path (the
    hosted suite on d6e1720 ran this module before
    test_billing_webhook_single and its over-size test then failed TLS)."""

    def test_need_tls_after_teardown_makes_a_new_certificate(self):
        if not shutil.which("openssl"):
            self.skipTest("openssl is not available")
        need_tls(self)
        old = CERT["cert"]
        tearDownModule()
        self.assertFalse(os.path.exists(old), "teardown must delete the file")
        # openssl is present, so a skip here would hide the bug: fail instead.
        with mock.patch.object(self, "skipTest", side_effect=AssertionError(
                "need_tls skipped although openssl is available")):
            need_tls(self)
        self.assertTrue(os.path.exists(CERT["cert"]))
        self.assertTrue(os.path.exists(CERT["key"]))


if __name__ == "__main__":
    unittest.main()
