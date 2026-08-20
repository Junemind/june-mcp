"""CX8 — one stdio stream, many in-flight tool calls (written FIRST, per plan v2).

A5 measured (2026-08-20, sandbox harness): the MCP stream carries pipelined
requests — the official client SDK multiplexes by request id, and Claude hosts
issue parallel tool calls — but the pre-CX8 server ran them strictly one at a
time (3×0.8s pipelined calls → 2.43s wall, engine max in-flight = 1), because
the sync ``run_tool`` executed ON the event loop and blocked even the transport
read. These tests pin the CX8 shape:

* tool execution is offloaded, so pipelined calls OVERLAP;
* the offload is BOUNDED by an explicit capacity limit (env-tunable), so a
  burst can never stampede the engine or exhaust the httpx pool;
* a slow tool never freezes the protocol — list_tools answers while a slow
  call is in flight (the property that keeps hosts from declaring us dead).

Same stub-June isolation as test_mc3_stdio, but THREADED (ThreadingHTTPServer):
a single-threaded stub would itself serialize and mask the thing under test.
Dev-machine tier; skips cleanly without the `mcp` extra.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import anyio
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    _MCP_OK, _MCP_ERR = True, ""
except Exception as exc:  # pragma: no cover
    _MCP_OK, _MCP_ERR = False, repr(exc)

KEY = "june_sk_cx8"
CANVAS_ID = "5a6b7c8d-9e0f-4a1b-8c2d-3e4f5a6b7c8d"
DELAY = 0.8


def _pin_spawn_to_imported_june_mcp(env: dict) -> None:
    """The spawned ``-m june_mcp`` must resolve the SAME package this test process
    imports — without the pin, an installed june-mcp shadows the repo copy in the
    child and the assertions run against the wrong artifact (the two-packages-
    one-import-name class, found live 2026-08-19 and again writing THIS test:
    the child served pre-CX8 code and 'proved' the offload absent)."""
    import june_mcp
    parent = os.path.dirname(os.path.dirname(os.path.abspath(june_mcp.__file__)))
    prior = env.get("PYTHONPATH")
    env["PYTHONPATH"] = parent if not prior else parent + os.pathsep + prior


class _Meter:
    """Thread-safe in-flight meter for the stub engine."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.inflight = 0
        self.max_inflight = 0
        self.calls = 0

    def enter(self) -> None:
        with self.lock:
            self.inflight += 1
            self.calls += 1
            self.max_inflight = max(self.max_inflight, self.inflight)

    def leave(self) -> None:
        with self.lock:
            self.inflight -= 1

    def reset(self) -> None:
        with self.lock:
            self.inflight = self.max_inflight = self.calls = 0


METER = _Meter()


class _SlowStub(BaseHTTPRequestHandler):
    """/v1/search sleeps DELAY and meters overlap; everything else is instant."""

    def _send(self, code: int, payload) -> None:
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
            self._send(200, [{"canvas_id": CANVAS_ID, "name": "mcp-trial",
                              "created_at": "2026-07-08T00:00:00Z"}])
        else:
            self._send(404, {"detail": "nf"})

    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        if self.path != "/v1/search":
            self._send(404, {"detail": "nf"})
            return
        METER.enter()
        try:
            time.sleep(DELAY)
        finally:
            METER.leave()
        self._send(200, {"items": [], "degraded_lanes": []})

    def log_message(self, *a: object) -> None:
        pass


