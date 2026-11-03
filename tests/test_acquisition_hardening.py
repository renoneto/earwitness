"""Acquisition hardening regressions (stdlib unittest, no network).

One test group per confirmed defect in `earwitness/pipeline/acquisition.py`:
non-audio bodies, undetectable truncation, snapshot overwrite, shared `.part`
files, thin observations, partial feeds, enclosure suffix matching, media
304, and the missing overall deadline.
"""
from __future__ import annotations

import errno
import hashlib
import io
import os
import random
import shutil
import tempfile
import threading
import time
import unittest
import importlib.util  # noqa: E402
if importlib.util.find_spec("requests") is None:
    # The corpus pipeline runs under .venv (requirements.txt pins requests);
    # the hosted app never imports it.
    raise unittest.SkipTest("requests not installed: corpus pipeline tests run under .venv")
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import requests

from earwitness.pipeline import acquisition as acq

URL = "https://cdn.example.com/e.mp3"
ID3_BODY = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 512


class Resp:
    def __init__(self, payload: bytes, status=200, headers=None, url=None,
                 history=None, chunks=None):
        self.payload = payload
        self.status_code = status
        self.headers = headers if headers is not None else {}
        if url is not None:
            self.url = url
        if history is not None:
            self.history = history
        self._chunks = chunks

    def iter_content(self, chunk_size=1 << 20):
        if self._chunks is not None:
            yield from self._chunks()
            return
        for i in range(0, len(self.payload), chunk_size):
            yield self.payload[i:i + chunk_size]


def ok(body=ID3_BODY, **headers):
    headers.setdefault("Content-Length", str(len(body)))
    return lambda u, h, t: Resp(body, 200, headers)


def make_mp3(seconds=2, noise=False, xing=True) -> bytes:
    import av
    path = Path(tempfile.mkdtemp()) / "t.mp3"
    out = av.open(str(path), "w", options=None if xing else {"write_xing": "0"})
    stream = out.add_stream("mp3", rate=8000)
    stream.layout = "mono"
    frame = av.AudioFrame(format="s16", layout="mono", samples=8000 * seconds)
    size = 2 * 8000 * seconds
    frame.planes[0].update(random.Random(1).randbytes(size) if noise else b"\x00" * size)
    frame.sample_rate = 8000
    for pkt in stream.encode(frame):
        out.mux(pkt)
    for pkt in stream.encode(None):
        out.mux(pkt)
    out.close()
    data = path.read_bytes()
    shutil.rmtree(path.parent)
    return data


def make_cover_art_mp3(seconds=30, size=200) -> bytes:
    """Xing mp3 behind an ID3v2.3 tag holding a real JPEG (APIC) cover."""
    import av
    from fractions import Fraction
    ctx = av.CodecContext.create("mjpeg", "w")
    ctx.width = ctx.height = size
    ctx.pix_fmt = "yuvj420p"
    ctx.time_base = Fraction(1, 25)
    frame = av.VideoFrame(size, size, "yuvj420p")
    for plane in frame.planes:
        plane.update(random.Random(3).randbytes(plane.buffer_size))
    jpeg = b"".join(bytes(p) for p in ctx.encode(frame) + ctx.encode(None))
    body = b"\x00image/jpeg\x00\x03\x00" + jpeg
    apic = b"APIC" + len(body).to_bytes(4, "big") + b"\x00\x00" + body
    n = len(apic)
    syncsafe = bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])
    return b"ID3\x03\x00\x00" + syncsafe + apic + make_mp3(seconds, noise=True)


def run_bounded(fn, timeout=5.0):
    """Run fn in a daemon thread; fail instead of hanging if it never returns."""
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise AssertionError(f"still running after {timeout}s")
    if "error" in box:
        raise box["error"]
    return box["value"]


def make_vbr_mp3(silence_s=40, noise_s=20) -> bytes:
    """VBR mp3 with no Xing/Info header: ffmpeg estimates its duration from size."""
    import av
    path = Path(tempfile.mkdtemp()) / "t.mp3"
    out = av.open(str(path), "w", options={"write_xing": "0"})
    stream = out.add_stream("mp3", rate=22050)
    stream.layout = "mono"
    stream.codec_context.options = {"flags": "+qscale", "global_quality": "708"}
    for seconds, noisy in ((silence_s, False), (noise_s, True)):
        n = 22050 * seconds
        frame = av.AudioFrame(format="s16", layout="mono", samples=n)
        frame.sample_rate = 22050
        frame.planes[0].update(random.Random(2).randbytes(2 * n) if noisy else b"\x00" * (2 * n))
        for pkt in stream.encode(frame):
            out.mux(pkt)
    for pkt in stream.encode(None):
        out.mux(pkt)
    out.close()
    data = path.read_bytes()
    shutil.rmtree(path.parent)
    return data


