"""Pod naming with Jev: name each spot inside a run of flagged Jev windows.

Back-to-back produced spots (ad pods) share one run of flagged 60-s Jev
windows. The local namer names a span from its text and gives a span one
brand, so most pod reads lose their time to a neighbouring spot. This pass
re-names each run spot by spot:

1. the run is cut into pieces of about `piece_s` seconds at transcript
   segment edges (and where a read opener such as "brought to you by"
   starts);
2. a candidate pool is gathered for the run: the brands and candidates the
   names doc already has there (spans and second brands), every piece's
   local candidates (confident or not), and optional extra names such as
   show-notes sponsors;
3. one Jev request per run asks, for every piece, a `choice` question
   (TypeSafe System One, https://docs.typesafe.ai/primitives/choice):
   which of the pool's brands this piece advertises, or `none` (no ad) or
   `other` (an ad for a brand not in the list). The whole run is the state,
   so Jev reads each piece in context;
4. pieces with the same brand join into one span (one `none`/`other` piece
   between two pieces of one brand is bridged); `none` time joins its
   neighbours (or is dropped, `none_policy="drop"`); `other` time becomes
   a needs-review span named by the local namer.

A run whose request fails, or whose pieces name no listed brand, keeps the
spans it had. Pod spans carry `pod: true`, method "jev-choice", the Jev
probability as confidence, and no second brands.

Off by default (rename --pod-jev). Responses are cached per episode under
<corpus>/jev-pod/<ep>.json keyed by the request's sha256, so a re-run pays
nothing; every paid call is counted in the pass report (tokens and USD).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

from earwitness.pipeline import brand_namer as bn
from earwitness.pipeline import jev_budget, jev_screen

POD_DIR = "jev-pod"
PIECE_S = 15.0
PIECE_PAD_S = 0.0
POOL_MAX = 24
NONE, OTHER = "none", "other"
NONE_DESC = ("this piece is not part of any ad: show conversation, an intro or outro, or a "
             "plain mention with no commercial message")
OTHER_DESC = "this piece is an ad, but for a brand, product or show not in this list"
QUESTION = ("Which brand, product, service or show does this piece advertise? An ad often "
            "runs over several pieces, and back-to-back ads follow each other: judge this "
            "piece by what it promotes, using the neighbouring pieces only for context.")
NONE_POLICIES = ("join", "drop")


class PodJevBudget(RuntimeError):
    """The pass reached its spending cap; no further Jev request is sent."""


def flagged_runs(windows: list, threshold: float) -> list:
    """[(lo, hi, [windows])] for each run of consecutive flagged windows."""
    ws = list(windows or ())
    idx = [i for i, w in enumerate(ws) if w["noul"] >= threshold]
    out = []
    for run in bn._runs_of_consecutive(idx):
        out.append((ws[run[0]]["start_s"], ws[run[-1]]["end_s"], [ws[i] for i in run]))
    return out


def pieces(segments, lo: float, hi: float, size: float = PIECE_S) -> list:
    """[(a, b)] covering [lo, hi]: consecutive transcript segments (by
    midpoint) grouped until a piece reaches `size` s, a new piece starting
    where a read opener starts. Time without segments joins the piece
    before it; the first piece starts at lo and the last ends at hi."""
    segs = [s for s in segments or () if lo <= (s["start"] + s["end"]) / 2 < hi]
    if not segs:
        return [(lo, hi)]
    opens = {s["start"] for s in segs
             if bn.OPENER_RE.search(str(s.get("text") or ""))}
    groups, cur = [], []
    for s in segs:
        if cur and (s["start"] in opens or s["end"] - cur[0]["start"] > size * 1.5
                    or cur[-1]["end"] - cur[0]["start"] >= size):
            groups.append(cur)
            cur = []
        cur.append(s)
    if cur:
        groups.append(cur)
    # a short tail (under a third of a piece, no opener) joins its neighbour
    if len(groups) > 1 and groups[-1][-1]["end"] - groups[-1][0]["start"] < size / 3 \
            and groups[-1][0]["start"] not in opens:
        tail = groups.pop()
        groups[-1] += tail
    edges = [lo] + [max(lo, min(hi, g[0]["start"])) for g in groups[1:]] + [hi]
    return [(a, b) for a, b in zip(edges, edges[1:]) if b > a]


def _key(brand) -> str:
    return bn._key(brand) if brand else ""


def candidate_pool(spans: list, piece_results: list, extra=(), max_n: int = POOL_MAX) -> list:
    """Distinct brand names (display spelling) for one run, best first:
    the run's span brands and second brands, then candidates by their best
    score over the spans and pieces, then `extra` (e.g. show-notes
    sponsors) not yet in the pool. Names with the same compact key appear
    once (distinct brands that share a word stay apart); at most `max_n`
    (extras always fit)."""
    scored: dict = {}
    order: list = []

    def add(name, score):
        if not name or not str(name).strip():
            return
        name = str(name).strip()
        k = _key(name)
        if not k or k in (NONE, OTHER):
            return
        # exact key only: "amazon" and "amazon music" stay two options
        hit = next((o for o in order if _key(o) == k), None)
        if hit is not None:
            scored[hit] = max(scored[hit], score)
            return
        order.append(name)
        scored[name] = score

    for sp in spans:
        add(sp.get("brand"), 1e6)
        for sb in sp.get("second_brands") or ():
            add(sb.get("brand"), 1e5)
    for src in list(spans) + list(piece_results):
        for c in src.get("candidates") or ():
            add(c.get("brand"), float(c.get("score") or 0.0))
    pool = sorted(order, key=lambda n: -scored[n])[:max_n]
    for name in extra or ():
        if name and _key(name) and not any(_key(p) == _key(name) for p in pool):
            pool.append(str(name).strip())
    return pool


def build_request(episode_id, run_pieces: list, texts: list, pool: list, *,
                  model: str = jev_screen.DEFAULT_MODEL) -> dict:
    """One System One request: the run's pieces as state, one choice
    question per piece over pool + none + other."""
    ids = [f"p{i:02d}" for i in range(len(run_pieces))]
    state = {"episode_id": episode_id, "pieces": {
        pid: {"start_s": round(a, 2), "end_s": round(b, 2), "transcript": t}
        for pid, (a, b), t in zip(ids, run_pieces, texts)}}
    criteria = {name: None for name in pool}
    criteria[NONE] = NONE_DESC
    criteria[OTHER] = OTHER_DESC
    questions = {pid: {"type": "choice",
                       "instructions": f"Piece {pid} ({a:.0f}s-{b:.0f}s): {QUESTION}",
                       "criteria": criteria}
                 for pid, (a, b) in zip(ids, run_pieces)}
    return {"model": model, "state": state, "questions": questions}


def request_key(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False)
                          .encode()).hexdigest()


def parse_choices(body: dict, response) -> dict:
    """{piece id: {"choice", "p", "confidence"}} after strict validation:
    every question answered with type choice, `choice` one of the offered
    options, finite probabilities in [0, 1] over exactly the options (sum
    about 1), and a finite confidence in [0, 1]."""
    if not isinstance(response, dict) or not isinstance(response.get("answers"), dict):
        raise ValueError("jev response answers must be an object")
    answers = response["answers"]
    if set(answers) != set(body["questions"]):
        raise ValueError(f"answered {sorted(answers)} != asked {sorted(body['questions'])}")
    out = {}
    for qid, q in body["questions"].items():
        a = answers[qid]
        opts = set(q["criteria"])
        if not isinstance(a, dict) or a.get("type", "choice") != "choice":
            raise ValueError(f"{qid}: not a choice answer")
        choice, probs, conf = a.get("choice"), a.get("probabilities"), a.get("confidence")
        if choice not in opts:
            raise ValueError(f"{qid}: choice {choice!r} was not offered")
        if not isinstance(probs, dict) or set(probs) != opts:
            raise ValueError(f"{qid}: probabilities must cover exactly the options")
        vals = []
        for k, v in probs.items():
            if not isinstance(v, (int, float)) or isinstance(v, bool) \
                    or not math.isfinite(v) or not 0.0 <= v <= 1.0:
                raise ValueError(f"{qid}: probability of {k!r} is not in [0, 1]")
            vals.append(float(v))
        if abs(sum(vals) - 1.0) > 0.02:
            raise ValueError(f"{qid}: probabilities sum to {sum(vals):.3f}")
        if not isinstance(conf, (int, float)) or isinstance(conf, bool) \
                or not math.isfinite(conf) or not 0.0 <= conf <= 1.0:
            raise ValueError(f"{qid}: confidence is not in [0, 1]")
        out[qid] = {"choice": choice, "p": float(probs[choice]), "confidence": float(conf),
                    "probs": {k: float(v) for k, v in probs.items()}}
    return out


class CachedAsk:
    """Send a request to Jev once: responses are kept per episode in
    <corpus>/jev-pod/<ep>.json ({request sha256: response}); a cached
    request costs nothing. `max_usd` caps what this object spends (checked
    before each paid call against the estimated cost of the request; one
    object per `rename` corpus run). Only answers that pass parse_choices
    are cached: a malformed answer raises ValueError and is not kept, and a
    cached entry that does not parse is ignored and asked again."""

    def __init__(self, corpus, transport=None, *, max_usd: float | None = None,
                 offline: bool = False):
        self.dir = Path(corpus) / POD_DIR
        self.transport, self.max_usd, self.offline = transport, max_usd, offline
        self.calls = self.cached = self.invalid_cached = 0
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.cost_usd = 0.0
        self._docs: dict = {}

    def _doc(self, ep):
        if ep not in self._docs:
            p = self.dir / f"{ep}.json"
            try:
                self._docs[ep] = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._docs[ep] = {"episode_id": ep, "responses": {}}
        return self._docs[ep]

    def __call__(self, ep, body: dict):
        doc, k = self._doc(ep), request_key(body)
        if k in doc["responses"]:
            try:
                parse_choices(body, doc["responses"][k])
                self.cached += 1
                return doc["responses"][k]
            except ValueError:
                self.invalid_cached += 1    # asked again below
        if self.offline or self.transport is None:
            return None
        est = jev_screen.cost_usd({"input_tokens": jev_screen.estimate_tokens(body)})
        if self.max_usd is not None and self.cost_usd + est > self.max_usd:
            raise PodJevBudget(f"pod Jev budget {self.max_usd} reached "
                               f"(spent {self.cost_usd:.6f})")
        try:
            resp = self.transport(body)
        except jev_budget.JevBudgetRefused as exc:   # spend cap: no further request
            raise PodJevBudget(f"Jev spend cap refused the request ({exc})") from exc
        self.calls += 1
        jev_screen.add_usage(self.usage, resp)
        self.cost_usd = jev_screen.cost_usd(self.usage)
        parse_choices(body, resp)   # raises before a malformed answer is cached
        doc["responses"][k] = resp
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / f".{ep}.json.tmp"
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        tmp.replace(self.dir / f"{ep}.json")
        return resp

    def report(self) -> dict:
        return {"calls": self.calls, "cached": self.cached,
                **({"invalid_cached": self.invalid_cached} if self.invalid_cached else {}),
                "usage": dict(self.usage),
                "cost_usd": round(self.cost_usd, 8)}


def _bridge(labels: list) -> list:
    """One none/other piece between two pieces of the same brand takes it."""
    out = list(labels)
    for i in range(1, len(out) - 1):
        if out[i] in (NONE, OTHER) and out[i - 1] == out[i + 1] \
                and out[i - 1] not in (NONE, OTHER):
            out[i] = out[i - 1]
    return out


def _groups(labels: list) -> list:
    out: list = []
    for i, lab in enumerate(labels):
        if out and out[-1][0] == lab:
            out[-1][2] = i
        else:
            out.append([lab, i, i])
    return out


TIDY_RULES = ("bridge_any", "merge_same", "absorb_unsure")


def _is_brand(lab) -> bool:
    return lab not in (NONE, OTHER)


def tidy_labels(choices: list, rules=(), min_p: float = 0.6) -> list:
    """Per-piece labels after _bridge and the optional `rules`:
    bridge_any: one piece of any other label between two pieces of one
    brand takes that brand (a read naming its own feature mid-way);
    merge_same: directly adjacent groups whose brands are one brand
    (bn._same_brand: "helix" / "helixsleep") become one, under the label
    with the larger summed probability; absorb_unsure: a brand group whose
    mean probability is under `min_p`, next to a sure brand group, takes
    that group's label (the longer neighbour when both are)."""
    bad = set(rules) - set(TIDY_RULES)
    if bad:
        raise ValueError(f"unknown tidy rules {sorted(bad)}")
    labels = _bridge([c["choice"] for c in choices])
    if "bridge_any" in rules:
        for i in range(1, len(labels) - 1):
            if labels[i - 1] == labels[i + 1] and _is_brand(labels[i - 1]):
                labels[i] = labels[i - 1]

    def mass(g):
        return sum(choices[i]["p"] for i in range(g[1], g[2] + 1)
                   if choices[i]["choice"] == g[0])

    if "merge_same" in rules:
        changed = True
        while changed:
            changed = False
            gs = _groups(labels)
            for g, h in zip(gs, gs[1:]):
                if _is_brand(g[0]) and _is_brand(h[0]) and bn._same_brand(g[0], h[0]):
                    win = g[0] if mass(g) >= mass(h) else h[0]
                    for i in range(g[1], h[2] + 1):
                        labels[i] = win
                    changed = True
                    break
    if "absorb_unsure" in rules:
        gs = _groups(labels)
        mean = [mass(g) / (g[2] - g[1] + 1) for g in gs]
        new = list(labels)
        for j, g in enumerate(gs):
            if not _is_brand(g[0]) or mean[j] >= min_p:
                continue
            nb = [k for k in (j - 1, j + 1) if 0 <= k < len(gs)
                  and _is_brand(gs[k][0]) and mean[k] >= min_p]
            if nb:
                k = max(nb, key=lambda k: gs[k][2] - gs[k][1])
                for i in range(g[1], g[2] + 1):
                    new[i] = gs[k][0]
        labels = new
    return labels


