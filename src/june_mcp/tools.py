"""June MCP tools — the agent-facing verbs, as plain testable functions.

Each tool is a function ``handler(client, args) -> dict`` over a ``JuneClient`` — no
MCP runtime needed to call or test them. ``server.py`` wraps these in an MCP server;
tests drive them directly against an in-process service. This keeps the MCP surface
(what an agent can do) decoupled from the MCP transport (how it connects).

MC2 hardening (Phase MC):

* **Descriptions are prompt text** — the agent's model chooses tools by reading them,
  so every description follows one template: what it does → *use when* (vs the
  neighbouring tool) → what comes back. Tool-selection accuracy is measured in the
  live trial (rubric axis 2); these strings are the lever.
* **Arg clamps, in front of service validation** — agent-supplied numbers are clamped
  to the service's own bounds (defense in depth, and a clamped call *succeeds* instead
  of bouncing a 422 the model must reason about). When a value was clamped the result
  carries a ``_clamped`` note so the behaviour is visible, never silent.
* **Write verbs are marked** (``writes=True``) so the server can run read-only
  (``JUNE_READONLY=1``) by construction — the tools simply aren't exposed.
* **`june_resolve` runs server-side** (``POST /v1/resolve``) — the service scans its
  own nodes (server-bounded), so no engine code rides the connector; conservative
  ``strong_only=True`` default; described as post-ingest maintenance.

Tools:
* ``june_answer``        — grounded, cited answer (may abstain). The flagship verb.
* ``june_search``        — fused retrieval over the universal graph (ranked hits).
* ``june_enumerate``     — recall-complete structured retrieval ("list ALL X").
* ``june_context``       — one-call, resolution-aware, budget-bounded context pack.
* ``june_neighborhood`` / ``june_subgraph`` — traverse/expand around a node.
* ``june_remember``      — save new information as text (server-side pure ingest).
* ``june_ingest``        — advanced: write raw nodes/edge-proposals.
* ``june_ingest_file``   — fenced local-file upload (operator-set JUNE_FILES_ROOT;
  absent unless the operator opts in).
* ``june_enrich``        — Pro: background re-extraction of the canvas with the
  richer engine (job + poll; 403 on free endpoints — the gate is server-side).
* ``june_resolve``       — maintenance: cross-format entity resolution (``same_as``),
  run SERVER-SIDE (``POST /v1/resolve``) so the thin connector stays engine-free.
* ``june_canvas_list`` / ``june_canvas_current`` / ``june_canvas_use`` /
  ``june_canvas_create`` — see and SWITCH the active canvas at runtime (no restart);
  create-and-switch for new projects. ``june_canvas_clear`` / ``june_canvas_delete`` —
  destructive canvas ops behind a TWO-PHASE confirm token (first call warns + mints,
  only the second call executes; single-use, ~2 min expiry; active canvas undeletable).
* ``june_page_list`` / ``june_page_get`` — list / read the canvas's graph-native pages.
* ``june_page_create`` / ``june_page_write`` / ``june_page_append`` / ``june_page_update`` —
  compose, replace, extend, or edit-in-place a
  rich page from ordered blocks: text (headings, lists, to-dos, quotes, callouts, code), Markdown
  tables, LIVE VIEWS (``__june_view__`` — a query rendered as table/board/calendar, so a page can be
  a real dashboard), and display-only MEDIA (``embed`` — images/links that show in the doc but never
  enter the graph). An optional canvas ``layout`` positions blocks as cards. So an agent can build
  wonderful, useful pages — documents AND dashboards — straight from a prompt, proactively.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from june_client import JuneClient, PageRevisionConflict

from .runtime import ToolInputError

# ── clamps (mirror the service's own validation bounds; see answer_route/search) ──
MAX_LIMIT = 100
MAX_DEPTH = 3
MAX_EDGES = 1000
MAX_TOKEN_BUDGET = 20_000
MAX_ITEMS = 100
MAX_REMEMBER_CHARS = 65_536      # tool-level cap (service allows more; agents shouldn't)


def _clamp(args: dict, key: str, default: int, lo: int, hi: int,
           notes: dict[str, str]) -> int:
    """Coerce+bound an agent-supplied integer; record a note when it was adjusted."""
    raw = args.get(key, default)
    try:
        v = int(raw)
    except (TypeError, ValueError):
        notes[key] = f"invalid {raw!r} → {default}"
        return default
    if v < lo or v > hi:
        c = max(lo, min(hi, v))
        notes[key] = f"{v} → {c}"
        return c
    return v


def _noted(result: Any, notes: dict[str, str]) -> Any:
    """Attach the clamp notes to dict results (visible, never silent)."""
    if notes and isinstance(result, dict):
        return {**result, "_clamped": notes}
    return result


# ── handlers ──────────────────────────────────────────────────────────────────
def _answer(client: JuneClient, a: dict) -> dict:
    notes: dict[str, str] = {}
    return _noted(client.answer(
        query=a.get("query", ""), seeds=a.get("seeds"),
        limit=_clamp(a, "limit", 20, 1, MAX_LIMIT, notes),
        token_budget=_clamp(a, "token_budget", 2000, 64, MAX_TOKEN_BUDGET, notes),
        max_items=_clamp(a, "max_items", 20, 1, MAX_ITEMS, notes),
        multihop=bool(a.get("multihop", False)),
        max_subqueries=_clamp(a, "max_subqueries", 4, 1, 8, notes),
    ), notes)


def _search(client: JuneClient, a: dict) -> dict:
    notes: dict[str, str] = {}
    return _noted(client.search(
        query=a.get("query", ""), seeds=a.get("seeds"),
        limit=_clamp(a, "limit", 20, 1, MAX_LIMIT, notes),
        min_confidence=float(a.get("min_confidence", 0.0))), notes)


def _context(client: JuneClient, a: dict) -> dict:
    notes: dict[str, str] = {}
    return _noted(client.context(
        query=a.get("query", ""), seeds=a.get("seeds"),
        limit=_clamp(a, "limit", 20, 1, MAX_LIMIT, notes),
        token_budget=_clamp(a, "token_budget", 2000, 64, MAX_TOKEN_BUDGET, notes),
        max_items=_clamp(a, "max_items", 20, 1, MAX_ITEMS, notes),
        mode=a.get("mode", "local")), notes)


def _neighborhood(client: JuneClient, a: dict) -> dict:
    notes: dict[str, str] = {}
    return _noted(client.neighborhood(
        a["node_id"], a["node_type"], direction=a.get("direction", "both"),
        limit=_clamp(a, "limit", 100, 1, MAX_EDGES, notes)), notes)


def _subgraph(client: JuneClient, a: dict) -> dict:
    notes: dict[str, str] = {}
    return _noted(client.subgraph(
        a["node_id"], a["node_type"],
        depth=_clamp(a, "depth", 1, 1, MAX_DEPTH, notes),
        max_edges=_clamp(a, "max_edges", 500, 1, MAX_EDGES, notes)), notes)


def _remember(client: JuneClient, a: dict) -> dict:
    text = str(a.get("text", ""))
    if not text.strip():
        raise ToolInputError("june_remember needs non-empty 'text'")
    notes: dict[str, str] = {}
    if len(text) > MAX_REMEMBER_CHARS:
        notes["text"] = f"{len(text)} chars → {MAX_REMEMBER_CHARS} (truncated)"
        text = text[:MAX_REMEMBER_CHARS]
    return _noted(client.ingest_text(
        text=text, format=a.get("format", "markdown"),
        source_app=str(a.get("source_app", "mcp"))[:64]), notes)


def _ingest(client: JuneClient, a: dict) -> dict:
    return client.ingest(nodes=a.get("nodes", []), proposals=a.get("proposals", []),
                         idempotency_key=a.get("idempotency_key"))


def _resolve(client: JuneClient, a: dict) -> dict:
    # Server-side by design (POST /v1/resolve): the service scans its own nodes
    # and runs the pure resolver, so the thin connector needs zero engine code
    # and the tool is universal in every install. The scan bound lives on the
    # service; a client-supplied "limit" is acknowledged, not silently eaten.
    notes: dict[str, str] = {}
    if "limit" in a:
        notes["limit"] = "resolution scans server-side (server-bounded); limit ignored"
    out = client.resolve(
        strong_only=bool(a.get("strong_only", True)),   # conservative by default (U12)
        min_confidence=float(a.get("min_confidence", 0.62)))
    return _noted(out, notes)


def _enumerate(client: JuneClient, a: dict) -> dict:
    notes: dict[str, str] = {}
    return _noted(client.enumerate(
        terms=a.get("terms"), regex=a.get("regex"),
        node_types=a.get("node_types"), subtype=a.get("subtype"),
        cap=_clamp(a, "cap", 500, 1, 5000, notes)), notes)


def _enrich(client: JuneClient, a: dict) -> dict:
    # One verb, two moments: no args ⇒ start a run (server refuses a second
    # in-flight run per canvas with 409); {"job": id} ⇒ poll that run's progress.
    job = str(a.get("job") or "").strip()
    if job:
        return client.enrich_status(job)
    return client.enrich()


MAX_FILE_BYTES = 25 * 1024 * 1024        # mirror the service's per-file cap


def _files_root() -> str:
    """The operator-set allowlist root for june_ingest_file (empty ⇒ tool absent)."""
    return os.environ.get("JUNE_FILES_ROOT", "").strip()


def _ingest_file(client: JuneClient, a: dict) -> dict:
    # Fenced local-file upload: the OPERATOR (not the agent) chooses the readable
    # root via JUNE_FILES_ROOT at spawn time; every path must resolve inside it
    # (symlinks followed BEFORE the containment check), bounded to one file per
    # call at the service's own size cap. No root ⇒ the tool never exists.
    from pathlib import Path
    root_s = _files_root()
    if not root_s:
        raise ToolInputError("june_ingest_file is disabled: the operator must set "
                         "JUNE_FILES_ROOT to the directory agents may upload from")
    root = Path(root_s).expanduser().resolve()
    raw = str(a.get("path") or "").strip()
    if not raw:
        raise ToolInputError("june_ingest_file needs 'path' (relative to JUNE_FILES_ROOT, "
                         "or absolute inside it)")
    p = Path(raw).expanduser()
    p = (p if p.is_absolute() else root / p).resolve()
    if p != root and root not in p.parents:
        raise ToolInputError("path escapes JUNE_FILES_ROOT — refused")
    if not p.is_file():
        raise ToolInputError(f"not a file under JUNE_FILES_ROOT: {p.name!r}")
    data = p.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise ToolInputError(f"file too large ({len(data)} bytes > {MAX_FILE_BYTES})")
    return client.ingest_file(filename=p.name, data=data)


# ── pages (compose graph-native documents in the current canvas) ────────────────
# Block types the page editor understands (mirror pages_route._BLOCK_TYPES and the frontend
# blocks_md BLOCK_TYPES — the three MUST stay in sync). A table is a `paragraph`/`code` block whose
# text is a GitHub-Markdown table; `embed` is a display-only media/link block (see _media_text).
_PAGE_BLOCK_TYPES = {
    "paragraph", "heading_1", "heading_2", "heading_3", "bulleted", "numbered",
    "todo", "todo_done", "quote", "callout", "code", "divider",
    "embed",                                                     # display-only media/link (out of KG)
    "text", "heading",   # legacy aliases the service still accepts
}
MAX_PAGE_BLOCKS = 2000
import json  # noqa: E402  (local to the pages section; keeps the import next to its use)

# View blocks (live KG surfaces) and canvas layout are ordinary blocks whose TEXT is a sentinel
# JSON the frontend renders — mirror frontend/lib/view_query.ts + page_layout.ts EXACTLY. No new
# node type, no backend change: the block rides the same save seam as any paragraph.
_VIEW_SENTINEL = "__june_view__"
_LAYOUT_SENTINEL = "__june_layout__"
_STYLE_SENTINEL = "__june_style__"
# Styling vocabularies — mirror frontend/lib/block_style.ts EXACTLY (colours, callout variants,
# to-do flags). A block's look rides in the __june_style__ sentinel keyed by real block id, so the
# agent never touches a block's editable text. Unknown values are dropped (same as the frontend).
_STYLE_COLORS = {  # expanded 8 → 18 on 2026-08-21, in lockstep with block_style.COLOR_KEYS
    "slate", "gray", "brown", "red", "rose", "orange", "amber", "yellow", "lime",
    "green", "teal", "cyan", "sky", "blue", "indigo", "purple", "fuchsia", "pink",
}
_CALLOUT_VARIANTS = {"note", "info", "tip", "success", "warning", "danger"}
_TODO_FLAGS = {"high", "low", "blocked"}
_VIEW_KINDS = {"table", "board", "calendar"}
_VIEW_NODE_TYPES = {"entity", "identity", "decision", "artifact"}
_CARD_W, _CARD_H = 300.0, 90.0            # frontend page_layout defaults (CARD_W / CARD_MIN_H)
# Media schemes the agent may reference. The FRONTEND renderer is the security boundary and
# re-checks; this is defense in depth so a javascript:/file: URL never becomes a rendered link.
_MEDIA_OK_SCHEMES = ("http://", "https://", "data:image/")
_IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif", ".bmp")


def _num(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _view_spec_text(b: dict) -> str:
    """A ``{type:'view', node_types, kind, cap, terms?, subtype?}`` block → the __june_view__
    sentinel JSON the editor renders as a live table/board/calendar. Invalid fields fall back to
    the same defaults view_query.parseSpec uses; a view needs ≥1 predicate so an empty one defaults
    to entity."""
    nts = [t for t in (b.get("node_types") or []) if t in _VIEW_NODE_TYPES]
    terms = [str(t) for t in (b.get("terms") or []) if str(t).strip()]
    subtype = str(b.get("subtype") or "").strip() or None
    kind = b.get("kind") if b.get("kind") in _VIEW_KINDS else "table"
    cap = int(_num(b.get("cap", 200), 200))
    cap = max(1, min(1000, cap))
    if not nts and not subtype and not terms:
        nts = ["entity"]
    spec: dict[str, Any] = {_VIEW_SENTINEL: 1, "node_types": nts, "kind": kind, "cap": cap}
    if subtype:
        spec["subtype"] = subtype
    if terms:
        spec["terms"] = terms
    return json.dumps(spec)


def _media_text(b: dict) -> str:
    """A media/link block → Markdown the `embed` renderer draws as an <img> or a link. Accepts a
    ready ``text`` (Markdown), or a ``url`` (+ optional ``alt``/``label``); an image URL (or
    type:'image', or a data:image URI) becomes ![alt](url), otherwise [label](url). Disallowed
    schemes (javascript:, file:, …) are dropped to plain text so a page can never carry an active
    link the renderer would have to defend against."""
    text = str(b.get("text") or "").strip()
    if text:
        return text
    url = str(b.get("url") or b.get("href") or "").strip()
    alt = str(b.get("alt") or b.get("label") or "").strip()
    if not url:
        return ""
    low = url.lower()
    if not (low.startswith(_MEDIA_OK_SCHEMES) or url.startswith("/")):
        return alt or url                                  # unsafe scheme → inert text, never a link
    is_img = (str(b.get("type") or "") == "image" or low.startswith("data:image/")
              or any(low.split("?", 1)[0].endswith(e) for e in _IMG_EXT))
    return f"![{alt}]({url})" if is_img else f"[{alt or url}]({url})"


import re as _re  # noqa: E402  (pages-section import, like json above)

# ── Dropdowns inside table cells: pipe-escape normalization (2026-08-21) ─────────────────────
# `|` is both the GFM cell separator and the select/multi option separator. An agent composing
# a dropdown INSIDE a table cell with raw pipes gets the cell sliced into ghost columns and
# truncated at the header count (found live 2026-08-21: a whole column destroyed after a UI
# round-trip). GFM's answer is `\|` — the frontend's `_cells` un-escapes it before parseSelect
# runs, and the app's own table editor auto-escapes via tableToMarkdown. The agent path is the
# only writer composing raw table text by hand, so the connector normalizes at ITS boundary:
# make the invalid unrepresentable instead of teaching every model an escape rule.
#
# Guard rails (each one is load-bearing — see tests/test_select_pipe_escape.py):
#   * Only when the text contains a REAL GFM table (header row + `---` separator — the
#     frontend's own detection), AND the span sits on a line with structural pipes outside it.
#     Escaping a standalone/mid-prose select would corrupt its labels (parseSelect splits on
#     raw pipes).
#   * Only for paragraph/text blocks — never `code` (verbatim display) or embed/sentinels.
#   * Idempotent: `(?<!\\)\|` never double-escapes the canonical form.
_SELECT_SPAN_RE = _re.compile(r"\[(?:select|multi):[^\]]*\]")
_UNESCAPED_PIPE_RE = _re.compile(r"(?<!\\)\|")
_TABLE_SEP_CELL_RE = _re.compile(r"^:?-+:?$")
_ESCAPABLE_BLOCK_TYPES = {"paragraph", "text"}


def _is_table_sep_line(line: str) -> bool:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    cells = [c for c in cells if c]
    return len(cells) >= 2 and all(_TABLE_SEP_CELL_RE.match(c) for c in cells)


def _has_gfm_table(text: str) -> bool:
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    return any("|" in lines[i] and _is_table_sep_line(lines[i + 1])
               for i in range(len(lines) - 1))


def _escape_select_pipes_in_tables(text: str) -> str:
    """Escape raw pipes inside ``[select: …]``/``[multi: …]`` spans that sit in GFM table
    rows, leaving every other select — and every non-select pipe — byte-identical."""
    if ("[select:" not in text and "[multi:" not in text) or not _has_gfm_table(text):
        return text

    def _fix_line(line: str) -> str:
        if "[select:" not in line and "[multi:" not in line:
            return line
        # Structural pipes OUTSIDE the spans mark a genuine table row; a standalone or
        # mid-prose select (no surrounding cells) must keep its raw pipes.
        if not _UNESCAPED_PIPE_RE.search(_SELECT_SPAN_RE.sub("", line)):
            return line
        return _SELECT_SPAN_RE.sub(
            lambda m: _UNESCAPED_PIPE_RE.sub(r"\\|", m.group(0)), line)

    return "\n".join(_fix_line(ln) for ln in text.split("\n"))


def _one_block(b: dict, order: float) -> dict:
    """One agent block → one service block ``{block_type, text, order}`` (id added later for
    in-place updates). Dispatches the two structured kinds (view, media) to their serializers."""
    t = str(b.get("type") or b.get("block_type") or "paragraph")
    if t == "view":
        return {"block_type": "paragraph", "text": _view_spec_text(b), "order": order}
    if t in ("image", "embed", "media", "link"):
        return {"block_type": "embed", "text": _media_text(b), "order": order}
    if t not in _PAGE_BLOCK_TYPES:
        t = "paragraph"
    text = str(b.get("text", ""))
    if t in _ESCAPABLE_BLOCK_TYPES:
        text = _escape_select_pipes_in_tables(text)
    out = {"block_type": t, "text": text, "order": order}
    # CARRY THE ID FORWARD (2026-08-17). A block id sent back means "this is the same block",
    # so the server updates it in place instead of tombstoning it and minting a new node. Two
    # things follow, and both were previously impossible: block IDENTITY survives a rewrite —
    # mentions, per-block styling and canvas layout all key on the id — and the write can be
    # judged, because a payload that carries ids demonstrably grew out of a real read.
    bid = b.get("id") or b.get("block_id")
    if isinstance(bid, str) and bid.strip():
        out["id"] = bid.strip()
    return out


def _to_blocks(raw: Any) -> list[dict]:
    """Agent-supplied ``[{type, text|url|view fields}]`` → service blocks. Rich types: `view`
    (live KG surface), `image`/`embed`/`link` (display-only media, out of KG), plus every text
    block type. Unknown/absent type → paragraph; order = position (1..n); bounded to the cap."""
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for i, b in enumerate(raw[:MAX_PAGE_BLOCKS]):
        if isinstance(b, dict):
            out.append(_one_block(b, float(i + 1)))
    return out


def _layout_text(cards: Any, ids_by_index: dict[int, str], columns: Any = None) -> str | None:
    """Canvas cards ``[{block:<index>, x, y, w, h, title?}]`` and/or doc ``columns``
    ``[[<index>, …], …]`` → the __june_layout__ sentinel JSON, keyed by the REAL block ids
    resolved after the first save (the same index↔id trick throughout). Column groups with
    fewer than two resolvable blocks dissolve (a one-column "group" is not a thing — mirrors
    frontend page_columns). Returns None when nothing resolves, so the caller skips the
    layout block entirely. THE GUARDED PATH the raw-sentinel hand-edit is not (2026-08-21):
    it exists only inside create/write, where the page is being written wholesale — no
    existing sentinel's pos/folds can be clobbered by omission."""
    if not isinstance(cards, list):
        cards = None
    if cards is None and not isinstance(columns, list):
        return None
    pos: dict[str, dict] = {}
    titles: dict[str, str] = {}
    for c in cards or []:
        if not isinstance(c, dict):
            continue
        try:
            idx = int(c.get("block"))
        except (TypeError, ValueError):
            continue
        bid = ids_by_index.get(idx)
        if not bid:
            continue
        pos[bid] = {"x": _num(c.get("x"), 0.0), "y": _num(c.get("y"), 0.0),
                    "w": _num(c.get("w"), _CARD_W) or _CARD_W,
                    "h": _num(c.get("h"), _CARD_H) or _CARD_H}
        title = str(c.get("title") or "").strip()
        if title:
            titles[bid] = title
    col_groups: list[list[str]] = []
    if isinstance(columns, list):
        for grp in columns:
            if not isinstance(grp, list):
                continue
            gids: list[str] = []
            for i in grp:
                try:
                    bid = ids_by_index.get(int(i))
                except (TypeError, ValueError):
                    continue
                if bid and bid not in gids:
                    gids.append(bid)
            if len(gids) >= 2:
                col_groups.append(gids)
    if not pos and not col_groups:
        return None
    body: dict[str, Any] = {_LAYOUT_SENTINEL: 1, "mode": "canvas" if pos else "doc", "pos": pos}
    if titles:
        body["titles"] = titles
    if col_groups:
        body["colGroups"] = col_groups
    return json.dumps(body)


