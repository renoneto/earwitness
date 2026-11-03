"""L11: private tenant-scoped object storage (interface + filesystem backend)."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
import threading
import unicodedata
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit, urlencode

from earwitness.app import objstore
from earwitness.app.objstore import (FilesystemObjectStore, InvalidKey,
                                     InvalidTTL, LinkRefused, ObjectNotFound,
                                     SecretMissing)

ENV = "EW_TEST_OBJSTORE_SECRET"
SECRET = "s3cret-" + "x" * 40


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve() / "store"
        self.outside = Path(self._tmp.name).resolve() / "outside"
        self.outside.mkdir()
        self.clock = Clock()
        self.store = self.make()

    def make(self, secret=SECRET, **kw):
        env = {ENV: secret} if secret is not None else {}
        return FilesystemObjectStore(self.root, secret_env=ENV, environ=env,
                                     clock=self.clock, **kw)

    def tenant_dir(self, tenant):
        return self.root / "tenants" / tenant


def retarget(url, **changes):
    """Rewrite one part of a signed URL: path=, exp=, sig= (None drops it)."""
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query))
    path = changes.pop("path", parts.path)
    for k, v in changes.items():
        if v is None:
            q.pop(k, None)
        else:
            q[k] = v
    return urlunsplit(("", "", path, urlencode(q), ""))


class RoundTripTests(StoreCase):
    def test_put_get_list_delete(self):
        self.store.put("acme", ("audio", "ep1.mp3"), b"abc", "audio/mpeg")
        obj = self.store.get("acme", ("audio", "ep1.mp3"))
        self.assertEqual(obj.data, b"abc")
        self.assertEqual(obj.content_type, "audio/mpeg")
        self.assertEqual(self.store.list("acme", ()), [("audio", "ep1.mp3")])
        self.assertEqual(self.store.list("acme", ("audio",)),
                         [("audio", "ep1.mp3")])
        self.assertEqual(self.store.list("acme", ("video",)), [])
        self.assertTrue(self.store.delete("acme", ("audio", "ep1.mp3")))
        self.assertFalse(self.store.delete("acme", ("audio", "ep1.mp3")))
        with self.assertRaises(ObjectNotFound):
            self.store.get("acme", ("audio", "ep1.mp3"))

    def test_single_string_key_is_one_segment(self):
        self.store.put("acme", "a.txt", b"1", "text/plain")
        self.assertEqual(self.store.get("acme", ("a.txt",)).data, b"1")

    def test_overwrite_in_same_tenant(self):
        self.store.put("acme", "k", b"1", "text/plain")
        self.store.put("acme", "k", b"2", "text/plain")
        self.assertEqual(self.store.get("acme", "k").data, b"2")

    def test_objects_live_under_the_tenant_directory(self):
        self.store.put("acme", ("a", "b"), b"payload", "text/plain")
        raw = (self.tenant_dir("acme") / "a" / "b").read_bytes()
        self.assertTrue(raw.endswith(b"payload"))
        self.assertFalse((self.root / "meta").exists())

    def test_rejects_bad_payload_and_content_type(self):
        for data in ("text", None, 5):
            with self.assertRaises(TypeError):
                self.store.put("acme", "k", data, "text/plain")
        for ct in ("", "text", "text/plain\r\nX: y", "a/b/c", None):
            with self.assertRaises(ValueError):
                self.store.put("acme", "k", b"1", ct)


class TenantIsolationTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.store.put("alpha", ("docs", "plan"), b"alpha-secret", "text/plain")

    def test_other_tenant_cannot_read(self):
        with self.assertRaises(ObjectNotFound):
            self.store.get("beta", ("docs", "plan"))

    def test_other_tenant_cannot_list(self):
        self.assertEqual(self.store.list("beta", ()), [])
        self.assertEqual(self.store.list("beta", ("docs",)), [])
        self.assertEqual(self.store.list("alpha", ()), [("docs", "plan")])

    def test_other_tenant_cannot_overwrite(self):
        self.store.put("beta", ("docs", "plan"), b"beta-data", "text/plain")
        self.assertEqual(self.store.get("alpha", ("docs", "plan")).data,
                         b"alpha-secret")
        self.assertEqual(self.store.get("beta", ("docs", "plan")).data,
                         b"beta-data")

    def test_other_tenant_cannot_delete(self):
        self.assertFalse(self.store.delete("beta", ("docs", "plan")))
        self.assertEqual(self.store.get("alpha", ("docs", "plan")).data,
                         b"alpha-secret")

    def test_tenant_id_cannot_reach_another_tenant(self):
        for tenant in ("../alpha", "beta/../alpha", "alpha/docs", "..", ".",
                       "", "/alpha", "a\\b"):
            with self.assertRaises(InvalidKey, msg=repr(tenant)):
                self.store.get(tenant, ("docs", "plan"))
            with self.assertRaises(InvalidKey, msg=repr(tenant)):
                self.store.list(tenant, ())
            with self.assertRaises(InvalidKey, msg=repr(tenant)):
                self.store.put(tenant, "k", b"x", "text/plain")
            with self.assertRaises(InvalidKey, msg=repr(tenant)):
                self.store.delete(tenant, "k")

    def test_tenant_symlink_to_other_tenant_is_not_followed(self):
        os.symlink(self.tenant_dir("alpha"), self.tenant_dir("beta"))
        with self.assertRaises((InvalidKey, ObjectNotFound)):
            self.store.get("beta", ("docs", "plan"))
        self.assertEqual(self.store.list("beta", ()), [])


class KeyValidationTests(StoreCase):
    BAD_SEGMENTS = ["", ".", "..", "/", "/etc", "/etc/passwd", "a/b", "a/",
                    "\\", "a\\b", "..\\x", "a\x00b", "\x00", "a\nb", "a\rb",
                    "a\tb", "a\x1fb", "a\x7fb", "x" * 300]

    def test_bad_segments_refused_everywhere(self):
        for seg in self.BAD_SEGMENTS:
            with self.subTest(seg=seg):
                for key in (seg, ("ok", seg), (seg, "ok")):
                    with self.assertRaises(InvalidKey):
                        self.store.put("acme", key, b"x", "text/plain")
                    with self.assertRaises(InvalidKey):
                        self.store.get("acme", key)
                    with self.assertRaises(InvalidKey):
                        self.store.delete("acme", key)
                    with self.assertRaises(InvalidKey):
                        self.store.signed_url("acme", key, 60)
                with self.assertRaises(InvalidKey):
                    self.store.list("acme", (seg,))

    def test_empty_key_and_wrong_types_refused(self):
        for key in ((), [], None, 5, b"x", ("a", None), ("a", 5)):
            with self.assertRaises(InvalidKey, msg=repr(key)):
                self.store.get("acme", key)

    def test_dotdot_never_touches_a_file_outside_the_tenant(self):
        (self.outside / "victim").write_text("keep")
        rel = os.path.relpath(self.outside / "victim",
                              self.tenant_dir("acme"))
        parts = tuple(rel.split(os.sep))
        self.assertIn("..", parts)
        with self.assertRaises(InvalidKey):
            self.store.put("acme", parts, b"pwn", "text/plain")
        with self.assertRaises(InvalidKey):
            self.store.get("acme", parts)
        with self.assertRaises(InvalidKey):
            self.store.delete("acme", parts)
        self.assertEqual((self.outside / "victim").read_text(), "keep")

    def test_absolute_segment_cannot_replace_the_root(self):
        target = self.outside / "abs.txt"
        with self.assertRaises(InvalidKey):
            self.store.put("acme", (str(target),), b"pwn", "text/plain")
        self.assertFalse(target.exists())


class SymlinkTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.store.put("acme", "real", b"mine", "text/plain")
        self.target = self.outside / "target.txt"
        self.target.write_text("outside")

    def test_file_symlink_is_not_followed(self):
        os.symlink(self.target, self.tenant_dir("acme") / "link")
        with self.assertRaises((InvalidKey, ObjectNotFound)):
            self.store.get("acme", "link")
        self.assertNotIn(("link",), self.store.list("acme", ()))
        self.assertEqual(self.store.list("acme", ()), [("real",)])

    def test_put_through_file_symlink_does_not_write_outside(self):
        os.symlink(self.target, self.tenant_dir("acme") / "link")
        try:
            self.store.put("acme", "link", b"pwn", "text/plain")
        except objstore.ObjectStoreError:
            pass
        self.assertEqual(self.target.read_text(), "outside")

    def test_delete_through_symlink_does_not_delete_outside(self):
        os.symlink(self.target, self.tenant_dir("acme") / "link")
        try:
            self.store.delete("acme", "link")
        except objstore.ObjectStoreError:
            pass
        self.assertEqual(self.target.read_text(), "outside")

    def test_directory_symlink_is_not_followed(self):
        os.symlink(self.outside, self.tenant_dir("acme") / "dir")
        with self.assertRaises((InvalidKey, ObjectNotFound)):
            self.store.get("acme", ("dir", "target.txt"))
        with self.assertRaises(objstore.ObjectStoreError):
            self.store.put("acme", ("dir", "new.txt"), b"pwn", "text/plain")
        self.assertFalse((self.outside / "new.txt").exists())
        self.assertEqual(self.store.list("acme", ()), [("real",)])
        self.assertEqual(self.store.list("acme", ("dir",)), [])
        self.assertTrue(self.target.exists())

    def test_symlinked_parent_of_put_is_not_followed(self):
        os.symlink(self.outside, self.tenant_dir("acme") / "deep")
        with self.assertRaises(objstore.ObjectStoreError):
            self.store.put("acme", ("deep", "x", "y"), b"pwn", "text/plain")
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()),
                         ["target.txt"])

    def test_signed_link_for_symlink_does_not_serve_outside_data(self):
        os.symlink(self.target, self.tenant_dir("acme") / "link")
        url = self.store.signed_url("acme", "link", 60)
        ref = self.store.verify_signed(url, "acme")
        with self.assertRaises((InvalidKey, ObjectNotFound)):
            self.store.get(ref.tenant, ref.key)


class SignedLinkTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.store.put("alpha", ("a", "b.txt"), b"data", "text/plain")
        self.url = self.store.signed_url("alpha", ("a", "b.txt"), 300)

    def test_valid_link_verifies_and_resolves(self):
        ref = self.store.verify_signed(self.url, "alpha")
        self.assertEqual((ref.tenant, ref.key), ("alpha", ("a", "b.txt")))
        self.assertEqual(self.store.get(ref.tenant, ref.key).data, b"data")
        self.assertEqual(ref.expires_at, int(self.clock.now) + 300)

    def test_link_has_the_documented_shape_and_no_secret(self):
        self.assertRegex(
            self.url,
            r"^/objects/alpha/a/b\.txt\?exp=%d&sig=[0-9a-f]{64}$"
            % (int(self.clock.now) + 300))
        self.assertNotIn(SECRET, self.url)

    def test_unsigned_links_refused(self):
        for url in (retarget(self.url, sig=None), retarget(self.url, exp=None),
                    retarget(self.url, sig=None, exp=None),
                    retarget(self.url, sig=""),
                    urlsplit(self.url).path, "", "garbage", "sig=abc"):
            with self.assertRaises(LinkRefused, msg=url):
                self.store.verify_signed(url, "alpha")

    def test_wrong_tenant_refused(self):
        with self.assertRaises(LinkRefused):
            self.store.verify_signed(self.url, "beta")
        with self.assertRaises(LinkRefused):
            self.store.verify_signed(self.url, "")

    def test_wrong_tenant_link_for_other_tenants_object(self):
        self.store.put("beta", ("a", "b.txt"), b"beta", "text/plain")
        beta_url = self.store.signed_url("beta", ("a", "b.txt"), 60)
        with self.assertRaises(LinkRefused):
            self.store.verify_signed(beta_url, "alpha")

    def test_expired_link_refused(self):
        self.clock.now += 301
        with self.assertRaises(LinkRefused) as cm:
            self.store.verify_signed(self.url, "alpha")
        self.assertEqual(cm.exception.reason, "expired")

    def test_link_valid_until_expiry_then_refused(self):
        self.clock.now += 299
        self.store.verify_signed(self.url, "alpha")
        self.clock.now += 1
        with self.assertRaises(LinkRefused):
            self.store.verify_signed(self.url, "alpha")

    def test_tampered_key_refused(self):
        path = urlsplit(self.url).path
        for new in (path.replace("b.txt", "c.txt"),
                    path.replace("/a/", "/z/"),
                    path + "/extra"):
            with self.assertRaises(LinkRefused, msg=new):
                self.store.verify_signed(retarget(self.url, path=new), "alpha")

    def test_tampered_tenant_refused(self):
        self.store.put("beta", ("a", "b.txt"), b"beta", "text/plain")
        path = urlsplit(self.url).path.replace("/alpha/", "/beta/")
        forged = retarget(self.url, path=path)
        for expected in ("alpha", "beta"):
            with self.assertRaises(LinkRefused):
                self.store.verify_signed(forged, expected)

    def test_tampered_expiry_refused(self):
        exp = int(dict(parse_qsl(urlsplit(self.url).query))["exp"])
        for new in (exp + 1, exp + 3600, exp - 1, "x", ""):
            with self.assertRaises(LinkRefused, msg=str(new)):
                self.store.verify_signed(retarget(self.url, exp=str(new)),
                                         "alpha")

    def test_tampered_signature_refused(self):
        sig = dict(parse_qsl(urlsplit(self.url).query))["sig"]
        flipped = ("0" if sig[0] != "0" else "1") + sig[1:]
        for new in (flipped, sig[:-1], sig + "0", sig.upper() + "!", "0" * 64):
            with self.assertRaises(LinkRefused, msg=new):
                self.store.verify_signed(retarget(self.url, sig=new), "alpha")

    def test_duplicate_query_parameters_refused(self):
        q = dict(parse_qsl(urlsplit(self.url).query))
        # Duplicates carrying the same valid value must still be refused.
        for extra in ("&sig=" + q["sig"], "&exp=" + q["exp"]):
            with self.assertRaises(LinkRefused, msg=extra) as cm:
                self.store.verify_signed(self.url + extra, "alpha")
            self.assertEqual(cm.exception.reason, "malformed")
        self.store.verify_signed(self.url, "alpha")

    def test_encoded_separator_in_link_path_refused(self):
        parts = urlsplit(self.url).path.split("/")
        forged = "/".join(parts[:-2] + [quote("a/b.txt", safe="")])
        with self.assertRaises((LinkRefused, InvalidKey)):
            self.store.verify_signed(retarget(self.url, path=forged), "alpha")

    def test_link_from_another_secret_refused(self):
        other = self.make(secret="another-" + "y" * 40)
        with self.assertRaises(LinkRefused):
            other.verify_signed(self.url, "alpha")

    def test_signature_is_hmac_sha256_over_tenant_key_expiry(self):
        exp = int(self.clock.now) + 60
        msg = json.dumps(["ew-objstore-v1", "alpha", ["a", "b"], exp],
                         separators=(",", ":")).encode()
        want = hmac.new(SECRET.encode(), msg, hashlib.sha256).hexdigest()
        self.assertEqual(self.store._sign("alpha", ("a", "b"), exp), want)
        self.assertIn("sig=" + want,
                      self.store.signed_url("alpha", ("a", "b"), 60))

        # Changing any one input changes the signature.
        def sig(t, k, ttl):
            return dict(parse_qsl(urlsplit(
                self.store.signed_url(t, k, ttl)).query))["sig"]
        base = sig("alpha", ("a", "b"), 60)
        self.assertEqual(len(base), 64)
        self.assertEqual(base, sig("alpha", ("a", "b"), 60))
        self.assertNotEqual(base, sig("beta", ("a", "b"), 60))
        self.assertNotEqual(base, sig("alpha", ("a", "c"), 60))
        self.assertNotEqual(base, sig("alpha", ("a", "b"), 61))
        # Segment boundaries are signed: ("ab",) differs from ("a", "b").
        self.assertNotEqual(sig("alpha", ("ab",), 60),
                            sig("alpha", ("a", "b"), 60))
        self.assertNotEqual(sig("alpha", ("a", "b", "c"), 60),
                            sig("alpha", ("a", "b.c"), 60))

    def test_constant_time_comparison_is_used(self):
        with mock.patch.object(objstore.hmac, "compare_digest",
                               wraps=hmac.compare_digest) as spy:
            self.store.verify_signed(self.url, "alpha")
        self.assertTrue(spy.called)

    def test_ttl_cap_and_bounds(self):
        self.assertEqual(objstore.MAX_TTL_S, 900)
        self.store.signed_url("alpha", "k", 900)
        self.store.signed_url("alpha", "k", 1)
        for ttl in (901, 3600, 0, -5, float("nan"), float("inf"), "60", None,
                    True):
            with self.assertRaises(InvalidTTL, msg=repr(ttl)):
                self.store.signed_url("alpha", "k", ttl)

    def test_link_expiring_beyond_the_cap_is_refused(self):
        # A validly signed but over-cap expiry (e.g. from a buggy issuer).
        exp = int(self.clock.now) + 3600
        sig = self.store._sign("alpha", ("a", "b.txt"), exp)
        url = retarget(self.url, exp=str(exp), sig=sig)
        with self.assertRaises(LinkRefused):
            self.store.verify_signed(url, "alpha")

    def test_absolute_url_is_accepted_by_path_and_query(self):
        absolute = "https://files.example.test" + self.url
        ref = self.store.verify_signed(absolute, "alpha")
        self.assertEqual(ref.key, ("a", "b.txt"))


class SecretTests(StoreCase):
    def test_missing_secret_refused(self):
        for env in ({}, {ENV: ""}, {ENV: "   "}):
            with self.assertRaises(SecretMissing, msg=repr(env)):
                FilesystemObjectStore(self.root, secret_env=ENV, environ=env)

    def test_short_secret_refused(self):
        with self.assertRaises(SecretMissing):
            FilesystemObjectStore(self.root, secret_env=ENV,
                                  environ={ENV: "short"})

    def test_refusal_names_the_variable_not_a_value(self):
        with self.assertRaises(SecretMissing) as cm:
            FilesystemObjectStore(self.root, secret_env=ENV, environ={})
        self.assertIn(ENV, str(cm.exception))

    def test_process_environment_is_the_default_source(self):
        with mock.patch.dict(os.environ, {ENV: SECRET}):
            s = FilesystemObjectStore(self.root, secret_env=ENV,
                                      clock=self.clock)
        url = s.signed_url("acme", "k", 60)
        self.make().verify_signed(url, "acme")  # same secret, same signature
        with mock.patch.dict(os.environ):
            os.environ.pop(ENV, None)
            with self.assertRaises(SecretMissing):
                FilesystemObjectStore(self.root, secret_env=ENV)

    def test_secret_length_is_counted_in_bytes(self):
        FilesystemObjectStore(self.root, secret_env=ENV,
                              environ={ENV: "\u00e9" * 16})  # 32 bytes
        with self.assertRaises(SecretMissing):
            FilesystemObjectStore(self.root, secret_env=ENV,
                                  environ={ENV: "x" * 31})
        with self.assertRaises(SecretMissing):
            FilesystemObjectStore(self.root, secret_env=ENV,
                                  environ={ENV: "\u00e9" * 15})  # 30 bytes

    def test_test_mode_must_be_exactly_true(self):
        for flag in (1, "yes", "true", [1], object()):
            with self.assertRaises(SecretMissing, msg=repr(flag)):
                FilesystemObjectStore(self.root, secret_env=ENV, environ={},
                                      test_mode=flag)
        with self.assertRaises(SecretMissing):
            FilesystemObjectStore(self.root, secret_env=ENV, environ={},
                                  test_mode=False)

    def test_empty_url_prefix_refused(self):
        for prefix in ("", "/", "//", None):
            with self.assertRaises(ValueError, msg=repr(prefix)):
                self.make(url_prefix=prefix)
        self.assertTrue(self.make(url_prefix="/dl").signed_url(
            "acme", "k", 60).startswith("/dl/acme/k?"))

    def test_test_mode_works_without_a_secret(self):
        s = FilesystemObjectStore(self.root, secret_env=ENV, environ={},
                                  test_mode=True, clock=self.clock)
        s.put("acme", "k", b"1", "text/plain")
        url = s.signed_url("acme", "k", 60)
        self.assertEqual(s.verify_signed(url, "acme").key, ("k",))

    def test_test_mode_links_do_not_verify_on_a_configured_store(self):
        t = FilesystemObjectStore(self.root, secret_env=ENV, environ={},
                                  test_mode=True, clock=self.clock)
        url = t.signed_url("acme", "k", 60)
        with self.assertRaises(LinkRefused):
            self.make().verify_signed(url, "acme")

    def test_secret_is_not_in_repr(self):
        self.assertNotIn(SECRET, repr(self.store))
        self.assertNotIn(SECRET, str(self.store))
        self.assertNotIn(SECRET.encode().hex(), repr(self.store))


class TenantAndKeyCharsetTests(StoreCase):
    """HIGH-1: tenants and segments must not collide on case-insensitive or
    Unicode-normalising filesystems (APFS). Chosen rule: tenants match the
    workspace-id pattern; segments are lowercase ASCII only."""

    BAD_TENANTS = ["ACME", "AcMe", "Acme", "acmE", "café",
                   unicodedata.normalize("NFD", "café"), "a_b", "-a",
                   "a.b", "a" * 65, "acme\n", "acme ", " acme", "ａcme",
                   "acme​", "a b", "", "ı"]
    BAD_SEGMENTS = ["Doc", "DOC", "dOc", "café",
                    unicodedata.normalize("NFD", "café"), "a b", "a:b",
                    "a*", "a?", "é", "ａ", "doc\n", "x" * 129,
                    "K", "ﬁ"]

    def test_tenant_pattern_matches_workspace_ids(self):
        for tenant in ("a", "acme", "a-b-1", "0", "a" * 64, "9a"):
            self.store.put(tenant, "k", b"1", "text/plain")
            self.assertEqual(self.store.get(tenant, "k").data, b"1")

    def test_bad_tenants_refused_everywhere(self):
        for tenant in self.BAD_TENANTS:
            with self.subTest(tenant=tenant):
                with self.assertRaises(InvalidKey):
                    self.store.put(tenant, "k", b"x", "text/plain")
                with self.assertRaises(InvalidKey):
                    self.store.get(tenant, "k")
                with self.assertRaises(InvalidKey):
                    self.store.delete(tenant, "k")
                with self.assertRaises(InvalidKey):
                    self.store.list(tenant, ())
                with self.assertRaises(InvalidKey):
                    self.store.signed_url(tenant, "k", 60)

    def test_case_variant_tenant_cannot_touch_the_real_tenant(self):
        self.store.put("acme", "doc", b"acme-secret", "text/plain")
        for variant in ("ACME", "AcMe", "Acme"):
            with self.assertRaises(InvalidKey):
                self.store.get(variant, "doc")
            with self.assertRaises(InvalidKey):
                self.store.put(variant, "doc", b"pwned", "text/plain")
            with self.assertRaises(InvalidKey):
                self.store.delete(variant, "doc")
            with self.assertRaises(InvalidKey):
                self.store.list(variant, ())
        self.assertEqual(self.store.get("acme", "doc").data, b"acme-secret")

    def test_unicode_variant_tenant_cannot_touch_the_real_tenant(self):
        nfc = unicodedata.normalize("NFC", "café")
        nfd = unicodedata.normalize("NFD", "café")
        for tenant in (nfc, nfd):
            with self.assertRaises(InvalidKey):
                self.store.put(tenant, "doc", b"x", "text/plain")
            with self.assertRaises(InvalidKey):
                self.store.get(tenant, "doc")

    def test_link_for_a_case_variant_tenant_is_refused(self):
        self.store.put("acme", "doc", b"x", "text/plain")
        good = self.store.signed_url("acme", "doc", 60)
        forged = retarget(good, path=urlsplit(good).path.replace("/acme/",
                                                                 "/ACME/"))
        with self.assertRaises(LinkRefused):
            self.store.verify_signed(forged, "acme")
        with self.assertRaises(LinkRefused):
            self.store.verify_signed(good, "ACME")

    def test_valid_segment_charset(self):
        for seg in ("a", "ep1.mp3", "a_b-c.d", "0", "...", "x" * 128,
                    "a.b.c"):
            self.store.put("acme", ("d", seg), b"1", "text/plain")
            self.assertEqual(self.store.get("acme", ("d", seg)).data, b"1")

    def test_bad_segments_refused_everywhere(self):
        for seg in self.BAD_SEGMENTS:
            with self.subTest(seg=seg):
                for key in (seg, ("ok", seg), (seg, "ok")):
                    with self.assertRaises(InvalidKey):
                        self.store.put("acme", key, b"x", "text/plain")
                    with self.assertRaises(InvalidKey):
                        self.store.get("acme", key)
                    with self.assertRaises(InvalidKey):
                        self.store.delete("acme", key)
                    with self.assertRaises(InvalidKey):
                        self.store.signed_url("acme", key, 60)
                with self.assertRaises(InvalidKey):
                    self.store.list("acme", (seg,))

    def test_case_and_unicode_variant_keys_cannot_alias(self):
        self.store.put("acme", ("docs", "plan"), b"real", "text/plain")
        for key in (("DOCS", "plan"), ("docs", "PLAN"), ("Docs", "Plan"),
                    ("docs", "plán")):
            with self.assertRaises(InvalidKey):
                self.store.get("acme", key)
            with self.assertRaises(InvalidKey):
                self.store.put("acme", key, b"pwned", "text/plain")
            with self.assertRaises(InvalidKey):
                self.store.delete("acme", key)
        self.assertEqual(self.store.get("acme", ("docs", "plan")).data, b"real")
        self.assertEqual(self.store.list("acme", ()), [("docs", "plan")])


class KeySizeCapTests(StoreCase):
    def test_segment_count_cap(self):
        ok = tuple("s%d" % i for i in range(objstore.MAX_KEY_SEGMENTS))
        self.store.put("acme", ok, b"1", "text/plain")
        self.assertEqual(self.store.get("acme", ok).data, b"1")
        self.store.signed_url("acme", ok, 60)
        self.assertEqual(self.store.list("acme", ()), [ok])
        too_many = ok + ("x",)
        for call in (
                lambda: self.store.put("acme", too_many, b"1", "text/plain"),
                lambda: self.store.get("acme", too_many),
                lambda: self.store.delete("acme", too_many),
                lambda: self.store.signed_url("acme", too_many, 60),
                lambda: self.store.list("acme", too_many)):
            with self.assertRaises(InvalidKey):
                call()

    def test_total_key_length_cap(self):
        seg = "a" * 128
        n = objstore.MAX_KEY_BYTES // 129          # fits
        ok = (seg,) * n
        self.store.signed_url("acme", ok, 60)
        too_long = (seg,) * (n + 1)
        self.assertGreater(sum(map(len, too_long)) + len(too_long) - 1,
                           objstore.MAX_KEY_BYTES)
        for call in (
                lambda: self.store.put("acme", too_long, b"1", "text/plain"),
                lambda: self.store.get("acme", too_long),
                lambda: self.store.delete("acme", too_long),
                lambda: self.store.signed_url("acme", too_long, 60),
                lambda: self.store.list("acme", too_long)):
            with self.assertRaises(InvalidKey):
                call()

    def test_a_link_for_an_oversized_key_is_refused(self):
        too_many = tuple("s%d" % i for i in range(objstore.MAX_KEY_SEGMENTS + 1))
        exp = int(self.clock.now) + 60
        sig = self.store._sign("acme", too_many, exp)
        url = "/objects/acme/%s?exp=%d&sig=%s" % ("/".join(too_many), exp, sig)
        with self.assertRaises(LinkRefused):
            self.store.verify_signed(url, "acme")


class AtomicObjectTests(StoreCase):
    """MED-1 / LOW-1: bytes and content type are published together."""

    def failing_replace(self, fail_from):
        real, calls = os.replace, []

        def replace(src, dst):
            calls.append(dst)
            if len(calls) >= fail_from:
                raise OSError(5, "injected failure", str(dst))
            return real(src, dst)
        return mock.patch.object(objstore.os, "replace", replace)

    def test_failed_write_never_pairs_new_bytes_with_old_type(self):
        for fail_from in (1, 2, 3):
            with self.subTest(fail_from=fail_from):
                self.store.put("acme", "doc", b"old", "text/plain")
                with self.failing_replace(fail_from):
                    try:
                        self.store.put("acme", "doc", b"<script>",
                                       "image/png")
                    except objstore.ObjectStoreError:
                        pass
                obj = self.store.get("acme", "doc")
                self.assertIn((obj.data, obj.content_type),
                              [(b"old", "text/plain"),
                               (b"<script>", "image/png")])
                self.assertEqual(list((self.root / "tmp").iterdir()), [])

    def test_failed_first_write_leaves_nothing_readable(self):
        with self.failing_replace(1):
            with self.assertRaises(objstore.ObjectStoreError) as cm:
                self.store.put("acme", "doc", b"x", "text/plain")
        self.assertNotIn(str(self.root), str(cm.exception))
        with self.assertRaises(ObjectNotFound):
            self.store.get("acme", "doc")
        self.assertEqual(self.store.list("acme", ()), [])
        self.assertEqual(list((self.root / "tmp").iterdir()), [])

    def test_overlapping_puts_never_mix_bytes_and_type(self):
        a, b = b"A" * 100_000, b"B" * 100_000
        pairs = {a: "text/plain", b: "image/png"}
        stop, bad = threading.Event(), []

        def writer(data):
            for _ in range(40):
                self.store.put("acme", "doc", data, pairs[data])

        def reader():
            while not stop.is_set():
                try:
                    obj = self.store.get("acme", "doc")
                except ObjectNotFound:
                    continue
                if pairs.get(obj.data) != obj.content_type:
                    bad.append((obj.data[:1], obj.content_type))

        self.store.put("acme", "doc", a, pairs[a])
        r = threading.Thread(target=reader)
        ws = [threading.Thread(target=writer, args=(d,)) for d in (a, b, a, b)]
        r.start()
        for w in ws:
            w.start()
        for w in ws:
            w.join()
        stop.set()
        r.join()
        self.assertEqual(bad, [])
        self.assertEqual(list((self.root / "tmp").iterdir()), [])

    def test_concurrent_put_and_delete_leave_no_orphans(self):
        def putter():
            for _ in range(60):
                self.store.put("acme", ("d", "doc"), b"x", "text/plain")

        def deleter():
            for _ in range(60):
                self.store.delete("acme", ("d", "doc"))

        ts = [threading.Thread(target=f) for f in (putter, deleter, putter,
                                                   deleter)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        files = sorted(p.relative_to(self.tenant_dir("acme")).parts
                       for p in self.tenant_dir("acme").rglob("*")
                       if p.is_file())
        self.assertEqual(files, sorted(self.store.list("acme", ())))
        self.assertLessEqual(len(files), 1)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         ["tenants", "tmp"])
        self.assertEqual(list((self.root / "tmp").iterdir()), [])
        self.store.put("acme", ("d", "doc"), b"y", "text/plain")
        self.assertTrue(self.store.delete("acme", ("d", "doc")))
        self.assertEqual(self.store.list("acme", ()), [])

    def test_corrupt_object_is_an_error_not_data(self):
        path = self.tenant_dir("acme") / "doc"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not an object")
        with self.assertRaises(objstore.ObjectStoreError) as cm:
            self.store.get("acme", "doc")
        self.assertNotIsInstance(cm.exception, ObjectNotFound)
        self.assertNotIn(str(self.root), str(cm.exception))


class ListHygieneTests(StoreCase):
    """LOW-4: list returns object keys only."""

    def test_list_ignores_files_that_are_not_objects(self):
        self.store.put("acme", ("a", "real"), b"1", "text/plain")
        base = self.tenant_dir("acme")
        (base / ".DS_Store").write_bytes(b"junk")
        (base / "notes.txt").write_bytes(b"no header")
        (base / "Bad Name").write_bytes(b"EWOBJ1 text/plain\nx")
        (base / "a" / "UPPER").write_bytes(b"EWOBJ1 text/plain\nx")
        (base / "Odd Dir").mkdir()
        (base / "Odd Dir" / "inner").write_bytes(b"EWOBJ1 text/plain\nx")
        (base / "empty-dir").mkdir()
        self.assertEqual(self.store.list("acme", ()), [("a", "real")])
        self.assertEqual(self.store.list("acme", ("a",)), [("a", "real")])

    def test_list_stays_within_the_depth_cap(self):
        deep = self.tenant_dir("acme").joinpath(
            *["d%d" % i for i in range(objstore.MAX_KEY_SEGMENTS + 1)])
        deep.mkdir(parents=True)
        (deep / "f").write_bytes(b"EWOBJ1 text/plain\nx")
        self.assertEqual(self.store.list("acme", ()), [])


class ErrorHygieneTests(StoreCase):
    """LOW-2: OS errors never escape raw or leak the storage root."""

    def boom(self, *a, **k):
        raise OSError(13, "Permission denied", str(self.root / "secret-path"))

    def check(self, call):
        with self.assertRaises(objstore.ObjectStoreError) as cm:
            call()
        self.assertNotIn(str(self.root), str(cm.exception))
        self.assertNotIn(str(self.root), repr(cm.exception))
        self.assertIsNone(cm.exception.__cause__)
        self.assertTrue(cm.exception.__suppress_context__
                        or cm.exception.__context__ is None)

    def test_delete_wraps_os_errors(self):
        self.store.put("acme", "doc", b"1", "text/plain")
        with mock.patch.object(objstore.os, "unlink", self.boom):
            self.check(lambda: self.store.delete("acme", "doc"))
        self.assertEqual(self.store.get("acme", "doc").data, b"1")

    def test_list_wraps_os_errors(self):
        self.store.put("acme", "doc", b"1", "text/plain")
        with mock.patch.object(objstore.os, "scandir", self.boom):
            self.check(lambda: self.store.list("acme", ()))

    def test_get_and_put_wrap_os_errors(self):
        self.store.put("acme", "doc", b"1", "text/plain")
        with mock.patch.object(objstore.os, "open", self.boom):
            self.check(lambda: self.store.get("acme", "doc"))
        with mock.patch.object(objstore.tempfile, "mkstemp", self.boom):
            self.check(lambda: self.store.put("acme", "doc2", b"1",
                                              "text/plain"))

    def test_delete_of_a_directory_is_not_an_object(self):
        self.store.put("acme", ("d", "doc"), b"1", "text/plain")
        self.assertFalse(self.store.delete("acme", "d"))
        self.assertEqual(self.store.get("acme", ("d", "doc")).data, b"1")


class OpenNoFollowTests(StoreCase):
    def test_final_open_refuses_a_symlink_even_if_the_path_check_is_bypassed(
            self):
        target = self.outside / "t.txt"
        target.write_bytes(b"EWOBJ1 text/plain\nouter")
        link = self.tenant_dir("acme") / "link"
        link.parent.mkdir(parents=True)
        os.symlink(target, link)
        with mock.patch.object(self.store, "_path", return_value=link):
            with self.assertRaises(objstore.ObjectStoreError):
                self.store.get("acme", "link")


if __name__ == "__main__":
    unittest.main()
