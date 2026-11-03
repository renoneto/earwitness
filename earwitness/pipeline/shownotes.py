"""Show notes as brand evidence for named ad reads.

An episode's show notes (its RSS item description, content:encoded and
itunes:summary) often list the episode's sponsors: "SPONSORS:" blocks,
"This episode is brought to you by X", promo codes ("use code SHOW") and
vanity links ("go to x.com/SHOW"). This module stores them per episode and
uses them to confirm or correct the brand of a named read we already found.
It never creates an ad: inserted (DAI) ads are often not listed, and a
listing does not prove a read ran.

Storage: <corpus>/shownotes/<episode_id>.json, one file per episode,
written by `discover` from the feed it already fetched (store_feed_notes):
  {version, episode_id, feed_url, guid, first_seen_at, updated_at,
   sha256 (of the stored fields), fields: [{field, type, value}]}
Fields are the raw item texts (HTML as in the feed), each capped at
FIELD_MAX_BYTES (UTF-8); a field whose text another kept field already contains is
dropped. A file is rewritten only when its fields change.

The pass (`corpus_run.py shownotes`, run_pass) writes
<corpus>/shownotes-reads/<episode_id>.json per episode with named reads:
  {version, episode_id, notes_sha256, sponsors: [listing...],
   reads: {candidate_id: {status, match, brand, brand_key, written, evidence,
           clears_review,
           brand_in, canonical_in, start_s, end_s, text_sha256}}}
status: confirmed | corrected | not_listed | no_notes. match: exact |
alias | domain | fuller-name | sound-alike (None when not listed;
"ambiguous" when two listed brands fit equally, or when a listed domain
spoken in the read is not the brand the read itself names). No match
clears needs_review (a supporting signal only). A correction changes only the read's
suggested brand, in its own per-read field shownotes_brand; never
brand_canonical (canon's corpus-wide map), brand / brand_spoken / brand_raw
or the span (evidence identity).

export (apply_to_candidates) copies a read's result onto its named
candidate when the candidate still has the brand, span and text the pass
saw and the episode's notes are unchanged; otherwise the read is counted
stale and left without show-notes attrs.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from earwitness.gazetteer import normalize_brand
from earwitness.pipeline import brand_canon
from earwitness.pipeline import brand_namer as bn
from earwitness.pipeline import self_promo as sp
from earwitness.pipeline.chunked_stt import atomic_write_json

NOTES_DIR = "shownotes"
READS_DIR = "shownotes-reads"
NOTES_VERSION = 1
READS_VERSION = 2               # 2: notes and read-text digests for the stale check
FIELD_MAX_BYTES = 64 * 1024     # per note field, UTF-8
EVIDENCE_MAX_CHARS = 300
BLOCK_MAX_LINES = 40          # a sponsor block ends after this many non-empty lines
MAX_LISTINGS = 50             # per episode; real notes list a handful
STATUSES = ("confirmed", "corrected", "not_listed", "no_notes")
# No match clears needs_review: in the app a needs-review read fires no
# tracker until a person confirms it, so the notes only add a signal.
CLEARING_MATCHES = ()
EXPORT_ATTRS = ("shownotes_status", "shownotes_match", "shownotes_brand",
                "shownotes_evidence")

_INVISIBLE = re.compile("[​‌‍⁠﻿­]")
_BULLET = re.compile(r"^[\s\-–—•*·▪►➡️👉→>|]+")
_SEPARATOR = re.compile(r"^[\s\-–—_=*~▬━─•.]{3,}$")
# A line that names sponsors, or opens a block of them. A sponsor phrase
# counts anywhere; a bare word ("partners", "advertisers") only as a label
# ("Partner: X", "Our sponsors: A, B") or a heading ("GLENN'S SPONSORS"),
# so prose ("advertisers fled Twitter", "his business partner Jeff") is none.
SPONSOR_PHRASE = re.compile(
    r"\b(?:sponsored by|made possible by|brought to you by|"
    r"supported by|support for (?:this|the) (?:show|podcast|episode)(?: comes from)?|"
    r"thanks? (?:you )?to (?:our|today'?s|this week'?s) (?:sponsors?|partners?))\b", re.I)
SPONSOR_WORD = re.compile(r"\b(?:sponsors?|(?:show |our )?partners?|advertisers?)\b(\s*:)?",
                          re.I)


def _sponsor_cue(plain: str, heading: bool):
    """The sponsor cue of a line (a match whose end is where names start), or None."""
    m = SPONSOR_PHRASE.search(plain)
    if m:
        return m
    m = SPONSOR_WORD.search(plain)
    return m if m and (m.group(1) or heading) else None
CODE_RE = re.compile(r"\b(?:promo |discount |offer |coupon )?code[\s:]*[\"“”'‘’]?"
                     r"([A-Za-z0-9][A-Za-z0-9_-]{1,23})\b", re.I)
OFFER_CUE = re.compile(r"(?:\b\d{1,3}\s?% off|\$\s?\d[\d,]*\s?(?:off|credit|bonus)|"
                       r"\bfree (?:shipping|trial|month|bottle|sample|gift)|\bfirst (?:month|order|"
                       r"purchase|box)\b|\brisk[- ]free\b|\bmonths? free\b)", re.I)
CTA_CUE = re.compile(r"\b(?:go to|head to|visit|learn more at|check out|sign up at|"
                     r"get started at|shop at|available at|join at|try)\b", re.I)
# Sections that end a sponsor block (headings, credits, self-promo).
SECTION_WORDS = re.compile(
    r"\b(?:timestamps?|chapters|socials?|social media|follow|links?|guests?|hosts?|"
    r"credits?|sources?|resources|about|connect|subscribe|listen|watch|find us|contact|"
    r"producers?|production|music|newsletter|merch|disclaimer|show ?notes|reminder|"
    r"extra|support (?:us|the show)|patreon|instagram|youtube|tiktok|facebook|twitter|"
    r"transcript|episode resources|upcoming)\b", re.I)
FIRST_PERSON_PROMO = re.compile(
    r"\b(?:my|our) (?:foundation|merch|shop|store|documentar\w*|other podcasts?|podcasts?|"
    r"books?|course|patreon|newsletter|channel|socials?|community|live shows?|tour)\b", re.I)
FIRST_PERSON = re.compile(r"\b(?:my|our|mine)\b", re.I)
# "Join Pearler using the code ..." -> Pearler (a code line without a sponsor link).
CTA_NAME = re.compile(r"\b(?:join|try|use|download|get|visit|with)\s+"
                      r"([A-Z][\w&'’.-]*(?:\s+[A-Z][\w&'’.-]*){0,2})")
# Not a sponsor's name: calls to action, self references, generic words.
NAME_STOP = re.compile(
    r"\b(?:my|our|your|us|we|check|follow|watch|listen|subscribe|support|visit|click|"
    r"download|get|go|use|code|episode|podcast|show|today|here|links?|this|that|and the|"
    r"sponsors?|partners?|resources|learn|more|join|thanks?|please|see|privacy|policy)\b", re.I)
# Network / company suffixes dropped from listed names ("HubSpot Media").
NAME_SUFFIXES = {"media", "inc", "llc", "ltd", "co", "company", "corp", "group", "network",
                 "podcasts", "studios", "productions", "app", "official"}
BOILERPLATE = re.compile(
    r"ad ?choices|podcastchoices|privacy (?:policy|information|notice)|omnystudio\.com/listener|"
    r"adswizz|art19\.com/privacy|acast\.com/privacy|do[- ]not[- ]sell|opt[- ]out|"
    r"hosted (?:by|on) (?:simplecast|acast|megaphone|libsyn|spreaker|buzzsprout|podbean|"
    r"omny|transistor|captivate|redcircle|art19)|supporter\.acast|plus\.acast|"
    r"become a supporter of this podcast|listen ad[- ]free|without ads|ad[- ]free|"
    r"responsible gaming|gambling problem|1-800-gambler", re.I)
# Link hosts that never name a sponsor (beyond self_promo's platform lists).
SKIP_LABELS = {"amzn", "spoti", "goo", "forms", "typeform", "safelinks", "apple", "linktr",
               "signalaward", "swap", "wikipedia", "op3", "clrtpod", "mgln", "pscrb",
               "dts", "podtrac", "chrt", "pdst", "arttrk", "claritaspod", "prfx", "byspotify",
               # link shorteners / affiliate redirects: shared by many sponsors
               "geni", "smarturl", "trib", "zpr", "shareasale", "rstyle", "shopstyle", "howl",
               "skimresources", "sjv", "anrdoezrs", "dpbolvw", "tkqlhce", "kqzyfj", "jdoqocy",
               "awin1", "impactradius", "pxf", "ojrq", "mavely", "liketk", "rebrand", "cutt",
               "shorturl", "t2m", "urlgeni", "hyperurl", "fanlink", "ffm", "lnkfi", "linkfire"}
LABEL_SUFFIXES = ("podcast", "pod", "show", "app", "hq", "inc", "official")
REDIRECT_KEYS = ("url", "u", "dest", "destination", "target", "redirect", "to")
UNWRAP_MAX_LEVELS = 5


# ---------------------------------------------------------------- notes capture

class _Text(HTMLParser):
    """HTML -> lines of (text, [(anchor text, href)]). Block tags break lines."""

    BLOCK = {"p", "br", "li", "div", "ul", "ol", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
             "blockquote", "hr", "section", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines, self.cur, self.links = [], [], []
        self.href, self.anchor = None, []

    def _break(self):
        text = re.sub(r"[ \t\r\f\v]+", " ", "".join(self.cur)).strip()
        if text or self.links:
            self.lines.append((text, self.links))
        elif self.lines and self.lines[-1] != ("", []):
            self.lines.append(("", []))
        self.cur, self.links = [], []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK:
            self._break()
        if tag == "a":
            self.href, self.anchor = dict(attrs).get("href"), []

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        if tag == "a" and self.href is not None:
            self.links.append(("".join(self.anchor).strip(), self.href.strip()))
            self.href = None
        if tag in self.BLOCK:
            self._break()

    def handle_data(self, data):
        for i, part in enumerate(data.split("\n")):
            if i:
                self._break()
            self.cur.append(part)
            if self.href is not None:
                self.anchor.append(part)

    def close(self):
        super().close()
        self._break()
        return self.lines


def to_lines(value: str) -> list:
    """Lines of (text, links) of one note field (HTML or plain text)."""
    value = _INVISIBLE.sub("", value or "")
    if "<" in value and ">" in value:
        p = _Text()
        p.feed(value)
        return p.close()
    return [(re.sub(r"[ \t]+", " ", html.unescape(line)).strip(), [])
            for line in value.splitlines()]


def plain_text(value: str) -> str:
    return "\n".join(t for t, _ in to_lines(value))


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def _cap(value: str) -> str:
    """At most FIELD_MAX_BYTES of UTF-8, cut on a character boundary."""
    return value.encode("utf-8")[:FIELD_MAX_BYTES].decode("utf-8", "ignore")


def entry_fields(entry: dict) -> list:
    """The note fields of a feedparser entry: description (`summary`),
    content:encoded / itunes:summary (`content`), itunes:subtitle. A field
    whose text another kept field already contains is dropped."""
    raw = [("description", (entry.get("summary_detail") or {}).get("type"),
            entry.get("summary"))]
    for c in entry.get("content") or ():
        if isinstance(c, dict):
            raw.append(("content", c.get("type"), c.get("value")))
    raw.append(("subtitle", None, entry.get("subtitle")))
    cand = [(f, t, _cap(str(v))) for f, t, v in raw if v and str(v).strip()]
    texts = [_squash(plain_text(v)) for _, _, v in cand]
    keep = []
    for i, (f, t, v) in enumerate(cand):
        if not texts[i]:
            continue
        # same text: keep the richest markup (links), then the first
        dup = any(j != i and texts[i] in texts[j] and (
            len(texts[j]) > len(texts[i])
            or (len(cand[j][2]), -j) > (len(cand[i][2]), -i)) for j in range(len(cand)))
        if not dup:
            keep.append({"field": f, "type": t, "value": v})
    return keep


def notes_path(corpus, episode_id: str) -> Path:
    return Path(corpus) / NOTES_DIR / f"{episode_id}.json"


def _write_json(path: Path, doc) -> None:
    atomic_write_json(path, doc)  # fsync, replace; no temp file left on failure


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def store_entry_notes(corpus, episode_id: str, feed_url: str, entry: dict, now: str) -> str:
    """Write one episode's notes file: "written", "unchanged" or "empty"
    (the entry has no note text; nothing is written)."""
    fields = entry_fields(entry)
    if not fields:
        return "empty"
    digest = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
    path = notes_path(corpus, episode_id)
    old = _read_json(path)
    if isinstance(old, dict) and old.get("sha256") == digest:
        return "unchanged"
    guid = (entry.get("id") or entry.get("guid") or "").strip() or None
    _write_json(path, {"version": NOTES_VERSION, "episode_id": episode_id, "feed_url": feed_url,
                       "guid": guid, "sha256": digest, "fields": fields,
                       "first_seen_at": (old.get("first_seen_at") if isinstance(old, dict)
                                         else None) or now, "updated_at": now})
    return "written"


def store_feed_notes(corpus, feed_url: str, entries, known: dict, picked_ids, now: str,
                     episode_id_for) -> dict:
    """Store the notes of a fetched feed's entries that are in the corpus
    (`known`: {"ids": set, "guids": {guid: episode_id}} of this feed, read
    before discovery) or are being enqueued (`picked_ids`). An entry
    matches by episode id, else by guid (same feed). Only files are
    written: no database access."""
    counts = {"written": 0, "unchanged": 0, "empty": 0, "errors": 0}
    ids = set(known.get("ids") or ()) | set(picked_ids or ())
    guids = known.get("guids") or {}
    for entry in entries:
        try:
            ep_id, _ = episode_id_for(feed_url, entry)
        except Exception:  # no usable identity: such entries are never enqueued
            continue
        if ep_id not in ids:
            guid = (entry.get("id") or entry.get("guid") or "").strip()
            ep_id = guids.get(guid) if guid else None
            if ep_id is None:
                continue
        try:
            counts[store_entry_notes(corpus, ep_id, feed_url, entry, now)] += 1
        except Exception:  # one bad entry or a full disk: the rest still go
            counts["errors"] += 1
    return counts


def load_notes(corpus, episode_id: str):
    doc = _read_json(notes_path(corpus, episode_id))
    return doc if isinstance(doc, dict) and doc.get("fields") else None


# ---------------------------------------------------------------- sponsor extraction

def _unwrap(href: str) -> str:
    """A redirect / safe-link's target when its query carries one (at most
    UNWRAP_MAX_LEVELS redirects deep)."""
    for _ in range(UNWRAP_MAX_LEVELS):
        try:
            q = parse_qs(urlsplit(href).query)
        except ValueError:
            return href
        nxt = next((v for k in REDIRECT_KEYS for v in q.get(k, ())
                    if v.startswith(("http://", "https://"))), None)
        if nxt is None:
            return href
        href = nxt
    return href


def _host_label(url: str) -> tuple:
    """(site label, has a subdomain other than www) of a URL or bare domain."""
    url = _unwrap(url.strip())
    try:
        host = urlsplit(url if "//" in url else f"//{url}").hostname or ""
    except ValueError:
        return None, False
    lab = sp.site_label(host)
    parts = host.lower().split(".")
    sub = len(parts) > 2 and parts[0] not in ("www", "go", "try", "get", "join", "shop", "app")
    return lab, sub


def label_forms(label: str) -> set:
    """A domain label and its brand forms: 'drinklmnt' -> {drinklmnt, lmnt};
    'crimejunkiepodcast' -> {crimejunkiepodcast, crimejunkie}."""
    out = {label}
    for p in bn.PREFIXES:
        if label.startswith(p) and len(label) - len(p) >= 3:
            out.add(label[len(p):])
    for s in LABEL_SUFFIXES:
        for f in list(out):
            if f.endswith(s) and len(f) - len(s) >= 3:
                out.add(f[:-len(s)])
    return out


class _Own:
    """The show's own domains and names (self_promo show identity, else
    labels carrying the show title)."""

    def __init__(self, ident: dict | None, show_title: str | None):
        self.ident = ident or {}
        self.title = (self.ident.get("title") or show_title or "").strip()
        self.authors = list(self.ident.get("authors") or ())
        self.labels = sp._own_labels(self.ident) + sp._name_keys(self.ident)
        names = sp.own_names(self.ident) if self.ident else (
            [self.title] if len(sp.name_key(self.title)) >= 4 else [])
        self.name_keys = {sp.name_key(n) for n in names if len(sp.name_key(n)) >= 4}

    def domain(self, label: str) -> bool:
        return sp._is_own_label(label, self.labels) or bool(
            sp._carries_name(label, self.title, self.authors))

    def name(self, name: str) -> bool:
        k = sp.name_key(name)
        return k in self.name_keys or (len(k) >= 4 and self.domain(k))


def _sponsor_label(url: str, own: _Own) -> str | None:
    lab, sub = _host_label(url)
    if not lab or lab in SKIP_LABELS or sp._platform(lab) or lab.endswith(("cdn", "static")):
        return None
    if lab in sp.SITE_BUILDERS and sub:  # a show's own site on a builder
        return None
    return None if own.domain(lab) else lab


def _clean_name(raw: str) -> str | None:
    """A sponsor name from a line head / cue tail, or None."""
    s = _BULLET.sub("", html.unescape(raw or "")).strip()
    s = re.split(r"\s[-–—|]\s|[.!?;(\[]|:\s|,|\s(?:is|are|has|gives|offers|helps|lets)\s",
                 s, maxsplit=1)[0]
    s = s.strip(" \"'“”‘’:-–—*")
    if s.lower().startswith(("the ", "a ")) and len(s.split()) > 1:
        s = s.split(" ", 1)[1]
    words = s.split()
    while words and words[-1].lower().strip(".,") in NAME_SUFFIXES and len(words) > 1:
        words.pop()
    s = " ".join(words)
    if not s or len(words) > 4 or len(bn.compact(s)) < 2 or NAME_STOP.search(s):
        return None
    if FEED_DOMAIN(s):
        return None
    if not (s[0].isupper() or s[0].isdigit()):
        return None
    return s


def FEED_DOMAIN(text: str) -> list:
    return [m.group(1) for m in sp.FEED_DOMAIN_RE.finditer(text or "")]


def _split_names(tail: str) -> list:
    """'A, B and C' -> [A, B, C] (each cleaned; junk dropped)."""
    tail = re.split(r"[.!?;]\s|\s[-–—]\s|\s\(", tail, maxsplit=1)[0]
    parts = re.split(r",\s*|\s+and\s+|\s*\+\s*", tail)
    return [n for n in (_clean_name(p) for p in parts) if n]


def _heading_like(text: str) -> bool:
    words = text.rstrip(":").split()
    return 0 < len(words) <= 6 and (text.endswith(":") or text.isupper()
                                    or not re.search(r"[.!?]", text))


def _line_domains(text: str, links, own: _Own) -> list:
    out = []
    for anchor, href in links:
        lab = _sponsor_label(href, own)
        if lab and lab not in out:
            out.append(lab)
    for d in FEED_DOMAIN(text):
        lab = _sponsor_label(d, own)
        if lab and lab not in out:
            out.append(lab)
    return out


def _vanity(text: str, links) -> bool:
    """A link or bare domain with a short one-segment path (x.com/SHOW)."""
    urls = [h for _, h in links] + [m.group(0) for m in sp.FEED_DOMAIN_RE.finditer(text)]
    for u in urls:
        path = urlsplit(_unwrap(u) if "//" in u else f"//{u}").path.strip("/")
        if path and "/" not in path and len(path) <= 24:
            return True
    return False


def _codes(text: str) -> list:
    return list(dict.fromkeys(m.group(1) for m in CODE_RE.finditer(text)
                              if not m.group(1).lower() in ("for", "at", "to", "and", "the")))


def sponsors(notes: dict | None, ident: dict | None = None, show_title: str | None = None) -> list:
    """Sponsor listings of one episode's notes: [{brand, name, domains,
    codes, evidence, source}] in note order, deduplicated by brand.
    `brand` is the gazetteer-normalized name (else the domain label);
    `source`: block (under a sponsor heading), cue (a sponsor phrase on the
    line), code / offer (a promo code or offer with a sponsor link) or
    cta-link (a call to action with a vanity link). The show's own links
    and names, platforms, ad-choices and hosting lines never count."""
    if not notes:
        return []
    own = _Own(ident, show_title)
    lines = []
    for f in notes.get("fields") or ():
        lines.extend(to_lines(f.get("value") or ""))
        lines.append(("", []))
    out: list = []
    block_left, current, since_name = 0, None, 99

    def add(name, domains, codes, evidence, source):
        if len(out) >= MAX_LISTINGS:
            return None
        if name and own.name(name):
            return None
        if not name and not domains:
            return None
        if not name:
            name = _name_from_line(domains[0], evidence)
        brand = normalize_brand(name) if name else domains[0]
        c = {"brand": brand, "name": name, "domains": list(domains), "codes": list(codes),
             "evidence": evidence[:EVIDENCE_MAX_CHARS], "source": source}
        out.append(c)
        return c

    for text, links in lines:
        text = text.strip()
        if not text and not links:
            continue
        plain = _BULLET.sub("", text).strip()
        if BOILERPLATE.search(text) or any(BOILERPLATE.search(h) for _, h in links):
            continue
        if _SEPARATOR.match(text) and not links:
            block_left, current = 0, None
            continue
        domains = _line_domains(text, links, own)
        codes = _codes(text)
        heading = _heading_like(plain)
        cue = _sponsor_cue(plain, heading)
        if FIRST_PERSON_PROMO.search(plain):
            block_left, current = 0, None
            continue
        if cue:
            tail = plain[cue.end():].strip(" :-–—")
            names = _split_names(tail) if tail else []
            if heading and not names and not domains:
                block_left, current, since_name = BLOCK_MAX_LINES, None, 99
                continue
            if names:
                for n in names:
                    near = [d for d in domains if _fits(n, d)] or (domains if len(names) == 1
                                                                   else [])
                    add(n, near, codes, plain, "cue")
                block_left = BLOCK_MAX_LINES if text.endswith(":") else block_left
                continue
            for d in domains:
                add(None, [d], codes, plain, "cue")
            if not domains and heading:
                block_left, current, since_name = BLOCK_MAX_LINES, None, 99
            continue
        if block_left > 0:
            block_left -= 1
            if heading and SECTION_WORDS.search(plain) and not domains:
                block_left, current = 0, None
                continue
            head = re.match(r"^([^:–—]{1,40}?)(?::\s+|:$|\s[-–—]\s+)(.*)$", plain)
            name = _clean_name(head.group(1)) if head else None
            if head and name and not (head.group(2) or "").strip() and not domains:
                current, since_name = add(name, [], codes, plain, "block"), 0
                continue
            if name and (domains or (head.group(2) or "").strip()):
                current, since_name = add(name, domains, codes, plain, "block"), 0
                continue
            lone = _clean_name(plain) if heading and not domains and not links else None
            if lone and len(plain.split()) <= 4:
                current, since_name = add(lone, [], codes, plain, "block"), 0
                continue
            since_name += 1
            if current is not None and since_name <= 3 and (domains or codes):
                # a link joins the sponsor named above when it fits that name,
                # or when that sponsor has no link yet ('Morgan & Morgan' /
                # forthepeople.com); another sponsor's link is its own listing
                own_links = [d for d in domains if _fits(current["name"] or "", d)
                             or not current["domains"]]
                if own_links or not domains:
                    for d in own_links:
                        if d not in current["domains"]:
                            current["domains"].append(d)
                    current["codes"] = list(dict.fromkeys(current["codes"] + codes))
                    current["evidence"] = (current["evidence"] + " / " + plain)[:EVIDENCE_MAX_CHARS]
                    domains = [d for d in domains if d not in own_links]
                    if not domains:
                        continue
            for d in domains:
                anchor = next((_clean_name(a) for a, h in links
                               if a and _sponsor_label(h, own) == d), None)
                add(anchor, [d], codes, plain, "block")
            if not domains and codes:
                m = CTA_NAME.search(plain)
                name = _clean_name(m.group(1)) if m else None
                if name:
                    add(name, [], codes, plain, "block")
            continue
        if not domains:
            continue
        if FIRST_PERSON.search(plain):  # the host's own product / page
            continue
        if codes or OFFER_CUE.search(plain):
            source = "code" if codes else "offer"
        elif CTA_CUE.search(plain) and _vanity(text, links):
            source = "cta-link"
        else:
            continue
        head = re.match(r"^([^:–—]{1,40}?)(?::\s+|\s[-–—]\s+)", plain)
        name = _clean_name(head.group(1)) if head else None
        if name and any(_fits(name, d) for d in domains):
            add(name, [d for d in domains if _fits(name, d)] or domains, codes, plain, source)
        else:
            for d in domains:
                anchor = next((_clean_name(a) for a, h in links
                               if a and _sponsor_label(h, own) == d), None)
                add(anchor if anchor and _fits(anchor, d) else None, [d], codes, plain, source)
    return _dedupe(out)


def _name_from_line(label: str, text: str) -> str | None:
    """The spelling a line gives a domain-only listing: a run of 1-3 words
    whose compact form is the label or a brand form of it ('LMNT’s' for
    drinklmnt, 'PreBorn' for preborn); else the hyphenated host's words
    ('good-practice-therapy.com' -> 'good practice therapy')."""
    forms = {f for f in label_forms(label) if len(f) >= 3}
    words = re.findall(r"[A-Za-z0-9][\w&'’.-]*", re.sub(r"https?://\S+", " ", text or ""))
    for n in (1, 2, 3):
        for i in range(len(words) - n + 1):
            run = [re.sub(r"(?:['’]s|[.,:;!?])+$", "", w) for w in words[i:i + n]]
            if bn.compact("".join(run)) in forms and (run[0][:1].isupper() or run[0][:1].isdigit()):
                return " ".join(run)
    m = re.search(r"\b((?:[a-z0-9]+-){1,3}[a-z0-9]+)\.(?:com|net|org|co|io|app)\b",
                  (text or "").lower())
    if m and bn.compact(m.group(1)) == label:
        return m.group(1).replace("-", " ")
    return None


def _fits(name: str, label: str) -> bool:
    """The domain label belongs to the name ('Neuro Gum' / neurogum,
    'Chirp' / gochirp); a sound-alike counts."""
    k = bn.compact(name)
    if len(k) < 3:
        return False
    return any(k == f or (len(k) >= 4 and (k in f or f in k)) or brand_canon.similar(k, f)
               for f in label_forms(label))


def _dedupe(cands: list) -> list:
    out, by = [], {}
    for c in cands:
        if not c["brand"]:
            continue
        keys = {bn.compact(c["brand"])} | {f for d in c["domains"] for f in label_forms(d)}
        hit = next((by[k] for k in keys if k in by), None)
        if hit is None:
            out.append(c)
            hit = c
        else:
            if not hit["name"] and c["name"]:
                hit["name"], hit["brand"] = c["name"], c["brand"]
            hit["domains"] = list(dict.fromkeys(hit["domains"] + c["domains"]))
            hit["codes"] = list(dict.fromkeys(hit["codes"] + c["codes"]))
            if c["evidence"] not in hit["evidence"]:
                hit["evidence"] = (hit["evidence"] + " / " + c["evidence"])[:EVIDENCE_MAX_CHARS]
        for k in keys | {bn.compact(hit["brand"])}:
            by.setdefault(k, hit)
    return out


# ---------------------------------------------------------------- matching

def _forms(listing: dict) -> tuple:
    """(name as written, name normalized, domain forms) of a listing, compacted."""
    raw = {bn.compact(listing["name"])} if listing.get("name") else set()
    norm = {bn.compact(listing["brand"] or "")} if listing.get("name") else set()
    doms = {f for d in listing.get("domains") or () for f in label_forms(d)}
    return raw - {""}, norm - {""}, doms


def spoken_domains(text: str) -> set:
    """Domain labels spoken in a read's text ('wayfair dot com')."""
    if not text or not bn._TLD_HINT.search(text):
        return set()
    out = set()
    for m in bn.DOMAIN_RE.finditer(text.lower()):
        for lab in bn.domain_labels(m.group(1)):
            out |= label_forms(lab)
    return out


