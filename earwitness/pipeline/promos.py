"""Network promos: a promo for another show is named as that show.

The label spec counts a promo for another podcast (the network's included)
as a read whose brand is the promoted show's title ("marketing school"). The
namer looks for advertisers, so such a span gets a platform ("apple
podcasts"), the network, or a word of the promo. This module finds the
promoted show in a flagged span's own text and names the span after it.

Show titles come from a versioned gazetteer built from the show lists on
disk (scripts/build_show_titles.py): {"format": FORMAT, "version", "sha256"
(of the entries), "built_at", "sources", "entries": [{"name", "key",
"forms", "networks", "feeds"}]}. `name` is the main title (before a
subtitle), lowercased; `forms` are the normalized texts that may be spoken
for it; `networks` the publishers the feeds name (itunes:author / owner).
Loading checks the digest; each names doc made with the gazetteer records
it (ShowTitles.describe()).

A span is a promo when its text has a promo cue ("wherever you get your
podcasts", "new episodes", "subscribe to", "a <network> podcast", ...) and
a show title:
  - a distinctive title form (2+ words that are neither filler nor genre
    words, or one 6+ letter word that is no dictionary word) with a cue
    near it
    (within CUE_NEAR_CHARS);
  - a show's whole name of 2+ other words only right next to a cue
    ("listen to the daily", "up first, wherever you get your podcasts").
The span's own show is never a promo: own-show outros stay self-promotion
(the export's self-promo filter names them by the show title).

The span brand becomes the show (display: the gazetteer name; the previous
brand is kept in promo.previous_brand), unless the previous brand is
spoken outside the promo's own text and is not a platform or network: then
the show joins the span as a second brand with the promo's own range (a
promo and a spot in one span). Every promo carries read_format "network
promo" and promo {"show", "how", "cue", "networks", "own_network",
"previous_brand"}. A promo for the episode's own network (a network the
show's feed names, spoken in the promo or publishing the promoted show)
records promo.own_network; with self_promo_own_network=True it also sets
promo.self_promo, and the export marks the read self-promo as the operator
rules do (self_promo.apply_rules, reason "network:<network>").
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from earwitness.pipeline import brand_namer as bn
from earwitness.pipeline import self_promo as sp

FORMAT = "earwitness-show-titles/1"
READ_FORMAT = "network promo"
MAX_TITLE_WORDS = 8
# Words that never make a title distinctive on their own.
TITLE_STOP = {
    "the", "a", "an", "and", "of", "with", "w", "in", "on", "to", "for", "my", "your",
    "our", "is", "it", "this", "that", "at", "by", "from", "me", "you", "we", "i",
    "podcast", "podcasts", "show", "pod", "radio", "episode", "episodes", "daily", "news",
    "live", "talk", "hour", "today", "weekly", "official", "presents", "network",
}
# Spoken platforms, apps and networks: never the promoted show, and a span
# named one of them is renamed to the show.
PLATFORMS = {
    "applepodcasts", "apple", "spotify", "iheartradio", "iheart", "iheartradioapp",
    "iheartpodcasts", "youtube", "youtubemusic", "amazonmusic", "audible", "pandora",
    "stitcher", "siriusxm", "sirius", "wondery", "wonderyplus", "audacy", "npr", "npr+",
    "podcast", "podcasts", "patreon", "apple podcast", "googlepodcasts", "castbox",
    "overcast", "pocketcasts", "podcastaddict", "applepodcast", "theiheartradioapp",
}
PROMO_CUE = re.compile(
    r"wherever you (?:get|listen to|find|stream) (?:your |all your |great )?(?:podcasts?|shows)"
    r"|(?:subscribe|follow)(?: (?:and|or) (?:listen|subscribe|follow))* (?:to|on|along)\b"
    r"|new episodes? (?:every|drop|are|is|out|available|come|of)\b"
    r"|(?:the |a |an )?(?:new |brand new |original )?(?:podcast|series|show|original) from \w+"
    r"|\b(?:a|an) [\w'&. -]{1,30} (?:podcast|original|production)\b"
    r"|search for\b|season \w+ (?:of|is|out|available)\b|full season\b"
    r"|(?:on|in) (?:apple podcasts|spotify|the \w+ ?(?:radio )?app|amazon music)\b", re.I)
CUE_NEAR_CHARS = 200  # a distinctive title needs a promo cue this close
# A cue that sits right next to a title ("listen to X" counts only here).
NEAR_BEFORE = re.compile(
    r"(?:listen to|subscribe to|follow|search for|check out|episodes? of|season \w+ of|"
    r"tune in to|binge)\s*$", re.I)
NEAR_AFTER = re.compile(
    r"^[\s,.]*(?:wherever|on apple podcasts|"
    r"is (?:a|an|available|out|back)|every \w+day|new episodes|podcast from|"
    r"(?:a|an) (?:new |brand new )?(?:\w+ ){0,2}(?:podcast|series|show)\b)", re.I)
# "hey, it's Bill Simmons from the Bill Simmons Podcast. Have you heard about
# ChatGPT": a host of the show voices a spot; the show is not promoted
INTRO_BEFORE = re.compile(r"\b(?:its|im|i am|this is)\s+(?:\w+\s+){0,3}from\s+(?:the\s+)?$")
SPEAKER_BEFORE = re.compile(r"\b(?:its|im|i am|this is)\s+$")  # "it's <title> from ...": a name
# "this is <host> from <title> and this episode is brought to you by X":
# the title introduces a sponsor read, it is not promoted
SPONSOR_AFTER = re.compile(r"^\W*(?:\w+\W+){0,3}(?:is |are )?(?:brought to you by|sponsored by|"
                           r"presented by|supported by)", re.I)
# podcast-ness: the span talks about a show
SHOW_WORD = re.compile(r"\b(?:podcasts?|episodes?|series|season|listen(?:ing)?|subscribe)\b",
                       re.I)
_TOKEN = re.compile(r"[a-z0-9]+")


def norm(text) -> str:
    """Lowercase words of `text` ("&" -> "and", apostrophes dropped)."""
    t = str(text or "").lower().replace("&", " and ").replace("'", "").replace("’", "")
    return " ".join(_TOKEN.findall(t))


def title_forms(title) -> list:
    """Spoken forms of a show title, normalized: the title, its main part
    (before a subtitle: ":", " - ", "|", "(", "with", "w/", ...), each also
    without a leading article and without a trailing "podcast" / "show"."""
    title = str(title or "")
    parts = [title] + [p for p in sp.TITLE_SPLIT.split(title)[:1] if p]
    out = []
    for p in parts:
        words = norm(p).split()
        variants = [words]
        if words and words[0] in ("the", "a", "an"):
            variants.append(words[1:])
        for v in list(variants):
            w = list(v)
            while w and w[-1] in ("podcast", "show", "pod", "radio"):
                w = w[:-1]
            variants.append(w)
        for v in variants:
            f = " ".join(v)
            if f and 1 <= len(v) <= MAX_TITLE_WORDS and f not in out:
                out.append(f)
    return out


def main_name(title) -> str:
    """The display name: the main title, lowercased."""
    main = sp.TITLE_SPLIT.split(str(title or ""))[0].strip() or str(title or "").strip()
    return re.sub(r"\s+", " ", main.lower()).strip(" .,!?")


# Genre and filler words: a title of only these ("true crime", "stories
# podcast", "the psychology of money") is said in passing far more often
# than it names a show.
GENRE = sp.GENERIC | {
    "true", "crime", "story", "stories", "history", "mystery", "mysteries", "murder",
    "murders", "comedy", "sports", "sport", "bible", "game", "games", "conversations",
    "conversation", "interview", "interviews", "money", "love", "music", "movie", "movies",
    "film", "tv", "politics", "science", "tech", "technology", "culture", "faith",
    "prayer", "god", "christ", "church", "kids", "women", "men", "girls", "guys", "people",
    "real", "good", "great", "big", "little", "best", "new", "old", "one", "two", "three",
    "first", "last", "next", "every", "all", "more", "most", "just", "only", "back",
    "out", "up", "down", "over", "off", "here", "there", "now", "then", "what", "why",
    "how", "who", "when", "where", "not", "no", "yes", "so", "too", "very", "much",
    "psychology", "mind", "minds", "brain", "body", "soul", "heart", "home", "house",
    "night", "morning", "evening", "week", "year", "years", "days", "dark", "light",
    "uncensored", "unfiltered", "explained", "unplugged", "edition", "series", "season",
    "chat", "chats", "talks", "stuff", "things", "thing", "way", "ways", "move", "moves",
}


FILLER = {"were", "was", "are", "be", "been", "being", "have", "has", "had", "do", "does",
          "did", "help", "can", "will", "would", "should", "could", "im", "youre", "were",
          "theyre", "dont", "cant", "wont", "its", "lets", "say", "said", "come", "came",
          "going", "gonna", "look", "keep", "give", "put", "call", "find", "show", "shows",
          "run", "through", "into", "about", "after", "before", "between", "under",
          "around", "bit", "better", "worse", "ever", "never", "always", "again", "still",
          "even", "really", "right", "left", "long", "short", "high", "low", "full", "half",
          "end", "start", "side", "part", "place", "world", "life", "lives", "man", "woman",
          "kind", "sort", "lot", "lots", "whole", "inside", "outside", "behind", "beyond",
          "without", "within", "against", "together", "alone", "everything", "nothing",
          "something", "anything", "someone", "everyone", "nobody", "somebody", "talking",
          "telling", "making", "getting", "doing", "being", "having", "saying"}


def distinctive(form: str, words: frozenset = frozenset()) -> bool:
    """Names a show when said with a promo cue: 2+ words that are neither
    filler nor genre words, or one such word of 6+ letters that is no
    dictionary word."""
    content = [w for w in form.split() if w not in TITLE_STOP and w not in GENRE
               and w not in bn.STOP and w not in FILLER and not w.isdigit()]
    if len(content) >= 2:
        return True
    w = content[0] if len(content) == 1 and len(form.split()) == 1 else ""
    return len(w) >= 6 and not w.isdigit() and not _word_form(w, words)


def _word_form(w: str, words: frozenset) -> bool:
    """A dictionary word or an inflection of one ("embedded", "stories")."""
    return w in words or any(w.endswith(x) and (w[:-len(x)] in words or w[:-len(x) - 1] in words
                                                or w[:-len(x)] + "e" in words)
                             for x in ("s", "es", "ed", "ing", "er", "ers", "ly", "ies"))


# a title of only these words is a genre ("true crime", "comedy podcast")
GENRE_CORE = {"the", "a", "an", "and", "of", "true", "crime", "story", "stories", "comedy",
              "history", "mystery", "mysteries", "murder", "murders", "sports", "business",
              "money", "love", "music", "faith", "bible", "game", "games", "health", "life",
              "podcast", "podcasts", "show", "pod", "radio", "politics", "science", "tech"}


def _named_by(form: str, entry: dict) -> bool:
    """A weak form names its show only as the show's whole name, 2+ words
    and not a genre ("the daily", "up first"; never "game" for "The Game",
    nor "true crime")."""
    return (len(form.split()) >= 2 and not set(form.split()) <= GENRE_CORE
            and form in {norm(entry["name"]), " ".join(norm(entry["name"]).split()[1:])})


def entries_digest(entries: list) -> str:
    return hashlib.sha256(json.dumps(entries, sort_keys=True, ensure_ascii=False)
                          .encode()).hexdigest()


def build(shows, *, built_at: str, version: int = 1, sources: dict | None = None) -> dict:
    """The gazetteer from [{"title", "feed_url"?, "networks"?}] (any
    order; one show may come from several lists). Entries are keyed on the
    main title's compact form; a key's forms, networks and feeds are the
    union over its shows."""
    by_key: dict = {}
    for s in shows:
        title = str(s.get("title") or "").strip()
        name = main_name(title)
        k = bn.compact(name)
        if len(k) < 3:
            continue
        e = by_key.setdefault(k, {"name": name, "key": k, "forms": [], "networks": [],
                                  "feeds": []})
        for f in title_forms(title):
            if f not in e["forms"]:
                e["forms"].append(f)
        for n in s.get("networks") or ():
            n = str(n).strip()
            if n and n not in e["networks"]:
                e["networks"].append(n)
        if s.get("feed_url") and s["feed_url"] not in e["feeds"]:
            e["feeds"].append(s["feed_url"])
    entries = []
    for e in sorted(by_key.values(), key=lambda e: e["key"]):
        entries.append({**e, "forms": sorted(e["forms"]), "networks": sorted(e["networks"]),
                        "feeds": sorted(e["feeds"])})
    return {"format": FORMAT, "version": version, "sha256": entries_digest(entries),
            "built_at": built_at, "sources": dict(sources or {}), "entries": entries}


class ShowTitles:
    def __init__(self, doc: dict, words: frozenset = frozenset()):
        if not isinstance(doc, dict) or doc.get("format") != FORMAT:
            raise ValueError(f"not a show-title gazetteer ({FORMAT})")
        entries = doc.get("entries") or []
        if entries_digest(entries) != doc.get("sha256"):
            raise ValueError("show-title gazetteer digest does not match its entries")
        self.doc, self.sha256, self.version = doc, doc["sha256"], doc.get("version")
        self.words = words
        self.by_form: dict = {}   # form -> (entry | None when ambiguous, distinctive)
        self.by_feed: dict = {}
        claims: dict = {}
        for e in entries:
            for f in e.get("forms") or ():
                if f not in TITLE_STOP and bn.compact(f) not in PLATFORMS:
                    claims.setdefault(f, []).append(e)
            for feed in e.get("feeds") or ():
                self.by_feed[feed] = e
        for f, es in claims.items():
            # a form several shows share names the one whose name it is, else no one
            named = [e for e in es if norm(e["name"]) == f]
            e = es[0] if len(es) == 1 else (named[0] if len(named) == 1 else None)
            self.by_form[f] = (e, distinctive(f, words))

    @classmethod
    def load(cls, path, words=None) -> "ShowTitles":
        """words None: the namer's system word list."""
        return cls(json.loads(Path(path).read_text(encoding="utf-8")),
                   bn._load_words() if words is None else words)

    def describe(self) -> dict:
        return {"version": self.version, "sha256": self.sha256,
                "entries": len(self.doc.get("entries") or ())}

    def find(self, text_norm: str) -> list:
        """[(entry, form, distinctive, start char, end char)] of title forms
        in normalized text, longest first at each position, not overlapping."""
        toks = list(re.finditer(r"\S+", text_norm))
        out, i = [], 0
        while i < len(toks):
            hit = None
            for n in range(min(MAX_TITLE_WORDS, len(toks) - i), 0, -1):
                f = " ".join(t.group(0) for t in toks[i:i + n])
                got = self.by_form.get(f)
                if got is not None and got[0] is not None:
                    hit = (got[0], f, got[1], toks[i].start(), toks[i + n - 1].end(), n)
                    break
            if hit:
                out.append(hit[:5])
                i += hit[5]
            else:
                i += 1
        return out


