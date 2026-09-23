"""FX3 / S5 — messages state only what is known (fix plan page 48145480).

The Incidents page's LYING-PROBE class: a message that names one cause it cannot know. These
tests pin the replacement — one module (explain.py) that states facts in hand and lists
possibilities as possibilities — and the specific repairs: G4, L4, L9 (in test_canvas_tools),
N7/N8 (FX0 regressions), N9, N10, N11, and the june_usage beacon.
"""
from __future__ import annotations

import ast
import json
import pathlib
import re

import pytest

httpx = pytest.importorskip("httpx")

from june_client import JuneClient  # noqa: E402
from june_mcp import explain as X  # noqa: E402
from june_mcp import tools as T  # noqa: E402
from june_mcp.capabilities import _probe_pages  # noqa: E402
from june_mcp.runtime import map_error  # noqa: E402

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "june_mcp"
PAGE = "11111111-2222-3333-4444-555555555555"
CANVAS = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def _client(handler) -> JuneClient:
    return JuneClient("http://june.test", "june_sk_secretvalue", canvas=CANVAS, write_timeout=42.0,
                      client=httpx.Client(base_url="http://june.test",
                                          transport=httpx.MockTransport(handler)))


def _failure(name: str, handler, args: dict) -> str:
    with pytest.raises(Exception) as ei:
        T.run_tool(name, _client(handler), args)
    return map_error(ei.value)


# ── the class: no file but explain.py composes a causal claim ────────────────────────────────
# Phrases that assert a cause. Legitimate only (a) inside explain.py, (b) as an item passed to
# explain.possible(...), which renders them as possibilities, or (c) with a stated proof below.
_CAUSAL = re.compile(r"\bvanish|\bmay be busy\b|\bis busy\b|\b(engine|service|server) (was |has been |has )?"
                     r"restart|\breceipts are off\b|\bis off on\b", re.IGNORECASE)
PROVEN: dict[tuple[str, str], str] = {
    ("tools.py", "its health beacon says so"): "the beacon answered enabled=false — known, not guessed",
}


def _literals_outside_possible(tree: ast.AST):
    skip: set[int] = set()
    for node in ast.walk(tree):
        fn = node.func if isinstance(node, ast.Call) else None
        if fn is not None and ((isinstance(fn, ast.Attribute) and fn.attr == "possible")
                               or (isinstance(fn, ast.Name) and fn.id == "possible")):
            for sub in ast.walk(node):
                skip.add(id(sub))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            yield node


def _docstring_lines(tree: ast.AST) -> set[int]:
    """Docstrings narrate history ("jobs vanished on restart" in an incident note); only strings
    that can reach an agent are held to the rule."""
    out: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            if n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant):
                out.add(n.body[0].value.lineno)
    return out


def test_no_file_but_explain_composes_a_causal_claim():
    offenders = []
    for path in sorted(SRC.glob("*.py")):
        if path.name == "explain.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docs = _docstring_lines(tree)
        for node in _literals_outside_possible(tree):
            if node.lineno in docs or not _CAUSAL.search(node.value):
                continue
            if any(path.name == f and sub in node.value for (f, sub) in PROVEN):
                continue
            offenders.append(f"{path.name}:{node.lineno}: {node.value[:90]!r}")
    assert not offenders, "causal claims outside explain.py:\n" + "\n".join(offenders)


def test_the_scan_catches_what_it_is_for():
    """A gate tested only on clean input is untested: the old G4 and timeout texts trip it."""
    for old in ("no such job on this engine — jobs live in engine memory and vanish on restart.",
                "June request timed out — the service may be busy or the question very broad."):
        assert _CAUSAL.search(old)
    tree = ast.parse("possible(['the engine was restarted since'])")
    assert list(_literals_outside_possible(tree)) == []


# ── explain.py itself ─────────────────────────────────────────────────────────────────────────
def test_possible_lists_possibilities_in_order():
    assert X.possible(["a"]) == "possible cause: a"
    assert X.possible(["a", "b", "c"]) == "possible causes: a; b; or c"
    assert X.possible([]) == ""


def test_engine_reason_is_cleaned_and_bounded():
    r = httpx.Response(403, json={"detail": "enrich needs Pro. key june_sk_abc123 at https://x.y/z " + "w" * 400})
    reason = X.engine_reason(r)
    assert "june_sk_abc123" not in reason and "https://" not in reason and "<redacted>" in reason
    assert len(reason) <= 301
    assert X.engine_reason(httpx.Response(404, json={"detail": "Not Found"})) == ""
    assert X.engine_reason(httpx.Response(500, text="<html>")) == ""
    v = httpx.Response(422, json={"detail": [{"loc": ["body", "cap"], "msg": "too big"}]})
    assert X.engine_reason(v) == "cap: too big"


def _timeout(cls, budgets):
    req = httpx.Request("POST", "http://june.test/v1/x", extensions={"timeout": budgets})
    return cls("t", request=req)


@pytest.mark.parametrize("cls,phase", [(httpx.ConnectTimeout, "connect"), (httpx.ReadTimeout, "read"),
                                       (httpx.WriteTimeout, "write"), (httpx.PoolTimeout, "pool")])
def test_timeout_names_its_phase_and_budget(cls, phase):
    msg = X.timeout("june_page_append", _timeout(cls, {phase: 42.0}), writes=True)
    assert f"{phase} phase (42 s budget)" in msg and "busy" not in msg


