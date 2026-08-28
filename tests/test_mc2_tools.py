"""MC2 step tests — tool surface: june_answer/june_remember, descriptions, clamps,
read-only fence, bounded resolve. httpx.MockTransport throughout (no server, no
network) — same seam the SB3 june-bench tests used.
"""
from __future__ import annotations

import json
import os
import unittest

try:
    import httpx

    from june_client import JuneClient
    from june_mcp.server import tool_manifest
    from june_mcp.tools import (
        MAX_DEPTH, MAX_LIMIT, MAX_REMEMBER_CHARS, MAX_TOKEN_BUDGET,
        TOOLS, run_tool, visible_tools,
    )
    _IMPORT_OK, _IMPORT_ERR = True, ""
except Exception as exc:  # pragma: no cover
    _IMPORT_OK, _IMPORT_ERR = False, repr(exc)


def _client(handler, **kw) -> "JuneClient":
    transport = httpx.MockTransport(handler)
    http = httpx.Client(base_url="http://june.test", transport=transport)
    return JuneClient("http://june.test", "june_sk_test", client=http,
                      canvas="11111111-1111-1111-1111-111111111111", **kw)


@unittest.skipUnless(_IMPORT_OK, f"june_mcp unavailable: {_IMPORT_ERR}")
class TestSurface(unittest.TestCase):
    """The manifest is the agent's prompt — assert its shape mechanically."""

    def test_eight_tools_with_template_descriptions(self) -> None:
        import os as _os
        manifest = tool_manifest()
        names = [t["name"] for t in manifest]
        # 14 reads + 15 writes (+ june_ingest_file only when JUNE_FILES_ROOT is set). Writes
        # include the five page-compose verbs (create/write/append/update/delete — update
        # is CX12), the three canvas-management writes (create/clear/delete), and the
        # three Phase-AM doc writes (doc_save/doc_delete/learn); reads hold the three
        # canvas verbs (list/current/use) and the three AM doc reads
        # (docs_refresh/doc_list/doc_get).
        expected = (29 + (1 if _os.environ.get("JUNE_FILES_ROOT", "").strip() else 0)
                    + (3 if _os.environ.get("JUNE_EXPORT_ROOT", "").strip() else 0))
        self.assertEqual(len(names), expected)
        for p in ("june_page_list", "june_page_get", "june_page_create",
                  "june_page_write", "june_page_append", "june_page_delete"):
            self.assertIn(p, names)
        self.assertIn("june_resolve", names)   # universal: resolution runs server-side
        self.assertIn("june_enumerate", names)
        self.assertIn("june_enrich", names)
        for expected in ("june_answer", "june_remember"):
            self.assertIn(expected, names)
        for t in manifest:
            self.assertIn("Use", t["description"],
                          f"{t['name']} description lacks a 'Use when' clause")
            self.assertGreater(len(t["description"]), 80,
                               f"{t['name']} description too thin to guide selection")
            self.assertEqual(t["input_schema"]["type"], "object")

    def test_answer_is_first_tool(self) -> None:
        # Ordering is deliberate: hosts show tools in list order; the flagship leads.
        self.assertEqual(TOOLS[0].name, "june_answer")

    def test_write_verbs_marked(self) -> None:
        writes = {t.name for t in TOOLS if t.writes}
        self.assertEqual(writes, {"june_remember", "june_ingest", "june_resolve",
                                 "june_ingest_file", "june_enrich",
                                 "june_page_create", "june_page_write", "june_page_append",
                                 "june_page_update", "june_page_delete",
                                 "june_canvas_create", "june_canvas_clear", "june_canvas_delete",
                                 "june_doc_save", "june_doc_delete", "june_learn",
                                 "june_page_import"})


