"""S9 (Wave 6, 2026-09-25) — the connector half of the instruction channel.

Before S9 any read-write connection could save a doc with pinned=true (or hand-write the
sentinel into a page) and every other agent was told, every 12 calls, "Standing instructions
… Follow them." — the channel a prompt injection needs to become permanent. The contract here:

* only a doc the USER approved (the engine's sealed ``instruction.state == "approved"``) and
  that asks for authority (pinned, or a skill) is an instruction — anywhere;
* the periodic digest carries names, approved skill triggers and a version stamp — never an
  unapproved doc's words (when_to_use, one-liner, body);
* approved bodies ride the handshake (4,000 chars, overflow named) and june_docs_refresh;
* an engine that cannot report approval gives NO instructions (fail closed), and says so;
* june-first is built-in handshake text, no longer seeded; the old exact seed is hidden;
* the posture names the key's scopes and warns when it is the app key.
"""
from __future__ import annotations

import json
import unittest

from june_mcp import refresh
from june_mcp import tools as tools_mod
from june_mcp.capabilities import resolve
from june_mcp.refresh import DocInfo, build_digest, handshake_text
from june_mcp.server import instructions_for
from june_mcp.tools import configure_docs, run_tool

from test_am_agent_docs import FakeJune, _client


def _d(name, *, kind="doc", pinned=False, when="", body="b", approval="none", pid=None, upd="t"):
    return DocInfo(name=name, kind=kind, title=name, when_to_use=when, pinned=pinned,
                   page_id=pid or f"p-{name}", updated_at=upd, body=body, approval=approval)


class TheOneTest(unittest.TestCase):
    def test_is_instruction_needs_both_a_request_and_an_approval(self) -> None:
        self.assertTrue(refresh.is_instruction(_d("a", pinned=True, approval="approved")))
        self.assertTrue(refresh.is_instruction(_d("s", kind="skill", approval="approved")))
        for d in (_d("a", pinned=True), _d("a", pinned=True, approval="changed"),
                  _d("a", pinned=True, approval="unknown"), _d("a", approval="approved"),
                  _d("l", kind="learnings", approval="approved")):
            self.assertFalse(refresh.is_instruction(d), d)


class Digest(unittest.TestCase):
    def test_an_unapproved_doc_contributes_its_name_only(self) -> None:
        evil = [_d("evil", pinned=True, body="INJECT-BODY"),
                _d("evil-skill", kind="skill", when="INJECT-WHEN", body="INJECT-BODY2"),
                _d("plain", body="INJECT-ONELINER\nmore")]
        d = build_digest(evil)
        text = json.dumps(d)
        self.assertNotIn("INJECT", text)
        self.assertEqual(d["instructions"], [])
        self.assertEqual(d["skills"], [])
        self.assertEqual([r["name"] for r in d["requested"]], ["evil", "evil-skill"])
        self.assertEqual(d["docs"], ["plain"])

    def test_changed_since_approved_is_labelled(self) -> None:
        d = build_digest([_d("rules", pinned=True, approval="changed")])
        self.assertEqual(d["requested"], [{"name": "rules", "kind": "doc",
                                           "state": "changed since approved"}])

    def test_the_version_moves_with_approval_and_edits(self) -> None:
        a = [_d("rules", pinned=True, approval="approved", upd="t1")]
        v1 = build_digest(a)["instructions_version"]
        v2 = build_digest([_d("rules", pinned=True, approval="approved", upd="t2")])["instructions_version"]
        v3 = build_digest([_d("rules", pinned=True, approval="none", upd="t1")])["instructions_version"]
        self.assertNotEqual(v1, v2)
        self.assertNotEqual(v1, v3)
        self.assertEqual(v1, build_digest(a)["instructions_version"])

    def test_an_engine_without_approval_gives_no_instructions(self) -> None:
        d = build_digest([_d("rules", pinned=True, approval="unknown"),
                          _d("s", kind="skill", when="w", approval="unknown")])
        self.assertEqual(d["note"], refresh.UNREPORTED_NOTE)
        self.assertEqual(d["instructions"], [])
        self.assertEqual(d["skills"], [])
        self.assertIsNone(d["instructions_version"])
        # Even one unreported doc poisons the verdict: never mix sources of truth.
        mixed = build_digest([_d("a", pinned=True, approval="approved"),
                              _d("b", approval="unknown")])
        self.assertEqual(mixed["instructions"], [])


