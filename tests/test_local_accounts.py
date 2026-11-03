"""PW1: local accounts store, hash format, email rule and subject.

Design: V/docs/plans/2026-10-21-pilot-password-signin.md rev 4, sections
3.1-3.4 and task PW1. Synthetic emails and passwords only.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import unittest
from unittest import mock

from earwitness.app import browser_auth as _auth
from earwitness.app import collaboration as _collab
from earwitness.app import local_accounts as la

ISS = "https://pilot.example"


def control() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    _auth.ensure_auth_schema(con)
    return con


class CountingScrypt:
    """Wraps hashlib.scrypt: counts calls and records their parameters."""

    def __init__(self):
        self.calls = []
        self._real = hashlib.scrypt

    def __call__(self, password, **kw):
        self.calls.append(kw)
        return self._real(password, **kw)


class HashFormatTest(unittest.TestCase):
    def test_round_trip_and_fixed_parameters(self):
        counter = CountingScrypt()
        with mock.patch.object(la.hashlib, "scrypt", counter):
            stored = la.hash_password("correct horse battery")
            self.assertTrue(re.fullmatch(
                r"scrypt-v1\$[A-Za-z0-9_-]{22}\$[A-Za-z0-9_-]{43}", stored), stored)
            self.assertTrue(la.verify(stored, "correct horse battery"))
            self.assertFalse(la.verify(stored, "correct horse batterY"))
        self.assertEqual(len(counter.calls), 3)
        for kw in counter.calls:
            self.assertEqual((kw["n"], kw["r"], kw["p"], kw["dklen"], kw["maxmem"]),
                             (32768, 8, 1, 32, 64 * 1024 * 1024))
            self.assertEqual(len(kw["salt"]), 16)
        # A fresh salt makes every set unique.
        self.assertNotEqual(la.hash_password("same"), la.hash_password("same"))

    def test_malformed_and_other_parameter_records_fail_after_one_dummy_hash(self):
        good = la.hash_password("pw-one-two-three")
        _scheme, salt, key = good.split("$")
        cases = [None, "", "plain", "scrypt-v2$" + salt + "$" + key,
                 "bcrypt$" + salt + "$" + key,
                 "scrypt-v1$" + salt[:-1] + "$" + key,       # short salt
                 "scrypt-v1$" + salt + "$" + key + "A",      # long key
                 "scrypt-v1$" + salt + "==$" + key,          # padded
                 "scrypt-v1$" + salt + "$" + key + "\n",     # trailing newline
                 "scrypt-v1$" + salt.replace(salt[0], "+", 1) + "$" + key,  # not base64url
                 "scrypt-v1:N=16384$" + salt + "$" + key,
                 "SCRYPT-V1$" + salt + "$" + key]
        # A non-canonical last salt character decodes to the same bytes.
        b64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        alt = b64[b64.index(salt[-1]) + 1]  # canonical last chars are multiples of 16
        cases.append("scrypt-v1$" + salt[:-1] + alt + "$" + key)
        for stored in cases:
            with self.subTest(stored=stored):
                counter = CountingScrypt()
                with mock.patch.object(la.hashlib, "scrypt", counter):
                    self.assertFalse(la.verify(stored, "pw-one-two-three"))
                self.assertEqual(len(counter.calls), 1)
                self.assertEqual(counter.calls[0]["n"], 32768)
                self.assertFalse(la.valid_hash(stored))
        self.assertTrue(la.valid_hash(good))

    def test_unknown_account_runs_one_hash(self):
        counter = CountingScrypt()
        with mock.patch.object(la.hashlib, "scrypt", counter):
            self.assertFalse(la.verify(None, "anything"))
        self.assertEqual(len(counter.calls), 1)

    def test_generated_password_shape(self):
        self.assertEqual(len(la.ALPHABET), 55)
        self.assertEqual(len(set(la.ALPHABET)), 55)
        for ch in "IOilo01":
            self.assertNotIn(ch, la.ALPHABET)
        seen = set()
        for _ in range(50):
            pw = la.generate_password()
            self.assertTrue(re.fullmatch(r"([%s]{5}-){3}[%s]{5}" % (la.ALPHABET, la.ALPHABET), pw), pw)
            seen.add(pw)
        self.assertEqual(len(seen), 50)

    def test_new_password_bounds(self):
        problem = la.new_password_problem
        email = "judge@example.com"
        self.assertIsNone(problem("twelve chars", current="old password 1", email_key=email))
        self.assertIsNotNone(problem("eleven char", current="x", email_key=email))
        self.assertIsNotNone(problem("x" * 129, current="y", email_key=email))
        self.assertIsNone(problem("x" * 128, current="y", email_key=email))
        self.assertIsNotNone(problem("\U0001F600" * 129, current="y", email_key=email))
        self.assertIsNone(problem("\U0001F600" * 128, current="y", email_key=email))  # 512 bytes
        # A lone surrogate (valid JSON, not UTF-8) is refused, never raised.
        self.assertIsNotNone(problem("password\ud800word", current="y", email_key=email))
        self.assertIsNone(problem("\U0001F600" * 12, current="y", email_key=email))
        self.assertIsNotNone(problem("same password!", current="same password!", email_key=email))
        self.assertIsNotNone(problem("Judge@Example.com", current="y", email_key=email))
        self.assertIsNotNone(problem(None, current="y", email_key=email))


class EmailRuleTest(unittest.TestCase):
    def test_rule_table(self):
        refused = ["", "   ", "a:b@example.com", "judge @example.com", "ju\tdge@example.com",
                   "judge\x01@example.com", "judge\x7f@example.com", "a@b@example.com",
                   "judgeexample.com", "@example.com", "judge@", "jüdge@example.com",
                   "judge@exämple.com", "x" * 250 + "@ex.com", "judge@example.com ",
                   None, 42]
        for raw in refused:
            with self.subTest(raw=raw):
                with self.assertRaises(la.EmailError):
                    la.canonical(raw)
        self.assertEqual(la.canonical("  Judge@Example.COM \t"),
                         ("judge@example.com", "Judge@Example.COM"))
        self.assertEqual(la.canonical("x" * 242 + "@example.com")[0], "x" * 242 + "@example.com")

    def test_key_matches_invitation_identity_key(self):
        for raw in ("Judge@Example.COM", "judge@example.com", "J.U.D.G.E+tag@Sub.Example.org"):
            key, display = la.canonical(raw)
            self.assertEqual(key, _collab.identity_key(display))
            self.assertEqual(key, _collab.identity_key(raw))

    def test_lookup_key_for_refused_values(self):
        self.assertEqual(la.lookup_key("  A:B@Example.com "), "a:b@example.com")
        self.assertEqual(la.lookup_key("Judge@Example.com"), "judge@example.com")
        self.assertEqual(la.lookup_key(None), "")


class SubjectTest(unittest.TestCase):
    def test_fixed_vector(self):
        self.assertEqual(la.subject(ISS, "judge@example.com"),
                         "acct-b5ab9c7a24b751cfae45c044a51d5335")
        self.assertEqual(la.principal(ISS, "judge@example.com"),
                         "oidc:https://pilot.example:acct-b5ab9c7a24b751cfae45c044a51d5335")

    def test_equivalent_spellings_share_a_subject(self):
        want = la.principal(ISS, "judge@example.com")
        for raw in ("Judge@Example.com", "  judge@example.com  ", "JUDGE@EXAMPLE.COM\t"):
            self.assertEqual(la.principal(ISS, raw), want)
        self.assertNotEqual(la.principal("https://other.example", "judge@example.com"), want)
        self.assertNotIn(":", la.subject(ISS, "judge@example.com"))

    def test_local_issuer_is_the_public_origin(self):
        self.assertEqual(la.local_issuer("https://Pilot.Example/"), "https://pilot.example")
        self.assertEqual(la.local_issuer("https://pilot.example:8443/app"), "https://pilot.example:8443")
        self.assertEqual(la.local_issuer("http://127.0.0.1:8771"), "http://127.0.0.1:8771")
        for bad in ("", "pilot.example", "ftp://pilot.example", "https://user@pilot.example",
                    "https://pilot.example?x=1"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    la.local_issuer(bad)


class StoreTest(unittest.TestCase):
    def test_table_is_part_of_every_control_store(self):
        con = control()
        cols = [r["name"] for r in con.execute("PRAGMA table_info(local_accounts)")]
        self.assertEqual(cols, ["issuer", "subject", "email", "password_hash",
                                "password_set_at", "created_at"])
        self.assertIn("local_accounts_email", [r["name"] for r in con.execute(
            "PRAGMA index_list(local_accounts)")])

    def test_duplicate_insert_fails_and_never_replaces(self):
        con = control()
        first = la.insert_account(con, ISS, "Judge@Example.com", la.hash_password("first pw x"), 1.0)
        self.assertEqual(first["principal"], la.principal(ISS, "judge@example.com"))
        for raw in ("judge@example.com", " JUDGE@example.com "):
            with self.subTest(raw=raw):
                with self.assertRaises(la.DuplicateAccount):
                    la.insert_account(con, ISS, raw, la.hash_password("second pw"), 2.0)
        rows = con.execute("SELECT * FROM local_accounts").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], "Judge@Example.com")
        self.assertTrue(la.verify(rows[0]["password_hash"], "first pw x"))
        self.assertEqual(rows[0]["created_at"], 1.0)
        # The unique email index spans issuers too.
        with self.assertRaises(la.DuplicateAccount):
            la.insert_account(con, "https://other.example", "judge@example.com", None, 3.0)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM local_accounts").fetchone()[0], 1)

    def test_find_by_key_and_principal(self):
        con = control()
        la.insert_account(con, ISS, "Judge@Example.com", None, 1.0)
        row = la.find_by_key(con, ISS, "judge@example.com")
        self.assertIsNotNone(row)
        self.assertIsNone(row["password_hash"])
        self.assertIsNone(la.find_by_key(con, "https://other.example", "judge@example.com"))
        self.assertEqual(la.find_by_principal(con, ISS, la.principal(ISS, "judge@example.com"))["email"],
                         "Judge@Example.com")
        self.assertIsNone(la.find_by_principal(con, ISS, "oidc:https://other.example:acct-x"))


class OpenSessionCoreTest(unittest.TestCase):
    def test_issuer_is_an_argument(self):
        con = control()
        out = _auth.open_issuer_session(
            con, ISS, {"subject": "acct-1", "email": "Judge@Example.com", "email_verified": True},
            {"ts": 100.0, "return_to": "/app/"}, session_ttl_s=60)
        row = con.execute("SELECT * FROM browser_sessions").fetchone()
        self.assertEqual((row["issuer"], row["subject"], row["email_verified"], row["expires_at"]),
                         (ISS, "acct-1", 1, 160.0))
        self.assertEqual(out["issuer"], ISS)
        session = _auth.lookup_session(con, out["session_id"], now=120.0)
        self.assertEqual(_auth.session_principal(session), f"oidc:{ISS}:acct-1")


if __name__ == "__main__":
    unittest.main()