@unittest.skipUnless(_IMPORT_OK, f"june_mcp unavailable: {_IMPORT_ERR}")
class TestAnswerTool(unittest.TestCase):
    def test_answer_posts_v1_answer_with_budget_and_headers(self) -> None:
        seen = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["path"] = req.url.path
            seen["body"] = json.loads(req.content)
            seen["llm_key"] = req.headers.get("X-LLM-Key")
            seen["api_key"] = req.headers.get("X-API-Key")
            seen["canvas"] = req.headers.get("X-Canvas")
            seen["timeout"] = req.extensions.get("timeout")
            return httpx.Response(200, json={
                "answer": "Meridian's renewal is in Q3.",
                "citations": [], "used_edge_ids": [], "degraded": [], "mode": "local"})

        client = _client(handler, answer_timeout=99.0, llm_key="or_sk_BYO")
        out = run_tool("june_answer", client, {"query": "when is the renewal?"})
        self.assertEqual(out["answer"], "Meridian's renewal is in Q3.")
        self.assertEqual(seen["path"], "/v1/answer")
        self.assertEqual(seen["body"]["query"], "when is the renewal?")
        self.assertEqual(seen["llm_key"], "or_sk_BYO")          # BYO key forwarded
        self.assertEqual(seen["api_key"], "june_sk_test")
        self.assertEqual(seen["canvas"], "11111111-1111-1111-1111-111111111111")
        self.assertEqual(seen["timeout"]["read"], 99.0)          # per-verb budget applied

    def test_answer_clamps_and_notes(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content)
            assert body["limit"] <= MAX_LIMIT and body["token_budget"] <= MAX_TOKEN_BUDGET
            return httpx.Response(200, json={"answer": "", "citations": [],
                                             "used_edge_ids": [], "degraded": ["abstain:none"],
                                             "mode": "local"})

        out = run_tool("june_answer", _client(handler),
                       {"query": "x", "limit": 10_000, "token_budget": 10**9})
        self.assertIn("_clamped", out)
        self.assertIn("limit", out["_clamped"])
        self.assertIn("token_budget", out["_clamped"])

    def test_answer_query_required_by_schema(self) -> None:
        answer = next(t for t in TOOLS if t.name == "june_answer")
        self.assertIn("query", answer.input_schema["required"])


@unittest.skipUnless(_IMPORT_OK, f"june_mcp unavailable: {_IMPORT_ERR}")
class TestRememberTool(unittest.TestCase):
    def test_remember_posts_ingest_text(self) -> None:
        seen = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["path"] = req.url.path
            seen["body"] = json.loads(req.content)
            return httpx.Response(200, json={"nodes_written": 3, "edges_written": 2,
                                             "format": "markdown", "source_app": "mcp"})

        out = run_tool("june_remember", _client(handler),
                       {"text": "Acme renewed for two years."})
        self.assertEqual(seen["path"], "/v1/ingest/text")
        self.assertEqual(seen["body"]["format"], "markdown")
        self.assertEqual(seen["body"]["source_app"], "mcp")
        self.assertEqual(out["nodes_written"], 3)

    def test_remember_rejects_empty_and_truncates_oversize(self) -> None:
        with self.assertRaises(ValueError):
            run_tool("june_remember", _client(lambda r: httpx.Response(200, json={})),
                     {"text": "   "})

        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content)
            assert len(body["text"]) == MAX_REMEMBER_CHARS
            return httpx.Response(200, json={"nodes_written": 1, "edges_written": 0,
                                             "format": "text", "source_app": "mcp"})

        out = run_tool("june_remember", _client(handler),
                       {"text": "x" * (MAX_REMEMBER_CHARS + 500), "format": "text"})
        self.assertIn("text", out["_clamped"])


