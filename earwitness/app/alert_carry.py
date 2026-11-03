"""Alert baselines absorb the reads a names run named (`carry-alert-baselines`).

R1-04 = A (Reno, 2026-10-16). `import-names` gives reads that were already in
the workspace a machine brand suggestion; a brand alert then matches them,
and its next check would report them as new findings although nothing new
aired. This publish-time step, run after import-names, adds exactly those
reads to the alert baselines, in one transaction:

    carry_alert_baselines(con, names_run_id)   `app_workspace.py carry-alert-baselines`

The reads to carry are the evidence ids the names_import audit row(s) of
that run wrote (decision "suggest" or "update"); nothing else. For every
active alert with a complete baseline, the current snapshot is computed with
the check's own membership (customer._alert_snapshot on the stored
definition). A carried read whose `finding:<id>` member is in that snapshot
but not in the stored baseline gets the snapshot's member added to the
baseline's members; the rest of the baseline (other new members, held
counts, revision, coverage, episode text) and the alert row stay as they
are. A read held for brand review is not a snapshot member, so it is never
carried and keeps holding. Paused or stopped alerts, incomplete baselines,
alert events and history, reads the run did not name and reads that arrive
later are never touched: those stay news. A run with nothing to carry
writes nothing, not even an audit row.

The same carry, on a list of evidence ids, is carry_reads. A corpus load
with the ad rules on calls it (import_sources.seed_all, ADA-R1-02) with
the reads the rules changed or added on episodes the workspace already had,
whose transcript copy the load did not change: the rules naming an old
read is not news. Reads of a new episode, or of an episode whose
transcript changed, stay news; with the rules off nothing is carried.
A load also passes repoint=True (ADI-R4-01): a carried read already in a
baseline whose publication moved (the rules switched off and on again)
gets that member's ref and transition from the current snapshot, keeping
its ordinal, as corrections.publish_keeping_trackers does; the audit row
lists it under "repointed", with the ref it had under "repointed_from"
(so that earlier reference stays on record).

DETBUILD: a load that changes what the display list merges (the DET flip,
its switch-off, another curated file; import_sources._rekeyed_old_reads)
also passes the old reads the curated list re-keys, which the namer did not
rename. Such a read starts (or stops) matching a brand alert, and its
attribution brands, so the offer signature an alert stored for it, change
though nothing new aired. Two guards keep real news news (DBC-R2-01,
DBS-R2-01, mirroring CONFIRM-QUIET):
- seen_only: such a read is carried only into an alert that had already
  seen it (corrections._seen_reads: a baseline member, a read held for
  brand review then, or one created before the alert's last successful
  check). A read that came after that check stays news. The same holds for
  every read a load with the rules on carries (DBC-R3-01): a namer read the
  rules wrote on, and a row the rules added (seen_via: an R6 part when the
  alert had seen the read it was split from; an R1b or R9 row when the
  alert's last check came after its episode's transcript copy).
- refresh_offer with prior: a member's stored offer signature is replaced
  by the snapshot's only when it equals the signature the check computed
  for it just before the load, under the merge the web had then
  (prior_offers, corrections._carried_members within
  customer.curated_merge). So a difference the alert had not yet reported
  (an offer edit, a confirmation) is still news. The read is one the namer
  did not rename and the importer writes no offer fields, so what changes
  its signature at that load is the list: its merge, and what the rules
  name with it (an attribution change the rules make under the new list
  is absorbed with it; DBC-R2-02). The member keeps its ordinal (and
  takes its ref and transition when repoint applies); the audit row lists
  it under "offer_refreshed".
"""
from __future__ import annotations

import datetime
import json

from . import db as _db

ACTION = "alert_baseline_carry"
WRITTEN = ("suggest", "update")


def names_run_reads(con, names_run_id: str) -> list[str]:
    """The evidence ids the names run wrote, in audit order."""
    rows = con.execute("SELECT detail FROM audit WHERE action='names_import' AND target_id=?"
                       " ORDER BY id", (names_run_id,)).fetchall()
    if not rows:
        raise ValueError(f"no names_import audit row for run {names_run_id!r};"
                         " run import-names first or pass its --run-id")
    out: list[str] = []
    for (detail,) in rows:
        for d in (json.loads(detail or "{}").get("decisions") or ()):
            if d.get("decision") in WRITTEN and d.get("evidence_id") not in out:
                out.append(d["evidence_id"])
    return out


def _skip_reason(alert: dict, baseline: dict) -> str:
    from . import customer, customer_contracts
    if alert["lifecycle"] != "active_local":
        return f"alert is {alert['lifecycle']}"
    if not customer._baseline_state(baseline)[0]:
        return "baseline not complete"
    query = json.loads(alert["query_json"])
    if not isinstance(query, dict) or query.get("version") != customer_contracts.QUERY_VERSION:
        return "stored definition predates Query2"
    if any(not isinstance(m, dict) or not isinstance(m.get("transition"), dict)
           or "source_ref" not in m["transition"]
           for m in (baseline.get("members") or {}).values()):
        return "baseline predates source publications"
    return ""


