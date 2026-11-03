"""Real advertiser names: a versioned list the brand repair can trust.

The brand lists the namer repairs onto are harvested from our own
transcripts, so they carry the speech model's mishearings ("coligan",
"linkin", "blissie"): a garble that recurs becomes an anchor. This list is
built from sources outside the transcripts (scripts/build_advertisers.py):

  corrections  an owner's corrections export: new brands and aliases
  notes        brand names as show notes write them (sponsor listings)
  seed         a curated seed of real podcast advertisers
  canon        corpus canon anchors with strong support (spoken as a
               domain on several shows); exact matches and collapse heads
               only, never a sound-alike target or repair evidence,
               because a recurring mishearing can reach that support too

File: {"format": FORMAT, "version", "sha256" (of the entries), "built_at",
"sources": {name: {...}}, "entries": [{"name", "key", "aliases", "tier"}]}.
The digest is recorded in each names doc that used the list (describe()),
so a changed list shows up as a change, like the brand list digest.

Two uses, both off unless a list is given:
  match(name)     exact name or alias -> the entry; else a sound-alike of a
                  trusted entry (tiers corrections/notes/seed; >= 5 letters;
                  one best target, never two equally good ones)
  collapse(name)  a product phrase -> its brand: "colgate optic white pro
                  series toothpaste" -> "colgate" when "colgate" is known
                  and the whole phrase is not ("amazon music" stays)
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from earwitness.pipeline import brand_namer as bn

FORMAT = "earwitness-advertisers/1"
TIERS = ("corrections", "notes", "seed", "canon")  # trust order
FUZZY_TIERS = ("corrections", "notes", "seed")
CURATED_TIERS = ("corrections", "seed")  # a one-word dictionary head must be one
MIN_FUZZY_LEN = 5
TLD_WORDS = {"com", "net", "org", "edu", "io", "co", "fm", "ai", "app", "tv", "me", "dot"}


def key(name) -> str:
    return bn.compact(str(name or ""))


def entries_digest(entries: list) -> str:
    return hashlib.sha256(json.dumps(entries, sort_keys=True, ensure_ascii=False)
                          .encode()).hexdigest()


def build(sources: dict, *, built_at: str, version: int = 1,
          words: frozenset = frozenset()) -> dict:
    """The list document from {tier: [{"name", "aliases"?, ...}]} (tiers in
    TIERS; a notes item may carry "feeds", the feeds listing it). An entry's
    key is its compact name; a name met in several tiers keeps the most
    trusted tier and the union of aliases. A key claimed as an alias by a
    more trusted entry is that entry's ("forhers" -> hers), not an entry of
    its own. A notes spelling one letter off a curated entry or a notes
    entry on more feeds (7+ letters, or a one-letter prefix) becomes its
    alias (a typo: "betterhel", "jinfinti"). Canon forms
    are dropped when they are a dictionary word (`words`), a sound-alike, a
    same-metaphone form or a prefix of a trusted entry: those are
    mishearings that recur ("linkin", "zippercruiter", "zock")."""
    by_key: dict = {}
    n: dict = {}  # notes: feeds listing the name
    for tier in TIERS:
        for item in sources.get(tier) or ():
            name = str(item.get("name") or "").strip()
            k = key(name)
            if len(k) < 2:
                continue
            if tier == "notes":
                n[k] = max(n.get(k, 0), int(item.get("feeds") or 1))
            e = by_key.setdefault(k, {"name": name.lower(), "key": k, "aliases": [],
                                      "tier": tier})
            for a in item.get("aliases") or ():
                ak = key(a)
                if ak and ak != k and ak not in e["aliases"]:
                    e["aliases"].append(ak)
    rank = {t: i for i, t in enumerate(TIERS)}
    for e in sorted(by_key.values(), key=lambda e: rank[e["tier"]]):
        if e["key"] not in by_key:
            continue
        for a in e["aliases"]:
            other = by_key.get(a)
            if other is not None and rank[other["tier"]] > rank[e["tier"]]:
                del by_key[a]
    for e in by_key.values():
        e["aliases"] = sorted(a for a in e["aliases"] if a not in by_key)
    # a notes spelling that is a sound-alike of a curated entry, or of a
    # notes entry listed on more feeds, is that entry's typo ("betterhel")
    weight = {k: n.get(k, 0) for k in by_key}
    for k in sorted([k for k, e in by_key.items() if e["tier"] == "notes"],
                    key=lambda k: (weight[k], k)):
        e = by_key[k]
        others = Advertisers._from_entries([o for o in by_key.values()
                                            if o["tier"] in FUZZY_TIERS and o is not e])
        hit = others.match(e["name"])
        if hit and hit[1] == "sound-alike":
            f = hit[0]
            fk = f["key"]
            typo = (len(k) >= 7 and bn.levenshtein(k, fk) == 1) or (
                fk.startswith(k) and len(fk) - len(k) == 1)
            if typo and (f["tier"] in CURATED_TIERS or weight.get(fk, 0) > weight[k]):
                del by_key[k]
                f = by_key[f["key"]]
                f["aliases"] = sorted(set(f["aliases"]) | {k} | set(e["aliases"]))
    trusted = Advertisers._from_entries([e for e in by_key.values()
                                         if e["tier"] in FUZZY_TIERS])
    tkeys = [t for t in trusted.by_key if len(t) >= 4]
    tmph = {bn.metaphone(t) for t in tkeys}
    for k in [k for k, e in by_key.items() if e["tier"] == "canon"]:
        name = by_key[k]["name"]
        if (name in words or k in words or trusted.match(name) is not None
                or bn.metaphone(k) in tmph
                or any(t != k and t.startswith(k) for t in tkeys)):
            del by_key[k]
    entries = sorted(by_key.values(), key=lambda e: e["key"])
    return {"format": FORMAT, "version": version, "sha256": entries_digest(entries),
            "built_at": built_at,
            "sources": {t: len(sources.get(t) or ()) for t in TIERS},
            "entries": entries}


class Advertisers:
    @classmethod
    def _from_entries(cls, entries: list) -> "Advertisers":
        return cls({"format": FORMAT, "version": None, "sha256": entries_digest(entries),
                    "entries": entries})

    def __init__(self, doc: dict, words: frozenset = frozenset()):
        # dictionary words (brand_namer word list): never sound-alike sources
        self.words = words
        if not isinstance(doc, dict) or doc.get("format") != FORMAT:
            raise ValueError(f"not an advertiser list ({FORMAT})")
        entries = doc.get("entries") or []
        if entries_digest(entries) != doc.get("sha256"):
            raise ValueError("advertiser list digest does not match its entries")
        self.doc, self.sha256, self.version = doc, doc["sha256"], doc.get("version")
        self.by_key: dict = {}
        for e in entries:
            self.by_key[e["key"]] = e
            for a in e.get("aliases") or ():
                self.by_key.setdefault(a, e)
        self.trusted_keys = {k for k, e in self.by_key.items() if e["tier"] in FUZZY_TIERS}
        self.fuzzy = [e for e in entries if e["tier"] in FUZZY_TIERS
                      and len(e["key"]) >= MIN_FUZZY_LEN]
        self._mph = {e["key"]: bn.metaphone(e["key"]) for e in self.fuzzy}

    @classmethod
    def load(cls, path, words=None) -> "Advertisers":
        """words None: the namer's system word list."""
        return cls(json.loads(Path(path).read_text(encoding="utf-8")),
                   bn._load_words() if words is None else words)

    def describe(self) -> dict:
        return {"version": self.version, "sha256": self.sha256,
                "entries": len(self.doc.get("entries") or ())}

    def known(self, name) -> bool:
        return key(name) in self.by_key

    def trusted(self, name) -> bool:
        """On the list from a trusted tier (not canon only)."""
        return key(name) in self.trusted_keys

    def match(self, name):
        """(entry, "exact" | "sound-alike") or None."""
        k = key(name)
        if not k:
            return None
        if k in self.trusted_keys:
            return self.by_key[k], "exact"
        canon = self.by_key.get(k)
        if len(k) < MIN_FUZZY_LEN:
            return (canon, "exact") if canon else None
        m = bn.metaphone(k)
        nw = str(name).lower().split()
        best, score, tie = None, 0.0, False
        for e in self.fuzzy:
            ek = e["key"]
            ew = e["name"].split()
            if len(nw) > 1 and len(ew) > 1 and nw[0] == ew[0] and nw[1:] != ew[1:]:
                continue  # same brand word, other words: a sibling ("youtube site" / "youtube tv")
            if abs(len(ek) - len(k)) > 3:
                continue
            short, long_ = sorted((k, ek), key=len)
            if long_.startswith(short) and len(long_) - len(short) >= 2:
                continue  # another brand that extends it ("youtube" / "youtube tv")
            jw = bn.jaro_winkler(k, ek)
            lev = None
            ok = jw >= 0.92
            if not ok and (self._mph[ek] == m or ek[:3] == k[:3]):
                lev = bn.levenshtein(k, ek)
                ok = lev <= (1 if len(k) < 7 else 2) and jw >= 0.8
            if not ok:
                continue
            if jw > score + 1e-9:
                best, score, tie = e, jw, False
            elif abs(jw - score) <= 1e-9 and e is not best:
                tie = True
        if best is None or tie:
            return (canon, "exact") if canon else None
        return best, "sound-alike"

    def collapse(self, name, heads=(), words=frozenset()):
        """(brand head, product phrase) when `name` is a product phrase of a
        known brand, else None. The head is the longest leading run of words
        (trailing domain words like "edu" dropped) that is on this list or
        in `heads` (e.g. the run's candidates with a spoken domain), and the
        whole name must not be a known brand itself, so distinct brands that
        share a first word stay apart ("amazon music", "apple card"), and
        a product phrase has 2+ words after the brand: one word is more
        often a sub-brand the list lacks ("google chrome"). A
        one-word head that is a dictionary word (`words`) must be on this
        curated entry (corrections or seed), not only in `heads` or the
        notes ("better results" never becomes "better", "mint mobile" never
        "mint" from a show-notes listing of Mint)."""
        words_ = [w for w in re.findall(r"[a-z0-9&'+.-]+", str(name or "").lower())
                  if w.strip(".")]
        if len(words_) < 2 or self.known(name):
            return None
        hk = {key(h) for h in heads or () if key(h)}
        while len(words_) > 1 and words_[-1].strip(".") in TLD_WORDS:
            words_ = words_[:-1]
        if len(words_) == 1:  # "snhu edu" -> "snhu"
            w = words_[0]
            return (w, str(name)) if (key(w) in self.by_key or key(w) in hk) else None
        for n in range(len(words_) - 2, 0, -1):  # 2+ words after the brand
            head = " ".join(words_[:n])
            hkey = key(head)
            if len(hkey) < 3:
                continue
            listed = hkey in self.by_key
            if not (listed or hkey in hk):
                continue
            if n == 1 and head in words and (self.by_key.get(hkey) or {}).get(
                    "tier") not in CURATED_TIERS:
                continue
            return head, str(name)
        return None

    def canonical(self, name, heads=(), words=frozenset()) -> tuple:
        """(display name, rule, product) for one brand name: collapse a
        product phrase, then map onto the list (exact or sound-alike).
        rule None and product None: unchanged."""
        if not name:
            return name, None, None
        words = words or self.words
        product = None
        rules = []
        c = self.collapse(name, heads, words)
        if c:
            name, product = c[0], c[1]
            rules.append("collapse")
        hit = self.match(name)
        w = str(name).lower()
        if hit and hit[1] == "sound-alike" and (w in words or w.endswith("s") and w[:-1] in words):
            hit = None  # a real word is that word ("courses" is not Coursera)
        if hit:
            e, how = hit
            if e["name"] != str(name).lower():
                rules.append("advertiser" if how == "exact" else "advertiser-sound-alike")
                name = e["name"]
        return name, ("+".join(rules) or None), product
