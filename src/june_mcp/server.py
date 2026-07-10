"""June MCP server — expose the June tools to any MCP-capable agent.

A thin shell over ``june_mcp.tools`` (which holds the actual logic) and a
``JuneClient`` (which carries auth/tenancy/idempotency). The ``mcp`` package is
imported **lazily inside ``build_server``**, so importing this module — and testing
the tools — needs no MCP runtime. Install the optional extra to run a real server:
``pip install june-ai[mcp]``.

Because the server is a pure client of the REST service, it inherits every security
property (API key → workspace fence, rate-limit, headers) and adds **zero** new
surface to the core (CLAUDE.md §1/§9).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from june_client import JuneClient
from june_mcp.runtime import map_error
from june_mcp.tools import run_tool, visible_tools

log = logging.getLogger("june_mcp")


def build_server(client: JuneClient, *, name: str = "june", readonly: bool = False):
    """Build an MCP ``Server`` exposing the June tools over ``client``.

    ``readonly=True`` (JUNE_READONLY=1) removes every write verb from BOTH the
    advertised list and the execution path — a read-only server can't be talked
    into writing. Raises a clear error if the optional ``mcp`` package isn't
    installed.
    """
    try:
        from mcp.server import Server
        from mcp.types import TextContent, Tool as McpTool
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            "The MCP server needs the 'mcp' package. Install it with: pip install june-ai[mcp]"
        ) from exc

    server = Server(name)
    tools = visible_tools(readonly=readonly)

    @server.list_tools()
    async def _list() -> list:  # pragma: no cover - needs mcp runtime
        return [McpTool(name=t.name, description=t.description, inputSchema=t.input_schema)
                for t in tools]

    @server.call_tool()
    async def _call(tool_name: str, arguments: dict[str, Any] | None):  # pragma: no cover
        try:
            result = run_tool(tool_name, client, arguments or {}, readonly=readonly)
        except Exception as exc:
            # Redacted by construction (runtime.map_error): agent-visible text is
            # built from exception TYPE + HTTP status only — never str(exc), which
            # can embed URLs/headers/keys. Full detail goes to stderr for the
            # operator; the protocol stream stays clean and the server stays up.
            log.warning("tool %s failed: %s", tool_name, type(exc).__name__)
            return [TextContent(type="text",
                                text=json.dumps({"error": map_error(exc)}))]
        return [TextContent(type="text", text=json.dumps(result, default=str))]

    return server


def tool_manifest(*, readonly: bool = False) -> list[dict]:
    """The tool list as plain dicts (name/description/schema) — handy for docs, a
    capabilities endpoint, or asserting the surface in tests without the mcp runtime."""
    return [{"name": t.name, "description": t.description, "input_schema": t.input_schema,
             "writes": t.writes}
            for t in visible_tools(readonly=readonly)]


__all__ = ["build_server", "tool_manifest"]