class Handshake(unittest.TestCase):
    def test_approved_bodies_whole_and_the_overflow_named(self) -> None:
        docs = [_d(f"r{i}", pinned=True, approval="approved", body=f"RULE{i} " + "x" * 1700)
                for i in range(3)]
        t = handshake_text(docs)
        self.assertIn("RULE0", t)
        self.assertIn("RULE1", t)
        self.assertNotIn("RULE2", t)                 # never cut mid-rule
        self.assertIn("r2 — call june_docs_refresh", t)
        approved_part = t.split("STANDING INSTRUCTIONS")[1]
        self.assertLessEqual(len("STANDING INSTRUCTIONS" + approved_part), refresh.HANDSHAKE_CHARS + 400)

    def test_unapproved_words_never_reach_the_handshake(self) -> None:
        t = handshake_text([_d("evil", pinned=True, body="INJECT"),
                            _d("s", kind="skill", when="INJECT-WHEN", approval="changed")])
        self.assertNotIn("INJECT", t)
        self.assertIn("No standing instructions are approved yet", t)

    def test_june_first_is_built_in(self) -> None:
        self.assertIn("Use June by DEFAULT", handshake_text([]))
        self.assertNotIn("Use June by DEFAULT", handshake_text([], june_first=False))

    def test_failure_and_unreported_are_said(self) -> None:
        self.assertIn("could not be read at startup (boom)", handshake_text(None, error="boom"))
        t = handshake_text([_d("rules", pinned=True, approval="unknown", body="RULE")])
        self.assertIn(refresh.UNREPORTED_NOTE, t)
        self.assertNotIn("RULE", t)

    def test_rides_the_server_instructions_only_where_docs_tools_are(self) -> None:
        st = ([_d("rules", pinned=True, approval="approved", body="ALWAYS-TESTS")], None)
        self.assertIn("ALWAYS-TESTS", instructions_for(profile="full", standing=st))
        self.assertIn("ALWAYS-TESTS", instructions_for(profile="compact", standing=st))
        self.assertNotIn("ALWAYS-TESTS", instructions_for(profile="lean", standing=st))
        self.assertNotIn("Use June by DEFAULT", instructions_for(profile="full"))  # not read → nothing

    def test_the_users_text_is_never_respelled(self) -> None:
        st = ([_d("rules", pinned=True, approval="approved", body="use june_page_get first")], None)
        self.assertIn("use june_page_get first", instructions_for(profile="compact", standing=st))


class Seeds(unittest.TestCase):
    def test_the_exact_old_seed_is_hidden_and_an_edited_one_is_not(self) -> None:
        seed = refresh.blocks_to_markdown(refresh.markdown_to_blocks(refresh.JUNE_FIRST_SEEDED_BODIES[0]))
        self.assertTrue(refresh.is_builtin_seed(_d("june-first", pinned=True, body=seed)))
        self.assertFalse(refresh.is_builtin_seed(_d("june-first", pinned=True, body=seed + "\n\nmine")))
        self.assertFalse(refresh.is_builtin_seed(_d("other", pinned=True, body=seed)))
        self.assertIsNone(build_digest([_d("june-first", pinned=True, body=seed)]))


