#!/usr/bin/env python3
"""EarWitness local MCP reader over stdio (package D, owner-operated).

Explicit local command: absolute workspace path + fixed read profile at
launch. Startup performs read-only inspection (schema compatible or
actionable error; NEVER init/migrate/seed/reindex). No network listener;
no model-selectable filesystem paths. stdout carries protocol traffic ONLY.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

READER_SCOPES = frozenset({"research:read", "transcripts:read"})
OPTIONAL_SCOPES = {"campaigns": "campaigns:read",
                   "watchlists": "watchlists:read",
                   "notes": "notes:read"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="EarWitness local MCP reader (stdio).")
    p.add_argument("--workspace", required=True,
                   help="Absolute workspace directory path (fixed at launch).")
    p.add_argument("--workspace-id", default="local",
                   help="Workspace identity label for cursors/citations.")
    p.add_argument("--allow-campaigns", action="store_true")
    p.add_argument("--allow-watchlists", action="store_true")
    p.add_argument("--allow-notes", action="store_true")
    return p


def profile_scopes(args) -> frozenset:
    scopes = set(READER_SCOPES)
    if args.allow_campaigns:
        scopes.add(OPTIONAL_SCOPES["campaigns"])
    if args.allow_watchlists:
        scopes.add(OPTIONAL_SCOPES["watchlists"])
    if args.allow_notes:
        scopes.add(OPTIONAL_SCOPES["notes"])
    return frozenset(scopes)


def inspect_workspace(ws: Path):
    """Read-only startup inspection. Returns (db_path, problems)."""
    from earwitness.app import db as _db
    db_path = ws / "workspace.sqlite3"
    if not db_path.exists():
        return None, (f"no workspace database at {db_path}; prepare one first "
                       "(see SETUP.md) — this command never creates one")
    import sqlite3
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        try:
            _db.require_current_schema(con)
        except RuntimeError as exc:
            return None, str(exc)
    finally:
        con.close()
    if (ws / "quarantine.json").exists():
        try:
            reason = json.loads((ws / "quarantine.json").read_text()).get(
                "reason", "quarantined")
        except (OSError, ValueError):
            reason = "quarantined"
        return None, (f"workspace is quarantined ({reason}); reconcile with: "
                       "python3 scripts/app_workspace.py reconcile "
                       "--workspace <workspace>")
    return db_path, None


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    ws = Path(args.workspace)
    if not ws.is_absolute():
        print("error: --workspace must be an absolute path (fixed at launch; "
              "clients cannot select filesystem paths)", file=sys.stderr)
        return 2
    if REPO.resolve() not in ws.resolve().parents and ws.resolve() != REPO.resolve():
        print("error: --workspace must live inside the repository",
              file=sys.stderr)
        return 2
    _, problem = inspect_workspace(ws)
    if problem is not None:
        print(f"error: cannot serve workspace: {problem}", file=sys.stderr)
        return 1
    try:
        from earwitness.agents import mcp_server
        from earwitness.app import connections
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    scopes = profile_scopes(args)
    try:
        server = mcp_server.build_server(args.workspace_id, str(ws), scopes)
    except (OSError, ValueError, RuntimeError, connections.GrantError) as exc:
        print(f"error: prepared local reader control/profile required: {exc}", file=sys.stderr)
        return 1
    print(f"EarWitness MCP reader on stdio "
          f"(workspace {ws}, scopes {sorted(scopes)})", file=sys.stderr)
    asyncio.run(mcp_server.run_stdio(server))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
