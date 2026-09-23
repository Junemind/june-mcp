"""FX3 / S7 — june_enumerate tells the truth about completeness (FX E3, E4).

The engine now says whether a result is complete (``exhaustive``); the tool must not promise
more than that, must pass the engine's flags through untouched, and must send ``source_app``
only when the caller gives it (so older engines see exactly the payload they always did).
"""
from __future__ import annotations

import json

import pytest

httpx = pytest.importorskip("httpx")

from june_client import JuneClient  # noqa: E402
from june_mcp import tools as T  # noqa: E402


def _client(seen: list):
    def handler(req):
        seen.append(json.loads(req.content or b"{}"))
        return httpx.Response(200, json={"items": [], "returned": 0, "truncated": False,
                                         "exhaustive": False, "scan_limit_hit": True,
                                         "scanned": 20000, "order": "node_id"})
    return JuneClient("http://june.test", "k", canvas="c0", client=httpx.Client(
        base_url="http://june.test", transport=httpx.MockTransport(handler)))


def test_flags_pass_through_and_source_app_is_sent_only_when_given():
    seen: list = []
    c = _client(seen)
    out = T.run_tool("june_enumerate", c, {"regex": "Live round"})
    assert out["exhaustive"] is False and out["scan_limit_hit"] is True and out["scanned"] == 20000
    T.run_tool("june_enumerate", c, {"terms": ["x"], "source_app": "mcp"})
    assert "source_app" not in seen[0] and seen[1]["source_app"] == "mcp"


def test_the_description_states_the_absence_rule_and_no_longer_promises_every_node():
    tool = next(t for t in T.TOOLS if t.name == "june_enumerate")
    assert "exhaustive" in tool.description and "does not prove" in tool.description
    assert "return EVERY node" not in tool.description
    assert "source_app" in tool.input_schema["properties"]