def _word_prefix(short: str, full: str) -> bool:
    """'mint' / 'Mint Mobile': the read's brand is the listed name's leading
    whole words."""
    sw = re.findall(r"[a-z0-9]+", short.lower())
    fw = re.findall(r"[a-z0-9]+", full.lower())
    return 0 < len(sw) < len(fw) and fw[:len(sw)] == sw and len("".join(sw)) >= 4


def match_read(read: dict, listings: list) -> dict:
    """{status, match, brand, evidence, clears_review, listing} for one named
    read ({brand_spoken, brand_raw, brand_canonical, excerpt}) against an
    episode's listings (None: no notes -> status no_notes)."""
    if listings is None:
        return {"status": "no_notes", "match": None, "brand": None, "evidence": None,
                "clears_review": False, "listing": None}
    current = read.get("brand_canonical") or read.get("brand_spoken")
    if current == "(unnamed)":
        current = None
    # as written (raw, chosen canonical) / normalized (spoken = gazetteer form)
    exact = {bn.compact(b) for b in (read.get("brand_raw"), read.get("brand_canonical")) if b}
    alias = {bn.compact(b) for b in (read.get("brand_spoken"),
                                     normalize_brand(read.get("brand_raw") or "")) if b}
    exact.discard("")
    alias.discard("")
    alias -= {"unnamed"}
    forms = exact | alias
    spoken = spoken_domains(read.get("excerpt") or "")
    # the read says its own brand: a listed domain that is not that brand is
    # likely spill from a neighbouring read, so it cannot correct it
    said = bn.compact(read.get("excerpt") or "")
    named = bn.compact(current or "")
    named = named if len(named) >= 3 and named in said else None
    levels = {"exact": [], "alias": [], "conflict": [], "domain": [], "fuller-name": [],
              "sound-alike": []}
    for li in listings:
        raw, norm, doms = _forms(li)
        if exact & (raw | doms):
            levels["exact"].append(li)
        elif forms & (raw | norm | doms):
            levels["alias"].append(li)
        elif spoken & doms:
            related = not named or any(named in d or d in named or brand_canon.similar(named, d)
                                       for d in spoken & doms)
            levels["domain" if related else "conflict"].append(li)
        elif not any(len(g) >= 4 and g in said for g in raw | norm | doms):
            continue  # a near match needs the read to say the listed name or domain
        elif current and li.get("name") and _word_prefix(current, li["name"]):
            levels["fuller-name"].append(li)
        elif any(brand_canon.similar(f, g) for f in forms if len(f) >= 4
                 for g in raw | norm | doms if len(g) >= 4):
            levels["sound-alike"].append(li)
    for level, hits in levels.items():
        brands = {bn.compact(h["brand"]) for h in hits}
        if not hits:
            continue
        if len(brands) > 1 or level == "conflict":
            if level in ("exact", "alias"):  # both listed: the read's own form wins
                hits = [h for h in hits if forms & (_forms(h)[0] | _forms(h)[1])] or hits[:1]
            else:
                return {"status": "not_listed", "match": "ambiguous", "brand": None,
                        "evidence": None, "clears_review": False, "listing": None}
        li = hits[0]
        if level in ("exact", "alias") and not forms & (_forms(li)[0] | _forms(li)[1]):
            # only a listed domain is the read's brand: confirm, keep its name
            return {"status": "confirmed", "match": level, "brand": current or li["brand"],
                    "brand_key": li["brand"], "written": False, "evidence": li["evidence"],
                    "clears_review": level in CLEARING_MATCHES, "listing": li}
        same =bn.compact(li["brand"]) in forms and (
            not current or bn.compact(li["brand"]) == bn.compact(current))
        return {"status": "confirmed" if same else "corrected", "match": level,
                "brand": li["name"] or li["brand"], "brand_key": li["brand"],
                "written": bool(li.get("name")),
                "evidence": li["evidence"], "clears_review": level in CLEARING_MATCHES,
                "listing": li}
    return {"status": "not_listed", "match": None, "brand": None, "evidence": None,
            "clears_review": False, "listing": None}


