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
            client = client.for_canvas(resolved)   # CX3: derive, never mutate
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


def _install_instructions(target: str) -> int:
    """``--install-instructions [file]`` — close the cold-start gap host-side.

    Writes prompts.HOST_INSTRUCTIONS as a MANAGED SECTION into the instruction
    file the host loads natively every session (CLAUDE.md by default; pass
    AGENTS.md for agents that read that instead), fenced under JUNE_EXPORT_ROOT.
    Only the marked region is ever touched; re-runs update it in place, so a
    new june-mcp version can refresh its own integration text. Pure file op —
    needs no June connection, no API key. Exit 0 ok · 2 config/refused."""
    from june_mcp import export as export_mod
    from june_mcp.prompts import HOST_INSTRUCTIONS

    root = export_mod.export_root()
    if root is None or not root.is_dir():
        print(f"june-mcp: {export_mod.ENV_EXPORT_ROOT} must be set to the project "
              "directory (the file is written inside that fence)", file=sys.stderr)
        return 2
    try:
        path = export_mod.fenced(root, target)
        existing = path.read_text(encoding="utf-8") if path.exists() else None
        new_text = export_mod.section_replace(existing, "june-integration",
                                              HOST_INSTRUCTIONS)
    except ValueError as exc:
        print(f"june-mcp: refused — {exc}", file=sys.stderr)
        return 2
    if existing == new_text:
        print(f"june-mcp: {target} already carries the current June section — "
              "nothing to do", file=sys.stderr)
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text, encoding="utf-8")
    print(f"june-mcp: {'updated' if existing is not None else 'created'} the "
          f"june-integration section in {target} — every session that loads this "
          "file now uses June by default", file=sys.stderr)
    return 0


def _export_mode(check: bool) -> int:
    """``--export`` / ``--export-check`` — the repo-sync CI seam (Phase AM2).

    Runs the SAME tool code paths an agent uses (no parallel implementation to
    drift): a full agent-docs export, then a re-export of every page/section the
    manifest says is managed. ``--export-check`` writes nothing and exits 1 on
    drift, so CI can prove "the repo matches June" the same way a parity gate
    proves a build. Exit: 0 current/exported · 1 drift or refusals · 2 config."""
    from june_mcp import export as export_mod

    try:
        cfg = load_config()
    except ConfigError as exc:
        _print_config_error(exc)
        return 2
    configure_logging()
    root = export_mod.export_root()
    if root is None or not root.is_dir():
        print(f"june-mcp: {export_mod.ENV_EXPORT_ROOT} must be set to an existing "
              "directory for export mode", file=sys.stderr)
        return 2

    from june_mcp.tools import run_tool

    client = make_client(cfg)
    try:
        try:
            resolved, _how = resolve_canvas(client, cfg.canvas, create=False)
        except Exception as exc:  # noqa: BLE001 — same operator-facing exit as serve
            print(f"june-mcp: canvas resolution failed — {map_error(exc)}", file=sys.stderr)
            return 2
        client = client.for_canvas(resolved)

        problems: list[str] = []
        args = {"check": True} if check else {}
        out = run_tool("june_docs_export", client, dict(args))
        for r in out.get("refused") or []:
            problems.append(f"refused: {r}")
        for d in out.get("drift") or []:
            problems.append(f"drift: {d['path']} ({d['status']})")
        wrote = list(out.get("written") or [])

        # Re-export every managed page/section the manifest records — the
        # manifest is what makes "everything relevant" enumerable.
        manifest = export_mod.manifest_load(root)
        for rel, entry in sorted(manifest.get("files", {}).items()):
            if entry.get("kind") != "page":
                continue                     # agent docs were covered above
            pargs: dict = {"page_id": entry.get("page_id", ""), "path": rel, **args}
            if entry.get("section"):
                pargs["section"] = entry["section"]
            if entry.get("canvas"):
                pargs["canvas"] = entry["canvas"]
            try:
                pres = run_tool("june_page_export", client, pargs)
            except Exception as exc:  # noqa: BLE001 — one bad entry must not hide the rest
                problems.append(f"error: {rel} — {map_error(exc)}")
                continue
            if pres.get("refused"):
                problems.append(f"refused: {rel} — {pres.get('message', '')[:120]}")
            elif check and pres.get("status") != "unchanged":
                problems.append(f"drift: {rel} ({pres.get('status')})")
            elif pres.get("status") != "unchanged":
                wrote.append(rel)

        mode = "check" if check else "export"
        for p in problems:
            print(f"june-mcp {mode}: {p}", file=sys.stderr)
        print(f"june-mcp {mode}: {len(wrote)} written, {len(problems)} problem(s)"
              + (f" — {out.get('git')}" if out.get("git") else ""), file=sys.stderr)
        return 1 if problems else 0
    finally:
        client.close()


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
    # CX3: the default canvas is IMMUTABLE — derive the serving client from the
    # resolved id instead of mutating (JuneClient.canvas raises on assignment),
    # and seed the display-name memo so results can echo the name traffic-free.
    client = client.for_canvas(resolved)
    import uuid as _uuid_mod

    from june_mcp.tools import note_canvas_name
    try:
        _uuid_mod.UUID(cfg.canvas)
    except ValueError:
        note_canvas_name(resolved, cfg.canvas)     # JUNE_CANVAS carried a NAME

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
    # Phase AM — agent memory. The doc tools always work; this switches on the
    # periodic standing_docs digest (the anti-forgetting layer) with the
    # validated env knobs. Library/test callers who never run __main__ stay at
    # the module default (injection off) unless they call configure_docs.
    from june_mcp.tools import configure_docs
    configure_docs(enabled=cfg.docs_refresh, canvas=cfg.docs_canvas,
                   calls=cfg.docs_refresh_calls, minutes=cfg.docs_refresh_minutes,
                   digest_chars=cfg.docs_digest_chars)

    print(f"june-mcp: connected {cfg.base_url} canvas {how}"
          + (f" [{tag}]" if tag else "")
          + (" (read-only)" if cfg.readonly else "")
          + ("" if pro else " · agent page-authoring: Pro only")
          + (f" · agent docs: {cfg.docs_canvas!r} (digest every "
             f"{cfg.docs_refresh_calls} calls / {cfg.docs_refresh_minutes:g} min)"
             if cfg.docs_refresh else " · agent docs digest: off"), file=sys.stderr)

    server = build_server(client, readonly=cfg.readonly, pro=pro,
                          strict=cfg.canvas_strict,
                          tool_concurrency=cfg.tool_concurrency)
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
    parser.add_argument("--export", action="store_true",
                        help="sync agent docs + managed pages to JUNE_EXPORT_ROOT, then exit")
    parser.add_argument("--export-check", action="store_true",
                        help="CI drift gate: exit 1 if the repo diverges from June (writes nothing)")
    parser.add_argument("--install-instructions", nargs="?", const="CLAUDE.md",
                        metavar="FILE",
                        help="write June's standing instructions as a managed section into "
                             "FILE (default CLAUDE.md) under JUNE_EXPORT_ROOT — the host "
                             "then loads them into every session automatically")
    args = parser.parse_args(argv)

    if args.manifest:
        return _manifest()
    if args.install_instructions:
        return _install_instructions(args.install_instructions)
    if args.export or args.export_check:
        return _export_mode(check=args.export_check)
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
