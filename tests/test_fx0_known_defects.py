"""FX0 — the verified connector defects, pinned as STRICT expected failures.

Source: june-mcp canvas, page "Verified findings and the structural fix plan — 2026-09-23"
(48145480-0836-4de2-af6b-501a1a7eba3d). Every test below asserts the CORRECT behaviour and is
marked ``xfail(strict=True)``:

* today each one fails, which proves the defect is real in this tree (not inferred);
* the day a fix lands, the test XPASSes and ``strict`` turns that into a FAILURE, so the fix
  cannot ship without someone removing the marker in the same commit, deliberately.

Nothing here changes behaviour. Where a fix must also flip an existing test that pins today's
behaviour, the ``reason`` names that test, so the flip is found by reading, not by breaking.

Wave 2 (S1, 2026-09-23): the page defects G1, G3, C5, C6 and R2 are fixed where the ENGINE owns
page attributes. The in-memory engine below predates S1 on purpose — it is what an older engine
still does — so those five stay expected failures HERE, as the record of what the legacy path
cannot fix. Their fixed behaviour is pinned against an S1 engine in ``test_fx2_s1_connector.py``
and, end to end against the real engine, in june_ai ``tests/test_s1_connector_e2e.py``.
"""
from __future__ import annotations

import inspect
import json
import re
import uuid

import pytest

httpx = pytest.importorskip("httpx")

from june_client import JuneClient  # noqa: E402
from june_mcp import tools as T  # noqa: E402
from june_mcp.surfaces import build_surface, display_name, respell_guidance  # noqa: E402

STYLE = "__june_style__"
LAYOUT = "__june_layout__"


# ── an in-memory /v1/pages with the engine's save rules (same seam as test_page_compose) ──────
def _pages_handler(store: dict):
    def handler(req: "httpx.Request") -> "httpx.Response":
        path, method = req.url.path, req.method
        if method == "POST" and path == "/v1/pages":
            body = json.loads(req.content or b"{}")
            pid = str(uuid.uuid4())
            store[pid] = {"page_id": pid, "title": body.get("title") or "Untitled", "blocks": [],
                          "updated_at": "t0"}
            return httpx.Response(200, json={"page_id": pid, "title": store[pid]["title"]})
        m = re.match(r"^/v1/pages/([^/]+)/blocks$", path)
        if method == "POST" and m:
            page = store[m.group(1)]
            owned = {b["block_id"] for b in page["blocks"]}
            new = []
            for b in json.loads(req.content or b"{}").get("blocks", []):
                bid = b.get("id")
                bid = str(bid) if (bid and str(bid) in owned) else "srv-" + uuid.uuid4().hex[:10]
                new.append({"block_id": bid, "block_type": b.get("block_type", "paragraph"),
                            "text": b.get("text", ""), "order": float(b.get("order", 0.0))})
            page["blocks"] = new
            page["updated_at"] = "t" + uuid.uuid4().hex[:6]
            return httpx.Response(200, json={"page_id": page["page_id"], "title": page["title"],
                                             "blocks": new, "updated_at": page["updated_at"]})
        m = re.match(r"^/v1/pages/([^/]+)$", path)
        if method == "GET" and m:
            page = store[m.group(1)]
            return httpx.Response(200, json={"page_id": page["page_id"], "title": page["title"],
                                             "blocks": page["blocks"],
                                             "updated_at": page["updated_at"]})
        return httpx.Response(404, json={"detail": f"unhandled {method} {path}"})
    return handler


def _client(store: dict) -> JuneClient:
    http = httpx.Client(base_url="http://june.test",
                        transport=httpx.MockTransport(_pages_handler(store)))
    return JuneClient("http://june.test", "june_sk_test", client=http,
                      canvas="11111111-1111-1111-1111-111111111111")


def _sentinels(page: dict, marker: str) -> list[dict]:
    return [b for b in page["blocks"] if marker in (b.get("text") or "")]


def _styled_page(store: dict) -> str:
    out = T.run_tool("june_page_create", _client(store), {
        "title": "FX0 styled", "theme": "red", "icon": "🧪", "cover": "ember",
        "blocks": [{"type": "callout", "text": "one", "variant": "warning"},
                   {"type": "paragraph", "text": "two"}]})
    return out["page_id"]


