#!/usr/bin/env python3
"""Before/after facts of an ad rules re-import (docs/hosting/ad-rules-reimport.md).

  adrules_reimport_check.py corpus-gate DB EPISODES.json NAMED_READS.json
  adrules_reimport_check.py snapshot DB OUT.json [--replace]
  adrules_reimport_check.py compare BEFORE.json AFTER.json
  adrules_reimport_check.py switch-off-count DB

Read-only. Opens DB with mode=ro&immutable=1, so run it only while the web
and every writer are stopped, or on a copy made with SQLite's backup API
(never a cp of a live file). It reads the main file only,
so it refuses (exit 2) while a non-empty DB-wal or DB-journal file is
present (pilot_inventory.journal_unresolved; an empty one is a stopped
database's). Run from the release with the release's venv.

corpus-gate decides whether re-importing a corpus is safe: exit 0 when
the workspace still holds exactly that corpus (the active episodes file
lists exactly the episodes of EPISODES.json, and the active named-reads
version is NAMED_READS.json, by sha256); 1 otherwise (a nightly import
has loaded more since, and a re-import would roll its content back).

snapshot never overwrites OUT.json (exit 2) unless --replace is given.

snapshot writes, for App's comparison:
- seed: row counts and digests of the seed tables (campaigns, targets,
  history, attached evidence, saved items, alerts, alert events); an
  alert's baseline members are left out of its row's digest and kept
  apart in alert_members (one digest per member, one without its ref and
  transition in alert_member_cores, and one without its ref, transition
  and offer in alert_member_bases), because a rules-on load adds the
  reads the rules named to the baselines, re-points those already there
  and refreshes the offer signature of those the curated list re-keys
  (alert_carry); alert_carry_repoints lists, per alert carry audit row,
  the members it re-pointed, and alert_carry_offer_refreshed those whose
  offer signature it refreshed; alert_member_offers holds each member's
  stored offer signature and alert_member_live_offers the one the check
  computes now (customer._alert_snapshot of each active alert with a
  complete baseline, with the curated list merged as the workspace's last
  load recorded, import_sources.AD_RULES_DISPLAY_META: what the web shows
  when it starts with the setting that load had);
- human: digests of clips, operator rules and labels, label undos, member
  ad reports, and reviews;
- confirmed_brands: the digest of every confirmed brand association;
- refs: every sr1 reference any text column of any table holds (the seed
  tables, audit details, alert baselines, ...; source_states.id is the
  state itself, not a reference); each must have its source_states row
  and resolve through the app;
- source_states, review_history and association_history: one digest per
  row (a load may add rows; it never changes or deletes one);
- reviewed_ads_hidden: reads reviewed as an ad that are not customer rows
  (a switch-off hides the R1b rows among them);
- observations: attached evidence per campaign;
- numbers: customer ads, "Brand not identified" reads and ads, the Brands
  list, and named brands (as scripts/adrules_measure.py demo-count counts),
  and clip_guard: what the web's clip guard (CLIPGUARD R8) would refuse.

compare exits 0 when the seed, human work, confirmations, earlier source
states, earlier history rows, every alert's earlier baseline members and
observations are all unchanged (a load may add baseline members, and may
change a member's ref and transition only when an alert carry audit row
newer than BEFORE.json lists it as re-pointed, and its offer signature too
when such a row lists it as offer_refreshed, and then only when its stored
offer equals the live one in BEFORE.json and again in AFTER.json:
the refresh took the merge's change and no other), every
earlier reference is still there and resolves (a load may add references,
which must resolve too), and no read reviewed as an ad left customers
(reviewed_ads_shown); 1 otherwise. It prints both sides' numbers, and
(cited_reads_no_longer_shown, never a check) the reads a campaign's
observations, a saved item or a campaign link cites that customers saw as ads
before and do not see after.

switch-off-count, run before EARWITNESS_AD_RULES=off, counts the rows the
rules added that people worked on: R1b rows, R6 split parts (record key
holds ".split-") and R9 rows (".adrules-r9."), the last two only after a
load with EARWITNESS_AD_RULES_DET on. It prints rule_rows and touched_rows
(every kind together) and, apart, rule_rows_by_kind and touched_rows_by_kind
({"r1b", "split", "r9"}). The work counted is confirmations, reviews, member
reports (customer_mistake_reports, by reference), ad_reports, labels,
label undos and association history a person made (revision 1 on), and
the rest of lineage.human_work_ids's human work: brand associations a
person made (origin not detector), observations, campaign links and
checks, placement and target evidence and saved captures (by reference).
Alert baseline members and events are not counted: the alert carry makes
a rule-added row a member. A load with the switch off hides those rows from
customers (a load with the DET setting off hides the R6 parts and R9 rows
the same way); the rows and the work stay in the database and show again
when the switch is back on. Exit 0.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SEED = ("campaigns", "campaign_targets", "campaign_history", "target_evidence",
        "customer_saved_items", "customer_alerts", "customer_alert_events")
HUMAN = ("ad_clips", "ad_rules", "ad_rules_removed", "ad_labels", "ad_label_undo",
         "ad_reports", "reviews")
BRANDS = ("quince", "ag1", "betterhelp", "nordvpn", "shopify")
REF = re.compile(r"sr1:[0-9a-f]{32}")


class Refused(Exception):
    pass


def _open(path: str) -> sqlite3.Connection:
    from earwitness.app import ad_reads, db
    from scripts import pilot_inventory
    if pilot_inventory.journal_unresolved(path):
        raise Refused(f"{path} has a non-empty -wal or -journal file: stop every writer"
                      " (or check a copy) first")
    con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True, isolation_level=None,
                          factory=db._Connection)
    con.row_factory = sqlite3.Row
    ad_reads.register_functions(con)
    return con


def _rows(con, table, where=""):
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (table,)).fetchone():
        return None
    cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
    names = ", ".join(f'"{c}"' for c in cols)
    order = ", ".join(str(i + 1) for i in range(len(cols)))
    return [json.dumps(list(r), default=str) for r in
            con.execute(f'SELECT {names} FROM "{table}" {where} ORDER BY {order}')]


def _digest(con, table, where=""):
    rows = _rows(con, table, where)
    if rows is None:
        return None
    return {"rows": len(rows), "sha256": hashlib.sha256("\n".join(rows).encode()).hexdigest()}


def _hashes(con, table):
    return sorted(hashlib.sha256(r.encode()).hexdigest() for r in _rows(con, table) or ())


def _refs(con) -> set:
    """Every sr1 reference in any text column of any ordinary table."""
    refs = set()
    for name, sql in con.execute("SELECT name, sql FROM sqlite_master WHERE type='table'"
                                 " ORDER BY name").fetchall():
        if (sql or "").upper().startswith("CREATE VIRTUAL"):
            continue
        for col in [r[1] for r in con.execute(f'PRAGMA table_info("{name}")')]:
            if (name, col) == ("source_states", "id"):
                continue
            for (value,) in con.execute(f'SELECT "{col}" FROM "{name}" WHERE'
                                        f' typeof("{col}")=\'text\' AND instr("{col}", \'sr1:\')>0'):
                refs.update(REF.findall(value))
    return refs


def _numbers(con) -> dict:
    from earwitness.app import ad_breaks, customer, customer_query, db
    q = {"version": 2, "mode": "ads", "q": "", "brands": None, "shows": None,
         "categories": None, "statuses": None, "audio": None, "date_status": "all",
         "from": None, "to": None, "sort": "newest", "page": 1, "ids": None}
    with db.transaction(con, write=False), customer.model_names(False):  # PODS: model rows are display-only
        rows = customer._customer_ad_rows(con)
        unnamed = {r["id"] for r in rows if not r["_brand_keys"]}
        ads = {}
        for ad in ad_breaks.scope_index(con).values():
            ads.setdefault(ad["id"], ad)
        unnamed_ads = sum(1 for ad in ads.values() if all(rd["key"] in unnamed
                                                          for rd in ad["reads"]))
        result = customer_query.research_bundle(con, q, None, [], False, "")["result"]
        directory = {b["key"]: b["records"] for b in customer.brands_directory(con)}
        guard = _clip_guard(con)
    return {"search_ads": result["break_total"], "reads": len(rows),
            "unnamed_reads": len(unnamed), "ads": len(ads), "unnamed_ads": unnamed_ads,
            "rule_rows": sum(1 for r in rows if ".adrules-r1b." in (r["source_record_key"] or "")),
            "brands_list": len(directory), "brands": {k: directory.get(k) for k in BRANDS},
            "clip_guard": guard}


def _clip_guard(con) -> dict:
    """CLIPGUARD R8, what the web's clip guard would do: whether the clean
    list is there; named reads R8 judged clean (on the list), marked, marked
    with a linked clip (the clips the guard stops), never judged; and the
    linked clips the guard refuses in all (marked or unjudged)."""
    from earwitness.app import ad_reads
    shown = ad_reads.shown_clips(con)
    verdict = {f"named:{r.get('candidate_id')}": r.get("clip_hidden")
               for r in ad_reads.loaded_named(con)}
    reads = con.execute("SELECT evidence_id, record_key, clip_id FROM ad_reads"
                        " WHERE kind='named'").fetchall()
    marked = [r for r in reads if verdict.get(r[1]) is True]
    return {"list": shown is not None, "shown": len(shown or ()),
            "hidden": len(marked), "hidden_with_clip": sum(1 for r in marked if r[2]),
            "unjudged": sum(1 for r in reads if not isinstance(verdict.get(r[1]), bool)),
            "refused_with_clip": sum(1 for r in reads if r[2] and r[0] not in (shown or ()))}


def _reviewed_ads_hidden(con) -> list:
    """Reads a person reviewed as an ad that customers do not see."""
    from earwitness.app import customer, db
    with db.transaction(con, write=False):
        shown = {r["id"] for r in customer._customer_ad_rows(con)}
    return sorted(r[0] for r in con.execute("SELECT evidence_id FROM reviews WHERE judgment='ad'")
                  if r[0] not in shown)


def _cited_ads_shown(con) -> list:
    """Evidence ids a campaign or a person holds on to (target_evidence refs,
    saved items' target and captured refs, campaign_evidence) that customers
    see as ads now."""
    from earwitness.app import customer, db, lineage
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    cited = set()
    if "campaign_evidence" in tables:
        cited.update(r[0] for r in con.execute("SELECT evidence_id FROM campaign_evidence"))
    refs = []
    if "target_evidence" in tables:
        refs += [r[0] for r in con.execute("SELECT ref FROM target_evidence")]
    refs += [ref for _item, target, captured in lineage._saved_refs(con)
             for ref in (target, captured)]
    if "source_states" in tables:
        records = lineage._ref_records(con, refs)
        cited.update(records[s] for s in map(lineage._state_id, refs) if s in records)
    with db.transaction(con, write=False):
        shown = {r["id"] for r in customer._customer_ad_rows(con)}
    return sorted(cited & shown)


# The rows the rules add, by kind (ad_rules.py ids): R1b "<ep>.adrules-r1b.<ms>-<ms>",
# R6 split part "<id>.split-<ms>-<ms>", R9 "<ep>.adrules-r9.<ms>-<ms>".
RULE_KINDS = (("r1b", ".adrules-r1b."), ("split", ".split-"), ("r9", ".adrules-r9."))


def _kind_sql(marker: str) -> str:
    return ("SELECT id FROM evidence WHERE source_record_key LIKE 'named:%'"
            f" AND instr(source_record_key, '{marker}')>0")


R1B_SQL = _kind_sql(RULE_KINDS[0][1])


def switch_off_count(path: str) -> dict:
    """The rows the rules added that people worked on (see the module doc)."""
    from earwitness.app import lineage
    from earwitness.app.import_sources import MACHINE_ORIGINS_SQL
    con = _open(path)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        kinds = {kind: {r[0] for r in con.execute(_kind_sql(marker))}
                 for kind, marker in RULE_KINDS}
        rule = set().union(*kinds.values())
        queries = {
            "confirmations": "SELECT evidence_id FROM brand_assocs WHERE basis='confirmed'",
            "reviews": "SELECT evidence_id FROM reviews",
            "customer_mistake_reports": "SELECT s.record_id FROM customer_mistake_reports m"
                                        " JOIN source_states s ON s.id=m.ref"
                                        " WHERE s.kind='evidence'",
            "ad_reports": "SELECT evidence_id FROM ad_reports",
            "ad_labels": "SELECT evidence_id FROM ad_labels",
            "ad_label_undo": "SELECT l.evidence_id FROM ad_label_undo u JOIN ad_labels l"
                             " ON l.id=u.label_id",
            # Revision 0 is the row as the load wrote it; later machine
            # revisions are the load's refreshes.
            "association_history": "SELECT evidence_id FROM association_history WHERE"
                                   " association_revision>0 AND history_origin NOT IN"
                                   f" {MACHINE_ORIGINS_SQL}",
            # ADI-R4-03: lineage.human_work_ids's other human work, without
            # alert baseline members and events.
            "brand_associations": "SELECT evidence_id FROM brand_assocs WHERE"
                                  " origin<>'detector' AND basis<>'model'",  # PODS: model rows are display-only
            "observations": "SELECT id FROM evidence WHERE observation_revision_id"
                            " IS NOT NULL",
            "campaign_evidence": "SELECT evidence_id FROM campaign_evidence",
            "campaign_checks": "SELECT evidence_id FROM campaign_checks WHERE"
                               " evidence_id IS NOT NULL"}
        need = {"confirmations": ("brand_assocs",), "brand_associations": ("brand_assocs",),
                "customer_mistake_reports": ("customer_mistake_reports", "source_states"),
                "ad_label_undo": ("ad_label_undo", "ad_labels"), "observations": ("evidence",)}
        by_table, touched = {}, set()
        for name, sql in queries.items():
            if not set(need.get(name, (name,))) <= tables:
                by_table[name] = 0
                continue
            ids = [r[0] for r in con.execute(sql) if r[0] in rule]
            by_table[name] = len(ids)
            touched.update(ids)
        # Citations and saved captures, by reference (lineage._ref_records).
        cited = {"placement_evidence": [], "target_evidence": []}
        for name in cited:
            if name in tables:
                cited[name] = [r[0] for r in con.execute(f"SELECT ref FROM {name}")]
        cited["saved_captures"] = [ref for _item, target, captured in lineage._saved_refs(con)
                                   for ref in (target, captured)]
        for name, refs in cited.items():
            records = lineage._ref_records(con, refs) if "source_states" in tables else {}
            ids = [records[s] for s in map(lineage._state_id, refs)
                   if s in records and records[s] in rule]
            by_table[name] = len(ids)
            touched.update(ids)
    finally:
        con.close()
    return {"rule_rows": len(rule), "touched_rows": len(touched), "by_table": by_table,
            "evidence_ids": sorted(touched),
            "rule_rows_by_kind": {k: len(ids) for k, ids in kinds.items()},
            "touched_rows_by_kind": {k: len(ids & touched) for k, ids in kinds.items()}}


def _member_digest(member, core=False, base=False) -> str:
    """A baseline member's digest; core=True leaves out its ref and
    transition (what a re-point by the alert carry changes), base=True its
    offer signature too (what an offer refresh changes)."""
    if (core or base) and isinstance(member, dict):
        left = ("ref", "transition", "offer") if base else ("ref", "transition")
        member = {k: v for k, v in member.items() if k not in left}
    return hashlib.sha256(json.dumps(member, sort_keys=True).encode()).hexdigest()


def _alerts(con) -> tuple:
    """(digest of customer_alerts without baseline members, {alert id:
    {member: digest}}, {alert id: {member: digest without ref and
    transition}}, {alert id: {member: digest without ref, transition and
    offer}})."""
    rows = _rows(con, "customer_alerts")
    if rows is None:
        return None, {}, {}, {}
    cols = [r[1] for r in con.execute('PRAGMA table_info("customer_alerts")')]
    at, ident = cols.index("baseline_json"), cols.index("id")
    kept, members, cores, bases = [], {}, {}, {}
    for text in rows:
        row = json.loads(text)
        try:
            baseline = json.loads(row[at] or "{}")
        except (TypeError, ValueError):
            baseline = None
        if isinstance(baseline, dict) and isinstance(baseline.get("members"), dict):
            found = baseline.pop("members")
            members[str(row[ident])] = {k: _member_digest(v) for k, v in found.items()}
            cores[str(row[ident])] = {k: _member_digest(v, True) for k, v in found.items()}
            bases[str(row[ident])] = {k: _member_digest(v, base=True) for k, v in found.items()}
            row[at] = json.dumps(baseline, sort_keys=True)
        kept.append(json.dumps(row))
    return ({"rows": len(kept), "sha256": hashlib.sha256("\n".join(kept).encode()).hexdigest()},
            members, cores, bases)


def _alert_offers(con) -> tuple:
    """({alert id: {member: stored offer signature}}, {alert id: {member:
    offer signature the check computes now}}), the second for each active
    alert with a complete baseline (alert_carry._skip_reason; an alert whose
    snapshot fails has none), with the curated list merged as the last load
    recorded (not this process's setting)."""
    from earwitness.app import alert_carry, customer, db, import_sources, ops, search
    stored, live = {}, {}
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                       " AND name='customer_alerts'").fetchone():
        return stored, live
    state = con.execute("SELECT value FROM meta WHERE key=?",
                        (import_sources.AD_RULES_DISPLAY_META,)).fetchone()
    merged = bool(state and state[0] and state[0] != "off")
    with db.transaction(con, write=False), customer.curated_merge(merged):
        for alert in [dict(r) for r in con.execute("SELECT * FROM customer_alerts ORDER BY id")]:
            try:
                baseline = json.loads(alert["baseline_json"] or "{}")
            except (TypeError, ValueError):
                continue
            members = baseline.get("members") if isinstance(baseline, dict) else None
            if not isinstance(members, dict):
                continue
            stored[str(alert["id"])] = {k: v.get("offer") for k, v in members.items()
                                        if isinstance(v, dict)}
            if alert.get("workspace_id") != "local" or alert_carry._skip_reason(alert, baseline):
                continue
            q = json.loads(alert["query_json"])
            selector = json.loads(alert.get("live_selector_json") or "null")
            try:
                now = customer._alert_snapshot(con, q, selector or None,
                                               json.loads(alert["event_types"]))
            except (ops.BadInput, search.FilterRejected, ValueError, TypeError, KeyError):
                continue
            live[str(alert["id"])] = {k: v.get("offer") for k, v in now["members"].items()
                                      if k in members and isinstance(v, dict)}
    return stored, live


def _carry_repoints(con, field="repointed") -> dict:
    """{audit id: {alert id: [member]}}: the baseline members each alert
    carry audit row says it re-pointed (alert_carry, ADI-R4-01), or with
    field="offer_refreshed" whose offer signature it refreshed."""
    out = {}
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit'"
                       ).fetchone():
        return out
    for audit_id, detail in con.execute("SELECT id, detail FROM audit WHERE action="
                                        "'alert_baseline_carry' ORDER BY id"):
        try:
            alerts = json.loads(detail or "{}").get("alerts") or []
        except (TypeError, ValueError, AttributeError):
            continue
        out[str(audit_id)] = {str(a.get("alert_id")): [f"finding:{e}" for e in
                                                       a.get(field) or ()]
                              for a in alerts if isinstance(a, dict) and a.get(field)}
    return out