def own_keys(show_title, ident: dict | None) -> set:
    """Compact keys that name the episode's own show (title, its main part,
    identity names that are not networks)."""
    names = [show_title, main_name(show_title)] + list((ident or {}).get("names") or ())
    return {bn.compact(f) for n in names if n and not sp.is_network(n)
            for f in title_forms(n)} - {""}


def own_networks(ident: dict | None, own=frozenset()) -> list:
    """The publishers the show's feed names (authors, owners), as compact
    keys without network words ("Hubspot Media" -> "hubspot"), minus the
    show's own names (`own`: own_keys; a host's own company is no network)."""
    out = []
    for n in list((ident or {}).get("authors") or ()) + list((ident or {}).get("owners") or ()):
        words = [w for w in norm(n).split() if w not in sp.NETWORK_WORDS
                 and w not in ("and", "the", "inc", "llc", "corp", "co", "studios", "studio")]
        k = "".join(words)
        if len(k) >= 3 and k not in out and k not in own:
            out.append(k)
    return out


def _near_cue(text_norm: str, a: int, b: int) -> bool:
    return bool(NEAR_BEFORE.search(text_norm[max(0, a - 40):a])
                or NEAR_AFTER.search(text_norm[b:b + 40]))


def find_promo(text: str, shows: ShowTitles, *, own=frozenset(), stopped=None) -> dict | None:
    """The promoted show in one span's text: {"show", "how": "gazetteer",
    "cue", "entry", "span": (start char, end char) in the normalized text}
    or None. The span's own show (`own`: compact keys) is never promoted,
    nor a show whose name the operator stoplisted (`stopped(name)`:
    corrections.Corrections.stopped)."""
    t = norm(text)
    if not SHOW_WORD.search(t):
        return None
    cues = list(PROMO_CUE.finditer(t))
    hits = []
    for e, f, dist, a, b in shows.find(t):
        if (bn.compact(f) in own or e["key"] in own or SPONSOR_AFTER.search(t[b:b + 80])
                or (stopped is not None and stopped(e["name"]))
                or INTRO_BEFORE.search(t[max(0, a - 60):a])
                or (SPEAKER_BEFORE.search(t[max(0, a - 15):a]) and t[b:b + 6] == " from ")):
            continue
        adjacent = _near_cue(t, a, b)
        near = min(cues, key=lambda m: max(0, m.start() - b, a - m.end()), default=None)
        close = near is not None and max(0, near.start() - b, a - near.end()) <= CUE_NEAR_CHARS
        if adjacent and (dist or _named_by(f, e)):
            hits.append((0, a, e, "adjacent", b))     # "listen to <title>" first
        elif dist and close:
            hits.append((1, a, e, near.group(0), b))
    if not hits:
        return None
    _, a, e, cue, b = min(hits, key=lambda h: h[:2])
    return {"show": e["name"], "how": "gazetteer", "cue": cue, "entry": e, "span": (a, b)}


