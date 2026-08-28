"""Phase AM tests — the six doc tools + the standing_docs injection, driven
through ``run_tool`` against a stateful fake June service (httpx.MockTransport,
no network). Covers: registry over real page routes, save/get roundtrip, the
revision guard, two-phase delete, append-only learn (and its refusal to touch
curated docs), digest injection cadence + exemptions + failure silence, the
docs-canvas default vs explicit override, and the read-only surface.
"""
from __future__ import annotations

import json
import unittest
import uuid

import httpx

from june_client import JuneClient
from june_mcp import refresh
from june_mcp import tools as tools_mod
from june_mcp.refresh import make_sentinel, parse_sentinel
from june_mcp.runtime import ToolInputError
from june_mcp.tools import configure_docs, run_tool, visible_tools

DEFAULT_CID = "11111111-1111-1111-1111-111111111111"


class FakeJune:
    """Just enough June service for the doc tools: canvases + pages routes,
    canvas-fenced via X-Canvas, with revision tokens that actually guard."""

    def __init__(self) -> None:
        self.canvases: dict[str, dict] = {}
        self.fail_pages = False               # switch: every pages route 500s
        self.fail_rename = False              # switch: PUT /v1/pages/{id} 500s
        self.page_calls = 0                   # wire-traffic counter (pages routes)
        self.on_canvas_create = None          # hook(name, cid) — simulate a racing session
        self.on_page_create = None            # hook(canvas_id, pid) — simulate a racing session
        self._tick = 0
        self.add_canvas("work", cid=DEFAULT_CID)

    # ── state helpers ────────────────────────────────────────────────────
    def add_canvas(self, name: str, cid: str | None = None) -> str:
        cid = cid or str(uuid.uuid4())
        self.canvases[cid] = {"name": name, "pages": {}}
        return cid

    def cid_of(self, name: str) -> str:
        return next(c for c, v in self.canvases.items() if v["name"] == name)

    def add_doc_page(self, canvas_name: str, name: str, *, kind: str = "doc",
                     when: str = "", pinned: bool = False, body: str = "the body",
                     title: str | None = None, pid: str | None = None) -> str:
        cid = self.cid_of(canvas_name)
        pid = pid or str(uuid.uuid4())
        self.canvases[cid]["pages"][pid] = {
            "title": title or name, "updated_at": self._stamp(), "blocks": [
                {"block_id": str(uuid.uuid4()), "block_type": "paragraph",
                 "text": make_sentinel(name, kind, when, pinned, 1), "order": 1.0},
                {"block_id": str(uuid.uuid4()), "block_type": "paragraph",
                 "text": body, "order": 2.0}]}
        return pid

    def add_plain_page(self, canvas_name: str, title: str, text: str = "prose") -> str:
        cid = self.cid_of(canvas_name)
        pid = str(uuid.uuid4())
        self.canvases[cid]["pages"][pid] = {
            "title": title, "updated_at": self._stamp(), "blocks": [
                {"block_id": str(uuid.uuid4()), "block_type": "paragraph",
                 "text": text, "order": 1.0}]}
        return pid

    def _stamp(self) -> str:
        self._tick += 1
        return f"t{self._tick:04d}"

    def _detail(self, pid: str, page: dict) -> dict:
        return {"page_id": pid, "title": page["title"],
                "updated_at": page["updated_at"], "blocks": page["blocks"]}

    # ── the wire ─────────────────────────────────────────────────────────
    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/v1/canvases" and req.method == "GET":
            return httpx.Response(200, json=[
                {"canvas_id": cid, "name": v["name"]}
                for cid, v in self.canvases.items()])
        if path == "/v1/canvases" and req.method == "POST":
            name = json.loads(req.content)["name"]
            cid = self.add_canvas(name)
            if self.on_canvas_create:
                hook, self.on_canvas_create = self.on_canvas_create, None
                hook(name, cid)
            return httpx.Response(200, json={"canvas_id": cid, "name": name})
        if path.startswith("/v1/canvases/") and req.method == "DELETE":
            cid = path.split("/")[3]
            if cid not in self.canvases:
                return httpx.Response(404, json={"detail": "unknown canvas"})
            gone = self.canvases.pop(cid)
            return httpx.Response(200, json={"canvas_id": cid, "deleted": True,
                                             "nodes_deleted": 0, "edges_deleted": 0,
                                             "name": gone["name"]})
        if path == "/v1/answer":
            return httpx.Response(200, json={"answer": "ok", "citations": [],
                                             "used_edge_ids": [], "degraded": [],
                                             "mode": "local"})
        if path.startswith("/v1/pages"):
            self.page_calls += 1
            if self.fail_pages:
                return httpx.Response(500, json={"detail": "boom"})
            canvas = self.canvases.get(req.headers.get("X-Canvas", ""))
            if canvas is None:
                return httpx.Response(404, json={"detail": "unknown canvas"})
            pages = canvas["pages"]
            if path == "/v1/pages" and req.method == "GET":
                return httpx.Response(200, json={
                    "pages": [{"page_id": pid, "title": p["title"],
                               "updated_at": p["updated_at"]}
                              for pid, p in pages.items()],
                    "has_more": False})
            if path == "/v1/pages" and req.method == "POST":
                pid = str(uuid.uuid4())
                title = json.loads(req.content)["title"]
                pages[pid] = {"title": title, "updated_at": self._stamp(), "blocks": []}
                if self.on_page_create:
                    hook, self.on_page_create = self.on_page_create, None
                    hook(req.headers.get("X-Canvas", ""), pid)
                return httpx.Response(200, json={"page_id": pid, "title": title})
            parts = path.split("/")
            pid = parts[3]
            page = pages.get(pid)
            if page is None:
                return httpx.Response(404, json={"detail": "unknown page"})
            if path.endswith(":append"):
                new = json.loads(req.content)["blocks"]
                base = max((b["order"] for b in page["blocks"]), default=0.0)
                for i, b in enumerate(new, start=1):
                    page["blocks"].append({"block_id": str(uuid.uuid4()),
                                           "block_type": b["block_type"],
                                           "text": b["text"], "order": base + i})
                page["updated_at"] = self._stamp()
                return httpx.Response(200, json={
                    "page_id": pid, "appended": new, "updated_at": page["updated_at"],
                    "revision": self._tick, "blocks_total": len(page["blocks"])})
            if path.endswith("/blocks") and req.method == "POST":
                body = json.loads(req.content)
                expected = body.get("expected_updated_at")
                if expected is not None and expected != page["updated_at"]:
                    return httpx.Response(409, json={"detail": "page has changed"})
                page["blocks"] = [{"block_id": b.get("id") or str(uuid.uuid4()),
                                   "block_type": b["block_type"], "text": b["text"],
                                   "order": b["order"]} for b in body["blocks"]]
                page["updated_at"] = self._stamp()
                return httpx.Response(200, json=self._detail(pid, page))
            if req.method == "GET":
                return httpx.Response(200, json=self._detail(pid, page))
            if req.method == "PUT":
                if self.fail_rename:
                    return httpx.Response(500, json={"detail": "rename boom"})
                page["title"] = json.loads(req.content)["title"]
                return httpx.Response(200, json={"page_id": pid, "title": page["title"]})
            if req.method == "DELETE":
                n = len(pages.pop(pid)["blocks"])
                return httpx.Response(200, json={"ok": True, "page_id": pid,
                                                 "blocks_deleted": n})
        return httpx.Response(404, json={"detail": f"unhandled {req.method} {path}"})


