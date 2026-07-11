"""MC1/MC3 protocol test — REALLY spawn `python -m june_mcp` and speak MCP over stdio.

This is the test no unit test can substitute: it proves an agent host can launch
the server (C10), the transport stays uncorrupted end-to-end (C1), tools list and
execute through the real JSON-RPC loop, and a failing tool surfaces a redacted
error WITHOUT killing the process (C6). June's service side is a stub HTTP server
(stdlib http.server) so the test needs no engine, no DB, and no network beyond
localhost — it isolates exactly one seam: the MCP transport.

Dev-machine tier (needs the `mcp` extra + a bindable localhost port); skips
cleanly where those aren't available, same posture as Gate-3 (CLAUDE.md B5).
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

SECRET_KEY = "june_sk_stdio_test_SECRET"
TRIAL_CANVAS_ID = "3d1c8a52-6f4e-4b7a-8c9d-2e5f7a1b3c4d"


class _StubJune(BaseHTTPRequestHandler):
    """Minimal June service double: /healthz, /v1/canvases (MC-N1 name
    resolution), /v1/search (fenced), 500 on /v1/graph."""

    calls: list[tuple[str, str, str]] = []      # (path, api_key, canvas)

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path == "/healthz":
            self._send(200, {"ok": True})
        elif self.path == "/v1/canvases":
            # Real CanvasOut rows (stubs are written from the route models —
            # MC3 lesson). Lets the spawned server resolve the NAME in
            # JUNE_CANVAS to this id at startup (MC-N1).
            self._send(200, [{"canvas_id": TRIAL_CANVAS_ID, "name": "mcp-trial",
                              "created_at": "2026-07-08T00:00:00Z"}])
        else:
            self._send(404, {"detail": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        type(self).calls.append((self.path, self.headers.get("X-API-Key", ""),
                                 self.headers.get("X-Canvas", "")))
        if self.path == "/v1/search":
            if self.headers.get("X-API-Key") != SECRET_KEY:
                self._send(401, {"detail": "bad key"})
                return
            self._send(200, {"items": [{"label": "Meridian Systems", "score": 0.9}],
                             "degraded_lanes": []})
        elif self.path == "/v1/graph":
            self._send(500, {"detail": f"internal (key={SECRET_KEY})"})  # planted leak
        else:
            self._send(404, {"detail": "not found"})

    def log_message(self, *a: object) -> None:  # keep test output clean
        pass


@unittest.skipUnless(_MCP_OK, f"mcp client SDK unavailable: {_MCP_ERR}")
class TestStdioSpawn(unittest.TestCase):
    """Spawn → initialize → list → call → error-path → clean shutdown, over real stdio."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = HTTPServer(("127.0.0.1", 0), _StubJune)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()

    def _params(self) -> "StdioServerParameters":
        env = {k: v for k, v in os.environ.items() if k.startswith(("PATH", "PYTHON", "LANG", "LC_"))}
        env.update({
            "JUNE_BASE_URL": f"http://127.0.0.1:{self.port}",
            "JUNE_API_KEY": SECRET_KEY,
            "JUNE_CANVAS": "mcp-trial",
        })
        return StdioServerParameters(command=sys.executable,
                                     args=["-m", "june_mcp"], env=env)

    def test_full_protocol_roundtrip(self) -> None:
        async def scenario() -> None:
            async with stdio_client(self._params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    tools = await session.list_tools()
                    names = {t.name for t in tools.tools}
                    self.assertIn("june_search", names)
                    self.assertGreaterEqual(len(names), 6)

                    # happy path: tool call → stub service → JSON back through stdio
                    result = await session.call_tool("june_search",
                                                     {"query": "meridian", "limit": 5})
                    payload = json.loads(result.content[0].text)
                    self.assertEqual(payload["items"][0]["label"], "Meridian Systems")

                    # tenancy headers actually crossed the wire (C8 evidence) —
                    # and the NAME in JUNE_CANVAS crossed as its RESOLVED id
                    # (MC-N1: the service fence stays strict-UUID; friendliness
                    # is the client's job).
                    path, key, canvas = _StubJune.calls[-1]
                    self.assertEqual((path, key, canvas),
                                     ("/v1/search", SECRET_KEY, TRIAL_CANVAS_ID))

                    # error path: 500 with a PLANTED secret in the service detail —
                    # the agent-visible text must carry the status, not the secret,
                    # and the server must survive to answer the next call (C6+C1).
                    err = await session.call_tool("june_graph_should_not_exist", {})
                    err_payload = json.loads(err.content[0].text)
                    self.assertIn("error", err_payload)
                    self.assertNotIn(SECRET_KEY, err.content[0].text)

                    again = await session.call_tool("june_search", {"query": "still alive"})
                    self.assertIn("items", json.loads(again.content[0].text))

        anyio.run(scenario)

    def test_manifest_mode_is_valid_json(self) -> None:
        import subprocess
        proc = subprocess.run([sys.executable, "-m", "june_mcp", "--manifest"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        manifest = json.loads(proc.stdout)
        self.assertGreaterEqual(len(manifest), 6)
        for tool in manifest:
            self.assertIn("input_schema", tool)

    def test_missing_config_fails_closed_with_exit_2(self) -> None:
        import subprocess
        env = {k: v for k, v in os.environ.items() if not k.startswith("JUNE_")}
        proc = subprocess.run([sys.executable, "-m", "june_mcp"],
                              capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("JUNE_CANVAS", proc.stderr)
        self.assertEqual(proc.stdout, "", "fail-closed exit must not touch stdout")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
