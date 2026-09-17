"""June MCP server — expose the June tools to any MCP-capable agent.

A thin shell over ``june_mcp.tools`` (which holds the actual logic) and a
``JuneClient`` (which carries auth/tenancy/idempotency). The ``mcp`` package is
imported **lazily inside ``build_server``**, so importing this module — and testing
the tools — needs no MCP runtime. Install the optional extra to run a real server:
``pip install june-mcp`` (the ``mcp`` SDK is a hard dependency of that package).

Because the server is a pure client of the REST service, it inherits every security
property (API key → workspace fence, rate-limit, headers) and adds **zero** new
surface to the core (CLAUDE.md §1/§9).
"""
from __future__ import annotations

import functools
import json
import logging
from typing import Any

from june_client import JuneClient
from june_mcp.prompts import PROMPTS, render_prompt
from june_mcp.runtime import DEFAULT_TOOL_CONCURRENCY, map_error
from june_mcp.tools import run_tool, visible_tools

log = logging.getLogger("june_mcp")


def build_server(client: JuneClient, *, name: str = "june", readonly: bool = False,
                 pro: bool = True, strict: bool = False,
                 tool_concurrency: int = DEFAULT_TOOL_CONCURRENCY, profile: str = "full",
                 absent: frozenset[str] | set[str] = frozenset()):
    """Build an MCP ``Server`` exposing the June tools over ``client``.

    ``readonly=True`` (JUNE_READONLY=1) removes every write verb from BOTH the
    advertised list and the execution path — a read-only server can't be talked
    into writing. Raises a clear error if the optional ``mcp`` package isn't
    installed.

    The server is created with ``instructions`` (SERVER_INSTRUCTIONS) so a connecting
    agent learns, at the handshake, that it may proactively build pages/dashboards; and
    it advertises PROMPTS (host-surfaced starters that expand a vague ask into a concrete
    page build). Both are inert transport — the security posture is unchanged.

    CX8 — one stream, many in-flight calls. A5 measured that hosts pipeline requests
    on a single stdio stream, while the sync ``run_tool`` used to execute ON the event
    loop: every call froze the transport read itself, so pipelined requests serialized
    and even list_tools/pings stalled behind a slow answer. Tool execution is therefore
    offloaded to worker threads behind a ``CapacityLimiter(tool_concurrency)`` — overlap
    is real, and the burst ceiling is explicit (backpressure, never a stampede; sized
    inside the httpx pool). Thread-safety audit for the offload: ``httpx.Client`` is
    thread-safe by contract; the tool layer's shared state is ``_NAMES``/``_CONFIRMS``
    (single dict get/set/pop operations — atomic under the GIL) and per-call
    ``for_canvas`` views (fresh objects). Nothing else is shared.
    """
    try:
        import anyio
        from mcp.server import Server
        from mcp.types import (CallToolResult, GetPromptResult, Prompt as McpPrompt,
                               PromptArgument, PromptMessage, TextContent,
                               Tool as McpTool, ToolAnnotations)
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            "The MCP server needs the 'mcp' package. Install it with: pip install june-mcp"
        ) from exc

    lean = profile == "lean"
    server = Server(name, instructions=instructions_for(readonly=readonly, pro=pro, profile=profile,
                                                       absent=absent))
    absent = frozenset(absent)
    tools = visible_tools(readonly=readonly, pro=pro, profile=profile, absent=absent)
    limiter = anyio.CapacityLimiter(max(1, int(tool_concurrency)))  # CX8 ceiling
    # Prompts that would drive a write are meaningless on a read-only server (they exist only to
    # produce pages) — hide them under the same posture as the write tools, by construction. The
    # lean profile has no page verbs at all, so its prompts would point at tools it cannot call.
    prompts = PROMPTS if not (readonly or lean) else []

    @server.list_tools()
    async def _list() -> list:  # pragma: no cover - needs mcp runtime
        # 0.4.2: title + annotations derived from the registry (design §8.4). Without them the
        # MCP defaults present every tool as non-read-only AND destructive.
        return [McpTool(name=t.name, title=t.title or None, description=t.description,
                        inputSchema=t.input_schema,
                        annotations=ToolAnnotations(**t.annotations))
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
        if readonly or lean or name not in {p.name for p in prompts}:
            raise KeyError(f"unknown prompt {name!r}")
        text = render_prompt(name, arguments or {})
        return GetPromptResult(
            description=f"June: {name}",
            messages=[PromptMessage(role="user",
                                    content=TextContent(type="text", text=text))])

    @server.call_tool()
    async def _call(tool_name: str, arguments: dict[str, Any] | None):  # pragma: no cover
        try:
            # CX8: run the sync tool in a worker thread, bounded by the limiter —
            # the event loop keeps reading the stream (overlap, liveness) while
            # at most `tool_concurrency` tools execute.
            result = await anyio.to_thread.run_sync(
                functools.partial(run_tool, tool_name, client, arguments or {},
                                  readonly=readonly, pro=pro, strict=strict, profile=profile,
                                  absent=absent),
                limiter=limiter)
        except Exception as exc:
            # Redacted by construction (runtime.map_error): agent-visible text is
            # built from exception TYPE + HTTP status only — never str(exc), which
            # can embed URLs/headers/keys. Full detail goes to stderr for the
            # operator; the protocol stream stays clean and the server stays up.
            # N2 (0.4.2): a failed call is a tool-execution error and is flagged as one —
            # `isError: true`, as the spec requires for validation/API/business failures — so
            # hosts can count and style it and models self-correct. REFUSALS are not errors:
            # a refusal is the tool working (it returns a normal result with `refused`).
            log.warning("tool %s failed: %s", tool_name, type(exc).__name__)
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps({"error": map_error(exc)}))],
                isError=True)
        return [TextContent(type="text", text=json.dumps(result, default=str))]

    return server


def instructions_for(*, readonly: bool = False, pro: bool = True, profile: str = "full",
                     absent: frozenset[str] | set[str] = frozenset()) -> str:
    """The server instructions a connection receives at the handshake for this posture — generated
    from the paragraphs that teach tools actually ON this surface (N4: never teach what a
    connection cannot call)."""
    from june_mcp.instructions import render
    names = [t.name for t in visible_tools(readonly=readonly, pro=pro, profile=profile, absent=absent)]
    return render(names, profile=profile)


def tool_manifest(*, readonly: bool = False, pro: bool = True, profile: str = "full",
                  absent: frozenset[str] | set[str] = frozenset()) -> list[dict]:
    """The tool list as plain dicts (name/title/description/schema/annotations) — handy for docs,
    a capabilities endpoint, or asserting the surface in tests without the mcp runtime. This is
    exactly what ``tools/list`` sends, minus the wire envelope."""
    return [{"name": t.name, "title": t.title, "description": t.description,
             "input_schema": t.input_schema, "annotations": t.annotations, "writes": t.writes}
            for t in visible_tools(readonly=readonly, pro=pro, profile=profile, absent=absent)]


__all__ = ["build_server", "instructions_for", "tool_manifest"]