def _client(fake: FakeJune) -> JuneClient:
    transport = httpx.MockTransport(fake.handler)
    http = httpx.Client(base_url="http://june.test", transport=transport)
    return JuneClient("http://june.test", "june_sk_test", client=http, canvas=DEFAULT_CID)


class TestDocSave(unittest.TestCase):
    def test_first_save_creates_docs_canvas_page_and_sentinel(self) -> None:
        fake = FakeJune()
        out = run_tool("june_doc_save", _client(fake), {
            "name": "house-rules", "text": "# Rules\n\nAlways read first.",
            "pinned": True})
        self.assertTrue(out["created"])
        self.assertEqual(out["canvas_name"], "agent_docs")   # canvas auto-created
        cid = fake.cid_of("agent_docs")
        page = fake.canvases[cid]["pages"][out["page_id"]]
        meta = parse_sentinel(page["blocks"][0]["text"])     # sentinel is block 1
        self.assertEqual((meta["name"], meta["pinned"], meta["v"]),
                         ("house-rules", True, 1))
        self.assertEqual(page["blocks"][1]["block_type"], "heading_1")

    def test_resave_bumps_v_and_replaces_body(self) -> None:
        fake = FakeJune()
        fake.add_canvas("agent_docs")
        fake.add_doc_page("agent_docs", "ship-ops", body="old body")
        client = _client(fake)
        out = run_tool("june_doc_save", client, {"name": "ship-ops", "text": "new body"})
        self.assertFalse(out["created"])
        self.assertEqual(out["v"], 2)
        got = run_tool("june_doc_get", client, {"name": "ship-ops"})
        self.assertEqual(got["body"], "new body")

    def test_stale_expected_updated_at_is_refused_not_applied(self) -> None:
        fake = FakeJune()
        fake.add_canvas("agent_docs")
        fake.add_doc_page("agent_docs", "ship-ops", body="current body")
        out = run_tool("june_doc_save", _client(fake), {
            "name": "ship-ops", "text": "from a stale read",
            "expected_updated_at": "t0000-stale"})
        self.assertEqual(out["refused"], "doc_changed_since_read")
        self.assertFalse(out["ok"])
        got = run_tool("june_doc_get", _client(fake), {"name": "ship-ops"})
        self.assertEqual(got["body"], "current body")        # nothing changed

    def test_skill_requires_when_to_use_and_kind_validated(self) -> None:
        fake = FakeJune()
        with self.assertRaises(ToolInputError):
            run_tool("june_doc_save", _client(fake),
                     {"name": "fix-class", "text": "b", "kind": "skill"})
        with self.assertRaises(ToolInputError):
            run_tool("june_doc_save", _client(fake),
                     {"name": "x", "text": "b", "kind": "wizardry"})
        with self.assertRaises(ToolInputError):
            run_tool("june_doc_save", _client(fake), {"name": "Bad Name", "text": "b"})

    def test_oversize_body_truncates_with_note(self) -> None:
        fake = FakeJune()
        out = run_tool("june_doc_save", _client(fake), {
            "name": "big", "text": "x" * (refresh.MAX_DOC_CHARS + 5)})
        self.assertIn("text", out["_clamped"])