def _block_style(b: dict) -> dict:
    """A per-block style dict from an agent block ``{variant?, flag?/priority?, color?/bg?, accent?,
    icon?}`` → only the valid keys (mirrors frontend block_style _cleanBlockStyle). `variant` styles a
    callout (note/info/tip/success/warning/danger); `flag` a to-do (high/low/blocked); `bg`/`accent`
    tint any block; `icon` badges a block/card. Each SEMANTIC value carries colour + icon + label on
    render, so meaning survives greyscale (the 'not just colour' rule)."""
    s: dict[str, Any] = {}
    v = str(b.get("variant") or "").strip().lower()
    if v in _CALLOUT_VARIANTS:
        s["variant"] = v
    fl = str(b.get("flag") or b.get("priority") or "").strip().lower()
    if fl in _TODO_FLAGS:
        s["flag"] = fl
    bg = str(b.get("bg") or b.get("color") or "").strip().lower()
    if bg in _STYLE_COLORS:
        s["bg"] = bg
    ac = str(b.get("accent") or "").strip().lower()
    if ac in _STYLE_COLORS:
        s["accent"] = ac
    icon = str(b.get("icon") or "").strip()
    if icon and len(icon) <= 8:
        s["icon"] = icon
    return s


def _styles_by_index(raw: Any) -> dict[int, dict]:
    """Agent blocks → {input-index: style} for the non-empty styles, so a follow-up save can key each
    on the server's real block id (the same index↔order trick the layout uses)."""
    out: dict[int, dict] = {}
    if isinstance(raw, list):
        for i, b in enumerate(raw[:MAX_PAGE_BLOCKS]):
            if isinstance(b, dict):
                s = _block_style(b)
                if s:
                    out[i] = s
    return out


def _style_text(styles: dict[int, dict], ids_by_index: dict[int, str], page_accent: Any) -> str | None:
    """Per-index styles + an optional page accent → the __june_style__ sentinel JSON keyed by REAL
    block ids. Returns None when nothing valid is styled (so no empty sentinel block is written)."""
    blocks: dict[str, dict] = {}
    for idx, s in styles.items():
        bid = ids_by_index.get(idx)
        if bid:
            blocks[bid] = s
    body: dict[str, Any] = {_STYLE_SENTINEL: 1, "blocks": blocks}
    pa = str(page_accent or "").strip().lower()
    if pa in _STYLE_COLORS:
        body["page"] = {"accent": pa}
    if not blocks and "page" not in body:
        return None
    return json.dumps(body)


def _ids_by_order(detail: dict) -> dict[int, str]:
    """Map a saved page's blocks back to their 0-based input index via `order` (which we set to
    i+1), so a follow-up layout save can key on the server's real block ids."""
    out: dict[int, str] = {}
    for blk in (detail.get("blocks") or []):
        try:
            idx = int(round(_num(blk.get("order"), 0.0))) - 1
        except (TypeError, ValueError):
            continue
        if idx >= 0 and blk.get("block_id"):
            out[idx] = str(blk["block_id"])
    return out


def _carry_ids(detail: dict) -> list[dict]:
    """Saved blocks → save payload carrying their real ids, so a second authoritative save updates
    them in place (never re-creates/orphans them)."""
    return [{"id": b.get("block_id"), "block_type": b.get("block_type"),
             "text": b.get("text", ""), "order": b.get("order", 0.0)}
            for b in (detail.get("blocks") or []) if b.get("block_id")]


def _page_list(client: JuneClient, a: dict) -> dict:
    notes: dict[str, str] = {}
    return _noted(client.list_pages(
        limit=_clamp(a, "limit", 200, 1, 2000, notes),
        offset=max(0, _clamp(a, "offset", 0, 0, 10_000_000, notes))), notes)


def _page_get(client: JuneClient, a: dict) -> dict:
    pid = str(a.get("page_id", "")).strip()
    if not pid:
        raise ToolInputError("june_page_get needs 'page_id'")
    return client.get_page(pid)


def _wants_canvas(layout: Any) -> bool:
    return isinstance(layout, dict) and str(layout.get("mode", "")).lower() == "canvas"


def _save_with_layout(client: JuneClient, pid: str, blocks: list[dict], layout: Any,
                      styles: dict[int, dict] | None = None, page_accent: Any = None,
                      *, expected_updated_at: str | None = None, force: bool = False) -> dict:
    """Save content blocks, then — if a canvas layout OR any per-block/page styling was requested —
    resolve the server's real block ids and re-save the SAME content (carrying those ids so they
    update in place) plus a __june_layout__ and/or __june_style__ sentinel block. The second save is
    irreducible: a card's position and a block's style both key on a block id, which exists only after
    the first save. Returns {mode, cards, styled} describing what landed.

    ``expected_updated_at`` guards the FIRST save (the caller's revision token). The second save
    is guarded by the token the first save just issued — never by the caller's, which is stale by
    construction the moment the first save lands. Getting that wrong would make every styled write
    conflict with itself."""
    detail = client.save_blocks(pid, blocks, expected_updated_at=expected_updated_at, force=force)
    styles = styles or {}
    ids = _ids_by_order(detail)
    lt = None
    if isinstance(layout, dict):
        lt = _layout_text(layout.get("cards") if _wants_canvas(layout) else None, ids,
                          columns=layout.get("columns"))
    stext = _style_text(styles, ids, page_accent)
    if lt is None and stext is None:
        return {"mode": "doc", "cards": 0, "styled": 0}    # nothing to key on ids → stay a linear doc
    content = _carry_ids(detail)
    for extra in (lt, stext):
        if extra is not None:
            content.append({"block_type": "paragraph", "text": extra, "order": float(len(content) + 1)})
    # Phase two is guarded by the token phase one just issued. When the server issued none
    # (a legacy row with no updated_at) there is nothing to verify, so continue our own
    # two-phase save explicitly rather than failing a write that is already half-applied.
    rev2 = detail.get("updated_at")
    try:
        client.save_blocks(pid, content, expected_updated_at=rev2, force=rev2 is None)
    except PageRevisionConflict as exc:
        # PHASE ONE ALREADY LANDED. Reporting this as an ordinary conflict would tell the caller
        # the write "was NOT applied" — a confident, false statement about their page, with an
        # instruction not to resend. The content IS there; only the styling/layout sentinel is
        # not. Re-raised as a distinct type so `_page_write` can say exactly that.
        raise StylingConflict(str(exc)) from exc
    cards = 0
    if lt is not None:
        try:
            cards = len(json.loads(lt).get("pos", {}))
        except Exception:  # noqa: BLE001
            cards = 0
    try:
        styled = len(json.loads(stext).get("blocks", {})) if stext is not None else 0
    except Exception:  # noqa: BLE001
        styled = 0
    return {"mode": "canvas" if lt is not None else "doc", "cards": cards, "styled": styled}


def _page_create(client: JuneClient, a: dict) -> dict:
    title = str(a.get("title", "")).strip() or "Untitled"
    created = client.create_page(title)
    pid = created["page_id"]
    blocks = _to_blocks(a.get("blocks"))
    styles = _styles_by_index(a.get("blocks"))
    page_accent = a.get("theme") or a.get("accent")
    layout = {"mode": "doc", "cards": 0, "styled": 0}
    if blocks or styles or page_accent:
        # force=True is correct here and ONLY here: the page was created one line above, so it
        # holds nothing anyone else wrote. Every other save must prove it read first.
        layout = _save_with_layout(client, pid, blocks, a.get("layout"), styles, page_accent,
                                   force=True)
    return {"page_id": pid, "title": created.get("title", title),
            "blocks_written": len(blocks), "layout": layout}


# How much a single write may quietly remove before a human has to mean it. Chosen against the
# real incidents rather than a round number: the 2026-08-17 loss dropped 31 blocks, the 2026-08-16
# one dropped 191, and ordinary editing — retitling a section, deleting a stale paragraph or two —
# sits far below this. A page-sized rewrite that legitimately drops more says `force` once.
LOST_BLOCKS_REFUSAL = 10


def _norm_text(t: Any) -> str:
    """Whitespace-insensitive block identity, matching the service's own supersede test."""
    return " ".join(str(t or "").split())


class StylingConflict(RuntimeError):
    """A styled/canvas write whose CONTENT save succeeded and whose sentinel save then lost a
    race. Separate from ``PageRevisionConflict`` because the two demand opposite advice: the
    plain conflict means nothing was written and the caller must re-read; this one means the
    page was rewritten and only the styling is missing, so telling the caller "not applied"
    would be false and telling them "do not resend" would strand a half-styled page."""


def _refusal(reason: str, page_id: str, message: str, **fields: Any) -> dict:
    """A write this connector declined to perform — returned as a RESULT, never raised.

    WHY NOT AN EXCEPTION (2026-08-17). The first version raised ``ValueError`` with this text.
    The agent never saw a word of it: ``server._call`` hands every exception to
    ``runtime.map_error``, which builds agent-visible text from the exception TYPE alone and
    NEVER from ``str(exc)`` — deliberately, because a transport exception can carry the request
    URL, headers or key material. So the whole refusal collapsed to "Tool arguments were invalid
    (ValueError) — check the tool's input schema and retry", and a model reading that will go and
    mangle its arguments rather than carry block ids forward or pass ``force``. The guard still
    protected the page; the half that was supposed to TEACH was mute.

    That redaction is correct and stays. The bug was putting deliberately-authored, secret-free
    guidance into the one channel the server is required to redact. A refusal is not a failure of
    the tool — it is the tool working, deciding, and reporting. Results are serialized verbatim,
    so this shape survives every client, including ones that render errors as a status line.

    Unmistakably not a success: no ``blocks_written`` key, ``ok`` and ``written`` both false, and
    the reason first. ``reason`` is the machine-readable form for anything that wants to branch;
    ``message`` is what a human or a model reads.
    """
    out = {"ok": False, "written": False, "refused": reason, "page_id": page_id,
           "message": message}
    out.update({k: v for k, v in fields.items() if v is not None})
    return out


