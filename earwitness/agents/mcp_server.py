"""EarWitness local MCP reader: official-SDK protocol mapping only (package D).

All domain validation lives in :mod:`earwitness.agents`; this module only
registers the scoped tool catalog on an ``mcp.server.MCPServer``, shapes
``CallToolResult`` (structuredContent object + the same JSON in a text
block, BOTH copies budgeted), and runs stdio. Requires the optional
``requirements-agent.txt`` install; the application never imports this
module without it.
"""
from __future__ import annotations

import json
import sys

try:
    from mcp.server import MCPServer
    from mcp.types import CallToolResult, TextContent, ToolAnnotations
except ImportError as exc:
    raise RuntimeError(
        "the MCP reader needs the optional agent dependencies: install "
        "requirements-agent.txt into this interpreter first") from exc

from earwitness import agents as _domain

READ_ONLY_HINTS = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                  idempotent_hint=True, open_world_hint=False)

_SERVER_NAME = "earwitness-local-reader"
_SERVER_VERSION = "1.0.0"


def result_payload(payload: dict) -> CallToolResult:
    """One result in both MCP representations from a single serialization."""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return CallToolResult(content=[TextContent(type="text", text=text)],
                          structured_content=json.loads(text),
                          is_error=False)


def error_result(failure: _domain.ToolFailure) -> CallToolResult:
    """Executed tool-domain failure: isError with the stable §13 envelope."""
    payload = {"error": failure.envelope()}
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return CallToolResult(content=[TextContent(type="text", text=text)],
                          structured_content=json.loads(text),
                          is_error=True)


_JSON_TYPES = {"string": str, "integer": int, "number": float,
               "boolean": bool, "object": dict}


def _handler_signature(schema: dict):
    """Strict per-tool signature derived from the single catalog source, so
    the SDK publishes exact input schemas (additionalProperties stays
    enforced domain-side by the frozen request parsers)."""
    import inspect
    params = []
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    for pname, pspec in properties.items():
        annotation = _JSON_TYPES.get(pspec.get("type"), str)
        if pname in required:
            default = inspect.Parameter.empty
        else:
            annotation = annotation | None
            default = None
        params.append(inspect.Parameter(pname, inspect.Parameter.KEYWORD_ONLY,
                                        annotation=annotation, default=default))
    return inspect.Signature(params)


def _register(server: MCPServer, workspace_id: str, scopes: frozenset, context=None) -> None:
    """Register exactly the scoped catalog: narrower grants advertise only
    permitted tools. Handler bodies pass caller arguments straight through;
    strict domain validation stays in earwitness.agents."""
    import anyio
    catalog = {t["name"] for t in _domain.tools_for_scopes(scopes)}

    def _tool(spec: dict) -> None:
        name = spec["name"]

        async def handler(**kwargs):
            try:
                payload = await anyio.to_thread.run_sync(
                    lambda: _domain.call_tool(workspace_id, scopes, name,
                                             kwargs, context=context))
            except _domain.ToolFailure as failure:
                return error_result(failure)
            return result_payload(payload)

        handler.__name__ = f"earwitness_{name}"
        handler.__signature__ = _handler_signature(spec["schema"])
        server.add_tool(handler, name=name, description=spec["description"],
                        annotations=READ_ONLY_HINTS)

    for spec in _domain.TOOL_DEFS:
        if spec["name"] in catalog:
            _tool(spec)


def build_server(workspace_id: str, workspace_path: str,
                 scopes, *, context=None) -> MCPServer:
    """Create the reader bound to one workspace and one fixed read profile.

    Binding is server-side: the workspace path and scopes are fixed at
    launch, never model-selectable. Returns the configured MCPServer (the
    caller chooses stdio or loopback HTTP transport).
    """
    from pathlib import Path
    from earwitness.app import agent_contracts as _contracts
    _contracts.bind_workspace(workspace_id, str(Path(workspace_path)))
    scope_set = frozenset(scopes)
    if context is None:
        from earwitness.app import connections
        connections.local_reader_context(workspace_id, workspace_path, scope_set)
    server = MCPServer(name=_SERVER_NAME, version=_SERVER_VERSION,
                       instructions=("EarWitness local read-only research reader. "
                                     "All tools read stored workspace data only; "
                                     "no tool writes, exports, or acquires media."))
    _register(server, workspace_id, scope_set, context)
    return server


async def run_stdio(server: MCPServer) -> None:
    """Serve stdio: stdout carries protocol traffic ONLY (logs go to stderr
    by construction — this module never prints)."""
    await server.run_stdio_async()


def main(argv=None) -> int:
    print("earwitness-local-reader: use scripts/app_mcp.py "
          "(workspace + profile are launch-fixed)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
