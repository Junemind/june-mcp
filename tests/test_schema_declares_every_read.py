"""N7 (0.4.2): every argument a handler READS is DECLARED in its schema, and every array has `items`.

The engine repo's lockstep gate (tests/architecture/test_vocabulary_is_taught.py) matches
``a.get("x")`` with a regex, so reads through ``_clamp(a, "x", …)`` were invisible to it — which
is how ``max_subqueries`` shipped undeclared. This gate parses the handler AST instead: any
``<argdict>.get("x")``, ``<argdict>["x"]``, ``"x" in <argdict>`` or ``_clamp(<argdict>, "x", …)``
counts as a read, where ``<argdict>`` is the handler's second parameter.
"""
from __future__ import annotations

import ast
import pathlib

from june_mcp.tools import TOOLS

TOOLS_PY = pathlib.Path(__file__).resolve().parents[1] / "src" / "june_mcp" / "tools.py"
EXEMPT = {"canvas", "force", "expected_updated_at", "expected_revision"}
# Read-but-undeclared arguments that are deliberate, each with the reason. Anything else fails.
# (The engine repo's regex gate also exempted seven june_page_import frontmatter fields: those were
# false positives — `meta.get("kind")` matched its `a\.get\("` pattern. The AST sees only `path`.)
DECLARED_SCHEMA_GAPS: dict[tuple[str, str], str] = {
    ("june_page_write", "accent"): "an undeclared ALIAS for the declared `theme` argument (the handler "
                                   "reads `theme or accent`), reachable through the documented name",
    ("june_page_create", "accent"): "the same `theme` alias on page_create (found by the 2026-09-16 "
                                    "second pass over the source)",
    ("june_context", "mode"): "pre-existing before the gate (2026-09-12); raise it deliberately, with "
                              "its own teaching, rather than widening a built release",
    ("june_resolve", "limit"): "not an argument: the handler only ACKNOWLEDGES a client-sent limit "
                               "with a note (resolution is server-bounded); declaring it would invite it",
}


def _reads_by_handler() -> dict[str, set[str]]:
    tree = ast.parse(TOOLS_PY.read_text(encoding="utf-8"))
    out: dict[str, set[str]] = {}
    for fn in tree.body:
        if not isinstance(fn, ast.FunctionDef) or len(fn.args.args) < 2:
            continue
        arg = fn.args.args[1].arg
        keys: set[str] = set()
        for node in ast.walk(fn):
            # a.get("x") / a.pop("x")
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == arg
                    and node.func.attr in ("get", "pop") and node.args
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                keys.add(node.args[0].value)
            # a["x"]
            if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                    and node.value.id == arg and isinstance(node.slice, ast.Constant)
                    and isinstance(node.slice.value, str)):
                keys.add(node.slice.value)
            # "x" in a
            if (isinstance(node, ast.Compare) and isinstance(node.left, ast.Constant)
                    and isinstance(node.left.value, str) and node.comparators
                    and isinstance(node.comparators[0], ast.Name) and node.comparators[0].id == arg
                    and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops)):
                keys.add(node.left.value)
            # _clamp(a, "x", …) and any helper called as f(a, "x", …)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and len(node.args) >= 2 and isinstance(node.args[0], ast.Name)
                    and node.args[0].id == arg and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str) and node.func.id.startswith("_clamp")):
                keys.add(node.args[1].value)
        if keys:
            out[fn.name] = keys
    assert out, "the AST read-extractor found nothing — it has stopped reading tools.py"
    return out


def _walk_arrays(schema: dict, path: str = "") -> list[str]:
    missing = []
    if not isinstance(schema, dict):
        return missing
    if schema.get("type") == "array" and "items" not in schema:
        missing.append(path)
    for k, v in (schema.get("properties") or {}).items():
        missing += _walk_arrays(v, f"{path}.{k}")
    if isinstance(schema.get("items"), dict):
        missing += _walk_arrays(schema["items"], path + "[]")
    return missing


def test_clamp_reads_are_visible_to_this_gate() -> None:
    reads = _reads_by_handler()
    assert "max_subqueries" in reads.get("_answer", set()), "the _clamp(a, …) read is not being seen"


def test_every_argument_a_tool_reads_is_declared() -> None:
    reads = _reads_by_handler()
    undeclared = []
    for t in TOOLS:
        handler = t.handler.__name__
        props = set((t.input_schema.get("properties") or {}).keys())
        for arg in sorted(reads.get(handler, set()) - props - EXEMPT):
            if (t.name, arg) in DECLARED_SCHEMA_GAPS:
                continue
            undeclared.append(f"  {t.name} ({handler}) reads {arg!r} and never declares it")
    assert not undeclared, ("a model composes arguments from the schema; these are unreachable:\n"
                            + "\n".join(undeclared))


def test_every_declared_gap_is_still_real() -> None:
    reads = _reads_by_handler()
    by_name = {t.name: t for t in TOOLS}
    for (tool, arg), reason in DECLARED_SCHEMA_GAPS.items():
        assert tool in by_name, tool
        assert len(reason) > 40, (tool, arg)
        assert arg in reads.get(by_name[tool].handler.__name__, set()), \
            f"{tool} no longer reads {arg!r} — delete the exemption rather than leaving it to rot"
        assert arg not in (by_name[tool].input_schema.get("properties") or {}), \
            f"{tool} now declares {arg!r} — the exemption is stale"


def test_every_array_declares_items() -> None:
    missing = [f"  {t.name}{p}" for t in TOOLS for p in _walk_arrays(t.input_schema)]
    assert not missing, ("Gemini's API rejects the whole tool list over any of these; models guess "
                         "element shapes for them:\n" + "\n".join(missing))


def test_direction_is_an_enum() -> None:
    nb = next(t for t in TOOLS if t.name == "june_neighborhood")
    assert nb.input_schema["properties"]["direction"]["enum"] == ["in", "out", "both"]
