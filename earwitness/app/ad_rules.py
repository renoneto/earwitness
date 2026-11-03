"""Import-side ad rules (ADRULES, lead assignment 2026-10-24).

Pure functions over the namer's named reads (the named-reads.json row
shape: anchor, brand, brand_code, brand_raw, brand_spoken, brand_url,
candidate_id, confidence, needs_review, offer_codes, urls, ep_id, start_s,
end_s, excerpt, fetch_date, provenance, source, ...), the episodes'
transcript records (segments, fetch_date, fetch_date_source, show_title),
the checked brand list and, when known, each show's identity. `apply`
returns the rows an import loads. Not to be confused with the `ad_rules`
table of operator corrections (corrections.py): nothing here reads or
writes a workspace.

Rules, each with an on/off switch (`apply(..., switches={"R3": False})`;
DEFAULT_SWITCHES has all on but R5, R6, R7, R9, R10, R11, R1b-spoken and R8-signoff):

- R1 self-promo: a read promoting the show itself (its own name or domain,
  a call to action toward it, "our Patreon", "my book", tour dates, ...)
  gets `self_promo` true and `self_promo_reason` "R1:<why>". These are the
  fields the importer already reads (import_sources.candidate_attrs,
  ad_reads.named_columns); customer._customer_ad_rows leaves self-promo out
  of customer ads by default. The rules are earwitness.pipeline.self_promo
  .classify, reused as they are (the self-promo filter, 8ebbd26), on the
  read's own transcript text, with the show identity when the caller has
  one, else one built from the show title; plus one clause of its own: the
  show's own calls (OWN_CALL_RE: rate and review, my book, tour dates, ...;
  OWN_CALL_SHOW_RE: our website or store, follow us on, a production
  credit, ad-free, bonus episodes, only when the read also says the show's
  own name or domain; a one-word show name must be written capitalized) in a
  read that names no checked brand and has no STRONG cue. R1 judges the
  namer's own pick (brand_raw, else the brand_spoken before R4/R5), so a
  second apply judges the same brand. Same-network promos for other
  shows stay ads here (classify's rule, and the labels' "network promo"
  read kind); the operator's network self-promo rule
  (corrections.self_promo_reads) marks them on every load.
- R2 held reads: a read held for brand review (needs_review) whose brand is
  checked shows that brand (needs_review becomes false) when the read's own
  words name it: spoken, or in a web address or code (the row's excerpt
  when the episode has no transcript). The hold means the namer doubted its
  pick; the checked list says the name exists, not that this read says it
  ("bloomberg" heard on a Bloom Energy spot stays held).
- R3 URL/code: a web address or promo code in the read text that names one
  checked brand fills brand_url / brand_code (the importer's url / code
  brand channels), replacing an unchecked value there (kept in "was").
  Platforms where shows send listeners and retailers that carry other
  brands' products are exceptions (EXCEPTIONS). An address or
  code in the read's first EDGE_S seconds does not count: an ad closes on
  its address, so one at a read's very start is the previous ad's close
  that the detected boundary cut into (DET-BOUND).
- R4 strict spoken: when exactly one checked brand is spoken in the read
  at least twice, and at least one of those mentions lies inside the read
  (not within EDGE_S of either end, where the neighbouring ads bleed in),
  it becomes brand_spoken (the namer's pick stays in brand and brand_raw).
- Spoken names (R2, R4, R1b, R5): a name said only inside a list of three
  or more names ("brands like Nestle, Quaker, Pepsi, and Poppy") is not
  spoken: a store's ad lists the brands it carries, which are not the
  advertiser (the RETAILERS rationale). A possessive counts as the name; a
  multi-word name never spans a sentence end.
- R1b sponsor cues: a cue cluster (CUES; cues within CUE_MERGE_S merge) that
  no input named read covers (+-CUE_COVER_S) becomes a new ad row only when
  the cue names a checked brand: a web address or code in the cue text, or
  (sub-switch "R1b-spoken", off by default) exactly one checked brand
  spoken in the row's window. Only reads shown as ads cover a cue: a
  self-promo read does not, a held read does. A WEAK cue counts only in a
  segment that itself names a checked brand (cue_tier). The row spans the
  cue segments that name its brand (address, code or the name said; the
  spoken route: the whole cue chain) +-CUE_PAD_S on whole transcript
  segments, cut at the window edge when a segment runs past it (a host read
  runs 30-60 s with its cue at either end; whole segments start and end on
  sentence edges; the cap and CUE_PAD_S < CUE_COVER_S keep it off every
  other read, and a chain of sponsors' cues gives a row only its own
  brand's part). Its fetch_date is the transcript record's, with its basis in
  provenance["fetch_date"]. A row named by a web address or code has no
  `brand` (no name was heard; the importer reads brand_spoken or brand as
  the heard name); a spoken-route row's brand is the name heard. Its `urls`
  (the app's "Address heard") holds the address itself ("quince.com/bible");
  the raw match stays in its provenance. Its urls and offer_codes hold only
  its own brand's (not the previous ad's close). With R1
  on, a cue row R1 would mark as self-promo is not added.

- R5 adjacency (owner addition): a read with no checked brand directly
  next to a read that shows one (gap <= ADJACENT_S, either side, same
  episode), whose own text names that same brand, takes it (brand_url /
  brand_code when a web address or code names it, else brand_spoken),
  replacing an unchecked value there (kept in "was"). The neighbour's
  provenance is recorded; rows are never merged.

- R8 clip guard (CLIPGUARD, ws-fresh/adrules/BOUND1.md section 1, variant
  V3): a read whose clip file window [start - CLIP_LEAD_S, end +
  CLIP_TAIL_S] (scripts/clip_package.py LEAD_S / TAIL_S, the padding every
  demo clip has) holds another ad gets `clip_hidden` true; every other row
  it judges gets `clip_hidden` false (a row with no transcript record gets
  no verdict). The build lists the reads judged clean (ad_reads.CLIP_SHOWN)
  and every clip link stays; the web's clip guard
  (EARWITNESS_AD_RULES_CLIP_GUARD, ad_reads.clip_guard_on) makes the clip
  lookups refuse every read not on that list (ad_reads.clip_for,
  media.clip_for_publication, excerpt_clip_for, customer._clip_playable).
  Another ad is, in the transcript segments overlapping the window by more
  than CLIP_OVERLAP_S (B = the read's final shown brand, else the heard
  name): an opening sponsor cue (OPEN_CUES) in a segment that, with the
  segment after it, does not name B ("open"); a web address or promo code
  of a checked brand other than B ("url"); a checked brand other than B
  said at least twice ("spoken"); a spoken web address that is neither B
  nor B misheard (similarity >= CLIP_NEAR) nor a platform or retailer
  ("address"). It runs last, on every row (R1b rows too), and never
  changes a brand field; its entry records the window, B and each signal's
  reason. Hiding is a flag: no clip file is removed, and no link changes.
  Signoff (DETBUILD, switch "R8-signoff", in DET_RULES, off unless the DET
  switch turns it on): when no other signal fires, the clip is hidden if the
  window holds the read's own address in at least two segments and, after
  the last of them, more than half the window is speech that never names B.

- R6 split (DETBUILD, rule A2): after R1-R5, a read with a transcript is cut
  at each opening sponsor cue (OPEN_CUE_RE) that starts more than
  SPLIT_EDGE_S after its start and before its end; cuts closer than
  SPLIT_EDGE_S to the previous cut merge. Same-ad guard: a cut is skipped
  when the text on both sides says the read's brand (a repaired spelling does
  not count here: the next ad's opener often shares a segment with this
  read's misheard address) and every checked brand
  either side names (address, code or spoken) is that brand. The part whose
  text says the read's brand most (spoken, address, code, or its letters;
  a spelling the namer repaired to the brand, "repair:quints->quince" in
  its candidate's evidence, counts too: DET-SPANS-2; ties to the earliest;
  no mention at all: the first) is the keeper: the
  row itself, with display_start_s / display_end_s set to its part's bounds
  and an R6 entry; its start_s, end_s and every other field stay. A lead
  part (before the first cut) that is not the keeper, has no ad words (no
  checked brand, no STRONG cue, address or code) and says the show's own
  name (_says_show) is the episode's intro: no row, its bounds in the
  entry's "not_ad" (DET-SPANS-2). Each other
  part is a new row (source R6_SOURCE, candidate_id
  "<id>.split-<ms>-<ms>", start_s / end_s the part's bounds), a copy of the
  namer's row with no brand, no hold, no self-promo or clip verdict, no
  namer judgement (confidence "low", confidence_score None, no
  provenance["candidates"]), its other provenance and fetch_date kept, and
  an R6 entry {"split_of", "part",
  "parts", "was", "cuts"} first; R1-R4 then name it from its own words. R5
  does not run on parts. R1b sees the namer's rows, never the parts.
- R7 end trim (DETBUILD, BOUND1 rule B, sides=end): a row (not an R1b or R9
  row) whose bounds (display bounds, else start_s / end_s) end on a whole
  minute ends, for display, at the last segment that says its brand,
  running on through later segments to the last one with ad evidence (a
  STRONG cue, a web address, a code) up to the first opening cue that does
  not name the brand, or the first segment that names another checked brand
  (address, code or spoken; DET-SPANS-2: when that stop leaves the read
  under TRIM_MIN_S, the run-on ignores it); with no brand, at the last segment with ad evidence
  when the read has a STRONG cue. It does nothing when the brand is said in
  the TRIM_LOOK_S seconds past the end, or when the read would be shorter
  than TRIM_MIN_S. Only display_end_s moves (display_start_s is the start);
  start_s and end_s never change.
- R9 address route (DETBUILD, DET-PLAN S2): a web address or promo code of a
  checked brand in a transcript segment with no STRONG cue within
  ADDRESS_QUIET_S either side is a fire (fires of one brand within
  ADDRESS_QUIET_S merge). An ad ends on its address, often just past the
  row's end, so each fire passes three guards (rows that are not self-promo):
  (1) with R1 on, address segments that say the show's name are the show's
  own address: nothing happens; (2) a row within ADDRESS_QUIET_S either side
  that already shows the fire's brand is the same read: nothing happens;
  (3) the fire belongs to the latest-starting row that started at least
  EDGE_S before it and ended no earlier than EDGE_S before it (the R3 edge
  rule): that row gets brand_url / brand_code when it shows no brand and has
  no checked brand, else nothing; with no such row, a fire that overlaps a
  row (in its first EDGE_S: the previous ad's close) does nothing. A fire
  that touches no row is a new row (anchor "address", source R9_SOURCE) in
  the shape of an R1b row, spanning the fire +-CUE_PAD_S on whole segments
  and clamped between its neighbours; with R1 on, a self-promo one is not
  added.
  R9 reads no spoken name and never writes brand_spoken.

- R10 same ad (DET-SPANS-2, item 5): after R7, a namer second-brand row
  (".b-" in its id, an R6 part of one included) that shows the brand a row of
  its own read shows (the read, its parts, an earlier second-brand row) over
  more than half of its own span gets `same_ad_as` (that row's id) and an
  R10 entry; so does each brandless R6 part of a marked row when a kept row
  of the read covers more than half of the part's own span (`same_ad_as` is
  the kept row with the largest overlap). The importer leaves such a row
  out, so one ad is one row.

- R11 said address (DETBUILD, ADDR-MISS case 1): after R7, a row that shows
  a checked brand (not held, not self-promo), has no `urls` and is not an
  R1b or R9 row (those write their own) gets `urls`: the web addresses its
  own words (its display bounds, else start_s / end_s) hold that name its
  brand (url_addresses, "own"), plus each address the transcript misspells
  for that brand on the curated list SAID_REPAIRS_PATH ("heard"), written as
  heard ("quints.com" on a Quince read; never rewritten to the brand's own
  address). Own addresses count only when their label spells one of the
  checked list's own names for the brand (_own_name: a display-list name,
  or an advertiser-list key or name); an alias, a namer spelling or any
  other brand is written only from the curated list. No own address is
  written right after "for" ("for hers.com"), or when its label is the tail
  of a longer name the row says ("Henry Ford.com" is not ford.com); a
  spoken path whose first word is a call-to-action word ("slash get flex"),
  or that the words do not end ("slash Washington Wise"), is left off
  (_path_after). A row whose brand_url names another brand is
  left alone. Only `urls` changes; brand_url stays as it was. The namer
  writes no address, so without R11 a namer read's `urls` is always [].
  With the same switch an R1b or R9 row's urls follow the same own-name
  rule (said_urls), and a spelled-out address wins (url_addresses `spell`,
  ADDR-MISS case 2); with it off every row's urls are as before.

Output order: the input rows in order (with a DET rule on, less any row
whose source is a rule's, ADDED_SOURCES), then the new rows by episode
(sorted ep_id): R6 parts, then R1b rows, then R9 rows. R8 runs last on all.

Shown brand precedence (PRECEDENCE): an existing checked brand (the namer's
brand_spoken / brand, or a filled brand_url / brand_code) > R3 URL/code >
R4 spoken > R5 adjacency. R3, R4 and R5 run only on reads with no checked
brand, so they never overwrite one; R4 runs only when R3 named nothing, R5
only on reads still unnamed after R2-R4. A held read named by
R3 or R4 is shown (needs_review false): the text, not the namer's own pick,
names it. Self-promo reads get no brand rule.

Provenance: every rule that changes a row appends {"rule", "set", "was",
...evidence} to provenance["ad_rules"] (the importer keeps `provenance` in
the evidence attrs). Original fields stay; a field a rule must set for the
importer to read it (needs_review, brand_spoken) keeps its old value in
"was". fetch_date is never changed on an existing row.

`apply` takes the namer's rows, not an earlier output; it is idempotent all
the same (apply(apply(x)) == apply(x)). It trusts no R6, R7 or R8 result in
its input: clip_hidden, the display bounds and those rules' provenance["ad_rules"]
entries are dropped from every row (any other entry stays), and a row whose
source is a rule's (ADDED_SOURCES) is, with a DET rule on (DET_RULES), dropped
and then made again, so a second apply adds no row twice; with every DET rule
off the rules only R1b makes such a row and the input's rule-source rows pass
through as any named row does (DETBUILD, DBC-R2-03: off loads as before).
Malformed input is tolerated: a row that is not a dict is skipped, a non-numeric start_s /
end_s reads as 0, a segment without finite start <= end is dropped, a
non-list segments is empty, transcript and show keys are read as strings,
a brand field that is not a string reads as none, and a provenance that is
not a dict is kept as provenance["original"].
"""
from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType

