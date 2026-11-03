"""Ask passages and the cite-or-drop checks (T6 + T7, ASK-CONTRACT section 2).

Known limits (owner decisions, 2026-10-25; conservative for Nov 4):
- Status and tone words inside a verified quote are exempt, even a one- or
  two-word quote ('This is "likely" a Saatva ad.' is kept).
- "confirmed", "verified" and "suggested" (and "confirms", "confirming",
  "confirmation", "verifies", "verifying", "verification") are removed
  anywhere, not only when applied to an ad or brand ("the Fed confirmed a
  rate cut" goes).
- A quote that skips words with an ellipsis ("...", "…", "[...]") is
  removed unless the whole quoted text is in one passage.
- Not checked: numbers written as words ("seventy percent" passes; "15%"
  against "fifteen percent" is removed).
- "most popular" (and "most frequent", "most common") counts as a count
  claim, even when an ad says it of its own product.
- Labels carry no channel word ("spoken", "url", "code").
- A one-word brand or show name written in lower case is not checked
  against its passages, nor for held-ad backing (only capitalised names
  are; no word list is read). A held ad's brand that is not in the name
  table is not checked.
- A workspace's no-display brand key made only of plain words with fewer
  than 3 ads ("asana", "store") is a name only in an item with ad wording
  or citing its detected ad, and only capitalised there ("Asana is
  advertised" is checked; "asana is advertised" and "Store credit comes
  back" are not). A plain phrase with fewer than 3 ads ("magic spoon") is
  read in lower case under the same condition, and binds ad wording only
  right next to it (ASK-CHECK-WORDS). Such a word or phrase no cited
  passage says is not checked at all ("Asana is advertised" citing an ad
  that never says Asana is kept, as before ASK-CHECK-WORDS), nor one word at
  a sentence start that the cited passages say only at a sentence start or
  in lower case ("Therapy ads for BetterHelp", "Dan reads the ad"), and
  "<plain-word name> reads" is never ad wording about it (R1 round 2). Only
  a passage in an ad makes such a word a name; it never takes ad wording
  from another brand within 6 words ("An ad promotes the June sale at
  Wayfair"), and "<plain-word name> offers|codes|discounts" within 6 words of
  a brand a cited ad backs is no ad wording about it ("Dan offers 20 percent
  off at Wayfair"; R1 round 3). A plain word that is the only name near ad
  wording and that the cited ad says is checked ("Contentful, in
  partnership with the Atlantic" citing an ad attributed to "contentfull").
- F6 is a word rule: any ad word in the item spares it. "Saatva is
  mentioned in the non ad segment." is kept; the Sources label still shows
  the passage is an ad.
- Plain-code word lists (status, tone, count, absence, ad wording) are never
  complete; an integrated host read with no cue still reads as a mention
  (F12), and an ASR homophone can still ground a brand (F13).
- Ad wording binds only to the brand inside it or the nearest brand within
  6 words: "Casper, not Saatva, sponsored this episode." was kept when a cited
  ad is Saatva's (the absence rule now removes it: "not" near "sponsored",
  plan review M2), and a brand farther away is not tied.
- The broad word lists also remove some honest paraphrases ("consumer
  sentiment fell", "record label", "usually ripen", "does not cover", "3pm",
  "Shopify's ad business"); measured honest removal 15.6%, under the 20% bar.
- A show name with partial capitals ("Garden hour") is not checked; a held
  ad's label wording ("an ad without an identified brand", "a brandless ad")
  passes; "This hundred-night offer is from Saatva." citing a held ad and a
  talk passage is kept.
- Quotes fold punctuation, so '"for $100"' matches "for 100 nights"; passage
  metadata grounds numbers ("12%" by pub_date 2026-09-12).
- "exclusive sponsor" is not an absence claim, and "appears on" is not an F6
  talk verb.
- An item citing only in-ad passages with no detected ad keeps neither
  "mentioned" (F6: an ad read is no mention) nor "advertised" (item 5: no
  detected ad to credit); it is removed (owner, 2026-10-26).
- "both" is not a count word ("Both shows talk about beds." is kept).
- Doubt next to a capitalised brand is removed even in an uncertain item
  ("This Quince is probably the fruit."); only a lower-case brand word may
  carry doubt there (owner, 2026-10-26, design 5.2 item 9).
- A passage's spoken mailbox grounds the written one only after a mail cue
  ("email help at casper dot com"); "it is help at casper dot com" does not.
- Two codes joined by "/" ("NIGHT15/NIGHT20") are read as one code; a
  joined form the passage spells differently ("U.S.D.A." for "usda",
  "GPT-4" for "gpt four") is removed; "casper.com.Use" (no space) does not
  ground "casper.com"; "suggests ... brand" also removes some business talk.

Pure logic plus read-only SQLite reads; no HTTP, no model. Sources: the
chat doc (docs/plans/2026-10-24-ask-chat-simplified.md, sections 3, 7.5, 8)
over the base design (docs/plans/2026-10-23-meta-ask-design.md 4.4, 5.1-5.6).

Passages. A `transcript_passage` is "inside an ad" (`in_ad`) when it
overlaps any `ad_reads` row of the same episode (its detected span or its
display span, any kind: a false "inside an ad" only hides a mention), or
when transcript text within +-90 s holds a sponsor cue, STRONG or WEAK
(`ad_rules.CUES`, imported, never copied). A `detected_ad` is always in an
ad. A held ad carries no brand: its label says "brand not identified",
whatever its `brands` field holds, and it never attributes a brand.

Labels (base 4.4, contract form): `[p7] ad · Saatva · <show> · "<title>" ·
<date> · 12:40–13:22`. No status word, and no channel word (the contract's
exact example has none; `brand_channel` stays on the Passage, unmerged).
Every label part and the passage text are cleaned; a brand, show, title or
date loses any spaced " · " or look-alike (a dot-like mark, any spaced
non-ASCII punctuation or symbol, or a letter that is a dot: ᐧ ꞏ ᆞ), a title
its '"', and both any "[p" or opening bracket before a passage id ("【p8");
the text also loses spaced non-ASCII dots (" · ", " • "), so whatever
look-alike stands for "[p8]" it cannot show a header with separators.

Ambiguous brands: `is_ambiguous_brand` uses an entry's own flag
({"ambiguous": bool}) when it has one, else membership in AMBIGUOUS_BRANDS
(the chat doc 7.4 names); nothing else. No system word list is read.

Checks (`check_answer`), on each item, in this order; the first failure
removes the item and records one short reason code. Text cleaning first:
NFKC; format, unassigned, private-use, surrogate and control characters,
and the combining marks NFKC leaves (Mn, Me), dropped.

- schema: text a string within 400 (paragraph) or 300 (uncertain)
  characters, both as sent and once cleaned, cites a list; at most 6 paragraphs and 3 uncertain items;
- cites (5.2 item 1): unknown ids dropped, an item with none left removed,
  at most 4 (paragraph) or 2 (uncertain) distinct cites;
- a combining mark (Mn, Me) that no cited passage's text, show or title has;
- a word with a letter or digit outside ASCII once accents are dropped
  (look-alike letters, small capitals, other scripts), or a symbol touching
  a word (not currency, not ° × ÷ ±), that no cited passage has;
- quotes (item 2): text between `"..."`, `“...”`, `„...“`, `«...»`,
  `‹...›`, `‘...’`, `'...'` (opening at a word start), `「...」`, `『...』`,
  `〝...〞`, `❝...❞`, `《...》`, `〈...〉` or backticks must appear in one cited
  passage's text, episode title or show, matched on whole words with case,
  whitespace, apostrophes and all punctuation folded (F9), so `.,;:!?` at
  either end of a quote never matter (F14). Outside a quote, `’` or `'`
  between letters, or after an `s`, is an apostrophe; modifier and spacing
  apostrophes (ʼ ´ ...) read as `'`. An unbalanced, stray
  or unknown quotation mark (any other Unicode Pi/Pf mark, any mark named
  QUOTATION, primes) fails. `'90s`, `'til` are apostrophes;
- names (shows, brands) are read in the whole text, quotes included; the
  word checks read the text outside the verified quotes in three readings
  (as written; punctuation inside words removed, "on-ly"; and a capital I
  next to a lower-case letter read as l, "OnIy"), with found names masked
  (a show called "We Hate Movies" is not a tone claim);
- status words (item 9 and F7, with up to two words inside "not ...
  identified"; also doubt words within 3 words of an ad word, any doubt
  phrase ("likely", "seems to be", "unclear whether") up to 5 words before
  a brand named by the case rules, a plain hedge up to 3 words after one,
  and a hedge such as "likely" or "suggests" up to 5 words before an ad
  word, or before "brand" outside uncertain items; a lower-case brand word
  next to doubt counts in an uncertain item only with ad wording, so "quince
  may be the fruit" stays), tone claims, counts (a time or a date, "the
  2026-09-12 episode", is no count unless a count's shape follows: "3/4 of
  the comedy shows") and absence (item 4; a bare "no" passes);
- grounded tokens (item 3) against the cited passages' text and metadata
  (show, title, date, times, unheld ads' brands): web addresses (a
  look-alike dot such as 。 or · between letters, "[.]" or "(dot)", and a
  spaced dot, "point" or "period" before a lower-case com, net or org,
  read as "."; "the dot com" stays words; whole host labels, and a
  mailbox "deals@" is part of the address: "@" spaced or written "( at )",
  "[at]", "{at}", "<at>" or "-at-", and a spoken "at" only after a mail cue
  ("email deals at x.com") and never before a web page's path), times,
  numbers of 2 or more digits, show and brand names with aliases (S9: a
  one-word brand display name counts only when capitalised, longer ones in
  any case; a show title, key and alias spellings, and a camel-case name
  split in two ("Better Help"), only when capitalised),
  code-like tokens (a digit and a letter, or 4 or more capitals; a run
  joined by any one symbol, or a symbol between single spaces, with no
  lower-case letter, "NIGHT-15", "NIGHT~15", "NIGHT - 15", is checked when a
  part is code-like or the joined form is code-like with a digit, so a
  short capital pair with no cue, "XL-PRO" or "NPR/PBS", is not; its words
  must stand in a row in one cited passage, so "365-night" stays a word) and the
  word after "code" (checked the same way) when a use cue comes first ("use", "enter", "apply",
  "with" ... "code") or in "code is X" / "code: X";
- a brand named (by the case rules, or as a web address's site name)
  citing only held ads, or backed only by a held ad; ad wording
  ("advertised", "sponsored", "ad", "brought to you by", "<brand> offer"...;
  "outside its ads" is not) about a brand that no cited unheld ad
  attributes (item 5): the wording is tied to the brand inside it, else the
  nearest brand (or a web address's site name) within 6 words and any
  brands listed with it, and a lower-case brand only right next to it; ad wording tied to any brand in
  an item that also cites a held ad is removed; a no-display name the wording
  binds to that no cited ad attributes passes only when a cited ad is the
  own read of a brand the item names in lower case or as a plain-word brand
  ("Hard Lessons ... Morgan Stanley" citing Hard's read) and says that name with
  the item's words on both sides ("indeed sponsor jobs" citing Indeed's read);
- plain mode, F6: every cite in an ad (a detected ad, or a transcript
  passage in an ad) and a talk verb (mentions, talks about, discusses,
  comes up, brings up, says about) removes the item, unless an ad word (ad,
  ads, advert, advertisement(s), advertises, advertised, advertising, sponsor,
  sponsors, sponsored, sponsorship, promo(s), promotion(s), commercial(s),
  read) stands anywhere
  outside a quote;
- brand mode, instead of F6: a cited ad that is not the brand's (F10);
  each paragraph has a section: an "ads" item must cite an unheld detected
  ad attributed to the brand (no brand given: never), a "mentions" item
  (and every uncertain item) a passage outside ads (F1, chat doc 3); a
  missing or unknown section is malformed.

Then F8 (outcome "nothing" drops every model item) and F4 (items never
outnumber the distinct passages they cite; later items go first).

All matching runs on NFKC text with format characters (zero-width, bidi,
tags) and leftover combining marks removed; word checks also drop accents
and fold case. Kept
items carry that cleaned text, so what is shown is what was checked.
"""
from __future__ import annotations

import functools
import json
import math
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .ad_rules import CUES, OPEN_CUE_RE, STRONG_CUE_RE, WEAK_CUE_RE  # noqa: F401  (CUES: the one source)

PARAGRAPHS_MAX, UNCERTAIN_MAX = 6, 3
PARAGRAPH_CHARS, UNCERTAIN_CHARS = 400, 300
PARAGRAPH_CITES, UNCERTAIN_CITES = 4, 2
CUE_WINDOW_S = 90.0

# ASK-UX-R1 (a) (UX words draft 2, 3d304b4c0): shown once, above the answer.
TONE_LINE = ("Ask can't tell whether people sound positive or negative. Here is what they "
             "said. Open a passage to judge its tone.")
PARTIAL_LINE = ("Ask searched the words of {n} of {m} episodes, so it may have missed some "
                "passages.")
NOTHING_FULL_LINE = ("No passage in this collection answers that. Search is by words, so a "
                     "passage worded differently can be missed.")
NOTHING_PART_LINE = ("We found no passage about this in the part of the collection we searched "
                     "({n} of {m} episodes).")
# LEDGER-LEAD3-ASKF-3: when no incomplete search has episode numbers.
PARTIAL_NO_COUNT_LINE = ("Ask searches by words and didn't reach every match, so it may have "
                         "missed some passages.")
NOTHING_PART_NO_COUNT_LINE = ("We found no passage about this in the part of the collection "
                              "we searched.")
# LEDGER-LEAD3-AC-5: partial, transcripts in scope and no transcript word search ran.
PARTIAL_ADS_ONLY_LINE = ("Ask searched the ads only, not what hosts said, so it may have missed "
                         "some passages.")
# ASK-CHECK-SMALL (UX 30cd554ed): every word search covered every episode and
# only a detected-ads search was cut.
PARTIAL_ADS_CUT_LINE = ("Ask searched what hosts said in every episode but didn't reach every "
                        "ad, so it may have missed some ads.")

# Chat doc 7.4: the names the brand button hides for (unless an entry's flag says otherwise).
AMBIGUOUS_BRANDS = ("Helix", "Apple", "Target", "Square", "Quince", "Mint", "Honey")
_AMBIGUOUS = frozenset(n.casefold() for n in AMBIGUOUS_BRANDS)


@dataclass(frozen=True)
class Passage:
    pid: str
    kind: str                      # "transcript_passage" | "detected_ad"
    in_ad: bool
    held: bool
    brands: tuple
    brand_channel: str | None      # "spoken" | "url" | "code"; never merged
    episode_id: str
    version_id: str | None
    evidence_id: str | None
    evidence_revision: int | None
    start_s: float
    end_s: float
    show: str
    episode_title: str
    pub_date: str
    fetch_date: str
    fetch_source: str
    disclosure_level: str          # "full" | "excerpt"
    text: str
    # Brands of the detected ads a transcript passage overlaps (near_ad_brands):
    # an in-ad passage backs ad wording only for its own spoken brand (B1).
    near_ad_brands: tuple = ()


@dataclass
class Item:
    text: str
    cites: list
    section: str | None = None   # brand-mode paragraphs: "ads" | "mentions"


@dataclass
class CheckResult:
    paragraphs: list = field(default_factory=list)
    uncertain: list = field(default_factory=list)
    dropped: int = 0
    reasons: list = field(default_factory=list)
    # ASK-RT2 item 7: one {"code", "text", "cites"} per reason, in order
    # (coverage_json only, never shown).
    removed_items: list = field(default_factory=list)


def removed_item(code, item) -> dict:
    """A removed item as coverage_json keeps it: its reason code, the text
    as the model sent it and its string cites (ASK-RT2 item 7)."""
    if isinstance(item, Item):
        text, cites = item.text, item.cites
    elif isinstance(item, dict):
        text, cites = item.get("text"), item.get("cites")
    else:
        text, cites = None, None
    return {"code": str(code), "text": text if isinstance(text, str) else "",
            "cites": [c for c in cites if isinstance(c, str)] if isinstance(cites, list) else []}


# ---------------------------------------------------------------- text folds

_MARKS = str.maketrans({"\u2122": " ", "\u2120": " ", "\u00ae": " ", "\u00a9": " ",
                        # modifier and spacing apostrophes read as '
                        "\u02bc": "'", "\u02bb": "'", "\u02bd": "'", "\u00b4": "'",
                        "\u0384": "'", "\u1fbf": "'", "\u055a": "'"})


_DROP = frozenset(("Cf", "Cn", "Co", "Cs", "Mn", "Me"))


def _clean(text) -> str:
    """NFKC with format (zero-width, bidi, tags), unassigned, private-use,
    surrogate and control characters removed (control whitespace and line
    or paragraph separators become a space), and the combining marks NFKC leaves (Mn, Me: invisible joiners,
    variation selectors) removed; ™ ℠ ® © become spaces first (NFKC would
    glue "TM" onto a name)."""
    text = (text if isinstance(text, str) else "").translate(_MARKS)
    text = unicodedata.normalize("NFKC", text)
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat in _DROP:
            continue
        if cat == "Cc" or cat in ("Zl", "Zp"):
            if ch.isspace():
                out.append(" ")
            continue
        out.append(ch)
    return "".join(out)


_GLUE_RE = re.compile(r"(?<=[^\W\d_])(?:[^\w\s'\u2019]|_)+(?=[^\W\d_])")


def _glue(text: str) -> str:
    """Punctuation or `_` inside a word removed ("on-ly" -> "only"), so a split word
    cannot hide from the word and name checks (read in both forms)."""
    return _GLUE_RE.sub("", text)


_CAPITAL_I_RE = re.compile(r"(?<=[a-z])I|I(?=[a-z])")


def _readings(text: str) -> list:
    """The forms the word and name checks read: as written, glued, and glued
    with a capital I next to a lower-case letter read as l ("OnIy")."""
    glued = _glue(text)
    return list(dict.fromkeys((text, glued, _CAPITAL_I_RE.sub("l", glued))))


_APOS = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "′": "'", "`": "'"})
_TOKEN_RE = re.compile(r"[^\W_]+(?:'[^\W_]+)*")


def _marks(text) -> set:
    """The combining marks (Mn, Me) left after NFKC, which _clean drops."""
    text = (text if isinstance(text, str) else "").translate(_MARKS)
    return {ch for ch in unicodedata.normalize("NFKC", text)
            if unicodedata.category(ch) in ("Mn", "Me")}


def _plain(text: str) -> str:
    """Cleaned, combining marks dropped, apostrophes unified (case kept)."""
    text = unicodedata.normalize("NFKD", _clean(text))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return unicodedata.normalize("NFC", text).translate(_APOS)


def _fold_token(tok: str) -> str:
    tok = tok.casefold()
    if tok.endswith("'s"):
        tok = tok[:-2]
    return tok.replace("'", "")


def _orig_tokens(text: str) -> list:
    """(original token, folded token) pairs; punctuation separates tokens."""
    out = []
    for m in _TOKEN_RE.finditer(_plain(text)):
        folded = _fold_token(m.group())
        if folded:
            out.append((m.group(), folded))
    return out


def _tokens(text) -> list:
    return [f for _, f in _orig_tokens(text if isinstance(text, str) else "")]


def _words(text: str) -> str:
    """Folded words joined by single spaces (the word checks' view)."""
    return " ".join(_tokens(text))


def _compact(text) -> str:
    return "".join(_tokens(text))


def _clock(s) -> str:
    try:
        s = float(s)
    except (TypeError, ValueError, OverflowError):
        return ""
    if not math.isfinite(s) or s < 0:
        return ""
    s = int(round(s))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _brand_list(p) -> list:
    brands = p.brands
    if isinstance(brands, str):
        return [brands]
    if brands is None:
        return []
    if not isinstance(brands, (list, tuple, set, frozenset)):
        raise ValueError("Passage.brands must be a string or a sequence of strings")
    return list(brands)


def _passages(passages) -> list:
    """A caller's passages as a list; anything but Passage objects is a ValueError."""
    if isinstance(passages, (str, bytes, dict)) or not hasattr(passages, "__iter__"):
        raise ValueError("passages must be an iterable of Passage")
    out = list(passages)
    if not all(isinstance(p, Passage) for p in out):
        raise ValueError("passages must be an iterable of Passage")
    return out


def _one_line(value) -> str:
    """Cleaned (no lone surrogate, control or format character) on one line."""
    return " ".join(_clean(str(value)).split()) if value is not None else ""


