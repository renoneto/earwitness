"""Operator step: record an episode as an explicit alias of the canonical
episode with the same audio (decision 3, 2026-10-16: bench_3 -> s003e0).

The alias mechanism is the one the seed import uses for bench_0 -> s000e0
(import_sources._build_seed_plan): episodes.alias_of = target and
kind = 'supplied-alias'. Customer views already collapse such aliases
(customer._episode_scope, _fold_alias_twin_reads, the show map). The seed
records an alias only for an exact transcript match; a benchmark twin
transcribed in another run of the same audio never matches exactly, so
this step also accepts the same audio proven by the same show and episode
title, the same known duration, a word match of at least MIN_WORD_MATCH
with a stored copy of the target's transcript, and every read of the twin
overlapping a read of the same source on the target. Anything else is
refused, and so are swapped arguments: the canonical episode is the
supplied / feed-resolved one, never its benchmark twin.

One write transaction: the episode row (revision + 1, history snapshot,
source publication), the twin's show mapping set to its target's show
(basis alias_of, as the show map records bench_0), one audit row
(action episode_alias) carrying before/after and the measured basis.
Transcripts, evidence, reviews and media are never touched. A later seed
reimport keeps the recorded alias (import_sources._operator_aliases reads
the audit row). A dry run
returns the same plan and writes nothing, and lists what references the
alias episode (dependents) so the operator sees it; an already recorded
alias is a no-op.
"""
from __future__ import annotations

import difflib
import json
import re
import sqlite3

from . import db
from .import_sources import canonical_transcript_text

ALIAS_KIND = "supplied-alias"
AUDIT_ACTION = "episode_alias"
# Operator-side safety check only: it can only refuse a record-alias run;
# it is not a product rule (customer views decide twins by alias_of and
# spans alone, customer._fold_alias_twin_reads). Measured 2026-10-16 on
# the owner data: bench_3 vs s003e0 (same audio, two transcription runs)
# 0.970; the closest different cut, bench_2 vs s002e0, 0.943 (and a
# different duration).
MIN_WORD_MATCH = 0.95


class AliasRefused(ValueError):
    """The twin is not provably the same audio as the target."""


def _words(text: str) -> list[str]:
    return re.sub(r"[^\w ]+", "", text.lower()).split()


def _payload(con, version_id):
    row = con.execute("SELECT payload FROM source_versions WHERE id=?",
                      (version_id,)).fetchone() if version_id is not None else None
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) and isinstance(payload.get("segments"), list) else None


def _target_copies(con, target: dict) -> list[int]:
    """Every stored copy of the target's transcript, current or earlier."""
    ids = [r["id"] for r in con.execute(
        "SELECT id FROM source_versions WHERE source_key=? ORDER BY id",
        (f"transcript:{target['id']}",))]
    if target.get("transcript_version_id") and target["transcript_version_id"] not in ids:
        ids.append(target["transcript_version_id"])
    return ids


def _transcript_basis(con, alias: dict, target: dict) -> dict:
    mine = _payload(con, alias.get("transcript_version_id"))
    if mine is None:
        raise AliasRefused(f"{alias['id']} has no stored transcript")
    copies = [(v, p) for v in _target_copies(con, target) if (p := _payload(con, v)) is not None]
    if not copies:
        raise AliasRefused(f"target {target['id']} has no stored transcript")
    text = canonical_transcript_text(mine)
    for version, payload in copies:
        if canonical_transcript_text(payload) == text:
            return {"rule": "exact-text", "word_match": 1.0, "target_copy": version,
                    "alias_copy": alias["transcript_version_id"]}
    words = _words(text)
    best_version, best = None, -1.0
    for version, payload in copies:
        ratio = difflib.SequenceMatcher(
            None, words, _words(canonical_transcript_text(payload)), autojunk=False).ratio()
        if ratio > best:
            best_version, best = version, ratio
    if best < MIN_WORD_MATCH:
        raise AliasRefused(
            f"transcript differs: {alias['id']} matches {best:.3f} of the words of"
            f" {target['id']} copy {best_version} (needs {MIN_WORD_MATCH})")
    return {"rule": "same-audio-transcript", "word_match": round(best, 4),
            "target_copy": best_version, "alias_copy": alias["transcript_version_id"],
            "min_word_match": MIN_WORD_MATCH}


