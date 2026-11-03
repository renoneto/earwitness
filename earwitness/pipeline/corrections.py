"""Ad review corrections: the app's operator rules, read by the pipeline.

The operator works the app's Ad review page; every action becomes a rule
in the workspace, and `scripts/app_workspace.py corrections export --out
FILE` writes them as one JSON file (SETUP.md, "Ad review corrections
file"). This module is the pipeline's only reader of that file: a change
to the contract is an edit here. Every pipeline step takes it through an
explicit `--corrections FILE` option and is unchanged without it.

Format "earwitness-corrections/3". Versions 1 and 2 are refused with a
message to export again (version 2 had no reviewed_reads, so reads
reviewed on other pages were unknown; version 1 had no actors, times or
superseded rules); so is any other format, any unknown key and any value
of the wrong shape. Brand keys are lowercase, accent-folded, trimmed and
have inner whitespace collapsed (`key`, the app's ad_reads.brand_key).
Every rule carries created_at, actor and from_read (kept in `rule_meta`,
never applied).

- brand_aliases [{raw, canonical, display, ...}]: a read heard as `raw` is
  the brand `canonical`. Only the canon map and the export apply it: they
  give `raw` the canonical over any support-based merge (brand_canonical,
  canon_reason "corrections:alias"), never on a read whose brand a person
  confirmed. The namer never applies an alias (brand_spoken, candidates
  and scores stay as heard), and the export never rewrites brand_spoken.
- stoplist [{word, ...}]: never a brand. The namer drops it from the
  candidates and takes the next one (or no brand), with needs_review;
  never on the span of a named read whose brand a person confirmed
  (reviewed_reads with brands, `protected_spans`): that span is named as
  without the namer rules and never merged away. The export then gives
  each read of such a span that nobody confirmed the stoplist, as the
  app's stoplist_reads does (stop_kept_spans).
- new_brands [{brand, display, ...}]: brands the namer's brand list lacks;
  each joins the namer's repair list as one exact entry (by key; display
  is the app's). A new brand never takes over another spelling: a
  harvested sound-alike keeps its group, and a spelling off the list is
  repaired as without the rules.
- self_promo [{brand, network, exact_show, ...}]: a read whose canonical
  brand is `brand`, on the show titled `network` (exact_show) or a show
  whose title has the network words as whole words, case- and
  accent-insensitive (promo_match, as the app), is self-promotion, reason
  "network:<network>"; never a read a person reviewed (self_promo_network;
  applied by self_promo.apply_rules).
- not_ads [{record_key, evidence_id, episode_id, show_title, start_s,
  end_s, brand, created_at, actor}]: reads the operator marked not an ad;
  the export leaves them out. Matched by record key ("named:<candidate_id>");
  a named read that moved (a re-name changed its span) by episode, brand
  and overlapping span (drop_not_ads). A not-ad on a detector, Jev or LLM
  read judges that read only: it never drops a named read.
- superseded [{kind, key, value, ..., reason}]: rules removed, replaced or
  undone (reason "undone": the operator undid the action that made it).
  Never applied. The lists above hold only rules in force, and every step
  recomputes from the file, so re-running a step with a newer file undoes
  a removed rule's effect (names docs: `rename --corrections`).
- labels [..., rules, superseded, superseded_reason]: one labeled example
  per operator action; superseded ones (a later action on the read
  replaced it, the operator removed a rule it made, or he undid the
  action: superseded_reason "undone") are skipped
  (`write_labels`). Labels never say which reads were reviewed.
- reviewed_reads [{record_key, evidence_id, episode_id, show_title,
  start_s, end_s, judgment, brands, reviewed_at}]: every current read a
  person reviewed in the app, on any page. Matched by record key only.
  The alias and self-promo rules never touch a reviewed read (`reviewed`;
  aliases skip the ones with confirmed `brands`, `confirmed`). A read
  judged not_ad on any page is left out of the export like an Ad review
  not-ad (`not_ad_reads`), by record key only.

Digests (never exported_at, rule times or actors, so a re-export of the
same rules keeps them), one per consumer, so a stale-check notices only a
change that step reads:
- `namer_sha256`: the stoplist and new brands. Each names doc records its
  episode's digest (`namer_sha256_for`: these, plus the episode's spans
  the rules spare because a person confirmed a read in them), only when
  there are such rules (without them the namer is unchanged). A confirm
  stales only its own episode.
- `canon_sha256`: the aliases and the stoplist. The canon map records it
  (only when there are such rules).
- `sha256`: everything the export applies (the rules above, self-promo,
  not-ads, the reviewed and confirmed reads). The export report and the
  label files carry it. A review or a not-ad changes only this one.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from earwitness.gazetteer import normalize_brand

FORMAT = "earwitness-corrections/3"
OLD_FORMATS = {
    "earwitness-corrections/1": "no rule times, actors or superseded rules",
    "earwitness-corrections/2": "no reviewed_reads, so reads reviewed on other pages are "
                                "unknown; labels without rules or superseded_reason"}
KEYS = ("format", "exported_at", "brand_aliases", "stoplist", "self_promo", "not_ads",
        "new_brands", "superseded", "labels", "reviewed_reads")
RULE_META = ("created_at", "actor", "from_read")
RULE_KINDS = ("alias", "stoplist", "self_promo", "not_ad", "brand")
SUPERSEDED_REASONS = ("removed", "replaced", "undone")
ACTIONS = ("confirm", "pick", "type", "alias", "not_ad", "self_promo", "generic")
BRAND_ACTIONS = ("confirm", "pick", "type", "alias")   # labels: the answer is the brand
NOT_AD = "not_ad"                  # the review judgment a not-ad read carries
ALIAS_REASON = "corrections:alias"
SELF_PROMO_REASON = "network"      # self_promo_reason "network:<network>"
LABEL_SOURCE = "earwitness-corrections"
LABEL_READ_TYPE = "ad review"      # any value but "uncertain" counts in the denominator
MOVED_MIN_OVERLAP = 0.5            # share of the shorter span a moved read must overlap
_RECORD_KEY = re.compile(r"^named:(?P<ep>.+)\.named\.(?P<lo>\d+)-(?P<hi>\d+)(?P<rest>(?:\..*)?)$")


class CorrectionsError(ValueError):
    """The file is not a corrections file this pipeline can read."""


def fold(text) -> str:
    """Accent-insensitive case-folded text (the app's customer.normalize_token)."""
    text = str(text or "")
    if not text.isascii():
        text = "".join(ch for ch in unicodedata.normalize("NFKD", text)
                       if not unicodedata.combining(ch))
    return text.casefold()


def key(text) -> str:
    """A brand key as the app writes it (ad_reads.brand_key): folded,
    trimmed, inner whitespace collapsed."""
    return " ".join(fold(str(text or "")).split())


def _compact(text) -> str:
    return re.sub(r"[^a-z0-9]", "", fold(text))


# ---------------------------------------------------------------- reading

def _fail(where: str, why: str):
    raise CorrectionsError(f"corrections file: {where}: {why}")


def _list(doc, name, item_keys, optional=()):
    items = doc[name]
    if not isinstance(items, list):
        _fail(name, "must be a list")
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            _fail(f"{name}[{i}]", "must be an object")
        missing = [k for k in item_keys if k not in item and k not in optional]
        extra = sorted(set(item) - set(item_keys))
        if missing or extra:
            _fail(f"{name}[{i}]", f"missing {missing} / unknown {extra}" if missing and extra
                  else f"missing {missing}" if missing else f"unknown keys {extra}")
    return items


def _text(where, value, *, null=False):
    if value is None and null:
        return None
    if not isinstance(value, str) or not value.strip():
        _fail(where, "must be a non-empty string" + (" or null" if null else ""))
    return value


def _seconds(where, value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(where, "must be a number or null")
    return float(value)


def _meta(where, item, keys=RULE_META):
    """A rule's who / when / from-read (from_read may be null)."""
    _text(f"{where}.created_at", item["created_at"])
    _text(f"{where}.actor", item["actor"])
    if "from_read" in keys:
        _text(f"{where}.from_read", item["from_read"], null=True)
    return {k: item[k] for k in keys}


def parse(doc) -> "Corrections":
    """A Corrections from the decoded file (strict: CorrectionsError).
    Only the rules in force are kept: removed, replaced or undone rules
    (`superseded`) and superseded labels are validated, counted and
    otherwise ignored."""
    if not isinstance(doc, dict):
        _fail("top level", "must be an object")
    if doc.get("format") in OLD_FORMATS:
        _fail("format", f"{doc.get('format')!r} is an older version "
                        f"({OLD_FORMATS[doc['format']]}); export again from the current app "
                        f"({FORMAT!r})")
    if doc.get("format") != FORMAT:
        _fail("format", f"{doc.get('format')!r} is not {FORMAT!r} (this pipeline reads only "
                        "that version)")
    missing = [k for k in KEYS if k not in doc]
    extra = sorted(set(doc) - set(KEYS))
    if missing:
        _fail("top level", f"missing keys {missing}")
    if extra:
        _fail("top level", f"unknown keys {extra}")
    aliases, meta = {}, {}
    for i, a in enumerate(_list(doc, "brand_aliases", ("raw", "canonical", "display")
                                + RULE_META)):
        raw = key(_text(f"brand_aliases[{i}].raw", a["raw"]))
        canonical = key(_text(f"brand_aliases[{i}].canonical", a["canonical"]))
        display = _text(f"brand_aliases[{i}].display", a["display"], null=True)
        meta[("alias", raw)] = _meta(f"brand_aliases[{i}]", a)
        if raw != canonical:
            aliases[raw] = (canonical, display)
    stoplist = set()
    for i, w in enumerate(_list(doc, "stoplist", ("word",) + RULE_META)):
        word = key(_text(f"stoplist[{i}].word", w["word"]))
        meta[("stoplist", word)] = _meta(f"stoplist[{i}]", w)
        stoplist.add(word)
    new_brands = {}
    for i, b in enumerate(_list(doc, "new_brands", ("brand", "display") + RULE_META)):
        k = key(_text(f"new_brands[{i}].brand", b["brand"]))
        new_brands[k] = _text(f"new_brands[{i}].display", b["display"], null=True)
        meta[("brand", k)] = _meta(f"new_brands[{i}]", b)
    self_promo = []
    for i, r in enumerate(_list(doc, "self_promo", ("brand", "network", "exact_show")
                                + RULE_META)):
        if not isinstance(r["exact_show"], bool):
            _fail(f"self_promo[{i}].exact_show", "must be true or false")
        rule = (key(_text(f"self_promo[{i}].brand", r["brand"])),
                _text(f"self_promo[{i}].network", r["network"]).strip(), r["exact_show"])
        meta[("self_promo", rule)] = _meta(f"self_promo[{i}]", r)
        if rule not in self_promo:
            self_promo.append(rule)
    not_ads = []
    for i, n in enumerate(_list(doc, "not_ads", ("record_key", "evidence_id", "episode_id",
                                                 "show_title", "start_s", "end_s", "brand",
                                                 "created_at", "actor"))):
        where = f"not_ads[{i}]"
        not_ads.append({"record_key": _text(f"{where}.record_key", n["record_key"]),
                        "evidence_id": _text(f"{where}.evidence_id", n["evidence_id"], null=True),
                        "episode_id": _text(f"{where}.episode_id", n["episode_id"], null=True),
                        "show_title": _text(f"{where}.show_title", n["show_title"], null=True),
                        "start_s": _seconds(f"{where}.start_s", n["start_s"]),
                        "end_s": _seconds(f"{where}.end_s", n["end_s"]),
                        "brand": _text(f"{where}.brand", n["brand"], null=True),
                        **_meta(where, n, ("created_at", "actor"))})
    superseded = []
    for i, r in enumerate(_list(doc, "superseded", ("kind", "key", "value", "created_at",
                                                    "actor", "removed_at", "removed_by",
                                                    "reason"))):
        where = f"superseded[{i}]"
        if r["kind"] not in RULE_KINDS:
            _fail(f"{where}.kind", f"{r['kind']!r} is not one of {', '.join(RULE_KINDS)}")
        if r["reason"] not in SUPERSEDED_REASONS:
            _fail(f"{where}.reason", f"{r['reason']!r} is not one of "
                                     f"{', '.join(SUPERSEDED_REASONS)}")
        if not isinstance(r["value"], dict):
            _fail(f"{where}.value", "must be an object")
        for k in ("key", "created_at", "actor", "removed_at", "removed_by"):
            _text(f"{where}.{k}", r[k])
        superseded.append(dict(r))
    labels = []
    for i, lab in enumerate(_list(doc, "labels", (
            "record_key", "evidence_id", "episode_id", "show_title", "start_s", "end_s",
            "action", "answer", "raw_brand", "transcript", "at", "actor", "rules",
            "superseded", "superseded_reason"))):
        where = f"labels[{i}]"
        if lab["action"] not in ACTIONS:
            _fail(f"{where}.action", f"{lab['action']!r} is not one of {', '.join(ACTIONS)}")
        if not isinstance(lab["answer"], dict):
            _fail(f"{where}.answer", "must be an object")
        if not isinstance(lab["superseded"], bool):
            _fail(f"{where}.superseded", "must be true or false")
        if lab["superseded_reason"] not in (None,) + SUPERSEDED_REASONS \
                or (lab["superseded_reason"] is None) == lab["superseded"]:
            _fail(f"{where}.superseded_reason", f"{lab['superseded_reason']!r} must be one of "
                  f"{', '.join(SUPERSEDED_REASONS)} when superseded, else null")
        if not isinstance(lab["rules"], list):
            _fail(f"{where}.rules", "must be a list")
        for j, r in enumerate(lab["rules"]):
            if not isinstance(r, dict) or set(r) != {"kind", "key"} \
                    or r["kind"] not in RULE_KINDS:
                _fail(f"{where}.rules[{j}]", f"must be {{kind, key}} with kind one of "
                      f"{', '.join(RULE_KINDS)}")
            _text(f"{where}.rules[{j}].key", r["key"])
        _text(f"{where}.evidence_id", lab["evidence_id"])
        _text(f"{where}.at", lab["at"])
        _text(f"{where}.actor", lab["actor"])
        for k in ("record_key", "episode_id", "show_title", "raw_brand"):
            _text(f"{where}.{k}", lab[k], null=True)
        if not isinstance(lab["transcript"], str):
            _fail(f"{where}.transcript", "must be a string")
        _seconds(f"{where}.start_s", lab["start_s"])
        _seconds(f"{where}.end_s", lab["end_s"])
        labels.append(dict(lab))
    # Reads a person reviewed on any page (the app's reviewed_ids: a
    # judgment or a confirmed brand), by record key; never from labels.
    reviewed_reads = []
    for i, r in enumerate(_list(doc, "reviewed_reads", (
            "record_key", "evidence_id", "episode_id", "show_title", "start_s", "end_s",
            "judgment", "brands", "reviewed_at"))):
        where = f"reviewed_reads[{i}]"
        _text(f"{where}.evidence_id", r["evidence_id"])
        for k in ("record_key", "episode_id", "show_title", "judgment"):
            _text(f"{where}.{k}", r[k], null=True)
        _seconds(f"{where}.start_s", r["start_s"])
        _seconds(f"{where}.end_s", r["end_s"])
        if not isinstance(r["reviewed_at"], str):
            _fail(f"{where}.reviewed_at", "must be a string")
        if not isinstance(r["brands"], list):
            _fail(f"{where}.brands", "must be a list")
        for j, b in enumerate(r["brands"]):
            _text(f"{where}.brands[{j}]", b)
        reviewed_reads.append(dict(r))
    reviewed = frozenset(r["record_key"] or r["evidence_id"] for r in reviewed_reads)
    confirmed = frozenset(r["record_key"] or r["evidence_id"] for r in reviewed_reads
                          if r["brands"])
    not_ad_reads = frozenset(r["record_key"] or r["evidence_id"] for r in reviewed_reads
                             if r["judgment"] == NOT_AD)
    # with namer rules, the confirmed named reads they never touch are namer
    # input for their own episode (namer_sha256_for)
    protected = sorted(k for k in confirmed
                       if (stoplist or new_brands) and _RECORD_KEY.match(k))
    namer = _namer_rules(stoplist, new_brands)
    canon = {"aliases": sorted(aliases.items()), "stoplist": sorted(stoplist)}
    effective = {"format": FORMAT, **namer, **canon, "self_promo": sorted(self_promo),
                 "not_ads": sorted((n["record_key"], n["episode_id"], n["start_s"], n["end_s"])
                                   for n in not_ads),
                 "reviewed": sorted(reviewed), "confirmed": sorted(confirmed),
                 "not_ad_reads": sorted(not_ad_reads)}
    return Corrections(sha256=_digest(effective), aliases=aliases, stoplist=frozenset(stoplist),
                       new_brands=new_brands, self_promo=tuple(self_promo),
                       not_ads=tuple(not_ads), labels=tuple(labels),
                       superseded=tuple(superseded), reviewed=reviewed, confirmed=confirmed,
                       not_ad_reads=not_ad_reads, reviewed_reads=tuple(reviewed_reads),
                       protected=_by_episode(protected),
                       rule_meta=meta, namer_sha256=_digest(namer),
                       canon_sha256=_digest(canon))


def _namer_rules(stoplist, new_brands) -> dict:
    return {"stoplist": sorted(stoplist), "new_brands": sorted(new_brands)}


def _by_episode(record_keys) -> dict:
    """{episode id: frozenset of span ids} of named-read record keys (a
    second brand's key names its span)."""
    out: dict = {}
    for k in record_keys:
        m = _RECORD_KEY.match(k)
        out.setdefault(m.group("ep"), set()).add(f"{m.group('lo')}-{m.group('hi')}")
    return {ep: frozenset(ids) for ep, ids in out.items()}


def _digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                     default=list).encode()).hexdigest()