def test_a_write_that_timed_out_reading_may_have_been_applied():
    msg = X.timeout("june_page_append", _timeout(httpx.ReadTimeout, {"read": 42.0}), writes=True,
                    read_call="june_page_get(page_id='p')")
    assert "may have APPLIED" in msg and "june_page_get(page_id='p')" in msg and "twice" in msg
    msg = X.timeout("june_page_append", _timeout(httpx.ConnectTimeout, {"connect": 5.0}), writes=True)
    assert "was not applied" in msg and "retrying is safe" in msg
    msg = X.timeout("june_search", _timeout(httpx.ReadTimeout, {"read": 15.0}), writes=False)
    assert "safe to retry" in msg and "APPLIED" not in msg


def test_call_carries_the_canvas():
    assert X.call("june_page_list", CANVAS) == f"june_page_list(canvas='{CANVAS}')"
    assert X.call("june_page_list", "home") == "june_page_list()"


# ── the chokepoint: run_tool attaches the explanation; map_error renders it ──────────────────
def test_L4_page_get_404_names_the_page_the_canvas_and_the_next_call():
    def h(req):
        if req.url.path == "/v1/pages/health":
            return httpx.Response(404, json={"detail": "Not Found"})
        return httpx.Response(404, json={"detail": "no such page in your workspace"})
    msg = _failure("june_page_get", h, {"page_id": PAGE})
    assert "no such page in your workspace" in msg                      # the engine's own reason
    assert f"june_page_list(canvas='{CANVAS}')" in msg
    assert "unknown route or node" not in msg


def test_N10_the_engine_reason_outranks_the_status_hint():
    msg = _failure("june_enrich", lambda r: httpx.Response(403, json={"detail": "enrichment needs June Pro"}), {})
    assert "enrichment needs June Pro" in msg


def test_N11_a_write_that_timed_out_says_check_before_retrying():
    def h(req):
        if req.url.path == "/v1/pages/health":
            return httpx.Response(200, json={"features": []})
        raise httpx.ReadTimeout("slow", request=req)
    msg = _failure("june_page_append", h, {"page_id": PAGE, "blocks": [{"type": "paragraph", "text": "x"}]})
    # the budget named is the one the request actually ran with (the SDK scales write budgets),
    # and (S6) the variable that sets that class's ceiling
    assert re.search(r"read phase \([\d.]+ s budget, set by JUNE_TIMEOUT_WRITE\)", msg)
    assert "may have APPLIED" in msg
    assert f"june_page_get(page_id='{PAGE}', canvas='{CANVAS}')" in msg
    assert "june_sk_secretvalue" not in msg


def test_a_404_without_a_reason_lists_possibilities_not_a_cause():
    msg = _failure("june_search", lambda r: httpx.Response(404, json={"detail": "Not Found"}), {"query": "x"})
    assert "possible causes:" in msg and "this engine does not serve that route" in msg


# ── specific repairs ────────────────────────────────────────────────────────────────────────
def test_G4_an_unknown_job_names_possibilities():
    out = T.run_tool("june_remember", _client(lambda r: httpx.Response(404, json={"detail": "x"})),
                     {"job_id": "job-123"})
    assert out["state"] == "unknown"
    assert "possible causes:" in out["note"] and "mistyped" in out["note"]
    assert "vanish" not in out["note"]


def test_usage_says_unknown_when_the_beacon_cannot_be_read():
    def h(req):
        if req.url.path == "/v1/usage/health":
            return httpx.Response(503, json={"detail": "x"})
        return httpx.Response(404, json={"detail": "x"})
    out = T.run_tool("june_usage", _client(h), {})
    assert out["enabled"] is None and "could not tell" in out["note"]


def test_usage_without_a_beacon_lists_both_possibilities():
    out = T.run_tool("june_usage", _client(lambda r: httpx.Response(404, json={"detail": "Not Found"})), {})
    assert out["enabled"] is False and "predates receipts" in out["note"] and "JUNE_USAGE" in out["note"]


class _Pages:
    def __init__(self, resp):
        self.resp = resp

    def list_pages(self, limit=1):
        raise httpx.HTTPStatusError("x", request=httpx.Request("GET", "http://j/v1/pages"),
                                    response=self.resp)


def test_N9_a_stale_canvas_is_not_an_engine_without_pages():
    stale = httpx.Response(404, json={"detail": "canvas not found"},
                           headers={"X-June-Error": "canvas_not_found"})
    assert _probe_pages(_Pages(stale)) is None
    old_engine_stale = httpx.Response(404, json={"detail": "canvas not found"})
    assert _probe_pages(_Pages(old_engine_stale)) is None
    no_route = httpx.Response(404, json={"detail": "Not Found"})
    assert _probe_pages(_Pages(no_route)) is False


def test_the_explanation_never_replaces_the_exception_type():
    with pytest.raises(httpx.HTTPStatusError) as ei:
        T.run_tool("june_search", _client(lambda r: httpx.Response(500, json={"detail": "boom"})),
                   {"query": "x"})
    assert "boom" in getattr(ei.value, "june_explained", "")
    assert json.dumps(map_error(ei.value))                      # renders as plain text


def test_L5_a_zero_summary_says_what_it_covered_and_scope_all_is_sent():
    seen: list = []

    def h(req):
        seen.append(dict(req.url.params))
        return httpx.Response(200, json={"window": "week", "calls": 0,
                                         "scope": req.url.params.get("scope", "canvas")})
    c = _client(h)
    out = T.run_tool("june_usage", c, {})
    assert "this canvas only" in out["note"] and "scope='all'" in out["note"]
    assert "scope" not in seen[0]
    out = T.run_tool("june_usage", c, {"scope": "all"})
    assert seen[1]["scope"] == "all" and "every canvas you own" in out["note"]
    with pytest.raises(Exception):
        T.run_tool("june_usage", c, {"scope": "everything"})
    busy = T.run_tool("june_usage", _client(lambda r: httpx.Response(200, json={"calls": 3})), {})
    assert "note" not in busy
