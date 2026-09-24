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
import os
import time
from typing import Any

from june_client import JuneClient
from june_mcp.prompts import PROMPTS, render_prompt
from june_mcp.runtime import DEFAULT_TOOL_CONCURRENCY, map_error
from june_mcp.surfaces import (PAGE_GRAMMAR, alias_lines, build_surface, display_name,
                               ignored_args, resolve_call, respell_guidance)
from june_mcp.tools import TOOLS, run_tool, visible_tools

_ALL_TOOL_NAMES = frozenset(t.name for t in TOOLS)

log = logging.getLogger("june_mcp")


def _pro_grace() -> bool:
    """B4: hold Pro for ONE refresh interval when the tier reports free. Default OFF.

    Deliberately behaviour-changing, which is why it ships at 0. With it on, a connection
    that has genuinely lapsed keeps the Pro verbs for up to one interval — a real, if
    bounded, over-grant. The case it exists for is the opposite error: a single
    /v1/whoami that answers "free" during a billing write, a token refresh or a replica
    lag strips a paying user's tools mid-session, and the tools reappear an interval later
    with nothing to explain the gap. B1 already fails open on an unreachable whoami; this
    covers the whoami that answers, and answers wrong.

    The asymmetry is the point: a DOWNGRADE waits, an UPGRADE never does. Delaying a tier
    someone has just paid for has no failure mode worth protecting against.
    """
    return os.environ.get("JUNE_PRO_GRACE", "").strip().lower() in ("1", "true", "yes", "on")


def grace_decision(*, current_pro: bool, reported_pro: bool, held: bool,
                   grace: bool) -> tuple[bool, bool]:
    """B4's state machine, pure: ``(apply_now, still_held)``.

    Extracted from the refresh closure deliberately. That closure needs the mcp runtime,
    so it carries `# pragma: no cover` and no test reaches it — and a rule that decides
    when a paying user loses their tools is not a rule to leave untested. The closure
    keeps the I/O; this owns the decision.

    HOLD only on the first free reading after Pro, and only with grace on. Everything
    else applies immediately: a confirmed second free reading, a recovery back to Pro, and
    every upgrade. A held state that never clears would strand a real downgrade forever.
    """
    if grace and current_pro and not reported_pro and not held:
        return (False, True)
    return (True, False)


def _refresh_secs() -> float:
    """B1: how often to re-resolve the tier, in seconds. 0 — the DEFAULT — means never,
    which is 0.4.3's exact behaviour: the surface is fixed for the life of the process.
    Opt in and a tier bought mid-session becomes visible without replacing the connector."""
    try:
        return max(0.0, float(os.environ.get("JUNE_SURFACE_REFRESH_SECS", "0")))
    except ValueError:
        return 0.0


