#!/usr/bin/env python3
"""Owner-run importer of Meta's Muse Spark pods names (PODS).

Each kept row becomes one brand_assocs row (basis "model", origin
"muse-spark", channel spoken or url). Three modes:

  --dry-run (default)  plan the import, print counts, write nothing;
  --commit             insert the new kept rows, one audit row and one
                       revision bump in a single transaction; a rerun of
                       the same file changes nothing; a different file, or
                       a changed kept set, is refused while this origin's
                       rows are active (--deactivate first);
  --deactivate         delete every model/muse-spark row, one audit row,
                       one revision bump, in a single transaction.

The input must match --sha256 (checked before it is parsed). The importer
never edits the checked-brand list (earwitness/data/display-brands.json)
or any non-model row, and never remaps an evidence id.

Runbook: ws-fresh/pods/RUNBOOK.md.

    python -m scripts.import_model_names --workspace DIR|DB --input FILE \\
        --sha256 HEX --det on|off [--dry-run|--commit] [--kept-out FILE] [--actor NAME]
    python -m scripts.import_model_names --workspace DIR|DB --deactivate --det on|off

--det must equal what the display merges now: on only when EARWITNESS_AD_RULES_DET
and EARWITNESS_AD_RULES are both on (exit 2 if not).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from earwitness.app import ad_reads, customer, db, import_sources, ops  # noqa: E402

LABEL = "model_named"
IMPORT_ACTION = "model_names_import"
DEACTIVATE_ACTION = "model_names_deactivate"
# The checked-brand list's base file, as customer.py names it (read only, for its sha).
BRANDS_FILE = customer._DISPLAY_BRANDS
KEPT_OUT_FIELDS = ("evidence_id", "brand", "channel", "span_equal", "new")
SKIP_REASONS = ("bad_row", "duplicate_row", "id_missing", "episode_mismatch", "not_shown",
                "held", "already_named", "quote_not_in_span", "brand_not_checked")


class InputRefused(Exception):
    """The input file does not match the expected sha256 (or is not JSON lines)."""


class ImportRefused(Exception):
    """Another import's model rows are active; deactivate them first."""


class _Rollback(Exception):
    pass