from earwitness.pipeline import brand_namer as _bn
from earwitness.pipeline import self_promo as _sp

RULES = ("R1", "R1b", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9", "R10", "R11")
# DETBUILD: the boundary and address rules (R6 split, R7 end trim, R9 address
# route, R11 said address) and R8's signoff, all off until the host turns them on.
DET_RULES = ("R6", "R7", "R9", "R8-signoff", "R10", "R11")
PRECEDENCE = ("checked", "R3", "R4", "R5")
# R5 is opt-in: the owner admits it only if visible precision stays >= 0.696;
# on the 565 benchmark rule all rules with R5 score 0.685, without it 0.697
# (ws-fresh/adrules/MEASURE.md).
# "R1b-spoken" (owner Q3): R1b may name a new row from a spoken brand; off, it
# keeps the URL/code route only (eye check: spoken 1 of 14 right, URL 11 of 11).
DEFAULT_SWITCHES = MappingProxyType({"R1": True, "R1b": True, "R1b-spoken": False, "R2": True,
                                     "R3": True, "R4": True, "R5": False, "R6": False, "R7": False,
                                     "R8": True, "R9": False, "R8-signoff": False,
                                     "R10": False, "R11": False})
R1B_SOURCE = "ad_rules:R1b"
R6_SOURCE = "ad_rules:R6"
R9_SOURCE = "ad_rules:R9"
# The rows the rules add (the importer pins them to the rules version).
ADDED_SOURCES = (R1B_SOURCE, R6_SOURCE, R9_SOURCE)

# Sponsor cues, the single source for the import (R1b) and Ask (T6). STRONG
# = the union of ws-fresh adtag/scripts/missed_host_reads.py STRONG and Meta
# AI's VRun.CUE (ws-meta/model-test/run_value.py), less its "code xxx" cue;
# WEAK ("% off", "code xxx") counts only together with a checked-brand name.
# Patterns are case-insensitive.
CUES = {
    "strong": (
        r"\bsponsored by\b",
        r"\bbrought to you by\b",
        r"\b(?:this|today's) (?:episode|show) is (?:also )?(?:supported|presented) by\b",
        r"\btoday's sponsor\b",
        r"\buse (?:promo )?code\b",
        r"\bpromo code\b",
        r"\boffer code\b",
        r"\b(?:go|head|visit|check out) (?:to )?[a-z0-9-]+\s?(?:dot com|\.com)\b",
        r"(?<![a-z0-9-])[a-z0-9-]+\.com/[a-z0-9-]+",  # anchored start: linear
        r"support for .{0,40} comes from",
        r"this message comes from",
        r"thanks to .{0,30} for sponsoring",
        r"\bour sponsor",
        r"dot com slash",
        r"\.com slash",
        r"\.com ?/",
        r"(?:visit|go to|head to|check out) [a-z0-9-]+(?:\.| dot )com",
    ),
    "weak": (
        r"% off",
        r"\bpercent off\b",
        r"\bcode [a-z0-9]{3,}",  # owner Q4: fires on "code name", "write code"
    ),
}
STRONG_CUE_RE = re.compile("|".join(f"(?:{p})" for p in CUES["strong"]), re.I)
WEAK_CUE_RE = re.compile("|".join(f"(?:{p})" for p in CUES["weak"]), re.I)

# R8: the cues that open an ad (not its call to action): sponsored by,
# brought to you by, this episode is supported by, today's sponsor, support
# for .. comes from, this message comes from, thanks to .. for sponsoring,
# our sponsor.
OPEN_CUES = tuple(CUES["strong"][i] for i in (0, 1, 2, 3, 9, 10, 11, 12))
OPEN_CUE_RE = re.compile("|".join(f"(?:{p})" for p in OPEN_CUES), re.I)

CUE_MERGE_S = 90.0   # cues this close (segment starts) are one cluster
CUE_COVER_S = 90.0   # a named read this close to a cluster covers it
CUE_PAD_S = 30.0     # a new row: the cue segments +- this, whole segments
ADJACENT_S = 5.0     # R5: reads this close (or overlapping) are adjacent
EDGE_S = 10.0        # R3/R4: a read's first (R3) or either (R4) edge, where
#                      the neighbouring ad bleeds over the detected boundary
SPLIT_EDGE_S = 10.0  # R6: a cut lies more than this inside both ends and from the last cut
ADDRESS_QUIET_S = 30.0  # R9: no STRONG cue within this of the address (and fires merge)
TRIM_MIN_S = 20.0    # R7: a trimmed read is at least this long
TRIM_LOOK_S = 20.0   # R7: the brand said this soon past the end stops the trim
CLIP_LEAD_S = 5.0    # R8: a clip file starts this long before its read
CLIP_TAIL_S = 3.0    # ... and ends this long after it (clip_package.py)
CLIP_OVERLAP_S = 0.5  # R8: a segment counts when it overlaps the window by more
CLIP_NEAR = 0.6      # R8: an address this similar to the brand is its own

# Never named from a web address, code or spoken name: where shows send
# listeners (podcast apps, membership and social platforms) and retailers
# that carry other brands' products ("find it at walmart.com").
PLATFORMS = frozenset({
    "apple", "applepodcasts", "applemusic", "spotify", "youtube", "youtubemusic", "iheart",
    "iheartradio", "pandora", "amazonmusic", "siriusxm", "stitcher", "patreon", "substack",
    "instagram", "facebook", "tiktok", "twitter"})
RETAILERS = frozenset({
    "amazon", "walmart", "target", "costco", "bestbuy", "kroger", "walgreens", "cvs", "ulta",
    "sephora", "homedepot", "lowes"})
EXCEPTIONS = PLATFORMS | RETAILERS

# R1's second clause: the show's own calls to action. Counted only in a
# read with no checked brand and no STRONG sponsor cue: network promos for
# other shows ("wherever you get your podcasts") and sponsor reads stay ads.
# Whole words only. OWN_CALL_RE is the show's own on its face; the calls in
# OWN_CALL_SHOW_RE are as much any advertiser's ("visit our website", "at
# our stores", "follow us on", "a Legendary Pictures production") and count
# only when the read also says the show's own name or domain.
OWN_CALL_RE = re.compile(
    r"\b(?:rate,? (?:and )?review|leave (?:us |me )?a (?:\w+ )?(?:review|rating|voicemail|"
    r"voice mail)|subscribe to (?:the|this|our|my) (?:show|podcast|feed|channel)|our (?:newsletter|"
    r"live shows?|discord|merch|substack|patreon)|my (?:book|new book|tour|newsletter|"
    r"website)|tour dates)\b", re.I)
OWN_CALL_SHOW_RE = re.compile(
    r"\b(?:(?:is|was) (?:a|an) [a-z ]{0,30}production|(?:follow|find) (?:us|me) on|"
    r"our (?:website|stores?)|bonus episodes|ad-free)\b", re.I)

_URL_SUFFIXES = ("app", "hq", "now", "usa", "inc")
_CODE_RE = re.compile(
    r"\b(?:(?:use|enter|with|apply)\s+(?:the\s+|my\s+)?(?:promo\s+|offer\s+|discount\s+|"
    r"coupon\s+)?code|(?:promo|offer|discount|coupon)\s+code)\s+([A-Za-z0-9]{2,20})\b", re.I)
_URL_SPAN_RE = re.compile(
    r"(?<![A-Za-z0-9-])[A-Za-z0-9-]+(?:\.|\s+dot\s+)" + _bn.TLD
    + r"\b(?:\s*(?:/|slash)\s*[A-Za-z0-9-]+)?", re.I)
_TOKEN_RE = re.compile(r"[A-Za-z0-9&'’]+")
_NAME = r"[A-Z][\w'’&-]+(?: [A-Z][\w'’&-]+){0,2}"
# Anchored starts (no match begins inside a word) keep both patterns linear.
_LIST_RE = re.compile(rf"(?<![\w'’&-])(?:{_NAME}, ){{2,}}(?:(?:and|or) )?{_NAME}")
_POSSESSIVE_RE = re.compile(r"['’]s$", re.I)
# A multi-word name never spans a sentence end; a period after an initial
# ("U.S. Bank") or one of these abbreviations ("Dr. Squatch") is none.
_ABBREV = frozenset({"dr", "mr", "mrs", "ms", "st", "jr", "sr"})
_GRAM_MAX = 4
_CUT = frozenset(_bn.compact(w) for w in _bn.CUT_WORDS)


def compact(text) -> str:
    """Letters and digits only, lower case (customer._compact_key)."""
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _exact(text) -> str:
    from .ad_reads import brand_key
    return brand_key(text)


class CheckedList:
    """The checked brands as customer.brand_checker defines them: the
    display list's keys and aliases (display-brands.json) and `extra` keys
    (corrections.known_keys and the advertiser list; the caller's), as
    letters and digits, plus each display name exactly (ignoring case).

    match(compact form) gives the brand a rule writes for a spelling heard
    or seen in the text: a display list entry's display name, else the key
    itself. What it gives is always checked()."""

    def __init__(self, display_doc=None, extra=(), own_names=()):
        doc = display_doc if isinstance(display_doc, dict) else {}
        extra = [k for k in extra or () if isinstance(k, str)]
        parts = [{k: v for k, v in (doc.get(p) if isinstance(doc.get(p), dict) else {}).items()
                  if isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip()}
                 for p in ("brands", "aliases")]
        self._known = {compact(k) for k in extra}
        self._known |= {compact(k) for part in parts for k in part}
        self._known.discard("")
        self._exact = {_exact(v) for part in parts for v in part.values()}
        self._names: dict[str, str] = {}
        # R11's own names (letters and digits): the display list's names and
        # `own_names` (the advertiser list's keys and names, never aliases)
        self._display_names = frozenset(compact(v) for part in parts for v in part.values()) \
            | frozenset(compact(k) for k in own_names or () if isinstance(k, str)) - {""}
        for k in extra:
            if compact(k):
                self._names.setdefault(compact(k), k)
        for part in parts:
            for k, v in part.items():
                self._names[compact(k)] = v
                self._names[compact(v)] = v
        self.keys = frozenset(self._known)

    def checked(self, key) -> bool:
        return isinstance(key, str) and bool(key) and (compact(key) in self._known
                                                       or _exact(key) in self._exact)

    def display_name(self, form: str) -> bool:
        """`form` (letters and digits) is one of R11's own names: a
        display-list name or an `own_names` entry (display_name("quince"))."""
        return form in self._display_names

    def match(self, form: str):
        if len(form) < 3 or form in EXCEPTIONS or (form.startswith("the")
                                                   and form[3:] in EXCEPTIONS):
            return None
        return self._names.get(form)


# ---------------------------------------------------------------- text

def _segments(record) -> list:
    """The record's segments, the one normal form every rule reads: float
    start and end (a segment without both as finite numbers, start <= end,
    is dropped), text a string ("" when missing), sorted by start. A
    segments that is not a list is empty."""
    out = []
    segs = record.get("segments") if isinstance(record, dict) else None
    for s in segs if isinstance(segs, list) else ():
        if not isinstance(s, dict):
            continue
        try:
            start, end = float(s["start"]), float(s["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(start) and math.isfinite(end)) or end < start:
            continue
        text = s.get("text")
        out.append(dict(s, start=start, end=end, text="" if text is None else str(text)))
    return sorted(out, key=lambda s: s["start"])


def _t(value) -> float:
    """A row time as a float: a missing, non-numeric or non-finite value is 0."""
    try:
        t = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return t if math.isfinite(t) else 0.0


def _rules_list(prov) -> list:
    """provenance["ad_rules"] as a list: a non-list value is kept as its
    first entry."""
    old = prov.get("ad_rules") if isinstance(prov, dict) else None
    return list(old) if isinstance(old, list) else ([] if old is None else [old])


def _text(segments, start, end) -> str:
    return " ".join(s["text"].strip() for s in segments if s["end"] > start and s["start"] < end)


_TLD_END_RE = re.compile(_bn.TLD + r"$", re.I)
# The namer's DOMAIN_RE with an anchored start (no match begins inside a
# word): linear on a long run after an address hint.
_DOMAIN_RE = re.compile(
    r"(?<![a-z0-9'&-])((?:[a-z0-9'&-]+\s+){0,2}[a-z0-9'&-]+)(?:\.|\s+dot\s+)" + _bn.TLD + r"\b",
    re.I)
_PATH_RE = re.compile(r"(?:\s?/|\s+slash\s+)([A-Za-z0-9-]+)", re.I)
# "/slash podcast": the transcript wrote one spoken "slash" twice
_SLASH_WORD_RE = re.compile(r"slash\s+([A-Za-z0-9-]+)", re.I)
# Call-to-action words that carry a spoken path on rather than end it
# (security r6 #1: "slash pay in four", "slash join the trades")
_PATH_GOES_ON = frozenset({"slash", "in", "of", "is", "it", "the", "on"})
# What follows a path word: punctuation, then a space or the end of the text
_PATH_END_RE = re.compile(r"([.,!?;:\"')\]\u2026\u201d\u2019]*)(?:\s+(\S+)|\s*$)")
# A word spelled out letter by letter, as the speech recognition writes it
# ("QU-I-N-C-E", "B-R-E-T-T", "HA-L-O-2-0"): three or more chunks of one or
# two letters or digits joined by hyphens.
_SPELLED_RE = re.compile(r"(?:[A-Za-z0-9]{1,2}-){2,}[A-Za-z0-9]{1,2}")


def _spelled(word: str) -> bool:
    return bool(_SPELLED_RE.fullmatch(word or ""))


def _path_after(text: str, end: int, spell: bool = True):
    """The /path or "slash path" right after an address ending at `end`,
    lower case, and whether it was spelled out; (None, False) when there is
    none. With `spell` (R11 and the R1b/R9 rows it governs, security r4
    #2): a spelled-out one is written as its letters ("B-R-E-T-T" is
    "brett"); "/slash podcast" is "podcast"; a first word that is a
    call-to-action word ("slash get flex", "slash the big picture", "slash
    that's ...") starts words the host went on to say, not a path the
    words hold: no path then; so is a path the words do not end (security
    r5 #1: "slash Washington Wise" is no path "washington", "slash u s
    slash geob" no path "u"). A path ends at the end of the text, at
    punctuation, or before a call-to-action word other than "slash", "in",
    "of", "is", "it", "the" or "on" (security r6 #1: "slash pay in four")."""
    path = _PATH_RE.match(text, end)
    if not path:
        return None, False
    word, stop = path.group(1), path.end()
    if spell:
        if word.lower() == "slash":
            again = _SLASH_WORD_RE.match(text, path.start(1))
            if not again:
                return None, False
            word, stop = again.group(1), again.end()
        if compact(word) in _CUT:
            return None, False
        after = _PATH_END_RE.match(text, stop)
        if not after or not (after.group(1) or after.group(2) is None
                             or (compact(after.group(2)) in _CUT - _PATH_GOES_ON)
                             or not compact(after.group(2))):
            return None, False
    if _spelled(word):
        return (word.replace("-", "") if spell else word).lower(), True
    return word.lower(), False


def _name_forms(lab: str) -> list:
    """The spellings an address label may name a brand by: the label, less a
    get/try/drink/... prefix, less an app/hq/... suffix."""
    forms = [lab]
    for p in _bn.PREFIXES:
        if lab.startswith(p) and len(lab) - len(p) >= 3:
            forms.append(lab[len(p):])
    forms += [f[:-len(s)] for f in list(forms) for s in _URL_SUFFIXES
              if f.endswith(s) and len(f) - len(s) >= 3]
    return forms


def _url_hits(text: str, checked: CheckedList, spell: bool = False):
    """(brand, raw match, address, spelled, before) for web addresses in `text`
    naming a checked brand: the longest trailing run (1-3 words, no
    call-to-action words) of the spoken address, as is, without a
    get/try/drink/... prefix, or without an app/hq/... suffix. The address is
    those words, the TLD and an optional /path or "slash path", normalized
    ("quince.com/bible"; a spelled-out path as its letters); the raw match
    may start two words early and has no path. `spelled`: a word of the
    address or its path is spelled out letter by letter (_SPELLED_RE).
    `before`: the word right before those words inside the raw match
    (letters and digits; "henry" in "Henry Ford.com"), else None.
    `spell` off (DET off): a spelled-out path stays as written, as before
    ADDR-MISS."""
    if not text or not _bn._TLD_HINT.search(text):
        return
    for m in _DOMAIN_RE.finditer(text):
        words = [compact(w[:-2] if w.lower().endswith("'s") else w) for w in m.group(1).split()]
        for n in (3, 2, 1):
            tail = words[-n:]
            if len(tail) < n or not all(tail) or any(w in _CUT for w in tail):
                continue
            lab = "".join(tail)
            brand = next((b for b in map(checked.match, _name_forms(lab)) if b), None)
            if brand:
                path, spelled_path = _path_after(text, m.end(), spell)
                address = f"{lab}.{_TLD_END_RE.search(m.group(0)).group(0).lower()}" + (
                    f"/{path}" if path else "")
                spelled = spelled_path or any(_spelled(w) for w in m.group(1).split()[-n:])
                yield brand, m.group(0).strip(), address, spelled, \
                    (words[-n - 1] or None) if len(words) > n else None
                break


def url_brands(text: str, checked: CheckedList) -> dict:
    """{brand: raw match} for web addresses in `text` naming a checked
    brand (_url_hits): the evidence rules record in provenance."""
    out: dict[str, str] = {}
    for brand, raw, _address, _spelled_out, _before in _url_hits(text, checked):
        out.setdefault(brand, raw)
    return out


def url_addresses(text: str, checked: CheckedList, spell: bool = False, keep=None) -> dict:
    """{brand: address} for web addresses in `text` naming a checked brand
    (_url_hits): what a new row's `urls` shows ("quince.com/bible"): the
    first address of a brand. With `spell` (the DET switch R11, ADDR-MISS
    case 2), a later address spelled out letter by letter ("That's
    QU-I-N-C-E.com slash B-R-E-T-T" after a misheard "quince.com slash
    bread") wins: the first spelled-out one, except one with no path over a
    first address that has one. `keep(brand, address, before)` (before:
    _url_hits): only those hits count."""
    out: dict[str, str] = {}
    spelled: set = set()
    for brand, _raw, address, spelled_out, before in _url_hits(text, checked, spell):
        if keep is not None and not keep(brand, address, before):
            continue
        spelled_out = spelled_out and spell
        if brand not in out or (spelled_out and brand not in spelled
                                and ("/" in address or "/" not in out[brand])):
            out[brand] = address
            if spelled_out:
                spelled.add(brand)
    return out


SAID_REPAIRS_PATH = Path(__file__).resolve().parents[1] / "data" / "said-address-repairs.json"
_HEARD_RE = re.compile(r"[a-z0-9-]+\." + _bn.TLD)


def said_repairs(path=None) -> tuple:
    """The curated R11 list (SAID_REPAIRS_PATH): ((brand, heard label, TLD),
    ...), a web address the transcript has, spelled other than the brand's
    name, and the brand whose own address it is. () when the file is
    missing, unreadable or any entry is malformed (no brand, a heard value
    that is not a lower-case bare domain, no evidence line): R11 then
    writes only addresses spelled as the brand's name. Read again whenever
    the file's size or modification time changes."""
    p = Path(path or SAID_REPAIRS_PATH)
    try:
        st = p.stat()
    except OSError:
        return ()
    return _said_repairs(str(p), st.st_mtime_ns, st.st_size)


@lru_cache(maxsize=8)
def _said_repairs(path: str, _mtime_ns: int, _size: int) -> tuple:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    pairs = doc.get("pairs") if isinstance(doc, dict) else None
    if not isinstance(pairs, list):
        return ()
    out = []
    for p in pairs:
        if not isinstance(p, dict):
            return ()
        brand, heard, evidence = p.get("brand"), p.get("heard"), p.get("evidence")
        if not (isinstance(brand, str) and compact(brand) and isinstance(heard, str)
                and _HEARD_RE.fullmatch(heard) and isinstance(evidence, str)
                and evidence.strip()):
            return ()
        label, tld = heard.rsplit(".", 1)
        out.append((brand, compact(label), tld))
    return tuple(out)


def heard_addresses(text: str, brand, checked: CheckedList, repairs=()) -> list:
    """R11: the addresses in `text` that `repairs` (said_repairs) lists for
    `brand` (any spelling the checked list gives the same brand), as heard:
    the label and TLD as the transcript has them, lower case, plus the
    path (_path_after). Label and TLD must both be the pair's."""
    wanted = {(label, tld) for b, label, tld in repairs if _same_brand(b, brand, checked)}
    if not wanted or not text or not _bn._TLD_HINT.search(text):
        return []
    out = []
    for m in _DOMAIN_RE.finditer(text):
        words = [compact(w) for w in m.group(1).split()]
        tld = _TLD_END_RE.search(m.group(0)).group(0).lower()
        for n in (3, 2, 1):
            tail = words[-n:]
            # a call-to-action word may be part of a curated label ("for
            # hers.com" is forhers.com): only an exact pair is taken
            if len(tail) < n or not all(tail):
                continue
            if ("".join(tail), tld) in wanted:
                path, _sp = _path_after(text, m.end())
                out.append(f"{''.join(tail)}.{tld}" + (f"/{path}" if path else ""))
                break
    return sorted(set(out))


def _own_name(brand, address: str, checked: CheckedList) -> bool:
    """The address label spells the brand's own name (its letters and
    digits, or those after a get/try/... prefix or before an app/hq/...
    suffix), an own name of the checked list (a display-list name or an
    advertiser-list key or name, CheckedList.display_name). An alias or
    namer spelling the checked list maps to another name never counts, nor
    a brand that is no own name: only the curated list may write those."""
    lab = address.split("/", 1)[0].rsplit(".", 1)[0]
    name = compact(brand)
    return name in _name_forms(lab) and checked.display_name(name)


def _cut_of_spoken(address: str, spoken) -> bool:
    """The address label is the tail of a longer name the row's words say
    (`spoken`: brand_spoken / brand_raw / brand): "ford" of "Henry Ford",
    "blinds" of "Three Day Blinds" (security r4 #1). A leading
    call-to-action word ("the" of "The Home Depot") does not make it
    longer."""
    lab = address.split("/", 1)[0].rsplit(".", 1)[0]
    for name in spoken or ():
        words = [compact(w) for w in str(name or "").split()]
        words = [w for w in words if w]
        while words and words[0] in _CUT:
            words = words[1:]
        if any("".join(words[i:]) == lab for i in range(1, len(words))):
            return True
    return False


def said_urls(text: str, brand, checked: CheckedList, repairs=(), spoken=()) -> tuple:
    """(own, heard): the addresses `text` says for `brand` that R11 writes:
    those spelling its own name (url_addresses with the spelled-out
    preference, _own_name), then the
    curated ones (heard_addresses) not already among them; each sorted.
    These are no own address: one a curated one holds at its end ("for
    hers.com", the curated forhers.com) is that curated one's cut; one
    right after "for" in the spoken address, even with no curated list
    (security r4 #4); one whose label is the tail of a longer name the
    row's words say (`spoken`, _cut_of_spoken: "Henry Ford.com" on a Henry
    Ford read is not ford.com)."""
    heard = heard_addresses(text, brand, checked, repairs)
    own = sorted(set(url_addresses(
        text, checked, spell=True,
        keep=lambda b, a, before: _same_brand(b, brand, checked)
        and _own_name(b, a, checked) and before != "for"
        and not _cut_of_spoken(a, spoken)).values()))
    own = [u for u in own if not any(h != u and h.endswith(u) and h[-len(u) - 1:-len(u)].isalnum()
                                     for h in heard)]
    return own, [u for u in heard if u not in own]


def said_addresses(row: dict, segs: list, checked: CheckedList, repairs=()) -> dict:
    """R11 for one row (see the module docstring): `row` with `urls` set to
    the addresses its own words say for the brand it shows (its own name's
    first, then the curated ones), or `row` unchanged."""
    if row.get("urls") or row.get("source") in (R1B_SOURCE, R9_SOURCE):
        return row
    brand = _shown(row, checked)
    if not brand:
        return row
    # an address brand of another brand (checked or not) stays its own fact:
    # no R11 then
    url_brand = row.get("brand_url")
    if isinstance(url_brand, str) and url_brand.strip() \
            and not _same_brand(url_brand, brand, checked):
        return row
    lo, hi = bounds(row)
    own, heard = said_urls(_text(segs, lo, hi), brand, checked, repairs,
                           spoken=[row.get(f) for f in ("brand_spoken", "brand_raw", "brand")])
    if not own and not heard:
        return row
    return _record(row, {"rule": "R11", "set": {"urls": own + heard},
                         "was": {"urls": row.get("urls")}, "own": own, "heard": heard})


def code_brands(text: str, checked: CheckedList) -> dict:
    """{brand: code} for promo codes in `text` that are a checked brand."""
    out: dict[str, str] = {}
    for code in _CODE_RE.findall(text or ""):
        brand = checked.match(compact(code))
        if brand:
            out.setdefault(brand, code)
    return out


def spoken_brands(text: str, checked: CheckedList, named=()) -> dict:
    """{brand: times spoken} for checked brands said in `text` (web
    addresses removed: they are URL attribution; lists of three or more
    capitalized names removed: a store's list of the brands it carries). A
    one-word name counts only when the text writes it as a name
    (capitalized) at least once, or is in `named` (written so elsewhere in
    the read); then every case counts ("Quince makes ... these quince
    sweaters" is Quince twice; "a factor in ... another factor" is no
    Factor). Three letters must be capitals. A hit inside a longer hit
    counts once, as the longer one. A name's possessive counts as the name
    ("Quince's sheets"); a multi-word name never spans a sentence end ("a
    mint. Mobile ..." is no Mint Mobile)."""
    text = _LIST_RE.sub(" ", _URL_SPAN_RE.sub(" ", text or ""))
    toks = list(_TOKEN_RE.finditer(text))
    # brk[i]: a sentence ends between token i and token i + 1
    brk = []
    for i in range(len(toks) - 1):
        gap = text[toks[i].end():toks[i + 1].start()]
        word = toks[i].group(0)
        brk.append(bool(re.search(r"[?!]", gap)) or ("." in gap and not (
            len(word) == 1 or word.lower() in _ABBREV)))
    hits = []
    starts: dict[int, list] = {}   # hit start -> ends, for the containment check
    for n in range(_GRAM_MAX, 0, -1):
        for i in range(len(toks) - n + 1):
            if any(brk[i:i + n - 1]):
                continue
            words = [t.group(0) for t in toks[i:i + n]]
            raw = "".join(words)
            brand = checked.match(compact(raw))
            if not brand and _POSSESSIVE_RE.search(words[-1]):
                raw = "".join(words[:-1]) + words[-1][:-2]
                brand = checked.match(compact(raw))
            if not brand or (len(compact(raw)) == 3 and not raw.isupper()):
                continue
            # inside a longer (earlier) hit: it starts at most _GRAM_MAX - n before
            if any(b >= i + n for a in range(max(0, i + n - _GRAM_MAX), i + 1)
                   for b in starts.get(a, ())):
                continue
            starts.setdefault(i, []).append(i + n)
            hits.append((i, i + n, brand, n > 1 or raw[:1].isupper()))
    named = {brand for _a, _b, brand, as_name in hits if as_name} | set(named)
    out: dict[str, int] = {}
    for _a, _b, brand, _c in hits:
        if brand in named:
            out[brand] = out.get(brand, 0) + 1
    return out


def cue_tier(text: str, checked: CheckedList):
    """The sponsor-cue tier of `text`: "strong" for a STRONG cue; "weak" for
    a WEAK cue in text that also names a checked brand (web address, code or
    spoken); else None. The WEAK gate R1b uses, for Ask (T6) too."""
    if STRONG_CUE_RE.search(text or ""):
        return "strong"
    if WEAK_CUE_RE.search(text or "") and (url_brands(text, checked) or code_brands(text, checked)
                                           or spoken_brands(text, checked)):
        return "weak"
    return None


# ---------------------------------------------------------------- rows

def _identity(show, title):
    if isinstance(show, dict) and show:
        return show
    title = str(title or "").strip()
    if not title:
        return {}
    return {"title": title, "names": _sp._uniq([title] + _sp.TITLE_SPLIT.split(title)),
            "authors": [], "domains": [], "platforms": {}}


def _says_show(text: str, show: dict) -> bool:
    """The read says the show's own name or domain label (self_promo's own
    names, handles and domains; 5+ letters), as whole words. A one-word
    show name counts only written capitalized ("Serial", not "every serial
    number"): one word is often a common word."""
    if not show:
        return False
    names = _sp.own_names(show)
    one_word = {_sp.name_key(n) for n in names if len(_sp._words(n)) < 2}
    keys = {k for k in (_sp.name_key(n) for n in names + _sp._handles(show)) if len(k) >= 5}
    keys |= {d for d in _sp._own_labels(show) if len(d) >= 5}
    if not keys:
        return False
    toks = re.findall(r"[A-Za-z0-9]+", (text or "").replace("'", "").replace("’", ""))
    words = [t.lower() for t in toks]
    for i in range(len(words)):
        for j in range(i + 1, min(i + 7, len(words) + 1)):
            for k in keys:
                if not _sp._same_label("".join(words[i:j]), k):
                    continue
                first = i + 1 if words[i] == "the" and j - i > 1 else i
                if k not in one_word or toks[first][:1].isupper():
                    return True
    return False


def _str(row: dict, key: str):
    """row[key] when it is a string, else None (a malformed field is none)."""
    value = row.get(key)
    return value if isinstance(value, str) else None


def _original_brand(row: dict):
    """The namer's own pick, whatever R4/R5 wrote since: brand_raw, else the
    brand_spoken the first rule to change it recorded, else the row's
    (self_promo.candidate_brand)."""
    row = {k: _str(row, k) for k in ("brand", "brand_raw", "brand_spoken")} | {
        "provenance": row.get("provenance")}
    if not row["brand_raw"]:
        was = next((e["was"] for e in _rules_list(row["provenance"])
                    if isinstance(e, dict) and isinstance(e.get("was"), dict)
                    and "brand_spoken" in e["was"]), None)
        if was is not None:
            row["brand_spoken"] = _str(was, "brand_spoken")
    return _sp.candidate_brand(row)


def _has_checked(row: dict, checked: CheckedList) -> bool:
    return any(checked.checked(row.get(k)) for k in ("brand_spoken", "brand_url", "brand_code")) \
        or (not _str(row, "brand_spoken") and checked.checked(row.get("brand")))


def _names_checked(row: dict, text: str, checked: CheckedList) -> bool:
    return _has_checked(row, checked) or bool(url_brands(text, checked) or code_brands(
        text, checked) or spoken_brands(text, checked))


def _record(row: dict, entry: dict) -> dict:
    out = dict(row)
    old = row.get("provenance")
    # a provenance that is not a dict is kept, as "original"
    prov = dict(old) if isinstance(old, dict) else {} if old is None else {"original": old}
    prov["ad_rules"] = _rules_list(prov) + [entry]
    out["provenance"] = prov
    out.update(entry.get("set") or {})
    return out


def _segs_text(segments, lo, hi) -> str:
    """The text of the segments that start in [lo, hi) and end by `hi`."""
    return " ".join(s["text"].strip() for s in segments if lo <= s["start"] < hi and s["end"] <= hi)


def _rule_rows(row, segs, show, checked, on) -> dict:
    if on["R1"] and segs is not None and not row.get("self_promo"):
        text = _text(segs, _t(row.get("start_s")), _t(row.get("end_s")))
        brand = _original_brand(row)
        hit, why = _sp.classify(brand, text, show, None, checked.keys)
        if not hit:
            call = OWN_CALL_RE.search(text) or (_says_show(text, show)
                                                and OWN_CALL_SHOW_RE.search(text))
            if call and not STRONG_CUE_RE.search(text) and not _names_checked(row, text, checked):
                hit, why = True, f"own-call:{call.group(0).lower()}"
        if hit:
            row = _record(row, {"rule": "R1", "set": {"self_promo": True,
                                                      "self_promo_reason": f"R1:{why}"},
                                "was": {"self_promo": row.get("self_promo"),
                                        "self_promo_reason": row.get("self_promo_reason")}})
    if row.get("self_promo"):
        return row
    held = bool(row.get("needs_review"))
    if _has_checked(row, checked):
        if held and on["R2"]:
            brand = _shown(dict(row, needs_review=False), checked)
            words = _text(segs, _t(row.get("start_s")), _t(row.get("end_s"))) \
                if segs is not None else str(row.get("excerpt") or "")
            evidence = {}
            for route, found in (("spoken", spoken_brands(words, checked)),
                                 ("url", url_brands(words, checked)),
                                 ("code", code_brands(words, checked))):
                ev = next((v for b, v in found.items() if _same_brand(b, brand, checked)), None)
                if ev is not None:
                    evidence[route] = ev
            if not evidence:
                return row
            row = _record(row, {"rule": "R2", "set": {"needs_review": False},
                                "was": {"needs_review": row.get("needs_review")},
                                "evidence": evidence})
        return row
    if segs is None:
        return row
    text = _text(segs, _t(row.get("start_s")), _t(row.get("end_s")))
    unhold = {"needs_review": False} if held else {}
    unheld = {"needs_review": row.get("needs_review")} if held else {}
    s0, s1 = _t(row.get("start_s")), _t(row.get("end_s"))
    if on["R3"]:
        late = " ".join(s["text"].strip() for s in segs if s0 + EDGE_S <= s["start"] < s1)
        urls, codes = url_brands(late, checked), code_brands(late, checked)
        # A brand_url / brand_code already there is unchecked here (the row
        # has no checked brand) and shows nothing: R3 replaces it ("was").
        if len(urls) == 1:
            (brand, ev), = urls.items()
            return _record(row, {"rule": "R3", "set": {"brand_url": brand, **unhold},
                                 "was": {"brand_url": row.get("brand_url"), **unheld},
                                 "evidence": ev})
        if not urls and len(codes) == 1:
            (brand, ev), = codes.items()
            return _record(row, {"rule": "R3", "set": {"brand_code": brand, **unhold},
                                 "was": {"brand_code": row.get("brand_code"), **unheld},
                                 "evidence": ev})
    if on["R4"]:
        said = spoken_brands(text, checked)
        twice = [b for b, n in said.items() if n >= 2]
        inner = _segs_text(segs, s0 + EDGE_S, s1 - EDGE_S)
        if len(twice) == 1 and not any(_same_brand(b, twice[0], checked)
                                       for b in spoken_brands(inner, checked, named=twice)):
            twice = []
        if len(twice) == 1:
            brand, times = twice[0], said[twice[0]]
            return _record(row, {"rule": "R4", "set": {"brand_spoken": brand, **unhold},
                                 "was": {"brand_spoken": row.get("brand_spoken"), **unheld},
                                 "evidence": {brand: times}})
    return row


def _shown(row: dict, checked: CheckedList):
    """The checked brand a customer sees on a row (PRECEDENCE order), or
    None: self-promo and held rows show none."""
    if row.get("self_promo") or row.get("needs_review"):
        return None
    for key in ("brand_spoken", "brand_url", "brand_code"):
        if checked.checked(row.get(key)):
            return row[key]
    if not _str(row, "brand_spoken") and checked.checked(row.get("brand")):
        return row["brand"]
    return None


def _same_brand(a, b, checked: CheckedList) -> bool:
    ca, cb = compact(a), compact(b)
    return bool(ca) and (ca == cb or compact(checked.match(ca) or ca)
                         == compact(checked.match(cb) or cb))


def _adjacent(a: dict, b: dict) -> bool:
    a0, a1 = _t(a.get("start_s")), _t(a.get("end_s"))
    b0, b1 = _t(b.get("start_s")), _t(b.get("end_s"))
    return max(b0 - a1, a0 - b1) <= ADJACENT_S


def adjacency(rows: list, segs: list, checked: CheckedList) -> list:
    """R5 over one episode's rows (after the other rules): an ad with no
    checked brand (not self-promo) adjacent to an ad that shows one, whose
    own text names that brand (a web address or code, else spoken) and no
    other checked brand more often, takes it. Rows are never merged and
    keep their bounds. Repeats until nothing changes, so a run of split
    reads names in one pass (and a second apply changes nothing)."""
    rows = list(rows)
    changed = True
    while changed:
        changed = False
        for i, r in enumerate(rows):
            if r.get("self_promo") or _has_checked(r, checked):
                continue
            hits = {}
            for n in rows:
                brand = _shown(n, checked) if n is not r and _adjacent(r, n) else None
                if brand:
                    hits.setdefault(compact(checked.match(compact(brand)) or brand), (n, brand))
            if len(hits) != 1:
                continue
            (n, brand), = hits.values()
            text = _text(segs, _t(r.get("start_s")), _t(r.get("end_s")))
            urls, codes = url_brands(text, checked), code_brands(text, checked)
            said = spoken_brands(text, checked)
            field = next((f for f, found in (("brand_url", urls), ("brand_code", codes),
                                             ("brand_spoken", said))
                          if any(_same_brand(b, brand, checked) for b in found)), None)
            if field is None or (field == "brand_url" and len(urls) > 1) \
                    or (field == "brand_code" and len(codes) > 1):
                continue
            found = {"brand_url": urls, "brand_code": codes, "brand_spoken": said}[field]
            name = next(b for b in found if _same_brand(b, brand, checked))
            if field == "brand_spoken" and any(v > said[name] for b, v in said.items()
                                               if b != name):
                continue
            held = bool(r.get("needs_review"))
            rows[i] = _record(r, {
                "rule": "R5", "set": {field: name, **({"needs_review": False} if held else {})},
                "was": {field: r.get(field), **({"needs_review": r.get("needs_review")}
                                                if held else {})},
                "neighbour": n.get("candidate_id"), "evidence": found[name]})
            changed = True
    return rows


def _fetch_date(record: dict, rows: list):
    if record.get("fetch_date"):
        return record["fetch_date"], {"from": "transcript",
                                      "basis": record.get("fetch_date_source")}
    dated = next((r for r in rows or () if isinstance(r, dict) and r.get("fetch_date")), None)
    if dated is not None:
        return dated["fetch_date"], {"from": "named-read",
                                     "candidate_id": dated.get("candidate_id"),
                                     "basis": "the episode's named reads state its date"}
    return None, {"from": "missing"}


def _span(segs, a, b):
    """(lo, hi) of a new row around [a, b]: whole segments within
    +-CUE_PAD_S, cut at the window edge; None when no segment lies inside
    (times so large the padding rounds away) or the row id would overflow."""
    inside = [s for s in segs if s["end"] > a - CUE_PAD_S and s["start"] < b + CUE_PAD_S]
    if not inside:
        return None
    lo = max(inside[0]["start"], a - CUE_PAD_S, 0.0)
    hi = min(max(s["end"] for s in inside), b + CUE_PAD_S)
    return (lo, hi) if math.isfinite(lo * 1000) and math.isfinite(hi * 1000) else None


def _new_row(ep_id, record, rows, checked, anchor, source, tag, field, brand, lo, hi,
             window, entry, spell: bool = False) -> dict:
    """A row the rules add (R1b, R9): `field` names `brand` over [lo, hi]
    (`window` is its text); fetch_date from the record, else the episode's
    named reads, with its basis in provenance["fetch_date"]. `spell` (the
    DET switch R11): its urls are R11's for its brand (said_urls: the
    addresses spelling its own name, a display-list name or an
    advertiser-list key or name, a spelled-out one winning, then the
    curated ones); off, every address naming its brand, as before."""
    fetch_date, basis = _fetch_date(record, rows)
    if spell:
        # the same own names as R11 (security r4 #3): a namer spelling that is
        # only a checked key ("cachava") writes no own address
        own, heard = said_urls(window, brand, checked, said_repairs())
        urls = own + heard
    else:
        urls = sorted({u for n, u in url_addresses(window, checked).items()
                       if _same_brand(n, brand, checked)})
    return {
        # a web address or code is no heard name: brand stays empty
        "anchor": anchor, "brand": brand if field == "brand_spoken" else None,
        "brand_code": None, "brand_raw": None,
        "brand_spoken": None, "brand_url": None, field: brand,
        "candidate_id": f"{ep_id}.adrules-{tag}.{int(round(lo * 1000))}-{int(round(hi * 1000))}",
        "confidence": "low", "confidence_score": None, "ep_id": ep_id,
        "ep_title": record.get("ep_title"), "pub_date": record.get("pub_date"),
        "show_title": record.get("show_title"), "start_s": lo, "end_s": hi,
        "excerpt": window[:1000], "fetch_date": fetch_date, "needs_review": False,
        "offer_codes": sorted({c for n, c in code_brands(window, checked).items()
                               if _same_brand(n, brand, checked)}),
        "urls": urls,
        "source": source,
        "provenance": {"pipeline": "ad_rules", "fetch_date": basis, "ad_rules": [entry]}}


def cue_rows(ep_id: str, record: dict, rows: list, checked: CheckedList,
             spoken: bool = False, spell: bool = False) -> list:
    """R1b: new ad rows for the episode's cue clusters that no read in `rows`
    shown as an ad (not self-promo; held reads count) covers and whose cue
    names one checked brand. The row spans the cue segments that name its
    brand (address, code or the name said; the spoken route: the whole cue
    chain)
    +-CUE_PAD_S, cut to whole segments inside that window (a segment running
    past it is cut at the window edge), so it never reaches a read
    CUE_COVER_S away nor the chain's other sponsors. Its urls and
    offer_codes hold only its own brand's (`spell`: _new_row)."""
    segs = _segments(record)
    cues = []
    for s in segs:
        tier = cue_tier(s["text"], checked)
        if tier:
            cues.append((s, tier))
    clusters: list[list] = []
    for s, tier in cues:
        if clusters and s["start"] - clusters[-1][-1][0]["start"] <= CUE_MERGE_S:
            clusters[-1].append((s, tier))
        else:
            clusters.append([(s, tier)])
    spans = [(_t(r.get("start_s")), _t(r.get("end_s"))) for r in rows or ()
             if isinstance(r, dict) and not r.get("self_promo")]
    out = []
    for cl in clusters:
        a, b = cl[0][0]["start"], max(s["end"] for s, _t in cl)
        if any(s < b + CUE_COVER_S and e > a - CUE_COVER_S for s, e in spans):
            continue
        cue_text = " ".join(s["text"] for s, _t in cl)
        urls, codes = url_brands(cue_text, checked), code_brands(cue_text, checked)
        named = {**codes, **urls}
        if len(named) > 1:
            continue
        if named:
            field = "brand_url" if urls else "brand_code"
            brand, ev = next(iter(named.items()))
            # the cue segments that name it: its address or code, or its name
            # said ("brought to you by Quince"); all, when only the joined text does
            own = [s for s, _t in cl if any(_same_brand(n, brand, checked) for n in {
                **url_brands(s["text"], checked), **code_brands(s["text"], checked),
                **spoken_brands(s["text"], checked)})]
            if own:
                a, b = own[0]["start"], max(s["end"] for s in own)
        span = _span(segs, a, b)
        if span is None:
            continue
        lo, hi = span
        window = _text(segs, lo, hi)
        if not named:
            said = spoken_brands(window, checked) if spoken else {}
            if len(said) != 1:
                continue
            field = "brand_spoken"
            brand, ev = next(iter(said.items()))
        entry = {"rule": "R1b", "set": {field: brand}, "route": field[len("brand_"):],
                 "evidence": ev, "cues": [{"start_s": s["start"], "tier": t,
                                           "text": s["text"][:200]} for s, t in cl]}
        out.append(_new_row(ep_id, record, rows, checked, "cue", R1B_SOURCE, "r1b", field,
                            brand, lo, hi, window, entry, spell))
    return out


def _mentions(text: str, brand, checked: CheckedList) -> bool:
    """R8: `text` names `brand`: spoken, or in a web address or code; else
    its letters appear in the text (4+ letters), or as a word (3 letters)."""
    if not brand or not text:
        return False
    if any(_same_brand(b, brand, checked)
           for b in spoken_brands(text, checked, named=[brand])):
        return True
    if any(_same_brand(b, brand, checked)
           for b in {**url_brands(text, checked), **code_brands(text, checked)}):
        return True
    k = compact(brand)
    if len(k) >= 4:
        return k in compact(text)
    return len(k) == 3 and bool(re.search(rf"\b{k}\b", text.lower()))


def _own(b, brand, checked: CheckedList) -> bool:
    """R8: the brand `b` found in the window is the read's own: the same
    checked brand, or one name inside the other ("drinkag1" for AG1)."""
    x, y = compact(b), compact(brand)
    return bool(y) and (_same_brand(b, brand, checked) or (
        min(len(x), len(y)) >= 3 and (x in y or y in x)))


def _near(label, brand) -> bool:
    """R8: a spoken address label that is the brand misheard ("Quints.com"
    for Quince): one inside the other, or similarity >= CLIP_NEAR."""
    import difflib
    x, y = compact(label), compact(brand)
    return bool(x and y) and (x in y or y in x or difflib.SequenceMatcher(
        None, x, y).ratio() >= CLIP_NEAR)


def _own_address(text: str, brand, checked: CheckedList) -> bool:
    """R8 signoff: `text` holds the read's own web address or code, or a
    spoken address whose label (or one of its last three words) is the brand
    or the brand misheard."""
    if any(_own(b, brand, checked)
           for b in {**url_brands(text, checked), **code_brands(text, checked)}):
        return True
    for m in _DOMAIN_RE.finditer(text):
        words = m.group(1).split()
        label = compact(words[-1]) if words else ""
        if label and (_near(label, brand) or any(_near(w, brand) for w in words[-3:])):
            return True
    return False


def clip_guard(row: dict, segs: list, checked: CheckedList, signoff: bool = False) -> tuple:
    """R8 for one read over its episode's segments (_segments' form):
    (signals {signal: reason}, window [lo, hi], brand B). No signals: the
    clip shows. signoff: also the DETBUILD signoff signal, only for a read the
    other signals leave clean."""
    s0, s1 = _t(row.get("start_s")), _t(row.get("end_s"))
    lo, hi = s0 - CLIP_LEAD_S, s1 + CLIP_TAIL_S
    brand = _shown(row, checked) or _str(row, "brand_spoken") or _str(row, "brand")
    win = [s for s in segs if min(hi, s["end"]) - max(lo, s["start"]) > CLIP_OVERLAP_S]
    text = " ".join(s["text"] for s in win)
    out: dict[str, str] = {}
    for i, s in enumerate(win):
        if OPEN_CUE_RE.search(s["text"]):
            nxt = win[i + 1]["text"] if i + 1 < len(win) else ""
            if not _mentions(s["text"] + " " + nxt, brand, checked):
                out.setdefault("open", f"@{s['start']:.0f} {s['text'][:70]}")
    other = {b: e for b, e in {**url_brands(text, checked), **code_brands(text, checked)}.items()
             if not _own(b, brand, checked)}
    if other:
        out["url"] = ", ".join(f"{b} ({e})" for b, e in list(other.items())[:3])
    said = {b: n for b, n in spoken_brands(text, checked).items()
            if n >= 2 and not _own(b, brand, checked)}
    if said:
        out["spoken"] = ", ".join(f"{b} x{n}" for b, n in said.items())
    for m in _DOMAIN_RE.finditer(text):
        words = m.group(1).split()
        label = compact(words[-1]) if words else ""
        if len(label) >= 3 and label not in EXCEPTIONS and not _near(label, brand) \
                and not any(_near(w, brand) for w in words[-3:]):
            out.setdefault("address", m.group(0).strip()[:40])
    if signoff and not out and brand:
        oa = [i for i, s in enumerate(win) if _own_address(s["text"], brand, checked)]
        if len(oa) >= 2:
            after = win[oa[-1] + 1:]
            tail_speech = sum(min(hi, s["end"]) - max(lo, s["start"]) for s in after)
            if tail_speech > (hi - lo) / 2 \
                    and not any(_mentions(s["text"], brand, checked) for s in after):
                out["signoff"] = (f"own address x{len(oa)}, {tail_speech:.0f} s after "
                                  f"@{win[oa[-1]]['end']:.0f}")
    return out, [lo, hi], brand


def _clip_rule(row: dict, segs: list, checked: CheckedList, signoff: bool = False) -> dict:
    """R8 on one row (apply has removed any verdict it came with,
    _without_verdict): hide its clip when clip_guard finds another ad, else
    the verdict clean."""
    signals, window, brand = clip_guard(row, segs, checked, signoff)
    if not signals:
        # the verdict "clean": the build lists only reads judged clean
        return row if row.get("clip_hidden") is False else dict(row, clip_hidden=False)
    return _record(row, {"rule": "R8", "set": {"clip_hidden": True},
                         "was": {"clip_hidden": row.get("clip_hidden")},
                         "variant": "V3", "window": window, "brand": brand,
                         "signals": signals})


# What only this apply gives a row: R8's verdict, and R6/R7 display bounds.
_APPLY_ONLY_RULES = ("R6", "R7", "R8", "R10")
_APPLY_ONLY_FIELDS = ("clip_hidden", "display_start_s", "display_end_s", "same_ad_as")


def _without_verdict(row: dict) -> dict:
    """`row` without an R8 verdict or R6/R7 display bounds it came with
    (clip_hidden, display_start_s / display_end_s and those rules'
    provenance entries): only the rules themselves, in this apply, give
    them, so a named-reads row can never bring its own (CGS-R2-02;
    DETBUILD: ad_reads shows a rules row's R6/R7 display bounds). Any other
    incoming entry stays, so a second apply gives the same result
    (idempotent); the importer's alert carry trusts only the entries its own
    apply added (DBS-R1-03)."""
    prov = row.get("provenance")
    entries = _rules_list(prov) if isinstance(prov, dict) else []
    kept = [e for e in entries
            if not (isinstance(e, dict) and e.get("rule") in _APPLY_ONLY_RULES)]
    if not any(k in row for k in _APPLY_ONLY_FIELDS) and len(kept) == len(entries):
        return row
    out = {k: v for k, v in row.items() if k not in _APPLY_ONLY_FIELDS}
    if len(kept) != len(entries):
        out["provenance"] = {k: v for k, v in prov.items() if k != "ad_rules"}
        if kept:
            out["provenance"]["ad_rules"] = kept
    return out


# ---------------------------------------------------------------- DETBUILD

def added(row) -> bool:
    """The row is one the rules add (ADDED_SOURCES): R1b, R6 part, R9 row."""
    return isinstance(row, dict) and row.get("source") in ADDED_SOURCES


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def bounds(row: dict) -> tuple:
    """(lo, hi) of a row: its display bounds when both are numbers (R6, R7),
    else start_s and end_s."""
    lo, hi = row.get("display_start_s"), row.get("display_end_s")
    if _number(lo) and _number(hi):
        return float(lo), float(hi)
    return _t(row.get("start_s")), _t(row.get("end_s"))


def _names(text: str, checked: CheckedList) -> dict:
    """Every checked brand `text` names: web address, code or spoken."""
    return {**url_brands(text, checked), **code_brands(text, checked),
            **spoken_brands(text, checked)}


def _mentions_count(text: str, brand, checked: CheckedList) -> int:
    """How often `text` names `brand`: spoken, once per address or code
    naming it, else (4+ letters) the brand's letters as a substring."""
    if not brand or not text:
        return 0
    n = sum(v for b, v in spoken_brands(text, checked, named=[brand]).items()
            if _same_brand(b, brand, checked))
    n += sum(1 for b in {**url_brands(text, checked), **code_brands(text, checked)}
             if _same_brand(b, brand, checked))
    if not n:
        k = compact(brand)
        if len(k) >= 4:
            n = compact(text).count(k)
    return n


def _row_brand(row: dict, checked: CheckedList):
    return _shown(row, checked) or _str(row, "brand_spoken") or _str(row, "brand")


def _cuts(row: dict, segs: list, checked: CheckedList) -> list:
    """R6: the cut times of one read (see the module docstring)."""
    s0, s1 = _t(row.get("start_s")), _t(row.get("end_s"))
    cuts: list = []
    for s in segs:
        st = s["start"]
        if s0 + SPLIT_EDGE_S < st < s1 - SPLIT_EDGE_S and OPEN_CUE_RE.search(s["text"]):
            if not cuts or st - cuts[-1] > SPLIT_EDGE_S:
                cuts.append(st)
    brand = _row_brand(row, checked)
    if not cuts or not brand:
        return cuts
    kept: list = []
    for c in cuts:
        lo = kept[-1] if kept else s0
        nxt = next((x for x in cuts if x > c), s1)
        left, right = _text(segs, lo, c), _text(segs, c, nxt)
        same = (_mentions_count(left, brand, checked) and _mentions_count(right, brand, checked)
                and all(_same_brand(b, brand, checked)
                        for b in {**_names(left, checked), **_names(right, checked)}))
        if not same:
            kept.append(c)
    return kept


def _says(text: str, brand, heard: list, checked: CheckedList) -> int:
    """R6: how often `text` names `brand` (_mentions_count) or says one of
    its repaired spellings `heard` (_repaired)."""
    return _mentions_count(text, brand, checked) + sum(compact(text).count(k) for k in heard)


def _repaired(namer: dict, brand, checked: CheckedList) -> list:
    """R6 keeper: the spellings the namer heard and repaired to `brand`
    ("repair:quints->quince" in the evidence of its candidate for that
    brand), compacted, 4+ letters: the read says its brand misheard."""
    prov = namer.get("provenance")
    cands = prov.get("candidates") if isinstance(prov, dict) else None
    out: list = []
    seen: set = set()
    for c in cands if isinstance(cands, list) and brand else ():
        if not isinstance(c, dict) or not _same_brand(_str(c, "brand"), brand, checked):
            continue
        for e in c.get("evidence") if isinstance(c.get("evidence"), list) else ():
            if isinstance(e, str) and e.startswith("repair:") and "->" in e:
                k = compact(e[len("repair:"):].split("->", 1)[0])
                if len(k) >= 4 and k not in seen and k not in compact(brand):
                    seen.add(k)
                    out.append(k)
    return out


def split_row(namer: dict, row: dict, segs: list, show, checked: CheckedList, on: dict) -> list:
    """R6 for one read: `namer` is the namer's row, `row` what R1-R5 made of
    it. [row] when it has no cut; else the keeper first (`row` with display bounds
    and an R6 entry) followed by the other parts, new rows named by R1-R4
    from their own words."""
    cid = namer.get("candidate_id")
    if not (isinstance(cid, str) and cid):
        return [row]
    cuts = _cuts(row, segs, checked)
    if not cuts:
        return [row]
    s0, s1 = _t(row.get("start_s")), _t(row.get("end_s"))
    edges = [s0] + cuts + [s1]
    if not all(math.isfinite(x * 1000) for x in edges):
        return [row]
    parts = list(zip(edges, edges[1:]))
    texts = [_text(segs, a, b) for a, b in parts]
    brand = _row_brand(row, checked)
    heard = _repaired(namer, brand, checked)
    counts = [_says(t, brand, heard, checked) for t in texts]
    keep = counts.index(max(counts)) if max(counts) > 0 else 0
    ids = [cid if i == keep else f"{cid}.split-{int(round(a * 1000))}-{int(round(b * 1000))}"
           for i, (a, b) in enumerate(parts)]
    # DET-SPANS-2: a lead part (before the first cut) that is not the keeper,
    # has no ad words (no checked brand, cue, address or code) and says the
    # show's own name is the episode's intro: no row is made of it
    talk = keep != 0 and not _ad_evidence(texts[0]) and not _names(texts[0], checked) \
        and _says_show(texts[0], show)
    entry = {"rule": "R6", "set": {"display_start_s": parts[keep][0],
                                   "display_end_s": parts[keep][1]},
             "was": {"display_start_s": row.get("display_start_s"),
                     "display_end_s": row.get("display_end_s")},
             "parts": ids[1:] if talk else ids, "cuts": cuts}
    if talk:
        entry["not_ad"] = [list(parts[0])]
    old = namer.get("provenance")
    keeper = _record(row, entry)
    out = [keeper]
    for i, ((a, b), text) in enumerate(zip(parts, texts)):
        if i == keep or (i == 0 and talk):
            continue
        prov = {k: v for k, v in old.items() if k not in ("ad_rules", "candidates")} \
            if isinstance(old, dict) \
            else {} if old is None else {"original": old}
        # `part` and `parts` count the intro part B2 dropped (the keeper's "parts" does not)
        prov["ad_rules"] = [{"rule": "R6", "split_of": cid, "part": i, "parts": len(parts),
                             "was": {"start_s": s0, "end_s": s1}, "cuts": cuts}]
        k = compact(text)
        part = {key: v for key, v in namer.items()
                if key not in ("self_promo", "self_promo_reason", "clip_hidden")}
        part.update(
            candidate_id=ids[i], start_s=a, end_s=b, excerpt=text[:1000],
            urls=[u for u in namer.get("urls") or [] if compact(u) in k],
            offer_codes=[c for c in namer.get("offer_codes") or [] if compact(c) in k],
            brand=None, brand_spoken=None, brand_raw=None, brand_url=None, brand_code=None,
            needs_review=False, confidence="low", confidence_score=None,
            source=R6_SOURCE, provenance=prov)
        out.append(_rule_rows(part, segs, show, checked, on))
    return out


def _ad_evidence(text: str) -> bool:
    """Any ad words: a STRONG cue, a web address or a promo code."""
    return bool(STRONG_CUE_RE.search(text) or _URL_SPAN_RE.search(text) or _CODE_RE.search(text))


def _other_ad(text: str, brand, checked: CheckedList) -> bool:
    """R7 run-on: `text` names another checked brand (address, code or
    spoken; not one name inside the other, R8's _own). A spoken address of
    no checked brand does not count: it is often the read's own misheard."""
    return any(not _own(b, brand, checked) for b in _names(text, checked))


def _run_on(inwin: list, last: dict, brand, checked: CheckedList, stop: bool) -> dict:
    """R7: the segment a read ends on, from its last mention `last`: the last
    call to action after it (ad evidence), never past an opening cue that
    does not name the brand (the next ad's) nor, with `stop`, a segment with
    another ad's words (_other_ad, DET-SPANS-2)."""
    passed = []
    for s in inwin:
        if s["start"] <= last["start"]:
            continue
        if OPEN_CUE_RE.search(s["text"]) and not _mentions_count(s["text"], brand, checked):
            break
        if stop and _other_ad(s["text"], brand, checked):
            break
        passed.append(s)
    evidence = [s for s in passed if _ad_evidence(s["text"])]
    return evidence[-1] if evidence else last


def trim_end(row: dict, segs: list, checked: CheckedList) -> dict:
    """R7 for one row: display_end_s moved in from a whole-minute end (see
    the module docstring), else `row`."""
    lo, hi = bounds(row)
    if abs(hi - round(hi / 60) * 60) >= 0.01:
        return row
    inwin = [s for s in segs if s["end"] > lo and s["start"] < hi]
    brand = _row_brand(row, checked)
    if brand:
        hits = [s for s in inwin if _mentions_count(s["text"], brand, checked)]
        if not hits:
            return row
        last = _run_on(inwin, hits[-1], brand, checked, True)
        if min(hi, last["end"]) - lo < TRIM_MIN_S:
            # DET-SPANS-2: a stop at another ad's words that leaves the read
            # too short trims as before, never later
            last = _run_on(inwin, hits[-1], brand, checked, False)
        after = " ".join(s["text"] for s in segs if hi <= s["start"] < hi + TRIM_LOOK_S)
        if _mentions_count(after, brand, checked):
            return row
    else:
        evidence = [s for s in inwin if _ad_evidence(s["text"])]
        if not evidence or not any(STRONG_CUE_RE.search(s["text"]) for s in inwin):
            return row
        last = evidence[-1]
    end = min(hi, last["end"])
    if end == hi or end - lo < TRIM_MIN_S:
        return row
    return _record(row, {"rule": "R7", "set": {"display_start_s": lo, "display_end_s": end},
                         "was": {"display_start_s": row.get("display_start_s"),
                                 "display_end_s": row.get("display_end_s")},
                         "brand": brand or None, "end_anchor": round(last["end"], 2),
                         "was_end": hi})


def address_fires(segs: list, checked: CheckedList) -> list:
    """R9: [(brand, lo, hi, route, evidence)] for the web addresses and codes
    of checked brands in segments with no STRONG cue within ADDRESS_QUIET_S
    either side; a fire of the brand of the previous fire that starts within
    ADDRESS_QUIET_S of its end extends it."""
    strong = [s for s in segs if STRONG_CUE_RE.search(s["text"])]
    out: list = []
    for s in segs:
        found: dict = {}
        for brand, ev in url_brands(s["text"], checked).items():
            found[brand] = ("url", ev)
        for brand, ev in code_brands(s["text"], checked).items():
            found.setdefault(brand, ("code", ev))
        if not found or any(x["end"] > s["start"] - ADDRESS_QUIET_S
                            and x["start"] < s["end"] + ADDRESS_QUIET_S for x in strong):
            continue
        for brand, (route, ev) in found.items():
            prev = out[-1] if out else None
            if prev and prev[0] == brand and s["start"] <= prev[2] + ADDRESS_QUIET_S:
                out[-1] = (brand, prev[1], s["end"], prev[3], prev[4])
            else:
                out.append((brand, s["start"], s["end"], route, ev))
    return out


def address_rows(ep_id: str, record: dict, segs: list, slots: list, show, checked: CheckedList,
                 on: dict) -> list:
    """R9 over one episode. `slots` are [container, index] of the episode's
    current rows (a row is read, and named, in place); the new rows are
    returned. A fire is dropped when it is the show's own address (R1), when a
    live row within ADDRESS_QUIET_S already shows its brand, or when it
    overlaps a live row it does not close; it names the row it closes (see
    the module docstring, R9), else it becomes a new row."""
    new = []
    for brand, lo, hi, route, ev in address_fires(segs, checked):
        field = "brand_url" if route == "url" else "brand_code"
        fire = [lo, hi]
        rows = [c[i] for c, i in slots]
        live = [(bounds(r), j) for j, r in enumerate(rows) if not r.get("self_promo")]
        # the show's own address (R1): the address segments say the show's name
        if on["R1"] and _says_show(_text(segs, lo, hi), show):
            continue
        # a row within ADDRESS_QUIET_S already shows this brand: the same read
        if any(b0 < hi + ADDRESS_QUIET_S and b1 > lo - ADDRESS_QUIET_S
               and _same_brand(_shown(rows[j], checked), brand, checked) for (b0, b1), j in live):
            continue
        # an ad closes on its address: the fire belongs to the row that started
        # over EDGE_S before it and ended at most EDGE_S before it; in a row's
        # first EDGE_S it is the previous ad's close (R3), never that row's name
        own = [(b0, j) for (b0, b1), j in live if b0 <= lo - EDGE_S and b1 >= lo - EDGE_S]
        if own:
            j = max(own)[1]
            r = rows[j]
            if _shown(r, checked) or _has_checked(r, checked):
                continue
            held = bool(r.get("needs_review"))
            c, i = slots[j]
            c[i] = _record(r, {
                "rule": "R9", "set": {field: brand, **({"needs_review": False} if held else {})},
                "was": {field: r.get(field), **({"needs_review": r.get("needs_review")}
                                                if held else {})},
                "route": route, "evidence": ev, "fire": fire})
            continue
        if any(min(hi, b1) > max(lo, b0) for (b0, b1), _j in live):
            continue
        span = _span(segs, lo, hi)
        if span is None:
            continue
        lo2 = max([span[0]] + [b1 for (b0, b1), _j in live if b1 <= lo])
        hi2 = min([span[1]] + [b0 for (b0, b1), _j in live if b0 >= hi])
        if hi2 <= lo2:
            continue
        window = _text(segs, lo2, hi2)
        entry = {"rule": "R9", "set": {field: brand}, "route": route, "evidence": ev,
                 "fire": fire}
        row = _new_row(ep_id, record, rows, checked, "address", R9_SOURCE, "r9", field, brand,
                       lo2, hi2, window, entry, on["R11"])
        if on["R1"] and _rule_rows(row, segs, show, checked, on).get("self_promo"):
            continue
        new.append(row)
        slots.append([new, len(new) - 1])
    return new


def _family(cid: str) -> str:
    """The namer read a row belongs to: its id before any second-brand
    (".b-") or split (".split-") suffix."""
    return cid.split(".b-")[0].split(".split-")[0]


def same_ad(rows: list, checked: CheckedList) -> list:
    """R10 over one episode's rows (after R1-R9 and R7): a second-brand row
    (".b-" in its id, an R6 part of one included) that shows the brand a row
    of its own read shows (the read itself, its parts, an earlier
    second-brand row), over more than half of its own span, gets
    same_ad_as = that row's id: the same ad, which the importer does not
    load twice. Each R6 part of a marked row (its id is the marked row's id
    + ".split-") that shows no brand gets same_ad_as = the kept row of its
    read with the largest overlap, when that overlap is more than half of
    the part's own span; else the part stays."""
    out = list(rows)
    cids = [str(r.get("candidate_id")) for r in out]
    shown = [_shown(r, checked) for r in out]
    box = [bounds(r) for r in out]
    order = sorted(range(len(out)), key=lambda i: (".b-" in cids[i], cids[i]))
    kept: list = []
    marked: list = []   # the ids of the rows marked so far
    for i in order:
        cid, brand = cids[i], shown[i]
        lo, hi = box[i]
        target = None
        if ".b-" in cid and brand:
            for j in kept:
                k0, k1 = box[j]
                if _family(cids[j]) == _family(cid) \
                        and _same_brand(shown[j], brand, checked) \
                        and min(hi, k1) - max(lo, k0) > 0.5 * (hi - lo):
                    target = j
                    break
        elif ".b-" in cid and any(cid.startswith(m + ".split-") for m in marked):
            best = 0.0
            for j in kept:
                k0, k1 = box[j]
                over = min(hi, k1) - max(lo, k0)
                if _family(cids[j]) == _family(cid) and over > best:
                    target, best = j, over
            if best <= 0.5 * (hi - lo):
                target = None
        if target is None:
            kept.append(i)
            continue
        out[i] = _record(out[i], {"rule": "R10",
                                  "set": {"same_ad_as": out[target].get("candidate_id")},
                                  "was": {"same_ad_as": None}})
        marked.append(cid)
    return out


def apply(rows, transcripts, checked: CheckedList, shows=None, switches=None) -> list:
    """The rows an import loads: `rows` (named reads, any episodes) with the
    rules applied, in their order, less any row whose source is a rule's
    (ADDED_SOURCES: with a DET rule on only this apply makes those, it makes
    them again; with every DET rule off such a row passes through), then the
    new rows by episode: R6 parts, R1b rows, R9 rows. transcripts: {ep_id: transcript record
    (segments, fetch_date, fetch_date_source, show_title, ...)}; an episode
    with no record gets only R2. shows: {ep_id: show identity (self_promo
    identity shape)}; absent, the show title stands in. switches: {rule:
    bool} over RULES (DEFAULT_SWITCHES: all on but R5, R6, R7, R9, R10,
    R1b-spoken, R8-signoff and R11). Order of
    the rules: R1-R4 per row, R5, R6, R1b, R9, R7, R11, R10, R8. R8 runs last, on every
    row with a transcript record. The input is not changed."""
    on = dict(DEFAULT_SWITCHES)
    for k, v in (switches or {}).items():
        if k not in on:
            raise ValueError(f"unknown ad rule {k!r}; switches are {tuple(on)}")
        on[k] = bool(v)
    # keys as strings, as rows' ep_ids are read (str(ep_id)); non-dicts skipped
    det = any(on[rule] for rule in DET_RULES)   # off: rule-source rows load as at base
    transcripts = {str(k): v for k, v in transcripts.items()} \
        if isinstance(transcripts, dict) else {}
    shows = {str(k): v for k, v in shows.items()} if isinstance(shows, dict) else {}
    sorted_segs: dict[str, list] = {}
    out = []
    namer = []   # each output row's namer row and show identity, for R6
    for r in rows or ():
        if not isinstance(r, dict):
            continue
        ep = str(r.get("ep_id"))
        record = transcripts.get(ep)
        if not isinstance(record, dict):
            record = None
        elif ep not in sorted_segs:
            sorted_segs[ep] = _segments(record)
        show = _identity(shows.get(ep), (record or {}).get("show_title") or r.get("show_title"))
        if det and r.get("source") in ADDED_SOURCES:
            continue   # with a DET rule on only this apply makes such rows, below
        clean = _without_verdict(r)
        namer.append((clean, show))
        out.append(_rule_rows(clean, sorted_segs.get(ep), show, checked, on))
    if on["R5"]:
        at: dict[str, list] = {}
        for i, r in enumerate(out):
            at.setdefault(str(r.get("ep_id")), []).append(i)
        for ep, idx in at.items():
            if ep in sorted_segs and len(idx) > 1:
                for i, r in zip(idx, adjacency([out[i] for i in idx], sorted_segs[ep], checked)):
                    out[i] = r
    new: dict[str, list] = {}   # the rows the rules add, by episode
    if on["R6"]:
        for i, r in enumerate(out):
            ep = str(r.get("ep_id"))
            if sorted_segs.get(ep) is None:
                continue
            first, *parts = split_row(namer[i][0], r, sorted_segs[ep], namer[i][1], checked, on)
            out[i] = first
            new.setdefault(ep, []).extend(parts)
    if on["R1b"]:
        shown: dict[str, list] = {}   # the rows after R1-R5: R1 may have marked a read
        for r in out:
            shown.setdefault(str(r.get("ep_id")), []).append(r)
        for ep in sorted(transcripts):
            record = transcripts[ep]
            if not isinstance(record, dict):
                continue
            show = _identity(shows.get(ep), record.get("show_title"))
            for row in cue_rows(ep, record, shown.get(ep, []), checked, on["R1b-spoken"],
                                on["R11"]):
                # A cue that is the show's own promotion is no new ad (R1).
                if not (on["R1"] and _rule_rows(row, _segments(record), show, checked,
                                                 on).get("self_promo")):
                    new.setdefault(ep, []).append(row)
    if on["R9"]:
        for ep in sorted(transcripts):
            record = transcripts[ep]
            if not isinstance(record, dict):
                continue
            if ep not in sorted_segs:
                sorted_segs[ep] = _segments(record)
            show = _identity(shows.get(ep), record.get("show_title"))
            slots = [[out, i] for i, r in enumerate(out) if str(r.get("ep_id")) == ep]
            mine = new.setdefault(ep, [])
            slots += [[mine, i] for i in range(len(mine))]
            mine.extend(address_rows(ep, record, sorted_segs[ep], slots, show, checked, on))
    out += [r for ep in sorted(new) for r in new[ep]]
    if on["R7"]:
        for i, r in enumerate(out):
            segs = sorted_segs.get(str(r.get("ep_id")))
            if segs is None and isinstance(transcripts.get(str(r.get("ep_id"))), dict):
                segs = sorted_segs[str(r.get("ep_id"))] = _segments(transcripts[str(r.get("ep_id"))])
            if segs and r.get("source") not in (R1B_SOURCE, R9_SOURCE):
                out[i] = trim_end(r, segs, checked)
    if on["R11"]:
        repairs = said_repairs()
        for i, r in enumerate(out):
            ep = str(r.get("ep_id"))
            if ep not in sorted_segs and isinstance(transcripts.get(ep), dict):
                sorted_segs[ep] = _segments(transcripts[ep])
            if sorted_segs.get(ep):
                out[i] = said_addresses(r, sorted_segs[ep], checked, repairs)
    if on["R10"]:
        at: dict[str, list] = {}
        for i, r in enumerate(out):
            at.setdefault(str(r.get("ep_id")), []).append(i)
        for idx in at.values():
            for i, r in zip(idx, same_ad([out[i] for i in idx], checked)):
                out[i] = r
    if on["R8"]:
        for i, r in enumerate(out):
            ep = str(r.get("ep_id"))
            record = transcripts.get(ep)
            if not isinstance(record, dict):
                continue
            if ep not in sorted_segs:
                sorted_segs[ep] = _segments(record)
            out[i] = _clip_rule(r, sorted_segs[ep], checked, on["R8-signoff"])
    return out
