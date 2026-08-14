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
* ``june_page_create`` / ``june_page_write`` / ``june_page_append`` — compose, replace, or extend a
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

from june_client import JuneClient

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
        raise ValueError("june_remember needs non-empty 'text'")
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
        raise ValueError("june_ingest_file is disabled: the operator must set "
                         "JUNE_FILES_ROOT to the directory agents may upload from")
    root = Path(root_s).expanduser().resolve()
    raw = str(a.get("path") or "").strip()
    if not raw:
        raise ValueError("june_ingest_file needs 'path' (relative to JUNE_FILES_ROOT, "
                         "or absolute inside it)")
    p = Path(raw).expanduser()
    p = (p if p.is_absolute() else root / p).resolve()
    if p != root and root not in p.parents:
        raise ValueError("path escapes JUNE_FILES_ROOT — refused")
    if not p.is_file():
        raise ValueError(f"not a file under JUNE_FILES_ROOT: {p.name!r}")
    data = p.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"file too large ({len(data)} bytes > {MAX_FILE_BYTES})")
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
_STYLE_COLORS = {"slate", "red", "amber", "green", "teal", "blue", "purple", "pink"}
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
    return {"block_type": t, "text": str(b.get("text", "")), "order": order}


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


def _layout_text(cards: Any, ids_by_index: dict[int, str]) -> str | None:
    """Canvas cards ``[{block:<index>, x, y, w, h, title?}]`` → the __june_layout__ sentinel JSON,
    keyed by the REAL block ids resolved after the first save. Returns None if no card resolves (so
    the caller skips the layout block entirely rather than writing an empty canvas)."""
    if not isinstance(cards, list):
        return None
    pos: dict[str, dict] = {}
    titles: dict[str, str] = {}
    for c in cards:
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
    if not pos:
        return None
    body: dict[str, Any] = {_LAYOUT_SENTINEL: 1, "mode": "canvas", "pos": pos}
    if titles:
        body["titles"] = titles
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
        raise ValueError("june_page_get needs 'page_id'")
    return client.get_page(pid)


def _wants_canvas(layout: Any) -> bool:
    return isinstance(layout, dict) and str(layout.get("mode", "")).lower() == "canvas"


def _save_with_layout(client: JuneClient, pid: str, blocks: list[dict], layout: Any,
                      styles: dict[int, dict] | None = None, page_accent: Any = None) -> dict:
    """Save content blocks, then — if a canvas layout OR any per-block/page styling was requested —
    resolve the server's real block ids and re-save the SAME content (carrying those ids so they
    update in place) plus a __june_layout__ and/or __june_style__ sentinel block. The second save is
    irreducible: a card's position and a block's style both key on a block id, which exists only after
    the first save. Returns {mode, cards, styled} describing what landed."""
    detail = client.save_blocks(pid, blocks)
    styles = styles or {}
    ids = _ids_by_order(detail)
    lt = _layout_text(layout.get("cards"), ids) if _wants_canvas(layout) else None
    stext = _style_text(styles, ids, page_accent)
    if lt is None and stext is None:
        return {"mode": "doc", "cards": 0, "styled": 0}    # nothing to key on ids → stay a linear doc
    content = _carry_ids(detail)
    for extra in (lt, stext):
        if extra is not None:
            content.append({"block_type": "paragraph", "text": extra, "order": float(len(content) + 1)})
    client.save_blocks(pid, content)
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
        layout = _save_with_layout(client, pid, blocks, a.get("layout"), styles, page_accent)
    return {"page_id": pid, "title": created.get("title", title),
            "blocks_written": len(blocks), "layout": layout}


def _page_write(client: JuneClient, a: dict) -> dict:
    pid = str(a.get("page_id", "")).strip()
    if not pid:
        raise ValueError("june_page_write needs 'page_id'")
    blocks = _to_blocks(a.get("blocks"))
    styles = _styles_by_index(a.get("blocks"))
    page_accent = a.get("theme") or a.get("accent")
    layout = _save_with_layout(client, pid, blocks, a.get("layout"), styles, page_accent)
    return {"page_id": pid, "blocks_written": len(blocks), "layout": layout}


