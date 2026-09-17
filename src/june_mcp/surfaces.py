"""Surfaces: what ``tools/list`` sends for a profile, GENERATED from the registry (design §8.2).

* ``full`` and ``lean`` are the identity mapping over ``visible_tools`` — one SurfaceTool per Tool.
* ``compact`` (B-prime, design §7.3) groups members by ``Tool.family`` into seven family tools —
  ``june_graph``, ``june_maintain``, ``june_page_read``, ``june_page_edit``, ``june_canvas_read``,
  ``june_canvas_erase``, ``june_docs_read`` — and keeps the other tools single, so a full/Pro/rw
  connection lists 20 tools instead of 30. Every family has ONE effect class (R1), the same gates
  (R2), the same canvas rule (R3) and the same result decoration (R4); flagship verbs stay alone
  (R5); the family name never reuses a member name, and the op is the member name minus the
  family's noun — ``june_page_read(op='get')`` — the spelling §8.4 of the design uses (R7 as applied).

A family tool's schema is an object with ``op`` (an enum of ONLY the member ops visible in this
posture) plus the union of the members' arguments; arguments that belong to some ops only say so.
Its description is a one-line summary followed by each member's description, unchanged, prefixed
``op='…'`` — no teaching is lost by merging. Two compact-only levers (§7.5): W1a, the per-call
``canvas`` argument carries a short note instead of the full one on every tool; W1b, the page
block grammar (tables, diagrams, live views, media, controls, styling, layout) moves out of the
create description into ``june_page_read(op='grammar')``, fetched on demand.

Calls are dispatched back to ``run_tool(member_name, …)`` — the same chokepoint as ``full`` — so
every gate, canvas resolution, clamp, receipt, digest rule and confirm-token binding runs exactly
as today, keyed by the member name.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from june_mcp.runtime import ToolInputError
from june_mcp.tools import Tool, _BY_NAME, visible_tools

PROFILES = ("full", "lean", "compact")

# family → (tool name, title, one-line summary)
FAMILIES: dict[str, tuple[str, str, str]] = {
    "graph":        ("june_graph", "Graph neighbourhood",
                     "Read the edges around a known node: op='neighborhood' for the 1-hop edges, "
                     "op='subgraph' for a bounded multi-hop expansion. Both need node_id + node_type "
                     "from a prior result."),
    "maintain":     ("june_maintain", "Graph maintenance",
                     "Write derived facts into the graph: op='enrich' re-extracts with the Pro engines, "
                     "op='resolve' merges duplicate entities."),
    "page_read":    ("june_page_read", "Read pages",
                     "Read pages in a canvas: op='list' lists them, op='get' returns one page with its "
                     "ordered blocks (call it in the same turn before any edit), op='grammar' returns the "
                     "full block grammar for composing rich pages (tables, diagrams, live views, media, "
                     "controls, styling, layout) — fetch it once before building anything beyond plain text."),
    "page_edit":    ("june_page_edit", "Create or add to pages",
                     "Page writes that CANNOT remove anything: op='create' makes a new page, op='append' "
                     "adds blocks to the end of a page, op='update' edits named existing blocks in place. "
                     "For rich content fetch june_page_read(op='grammar') first. Replacing a page's blocks "
                     "is a different tool (june_page_write) because it can remove content."),
    "canvas_read":  ("june_canvas_read", "Canvases",
                     "See canvases: op='list' every canvas, op='current' the connection's immutable default, "
                     "op='use' resolve a name/id to a canvas_handle for per-call targeting (nothing is "
                     "switched; CX3)."),
    "canvas_erase": ("june_canvas_erase", "Erase a canvas",
                     "IRREVERSIBLE two-phase canvas operations: op='clear' erases every node and edge, "
                     "op='delete' removes the canvas itself. The first call returns a confirm_token and "
                     "a next_call; only the second call with that exact confirm executes."),
    "docs_read":    ("june_docs_read", "Standing docs",
                     "Read the agent's standing docs: op='refresh' the digest of pinned docs and skills "
                     "(once per session), op='list' every doc, op='get' one doc's full body."),
}
_FAMILY_TOOL_NAMES = {v[0] for v in FAMILIES.values()}

# member tool → its op on the family tool. R7 as applied (design §8.4's own example is
# june_page_read(op='get')): the op is the member name minus the family's noun, so a model reads
# op='get' on june_page_read rather than op='page_get'. Confirm tokens already bind to 'clear' /
# 'delete'. A test asserts every family member has exactly one op and no two collide.
OPS: dict[str, str] = {
    "june_neighborhood": "neighborhood", "june_subgraph": "subgraph",
    "june_enrich": "enrich", "june_resolve": "resolve",
    "june_page_list": "list", "june_page_get": "get",
    "june_page_create": "create", "june_page_append": "append", "june_page_update": "update",
    "june_canvas_list": "list", "june_canvas_current": "current", "june_canvas_use": "use",
    "june_canvas_clear": "clear", "june_canvas_delete": "delete",
    "june_docs_refresh": "refresh", "june_doc_list": "list", "june_doc_get": "get",
}

# W1a: the shared per-call canvas note, short. Full keeps the long one (byte-identical).
SHORT_CANVAS_DOC = ("Canvas for THIS call only (name | id | canvas_handle). Omitted = the connection's "
                    "immutable default. Nothing is remembered between calls.")
SHORT_DOCS_CANVAS_DOC = ("Canvas holding the agent docs, for THIS call only. Omitted = the configured "
                         "docs canvas (JUNE_DOCS_CANVAS).")

# W1b: the page grammar lives in june_page_create's description on full. Split it out here by its
# structure (the bullets from DIAGRAM to the layout sentence) so full stays the literal text and
# compact serves the same words on demand. The one-line TABLE rule stays inline: it is the block a
# model reaches for most, and the first compact gate run (2026-09-17) showed what happens when it is
# only on demand — Claude guessed {type:'table', headers, rows}, got a coerced paragraph, THEN read
# the grammar and rewrote the page with june_page_write (3/3 runs of page_create-2, a forbidden op
# for a fresh create). So the note also says to read the grammar BEFORE composing, and why.
# A test asserts short + grammar == the literal.
_PC = _BY_NAME["june_page_create"].description
_G0, _G1 = _PC.index("• DIAGRAM"), _PC.index("Returns {page_id, title, blocks_written")
PAGE_GRAMMAR = _PC[_G0:_G1].rstrip()
PAGE_CREATE_SHORT = (_PC[:_G0].rstrip() + "\n(Every other block kind — DIAGRAM/CHART, LIVE VIEW, MEDIA, "
                     "ILLUSTRATION, INTERACTIVE CONTROLS, PROGRESS/DATES/BUTTONS — plus inline markdown, "
                     "STYLING, the MASTHEAD and `layout` has an exact shape: call june_page_read(op='grammar') "
                     "once BEFORE composing such a page and follow it. A guessed shape is not rejected — it "
                     "is downgraded to plain text — so read first rather than rewrite the page after.)\n"
                     + _PC[_G1:])


@dataclass(frozen=True)
class SurfaceTool:
    name: str
    title: str
    description: str
    input_schema: dict
    annotations: dict
    members: tuple[str, ...]              # member tool names; (name,) for a single tool
    ops: dict[str, str] = field(default_factory=dict)   # op → member name (families only)
    writes: bool = False

    @property
    def is_family(self) -> bool:
        return bool(self.ops)


def _short_canvas(schema: dict, docs_tool: bool) -> dict:
    props = dict(schema.get("properties") or {})
    if "canvas" in props:
        props = {**props, "canvas": {"type": "string",
                                     "description": SHORT_DOCS_CANVAS_DOC if docs_tool else SHORT_CANVAS_DOC}}
    return {**schema, "properties": props}


def _single(t: Tool, *, compact: bool) -> SurfaceTool:
    schema = t.input_schema
    description = t.description
    if compact:
        schema = _short_canvas(schema, t.docs_tool)
    return SurfaceTool(name=t.name, title=t.title, description=description, input_schema=schema,
                       annotations=t.annotations, members=(t.name,), writes=t.writes)


def _family(family: str, members: list[Tool]) -> SurfaceTool:
    name, title, summary = FAMILIES[family]
    ops = {OPS[m.name]: m.name for m in members}
    # union of arguments; an argument used by a subset of ops says which
    props: dict[str, dict] = {}
    owners: dict[str, set[str]] = {}
    required_by_op: dict[str, list[str]] = {}
    for m in members:
        sch = _short_canvas(m.input_schema, m.docs_tool)
        for k, v in (sch.get("properties") or {}).items():
            props.setdefault(k, dict(v))
            owners.setdefault(k, set()).add(OPS[m.name])
        required_by_op[OPS[m.name]] = list(sch.get("required") or [])
    for k, v in props.items():
        if owners[k] != set(ops):
            prefix = f"(ops: {', '.join(sorted(owners[k]))}) "
            v["description"] = prefix + str(v.get("description") or "")
    op_lines = []
    for op, mname in ops.items():
        req = required_by_op[op]
        op_lines.append(f"'{op}'" + (f" (requires {', '.join(req)})" if req else ""))
    schema = {"type": "object",
              "properties": {"op": {"type": "string", "enum": list(ops),
                                    "description": "which operation: " + "; ".join(op_lines)},
                             **props},
              "required": ["op"]}
    desc_parts = [summary] + [f"op='{OPS[m.name]}': {PAGE_CREATE_SHORT if m.name == 'june_page_create' else m.description}"
                              for m in members]
    if family == "page_read":
        desc_parts.append("op='grammar': returns the block grammar as text (no arguments).")
        schema["properties"]["op"]["enum"] = list(ops) + ["grammar"]
        schema["properties"]["op"]["description"] += "; 'grammar' (no arguments)"
    first = members[0]
    ann = {**first.annotations, "title": title}
    return SurfaceTool(name=name, title=title, description="\n".join(desc_parts), input_schema=schema,
                       annotations=ann, members=tuple(m.name for m in members), ops=ops,
                       writes=any(m.writes for m in members))


def build_surface(profile: str = "full", *, readonly: bool = False, pro: bool = True,
                  absent: Iterable[str] = ()) -> list[SurfaceTool]:
    """The ordered tool list for a posture. Deterministic (C3): registry order, a family placed
    where its first visible member sits."""
    if profile not in PROFILES:
        raise KeyError(f"unknown tool profile {profile!r}; known: {list(PROFILES)}")
    base = "full" if profile == "compact" else profile
    vis = visible_tools(readonly=readonly, pro=pro, profile=base, absent=frozenset(absent))
    if profile != "compact":
        return [_single(t, compact=False) for t in vis]
    out: list[SurfaceTool] = []
    seen: set[str] = set()
    for t in vis:
        if t.family is None:
            out.append(_single(t, compact=True))
        elif t.family not in seen:
            seen.add(t.family)
            out.append(_family(t.family, [m for m in vis if m.family == t.family]))
    return out


def surface_names(surface: list[SurfaceTool]) -> set[str]:
    return {s.name for s in surface}


def display_name(surface: list[SurfaceTool]):
    """A formatter: member tool name → how it is called on this surface."""
    member_to = {}
    for s in surface:
        for op, m in s.ops.items():
            member_to[m] = f"{s.name}(op='{op}')"
    return lambda name: member_to.get(name, name)


def alias_lines(surface: list[SurfaceTool]) -> str:
    """The alias map for the standing-docs digest on compact (old names persist in users' docs)."""
    pairs = []
    for s in surface:
        for op, m in s.ops.items():
            pairs.append(f"{m} → {s.name} op={op}")
    return "; ".join(pairs)


def resolve_call(surface: list[SurfaceTool], name: str, args: dict | None) -> tuple[str, dict]:
    """Surface fence + op check (design §8.3). Returns (member tool name, args without `op`)."""
    by = {s.name: s for s in surface}
    a = dict(args or {})
    if name not in by:
        member = _BY_NAME.get(name)
        if member is None:
            raise ToolInputError(f"unknown tool {name!r} on this surface; valid: {', '.join(sorted(by))}")
        if member.family is not None and FAMILIES[member.family][0] in by:
            fam = FAMILIES[member.family][0]
            raise ToolInputError(f"{name} is not a tool on this surface (compact): call {fam} "
                                 f"with op='{OPS[member.name]}'.")
        # A real tool that is hidden here: let run_tool's fence chain say WHY (read-only, Pro,
        # not served by this engine, opt-in, profile) — those messages are pinned by tests.
        return name, a
    st = by[name]
    if not st.is_family:
        return name, a
    op = str(a.pop("op", "") or "").strip()
    valid = "; ".join(f"'{o}'" + (f" requires {', '.join(_BY_NAME[m].input_schema.get('required') or [])}"
                                  if _BY_NAME[m].input_schema.get("required") else "")
                      for o, m in st.ops.items())
    if name == "june_page_read" and op == "grammar":
        return "__grammar__", {}
    if op not in st.ops:
        raise ToolInputError(f"{name} needs op = one of {list(st.ops)}"
                             + (" or 'grammar'" if name == "june_page_read" else "")
                             + f" (got {op!r}). Ops: {valid}")
    member = _BY_NAME[st.ops[op]]
    req = [k for k in (member.input_schema.get("required") or []) if k not in a]
    if req:
        raise ToolInputError(f"{name} op='{op}' requires {', '.join(req)}. Ops: {valid}")
    allowed = set(member.input_schema.get("properties") or {}) | {"canvas"}
    stray = sorted(k for k in a if k not in allowed and k not in st.input_schema["properties"])
    if stray:
        raise ToolInputError(f"{name} op='{op}' does not take {', '.join(stray)}. Ops: {valid}")
    return member.name, a


__all__ = ["FAMILIES", "OPS", "PAGE_CREATE_SHORT", "PAGE_GRAMMAR", "PROFILES", "SurfaceTool", "alias_lines",
           "build_surface", "display_name", "resolve_call", "surface_names"]
