"""Tracking-redirect fallback helpers in earwitness/pipeline/acquisition.py.

Offline: the transport tests use a fake session whose `.get` raises.
"""
from __future__ import annotations

import importlib.util
import socket
import tempfile
import unittest

from earwitness.pipeline import acquisition as acq

HAVE_REQUESTS = importlib.util.find_spec("requests") is not None

MGLN = "https://mgln.ai/e/1727/track/pscrb.fm/rss/p/traffic.megaphone.fm/WELII5504718424.mp3"


class EmbeddedMediaUrlTest(unittest.TestCase):
    def check(self, url, expected):
        got = acq.embedded_media_url(url)
        self.assertEqual(got, expected, url)
        if got is not None:
            self.assertEqual(acq.validate_enclosure_url(got), got)
            self.assertNotEqual(got, url)

    def test_megaphone_through_magellan_and_pscrb(self):
        self.check(MGLN, "https://traffic.megaphone.fm/WELII5504718424.mp3")

    def test_podtrac_redirect_mp3_is_never_a_host(self):
        self.check("https://dts.podtrac.com/redirect.mp3/traffic.libsyn.com/show/ep1.mp3",
                   "https://traffic.libsyn.com/show/ep1.mp3")

    def test_chtbl_keeps_query(self):
        self.check("https://chtbl.com/track/ABC123/traffic.megaphone.fm/X1.mp3?updated=17",
                   "https://traffic.megaphone.fm/X1.mp3?updated=17")

    def test_fragment_dropped(self):
        self.check("https://chtbl.com/track/A/traffic.megaphone.fm/X1.mp3#t=5",
                   "https://traffic.megaphone.fm/X1.mp3")

    def test_op3_explicit_schemes(self):
        self.check("https://op3.dev/e/https://traffic.megaphone.fm/X2.mp3",
                   "https://traffic.megaphone.fm/X2.mp3")
        self.check("https://op3.dev/e/http://traffic.megaphone.fm/X3.mp3",
                   "http://traffic.megaphone.fm/X3.mp3")
        self.check("https://op3.dev/e/https:/traffic.megaphone.fm/X4.mp3",
                   "https://traffic.megaphone.fm/X4.mp3")

    def test_other_schemes_refused(self):
        for tail in ("ftp://traffic.megaphone.fm/X.mp3", "file:///etc/passwd.mp3",
                     "javascript:traffic.megaphone.fm/x.mp3",
                     "gopher://traffic.megaphone.fm/x.mp3"):
            self.check("https://op3.dev/e/" + tail, None)

    def test_crafted_non_media_host(self):
        self.check("https://mgln.ai/e/1/track/evil.example/x.mp3", None)

    def test_innermost_host_decides(self):
        self.check("https://mgln.ai/e/1/traffic.megaphone.fm/evil.example/x.mp3", None)

    def test_lookalike_inner_hosts(self):
        self.check("https://mgln.ai/e/1/megaphone.fm.evil.example/x.mp3", None)
        self.check("https://mgln.ai/e/1/evilmegaphone.fm/x.mp3", None)

    def test_lookalike_outer_hosts(self):
        self.check("https://mgln.ai.evil.example/e/1/traffic.megaphone.fm/x.mp3", None)
        self.check("https://evilmgln.ai/e/1/traffic.megaphone.fm/x.mp3", None)

    def test_ip_literal_inner_host(self):
        self.check("https://mgln.ai/e/1/127.0.0.1/x.mp3", None)
        self.check("https://mgln.ai/e/1/169.254.169.254/x.mp3", None)

    def test_single_label_inner_host(self):
        self.check("https://mgln.ai/e/1/track/x.mp3", None)

    def test_port_or_userinfo_inner_host(self):
        self.check("https://mgln.ai/e/1/traffic.megaphone.fm:8080/x.mp3", None)
        self.check("https://mgln.ai/e/1/user@traffic.megaphone.fm/x.mp3", None)

    def test_unclean_outer_authority(self):
        for outer in ("evil.example\\@mgln.ai", "evil.example@mgln.ai", "user:pw@mgln.ai",
                      "mgln.ai\\@evil.example"):
            self.check(f"https://{outer}/e/traffic.megaphone.fm/x.mp3", None)
        self.check("https://MGLN.ai:443/e/traffic.megaphone.fm/x.mp3",
                   "https://traffic.megaphone.fm/x.mp3")

    def test_overlong_host_refused(self):
        host = ".".join(["a" * 60] * 4) + ".megaphone.fm"
        self.assertGreater(len(host), 253)
        self.check(f"https://mgln.ai/e/1/{host}/x.mp3", None)

    def test_trailing_newline_is_not_a_dns_name(self):
        self.assertFalse(acq._is_valid_dns_name("traffic\n.megaphone.fm"))
        self.assertFalse(acq._is_valid_dns_name("traffic.megaphone.fm\n"))
        self.assertTrue(acq._is_valid_dns_name("traffic.megaphone.fm"))

    def test_no_audio_suffix(self):
        self.check("https://mgln.ai/e/1/traffic.megaphone.fm/page.html", None)
        self.check("https://mgln.ai/e/1/traffic.megaphone.fm/", None)

    def test_non_tracking_outer_host(self):
        self.check("https://traffic.megaphone.fm/a.mp3", None)
        self.check("https://example.com/e/traffic.megaphone.fm/x.mp3", None)

    def test_outer_scheme_must_be_http(self):
        self.check("ftp://mgln.ai/e/traffic.megaphone.fm/x.mp3", None)

    def test_garbage_input(self):
        self.check("", None)
        self.check("not a url", None)
        self.assertIsNone(acq.embedded_media_url(None))
        self.assertIsNone(acq.embedded_media_url("https://[bad/e/traffic.megaphone.fm/x.mp3"))

    def test_backslash_or_non_ascii_outer_authority_refused(self):
        tail = "/e/1727/track/pscrb.fm/rss/p/traffic.megaphone.fm/WELII5504718424.mp3"
        self.check("https://evil.com\\.mgln.ai" + tail, None)
        self.check("https://a\\.mgln.ai" + tail, None)
        self.check("https://b\u00fccher.mgln.ai" + tail, None)

    def test_subdomain_and_case_insensitive_host(self):
        self.check("https://mgln.ai/e/1/media.blubrry.com/show/x.mp3",
                   "https://media.blubrry.com/show/x.mp3")
        self.check("https://mgln.ai/e/1/TRAFFIC.MEGAPHONE.FM/X.MP3",
                   "https://traffic.megaphone.fm/X.MP3")


