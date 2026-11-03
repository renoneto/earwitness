"""Stored alert events on title-only ads (R3-03 correction).

An alert with words watches only ads matched on their own text (customer.
_alert_snapshot). Events stored before that rule may name an ad matched
only on its show/episode title, and the stored baseline may still count a
title-only read as held for brand review. This lists both and, only with
remove=True, corrects them in one transaction (one audit row; nothing to
correct writes nothing). It is idempotent: a second run finds none.

An event is listed when it is a `new_finding` or `offer_difference` event
of an ads or mentions alert with words, under the alert's current words
(its membership: query and live selector), and its ad matches the alert's
stored definition today only on the title (match_basis "title", owner
view). An edit of the notification types only keeps the words, so events
from before it still count (ATM-R2-01). An event whose ad no longer
matches at all is not judged and is kept (counted as "unmatched"). Events
of older words are kept.

A baseline is listed when it is complete for the alert's current words
and its held counts or result count differ from a fresh match
(the same match Current matches shows); remove rewrites just those
fields, so the Alerts list and the alert page agree with Current matches
without a check. Its members are not changed.

Notifications: a removed event may have been published to the monitor
outbox (monitor_events under "alert:<id>"). They are listed; remove is
refused while one is still pending, queued, uncertain or failed (a failed
send may be retried). Delivered ones are kept as delivery history.

The audit row holds every removed event row and each old baseline field,
so restore(audit_id) puts them back. Restore refuses, writing nothing,
when an alert changed since the removal: its definition revision, its
stored baseline or its last check (ATM-R2-02).

With --layout (a pilot host), --remove and --restore hold the L6 web lock
that pilot_seed and pilot_sql take, and every run requires the layout's
declared workspace path. A busy database is a refusal (exit 2).

    python -m earwitness.app.alert_title_events <workspace dir or .sqlite3>            # dry run
    python -m earwitness.app.alert_title_events <workspace dir or .sqlite3> --remove   # corrects
    python -m earwitness.app.alert_title_events <workspace dir or .sqlite3> --restore <audit id>
    ... [--layout <pilot-layout.json>]
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

from . import db as _db

ACTION = "alert_title_only_events_removed"
RESTORE_ACTION = "alert_title_only_events_restored"
KINDS = ("new_finding", "offer_difference")
BASELINE_KEYS = ("held_for_brand_review", "held_ads_for_brand_review", "held_refs",
                 "result_count")
WAITING = ("pending", "queued", "uncertain", "failed")


def _tables(con: sqlite3.Connection) -> set:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _notifications(con: sqlite3.Connection, alert_id: int, logicals: set) -> list:
    """Monitor events published for these events, and whether one can
    still be delivered."""
    out = []
    for row in con.execute("SELECT id, detail FROM monitor_events WHERE schedule_id=?"
                           " ORDER BY id", (f"alert:{alert_id}",)):
        detail = json.loads(row["detail"] or "{}")
        if detail.get("logical_identity") not in logicals:
            continue
        states = [r[0] for r in con.execute(
            "SELECT state FROM notification_outbox WHERE event_id=?", (row["id"],))]
        out.append({"monitor_event_id": row["id"],
                    "logical_identity": detail.get("logical_identity"),
                    "publication": detail.get("publication"), "outbox": states,
                    "pending": detail.get("publication") == "pending"
                    or any(s in WAITING for s in states)})
    return out


def _words_since(con: sqlite3.Connection, alert: dict) -> int:
    """The first definition revision of the alert's current words.

    save_alert advances definition_revision on a membership (query or
    selector) edit and on an event-types-only edit; only the first changes
    what the alert matches. Walk back over event-types-only saves in the
    audit. A revision with no save row is its own floor (the narrow
    choice: older events are kept)."""
    entered = {}
    for row in con.execute("SELECT detail FROM audit WHERE action='customer_alert_save'"
                           " AND target_type='customer_alert' AND target_id=? ORDER BY id",
                           (str(alert["id"]),)):
        detail = json.loads(row["detail"] or "{}")
        if detail.get("membership_changed") or detail.get("event_changed"):
            entered[detail.get("definition_revision")] = detail
    floor = int(alert["definition_revision"])
    while floor > 1:
        detail = entered.get(floor)
        if not detail or detail.get("membership_changed") is not False \
                or not detail.get("event_changed"):
            break
        floor -= 1
    return floor


def _alert_state(con: sqlite3.Connection, alert_id: int):
    """What a later change to the alert moves: its definition revision, a
    digest of its stored baseline and its run state (every check advances
    resource_revision; timestamps alone can repeat within a second). None
    if it is gone."""
    row = con.execute("SELECT * FROM customer_alerts WHERE id=?", (alert_id,)).fetchone()
    if row is None:
        return None
    return {"definition_revision": row["definition_revision"],
            "baseline_sha256": hashlib.sha256((row["baseline_json"] or "").encode()).hexdigest(),
            "last_attempted_at": row["last_attempted_at"],
            "resource_revision": row["resource_revision"] if "resource_revision" in row.keys()
            else None}


def title_only_events(con: sqlite3.Connection, *, remove: bool = False,
                      actor: str = "operator") -> dict:
    """Plan (and only with remove=True, apply) the correction."""
    from . import citations, customer, customer_contracts, customer_query, ops, search
    out = {"remove": remove, "alerts": [], "listed": 0, "removed": 0, "baselines": 0,
           "pending_notifications": 0}
    with _db.transaction(con, write=remove):
        # ATM-R2-05: earlier removals, so a restore needs no sqlite3 shell.
        restored = {json.loads(r["detail"] or "{}").get("audit_id") for r in con.execute(
            "SELECT detail FROM audit WHERE action=?", (RESTORE_ACTION,))}
        out["removals"] = [{"audit_id": r["id"], "at": r["at"], "restored": r["id"] in restored}
                           for r in con.execute("SELECT id, at FROM audit WHERE action=?"
                                                " ORDER BY id", (ACTION,))]
        tables = _tables(con)
        monitored = {"monitor_events", "notification_outbox"} <= tables
        excerpt = customer.excerpt_workspace(con)
        alerts = [dict(r) for r in con.execute(
            "SELECT * FROM customer_alerts WHERE workspace_id='local' ORDER BY id")]
        for alert in alerts:
            query = json.loads(alert["query_json"] or "{}")
            entry = {"alert_id": alert["id"], "name": alert["name"],
                     "q": query.get("q") if isinstance(query, dict) else None,
                     "finding_events": 0, "unmatched": 0, "events": [], "skipped": "",
                     "baseline": None, "notifications": []}
            out["alerts"].append(entry)
            if not isinstance(query, dict) or query.get("version") != customer_contracts.QUERY_VERSION:
                entry["skipped"] = "stored definition predates Query2"
                continue
            if query.get("mode") not in ("ads", "mentions"):
                entry["skipped"] = f"{query.get('mode')} mode watches no ads"
                continue
            if not any(customer.tokenize_query(query.get("q") or "")):
                entry["skipped"] = "no words"
                continue
            live = json.loads(alert.get("live_selector_json") or "null")
            if isinstance(live, dict) and not live:
                live = None
            baseline = json.loads(alert["baseline_json"] or "{}")
            complete, base_rev = customer._baseline_state(baseline)
            # ATM-R2-01: key off the current words, not the raw revision.
            since = _words_since(con, alert)
            try:
                if complete and since <= base_rev <= alert["definition_revision"] \
                        and (not excerpt or baseline.get("basis") == citations.PA1):
                    # ATM-R1-01: the stored held counts, from the match
                    # Current matches shows.
                    fresh = customer._alert_snapshot(con, query, live,
                                                     json.loads(alert["event_types"]))
                    before = {k: baseline.get(k) for k in BASELINE_KEYS}
                    after = {k: fresh.get(k) for k in BASELINE_KEYS}
                    if before != after:
                        entry["baseline"] = {"before": before, "after": after}
                        out["baselines"] += 1
                events = [dict(r) for r in con.execute(
                    "SELECT * FROM customer_alert_events WHERE alert_id=?"
                    " AND definition_revision BETWEEN ? AND ?"
                    f" AND kind IN ({','.join('?' * len(KINDS))})"
                    " AND logical_identity LIKE 'finding:%' ORDER BY id",
                    (alert["id"], since, alert["definition_revision"], *KINDS))]
                entry["finding_events"] = len(events)
                if not events:
                    continue
                effective, clause, units = customer_query.tracker_effective(query, live)
                rows, _info = customer_query.match_effective(
                    con, dict(effective, mode="ads"), clause, units, light=True)
            except (ops.BadInput, search.FilterRejected) as exc:
                entry["skipped"] = f"match unavailable: {exc}"
                if entry["baseline"]:
                    out["baselines"] -= 1
                    entry["baseline"] = None
                continue
            basis = {r["evidence_id"]: r.get("match_basis") for r in rows
                     if r.get("record_kind") == "evidence"}
            for event in events:
                evidence_id = event["logical_identity"][len("finding:"):]
                if evidence_id not in basis:
                    entry["unmatched"] += 1
                elif basis[evidence_id] == "title":
                    entry["events"].append({"event_id": event["id"], "kind": event["kind"],
                                            "evidence_id": evidence_id,
                                            "historical": bool(event["historical"]),
                                            "row": event})
            out["listed"] += len(entry["events"])
            if monitored and entry["events"]:
                entry["notifications"] = _notifications(
                    con, alert["id"], {e["row"]["logical_identity"] for e in entry["events"]})
                out["pending_notifications"] += sum(n["pending"] for n in entry["notifications"])
        if not remove or not (out["listed"] or out["baselines"]):
            return out
        if out["pending_notifications"]:
            raise ops.BadInput(f"refused: {out['pending_notifications']} notification(s) of"
                               " listed events can still be delivered; nothing was removed")
        ids = [(e["event_id"],) for a in out["alerts"] for e in a["events"]]
        con.executemany("DELETE FROM customer_alert_events WHERE id=?", ids)
        out["removed"] = len(ids)
        for a in out["alerts"]:
            if a["baseline"]:
                _write_baseline(con, a["alert_id"], a["baseline"]["after"])
        rev = _db.bump_revision(con)
        _db.audit(con, actor, ACTION, "workspace", "local",
                  {"alerts": [{"alert_id": a["alert_id"],
                               "event_ids": [e["event_id"] for e in a["events"]],
                               "events": [e["row"] for e in a["events"]],
                               "baseline_before": (a["baseline"] or {}).get("before"),
                               "baseline_after": (a["baseline"] or {}).get("after"),
                               # ATM-R2-02: restore only onto this state.
                               "state_after": _alert_state(con, a["alert_id"])}
                              for a in out["alerts"] if a["events"] or a["baseline"]]}, rev)
    return out


def _write_baseline(con: sqlite3.Connection, alert_id: int, fields: dict) -> None:
    row = con.execute("SELECT baseline_json FROM customer_alerts WHERE id=?",
                      (alert_id,)).fetchone()
    baseline = json.loads(row["baseline_json"] or "{}")
    for key, value in fields.items():
        if value is None:
            baseline.pop(key, None)
        else:
            baseline[key] = value
    con.execute("UPDATE customer_alerts SET baseline_json=? WHERE id=?",
                (json.dumps(baseline, sort_keys=True), alert_id))


def restore(con: sqlite3.Connection, audit_id: int, *, actor: str = "operator") -> dict:
    """Put back what one removal (its audit row) took out: the event rows
    with their ids, and each baseline's old fields. Once only."""
    from . import ops
    with _db.transaction(con, write=True):
        row = con.execute("SELECT detail FROM audit WHERE id=? AND action=?",
                          (audit_id, ACTION)).fetchone()
        if row is None:
            raise ops.BadInput(f"audit row {audit_id} is not a title-only removal")
        if con.execute("SELECT 1 FROM audit WHERE action=? AND"
                       " json_extract(detail, '$.audit_id')=?",
                       (RESTORE_ACTION, audit_id)).fetchone():
            raise ops.BadInput(f"audit row {audit_id} was already restored")
        detail = json.loads(row["detail"])
        cols = {r["name"] for r in con.execute("PRAGMA table_info(customer_alert_events)")}
        for alert in detail.get("alerts", []):
            if "events" not in alert or "state_after" not in alert:
                raise ops.BadInput(f"audit row {audit_id} predates guarded restores;"
                                   " nothing restored")
            # ATM-R2-02: a check, a redefinition or a deletion since the
            # removal makes the recorded rows wrong for the alert today.
            if _alert_state(con, alert["alert_id"]) != alert["state_after"]:
                raise ops.BadInput(f"alert {alert['alert_id']} changed since the removal;"
                                   " nothing restored")
        restored = baselines = 0
        for alert in detail.get("alerts", []):
            for event in alert["events"]:
                keys = [k for k in event if k in cols]
                try:
                    con.execute(f"INSERT INTO customer_alert_events ({', '.join(keys)})"
                                f" VALUES ({', '.join('?' * len(keys))})",
                                [event[k] for k in keys])
                except sqlite3.IntegrityError as exc:
                    raise ops.BadInput(f"event {event.get('id')} cannot be restored: {exc}")
                restored += 1
            if alert.get("baseline_before"):
                _write_baseline(con, alert["alert_id"], alert["baseline_before"])
                baselines += 1
        rev = _db.bump_revision(con)
        _db.audit(con, actor, RESTORE_ACTION, "workspace", "local",
                  {"audit_id": audit_id, "events": restored, "baselines": baselines}, rev)
    return {"restored": restored, "baselines": baselines}


