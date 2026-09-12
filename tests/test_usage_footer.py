"""Usage receipts in the connector (metrics widget step 4).

Three things, all over httpx.MockTransport (no server, no network):
  * the client the server runs over identifies itself (X-June-Source: mcp), carries one
    session id per server process (X-June-Session) and asks for the receipt on the
    response (X-June-Receipt-Sync: 1) — on EVERY call, canvas views included;
  * a receipted read (june_answer / june_context / june_search) carries `receipt` +
    `receipt_footer` when the engine answered with X-June-Receipt, and nothing when it
    did not (older engine, or JUNE_USAGE off) — never an estimate, never the word "saved";
  * `june_usage` routes to /v1/usage/summary (window) or /v1/usage/receipt/{id}.
"""
from __future__ import annotations

import json
import unittest

try:
    import httpx

    from june_client import JuneClient, parse_receipt_header
    from june_mcp.runtime import MCP_SESSION_ID, McpConfig, make_client
    from june_mcp.tools import TOOLS, receipt_footer, run_tool, visible_tools
    _IMPORT_OK, _IMPORT_ERR = True, ""
except Exception as exc:  # pragma: no cover
    _IMPORT_OK, _IMPORT_ERR = False, repr(exc)

CANVAS = "11111111-1111-1111-1111-111111111111"
HDR = "id=r_abc123; tokens=812; counter=tiktoken:cl100k_base; verified=1; blocks=3; docs=2; rereads=1"


def _client(handler) -> "JuneClient":
    http = httpx.Client(base_url="http://june.test", transport=httpx.MockTransport(handler))
    return JuneClient("http://june.test", "june_sk_test", client=http, canvas=CANVAS)


def _engine(receipt: str | None):
    """A fake engine: answers every read with an empty-but-valid body, optionally with a
    receipt header, and records the requests it saw."""
    seen: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        headers = {"X-June-Receipt": receipt} if receipt else {}
        p = r.url.path
        if p == "/v1/context":
            body = {"items": [], "provenance": [], "budget": {}, "degraded": []}
        elif p == "/v1/search":
            body = {"items": [], "degraded": []}
        elif p == "/v1/answer":
            body = {"answer": "x", "sources": [], "confidence": 0.5, "abstained": False,
                    "budget": {}, "degraded": []}
        elif p == "/v1/usage/summary":
            body = {"window": r.url.params.get("window"), "calls": 2, "tokenizer": "tiktoken:cl100k_base"}
        elif p.startswith("/v1/usage/receipt/"):
            body = {"receipt_id": p.rsplit("/", 1)[-1], "served": {"tokens": 812}}
        else:
            body = {}
        return httpx.Response(200, json=body, headers=headers)
    return handler, seen


@unittest.skipUnless(_IMPORT_OK, f"june_mcp unavailable: {_IMPORT_ERR}")
class TestHeader(unittest.TestCase):
    def test_parse_receipt_header(self) -> None:
        rec = parse_receipt_header(HDR)
        self.assertEqual(rec["id"], "r_abc123")
        self.assertEqual(rec["tokens"], 812)
        self.assertEqual(rec["counter"], "tiktoken:cl100k_base")
        self.assertTrue(rec["verified"])
        self.assertEqual((rec["blocks"], rec["docs"], rec["rereads"]), (3, 2, 1))
        self.assertIsNone(parse_receipt_header(None))
        self.assertIsNone(parse_receipt_header(""))
        self.assertIsNone(parse_receipt_header("garbage without an id"))

    def test_footer_text_names_the_counter_and_never_says_saved(self) -> None:
        line = receipt_footer(parse_receipt_header(HDR))
        self.assertIn("receipt r_abc123", line)
        self.assertIn("812 tokens (exact, tiktoken:cl100k_base)", line)
        self.assertIn("3 blocks across 2 docs", line)
        self.assertIn("1 doc this session already had", line)
        self.assertIn('june_usage(receipt_id="r_abc123")', line)
        self.assertNotIn("saved", line.lower())
        unverified = receipt_footer({"id": "r_1", "tokens": 5, "counter": "unverified:chars/4",
                                     "verified": False, "blocks": 1, "docs": 1, "rereads": 0})
        self.assertIn("unverified count", unverified)
        self.assertNotIn("already had", unverified)
        self.assertIsNone(receipt_footer(None))
        self.assertIsNone(receipt_footer({}))


