"""Corpus-wide brand canonicalization for named ad reads.

The brand namer names each span from one window of one transcript, so one
brand comes out under several spellings ("apple card" / "applecard") and
as speech-to-text garbles ("zipracruter", "growtharopy"). This module maps
each raw brand key onto one canonical key using evidence from the whole
corpus, and writes a reviewable map with a reason per entry.

Evidence per form (a form is the compact key: letters and digits only):
- name support: episodes and shows whose named spans gave it;
- domain support: episodes and shows whose transcript speaks it as a domain
  ("ziprecruiter dot com", "zock.app"), harvested as the namer's brand list
  harvests domains. Without transcripts, the brand list's episode weight
  stands in (shows unknown: one per episode);
- mention support: episodes and shows whose transcript writes it as a
  capitalized run of 1-3 tokens ("LinkedIn", "Grow Therapy"). A single
  token that is a lowercase dictionary word ("Cloud", "Whole") is not a
  mention: sentence starts and common words are no brand evidence.
- the curated gazetteer canonicals.

Rules (constants below; copied into the map file's "rules"):
1. Spacing/punctuation variants (same form) always merge.
2. A named form F merges onto a target form T only when ALL of these hold:
   a. T is an anchor: a gazetteer canonical or a spoken domain (>= 1
      episode). Mentions add support but never make an anchor: on the
      pilot, mention anchors pulled garbles onto person names and common
      words ("shanko" -> "shannon", "hers" -> "heres");
   b. F and T sound alike (`similar`; no Double Metaphone in the stdlib
      port, so single metaphone + Jaro-Winkler + edit distance);
   c. T dominates F: support(T) >= 1.5 x support(F) and >= support(F) + 2,
      where support = distinct shows naming it, speaking it as a domain or
      mentioning it;
   d. F is not protected: a gazetteer canonical, or a form spoken as a
      domain whose own shows (naming it or speaking its domain) number >=
      PROTECT_SHOWS, is a distinct brand in its own right and never merges
      onto another brand however similar ("visibly" heard as a domain and
      named on 3 shows stays apart from "visible");
   e. a plural/singular pair (caesar/caesars) merges only when F has no
      domain of its own ("rituals.com" keeps Rituals apart from Ritual).
   Similar pairs that fail only (c) or (d) are listed in "rejected".
3. Merges chain to the root (dominance strictly increases support, so no
   cycles). A root's canonical key is its gazetteer canonical, else its
   spelling named in the most episodes; ties go to the spacing its
   capitalized transcript mentions use most ("FreshBooks" vs "Liberty
   Mutual"), then spaced, then alphabetical.

Canonical keys stay lowercase and gazetteer-normalized
(`gazetteer.normalize_brand`); pretty display names are out of scope.
"""
from __future__ import annotations

import collections
import json
import os
import re
import sqlite3
from pathlib import Path

from earwitness.gazetteer import GAZETTEER, normalize_brand
from earwitness.pipeline import brand_namer as bn

CANON_NAME = "brand-canon.json"
CANON_VERSION = 1
MIN_PHONETIC_LEN = 4      # shorter forms merge only on exact form equality
LONG_LEN = 6              # forms this long use the long-form tests
EDIT_LEN = 8              # plain edit distance <= 2 only for forms this long
DOMINANCE_RATIO = 1.5
DOMINANCE_MARGIN = 2
PROTECT_SHOWS = 3
MENTION_MAX_TOKENS = 3

RULES = {
    "spacing": "same letters and digits (spacing/punctuation variant): always merged",
    "similar": "plural (x / xs, 4+ chars); 4-5 chars: same metaphone, edit <= 2, JW >= 0.85; "
               "6+ chars: JW >= 0.9 and edit <= max(2, 0.35 x len), or same metaphone "
               "(4+ symbols) and edit <= max(2, 0.4 x len), or edit <= 2 with the same "
               f"first letter ({EDIT_LEN}+ chars), or "
               "metaphone keys (5+ symbols) one edit apart and JW >= 0.85; < 4 chars: never",
    "anchor": "target is a gazetteer canonical or a spoken domain (>= 1 episode); "
              "mentions add support, never anchor",
    "dominance": f"support(target) >= {DOMINANCE_RATIO:g} x support(form) and >= "
                 f"support(form) + {DOMINANCE_MARGIN}; support = distinct shows naming it, "
                 "speaking it as a domain or mentioning it",
    "protected": "a gazetteer canonical, or a spoken domain whose own shows (naming it or "
                 f"speaking its domain) number >= {PROTECT_SHOWS}, is a distinct brand: never "
                 "merged onto another",
    "plural": "x / xs merge only when the merged form has no spoken domain of its own",
    "canonical": "gazetteer canonical, else the spelling named in the most episodes "
                 "(ties: the spacing most capitalized transcript mentions use, then "
                 "spaced, then alphabetical)",
}

