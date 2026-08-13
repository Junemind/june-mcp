"""Page-compose tests — the rich page surface (views, media, canvas layout, append).

Same seam as test_mc2_tools: an httpx.MockTransport emulates the /v1/pages endpoints in memory,
so the REAL JuneClient methods (save_blocks/get_page/append_blocks) and the REAL tool handlers run
end-to-end with no server. Asserts the block TEXT that lands is exactly what the frontend
(view_query.ts / page_layout.ts) parses — the contract between the connector and the editor.
"""
from __future__ import annotations

import json
import re
import unittest
import uuid

try:
    import httpx

    from june_client import JuneClient
    from june_mcp.prompts import PROMPTS, SERVER_INSTRUCTIONS, render_prompt
    from june_mcp.tools import (
        _block_style, _layout_text, _media_text, _style_text, _styles_by_index,
        _to_blocks, _view_spec_text, run_tool,
    )
    _IMPORT_OK, _IMPORT_ERR = True, ""
except Exception as exc:  # pragma: no cover
    _IMPORT_OK, _IMPORT_ERR = False, repr(exc)


def _pages_handler(store: dict):
    """A tiny in-memory /v1/pages service: create, get, and AUTHORITATIVE block save with the
    server's id rules (a payload id that names an owned block updates it in place; anything else
    gets a fresh server id). Order is preserved verbatim, exactly like the real route."""
    def handler(req: "httpx.Request") -> "httpx.Response":
        path, method = req.url.path, req.method
        if method == "POST" and path == "/v1/pages":
            body = json.loads(req.content or b"{}")
            pid = str(uuid.uuid4())
            store[pid] = {"page_id": pid, "title": body.get("title") or "Untitled", "blocks": []}
            return httpx.Response(200, json={"page_id": pid, "title": store[pid]["title"]})
        m = re.match(r"^/v1/pages/([^/]+)/blocks$", path)
        if method == "POST" and m:
            page = store[m.group(1)]
            owned = {b["block_id"] for b in page["blocks"]}
            new = []
            for b in json.loads(req.content or b"{}").get("blocks", []):
                bid = b.get("id")
                bid = str(bid) if (bid and str(bid) in owned) else "srv-" + uuid.uuid4().hex[:10]
                new.append({"block_id": bid, "block_type": b.get("block_type", "text"),
                            "text": b.get("text", ""), "order": float(b.get("order", 0.0))})
            page["blocks"] = new
            return httpx.Response(200, json={"page_id": page["page_id"], "title": page["title"],
                                             "blocks": new})
        m = re.match(r"^/v1/pages/([^/]+)$", path)
        if method == "GET" and m:
            page = store[m.group(1)]
            return httpx.Response(200, json={"page_id": page["page_id"], "title": page["title"],
                                             "blocks": page["blocks"]})
        return httpx.Response(404, json={"detail": f"unhandled {method} {path}"})
    return handler


def _client(store: dict) -> "JuneClient":
    http = httpx.Client(base_url="http://june.test", transport=httpx.MockTransport(_pages_handler(store)))
    return JuneClient("http://june.test", "june_sk_test", client=http,
                      canvas="11111111-1111-1111-1111-111111111111")


