"""P01 acquisition: feed/episode identity and bounded atomic media fetch.

Implements the acquisition half of full-product plan section 6 on top of
the frozen requested-graph identities (plan section 3):

- Episode identity is feed GUID first; without a GUID the feed identity
  plus enclosure locator is an explicitly provisional key. No query
  parameters are removed by guesswork: distinct locators stay distinct
  until evidence or an owner mapping establishes identity.
- Feed conditional GET saves metadata work only: a feed 304 is never
  proof that media bytes are unchanged (DAI resampling).
- Downloads are bounded (byte cap, timeout), streamed to a `.part` file
  with incremental hashing, and atomically published. Truncation is a
  failure, never a complete result.
- Same URL + different bytes yields a distinct fetch/media snapshot: a
  published file is never overwritten with different bytes (refused, or
  content-addressed as `<sha256>.<ext>`).
- A media body must look like audio (type, magic bytes, and a decode probe
  when no Content-Length proves completeness); error pages and empty
  bodies are failures, never `fetched`.
- Media is never fetched conditionally: a media 304 is an error.
- Truncated JSON documents raise instead of being skipped as complete.
- A redirect that lands on an unresolvable or invalid host raises
  `BadRedirectHost`; `embedded_media_url` extracts the innermost allowlisted
  media URL from a tracking-redirect locator for one caller-side retry.

Parsing conventions (entry datetime, enclosure selection) are reused from
`scripts/ingest.py` via import, not forked.
"""
from __future__ import annotations

import errno
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import socket
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse, urlsplit
from xml.sax import SAXParseException

REPO = Path(__file__).resolve().parent.parent.parent
_INGEST_PATH = REPO / "scripts" / "ingest.py"

_ingest_mod = None