def load(path) -> "Corrections":
    """Read and validate a corrections file (CorrectionsError)."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise CorrectionsError(f"corrections file {path}: {exc.strerror or exc}") from exc
    except ValueError as exc:
        raise CorrectionsError(f"corrections file {path}: not JSON ({exc})") from exc
    return parse(doc)


# ---------------------------------------------------------------- the rules

def promo_match(network: str, exact: bool, title) -> bool:
    """A self-promo rule's show test, as the app's corrections.promo_match:
    with `exact`, the show title equals the network text; else the network
    words appear as whole words of the title, in order (case- and
    accent-insensitive)."""
    t = fold(title or "")
    if exact:
        return t == fold(network)
    words = re.findall(r"[a-z0-9&']+", fold(network))
    return bool(words) and re.search(
        r"(?<![a-z0-9])" + r"[^a-z0-9]+".join(map(re.escape, words)) + r"(?![a-z0-9])",
        t) is not None


@dataclass(frozen=True)
class Corrections:
    sha256: str
    aliases: dict          # raw key -> (canonical key, display or None)
    stoplist: frozenset    # keys
    new_brands: dict       # key -> display or None
    self_promo: tuple      # ((brand key, network text), ...)
    not_ads: tuple
    labels: tuple          # every label, superseded ones included (label_docs skips them)
    superseded: tuple = ()  # removed / replaced / undone rules: never applied
    reviewed: frozenset = frozenset()   # record keys a person reviewed (reviewed_reads)
    confirmed: frozenset = frozenset()  # ... with a brand a person confirmed
    not_ad_reads: frozenset = frozenset()  # ... judged not an ad, on any page
    reviewed_reads: tuple = ()
    protected: dict = None  # episode id -> span ids the stoplist never touches
    rule_meta: dict = None  # (kind, key) -> {created_at, actor, from_read}
    namer_sha256: str = ""  # the stoplist and new brands (what the namer reads)
    canon_sha256: str = ""  # the aliases and the stoplist (what the canon map reads)

    def provenance(self) -> dict:
        """The export report's record: every digest."""
        return {"format": FORMAT, "sha256": self.sha256, "namer_sha256": self.namer_sha256,
                "canon_sha256": self.canon_sha256}

    def protected_spans(self, episode_id) -> frozenset:
        """Span ids of the episode the namer never stoplists (nor merges
        away): spans of named reads whose brand a person confirmed, when
        there is a stoplist."""
        return (self.protected or {}).get(episode_id, frozenset())

    def namer_sha256_for(self, episode_id) -> str:
        """The namer digest of one episode: namer_sha256, plus the span ids
        of the episode the rules spare (protected_spans) when there are any."""
        spans = self.protected_spans(episode_id)
        if not spans:
            return self.namer_sha256
        return _digest({**_namer_rules(self.stoplist, self.new_brands),
                        "protected": sorted(spans)})

    def namer_provenance(self, episode_id=None):
        """What the names doc of `episode_id` records, or None when there
        are no namer rules (then the names are exactly the names without the
        file)."""
        if not (self.stoplist or self.new_brands):
            return None
        return {"format": FORMAT, "namer_sha256": self.namer_sha256_for(episode_id)}

    def canon_provenance(self):
        """What the canon map records, or None when there are no canon rules."""
        if not (self.aliases or self.stoplist):
            return None
        return {"format": FORMAT, "canon_sha256": self.canon_sha256}

    def names_stale(self, names_doc, episode_id) -> bool:
        """The episode's names doc was named with other namer rules (or
        other spared spans) than these."""
        got = ((names_doc or {}).get("corrections") or {}).get("namer_sha256")
        return got != (self.namer_provenance(episode_id) or {}).get("namer_sha256")

    def canon_stale(self, canon_doc) -> bool:
        """The canon map was built with other canon rules than these."""
        got = ((canon_doc or {}).get("corrections") or {}).get("canon_sha256")
        return got != (self.canon_provenance() or {}).get("canon_sha256")

    # -- naming

    def stopped(self, brand) -> bool:
        """`brand` is a generic word, never a brand."""
        if not brand or not self.stoplist:
            return False
        return key(brand) in self.stoplist or key(normalize_brand(str(brand))) in self.stoplist

    def namer_brands(self) -> dict:
        """{compact form: brand}: the exact entries the namer's repair list
        gains, the new brands, each its own spelling only (no sound-alike of
        them is re-pointed). An alias canonical the app did not know comes
        as a new brand too; aliases themselves never reach the namer."""
        return {_compact(b): b for b in sorted(self.new_brands) if _compact(b)}

    def fix_name(self, span: dict) -> dict:
        """A span's name fields with the namer's rules applied (a name kept
        from an earlier naming, refine.rename): stoplisted candidates
        dropped, a stoplisted brand replaced by the next candidate (or none)
        with needs_review and confidence 0 (the confidence was the
        stoplisted word's). Aliases are not applied (the name stays as
        heard). The same dict when nothing changes."""
        if not isinstance(span, dict):
            return span
        cands = span.get("candidates") or []
        kept = [c for c in cands if not (isinstance(c, dict) and self.stopped(c.get("brand")))]
        out = dict(span, candidates=kept) if len(kept) != len(cands) else dict(span)
        if self.stopped(span.get("brand")):
            nxt = next((c["brand"] for c in kept if isinstance(c, dict) and c.get("brand")), None)
            out.update(brand=nxt, needs_review=True, confidence=0.0)
        return span if out == span else out

    # -- canon

    def apply_to_canon(self, doc: dict) -> dict:
        """The canon document with the alias rules over its merges: each
        heard spelling maps to its canonical (reason ALIAS_REASON, added when
        no span named it yet). Only the alias's own key: a spelling the map
        merged onto a heard spelling keeps that merge, as in the app. Each
        entry an alias changed keeps what it was in
        `before_alias` ({canonical, reason}, or null for an added entry), so
        the export can give a person-confirmed read its plain canon
        (apply_aliases). Records canon_provenance under "corrections". Without
        canon rules, the document is returned as it is."""
        prov = self.canon_provenance()
        if prov is None:
            return doc
        cmap = {k: dict(v) for k, v in doc["map"].items()}
        zero = {"support_shows": 0, "name_episodes": 0, "name_shows": 0, "domain_episodes": 0,
                "domain_shows": 0, "mention_episodes": 0, "mention_shows": 0,
                "gazetteer": False}
        n = 0
        by_key = {key(k): k for k in cmap}
        for raw, (canonical, _) in sorted(self.aliases.items()):
            k = by_key.get(raw, raw)
            entry = cmap.get(k)
            before = entry.get("before_alias", {"canonical": entry["canonical"],
                                                "reason": entry["reason"]}) if entry else None
            cmap[k] = dict(entry or {"support": dict(zero), "canonical_support": dict(zero)},
                           canonical=canonical, reason=ALIAS_REASON, before_alias=before)
            n += 1
        cmap = dict(sorted(cmap.items()))
        return dict(doc, map=cmap, keys=len(cmap),
                    canonical_keys=len({v["canonical"] for v in cmap.values()}),
                    merged_keys=sum(1 for k, v in cmap.items() if v["canonical"] != k),
                    corrections=dict(prov, aliases=n))

    # -- export

    def stop_kept_spans(self, cands: list) -> tuple:
        """(candidates, how many fell through): in a span kept for a
        confirmed read (protected_spans), each read nobody confirmed gets
        the stoplist as the app's stoplist_reads: stoplisted candidates
        dropped; a stoplisted brand becomes the next listed candidate that
        is not stoplisted (or none), needs_review, confidence 0, no
        canonical. Other candidates are returned as they are."""
        if not self.stoplist or not self.protected:
            return cands, 0
        out, n = [], 0
        for c in cands:
            cid = str(c.get("candidate_id") or "")
            m = _RECORD_KEY.match(f"named:{cid}")
            if m is None or f"named:{cid}" in self.confirmed or \
                    f"{m.group('lo')}-{m.group('hi')}" not in self.protected_spans(m.group("ep")):
                out.append(c)
                continue
            prov = c.get("provenance") if isinstance(c.get("provenance"), dict) else {}
            listed = prov.get("candidates") if isinstance(prov.get("candidates"), list) else []
            kept = [x for x in listed if not (isinstance(x, dict) and self.stopped(x.get("brand")))]
            raw = c.get("brand_spoken") or c.get("brand")
            if not self.stopped(raw) and len(kept) == len(listed):
                out.append(c)
                continue
            c = dict(c, provenance=dict(prov, candidates=kept))
            if self.stopped(raw):
                nxt = next((x["brand"] for x in listed if isinstance(x, dict)
                            and isinstance(x.get("brand"), str) and key(x["brand"])
                            and not self.stopped(x["brand"]) and key(x["brand"]) != key(raw)),
                           None)
                brand = (normalize_brand(nxt) or None) if nxt else None
                c = {k: v for k, v in c.items() if k not in ("brand_canonical", "canon_reason")}
                c.update(brand=brand, brand_spoken=brand, needs_review=True,
                         confidence="low", confidence_score=0.0)
                n += 1
            out.append(c)
        return out, n

    def apply_aliases(self, cands: list, canon=None) -> list:
        """Named-read candidates with the alias rules over the canon map:
        brand_canonical (and canon_reason) for a read heard as an alias's
        raw spelling, its own key only (as the app: never a spelling the
        canon map merged onto it); never on a read whose brand a person
        confirmed. A confirmed read that `canon` (the canon document
        apply_canon used, built with these rules) aliased gets the canon it
        had without them (its entry's before_alias), or none. brand_spoken
        is never changed. Idempotent."""
        if not self.aliases:
            return cands
        cmap = (canon or {}).get("map") or {}
        out = []
        for c in cands:
            if f"named:{c.get('candidate_id')}" in self.confirmed:
                out.append(_unalias(c, cmap))
                continue
            spoken = c.get("brand_spoken")
            hit = self.aliases.get(key(spoken)) if spoken else None
            if hit is None or c.get("canon_reason") == ALIAS_REASON or hit[0] == spoken:
                out.append(c)
                continue
            out.append(dict(c, brand_canonical=hit[0], canon_reason=ALIAS_REASON))
        return out

    def canonical(self, c: dict) -> str:
        """The canonical brand key of a named-read candidate (the app's
        alias table): the alias of its heard spelling, else brand_canonical,
        else brand_spoken."""
        hit = self.aliases.get(key(c.get("brand_spoken") or c.get("brand")))
        return hit[0] if hit else key(c.get("brand_canonical") or c.get("brand_spoken")
                                      or c.get("brand"))

    def self_promo_hit(self, brand_key: str, show_title) -> str | None:
        """The network text of the first self-promo rule whose brand is
        `brand_key` and whose show test (promo_match) the show title
        passes (the app's corrections.self_promo_reads); else None."""
        return next((network for brand, network, exact in self.self_promo
                     if brand_key and brand == brand_key
                     and promo_match(network, exact, show_title)), None)

    def self_promo_network(self, c: dict) -> str | None:
        """self_promo_hit for a named-read candidate (its canonical brand
        and show title); never for a read a person reviewed."""
        if not self.self_promo or f"named:{c.get('candidate_id')}" in self.reviewed:
            return None
        return self.self_promo_hit(self.canonical(c), c.get("show_title"))

    def self_promo_shows(self, titles) -> list:
        """Per rule, {brand, network, exact_show, shows}: the show titles it
        covers."""
        return [{"brand": b, "network": n, "exact_show": x,
                 "shows": [t for t in titles if promo_match(n, x, t)]}
                for b, n, x in self.self_promo]

    def drop_not_ads(self, cands: list) -> tuple:
        """(candidates without the operator's not-ad reads, report).

        A not-ad on a named read ("named:" record key) matches the read
        with that record key ("named:" + candidate_id). When a re-name moved
        it, it matches the read of the same episode and kind (primary read /
        the same second brand) whose brand agrees (_brand_keys: heard or
        canonical, through the aliases) and that overlaps at least
        MOVED_MIN_OVERLAP of the shorter span; of several, the largest
        overlap, then the earliest start. Unmatched not-ads are listed.

        A not-ad on any other read (detector "det:", Jev, LLM) judges that
        read only. The app keeps it by its record key, so it drops nothing
        here and is listed in `not_named`.

        A read judged not an ad on any app page (reviewed_reads) is left out
        too, by its record key only (`reviewed_not_ad`: those not already
        dropped by a not-ad rule)."""
        rep = {"not_ads": len(self.not_ads), "dropped": 0, "moved": [], "unmatched": [],
               "not_named": [], "reviewed_not_ad": 0}
        if not self.not_ads and not self.not_ad_reads:
            return cands, rep
        by_id = {c.get("candidate_id"): i for i, c in enumerate(cands)}
        drop: set = set()
        for n in self.not_ads:
            rk = n["record_key"]
            if not rk.startswith("named:"):
                rep["not_named"].append(rk)
                continue
            i = by_id.get(rk[len("named:"):])
            if i is None:
                i = self._moved(n, cands, drop)
                if i is not None:
                    rep["moved"].append({"record_key": rk,
                                         "now": f"named:{cands[i]['candidate_id']}"})
            if i is None:
                rep["unmatched"].append(rk)
            else:
                drop.add(i)
        for rk in sorted(self.not_ad_reads):
            i = by_id.get(rk[len("named:"):]) if rk.startswith("named:") else None
            if i is not None and i not in drop:
                drop.add(i)
                rep["reviewed_not_ad"] += 1
        rep["dropped"] = len(drop)
        return [c for i, c in enumerate(cands) if i not in drop], rep

    def _brand_keys(self, *brands) -> set:
        """The brand keys of the given names and their alias canonicals."""
        keys = {key(b) for b in brands if b and key(b)}
        return keys | {self.aliases[k][0] for k in keys if k in self.aliases}

    def _moved(self, n: dict, cands: list, taken: set):
        m = _RECORD_KEY.match(n["record_key"])
        ep = n.get("episode_id") or (m.group("ep") if m else None)
        lo, hi = n.get("start_s"), n.get("end_s")
        if m and (lo is None or hi is None):
            lo, hi = int(m.group("lo")) / 1000.0, int(m.group("hi")) / 1000.0
        if ep is None or lo is None or hi is None or hi <= lo:
            return None
        rest = m.group("rest") if m else ""
        want = self._brand_keys(n.get("brand"))
        best, best_rank = None, None
        for i, c in enumerate(cands):
            cid = str(c.get("candidate_id") or "")
            if i in taken or c.get("ep_id") != ep or c.get("start_s") is None \
                    or c.get("end_s") is None:
                continue
            cm = _RECORD_KEY.match(f"named:{cid}")
            if (cm.group("rest") if cm else "") != rest:
                continue
            have = self._brand_keys(c.get("brand_spoken"), c.get("brand_canonical"))
            if not (want & have if want else not have):
                continue
            s, e = float(c["start_s"]), float(c["end_s"])
            ov = min(hi, e) - max(lo, s)
            if ov <= 0 or ov < MOVED_MIN_OVERLAP * min(hi - lo, e - s):
                continue
            rank = (-ov, s, cid)
            if best_rank is None or rank < best_rank:
                best, best_rank = i, rank
        return best

    # -- labels

    def label_docs(self) -> dict:
        """{episode_id: label document} in the label format the eval
        scripts read (score_labels.load_labels; labels-blind / blind2):
        reads (confirm / pick / type / alias: the answer brand),
        organic_mentions (self_promo, and not_ad: negatives) and unresolved
        (generic: the word is no brand, the read's brand unknown). Only
        labels the file does not mark superseded count (a later action on
        the same read replaced the others); each item keeps its provenance
        (action, actor, time, record key, evidence id, raw brand, answer,
        how many superseded actions preceded it). coverage "partial": only
        the reviewed reads are labeled (score_labels scores only the
        candidates that overlap them)."""
        latest: dict = {}
        superseded: dict = {}
        for lab in self.labels:
            ident = (lab["episode_id"], lab["record_key"] or lab["evidence_id"])
            if lab["superseded"]:
                superseded[ident] = superseded.get(ident, 0) + 1
            else:
                latest[ident] = lab
        docs: dict = {}
        for (ep, _), lab in latest.items():
            if not ep:
                continue
            doc = docs.setdefault(ep, {
                "episode_id": ep, "show_title": lab["show_title"], "source": LABEL_SOURCE,
                "coverage": "partial", "corrections_sha256": self.sha256,
                "label_provenance": "EarWitness ad review: one item per reviewed read, the "
                                    "operator's current action on it (corrections file, "
                                    f"rules {self.sha256[:12]})",
                "labeled_by": [], "labeled_date": None,
                "reads": [], "organic_mentions": [], "unresolved": []})
            if lab["actor"] not in doc["labeled_by"]:
                doc["labeled_by"].append(lab["actor"])
            day = str(lab["at"])[:10]
            doc["labeled_date"] = max(doc["labeled_date"] or day, day)
            ident = (ep, lab["record_key"] or lab["evidence_id"])
            prov = {"action": lab["action"], "actor": lab["actor"], "at": lab["at"],
                    "record_key": lab["record_key"], "evidence_id": lab["evidence_id"],
                    "raw_brand": lab["raw_brand"], "answer": lab["answer"],
                    "superseded_actions": superseded.get(ident, 0)}
            span = {"start_s": lab["start_s"], "end_s": lab["end_s"]}
            answer, action = lab["answer"], lab["action"]
            if action in BRAND_ACTIONS:
                doc["reads"].append({"brand": answer.get("brand") or lab["raw_brand"],
                                     "read_type": LABEL_READ_TYPE, **span,
                                     "evidence": lab["transcript"], "confidence": "high",
                                     "provenance": prov})
            elif action == "self_promo":
                doc["organic_mentions"].append({
                    "brand": f"{answer.get('brand') or lab['raw_brand']} (self-promo)",
                    "context": f"self-promotion ({answer.get('network') or lab['show_title']})",
                    **span, "provenance": prov})
            elif action == "not_ad":
                doc["organic_mentions"].append({
                    "brand": f"{lab['raw_brand'] or 'unnamed'} (not an ad)",
                    "context": "operator: not an ad", **span, "provenance": prov})
            else:  # generic
                doc["unresolved"].append({
                    "brand": None, **span,
                    "reason": f"generic word {answer.get('word') or lab['raw_brand']!r} is not "
                              "the brand; the brand is unknown", "provenance": prov})
        for doc in docs.values():
            doc["labeled_by"] = ", ".join(sorted(doc["labeled_by"]))
            for k in ("reads", "organic_mentions", "unresolved"):
                doc[k].sort(key=lambda it: (it["start_s"] is None, it["start_s"] or 0.0,
                                            it["provenance"]["record_key"] or ""))
        return dict(sorted(docs.items()))