def make_wav() -> bytes:
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(8000)
    w.writeframes(b"\x00\x01" * 8000)
    w.close()
    return buf.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.dest = Path(tempfile.mkdtemp(prefix="acq-hardening-"))
        self.addCleanup(shutil.rmtree, self.dest, True)

    def dl(self, fetcher, name="e.mp3", **kw):
        kw.setdefault("max_bytes", 1 << 20)
        return acq.download_media(URL, dest_dir=self.dest, name=name,
                                  fetcher=fetcher, **kw)

    def assertNothingPublished(self):
        self.assertEqual(list(self.dest.iterdir()), [])


class NonAudioBodyTest(Base):
    def test_html_error_page_with_200_is_rejected(self):
        body = b"<html><body>Not found</body></html>"
        with self.assertRaises(acq.AcquisitionError):
            self.dl(ok(body, **{"Content-Type": "text/html; charset=utf-8"}))
        self.assertNothingPublished()

    def test_zero_byte_body_is_rejected(self):
        with self.assertRaises(acq.AcquisitionError):
            self.dl(lambda u, h, t: Resp(b"", 200, {}))
        self.assertNothingPublished()

    def test_json_and_xml_content_types_are_rejected(self):
        for ctype in ("application/json", "application/xml", "text/plain"):
            with self.subTest(ctype=ctype), self.assertRaises(acq.AcquisitionError):
                self.dl(ok(ID3_BODY, **{"Content-Type": ctype}))
            self.assertNothingPublished()

    def test_octet_stream_and_missing_type_need_magic_bytes(self):
        for headers in ({"Content-Type": "application/octet-stream"}, {}):
            with self.subTest(headers=headers):
                with self.assertRaises(acq.AcquisitionError):
                    self.dl(ok(b"<html>nope</html>" * 8, **headers))
                self.assertNothingPublished()
                self.assertEqual(self.dl(ok(ID3_BODY, **headers))["status"], "fetched")
                (self.dest / "e.mp3").unlink()

    def test_known_audio_signatures_are_accepted(self):
        samples = {
            "id3": ID3_BODY,
            "mpeg": b"\xff\xfb\x90\x00" + b"\x00" * 64,
            "adts": b"\xff\xf1\x50\x80" + b"\x00" * 64,
            "mp4": b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 64,
            "ogg": b"OggS\x00\x02" + b"\x00" * 64,
            "wav": b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 64,
            "flac": b"fLaC\x00\x00\x00\x22" + b"\x00" * 64,
        }
        for label, body in samples.items():
            with self.subTest(label=label):
                obs = self.dl(ok(body, **{"Content-Type": "audio/mpeg"}),
                              name=label)
                self.assertEqual(obs["status"], "fetched")


