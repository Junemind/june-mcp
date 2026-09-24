"""S2 — the connector's side of "one page vocabulary, validated at write time".

Against an engine that advertises ``vocab`` the connector sends the agent's block types and style
values AS WRITTEN, and passes the engine's ``coerced`` through to the receipt in the agent's own
terms (positions in the ``blocks`` it sent). Against an older engine it filters as before and
builds the same ``coerced`` list itself, from its generated copy of the vocabulary. A clean write's
receipt is unchanged. Also: FX N15 (blocks past the per-call cap) and FX N14 (an argument another
op of the family takes) are reported, never silent.

The engine side is pinned against the real routes in june_ai ``tests/test_s2_page_vocab_routes.py``.
"""
from __future__ import annotations

import json

import pytest

httpx = pytest.importorskip("httpx")

from june_client import JuneClient  # noqa: E402
from june_mcp import page_vocab as V  # noqa: E402
from june_mcp import tools as T  # noqa: E402
from june_mcp.server import _tag_op  # noqa: E402
from june_mcp.surfaces import build_surface, ignored_args, resolve_call  # noqa: E402

S1 = ["meta", "view", "attrs", "sentinel-merge"]
S2 = [*S1, "vocab"]


class Engine:
    def __init__(self, features=S2, coerced=None):
        self.features, self.coerced = features, coerced or []
        self.sent: list[tuple[str, dict]] = []

    def __call__(self, req):
        body = json.loads(req.content) if req.content else None
        path = req.url.path
        if path == "/v1/pages/health":
            return httpx.Response(200, json={"enabled": True, "features": self.features})
        if path == "/v1/pages" and req.method == "POST":
            return httpx.Response(200, json={"page_id": "p1", "title": body["title"]})
        if path == "/v1/pages/p1/view":
            return httpx.Response(200, json={"page_id": "p1", "title": "t", "updated_at": "u1",
                                             "blocks": [], "attrs": {"content_blocks": 0}})
        attrs = {"content_blocks": len((body or {}).get("blocks") or []), "mode": "doc",
                 "cards": 0, "styled": 0, "page_style_keys": [], "notes": []}
        if path in ("/v1/pages/p1/blocks", "/v1/pages/p1/blocks:append", "/v1/pages/p1/blocks:update"):
            self.sent.append((path, body))
            out = {"page_id": "p1", "blocks": body.get("blocks"), "appended": [], "updated": [],
                   "blocks_total": 2, "revision": 5, "attrs": attrs}
            if "vocab" in self.features:
                out["coerced"] = self.coerced
            return httpx.Response(200, json=out)
        return httpx.Response(404, json={"detail": f"unhandled {path}"})


def _client(engine):
    return JuneClient("http://june.test", "k", canvas="c0",
                      client=httpx.Client(base_url="http://june.test",
                                          transport=httpx.MockTransport(engine)))


BLOCKS = ["not a block",                                           # skipped: agent index 0
          {"type": "h1", "text": "Title", "variant": "Fancy"},     # agent 1 → payload 0
          {"type": "paragraph", "text": "x", "color": "crimson"}]  # agent 2 → payload 1


class TestVocabEngine:
    def test_types_and_style_values_go_as_written(self):
        e = Engine()
        T._page_create(_client(e), {"title": "t", "blocks": BLOCKS, "theme": "Neon", "cover": "ink"})
        path, body = e.sent[0]
        assert [b["block_type"] for b in body["blocks"]] == ["h1", "paragraph"]
        assert body["style"]["blocks"]["#0"]["variant"] == "Fancy"
        assert body["style"]["blocks"]["#1"]["bg"] == "crimson"
        assert body["style"]["page"] == {"accent": "Neon", "cover": "ink"}

    def test_engine_coerced_comes_back_in_the_agents_positions(self):
        e = Engine(coerced=[{"block": 0, "field": "block_type", "value": "h1", "to": "heading_1", "reason": "r"},
                            {"block": "#1", "field": "bg", "value": "crimson", "reason": "r"},
                            {"block": "page", "field": "accent", "value": "Neon", "to": "neon", "reason": "r"}])
        out = T._page_create(_client(e), {"title": "t", "blocks": BLOCKS})
        assert [c["block"] for c in out["coerced"]] == [1, 2, "page"]
        assert "_notes" not in out or "vocabulary" not in out["_notes"]

    def test_a_clean_write_has_no_coerced_key(self):
        out = T._page_create(_client(Engine()), {"title": "t", "blocks": [{"type": "paragraph", "text": "x"}]})
        assert "coerced" not in out

    def test_append_and_update_pass_it_through(self):
        e = Engine(coerced=[{"block": 0, "field": "text", "value": "[date: x]", "reason": "r"}])
        c = _client(e)
        out = T._page_append(c, {"page_id": "p1", "blocks": [{"type": "paragraph", "text": "[date: x]"}]})
        assert out["coerced"][0]["field"] == "text"
        out = T._page_update(c, {"page_id": "p1", "force": True, "blocks": [{"id": "b1", "text": "[date: x]"}]})
        assert out["coerced"][0]["block"] == 0


