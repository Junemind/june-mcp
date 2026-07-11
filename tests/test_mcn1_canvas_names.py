"""MC-N1 step tests — canvas NAMES in JUNE_CANVAS (resolution at startup).

The UX gap this closes: every host except the Connect pane made users hunt a
canvas UUID. Now ``JUNE_CANVAS=work`` resolves by name via ``GET /v1/canvases``
— client-side friendliness; the service's strict-UUID ``X-Canvas`` fence is
untouched. UUID-shaped values pass through with ZERO network traffic, so every
existing config keeps byte-identical behavior.

Style: unittest.TestCase (runs under `python -m unittest` in-sandbox and pytest
on the dev machine). Needs httpx; skips cleanly if the import chain is
unavailable so the standalone pure suite never hard-fails.
"""
from __future__ import annotations

import contextlib
import io
import json
import unittest

try:
    import httpx

    from june_client import JuneClient
    from june_mcp.runtime import (
        CanvasAmbiguousError, CanvasNotFoundError, CanvasResolutionError,
        ConfigError, canvas_is_id, load_config, map_error, resolve_canvas,
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

UUID_A = "11111111-2222-4333-8444-555555555555"
UUID_B = "99999999-8888-4777-8666-555555555555"
UUID_NEW = "abcdefab-cdef-4abc-8def-abcdefabcdef"


def _canvas_service(rows, created_id=UUID_NEW, calls=None):
    """MockTransport speaking the REAL /v1/canvases contract (CanvasOut rows —
    stubs are written from the route models, never from memory; MC3 lesson)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append((request.method, request.url.path,
                          request.headers.get("X-Canvas")))
        if request.url.path == "/v1/canvases" and request.method == "GET":
            return httpx.Response(200, json=rows)
        if request.url.path == "/v1/canvases" and request.method == "POST":
            name = json.loads(request.content)["name"]
            return httpx.Response(200, json={"canvas_id": created_id, "name": name,
                                             "created_at": "2026-07-11T00:00:00Z"})
        return httpx.Response(404, json={"detail": "unknown route"})

    return handler


def _client(rows, created_id=UUID_NEW, calls=None) -> "JuneClient":
    http = httpx.Client(base_url="http://svc",
                        transport=httpx.MockTransport(
                            _canvas_service(rows, created_id, calls)))
    return JuneClient("http://svc", "june_sk_test_key", client=http, canvas="unset")


ROWS_ONE = [{"canvas_id": UUID_A, "name": "work", "created_at": "2026-07-01T00:00:00Z"}]
ROWS_TWO_NAMES = ROWS_ONE + [
    {"canvas_id": UUID_B, "name": "notes", "created_at": "2026-07-02T00:00:00Z"}]
ROWS_DUP = ROWS_ONE + [
    {"canvas_id": UUID_B, "name": "work", "created_at": "2026-07-02T00:00:00Z"}]


@unittest.skipUnless(_IMPORT_OK, f"june_mcp.runtime unavailable: {_IMPORT_ERR}")
class TestUuidPassthrough(unittest.TestCase):
    """UUID-shaped values NEVER trigger a lookup — existing configs unchanged."""

    def test_uuid_detection(self) -> None:
        self.assertTrue(canvas_is_id(UUID_A))
        self.assertTrue(canvas_is_id(f"  {UUID_A}  "))
        self.assertFalse(canvas_is_id("work"))
        self.assertFalse(canvas_is_id("mcp-trial"))
        self.assertFalse(canvas_is_id(""))

    def test_uuid_resolves_with_zero_network_calls(self) -> None:
        calls: list = []
        client = _client(ROWS_ONE, calls=calls)
        cid, how = resolve_canvas(client, UUID_A)
        self.assertEqual(cid, UUID_A)
        self.assertIn("as given", how)
        self.assertEqual(calls, [], "an id must resolve with NO network traffic")


@unittest.skipUnless(_IMPORT_OK, f"june_mcp.runtime unavailable: {_IMPORT_ERR}")
class TestNameResolution(unittest.TestCase):
    def test_exact_name_resolves_to_id(self) -> None:
        cid, how = resolve_canvas(_client(ROWS_TWO_NAMES), "work")
        self.assertEqual(cid, UUID_A)
        self.assertIn('"work"', how)
        self.assertIn(UUID_A, how)

    def test_case_insensitive_fallback_when_unique(self) -> None:
        cid, _ = resolve_canvas(_client(ROWS_TWO_NAMES), "Work")
        self.assertEqual(cid, UUID_A)

    def test_exact_match_beats_case_fold(self) -> None:
        rows = ROWS_ONE + [{"canvas_id": UUID_B, "name": "Work",
                            "created_at": "2026-07-02T00:00:00Z"}]
        cid, _ = resolve_canvas(_client(rows), "Work")   # exact "Work" wins
        self.assertEqual(cid, UUID_B)

    def test_ambiguous_name_fails_closed_listing_ids(self) -> None:
        with self.assertRaises(CanvasAmbiguousError) as ctx:
            resolve_canvas(_client(ROWS_DUP), "work")
        text = str(ctx.exception)
        self.assertIn(UUID_A, text)
        self.assertIn(UUID_B, text)
        # ambiguity is NOT fixable by create — must still raise with create=True
        with self.assertRaises(CanvasAmbiguousError):
            resolve_canvas(_client(ROWS_DUP), "work", create=True)

    def test_missing_name_lists_existing_and_hints_create(self) -> None:
        with self.assertRaises(CanvasNotFoundError) as ctx:
            resolve_canvas(_client(ROWS_TWO_NAMES), "june_demo")
        text = str(ctx.exception)
        self.assertIn('"june_demo"', text)
        self.assertIn("work", text)                       # existing names shown
        self.assertIn("JUNE_CANVAS_CREATE", text)         # the remedy is named

    def test_create_flag_mints_the_canvas(self) -> None:
        calls: list = []
        cid, how = resolve_canvas(_client([], calls=calls), "fresh", create=True)
        self.assertEqual(cid, UUID_NEW)
        self.assertIn("(created)", how)
        self.assertEqual([c[:2] for c in calls],
                         [("GET", "/v1/canvases"), ("POST", "/v1/canvases")])

    def test_management_calls_carry_no_x_canvas_header(self) -> None:
        # A stale/unresolved selection must never gate the fix-it calls.
        calls: list = []
        resolve_canvas(_client([], calls=calls), "fresh", create=True)
        for _, _, x_canvas in calls:
            self.assertIsNone(x_canvas)

    def test_transport_errors_propagate_for_map_error(self) -> None:
        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused june_sk_test_key")
        http = httpx.Client(base_url="http://svc",
                            transport=httpx.MockTransport(down))
        client = JuneClient("http://svc", "june_sk_test_key", client=http)
        with self.assertRaises(httpx.ConnectError) as ctx:
            resolve_canvas(client, "work")
        msg = map_error(ctx.exception)                    # the caller's contract
        self.assertNotIn("june_sk_test_key", msg)
        self.assertNotIn("svc", msg)


@unittest.skipUnless(_IMPORT_OK, f"june_mcp.runtime unavailable: {_IMPORT_ERR}")
class TestConfigSurface(unittest.TestCase):
    def test_create_flag_parses(self) -> None:
        self.assertFalse(load_config(GOOD_ENV).canvas_create)
        self.assertTrue(load_config({**GOOD_ENV, "JUNE_CANVAS_CREATE": "1"}).canvas_create)

    def test_create_conflicts_with_readonly(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            load_config({**GOOD_ENV, "JUNE_CANVAS_CREATE": "1", "JUNE_READONLY": "1"})
        self.assertIn("JUNE_CANVAS_CREATE", str(ctx.exception))

    def test_resolution_error_is_a_config_class_error(self) -> None:
        # Both subclasses collapse to the operator-facing base for callers.
        self.assertTrue(issubclass(CanvasNotFoundError, CanvasResolutionError))
        self.assertTrue(issubclass(CanvasAmbiguousError, CanvasResolutionError))


@unittest.skipUnless(_IMPORT_OK, f"june_mcp.runtime unavailable: {_IMPORT_ERR}")
class TestStdoutPurity(unittest.TestCase):
    """C1 holds through the new startup step: resolution writes zero stdout bytes."""

    def test_resolution_paths_write_nothing_to_stdout(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            resolve_canvas(_client(ROWS_TWO_NAMES), "work")
            resolve_canvas(_client(ROWS_ONE), UUID_A)
            resolve_canvas(_client([]), "fresh", create=True)
            with contextlib.suppress(CanvasResolutionError):
                resolve_canvas(_client(ROWS_DUP), "work")
            with contextlib.suppress(CanvasResolutionError):
                resolve_canvas(_client([]), "nope")
        self.assertEqual(buf.getvalue(), "", "stdout must stay byte-clean (C1)")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
