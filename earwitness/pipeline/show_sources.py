"""Show lists for the corpus runner, built from public podcast charts.

Charts (public JSON, no key, checked 2026-10-10):
  apple-top               Apple Podcasts top shows, legacy iTunes RSS chart
                          (itunes.apple.com/<cc>/rss/toppodcasts, max 200)
  apple-genre:<name>      the same chart for each of Apple's 19 top-level genres
  spotify-top             Spotify Podcast Charts top podcasts
                          (podcastcharts.byspotify.com/api/charts, max 200)
  spotify-category:<id>   Spotify's 17 category charts (max 50 each)

Apple shows get their RSS feed from the iTunes lookup API. Spotify shows
carry no feed: each is matched to an Apple show by title and publisher,
first among the Apple chart shows, and only when none matches with the
iTunes search API (whose results hold clones and re-uploads). A match is
"high" (same title and publisher) or "medium" (the only candidate with
that exact title). A medium match whose publisher clearly differs (Spotify
often lists the hosts, Apple the network) is kept with
`publisher_mismatch: true`, unless another candidate with a related title
has the Spotify publisher. Anything else is dropped with its reason. Shows
without a public feed (Apple subscriber-only shows, Spotify exclusives)
are dropped, not guessed.

Not covered: Podtrac (only a top 20 is public, as an HTML page with
misspelled titles; the full rankings are paid), YouTube, private feeds.

Shows are merged by Apple id and by normalised feed URL; a show on several
charts keeps every chart tag with its rank. The file records the fetch
time, the arguments, per-chart counts, source errors and dropped shows.
Given the same fetched data it is byte-identical.
"""
from __future__ import annotations

import difflib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlparse

from earwitness.pipeline import acquisition as acq
from earwitness.pipeline import corpus as cr

APPLE_GENRES = {  # top-level podcast genres, iTunes genre ids
    "Arts": 1301, "Business": 1321, "Comedy": 1303, "Education": 1304, "Fiction": 1483,
    "Government": 1511, "Health & Fitness": 1512, "History": 1487, "Kids & Family": 1305,
    "Leisure": 1502, "Music": 1310, "News": 1489, "Religion & Spirituality": 1314,
    "Science": 1533, "Society & Culture": 1324, "Sports": 1545, "Technology": 1318,
    "True Crime": 1488, "TV & Film": 1309}
SPOTIFY_CATEGORIES = ("arts", "business", "comedy", "education", "fiction", "health-fitness",
                      "history", "leisure", "music", "news", "religion-spirituality", "science",
                      "society-culture", "sports", "technology", "true-crime", "tv-film")
APPLE_CHART_MAX = 200
SPOTIFY_TOP_MAX = 200
SPOTIFY_CATEGORY_MAX = 50
LOOKUP_BATCH = 150
SEARCH_LIMIT = 15
JSON_MAX_BYTES = 16 * 1024 * 1024
HOST_INTERVAL_S = {"itunes.apple.com": 3.0}  # Apple asks for about 20 calls a minute
DEFAULT_INTERVAL_S = 1.0
RETRIES = 3        # tries after the first, on network errors and 408 / 5xx
BACKOFF_S = 2.0    # doubled per retry
RETRIES_429 = 6    # a throttle (429) gets more, longer tries: it passes, unlike an outage
BACKOFF_429_S = 15.0
RETRY_AFTER_MAX_S = 300.0
HOST_INTERVAL_MAX_S = 30.0  # each 429 doubles the host's interval up to this
DECAY_AFTER_OK = 10  # ... and every 10 successes in a row halve it, back to the base
TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}


def _err(exc) -> str:
    return f"{type(exc).__name__}: {exc}"[:300]


def _status(exc):
    m = re.match(r"HTTP (\d+)", str(exc))
    return int(m.group(1)) if m else None


def _transient(exc) -> bool:
    if isinstance(exc, OSError):  # requests' ConnectionError / Timeout too
        return True
    return _status(exc) in TRANSIENT_STATUS


def _retry_after_s(exc):
    value = str(getattr(exc, "retry_after", None) or "").strip()
    return float(value) if value.isdigit() else None  # an HTTP date falls back to backoff


