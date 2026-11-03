"""P01 acquisition regressions (stdlib unittest, controlled fixtures only).

Covers feed/episode identity, conditional GET semantics, bounded `.part`
download with hashing and atomic publication, DAI snapshot separation,
and truncated-JSON rejection. Never touches real feeds.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from earwitness.pipeline import acquisition as acq

REPO = Path(__file__).resolve().parent.parent
FIXTURE_ROOT = REPO / ".muse-product-p01p04" / "acq-fixtures"


class FakeResponse:
    def __init__(self, payload: bytes, status=200, headers=None):
        self.payload = payload
        self.status_code = status
        self.headers = headers or {}

    def iter_content(self, chunk_size=1 << 20):
        for i in range(0, len(self.payload), chunk_size):
            yield self.payload[i:i + chunk_size]


def tmpdir(testcase, name: str) -> Path:
    path = FIXTURE_ROOT / name
    path.mkdir(parents=True, exist_ok=True)
    testcase.addCleanup(lambda: [p.unlink(missing_ok=True) for p in path.iterdir()])
    return path


class IdentityTest(unittest.TestCase):
    def test_guid_stable_across_feed_reorder(self):
        feed = "https://example.com/feed"
        first = acq.episode_identity_key(feed, {"id": "guid-9", "title": "ep"})
        reordered = acq.episode_identity_key(feed, {"title": "ep", "guid": "guid-9"})
        self.assertEqual(first["basis"], "guid")
        self.assertEqual(first["key"], reordered["key"])

    def test_provisional_key_without_guid_and_no_query_guessing(self):
        feed = "https://example.com/feed"
        no_guid = {"title": "ep",
                   "enclosures": [{"href": "https://cdn.example.com/e.mp3?x=1",
                                   "type": "audio/mpeg"}],
                   "links": []}
        other_query = {"title": "ep",
                       "enclosures": [{"href": "https://cdn.example.com/e.mp3?x=2",
                                       "type": "audio/mpeg"}],
                       "links": []}
        a = acq.episode_identity_key(feed, dict(no_guid))
        b = acq.episode_identity_key(feed, dict(other_query))
        self.assertEqual(a["basis"], "provisional")
        self.assertTrue(a.get("provisional"))
        # Query parameters are never stripped by guesswork: distinct locators.
        self.assertNotEqual(a["key"], b["key"])

    def test_guidless_enclosureless_entries_sharing_link_stay_distinct(self):
        # F3: without GUID or enclosure, the provisional key is feed + href
        # + entry ordinal; distinct entries never collapse onto one key.
        feed = "https://example.com/feed"
        site = "https://example.com/episodes"
        e1 = {"title": "ep one", "links": [{"href": site}]}
        e2 = {"title": "ep two", "links": [{"href": site}]}
        k1 = acq.episode_identity_key(feed, e1, 0)
        k2 = acq.episode_identity_key(feed, e2, 1)
        self.assertEqual(k1["basis"], "provisional")
        self.assertTrue(k1.get("provisional"))
        self.assertNotEqual(k1["key"], k2["key"])
        self.assertEqual(k1["key"],
                         acq.episode_identity_key(feed, e1, 0)["key"])
        # Refusing to guess beats collapsing: the fallback needs an ordinal.
        with self.assertRaises(acq.AcquisitionError):
            acq.episode_identity_key(feed, e1)

    def test_unsafe_enclosure_rejected(self):
        with self.assertRaises(acq.UnsafeURL):
            acq.validate_enclosure_url("file:///etc/passwd")
        with self.assertRaises(acq.UnsafeURL):
            acq.validate_enclosure_url("ftp://example.com/e.mp3")


class ConditionalGetTest(unittest.TestCase):
    def test_feed_304_saves_metadata_but_never_proves_media_unchanged(self):
        self.assertTrue(acq.is_feed_not_modified(304))
        self.assertFalse(acq.is_feed_not_modified(200))
        self.assertFalse(acq.feed_304_implies_media_unchanged())
        headers = acq.conditional_headers({"etag": '"abc"', "last_modified": "Tue"})
        self.assertEqual(headers, {"If-None-Match": '"abc"',
                                   "If-Modified-Since": "Tue"})
        self.assertEqual(acq.conditional_headers(None), {})


class BoundedDownloadTest(unittest.TestCase):
    def test_hash_atomic_publish_and_no_part_leftovers(self):
        dest = tmpdir(self, "ok")
        body = b"ID3" + b"A" * 65536 + b"audio-bytes"
        seen = {}

        def fetcher(url, headers, timeout):
            seen["headers"] = headers
            return FakeResponse(body, 200, {"Content-Length": str(len(body))})

        obs = acq.download_media("https://cdn.example.com/e.mp3", dest_dir=dest,
                                 name="e.mp3", fetcher=fetcher,
                                 max_bytes=1 << 20)
        import hashlib
        self.assertEqual(obs["status"], "fetched")
        self.assertEqual(obs["sha256"], hashlib.sha256(body).hexdigest())
        self.assertTrue((dest / "e.mp3").is_file())
        self.assertFalse((dest / "e.mp3.part").exists())
        self.assertIn("User-Agent", seen["headers"])

    def test_byte_cap_enforced_without_final_file(self):
        dest = tmpdir(self, "cap")
        body = b"B" * (3 << 20)

        def fetcher(url, headers, timeout):
            return FakeResponse(body, 200, {"Content-Length": str(len(body))})

        with self.assertRaises(acq.OverBudget):
            acq.download_media("https://cdn.example.com/big.mp3", dest_dir=dest,
                               name="big.mp3", fetcher=fetcher, max_bytes=1 << 20)
        self.assertFalse((dest / "big.mp3").exists())
        self.assertFalse((dest / "big.mp3.part").exists())

    def test_declared_length_truncation_is_failure_not_complete(self):
        dest = tmpdir(self, "trunc")

        def fetcher(url, headers, timeout):
            return FakeResponse(b"short", 200, {"Content-Length": "100"})

        with self.assertRaises(acq.TruncatedDownload):
            acq.download_media("https://cdn.example.com/e.mp3", dest_dir=dest,
                               name="e.mp3", fetcher=fetcher, max_bytes=1 << 20)
        self.assertFalse((dest / "e.mp3").exists())

    def test_dai_same_url_different_bytes_new_snapshot(self):
        dest = tmpdir(self, "dai")
        # Audio magic bytes + declared length: bodies must look like audio now.
        one, two = b"ID3ad-version-one", b"ID3ad-version-two!"
        first = acq.download_media(
            "https://cdn.example.com/e.mp3", dest_dir=dest, name="a.mp3",
            fetcher=lambda u, h, t: FakeResponse(
                one, 200, {"Content-Length": str(len(one))}),
            max_bytes=1 << 20)
        second = acq.download_media(
            "https://cdn.example.com/e.mp3", dest_dir=dest, name="b.mp3",
            fetcher=lambda u, h, t: FakeResponse(
                two, 200, {"Content-Length": str(len(two))}),
            max_bytes=1 << 20)
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertTrue(acq.is_new_snapshot(first["sha256"], second["sha256"]))
        self.assertFalse(acq.is_new_snapshot(first["sha256"], first["sha256"]))

    def test_pre_stream_fetcher_failure_is_typed_and_leaves_no_part(self):
        # F2: a fetcher raising before streaming must surface the module's
        # acquisition error type and must not leave a stale .part behind.
        dest = tmpdir(self, "fetchfail")
        (dest / "e.mp3.part").write_bytes(b"stale-bytes")

        def fetcher(url, headers, timeout):
            raise RuntimeError("boom: connection reset")

        with self.assertRaises(acq.AcquisitionError):
            acq.download_media("https://cdn.example.com/e.mp3", dest_dir=dest,
                               name="e.mp3", fetcher=fetcher, max_bytes=1 << 20)
        self.assertFalse((dest / "e.mp3.part").exists())
        self.assertFalse((dest / "e.mp3").exists())

    def test_http_error_cleans_stale_part(self):
        # R3/M2a: a non-2xx status raises the typed error and removes a
        # planted stale .part, like the fetcher-failure path does.
        dest = tmpdir(self, "httperr")
        (dest / "e.mp3.part").write_bytes(b"stale-bytes")

        def fetcher(url, headers, timeout):
            return FakeResponse(b"server-side", 500, {})

        with self.assertRaises(acq.AcquisitionError):
            acq.download_media("https://cdn.example.com/e.mp3", dest_dir=dest,
                               name="e.mp3", fetcher=fetcher,
                               max_bytes=1 << 20)
        self.assertFalse((dest / "e.mp3.part").exists())
        self.assertFalse((dest / "e.mp3").exists())

    def test_declared_over_cap_cleans_stale_part(self):
        # R3/M2b: a declared Content-Length over max_bytes raises
        # OverBudget and removes a planted stale .part.
        dest = tmpdir(self, "overcap")
        (dest / "big.mp3.part").write_bytes(b"stale-bytes")

        def fetcher(url, headers, timeout):
            return FakeResponse(b"TINY", 200,
                                {"Content-Length": str(3 << 20)})

        with self.assertRaises(acq.OverBudget):
            acq.download_media("https://cdn.example.com/big.mp3", dest_dir=dest,
                               name="big.mp3", fetcher=fetcher,
                               max_bytes=1 << 20)
        self.assertFalse((dest / "big.mp3.part").exists())
        self.assertFalse((dest / "big.mp3").exists())

    def test_media_304_is_an_error_without_media_claim(self):
        # Media is always refetched (DAI): a 304 is never a success.
        dest = tmpdir(self, "notmod")
        with self.assertRaises(acq.MediaNotModified):
            acq.download_media(
                "https://cdn.example.com/e.mp3", dest_dir=dest, name="e.mp3",
                fetcher=lambda u, h, t: FakeResponse(b"", 304, {}),
                max_bytes=1 << 20,
                validators={"etag": '"v1"'})
        self.assertFalse((dest / "e.mp3").exists())


class TruncatedJsonTest(unittest.TestCase):
    def test_truncated_json_never_skipped_as_complete(self):
        dest = tmpdir(self, "json")
        bad = dest / "feed.json"
        bad.write_text('{"entries": [{"id": "a", "title": "half')
        with self.assertRaises(acq.TruncatedDocument):
            acq.load_json_document(bad)
        good = dest / "ok.json"
        good.write_text(json.dumps({"entries": []}))
        self.assertEqual(acq.load_json_document(good), {"entries": []})


if __name__ == "__main__":
    unittest.main()
