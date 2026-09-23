"""FX2 / S1 — the connector's side of "the engine owns page settings".

Against an engine that advertises the ``attrs`` and ``view`` page features, the connector must
read through ``/view``, write a page's look as ``style``/``layout`` merge patches in ONE save,
and take its receipts from the engine's ``attrs``. Against anything else it must take the legacy
path exactly as before. These tests pin the WIRE: what is sent, what is read, and when.

The engine's behaviour itself (the merge, the invariant) is proven against the real engine in
june_ai ``tests/test_s1_connector_e2e.py``; this fake only answers in the engine's shapes.
"""
from __future__ import annotations

import json
import re

import pytest

httpx = pytest.importorskip("httpx")

from june_client import JuneClient  # noqa: E402
from june_client.client import UNSET  # noqa: E402
from june_mcp import tools as T  # noqa: E402

FEATURES = ["meta", "view", "attrs", "sentinel-merge"]


class Engine:
    """Records every request; answers like an S1 engine (or a legacy one)."""

    def __init__(self, features=FEATURES, health_status=200, attrs=True):
        self.features, self.health_status, self.attrs = features, health_status, attrs
        self.calls: list[tuple[str, str, dict | None]] = []
        self.content = [{"block_id": "b1", "block_type": "callout", "text": "one", "order": 1.0},
                        {"block_id": "b2", "block_type": "paragraph", "text": "two", "order": 2.0}]

    def _attrs(self, body: dict | None) -> dict:
        style = (body or {}).get("style") or {}
        layout = (body or {}).get("layout") or {}
        n = len((body or {}).get("blocks") or self.content)
        return {"style": style or None, "layout": layout or None, "content_blocks": n,
                "mode": layout.get("mode", "doc"), "cards": len(layout.get("pos") or {}),
                "styled": len(style.get("blocks") or {}),
                "page_style_keys": sorted(style.get("page") or {}), "notes": []}

    def __call__(self, req: "httpx.Request") -> "httpx.Response":
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, req.url.path, body))
        path = req.url.path
        if path == "/v1/pages/health":
            if self.health_status != 200:
                return httpx.Response(self.health_status, json={"detail": "x"})
            return httpx.Response(200, json={"enabled": True, "features": self.features})
        if path == "/v1/pages" and req.method == "POST":
            return httpx.Response(200, json={"page_id": "p1", "title": body["title"]})
        if re.fullmatch(r"/v1/pages/p1/view", path):
            return httpx.Response(200, json={
                "page_id": "p1", "title": "t", "updated_at": "u1", "revision": 3,
                "blocks": self.content,
                "attrs": {**self._attrs(None), "style": {"page": {"accent": "red"}},
                          "notes": ["style-sentinels-at-rest:2 (the first renders)"]}})
        if path == "/v1/pages/p1":
            return httpx.Response(200, json={"page_id": "p1", "title": "t", "updated_at": "u1",
                                             "blocks": self.content})
        if path == "/v1/pages/p1/blocks":
            out = {"page_id": "p1", "title": "t", "updated_at": "u2", "blocks": body["blocks"]}
            if self.attrs:
                out["attrs"] = self._attrs(body)
            return httpx.Response(200, json=out)
        if path == "/v1/pages/p1/blocks:append":
            out = {"page_id": "p1", "appended": [], "blocks_total": 9, "revision": 4}
            if self.attrs:
                out["attrs"] = {**self._attrs(body), "content_blocks": 3}
            return httpx.Response(200, json=out)
        return httpx.Response(404, json={"detail": f"unhandled {req.method} {path}"})

    def sent(self, path_end: str) -> list[dict]:
        return [b for m, p, b in self.calls if p.endswith(path_end) and m == "POST"]

    def probes(self) -> int:
        return sum(1 for _, p, _ in self.calls if p == "/v1/pages/health")


def _client(engine: Engine) -> JuneClient:
    return JuneClient("http://june.test", "k", canvas="c0",
                      client=httpx.Client(base_url="http://june.test",
                                          transport=httpx.MockTransport(engine)))


