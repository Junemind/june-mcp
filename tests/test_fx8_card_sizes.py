"""FX8: canvas card sizes are PIXELS, bounded by the page vocabulary.

Live round 4 (2026-09-27): once R9 made placed cards a 'poster', an agent's grid-sized cards
(w=4, h=2) drew as four-pixel slivers. Nothing had ever said the unit, and nothing bounded it.
Now the grammar states it with a worked example. Against an engine that checks the vocabulary
(``vocab``), the layout goes as written and the ENGINE bounds it and reports it: one account,
the engine's. Against an older engine this connector bounds it and reports it itself. The app
also clamps at render (frontend page_card_size.test.ts), and the engine side is pinned against
the real routes in june_ai ``tests/test_fx8_card_sizes.py``.
"""
from __future__ import annotations

import json
import math

import pytest

httpx = pytest.importorskip("httpx")

from june_client import JuneClient  # noqa: E402
from june_mcp import page_vocab as V  # noqa: E402
from june_mcp import tools as T  # noqa: E402

S1 = ["meta", "view", "attrs", "sentinel-merge"]
S2 = [*S1, "vocab"]
LO, HI, HMIN = V.CARD_WIDTH_MIN, V.CARD_WIDTH_MAX, V.CARD_HEIGHT_MIN
ROUND4 = {"mode": "canvas", "cards": [{"block": 0, "x": 0, "y": 0, "w": 4, "h": 2, "title": "A"},
                                      {"block": 1, "x": 8, "y": 0, "w": 4, "h": 4}]}
BLOCKS = [{"type": "heading_2", "text": "a"}, {"type": "paragraph", "text": "b"}]


class Engine:
    def __init__(self, features):
        self.features, self.sent = features, []

    def __call__(self, req):
        body = json.loads(req.content) if req.content else None
        path = req.url.path
        if path == "/v1/pages/health":
            return httpx.Response(200, json={"enabled": True, "features": self.features})
        if path == "/v1/pages" and req.method == "POST":
            return httpx.Response(200, json={"page_id": "p1", "title": body["title"]})
        if path == "/v1/pages/p1/view":
            return httpx.Response(200, json={"page_id": "p1", "title": "t", "updated_at": "u1",
                                             "revision": 4, "blocks": [], "attrs": {"content_blocks": 0}})
        if path == "/v1/pages/p1/blocks":
            self.sent.append(body)
            stored = [{**b, "block_id": b.get("id") or f"b{i}", "order": b.get("order", i + 1)}
                      for i, b in enumerate(body.get("blocks") or [])]
            out = {"page_id": "p1", "blocks": stored, "updated_at": f"u{len(self.sent)}",
                   "blocks_total": 2, "revision": 5,
                   "attrs": {"content_blocks": 2, "mode": "canvas", "cards": 2, "styled": 0,
                             "page_style_keys": [], "notes": []}}
            if "vocab" in self.features:
                out["coerced"] = []
            return httpx.Response(200, json=out)
        return httpx.Response(404, json={"detail": f"unhandled {path}"})


def _client(engine):
    return JuneClient("http://june.test", "k", canvas="c0",
                      client=httpx.Client(base_url="http://june.test",
                                          transport=httpx.MockTransport(engine)))


def _sizes(body) -> list[tuple]:
    return [(r["w"], r["h"]) for r in body["layout"]["pos"].values()]


def test_the_connector_bounds_are_the_vocabulary():
    assert (T._CARD_W, T._CARD_H) == (float(V.CARD_WIDTH_DEFAULT), float(V.CARD_HEIGHT_MIN))
    assert (LO, HI, HMIN) == (180, 900, 90)


def test_the_grammar_says_pixels_with_the_bounds_and_an_example():
    create = next(t for t in T.TOOLS if t.name in ("june_page_create",))
    d = create.description
    assert "PIXELS" in d and f"{LO}–{HI} wide" in d and f"at least {HMIN} tall" in d
    assert "x 0, 324, 648 with w 300" in d