def _ingest():
    """Load scripts/ingest.py read-only for its entry conventions."""
    global _ingest_mod
    if _ingest_mod is None:
        spec = importlib.util.spec_from_file_location("ew_ingest_ref", _INGEST_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _ingest_mod = mod
    return _ingest_mod


class AcquisitionError(Exception):
    """Base error for acquisition failures."""


class UnsafeURL(AcquisitionError):
    """The locator failed validation (scheme, host, or shape)."""


class OverBudget(AcquisitionError):
    """The download exceeded its byte cap."""


class TruncatedDownload(AcquisitionError):
    """The transfer ended before the declared complete body arrived."""


class TruncatedDocument(AcquisitionError):
    """A JSON document could not be fully parsed; never complete."""


class NotAFeed(AcquisitionError):
    """The document has no rss/feed/rdf root (empty body, HTML error page)."""


class NotAudio(AcquisitionError):
    """The body is empty, wrongly typed, or does not look like audio."""


class MediaNotModified(AcquisitionError):
    """A media request returned 304; media is never fetched conditionally."""


class SnapshotConflict(AcquisitionError):
    """The final name already holds different bytes; never overwritten."""


class DeadlineExceeded(AcquisitionError):
    """The whole download outlived its overall deadline."""


class BadRedirectHost(AcquisitionError):
    """A redirect led to a host that cannot be resolved or is not a valid DNS name."""

    def __init__(self, message, *, url=None, host=None, reason=None):
        super().__init__(message)
        self.url = url
        self.host = host
        self.reason = reason


ALLOWED_SCHEMES = {"http", "https"}

# Known podcast measurement/tracking redirect prefixes.
TRACKING_DOMAINS = ("mgln.ai", "pscrb.fm", "chtbl.com", "podtrac.com", "op3.dev",
                    "chrt.fm", "pdst.fm", "arttrk.com", "claritaspod.com",
                    "podscribe.com")

# Explicit allowlist of podcast media CDNs an embedded URL may point at.
MEDIA_HOST_DOMAINS = ("megaphone.fm", "libsyn.com", "simplecastaudio.com",
                      "simplecast.com", "buzzsprout.com", "podbean.com",
                      "blubrry.com", "omny.fm", "omnycontent.com", "acast.com",
                      "art19.com", "spreaker.com", "captivate.fm", "transistor.fm",
                      "audioboom.com", "redcircle.com", "anchor.fm",
                      "podcasters.spotify.com", "pinecast.com", "fireside.fm",
                      "castos.com", "npr.org", "iheart.com", "sndcdn.com",
                      "soundcloud.com")

AUDIO_PATH_SUFFIXES = (".mp3", ".m4a", ".aac", ".ogg", ".wav")

_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_CLEAN_PORT = re.compile(r":[0-9]+")
_ASCII_NETLOC = re.compile(r"[a-z0-9.-]+(:[0-9]{1,5})?")
_SCHEME_PREFIX = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def _domain_match(host: str, domains) -> bool:
    """True iff `host` is one of `domains` or a subdomain of one."""
    return any(host == d or host.endswith("." + d) for d in domains)


def _is_ip_literal(text: str) -> bool:
    try:
        ipaddress.ip_address(text.strip("[]"))
    except ValueError:
        return False
    return True


def _clean_netloc(parts) -> bool:
    """True iff the netloc is just the host, optionally followed by `:<digits>`,
    in ASCII host characters only (letters, digits, `.`, `-`).

    No userinfo, backslash, non-ASCII or other text: then urlparse's idea of the host
    cannot differ from what a client will connect to."""
    try:
        host = parts.hostname
    except ValueError:
        return False
    if not host:
        return False
    netloc = parts.netloc.lower()
    if _ASCII_NETLOC.fullmatch(netloc) is None:
        return False
    return netloc == host or (netloc.startswith(host)
                              and _CLEAN_PORT.fullmatch(netloc[len(host):]) is not None)


def _is_valid_dns_name(host: str) -> bool:
    if len(host) > 253:
        return False
    labels = host.split(".")
    if len(labels) < 2 or not all(_DNS_LABEL.fullmatch(label) for label in labels):
        return False
    last = labels[-1]
    return len(last) >= 2 and last.isalpha()


def _is_media_host(host: str) -> bool:
    return (_is_valid_dns_name(host) and not _is_ip_literal(host)
            and _domain_match(host, MEDIA_HOST_DOMAINS)
            and not _domain_match(host, TRACKING_DOMAINS))


def _is_host_like(seg: str) -> bool:
    seg = seg.lower()
    if _is_ip_literal(seg):
        return True
    if "." not in seg:
        return False
    if "@" in seg or ":" in seg:
        return True
    last = seg.rsplit(".", 1)[1]
    return len(last) >= 2 and last.isalpha() and "." + last not in AUDIO_PATH_SUFFIXES


def embedded_media_url(url: str) -> str | None:
    """Innermost media URL embedded in a tracking-redirect locator, or None.

    The outer host must be a tracking domain (`TRACKING_DOMAINS`, subdomains
    included). The path is scanned from the end for the first host-like
    segment; that innermost host decides and scanning never continues outward
    past a host that is not acceptable. A media host is a lowercased,
    syntactically valid DNS name with at least one dot (labels 1-63 chars of
    [a-z0-9-], not starting or ending with '-', last label alphabetic and at
    least 2 chars), not an IP literal, with no port and no userinfo, that
    matches `MEDIA_HOST_DOMAINS` and is never a tracking domain. Only http and
    https are accepted (default https); any other scheme marker in the path
    refuses the URL. The remainder must end in an audio suffix; the query is
    kept and the fragment dropped. The input URL is never returned.
    """
    try:
        parts = urlsplit(url)
        outer = (parts.hostname or "").lower()
    except (ValueError, AttributeError, TypeError):
        return None
    if parts.scheme.lower() not in ALLOWED_SCHEMES or not _domain_match(outer, TRACKING_DOMAINS):
        return None
    if not _clean_netloc(parts):
        return None
    segs = parts.path.split("/")
    for seg in segs:
        m = _SCHEME_PREFIX.match(seg)
        if m and m.group(0)[:-1].lower() not in ALLOWED_SCHEMES:
            return None
    idx = next((i for i in range(len(segs) - 2, -1, -1) if _is_host_like(segs[i])), None)
    if idx is None:
        return None
    host = segs[idx].lower()
    if not _is_media_host(host):
        return None
    before = [s.lower() for s in segs[max(idx - 2, 0):idx]]
    if before[-1:] == [""]:
        before = before[:-1]  # `scheme://host`: the empty segment is the second slash
    scheme = "http" if before[-1:] == ["http:"] else "https"
    rest = "/".join(segs[idx + 1:])
    if not rest.lower().endswith(AUDIO_PATH_SUFFIXES):
        return None
    result = f"{scheme}://{host}/{rest}"
    if parts.query:
        result += "?" + parts.query
    try:
        return validate_enclosure_url(result)
    except UnsafeURL:
        return None


def validate_enclosure_url(url: str) -> str:
    """Accept an http(s) enclosure locator exactly as given.

    No query parameters are stripped or normalized: `?x=1` and `?x=2`
    are different locators until identity evidence says otherwise.
    """
    if not isinstance(url, str) or not url:
        raise UnsafeURL("empty enclosure locator")
    parts = urlparse(url)
    if parts.scheme not in ALLOWED_SCHEMES or not parts.netloc:
        raise UnsafeURL(f"enclosure must be an absolute http(s) URL: {url[:80]}")
    return url


def _enclosure_href(entry: dict) -> str:
    """Enclosure selection, verbatim mirror of `scripts/ingest.py::audio_url`.

    The primary path reuses ingest via import (`entry_audio_url`);
    this mirror is used when that import is unavailable (e.g. an
    interpreter without the pinned feedparser/requests install) and when
    ingest finds nothing. The convention is the same: audio-typed
    enclosures win, then known audio suffixes.
    """
    # Suffix is read from the URL path, case-insensitively, so `ep.mp3?x=1`
    # and `EP.MP3` count; `ingest.audio_url` matches on the raw string.
    for link in entry.get("enclosures", []) or []:
        href = link.get("href", "")
        typ = link.get("type", "")
        path = urlparse(href).path.lower()
        if href and ("audio" in typ or path.endswith((".mp3", ".m4a", ".aac", ".ogg", ".wav"))):
            return href
    for link in entry.get("links", []) or []:
        href = link.get("href", "")
        if href and urlparse(href).path.lower().endswith((".mp3", ".m4a", ".aac")):
            return href
    return ""


def episode_identity_key(feed_id: str, entry: dict,
                         ordinal: int | None = None) -> dict:
    """Stable identity basis for a feed entry (plan section 3).

    Returns `{"basis": "guid", "key": ...}` when the entry carries a
    GUID, else an explicitly provisional
    `{"basis": "provisional", "key": feed_id + enclosure}` record.
    A GUID reused with conflicting evidence is an identity conflict for
    the caller to record, not a silent overwrite here.

    The enclosure locator is preferred. An entry with neither GUID nor
    enclosure locator may only fall back to a site-link href together
    with the caller's entry `ordinal` (feed identity + href + ordinal),
    so distinct entries never collapse onto one key. Without an ordinal
    the fallback is refused rather than guessed.
    """
    guid = (entry.get("id") or entry.get("guid") or "").strip()
    if guid:
        return {"basis": "guid", "key": f"{feed_id}\x00{guid}"}
    enclosure = entry_audio_url(entry) or ""
    if enclosure:
        return {"basis": "provisional", "key": f"{feed_id}\x00{enclosure}",
                "provisional": True}
    link_href = ""
    for link in entry.get("links", []) or []:
        if link.get("href"):
            link_href = link["href"]
            break
    if not link_href:
        raise AcquisitionError("entry has neither GUID nor enclosure locator")
    if ordinal is None:
        raise AcquisitionError("entry ordinal required for link-based "
                               "provisional key; refusing to guess identity")
    return {"basis": "provisional",
            "key": f"{feed_id}\x00{link_href}\x00ordinal:{ordinal}",
            "provisional": True}


def entry_datetime(entry: dict):
    """Reuse ingest.py's publication-instant parsing (no forked convention)."""
    try:
        return _ingest().ep_datetime(entry)
    except Exception:
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime
        for key in ("published_parsed", "updated_parsed"):
            if entry.get(key):
                try:
                    return datetime(*entry[key][:6], tzinfo=timezone.utc)
                except Exception:
                    pass
        if entry.get("published"):
            try:
                dt = parsedate_to_datetime(entry["published"])
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except Exception:
                pass
        return None


def entry_audio_url(entry: dict):
    """Reuse ingest.py's enclosure-selection convention."""
    try:
        found = _ingest().audio_url(entry)
    except Exception:
        found = None
    return found or _enclosure_href(entry) or None


_TRUNCATION_MESSAGES = re.compile(r"unclosed|no element found|premature", re.I)
_ROOT_OPEN = re.compile(rb"<(?:[\w.-]+:)?(?:rss|feed|rdf)[\s>:/]", re.I)
_ROOT_CLOSE = re.compile(rb"</(?:[\w.-]+:)?(?:rss|feed|rdf)\s*>", re.I)


def parse_feed_entries(feed_bytes: bytes, feed_url: str = "") -> list:
    """Parse feed bytes into entry dicts using the ingest convention.

    Raises TruncatedDocument when the bytes cannot be parsed at all.
    Feedparser is lenient by design; entries without any usable identity
    locator are reported, not silently dropped, via the `identity_error`
    marker the caller must handle.
    """
    return list(parse_feed(feed_bytes, feed_url).get("entries", []))


def parse_feed(feed_bytes: bytes, feed_url: str = ""):
    """The whole feedparser result (channel fields under "feed"), with the
    same root and truncation checks as parse_feed_entries."""
    try:
        import feedparser
    except ImportError as exc:
        raise AcquisitionError(f"feedparser is required for feed parsing: {exc}")
    raw = feed_bytes if isinstance(feed_bytes, bytes) else str(feed_bytes).encode()
    # Dropping NULs lets the byte regexes see roots in UTF-16 documents.
    probe = raw.replace(b"\x00", b"")
    if not _ROOT_OPEN.search(probe):
        raise NotAFeed(f"document has no rss/feed/rdf root ({feed_url}): {raw[:60]!r}")
    try:
        parsed = feedparser.parse(feed_bytes)
    except Exception as exc:
        raise TruncatedDocument(f"feed could not be parsed ({feed_url}): {exc}")
    if parsed.get("bozo") and not parsed.get("entries"):
        raise TruncatedDocument(f"feed parse failed with no entries ({feed_url}): "
                                f"{parsed.get('bozo_exception')}")
    # Real feeds are often malformed but complete (bare "&", "&nbsp;", a stray
    # <br>); only truncation signatures fail: expat ran out of input, or the
    # document never closes its root element. Encoding overrides are fine.
    exc = parsed.get("bozo_exception")
    if isinstance(exc, SAXParseException):
        if (_TRUNCATION_MESSAGES.search(exc.getMessage())
                or not _ROOT_CLOSE.search(probe)):
            raise TruncatedDocument(f"feed looks truncated ({feed_url}): {exc}")
    return parsed


def conditional_headers(prior: dict | None) -> dict:
    """Build conditional-GET validators from a prior fetch observation."""
    prior = prior or {}
    headers = {}
    if prior.get("etag"):
        headers["If-None-Match"] = prior["etag"]
    if prior.get("last_modified"):
        headers["If-Modified-Since"] = prior["last_modified"]
    return headers


def is_feed_not_modified(status_code: int) -> bool:
    """A feed 304 saves metadata work -- and nothing else.

    Explicitly: `feed_304_implies_media_unchanged()` is False. Schedules
    must distinguish discovering new episodes from explicitly requested
    DAI resampling, which always refetches the enclosure bytes.
    """
    return status_code == 304


def feed_304_implies_media_unchanged() -> bool:
    """Always False: a feed 304 is not proof that media is unchanged."""
    return False


def snapshot_id(enclosure_url: str, sha256: str, fetch_started_at: str = "") -> str:
    """Content snapshot identity: same URL + different bytes = distinct."""
    return hashlib.sha256(
        f"{enclosure_url}\x00{sha256}\x00{fetch_started_at}".encode()).hexdigest()[:32]


def is_new_snapshot(prior_sha256: str | None, new_sha256: str) -> bool:
    """DAI resampling creates a new snapshot whenever the bytes differ."""
    if not prior_sha256:
        return True
    return prior_sha256 != new_sha256


_NON_AUDIO_TYPES = {"application/json", "application/xml", "application/xhtml+xml"}
# Headers kept on the observation: they show whether a CDN/DAI layer served
# or cached the bytes. Only those present on the response are recorded.
_OBSERVED_HEADERS = ("content-type", "content-length", "date", "age",
                     "cache-control", "expires", "via", "x-cache",
                     "cf-cache-status", "x-served-by", "cdn-cache-control",
                     "surrogate-control", "server")


def _header(response, name: str):
    """Case-insensitive header lookup that also works on plain dicts."""
    for key, value in (getattr(response, "headers", None) or {}).items():
        if key.lower() == name:
            return value
    return None


def looks_like_audio(head: bytes) -> bool:
    """Magic-byte sniff: ID3, MPEG/ADTS frame sync, MP4, Ogg, WAV, FLAC."""
    if head[:3] == b"ID3" or head[4:8] == b"ftyp" or head[:4] in (b"OggS", b"fLaC"):
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return True
    # 0xFF + (byte & 0xE0 == 0xE0) covers MPEG audio frames and ADTS AAC (0xFFF).
    return len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0


_MAX_DECODE_ERRORS = 1000


# MPEG audio header tables: bitrates (kbps) by version family and layer, sample rates.
_MPEG_BITRATES = {
    (1, 1): (32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (1, 2): (32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (1, 3): (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (2, 1): (32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (2, 2): (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    (2, 3): (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_MPEG_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def _mpeg_frame_length(head: bytes):
    """Byte length of the MPEG audio frame at `head`, or None if not a valid header."""
    if len(head) < 4 or head[0] != 0xFF or head[1] & 0xE0 != 0xE0:
        return None
    version, layer = (head[1] >> 3) & 3, 4 - ((head[1] >> 1) & 3)
    bitrate_i, rate_i, pad = head[2] >> 4, (head[2] >> 2) & 3, (head[2] >> 1) & 1
    if version == 1 or layer == 4 or bitrate_i in (0, 15) or rate_i == 3:
        return None
    family = 1 if version == 3 else 2
    bitrate = _MPEG_BITRATES[(family, layer)][bitrate_i - 1] * 1000
    rate = _MPEG_RATES[version][rate_i]
    if layer == 1:
        return (12 * bitrate // rate + pad) * 4
    return (144 if layer == 2 or family == 1 else 72) * bitrate // rate + pad


def _skip_leading_tags(handle) -> int:
    """Offset past consecutive ID3v2 tags and leading APE tags."""
    pos = 0
    while True:
        handle.seek(pos)
        head = handle.read(32)
        if head[:3] == b"ID3" and len(head) >= 10:
            # Syncsafe size: 7 bits per byte (cover art makes tags hundreds of KB).
            size = (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]
            pos += 10 + size + (10 if head[5] & 0x10 else 0)
        elif head[:8] == b"APETAGEX" and len(head) == 32:
            pos += 32 + int.from_bytes(head[12:16], "little")  # size counts items + footer
        else:
            return pos


def _has_vbr_header(path) -> bool:
    """True when the first valid MPEG frame carries a Xing/Info/VBRI duration header."""
    with open(path, "rb") as handle:
        start = _skip_leading_tags(handle)
        handle.seek(start)
        region = handle.read(1 << 16)
    for i in range(len(region) - 3):
        length = _mpeg_frame_length(region[i:i + 4])
        if not length:
            continue
        nxt = i + length
        if nxt + 4 <= len(region) and not _mpeg_frame_length(region[nxt:nxt + 4]):
            continue  # a second frame must follow; image bytes can fake one header
        return any(tag in region[i:i + 64] for tag in (b"Xing", b"Info", b"VBRI"))
    return False


def probe_audio_duration(path: str | os.PathLike) -> float:
    """Decode the whole file with PyAV and return the decoded seconds.

    Isolated corrupt frames are skipped and counted, not fatal. Raises when
    the file will not open, has no audio, decodes to nothing, or decodes to
    well under the container's declared duration (a truncated body that
    carries a duration header, e.g. Xing/moov). An mp3/ADTS file without a
    Xing/Info/VBRI header has only a size-based estimate, which is not a
    truncation reference (VBR and trailing padding break it), so a cleanly
    cut one passes; callers see that as length_verified false.
    """
    try:
        import av
    except ImportError as exc:
        raise AcquisitionError(f"PyAV is required for the decode probe: {exc}")
    try:
        with av.open(str(path)) as container:
            stream = next((s for s in container.streams if s.type == "audio"), None)
            if stream is None:
                raise NotAudio(f"no audio stream in {path}")
            decoded = 0.0
            errors = 0
            packets = container.demux(stream)
            while True:
                try:
                    packet = next(packets)
                    for frame in packet.decode():
                        decoded += frame.samples / frame.sample_rate
                except StopIteration:
                    break
                except av.error.FFmpegError:
                    errors += 1
                    if errors > _MAX_DECODE_ERRORS:
                        raise
            declared = (container.duration or 0) / av.time_base
            if container.format.name in ("mp3", "aac") and not _has_vbr_header(path):
                declared = 0  # bitrate-from-size estimate: no truncation reference
    except AcquisitionError:
        raise
    except Exception as exc:
        raise TruncatedDownload(f"audio failed to decode ({path}): {exc}")
    if decoded <= 0:
        raise TruncatedDownload(f"audio decoded to zero duration: {path}")
    if declared > 0 and decoded < 0.95 * declared:
        raise TruncatedDownload(f"decoded {decoded:.1f}s of declared {declared:.1f}s "
                                f"({errors} bad frames): {path}")
    return decoded


class _StreamingResponse:
    """`requests` response adapted so each read yields what has arrived.

    `iter_content(n)` blocks until n bytes arrive; a slow-drip server would
    outrun the per-read timeout yet never let the deadline check run.
    """

    def __init__(self, response):
        self._response = response
        self.status_code = response.status_code
        self.headers = response.headers
        self.url = response.url
        self.history = response.history

    def iter_content(self, chunk_size=1 << 20):
        raw = self._response.raw
        while True:
            chunk = raw.read1(chunk_size, decode_content=True)
            if not chunk:
                return
            yield chunk

    def close(self):
        self._response.close()


def _is_name_resolution_error(exc) -> bool:
    """True if `exc` or anything in its cause/context/args/reason chain is a DNS failure."""
    todo, seen = [exc], set()
    while todo and len(seen) < 20:
        node = todo.pop()
        if not isinstance(node, BaseException) or id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, socket.gaierror) or type(node).__name__ == "NameResolutionError":
            return True
        todo.extend([node.__cause__, node.__context__, getattr(node, "reason", None),
                     *node.args])
    return False


def redirect_host_failure(url, exc):
    """Classify a transport error raised for a redirect hop host.

    Returns `(reason, host, failing_url)` when the failing request was not
    to the original host and its host is invalid (`"invalid"`), or fails DNS
    with a single label (`"single-label"`) or a dotted name
    (`"unresolvable"`); otherwise None. None too when the original URL's
    netloc is not a plain host[:port].
    """
    failing_url = getattr(getattr(exc, "request", None), "url", None)
    if not failing_url:
        return None
    try:
        fhost = (urlparse(failing_url).hostname or "").lower()
        original = urlparse(url)
        ohost = (original.hostname or "").lower()
    except ValueError:
        return None
    if not _clean_netloc(original):
        return None  # urlparse's origin host cannot be trusted
    if fhost == ohost:
        return None
    if not fhost:
        reason = "invalid"
    elif not _is_name_resolution_error(exc):
        return None
    elif "." not in fhost:
        reason = "single-label"
    else:
        reason = "unresolvable"
    return reason, fhost, failing_url


def requests_fetcher(session=None):
    """Production transport for `download_media`, built on `requests`.

    Asks for `Accept-Encoding: identity` so Content-Length matches the bytes
    streamed, follows redirects, and exposes `.url` and `.history` so the
    observation can record the final URL and redirect chain.
    """
    def fetch(url, headers, timeout_s):
        import requests
        merged = dict(headers)
        merged["Accept-Encoding"] = "identity"
        try:
            response = (session or requests).get(url, headers=merged, stream=True,
                                                 timeout=timeout_s, allow_redirects=True)
        except Exception as exc:
            failure = redirect_host_failure(url, exc)
            if failure:
                reason, host, failing_url = failure
                raise BadRedirectHost(f"redirect to {reason} host {host!r}: {exc}",
                                      url=failing_url, host=host, reason=reason) from exc
            raise
        return _StreamingResponse(response)
    return fetch


def _fsync_dir(path: Path) -> None:
    """Make a rename durable where the platform allows opening a directory."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


_LOCK_STALE_S = 30.0  # a publish takes milliseconds; older lock/empty file = killed writer


def _unlink_quiet(path: Path) -> None:
    """Best-effort cleanup of a legacy leftover; never fails the download."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _rename_noreplace(src, dst):
    """Rename without replacing `dst`: True on success, FileExistsError if
    `dst` exists, None when the platform/filesystem has no such primitive.
    """
    import ctypes
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if sys.platform == "darwin":
            rc = libc.renamex_np(os.fsencode(src), os.fsencode(dst), 0x4)  # RENAME_EXCL
        elif sys.platform.startswith("linux"):
            rc = libc.renameat2(-100, os.fsencode(src), -100, os.fsencode(dst), 1)  # NOREPLACE
        else:
            return None
    except (OSError, AttributeError):
        return None
    if rc == 0:
        return True
    err = ctypes.get_errno()
    if err == errno.EEXIST:
        raise FileExistsError(errno.EEXIST, os.strerror(err), str(dst))
    if err in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP):
        return None
    raise OSError(err, os.strerror(err), str(dst))


def _publish_with_lock(part: Path, final: Path, sha: str) -> None:
    """Last resort: serialize publishers on `<final>.lock`; `final` is never empty."""
    lock = final.with_name(final.name + ".lock")
    give_up = time.monotonic() + 2 * _LOCK_STALE_S
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
            break
        except FileExistsError:
            if time.monotonic() > give_up:
                raise AcquisitionError(f"timed out waiting for publish lock: {lock.name}")
            try:
                age = abs(time.time() - lock.stat().st_mtime)  # abs: clock skew
            except FileNotFoundError:
                continue
            if age > _LOCK_STALE_S:
                _unlink_quiet(lock)  # left by a killed publisher
            else:
                time.sleep(0.02)
    try:
        if final.exists():
            _verify_existing(part, final, sha)
        else:
            os.replace(part, final)
    finally:
        _unlink_quiet(lock)


def _verify_existing(part: Path, final: Path, sha: str) -> None:
    """`final` exists: identical bytes are idempotent, different bytes conflict."""
    st = final.stat()
    if st.st_size == 0 and abs(time.time() - st.st_mtime) > _LOCK_STALE_S:
        os.replace(part, final)  # empty leftover of a killed publisher
        return
    if _sha256_file(final) != sha:
        raise SnapshotConflict(f"{final.name} already holds different bytes")


def _publish_no_clobber(part: Path, final: Path, sha: str) -> None:
    """Publish `part` as `final` without ever replacing different bytes."""
    try:
        os.link(part, final)  # atomic no-clobber publish
        return
    except FileExistsError:
        pass
    except OSError:
        # No hard links here (FAT, some network mounts).
        try:
            if _rename_noreplace(part, final):
                return
            return _publish_with_lock(part, final, sha)
        except FileExistsError:
            pass
    _verify_existing(part, final, sha)


def sweep_stale_parts(dest_dir: str | os.PathLike, older_than_s: float) -> list:
    """Remove `*.part` files untouched for `older_than_s` (crash leftovers)."""
    cutoff = time.time() - older_than_s
    removed = []
    for path in sorted(Path(dest_dir).glob("*.part")):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path.name)
        except OSError:
            pass  # vanished or unremovable: skip, keep sweeping
    return removed


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def download_media(url: str, *, dest_dir: str | os.PathLike, name: str,
                   fetcher, max_bytes: int, timeout_s: float = 60.0,
                   validators: dict | None = None,
                   chunk_size: int = 1 << 20, deadline_s: float = 1800.0,
                   content_addressed: bool = False, verify_fn=None) -> dict:
    """Bounded download to a unique `.part` with hashing and atomic publication.

    `fetcher(url, headers, timeout_s)` is an injectable transport returning
    a response with `.status_code`, `.headers` and `.iter_content(n)` (and
    optionally `.url`, `.history`, `.close()`); production passes
    `requests_fetcher()`, tests pass fixtures -- never real feeds in tests.
    On success the fsynced `.part` file is atomically published. Byte-cap
    overruns, the overall `deadline_s`, declared-length truncation, empty or
    non-audio bodies and transport errors raise and leave no final file.
    `verify_fn(path)` may raise to reject the finished part file; by default
    a PyAV decode probe runs when no Content-Length proves completeness.
    The final name is never overwritten with different bytes: without
    `content_addressed` a differing file raises SnapshotConflict, with it
    the file is named `<sha256><suffix>`. `validators` is accepted for
    compatibility but never sent: media is always refetched (DAI), and a
    media 304 is an error. Returns the fetch observation (distinct per
    request, even for one URL).
    """
    validate_enclosure_url(url)
    if max_bytes <= 0:
        raise AcquisitionError("bounded download requires a positive byte cap")
    started = time.time()
    clock = time.monotonic()
    started_at = _utcnow()
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Older runs shared one fixed-name part file; stale ones are still cleaned.
    legacy_part = dest_dir / (name + ".part")
    headers = {"User-Agent": "EarWitnessAcquisition/1.0 (research project)"}
    try:
        response = fetcher(url, headers, timeout_s)
    except AcquisitionError:
        _unlink_quiet(legacy_part)
        raise
    except Exception as exc:
        _unlink_quiet(legacy_part)
        raise AcquisitionError(f"fetch failed: {exc}")
    try:
        return _stream_and_publish(
            response, url, dest_dir, name, legacy_part, max_bytes, chunk_size,
            deadline_s, clock, started, started_at, content_addressed, verify_fn)
    finally:
        try:
            getattr(response, "close", lambda: None)()
        except Exception:
            pass  # never mask the result or the original error


def _stream_and_publish(response, url, dest_dir, name, legacy_part, max_bytes,
                        chunk_size, deadline_s, clock, started, started_at,
                        content_addressed, verify_fn) -> dict:
    status = getattr(response, "status_code", 200)
    if status == 304:
        _unlink_quiet(legacy_part)
        raise MediaNotModified(f"media returned 304; DAI media is always refetched: {url[:80]}")
    if status < 200 or status >= 300:
        _unlink_quiet(legacy_part)
        raise AcquisitionError(f"fetch failed with HTTP {status}: {url[:80]}")
    declared = _header(response, "content-length")
    try:
        declared_n = int(declared) if declared is not None else None
    except (TypeError, ValueError):
        declared_n = None
    encoding = (_header(response, "content-encoding") or "").strip().lower()
    if encoding in ("", "identity"):
        verified_n = declared_n
    else:
        # The server ignored Accept-Encoding: identity; Content-Length counts
        # compressed bytes, so it cannot verify the decoded stream.
        verified_n = None
    if declared_n is not None and declared_n > max_bytes:
        _unlink_quiet(legacy_part)
        raise OverBudget(f"declared {declared_n} bytes exceeds cap {max_bytes}")
    content_type = (_header(response, "content-type") or "").split(";")[0].strip().lower()
    if (content_type.startswith("text/") or content_type in _NON_AUDIO_TYPES
            or content_type.endswith(("+json", "+xml"))):
        _unlink_quiet(legacy_part)
        raise NotAudio(f"media response is {content_type}, not audio: {url[:80]}")
    try:
        fd, part_name = tempfile.mkstemp(dir=dest_dir, prefix=name + ".", suffix=".part")
    except OSError as exc:
        raise AcquisitionError(f"cannot create part file: {exc}")
    part = Path(part_name)
    _unlink_quiet(legacy_part)
    digest = hashlib.sha256()
    received = 0
    probe_s = None
    try:  # finally removes the part file on every exit, Ctrl-C included
        try:
            with os.fdopen(fd, "wb") as handle:
                for chunk in response.iter_content(chunk_size):
                    if time.monotonic() - clock > deadline_s:
                        raise DeadlineExceeded(f"download exceeded {deadline_s}s deadline")
                    if not chunk:
                        continue
                    received += len(chunk)
                    if received > max_bytes:
                        raise OverBudget(f"received {received} bytes exceeds cap {max_bytes}")
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        except AcquisitionError:
            raise
        except Exception as exc:
            raise AcquisitionError(f"download failed: {exc}")
        try:
            if verified_n is not None and received != verified_n:
                raise TruncatedDownload(f"received {received} of declared {verified_n} bytes")
            if received == 0:
                raise NotAudio(f"empty media body: {url[:80]}")
            with open(part, "rb") as handle:
                if not looks_like_audio(handle.read(16)):
                    raise NotAudio(f"body does not look like audio: {url[:80]}")
            verifier = verify_fn or (probe_audio_duration if verified_n is None else None)
            if verifier:
                probe_s = verifier(part)
            sha = digest.hexdigest()
            final = dest_dir / ((sha + Path(name).suffix) if content_addressed else name)
            os.chmod(part, 0o644)
            if content_addressed:
                os.replace(part, final)  # same name means same bytes
            else:
                _publish_no_clobber(part, final, sha)
            _fsync_dir(dest_dir)
        except AcquisitionError:
            raise
        except Exception as exc:
            raise AcquisitionError(f"publish failed: {exc}")
    finally:
        _unlink_quiet(part)
    ended_at = _utcnow()
    snapshot = snapshot_id(url, sha, started_at)
    return {
        "status": "fetched",
        "url": url,
        "final_url": getattr(response, "url", None) or url,
        "http_status": status,
        "redirect_chain": [{"url": getattr(hop, "url", None),
                            "status": getattr(hop, "status_code", None)}
                           for hop in getattr(response, "history", None) or []],
        "content_type": _header(response, "content-type"),
        "content_length": declared_n,
        "content_encoding": encoding or None,
        # False: completeness rests on the decode probe alone (a cleanly cut
        # no-Xing mp3 passes it); downstream should treat the bytes as unverified.
        "length_verified": verified_n is not None,
        "probe_duration_s": probe_s if isinstance(probe_s, float) else None,
        "date": _header(response, "date"),
        "age": _header(response, "age"),
        "cache_control": _header(response, "cache-control"),
        "response_headers": {n: v for n in _OBSERVED_HEADERS
                             if (v := _header(response, n)) is not None},
        "path": str(final),
        "content_addressed": content_addressed,
        "bytes": received,
        "sha256": sha,
        "snapshot_id": snapshot,
        "fetch_started_at": started_at,
        "fetch_ended_at": ended_at,
        "elapsed_s": round(time.time() - started, 3),
        "validators": {
            "etag": _header(response, "etag"),
            "last_modified": _header(response, "last-modified"),
        },
    }


def load_json_document(path: str | os.PathLike) -> object:
    """Parse a JSON document; truncation is an error, never a complete read."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise TruncatedDocument(f"truncated JSON in {path}: {exc}")
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise AcquisitionError(f"cannot read {path}: {exc}")


def fetch_observation(fetch_mode: str, episode_key: dict, validators: dict | None,
                      result: dict) -> dict:
    """Build the per-request fetch observation record (plan section 3)."""
    return {
        "episode_key": episode_key,
        "fetch_mode": fetch_mode,
        "validators": validators or {},
        "status": result.get("status", "unknown"),
        "response": {k: v for k, v in result.items() if k != "path"},
        "fetch_started_at": result.get("fetch_started_at", ""),
        "fetch_ended_at": result.get("fetch_ended_at", ""),
    }


def _utcnow() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