def spans_from_choices(run_pieces: list, choices: list, *, none_policy: str = "join",
                       tidy=(), min_p: float = 0.6) -> list:
    """[(label, a, b, piece indexes)] from one label per piece (a brand,
    NONE or OTHER; `choices` [{"choice", "p"}]). Brand pieces join (with
    tidy_labels and `tidy` rules); `none` time joins the neighbouring spans ("join": split at
    the piece edge nearest its middle; at a run edge it joins the one
    neighbour) or is dropped ("drop"). OTHER groups are kept as their own
    spans. [] when no piece names a listed brand."""
    if none_policy not in NONE_POLICIES:
        raise ValueError(f"none_policy must be one of {NONE_POLICIES}")
    labels = tidy_labels(choices, tidy, min_p)
    if all(lab in (NONE, OTHER) for lab in labels):
        return []
    groups = _groups(labels)
    keep = [g for g in groups if g[0] != NONE]
    if none_policy == "drop":
        return [(g[0], run_pieces[g[1]][0], run_pieces[g[2]][1], list(range(g[1], g[2] + 1)))
                for g in keep]
    edges = [a for a, _ in run_pieces] + [run_pieces[-1][1]]
    lo, hi = edges[0], edges[-1]
    bounds = [lo]
    for p, n in zip(keep, keep[1:]):
        x0, x1 = run_pieces[p[2]][1], run_pieces[n[1]][0]
        mid = (x0 + x1) / 2
        bounds.append(min((e for e in edges if x0 <= e <= x1), key=lambda e: abs(e - mid)))
    bounds.append(hi)
    return [(g[0], a, b, list(range(g[1], g[2] + 1)))
            for g, a, b in zip(keep, bounds, bounds[1:])]


