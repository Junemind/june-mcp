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
from june_mcp.prompts import PROMPTS, SERVER_INSTRUCTIONS, render_prompt
from june_mcp.runtime import map_error
from june_mcp.tools import run_tool, visible_tools

log = logging.getLogger("june_mcp")


def build_server(client: JuneClient, *, name: str = "june", readonly: bool = False,
                 pro: bool = True):
    """Build an MCP ``Server`` exposing the June tools over ``client``.

    ``readonly=True`` (JUNE_READONLY=1) removes every write verb from BOTH the
    advertised list and the execution path — a read-only server can't be talked
    into writing. Raises a clear error if the optional ``mcp`` package isn't
    installed.

    The server is created with ``instructions`` (SERVER_INSTRUCTIONS) so a connecting
    agent learns, at the handshake, that it may proactively build pages/dashboards; and
    it advertises PROMPTS (host-surfaced starters that expand a vague ask into a concrete
    page build). Both are inert transport — the security posture is unchanged.
    """
    try:
        from mcp.server import Server
        from mcp.types import (GetPromptResult, Prompt as McpPrompt,
                               PromptArgument, PromptMessage, TextContent,
                               Tool as McpTool)
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            "The MCP server needs the 'mcp' package. Install it with: pip install june-ai[mcp]"
        ) from exc

    server = Server(name, instructions=SERVER_INSTRUCTIONS)
    tools = visible_tools(readonly=readonly, pro=pro)
    # Prompts that would drive a write are meaningless on a read-only server (they exist only to
    # produce pages) — hide them under the same posture as the write tools, by construction.
    prompts = PROMPTS if not readonly else []

    @server.list_tools()
    async def _list() -> list:  # pragma: no cover - needs mcp runtime
        return [McpTool(name=t.name, description=t.description, inputSchema=t.input_schema)
                for t in tools]

    @server.list_prompts()
    async def _list_prompts() -> list:  # pragma: no cover - needs mcp runtime
        return [McpPrompt(
            name=p.name, description=p.description,
            arguments=[PromptArgument(name=a.name, description=a.description,
                                      required=a.required) for a in p.arguments])
            for p in prompts]

    @server.get_prompt()
    async def _get_prompt(name: str, arguments: dict | None):  # pragma: no cover
        if readonly or name not in {p.name for p in prompts}:
            raise KeyError(f"unknown prompt {name!r}")
        text = render_prompt(name, arguments or {})
        return GetPromptResult(
            description=f"June: {name}",
            messages=[PromptMessage(role="user",
                                    content=TextContent(type="text", text=text))])

    @server.call_tool()
    async def _call(tool_name: str, arguments: dict[str, Any] | None):  # pragma: no cover
        try:
            result = run_tool(tool_name, client, arguments or {}, readonly=readonly, pro=pro)
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


def tool_manifest(*, readonly: bool = False, pro: bool = True) -> list[dict]:
    """The tool list as plain dicts (name/description/schema) — handy for docs, a
    capabilities endpoint, or asserting the surface in tests without the mcp runtime."""
    return [{"name": t.name, "description": t.description, "input_schema": t.input_schema,
             "writes": t.writes}
            for t in visible_tools(readonly=readonly, pro=pro)]


__all__ = ["build_server", "tool_manifest"]