def _overlap(a, b) -> float:
    if None in (*a, *b):
        return 0.0
    return min(a[1], b[1]) - max(a[0], b[0])


def _read_pairs(con, alias_id: str, target_id: str) -> list[dict]:
    """Each twin read paired with the target read of the same source and
    kind it overlaps most (one to one); a twin read without one refuses."""
    q = ("SELECT id, source, record_kind, orig_start_s, orig_end_s FROM evidence"
         " WHERE episode_id=? ORDER BY orig_start_s, id")
    theirs = [dict(r) for r in con.execute(q, (target_id,))]
    taken, pairs = set(), []
    for read in (dict(r) for r in con.execute(q, (alias_id,))):
        span = (read["orig_start_s"], read["orig_end_s"])
        best, best_overlap = None, 0.0
        for other in theirs:
            if other["id"] in taken or (other["source"], other["record_kind"]) != (
                    read["source"], read["record_kind"]):
                continue
            overlap = _overlap(span, (other["orig_start_s"], other["orig_end_s"]))
            if overlap > best_overlap:
                best, best_overlap = other, overlap
        if best is None:
            raise AliasRefused(f"reads differ: {alias_id} read {read['id']} has no"
                               f" overlapping {read['source']} read on {target_id}")
        taken.add(best["id"])
        pairs.append({"alias_read": read["id"], "target_read": best["id"],
                      "span": list(span)})
    return pairs


def _mapping(con, episode_id):
    row = con.execute(
        "SELECT show_identity_id, basis, source_version_id FROM customer_episode_shows"
        " WHERE workspace_id='local' AND episode_id=?", (episode_id,)).fetchone()
    return dict(row) if row else None


def _refuse_swapped(con, alias: dict, target: dict) -> None:
    """The canonical episode is the supplied / feed-resolved one: refuse
    recording it as an alias of an episode that is neither."""
    if alias["kind"] == "supplied" and target["kind"] != "supplied":
        raise AliasRefused(
            f"{alias['id']} is supplied and {target['id']} is {target['kind']!r}: the"
            f" canonical episode is the supplied one; swap --alias and --target")
    mine, theirs = _mapping(con, alias["id"]), _mapping(con, target["id"])
    if (mine or {}).get("basis") == "feed_url" and (theirs or {}).get("basis") != "feed_url":
        raise AliasRefused(
            f"{alias['id']} has a feed_url show identity and {target['id']} does not: the"
            f" canonical episode is the feed one; swap --alias and --target")


def _table(con, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (name,)).fetchone() is not None


def _references(value, keys: set) -> bool:
    """Whether any string in a stored JSON value names one of keys (an
    interval ref sr1:<id>:w:a:b counts as its publication sr1:<id>)."""
    if isinstance(value, str):
        return value in keys or value.split(":w:")[0] in keys
    if isinstance(value, dict):
        return any(_references(v, keys) for v in value.values())
    if isinstance(value, list):
        return any(_references(v, keys) for v in value)
    return False


def _json_refs(text, keys: set) -> bool:
    try:
        return _references(json.loads(text or "null"), keys)
    except (TypeError, ValueError):
        return False