@unittest.skipUnless(_IMPORT_OK, f"june_mcp unavailable: {_IMPORT_ERR}")
class TestReadonlyFence(unittest.TestCase):
    """C8/C4: read-only posture removes write verbs from list AND execution."""

    def test_visible_tools_hides_writes(self) -> None:
        import os as _os
        names = {t.name for t in visible_tools(readonly=True)}
        # Reads survive read-only; page_list/page_get are reads, so they stay too.
        self.assertEqual(names, {"june_answer", "june_search", "june_enumerate",
                                 "june_context", "june_neighborhood", "june_subgraph",
                                 "june_page_list", "june_page_get",
                                 "june_canvas_list", "june_canvas_current", "june_canvas_use",
                                 "june_docs_refresh", "june_doc_list", "june_doc_get"})
        expected = (29 + (1 if _os.environ.get("JUNE_FILES_ROOT", "").strip() else 0)
                    + (3 if _os.environ.get("JUNE_EXPORT_ROOT", "").strip() else 0))
        self.assertEqual(len(visible_tools(readonly=False)), expected)

    def test_manifest_respects_readonly(self) -> None:
        self.assertEqual(len(tool_manifest(readonly=True)), 14)  # 6 core reads + 2 page reads + 3 canvas reads + 3 AM doc reads (docs_refresh/doc_list/doc_get)

    def test_run_tool_refuses_writes_in_readonly(self) -> None:
        client = _client(lambda r: httpx.Response(200, json={}))
        for name in ("june_remember", "june_ingest", "june_resolve", "june_enrich"):
            with self.assertRaises(KeyError) as ctx:
                run_tool(name, client, {"text": "x"}, readonly=True)
            self.assertIn("read-only", str(ctx.exception))

    def test_reads_still_work_in_readonly(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"items": [], "degraded_lanes": []})
        out = run_tool("june_search", _client(handler), {"query": "q"}, readonly=True)
        self.assertIn("items", out)


