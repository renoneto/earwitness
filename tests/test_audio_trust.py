"""Audio trust: media descriptors carry the 2026-09-24 audit verdict.

exact: audio matches the transcript, listening verifies. rotated: same
show, prerolls rotated — ad windows near 0 may play a different
creative. unknown: unverified, generic live-stream caution applies.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from earwitness.app import media as media_mod


class AudioTrustTest(unittest.TestCase):
    def test_audio_trust_mapping(self):
        self.assertEqual(media_mod.audio_trust("bench_3"), "exact")
        self.assertEqual(media_mod.audio_trust("upload:pilot-acquired"), "exact")
        self.assertEqual(media_mod.audio_trust("s005e0"), "exact")
        self.assertEqual(media_mod.audio_trust("s001e0"), "rotated")
        self.assertEqual(media_mod.audio_trust("s009e1"), "rotated")
        self.assertEqual(media_mod.audio_trust("s028e0"), "unknown")
        self.assertEqual(media_mod.audio_trust("no-such-ep"), "unknown")

    def test_remote_descriptors_carry_trust(self):
        exact = media_mod.media_for_episode(
            {"id": "s005e0", "audio_url": "https://audio.example-cdn.net/x.mp3"})
        self.assertEqual(exact["trust"], "exact")
        rotated = media_mod.media_for_episode(
            {"id": "s001e0", "audio_url": "https://audio.example-cdn.net/x.mp3"})
        self.assertEqual(rotated["trust"], "rotated")
        unknown = media_mod.media_for_episode(
            {"id": "s028e0", "audio_url": "https://audio.example-cdn.net/x.mp3"})
        self.assertEqual(unknown["trust"], "unknown")

    def test_clip_descriptor_is_exact(self):
        media = media_mod.media_for_episode({"id": "s002e1", "media_id": "practice-clip-s002e1"})
        self.assertEqual(media["kind"], "local_clip")
        self.assertEqual(media["trust"], "exact")


if __name__ == "__main__":
    unittest.main()