@unittest.skipUnless(_MCP_OK, f"mcp client SDK unavailable: {_MCP_ERR}")
class TestOneStreamManyCalls(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _SlowStub)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()

    def setUp(self) -> None:
        METER.reset()

    def _params(self, **extra_env: str) -> "StdioServerParameters":
        env = {k: v for k, v in os.environ.items()
               if k.startswith(("PATH", "PYTHON", "LANG", "LC_"))}
        env.update({"JUNE_BASE_URL": f"http://127.0.0.1:{self.port}",
                    "JUNE_API_KEY": KEY, "JUNE_CANVAS": "mcp-trial", **extra_env})
        _pin_spawn_to_imported_june_mcp(env)
        return StdioServerParameters(command=sys.executable,
                                     args=["-m", "june_mcp"], env=env)

    # ── the A5 regression bar ────────────────────────────────────────────────
    def test_pipelined_slow_calls_overlap(self) -> None:
        """3 pipelined slow calls must run CONCURRENTLY: wall well under 3×DELAY
        and the engine must actually see >1 in flight. Pre-CX8 this fails with
        wall ≈ 3×DELAY and max_inflight = 1 (the measured A5 state)."""
        async def scenario() -> float:
            async with stdio_client(self._params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    t0 = time.monotonic()

                    async def one(i: int) -> None:
                        r = await session.call_tool("june_search", {"query": f"cx8-{i}"})
                        assert not r.isError, r.content

                    async with anyio.create_task_group() as tg:
                        for i in range(3):
                            tg.start_soon(one, i)
                    return time.monotonic() - t0

        wall = anyio.run(scenario)
        self.assertLess(wall, 2 * DELAY,
                        f"3 pipelined {DELAY}s calls took {wall:.2f}s — the server is "
                        "serializing tool execution on the event loop")
        self.assertGreaterEqual(METER.max_inflight, 2,
                                "the engine never saw overlapping requests — offload "
                                "is not happening")

    def test_concurrency_is_bounded_by_the_limiter(self) -> None:
        """The offload must be BOUNDED: with JUNE_TOOL_CONCURRENCY=2, six pipelined
        calls may never put more than 2 requests in flight at the engine — a burst
        gets backpressure, not a stampede. (All six still complete.)"""
        async def scenario() -> None:
            async with stdio_client(self._params(JUNE_TOOL_CONCURRENCY="2")) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def one(i: int) -> None:
                        r = await session.call_tool("june_search", {"query": f"cx8b-{i}"})
                        assert not r.isError, r.content

                    async with anyio.create_task_group() as tg:
                        for i in range(6):
                            tg.start_soon(one, i)

        anyio.run(scenario)
        self.assertEqual(METER.calls, 6)
        self.assertLessEqual(METER.max_inflight, 2,
                             f"limiter breached: engine saw {METER.max_inflight} in flight "
                             "with JUNE_TOOL_CONCURRENCY=2")
        self.assertGreaterEqual(METER.max_inflight, 2,
                                "limit=2 should still allow 2 in flight — over-throttling")

    def test_slow_tool_never_freezes_the_protocol(self) -> None:
        """While a slow tool call is in flight, list_tools must still answer fast.
        Pre-CX8 the sync handler blocked the transport read itself, so EVERYTHING
        (including pings hosts use for liveness) stalled behind the slow call."""
        async def scenario() -> float:
            async with stdio_client(self._params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    got_list_after: list[float] = []

                    async def slow() -> None:
                        r = await session.call_tool("june_search", {"query": "cx8-slow"})
                        assert not r.isError, r.content

                    async def probe() -> None:
                        await anyio.sleep(DELAY / 4)     # slow call is now in flight
                        t0 = time.monotonic()
                        tools = await session.list_tools()
                        got_list_after.append(time.monotonic() - t0)
                        assert tools.tools

                    async with anyio.create_task_group() as tg:
                        tg.start_soon(slow)
                        tg.start_soon(probe)
                    return got_list_after[0]

        latency = anyio.run(scenario)
        self.assertLess(latency, DELAY / 2,
                        f"list_tools took {latency:.2f}s behind a {DELAY}s tool call — "
                        "the protocol is frozen while a tool runs")


class TestConcurrencyConfig(unittest.TestCase):
    """The knob is part of the fail-closed env contract (no mcp extra needed)."""

    BASE = {"JUNE_BASE_URL": "http://localhost:8000", "JUNE_API_KEY": "k",
            "JUNE_CANVAS": "work"}

    def test_default_is_bounded_and_positive(self) -> None:
        from june_mcp.runtime import load_config
        cfg = load_config(self.BASE)
        self.assertGreaterEqual(cfg.tool_concurrency, 1)
        self.assertLessEqual(cfg.tool_concurrency, 32,
                             "default must stay well inside the httpx pool (100) — "
                             "'unbounded by default' is the CX8 anti-goal")

    def test_explicit_value_is_honored(self) -> None:
        from june_mcp.runtime import load_config
        cfg = load_config({**self.BASE, "JUNE_TOOL_CONCURRENCY": "4"})
        self.assertEqual(cfg.tool_concurrency, 4)

    def test_invalid_values_refuse_loudly(self) -> None:
        from june_mcp.runtime import ConfigError, load_config
        for bad in ("0", "-3", "many", "2.5"):
            with self.assertRaises(ConfigError, msg=f"JUNE_TOOL_CONCURRENCY={bad!r}"):
                load_config({**self.BASE, "JUNE_TOOL_CONCURRENCY": bad})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