def load_input(path, expected_sha256: str) -> list:
    data = Path(path).read_bytes()
    if hashlib.sha256(data).hexdigest() != str(expected_sha256 or "").strip().lower():
        raise InputRefused("input sha256 does not match --sha256")
    rows = []
    for number, line in enumerate(data.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            raise InputRefused(f"line {number} is not JSON") from None
        if not isinstance(row, dict):
            raise InputRefused(f"line {number} is not a JSON object")
        rows.append(row)
    return rows


def _norm(text) -> str:
    return re.sub(r"\s+", " ", str(text or "").casefold()).strip()


def _span_text(con, episode_id, version_id, lo, hi) -> str:
    parts = []
    for seg in ops.transcript_segments(con, episode_id, version_id):
        try:
            start, end = float(seg["start"]), float(seg["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end > lo and start < hi:
            parts.append(str(seg.get("text") or "").strip())
    return " ".join(parts)


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _span_equal(row, span) -> bool:
    got = row.get("span")
    try:
        return (abs(float(got["start_s"]) - float(span[0])) < 1e-6
                and abs(float(got["end_s"]) - float(span[1])) < 1e-6)
    except (KeyError, TypeError, ValueError):
        return False


def _plan(con, rows, input_sha256) -> dict:
    targets = customer.model_name_targets(con)
    aliases = customer.customer_aliases(con)
    checked = customer.brand_checker(con)
    stored = [tuple(r) for r in con.execute(
        "SELECT evidence_id, brand, channel FROM brand_assocs WHERE basis=? AND origin=?"
        " ORDER BY id", (customer.MODEL_BASIS, customer.MODEL_ORIGIN))]
    existing = set(stored)
    skipped = {reason: 0 for reason in SKIP_REASONS}
    seen, texts, kept, first_seen = set(), {}, [], {}
    for row in rows:
        if not isinstance(row, dict):
            skipped["bad_row"] += 1
            continue
        eid, episode = _text(row.get("evidence_id")), _text(row.get("episode_id"))
        brand, quote = _text(row.get("canonical_brand")), _text(row.get("quote"))
        channel = row.get("name_channel")
        if (row.get("label") != LABEL or channel not in customer.MODEL_CHANNELS
                or not (eid and episode and brand and quote)
                or not (_text(row.get("fetch_date")) and _text(row.get("fetch_date_source")))):
            skipped["bad_row"] += 1
            continue
        key = ad_reads.canonical(aliases, brand)
        if (eid, key, channel) in seen:
            skipped["duplicate_row"] += 1
            continue
        seen.add((eid, key, channel))
        ev = con.execute("SELECT episode_id FROM evidence WHERE id=?", (eid,)).fetchone()
        if ev is None:
            skipped["id_missing"] += 1
            continue
        if ev[0] != episode:
            skipped["episode_mismatch"] += 1
            continue
        target = targets.get(eid)
        if target is None:
            skipped["not_shown"] += 1
            continue
        if target["state"] == "held":
            skipped["held"] += 1
            continue
        if target["state"] == "named":
            skipped["already_named"] += 1
            continue
        lo, hi = target["display_span"]
        cache_key = (target["episode_id"], target["transcript_version_id"], lo, hi)
        if cache_key not in texts:
            texts[cache_key] = _norm(_span_text(con, *cache_key))
        position = texts[cache_key].find(_norm(quote))
        if position < 0:
            skipped["quote_not_in_span"] += 1
            continue
        if not checked(key):
            skipped["brand_not_checked"] += 1
            continue
        prov = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
        first_seen.setdefault(eid, len(first_seen))
        kept.append(((first_seen[eid], position, key), {
            "evidence_id": eid, "brand": key, "channel": channel,
            "label": LABEL, "model": row.get("model"),
            "fetch_date": row.get("fetch_date"),
            "fetch_date_source": row.get("fetch_date_source"),
            "call_id": prov.get("call_id"),
            "display_brands_sha256": prov.get("display_brands_sha256"),
            "span_equal": _span_equal(row, target["display_span"]),
            "new": (eid, key, channel) not in existing}))
    kept = [entry for _, entry in sorted(kept, key=lambda pair: pair[0])]
    kept_keys = {(k["evidence_id"], k["brand"], k["channel"]) for k in kept}
    planned_order, stored_order = {}, {}
    for k in kept:
        planned_order.setdefault(k["evidence_id"], []).append((k["brand"], k["channel"]))
    for eid, brand, channel in stored:
        if (eid, brand, channel) in kept_keys:
            stored_order.setdefault(eid, []).append((brand, channel))
    reordered = sum(1 for eid, order in stored_order.items()
                    if order != [b for b in planned_order[eid] if b in set(order)])
    by_channel = {channel: sum(1 for k in kept if k["channel"] == channel)
                  for channel in ("spoken", "url")}
    return {"input_sha256": input_sha256, "rows": len(rows), "kept": kept,
            "skipped": {k: v for k, v in skipped.items() if v},
            "kept_new": sum(1 for k in kept if k["new"]),
            "kept_unchanged": sum(1 for k in kept if not k["new"]),
            "span_equal": sum(1 for k in kept if k["span_equal"]),
            "span_changed": sum(1 for k in kept if not k["span_equal"]),
            "stale": len(existing - kept_keys),
            "reordered": reordered,
            "by_channel": by_channel}


def plan(con, rows, input_sha256: str) -> dict:
    """The import plan. Runs inside a transaction that is always rolled
    back, so nothing is written even if a customer helper creates map rows."""
    out = {}
    try:
        with db.transaction(con, write=True):
            out.update(_plan(con, rows, input_sha256))
            out["would_refuse"] = _refusal(con, input_sha256, out) is not None
            raise _Rollback
    except _Rollback:
        pass
    return out


def _counts(planned: dict) -> dict:
    return {**{k: v for k, v in planned.items() if k != "kept"}, "kept": len(planned["kept"])}


def _other_import_active(con, input_sha256: str) -> bool:
    """The commit refusal rule: another import's rows are active."""
    have = con.execute("SELECT 1 FROM brand_assocs WHERE basis=? AND origin=? LIMIT 1",
                       (customer.MODEL_BASIS, customer.MODEL_ORIGIN)).fetchone()
    if have is None:
        return False
    last = con.execute("SELECT action, detail FROM audit WHERE action IN (?, ?)"
                       " ORDER BY id DESC LIMIT 1", (IMPORT_ACTION, DEACTIVATE_ACTION)).fetchone()
    if last is None or last["action"] != IMPORT_ACTION:
        return False
    try:
        active = json.loads(last["detail"]).get("input_sha256")
    except (TypeError, ValueError, AttributeError):
        active = None
    return active != input_sha256


def _refusal(con, input_sha256: str, planned: dict):
    """The commit refusal message, or None: another import's rows are
    active, or this origin's rows are active and the planned kept set
    differs from them (a new kept row, a stale one, or a read whose names
    would be inserted in another order than the stored rows: brand_assocs id
    order is the display order)."""
    if _other_import_active(con, input_sha256):
        return "model names from a different input are active; run --deactivate first"
    if (planned["kept_new"] or planned["stale"] or planned["reordered"]) and con.execute(
            "SELECT 1 FROM brand_assocs WHERE basis=? AND origin=? LIMIT 1",
            (customer.MODEL_BASIS, customer.MODEL_ORIGIN)).fetchone() is not None:
        return "the data changed since the active import; run --deactivate, then --commit"
    return None


def _check_not_refused(con, input_sha256: str, planned: dict) -> None:
    message = _refusal(con, input_sha256, planned)
    if message is not None:
        raise ImportRefused(message)


def commit(con, rows, input_sha256: str, actor: str = "owner", det=None) -> dict:
    return _commit(con, rows, input_sha256, actor, det)[0]


def _commit(con, rows, input_sha256: str, actor: str, det) -> tuple:
    with db.transaction(con, write=True):
        planned = {}
        try:
            with db.transaction(con, write=True):
                planned.update(_plan(con, rows, input_sha256))
                raise _Rollback
        except _Rollback:
            pass
        _check_not_refused(con, input_sha256, planned)
        new = [k for k in planned["kept"] if k["new"]]
        written = 0
        if new:
            now = db.utcnow()
            for entry in new:
                con.execute("INSERT INTO brand_assocs (evidence_id, brand, basis, origin,"
                            " revision, updated_at, channel) VALUES (?, ?, ?, ?, 1, ?, ?)",
                            (entry["evidence_id"], entry["brand"], customer.MODEL_BASIS,
                             customer.MODEL_ORIGIN, now, entry["channel"]))
                written += 1
            rev = db.bump_revision(con)
            detail = {"input_sha256": input_sha256, "label": LABEL,
                      "origin": customer.MODEL_ORIGIN, "counts": _counts(planned),
                      "kept": [{k: v for k, v in e.items() if k != "new"}
                               for e in planned["kept"]]}
            if det is not None:
                detail.update(det=bool(det), display_merge_state=customer.display_merge_state(),
                              ad_rules=_ad_rules())
            db.audit(con, actor, IMPORT_ACTION, "brand_assocs", customer.MODEL_ORIGIN,
                     detail, rev)
    return {"written": written, **_counts(planned)}, planned["kept"]


def deactivate(con, actor: str = "owner", det=None) -> dict:
    with db.transaction(con, write=True):
        rows = con.execute("SELECT id, evidence_id, brand, channel FROM brand_assocs"
                           " WHERE basis=? AND origin=? ORDER BY id",
                           (customer.MODEL_BASIS, customer.MODEL_ORIGIN)).fetchall()
        if not rows:
            return {"deleted": 0}
        con.execute("DELETE FROM brand_assocs WHERE basis=? AND origin=?",
                    (customer.MODEL_BASIS, customer.MODEL_ORIGIN))
        rev = db.bump_revision(con)
        detail = {"deleted": len(rows),
                  "rows": [{"evidence_id": r["evidence_id"], "brand": r["brand"],
                            "channel": r["channel"]} for r in rows]}
        if det is not None:
            detail.update(det=bool(det), display_merge_state=customer.display_merge_state(),
                          ad_rules=_ad_rules())
        db.audit(con, actor, DEACTIVATE_ACTION, "brand_assocs", customer.MODEL_ORIGIN,
                 detail, rev)
    return {"deleted": len(rows)}


def _ad_rules() -> str:
    """The raw EARWITNESS_AD_RULES setting, on (also unset) or off."""
    value = str(os.environ.get("EARWITNESS_AD_RULES") or "on").strip().lower() or "on"
    return "on" if value == "on" else "off"


def _display_brands_info(rows) -> dict:
    here = hashlib.sha256(BRANDS_FILE.read_bytes()).hexdigest()
    different = missing = 0
    for row in rows:
        prov = row.get("provenance") if isinstance(row, dict) else None
        got = prov.get("display_brands_sha256") if isinstance(prov, dict) else None
        if not got:
            missing += 1
        elif got != here:
            different += 1
    return {"checkout_sha256": here, "rows_different_sha256": different,
            "rows_without_sha256": missing}


def _connect(path: Path):
    """The database file opened as db.connect opens a workspace's (never
    created: main checks it exists; modes and folder left as they are)."""
    con = sqlite3.connect(str(path), timeout=15.0, isolation_level=None,
                          factory=db._Connection)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA foreign_keys=ON;")
    con.execute("PRAGMA busy_timeout=15000;")
    return con


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.import_model_names",
                                     description="Import Muse Spark pods names (owner-run).")
    parser.add_argument("--workspace", required=True,
                        help="workspace folder (workspace.sqlite3) or database file")
    parser.add_argument("--input", help="JSON lines file (dry-run and commit)")
    parser.add_argument("--sha256", help="expected sha256 of the input file")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="mode", action="store_const", const="dry-run",
                      help="plan and print counts, write nothing (default)")
    mode.add_argument("--commit", dest="mode", action="store_const", const="commit")
    mode.add_argument("--deactivate", dest="mode", action="store_const", const="deactivate")
    parser.add_argument("--det", choices=("on", "off"),
                        help="the DET setting; must equal EARWITNESS_AD_RULES_DET (required)")
    parser.add_argument("--kept-out", help="write the kept rows' ids to this new file "
                                           "(JSON lines; not with --deactivate)")
    parser.add_argument("--actor", default="owner")
    parser.set_defaults(mode="dry-run")
    return parser


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 2


def main(argv=None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        import_sources.ad_rules_det_enabled()
    except (import_sources.ImportError, ImportError) as exc:
        return _fail(f"refused: {exc}")
    state = customer.display_merge_state()
    det = state != "off"
    if args.det is None:
        return _fail(f"refused: --det on|off is required (must equal the display state, "
                     f"{import_sources.DET_ENV} on with EARWITNESS_AD_RULES on)")
    if (args.det == "on") != det:
        return _fail(f"refused: --det {args.det} does not match the display state "
                     f"({'on' if det else 'off'}: {import_sources.DET_ENV} "
                     f"{os.environ.get(import_sources.DET_ENV) or 'unset'}, "
                     f"EARWITNESS_AD_RULES {_ad_rules()})")
    kept_out = Path(args.kept_out) if args.kept_out else None
    if kept_out is not None and (args.mode == "deactivate" or kept_out.exists()):
        return _fail("refused: --kept-out is not allowed with --deactivate"
                     if args.mode == "deactivate" else "refused: --kept-out file exists")
    workspace = Path(args.workspace)
    target = workspace / "workspace.sqlite3" if workspace.is_dir() else workspace
    if not target.is_file():
        return _fail("refused: --workspace is not a workspace folder or database file")
    summary = {"mode": args.mode, "det": det, "display_merge_state": state,
               "ad_rules": _ad_rules()}
    rows = []
    if args.mode != "deactivate":
        if not args.input or not args.sha256:
            return _fail("refused: --input and --sha256 are required")
        try:
            rows = load_input(args.input, args.sha256)
        except InputRefused as exc:
            return _fail(f"refused: {exc}")
        except (OSError, UnicodeDecodeError) as exc:
            return _fail(f"refused: cannot read input ({type(exc).__name__})")
        sha = args.sha256.strip().lower()
        summary["input_sha256"] = sha
        summary["display_brands"] = _display_brands_info(rows)
    handle = None
    if kept_out is not None:
        # Opened before the database, so a bad path refuses before any write.
        try:
            handle = open(kept_out, "x", encoding="utf-8")
        except OSError as exc:
            return _fail(f"refused: cannot write --kept-out ({type(exc).__name__})")
    con = None
    kept = []
    try:
        con = _connect(target)
        if args.mode == "deactivate":
            summary.update(deactivate(con, args.actor, det))
        elif args.mode == "commit":
            result, kept = _commit(con, rows, sha, args.actor, det)
            summary.update(result)
        else:
            planned = plan(con, rows, sha)
            kept = planned["kept"]
            summary.update(_counts(planned))
    except Exception as exc:
        if handle is not None:
            handle.close()
            kept_out.unlink(missing_ok=True)
        if isinstance(exc, ImportRefused):
            return _fail(f"refused: {exc}")
        if isinstance(exc, sqlite3.DatabaseError):
            # sqlite's own text (e.g. "file is not a database", "database is
            # locked"); a failed commit rolled back, so nothing was written.
            return _fail(f"refused: workspace database error ({type(exc).__name__}: {exc})")
        raise
    finally:
        if con is not None:
            con.close()
    if handle is not None:
        with handle:
            for entry in kept:
                handle.write(json.dumps({k: entry[k] for k in KEPT_OUT_FIELDS},
                                        sort_keys=True) + "\n")
    print(json.dumps(summary, sort_keys=True, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
