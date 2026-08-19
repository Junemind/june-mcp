"""CX3–CX6 — per-call canvas addressing over an IMMUTABLE default.

The contract these pin (plan v2, D1/D2):

* CX3 — the connector holds no mutable canvas state: the startup default cannot
  move; ``june_canvas_use`` resolves and returns, it switches NOTHING; a
  per-call ``canvas`` applies to that call only. Interleaved and fanned-out
  callers can never redirect each other.
* CX4 — a ``canvas_handle`` embeds the process epoch: a restart's stale handle
  is REFUSED (naming expected vs actual), never silently redirected; malformed
  handles refuse rather than degrade to a name lookup; a valid handle resolves
  with ZERO extra network calls.
* CX5 — every canvas-scoped tool takes the optional ``canvas``; management
  tools do not; ``strict`` refuses canvas-less canvas-scoped calls and writes
  nothing; strict off is behaviour-identical to before.
* CX6 — every canvas-scoped result names the EFFECTIVE canvas it landed in.

httpx.MockTransport throughout (no server, no network) — same seam as
test_canvas_tools. A sequential test passes on the broken code and proves
nothing, so the isolation tests interleave callers in every meaningful order.
"""
from __future__ import annotations

import unittest

try:
    import httpx

    from june_client import JuneClient
    from june_mcp import tools as tools_mod
    from june_mcp.tools import run_tool, visible_tools
    _IMPORT_OK, _IMPORT_ERR = True, ""
except Exception as exc:  # pragma: no cover
    _IMPORT_OK, _IMPORT_ERR = False, repr(exc)

_A = "11111111-1111-1111-1111-111111111111"
_B = "22222222-2222-2222-2222-222222222222"
_C = "44444444-4444-4444-4444-444444444444"
_DEFAULT = "99999999-9999-9999-9999-999999999999"