def _page_write(client: JuneClient, a: dict) -> dict:
    """Authoritative page replace, judged on WHAT IT WOULD REMOVE rather than on procedure.

    The earlier version of this demanded ``expected_updated_at`` from the caller, which made
    every rewrite pay for a read it might not need and broke every existing caller. It also
    policed the wrong thing: the danger is not that an agent skipped a step, it is that content
    disappears. So the connector now does the read ITSELF — one call it was already making for
    the receipt — and uses it three ways:

    * the page's own revision token guards the save, so a concurrent write is refused rather
      than silently reverted, and the caller supplies nothing;
    * the current blocks are compared against the payload, so the receipt can say what would be
      LOST (text that will not survive) instead of only what was written;
    * a write that would lose ``LOST_BLOCKS_REFUSAL`` or more blocks is refused unless the
      caller says ``force``, and the refusal NAMES the first few, so a human sees the sections
      about to disappear rather than a number.

    Carrying block ``id``s forward from ``june_page_get`` is now supported and is the good path:
    matched blocks update in place, so ids survive and nothing is lost to compare in the first
    place. ``expected_updated_at`` is still accepted for a caller that wants to pin a specific
    revision, but it is no longer required.
    """
    pid = str(a.get("page_id", "")).strip()
    if not pid:
        raise ToolInputError("june_page_write needs 'page_id'")
    blocks = _to_blocks(a.get("blocks"))
    styles = _styles_by_index(a.get("blocks"))
    page_accent = a.get("theme") or a.get("accent")
    force = bool(a.get("force", False))

    # THE READ THE CALLER NO LONGER HAS TO MAKE. Best-effort: an engine that cannot serve it
    # must not turn a legal write into an error, so we degrade to the old unguarded behaviour
    # rather than failing closed on a read we added for the caller's benefit.
    current: dict = {}
    try:
        current = client.get_page(pid) or {}
    except Exception:  # noqa: BLE001
        current = {}
    existing = current.get("blocks") or []
    rev = a.get("expected_updated_at") or current.get("updated_at")
    rev = str(rev).strip() if rev else None

    lost: list[dict] = []
    if existing:
        keep_ids = {str(b["id"]) for b in blocks if b.get("id")}
        keep_text = {_norm_text(b.get("text")) for b in blocks}
        lost = [b for b in existing
                if str(b.get("block_id")) not in keep_ids
                and _norm_text(b.get("text")) not in keep_text
                and _norm_text(b.get("text"))]          # an empty block is not a loss
    if len(lost) >= LOST_BLOCKS_REFUSAL and not force:
        return _refusal(
            "would_remove_blocks", pid,
            f"REFUSED — this write would remove {len(lost)} block(s) from the page, and it was "
            f"NOT applied. First few: "
            + " | ".join(_norm_text(b.get("text"))[:60] for b in lost[:4])
            + ". If you meant to replace the page wholesale, repeat the call with force=true. "
              "If you meant to ADD, use june_page_append. If you meant to revise, call "
              "june_page_get and send its blocks back WITH their `id` fields, changing only "
              "what should change — matched blocks update in place and nothing is lost.",
            blocks_before=len(existing), would_remove=len(lost),
            would_remove_first=[_norm_text(b.get("text"))[:120] for b in lost[:4]])

    warning: str | None = None
    try:
        layout = _save_with_layout(client, pid, blocks, a.get("layout"), styles, page_accent,
                                   expected_updated_at=rev, force=rev is None)
    except PageRevisionConflict:
        return _refusal(
            "page_changed_since_read", pid,
            "REFUSED — the page changed while this write was being prepared, so it was NOT "
            "applied. Another session, another host, or the user's own editor wrote to it. "
            "Call june_page_get again and decide against the CURRENT content — do not resend "
            "this payload, it would delete whatever arrived in the meantime.",
            blocks_before=len(existing) or None)
    except StylingConflict:
        # NOT a refusal: the content write landed. Report the truth — written, unstyled — and
        # let the receipt below carry blocks_removed and the restore handle exactly as it would
        # for any other successful write, because from the page's point of view this WAS one.
        layout = {"mode": "doc", "cards": 0, "styled": 0}
        warning = ("the content was WRITTEN, but the styling/layout could not be applied: the "
                   "page changed between the two saves this needs. Do NOT resend the whole "
                   "payload — call june_page_get and re-apply only the styling against the "
                   "current blocks.")

    out: dict = {"page_id": pid, "blocks_written": len(blocks), "layout": layout}
    if warning:
        out["warning"] = warning
    if existing:
        out["blocks_before"] = len(existing)
        out["blocks_removed"] = len(lost)
        if lost:
            # Recoverable, and the receipt says so at the moment it matters. A removed block is
            # tombstoned, not deleted: GET /v1/pages/{id}/removed lists it, POST .../restore
            # puts it back with its original id and position.
            out["recover"] = f"POST /v1/pages/{pid}/restore restores what this write removed"
    return out


def _page_append(client: JuneClient, a: dict) -> dict:
    pid = str(a.get("page_id", "")).strip()
    if not pid:
        raise ToolInputError("june_page_append needs 'page_id'")
    blocks = _to_blocks(a.get("blocks"))
    if not blocks:
        raise ToolInputError("june_page_append needs a non-empty 'blocks'")
    detail = client.append_blocks(pid, blocks)
    # CX7 engines return {appended, blocks_total, revision}; the legacy fallback
    # returns the full page detail. Read whichever shape arrived — never guess.
    total = detail.get("blocks_total")
    if total is None:
        total = len(detail.get("blocks") or [])
    out = {"page_id": pid, "blocks_appended": len(blocks), "blocks_total": total}
    if detail.get("revision") is not None:
        out["revision"] = detail["revision"]
    return out


def _page_update(client: JuneClient, a: dict) -> dict:
    """CX12 — edit NAMED blocks in place. The small-payload safe edit: found live
    2026-08-20 when fixing ten blocks of a 78-block page required resending the
    whole document through june_page_write (whose omissions DELETE)."""
    pid = str(a.get("page_id", "")).strip()
    if not pid:
        raise ToolInputError("june_page_update needs 'page_id'")
    raw = a.get("blocks")
    if not isinstance(raw, list) or not raw:
        raise ToolInputError("june_page_update needs a non-empty 'blocks' — each "
                             "{id, text, block_type?}")
    blocks: list[dict] = []
    for i, b in enumerate(raw):
        if not isinstance(b, dict) or not str(b.get("id") or "").strip():
            raise ToolInputError(
                f"june_page_update blocks[{i}] has no 'id' — an update names its "
                "targets (ids come from june_page_get). To ADD new blocks use "
                "june_page_append instead.")
        # Same normalization as _one_block, same type rule: an update that carries text
        # for a paragraph (declared or implied — the server keeps the existing type when
        # block_type is omitted, and paragraphs are what tables live in) gets its in-table
        # select pipes escaped; a declared code block stays verbatim.
        bt = str(b.get("block_type") or b.get("type") or "paragraph")
        if bt in _ESCAPABLE_BLOCK_TYPES and isinstance(b.get("text"), str):
            b = {**b, "text": _escape_select_pipes_in_tables(b["text"])}
        blocks.append(b)
    kw: dict = {}
    if a.get("expected_revision") is not None:
        kw["expected_revision"] = int(a["expected_revision"])
    if a.get("force"):
        kw["force"] = True
    import httpx as _httpx
    try:
        res = client.update_blocks(pid, blocks, **kw)
    except _httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            # Composed from literals + agent-supplied ids only (redaction rule).
            raise KeyError(
                "june_page_update refused: the page or one of the named block ids "
                "does not exist in this canvas — or this engine predates the "
                "blocks:update route. Verify the ids with june_page_get. "
                "Nothing was updated (the call is atomic).")
        raise
    out = {"page_id": pid, "blocks_updated": len(blocks),
           "blocks_total": res.get("blocks_total")}
    if res.get("revision") is not None:
        out["revision"] = res["revision"]
    return out


def _page_delete(client: JuneClient, a: dict) -> dict:
    pid = str(a.get("page_id", "")).strip()
    if not pid:
        raise ToolInputError("june_page_delete needs 'page_id'")
    return client.delete_page(pid)


# ── canvases (address / create / manage isolated workspaces) ──────────────────
# CX3 (2026-08-19): the connector holds NO mutable canvas state. The startup
# default (JUNE_CANVAS) is resolved once and IMMUTABLE — ``JuneClient.canvas``
# is a read-only property, so no conversation can redirect another conversation
# sharing this process (the Gap-1 interleave observed live 2026-08-17/18/19).
# A canvas is chosen PER CALL: every canvas-scoped tool takes an optional
# ``canvas`` (name | id | canvas_handle); absent ⇒ the immutable default.
# ``june_canvas_use`` resolves and returns a handle — it moves nothing.
# Deliberate invariants:
#   * A canvas_handle embeds this process's epoch (CX4): after a connector
#     restart a stale handle is REFUSED loudly, never silently redirected.
#   * Destructive ops (clear/delete) are TWO-PHASE: the first call executes
#     nothing and returns a single-use, short-lived confirm token bound to
#     (op, canvas); only a second call carrying that token executes. An agent
#     cannot erase a canvas in one tool call, and the human sees the warning
#     turn in the transcript between the two.
#   * These tools address NAMED canvases only — the home workspace is not
#     reachable through them (the service's canvas routes are canvas-scoped).
#   * Deleting the ACTIVE canvas is refused outright: switch away first, so a
#     successful delete can never leave the connector pointed at a 404.
import threading as _threading  # noqa: E402  (canvas-section imports, like json above)
import time as _time  # noqa: E402  (local to the canvas section, like json above)
import uuid as _uuid  # noqa: E402

CONFIRM_TTL_SECONDS = 120.0


class PendingConfirms:
    """Single-use, expiring confirmation tokens, bound to (op, canvas_id).
    Injectable clock for tests; module state below is per-connector-process,
    which is exactly the lifetime the two-phase gate should have."""

    def __init__(self, clock=_time.monotonic) -> None:
        self._clock = clock
        self._rows: dict[str, tuple[str, str, float]] = {}

    def mint(self, op: str, canvas_id: str) -> str:
        token = _uuid.uuid4().hex
        self._rows[token] = (op, canvas_id, self._clock() + CONFIRM_TTL_SECONDS)
        return token

    def consume(self, token: str, op: str, canvas_id: str) -> tuple[bool, str]:
        """Pop-first (single-use even on mismatch — conservative), then validate."""
        row = self._rows.pop(str(token or ""), None)
        if row is None:
            return False, "unknown or already-used confirm token"
        t_op, t_canvas, expires = row
        if self._clock() > expires:
            return False, "the confirm token expired"
        if t_op != op or t_canvas != canvas_id:
            return False, "the confirm token was minted for a different operation or canvas"
        return True, ""


# CX10 note: confirm tokens are PROCESS-scoped by construction — this dict lives in
# process memory, so a connector restart destroys every pending confirmation and a
# cross-epoch consume is refused as "unknown token" (loud, single-use, pop-first).
# v1's "To Be Done" §6 overstated this as a cross-conversation hole; it is defence in
# depth over an already-unguessable capability (uuid4().hex), pinned by test.
_CONFIRMS = PendingConfirms()

# CX4: the process epoch a canvas_handle is bound to. A handle minted by a
# previous connector process fails the epoch check and is refused — a restart
# becomes a visible refusal instead of a silent redirect to the default.
_EPOCH = _uuid.uuid4().hex[:12]
_HANDLE_PREFIX = "jch1."

# Display-only memo of canvas names seen this process (id → name). Never a
# correctness input — ownership/existence are enforced server-side per call.
_NAMES: dict[str, str] = {}

# ── CX9: name→id resolution cache ─────────────────────────────────────────────
# Pre-CX9, EVERY per-call ``canvas=<name|id>`` resolution issued a live
# ``GET /v1/canvases`` before the actual request — two wire calls per tool call.
# This cache holds POSITIVE, UNAMBIGUOUS resolutions only (a miss or ambiguity is
# never cached, so a just-created canvas is findable immediately), for a bounded
# TTL, with explicit eviction on the two events that can falsify an entry: our
# own june_canvas_delete, and a canvas-scoped 404 (deleted elsewhere). It is a
# TRAFFIC optimization, never a correctness input — the server still enforces
# ownership/existence on the call itself (the CX4 handle posture, extended).
# Guarded by a lock: CX8 runs tools on worker threads concurrently.
_RESOLVE_TTL = 60.0
_RESOLVE_LOCK = _threading.Lock()
_RESOLVE: dict[str, tuple[str, str, float]] = {}   # wanted → (canvas_id, name, expires)


def _cache_now() -> float:      # module-level seam so tests can age entries
    return _time.monotonic()


def _cache_get(wanted: str) -> tuple[str, str] | None:
    with _RESOLVE_LOCK:
        entry = _RESOLVE.get(wanted)
        if entry is None:
            return None
        cid, name, expires = entry
        if expires <= _cache_now():
            _RESOLVE.pop(wanted, None)
            return None
        return cid, name


def _cache_put(wanted: str, cid: str, name: str) -> None:
    expires = _cache_now() + _RESOLVE_TTL
    with _RESOLVE_LOCK:
        _RESOLVE[wanted] = (cid, name, expires)
        if cid != wanted:                     # the id itself is now verified too
            _RESOLVE[cid] = (cid, name, expires)


def _cache_drop_canvas(cid: str) -> None:
    """Evict every entry that resolves to ``cid`` (delete / canvas-scoped 404)."""
    with _RESOLVE_LOCK:
        for k in [k for k, v in _RESOLVE.items() if v[0] == cid]:
            _RESOLVE.pop(k, None)


def _cache_reset() -> None:                    # test seam
    with _RESOLVE_LOCK:
        _RESOLVE.clear()


def note_canvas_name(canvas_id: str, name: str) -> None:
    """Record a canvas display name (e.g. the startup default) for result echoes."""
    if canvas_id and name:
        _NAMES[str(canvas_id)] = str(name)


def make_canvas_handle(canvas_id: str) -> str:
    """An unsigned CORRECTNESS token (never a credential): pins one canvas id to
    this process epoch so later calls can address it with zero lookups — and so
    a connector restart refuses instead of redirecting."""
    return f"{_HANDLE_PREFIX}{_EPOCH}.{canvas_id}"


def _decode_handle(wanted: str) -> str | None:
    """canvas_id from a handle, None if ``wanted`` is not handle-shaped. A string
    that IS handle-shaped but invalid is REFUSED — never reinterpreted as a name
    (fail closed; a malformed handle must not silently become a lookup)."""
    if not wanted.startswith(_HANDLE_PREFIX):
        return None
    parts = wanted.split(".")
    if len(parts) != 3 or not parts[1] or not parts[2]:
        raise KeyError("malformed canvas_handle — refusing rather than guessing "
                       "(a handle is never reinterpreted as a name). Call "
                       "june_canvas_use again to mint a fresh one. Nothing was written.")
    epoch, cid = parts[1], parts[2]
    if epoch != _EPOCH:
        raise KeyError(f"stale canvas_handle: minted by a previous connector process "
                       f"(handle epoch {epoch!r}, current epoch {_EPOCH!r}). The "
                       "connector has restarted since that handle was issued — call "
                       "june_canvas_use again for a fresh handle. Nothing was written.")
    try:
        _uuid.UUID(cid)
    except ValueError:
        raise KeyError("malformed canvas_handle — the embedded canvas id is not a "
                       "UUID; call june_canvas_use again. Nothing was written.")
    return cid


def _canvas_target(client: JuneClient, wanted: str) -> tuple[str, str | None]:
    """Resolve a per-call canvas reference (handle | id | name) → (canvas_id, name?).
    Handles resolve with ZERO network calls (CX4); ids/names go through the
    fail-closed ``_canvas_ref``. Authorization is NOT decided here — the server
    enforces ownership/existence on the call itself, so a deleted canvas is a
    404 from the service, never a silent write."""
    hid = _decode_handle(wanted)
    if hid is not None:
        return hid, _NAMES.get(hid)
    cid, name = _canvas_ref(client, wanted)
    return cid, name or _NAMES.get(cid)