# ---------------------------------------------------------------- pass

def _text_digest(cand: dict) -> str:
    return hashlib.sha256((cand.get("excerpt") or "").encode("utf-8")).hexdigest()


def reads_path(corpus, episode_id: str) -> Path:
    return Path(corpus) / READS_DIR / f"{episode_id}.json"


def episode_result(episode_id: str, cands: list, notes, ident, show_title) -> dict:
    """The per-episode pass document for its named candidates."""
    listings = sponsors(notes, ident, show_title) if notes else None
    reads = {}
    for c in cands:
        m = match_read(c, listings)
        reads[c["candidate_id"]] = {
            "status": m["status"], "match": m["match"], "brand": m["brand"],
            "brand_key": m.get("brand_key"), "written": m.get("written", False),
            "evidence": m["evidence"],
            "clears_review": m["clears_review"],
            "brand_in": c.get("brand_spoken"), "canonical_in": c.get("brand_canonical"),
            "start_s": c.get("start_s"), "end_s": c.get("end_s"),
            "text_sha256": _text_digest(c)}
    return {"version": READS_VERSION, "episode_id": episode_id,
            "notes_sha256": (notes or {}).get("sha256"), "sponsors": listings,
            "reads": reads}


def write_result(corpus, doc: dict) -> bool:
    """Write the pass document; False when the stored one is identical."""
    path = reads_path(corpus, doc["episode_id"])
    if _read_json(path) == doc:
        return False
    _write_json(path, doc)
    return True


