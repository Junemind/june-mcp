"""Canvas tools — runtime canvas switching + two-phase destructive ops (2026-08-14).

The capability these pin: an agent can see, switch, and create canvases WITHOUT a
connector restart (the active canvas is the client's per-request ``X-Canvas``
value), and destructive ops (clear/delete) physically cannot execute in one tool
call — the first call mints a single-use, expiring confirm token and executes
nothing; only the second call carrying it executes.

Invariants under test, most safety-critical first:
* one-call destruction is impossible; the pending response names the canvas and warns;
* tokens are single-use, expire, and are bound to (op, canvas) — a clear token
  cannot drive a delete, canvas A's token cannot wipe canvas B;
* deleting the ACTIVE canvas is refused outright;
* switching is fail-closed (unknown id / unknown or ambiguous name → error, active
  canvas unchanged);
* every non-canvas write result names the canvas it landed in (write provenance);
* the read-only posture hides create/clear/delete but keeps list/current/use.

httpx.MockTransport throughout (no server, no network) — same seam as test_mc2.
"""
from __future__ import annotations

import json
import unittest

try:
    import httpx

    from june_client import JuneClient
    from june_mcp.tools import (
        CONFIRM_TTL_SECONDS, PendingConfirms, run_tool, visible_tools,
    )
    _IMPORT_OK, _IMPORT_ERR = True, ""
except Exception as exc:  # pragma: no cover
    _IMPORT_OK, _IMPORT_ERR = False, repr(exc)

_A = "11111111-1111-1111-1111-111111111111"
_B = "22222222-2222-2222-2222-222222222222"