def _canvas_ref(client: JuneClient, wanted: str) -> tuple[str, str]:
    """Resolve an agent-supplied canvas name-or-id to ``(canvas_id, name)`` against
    the live canvas list — fail-closed: ids must exist, names must match exactly
    one canvas (exact match first, then unique case-insensitive). Raises KeyError
    with a message built only from canvas names/ids (never transport internals).

    CX9: positive, unambiguous resolutions are served from a bounded-TTL cache
    (see ``_RESOLVE``) — misses and ambiguities always re-check live."""
    cached = _cache_get(wanted)
    if cached is not None:
        return cached
    rows = client.list_canvases()
    for r in rows:                       # display-name memo for result echoes (CX6)
        note_canvas_name(str(r.get("canvas_id", "")), str(r.get("name", "")))
    try:
        _uuid.UUID(wanted)
        for r in rows:
            if str(r.get("canvas_id")) == wanted:
                _cache_put(wanted, wanted, str(r.get("name", "")))
                return wanted, str(r.get("name", ""))
        raise KeyError(f"no canvas with id {wanted} on this endpoint")
    except ValueError:
        pass
    matches = [r for r in rows if str(r.get("name", "")) == wanted]
    if not matches:
        matches = [r for r in rows
                   if str(r.get("name", "")).strip().lower() == wanted.strip().lower()]
    if len(matches) == 1:
        cid, cname = str(matches[0]["canvas_id"]), str(matches[0].get("name", ""))
        _cache_put(wanted, cid, cname)
        return cid, cname
    if len(matches) > 1:
        ids = ", ".join(sorted(str(r["canvas_id"]) for r in matches))
        raise KeyError(f"canvas name {wanted!r} is ambiguous ({ids}) — use an id")
    existing = ", ".join(sorted({str(r.get("name", "")) for r in rows if r.get("name")}))
    raise KeyError(f"no canvas named {wanted!r}"
                   + (f" (existing: {existing})" if existing else " (no canvases exist yet)")
                   + " — june_canvas_create can make it")


_IMMUTABLE_NOTE = (
    "the startup default canvas (JUNE_CANVAS) is IMMUTABLE for this process (CX3): "
    "nothing any conversation does can move it, so calls that omit 'canvas' are "
    "deterministic. To act in another canvas, pass canvas=<name | id | canvas_handle> "
    "on that call — the choice applies to that call only, nothing is remembered")


def _canvas_list(client: JuneClient, a: dict) -> dict:
    rows = client.list_canvases()
    for r in rows:
        note_canvas_name(str(r.get("canvas_id", "")), str(r.get("name", "")))
    default = client.canvas or ""
    return {"canvases": [{**r, "default": str(r.get("canvas_id")) == default,
                          # deprecated alias of "default" (pre-CX3 shape); one release
                          "active": str(r.get("canvas_id")) == default} for r in rows],
            "default_canvas_id": default or None,
            "active_canvas_id": default or None,   # deprecated alias; one release
            "note": _IMMUTABLE_NOTE}


def _canvas_current(client: JuneClient, a: dict) -> dict:
    default = client.canvas or ""
    name = _NAMES.get(default)
    if default and name is None:
        name = next((str(r.get("name", "")) for r in client.list_canvases()
                     if str(r.get("canvas_id")) == default), None)
        if name:
            note_canvas_name(default, name)
    return {"default_canvas_id": default or None,
            "active_canvas_id": default or None,   # deprecated alias; one release
            "name": name, "note": _IMMUTABLE_NOTE}


def _canvas_use(client: JuneClient, a: dict) -> dict:
    wanted = str(a.get("canvas") or "").strip()
    if not wanted:
        raise ToolInputError("june_canvas_use needs 'canvas' (a canvas name, id, or handle)")
    cid, name = _canvas_target(client, wanted)
    return {"canvas_id": cid, "name": name,
            "canvas_handle": make_canvas_handle(cid),
            "default_canvas_id": client.canvas or None,
            "switched": False,
            "note": ("nothing moved (CX3): this call RESOLVED the canvas and minted a "
                     "handle — it did not, and cannot, change where other calls land. "
                     "Pass canvas=<this canvas_handle> (or the name/id) on each call "
                     "that should act in it. The handle is a correctness token, not a "
                     "credential: after a connector restart it is refused rather than "
                     "silently redirected. " + _IMMUTABLE_NOTE)}


def _canvas_create(client: JuneClient, a: dict) -> dict:
    name = str(a.get("name") or "").strip()
    if not name:
        raise ToolInputError("june_canvas_create needs 'name'")
    clash = [r for r in client.list_canvases() if str(r.get("name", "")) == name]
    if clash:
        raise KeyError(f"a canvas named {name!r} already exists "
                       f"({clash[0]['canvas_id']}) — address it with canvas=..., or pick "
                       "another name (duplicate names make every later name lookup ambiguous)")
    made = client.create_canvas(name)
    cid = str(made["canvas_id"])
    note_canvas_name(cid, str(made.get("name", name)))
    return {"canvas_id": cid, "name": str(made.get("name", name)), "created": True,
            "canvas_handle": make_canvas_handle(cid), "switched": False,
            "note": ("created. Nothing switched (CX3) — pass canvas=<this "
                     "canvas_handle or the name> on calls that should act in it. "
                     + _IMMUTABLE_NOTE)}


def _canvas_destructive(client: JuneClient, a: dict, *, op: str) -> dict:
    wanted = str(a.get("canvas") or "").strip()
    if not wanted:
        raise ToolInputError(f"june_canvas_{op} needs 'canvas' (a canvas name, id, or handle)")
    cid, name = _canvas_target(client, wanted)
    if op == "delete" and cid == (client.canvas or ""):
        raise KeyError("refusing to delete this connection's DEFAULT canvas "
                       "(JUNE_CANVAS): the default is immutable for the process "
                       "(CX3), so deleting it would leave every canvas-less call "
                       "pointed at a canvas that no longer exists. Restart the "
                       "connector with a different JUNE_CANVAS first.")
    token = str(a.get("confirm") or "").strip()
    if not token:
        minted = _CONFIRMS.mint(op, cid)
        effect = ("erase every node and edge in" if op == "clear"
                  else "erase the graph of AND permanently remove")
        return {"pending": True, "op": op, "canvas_id": cid, "name": name,
                "confirm_token": minted,
                "expires_in_seconds": int(CONFIRM_TTL_SECONDS),
                "warning": (f"This will IRREVERSIBLY {effect} canvas {name!r} ({cid}). "
                            "NOTHING has been executed. Confirm with the user that this "
                            f"is intended, then call june_canvas_{op} again with this "
                            "confirm_token to execute.")}
    ok, reason = _CONFIRMS.consume(token, op, cid)
    if not ok:
        raise KeyError(f"confirmation failed: {reason} — call june_canvas_{op} again "
                       "without 'confirm' to mint a fresh token")
    res = client.clear_canvas(cid) if op == "clear" else client.delete_canvas(cid)
    if op == "delete":
        _cache_drop_canvas(cid)   # CX9: a deleted canvas must never resolve from cache
    return {**res, "op": op, "name": name}


def _canvas_clear(client: JuneClient, a: dict) -> dict:
    return _canvas_destructive(client, a, op="clear")


def _canvas_delete(client: JuneClient, a: dict) -> dict:
    return _canvas_destructive(client, a, op="delete")


# ── agent docs (Phase AM — June as the agent's instruction memory) ────────────
# Docs (CLAUDE.md-style standing instructions), skills (procedures with a
# when-to-use trigger), and learnings (append-only dated notes) are ORDINARY
# June pages in a dedicated docs canvas, marked by a __june_agent_doc__ sentinel
# first block (refresh.py owns the pure logic). Deliberate invariants:
#   * The registry is DERIVED from the page list + sentinel — never stored — so
#     the connector stays thin and the user edits their agent's memory in the app.
#   * These tools are canvas_scoped=False: their DEFAULT canvas is the docs
#     canvas (JUNE_DOCS_CANVAS, default "agent_docs"), not the connection
#     default, and an explicit per-call `canvas` still overrides. Only a WRITE
#     to the DEFAULT docs canvas may create it; reads degrade to empty.
#   * The periodic standing_docs digest (see run_tool) is the anti-forgetting
#     layer: enabled by the SERVER at startup (configure_docs), off by default
#     for library/test callers, and a digest failure never hurts the carrying
#     call. State is per-process = per-session for a stdio server.
from june_mcp import refresh as _refresh  # noqa: E402  (section-local import, like json above)

_DOCS_CFG = _refresh.DocsConfig()
_DOCS_STATE = _refresh.RefreshState(_DOCS_CFG.calls, _DOCS_CFG.minutes)
_DOCS_LOG = __import__("logging").getLogger("june_mcp")


def configure_docs(*, enabled: bool | None = None, canvas: str | None = None,
                   calls: int | None = None, minutes: float | None = None,
                   digest_chars: int | None = None) -> None:
    """Set this process's agent-memory posture (called once by __main__ from the
    validated env config; tests call it directly). Rebuilds the cadence state,
    so reconfiguring also resets the fire-on-first-call bootstrap."""
    global _DOCS_CFG, _DOCS_STATE
    c = _DOCS_CFG
    _DOCS_CFG = _refresh.DocsConfig(
        enabled=c.enabled if enabled is None else bool(enabled),
        canvas=(canvas or c.canvas).strip() or _refresh.DEFAULT_DOCS_CANVAS,
        calls=c.calls if calls is None else max(1, int(calls)),
        minutes=c.minutes if minutes is None else float(minutes),
        digest_chars=c.digest_chars if digest_chars is None else int(digest_chars))
    _DOCS_STATE = _refresh.RefreshState(_DOCS_CFG.calls, _DOCS_CFG.minutes)


def _docs_reset() -> None:                     # test seam (conftest resets around tests)
    global _DOCS_CFG, _DOCS_STATE
    _DOCS_CFG = _refresh.DocsConfig()
    _DOCS_STATE = _refresh.RefreshState(_DOCS_CFG.calls, _DOCS_CFG.minutes)


def _docs_canvas_resolve(client: JuneClient, wanted: str, *, create: bool = False
                         ) -> tuple[str, str, dict[str, str]]:
    """Resolve the DOCS canvas name → (canvas_id, name, notes) — the one name the
    connector may resolve DETERMINISTICALLY instead of failing closed on
    ambiguity. Rationale: duplicates of the docs canvas can only arise from the
    auto-create racing itself (two sessions' first doc_save in the same moment),
    and CX's fail-closed ambiguity rule exists to avoid guessing between USER
    workspaces — this canvas is feature-owned, and every session picking the
    same winner (lowest canvas_id) CONVERGES the race instead of bricking the
    doc tools until a human deletes a canvas. Duplicates are always noted.

    Create path self-heals: after creating, re-list; if the race made
    duplicates and ours lost, delete OUR canvas — it is empty by construction
    (verified before deleting) because every racer targets the same winner."""
    notes: dict[str, str] = {}
    try:
        cid, name = _canvas_target(client, wanted)     # cached, unambiguous fast path
        return cid, (name or _NAMES.get(cid) or wanted), notes
    except KeyError:
        pass                                           # not found OR ambiguous → resolve live

    def _matches() -> list[dict]:
        rows = client.list_canvases()
        return [r for r in rows if str(r.get("name", "")) == wanted]

    def _winner(matches: list[dict]) -> str:
        return str(min(matches, key=lambda r: str(r.get("canvas_id", "")))["canvas_id"])

    matches = _matches()
    if len(matches) > 1:
        cid = _winner(matches)
        ids = ", ".join(sorted(str(r["canvas_id"]) for r in matches))
        notes["docs_canvas_duplicates"] = (
            f"{len(matches)} canvases are named {wanted!r} ({ids}) — using "
            f"{cid} (deterministic: lowest id, so every session converges). "
            "Consolidate or delete the extras in the June app.")
        note_canvas_name(cid, wanted)
        return cid, wanted, notes                      # NOT cached: keep the check live
    if len(matches) == 1:
        cid = str(matches[0]["canvas_id"])
        note_canvas_name(cid, wanted)
        _cache_put(wanted, cid, wanted)
        return cid, wanted, notes
    if not create:
        raise KeyError(f"no canvas named {wanted!r} — june_doc_save creates it on first save")

    made = client.create_canvas(wanted)
    cid = str(made["canvas_id"])
    notes["docs_canvas_created"] = (f"created the {wanted!r} docs canvas — "
                                    "the agent-memory store now exists")
    matches = _matches()                               # post-create race check
    if len(matches) > 1:
        winner = _winner(matches)
        if winner != cid:
            # We lost the create race. Ours holds nothing (no write has targeted
            # it — every racer converges on the winner), but verify before the
            # one irreversible step; on ANY doubt, leave it and just note.
            try:
                if not (client.for_canvas(cid).list_pages(limit=1) or {}).get("pages"):
                    client.delete_canvas(cid)
                    _cache_drop_canvas(cid)
                    notes["docs_canvas_race"] = (
                        f"another session created {wanted!r} in the same moment — "
                        f"converged on {winner}; this session's empty duplicate was removed")
                else:
                    notes["docs_canvas_duplicates"] = (
                        f"duplicate {wanted!r} canvases exist — using {winner}; "
                        "consolidate in the June app")
            except Exception:  # noqa: BLE001 — healing is best-effort, never blocking
                notes["docs_canvas_duplicates"] = (
                    f"duplicate {wanted!r} canvases exist — using {winner}; "
                    "consolidate in the June app")
            cid = winner
        else:
            notes["docs_canvas_duplicates"] = (
                f"a concurrent session also created {wanted!r} — this one won "
                "deterministically; the other session removes its own duplicate")
    else:
        _cache_put(wanted, cid, wanted)
    note_canvas_name(cid, wanted)
    return cid, wanted, notes


def _doc_target(client: JuneClient, a: dict, *, create: bool = False
                ) -> tuple[JuneClient, str, str, dict[str, str]]:
    """The (docs-canvas client, canvas_id, canvas_name, notes) a doc tool acts
    through. Per-call ``canvas`` overrides; otherwise the configured docs canvas.
    An EXPLICIT canvas that is not the docs canvas keeps the strict fail-closed
    CX resolution (never guess between user workspaces); the docs canvas name —
    explicit or implied — goes through the converging resolver above. Missing
    DEFAULT docs canvas + ``create=True`` (write verbs only) → created."""
    wanted = str(a.get("canvas") or "").strip()
    explicit = bool(wanted)
    wanted = wanted or _DOCS_CFG.canvas
    if explicit and wanted != _DOCS_CFG.canvas:
        cid, name = _canvas_target(client, wanted)     # fail-closed, unchanged
        if name:
            note_canvas_name(cid, name)
        return client.for_canvas(cid), cid, (name or _NAMES.get(cid) or wanted), {}
    cid, name, notes = _docs_canvas_resolve(client, wanted, create=create)
    dclient = client.for_canvas(cid)
    if "docs_canvas_created" in notes:
        _seed_guide(dclient, notes)                    # self-hosting manual, best-effort
    return dclient, cid, name, notes


