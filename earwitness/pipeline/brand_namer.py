"""Brand naming for Jev-flagged ad windows.

Jev finds where an ad read sits but cannot say whose ad it is. This module
names the brand from the transcript text of a flagged span. It is the
winner of the 2026-10-09 local bake-off
(.corpus-scale-20261008/brand-naming-bakeoff.md; holdout top-1 lenient 71%
on tight read windows, 65% on 60-s Jev windows, top-3 77%):

1. domain/CTA extraction: spoken and written domains ("x dot com",
   "getx.com slash y") and names after cue phrases ("brought to you by",
   "let me tell you about", "go to", "use code" ...);
2. GLiNER-small zero-shot NER as the fallback when extraction is not
   confident (optional: needs `gliner` + `torch`, imported lazily);
3. sound-alike repair (metaphone + Jaro-Winkler + edit distance) against a
   brand list = the curated gazetteer + domain labels harvested from corpus
   transcripts (`harvest_brand_list`).

Thresholds were tuned on the DEV set only (scripts/named_reads_eval.py
tune); the holdout was never used to choose them.

The phonetic functions are a stdlib port of jellyfish 1.2.1 (`metaphone`,
`jaro_winkler_similarity`, `levenshtein_distance`) as the bake-off used
them, checked equal on the bake-off's word lists, so extraction + repair
need no third-party package.
"""
from __future__ import annotations

import collections
import contextlib
import copy
import functools
import json
import re
from pathlib import Path

from earwitness.gazetteer import GAZETTEER, normalize_brand

GLINER_MODELS = {"small": "urchade/gliner_small-v2.1"}
GLINER_REVISIONS = {"small": "4e091416cf7c3481db542c2a3d26156916f3a47f"}
GLINER_LABELS = ["sponsor brand", "company", "product", "website", "promo code"]
GLINER_LABEL_W = {"sponsor brand": 1.5, "company": 1.0, "product": 0.6, "website": 1.2}
GLINER_THRESHOLD = 0.3
GLINER_CHUNK_WORDS = 150

# Raw-score thresholds tuned on DEV only (`named_reads_eval.py tune --split
# dev`: the bake-off objective, mean dev top-1 lenient over tight and 60-s
# windows minus half the dev non-ad naming rate; 28 reads, 20 non-ad
# windows). Dev cannot tell apart any domain/CTA threshold in (1.2, 3.8];
# the scan keeps the observed score 3.8. (The bake-off's 2.6 / 1.561 came
# from a scan whose cut points included holdout scores.)
DEV_THRESHOLDS = {"cta": 3.8, "gliner": 1.709}
TOP_K = 5
# Span rules from the 2026-10-10 engine check (engine-validation.md fixes 1
# and 3), both chosen on DEV before the holdout was scored:
# - merge rule "both": neighbouring flagged windows also join when either
#   window's name needs review or either brand is among the other window's
#   candidates, and the span takes the brand of its most confident window
#   ("deployed" is the earlier rule: join only on the same brand). Two
#   confident different brands never share a span, directly or through an
#   unsure window, and joins on anything but the same brand stop at
#   MAX_SPAN_WINDOWS windows (review of 897e9f2: without both limits a pod
#   of six confident spots chained into one 720-s span);
# - a second brand per span (ad pods): a runner-up candidate spoken as a
#   domain, scoring at least SECOND_BRAND_MIN_SCORE and no sound-alike of a
#   brand already named for the span. The engine check's prototype used the
#   CTA threshold (3.8); the dev scan (`rename --second-brand-min-score`,
#   2.0-10.0) keeps both dev pod reads (scores 10.9, 12.4) and drops its one
#   wrong second brand (6.0) for any value in (6.5, 10.9]: the low edge, 7.0.
MERGE_RULES = ("deployed", "both")
DEFAULT_MERGE_RULE = "both"
SECOND_BRAND_MIN_SCORE = 7.0
MAX_SPAN_WINDOWS = 3
WORDS_PATH = "/usr/share/dict/words"  # present on macOS; bare dictionary words rank lower

TLD = r"(?:com|net|org|io|co|fm|ai|app|tv|me)"
DOMAIN_RE = re.compile(
    r"((?:[a-z0-9'&-]+\s+){0,2}[a-z0-9'&-]+)(?:\.|\s+dot\s+)" + TLD + r"\b", re.I)
CUT_WORDS = {
    "go", "to", "visit", "at", "head", "over", "check", "out", "the", "a", "an", "and",
    "that's", "thats", "is", "it's", "its", "on", "website", "site", "our", "my", "your",
    "slash", "dot", "www", "or", "just", "now", "today", "online", "app", "download",
    "find", "learn", "more", "sign", "up", "get", "started", "by", "from", "for", "in",
    "of", "with", "you", "can", "also", "again", "again,", "simply", "there", "here",
    "please", "today,", "only", "us", "me", "them", "this", "so", "um", "uh", "like",
    "that", "visiting", "minutes", "if", "but", "when", "then", "than", "be", "was",
    "were", "are", "do", "does", "see", "hop", "run", "into", "about", "under",
    "every", "all", "any", "it", "they", "we", "i", "he", "she", "what", "who", "how",
    "click", "call", "text", "says", "say", "said", "says,", "page", "two", "one",
}
# Domains that are infrastructure, not sponsors (hosting, social, email).
INFRA = {
    "gmail", "yahoo", "hotmail", "outlook", "icloud", "aol", "protonmail", "megaphone",
    "podtrac", "omny", "omnystudio", "simplecast", "acast", "art19", "libsyn", "anchor",
    "spotify", "apple", "podcasts", "iheart", "iheartradio", "patreon", "instagram",
    "twitter", "facebook", "tiktok", "x", "linkedin", "substack", "pod", "podcast",
    "show", "email", "mail", "site", "website", "help", "mobile", "business", "link",
    "privacy", "policy", "privacypolicy", "adchoices", "pcm", "terms", "info", "news",
}
PREFIXES = ("get", "try", "drink", "meet", "join", "go", "use", "shop", "my", "the")
STOP = {
    "the", "a", "an", "our", "my", "your", "their", "this", "that", "these", "it", "its",
    "them", "you", "we", "us", "me", "i", "he", "she", "they", "today", "episode", "show",
    "podcast", "website", "link", "description", "app", "and", "or", "to", "of", "for",
    "with", "on", "in", "at", "is", "are", "be", "all", "some", "more", "now", "here",
    "there", "what", "who", "how", "um", "uh", "so", "just", "like", "one", "people",
    "everyone", "everything", "something", "anything", "out", "up", "get", "go", "check",
    "see", "learn", "find", "try", "sure", "again", "also", "really", "very",
    "yeah", "okay", "oh", "well", "sponsor", "sponsors", "company", "companies", "brand",
    "brands", "product", "products", "free", "new", "best", "first", "next", "last",
    "time", "day", "week", "month", "year", "home", "work", "life", "way", "thing", "things",
    "mean", "know", "think", "want", "need", "got", "make", "take", "let", "tell", "about",
}
ANCHORS = [  # (cue regex, weight)
    (r"brought to you by", 3.0), (r"sponsored by", 3.0), (r"today'?s sponsor(?: is)?", 3.0),
    (r"support for [^.]{0,80}? comes from", 3.0), (r"thanks? (?:you )?to", 1.5),
    (r"(?:let me|i want to|wanna|want to) tell you about", 2.5),
    (r"(?:go|head|head over|hop over|run) to", 1.5), (r"visit", 1.5), (r"check out", 1.0),
    (r"download the", 1.0), (r"switch to", 1.0), (r"sign up (?:for|at)", 1.0),
    (r"(?:shop|available) at", 1.0), (r"(?:i|we) (?:love|use|trust)", 0.7),
]
ANCHOR_RE = re.compile(
    r"\b(?:" + "|".join(f"(?P<a{i}>{a})" for i, (a, _) in enumerate(ANCHORS))
    + r")[\s,]+((?:[a-z0-9'&.-]+\s+){0,2}[a-z0-9'&.-]+)", re.I)
