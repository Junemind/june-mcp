"""CX12 (connector half) — ``june_page_update``: edit NAMED blocks in place,
without transporting the page (written FIRST, per plan-v2 discipline).

Found live 2026-08-20: fixing ten blocks of a 78-block page through the
connector required resending the ENTIRE page (june_page_write is authoritative
— omissions delete), which payload-constrained callers cannot always do; the
safest edit had no small-payload shape. The engine gained the guarded
``blocks:update`` route (CX12); this is the tool that speaks it.

Claims under guard:
1. ``client.update_blocks`` posts ONLY ``{id, text[, block_type]}`` per block
   to ``…/blocks:update`` — a caller-supplied ``order`` is STRIPPED (the route
   would 422 it; the client never even utters it), and guard fields
   (``expected_revision`` / ``force``) pass through.
2. The tool refuses malformed input before any wire call: missing page_id,
   empty blocks, or any block without an ``id`` (an update names its targets —
   the id-less shape belongs to june_page_append).
3. A 404 fails LOUDLY with an actionable message — never a silent fallback to
   the full-page save (this is a NEW tool; a fallback would reintroduce
   exactly the transport-the-document shape it exists to avoid).
4. Posture parity: hidden+refused when read-only, Pro-gated, canvas-scoped
   with the CX6 receipt.

httpx.MockTransport throughout (no server, no network).
"""
from __future__ import annotations

import json
import unittest

try:
    import httpx

    from june_client import JuneClient
    from june_mcp.tools import run_tool, visible_tools
    _IMPORT_OK, _IMPORT_ERR = True, ""
except Exception as exc:  # pragma: no cover
    _IMPORT_OK, _IMPORT_ERR = False, repr(exc)

_DEFAULT = "99999999-9999-9999-9999-999999999999"
_B1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_B2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _service(seen: dict, *, update_status: int = 200, update_detail: str = ""):
    def handler(req: httpx.Request) -> httpx.Response:
        seen.setdefault("calls", []).append((req.method, req.url.path))
        if req.url.path == "/v1/canvases":
            return httpx.Response(200, json=[
                {"canvas_id": _DEFAULT, "name": "default", "created_at": "2026-08-01"}])
        if req.url.path.endswith("/blocks:update"):
            seen["payload"] = json.loads(req.content)
            if update_status != 200:
                return httpx.Response(update_status, json={"detail": update_detail})
            blocks = seen["payload"]["blocks"]
            return httpx.Response(200, json={
                "page_id": "p1",
                "updated": [{"block_id": b["id"], "block_type": b.get("block_type", "paragraph"),
                             "text": b["text"], "order": 5.0} for b in blocks],
                "updated_at": "t-new", "revision": 9, "blocks_total": 78})
        return httpx.Response(200, json={"ok": True})
    return httpx.MockTransport(handler)


def _client(seen: dict, **kw) -> "JuneClient":
    return JuneClient(api_key="k", canvas=_DEFAULT,
                      client=httpx.Client(transport=_service(seen, **kw),
                                          base_url="http://june"))


@unittest.skipUnless(_IMPORT_OK, f"import failed: {_IMPORT_ERR}")
class TestClientSpeaksTheRoute(unittest.TestCase):
    def test_posts_only_id_text_type_and_strips_order(self) -> None:
        seen: dict = {}
        out = _client(seen).update_blocks("p1", [
            {"id": _B1, "text": "new text", "order": 99.0, "sneaky": True},
            {"id": _B2, "text": "other", "block_type": "heading_2"}])
        self.assertEqual(seen["calls"][-1], ("POST", "/v1/pages/p1/blocks:update"))
        sent = seen["payload"]["blocks"]
        self.assertEqual(sent[0], {"id": _B1, "text": "new text"})
        self.assertEqual(sent[1], {"id": _B2, "text": "other", "block_type": "heading_2"})
        self.assertEqual(out["revision"], 9)

    def test_guard_fields_pass_through(self) -> None:
        seen: dict = {}
        _client(seen).update_blocks("p1", [{"id": _B1, "text": "x"}], expected_revision=7)
        self.assertEqual(seen["payload"]["expected_revision"], 7)
        self.assertNotIn("force", seen["payload"])          # absent unless asked
        _client(seen).update_blocks("p1", [{"id": _B1, "text": "x"}], force=True)
        self.assertIs(seen["payload"]["force"], True)


@unittest.skipUnless(_IMPORT_OK, f"import failed: {_IMPORT_ERR}")
class TestToolContract(unittest.TestCase):
    def test_happy_path_reports_counts_and_receipt(self) -> None:
        seen: dict = {}
        out = run_tool("june_page_update", _client(seen), {
            "page_id": "p1",
            "blocks": [{"id": _B1, "text": "fixed"}, {"id": _B2, "text": "also fixed"}]})
        self.assertEqual(out["blocks_updated"], 2)
        self.assertEqual(out["blocks_total"], 78)
        self.assertEqual(out["revision"], 9)
        self.assertEqual(out["canvas"], _DEFAULT)           # CX6 receipt

    def test_refuses_malformed_input_before_any_wire_call(self) -> None:
        seen: dict = {}
        c = _client(seen)
        for args in ({"blocks": [{"id": _B1, "text": "x"}]},          # no page_id
                     {"page_id": "p1"},                               # no blocks
                     {"page_id": "p1", "blocks": []},                 # empty
                     {"page_id": "p1", "blocks": [{"text": "no id"}]}):
            with self.assertRaises(ValueError, msg=repr(args)):
                run_tool("june_page_update", c, args)
        self.assertEqual([c_ for c_ in seen.get("calls", [])
                          if c_[1].endswith("blocks:update")], [],
                         "malformed input must never reach the wire")

    def test_404_fails_loudly_with_no_fallback(self) -> None:
        seen: dict = {}
        c = _client(seen, update_status=404, update_detail="Not Found")
        with self.assertRaises(KeyError) as ctx:
            run_tool("june_page_update", c, {"page_id": "p1",
                                             "blocks": [{"id": _B1, "text": "x"}]})
        msg = str(ctx.exception)
        self.assertIn("june_page_get", msg)                 # actionable next step
        self.assertIn("nothing was updated", msg.lower())
        # crucially: no second request tried the full-save route
        paths = [p for _m, p in seen["calls"]]
        self.assertNotIn("/v1/pages/p1/blocks", paths,
                         "a 404 must not fall back to the full-page save")

    def test_readonly_and_pro_postures(self) -> None:
        names_ro = {t.name for t in visible_tools(readonly=True)}
        self.assertNotIn("june_page_update", names_ro)
        names_free = {t.name for t in visible_tools(readonly=False, pro=False)}
        self.assertNotIn("june_page_update", names_free)
        names = {t.name for t in visible_tools()}
        self.assertIn("june_page_update", names)
        seen: dict = {}
        with self.assertRaises(KeyError):
            run_tool("june_page_update", _client(seen),
                     {"page_id": "p1", "blocks": [{"id": _B1, "text": "x"}]},
                     readonly=True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