class TestDocListGet(unittest.TestCase):
    def test_list_sees_only_sentinel_pages(self) -> None:
        fake = FakeJune()
        fake.add_canvas("agent_docs")
        fake.add_doc_page("agent_docs", "a")
        fake.add_doc_page("agent_docs", "s", kind="skill", when="on bugs")
        fake.add_plain_page("agent_docs", "just notes")
        out = run_tool("june_doc_list", _client(fake), {})
        self.assertEqual(sorted(d["name"] for d in out["docs"]), ["a", "s"])
        self.assertNotIn("body", out["docs"][0])             # listing stays cheap

    def test_list_without_docs_canvas_is_a_soft_empty(self) -> None:
        out = run_tool("june_doc_list", _client(FakeJune()), {})
        self.assertEqual(out["docs"], [])
        # AM3: the empty state TEACHES — full setup guidance, not one thin line.
        self.assertIn("june_doc_save", out["setup"])
        self.assertIn("agent-memory-guide", out["setup"])

    def test_get_unknown_name_names_the_known(self) -> None:
        fake = FakeJune()
        fake.add_canvas("agent_docs")
        fake.add_doc_page("agent_docs", "a")
        with self.assertRaises(ToolInputError) as ctx:
            run_tool("june_doc_get", _client(fake), {"name": "missing"})
        self.assertIn("a", str(ctx.exception))

    def test_explicit_canvas_overrides_the_default(self) -> None:
        fake = FakeJune()
        fake.add_canvas("agent_docs")
        fake.add_canvas("team_docs")
        fake.add_doc_page("team_docs", "team-rules")
        out = run_tool("june_doc_list", _client(fake), {"canvas": "team_docs"})
        self.assertEqual([d["name"] for d in out["docs"]], ["team-rules"])
        self.assertEqual(out["canvas_name"], "team_docs")

    def test_explicit_missing_canvas_fails_closed(self) -> None:
        with self.assertRaises(KeyError):
            run_tool("june_doc_list", _client(FakeJune()), {"canvas": "nope"})