def _text(segments, a, b) -> str:
    return bn._segments_text(bn._text_between(segments, a, b))


def review_flags(span_text: str, brand: str, jev_p: float, local: dict, *,
                 duration_s: float, brand_list_keys=None, min_p: float = 0.6,
                 min_s: float = 8.0) -> list:
    """Reasons a pod span's name needs review: Jev unsure (mean
    probability < min_p), the local namer names another brand on the
    span's own text, the brand is never spoken in it, the span is shorter
    than min_s, the brand is not on the brand list (when given)."""
    why = []
    if jev_p < min_p:
        why.append("jev_unsure")
    lb = local.get("brand")
    if lb and not bn._same_brand(lb, brand):
        why.append("local_disagrees")
    if bn.count_mentions(brand, span_text.lower()) == 0 and not any(
            bn._same_brand(brand, c["brand"]) for c in local.get("candidates") or ()):
        why.append("not_spoken")
    if duration_s < min_s:
        why.append("short")
    if brand_list_keys is not None and bn.compact(brand) not in brand_list_keys:
        why.append("off_list")
    return why


DEFAULT_REVIEW = ("jev_unsure", "local_disagrees", "not_spoken")


def pod_name_run(episode_id, segments, run, spans_in_run: list, namer, ask, *,
                 piece_s: float = PIECE_S, extra=(), none_policy: str = "join",
                 review_on=DEFAULT_REVIEW, min_p: float = 0.6, min_s: float = 8.0,
                 tidy=(), model: str = jev_screen.DEFAULT_MODEL) -> tuple:
    """(spans, info) for one run (lo, hi, windows). spans None: keep the
    run's existing spans (no pool, no response, or no listed brand)."""
    lo, hi, wins = run
    ps = pieces(segments, lo, hi, piece_s)
    texts = [_text(segments, a, b) for a, b in ps]
    local = [namer.name_span(bn._text_between(segments, a, b)) for a, b in ps]
    adv = getattr(namer, "advertisers", None)
    if adv is not None and extra:
        extra = [adv.canonical(x, (), getattr(namer, "words", frozenset()))[0] for x in extra]
    corr = getattr(namer, "corrections", None)
    pool = candidate_pool(spans_in_run, local, extra)
    if corr is not None:   # the operator's stoplist: never an option (show-notes sponsors too)
        pool = [b for b in pool if not corr.stopped(b)]
    info = {"lo": lo, "hi": hi, "pieces": len(ps), "pool": len(pool)}
    if not pool or not any(t.strip() for t in texts):
        info["kept"] = "no pool" if not pool else "no text"
        return None, info
    body = build_request(episode_id, ps, texts, pool, model=model)
    resp = ask(episode_id, body)
    if resp is None:
        info["kept"] = "no response"
        return None, info
    ans = parse_choices(body, resp)
    choices = [ans[f"p{i:02d}"] for i in range(len(ps))]
    info["choices"] = [c["choice"] for c in choices]
    groups = spans_from_choices(ps, choices, none_policy=none_policy, tidy=tidy, min_p=min_p)
    if not groups:
        info["kept"] = "no listed brand"
        return None, info
    out = []
    for label, a, b, idx in groups:
        own = [choices[i] for i in idx if choices[i]["choice"] == label]
        # absorbed pieces count with their probability for this label
        p = sum(choices[i]["probs"].get(label, 0.0) if "probs" in choices[i]
                else (choices[i]["p"] if choices[i]["choice"] == label else 0.0)
                for i in idx) / len(idx) if label != OTHER else (
            sum(c["p"] for c in own) / len(own) if own else 0.0)
        segs = bn._text_between(segments, a, b)
        loc = namer.name_span(segs)
        inside = [w for w in wins if w["end_s"] > a and w["start_s"] < b]
        if label == OTHER:
            brand = loc["brand"]
            why = ["other"]
            if brand is None:
                continue
        else:
            brand = label
            why = review_flags(_text(segments, a, b), brand, p, loc, duration_s=b - a,
                               brand_list_keys=set(getattr(namer, "entries", {}) or ()),
                               min_p=min_p, min_s=min_s)
        flags = [w for w in why if w in review_on or w == "other"]
        out.append({"span_id": f"{round(a * 1000)}-{round(b * 1000)}", "start_s": a,
                    "end_s": b, "brand": brand,
                    "candidates": [{"brand": brand, "score": round(p, 3),
                                    "evidence": ["jev-choice"]}]
                    + [c for c in loc["candidates"] if not bn._same_brand(c["brand"], brand)
                       ][:bn.TOP_K - 1],
                    "confidence": round(p, 3), "needs_review": bool(flags),
                    "method": "jev-choice",
                    "jev_window_ids": [w["window_id"] for w in inside],
                    "noul_max": max((w["noul"] for w in inside), default=None),
                    "context_s": [a, b], "pod": True, "second_brands": [],
                    "pod_review": why, "local_brand": loc["brand"],
                    "local_confidence": loc["confidence"]})
    if not out:
        info["kept"] = "no named span"
        return None, info
    return _join_same_brand(out), info


