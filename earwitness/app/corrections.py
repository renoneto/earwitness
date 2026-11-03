"""Operator ad review: corrections become rules (admin review, 2026-10-11).

The operator (loopback only) works a queue of reads: the namer's
needs-review reads plus reads members reported as wrong, most impact
first. Each action fixes the read in front of him and, where it says
something about the corpus, becomes a rule kept in the workspace:

- alias: a raw spoken spelling maps to a canonical brand ("way fair" ->
  wayfair). Every load writes it into the brand alias table, so every
  read heard that way shows and counts as the canonical brand. The raw
  spelling stays the read's identity. An alias that changes any read
  other than the one in front of the operator (Enter on a typed brand, or
  the explicit "every read heard as X") is saved only after he has seen
  the count; a known brand (known_brand) adds a warning. A human-confirmed
  brand is never alias-mapped. Actions on the heard-as spelling name the
  spelling the page showed, so a stale tab never acts on another one.
- stoplist: a generic word is never a brand ("term life"). Every load
  gives a read whose namer pick is stoplisted the namer's next candidate
  (or no brand) and leaves it needs-review. The operator sees the count
  first, and confirms again when the word is a known brand.
- self_promo: brand X is self-promotion on one exact show, or on every
  show whose title has the network words. Never over a read a person
  reviewed.
- not_ad: a read is not an ad (also the read's review judgment).
- brand: a brand the namer's brand list lacks.

Rules act at load time (ad_reads.rebuild and the seed plan) and at once
when the operator makes them, on the reads they touch only, with the same
transforms (ad_reads.refresh_rule_facts), so the next unchanged load
changes nothing. A removed rule re-applies its reads at once: every read
it changed that no person reviewed is as the namer gave it. Republished
reads keep their tracker baseline membership, and an action or its undo
carries the reads it changed into them: never news (carry_into_trackers).
A person's review from any page beats a self-promo rule (after_review).
`undo` reverses the newest
action on a read when nothing changed it since (decision 11). `export` writes the
rules, the removed ones and the labeled examples every action records,
as the JSON the pipeline reads (SETUP.md, "Ad review corrections file").

Members report a read as wrong (`report`): the report waits in the queue
and changes nothing members see until the operator acts.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from . import db

_LOG = logging.getLogger("earwitness.corrections")

# What one labelled action changed, taken in its own transaction just before
# it wrote (undo): the read's review and brand associations, the rules it
# overwrote or replaced, the reports it resolved, and the read's revisions
# right after it. Created on first use too, so an older workspace needs no
# upgrade (not in TABLES).
UNDO_SCHEMA = ("CREATE TABLE IF NOT EXISTS ad_label_undo ("
               " label_id INTEGER PRIMARY KEY, snapshot TEXT NOT NULL,"
               " undone_at TEXT, undone_by TEXT)")
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS ad_rules ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, key TEXT NOT NULL,"
    " value TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, actor TEXT NOT NULL,"
    " UNIQUE (kind, key))",
    "CREATE TABLE IF NOT EXISTS ad_rules_removed ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id INTEGER NOT NULL, kind TEXT NOT NULL,"
    " key TEXT NOT NULL, value TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,"
    " actor TEXT NOT NULL, removed_at TEXT NOT NULL, removed_by TEXT NOT NULL,"
    " reason TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS ad_labels ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, evidence_id TEXT NOT NULL, record_key TEXT,"
    " episode_id TEXT, action TEXT NOT NULL, answer TEXT NOT NULL DEFAULT '{}',"
    " raw_brand TEXT, transcript TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,"
    " actor TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS ad_reports ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, evidence_id TEXT NOT NULL, record_key TEXT,"
    " ref TEXT NOT NULL, reporter TEXT NOT NULL, reason TEXT NOT NULL,"
    " note TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open',"
    " created_at TEXT NOT NULL, resolved_at TEXT, resolution TEXT)",
    "CREATE INDEX IF NOT EXISTS idx_ad_reports_reporter ON ad_reports (reporter, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_ad_reports_open ON ad_reports (status, record_key)",
    "CREATE TABLE IF NOT EXISTS ad_show_ranks ("
    " show_title TEXT PRIMARY KEY, rank INTEGER NOT NULL)",
    # Load fact: reads whose stoplisted pick fell through to the namer's
    # next candidate (never decided by an alias of that candidate).
    "CREATE TABLE IF NOT EXISTS ad_fallthrough (evidence_id TEXT PRIMARY KEY)",
    UNDO_SCHEMA,
)
TABLES = ("ad_rules", "ad_rules_removed", "ad_labels", "ad_reports", "ad_show_ranks",
          "ad_fallthrough")
RULE_KINDS = ("alias", "stoplist", "self_promo", "not_ad", "brand")
ACTIONS = ("confirm", "pick", "type", "alias", "not_ad", "self_promo", "generic")
REPORT_REASONS = ("wrong_brand", "not_an_ad", "wrong_time", "other")
REPORT_LIMIT = 10          # reports per member per window
REPORT_WINDOW = timedelta(hours=1)
NOTE_MAX = 1000
EXPORT_FORMAT = "earwitness-corrections/3"
HISTORY_ORIGIN = "admin-review"
SELF_PROMO_REASON = "network"  # self_promo_reason "network:<text>"
UPGRADE_MESSAGE = ("This workspace has no ad review tables yet. Stop the server and run:"
                   " python3 scripts/app_workspace.py --workspace <workspace> upgrade")
UNLOADED_MESSAGE = ("This workspace has no corpus load to review yet. Load a corpus export"
                    " (app_workspace.py seed --source-root <export>) or run upgrade.")
REMOVED_STAYS = ("What stays: the brand or judgment you gave the read you acted on, and any"
                 " alerts already sent. The examples the rule came from are marked superseded.")


class RateLimited(Exception):
    """A member sent too many reports in the window."""


class NeedsUpgrade(Exception):
    """The workspace lacks the ad review tables (or any corpus load)."""


class ConfirmNeeded(Exception):
    """A corpus-wide action the operator has not confirmed with its
    current count (details: what it changes and how many reads)."""

    def __init__(self, message: str, details: dict):
        super().__init__(message)
        self.details = details


class ReadChanged(Exception):
    """The read is no longer what the operator saw (details: what it is
    now); nothing was saved."""

    def __init__(self, message: str, details: dict):
        super().__init__(message)
        self.details = details


def ensure_schema(con: sqlite3.Connection) -> None:
    for statement in SCHEMA:
        con.execute(statement)
    # Last: a new or rebuilt guarded table gets its view-state guards, or
    # view_state_key stays None and no research read is reused (UPD3-SLOW).
    db.ensure_view_state_guards(con)


def present(con: sqlite3.Connection) -> bool:
    names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN (%s)"
        % ",".join("?" * len(TABLES)), TABLES)}
    return len(names) == len(TABLES)


def require_tables(con: sqlite3.Connection) -> None:
    from . import ad_reads as _ad
    if not present(con):
        raise NeedsUpgrade(UPGRADE_MESSAGE)
    if not _ad.tables_present(con):
        raise NeedsUpgrade(UNLOADED_MESSAGE)


def _key(text) -> str:
    from .ad_reads import brand_key
    return brand_key(text)


# -- Rules -----------------------------------------------------------------------

def load(con: sqlite3.Connection) -> dict:
    """The workspace's rules: alias {raw: (canonical, display)}, stoplist
    {word}, self_promo [(brand, network, exact)], not_ad {record key:
    value}, brand {key: display}; rows: every rule row (export). Rows of
    another kind (a stored brand suggestion) are not rules and are skipped."""
    out = {"alias": {}, "stoplist": set(), "self_promo": [], "not_ad": {}, "brand": {},
           "rows": []}
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ad_rules'"
                       ).fetchone():
        return out
    for rid, kind, key, value, created, actor in con.execute(
            "SELECT id, kind, key, value, created_at, actor FROM ad_rules ORDER BY id"):
        if kind not in RULE_KINDS:   # a brand suggestion (brand_suggest) is no rule until confirmed
            continue
        v = json.loads(value or "{}")
        out["rows"].append({"id": rid, "kind": kind, "key": key, "value": v,
                            "created_at": created, "actor": actor})
        if kind == "alias":
            out["alias"][key] = (v["canonical"], v.get("display"))
        elif kind == "stoplist":
            out["stoplist"].add(key)
        elif kind == "self_promo":
            out["self_promo"].append((v["brand"], v["network"], bool(v.get("exact"))))
        elif kind == "not_ad":
            out["not_ad"][key] = dict(v, created_at=created, actor=actor)
        elif kind == "brand":
            out["brand"][key] = v.get("display")
    return out


def _put_rule(con, kind: str, key: str, value: dict, actor: str) -> None:
    con.execute("INSERT INTO ad_rules (kind, key, value, created_at, actor) VALUES (?,?,?,?,?)"
                " ON CONFLICT(kind, key) DO UPDATE SET value=excluded.value,"
                " created_at=excluded.created_at, actor=excluded.actor",
                (kind, key, json.dumps(value, sort_keys=True), db.utcnow(), actor))


def _retire(con, rule_id: int, actor: str, reason: str) -> dict | None:
    """Move one rule to ad_rules_removed (the export lists it as
    superseded)."""
    row = con.execute("SELECT id, kind, key, value, created_at, actor FROM ad_rules WHERE id=?",
                      (rule_id,)).fetchone()
    if row is None:
        return None
    con.execute("INSERT INTO ad_rules_removed (rule_id, kind, key, value, created_at, actor,"
                " removed_at, removed_by, reason) VALUES (?,?,?,?,?,?,?,?,?)",
                (*tuple(row), db.utcnow(), actor, reason))
    con.execute("DELETE FROM ad_rules WHERE id=?", (rule_id,))
    return dict(row)


def _gazetteer_keys() -> frozenset:
    from earwitness.gazetteer import GAZETTEER
    keys = set()
    for canon, aliases in GAZETTEER.items():
        keys.add(_key(canon))
        keys.update(_key(a) for a in aliases)
    return frozenset(k for k in keys if k)


def known_brand(con: sqlite3.Connection, key: str, rules: dict | None = None) -> bool:
    """A brand key the operator must never remap or stoplist by one key:
    curated, in the namer's gazetteer, a new-brand rule, an export brand
    list spelling or canon anchor, or a brand a person confirmed anywhere."""
    from . import ad_reads as _ad
    from .customer import BRAND_DISPLAY_NAMES
    if not key:
        return False
    rules = load(con) if rules is None else rules
    if key in BRAND_DISPLAY_NAMES or key in rules["brand"] or key in _gazetteer_keys():
        return True
    if _ad.tables_present(con) and con.execute(
            "SELECT 1 FROM ad_brand_aliases WHERE (raw_key=? AND display_basis='brand_list')"
            " OR (canonical_key=? AND raw_key<>?) LIMIT 1", (key, key, key)).fetchone():
        return True
    return any(_key(b) == key for (b,) in con.execute(
        "SELECT brand FROM brand_assocs WHERE basis='confirmed' AND origin<>'detector'"))


def known_keys(con: sqlite3.Connection, rules: dict | None = None) -> set:
    """Every brand key known_brand() accepts, enumerated from the same
    sources: the curated labels, the brand rules, the gazetteer, the export
    brand list's spellings and canon anchors, and the brands a person
    confirmed."""
    from . import ad_reads as _ad
    from .customer import BRAND_DISPLAY_NAMES
    rules = load(con) if rules is None else rules
    keys = set(BRAND_DISPLAY_NAMES) | set(rules["brand"]) | set(_gazetteer_keys())
    if _ad.tables_present(con):
        keys.update(r[0] for r in con.execute(
            "SELECT raw_key FROM ad_brand_aliases WHERE display_basis='brand_list'"))
        keys.update(r[0] for r in con.execute(
            "SELECT DISTINCT a.canonical_key FROM ad_brand_aliases a WHERE EXISTS"
            " (SELECT 1 FROM ad_brand_aliases b WHERE b.canonical_key=a.canonical_key"
            " AND b.raw_key<>a.canonical_key)"))
    keys.update(_key(b) for (b,) in con.execute(
        "SELECT brand FROM brand_assocs WHERE basis='confirmed' AND origin<>'detector'"))
    keys.discard("")
    return keys


def _next_brand(candidates: list, stoplist: set, raw: str):
    """The namer's next pick once its own is stoplisted: the first listed
    candidate that is not stoplisted, else none."""
    for c in candidates or []:
        brand = c.get("brand") if isinstance(c, dict) else None
        if isinstance(brand, str) and _key(brand) and _key(brand) not in stoplist \
                and _key(brand) != raw:
            return brand
    return None


def _without_stopped(candidates: list, stoplist: set) -> list:
    return [c for c in candidates or [] if not (isinstance(c, dict)
            and _key(c.get("brand")) in stoplist)]


def stoplist_reads(reads: list, rules: dict) -> list:
    """Named reads (export shape) as the namer would give them with the
    stoplist: a stoplisted pick becomes the next candidate (or no brand),
    needs review and is marked `_fell_through`; stoplisted candidates are
    dropped. Unchanged reads are returned as they are; changed ones are
    copies."""
    stop = rules["stoplist"]
    if not stop:
        return reads
    out = []
    for read in reads:
        if not isinstance(read, dict):
            out.append(read)
            continue
        provenance = read.get("provenance") if isinstance(read.get("provenance"), dict) else {}
        candidates = provenance.get("candidates") if isinstance(provenance.get("candidates"), list) else []
        raw = _key(read.get("brand_spoken") or read.get("brand"))
        kept = _without_stopped(candidates, stop)
        if raw not in stop and len(kept) == len(candidates):
            out.append(read)
            continue
        read = dict(read, provenance=dict(provenance, candidates=kept))
        if raw in stop:
            brand = _next_brand(candidates, stop, raw)
            read.update(brand=brand, brand_spoken=brand, needs_review=True, _fell_through=True,
                        brand_canonical=None, brand_display=None, brand_display_basis="none",
                        product=None, network=None)
        out.append(read)
    return out


def promo_match(network: str, exact: bool, title: str) -> bool:
    """A self-promo rule's show test: the exact show title, else the
    network words as whole words of the title."""
    from .ad_reads import fold
    t = fold(title or "")
    if exact:
        return t == fold(network)
    words = re.findall(r"[a-z0-9&']+", fold(network))
    return bool(words) and re.search(
        r"(?<![a-z0-9])" + r"[^a-z0-9]+".join(map(re.escape, words)) + r"(?![a-z0-9])",
        t) is not None


def self_promo_reads(reads: list, rules: dict, shows: dict, canonical, skip=()) -> list:
    """Named reads with the self-promo rules applied (promo_match). Reads
    whose record key is in `skip` (a person reviewed them) are never
    changed. shows: episode -> title."""
    if not rules["self_promo"]:
        return reads
    out = []
    for read in reads:
        if not isinstance(read, dict) or read.get("self_promo") \
                or f"named:{read.get('candidate_id')}" in skip:
            out.append(read)
            continue
        brand = canonical(read.get("brand_spoken") or read.get("brand"))
        title = shows.get(str(read.get("ep_id"))) or read.get("show_title") or ""
        hit = next((network for rule_brand, network, exact in rules["self_promo"]
                    if brand and rule_brand == brand and promo_match(network, exact, title)), None)
        out.append(dict(read, self_promo=True, self_promo_reason=f"{SELF_PROMO_REASON}:{hit}",
                        needs_review=False) if hit else read)
    return out


def alias_rows(rules: dict, aliases: dict) -> dict:
    """The load's alias table with the alias rules over it:
    raw -> (raw, canonical, basis, display)."""
    out = dict(aliases)
    for raw, (canonical, display) in rules["alias"].items():
        if not display:
            display = next((v[3] for v in out.values() if v[1] == canonical and v[3]), None)
        out[raw] = (raw, canonical, "brand_list" if display else "none", display)
    return out


def reviewed_ids_of(con: sqlite3.Connection, evidence_ids) -> set:
    """reviewed_ids, for the given rows only."""
    ids = list(evidence_ids)
    marks = ",".join("?" * len(ids))
    return {r[0] for r in con.execute(
        f"SELECT evidence_id FROM reviews WHERE judgment IS NOT NULL AND evidence_id IN ({marks})"
        f" UNION SELECT evidence_id FROM brand_assocs WHERE origin<>'detector' AND basis='confirmed'"
        f" AND evidence_id IN ({marks})", ids + ids)} if ids else set()


def reviewed_ids(con: sqlite3.Connection) -> set:
    """Rows a person reviewed: a review judgment or a confirmed brand."""
    return {r[0] for r in con.execute(
        "SELECT evidence_id FROM reviews WHERE judgment IS NOT NULL UNION"
        " SELECT evidence_id FROM brand_assocs WHERE origin<>'detector' AND basis='confirmed'")}


def apply_to_plan(con: sqlite3.Connection, plan: dict) -> None:
    """The seed plan's named reads with the stoplist applied (their
    evidence attrs and the read list the side tables are built from), so
    a load writes the namer's corrected pick as the machine brand."""
    from . import import_sources
    rules = load(con)
    if not rules["stoplist"] or not plan.get("_named_raw"):
        return
    plan["_named_raw"] = stoplist_reads(plan["_named_raw"], rules)
    fixed = {r["candidate_id"]: r for r in plan["_named_raw"]
             if isinstance(r, dict) and r.get("candidate_id")}
    for ev in plan["evidence"]:
        if not ev["id"].startswith("named:"):
            continue
        read = fixed.get(ev["id"][len("named:"):])
        if read is None:
            continue
        # The attrs a rule writes at once (ad_reads.refresh_rule_facts).
        ev["attrs"] = json.dumps(import_sources.candidate_attrs(read, "named"))