class Polite:
    """GET with the corpus User-Agent, a minimum interval per host and
    retries with exponential backoff. A 429 honours Retry-After, backs off
    longer (RETRIES_429 tries from BACKOFF_429_S) and doubles that host's
    interval, so a throttle slows the run instead of changing its result;
    every DECAY_AFTER_OK successes in a row halve the interval again, down
    to the host's base. `get(url) -> bytes` raises on error."""

    def __init__(self, get=None, *, sleep=time.sleep, clock=time.monotonic,
                 retries: int = RETRIES):
        self._get = get or (lambda url: cr.http_get_capped(url, max_bytes=JSON_MAX_BYTES))
        self._sleep, self._clock, self._retries = sleep, clock, retries
        self._next, self._interval, self._lock = {}, dict(HOST_INTERVAL_S), threading.Lock()
        self._ok = {}  # host -> successes in a row since its last 429

    def _wait(self, host: str) -> None:
        with self._lock:
            now = self._clock()
            at = max(now, self._next.get(host, now))
            self._next[host] = at + self._interval.get(host, DEFAULT_INTERVAL_S)
        if at > now:
            self._sleep(at - now)

    def get(self, url: str) -> bytes:
        host = (urlparse(url).hostname or "").lower()
        tries = throttles = 0
        while True:
            self._wait(host)
            try:
                body = self._get(url)
                self._succeeded(host)
                return body
            except Exception as exc:
                if _status(exc) == 429:
                    throttles += 1
                    if throttles > RETRIES_429:
                        raise
                    with self._lock:
                        self._ok[host] = 0
                        self._interval[host] = min(HOST_INTERVAL_MAX_S, 2 * self._interval.get(
                            host, DEFAULT_INTERVAL_S))
                    wait = max(BACKOFF_429_S * 2 ** (throttles - 1), _retry_after_s(exc) or 0)
                    self._sleep(min(wait, RETRY_AFTER_MAX_S))
                    continue
                tries += 1
                if tries > self._retries or not _transient(exc):
                    raise
                self._sleep(BACKOFF_S * 2 ** (tries - 1))

    def _succeeded(self, host: str) -> None:
        base = HOST_INTERVAL_S.get(host, DEFAULT_INTERVAL_S)
        with self._lock:
            if self._interval.get(host, base) <= base:
                return
            self._ok[host] = self._ok.get(host, 0) + 1
            if self._ok[host] >= DECAY_AFTER_OK:
                self._ok[host] = 0
                self._interval[host] = max(base, self._interval[host] / 2)

    def json(self, url: str):
        return json.loads(self.get(url))


# ---------------------------------------------------------------- fetchers

def _itunes_show(r: dict) -> dict:
    return {"apple_id": int(r["collectionId"]), "title": r.get("collectionName"),
            "artist": r.get("artistName"), "feed_url": r.get("feedUrl") or None,
            "category": r.get("primaryGenreName")}


def apple_chart(http, country: str, limit: int, genre_id: int | None = None) -> list:
    genre = f"/genre={genre_id}" if genre_id else ""
    doc = http.json(f"https://itunes.apple.com/{country}/rss/toppodcasts/limit={limit}"
                    f"{genre}/json")
    entries = doc.get("feed", {}).get("entry") or []
    if isinstance(entries, dict):  # a one-show chart
        entries = [entries]
    return [{"apple_id": int(e["id"]["attributes"]["im:id"]), "title": e["im:name"]["label"],
             "artist": (e.get("im:artist") or {}).get("label")} for e in entries][:limit]


def apple_lookup(http, country: str, ids: list) -> tuple[dict, dict]:
    """({apple_id: show}, {apple_id: error}) for every id, LOOKUP_BATCH per call."""
    found, failed = {}, {}
    for i in range(0, len(ids), LOOKUP_BATCH):
        batch = ids[i:i + LOOKUP_BATCH]
        try:
            doc = http.json(f"https://itunes.apple.com/lookup?id={','.join(map(str, batch))}"
                            f"&country={country}&entity=podcast")
            for r in doc.get("results", []):
                if r.get("collectionId"):
                    found[int(r["collectionId"])] = _itunes_show(r)
        except Exception as exc:
            failed.update({aid: _err(exc) for aid in batch})
    return found, failed


def itunes_search(http, country: str, term: str) -> list:
    doc = http.json(f"https://itunes.apple.com/search?media=podcast&entity=podcast"
                    f"&country={country}&limit={SEARCH_LIMIT}&term={quote(term)}")
    return [_itunes_show(r) for r in doc.get("results", []) if r.get("collectionId")]


