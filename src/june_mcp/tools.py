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
]

_BY_NAME = {t.name: t for t in TOOLS}


def visible_tools(*, readonly: bool = False) -> list[Tool]:
    """The tool surface for a server posture: read-only hides every write verb,
    and capability-absent tools (see ``Tool.available``) are never shown."""
    return [t for t in TOOLS if t.available and not (readonly and t.writes)]


def run_tool(name: str, client: JuneClient, args: dict | None = None, *,
             readonly: bool = False) -> Any:
    """Invoke a tool by name (the path both the MCP server and tests use).

    ``readonly=True`` refuses write verbs even if a caller addresses them directly —
    the same fence as the visible list, enforced at execution (defense in depth)."""
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
    return tool.handler(client, args or {})


__all__ = ["Tool", "TOOLS", "run_tool", "visible_tools"]