def _seed_guide(dclient: JuneClient, notes: dict[str, str]) -> None:
    """Seed the operating manual (Phase AM3) the moment the docs canvas is
    born, so June TEACHES every future agent how to use its memory — the
    conventions live in the store itself, listed in every registry and digest,
    editable by the user like any page. Best-effort by design: a seeding
    failure must never cost the save that triggered canvas creation."""
    seeded: list[str] = []
    seeds = (
        # (name, when_to_use, pinned, body) — the manual (on-demand) and the
        # JUNE-FIRST posture (pinned: rides every digest, so depending on June
        # by default is re-asserted all session, not hoped from the handshake).
        (_refresh.GUIDE_DOC_NAME, _refresh.GUIDE_DOC_WHEN, False,
         _refresh.GUIDE_DOC_BODY),
        (_refresh.JUNE_FIRST_NAME, _refresh.JUNE_FIRST_WHEN, True,
         _refresh.JUNE_FIRST_BODY),
    )
    for name, when, pinned, body in seeds:
        try:
            made = dclient.create_page(name)
            pid = str(made["page_id"])
            blocks = [{"block_type": "paragraph", "order": 1.0,
                       "text": _refresh.make_sentinel(name, "doc", when, pinned, 1)}]
            blocks += [{**b, "order": float(i + 2)} for i, b in
                       enumerate(_refresh.markdown_to_blocks(body))]
            dclient.save_blocks(pid, blocks, force=True)   # page created one line above
            seeded.append(name)
        except Exception as exc:  # noqa: BLE001
            _DOCS_LOG.debug("seeding %s skipped: %s", name, type(exc).__name__)
    if seeded:
        notes["seeded"] = (f"seeded {', '.join(seeded)} — the operating manual and "
                           "the pinned june-first posture; both are editable pages "
                           "the user owns")


def _doc_find(docs: list, name: str):
    return next((d for d in docs if d.name == name), None)


def _with_notes(result: dict, *note_dicts: dict | None) -> dict:
    """Attach operational notes (canvas duplicates, scan bounds, collisions) to a
    result under ``_notes`` — the same visible-never-silent channel the digest
    uses. ``_clamped`` stays reserved for genuine input clamps."""
    merged: dict[str, str] = {}
    for nd in note_dicts:
        if nd:
            merged.update(nd)
    if not merged:
        return result
    existing = dict(result.get("_notes") or {})
    existing.update(merged)
    return {**result, "_notes": existing}


def _doc_name_arg(a: dict, tool: str, key: str = "name") -> str:
    name = str(a.get(key, "")).strip().lower()
    if not _refresh.valid_name(name):
        raise ToolInputError(
            f"{tool} needs '{key}': a lowercase slug (a-z 0-9 . _ -, max "
            f"{_refresh.MAX_DOC_NAME} chars, starting alphanumeric) — got {name!r}")
    return name


def _doc_row(d) -> dict:
    return {"name": d.name, "kind": d.kind, "title": d.title,
            "when_to_use": d.when_to_use, "pinned": d.pinned,
            "page_id": d.page_id, "updated_at": d.updated_at, "v": d.v}


def _doc_list(client: JuneClient, a: dict) -> dict:
    try:
        dclient, cid, cname, cnotes = _doc_target(client, a)
    except KeyError:
        if str(a.get("canvas") or "").strip():
            raise                                   # an explicitly named canvas must exist
        return {"docs": [], "count": 0, "setup": _refresh.SETUP_NOTE}
    docs, notes = _refresh.derive_registry(dclient)
    out: dict = {"docs": [_doc_row(d) for d in docs], "count": len(docs),
                 "canvas": cid, "canvas_name": cname}
    if not docs:
        out["setup"] = _refresh.SETUP_NOTE
    return _with_notes(out, cnotes, notes)


def _doc_get(client: JuneClient, a: dict) -> dict:
    name = _doc_name_arg(a, "june_doc_get")
    dclient, cid, cname, cnotes = _doc_target(client, a)
    docs, _notes = _refresh.derive_registry(dclient)
    d = _doc_find(docs, name)
    if d is None:
        known = ", ".join(sorted(x.name for x in docs)) or "none saved yet"
        raise ToolInputError(f"no agent doc named {name!r} (known: {known}) — "
                             "june_doc_save creates one")
    return _with_notes({**_doc_row(d), "body": d.body,
                        "canvas": cid, "canvas_name": cname}, cnotes)


def _doc_save(client: JuneClient, a: dict) -> dict:
    name = _doc_name_arg(a, "june_doc_save")
    text = str(a.get("text", ""))
    if not text.strip():
        raise ToolInputError("june_doc_save needs non-empty 'text' (the doc body, markdown)")
    kind = str(a.get("kind", "doc")).strip().lower() or "doc"
    if kind not in _refresh.DOC_KINDS:
        raise ToolInputError(f"june_doc_save 'kind' must be one of "
                             f"{'/'.join(_refresh.DOC_KINDS)} — got {kind!r}")
    when = str(a.get("when_to_use", "")).strip()[:_refresh.MAX_WHEN_TO_USE]
    if kind == "skill" and not when:
        raise ToolInputError(
            "a skill needs 'when_to_use' — the one-line trigger shown in every "
            "standing_docs digest that tells an agent when to load the full body")
    pinned = bool(a.get("pinned", False))
    notes: dict[str, str] = {}
    if len(text) > _refresh.MAX_DOC_CHARS:
        notes["text"] = f"{len(text)} chars → {_refresh.MAX_DOC_CHARS} (truncated)"
        text = text[:_refresh.MAX_DOC_CHARS]

    dclient, cid, cname, cnotes = _doc_target(client, a, create=True)
    docs, _reg_notes = _refresh.derive_registry(dclient)
    existing = _doc_find(docs, name)
    v = (existing.v + 1) if existing else 1
    body_blocks = _refresh.markdown_to_blocks(text)
    # Same block-count cap as every other page write (_to_blocks): a char-legal
    # body of thousands of one-line list items must not become an oversized save
    # the service may refuse — or accept into an unusable page. Visible, never silent.
    if len(body_blocks) > MAX_PAGE_BLOCKS - 1:
        notes["blocks"] = f"{len(body_blocks)} blocks → {MAX_PAGE_BLOCKS - 1} (truncated)"
        body_blocks = body_blocks[:MAX_PAGE_BLOCKS - 1]
    blocks = [{"block_type": "paragraph",
               "text": _refresh.make_sentinel(name, kind, when, pinned, v), "order": 1.0}]
    blocks += [{**b, "order": float(i + 2)} for i, b in enumerate(body_blocks)]

    title = str(a.get("title", "")).strip()
    warning: str | None = None
    op_notes: dict[str, str] = {}                  # operational notes (_notes channel)
    if existing is None:
        made = dclient.create_page(title or name)
        pid = str(made["page_id"])
        # force=True is the _page_create shape: a page created one line above holds
        # nothing anyone else wrote.
        dclient.save_blocks(pid, blocks, force=True)
        created = True
        # CONCURRENT-CREATE CONVERGENCE. Two sessions saving the same NEW name in
        # the same moment each create a page; every session then applies the same
        # deterministic winner rule (lowest page_id), so exactly one copy
        # survives. The loser removes ITS OWN page — never the rival's — and
        # reports the truth: this content was NOT kept, read-and-merge. Silent
        # split-brain (two half-registries) is the failure this prevents.
        all_docs, _dup_notes = _refresh.derive_registry(dclient, dedupe=False)
        rivals = [d for d in all_docs if d.name == name]
        if len(rivals) > 1:
            winner = min(rivals, key=lambda d: d.page_id)
            if winner.page_id != pid:
                try:
                    dclient.delete_page(pid)
                except Exception as heal_exc:  # noqa: BLE001 — healing is best-effort
                    _DOCS_LOG.debug("collision heal: duplicate page not removed (%s)",
                                    type(heal_exc).__name__)
                return _refusal(
                    "doc_name_collision", winner.page_id,
                    f"NOT saved — another session created agent doc {name!r} at the "
                    "same moment and its copy wins deterministically. This call's "
                    f"duplicate page was removed. Call june_doc_get('{name}') and "
                    "merge this content into the surviving doc with june_doc_save.",
                    name=name)
            op_notes["collision"] = (f"a concurrent session also created {name!r} — this "
                                     "copy won deterministically; the other converges on it")
    else:
        pid = existing.page_id
        rev = str(a.get("expected_updated_at") or "").strip() or existing.updated_at
        try:
            dclient.save_blocks(pid, blocks, expected_updated_at=rev, force=rev is None)
        except PageRevisionConflict:
            return _refusal(
                "doc_changed_since_read", pid,
                f"REFUSED — agent doc {name!r} changed while this save was being "
                "prepared (another session, or the user editing the page), so it was "
                "NOT applied. Call june_doc_get again and revise against the current "
                "body — do not resend this payload.", name=name)
        if title and title != existing.title:
            # The body save above ALREADY LANDED. A rename failure must not
            # surface as an error the agent reads as "save failed" (whose retry
            # would then bounce off the now-stale revision guard) — report the
            # truth instead: written, title unchanged.
            try:
                dclient.rename_page(pid, title)
            except Exception:  # noqa: BLE001
                warning = (f"the body was SAVED, but renaming the page to {title!r} "
                           "failed — the doc keeps its old title. Rename it in the "
                           "June app, or repeat this call later.")
        created = False
    out = {"name": name, "kind": kind, "page_id": pid, "created": created,
           "pinned": pinned, "v": v, "blocks_written": len(blocks) - 1,
           "canvas": cid, "canvas_name": cname}
    if warning:
        out["warning"] = warning
    return _with_notes(_noted(out, notes), cnotes, op_notes)


def _doc_delete(client: JuneClient, a: dict) -> dict:
    name = _doc_name_arg(a, "june_doc_delete")
    dclient, cid, cname, _cnotes = _doc_target(client, a)
    docs, _notes = _refresh.derive_registry(dclient)
    d = _doc_find(docs, name)
    if d is None:
        known = ", ".join(sorted(x.name for x in docs)) or "none"
        raise ToolInputError(f"no agent doc named {name!r} to delete (known: {known})")
    confirm = str(a.get("confirm", "")).strip()
    if not confirm:
        token = _CONFIRMS.mint("doc_delete", d.page_id)
        return {"pending": True, "confirm_token": token, "name": name,
                "page_id": d.page_id, "canvas": cid, "canvas_name": cname,
                "warning": (f"This permanently removes agent doc {name!r} ({d.kind}"
                            f"{', PINNED — it rides every digest' if d.pinned else ''}) "
                            "from the agent's standing instructions. Nothing was "
                            "deleted. To proceed, call june_doc_delete again with "
                            "this confirm_token (single-use, expires in ~2 minutes).")}
    ok, why = _CONFIRMS.consume(confirm, "doc_delete", d.page_id)
    if not ok:
        raise ToolInputError(f"june_doc_delete refused: {why}. Nothing was deleted.")
    res = dclient.delete_page(d.page_id)
    return {"deleted": True, "name": name, "page_id": d.page_id,
            "blocks_deleted": res.get("blocks_deleted"),
            "canvas": cid, "canvas_name": cname}


def _learn(client: JuneClient, a: dict) -> dict:
    text = str(a.get("text", "")).strip()
    if not text:
        raise ToolInputError("june_learn needs non-empty 'text' — the lesson worth keeping")
    docname = str(a.get("doc", "") or "learnings").strip().lower()
    if not _refresh.valid_name(docname):
        raise ToolInputError(f"june_learn 'doc' must be a valid doc name — got {docname!r}")
    dclient, cid, cname, cnotes = _doc_target(client, a, create=True)
    docs, _notes = _refresh.derive_registry(dclient)
    d = _doc_find(docs, docname)
    created = False
    notes: dict[str, str] = {}
    if d is None:
        made = dclient.create_page(docname)
        pid = str(made["page_id"])
        dclient.save_blocks(pid, [{
            "block_type": "paragraph",
            "text": _refresh.make_sentinel(docname, "learnings", "", False, 1),
            "order": 1.0}], force=True)
        created = True
        # CONCURRENT-CREATE CONVERGENCE — and unlike doc_save, learn can heal
        # FULLY: the payload is one append-only entry, so the loser deletes its
        # own duplicate page and appends onto the winner instead. No content
        # conflict is possible; the entry simply lands where everyone converged.
        all_docs, _dups = _refresh.derive_registry(dclient, dedupe=False)
        rivals = [x for x in all_docs if x.name == docname]
        if len(rivals) > 1:
            winner = min(rivals, key=lambda x: x.page_id)
            if winner.page_id != pid:
                try:
                    dclient.delete_page(pid)
                except Exception as heal_exc:  # noqa: BLE001 — healing is best-effort
                    _DOCS_LOG.debug("collision heal: duplicate page not removed (%s)",
                                    type(heal_exc).__name__)
                pid = winner.page_id
                created = False
                notes["collision"] = (f"another session created {docname!r} in the same "
                                      "moment — converged on its copy; the entry was "
                                      "appended there")
                if winner.kind != "learnings":
                    raise ToolInputError(
                        f"june_learn appends only to 'learnings' docs — a concurrent "
                        f"session created {docname!r} as a {winner.kind}, which wins "
                        "deterministically. Nothing was appended (this session's "
                        "duplicate was removed); pick another learnings doc name.")
    elif d.kind != "learnings":
        # The Dev Practices lesson (2026-08-15), enforced structurally: raw incident
        # notes never auto-append into a curated doc/skill. Promotion is deliberate.
        raise ToolInputError(
            f"june_learn appends only to 'learnings' docs — {docname!r} is a "
            f"{d.kind}. Save the lesson to a learnings doc, and promote it into "
            f"{docname!r} deliberately with june_doc_save if it belongs there.")
    else:
        pid = d.page_id
    stamp = _time.strftime("%Y-%m-%d")
    res = dclient.append_blocks(pid, [{"block_type": "bulleted",
                                       "text": f"[{stamp}] {text}"}])
    total = res.get("blocks_total")
    if total is None:
        total = len(res.get("blocks") or []) or None
    return _with_notes({"doc": docname, "page_id": pid, "appended": 1,
                        "created_doc": created, "blocks_total": total,
                        "canvas": cid, "canvas_name": cname}, cnotes, notes)


def _docs_refresh_tool(client: JuneClient, a: dict) -> dict:
    try:
        dclient, cid, cname, cnotes = _doc_target(client, a)
    except KeyError:
        if str(a.get("canvas") or "").strip():
            raise
        _DOCS_STATE.fired()
        return {"docs": [], "setup": _refresh.SETUP_NOTE}
    docs, notes = _refresh.derive_registry(dclient)
    digest = _refresh.build_digest(docs, cap_chars=_DOCS_CFG.digest_chars)
    _DOCS_STATE.fired()                            # an explicit refresh resets the cadence
    if digest is None:
        return _with_notes({"docs": [], "canvas": cid, "canvas_name": cname,
                            "setup": _refresh.SETUP_NOTE}, cnotes, notes)
    return _with_notes({**digest, "canvas": cid, "canvas_name": cname}, cnotes, notes)


# Doc tools never trigger the periodic digest — their results ARE doc content.
_DOCS_TOOL_NAMES = {"june_doc_list", "june_doc_get", "june_doc_save",
                    "june_doc_delete", "june_learn", "june_docs_refresh"}


def _standing_docs(client: JuneClient) -> dict | None:
    """Build the periodic digest, honoring the cadence contract: the caller has
    already reserved the build via ``_DOCS_STATE.tick()``; every path here ends
    in exactly one ``fired()`` or ``failed()`` — the ``finally`` makes that hold
    even for a BaseException (cancellation, interpreter shutdown), so a torn-down
    build can never leave the reservation stuck and kill injection for the rest
    of the session. A missing docs canvas counts as 'legitimately idle' (full
    quiet interval, no nagging retries); anything else is a failure → short
    retry. The registry scan runs under DIGEST_BUILD_BUDGET_SECONDS so the
    digest can never stall the tool call it rides on. Never raises Exception."""
    settled = False
    try:
        # The converging resolver, not the strict one: duplicate docs canvases
        # (a healed-in-progress create race) must degrade to "use the winner",
        # never to injection silently dying while docs exist.
        cid, _name, cnotes = _docs_canvas_resolve(client, _DOCS_CFG.canvas)
        docs, notes = _refresh.derive_registry(
            client.for_canvas(cid),
            budget_seconds=_refresh.DIGEST_BUILD_BUDGET_SECONDS)
        notes = {**cnotes, **notes}
        digest = _refresh.build_digest(docs, cap_chars=_DOCS_CFG.digest_chars)
        _DOCS_STATE.fired()
        settled = True
        if digest is not None and notes:
            digest = {**digest, "_notes": notes}    # partial scan is visible, never silent
        return digest
    except KeyError:
        _DOCS_STATE.fired()                        # feature unused on this endpoint
        settled = True
        return None
    except Exception as exc:  # noqa: BLE001 — the carrying call must never pay
        import httpx as _httpx
        if (isinstance(exc, _httpx.HTTPStatusError)
                and exc.response.status_code in (404, 405)):
            # STRUCTURAL absence, not a transient failure: the endpoint predates
            # /v1/canvases or has pages gated off (JUNE_PAGES unset). Retrying
            # every 60s would tax old endpoints forever for a feature they
            # cannot serve — treat like the missing-canvas case: quiet interval.
            _DOCS_STATE.fired()
        else:
            _DOCS_STATE.failed()
        settled = True
        _DOCS_LOG.debug("standing_docs digest skipped: %s", type(exc).__name__)
        return None
    finally:
        if not settled:                            # BaseException path: release, retry soon
            _DOCS_STATE.failed()