# ── the SDK ────────────────────────────────────────────────────────────────────────────────
class TestClientAttrs:
    def test_features_are_asked_once_and_shared_by_views(self):
        e = Engine()
        c = _client(e)
        assert c.pages_features() == frozenset(FEATURES)
        c.for_canvas("c1").pages_features()
        c.for_canvas("c2").pages_features()
        assert e.probes() == 1
        c.forget_pages_features()
        c.pages_features()
        assert e.probes() == 2

    def test_an_engine_without_the_route_advertises_nothing(self):
        assert _client(Engine(health_status=404)).pages_features() == frozenset()

    def test_could_not_ask_raises_and_caches_nothing(self):
        e = Engine(health_status=500)
        c = _client(e)
        for _ in range(2):
            with pytest.raises(httpx.HTTPStatusError):
                c.pages_features()
        assert e.probes() == 2

    def test_omitted_fields_are_not_sent_and_None_is_sent_as_null(self):
        e = Engine()
        c = _client(e)
        c.save_blocks("p1", [{"text": "a"}], force=True)
        c.save_blocks("p1", [{"text": "a"}], force=True, style=None, layout={"mode": "doc"},
                      keep_attrs=True)
        first, second = e.sent("/blocks")
        assert set(first) == {"blocks"}
        assert second["style"] is None and second["layout"] == {"mode": "doc"} and second["keep_attrs"] is True
        c.append_blocks("p1", [{"text": "x"}])
        c.append_blocks("p1", [{"text": "x"}], style={"blocks": {"#0": {"flag": "high"}}})
        a1, a2 = e.sent("blocks:append")
        assert "style" not in a1 and a2["style"] == {"blocks": {"#0": {"flag": "high"}}}
        assert not UNSET and repr(UNSET) == "UNSET"

    def test_a_styled_append_never_falls_back_to_an_unstyled_legacy_save(self):
        def old_engine(req):
            return httpx.Response(404, json={"detail": "no route"})
        c = JuneClient("http://june.test", "k", canvas="c0", client=httpx.Client(
            base_url="http://june.test", transport=httpx.MockTransport(old_engine)))
        with pytest.raises(RuntimeError, match="nothing was appended"):
            c.append_blocks("p1", [{"text": "x"}], style={"page": {"accent": "red"}})


