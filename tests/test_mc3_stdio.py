"""MC3 protocol tests — the MC2 surface over REAL stdio: june_answer, june_remember,
and the read-only posture, end to end through a spawned `python -m june_mcp`.

Extends test_mc1_stdio (spawn/list/search/error/survive) with the verbs MC2 added —
they were MockTransport-proven in test_mc2_tools; here they cross the actual JSON-RPC
wire. Same stub-June isolation: a failure here is a transport/surface bug, never an
engine bug. Dev-machine tier; skips cleanly without the `mcp` extra.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    import anyio
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    _MCP_OK, _MCP_ERR = True, ""
except Exception as exc:  # pragma: no cover
    _MCP_OK, _MCP_ERR = False, repr(exc)

KEY = "june_sk_mc3"


def _pin_spawn_to_imported_june_mcp(env: dict) -> None:
    """The spawned ``-m june_mcp`` must resolve the SAME package this test process
    imports — without the pin, an installed june-mcp shadows the repo copy in the
    child and every assertion runs against the wrong artifact. Fourth occurrence
    of the class (2026-08-20, caught by release.sh's hermetic preflight: the repo
    venv's stale install made the old 22-tool pin pass falsely while the current
    code truthfully serves 29)."""
    import june_mcp
    parent = os.path.dirname(os.path.dirname(os.path.abspath(june_mcp.__file__)))
    prior = env.get("PYTHONPATH")
    env["PYTHONPATH"] = parent if not prior else parent + os.pathsep + prior




class _StubJune(BaseHTTPRequestHandler):
    """Stub June: /v1/answer and /v1/ingest/text (the MC2 verbs) + /healthz."""

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._send(200, {"ok": True})
        elif self.path == "/v1/canvases":
            # MC-N1: JUNE_CANVAS carries a NAME; the spawned server resolves it
            # here at startup (real CanvasOut rows, per the route model).
            self._send(200, [{"canvas_id": "5a6b7c8d-9e0f-4a1b-8c2d-3e4f5a6b7c8d",
                              "name": "mcp-trial",
                              "created_at": "2026-07-08T00:00:00Z"}])
        else:
            self._send(404, {"detail": "nf"})

    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/v1/answer":
            self._send(200, {"answer": f"grounded: {body['query']}",
                             "citations": [{"node_id": "n1"}], "used_edge_ids": [],
                             "degraded": [], "mode": "local"})
        elif self.path == "/v1/ingest/text":
            self._send(200, {"nodes": 2, "edges": 1, "format": body.get("format", "?"),
                             "source_app": body.get("source_app", "?")})
        else:
            self._send(404, {"detail": "nf"})

    def log_message(self, *a: object) -> None:
        pass


@unittest.skipUnless(_MCP_OK, f"mcp client SDK unavailable: {_MCP_ERR}")
class TestMc2SurfaceOverStdio(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = HTTPServer(("127.0.0.1", 0), _StubJune)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()

    def _params(self, **extra_env: str) -> "StdioServerParameters":
        env = {k: v for k, v in os.environ.items()
               if k.startswith(("PATH", "PYTHON", "LANG", "LC_"))}
        env.update({"JUNE_BASE_URL": f"http://127.0.0.1:{self.port}",
                    "JUNE_API_KEY": KEY, "JUNE_CANVAS": "mcp-trial", **extra_env})
        _pin_spawn_to_imported_june_mcp(env)
        return StdioServerParameters(command=sys.executable,
                                     args=["-m", "june_mcp"], env=env)

    def test_answer_and_remember_over_stdio(self) -> None:
        async def scenario() -> None:
            async with stdio_client(self._params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    tools = await session.list_tools()
                    names = [t.name for t in tools.tools]
                    # D6: the wire default is compact — 20 tools, not the 30 members. `full` is
                    # asserted below, so both halves of the decision are pinned on the wire.
                    self.assertEqual(len(names), 20)
                    self.assertEqual(names[0], "june_answer")   # flagship leads

                    res = await session.call_tool(
                        "june_answer", {"query": "when is Meridian's renewal?"})
                    payload = json.loads(res.content[0].text)
                    self.assertEqual(payload["answer"],
                                     "grounded: when is Meridian's renewal?")
                    self.assertTrue(payload["citations"])

                    res = await session.call_tool(
                        "june_remember", {"text": "Acme renewed for two years."})
                    self.assertEqual(json.loads(res.content[0].text)["nodes"], 2)

        anyio.run(scenario)

    def test_full_profile_is_one_env_var_away(self) -> None:
        """D6's other half: flipping the default did not remove the unfolded surface. With
        JUNE_TOOL_PROFILE=full a connection gets the same 30 member tools it always got, by their
        own names, and they are callable — so an operator who wants the old shape keeps it."""
        async def scenario() -> None:
            async with stdio_client(self._params(JUNE_TOOL_PROFILE="full")) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    names = [t.name for t in (await session.list_tools()).tools]
                    self.assertEqual(len(names), 30)
                    self.assertEqual(names[0], "june_answer")
                    for member in ("june_page_get", "june_page_list", "june_docs_refresh",
                                   "june_neighborhood", "june_canvas_delete"):
                        self.assertIn(member, names)
                    self.assertFalse({"june_page_read", "june_graph"} & set(names))
                    res = await session.call_tool("june_answer", {"query": "x"})
                    self.assertFalse(res.isError)

        anyio.run(scenario)

    def test_readonly_posture_over_stdio(self) -> None:
        async def scenario() -> None:
            async with stdio_client(self._params(JUNE_READONLY="1")) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    tools = await session.list_tools()
                    names = {t.name for t in tools.tools}
                    # On the wire default (compact) the same READS appear, folded: nine tools
                    # instead of fifteen. Every write verb is still gone — from the list AND
                    # from the execution path, asserted below.
                    self.assertEqual(names, {"june_answer", "june_search",
                                             "june_enumerate", "june_context",
                                             # neighborhood + subgraph
                                             "june_graph",
                                             # page READS survive read-only (list + get); every
                                             # page write is hidden, so june_page_edit and
                                             # june_page_write are absent entirely.
                                             "june_page_read",
                                             # canvas READS survive (list/current/use); create
                                             # and the erase family are hidden like writes.
                                             "june_canvas_read",
                                             # Phase AM doc READS survive (refresh/list/get);
                                             # doc_save/doc_delete/learn hide like writes.
                                             "june_docs_read",
                                             # usage receipts are a read (2026-09-04)
                                             "june_usage"})
                    # and the ops a read-only family offers are only the read ops
                    ops = {t.name: t.inputSchema["properties"]["op"]["enum"]
                           for t in tools.tools if "op" in t.inputSchema.get("properties", {})}
                    self.assertEqual(sorted(ops["june_page_read"]), ["get", "list"])
                    self.assertEqual(sorted(ops["june_canvas_read"]), ["current", "list", "use"])

                    # Addressing a write verb directly must refuse — and the refusal
                    # crosses the wire as a redacted, actionable error.
                    res = await session.call_tool("june_remember", {"text": "x"})
                    self.assertIn("read-only", res.content[0].text)

                    # …and reads still work afterwards (server alive).
                    res = await session.call_tool(
                        "june_answer", {"query": "still alive?"})
                    self.assertIn("grounded", json.loads(res.content[0].text)["answer"])

        anyio.run(scenario)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class _StubJuneWithWhoami(_StubJune):
    """A June that answers /v1/whoami (tier pro, features) but serves NO pages (/v1/pages → 404):
    the hosted shape. /v1/search → 500 so a real engine failure can be observed on the wire."""

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith("/v1/search"):
            self._send(500, {"detail": "boom"})
        else:
            super().do_POST()

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/v1/whoami"):
            self._send(200, {"workspace_id": "w", "tier": "pro",
                             "features": ["entities_ml", "llm_edges", "llm_extract"],
                             "edition_tag": "june-pro"})
        elif self.path.startswith("/v1/pages"):
            self._send(404, {"detail": "Not Found"})
        else:
            super().do_GET()


@unittest.skipUnless(_MCP_OK, f"mcp client SDK unavailable: {_MCP_ERR}")
class TestZeroFourTwoOverStdio(TestMc2SurfaceOverStdio):
    """0.4.2 on the wire: D1 capability gating, annotations + title on tools/list, isError on
    failures (N2)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = HTTPServer(("127.0.0.1", 0), _StubJuneWithWhoami)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    # the inherited MC2 tests assume a pages-serving engine and no whoami; they are not this
    # class's subject (here the stub 404s /v1/pages, so every count is the no-pages one)
    def test_answer_and_remember_over_stdio(self) -> None:  # noqa: D102
        self.skipTest("covered by TestMc2SurfaceOverStdio")

    def test_full_profile_is_one_env_var_away(self) -> None:  # noqa: D102
        self.skipTest("covered by TestMc2SurfaceOverStdio")

    def test_readonly_posture_over_stdio(self) -> None:  # noqa: D102
        self.skipTest("covered by TestMc2SurfaceOverStdio")

    def test_engine_without_pages_lists_no_page_tools_and_flags_errors(self) -> None:
        from june_mcp.capabilities import NEEDS_PAGES

        async def scenario() -> None:
            async with stdio_client(self._params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    names = [t.name for t in tools.tools]
                    self.assertEqual(names[0], "june_answer")
                    self.assertFalse(set(names) & NEEDS_PAGES, "page-backed tools listed on a no-pages engine")
                    self.assertEqual(len(names), 12)   # compact on a no-pages engine (20 − 8)
                    # annotations + title on every tool (§8.4)
                    by = {t.name: t for t in tools.tools}
                    self.assertTrue(by["june_answer"].annotations.readOnlyHint)
                    self.assertFalse(by["june_answer"].annotations.destructiveHint)
                    # the erase FAMILY carries the destructive hint — one effect class per family
                    # (R1) is what makes a family's annotations honest
                    self.assertTrue(by["june_canvas_erase"].annotations.destructiveHint)
                    self.assertFalse(by["june_remember"].annotations.readOnlyHint)
                    self.assertFalse(by["june_remember"].annotations.destructiveHint)
                    self.assertEqual(by["june_answer"].title, "Answer from the graph")
                    # N2: an engine failure is a tool-execution error
                    res = await session.call_tool("june_search", {"query": "x"})
                    self.assertTrue(res.isError)
                    self.assertIn("HTTP 500", res.content[0].text)
                    # a hidden tool addressed by name is refused as an error too
                    res = await session.call_tool("june_page_list", {})
                    self.assertTrue(res.isError)
                    # a schema violation is still caught by the SDK — the enum survives the fold
                    res = await session.call_tool("june_graph",
                                                  {"op": "neighborhood",
                                                   "node_id": "n", "node_type": "entity",
                                                   "direction": "outgoing"})
                    self.assertTrue(res.isError)
                    self.assertIn("validation", res.content[0].text.lower())

        anyio.run(scenario)
