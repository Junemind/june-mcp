"""S8 — the connector's side of the missing page verbs and safer writes (fix plan 48145480).

The WIRE, against a fake engine: what each verb sends, when it refuses before sending, and what
the receipt says. The behaviour of the engine itself is proven end to end in june_ai
``tests/test_s8_connector_e2e.py``.
"""
from __future__ import annotations

import json

import pytest

httpx = pytest.importorskip("httpx")

from june_client import JuneClient  # noqa: E402
from june_mcp import tools as T  # noqa: E402
from june_mcp.runtime import ToolInputError  # noqa: E402

S8 = ["meta", "view", "attrs", "sentinel-merge", "vocab", "positions"]


class Engine:
    def __init__(self, features=S8, status: dict | None = None):
        self.features, self.status = features, status or {}
        self.sent: list[tuple[str, str, dict | None]] = []

    def __call__(self, req):
        body = json.loads(req.content) if req.content else None
        path = req.url.path
        self.sent.append((req.method, path, body))
        if path == "/v1/pages/health":
            return httpx.Response(200, json={"enabled": True, "features": self.features})
        for suffix, code in self.status.items():
            if path.endswith(suffix):
                return httpx.Response(code, json={"detail": "x revision moved"})
        if path.endswith("/view"):
            return httpx.Response(200, json={"page_id": "p1", "title": "t", "revision": 7,
                                             "updated_at": "u1", "blocks": [], "attrs": {}})
        if path.endswith("/blocks"):
            return httpx.Response(200, json={"page_id": "p1", "blocks": body["blocks"],
                                             "attrs": {"content_blocks": len(body["blocks"])}})
        if path.endswith("blocks:insert"):
            return httpx.Response(200, json={"page_id": "p1", "inserted": [
                {"block_id": f"n{i}", "block_type": "paragraph", "text": "", "order": 1.5}
                for i, _ in enumerate(body["blocks"])], "blocks_total": 9, "revision": 8,
                "attrs": {"content_blocks": 5}, "coerced": []})
        if path.endswith("blocks:append"):
            return httpx.Response(200, json={"page_id": "p1", "appended": [], "blocks_total": 3,
                                             "revision": 8, "attrs": {"content_blocks": 3}})
        if path.endswith("blocks:move"):
            return httpx.Response(200, json={"page_id": "p1", "moved": [{"block_id": i} for i in body["ids"]],
                                             "blocks_total": 4, "revision": 8})
        if path.endswith("/meta"):
            return httpx.Response(200, json={"page_id": "p1", "title": "t", "pinned": bool(body.get("pinned")),
                                             "group": body.get("group")})
        if path.endswith("/restore"):
            return httpx.Response(200, json={"page_id": "p1", "restored": 2, "blocks": [{}, {}, {}],
                                             "skipped_sentinels": 1})
        if path.endswith("/removed"):
            return httpx.Response(200, json={"page_id": "p1", "count": 0, "blocks": []})
        if path == "/v1/pages/p1" and req.method == "PUT":
            return httpx.Response(200, json={"page_id": "p1", "title": body["title"]})
        return httpx.Response(404, json={"detail": f"unhandled {path}"})

    def last(self, suffix: str) -> dict:
        return next(b for m, p, b in reversed(self.sent) if p.endswith(suffix))


def _c(e):
    return JuneClient("http://june.test", "k", canvas="c0",
                      client=httpx.Client(base_url="http://june.test", transport=httpx.MockTransport(e)))


class TestInsert:
    def test_the_anchor_is_required_and_top_is_spelled_many_ways(self):
        e = Engine()
        with pytest.raises(ToolInputError, match="needs 'after'"):
            T._page_insert(_c(e), {"page_id": "p1", "blocks": [{"text": "x"}]})
        for top in (None, "top", "TOP", "", "start"):
            T._page_insert(_c(e), {"page_id": "p1", "after": top, "blocks": [{"text": "x"}]})
            assert e.last("blocks:insert")["after"] is None
        out = T._page_insert(_c(e), {"page_id": "p1", "after": "b3", "blocks": [{"type": "h1", "text": "x"}]})
        body = e.last("blocks:insert")
        assert body["after"] == "b3" and body["blocks"][0]["block_type"] == "h1"          # raw to a vocab engine
        assert out["blocks_total"] == 5 and out["revision"] == 8                          # content, not sentinels

    def test_an_older_engine_refuses_before_sending(self):
        e = Engine(features=["meta", "view", "attrs"])
        out = T._page_insert(_c(e), {"page_id": "p1", "after": "b", "blocks": [{"text": "x"}]})
        assert out["refused"] == "engine_predates_positions"
        assert not [p for _, p, _ in e.sent if p.endswith("blocks:insert")]

    def test_a_404_names_the_anchor(self):
        e = Engine(status={"blocks:insert": 404})
        with pytest.raises(KeyError, match="`after` block"):
            T._page_insert(_c(e), {"page_id": "p1", "after": "zz", "blocks": [{"text": "x"}]})


