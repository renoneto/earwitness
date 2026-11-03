"""Audio quarantine: proven-wrong audio resolves to no media.

Quarantined episode ids (earwitness.app.media.AUDIO_QUARANTINED) map to
nothing in media_for_episode regardless of remote URL or local scratch
files, so review pages show the honest unavailable path. Reversible by
deleting the set entry once content-verified audio exists.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from earwitness.app import media as media_mod

FAKE_QUARANTINED = "audit:fake-quarantined-ep"


class AudioQuarantineTest(unittest.TestCase):
    def setUp(self):
        self._prev = set(media_mod.AUDIO_QUARANTINED)
        media_mod.AUDIO_QUARANTINED.add(FAKE_QUARANTINED)

    def tearDown(self):
        media_mod.AUDIO_QUARANTINED.clear()
        media_mod.AUDIO_QUARANTINED.update(self._prev)

    def test_quarantined_episode_has_no_media(self):
        ep = {"id": FAKE_QUARANTINED, "audio_url": "https://audio.example-cdn.net/x.mp3"}
        self.assertIsNone(media_mod.media_for_episode(ep, repo=REPO))

    def test_non_quarantined_remote_episode_unchanged(self):
        ep = {"id": "s001e1", "audio_url": "https://audio.example-cdn.net/s001e1.mp3"}
        media = media_mod.media_for_episode(ep, repo=REPO)
        self.assertIsNotNone(media)
        self.assertEqual(media["kind"], "remote_full")
        self.assertEqual(media["src"], ep["audio_url"])

    def test_s001e0_cleared_by_audit(self):
        # 2026-09-24: the reported "wrong show" proved to be a rotating
        # cross-promo ad pod; the stream body is the True Crime episode.
        self.assertNotIn("s001e0", media_mod.AUDIO_QUARANTINED)

    def test_quarantine_set_holds_only_strings(self):
        for ep_id in self._prev:
            self.assertIsInstance(ep_id, str)
            self.assertNotIn("/", ep_id)


if __name__ == "__main__":
    unittest.main()