def _join_same_brand(spans: list) -> list:
    """Touching pod spans of one brand are one read: an "other" piece whose
    own name is its neighbour's label joins it (the labeled one leads)."""
    out: list = []
    for sp in sorted(spans, key=lambda s: s["start_s"]):
        prev = out[-1] if out else None
        if prev is None or abs(sp["start_s"] - prev["end_s"]) > 0.5 \
                or bn.compact(sp["brand"] or "") != bn.compact(prev["brand"] or ""):
            out.append(sp)
            continue
        lead = sp if "other" in (prev.get("pod_review") or ()) \
            and "other" not in (sp.get("pod_review") or ()) else prev
        a, b = prev["start_s"], sp["end_s"]
        out[-1] = {**lead, "span_id": f"{round(a * 1000)}-{round(b * 1000)}",
                   "start_s": a, "end_s": b, "context_s": [a, b],
                   "jev_window_ids": list(dict.fromkeys(prev["jev_window_ids"]
                                                        + sp["jev_window_ids"])),
                   "noul_max": max((x for x in (prev["noul_max"], sp["noul_max"])
                                    if x is not None), default=None)}
    return out


def pod_rename(names_doc: dict, segments, jev_doc, namer, ask, *, threshold=None,
               protected=frozenset(), **kw) -> tuple:
    """(new names doc, report): every run of flagged Jev windows re-named
    by pod_name_run on `segments` (the best text: refined when refined);
    spans outside runs, and runs kept, stay as they were. A run holding a
    span in `protected` (span ids a person confirmed:
    bn.protected_spans) is kept whole, with no Jev question."""
    ws = bn.jev_windows(jev_doc) or []
    thr = threshold if threshold is not None else names_doc.get(
        "jev_threshold", jev_screen.DEFAULT_THRESHOLD)
    ep = names_doc.get("episode_id")
    old = list(names_doc.get("spans") or ())
    used, new_spans, rep = set(), [], {"runs": 0, "renamed": 0, "kept": 0, "runs_info": []}
    for run in flagged_runs(ws, thr):
        lo, hi, _ = run
        inside = [i for i, sp in enumerate(old) if sp["start_s"] < hi and sp["end_s"] > lo]
        rep["runs"] += 1
        if any(old[i].get("span_id") in protected for i in inside):
            rep["kept"] += 1
            rep["runs_info"].append({"lo": lo, "hi": hi, "kept": "confirmed span"})
            continue
        spans, info = pod_name_run(ep, segments, run, [old[i] for i in inside], namer, ask, **kw)
        rep["runs_info"].append(info)
        if spans is None:
            rep["kept"] += 1
            continue
        rep["renamed"] += 1
        used.update(inside)
        new_spans += spans
    spans = sorted([sp for i, sp in enumerate(old) if i not in used] + new_spans,
                   key=lambda s: (s["start_s"], s["end_s"]))
    doc = {**names_doc, "spans": spans, "pod_jev": {
        "piece_s": kw.get("piece_s", PIECE_S), "none_policy": kw.get("none_policy", "join"),
        "runs": rep["runs"], "renamed": rep["renamed"]}}
    return doc, rep