def spotify_chart(http, country: str, category: str, limit: int) -> list:
    doc = http.json(f"https://podcastcharts.byspotify.com/api/charts/{category}"
                    f"?region={country}&limit={limit}")
    return [{"title": r.get("showName"), "publisher": r.get("showPublisher"),
             "spotify_uri": r.get("showUri")} for r in doc][:limit]


# ---------------------------------------------------------------- matching

norm_name = cr.norm_name


_GENERIC = {"media", "podcast", "podcasts", "network", "networks", "studio", "studios",
            "production", "productions", "radio", "show", "official", "entertainment",
            "audio", "group", "company", "news", "with", "and", "the", "llc", "inc"}
_NO_PUBLISHER = {"", "unknown", "n a", "none"}


def _names_show(publisher: str, title: str) -> bool:
    """The publisher is the show's own name, a name in its title, or the
    initials of its leading title words (DOAC = Diary Of A CEO)."""
    if len(publisher) >= 3 and (re.search(rf"\b{re.escape(publisher)}\b", title)
                                or title in publisher):
        return True
    words = title.split()
    initials = {"".join(w[0] for w in words[:n]) for n in range(3, len(words) + 1)}
    return " " not in publisher and publisher in initials


def _publisher_compatible(publisher, artist, title) -> bool:
    """False only for clearly different publishers: both known, neither
    naming the show, no shared distinctive word."""
    a, b, t = norm_name(publisher), norm_name(artist), norm_name(title)
    if a in _NO_PUBLISHER or b in _NO_PUBLISHER:
        return True
    if _same_publisher(a, b) or _names_show(a, t) or _names_show(b, t):
        return True
    words = lambda s: {w for w in s.split() if len(w) >= 4 and w not in _GENERIC}  # noqa: E731
    return bool(words(a) & words(b))


def _same_publisher(a, b) -> bool:
    a, b = norm_name(a), norm_name(b)
    if not (a and b):
        return False
    short, long_ = sorted((a, b), key=len)
    return (len(short) >= 4 and short in long_) or a == b \
        or difflib.SequenceMatcher(None, a, b).ratio() >= 0.6


def _related_title(key: str, title) -> bool:
    t = norm_name(title)
    return bool(t) and (key in t or t in key)


def publisher_mismatch(publisher, candidate, title) -> bool:
    """A match whose publishers clearly differ (Spotify often lists the
    hosts, Apple the network): kept, flagged on the show list."""
    return not _publisher_compatible(publisher, candidate.get("artist"), title)


def match_show(title, publisher, candidates: list):
    """(candidate, "high" | "medium") or (None, "ambiguous" | "publisher" |
    "none"). "medium" is the only exact-title candidate. Its publisher may
    differ (see publisher_mismatch) unless another candidate with a related
    title has the Spotify publisher: then the exact title is likely a
    re-upload and the show is dropped ("publisher")."""
    key = norm_name(title)
    if not key:
        return None, "none"
    same = sorted({c["apple_id"]: c for c in candidates
                   if norm_name(c["title"]) == key}.values(), key=lambda c: c["apple_id"])
    both = [c for c in same if _same_publisher(publisher, c.get("artist"))]
    if len(both) == 1:
        return both[0], "high"
    if len(same) == 1:
        if publisher_mismatch(publisher, same[0], title) and any(
                c["apple_id"] != same[0]["apple_id"] and _related_title(key, c["title"])
                and _same_publisher(publisher, c.get("artist")) for c in candidates):
            return None, "publisher"
        return same[0], "medium"
    return None, "ambiguous" if same else "none"


feed_key = cr.feed_key


def resolve_names(value: str, known) -> tuple:
    """'all', '' or a comma list of genre / category names, matched loosely
    ('tv-film' = 'TV & Film'). Raises ValueError on an unknown name."""
    def key(s):
        return re.sub(r"\band\b", " ", norm_name(s)).split()
    if value.strip().lower() == "all":
        return tuple(known)
    out = []
    for name in (v.strip() for v in value.split(",") if v.strip()):
        hit = [k for k in known if key(k) == key(name)]
        if not hit:
            raise ValueError(f"unknown name {name!r}; use one of {', '.join(known)} or 'all'")
        out.append(hit[0])
    return tuple(dict.fromkeys(out))