@unittest.skipUnless(_IMPORT_OK, f"june_mcp unavailable: {_IMPORT_ERR}")
class TestPureSerializers(unittest.TestCase):
    """The block text a rich block becomes — must match the frontend parsers byte-for-byte."""

    def test_view_spec_is_sentinel_json_with_predicate(self) -> None:
        txt = _view_spec_text({"type": "view", "node_types": ["entity", "decision"],
                               "kind": "board", "cap": 50, "terms": ["atlas"]})
        o = json.loads(txt)
        self.assertEqual(o["__june_view__"], 1)
        self.assertEqual(o["node_types"], ["entity", "decision"])
        self.assertEqual(o["kind"], "board")
        self.assertEqual(o["cap"], 50)
        self.assertEqual(o["terms"], ["atlas"])

    def test_view_invalid_fields_fall_back(self) -> None:
        o = json.loads(_view_spec_text({"type": "view", "node_types": ["bogus"],
                                        "kind": "pie", "cap": 999999}))
        self.assertEqual(o["node_types"], ["entity"])   # a view needs >=1 predicate
        self.assertEqual(o["kind"], "table")            # unknown kind → table
        self.assertEqual(o["cap"], 1000)                # clamped to the frontend max

    def test_media_image_vs_link_vs_unsafe(self) -> None:
        self.assertEqual(_media_text({"type": "image", "url": "https://x/y.png", "alt": "chart"}),
                         "![chart](https://x/y.png)")
        # a non-image http url → link
        self.assertEqual(_media_text({"type": "embed", "url": "https://x/doc", "label": "Doc"}),
                         "[Doc](https://x/doc)")
        # data:image is treated as an image
        self.assertTrue(_media_text({"type": "image", "url": "data:image/png;base64,AAAA"}).startswith("!["))
        # a javascript: url is NEVER emitted as an active link — it degrades to inert text
        self.assertEqual(_media_text({"type": "embed", "url": "javascript:alert(1)", "label": "x"}), "x")
        # explicit ready Markdown text passes through untouched
        self.assertEqual(_media_text({"type": "embed", "text": "[a](https://a)"}), "[a](https://a)")

    def test_to_blocks_dispatches_types(self) -> None:
        blocks = _to_blocks([
            {"type": "heading_1", "text": "Title"},
            {"type": "view", "node_types": ["entity"], "kind": "table"},
            {"type": "image", "url": "https://x/y.jpg"},
            {"type": "bogus", "text": "coerced"},
        ])
        self.assertEqual([b["block_type"] for b in blocks],
                         ["heading_1", "paragraph", "embed", "paragraph"])
        self.assertIn("__june_view__", blocks[1]["text"])
        self.assertEqual([b["order"] for b in blocks], [1.0, 2.0, 3.0, 4.0])

    def test_layout_text_keys_on_real_ids(self) -> None:
        txt = _layout_text([{"block": 0, "x": 0, "y": 0, "title": "A"},
                            {"block": 1, "x": 320, "y": 0}],
                           {0: "id-a", 1: "id-b"})
        o = json.loads(txt)
        self.assertEqual(o["__june_layout__"], 1)
        self.assertEqual(o["mode"], "canvas")
        self.assertEqual(set(o["pos"]), {"id-a", "id-b"})
        self.assertEqual(o["titles"], {"id-a": "A"})

    def test_layout_none_when_no_card_resolves(self) -> None:
        self.assertIsNone(_layout_text([{"block": 9}], {0: "id-a"}))

    def test_block_style_keeps_only_valid_keys(self) -> None:
        # good values kept, unknown colour/variant/flag dropped (mirrors frontend _cleanBlockStyle)
        self.assertEqual(_block_style({"variant": "warning", "color": "amber", "icon": "🔥"}),
                         {"variant": "warning", "bg": "amber", "icon": "🔥"})
        self.assertEqual(_block_style({"priority": "high"}), {"flag": "high"})   # priority alias → flag
        self.assertEqual(_block_style({"variant": "bogus", "color": "chartreuse"}), {})   # all invalid → empty
        self.assertEqual(_styles_by_index([{"text": "x"}, {"variant": "danger"}]), {1: {"variant": "danger"}})

    def test_style_text_keys_on_real_ids_and_page_accent(self) -> None:
        txt = _style_text({0: {"variant": "success"}, 1: {"flag": "high"}},
                          {0: "id-a", 1: "id-b"}, "blue")
        o = json.loads(txt)
        self.assertEqual(o["__june_style__"], 1)
        self.assertEqual(o["blocks"], {"id-a": {"variant": "success"}, "id-b": {"flag": "high"}})
        self.assertEqual(o["page"], {"accent": "blue"})
        # nothing valid → no sentinel; an unknown accent is dropped
        self.assertIsNone(_style_text({}, {}, ""))
        self.assertNotIn("page", json.loads(_style_text({0: {"bg": "red"}}, {0: "x"}, "bogus")))