class TestMove:
    def test_sends_ids_anchor_and_revision(self):
        e = Engine()
        out = T._page_move(_c(e), {"page_id": "p1", "block_ids": ["a", "b"], "after": "c",
                                   "expected_revision": 7})
        assert e.last("blocks:move") == {"ids": ["a", "b"], "after": "c", "expected_revision": 7}
        assert out["blocks_moved"] == 2

    @pytest.mark.parametrize("code,needle", [(404, "not a content block"), (409, "named twice"),
                                             (422, "one of the blocks being moved")])
    def test_refusals_say_why(self, code, needle):
        with pytest.raises(KeyError, match=needle):
            T._page_move(_c(Engine(status={"blocks:move": code})), {"page_id": "p1", "block_ids": ["a"], "after": None})

    def test_a_stale_revision_is_a_refusal_not_an_error(self):
        out = T._page_move(_c(Engine(status={"blocks:move": 409})),
                           {"page_id": "p1", "block_ids": ["a"], "after": None, "expected_revision": 1})
        assert out["refused"] == "page_changed_since_read"

    def test_ids_are_required(self):
        with pytest.raises(ToolInputError, match="block_ids"):
            T._page_move(_c(Engine()), {"page_id": "p1", "after": None})


class TestSmallVerbs:
    def test_rename(self):
        e = Engine()
        assert T._page_rename(_c(e), {"page_id": "p1", "title": " New "})["title"] == "New"
        with pytest.raises(ToolInputError):
            T._page_rename(_c(e), {"page_id": "p1", "title": "  "})

    def test_meta_sends_only_what_was_given(self):
        e = Engine()
        T._page_meta(_c(e), {"page_id": "p1", "pinned": True})
        assert e.last("/meta") == {"pinned": True}
        T._page_meta(_c(e), {"page_id": "p1", "group": ""})
        assert e.last("/meta") == {"group": None}
        with pytest.raises(ToolInputError, match="pinned"):
            T._page_meta(_c(e), {"page_id": "p1"})

    def test_restore_all_or_named(self):
        e = Engine()
        out = T._page_restore(_c(e), {"page_id": "p1"})
        assert e.last("/restore") == {} and out == {"page_id": "p1", "restored": 2, "blocks_total": 3,
                                                   "skipped_style_blocks": 1}
        T._page_restore(_c(e), {"page_id": "p1", "block_ids": ["x"]})
        assert e.last("/restore") == {"block_ids": ["x"]}

    def test_removed_counts_as_a_read_of_the_page(self):
        T._KNOWN_PAGES.discard("p1")
        T._page_removed(_c(Engine()), {"page_id": "p1"})
        assert "p1" in T._KNOWN_PAGES


class TestSaferWrites:
    def test_write_guards_with_the_revision_it_read(self):
        e = Engine()
        T._page_write(_c(e), {"page_id": "p1", "blocks": [{"text": "a"}]})
        assert e.last("/blocks")["expected_revision"] == 7
        T._page_write(_c(e), {"page_id": "p1", "blocks": [{"text": "a"}], "expected_revision": 3})
        assert e.last("/blocks")["expected_revision"] == 3

    def test_orders_in_sequence_are_not_noise_but_a_reorder_is_named(self):
        assert T._order_notes([{"order": 1.0}, {"order": 2.5}, {"text": "x"}, {"order": 9}]) == []
        got = T._order_notes([{"order": 2}, {"order": 1}])
        assert [c["field"] for c in got] == ["order", "order"] and "june_page_insert" in got[0]["reason"]

    def test_title_on_a_content_write_is_named(self):
        e = Engine()
        out = T._page_append(_c(e), {"page_id": "p1", "title": "X", "blocks": [{"text": "a"}]})
        assert "june_page_rename" in out["_notes"]["title_ignored"]

    def test_the_receipt_points_at_the_undo_verbs(self):
        class Lost(Engine):
            def __call__(self, req):
                if req.url.path.endswith("/view"):
                    return httpx.Response(200, json={"page_id": "p1", "revision": 7, "updated_at": "u",
                                                     "blocks": [{"block_id": f"b{i}", "text": f"t{i}"} for i in range(3)],
                                                     "attrs": {}})
                return super().__call__(req)
        out = T._page_write(_c(Lost()), {"page_id": "p1", "blocks": [{"text": "new"}]})
        assert "june_page_restore" in out["recover"] and "POST" not in out["recover"]