def format_report(result: dict) -> str:
    lines = [("" if result["remove"] else "DRY RUN (writes nothing): ")
             + "ad events whose ad matches the alert only on its title, and stale baselines",
             "alert\tname\tq\tad events\ttitle-only\tunmatched (kept)\tskipped"]
    for a in result["alerts"]:
        lines.append(f"{a['alert_id']}\t{a['name']}\t{a['q'] or '-'}\t{a['finding_events']}"
                     f"\t{len(a['events'])}\t{a['unmatched']}\t{a['skipped'] or '-'}")
        for e in a["events"]:
            lines.append(f"\tevent {e['event_id']}\t{e['kind']}\t{e['evidence_id']}"
                         + ("\t(past)" if e["historical"] else ""))
        if a["baseline"]:
            b, f = a["baseline"]["before"], a["baseline"]["after"]
            lines.append(f"\tbaseline: held reads {b['held_for_brand_review']} -> "
                         f"{f['held_for_brand_review']}, held ads {b['held_ads_for_brand_review']}"
                         f" -> {f['held_ads_for_brand_review']}, result count"
                         f" {b['result_count']} -> {f['result_count']}")
        for n in a["notifications"]:
            lines.append(f"\tnotification: monitor event {n['monitor_event_id']}\t"
                         f"{n['logical_identity']}\tpublication {n['publication']}\toutbox "
                         f"{','.join(n['outbox']) or '-'}\t"
                         + ("can still be delivered (blocks --remove)" if n["pending"] else "kept"))
    for r in result.get("removals", []):
        lines.append(f"earlier removal: audit {r['audit_id']} at {r['at']}: "
                     + ("restored" if r["restored"] else f"undo with --restore {r['audit_id']}"))
    lines.append(f"title-only events: {result['listed']}; stale baselines: {result['baselines']}"
                 f"; pending notifications: {result['pending_notifications']}"
                 + (f"; removed {result['removed']}" if result["remove"]
                    else "; removed 0 (dry run)"))
    return "\n".join(lines)