@unittest.skipUnless(_IMPORT_OK, f"june_mcp unavailable: {_IMPORT_ERR}")
class TestPageHandlersEndToEnd(unittest.TestCase):
    def test_create_writes_view_and_media_blocks(self) -> None:
        store: dict = {}
        out = run_tool("june_page_create", _client(store), {
            "title": "Atlas",
            "blocks": [
                {"type": "heading_1", "text": "Atlas"},
                {"type": "view", "node_types": ["entity"], "kind": "table", "cap": 100},
                {"type": "image", "url": "https://cdn/x.png", "alt": "diagram"},
            ]})
        self.assertEqual(out["blocks_written"], 3)
        self.assertEqual(out["layout"]["mode"], "doc")
        page = next(iter(store.values()))
        types = [b["block_type"] for b in page["blocks"]]
        self.assertEqual(types, ["heading_1", "paragraph", "embed"])
        self.assertIn("__june_view__", page["blocks"][1]["text"])
        self.assertEqual(page["blocks"][2]["text"], "![diagram](https://cdn/x.png)")

    def test_canvas_layout_two_phase_lands_layout_block(self) -> None:
        store: dict = {}
        out = run_tool("june_page_create", _client(store), {
            "title": "Board",
            "blocks": [{"type": "view", "node_types": ["decision"], "kind": "board"},
                       {"type": "paragraph", "text": "notes"}],
            "layout": {"mode": "canvas",
                       "cards": [{"block": 0, "x": 0, "y": 0, "w": 320, "h": 200, "title": "Decisions"},
                                 {"block": 1, "x": 340, "y": 0}]}})
        self.assertEqual(out["layout"]["mode"], "canvas")
        self.assertEqual(out["layout"]["cards"], 2)
        page = next(iter(store.values()))
        # 2 content blocks + 1 hidden layout block
        self.assertEqual(len(page["blocks"]), 3)
        layout_blocks = [b for b in page["blocks"] if "__june_layout__" in b["text"]]
        self.assertEqual(len(layout_blocks), 1)
        lay = json.loads(layout_blocks[0]["text"])
        content_ids = {b["block_id"] for b in page["blocks"] if b is not layout_blocks[0]}
        # layout positions key on the REAL server block ids (not temp/index)
        self.assertEqual(set(lay["pos"]), content_ids)
        self.assertIn("Decisions", lay.get("titles", {}).values())

    def test_content_ids_are_stable_across_the_second_save(self) -> None:
        # The two-phase save must UPDATE content blocks in place, never re-create them.
        store: dict = {}
        run_tool("june_page_create", _client(store), {
            "title": "x",
            "blocks": [{"type": "paragraph", "text": "one"}],
            "layout": {"mode": "canvas", "cards": [{"block": 0, "x": 0, "y": 0}]}})
        page = next(iter(store.values()))
        content = [b for b in page["blocks"] if "__june_layout__" not in b["text"]]
        self.assertEqual(len(content), 1)
        self.assertEqual(content[0]["text"], "one")

    def test_create_writes_style_sentinel_keyed_on_real_ids(self) -> None:
        store: dict = {}
        out = run_tool("june_page_create", _client(store), {
            "title": "Styled",
            "theme": "purple",
            "blocks": [
                {"type": "callout", "text": "Heads up", "variant": "warning"},
                {"type": "todo", "text": "ship", "flag": "high"},
                {"type": "paragraph", "text": "plain"},
            ]})
        self.assertEqual(out["blocks_written"], 3)
        self.assertEqual(out["layout"]["styled"], 2)          # two blocks carry style
        page = next(iter(store.values()))
        # 3 content blocks + 1 hidden style block
        style_blocks = [b for b in page["blocks"] if "__june_style__" in b["text"]]
        self.assertEqual(len(style_blocks), 1)
        st = json.loads(style_blocks[0]["text"])
        self.assertEqual(st["page"], {"accent": "purple"})
        content = [b for b in page["blocks"] if b is not style_blocks[0]]
        content_ids = {b["block_id"] for b in content}
        # every styled key is a REAL server content id (not an index/temp)
        self.assertTrue(set(st["blocks"]).issubset(content_ids))
        variants = {v.get("variant") for v in st["blocks"].values()}
        self.assertIn("warning", variants)
        # the content text is untouched by styling (marker rides in the sentinel, not the block text)
        self.assertIn("Heads up", [b["text"] for b in content])

    def test_append_preserves_existing_and_orders_after(self) -> None:
        store: dict = {}
        c = _client(store)
        created = run_tool("june_page_create", c, {
            "title": "Log", "blocks": [{"type": "heading_1", "text": "Log"}]})
        pid = created["page_id"]
        before = list(store[pid]["blocks"])
        out = run_tool("june_page_append", c, {
            "page_id": pid, "blocks": [{"type": "paragraph", "text": "day 2"}]})
        self.assertEqual(out["blocks_appended"], 1)
        self.assertEqual(out["blocks_total"], 2)
        after = store[pid]["blocks"]
        # the original block kept its id (preserved, not re-created)
        self.assertEqual(after[0]["block_id"], before[0]["block_id"])
        self.assertEqual(after[0]["text"], "Log")
        # the new block sorts strictly after the existing content
        self.assertGreater(after[1]["order"], after[0]["order"])
        self.assertEqual(after[1]["text"], "day 2")


