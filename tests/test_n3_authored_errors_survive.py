"""N3 (0.4.2): every message the tools AUTHOR must reach the agent.

``runtime.map_error`` rebuilds agent-visible text from the exception type alone (so transport
errors can never leak URLs/keys). That is right for exceptions we did not write — and wrong for
the ones we did: recovery advice raised as a bare ``RuntimeError`` was reduced to
``June tool failed (RuntimeError)``. This scan makes the rule structural: inside a tool handler,
a ``raise`` of a type ``map_error`` redacts is a test failure, unless it is one of the authored
types (``ToolInputError``, ``ToolFailure``) or a ``KeyError`` (whose args map_error passes
through as tool/argument identifiers).
"""
from __future__ import annotations

import ast
import pathlib

from june_mcp.runtime import ToolFailure, ToolInputError, map_error

TOOLS_PY = pathlib.Path(__file__).resolve().parents[1] / "src" / "june_mcp" / "tools.py"
AUTHORED = {"ToolInputError", "ToolFailure", "KeyError"}
# Internal control-flow exceptions that a caller inside tools.py always catches (verified by the
# scan below: a listed name must appear in an `except` clause in the same file).
CAUGHT_INTERNALLY = {"StylingConflict"}
# Import-time guards (registry consistency) never reach an agent; they are not handler raises.
IMPORT_TIME_OK = {"RuntimeError"}


def _handler_raises() -> list[tuple[str, int, str]]:
    tree = ast.parse(TOOLS_PY.read_text(encoding="utf-8"))
    out = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Raise) and sub.exc is not None:
                call = sub.exc
                name = None
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                    name = call.func.id
                elif isinstance(call, ast.Name):
                    name = call.id
                if name:
                    out.append((node.name, sub.lineno, name))
    return out


def test_scanner_still_reads_tools_py() -> None:
    raises = _handler_raises()
    assert len(raises) > 20, "the raise scanner found almost nothing — it has stopped reading tools.py"


def test_internal_exceptions_are_really_caught() -> None:
    src = TOOLS_PY.read_text(encoding="utf-8")
    for name in CAUGHT_INTERNALLY:
        assert f"except {name}" in src, f"{name} is listed as caught internally but nothing catches it"


def test_no_handler_raises_a_type_map_error_redacts() -> None:
    bad = [f"  {fn}:{line} raises {name}" for fn, line, name in _handler_raises()
           if name not in AUTHORED | CAUGHT_INTERNALLY]
    assert not bad, ("these raises reach the agent as 'June tool failed (<type>)' with the advice "
                     "stripped — raise ToolInputError (bad arguments) or ToolFailure (runtime, "
                     "with recovery advice) instead:\n" + "\n".join(bad))


def test_authored_types_pass_through_verbatim() -> None:
    assert map_error(ToolInputError("needs 'page_id'")) == "needs 'page_id'"
    assert map_error(ToolFailure("send the text again — a re-send upserts")) == \
        "send the text again — a re-send upserts"
    # and the redaction rule still holds for everything else
    assert "boom-secret" not in map_error(RuntimeError("boom-secret http://x?key=1"))
