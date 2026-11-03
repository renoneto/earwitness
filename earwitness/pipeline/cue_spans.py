"""Cue spans: short reads the 60-s Jev windows do not flag.

A sponsor tag ("This episode is brought to you by X", "Let me tell you about
X") lasts 3-10 s, so the 60-s window around it often scores below the
flag threshold and no span covers it. A promo for another show can fall
below it too. This pass adds a span for such a read when the text has the
cue and the window around it scores at least `min_noul` (well below the
flag threshold, well above talk: on dev and the holdout, real tags scored
0.27-0.85 and spoken look-alikes ("I'm gonna tell you about Robert
Durst", a joking "brought to you by whatnot") 0.02-0.04).

kinds:
  tags    a sponsor-tag cue outside every span: a span from the cue to the
          end of the sentence run (TAG_S, at most TAG_MAX_S, never into the
          next span), named by the namer on that text when that name is
          spoken right after the cue, else by the longest run of words
          after the cue that the advertiser list or the brand list knows
          (needs_review off); else no span. method "tag", read_format
          "sponsor tag".
  promos  an unflagged window outside every span whose text is a promo for
          another show (promos.find_promo): a span over the window, named
          after the show. method "promo", read_format "network promo".
"""
from __future__ import annotations

import re

from earwitness.pipeline import brand_namer as bn
from earwitness.pipeline import promos

KINDS = ("tags", "promos")
MIN_NOUL = 0.2
TAG_S = 8.0
TAG_MAX_S = 15.0
TAG_FORMAT = "sponsor tag"
TAG_CUE = re.compile(
    r"\b(?:brought to you by|sponsored by|presented by|powered by|"
    r"support for [^.]{0,60}? comes from|supported by|today'?s sponsor is|"
    r"(?:let me|i want to|i wanna|i'll|i will|i(?: got| have|'ve got)? to|i gotta|gotta)"
    r" tell (?:you|him|her|them|y'all|everyone|everybody) about)\b", re.I)


AFTER_CHARS = 60
_LEAD = re.compile(r"^(?:\s|,)*(?:(?:our|my) (?:good )?(?:friends?|partners?|sponsors?)(?: over)? at |"
                   r"the (?:folks|team|people) at |the |our |a |an )", re.I)


def _cue_times(segments) -> list:
    """[(time, segment index, text right after the cue)] of each tag cue,
    the time interpolated within its segment by character position."""
    out = []
    segments = list(segments or ())
    for i, s in enumerate(segments):
        text = str(s.get("text") or "")
        for m in TAG_CUE.finditer(text):
            t = s["start"] + (s["end"] - s["start"]) * m.start() / max(1, len(text))
            after = " ".join([text[m.end():]] + [str(x.get("text") or "")
                                                 for x in segments[i + 1:i + 3]])
            out.append((round(t, 2), i, after.strip()[:AFTER_CHARS * 2]))
    return out


def _tag_name(res: dict, after: str, namer) -> tuple:
    """(brand, trusted) for a tag, from the words right after the cue (up
    to the first stop): the longest run of them the advertiser list or the
    brand list knows, unless the namer's name is spoken there and is
    longer ("whole foods market", never "whole"); else the namer's name
    when it is spoken there; else (None, False)."""
    head = after[:AFTER_CHARS]
    phrase = _LEAD.sub("", re.split(r"[.,;!?]", head)[0]).strip()
    words = phrase.split()[:5]
    adv = getattr(namer, "advertisers", None)
    entries = getattr(namer, "entries", None) or {}
    corr = getattr(namer, "corrections", None)
    stopped = corr.stopped if corr is not None else (lambda b: False)
    listed = None
    for n in range(len(words), 0, -1):
        cand = " ".join(words[:n]).lower()
        if stopped(cand):
            continue
        if adv is not None:
            hit = adv.match(cand)
            if hit and hit[1] == "exact":
                listed = hit[0]["name"]
                break
        if bn.compact(cand) in entries and len(bn.compact(cand)) >= 4 \
                and cand not in getattr(namer, "words", ()):
            listed = entries[bn.compact(cand)]
            break
    b = res.get("brand")
    if b and stopped(b):
        b = None
    if listed and stopped(listed):
        listed = None
    spoken = bool(b and bn.compact(b) and bn.compact(b) in bn.compact(head))
    if spoken and (listed is None or len(bn.compact(b)) > len(bn.compact(listed))):
        return b, False
    return (listed, True) if listed else (None, False)