# ---------------------------------------------------------------- build

DROP_REASONS = {"none": "no apple match", "ambiguous": "ambiguous apple match",
                "publisher": "apple title match with another publisher, while a"
                             " related show has this publisher"}


def build_show_list(http, *, country: str = "us", apple_top: int = 0, apple_genres=(),
                    apple_genre_top: int = 0, spotify_top: int = 0, spotify_categories=(),
                    spotify_category_top: int = 0, args=None, now: str | None = None) -> dict:
    """Fetch the charts, resolve every show to its feed, merge. One failing
    chart or lookup batch is reported and never stops the others."""
    apple = ([("apple-top", None, apple_top)] if apple_top else []) + [
        (f"apple-genre:{g}", APPLE_GENRES[g], apple_genre_top)
        for g in (apple_genres if apple_genre_top else ())]
    spotify = ([("spotify-top", "top-podcasts", spotify_top)] if spotify_top else []) + [
        (f"spotify-category:{c}", c, spotify_category_top)
        for c in (spotify_categories if spotify_category_top else ())]
    order = {name: i for i, (name, _, _) in enumerate(apple + spotify)}
    counts, errors, dropped = {}, [], []

    def fetch_chart(name, fn):
        try:
            rows = fn()
        except Exception as exc:
            errors.append({"chart": name, "error": _err(exc)})
            return []
        counts[name] = len(rows)
        return rows

    tags, titles = {}, {}  # apple_id -> [tag], apple_id -> chart title
    for name, genre, limit in apple:
        for rank, r in enumerate(fetch_chart(
                name, lambda: apple_chart(http, country, limit, genre)), 1):
            tags.setdefault(r["apple_id"], []).append({"chart": name, "rank": rank})
            titles.setdefault(r["apple_id"], r["title"])
    info, failed = apple_lookup(http, country, sorted(tags))
    pool = [info[i] for i in sorted(info)]
    searched = {}
    for name, cat, limit in spotify:
        for rank, s in enumerate(fetch_chart(
                name, lambda: spotify_chart(http, country, cat, limit)), 1):
            what = {"title": s["title"], "publisher": s["publisher"], "chart": name,
                    "rank": rank}
            cand, conf = match_show(s["title"], s["publisher"], pool)
            if cand is None:  # a chart show with that exact title wins over search hits
                key = norm_name(s["title"])
                if key not in searched:
                    try:
                        searched[key] = itunes_search(http, country, s["title"])
                    except Exception as exc:
                        searched[key] = exc
                if isinstance(searched[key], Exception):
                    dropped.append(dict(what, reason=f"search failed: {_err(searched[key])}"))
                    continue
                cand, conf = match_show(s["title"], s["publisher"], pool + searched[key])
            if cand is None:
                dropped.append(dict(what, reason=DROP_REASONS[conf]))
                continue
            info.setdefault(cand["apple_id"], cand)
            titles.setdefault(cand["apple_id"], cand["title"])
            tag = {"chart": name, "rank": rank, "match": conf, "spotify_uri": s["spotify_uri"]}
            if conf == "medium" and publisher_mismatch(s["publisher"], cand, s["title"]):
                tag["publisher_mismatch"] = True
            tags.setdefault(cand["apple_id"], []).append(tag)

    shows = {}
    for aid in sorted(tags):
        what = {"title": titles[aid], "apple_id": aid,
                "charts": [f"{t['chart']}#{t['rank']}" for t in tags[aid]]}
        meta = info.get(aid)
        if aid in failed and not meta:
            dropped.append(dict(what, reason=f"lookup failed: {failed[aid]}"))
        elif not meta:
            dropped.append(dict(what, reason="not in iTunes lookup"))
        elif not meta.get("feed_url"):
            dropped.append(dict(what, reason="no public feed"))
        elif feed_key(meta["feed_url"]) in shows:
            show = shows[feed_key(meta["feed_url"])]
            show["sources"] += tags[aid]
            show.setdefault("also_apple_ids", []).append(aid)
        else:
            shows[feed_key(meta["feed_url"])] = {
                "title": meta["title"] or titles[aid], "feed_url": meta["feed_url"],
                "category": meta["category"], "apple_id": aid, "sources": list(tags[aid])}
    for show in shows.values():
        show["sources"].sort(key=lambda t: (order[t["chart"]], t["rank"]))
        show["source"] = ",".join(f"{t['chart']}#{t['rank']}" for t in show["sources"])
    ranked = sorted(shows.values(), key=lambda s: (min(t["rank"] for t in s["sources"]),
                                                   order[s["sources"][0]["chart"]],
                                                   s["apple_id"]))
    return {"built_at": now or cr.utcnow(), "country": country, "args": args,
            "selection": ", ".join(f"{n} top {lim}" for n, _, lim in apple + spotify),
            "charts": counts, "errors": errors, "shows": ranked,
            "dropped": sorted(dropped, key=lambda d: (d["reason"], d["title"] or "",
                                                      d.get("apple_id") or 0,
                                                      d.get("chart", ""), d.get("rank", 0)))}


