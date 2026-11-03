#!/usr/bin/env python3
"""WIN-2 #1 host import of a checked Muse Spark brief-check bundle.

  run    --layout L --workspace-id W --db-identity I --bundle B --sha256 S --req REQ
         --mode rollback|commit --out R
  remove --layout L --workspace-id W --db-identity I --run-id ID --req REQ --out R

No model call, no key, no ledger: this script imports no model client. A
row's model must be brief_checks.MODEL_ID (meta_client.MODEL, pinned by a
test), with an API date suffix at most; MODEL_INVALID otherwise. The
bundle (format earwitness-brief-checks/1, made on the laptop by
ws-meta/model-test/brief_check_run.py) carries checked fields only.

run re-runs every code check (brief_checks.recheck) against THIS database's
transcripts, brief and pins; a row that does not hold is dropped with its
reason, never adjusted: every column the database derives for the ad
(brief_checks.derived_columns) must equal the row's. The bundle file's
SHA-256 must equal --sha256. As scripts/pilot_seed.py (whose helpers it
uses): the web-unit lock is held for the run (SERVICE_RUNNING otherwise),
the target is W's database in the layout's data_dir with its registry row
(W, I) and meta.workspace_id = I, no unresolved journal, and the schema
digest equal to the layout's workspace pin before and after. It runs as one
transaction under a SQLite authorizer that allows writes to
model_brief_checks only and refuses DDL. model_brief_checks must exist first
(brief_checks.ensure_brief_check_schema, run once in the STOP window).
Rerunning a bundle stores nothing new (unique run, placement, evidence);
another bundle on the same request is refused (REQUEST_REUSED).
remove deletes one run's rows in one transaction.

Run with the web unit stopped. Exit 0 done (rolled back, committed or
removed); 2 refused or failed; 3 usage error.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from earwitness.app import brief_checks as _bc  # noqa: E402
from earwitness.app import health as _health  # noqa: E402
from scripts import pilot_import as pimp  # noqa: E402
from scripts import pilot_inventory as inv  # noqa: E402
from scripts import pilot_sql as psql  # noqa: E402

TOOL = "brief_check_import"
EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 2, 3
_DDL_ACTIONS = frozenset(
    getattr(sqlite3, name) for name in dir(sqlite3)
    if name.startswith(("SQLITE_CREATE_", "SQLITE_DROP_")) and isinstance(getattr(sqlite3, name), int)
) | {sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}
_WRITE_ACTIONS = frozenset({sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE})
# AUTOINCREMENT keeps its counter in sqlite_sequence.
_WRITABLE = frozenset({_bc.TABLE, "sqlite_sequence"})


class Refused(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def authorizer(denied: list):
    """Writes to model_brief_checks only; no DDL, no ATTACH."""
    def check(action, arg1, arg2, dbname, trigger):
        if action in _DDL_ACTIONS or (action in _WRITE_ACTIONS and arg1 not in _WRITABLE):
            denied.append(arg1)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    return check


def _open(layout, workspace_id, identity) -> sqlite3.Connection:
    """W's database as pilot_seed declares it (pilot_sql / pilot_import helpers)."""
    try:
        target = psql.declared_target(layout, os.path.join(layout["data_dir"],
                                                           workspace_id + ".sqlite3"), "workspace")
    except psql.Refused as exc:
        raise Refused("TARGET_INVALID", str(exc)) from None
    reg = pimp._registry_row(layout, workspace_id)
    if reg is None or reg[1] != identity or os.path.realpath(reg[0]) != os.path.realpath(target):
        raise Refused("TARGET_MISMATCH", "no registry row (W, I) for this file")
    clean, state = psql._sidecars_clean(target)
    if not clean:
        raise Refused("JOURNAL_UNRESOLVED", f"{state}; run `pilot_sql.py check`")
    con = sqlite3.connect(target, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT value FROM meta WHERE key='workspace_id'").fetchone()
        if row is None or row[0] != identity:
            raise Refused("TARGET_MISMATCH", "meta.workspace_id differs from --db-identity")
        problem = psql.schema_pin_problem(con, "workspace", layout)
        if problem:
            raise Refused("SCHEMA_UNPINNED", problem)
        if not _bc.table_exists(con):
            raise Refused("TABLE_MISSING", "run brief_checks.ensure_brief_check_schema first")
    except BaseException:
        con.close()
        raise
    return con


def _locked(layout, work):
    """work() with the web-unit lock held (pilot_seed's STOP window)."""
    try:
        lock = _health.acquire_single_process_lock(layout["control_db"])
    except _health.ConfigError:
        raise Refused("SERVICE_RUNNING", "a web process holds the control store") from None
    try:
        return work()
    except sqlite3.DatabaseError as exc:
        raise Refused("TARGET_INVALID", type(exc).__name__) from None
    finally:
        lock.close()


def _transaction(con, layout, work, mode):
    denied = []
    con.execute("BEGIN IMMEDIATE")
    con.set_authorizer(authorizer(denied))
    try:
        try:
            out = work()
        except sqlite3.DatabaseError as exc:
            if denied:
                raise Refused("WRITE_REFUSED", f"refused writes: {sorted(set(map(str, denied)))}") \
                    from None
            raise Refused("SQLITE_ERROR", type(exc).__name__) from None
        if psql.schema_pin_problem(con, "workspace", layout):
            raise Refused("SCHEMA_CHANGED", "the schema digest changed during the run")
    except BaseException:
        con.set_authorizer(None)
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    con.set_authorizer(None)
    con.execute("COMMIT" if mode == "commit" else "ROLLBACK")
    return out


def _load_bundle(path: str, sha256: str) -> dict:
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise Refused("BUNDLE_INVALID", "the bundle is not readable") from None
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise Refused("BUNDLE_SHA256", "the bundle file's SHA-256 differs from --sha256")
    try:
        doc = json.loads(raw)
    except (ValueError, RecursionError):
        raise Refused("BUNDLE_INVALID", "the bundle is not JSON") from None
    if not _bc.bundle_digest_ok(doc):
        raise Refused("BUNDLE_DIGEST", "the bundle's format or bundle_sha256 does not hold")
    rows = doc["rows"]
    if not isinstance(rows, list) or not all(
            _bc.row_shape_ok(r) and r["run_id"] == doc["run_id"] for r in rows):
        raise Refused("BUNDLE_INVALID", "a row is not a bundle row of this run")
    return doc


def run(layout, workspace_id, identity, bundle_path, sha256, req, mode, out) -> int:
    result = {"tool": TOOL, "mode": mode, "request_id": req, "workspace_id": workspace_id,
              "db_identity": identity, "bundle": str(bundle_path), "sha256": sha256,
              "started_at": _now(), "outcome": "refused"}
    try:
        doc = _load_bundle(bundle_path, sha256)
        result.update(run_id=doc["run_id"], bundle_sha256=doc["bundle_sha256"],
                      code_sha=doc["code_sha"])
        kept, dropped, inserted = _locked(layout, lambda: _run(layout, workspace_id, identity,
                                                                doc, req, mode))
    except Refused as exc:
        result.update(outcome="refused", code=exc.code, error=str(exc), finished_at=_now())
        inv.atomic_write_json(out, result)
        return EXIT_FAILED
    result.update(outcome="committed" if mode == "commit" else "rolled_back", kept=len(kept),
                  dropped=dropped, inserted=inserted, already=len(kept) - inserted,
                  finished_at=_now())
    inv.atomic_write_json(out, result)
    return EXIT_OK


def _run(layout, workspace_id, identity, doc, req, mode):
    con = _open(layout, workspace_id, identity)
    try:
        other = con.execute(f"SELECT 1 FROM {_bc.TABLE} WHERE import_req=? AND run_id!=?"
                            " LIMIT 1", (req, doc["run_id"])).fetchone()
        if other:
            raise Refused("REQUEST_REUSED", "this request imported another run")

        def work():
            kept, dropped, inserted = [], [], 0
            calls = {}
            # Each placement's own campaign, as this database has it: a row's
            # campaign_id is compared (derived_columns), never trusted.
            pids = sorted({r["placement_id"] for r in doc["rows"]})
            cids = {r[0] for r in con.execute(
                "SELECT campaign_id FROM expected_placements WHERE id IN (%s)"
                % ",".join("?" * len(pids)), pids)} if pids else set()
            for cid in sorted(cids):
                for c in _bc.prepare_calls(con, cid, model=None):
                    calls[(c["ad"]["placement_id"], tuple(c["ad"]["evidence_ids"]))] = c
            at = _now()
            for row in doc["rows"]:
                key = (row["placement_id"], tuple(r["evidence_id"] for r in row["reads"]))
                call = calls.get(key)
                reason = "MODEL_INVALID" if not _bc.model_ok(row["model"], _bc.MODEL_ID) else \
                    "AD_NOT_FOUND" if call is None else _bc.recheck(call, row)
                if reason:
                    dropped.append({"placement_id": row["placement_id"],
                                    "evidence_id": row["evidence_id"], "reason": reason})
                    continue
                kept.append(row)
                inserted += _bc.insert_row(con, row, req=req, imported_at=at)
            return kept, dropped, inserted
        return _transaction(con, layout, work, mode)
    finally:
        con.close()


def remove(layout, workspace_id, identity, run_id, req, out) -> int:
    result = {"tool": TOOL, "mode": "remove", "request_id": req, "workspace_id": workspace_id,
              "db_identity": identity, "run_id": run_id, "started_at": _now(),
              "outcome": "refused"}

    def work():
        con = _open(layout, workspace_id, identity)
        try:
            return _transaction(con, layout, lambda: con.execute(
                f"DELETE FROM {_bc.TABLE} WHERE run_id=?", (run_id,)).rowcount, "commit")
        finally:
            con.close()
    try:
        removed = _locked(layout, work)
    except Refused as exc:
        result.update(code=exc.code, error=str(exc), finished_at=_now())
        inv.atomic_write_json(out, result)
        return EXIT_FAILED
    result.update(outcome="removed", removed=removed, finished_at=_now())
    inv.atomic_write_json(out, result)
    return EXIT_OK


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise inv.UsageError(message)


def main(argv=None) -> int:
    parser = _Parser(prog="brief_check_import.py")
    sub = parser.add_subparsers(dest="command", required=True)
    target = ("--layout", "--workspace-id", "--db-identity", "--req", "--out")
    p = sub.add_parser("run")
    for flag in target + ("--bundle", "--sha256"):
        p.add_argument(flag, required=True)
    p.add_argument("--mode", required=True, choices=("rollback", "commit"))
    q = sub.add_parser("remove")
    for flag in target + ("--run-id",):
        q.add_argument(flag, required=True)
    try:
        args = parser.parse_args(argv)
        if not inv.REQ_RE.match(args.req):
            raise inv.UsageError("--req must be REQ-<yyyymmdd>-<n>")
        if not inv._connections._workspace_id_pattern().fullmatch(args.workspace_id):
            raise inv.UsageError("invalid workspace id")
        layout = inv.load_layout(args.layout)
        out = inv._out_path(layout, args.out)
        if args.command == "run":
            return run(layout, args.workspace_id, args.db_identity, args.bundle, args.sha256,
                       args.req, args.mode, out)
        return remove(layout, args.workspace_id, args.db_identity, args.run_id, args.req, out)
    except inv.UsageError as exc:
        print(f"usage error: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