class _ProDerived:
    """Everything that depends on ``pro``, recomputed as ONE act (B1).

    ``pro`` is resolved from ``/v1/whoami`` at startup, and five things hang off it: the
    advertised ``surface``, the display names, the compact alias map, the listed prompts,
    and the ``pro=`` handed to ``run_tool``. Recomputing them separately is how they come
    to disagree — a tool listed but not callable, or callable but not listed — so they are
    rebuilt together or not at all.

    Why a holder rather than the 0.4.2 build-once locals: a tier bought DURING a session
    was invisible until the process was replaced, because the surface was frozen at
    startup. Reads are one attribute lookup, so the CX8 hot path is unchanged; the rebuild
    happens only when ``pro`` actually flips.
    """

    __slots__ = ("pro", "surface", "disp", "aliases", "prompts", "_fixed")

    def __init__(self, *, profile: str, readonly: bool, absent, lean: bool,
                 compact: bool, pro: bool) -> None:
        self._fixed = (profile, readonly, absent, lean, compact)
        self.pro = None
        self.set_pro(pro)

    def set_pro(self, pro: bool) -> bool:
        """Adopt a new tier. True when the surface actually changed (so a caller knows
        whether a ``tools/list_changed`` notification is owed)."""
        pro = bool(pro)
        if pro == self.pro:
            return False
        profile, readonly, absent, lean, compact = self._fixed
        self.pro = pro
        self.surface = build_surface(profile, readonly=readonly, pro=pro, absent=absent)
        self.disp = display_name(self.surface)
        self.aliases = alias_lines(self.surface) if compact else ""
        self.prompts = prompts_for(self.surface, readonly=readonly, lean=lean)
        return True


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
    compact = profile == "compact"
    # The standing-docs digest carries the alias map on compact (old member names persist in
    # users' docs); on any other profile it carries none. N13: this belongs to THIS server, so it
    # is computed once here and passed into every run_tool call — never parked in module state,
    # where two servers in one process would overwrite each other's map.
    derived = _ProDerived(profile=profile, readonly=readonly, absent=absent,
                          lean=lean, compact=compact, pro=pro)
    limiter = anyio.CapacityLimiter(max(1, int(tool_concurrency)))  # CX8 ceiling
    # A prompt is listed only when every tool it drives is on this surface (N4 for prompts): the
    # page prompts need Pro page authoring, the memory prompts need the docs tools, none of them
    # make sense read-only or on lean, and a no-pages engine has neither.

    @server.list_tools()
    async def _list() -> list:  # pragma: no cover - needs mcp runtime
        # 0.4.2: title + annotations derived from the registry (design §8.4). Without them the
        # MCP defaults present every tool as non-read-only AND destructive.
        return [McpTool(name=t.name, title=t.title or None, description=t.description,
                        inputSchema=t.input_schema,
                        annotations=ToolAnnotations(**t.annotations))
                for t in derived.surface]

    @server.list_prompts()
    async def _list_prompts() -> list:  # pragma: no cover - needs mcp runtime
        return [McpPrompt(
            name=p.name, description=p.description,
            arguments=[PromptArgument(name=a.name, description=a.description,
                                      required=a.required) for a in p.arguments])
            for p in derived.prompts]

    @server.get_prompt()
    async def _get_prompt(name: str, arguments: dict | None):  # pragma: no cover
        if readonly or lean or name not in {p.name for p in derived.prompts}:
            raise KeyError(f"unknown prompt {name!r}")
        text = render_prompt(name, arguments or {})
        if compact:
            text = respell_guidance(text, derived.disp)   # "compose it with june_page_edit(op='create')"
        return GetPromptResult(
            description=f"June: {name}",
            messages=[PromptMessage(role="user",
                                    content=TextContent(type="text", text=text))])

    _tier_checked = [0.0]
    _demotion_held = [False]   # B4: a pro->free reading is pending a second opinion

    async def _maybe_refresh_tier() -> None:  # pragma: no cover - needs mcp runtime
        """B1+B2: re-resolve the tier on a cadence, and tell the host when the surface moved.

        Fail-open and fail-quiet, in that order. An unreachable or legacy ``/v1/whoami`` must
        never lock a paying user out (D1) and must never break the tool call it precedes, so
        every failure here is swallowed and the previous surface stands. A host that ignores
        ``tools/list_changed`` is no worse off than before this existed.

        Default OFF. The surface is read on the CX8 hot path, so opting in is a deliberate
        act: ``JUNE_SURFACE_REFRESH_SECS``.
        """
        secs = _refresh_secs()
        if secs <= 0:
            return
        now = time.monotonic()
        if now - _tier_checked[0] < secs:
            return
        _tier_checked[0] = now                      # stamp BEFORE the probe: a slow or failing
        try:                                        # whoami must not retry on every call
            from june_mcp.capabilities import resolve as _resolve_caps
            caps = await anyio.to_thread.run_sync(
                functools.partial(_resolve_caps, client), limiter=limiter)
        except Exception:
            return
        new_pro = bool(caps.pro)
        apply_now, _demotion_held[0] = grace_decision(
            current_pro=bool(derived.pro), reported_pro=new_pro,
            held=_demotion_held[0], grace=_pro_grace())
        if not apply_now:
            # Logged at INFO, not debug: a capability that is ABOUT to disappear is exactly
            # what an operator needs in the record when it later does.
            log.info("tier reported free; holding Pro for one refresh interval "
                     "(JUNE_PRO_GRACE). A second free reading will apply the downgrade.")
            return
        if not derived.set_pro(new_pro):
            return
        try:
            await server.request_context.session.send_tool_list_changed()
        except Exception:
            # The new surface is already live for resolve_call; only the host's cached LIST
            # is stale, and that is exactly the condition this notification exists to fix.
            log.warning("tier changed to pro=%s but tools/list_changed could not be sent; "
                         "the host may serve a stale tool list until it reconnects", derived.pro)

    @server.call_tool()
    async def _call(tool_name: str, arguments: dict[str, Any] | None):  # pragma: no cover
        try:
            await _maybe_refresh_tier()
            # Surface fence + op check (design §8.3): on compact a family call becomes its member
            # call; on full/lean the name passes through. Then the SAME chokepoint as always.
            ignored = ignored_args(derived.surface, tool_name, arguments or {})
            member, args = resolve_call(derived.surface, tool_name, arguments or {})
            if member == "__grammar__":
                return [TextContent(type="text", text=json.dumps({"grammar": PAGE_GRAMMAR}))]
            # CX8: run the sync tool in a worker thread, bounded by the limiter —
            # the event loop keeps reading the stream (overlap, liveness) while
            # at most `tool_concurrency` tools execute.
            result = await anyio.to_thread.run_sync(
                functools.partial(run_tool, member, client, args,
                                  readonly=readonly, pro=derived.pro, strict=strict,
                                  profile=profile, absent=absent, aliases=derived.aliases),
                limiter=limiter)
            if compact and member != tool_name and isinstance(result, dict):
                result = _tag_op(result, tool_name, member, derived.surface, ignored)
            if compact:
                # Connector-authored guidance in the result ("read it first with june_page_get",
                # "call june_canvas_use again", the digest's note) is spelled for this surface.
                result = respell_guidance(result, derived.disp)
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
            err = map_error(exc)
            if compact:
                err = respell_guidance(err, derived.disp)
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps({"error": err}))],
                isError=True)
        return [TextContent(type="text", text=json.dumps(result, default=str))]

    return server