# ---------------------------------------------------------------- spelling

_WORD = re.compile(r"[a-z0-9'&-]+")


_TLD_END = re.compile(r"(?:\.|\s+dot\s+)(?:com|net|org|edu|io|co|fm|ai|app|tv|me|us)\b")


def _written_ok(brand: str, new: str) -> bool:
    """A respelling keeps a written TLD and never splits a name into
    syllable fragments ("sie r-r-a")."""
    if _TLD_END.search(brand.lower()) and not _TLD_END.search(new.lower()):
        return False
    return "-" in brand or "-" not in new


def respell(brand: str, episode_text_l: str = "", sponsors=(), *, min_len: int = 5,
            written=()) -> tuple:
    """(spelling, rule) for a named brand, only ever a spelling that a
    source writes: a show-notes sponsor that is the same brand (compact
    form equal, or one spelling of the other by bn._similar) gives its
    spelling ("sponsor"); else a written spelling (`written`: e.g. the
    advertiser list's names) with the same letters ("written"). Never a
    syllable split or a dropped TLD; names shorter than `min_len` never
    change; (brand, None) when no rule applies. (`episode_text_l` is no
    longer used: the spoken spacing is no written spelling.)"""
    if not brand:
        return brand, None
    c = bn.compact(brand)
    if len(c) < min_len:
        return brand, None
    for s in sponsors or ():
        sc = bn.compact(s or "")
        if len(sc) < min_len:
            continue
        if sc == c or bn._similar(sc, c):  # the notes' written form wins
            new = s.strip().lower()
            return (new, "sponsor") if _written_ok(brand, new) else (brand, None)
    for w in written or ():
        new = str(w or "").strip().lower()
        if bn.compact(new) == c and new != brand.strip().lower() and _written_ok(brand, new):
            return new, "written"
    return brand, None