CTA_RE = re.compile(r"\b(?:use (?:the )?code|promo code|\d+% off|go to|head to|visit|"
                    r"dot com|\.com|sponsored by|brought to you by|free shipping|"
                    r"first month|sign up|download the)\b", re.I)
# Pronouns a short sub-window's text can make look like a name; only
# sub-window naming drops them, so default naming stays the runner's (and
# never "hers": Hers, of Hims & Hers, is an advertiser).
SUB_WINDOW_STOP = {"his", "her", "him"}
# Sub-window naming (name_windows(sub_window_s=...)): flagged windows are
# named in sub-windows of this length so back-to-back reads get one span
# each; chosen on DEV only (scripts/named_reads_eval.py run --sub-window-s).
SUB_WINDOW_S = 20.0
SUB_WINDOW_PAD_S = 5.0   # text either side of a sub-window (URLs cut at its edge)
SUB_WINDOW_GAP_S = 20.0  # unnamed time a same-brand run may bridge
# Strong read openers: a sub-window boundary is put where one starts.
OPENER_RE = re.compile(r"\b(?:brought to you by|sponsored by|today'?s sponsor|"
                       r"support for [^.]{0,80}? comes from)", re.I)
_TLD_SPLIT = re.compile(r"(?:\.|\s+dot\s+)" + TLD + r"\b")
# Cheap necessary condition for a DOMAIN_RE match (that regex is slow to
# scan over text with no domain in it).
_TLD_HINT = re.compile(r"(?:\.|\sdot\s+)" + TLD + r"\b", re.I)


# ---------------------------------------------------------------- phonetics
# Stdlib port of jellyfish 1.2.1 (BSD-2): equal outputs on lowercase input.

def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def jaro_winkler(s1: str, s2: str) -> float:
    n1, n2 = len(s1), len(s2)
    if not n1 or not n2:
        return 0.0
    rng = max(0, max(n1, n2) // 2 - 1)
    f1, f2 = [False] * n1, [False] * n2
    common = 0
    for i, ch in enumerate(s1):
        for j in range(max(0, i - rng), min(i + rng, n2 - 1) + 1):
            if not f2[j] and s2[j] == ch:
                f1[i] = f2[j] = True
                common += 1
                break
    if not common:
        return 0.0
    k = trans = 0
    for i in range(n1):
        if f1[i]:
            j = k
            while j < n2:
                if f2[j]:
                    k = j + 1
                    break
                j += 1
            if s1[i] != s2[j]:
                trans += 1
    trans //= 2
    w = (common / n1 + common / n2 + (common - trans) / common) / 3
    if w > 0.7:
        p = 0
        while p < min(n1, n2, 4) and s1[p] == s2[p]:
            p += 1
        w += p * 0.1 * (1.0 - w)
    return w


def metaphone(s: str) -> str:
    s = s.lower()
    if s[:2] in ("kn", "gn", "pn", "wr", "ae"):
        s = s[1:]
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        nx = s[i + 1] if i + 1 < n else ""
        nn = s[i + 2] if i + 2 < n else ""
        if c == nx and c != "c":
            i += 1
            continue
        if c in "aeiou":
            if i == 0 or s[i - 1] == " ":
                out.append(c)
        elif c == "b":
            if not (i and s[i - 1] == "m") or nx:
                out.append("b")
        elif c == "c":
            if (nx == "i" and nn == "a") or nx == "h":
                out.append("x")
                i += 1
            elif nx and nx in "iey":
                out.append("s")
                i += 1
            else:
                out.append("k")
        elif c == "d":
            if nx == "g" and nn and nn in "iey":
                out.append("j")
                i += 2
            else:
                out.append("t")
        elif c in "fjlmnr":
            out.append(c)
        elif c == "g":
            if nx and nx in "iey":
                out.append("j")
            elif nx == "h" and nn and nn not in "aeiou":
                i += 1
            elif nx == "n" and not nn:
                i += 1
            else:
                out.append("k")
        elif c == "h":
            if i == 0 or (nx and nx in "aeiou") or s[i - 1] not in "aeiou":
                out.append("h")
        elif c == "k":
            if i == 0 or s[i - 1] != "c":
                out.append("k")
        elif c == "p":
            if nx == "h":
                out.append("f")
                i += 1
            else:
                out.append("p")
        elif c == "q":
            out.append("k")
        elif c == "s":
            if nx == "h":
                out.append("x")
                i += 1
            elif nx == "i" and nn and nn in "oa":
                out.append("x")
                i += 2
            else:
                out.append("s")
        elif c == "t":
            if nx == "i" and nn and nn in "oa":
                out.append("x")
            elif nx == "h":
                out.append("0")
                i += 1
            elif nx != "c" or nn != "h":
                out.append("t")
        elif c == "v":
            out.append("f")
        elif c == "w":
            if i == 0 and nx == "h":
                i += 1
                out.append("w")
            elif nx and nx in "aeiou":
                out.append("w")
        elif c == "x":
            if i == 0:
                out.append("x" if nx == "h" or (nx == "i" and nn and nn in "oa") else "s")
            else:
                out.append("ks")
        elif c == "y":
            if nx and nx in "aeiou":
                out.append("y")
        elif c == "z":
            out.append("s")
        elif c == " ":
            if out and out[-1] != " ":
                out.append(" ")
        i += 1
    return "".join(out).upper()


# ---------------------------------------------------------------- brand list

def compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def domain_label(capture: str) -> str | None:
    """'go to helixsleep' -> 'helixsleep': trailing content words of a
    domain capture, compacted; None for infrastructure or junk."""
    words = [w.strip(".,!?'\"").lower() for w in capture.split()]
    words = [w[:-2] if w.endswith("'s") else w for w in words]
    keep: list[str] = []
    for w in reversed(words):
        if not w or w in CUT_WORDS:
            break
        keep.insert(0, w)
    lab = re.sub(r"[^a-z0-9]", "", "".join(keep))
    if len(lab) < 3 or lab in INFRA or lab.isdigit():
        return None
    return lab


def domain_labels(capture: str) -> list[str]:
    """Last-word label, plus the two-word compact form when both words are
    content words ('mint mobile.com' -> mobile, mintmobile)."""
    words = [w.strip(".,!?'\"").lower() for w in capture.split()]
    out = []
    one = domain_label(words[-1]) if words else None
    if one:
        out.append(one)
    if len(words) >= 2:
        two = domain_label(" ".join(words[-2:]))
        if two and two != one:
            out.append(two)
    return out


def _similar(a: str, b: str) -> bool:
    """Two spellings of one brand (STT variants)."""
    la, lb = len(a), len(b)
    if min(la, lb) < 5:
        return False
    # Exact prefilter: Jaro-Winkler >= 0.92 needs length ratio >= 0.6 and
    # the other two tests need lengths within 2.
    if min(la, lb) < 0.6 * max(la, lb) and abs(la - lb) > 2:
        return False
    if jaro_winkler(a, b) >= 0.92:
        return True
    if a[:3] == b[:3] and abs(la - lb) <= 1 and levenshtein(a, b) <= 2:
        return True
    return abs(la - lb) <= 2 and _metaphone_cached(a) == _metaphone_cached(b) \
        and levenshtein(a, b) <= 2


@functools.lru_cache(maxsize=1 << 16)
def _metaphone_cached(s: str) -> str:
    return metaphone(s)


def _transcript_segments(transcript_paths):
    for path in sorted(str(p) for p in transcript_paths):
        try:
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        yield Path(path).stem, doc.get("segments") or [] if isinstance(doc, dict) else []


def _spoken_domain_labels(segments):
    """(label, k) for each domain label spoken in `segments` (k > 0: a
    two-word split of the same domain)."""
    for s in segments:
        text = str(s.get("text") or "")
        if not _TLD_HINT.search(text):
            continue
        for m in DOMAIN_RE.finditer(text):
            yield from ((lab, k) for k, lab in enumerate(domain_labels(m.group(1))))


def harvest_brand_list(transcript_paths=(), *, min_episodes: int = 1,
                       episodes=None, trusted=None) -> list:
    """The repair brand list: the curated gazetteer plus every domain label
    spoken in `transcript_paths` (one transcript JSON per episode; or
    `episodes`, (episode_id, segments) pairs, instead), with STT variants
    grouped onto the spelling heard in the most episodes (curated names
    always win their group). `trusted`, (episode_id, segments) pairs from a
    stronger speech model, overrides the majority: a spelling heard there
    heads its group before any spelling heard only in weaker text (a common
    first-pass garble cannot rename it). Deterministic for a given set of
    files; no brand is ever added by hand. Returns [{"variant", "brand",
    "weight"}], heaviest first."""
    eps: dict = collections.defaultdict(set)
    two_word: set = set()
    for ep, segments in (_transcript_segments(transcript_paths) if episodes is None
                         else episodes):
        for lab, k in _spoken_domain_labels(segments):
            eps[lab].add(ep)
            if k:
                two_word.add(lab)
    good: dict = collections.defaultdict(set)
    for ep, segments in trusted or ():
        for lab, _ in _spoken_domain_labels(segments):
            good[lab].add(ep)
    weight: dict = {}
    display: dict = {}
    for canon, aliases in GAZETTEER.items():
        c = compact(canon)
        weight[c] = 1000.0
        display[c] = canon
        for a in aliases:
            ca = compact(a)
            if len(ca) >= 3 and ca not in weight:
                weight[ca] = 0.5
                display[ca] = canon
    for lab, s in eps.items():
        if len(s) < min_episodes or (len(s) < 2 and lab in two_word):
            continue
        if lab not in weight:
            weight[lab] = float(len(s))
            display[lab] = lab
    labs = sorted(weight, key=lambda x: -weight[x])
    head_of: dict = {}
    heads: list = []
    # group heads: curated names, then trusted spellings, then by weight
    # (without `trusted` this is exactly the weight order)
    for lab in sorted(labs, key=lambda x: (-(weight[x] >= 1000.0), -len(good.get(x, ())),
                                           -weight[x])):
        h = next((h for h in heads if _similar(lab, h)), None)
        if h is None:
            heads.append(lab)
            h = lab
        head_of[lab] = h
    return [{"variant": lab,
             "brand": display[head_of[lab]] if display[lab] == lab else display[lab],
             "weight": weight[lab]} for lab in labs]


def brand_list_digest(brand_list: list) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(brand_list, sort_keys=True).encode()).hexdigest()