def _new_carry_members(before: dict, after: dict, key: str) -> dict:
    """{alert id: {member}}: the members the alert carry audit rows that the
    before-snapshot did not have list under `key` ("alert_carry_repoints" or
    "alert_carry_offer_refreshed"; a before-snapshot without the second
    knows its audit rows by the first)."""
    known = before.get(key, before.get("alert_carry_repoints", {}))
    out: dict = {}
    for audit_id, alerts in after.get(key, {}).items():
        if audit_id in known:
            continue
        for alert, ids in alerts.items():
            out.setdefault(alert, set()).update(ids)
    return out


def _baselines_kept(before: dict, after: dict) -> bool:
    """Every alert's earlier baseline members are still there as they were,
    except a member the alert carry re-pointed after the before-snapshot
    (an alert_baseline_carry audit row the before-snapshot did not have
    lists it), which may change its ref and transition only, or one such a
    row lists as offer_refreshed, which may change its offer signature too
    (its digest without ref, transition and offer is unchanged), and whose
    stored offer equals the live one in both snapshots (DBS-R2-02: a refresh
    that took a difference the alert had not reported, or a baseline
    rewritten afterwards, is refused)."""
    repointed = _new_carry_members(before, after, "alert_carry_repoints")
    refreshed = _new_carry_members(before, after, "alert_carry_offer_refreshed")
    for alert, members in before.get("alert_members", {}).items():
        now = after.get("alert_members", {}).get(alert, {})
        for member, digest in members.items():
            if now.get(member) == digest:
                continue
            if member not in now:
                return False
            cores = (before.get("alert_member_cores", {}).get(alert, {}).get(member),
                     after.get("alert_member_cores", {}).get(alert, {}).get(member))
            bases = (before.get("alert_member_bases", {}).get(alert, {}).get(member),
                     after.get("alert_member_bases", {}).get(alert, {}).get(member))
            if member in repointed.get(alert, ()) and cores[0] == cores[1]:
                continue
            if member in refreshed.get(alert, ()) and bases[0] is not None \
                    and bases[0] == bases[1] and all(
                        snap.get("alert_member_offers", {}).get(alert, {}).get(member, 0)
                        == snap.get("alert_member_live_offers", {}).get(alert, {}).get(member, 1)
                        for snap in (before, after)):
                continue
            return False
    return True