# ── G1 · a styled round trip mints a second style sentinel, and the stale one wins ──────────
@pytest.mark.xfail(strict=True, reason="FX G1 on a PRE-S1 engine (this fake). Fixed by the S1 engine for every connector "
                   "version — proven against the real engine in june_ai tests/test_s1_connector_e2e.py")
def test_G1_styled_round_trip_keeps_exactly_one_style_sentinel_carrying_the_new_style():
    store: dict = {}
    pid = _styled_page(store)
    c = _client(store)
    page = T.run_tool("june_page_get", c, {"page_id": pid})
    carried = [{"id": b["block_id"], "type": b["block_type"], "text": b["text"]}
               for b in page["blocks"]]
    T.run_tool("june_page_write", c, {"page_id": pid, "blocks": carried, "theme": "green"})
    sents = _sentinels(store[pid], STYLE)
    assert len(sents) == 1, f"{len(sents)} style sentinels on the page"
    first = json.loads(sorted(sents, key=lambda b: b["order"])[0]["text"])
    assert first["page"].get("accent") == "green", "the reader's (first) sentinel is stale"


# ── G3 · page_get hands the agent the hidden sentinel as an ordinary block ──────────────────
@pytest.mark.xfail(strict=True, reason="FX G3 on a PRE-S1 engine (this fake has no /view). Fixed where the engine advertises "
                   "`view`: test_fx2_s1_connector.py + june_ai tests/test_s1_connector_e2e.py")
def test_G3_page_get_returns_no_sentinel_blocks():
    store: dict = {}
    pid = _styled_page(store)
    page = T.run_tool("june_page_get", _client(store), {"page_id": pid})
    leaked = [b for b in page["blocks"] if STYLE in b["text"] or LAYOUT in b["text"]]
    assert leaked == []


# ── C5 · page-level style lands but the receipt says nothing was styled ─────────────────────
@pytest.mark.xfail(strict=True, reason="FX C5 on a PRE-S1 engine. Fixed where the engine advertises `attrs` (receipt = attrs)")
def test_C5_page_level_style_is_reported_in_the_receipt():
    store: dict = {}
    out = T.run_tool("june_page_create", _client(store), {
        "title": "masthead only", "theme": "sky", "icon": "📌", "cover": "ocean",
        "blocks": [{"type": "paragraph", "text": "x"}]})
    assert _sentinels(next(iter(store.values())), STYLE), "precondition: a style sentinel was written"
    assert set(out["layout"].get("page_style_keys", [])) >= {"accent", "icon", "cover"}


# ── C6 · sentinels are counted as content by the write guard ────────────────────────────────
@pytest.mark.xfail(strict=True, reason="FX C6 on a PRE-S1 engine. Fixed where the engine advertises `view` (guard reads content)")
def test_C6_blocks_before_counts_content_only():
    store: dict = {}
    pid = _styled_page(store)
    c = _client(store)
    page = T.run_tool("june_page_get", c, {"page_id": pid})
    content = [{"id": b["block_id"], "type": b["block_type"], "text": b["text"]}
               for b in page["blocks"] if STYLE not in b["text"]]
    out = T.run_tool("june_page_write", c, {"page_id": pid, "blocks": content})
    assert out["blocks_before"] == 2


# ── R2 · doc columns are reported as a canvas ───────────────────────────────────────────────
@pytest.mark.xfail(strict=True, reason="FX R2 on a PRE-S1 engine. Fixed where the engine advertises `attrs` (receipt = attrs)")
def test_R2_doc_columns_receipt_says_doc():
    store: dict = {}
    out = T.run_tool("june_page_create", _client(store), {
        "title": "columns", "layout": {"columns": [[0, 1, 2]]},
        "blocks": [{"type": "paragraph", "text": t} for t in ("a", "b", "c")]})
    stored = json.loads(_sentinels(next(iter(store.values())), LAYOUT)[0]["text"])
    assert stored.get("mode") == "doc", "precondition: the stored layout is a doc"
    assert out["layout"]["mode"] == stored["mode"]


