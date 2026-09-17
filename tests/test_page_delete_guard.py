"""june_page_delete deletes only a page this connection has read (june_page_get) or created.

The description always said READ IT FIRST; from 0.4.2 it is enforced. The desktop Codex run of
2026-09-17 showed both failure shapes: june_page_delete called where june_page_read(op='get') was
meant (a page soft-deleted unseen), and the delete tool probed with made-up ids to see what it did.
Neither may reach the engine.
"""
from __future__ import annotations

import json
import unittest

import httpx

from june_client import JuneClient
from june_mcp.runtime import ToolInputError
from june_mcp.tools import _KNOWN_PAGES, run_tool

_A = "11111111-1111-1111-1111-111111111111"
_P = "33333333-3333-3333-3333-333333333333"
_Q = "44444444-4444-4444-4444-444444444444"


def _client(seen: list) -> JuneClient:
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path))
        if req.url.path == "/v1/canvases" and req.method == "GET":
            return httpx.Response(200, json=[{"canvas_id": _A, "name": "work", "created_at": "2026-08-01"}])
        if req.url.path == f"/v1/pages/{_P}" and req.method == "GET":
            return httpx.Response(200, json={"page_id": _P, "title": "T", "revision": 1,
                                             "updated_at": "2026-09-01T00:00:00Z", "blocks": []})
        if req.url.path.startswith("/v1/pages/") and req.method == "DELETE":
            return httpx.Response(200, json={"ok": True, "page_id": req.url.path.split("/")[3], "blocks_deleted": 0})
        if req.url.path == "/v1/pages" and req.method == "POST":
            return httpx.Response(200, json={"page_id": _Q, "title": json.loads(req.content)["title"], "revision": 1})
        if req.url.path == f"/v1/pages/{_Q}" and req.method == "PUT":
            return httpx.Response(200, json={"page_id": _Q, "revision": 2, "blocks": []})
        return httpx.Response(404, json={"detail": "nf"})
    http = httpx.Client(base_url="http://june.test", transport=httpx.MockTransport(handler))
    return JuneClient("http://june.test", "june_sk_test", client=http, canvas=_A)


class TestReadBeforeDelete(unittest.TestCase):
    def setUp(self) -> None:
        _KNOWN_PAGES.clear()
        self.seen: list = []
        self.client = _client(self.seen)

    def test_a_page_never_read_here_is_refused_before_any_request(self) -> None:
        for pid in (_P, "__invalid_probe__", "123", "noop"):
            with self.assertRaises(ToolInputError) as cm:
                run_tool("june_page_delete", self.client, {"page_id": pid})
            self.assertIn("has not read page", str(cm.exception))
            self.assertIn("Nothing was deleted", str(cm.exception))
        self.assertFalse([s for s in self.seen if s[0] == "DELETE"])

    def test_read_then_delete_goes_through(self) -> None:
        run_tool("june_page_get", self.client, {"page_id": _P})
        res = run_tool("june_page_delete", self.client, {"page_id": _P})
        self.assertTrue(res["ok"])
        self.assertIn(("DELETE", f"/v1/pages/{_P}"), self.seen)

    def test_a_page_this_connection_created_may_be_deleted(self) -> None:
        made = run_tool("june_page_create", self.client, {"title": "scratch", "blocks": []})
        self.assertEqual(made["page_id"], _Q)
        run_tool("june_page_delete", self.client, {"page_id": _Q})
        self.assertIn(("DELETE", f"/v1/pages/{_Q}"), self.seen)

    def test_reading_one_page_does_not_unlock_another(self) -> None:
        run_tool("june_page_get", self.client, {"page_id": _P})
        with self.assertRaises(ToolInputError):
            run_tool("june_page_delete", self.client, {"page_id": _Q})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