# ── the connector on an S1 engine ─────────────────────────────────────────────────────────────
class TestConnectorS1:
    def test_page_get_reads_the_view_and_returns_attributes_as_fields(self):
        e = Engine()
        out = T.run_tool("june_page_get", _client(e), {"page_id": "p1"})
        assert [p for _, p, _ in e.calls if p.startswith("/v1/pages/p1")] == ["/v1/pages/p1/view"]
        assert [b["text"] for b in out["blocks"]] == ["one", "two"]
        assert out["style"] == {"page": {"accent": "red"}} and "attrs" not in out
        assert "at-rest" in out["_notes"]["attrs"]

    def test_page_write_is_one_save_with_patches_and_a_receipt_from_attrs(self):
        e = Engine()
        out = T.run_tool("june_page_write", _client(e), {
            "page_id": "p1", "theme": "green",
            "layout": {"mode": "canvas", "cards": [{"block": 2, "x": 0, "y": 0}]},
            "blocks": [{"id": "b1", "type": "callout", "text": "one", "variant": "tip"},
                       "skipped: not an object",
                       {"id": "b2", "type": "paragraph", "text": "two"}]})
        saves = e.sent("/blocks")
        assert len(saves) == 1, "one save — no second save keyed on ids from the first"
        body = saves[0]
        assert body["keep_attrs"] is True and body["expected_updated_at"] == "u1"
        assert body["style"]["page"] == {"accent": "green"}
        assert body["style"]["blocks"] == {"#0": {"variant": "tip", "flag": None, "bg": None,
                                                  "accent": None, "icon": None, "space": None}}
        assert list(body["layout"]["pos"]) == ["#1"], "input index 2 is payload index 1"
        assert body["layout"]["mode"] == "canvas"
        assert out["layout"] == {"mode": "canvas", "cards": 1, "styled": 1, "page_style_keys": ["accent"]}
        assert out["blocks_before"] == 2 and out["blocks_written"] == 2 and "warning" not in out
        assert not any(p == "/v1/pages/p1" for _, p, _ in e.calls), "the guard read is /view"

    def test_a_plain_content_write_still_keeps_the_look(self):
        e = Engine()
        T.run_tool("june_page_write", _client(e), {"page_id": "p1", "blocks": [
            {"id": "b1", "type": "callout", "text": "one"}, {"id": "b2", "type": "paragraph", "text": "2"}]})
        body = e.sent("/blocks")[0]
        assert body["keep_attrs"] is True and "style" not in body and "layout" not in body

    def test_page_create_sends_the_look_in_the_one_save(self):
        e = Engine()
        out = T.run_tool("june_page_create", _client(e), {
            "title": "t", "theme": "sky", "icon": "📌", "cover": "ocean",
            "layout": {"columns": [[0, 1]]},
            "blocks": [{"type": "paragraph", "text": "a"}, {"type": "paragraph", "text": "b"}]})
        (body,) = e.sent("/blocks")
        assert body["style"] == {"page": {"accent": "sky", "icon": "📌", "cover": "ocean"}}
        assert body["layout"]["colGroups"] == [["#0", "#1"]] and body["layout"]["mode"] == "doc"
        assert out["layout"]["page_style_keys"] == ["accent", "cover", "icon"]
        assert out["layout"]["mode"] == "doc"

    def test_page_create_with_nothing_to_write_does_not_save(self):
        e = Engine()
        out = T.run_tool("june_page_create", _client(e), {"title": "empty"})
        assert e.sent("/blocks") == [] and out["blocks_written"] == 0

    def test_append_styles_its_own_blocks(self):
        e = Engine()
        out = T.run_tool("june_page_append", _client(e), {"page_id": "p1", "blocks": [
            {"type": "paragraph", "text": "x"}, {"type": "todo", "text": "y", "flag": "high"}]})
        (body,) = e.sent("blocks:append")
        assert list(body["style"]["blocks"]) == ["#1"] and "page" not in body["style"]
        assert out["layout"]["styled"] == 1 and out["blocks_total"] == 3
        assert "_notes" not in out

    def test_an_unstyled_append_sends_no_fields(self):
        e = Engine()
        T.run_tool("june_page_append", _client(e), {"page_id": "p1", "blocks": [{"type": "paragraph", "text": "x"}]})
        assert set(e.sent("blocks:append")[0]) == {"blocks"}

    def test_a_response_without_attrs_is_reported_and_the_capability_is_asked_again(self):
        e = Engine(attrs=False)
        c = _client(e)
        out = T.run_tool("june_page_write", c, {"page_id": "p1", "theme": "green", "blocks": [
            {"id": "b1", "type": "paragraph", "text": "one"}]})
        assert "did not confirm the styling" in out["warning"]
        before = e.probes()
        T.run_tool("june_page_get", c, {"page_id": "p1"})
        assert e.probes() == before + 1, "the cached answer was dropped"


# ── the connector on a legacy engine: the path it always took ──────────────────────────────────
class TestConnectorLegacy:
    @pytest.mark.parametrize("engine", [Engine(features=["meta"]), Engine(health_status=404),
                                        Engine(health_status=503)])
    def test_no_s1_means_the_legacy_read_and_no_new_fields(self, engine):
        c = _client(engine)
        T.run_tool("june_page_get", c, {"page_id": "p1"})
        T.run_tool("june_page_write", c, {"page_id": "p1", "blocks": [
            {"id": "b1", "type": "paragraph", "text": "one"}, {"id": "b2", "type": "paragraph", "text": "two"}]})
        paths = [p for _, p, _ in engine.calls]
        assert "/v1/pages/p1/view" not in paths
        for body in engine.sent("/blocks"):
            assert not {"style", "layout", "keep_attrs"} & set(body)

    def test_a_styled_append_on_a_legacy_engine_says_it_was_not_styled(self):
        e = Engine(features=["meta"])
        out = T.run_tool("june_page_append", _client(e), {"page_id": "p1", "blocks": [
            {"type": "todo", "text": "y", "flag": "high"}]})
        assert "UNSTYLED" in out["_notes"]["styling_ignored"]
        assert set(e.sent("blocks:append")[0]) == {"blocks"}