# -- Rules act at once -------------------------------------------------------------

def _rule_key(rule: dict) -> str:
    """The ad_rules key of a rule an action made (out["rules"] entry)."""
    if rule["kind"] == "alias":
        return rule["raw"]
    if rule["kind"] == "stoplist":
        return rule["word"]
    if rule["kind"] == "brand":
        return rule["brand"]
    if rule["kind"] == "self_promo":
        return f"{rule['brand']}\t{'=' if rule['exact'] else '~'}{_key(rule['network'])}"
    return rule.get("key", "")


def publish_keeping_trackers(con, evidence_ids, actor: str) -> None:
    """Publish rows a rule changed, and keep them in every tracker baseline
    that had them (the same carry-over as lineage.repoint): a rule or its
    undo moving a read's publication is never news to a tracker."""
    from . import citations
    if not evidence_ids or not db.has_v8_tables(con):
        return
    fresh, digests = {}, {}  # one payload digest per source version, not per read
    for eid in evidence_ids:
        citations.publish_source_state(con, kind="evidence", record_id=eid, actor=actor,
                                       digests=digests)
        fresh[f"finding:{eid}"] = citations.current_source_ref(con, "evidence", eid)
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='customer_alerts'"
                       ).fetchone():
        return
    for alert_id, raw in con.execute("SELECT id, baseline_json FROM customer_alerts").fetchall():
        try:
            baseline = json.loads(raw or "{}")
        except (ValueError, TypeError):
            continue
        members = baseline.get("members") if isinstance(baseline, dict) else None
        if not isinstance(members, dict):
            continue
        changed = False
        for logical, ref in fresh.items():
            member = members.get(logical)
            if isinstance(member, dict) and member.get("ref") != ref:
                member = dict(member, ref=ref)
                if isinstance(member.get("transition"), dict):
                    member["transition"] = dict(member["transition"], source_ref=ref)
                members[logical] = member
                changed = True
        if changed:
            con.execute("UPDATE customer_alerts SET baseline_json=? WHERE id=?",
                        (json.dumps(baseline, sort_keys=True), alert_id))


