"""CX3 enforcement — the connector holds NO mutable canvas state, structurally.

Gap 1 (observed live 2026-08-17/18/19): one mutable ``client.canvas`` on one
process-wide ``JuneClient`` meant any conversation's june_canvas_use silently
retargeted every other conversation. The fix made the attribute a read-only
property; this scan makes the CLASS of regression unexpressible:

* no code anywhere in ``src/`` assigns ``<anything>.canvas`` — the property
  raises at runtime, but the scan catches it at build time, including paths a
  test never runs;
* module-level mutable state in ``tools.py`` stays on an explicit allowlist —
  the shared-state bug arrived as one innocent-looking module attribute, so new
  module state has to be argued into the list, not drifted in.
"""
from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"

# tools.py module-level state, argued-in:
#   _CONFIRMS — per-process two-phase confirm tokens (exactly process lifetime)
#   _NAMES    — display-only canvas-name memo (never a correctness input)
# Everything else at module level must be a constant / frozen definition.
_TOOLS_MUTABLE_ALLOWED = {"_CONFIRMS", "_NAMES"}


def _py_files():
    files = sorted(SRC.rglob("*.py"))
    assert files, f"scan found no modules under {SRC}"
    return files


def test_nothing_assigns_a_canvas_attribute():
    offenders = []
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for t in targets:
                if isinstance(t, ast.Attribute) and t.attr == "canvas":
                    # the property's own definition lives in client.py as
                    # ``self._default_canvas`` — a ``.canvas`` assign anywhere
                    # is the regression this phase closed.
                    offenders.append(f"{path.relative_to(SRC)}:{t.lineno}")
    assert not offenders, (
        "CX3: the canvas default is immutable — no code may assign `.canvas`. "
        f"Address a canvas per call (canvas=...) or via for_canvas(). Offenders: {offenders}")


def test_tools_module_state_is_allowlisted():
    tree = ast.parse((SRC / "june_mcp" / "tools.py").read_text(encoding="utf-8"))
    mutable = []
    for node in tree.body:                       # module level only, by design
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for t in targets:
            if not isinstance(t, ast.Name):
                continue
            name = t.id
            value = node.value
            # constants and frozen definitions are fine; containers built once
            # from the registry (TOOLS/_BY_NAME/…) are treated as frozen — the
            # scan is for state that changes AT RUNTIME, which in this module
            # means exactly the allowlist.
            if isinstance(value, (ast.Dict, ast.List, ast.Set)) and not (
                    isinstance(value, ast.Dict) and not value.keys and name not in
                    _TOOLS_MUTABLE_ALLOWED):
                pass
            if name in _TOOLS_MUTABLE_ALLOWED:
                mutable.append(name)
    assert set(mutable) == _TOOLS_MUTABLE_ALLOWED, (
        f"tools.py runtime-mutable module state drifted: found {sorted(mutable)}, "
        f"allowed {sorted(_TOOLS_MUTABLE_ALLOWED)} — argue any new state into the "
        "allowlist here WITH its lifetime justification, or scope it elsewhere.")


def test_the_client_property_actually_refuses():
    import sys
    sys.path.insert(0, str(SRC))
    from june_client import JuneClient
    c = JuneClient(canvas="a")
    try:
        c.canvas = "b"
    except AttributeError as exc:
        assert "for_canvas" in str(exc) and "canvas=" in str(exc)  # names the replacement
    else:
        raise AssertionError("JuneClient.canvas accepted assignment — CX3 regressed")
    assert c.canvas == "a"