def _service(seen: dict):
    rows = [
        {"canvas_id": _DEFAULT, "name": "default", "created_at": "2026-08-01"},
        {"canvas_id": _A, "name": "alpha", "created_at": "2026-08-02"},
        {"canvas_id": _B, "name": "beta", "created_at": "2026-08-03"},
        {"canvas_id": _C, "name": "gamma", "created_at": "2026-08-04"},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        seen.setdefault("calls", []).append(
            (req.method, req.url.path, req.headers.get("X-Canvas")))
        if req.url.path == "/v1/canvases" and req.method == "GET":
            return httpx.Response(200, json=rows)
        if req.url.path == "/v1/ingest/text":
            return httpx.Response(200, json={"nodes": 1, "edges": 0})
        if req.url.path == "/v1/search":
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(handler)


def _client(seen: dict, canvas: str = _DEFAULT) -> "JuneClient":
    return JuneClient(api_key="k", canvas=canvas,
                      client=httpx.Client(transport=_service(seen),
                                          base_url="http://june"))


def _writes(seen: dict) -> list:
    """(path, X-Canvas) of every request that was a graph WRITE."""
    return [(p, c) for (m, p, c) in seen.get("calls", []) if p == "/v1/ingest/text"]


@unittest.skipUnless(_IMPORT_OK, f"import failed: {_IMPORT_ERR}")
class TestImmutableDefault(unittest.TestCase):
    def test_per_call_override_wins_absent_inherits_default_unchanged(self) -> None:
        seen: dict = {}
        c = _client(seen)
        run_tool("june_remember", c, {"text": "x", "canvas": "alpha"})
        run_tool("june_remember", c, {"text": "y"})              # absent ⇒ default
        self.assertEqual(_writes(seen), [("/v1/ingest/text", _A),
                                         ("/v1/ingest/text", _DEFAULT)])
        self.assertEqual(c.canvas, _DEFAULT)     # the overridden call moved nothing

    def test_canvas_use_switches_nothing(self) -> None:
        seen: dict = {}
        c = _client(seen)
        out = run_tool("june_canvas_use", c, {"canvas": "beta"})
        self.assertEqual(out["canvas_id"], _B)
        self.assertIs(out["switched"], False)
        self.assertIn("canvas_handle", out)
        run_tool("june_remember", c, {"text": "after use"})      # still the default
        self.assertEqual(_writes(seen), [("/v1/ingest/text", _DEFAULT)])

    def test_interleaved_callers_never_redirect_each_other(self) -> None:
        # Two "conversations" on ONE process/client, interleaved in every
        # meaningful order — each write must land where ITS caller pointed.
        for order in (["A", "B"], ["B", "A"], ["A", "B", "A"], ["B", "A", "B"]):
            seen: dict = {}
            c = _client(seen)
            want = {"A": ("alpha", _A), "B": ("beta", _B)}
            for who in order:
                name, _ = want[who]
                run_tool("june_remember", c, {"text": who, "canvas": name})
            self.assertEqual([cid for _, cid in _writes(seen)],
                             [want[w][1] for w in order], order)

    def test_fan_out_one_caller_n_canvases(self) -> None:
        seen: dict = {}
        c = _client(seen)
        for name, cid in (("alpha", _A), ("beta", _B), ("gamma", _C),
                          ("alpha", _A), ("gamma", _C)):
            run_tool("june_remember", c, {"text": name, "canvas": name})
        self.assertEqual([cid for _, cid in _writes(seen)], [_A, _B, _C, _A, _C])


@unittest.skipUnless(_IMPORT_OK, f"import failed: {_IMPORT_ERR}")
class TestCanvasHandle(unittest.TestCase):
    def test_round_trip_and_zero_lookup(self) -> None:
        seen: dict = {}
        c = _client(seen)
        handle = run_tool("june_canvas_use", c, {"canvas": "beta"})["canvas_handle"]
        before = len([1 for (m, p, _) in seen["calls"] if p == "/v1/canvases"])
        out = run_tool("june_remember", c, {"text": "via handle", "canvas": handle})
        after = len([1 for (m, p, _) in seen["calls"] if p == "/v1/canvases"])
        self.assertEqual(after, before)          # CX4: handle resolves with zero traffic
        self.assertEqual(_writes(seen), [("/v1/ingest/text", _B)])
        self.assertEqual(out["canvas"], _B)

    def test_foreign_epoch_refuses_and_writes_nothing(self) -> None:
        seen: dict = {}
        c = _client(seen)
        stale = f"jch1.{'0' * 12}.{_B}"          # someone else's process epoch
        with self.assertRaises(KeyError) as ctx:
            run_tool("june_remember", c, {"text": "x", "canvas": stale})
        msg = str(ctx.exception)
        self.assertIn("0" * 12, msg)             # names the handle's epoch…
        self.assertIn(tools_mod._EPOCH, msg)     # …and the current one
        self.assertEqual(_writes(seen), [])      # nothing was written

    def test_malformed_handles_refuse_never_reinterpreted(self) -> None:
        seen: dict = {}
        c = _client(seen)
        for bad in ("jch1.only-two", f"jch1..{_B}", f"jch1.{tools_mod._EPOCH}.",
                    f"jch1.{tools_mod._EPOCH}.not-a-uuid"):
            with self.assertRaises(KeyError, msg=bad):
                run_tool("june_remember", c, {"text": "x", "canvas": bad})
        self.assertEqual(_writes(seen), [])


@unittest.skipUnless(_IMPORT_OK, f"import failed: {_IMPORT_ERR}")
class TestPerCallSurfaceAndStrict(unittest.TestCase):
    def test_every_canvas_scoped_tool_offers_the_argument(self) -> None:
        for t in visible_tools():
            props = t.input_schema.get("properties", {})
            if t.canvas_scoped:
                self.assertIn("canvas", props, t.name)
                self.assertIn("THIS call only", props["canvas"]["description"], t.name)
            elif t.name in ("june_canvas_list", "june_canvas_current",
                            "june_canvas_create"):
                self.assertNotIn("canvas", props, t.name)   # mgmt: no injected arg

    def test_unknown_name_fails_closed_before_any_write(self) -> None:
        seen: dict = {}
        c = _client(seen)
        with self.assertRaises(KeyError):
            run_tool("june_remember", c, {"text": "x", "canvas": "no-such"})
        self.assertEqual(_writes(seen), [])

    def test_strict_refuses_canvasless_calls_and_writes_nothing(self) -> None:
        seen: dict = {}
        c = _client(seen)
        with self.assertRaises(KeyError) as ctx:
            run_tool("june_remember", c, {"text": "x"}, strict=True)
        self.assertIn("JUNE_CANVAS_STRICT", str(ctx.exception))
        self.assertEqual(_writes(seen), [])
        # explicit canvas satisfies strict; management tools are exempt
        run_tool("june_remember", c, {"text": "x", "canvas": "alpha"}, strict=True)
        run_tool("june_canvas_current", c, {}, strict=True)
        self.assertEqual(_writes(seen), [("/v1/ingest/text", _A)])

    def test_strict_off_is_todays_behaviour(self) -> None:
        seen: dict = {}
        c = _client(seen)
        run_tool("june_remember", c, {"text": "x"})
        self.assertEqual(_writes(seen), [("/v1/ingest/text", _DEFAULT)])


@unittest.skipUnless(_IMPORT_OK, f"import failed: {_IMPORT_ERR}")
class TestResultsTellTheTruth(unittest.TestCase):
    def test_echo_matches_the_canvas_the_call_used(self) -> None:
        seen: dict = {}
        c = _client(seen)
        over = run_tool("june_remember", c, {"text": "x", "canvas": "beta"})
        self.assertEqual(over["canvas"], _B)
        self.assertEqual(over.get("canvas_name"), "beta")
        home = run_tool("june_remember", c, {"text": "y"})
        self.assertEqual(home["canvas"], _DEFAULT)

    def test_reads_carry_the_receipt_too(self) -> None:
        seen: dict = {}
        c = _client(seen)
        out = run_tool("june_search", c, {"query": "q", "canvas": "alpha"})
        self.assertEqual(out["canvas"], _A)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
