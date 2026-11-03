"""Re-name an existing corpus: run the name stage again from the corpus's
transcripts and Jev results, with the current namer rules (merge rule,
second brands) and a chosen brand list. No audio, no speech model, no API.

For each episode (stage `done` only, unless asked otherwise) with a
transcript and a Jev result:

1. the first-pass text is named afresh (bn.name_episode), keeping the
   threshold and context windows the episode was named with;
2. an episode whose names doc is refined ("refined": true) is then re-named
   on its refined timeline from refined/<ep>.json exactly as `refine` does
   (refine.rename: the new first-pass spans keep their ranges, the refined
   name is accepted or the first-pass one kept by the same rules), so
   `refine` still sees it as finished;
3. names/<ep>.json is replaced atomically only when the result differs
   (ignoring timestamps), so a rerun changes nothing.

Downstream of names: export computes self-promo flags, voice attrs and
named-read candidates from names/ each time, so they follow with no extra
step; the brand canon map (brand-canon.json) is built from names/, so it
goes stale and must be rebuilt (`rename --canon` or `canon --apply`).

Live corpora: like `refine`, it takes no leases and writes no queue state.
Only `done` episodes are touched by default (the name worker writes names
only for episodes at stage `name`). A names file that changes while its
episode is being re-named (e.g. by a `refine` pass) is left alone and
counted as changed_meanwhile (check and write under the lock that refine
also takes, rf.replace_if_unchanged).

A re-name can change span ranges (merge rule), and with them the span ids
of named reads. The app keys a named read on episode + span + transcript
version, so re-name before the app's first real load, or expect re-named
spans to arrive as new evidence.
"""
from __future__ import annotations

import json
from pathlib import Path

from earwitness.pipeline import brand_namer as bn
from earwitness.pipeline import corpus as cr
from earwitness.pipeline import cue_spans
from earwitness.pipeline import jev_screen
from earwitness.pipeline import pod_naming as pod
from earwitness.pipeline import promos
from earwitness.pipeline import refine as rf
from earwitness.pipeline import self_promo
from earwitness.pipeline import spelling as spl

VOLATILE = ("named_at", "renamed_at")


class BrandListMissing(LookupError):
    """The brand list an episode was named with is not archived."""


def _stable(doc):
    """`doc` without timestamps (top level and its `previous`)."""
    if not isinstance(doc, dict):
        return doc
    out = {k: v for k, v in doc.items() if k not in VOLATILE}
    if isinstance(out.get("previous"), dict):
        out["previous"] = _stable(out["previous"])
    return out


CORPUS_POD_CONFIG = "pod-config.json"   # the config of the last flagged rename


def corpus_config(corpus):
    """The corpus-level pod config (written by a rename with pod / round-3
    flags), or None."""
    doc = cr._read_json(Path(corpus) / CORPUS_POD_CONFIG)
    return doc if isinstance(doc, dict) and doc else None


def write_corpus_config(corpus, cfg: dict) -> None:
    path = Path(corpus) / CORPUS_POD_CONFIG
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


_LISTS = ("unknown", "not_done")


def merge_reports(reps: list) -> dict:
    """One per-corpus report from the reports of its config groups: counts
    summed (nested counts too), episode lists joined, the pod Jev report of
    the shared ask (the last one), anything else from the first."""
    if len(reps) == 1:
        return reps[0]
    out = dict(reps[0])
    for r in reps[1:]:
        for k, v in r.items():
            if k == "pod":
                pod = dict(out.get("pod") or {})
                for kk, vv in v.items():
                    pod[kk] = vv if kk == "jev" or not isinstance(vv, int) \
                        else pod.get(kk, 0) + vv
                out["pod"] = pod
            elif k in _LISTS:
                out[k] = list(out.get(k) or []) + list(v)
            elif isinstance(v, bool) or not isinstance(v, (int, float, dict)):
                out.setdefault(k, v)
            elif isinstance(v, dict):
                d = dict(out.get(k) or {})
                for kk, vv in v.items():
                    d[kk] = d.get(kk, 0) + vv if isinstance(vv, (int, float)) else vv
                out[k] = d
            else:
                out[k] = out.get(k, 0) + v
    out["config_groups"] = len(reps)
    return out