class TruncationWithoutLengthTest(Base):
    def test_truncated_mp3_without_content_length_is_rejected(self):
        mp3 = make_mp3()
        with self.assertRaises(acq.AcquisitionError):
            self.dl(lambda u, h, t: Resp(mp3[:len(mp3) // 2], 200, {}))
        self.assertNothingPublished()

    def test_undecodable_body_with_audio_magic_is_rejected(self):
        with self.assertRaises(acq.AcquisitionError):
            self.dl(lambda u, h, t: Resp(ID3_BODY, 200, {}))
        self.assertNothingPublished()

    def test_complete_audio_without_content_length_is_published(self):
        mp3 = make_mp3()
        obs = self.dl(lambda u, h, t: Resp(mp3, 200, {}))
        self.assertEqual(obs["status"], "fetched")
        self.assertGreater(acq.probe_audio_duration(obs["path"]), 1.5)

    def test_verify_fn_hook_can_reject_and_default_is_skipped_with_length(self):
        calls = []

        def verify(path):
            calls.append(path)
            raise acq.AcquisitionError("verifier says no")

        with self.assertRaises(acq.AcquisitionError):
            self.dl(ok(), verify_fn=verify)
        self.assertEqual(len(calls), 1)
        self.assertNothingPublished()
        # Declared length matches and no hook: no decode probe, fake payload ok.
        self.assertEqual(self.dl(ok())["status"], "fetched")


class SnapshotOverwriteTest(Base):
    def test_refetch_with_different_bytes_does_not_overwrite_same_name(self):
        first = self.dl(ok(ID3_BODY + b"one"))
        with self.assertRaises(acq.AcquisitionError):
            self.dl(ok(ID3_BODY + b"two"))
        self.assertEqual((self.dest / "e.mp3").read_bytes(), ID3_BODY + b"one")
        self.assertEqual(first["sha256"],
                         hashlib.sha256(ID3_BODY + b"one").hexdigest())
        self.assertEqual([p.name for p in self.dest.iterdir()], ["e.mp3"])

    def test_identical_bytes_are_idempotent(self):
        a = self.dl(ok(ID3_BODY))
        b = self.dl(ok(ID3_BODY))
        self.assertEqual(a["sha256"], b["sha256"])
        self.assertEqual([p.name for p in self.dest.iterdir()], ["e.mp3"])

    def test_content_addressed_keeps_both_snapshots(self):
        a = self.dl(ok(ID3_BODY + b"one"), content_addressed=True)
        b = self.dl(ok(ID3_BODY + b"two"), content_addressed=True)
        self.assertNotEqual(a["path"], b["path"])
        self.assertEqual(Path(a["path"]).name, a["sha256"] + ".mp3")
        self.assertEqual(Path(a["path"]).read_bytes(), ID3_BODY + b"one")
        self.assertEqual(Path(b["path"]).read_bytes(), ID3_BODY + b"two")
        again = self.dl(ok(ID3_BODY + b"one"), content_addressed=True)
        self.assertEqual(again["path"], a["path"])
        self.assertEqual(len(list(self.dest.iterdir())), 2)


class ConcurrentPartTest(Base):
    def test_two_downloads_of_same_name_do_not_share_a_part_file(self):
        sized = {"Content-Length": str(len(ID3_BODY))}

        def inner_fetcher(u, h, t):
            return Resp(ID3_BODY, 200, sized)

        def outer_chunks():
            yield ID3_BODY[:100]
            # A second worker finishes the same name while this one is mid-stream.
            acq.download_media(URL, dest_dir=self.dest, name="e.mp3",
                               fetcher=inner_fetcher, max_bytes=1 << 20)
            yield ID3_BODY[100:]

        outer = Resp(ID3_BODY, 200, sized, chunks=outer_chunks)
        obs = self.dl(lambda u, h, t: outer)
        self.assertEqual(obs["status"], "fetched")
        self.assertEqual([p.name for p in self.dest.iterdir()], ["e.mp3"])
        self.assertEqual((self.dest / "e.mp3").read_bytes(), ID3_BODY)

    def test_publish_oserror_surfaces_as_acquisition_error(self):
        boom = OSError(errno.ENOSPC, "No space left on device")
        with mock.patch.object(acq.os, "replace", side_effect=boom), \
                mock.patch.object(acq.os, "link", side_effect=boom), \
                mock.patch.object(acq, "_rename_noreplace", side_effect=boom):
            with self.assertRaises(acq.AcquisitionError):
                self.dl(ok())
        self.assertNothingPublished()


class ObservationTest(Base):
    def test_observation_records_final_url_headers_and_redirects(self):
        hop = Resp(b"", 302, {}, url="https://tracker.example.com/r?u=1")
        headers = {"Content-Type": "audio/mpeg", "Content-Length": str(len(ID3_BODY)),
                   "Date": "Wed, 07 Oct 2026 10:00:00 GMT", "Age": "42",
                   "Cache-Control": "max-age=60", "X-Cache": "HIT",
                   "ETag": '"v1"'}
        resp = Resp(ID3_BODY, 200, headers, url="https://cdn2.example.com/final.mp3",
                    history=[hop])
        obs = self.dl(lambda u, h, t: resp)
        self.assertEqual(obs["final_url"], "https://cdn2.example.com/final.mp3")
        self.assertEqual(obs["http_status"], 200)
        self.assertEqual(obs["content_type"], "audio/mpeg")
        self.assertEqual(obs["content_length"], len(ID3_BODY))
        self.assertEqual(obs["date"], "Wed, 07 Oct 2026 10:00:00 GMT")
        self.assertEqual(obs["age"], "42")
        self.assertEqual(obs["cache_control"], "max-age=60")
        self.assertEqual(obs["response_headers"]["x-cache"], "HIT")
        self.assertEqual(obs["redirect_chain"],
                         [{"url": "https://tracker.example.com/r?u=1", "status": 302}])
        self.assertEqual(obs["validators"]["etag"], '"v1"')
        for key in ("fetch_started_at", "fetch_ended_at"):
            self.assertRegex(obs[key], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")
        record = acq.fetch_observation("dai", {"key": "k"}, None, obs)
        self.assertEqual(record["response"]["final_url"], obs["final_url"])

    def test_final_url_defaults_to_requested_url(self):
        obs = self.dl(ok())
        self.assertEqual(obs["final_url"], URL)
        self.assertEqual(obs["redirect_chain"], [])


class PartialFeedTest(unittest.TestCase):
    HEAD = (b'<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>'
            b'<title>t</title><item><title>a</title><guid>1</guid>'
            b'<enclosure url="https://x/a.mp3" type="audio/mpeg"/></item>')

    def test_truncated_feed_with_partial_entries_raises(self):
        data = self.HEAD + b"<item><title>b</title><gu"
        with self.assertRaises(acq.TruncatedDocument):
            acq.parse_feed_entries(data, "https://x/feed")

    def test_character_encoding_override_alone_is_not_a_failure(self):
        data = (b'<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>'
                b'<title>caf\xe9</title><item><guid>1</guid></item></channel></rss>')
        self.assertEqual(len(acq.parse_feed_entries(data)), 1)

    def test_complete_feed_parses(self):
        data = self.HEAD + b"</channel></rss>"
        self.assertEqual(len(acq.parse_feed_entries(data)), 1)


class EnclosureSuffixTest(unittest.TestCase):
    def test_untyped_enclosure_with_query_string_or_uppercase_suffix(self):
        for href in ("https://cdn.example.com/ep.mp3?source=feed",
                     "https://cdn.example.com/EP.MP3",
                     "https://cdn.example.com/ep.M4A?x=1#frag"):
            with self.subTest(href=href):
                entry = {"enclosures": [{"href": href}]}
                self.assertEqual(acq.entry_audio_url(entry), href)
                self.assertEqual(acq._enclosure_href(entry), href)
                key = acq.episode_identity_key("f", entry)
                self.assertEqual(key["key"], f"f\x00{href}")

    def test_non_audio_untyped_enclosure_still_ignored(self):
        entry = {"enclosures": [{"href": "https://cdn.example.com/cover.jpg?a=1"}]}
        self.assertFalse(acq.entry_audio_url(entry))


class MediaNotModifiedTest(Base):
    def test_validators_are_never_sent_for_media(self):
        seen = {}

        def fetcher(u, h, t):
            seen.update(h)
            return Resp(ID3_BODY, 200, {"Content-Length": str(len(ID3_BODY))})

        self.dl(fetcher, validators={"etag": '"v1"', "last_modified": "Tue"})
        self.assertNotIn("If-None-Match", seen)
        self.assertNotIn("If-Modified-Since", seen)

    def test_media_304_is_an_error_not_success(self):
        with self.assertRaises(acq.AcquisitionError):
            self.dl(lambda u, h, t: Resp(b"", 304, {}))
        self.assertNothingPublished()


class DeadlineTest(Base):
    def test_slow_drip_exceeding_deadline_raises_and_removes_part(self):
        def drip():
            for _ in range(40):
                time.sleep(0.05)
                yield ID3_BODY[:8]

        resp = Resp(b"", 200, {}, chunks=drip)
        start = time.monotonic()
        with self.assertRaises(acq.AcquisitionError):
            self.dl(lambda u, h, t: resp, deadline_s=0.3)
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertNothingPublished()


class FeedToleranceTest(unittest.TestCase):
    HEAD = (b'<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>'
            b'<title>t</title>')
    ITEM = b"<item><title>%s</title><guid>1</guid></item>"
    TAIL = b"</channel></rss>"

    def test_real_world_malformed_but_complete_feeds_still_parse(self):
        cases = {
            "unescaped ampersand": self.HEAD + self.ITEM % b"Q&A" + self.TAIL,
            "html entity": self.HEAD + self.ITEM % b"a&nbsp;b" + self.TAIL,
            "whitespace before declaration":
                b"\n  " + self.HEAD + self.ITEM % b"a" + self.TAIL,
            "control character": self.HEAD + self.ITEM % b"a\x08b" + self.TAIL,
            "stray br": self.HEAD + self.ITEM % b"a<br>b" + self.TAIL,
            "junk after root": self.HEAD + self.ITEM % b"a" + self.TAIL + b"garbage",
        }
        for label, data in cases.items():
            with self.subTest(label=label):
                self.assertEqual(len(acq.parse_feed_entries(data)), 1)

    def test_truncation_signatures_raise_even_with_entries(self):
        atom = (b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
                b"<entry><title>a</title></entry>")
        cases = {
            "cut at element boundary": self.HEAD + self.ITEM % b"a",
            "cut inside a tag": self.HEAD + self.ITEM % b"a" + b"<item><title>b</ti",
            "cut inside CDATA": self.HEAD + b"<item><title><![CDATA[abc",
            "atom cut inside a tag": atom + b"<entry><ti",
        }
        for label, data in cases.items():
            with self.subTest(label=label), self.assertRaises(acq.TruncatedDocument):
                acq.parse_feed_entries(data)

    def test_missing_closing_root_tag_raises_for_other_syntax_errors(self):
        data = self.HEAD + self.ITEM % b"a<br>b" + b"<item><title>x</title></item>"
        with self.assertRaises(acq.TruncatedDocument):
            acq.parse_feed_entries(data)


class ProbeToleranceTest(Base):
    def corrupt_mid(self, data: bytes) -> bytes:
        mid = len(data) // 2
        return data[:mid] + b"\x00" * 200 + data[mid + 200:]

    def test_isolated_frame_errors_do_not_reject_a_complete_mp3(self):
        mp3 = self.corrupt_mid(make_mp3(30, noise=True))
        path = self.dest / "bad.mp3"
        path.write_bytes(mp3)
        self.assertGreater(acq.probe_audio_duration(path), 25)
        obs = self.dl(lambda u, h, t: Resp(mp3, 200, {}), name="e.mp3")
        self.assertEqual(obs["status"], "fetched")

    def test_truncation_still_rejected_when_duration_header_present(self):
        mp3 = make_mp3(30, noise=True)
        with self.assertRaises(acq.AcquisitionError):
            self.dl(lambda u, h, t: Resp(mp3[:len(mp3) // 2], 200, {}))


class LengthVerifiedTest(Base):
    def test_length_verified_true_with_content_length(self):
        obs = self.dl(ok())
        self.assertIs(obs["length_verified"], True)
        self.assertIsNone(obs["probe_duration_s"])

    def test_length_verified_false_and_probe_recorded_without_content_length(self):
        mp3 = make_mp3(5, noise=True)
        obs = self.dl(lambda u, h, t: Resp(mp3, 200, {}))
        self.assertIs(obs["length_verified"], False)
        self.assertAlmostEqual(obs["probe_duration_s"], 5, delta=0.5)

    def test_clean_cut_of_mp3_without_xing_passes_but_is_marked_unverified(self):
        # Documents the probe's blind spot: ffmpeg estimates duration from the
        # file size, so a clean cut of a no-Xing mp3 decodes "completely".
        mp3 = make_mp3(30, noise=True, xing=False)
        obs = self.dl(lambda u, h, t: Resp(mp3[:len(mp3) // 2], 200, {}))
        self.assertEqual(obs["status"], "fetched")
        self.assertIs(obs["length_verified"], False)
        self.assertLess(obs["probe_duration_s"], 20)


class ContentEncodingTest(Base):
    def test_gzip_encoded_response_skips_length_check_and_records_encoding(self):
        mp3 = make_mp3(5, noise=True)
        headers = {"Content-Encoding": "gzip", "Content-Length": "123"}
        obs = self.dl(lambda u, h, t: Resp(mp3, 200, headers))
        self.assertEqual(obs["status"], "fetched")
        self.assertEqual(obs["content_encoding"], "gzip")
        self.assertIs(obs["length_verified"], False)
        self.assertGreater(obs["probe_duration_s"], 4)

    def test_identity_encoding_keeps_length_check(self):
        headers = {"Content-Encoding": "identity", "Content-Length": "999"}
        with self.assertRaises(acq.TruncatedDownload):
            self.dl(lambda u, h, t: Resp(ID3_BODY, 200, headers))


class InterruptCleanupTest(Base):
    def test_keyboard_interrupt_mid_stream_leaves_no_part_file(self):
        def chunks():
            yield ID3_BODY[:10]
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.dl(lambda u, h, t: Resp(b"", 200, {}, chunks=chunks))
        self.assertNothingPublished()

    def test_sweep_stale_parts_removes_only_old_part_files(self):
        old = self.dest / "a.mp3.x1.part"
        fresh = self.dest / "b.mp3.x2.part"
        keep = self.dest / "c.mp3"
        for p in (old, fresh, keep):
            p.write_bytes(b"x")
        past = time.time() - 7200
        os.utime(old, (past, past))
        os.utime(keep, (past, past))
        self.assertEqual(acq.sweep_stale_parts(self.dest, older_than_s=3600),
                         [old.name])
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()),
                         ["b.mp3.x2.part", "c.mp3"])


class NoHardLinkFilesystemTest(Base):
    def no_link(self):
        return mock.patch.object(acq.os, "link",
                                 side_effect=OSError(errno.ENOTSUP, "no hard links"))

    def test_publish_falls_back_when_hard_links_are_unsupported(self):
        with self.no_link():
            obs = self.dl(ok(ID3_BODY + b"one"))
            self.assertEqual(Path(obs["path"]).read_bytes(), ID3_BODY + b"one")
            self.assertEqual(self.dl(ok(ID3_BODY + b"one"))["sha256"], obs["sha256"])
            with self.assertRaises(acq.SnapshotConflict):
                self.dl(ok(ID3_BODY + b"two"))
        self.assertEqual((self.dest / "e.mp3").read_bytes(), ID3_BODY + b"one")
        self.assertEqual([p.name for p in self.dest.iterdir()], ["e.mp3"])


class EstimatedDurationTest(Base):
    def test_complete_vbr_mp3_without_xing_is_not_rejected_by_size_estimate(self):
        mp3 = make_vbr_mp3()
        obs = self.dl(lambda u, h, t: Resp(mp3, 200, {}))
        self.assertEqual(obs["status"], "fetched")
        self.assertIs(obs["length_verified"], False)
        self.assertAlmostEqual(obs["probe_duration_s"], 60, delta=1)

    def test_trailing_padding_on_mp3_without_xing_is_not_rejected(self):
        mp3 = make_mp3(30, noise=True, xing=False)
        padded = mp3 + b"\x00" * (len(mp3) // 10)
        obs = self.dl(lambda u, h, t: Resp(padded, 200, {}))
        self.assertEqual(obs["status"], "fetched")
        self.assertIs(obs["length_verified"], False)


class NoClobberPublishTest(Base):
    def spy_replace(self, seen):
        real = os.replace

        def spy(src, dst, *a, **kw):
            if os.path.exists(dst):
                seen.append(os.path.getsize(dst))
            return real(src, dst, *a, **kw)
        return mock.patch.object(acq.os, "replace", side_effect=spy)

    def test_final_is_never_visible_empty_when_hard_links_fail(self):
        link = mock.patch.object(acq.os, "link", side_effect=OSError(errno.ENOTSUP, "no"))
        for label, patches in (
                ("rename-noreplace", []),
                ("lock fallback", [mock.patch.object(acq, "_rename_noreplace",
                                                     return_value=None, create=True)])):
            with self.subTest(label=label):
                seen = []
                with link, self.spy_replace(seen):
                    for p in patches:
                        p.start()
                        self.addCleanup(p.stop)
                    obs = self.dl(ok(ID3_BODY + label.encode()), name=label + ".mp3")
                self.assertNotIn(0, seen)
                self.assertEqual(Path(obs["path"]).read_bytes(), ID3_BODY + label.encode())
                self.assertEqual([p.name for p in self.dest.glob(label + "*")],
                                 [label + ".mp3"])

    def test_concurrent_identical_downloads_never_conflict_without_hard_links(self):
        errors = []

        def worker():
            try:
                self.dl(ok(ID3_BODY + b"same"))
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(acq.os, "link", side_effect=OSError(errno.ENOTSUP, "no")), \
                mock.patch.object(acq, "_rename_noreplace", return_value=None, create=True):
            threads = [threading.Thread(target=worker) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(errors, [])
        self.assertEqual([p.name for p in self.dest.iterdir()], ["e.mp3"])

    def test_stale_lock_file_does_not_block_publish(self):
        lock = self.dest / "e.mp3.lock"
        lock.write_bytes(b"")
        os.utime(lock, (time.time() - 7200, time.time() - 7200))
        with mock.patch.object(acq.os, "link", side_effect=OSError(errno.ENOTSUP, "no")), \
                mock.patch.object(acq, "_rename_noreplace", return_value=None, create=True):
            self.assertEqual(self.dl(ok())["status"], "fetched")
        self.assertEqual([p.name for p in self.dest.iterdir()], ["e.mp3"])

    def test_stale_zero_byte_final_from_a_killed_publish_is_replaced(self):
        final = self.dest / "e.mp3"
        final.write_bytes(b"")
        os.utime(final, (time.time() - 7200, time.time() - 7200))
        self.assertEqual(self.dl(ok())["status"], "fetched")
        self.assertEqual(final.read_bytes(), ID3_BODY)

    def test_rename_noreplace_primitive_never_overwrites(self):
        src, dst = self.dest / "src", self.dest / "dst"
        src.write_bytes(b"new")
        dst.write_bytes(b"old")
        with self.assertRaises(FileExistsError):
            acq._rename_noreplace(src, dst)
        self.assertEqual(dst.read_bytes(), b"old")
        dst.unlink()
        if acq._rename_noreplace(src, dst) is None:
            self.skipTest("platform has no no-replace rename")
        self.assertEqual(dst.read_bytes(), b"new")


class CleanupRobustnessTest(Base):
    def test_legacy_part_that_is_a_directory_does_not_leak_a_part_file(self):
        (self.dest / "e.mp3.part").mkdir()
        self.assertEqual(self.dl(ok())["status"], "fetched")
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()),
                         ["e.mp3", "e.mp3.part"])

    def test_sweep_skips_unremovable_entries_and_continues(self):
        stuck = self.dest / "a.part"
        stuck.mkdir()
        (stuck / "child").write_bytes(b"x")
        old = self.dest / "b.mp3.x.part"
        old.write_bytes(b"x")
        past = time.time() - 7200
        for p in (stuck, old):
            os.utime(p, (past, past))
        self.assertEqual(acq.sweep_stale_parts(self.dest, older_than_s=3600),
                         [old.name])
        self.assertTrue(stuck.exists())


class NotAFeedTest(unittest.TestCase):
    def test_empty_and_html_documents_raise_instead_of_returning_no_entries(self):
        for label, data in (("empty", b""),
                            ("html 200", b"<html><body>Not found</body></html>"),
                            ("whitespace", b"  \n")):
            with self.subTest(label=label), self.assertRaises(acq.NotAFeed):
                acq.parse_feed_entries(data, "https://x/feed")

    def test_valid_feed_with_zero_items_is_still_empty_list(self):
        data = (b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
                b"</channel></rss>")
        self.assertEqual(acq.parse_feed_entries(data), [])
        atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><title>t</title></feed>'
        self.assertEqual(acq.parse_feed_entries(atom), [])


class CoverArtMp3Test(Base):
    def test_large_id3v2_tag_keeps_the_duration_reference(self):
        mp3 = make_cover_art_mp3()
        self.assertGreater(len(mp3), 10000)
        path = self.dest / "art.mp3"
        path.write_bytes(mp3)
        self.assertTrue(acq._has_vbr_header(path))
        obs = self.dl(lambda u, h, t: Resp(mp3, 200, {}))
        self.assertAlmostEqual(obs["probe_duration_s"], 30, delta=1)

    def test_truncated_cover_art_mp3_without_content_length_is_rejected(self):
        mp3 = make_cover_art_mp3()
        with self.assertRaises(acq.AcquisitionError):
            self.dl(lambda u, h, t: Resp(mp3[:len(mp3) * 6 // 10], 200, {}))
        self.assertNothingPublished()


def _jpeg(size=64) -> bytes:
    import av
    from fractions import Fraction
    ctx = av.CodecContext.create("mjpeg", "w")
    ctx.width = ctx.height = size
    ctx.pix_fmt = "yuvj420p"
    ctx.time_base = Fraction(1, 25)
    frame = av.VideoFrame(size, size, "yuvj420p")
    for plane in frame.planes:
        plane.update(random.Random(4).randbytes(plane.buffer_size))
    return b"".join(bytes(p) for p in ctx.encode(frame) + ctx.encode(None))


def _syncsafe(n: int) -> bytes:
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def _id3(body: bytes, version=3, flags=0, footer=False) -> bytes:
    tag = b"ID3" + bytes([version, 0, flags]) + _syncsafe(len(body)) + body
    return tag + (b"3DI" + bytes([version, 0, flags]) + _syncsafe(len(body)) if footer else b"")


def _apic(jpeg: bytes, unsync=False) -> bytes:
    body = b"\x00image/jpeg\x00\x03\x00" + jpeg
    if unsync:
        body = body.replace(b"\xff", b"\xff\x00")
    return b"APIC" + len(body).to_bytes(4, "big") + b"\x00\x00" + body


def _ape(items: bytes) -> bytes:
    header = b"APETAGEX" + (2000).to_bytes(4, "little") + (len(items) + 32).to_bytes(4, "little") \
        + (1).to_bytes(4, "little") + (1 << 31).to_bytes(4, "little") + b"\x00" * 8
    return header + items + b"\x00" * 32


def _bare_audio(seconds=30) -> bytes:
    """Xing mp3 frames with PyAV's own ID3 tag stripped."""
    data = make_mp3(seconds, noise=True)
    n = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
    return data[10 + n:]


class LeadingTagVariantsTest(Base):
    def prefixes(self):
        jpg = _apic(_jpeg(200))
        return {
            "no id3": b"",
            "two id3 tags": _id3(jpg) + _id3(jpg),
            "three id3 tags": _id3(jpg) + _id3(_apic(_jpeg(80))) + _id3(jpg),
            "v2.4 unsynchronised": _id3(_apic(_jpeg(200), unsync=True), 4, 0x80),
            "v2.4 extended header": _id3(b"\x00\x00\x00\x06\x01\x00" + jpg, 4, 0x40),
            "v2.4 footer": _id3(jpg, 4, 0x10, footer=True),
            "leading APE tag": _ape(b"\x00\x00\x00\x00" + b"\x00" * 8 + b"Cover\x00" + _jpeg(100)),
            "APE then ID3": _ape(_jpeg(100)) + _id3(jpg),
        }

    def test_duration_reference_survives_every_leading_tag_layout(self):
        audio = _bare_audio()
        for label, prefix in self.prefixes().items():
            with self.subTest(label=label):
                path = self.dest / "t.mp3"
                path.write_bytes(prefix + audio)
                self.assertTrue(acq._has_vbr_header(path))

    def test_truncated_audio_behind_every_leading_tag_layout_is_rejected(self):
        audio = _bare_audio()
        for label, prefix in self.prefixes().items():
            with self.subTest(label=label):
                body = prefix + audio[:len(audio) * 4 // 10]
                name = label.replace(" ", "_") + ".mp3"
                with self.assertRaises(acq.AcquisitionError):
                    self.dl(lambda u, h, t: Resp(body, 200, {}), name=name)
                self.assertFalse((self.dest / name).exists())

    def test_image_bytes_that_look_like_a_sync_are_not_taken_for_a_frame(self):
        # No Xing anywhere: art with FF E0 bytes must not be mistaken for MPEG audio.
        path = self.dest / "t.mp3"
        path.write_bytes(_id3(_apic(_jpeg(200))) + b"\xff\xe0" * 64)
        self.assertFalse(acq._has_vbr_header(path))


class BestEffortFinallyTest(Base):
    def test_close_failure_does_not_mask_success_or_original_error(self):
        class Closing(Resp):
            def close(self):
                raise RuntimeError("close boom")

        body = ID3_BODY
        obs = self.dl(lambda u, h, t: Closing(body, 200, {"Content-Length": str(len(body))}))
        self.assertEqual(obs["status"], "fetched")
        with self.assertRaises(acq.NotAudio):
            self.dl(lambda u, h, t: Closing(b"<html>x</html>" * 4, 200,
                                            {"Content-Length": "56"}), name="x.mp3")

    def test_part_unlink_failure_does_not_mask_the_original_error(self):
        real = Path.unlink

        def flaky(self_, missing_ok=False):
            if self_.name.endswith(".part"):
                raise PermissionError("unlink boom")
            return real(self_, missing_ok=missing_ok)

        with mock.patch.object(Path, "unlink", flaky):
            with self.assertRaises(acq.NotAudio):
                self.dl(ok(b"<html>x</html>" * 4))
            self.assertEqual(self.dl(ok(), name="y.mp3")["status"], "fetched")


class Utf16FeedTest(unittest.TestCase):
    XML = ('<?xml version="1.0" encoding="utf-16"?><rss version="2.0"><channel>'
           "<title>t</title><item><title>a</title><guid>1</guid></item>")

    def test_utf16_feed_parses(self):
        for label, data in (("utf-16 bom", (self.XML + "</channel></rss>").encode("utf-16")),
                            ("utf-16le", (self.XML + "</channel></rss>").encode("utf-16-le")),
                            ("utf-16be", (self.XML + "</channel></rss>").encode("utf-16-be"))):
            with self.subTest(label=label):
                self.assertEqual(len(acq.parse_feed_entries(data)), 1)

    def test_truncated_utf16_feed_still_raises(self):
        data = (self.XML + "<item><title>b</ti").encode("utf-16")
        with self.assertRaises(acq.TruncatedDocument):
            acq.parse_feed_entries(data)


class FutureMtimeAndLockTimeoutTest(Base):
    def no_link(self):
        return (mock.patch.object(acq.os, "link", side_effect=OSError(errno.ENOTSUP, "no")),
                mock.patch.object(acq, "_rename_noreplace", return_value=None))

    def future(self, path):
        t = time.time() + 86400
        os.utime(path, (t, t))

    def test_future_mtime_lock_is_treated_as_stale(self):
        lock = self.dest / "e.mp3.lock"
        lock.write_bytes(b"")
        self.future(lock)
        a, b = self.no_link()
        with a, b:
            self.assertEqual(run_bounded(lambda: self.dl(ok()))["status"], "fetched")

    def test_future_mtime_empty_final_is_treated_as_stale(self):
        final = self.dest / "e.mp3"
        final.write_bytes(b"")
        self.future(final)
        self.assertEqual(run_bounded(lambda: self.dl(ok()))["status"], "fetched")
        self.assertEqual(final.read_bytes(), ID3_BODY)

    def test_lock_wait_has_an_overall_timeout(self):
        lock = self.dest / "e.mp3.lock"
        lock.write_bytes(b"")
        past = time.time() - 7200
        os.utime(lock, (past, past))
        a, b = self.no_link()
        # An undeletable lock must end in an error, not an endless loop.
        with a, b, mock.patch.object(acq, "_LOCK_STALE_S", 0.1), \
                mock.patch.object(acq, "_unlink_quiet", lambda p: None if p.suffix == ".lock" else p.unlink(missing_ok=True)):
            with self.assertRaises(acq.AcquisitionError):
                run_bounded(lambda: self.dl(ok()), timeout=5.0)


class LoopbackHandler(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_address[1]}":
            return self.send_error(404)          # an outside probe: not recorded
        LoopbackHandler.seen.append((self.path, self.headers.get("Accept-Encoding")))
        if self.path == "/r":
            self.send_response(302)
            self.send_header("Location", "/final.mp3")
            self.end_headers()
        elif self.path == "/final.mp3":
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(ID3_BODY)))
            self.end_headers()
            self.wfile.write(ID3_BODY)
        elif self.path == "/drip":
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b"ID3")
                    self.wfile.flush()
                    time.sleep(0.1)
            except OSError:
                pass


class RequestsFetcherTest(Base):
    def setUp(self):
        super().setUp()
        LoopbackHandler.seen = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), LoopbackHandler)  # ephemeral port
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_address[1]}"
        self.session = requests.Session()
        self.session.trust_env = False
        self.addCleanup(self.session.close)

    def fetch(self, path, **kw):
        return acq.download_media(
            self.base + path, dest_dir=self.dest, name="e.mp3",
            fetcher=acq.requests_fetcher(self.session), max_bytes=1 << 20, **kw)

    def test_identity_encoding_redirect_chain_and_final_url(self):
        obs = self.fetch("/r")
        self.assertEqual(obs["final_url"], self.base + "/final.mp3")
        self.assertEqual([h["status"] for h in obs["redirect_chain"]], [302])
        self.assertEqual(obs["content_length"], len(ID3_BODY))
        self.assertEqual(obs["bytes"], len(ID3_BODY))
        self.assertEqual({enc for _, enc in LoopbackHandler.seen}, {"identity"})

    def test_a_request_for_another_host_is_refused_and_not_recorded(self):
        port = self.base.rsplit(":", 1)[1]
        r = self.session.get(self.base + "/final.mp3", headers={"Host": f"localhost:{port}"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(LoopbackHandler.seen, [])

    def test_requests_slow_drip_hits_deadline_not_chunk_size(self):
        start = time.monotonic()
        with self.assertRaises(acq.DeadlineExceeded):
            self.fetch("/drip", deadline_s=0.5)
        self.assertLess(time.monotonic() - start, 3)
        self.assertNothingPublished()


if __name__ == "__main__":
    unittest.main()