@unittest.skipUnless(_IMPORT_OK, f"june_mcp unavailable: {_IMPORT_ERR}")
class TestPromptsAndInstructions(unittest.TestCase):
    def test_instructions_invite_proactive_page_building(self) -> None:
        s = SERVER_INSTRUCTIONS.lower()
        for token in ("proactive", "page", "dashboard", "live view"):
            self.assertIn(token, s)

    def test_prompts_expand_layman_intent(self) -> None:
        names = {p.name for p in PROMPTS}
        self.assertEqual(names, {"june_new_page", "june_dashboard", "june_meeting_notes"})
        dash = render_prompt("june_dashboard", {"topic": "Atlas"})
        self.assertIn("Atlas", dash)
        self.assertIn("june_page_create", dash)
        with self.assertRaises(KeyError):
            render_prompt("nope", {})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


@unittest.skipUnless(_IMPORT_OK, f"june_mcp unavailable: {_IMPORT_ERR}")
class TestProGate(unittest.TestCase):
    """Agent page-authoring (create/write/append) is Pro; reads stay free; UI edits are unaffected
    (they don't go through this connector). pro defaults True so Pro connections + tests are normal."""

    def test_pro_connection_sees_all_page_tools(self):
        from june_mcp.tools import visible_tools
        names = {t.name for t in visible_tools(pro=True)}
        for n in ("june_page_list", "june_page_get", "june_page_create",
                  "june_page_write", "june_page_append", "june_page_delete"):
            self.assertIn(n, names)

    def test_free_connection_hides_agent_page_writes_but_keeps_reads(self):
        from june_mcp.tools import visible_tools
        names = {t.name for t in visible_tools(pro=False)}
        self.assertIn("june_page_list", names)        # reading pages stays free
        self.assertIn("june_page_get", names)
        for n in ("june_page_create", "june_page_write", "june_page_append", "june_page_delete"):
            self.assertNotIn(n, names)                # agent authoring/deletion is Pro

    def test_free_connection_refuses_page_write_with_pro_message(self):
        from june_mcp.tools import run_tool
        with self.assertRaises(KeyError) as ctx:
            run_tool("june_page_create", client=None, args={"title": "x"}, pro=False)
        self.assertIn("Pro", str(ctx.exception))
        # a read verb is still callable on a free connection (reaches the client, which here is None)
        with self.assertRaises(Exception) as ctx2:
            run_tool("june_page_list", client=None, args={}, pro=False)
        self.assertNotIn("requires June Pro", str(ctx2.exception))