def _weak_brand(brand, own=frozenset(), networks=()) -> bool:
    k = bn.compact(str(brand or ""))
    return (not k or k in PLATFORMS or k in own or sp.is_network(str(brand or ""))
            or any(n and n in k for n in networks) or k in sp.NETWORKS)


def _seg_range(segments, lo, hi, needle_norm: str):
    """Times of the segments in [lo, hi] whose text has `needle_norm`."""
    hits = [s for s in bn._text_between(segments, lo, hi) if needle_norm
            and needle_norm in norm(s.get("text"))]
    return (min(s["start"] for s in hits), max(s["end"] for s in hits)) if hits else None


def name_promos(names_doc: dict, segments, shows: ShowTitles, *, own=frozenset(),
                networks=(), self_promo_own_network: bool = False,
                pad_s: float = 3.0, protected=frozenset(), stopped=None) -> tuple:
    """(doc, changes): each span whose own text (+ `pad_s`) is a promo for
    another show is named after it (module doc). `own`: compact keys of the
    episode's show (own_keys); `networks`: its publishers (own_networks).
    Spans in `protected` (a person confirmed them) stay as they are;
    `stopped`: the operator's stoplist (find_promo)."""
    changes = []
    spans = []
    for span in names_doc.get("spans") or ():
        if span.get("span_id") in protected:
            spans.append(span)
            continue
        span = dict(span)
        lo, hi = span["start_s"] - pad_s, span["end_s"] + pad_s
        text = bn._segments_text(bn._text_between(segments, lo, hi))
        hit = find_promo(text, shows, own=own, stopped=stopped)
        if hit is None:
            spans.append(span)
            continue
        e = hit["entry"]
        tn = norm(text)
        spoken_nets = [n for n in networks if n and n in tn.replace(" ", "")]
        show_nets = [k for k in own_networks({"authors": e.get("networks")}) if k in networks]
        own_net = (spoken_nets or show_nets or [None])[0]
        prev = span.get("brand")
        promo = {"show": hit["show"], "how": hit["how"], "cue": hit["cue"],
                 "networks": list(e.get("networks") or ()), "own_network": own_net,
                 "previous_brand": prev}
        if own_net and self_promo_own_network:
            promo["self_promo"] = True
        same = prev and (bn.compact(prev) == bn.compact(hit["show"])
                         or bn.compact(prev) in {bn.compact(f) for f in e.get("forms") or ()})
        rng = _seg_range(segments, lo, hi, norm(hit["show"])) or \
            _seg_range(segments, lo, hi, tn[hit["span"][0]:hit["span"][1]])
        kept = False
        if prev and not same and not _weak_brand(prev, own, networks):
            pk = bn.compact(prev)
            outside = [s for s in bn._text_between(segments, span["start_s"], span["end_s"])
                       if rng is None or s["end"] <= rng[0] or s["start"] >= rng[1]]
            kept = any(pk and pk in bn.compact(s.get("text") or "") for s in outside)
        if kept:
            a = max(span["start_s"], rng[0]) if rng else span["start_s"]
            b = min(span["end_s"], rng[1]) if rng else span["end_s"]
            sb = {"brand": hit["show"], "score": None, "evidence": [f"promo:{hit['how']}"],
                  "rank": None, "start_s": a, "end_s": b,
                  "jev_window_ids": list(span.get("jev_window_ids") or ()),
                  "read_format": READ_FORMAT, "promo": promo}
            span["second_brands"] = [x for x in span.get("second_brands") or ()
                                     if bn.compact(x.get("brand") or "")
                                     != bn.compact(hit["show"])] + [sb]
            changes.append({"span_id": span.get("span_id"), "second": hit["show"],
                            "how": hit["how"]})
        else:
            span["brand"] = hit["show"]
            span["read_format"] = READ_FORMAT
            span["promo"] = promo
            span["needs_review"] = False
            # platform / network second brands were words of the promo
            span["second_brands"] = [x for x in span.get("second_brands") or ()
                                     if not _weak_brand(x.get("brand"), own, networks)
                                     and bn.compact(x.get("brand") or "")
                                     != bn.compact(hit["show"])]
            if not same:
                changes.append({"span_id": span.get("span_id"), "from": prev,
                                "to": hit["show"], "how": hit["how"]})
        spans.append(span)
    return {**names_doc, "spans": spans}, changes