class TestOlderEngine:
    def test_filtered_as_before_and_reported_locally(self):
        e = Engine(features=S1)
        out = T._page_create(_client(e), {"title": "t", "blocks": BLOCKS, "theme": "neon",
                                          "icon": "👨‍👩‍👧‍👦"})
        _, body = e.sent[0]
        assert [b["block_type"] for b in body["blocks"]] == ["heading_1", "paragraph"]   # alias resolved here
        assert "bg" not in json.dumps(body.get("style") or {})
        got = {(c["block"], c["field"]) for c in out["coerced"]}
        assert got == {(1, "block_type"), (1, "variant"), (2, "bg"), ("page", "accent"), ("page", "icon")}
        assert "does not check text controls" in out["_notes"]["vocabulary"]

    def test_unknown_type_is_still_a_paragraph_here(self):
        e = Engine(features=[])
        out = T._page_create(_client(e), {"title": "t", "blocks": [{"type": "table", "text": "x"}]})
        assert out["coerced"][0]["to"] == "paragraph" and "unknown block type" in out["coerced"][0]["reason"]


class TestCap:
    def test_create_reports_the_cut(self):
        many = [{"type": "paragraph", "text": str(i)} for i in range(T.MAX_PAGE_BLOCKS + 3)]
        out = T._page_create(_client(Engine()), {"title": "t", "blocks": many})
        cut = [c for c in out["coerced"] if c["field"] == "blocks"]
        assert cut and cut[0]["value"] == 3 and "june_page_append" in cut[0]["reason"]

    def test_write_refuses_rather_than_delete_the_tail(self):
        e = Engine()
        many = [{"type": "paragraph", "text": str(i)} for i in range(T.MAX_PAGE_BLOCKS + 1)]
        out = T._page_write(_client(e), {"page_id": "p1", "blocks": many})
        assert out["refused"] == "too_many_blocks" and e.sent == []


class TestIgnoredArguments:
    def test_an_argument_only_another_op_takes_is_named(self):
        surface = build_surface("compact")
        args = {"op": "append", "page_id": "p1", "blocks": [{"text": "x"}], "theme": "red", "icon": "🧱"}
        assert ignored_args(surface, "june_page_edit", args) == ["icon", "theme"]
        member, a = resolve_call(surface, "june_page_edit", dict(args))
        res = _tag_op({"page_id": "p1"}, "june_page_edit", member, surface, ["icon", "theme"])
        assert "does not take icon, theme" in res["_notes"]["ignored_arguments"]

    def test_nothing_ignored_is_nothing_said(self):
        surface = build_surface("compact")
        assert ignored_args(surface, "june_page_edit", {"op": "append", "page_id": "p", "blocks": []}) == []
        assert "_notes" not in _tag_op({"x": 1}, "june_page_edit", "june_page_append", surface, [])


def test_the_connector_uses_the_generated_vocabulary():
    assert T._STYLE_COLORS == set(V.COLORS) and T._COVER_KEYS == set(V.COVERS)
    assert T._PAGE_BLOCK_TYPES == set(V.BLOCK_TYPES)
    assert not V.icon_ok("🧱🧱🧱🧱a") and V.icon_ok("🧱🧱🧱🧱")      # N17: UTF-16 units, as the app


class TestGrammar:
    """S2's four grammar changes (L7, the data-URI rule, R8, G2's `theme`), and the lists taught
    being the vocabulary itself."""

    def setup_method(self):
        from june_mcp.surfaces import PAGE_GRAMMAR
        self.g = PAGE_GRAMMAR

    def test_the_four_lessons(self):
        assert "{type:'todo', text:" in self.g                        # L7: in op='grammar' now
        assert "percent-encode a data: URI" in self.g
        assert "`icon` badges a CANVAS card (it is not drawn on a document block)" in self.g
        assert "`theme` = a colour, one of " + "|".join(V.VOCAB["colors"]) in self.g
        assert "the receipt's `coerced` names it" in self.g

    def test_every_list_is_the_vocabulary(self):
        for needle in (" ".join(V.ILLUSTRATION_NAMES), " ".join(V.VOCAB["colors"]),
                       "|".join(V.VOCAB["callout_variants"]), "|".join(V.VOCAB["todo_flags"]),
                       "|".join(V.VOCAB["covers"]), "size " + "|".join(V.ILLUSTRATION_SIZES)):
            assert needle in self.g, needle

    def test_theme_is_described_not_an_enum(self):
        for name in ("june_page_create", "june_page_write"):
            prop = T._BY_NAME[name].input_schema["properties"]["theme"]
            assert "enum" not in prop and "|".join(V.VOCAB["colors"]) in prop["description"]
