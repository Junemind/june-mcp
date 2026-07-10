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
        self._send(200, {"ok": True} if self.path == "/healthz" else {"detail": "nf"})

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
        return StdioServerParameters(command=sys.executable,
                                     args=["-m", "june_mcp"], env=env)

    def test_answer_and_remember_over_stdio(self) -> None:
        async def scenario() -> None:
            async with stdio_client(self._params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    tools = await session.list_tools()
                    names = [t.name for t in tools.tools]
                    self.assertEqual(len(names), 10)  # 11 with JUNE_FILES_ROOT
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

    def test_readonly_posture_over_stdio(self) -> None:
        async def scenario() -> None:
            async with stdio_client(self._params(JUNE_READONLY="1")) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    tools = await session.list_tools()
                    names = {t.name for t in tools.tools}
                    self.assertEqual(names, {"june_answer", "june_search",
                                             "june_enumerate", "june_context",
                                             "june_neighborhood",
                                             "june_subgraph"})   # writes hidden

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