# ── repo sync (Phase AM2 — export pages/docs to a fenced repo, commit-only git) ──
# Operator opt-in via JUNE_EXPORT_ROOT (the JUNE_FILES_ROOT consent shape): the
# three tools are absent from the surface without it AND their handlers re-check
# at call time (two fences). export.py holds the pure logic + the git safety
# rules: pathspec-limited commit, never push, never overwrite an unmanaged file,
# never delete anything.
from june_mcp import export as _export  # noqa: E402  (section-local import)


def _export_required_root():
    root = _export.export_root()
    if root is None:
        raise ToolInputError(
            "repo export is disabled: the operator must set JUNE_EXPORT_ROOT to "
            "the repository directory june-mcp may write files into")
    if not root.is_dir():
        raise ToolInputError(f"{_export.ENV_EXPORT_ROOT} is not an existing "
                             "directory — fix the spawn environment")
    return root


def _export_fenced(root, rel: str):
    """fenced() with its refusal surfaced as agent-readable text (a plain
    ValueError would collapse to the generic redacted message)."""
    try:
        return _export.fenced(root, rel)
    except ValueError as exc:
        raise ToolInputError(f"refused: {exc}") from exc


def _export_finish(root, manifest, manifest_before: str, written: list[str],
                   message: str) -> dict:
    """Shared tail: persist the manifest when it changed, then the (optional,
    pathspec-limited) commit of exactly what this call wrote."""
    rels = list(written)
    if json.dumps(manifest, sort_keys=True) != manifest_before:
        rels.append(_export.manifest_save(root, manifest))
    if not rels:
        return {"git": "nothing to commit"} if _export.git_enabled() else {}
    if _export.git_enabled():
        return _export.git_commit(root, rels, message)
    return {"git": "off (JUNE_EXPORT_GIT unset) — files written, commit is yours"}


def _docs_export(client: JuneClient, a: dict) -> dict:
    root = _export_required_root()
    check = bool(a.get("check", False))
    try:
        dclient, cid, cname, cnotes = _doc_target(client, a)
    except KeyError:
        if str(a.get("canvas") or "").strip():
            raise
        return {"written": [], "unchanged": [], "note":
                f"no {_DOCS_CFG.canvas!r} canvas exists yet — nothing to export"}
    docs, reg_notes = _refresh.derive_registry(dclient)
    manifest = _export.manifest_load(root)
    before = json.dumps(manifest, sort_keys=True)
    written: list[str] = []
    unchanged: list[str] = []
    refused: list[str] = []
    drift: list[dict] = []
    for d in docs:
        rel = f"{_export.export_dir()}/{d.name}.md"
        content = _export.render_managed_file(
            {"kind": d.kind, "name": d.name, "title": d.title, "page_id": d.page_id,
             "canvas": cid, "v": d.v, "updated_at": d.updated_at,
             "when_to_use": d.when_to_use, "pinned": d.pinned}, d.body)
        plan = _export.plan_write(root, rel, content)
        if plan["status"] == "refused_unmanaged":
            refused.append(plan["reason"])
            continue
        if plan["status"] == "unchanged":
            unchanged.append(rel)
        elif check:
            drift.append({"path": rel, "status": plan["status"],
                          "doc": d.name})
        else:
            _export.apply_write(plan, content)
            written.append(rel)
        _export.manifest_note(manifest, rel, mode="file", page_id=d.page_id,
                              canvas=cid, kind=d.kind, updated_at=d.updated_at,
                              content_hash=_export.sha256_text(content))
    if check:
        out = {"check": True, "current": not drift and not refused,
               "drift": drift, "unchanged": len(unchanged), "refused": refused,
               "canvas": cid, "canvas_name": cname}
        return _with_notes(out, cnotes, reg_notes)
    out = {"written": written, "unchanged": len(unchanged), "refused": refused,
           "docs": len(docs), "canvas": cid, "canvas_name": cname}
    out.update(_export_finish(root, manifest, before, written,
                              f"june-export: {len(written)} agent doc(s)"))
    return _with_notes(out, cnotes, reg_notes)


def _page_export(client: JuneClient, a: dict) -> dict:
    root = _export_required_root()
    pid = str(a.get("page_id", "")).strip()
    if not pid:
        raise ToolInputError("june_page_export needs 'page_id' (from june_page_list)")
    check = bool(a.get("check", False))
    detail = client.get_page(pid) or {}
    title = str(detail.get("title") or pid)
    body = _refresh.blocks_to_markdown(detail.get("blocks") or [])
    updated = detail.get("updated_at")
    section = str(a.get("section", "")).strip()
    rel = str(a.get("path", "")).strip()
    manifest = _export.manifest_load(root)
    before = json.dumps(manifest, sort_keys=True)
    eff_canvas = client.canvas or ""

    if section:
        # SURGICAL MODE — the sanctioned way into a human-owned file (KNOWHOW.md,
        # CHANGELOG.md): only the text between this section's markers is touched.
        if not rel:
            raise ToolInputError("a section export needs 'path' — the file that "
                                 "holds the section (e.g. KNOWHOW.md)")
        name = _export.slugify(section)
        target = _export_fenced(root, rel)
        existing = target.read_text(encoding="utf-8") if target.exists() else None
        try:
            new_text = _export.section_replace(existing, name, body)
        except ValueError as exc:
            raise ToolInputError(f"refused: {exc}") from exc
        status = ("unchanged" if existing == new_text
                  else ("update" if existing is not None else "create"))
        if not check and status != "unchanged":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(new_text, encoding="utf-8")
        _export.manifest_note(manifest, rel, mode="section", page_id=pid,
                              canvas=eff_canvas, kind="page", updated_at=updated,
                              content_hash=_export.sha256_text(body), section=name)
        out = {"page_id": pid, "title": title, "path": rel, "mode": "section",
               "section": name, "status": status}
    else:
        rel = rel or f"{_export.export_dir()}/pages/{_export.slugify(title)}.md"
        _export_fenced(root, rel)                  # fence before any planning
        content = _export.render_managed_file(
            {"kind": "page", "title": title, "page_id": pid,
             "canvas": eff_canvas, "updated_at": updated}, body)
        plan = _export.plan_write(root, rel, content)
        if plan["status"] == "refused_unmanaged":
            return _refusal("would_overwrite_unmanaged_file", pid,
                            plan["reason"], path=rel)
        out = {"page_id": pid, "title": title, "path": rel, "mode": "file",
               "status": plan["status"]}
        if not check and plan["status"] != "unchanged":
            _export.apply_write(plan, content)
        _export.manifest_note(manifest, rel, mode="file", page_id=pid,
                              canvas=eff_canvas, kind="page", updated_at=updated,
                              content_hash=_export.sha256_text(content))

    if check:
        return {**out, "check": True, "current": out["status"] == "unchanged"}
    wrote = [rel] if out["status"] != "unchanged" else []
    out.update(_export_finish(root, manifest, before, wrote,
                              f"june-export: {title}"))
    return out


def _page_import(client: JuneClient, a: dict) -> dict:
    root = _export_required_root()
    rel = str(a.get("path", "")).strip()
    if not rel:
        raise ToolInputError("june_page_import needs 'path' — a june-managed file "
                             "under JUNE_EXPORT_ROOT")
    target = _export_fenced(root, rel)
    if not target.is_file():
        raise ToolInputError(f"no such file under {_export.ENV_EXPORT_ROOT}: {rel}")
    meta, body = _export.parse_frontmatter(target.read_text(encoding="utf-8"))
    if meta is None:
        raise ToolInputError(
            f"{rel} is not a june-managed file (no frontmatter marker) — only "
            "files written by june_page_export / june_docs_export round-trip; "
            "managed SECTIONS are export-only")
    pid = str(meta.get("page_id") or "").strip()
    if not pid:
        raise ToolInputError(f"{rel} has no page_id in its frontmatter — re-export it first")
    canvas_id = str(meta.get("canvas") or "").strip()
    eff = client.for_canvas(canvas_id) if canvas_id else client

    body_blocks = _refresh.markdown_to_blocks(body)
    if len(body_blocks) > MAX_PAGE_BLOCKS - 1:
        body_blocks = body_blocks[:MAX_PAGE_BLOCKS - 1]
    blocks: list[dict] = []
    kind = str(meta.get("kind") or "page")
    name = str(meta.get("name") or "")
    if kind in _refresh.DOC_KINDS and _refresh.valid_name(name):
        # An agent doc's identity lives in its sentinel FIRST block — a round-trip
        # that dropped it would silently delete the doc from the registry.
        pinned = str(meta.get("pinned", "")).strip().lower() in {"1", "true", "yes"}
        try:
            v = max(1, int(meta.get("v", 1))) + 1
        except (TypeError, ValueError):
            v = 1
        blocks.append({"block_type": "paragraph", "order": 1.0,
                       "text": _refresh.make_sentinel(
                           name, kind, str(meta.get("when_to_use") or ""), pinned, v)})
    blocks += [{**b, "order": float(i + len(blocks) + 1)}
               for i, b in enumerate(body_blocks)]

    rev = str(meta.get("updated_at") or "").strip() or None
    try:
        saved = eff.save_blocks(pid, blocks, expected_updated_at=rev, force=rev is None)
    except PageRevisionConflict:
        return _refusal(
            "page_changed_in_june", pid,
            f"NOT imported — the June page behind {rel} changed after this file "
            "was exported (another session, or the user editing in the app). "
            "Export it again (june_page_export), merge locally, then import.",
            path=rel)
    # Keep the repo file current: refresh its frontmatter revision so the next
    # import has a valid token, and record the new state in the manifest.
    manifest = _export.manifest_load(root)
    before = json.dumps(manifest, sort_keys=True)
    new_meta = {**meta, "updated_at": saved.get("updated_at") or "",
                "v": (v if kind in _refresh.DOC_KINDS and name else meta.get("v"))}
    new_meta.pop(_export.FRONTMATTER_KEY, None)
    content = _export.render_managed_file(new_meta, body)
    target.write_text(content, encoding="utf-8")
    _export.manifest_note(manifest, rel, mode="file", page_id=pid,
                          canvas=canvas_id, kind=kind,
                          updated_at=saved.get("updated_at"),
                          content_hash=_export.sha256_text(content))
    out = {"page_id": pid, "path": rel, "imported": True,
           "blocks_written": len(blocks)}
    out.update(_export_finish(root, manifest, before, [rel],
                              f"june-sync: import {rel}"))
    return out


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    handler: Callable[[JuneClient, dict], Any]
    input_schema: dict
    writes: bool = field(default=False)   # True ⇒ hidden when the server is read-only
    canvas_scoped: bool = field(default=True)   # CX5: takes the per-call ``canvas`` arg
    #                                             (False = canvas-MANAGEMENT tools, which
    #                                             name their target explicitly already)
    available: bool = field(default=True)  # False ⇒ capability absent in this install:
    #                                        hidden from the surface AND refused if
    #                                        addressed directly (same two-fence shape
    #                                        as the read-only posture)


def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


_INT = {"type": "integer"}
_STR = {"type": "string"}
_NUM = {"type": "number"}
_BOOL = {"type": "boolean"}
_ARR = {"type": "array"}

