"""``python -m june_mcp`` — run June's MCP server over stdio (Claude-spawnable).

This is the missing entrypoint Phase MC adds: agent hosts (Claude Desktop,
Claude Code) launch exactly this module and speak JSON-RPC over its
stdin/stdout. Three modes:

* *(default)*      — serve MCP over stdio until the host closes the pipe.
* ``--manifest``   — print the tool manifest as JSON and exit (docs/debug; the
                     only mode that intentionally writes to stdout).
* ``--doctor``     — verify config + connectivity and print PASS/FAIL per check;
                     exit 0 only if all pass. Run this BEFORE wiring the server
                     into any agent host config.

Exit codes: 0 ok · 1 doctor found failures · 2 configuration invalid.
All server-mode diagnostics go to stderr; stdout carries only protocol frames.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from june_mcp.runtime import (
    CanvasNotFoundError, CanvasResolutionError, ConfigError, configure_logging,
    load_config, make_client, map_error, resolve_canvas,
)


def _print_config_error(exc: ConfigError) -> None:
    print("june-mcp: configuration invalid — refusing to start (fail-closed):",
          file=sys.stderr)
    for p in exc.problems:
        print(f"  - {p}", file=sys.stderr)


def _manifest() -> int:
    # Imported lazily so `--manifest` works without the 'mcp' extra installed.
    from june_mcp.server import tool_manifest
    json.dump(tool_manifest(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def _doctor() -> int:
    """Pre-flight: every check prints one line; exit 0 iff all PASS."""
    results: list[tuple[str, bool, str]] = []

    try:
        cfg = load_config()
        results.append(("config", True, f"base_url={cfg.base_url} canvas={cfg.canvas!r} "
                        f"readonly={cfg.readonly} timeouts={cfg.timeout_read:g}s/"
                        f"{cfg.timeout_answer:g}s"))
    except ConfigError as exc:
        for p in exc.problems:
            results.append(("config", False, p))
        _report(results)
        return 1

    import httpx

    def _detail(exc: Exception) -> str:
        # Human-facing doctor: redacted mapped message + a canvas-specific hint,
        # because "HTTPStatusError" alone sent a real first run hunting (2026-07-08):
        # X-Canvas takes the canvas id, and an unknown/foreign canvas fails closed.
        msg = map_error(exc)
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (403, 404):
            msg += (" Hint: JUNE_CANVAS must be an existing canvas (name or id) owned "
                    "by this key — create one in the Junê app, or set "
                    "JUNE_CANVAS_CREATE=1 to create a named canvas on first run.")
        return msg

    client = make_client(cfg)
    try:
        try:
            client.healthz()
            results.append(("service reachable (/healthz)", True, "ok"))
        except Exception as exc:
            results.append(("service reachable (/healthz)", False, _detail(exc)))
        # Canvas resolution — names resolve via GET /v1/canvases; ids pass through
        # with zero traffic. The doctor NEVER creates: with JUNE_CANVAS_CREATE=1 a
        # missing name is reported as the (accurate) prediction that serve-mode
        # will create it, without the doctor doing the write itself.
        try:
            resolved, how = resolve_canvas(client, cfg.canvas, create=False)
            client.canvas = resolved
            results.append(("canvas resolution", True, how))
        except CanvasNotFoundError as exc:
            if cfg.canvas_create:
                results.append(("canvas resolution", True,
                                f'name "{cfg.canvas}" not found — will be created on '
                                "first run (JUNE_CANVAS_CREATE=1)"))
            else:
                results.append(("canvas resolution", False, str(exc)))
        except CanvasResolutionError as exc:  # ambiguity: CREATE can't fix this
            results.append(("canvas resolution", False, str(exc)))
        except Exception as exc:
            results.append(("canvas resolution", False, _detail(exc)))
        try:
            client.search_health()
            results.append(("canvas + search seam (/v1/search/health)", True, "ok"))
        except Exception as exc:
            results.append(("canvas + search seam (/v1/search/health)", False, _detail(exc)))
        # Edition tag — display-only, never a gate: entitlements are enforced
        # server-side per route no matter what this prints, and a service that
        # predates /v1/whoami still doctors clean (graceful degradation).
        try:
            who = client.whoami()
            tag = str(who.get("edition_tag") or f"june-{who.get('tier', 'free')}")
            results.append(("edition (/v1/whoami)", True,
                            f"tier={who.get('tier', 'free')} → [{tag}]"))
        except Exception:
            results.append(("edition (/v1/whoami)", True,
                            "not reported (service predates /v1/whoami) — display-only"))
    finally:
        client.close()

    from june_mcp.server import tool_manifest
    tools = tool_manifest(readonly=cfg.readonly)
    results.append((f"tool manifest ({len(tools)} tools"
                    f"{', read-only' if cfg.readonly else ''})", bool(tools),
                    ", ".join(t["name"] for t in tools)))

    _report(results)
    return 0 if all(ok for _, ok, _ in results) else 1


def _report(results: list[tuple[str, bool, str]]) -> None:
    for name, ok, detail in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name} — {detail}", file=sys.stderr)


async def _serve() -> int:
    try:
        cfg = load_config()
    except ConfigError as exc:
        _print_config_error(exc)
        return 2
    configure_logging()

    # MCP runtime imported lazily (optional extra), mirroring build_server().
    from mcp.server.stdio import stdio_server

    from june_mcp.server import build_server

    client = make_client(cfg)
    # Canvas resolution BEFORE serving (fail-closed): a name that resolves to
    # nothing / more than one canvas must stop the server here, with a clear
    # operator-facing line — never surface later as a cryptic 404 to the agent.
    try:
        resolved, how = resolve_canvas(client, cfg.canvas, create=cfg.canvas_create)
    except CanvasResolutionError as exc:
        print(f"june-mcp: {exc}", file=sys.stderr)
        client.close()
        return 2
    except Exception as exc:
        import httpx
        hint = ""
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
            hint = (" (this endpoint may predate /v1/canvases — set JUNE_CANVAS "
                    "to a canvas id instead of a name)")
        print(f"june-mcp: canvas resolution failed — {map_error(exc)}{hint}",
              file=sys.stderr)
        client.close()
        return 2
    client.canvas = resolved

    # Connection banner (stderr — stdout is the MCP wire). The edition tag comes
    # from the SERVICE's own /v1/whoami; display-only, absent on older services.
    tag = ""
    # Agent page-authoring (june_page_create/write/append) is Pro. Resolve the tier from the
    # service's /v1/whoami. FAIL-OPEN: an unreachable or legacy whoami must never lock a paying
    # user out of a feature they bought — only an EXPLICIT non-Pro tier gates the write verbs.
    pro = True
    try:
        who = client.whoami()
        tag = str(who.get("edition_tag") or "").strip()
        tier = str(who.get("tier") or "").strip().lower()
        if tier and tier != "pro":
            pro = False
    except Exception:
        tag = ""
    print(f"june-mcp: connected {cfg.base_url} canvas {how}"
          + (f" [{tag}]" if tag else "")
          + (" (read-only)" if cfg.readonly else "")
          + ("" if pro else " · agent page-authoring: Pro only"), file=sys.stderr)

    server = build_server(client, readonly=cfg.readonly, pro=pro)
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream,
                             server.create_initialization_options())
    finally:
        client.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="june-mcp",
        description="June MCP server (stdio) — see module docstring for modes.")
    parser.add_argument("--manifest", action="store_true",
                        help="print the tool manifest as JSON and exit")
    parser.add_argument("--doctor", action="store_true",
                        help="verify config + connectivity, then exit")
    args = parser.parse_args(argv)

    if args.manifest:
        return _manifest()
    if args.doctor:
        try:
            return _doctor()
        except ConfigError as exc:  # pragma: no cover - doctor handles its own
            _print_config_error(exc)
            return 2
    try:
        return asyncio.run(_serve())
    except KeyboardInterrupt:  # clean host-initiated shutdown
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
