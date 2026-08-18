"""A destructive page write is judged on WHAT IT REMOVES (2026-08-17 data loss).

THE INCIDENT. An agent was asked to "update" a 137-block page. It called ``june_page_write``
with a block list composed from its own context — sections it remembered authoring. A
different session had appended 31 blocks the day before. Those blocks were not in the payload,
the save is authoritative, and so they were deleted. Nothing errored: by its own contract the
call succeeded. The tool description at the time politely suggested calling ``june_page_get``
first "if you need its current content"; an agent rebuilding from memory does not believe it
needs the current content.

THE FIRST FIX WAS WRONG IN SHAPE. It demanded a revision token from the caller, which made
every rewrite pay for a read it might not need, broke every existing caller, and policed the
procedure rather than the harm. The danger was never that a step was skipped — it is that
content disappears.

WHAT REPLACED IT. The connector makes the read itself (one call it was already making for the
receipt) and uses it three ways: the page's own token guards the save, so a concurrent write is
refused rather than silently reverted and the caller passes nothing; the current blocks are
diffed against the payload, so the receipt reports what would be LOST rather than only what was
written; and a write that would lose ten or more blocks is refused, naming the first few, unless
the caller says ``force``. Carrying block ``id``s forward is now supported and is the good path
— matched blocks update in place, so ids survive and there is nothing to lose.

These tests pin the harm threshold, the identity-preserving path, the automatic concurrency
guard, the receipt, and — the subtle one — that the second save of a styled write is keyed on
the token the FIRST save issued, not the one we started with.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

from june_client import JuneClient, PageRevisionConflict  # noqa: E402
from june_mcp import tools as T  # noqa: E402


# ── a client stub that records what the tool layer actually sent ────────────────────────────
class FakeClient:
    """Records every save; serves reads from a scripted page state."""

    def __init__(self, *, blocks: int = 137, updated_at: str = "2026-08-17T04:00:00",
                 conflict_on: set[int] | None = None) -> None:
        self._blocks = blocks
        self.canvas = "test-canvas"          # run_tool stamps write results with it
        self.updated_at = updated_at
        self.conflict_on = conflict_on or set()
        self.saves: list[dict] = []
        self.reads = 0

    def get_page(self, page_id):                                   # noqa: ANN001
        self.reads += 1
        return {"page_id": str(page_id), "title": "To Be Done", "updated_at": self.updated_at,
                "blocks": [{"block_id": f"b{i}", "block_type": "paragraph", "text": f"t{i}",
                            "order": float(i + 1)} for i in range(self._blocks)]}

    def save_blocks(self, page_id, blocks, *, expected_updated_at=None, force=False):  # noqa: ANN001
        n = len(self.saves) + 1
        self.saves.append({"page_id": str(page_id), "blocks": list(blocks),
                           "expected_updated_at": expected_updated_at, "force": force})
        if n in self.conflict_on:
            raise PageRevisionConflict("page has changed since it was loaded")
        self.updated_at = f"rev-after-save-{n}"
        return {"page_id": str(page_id), "updated_at": self.updated_at,
                "blocks": [{"block_id": f"s{i}", "block_type": b.get("block_type", "paragraph"),
                            "text": b.get("text", ""), "order": float(i + 1)}
                           for i, b in enumerate(blocks)]}

    def create_page(self, title):                                  # noqa: ANN001
        return {"page_id": "new-page-id", "title": title}


BLOCKS = [{"type": "paragraph", "text": "rebuilt from memory"}]


class TestTheHarmIsWhatIsJudged(unittest.TestCase):
    def test_the_incident_shape_is_refused_and_nothing_is_written(self) -> None:
        """A rewrite from memory: the payload simply omits the section it never saw."""
        c = FakeClient(blocks=137)
        out = T.run_tool("june_page_write", c, {"page_id": "p1", "blocks": BLOCKS})
        msg = out["message"]
        self.assertEqual(out["refused"], "would_remove_blocks")
        self.assertIn("REFUSED", msg)
        self.assertIn("NOT applied", msg)
        self.assertIn("june_page_append", msg, "the refusal must name the additive verb")
        self.assertIn("`id`", msg, "and the identity-preserving path")
        self.assertEqual(c.saves, [], "a refused write must not touch the page")

    def test_a_refusal_cannot_be_mistaken_for_a_write(self) -> None:
        """It is returned rather than raised, so it must be unmistakable in the result itself."""
        c = FakeClient(blocks=137)
        out = T.run_tool("june_page_write", c, {"page_id": "p1", "blocks": BLOCKS})
        self.assertIs(out["ok"], False)
        self.assertIs(out["written"], False)
        self.assertNotIn("blocks_written", out, "the success key must be ABSENT, not zero")
        self.assertNotIn("blocks_removed", out, "nothing was removed — nothing was applied")
        self.assertEqual(out["would_remove"], 137)

    def test_the_refusal_survives_the_servers_error_redaction(self) -> None:
        """THE REGRESSION THIS EXISTS FOR. Raised as an exception, every word of this text was
        replaced by `map_error` with a generic "arguments were invalid" line — by design, since
        that function must never echo `str(exc)`. A refusal therefore travels as a RESULT, which
        the server serializes verbatim. This asserts the text reaches the wire, not merely that
        the function composed it."""
        from june_mcp.runtime import map_error

        c = FakeClient(blocks=137)
        out = T.run_tool("june_page_write", c, {"page_id": "p1", "blocks": BLOCKS})
        wire = json.dumps(out, default=str)                    # exactly what server._call sends
        self.assertIn("june_page_append", wire)
        self.assertIn("force=true", wire)
        self.assertNotIn("june_page_append", map_error(ValueError(out["message"])),
                         "if this ever passes through map_error, the guidance is lost again")

    def test_the_refusal_shows_what_would_have_gone(self) -> None:
        """A count is a number; the text is what makes someone say 'wait, not that'."""
        c = FakeClient(blocks=40)
        out = T.run_tool("june_page_write", c, {"page_id": "p1", "blocks": BLOCKS})
        self.assertIn("t0", out["message"], "the first few lost blocks are quoted")
        self.assertTrue(any("t0" in t for t in out["would_remove_first"]),
                        "and are available as data, not only inside a sentence")

    def test_ordinary_editing_is_never_blocked(self) -> None:
        """Below the threshold this must be invisible — that is the whole point of a harm test."""
        c = FakeClient(blocks=12)
        keep = [{"type": "paragraph", "text": f"t{i}"} for i in range(5)]     # 7 lost < 10
        out = T.run_tool("june_page_write", c, {"page_id": "p1", "blocks": keep})
        self.assertEqual(out["blocks_removed"], 7)
        self.assertEqual(len(c.saves), 1)

    def test_force_is_the_deliberate_wholesale_replace(self) -> None:
        c = FakeClient(blocks=137)
        out = T.run_tool("june_page_write", c,
                         {"page_id": "p1", "blocks": BLOCKS, "force": True})
        self.assertEqual(out["blocks_written"], 1)
        self.assertEqual(out["blocks_removed"], 137)

    def test_carrying_ids_forward_loses_nothing_and_is_never_refused(self) -> None:
        """The good path: read, send it back with ids, change one line. 137 blocks, 0 lost."""
        c = FakeClient(blocks=137)
        current = c.get_page("p1")["blocks"]
        payload = [{"type": b["block_type"], "text": b["text"], "id": b["block_id"]}
                   for b in current]
        payload[3]["text"] = "revised in place"
        out = T.run_tool("june_page_write", c, {"page_id": "p1", "blocks": payload})
        self.assertEqual(out["blocks_removed"], 0, "an id-matched block is updated, not lost")
        self.assertEqual(c.saves[0]["blocks"][3]["id"], current[3]["block_id"],
                         "the id must reach the service, or identity dies on every write")

    def test_a_new_or_empty_page_is_wide_open(self) -> None:
        c = FakeClient(blocks=0)
        out = T.run_tool("june_page_write", c, {"page_id": "p1", "blocks": BLOCKS})
        self.assertEqual(out["blocks_written"], 1)
        self.assertNotIn("blocks_removed", out, "nothing to remove, nothing to report")


class TestConcurrencyIsHandledForTheCaller(unittest.TestCase):
    def test_the_save_carries_the_pages_own_token_with_no_caller_input(self) -> None:
        c = FakeClient(blocks=0, updated_at="rev-live")
        T.run_tool("june_page_write", c, {"page_id": "p1", "blocks": BLOCKS})
        self.assertEqual(c.saves[0]["expected_updated_at"], "rev-live")
        self.assertFalse(c.saves[0]["force"])

    def test_a_caller_may_still_pin_a_specific_revision(self) -> None:
        c = FakeClient(blocks=0, updated_at="rev-live")
        T.run_tool("june_page_write", c,
                   {"page_id": "p1", "blocks": BLOCKS, "expected_updated_at": "rev-mine"})
        self.assertEqual(c.saves[0]["expected_updated_at"], "rev-mine")

    def test_a_racing_write_is_refused_and_says_what_to_do(self) -> None:
        c = FakeClient(blocks=0, conflict_on={1})
        out = T.run_tool("june_page_write", c, {"page_id": "p1", "blocks": BLOCKS})
        msg = out["message"]
        self.assertEqual(out["refused"], "page_changed_since_read")
        self.assertIs(out["written"], False)
        self.assertIn("NOT applied", msg)
        self.assertIn("june_page_get", msg)
        self.assertIn("do not resend", msg.lower())
        self.assertNotIn("page has changed since it was loaded", msg,
                         "the engine's own 409 body is not echoed — authored text only")

    def test_a_conflict_on_the_STYLING_save_does_not_claim_nothing_was_written(self) -> None:
        """Found by an adversarial audit. `_save_with_layout` writes twice: content, then the
        style/layout sentinel keyed on the ids the first save minted. If the SECOND save loses a
        race, the page has already been replaced — so reporting the generic conflict ("was NOT
        applied … do not resend") tells the agent something false about the user's page and
        leaves a half-written document with nobody looking for it."""
        c = FakeClient(blocks=4, conflict_on={2})
        out = T.run_tool("june_page_write", c, {
            "page_id": "p1",
            "blocks": [{"type": "callout", "text": "styled", "variant": "warning"}]})

        self.assertEqual(len(c.saves), 2, "the content save landed; only the sentinel failed")
        self.assertNotIn("refused", out, "a written page must not be reported as a refusal")
        self.assertEqual(out["blocks_written"], 1)
        self.assertIn("WRITTEN", out["warning"])
        self.assertIn("re-apply only the styling", out["warning"])
        self.assertIn("recover", out, "and the restore handle is still offered")

    def test_an_engine_that_cannot_be_read_still_writes(self) -> None:
        """Degrade to the old behaviour rather than failing a legal write on OUR added read."""
        class Blind(FakeClient):
            def get_page(self, page_id):                            # noqa: ANN001
                raise RuntimeError("no such route on this engine")

        c = Blind(blocks=0)
        out = T.run_tool("june_page_write", c, {"page_id": "p1", "blocks": BLOCKS})
        self.assertEqual(out["blocks_written"], 1)
        self.assertTrue(c.saves[0]["force"], "no token to be had, so say so explicitly")


class TestTheReceiptNamesTheDeletion(unittest.TestCase):
    def test_it_reports_what_was_removed_not_only_what_was_written(self) -> None:
        """The old receipt could say `blocks_written: 7` after destroying 198 and be truthful."""
        c = FakeClient(blocks=198)
        out = T.run_tool("june_page_write", c, {
            "page_id": "p1", "blocks": [{"type": "paragraph", "text": f"x{i}"} for i in range(7)],
            "force": True})
        self.assertEqual(out["blocks_before"], 198)
        self.assertEqual(out["blocks_removed"], 198, "none of the 198 survives this payload")
        self.assertIn("restore", out["recover"], "and the receipt says how to undo it")

    def test_a_growing_write_that_keeps_the_text_removes_nothing(self) -> None:
        c = FakeClient(blocks=3)
        keep = [{"type": "paragraph", "text": f"t{i}"} for i in range(3)]
        out = T.run_tool("june_page_write", c, {
            "page_id": "p1", "blocks": keep + [{"type": "paragraph", "text": "new"}]})
        self.assertEqual(out["blocks_removed"], 0, "re-sent text is not a loss, ids or no ids")
        self.assertNotIn("recover", out)


class TestTheTwoPhaseStyledWrite(unittest.TestCase):
    """A styled/laid-out write saves twice. Phase two must use the token phase one ISSUED."""

    def test_phase_two_uses_the_fresh_token_not_the_callers(self) -> None:
        c = FakeClient()
        T.run_tool("june_page_write", c, {
            "page_id": "p1",
            "blocks": [{"type": "callout", "text": "hi", "variant": "warning"}],
            "force": True})
        self.assertEqual(len(c.saves), 2, "styling requires the second, id-keyed save")
        self.assertEqual(c.saves[0]["expected_updated_at"], "2026-08-17T04:00:00")
        self.assertEqual(c.saves[1]["expected_updated_at"], "rev-after-save-1",
                         "reusing the caller's token here would 409 against our own first save")
        self.assertFalse(c.saves[1]["force"])


class TestCreateIsTheOneLegitimateForce(unittest.TestCase):
    def test_a_page_this_call_just_created_may_save_without_a_token(self) -> None:
        c = FakeClient()
        T.run_tool("june_page_create", c, {"title": "fresh", "blocks": BLOCKS})
        self.assertTrue(c.saves[0]["force"])
        self.assertIsNone(c.saves[0]["expected_updated_at"])


# ── the client-level chokepoint, exercised over a real httpx transport ───────────────────────
def _client(handler) -> JuneClient:                                # noqa: ANN001
    return JuneClient(client=httpx.Client(transport=httpx.MockTransport(handler),
                                          base_url="http://test"))


class TestTheClientRefusesAnUnguardedSave(unittest.TestCase):
    def test_tokenless_save_raises_before_any_request(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json={})

        with self.assertRaises(ValueError) as ctx:
            _client(handler).save_blocks("p1", [{"block_type": "paragraph", "text": "x"}])
        self.assertIn("force=True", str(ctx.exception))
        self.assertEqual(calls, [], "the refusal must happen before the wire, not after")

    def test_force_is_the_named_escape_hatch(self) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json as _j
            seen.update(_j.loads(request.content))
            return httpx.Response(200, json={"page_id": "p1", "updated_at": "r1", "blocks": []})

        _client(handler).save_blocks("p1", [{"block_type": "paragraph", "text": "x"}], force=True)
        self.assertNotIn("expected_updated_at", seen, "force sends no token, deliberately")

    def test_the_token_rides_the_body_and_409_becomes_a_typed_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            import json as _j
            body = _j.loads(request.content)
            assert body["expected_updated_at"] == "t0"
            return httpx.Response(409, json={"detail": "page has changed since it was loaded"})

        with self.assertRaises(PageRevisionConflict) as ctx:
            _client(handler).save_blocks("p1", [], expected_updated_at="t0")
        self.assertIn("changed", str(ctx.exception))


class TestAppendIsGuardedToo(unittest.TestCase):
    """append_blocks is itself a read-then-write full-set save — it could clobber a racing writer."""

    def test_it_sends_the_token_from_its_own_read(self) -> None:
        sent: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            import json as _j
            if request.method == "GET":
                return httpx.Response(200, json={
                    "page_id": "p1", "updated_at": "t-read",
                    "blocks": [{"block_id": "b1", "block_type": "paragraph", "text": "old",
                                "order": 1.0}]})
            sent.append(_j.loads(request.content))
            return httpx.Response(200, json={"page_id": "p1", "updated_at": "t-new", "blocks": []})

        _client(handler).append_blocks("p1", [{"block_type": "paragraph", "text": "new"}])
        self.assertEqual(sent[0]["expected_updated_at"], "t-read")
        self.assertEqual(len(sent[0]["blocks"]), 2, "the existing block must be carried forward")

    def test_one_conflict_is_retried_against_fresh_content_then_it_gives_up(self) -> None:
        """Replaying a pure append against newer content is right; guessing twice is not."""
        state = {"gets": 0, "posts": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                state["gets"] += 1
                return httpx.Response(200, json={
                    "page_id": "p1", "updated_at": f"t{state['gets']}", "blocks": []})
            state["posts"] += 1
            return httpx.Response(409, json={"detail": "page has changed since it was loaded"})

        with self.assertRaises(PageRevisionConflict):
            _client(handler).append_blocks("p1", [{"block_type": "paragraph", "text": "n"}])
        self.assertEqual(state["posts"], 2, "exactly one retry — never an unbounded loop")
        self.assertEqual(state["gets"], 2, "the retry must RE-READ, not resend the stale merge")

    def test_an_engine_that_issues_no_token_still_appends(self) -> None:
        """Capability-gating, not version-branching: a SAFE verb must not break on an old
        server that cannot issue a revision token. The guard switches on where it can be
        honoured; appends were unguarded everywhere before this, so proceeding is the status
        quo rather than a regression."""
        sent: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            import json as _j
            if request.method == "GET":
                return httpx.Response(200, json={"page_id": "p1", "blocks": []})   # no updated_at
            sent.append(_j.loads(request.content))
            return httpx.Response(200, json={"page_id": "p1", "blocks": []})

        _client(handler).append_blocks("p1", [{"block_type": "paragraph", "text": "n"}])
        self.assertNotIn("expected_updated_at", sent[0])


if __name__ == "__main__":                                          # pragma: no cover
    unittest.main()


class TestAuthoredArgumentErrorsReachTheAgent(unittest.TestCase):
    """The other half of the flattened-message defect.

    `map_error` builds agent-visible text from the exception TYPE alone, because `str(exc)` on a
    transport failure can carry the request URL, headers or key material. That is correct and
    stays. But it also silenced the messages the TOOLS wrote — the ones an agent could act on.
    `ToolInputError` is the narrow exemption: our own argument checks, composed from literals,
    never wrapping a service response.
    """

    def test_a_missing_argument_says_which_one(self) -> None:
        from june_mcp.runtime import ToolInputError, map_error

        with self.assertRaises(ToolInputError) as ctx:
            T.run_tool("june_page_write", FakeClient(blocks=0), {"blocks": BLOCKS})
        self.assertIn("page_id", map_error(ctx.exception),
                      "the agent must be told WHICH argument, not that 'arguments were invalid'")

    def test_it_is_still_a_ValueError_for_every_existing_caller(self) -> None:
        from june_mcp.runtime import ToolInputError

        self.assertTrue(issubclass(ToolInputError, ValueError),
                        "subclassing is what keeps every existing except/assertRaises working")

    def test_a_transport_failure_is_still_redacted(self) -> None:
        """The exemption must not widen. A generic ValueError, and anything carrying service
        text, still collapses to the type-only line."""
        from june_mcp.runtime import map_error

        leaky = ValueError("http://127.0.0.1:8799/v1/pages?key=SECRET failed")
        self.assertNotIn("SECRET", map_error(leaky))
        self.assertIn("arguments were invalid", map_error(leaky))
