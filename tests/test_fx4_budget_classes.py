"""FX4 · S6 — every tool runs on its declared time-budget class.

The defect (june-mcp canvas, page 48145480, §4 and A8): `june_context` ran on the 15 s READ
budget while its engine path reranks every fused candidate (~950 on a large canvas); 23 measured
passes took 17.7-69.7 s, so it timed out on june-mcp and never on small canvases, and the error
told the agent to narrow a request that narrowing cannot shorten.

The fix is a class per tool (`tools._FACTS`, fifth field) with one row per class in
`runtime.BUDGET_CLASSES`; the client carries one timeout per class. These tests pin: the table
is complete and consistent; config reads the new class; the class really is the timeout on the
wire (driven through run_tool, with a distinct value per class); a view keeps it; and a timeout
names the knob and, for the retrieval class, does not send the agent to narrow the request.
"""
from __future__ import annotations

import httpx
import pytest

from june_client import JuneClient
from june_mcp import explain as X
from june_mcp import tools as T
from june_mcp.runtime import (BUDGET_CLASSES, DEFAULT_TIMEOUT_RETRIEVAL, ConfigError, McpConfig,
                              load_config, make_client)

GOOD_ENV = {"JUNE_BASE_URL": "http://june.test", "JUNE_API_KEY": "june_sk_x", "JUNE_CANVAS":
            "11111111-1111-1111-1111-111111111111"}
PAGE = "22222222-2222-2222-2222-222222222222"
NODE = "33333333-3333-3333-3333-333333333333"
CLOCK = {"fast": 11.0, "retrieval": 22.0, "answer": 33.0, "write": 44.0}


# ── the table ───────────────────────────────────────────────────────────────────────────────
def test_every_tool_has_a_known_class_and_writes_never_run_on_a_read_clock():
    for t in T.TOOLS:
        assert t.budget in BUDGET_CLASSES, t.name
        if t.writes:
            assert t.budget == "write", t.name


def test_the_classes_the_plan_names():
    by = {t.name: t.budget for t in T.TOOLS}
    assert by["june_context"] == "retrieval"
    assert by["june_answer"] == "answer"
    assert by["june_search"] == "fast"          # search reranks only with deep=true, never sent
    assert {c for c in by.values()} == set(BUDGET_CLASSES)


def test_each_class_is_one_env_var_and_one_config_field():
    envs = [e for e, _ in BUDGET_CLASSES.values()]
    assert len(set(envs)) == len(envs)
    for _, fld in BUDGET_CLASSES.values():
        assert fld in McpConfig.__dataclass_fields__


# ── config ──────────────────────────────────────────────────────────────────────────────────
def test_retrieval_budget_default_and_override():
    assert load_config(GOOD_ENV).timeout_retrieval == DEFAULT_TIMEOUT_RETRIEVAL == 90.0
    assert load_config({**GOOD_ENV, "JUNE_TIMEOUT_RETRIEVAL": "150"}).timeout_retrieval == 150.0
    for bad in ("slow", "0", "-3"):
        with pytest.raises(ConfigError):
            load_config({**GOOD_ENV, "JUNE_TIMEOUT_RETRIEVAL": bad})


def test_make_client_hands_the_client_every_class():
    cfg = McpConfig(base_url="http://june.test", api_key="k", canvas="c", timeout_read=11.0,
                    timeout_retrieval=22.0, timeout_answer=33.0, timeout_write=44.0)
    c = make_client(cfg)
    try:
        assert (c.retrieval_timeout, c.answer_timeout, c.write_timeout) == (22.0, 33.0, 44.0)
        assert c.for_canvas("x").retrieval_timeout == 22.0          # a view keeps it (N4 class)
    finally:
        c.close()