def _service(seen: dict, *, canvases: list[dict] | None = None):
    """A fake June service: canvas list/create/clear/delete + text ingest."""
    rows = canvases if canvases is not None else [
        {"canvas_id": _A, "name": "work", "created_at": "2026-08-01"},
        {"canvas_id": _B, "name": "home-lab", "created_at": "2026-08-02"},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        seen.setdefault("calls", []).append((req.method, req.url.path))
        seen["last_canvas_header"] = req.headers.get("X-Canvas")
        if req.url.path == "/v1/canvases" and req.method == "GET":
            return httpx.Response(200, json=rows)
        if req.url.path == "/v1/canvases" and req.method == "POST":
            body = json.loads(req.content)
            made = {"canvas_id": "33333333-3333-3333-3333-333333333333",
                    "name": body["name"], "created_at": "2026-08-14"}
            rows.append(made)
            return httpx.Response(200, json=made)
        if req.url.path.endswith("/clear") and req.method == "POST":
            cid = req.url.path.split("/")[3]
            return httpx.Response(200, json={"canvas_id": cid, "nodes_deleted": 7,
                                             "edges_deleted": 9})
        if req.url.path.startswith("/v1/canvases/") and req.method == "DELETE":
            cid = req.url.path.split("/")[3]
            return httpx.Response(200, json={"canvas_id": cid, "nodes_deleted": 7,
                                             "edges_deleted": 9, "deleted": True})
        if req.url.path == "/v1/ingest/text":
            return httpx.Response(200, json={"nodes": 1, "edges": 0, "engine": "floor"})
        return httpx.Response(404, json={"detail": "unknown route"})

    return handler


def _client(seen: dict, **kw) -> "JuneClient":
    transport = httpx.MockTransport(_service(seen, **kw))
    http = httpx.Client(base_url="http://june.test", transport=transport)
    return JuneClient("http://june.test", "june_sk_test", client=http, canvas=_A)


@unittest.skipUnless(_IMPORT_OK, f"june_mcp unavailable: {_IMPORT_ERR}")
class TestSwitching(unittest.TestCase):

    def test_list_marks_the_default(self) -> None:
        c = _client({})
        out = run_tool("june_canvas_list", c)
        self.assertEqual(out["default_canvas_id"], _A)
        self.assertEqual(out["active_canvas_id"], _A)   # deprecated alias, one release
        flags = {r["name"]: r["default"] for r in out["canvases"]}
        self.assertTrue(flags["work"])
        self.assertFalse(flags["home-lab"])

    def test_current_names_the_default_canvas(self) -> None:
        out = run_tool("june_canvas_current", _client({}))
        self.assertEqual(out["default_canvas_id"], _A)
        self.assertEqual(out["active_canvas_id"], _A)   # deprecated alias, one release
        self.assertEqual(out["name"], "work")
        self.assertIn("IMMUTABLE", out["note"])       # the CX3 contract is stated

    def test_use_resolves_and_moves_nothing(self) -> None:
        seen: dict = {}
        c = _client(seen)
        out = run_tool("june_canvas_use", c, {"canvas": "home-lab"})
        self.assertEqual(out["canvas_id"], _B)
        self.assertIs(out["switched"], False)          # CX3: it resolves, it moves nothing
        self.assertIn("canvas_handle", out)
        # a canvas-less write STILL rides the immutable default — no redirect.
        run_tool("june_remember", c, {"text": "note"})
        self.assertEqual(seen["last_canvas_header"], _A)
        # …and the per-call form is how you actually act in the resolved canvas.
        run_tool("june_remember", c, {"text": "note2", "canvas": out["canvas_handle"]})
        self.assertEqual(seen["last_canvas_header"], _B)

    def test_use_is_fail_closed(self) -> None:
        c = _client({})
        for bad in ("no-such-canvas", "99999999-9999-9999-9999-999999999999"):
            with self.assertRaises(KeyError):
                run_tool("june_canvas_use", c, {"canvas": bad})
            self.assertEqual(c.canvas, _A)             # default untouched on failure

    def test_use_refuses_ambiguous_names(self) -> None:
        rows = [{"canvas_id": _A, "name": "twin"}, {"canvas_id": _B, "name": "twin"}]
        c = _client({}, canvases=rows)
        with self.assertRaises(KeyError):
            run_tool("june_canvas_use", c, {"canvas": "twin"})

    def test_create_switches_nothing_and_refuses_duplicates(self) -> None:
        c = _client({})
        out = run_tool("june_canvas_create", c, {"name": "fresh"})
        self.assertTrue(out["created"])
        self.assertIs(out["switched"], False)          # CX3: creation moves nothing
        self.assertIn("canvas_handle", out)
        self.assertEqual(c.canvas, _A)                 # default untouched
        with self.assertRaises(KeyError):              # duplicate names breed ambiguity
            run_tool("june_canvas_create", c, {"name": "work"})


@unittest.skipUnless(_IMPORT_OK, f"june_mcp unavailable: {_IMPORT_ERR}")
class TestTwoPhaseDestruction(unittest.TestCase):

    def test_first_call_executes_nothing_and_warns(self) -> None:
        seen: dict = {}
        c = _client(seen)
        out = run_tool("june_canvas_clear", c, {"canvas": "home-lab"})
        self.assertTrue(out["pending"])
        self.assertIn("IRREVERSIBLY", out["warning"])
        self.assertEqual(out["name"], "home-lab")
        # No destructive route was touched — only the canvas list for resolution.
        self.assertTrue(all(p == "/v1/canvases" for _, p in seen["calls"]))

    def test_token_executes_exactly_once(self) -> None:
        seen: dict = {}
        c = _client(seen)
        pend = run_tool("june_canvas_clear", c, {"canvas": "home-lab"})
        done = run_tool("june_canvas_clear", c,
                        {"canvas": "home-lab", "confirm": pend["confirm_token"]})
        self.assertEqual(done["nodes_deleted"], 7)
        self.assertEqual(done["op"], "clear")
        with self.assertRaises(KeyError):              # single-use: replay is refused
            run_tool("june_canvas_clear", c,
                     {"canvas": "home-lab", "confirm": pend["confirm_token"]})

    def test_token_is_bound_to_op_and_canvas(self) -> None:
        c = _client({})
        pend = run_tool("june_canvas_clear", c, {"canvas": "home-lab"})
        with self.assertRaises(KeyError):              # clear token cannot drive delete
            run_tool("june_canvas_delete", c,
                     {"canvas": "home-lab", "confirm": pend["confirm_token"]})
        pend2 = run_tool("june_canvas_clear", c, {"canvas": "home-lab"})
        with self.assertRaises(KeyError):              # canvas B's token cannot wipe A… via name 'work'
            run_tool("june_canvas_clear", c,
                     {"canvas": "work", "confirm": pend2["confirm_token"]})

    def test_tokens_expire(self) -> None:
        t = [0.0]
        pc = PendingConfirms(clock=lambda: t[0])
        token = pc.mint("clear", _B)
        t[0] = CONFIRM_TTL_SECONDS + 1.0
        ok, reason = pc.consume(token, "clear", _B)
        self.assertFalse(ok)
        self.assertIn("expired", reason)

    def test_deleting_the_default_canvas_is_refused(self) -> None:
        c = _client({})
        with self.assertRaises(KeyError):
            run_tool("june_canvas_delete", c, {"canvas": "work"})   # work == the default
        # a NON-default canvas is deletable (two-phase pending as usual); the
        # default itself stays undeletable for the whole process life (CX3 —
        # a canvas-less call must always have somewhere real to land).
        pend = run_tool("june_canvas_delete", c, {"canvas": "home-lab"})
        self.assertTrue(pend["pending"])


@unittest.skipUnless(_IMPORT_OK, f"june_mcp unavailable: {_IMPORT_ERR}")
class TestProvenanceAndPosture(unittest.TestCase):

    def test_write_results_name_their_canvas(self) -> None:
        c = _client({})
        out = run_tool("june_remember", c, {"text": "a fact"})
        self.assertEqual(out["canvas"], _A)            # provenance rides every write
        out2 = run_tool("june_remember", c, {"text": "another", "canvas": "home-lab"})
        self.assertEqual(out2["canvas"], _B)           # …including per-call targets (CX6)

    def test_readonly_hides_destructive_keeps_switching(self) -> None:
        names = {t.name for t in visible_tools(readonly=True)}
        for kept in ("june_canvas_list", "june_canvas_current", "june_canvas_use"):
            self.assertIn(kept, names)
        for hidden in ("june_canvas_create", "june_canvas_clear", "june_canvas_delete"):
            self.assertNotIn(hidden, names)
        with self.assertRaises(KeyError):              # execution fence, not just visibility
            run_tool("june_canvas_clear", _client({}), {"canvas": "home-lab"}, readonly=True)


if __name__ == "__main__":
    unittest.main()