def _unalias(c: dict, cmap: dict) -> dict:
    """A candidate without the canon map's alias: the canonical its map
    entry had before the alias (brand_canon.apply_canon's rule), or no
    brand_canonical when it had none."""
    if ALIAS_REASON not in str(c.get("canon_reason") or ""):
        return c
    from earwitness.pipeline import brand_canon
    spoken = c.get("brand_spoken")
    _, entry = brand_canon.canonical_of(spoken, cmap) if spoken else (None, None)
    before = (entry or {}).get("before_alias")
    plain = {k: v for k, v in c.items() if k not in ("brand_canonical", "canon_reason")}
    if not before or before["canonical"] == spoken:
        return plain
    return dict(plain, brand_canonical=before["canonical"], canon_reason=before["reason"])


def write_labels(corrections: Corrections, out_dir) -> dict:
    """Write label_docs() as <out_dir>/<episode_id>.json (rewritten only
    when they differ; files this command wrote for episodes no longer
    labeled are removed, other files are left alone)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    docs = corrections.label_docs()
    rep = {"out": str(out), "episodes": len(docs), "written": 0, "unchanged": 0, "removed": 0,
           "reads": 0, "negatives": 0, "unresolved": 0, "labels": len(corrections.labels),
           "corrections_sha256": corrections.sha256}
    for ep, doc in docs.items():
        if "/" in ep or ep.startswith("."):
            raise CorrectionsError(f"corrections file: episode id {ep!r} is not a file name")
        data = json.dumps(doc, indent=1, ensure_ascii=False) + "\n"
        path = out / f"{ep}.json"
        rep["reads"] += len(doc["reads"])
        rep["negatives"] += len(doc["organic_mentions"])
        rep["unresolved"] += len(doc["unresolved"])
        if path.is_file() and path.read_text(encoding="utf-8") == data:
            rep["unchanged"] += 1
            continue
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, path)
        rep["written"] += 1
    for path in sorted(out.glob("*.json")):
        if path.stem in docs:
            continue
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(old, dict) and old.get("source") == LABEL_SOURCE:
            path.unlink()
            rep["removed"] += 1
    return rep