def chart_ranks(source) -> dict:
    """{chart: best rank} from a show's chart tags ("apple-top#5,spotify-top#9",
    the `source` of a show list entry and of a corpus show row)."""
    out = {}
    for tag in (source or "").split(","):
        chart, _, rank = tag.strip().rpartition("#")
        if chart and rank.isdigit():
            out[chart] = min(out.get(chart, int(rank)), int(rank))
    return out


def within_top(source, *, apple_top: int = 0, apple_genre_top: int = 0, spotify_top: int = 0,
               spotify_category_top: int = 0) -> bool:
    """Ranked within any given limit (0: chart not used): Apple overall,
    any Apple genre, Spotify overall, any Spotify category. The "shows that
    matter" for ads, as arguments: e.g. apple_top=200, apple_genre_top=25,
    spotify_top=200."""
    for chart, rank in chart_ranks(source).items():
        limit = (apple_top if chart == "apple-top" else
                 apple_genre_top if chart.startswith("apple-genre:") else
                 spotify_top if chart == "spotify-top" else
                 spotify_category_top if chart.startswith("spotify-category:") else 0)
        if limit and rank <= limit:
            return True
    return False


def verify_shows(doc: dict, http, *, workers: int = 8, now: str | None = None,
                 log=None, languages=None) -> dict:
    """Load every feed (through `http`, a Polite over the capped feed
    fetcher) and keep only shows whose feed parses and has audio episodes;
    the rest move to "dropped" with the reason. Each kept show records its
    feed's RSS <language> as "language" (None when absent). `languages`
    (Whisper codes, e.g. {"en"}): a show whose language is another one is
    dropped ("language: de"); one with no clear language is kept.
    `log(str)` gets a progress line every 250 feeds."""
    done, lock = [0], threading.Lock()

    def check(show):
        try:
            return _check(show)
        finally:
            with lock:
                done[0] += 1
                if log and done[0] % 250 == 0:
                    log(f"verified {done[0]}/{len(doc['shows'])} feeds")

    def _check(show):
        try:
            body = http.get(show["feed_url"])
            size = len(body)
            entries, show["language"] = cr.parse_feed_bounded(body, show["feed_url"])
            del body  # raw bytes freed before the entries are walked
            dates = [acq.entry_datetime(e) for e in entries if acq.entry_audio_url(e)]
            del entries
            if not dates:
                return show, None, "no audio episodes"
            if languages is not None and cr.language_allowed(show["language"],
                                                             languages) is False:
                return show, None, f"language {show['language']}"
            days = [d.date() for d in dates if d is not None]
            return show, {"audio_episodes": len(dates), "feed_bytes": size,
                          "newest": max(days).isoformat() if days else None}, None
        except Exception as exc:
            return show, None, _err(exc)

    with ThreadPoolExecutor(max(1, workers)) as pool:
        results = list(pool.map(check, doc["shows"]))
    doc["shows"] = []
    for show, verified, error in results:
        if error is None:
            show["verified"] = verified
            doc["shows"].append(show)
        else:
            doc["dropped"].append({"title": show["title"], "apple_id": show["apple_id"],
                                   "charts": show["source"].split(","),
                                   "feed_url": show["feed_url"],
                                   "reason": (f"language: {error.split(' ', 1)[1]}"
                                              if error.startswith("language ")
                                              else f"feed: {error}")})
    doc["dropped"].sort(key=lambda d: (d["reason"], d["title"] or "", d.get("apple_id") or 0,
                                       d.get("chart", ""), d.get("rank", 0)))
    doc["verified_at"] = now or cr.utcnow()
    return doc