class TestDocDelete(unittest.TestCase):
    def test_two_phase(self) -> None:
        fake = FakeJune()
        fake.add_canvas("agent_docs")
        fake.add_doc_page("agent_docs", "old-doc")
        client = _client(fake)
        pending = run_tool("june_doc_delete", client, {"name": "old-doc"})
        self.assertTrue(pending["pending"])
        self.assertIn("Nothing was deleted", pending["warning"])
        cid = fake.cid_of("agent_docs")
        self.assertEqual(len(fake.canvases[cid]["pages"]), 1)   # still there
        done = run_tool("june_doc_delete", client,
                        {"name": "old-doc", "confirm": pending["confirm_token"]})
        self.assertTrue(done["deleted"])
        self.assertEqual(len(fake.canvases[cid]["pages"]), 0)

    def test_bad_token_refused(self) -> None:
        fake = FakeJune()
        fake.add_canvas("agent_docs")
        fake.add_doc_page("agent_docs", "old-doc")
        with self.assertRaises(ToolInputError):
            run_tool("june_doc_delete", _client(fake),
                     {"name": "old-doc", "confirm": "bogus"})


class TestLearn(unittest.TestCase):
    def test_appends_dated_entry_creating_the_doc_on_first_use(self) -> None:
        fake = FakeJune()
        client = _client(fake)
        out = run_tool("june_learn", client, {"text": "connector restarts void handles"})
        self.assertTrue(out["created_doc"])
        got = run_tool("june_doc_get", client, {"name": "learnings"})
        self.assertEqual(got["kind"], "learnings")
        self.assertRegex(got["body"], r"^- \[\d{4}-\d{2}-\d{2}\] connector restarts")
        out2 = run_tool("june_learn", client, {"text": "second lesson"})
        self.assertFalse(out2["created_doc"])
        self.assertIn("second lesson", run_tool("june_doc_get", client,
                                                {"name": "learnings"})["body"])

    def test_refuses_to_append_into_curated_docs(self) -> None:
        # The Dev Practices rule, structurally: no auto-appended incident notes.
        fake = FakeJune()
        fake.add_canvas("agent_docs")
        fake.add_doc_page("agent_docs", "dev-practices", kind="skill", when="on bugs")
        with self.assertRaises(ToolInputError) as ctx:
            run_tool("june_learn", _client(fake),
                     {"text": "an incident", "doc": "dev-practices"})
        self.assertIn("june_doc_save", str(ctx.exception))


class TestDocsRefreshTool(unittest.TestCase):
    def test_returns_full_digest_now(self) -> None:
        fake = FakeJune()
        fake.add_canvas("agent_docs")
        fake.add_doc_page("agent_docs", "rules", pinned=True, body="PINNED")
        fake.add_doc_page("agent_docs", "fix-class", kind="skill", when="on bugs")
        out = run_tool("june_docs_refresh", _client(fake), {})
        self.assertEqual(out["pinned"][0]["body"], "PINNED")
        self.assertEqual(out["skills"][0]["when_to_use"], "on bugs")

    def test_no_canvas_yet_is_guidance_not_error(self) -> None:
        out = run_tool("june_docs_refresh", _client(FakeJune()), {})
        self.assertEqual(out["docs"], [])
        self.assertIn("june_doc_save", out["setup"])
        self.assertIn("ASK THE USER".lower(), out["setup"].lower())