def _dependents(con, alias_id: str, mine: dict | None, theirs: dict | None) -> dict:
    """What references the alias episode, for the operator to see before
    recording: its id, its reads, their and its publications, and its own
    show identity when that show differs from the target's (the alias
    leaves it). Reads only; nothing here blocks the alias."""
    evidence = [r["id"] for r in con.execute(
        "SELECT id FROM evidence WHERE episode_id=? ORDER BY id", (alias_id,))]
    keys = {alias_id, *evidence}
    if _table(con, "source_states"):
        keys |= {r["id"] for r in con.execute(
            "SELECT id FROM source_states WHERE workspace_id='local' AND"
            " ((kind='episode' AND record_id=?) OR (kind='evidence' AND record_id IN"
            f" ({','.join('?' * len(evidence))})))", (alias_id, *evidence))}
    show = mine["show_identity_id"] if mine and (
        theirs is None or mine["show_identity_id"] != theirs["show_identity_id"]) else None
    if show:
        keys.add(show)
    out = {"aliased_episodes": [r["id"] for r in con.execute(
        "SELECT id FROM episodes WHERE alias_of=? ORDER BY id", (alias_id,))],
        "show_map": [], "campaigns": [], "placements": [], "campaign_targets": [],
        "alerts": [], "saved_items": [], "saved_searches": []}
    if show:
        out["show_map"] = [
            {"episode": r["episode_id"], "show_identity_id": r["show_identity_id"],
             "basis": r["basis"]} for r in con.execute(
                "SELECT episode_id, show_identity_id, basis FROM customer_episode_shows"
                " WHERE workspace_id='local' AND show_identity_id=? ORDER BY episode_id",
                (show,))]
    campaigns = set()
    if _table(con, "campaigns"):
        campaigns |= {r["id"] for r in con.execute("SELECT id, target_shows FROM campaigns")
                      if _json_refs(r["target_shows"], keys)}
        campaigns |= {r["campaign_id"] for r in con.execute(
            "SELECT campaign_id, evidence_id FROM campaign_evidence") if r["evidence_id"] in keys}
    for table, attach, out_key in (("expected_placements", "placement_evidence", "placements"),
                                   ("campaign_targets", "target_evidence", "campaign_targets")):
        if not _table(con, table):
            continue
        id_col = "placement_id" if attach == "placement_evidence" else "target_id"
        attached = {r[id_col] for r in con.execute(f"SELECT {id_col}, ref FROM {attach}")
                    if _references(r["ref"], keys)} if _table(con, attach) else set()
        hits = [r for r in con.execute(
            f"SELECT id, campaign_id, episode_id, show_id FROM {table} ORDER BY id")
            if r["id"] in attached or r["episode_id"] in keys or r["show_id"] in keys]
        out[out_key] = [r["id"] for r in hits]
        campaigns |= {r["campaign_id"] for r in hits}
    out["campaigns"] = sorted(campaigns)
    if _table(con, "customer_alerts"):
        out["alerts"] = [{"id": r["id"], "name": r["name"]} for r in con.execute(
            "SELECT id, name, query_json FROM customer_alerts ORDER BY id")
            if _json_refs(r["query_json"], keys)]
    if _table(con, "customer_saved_items"):
        out["saved_items"] = [r["id"] for r in con.execute(
            "SELECT id, target_json, query_json FROM customer_saved_items ORDER BY id")
            if _json_refs(r["target_json"], keys) or _json_refs(r["query_json"], keys)]
    if _table(con, "customer_saved_searches"):
        out["saved_searches"] = [{"id": r["id"], "name": r["name"]} for r in con.execute(
            "SELECT id, name, query_json FROM customer_saved_searches ORDER BY id")
            if _json_refs(r["query_json"], keys)]
    return out