NEAR_S = 30.0  # a span of the same brand this close already has the read


def _named_near(spans, brand, lo: float, hi: float) -> bool:
    k = bn.compact(brand)
    return any(bn.compact(o.get("brand") or "") == k
               and o.get("start_s", sp["start_s"]) < hi + NEAR_S
               and o.get("end_s", sp["end_s"]) > lo - NEAR_S
               for sp in spans for o in [sp] + list(sp.get("second_brands") or ()))


def _inside(t: float, ranges, pad: float = 1.0) -> bool:
    return any(a - pad <= t <= b + pad for a, b in ranges)


def _noul_at(windows, lo: float, hi: float):
    ws = [w for w in windows if w["start_s"] < hi and w["end_s"] > lo]
    return (max(w["noul"] for w in ws), [w["window_id"] for w in ws]) if ws else (None, [])


def _span(lo, hi, brand, res, ids, noul, method, fmt, **extra) -> dict:
    return {"span_id": f"{round(lo * 1000)}-{round(hi * 1000)}", "start_s": lo, "end_s": hi,
            "brand": brand, "candidates": (res or {}).get("candidates") or [],
            "confidence": (res or {}).get("confidence", 1.0),
            "needs_review": bool((res or {}).get("needs_review", False)),
            "method": method, "jev_window_ids": ids, "noul_max": noul,
            "context_s": [lo, hi], "second_brands": [], "read_format": fmt, **extra}


def add_cue_spans(names_doc: dict, segments, jev_doc, namer, *, kinds=("tags",),
                  min_noul: float = MIN_NOUL, shows=None, own=frozenset(),
                  threshold: float = 0.8) -> tuple:
    """(doc, added spans) with cue spans (module doc) among the doc's
    spans, sorted by start. Existing spans are never changed."""
    windows = bn.jev_windows(jev_doc) or []
    segments = list(segments or ())
    spans = list(names_doc.get("spans") or ())
    taken = [(s["start_s"], s["end_s"]) for s in spans]
    added = []
    if "tags" in kinds:
        for t, i, after in _cue_times(segments):
            if _inside(t, taken):
                continue
            noul, _ = _noul_at(windows, t, t + 0.01)
            if noul is None or noul < min_noul:
                continue
            lo = t
            hi = next((s["end"] for s in segments[i:] if s["end"] >= lo + TAG_S),
                      segments[-1]["end"])
            hi = min(hi, lo + TAG_MAX_S, *[a for a, _ in taken if a > lo])
            if hi - lo < 2.0:
                continue
            res = namer.name_span(bn._text_between(segments, lo, hi))
            brand, trusted = _tag_name(res, after, namer)
            if not brand or bn.compact(brand) in own or _named_near(spans, brand, lo, hi):
                continue
            noul, ids = _noul_at(windows, lo, hi)
            sp = _span(lo, hi, brand, res, ids, noul, "tag", TAG_FORMAT)
            if trusted:
                sp["needs_review"] = False
            added.append(sp)
            taken.append((lo, hi))
    if "promos" in kinds and shows is not None:
        for w in windows:
            if not (min_noul <= w["noul"] < threshold):
                continue
            lo, hi = w["start_s"], w["end_s"]
            if any(a < hi and b > lo for a, b in taken):
                continue
            corr = getattr(namer, "corrections", None)
            hit = promos.find_promo(bn._segments_text(bn._text_between(segments, lo, hi)),
                                    shows, own=own,
                                    stopped=corr.stopped if corr is not None else None)
            if hit is None:
                continue
            promo = {"show": hit["show"], "how": hit["how"], "cue": hit["cue"],
                     "networks": list(hit["entry"].get("networks") or ()),
                     "own_network": None, "previous_brand": None}
            sp = _span(lo, hi, hit["show"], None, [w["window_id"]], w["noul"], "promo",
                       promos.READ_FORMAT, promo=promo)
            added.append(sp)
            taken.append((lo, hi))
    out = sorted(spans + added, key=lambda s: (s["start_s"], s["end_s"]))
    return {**names_doc, "spans": out}, added
