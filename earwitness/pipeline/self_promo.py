"""Self-promotion filter for named reads.

A named span that promotes the show itself (its merch, Patreon, newsletter,
courses, book, member portal, agency, other shows by the same host) is
organic self-promotion, not an ad. The evidence is general, never a list of
shows:

- Show identity: the show's own web identity from its RSS feed. Own
  domains come from the channel <link> and episode <link> hosts (linked by
  at least two episodes: a one-off link may be a partner's); any other
  feed domain (description, image host, feed URL) only when it carries the
  show title or the host's full name -- show notes list sponsors, and the
  owner email is often the network's, so neither ever counts on its own.
  Names: show title (and its parts) and itunes:author, minus networks.
  Cached as <cache>/<sha256(feed_url)[:16]>.json; failures are retried
  after RETRY_FAILED_H, an older IDENTITY_VERSION is refetched, and
  `refresh` refetches everything.
- classify(), on the span's own text only: the brand is the show / host
  name, an own domain, or a membership platform the feed links to; the
  span speaks an own domain or calls to action toward an own name; the
  brand contains a full own name / domain AND recurs in >= half of the
  show's episodes (recurrence alone is not enough: sponsors recur too); or
  a first-person self-promo phrase ("our Patreon", "my book", ...). A
  third-party domain spoken in the span (helixsleep.com, even with the
  show's code in its path) vetoes every rule; a sponsor-read cue ("brought
  to you by", "use code", ...) vetoes the name, recurrence and phrase rules.
  Promos for other shows, the network's included, are ads, not self-promo.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from earwitness.pipeline import brand_namer as bn

IDENTITY_VERSION = 3       # 2: strict own-domain sources; 3: episode links need 2 episodes
RETRY_FAILED_H = 24.0      # a failed feed fetch is retried after this long
IDENTITY_DIR = "show-identity"
MAX_FETCHES = 200
RECUR_MIN_SHARE = 0.5     # brand named in >= half of the show's episodes ...
RECUR_MIN_EPISODES = 2    # ... and in at least two of them

# Domains that never identify a show: podcast hosting, CDNs, ad-tech and
# analytics, mail, site builders and link shorteners (plus the namer's INFRA).
HOSTING = {
    "megaphone", "simplecast", "libsyn", "libsynpro", "art19", "omnycontent", "omny",
    "omnystudio", "acast", "acaststudio", "anchor", "buzzsprout", "podbean", "spreaker",
    "podigee", "transistor", "captivate", "redcircle", "blubrry", "podomatic", "audioboom",
    "soundcloud", "sndcdn", "podtrac", "chartable", "chrt", "pdst", "podscribe", "veritone",
    "pod", "podcastchoices", "adchoices", "megaphonefm", "iheart", "iheartradio",
    "iheartpodcasts", "simplecastaudio", "riverside", "cloudfront", "amazonaws", "akamaihd", "akamaized",
    "fastly", "cloudinary", "imgix", "googleusercontent", "gstatic", "feedburner", "feedblitz",
    "feedpress", "pinecast", "fireside", "castos", "zencast", "podcastics", "podcasts", "podnews",
    "jsdelivr", "typepad", "blogspot", "ggpht", "mzstatic", "scdn", "cdn", "rss", "feeds",
    "gmail", "googlemail", "yahoo", "hotmail", "outlook", "live", "icloud", "me", "mac", "aol",
    "proton", "protonmail", "gmx", "web", "msn", "bit", "tinyurl", "lnkd", "ow", "buff",
    "w3", "itunes", "xmlns", "example", "pcm", "appspot", "pippa", "chtbl", "lnk", "prx",
    "prxu", "podcastone", "bleav", "audioboom", "podcastaddict", "pocketcasts", "overcast",
    "castbox", "podchaser", "player", "embed", "storage", "googleapis", "firebaseapp",
}
# Site builders and storefront platforms: never a show's identity (a show's
# site may live on them), but spoken they are a sponsor's call to action.
SITE_BUILDERS = {"squarespace", "sqspcdn", "wix", "wixstatic", "wordpress", "wp", "shopify",
                 "myshopify", "kajabi", "teachable", "thinkific", "godaddy", "weebly", "webflow",
                 "carrd", "mailchimp", "convertkit", "beehiiv", "ghost", "linktree", "linktr"}
# Podcast networks: their promos (other shows, network memberships) are ads,
# so a network never counts as the show's own name or domain.
NETWORKS = {
    "wondery", "crooked", "crookedmedia", "iheartmedia", "npr", "audiochuck", "gimlet",
    "theringer", "ringer", "spotifystudios", "siriusxm", "stitcher", "earwolf", "cumulus",
    "westwoodone", "pushkin", "lemonada", "exactlyright", "barstool", "barstoolsports",
    "dearmedia", "cadence13", "audacy", "headgum", "maximumfun", "maxfun", "relayfm",
    "voxmedia", "nytimes", "wsj", "dowjones", "bbc", "cnn", "parcast", "kastmedia", "studio71",
    "allthingscomedy", "sonymusic", "tedaudiocollective", "nbcnews", "abcnews", "cbsnews",
    "foxnews", "foxnewspodcasts", "puck", "dailywire", "blaze", "theblaze", "pbs", "wnyc",
    "kcrw", "wbur", "slate", "theatlantic", "washingtonpost", "latimes", "bloomberg",
}
# A name with one of these words is an organisation that publishes many
# shows ("Crooked Media", "iHeartPodcasts", "X Podcast Network").
NETWORK_WORDS = {"network", "networks", "media", "podcasts", "studios", "productions",
                 "collective", "entertainment", "radio", "broadcasting", "audio"}
# Membership / support platforms: a show links its own page there.
MEMBERSHIP = {"patreon", "substack", "supercast", "buymeacoffee", "kofi", "memberful",
              "supportingcast", "glow", "locals", "subscribestar", "patron"}
# Platforms a show links to but does not own; spoken, they are no sponsor CTA.
NEUTRAL = {"youtube", "youtu", "instagram", "facebook", "fb", "twitter", "x", "tiktok",
           "linkedin", "threads", "discord", "reddit", "twitch", "amazon", "kindle",
           "bookshop", "barnesandnoble", "eventbrite", "ticketmaster", "linktr", "linktree",
           "wikipedia", "medium", "github", "bsky", "mastodon", "pinterest", "snapchat",
           "whatsapp", "telegram", "goodreads", "spotify", "apple", "google", "gmail"}
JUNK_HANDLES = {"c", "user", "join", "u", "home", "p", "feed", "feeds", "podcast", "api",
                "posts", "s", "login", "signup", "subscribe", "about", "rss"}
# Words that never identify a show or host on their own.
GENERIC = {
    "the", "and", "with", "from", "podcast", "podcasts", "show", "shows", "radio", "daily",
    "weekly", "news", "media", "network", "networks", "productions", "production", "studio",
    "studios", "group", "international", "official", "presents", "episode", "episodes",
    "company", "inc", "llc", "ltd", "gmbh", "co", "true", "crime", "business", "audio",
    "life", "live", "talk", "talks", "hour", "minute", "minutes", "podcasting", "channel",
    "academy", "club", "team", "brand", "global", "world", "health", "money", "time",
    "your", "with", "mit", "und", "der", "die", "das", "les", "des", "family",
}
TLDS = ("com|net|org|io|co|fm|ai|app|tv|me|us|uk|de|at|ch|ca|au|nz|ie|fr|es|it|nl|be|se|"
        "no|dk|br|mx|in|info|biz|show|shop|store|live|link|xyz|club|news|media|studio|online|"
        "site|world|page|ly|gg|so|to|cc|education|academy|church|org|life")
FEED_DOMAIN_RE = re.compile(
    r"(?<![\w@.-])(?:https?://)?((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:" + TLDS
    + r"))(?![\w-])(/[^\s\"'<>()\[\]]*)?", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@((?:[\w-]+\.)+[a-z]{2,})", re.I)
TITLE_SPLIT = re.compile(r"\s*(?:[:|–—(\[\]]|\s-\s|\bw/|\bwith\b|\bmit\b|\bby\b|"
                         r"\bfeat\.?|\bpresented by\b)\s*", re.I)
_LEAD = ("the", "a", "an")
_TRAIL = ("podcast", "podcasts", "show", "pod", "radio")
OWN_TLD = r"\.(?:com|net|org|io|co|fm|tv|ai|app|me|us|uk|de|at|ch|ca|au|shop|store|show)"
PHRASES = [re.compile(p) for p in (
    r"\bour (?:patreon|substack|newsletter|merch(?:andise)?|bonus episodes?|"
    r"members(?:hip)? (?:portal|area|program)|live shows?|tour dates|discord)\b",
    r"\bmy (?:new |latest |first )?(?:patreon|substack|newsletter|books?|novels?|courses?|"
    r"coaching(?: program)?|masterclass|workshop|academy|agency|consulting (?:firm|business)|"
    r"program|programme|merch)\b",
    r"\bour (?:[a-z]+ ){0,2}(?:books?|courses?)\b",
    r"\b(?:on|at|via) (?:patreon|substack|supercast)\b",
    r"\bjoin (?:our|my) (?:community|patreon|membership|discord|newsletter|mailing list|"
    r"wait ?list)\b",
    r"\bbecome a patron\b",
    r"\bsupport (?:the|this|our) (?:show|podcast) (?:on|at|through|by joining)\b",
    r"\blink in the (?:show ?notes|description) to (?:our|my)\b",
    r"\b(?:patreon|substack|supercast)(?:\.| dot )com(?:/| slash )\s*[a-z0-9]",
    # German: the same first-person constructions
    r"\b(?:mein|meine|meinen|meinem|meiner|unser|unsere|unseren|unserem|unserer) "
    r"(?:[a-z\u00e4\u00f6\u00fc\u00df-]+ ){0,3}(?:programm|programme|kurs|kurse|onlinekurs|"
    r"online-kurs|buch|b\u00fccher|coaching|community|newsletter|masterclass|workshop|akademie|"
    r"agentur|bootcamp|boot camp|mitgliederbereich|membership)\b",
    r"\b(?:auf die|zur|der) warteliste\b",
)]


# A sponsor read naming the show ("this episode of X is brought to you by"):
# the show's name or domain in it is no evidence of self-promotion.
SPONSOR_CUE = re.compile(
    r"\b(?:sponsor(?:ed)? (?:by|of)|brought to you by|today'?s sponsors?|our sponsors?|"
    r"(?:presenting|founding|official) sponsor|support for (?:the|this) (?:show|podcast|episode)"
    r" comes from|promo code|use code)\b")
CTA_VERB = re.compile(r"\b(?:(?:go|head|hop|run)(?: over)? to|visit|check out) ")


# ---------------------------------------------------------------- helpers

def brand_key(brand) -> str:
    """Recurrence key: the gazetteer-normalized brand, compacted."""
    from earwitness.gazetteer import normalize_brand
    return bn.compact(normalize_brand(str(brand)) or str(brand)) if brand else ""


def site_label(host: str) -> str | None:
    """'shop.TBRstore.com' -> 'tbrstore'; 'www.bbc.co.uk' -> 'bbc'."""
    parts = [p for p in (host or "").lower().strip(".").split(".") if p]
    if len(parts) < 2:
        return None
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in {"co", "com", "org", "net",
                                                                 "ac", "gov", "edu"}:
        lab = parts[-3]
    else:
        lab = parts[-2]
    lab = re.sub(r"[^a-z0-9]", "", lab)
    return lab if len(lab) >= 3 and not lab.isdigit() else None


def _platform(label: str) -> bool:
    """Spoken, not a sponsor's call to action (nor the show's own): hosting /
    ad-tech, mail, social and retail platforms, membership platforms,
    networks."""
    return (label in HOSTING or label in bn.INFRA or label in NEUTRAL or label in MEMBERSHIP
            or label in NETWORKS)


def _not_own(label: str) -> bool:
    return (_platform(label) or label in SITE_BUILDERS
            or label.endswith(("cdn", "static")) or len(label) < 3)


def name_key(name: str) -> str:
    """Comparison key of a show / host name or brand: compacted, without a
    leading article or a trailing 'podcast' / 'show'."""
    words = re.findall(r"[a-z0-9]+", html.unescape(name or "").lower().replace("'", ""))
    while words and words[0] in _LEAD:
        words = words[1:]
    while words and words[-1] in _TRAIL:
        words = words[:-1]
    return "".join(words)


def _tokens(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
            if len(w) >= 4 and w not in GENERIC}


# ---------------------------------------------------------------- identity

def identity_path(cache_dir, feed_url: str) -> Path:
    return Path(cache_dir) / f"{hashlib.sha256(feed_url.encode()).hexdigest()[:16]}.json"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _raw_channel_tag(raw: bytes, tag: str) -> list:
    head = raw.split(b"<item", 1)[0].decode("utf-8", "replace")
    pat = rf"<{tag}(?:\s[^>]*)?>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</{tag}>"
    return [html.unescape(m).strip() for m in re.findall(pat, head, re.I | re.S) if m.strip()]


def _uniq(names) -> list:
    seen, out = set(), []
    for n in (str(x).strip() for x in names if x and str(x).strip()):
        if n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def is_network(name: str) -> bool:
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    return name_key(name) in NETWORKS or any(w in NETWORK_WORDS for w in words) or any(
        w in NETWORKS for w in words if len(w) >= 4)


def _descriptive(name: str) -> bool:
    """Only generic words ("A True Crime Podcast"): names no one."""
    return all(w in GENERIC or w in _LEAD or w in _TRAIL
               for w in re.findall(r"[a-z0-9]+", (name or "").lower()))


def own_names(ident: dict) -> list:
    """Show title, its parts and itunes:author names that can identify the
    show: not networks, hosting, generic or descriptive. A title part (not
    the whole title) needs two words or 8 characters: "Sleep With Me" names
    the show, its part "Sleep" does not."""
    whole = {(ident.get("title") or "").lower()} | {a.lower() for a in ident.get("authors") or ()}
    out = []
    for n in ident.get("names") or ():
        k = name_key(n)
        if (len(k) < 4 or k in GENERIC or k in bn.INFRA or k in HOSTING or _descriptive(n)
                or is_network(n)):
            continue
        if n.lower() not in whole and len(_words(n)) < 2 and len(k) < 8:
            continue
        out.append(n)
    return out


def _words(name: str) -> list:
    """Content words of a name: no leading article, no trailing 'podcast'."""
    words = re.findall(r"[a-z0-9]+", html.unescape(name or "").lower().replace("'", ""))
    while words and words[0] in _LEAD:
        words = words[1:]
    while words and words[-1] in _TRAIL:
        words = words[:-1]
    return words


def _carries_name(label: str, title: str, authors) -> str | None:
    """The own name a domain label carries: the full key of the show title,
    a title part or an author ('biblerecapjournal' <- 'The Bible Recap'), or
    a multi-word author's surname ('whittemore'). First names and single
    title words never count."""
    ident = {"names": _uniq([title] + TITLE_SPLIT.split(title or "") + list(authors))}
    for n in own_names(ident):
        k = name_key(n)
        if len(k) >= 5 and (k in label or (label in k and len(label) / len(k) >= 0.8)):
            return k
    for a in authors:
        words = re.findall(r"[a-z0-9]+", a.lower())
        if len(words) >= 2 and not is_network(a) and len(words[-1]) >= 4 \
                and words[-1] not in GENERIC and words[-1] in label:
            return words[-1]
    return None


def identity_from_feed(feed_url: str, raw: bytes) -> dict:
    """The show's own domains, names and platform pages from its feed bytes
    (parsed with acquisition's feed parser). `domain_sources` records every
    domain seen and where, own or not."""
    from earwitness.pipeline import acquisition as acq
    parsed = acq.parse_feed(raw, feed_url)
    feed = parsed.get("feed") or {}
    sources: dict = {}
    platforms: dict = {}

    def add_url(url: str, kind: str, path: str = "") -> None:
        parts = urlsplit(url if "//" in url else f"//{url}")
        lab = site_label(parts.hostname or "")
        if not lab:
            return
        sources.setdefault(lab, set()).add(kind)
        if lab in MEMBERSHIP and kind != "owner-email":
            seg = (path or parts.path or "").strip("/").split("/")[0].lstrip("@")
            seg = re.sub(r"[^A-Za-z0-9_.-]", "", seg).strip(".-_").lower()
            platforms.setdefault(lab, set())
            if seg and seg not in JUNK_HANDLES:
                platforms[lab].add(seg)

    def add_text(text: str, kind: str) -> None:
        text = html.unescape(text or "")
        for m in FEED_DOMAIN_RE.finditer(text):
            add_url(m.group(1), kind, (m.group(2) or "").rstrip(".,;:!?"))

    add_url(feed_url, "feed-url")
    add_url(feed.get("link") or "", "channel-link")
    for link in feed.get("links") or ():
        if link.get("rel") != "self":
            add_url(link.get("href") or "", "other-link")
    for detail in (feed.get("publisher_detail"), feed.get("author_detail")):
        if detail and detail.get("email"):
            add_url(detail["email"].rsplit("@", 1)[-1], "owner-email")
    add_url((feed.get("image") or {}).get("href") or "", "image")
    for img in _raw_channel_tag(raw, "url"):
        add_url(img, "image")
    for text in (feed.get("subtitle"), feed.get("summary"), feed.get("rights"),
                 *_raw_channel_tag(raw, "description"), *_raw_channel_tag(raw, "itunes:summary")):
        add_text(text or "", "description")
    entries = parsed.get("entries") or ()
    ep_links: dict = {}
    for e in entries:
        lab = site_label(urlsplit(e.get("link") or "").hostname or "")
        if lab:
            ep_links[lab] = ep_links.get(lab, 0) + 1
            add_url(e.get("link"), "episode-link")
    title = html.unescape(feed.get("title") or "").strip()
    authors = _uniq(_raw_channel_tag(raw, "itunes:author") + _raw_channel_tag(raw, "author")
                    + _raw_channel_tag(raw, "dc:creator"))
    owners = _uniq(_raw_channel_tag(raw, "itunes:name")
                   + [(feed.get("publisher_detail") or {}).get("name")])
    domains = set()
    for lab, kinds in sources.items():
        if _not_own(lab):
            continue
        if "channel-link" in kinds or ep_links.get(lab, 0) >= min(2, len(entries)) > 0 or (
                kinds - {"owner-email"} and _carries_name(lab, title, authors)):
            domains.add(lab)
    return {"version": IDENTITY_VERSION, "status": "ok", "feed_url": feed_url,
            "fetched_at": _utcnow().isoformat(timespec="seconds"),
            "title": title, "names": _uniq([title] + TITLE_SPLIT.split(title) + authors),
            "authors": authors, "owners": owners, "domains": sorted(domains),
            "domain_sources": {k: sorted(v) for k, v in sorted(sources.items())},
            "episode_link_counts": dict(sorted(ep_links.items())),
            "platforms": {k: sorted(v) for k, v in sorted(platforms.items())}}


def _write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _read_doc(path: Path):
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _age_h(doc: dict, now: datetime) -> float:
    try:
        then = datetime.fromisoformat(str(doc.get("fetched_at")))
    except ValueError:
        return float("inf")
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (now - then).total_seconds() / 3600


def build_identities(feed_urls, cache_dir, fetch_feed, *, max_fetches: int = MAX_FETCHES,
                     refresh: bool = False, retry_failed_h: float = RETRY_FAILED_H) -> dict:
    """Fetch (at most `max_fetches`) and cache the identity of each feed that
    is uncached, cached by an older IDENTITY_VERSION, a failure older than
    `retry_failed_h`, or any cached feed when `refresh`. A failure is cached
    as {"status": "error"}."""
    urls = list(dict.fromkeys(u for u in feed_urls if u))
    rep = {"feeds": len(urls), "fetched": 0, "ok": 0, "failed": 0, "cached": 0,
           "skipped_cap": 0, "refreshed": 0, "stale_version": 0, "retried_failed": 0}
    now = _utcnow()
    for url in urls:
        path = identity_path(cache_dir, url)
        doc = _read_doc(path)
        why = None
        if doc is None:
            why = "new"
        elif refresh:
            why = "refreshed"
        elif doc.get("version") != IDENTITY_VERSION:
            why = "stale_version"
        elif doc.get("status") != "ok" and _age_h(doc, now) >= retry_failed_h:
            why = "retried_failed"
        if why is None:
            rep["cached"] += 1
            continue
        if rep["fetched"] >= max_fetches:
            rep["skipped_cap"] += 1
            continue
        if why != "new":
            rep[why] += 1
        rep["fetched"] += 1
        try:
            doc = identity_from_feed(url, fetch_feed(url))
            rep["ok"] += 1
        except Exception as exc:  # recorded per show, never fatal
            doc = {"version": IDENTITY_VERSION, "status": "error", "feed_url": url,
                   "fetched_at": _utcnow().isoformat(timespec="seconds"),
                   "error": f"{type(exc).__name__}: {exc}"[:300]}
            rep["failed"] += 1
        _write_json(path, doc)
    return rep


def load_identity(cache_dir, feed_url) -> dict | None:
    """The cached identity of a show, or None (not fetched, failed, or an
    older identity version)."""
    if not feed_url:
        return None
    doc = _read_doc(identity_path(cache_dir, feed_url))
    ok = doc is not None and doc.get("status") == "ok" and doc.get("version") == IDENTITY_VERSION
    return doc if ok else None


# ---------------------------------------------------------------- classify

def _handles(ident: dict) -> list:
    return [h for hs in (ident.get("platforms") or {}).values() for h in hs
            if h not in JUNK_HANDLES]


def _own_labels(ident: dict) -> list:
    # re-filtered here so list fixes apply to identities cached earlier
    return [d for d in ident.get("domains") or () if not _not_own(d)]


def _name_keys(ident: dict, min_len: int = 4) -> list:
    return [k for k in (name_key(n) for n in own_names(ident) + _handles(ident))
            if len(k) >= min_len]


def _same_label(a: str, b: str) -> bool:
    """Equal labels, up to a leading 'the' ('biblerecap' / 'thebiblerecap')."""
    return bool(a) and (a == b or a == "the" + b or b == "the" + a)


def _is_own_label(lab: str, own: list) -> bool:
    return any(_same_label(lab, o) for o in own)


def _capture_labels(capture: str) -> list:
    """Compact labels of a spoken domain capture: its last 1, 2 and 3
    content words ('the bible recap' -> recap, biblerecap, thebiblerecap)."""
    words = [w.strip(".,!?'\"").lower() for w in capture.split()]
    words = [re.sub(r"[^a-z0-9]", "", w[:-2] if w.endswith("'s") else w) for w in words]
    out = []
    for n in (1, 2, 3):
        tail = words[-n:]
        if len(tail) == n and not any(w in bn.CUT_WORDS - {"the"} for w in tail) and all(tail):
            out.append("".join(tail))
    return out


# bn.DOMAIN_RE anchored at the start of a word and with each word capped (a
# domain label is at most 63 characters): on a long run of word characters
# the unbounded, unanchored form is quadratic, each start scanning the whole
# run. The same matches on normal text; they can differ on rare glued-domain
# text ('box.com-alwa.io': a domain glued straight onto the previous one is
# skipped or shortened here). No ad-rules change on the demo.
_DOMAIN_RE = re.compile(
    r"(?<![a-z0-9'&-])((?:[a-z0-9'&-]{1,63}\s+){0,2}[a-z0-9'&-]{1,63})"
    r"(?:\.|\s+dot\s+)" + bn.TLD + r"\b", re.I)


def third_party_domains(text: str, ident: dict | None) -> list:
    """Domains spoken in `text` that are neither the show's own, nor a
    platform / network, nor infrastructure: a sponsor's call to action."""
    if not text or not bn._TLD_HINT.search(text):
        return []
    ident = ident or {}
    own = _own_labels(ident) + _name_keys(ident)
    out = []
    for m in _DOMAIN_RE.finditer(text):
        labs = bn.domain_labels(m.group(1))
        if labs and not any(_is_own_label(lab, own) or _platform(lab) for lab in labs):
            out.append(labs[-1])
    return out


def _cta_to_own(text: str, labels) -> str | None:
    """'head to marketing leap' -> 'marketingleap': a call to action whose
    target (its next 1-4 words, exactly) is an own name or domain label."""
    labels = [lab for lab in labels if len(lab) >= 6]
    for m in CTA_VERB.finditer(text):
        nxt = [bn.compact(w) for w in text[m.end():m.end() + 80].split()[:4]]
        for n in range(1, len(nxt) + 1):
            hit = next((lab for lab in labels if _same_label("".join(nxt[:n]), lab)), None)
            if hit:
                return hit
    return None


def _spoken_own_domain(text: str, labels) -> str | None:
    """An own label spoken as a whole domain ('thebiblerecap.com', 'the
    bible recap dot com'), never inside a longer one."""
    t = re.sub(r"\s+dot\s+", " dot ", (text or "").lower())
    for m in re.finditer(r"(?<![a-z0-9'&-])((?:[a-z0-9'&-]{1,63}\s+){0,2}[a-z0-9'&-]{1,63})"
                         r"(?:\.| dot )"
                         r"(?:com|net|org|io|co|fm|tv|ai|app|me|us|uk|de|at|ch|ca|au|shop|store|"
                         r"show)\b", t):
        for cap in _capture_labels(m.group(1)):
            hit = next((lab for lab in labels if len(lab) >= 4 and _same_label(cap, lab)), None)
            if hit:
                return hit
    return None


def _name_match(bkey: str, ident: dict) -> str | None:
    if len(bkey) < 4:
        return None
    for n in own_names(ident) + _handles(ident):
        if name_key(n) == bkey:
            return n.lower()
    return None


def _domain_match(bkey: str, own: list) -> str | None:
    return next((o for o in own if _same_label(bkey, o)), None)


def _runs(words: list) -> set:
    """Compacts of every contiguous run of `words`."""
    return {"".join(words[i:j]) for i in range(len(words)) for j in range(i + 1, len(words) + 1)}


def _contains_own(brand: str, ident: dict) -> str | None:
    """A full own name or own domain as whole words of the brand ('Bible
    Recap Kids' <- 'The Bible Recap'), never a word inside a word
    ('Sleep Number' is not 'Sleep...')."""
    runs = _runs(_words(brand))
    for n in own_names(ident) + _handles(ident):
        k = "".join(_words(n))
        if len(k) >= 5 and k in runs:
            return f"own-name:{k}"
    for o in _own_labels(ident):
        if len(o) >= 5 and any(_same_label(r, o) for r in runs):
            return f"own-domain:{o}"
    return None


def _platform_self_ad(bkey: str, text: str, ident: dict) -> bool:
    """The span advertises a membership platform itself: it names the
    platform and speaks its domain with no path to the show's own page."""
    if bkey not in MEMBERSHIP:
        return False
    own = [h for h in _handles(ident) + _name_keys(ident, 5) + _own_labels(ident) if len(h) >= 4]
    spoken = False
    for m in re.finditer(rf"\b{bkey}(?:\.| dot )com\b((?:/| slash )\s*((?:[a-z0-9-]+ ?){{1,4}}))?",
                         text):
        spoken = True
        path = bn.compact(m.group(2) or "")
        if path and any(path.startswith(h) for h in own):
            return False
    return spoken


def _third_party_brand(bkey: str, ident: dict, known_brands) -> bool:
    """The brand is a known brand (brand list / gazetteer) that is neither
    the show's own nor a platform."""
    if not bkey or not known_brands or bkey not in known_brands or _platform(bkey):
        return False
    return not _is_own_label(bkey, _own_labels(ident) + _name_keys(ident))


def classify(brand, text: str, ident: dict | None, recur: dict | None = None,
             known_brands=None) -> tuple:
    """(self_promo, reason) for one named span from the span's own text:
    `brand` as named, `text` the span's transcript text, `ident` the show
    identity (None / {} when the feed is unknown: only phrases apply),
    `recur` {"episodes": n, "count": k} = the brand is named in k of the
    show's n named episodes, `known_brands` compact brand keys (the brand
    list): a known third-party brand vetoes the phrase rules."""
    ident = ident or {}
    text_l = re.sub(r"\s+", " ", (text or "").lower())
    if third_party_domains(text_l, ident):
        return False, None
    bkey = name_key(brand) if brand else ""
    if _platform_self_ad(bkey, text_l, ident):
        return False, None
    own = _own_labels(ident)
    sponsor_read = bool(SPONSOR_CUE.search(text_l))
    if bkey and not sponsor_read:
        hit = _name_match(bkey, ident)
        if hit:
            return True, f"own-name:{hit}"
        hit = _domain_match(bkey, own)
        if hit:
            return True, f"own-domain:{hit}"
        if bkey in (ident.get("platforms") or {}):
            return True, f"own-platform:{bkey}"
    labels = own + _name_keys(ident, 6)
    hit = _spoken_own_domain(text_l, labels) or _cta_to_own(text_l, labels)
    if hit:
        return True, f"own-cta:{hit}"
    if sponsor_read:
        return False, None
    if bkey and recur and recur.get("count", 0) >= RECUR_MIN_EPISODES \
            and recur["count"] >= RECUR_MIN_SHARE * max(1, recur.get("episodes", 0)):
        hit = _contains_own(str(brand), ident)
        if hit:
            return True, f"recurring+{hit}"
    if _third_party_brand(bkey, ident, known_brands):
        return False, None
    for pat in PHRASES:
        m = pat.search(text_l)
        if m:
            return True, f"phrase:{m.group(0)}"
    return False, None


# ---------------------------------------------------------------- corpus

def show_recurrence(items) -> dict:
    """{feed: {"episodes": n, "brands": {brand_key: episodes naming it}}}
    from (feed, episode_id, brand or None) triples."""
    eps: dict = {}
    for feed, ep, brand in items:
        eps.setdefault(feed, {}).setdefault(ep, set())
        if brand:
            eps[feed][ep].add(brand_key(brand))
    out = {}
    for feed, by_ep in eps.items():
        counts: dict = {}
        for keys in by_ep.values():
            for k in keys:
                counts[k] = counts.get(k, 0) + 1
        out[feed] = {"episodes": len(by_ep), "brands": counts}
    return out


def candidate_brand(c: dict):
    """The brand a named candidate was named as (None when unnamed)."""
    b = c.get("brand_raw") or c.get("brand_spoken")
    if b:
        return b
    return c.get("brand") if c.get("brand") not in (None, "(unnamed)") else None


def known_brand_keys(brand_list=()) -> frozenset:
    """Compact keys of a namer brand list's brands plus the gazetteer."""
    from earwitness.gazetteer import GAZETTEER
    keys = {bn.compact(e.get("brand") or "") for e in brand_list or () if isinstance(e, dict)}
    keys |= {bn.compact(k) for k in GAZETTEER}
    return frozenset(k for k in keys if len(k) >= 3)


def mark(cands, *, texts, identities, recurrence: dict, feeds, known_brands=None) -> list:
    """Copies of named candidates with the display attrs self_promo /
    self_promo_reason. texts (each span's own text) / identities / feeds
    are parallel to `cands`; known_brands: known_brand_keys()."""
    out = []
    for c, text, ident, feed in zip(cands, texts, identities, feeds):
        brand = candidate_brand(c)
        rec = (recurrence or {}).get(feed)
        recur = ({"episodes": rec["episodes"], "count": rec["brands"].get(brand_key(brand), 0)}
                 if rec and brand else None)
        ok, why = classify(brand, text, ident, recur, known_brands)
        out.append(dict(c, self_promo=ok, self_promo_reason=why))
    return out


def span_text(segments, start_s: float, end_s: float) -> str:
    return " ".join(str(s.get("text", "")).strip() for s in segments or ()
                    if s.get("start", 0) < end_s and s.get("end", 0) > start_s)



# ---------------------------------------------------------------- operator rules

def apply_rules(cands, network_of, reason: str = "network") -> list:
    """Named candidates with the operator's self-promo rules applied, as the
    app applies them on load: a read not already self-promo for which
    `network_of(candidate)` (corrections.Corrections.self_promo_network,
    where the matching rule lives) gives a network becomes self_promo,
    self_promo_reason "<reason>:<network>", needs_review false. Other
    candidates are returned as they are. Idempotent."""
    out = []
    for c in cands:
        hit = None if c.get("self_promo") else network_of(c)
        out.append(dict(c, self_promo=True, self_promo_reason=f"{reason}:{hit}",
                        needs_review=False) if hit else c)
    return out
