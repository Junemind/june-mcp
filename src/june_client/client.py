"""The June AI connector client (sync, httpx-based).

Thin and dependency-light on purpose. The client is constructed with a base URL
+ API key; for testing/embedding you may inject a pre-built ``httpx.Client``
(e.g. a FastAPI ``TestClient``) so calls route in-process with no network.
"""
from __future__ import annotations

import uuid
from typing import Any, Sequence

import httpx


class PageRevisionConflict(RuntimeError):
    """A page save was refused because the page moved since the caller read it (HTTP 409).

    Raised only when the caller supplied ``expected_updated_at`` — i.e. only when it
    ASKED to be protected. The correct response is always the same: re-read the page and
    decide again against its real current content. Never retry the same payload blindly;
    that is the overwrite this exception exists to prevent.
    """


def _detail(r: httpx.Response) -> str:
    """The server's ``detail`` string, if the body is the usual JSON error shape."""
    try:
        body = r.json()
    except Exception:  # noqa: BLE001 — a non-JSON error body is not worth a failure
        return ""
    return str(body.get("detail", "")) if isinstance(body, dict) else ""


def node(
    node_id: str | uuid.UUID, node_type: str, label: str,
    *, source_app: str = "unknown", extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a node display row for ``ingest(nodes=...)``."""
    return {
        "node_id": str(node_id), "node_type": node_type, "label": label,
        "source_app": source_app, "extra": dict(extra or {}),
    }


def edge(
    source_node_id: str | uuid.UUID, source_node_type: str,
    target_node_id: str | uuid.UUID, target_node_type: str,
    edge_kind: str, *, confidence: float = 1.0,
    source_tag: str = "explicit", rule_name: str | None = None,
    evidence_node_ids: Sequence[str | uuid.UUID] = (),
) -> dict[str, Any]:
    """Build an edge proposal for ``ingest(proposals=...)``."""
    return {
        "source_node_id": str(source_node_id), "source_node_type": source_node_type,
        "target_node_id": str(target_node_id), "target_node_type": target_node_type,
        "edge_kind": edge_kind, "confidence": confidence,
        "source_tag": source_tag, "rule_name": rule_name or edge_kind,
        "evidence_node_ids": [str(e) for e in evidence_node_ids],
    }


class JuneClient:
    """Sync client for a June AI service.

    :param base_url: the service base URL (used only when building an own client).
    :param api_key:  the connector's API key (sent as ``X-API-Key``).
    :param client:   optional pre-built ``httpx.Client`` (e.g. a TestClient) for
                     in-process use; when given, this client is NOT closed by us.
    """

    def __init__(
        self, base_url: str = "http://localhost:8000", api_key: str = "",
        *, client: httpx.Client | None = None, timeout: float = 10.0,
        canvas: str = "",
        answer_timeout: float | None = None, llm_key: str = "", llm_model: str = "",
    ) -> None:
        self.api_key = api_key
        self._default_canvas = canvas   # immutable per-client (CX3); see the property below
        # Per-verb budget: /v1/answer carries an LLM call and legitimately outlives
        # the read-verb timeout; the client owns wire shapes AND wire budgets (MC2).
        self.answer_timeout = answer_timeout
        self.llm_key = llm_key      # optional BYO key, sent as X-LLM-Key on answer()
        self.llm_model = llm_model  # optional BYO model, sent as X-LLM-Model on answer()
        self._owns_client = client is None
        self._client = client or httpx.Client(base_url=base_url, timeout=timeout)

    # ── canvas (CX3: immutable default + per-call override) ─────────────
    @property
    def canvas(self) -> str:
        """The canvas this client targets when a call names none — set ONCE at
        construction and immutable thereafter (CX3). Mutating it was how one
        conversation silently redirected every other conversation sharing the
        process (Gap 1, observed live 2026-08-17/18/19)."""
        return self._default_canvas

    @canvas.setter
    def canvas(self, value: object) -> None:
        raise AttributeError(
            "JuneClient.canvas is immutable (CX3): the default set at construction "
            "cannot move, so no caller can redirect another caller's requests. "
            "Pass canvas=... on the individual call for a one-call override, or "
            "derive a bound view with for_canvas(...) — nothing is remembered "
            "between calls.")

    def for_canvas(self, canvas: str) -> "JuneClient":
        """A VIEW of this client bound to ``canvas`` as its immutable default.
        Shares the transport (and never closes it — the view does not own it),
        so views are cheap; deriving one never affects this client."""
        return JuneClient(api_key=self.api_key, client=self._client,
                          canvas=canvas, answer_timeout=self.answer_timeout,
                          llm_key=self.llm_key, llm_model=self.llm_model)

    # ── context manager ─────────────────────────────────────────────────
    def __enter__(self) -> "JuneClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _headers(self, extra: dict[str, str] | None = None, *,
                 canvas: str | None = None) -> dict[str, str]:
        h = {"X-API-Key": self.api_key}
        effective = canvas if canvas else self._default_canvas   # falsy ⇒ inherit
        if effective:
            h["X-Canvas"] = effective
        if extra:
            h.update(extra)
        return h

    # ── API ─────────────────────────────────────────────────────────────
    def healthz(self) -> dict[str, Any]:
        r = self._client.get("/healthz")
        r.raise_for_status()
        return r.json()

    def ingest(
        self, *,
        nodes: Sequence[dict[str, Any]] = (),
        proposals: Sequence[dict[str, Any]] = (),
        idempotency_key: str | None = None, canvas: str | None = None,
    ) -> dict[str, Any]:
        """Atomically push nodes + edge proposals. Returns the write counts.

        Pass ``idempotency_key`` to make a retried push safe (the service serves
        the original response from cache instead of re-applying).
        """
        extra = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        r = self._client.post(
            "/v1/ingest",
            json={"nodes": list(nodes), "proposals": list(proposals)},
            headers=self._headers(extra, canvas=canvas),
        )
        r.raise_for_status()
        return r.json()

    def propose(self, proposals: Sequence[dict[str, Any]], canvas: str | None = None) -> dict[str, Any]:
        """S12c: submit machine-proposed facts to the DORMANT review queue.

        Unlike :meth:`ingest`, nothing goes live — a human approves each fact
        in the shared queue (`/v1/resolution/suggestions`) before it can affect
        any read."""
        r = self._client.post("/v1/proposals",
                              json={"proposals": list(proposals)},
                              headers=self._headers(canvas=canvas))
        r.raise_for_status()
        return r.json()

    def graph(self, *, limit: int = 100, min_confidence: float = 0.0, canvas: str | None = None) -> dict[str, Any]:
        """Fetch the workspace graph (edges + hydrated node labels + budget)."""
        return self._get("/v1/graph", {"limit": limit, "min_confidence": min_confidence}, canvas=canvas)

    # ── node-anchored reads ─────────────────────────────────────────────
    def neighborhood(
        self, node_id: str | uuid.UUID, node_type: str, *,
        direction: str = "both", min_confidence: float = 0.0, limit: int = 100,
        edge_kinds: Sequence[str] | None = None, canvas: str | None = None,
    ) -> dict[str, Any]:
        """1-hop edges around a node (+ hydrated labels + budget)."""
        return self._get("/v1/neighborhood", {
            "node_id": str(node_id), "node_type": node_type, "direction": direction,
            "min_confidence": min_confidence, "limit": limit,
            "edge_kinds": list(edge_kinds) if edge_kinds else None,
        }, canvas=canvas)

    def backlinks(
        self, node_id: str | uuid.UUID, node_type: str, *,
        min_confidence: float = 0.0, limit: int = 100,
        edge_kinds: Sequence[str] | None = None, canvas: str | None = None,
    ) -> dict[str, Any]:
        """Inbound edges to a node."""
        return self._get("/v1/backlinks", {
            "node_id": str(node_id), "node_type": node_type,
            "min_confidence": min_confidence, "limit": limit,
            "edge_kinds": list(edge_kinds) if edge_kinds else None,
        }, canvas=canvas)

    def subgraph(
        self, node_id: str | uuid.UUID, node_type: str, *,
        depth: int = 1, min_confidence: float = 0.0, max_edges: int = 500,
        edge_kinds: Sequence[str] | None = None, canvas: str | None = None,
    ) -> dict[str, Any]:
        """Depth-N neighbourhood (+ per-edge decay weights)."""
        return self._get("/v1/subgraph", {
            "node_id": str(node_id), "node_type": node_type, "depth": depth,
            "min_confidence": min_confidence, "max_edges": max_edges,
            "edge_kinds": list(edge_kinds) if edge_kinds else None,
        }, canvas=canvas)

    # ── edge edit verbs ─────────────────────────────────────────────────
    def create_edge(
        self, source_node_id: str | uuid.UUID, source_node_type: str,
        target_node_id: str | uuid.UUID, target_node_type: str,
        edge_kind: str, *, confidence: float = 1.0, canvas: str | None = None,
    ) -> dict[str, Any]:
        """Create a user-authored edge. Returns ``{status, edge_id}``."""
        r = self._client.post("/v1/edges", headers=self._headers(canvas=canvas), json={
            "source_node_id": str(source_node_id), "source_node_type": source_node_type,
            "target_node_id": str(target_node_id), "target_node_type": target_node_type,
            "edge_kind": edge_kind, "confidence": confidence,
        })
        r.raise_for_status()
        return r.json()

    def revoke_edge(self, edge_id: str | uuid.UUID, canvas: str | None = None) -> dict[str, Any]:
        """Soft-delete an edge (gone from all reads)."""
        r = self._client.post(f"/v1/edges/{edge_id}/revoke", headers=self._headers(canvas=canvas))
        r.raise_for_status()
        return r.json()

    def correct_edge(self, edge_id: str | uuid.UUID, *, confidence: float | None = None, canvas: str | None = None) -> dict[str, Any]:
        """Record a correction (increments user_corrections; optional new confidence)."""
        r = self._client.post(f"/v1/edges/{edge_id}/correct",
                              headers=self._headers(canvas=canvas), json={"confidence": confidence})
        r.raise_for_status()
        return r.json()

    def delete_edge(self, edge_id: str | uuid.UUID, canvas: str | None = None) -> dict[str, Any]:
        """Permanently delete an edge (admin-only on the server)."""
        r = self._client.request("DELETE", f"/v1/edges/{edge_id}", headers=self._headers(canvas=canvas))
        r.raise_for_status()
        return r.json()

    # ── unified fused search ──────────────────────────────────────────────
    def search(
        self, *, query: str = "", seeds: list[dict[str, Any]] | None = None,
        limit: int = 20, min_confidence: float = 0.0,
        edge_kinds: list[str] | None = None, deep: bool = False, canvas: str | None = None,
    ) -> dict[str, Any]:
        """One fused, budget-transparent search over the knowledge graph.

        ``seeds`` is a list of ``{"node_id": ..., "node_type": ...}``. Returns
        ranked items with per-lane provenance + ``degraded_lanes``."""
        body = {"query": query, "seeds": seeds or [], "limit": limit,
                "min_confidence": min_confidence, "edge_kinds": edge_kinds, "deep": deep}
        r = self._client.post("/v1/search", headers=self._headers(canvas=canvas), json=body)
        r.raise_for_status()
        return r.json()

    def search_health(self, canvas: str | None = None) -> dict[str, Any]:
        """Integration health beacon for the search seam (lanes/fusion available)."""
        r = self._client.get("/v1/search/health", headers=self._headers(canvas=canvas))
        r.raise_for_status()
        return r.json()

    def whoami(self, canvas: str | None = None) -> dict[str, Any]:
        """The connection's identity + entitlement as the SERVICE resolves it
        (``GET /v1/whoami``): ``{workspace_id, tier, features, edition_tag}``.
        Display/UX seam only — entitlements are enforced server-side per route
        regardless of what any caller shows."""
        r = self._client.get("/v1/whoami", headers=self._headers(canvas=canvas))
        r.raise_for_status()
        return r.json()

    # ── canvas management (create/list — SELECTION stays via X-Canvas) ───
    def list_canvases(self) -> list[dict[str, Any]]:
        """All canvases owned by this key (``GET /v1/canvases``):
        ``[{canvas_id, name, created_at}, …]``. Sent deliberately WITHOUT the
        ``X-Canvas`` header: canvas *management* must never be gated on the
        current selection being valid (mirrors the route's own ``get_caller``
        posture), otherwise a stale selection locks you out of the very call
        that would fix it."""
        r = self._client.get("/v1/canvases", headers={"X-API-Key": self.api_key})
        r.raise_for_status()
        return r.json()

    def create_canvas(self, name: str) -> dict[str, Any]:
        """Mint a canvas (``POST /v1/canvases``) → ``{canvas_id, name, created_at}``.
        Same no-``X-Canvas`` posture as :meth:`list_canvases`."""
        r = self._client.post("/v1/canvases", headers={"X-API-Key": self.api_key},
                              json={"name": name})
        r.raise_for_status()
        return r.json()

    def clear_canvas(self, canvas_id: str | uuid.UUID) -> dict[str, Any]:
        """IRREVERSIBLY erase every node + edge in a canvas, keeping the canvas
        (``POST /v1/canvases/{id}/clear``; owner-fenced server-side) →
        ``{canvas_id, nodes_deleted, edges_deleted}``. Same no-``X-Canvas``
        management posture as :meth:`list_canvases` — the op names its target
        explicitly and must not depend on the current selection being valid."""
        r = self._client.post(f"/v1/canvases/{canvas_id}/clear",
                              headers={"X-API-Key": self.api_key})
        r.raise_for_status()
        return r.json()

    def delete_canvas(self, canvas_id: str | uuid.UUID) -> dict[str, Any]:
        """IRREVERSIBLY erase a canvas's graph AND remove the canvas itself
        (``DELETE /v1/canvases/{id}``; owner-fenced) →
        ``{canvas_id, nodes_deleted, edges_deleted, deleted}``. Same management
        posture as :meth:`clear_canvas`."""
        r = self._client.request("DELETE", f"/v1/canvases/{canvas_id}",
                                 headers={"X-API-Key": self.api_key})
        r.raise_for_status()
        return r.json()

    def resolve(self, *, strong_only: bool = True,
                min_confidence: float = 0.62, canvas: str | None = None) -> dict[str, Any]:
        """Run cross-format entity resolution SERVER-SIDE over the bound canvas
        (``POST /v1/resolve``): the service scans its own nodes (server-bounded),
        computes reversible ``same_as`` merges with the pure resolver, and writes
        them through the core ingest seam. Returns
        ``{same_as_written, groups, candidates}``."""
        r = self._client.post("/v1/resolve", headers=self._headers(canvas=canvas),
                              json={"strong_only": bool(strong_only),
                                    "min_confidence": float(min_confidence)})
        r.raise_for_status()
        return r.json()

    # ── context pack (resolution-aware, budget-bounded) ──────────────────
    def context(
        self, *, query: str = "", seeds: list[dict[str, Any]] | None = None,
        limit: int = 20, token_budget: int = 2000, max_items: int = 20,
        min_confidence: float = 0.0, edge_kinds: list[str] | None = None,
        mode: str = "local", canvas: str | None = None,
    ) -> dict[str, Any]:
        """One call → a ready-to-prompt context pack: ranked items folded to
        canonical (via ``same_as``) with aliases + provenance, trimmed to a token
        budget. The agent-friendly read surface (also exposed over MCP)."""
        body = {"query": query, "seeds": seeds or [], "limit": limit,
                "token_budget": token_budget, "max_items": max_items,
                "min_confidence": min_confidence, "edge_kinds": edge_kinds, "mode": mode}
        r = self._client.post("/v1/context", headers=self._headers(canvas=canvas), json=body)
        r.raise_for_status()
        return r.json()

    # ── grounded answer (June's flagship read verb) ───────────────────────
    def answer(
        self, *, query: str, seeds: list[dict[str, Any]] | None = None,
        limit: int = 20, token_budget: int = 2000, max_items: int = 20,
        min_confidence: float = 0.0, edge_kinds: list[str] | None = None,
        mode: str = "local", multihop: bool = False, max_subqueries: int = 4,
        passages: Sequence[str] = (), timeout: float | None = None, canvas: str | None = None,
    ) -> dict[str, Any]:
        """One call → a grounded, cited answer (may abstain) from ``/v1/answer``.

        Uses the client's ``answer_timeout`` (per-verb budget) unless an explicit
        ``timeout`` is given; forwards the optional BYO ``llm_key``/``llm_model``
        as ``X-LLM-Key``/``X-LLM-Model`` headers (never logged, never in the body)."""
        body = {"query": query, "seeds": seeds or [], "limit": limit,
                "token_budget": token_budget, "max_items": max_items,
                "min_confidence": min_confidence, "edge_kinds": edge_kinds,
                "mode": mode, "multihop": multihop, "max_subqueries": max_subqueries,
                "passages": list(passages)}
        extra: dict[str, str] = {}
        if self.llm_key:
            extra["X-LLM-Key"] = self.llm_key
        if self.llm_model:
            extra["X-LLM-Model"] = self.llm_model
        eff_timeout = timeout if timeout is not None else self.answer_timeout
        kwargs: dict[str, Any] = {"headers": self._headers(extra or None, canvas=canvas), "json": body}
        if eff_timeout is not None:
            kwargs["timeout"] = eff_timeout
        r = self._client.post("/v1/answer", **kwargs)
        r.raise_for_status()
        return r.json()

    # ── text ingest (the natural "remember this" write verb) ─────────────
    def ingest_text(
        self, *, text: str, format: str = "markdown", source_app: str = "mcp", canvas: str | None = None,
    ) -> dict[str, Any]:
        """Server-side ingest of raw text (``/v1/ingest/text``): read → extract →
        map → graph, bounded input. Extraction is TIER-AWARE on the service: a Pro
        endpoint runs the richer entity/edge engines (BYO ``X-LLM-*`` forwarded
        here, same as ``answer()``); free endpoints run the deterministic floor.
        Returns counts + which ``engine`` ran."""
        extra: dict[str, str] = {}
        if self.llm_key:
            extra["X-LLM-Key"] = self.llm_key
        if self.llm_model:
            extra["X-LLM-Model"] = self.llm_model
        r = self._client.post("/v1/ingest/text", headers=self._headers(extra or None, canvas=canvas),
                              json={"text": text, "format": format,
                                    "source_app": source_app})
        r.raise_for_status()
        return r.json()

    def ingest_file(self, *, filename: str, data: bytes, canvas: str | None = None) -> dict[str, Any]:
        """Upload ONE file to ``/v1/ingest/file`` (multipart): the server picks the
        matching reader (pdf/docx/xlsx/csv/html/md/images/audio…), runs the same
        tier-aware extraction as the text path, and writes through the core ingest
        seam. Per-file fail-soft on the server; returns the per-file result rows +
        totals + which ``engine`` ran."""
        extra: dict[str, str] = {}
        if self.llm_key:
            extra["X-LLM-Key"] = self.llm_key
        if self.llm_model:
            extra["X-LLM-Model"] = self.llm_model
        r = self._client.post("/v1/ingest/file", headers=self._headers(extra or None, canvas=canvas),
                              files=[("files", (filename, data))])
        r.raise_for_status()
        return r.json()

    def enrich(self, canvas: str | None = None) -> dict[str, Any]:
        """Start a Pro-gated background enrichment of the bound canvas
        (``POST /v1/enrich``): re-extracts stored artifacts with the richer engine,
        idempotently (a second run writes 0 new). Returns ``{job_id, total, state}``;
        403 on free endpoints (the entitlement gate is server-side)."""
        extra: dict[str, str] = {}
        if self.llm_key:
            extra["X-LLM-Key"] = self.llm_key
        if self.llm_model:
            extra["X-LLM-Model"] = self.llm_model
        r = self._client.post("/v1/enrich", headers=self._headers(extra or None, canvas=canvas))
        r.raise_for_status()
        return r.json()

    def enrich_status(self, job_id: str, canvas: str | None = None) -> dict[str, Any]:
        """Progress of an enrichment job (``GET /v1/enrich/status``):
        ``{job_id, state, total, processed, nodes, edges, errors}``."""
        r = self._client.get("/v1/enrich/status", headers=self._headers(canvas=canvas),
                             params={"job": job_id})
        r.raise_for_status()
        return r.json()

    # ── enumeration (exhaustive structured retrieval; "list all X") ───────
    def enumerate(
        self, *, terms: list[str] | None = None, regex: str | None = None,
        node_types: list[str] | None = None, subtype: str | None = None,
        cap: int = 500, canvas: str | None = None,
    ) -> dict[str, Any]:
        """Return EVERY node matching a structured predicate (not a top-k slice) —
        the recall-complete path for "list all" questions. Workspace/canvas fenced."""
        body = {"terms": terms or [], "regex": regex, "node_types": node_types,
                "subtype": subtype, "cap": cap}
        r = self._client.post("/v1/enumerate", headers=self._headers(canvas=canvas), json=body)
        r.raise_for_status()
        return r.json()

    # ── pages (graph-native documents; server gates behind JUNE_PAGES) ────
    def list_pages(self, *, limit: int = 200, offset: int = 0, canvas: str | None = None) -> dict[str, Any]:
        """List the pages in the bound canvas (``GET /v1/pages``) →
        ``{pages:[{page_id,title,created_at,updated_at,pinned,pinned_ms,group}],
        has_more, next_offset}``. ``pinned``/``group`` (0.0.12+ engines) are the user's
        own organization of the pages list — honor them when presenting pages (pinned
        first, then grouped); older engines simply omit the fields."""
        return self._get("/v1/pages", {"limit": limit, "offset": offset}, canvas=canvas)

    def get_page(self, page_id: str | uuid.UUID, canvas: str | None = None) -> dict[str, Any]:
        """A page and its ordered blocks (``GET /v1/pages/{id}``)."""
        r = self._client.get(f"/v1/pages/{page_id}", headers=self._headers(canvas=canvas))
        r.raise_for_status()
        return r.json()

    def create_page(self, title: str, canvas: str | None = None) -> dict[str, Any]:
        """Create a page (``POST /v1/pages``) → ``{page_id, title}``."""
        r = self._client.post("/v1/pages", headers=self._headers(canvas=canvas), json={"title": title})
        r.raise_for_status()
        return r.json()

    def rename_page(self, page_id: str | uuid.UUID, title: str, canvas: str | None = None) -> dict[str, Any]:
        """Rename a page (``PUT /v1/pages/{id}``) → ``{page_id, title}``."""
        r = self._client.put(f"/v1/pages/{page_id}", headers=self._headers(canvas=canvas),
                             json={"title": title})
        r.raise_for_status()
        return r.json()

    def delete_page(self, page_id: str | uuid.UUID, canvas: str | None = None) -> dict[str, Any]:
        """Delete a page and its blocks (``DELETE /v1/pages/{id}``) — reversible tombstone
        server-side → ``{ok, page_id, blocks_deleted}``."""
        r = self._client.request("DELETE", f"/v1/pages/{page_id}", headers=self._headers(canvas=canvas))
        r.raise_for_status()
        return r.json()

    def save_blocks(self, page_id: str | uuid.UUID,
                    blocks: Sequence[dict[str, Any]],
                    *, expected_updated_at: str | None = None,
                    force: bool = False, canvas: str | None = None) -> dict[str, Any]:
        """Replace a page's blocks (``POST /v1/pages/{id}/blocks``) — an AUTHORITATIVE
        full-set save: blocks absent from the payload are removed. Each block is
        ``{block_type, text, order}`` and may carry an ``id`` to update an existing
        block in place (matched + ownership-fenced server-side). Returns the fresh
        page detail (``{page_id, title, updated_at, blocks:[…]}``).

        ``expected_updated_at`` is the page's ``updated_at`` exactly as a prior read
        issued it (W3 optimistic concurrency). Supply it and the server refuses the
        save with **409** when the page has moved since — raised here as
        :class:`PageRevisionConflict`.

        **A tokenless save is REFUSED here unless the caller passes ``force=True``.**
        That default is the actual fix, not the token: the 2026-08-17 data loss did not
        happen because someone chose to overwrite, it happened because overwriting was
        what you got by *not thinking about it*. Now the unguarded call cannot be reached
        by omission — only by naming it, in code, where it is greppable and reviewable.
        ``force=True`` is legitimate in exactly one shape: a page this process just
        created, which by definition holds nothing anyone else wrote.

        This is the seam that makes "read before you write" checkable rather than
        advisory: a caller that never read the page has no token to send."""
        if expected_updated_at is None and not force:
            raise ValueError(
                "save_blocks is an AUTHORITATIVE full-set save: blocks absent from the payload "
                "are deleted. Pass expected_updated_at (the `updated_at` from a get_page in this "
                "same operation) so a concurrent write is refused rather than silently lost — or "
                "pass force=True if overwriting whatever is there is the deliberate intent.")
        payload: dict[str, Any] = {"blocks": list(blocks)}
        if expected_updated_at is not None:
            payload["expected_updated_at"] = expected_updated_at
        r = self._client.post(f"/v1/pages/{page_id}/blocks", headers=self._headers(canvas=canvas),
                             json=payload)
        if r.status_code == 409:
            raise PageRevisionConflict(_detail(r) or "page has changed since it was read")
        r.raise_for_status()
        return r.json()

    def append_blocks(self, page_id: str | uuid.UUID,
                      blocks: Sequence[dict[str, Any]], canvas: str | None = None) -> dict[str, Any]:
        """ADD ``blocks`` after a page's current content WITHOUT resending it. Reads the
        page (carrying every existing block's id + order forward, so nothing is dropped by
        the authoritative full-set save), appends the new blocks after the highest existing
        order, and saves once. New blocks' ``order`` is assigned here (any value they carry
        is overwritten). Returns the fresh page detail.

        It is a read-then-write on the client, so the window between the read and the save
        is real: another writer landing inside it would previously have been **silently
        reverted** by this full-set save. That no longer happens — the save carries the
        revision token from *this* method's own read, so a colliding write is refused
        server-side. On a refusal we re-read and re-append ONCE (the merge is a pure
        append, so replaying it against fresher content is exactly right); a second
        collision raises :class:`PageRevisionConflict` rather than guessing again."""
        for attempt in (1, 2):
            detail = self.get_page(page_id, canvas=canvas)
            existing = detail.get("blocks") or []
            merged: list[dict[str, Any]] = [
                {"id": b.get("block_id"), "block_type": b.get("block_type"),
                 "text": b.get("text", ""), "order": b.get("order", 0.0)}
                for b in existing if b.get("block_id")
            ]
            base = max((float(b.get("order", 0.0)) for b in existing), default=0.0)
            for i, nb in enumerate(blocks, start=1):
                row = dict(nb)
                row["order"] = base + i
                merged.append(row)
            # Capability-gated, not version-branched: an engine old enough not to issue a
            # revision token cannot be protected, and refusing the append there would break a
            # SAFE verb against older servers. Appends were unguarded everywhere until now, so
            # proceeding is the status quo, not a regression — the guard simply switches on
            # wherever the server can honour it.
            rev = detail.get("updated_at")
            try:
                return self.save_blocks(page_id, merged, expected_updated_at=rev,
                                        force=rev is None, canvas=canvas)
            except PageRevisionConflict:
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")   # pragma: no cover

    # ── internal ────────────────────────────────────────────────────────
    def _get(self, path: str, params: dict[str, Any], *,
             canvas: str | None = None) -> dict[str, Any]:
        clean = {k: v for k, v in params.items() if v is not None}
        r = self._client.get(path, params=clean,
                             headers=self._headers(canvas=canvas))
        r.raise_for_status()
        return r.json()


__all__ = ["JuneClient", "PageRevisionConflict", "node", "edge"]