def _seen_reads(con, alert: dict, baseline: dict, ids: list[str]) -> list[str]:
    """The reads among ids the alert had already seen at its last successful
    check (CQT-R1-01): a baseline member, a read held for brand review then
    (baseline held_refs, by publication), or a read created before that
    check. A read created in the check's second or later is not seen: it
    stays news even if a person saves on it first."""
    members = baseline.get("members") or {}
    rest = [e for e in ids if f"finding:{e}" not in members]
    seen = set(ids) - set(rest)
    refs = [r for r in baseline.get("held_refs") or () if isinstance(r, str)]
    last = alert.get("last_successful_at")
    for at in range(0, len(rest), 400):
        chunk = rest[at:at + 400]
        marks = ",".join("?" * len(chunk))
        if last:
            seen.update(r[0] for r in con.execute(
                f"SELECT id FROM evidence WHERE id IN ({marks}) AND created_at < ?",
                [*chunk, last]))
        for start in range(0, len(refs), 400):
            part = refs[start:start + 400]
            seen.update(r[0] for r in con.execute(
                f"SELECT record_id FROM source_states WHERE kind='evidence'"
                f" AND record_id IN ({marks}) AND id IN ({','.join('?' * len(part))})",
                [*chunk, *part]))
    return [e for e in ids if e in seen]


def _carried_members(con, alert: dict, ids: list[str], episodes: set) -> dict[str, dict]:
    """finding:<id> -> the member a check would store now, for the reads ids
    the alert's ads source matches (customer._alert_snapshot, CQT-R1-02):
    the same Query2 pipeline restricted to the reads' canonical episodes
    (customer._light_match `episodes`: each kept row is its row in the full
    scan), ads only (findings come from no other source), no transcript
    passages, held counts or view revision. A held read and a read with no
    source ref are no member."""
    from . import customer, customer_query, ops
    query = json.loads(alert["query_json"])
    live = json.loads(alert.get("live_selector_json") or "null")
    if query.get("mode") not in ("ads", "mentions"):
        return {}  # episodes mode has no finding members (CQT-R1-05)
    effective, clause, _units = customer_query.tracker_effective(query, live or None)
    ads = dict(effective, mode="ads")
    if ads.get("ids") == []:
        return {}
    transcripts = customer_query.pass_transcripts(con)
    transcripts = {} if transcripts is None else transcripts
    light = customer._light_match(con, customer_query._v1_criteria(ads), None,
                                  transcripts=transcripts, episodes=episodes,
                                  episode_rights_only=True)
    if light["partial"] or light["reason"]:
        raise ops.BadInput("baseline scan is incomplete")
    rows = customer_query._apply_set_predicates(con, ads, light["rows"], light=True,
                                                transcripts=transcripts, episodes=episodes)
    rows, _units = customer_query._apply_clause(con, ads, clause, rows, None)
    wanted, out = set(ids), {}
    for row in rows:
        eid = row.get("evidence_id")
        if row.get("record_kind") != "evidence" or eid not in wanted \
                or row.get("match_basis") == "title":  # R3-03: own text only
            continue
        read = row.get("read") or {}
        if (read.get("needs_review") and row.get("status") != "confirmed") or not row.get("ref"):
            continue
        out[f"finding:{eid}"] = {"kind": "new_finding", "ref": row["ref"],
                                 "transition": customer._dependency_revisions(con, row),
                                 "offer": customer._offer_signature(con, row),
                                 "episode_id": row.get("episode_id")}
    return out


def carry_into_trackers(con, evidence_ids) -> int:
    """A person's review action is never news to a tracker (CONFIRM-QUIET,
    ADA-R1-05): `act` and `undo` carry the reads they changed into every
    alert baseline in their own transaction. For each active alert with a
    complete baseline (alert_carry's rules), a read the alert had already
    seen (_seen_reads) and matches now goes into the baseline as the check
    computes it (_carried_members; a member keeps its ordinal). A read the
    alert no longer matches, a read held for brand review, other reads, and
    reads created at or after the alert's last check are untouched: those
    stay news. An alert whose stored data does not parse is skipped and
    logged (CQT-R1-06). Returns the number of baselines written."""
    from . import ad_breaks, alert_carry, ops, search
    ids = list(dict.fromkeys(e for e in evidence_ids if e))
    if not ids or not con.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                                  " AND name='customer_alerts'").fetchone():
        return 0
    episodes: set | None = None
    written = 0
    for alert in [dict(r) for r in con.execute(
            "SELECT * FROM customer_alerts WHERE workspace_id='local' ORDER BY id")]:
        try:
            baseline = json.loads(alert["baseline_json"] or "{}")
            if not isinstance(baseline, dict) or alert_carry._skip_reason(alert, baseline):
                continue
            seen = _seen_reads(con, alert, baseline, ids)
            if not seen:
                continue
            if episodes is None:
                # Both ids, as ad_breaks.index_for_reads: an alias whose
                # target the viewer may not see is an episode of its own.
                canonical = ad_breaks.canonical_map(con)
                episodes = set()
                for at in range(0, len(ids), 400):
                    chunk = ids[at:at + 400]
                    for (ep,) in con.execute("SELECT episode_id FROM evidence WHERE id IN (%s)"
                                             % ",".join("?" * len(chunk)), chunk):
                        episodes.update((ep, canonical.get(ep, ep)))
            current = _carried_members(con, alert, seen, episodes)
        except (ValueError, TypeError, KeyError, AttributeError,
                ops.BadInput, search.FilterRejected) as exc:
            _LOG.warning("alert %s: review carry skipped (%s)", alert["id"], type(exc).__name__)
            continue
        members = baseline.setdefault("members", {})
        changed = False
        for logical, now in current.items():
            old = members.get(logical)
            entry = dict(now, ordinal=int((old or {}).get("ordinal", 0)))
            if old != entry:
                members[logical] = entry
                changed = True
        if changed:
            con.execute("UPDATE customer_alerts SET baseline_json=? WHERE id=?"
                        " AND workspace_id='local'", (json.dumps(baseline, sort_keys=True),
                                                       alert["id"]))
            written += 1
    return written


def after_review(con, evidence_id: str, actor: str) -> None:
    """A person's review or brand change on a read, from any page: a
    self-promo rule never stands over a reviewed read, and stands again
    once the review is cleared. Only when a self-promo rule names this
    read's brand, and only this read is re-applied
    (ad_reads.refresh_read_rules)."""
    from . import ad_reads as _ad
    if not _ad.tables_present(con) or not present(con):
        return
    promo = {json.loads(v or "{}").get("brand") for (v,) in con.execute(
        "SELECT value FROM ad_rules WHERE kind='self_promo'")}
    if not promo:
        return
    row = con.execute("SELECT ar.brand_suggested, al.canonical_key FROM ad_reads ar"
                      " LEFT JOIN ad_brand_aliases al ON al.raw_key=ar.brand_suggested"
                      " WHERE ar.evidence_id=? AND ar.kind='named'", (evidence_id,)).fetchone()
    if row is not None and row[0] and (row[1] or row[0]) in promo:
        _ad.refresh_read_rules(con, evidence_id)


def _shown_reads(con) -> dict:
    """evidence id -> (brand key shown, self-promo) for every active read,
    named reads and rule candidates alike. The brand is picked as the read
    block picks it (customer._read_block): a person's confirmed brand,
    else the first brand association, else the namer's pick, each through
    the alias rows. A rule's count, made or removed, is the reads whose
    entry here changes (_count_changes). The rows are the ones customers
    see (customer.customer_row_where, never a reviewed not_ad read), also
    those without a load row (a read pinned to an older transcript): an
    alias maps their spelling at read time too, so the count covers every
    read the write changes. A write that makes a read join or leave that
    set counts it too (its entry appears or goes)."""
    from . import ad_reads as _ad
    from .customer import customer_row_where
    where, args = customer_row_where(con)
    # Reviewed not_ad reads customers never see (customer._customer_ad_rows).
    where = f"{where} AND (r.judgment IS NULL OR r.judgment<>'not_ad')"
    aliases = _ad.alias_map(con)
    joins = (" FROM evidence e JOIN episodes ep ON ep.id=e.episode_id"
             " LEFT JOIN reviews r ON r.evidence_id=e.id"
             " LEFT JOIN ad_reads ar ON ar.evidence_id=e.id")
    assocs: dict = {}
    # PODS: model rows are display-only
    for eid, brand, basis in con.execute(
            "SELECT a.evidence_id, a.brand, a.basis" + joins
            + f" JOIN brand_assocs a ON a.evidence_id=e.id AND a.basis<>'model'"
            f" WHERE {where} ORDER BY a.id", args):
        assocs.setdefault(eid, []).append({"brand": brand, "basis": basis})
    out = {}
    for eid, suggested, promo in con.execute(
            "SELECT e.id, ar.brand_suggested, COALESCE(ar.self_promo, 0)" + joins + f" WHERE {where}", args):
        rows = [a for a in assocs.get(eid, ()) if a["brand"]]
        confirmed = [a for a in rows if a["basis"] == "confirmed"]
        pick = confirmed[0] if confirmed else rows[0] if rows else None
        key = _ad.assoc_key(aliases, pick) if pick else (_ad.canonical(aliases, suggested) or None)
        out[eid] = (key, bool(promo))
    return out


def _count_changes(before: dict, after: dict) -> set:
    """The reads whose shown brand or self-promotion differs."""
    return {eid for eid in before.keys() | after.keys() if before.get(eid) != after.get(eid)}


UNCHECKED = object()  # remove_rule from code that shows no count (tests, tools)