def snapshot(path: str) -> dict:
    from earwitness.app import citations, db
    con = _open(path)
    try:
        out = {"seed": {t: _digest(con, t) for t in SEED if t != "customer_alerts"},
               "human": {t: _digest(con, t) for t in HUMAN},
               "confirmed_brands": _digest(con, "brand_assocs", "WHERE basis='confirmed'")}
        out["seed"]["customer_alerts"], out["alert_members"], out["alert_member_cores"], \
            out["alert_member_bases"] = _alerts(con)
        out["alert_carry_repoints"] = _carry_repoints(con)
        out["alert_carry_offer_refreshed"] = _carry_repoints(con, "offer_refreshed")
        out["alert_member_offers"], out["alert_member_live_offers"] = _alert_offers(con)
        out["refs"] = sorted(_refs(con))
        out["refs_missing"] = [r for r in out["refs"] if not con.execute(
            "SELECT 1 FROM source_states WHERE id=?", (r,)).fetchone()]
        unresolved = []
        with db.transaction(con, write=False):
            for ref in out["refs"]:
                try:
                    if not citations.resolve_source_state(con, ref, include_text=False,
                                                          media=False):
                        unresolved.append(ref)
                except Exception as exc:  # noqa: BLE001 - reported, never raised
                    unresolved.append(f"{ref}: {type(exc).__name__}")
        out["refs_unresolved"] = unresolved
        out["source_states"] = _hashes(con, "source_states")
        out["review_history"] = _hashes(con, "review_history")
        out["association_history"] = _hashes(con, "association_history")
        out["reviewed_ads_hidden"] = _reviewed_ads_hidden(con)
        out["cited_ads_shown"] = _cited_ads_shown(con)
        out["observations"] = {r[0]: r[1] for r in con.execute(
            "SELECT c.name, count(te.ref) FROM campaigns c JOIN campaign_targets t"
            " ON t.campaign_id=c.id LEFT JOIN target_evidence te ON te.target_id=t.id"
            " GROUP BY c.id ORDER BY c.id")}
        out["numbers"] = _numbers(con)
    finally:
        con.close()
    return out


