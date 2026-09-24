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

import re

from june_mcp.runtime import ToolInputError
from june_mcp.tools import TOOLS, Tool, _BY_NAME, visible_tools

PROFILES = ("full", "lean", "compact")
_NAME_RE = re.compile(r"june_[a-z_]+")

# family → (tool name, title, one-line summary)
FAMILIES: dict[str, tuple[str, str, str]] = {
    "graph":        ("june_graph", "Graph neighbourhood",
                     "Read the edges around a known node: op='neighborhood' for the 1-hop edges, "
                     "op='subgraph' for a bounded multi-hop expansion, op='backlinks' for what points "
                     "AT it (e.g. the pages that mention it). All need node_id + node_type from a "
                     "prior result."),
    "maintain":     ("june_maintain", "Graph maintenance",
                     "Write derived facts into the graph: op='enrich' re-extracts with the Pro engines, "
                     "op='resolve' merges duplicate entities."),
    "page_read":    ("june_page_read", "Read pages",
                     "Read pages in a canvas: op='list' lists them, op='get' returns one page with its "
                     "ordered blocks (call it in the same turn before any edit), op='removed' lists "
                     "what a page has lost (for op='restore')."),
    "page_edit":    ("june_page_edit", "Create or add to pages",
                     "Page writes that CANNOT remove anything: op='create' makes a new page, op='append' "
                     "adds blocks to the end of a page, op='insert' adds them after a named block, "
                     "op='update' edits named existing blocks in place, op='move' reorders blocks, "
                     "op='rename' retitles the page, op='meta' pins or groups it, op='restore' puts "
                     "back blocks it lost. "
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
    "june_neighborhood": "neighborhood", "june_subgraph": "subgraph", "june_backlinks": "backlinks",
    "june_enrich": "enrich", "june_resolve": "resolve",
    "june_page_list": "list", "june_page_get": "get", "june_page_removed": "removed",
    "june_page_create": "create", "june_page_append": "append", "june_page_update": "update",
    "june_page_insert": "insert", "june_page_move": "move", "june_page_rename": "rename",
    "june_page_meta": "meta", "june_page_restore": "restore",
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


_GRAMMAR_SUMMARY = (" op='grammar' returns the full block grammar for composing rich pages (tables, "
                    "diagrams, live views, media, controls, styling, layout) — fetch it once before "
                    "building anything beyond plain text and tables.")
_AUTHORING = frozenset({"june_page_create", "june_page_append", "june_page_update", "june_page_write",
                        "june_page_insert"})


def _family(family: str, members: list[Tool], *, grammar: bool = False) -> SurfaceTool:
    name, title, summary = FAMILIES[family]
    if grammar:
        summary = summary + _GRAMMAR_SUMMARY
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
    if grammar:
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
    authoring = any(t.name in _AUTHORING for t in vis)   # the grammar exists to compose pages
    for t in vis:
        if t.family is None:
            out.append(_single(t, compact=True))
        elif t.family not in seen:
            seen.add(t.family)
            out.append(_family(t.family, [m for m in vis if m.family == t.family],
                               grammar=(t.family == "page_read" and authoring)))
    # N4 applies to tool text too: member descriptions say "as june_page_create" / "after
    # june_page_get"; on this surface those names are not callable, so spell them as they are
    # called here (june_page_edit(op='create')). Names of members hidden in this posture are left
    # as they are — there is nothing to respell them to, and the fence names the tool when called.
    disp = display_name(out)
    return [_respell(st, disp) for st in out]


def _respell(st: SurfaceTool, disp) -> SurfaceTool:
    from dataclasses import replace
    sub = lambda text: _NAME_RE.sub(lambda m: m.group(0) if m.group(0).endswith("__") else disp(m.group(0)), text)
    def schema(node):
        if isinstance(node, dict):
            out = {k: (sub(v) if k == "description" and isinstance(v, str) else schema(v)) for k, v in node.items()}
            return out
        if isinstance(node, list):
            return [schema(x) for x in node]
        return node
    return replace(st, description=sub(st.description), input_schema=schema(st.input_schema))


# Result fields the CONNECTOR authors (guidance an agent reads and acts on). User content — page
# blocks, doc bodies, answers, one-liners — is never rewritten: a user's doc that says
# "june_page_get" is the user's text. The truncation marker refresh.py appends inside a doc body is
# the one connector-authored string that lives in a content field, so it is matched literally.
# N8 (S5): `tool_aliases` is NOT guidance. Its left-hand side is the OLD member name it exists to
# translate; respelling it turned "june_page_get → june_page_read(op='get')" into a map from the
# new spelling to itself. Its right-hand side is already spelled for the surface by alias_lines.
GUIDANCE_KEYS = frozenset({"note", "notes", "_notes", "warning", "hint", "refused", "reason", "error",
                           "message", "detail", "recover"})   # S8: `recover` names the undo verbs
CONTENT_KEYS = frozenset({"body", "text", "blocks", "answer", "one_liner", "when_to_use", "items",
                          "candidates", "citations", "evidence", "nodes", "edges", "pages", "docs",
                          "pinned", "skills"})
_TRUNC_RE = re.compile(r"read june_doc_get\('")


def respell_guidance(obj, disp):
    """Rewrite folded member names inside connector-authored strings of a tool result (or an error
    text) the way they are called on this surface. Content fields pass through untouched."""
    sub = lambda text: _NAME_RE.sub(lambda m: m.group(0) if m.group(0).endswith("__") else disp(m.group(0)), text)
    if isinstance(obj, str):
        return sub(obj)
    if isinstance(obj, list):
        return [respell_guidance(x, disp) for x in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in CONTENT_KEYS:
                if k == "body" and isinstance(v, str) and _TRUNC_RE.search(v):
                    v = _TRUNC_RE.sub(lambda m: "read " + disp("june_doc_get") + "('", v)
                elif isinstance(v, list) and k in ("docs", "pinned", "skills", "pages", "items"):
                    v = [respell_guidance(x, disp) if isinstance(x, dict) else x for x in v]
                out[k] = v
            elif k in GUIDANCE_KEYS and isinstance(v, str):
                out[k] = sub(v)
            elif k in GUIDANCE_KEYS and isinstance(v, dict):
                out[k] = {kk: (sub(vv) if isinstance(vv, str) else vv) for kk, vv in v.items()}
            elif k == "next_call" and isinstance(v, dict):
                out[k] = v          # already respelled structurally by the server
            else:
                out[k] = respell_guidance(v, disp) if isinstance(v, (dict, list)) else v
        return out
    return obj


def respell_text(text: str, disp) -> str:
    """Spell every folded member name in a plain string the way it is called on this surface.

    N14: ANY command that emits agent-facing text must render it through the surface, not the
    registry — `--manifest` and `--doctor` already serve `build_surface`, and
    `--install-instructions` writes HOST_INSTRUCTIONS into a repo's CLAUDE.md / AGENTS.md, where
    two of the eight tool names it mentions (june_docs_refresh, june_doc_get) are folded. Sentinels
    ending in `__` (june_sync__, __june_view__) are never tool names and are left alone.
    """
    return _NAME_RE.sub(lambda m: m.group(0) if m.group(0).endswith("__") else disp(m.group(0)),
                        text)


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
            fam_key = next((k for k, v in FAMILIES.items() if v[0] == name), None)
            if fam_key is not None:
                fam_members = [t.name for t in TOOLS if t.family == fam_key]
                if not any(m in by for m in fam_members):
                    # The family exists but every member is hidden in THIS posture (free, read-only,
                    # no-pages engine). Fall through to run_tool on one member so the agent gets the
                    # real reason ("requires June Pro", "read-only", "does not serve pages") instead
                    # of "unknown tool". Never taken on full: there the members ARE on the surface.
                    op = str(a.pop("op", "") or "").strip()
                    target = next((m for m in fam_members if OPS[m] == op), fam_members[0])
                    return target, a
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
    has_grammar = "grammar" in st.input_schema["properties"]["op"]["enum"]
    if name == "june_page_read" and op == "grammar" and has_grammar:
        return "__grammar__", {}
    if op not in st.ops:
        raise ToolInputError(f"{name} needs op = one of {list(st.ops)}"
                             + (" or 'grammar'" if has_grammar else "")
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


def ignored_args(surface: list[SurfaceTool], name: str, args: dict | None) -> list[str]:
    """FX N14: arguments a family call accepts (another op of the family takes them) but THIS op
    does not — so ``resolve_call`` lets them through and the member never reads them. They used
    to vanish without a word (``theme`` on op='append', say); the caller now gets them named.
    Not an error: the call worked, and refusing would break callers that pass extras today."""
    st = next((s for s in surface if s.name == name), None)
    if st is None or not st.is_family:
        return []
    op = str((args or {}).get("op", "") or "").strip()
    if op not in st.ops:
        return []
    member = _BY_NAME[st.ops[op]]
    allowed = set(member.input_schema.get("properties") or {}) | {"canvas", "op"}
    return sorted(k for k in (args or {}) if k not in allowed)


__all__ = ["FAMILIES", "OPS", "PAGE_CREATE_SHORT", "PAGE_GRAMMAR", "PROFILES", "SurfaceTool", "alias_lines",
           "build_surface", "display_name", "ignored_args", "resolve_call", "respell_guidance",
           "respell_text", "surface_names"]