def main(argv=None) -> int:
    from . import ops
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("db", help="workspace directory or its .sqlite3 file (stop its server first)")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--remove", action="store_true",
                        help="delete the listed events and rewrite the listed baselines")
    action.add_argument("--restore", type=int, metavar="AUDIT_ID",
                        help="put back what one removal took out")
    parser.add_argument("--layout", metavar="PILOT_LAYOUT",
                        help="pilot host: hold the web lock for --remove/--restore and"
                             " require the layout's declared workspace path")
    args = parser.parse_args(argv)
    path = Path(args.db)
    target = path / "workspace.sqlite3" if path.is_dir() else path
    write = args.remove or args.restore is not None
    with contextlib.ExitStack() as held:
        if args.layout:
            # ATM-R2-04: the L6 web lock pilot_seed and pilot_sql take.
            from scripts import pilot_inventory as inv
            from scripts import pilot_sql as psql
            try:
                layout = inv.load_layout(args.layout)
                target = Path(psql.declared_target(layout, str(target.absolute()), "workspace"))
                if write:
                    held.enter_context(psql.web_lock(layout))
            except (inv.UsageError, psql.Refused) as exc:
                print(f"error: {exc}; nothing was written", file=sys.stderr)
                return 2
        try:
            # ATM-R1-02: opens an existing current-schema database only;
            # the dry run read-only.
            con = _db.open_existing(target, readonly=not write)
        except (FileNotFoundError, RuntimeError) as exc:
            parser.error(str(exc))
        except sqlite3.OperationalError as exc:
            return _db_error(exc)
        try:
            if args.restore is not None:
                out = restore(con, args.restore)
                print(f"restored {out['restored']} events and {out['baselines']} baselines")
            else:
                print(format_report(title_only_events(con, remove=args.remove)))
        except ops.BadInput as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except sqlite3.OperationalError as exc:
            return _db_error(exc)
        finally:
            con.close()
    return 0


def _db_error(exc: sqlite3.OperationalError) -> int:
    """A refusal, never a traceback; the transaction rolled back."""
    busy = any(w in str(exc).lower() for w in ("locked", "busy"))
    print("error: database is busy: stop the service first; nothing was written" if busy
          else f"error: database error: {exc}; nothing was written", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
