"""Phase 4 (design §11): the compact surface — B-prime, 20 tools — is the SAME connector.

Every op reachable, every hidden op refused, per-op required args, errors list the valid ops,
results carry ``op``, and decorations are identical to calling the member directly, because a
family call IS the member call after ``resolve_call`` (one chokepoint, ``run_tool``). Also pinned:
the counts per posture (20 / 17 / 9), the deterministic order, the schema shape (``op`` enum of
visible ops only, union of arguments, ownership prefixes), W1a/W1b (short canvas note; grammar on
demand and byte-equal to the literal), D10 ``next_call`` respelling, the alias map in the digest,
and the fence chain's messages surviving underneath (read-only / Pro / no-pages / opt-in).

httpx.MockTransport for the client-level checks (same seam as test_canvas_tools); one stdio case
spawns the real server with JUNE_TOOL_PROFILE=compact.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import unittest
from http.server import HTTPServer

import httpx

from june_client import JuneClient
from june_mcp import tools as tools_mod
from june_mcp.capabilities import NEEDS_PAGES
from june_mcp.runtime import ToolInputError
from june_mcp.server import _tag_op, instructions_for, tool_manifest
from june_mcp.surfaces import (FAMILIES, OPS, PAGE_CREATE_SHORT, PAGE_GRAMMAR, SHORT_CANVAS_DOC,
                               SHORT_DOCS_CANVAS_DOC, alias_lines, build_surface, display_name,
                               resolve_call)
from june_mcp.tools import TOOLS, _BY_NAME, configure_surface, run_tool, visible_tools

_A = "11111111-1111-1111-1111-111111111111"
_B = "22222222-2222-2222-2222-222222222222"
_PAGE = "33333333-3333-3333-3333-333333333333"
FAMILY_NAMES = {v[0] for v in FAMILIES.values()}
MEMBERS = set(OPS)


def _service(seen: dict):
    def handler(req: httpx.Request) -> httpx.Response:
        seen.setdefault("calls", []).append((req.method, req.url.path))
        if req.url.path == "/v1/canvases" and req.method == "GET":
            return httpx.Response(200, json=[
                {"canvas_id": _A, "name": "work", "created_at": "2026-08-01"},
                {"canvas_id": _B, "name": "home-lab", "created_at": "2026-08-02"}])
        if req.url.path.endswith("/clear") and req.method == "POST":
            return httpx.Response(200, json={"canvas_id": req.url.path.split("/")[3],
                                             "nodes_deleted": 7, "edges_deleted": 9})
        if req.url.path == "/v1/pages" and req.method == "GET":
            return httpx.Response(200, json={"pages": [{"page_id": _PAGE, "title": "T", "revision": 1,
                                                        "updated_at": "2026-09-01T00:00:00Z"}],
                                             "total": 1})
        if req.url.path == f"/v1/pages/{_PAGE}" and req.method == "GET":
            return httpx.Response(200, json={"page_id": _PAGE, "title": "T", "revision": 1,
                                             "updated_at": "2026-09-01T00:00:00Z", "blocks": []})
        return httpx.Response(404, json={"detail": "unknown route"})
    return handler


def _client(seen: dict) -> JuneClient:
    http = httpx.Client(base_url="http://june.test", transport=httpx.MockTransport(_service(seen)))
    return JuneClient("http://june.test", "june_sk_test", client=http, canvas=_A)


def _family_call(surface, client, name, args, **posture):
    """What server._call does for one tool call, minus the wire."""
    member, a = resolve_call(surface, name, args)
    res = run_tool(member, client, a, **posture)
    if member != name and isinstance(res, dict):
        res = _tag_op(res, name, member, surface)
    return member, res


class TestShape(unittest.TestCase):
    def test_counts_per_posture(self) -> None:
        self.assertEqual(len(build_surface("compact")), 20)                       # Pro, rw
        self.assertEqual(len(build_surface("compact", pro=False)), 17)            # free
        self.assertEqual(len(build_surface("compact", readonly=True)), 9)         # read-only
        self.assertEqual(len(build_surface("compact", absent=NEEDS_PAGES)), 20 - 8)  # hosted: no pages
        self.assertEqual(len(build_surface("full")), 30)

    def test_order_is_deterministic_and_families_sit_at_their_first_member(self) -> None:
        names = [s.name for s in build_surface("compact")]
        self.assertEqual(names, [
            "june_answer", "june_search", "june_enumerate", "june_context", "june_usage",
            "june_graph", "june_remember", "june_ingest", "june_maintain", "june_page_read",
            "june_page_edit", "june_page_write", "june_page_delete", "june_canvas_read",
            "june_canvas_create", "june_canvas_erase", "june_docs_read", "june_doc_save",
            "june_doc_delete", "june_learn"])
        # the registry's order (test_registry_facts pins it): a family sits where its first member sat
        folded = []
        for t in visible_tools():
            n = FAMILIES[t.family][0] if t.family else t.name
            if n not in folded:
                folded.append(n)
        self.assertEqual(names, folded)
        self.assertEqual(names, [s.name for s in build_surface("compact")])  # stable across calls

    def test_family_names_never_reuse_a_member_name(self) -> None:
        self.assertFalse(FAMILY_NAMES & {t.name for t in TOOLS})

    def test_every_member_has_exactly_one_op_and_none_collide_within_a_family(self) -> None:
        fam_members = {t.name for t in TOOLS if t.family is not None}
        self.assertEqual(fam_members, MEMBERS)
        for s in build_surface("compact"):
            if s.is_family:
                self.assertEqual(len(set(s.ops.values())), len(s.ops), s.name)

    def test_op_enum_lists_only_visible_ops_per_posture(self) -> None:
        for kw in ({}, {"pro": False}, {"readonly": True}, {"absent": NEEDS_PAGES}):
            vis = {t.name for t in visible_tools(**{k: v for k, v in kw.items()})}
            for s in build_surface("compact", **kw):
                if not s.is_family:
                    continue
                enum = s.input_schema["properties"]["op"]["enum"]
                expected = [OPS[m] for m in s.members] + (["grammar"] if s.name == "june_page_read" else [])
                self.assertEqual(enum, expected, (kw, s.name))
                self.assertTrue(set(s.members) <= vis, (kw, s.name))
                self.assertEqual(s.input_schema["required"], ["op"])

    def test_family_schema_is_the_union_with_ownership_prefixes(self) -> None:
        pe = next(s for s in build_surface("compact") if s.name == "june_page_edit")
        props = pe.input_schema["properties"]
        union = set()
        for m in pe.members:
            union |= set(_BY_NAME[m].input_schema["properties"])
        self.assertEqual(set(props) - {"op"}, union)
        # `page_id` belongs to append/update, not create → says so; `title` (create only) too
        self.assertTrue(props["page_id"]["description"].startswith("(ops: append, update) "))
        self.assertTrue(props["title"]["description"].startswith("(ops: create) "))
        # `canvas` is on every op → no prefix, and it is the SHORT note (W1a)
        self.assertEqual(props["canvas"]["description"], SHORT_CANVAS_DOC)

    def test_w1a_short_canvas_note_on_compact_only(self) -> None:
        full = {s.name: s for s in build_surface("full")}
        comp = {s.name: s for s in build_surface("compact")}
        self.assertNotEqual(full["june_search"].input_schema["properties"]["canvas"]["description"],
                            SHORT_CANVAS_DOC)
        self.assertEqual(comp["june_search"].input_schema["properties"]["canvas"]["description"],
                         SHORT_CANVAS_DOC)
        self.assertEqual(comp["june_doc_save"].input_schema["properties"]["canvas"]["description"],
                         SHORT_DOCS_CANVAS_DOC)
        self.assertLess(sum(len(json.dumps(s.input_schema) + s.description) for s in comp.values()),
                        0.85 * sum(len(json.dumps(s.input_schema) + s.description) for s in full.values()))

    def test_w1b_grammar_split_is_byte_faithful_to_the_literal(self) -> None:
        literal = _BY_NAME["june_page_create"].description
        self.assertIn(PAGE_GRAMMAR, literal)
        head = literal[:literal.index(PAGE_GRAMMAR)].rstrip()
        self.assertTrue(PAGE_CREATE_SHORT.startswith(head))
        self.assertTrue(PAGE_CREATE_SHORT.endswith(literal[literal.index("Returns {page_id, title, blocks_written"):]))
        for kw in ("• DIAGRAM", "LIVE VIEW", "MEDIA", "STYLING", "layout"):
            self.assertIn(kw, PAGE_GRAMMAR, kw)
        # the one-line TABLE rule stays inline (the block models reach for most); the rest is on demand
        self.assertIn("• TABLE", PAGE_CREATE_SHORT)
        self.assertNotIn("• TABLE", PAGE_GRAMMAR)
        self.assertIn("BEFORE composing", PAGE_CREATE_SHORT)
        pe = next(s for s in build_surface("compact") if s.name == "june_page_edit")
        self.assertIn("op='create': " + PAGE_CREATE_SHORT, pe.description)
        self.assertNotIn("• DIAGRAM", pe.description)

    def test_family_annotations_have_one_effect_class(self) -> None:
        for s in build_surface("compact"):
            if s.is_family:
                ro = {_BY_NAME[m].annotations["readOnlyHint"] for m in s.members}
                de = {_BY_NAME[m].annotations["destructiveHint"] for m in s.members}
                self.assertEqual(len(ro), 1, s.name); self.assertEqual(len(de), 1, s.name)
                self.assertEqual(s.annotations["readOnlyHint"], ro.pop())
                self.assertEqual(s.annotations["destructiveHint"], de.pop())
                self.assertEqual(s.annotations["title"], s.title)

    def test_every_array_on_compact_declares_items(self) -> None:
        def walk(schema, path=""):
            out = []
            if schema.get("type") == "array" and "items" not in schema:
                out.append(path)
            for k, v in (schema.get("properties") or {}).items():
                out += walk(v, f"{path}.{k}")
            return out
        self.assertEqual([f"{s.name}{p}" for s in build_surface("compact") for p in walk(s.input_schema)], [])

    def test_manifest_serves_the_surface(self) -> None:
        m = tool_manifest(profile="compact")
        self.assertEqual(len(m), 20)
        pr = next(t for t in m if t["name"] == "june_page_read")
        self.assertEqual(pr["ops"], {"list": "june_page_list", "get": "june_page_get"})
        self.assertEqual(pr["members"], ["june_page_list", "june_page_get"])
        self.assertEqual([t["name"] for t in tool_manifest(profile="full")], [t.name for t in TOOLS if t.available])


class TestDispatch(unittest.TestCase):
    def setUp(self) -> None:
        self.surface = build_surface("compact")
        self.seen: dict = {}
        self.client = _client(self.seen)

    def test_every_op_on_every_family_resolves_to_its_member(self) -> None:
        for s in self.surface:
            for op, member in s.ops.items():
                req = {k: "x" for k in (_BY_NAME[member].input_schema.get("required") or [])}
                got, args = resolve_call(self.surface, s.name, {"op": op, **req})
                self.assertEqual(got, member, (s.name, op))
                self.assertNotIn("op", args)
                self.assertEqual(args, req)

    def test_hidden_op_is_refused_and_the_error_lists_the_valid_ops(self) -> None:
        ro = build_surface("compact", readonly=True)
        pr = next(s for s in ro if s.name == "june_page_read")
        self.assertEqual(list(pr.ops), ["list", "get"])
        with self.assertRaises(ToolInputError) as cm:
            resolve_call(ro, "june_page_read", {"op": "create", "title": "t"})
        self.assertIn("'list'", str(cm.exception)); self.assertIn("'get' requires page_id", str(cm.exception))
        # an op that exists on no posture, and no op at all
        with self.assertRaises(ToolInputError) as cm:
            resolve_call(self.surface, "june_graph", {"op": "explode"})
        self.assertIn("'neighborhood' requires node_id, node_type", str(cm.exception))
        with self.assertRaises(ToolInputError) as cm:
            resolve_call(self.surface, "june_graph", {"node_id": "n"})
        self.assertIn("needs op = one of ['neighborhood', 'subgraph']", str(cm.exception))
        # a family that is not on this posture at all
        with self.assertRaises(ToolInputError) as cm:
            resolve_call(ro, "june_page_edit", {"op": "create", "title": "t"})
        self.assertIn("unknown tool 'june_page_edit' on this surface; valid:", str(cm.exception))

    def test_per_op_required_args_and_stray_args(self) -> None:
        with self.assertRaises(ToolInputError) as cm:
            resolve_call(self.surface, "june_page_read", {"op": "get"})
        self.assertIn("op='get' requires page_id", str(cm.exception))
        with self.assertRaises(ToolInputError) as cm:
            resolve_call(self.surface, "june_graph", {"op": "subgraph", "node_id": "n", "node_type": "entity",
                                                      "page_id": "p"})
        self.assertIn("does not take page_id", str(cm.exception))
        # `canvas` is never stray
        got, args = resolve_call(self.surface, "june_graph", {"op": "subgraph", "node_id": "n",
                                                              "node_type": "entity", "canvas": "work"})
        self.assertEqual(got, "june_subgraph"); self.assertEqual(args["canvas"], "work")

    def test_member_name_on_compact_is_refused_with_the_op_hint(self) -> None:
        with self.assertRaises(ToolInputError) as cm:
            resolve_call(self.surface, "june_page_get", {"page_id": _PAGE})
        self.assertIn("call june_page_read with op='get'", str(cm.exception))
        for member in MEMBERS:
            with self.assertRaises(ToolInputError):
                resolve_call(self.surface, member, {})

    def test_hidden_member_falls_through_to_the_fence_chain(self) -> None:
        """A tool hidden by posture keeps its 0.4.1 refusal (read-only / Pro / no pages / opt-in)."""
        ro = build_surface("compact", readonly=True)
        member, args = resolve_call(ro, "june_remember", {"text": "x"})
        self.assertEqual(member, "june_remember")
        with self.assertRaises(KeyError) as cm:
            run_tool(member, self.client, args, readonly=True, profile="compact")
        self.assertIn("read-only", str(cm.exception))
        free = build_surface("compact", pro=False)
        member, _ = resolve_call(free, "june_page_write", {"page_id": _PAGE, "blocks": []})
        with self.assertRaises(KeyError) as cm:
            run_tool(member, self.client, {"page_id": _PAGE, "blocks": []}, pro=False, profile="compact")
        self.assertIn("requires June Pro", str(cm.exception))
        nopages = build_surface("compact", absent=NEEDS_PAGES)
        member, _ = resolve_call(nopages, "june_page_write", {"page_id": _PAGE, "blocks": []})
        with self.assertRaises(KeyError) as cm:
            run_tool(member, self.client, {"page_id": _PAGE, "blocks": []}, absent=NEEDS_PAGES, profile="compact")
        self.assertIn("does not serve pages", str(cm.exception))
        # a family whose members are all absent is not on the surface: no op-hint (it would lie),
        # the member falls through to run_tool's "does not serve pages"
        self.assertEqual(resolve_call(nopages, "june_page_get", {"page_id": _PAGE})[0], "june_page_get")

    def test_grammar_op_is_the_literal_grammar(self) -> None:
        self.assertEqual(resolve_call(self.surface, "june_page_read", {"op": "grammar"}), ("__grammar__", {}))
        with self.assertRaises(ToolInputError):
            resolve_call(build_surface("compact", absent=NEEDS_PAGES), "june_page_read", {"op": "grammar"})

    def test_results_carry_op_and_match_the_member_call_exactly(self) -> None:
        direct = run_tool("june_canvas_list", self.client, {})
        member, via = _family_call(self.surface, self.client, "june_canvas_read", {"op": "list"})
        self.assertEqual(member, "june_canvas_list")
        self.assertEqual(via.pop("op"), "list")
        self.assertEqual(via, direct)
        direct = run_tool("june_page_get", self.client, {"page_id": _PAGE})
        _, via = _family_call(self.surface, self.client, "june_page_read", {"op": "get", "page_id": _PAGE})
        self.assertEqual(via.pop("op"), "get")
        self.assertEqual(via, direct)
        # per-call canvas targeting works through the family exactly as through the member
        _, via = _family_call(self.surface, self.client, "june_page_read",
                              {"op": "list", "canvas": "home-lab"})
        self.assertEqual((via["canvas"], via["canvas_name"]), (_B, "home-lab"))

    def test_two_phase_erase_respells_next_call_and_warning_for_this_surface(self) -> None:
        _, pend = _family_call(self.surface, self.client, "june_canvas_erase", {"op": "clear", "canvas": "home-lab"})
        self.assertTrue(pend["pending"]); self.assertEqual(pend["op"], "clear")
        self.assertEqual(pend["next_call"], {"tool": "june_canvas_erase",
                                             "arguments": {"op": "clear", "canvas": "home-lab",
                                                           "confirm": pend["confirm_token"]}})
        self.assertNotIn("june_canvas_clear", pend["warning"])
        self.assertIn("june_canvas_erase(op='clear')", pend["warning"])
        self.assertNotIn(("POST", f"/v1/canvases/{_B}/clear"), self.seen["calls"])   # nothing executed
        # the respelled next_call is copy-pasteable as-is and executes
        _, done = _family_call(self.surface, self.client, pend["next_call"]["tool"], pend["next_call"]["arguments"])
        self.assertEqual(done["nodes_deleted"], 7); self.assertEqual(done["op"], "clear")
        self.assertIn(("POST", f"/v1/canvases/{_B}/clear"), self.seen["calls"])
        # the member spelling of next_call is untouched on full
        full = build_surface("full")
        _, pend = _family_call(full, self.client, "june_canvas_clear", {"canvas": "home-lab"})
        self.assertEqual(pend["next_call"]["tool"], "june_canvas_clear")
        self.assertIn("june_canvas_clear", pend["warning"])


class TestTeaching(unittest.TestCase):
    def test_instructions_on_compact_never_name_a_member_outside_the_alias_map(self) -> None:
        text = instructions_for(profile="compact")
        body, alias = text.rsplit("\n\n", 1)
        self.assertTrue(alias.startswith("Older docs and notes may name the member tools directly"))
        bare = {m for m in re.findall(r"june_[a-z_]+", body) if m in MEMBERS}
        self.assertEqual(bare, set(), "a compact connection was taught a name it cannot call")
        self.assertIn("june_page_read(op='get')", body)
        for member in MEMBERS:
            self.assertIn(f"{member} → ", alias, member)
        # D9 rewordings landed
        self.assertIn("ONCE per session", body)
        self.assertIn("do not log routine, successful tool calls", body)
        self.assertNotIn("ONCE per session", instructions_for(profile="full"))

    def test_display_and_alias_cover_exactly_the_visible_ops(self) -> None:
        ro = build_surface("compact", readonly=True)
        disp = display_name(ro)
        self.assertEqual(disp("june_page_get"), "june_page_read(op='get')")
        self.assertEqual(disp("june_page_create"), "june_page_create")     # not on this surface
        self.assertEqual(disp("june_answer"), "june_answer")
        self.assertNotIn("june_page_create", alias_lines(ro))
        self.assertIn("june_page_get → june_page_read op=get", alias_lines(ro))

    def test_alias_map_rides_the_standing_docs_digest_on_compact_only(self) -> None:
        from june_mcp import refresh as refresh_mod
        saved = (tools_mod._docs_canvas_resolve, refresh_mod.derive_registry, refresh_mod.build_digest)
        tools_mod._docs_canvas_resolve = lambda client, name: (_B, name, {})
        refresh_mod.derive_registry = lambda client, budget_seconds: ([], {})
        refresh_mod.build_digest = lambda docs, cap_chars: {"docs": []}
        try:
            configure_surface(aliases=alias_lines(build_surface("compact")))
            d = tools_mod._standing_docs(_client({}))
            self.assertIn("june_page_get → june_page_read op=get", d["tool_aliases"])
            configure_surface(aliases="")
            self.assertNotIn("tool_aliases", tools_mod._standing_docs(_client({})))
        finally:
            configure_surface(aliases="")
            tools_mod._docs_canvas_resolve, refresh_mod.derive_registry, refresh_mod.build_digest = saved


try:
    import anyio
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from tests.test_mc3_stdio import _StubJune, _pin_spawn_to_imported_june_mcp
    _MCP_OK, _MCP_ERR = True, ""
except Exception as exc:  # pragma: no cover
    _MCP_OK, _MCP_ERR = False, repr(exc)


@unittest.skipUnless(_MCP_OK, f"mcp client SDK unavailable: {_MCP_ERR}")
class TestCompactOverStdio(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = HTTPServer(("127.0.0.1", 0), _StubJune)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()

    def _params(self, **extra_env: str) -> "StdioServerParameters":
        env = {k: v for k, v in os.environ.items() if k.startswith(("PATH", "PYTHON", "LANG", "LC_"))}
        env.update({"JUNE_BASE_URL": f"http://127.0.0.1:{self.port}", "JUNE_API_KEY": "june_sk_c",
                    "JUNE_CANVAS": "mcp-trial", "JUNE_TOOL_PROFILE": "compact", **extra_env})
        _pin_spawn_to_imported_june_mcp(env)
        return StdioServerParameters(command=sys.executable, args=["-m", "june_mcp"], env=env)

    def test_compact_profile_on_the_wire(self) -> None:
        async def scenario() -> None:
            async with stdio_client(self._params()) as (read, write):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    self.assertIn("june_page_read(op='get')", init.instructions)
                    tools = await session.list_tools()
                    names = [t.name for t in tools.tools]
                    self.assertEqual(len(names), 20)
                    self.assertEqual(names[0], "june_answer")
                    by = {t.name: t for t in tools.tools}
                    self.assertEqual(by["june_page_read"].inputSchema["required"], ["op"])
                    self.assertTrue(by["june_page_read"].annotations.readOnlyHint)
                    self.assertTrue(by["june_canvas_erase"].annotations.destructiveHint)
                    self.assertEqual(by["june_canvas_erase"].title, "Erase a canvas")
                    # a family call runs the member and the result carries the op
                    res = await session.call_tool("june_canvas_read", {"op": "list"})
                    self.assertFalse(res.isError)
                    payload = json.loads(res.content[0].text)
                    self.assertEqual(payload["op"], "list")
                    self.assertEqual(payload["canvases"][0]["name"], "mcp-trial")
                    # grammar on demand
                    res = await session.call_tool("june_page_read", {"op": "grammar"})
                    self.assertEqual(json.loads(res.content[0].text)["grammar"], PAGE_GRAMMAR)
                    # a member name is refused with the op hint, as an error
                    res = await session.call_tool("june_canvas_list", {})
                    self.assertTrue(res.isError)
                    self.assertIn("call june_canvas_read with op='list'", res.content[0].text)
                    # an op the schema does not allow is caught by the SDK's validation
                    res = await session.call_tool("june_canvas_read", {"op": "nuke"})
                    self.assertTrue(res.isError)
                    # a single tool is unchanged
                    res = await session.call_tool("june_answer", {"query": "q"})
                    self.assertEqual(json.loads(res.content[0].text)["answer"], "grounded: q")

        anyio.run(scenario)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