def apply_to_candidates(corpora, cands: list) -> tuple:
    """Named candidates with the show-notes attrs from the first corpus
    holding a pass result for their episode, plus counts. A candidate whose
    brand, span or text differs from what the pass saw, or whose episode's
    notes changed since (or a result of another version), is left unchanged
    and counted as stale. No pass result anywhere: candidates unchanged."""
    docs, out = {}, []
    counts = {s: 0 for s in STATUSES}
    counts["stale"] = 0
    for c in cands:
        ep = c.get("ep_id")
        if ep not in docs:
            docs[ep] = next(((p, d) for p, d in ((p, _read_json(reads_path(p, ep)))
                                                 for p in corpora) if isinstance(d, dict)),
                            (None, None))
            corpus, d = docs[ep]
            if d is not None:  # the notes the pass saw are still the episode's notes
                d["_current"] = d.get("version") == READS_VERSION and d.get("notes_sha256") == (
                    load_notes(corpus, ep) or {}).get("sha256")
        doc = docs[ep][1] or {}
        r = (doc.get("reads") or {}).get(c.get("candidate_id"))
        if r is None:
            out.append(c)
            continue
        if (not doc["_current"] or r.get("text_sha256") != _text_digest(c)
                or r.get("brand_in") != c.get("brand_spoken")
                or r.get("canonical_in") != c.get("brand_canonical")
                or r.get("start_s") != c.get("start_s") or r.get("end_s") != c.get("end_s")):
            counts["stale"] += 1
            out.append(c)
            continue
        new = dict(c, shownotes_status=r["status"])
        if r["status"] in ("confirmed", "corrected"):
            new.update(shownotes_match=r["match"], shownotes_brand=r["brand"],
                       shownotes_evidence=r["evidence"])
        elif r.get("match"):
            new["shownotes_match"] = r["match"]
        counts[r["status"]] += 1
        out.append(new)
    return out, counts