# Every brand list a namer was built from, kept by digest
# (<corpus>/brand-lists/<sha256>.json; names docs record the digest under
# brand_list.sha256), so a naming can be explained and re-run after the
# harvested brand-list.json was rebuilt by a later worker start.
BRAND_LISTS_DIR = "brand-lists"


def archive_brand_list(corpus, brand_list: list) -> str:
    """Keep `brand_list` under <corpus>/brand-lists/<digest>.json (written
    once, atomically; same list, same file). Returns the digest."""
    import os
    import tempfile
    sha = brand_list_digest(list(brand_list))
    d = Path(corpus) / BRAND_LISTS_DIR
    path = d / f"{sha}.json"
    if not path.is_file():
        d.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(list(brand_list), f)
        os.replace(tmp, path)
    return sha


# Every names/<ep>.json writer (name worker, name-backfill, refine, rename)
# writes under an exclusive lock on <corpus>/.names.lock (beside names/).
NAMES_LOCK = ".names.lock"


class NamesLockUnavailable(RuntimeError):
    """<corpus>/.names.lock cannot be opened (e.g. a read-only corpus root)."""


def _names_lock_path(names_dir) -> Path:
    return Path(names_dir).parent / NAMES_LOCK


def check_names_lock(names_dir) -> None:
    """Fail fast, before a pass starts, when the names lock cannot be opened."""
    path = _names_lock_path(names_dir)
    try:
        with open(path, "a"):
            pass
    except OSError as exc:
        raise NamesLockUnavailable(f"cannot open the names write lock {path}: {exc.strerror} "
                                   "(is the corpus root read-only?)") from exc


