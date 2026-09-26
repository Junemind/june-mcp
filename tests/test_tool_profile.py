"""JUNE_TOOL_PROFILE — `full` (default, byte-identical to before) | `lean` (the six verbs a coding
agent uses). Measured motivation: the full manifest costs ~9.6k prompt tokens per agent turn
(`june-bench tokens-saved`, 2026-09-04). httpx.MockTransport only; no server, no network."""
from __future__ import annotations

import json
import unittest

try:
    import httpx

    from june_client import JuneClient
    from june_mcp.prompts import SERVER_INSTRUCTIONS, SERVER_INSTRUCTIONS_LEAN
    from june_mcp.runtime import ConfigError, load_config
    from june_mcp.server import tool_manifest
    from june_mcp.tools import LEAN_PROFILE, PROFILES, run_tool, visible_tools
    _IMPORT_OK, _IMPORT_ERR = True, ""
except Exception as exc:  # pragma: no cover
    _IMPORT_OK, _IMPORT_ERR = False, repr(exc)

CANVAS = "11111111-1111-1111-1111-111111111111"
BASE_ENV = {"JUNE_BASE_URL": "http://june.test", "JUNE_API_KEY": "june_sk_test", "JUNE_CANVAS": "work"}


def _client(handler) -> "JuneClient":
    http = httpx.Client(base_url="http://june.test", transport=httpx.MockTransport(handler))
    return JuneClient("http://june.test", "june_sk_test", client=http, canvas=CANVAS)


@unittest.skipUnless(_IMPORT_OK, f"june_mcp unavailable: {_IMPORT_ERR}")
class TestLeanProfile(unittest.TestCase):
    def test_full_is_the_default_and_unchanged(self):
        self.assertEqual([t.name for t in visible_tools()], [t.name for t in visible_tools(profile="full")])
        self.assertEqual(tool_manifest(), tool_manifest(profile="full"))
        self.assertIsNone(PROFILES["full"])

    def test_lean_is_exactly_the_six_agent_verbs(self):
        names = {t["name"] for t in tool_manifest(profile="lean")}
        self.assertEqual(names, set(LEAN_PROFILE))
        self.assertEqual(names, {"june_answer", "june_context", "june_search", "june_remember",
                                 "june_learn", "june_usage"})
        # composes with read-only: writes drop out of lean too
        ro = {t["name"] for t in tool_manifest(profile="lean", readonly=True)}
        self.assertEqual(ro, {"june_answer", "june_context", "june_search", "june_usage"})

    def test_lean_manifest_is_a_fraction_of_full(self):
        def chars(m):  # noqa: ANN001
            return sum(len(t["description"]) + len(json.dumps(t["input_schema"])) for t in m)
        full, lean = chars(tool_manifest()), chars(tool_manifest(profile="lean"))
        self.assertLess(lean, full / 4)                      # measured 6.1k vs 31.7k chars
        self.assertLess(len(SERVER_INSTRUCTIONS_LEAN), len(SERVER_INSTRUCTIONS) / 5)
        self.assertLess(len(SERVER_INSTRUCTIONS_LEAN), 1200)  # a paragraph, not a manual
        for verb in ("june_answer", "june_context", "june_search", "june_remember", "june_learn", "june_usage"):
            self.assertIn(verb, SERVER_INSTRUCTIONS_LEAN)
        self.assertNotIn("june_page", SERVER_INSTRUCTIONS_LEAN)  # never teaches tools it cannot call

    def test_hidden_verbs_are_fenced_at_execution_too(self):
        client = _client(lambda r: httpx.Response(200, json={"items": [], "degraded": []}))
        out = run_tool("june_search", client, {"query": "q"}, profile="lean")
        self.assertIsInstance(out, dict)
        for name, args in (("june_page_list", {}), ("june_canvas_list", {}), ("june_ingest", {"nodes": []})):
            with self.assertRaises(KeyError) as ctx:
                run_tool(name, client, args, profile="lean")
            self.assertIn("lean", str(ctx.exception))
        # full profile still runs them (a 200 from the fake engine is enough to prove the fence is gone)
        run_tool("june_page_list", client, {}, profile="full")

    def test_unknown_profile_is_refused_everywhere(self):
        with self.assertRaises(KeyError):
            visible_tools(profile="tiny")
        with self.assertRaises(ConfigError) as ctx:
            load_config({**BASE_ENV, "JUNE_TOOL_PROFILE": "tiny"})
        self.assertIn("JUNE_TOOL_PROFILE", " ".join(ctx.exception.problems))

    def test_config_reads_the_knob(self):
        # D6 (0.4.2): the DEPLOYMENT default is compact — a connection that says nothing gets the
        # folded surface. `full` is one env var away and is the same patched 0.4.2 surface.
        self.assertEqual(load_config(BASE_ENV).profile, "compact")
        self.assertEqual(load_config({**BASE_ENV, "JUNE_TOOL_PROFILE": ""}).profile, "compact")
        self.assertEqual(load_config({**BASE_ENV, "JUNE_TOOL_PROFILE": "Full"}).profile, "full")
        self.assertEqual(load_config({**BASE_ENV, "JUNE_TOOL_PROFILE": "Lean"}).profile, "lean")

    def test_manifest_cli_resolves_the_same_default_as_the_server(self):
        """`--manifest` is what an operator runs to see what their connection will expose, so its
        no-env answer must be the SERVER's default, not a literal of its own. 0.4.2 shipped with
        `or "full"` hardcoded here while the server default was compact: the command reported 30
        tools for a connection that serves 20. One default, one place, asserted from the CLI."""
        import json as _json
        import subprocess as _sp
        import sys as _sys
        from june_mcp.runtime import DEFAULT_TOOL_PROFILE
        from june_mcp.surfaces import build_surface

        def names(**env):
            from _child_env import child_env
            e = child_env()
            e.update(env)
            out = _sp.run([_sys.executable, "-m", "june_mcp", "--manifest"],
                          capture_output=True, text=True, timeout=120, env=e, check=True).stdout
            return [t["name"] for t in _json.loads(out)]

        self.assertEqual(len(names()), len(build_surface(DEFAULT_TOOL_PROFILE)))
        self.assertEqual(names(), [t.name for t in build_surface(DEFAULT_TOOL_PROFILE)])
        self.assertEqual(names(JUNE_TOOL_PROFILE="full"), [t.name for t in build_surface("full")])
        self.assertEqual(len(names(JUNE_TOOL_PROFILE="lean")), 6)

    def test_library_signatures_still_default_to_full(self):
        """The default moved for the SERVER, not for the API: a library caller that names no
        profile still gets the unfolded surface, so nothing about the importable contract moved."""
        from june_mcp.surfaces import build_surface
        self.assertEqual(len(tool_manifest()), len(visible_tools()))
        self.assertEqual(len(build_surface()), len(visible_tools()))
        self.assertIn("june_page_get", {t["name"] for t in tool_manifest()})


if __name__ == "__main__":
    unittest.main()