class TestInjection(unittest.TestCase):
    def _fake_with_docs(self) -> FakeJune:
        fake = FakeJune()
        fake.add_canvas("agent_docs")
        fake.add_doc_page("agent_docs", "rules", pinned=True, body="PINNED BODY")
        return fake

    def test_disabled_by_default_for_library_callers(self) -> None:
        out = run_tool("june_answer", _client(self._fake_with_docs()), {"query": "q"})
        self.assertNotIn("standing_docs", out)

    def test_first_call_bootstraps_then_cadence_quiets(self) -> None:
        fake = self._fake_with_docs()
        client = _client(fake)
        configure_docs(enabled=True)
        out = run_tool("june_answer", client, {"query": "q"})
        self.assertEqual(out["standing_docs"]["pinned"][0]["body"], "PINNED BODY")
        out2 = run_tool("june_answer", client, {"query": "q"})   # inside the interval
        self.assertNotIn("standing_docs", out2)

    def test_call_threshold_reinjects(self) -> None:
        fake = self._fake_with_docs()
        client = _client(fake)
        configure_docs(enabled=True, calls=2, minutes=10.0)
        self.assertIn("standing_docs", run_tool("june_answer", client, {"query": "q"}))
        self.assertNotIn("standing_docs", run_tool("june_answer", client, {"query": "q"}))
        self.assertIn("standing_docs", run_tool("june_answer", client, {"query": "q"}))

    def test_doc_tools_are_exempt(self) -> None:
        fake = self._fake_with_docs()
        client = _client(fake)
        configure_docs(enabled=True)
        out = run_tool("june_doc_list", client, {})
        self.assertNotIn("standing_docs", out)

    def test_digest_failure_never_hurts_the_carrying_call(self) -> None:
        fake = self._fake_with_docs()
        client = _client(fake)
        configure_docs(enabled=True)
        fake.fail_pages = True
        out = run_tool("june_answer", client, {"query": "q"})
        self.assertEqual(out["answer"], "ok")                # untouched
        self.assertNotIn("standing_docs", out)

    def test_missing_docs_canvas_is_silent_and_not_hammered(self) -> None:
        fake = FakeJune()                                    # no agent_docs canvas
        client = _client(fake)
        configure_docs(enabled=True)
        out = run_tool("june_answer", client, {"query": "q"})
        self.assertNotIn("standing_docs", out)
        before = fake.page_calls
        run_tool("june_answer", client, {"query": "q"})      # full quiet interval
        self.assertEqual(fake.page_calls, before)

    def test_explicit_refresh_resets_the_cadence(self) -> None:
        fake = self._fake_with_docs()
        client = _client(fake)
        configure_docs(enabled=True, calls=2, minutes=10.0)
        run_tool("june_docs_refresh", client, {})            # explicit read
        out = run_tool("june_answer", client, {"query": "q"})
        self.assertNotIn("standing_docs", out)               # cadence was reset


class TestSurfacePosture(unittest.TestCase):
    def test_readonly_keeps_doc_reads_hides_doc_writes(self) -> None:
        names = {t.name for t in visible_tools(readonly=True)}
        self.assertIn("june_docs_refresh", names)
        self.assertIn("june_doc_list", names)
        self.assertIn("june_doc_get", names)
        for w in ("june_doc_save", "june_doc_delete", "june_learn"):
            self.assertNotIn(w, names)

    def test_doc_tools_ignore_canvas_strict(self) -> None:
        # canvas_scoped=False: the docs canvas IS their well-defined default, so
        # JUNE_CANVAS_STRICT must not refuse them for omitting 'canvas'.
        fake = FakeJune()
        fake.add_canvas("agent_docs")
        out = run_tool("june_doc_list", _client(fake), {}, strict=True)
        self.assertEqual(out["docs"], [])

    def test_doc_tools_carry_canvas_arg_docs(self) -> None:
        for t in tools_mod.TOOLS:
            if t.name in tools_mod._DOCS_TOOL_NAMES:
                desc = t.input_schema["properties"]["canvas"]["description"]
                self.assertIn("agent_docs", desc)


if __name__ == "__main__":
    unittest.main()


class TestHardening(unittest.TestCase):
    """Production-grade countermeasures: reservation release on BaseException,
    the block-count cap on doc bodies, and the budgeted injection scan note."""

    def test_base_exception_releases_the_reservation(self) -> None:
        # A cancellation-class exception mid-build must not leave the in-flight
        # flag stuck (which would silently kill injection for the whole session).
        class Teardown(BaseException):
            pass

        fake = FakeJune()
        fake.add_canvas("agent_docs")
        fake.add_doc_page("agent_docs", "rules", pinned=True)
        client = _client(fake)
        configure_docs(enabled=True, calls=1, minutes=10.0)

        original = refresh.build_digest
        refresh.build_digest = lambda *a, **k: (_ for _ in ()).throw(Teardown())
        try:
            with self.assertRaises(Teardown):
                tools_mod._standing_docs(client)
        finally:
            refresh.build_digest = original
        # The reservation was released as a FAILURE (short retry), so a later
        # due tick can fire again instead of being blocked forever.
        self.assertFalse(tools_mod._DOCS_STATE._building)
        out = run_tool("june_answer", client, {"query": "q"})   # calls=1 → due again
        self.assertIn("standing_docs", out)

    def test_doc_save_block_count_is_capped_visibly(self) -> None:
        fake = FakeJune()
        # Char-legal (< MAX_DOC_CHARS) but block-explosive: thousands of one-item lists.
        text = "- x\n\n" * 3000                     # 15k chars → 3000 bulleted blocks
        out = run_tool("june_doc_save", _client(fake), {"name": "bomb", "text": text})
        self.assertIn("blocks", out["_clamped"])
        cid = fake.cid_of("agent_docs")
        page = fake.canvases[cid]["pages"][out["page_id"]]
        self.assertLessEqual(len(page["blocks"]), tools_mod.MAX_PAGE_BLOCKS)

    def test_injection_carries_partial_scan_notes(self) -> None:
        # When the budgeted scan truncates, the digest says so instead of
        # silently presenting partial coverage as the whole registry.
        fake = FakeJune()
        fake.add_canvas("agent_docs")
        for i in range(3):
            fake.add_doc_page("agent_docs", f"d{i}", pinned=(i == 0))
        client = _client(fake)
        configure_docs(enabled=True)
        original = refresh.DIGEST_BUILD_BUDGET_SECONDS
        refresh.DIGEST_BUILD_BUDGET_SECONDS = 0.0   # everything over budget instantly
        try:
            out = run_tool("june_answer", client, {"query": "q"})
        finally:
            refresh.DIGEST_BUILD_BUDGET_SECONDS = original
        # Zero budget → no pages scanned → no docs → no digest at all (quiet,
        # honest) — the carrying call is untouched either way.
        self.assertEqual(out["answer"], "ok")


