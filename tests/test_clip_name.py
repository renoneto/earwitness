"""CLIP design 4.2 and 4.3 step 4: one canonical clip name with one parser,
and the MP3 duration the host measures by walking frame headers (standard
library, no ffprobe). Synthetic frames only.
"""
from __future__ import annotations

import unittest

try:
    from scripts import clip_name
except ImportError:  # red: the module does not exist yet
    clip_name = None

# MPEG-2 Layer III, 24 kbps, 22,050 Hz, mono: the cutter's output.
MPEG2_HEADER = bytes([0xFF, 0xF3, 0x30, 0xC0])
MPEG2_FRAME = 72 * 24000 // 22050          # 78 bytes
MPEG2_SECONDS = 576 / 22050
# MPEG-1 Layer III, 128 kbps, 44,100 Hz, mono.
MPEG1_HEADER = bytes([0xFF, 0xFB, 0x90, 0xC0])
MPEG1_FRAME = 144 * 128000 // 44100        # 417 bytes
MPEG1_SECONDS = 1152 / 44100


def frames(seconds: float, *, mpeg: int = 2) -> bytes:
    """Frame-only MP3 bytes (no tag, no Xing frame) lasting about `seconds`."""
    header, size, per = ((MPEG2_HEADER, MPEG2_FRAME, MPEG2_SECONDS) if mpeg == 2
                         else (MPEG1_HEADER, MPEG1_FRAME, MPEG1_SECONDS))
    return (header + b"\x00" * (size - 4)) * round(seconds / per)


class _Base(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(clip_name, "scripts/clip_name.py is missing")


class ParseTest(_Base):
    def test_canonical_name_parses(self):
        self.assertEqual(clip_name.parse("acme-REQ-20261102-1-clip-ep_a-5000-28000.mp3"),
                         {"workspace_id": "acme", "request_id": "REQ-20261102-1",
                          "episode_id": "ep_a", "start_ms": 5000, "end_ms": 28000})

    def test_an_episode_id_with_hyphens_parses_from_the_right(self):
        got = clip_name.parse("acme-REQ-20261102-1-clip-ep-1-2-10000-25000.mp3")
        self.assertEqual((got["episode_id"], got["start_ms"], got["end_ms"]),
                         ("ep-1-2", 10000, 25000))
        got = clip_name.parse("my-ws-REQ-20261102-12-clip-a.b_c-0-1.mp3")
        self.assertEqual((got["workspace_id"], got["request_id"], got["episode_id"]),
                         ("my-ws", "REQ-20261102-12", "a.b_c"))

    def test_other_names_do_not_parse(self):
        for name in ("acme-REQ-20261102-1-clip-ep_a-5000-28000.wav",
                     "acme-REQ-20261102-1-clip-ep_a-5000-28000.mp3\n",
                     "acme-REQ-20261102-1-clip-ep_a-28000-5000.mp3",
                     "acme-REQ-20261102-1-clip-ep_a-5000-5000.mp3",
                     "acme-REQ-20261102-1-clip-ep_a-5000.mp3",
                     "acme-REQ-20261102-1-ep_a-5000-28000.mp3",
                     "Acme-REQ-20261102-1-clip-ep_a-5000-28000.mp3",
                     "acme-REQ-2026110-1-clip-ep_a-5000-28000.mp3",
                     "acme-REQ-20261102-1-clip-ep_a-5000-1234567890.mp3",
                     "acme-REQ-20261102-1-clip-ep/a-5000-28000.mp3",
                     "acme-REQ-20261102-1-clip-" + "e" * 65 + "-5000-28000.mp3",
                     "../acme-REQ-20261102-1-clip-ep_a-5000-28000.mp3",
                     "acme-REQ-20261102-1-clip-ep_a-5000-28000.mp3.part"):
            with self.subTest(name=name):
                self.assertIsNone(clip_name.parse(name))

    def test_name_builds_what_parse_reads(self):
        name = clip_name.name("acme", "REQ-20261102-1", "ep-1-2", 10000, 25000)
        self.assertEqual(name, "acme-REQ-20261102-1-clip-ep-1-2-10000-25000.mp3")
        self.assertEqual(clip_name.parse(name)["episode_id"], "ep-1-2")


class Mp3SecondsTest(_Base):
    def test_mpeg2_frames_give_their_duration(self):
        data = frames(23.0)
        self.assertAlmostEqual(clip_name.mp3_seconds(data),
                               len(data) // MPEG2_FRAME * MPEG2_SECONDS, places=6)
        self.assertAlmostEqual(clip_name.mp3_seconds(data), 23.0, delta=0.03)

    def test_mpeg1_frames_give_their_duration(self):
        self.assertAlmostEqual(clip_name.mp3_seconds(frames(10.0, mpeg=1)), 10.0, delta=0.03)

    def test_a_padded_frame_is_one_byte_longer(self):
        padded = bytes([0xFF, 0xF3, 0x32, 0xC0]) + b"\x00" * (MPEG2_FRAME - 3)
        self.assertAlmostEqual(clip_name.mp3_seconds(padded + frames(1.0)),
                               MPEG2_SECONDS * (1 + round(1.0 / MPEG2_SECONDS)), places=6)

    def test_bytes_outside_frames_are_refused(self):
        good = frames(5.0)
        frame = MPEG2_HEADER + b"\x00" * (MPEG2_FRAME - 4)
        for label, data in (
                ("empty", b""),
                ("trailing ID3v1 tag", good + b"TAG" + b"\x00" * 125),
                ("leading ID3v2 tag", b"ID3\x04\x00\x00\x00\x00\x00\x0a" + b"\x00" * 10 + good),
                ("truncated last frame", good[:-1]),
                ("one stray byte", good + b"\x00"),
                ("free-format bitrate", bytes([0xFF, 0xF3, 0x00, 0xC0]) + frame[4:]),
                ("bad bitrate", bytes([0xFF, 0xF3, 0xF0, 0xC0]) + frame[4:]),
                ("reserved sample rate", bytes([0xFF, 0xF3, 0x3C, 0xC0]) + frame[4:]),
                ("layer II", bytes([0xFF, 0xF5, 0x30, 0xC0]) + frame[4:]),
                ("MPEG-2.5", bytes([0xFF, 0xE3, 0x30, 0xC0]) + frame[4:]),
                ("reserved version", bytes([0xFF, 0xEB, 0x30, 0xC0]) + frame[4:])):
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    clip_name.mp3_seconds(data)


if __name__ == "__main__":
    unittest.main()
