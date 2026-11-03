"""Display spelling: a brand is shown as the brand writes itself.

The namer's name is what it heard ("jonesroadbeauty", "public"), and the
same brand can come out spaced or joined. A versioned spelling map
(scripts/build_spelling.py) maps a format key onto one written spelling,
from sources outside the transcripts, most trusted first:

  advertisers  the advertiser list's names (earwitness/data/advertisers-v*.json)
  notes        sponsor names as show notes write them
  canon        corpus canon anchors (canonical forms with strong support)

The format key ignores case, spacing, punctuation and a written TLD
("Public.com", "public com" and "public" share one key; "Jones Road
Beauty" and "jonesroadbeauty" another). Respelling is exact on that key:
no sound-alikes and no prefixes, so it never moves a name to another brand.

File: {"format": FORMAT, "version", "sha256" (of the entries), "built_at",
"sources", "entries": [{"key", "display", "tier"}]}. Loading checks the
digest; a names doc respelled with the map records it (describe()).
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from earwitness.pipeline import brand_namer as bn

FORMAT = "earwitness-spelling/1"
TIERS = ("advertisers", "notes", "canon")
# a written TLD: ".com" / " dot com" (any of these), or a spaced " com"
_TLD = re.compile(r"(?:(?:\.|\s+dot\s+)(?:com|net|org|io|co|fm|ai|app|tv|me|us|edu|ly|so|xyz|"
                  r"co\.uk|com\.au)|\s+com)$", re.I)


def key(name) -> str:
    """Format key: lowercase letters and digits, a written TLD dropped."""
    s = str(name or "").strip().lower()
    t = _TLD.sub("", s)
    return bn.compact(t if bn.compact(t) else s)


def entries_digest(entries: list) -> str:
    return hashlib.sha256(json.dumps(entries, sort_keys=True, ensure_ascii=False)
                          .encode()).hexdigest()


def build(sources: dict, *, built_at: str, version: int = 1, inputs: dict | None = None,
          words: frozenset = frozenset()) -> dict:
    """The map from {tier: [written names]} (TIERS). A key takes the most
    trusted tier's spelling; within a tier, the spelling written most often
    (ties: the alphabetically first). A name with a TLD whose rest is one
    dictionary word (`words`: "better.com", "stamps.com") keeps its TLD in
    the key: the bare word is no brand."""
    out: dict = {}
    for tier in TIERS:
        counts: dict = {}
        for name in sources.get(tier) or ():
            disp = re.sub(r"\s+", " ", str(name or "").strip().lower())
            k = key(disp)
            if k != bn.compact(disp) and k in words:
                k = bn.compact(disp)
            if len(k) < 2:
                continue
            counts.setdefault(k, Counter())[disp] += 1
        for k, c in counts.items():
            if k not in out:
                disp = sorted(c.items(), key=lambda x: (-x[1], x[0]))[0][0]
                out[k] = {"key": k, "display": disp, "tier": tier}
    entries = sorted(out.values(), key=lambda e: e["key"])
    return {"format": FORMAT, "version": version, "sha256": entries_digest(entries),
            "built_at": built_at, "sources": dict(inputs or {}), "entries": entries}


class Spelling:
    def __init__(self, doc: dict):
        if not isinstance(doc, dict) or doc.get("format") != FORMAT:
            raise ValueError(f"not a spelling map ({FORMAT})")
        entries = doc.get("entries") or []
        if entries_digest(entries) != doc.get("sha256"):
            raise ValueError("spelling map digest does not match its entries")
        self.doc, self.sha256, self.version = doc, doc["sha256"], doc.get("version")
        self.by_key = {e["key"]: e for e in entries}

    @classmethod
    def load(cls, path) -> "Spelling":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def describe(self) -> dict:
        return {"version": self.version, "sha256": self.sha256,
                "entries": len(self.doc.get("entries") or ())}

    def display(self, name):
        """(spelling, tier) when the map writes `name` otherwise, else None."""
        if not name:
            return None
        e = self.by_key.get(key(name)) or self.by_key.get(bn.compact(str(name)))
        if e is None or e["display"] == str(name).strip().lower():
            return None
        return e["display"], e["tier"]


def respell_doc(names_doc: dict, spelling: Spelling, protected=frozenset()) -> tuple:
    """(doc, changes): every span brand and second brand in the map's
    spelling; the name before is kept as brand_spelled_from (when not
    already set by an earlier respelling). Spans in `protected` (a person
    confirmed them) stay as they are."""
    changes = []

    def fix(obj):
        hit = spelling.display(obj.get("brand"))
        if hit:
            changes.append({"from": obj["brand"], "to": hit[0], "rule": f"display:{hit[1]}"})
            obj.setdefault("brand_spelled_from", obj["brand"])
            obj["brand"] = hit[0]
        return obj

    spans = []
    for sp in names_doc.get("spans") or ():
        if sp.get("span_id") in protected:
            spans.append(sp)
            continue
        sp = fix(dict(sp))
        sp["second_brands"] = [fix(dict(sb)) for sb in sp.get("second_brands") or ()]
        spans.append(sp)
    return {**names_doc, "spans": spans}, changes