def recorded_configs(corpus, episodes=None, default=None) -> list:
    """[(pod_config or None, [episode ids])]: the episodes of `corpus` (or
    of `episodes`) grouped by the pod / round-3 options their names doc
    records, in first-seen order. A doc with no record (or no doc) takes
    `default` (the corpus-level config; None: none)."""
    names = cr.corpus_dirs(corpus)["names"]
    con = cr.connect(corpus)
    try:
        cr.ensure_schema(con)
        ids = [r[0] for r in con.execute("SELECT episode_id FROM episodes ORDER BY episode_id")]
    finally:
        con.close()
    if episodes is not None:
        want = set(episodes)
        ids = [e for e in ids if e in want] + [e for e in dict.fromkeys(episodes)
                                               if e not in set(ids)]
    groups: dict = {}
    for ep in ids:
        doc = cr._read_json(names / f"{ep}.json")
        cfg = (doc.get("pod_config") if isinstance(doc, dict) else None) or default
        k = json.dumps(cfg, sort_keys=True) if cfg else ""
        groups.setdefault(k, (cfg or None, []))[1].append(ep)
    return list(groups.values())


def namer_factory(corpus, *, brand_list=None, gliner: str = "small",
                  threads: int | None = None, write: bool = True, log=print,
                  advertisers=None, corrections=None):
    """namer_for(names_doc, refined) -> BrandNamer for rename_corpus.

    brand_list None: harvest from the corpus now, as a name worker start
    does (first pass: brand-list.json; refined text: refine's
    brand-list-refined.json harvest), built once, on first use.
    brand_list "recorded": the archived list whose digest the episode's
    names doc records; corpora named before the archive existed fall back
    to brand-list.json / brand-list-refined.json when its digest is the
    recorded one (archived then, unless a dry run); BrandListMissing when
    neither holds it. namer_for.recorded_from counts where each recorded
    list came from ("archive" or the file name).
    brand_list PATH: that JSON list for both texts.
    write False (dry run): harvested lists are not written or archived.
    advertisers: an advertisers.Advertisers every namer repairs with first
    (None: off).
    corrections (corrections.Corrections): the operator's rules join every
    namer (None: names exactly as without them)."""
    corpus = Path(corpus)
    g = None if gliner == "none" else gliner
    cache: dict = {}
    sources: dict = {}
    recorded_from: dict = {}

    def recorded(sha):
        blist = bn.load_archived_brand_list(corpus, sha)
        if blist is not None:
            return blist, "archive"
        for name in (cr.BRAND_LIST_NAME, rf.REFINED_BRAND_LIST_NAME):
            try:
                cand = json.loads((corpus / name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(cand, list) and bn.brand_list_digest(cand) == sha:
                if write:
                    bn.archive_brand_list(corpus, cand)
                log(f"brand list {sha[:12]}: not archived; using {name} (same digest)")
                return cand, name
        return None, None

    def harvested(refined: bool) -> bn.BrandNamer:
        if write:
            build = rf.build_refine_namer if refined else cr.build_namer
            return build(corpus, gliner=gliner, threads=threads, log=log,
                         corrections=corrections)
        if refined:
            blist = bn.harvest_brand_list(episodes=rf.refined_timelines(corpus),
                                          trusted=rf.trusted_text(corpus))
        else:
            blist = bn.harvest_brand_list(sorted(cr.corpus_dirs(corpus)["transcripts"]
                                                 .glob("*.json")))
        return bn.BrandNamer(blist, g, threads=threads, corrections=corrections)

    def namer_for(doc, refined: bool) -> bn.BrandNamer:
        if brand_list == "recorded":
            sha = ((doc or {}).get("brand_list") or {}).get("sha256")
            if sha not in cache:
                blist, src = recorded(sha) if sha else (None, None)
                if blist is None:
                    raise BrandListMissing(f"brand list {sha} is not in "
                                           f"{corpus / bn.BRAND_LISTS_DIR} nor in "
                                           f"{cr.BRAND_LIST_NAME} / "
                                           f"{rf.REFINED_BRAND_LIST_NAME}")
                cache[sha] = bn.BrandNamer(blist, g, threads=threads, corrections=corrections)
                sources[sha] = src
            recorded_from[sources[sha]] = recorded_from.get(sources[sha], 0) + 1
            return cache[sha]
        key = "file" if brand_list else ("refined" if refined else "first")
        if key not in cache:
            if brand_list:
                cache[key] = cr.build_namer(corpus, gliner=gliner, brand_list=brand_list,
                                            threads=threads, log=log,
                                            corrections=corrections) if write else \
                    bn.BrandNamer(json.loads(Path(brand_list).read_text(encoding="utf-8")), g,
                                  threads=threads, corrections=corrections)
            else:
                cache[key] = harvested(refined)
        return cache[key]

    def namer_with_list(doc, refined: bool) -> bn.BrandNamer:
        n = namer_for(doc, refined)
        n.advertisers = advertisers
        return n

    namer_with_list.recorded_from = recorded_from
    return namer_with_list


def _round3(doc, rep, ep_id, segments, jev, corpus, show, namer, *, cue_kinds, cue_min_noul,
            promo_shows, own_network_self_promo, spelling, threshold,
            protected=frozenset()) -> dict:
    """The round-3 passes on one episode's new names doc (rename_corpus)."""
    title, feed = show
    ident = self_promo.load_identity(Path(corpus) / self_promo.IDENTITY_DIR, feed)
    own = promos.own_keys(title, ident)
    if cue_kinds:
        doc, added = cue_spans.add_cue_spans(doc, segments, jev, namer, kinds=tuple(cue_kinds),
                                             min_noul=cue_min_noul, shows=promo_shows,
                                             own=own, threshold=threshold)
        doc["cue_spans"] = {"kinds": sorted(cue_kinds), "min_noul": cue_min_noul}
        cs = rep.setdefault("cue_spans", {})
        for sp in added:
            cs[sp["method"]] = cs.get(sp["method"], 0) + 1
    if promo_shows is not None:
        corr = getattr(namer, "corrections", None)
        doc, ch = promos.name_promos(doc, segments, promo_shows, own=own,
                                     networks=promos.own_networks(ident, own),
                                     self_promo_own_network=own_network_self_promo,
                                     protected=protected,
                                     stopped=corr.stopped if corr is not None else None)
        doc["promo_shows"] = {**promo_shows.describe(),
                              "own_network_self_promo": own_network_self_promo}
        rep["promos"] = rep.get("promos", 0) + len(ch)
    if spelling is not None:
        doc, ch = spl.respell_doc(doc, spelling, protected=protected)
        doc["display_spelling"] = spelling.describe()
        rep["spelled"] = rep.get("spelled", 0) + len(ch)
    return doc


def rename_corpus(corpus, *, namer_for, episodes=None, any_stage: bool = False,
                  dry_run: bool = False, threshold: float | None = None,
                  context_windows: int | None = None,
                  merge_rule: str = bn.DEFAULT_MERGE_RULE,
                  second_brand_min_score: float | None = bn.SECOND_BRAND_MIN_SCORE,
                  max_span_windows: int | None = bn.MAX_SPAN_WINDOWS,
                  sub_window_s: float | None = cr.DEFAULT_NAME_SUB_WINDOW_S,
                  pod_ask=None, pod_options: dict | None = None,
                  pod_shownotes: bool = False, respell: bool = False,
                  cue_kinds=(), cue_min_noul: float = cue_spans.MIN_NOUL,
                  promo_shows=None, promo_self_promo_own_network: bool = False,
                  spelling=None, pod_config: dict | None = None, pod_strict: bool = False,
                  log=print) -> dict:
    """Re-name the episodes of `corpus` (see the module doc). `episodes`
    limits it to those ids; `any_stage` also takes episodes not yet done
    (a live name worker may then overwrite them). threshold /
    context_windows: None keeps each episode's recorded value; sub_window_s
    (None: off) applies to every episode, as the merge rule does.

    Round-3 passes, each off by default, run after the pod pass and the
    respelling, in this order: cue_kinds (cue_spans.KINDS) adds cue spans
    for short reads the Jev windows did not flag (windows >= cue_min_noul);
    promo_shows (promos.ShowTitles) names network promos after the
    promoted show (promo_self_promo_own_network: a promo for the show's own
    network becomes self-promo at export); spelling (spelling.Spelling)
    writes every brand in its display spelling. Each records itself in the
    names doc (so turning one on or off re-names). pod_config (the options
    as scripts/corpus_run.py records them; None: off) is written to each
    names doc, so a later rename without flags can reuse them
    (recorded_configs). pod_strict (reuse): an episode whose pod pass gets
    no answer for a run (no Jev, nothing cached) is left as it was and
    counted as skipped_pod_unavailable, never named without it."""
    if merge_rule not in bn.MERGE_RULES:
        raise ValueError(f"merge rule must be one of {bn.MERGE_RULES}")
    corpus = Path(corpus)
    dirs = cr.corpus_dirs(corpus)
    if not dry_run:
        bn.check_names_lock(dirs["names"])
    con = cr.connect(corpus)
    try:
        cr.ensure_schema(con)
        rows = [tuple(r) for r in con.execute(
            "SELECT episode_id, stage, show_title, feed_url FROM episodes ORDER BY episode_id")]
    finally:
        con.close()
    show = {r[0]: (r[2], r[3]) for r in rows}
    rows = [r[:2] for r in rows]
    stage = dict(rows)
    want = None if episodes is None else list(dict.fromkeys(episodes))
    ids = [e for e, _ in rows] if want is None else [e for e in want if e in stage]
    not_done = [e for e in ids if stage[e] != "done"]
    todo = ids if any_stage else [e for e in ids if stage[e] == "done"]
    rep = {"corpus": str(corpus), "dry_run": dry_run, "merge_rule": merge_rule,
           "second_brand_min_score": second_brand_min_score,
           "max_span_windows": max_span_windows,
           **({"sub_window_s": sub_window_s} if sub_window_s else {}),  # as the runner when off
           "episodes": len(todo),
           "renamed": 0, "unchanged": 0, "refined": 0, "failed": 0,
           "skipped": {"no_transcript": 0, "no_jev": 0, "refined_text_missing": 0,
                       "brand_list_missing": 0, "changed_meanwhile": 0,
                       "requeued_meanwhile": 0},
           "spans_before": 0, "spans_after": 0, "second_brands": 0, "brands_changed": 0,
           "ranges_changed": 0}
    if want is not None:
        rep["unknown"] = [e for e in want if e not in stage]
    if not any_stage and want is not None:
        rep["not_done"] = not_done
    for i, ep_id in enumerate(todo, 1):
        tr = cr._read_json(dirs["transcripts"] / f"{ep_id}.json")
        segs = tr.get("segments") if isinstance(tr, dict) else None
        if not isinstance(segs, list):
            rep["skipped"]["no_transcript"] += 1
            continue
        jev = cr._read_json(dirs["jev"] / f"{ep_id}.json")
        if bn.jev_windows(jev) is None:
            rep["skipped"]["no_jev"] += 1
            continue
        path = dirs["names"] / f"{ep_id}.json"
        raw = path.read_bytes() if path.is_file() else None
        try:
            old = json.loads(raw) if raw is not None else None
        except ValueError:
            old = None
        old = old if isinstance(old, dict) else None
        refined = bool(old and old.get("refined") is True)
        first = (old.get("previous") if refined else old) or {}
        thr = threshold if threshold is not None else first.get(
            "jev_threshold", jev_screen.DEFAULT_THRESHOLD)
        cw = context_windows if context_windows is not None else first.get(
            "context_windows", cr.DEFAULT_JEV_CONTEXT_WINDOWS)
        ref = None
        if refined:
            ref = cr._read_json(dirs["refined"] / f"{ep_id}.json")
            if not isinstance(ref, dict) or not ref.get("clips"):
                rep["skipped"]["refined_text_missing"] += 1
                continue
        try:
            first_namer = namer_for(first or None, False)
            new = bn.name_episode(ep_id, segs, jev, first_namer,
                                  threshold=thr, context_windows=cw, merge_rule=merge_rule,
                                  second_brand_min_score=second_brand_min_score,
                                  max_span_windows=max_span_windows, sub_window_s=sub_window_s)
            if refined:
                new = rf.rename(ep_id, new, jev, segs, ref, namer_for(old, True),
                                model=old.get("refine_model"), threshold=thr,
                                context_windows=cw, merge_rule=merge_rule,
                                second_brand_min_score=second_brand_min_score,
                                max_span_windows=max_span_windows, sub_window_s=sub_window_s)
            keep = bn.protected_spans(first_namer, ep_id)  # spans a person confirmed
            if pod_ask is not None or respell:
                best = cr.refined_segments(segs, ref) if refined else segs
                sponsors = pod.sponsors_of(corpus, ep_id) if pod_shownotes else []
                adv = getattr(first_namer, "advertisers", None)
                if pod_ask is not None:
                    new, prep = pod.pod_rename(new, best, jev, namer_for(old if refined else (
                        first or None), refined), pod_ask, threshold=thr, extra=sponsors,
                        protected=keep, **(pod_options or {}))
                    log(f"[rename] {ep_id} pod: " + json.dumps(prep["runs_info"]))
                    if pod_strict and any(i.get("kept") == "no response"
                                          for i in prep["runs_info"]):
                        rep["skipped_pod_unavailable"] = rep.get("skipped_pod_unavailable",
                                                                 0) + 1
                        log(f"[rename] {ep_id}: recorded pod naming unavailable (no answer for "
                            "a run); left as it was")
                        continue
                    pr = rep.setdefault("pod", {"runs": 0, "renamed": 0, "kept": 0})
                    for k in pr:
                        pr[k] += prep[k]
                if respell:
                    written = pod.sponsors_of(corpus, ep_id, written_only=True) \
                        if pod_shownotes else []
                    corr = getattr(first_namer, "corrections", None)
                    new, ch = pod.respell_doc(new, best, written, advertisers=adv,
                                              protected=keep,
                                              stopped=corr.stopped if corr is not None else None)
                    rep["respelled"] = rep.get("respelled", 0) + len(ch)
            if cue_kinds or promo_shows is not None or spelling is not None:
                best = cr.refined_segments(segs, ref) if refined else segs
                new = _round3(new, rep, ep_id, best, jev, corpus, show.get(ep_id) or (None, None),
                              namer_for(old if refined else (first or None), refined),
                              cue_kinds=cue_kinds, cue_min_noul=cue_min_noul,
                              promo_shows=promo_shows,
                              own_network_self_promo=promo_self_promo_own_network,
                              spelling=spelling, threshold=thr, protected=keep)
        except pod.PodJevBudget as exc:
            rep["failed"] += 1
            rep["pod_budget_reached"] = str(exc)
            log(f"[rename] {ep_id}: {exc}; stopping")
            break
        except BrandListMissing as exc:
            rep["skipped"]["brand_list_missing"] += 1
            log(f"[rename] {i}/{len(todo)} {ep_id} skipped: {exc}")
            continue
        except Exception as exc:  # one bad episode never stops the pass
            rep["failed"] += 1
            log(f"[rename] {i}/{len(todo)} {ep_id} FAILED: {type(exc).__name__}: {exc}"[:500])
            continue
        if pod_config:
            new["pod_config"] = pod_config
        if old is not None and _stable(new) == _stable(old):
            rep["unchanged"] += 1
            continue
        before = (old or {}).get("spans") or []
        after = new["spans"]
        rep["spans_before"] += len(before)
        rep["spans_after"] += len(after)
        rep["second_brands"] += sum(len(sp.get("second_brands") or ()) for sp in after)
        rep["brands_changed"] += sorted((s["span_id"], s.get("brand")) for s in before) != \
            sorted((s["span_id"], s.get("brand")) for s in after)
        rep["ranges_changed"] += bool(before) and sorted(s["span_id"] for s in before) != \
            sorted(s["span_id"] for s in after)
        rep["refined"] += refined
        log(f"[rename] {i}/{len(todo)} {ep_id}{' (refined)' if refined else ''}: " + (", ".join(
            f"{sp['brand'] or '?'}{'?' if sp['needs_review'] else ''}"
            + "".join(f"+{x['brand']}" for x in sp.get("second_brands") or ())
            for sp in after) or "no flagged windows"))
        if dry_run:
            rep["renamed"] += 1
            continue
        if not any_stage and not rf._still_done(corpus, ep_id):  # e.g. repair-seams
            rep["skipped"]["requeued_meanwhile"] += 1
            log(f"[rename] {ep_id}: requeued meanwhile; left alone")
            continue
        now = cr.utcnow()
        if not rf.replace_if_unchanged(path, raw, {**new, "named_at": now, "renamed_at": now}):
            rep["skipped"]["changed_meanwhile"] += 1
            log(f"[rename] {ep_id}: names file changed meanwhile; left alone")
            continue
        rep["renamed"] += 1
    if pod_ask is not None:
        rep.setdefault("pod", {})["jev"] = pod_ask.report()
    rf_from = getattr(namer_for, "recorded_from", None)
    if rf_from is not None:
        rep["brand_list_recorded_from"] = dict(rf_from)
    return rep