# ---------------------------------------------------------------- labels

# A character with a space or an end on either side: dropped when dot-like
# (· ・ ∙ • ⋅ ...), any non-ASCII punctuation or symbol (᛫ ་ ...), or a
# letter that is a dot (ᐧ ꞏ ᆞ).
_FORGED_SEP_RE = re.compile(r"(?<!\S)\S|\S(?!\S)")
_FORGED_PID_RE = re.compile(r"\[(?=p)", re.I)
# Any opening bracket (【 ( < { ⟦ ...) right before a passage id ("p8").
_OPEN_PID_RE = re.compile(r"[^\w\s](?=\s*p\d)", re.I)


@functools.lru_cache(maxsize=4096)
def _sep_like(ch: str) -> bool:
    if ord(ch) < 128:
        return not ch.isalnum() and _dot_like(ch)
    return unicodedata.category(ch)[0] in "PS" or _dot_sep(ch)


@functools.lru_cache(maxsize=4096)
def _dot_sep(ch: str) -> bool:
    """A non-ASCII dot (· ・ ᐧ ꞏ ᆞ ...): the label separator or a look-alike."""
    if ord(ch) < 128:
        return False
    name = unicodedata.name(ch, "")
    return _dot_like(ch) or "ARAEA" in name or (" DOT" in name and " WITH " not in name)


def _no_pid(text: str) -> str:
    """No "[p", and no opening bracket before a passage id, so text cannot
    show a passage header."""
    text = _OPEN_PID_RE.sub(lambda m: "" if m.group() == "<" or
                            unicodedata.category(m.group()) == "Ps" else m.group(), text)
    return _FORGED_PID_RE.sub("", text)


def _field(value, *, title=False) -> str:
    """A brand, show, title or date on one line with no " · " separator or
    look-alike and no "[p" (a title also no '"'), so it cannot pose as
    another label field or passage."""
    text = _one_line(value)
    if title:
        text = text.replace('"', " ")
    text = _FORGED_SEP_RE.sub(lambda m: " " if _sep_like(m.group()) else m.group(), text)
    return " ".join(_no_pid(text).split())


def label_line(p: Passage) -> str:
    """The header the model sees; no status word, no channel word."""
    if p.kind == "detected_ad":
        brands = [_field(b) for b in _brand_list(p) if _field(b)]
        parts = ["ad", ", ".join(brands) if brands and not p.held else "brand not identified"]
    else:
        parts = ["transcript passage", "inside an ad" if p.in_ad else "mention"]
    if _field(p.show):
        parts.append(_field(p.show))
    if _field(p.episode_title, title=True):
        parts.append(f'"{_field(p.episode_title, title=True)}"')
    if _field(p.pub_date):
        parts.append(_field(p.pub_date))
    start, end = _clock(p.start_s), _clock(p.end_s)
    if start and end:
        parts.append(f"{start}–{end}")
    return f"[{_one_line(p.pid)}] " + " · ".join(parts)


def render_passage(p: Passage, max_bytes: int = 1024) -> str:
    """Label line, newline, then the text cut at a word boundary so the
    whole stays within max_bytes (UTF-8)."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative int")
    if p.text is not None and not isinstance(p.text, str):
        raise ValueError("Passage.text must be a string")
    label = label_line(p)
    room = max_bytes - len(label.encode("utf-8")) - 1
    if room < 0:
        return label.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")
    # No "[p" and no spaced dot separator in the text either, so it cannot
    # show an inline fake header, whatever look-alike stands for "[p8]".
    text = _FORGED_SEP_RE.sub(lambda m: " " if _dot_sep(m.group()) else m.group(),
                              _no_pid(_clean(p.text)))
    text = " ".join(text.split())
    data = text.encode("utf-8")
    if len(data) > room:
        head = data[:room].decode("utf-8", "ignore")
        if not text[len(head):len(head) + 1].isspace():
            head = head.rsplit(" ", 1)[0] if " " in head else ""
        text = head.rstrip()
    return label + "\n" + text


# ---------------------------------------------------------------- in an ad

def _segments(conn: sqlite3.Connection, episode_id, version_id, _depth=0) -> list:
    if version_id is None:
        try:
            row = conn.execute("SELECT transcript_version_id, alias_of FROM episodes WHERE id=?",
                               (episode_id,)).fetchone()
        except sqlite3.OperationalError:
            row = conn.execute("SELECT transcript_version_id, NULL FROM episodes WHERE id=?",
                               (episode_id,)).fetchone()
        version_id = row[0] if row else None
        if version_id is None:
            # An alias episode reads its target's current transcript
            # (ops.transcript_segments).
            alias = row[1] if row else None
            return _segments(conn, alias, None, _depth + 1) if alias and _depth < 3 else []
    if isinstance(version_id, str) and _ASCII_DIGITS_RE.fullmatch(version_id.strip()) \
            and int(version_id) <= _SQL_INT_MAX:
        version_id = int(version_id)
    row = conn.execute("SELECT payload FROM source_versions WHERE id=?", (version_id,)).fetchone()
    try:
        payload = json.loads(row[0]) if row else None
    except (TypeError, ValueError):
        return []
    segs = payload.get("segments") if isinstance(payload, dict) else None
    return segs if isinstance(segs, list) else []


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:   # an int too large for a float
        return None


_SQL_INT_MAX = 2 ** 63 - 1
_ASCII_DIGITS_RE = re.compile(r"[0-9]+")


def _id(value, what, *, optional=False):
    """An episode or version id for SQL: a str, or an int SQLite can hold
    (None when optional)."""
    if (value is None and optional) or isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool) and \
            -_SQL_INT_MAX - 1 <= value <= _SQL_INT_MAX:
        return value
    raise ValueError(f"{what} must be a string or an int within SQLite's range")


def sponsor_cue_near(conn, episode_id, version_id, start_s, end_s, window_s=CUE_WINDOW_S) -> bool:
    """A STRONG or WEAK sponsor cue (ad_rules) in the transcript segments
    within window_s of [start_s, end_s]."""
    start, end = _num(start_s), _num(end_s)
    if start is None or end is None:
        raise ValueError("start_s and end_s must be finite numbers")
    _id(episode_id, "episode_id")
    _id(version_id, "version_id", optional=True)
    lo, hi = start - window_s, end + window_s
    texts = []
    for seg in _segments(conn, episode_id, version_id):
        if not isinstance(seg, dict) or not isinstance(seg.get("text"), str):
            continue
        s, e = _num(seg.get("start")), _num(seg.get("end"))
        if s is None:
            continue
        e = s if e is None else e
        if s <= hi and e >= lo:
            texts.append(seg["text"])
    joined = _clean(" ".join(texts))
    return bool(STRONG_CUE_RE.search(joined) or WEAK_CUE_RE.search(joined))


def ad_overlap(conn, episode_id, start_s, end_s) -> bool:
    """Any ad_reads row of the episode whose span or display span touches
    [start_s, end_s]."""
    _id(episode_id, "episode_id")
    start_s, end_s = _num(start_s), _num(end_s)
    if start_s is None or end_s is None:
        raise ValueError("start_s and end_s must be finite numbers")
    row = conn.execute(
        "SELECT 1 FROM ad_reads WHERE episode_id=? AND ("
        " (start_s <= ? AND end_s >= ?) OR (display_start_s <= ? AND display_end_s >= ?))"
        " LIMIT 1", (episode_id, end_s, start_s, end_s, start_s)).fetchone()
    return row is not None


def near_ad_brands(conn, episode_id, start_s, end_s) -> tuple:
    """The attributed brands (confirmed or suggested) of the ad_reads rows
    whose span or display span touches [start_s, end_s], sorted."""
    _id(episode_id, "episode_id")
    start_s, end_s = _num(start_s), _num(end_s)
    if start_s is None or end_s is None:
        raise ValueError("start_s and end_s must be finite numbers")
    rows = conn.execute(
        "SELECT DISTINCT b.brand FROM ad_reads r JOIN brand_assocs b ON b.evidence_id = r.evidence_id"
        " WHERE r.episode_id=? AND b.basis IN ('confirmed', 'suggested') AND ("
        " (r.start_s <= ? AND r.end_s >= ?) OR (r.display_start_s <= ? AND r.display_end_s >= ?))",
        (episode_id, end_s, start_s, end_s, start_s)).fetchall()
    return tuple(sorted({r[0].strip() for r in rows if isinstance(r[0], str) and r[0].strip()}))


def transcript_in_ad(conn, episode_id, version_id, start_s, end_s) -> bool:
    """A transcript passage's `in_ad`: an ad_reads overlap or a sponsor cue
    within 90 s. Brand mode's `outside_ads` keeps passages where this is False."""
    return ad_overlap(conn, episode_id, start_s, end_s) or \
        sponsor_cue_near(conn, episode_id, version_id, start_s, end_s)


# ---------------------------------------------------------------- names

_SMALL = frozenset({"the", "of", "and", "a", "an", "in", "on", "for", "to", "at", "by", "with"})


def is_ambiguous_brand(display_name, entry) -> bool:
    """The entry's own flag ({"ambiguous": bool}) when present; otherwise
    membership in AMBIGUOUS_BRANDS (case-insensitive)."""
    if isinstance(entry, dict) and isinstance(entry.get("ambiguous"), bool):
        return entry["ambiguous"]
    name = display_name if isinstance(display_name, str) else ""
    return " ".join(name.split()).casefold() in _AMBIGUOUS


def _one_word(tup: tuple) -> bool:
    """One word, small words aside ("The Daily")."""
    return len([t for t in tup if t not in _SMALL]) == 1


def _ascii_compact(text) -> str:
    return "".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def plain_alias_words(con, words, min_ads: int = 3) -> dict:
    """{"plain", "capital_only"} (sorted) for one workspace: its
    ad_brand_aliases keys with no display name made only of `words` (plain
    English words; a one-word "plain" key also a regular plural, -s or -es, of
    one that is no word of a longer alias key), by their attributed detected ads (ad_reads with a
    brand_assocs basis of confirmed or suggested). "plain": one word with
    fewer than min_ads ads; "capital_only": one word with min_ads or more,
    or a phrase with fewer (as its lower-case key). A phrase with min_ads or
    more is in neither. The rules of scripts/gen_plain_alias_words.py and of
    Ask's name table (ask._plain_words); a missing table raises sqlite3.Error."""
    rows = con.execute("SELECT raw_key, canonical_key FROM ad_brand_aliases"
                       " WHERE display IS NULL").fetchall()
    ads: dict = {}
    for brand, n in con.execute(
            "SELECT b.brand, COUNT(DISTINCT r.evidence_id) FROM brand_assocs b"
            " JOIN ad_reads r ON r.evidence_id = b.evidence_id"
            " WHERE b.basis IN ('confirmed', 'suggested') GROUP BY b.brand"):
        ads[_ascii_compact(brand)] = ads.get(_ascii_compact(brand), 0) + n
    advertising = {k for k, n in ads.items() if n >= min_ads}
    plain, kept = set(), set()
    # Words of the workspace's longer alias keys ("indeed sponsored jobs").
    longer = {t for r in con.execute("SELECT raw_key, canonical_key FROM ad_brand_aliases")
              for key in r for toks in [re.findall(r"[a-z0-9]+", str(key or "").lower())]
              if len(toks) > 1 for t in toks}

    def plural(t) -> bool:
        return t.endswith("s") and t not in longer \
            and (t[:-1] in words or t.endswith("es") and t[:-2] in words)

    for raw, canonical in rows:
        advertiser = _ascii_compact(raw) in advertising or _ascii_compact(canonical) in advertising
        for key in (raw, canonical):
            toks = re.findall(r"[a-z0-9]+", str(key or "").lower())
            if not toks or not all(t in words for t in toks):
                # One word with fewer ads that is a regular plural of a listed
                # word is plain too ("maps"; ASK-CHECK-WORDS R1 round 4). Not
                # an advertiser, a phrase, or a word of a longer alias key
                # ("jobs" of "indeed sponsored jobs"): those stay checked in
                # any case.
                if len(toks) == 1 and not advertiser and plural(toks[0]):
                    plain.add(toks[0])
                continue
            if len(toks) == 1:
                (kept if advertiser else plain).add(toks[0])
            elif not advertiser:
                kept.add(str(key).strip().lower())
    plain -= kept
    return {"plain": sorted(plain), "capital_only": sorted(kept)}


# A sentence's first word followed by a comma or by one of these reads as an
# ordinary word, not a plain-word brand ("Yes, I said it", "Book your stay");
# so do up to three words after "code" ("promo code Good Life").
_ORDINARY_NEXT = frozenset({"your", "the", "a", "an", "i", "it", "its", "you", "our", "my", "us",
                            "me", "we", "that", "this", "these", "those", "them", "him", "her",
                            "his", "their", "some", "all", "every", "more"})


def _ordinary_words(text: str) -> set:
    """Indexes (as in _orig_tokens) of the words that start a sentence and
    are followed by a comma or an _ORDINARY_NEXT word, and of the (up to
    three) words after "code" or "codes", "is" aside."""
    plain = _plain(text)
    spans = [(m.start(), m.end(), _fold_token(m.group())) for m in _TOKEN_RE.finditer(plain)]
    spans = [x for x in spans if x[2]]
    out = set()
    for i, (_a, _b, f) in enumerate(spans):
        if f in ("code", "codes"):
            k = i + 1 + (i + 1 < len(spans) and spans[i + 1][2] in ("is", "was"))
            out.update(range(k, min(k + 3, len(spans))))
    for i, (a, b, _f) in enumerate(spans):
        before = plain[:a].rstrip()
        if before and before[-1] not in ".!?:;\"\u201c\u201d(":
            continue
        nxt = spans[i + 1] if i + 1 < len(spans) else None
        if plain[b:].lstrip().startswith(",") or (nxt and nxt[2] in _ORDINARY_NEXT
                                                  and not plain[b:nxt[0]].strip()):
            out.add(i)
    return out


def _sentence_starts(text: str) -> set:
    """Indexes (as in _orig_tokens) of the words that start a sentence."""
    plain = _plain(text)
    out, i = set(), 0
    for m in _TOKEN_RE.finditer(plain):
        if not _fold_token(m.group()):
            continue
        before = plain[:m.start()].rstrip()
        if not before or before[-1] in ".!?:;\"“”(":
            out.add(i)
        i += 1
    return out


def _said_mid_sentence(word: str, cited: list) -> bool:
    """A cited passage says the (folded) word capitalised other than at a
    sentence start ("Thanks, Dan."; not "Dan here.", "Therapy can help")."""
    for p in cited:
        starts = _sentence_starts(p.text)
        if any(f == word and i not in starts and any(c.isupper() for c in o)
               for i, (o, f) in enumerate(_orig_tokens(p.text))):
            return True
    return False


