"""B5 — a write must never run on the READ-verb budget.

Found live twice, the same class both times:

* 2026-09-05, a 40k-char ``june_remember`` outran the transport's 15 s read
  timeout. Fixed — for ``june_remember`` alone: an async job route, a budget
  scaled to the text, and ``state: running`` instead of an error.
* 2026-09-19, a 63-block ``june_page_append`` did exactly the same thing. The
  connector reported "timed out"; the engine had committed all 63 blocks. A
  second append, and a ``june_remember``, behaved identically.

The 2026-09-05 fix repaired the INSTANCE and left the CLASS open: every other
write verb still inherited ``timeout_read``. These scans close it, and they are
derived from the client's own source, so a write verb added next month is
covered without anyone remembering this file exists.
"""
from __future__ import annotations

import ast
import pathlib

import httpx

from june_client import JuneClient

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
CLIENT = SRC / "june_client" / "client.py"

# POSTs that carry a QUERY rather than a write. Argued in one by one, never grown
# by convenience: adding a name here removes it from the scan, so a real write
# hidden behind a read-shaped name would go unbudgeted.
READ_SHAPED_POSTS = {"search", "context", "resolve", "enumerate"}

_HTTP_VERBS = {"post", "put", "patch", "request", "delete"}


def _methods() -> dict[str, ast.FunctionDef]:
    tree = ast.parse(CLIENT.read_text(encoding="utf-8"))
    out: dict[str, ast.FunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            out[node.name] = node
    assert out, f"scan found no methods in {CLIENT}"
    return out


def _http_calls(fn: ast.FunctionDef) -> list[ast.Call]:
    calls = []
    for node in ast.walk(fn):
        f = getattr(node, "func", None)
        if (isinstance(node, ast.Call) and isinstance(f, ast.Attribute)
                and isinstance(f.value, ast.Attribute) and f.value.attr == "_client"
                and f.attr in _HTTP_VERBS):
            calls.append(node)
    return calls


def test_the_read_allowlist_names_real_methods():
    """A renamed method must not silently fall out of the scan."""
    missing = sorted(READ_SHAPED_POSTS - set(_methods()))
    assert not missing, (
        f"the read-shaped allowlist names methods that no longer exist: {missing}. "
        "Rename them here or the scan below stops covering them.")


def test_every_write_call_states_a_budget():
    offenders = []
    for name, fn in sorted(_methods().items()):
        if name.startswith("_") or name in READ_SHAPED_POSTS:
            continue
        # a method that takes its own `timeout` owns its budget explicitly
        if "timeout" in [a.arg for a in fn.args.args + fn.args.kwonlyargs]:
            continue
        for call in _http_calls(fn):
            kw = [k.arg for k in call.keywords]
            if "timeout" not in kw and None not in kw:
                offenders.append(f"{name}() line {call.lineno}")
    assert not offenders, (
        "these write calls inherit the READ-verb timeout — the 2026-09-19 class:\n  "
        + "\n  ".join(offenders)
        + "\nPass timeout=self._wt(<payload chars>) so the budget scales with the work.")


def test_a_write_budget_is_never_the_read_budget():
    """The floor alone must clear the read timeout, or the fix is cosmetic."""
    assert JuneClient.WRITE_BASE_SECONDS > 15.0


def test_wt_is_inert_when_no_write_timeout_is_configured():
    """Additive proof: an existing caller that sets nothing is unchanged."""
    c = JuneClient("http://example.invalid", "k")
    assert c.write_timeout is None
    assert c._wt() is httpx.USE_CLIENT_DEFAULT
    assert c._wt(10_000_000) is httpx.USE_CLIENT_DEFAULT


def test_the_budget_scales_with_the_payload_and_is_capped():
    c = JuneClient("http://example.invalid", "k", write_timeout=85.0)
    assert c._wt(0) == 30.0                    # floor, not the 15 s read budget
    assert c._wt(10_000) == 50.0               # 30 + 2s per 1k chars
    assert c._wt(10_000_000) == 85.0           # capped by write_timeout, never unbounded


def test_a_negative_or_absurd_size_cannot_shrink_the_floor():
    c = JuneClient("http://example.invalid", "k", write_timeout=85.0)
    assert c._wt(-5) == 30.0


def test_the_cap_can_be_lowered_below_the_floor_without_inverting():
    """A deliberately tiny ceiling wins over the floor — min(), not max()."""
    c = JuneClient("http://example.invalid", "k", write_timeout=5.0)
    assert c._wt(0) == 5.0
