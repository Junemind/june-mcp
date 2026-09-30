"""FX9 (live report 9f5af9e7, 2026-09-29): what a save's receipt and june_usage must not hide.

#4: a job result kept five allow-listed keys, so the engine's `hosted_chunks_failed` /
`hosted_last_error` (WHY extraction was degraded: 20 of 20 chunks, HTTP 401) never reached the
agent. #9: a result collected later by job id echoed `format: ""` / `source_app: ""`. #5: while
running, only a stage fraction was shown. #3: june_usage said "calls: 0" through an outage.
"""
from __future__ import annotations

import httpx

from june_client import JuneClient
from june_mcp import tools as T

DONE = {"job_id": "j1", "state": "done", "stage": "done", "pct": 1.0, "detail": "",
        "nodes_written": 24, "edges_written": 23, "engine": "hosted", "hosted_degraded": True,
        "hosted_chunks_total": 20, "hosted_chunks_failed": 20, "hosted_auth_retries": 1,
        "hosted_auth_retries_recovered": 0, "hosted_last_error": "HTTP 401",
        "superseded": [], "supersedes_from": None,
        "created": {"node_ids": ["a"], "updated_node_ids": [], "edge_ids": ["e"]}}


def _client(handler) -> JuneClient:
    return JuneClient("http://june.test", "june_sk_t",
                      client=httpx.Client(base_url="http://june.test", transport=httpx.MockTransport(handler)))


def test_a_finished_job_passes_on_everything_the_engine_said_minus_bookkeeping():
    out = T._job_result({**DONE, "format": "markdown", "source_app": "cowork"})
    assert out["hosted_chunks_failed"] == 20 and out["hosted_last_error"] == "HTTP 401"
    assert out["format"] == "markdown" and out["source_app"] == "cowork"
    assert out["state"] == "done" and out["job_id"] == "j1"
    assert not {"stage", "pct", "detail"} & set(out)


def test_a_value_nobody_knows_is_left_out_never_echoed_blank():
    out = T._job_result(DONE)                                    # an engine from before FX9
    assert "format" not in out and "source_app" not in out
    assert T._job_result(DONE, "markdown", "mcp")["format"] == "markdown"   # the call knew it


def test_collect_by_job_id_on_a_new_engine_says_what_was_asked():
    def handler(req):
        return httpx.Response(200, json={**DONE, "format": "markdown", "source_app": "cowork"})
    out = T._remember(_client(handler), {"job_id": "j1"})
    assert out["format"] == "markdown" and out["source_app"] == "cowork"
    assert out["hosted_chunks_failed"] == 20


def test_a_running_job_shows_how_far_extraction_got():
    def handler(req):
        return httpx.Response(200, json={"job_id": "j1", "state": "running", "stage": "extracting",
                                         "pct": 0.3, "hosted_chunks_attempted": 9,
                                         "hosted_chunks_failed": 9,
                                         "progress_basis": "pct is the stage reached"})
    out = T._remember(_client(handler), {"job_id": "j1"})
    assert out["state"] == "running" and out["hosted_chunks_attempted"] == 9
    assert out["hosted_chunks_failed"] == 9
    # 0.0.14 live round: the engine's explanation of `pct` was dropped by the connector.
    assert out["progress_basis"] == "pct is the stage reached"


def test_usage_says_calls_failed_even_when_none_succeeded():
    def handler(req):
        return httpx.Response(200, json={"calls": 0, "scope": "canvas", "failed_calls_total": 7,
                                         "failed_calls_since_start": {"/v1/search": 5, "/v1/context": 2},
                                         "last_failure": {"route": "/v1/context", "error_class": "error",
                                                          "request_id": "abc123def456"}})
    out = T._usage(_client(handler), {"window": "day"})
    assert "7 call(s) FAILED" in out["failures"] and "abc123def456" in out["failures"]
    assert "no receipted calls" in out["note"]


def test_usage_without_failures_is_unchanged():
    def handler(req):
        return httpx.Response(200, json={"calls": 3, "scope": "canvas", "failed_calls_total": 0})
    assert "failures" not in T._usage(_client(handler), {"window": "day"})


def test_an_engine_crash_reaches_the_agent_with_its_request_id():
    """#2: the engine's FX9 envelope (june_service.error_envelope) rides the existing N10 path."""
    from june_mcp.explain import http_failure
    body = {"detail": "request_id 0123456789ab: /v1/search failed at "
                      "june_service/search_route.py:search_route: error. the engine's program files "
                      "were replaced while it was running (a rebuild or an update): restart June",
            "request_id": "0123456789ab"}
    req = httpx.Request("POST", "http://june.test/v1/search")
    resp = httpx.Response(500, json=body, request=req)
    msg = http_failure("june_search", httpx.HTTPStatusError("x", request=req, response=resp), writes=False)
    assert "0123456789ab" in msg and "restart June" in msg and "search_route" in msg