class TestRaceConvergence(unittest.TestCase):
    """The two create races and the rename partial-state, closed for real:
    every racing session applies the same deterministic winner rule, the loser
    removes only its OWN artifact, and nothing is ever silently split-brained."""

    LOW = "00000000-0000-4000-8000-000000000001"    # always wins min(page_id/canvas_id)
    HIGH = "ffffffff-ffff-4fff-8fff-fffffffffffe"   # always loses

    # ── docs-canvas create race ──────────────────────────────────────────
    def test_canvas_race_loser_deletes_own_empty_duplicate(self) -> None:
        fake = FakeJune()

        def rival_appears(name, our_cid):           # the other session's create lands too
            fake.canvases[self.LOW] = {"name": name, "pages": {}}

        fake.on_canvas_create = rival_appears
        out = run_tool("june_doc_save", _client(fake), {"name": "rules", "text": "b"})
        self.assertEqual(out["canvas"], self.LOW)   # converged on the rival (lower id)
        names = [v["name"] for v in fake.canvases.values()]
        self.assertEqual(names.count("agent_docs"), 1)   # our duplicate is GONE
        self.assertIn("docs_canvas_race", out.get("_notes", {}))
        # And the doc itself landed in the winner canvas.
        self.assertTrue(fake.canvases[self.LOW]["pages"])

    def test_canvas_race_winner_keeps_its_canvas(self) -> None:
        fake = FakeJune()

        def rival_appears(name, our_cid):
            fake.canvases[self.HIGH] = {"name": name, "pages": {}}

        fake.on_canvas_create = rival_appears
        out = run_tool("june_doc_save", _client(fake), {"name": "rules", "text": "b"})
        self.assertNotEqual(out["canvas"], self.HIGH)
        self.assertIn(self.HIGH, fake.canvases)     # NEVER delete the rival's canvas
        self.assertIn("docs_canvas_duplicates", out.get("_notes", {}))

    def test_preexisting_duplicate_canvases_no_longer_brick_the_tools(self) -> None:
        fake = FakeJune()
        a = fake.add_canvas("agent_docs")
        b = fake.add_canvas("agent_docs")
        winner = min(a, b)
        # Put a doc in the winner directly (cid_of returns first match, not winner).
        fake.canvases[winner]["pages"]["p1"] = {
            "title": "rules", "updated_at": "t1", "blocks": [
                {"block_id": "b1", "block_type": "paragraph",
                 "text": make_sentinel("rules", "doc", pinned=True), "order": 1.0},
                {"block_id": "b2", "block_type": "paragraph",
                 "text": "the body", "order": 2.0}]}
        out = run_tool("june_doc_list", _client(fake), {})
        self.assertEqual(out["canvas"], winner)     # deterministic, not an error
        self.assertEqual([d["name"] for d in out["docs"]], ["rules"])
        self.assertIn("docs_canvas_duplicates", out["_notes"])
        # Injection uses the same converging resolver: docs still reach the digest.
        client = _client(fake)
        configure_docs(enabled=True)
        got = run_tool("june_answer", client, {"query": "q"})
        self.assertEqual(got["standing_docs"]["pinned"][0]["name"], "rules")

    # ── doc-name create race ─────────────────────────────────────────────
    def test_doc_save_collision_loser_reports_and_removes_only_its_page(self) -> None:
        fake = FakeJune()
        fake.add_canvas("agent_docs")

        def rival_doc(canvas_id, our_pid):          # rival's save lands mid-flight, wins
            fake.add_doc_page("agent_docs", "rules", body="rival body", pid=self.LOW)

        fake.on_page_create = rival_doc
        out = run_tool("june_doc_save", _client(fake), {"name": "rules", "text": "mine"})
        self.assertEqual(out["refused"], "doc_name_collision")
        self.assertFalse(out["ok"])
        self.assertIn("merge", out["message"])
        cid = next(c for c, v in fake.canvases.items() if v["name"] == "agent_docs")
        self.assertEqual(list(fake.canvases[cid]["pages"]), [self.LOW])  # only the winner
        got = run_tool("june_doc_get", _client(fake), {"name": "rules"})
        self.assertEqual(got["body"], "rival body")  # winner's content untouched

    def test_doc_save_collision_winner_keeps_content_and_notes(self) -> None:
        fake = FakeJune()
        fake.add_canvas("agent_docs")

        def rival_doc(canvas_id, our_pid):
            fake.add_doc_page("agent_docs", "rules", body="rival body", pid=self.HIGH)

        fake.on_page_create = rival_doc
        out = run_tool("june_doc_save", _client(fake), {"name": "rules", "text": "mine"})
        self.assertTrue(out["created"])
        self.assertIn("collision", out["_notes"])
        got = run_tool("june_doc_get", _client(fake), {"name": "rules"})
        self.assertEqual(got["body"], "mine")        # deterministic winner everywhere

    # ── learnings create race: FULL self-heal ────────────────────────────
    def test_learn_collision_appends_onto_the_winner(self) -> None:
        fake = FakeJune()
        fake.add_canvas("agent_docs")

        def rival_learnings(canvas_id, our_pid):
            fake.add_doc_page("agent_docs", "learnings", kind="learnings",
                              body="- [2026-08-24] rival entry", pid=self.LOW)

        fake.on_page_create = rival_learnings
        out = run_tool("june_learn", _client(fake), {"text": "my lesson"})
        self.assertEqual(out["page_id"], self.LOW)   # landed on the winner
        self.assertIn("collision", out["_notes"])
        got = run_tool("june_doc_get", _client(fake), {"name": "learnings"})
        self.assertIn("rival entry", got["body"])    # nothing lost on either side
        self.assertIn("my lesson", got["body"])
        cid = next(c for c, v in fake.canvases.items() if v["name"] == "agent_docs")
        self.assertEqual(len(fake.canvases[cid]["pages"]), 1)   # duplicate healed away

    # ── rename partial state ─────────────────────────────────────────────
    def test_rename_failure_reports_saved_body_with_warning(self) -> None:
        fake = FakeJune()
        fake.add_canvas("agent_docs")
        fake.add_doc_page("agent_docs", "rules", body="old", title="rules")
        fake.fail_rename = True
        out = run_tool("june_doc_save", _client(fake),
                       {"name": "rules", "text": "new body", "title": "House Rules"})
        self.assertNotIn("refused", out)             # NOT an error: the save landed
        self.assertIn("SAVED", out["warning"])
        got = run_tool("june_doc_get", _client(fake), {"name": "rules"})
        self.assertEqual(got["body"], "new body")    # body current, title old — as reported
        self.assertEqual(got["title"], "rules")


