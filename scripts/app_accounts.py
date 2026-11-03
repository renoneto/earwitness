#!/usr/bin/env python3
"""Pilot local accounts: the operator's login command (PW design rev 4, section 4).

principal --email E
    Read-only. Prints the principal P for E. No database access.
create --email E [--expect-principal P] --out <result.json>
    Creates E's login with a generated password, printed once. Refuses a
    duplicate, a principal in removed_principals, an unreadable admission
    file, or P not equal to the derived principal.
reset-password --principal P --out <result.json>
    A new generated password, printed once. Revokes every session of P in
    the same transaction.
remove --principal P --out <result.json>
    Only when P is in neither admission list and no browser session names
    P (the typo path). Never an alternative to runbook A.
remove --principal P --completed --request <request.json> --baseline <WP-pre.json> --out <result.json>
    Recovery for a remove that committed but lost its result file (runbook
    AC, interruption). The same checks as remove, and P has no row; the
    recorded AC request (kind account_credential) must name P with an
    email that derives P, and local_accounts must differ from the window
    baseline. Writes a fresh result for preserve; never recreates the row.
list
    Read-only, one JSON line per account. Can run while the service is up.

`create`, `reset-password` and `remove` run only in a stopped window:
stdout must be a terminal, and the web lock beside the control store must
be free. Each makes one transaction on EARWITNESS_GRANTS_DB and then
writes a result file for `pilot_inventory.py preserve`. The result file
holds no email, password, hash or salt. The local issuer is the origin of
EARWITNESS_PUBLIC_BASE_URL; the admission file is EARWITNESS_ADMISSION_FILE.

Exit 0 done, 2 refused.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from earwitness.app import admission as _admission  # noqa: E402
from earwitness.app import health as _health  # noqa: E402
from earwitness.app import local_accounts as _la  # noqa: E402

TOOL = "app_accounts"
WRITES = ["local_accounts", "browser_sessions"]
MUTATING = ("create", "reset-password", "remove")


class Refused(Exception):
    """The command refused; nothing was written."""


def _issuer() -> str:
    try:
        return _la.local_issuer(os.environ.get("EARWITNESS_PUBLIC_BASE_URL", ""))
    except ValueError as exc:
        raise Refused(str(exc)) from None


def _grants_db() -> str:
    path = str(os.environ.get("EARWITNESS_GRANTS_DB") or "")
    if not path or not os.path.isabs(path) or not os.path.isfile(path):
        raise Refused("EARWITNESS_GRANTS_DB must name the existing control store")
    return path


def _policy():
    """The loaded admission policy; Refused when it cannot be read."""
    path = _admission.configured_path()
    if not path:
        raise Refused("EARWITNESS_ADMISSION_FILE is not set")
    try:
        return _admission.load_file(path, warn=False)
    except _admission.AdmissionError:
        raise Refused("the admission file is unreadable") from None


def _connect(db: str, *, readonly: bool = False) -> sqlite3.Connection:
    mode = "ro" if readonly else "rw"
    con = sqlite3.connect(f"file:{quote(db, safe='/')}?mode={mode}", uri=True,
                          isolation_level=None)
    con.row_factory = sqlite3.Row
    return con


def _subject(issuer: str, value: str) -> str:
    sub = _la.split_principal(issuer, value)
    if sub is None:
        raise Refused("--principal is not a local principal of this issuer")
    return sub


def _write_result(path: str, doc: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(doc, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _check_out(path: str) -> None:
    """Review LOW-3: the result file must be writable before any commit:
    its folder exists and is writable, and the file does not exist yet."""
    target = os.path.abspath(str(path or ""))
    folder = os.path.dirname(target)
    if not os.path.isdir(folder):
        raise Refused("--out: the folder does not exist")
    if os.path.lexists(target) or os.path.lexists(target + ".tmp"):
        raise Refused("--out: the file already exists; use a fresh name")
    if not os.access(folder, os.W_OK | os.X_OK):
        raise Refused("--out: the folder is not writable")


def _transaction(db: str, body):
    """Run body(con) inside one BEGIN IMMEDIATE transaction."""
    con = _connect(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            out = body(con)
            con.execute("COMMIT")
            return out
        except BaseException:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()


def _create(args, issuer: str, db: str) -> tuple[str, str]:
    try:
        key, _display = _la.canonical(args.email)
    except _la.EmailError as exc:
        raise Refused(str(exc)) from None
    principal = _la.principal(issuer, key)
    if args.expect_principal is not None and args.expect_principal != principal:
        raise Refused("--expect-principal is not the principal derived from --email")
    if principal in _policy().removed_principals:
        raise Refused("this principal was removed from the pilot")
    password = _la.generate_password()
    stored = _la.hash_password(password)

    def body(con):
        try:
            _la.insert_account(con, issuer, args.email, stored, time.time())
        except _la.DuplicateAccount as exc:
            raise Refused(str(exc)) from None
    _transaction(db, body)
    return principal, password, None


def _purchase_seat_check(con, issuer: str, principal: str) -> None:
    """Purchase design 9.5: while a purchase seat's credential is not
    delivered (credential_delivered_at NULL), its account is reset only in
    the fallback states (`awaiting_account`, or `active` and undelivered),
    and only when the protected admission_change record of that seat names
    this P and E. A delivered seat, or an account with no seat, is reset as
    before (runbook AC)."""
    from earwitness.app import purchases as _purchases
    seat = _purchases.holding_seat(con, principal)
    if seat is None or seat.get(_purchases.CREDENTIAL_COLUMN):
        return
    if not _purchases.fallback_reset_allowed(seat):
        raise Refused(f"this principal's purchase seat is {seat['state']}: no reset"
                      " until it is admitted (purchase design 9.5)")
    account = _la.find_by_principal(con, issuer, principal)
    if account is None or _la.lookup_key(account["email"]) != seat["email"]:
        raise Refused("the account's email is not the purchase seat's email")
    from scripts import app_purchases as _ap
    try:
        layout = _ap.Ctx().layout
        req, doc = _ap.buyer_request(_ap.Ctx(), seat["subscription_id"])
    except _ap.Refused as exc:
        raise Refused(f"the protected purchase record cannot be read: {exc}") from None
    want = {"principal": principal, "email": seat["email"]}
    copies = [doc] if doc else []
    if req:
        try:
            copies.append(json.loads(Path(layout["protected_dir"], "register", req,
                                          "request.json").read_bytes()))
        except (OSError, ValueError):
            copies = []
    named = len(copies) == 2 and all(
        any(isinstance(x, dict) and x.get("principal") == want["principal"]
            and _la.lookup_key(str(x.get("email") or "")) == want["email"]
            for x in (c.get("principals") or [])) for c in copies)
    if not named or not _ap.buyer_recorded(_ap.Ctx(), req):
        raise Refused("no protected purchase record names this seat's principal and email")


def _reset(args, issuer: str, db: str) -> tuple[str, str]:
    sub = _subject(issuer, args.principal)
    password = _la.generate_password()
    stored = _la.hash_password(password)

    def body(con):
        _purchase_seat_check(con, issuer, args.principal)
        now = time.time()
        changed = con.execute("UPDATE local_accounts SET password_hash=?, password_set_at=?"
                              " WHERE issuer=? AND subject=?", (stored, now, issuer, sub))
        if changed.rowcount != 1:
            raise Refused("no local account has this principal")
        con.execute("UPDATE browser_sessions SET revoked_at=? WHERE issuer=? AND subject=?"
                    " AND revoked_at IS NULL", (now, issuer, sub))
    _transaction(db, body)
    return args.principal, password, None


def _remove_checks(args, issuer: str) -> str:
    """remove's refusals before the transaction; returns P's subject."""
    sub = _subject(issuer, args.principal)
    policy = _policy()
    if args.principal in policy.principals or args.principal in policy.removed_principals:
        raise Refused("this principal is in the admission file; use runbook A")
    return sub


def _no_session(con, issuer: str, sub: str) -> None:
    if con.execute("SELECT 1 FROM browser_sessions WHERE issuer=? AND subject=? LIMIT 1",
                   (issuer, sub)).fetchone():
        raise Refused("a browser session names this principal; use runbook A")


def _remove(args, issuer: str, db: str) -> tuple:
    if args.completed:
        return _remove_completed(args, issuer, db)
    if args.request is not None or args.baseline is not None:
        raise Refused("--request and --baseline go only with --completed")
    sub = _remove_checks(args, issuer)

    def body(con):
        _no_session(con, issuer, sub)
        gone = con.execute("DELETE FROM local_accounts WHERE issuer=? AND subject=?",
                           (issuer, sub))
        if gone.rowcount != 1:
            raise Refused("no local account has this principal")
    _transaction(db, body)
    return args.principal, None, None


def _read_doc(path, what: str) -> dict:
    try:
        with open(str(path or ""), encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError):
        raise Refused(f"{what} is unreadable") from None
    if not isinstance(doc, dict):
        raise Refused(f"{what} is not a JSON object")
    return doc


def _remove_completed(args, issuer: str, db: str) -> tuple:
    """Astra PWB-R1-02: a remove committed in this AC window, but its
    result file was lost. Confirm the removal against the recorded AC
    request and the window baseline, and write fresh evidence. Nothing is
    written to the control store; the account is never recreated."""
    from scripts import pilot_inventory as _inv
    if not args.request or not args.baseline:
        raise Refused("--completed needs --request and --baseline")
    sub = _remove_checks(args, issuer)
    request = _read_doc(args.request, "the AC request")
    if request.get("kind") != "account_credential":
        raise Refused("the request is not an account_credential (AC) request")
    named = [p for p in request.get("principals") or ()
             if isinstance(p, dict) and p.get("principal") == args.principal]
    if len(named) != 1:
        raise Refused("the AC request does not name this principal once")
    try:
        derived = _la.principal(issuer, named[0].get("email"))
    except _la.EmailError:
        derived = None
    if derived != args.principal:
        raise Refused("the AC request's email does not derive this principal")
    baseline = _read_doc(args.baseline, "the window baseline")
    digests = {os.path.normpath(k): v for k, v in
               (baseline.get("table_digests") or {}).items() if isinstance(v, dict)}
    before = digests.get(os.path.normpath(db), {}).get("local_accounts")
    if not before:
        raise Refused("the baseline is not a window inventory of this control store")

    def body(con):
        _no_session(con, issuer, sub)
        if con.execute("SELECT 1 FROM local_accounts WHERE issuer=? AND subject=?",
                       (issuer, sub)).fetchone():
            raise Refused("the account still exists; run remove without --completed")
        if _inv.table_digest(con, "local_accounts") == before:
            raise Refused("local_accounts is unchanged since the baseline: nothing was"
                          " removed in this window")
    _transaction(db, body)
    return args.principal, None, {
        "of": "remove", "baseline_sha256": _inv.sha256_file(args.baseline),
        "request_sha256": _inv.sha256_file(args.request)}


def _mutate(args) -> int:
    # A stopped window only: a terminal for the one-time password, and no
    # web process holding the control store.
    if not sys.stdout.isatty():
        raise Refused("stdout must be a terminal: the password is shown once")
    _check_out(args.out)
    issuer = _issuer()
    db = _grants_db()
    try:
        lock = _health.acquire_single_process_lock(db)
    except _health.ConfigError:
        raise Refused("the web process holds the control store; run in a stopped window") from None
    try:
        action = {"create": _create, "reset-password": _reset, "remove": _remove}[args.command]
        principal, password, recovery = action(args, issuer, db)
    finally:
        lock.close()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    doc = {"tool": TOOL, "action": args.command, "principal": principal,
           "db": os.path.abspath(db), "mode": "commit", "outcome": "committed",
           "writes": WRITES, "committed_at": now}
    if recovery is not None:
        # The commit happened earlier, in this window; this run confirmed
        # it and wrote no row. committed_at is unknown, so it is null.
        doc["committed_at"] = None
        doc["recovery"] = {**recovery, "confirmed_at": now}
    _write_result(args.out, doc)
    print(f"principal: {principal}")
    if password is not None:
        print(f"password:  {password}")
        print("Shown once. Email it only to this account's own address.")
    return 0


def _list() -> int:
    issuer = _issuer()
    db = _grants_db()
    try:
        policy = _policy()
    except Refused:
        policy = None
    con = _connect(db, readonly=True)
    try:
        rows = con.execute("SELECT issuer, subject, email, password_hash, created_at"
                           " FROM local_accounts ORDER BY created_at, subject").fetchall()
    finally:
        con.close()
    for row in rows:
        principal = f"oidc:{row['issuer']}:{row['subject']}"
        if policy is None:
            admitted = "unknown"
        elif principal in policy.removed_principals:
            admitted = "removed"
        else:
            admitted = "yes" if principal in policy.principals else "no"
        print(json.dumps({
            "principal": principal, "issuer": row["issuer"], "email": row["email"],
            "local_issuer": row["issuer"] == issuer,
            "created_at": datetime.fromtimestamp(row["created_at"], timezone.utc)
            .isoformat(timespec="seconds"),
            "admitted": admitted,
            "password_set": "yes" if row["password_hash"] is not None else "no",
            "malformed_hash": row["password_hash"] is not None
            and not _la.valid_hash(row["password_hash"])}, sort_keys=True))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="app_accounts.py",
                                     description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("principal")
    p.add_argument("--email", required=True)
    p = sub.add_parser("create")
    p.add_argument("--email", required=True)
    p.add_argument("--expect-principal")
    p.add_argument("--out", required=True)
    for name in ("reset-password", "remove"):
        p = sub.add_parser(name)
        p.add_argument("--principal", required=True)
        p.add_argument("--out", required=True)
        if name == "remove":
            p.add_argument("--completed", action="store_true")
            p.add_argument("--request")
            p.add_argument("--baseline")
    sub.add_parser("list")
    args = parser.parse_args(argv)
    try:
        if args.command == "principal":
            try:
                print(_la.principal(_issuer(), args.email))
            except _la.EmailError as exc:
                raise Refused(str(exc)) from None
            return 0
        if args.command == "list":
            return _list()
        return _mutate(args)
    except Refused as exc:
        print(f"app_accounts {args.command}: refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
