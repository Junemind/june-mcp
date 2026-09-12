"""june_remember must never lose, duplicate, or leave a write unknowable (2026-09-05).

Found live: a 40k-char remember with hosted extraction outran the 15 s read timeout; the
connector said "timed out" while the engine kept writing. Now: the job route is used whenever
the engine offers it (submit → poll → collect); a write that cannot finish inside one host tool
call hands back {state: running, job_id} and june_remember(job_id=…) collects it; on engines
without the job route the sync call gets a budget scaled to the text; a transport timeout comes
back as a structured `state: unknown` with the verification step, never a bare error."""
from __future__ import annotations

import json
import unittest

try:
    import httpx

    from june_client import JuneClient
    from june_mcp import tools as T
    from june_mcp.tools import run_tool
    _IMPORT_OK, _IMPORT_ERR = True, ""
except Exception as exc:  # pragma: no cover
    _IMPORT_OK, _IMPORT_ERR = False, repr(exc)


def _client(handler) -> "JuneClient":
    http = httpx.Client(base_url="http://june.test", transport=httpx.MockTransport(handler))
    return JuneClient("http://june.test", "june_sk_test", client=http, llm_key="byo-secret")


DONE = {"job_id": "j1", "state": "done", "stage": "done", "pct": 1.0, "nodes_written": 4, "edges_written": 3,
        "engine": "hosted", "hosted_degraded": False,
        "created": {"node_ids": ["a", "b"], "updated_node_ids": [], "edge_ids": ["e"]}}