class TestClampCards:
    def test_round4_cards_come_back_visible_and_reported_in_the_agents_indices(self):
        out, notes = T._clamp_cards(ROUND4)
        assert [(c["w"], c["h"]) for c in out["cards"]] == [(LO, HMIN), (LO, HMIN)]
        assert [(c["x"], c["y"]) for c in out["cards"]] == [(0, 0), (8, 0)]           # never moved
        assert out["cards"][0]["title"] == "A" and ROUND4["cards"][0]["w"] == 4         # input untouched
        assert [(n["block"], n["field"], n["value"], n["to"]) for n in notes] == [
            (0, "layout.w", 4, LO), (0, "layout.h", 2, HMIN), (1, "layout.w", 4, LO), (1, "layout.h", 4, HMIN)]

    def test_inside_the_bounds_is_silent_and_over_the_max_is_capped(self):
        out, notes = T._clamp_cards({"mode": "canvas", "cards": [{"block": 0, "w": 300, "h": 90}]})
        assert notes == [] and out["cards"][0] == {"block": 0, "w": 300, "h": 90}
        out, notes = T._clamp_cards({"mode": "canvas", "cards": [{"block": 0, "w": 5000}]})
        assert out["cards"][0]["w"] == HI and notes[0]["to"] == HI

    @pytest.mark.parametrize("bad", ["wide", math.nan, math.inf, True])
    def test_a_size_that_is_not_a_finite_number_is_dropped_and_said(self, bad):
        out, notes = T._clamp_cards({"mode": "canvas", "cards": [{"block": 0, "x": 1, "w": bad}]})
        assert out["cards"][0] == {"block": 0, "x": 1}
        assert notes[0]["field"] == "layout.w" and "not stored" in notes[0]["reason"]

    def test_no_cards_is_left_alone(self):
        for lay in (None, "x", {"mode": "doc"}, {"columns": [[0, 1]]}):
            assert T._clamp_cards(lay) == (lay, [])


class TestVocabEngine:
    def test_the_layout_goes_as_written_so_the_engine_account_is_the_only_one(self):
        e = Engine(S2)
        out = T._page_create(_client(e), {"title": "t", "blocks": BLOCKS, "layout": ROUND4})
        assert _sizes(e.sent[0]) == [(4.0, 2.0), (4.0, 4.0)]
        assert e.sent[0]["layout"]["canvas"] == "poster"
        assert "coerced" not in out                                  # the engine said nothing here


class TestOlderEngine:
    @pytest.mark.parametrize("features", [S1, []])
    def test_create_bounds_the_cards_and_reports_them(self, features):
        e = Engine(features)
        out = T._page_create(_client(e), {"title": "t", "blocks": BLOCKS, "layout": ROUND4})
        body = e.sent[-1]
        if "layout" in body:                                          # S1: the merge patch
            assert _sizes(body) == [(LO, HMIN), (LO, HMIN)]
        else:                                                         # legacy: the sentinel block
            sent = [json.loads(b["text"]) for b in body["blocks"] if "__june_layout__" in b["text"]]
            assert [(r["w"], r["h"]) for r in sent[0]["pos"].values()] == [(LO, HMIN), (LO, HMIN)]
        got = [(c["block"], c["field"]) for c in out["coerced"] if c["field"].startswith("layout.")]
        assert got == [(0, "layout.w"), (0, "layout.h"), (1, "layout.w"), (1, "layout.h")]

    def test_write_bounds_them_the_same_way(self):
        e = Engine(S1)
        out = T._page_write(_client(e), {"page_id": "p1", "blocks": BLOCKS, "layout": ROUND4})
        assert _sizes(e.sent[-1]) == [(LO, HMIN), (LO, HMIN)]
        assert sum(c["field"].startswith("layout.") for c in out["coerced"]) == 4