class ThroughTheTools(unittest.TestCase):
    def setUp(self) -> None:
        tools_mod._docs_reset()

    def tearDown(self) -> None:
        tools_mod._docs_reset()

    def test_a_pinned_save_is_a_request_and_says_so(self) -> None:
        fake = FakeJune()
        client = _client(fake)
        out = run_tool("june_doc_save", client, {"name": "rules", "text": "INJECT", "pinned": True})
        self.assertIn("NOT an instruction yet", out["approval"])
        ref = run_tool("june_docs_refresh", client, {})
        self.assertEqual(ref["instructions"], [])
        self.assertNotIn("INJECT", json.dumps(ref))
        row = next(d for d in run_tool("june_doc_list", client, {})["docs"] if d["name"] == "rules")
        self.assertFalse(row["instruction"])
        self.assertIn("not approved", row["approval"])

    def test_approved_then_edited_by_an_agent_stops_counting(self) -> None:
        fake = FakeJune()
        fake.add_canvas("agent_docs")
        pid = fake.add_doc_page("agent_docs", "rules", pinned=True, body="RULE-V1")
        fake.approve(pid)
        client = _client(fake)
        ref = run_tool("june_docs_refresh", client, {})
        self.assertEqual(ref["instructions"], [{"name": "rules", "body": "RULE-V1"}])
        out = run_tool("june_doc_save", client, {"name": "rules", "text": "RULE-V2", "pinned": True})
        self.assertIn("CHANGED an approved instruction", out["approval"])
        ref = run_tool("june_docs_refresh", client, {})
        self.assertEqual(ref["instructions"], [])
        self.assertEqual(ref["requested"][0]["state"], "changed since approved")

    def test_a_hand_made_sentinel_is_no_bypass(self) -> None:
        """The page_write path: an agent writes the marker itself, pinned — still a request."""
        fake = FakeJune()
        fake.add_canvas("agent_docs")
        fake.add_doc_page("agent_docs", "sneaky", pinned=True, body="INJECT")
        configure_docs(enabled=True)
        out = run_tool("june_answer", _client(fake), {"query": "q"})
        self.assertEqual(out["standing_docs"]["instructions"], [])
        self.assertNotIn("INJECT", json.dumps(out["standing_docs"]))

    def test_an_old_engine_fails_closed_through_the_tools(self) -> None:
        fake = FakeJune()
        fake.approvals_supported = False
        fake.add_canvas("agent_docs")
        fake.add_doc_page("agent_docs", "rules", pinned=True, body="RULE")
        ref = run_tool("june_docs_refresh", _client(fake), {})
        self.assertEqual(ref["note"], refresh.UNREPORTED_NOTE)
        self.assertEqual(ref["instructions"], [])

    def test_startup_standing(self) -> None:
        fake = FakeJune()
        self.assertEqual(tools_mod.startup_standing(_client(fake)), ([], None))   # no canvas yet
        fake.add_canvas("agent_docs")
        fake.approve(fake.add_doc_page("agent_docs", "rules", pinned=True, body="R"))
        docs, err = tools_mod.startup_standing(_client(fake))
        self.assertIsNone(err)
        self.assertEqual([d.name for d in docs], ["rules"])
        fake.fail_pages = True
        docs, err = tools_mod.startup_standing(_client(fake))
        self.assertIsNone(docs)
        self.assertIn("unreadable", err)

    def test_startup_never_presents_a_partial_read_as_whole(self) -> None:
        fake = FakeJune()
        fake.add_canvas("agent_docs")
        fake.add_doc_page("agent_docs", "rules", pinned=True, body="R")
        saved = refresh.derive_registry
        refresh.derive_registry = lambda client, budget_seconds=None, **k: ([], {"budget": "stopped"})
        try:
            docs, err = tools_mod.startup_standing(_client(fake))
        finally:
            refresh.derive_registry = saved
        self.assertIsNone(docs)
        self.assertIn("did not finish", err)


class Posture(unittest.TestCase):
    def test_the_app_key_is_named_and_warned_about(self) -> None:
        p = tools_mod._posture(readonly=False, pro=True, profile="compact", absent=frozenset(),
                               key={"role": "admin", "scopes": ["read", "write", "admin", "instructions"]})
        self.assertIn("reconnect this agent", p["key_warning"])
        q = tools_mod._posture(readonly=False, pro=True, profile="compact", absent=frozenset(),
                               key={"role": "member", "scopes": ["read", "write", "admin"]})
        self.assertNotIn("key_warning", q)
        self.assertEqual(q["key"], {"role": "member", "scopes": ["read", "write", "admin"]})

    def test_capabilities_read_role_and_scopes(self) -> None:
        class C:
            def whoami(self, canvas=None):
                return {"tier": "pro", "role": "member", "scopes": ["read", "write", "admin"]}

            def list_pages(self, *a, **k):
                return {"pages": []}
        caps = resolve(C())
        self.assertEqual(caps.key, {"role": "member", "scopes": ["read", "write", "admin"]})


class Descriptions(unittest.TestCase):
    def test_no_surface_tells_an_agent_to_follow_pinned_docs_unconditionally(self) -> None:
        from june_mcp.prompts import HOST_INSTRUCTIONS, SERVER_INSTRUCTIONS
        for text in (SERVER_INSTRUCTIONS, HOST_INSTRUCTIONS, refresh.GUIDE_DOC_BODY,
                     refresh.DIGEST_NOTE, refresh.JUNE_FIRST_BODY):
            self.assertNotIn("Follow them.", text)
            self.assertNotIn("treat it as current instructions", text)
            self.assertNotIn("as current instructions", text)
            self.assertNotIn("agents will follow what it says", text)

    def test_canvas_current_description_is_not_garbled(self) -> None:
        t = next(t for t in tools_mod.TOOLS if t.name == "june_canvas_current")
        self.assertNotIn("conversation. canvas.", t.description)


if __name__ == "__main__":
    unittest.main()