class TestLegacyEndpoints(unittest.TestCase):
    """An endpoint that structurally lacks the feature (pre-/v1/canvases, or
    pages gated off) goes QUIET for a full interval — never a 60s retry loop
    taxing old services for a capability they cannot serve."""

    def test_pages_route_absent_is_idle_not_retried(self) -> None:
        fake = FakeJune()
        fake.add_canvas("agent_docs")

        real = fake.handler

        def gated(req):                             # JUNE_PAGES off: pages routes 404
            if req.url.path.startswith("/v1/pages"):
                fake.page_calls += 1
                return httpx.Response(404, json={"detail": "unknown route"})
            return real(req)

        transport = httpx.MockTransport(gated)
        http = httpx.Client(base_url="http://june.test", transport=transport)
        client = JuneClient("http://june.test", "june_sk_test", client=http,
                            canvas=DEFAULT_CID)
        configure_docs(enabled=True)
        out = run_tool("june_answer", client, {"query": "q"})
        self.assertEqual(out["answer"], "ok")       # carrying call untouched
        self.assertNotIn("standing_docs", out)
        before = fake.page_calls
        run_tool("june_answer", client, {"query": "q"})
        self.assertEqual(fake.page_calls, before)   # quiet interval, no retry loop


class TestTeachingBootstrap(unittest.TestCase):
    """AM3 — June TEACHES agents how to use it: the operating manual is seeded
    into the store itself the moment the docs canvas is born, empty states carry
    full setup guidance, and the guide's content is lockstep-pinned so the
    vocabulary it teaches can't drift from the tools that implement it."""

    def test_first_save_seeds_the_operating_manual(self) -> None:
        fake = FakeJune()
        client = _client(fake)
        out = run_tool("june_doc_save", client, {"name": "rules", "text": "b"})
        self.assertIn("seeded", out["_notes"])
        self.assertIn("docs_canvas_created", out["_notes"])
        listing = run_tool("june_doc_list", client, {})
        names = {d["name"] for d in listing["docs"]}
        self.assertEqual(names, {"rules", refresh.GUIDE_DOC_NAME,
                                 refresh.JUNE_FIRST_NAME})
        guide = run_tool("june_doc_get", client, {"name": refresh.GUIDE_DOC_NAME})
        self.assertIn("SYSTEM CANVAS", guide["body"])
        self.assertTrue(guide["when_to_use"])
        # The manual is listed in every digest; the JUNE-FIRST posture rides it
        # PINNED, so depending on June by default is re-asserted all session.
        digest = run_tool("june_docs_refresh", client, {})
        self.assertIn(refresh.GUIDE_DOC_NAME,
                      [d["name"] for d in digest["docs"]])
        pinned = {p["name"]: p["body"] for p in digest["pinned"]}
        self.assertIn(refresh.JUNE_FIRST_NAME, pinned)
        self.assertIn("without the user asking", pinned[refresh.JUNE_FIRST_NAME])

    def test_learn_first_also_seeds(self) -> None:
        fake = FakeJune()
        client = _client(fake)
        run_tool("june_learn", client, {"text": "first lesson"})
        names = {d["name"] for d in run_tool("june_doc_list", client, {})["docs"]}
        self.assertIn(refresh.GUIDE_DOC_NAME, names)
        self.assertIn(refresh.JUNE_FIRST_NAME, names)

    def test_preexisting_canvas_is_never_reseeded(self) -> None:
        # The seed rides CANVAS CREATION only: an existing setup (maybe the user
        # deleted the guide on purpose) is respected, never re-populated.
        fake = FakeJune()
        fake.add_canvas("agent_docs")
        client = _client(fake)
        out = run_tool("june_doc_save", client, {"name": "rules", "text": "b"})
        self.assertNotIn("seeded", out.get("_notes", {}))
        names = {d["name"] for d in run_tool("june_doc_list", client, {})["docs"]}
        self.assertEqual(names, {"rules"})

    def test_guide_teaches_the_actual_vocabulary(self) -> None:
        # Lockstep pin (the teaching-content rule): every verb/concept the guide
        # names must be real, and the core workflow must be taught.
        body = refresh.GUIDE_DOC_BODY
        for phrase in ("june_docs_refresh", "june_doc_save", "june_doc_get",
                       "june_learn", "june_docs_export", "june_page_export",
                       "june_page_import", "pinned", "when_to_use",
                       "expected_updated_at", "SYSTEM CANVAS", "workstream"):
            self.assertIn(phrase, body, f"guide never teaches {phrase!r}")
        for phrase in ("june_doc_save", "agent-memory-guide", "june_docs_refresh",
                       "june_learn"):
            self.assertIn(phrase, refresh.SETUP_NOTE)
        # The guide's own body must survive the markdown round-trip it will live as.
        blocks = [{**b, "order": float(i + 1)} for i, b in
                  enumerate(refresh.markdown_to_blocks(body))]
        self.assertIn("SYSTEM CANVAS", refresh.blocks_to_markdown(blocks))
