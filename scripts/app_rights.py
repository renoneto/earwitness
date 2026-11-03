#!/usr/bin/env python3
"""Pilot rights records (hosted runtime design 5.2, task B2).

Three commands, and no other: nothing narrows, edits or deletes a record
or an addendum.

approve --file <approval.json> --sig <approval.json.sig> --policy <policy.json>
        --layout <pilot-layout.json> [--out <result.json>]
    Admits one signed rights record for one registered workspace. Runs in
    a STOP window only: it takes the web lock beside the control store and
    is refused while a web process holds it. It verifies the SSH signature
    against `allowed_signers` in the protected bundle (namespace
    earwitness-rights-admission), checks the exact fields, their formats,
    one record per workspace and the dates, writes the row (creating the
    table on first use), copies the file and signature into the protected
    bundle, and checks that the row equals the file before it commits.

approve-addendum --file <addendum.json> --sig <addendum.json.sig> --layout <pilot-layout.json>
        [--out <result.json>]
    Admits one signed model-processing addendum (Ask design 8.1) for one
    registered workspace with a rights record. Same STOP window and web
    lock as approve. It verifies the signature in the namespace
    earwitness-model-addendum, the exact fields and formats (only
    [model_read, metadata|excerpt|transcript]; [model_read, audio] is a
    format error), the workspace's record (present, untampered, equal to
    its signed file, and the record the addendum names), coverage of every
    text category the record lets members view, and one addendum per
    workspace; writes the row into pilot_model_addenda, copies the file and
    signature beside the record's in the protected bundle, and checks that
    the row equals the file before it commits.

check-permissions --layout <pilot-layout.json> --workspace-id W --set <permission_set.json>
    Read-only. Checks that W's rights row equals its signed approval file
    in the mounted protected bundle, then checks a proposed source
    permission set (a JSON array of source_content_policy rows) against
    W's record and W's current keys. Where the set holds rows for
    named-reads-ad-rules (the import-side ad rules' rows of the named
    reads), each must equal named-reads-output's row of the same category
    (AD_RULES_PERMISSION_DIFFERS otherwise).

Output is one JSON document on stdout (and in --out). Exit 0 accepted,
2 refused.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from earwitness.app import health as _health  # noqa: E402
from earwitness.app import import_sources as _import  # noqa: E402
from earwitness.app import rights_admission as _rights  # noqa: E402

COMMANDS = ("approve", "approve-addendum", "check-permissions")
WRITES = ["pilot_rights_admissions"]
ADDENDUM_WRITES = ["pilot_model_addenda"]


def _load_json(path: str, what: str):
    try:
        with open(path, "rb") as handle:
            return json.loads(handle.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise _rights.RightsRefused("INVALID_INPUT", f"{what} is not readable JSON") from None


def _layout(path: str) -> dict:
    layout = _load_json(path, "layout")
    if not isinstance(layout, dict) or not all(
            isinstance(layout.get(k), str) and layout.get(k)
            for k in ("control_db", "protected_dir", "pilot_end")):
        raise _rights.RightsRefused(
            "INVALID_INPUT", "layout needs control_db, protected_dir and pilot_end")
    deployed = os.environ.get("EARWITNESS_PILOT_LAYOUT")
    if deployed and not _rights._same_layout(layout, deployed):
        # The host's installed layout names the real control store and
        # signer list; any other layout is refused.
        raise _rights.RightsRefused("LAYOUT_MISMATCH", "layout differs from EARWITNESS_PILOT_LAYOUT")
    return layout


def _policy_refused(problems) -> _rights.RightsRefused:
    error = _rights.RightsRefused("POLICY_INVALID", "; ".join(problems))
    error.problems = [("POLICY_INVALID", p) for p in problems]
    return error


def _policy_d(policy_path: str, layout_path: str, layout: dict) -> int:
    """d from the approved L5p policy, after its date rules pass. Uses
    `pilot_inventory.py policy-check` when that script exists (L5p T1);
    otherwise the same rules run here. The refusal carries the problems."""
    policy = _load_json(policy_path, "policy")
    inventory = REPO / "scripts" / "pilot_inventory.py"
    if inventory.is_file():
        done = subprocess.run(
            [sys.executable, str(inventory), "policy-check", "--policy",
             policy_path, "--layout", layout_path],
            capture_output=True, text=True, timeout=120)
        if done.returncode != 0:
            try:
                problems = list(json.loads(done.stdout)["problems"])
            except (ValueError, KeyError, TypeError):
                # Exit 3 (bad layout or policy) reports on stderr.
                problems = [line for line in done.stderr.splitlines() if line.strip()][-1:] \
                    or [f"policy-check exited {done.returncode}"]
            raise _policy_refused([str(p) for p in problems])
    else:
        problems = _rights.policy_problems(policy, layout)
        if problems:
            raise _policy_refused(problems)
    d = policy.get("d") if isinstance(policy, dict) else None
    if isinstance(d, bool) or not isinstance(d, int) or d < 1:
        raise _rights.RightsRefused("POLICY_INVALID", "policy d must be a positive integer")
    return d


def _fsync_dir(folder: str) -> None:
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _install(target: str, data: bytes) -> None:
    """Write a bundle file durably; an existing file must hold the same
    bytes (bundle files are kept, never replaced)."""
    if os.path.exists(target):
        with open(target, "rb") as handle:
            if handle.read() != data:
                raise _rights.RightsRefused(
                    "BUNDLE_CONFLICT", "a different file is already kept at this name")
        return
    folder = os.path.dirname(target)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    temp = target + ".partial"
    with open(temp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, target)
    _fsync_dir(folder)
    with open(target, "rb") as handle:
        if _rights.sha256(handle.read()) != _rights.sha256(data):
            raise _rights.RightsRefused("BUNDLE_CONFLICT", "bundle copy does not match")


# Seat states that no longer hold a sale (purchases.TRANSITIONS: ended
# and retired are the end of every path).
_RELEASED_SEAT_STATES = ("ended", "retired")


def _require_unseated(con, workspace_id: str) -> None:
    """RB1-R3-02: an evaluation-only record never lands on a workspace a
    purchase seat holds in any state but ended or retired (reserved to
    active, awaiting_account and needs_refund are a sale in progress or
    not yet refunded). No purchase_seats table means no seats; any other
    read failure refuses (PAID_STATE_UNREADABLE)."""
    from earwitness.app import purchases as _purchases
    try:
        seat = _purchases.seat_for_workspace(con, workspace_id)
    except sqlite3.Error:
        raise _rights.RightsRefused(
            "PAID_STATE_UNREADABLE", "purchase seats could not be read") from None
    if seat is not None and seat["state"] not in _RELEASED_SEAT_STATES:
        raise _rights.RightsRefused(
            "WORKSPACE_SEATED", f"a purchase seat ({seat['state']}) holds the workspace;"
            " end and refund it first")


def approve(args) -> dict:
    layout = _layout(args.layout)
    control_db = layout["control_db"]
    protected = layout["protected_dir"]
    try:
        lock = _health.acquire_single_process_lock(control_db)
    except _health.ConfigError:
        raise _rights.RightsRefused(
            "SERVICE_RUNNING", "a web process holds the control store; run in a STOP window") from None
    try:
        principal = _rights.verify_signature(
            args.file, args.sig, _rights.allowed_signers_path(protected))
        with open(args.file, "rb") as handle:
            raw = handle.read()
        with open(args.sig, "rb") as handle:
            sig = handle.read()
        # The signature covers these exact bytes; re-check them after the
        # read so a file swapped between verify and read is refused.
        record = _rights.build_record(raw, principal)
        principal_again = _rights.verify_signature(
            args.file, args.sig, _rights.allowed_signers_path(protected))
        with open(args.file, "rb") as handle:
            if handle.read() != raw or principal_again != principal:
                raise _rights.RightsRefused("SIGNATURE_INVALID", "file changed during approval")
        d = _policy_d(args.policy, args.layout, layout)
        if record["deletion_days"] != d:
            raise _rights.RightsRefused(
                "DELETION_DAYS_MISMATCH", "deletion_days must equal the approved policy's d")
        for deployed in (layout["pilot_end"], os.environ.get("EARWITNESS_PILOT_END")):
            if deployed is None:
                continue
            problem = _rights.date_problem(record, deployed, d)
            if problem:
                raise _rights.RightsRefused(problem)
        if not os.path.isfile(control_db):
            raise _rights.RightsRefused("INVALID_INPUT", "control store does not exist")
        con = sqlite3.connect("file:" + quote(os.path.abspath(control_db), safe="/") + "?mode=rw",
                              uri=True, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute(_rights.DDL)
                if not _rights.table_current(con):
                    raise _rights.RightsRefused(
                        "SCHEMA_OUTDATED", "the control store's rights table predates this"
                        " build (basis CHECK); it is not migrated in place")
                identity = record["workspace_identity"]
                if con.execute("SELECT 1 FROM workspace_registry WHERE db_identity=?",
                               (identity,)).fetchone() is None:
                    raise _rights.RightsRefused(
                        "WORKSPACE_NOT_REGISTERED", "no registered workspace has this identity")
                if con.execute("SELECT 1 FROM pilot_rights_admissions WHERE workspace_identity=?",
                               (identity,)).fetchone() is not None:
                    raise _rights.RightsRefused("WORKSPACE_HAS_RECORD")
                if not _rights.basis_allows_paid_use(record):
                    # RB-R1-01: an evaluation-only record never lands on a
                    # workspace that already holds a paid subscription.
                    workspace_id, db_path = con.execute(
                        "SELECT workspace_id, db_path FROM workspace_registry"
                        " WHERE db_identity=?", (identity,)).fetchone()
                    problem = _rights.workspace_paid_problem(db_path)
                    if problem:
                        raise _rights.RightsRefused(
                            problem, "the workspace holds a paid subscription, or its"
                            " billing state could not be read; end and refund it first")
                    _require_unseated(con, workspace_id)
                if con.execute("SELECT 1 FROM pilot_rights_admissions WHERE admission_id=?",
                               (record["admission_id"],)).fetchone() is not None:
                    raise _rights.RightsRefused("ADMISSION_ID_TAKEN")
                row = _rights.to_row(record)
                con.execute(
                    "INSERT INTO pilot_rights_admissions (" + ", ".join(_rights.COLUMNS) +
                    ") VALUES (" + ", ".join("?" for _ in _rights.COLUMNS) + ")",
                    [row[k] for k in _rights.COLUMNS])
                stored = con.execute(
                    "SELECT * FROM pilot_rights_admissions WHERE workspace_identity=?",
                    (identity,)).fetchone()
                if not _rights.row_equals_file(stored, raw, principal):
                    raise _rights.RightsRefused("RECORD_TAMPERED", "stored row differs from the file")
                data_path, sig_path = _rights.bundle_paths(
                    protected, identity, record["approval_file_sha256"])
                _install(data_path, raw)
                _install(sig_path, sig)
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
        finally:
            con.close()
    finally:
        lock.close()
    # The result shape L5p T1's `preserve` accepts for this window: a
    # committed write on the control store (5.2.1 step 7).
    return {"ok": True, "command": "approve", "tool": "app_rights approve",
            "mode": "commit", "outcome": "committed",
            "db": os.path.abspath(control_db), "writes": WRITES,
            "admission_id": record["admission_id"],
            "workspace_identity": record["workspace_identity"],
            "approved_by": principal,
            "approval_file_sha256": record["approval_file_sha256"],
            "record_sha256": record["record_sha256"]}


def approve_addendum(args) -> dict:
    layout = _layout(args.layout)
    control_db = layout["control_db"]
    protected = layout["protected_dir"]
    try:
        lock = _health.acquire_single_process_lock(control_db)
    except _health.ConfigError:
        raise _rights.RightsRefused(
            "SERVICE_RUNNING", "a web process holds the control store; run in a STOP window") from None
    try:
        signers = _rights.allowed_signers_path(protected)
        principal = _rights.verify_signature(args.file, args.sig, signers,
                                             namespace=_rights.ADDENDUM_NAMESPACE)
        with open(args.file, "rb") as handle:
            raw = handle.read()
        with open(args.sig, "rb") as handle:
            sig = handle.read()
        addendum = _rights.build_addendum(raw, principal)
        # The signature covers these exact bytes; a file swapped between
        # verify and read is refused.
        principal_again = _rights.verify_signature(args.file, args.sig, signers,
                                                   namespace=_rights.ADDENDUM_NAMESPACE)
        with open(args.file, "rb") as handle:
            if handle.read() != raw or principal_again != principal:
                raise _rights.RightsRefused("SIGNATURE_INVALID", "file changed during approval")
        if not os.path.isfile(control_db):
            raise _rights.RightsRefused("INVALID_INPUT", "control store does not exist")
        con = sqlite3.connect("file:" + quote(os.path.abspath(control_db), safe="/") + "?mode=rw",
                              uri=True, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute(_rights.ADDENDUM_DDL)
                identity = addendum["workspace_identity"]
                if con.execute("SELECT 1 FROM workspace_registry WHERE db_identity=?",
                               (identity,)).fetchone() is None:
                    raise _rights.RightsRefused(
                        "WORKSPACE_NOT_REGISTERED", "no registered workspace has this identity")
                record_row = _rights.record_in(con, identity)
                if record_row is None:
                    raise _rights.RightsRefused("NO_RECORD", "the workspace has no rights record")
                if not _rights.row_digest_ok(record_row) or \
                        not _rights.row_matches_bundle(record_row, protected):
                    raise _rights.RightsRefused(
                        "RECORD_TAMPERED", "the rights record differs from its signed file")
                if addendum["admission_id"] != record_row["admission_id"]:
                    raise _rights.RightsRefused(
                        "ADMISSION_MISMATCH", "the addendum names another rights record")
                granted = {tuple(p) for p in addendum["uses"]}
                viewed = _rights.viewed_categories(_rights.row_fields(record_row))
                if any((_rights.MODEL_USE, c) not in granted for c in viewed):
                    raise _rights.RightsRefused(
                        "NOT_COVERED", "the addendum must cover every text category the"
                        " record lets members view")
                if _rights.addendum_in(con, identity) is not None:
                    raise _rights.RightsRefused("WORKSPACE_HAS_ADDENDUM")
                if con.execute("SELECT 1 FROM pilot_model_addenda WHERE addendum_id=?",
                               (addendum["addendum_id"],)).fetchone() is not None:
                    raise _rights.RightsRefused("ADDENDUM_ID_TAKEN")
                row = _rights.addendum_to_row(addendum)
                con.execute(
                    "INSERT INTO pilot_model_addenda (" + ", ".join(_rights.ADDENDUM_COLUMNS) +
                    ") VALUES (" + ", ".join("?" for _ in _rights.ADDENDUM_COLUMNS) + ")",
                    [row[k] for k in _rights.ADDENDUM_COLUMNS])
                stored = _rights.addendum_in(con, identity)
                if not _rights.addendum_row_equals_file(stored, raw, principal):
                    raise _rights.RightsRefused("ADDENDUM_TAMPERED",
                                                "stored row differs from the file")
                data_path, sig_path = _rights.addendum_bundle_paths(
                    protected, identity, addendum["approval_file_sha256"])
                _install(data_path, raw)
                _install(sig_path, sig)
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
        finally:
            con.close()
    finally:
        lock.close()
    return {"ok": True, "command": "approve-addendum", "tool": "app_rights approve-addendum",
            "mode": "commit", "outcome": "committed",
            "db": os.path.abspath(control_db), "writes": ADDENDUM_WRITES,
            "addendum_id": addendum["addendum_id"],
            "workspace_identity": addendum["workspace_identity"],
            "approved_by": principal,
            "approval_file_sha256": addendum["approval_file_sha256"],
            "addendum_sha256": addendum["addendum_sha256"]}


def _permission_set(path: str) -> list:
    rows = _load_json(path, "permission set")
    if not isinstance(rows, list):
        raise _rights.RightsRefused("INVALID_INPUT", "permission set must be a JSON array")
    out = []
    for row in rows:
        if not isinstance(row, dict) or row.get("workspace_id", "local") != "local" or \
                not isinstance(row.get("source_key"), str) or \
                not isinstance(row.get("content_category"), str) or \
                not isinstance(row.get("agent_disclosure"), str) or \
                row.get("export_eligible") not in (0, 1) or \
                isinstance(row.get("export_eligible"), bool):
            raise _rights.RightsRefused("INVALID_INPUT", "malformed permission row")
        out.append(row)
    keys = [(r["source_key"], r["content_category"]) for r in out]
    if len(set(keys)) != len(keys):
        raise _rights.RightsRefused("INVALID_INPUT", "duplicate permission row")
    return out


def _ad_rules_differ(rows) -> list:
    """The ad rules' rows of the named reads carry the named reads' own
    rows (B5 copies them at that key's first load; ADI-R2-05): each
    category of named-reads-ad-rules in the set that differs from
    named-reads-output's row of that category."""
    value = {(r["source_key"], r["content_category"]):
             (r["agent_disclosure"], bool(r["export_eligible"])) for r in rows}
    return [("AD_RULES_PERMISSION_DIFFERS", f"{key} {category}")
            for (key, category), v in sorted(value.items())
            if key == _import.AD_RULES_SOURCE_KEY
            and value.get(("named-reads-output", category), v) != v]


def check_permissions(args) -> dict:
    layout = _layout(args.layout)
    rows = _permission_set(args.set)
    registry, records = _rights.read_control(layout["control_db"])
    entry = next((r for r in registry if r["workspace_id"] == args.workspace_id), None)
    if entry is None:
        raise _rights.RightsRefused("UNKNOWN_WORKSPACE")
    row = next((r for r in records if r["workspace_identity"] == entry["db_identity"]), None)
    record = None
    if row is not None:
        # The full "row equals file" comparison against the mounted bundle,
        # signature verified: a row widened with its digest recomputed fails.
        if not _rights.bundle_available(layout["protected_dir"]):
            raise _rights.RightsRefused("BUNDLE_UNAVAILABLE",
                                        "mount the protected bundle and run again")
        if not _rights.row_digest_ok(row) or \
                not _rights.row_matches_bundle(row, layout["protected_dir"]):
            raise _rights.RightsRefused("RECORD_TAMPERED")
        record = _rights.row_fields(row)
    con = _rights._open_ro(entry["db_path"])
    try:
        keys = _rights.disclosure_keys(con)
    finally:
        con.close()
    problems = _rights.set_problems(record, keys, rows) + _ad_rules_differ(rows)
    features = _rights.parse_features(os.environ.get("EARWITNESS_PILOT_FEATURES")) or ()
    if "ask" in features or "brief_check_live" in features:
        # Start-up refuses ask for an excerpt-level workspace (chat doc S7):
        # never apply a set that would make it one.
        problems += [("ASK_EXCERPT_LEVEL", f"{r['source_key']} transcript") for r in rows
                     if r["content_category"] == "transcript" and r["agent_disclosure"] != "allow"]
    if problems:
        error = _rights.RightsRefused(problems[0][0])
        error.problems = problems
        raise error
    return {"ok": True, "command": "check-permissions", "writes": [],
            "workspace_id": args.workspace_id, "rows": len(rows)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="app_rights.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("approve")
    p.add_argument("--file", required=True)
    p.add_argument("--sig", required=True)
    p.add_argument("--policy", required=True)
    p.add_argument("--layout", required=True)
    p.add_argument("--out")
    p = sub.add_parser("approve-addendum")
    p.add_argument("--file", required=True)
    p.add_argument("--sig", required=True)
    p.add_argument("--layout", required=True)
    p.add_argument("--out")
    p = sub.add_parser("check-permissions")
    p.add_argument("--layout", required=True)
    p.add_argument("--workspace-id", required=True)
    p.add_argument("--set", required=True)
    p.add_argument("--out")
    args = parser.parse_args(argv)
    try:
        result = {"approve": approve, "approve-addendum": approve_addendum,
                  "check-permissions": check_permissions}[args.command](args)
        code = 0
    except _rights.RightsRefused as exc:
        result = {"ok": False, "command": args.command, "code": exc.code,
                  "message": str(exc), "writes": [],
                  "problems": [list(p) for p in getattr(exc, "problems", [])]}
        code = 2
    text = json.dumps(result, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print(text)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