class NameTable:
    """Brands and show titles. Display names and show titles are looked for
    in an item by the case rules of find(); display-list keys and aliases (and
    `aliases`, e.g. the workspace's ad_brand_aliases raw_key -> display) are
    looked for only when capitalised. Every spelling grounds its name.
    `plain_aliases` are looked for only when find() is given them as active
    (ASK-CHECK-WORDS: an item with ad wording, or citing their ad)."""

    def __init__(self):
        self._spell: dict = {}     # group key -> set of token tuples
        self._lookup: dict = {}    # token tuple (any spelling) -> group keys
        self._index: dict = {}     # token tuple looked for -> [group keys, {brand|show|alias|split}]
        self._cond: dict = {}      # token tuple looked for when active -> {group key: {cap|brand}}
        self._lengths: dict = {}   # first token -> the lengths of the forms that start with it
        self._display: dict = {}   # brand key -> its display-name forms (spoken_brands)
        self._shown: dict = {}     # raw brand key (folded) -> its display names (display_for)
        self.weak: frozenset = frozenset()   # brand keys with no display name, never a show

    def _add(self, key, spelling, *, name: bool, spoken: bool = True):
        """name: a display name or show title (S9 case rule); else a key or
        alias spelling, looked for only when capitalised. spoken=False: a
        brand name by the display-name case rules that spoken_brands()
        never counts (any_case_aliases)."""
        tup = tuple(_tokens(spelling))
        if not tup:
            return
        mode = key[0] if name else "alias"
        forms = {tup: mode}
        if len(tup) > 1:
            forms[("".join(tup),)] = mode
        # "BetterHelp" heard as "Better Help": the split form, capitalised only.
        split = tuple(_tokens(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", spelling)))
        if len(split) > len(tup):
            forms.setdefault(split, "split")
        for form, form_mode in forms.items():
            self._spell.setdefault(key, set()).add(form)
            self._lookup.setdefault(form, set()).add(key)
            entry = self._index.setdefault(form, [set(), set(), set()])
            entry[0].add(key)
            entry[1].add(form_mode)
            if name and form == tup:
                # The display spelling as written ("lululemon") counts as capitalised.
                entry[2].add(tuple(o for o, _ in _orig_tokens(spelling)))
            if name and spoken and key[0] == "brand" and form_mode != "split":
                self._display.setdefault(key, set()).add(form)
            self._lengths.setdefault(form[0], set()).add(len(form))

    def _add_cond(self, key, spelling):
        """A spelling looked for only when its key is active: one word only
        when capitalised, a phrase in any case (S9). It always grounds its
        name and resolves to its key (keys_for)."""
        tup = tuple(_tokens(spelling))
        if not tup:
            return
        self._spell.setdefault(key, set()).add(tup)
        self._lookup.setdefault(tup, set()).add(key)
        mode = "cap" if len(tup) == 1 else "brand"
        self._cond.setdefault(tup, {}).setdefault(key, set()).add(mode)
        self._lengths.setdefault(tup[0], set()).add(len(tup))

    def _entry(self, tup, active):
        """(group keys, modes, display spellings as written) of a form, with
        the conditional spellings of the active keys (True: every key)."""
        entry = self._index.get(tup)
        keys, modes = (set(entry[0]), set(entry[1])) if entry else (set(), set())
        for key, more in (self._cond.get(tup, {}).items() if active else ()):
            if active is True or key in active:
                keys.add(key)
                modes |= more
        return keys, modes, entry[2] if entry else ()

    @staticmethod
    def _need_caps(tup, modes, any_case) -> bool:
        if "brand" in modes:
            return _one_word(tup) and not any_case
        if "show" in modes or modes <= {"cap"}:
            return True
        return not any_case

    @classmethod
    def load(cls, display_brands_path, show_titles: Iterable[str], *, aliases=None,
             capital_aliases=None, any_case_aliases=None, plain_aliases=None):
        """capital_aliases: spelling -> group name with no display name (the
        workspace's ad_brand_aliases rows without one): both are spellings
        looked for only when capitalised, never a display name.
        any_case_aliases: the same, but the group name is looked for by a
        display name's case rules, as `aliases` does with a display, without
        counting as spoken (spoken_brands, B1).
        plain_aliases: the same rows made only of plain words (ASK-CHECK-WORDS):
        both spellings looked for only when find() makes their key active,
        a one-word spelling only when capitalised, a phrase in any case."""
        table = cls()
        strong, weak = set(), set()
        try:
            doc = json.loads(Path(display_brands_path).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            doc = {}
        doc = doc if isinstance(doc, dict) else {}
        pairs = []
        for part in ("brands", "aliases"):
            value = doc.get(part)
            pairs += list(value.items()) if isinstance(value, dict) else []
        pairs += list(aliases.items()) if isinstance(aliases, dict) else []
        for k, v in pairs:
            display = v.get("display") if isinstance(v, dict) else v
            if not (isinstance(k, str) and isinstance(display, str) and display.strip()):
                continue
            key = ("brand", _compact(display))
            strong.add(key)
            table._add(key, display, name=True)
            table._add(key, k, name=False)
            table._shown.setdefault(_key_fold(k), set()).add(" ".join(display.split()))
        def group_key(k, group):
            # A key the display list maps to one display name is that brand
            # ("contentfull" -> Contentful): one name group, so B1 sees one
            # brand (ASK-CHECK-WORDS R1 round 4).
            shown = table._shown.get(_key_fold(k), set()) | table._shown.get(_key_fold(group), set())
            return ("brand", _compact(next(iter(shown)) if len(shown) == 1 else group))

        for k, group in (capital_aliases.items() if isinstance(capital_aliases, dict) else ()):
            if isinstance(k, str) and isinstance(group, str) and _compact(group):
                key = group_key(k, group)
                weak.add(key)
                table._add(key, k, name=False)
                table._add(key, group, name=False)
        for k, group in (any_case_aliases.items() if isinstance(any_case_aliases, dict) else ()):
            if isinstance(k, str) and isinstance(group, str) and _compact(group):
                key = group_key(k, group)
                weak.add(key)
                table._add(key, group, name=True, spoken=False)
                table._add(key, k, name=False)
        for k, group in (plain_aliases.items() if isinstance(plain_aliases, dict) else ()):
            if isinstance(k, str) and isinstance(group, str) and _compact(group):
                key = group_key(k, group)
                weak.add(key)
                table._add_cond(key, k)
                table._add_cond(key, group)
        if isinstance(show_titles, str):
            titles = [show_titles]
        elif show_titles is None:
            titles = []
        elif isinstance(show_titles, (bytes, dict)) or not hasattr(show_titles, "__iter__"):
            raise ValueError("show_titles must be a string or an iterable of strings")
        else:
            titles = list(show_titles)
        for title in titles:
            if isinstance(title, str) and title.strip():
                table._add(("show", _compact(title)), title, name=True)
        table.weak = frozenset(weak - strong)
        return table

    def display_for(self, key) -> str | None:
        """ASK-DISPLAY-NAME: the display name a raw brand key shows as, when
        the display list and aliases give it exactly one and it differs from
        the key beyond case ("chim" -> "Chime"; "target" stays "target")."""
        if not isinstance(key, str):
            return None
        shown = self._shown.get(_key_fold(key), set())
        if len(shown) != 1:
            return None
        (display,) = shown
        return display if display.casefold() != _key_fold(key) else None

    def keys_for(self, name) -> set:
        tup = tuple(_tokens(name))
        keys = set(self._lookup.get(tup, ())) | set(self._lookup.get(("".join(tup),), ()))
        return keys or {("name", "".join(tup))}

    def spellings(self, name) -> set:
        tup = tuple(_tokens(name))
        out = {tup, ("".join(tup),)} if tup else set()
        for key in self.keys_for(name):
            out |= self._spell.get(key, set())
        return out

    def find(self, text: str, *, any_case: bool = False, active=None, soft=None, cond=None,
             own=(), ours=(), keep=None) -> list:
        """(token tuple, group keys) of the names in text (in each of its
        readings), longest first. Case rules: a one-word brand display name
        counts only when capitalised, longer ones in any case (S9); a show
        title, and a key or alias spelling, only when capitalised. any_case:
        brands in any case (the held-ad and adjacent ad-wording checks).
        active: the plain_aliases keys looked for too (True: all); such a
        one-word spelling that reads as an ordinary word ("Yes, ...", "Book
        your stay", "promo code Good Life") is not one. soft, a set: gets the
        forms found only as an active phrase in lower case; cond, a set: the
        forms found only as active plain_aliases spellings. A one-word such
        spelling inside a longer capitalised name or a contraction ("General
        Dynamics", "We'll") is not one either, unless its key is in `own` (a
        cited ad's brand); `ours`: the words of the cited ads' brands, which
        make a longer name ("Aura House" next to Aura's ad). keep(form, keys,
        initial): whether such a spelling is a name; `initial` is a one-word
        spelling at a sentence start."""
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        found = []
        # Plain-word names are not looked for in the reading with a capital I
        # read as l ("The Rest Is Classified" -> "the rest ls Classified").
        as_l = _CAPITAL_I_RE.sub("l", _glue(text))
        as_l = None if as_l in (text, _glue(text)) else as_l
        for variant in _readings(text):
            toks = _orig_tokens(variant)
            active_here = None if variant == as_l else active
            starts = _ordinary_words(variant) if active_here else ()
            longer = self._longer_name(variant, ours) if active_here else ()
            initial = _sentence_starts(variant) if active_here and keep else ()
            i = 0
            while i < len(toks):
                lengths = self._lengths.get(toks[i][1], ())
                for n in sorted((n for n in lengths if n <= len(toks) - i), reverse=True):
                    tup = tuple(f for _, f in toks[i:i + n])
                    keys, modes, written = self._entry(tup, active_here)
                    if not keys:
                        continue
                    caps = all(o[:1].isdigit() or any(c.isupper() for c in o)
                               for o, f in toks[i:i + n] if f not in _SMALL) \
                        or tuple(o for o, _ in toks[i:i + n]) in written
                    if self._need_caps(tup, modes, any_case) and not caps:
                        continue
                    base = self._index.get(tup)
                    if not (base and (caps or not self._need_caps(tup, base[1], any_case))):
                        # Found only as an active plain_aliases spelling.
                        if i in starts and n == 1:
                            continue
                        # Nor inside a longer capitalised name ("General
                        # Dynamics", "Golden Crest Medals", "Alaskan Company")
                        # or a contraction ("We'll" -> "well"), unless it is a
                        # cited ad's brand (ASK-CHECK-WORDS R1 HIGH-1).
                        if n == 1 and not (keys & set(own)):
                            if "'" in toks[i][0].removesuffix("'s") or i in longer:
                                continue
                        if keep is not None and not keep(tup, keys, n == 1 and i in initial):
                            continue
                        if cond is not None:
                            cond.add(tup)
                        if soft is not None and not caps:
                            soft.add(tup)
                    if all(t != tup for t, _ in found):
                        found.append((tup, keys))
                    i += n
                    break
                else:
                    i += 1
        return found

    def _longer_name(self, text: str, ours=()) -> set:
        """Indexes (as in _orig_tokens) of the words next to a
        capitalised word that is not a one-word plain name, with only spaces
        between ("General Dynamics", "Golden Crest Medals",
        "Un-believeable Company"; not "Asana Company", not "Asana. Great")."""
        plain = _plain(text)
        spans = [(m.start(), m.end(), m.group()) for m in _TOKEN_RE.finditer(plain)
                 if _fold_token(m.group())]
        one = {t[0] for t in self._cond if len(t) == 1}
        out = set()
        for i in range(len(spans)):
            for j, step in ((i - 1, -1), (i + 1, 1)):
                if not 0 <= j < len(spans):
                    continue
                a, b = (spans[j][1], spans[i][0]) if step < 0 else (spans[i][1], spans[j][0])
                if plain[a:b].strip():
                    continue
                k = j          # a hyphenated word reads from its first part
                while k > 0 and plain[spans[k - 1][1]:spans[k][0]] == "-":
                    k -= 1
                if any(c.isupper() for c in spans[k][2]) and (_fold_token(spans[j][2]) not in one
                                                              or _fold_token(spans[j][2]) in ours):
                    out.add(i)
        return out

    def modes(self, tup, active=None) -> set:
        """How a form found by find() with `active` is looked for."""
        return self._entry(tup, active)[1]

    def spoken_brands(self, text: str) -> set:
        """Brand keys whose display name (not an alias or split spelling) the
        text speaks, by find()'s case rules."""
        return {k for tup, keys in self.find(text) for k in keys
                if k[0] == "brand" and tup in self._display.get(k, ())}

    def grounded(self, name, passages) -> bool:
        """Some spelling of the name, as whole words, in a passage's text,
        show, title or (unheld ads) attributed brands."""
        passages = _passages(passages)
        spellings = [s for s in self.spellings(name) if s]
        wanted = [" " + " ".join(s) + " " for s in spellings]
        for p in passages:
            for padded in _name_hays(p, self):
                if any(w in padded for w in wanted):
                    return True
        return False


def _key_fold(key: str) -> str:
    return " ".join(_clean(key).split()).casefold()


def _ad_brands(p: Passage) -> list:
    if p.kind != "detected_ad" or p.held:
        return []
    return [b for b in _brand_list(p) if isinstance(b, str) and b.strip()]


@functools.lru_cache(maxsize=64)
def _padded_cached(text: str) -> str:
    return " " + " ".join(_tokens(text)) + " "


def _padded(text) -> str:
    """A passage field's folded words, space-padded; cached, so many quotes
    and names in one answer tokenise each passage once."""
    return _padded_cached(text if isinstance(text, str) else "")


def _name_hays(p: Passage, table: NameTable) -> list:
    hays = [_padded(p.text), _padded(p.show), _padded(p.episode_title)]
    for b in _ad_brands(p):
        hays.extend(" " + " ".join(s) + " " for s in table.spellings(b))
    return hays


# ---------------------------------------------------------------- quotes

# Paired marks: opener -> its closers. Every other quotation-like mark
# (Unicode Pi/Pf, primes, ditto and the like) is unbalanced wherever it
# stands, so no unknown mark can wrap an unchecked quote.
_OPENERS = {'"': '"', "“": "”", "„": "“”", "‟": "”",
            "«": "»", "‹": "›", "「": "」", "『": "』",
            "〝": "〞〟", "❝": "❞", "《": "》",
            "〈": "〉", "⹂": "“”", "`": "`",
            "‘": "’'", "‚": "‘’", "‛": "’", "❛": "❜"}
_SINGLE_CLOSERS = "’'‘❜"
_QUOTE_LIKE = set("′″‴‵‶‷˝̋׳״〃〞"
                  "〟❞❜》〉」』”»›")


# '90s, '24, 'til, 'em: a leading apostrophe, not an opening quote.
_SHORT_FORM_RE = re.compile(r"(?:\d\ds?|til|em|cause|cuz|bout|round|n)(?![A-Za-z0-9])", re.I)


@functools.lru_cache(maxsize=4096)
def _quote_like(ch: str) -> bool:
    return ch in _QUOTE_LIKE or unicodedata.category(ch) in ("Pi", "Pf") \
        or "QUOTATION" in unicodedata.name(ch, "")


def _closes_later(s: str, k: int) -> bool:
    """A later `'` or `’` that would close a single quotation."""
    n = len(s)
    return any(s[j] in ("'", "\u2019") and s[j - 1].isalnum() and (j + 1 == n or not s[j + 1].isalnum())
               for j in range(k + 1, n))


def _quotes(s: str):
    """[(start, end)] spans of quotations (marks included), or None when a
    quotation mark is unbalanced, stray or unknown. Outside a quotation,
    `'` or `’` between letters or digits, or after an `s` before a non-letter,
    is an apostrophe; at a word start it opens a quotation. Inside a single
    quotation, `'` or `’` closes it unless it stands between letters."""
    spans, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        prev = s[i - 1] if i else ""
        nxt = s[i + 1] if i + 1 < n else ""
        if c in ("'", "\u2019"):
            short = not prev.isalnum() and _SHORT_FORM_RE.match(s, i + 1) \
                and not _closes_later(s, i + 1)
            if (prev.isalnum() and nxt.isalnum()) or (prev in ("s", "S") and not nxt.isalnum()) \
                    or short:
                i += 1
                continue
            if prev.isalnum() or not nxt.isalnum():
                return None
            closers = "'’"
        elif c in _OPENERS:
            closers = _OPENERS[c]
        elif _quote_like(c):
            return None
        else:
            i += 1
            continue
        j = i + 1
        while j < n:
            ch = s[j]
            if ch in closers:
                inner = ch in _SINGLE_CLOSERS and s[j - 1].isalnum() and j + 1 < n \
                    and s[j + 1].isalnum()
                if not inner:
                    break
            j += 1
        if j >= n:
            return None
        spans.append((i, j + 1))
        i = j + 1
    return spans


_EDGE = ".,;:!?"


def _quote_ok(quote: str, cited: list) -> bool:
    # The token fold ignores all punctuation (F9), so `.,;:!?` at either end
    # of a quote never matter (F14).
    needle = _tokens(quote)
    if not needle:
        return False
    wanted = " " + " ".join(needle) + " "
    return any(wanted in _padded(t) for p in cited for t in (p.text, p.episode_title, p.show))


# ---------------------------------------------------------------- word checks

_NUM_WORDS = ("zero|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
              "fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|"
              "hundred|hundreds|thousands|dozen|dozens|several|multiple|numerous|many|most|all|"
              "every|various|countless|few|a few|a couple|lots|a lot|a number|a handful|plenty|"
              "each")
# Up to two words may stand between a count and its noun ("3 recent
# episodes"), but not these ("$50 off the ad" is no count), and not after
# "most" ("the most recent episode").
_COUNT_GAP = (r"(?:(?<!\bmost )(?!(?:the|a|an|off|in|on|at|for|to|of|with|from|by|and|or|per|percent|"
              r"dollars?|cents?|bucks)\b)\w+ ){0,2}")
COUNT_RE = re.compile(
    rf"\b(?:\d+|{_NUM_WORDS}) (?:of )?(?:(?:the|these|those) )?{_COUNT_GAP}"
    r"(?:(?:zz[bl]\d+|zzshow) )?(?:episodes?|shows?|podcasts?|passages?|sponsors?|advertisers?|brands?|ads?|"
    r"mentions?|segments?|clips?|reads?)\b"
    rf"|\b(?:mention\w*|said|named|appear\w*|comes? up|came up|cited|advertis\w*|sponsor\w*|"
    rf"plug(?:s|ged|ging)?|aired|airs|airing|ran|runs|played) (?:\w+ ){{0,2}}"
    rf"(?:(?:\d+|{_NUM_WORDS}) times|twice|thrice)\b"
    r"|\b(?:several|many|multiple|numerous|various|most|all|every|few|lots of|dozens of) "
    r"(?:of (?:the )?)?(?:hosts?|guests?)\b"
    r"|\bjust one (?:\w+ )?(?:shows?|episodes?|podcasts?|hosts?|passages?)\b"
    r"|\b(?:frequent|frequently|often|rarely|seldom|repeatedly|most (?:frequent|common|often|"
    r"popular)|always|usually|typically|each of|half (?:of )?the)\b")
# A date ("2026-09-12", "9/12", "September 12, 2026", "12 Sept.") is no
# count, unless a plural count noun follows ("Sept 3 shows" stays a count).
_MONTH = (r"(?:January|February|March|April|May|June|July|August|September|October|November|"
          r"December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)(?![A-Za-z])\.?")
_DATE_RE = re.compile(
    rf"(?:\b\d{{4}}-\d{{1,2}}-\d{{1,2}}\b|\b\d{{1,2}}/\d{{1,2}}(?:/\d{{2,4}})?\b"
    rf"|\b{_MONTH} \d{{1,2}}(?:st|nd|rd|th)?\b(?:,? \d{{4}}\b)?"
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)? (?:of )?{_MONTH}(?:,? \d{{4}}\b)?)"
    # The count's own shape ("3/4 of the comedy shows", "May 4 of the late
    # night shows") stays a count (postcap-r1 H3).
    r"(?! (?:of (?:(?:the|these|those) )?(?:[\w'\u2019]+ ){0,3}|(?!(?:the|these|those|a|an)\b)"
    r"(?:[\w'\u2019]+ ){0,2})(?:episodes|shows|podcasts|passages|"
    r"sponsors|advertisers|brands|ads|mentions|segments|clips|reads|times)\b)")
ABSENCE_RE = re.compile(
    r"\b(?:no other|none|never|only|not any|nobody|no one|nothing else|not a single|not once|"
    r"exclusively|solely|sole|neither|nowhere|elsewhere|anywhere else)\b"
    # ASK-FIX1 confirm item 4: up to 3 words in "no ... ads" ("carry no
    # Saatva ads"), "didn't place ads", "not <scope>" (", not sleep shows"; the words carry no
    # comma; never the partial opening "Not in the episodes we searched:"),
    # lack, missing, limited/confined/restricted to (about ads, a brand or
    # placement) and "show up".
    # The gap never holds an article, nor a joining word that starts a new
    # clause ("there's no risk and an ad says", "No, a Saatva ad"; ASK-30
    # E2), but holds a list ("no Saatva or Casper ads"; r1 reviews).
    r"|\bno (?:(?!(?:an?|the)\b|(?:and|or|but|then|so) (?:an?|the|this|that|these|those|"
    r"its|their|his|her|our|your|my|it|they|he|she|we|you|i)\b)\w+ ){0,3}?"
    r"(?:episodes?|shows?|podcasts?|passages?|hosts?|guests?|mentions?|ads?|"
    r"brands?|sponsors?|advertisers?|segments?)\b"
    r"|\bnot (?!in the episodes we searched\b)(?:\w+ ){0,3}?(?:episodes?|shows?|podcasts?)\b"
    r"|\black(?:s|ed|ing)?\b|\bmissing\b"
    # About ads, a brand or placement only: "the offer is limited to new
    # customers" is ad copy (confirm round 3, Fable LOW).
    r"|\b(?:ads?|zzb\d+|zzl\d+|spots?|reads?|placements?|campaigns?|advertising)"
    r"(?: \w+){0,2} (?:limited|confined|restricted) to\b"
    r"|\b(?:not|isnt|arent|wasnt|werent|doesnt|dont|didnt|cannot|cant|wont|hasnt|havent) "
    r"(?:\w+ ){0,2}(?:mention\w*|appear\w*|discuss\w*|come up|comes up|came up|cover\w*|"
    r"talk\w* about|bring\w* up|brought up|show\w* up)\b"
    r"|\bfail(?:s|ed)? to (?:mention|discuss|cover|name)\b"
    r"|\b(?:says?|said) nothing about\b|\bno discussion of\b"
    # ASK-FIX1 plan review M2: a negation 0-3 words before an ad verb
    # ("did not advertise", "has not been sponsoring"), and "ad free",
    # "absent", "skipped" and "nor", in any paragraph.
    r"|\b(?:not|no|never|nor|isnt|arent|wasnt|werent|doesnt|dont|didnt|cannot|cant|wont|hasnt|"
    r"havent|hadnt|wouldnt|couldnt|shouldnt)(?: \w+){0,3} (?:advertis\w*|sponsor\w*|ran|runs?|"
    r"aired|carried|featured|promot\w*|plac\w*|buy\w*|bought|book\w*)\b"
    r"|\bad ?free\b|\babsent\b|\bskipped\b|\bnor\b")
# ASK-FIX1 plan review M1/M3: after a complete search a paragraph that opens
# with "No," keeps its verdict only in UX's W6 form, decided on the whole
# paragraph before any other rule: "No, none of the <brand> ads <tail>",
# <brand> a whole spelling of a cited ad's brand, the tail at most
# W6_TAIL_MAX words, each one of the question's words, a whole spelling of
# that brand or W6_FUNCTION_WORDS ("or" and "any" only as the question's
# own), and never "ad free". Anything else is removed (VERDICT_NOT_W6, M7),
# so the verdict shows "not shown". Every other rule still runs on a W6
# paragraph; the absence rule leaves only the head's "none".
VERDICT_NOT_W6 = "verdict_not_w6"
W6_TAIL_MAX = 12
W6_FUNCTION_WORDS = frozenset(("the a an on in of for at ran run aired advertised say mention"
                               " offer with").split())
_W6_HEAD = ("no", "none", "of", "the")
_W6_HEAD_RE = re.compile(r"\s*no (none) of the ")
_AD_FREE_RE = re.compile(r"\bad ?free\b")
# ASK-FIX1 confirm (Opus items 2-3, Fable MED): the tail has one of these
# verbs, and every question word that is not the asked brand's and not in
# W6_SCOPE_STOP ("Has Saatva advertised on a sleep or health show?" needs
# "sleep" and "health"). When the question names a brand, the W6 brand is it.
W6_VERBS = frozenset("ran run aired advertised say mention offer".split())
W6_SCOPE_STOP = frozenset(("did has was there any a an the on in of or ads ad show shows"
                           " podcast podcasts run ran"
                           # Confirm round 3 LOW: question openers, the
                           # possessive "s", and the ad verbs' -ing/-s stems.
                           " does do is are have had ever with for to by s"
                           " advertising advertises runs running airs mentions offers").split())


def _named_in(tup, toks) -> bool:
    return any(tuple(toks[i:i + len(tup)]) == tup for i in range(len(toks) - len(tup) + 1))


def _w6_form(text: str, cited: list, names, question) -> bool:
    """Whether a "No," paragraph is UX's W6 form (above)."""
    toks = _tokens(text)
    if tuple(toks[:4]) != _W6_HEAD or _AD_FREE_RE.search(" ".join(toks)):
        return False
    spellings = {b: {t for t in names.spellings(b) | {tuple(_tokens(b))} if t}
                 for p in cited for b in _ad_brands(p)}
    spelled = sorted(set().union(*spellings.values()), key=len, reverse=True)
    q_toks = _tokens(question) if isinstance(question, str) else []
    asked = set(q_toks)
    q_brands = set().union(*(k for _, k in names.find(question, any_case=True)
                             if any(x[0] == "brand" for x in k))) if q_toks else set()
    rest = toks[4:]
    for brand in spelled:
        if tuple(rest[:len(brand)]) != brand or rest[len(brand):len(brand) + 1] != ["ads"]:
            continue
        # The W6 brand's own spellings (a cited ad's brand with this spelling).
        own = set().union(*(f for f in spellings.values() if brand in f))
        if q_brands and not (names.keys_for(" ".join(brand)) & q_brands
                             or any(_named_in(f, q_toks) for f in own)):
            return False
        tail = rest[len(brand) + 1:]
        if len(tail) > W6_TAIL_MAX or not W6_VERBS & set(tail):
            return False
        own_words = {w for f in own for w in f}
        if any(w not in tail for w in q_toks if w not in own_words
               and w not in W6_SCOPE_STOP and not w.startswith("advertis")):
            return False
        i = 0
        while i < len(tail):
            whole = next((t for t in spelled if tuple(tail[i:i + len(t)]) == t), None)
            if whole:
                i += len(whole)
            elif tail[i] in W6_FUNCTION_WORDS or tail[i] in asked:
                i += 1
            else:
                return False
        return True
    return False


# Ad words, and the placeholders _mark puts for named brands (zzb0, zzl0).
_AD_WORD = r"(?:ads?|advert\w*|sponsor\w*|brands?|advertisers?|commercials?|promos?|spots?)"
_BRAND_TOKEN = r"(?:zzb\d+|zzl\d+)"
_DOUBT_NAME = (r"(?:possible|possibly|potential|potentially|suspected|likely|probably|probable|"
               r"perhaps|maybe|apparent|apparently|presumably|seemingly|"
               r"(?:may|might|could) (?:not )?be)")
_DOUBT = (rf"(?:{_DOUBT_NAME}|unclear|uncertain|unknown|unnamed|unspecified|mystery|mysterious|"
          r"withheld|anonymous|"
          r"(?:seems?|seemed|appears?|appeared) (?:to be|like)|(?:sounds?|sounded|looks?|looked) "
          r"like|(?:not|isnt|wasnt|arent|werent) (?:given|named|stated|clear|known|attributed|"
          r"identified|specified|tagged|matched|linked|assigned|mapped|tied)|"
          r"(?:doesnt|does not|dont|didnt|did not) (?:name|say|give|state|identify|reveal|specify)|"
          r"(?:cant|cannot|can not|couldnt|could not) (?:tell|say|identify))")
STATUS_RE = re.compile(
    r"\b(?:(?:un)?confirm(?:ed|s|ing|ation)|not confirmed|possible ads?|possibly an? ads?|suggested|"
    r"(?:un)?verif(?:ied|ies|ying|ication)|"
    r"(?:high|low) confidence|not (?:\w+ ){0,2}identified|(?:isnt|wasnt|arent|werent|hasnt|havent) "
    r"(?:\w+ ){0,2}identified|unidentified|unclear brands?|unknown brands?|unnamed brands?|"
    r"(?:un)?label\w*|no identified|unattributed|not attributed|unbranded|"
    r"no (?:named|known|clear) brands?)\b"
    rf"|\b{_DOUBT} (?:\w+ ){{0,3}}{_AD_WORD}\b|\b{_AD_WORD} (?:\w+ ){{0,3}}{_DOUBT}\b"
    # A hedge reaches an ad word (not "brand") further on ("likely a soft foam
    # mattress ad", "suggests this is a Saatva ad").
    rf"|\b(?:{_DOUBT_NAME}|suggests?|suggesting) (?:\w+ ){{0,5}}"
    r"(?:ads?|advert\w*|sponsor\w*|advertisers?|commercials?|promos?|spots?)\b")
# A hedge next to a named brand ("This is likely Saatva."). In an uncertain
# item it is a status word only with ad wording ("Quince may be the sponsor"),
# so doubt about what a passage says ("quince may be the fruit") stays.
_HEDGE_BRAND_RE = re.compile(
    rf"\b{_DOUBT_NAME} (?:\w+ ){{0,3}}zzl\d+\b|\bzzl\d+ (?:\w+ ){{0,3}}{_DOUBT_NAME}\b")
# A hedge next to a brand found by the case rules ("This segment is likely
# Saatva.") is a status word in every item (design 5.2 item 9; postcap-r1 H2).
_DOUBT_BRAND = (rf"(?:{_DOUBT_NAME}|unclear|uncertain|not sure|not certain|hard to tell|ambiguous|"
                r"supposedly|(?:seems?|seemed|appears?|appeared) (?:to be|like)|"
                r"(?:sounds?|sounded|looks?|looked) like|(?:not|isnt|wasnt) (?:clear|known))")
_HEDGE_NAMED_RE = re.compile(
    # After the brand only the plain hedges ("Saatva is probably ..."): "the
    # Saatva bed looks like a hotel bed" is about the bed.
    rf"\b{_DOUBT_BRAND} (?:\w+ ){{0,5}}zzb\d+\b"
    rf"|\bzzb\d+ (?:\w+ ){{0,3}}(?:{_DOUBT_NAME}|(?:seems?|seemed|appears?|appeared) to be)\b")
# A hedge before "brand" ("This may be a lesser known mattress brand.") is a
# status word outside uncertain items (postcap-r1 M2).
_HEDGE_BRAND_WORD_RE = re.compile(rf"\b(?:{_DOUBT}|suggests?|suggesting) (?:\w+ ){{0,5}}brands?\b")
_READ_RE = re.compile(r"\breads?\b")
_STANCE_NEXT = (r"(?:about|on|toward\w*|to|reviews?|comments?|takes?|views?|opinions?|feedback|"
                r"light|reactions?|remarks?|things|notes?|press|words?|mentions?|coverage|spin|"
                r"framing|sentiment|tone)")
_STANCE_PREV = (r"(?:is|was|are|were|be|been|sounds?|seems?|stays?|remains?|very|quite|mostly|"
                r"largely|generally|overwhelmingly|less|more|too|so)")
TONE_RE = re.compile(
    rf"\b(?:(?:negative|positive) {_STANCE_NEXT}|{_STANCE_PREV} (?:negative|positive)|negatively|"
    r"positively|critici[sz]\w*|critical of|critiqu\w*|critics? of|complain\w*|prais\w*|"
    r"mock(?:s|ed|ing|ery)|ridicul\w*|sarcas\w*|sentiment\w*|"
    r"(?<!skin )(?<!muscle )(?<!dial )(?<!ring )(?<!two )(?<!even )tones?|"
    r"favou?rabl\w*|unfavou?rabl\w*|disparag\w*|"
    r"scathing\w*|enthusias\w*|dismissive\w*|skeptic\w*|sceptic\w*|disapprov\w*|derisi\w*|"
    r"contempt\w*|disdain\w*|cynic\w*|hostil\w*|gush\w*|rave|raves|raved|raving|slam|slams|"
    r"slammed|slamming|bash|bashes|bashed|bashing|love|loves|loved|hate|hates|hated|dislik\w*|"
    r"frustrat\w*|annoy\w*|angry|upset|recommend\w* against|trashed|trashes|trashing|"
    r"roast(?:s|ed|ing)? (?:the|a|an|his|her|their|its|it|them|him|this|that|our|my|your|zz[bl]\d+)|"
    r"disappoint\w*|unhappy|big fans?|fans? of|swear\w* by|swore by|speak\w* highly|spoke highly|"
    r"(?:make|makes|made|making) fun|overpriced|thrilled|delight\w*|excit\w*|likes|liked|"
    r"ador(?:e|es|ed|ing|able)|impressed|impressive|unimpressed|glowing|rants?|ranted|ranting|"
    r"blasts|blasted|blasting|panned|pans (?:the|a|an|it|them|this|that|his|her|their|zz[bl]\d+)|"
    r"jok(?:e|es|ed|ing) about|rips? into|ripped into|warmly|glowingly|bullish|bearish|"
    r"optimis\w*|pessimis\w*|upbeat|dismiss(?:es|ed|ing)?|pok(?:e|es|ed|ing) fun|"
    r"teas(?:e|es|ed|ing)|enjoys|enjoyed|enjoying|"
    r"(?:is|was|are|were|been|very|quite|highly|sharply|harshly|increasingly) critical"
    r"(?! (?:for|to|in|of|infrastructure|issue|part|role|factor|step|point)\b)|critically|"
    r"critical (?:about|on|toward\w*|reviews?|comments?|takes?|views?|opinions?|feedback|light|"
    r"reactions?|remarks?|notes?|press|words?|coverage|eye|stance))\b")
_TONE_Q_RE = re.compile(
    r"\b(?:attitude\w*|stance\w*|opinion\w*|feel|feels|feeling\w*|think of|think about|"
    r"like or dislike|bad things|good things|trash talk\w*)\b")
AD_CLAIM_RE = re.compile(r"\b(?:advertis\w*|sponsor\w*|promot\w*|commercials?|promos?|spots?|"
                         r"ads?|host reads?|paid (?:partner\w*|promotion\w*)|paid for by|"
                         r"brought to you by|presented by|supported by|partner\w* with|"
                         r"pitch(?:es|ed|ing)|plugs|plugged|plugging|endors\w*|underwrit\w*|"
                         r"support for (?:\w+ ){1,6}(?:comes?|came) from)\b"
                         rf"|\b{_BRAND_TOKEN} (?:reads?|offers?|codes?|discounts?)\b")
# Only these exact words: any other word inside stays readable to the checks.
_OUTSIDE_ADS_RE = re.compile(r"\boutside (?:of )?(?:(?:its|their|the|any|detected|these|those) )?"
                             r"ads?\b")
# F6 (plain mode): a talk verb with every cite in an ad, unless an ad word
# stands anywhere outside a quote. A word rule, no clause logic (owner).
F6_TALK_RE = re.compile(r"\b(?:mention(?:s|ed|ing)?|talk(?:s|ed|ing)? about|"
                        r"discuss(?:es|ed|ing)?|(?:come|comes|came|coming) up|"
                        r"(?:bring|brings|brought|bringing) up|(?:say|says|said|saying) about)\b")
F6_AD_WORDS = frozenset({"ad", "ads", "advert", "advertisement", "advertises", "advertised",
                         "advertising", "advertisements", "sponsor", "sponsors", "sponsored",
                         "sponsorship", "promo", "promos", "promotion", "promotions", "commercial",
                         "commercials", "read"})
# A lower-case brand counts for ad wording only right next to it.
_ADJACENT_AD_RE = re.compile(
    r"\b(zzl\d+) (?:ads?|reads?|spots?|commercials?|promos?|offers?|codes?|advertis\w*|sponsor\w*)\b"
    r"|\b(?:ads?|commercials?|spots?|reads?|promos?) (?:for|from|by) (zzl\d+)\b"
    r"|\b(?:sponsored|presented|brought to you|supported|paid for|underwritten) by (zzl\d+)\b")

# What an ad does, after an ad word: a verb (any tense, or its -ing form,
# R1 round 2 MED-2), with up to two helper words before it.
_AD_DOES = (r"(?:(?:may|might|also|then|again|could|would|will|can|reportedly) ){0,2}"
            r"(?:offers?|offered|offering|says?|said|saying|gives?|gave|giving|"
            r"promises?|promised|promising|pitch(?:es|ed|ing)?|urges?|urged|urging|"
            r"tells?|told|telling|reads?|reading|features?|featured|featuring|"
            r"promotes?|promoted|promoting|touts?|touted|touting|claims?|claimed|claiming|"
            r"mentions?|mentioned|mentioning|asks?|asked|asking|invites?|invited|inviting|"
            r"describes?|described|describing|calls?|called|calling|lists?|listed|listing|"
            r"highlights?|highlighted|highlighting|encourages?|encouraged|encouraging|"
            r"sends?|sent|sending|airs?|aired|airing|runs?|ran|running|"
            r"plugs?|plugged|plugging)\b")
# ASK-AD-PRONOUN (a): "its", "their" or "the company's / the brand's"
# before ad wording ties it to a brand the item need not name, and so do
# "it / they advertise" ("it's" folds to "it", so it is no "its"). A
# "spot", a "read" or a singular "commercial" is an ad only as "ad spot",
# "host / sponsor read" or before what an ad does, a short phrase allowed
# between ("its spot offers", "their commercial on TV offers"); "earns its
# spot", "their customers read", "its commercial success" stay (round 2
# MED-3, round 3 LOW-3).
_AD_NOUN = (r"(?:ads?|adverts?|advertis\w*|sponsor\w*|commercials|promos?|"
            r"(?:ad|host|sponsor) (?:spots?|reads?)|"
            rf"(?:spots?|reads?|commercial) "
            rf"(?=(?:(?:for|from|on|in|during|about|by) (?:\w+ ){{1,3}})?{_AD_DOES}))")
_PRONOUN_AD_RE = re.compile(
    r"\b(?:its|their|(?:the|this) (?:company|brand|advertiser|sponsor)s?) (?:\w+ ){0,3}"
    rf"{_AD_NOUN}\b|\b(?:it|they) advertis\w*")
# ASK-AD-PRONOUN (b): ad wording ("an ad offers...", "a mattress ad says...")
# needs a cited passage inside an ad. R1 MED-3, shaped by the offline
# replay: an ad word is ad wording only next to what an ad does ("the
# sponsor gives", "a TV commercial offers", "an ad for a mattress says",
# "an ad saying", "an ad that promises"; "in the ad, the host promises":
# _IN_THE_AD_RE), or as "advertised with",
# and so is a cue that opens an ad ("sponsored by", "brought to you by":
# ad_rules.OPEN_CUE_RE, imported). An ad word next to a brand is
# advertiser_not_attributed before this. Talk about ads stays ("podcast
# advertising works", "turned down a sponsor", "promo code", "ad free", "a
# commercial real estate deal", "these ads started airing", "an ad break",
# "advertised on a billboard"), and so does ad wording a cited passage
# says word for word, with the claim's next words (_ad_words_unsaid).
_AD_WORD = (r"(?:ads?|adverts?|advertisements?|advertis(?:ing|ers?)|commercials?|"
            r"promos?|sponsor\w*)")
_AD_WORDING_RE = re.compile(
    r"(?<!\bnon )(?<!\bnot a )(?<!\bnot an )(?<!\bisnt a )(?<!\bisnt an )"
    rf"(?<!\bwasnt a )(?<!\bwasnt an )\b{_AD_WORD} "
    rf"(?:(?:for|from|on|in|during|about|by) (?:\w+ ){{1,3}})?(?:(?:that|which) )?{_AD_DOES}"
    r"|\badvertis(?:e|es|ed) with\b")
# "In the ad, the host promises ...": read as written, as only the comma
# (or "the ad itself") right after the ad word sets it apart from "during
# the ad break, the host says" or "in the ad industry" (round 3 LOW-2).
_IN_THE_AD_RE = re.compile(
    rf"\b(?:in|during) (?:the|this|that|an?|one|each|every) (?:[^\W_]+ ){{0,2}}{_AD_WORD}"
    rf"(?:,|\s+itself\b)[\s,]*(?:[^\W_][\w'\u2019-]*[\s,]+){{0,4}}?{_AD_DOES}", re.I)


# ASK-30 E1: what an advertiser spends is never heard, so a claim that one
# spends or invests on ads, advertising or podcasts, outspends another, or
# has a bigger ad budget is removed (spend_claim). Only with that object:
# "on up to $150 monthly spending", "auto invest" and "How I Invest" are no
# claim. A sentence with a negation is a caveat ("We don't know what brands
# spend on ads"), and wording a cited passage says is kept.
# Time spent is no money ("spends hours on podcasts"), "spent ... in" needs
# a money object ("spent years in podcasts" is a career), and "budget
# friendly" is ad copy (ASK-30 r1 reviews, Fable MED-2, Opus L4).
_SPEND_NOT_TIME = (r"(?!(?:hours?|minutes?|days?|weeks?|months?|years?|decades?|time|"
                   r"evenings?|nights?|weekends?|life|lives)\b)")
_SPEND_MONEY = (r"(?:\d\w*|hundreds?|thousands?|millions?|billions?|money|dollars?|cash|"
                r"big|heavily|lots?|much|more|most)")
_SPEND_OBJECT = r"(?:\w+ ){0,2}?(?:ads?|advert\w*|commercials?|podcasts?|sponsorships?)\b"
SPEND_RE = re.compile(
    r"\bout-?spen(?:d|ds|t|ding)\b"
    rf"|\b(?:spend|spends|spent|spending) (?:{_SPEND_NOT_TIME}\w+ ){{0,2}}?on {_SPEND_OBJECT}"
    rf"|\b(?:spend|spends|spent|spending) (?:{_SPEND_NOT_TIME}\w+ )?{_SPEND_MONEY} "
    rf"(?:{_SPEND_NOT_TIME}\w+ )?in {_SPEND_OBJECT}"
    rf"|\b(?:invest|invests|invested|investing) (?:{_SPEND_NOT_TIME}\w+ ){{0,2}}?(?:on|in) "
    rf"{_SPEND_OBJECT}"
    r"|\b(?:bigger|biggest|larger|largest|higher|highest|more|most) "
    r"(?:(?:ad|advertising|marketing|podcast|media) (?:budgets?|spend|spending)"
    r"|budgets?\b(?!-?\s*friendly\b))\b")
_SPEND_CAVEAT_RE = re.compile(
    r"\b(?:not|no|never|cannot|cant|dont|doesnt|didnt|isnt|arent|wont|unknown|nothing)\b")


def _spend_claim(texts: list, table: list, cited: list) -> bool:
    """A SPEND_RE hit, names masked (a title's words are no claim), in a
    sentence with no negation that no cited passage says word for word
    (texts: the readings outside quotes)."""
    hits = []
    for t in texts:
        for sentence in re.split(r"[.!?;]+", t):
            words = _words(sentence)
            masked = _mark(words, table)
            if SPEND_RE.search(masked) and not _SPEND_CAVEAT_RE.search(masked):
                hits.append(words)
    return bool(hits) and _ad_words_unsaid(SPEND_RE, hits, cited)


def _ad_words_unsaid(rx, texts: list, cited: list, *, folded: bool = True) -> bool:
    """Some hit of rx in texts that no one cited passage says word for word
    together with the claim's next three words (R1 round 2 MED-1: two words
    anywhere back nothing), with a word edge at both ends (LOW-1). A hit
    that ends inside a word ("our sponsor" in "our sponsors") is read to the
    end of that word first. Windows come from the item as written (texts[0]);
    a glued reading adds only hits the written one lacks ("a-ds" reads
    "ads"), so "risk-free" is said by "risk free" (round 3 LOW-1). No hit at
    all counts as unsaid, so the check never weakens when only the masked
    text matched."""
    # Each reading of each passage, as the item's hits come from its readings
    # ("soul-to-story" also reads "soultostory").
    hay = [f" {_words(r)} " for p in cited for r in _readings(p.text if isinstance(p.text, str) else "")]
    wins, written = [], set()
    for i, t in enumerate(texts):
        for h in rx.finditer(t):
            rest = (t[h.start():] if folded else _words(t[h.start():])).split()
            hit = (h.group() if folded else _words(h.group())).split()
            if i and " ".join(hit) in written:
                continue
            if not i:
                written.add(" ".join(hit))
            wins.append(" ".join(rest[:len(hit) + 3]))
    return not wins or any(not any(f" {w} " in s for s in hay) for w in wins)


def is_tone_question(text) -> bool:
    words = _words(text if isinstance(text, str) else "")
    return bool(TONE_RE.search(words) or _TONE_Q_RE.search(words))


# ---------------------------------------------------------------- tokens

_URL_RE = re.compile(r"(?<![a-z0-9-])(?:https?://)?(?:www\.)?[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*"
                     r"\.[a-z]{2,24}(?![a-z0-9-])(?:/[\w\-./%?=&+#~@!$*,;:]*)?", re.I)
# A web address read aloud: the 1-3 words before a spoken "dot" or "point"
# and a top-level domain, with any path after "slash" (B1, review r2 #2).
_TLD = r"(?:com|net|org|co|io|us|ca|uk|app|tv|me|ai|shop|store|info|biz)"
# Tried only at a word start, words without separators: linear time.
_SPOKEN_URL_RE = re.compile(r"(?<![\w'&])(?:[\w'&]+[\s,-]+){0,2}[\w'&]+(?:[\s-]+(?:dot|point)[\s-]+"
                            + _TLD + r"\b)+(?:\s*(?:\bslash\b|/)\s*[\w-]+)*", re.I)
_TIME_RE = re.compile(r"(?<![\d:])\d{1,2}:\d{2}(?::\d{2})?(?![\d:])")
_NUM_RE = re.compile(r"(?<![a-z0-9])\d+(?:[.,]\d+)*(?![a-z0-9])", re.I)


# A dot look-alike (any punctuation or symbol named FULL STOP, DOT, POINT or
# BULLET: 。 · ‧ ・ ∙ ⋅ • ⸱ ⸳ ...) between letters or digits reads as ".", and so
# do "[.]", "(.)", "(dot)", "[dot]" anywhere.
_BETWEEN_RE = re.compile(r"(?<=[a-z0-9])[^\w\s.](?=[a-z0-9])")
_BRACKET_DOT_RE = re.compile(r"\s*[\[({]\s*(?:\.|dot)\s*[\])}]\s*")


@functools.lru_cache(maxsize=4096)
def _dot_like(ch: str) -> bool:
    if unicodedata.category(ch)[0] not in "PS":
        return False
    name = unicodedata.name(ch, "")
    return any(w in name for w in ("FULL STOP", "DOT", "POINT", "BULLET"))


def _fold_dots(text: str) -> str:
    text = _BRACKET_DOT_RE.sub(".", text)
    return _BETWEEN_RE.sub(lambda m: "." if _dot_like(m.group()) else m.group(), text)


def _url_fold(text: str) -> str:
    # Whitespace runs collapsed first: the patterns below stay linear. A
    # spaced dot, "point" or "period" before a lower-case com/net/org is a
    # dot ("saatvadeals. com"); a capital ("fell. Net income") is a sentence.
    text = " ".join(_clean(text).split())
    text = re.sub(r"(?: ?\.| (?:point|period)) (?=(?:com|net|org)\b)", ".", text)
    text = _fold_dots(text.casefold())
    # "the dot com bubble" stays words.
    text = re.sub(r"(?<!\bthe)(?<!\ban)(?<!\ba)\s+dot\s+", ".", text)
    text = re.sub(r"\s+\.(?=[a-z]{2,24}\b)", ".", text)
    text = re.sub(r"\s*(?:\bslash\b|/)\s*", "/", text)
    # A spaced or spelled mailbox is a mailbox (postcap-r1 M3, M4; r2 M-1..M-3).
    text = _AT_SIGN_RE.sub("@", text)
    return _SPOKEN_AT_RE.sub(lambda m: m.group()[:-4] + "@", text)


# "@", or "at" in brackets or dashes ("( at )", "[at]", "{at}", "<at>", "-at-").
_AT_SIGN_RE = re.compile(r"\s{0,3}(?:@|[(\[{<]\s{0,3}(?:at|@)\s{0,3}[)\]}>]|-at-)\s{0,3}")
# A spoken "at" is a mailbox's only after a mail cue ("email deals at x.com",
# "email them: deals at x.com"), never before a web page's path ("contact us
# at x.com/help"), in items and passages alike (postcap-r2 M-1..M-3).
_MAIL_WORD = r"(?:e-?mail(?:s|ed|ing)?|mail(?:s|ed|ing)?)\b"
_MAIL_TO = r"(?: (?:us|them|me|him|her|it):?)"
_MAILBOX_AT = r" [a-z0-9._%+-]{1,64} at (?=[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63}){1,8}(?![a-z0-9/-]))"
# An offer or list word after a bare "email"/"mail" is not a mailbox only after an
# offer context word ("sign up for email updates at x.com", "join our mailing list at
# x.com"); anywhere else the cue reads the next word as a mailbox ("Email deals at x.com",
# "listeners can email deals at x.com", a passage's "for deals email deals at x dot com")
# (postcap-r3 M3, r4 L2/L3).
_OFFER_CONTEXT = ("the", "our", "your", "their", "its", "for", "get", "join", "receive", "plus")
_OFFER_WORD = (r"(?:updates?|offers?|deals?|lists?|newsletters?|alerts?|discounts?|coupons?|"
               r"promotions?|specials?|savings|news|sign-?ups?) at ")
_MAIL_MARKED = (r"\b(?:" + _MAIL_WORD + r"|write to\b|writing to\b)"
                r"(?::(?: to)?" + _MAIL_TO + r"?(?: at)?| to" + _MAIL_TO + r"?(?: at)?|"
                + _MAIL_TO + r"(?: at)?| at)|\b(?:write|writing) to\b")
_SPOKEN_AT_RE = re.compile(
    r"(?:" + _MAIL_MARKED
    + r"|" + "".join(rf"(?<!\b{w} )" for w in _OFFER_CONTEXT) + r"\b" + _MAIL_WORD
    + r"|(?:" + "|".join(rf"(?<=\b{w} )" for w in _OFFER_CONTEXT) + r")\b" + _MAIL_WORD
    + r"(?= (?!" + _OFFER_WORD + r")))" + _MAILBOX_AT)


_MAILBOX_RE = re.compile(r"[a-z0-9._%+-]+@$")


def _site(url: str) -> str:
    """The first host label of a web address ("www.Saatva.com/x" -> "Saatva")."""
    return re.sub(r"^(?:https?://)?(?:www\.)?", "", url, flags=re.I).split(".")[0]


def _url_key(url: str) -> str:
    url = re.sub(r"^(?:https?://)?(?:www\.)?", "", url.casefold())
    return url.rstrip("/" + _EDGE)


def _seconds(t: str):
    parts = [int(x) for x in t.split(":")]
    return parts[0] * 60 + parts[1] if len(parts) == 2 else parts[0] * 3600 + parts[1] * 60 + parts[2]


def _numbers(text: str) -> list:
    """Numbers outside times; `_` separates, `,` groups digits."""
    return [n.replace(",", "") for n in _NUM_RE.findall(_TIME_RE.sub(" ", text.replace("_", " ")))]


@functools.lru_cache(maxsize=64)
def _hay_parts(texts: tuple) -> tuple:
    """One passage's grounded web addresses, times, numbers, tokens and plain
    text; cached, so many items citing the same passages read them once."""
    joined = "\n".join(_clean(t) for t in texts)
    return ("\n".join(_url_fold(t) for t in texts),
            frozenset(_seconds(t) for t in _TIME_RE.findall(joined)),
            frozenset(_numbers(joined)), frozenset(_tokens(joined)), _plain(joined))


class _Hay:
    """What the cited passages ground: their text and metadata."""

    def __init__(self, cited: list):
        parts = []
        for p in cited:
            texts = [p.text or "", p.show or "", p.episode_title or "", p.pub_date or ""]
            texts += _ad_brands(p)
            parts.append(_hay_parts(tuple(t if isinstance(t, str) else "" for t in texts)))
        self.urls = "\n".join(x[0] for x in parts)
        self.times = set().union(*(x[1] for x in parts))
        self.times |= {int(round(float(x))) for p in cited for x in (p.start_s, p.end_s)
                       if _num(x) is not None}
        self.numbers = set().union(*(x[2] for x in parts))
        self.tokens = set().union(*(x[3] for x in parts))
        self.raw = "\n".join(x[4] for x in parts)

    def url_ok(self, url: str) -> bool:
        key = _url_key(url)
        # Whole host labels: "saatva.com" is not grounded by "saatva.com.au".
        return bool(key) and re.search(r"(?<![a-z0-9.-])" + re.escape(key) +
                                       r"(?![a-z0-9-]|\.[a-z0-9])", self.urls) is not None


_SYMBOL_OK = frozenset("°×÷±")


def _lookalike(text: str, hay: _Hay) -> bool:
    """A word with a letter or digit outside ASCII once accents are dropped
    (look-alike letters, small capitals, other scripts) that no cited
    passage has; or a symbol (not currency, not ° × ÷ ±) touching a letter
    or digit that no cited passage has."""
    for tok, folded in _orig_tokens(text):
        if any(ord(ch) > 127 for ch in tok) and folded not in hay.tokens:
            return True
    plain = _plain(text)
    for i, ch in enumerate(plain):
        if ord(ch) < 128 or ch.isspace() or ch in _SYMBOL_OK:
            continue
        cat = unicodedata.category(ch)
        if cat[0] in "SM" and cat != "Sc" and ch not in hay.raw \
                and any(c.isalnum() for c in plain[max(i - 1, 0):i] + plain[i + 1:i + 2]):
            return True
    return False


def _code_like(tok: str) -> bool:
    has_digit = any(ch.isdigit() for ch in tok)
    has_alpha = any(ch.isalpha() for ch in tok)
    return (has_digit and has_alpha) or sum(ch.isupper() for ch in tok) >= 4


# A code-like run: letters and digits joined by any one symbol ("NIGHT-15",
# "NIGHT~15", "Night_15", "NIGHT/15"), or by a symbol between single spaces
# ("NIGHT - 15"); a "." or ":" joins only when no space follows (postcap-r2 H-3).
# An em dash or bar breaks a run (prose: "NIGHT15\u2014it saves 15 percent"). After a
# spaced symbol a run goes on only into a part with a capital or a digit ("NIGHT15 - it
# gives" stops), and never into an amount ("NIGHT15 - 200 dollars") (postcap-r3 M1, r4 M1/M2).
_JOINER = r"(?:[^\w\s'\u2019\"\u201c\u201d,;!?()\[\]{}<>\u2014\u2015\u2e3a\u2e3b]|_)"
_CODE_RUN = (rf"[A-Za-z0-9]+(?:(?:{_JOINER}(?=[A-Za-z0-9])|\s[^\w\s.:'\u2019\"\u201c\u201d,;!?()\[\]{{}}<>\u2014\u2015\u2e3a\u2e3b]\s"
             r"(?=(?-i:[a-z]*[A-Z0-9]))(?![0-9]+(?:%| ?percent\b| off\b| dollars\b)))[A-Za-z0-9]+)*")
# The word after "code" is a code only after a use cue ("use code sleepy",
# "enter the promo code sleepy", "with code sleepy") or in "code is X" / "code: X";
# "the ad gives a promo code twice" names no code.
_CODE_AFTER_RE = re.compile(
    r"\b(?:use|uses|used|using|enter|enters|entered|entering|apply|applies|applied|applying|with)"
    rf"\s+(?:[A-Za-z]+\s+){{0,3}}?codes?\b[\s:]+(?:(?:is|was|are)\s+)?({_CODE_RUN})"
    rf"|\bcodes?\b(?:\s*:\s*|\s+(?:is|was|are)\s+)({_CODE_RUN})", re.I)
_JOINED_RE = re.compile(_CODE_RUN)


def _joined_ok(toks: list, hay: "_Hay", cited: list) -> bool:
    """One word the cited passages have, or the words in a row in one of them."""
    return toks[0] in hay.tokens if len(toks) == 1 else _quote_ok(" ".join(toks), cited)


_CODE_STOP = frozenset({"at", "for", "to", "the", "a", "an", "and", "or", "in", "on", "with", "of",
                        "but", "no", "not",
                        "is", "was", "are", "name", "names", "word", "words", "that", "this",
                        "which", "when", "you", "your", "it", "will", "can", "get", "gets",
                        "from", "by", "as", "if", "they", "we", "he", "she"})


def _placeholders(found: list, found_any: list, soft=()) -> list:
    """(token tuple, placeholder, brand keys) for each name found: zzb<i> for
    a brand found by the case rules, zzl<i> for a brand found only in any
    case (lower-case dictionary words) or a `soft` form (a lower-case plain
    phrase, ASK-CHECK-WORDS rule 3), zzshow for a show."""
    table, seen = [], set()
    for tup, keys in found:
        brands = {k for k in keys if k[0] == "brand"}
        table.append((tup, (f"zzl{len(table)}" if tup in soft else f"zzb{len(table)}")
                      if brands else "zzshow", brands))
        seen.add(tup)
    for tup, keys in found_any:
        brands = {k for k in keys if k[0] == "brand"}
        if tup not in seen and brands:
            table.append((tup, f"zzl{len(table)}", brands))
            seen.add(tup)
    return table


def _mark(words: str, table: list) -> str:
    """Found names masked by their placeholders, so a title's words are never
    read as wording and ad or doubt words can be tied to a brand."""
    for tup, token, _ in sorted(table, key=lambda t: -len(t[0])):
        words = re.sub(rf"(?<!\S){re.escape(' '.join(tup))}(?!\S)", token, words)
    return " ".join(words.split())


_NEAR = 6


# ASK-30 fix 9 (A1''): the words of a code offer are no brand. "code" right
# after promo/discount/coupon/offer/use/enter/with (a "the", "my"... between
# allowed) or right before "at checkout", whether or not "code" is a name
# here, and a no-display name right after it (the code itself: "promo code
# Beckett"), read as plain words for ad wording. A display brand after the
# phrase is still a brand.
_CODE_LEAD = frozenset(("promo", "discount", "coupon", "offer", "use", "enter", "with"))
_CODE_DET = frozenset(("the", "my", "our", "your", "a", "this", "that"))


def _offer_words(marked: str, table: list, weak) -> str:
    codes = {"code", "codes"} | {token for tup, token, _ in table if tup in (("code",), ("codes",))}
    nameless = {token for _, token, keys in table if keys and keys <= weak}
    toks = marked.split()
    for i, t in enumerate(toks):
        if t not in codes:
            continue
        lead = (i >= 1 and toks[i - 1] in _CODE_LEAD) or (
            i >= 2 and toks[i - 1] in _CODE_DET and toks[i - 2] in _CODE_LEAD)
        if not (lead or toks[i + 1:i + 3] == ["at", "checkout"]):
            continue
        toks[i] = "code"
        if i + 1 < len(toks) and toks[i + 1] in nameless:
            toks[i + 1] = "x"
    return " ".join(toks)


def _bound_brands(marked: str, plain=(), backed=(), talk=()) -> set:
    """Placeholders of the brands that ad wording is about: a brand inside the
    wording, else the nearest brand within _NEAR words, with any brands
    listed next to it ("Saatva and Casper"); a lower-case brand only when
    right next to the wording. A `plain` placeholder (a plain-word name) is
    never the nearest brand, nor listed, when another brand is in reach ("An
    ad promotes the June sale at Wayfair"); "<name> reads" is no ad wording
    for it, nor "<name> offers|codes|discounts" within _NEAR of a `backed`
    brand (one a cited ad backs: "Dan offers 20 percent off at Wayfair"), or
    that a cited talk passage says (`talk`: (placeholder, verb) pairs). Nor is a
    plain name the nearest brand within _NEAR words of a backed brand, also a
    lower-case one ("zenni's ad mentions Ultra", "An ad offers Phoenix shoppers
    free shipping from Wayfair"; R1 round 4)."""
    toks = marked.split()
    is_brand = [bool(re.fullmatch(r"zzb\d+", t)) for t in toks]
    backed_at = [k for k, t in enumerate(toks) if t in backed]
    bound = set()

    def host_form(m) -> bool:
        words = m.group().split()
        if len(words) != 2 or words[0] not in plain:
            return False
        if words[1] in ("read", "reads") or (words[0], words[1]) in talk:
            return True
        a = marked[:m.start()].count(" ")
        return re.fullmatch(r"offers?|codes?|discounts?", words[1]) is not None and any(
            t in backed and min(abs(k - a), abs(k - a - 1)) <= _NEAR for k, t in enumerate(toks))

    def chain(k, step):
        while 0 <= k + step < len(toks):
            if is_brand[k + step] and toks[k + step] not in plain:
                k += step
            elif toks[k + step] in ("and", "or") and 0 <= k + 2 * step < len(toks) \
                    and is_brand[k + 2 * step] and toks[k + 2 * step] not in plain:
                k += 2 * step
            else:
                return
            bound.add(toks[k])

    for m in AD_CLAIM_RE.finditer(marked):
        if host_form(m):
            continue
        a = marked[:m.start()].count(" ")
        b = marked[:m.end()].count(" ")
        # "Support for X comes from Y": the sponsor is Y, after "from"; X is
        # what is supported (often the show), never bound (ASK-30 B).
        support = m.group().startswith("support for ")
        inside = [] if support else [t for t in toks[a:b + 1] if re.fullmatch(r"zz[bl]\d+", t)]
        if inside:
            bound.update(inside)
            continue
        near = [(min(abs(k - a), abs(k - b)), k) for k in range(len(toks))
                if is_brand[k] and (not support or k > b)]
        near = [x for x in near if x[0] <= _NEAR]
        if any(toks[k] not in plain for _, k in near):
            near = [x for x in near if toks[x[1]] not in plain]
        near = [x for x in near if toks[x[1]] not in plain or not any(
            min(abs(j - x[1]), abs(j - a), abs(j - b)) <= _NEAR for j in backed_at)]
        if near:
            _, k = min(near)
            bound.add(toks[k])
            chain(k, 1)
            chain(k, -1)
    for m in _ADJACENT_AD_RE.finditer(marked):
        if host_form(m):
            continue
        bound.update(g for g in m.groups() if g)
    return bound


# ---------------------------------------------------------------- one item

# Spelled and glued read-aloud forms made written before B1 (ASK-CHECK-WORDS
# rule 5; copied from brief_checks._ADDRESS_PREP, which Ask may not import):
# "geicodotcom" and "dotcom", a word spelled letter by letter ("G E I C O",
# "c o m"), and dots split by a space ("R. .com").
_ADDRESS_PREP = (
    (re.compile(r"(?<=[^\W\d_])dot(?=(?:com|net|org)\b)", re.I), " dot "),
    (re.compile(r"\bdot(?=(?:com|net|org)\b)", re.I), "dot "),
    (re.compile(r"\b(?:[^\W\d_][-.\s]){2,}[^\W\d_]\b"),
     lambda m: re.sub(r"[-.\s]", "", m.group())),
    (re.compile(r"\.(?:\s*\.)+"), "."),
)


def _address_form(text: str) -> str:
    """text with spelled and glued read-aloud addresses in the form the
    address patterns read ("geicodotcom" -> "geico dot com")."""
    for rx, repl in _ADDRESS_PREP:
        text = rx.sub(repl, text)
    return text


def _spoken_backing(p: Passage, names: NameTable, asked) -> set:
    """B1 (LEDGER-LEAD3-AW-2, round 2 AW-4): the brand key an in-ad
    transcript passage backs for ad wording, or nothing. The passage's own
    words, web addresses (written or read aloud, also spelled or glued:
    "dot c o m", "geicodotcom") aside, speak exactly one display name X; it
    names no other brand in any case, spelling (also letter by letter) or
    web address; it overlaps a detected ad attributed to X and to no other
    brand; in brand mode X is the asked brand."""
    if p.kind != "transcript_passage" or not p.in_ad or p.held or not p.near_ad_brands:
        return set()
    text = _address_form(p.text)
    spoken = names.spoken_brands(_SPOKEN_URL_RE.sub(" ", _URL_RE.sub(" ", text)))
    if len(spoken) != 1:
        return set()
    named = {k for t in (p.text, text) for _, keys in names.find(t, any_case=True)
             for k in keys if k[0] == "brand"}
    near = set().union(*(names.keys_for(b) for b in p.near_ad_brands))
    if not named <= spoken or near != spoken:
        return set()
    if asked is not None and not spoken <= names.keys_for(asked):
        return set()
    return spoken


def _brand_keys(names: NameTable, b) -> set:
    """The brand keys of a cited ad's brand, or its compact key when the name
    table has none ("stamps.com")."""
    return {k for k in names.keys_for(b) if k[0] == "brand"} or {("brand", _compact(b))}


def _our_words(cited: list) -> set:
    """The words of the cited unheld ads' brands and of the brands of the
    detected ads cited in-ad passages overlap."""
    out = set()
    for p in cited:
        near = p.near_ad_brands if p.kind == "transcript_passage" and p.in_ad else ()
        for b in list(_ad_brands(p)) + [b for b in near if isinstance(b, str)]:
            out.update(_tokens(b))
    return out


def _active_names(site_text: str, cited: list, names: NameTable, keep=None):
    """ASK-CHECK-WORDS rule 1: the plain-word names an item is checked for:
    all of them (True) when it has ad wording ("ad", "sponsor", "<brand>
    offers"), else those of the unheld detected ads it cites. The wording is
    read with the other names only, so a plain name never turns its own check
    on ("Dan reads the headlines"); "<plain name> offers|reads|codes" counts
    only with a cited passage in an ad (R1 round 2 HIGH-2). keep: find()'s."""
    own = set()
    for p in cited:
        for b in _ad_brands(p):
            own |= names.keys_for(b)
    ours = _our_words(cited)
    if not names._cond:
        return own
    tables = [_placeholders(names.find(site_text), names.find(site_text, any_case=True))]
    if any(p.in_ad or p.kind == "detected_ad" for p in cited):
        kw = dict(active=True, own=own, ours=ours, keep=keep)
        tables.append(_placeholders(names.find(site_text, **kw),
                                    names.find(site_text, any_case=True, **kw)))
    for table in tables:
        for v in _readings(site_text):
            m = _OUTSIDE_ADS_RE.sub(" ", _mark(_words(v), table))
            if AD_CLAIM_RE.search(m) or _AD_WORDING_RE.search(m):
                return True
    return own


def _in_brand_name(tup, cited: list) -> bool:
    """ASK-CHECK-WORDS: a one-word plain name (never checked before) is part of
    the name of a cited unheld ad's brand, or of a brand whose detected ad a
    cited in-ad passage overlaps ("Rock Skin Care", "brought to you by
    Hinge" next to Hinge's ad)."""
    n = len(tup)
    for p in cited:
        near = p.near_ad_brands if p.kind == "transcript_passage" and p.in_ad else ()
        for b in list(_ad_brands(p)) + [b for b in near if isinstance(b, str)]:
            toks = _tokens(b)
            if any(tuple(toks[i:i + n]) == tup for i in range(len(toks) - n + 1)):
                return True
    return False


def _own_read(tup, keys, text: str, cited: list, names: NameTable, lower: set) -> bool:
    """ASK-CHECK-WORDS rule 4 (AW-7 (1)): a name the ad wording binds to and
    no cited ad attributes still passes when it has no display name (never
    a display name or a show), and a cited unheld detected ad is the own read
    of a brand the item names in lower case or as a plain-word brand
    ("indeed", "Hard", "General"; `lower`: those brands' keys) and, in one
    sentence with that brand's name, says the name with the item's words on
    both sides of it, everywhere the item says it ("This is a job for indeed
    sponsor jobs." citing Indeed's read)."""
    if not tup or not keys or not keys <= names.weak:
        return False
    toks = _tokens(text)
    n = len(tup)
    spots = [i for i in range(len(toks) - n + 1) if tuple(toks[i:i + n]) == tup]
    if not spots:
        return False
    for p in cited:
        if not any(names.keys_for(b) & lower for b in _ad_brands(p)):
            continue
        # The name and the read's own brand name share a sentence of the read
        # ("indeed sponsor jobs"; not "... indeed. Brought to you by Little
        # Caesars.", R1 HIGH-2).
        own_forms = {f for k in lower for b in _ad_brands(p) if k in names.keys_for(b)
                     for f in names._spell.get(k, ())}
        sentences = [_tokens(x) for x in re.split(r"(?<=[.!?])\s+", p.text)]

        def shares(i, st):
            hay = " " + " ".join(st) + " "
            return (" " + " ".join(toks[max(i - 1, 0):i + n + 1]) + " " in hay
                    and any(" " + " ".join(f) + " " in hay for f in own_forms))
        if all(any(shares(i, st) for st in sentences) for i in spots):
            return True
    return False


def _check_item(text: str, cited: list, *, names: NameTable, brand_mode: bool, brand,
                section=None, uncertain=False, verdict_no=False, question=None) -> str | None:
    if verdict_no and not _w6_form(text, cited, names, question):
        return VERDICT_NOT_W6   # plan review M1/M3: before any other rule
    hay = _Hay(cited)
    if _lookalike(text, hay):
        return "mixed_script"
    spans = _quotes(text)
    if spans is None:
        return "quote_unbalanced"
    for a, b in spans:
        if not _quote_ok(text[a + 1:b - 1], cited):
            return "quote_not_found"
    # For the held-ad and ad-wording checks a web address reads as its site
    # name ("saatva.com/night" -> "saatva").
    site_text = _URL_RE.sub(lambda m: " " + _site(m.group()) + " ", text)
    # The cited ads' own brands: their plain words stay names (R1 HIGH-1).
    own = set().union(*(names.keys_for(b) for p in cited for b in _ad_brands(p)))
    ours = _our_words(cited)

    ads = [p for p in cited if p.in_ad or p.kind == "detected_ad"]

    def is_name(tup, keys, initial) -> bool:
        # R1 round 2: a plain-word spelling (never a name on base) is one only
        # when a cited passage says it (HIGH-1: "in June", "life insurance
        # quotes"), and at a sentence start only as a cited ad's brand or said
        # capitalised mid-sentence there (MED-1: "Therapy ads", "Dan reads").
        # Round 3 (MED-2): only a passage in an ad counts ("The guest says
        # Nature offers a cure." citing a talk passage and an ad).
        if not names.grounded(" ".join(tup), ads):
            return False
        return not initial or bool(keys & own) or _said_mid_sentence(tup[0], ads)

    active = _active_names(site_text, cited, names, is_name)
    outside = text
    for a, b in reversed(spans):
        # A quote that is only a name ('likely "Saatva"', 'likely "saatva"')
        # stays: doubt next to it is still read (postcap-r3 M4, r4 L4).
        q = text[a + 1:b - 1]
        named = names.find(q, any_case=True, active=active, own=own, ours=ours, keep=is_name)
        keep = q if len(named) == 1 and named[0][0] == tuple(_tokens(q)) else " "
        outside = outside[:a] + " " + keep + " " + outside[b:]
    # Names are read in the whole text, quotes included: a quoted brand is
    # still a brand named (held ads, advertiser, status doubt).
    no_url_text = _URL_RE.sub(" ", text)
    soft: set = set()
    cond: set = set()
    found = names.find(no_url_text, active=active, own=own, ours=ours, keep=is_name, soft=soft,
                       cond=cond)
    found_any = names.find(site_text, any_case=True, active=active, own=own, ours=ours,
                           keep=is_name, cond=cond)
    # A web address's site name is a brand named by the case rules for ad
    # wording ("the sponsor is casper.com"), whatever its case.
    sites = " ".join(_site(m.group()) for m in _URL_RE.finditer(_url_fold(text)))
    site_brands = [(tup, keys) for tup, keys
                   in names.find(sites, any_case=True, active=active, own=own, ours=ours,
                                 keep=is_name)
                   if any(k[0] == "brand" for k in keys) and all(tup != t for t, _ in found)]
    # A written address whose host is a cited ad's brand key is that brand
    # ("stamps.com" citing stamps.com's ad; R1 round 4); never another's.
    for m in _URL_RE.finditer(_url_fold(text)):
        tup = tuple(_tokens(_site(m.group())))
        host = _compact(_url_key(m.group()).split("/")[0])
        b = next((b for p in cited for b in _ad_brands(p)
                  if _compact(b) in (host, _compact(_site(m.group())))), None)
        if tup and b and all(tup != t for t, _ in found + site_brands):
            site_brands.append((tup, _brand_keys(names, b)))
    table = _placeholders(found + site_brands, found_any, soft)
    # Word checks read the text outside quotes in each reading, names masked;
    # and again with only the case-rule names masked, so a lower-case brand
    # that is also a list word ("only", "all") hides nothing.
    readings = _readings(outside)
    plain_marked = [_mark(_words(v), table) for v in readings]
    strict = [t for t in table if not t[1].startswith("zzl")]
    # ASK-CHECK-WORDS: plain-word names never hide words from these checks
    # ("Love ... brought to you by" stays a tone claim).
    tables = [strict] + ([[t for t in strict if t[0] not in cond]] if cond else [])
    marked = [_OUTSIDE_ADS_RE.sub(" ", m) for m in plain_marked] + \
        [_OUTSIDE_ADS_RE.sub(" ", _mark(_words(v), tab)) for v in readings for tab in tables]
    untimed = [_mark(_words(_DATE_RE.sub(" ", _TIME_RE.sub(" ", v))), tab)
               for v in readings for tab in [table] + tables]
    if any(STATUS_RE.search(m) or _HEDGE_NAMED_RE.search(m)
           or (not uncertain and _HEDGE_BRAND_WORD_RE.search(m))
           or (_HEDGE_BRAND_RE.search(m) and not (
               uncertain and not AD_CLAIM_RE.search(m) and not _READ_RE.search(m))) for m in marked):
        return "status_word"
    if any(TONE_RE.search(m) for m in marked):
        return "tone_claim"
    if any(COUNT_RE.search(m) for m in untimed):
        return "count_claim"
    if _spend_claim(readings, table, cited):
        return "spend_claim"
    # A W6 verdict (checked above) leaves only its head's "none".
    if any(not (verdict_no and (h := _W6_HEAD_RE.match(m)) and a.span() == h.span(1))
           for m in marked for a in ABSENCE_RE.finditer(m)):
        return "absence_claim"

    folded = _url_fold(outside)
    for m in _URL_RE.finditer(folded):
        if not hay.url_ok(m.group()):
            return "url_not_grounded"
        # A mailbox ("deals@") is part of the address.
        box = _MAILBOX_RE.search(folded, max(m.start() - 64, 0), m.start())
        if box and not hay.url_ok(box.group() + m.group()):
            return "url_not_grounded"
    rest = _URL_RE.sub(" ", folded)
    for t in _TIME_RE.findall(rest):
        if _seconds(t) not in hay.times:
            return "time_not_grounded"
    for n in _numbers(rest):
        if sum(ch.isdigit() for ch in n) >= 2 and n not in hay.numbers:
            return "number_not_grounded"

    # Held ads: the brand names of the name table found by the case rules
    # (decision 1: only one-word names need a capital, so multi-word spellings
    # count in any case; a camel-case split form is the one-word name), plus
    # web addresses' site names ("saatva dot com").
    multi = [(tup, keys) for tup, keys
             in names.find(no_url_text, any_case=True, active=active, own=own, ours=ours,
                           keep=is_name)
             if not _one_word(tup) and names.modes(tup, active) - {"split"}]
    held_names = [tup for tup, keys in found + multi
                  + names.find(sites, any_case=True, active=active, own=own, ours=ours,
                               keep=is_name)
                  if any(k[0] == "brand" for k in keys)]
    if held_names and all(p.kind == "detected_ad" and p.held for p in cited):
        return "held_ad_brand"
    unheld = [p for p in cited if not (p.kind == "detected_ad" and p.held)]
    if len(unheld) < len(cited):
        # A held ad never backs a brand name, even next to other cites.
        for tup in held_names:
            if not names.grounded(" ".join(tup), unheld):
                return "held_ad_brand"
    for tup, keys in found:
        if not names.grounded(" ".join(tup), cited):
            return "name_not_grounded"
    no_urls = _URL_RE.sub(" ", _clean(outside))
    for tok, folded_tok in _orig_tokens(no_urls):
        if _code_like(tok) and folded_tok not in hay.tokens:
            return "code_not_grounded"
    # A code joined by - _ . ("NIGHT-15") is checked whole, not part by part;
    # a run with a lower-case letter is a compound word ("365-night").
    for m in _JOINED_RE.finditer(no_urls):
        run_ = m.group()
        parts = [x for x in re.split(r"[^A-Za-z0-9]+", run_) if x]
        if (any(_code_like(x) for x in parts) or _code_like("".join(parts))
                and any(ch.isdigit() for ch in run_)) and not any(ch.islower() for ch in run_) \
                and not _joined_ok(_tokens(run_), hay, cited):
            return "code_not_grounded"
    for m in _CODE_AFTER_RE.finditer(no_urls):
        toks = _tokens(m.group(1) or m.group(2))
        if toks and toks[0] not in _CODE_STOP and not _joined_ok(toks, hay, cited):
            return "code_not_grounded"

    attributed = set()
    for p in cited:
        for b in _ad_brands(p):
            attributed |= names.keys_for(b) | _brand_keys(names, b)
        attributed |= _spoken_backing(p, names, brand if brand_mode else None)
    # Rule 4: the read's own brand named in lower case, or as a plain word.
    lower = set().union(*(b for tup, t, b in table if t.startswith("zzl") or tup in cond))

    def credited(tab, token) -> bool:
        keys = next((b for _, t, b in tab if t == token), set())
        tup = next((u for u, t, _ in tab if t == token), None)
        return bool(keys & attributed) or (tup in cond - soft and _in_brand_name(tup, cited)) \
            or _own_read(tup, keys, text, cited, names, lower)

    if cond:
        # ASK-CHECK-WORDS: the wording is also read with the names found before
        # plain-word names were looked for, so a plain word nearer the wording
        # never shields a brand ("brought to you by Monopoly at McDonald's"
        # still ties McDonald's).
        base_found = names.find(no_url_text)
        base_table = _placeholders(base_found + [
            (tup, keys) for tup, keys in names.find(sites, any_case=True)
            if any(k[0] == "brand" for k in keys) and all(tup != t for t, _ in base_found)],
            names.find(site_text, any_case=True))
        for m in [_OUTSIDE_ADS_RE.sub(" ", _mark(_words(v), base_table))
                  for v in _readings(site_text)]:
            bound = _bound_brands(_offer_words(m, base_table, names.weak))
            if any(not credited(base_table, token) for token in bound):
                return "advertiser_not_attributed"
            if bound and len(unheld) < len(cited):
                return "held_ad_brand"
    keys_of = {token: brands for _, token, brands in table}
    plain_tokens = {token for tup, token, _ in table if tup in cond}
    backed = {token for tup, token, keys in table if tup not in cond and keys & attributed}
    # "<plain name> offers|codes|discounts" a cited talk passage says is talk.
    talk_text = [" " + " ".join(_tokens(p.text)) + " " for p in cited
                 if p.kind == "transcript_passage" and not p.in_ad]
    talk = {(token, verb) for tup, token, _ in table if tup in cond
            for verb in ("offer", "offers", "code", "codes", "discount", "discounts")
            if any(" " + " ".join(tup) + " " + verb + " " in t for t in talk_text)}
    # As in find(), plain-word names are not read in the reading with a
    # capital I read as l: there "Indeed" is "lndeed", no name, and "Pro" would
    # be the only brand near the wording (R1 round 3, grounded replay).
    no_plain = [t for t in table if t[0] not in cond]
    # Ad wording is tied to brands across the whole text, quotes included.
    for v in _readings(site_text):
        tab = table if v in (site_text, _glue(site_text)) else no_plain
        m = _offer_words(_OUTSIDE_ADS_RE.sub(" ", _mark(_words(v), tab)), tab, names.weak)
        bound = _bound_brands(m, plain_tokens, backed, talk)
        if any(not credited(table, token) for token in bound):
            return "advertiser_not_attributed"
        if bound and len(unheld) < len(cited):
            # Code cannot tell which cited ad the wording is about, so a held
            # ad next to the brand's ad could be credited to the brand.
            return "held_ad_brand"
    # ASK-AD-PRONOUN: "its / their ads" citing a held or unattributed ad, or
    # an in-ad transcript passage with no brand near (R1 MED-2: ask.py makes
    # one when the ad it overlaps is held); ad wording citing no passage
    # inside an ad.
    # A cited passage that says the wording word for word backs it (a host
    # quoted in their own words is no claim about an ad).
    # The whole item, quotes too, names as written: a verbatim quote stays
    # one run of words the passage can say. "Outside ads" words stay in the
    # windows: the passage says them too.
    full = _readings(text)
    plain = [_words(v) for v in full]
    if any((p.kind == "detected_ad" and not _ad_brands(p))
           or (p.kind == "transcript_passage" and p.in_ad and not p.near_ad_brands)
           for p in cited) \
            and any(_PRONOUN_AD_RE.search(m) for m in marked) \
            and _ad_words_unsaid(_PRONOUN_AD_RE, plain, cited):
        return "ad_pronoun_unattributed"
    if not any(p.in_ad or p.kind == "detected_ad" for p in cited):
        if (any(_AD_WORDING_RE.search(m) for m in marked)
                and _ad_words_unsaid(_AD_WORDING_RE, plain, cited)) \
                or (any(OPEN_CUE_RE.search(v) for v in readings)
                    and _ad_words_unsaid(OPEN_CUE_RE, full, cited, folded=False)) \
                or (any(_IN_THE_AD_RE.search(v) for v in readings)
                    and _ad_words_unsaid(_IN_THE_AD_RE, full, cited, folded=False)):
            return "ad_wording_no_ad"
    # ASK-RT2 item 6 (REDTEAM Q24), stricter, after the check above: ad
    # wording needs a detected ad, or an in-ad transcript passage that
    # overlaps an ad with a brand; an unbranded in-ad line (a news line
    # inside an ad slot) carries none. With such a line cited, any ad word
    # is ad wording (_AD_TALK_RE, item 6 as built and replayed); with only
    # passages outside ads, the check above decides (B's narrower wording,
    # shaped by its replay). As above, a cited passage that says the ad
    # words itself backs them; else this check would undo that one.
    if not any(p.kind == "detected_ad" or _branded_in_ad(p) for p in cited):
        rx = _AD_TALK_RE if any(p.in_ad for p in cited) else _AD_WORDING_RE
        if any(rx.search(m) for m in marked) and _ad_words_unsaid(rx, plain, cited):
            return AD_WORDING_UNBRANDED
    if brand_mode:
        if brand:
            want = names.keys_for(brand)
            for p in cited:
                if p.kind == "detected_ad" and not any(names.keys_for(b) & want
                                                       for b in _ad_brands(p)):
                    return "brand_mode_unrelated_ad"
        if section == "ads":
            want = names.keys_for(brand) if brand else set()
            if not any(p.kind == "detected_ad" and not p.held
                       and any(names.keys_for(b) & want for b in _ad_brands(p)) for p in cited):
                return "brand_mode_no_brand_ad"
        elif all(p.in_ad or p.kind == "detected_ad" for p in cited):
            return "brand_mode_no_mention"
        return None   # brand mode has no F6 (the sections decide)
    if all(p.in_ad or p.kind == "detected_ad" for p in cited) \
            and any(F6_TALK_RE.search(m) for m in plain_marked + [
                _mark(_words(v), [t for t in table if t[0] not in cond]) for v in readings if cond]) \
            and not set(plain_marked[0].split()) & F6_AD_WORDS:
        return "mentioned_all_ads"
    return None


# ---------------------------------------------------------------- display names

# A citation marker: anything in square or lenticular brackets, or in
# parentheses with a passage id ("(p1 ad · chim)").
_CITE_MARK_RE = re.compile(r"\[[^\]]*\]|\u3010[^\u3011]*\u3011|\([^()]*\bp\d+\b[^()]*\)", re.I)


_POSSESSIVE_RE = re.compile(r"(?:['\u2019]s)?")
# r2 MED-A: what lies between a key and its own display name for the two to
# be next to each other: spaces and ( ) , / - \u2013 \u2014 : ; &, with at most
# one "or" or "and" ("Chime (chim)", "Charles Schwab, or schwab", "chim and
# Chime", "Chime: chim", "Chime & chim"; ASK-DISPLAY-NEXT-TO); never another
# word or a ".".
_NEXT_SEP = r"[\s(),/\-\u2013\u2014:;&]"
_NEXT_TO_RE = re.compile(rf"{_NEXT_SEP}+(?:(?:or|and){_NEXT_SEP}+)?", re.I)
# An article right before a rewritten word: a/an at a word start, one run of spaces.
_ARTICLE_RE = re.compile(r"(?<![^\s(])(an?)\s+$", re.I)


def _display_words(text: str, cited: list, names: NameTable) -> str:
    """ASK-DISPLAY-NAME: each whole word that is the one-word raw brand key
    of a cited, brand-identified ad, shown as that key's display name
    (NameTable.display_for: "a chim ad" -> "a Chime ad", "chim's" ->
    "Chime's"). A plural or a multi-word key stays, and so does a word in a
    quote, web address (written or read aloud), code or citation marker, a
    display name already written ("Charles Schwab", "Macy's"), or a key
    next to its own display name ("Anthropic's Claude", "Chime (chim)"). A
    directly preceding a/an follows a changed first letter ("An anthropic
    ad" -> "A Claude ad"; "a usbank ad" -> "a US Bank ad"). Keys and display
    names next to each other form a chain, rewritten whole or not at all
    ("chim, chim and Chime" stays; "chim or chim" -> "Chime or Chime")."""
    shown = {}
    for p in cited:
        for b in _ad_brands(p):
            key, display = b.strip(), names.display_for(b)
            if display and re.fullmatch(r"[^\W_]+", key):
                shown[key.casefold()] = display
    spans = _quotes(text) if shown else None
    if spans is None:
        return text
    spans += [m.span() for rx in (_URL_RE, _SPOKEN_URL_RE, _CITE_MARK_RE)
              for m in rx.finditer(text)]
    spans += [m.span() for m in _JOINED_RE.finditer(text) if not m.group().isalnum()]
    spans += [m.span(1 if m.group(1) is not None else 2) for m in _CODE_AFTER_RE.finditer(text)]
    # r1 HIGH-1: each display name as already written, in any case and with
    # either apostrophe, so it is never written twice.
    written = {d: [m.span() for m in re.finditer(
        r"(?<!\w)" + re.sub(r"['\u2019]", "['\u2019]", re.escape(d).replace("\\ ", r"\s+"))
        + r"(?!\w)", text, re.I)] for d in set(shown.values())}
    spans += [span for found in written.values() for span in found]
    # After a space or "(" (or at the start); before an end, a space, , ; : ! ? )
    # or a "." that ends a sentence, with a possessive 's between.
    word_re = re.compile(r"(?<![^\s(])(" + "|".join(map(re.escape, sorted(shown, key=len, reverse=True)))
                         + r")(?=(?:['\u2019]s)?(?:$|[\s,;:!?)]|\.(?:$|\s)))", re.I)
    found = [m for m in word_re.finditer(text) if m.group(1).casefold() in shown]
    taken = [span for spans_ in written.values() for span in spans_]
    # ASK-DISPLAY-NEXT-TO: a chain is a run of one display's keys and written
    # display names, each next to the one before (its possessive 's aside);
    # a chain with a written display name, or a key that stays anyway (code
    # like, or in a quote, address or code: R1 LOW-2), keeps every key in it.
    stay = set()
    for display in set(shown.values()):
        items = sorted([(m.start(1), m.end(1), m) for m in found
                        if shown[m.group(1).casefold()] == display
                        and not any(s < m.end(1) and m.start(1) < e for s, e in taken)]
                       + [(s, e, None) for s, e in written[display]], key=lambda t: t[:2])
        chain = []
        for item in items + [None]:
            if chain and (item is None or not _NEXT_TO_RE.fullmatch(text[
                    chain[-1][1] + len(_POSSESSIVE_RE.match(text, chain[-1][1]).group()):item[0]])):
                if any(m is None or _code_like(m.group(1))
                       or any(s < m.end(1) and m.start(1) < e for s, e in spans)
                       for _, _, m in chain):
                    stay.update(m.start(1) for _, _, m in chain if m is not None)
                chain = []
            if item is not None:
                chain.append(item)
    out, last = [], 0
    for m in found:
        a, b = m.span(1)
        display = shown[m.group(1).casefold()]
        if a in stay or _code_like(m.group(1)) or any(s < b and a < e for s, e in spans):
            continue
        before = text[last:a]
        art = _ARTICLE_RE.search(before)
        first = display[:1].lower()
        # r2 LOW-B: only when the first letter changes ("a usbank ad" -> "a US Bank ad").
        if art and first.isascii() and first.isalpha() and first != m.group(1)[:1].lower() \
                and not any(s < last + art.end(1) and last + art.start(1) < e for s, e in spans):
            word = "an" if first in "aeiou" else "a"
            word = word.capitalize() if art.group(1)[0].isupper() else word
            before = before[:art.start(1)] + word + before[art.end(1):]
        out += [before, display]
        last = b
    return "".join(out + [text[last:]])


# ---------------------------------------------------------------- the answer

def check_answer(answer: dict, passages: dict, *, names: NameTable, brand_mode: bool,
                 brand, verdict_no: bool = False, question=None) -> CheckResult:
    """Cite-or-drop: every item that fails a check is removed; `reasons`
    holds one short code per removed item, never text. `verdict_no`: a
    yes/no turn's "no" verdict after a complete search; a paragraph that
    opens with "No," stands only in UX's W6 form for `question` (the
    turn's question; _w6_form), else it is removed (VERDICT_NOT_W6) before
    any other rule. Every other rule still runs on a W6 paragraph; the
    absence rule leaves only its head's "none" (R1 LOW-3, count paths r1
    MED-1, plan review M1/M3)."""
    result = CheckResult()
    answer = answer if isinstance(answer, dict) else {}
    if not isinstance(names, NameTable):
        raise ValueError("names must be a NameTable")
    if passages is None:
        passages = {}
    if not isinstance(passages, dict):
        raise ValueError("passages must be a dict of pid -> Passage")
    for p in _passages(passages.values()):
        _brand_list(p)   # a caller's malformed brands field fails here, not mid-check
    outcome = answer.get("outcome")
    candidates = []
    for part, limit, chars, max_cites in (("paragraphs", PARAGRAPHS_MAX, PARAGRAPH_CHARS,
                                           PARAGRAPH_CITES),
                                          ("uncertain", UNCERTAIN_MAX, UNCERTAIN_CHARS,
                                           UNCERTAIN_CITES)):
        items = answer.get(part)
        for n, item in enumerate(items if isinstance(items, list) else []):
            candidates.append((part, n >= limit, chars, max_cites, item,
                               # Any "No," paragraph: one that survives first is
                               # the verdict check_verdict reads (plan review M1).
                               verdict_no and part == "paragraphs"
                               and isinstance(item, dict) and isinstance(item.get("text"), str)
                               and item["text"].startswith(VERDICT_OPENINGS["no"])))

    current = None

    def drop(code):
        result.dropped += 1
        result.reasons.append(code)
        result.removed_items.append(removed_item(code, current))

    survivors = []
    for part, over, chars, max_cites, item, is_verdict in candidates:
        current = item
        if outcome == "nothing":
            drop("outcome_nothing")
            continue
        if outcome not in ("answered", "partial"):
            drop("malformed_answer")
            continue
        if over:
            drop("over_limit")
            continue
        if not (isinstance(item, dict) and isinstance(item.get("text"), str)
                and isinstance(item.get("cites"), list)):
            drop("malformed_item")
            continue
        section = item.get("section") if brand_mode and part == "paragraphs" else None
        if brand_mode and part == "paragraphs" and section not in ("ads", "mentions"):
            drop("malformed_item")
            continue
        if len(item["text"]) > chars:
            # The raw length first, so an oversized text is never cleaned.
            drop("too_long")
            continue
        text = _clean(item["text"]).strip()
        if not text or len(text) > chars:
            drop("too_long" if text else "malformed_item")
            continue
        cites = []
        for c in item["cites"]:
            if isinstance(c, str) and c in passages and c not in cites:
                cites.append(c)
        if not cites:
            drop("no_cite")
            continue
        if len(cites) > max_cites:
            drop("too_many_cites")
            continue
        # A combining mark no cited passage has (an invisible joiner inside
        # "4\u034f7%") removes the item; one a passage has is dropped silently.
        have = set()
        for c in cites:
            p = passages[c]
            have |= _marks(p.text) | _marks(p.show) | _marks(p.episode_title)
        if _marks(item["text"]) - have:
            drop("hidden_mark")
            continue
        cited_ps = [passages[c] for c in cites]
        check = functools.partial(_check_item, cited=cited_ps, names=names, brand_mode=brand_mode,
                                  brand=brand, section=section, uncertain=part == "uncertain",
                                  verdict_no=is_verdict, question=question)
        why = check(text)
        if why:
            drop(why)
            continue
        # ASK-DISPLAY-NAME: shown with the display name only when that text
        # passes the same checks, so what is shown is what was checked.
        shown = _display_words(text, cited_ps, names)
        if shown != text and len(shown) <= chars and check(shown) is None:
            text = shown
        survivors.append((part, Item(text=text, cites=cites, section=section)))

    # F4: items never outnumber the distinct passages they cite.
    cited, count = set(), 0
    for part, item in survivors:
        current = item
        if count + 1 > len(cited | set(item.cites)):
            drop("items_exceed_passages")
            continue
        cited |= set(item.cites)
        count += 1
        (result.paragraphs if part == "paragraphs" else result.uncertain).append(item)
    return result


# ---------------------------------------------------------------- coverage

_COUNT_MAX = 10 ** 9


def _count(value):
    """A whole number from 0 to 10**9 (5 or 5.0), else None: no search
    covers more episodes, and a larger one would overflow the ratio."""
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        value = int(value)
    return value if isinstance(value, int) and not isinstance(value, bool) \
        and 0 <= value <= _COUNT_MAX else None


def verdict_complete(searches) -> bool:
    """Whether a "No," verdict may stand: coverage_outcome gives no partial
    line, and no transcript search covered 0 episodes (a window wholly
    outside the collection searched nothing; count paths r1 MED-2)."""
    if coverage_outcome(searches, True)[1] is not None:
        return False
    if any(isinstance(s, dict) and s.get("window_misses_collection") for s in searches):
        return False   # a dated search wholly outside the collection (ASK-FIX1 r1 LOW-A)
    if any(isinstance(s, dict) and s.get("covers_no_episode") for s in searches):
        return False   # an ad search whose filter matches no episode (plan review M5)
    return not any(isinstance(s, dict) and s.get("tool") == "search_transcripts"
                   and _count(s.get("episodes_in_scope")) == 0 for s in searches)


def coverage_outcome(searches: list, survived: bool):
    """(shown, server_line) by base 5.3. A search is complete when it
    searched every episode in scope with no truncation, timeout or
    un-indexed version; no searches, or a malformed one, is not complete.
    N of M are the least-covered incomplete search's own numbers, among
    searches that report both with N < M; when none does, the lines without
    numbers (LEDGER-LEAD3-ASKF-3). N > M ("12 of 9") is malformed: no
    numbers. N = 0 of M > 0 (a failed word search) has no numbers either
    (ASK-UX-R1 r1 MED-2). When every word search is complete and only detected-ads
    searches (0 of 0, truncated) were cut, PARTIAL_ADS_CUT_LINE."""
    if isinstance(searches, (str, bytes, dict)) or not hasattr(searches, "__iter__"):
        searches = []
    searches = list(searches)
    rows = [s for s in searches if isinstance(s, dict)]
    complete = bool(rows) and len(rows) == len(searches)
    worst = None
    # ASK-CHECK-SMALL: a word search covered every episode; a gap other than
    # a cut detected-ads search (ask._coverage_entry's 0 of 0).
    words_full, other_gap, ads_cut = False, not complete, False
    for s in rows:
        n, m = _count(s.get("episodes_searched")), _count(s.get("episodes_in_scope"))
        if n is not None and m is not None and n > m:
            n = m = None  # "12 of 9": malformed, no numbers
        if n == 0 and m is not None and m > 0:
            # ASK-UX-R1 r1 MED-2 (Meta's ruling, 11-02): a failed word search's
            # 0 is no number (the reached count is not known), never "0 of M".
            n = m = None
        flags = [k for k in ("truncated", "timed_out", "unindexed") if s.get(k)]
        if n is not None and m is not None and n == m and not flags:
            words_full = words_full or m > 0
            continue
        complete = False
        if n == m == 0 and flags == ["truncated"]:
            ads_cut = True
        else:
            other_gap = True
        if n is None or m is None or n == m:
            continue  # no usable numbers: never the N of M shown
        if worst is None or (n / m, n) < (worst[0] / worst[1], worst[0]):
            worst = (n, m)
    if survived:
        if complete:
            return ("answered", None)
        if worst:
            return ("partial", PARTIAL_LINE.format(n=worst[0], m=worst[1]))
        return ("partial", PARTIAL_ADS_CUT_LINE if ads_cut and words_full and not other_gap
                else PARTIAL_NO_COUNT_LINE)
    if complete:
        return ("nothing", NOTHING_FULL_LINE)
    return ("nothing", NOTHING_PART_LINE.format(n=worst[0], m=worst[1]) if worst
            else NOTHING_PART_NO_COUNT_LINE)


def based_on_line(passages_cited) -> str:
    ps = _passages(passages_cited if passages_cited is not None else ())
    try:
        n = len({p.pid for p in ps})
        m = len({p.episode_id for p in ps})
    except TypeError:
        raise ValueError("Passage.pid and episode_id must be hashable") from None
    return (f"Based on {n} passage{'s' if n != 1 else ''} in "
            f"{m} episode{'s' if m != 1 else ''}")


def answer_schema(brand_mode: bool = False, *, counts: bool = False,
                  yes_no: bool = False) -> dict:
    """The `response_format` for the final call (base 5.1 plus
    `asks_about_tone`; in brand mode each paragraph also has a required
    `section`, "ads" or "mentions"). ASK-RT2: `counts` adds the required
    count references ("c1", COUNTS_MAX at most), `yes_no` the required
    `verdict` (VERDICTS)."""
    def items(chars, cites, most, section=False):
        props = {"text": {"type": "string", "maxLength": chars},
                 "cites": {"type": "array", "minItems": 1, "maxItems": cites,
                           "items": {"type": "string", "pattern": "^p[0-9]+$"}}}
        required = ["text", "cites"]
        if section:
            props["section"] = {"type": "string", "enum": ["ads", "mentions"]}
            required.append("section")
        return {"type": "array", "maxItems": most,
                "items": {"type": "object", "additionalProperties": False,
                          "required": required, "properties": props}}
    schema = {"type": "object", "additionalProperties": False,
              "required": ["outcome", "paragraphs", "uncertain", "asks_about_tone"],
              "properties": {
                  "outcome": {"type": "string", "enum": ["answered", "partial", "nothing"]},
                  "paragraphs": items(PARAGRAPH_CHARS, PARAGRAPH_CITES, PARAGRAPHS_MAX,
                                      section=bool(brand_mode)),
                  "uncertain": items(UNCERTAIN_CHARS, UNCERTAIN_CITES, UNCERTAIN_MAX),
                  "asks_about_tone": {"type": "boolean"}}}
    if counts:
        schema["required"].append("counts")
        schema["properties"]["counts"] = {"type": "array", "maxItems": COUNTS_MAX,
                                          "items": {"type": "string", "pattern": "^c[0-9]+$"}}
    if yes_no:
        schema["required"].append("verdict")
        schema["properties"]["verdict"] = {"type": "string", "enum": list(VERDICTS)}
    return {"type": "json_schema",
            "json_schema": {"name": "ask_answer", "strict": True, "schema": schema}}


# ---------------------------------------------------------------- counts (ASK-RT1 RT-2)

COUNT_NOT_GROUNDED = "count_not_grounded"
_COUNT_WORDS = {
    "a single": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "a dozen": 12, "a hundred": 100}
# A count with no exact number: never equal to a total.
_COUNT_VAGUE = ("several", "multiple", "numerous", "many", "a few", "few", "a couple",
                "a handful", "dozens", "hundreds", "thousands", "countless", "various")
# "one" has its own pattern (_COUNT_ONE_RE).
_COUNT_NUMBER = (r"\d{1,3}(?:,\d{3})+|\d+|"
                 + "|".join(sorted(map(re.escape, [w for w in list(_COUNT_WORDS)
                                                   + list(_COUNT_VAGUE) if w != "one"]),
                                   key=len, reverse=True)))
# As COUNT_RE: up to two words between a count and its noun, not these;
# the nearest noun counts ("one brand has ads" counts a brand).
_COUNT_SKIP = (r"(?:(?!(?:the|a|an|off|in|on|at|for|to|of|with|from|by|and|or|per|percent|"
               r"dollars?|cents?|bucks)\b)[\w'’-]+ ){0,2}?")
# A noun that heads a longer noun is not what "one" counts ("one promo code",
# "one show host"). Singular words only: "one show hosts ..." counts a show
# (R1 round 4 LOW-1).
_COUNT_COMPOUND = (r"(?! (?:code|host|title|note|page|slot|break|segment|network|name|"
                   r"list)\b)")
# Nor what a plural count counts ("two promo codes", "three ad breaks"; R1
# round 3 LOW-1), but never names, titles, lists, slots or segments: "two
# brand names" counts brands (R1 round 2 LOW-1). Only after a singular noun:
# after "12 ads" the word is a verb ("12 ads notes ..."; R1 round 4 MED-1).
# "a single" and "1" take the "one" list ("a single show hosts ..." counts a
# show; R1 round 5 LOW-1). In "N of the M ...", M is a count too (`gap`),
# with or without a comma ("two of the 1,500 episodes").
_COUNT_PLURAL_COMPOUND = r"(?! (?:codes?|hosts?|notes?|pages?|breaks?|networks?)\b)"
_COUNT_TOTAL_RE = re.compile(
    rf"(?<![\w'’.,-])(?P<n>(?P<single>a single|1(?![\d,]))|{_COUNT_NUMBER}) (?P<of>of )?"
    rf"(?:(?:the|these|those) )?(?P<gap>(?(of)(?:\d{{1,3}}(?:,\d{{3}})+ )?){_COUNT_SKIP})"
    r"(?:(?P<ads>(?:ad|sponsor) reads?|ads?|adverts?|advertisements?|commercials?|promos?|"
    r"spots?|reads)|(?P<brands>brands?|advertisers?|sponsors?)|(?P<episodes>episodes?)|"
    r"(?P<shows>shows?|podcasts?))"
    rf"\b(?:(?<=s)|(?(single){_COUNT_COMPOUND}|{_COUNT_PLURAL_COMPOUND}))")
# R1 LOW-1: "one" counts an ad, brand, episode or show as such: a singular
# noun ("one host reads", "one Rocket Money ad shows how" count no reads or
# shows), never "one of the ..." ("one of the sponsors": the gap takes no "of").
_COUNT_ONE_RE = re.compile(
    rf"(?<![\w'’.,-])(?P<n>one) (?P<x>{_COUNT_SKIP})"
    r"(?:(?P<ads>(?:ad|sponsor) read|ad|advert|advertisement|commercial|promo|spot)|"
    r"(?P<brands>brand|advertiser|sponsor)|(?P<episodes>episode)|(?P<shows>show|podcast))"
    rf"\b{_COUNT_COMPOUND}")
# A count after these words matches no total ("more than one", "at most 3",
# "more than just one").
_COUNT_BOUND_RE = re.compile(
    r"\b(?:more than|less than|fewer than|at most|at least|no more than|not|not even|over|"
    r"under|up to|nearly|almost|about|around|roughly|approximately|some)"
    r"(?: (?:just|only|merely|exactly))? $")
# "One [X] ad said ...": a reference to one ad, not a count, when it starts
# the item or a sentence (R1 MED-1), a speech verb follows the noun (cb668950;
# R1 round 2 MED-1) and the item cites exactly one detected ad of brand X
# (_one_ad). "One Rocket Money ad ran ..." stays a count.
SINGLE_AD_VERBS = ("says", "said", "frames", "framed", "offers", "offered", "pitches",
                   "pitched", "promises", "promised", "claims", "claimed", "describes",
                   "described", "tells", "told", "urges", "urged", "asks", "asked",
                   "calls", "called", "highlights", "highlighted", "promotes", "promoted",
                   "touts", "touted", "notes", "noted", "adds", "added", "explains",
                   "explained", "mentions", "mentioned", "shows how", "showed how")
_SINGLE_AD_VERB_RE = re.compile(rf" (?:{'|'.join(SINGLE_AD_VERBS)})\b")
_START_RE = re.compile(r"(?:^|[.;:] )$")
# "In one episode, ..." starting the item or a sentence, comma and all, may
# set the scene of its one cite (R1 LOW-1); any other "one episode/show" is a
# count no tool can ground (R1 round 2 MED-2).
_IN_ONE_EPISODE_RE = re.compile(r"(?:^|[.;:] )in $")
# The kind of total a count needs (ask.py: list_brands gives "brands",
# search_evidence "ads"; no tool gives an episode or show count).
COUNT_KINDS = ("brands", "ads", "episodes", "shows")


def count_claims(text) -> list:
    """The counts of brands, ads, episodes or shows an item states outside
    its quotes ("one different brand", "a single brand", "12 ads", "in 5
    episodes"), in order: an int each, or None for a count with no exact
    number ("several brands", "more than one ad"). "A Saatva ad ran on ...",
    "one of the sponsors", "one host reads", "two hosts read" and "one promo
    code" state none; a date or a clock time is no count."""
    return [n for _kind, n, _ref in _count_items(text)]


def _count_items(text) -> list:
    """count_claims as (kind, n, ref), kind one of COUNT_KINDS. ref, for a
    "one" that may refer rather than count (check_counts decides from the
    cites): ("ad", X words) for "One [X] ad said ..." starting the item or a
    sentence; ("episode",) for "In one episode," there; else None."""
    if not isinstance(text, str):
        return []
    spans = _quotes(text) or []
    for a, b in reversed(spans):
        text = text[:a] + " " + text[b:]
    text = " ".join(_DATE_RE.sub(" ", _TIME_RE.sub(" ", _clean(text))).lower().split())
    found = []
    for m in list(_COUNT_TOTAL_RE.finditer(text)) + list(_COUNT_ONE_RE.finditer(text)):
        n, before = m.group("n"), text[:m.start()]
        kind = next(k for k in COUNT_KINDS if m.group(k))
        value = int(n.replace(",", "")) if n[0].isdigit() else _COUNT_WORDS.get(n)
        if _COUNT_BOUND_RE.search(before):
            value = None
        ref = None
        if n == "one" and value is not None:
            if kind == "ads" and _START_RE.search(before) \
                    and _SINGLE_AD_VERB_RE.match(text, m.end()):
                ref = ("ad", tuple(_tokens(m.group("x"))))
            elif kind == "episodes" and not m.group("x") and text[m.end():m.end() + 1] == "," \
                    and _IN_ONE_EPISODE_RE.search(before):
                ref = ("episode",)
        found.append((m.start(), kind, value, ref))
        # NOGAP-1: "two of the 12 brands" also counts the 12 brands; after a
        # bound word ("over 40") the number matches no total, and "one" there
        # is no count ("one hundred"; RT1 round 6 LOW-1, LOW-2).
        if "of" in m.groupdict() and m.group("of"):
            at = m.start("gap")
            for word in m.group("gap").split():
                digits = word.replace(",", "")
                if digits.isdigit() or word in _COUNT_WORDS and word != "one":
                    found.append((at, kind, None if _COUNT_BOUND_RE.search(text[:at])
                                  else int(digits) if digits.isdigit()
                                  else _COUNT_WORDS[word], None))
                at += len(word) + 1
    return [(kind, value, ref) for _start, kind, value, ref in sorted(found)]


def _one_ad(item, x, passages, names) -> bool:
    """Whether "One [X] ad said" in `item` refers to the one ad it cites:
    exactly one cite, a detected ad with a brand (not held), and X a spelling
    of that brand; with X unnamed, the item names that brand. A spelling is
    the name's words or the same letters run together ("draft Kings" for
    draftkings)."""
    if not isinstance(names, NameTable) or not isinstance(passages, dict):
        return False
    cites = list(dict.fromkeys(item.cites))
    p = passages.get(cites[0]) if len(cites) == 1 else None
    if not isinstance(p, Passage) or p.kind != "detected_ad" or p.held:
        return False
    spell = {tuple(s) for b in _ad_brands(p) for s in names.spellings(b) if s}
    joined = {"".join(s) for s in spell}

    def brand(toks) -> bool:
        return bool(toks) and (tuple(toks) in spell or "".join(toks) in joined)
    if x:
        return brand(x)
    toks = _tokens(item.text)
    return any(brand(toks[i:j]) for i in range(len(toks))
               for j in range(i + 1, min(i + 4, len(toks)) + 1))


def _refers(item, ref, passages, names) -> bool:
    """Whether a "one" (_count_items ref) refers to what the item cites: "One
    [X] ad said" to its one cited X ad (_one_ad); "In one episode," to the
    episode of its one cited passage."""
    if ref is None:
        return False
    if ref[0] == "ad":
        return _one_ad(item, ref[1], passages, names)
    cites = list(dict.fromkeys(item.cites))
    return isinstance(passages, dict) and len(cites) == 1 \
        and isinstance(passages.get(cites[0]), Passage)


def check_counts(result: CheckResult, totals, passages=None, names=None) -> CheckResult:
    """ASK-RT1 RT-2 (d), after check_answer: a paragraph or uncertain item
    stays only when every count it states equals a total of its own kind a
    tool returned in this turn (`totals`, (kind, int) pairs); otherwise it
    is removed (count_not_grounded). "One [X] ad said" citing one X ad is
    no count, nor is "In one episode, ..." citing one passage (_refers, which needs the cited `passages` by pid and the
    NameTable; without them each is a count). Only adds removals; in place,
    and returns `result`."""
    have = {t for t in totals or () if isinstance(t, tuple) and len(t) == 2
            and t[0] in COUNT_KINDS and type(t[1]) is int}
    for part in ("paragraphs", "uncertain"):
        kept = []
        for item in getattr(result, part):
            if any(not _refers(item, ref, passages, names)
                   and (n is None or (kind, n) not in have)
                   for kind, n, ref in _count_items(item.text)):
                result.dropped += 1
                result.reasons.append(COUNT_NOT_GROUNDED)
                result.removed_items.append(removed_item(COUNT_NOT_GROUNDED, item))
                continue
            kept.append(item)
        setattr(result, part, kept)
    return result


# ---------------------------------------------------------------- ASK-RT2 (REDTEAM-30, UX W1-W6)

# UX's words, byte for byte: ws-ux/win/REDTEAM-30-DIAGNOSIS.md (0b69757e2; W6
# 2bfaeee8b) and the scoped forms in ws-ux/win/ASK-RT2-STRINGS.md (55cd12f60).
# W1: one brand's count, by scope (category, show, dates).
COUNT_BRAND_LINES = {
    (False, False, False): "{brand} has {n} ads in {e} episodes across {s} shows in this collection.",
    (False, False, True): ("From {from} to {to}, {brand} has {n} ads in {e} episodes across {s}"
                           " shows in this collection."),
    (True, False, False): ("On {category} shows in this collection, {brand} has {n} ads in {e}"
                           " episodes across {s} shows."),
    (True, False, True): ("On {category} shows in this collection, from {from} to {to}, {brand}"
                          " has {n} ads in {e} episodes across {s} shows."),
    (False, True, False): "On {show}, {brand} has {n} ads in {e} episodes.",
    (False, True, True): "On {show}, from {from} to {to}, {brand} has {n} ads in {e} episodes.",
}
COUNT_BRAND_LINE = COUNT_BRAND_LINES[(False, False, False)]
COUNT_BRAND_LINK = "See them in Search"
# W1-zero: no ad found for the brand in the scope (ASK-RT2-STRINGS.md (d),
# 584d26d38); never a Search link. ZERO_UNIDENTIFIED follows only when the
# same scope has an ad with brand not identified.
ZERO_BRAND_LINES = {
    (False, False, False): "We found no ads for {brand} in this collection.",
    (False, False, True): "From {from} to {to}, we found no ads for {brand} in this collection.",
    (True, False, False): "On {category} shows in this collection, we found no ads for {brand}.",
    (True, False, True): ("On {category} shows in this collection, from {from} to {to}, we found"
                          " no ads for {brand}."),
    (False, True, False): "On {show}, we found no ads for {brand}.",
    (False, True, True): "On {show}, from {from} to {to}, we found no ads for {brand}.",
}
ZERO_UNIDENTIFIED = "Some ads here show brand not identified, so we can't rule {brand} out."
# W2: a ranking, by scope (category, show).
COUNT_RANKING_LINES = {
    (False, False): "Most ads in this collection, {from} to {to}:",
    (True, False): "Most ads on {category} shows in this collection, {from} to {to}:",
    (False, True): "Most ads on {show}, {from} to {to}:",
}
COUNT_ROW = "{name} · {n} ads"
# W1c: how many different brands, by scope (category, show), always followed
# by BRAND_COUNT_NOTE.
BRAND_COUNT_LINES = {
    (False, False): "{n} brands have ads in this collection, {from} to {to}.",
    (True, False): "{n} brands have ads on {category} shows in this collection, {from} to {to}.",
    (False, True): "{n} brands have ads on {show}, {from} to {to}.",
}
BRAND_COUNT_NOTE = "Ads with brand not identified are not counted."
# ASK-30 F: a count window wholly outside the collection, or partly (a note
# after the first line), with the collection's dates.
WINDOW_EMPTY_LINE = "No episodes in this collection were published {window}; it {edge}."
WINDOW_AFTER = "This collection has no episodes after {last}."
WINDOW_BEFORE = "This collection has no episodes before {first}."
WINDOW_BOTH = "This collection runs from {first} to {last}."
READ_PASSAGES_LINE = ("We found passages about this, but we couldn’t back any claim with their "
                      "exact words, so we left the claims out. Read the passages:")
TIME_LINE = ("This collection runs from {first} to {last}. Newer episodes aren’t in it "
                   "yet.")
SPEND_LINE = ("We don’t know what brands spend or how many people heard an ad. We can count "
              "the ads we heard:")
VERDICT_OPENINGS = {"yes": "Yes,", "no": "No,", "no_partial": "Not in the episodes we searched:"}

# Any opening a verdict could have, in any case ("Yes.", "no -"): only the
# standing verdict's own first paragraph may have one.
# A verdict word opening an item, after any emphasis or quote marks ("**No**,",
# "\u201cNo,\u201d"), and the bare no forms "Nope", "Not at all" (R1 LOW-2).
_VERDICT_WORD_RE = re.compile(r"[*_\"'\u201c\u2018\s]*"
                              r"(?:yes|no|nope|not at all|not in the episodes we searched)\b", re.I)
AD_WORDING_UNBRANDED = "ad_wording_unbranded"


# Any ad word, as ad wording stood before ASK-CHECK-SMALL narrowed it
# (7a3ca98d, byte for byte): item 6's trigger on an unbranded in-ad line.
_AD_TALK_RE = re.compile(r"(?<!\bnon )(?<!\bnot a )(?<!\bnot an )(?<!\bisnt a )(?<!\bisnt an )"
                         r"(?<!\bwasnt a )(?<!\bwasnt an )\b(?:ads?|adverts?|advertis\w*|"
                         r"sponsor\w*|promos?|commercials|(?:an?|the|this|that|one) commercial)\b")


def _branded_in_ad(p: Passage) -> bool:
    """An in-ad transcript passage that overlaps a detected ad with an
    attributed brand (near_ad_brands; "?" is an unknown overlap)."""
    return p.kind == "transcript_passage" and p.in_ad \
        and any(b and b != "?" for b in p.near_ad_brands or ())


VERDICTS = ("yes", "no", "not shown")
COUNTS_MAX = 4
VERDICT_MISMATCH = "verdict_mismatch"

# Code decides these from the question, never the model (pinned).
TIME_WORDS = ("this week", "last week", "recently", "right now", "today", "yesterday", "new",
              "newest", "latest", "this month", "last month")
SPEND_WORDS = ("spend", "spends", "spent", "spending", "budget", "budgets", "cost", "costs",
               "roi", "revenue", "revenues", "reach", "downloads", "impressions")
# "Listeners" asks about reach only in these phrases (Meta, after round 1).
REACH_PHRASES = ("how many listeners", "number of listeners", "listener count", "listenership",
                 "audience size")
YES_NO_STARTS = ("did", "does", "do", "is", "are", "was", "were", "has", "have", "had", "can",
                 "could", "will", "would", "should")


def _question_words(text) -> str:
    return " " + " ".join(_TOKEN_RE.findall(_clean(text if isinstance(text, str) else "")
                                            .lower().translate(_APOS))) + " "


def is_time_question(text) -> bool:
    words = _question_words(text)
    return any(" " + w + " " in words for w in TIME_WORDS)


def is_spend_question(text) -> bool:
    words = _question_words(text)
    return any(" " + w + " " in words for w in SPEND_WORDS + REACH_PHRASES)


def is_yes_no_question(text) -> bool:
    words = _question_words(text).split()
    return bool(words) and words[0] in YES_NO_STARTS


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def long_date(day: str) -> str:
    """A YYYY-MM-DD day as the Brand page writes one (longDate: "Oct 21, 2026")."""
    y, m, d = day[:10].split("-")
    return f"{_MONTHS[int(m) - 1]} {int(d)}, {int(y)}"


def _fill(template: str, values: dict) -> str:
    """The template with the Brand page's words for counts: "{n} ads" is
    "1 ad" for 1, "1,500 ads" with a separator; "{n} brands have" is "1
    brand has"."""
    out = template
    for key, word in (("n", "ad"), ("e", "episode"), ("s", "show")):
        if key in values and "{%s} %ss" % (key, word) in out:
            n = values[key]
            out = out.replace("{%s} %ss" % (key, word), f"{n:,} {word}" + ("" if n == 1 else "s"))
    if "n" in values and "{n} brands have" in out:
        n = values["n"]
        out = out.replace("{n} brands have", f"{n:,} brand has" if n == 1 else f"{n:,} brands have")
    for key, value in values.items():
        out = out.replace("{%s}" % key, value if isinstance(value, str) else f"{value:,}")
    return out


def count_brand_line(display: str, ads: int, episodes: int, shows: int) -> str:
    """W1 (no scope) with the Brand page's singular words for 1."""
    return _fill(COUNT_BRAND_LINE, {"brand": _field(display), "n": ads, "e": episodes,
                                    "s": shows})


def _day_range(a: str, b: str) -> str:
    """Two YYYY-MM-DD days as "Oct 26 to Nov 1, 2026" (the year once when
    both share it)."""
    la, lb = long_date(a), long_date(b)
    return f"{la.rsplit(',', 1)[0]} to {lb}" if a[:4] == b[:4] else f"{la} to {lb}"


def count_row(name: str, ads: int) -> str:
    return _fill(COUNT_ROW, {"name": _field(name), "n": ads})


def count_lines(result, dates) -> list:
    """The lines one count_ads result shows (ASK-RT2 item 1; UX W1, W2, W1c),
    each {"kind", "text"} plus "rows" (a ranking) and, when Search can
    carry the same filters, "link" and "search" (a Query2). The scope comes
    first: a category scope needs exactly one category, a show scope exactly
    one show, and a category with a show gets no line (UX). A brand count
    gives W1; any other count W1c (rank_by brands) and W2. `dates`: the
    collection's (first, last) day, or None (then no W2 or W1c)."""
    if not isinstance(result, dict):
        return []
    f = result.get("filter") or {}
    cats = result.get("categories") or []
    titles = result.get("show_titles") or []
    on_cat, on_show = bool(f.get("category")), bool(f.get("show_query"))
    category = _category_name(f.get("category"), cats, result.get("category_episodes")) \
        if on_cat else None
    if (on_cat and category is None) or (on_show and len(titles) != 1) or (on_cat and on_show):
        return []
    dated = bool(f.get("published_from") or f.get("published_to"))
    first = last = None
    if dates:
        first = max(f.get("published_from") or dates[0], dates[0])
        last = min(f.get("published_to") or dates[1], dates[1])
    elif dated:
        first, last = f.get("published_from"), f.get("published_to")
    asked_from, asked_to = f.get("published_from"), f.get("published_to")
    if first and last and first > last:
        # The dates fall outside the collection: say so with its dates
        # (ASK-30 F; it gave no line, R1 MED-1).
        if not dates:
            return []
        window = _day_range(asked_from, asked_to) if asked_from and asked_to else \
            f"on or after {long_date(asked_from)}" if asked_from else \
            f"on or before {long_date(asked_to)}"
        edge = f"runs to {long_date(dates[1])}" if asked_from and asked_from > dates[1] \
            else f"starts on {long_date(dates[0])}"
        return [{"kind": "window_empty",
                 "text": WINDOW_EMPTY_LINE.format(window=window, edge=edge)}]
    # A window partly outside the collection: the line's dates are the
    # collection's part, and a note after the first line says where it ends.
    early = bool(dates and asked_from and asked_from < dates[0])
    late = bool(dates and asked_to and asked_to > dates[1])
    note = None
    if early or late:
        note = (WINDOW_BOTH if early and late else WINDOW_BEFORE if early else WINDOW_AFTER).format(
            first=long_date(dates[0]), last=long_date(dates[1]))

    def clipped(out: list) -> list:
        if note and out:
            out[0]["text"] += " " + note
        return out
    values = {}
    if on_cat:
        values["category"] = _field(category)
    if on_show:
        values["show"] = _field(titles[0])
    search = {"mode": "ads"}
    if on_cat:
        search["categories"] = list(cats)
    if on_show:
        ids = result.get("show_ids") or []
        search = search if len(ids) == 1 and ids[0] else None
        if search is not None:
            search["shows"] = list(ids)
    if dated and search is not None:
        search.update({k: f[v] for k, v in (("from", "published_from"), ("to", "published_to"))
                       if f.get(v)})
    if f.get("brand"):
        b, zero = result.get("brand"), result.get("zero")
        if not (isinstance(b, dict) or isinstance(zero, dict)) or (dated and not (first and last)):
            return []
        if dated:
            values.update({"from": long_date(first), "to": long_date(last)})
        if not isinstance(b, dict):
            values["brand"] = _field(zero["display"])
            text = _fill(ZERO_BRAND_LINES[(on_cat, on_show, dated)], values)
            if zero.get("unidentified"):
                text += " " + _fill(ZERO_UNIDENTIFIED, values)
            return clipped([{"kind": "brand_zero", "text": text}])
        values.update({"brand": _field(b["display"]), "n": b["ads"], "e": b["episodes"],
                       "s": b["shows"]})
        line = {"kind": "brand", "text": _fill(COUNT_BRAND_LINES[(on_cat, on_show, dated)],
                                               values)}
        if search is not None:
            line.update(link=COUNT_BRAND_LINK, search=dict(search, brands=[b["brand"]]))
        return clipped([line])
    if not (first and last):
        return []
    values.update({"from": long_date(first), "to": long_date(last)})
    out = []
    if result.get("rank_by", "brands") == "brands":
        out.append({"kind": "brands", "text": _fill(BRAND_COUNT_LINES[(on_cat, on_show)],
                                                    dict(values, n=result["total"]["brands"]))
                    + " " + BRAND_COUNT_NOTE})
    rows = result.get("rows") or []
    if rows:
        out.append({"kind": "ranking", "text": _fill(COUNT_RANKING_LINES[(on_cat, on_show)],
                                                     values),
                    "rows": [count_row(r["name"], r["ads"]) for r in rows]})
    return clipped(out)


def _category_name(word, cats, episodes=None):
    """The {category} of a count line, or None (no line). One category
    filter word is one scope, however many categories hold it (owner, R1:
    "comedy" holds Comedy, Comedy Fiction, Comedy Interviews and comedy).
    Named as most episodes spell the category the word is (spellings that
    differ only in case, spaces or punctuation are one category); else as the categories spell the
    word when it is a whole word of each ("crime": True Crime, Crime
    Fiction); else None (a part of a word: "comed")."""
    if not isinstance(word, str) or not word.strip() or not cats:
        return None
    eps = episodes or {}

    def most(names):
        return min(names, key=lambda c: (-eps.get(c, 0), c))
    from .ask_tools import fold_category   # count_ads' own match (red-team q4)
    folded = fold_category(word)
    if len({fold_category(c) for c in cats}) == 1:
        return most(cats)
    same = [c for c in cats if fold_category(c) == folded]
    if same:
        return most(same)
    rx = re.compile(r"(?<!\w)" + re.escape(word.strip()) + r"(?!\w)", re.I)
    found = [rx.search(c) for c in cats]
    if all(found):
        return found[cats.index(most(cats))].group()
    return None


def check_verdict(result: CheckResult, verdict, complete: bool) -> str:
    """ASK-RT2 item 5 (W6), after every other check: the verdict stands
    only when the first surviving paragraph opens with its words: "Yes,";
    "No," after a complete search; "Not in the episodes we searched:" when
    coverage was partial (a bare "No," then is removed). Otherwise the
    verdict is "not shown". Then every item that opens with a verdict's
    words other than the standing verdict's own first paragraph is removed
    (VERDICT_MISMATCH). Code adds no word. Returns the verdict that stands;
    `result` is changed in place."""
    want = {"yes": VERDICT_OPENINGS["yes"],
            "no": VERDICT_OPENINGS["no" if complete else "no_partial"]}.get(verdict)
    first = result.paragraphs[0] if result.paragraphs else None
    stands = want is not None and first is not None and first.text.startswith(want)
    for part in ("paragraphs", "uncertain"):
        kept = []
        for n, item in enumerate(getattr(result, part)):
            own = stands and part == "paragraphs" and n == 0
            if not own and _VERDICT_WORD_RE.match(item.text.lstrip()):
                result.dropped += 1
                result.reasons.append(VERDICT_MISMATCH)
                result.removed_items.append(removed_item(VERDICT_MISMATCH, item))
                continue
            kept.append(item)
        setattr(result, part, kept)
    return verdict if stands else "not shown"
