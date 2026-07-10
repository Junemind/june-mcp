"""june_mcp — expose June as an MCP server so any agent can attach.

A thin adapter over the ``june_client`` SDK: the tool logic lives in
:mod:`june_mcp.tools` (plain functions, testable without an MCP runtime), and
:func:`june_mcp.server.build_server` wraps them in an MCP ``Server`` (the ``mcp``
package is an optional extra, imported lazily). Agents get retrieval, the
resolution-aware context pack, traversal, write-back, and resolution — all over one
connection, inheriting the service's auth/tenancy/rate-limit.
"""
from __future__ import annotations

from june_mcp.server import build_server, tool_manifest
from june_mcp.tools import TOOLS, Tool, run_tool

__all__ = ["build_server", "tool_manifest", "TOOLS", "Tool", "run_tool"]