def _page_append(client: JuneClient, a: dict) -> dict:
    pid = str(a.get("page_id", "")).strip()
    if not pid:
        raise ValueError("june_page_append needs 'page_id'")
    blocks = _to_blocks(a.get("blocks"))
    if not blocks:
        raise ValueError("june_page_append needs a non-empty 'blocks'")
    detail = client.append_blocks(pid, blocks)
    return {"page_id": pid, "blocks_appended": len(blocks),
            "blocks_total": len(detail.get("blocks") or [])}


def _page_delete(client: JuneClient, a: dict) -> dict:
    pid = str(a.get("page_id", "")).strip()
    if not pid:
        raise ValueError("june_page_delete needs 'page_id'")
    return client.delete_page(pid)


# ── canvases (switch / create / manage the ACTIVE workspace at runtime) ────────
# The active canvas is the client's ``canvas`` attribute — the same value every
# request already sends as ``X-Canvas``. Switching it here changes where ALL
# subsequent tool calls read and write, with NO connector or agent restart.
# Deliberate invariants:
#   * On connector restart the selection RESETS to the configured default
#     (JUNE_CANVAS) — deterministic, never sticky-surprising. Results say so.
#   * Destructive ops (clear/delete) are TWO-PHASE: the first call executes
#     nothing and returns a single-use, short-lived confirm token bound to
#     (op, canvas); only a second call carrying that token executes. An agent
#     cannot erase a canvas in one tool call, and the human sees the warning
#     turn in the transcript between the two.
#   * These tools address NAMED canvases only — the home workspace is not
#     reachable through them (the service's canvas routes are canvas-scoped).
#   * Deleting the ACTIVE canvas is refused outright: switch away first, so a
#     successful delete can never leave the connector pointed at a 404.
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


_CONFIRMS = PendingConfirms()


def _canvas_ref(client: JuneClient, wanted: str) -> tuple[str, str]:
    """Resolve an agent-supplied canvas name-or-id to ``(canvas_id, name)`` against
    the live canvas list — fail-closed: ids must exist, names must match exactly
    one canvas (exact match first, then unique case-insensitive). Raises KeyError
    with a message built only from canvas names/ids (never transport internals)."""
    rows = client.list_canvases()
    try:
        _uuid.UUID(wanted)
        for r in rows:
            if str(r.get("canvas_id")) == wanted:
                return wanted, str(r.get("name", ""))
        raise KeyError(f"no canvas with id {wanted} on this endpoint")
    except ValueError:
        pass
    matches = [r for r in rows if str(r.get("name", "")) == wanted]
    if not matches:
        matches = [r for r in rows
                   if str(r.get("name", "")).strip().lower() == wanted.strip().lower()]
    if len(matches) == 1:
        return str(matches[0]["canvas_id"]), str(matches[0].get("name", ""))
    if len(matches) > 1:
        ids = ", ".join(sorted(str(r["canvas_id"]) for r in matches))
        raise KeyError(f"canvas name {wanted!r} is ambiguous ({ids}) — use an id")
    existing = ", ".join(sorted({str(r.get("name", "")) for r in rows if r.get("name")}))
    raise KeyError(f"no canvas named {wanted!r}"
                   + (f" (existing: {existing})" if existing else " (no canvases exist yet)")
                   + " — june_canvas_create can make it")


_RESET_NOTE = ("the selection lasts for this connector session and resets to the "
               "configured default (JUNE_CANVAS) when the connector restarts")


def _canvas_list(client: JuneClient, a: dict) -> dict:
    rows = client.list_canvases()
    active = client.canvas or ""
    return {"canvases": [{**r, "active": str(r.get("canvas_id")) == active} for r in rows],
            "active_canvas_id": active or None}


