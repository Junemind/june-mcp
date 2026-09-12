"""Teaching-content pins — the connector's answer to the frontend's brief-content tests.

THE RULE THIS ENCODES (Things to Remember, the lockstep table): June's capabilities live in
the code that renders them AND the surfaces that teach them; they drift independently unless a test
greps the actual teaching text. A vocabulary change without its teaching must fail CI here,
not surface as "add dropdowns produced plain text" in the field (2026-08-15's lesson, and
2026-08-21's table-cell incident — the ONE untaught road was this one).

These pins grep SERVER_INSTRUCTIONS + the page tools' descriptions as one teaching corpus:
what a connected agent can actually learn, wherever it happens to be written.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from june_mcp.prompts import SERVER_INSTRUCTIONS  # noqa: E402
from june_mcp.server import tool_manifest  # noqa: E402
from june_mcp.tools import _STYLE_COLORS  # noqa: E402


def _corpus() -> str:
    docs = " ".join(t.get("description", "") for t in tool_manifest())
    return SERVER_INSTRUCTIONS + "\n" + docs


def test_2026_08_21_conventions_are_taught() -> None:
    c = _corpus()
    for phrase in ("[progress:", "[date:", "[button:", "$$", "ring", "mermaid", "__june_sync__", "columns"):
        assert phrase in c, f"agents are never taught {phrase!r} — the untaught-road bug class"


def test_in_cell_pipe_escape_is_taught() -> None:
    # The exact lesson of the 2026-08-21 incident: in-table dropdown options separate with \|.
    assert "\\|" in _corpus()


def test_expanded_palette_is_taught_in_lockstep() -> None:
    """Every color the connector ACCEPTS (its _STYLE_COLORS whitelist) must be one an agent
    can DISCOVER — a color that validates but is never taught is dead vocabulary; a color
    that is taught but rejected is a lie. Derived from the whitelist, so the next palette
    expansion updates this test by construction, not by memory."""
    c = _corpus()
    untaught = sorted(color for color in _STYLE_COLORS if color not in c)
    assert untaught == [], f"colors accepted but never taught: {untaught}"


def test_iso_date_form_is_stated() -> None:
    # The chip grammar is ISO-only by design; the teaching must say so or agents will write
    # locale forms that render as inert text.
    assert "ISO" in _corpus()


def test_illustrations_and_file_refs_are_taught() -> None:
    # 2026-09-11: built-in drawings + the engine's own image refs. Names must be taught (a form
    # without names is unusable); the ref rule must say keep-verbatim / never-invent.
    c = _corpus()
    for phrase in ("[illustration:", "doro-wave", "compass", "june://files/", "never invent",
                   "align=center", "w=60%"):
        assert phrase in c, f"agents are never taught {phrase!r} — the untaught-road bug class"


def test_diagrams_and_charts_are_taught() -> None:
    """2026-09-11: the page had rendered mermaid diagrams AND charts for a long time, and the
    connector's vocabulary never mentioned either — so a connected agent writing a quarterly
    summary had no way to know a breakdown could be a pie chart rather than a paragraph. The
    chart types are the load-bearing half: an agent that knows only 'flowchart' writes prose."""
    c = _corpus()
    for phrase in ("mermaid", "flowchart TD", "pie title", "xychart-beta", "quadrantChart",
                   "sankey-beta", "never invent numbers"):
        assert phrase in c, f"agents are never taught {phrase!r} — the untaught-road bug class"