# ── the wire ────────────────────────────────────────────────────────────────────────────────
class _Recorder:
    def __init__(self):
        self.calls: list[tuple[str, str, float | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        t = (request.extensions.get("timeout") or {}).get("read")
        self.calls.append((request.method, request.url.path, t))
        return httpx.Response(200, json={})


def _client(rec: _Recorder) -> JuneClient:
    http = httpx.Client(base_url="http://june.test", transport=httpx.MockTransport(rec),
                        timeout=httpx.Timeout(connect=5.0, read=CLOCK["fast"],
                                              write=CLOCK["fast"], pool=CLOCK["fast"]))
    return JuneClient("http://june.test", "june_sk_x", client=http, canvas=GOOD_ENV["JUNE_CANVAS"],
                      retrieval_timeout=CLOCK["retrieval"], answer_timeout=CLOCK["answer"],
                      write_timeout=CLOCK["write"])


@pytest.mark.parametrize(("tool", "args", "path", "klass"), [
    ("june_context", {"query": "q"}, "/v1/context", "retrieval"),
    ("june_answer", {"query": "q"}, "/v1/answer", "answer"),
    ("june_search", {"query": "q"}, "/v1/search", "fast"),
])
def test_a_read_verb_runs_on_its_class_clock(tool, args, path, klass):
    rec = _Recorder()
    try:
        T.run_tool(tool, _client(rec), args)
    except Exception:  # noqa: BLE001 - the empty engine reply may not satisfy the formatter
        pass
    hits = [t for m, p, t in rec.calls if p == path]
    assert hits, f"{tool} never reached {path}: {rec.calls}"
    assert all(t == CLOCK[klass] for t in hits), (tool, hits)


def test_context_before_s6_ran_on_the_read_clock_and_now_does_not():
    """The characterisation: without a retrieval budget the client falls back to the transport
    default - byte-identical for any caller that does not opt in."""
    rec = _Recorder()
    http = httpx.Client(base_url="http://june.test", transport=httpx.MockTransport(rec),
                        timeout=httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=15.0))
    JuneClient("http://june.test", "k", client=http, canvas="c").context(query="q")
    assert rec.calls[-1][2] == 15.0
    JuneClient("http://june.test", "k", client=http, canvas="c", retrieval_timeout=90.0).context(query="q")
    assert rec.calls[-1][2] == 90.0
    JuneClient("http://june.test", "k", client=http, canvas="c",
               retrieval_timeout=90.0).context(query="q", timeout=7.0)
    assert rec.calls[-1][2] == 7.0


_FAST_ARGS = {
    "june_enumerate": {"terms": ["x"]},
    "june_usage": {},
    "june_neighborhood": {"node_id": NODE, "node_type": "entity"},
    "june_subgraph": {"node_id": NODE, "node_type": "entity"},
    "june_page_list": {},
    "june_page_get": {"page_id": PAGE},
    "june_canvas_list": {},
    "june_doc_list": {},
    "june_doc_get": {"name": "learnings"},
    "june_docs_refresh": {},
}


def test_the_fast_sweep_covers_every_fast_tool_that_talks_to_the_engine():
    fast = {t.name for t in T.TOOLS if t.budget == "fast"}
    no_wire = {"june_canvas_current", "june_canvas_use",       # answered from connection state
               "june_docs_export", "june_page_export"}         # need JUNE_EXPORT_ROOT + a repo
    assert fast - no_wire == set(_FAST_ARGS) | {"june_search"}, sorted(fast - no_wire)


@pytest.mark.parametrize("tool", sorted(_FAST_ARGS))
def test_every_request_of_a_fast_tool_is_on_the_fast_clock(tool):
    rec = _Recorder()
    try:
        T.run_tool(tool, _client(rec), _FAST_ARGS[tool])
    except Exception:  # noqa: BLE001
        pass
    assert rec.calls, f"{tool} made no request - the sweep would prove nothing"
    assert {t for _, _, t in rec.calls} == {CLOCK["fast"]}, (tool, rec.calls)


_WRITE_ARGS = {
    "june_page_append": {"page_id": PAGE, "blocks": [{"type": "paragraph", "text": "x"}]},
    "june_page_update": {"page_id": PAGE, "blocks": [{"id": NODE, "text": "x"}]},
    "june_page_create": {"title": "t", "blocks": [{"type": "paragraph", "text": "x"}]},
    "june_canvas_create": {"name": "fx4"},
    "june_enrich": {},
    "june_learn": {"text": "a lesson"},
    "june_doc_save": {"name": "fx4-doc", "text": "b"},
}


@pytest.mark.parametrize("tool", sorted(_WRITE_ARGS))
def test_no_mutating_request_of_a_write_tool_runs_on_the_read_clock(tool):
    rec = _Recorder()
    try:
        T.run_tool(tool, _client(rec), _WRITE_ARGS[tool])
    except Exception:  # noqa: BLE001
        pass
    muts = [(m, p, t) for m, p, t in rec.calls if m in ("POST", "PUT", "PATCH", "DELETE")]
    assert muts, f"{tool} made no mutating request - the sweep would prove nothing: {rec.calls}"
    for m, p, t in muts:
        assert t is not None and t != CLOCK["fast"] and t <= CLOCK["write"], (tool, m, p, t)


def test_page_delete_the_one_write_the_sweep_cannot_reach_through_run_tool():
    """june_page_delete refuses until the connection has READ the page (by design), so its wire
    budget is pinned at the client method it calls."""
    rec = _Recorder()
    _client(rec).delete_page(PAGE)
    assert [(m, t) for m, _, t in rec.calls] == [("DELETE", 30.0)]


# ── the message ─────────────────────────────────────────────────────────────────────────────
def _timeout(budget: float) -> httpx.ReadTimeout:
    req = httpx.Request("POST", "http://june.test/v1/context", extensions={"timeout": {"read": budget}})
    return httpx.ReadTimeout("timed out", request=req)


def test_a_retrieval_timeout_names_its_knob_and_does_not_say_narrow():
    msg = X.timeout("june_context", _timeout(90.0), writes=False, budget_class="retrieval",
                    budget_env="JUNE_TIMEOUT_RETRIEVAL")
    assert "(90 s budget, set by JUNE_TIMEOUT_RETRIEVAL)" in msg
    assert "narrow the request." not in msg and "may not shorten it" in msg
    assert "raise JUNE_TIMEOUT_RETRIEVAL" in msg and "june_search" in msg


def test_a_fast_read_keeps_its_advice_and_gains_the_knob():
    msg = X.timeout("june_search", _timeout(15.0), writes=False, budget_class="fast",
                    budget_env="JUNE_TIMEOUT_READ")
    assert "(15 s budget, set by JUNE_TIMEOUT_READ)" in msg and "narrow the request" in msg


def test_without_class_facts_the_message_is_unchanged():
    old = X.timeout("june_search", _timeout(15.0), writes=False)
    assert old == ("june_search timed out in the read phase (15 s budget): the engine did not answer "
                   "in time after receiving the request. A read is safe to retry; if it keeps timing "
                   "out, narrow the request.")


def test_run_tool_attaches_the_class_message_to_a_context_timeout():
    def boom(request):
        raise httpx.ReadTimeout("timed out", request=request)
    http = httpx.Client(base_url="http://june.test", transport=httpx.MockTransport(boom))
    c = JuneClient("http://june.test", "k", client=http, canvas=GOOD_ENV["JUNE_CANVAS"],
                   retrieval_timeout=90.0)
    with pytest.raises(httpx.ReadTimeout) as ei:
        T.run_tool("june_context", c, {"query": "q"})
    text = getattr(ei.value, "june_explained", "")
    assert "set by JUNE_TIMEOUT_RETRIEVAL" in text and "may not shorten it" in text