@unittest.skipUnless(_IMPORT_OK, f"june_mcp unavailable: {_IMPORT_ERR}")
class TestTraversalClampsAndResolveBounds(unittest.TestCase):
    def test_subgraph_depth_clamped(self) -> None:
        seen = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(req.content) if req.content else {}
            seen["url"] = str(req.url)
            return httpx.Response(200, json={"nodes": [], "edges": [], "budget": {}})

        out = run_tool("june_subgraph", _client(handler),
                       {"node_id": "n1", "node_type": "entity", "depth": 99})
        self.assertEqual(out["_clamped"]["depth"], f"99 → {MAX_DEPTH}")

    def test_resolve_runs_server_side_and_stays_conservative(self) -> None:
        seen: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            # Server-side resolution: ONE call, to /v1/resolve — never a graph
            # pull + client-side merge (that path needed engine code).
            self.assertEqual(req.url.path, "/v1/resolve")
            seen.update(json.loads(req.content))
            return httpx.Response(200, json={"same_as_written": 0, "groups": 0,
                                             "candidates": 0})

        out = run_tool("june_resolve", _client(handler), {"limit": 10_000})
        self.assertEqual(out["same_as_written"], 0)
        self.assertEqual(seen["strong_only"], True)          # conservative default (U12)
        self.assertEqual(seen["min_confidence"], 0.62)
        self.assertIn("limit", out["_clamped"])              # ignored, but VISIBLY noted

    def test_whoami_is_a_plain_authed_get(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            self.assertEqual(req.url.path, "/v1/whoami")
            self.assertEqual(req.headers.get("X-API-Key"), "june_sk_test")
            return httpx.Response(200, json={"workspace_id": "w", "tier": "pro",
                                             "features": ["entities_ml"],
                                             "edition_tag": "june-pro"})

        who = _client(handler).whoami()
        self.assertEqual(who["edition_tag"], "june-pro")

    def test_enumerate_is_recall_complete_verb(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            self.assertEqual(req.url.path, "/v1/enumerate")
            body = json.loads(req.content)
            self.assertEqual(body["cap"], 500)               # default, in bounds
            return httpx.Response(200, json={"items": [], "total": 0})

        out = run_tool("june_enumerate", _client(handler), {"terms": ["customer"]})
        self.assertEqual(out["total"], 0)

    def test_enrich_start_and_poll_are_one_verb(self) -> None:
        calls: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(f"{req.method} {req.url.path}")
            if req.url.path == "/v1/enrich":
                return httpx.Response(200, json={"job_id": "j1", "total": 3,
                                                 "state": "running"})
            return httpx.Response(200, json={"job_id": "j1", "state": "done",
                                             "total": 3, "processed": 3, "nodes": 9,
                                             "edges": 4, "errors": 0})

        client = _client(handler)
        start = run_tool("june_enrich", client, {})
        self.assertEqual(start["job_id"], "j1")
        status = run_tool("june_enrich", client, {"job": "j1"})
        self.assertEqual(status["state"], "done")
        self.assertEqual(calls, ["POST /v1/enrich", "GET /v1/enrich/status"])

    def test_remember_forwards_byo_llm_key(self) -> None:
        # The remember path is tier-aware server-side; the client must ride the
        # BYO key on it exactly like answer() does (never in the body, never logged).
        def handler(req: httpx.Request) -> httpx.Response:
            self.assertEqual(req.url.path, "/v1/ingest/text")
            self.assertEqual(req.headers.get("X-LLM-Key"), "byo-secret")
            self.assertNotIn(b"byo-secret", req.content)
            return httpx.Response(200, json={"nodes_written": 1, "edges_written": 0,
                                             "format": "markdown", "source_app": "mcp",
                                             "engine": "hosted"})

        out = run_tool("june_remember", _client(handler, llm_key="byo-secret"),
                       {"text": "Vireo renewed."})
        self.assertEqual(out["engine"], "hosted")


@unittest.skipUnless(_IMPORT_OK, f"june_mcp unavailable: {_IMPORT_ERR}")
class TestIngestFileFences(unittest.TestCase):
    """The _ingest_file handler's fences, tested directly (availability is decided
    at import from JUNE_FILES_ROOT — the spawn-env contract — so run_tool-level
    visibility is covered by the stdio test; here we prove the path fences)."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "note.md").write_text("# hello")
        self._old = os.environ.get("JUNE_FILES_ROOT")
        os.environ["JUNE_FILES_ROOT"] = str(self.root)

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("JUNE_FILES_ROOT", None)
        else:
            os.environ["JUNE_FILES_ROOT"] = self._old
        self._tmp.cleanup()

    def test_upload_inside_root(self) -> None:
        from june_mcp.tools import _ingest_file

        def handler(req: httpx.Request) -> httpx.Response:
            self.assertEqual(req.url.path, "/v1/ingest/file")
            self.assertIn(b"hello", req.content)
            return httpx.Response(200, json={"files": [], "nodes_written": 1,
                                             "edges_written": 0, "engine": "floor",
                                             "hosted_degraded": False})

        out = _ingest_file(_client(handler), {"path": "note.md"})
        self.assertEqual(out["nodes_written"], 1)

    def test_escape_is_refused(self) -> None:
        from june_mcp.tools import _ingest_file
        for evil in ("../etc/passwd", "/etc/passwd", "a/../../etc/passwd"):
            with self.assertRaises(ValueError) as ctx:
                _ingest_file(_client(lambda r: httpx.Response(200, json={})),
                             {"path": evil})
            self.assertIn("JUNE_FILES_ROOT", str(ctx.exception))

    def test_disabled_without_root(self) -> None:
        from june_mcp.tools import _ingest_file
        os.environ.pop("JUNE_FILES_ROOT", None)
        with self.assertRaises(ValueError) as ctx:
            _ingest_file(_client(lambda r: httpx.Response(200, json={})),
                         {"path": "note.md"})
        self.assertIn("disabled", str(ctx.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

@unittest.skipUnless(_IMPORT_OK, f"june_mcp unavailable: {_IMPORT_ERR}")
class TestMoatScan(unittest.TestCase):
    """C14: the MCP surface must never leak internal model identifiers
    (same banned-literal posture as tests/architecture/test_no_moat_leak.py;
    bge is exempt as a published benchmark parameter — see june-bench 0.0.27+)."""

    BANNED = ("gliner", "nomic", "minilm", "e5-", "instructor-")

    def test_manifest_carries_no_model_identifiers(self) -> None:
        blob = json.dumps(tool_manifest()).lower()
        for lit in self.BANNED:
            self.assertNotIn(lit, blob, f"moat leak: {lit!r} in tool manifest")

    def test_error_mapper_carries_no_model_identifiers(self) -> None:
        from june_mcp.runtime import map_error
        msgs = " ".join(map_error(e) for e in (
            httpx.ReadTimeout("x"), httpx.ConnectError("y"),
            RuntimeError("z"), KeyError("unknown tool"))).lower()
        for lit in self.BANNED:
            self.assertNotIn(lit, msgs)