def compare(before: dict, after: dict) -> int:
    ok = {"seed": before["seed"] == after["seed"],
          "human": before["human"] == after["human"],
          "confirmed_brands": before["confirmed_brands"] == after["confirmed_brands"],
          "refs": set(before["refs"]) <= set(after["refs"]) and not after["refs_missing"]
          and not after["refs_unresolved"],
          "source_states_kept": set(before["source_states"]) <= set(after["source_states"]),
          "review_history_kept": set(before["review_history"]) <= set(after["review_history"]),
          "association_history_kept": set(before.get("association_history", ()))
          <= set(after.get("association_history", ())),
          "reviewed_ads_shown": set(after.get("reviewed_ads_hidden", ()))
          <= set(before.get("reviewed_ads_hidden", ())),
          "alert_baselines_kept": _baselines_kept(before, after),
          "observations": before["observations"] == after["observations"]}
    # Printed only, never a check (ADA-R2-03): reads a campaign or a person
    # holds on to that customers saw as ads before and do not see after.
    lost = sorted(set(before.get("cited_ads_shown", ())) - set(after.get("cited_ads_shown", ())))
    print(json.dumps({"all": all(ok.values()), "ok": ok,
                      "source_states": [len(before["source_states"]), len(after["source_states"])],
                      "reviewed_ads_hidden": after.get("reviewed_ads_hidden"),
                      "cited_reads_no_longer_shown": lost,
                      "refs": len(after["refs"]), "observations": after["observations"],
                      "numbers": {"before": before["numbers"], "after": after["numbers"]}},
                     indent=1))
    return 0 if all(ok.values()) else 1


