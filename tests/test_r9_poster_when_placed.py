"""R9: a canvas card the agent PLACES (x/y) lands where it said.

The app's canvas has two arrangements: 'flow' (the default) packs cards into columns and ignores
x/y; 'poster' places each card at its x/y. The connector never wrote 'poster', so every agent
dashboard with coordinates rendered as a packed column (plan page 48145480, R9: "canvas card x/y
ignored ... the connector never writes canvas: 'poster'").
"""
from __future__ import annotations

import json

from june_mcp.tools import _layout_patch, _layout_text

IDS = {0: "id-a", 1: "id-b", 2: "id-c"}


def _body(cards, columns=None):
    return json.loads(_layout_text(cards, IDS, columns=columns))


def test_placed_cards_are_a_poster():
    body = _body([{"block": 0, "x": 10, "y": 20}, {"block": 1, "x": 340, "y": 0}])
    assert body["mode"] == "canvas" and body["canvas"] == "poster"


def test_one_coordinate_is_enough_and_zero_counts():
    assert _body([{"block": 0, "x": 0}])["canvas"] == "poster"
    assert _body([{"block": 0, "y": 0}])["canvas"] == "poster"


def test_cards_without_coordinates_keep_the_default_flow():
    body = _body([{"block": 0, "title": "A"}, {"block": 1, "w": 300}])
    assert body["mode"] == "canvas" and "canvas" not in body


def test_null_coordinates_are_not_a_placement():
    assert "canvas" not in _body([{"block": 0, "x": None, "y": None}])


def test_unresolvable_cards_do_not_turn_a_page_into_a_poster():
    body = _body([{"block": 0}, {"block": 99, "x": 5, "y": 5}])
    assert "canvas" not in body, "only cards that resolve to a block can place anything"


def test_columns_only_is_a_doc_never_a_poster():
    body = _body(None, columns=[[0, 1]])
    assert body["mode"] == "doc" and "canvas" not in body


def test_the_s1_merge_patch_carries_it_too():
    patch = _layout_patch({"mode": "canvas", "cards": [{"block": 0, "x": 8, "y": 16}]}, IDS)
    assert patch["canvas"] == "poster" and patch["mode"] == "canvas"