def plan(con: sqlite3.Connection, alias_id: str, target_id: str) -> dict:
    """Validate and describe the change; reads only."""
    if alias_id == target_id:
        raise AliasRefused("an episode cannot be an alias of itself")
    rows = {r["id"]: dict(r) for r in con.execute(
        "SELECT * FROM episodes WHERE id IN (?, ?)", (alias_id, target_id))}
    alias, target = rows.get(alias_id), rows.get(target_id)
    if alias is None:
        raise AliasRefused(f"episode {alias_id} does not exist")
    if target is None:
        raise AliasRefused(f"target episode {target_id} does not exist")
    if target.get("alias_of"):
        raise AliasRefused(f"target {target_id} is itself an alias of {target['alias_of']}")
    if alias.get("alias_of") and alias["alias_of"] != target_id:
        raise AliasRefused(f"{alias_id} is already an alias of {alias['alias_of']}")
    chained = [r["id"] for r in con.execute(
        "SELECT id FROM episodes WHERE alias_of=? ORDER BY id", (alias_id,))]
    if chained:
        raise AliasRefused(f"{alias_id} is the target of {', '.join(chained)}; aliases do not chain")
    _refuse_swapped(con, alias, target)
    for field in ("show_title", "ep_title"):
        if (alias.get(field) or "").strip() != (target.get(field) or "").strip():
            raise AliasRefused(f"{field} differs: {alias.get(field)!r} vs {target.get(field)!r}")
    if alias.get("duration_min") is None or alias.get("duration_min") != target.get("duration_min"):
        raise AliasRefused(f"duration differs or is unknown: {alias.get('duration_min')}"
                           f" vs {target.get('duration_min')} min")
    basis = _transcript_basis(con, alias, target)
    pairs = _read_pairs(con, alias_id, target_id)
    episode = {"before": {"kind": alias["kind"], "alias_of": alias.get("alias_of")},
               "after": {"kind": ALIAS_KIND, "alias_of": target_id}}
    mine, theirs = _mapping(con, alias_id), _mapping(con, target_id)
    show_map = {"before": ({k: mine[k] for k in ("show_identity_id", "basis")} if mine else None),
                "after": ({"show_identity_id": theirs["show_identity_id"], "basis": "alias_of"}
                          if theirs else None)}
    return {"alias": alias_id, "target": target_id,
            "changed": episode["before"] != episode["after"] or (
                theirs is not None and show_map["before"] != show_map["after"]),
            "episode": episode, "show_map": show_map, "basis": basis, "reads": pairs,
            "dependents": _dependents(con, alias_id, mine, theirs),
            "_target_mapping_version": theirs["source_version_id"] if theirs else None}


def record(con: sqlite3.Connection, alias_id: str, target_id: str, *,
           actor: str, dry_run: bool = False) -> dict:
    """Plan and (unless dry_run or already recorded) apply in one transaction."""
    with db.transaction(con, write=not dry_run):
        p = plan(con, alias_id, target_id)
        mapping_version = p.pop("_target_mapping_version")
        result = dict(p, dry_run=dry_run)
        if dry_run or not p["changed"]:
            return result
        if p["episode"]["before"] != p["episode"]["after"]:
            con.execute("UPDATE episodes SET kind=?, alias_of=?, revision=revision+1,"
                        " updated_at=? WHERE id=?",
                        (ALIAS_KIND, target_id, db.utcnow(), alias_id))
        after = p["show_map"]["after"]
        if after is not None and p["show_map"]["before"] != after:
            if p["show_map"]["before"] is None:
                con.execute(
                    "INSERT INTO customer_episode_shows (workspace_id, episode_id,"
                    " show_identity_id, basis, source_version_id, created_at)"
                    " VALUES ('local', ?, ?, 'alias_of', ?, ?)",
                    (alias_id, after["show_identity_id"], mapping_version, db.utcnow()))
            else:
                con.execute(
                    "UPDATE customer_episode_shows SET show_identity_id=?, basis='alias_of',"
                    " source_version_id=? WHERE workspace_id='local' AND episode_id=?",
                    (after["show_identity_id"], mapping_version, alias_id))
        if db.has_history_tables(con):
            db.record_episode_snapshot(con, alias_id, actor)
        if db.has_v8_tables(con):
            from . import citations
            citations.publish_episode_sources(con, alias_id, actor)
        rev = db.bump_revision(con)
        db.audit(con, actor, AUDIT_ACTION, "episode", alias_id,
                 {"idempotency_key": f"{AUDIT_ACTION}:{alias_id}:{target_id}",
                  "target": target_id, "episode": p["episode"], "show_map": p["show_map"],
                  "basis": p["basis"], "reads": p["reads"]}, rev)
        result["aggregate_rev"] = rev
        return result