def _drop_machine_names(con, key: str, actor: str) -> list:
    """A removed curated brand (auto or manual): the machine spoken
    suggestions import-names wrote for it go too, on the jev reads no person
    worked on. A read keeps its URL and code machine brands. Returns the
    evidence ids changed."""
    from . import import_sources as _src
    changed = []
    for (eid,) in con.execute(
            "SELECT DISTINCT a.evidence_id FROM brand_assocs a JOIN evidence e ON"
            " e.id=a.evidence_id WHERE e.source='jev' AND a.origin='detector'"
            " AND a.channel='spoken' ORDER BY a.evidence_id").fetchall():
        machine: dict = {}
        for brand, channel in con.execute("SELECT brand, channel FROM brand_assocs WHERE"
                                          " evidence_id=? AND origin='detector' ORDER BY id",
                                          (eid,)):
            machine.setdefault(channel, []).append(brand)
        if [_key(b) for b in machine.get("spoken", [])] != [key] or set(machine) - {
                "spoken", "url", "code"} or any(len(v) > 1 for c, v in machine.items()
                                                if c != "spoken"):
            continue
        incoming = {k: machine[c][0] for k, c in (("brand_url", "url"), ("brand_code", "code"))
                    if machine.get(c)}
        if _src.refresh_machine_assocs(con, eid, incoming, actor):
            changed.append(eid)
    return changed


@db.atomic
def remove_rule(con: sqlite3.Connection, rule_id, actor: str = "owner",
                expected_reads=UNCHECKED, reason: str = "removed") -> dict:
    """Undo one rule: it moves to the removed rules (export: superseded,
    with the labels it came from) and the reads it touched are as the
    current rules make them again, now and on every load. Only those reads
    are re-applied (ad_reads.refresh_rule_facts).
    Count first (decision 12, F-13): expected_reads is the count the page
    showed. Without it, or with another count, the removal is a dry run:
    the count is measured on the real removal, which then rolls back
    (db.atomic's transaction or savepoint), and ConfirmNeeded carries it.
    out["reads"]: reads whose shown brand or self-promotion changed.
    `reason` is what the superseded rule is marked with ("removed" from the
    Rules panel; "undone" when a brand suggestion's Undo takes back its
    rules, as `undo` marks the rules it retires)."""
    from . import ad_reads as _ad
    from .ops import BadInput
    require_tables(con)
    try:
        rule_id = int(rule_id)
    except (TypeError, ValueError):
        raise BadInput("unknown rule")
    kind = con.execute("SELECT kind FROM ad_rules WHERE id=?", (rule_id,)).fetchone()
    if kind is None or kind[0] not in RULE_KINDS:   # before anything is retired
        raise BadInput("unknown rule")
    before = _shown_reads(con)
    gone = _retire(con, rule_id, actor, reason)
    if gone is None:
        raise BadInput("unknown rule")
    value = json.loads(gone["value"] or "{}")
    if gone["kind"] in ("alias", "stoplist"):
        _ad.refresh_rule_facts(con, actor, words={gone["key"]})
    elif gone["kind"] == "self_promo":
        _ad.refresh_rule_facts(con, actor, brands={value.get("brand")})
    # Only when nothing else keeps the brand known (a person's confirmation
    # on another read does): else the next import-names would write them back.
    changed = _drop_machine_names(con, gone["key"], actor) \
        if gone["kind"] == "brand" and value.get("origin") in ("auto", "manual") \
        and not known_brand(con, gone["key"], load(con)) else []
    if changed:
        from . import import_sources as _src
        _src._reindex(con)
        publish_keeping_trackers(con, changed, actor)
    reads = len(_count_changes(before, _shown_reads(con)))
    if expected_reads is not UNCHECKED:
        _expect({"expected_reads": expected_reads},
                {"rule": rule_id, "kind": gone["kind"], "reads": reads, "known_brand": False},
                f"Removing this rule changes {reads} read{'' if reads == 1 else 's'}:"
                " confirm with the count.")
    rev = db.bump_revision(con)
    db.audit(con, actor, "admin_rule_removed", "ad_rule", str(rule_id),
             {"kind": gone["kind"], "key": gone["key"]}, rev)
    return {"removed": {"id": rule_id, "kind": gone["kind"], "key": gone["key"]},
            "message": "Rule removed. Every read it changed that nobody reviewed is back as"
                       " the namer gave it. " + REMOVED_STAYS,
            "reads": reads, "aggregate_rev": rev}


# -- Operator actions --------------------------------------------------------------

def _read_row(con, evidence_id: str):
    from . import ad_reads as _ad
    side = _ad.tables_present(con)
    return con.execute(
        "SELECT e.id, e.source_record_key, e.episode_id, e.orig_start_s, e.orig_end_s,"
        " e.attrs, e.assoc_revision, ep.show_title, ep.ep_title, ep.pub_date, "
        + ("ar.brand_suggested, ar.quote, ar.display_start_s, ar.display_end_s"
           if side else "NULL AS brand_suggested, '' AS quote, NULL AS display_start_s,"
           " NULL AS display_end_s")
        + " FROM evidence e JOIN episodes ep ON ep.id=e.episode_id"
        + (" LEFT JOIN ad_reads ar ON ar.evidence_id=e.id" if side else "")
        + " WHERE e.id=?", (evidence_id,)).fetchone()


def _raw_brand(row) -> str:
    if row["brand_suggested"]:
        return row["brand_suggested"]
    try:
        attrs = json.loads(row["attrs"] or "{}")
    except (ValueError, TypeError):
        attrs = {}
    return _key(attrs.get("brand_spoken") if isinstance(attrs, dict) else "")


def _set_brand(con, evidence_id: str, brand: str, actor: str, candidates=()) -> None:
    """The read is an ad of `brand` and of no other: one confirmed brand,
    the review judgment ad. The namer's suggestion goes (and stays gone on
    a carried-over row, lineage), so does any brand a person or agent set
    to one of the read's candidates (an earlier choice, as ops.choose_brand)
    and any other confirmed brand; every other added brand is written back
    unchanged (set_brand_assocs kept=). An association revision with
    history origin admin-review."""
    from . import ad_reads as _ad
    from . import ops
    aliases = _ad.alias_map(con)
    listed = {_ad.canonical(aliases, c) for c in candidates} | {brand}
    # PODS: model rows are display-only
    kept = [tuple(a) for a in con.execute(
        "SELECT brand, basis, origin, channel FROM brand_assocs WHERE evidence_id=?"
        " AND basis<>'model' ORDER BY id",
        (evidence_id,))
        if a["origin"] != "detector" and a["basis"] != "confirmed"
        and _ad.canonical(aliases, a["brand"]) not in listed]
    rev = con.execute("SELECT assoc_revision FROM evidence WHERE id=?", (evidence_id,)).fetchone()[0]
    ops.set_brand_assocs(con, evidence_id,
                         [{"brand": brand, "basis": "confirmed", "channel": "spoken"}],
                         rev, actor, history_origin=HISTORY_ORIGIN, kept=kept)
    _judge(con, evidence_id, "ad", actor)


def _judge(con, evidence_id: str, judgment: str, actor: str) -> None:
    from . import ops
    rev = con.execute("SELECT revision FROM reviews WHERE evidence_id=?", (evidence_id,)).fetchone()
    ops.save_review(con, evidence_id, {"judgment": judgment}, rev[0] if rev else 0, actor)


def _brand_key_of(con, text: str, rules: dict) -> str:
    """What a typed brand means: a known brand is itself (never through an
    operator alias), anything else goes through the alias table."""
    from . import ad_reads as _ad
    key = _key(text)
    return key if known_brand(con, key, rules) else _ad.canonical(_ad.alias_map(con), text)


def _changed(read: dict, what: str, outcome: str = "Nothing was saved; look again.") -> ReadChanged:
    brand = read.get("brand") or {}
    return ReadChanged(
        f"This read changed since the page showed it ({what}). {outcome}",
        {"brand": brand.get("key"), "display": brand.get("display"),
         "candidates": [c.get("brand") for c in read.get("candidates") or []
                        if isinstance(c, dict)]})


def _expect(body: dict, details: dict, message: str) -> None:
    """A corpus-wide action needs the count the operator saw, and an
    explicit yes when it touches a known brand."""
    expected = body.get("expected_reads")
    if isinstance(expected, bool) or expected != details["reads"] or (
            details["known_brand"] and body.get("known_brand") is not True):
        raise ConfirmNeeded(message, details)