TOOLS: list[Tool] = [
    Tool(
        "june_answer",
        "Answer a question from June's shared knowledge graph — grounded in stored "
        "evidence, with citations, and it abstains rather than guessing when the graph "
        "doesn't know. Use PROACTIVELY, without the user asking: whenever a question "
        "or task touches the user's work, people, projects, documents or past "
        "decisions, check June BEFORE answering from your own memory or claiming "
        "ignorance — the graph often knows what you don't. Use june_context "
        "instead when you want raw material to reason over yourself, and june_search when "
        "you only need ranked matching items. May take longer than other tools (it runs "
        "one LLM synthesis). Returns {answer, citations, used_edge_ids, degraded, mode}; "
        "an empty answer or 'abstain' in degraded means the graph has no grounded answer.",
        _answer,
        _schema({"query": _STR, "limit": _INT, "token_budget": _INT, "max_items": _INT,
                 "multihop": {**_BOOL, "description": "decompose multi-hop questions"},
                 "seeds": _ARR}, ["query"]),
    ),
    Tool(
        "june_search",
        "Fused retrieval over the knowledge graph: lexical + dense + graph signals in "
        "one ranked list. Use when you need matching items (nodes/snippets with scores "
        "and provenance) — e.g. to find entities or check what the graph holds on a "
        "topic; use june_answer for a finished cited answer, june_context for a "
        "prompt-ready pack. Returns {items[], degraded_lanes, …}.",
        _search,
        _schema({"query": _STR, "limit": _INT, "seeds": _ARR,
                 "min_confidence": _NUM}, ["query"]),
    ),
    Tool(
        "june_enumerate",
        "Exhaustive structured retrieval: return EVERY node matching a predicate "
        "(terms / regex / node_types / subtype) — not a top-k slice. Use for "
        "aggregation questions like 'list ALL customers/incidents/…' where "
        "june_search's ranked window could miss members; then reason over the "
        "complete list. Returns all matches up to cap (default 500).",
        _enumerate,
        _schema({"terms": _ARR, "regex": _STR, "node_types": _ARR,
                 "subtype": _STR, "cap": _INT}),
    ),
    Tool(
        "june_context",
        "One call → a ready-to-use context pack: ranked evidence folded to canonical "
        "entities (aliases merged), trimmed to a token budget. Use when you want June's "
        "knowledge as raw material inside YOUR reasoning or a long draft; use "
        "june_answer when you want June to produce the answer itself. Returns "
        "{items[], budget, …} sized to token_budget.",
        _context,
        _schema({"query": _STR, "token_budget": _INT, "max_items": _INT,
                 "limit": _INT, "seeds": _ARR}, ["query"]),
    ),
    Tool(
        "june_neighborhood",
        "The 1-hop edges around one node — who/what connects directly to it. Use after "
        "june_search gave you a node_id and you want its immediate relations; use "
        "june_subgraph for multi-hop expansion. Requires node_id + node_type from a "
        "prior result. Returns {edges[], …}.",
        _neighborhood,
        _schema({"node_id": _STR, "node_type": _STR, "direction": _STR,
                 "limit": _INT}, ["node_id", "node_type"]),
    ),
    Tool(
        "june_subgraph",
        "Depth-N neighbourhood around a node (multi-hop expansion, bounded). Use to map "
        "a cluster of related entities around a known node; use june_neighborhood for "
        "just the direct edges. Requires node_id + node_type from a prior result. "
        "Returns {nodes[], edges[], …}; depth ≤ 3.",
        _subgraph,
        _schema({"node_id": _STR, "node_type": _STR, "depth": _INT,
                 "max_edges": _INT}, ["node_id", "node_type"]),
    ),
    Tool(
        "june_remember",
        "Save new information into the shared graph by writing text: June extracts "
        "entities and relations server-side and links them to what it already knows "
        "(on Pro endpoints the richer entity/edge engines run automatically; the "
        "result reports which engine ran). Use PROACTIVELY, in the same turn, whenever "
        "the user states a fact, decision, update or preference worth persisting — "
        "they should never have to say 'remember that': meeting notes, a status "
        "change, a choice made. Plain text or markdown, up to ~64k chars. Returns write "
        "counts — cite them, don't echo the text back. Prefer this over june_ingest "
        "unless you must write explicit graph structure.",
        _remember,
        _schema({"text": _STR, "format": {**_STR, "description": "markdown|text|html"},
                 "source_app": _STR}, ["text"]),
        writes=True,
    ),
    Tool(
        "june_ingest",
        "Advanced write: push explicit graph structure (node rows + edge proposals) "
        "exactly as given. Use ONLY when you already have structured nodes/edges with "
        "ids and kinds — for ordinary 'remember this' information, june_remember is "
        "the right verb (it extracts structure for you). Returns write counts.",
        _ingest,
        _schema({"nodes": _ARR, "proposals": _ARR, "idempotency_key": _STR}),
        writes=True,
    ),
    Tool(
        "june_ingest_file",
        "Upload ONE local file (pdf, docx, xlsx, csv, html, md, images, audio) from "
        "the operator-approved folder into the graph — the server picks the right "
        "reader, extracts (richer engines on Pro endpoints), and links it in. Use "
        "when the user points you at a document to remember; 'path' is relative to "
        "the approved folder (JUNE_FILES_ROOT). One file per call, ≤25MB. Returns "
        "per-file status + write counts + which engine ran.",
        _ingest_file,
        _schema({"path": _STR}, ["path"]),
        writes=True,
        available=bool(os.environ.get("JUNE_FILES_ROOT", "").strip()),
    ),
    Tool(
        "june_enrich",
        "Pro: re-extract THIS canvas's existing artifacts with the richer engine, as "
        "a background job (idempotent — a second run writes 0 new). Use after a Pro "
        "upgrade to backfill memories that were written on the free floor, or after "
        "many june_remember writes. Call with no args to start (returns job_id; 409 "
        "if one is already running; 403 on free endpoints), then call again with "
        "{job: <job_id>} to check progress. Returns {job_id, state, total, "
        "processed, nodes, edges, errors}.",
        _enrich,
        _schema({"job": _STR}),
        writes=True,
    ),
    Tool(
        "june_resolve",
        "Maintenance: run cross-format entity resolution over the canvas — merges "
        "duplicate entities via reversible same_as edges (runs server-side, "
        "server-bounded scan). Default strong_only=true is conservative "
        "(deterministic signals only); pass strong_only=false to also use the "
        "fuzzy tier — which upgrades to SEMANTIC matching on Pro endpoints. Use "
        "once after a batch of june_remember/june_ingest writes, not per question; "
        "reads are already resolution-aware. Returns {same_as_written, groups, "
        "candidates}.",
        _resolve,
        _schema({"strong_only": _BOOL, "min_confidence": _NUM}),
        writes=True,
    ),
    Tool(
        "june_page_list",
        "List the PAGES (graph-native documents) in the current canvas. Use to see what "
        "documents already exist before creating or editing one — e.g. to find a page to "
        "update, or avoid duplicating one. Returns {pages:[{page_id, title, created_at, "
        "updated_at, pinned, pinned_ms, group}], has_more, next_offset} — pinned/group are "
        "the USER'S own organization of their pages list (newer engines; older ones omit "
        "them): when presenting pages, show pinned ones first and respect group labels. "
        "To read a page's content use june_page_get.",
        _page_list,
        _schema({"limit": _INT, "offset": _INT}),
    ),
    Tool(
        "june_page_get",
        "Read one page and its ordered blocks (its full content). **CALL THIS BEFORE ANY "
        "OPERATION ON AN EXISTING PAGE** — write, delete, or a rename you want to describe "
        "accurately — in the SAME turn, immediately before, and act on what it returns. This is "
        "not conditional on 'needing' the content: a page holds blocks written by other sessions, "
        "other hosts and the user's own editor, none of which are in your context, and a write "
        "built without reading DELETES exactly those. Use it also to show a page back to the "
        "user, and to VERIFY after a write — compare the block count to what you sent, because a "
        "success result is not proof the page holds it. Requires a page_id from june_page_list or "
        "june_page_create. Returns {page_id, title, blocks:[{block_id, block_type, text, order}]}.",
        _page_get,
        _schema({"page_id": _STR}, ["page_id"]),
    ),
    Tool(
        "june_page_create",
        "Create a NEW page — a rich document OR a laid-out dashboard — in the current canvas and "
        "write its content in one call. PROACTIVE USE: when a user's material could become a page "
        "(notes, a plan, a summary, a dashboard), OFFER to build it, then act — pick sensible "
        "structure yourself (or give the user 2-4 options and build the one they choose); the user "
        "never has to specify block-by-block. `blocks` is an ORDERED list; each item is one of:\n"
        "• TEXT — {type, text}; type ∈ paragraph, heading_1..3, bulleted, numbered, todo, "
        "todo_done, quote, callout, code, divider.\n"
        "• TABLE — a {type:'paragraph', text:'| A | B |\\n| --- | --- |\\n| 1 | 2 |'} GitHub-Markdown "
        "table; the editor renders a real grid.\n"
        "• LIVE VIEW — {type:'view', node_types:[entity|identity|decision|artifact], "
        "kind:'table'|'board'|'calendar', cap, terms?, subtype?}; renders a LIVE query over the "
        "graph (stays current as knowledge changes) — this is what makes a real dashboard.\n"
        "• MEDIA (display-only, NOT added to the graph) — {type:'image', url, alt?} or "
        "{type:'embed', url, label?}; renders an image or link inline for a richer page. Use for "
        "generated or referenced media; http/https/data:image only.\n"
        "• INTERACTIVE CONTROLS (plain-text conventions, app 0.0.11+) — a paragraph whose text is "
        "exactly '[select: Todo | *In progress | Done]' renders as a DROPDOWN (strictly one "
        "choice); '[multi: *urgent | blocked | frontend]' as a MULTI-SELECT; '*' marks the "
        "selected option(s). Table CELLS can hold the same dropdowns (escape the pipes inside a "
        "cell: '[select: A \\| *B]') and also to-do cells: a cell starting '[] task' / '[x] task' "
        "renders a real checkbox. Users flip these by clicking; reading the page back shows the "
        "current state via the same markers — so use them for status fields, priorities, tags, "
        "and per-row task tracking instead of static text. End an option or to-do with a "
        "'#color' tag to color it (app 0.0.11+): '[select: fail #red \\| pass #green \\| *todo "
        "#blue]', '- [ ] rotate the secret #red' — palette (18): slate gray brown red rose "
        "orange amber yellow lime green teal cyan sky blue indigo purple fuchsia pink "
        "(older apps render the original 8; unknown tags stay literal text); the tag colors "
        "the rendered chip/row and stays in the text.\n"
        "INLINE MARKDOWN (app 0.0.11+) — block text renders inline emphasis styled: **bold**, "
        "*italic*, `code`, ~~strike~~, and [links](url) all display properly in prose, lists, "
        "callouts, and table cells (and survive into HTML/PDF exports). Write naturally marked-up "
        "text where it helps readability.\n"
        "STYLING (optional, on ANY block item) — make the page readable at a glance, not just "
        "colourful: `variant` on a callout ∈ note|info|tip|success|warning|danger (each renders a "
        "colour + icon + label — e.g. a warning reads red, a confirmation green); `flag` on a to-do "
        "∈ high|low|blocked (a coloured priority badge); `color` tints any block's background; "
        "`accent` sets a block/card accent bar; `icon` badges a card. Set page-wide `theme` = a "
        "colour (any palette key, e.g. slate|red|orange|green|sky|indigo|pink) to accent headings/links. Prefer "
        "semantic styling (warning/danger/success) where it aids scanning; don't colour everything.\n"
        "Optional `layout` = {mode:'canvas', cards:[{block:<0-based block index>, x, y, w, h, "
        "title?}]} arranges blocks as positioned cards (a dashboard) instead of a linear doc; "
        "or `layout` = {columns: [[<0-based block indices>], ...]} renders each group of blocks "
        "SIDE BY SIDE as document columns (each group needs >=2 blocks; e.g. three metric "
        "blocks in a row). Omit for a normal document. Returns {page_id, title, blocks_written, layout:{mode,cards,styled}}.",
        _page_create,
        _schema({"title": _STR, "blocks": _ARR, "theme": _STR,
                 "layout": {"type": "object",
                            "description": "optional canvas arrangement; see the tool description"}},
                ["title"]),
        writes=True,
    ),
    Tool(
        "june_page_write",
        "REPLACE the entire content of an existing page with `blocks` (same rich block vocabulary "
        "and optional `layout` as june_page_create: text, tables, live views, media, interactive "
        "controls — dropdowns/to-do cells — and canvas "
        "layout). Authoritative — any block not in this call is DELETED. Use it when the user "
        "asked to replace, rewrite or restructure a page; when they say 'update' they usually "
        "mean ADD, which is june_page_append.\n"
        "THE GOOD PATH for a revision: june_page_get, then send its blocks back **carrying their "
        "`id` fields**, changing only what should change. Matched blocks update in place, so "
        "block ids survive — and mentions, per-block styling and canvas layout all key on those "
        "ids, so they survive with them. A payload without ids replaces every block with a new "
        "one.\n"
        "A write that would remove 10 or more blocks is REFUSED and names the first few, so "
        "nothing large disappears without someone meaning it; repeat with `force: true` for a "
        "deliberate wholesale replace. Concurrency is handled for you — no token to pass. "
        "Per-block styling (variant/flag/color/accent/icon) and a page `theme` colour work "
        "exactly as in june_page_create. Returns {page_id, blocks_written, blocks_before, "
        "blocks_removed, layout} — and if anything was removed, how to restore it.",
        _page_write,
        _schema({"page_id": _STR, "blocks": _ARR, "theme": _STR,
                 "force": {"type": "boolean",
                           "description": "confirm a wholesale replace that removes 10+ blocks"},
                 "expected_updated_at": {
                     **_STR,
                     "description": "optional: pin a specific revision (from june_page_get); "
                                    "omitted, the connector uses the page's current one"},
                 "layout": {"type": "object", "description": "optional canvas arrangement"}},
                ["page_id", "blocks"]),
        writes=True,
    ),
    Tool(
        "june_page_append",
        "ADD blocks to the END of an existing page WITHOUT resending its current content — the "
        "existing blocks are preserved (ids and order kept), the new `blocks` are appended after "
        "them. Use for iterative building: dropping in today's notes, adding a section or a card as "
        "you go. Same rich block vocabulary as june_page_create (text, tables, views, media, "
        "dropdowns/to-do cells).\n"
        "**THIS IS THE DEFAULT WAY TO ADD TO A PAGE, INCLUDING WHEN THE USER SAYS 'UPDATE'.** It "
        "cannot delete a block, so it is safe on a page whose current content you have not read — "
        "and safe when another session may have written to that page. Only fall back to "
        "june_page_write when the user explicitly asked to replace/rewrite/restructure the whole "
        "page AND you called june_page_get immediately beforehand. Note appended blocks carry no "
        "per-block styling (variant/flag/colour) — an acceptable trade against overwriting. "
        "Returns {page_id, blocks_appended, blocks_total}.",
        _page_append,
        _schema({"page_id": _STR, "blocks": _ARR}, ["page_id", "blocks"]),
        writes=True,
    ),
    Tool(
        "june_page_update",
        "EDIT SPECIFIC EXISTING BLOCKS of a page in place, by id, WITHOUT resending the rest of "
        "the page. Each item in `blocks` is {id, text, block_type?} — ids come from "
        "june_page_get. The server updates exactly those blocks: every position is preserved, "
        "nothing is created, nothing is deleted, and if any id is not a block of the page the "
        "WHOLE call refuses atomically (nothing changes). Use when the user wants to fix, "
        "reword or correct particular blocks of a page — especially a LARGE page, where "
        "june_page_write would mean transporting the entire document to change a few lines.\n"
        "Choose by intent: ADD new content → june_page_append; edit NAMED existing blocks → "
        "june_page_update (this); replace/restructure the whole page → june_page_write (after "
        "june_page_get).\n"
        "Concurrency: pass `expected_revision` from the june_page_get this edit grew out of — a "
        "stale edit is refused (the page moved) rather than applied; `force: true` is the "
        "deliberate, audited override. Returns {page_id, blocks_updated, blocks_total, "
        "revision}.",
        _page_update,
        _schema({"page_id": _STR, "blocks": _ARR,
                 "expected_revision": {"type": "integer",
                                       "description": "revision from the june_page_get this edit "
                                                      "is based on; stale → refused, not applied"},
                 "force": {"type": "boolean",
                           "description": "deliberate overwrite of the named blocks even if the "
                                          "page moved (leaves an audit trace)"}},
                ["page_id", "blocks"]),
        writes=True,
    ),
    Tool(
        "june_page_delete",
        "DELETE a whole page and all its blocks (reversible — the page is soft-deleted server-side, "
        "not dropped). Use to remove a page the user no longer wants. **READ IT FIRST with "
        "june_page_get** — so you can name what is being deleted, and so you notice if the page "
        "has grown content you and the user were not talking about. To remove only SOME blocks, "
        "use june_page_write with just the blocks to keep — and that path is the destructive one, "
        "so the pre-read is mandatory there, not optional. Requires a page_id from "
        "june_page_list. Returns {ok, page_id, blocks_deleted}.",
        _page_delete,
        _schema({"page_id": _STR}, ["page_id"]),
        writes=True,
    ),
    Tool(
        "june_canvas_list",
        "List every canvas (isolated June workspace) this connection can reach, marking "
        "the immutable DEFAULT — the canvas a call targets when it names none. Use to "
        "orient before addressing a canvas per call, or when the user asks what "
        "workspaces exist. Returns {canvases:[{canvas_id, name, created_at, default}], "
        "default_canvas_id}.",
        _canvas_list,
        _schema({}),
        canvas_scoped=False,
    ),
    Tool(
        "june_canvas_current",
        "The connection's immutable DEFAULT canvas — where a call lands when it names no "
        "canvas. Use to orient in a long conversation. "
        "canvas. It cannot be changed at runtime (CX3): no other conversation can move "
        "it under you, so you do NOT need to re-check it before writes. To act in a "
        "different canvas, pass canvas=<name | id | canvas_handle> on that call. "
        "Returns {default_canvas_id, name, note}.",
        _canvas_current,
        _schema({}),
        canvas_scoped=False,
    ),
    Tool(
        "june_canvas_use",
        "RESOLVE a canvas (name or id) and mint a canvas_handle for addressing it — "
        "**this call switches nothing** (CX3: the connector holds no movable canvas "
        "state, so no conversation can redirect another). Use when the work moves to a "
        "different project/workspace: take the returned canvas_handle (or keep using "
        "the name) and pass it as canvas=... on each call that should act there. The "
        "handle survives nothing it shouldn't: after a connector restart it is refused "
        "loudly instead of silently pointing elsewhere (it is a correctness token, not "
        "a credential). Fail-closed on unknown or ambiguous names. Returns {canvas_id, "
        "name, canvas_handle, default_canvas_id, switched:false, note}.",
        _canvas_use,
        _schema({"canvas": {**_STR, "description": "canvas name, id, or handle"}}, ["canvas"]),
        canvas_scoped=False,
    ),
    Tool(
        "june_canvas_create",
        "CREATE a new, empty canvas (an isolated June workspace). Use when the user "
        "starts a distinct project/topic whose memory should live apart. Duplicate "
        "names are refused (they would make later name lookups ambiguous). Creating "
        "switches nothing (CX3) — pass the returned canvas_handle (or the name) as "
        "canvas=... on calls that should act in it. Returns {canvas_id, name, created, "
        "canvas_handle, switched:false, note}.",
        _canvas_create,
        _schema({"name": _STR}, ["name"]),
        writes=True,
        canvas_scoped=False,
    ),
    Tool(
        "june_canvas_clear",
        "DANGER — IRREVERSIBLY erase every node and edge in a canvas (the canvas itself "
        "remains, empty). TWO-PHASE: the first call executes NOTHING and returns a "
        "confirm_token plus a warning to relay to the user; only a second call with "
        "that confirm_token executes (tokens are single-use and expire in ~2 minutes). "
        "Use ONLY on an explicit, unambiguous user request to wipe a canvas — never to "
        "tidy up on your own initiative. Call june_page_list first and relay what will be lost BY "
        "NAME, so the user confirms a specific canvas rather than a word; and note a confirm "
        "token is not bound to a conversation, so never use one you did not just mint. Returns "
        "the pending warning first, then {canvas_id, nodes_deleted, edges_deleted, op, name}.",
        _canvas_clear,
        _schema({"canvas": {**_STR, "description": "canvas name or id"},
                 "confirm": {**_STR, "description": "confirm_token from the pending call"}},
                ["canvas"]),
        writes=True,
        canvas_scoped=False,
    ),
    Tool(
        "june_canvas_delete",
        "DANGER — IRREVERSIBLY erase a canvas's entire graph AND remove the canvas "
        "itself. TWO-PHASE like june_canvas_clear: first call returns a confirm_token + "
        "warning, nothing executes; second call with the token executes. Deleting the "
        "connection's immutable DEFAULT canvas is refused. Use ONLY on "
        "an explicit, unambiguous user request. As with june_canvas_clear: list the pages first and "
        "relay what will be lost by name, and never use a confirm token you did not just mint. "
        "Returns the pending warning first, then {canvas_id, nodes_deleted, edges_deleted, "
        "deleted, op, name}.",
        _canvas_delete,
        _schema({"canvas": {**_STR, "description": "canvas name or id"},
                 "confirm": {**_STR, "description": "confirm_token from the pending call"}},
                ["canvas"]),
        writes=True,
        canvas_scoped=False,
    ),
    # ── Phase AM: agent docs / skills / learnings (June as instruction memory) ──
    Tool(
        "june_docs_refresh",
        "Re-read the agent's STANDING DOCS from June and return the full digest NOW: "
        "pinned doc bodies (always-in-effect instructions), skill trigger lines, and "
        "doc one-liners. Use ALWAYS at the START of every session, before other work, "
        "to load your standing instructions — unprompted; the user should not have to "
        "ask. Also use "
        "whenever you suspect drift in a long conversation; between calls a compact "
        "`standing_docs` digest also arrives periodically on ordinary June results — "
        "treat both as current instructions. Returns {note, pinned[], skills[], "
        "docs[], as_of}; fetch any full body with june_doc_get(name).",
        _docs_refresh_tool,
        _schema({}),
        canvas_scoped=False,
    ),
    Tool(
        "june_doc_list",
        "List the agent docs saved in June — the registry of standing instructions "
        "(kind=doc), skills (kind=skill, with when_to_use triggers), and learnings "
        "logs. Use to see what standing knowledge exists before saving a new doc or "
        "when choosing which skill to load; use june_docs_refresh for the digest "
        "form, june_doc_get for one full body. Returns {docs:[{name, kind, title, "
        "when_to_use, pinned, page_id, updated_at, v}], count}.",
        _doc_list,
        _schema({}),
        canvas_scoped=False,
    ),
    Tool(
        "june_doc_get",
        "Read ONE agent doc's full body (markdown) by name. Use when a skill's "
        "when_to_use matches the task at hand, when a standing_docs digest names a "
        "doc you need in full, or before revising a doc with june_doc_save (carry "
        "its updated_at forward as expected_updated_at). Returns {name, kind, body, "
        "when_to_use, pinned, page_id, updated_at, v}.",
        _doc_get,
        _schema({"name": {**_STR, "description": "the doc's slug name"}}, ["name"]),
        canvas_scoped=False,
    ),
    Tool(
        "june_doc_save",
        "Create or WHOLE-REPLACE an agent doc — durable instructions the agent (any "
        "session, any host) should keep following: kind='doc' for standing "
        "instructions (pinned=true rides every digest, the CLAUDE.md role), "
        "kind='skill' for a named procedure (when_to_use required — its trigger "
        "line), kind='learnings' for an append-only log (prefer june_learn to add "
        "entries). Use when the user states a lasting convention, or a procedure "
        "worth reusing emerges. Body is markdown; saving replaces the whole body, so "
        "when revising call june_doc_get first and pass its updated_at as "
        "expected_updated_at — a stale save is refused, not applied. The doc becomes "
        "a June page the user can read and edit. Returns {name, kind, page_id, "
        "created, pinned, v, blocks_written}.",
        _doc_save,
        _schema({"name": {**_STR, "description": "slug name (a-z 0-9 . _ -)"},
                 "text": {**_STR, "description": "the doc body, markdown"},
                 "kind": {**_STR, "description": "doc | skill | learnings (default doc)"},
                 "when_to_use": {**_STR, "description": "one-line trigger (required for skills)"},
                 "pinned": {**_BOOL, "description": "include full body in every digest"},
                 "title": {**_STR, "description": "page title (default: the name)"},
                 "expected_updated_at": {**_STR, "description":
                     "updated_at from june_doc_get when revising an existing doc"}},
                ["name", "text"]),
        writes=True,
        canvas_scoped=False,
    ),
    Tool(
        "june_doc_delete",
        "Remove an agent doc from the standing instructions. TWO-PHASE like the "
        "canvas deletes: the first call returns a confirm_token + warning and "
        "deletes nothing; only a second call carrying that token executes. Use ONLY "
        "on an explicit user request to forget/remove a doc — never to tidy. "
        "Returns the pending warning first, then {deleted, name, page_id}.",
        _doc_delete,
        _schema({"name": {**_STR, "description": "the doc's slug name"},
                 "confirm": {**_STR, "description": "confirm_token from the pending call"}},
                ["name"]),
        writes=True,
        canvas_scoped=False,
    ),
    # ── Phase AM2: repo sync (absent unless the operator sets JUNE_EXPORT_ROOT) ──
    Tool(
        "june_docs_export",
        "Mirror EVERY agent doc (docs/skills/learnings) to markdown files under "
        "the repo's docs tree (JUNE_EXPORT_ROOT + JUNE_EXPORT_DIR, default "
        "docs/agent/) so the repository always holds the current standing "
        "instructions. Use after saving or revising docs, or when the user asks "
        "to sync/update the repo. Files carry frontmatter (page_id, kind, v, "
        "updated_at) and round-trip via june_page_import; unmanaged files are "
        "never overwritten; with JUNE_EXPORT_GIT=1 exactly the written files are "
        "committed (never pushed). check=true writes nothing and reports drift. "
        "Returns {written[], unchanged, refused[], git…}.",
        _docs_export,
        _schema({"check": {**_BOOL, "description": "report drift only, write nothing"}}),
        canvas_scoped=False,
        available=bool(os.environ.get("JUNE_EXPORT_ROOT", "").strip()),
    ),
    Tool(
        "june_page_export",
        "Export ONE June page to the repo — as a whole managed markdown file "
        "(default docs/agent/pages/<title>.md, or an explicit 'path'), or with "
        "'section' as a MANAGED SECTION spliced between markers inside an "
        "existing human file (e.g. path=KNOWHOW.md section=june-learnings — only "
        "the marked region is ever touched). Use to keep KNOWHOW/CHANGELOG/"
        "runbook pages current in git. Unmanaged whole files are never "
        "overwritten; with JUNE_EXPORT_GIT=1 the written file is committed "
        "(never pushed). check=true reports without writing. Returns {path, "
        "mode, status, git…}.",
        _page_export,
        _schema({"page_id": _STR,
                 "path": {**_STR, "description": "target file, relative to JUNE_EXPORT_ROOT"},
                 "section": {**_STR, "description": "managed-section name inside 'path'"},
                 "check": {**_BOOL, "description": "report drift only, write nothing"}},
                ["page_id"]),
        available=bool(os.environ.get("JUNE_EXPORT_ROOT", "").strip()),
    ),
    Tool(
        "june_page_import",
        "Import a june-managed repo file BACK into its June page — the repo "
        "becomes a real editing surface: edit docs/agent/<name>.md in your "
        "editor, then this call updates the page (agent docs keep their "
        "identity; registry metadata is rebuilt from the frontmatter). Use when "
        "the user edited an exported file and wants June to match. Guarded like "
        "every page write: if the June page changed since the export, the "
        "import is REFUSED with re-export-and-merge instructions — a stale file "
        "can never clobber newer knowledge. Returns {page_id, path, imported, "
        "blocks_written, git…}.",
        _page_import,
        _schema({"path": {**_STR, "description":
                 "a june-managed file under JUNE_EXPORT_ROOT"}}, ["path"]),
        writes=True,
        canvas_scoped=False,
        available=bool(os.environ.get("JUNE_EXPORT_ROOT", "").strip()),
    ),
    Tool(
        "june_learn",
        "Append ONE dated lesson to a learnings doc. Use the moment something "
        "worth keeping emerges mid-session: a fix that worked, a gotcha, a decision, "
        "an approach that failed. Append-only by construction (it can never rewrite "
        "or remove existing entries), so save as you go, not when asked. Default doc "
        "is 'learnings' (created on first use); it refuses to append to docs/skills — "
        "promoting a lesson into those is a deliberate june_doc_save. Returns {doc, "
        "page_id, appended, blocks_total}.",
        _learn,
        _schema({"text": {**_STR, "description": "the lesson, one entry"},
                 "doc": {**_STR, "description": "target learnings doc (default 'learnings')"}},
                ["text"]),
        writes=True,
        canvas_scoped=False,
    ),
]