@unittest.skipUnless(_IMPORT_OK, f"june_mcp unavailable: {_IMPORT_ERR}")
class TestClientHeaders(unittest.TestCase):
    def test_make_client_sends_source_session_and_sync_on_every_call(self) -> None:
        handler, seen = _engine(HDR)
        cfg = McpConfig(base_url="http://june.test", api_key="june_sk_test", canvas=CANVAS)
        client = make_client(cfg)
        client._client = httpx.Client(base_url="http://june.test", transport=httpx.MockTransport(handler))
        client.context(query="q")
        client.search(query="q")
        # canvas views (for_canvas) must keep the headers — run_tool routes through them
        client.for_canvas("22222222-2222-2222-2222-222222222222").context(query="q")
        self.assertEqual(len(seen), 3)
        for r in seen:
            self.assertEqual(r.headers.get("X-June-Source"), "mcp")
            self.assertEqual(r.headers.get("X-June-Session"), MCP_SESSION_ID)
            self.assertEqual(r.headers.get("X-June-Receipt-Sync"), "1")
            self.assertEqual(r.headers.get("X-API-Key"), "june_sk_test")
        self.assertTrue(MCP_SESSION_ID.startswith("mcp-"))
        self.assertEqual(client.last_receipt["id"], "r_abc123")

    def test_last_receipt_tracks_the_most_recent_response(self) -> None:
        calls = {"n": 0}

        def handler(r: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            hdr = {"X-June-Receipt": HDR} if calls["n"] == 1 else {}
            return httpx.Response(200, json={"items": [], "degraded": []}, headers=hdr)
        client = _client(handler)
        client.search(query="a")
        self.assertEqual(client.last_receipt["id"], "r_abc123")
        client.search(query="b")
        self.assertIsNone(client.last_receipt)   # a receipt never outlives its own call


@unittest.skipUnless(_IMPORT_OK, f"june_mcp unavailable: {_IMPORT_ERR}")
class TestFooter(unittest.TestCase):
    def test_receipted_reads_carry_footer_when_engine_sends_one(self) -> None:
        handler, _ = _engine(HDR)
        client = _client(handler)
        for name, args in (("june_context", {"query": "q"}), ("june_search", {"query": "q"}),
                           ("june_answer", {"question": "q"})):
            out = run_tool(name, client, args)
            self.assertIsInstance(out, dict, name)
            self.assertEqual(out["receipt"]["id"], "r_abc123", name)
            self.assertEqual(out["receipt"]["tokens"], 812, name)
            self.assertIn("receipt r_abc123", out["receipt_footer"], name)
            self.assertNotIn("saved", json.dumps(out["receipt_footer"]).lower(), name)

    def test_no_header_no_footer(self) -> None:
        handler, _ = _engine(None)
        client = _client(handler)
        for name, args in (("june_context", {"query": "q"}), ("june_search", {"query": "q"}),
                           ("june_answer", {"question": "q"})):
            out = run_tool(name, client, args)
            self.assertNotIn("receipt", out, name)
            self.assertNotIn("receipt_footer", out, name)

    def test_non_receipted_reads_never_get_a_footer(self) -> None:
        # a stale receipt on the client (from an earlier read) must not leak onto other tools
        handler, _ = _engine(HDR)
        client = _client(handler)
        run_tool("june_search", client, {"query": "q"})
        self.assertIsNotNone(client.last_receipt)
        out = run_tool("june_neighborhood", client,
                       {"node_id": "33333333-3333-3333-3333-333333333333", "node_type": "entity"})
        self.assertNotIn("receipt_footer", out)


@unittest.skipUnless(_IMPORT_OK, f"june_mcp unavailable: {_IMPORT_ERR}")
class TestUsageTool(unittest.TestCase):
    def test_summary_and_receipt_routes(self) -> None:
        handler, seen = _engine(None)
        client = _client(handler)
        out = run_tool("june_usage", client, {})
        self.assertEqual(out["window"], "week")           # default window
        out = run_tool("june_usage", client, {"window": "day"})
        self.assertEqual(out["window"], "day")
        out = run_tool("june_usage", client, {"receipt_id": "r_abc123"})
        self.assertEqual(out["receipt_id"], "r_abc123")
        paths = [r.url.path for r in seen]
        self.assertEqual(paths, ["/v1/usage/summary", "/v1/usage/summary", "/v1/usage/receipt/r_abc123"])
        for r in seen:
            self.assertEqual(r.headers.get("X-Canvas"), CANVAS)

    def test_an_empty_read_footer_says_nothing_served_not_served_zero(self) -> None:
        # Found live 2026-09-05: an empty search minted "served 0 tokens (exact, …) from 0 blocks
        # across 0 docs" — arithmetically true, humanly misleading. It is a call, not a serving.
        line = receipt_footer({"id": "r_e", "tokens": 0, "counter": "tiktoken:cl100k_base", "verified": True,
                               "blocks": 0, "docs": 0, "rereads": 0})
        self.assertIn("nothing served", line)
        self.assertNotIn("served 0", line)
        self.assertIn('june_usage(receipt_id="r_e")', line)

    def test_bad_window_is_a_tool_input_error(self) -> None:
        from june_mcp.tools import ToolInputError
        handler, _ = _engine(None)
        with self.assertRaises(ToolInputError):
            run_tool("june_usage", _client(handler), {"window": "month"})

    def test_usage_is_a_read_and_survives_readonly(self) -> None:
        tool = next(t for t in TOOLS if t.name == "june_usage")
        self.assertFalse(tool.writes)
        self.assertIn("june_usage", {t.name for t in visible_tools(readonly=True)})
        handler, _ = _engine(None)
        self.assertEqual(run_tool("june_usage", _client(handler), {}, readonly=True)["window"], "week")

    def test_engine_without_receipts_answers_plainly_not_with_an_error(self) -> None:
        # JUNE_USAGE off ⇒ the engine mounts no /v1/usage routes ⇒ 404. The agent gets a
        # result that says so and how to turn it on — never a bare "HTTP 404".
        handler = lambda r: httpx.Response(404, json={"detail": "Not Found"})  # noqa: E731
        out = run_tool("june_usage", _client(handler), {})
        self.assertFalse(out["enabled"])
        self.assertIn("JUNE_USAGE", out["note"])
        # an OLD engine 404s on the beacon too ⇒ still "off" (the desktop says "predates receipts")
        one = run_tool("june_usage", _client(handler), {"receipt_id": "r_gone"})
        self.assertFalse(one["enabled"])
        self.assertIn("JUNE_USAGE", one["note"])
        # any other failure still surfaces as the error it is
        boom = lambda r: httpx.Response(500, json={"detail": "x"})  # noqa: E731
        with self.assertRaises(httpx.HTTPStatusError):
            run_tool("june_usage", _client(boom), {})

    def test_unknown_receipt_on_a_live_engine_is_not_reported_as_receipts_off(self) -> None:
        # Found live (v0.0.13 run): a bogus id on an engine counting exactly came back as
        # "receipts are off". The two 404s are told apart by the ungated beacon.
        seen: list[str] = []
        def handler(r: httpx.Request) -> httpx.Response:
            seen.append(r.url.path)
            if r.url.path == "/v1/usage/health":
                return httpx.Response(200, json={"enabled": True, "tokenizer": "tiktoken:cl100k_base"})
            return httpx.Response(404, json={"detail": "no such receipt"})
        out = run_tool("june_usage", _client(handler), {"receipt_id": "r_nope"})
        self.assertTrue(out["enabled"])
        self.assertFalse(out["found"])
        self.assertEqual(out["receipt_id"], "r_nope")
        self.assertNotIn("off", out["note"].lower())
        self.assertEqual(seen, ["/v1/usage/receipt/r_nope", "/v1/usage/health"])
        # the summary path never hits the beacon on a live engine (it does not 404 there)
        seen.clear()
        ok = lambda r: httpx.Response(200, json={"window": "week", "calls": 0})  # noqa: E731
        run_tool("june_usage", _client(ok), {})
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
