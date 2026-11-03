"""ASK-PROMPT-AD red test (lead AW-9): a sentence that cites only ad
passages must say it is an ad, even when the question asks what hosts
"mention". Otherwise F6 (mentioned_all_ads) drops the claim and the
Wayfair Rewards offer never shows (WAYFAIR-REWARDS-WHY.md)."""
import unittest

from earwitness.app import ask

ADVERTISED = ('Say a brand advertised only when you cite a passage labelled "ad · <that brand>";'
              ' otherwise say it was mentioned.')
AD_VOICE = ('When every passage a sentence cites is inside ads, say so in that sentence'
            ' ("a Wayfair ad says…", "an ad offers…"), even when the question asks what'
            ' hosts mention; name a brand only under the rule above.')


class AskPromptAdVoiceTest(unittest.TestCase):
    def test_system_has_the_ad_voice_line_right_after_the_advertised_line(self):
        lines = ask.SYSTEM.splitlines()
        advertised = [i for i, line in enumerate(lines) if line.startswith(ADVERTISED)]
        self.assertEqual(len(advertised), 1, lines)
        self.assertEqual(lines[advertised[0] + 1], AD_VOICE)
        self.assertEqual(ask.SYSTEM.count(AD_VOICE), 1)

    def test_no_example_ties_a_brand_pronoun_to_an_ad(self):
        # AW-10: "its ads offer…" invites a pronoun that ties a brand to a held ad.
        self.assertNotIn("its ads offer", ask.SYSTEM)

    def test_the_line_is_in_system_not_brand_system(self):
        self.assertNotIn(AD_VOICE, ask.BRAND_SYSTEM)


if __name__ == "__main__":
    unittest.main()