def _product_of(obj: dict):
    """The product phrase a span's brand was collapsed from (candidate
    evidence "product:<phrase>" on the candidate naming the brand)."""
    k = _key(obj.get("brand"))
    for c in obj.get("candidates") or ():
        if _key(c.get("brand")) == k:
            for e in c.get("evidence") or ():
                if str(e).startswith("product:"):
                    return str(e)[len("product:"):]
    return None


def respell_doc(names_doc: dict, segments, sponsors=(), advertisers=None,
                protected=frozenset(), stopped=None, **kw) -> tuple:
    """(doc, changes) with every span brand (and second brand) respelled:
    with an advertiser list, a product phrase collapses to its brand
    (kept as `product`) and the name maps onto the list first; then
    respell (show-notes spelling, the list's written spelling)."""
    text_l = " ".join(str(s.get("text") or "") for s in segments or ()).lower()
    written = [e["name"] for e in advertisers.doc.get("entries") or ()] \
        if advertisers is not None else []
    changes = []

    def fix(obj):
        before = obj.get("brand")
        product = _product_of(obj)
        if advertisers is not None and before:
            heads = [c["brand"] for c in obj.get("candidates") or ()
                     if "domain" in (c.get("evidence") or ())]
            name, rule, prod = advertisers.canonical(before, heads)
            if stopped is not None and stopped(name):
                name, rule, prod = before, None, None   # never onto a stoplisted name
            product = product or prod
            if rule:
                changes.append({"from": before, "to": name, "rule": rule})
                obj["brand"] = name
        new, rule = respell(obj.get("brand"), text_l, sponsors, written=written, **kw)
        if rule and stopped is not None and stopped(new):
            rule = None
        if rule:
            changes.append({"from": obj["brand"], "to": new, "rule": rule})
            obj["brand"] = new
        if obj.get("brand") != before:
            obj["brand_spelled_from"] = before
        if product:
            obj["product"] = product
    spans = []
    for sp in names_doc.get("spans") or ():
        if sp.get("span_id") in protected:  # a person confirmed it
            spans.append(sp)
            continue
        sp = dict(sp)
        fix(sp)
        sbs = []
        for sb in sp.get("second_brands") or ():
            sb = dict(sb)
            fix(sb)
            sbs.append(sb)
        sp["second_brands"] = sbs
        spans.append(sp)
    return {**names_doc, "spans": spans}, changes


def sponsors_of(corpus, episode_id: str, *, written_only: bool = False) -> list:
    """Show-notes sponsor names for an episode from
    <corpus>/shownotes-reads/<ep>.json (written by the show-notes pass),
    [] when absent. Candidates only: a listing is not proof the ad ran.
    written_only: only names as the notes write them (spelling targets),
    not the compact keys of listings that carry no written name."""
    p = Path(corpus) / "shownotes-reads" / f"{episode_id}.json"
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for s in doc.get("sponsors") or ():
        if isinstance(s, dict):
            name = s.get("name") or (None if written_only else s.get("brand"))
            if isinstance(name, str) and name.strip():
                out.append(name.strip())
    return out