@db.atomic
def act(con: sqlite3.Connection, evidence_id: str, body: dict, actor: str = "owner") -> dict:
    """One operator action on one read (module docstring). body: action,
    plus:
    - confirm: brand (the key the page showed); pick: rank and brand (the
      candidate shown); a read that changed meanwhile is ReadChanged;
    - type: brand; every_read (default true) also maps the read's raw
      spelling to it everywhere;
    - alias: brand, every read heard as the read's raw spelling;
    - generic: the read's raw spelling is never a brand;
      an alias that changes any read other than this one, and every
      generic word, needs expected_reads (the count shown) and, on a known
      brand, known_brand=true: else ConfirmNeeded with the count;
    - self_promo: network (default this exact show; other text = those
      words in a show title); needs expected_reads (the count shown),
      else ConfirmNeeded with the count;
    type, alias, self_promo and generic carry `heard`: the read's raw
    spelling as the page showed it; a read heard otherwise now is
    ReadChanged (a stale tab never acts on a spelling nobody saw).
    The latest decision on a read replaces its earlier not-an-ad rule.
    Records a labeled example, resolves the read's open reports."""
    from . import ad_reads as _ad
    from . import customer as _customer
    from . import ops
    from .ops import BadInput
    require_tables(con)
    if not isinstance(body, dict) or body.get("action") not in ACTIONS:
        raise BadInput("action must be one of " + ", ".join(ACTIONS))
    action = body["action"]
    row = _read_row(con, evidence_id)
    if row is None:
        raise BadInput("unknown evidence")
    ops._require_current(con, evidence_id)  # a replaced row never reaches customers
    read = _customer.read_detail(con, evidence_id) or {}
    raw = _raw_brand(row)
    rules = load(con)
    out = {"action": action, "evidence_id": evidence_id, "rules": [], "reads": 0}
    answer: dict = {}
    rkey = row["source_record_key"] or evidence_id
    snapshot = _undo_before(con, evidence_id)
    moved = {evidence_id}  # the reads this action changes (carry_into_trackers)

    def put(kind, key, value):  # a rule this action writes; undo restores what it overwrote
        snapshot["rules_before"].setdefault(f"{kind}\t{key}", _rule_row(con, kind, key))
        _put_rule(con, kind, key, value, actor)
    if action in ("type", "alias", "self_promo", "generic"):
        heard = body.get("heard")
        if not isinstance(heard, str):
            raise BadInput("heard is required (the spelling the page showed)")
        if _key(heard) != (raw or ""):
            raise _changed(read, f"it is heard as “{raw or 'nothing'}” now, not “{heard}”")
    if action != "not_ad" and rkey in rules["not_ad"]:
        rid = con.execute("SELECT id FROM ad_rules WHERE kind='not_ad' AND key=?",
                          (rkey,)).fetchone()[0]
        gone = _retire(con, rid, actor, "replaced")
        snapshot["replaced"] = {"rule": gone, "removed_id": con.execute(
            "SELECT MAX(id) FROM ad_rules_removed WHERE rule_id=?", (rid,)).fetchone()[0]}
        out["rules"].append({"kind": "not_ad", "replaced": True})
    if action in ("confirm", "pick", "type", "alias"):
        listed = [c.get("brand") for c in read.get("candidates") or []
                  if isinstance(c, dict) and isinstance(c.get("brand"), str) and c["brand"].strip()]
        shown = body.get("brand")
        if not isinstance(shown, str) or not _key(shown):
            raise BadInput("brand is required (the brand the page showed)")
        if action == "confirm":
            key = (read.get("brand") or {}).get("key")
            if not key or _brand_key_of(con, shown, rules) != key:
                raise _changed(read, "its brand is not the one shown")
        elif action == "pick":
            rank = body.get("rank")
            if isinstance(rank, bool) or not isinstance(rank, int) or not 1 <= rank <= 5:
                raise BadInput("rank must be 1-5")
            if rank > len(listed) or _key(listed[rank - 1]) != _key(shown):
                raise _changed(read, "its candidates moved")
            key = _brand_key_of(con, listed[rank - 1], rules)
            answer["rank"] = rank
        else:
            key = _brand_key_of(con, shown, rules)
        typed = shown.strip()
        if key in rules["stoplist"]:
            raise BadInput(f"“{typed}” is on the generic-word list, not a brand")
        alias = action == "alias" or (action == "type" and body.get("every_read", True))
        alias = alias and bool(raw) and raw != key and _ad.canonical(_ad.alias_map(con), raw) != key
        if action == "alias" and not alias:
            raise BadInput("every read heard this way already shows this brand")
        if alias:
            if raw in rules["stoplist"]:
                raise BadInput(f"“{raw}” is a generic word, not a spelling of a brand")
            # Count first, measured: the count is taken on the real write
            # below; a missing or other count raises, and db.atomic rolls
            # the whole action back (as remove_rule does).
            shown_before = _shown_reads(con)
            raw_known = known_brand(con, raw, rules)
        _set_brand(con, evidence_id, key, actor, listed)
        answer["brand"] = key
        display = typed if typed != typed.lower() else None
        if key not in rules["brand"] and not _known_without(con, key, rules, evidence_id):
            put("brand", key, {"display": display or typed, "from_read": rkey})
            out["rules"].append({"kind": "brand", "brand": key})
        if alias:
            put("alias", raw, {"canonical": key, "display": display, "from_read": rkey})
            _ad.refresh_rule_facts(con, actor, words={raw})
            changed = _count_changes(shown_before, _shown_reads(con))
            moved |= changed
            # Enter on a typed brand (every_read) asks only when other reads change.
            if action == "alias" or changed - {evidence_id}:
                _expect(body, {"raw": raw, "brand": key, "reads": len(changed),
                               "known_brand": raw_known},
                        f"Every read heard as “{raw}” becomes {typed}: confirm with the count.")
            out["reads"] = len(changed)
            out["rules"].append({"kind": "alias", "raw": raw, "brand": key})
    elif action == "not_ad":
        _judge(con, evidence_id, "not_ad", actor)
        put("not_ad", rkey,
            {"evidence_id": evidence_id, "episode_id": row["episode_id"],
             "start_s": row["orig_start_s"], "end_s": row["orig_end_s"],
             "show_title": row["show_title"], "brand": raw or None})
        out["rules"].append({"kind": "not_ad", "key": rkey})
    elif action == "self_promo":
        brand = _ad.canonical(_ad.alias_map(con), body.get("brand") or raw)
        show = str(row["show_title"] or "").strip()
        network = str(body.get("network") or show).strip()
        exact = _ad.fold(network) == _ad.fold(show)
        if not brand or not network:
            raise BadInput("self-promotion needs a brand and a show or network")
        promo_key = f"{brand}\t{'=' if exact else '~'}{_key(network)}"
        promo_value = {"brand": brand, "network": network, "exact": exact, "from_read": rkey}
        # Count first (UX-12b), measured on the real write; a missing or
        # other count raises and db.atomic rolls it back. refresh_rule_facts
        # writes only SQLite (its payload memo holds unchanged source rows).
        shown_before = _shown_reads(con)
        put("self_promo", promo_key, promo_value)
        _ad.refresh_rule_facts(con, actor, brands={brand})
        changed = _count_changes(shown_before, _shown_reads(con))
        moved |= changed
        where = "this show" if exact else f"shows with “{network}”"
        _expect(body, {"brand": brand, "network": network, "exact": exact, "reads": len(changed),
                       "known_brand": False},
                f"{brand} becomes self-promotion on {where}: confirm with the count.")
        out["reads"] = len(changed)
        out["rules"].append({"kind": "self_promo", "brand": brand, "network": network,
                             "exact": exact})
        answer.update(brand=brand, network=network, exact=exact)
    else:  # generic
        word = _key(body.get("word") or raw)
        if not word:
            raise BadInput("this read has no brand word to stoplist")
        if word in rules["alias"] or word in {c for c, _d in rules["alias"].values()}:
            raise BadInput(f"“{word}” is part of a brand alias; remove that rule first")
        # Count first, measured on the real write (rolled back on a miss).
        shown_before = _shown_reads(con)
        word_known = known_brand(con, word, rules)
        put("stoplist", word, {"from_read": rkey})
        _ad.refresh_rule_facts(con, actor, words={word})
        changed = _count_changes(shown_before, _shown_reads(con))
        moved |= changed
        _expect(body, {"word": word, "reads": len(changed), "known_brand": word_known},
                f"“{word}” becomes a generic word on every read: confirm with the count.")
        out["reads"] = len(changed)
        out["rules"].append({"kind": "stoplist", "word": word})
        answer["word"] = word
    # The rules this example made (kind, key, rule id): removing one
    # supersedes the example (export).
    made = []
    for r in out["rules"]:
        if not r.get("replaced"):
            key = _rule_key(r)
            made.append([r["kind"], key, con.execute(
                "SELECT id FROM ad_rules WHERE kind=? AND key=?", (r["kind"], key)).fetchone()[0]])
    if made:
        answer["rules"] = made
    label_id = con.execute(
        "INSERT INTO ad_labels (evidence_id, record_key, episode_id, action, answer,"
        " raw_brand, transcript, created_at, actor) VALUES (?,?,?,?,?,?,?,?,?)",
        (evidence_id, row["source_record_key"], row["episode_id"], action,
         json.dumps(answer, sort_keys=True), raw or None, row["quote"] or "",
         db.utcnow(), actor)).lastrowid
    reports = [r[0] for r in con.execute(
        "SELECT id FROM ad_reports"
        " WHERE status='open' AND (evidence_id=? OR (record_key IS NOT NULL AND record_key=?))",
        (evidence_id, row["source_record_key"]))]
    con.executemany("UPDATE ad_reports SET status='resolved', resolved_at=?, resolution=?"
                    " WHERE id=?", [(db.utcnow(), action, rid) for rid in reports])
    snapshot["reports"] = reports
    snapshot["after"] = _revisions(con, evidence_id)
    con.execute(UNDO_SCHEMA)
    con.execute("INSERT INTO ad_label_undo (label_id, snapshot) VALUES (?, ?)",
                (label_id, json.dumps(snapshot, sort_keys=True)))
    out["label_id"] = label_id
    out["reports_resolved"] = len(reports)
    out["corpus_changed"] = any(r["kind"] in ("alias", "stoplist", "self_promo")
                                for r in out["rules"])
    carry_into_trackers(con, sorted(moved))
    rev = db.bump_revision(con)
    db.audit(con, actor, "admin_review", "evidence", evidence_id,
             {"action": action, **answer, "rules": out["rules"], "reads": out["reads"]}, rev)
    out["aggregate_rev"] = rev
    return out


# -- Undo (Reno decision 11, T5 R2-08c) --------------------------------------------

def _revisions(con, evidence_id: str) -> dict:
    review = con.execute("SELECT revision FROM reviews WHERE evidence_id=?",
                         (evidence_id,)).fetchone()
    return {"review": review[0] if review else 0, "assoc": con.execute(
        "SELECT assoc_revision FROM evidence WHERE id=?", (evidence_id,)).fetchone()[0]}


def _undo_before(con, evidence_id: str) -> dict:
    """The read as it is before an action writes (act's undo snapshot).
    `untouched`: nobody had worked on the read, so imports could still
    refresh it; an Undo of this action puts that back (AD-03)."""
    from .import_sources import _has_row_edits
    review = con.execute("SELECT judgment FROM reviews WHERE evidence_id=?",
                         (evidence_id,)).fetchone()
    return {"judgment": review[0] if review else None,
            "untouched": not _has_row_edits(con, evidence_id),
            # PODS: model rows are display-only
            "assocs": [list(a) for a in con.execute(
                "SELECT brand, basis, origin, channel FROM brand_assocs WHERE evidence_id=?"
                " AND basis<>'model' ORDER BY id", (evidence_id,))],
            "rules_before": {}, "replaced": None, "reports": [], "after": None}


