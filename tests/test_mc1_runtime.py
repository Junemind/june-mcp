"""MC1 step tests — MCP runtime: fail-closed config, redacted errors, stdout purity.

Style: unittest.TestCase (runs under `python -m unittest` in-sandbox and pytest on
the dev machine). Needs httpx (a [june-api]/[mcp]-tier dep); skips cleanly if the
import chain is unavailable so the standalone pure suite never hard-fails.
"""
from __future__ import annotations

import contextlib
import io
import unittest

try:
    import httpx

    from june_mcp.runtime import (
        DEFAULT_TIMEOUT_ANSWER, DEFAULT_TIMEOUT_READ,
        ConfigError, load_config, make_client, map_error,
    )
    _IMPORT_OK = True
    _IMPORT_ERR = ""
except Exception as exc:  # pragma: no cover
    _IMPORT_OK, _IMPORT_ERR = False, repr(exc)

GOOD_ENV = {
    "JUNE_BASE_URL": "http://localhost:8000",
    "JUNE_API_KEY": "june_sk_test_key",
    "JUNE_CANVAS": "mcp-trial",
}


@unittest.skipUnless(_IMPORT_OK, f"june_mcp.runtime unavailable: {_IMPORT_ERR}")
class TestFailClosedConfig(unittest.TestCase):
    """C8: a misconfigured host must refuse to start — no implicit workspace."""

    def test_valid_env_loads_with_defaults(self) -> None:
        cfg = load_config(GOOD_ENV)
        self.assertEqual(cfg.base_url, "http://localhost:8000")
        self.assertEqual(cfg.canvas, "mcp-trial")
        self.assertEqual(cfg.timeout_read, DEFAULT_TIMEOUT_READ)
        self.assertEqual(cfg.timeout_answer, DEFAULT_TIMEOUT_ANSWER)
        self.assertFalse(cfg.readonly)

    def test_missing_base_url_refused(self) -> None:
        env = {k: v for k, v in GOOD_ENV.items() if k != "JUNE_BASE_URL"}
        with self.assertRaises(ConfigError) as ctx:
            load_config(env)
        self.assertIn("JUNE_BASE_URL", str(ctx.exception))

    def test_missing_canvas_refused(self) -> None:
        env = {k: v for k, v in GOOD_ENV.items() if k != "JUNE_CANVAS"}
        with self.assertRaises(ConfigError) as ctx:
            load_config(env)
        self.assertIn("JUNE_CANVAS", str(ctx.exception))

    def test_missing_key_refused_unless_anon_opt_in(self) -> None:
        env = {k: v for k, v in GOOD_ENV.items() if k != "JUNE_API_KEY"}
        with self.assertRaises(ConfigError):
            load_config(env)
        cfg = load_config({**env, "JUNE_ALLOW_ANON": "1"})   # explicit opt-in only
        self.assertTrue(cfg.allow_anon)

    def test_all_problems_reported_at_once(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            load_config({})
        text = str(ctx.exception)
        for var in ("JUNE_BASE_URL", "JUNE_CANVAS", "JUNE_API_KEY"):
            self.assertIn(var, text)

    def test_non_http_url_refused(self) -> None:
        with self.assertRaises(ConfigError):
            load_config({**GOOD_ENV, "JUNE_BASE_URL": "localhost:8000"})

    def test_bad_timeout_values_refused(self) -> None:
        with self.assertRaises(ConfigError):
            load_config({**GOOD_ENV, "JUNE_TIMEOUT_READ": "fast"})
        with self.assertRaises(ConfigError):
            load_config({**GOOD_ENV, "JUNE_TIMEOUT_ANSWER": "-5"})

    def test_timeout_and_flag_overrides(self) -> None:
        cfg = load_config({**GOOD_ENV, "JUNE_TIMEOUT_READ": "30",
                           "JUNE_TIMEOUT_ANSWER": "200", "JUNE_READONLY": "1"})
        self.assertEqual(cfg.timeout_read, 30.0)
        self.assertEqual(cfg.timeout_answer, 200.0)
        self.assertTrue(cfg.readonly)


@unittest.skipUnless(_IMPORT_OK, f"june_mcp.runtime unavailable: {_IMPORT_ERR}")
class TestErrorRedaction(unittest.TestCase):
    """C6: agent-visible error text must never carry key material, URLs or headers."""

    SECRET = "june_sk_SUPERSECRET_abc123"

    def _assert_clean(self, msg: str) -> None:
        self.assertNotIn(self.SECRET, msg)
        self.assertNotIn("SUPERSECRET", msg)
        self.assertNotIn("localhost", msg)          # no URL echo
        self.assertNotIn("X-API-Key", msg)          # no header echo

    def test_http_status_error_yields_status_only(self) -> None:
        req = httpx.Request("POST", f"http://localhost:8000/v1/search?key={self.SECRET}",
                            headers={"X-API-Key": self.SECRET})
        resp = httpx.Response(401, request=req, text=f"bad key {self.SECRET}")
        exc = httpx.HTTPStatusError("boom " + self.SECRET, request=req, response=resp)
        msg = map_error(exc)
        self.assertIn("401", msg)
        self._assert_clean(msg)

    def test_timeout_and_connect_errors_are_friendly_and_clean(self) -> None:
        for exc in (httpx.ReadTimeout("t " + self.SECRET),
                    httpx.ConnectError("refused " + self.SECRET)):
            msg = map_error(exc)
            self.assertTrue(msg)
            self._assert_clean(msg)

    def test_unknown_exception_collapses_to_type_name(self) -> None:
        msg = map_error(RuntimeError("leaky detail " + self.SECRET))
        self.assertIn("RuntimeError", msg)
        self._assert_clean(msg)

    def test_unknown_tool_keyerror_is_actionable(self) -> None:
        try:
            from june_mcp.tools import run_tool
            from june_client import JuneClient
            run_tool("nope", JuneClient("http://x", "k"))
        except KeyError as exc:
            msg = map_error(exc)
            self.assertIn("nope", msg)      # tool name is agent-supplied, not a secret
            self._assert_clean(msg)
        else:  # pragma: no cover
            self.fail("unknown tool did not raise")


@unittest.skipUnless(_IMPORT_OK, f"june_mcp.runtime unavailable: {_IMPORT_ERR}")
class TestStdoutPurity(unittest.TestCase):
    """C1: stdout is the JSON-RPC wire — nothing here may write a single byte."""

    def test_runtime_operations_write_nothing_to_stdout(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cfg = load_config(GOOD_ENV)
            client = make_client(cfg)
            client.close()
            map_error(httpx.ReadTimeout("x"))
            map_error(RuntimeError("y"))
            with self.assertRaises(ConfigError):
                load_config({})
            from june_mcp.server import tool_manifest
            tool_manifest()
        self.assertEqual(buf.getvalue(), "", "stdout must stay byte-clean (C1)")

    def test_client_transport_carries_config(self) -> None:
        cfg = load_config({**GOOD_ENV, "JUNE_TIMEOUT_READ": "17"})
        client = make_client(cfg)
        try:
            self.assertEqual(client.canvas, "mcp-trial")
            self.assertEqual(client.api_key, "june_sk_test_key")
            self.assertEqual(client._client.timeout.read, 17.0)
        finally:
            client.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