def _canvas_current(client: JuneClient, a: dict) -> dict:
    active = client.canvas or ""
    name = None
    if active:
        name = next((str(r.get("name", "")) for r in client.list_canvases()
                     if str(r.get("canvas_id")) == active), None)
    return {"active_canvas_id": active or None, "name": name, "note": _RESET_NOTE}


def _canvas_use(client: JuneClient, a: dict) -> dict:
    wanted = str(a.get("canvas") or "").strip()
    if not wanted:
        raise ValueError("june_canvas_use needs 'canvas' (a canvas name or id)")
    cid, name = _canvas_ref(client, wanted)
    previous = client.canvas or None
    client.canvas = cid
    return {"active_canvas_id": cid, "name": name, "previous": previous,
            "note": f"all subsequent June calls now read/write this canvas; {_RESET_NOTE}"}


def _canvas_create(client: JuneClient, a: dict) -> dict:
    name = str(a.get("name") or "").strip()
    if not name:
        raise ValueError("june_canvas_create needs 'name'")
    clash = [r for r in client.list_canvases() if str(r.get("name", "")) == name]
    if clash:
        raise KeyError(f"a canvas named {name!r} already exists "
                       f"({clash[0]['canvas_id']}) — june_canvas_use it, or pick "
                       "another name (duplicate names make every later name lookup ambiguous)")
    made = client.create_canvas(name)
    cid = str(made["canvas_id"])
    out = {"canvas_id": cid, "name": str(made.get("name", name)), "created": True}
    if bool(a.get("use", True)):
        out["previous"] = client.canvas or None
        client.canvas = cid
        out["active"] = True
        out["note"] = f"now the active canvas; {_RESET_NOTE}"
    return out


def _canvas_destructive(client: JuneClient, a: dict, *, op: str) -> dict:
    wanted = str(a.get("canvas") or "").strip()
    if not wanted:
        raise ValueError(f"june_canvas_{op} needs 'canvas' (a canvas name or id)")
    cid, name = _canvas_ref(client, wanted)
    if op == "delete" and cid == (client.canvas or ""):
        raise KeyError("refusing to delete the ACTIVE canvas — june_canvas_use another "
                       "canvas first, so a successful delete can never leave this "
                       "connection pointed at a canvas that no longer exists")
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
    return {**res, "op": op, "name": name}


def _canvas_clear(client: JuneClient, a: dict) -> dict:
    return _canvas_destructive(client, a, op="clear")