@contextlib.contextmanager
def names_lock(names_dir):
    """Hold the exclusive names write lock of the corpus owning `names_dir`."""
    import fcntl
    with open(_names_lock_path(names_dir), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def load_archived_brand_list(corpus, sha256: str) -> list | None:
    """The archived brand list with this digest; None when absent or not
    matching its digest."""
    try:
        blist = json.loads((Path(corpus) / BRAND_LISTS_DIR / f"{sha256}.json").read_text(
            encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return blist if isinstance(blist, list) and brand_list_digest(blist) == sha256 else None


# ---------------------------------------------------------------- ranking

def _load_words(path=WORDS_PATH) -> frozenset:
    try:
        return frozenset(w.strip().lower() for w in open(path, encoding="utf-8"))
    except OSError:
        return frozenset()


def _segments_text(text_segments) -> str:
    if isinstance(text_segments, str):
        return text_segments
    parts = []
    for s in text_segments or ():
        t = s.get("text") if isinstance(s, dict) else s
        if isinstance(t, str) and t.strip():
            parts.append(t.strip())
    return " ".join(parts)


class _Ranker:
    """Score accumulator keyed by normalized brand, with display spelling and
    the evidence behind each score."""

    def __init__(self):
        self.scores = collections.defaultdict(float)
        self.display: dict = {}
        self.evidence = collections.defaultdict(list)

    def add(self, cand: str, w: float, ev: str | None = None):
        k = compact(normalize_brand(cand) or cand)
        if len(k) < 3 or cand.lower() in STOP:
            return
        self.scores[k] += w
        d = self.display.get(k)
        if d is None or (compact(cand) == k and compact(d) != k) or (
                compact(cand) == k and " " in cand and " " not in d):
            self.display[k] = cand
        if ev and ev not in self.evidence[k]:
            self.evidence[k].append(ev)

    def ranked(self) -> list:
        return [(self.display[k], v, list(self.evidence[k]))
                for k, v in sorted(self.scores.items(), key=lambda kv: -kv[1])]


def _clean_tokens(capture: str) -> list[str]:
    toks = []
    for w in capture.lower().split():
        w = w.strip(".,!?\"'")
        if w.endswith("'s"):
            w = w[:-2]
        if not w or w in STOP or w in CUT_WORDS:
            break
        toks.append(w)
    return toks


def count_mentions(cand: str, text_l: str) -> int:
    c = compact(cand)
    if len(c) < 3:
        return 0
    n = len(re.findall(r"\b" + re.escape(cand.lower()) + r"(?:'s|s)?\b", text_l))
    n += len(re.findall(re.escape(c) + r"(?:\.|\s+dot\s+)",
                        re.sub(r"\s+(?=\S+(?:\.|\s+dot\s+)com)", "", text_l)))
    return n


def respace(label: str, text_l: str) -> str:
    """'helixsleep' -> 'helix sleep' when the window says it that way."""
    words = re.findall(r"[a-z0-9'&-]+", text_l)
    for n in (2, 3):
        for i in range(len(words) - n + 1):
            if compact(" ".join(words[i:i + n])) == label:
                return " ".join(words[i:i + n])
    return label


def strip_prefix(label: str, text_l: str) -> str:
    """'getacme' -> 'acme' when the window also says 'acme'."""
    for p in PREFIXES:
        rest = label[len(p):]
        if label.startswith(p) and len(rest) >= 3 and re.search(
                r"\b" + re.escape(rest) + r"\b", text_l):
            return rest
    return label


def rank_cta(text: str, words: frozenset = frozenset()) -> list:
    """Domain/CTA extraction: [(candidate, score, evidence)], best first."""
    t = text.lower()
    r = _Ranker()
    for m in DOMAIN_RE.finditer(t):
        labs = domain_labels(m.group(1))
        if not labs:
            continue
        # two-word form only when the last word alone is a short dictionary
        # word or the spoken two-word name recurs in the window
        lab = labs[0]
        if len(labs) > 1:
            two = respace(labs[1], t)
            if lab in words and len(lab) <= 6 or \
                    count_mentions(two, t) > count_mentions(lab, t) - 1:
                lab = labs[1]
        lab = strip_prefix(lab, t)
        r.add(respace(lab, t), 3.0, "domain")
    for m in ANCHOR_RE.finditer(t):
        i = next(i for i in range(len(ANCHORS)) if m.group(f"a{i}"))
        w, cue = ANCHORS[i][1], m.group(f"a{i}")
        cap = _TLD_SPLIT.split(m.group(len(ANCHORS) + 1))[0]
        toks = _clean_tokens(cap)
        for n in range(1, len(toks) + 1):
            cand = " ".join(toks[:n]).strip(".")
            cand = re.sub(r"\." + TLD + "$", "", cand)
            if cand in STOP:
                continue
            reps = count_mentions(cand, t)
            r.add(cand, w * (1.0 if n == 1 else 0.8) + 0.3 * min(reps, 5),
                  "cue:" + re.sub(r"\s+", " ", cue)[:40])
    for k in list(r.scores):
        mentions = count_mentions(r.display[k], t)
        r.scores[k] += 0.5 * min(mentions, 6)
        if mentions:
            r.evidence[k].append(f"mentions:{mentions}")
        # bare dictionary words with no domain support are weak
        if " " not in r.display[k] and r.display[k] in words and r.scores[k] < 3:
            r.scores[k] *= 0.5
    return r.ranked()


def _cta_near(text: str, start: int, end: int) -> float:
    return 1.0 if CTA_RE.search(text[max(0, start - 150):end + 150]) else 0.0


def _chunks(text: str, n_words: int):
    words = text.split(" ")
    pos, out = 0, []
    for i in range(0, len(words), n_words):
        chunk = " ".join(words[i:i + n_words])
        out.append((pos, chunk))
        pos += len(chunk) + 1
    return out


def rank_gliner(text: str, model) -> list:
    """GLiNER zero-shot entities weighted by label, by a nearby call to
    action, and by matching a spoken domain: [(candidate, score, evidence)]."""
    t = text.lower()
    domains = {domain_label(m.group(1)) for m in DOMAIN_RE.finditer(t)} - {None}
    r = _Ranker()
    for off, ch in _chunks(text, GLINER_CHUNK_WORDS):
        for e in model.predict_entities(ch, GLINER_LABELS, threshold=GLINER_THRESHOLD):
            if e["label"] not in GLINER_LABEL_W:
                continue
            a, b = off + e["start"], off + e["end"]
            s = re.sub(r"\." + TLD + r"\b.*$", "", e["text"].strip(" .,'\"").lower())
            if s.endswith("'s"):
                s = s[:-2]
            if not s or s in STOP or len(compact(s)) < 3:
                continue
            w = e["score"] * GLINER_LABEL_W[e["label"]] * (1.0 + _cta_near(text, a, b))
            if compact(s) in domains:
                w += 2.0
            r.add(s, w, "gliner:" + e["label"])
    return r.ranked()


def load_gliner(size: str = "small", threads: int | None = None):
    """The pinned GLiNER model, loaded once per process (lazy: gliner and
    torch are imported only here)."""
    import torch
    if threads:
        torch.set_num_threads(threads)
    return _gliner(size)


@functools.lru_cache(maxsize=None)
def _gliner(size: str):
    from gliner import GLiNER
    return GLiNER.from_pretrained(GLINER_MODELS[size], revision=GLINER_REVISIONS[size])


# ---------------------------------------------------------------- namer

class BrandNamer:
    """name_span(text_segments) names the brand of one ad span.

    Cascade (the bake-off's recommended pipeline): the domain/CTA answer
    when its score clears its DEV threshold; else the GLiNER answer when
    that clears its own; else the best remaining candidate, flagged for
    review. Sound-alike repair maps every candidate onto the brand list.

    `gliner_model`: None (extraction + repair only), "small" (load the
    pinned GLiNER-small; falls back to extraction + repair when gliner or
    torch is not installed, recorded in `pipeline` and `gliner_error`), or
    an object with GLiNER's predict_entities()."""

    def __init__(self, brand_list, gliner_model=None, *, thresholds: dict | None = None,
                 words: frozenset | None = None, threads: int | None = None,
                 corrections=None):
        self.entries, self.weights = {}, {}
        for e in brand_list or ():
            self.entries[e["variant"]] = e["brand"]
            self.weights[e["variant"]] = float(e["weight"])
        # The operator's rules (corrections.Corrections, None = off): a new
        # brand whose spelling is not on the list yet is one exact entry, its
        # own spelling only. A spelling already on the list keeps its group
        # (the alias at canon / export gives those reads the canonical), and
        # the sound-alike repair still looks only at the given list, so a
        # spelling that is not exactly a new brand is named as without the
        # rules. Stoplisted words are never a
        # brand (name_span). Aliases never reach the namer: the heard name,
        # the candidates and their scores stay as heard (canon and export
        # apply them). The brand list and its digest stay as given.
        self.brand_list_size = len(self.entries)
        self.corrections = corrections
        self._repair_to = dict(self.entries)     # the sound-alike targets
        self._list_weights = dict(self.weights)
        if corrections is not None:
            for variant, brand in corrections.namer_brands().items():
                if variant not in self.entries:
                    self.entries[variant] = brand
                    self.weights[variant] = 1000.0
        self._keys = list(self._repair_to)
        self._mph = {k: metaphone(k) for k in self._keys}
        self.brand_list_sha256 = brand_list_digest(list(brand_list or ()))
        self.thresholds = dict(DEV_THRESHOLDS, **(thresholds or {}))
        self.words = _load_words() if words is None else words
        self.gliner, self.gliner_name, self.gliner_error = None, None, None
        if isinstance(gliner_model, str):
            try:
                self.gliner = load_gliner(gliner_model, threads)
                self.gliner_name = GLINER_MODELS[gliner_model]
            except Exception as exc:  # any load failure (offline, bad cache): extraction + repair only
                self.gliner_error = f"{type(exc).__name__}: {exc}"[:300]
        elif gliner_model is not None:
            self.gliner, self.gliner_name = gliner_model, type(gliner_model).__name__
        self.pipeline = "cta+gliner+repair" if self.gliner else "cta+repair"
        # advertisers.Advertisers (off: None): real advertiser names that
        # repair trusts before the harvested brand list
        self.advertisers = None

    def without_rules(self) -> "BrandNamer":
        """This namer with no operator rules (no stoplist, no new brands, no
        advertiser list; the same brand list and model): names a span a
        person confirmed (protected_spans) as without the corrections file."""
        other = copy.copy(self)
        other.corrections = None
        other.advertisers = None   # as on the runner: no advertiser list either
        other.entries, other.weights = dict(self._repair_to), dict(self._list_weights)
        return other

    def describe(self, episode_id=None) -> dict:
        """The names doc's naming fields; with corrections, the episode's
        namer digest (Corrections.namer_provenance); with an advertiser
        list, its digest."""
        out = {"pipeline": self.pipeline, "gliner_model": self.gliner_name,
               "gliner_revision": GLINER_REVISIONS.get("small") if self.gliner_name
               == GLINER_MODELS["small"] else None,
               "gliner_error": self.gliner_error, "thresholds": dict(self.thresholds),
               "brand_list": {"entries": self.brand_list_size,
                              "sha256": self.brand_list_sha256},
               # only with namer rules: names without them stay byte-identical
               **({"corrections": prov} if self.corrections is not None
                  and (prov := self.corrections.namer_provenance(episode_id)) else {})}
        if self.advertisers is not None:  # recorded only when on
            out["advertisers"] = self.advertisers.describe()
        return out

    def _advertiser_repair(self, ranked: list) -> list:
        """With an advertiser list: a product phrase collapses to its brand
        (heads: the list, and the other candidates spoken as a domain;
        evidence "product:<phrase>"), then a name on the list or a
        sound-alike of a trusted entry takes the entry's name (evidence
        "advertiser" / "advertiser:<from>-><to>"), so a mishearing in the
        harvested list can no longer anchor it."""
        adv = self.advertisers
        heads = [c for c, _, ev in ranked if "domain" in ev]
        out = []
        for cand, sc, ev in ranked:
            ev = list(ev)
            new, rule, product = adv.canonical(cand, heads, self.words)
            if product:
                ev.append(f"product:{product}")
            if rule and "advertiser" in rule:
                ev.append("advertiser" if new == str(product or cand).lower()
                          else f"advertiser:{product or cand}->{new}")
            elif adv.trusted(new):
                ev.append("advertiser")
            out.append((new, sc, ev))
        # the list never lifts a name that starts another listed candidate
        # ("google" next to "google chrome"): that would flip the pick to
        # the parent brand
        listed = {str(b).lower() for b, _, ev in out
                  if any(x == "advertiser" or x.startswith("advertiser:") for x in ev)}
        return [(b, sc, [x for x in ev if not (x == "advertiser" or x.startswith("advertiser:"))]
                 if any(o.startswith(str(b).lower() + " ") for o in listed) else ev)
                for b, sc, ev in out]

    def repair(self, ranked: list) -> list:
        """Map each candidate onto the brand list (exact, phonetic, or
        fuzzy) and merge candidates that land on the same brand. With an
        advertiser list, _advertiser_repair runs first and a name it put on
        the list skips the harvested brand list."""
        out = collections.OrderedDict()
        if self.advertisers is not None:
            ranked = self._advertiser_repair(ranked)
        for cand, sc, ev in ranked:
            c = compact(cand)
            new, bonus, ev = cand, 0.0, list(ev)
            if any(x == "advertiser" or x.startswith("advertiser:") for x in ev):
                bonus = 1.0
            elif c in self.entries:
                head = compact(self.entries[c])
                # a tie with the group head keeps the window's own spelling
                own = self.weights.get(c, 0) >= self.weights.get(head, 0)
                new, bonus = (cand if own else self.entries[c]), 1.0
                ev.append("brand-list" if own else f"repair:{cand}->{new}")
            elif len(c) >= 4:
                m = metaphone(c)
                best, bs = None, 0.0
                for k in self._keys:
                    mk = self._mph[k]
                    if abs(len(k) - len(c)) > 3 or k[0] != c[0] and mk[:1] != m[:1]:
                        continue
                    jw = jaro_winkler(c, k)
                    ok = (mk == m and jw >= 0.8) or jw >= 0.9 or (
                        len(c) >= 5 and k[:3] == c[:3] and levenshtein(c, k) <= 2)
                    if ok and jw > bs:
                        best, bs = k, jw
                if best:
                    new, bonus = self._repair_to[best], 0.5
                    ev.append(f"repair:{cand}->{new}")
            key = compact(normalize_brand(new) or new)
            if key in out:
                b, s, e0 = out[key]
                out[key] = (b, s + sc + bonus, e0 + [x for x in ev if x not in e0])
            else:
                out[key] = (new, sc + bonus, ev)
        return sorted(out.values(), key=lambda x: -x[1])

    def _confidence(self, score: float, method: str) -> float:
        tau = self.thresholds[method]
        return round(score / (score + tau), 3) if score > 0 else 0.0

    def name_span(self, text_segments) -> dict:
        """{"brand", "candidates": [{"brand", "score", "evidence"}],
        "confidence", "needs_review", "method", "pipeline"}. confidence is
        score / (score + dev threshold) of the deciding source, so 0.5 sits
        exactly at the threshold. needs_review: confidence < 0.5, or no
        domain/CTA evidence supports the name (GLiNER alone), or (with
        corrections) the pick was a stoplisted word: those are dropped from
        the candidates and the next one, if any, is the brand, with
        confidence 0."""
        text = _segments_text(text_segments)
        cta = rank_cta(text, self.words)
        tc, tg = self.thresholds["cta"], self.thresholds["gliner"]
        g = []
        if cta and cta[0][1] >= tc:
            ranked, method, top = self.repair(cta), "cta", cta[0][1]
        else:
            g = rank_gliner(text, self.gliner) if self.gliner is not None else []
            if g and g[0][1] >= tg:
                ranked = self.repair(g + [(c, 0.01 * s, e) for c, s, e in cta])
                method, top = "gliner", g[0][1]
            else:
                ranked = self.repair(cta + g)
                cta_top = cta[0][1] if cta else 0.0
                g_top = g[0][1] if g else 0.0
                if not ranked:
                    method, top = "none", 0.0
                elif cta and self._confidence(cta_top, "cta") >= self._confidence(g_top, "gliner"):
                    method, top = "cta", cta_top
                else:
                    method, top = "gliner", g_top
        confidence = self._confidence(top, method) if method != "none" else 0.0
        stopped = False
        if self.corrections is not None and self.corrections.stoplist:
            kept = [r for r in ranked if not self.corrections.stopped(r[0])]
            stopped = bool(ranked) and (not kept or kept[0] is not ranked[0])
            ranked = kept
            if stopped:   # the confidence was the stopped word's (as fix_name)
                confidence = 0.0
        brand = ranked[0][0] if ranked else None
        cta_keys = {compact(normalize_brand(b) or b) for b, _, _ in self.repair(cta)} \
            if method != "cta" else None
        cta_backed = method == "cta" or (brand is not None
                                         and compact(normalize_brand(brand) or brand) in cta_keys)
        return {"brand": brand,
                "candidates": [{"brand": b, "score": round(s, 3), "evidence": ev}
                               for b, s, ev in ranked[:TOP_K]],
                "confidence": confidence,
                "needs_review": brand is None or confidence < 0.5 or not cta_backed or stopped,
                "method": method, "pipeline": self.pipeline}


# ---------------------------------------------------------------- spans

def jev_windows(doc) -> list | None:
    """Sorted [{window_id, start_s, end_s, noul}] from a corpus jev/<ep>.json
    ("windows" list) or a triage-run episode ("answers" map); None if not
    a Jev result."""
    if not isinstance(doc, dict):
        return None
    if isinstance(doc.get("windows"), list):
        raw = [w for w in doc["windows"] if isinstance(w, dict)]
    elif isinstance(doc.get("answers"), dict):
        raw = [dict(v, window_id=k) for k, v in doc["answers"].items() if isinstance(v, dict)]
    else:
        return None
    out = []
    for w in raw:
        try:
            out.append({"window_id": str(w["window_id"]), "start_s": float(w["start_s"]),
                        "end_s": float(w["end_s"]), "noul": float(w.get("noul") or 0.0)})
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda w: (w["start_s"], w["window_id"]))


def _text_between(segments, lo: float, hi: float) -> list:
    return [s for s in segments if s["start"] < hi and s["end"] > lo]


def _same_brand(a, b) -> bool:
    if a is None or b is None:
        return True
    ka, kb = compact(normalize_brand(a) or a), compact(normalize_brand(b) or b)
    short = min(ka, kb, key=len)
    return ka == kb or (len(short) >= 4 and (ka in kb or kb in ka))


def _merge_candidates(results: list) -> list:
    best: dict = {}
    for res in results:
        for c in res["candidates"]:
            k = compact(normalize_brand(c["brand"]) or c["brand"])
            if k not in best or c["score"] > best[k]["score"]:
                ev = list(dict.fromkeys((best.get(k) or {}).get("evidence", []) + c["evidence"]))
                best[k] = dict(c, evidence=ev)
    return sorted(best.values(), key=lambda c: -c["score"])[:TOP_K]


def _lead(results: list, brand) -> dict:
    """The result that decides a span's confidence: the most confident one
    naming the span's brand, else the first."""
    lead = [r for r in results if r["brand"] is not None and _same_brand(r["brand"], brand)]
    return max(lead, key=lambda r: r["confidence"]) if lead else results[0]


def _key(brand) -> str:
    return compact(normalize_brand(brand) or brand)


def _names_in(brand, res: dict) -> bool:
    """`brand` is one of the window result's candidates."""
    return brand is not None and any(_same_brand(brand, c["brand"]) for c in res["candidates"])


def _confident(res: dict) -> bool:
    return res["brand"] is not None and not res["needs_review"]


def _joins(results: list, res: dict, span_brand, rule: str, max_windows) -> bool:
    """Whether flagged window `res` continues the span of `results` (its
    windows' names, in order; `span_brand` its first brand named), see
    MERGE_RULES. "both": never a second confident brand; the same brand
    and a window naming nothing always; else (a needs-review window, or a
    brand among the other window's candidates) only while the span has
    fewer than `max_windows` named windows (None: no cap)."""
    if rule == "deployed":
        return _same_brand(span_brand, res["brand"])
    sure = [r["brand"] for r in results if _confident(r)]
    if sure and _confident(res) and not _same_brand(sure[0], res["brand"]):
        return False
    if res["brand"] is None:
        return True
    ref = sure[0] if sure else span_brand
    if ref is not None and _same_brand(ref, res["brand"]):
        return True
    if max_windows is not None and sum(r["brand"] is not None for r in results) >= max_windows:
        return False
    prev = results[-1]
    return bool(prev["needs_review"] or res["needs_review"] or ref is None
                or _names_in(res["brand"], prev) or _names_in(prev["brand"], res))


def _span_lead(results: list, first_brand, rule: str) -> tuple:
    """(span brand, the result deciding its confidence): "deployed" keeps
    the first brand named and its most confident window; "both" takes the
    most confident window naming anything (ties: the earliest)."""
    if rule == "deployed":
        return first_brand, _lead(results, first_brand)
    named = [r for r in results if r["brand"] is not None]
    lead = max(named, key=lambda r: r["confidence"]) if named else results[0]
    return lead["brand"], lead


def _check_rule(rule: str) -> None:
    if rule not in MERGE_RULES:
        raise ValueError(f"merge rule must be one of {MERGE_RULES}, not {rule!r}")


def _second_brands(brand, candidates: list, windows: list, min_score) -> list:
    """Further brands of one span (ad pods): candidates other than the
    span's brand (any rank: under "both" the span's brand need not rank
    first) spoken as a domain with score >= min_score, no sound-alike of
    a brand already named for the span. Each keeps its own range when
    derivable: the span's windows whose own text speaks its domain
    (`windows`: [(window, result)]), else the whole span."""
    if min_score is None or brand is None:
        return []
    seen, out = [compact(brand)], []
    for rank, c in enumerate(candidates, 1):
        if "domain" not in c["evidence"] or c["score"] < min_score:
            continue
        ck = compact(c["brand"])
        if any(_same_brand(c["brand"], s) or jaro_winkler(ck, s) >= 0.85 for s in seen):
            continue
        seen.append(ck)
        k = _key(c["brand"])
        own = [w for w, res in windows if any(
            _key(x["brand"]) == k and "domain" in x["evidence"] for x in res["candidates"])] \
            or [w for w, _ in windows]
        out.append({"brand": c["brand"], "score": c["score"], "evidence": list(c["evidence"]),
                    "rank": rank, "start_s": min(w["start_s"] for w in own),
                    "end_s": max(w["end_s"] for w in own),
                    "jev_window_ids": [w["window_id"] for w in own]})
    return out


def name_windows(segments, windows, namer: BrandNamer, *, threshold: float = 0.8,
                 context_windows: int = 1, merge_rule: str = DEFAULT_MERGE_RULE,
                 second_brand_min_score: float | None = SECOND_BRAND_MIN_SCORE,
                 max_span_windows: int | None = MAX_SPAN_WINDOWS,
                 sub_window_s: float | None = None,
                 sub_window_pad_s: float = SUB_WINDOW_PAD_S,
                 sub_window_gap_s: float = SUB_WINDOW_GAP_S,
                 pinned: dict | None = None) -> list:
    """Ad spans from Jev windows: each window at noul >= threshold is named
    on its own text widened by `context_windows` windows on each side (60 s
    windows cut URLs off), then consecutive flagged windows merge into one
    span by `merge_rule` (MERGE_RULES; back-to-back spots with distinct
    confident names stay apart; "both" joins other than on the same brand
    stop at `max_span_windows`). A window naming nothing joins its
    neighbour. A span's time range covers only its flagged windows; the
    context is for naming. Each span lists `second_brands` (ad pods; empty
    when none or `second_brand_min_score` is None).

    `sub_window_s` (off by default): each run of consecutive flagged
    windows is named in sub-windows instead (see _name_sub_windows), one
    span per distinct brand with its own range and no second brands; a run
    where no sub-window names a brand confidently is named window by window
    as above (merge rule, cap and second brands apply there).

    `pinned` ({window_id: group}): those windows are named with no
    operator rules (BrandNamer.without_rules) and form exactly their
    group's span, joining no other window (name_episode: a span a person
    confirmed). A run holding a pinned window is never cut in sub-windows."""
    _check_rule(merge_rule)
    ws = list(windows or ())
    pinned = pinned or {}
    flagged = [i for i, w in enumerate(ws) if w["noul"] >= threshold]
    if not sub_window_s:
        return _name_whole_windows(segments, ws, flagged, namer, context_windows,
                                   merge_rule, second_brand_min_score, max_span_windows,
                                   pinned)
    out: list = []
    for region in _runs_of_consecutive(flagged):
        subs = [] if any(ws[i]["window_id"] in pinned for i in region) else \
            _name_sub_windows(segments, ws, region, namer, sub_window_s,
                              sub_window_pad_s, sub_window_gap_s)
        out += subs or _name_whole_windows(segments, ws, region, namer, context_windows,
                                           merge_rule, second_brand_min_score,
                                           max_span_windows, pinned)
    return out


def _runs_of_consecutive(idx: list) -> list:
    runs: list = []
    for i in idx:
        if runs and runs[-1][-1] == i - 1:
            runs[-1].append(i)
        else:
            runs.append([i])
    return runs


def _name_whole_windows(segments, ws: list, flagged: list, namer: BrandNamer,
                        context_windows: int, merge_rule: str, second_brand_min_score,
                        max_span_windows, pinned: dict) -> list:
    plain = without_rules(namer) if pinned else namer
    named = []
    for i in flagged:
        w = ws[i]
        lo = ws[max(0, i - context_windows)]["start_s"]
        hi = ws[min(len(ws) - 1, i + context_windows)]["end_s"]
        res = (plain if w["window_id"] in pinned else namer).name_span(
            _text_between(segments, lo, hi))
        named.append((i, w, res, (lo, hi)))
    spans: list = []
    for i, w, res, ctx in named:
        cur = spans[-1] if spans else None
        group = pinned.get(w["window_id"])
        if cur and cur["_last"] == i - 1 and (
                group == cur["_group"] if group is not None or cur["_group"] is not None
                else _joins(cur["_results"], res, cur["brand"], merge_rule,
                            max_span_windows)):
            cur["_results"].append(res)
            cur["_windows"].append((w, res))
            cur["_last"] = i
            cur["end_s"] = w["end_s"]
            cur["jev_window_ids"].append(w["window_id"])
            cur["noul_max"] = max(cur["noul_max"], w["noul"])
            cur["context_s"][1] = ctx[1]
            if cur["brand"] is None:
                cur["brand"] = res["brand"]
        else:
            spans.append({"start_s": w["start_s"], "end_s": w["end_s"], "brand": res["brand"],
                          "jev_window_ids": [w["window_id"]], "noul_max": w["noul"],
                          "context_s": list(ctx), "_results": [res], "_windows": [(w, res)],
                          "_last": i, "_group": group})
    out = []
    for sp in spans:
        results = sp.pop("_results")
        wins = sp.pop("_windows")
        sp.pop("_last")
        sp.pop("_group")
        brand, lead = _span_lead(results, sp["brand"], merge_rule)
        cands = _merge_candidates(results)
        # a neighbouring span's brand may be a second brand too: the brand
        # can be read again later in this span (dev s001e0: Kachava 605-625 s
        # names the 600-660 span; its second, 8-s read at 705-714 s is found
        # only as a second brand of the She's Birdie span 660-720). The cost:
        # a read split as A + unsure b | B also yields b as A's second brand.
        seconds = _second_brands(brand, cands, wins, second_brand_min_score)
        out.append({"span_id": f"{round(sp['start_s'] * 1000)}-{round(sp['end_s'] * 1000)}",
                    "start_s": sp["start_s"], "end_s": sp["end_s"], "brand": brand,
                    "candidates": cands,
                    "confidence": lead["confidence"], "needs_review": lead["needs_review"],
                    "method": lead["method"], "jev_window_ids": sp["jev_window_ids"],
                    "noul_max": sp["noul_max"], "context_s": sp["context_s"],
                    "second_brands": seconds})
    return out


def opener_times(segments, lo: float, hi: float) -> list:
    """Sorted times in (lo + 3, hi - 3) where a strong read opener ("brought
    to you by", "sponsored by", ...) starts, interpolated within its
    segment by character position."""
    out = set()
    for s in _text_between(segments, lo, hi):
        text = str(s.get("text") or "")
        for m in OPENER_RE.finditer(text):
            t = s["start"] + (s["end"] - s["start"]) * m.start() / max(1, len(text))
            if lo + 3 < t < hi - 3:
                out.add(round(t, 2))
    return sorted(out)


def _sub_windows(lo: float, hi: float, cuts: list, size: float) -> list:
    """[(a, b)]: [lo, hi] cut at `cuts`, each piece split into `size`-s
    sub-windows (a remainder under size / 2 joins the last one)."""
    edges, subs = [lo, *cuts, hi], []
    for p0, p1 in zip(edges, edges[1:]):
        t = p0
        while t < p1 - 1e-6:
            e = min(t + size, p1)
            if p1 - e < size / 2:
                e = p1
            subs.append((t, e))
            t = e
    return subs


def _name_sub_windows(segments, ws: list, region: list, namer: BrandNamer, size: float,
                      pad: float, gap: float) -> list:
    """Spans of one run of flagged windows named in sub-windows: the run is
    cut where a read opener starts and into `size`-s pieces, each named on
    its own text plus `pad` s either side (none across an opener); a
    confident (not needs_review) name labels its sub-window. Consecutive
    labels of one brand merge into one span, bridging at most `gap` s of
    unlabeled sub-windows. The spans then partition the run: unlabeled time
    before the first span or after the last joins it, and unlabeled time
    between two spans is split at the sub-window edge nearest its middle
    (dropping it cut long reads that say their brand only at the start or
    end below half coverage). Spans carry sub_window: true and context_s =
    the text they were named on. [] when no sub-window is labeled."""
    lo, hi = ws[region[0]]["start_s"], ws[region[-1]]["end_s"]
    cuts = opener_times(segments, lo, hi)
    cut = set(cuts)
    runs: list = []
    subs = _sub_windows(lo, hi, cuts, size)
    for a, b in subs:
        ta = a if a in cut else max(0.0, a - pad)
        tb = b if b in cut else b + pad
        res = namer.name_span(_text_between(segments, ta, tb))
        if res["brand"] is None or res["needs_review"] \
                or str(res["brand"]).lower() in SUB_WINDOW_STOP:
            continue
        cur = runs[-1] if runs else None
        if cur and _same_brand(cur["brand"], res["brand"]) and a - cur["end_s"] <= gap:
            cur["end_s"], cur["context_s"][1] = b, tb
            cur["_results"].append(res)
        else:
            runs.append({"start_s": a, "end_s": b, "brand": res["brand"],
                         "context_s": [ta, tb], "_results": [res]})
    edges = sorted({e for ab in subs for e in ab})

    def split(x0, x1):
        return min((e for e in edges if x0 <= e <= x1), key=lambda e: abs(e - (x0 + x1) / 2))
    bounds = [lo] + [split(p["end_s"], n["start_s"]) for p, n in zip(runs, runs[1:])] + [hi]
    out = []
    for r, a, b in zip(runs, bounds, bounds[1:]):
        r["start_s"], r["end_s"] = a, b
        results = r.pop("_results")
        lead = _lead(results, r["brand"])
        inside = [ws[i] for i in region
                  if ws[i]["end_s"] > r["start_s"] and ws[i]["start_s"] < r["end_s"]]
        out.append({"span_id": f"{round(r['start_s'] * 1000)}-{round(r['end_s'] * 1000)}",
                    "start_s": r["start_s"], "end_s": r["end_s"], "brand": r["brand"],
                    "candidates": _merge_candidates(results),
                    "confidence": lead["confidence"], "needs_review": lead["needs_review"],
                    "method": lead["method"],
                    "jev_window_ids": [w["window_id"] for w in inside],
                    "noul_max": max(w["noul"] for w in inside),
                    "context_s": r["context_s"], "sub_window": True, "second_brands": []})
    return out


def rename_span(span: dict, segments, windows, namer: BrandNamer, *,
                context_windows: int = 0, merge_rule: str = DEFAULT_MERGE_RULE,
                second_brand_min_score: float | None = SECOND_BRAND_MIN_SCORE) -> dict:
    """Re-name an existing span on (other) `segments`, keeping its time
    range, id and Jev windows: each of its windows is named on its own text
    plus `context_windows` neighbours, as name_windows does; the span's
    brand and confidence follow `merge_rule` (_span_lead), and its
    second_brands are re-derived. Without its Jev windows, the span's
    recorded context_s (else its own range) is named once; so is a
    sub-window span (its Jev windows may hold other reads), which keeps
    no second brands (its neighbours carry their own spans)."""
    _check_rule(merge_rule)
    ws = list(windows or ())
    pos = {w["window_id"]: i for i, w in enumerate(ws)}
    own = [] if span.get("sub_window") else [
        ws[pos[wid]] for wid in span.get("jev_window_ids") or () if wid in pos]
    ranges = [(ws[max(0, pos[w["window_id"]] - context_windows)]["start_s"],
               ws[min(len(ws) - 1, pos[w["window_id"]] + context_windows)]["end_s"])
              for w in own]
    if not ranges:
        ctx = span.get("context_s") or (span["start_s"], span["end_s"])
        ranges = [(float(ctx[0]), float(ctx[1]))]
        own = [{"window_id": None, "start_s": span["start_s"], "end_s": span["end_s"]}]
    results = [namer.name_span(_text_between(segments, lo, hi)) for lo, hi in ranges]
    first = next((r["brand"] for r in results if r["brand"] is not None), None)
    brand, lead = _span_lead(results, first, merge_rule)
    cands = _merge_candidates(results)
    seconds = [] if span.get("sub_window") else _second_brands(
        brand, cands, list(zip(own, results)), second_brand_min_score)
    for sb in seconds:
        if sb["jev_window_ids"] == [None]:
            sb["jev_window_ids"] = list(span.get("jev_window_ids") or ())
    return {**span, "brand": brand, "candidates": cands,
            "confidence": lead["confidence"], "needs_review": lead["needs_review"],
            "method": lead["method"], "second_brands": seconds}


def name_episode(episode_id: str, segments, jev_doc, namer: BrandNamer, *,
                 threshold: float = 0.8, context_windows: int = 1,
                 merge_rule: str = DEFAULT_MERGE_RULE,
                 second_brand_min_score: float | None = SECOND_BRAND_MIN_SCORE,
                 max_span_windows: int | None = MAX_SPAN_WINDOWS,
                 sub_window_s: float | None = None) -> dict | None:
    """The names/<episode_id>.json document; None when `jev_doc` is not a
    Jev result. sub_window_s is recorded only when sub-window naming is on
    (documents named without it stay byte-identical)."""
    ws = jev_windows(jev_doc)
    if ws is None:
        return None
    kw = dict(threshold=threshold, context_windows=context_windows, merge_rule=merge_rule,
              second_brand_min_score=second_brand_min_score, max_span_windows=max_span_windows,
              sub_window_s=sub_window_s)
    # A span a person confirmed (protected_spans) is named as without the
    # operator's rules and kept whole: its windows are pinned to that span.
    keep = protected_spans(namer, episode_id)
    pinned = {wid: sp["span_id"] for sp in name_windows(segments, ws, without_rules(namer), **kw)
              if sp["span_id"] in keep for wid in sp["jev_window_ids"]} if keep else None
    return {"episode_id": episode_id, "jev_model": jev_doc.get("model"),
            "jev_threshold": threshold, "context_windows": context_windows,
            "merge_rule": merge_rule, "second_brand_min_score": second_brand_min_score,
            "max_span_windows": max_span_windows,
            **({"sub_window_s": sub_window_s} if sub_window_s else {}),
            **describe(namer, episode_id),
            "spans": name_windows(segments, ws, namer, pinned=pinned, **kw)}


def describe(namer, episode_id) -> dict:
    """namer.describe() for one episode's names doc (BrandNamer: with that
    episode's namer digest)."""
    return namer.describe(episode_id) if isinstance(namer, BrandNamer) else namer.describe()


def protected_spans(namer, episode_id) -> frozenset:
    """Span ids of `episode_id` the namer's stoplist never touches
    (corrections.Corrections.protected_spans; none without corrections)."""
    corr = getattr(namer, "corrections", None)
    return corr.protected_spans(episode_id) if corr is not None else frozenset()


def without_rules(namer):
    """`namer` with no operator rules (BrandNamer.without_rules), or as it is."""
    return namer.without_rules() if hasattr(namer, "without_rules") else namer


def _bucket(span: dict) -> str:
    if span.get("needs_review") or not span.get("brand"):
        return "low"
    return "high" if span.get("method") == "cta" else "medium"


UNNAMED_PLACEHOLDER = "(unnamed)"   # what older exports called a read with no name


FORMAT_ATTRS = ("read_format", "promo")


def _format_attrs(obj: dict) -> dict:
    """A span's (or second brand's) read_format / promo, when it has them."""
    return {k: obj[k] for k in FORMAT_ATTRS if obj.get(k)}


def named_candidates(ep_id: str, names_doc: dict, segments=None, meta: dict | None = None,
                     *, confident_only: bool = False) -> list:
    """Named spans as candidate records in candidates_normalized.json's
    shape ("source": "jev+namer", brand gazetteer-normalized), so
    score_labels.py and the seed importer read them like detector output.
    A span with no name has brand None (never a placeholder brand such as
    "(unnamed)"); norm is None for an empty or unnormalizable name too.

    Each of a span's second_brands (ad pods) follows its span as one more
    record of the same shape: id "<span candidate id>.b-<brand key>" (keyed
    on the brand, so a review never carries over to a different second
    brand), its own range,
    the span's review flag and confidence, plus "second_brand_of" (the
    span's brand) and provenance.second_brand. Spans without second_brands
    (names docs from before them) export exactly as before."""
    meta, out = meta or {}, []

    def excerpt_of(lo, hi):
        return " ".join(str(s.get("text", "")).strip() for s in segments or ()
                        if s.get("start", 0) < hi and s.get("end", 0) > lo)

    for sp in (names_doc or {}).get("spans") or []:
        if confident_only and sp.get("needs_review"):
            continue
        norm = normalize_brand(sp.get("brand")) if sp.get("brand") else None
        if norm in ("", UNNAMED_PLACEHOLDER):   # a names doc written with the old placeholder
            norm = None
        excerpt = excerpt_of(sp["start_s"], sp["end_s"])
        first = len(out)
        out.append({
            "candidate_id": f"{ep_id}.named.{sp['span_id']}", "ep_id": ep_id,
            "show_title": meta.get("show_title", "?"), "ep_title": meta.get("ep_title", "?"),
            "pub_date": meta.get("pub_date", "?"), "fetch_date": meta.get("fetch_date"),
            "brand": norm, "brand_spoken": norm, "brand_raw": sp.get("brand"),
            "brand_url": None, "brand_code": None, "anchor": f"namer:{sp.get('method')}",
            "confidence": _bucket(sp), "confidence_score": sp.get("confidence"),
            "needs_review": bool(sp.get("needs_review")),
            "offer_codes": [], "urls": [], "start_s": sp["start_s"], "end_s": sp["end_s"],
            "provenance": {"pipeline": names_doc.get("pipeline"),
                           # refined names only: unrefined exports stay byte-identical
                           **({"refine_model": names_doc.get("refine_model"),
                               "text_source": sp.get("text_source"),
                               "previous_brand": sp.get("previous_brand"),
                               "accepted": sp.get("accepted"), "rule": sp.get("rule"),
                               "refined_candidate": (sp.get("refined_candidate") or {}).get(
                                   "brand")}
                              if names_doc.get("refined") else {}),
                           "jev_window_ids": sp.get("jev_window_ids"),
                           "jev_threshold": names_doc.get("jev_threshold"),
                           "context_windows": names_doc.get("context_windows"),
                           "context_s": sp.get("context_s"), "noul_max": sp.get("noul_max"),
                           "candidates": sp.get("candidates")},
            "excerpt": excerpt[:1000], "source": "jev+namer",
            # round 3 (cue spans, network promos): only on spans that carry them
            **_format_attrs(sp)})
        prim = out[first]
        plain = {k: v for k, v in prim.items() if k not in FORMAT_ATTRS}
        for sb in sp.get("second_brands") or ():
            norm2 = normalize_brand(sb["brand"])
            lo, hi = sb.get("start_s", sp["start_s"]), sb.get("end_s", sp["end_s"])
            out.append({**plain, **_format_attrs(sb),
                        "candidate_id": f"{prim['candidate_id']}.b-{_key(sb['brand'])}",
                        "brand": norm2, "brand_spoken": norm2,
                        "brand_raw": sb["brand"], "start_s": lo, "end_s": hi,
                        "second_brand_of": sp.get("brand"),
                        "provenance": {**prim["provenance"], "second_brand": {
                            "of": sp.get("brand"), "rank": sb.get("rank"),
                            "score": sb.get("score"), "evidence": sb.get("evidence"),
                            "jev_window_ids": sb.get("jev_window_ids")}},
                        "excerpt": excerpt_of(lo, hi)[:1000]})
    return out