def _rule_row(con, kind: str, key: str) -> dict | None:
    row = con.execute("SELECT id, value, created_at, actor FROM ad_rules WHERE kind=? AND key=?",
                      (kind, key)).fetchone()
    return dict(row) if row else None


def undone_label_ids(con: sqlite3.Connection) -> set:
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ad_label_undo'"
                       ).fetchone():
        return set()
    return {r[0] for r in con.execute(
        "SELECT label_id FROM ad_label_undo WHERE undone_at IS NOT NULL")}


UNDO_ORIGIN = "admin-undo"
UNDO_REFUSED = "Nothing was undone."
_BRAND_ACTIONS = ("confirm", "pick", "type", "alias")


@db.atomic
def undo(con: sqlite3.Connection, evidence_id: str, actor: str = "owner",
         label_id=None) -> dict:
    """Undo the newest labelled action on a read (any action: each one
    records a label). Restores the read's brand associations and judgment
    as the action's snapshot had them just before it (brand actions; not_ad
    restores the judgment; self-promo and generic wrote neither), puts back
    a not-an-ad rule it replaced and any rule it overwrote, retires the
    rules it created (reason `undone`) and re-applies their reads as
    remove_rule does, reopens the member reports it resolved, and marks the
    label undone (export: superseded_reason `undone`). label_id, when
    given, must be that newest label. Anything that changed the read after
    the action (a later save on it, a changed review or brand, a rule it
    made removed or rewritten, a reload into a new row) is ReadChanged and
    nothing is written. One undo per action; an undone action has no
    second undo."""
    from . import ad_reads as _ad
    from . import customer as _customer
    from . import ops
    from .ops import BadInput
    require_tables(con)
    row = _read_row(con, evidence_id)
    if row is None:
        raise BadInput("unknown evidence")
    read = _customer.read_detail(con, evidence_id) or {}
    label = con.execute(
        "SELECT * FROM ad_labels WHERE evidence_id=? OR (record_key IS NOT NULL AND record_key=?)"
        " ORDER BY id DESC LIMIT 1", (evidence_id, row["source_record_key"])).fetchone()
    if label is None:
        raise BadInput("nothing to undo: no save on this read")
    if label_id is not None and (isinstance(label_id, bool) or label_id != label["id"]):
        raise _changed(read, "another save on it came after yours", UNDO_REFUSED)
    con.execute(UNDO_SCHEMA)
    kept = con.execute("SELECT snapshot, undone_at FROM ad_label_undo WHERE label_id=?",
                       (label["id"],)).fetchone()
    if kept is not None and kept["undone_at"]:
        raise BadInput("the last save on this read is already undone")
    if kept is None:
        raise BadInput("this save was made before Undo existed; correct the read on"
                       " Review evidence")
    try:
        ops._require_current(con, evidence_id)
    except BadInput:
        raise _changed(read, "it was reloaded into a new row", UNDO_REFUSED) from None
    if label["evidence_id"] != evidence_id:
        raise _changed(read, "it was reloaded into a new row", UNDO_REFUSED)
    snap = json.loads(kept["snapshot"])
    if _revisions(con, evidence_id) != snap["after"]:
        raise _changed(read, "its brand or judgment changed after that save", UNDO_REFUSED)
    made = json.loads(label["answer"]).get("rules") or []
    later = {m[2] for (a,) in con.execute("SELECT answer FROM ad_labels WHERE id>?",
                                          (label["id"],))
             for m in json.loads(a).get("rules") or []}
    for _kind, _key, rid in made:
        if rid in later or con.execute("SELECT 1 FROM ad_rules WHERE id=?", (rid,)).fetchone() is None:
            raise _changed(read, "a rule that save made was removed or changed since", UNDO_REFUSED)
    action = label["action"]
    # A save that made corpus-wide rules: the reads changed back are
    # measured on the real undo, as remove_rule does (D-03).
    bulk = any(kind in ("alias", "stoplist", "self_promo") for kind, _key, _rid in made)
    shown_before = _shown_reads(con) if bulk else None
    # Back to a read nobody had worked on: its revisions say so, and
    # imports refresh it again (snapshots before AD-03 lack the flag).
    from .import_sources import UNDO_UNTOUCHED_ORIGIN
    origin = UNDO_UNTOUCHED_ORIGIN if snap.get("untouched") else UNDO_ORIGIN
    if action in _BRAND_ACTIONS:
        ops.set_brand_assocs(con, evidence_id, [], row["assoc_revision"], actor,
                             history_origin=origin,
                             # PODS: model rows are display-only
                             kept=[tuple(a) for a in snap["assocs"] if a[1] != "model"])
    if action in _BRAND_ACTIONS + ("not_ad",):
        review = con.execute("SELECT revision FROM reviews WHERE evidence_id=?",
                             (evidence_id,)).fetchone()
        ops.save_review(con, evidence_id, {"judgment": snap["judgment"]},
                        review[0] if review else 0, actor, history_origin=origin)
    retired, words, brands = [], set(), set()
    for kind, key, rid in made:
        value = json.loads(con.execute("SELECT value FROM ad_rules WHERE id=?",
                                       (rid,)).fetchone()[0] or "{}")
        prior = snap["rules_before"].get(f"{kind}\t{key}")
        if prior:  # it overwrote a rule: that rule stands again as it was
            con.execute("UPDATE ad_rules SET value=?, created_at=?, actor=? WHERE id=?",
                        (prior["value"], prior["created_at"], prior["actor"], rid))
        else:
            _retire(con, rid, actor, "undone")
            retired.append({"kind": kind, "key": key})
        if kind in ("alias", "stoplist"):
            words.add(key)
        elif kind == "self_promo":
            brands.add(value.get("brand"))
            if prior:
                brands.add(json.loads(prior["value"] or "{}").get("brand"))
    replaced = snap.get("replaced")
    if replaced:  # the not-an-ad rule it replaced stands again, as it was
        r = replaced["rule"]
        con.execute("INSERT INTO ad_rules (id, kind, key, value, created_at, actor)"
                    " VALUES (?,?,?,?,?,?)",
                    (r["id"], r["kind"], r["key"], r["value"], r["created_at"], r["actor"]))
        con.execute("DELETE FROM ad_rules_removed WHERE id=?", (replaced["removed_id"],))
    if words or brands:
        _ad.refresh_rule_facts(con, actor, words=words, brands=brands)
    moved = _count_changes(shown_before, _shown_reads(con)) if bulk else set()
    reads = len(moved) if bulk else None
    reopened = 0
    for rid in snap["reports"]:
        rep = con.execute("SELECT reporter, status FROM ad_reports WHERE id=?", (rid,)).fetchone()
        if rep is None or rep["status"] != "resolved":
            continue
        # A member who reported the read again since keeps that newer report.
        if con.execute("SELECT 1 FROM ad_reports WHERE reporter=? AND status='open' AND"
                       " (evidence_id=? OR (record_key IS NOT NULL AND record_key=?))",
                       (rep["reporter"], evidence_id, row["source_record_key"])).fetchone():
            continue
        con.execute("UPDATE ad_reports SET status='open', resolved_at=NULL, resolution=NULL"
                    " WHERE id=?", (rid,))
        reopened += 1
    con.execute("UPDATE ad_label_undo SET undone_at=?, undone_by=? WHERE label_id=?",
                (db.utcnow(), actor, label["id"]))
    carry_into_trackers(con, [evidence_id, *sorted(moved - {evidence_id})])
    parts = []
    if action in _BRAND_ACTIONS:
        parts.append("the read's brand and judgment are back as they were")
    elif action == "not_ad":
        parts.append("the read's judgment is back as it was")
    if retired:
        parts.append(f"{len(retired)} rule{'' if len(retired) == 1 else 's'} retired")
    if reads is not None:
        parts.append(f"{reads} read{'' if reads == 1 else 's'} changed back")
    if replaced:
        parts.append("the earlier not-an-ad rule stands again")
    if reopened:
        parts.append(f"{reopened} member report{' is' if reopened == 1 else 's are'} open again")
    message = (("; ".join(parts) + ". ") if parts else "") + \
        "The labelled example is marked undone in the corrections export."
    rev = db.bump_revision(con)
    db.audit(con, actor, "admin_review_undo", "evidence", evidence_id,
             {"label_id": label["id"], "action": action, "retired": retired,
              "reports_reopened": reopened}, rev)
    out = {"action": action, "evidence_id": evidence_id, "label_id": label["id"],
           "rules_retired": retired, "reports_reopened": reopened,
           "corpus_changed": bool(words or brands),
           "message": message[0].upper() + message[1:], "aggregate_rev": rev}
    if reads is not None:
        out["reads"] = reads  # reads whose shown brand or self-promotion changed back
    return out


def _known_without(con, key: str, rules: dict, evidence_id: str) -> bool:
    """known_brand, not counting the confirmation just written on this read."""
    from . import ad_reads as _ad
    from .customer import BRAND_DISPLAY_NAMES
    if key in BRAND_DISPLAY_NAMES or key in rules["brand"] or key in _gazetteer_keys():
        return True
    if _ad.tables_present(con) and con.execute(
            "SELECT 1 FROM ad_brand_aliases WHERE canonical_key=? LIMIT 1", (key,)).fetchone():
        return True
    return any(_key(b) == key for (b,) in con.execute(
        "SELECT brand FROM brand_assocs WHERE basis='confirmed' AND origin<>'detector'"
        " AND evidence_id<>?", (evidence_id,)))


# -- Queue -------------------------------------------------------------------------

def _show_weight(rank) -> float:
    """Chart rank 1 counts double, rank 100 about once; unranked once."""
    return 1.0 + max(0, 101 - rank) / 100.0 if rank else 1.0


