r"""Dropdowns inside table cells — pipe-escape normalization (2026-08-21).

THE INCIDENT THIS ENCODES: an agent wrote `[select: *up #green | flat #amber | down #red]`
inside a Markdown-table cell. `|` is simultaneously the GFM cell separator and the select
option separator; the frontend's `_cells` splits on unescaped pipes, so the dropdown was
sliced into ghost columns, truncated at the header count — and a subsequent UI save wrote
the truncation back (a whole column destroyed). GFM's own answer is `\|` (a literal pipe
inside a cell), which `_cells` un-escapes before `parseSelect` runs — the app's own table
editor always writes through `tableToMarkdown`, which auto-escapes. The AGENT path was the
only writer composing raw table text by hand.

The fix is normalization at the connector boundary — make the invalid unrepresentable
rather than teaching every model on earth an escape rule:

* Applies ONLY when BOTH hold: the block text contains a real GFM table (header row +
  `---` separator, the frontend's own detection), AND the select/multi span sits on a line
  with structural pipes outside the span (i.e. genuinely inside a table row).
* Standalone select blocks, mid-prose selects, and select-free tables are byte-identical
  after normalization — escaping a NON-table select would corrupt its labels, because
  `parseSelect` splits on raw pipes.
* Idempotent: already-escaped `\|` is never double-escaped, so agent-written canonical
  form and round-tripped text both pass through unchanged.
* Seam coverage: `_one_block` (create/write/append) for paragraph/text blocks only —
  never `code` (verbatim display) or `embed`/sentinel blocks — and `_page_update`'s
  payload loop under the same type rule.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from june_mcp.tools import _escape_select_pipes_in_tables, _one_block  # noqa: E402

TABLE_RAW = (
    "| Course | Status |\n"
    "| --- | --- |\n"
    "| OS | [select: *on track #green | needs work #amber | behind #red] |"
)
TABLE_ESCAPED = (
    "| Course | Status |\n"
    "| --- | --- |\n"
    "| OS | [select: *on track #green \\| needs work #amber \\| behind #red] |"
)


# ── the helper ────────────────────────────────────────────────────────────────

def test_select_in_table_cell_is_escaped():
    assert _escape_select_pipes_in_tables(TABLE_RAW) == TABLE_ESCAPED


def test_multi_in_table_cell_is_escaped():
    raw = "| A | B |\n| --- | --- |\n| x | [multi: *a | b | c] |"
    assert _escape_select_pipes_in_tables(raw) == \
        "| A | B |\n| --- | --- |\n| x | [multi: *a \\| b \\| c] |"


def test_standalone_select_block_untouched():
    # A whole-block dropdown MUST keep raw pipes — parseSelect splits on them.
    raw = "[select: todo #amber | *in progress #blue | done #green]"
    assert _escape_select_pipes_in_tables(raw) == raw


def test_mid_prose_select_untouched():
    raw = "Status today: [select: A | *B] — revisit Friday."
    assert _escape_select_pipes_in_tables(raw) == raw


def test_prose_with_stray_pipe_but_no_table_untouched():
    # Structural-pipe check alone would false-positive here; the table gate saves it.
    raw = "either [select: A | B] or use x | y as you like"
    assert _escape_select_pipes_in_tables(raw) == raw


def test_table_without_selects_is_byte_identical():
    raw = "| a | b |\n| --- | --- |\n| 1 | 2 |"
    assert _escape_select_pipes_in_tables(raw) is raw or \
        _escape_select_pipes_in_tables(raw) == raw


def test_structural_cell_pipes_outside_span_untouched():
    # Only pipes INSIDE the select span are escaped — the row's own separators survive.
    out = _escape_select_pipes_in_tables(TABLE_RAW)
    assert out.count("\\|") == 2
    for line in out.split("\n"):
        assert line.startswith("|") and line.endswith("|")


def test_idempotent_on_canonical_form():
    once = _escape_select_pipes_in_tables(TABLE_RAW)
    assert _escape_select_pipes_in_tables(once) == once


def test_multiple_select_cells_same_row():
    raw = ("| C | S | K |\n| --- | --- | --- |\n"
           "| OS | [select: a | *b] | [select: *x | y] |")
    out = _escape_select_pipes_in_tables(raw)
    assert "[select: a \\| *b]" in out and "[select: *x \\| y]" in out


def test_select_on_non_table_line_of_table_block_untouched():
    # A block can hold a table AND trailing prose with a standalone select; only the
    # in-row span is escaped, the prose select keeps raw pipes.
    raw = TABLE_RAW + "\n\nOverall: [select: good | *mixed]"
    out = _escape_select_pipes_in_tables(raw)
    assert "[select: good | *mixed]" in out
    assert "\\| needs work" in out


# ── the seams ────────────────────────────────────────────────────────────────

def test_one_block_paragraph_escapes():
    out = _one_block({"type": "paragraph", "text": TABLE_RAW}, 1.0)
    assert out["text"] == TABLE_ESCAPED


def test_one_block_legacy_text_alias_escapes():
    out = _one_block({"type": "text", "text": TABLE_RAW}, 1.0)
    assert out["text"] == TABLE_ESCAPED


def test_one_block_code_block_verbatim():
    # Code renders verbatim — normalizing it would put literal backslashes on screen.
    out = _one_block({"type": "code", "text": TABLE_RAW}, 1.0)
    assert out["text"] == TABLE_RAW


def test_one_block_default_type_escapes():
    out = _one_block({"text": TABLE_RAW}, 1.0)          # absent type → paragraph
    assert out["text"] == TABLE_ESCAPED


def test_one_block_ordinary_paragraph_untouched():
    out = _one_block({"type": "paragraph", "text": "plain prose, no controls"}, 1.0)
    assert out["text"] == "plain prose, no controls"


def test_view_sentinel_untouched():
    out = _one_block({"type": "view", "node_types": ["entity"]}, 1.0)
    assert "__june_view__" in out["text"] and "\\|" not in out["text"]


# ── june_page_update seam ─────────────────────────────────────────────────────

class _CaptureClient:
    def __init__(self):
        self.sent = None

    def update_blocks(self, page_id, blocks, **kw):
        self.sent = blocks
        return {"blocks_total": len(blocks), "revision": 2}


def test_page_update_escapes_paragraph_text():
    from june_mcp.tools import _page_update
    c = _CaptureClient()
    _page_update(c, {"page_id": "p1", "blocks": [
        {"id": "b1", "text": TABLE_RAW},                          # type absent
        {"id": "b2", "text": TABLE_RAW, "block_type": "paragraph"},
        {"id": "b3", "text": TABLE_RAW, "block_type": "code"},    # verbatim — untouched
    ]})
    assert c.sent[0]["text"] == TABLE_ESCAPED
    assert c.sent[1]["text"] == TABLE_ESCAPED
    assert c.sent[2]["text"] == TABLE_RAW


def test_page_update_leaves_textless_blocks_alone():
    from june_mcp.tools import _page_update
    c = _CaptureClient()
    _page_update(c, {"page_id": "p1", "blocks": [{"id": "b1", "block_type": "todo_done"}]})
    assert "text" not in c.sent[0] or c.sent[0].get("text") in (None, "")
