"""CX9 (connector half) — name→id canvas resolution is CACHED, without ever
becoming a correctness input (written FIRST, per plan v2).

Pre-CX9, every per-call ``canvas=<name|id>`` resolution issued a live
``GET /v1/canvases`` before the actual request — two wire calls per tool call,
against an engine that CX8 just taught us hosts will hit concurrently. The
cache closes that, under three fail-closed rules pinned here:

* POSITIVE, UNAMBIGUOUS resolutions only — a miss or an ambiguity is never
  cached, so a just-created canvas is findable immediately and a name clash
  keeps refusing until it is resolved for real;
* bounded lifetime (TTL) + explicit invalidation — a successful
  ``june_canvas_delete`` drops every entry for that canvas, and a canvas-scoped
  404 (deleted elsewhere: another session, the app) drops the entries for the
  canvas the call addressed before the error propagates;
* the cache never decides authorization or existence — the server still
  enforces both on the call itself (the CX4 handle posture, extended).

httpx.MockTransport throughout (no server, no network).
"""
from __future__ import annotations

import unittest

try:
    import httpx

    from june_client import JuneClient
    from june_mcp import tools as tools_mod
    from june_mcp.tools import run_tool
    _IMPORT_OK, _IMPORT_ERR = True, ""
except Exception as exc:  # pragma: no cover
    _IMPORT_OK, _IMPORT_ERR = False, repr(exc)

_A = "11111111-1111-1111-1111-111111111111"
_B = "22222222-2222-2222-2222-222222222222"
_DEFAULT = "99999999-9999-9999-9999-999999999999"


def _service(seen: dict, state: dict):
    """Mutable stub: ``state['rows']`` is the live canvas list; ``state['gone']``
    is a set of canvas ids that 404 on canvas-scoped calls (deleted elsewhere)."""

    def handler(req: httpx.Request) -> httpx.Response:
        seen.setdefault("calls", []).append(
            (req.method, req.url.path, req.headers.get("X-Canvas")))
        if req.url.path == "/v1/canvases" and req.method == "GET":
            return httpx.Response(200, json=state["rows"])
        if req.url.path.startswith("/v1/canvases/") and req.method == "DELETE":
            cid = req.url.path.rsplit("/", 1)[1]
            state["rows"] = [r for r in state["rows"] if r["canvas_id"] != cid]
            return httpx.Response(200, json={"canvas_id": cid, "nodes_deleted": 0,
                                             "edges_deleted": 0, "deleted": True})
        if req.headers.get("X-Canvas") in state.get("gone", set()):
            return httpx.Response(404, json={"detail": "unknown canvas"})
        if req.url.path == "/v1/search":
            return httpx.Response(200, json={"items": []})
        if req.url.path == "/v1/ingest/text":
            return httpx.Response(200, json={"nodes": 1, "edges": 0})
        return httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(handler)


def _lists(seen: dict) -> int:
    return sum(1 for (m, p, _c) in seen.get("calls", [])
               if m == "GET" and p == "/v1/canvases")


@unittest.skipUnless(_IMPORT_OK, f"import failed: {_IMPORT_ERR}")
class CacheCase(unittest.TestCase):
    def setUp(self) -> None:
        tools_mod._cache_reset()
        self.seen: dict = {}
        self.state = {"rows": [
            {"canvas_id": _DEFAULT, "name": "default", "created_at": "2026-08-01"},
            {"canvas_id": _A, "name": "alpha", "created_at": "2026-08-02"},
            {"canvas_id": _B, "name": "beta", "created_at": "2026-08-03"},
        ], "gone": set()}
        self.client = JuneClient(
            api_key="k", canvas=_DEFAULT,
            client=httpx.Client(transport=_service(self.seen, self.state),
                                base_url="http://june"))

    def tearDown(self) -> None:
        tools_mod._cache_reset()
        self.client.close()


