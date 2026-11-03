#!/usr/bin/env python3
"""Ask operator commands (Meta Ask design 7.3; brief T58 from T1).

reopen-lifetime --total-cap <usd> --note <text> [--budget-dir D] [--grants-db G] [--out F]
    Reopens an Ask ledger whose lifetime cap was marked reached (a
    `recover`, or a turn that exceeded its bound), with a new lifetime
    figure that Reno decided. Runs in a STOP window only: it takes the web
    lock beside the control store (EARWITNESS_GRANTS_DB) and is refused
    while a web process holds it. It refuses a ledger in recovery and a
    figure at or below the lifetime spend so far: settled plus open
    amounts in the current ledger and in every ledger `recover` moved
    aside (ask.sqlite3.replaced-*, read only). In one ledger transaction
    under the ledger's writer lock it closes every lifetime mark and
    records, as a `total_cap` row (amount 0; the note beside it), what the
    current ledger may still spend: the new figure less the moved-aside
    spend. The web app reads that through ask.open_ledger. Day and
    month marks are left as they are. The ledger folder is
    EARWITNESS_META_BUDGET_DIR (default /srv/earwitness-vol/meta-budget).

Output is one JSON document on stdout (and in --out). Exit 0 done, 2 refused.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import sqlite3  # noqa: E402

from earwitness.app import ask as _ask  # noqa: E402
from earwitness.app import health as _health  # noqa: E402
from earwitness.app import meta_client as _meta  # noqa: E402
from earwitness.pipeline import jev_budget as _jb  # noqa: E402

COMMANDS = ("reopen-lifetime",)
MAX_CAP_USD = Decimal("10000")


class Refused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _cap_nusd(text: str) -> int:
    try:
        value = Decimal(str(text).strip())
    except InvalidOperation:
        raise Refused("INVALID_INPUT", "--total-cap must be a number of US dollars") from None
    if not value.is_finite() or value <= 0 or value > MAX_CAP_USD:
        raise Refused("INVALID_INPUT", "--total-cap must be above 0 and at most 10000")
    return int(value * Decimal(1_000_000_000))


def _prior_spend(folder: Path) -> int:
    """n$ counted in the ledgers `recover` moved aside (their lifetimes)."""
    total = 0
    for copy in sorted(folder.glob(_meta.AskLedger.LEDGER + ".replaced-*")):
        try:
            con = sqlite3.connect(copy.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                total += _jb.JevBudget._lifetime(con)["nusd"]
            finally:
                con.close()
        except sqlite3.Error:
            raise Refused("PRIOR_UNREADABLE",
                          f"a moved-aside ledger cannot be read: {copy.name}") from None
    return total


def reopen_lifetime(args) -> dict:
    note = " ".join((args.note or "").split())
    if not note:
        raise Refused("INVALID_INPUT", "--note must say who decided the new figure")
    cap = _cap_nusd(args.total_cap)
    grants = args.grants_db or os.environ.get("EARWITNESS_GRANTS_DB", "")
    if not grants:
        raise Refused("INVALID_INPUT", "--grants-db or EARWITNESS_GRANTS_DB is required")
    if not Path(grants).is_file():
        # The web lock is only meaningful beside the real control store: a
        # wrong path would take a lock no web process holds (review r1 #3).
        raise Refused("NO_CONTROL_STORE", "the control store named by --grants-db does not exist")
    folder = Path(args.budget_dir or _meta.budget_dir())
    if not (folder / _meta.AskLedger.LEDGER).is_file():
        raise Refused("NO_LEDGER", "no Ask ledger in the budget folder")
    try:
        lock = _health.acquire_single_process_lock(grants)
    except _health.ConfigError:
        raise Refused("SERVICE_RUNNING",
                      "a web process holds the control store; run in a STOP window") from None
    try:
        budget = _ask.open_ledger(folder).budget
        status = budget.status()
        if status.get("mode") != "normal":
            raise Refused("RECOVERY", "the ledger is in recovery; recover it first")
        prior = _prior_spend(folder)
        out = {}

        def decide(con, day, month):
            spent = prior + budget._lifetime(con)["nusd"]
            if cap <= spent:
                raise Refused("BELOW_SPEND", "the new figure must be above the lifetime spend"
                              f" so far (${spent / 1e9:.2f})")
            marks = con.execute(
                "SELECT reservation_id, la_day, la_month FROM entries WHERE seq IN"
                " (SELECT MAX(seq) FROM entries GROUP BY reservation_id)"
                " AND state='exhausted' AND scope='total'").fetchall()
            rows = [{"reservation_id": rid, "purpose": "total", "max_nusd": 0,
                     "state": "settled", "actual_nusd": 0, "la_day": d, "la_month": m,
                     "scope": "total", "note": "reopen-lifetime"} for rid, d, m in marks]
            rows.append({"reservation_id": uuid.uuid4().hex, "purpose": _ask.TOTAL_CAP_PURPOSE,
                         "max_nusd": cap - prior, "state": "settled", "actual_nusd": 0,
                         "la_day": day, "la_month": month, "scope": "total",
                         "note": f"reopen-lifetime: total {cap} n$, prior {prior} n$: {note}"[:500]})
            out.update(spent=spent, marks=len(marks))
            return rows
        budget._commit_rows(decide)
    finally:
        lock.close()
    return {"ok": True, "command": "reopen-lifetime", "total_cap_usd": cap / 1e9,
            "spent_usd": out["spent"] / 1e9, "prior_spent_usd": prior / 1e9,
            "ledger_cap_usd": (cap - prior) / 1e9, "marks_closed": out["marks"],
            "writes": [_meta.AskLedger.LEDGER, _meta.AskLedger.PIN]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="app_ask.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("reopen-lifetime")
    p.add_argument("--total-cap", required=True)
    p.add_argument("--note", required=True)
    p.add_argument("--budget-dir")
    p.add_argument("--grants-db")
    p.add_argument("--out")
    args = parser.parse_args(argv)
    try:
        result = reopen_lifetime(args)
        code = 0
    except Refused as exc:
        result = {"ok": False, "command": args.command, "code": exc.code, "message": str(exc),
                  "writes": []}
        code = 2
    text = json.dumps(result, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print(text)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