def queue(con: sqlite3.Connection, *, show: str = "", brand: str = "", reason: str = "",
          limit: int | None = 200) -> dict:
    """The review queue, most impact first: open member reports first,
    then the namer's needs-review reads nobody decided, by impact = how
    many reads carry the brand corpus-wide x the show's chart weight.
    Reads a rule already decided (an alias for their raw spelling,
    self-promotion) are gone. Filters: show title, brand key, reason
    (needs_review or a report reason)."""
    from . import ad_reads as _ad
    from .customer import customer_row_where
    require_tables(con)
    where, args = customer_row_where(con)  # the rows customers see
    aliases = _ad.alias_map(con)
    rules = load(con)
    indexes = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}

    def idx(name):  # a covering index when this workspace has it
        return f" INDEXED BY {name}" if name in indexes else ""
    freq: dict[str, int] = {}
    # ad_reads describes only the last load's (active) rows; its brand
    # index answers this without reading any row.
    for raw, n in con.execute(
            "SELECT brand_suggested, COUNT(*) FROM ad_reads" + idx("idx_ad_reads_named_brand") +
            " WHERE kind='named' AND self_promo=0 AND brand_suggested IS NOT NULL"
            " GROUP BY brand_suggested"):
        key = _ad.canonical(aliases, raw)
        freq[key] = freq.get(key, 0) + n
    ranks = dict(con.execute("SELECT show_title, rank FROM ad_show_ranks"))
    fell = {r[0] for r in con.execute("SELECT evidence_id FROM ad_fallthrough")}
    cols = ("SELECT e.id, e.source_record_key, e.episode_id, ar.brand_suggested,"
            " ar.display_start_s, ar.display_end_s, e.orig_start_s, e.orig_end_s,"
            " ep.show_title, ep.ep_title, ep.pub_date FROM evidence e"
            " JOIN episodes ep ON ep.id=e.episode_id LEFT JOIN ad_reads ar ON ar.evidence_id=e.id")
    rows = {r["id"]: dict(r, reasons=["needs_review"]) for r in con.execute(
        "SELECT e.id, e.source_record_key, e.episode_id, ar.brand_suggested,"
        " ar.display_start_s, ar.display_end_s, e.orig_start_s, e.orig_end_s,"
        " ep.show_title, ep.ep_title, ep.pub_date FROM ad_reads ar" + idx("idx_ad_reads_review")
        + " JOIN evidence e" + idx("idx_evidence_customer_light") + " ON e.id=ar.evidence_id"
        " JOIN episodes ep ON ep.id=e.episode_id"
        " LEFT JOIN reviews r ON r.evidence_id=e.id"
        f" WHERE {where} AND ar.kind='named' AND ar.self_promo=0 AND ar.needs_review=1"
        # Any read customers see under review waits here (ad_reads.brand_review_ids):
        # a judgment without a confirmed brand (operator page A / U) stays queued.
        " AND (r.judgment IS NULL OR r.judgment<>'not_ad') AND NOT EXISTS (SELECT 1 FROM brand_assocs a"
        " WHERE a.evidence_id=e.id AND a.origin<>'detector' AND a.basis='confirmed')", args)
        if not (r["brand_suggested"] and r["brand_suggested"] in rules["alias"]
                and r["id"] not in fell)}
    # Open reports follow their read's stable key to its active row.
    reports: dict[str, list] = {}
    for rep in con.execute("SELECT * FROM ad_reports WHERE status='open' ORDER BY id").fetchall():
        target = rep["evidence_id"]
        if rep["record_key"]:
            hit = con.execute(f"SELECT e.id FROM evidence e JOIN episodes ep ON ep.id=e.episode_id"
                              f" WHERE e.source_record_key=? AND {where}",
                              [rep["record_key"], *args]).fetchone()
            # Without named reads a report on a row pinned to an older
            # transcript (still an ad there) stays on that row.
            target = hit[0] if hit else (None if _ad.named_mode(con) else target)
        if target:
            reports.setdefault(target, []).append(rep["reason"])
    for evidence_id, reasons in reports.items():
        if evidence_id not in rows:
            row = con.execute(cols + " WHERE e.id=?", (evidence_id,)).fetchone()
            if row is None:
                continue
            rows[evidence_id] = dict(row, reasons=[])
        rows[evidence_id]["reasons"] += [f"report:{r}" for r in dict.fromkeys(reasons)]
        rows[evidence_id]["reports"] = len(reasons)
    items = []
    for r in rows.values():
        key = _ad.canonical(aliases, r["brand_suggested"]) if r["brand_suggested"] else None
        rank = ranks.get(r["show_title"])
        lo, hi = r["display_start_s"], r["display_end_s"]
        items.append({"evidence_id": r["id"], "record_key": r["source_record_key"],
                      "episode_id": r["episode_id"], "show_title": r["show_title"],
                      "ep_title": r["ep_title"], "pub_date": r["pub_date"],
                      "span": [r["orig_start_s"] if lo is None else lo,
                               r["orig_end_s"] if hi is None else hi],
                      "brand": key, "raw_brand": r["brand_suggested"],
                      "reasons": r["reasons"], "reports": r.get("reports", 0),
                      "brand_frequency": freq.get(key, 0) if key else 0, "show_rank": rank,
                      "impact": round((freq.get(key, 0) if key else 0) * _show_weight(rank), 2)})
    facets = {"shows": _counts(i["show_title"] or "" for i in items),
              "reasons": _counts(x for i in items for x in i["reasons"])}
    if show:
        items = [i for i in items if (i["show_title"] or "") == show]
    if brand:
        wanted = _ad.canonical(aliases, brand)
        items = [i for i in items if i["brand"] == wanted]
    if reason:
        items = [i for i in items if reason in i["reasons"]
                 or f"report:{reason}" in i["reasons"] or (reason == "report" and i["reports"])]
    items.sort(key=lambda i: (-i["reports"], -i["impact"], i["show_rank"] or 10 ** 6,
                              -(int((i["pub_date"] or "0000").replace("-", "")[:8] or 0)),
                              i["episode_id"], i["span"][0] or 0))
    if limit is not None:  # None: every item (the hosted owner route filters them)
        items_out = items[:max(1, min(int(limit), 1000))]
    else:
        items_out = items
    return {"items": items_out, "total": len(items),
            "facets": facets}


def _counts(values) -> list:
    out: dict = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return [{"value": k, "count": n} for k, n in sorted(out.items(), key=lambda kv: (-kv[1], kv[0]))]


def queue_item(con: sqlite3.Connection, evidence_id: str) -> dict | None:
    """What the review page shows for one read: show, episode, time, the
    read block (brand, candidates), the transcript lines of the read with
    one line either side, its open reports and earlier labels."""
    from . import customer as _customer
    from . import ops
    row = _read_row(con, evidence_id)
    if row is None:
        return None
    read = _customer.read_detail(con, evidence_id)
    lo = row["display_start_s"] if row["display_start_s"] is not None else row["orig_start_s"]
    hi = row["display_end_s"] if row["display_end_s"] is not None else row["orig_end_s"]
    tv = con.execute("SELECT transcript_version_id FROM evidence WHERE id=?",
                     (evidence_id,)).fetchone()[0]
    segments = ops.transcript_segments(con, row["episode_id"], tv) if lo is not None else []
    inside = [i for i, s in enumerate(segments) if s["end"] > lo and s["start"] < hi]
    lines = []
    if inside:
        first, last = max(0, inside[0] - 1), min(len(segments) - 1, inside[-1] + 1)
        lines = [{"start": s["start"], "end": s["end"], "text": s["text"],
                  "in_read": first + i in inside}
                 for i, s in enumerate(segments[first:last + 1])]
    reports = [dict(r) for r in con.execute(
        "SELECT id, reason, note, created_at, status FROM ad_reports WHERE status='open'"
        " AND (evidence_id=? OR (record_key IS NOT NULL AND record_key=?)) ORDER BY id",
        (evidence_id, row["source_record_key"]))]
    labels = [dict(r, answer=json.loads(r["answer"])) for r in con.execute(
        "SELECT action, answer, created_at FROM ad_labels WHERE evidence_id=?"
        " OR (record_key IS NOT NULL AND record_key=?) ORDER BY id",
        (evidence_id, row["source_record_key"]))]
    return {"evidence_id": evidence_id, "record_key": row["source_record_key"],
            "episode_id": row["episode_id"], "show_title": row["show_title"],
            "ep_title": row["ep_title"], "pub_date": row["pub_date"], "span": [lo, hi],
            "raw_brand": _raw_brand(row) or None, "read": read, "transcript": lines,
            "reports": reports, "labels": labels}


def known_brands(con: sqlite3.Connection, q: str = "", limit: int = 12) -> list:
    """Type-ahead over canonical brands (the alias table's, the brand
    rules', the curated list), prefix matches first, then by how many
    reads carry them."""
    from . import ad_reads as _ad
    from .customer import BRAND_DISPLAY_NAMES
    needle = _key(q)
    names: dict[str, str] = {k: v for k, v in BRAND_DISPLAY_NAMES.items()}
    counts: dict[str, int] = {}
    if _ad.tables_present(con):
        for canonical, display in con.execute(
                "SELECT canonical_key, display FROM ad_brand_aliases WHERE canonical_key<>''"):
            if display or canonical not in names:
                names[canonical] = display or names.get(canonical) or canonical
        aliases = _ad.alias_map(con)
        for raw, n in con.execute("SELECT brand_suggested, COUNT(*) FROM ad_reads"
                                  " WHERE brand_suggested IS NOT NULL GROUP BY brand_suggested"):
            key = _ad.canonical(aliases, raw)
            counts[key] = counts.get(key, 0) + n
    for key, display in load(con)["brand"].items():
        names.setdefault(key, display or key)
    stop = load(con)["stoplist"]
    hits = [(k, d) for k, d in names.items() if k not in stop
            and (not needle or needle in k or needle in _key(d))]
    hits.sort(key=lambda kd: (not (kd[0].startswith(needle) or _key(kd[1]).startswith(needle)),
                              -counts.get(kd[0], 0), kd[0]))
    return [{"key": k, "display": d, "reads": counts.get(k, 0)} for k, d in hits[:limit]]


# -- Member reports ----------------------------------------------------------------