def carry_alert_baselines(con, names_run_id: str, *, dry_run: bool,
                          run_id: str | None = None, actor: str = "operator") -> dict:
    """Plan (and unless dry_run, apply) the carry of the reads a names run
    wrote. See the module docstring."""
    with _db.transaction(con, write=not dry_run):
        reads = names_run_reads(con, names_run_id)
        return carry_reads(con, reads, dry_run=dry_run, run_id=run_id, actor=actor,
                           origin={"names_run_id": names_run_id})


def carry_reads(con, reads: list, *, dry_run: bool, run_id: str | None = None,
                actor: str = "operator", origin: dict, repoint: bool = False,
                refresh_offer=frozenset(), seen_only=frozenset(),
                prior: dict | None = None, seen_via=None) -> dict:
    """The carry of the evidence ids `reads` into every active alert with a
    complete baseline (the module docstring's rules), in the caller's
    transaction or its own. `origin` names who chose the reads: it is
    merged into the result and the one audit row (names_run_id for a names
    run; {"origin": "import"} for a load, import_sources.seed_all).
    repoint: also re-point members already there (module docstring).
    refresh_offer: evidence ids whose member already there also takes the
    snapshot's offer signature when its stored one is the signature
    prior_offers gave before the load (`prior`, {alert id: {evidence id:
    offer}}; none: no refresh). seen_only: evidence ids (of `reads`)
    carried only into an alert that had seen them (module docstring).
    seen_via: {evidence id of a row the rules added: the evidence id of the
    read it was split from, or None}: such a row is carried only into an
    alert that had seen that read, or (None) whose last successful check
    came after the episode's transcript copy (_episode_seen)."""
    from . import corrections, customer, ops, search
    run_id = run_id or "alerts-" + datetime.datetime.now(
        datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = {"run_id": run_id, **origin, "dry_run": dry_run,
           "named_reads": len(reads), "would_write": 0, "written": 0, "alerts": []}
    with _db.transaction(con, write=not dry_run):
        alerts = [dict(r) for r in con.execute(
            "SELECT * FROM customer_alerts WHERE workspace_id='local' ORDER BY id")]
        for alert in alerts:
            baseline = json.loads(alert["baseline_json"] or "{}")
            entry = {"alert_id": alert["id"], "name": alert["name"], "carried": [],
                     "repointed": [], "repointed_from": {}, "offer_refreshed": [],
                     "skipped": "",
                     "held_for_brand_review": None}
            out["alerts"].append(entry)
            entry["skipped"] = _skip_reason(alert, baseline)
            if entry["skipped"]:
                continue
            live = json.loads(alert.get("live_selector_json") or "null")
            if isinstance(live, dict) and not live:
                live = None
            try:
                current = customer._alert_snapshot(con, json.loads(alert["query_json"]), live,
                                                   json.loads(alert["event_types"]))
            except (ops.BadInput, search.FilterRejected) as exc:
                entry["skipped"] = f"snapshot unavailable: {exc}"
                continue
            entry["held_for_brand_review"] = current.get("held_for_brand_review", 0)
            members = baseline.setdefault("members", {})
            unseen = _unseen(con, alert, baseline, reads, seen_only, seen_via or {})
            was = (prior or {}).get(alert["id"]) or {}
            for eid in reads:
                logical = f"finding:{eid}"
                now, old = current["members"].get(logical), members.get(logical)
                if now is None:
                    continue
                if old is None:
                    if eid in unseen:
                        continue
                    members[logical] = now
                    entry["carried"].append(eid)
                elif isinstance(old, dict):
                    moved = repoint and \
                        (old.get("ref"), old.get("transition")) != (now["ref"], now["transition"])
                    renewed = eid in refresh_offer and eid in was \
                        and old.get("offer") != now.get("offer") and old.get("offer") == was[eid]
                    if moved or renewed:
                        change = {"ref": now["ref"], "transition": now["transition"]} \
                            if moved else {}
                        if renewed:
                            change["offer"] = now.get("offer")
                        members[logical] = dict(old, **change)
                    if moved:
                        entry["repointed"].append(eid)
                        entry["repointed_from"][eid] = old.get("ref")
                    if renewed:
                        entry["offer_refreshed"].append(eid)
            if not entry["carried"] and not entry["repointed"] and not entry["offer_refreshed"]:
                continue
            out["would_write"] += 1
            if not dry_run:
                con.execute("UPDATE customer_alerts SET baseline_json=? WHERE id=?"
                            " AND workspace_id='local'",
                            (json.dumps(baseline, sort_keys=True), alert["id"]))
                out["written"] += 1
        if out["written"]:
            rev = _db.bump_revision(con)
            _db.audit(con, actor, ACTION, "workspace", run_id,
                      {**origin, "named_reads": list(reads), "alerts": out["alerts"]}, rev)
    return out


def _unseen(con, alert: dict, baseline: dict, reads, seen_only, seen_via: dict) -> set:
    """The evidence ids among reads the alert had not seen (seen_only,
    seen_via; carry_reads)."""
    from . import corrections
    via = {e: k for e, k in seen_via.items() if e in reads}
    plain = [e for e in reads if e in seen_only and e not in via]
    if not plain and not via:
        return set()
    seen = set(corrections._seen_reads(con, alert, baseline, list(dict.fromkeys(
        plain + [k for k in via.values() if k]))))
    seen |= _episode_seen(con, alert, [e for e, k in via.items() if k is None])
    return {e for e in plain if e not in seen} | \
        {e for e, k in via.items() if (k not in seen if k else e not in seen)}


def _episode_seen(con, alert: dict, ids: list) -> set:
    """The evidence ids among ids whose episode's pinned transcript copy was
    stored before the alert's last successful check: that check covered the
    words the rules made the row from."""
    last = alert.get("last_successful_at")
    out: set = set()
    if not last:
        return out
    for at in range(0, len(ids), 400):
        chunk = ids[at:at + 400]
        out.update(r[0] for r in con.execute(
            "SELECT e.id FROM evidence e JOIN episodes p ON p.id=e.episode_id"
            " JOIN source_versions v ON v.id=p.transcript_version_id"
            " WHERE e.id IN (%s) AND v.created_at < ?" % ",".join("?" * len(chunk)),
            [*chunk, last]))
    return out


def prior_offers(con, prior_merge: bool) -> dict:
    """{alert id: {evidence id: offer signature}}, read before a load that
    changes what the display list merges (import_sources.seed_all): for each
    active alert with a complete baseline, its finding members that are
    named reads, as the check computes them now under the merge the web had
    (prior_merge: False = nothing curated, True = the curated file;
    corrections._carried_members within customer.curated_merge). Such a load
    changes their attribution through the curated list and through what the
    rules name with it (R9 at the DET flip, DBS-R4-01). An alert whose
    members cannot be computed gets none (nothing is refreshed)."""
    from . import ad_breaks, corrections, customer, ops, search
    canonical = ad_breaks.canonical_map(con)
    out: dict = {}
    for alert in [dict(r) for r in con.execute(
            "SELECT * FROM customer_alerts WHERE workspace_id='local' ORDER BY id")]:
        try:
            baseline = json.loads(alert["baseline_json"] or "{}")
            if not isinstance(baseline, dict) or _skip_reason(alert, baseline):
                continue
            members = baseline.get("members") or {}
            ids = [k[len("finding:"):] for k in members if k.startswith("finding:")]
            wanted = []
            for at in range(0, len(ids), 400):
                chunk = ids[at:at + 400]
                wanted += [r[0] for r in con.execute(
                    "SELECT id FROM evidence WHERE source_record_key LIKE 'named:%%'"
                    " AND id IN (%s)" % ",".join("?" * len(chunk)), chunk)]
            if not wanted:
                continue
            episodes: set = set()
            for at in range(0, len(wanted), 400):
                chunk = wanted[at:at + 400]
                for (ep,) in con.execute("SELECT episode_id FROM evidence WHERE id IN (%s)"
                                         % ",".join("?" * len(chunk)), chunk):
                    episodes.update((ep, canonical.get(ep, ep)))
            with customer.curated_merge(prior_merge):
                got = corrections._carried_members(con, alert, wanted, episodes)
        except (ValueError, TypeError, KeyError, AttributeError, ops.BadInput,
                search.FilterRejected):
            continue
        out[alert["id"]] = {e: got[f"finding:{e}"].get("offer") for e in wanted
                            if f"finding:{e}" in got}
    return out


def format_report(result: dict) -> str:
    dry = result["dry_run"]
    verb = "would carry" if dry else "carried"
    lines = [("DRY RUN (writes nothing): " if dry else "")
             + f"run {result['run_id']}, names run {result['names_run_id']}"
               f" ({result['named_reads']} named reads)",
             "alert\tname\tcarried\theld for brand review\tskipped\treads"]
    for a in result["alerts"]:
        held = "-" if a["held_for_brand_review"] is None else a["held_for_brand_review"]
        lines.append(f"{a['alert_id']}\t{a['name']}\t{len(a['carried'])}\t{held}"
                     f"\t{a['skipped'] or '-'}\t{' '.join(a['carried']) or '-'}")
    total = sum(len(a["carried"]) for a in result["alerts"])
    lines.append(f"{verb} {total} reads into {result['would_write']} alert baselines"
                 + ("" if dry else f"; written {result['written']}"))
    return "\n".join(lines)