# ── N4 · for_canvas() drops write_timeout (and any field it forgets to copy) ────────────────
# FX N4 — FIXED in Wave 1 (for_canvas is a shallow copy with declared resets; see
# tests/test_fx1_client_views.py). Kept as the regression it was written as.
def test_N4_for_canvas_keeps_every_configured_field():
    base = JuneClient("http://june.test", "k", canvas="c0", answer_timeout=120.0,
                      write_timeout=85.0, llm_key="lk", llm_model="lm",
                      extra_headers={"X-June-Source": "mcp"})
    view = base.for_canvas("c1")
    params = [p for p in inspect.signature(JuneClient.__init__).parameters
              if p not in ("self", "base_url", "client", "timeout", "canvas")]
    lost = {p: (getattr(base, p, None), getattr(view, p, None)) for p in params
            if getattr(base, p, None) != getattr(view, p, None)}
    assert lost == {}, f"fields the view dropped: {lost}"
    assert view.canvas == "c1"


# ── N5 · a failed pre-read turns page_write into an unguarded force save ────────────────────
class _Blind:
    canvas = "test-canvas"                  # run_tool stamps write results with it

    def __init__(self):
        self.saves: list[dict] = []

    def get_page(self, page_id):  # noqa: ANN001
        raise httpx.ReadTimeout("simulated transient read failure")

    def save_blocks(self, page_id, blocks, *, expected_updated_at=None, force=False):  # noqa: ANN001
        self.saves.append({"force": force, "n": len(blocks)})
        return {"page_id": page_id, "blocks": [{"block_id": f"b{i}", "order": i + 1.0}
                                               for i in range(len(blocks))]}


# FX N5 — FIXED in Wave 1 (D5: refuse unless force). Kept as the regression it was written as.
def test_N5_page_write_refuses_when_its_pre_read_fails():
    c = _Blind()
    out = T.run_tool("june_page_write", c, {"page_id": "p1",
                                            "blocks": [{"type": "paragraph", "text": "x"}]})
    assert c.saves == [], f"saved without a guard: {c.saves}"
    assert out.get("refused"), "a refusal is a result carrying `refused` (N2 rule), not an error"


# ── N6 · an update without text blanks the block ────────────────────────────────────────────
# FX N6 — FIXED in Wave 5 (S8: the SDK sends `text` only when given; the engine keeps it). Kept as
# the regression it was written as.
def test_N6_update_without_text_does_not_send_an_empty_text():
    seen: list[dict] = []

    def handler(req):
        seen.append(json.loads(req.content or b"{}"))
        return httpx.Response(200, json={"page_id": "p", "blocks_updated": 1})

    c = JuneClient("http://june.test", "k", canvas="c0",
                   client=httpx.Client(base_url="http://june.test",
                                       transport=httpx.MockTransport(handler)))
    c.update_blocks("p", [{"id": "b1", "block_type": "heading_1"}], force=True)
    assert "text" not in seen[0]["blocks"][0]


# ── N7 · posture counts member tools while the host is served the folded surface ────────────
# FX N7 — FIXED in Wave 3 (S5: posture counts build_surface). Kept as the regression it was written as.
@pytest.mark.parametrize("readonly,pro", [(False, True), (False, False), (True, True)])
def test_N7_posture_counts_what_tools_list_serves(readonly, pro):
    posture = T._posture(readonly=readonly, pro=pro, profile="compact", absent=frozenset())
    served = build_surface("compact", readonly=readonly, pro=pro, absent=frozenset())
    assert posture["tools_advertised"] == len(served)


# ── N8 · the compact rename pass rewrites the alias map's left side ─────────────────────────
# FX N8 — FIXED in Wave 3 (S5: tool_aliases is not guidance). Kept as the regression it was written as.
def test_N8_alias_map_keeps_the_old_member_names_it_translates():
    from june_mcp.surfaces import alias_lines
    surface = build_surface("compact")
    digest = {"tool_aliases": "this connection uses the compact surface: " + alias_lines(surface)}
    out = respell_guidance(digest, display_name(surface))
    assert "june_page_get" in out["tool_aliases"], out["tool_aliases"][:200]