# Opt-in tools a prompt may mention conditionally ("if JUNE_EXPORT_ROOT is set …"); their absence
# does not make the prompt useless.
_OPT_IN_TOOLS = frozenset({"june_docs_export", "june_page_export", "june_page_import", "june_ingest_file"})


def prompts_for(surface, *, readonly: bool = False, lean: bool = False) -> list:
    """The MCP prompts this posture may advertise: each one drives specific tools, and a prompt
    whose tools are not callable here (free tier, no-pages engine, read-only, lean) is not
    listed — a host would otherwise surface a starter that can only fail."""
    if readonly or lean:
        return []
    import re as _re
    on_surface = {m for st in surface for m in st.members}
    out = []
    for p in PROMPTS:
        text = render_prompt(p.name, {a.name: "x" for a in p.arguments})
        needs = {m for m in _re.findall(r"june_[a-z_]+", text) if m in _ALL_TOOL_NAMES} - _OPT_IN_TOOLS
        if needs <= on_surface:
            out.append(p)
    return out


def _tag_op(result: dict, family: str, member: str, surface,
            ignored: list[str] | tuple[str, ...] = ()) -> dict:
    """Results of a family call carry the op they ran (§8.3), and a two-phase `next_call` is
    respelled for this surface so the second call is copy-pasteable as-is. Arguments the op did
    not take are named (FX N14) under ``_notes.ignored_arguments``."""
    fam = next(s for s in surface if s.name == family)
    op = next(o for o, m in fam.ops.items() if m == member)
    out = {**result, "op": op}
    if ignored:
        notes = dict(out.get("_notes") or {})
        notes["ignored_arguments"] = (f"op='{op}' does not take {', '.join(ignored)}; "
                                      "they were NOT applied (another op of this tool takes them)")
        out["_notes"] = notes
    nc = out.get("next_call")
    if isinstance(nc, dict) and nc.get("tool") == member:
        out["next_call"] = {"tool": family, "arguments": {"op": op, **(nc.get("arguments") or {})}}
    # The human-readable warning names the member ("call june_canvas_clear again …"); on this
    # surface that name is not callable, so spell it the way it is called here.
    if isinstance(out.get("warning"), str) and member in out["warning"]:
        out["warning"] = out["warning"].replace(member, f"{family}(op='{op}')")
    return out


def instructions_for(*, readonly: bool = False, pro: bool = True, profile: str = "full",
                     absent: frozenset[str] | set[str] = frozenset()) -> str:
    """The server instructions a connection receives at the handshake for this posture — generated
    from the paragraphs that teach tools actually ON this surface (N4: never teach what a
    connection cannot call). On compact every member name is respelled as its family call, the
    D9 rewordings apply, and the alias map for persisted docs is appended."""
    from june_mcp.instructions import COMPACT_REWRITES, render
    base = "full" if profile == "compact" else profile
    names = [t.name for t in visible_tools(readonly=readonly, pro=pro, profile=base, absent=absent)]
    if profile != "compact":
        return render(names, profile=profile)
    surface = build_surface("compact", readonly=readonly, pro=pro, absent=absent)
    text = render(names, profile="full", display=display_name(surface), rewrites=COMPACT_REWRITES,
                  extra=("Older docs and notes may name the member tools directly; on this connection "
                         "they map as: " + alias_lines(surface) + "."))
    return text


def tool_manifest(*, readonly: bool = False, pro: bool = True, profile: str = "full",
                  absent: frozenset[str] | set[str] = frozenset()) -> list[dict]:
    """The tool list as plain dicts (name/title/description/schema/annotations) — handy for docs,
    a capabilities endpoint, or asserting the surface in tests without the mcp runtime. This is
    exactly what ``tools/list`` sends, minus the wire envelope."""
    return [{"name": t.name, "title": t.title, "description": t.description,
             "input_schema": t.input_schema, "annotations": t.annotations, "writes": t.writes,
             "members": list(t.members), "ops": dict(t.ops)}
            for t in build_surface(profile, readonly=readonly, pro=pro, absent=frozenset(absent))]


__all__ = ["build_server", "instructions_for", "prompts_for", "tool_manifest"]