@unittest.skipUnless(HAVE_REQUESTS, "requests not installed: transport tests run under .venv")
class RedirectHostClassificationTest(unittest.TestCase):
    ORIG = MGLN

    def setUp(self):
        import requests
        import urllib3.exceptions as u3
        self.requests, self.u3 = requests, u3

    def dns_error(self, host):
        gai = socket.gaierror(8, "nodename nor servname provided, or not known")
        try:
            return self.u3.NameResolutionError(host, None, gai)
        except Exception:  # constructor shape differs across urllib3 versions
            return gai

    def conn_error(self, failing_url, inner):
        """A requests ConnectionError the way `requests` builds it for a redirect hop."""
        host = failing_url.split("/")[2]
        reason = inner if inner is not None else OSError("boom")
        wrapped = self.u3.MaxRetryError(pool=None, url=failing_url, reason=reason)
        req = self.requests.Request("GET", failing_url).prepare()
        return self.requests.exceptions.ConnectionError(wrapped, request=req), host

    def session(self, exc):
        class FakeSession:
            def get(self, *a, **kw):
                raise exc
        return FakeSession()

    def fetch(self, exc, url=None):
        return acq.requests_fetcher(self.session(exc))(url or self.ORIG, {}, 5)

    def test_single_label_redirect_dns_failure(self):
        exc, _ = self.conn_error("https://track/pscrb.fm/rss/p/traffic.megaphone.fm/W.mp3",
                                 self.dns_error("track"))
        with self.assertRaises(acq.BadRedirectHost) as ctx:
            self.fetch(exc)
        self.assertEqual(ctx.exception.host, "track")
        self.assertEqual(ctx.exception.reason, "single-label")
        self.assertIs(ctx.exception.__cause__, exc)
        self.assertTrue(ctx.exception.url.startswith("https://track/"))

    def test_gaierror_chain_via_cause(self):
        exc, _ = self.conn_error("https://track/x.mp3", None)
        exc.__cause__ = socket.gaierror(8, "unknown")
        with self.assertRaises(acq.BadRedirectHost) as ctx:
            self.fetch(exc)
        self.assertEqual(ctx.exception.host, "track")

    def test_download_media_does_not_rewrap(self):
        exc, _ = self.conn_error("https://track/pscrb.fm/rss/p/traffic.megaphone.fm/W.mp3",
                                 self.dns_error("track"))
        with tempfile.TemporaryDirectory() as dest:
            with self.assertRaises(acq.BadRedirectHost) as ctx:
                acq.download_media(self.ORIG, dest_dir=dest, name="e.mp3",
                                   fetcher=acq.requests_fetcher(self.session(exc)),
                                   max_bytes=1 << 20)
        self.assertEqual(ctx.exception.host, "track")

    def test_first_hop_dns_failure_is_not_a_redirect(self):
        orig = "https://gone.example/x.mp3"
        exc, _ = self.conn_error(orig, self.dns_error("gone.example"))
        with self.assertRaises(self.requests.exceptions.ConnectionError) as ctx:
            self.fetch(exc, orig)
        self.assertIs(ctx.exception, exc)
        with tempfile.TemporaryDirectory() as dest:
            with self.assertRaises(acq.AcquisitionError) as ctx2:
                acq.download_media(orig, dest_dir=dest, name="e.mp3",
                                   fetcher=acq.requests_fetcher(self.session(exc)),
                                   max_bytes=1 << 20)
        self.assertNotIsInstance(ctx2.exception, acq.BadRedirectHost)

    def test_dotted_redirect_host_unresolvable(self):
        exc, _ = self.conn_error("https://cdn.gone.example/x.mp3", self.dns_error("cdn.gone.example"))
        with self.assertRaises(acq.BadRedirectHost) as ctx:
            self.fetch(exc)
        self.assertEqual(ctx.exception.host, "cdn.gone.example")
        self.assertEqual(ctx.exception.reason, "unresolvable")

    def test_single_label_redirect_non_dns_error(self):
        """A single-label hop that failed for a non-DNS reason is a plain error."""
        exc, _ = self.conn_error("https://track/x.mp3", None)
        with self.assertRaises(self.requests.exceptions.ConnectionError) as ctx:
            self.fetch(exc)
        self.assertIs(ctx.exception, exc)
        self.assertIsNone(acq.redirect_host_failure(self.ORIG, exc))

    def test_redirect_failure_ignored_for_unclean_original_authority(self):
        orig = "https://evil.example\\@mgln.ai/e/traffic.megaphone.fm/x.mp3"
        exc, _ = self.conn_error("http://deadhost/x", self.dns_error("deadhost"))
        self.assertIsNone(acq.redirect_host_failure(orig, exc))
        self.assertEqual(acq.redirect_host_failure(self.ORIG, exc),
                         ("single-label", "deadhost", "http://deadhost/x"))

    def test_redirect_failure_ignored_for_backslash_authority(self):
        orig = ("https://evil.com\\.mgln.ai/e/1727/track/pscrb.fm/rss/p/"
                "traffic.megaphone.fm/WELII5504718424.mp3")
        exc, _ = self.conn_error("http://deadhost/x", self.dns_error("deadhost"))
        self.assertIsNone(acq.redirect_host_failure(orig, exc))
        self.assertEqual(acq.redirect_host_failure(self.ORIG, exc),
                         ("single-label", "deadhost", "http://deadhost/x"))

    def test_dotted_redirect_host_non_dns_errors_pass_through(self):
        exc, _ = self.conn_error("https://cdn.other.example/x.mp3", None)
        with self.assertRaises(self.requests.exceptions.ConnectionError):
            self.fetch(exc)
        req = self.requests.Request("GET", "https://cdn.other.example/x.mp3").prepare()
        timeout = self.requests.exceptions.Timeout("slow", request=req)
        with self.assertRaises(self.requests.exceptions.Timeout):
            self.fetch(timeout)

    def test_connection_error_without_request(self):
        exc = self.requests.exceptions.ConnectionError("no request attached")
        with self.assertRaises(self.requests.exceptions.ConnectionError):
            self.fetch(exc)
        self.assertIsNone(acq.redirect_host_failure(self.ORIG, exc))

    def test_redirect_host_failure_is_pure(self):
        exc, _ = self.conn_error("https://track/x.mp3", self.dns_error("track"))
        self.assertEqual(acq.redirect_host_failure(self.ORIG, exc),
                         ("single-label", "track", "https://track/x.mp3"))


if __name__ == "__main__":
    unittest.main()