@unittest.skipUnless(_IMPORT_OK, f"june_mcp unavailable: {_IMPORT_ERR}")
class TestRememberJobs(unittest.TestCase):
    def setUp(self) -> None:
        T._ASYNC_INGEST.clear()
        self._wait, self._poll = T.REMEMBER_WAIT_IN_CALL, T.REMEMBER_POLL_START
        T.REMEMBER_POLL_START = 0.001

    def tearDown(self) -> None:
        T.REMEMBER_WAIT_IN_CALL, T.REMEMBER_POLL_START = self._wait, self._poll
        T._ASYNC_INGEST.clear()

    def test_budget_scales_with_the_text_and_is_capped(self) -> None:
        self.assertEqual(T.remember_budget(0), 30.0)
        self.assertEqual(T.remember_budget(40_000), 110.0)
        self.assertEqual(T.remember_budget(10**7), 600.0)
        self.assertGreater(T.remember_budget(64_000), T.remember_budget(1_000))

    def test_job_route_submit_poll_collect_returns_the_sync_shape(self) -> None:
        polls: list[int] = [0]
        seen: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req.url.path)
            if req.url.path == "/v1/ingest/text/async":
                self.assertEqual(req.headers.get("X-LLM-Key"), "byo-secret")     # BYO rides the job submit too
                self.assertEqual(json.loads(req.content)["format"], "markdown")
                return httpx.Response(200, json={"job_id": "j1", "state": "running"})
            if req.url.path == "/v1/ingest/text/status":
                polls[0] += 1
                if polls[0] < 3:
                    return httpx.Response(200, json={"job_id": "j1", "state": "running", "stage": "extracting", "pct": 0.3})
                return httpx.Response(200, json=DONE)
            return httpx.Response(500, json={"detail": "the sync route must not be used when the job route exists"})

        out = run_tool("june_remember", _client(handler), {"text": "# Note\n\nVireo renewed."})
        self.assertEqual(out["state"], "done")
        self.assertEqual(out["nodes_written"], 4)
        self.assertEqual(out["created"]["node_ids"], ["a", "b"])
        self.assertEqual(out["engine"], "hosted")
        self.assertEqual(out["format"], "markdown")
        self.assertEqual(out["job_id"], "j1")
        self.assertNotIn("/v1/ingest/text", seen)
        self.assertEqual(polls[0], 3)

    def test_a_write_that_outlives_the_tool_call_hands_back_the_job_id(self) -> None:
        T.REMEMBER_WAIT_IN_CALL = 0.01

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/v1/ingest/text/async":
                return httpx.Response(200, json={"job_id": "j9", "state": "running"})
            return httpx.Response(200, json={"job_id": "j9", "state": "running", "stage": "extracting", "pct": 0.3})

        out = run_tool("june_remember", _client(handler), {"text": "x" * 20_000})
        self.assertEqual(out["state"], "running")
        self.assertEqual(out["job_id"], "j9")
        self.assertIn('june_remember(job_id="j9")', out["note"])
        self.assertIn("Do not re-send", out["note"])

    def test_job_id_collects_a_finished_write_without_resending_text(self) -> None:
        seen: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req.method + " " + req.url.path)
            self.assertEqual(req.url.params.get("job"), "j1")
            return httpx.Response(200, json={**DONE, "format": "markdown", "source_app": "mcp"})

        out = run_tool("june_remember", _client(handler), {"job_id": "j1"})
        self.assertEqual(out["state"], "done")
        self.assertEqual(out["nodes_written"], 4)
        self.assertEqual(seen, ["GET /v1/ingest/text/status"])

    def test_job_id_states_running_error_and_unknown(self) -> None:
        for reply, expect in (
            (httpx.Response(200, json={"job_id": "j1", "state": "running", "stage": "writing", "pct": 0.9}), "running"),
            (httpx.Response(200, json={"job_id": "j1", "state": "error", "detail": "ValueError"}), "error"),
            (httpx.Response(404, json={"detail": "unknown job"}), "unknown"),
        ):
            out = run_tool("june_remember", _client(lambda r, reply=reply: reply), {"job_id": "j1"})
            self.assertEqual(out["state"], expect)
            self.assertIn("note", out)
        # an unknown job tells the agent HOW to recover: verify, then re-send (which upserts)
        out = run_tool("june_remember", _client(lambda r: httpx.Response(404, json={})), {"job_id": "gone"})
        self.assertIn("june_search", out["note"])
        self.assertIn("upserts", out["note"])

    def test_engine_without_the_job_route_uses_sync_with_a_scaled_budget_once_probed(self) -> None:
        seen: list[tuple[str, float | None]] = []
        text = "y" * 40_000

        def handler(req: httpx.Request) -> httpx.Response:
            to = req.extensions.get("timeout") or {}
            seen.append((req.url.path, to.get("read")))
            if req.url.path == "/v1/ingest/text/async":
                return httpx.Response(404, json={"detail": "Not Found"})
            return httpx.Response(200, json={"nodes_written": 1, "edges_written": 0, "format": "markdown", "source_app": "mcp"})

        c = _client(handler)
        out = run_tool("june_remember", c, {"text": text})
        self.assertEqual(out["nodes_written"], 1)
        self.assertEqual(seen[0][0], "/v1/ingest/text/async")
        self.assertEqual(seen[1], ("/v1/ingest/text", T.remember_budget(len(text))))   # scaled, not the 15 s default
        run_tool("june_remember", c, {"text": "second"})
        self.assertEqual([p for p, _ in seen].count("/v1/ingest/text/async"), 1, "the absence is remembered per transport")

    def test_sync_timeout_is_a_structured_unknown_with_the_verification_step(self) -> None:
        T._ASYNC_INGEST.clear()

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/v1/ingest/text/async":
                return httpx.Response(404, json={"detail": "Not Found"})
            raise httpx.ReadTimeout("slow engine", request=req)

        out = run_tool("june_remember", _client(handler), {"text": "# Standup decisions\n\nship Friday"})
        self.assertEqual(out["state"], "unknown")
        self.assertIn("june_search('Standup decisions')", out["note"])
        self.assertIn("upserts", out["note"])
        self.assertEqual(out["budget_s"], T.remember_budget(len("# Standup decisions\n\nship Friday")))

    def test_engine_side_failure_is_an_error_that_says_a_resend_is_safe(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/v1/ingest/text/async":
                return httpx.Response(200, json={"job_id": "j2", "state": "running"})
            return httpx.Response(200, json={"job_id": "j2", "state": "error", "stage": "error", "detail": "RuntimeError"})

        with self.assertRaises(RuntimeError) as cm:
            run_tool("june_remember", _client(handler), {"text": "note"})
        self.assertIn("upserts", str(cm.exception))

    def test_schema_allows_job_id_and_the_manifest_explains_it(self) -> None:
        from june_mcp.server import tool_manifest
        t = next(x for x in tool_manifest() if x["name"] == "june_remember")
        self.assertIn("job_id", t["input_schema"]["properties"])
        self.assertIn("state: running", t["description"])
        self.assertIn("never re-send", t["description"])


if __name__ == "__main__":
    unittest.main()