class TestResolutionIsCached(CacheCase):
    def test_repeat_name_resolution_hits_the_wire_once(self) -> None:
        for i in range(3):
            run_tool("june_search", self.client, {"query": f"q{i}", "canvas": "alpha"})
        self.assertEqual(_lists(self.seen), 1,
                         "three calls addressing the same canvas NAME should resolve "
                         "over the wire once — the repeat lookups are the CX9 waste")
        # ...and the calls themselves all landed on alpha.
        searches = [c for (_m, p, c) in self.seen["calls"] if p == "/v1/search"]
        self.assertEqual(searches, [_A, _A, _A])

    def test_id_resolution_is_cached_too(self) -> None:
        for i in range(2):
            run_tool("june_search", self.client, {"query": f"q{i}", "canvas": _A})
        self.assertEqual(_lists(self.seen), 1)

    def test_ttl_expiry_forces_a_live_lookup(self) -> None:
        run_tool("june_search", self.client, {"query": "q", "canvas": "alpha"})
        self.assertEqual(_lists(self.seen), 1)
        # Age every entry past the TTL (the test seam is the module clock).
        real_now = tools_mod._cache_now
        tools_mod._cache_now = lambda: real_now() + tools_mod._RESOLVE_TTL + 1
        try:
            run_tool("june_search", self.client, {"query": "q2", "canvas": "alpha"})
        finally:
            tools_mod._cache_now = real_now
        self.assertEqual(_lists(self.seen), 2,
                         "an expired entry must re-resolve live, not serve forever")


class TestCacheNeverLies(CacheCase):
    def test_miss_is_never_cached(self) -> None:
        with self.assertRaises(KeyError):
            run_tool("june_search", self.client, {"query": "q", "canvas": "new-one"})
        # The canvas appears (created in the app / another session)…
        cid = "33333333-3333-3333-3333-333333333333"
        self.state["rows"].append(
            {"canvas_id": cid, "name": "new-one", "created_at": "2026-08-20"})
        out = run_tool("june_search", self.client, {"query": "q", "canvas": "new-one"})
        self.assertEqual(out["canvas"], cid,
                         "a cached MISS would make a just-created canvas unreachable")

    def test_ambiguity_is_never_cached(self) -> None:
        self.state["rows"].append(
            {"canvas_id": "55555555-5555-5555-5555-555555555555", "name": "alpha",
             "created_at": "2026-08-20"})
        with self.assertRaises(KeyError):
            run_tool("june_search", self.client, {"query": "q", "canvas": "alpha"})
        # The clash is resolved (duplicate removed) — the next call must succeed.
        self.state["rows"] = [r for r in self.state["rows"]
                              if r["canvas_id"] != "55555555-5555-5555-5555-555555555555"]
        out = run_tool("june_search", self.client, {"query": "q", "canvas": "alpha"})
        self.assertEqual(out["canvas"], _A)

    def test_delete_invalidates_the_deleted_canvas(self) -> None:
        run_tool("june_search", self.client, {"query": "q", "canvas": "beta"})   # cached
        pending = run_tool("june_canvas_delete", self.client, {"canvas": "beta"})
        run_tool("june_canvas_delete", self.client,
                 {"canvas": "beta", "confirm": pending["confirm_token"]})
        with self.assertRaises(KeyError, msg="a deleted canvas must not keep "
                                             "resolving from the cache"):
            run_tool("june_search", self.client, {"query": "q", "canvas": "beta"})

    def test_canvas_scoped_404_drops_the_cache(self) -> None:
        run_tool("june_search", self.client, {"query": "q", "canvas": "beta"})   # cached
        # Deleted ELSEWHERE (the app, another session): engine now 404s canvas B,
        # and the live list no longer carries it.
        self.state["gone"].add(_B)
        self.state["rows"] = [r for r in self.state["rows"] if r["canvas_id"] != _B]
        lists_before = _lists(self.seen)
        with self.assertRaises(httpx.HTTPStatusError):
            run_tool("june_search", self.client, {"query": "q", "canvas": "beta"})
        # The 404 must have evicted the entry: the next resolution goes LIVE and
        # refuses with the fail-closed unknown-canvas error, not a cached id.
        with self.assertRaises(KeyError):
            run_tool("june_search", self.client, {"query": "q", "canvas": "beta"})
        self.assertGreater(_lists(self.seen), lists_before,
                           "after a canvas-scoped 404 the next resolve must be live")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