_BY_NAME = {t.name: t for t in TOOLS}

# CX5: every canvas-scoped tool takes an optional per-call ``canvas``. Injected in
# ONE place with ONE shared description (A6: the field must not cost a paragraph
# per tool), so the surface cannot drift tool-by-tool.
_CANVAS_ARG_DOC = ("Canvas to act in for THIS call only — a name, id, or the "
                   "canvas_handle from june_canvas_use. Omitted = the connection's "
                   "immutable default canvas. Nothing is remembered between calls, "
                   "which is why this is safe: the default cannot move.")
for _t in TOOLS:
    if _t.canvas_scoped:
        _t.input_schema.setdefault("properties", {})["canvas"] = {
            "type": "string", "description": _CANVAS_ARG_DOC}

# Doc tools are canvas_scoped=False (their default is the DOCS canvas, not the
# connection default) but still take a per-call ``canvas`` override — injected
# here with its own shared description, same one-place rule as above.
_DOCS_CANVAS_ARG_DOC = ("Canvas holding the agent docs, for THIS call only. Omitted = the "
                        "configured docs canvas (JUNE_DOCS_CANVAS, default 'agent_docs') — "
                        "NOT the connection's default canvas.")
for _t in TOOLS:
    if _t.name in _DOCS_TOOL_NAMES:
        _t.input_schema.setdefault("properties", {})["canvas"] = {
            "type": "string", "description": _DOCS_CANVAS_ARG_DOC}


# Pro-gated verbs: an AGENT building/editing pages is a paid capability (users edit their own
# pages free in the app — that path is the engine's, not this connector's). Reads (page_list /
# page_get) stay free. The tier comes from the service's /v1/whoami (see __main__/server); on a
# non-Pro connection these are hidden AND refused, the same two-fence shape as the read-only
# posture. `pro` defaults True so tests and Pro connections behave unchanged.
_PRO_ONLY = {"june_page_create", "june_page_write", "june_page_append",
             "june_page_update", "june_page_delete"}


def visible_tools(*, readonly: bool = False, pro: bool = True) -> list[Tool]:
    """The tool surface for a server posture: read-only hides every write verb, capability-absent
    tools (see ``Tool.available``) are never shown, and a non-Pro connection hides the agent
    page-authoring verbs (``_PRO_ONLY``)."""
    return [t for t in TOOLS
            if t.available
            and not (readonly and t.writes)
            and not (not pro and t.name in _PRO_ONLY)]


def run_tool(name: str, client: JuneClient, args: dict | None = None, *,
             readonly: bool = False, pro: bool = True, strict: bool = False) -> Any:
    """Invoke a tool by name (the path both the MCP server and tests use).

    ``readonly=True`` refuses write verbs even if a caller addresses them directly — the same fence
    as the visible list, enforced at execution (defense in depth). ``pro=False`` refuses the agent
    page-authoring verbs (``_PRO_ONLY``) with a clear upgrade message.

    CX3/CX5: a canvas-scoped call may carry ``canvas`` (name | id | canvas_handle) — resolved
    fail-closed HERE, applied to THIS call only via an immutable client view; nothing is
    remembered. ``strict=True`` (JUNE_CANVAS_STRICT=1) refuses canvas-scoped calls that name no
    canvas — a deployment posture for multi-canvas operators, not a safety crutch: safety comes
    from the default being immutable."""
    tool = _BY_NAME.get(name)
    if tool is None:
        raise KeyError(f"unknown tool {name!r}; known: {sorted(_BY_NAME)}")
    if not tool.available:
        raise KeyError(f"tool {name!r} is unavailable in this install: it needs the "
                       "June engine packages (the june-local distribution); the thin "
                       "connector exposes the remaining tools")
    if readonly and tool.writes:
        raise KeyError(f"tool {name!r} is disabled: this June connection is read-only "
                       "(JUNE_READONLY=1)")
    if not pro and name in _PRO_ONLY:
        raise KeyError(f"tool {name!r} requires June Pro: letting an agent build or edit pages is "
                       "a Pro capability. You can still read pages (june_page_list / june_page_get) "
                       "and edit pages yourself in the June app.")

    a = dict(args or {})
    eff_client = client
    eff_id = client.canvas or ""
    if tool.canvas_scoped:
        wanted = str(a.pop("canvas", "") or "").strip()
        if wanted:
            cid, cname = _canvas_target(client, wanted)   # fail-closed BEFORE any request
            eff_client = client.for_canvas(cid)           # this call only; nothing remembered
            eff_id = cid
            if cname:
                note_canvas_name(cid, cname)
        elif strict:
            raise KeyError(f"tool {name!r} refused: this connection runs "
                           "JUNE_CANVAS_STRICT=1, so every canvas-scoped call must name "
                           "its canvas explicitly (canvas=<name | id | canvas_handle>). "
                           "Nothing was executed.")

    try:
        result = tool.handler(eff_client, a)
    except Exception as exc:
        # CX9: a canvas-scoped 404 means the canvas this call addressed may no
        # longer exist (deleted in the app / another session) — evict its cache
        # entries BEFORE propagating, so the next resolution is live and honest.
        import httpx as _httpx
        if (tool.canvas_scoped and eff_id
                and isinstance(exc, _httpx.HTTPStatusError)
                and exc.response.status_code == 404):
            _cache_drop_canvas(eff_id)
        raise

    # CX6 — results tell the truth about where the call landed: every canvas-scoped
    # result names the EFFECTIVE canvas (id, plus display name when known without
    # extra traffic). This replaces the 2026-08-14 write-only provenance echo — with
    # per-call addressing, reads need the receipt as much as writes do. Canvas
    # management verbs name their target explicitly already and are left as-is.
    if tool.canvas_scoped and isinstance(result, dict):
        if "canvas" not in result:
            result = {**result, "canvas": eff_id or "home"}
        cname = _NAMES.get(eff_id)
        if cname and "canvas_name" not in result:
            result = {**result, "canvas_name": cname}

    # Phase AM — the anti-forgetting layer. Tool results re-enter the model's
    # FRESH context on every call (instructions read at session start do not),
    # so this is the one channel that reaches every MCP host: when the cadence
    # says a digest is due, the standing docs ride along on this result. Doc
    # tools are exempt (their results ARE doc content), and a failed build never
    # costs the carrying call anything (_standing_docs never raises).
    if (_DOCS_CFG.enabled and isinstance(result, dict)
            and tool.name not in _DOCS_TOOL_NAMES and _DOCS_STATE.tick()):
        digest = _standing_docs(client)
        if digest is not None:
            result = {**result, "standing_docs": digest}
    return result

__all__ = ["Tool", "TOOLS", "configure_docs", "run_tool", "visible_tools"]
