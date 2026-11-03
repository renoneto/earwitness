"""Meta model client and its Ask spend ledger (T1). Offline: fake keys, fake
transports and a patched opener only; nothing here touches the network or a
real key file."""
from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import logging
import os
import shutil
import socket
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from earwitness.app import meta_client as mc
from earwitness.pipeline import jev_budget as jb

LA = ZoneInfo("America/Los_Angeles")
KEY = "LLM_fake_test_key_9f3a"
SENTINEL = "PASSAGE_TEXT_SENTINEL"
REPLY_SENTINEL = "REPLYBODY_SENTINEL"
BODY = {"model": "muse-spark-1.3",
        "messages": [{"role": "user", "content": f"{SENTINEL} what do hosts say"}],
        "max_tokens": 100}
GOOD_REPLY = {"choices": [{"message": {"role": "assistant", "content": "hi"}}],
              "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


def la(*args) -> float:
    return datetime(*args, tzinfo=LA).timestamp()


def reply_bytes(**over) -> bytes:
    doc = json.loads(json.dumps(GOOD_REPLY))
    doc.update(over)
    return json.dumps(doc).encode()


class FakeTransport:
    """Records every call; `script` entries are returns or exceptions."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def __call__(self, url, headers, data, timeout_s):
        self.calls.append((url, headers, data, timeout_s))
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, BaseException):
            raise step
        return step


def ok_transport():
    return FakeTransport((200, {}, reply_bytes()))


class ConstantsTest(unittest.TestCase):
    def test_pinned_values(self):
        self.assertEqual(mc.MODEL, "muse-spark-1.3")
        self.assertEqual(mc.BASE_URL, "https://api.meta.ai/v1")
        self.assertEqual(mc.META_PRICING, jb.Pricing(
            1.25, 4.25, "https://dev.meta.ai/docs/pricing-rate-limits", "2026-10-23"))
        self.assertEqual(mc.TURN_BOUND_USD, 0.42)
        self.assertEqual(mc.TURN_BODY_BYTES, 160_000)
        self.assertEqual(mc.TURN_OUTPUT_TOKENS, 32_000)
        self.assertEqual(mc.ENV_KEY_FILE, "EARWITNESS_META_KEY_FILE")
        self.assertEqual(mc.DEFAULT_KEY_FILE, "/etc/earwitness/meta-model-api-key")
        self.assertEqual(mc.ENV_BUDGET_DIR, "EARWITNESS_META_BUDGET_DIR")
        self.assertEqual(mc.DEFAULT_BUDGET_DIR, "/srv/earwitness-vol/meta-budget")

    def test_turn_bound_is_the_maximum_charge_of_a_turn(self):
        self.assertAlmostEqual(
            jb.max_charge_usd(mc.TURN_BODY_BYTES, mc.TURN_OUTPUT_TOKENS, mc.META_PRICING),
            mc.TURN_BOUND_USD, places=9)

    def test_key_file_path_and_budget_dir_use_env_then_default(self):
        self.assertEqual(mc.key_file_path({}), mc.DEFAULT_KEY_FILE)
        self.assertEqual(mc.key_file_path({mc.ENV_KEY_FILE: ""}), mc.DEFAULT_KEY_FILE)
        self.assertEqual(mc.key_file_path({mc.ENV_KEY_FILE: "/x/key"}), "/x/key")
        self.assertEqual(mc.budget_dir({}), mc.DEFAULT_BUDGET_DIR)
        self.assertEqual(mc.budget_dir({mc.ENV_BUDGET_DIR: ""}), mc.DEFAULT_BUDGET_DIR)
        self.assertEqual(mc.budget_dir({mc.ENV_BUDGET_DIR: "/x/budget"}), "/x/budget")


class CleanErrorMixin:
    """An error that names a code and never carries a secret, a cause or a
    chained context."""

    def assertClean(self, exc, secrets=()):
        forms = (str(exc), repr(exc), repr(exc.args), str(exc.args))
        for secret in secrets:
            for form in forms:
                self.assertNotIn(secret, form)
        self.assertIsNone(exc.__cause__)
        # Not even a suppressed context: it could hold the reply or a transport's text.
        self.assertIsNone(exc.__context__)


class LoadKeyTest(CleanErrorMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="metakey-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.n = 0

    def write(self, content: bytes, mode=0o600) -> str:
        self.n += 1
        path = self.tmp / f"key{self.n}"
        path.write_bytes(content)
        os.chmod(path, mode)
        return str(path)

    def refused(self, path) -> mc.MetaKeyError:
        with self.assertRaises(mc.MetaKeyError) as cm:
            mc.load_key(path)
        return cm.exception

    def test_a_good_key_is_returned_without_its_one_trailing_newline(self):
        self.assertEqual(mc.load_key(self.write(b"LLM_x\n")), "LLM_x")
        self.assertEqual(mc.load_key(self.write(b"LLM_x")), "LLM_x")
        self.assertEqual(mc.load_key(self.write(KEY.encode() + b"\n")), KEY)

    def test_modes_0600_and_0400_are_accepted(self):
        self.assertEqual(mc.load_key(self.write(b"LLM_x\n", 0o600)), "LLM_x")
        self.assertEqual(mc.load_key(self.write(b"LLM_x\n", 0o400)), "LLM_x")

    def test_group_or_other_access_is_refused(self):
        for mode in (0o644, 0o640, 0o604, 0o660, 0o606, 0o620, 0o602, 0o610, 0o601):
            with self.subTest(mode=oct(mode)):
                exc = self.refused(self.write(b"LLM_SECRETVALUE_abc\n", mode))
                self.assertEqual(exc.code, "META_KEY_MODE")
                self.assertClean(exc, ["LLM_SECRETVALUE_abc", "SECRETVALUE"])

    def test_missing_path(self):
        exc = self.refused(str(self.tmp / "nope"))
        self.assertEqual(exc.code, "META_KEY_MISSING")
        self.assertClean(exc)

    def test_symlink_is_refused_even_to_a_good_file(self):
        target = self.write(b"LLM_SECRETVALUE_abc\n", 0o600)
        link = self.tmp / "link"
        os.symlink(target, link)
        exc = self.refused(str(link))
        self.assertEqual(exc.code, "META_KEY_NOT_FILE")
        self.assertClean(exc, ["LLM_SECRETVALUE_abc", "SECRETVALUE"])

    def test_directory_is_refused(self):
        folder = self.tmp / "dir"
        folder.mkdir()
        exc = self.refused(str(folder))
        self.assertEqual(exc.code, "META_KEY_NOT_FILE")
        self.assertClean(exc)

    def test_another_owner_is_refused(self):
        path = self.write(b"LLM_SECRETVALUE_abc\n", 0o600)
        other = os.stat(path).st_uid + 1
        with mock.patch("os.geteuid", return_value=other):
            exc = self.refused(path)
        self.assertEqual(exc.code, "META_KEY_OWNER")
        self.assertClean(exc, ["LLM_SECRETVALUE_abc", "SECRETVALUE"])

    def test_malformed_values_are_refused_without_echoing_them(self):
        cases = {
            "empty": b"",
            "newline only": b"\n",
            "space inside": b"LLM_SECRETVALUE a b",
            "newline inside": b"LLM_SECRETVALUE_a\nb",
            "two newlines": b"LLM_SECRETVALUE_a\n\n",
            "crlf": b"LLM_SECRETVALUE_a\r\n",
            "tab inside": b"LLM_SECRETVALUE_a\tb",
            "leading space": b" LLM_SECRETVALUE_a",
            "non-ascii": "LLM_SECRETVALUE_é".encode(),
            "nul byte": b"LLM_SECRETVALUE_a\x00b",
            "5000 bytes": b"LLM_SECRETVALUE_" + b"x" * 4984,
        }
        self.assertEqual(len(cases["5000 bytes"]), 5000)
        for label, content in cases.items():
            with self.subTest(label):
                exc = self.refused(self.write(content))
                self.assertEqual(exc.code, "META_KEY_FORMAT")
                secrets = ["SECRETVALUE"]
                text = content.decode("utf-8").strip("\n")
                if text:
                    secrets.append(text)
                self.assertClean(exc, secrets)


class ClientConstructionTest(CleanErrorMixin, unittest.TestCase):
    def test_malformed_keys_are_refused(self):
        for bad in ("a b", ""):
            with self.subTest(key=bad):
                with self.assertRaises(mc.MetaKeyError) as cm:
                    mc.MetaClient(bad)
                self.assertEqual(cm.exception.code, "META_KEY_FORMAT")
                self.assertClean(cm.exception, [bad] if bad else [])

    def test_non_https_base_url_is_refused(self):
        with self.assertRaises(ValueError):
            mc.MetaClient(KEY, base_url="http://api.meta.ai/v1")

    def test_the_key_is_not_in_repr_or_str(self):
        client = mc.MetaClient(KEY, transport=ok_transport())
        self.assertNotIn(KEY, repr(client))
        self.assertNotIn(KEY, str(client))
        self.assertNotIn("LLM_fake", repr(client))


class ChatTest(CleanErrorMixin, unittest.TestCase):
    def client(self, transport, **kw):
        return mc.MetaClient(KEY, transport=transport, **kw)

    def call_error(self, transport, body=BODY) -> mc.MetaCallError:
        with self.assertRaises(mc.MetaCallError) as cm:
            self.client(transport).chat(body, timeout_s=5)
        return cm.exception

    def assertCallClean(self, exc, *extra):
        self.assertClean(exc, [KEY, "Bearer", SENTINEL, REPLY_SENTINEL, *extra])

    def test_one_transport_call_with_the_exact_request(self):
        transport = ok_transport()
        reply = self.client(transport).chat(BODY, timeout_s=7.5)
        self.assertEqual(len(transport.calls), 1)
        url, headers, data, timeout_s = transport.calls[0]
        self.assertEqual(url, "https://api.meta.ai/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer " + KEY)
        self.assertEqual(json.loads(data), BODY)
        self.assertNotIn(KEY.encode(), data)
        self.assertEqual(timeout_s, 7.5)
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.message, {"role": "assistant", "content": "hi"})
        self.assertEqual(reply.usage, GOOD_REPLY["usage"])
        self.assertEqual(reply.request_bytes, len(data))

    def test_a_body_that_cannot_be_encoded_is_refused_without_its_text(self):
        # Review r4 item 3: a lone surrogate's UnicodeEncodeError holds the body.
        transport = ok_transport()
        body = {**BODY, "messages": [{"role": "user", "content": f"{SENTINEL} \ud800"}]}
        with self.assertRaises(ValueError) as cm:
            self.client(transport).chat(body, timeout_s=5)
        self.assertEqual(transport.calls, [])
        self.assertIs(type(cm.exception), ValueError)        # not UnicodeEncodeError
        self.assertNotIn(SENTINEL, str(cm.exception))
        self.assertNotIn(SENTINEL, repr(getattr(cm.exception, "object", "")))
        self.assertIsNone(cm.exception.__context__)
        self.assertIsNone(cm.exception.__cause__)

    def test_timeout_must_be_a_positive_number(self):
        # Review r3: None would mean no timeout at all.
        for bad in (None, 0, -1, float("inf"), float("nan"), True, "5"):
            with self.subTest(timeout_s=bad):
                transport = ok_transport()
                with self.assertRaises(ValueError):
                    self.client(transport).chat(BODY, timeout_s=bad)
                self.assertEqual(transport.calls, [])

    def test_base_url_is_used_for_the_request(self):
        transport = ok_transport()
        self.client(transport, base_url="https://example.test/v9").chat(BODY, timeout_s=5)
        self.assertEqual(transport.calls[0][0], "https://example.test/v9/chat/completions")

    def test_reply_is_frozen_and_repr_hides_the_message(self):
        secret = "SECRET_ANSWER_TEXT"
        body = json.dumps({"choices": [{"message": {"role": "assistant", "content": secret}}],
                           "usage": GOOD_REPLY["usage"]}).encode()
        reply = self.client(FakeTransport((200, {}, body))).chat(BODY, timeout_s=5)
        self.assertEqual(reply.message["content"], secret)
        self.assertNotIn(secret, repr(reply))
        self.assertTrue(dataclasses.is_dataclass(reply))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            reply.status = 500

    def test_fields_that_name_a_person_are_refused_before_sending(self):
        for field in ("user", "store", "metadata", "safety_identifier"):
            with self.subTest(field):
                transport = ok_transport()
                body = {**BODY, field: "FIELD_VALUE_SENTINEL"}
                with self.assertRaises(ValueError) as cm:
                    self.client(transport).chat(body, timeout_s=5)
                self.assertEqual(transport.calls, [])
                for text in ("FIELD_VALUE_SENTINEL", SENTINEL, KEY):
                    self.assertNotIn(text, str(cm.exception))

    def test_non_2xx_is_an_http_error_that_was_sent(self):
        for status in (400, 401, 429, 500, 503, 302):
            with self.subTest(status):
                transport = FakeTransport((status, {}, f"{REPLY_SENTINEL} {SENTINEL}".encode()))
                exc = self.call_error(transport)
                self.assertEqual(exc.code, f"META_HTTP_{status}")
                self.assertIs(exc.sent, True)
                self.assertIsNone(exc.retry_after)
                self.assertEqual(len(transport.calls), 1)
                self.assertCallClean(exc)

    def test_retry_after_is_read_when_numeric(self):
        exc = self.call_error(FakeTransport((429, {"Retry-After": "3"}, b"slow down")))
        self.assertEqual(exc.code, "META_HTTP_429")
        self.assertEqual(exc.retry_after, 3.0)
        exc = self.call_error(FakeTransport((429, {"Retry-After": "soon"}, b"slow down")))
        self.assertIsNone(exc.retry_after)

    def test_timeouts(self):
        for raised in (TimeoutError(), socket.timeout(),
                       urllib.error.URLError(TimeoutError()),
                       TimeoutError(f"{KEY} {SENTINEL} Bearer {KEY}")):
            with self.subTest(repr(raised)):
                exc = self.call_error(FakeTransport(raised))
                self.assertEqual(exc.code, "META_TIMEOUT")
                self.assertIs(exc.sent, True)
                self.assertCallClean(exc)

    def test_network_failures(self):
        for raised in (ConnectionResetError(), OSError(), urllib.error.URLError("refused"),
                       OSError(f"{KEY} {SENTINEL} Bearer {KEY}"),
                       urllib.error.URLError(f"{KEY} {SENTINEL}")):
            with self.subTest(repr(raised)):
                exc = self.call_error(FakeTransport(raised))
                self.assertEqual(exc.code, "META_NETWORK")
                self.assertIs(exc.sent, True)
                self.assertCallClean(exc)

    def bad_replies(self):
        usage = GOOD_REPLY["usage"]
        good_choice = GOOD_REPLY["choices"]
        replies = {
            "invalid json": b"{not json " + REPLY_SENTINEL.encode(),
            "not utf-8": b"\xff\xfe\xfd",
            "not an object": b"[1, 2]",
            "string": b'"x"',
            "no choices": json.dumps({"usage": usage}).encode(),
            "empty choices": json.dumps({"choices": [], "usage": usage}).encode(),
            "message not a dict": json.dumps(
                {"choices": [{"message": REPLY_SENTINEL}], "usage": usage}).encode(),
            "no message": json.dumps({"choices": [{}], "usage": usage}).encode(),
            "no usage": json.dumps({"choices": good_choice}).encode(),
        }
        for field in ("prompt_tokens", "completion_tokens"):
            other = "completion_tokens" if field == "prompt_tokens" else "prompt_tokens"
            for label, value in (("missing", None), ("negative", -1), ("bool", True),
                                 ("string", "10")):
                bad = {other: 5}
                if label != "missing":
                    bad[field] = value
                replies[f"{field} {label}"] = json.dumps(
                    {"choices": good_choice, "usage": bad}).encode()
        return replies

    def test_malformed_2xx_replies_are_bad_replies_that_were_sent(self):
        for label, body in self.bad_replies().items():
            with self.subTest(label):
                exc = self.call_error(FakeTransport((200, {}, body)))
                self.assertEqual(exc.code, "META_BAD_REPLY")
                self.assertIs(exc.sent, True)
                self.assertCallClean(exc)

    def test_every_error_keeps_secrets_out_and_the_context_suppressed(self):
        cases = [FakeTransport((500, {}, REPLY_SENTINEL.encode())),
                 FakeTransport(TimeoutError(KEY)), FakeTransport(OSError(SENTINEL)),
                 FakeTransport((200, {}, b"not json " + REPLY_SENTINEL.encode()))]
        for transport in cases:
            exc = self.call_error(transport)
            self.assertCallClean(exc)
            self.assertIsInstance(exc.sent, bool)


class LoggingTest(unittest.TestCase):
    def scenarios(self):
        return {
            "http 500": FakeTransport(
                (500, {}, json.dumps({"echo": BODY["messages"][0]["content"],
                                      "note": REPLY_SENTINEL}).encode())),
            "timeout": FakeTransport(TimeoutError(f"{KEY} {SENTINEL}")),
            "network": FakeTransport(OSError(f"Bearer {KEY} {SENTINEL}")),
            "bad reply": FakeTransport((200, {}, b"not json " + SENTINEL.encode())),
        }

    def test_failures_are_logged_without_key_bearer_or_text(self):
        for label, transport in self.scenarios().items():
            with self.subTest(label):
                client = mc.MetaClient(KEY, transport=transport)
                with self.assertLogs(level="DEBUG") as cm:
                    with self.assertRaises(mc.MetaCallError):
                        client.chat(BODY, timeout_s=5)
                self.assertTrue(any(r.levelno >= logging.WARNING for r in cm.records),
                                "no warning logged")
                lines = list(cm.output)
                for record in cm.records:
                    lines.append(record.getMessage())
                    lines.append(str(record.args))
                for line in lines:
                    for secret in (KEY, "Bearer", SENTINEL, REPLY_SENTINEL):
                        self.assertNotIn(secret, line)


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b"ok"):
        self.status = status
        self.headers = headers if headers is not None else {}
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        return self._body if n is None or n < 0 else self._body[:n]


class UrllibTransportTest(unittest.TestCase):
    URL = "https://api.meta.ai/v1/chat/completions"
    HEADERS = {"Authorization": "Bearer " + KEY, "Content-Type": "application/json"}

    def test_posts_with_authorization_as_an_unredirected_header(self):
        seen = []

        def fake(req, timeout=None):
            seen.append((req, timeout))
            return FakeResponse(200, {"X-Test": "1"}, b"payload")
        with mock.patch.object(mc._OPENER, "open", fake):
            result = mc.urllib_transport(self.URL, self.HEADERS, b'{"a":1}', 7.5)
        self.assertEqual(result[0], 200)
        self.assertIsInstance(result[1], dict)
        self.assertEqual(result[1]["X-Test"], "1")
        self.assertEqual(result[2], b"payload")
        (req, timeout), = seen
        self.assertEqual(timeout, 7.5)
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.full_url, self.URL)
        self.assertEqual(req.data, b'{"a":1}')
        self.assertEqual(req.get_header("Authorization"), self.HEADERS["Authorization"])
        self.assertNotIn("Authorization", req.headers)
        self.assertEqual(req.unredirected_hdrs.get("Authorization"),
                         self.HEADERS["Authorization"])

    def test_http_errors_come_back_as_a_result_not_an_exception(self):
        def fake(req, timeout=None):
            raise urllib.error.HTTPError(self.URL, 503, "x", {"Retry-After": "2"},
                                         io.BytesIO(b"err"))
        with mock.patch.object(mc._OPENER, "open", fake):
            status, headers, body = mc.urllib_transport(self.URL, self.HEADERS, b"{}", 5)
        self.assertEqual(status, 503)
        self.assertEqual(headers["Retry-After"], "2")
        self.assertEqual(body, b"err")

    def test_redirects_are_never_followed(self):
        self.assertTrue(any(isinstance(h, mc._NoRedirect) for h in mc._OPENER.handlers))
        req = urllib.request.Request(self.URL, data=b"{}", method="POST")
        self.assertIsNone(mc._NoRedirect().redirect_request(
            req, io.BytesIO(), 302, "Found", {}, "https://elsewhere.example/"))


class CostTest(unittest.TestCase):
    def test_prices(self):
        self.assertAlmostEqual(
            mc.cost_usd({"prompt_tokens": 1_000_000, "completion_tokens": 0}), 1.25, places=9)
        self.assertAlmostEqual(
            mc.cost_usd({"prompt_tokens": 0, "completion_tokens": 1_000_000}), 4.25, places=9)
        self.assertAlmostEqual(
            mc.cost_usd({"prompt_tokens": 12_000, "completion_tokens": 3_000}), 0.02775,
            places=9)

    def test_extra_keys_are_ignored(self):
        usage = {"prompt_tokens": 12_000, "completion_tokens": 3_000, "total_tokens": 15_000,
                 "completion_tokens_details": {"reasoning_tokens": 2_500}}
        self.assertAlmostEqual(mc.cost_usd(usage), 0.02775, places=9)

    def test_bad_usage_is_refused(self):
        cases = [{}, {"prompt_tokens": 1}, {"completion_tokens": 1},
                 {"prompt_tokens": -1, "completion_tokens": 1},
                 {"prompt_tokens": 1, "completion_tokens": -1},
                 {"prompt_tokens": True, "completion_tokens": 1},
                 {"prompt_tokens": 1, "completion_tokens": False},
                 {"prompt_tokens": "5", "completion_tokens": 1},
                 {"prompt_tokens": 1, "completion_tokens": "5"},
                 {"prompt_tokens": 1.5, "completion_tokens": 1},
                 {"prompt_tokens": 1, "completion_tokens": None}]
        for usage in cases:
            with self.subTest(usage=usage):
                with self.assertRaises(ValueError):
                    mc.cost_usd(usage)


def usage(prompt, completion=0):
    return {"prompt_tokens": prompt, "completion_tokens": completion}


class AskBase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="metabudget-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.now = [la(2026, 10, 21, 12, 0, 0)]

    def clock(self):
        return self.now[0]

    def init(self):
        mc.AskLedger.init(self.dir, clock=self.clock)

    def open(self, **kw):
        kw.setdefault("clock", self.clock)
        return mc.AskLedger.open(self.dir, **kw)

    def ready(self, **kw):
        self.init()
        return self.open(**kw)

    def rows(self):
        con = sqlite3.connect(self.dir / "ask.sqlite3")
        try:
            return con.execute("SELECT * FROM entries ORDER BY seq").fetchall()
        finally:
            con.close()

    def row_count(self):
        return len(self.rows())

    def committed(self, ledger):
        return ledger.budget.status()["day"]["committed_usd"]

    def assertRefused(self, ledger, turn_id, code):
        with self.assertRaises(mc.MetaBudgetRefused) as cm:
            ledger.reserve_turn(turn_id)
        self.assertEqual(cm.exception.code, code)


class AskLedgerOpenTest(AskBase):
    def test_init_creates_the_ledger_and_pin(self):
        self.init()
        self.assertTrue((self.dir / "ask.sqlite3").is_file())
        self.assertTrue((self.dir / "ask.pin").is_file())

    def test_open_binds_a_jev_budget_to_those_files(self):
        ledger = self.ready()
        self.assertIsInstance(ledger.budget, jb.JevBudget)
        self.assertEqual(Path(ledger.budget.ledger), self.dir / "ask.sqlite3")
        self.assertEqual(Path(ledger.budget.pin), self.dir / "ask.pin")

    def test_open_defaults_to_the_ask_caps(self):
        caps = self.ready().budget.status()["caps_usd"]
        self.assertEqual(caps, {"day": 10.0, "month": 60.0, "total": 100.0})

    def test_open_uses_the_meta_code_prefix(self):
        ledger = self.open()                          # no init: recovery
        self.assertEqual(ledger.budget.status()["code"], "META_BUDGET_RECOVERY")


class AvailabilityTest(AskBase):
    def test_available_when_a_turn_would_fit_and_writes_no_row(self):
        ledger = self.ready()
        before = self.row_count()
        self.assertEqual(ledger.availability(), (True, None))
        self.assertEqual(self.row_count(), before)

    def test_halt_file(self):
        ledger = self.ready()
        (self.dir / "HALT").touch()
        self.assertEqual(ledger.availability(), (False, "META_HALT"))

    def test_halt_beats_recovery(self):
        ledger = self.open()                          # no ledger at all
        (self.dir / "HALT").write_text("stop")
        self.assertEqual(ledger.availability(), (False, "META_HALT"))

    def test_missing_ledger_is_recovery(self):
        ledger = self.open()
        self.assertEqual(ledger.availability(), (False, "META_RECOVERY"))
        self.assertRefused(ledger, "t1", "META_RECOVERY")

    def test_missing_pin_is_recovery(self):
        ledger = self.ready()
        (self.dir / "ask.pin").unlink()
        self.assertEqual(ledger.availability(), (False, "META_RECOVERY"))
        self.assertRefused(ledger, "t1", "META_RECOVERY")

    def test_month_cap_beats_total_and_day(self):
        ledger = self.ready(day_cap_usd=0.42, month_cap_usd=0.42, total_cap_usd=0.42)
        ledger.reserve_turn("t1")
        self.assertEqual(ledger.availability(), (False, "META_MONTH_CAP"))
        self.assertRefused(ledger, "t2", "META_MONTH_CAP")

    def test_exactly_one_turn_left_is_available(self):
        ledger = self.ready(day_cap_usd=0.84)
        ledger.reserve_turn("t1")
        self.assertEqual(ledger.availability(), (True, None))
        ledger.reserve_turn("t2")
        self.assertEqual(ledger.availability(), (False, "META_DAY_CAP"))

    def test_an_unreadable_ledger_is_recovery_not_an_exception(self):
        ledger = self.ready()
        with mock.patch.object(ledger.budget, "status", side_effect=sqlite3.OperationalError("x")):
            self.assertEqual(ledger.availability(), (False, "META_RECOVERY"))

    def test_total_cap_beats_day(self):
        ledger = self.ready(day_cap_usd=0.42, total_cap_usd=0.42)
        ledger.reserve_turn("t1")
        self.assertEqual(ledger.availability(), (False, "META_TOTAL_CAP"))
        self.assertRefused(ledger, "t2", "META_TOTAL_CAP")


class ReserveTurnTest(AskBase):
    def test_reservation_is_one_turn_bound(self):
        ledger = self.ready()
        res = ledger.reserve_turn("turn_1")
        self.assertIsInstance(res, jb.Reservation)
        self.assertAlmostEqual(res.max_usd, 0.42, places=9)
        self.assertEqual(res.purpose, "ask")
        self.assertEqual(res.episode, "turn_1")
        self.assertAlmostEqual(self.committed(ledger), 0.42, places=9)

    def test_halt_refuses_and_writes_no_row(self):
        ledger = self.ready()
        (self.dir / "HALT").touch()
        before = self.row_count()
        self.assertRefused(ledger, "t1", "META_HALT")
        self.assertEqual(self.row_count(), before)

    def test_halt_created_while_reserving_cancels_the_reservation(self):
        # Review r4 item 2: HALT appearing while reserve waits on the lock.
        ledger = self.ready()
        real = ledger.budget.reserve

        def halt_then_reserve(**kw):
            (self.dir / "HALT").touch()
            return real(**kw)
        with mock.patch.object(ledger.budget, "reserve", side_effect=halt_then_reserve):
            self.assertRefused(ledger, "t1", "META_HALT")
        self.assertAlmostEqual(self.committed(ledger), 0.0, places=9)
        con = sqlite3.connect(self.dir / "ask.sqlite3")
        try:
            states = [r[0] for r in con.execute("SELECT state FROM entries ORDER BY seq")]
        finally:
            con.close()
        self.assertEqual(states, ["reserved", "settled"])

    def test_turn_ids_must_be_plain_ids(self):
        ledger = self.ready()
        before = self.row_count()
        for bad in ("what do hosts say about mattresses?", "", "a" * 65, "a b", "a/b",
                    "é", "a\n"):
            with self.subTest(turn_id=bad):
                with self.assertRaises(ValueError):
                    ledger.reserve_turn(bad)
        self.assertEqual(self.row_count(), before)

    def test_valid_turn_ids(self):
        ledger = self.ready()
        for good in ("a", "A-b_9", "a" * 64, "0"):
            with self.subTest(turn_id=good):
                self.assertEqual(ledger.reserve_turn(good).episode, good)

    def test_day_cap(self):
        ledger = self.ready(day_cap_usd=0.84)
        ledger.reserve_turn("t1")
        ledger.reserve_turn("t2")
        self.assertEqual(ledger.availability(), (False, "META_DAY_CAP"))
        self.assertRefused(ledger, "t3", "META_DAY_CAP")

    def test_month_cap(self):
        ledger = self.ready(day_cap_usd=10.0, month_cap_usd=0.84)
        ledger.reserve_turn("t1")
        ledger.reserve_turn("t2")
        self.assertEqual(ledger.availability(), (False, "META_MONTH_CAP"))
        self.assertRefused(ledger, "t3", "META_MONTH_CAP")

    def test_total_cap_and_settling_frees_room(self):
        ledger = self.ready(total_cap_usd=1.0)
        first = ledger.reserve_turn("t1")
        second = ledger.reserve_turn("t2")
        self.assertEqual(ledger.availability(), (False, "META_TOTAL_CAP"))
        self.assertRefused(ledger, "t3", "META_TOTAL_CAP")
        ledger.settle(first, [usage(10, 10)])
        ledger.settle(second, [usage(10, 10)])
        self.assertEqual(ledger.availability(), (True, None))
        ledger.reserve_turn("t3")

    def test_open_reservations_of_an_earlier_month_count_later(self):
        ledger = self.ready(total_cap_usd=1.0)
        ledger.reserve_turn("t1")
        ledger.reserve_turn("t2")
        self.now[0] = la(2026, 11, 1, 0, 0, 1)
        ledger = self.open(total_cap_usd=1.0)
        self.assertEqual(ledger.availability(), (False, "META_TOTAL_CAP"))
        self.assertRefused(ledger, "t3", "META_TOTAL_CAP")


class SettleTest(AskBase):
    def test_settles_with_the_sum_of_every_usage(self):
        ledger = self.ready()
        res = ledger.reserve_turn("t1")
        got = ledger.settle(res, [usage(10_000, 2_000), usage(100_000, 20_000),
                                  usage(3_000, 500)])
        self.assertIsNone(got)
        # 0.021 + 0.21 + 0.005875; the last alone or the first alone would differ
        self.assertAlmostEqual(self.committed(ledger), 0.236875, places=9)

    def test_over_the_bound_is_reported_and_closes_month_then_lifetime(self):
        ledger = self.ready()
        res = ledger.reserve_turn("t1")
        self.assertEqual(ledger.settle(res, [usage(400_000)]), "META_BOUND_EXCEEDED")
        self.assertAlmostEqual(self.committed(ledger), 0.50, places=9)
        self.assertEqual(self.open().availability(), (False, "META_MONTH_CAP"))
        self.now[0] = la(2026, 11, 1, 0, 0, 1)
        self.assertEqual(self.open().availability(), (False, "META_TOTAL_CAP"))

    def test_a_sum_over_the_bound_across_usages_is_reported(self):
        ledger = self.ready()
        res = ledger.reserve_turn("t1")
        got = ledger.settle(res, [usage(150_000), usage(150_000), usage(150_000)])
        self.assertEqual(got, "META_BOUND_EXCEEDED")

    def test_mark_unknown_keeps_the_maximum(self):
        ledger = self.ready()
        ledger.mark_unknown(ledger.reserve_turn("t1"))
        self.assertAlmostEqual(self.committed(ledger), 0.42, places=9)
        self.assertEqual(ledger.budget.status()["day"]["unknown"], 1)

    def test_settle_zero(self):
        ledger = self.ready()
        ledger.settle_zero(ledger.reserve_turn("t1"))
        self.assertAlmostEqual(self.committed(ledger), 0.0, places=9)

    def test_settle_needs_at_least_one_usage(self):
        # Review r3: an empty list would record sent calls as free;
        # settle_zero is the cancelled-before-sending close.
        ledger = self.ready()
        res = ledger.reserve_turn("t1")
        rows = self.row_count()
        with self.assertRaises(ValueError):
            ledger.settle(res, [])
        self.assertEqual(self.row_count(), rows)
        self.assertAlmostEqual(self.committed(ledger), 0.42, places=9)

    def test_a_close_must_name_the_reservation_as_recorded(self):
        # Review r4 item 1: a rebuilt Reservation with another amount or period
        # must not replace the recorded maximum.
        ledger = self.ready()
        res = ledger.reserve_turn("t1")
        forged = [dataclasses.replace(res, max_nusd=0),
                  dataclasses.replace(res, max_nusd=res.max_nusd * 10),
                  dataclasses.replace(res, la_day="2026-10-01"),
                  dataclasses.replace(res, la_month="2026-09")]
        for fake in forged:
            for close in (lambda r: ledger.mark_unknown(r), lambda r: ledger.settle_zero(r),
                          lambda r: ledger.settle(r, [usage(10, 10)])):
                with self.subTest(fake=fake):
                    rows = self.row_count()
                    with self.assertRaises(ValueError):
                        close(fake)
                    self.assertEqual(self.row_count(), rows)
        self.assertAlmostEqual(self.committed(ledger), 0.42, places=9)
        ledger.mark_unknown(res)                     # the real one still closes

    def test_a_closed_reservation_cannot_be_closed_again(self):
        # Review r1 item 3: mark_unknown then settle_zero must not drop $0.42 to $0.
        closers = {"settle": lambda l, r: l.settle(r, [usage(10, 10)]),
                   "mark_unknown": lambda l, r: l.mark_unknown(r),
                   "settle_zero": lambda l, r: l.settle_zero(r)}
        for first in closers:
            for second in closers:
                with self.subTest(first=first, second=second):
                    ledger = self.ready() if first == "settle" and second == "settle" \
                        else self.open()
                    res = ledger.reserve_turn(f"t_{first}_{second}")
                    closers[first](ledger, res)
                    rows, committed = self.row_count(), self.committed(ledger)
                    with self.assertRaises(ValueError):
                        closers[second](ledger, res)
                    self.assertEqual(self.row_count(), rows)
                    self.assertAlmostEqual(self.committed(ledger), committed, places=9)

    def test_a_close_racing_another_close_is_refused_under_the_lock(self):
        # Review r2 item 1: the open check runs inside the writer lock, so a
        # close that lost the race to another handle is refused, not appended.
        ledger = self.ready()
        res = ledger.reserve_turn("t1")
        other = self.open()
        real_lock, fired = jb._pin_lock, []

        @contextlib.contextmanager
        def other_closes_first(pin, timeout_s=None):
            if not fired:
                fired.append(True)
                other.mark_unknown(res)
            with real_lock(pin, timeout_s):
                yield
        with mock.patch.object(jb, "_pin_lock", other_closes_first):
            with self.assertRaises(ValueError):
                ledger.settle_zero(res)
        self.assertEqual(fired, [True])
        con = sqlite3.connect(self.dir / "ask.sqlite3")
        try:
            states = [r[0] for r in con.execute(
                "SELECT state FROM entries WHERE reservation_id=? ORDER BY seq", (res.id,))]
        finally:
            con.close()
        self.assertEqual(states, ["reserved", "unknown"])
        self.assertAlmostEqual(self.committed(ledger), 0.42, places=9)

    def test_recover_closes_the_month_then_the_lifetime(self):
        ledger = self.ready()
        ledger.reserve_turn("t1")
        mc.AskLedger.recover(self.dir, "restored from backup", clock=self.clock)
        self.assertEqual(self.open().availability(), (False, "META_MONTH_CAP"))
        self.now[0] = la(2026, 11, 1, 0, 0, 1)
        self.assertEqual(self.open().availability(), (False, "META_TOTAL_CAP"))


class RefusedMeansNothingSentTest(AskBase):
    def ask(self, ledger, client):
        try:
            ledger.reserve_turn("t1")
            client.chat(BODY, timeout_s=5)
        except mc.MetaBudgetRefused:
            pass

    def test_control_the_pattern_sends_when_not_refused(self):
        transport = ok_transport()
        self.ask(self.ready(), mc.MetaClient(KEY, transport=transport))
        self.assertEqual(len(transport.calls), 1)

    def test_halt_sends_nothing(self):
        ledger = self.ready()
        (self.dir / "HALT").touch()
        transport = ok_transport()
        self.ask(ledger, mc.MetaClient(KEY, transport=transport))
        self.assertEqual(len(transport.calls), 0)

    def test_cap_reached_sends_nothing(self):
        ledger = self.ready(total_cap_usd=0.1)
        transport = ok_transport()
        self.ask(ledger, mc.MetaClient(KEY, transport=transport))
        self.assertEqual(len(transport.calls), 0)

    def test_recovery_sends_nothing(self):
        ledger = self.open()
        transport = ok_transport()
        self.ask(ledger, mc.MetaClient(KEY, transport=transport))
        self.assertEqual(len(transport.calls), 0)


class LedgerRowsTest(AskBase):
    def test_rows_hold_only_ids_codes_and_amounts(self):
        ledger = self.ready()
        res = ledger.reserve_turn("turn_1")
        ledger.settle(res, [usage(10, 5)])
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        con = sqlite3.connect(self.dir / "ask.sqlite3")
        try:
            found = con.execute("SELECT purpose, episode, note FROM entries").fetchall()
        finally:
            con.close()
        for purpose, episode, note in found:
            self.assertIn(purpose, {"ask"})
            self.assertEqual(episode, "turn_1")
            self.assertIsNone(note)
        blob = json.dumps(rows, default=str)
        for text in ("prompt_tokens", "completion_tokens", "usage", SENTINEL, KEY,
                     "Bearer", "hosts say"):
            self.assertNotIn(text, blob)


if __name__ == "__main__":
    unittest.main()


class ReplyModelTest(unittest.TestCase):
    """The reply's model id rides on MetaReply.model (T58 owner item: Ask
    stores the id the API names, not a constant); anything else is None."""

    def chat(self, extra):
        body = json.dumps({**GOOD_REPLY, **extra}).encode()
        return mc.MetaClient(KEY, transport=FakeTransport((200, {}, body))).chat(BODY, timeout_s=5)

    def test_the_model_named_in_the_reply(self):
        self.assertEqual(self.chat({"model": "muse-spark-1.3"}).model, "muse-spark-1.3")

    def test_absent_or_not_a_string_is_none(self):
        for extra in ({}, {"model": None}, {"model": 5}, {"model": ["x"]}):
            with self.subTest(extra=extra):
                self.assertIsNone(self.chat(extra).model)