def corpus_gate(path: str, episodes_path: str, named_path: str) -> dict:
    """Whether the workspace holds exactly the corpus (see the module doc)."""
    from earwitness.app.pilot_sync_checks import ROLE_SOURCE_KEYS
    try:
        corpus = json.loads(Path(episodes_path).read_bytes().decode("utf-8"))
        named_sha = hashlib.sha256(Path(named_path).read_bytes()).hexdigest()
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise Refused(f"the corpus files cannot be read: {exc}") from None
    if not isinstance(corpus, list):
        raise Refused(f"{episodes_path} is not a list of episodes")
    want = sorted(str(e.get("ep_id")) for e in corpus if isinstance(e, dict))
    con = _open(path)
    try:
        def active(key):
            return con.execute("SELECT sha256, payload FROM source_versions WHERE source_key=?"
                               " AND is_active=1 ORDER BY id DESC LIMIT 1", (key,)).fetchone()
        eps = active(ROLE_SOURCE_KEYS["episodes"])
        named = active(ROLE_SOURCE_KEYS["named"])
    finally:
        con.close()
    try:
        have = sorted(str(e.get("ep_id")) for e in json.loads(eps[1]) if isinstance(e, dict)) \
            if eps else None
    except (TypeError, ValueError):
        have = None
    ok = {"episodes": have == want,
          "named_reads": named is not None and named[0] == named_sha}
    return {"ok": all(ok.values()), "checks": ok, "corpus_episodes": len(want),
            "workspace_episodes": None if have is None else len(have),
            "only_in_workspace": None if have is None else len(set(have) - set(want)),
            "named_reads_sha256": {"corpus": named_sha, "workspace": named[0] if named else None}}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) == 4 and argv[0] == "corpus-gate":
        try:
            gate = corpus_gate(argv[1], argv[2], argv[3])
        except Refused as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(gate, indent=1))
        return 0 if gate["ok"] else 1
    if len(argv) == 2 and argv[0] == "switch-off-count":
        try:
            print(json.dumps(switch_off_count(argv[1]), indent=1))
        except Refused as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        return 0
    replace = argv[3:] == ["--replace"]
    if argv[:1] == ["snapshot"] and (len(argv) == 3 or (len(argv) == 4 and replace)):
        if Path(argv[2]).exists() and not replace:
            print(f"refused: {argv[2]} exists; keep it, or pass --replace (runbook resume"
                  " rule)", file=sys.stderr)
            return 2
        try:
            snap = snapshot(argv[1])
        except Refused as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        with open(argv[2], "w" if replace else "x") as f:
            json.dump(snap, f, indent=1, sort_keys=True)
        print(json.dumps({"refs": len(snap["refs"]), "refs_missing": snap["refs_missing"],
                          "refs_unresolved": snap["refs_unresolved"],
                          "observations": snap["observations"], "numbers": snap["numbers"]}))
        return 0 if not snap["refs_missing"] and not snap["refs_unresolved"] else 1
    if len(argv) == 3 and argv[0] == "compare":
        with open(argv[1]) as a, open(argv[2]) as b:
            return compare(json.load(a), json.load(b))
    print(__doc__, file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main())