_GAZ_COMPACT = {bn.compact(c): c for c in GAZETTEER}
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9&'-]*|[^\sA-Za-z0-9]")  # punctuation ends a run
_WRITTEN_DOMAIN = re.compile(r"[A-Za-z0-9&'-]+\." + bn.TLD + r"\b\S*")


def brand_key(raw) -> str | None:
    """The map key of a raw brand: gazetteer-normalized lowercase, as the
    export writes `brand`."""
    if not raw or not str(raw).strip():
        return None
    return normalize_brand(str(raw))


def load_common_words(path=bn.WORDS_PATH) -> frozenset:
    """Lowercase entries of the system word list (common words, not proper
    names: web2 lists "Claude" capitalized, "cloud" lowercase)."""
    try:
        return frozenset(w.strip() for w in open(path, encoding="utf-8")
                         if w.strip() and w.strip().islower())
    except OSError:
        return frozenset()


# ---------------------------------------------------------------- similarity

def similar(a: str, b: str) -> str | None:
    """Why forms `a` and `b` sound like one brand, or None."""
    if a == b:
        return "spacing"
    la, lb = len(a), len(b)
    short = min(la, lb)
    if short < MIN_PHONETIC_LEN:
        return None
    if a + "s" == b or b + "s" == a:
        return "plural"
    if abs(la - lb) > max(3, short // 3):
        return None
    ma, mb = bn._metaphone_cached(a), bn._metaphone_cached(b)
    jw = bn.jaro_winkler(a, b)
    lev = bn.levenshtein(a, b)
    if short < LONG_LEN:
        return "phonetic-short" if ma == mb and lev <= 2 and jw >= 0.85 else None
    if jw >= 0.9 and lev <= max(2, round(0.35 * short)):
        return "jaro-winkler"
    if ma == mb and len(ma) >= 4 and lev <= max(2, short * 2 // 5):
        return "phonetic"
    if short >= EDIT_LEN and lev <= 2 and a[0] == b[0]:
        return "edit"
    if jw >= 0.85 and min(len(ma), len(mb)) >= 5 and bn.levenshtein(ma, mb) <= 1:
        return "phonetic-near"
    return None


# ---------------------------------------------------------------- evidence

class TranscriptIndex:
    """Lazy {episode_id: (show, text)} over transcript JSON files: items()
    reads one file at a time, so a large corpus never sits in memory."""

    def __init__(self, paths, shows: dict | None = None, exclude=()):
        self.paths = sorted(str(p) for p in paths)
        self.shows = shows or {}
        self.exclude = set(exclude)

    def items(self):
        for p in self.paths:
            ep = Path(p).stem
            if ep in self.exclude:
                continue
            try:
                doc = json.loads(Path(p).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            segs = doc.get("segments") if isinstance(doc, dict) else None
            text = "\n".join(str(s.get("text") or "") for s in segs or () if isinstance(s, dict))
            yield ep, (self.shows.get(ep) or ep, text)


def _mentions(line: str, common: frozenset):
    """(compact form, written as several tokens) of capitalized runs of
    1-3 tokens in `line`."""
    run: list = []
    for tok in _TOKEN.findall(_WRITTEN_DOMAIN.sub(" . ", line)) + [""]:
        if tok[:1].isupper() or tok[:1].isdigit():
            run.append(tok)
            continue
        for i in range(len(run)):
            for n in range(1, MENTION_MAX_TOKENS + 1):
                if i + n > len(run):
                    break
                if n == 1 and run[i].lower().split("'")[0] in common:
                    continue
                f = bn.compact("".join(run[i:i + n]))
                if len(f) >= MIN_PHONETIC_LEN:
                    yield f, n > 1
        run = []


def corpus_evidence(text_index, common: frozenset = frozenset()) -> tuple:
    """({form: (episodes, shows)} of spoken domains, {form: (episodes,
    shows, [unspaced, spaced] mention counts)} of proper-noun mentions)
    over every transcript of `text_index`."""
    dom_e, dom_s = collections.defaultdict(set), collections.defaultdict(set)
    men_e, men_s = collections.defaultdict(set), collections.defaultdict(set)
    men_sp = collections.defaultdict(lambda: [0, 0])
    for ep, val in text_index.items():
        show, text = val if isinstance(val, tuple) else (ep, val)
        show = show or ep
        for line in text.splitlines():
            if bn._TLD_HINT.search(line):
                for m in bn.DOMAIN_RE.finditer(line):
                    for lab in bn.domain_labels(m.group(1)):
                        dom_e[lab].add(ep)
                        dom_s[lab].add(show)
            for f, spaced in _mentions(line, common):
                men_e[f].add(ep)
                men_s[f].add(show)
                men_sp[f][spaced] += 1
    return ({k: (dom_e[k], dom_s[k]) for k in dom_e},
            {k: (men_e[k], men_s[k], men_sp[k]) for k in men_e})


class _Form:
    __slots__ = ("form", "spellings", "name_eps", "name_shows", "dom_eps", "dom_shows",
                 "men_eps", "men_shows", "men_spacing", "gazetteer")

    def __init__(self, form):
        self.form = form
        self.spellings = collections.defaultdict(set)  # key -> episodes
        self.name_eps, self.name_shows = set(), set()
        self.dom_eps, self.dom_shows = set(), set()
        self.men_eps, self.men_shows = set(), set()
        self.men_spacing = [0, 0]  # capitalized mentions written [unspaced, spaced]
        self.gazetteer = _GAZ_COMPACT.get(form)

    @property
    def support(self) -> int:
        return len(self.name_shows | self.dom_shows | self.men_shows)

    @property
    def anchor(self) -> bool:
        return self.gazetteer is not None or len(self.dom_eps) >= 1

    @property
    def protected(self) -> bool:
        return self.gazetteer is not None or (
            bool(self.dom_eps) and len(self.name_shows | self.dom_shows) >= PROTECT_SHOWS)

    def spelling(self) -> str:
        if self.gazetteer:
            return self.gazetteer
        if not self.spellings:
            return self.form
        spaced = self.men_spacing[1] >= self.men_spacing[0]
        return min(self.spellings, key=lambda k: (-len(self.spellings[k]),
                                                  (" " in k) != spaced, k))

    def evidence(self) -> dict:
        return {"support_shows": self.support,
                "name_episodes": len(self.name_eps), "name_shows": len(self.name_shows),
                "domain_episodes": len(self.dom_eps), "domain_shows": len(self.dom_shows),
                "mention_episodes": len(self.men_eps), "mention_shows": len(self.men_shows),
                "gazetteer": self.gazetteer is not None}


def _span_fields(sp: dict) -> tuple:
    ep = sp.get("episode_id") or sp.get("ep_id")
    show = sp.get("show") or sp.get("show_title") or ep
    raw = sp.get("brand_raw") if "brand_raw" in sp else sp.get("brand")
    return ep, show, raw


# ---------------------------------------------------------------- build

def build_canon(named_spans, brand_list=None, transcripts_text_index=None, *,
                common_words: frozenset | None = None, corrections=None) -> dict:
    """The reviewable canon document: {"version", "rules", "params",
    "keys", "merged_keys", "map": {key: {"canonical", "reason", "support",
    "canonical_support"}}, "rejected": [...]}.

    `named_spans`: dicts with episode_id/ep_id, show/show_title and the
    namer's raw brand (brand_raw, else brand). `brand_list`: a harvested
    brand list ([{"variant", "brand", "weight"}]); its weights are the
    domain support only when no transcripts are given.
    `transcripts_text_index`: {episode_id: (show, text) or text}, e.g.
    TranscriptIndex. `common_words`: lowercase dictionary words (default:
    the system word list). `corrections` (corrections.Corrections): the
    operator's alias rules over every merge (Corrections.apply_to_canon),
    recorded under "corrections"; None leaves the document as without."""
    common = load_common_words() if common_words is None else common_words
    forms: dict = {}

    def form(f):
        if f not in forms:
            forms[f] = _Form(f)
        return forms[f]

    n_spans = 0
    for sp in named_spans or ():
        ep, show, raw = _span_fields(sp)
        key = brand_key(raw)
        f = bn.compact(key or "")
        if not f or key == "(unnamed)":
            continue
        n_spans += 1
        fm = form(f)
        fm.spellings[key].add(ep)
        fm.name_eps.add(ep)
        fm.name_shows.add(show)
    if transcripts_text_index is not None:
        dom, men = corpus_evidence(transcripts_text_index, common)
        for lab, (eps, shows) in dom.items():
            if lab in forms or len(lab) >= MIN_PHONETIC_LEN:
                fm = form(lab)
                fm.dom_eps, fm.dom_shows = eps, shows
        for f, (eps, shows, spacing) in men.items():
            if f in forms:  # support only: mentions never make an anchor
                forms[f].men_eps, forms[f].men_shows, forms[f].men_spacing = eps, shows, spacing
    else:
        for e in brand_list or ():
            w = float(e.get("weight") or 0)
            if 1 <= w < 1000:  # 1000 / 0.5 = gazetteer entries, not spoken domains
                fm = form(e["variant"])
                fm.dom_eps = fm.dom_shows = {("brand-list", i) for i in range(int(w))}
    for f in _GAZ_COMPACT:
        form(f)

    named = sorted((f for f in forms.values() if f.spellings), key=lambda x: x.form)
    blocks: dict = collections.defaultdict(list)
    for a in forms.values():
        if a.anchor:
            for k in {a.form[0], bn._metaphone_cached(a.form)[:1].lower()} - {""}:
                blocks[k].append(a)
    parent, why, rejected, seen_pairs = {}, {}, [], set()
    for f in named:
        keys = {f.form[0], bn._metaphone_cached(f.form)[:1].lower()} - {""}
        pool = {a.form: a for k in keys for a in blocks.get(k, ())}
        best = None
        for t in pool.values():
            if t is f or abs(len(t.form) - len(f.form)) > max(3, len(f.form) // 2):
                continue
            reason = similar(f.form, t.form)
            if reason is None or (reason == "plural" and f.dom_eps):
                continue
            dominant = (t.support >= DOMINANCE_RATIO * f.support
                        and t.support >= f.support + DOMINANCE_MARGIN)
            if not dominant or f.protected:
                pair = tuple(sorted((f.form, t.form)))
                if t.support >= f.support and pair not in seen_pairs:
                    seen_pairs.add(pair)
                    rejected.append({"form": f.spelling(), "target": t.spelling(),
                                     "similar": reason,
                                     "rule": "protected" if dominant else "dominance",
                                     "form_support": f.evidence(),
                                     "target_support": t.evidence()})
                continue
            rank = (bn.jaro_winkler(f.form, t.form), t.support, t.form)
            if best is None or rank > best[0]:
                best = (rank, t, reason)
        if best is not None:
            parent[f.form] = best[1].form
            why[f.form] = best[2]

    cmap = {}
    for f in named:
        chain = [f.form]
        while chain[-1] in parent:
            chain.append(parent[chain[-1]])
        rf = forms[chain[-1]]
        canonical = rf.spelling()
        for key in sorted(f.spellings):
            if len(chain) == 1:
                reason = "canonical" if key == canonical else "spacing"
            else:
                reason = " -> ".join(f"{why[c]}:{parent[c]}" for c in chain[:-1])
            cmap[key] = {"canonical": canonical, "reason": reason,
                         "support": f.evidence(), "canonical_support": rf.evidence()}
    rejected.sort(key=lambda r: (-r["form_support"]["name_episodes"], r["form"], r["target"]))
    doc = {"version": CANON_VERSION, "rules": RULES,
           "params": {"min_phonetic_len": MIN_PHONETIC_LEN, "long_len": LONG_LEN,
                      "edit_len": EDIT_LEN, "dominance_ratio": DOMINANCE_RATIO,
                      "dominance_margin": DOMINANCE_MARGIN,
                      "protect_shows": PROTECT_SHOWS,
                      "evidence": "transcripts" if transcripts_text_index is not None
                      else "brand_list"},
           "spans": n_spans, "keys": len(cmap),
           "canonical_keys": len({v["canonical"] for v in cmap.values()}),
           "merged_keys": sum(1 for k, v in cmap.items() if v["canonical"] != k),
           "map": cmap, "rejected": rejected}
    return corrections.apply_to_canon(doc) if corrections is not None else doc


def build_canon_map(named_spans, brand_list=None, transcripts_text_index=None,
                    **kw) -> dict:
    """{raw key: {"canonical", "reason", "support", ...}} (see build_canon)."""
    return build_canon(named_spans, brand_list, transcripts_text_index, **kw)["map"]


# ---------------------------------------------------------------- apply

def canonical_of(brand, cmap: dict) -> tuple:
    """(canonical key, map entry or None) for a brand string."""
    key = brand_key(brand)
    entry = cmap.get(key) if key else None
    return (entry["canonical"], entry) if entry else (key, None)


def apply_canon(candidates: list, canon) -> list:
    """Named-read candidates with `brand_canonical` and `canon_reason` added
    where the map moves brand_spoken to another key (absent = already
    canonical). brand, brand_spoken and brand_raw are never changed: they
    are evidence identity (import_sources._DETECTOR_IDENTITY_ATTRS), and
    rewriting them would re-key reviewed rows on re-seed. Idempotent."""
    cmap = (canon.get("map") if "map" in canon else canon) if isinstance(canon, dict) else {}
    out = []
    for c in candidates:
        b = c.get("brand_spoken")
        canonical, entry = canonical_of(b, cmap) if b else (None, None)
        if entry is None or canonical == b:
            out.append(c)
            continue
        if c.get("brand_canonical") == canonical and c.get("canon_reason") == entry["reason"]:
            out.append(c)
            continue
        out.append(dict(c, brand_canonical=canonical, canon_reason=entry["reason"]))
    return out


def write_canon(path, doc: dict) -> None:
    """Write the map atomically, indented for review."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_canon(path) -> dict | None:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and isinstance(doc.get("map"), dict) else None


# ---------------------------------------------------------------- corpus inputs

def corpus_shows(corpus) -> dict:
    """{episode_id: show_title} from <corpus>/queue.sqlite3 (empty if none)."""
    db = Path(corpus) / "queue.sqlite3"
    if not db.is_file():
        return {}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return dict(con.execute("SELECT episode_id, show_title FROM episodes").fetchall())
    finally:
        con.close()


def corpus_inputs(corpora) -> tuple:
    """(named spans, TranscriptIndex) from names/*.json and transcripts/*.json
    of one or several corpus dirs (a duplicate episode keeps the first)."""
    spans, paths, shows, named_eps = [], [], {}, set()
    for corpus in corpora:
        corpus = Path(corpus)
        sh = corpus_shows(corpus)
        for p in sorted((corpus / "names").glob("*.json")):
            if p.stem in named_eps:
                continue
            named_eps.add(p.stem)
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for sp in (doc.get("spans") if isinstance(doc, dict) else None) or ():
                # a span's second brands (ad pods) are named reads too
                for b in [sp.get("brand")] + [x.get("brand") for x in sp.get("second_brands")
                                               or () if isinstance(x, dict)]:
                    if b:
                        spans.append({"episode_id": p.stem, "show": sh.get(p.stem) or p.stem,
                                      "brand": b})
        for p in sorted((corpus / "transcripts").glob("*.json")):
            if p.stem not in shows:
                shows[p.stem] = sh.get(p.stem) or p.stem
                paths.append(p)
    return spans, TranscriptIndex(paths, shows)


def inputs_digest(corpora) -> str:
    """sha256 over what a map is built from: each names doc's bytes and each
    transcript's name and size, in corpus order (a duplicate episode keeps
    the first), so export can tell a map built from other inputs."""
    import hashlib
    h, names, trs = hashlib.sha256(), set(), set()
    for corpus in corpora:
        corpus = Path(corpus)
        for p in sorted((corpus / "names").glob("*.json")):
            if p.stem not in names:
                names.add(p.stem)
                h.update(f"n {p.stem} {hashlib.sha256(p.read_bytes()).hexdigest()}\n".encode())
        for p in sorted((corpus / "transcripts").glob("*.json")):
            if p.stem not in trs:
                trs.add(p.stem)
                h.update(f"t {p.stem} {p.stat().st_size}\n".encode())
    return h.hexdigest()


def is_stale(canon: dict, corpora) -> bool | None:
    """Was the map built from other inputs than `corpora` now hold? None
    when it records no inputs. A map built over more corpora than are
    exported (`inputs_sha256_each`, one digest per corpus) is current when
    each exported corpus is unchanged; a change in a corpus that is not
    exported is not seen."""
    if not canon.get("inputs_sha256"):
        return None
    if canon["inputs_sha256"] == inputs_digest(corpora):
        return False
    each = canon.get("inputs_sha256_each")
    if not isinstance(each, list) or len(each) <= len(list(corpora)):
        return True
    return any(inputs_digest([c]) not in each for c in corpora)


def summarize(doc: dict, top: int = 25) -> dict:
    """Counts and the merges covering the most named episodes."""
    merges = sorted(((v["support"]["name_episodes"], k, v["canonical"], v["reason"])
                     for k, v in doc["map"].items() if v["canonical"] != k),
                    key=lambda m: (-m[0], m[1]))
    return {"spans": doc.get("spans"), "keys": doc["keys"],
            "canonical_keys": doc["canonical_keys"], "merged_keys": doc["merged_keys"],
            "rejected": len(doc["rejected"]),
            "top_merges": [{"raw": k, "canonical": c, "reason": r, "name_episodes": n}
                           for n, k, c, r in merges[:top]]}
