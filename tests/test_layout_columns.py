"""Doc-column layout via the STRUCTURED path (0.2.5) — the guarded alternative to the
raw-sentinel hand-edit (2026-08-21 lesson: the sentinel is all-or-nothing text; only a
create/write-scoped structured param can be safe enough to TEACH)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from june_mcp.tools import _layout_text  # noqa: E402

IDS = {0: "id-a", 1: "id-b", 2: "id-c", 3: "id-d"}


def test_columns_resolve_zero_based_indices_to_ids() -> None:
    out = json.loads(_layout_text(None, IDS, columns=[[0, 1, 2]]))
    assert out["colGroups"] == [["id-a", "id-b", "id-c"]]
    assert out["mode"] == "doc"                      # columns alone never force canvas mode
    assert out["pos"] == {}


def test_under_two_resolvable_blocks_dissolves_the_group() -> None:
    assert _layout_text(None, IDS, columns=[[0]]) is None
    assert _layout_text(None, IDS, columns=[[0, 99]]) is None      # 99 unresolvable → 1 left
    out = json.loads(_layout_text(None, IDS, columns=[[0], [1, 2]]))
    assert out["colGroups"] == [["id-b", "id-c"]]


def test_junk_is_inert_never_an_error() -> None:
    assert _layout_text(None, IDS, columns="nope") is None
    assert _layout_text(None, IDS, columns=[["x", None], "junk"]) is None
    assert _layout_text(None, IDS, columns=None) is None
    out = json.loads(_layout_text(None, IDS, columns=[[0, "1"]]))  # numeric strings coerce
    assert out["colGroups"] == [["id-a", "id-b"]]


def test_duplicate_indices_collapse() -> None:
    out = json.loads(_layout_text(None, IDS, columns=[[0, 0, 1]]))
    assert out["colGroups"] == [["id-a", "id-b"]]


def test_cards_and_columns_coexist_cards_untouched() -> None:
    cards = [{"block": 0, "x": 10, "y": 20, "w": 300, "h": 90}]
    out = json.loads(_layout_text(cards, IDS, columns=[[1, 2]]))
    assert out["mode"] == "canvas"                   # cards win the mode, as before
    assert out["pos"]["id-a"]["x"] == 10
    assert out["colGroups"] == [["id-b", "id-c"]]
    # and the pre-columns call shape is byte-compatible: no columns → no colGroups key
    legacy = json.loads(_layout_text(cards, IDS))
    assert "colGroups" not in legacy