def _canvas_delete(client: JuneClient, a: dict) -> dict:
    return _canvas_destructive(client, a, op="delete")


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    handler: Callable[[JuneClient, dict], Any]
    input_schema: dict
    writes: bool = field(default=False)   # True ⇒ hidden when the server is read-only
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
        "doesn't know. Use when you want a finished answer to a factual question about "
        "remembered knowledge (people, projects, documents, decisions); use june_context "
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
        "result reports which engine ran). Use when the user states a fact, decision, "
        "update or note worth persisting for later ('remember that…', meeting notes, "
        "a status change). Plain text or markdown, up to ~64k chars. Returns write "
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
        "updated_at}], has_more, next_offset}. To read a page's content use june_page_get.",
        _page_list,
        _schema({"limit": _INT, "offset": _INT}),
    ),
    Tool(
        "june_page_get",
        "Read one page and its ordered blocks (its full content). Use before june_page_write "
        "when you need a page's current content to revise it, or to show a page back to the "
        "user. Requires a page_id from june_page_list or june_page_create. Returns {page_id, "
        "title, blocks:[{block_id, block_type, text, order}]}.",
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
        "and per-row task tracking instead of static text.\n"
        "STYLING (optional, on ANY block item) — make the page readable at a glance, not just "
        "colourful: `variant` on a callout ∈ note|info|tip|success|warning|danger (each renders a "
        "colour + icon + label — e.g. a warning reads red, a confirmation green); `flag` on a to-do "
        "∈ high|low|blocked (a coloured priority badge); `color` tints any block's background; "
        "`accent` sets a block/card accent bar; `icon` badges a card. Set page-wide `theme` = a "
        "colour (slate|red|amber|green|teal|blue|purple|pink) to accent headings/links. Prefer "
        "semantic styling (warning/danger/success) where it aids scanning; don't colour everything.\n"
        "Optional `layout` = {mode:'canvas', cards:[{block:<0-based block index>, x, y, w, h, "
        "title?}]} arranges blocks as positioned cards (a dashboard) instead of a linear doc; omit "
        "for a normal document. Returns {page_id, title, blocks_written, layout:{mode,cards,styled}}.",
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
        "layout). Authoritative — any block not in this call is removed. Use to rewrite or "
        "restructure a page; call june_page_get first if you need its current content to build on, "
        "or use june_page_append to ADD without resending the whole page. Per-block styling "
        "(variant/flag/color/accent/icon) and a page `theme` colour work exactly as in "
        "june_page_create. Returns {page_id, blocks_written, layout:{mode,cards,styled}}.",
        _page_write,
        _schema({"page_id": _STR, "blocks": _ARR, "theme": _STR,
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
        "dropdowns/to-do cells). To "
        "rewrite/restructure the whole page instead, use june_page_write. Returns {page_id, "
        "blocks_appended, blocks_total}.",
        _page_append,
        _schema({"page_id": _STR, "blocks": _ARR}, ["page_id", "blocks"]),
        writes=True,
    ),
    Tool(
        "june_page_delete",
        "DELETE a whole page and all its blocks (reversible — the page is soft-deleted server-side, "
        "not dropped). Use to remove a page the user no longer wants. To remove only SOME blocks, "
        "use june_page_write with just the blocks to keep instead. Requires a page_id from "
        "june_page_list. Returns {ok, page_id, blocks_deleted}.",
        _page_delete,
        _schema({"page_id": _STR}, ["page_id"]),
        writes=True,
    ),
    Tool(
        "june_canvas_list",
        "List every canvas (isolated June workspace) this connection can reach, marking "
        "the ACTIVE one — the canvas all other June tools currently read and write. Use "
        "to orient before switching (june_canvas_use) or creating (june_canvas_create), "
        "or when the user asks what workspaces exist. Returns {canvases:[{canvas_id, "
        "name, created_at, active}], active_canvas_id}.",
        _canvas_list,
        _schema({}),
    ),
    Tool(
        "june_canvas_current",
        "Which canvas is ACTIVE right now — the workspace every other June tool is "
        "reading and writing. Use to re-orient in a long conversation, or after the "
        "connector may have restarted (the selection resets to the configured default "
        "on restart). Returns {active_canvas_id, name, note}.",
        _canvas_current,
        _schema({}),
    ),
    Tool(
        "june_canvas_use",
        "SWITCH the active canvas for this session — all subsequent June reads AND "
        "writes target the chosen canvas, no restart needed. Use when the work moves to "
        "a different project/workspace ('switch to my work canvas', organising memory "
        "per-project). Accepts a canvas name or id; fail-closed on unknown or ambiguous "
        "names. The selection resets to the configured default when the connector "
        "restarts — re-check with june_canvas_current after long gaps. Returns "
        "{active_canvas_id, name, previous, note}.",
        _canvas_use,
        _schema({"canvas": {**_STR, "description": "canvas name or id"}}, ["canvas"]),
    ),
    Tool(
        "june_canvas_create",
        "CREATE a new, empty canvas (an isolated June workspace) and — by default — "
        "switch to it. Use when the user starts a distinct project/topic whose memory "
        "should live apart from the current canvas. Duplicate names are refused (they "
        "would make later name lookups ambiguous). Pass use:false to create without "
        "switching. Returns {canvas_id, name, created, active?, previous?}.",
        _canvas_create,
        _schema({"name": _STR,
                 "use": {**_BOOL, "description": "switch to it (default true)"}},
                ["name"]),
        writes=True,
    ),
    Tool(
        "june_canvas_clear",
        "DANGER — IRREVERSIBLY erase every node and edge in a canvas (the canvas itself "
        "remains, empty). TWO-PHASE: the first call executes NOTHING and returns a "
        "confirm_token plus a warning to relay to the user; only a second call with "
        "that confirm_token executes (tokens are single-use and expire in ~2 minutes). "
        "Use ONLY on an explicit, unambiguous user request to wipe a canvas — never to "
        "tidy up on your own initiative. Returns the pending warning first, then "
        "{canvas_id, nodes_deleted, edges_deleted, op, name}.",
        _canvas_clear,
        _schema({"canvas": {**_STR, "description": "canvas name or id"},
                 "confirm": {**_STR, "description": "confirm_token from the pending call"}},
                ["canvas"]),
        writes=True,
    ),
    Tool(
        "june_canvas_delete",
        "DANGER — IRREVERSIBLY erase a canvas's entire graph AND remove the canvas "
        "itself. TWO-PHASE like june_canvas_clear: first call returns a confirm_token + "
        "warning, nothing executes; second call with the token executes. Deleting the "
        "ACTIVE canvas is refused — june_canvas_use another canvas first. Use ONLY on "
        "an explicit, unambiguous user request. Returns the pending warning first, then "
        "{canvas_id, nodes_deleted, edges_deleted, deleted, op, name}.",
        _canvas_delete,
        _schema({"canvas": {**_STR, "description": "canvas name or id"},
                 "confirm": {**_STR, "description": "confirm_token from the pending call"}},
                ["canvas"]),
        writes=True,
    ),
]

_BY_NAME = {t.name: t for t in TOOLS}


# Pro-gated verbs: an AGENT building/editing pages is a paid capability (users edit their own
# pages free in the app — that path is the engine's, not this connector's). Reads (page_list /
# page_get) stay free. The tier comes from the service's /v1/whoami (see __main__/server); on a
# non-Pro connection these are hidden AND refused, the same two-fence shape as the read-only
# posture. `pro` defaults True so tests and Pro connections behave unchanged.
_PRO_ONLY = {"june_page_create", "june_page_write", "june_page_append", "june_page_delete"}


def visible_tools(*, readonly: bool = False, pro: bool = True) -> list[Tool]:
    """The tool surface for a server posture: read-only hides every write verb, capability-absent
    tools (see ``Tool.available``) are never shown, and a non-Pro connection hides the agent
    page-authoring verbs (``_PRO_ONLY``)."""
    return [t for t in TOOLS
            if t.available
            and not (readonly and t.writes)
            and not (not pro and t.name in _PRO_ONLY)]


def run_tool(name: str, client: JuneClient, args: dict | None = None, *,
             readonly: bool = False, pro: bool = True) -> Any:
    """Invoke a tool by name (the path both the MCP server and tests use).

    ``readonly=True`` refuses write verbs even if a caller addresses them directly — the same fence
    as the visible list, enforced at execution (defense in depth). ``pro=False`` refuses the agent
    page-authoring verbs (``_PRO_ONLY``) with a clear upgrade message."""
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
    result = tool.handler(client, args or {})
    # Write provenance (2026-08-14): every write result names the canvas it acted on,
    # so a write that landed somewhere unexpected is visible in the transcript, not
    # silent — the runtime-switchable active canvas makes this non-negotiable. Canvas
    # management verbs name their target explicitly already and are left as-is.
    if (tool.writes and isinstance(result, dict)
            and not name.startswith("june_canvas_") and "canvas" not in result):
        result = {**result, "canvas": client.canvas or "home"}
    return result


__all__ = ["Tool", "TOOLS", "run_tool", "visible_tools"]