def report(con: sqlite3.Connection, ref: str, reason: str, note: str = "", ctx=None,
           now: datetime | None = None) -> dict:
    """A member reports an ad as wrong. The ref must be a current or
    pinned evidence publication the member may read in full; nothing the
    members see changes. One open report per member and read (a second
    one updates it); at most REPORT_LIMIT new reports per member per
    hour (RateLimited)."""
    from . import citations
    from .access import AuthorizationError
    from .ops import BadInput
    if reason not in REPORT_REASONS:
        raise BadInput("reason must be one of " + ", ".join(REPORT_REASONS))
    if not isinstance(note, str) or len(note) > NOTE_MAX:
        raise BadInput(f"note must be text of at most {NOTE_MAX} characters")
    if not isinstance(ref, str) or not ref.startswith("sr1:"):
        raise BadInput("ref must be a source reference")
    resolved = citations.resolve_source_state(con, ref, ctx=ctx, include_text=False)
    # A full record, or the server's excerpt-level projection (Plan A,
    # citations.disclosure_level); restricted sparse projections stay refused.
    full_or_excerpt = ("canonical_source_ref" in resolved
                       or resolved.get("disclosure") == "excerpt")
    if not full_or_excerpt or resolved.get("kind") != "evidence":
        raise AuthorizationError("record unavailable", 404)
    evidence_id = resolved["record_id"]
    key = con.execute("SELECT source_record_key FROM evidence WHERE id=?",
                      (evidence_id,)).fetchone()
    record_key = key[0] if key else None
    reporter = (getattr(ctx, "principal_id", None) or "owner") if ctx is not None else "owner"
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    open_row = con.execute(
        "SELECT id FROM ad_reports WHERE reporter=? AND status='open' AND"
        " (evidence_id=? OR (record_key IS NOT NULL AND record_key=?))",
        (reporter, evidence_id, record_key)).fetchone()
    replaced = open_row is not None
    if open_row is not None:
        con.execute("UPDATE ad_reports SET reason=?, note=? WHERE id=?",
                    (reason, note.strip(), open_row[0]))
        report_id = open_row[0]
    else:
        since = (now - REPORT_WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ")
        recent = con.execute("SELECT COUNT(*) FROM ad_reports WHERE reporter=? AND created_at>?",
                             (reporter, since)).fetchone()[0]
        if recent >= REPORT_LIMIT:
            raise RateLimited("too many reports; try again later")
        report_id = con.execute(
            "INSERT INTO ad_reports (evidence_id, record_key, ref, reporter, reason, note,"
            " created_at) VALUES (?,?,?,?,?,?,?)",
            (evidence_id, record_key, ref, reporter, reason, note.strip(), stamp)).lastrowid
    # "replaced": the member already had an open report on this read and
    # this one updated it (the page says so instead of a plain thank-you).
    return {"report": {"id": report_id, "status": "received", "replaced": replaced},
            "message": "Thanks — we'll check it."}


# -- Export ------------------------------------------------------------------------

# A removed rule supersedes the examples it came from. Examples recorded
# before they named their rules match by the rule's read and kind.
_KIND_ACTIONS = {"alias": ("type", "alias"), "stoplist": ("generic",),
                 "self_promo": ("self_promo",), "not_ad": ("not_ad",),
                 "brand": ("confirm", "pick", "type", "alias")}


def reviewed_reads(con: sqlite3.Connection) -> list:
    """Every current read a person reviewed in the app, from any page: a
    review judgment or a brand a person confirmed (export). Current: the
    rows customers see and operator saves reach (customer.customer_row_where),
    so a save on a read pinned to an older transcript is here."""
    from .customer import customer_row_where
    where, args = customer_row_where(con)
    brands: dict[str, list] = {}
    assoc_at: dict[str, str] = {}
    for eid, brand, at in con.execute(
            "SELECT evidence_id, brand, updated_at FROM brand_assocs WHERE origin<>'detector'"
            " AND basis='confirmed' ORDER BY id"):
        key = _key(brand)
        if key and key not in brands.setdefault(eid, []):
            brands[eid].append(key)
        assoc_at[eid] = max(assoc_at.get(eid, ""), at or "")
    out = []
    for r in con.execute(
            "SELECT e.id, e.source_record_key, e.episode_id, ep.show_title, e.orig_start_s,"
            " e.orig_end_s, r.judgment, r.updated_at FROM evidence e"
            " JOIN episodes ep ON ep.id=e.episode_id LEFT JOIN reviews r ON r.evidence_id=e.id"
            f" WHERE {where} ORDER BY e.episode_id, e.orig_start_s, e.id", args):
        if r["judgment"] is None and not brands.get(r["id"]):
            continue
        out.append({"record_key": r["source_record_key"], "evidence_id": r["id"],
                    "episode_id": r["episode_id"], "show_title": r["show_title"],
                    "start_s": r["orig_start_s"], "end_s": r["orig_end_s"],
                    "judgment": r["judgment"], "brands": sorted(brands.get(r["id"], [])),
                    "reviewed_at": max((r["updated_at"] if r["judgment"] is not None else "") or "",
                                       assoc_at.get(r["id"], ""))})
    return out


def export(con: sqlite3.Connection) -> dict:
    """The corrections file the pipeline reads (SETUP.md, "Ad review
    corrections file", format earwitness-corrections/3): every rule with
    who made it and when, the rules removed, replaced or undone
    (superseded), the labeled examples (superseded when a later action on
    the same read replaced them, a rule they made was removed, or the
    operator undid the action) and every read a person reviewed."""
    from . import ad_reads as _ad
    rules = load(con)
    meta = {(r["kind"], r["key"]): {"created_at": r["created_at"], "actor": r["actor"],
                                    "from_read": r["value"].get("from_read")}
            for r in rules["rows"]}
    labels, superseded, reviewed = [], [], []
    if present(con):
        removed = con.execute("SELECT * FROM ad_rules_removed ORDER BY id").fetchall()
        gone_ids = {r["rule_id"] for r in removed if r["reason"] == "removed"}
        rows = con.execute("SELECT l.*, ep.show_title FROM ad_labels l LEFT JOIN episodes ep"
                           " ON ep.id=l.episode_id ORDER BY l.id").fetchall()
        undone = undone_label_ids(con)
        last = {}  # an undone action never replaced the one before it
        for r in rows:
            if r["id"] not in undone:
                last[r["record_key"] or r["evidence_id"]] = r["id"]
        for r in rows:
            ev = con.execute("SELECT orig_start_s, orig_end_s FROM evidence WHERE id=?",
                             (r["evidence_id"],)).fetchone()
            answer = json.loads(r["answer"])
            made = answer.pop("rules", None)
            if made is not None:
                gone = any(m[2] in gone_ids for m in made)
            else:
                gone = any(g["reason"] == "removed" and r["action"] in _KIND_ACTIONS[g["kind"]]
                           and json.loads(g["value"]).get("from_read") == r["record_key"]
                           and g["removed_at"] >= r["created_at"] for g in removed)
            reason = "undone" if r["id"] in undone \
                else "replaced" if last.get(r["record_key"] or r["evidence_id"]) != r["id"] \
                else "removed" if gone else None
            labels.append({"record_key": r["record_key"], "evidence_id": r["evidence_id"],
                           "episode_id": r["episode_id"], "show_title": r["show_title"],
                           "start_s": ev[0] if ev else None, "end_s": ev[1] if ev else None,
                           "action": r["action"], "answer": answer,
                           "raw_brand": r["raw_brand"], "transcript": r["transcript"],
                           "at": r["created_at"], "actor": r["actor"],
                           "rules": [{"kind": m[0], "key": m[1]} for m in made or []],
                           "superseded": reason is not None, "superseded_reason": reason})
        for r in removed:
            superseded.append({"kind": r["kind"], "key": r["key"], "value": json.loads(r["value"]),
                               "created_at": r["created_at"], "actor": r["actor"],
                               "removed_at": r["removed_at"], "removed_by": r["removed_by"],
                               "reason": r["reason"]})
    reviewed = reviewed_reads(con)

    def display(key, stored):
        if stored and stored != stored.lower():
            return stored
        return _ad.brand_display(con, key)[0] if _ad.tables_present(con) else _ad.title_case(key)
    return {"format": EXPORT_FORMAT, "exported_at": db.utcnow(),
            "brand_aliases": [{"raw": raw, "canonical": c, "display": d, **meta[("alias", raw)]}
                              for raw, (c, d) in sorted(rules["alias"].items())],
            "stoplist": [{"word": w, **meta[("stoplist", w)]} for w in sorted(rules["stoplist"])],
            "self_promo": [{"brand": r["value"]["brand"], "network": r["value"]["network"],
                            "exact_show": bool(r["value"].get("exact")),
                            "created_at": r["created_at"], "actor": r["actor"],
                            "from_read": r["value"].get("from_read")}
                           for r in rules["rows"] if r["kind"] == "self_promo"],
            "not_ads": [{"record_key": k, "evidence_id": v.get("evidence_id"),
                         "episode_id": v.get("episode_id"), "show_title": v.get("show_title"),
                         "start_s": v.get("start_s"), "end_s": v.get("end_s"),
                         "brand": v.get("brand"), "created_at": v["created_at"],
                         "actor": v["actor"]} for k, v in rules["not_ad"].items()],
            "new_brands": [{"brand": k, "display": display(k, d), **meta[("brand", k)]}
                           for k, d in sorted(rules["brand"].items())],
            "superseded": superseded,
            "labels": labels,
            "reviewed_reads": reviewed}


def set_show_ranks(con: sqlite3.Connection, shows: list) -> int:
    """Chart ranks from a shows list (corpus_run --shows): the order of the
    chart shows (source "apple-top…"); other shows stay unranked."""
    con.execute("DELETE FROM ad_show_ranks")
    rank = 0
    for show in shows:
        if isinstance(show, dict) and str(show.get("source") or "").startswith("apple-top") \
                and show.get("title"):
            rank += 1
            con.execute("INSERT OR IGNORE INTO ad_show_ranks (show_title, rank) VALUES (?, ?)",
                        (show["title"], rank))
    return rank
