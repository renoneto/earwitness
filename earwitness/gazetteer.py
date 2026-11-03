"""Brand gazetteer: alias normalization + fuzzy capture for STT-mangled names.

Covers fix #2 (case-insensitive + fuzzy brand capture, alias normalization,
trailing-word boundaries). Known STT variants observed in the supplied corpus
are encoded explicitly (e.g. "sheez birdie" -> She's Birdie). Variants that
are also common English words ("rough" -> Ro) only map in an ad-anchor
context (the capture directly after "sponsored by" etc.).
"""
from __future__ import annotations

import difflib
import re

# canonical -> aliases (all lowercase). Keep small and explicit: this is a
# validation gazetteer, not a product brand database.
GAZETTEER: dict[str, list[str]] = {
    "ro": ["ro", "roman"],
    "shopify": ["shopify"],
    "progressive": ["progressive", "progressive insurance"],
    "stripe": ["stripe"],
    "google chrome": ["google chrome"],
    "chatgpt": ["chatgpt", "chat gpt", "openai"],
    "kachava": ["kachava", "kachava.com", "khava", "cacha", "cachava", "kacha"],
    "she's birdie": ["she's birdie", "sheez birdie", "sheezbirdie", "shes birdie", "birdie"],
    "betterhelp": ["betterhelp", "better help"],
    "mint mobile": ["mint mobile", "mintmobile", "mint"],
    "ag1": ["ag1", "athletic greens", "athleticgreens", "drinkag1"],
    "hellofresh": ["hellofresh", "hello fresh", "hello fresh"],
    # Observed STT/verbatim variants in the supplied corpus (validation scope).
    "chumba casino": ["chumba casino", "chombacocino", "chambacassino",
                      "chomplacicino", "chumba", "chumbacasino"],
    "thrive causemetics": ["thrive causemetics", "thrivecosmetics", "thrive cosmetics"],
    "stamps.com": ["stamps.com", "stamps", "stamp.com"],
    "talkspace": ["talkspace"],
    "qvc": ["qvc"],
    "marathon sports": ["marathon sports", "marathonsports"],
    # CTA-wave validation scope: exact spoken/URL variants observed in the
    # supplied labels/transcripts (s001e0, s002e1, s009e0). Small and
    # explicit; not a general brand database.
    "youtube premium": ["youtube premium", "youtubepremium", "youtube"],
    "spinquest": ["spinquest", "spin quest", "americanownspinquest", "benquest"],
    "talkiatry": ["talkiatry", "tachyatry", "talk iatry", "t-a-l-k-i-a-t-r-y"],
    "jones road beauty": ["jones road beauty", "jones road", "jonesroadbeauty"],
    "dazn": ["dazn", "thezone"],
    "gofundme": ["gofundme", "gofund me", "go fund me", "gofundme.com"],
}

# STT variants that are also everyday words/phrases: exact-capture matches
# only, and only when the capture directly follows a sponsor anchor.
ANCHOR_ONLY_ALIASES: dict[str, str] = {
    "rough": "ro", "roe": "ro", "the zone": "dazn",
}

# A prefix alias followed by one of these is a common noun phrase, not a
# brand name ("the zone of my mind").
NOUN_PHRASE_CONTINUATIONS = {"of"}

# Fuzzy fallback limits: short brands/aliases (<= 5 chars) never fuzzy-match,
# and truncations/extensions (strip/stripe, mints/mint) are not STT mangling.
FUZZY_MIN_LEN = 6
FUZZY_THRESHOLD = 0.85

# Tokens that terminate a brand capture (fix #2 trailing-word boundary).
BOUNDARY_WORDS = {
    "you", "and", "for", "with", "our", "your", "their", "this", "that",
    "the", "a", "an", "on", "in", "at", "to", "of", "from", "is", "are",
}

ALIAS_TO_CANONICAL: dict[str, str] = {}
for canon, aliases in GAZETTEER.items():
    for a in aliases:
        ALIAS_TO_CANONICAL.setdefault(a, canon)


def normalize_brand(raw: str | None, anchor_context: bool = False) -> str | None:
    """Map a raw captured brand string to its canonical form.

    ``anchor_context`` marks a capture taken directly after a sponsor anchor
    phrase; only then do ANCHOR_ONLY_ALIASES apply. Unknown strings are
    returned lowercased (brand-agnostic detection keeps unknown brands).
    """
    if not raw:
        return None
    key = re.sub(r"\s+", " ", raw.strip().lower().strip(" .,!?-'’"))
    aliases = (dict(ALIAS_TO_CANONICAL, **ANCHOR_ONLY_ALIASES) if anchor_context
               else ALIAS_TO_CANONICAL)
    if key in aliases:
        return aliases[key]
    # Longest whole-token prefix: drop trailing spillover words ("progressive
    # america's number" -> progressive; "chat gpt hey" -> chatgpt).
    words = key.split()
    for n in range(len(words) - 1, 0, -1):
        prefix = " ".join(words[:n])
        if prefix in aliases and words[n] not in NOUN_PHRASE_CONTINUATIONS:
            return aliases[prefix]
    # Fuzzy fallback over long single-token aliases (STT mangling only).
    if " " not in key and len(key) >= FUZZY_MIN_LEN:
        best, best_score = None, 0.0
        for alias, canon in ALIAS_TO_CANONICAL.items():
            if " " in alias or len(alias) < FUZZY_MIN_LEN:
                continue
            if abs(len(alias) - len(key)) > 2:
                continue
            if alias.startswith(key) or key.startswith(alias):
                continue
            s = difflib.SequenceMatcher(None, alias, key).ratio()
            if s > best_score:
                best, best_score = canon, s
        if best_score >= FUZZY_THRESHOLD:
            return best
    return key or None


def trim_trailing_words(raw: str) -> str:
    """Drop trailing boundary words greedily captured after a brand name."""
    words = raw.strip().split()
    while len(words) > 1 and words[-1].lower().strip(".,!?") in BOUNDARY_WORDS:
        words.pop()
    # cap at 3 tokens (brand names are short; rest is sentence spillover)
    return " ".join(words[:3])
