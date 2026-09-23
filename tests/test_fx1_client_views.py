"""FX1 · N4 — a canvas VIEW of the client keeps every configured field.

The bug (june-mcp canvas, page 48145480, finding N4): ``for_canvas`` rebuilt the client from a
hand-written argument list that forgot ``write_timeout``. The connector's SERVING client is itself
such a view (``__main__._serve`` resolves the canvas and calls ``for_canvas``), so the 0.5.0 write
budgets never reached a running connector: every write ran on the 15 s read timeout.

The fix makes the view a shallow copy with an explicit, reasoned list of what a view must NOT
inherit (``_VIEW_RESETS``). These tests pin both halves: everything else is carried, and the
serving path really has its write budget.
"""
from __future__ import annotations

import httpx
import pytest

from june_client import JuneClient
from june_mcp.runtime import DEFAULT_TIMEOUT_WRITE, McpConfig, make_client


def _configured() -> JuneClient:
    return JuneClient("http://june.test", "k", canvas="c0", answer_timeout=120.0,
                      write_timeout=85.0, llm_key="lk", llm_model="lm",
                      extra_headers={"X-June-Source": "mcp"})


def test_a_view_carries_every_attribute_except_the_declared_resets():
    base = _configured()
    base.last_receipt = {"id": "r_1"}
    view = base.for_canvas("c1")
    differ = {k for k in vars(base) if vars(base)[k] != vars(view).get(k)}
    assert differ <= set(JuneClient._VIEW_RESETS), f"undeclared differences: {differ}"
    assert set(vars(view)) == set(vars(base)), "a view gained or lost an attribute"


def test_the_declared_resets_do_what_they_say():
    base = _configured()
    base.last_receipt = {"id": "r_1"}
    view = base.for_canvas("c1")
    assert view.canvas == "c1" and base.canvas == "c0"
    assert view._owns_client is False
    assert view.last_receipt is None and base.last_receipt == {"id": "r_1"}
    view.extra_headers["X-Extra"] = "view-only"
    assert "X-Extra" not in base.extra_headers, "views must not share a mutable header dict"
    assert view._client is base._client, "a view shares the transport (cheap, never closes it)"


def test_a_view_of_a_view_still_carries_the_write_budget():
    view = _configured().for_canvas("c1").for_canvas("c2")
    assert view.write_timeout == 85.0 and view.canvas == "c2"


def test_closing_a_view_never_closes_the_shared_transport():
    base = JuneClient("http://june.test", "k", canvas="c0")      # owns its transport
    view = base.for_canvas("c1")
    view.close()
    assert not base._client.is_closed


def test_the_serving_client_has_its_write_budget():
    """The production path: make_client(cfg) → for_canvas(resolved), as __main__._serve does."""
    cfg = McpConfig(base_url="http://june.test", api_key="k", canvas="some-name")
    serving = make_client(cfg).for_canvas("11111111-1111-1111-1111-111111111111")
    budget = serving._wt(0)
    assert budget is not httpx.USE_CLIENT_DEFAULT, "writes fell back to the read timeout"
    assert budget == min(DEFAULT_TIMEOUT_WRITE, JuneClient.WRITE_BASE_SECONDS)
    big = serving._wt(10_000_000)
    assert big == pytest.approx(DEFAULT_TIMEOUT_WRITE), "a huge write is capped at the ceiling"
