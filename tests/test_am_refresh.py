"""Phase AM tests — the pure agent-memory module: sentinel parse/roundtrip, the
body ⇄ blocks mapping, registry derivation, digest shape + cap, and the refresh
cadence (fire-on-first, thresholds, failure retry, in-flight reservation).
No MCP runtime, no network — same posture as every other suite here.
"""
from __future__ import annotations

import json
import threading
import typing
import unittest

from june_mcp import refresh
from june_mcp.refresh import (
    DocInfo,
    RefreshState,
    blocks_to_markdown,
    build_digest,
    doc_from_page,
    make_sentinel,
    markdown_to_blocks,
    parse_sentinel,
    valid_name,
)


def _doc(name="house-rules", kind="doc", when="", pinned=False, body="Always read first.",
         page_id="p1", updated="2026-08-24T00:00:00Z") -> DocInfo:
    return DocInfo(name=name, kind=kind, title=name, when_to_use=when, pinned=pinned,
                   page_id=page_id, updated_at=updated, body=body)


class TestSentinel(unittest.TestCase):
    def test_roundtrip(self) -> None:
        text = make_sentinel("dev-practices", "skill",
                             when_to_use="before fixing any bug", pinned=False, v=3)
        meta = parse_sentinel(text)
        self.assertEqual(meta, {"name": "dev-practices", "kind": "skill",
                                "when_to_use": "before fixing any bug",
                                "pinned": False, "v": 3})

    def test_non_sentinel_texts_are_none(self) -> None:
        for text in ("", "plain prose", '{"__june_view__": 1}',
                     '{"__june_agent_doc__": 1}',                  # no name
                     '{"__june_agent_doc__": 1, "name": "Bad Name"}',   # invalid slug
                     '__june_agent_doc__ but not json'):
            self.assertIsNone(parse_sentinel(text), text)

    def test_unknown_kind_normalizes_to_doc(self) -> None:
        meta = parse_sentinel(json.dumps(
            {"__june_agent_doc__": 1, "name": "x", "kind": "wizardry"}))
        self.assertEqual(meta["kind"], "doc")

    def test_name_validation(self) -> None:
        for good in ("a", "dev-practices", "ship.ops_2", "x" * 64):
            self.assertTrue(valid_name(good), good)
        for bad in ("", "-lead", "Has Caps", "spaces no", "x" * 65, "é"):
            self.assertFalse(valid_name(bad), bad)


class TestBodyBlocks(unittest.TestCase):
    def test_markdown_to_blocks_structures(self) -> None:
        md = ("# Title\n\nA paragraph\nstill the same paragraph.\n\n"
              "- one\n- two\n\n1. first\n\n- [ ] open\n- [x] done\n\n> quoted\n\n"
              "---\n\n```\ncode here\n```")
        types = [b["block_type"] for b in markdown_to_blocks(md)]
        self.assertEqual(types, ["heading_1", "paragraph", "bulleted", "bulleted",
                                 "numbered", "todo", "todo_done", "quote",
                                 "divider", "code"])

    def test_roundtrip_is_stable(self) -> None:
        md = "# T\n\npara one\n\n- a\n- b\n\n> q\n\n```\nx = 1\n```"
        blocks = [{**b, "order": float(i + 1)}
                  for i, b in enumerate(markdown_to_blocks(md))]
        again = blocks_to_markdown(blocks)
        self.assertEqual(again, md)
        # And a second pass is byte-identical (idempotent).
        blocks2 = [{**b, "order": float(i + 1)}
                   for i, b in enumerate(markdown_to_blocks(again))]
        self.assertEqual(blocks_to_markdown(blocks2), md)

    def test_sentinels_never_leak_into_body(self) -> None:
        blocks = [
            {"block_type": "paragraph", "text": make_sentinel("x", "doc"), "order": 1.0},
            {"block_type": "paragraph", "text": '{"__june_layout__": 1, "pos": {}}', "order": 2.0},
            {"block_type": "paragraph", "text": "real content", "order": 3.0},
        ]
        self.assertEqual(blocks_to_markdown(blocks), "real content")


class TestDocFromPage(unittest.TestCase):
    def _detail(self, first_text: str) -> dict:
        return {"page_id": "p9", "updated_at": "2026-08-24T01:00:00Z", "blocks": [
            {"block_type": "paragraph", "text": first_text, "order": 1.0},
            {"block_type": "paragraph", "text": "the body", "order": 2.0}]}

    def test_sentinel_first_block_makes_a_doc(self) -> None:
        info = doc_from_page({"page_id": "p9", "title": "⚙ House rules"},
                             self._detail(make_sentinel("house-rules", "doc", pinned=True)))
        self.assertIsNotNone(info)
        self.assertEqual((info.name, info.kind, info.pinned), ("house-rules", "doc", True))
        self.assertEqual(info.body, "the body")     # sentinel stripped
        self.assertEqual(info.title, "⚙ House rules")

    def test_ordinary_page_is_invisible(self) -> None:
        self.assertIsNone(doc_from_page({"page_id": "p9"}, self._detail("just prose")))

    def test_sentinel_not_first_is_invisible(self) -> None:
        detail = {"page_id": "p9", "blocks": [
            {"block_type": "paragraph", "text": "prose first", "order": 1.0},
            {"block_type": "paragraph", "text": make_sentinel("x", "doc"), "order": 2.0}]}
        self.assertIsNone(doc_from_page({"page_id": "p9"}, detail))


class _FakePages:
    """Just enough client for derive_registry: list_pages + get_page."""

    def __init__(self, pages: list[tuple[dict, dict]], has_more: bool = False) -> None:
        self._rows = pages
        self._has_more = has_more
        self.gets = 0

    def list_pages(self, *, limit: int = 200, offset: int = 0):
        return {"pages": [row for row, _ in self._rows][:limit],
                "has_more": self._has_more}

    def get_page(self, page_id: str):
        self.gets += 1
        for row, detail in self._rows:
            if row["page_id"] == page_id:
                return detail
        raise KeyError(page_id)


def _page(pid: str, name: str | None, *, kind="doc", pinned=False, body="b") -> tuple[dict, dict]:
    blocks = []
    if name is not None:
        blocks.append({"block_type": "paragraph",
                       "text": make_sentinel(name, kind, pinned=pinned), "order": 1.0})
    blocks.append({"block_type": "paragraph", "text": body, "order": 2.0})
    return ({"page_id": pid, "title": name or pid},
            {"page_id": pid, "updated_at": "t", "blocks": blocks})


class TestRegistry(unittest.TestCase):
    def test_only_sentinel_pages_and_duplicate_names_noted(self) -> None:
        fake = _FakePages([_page("p1", "a"), _page("p2", None),
                           _page("p3", "b", kind="skill"), _page("p4", "a")])
        docs, notes = refresh.derive_registry(fake)
        self.assertEqual([d.name for d in docs], ["a", "b"])
        self.assertIn("a", notes.get("duplicates", ""))

    def test_truncation_is_noted_never_silent(self) -> None:
        fake = _FakePages([_page("p1", "a")], has_more=True)
        _, notes = refresh.derive_registry(fake, max_pages=1)
        self.assertIn("truncated", notes)


class TestDigest(unittest.TestCase):
    def test_no_docs_no_digest(self) -> None:
        self.assertIsNone(build_digest([]))

    def test_shape(self) -> None:
        docs = [_doc("rules", pinned=True, body="PINNED BODY"),
                _doc("fix-class", kind="skill", when="before fixing any bug"),
                _doc("ship-ops", body="deploy runbook first line\nmore")]
        d = build_digest(docs)
        self.assertEqual([p["name"] for p in d["pinned"]], ["rules"])
        self.assertEqual(d["pinned"][0]["body"], "PINNED BODY")
        self.assertEqual(d["skills"], [{"name": "fix-class",
                                        "when_to_use": "before fixing any bug"}])
        self.assertEqual(d["docs"], [{"name": "ship-ops",
                                      "one_liner": "deploy runbook first line"}])
        self.assertIn("as_of", d)
        self.assertIn("june_doc_get", d["note"])

    def test_cap_shrinks_pinned_bodies_with_a_naming_tail(self) -> None:
        docs = [_doc("big", pinned=True, body="x" * 10_000),
                _doc("s", kind="skill", when="w")]
        d = build_digest(docs, cap_chars=1000)
        self.assertLessEqual(len(json.dumps(d)), 1000)
        self.assertIn("june_doc_get('big')", d["pinned"][0]["body"])
        self.assertEqual(len(d["skills"]), 1)      # listing survives the squeeze


class TestCadence(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 0.0
        self.state = RefreshState(calls=3, minutes=10.0, clock=lambda: self.now)

    def test_fires_on_first_call_then_quiet(self) -> None:
        self.assertTrue(self.state.tick())
        self.state.fired()
        self.assertFalse(self.state.tick())
        self.assertFalse(self.state.tick())

    def test_call_threshold(self) -> None:
        self.state.tick(); self.state.fired()
        self.assertFalse(self.state.tick())        # count 1
        self.assertFalse(self.state.tick())        # count 2
        self.assertTrue(self.state.tick())         # count 3 == calls
        self.state.fired()
        self.assertFalse(self.state.tick())

    def test_time_threshold(self) -> None:
        self.state.tick(); self.state.fired()
        self.now += 599.0
        self.assertFalse(self.state.tick())
        self.now += 2.0                            # past 600 s
        self.assertTrue(self.state.tick())

    def test_failure_retries_soon_not_next_interval(self) -> None:
        self.state.tick(); self.state.failed(retry_seconds=60.0)
        self.now += 59.0
        self.assertFalse(self.state.tick())
        self.now += 2.0
        self.assertTrue(self.state.tick())

    def test_tick_reserves_the_build(self) -> None:
        # Two concurrent due ticks must yield exactly ONE True (no stampede).
        self.assertTrue(self.state.tick())
        self.assertFalse(self.state.tick())        # reserved by the first
        self.state.fired()

    def test_thread_hammer_yields_single_reservation(self) -> None:
        state = RefreshState(calls=1000, minutes=1000.0)  # only first-call fires
        hits: list[bool] = []
        barrier = threading.Barrier(8)

        def worker() -> None:
            barrier.wait()
            hits.append(state.tick())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(hits), 1)


if __name__ == "__main__":
    unittest.main()


class TestConfigKnobs(unittest.TestCase):
    """The five Phase-AM env knobs parse fail-closed like every other knob."""

    BASE: typing.ClassVar[dict[str, str]] = {
        "JUNE_BASE_URL": "http://x", "JUNE_API_KEY": "k", "JUNE_CANVAS": "w"}

    def _load(self, **extra):
        from june_mcp.runtime import load_config
        return load_config({**self.BASE, **extra})

    def test_defaults(self) -> None:
        cfg = self._load()
        self.assertEqual((cfg.docs_canvas, cfg.docs_refresh, cfg.docs_refresh_calls,
                          cfg.docs_refresh_minutes, cfg.docs_digest_chars),
                         ("agent_docs", True, 12, 10.0, 2000))

    def test_overrides(self) -> None:
        cfg = self._load(JUNE_DOCS_CANVAS="team_docs", JUNE_DOCS_REFRESH="0",
                         JUNE_DOCS_REFRESH_CALLS="5", JUNE_DOCS_REFRESH_MINUTES="2.5",
                         JUNE_DOCS_DIGEST_CHARS="4000")
        self.assertEqual((cfg.docs_canvas, cfg.docs_refresh, cfg.docs_refresh_calls,
                          cfg.docs_refresh_minutes, cfg.docs_digest_chars),
                         ("team_docs", False, 5, 2.5, 4000))

    def test_bad_values_fail_closed_all_at_once(self) -> None:
        from june_mcp.runtime import ConfigError
        with self.assertRaises(ConfigError) as ctx:
            self._load(JUNE_DOCS_REFRESH_CALLS="abc",
                       JUNE_DOCS_REFRESH_MINUTES="-3",
                       JUNE_DOCS_DIGEST_CHARS="50")
        msg = str(ctx.exception)
        for env in ("JUNE_DOCS_REFRESH_CALLS", "JUNE_DOCS_REFRESH_MINUTES",
                    "JUNE_DOCS_DIGEST_CHARS"):
            self.assertIn(env, msg)


class TestScanBudget(unittest.TestCase):
    """The injection-path registry scan is wall-clock bounded: a docs canvas of
    slow pages must not stall the tool call the digest rides on."""

    def test_budget_stops_the_scan_and_notes_it(self) -> None:
        now = [0.0]

        class SlowPages(_FakePages):
            def get_page(self, page_id: str):
                now[0] += 2.0                       # each page read costs 2s
                return super().get_page(page_id)

        fake = SlowPages([_page(f"p{i}", f"d{i}") for i in range(10)])
        docs, notes = refresh.derive_registry(
            fake, budget_seconds=5.0, clock=lambda: now[0])
        self.assertLess(len(docs), 10)              # stopped early…
        self.assertGreaterEqual(len(docs), 2)       # …but read what fit the budget
        self.assertIn("budget", notes)
        self.assertIn("june_docs_refresh", notes["budget"])

    def test_no_budget_scans_everything(self) -> None:
        fake = _FakePages([_page(f"p{i}", f"d{i}") for i in range(10)])
        docs, notes = refresh.derive_registry(fake)
        self.assertEqual(len(docs), 10)
        self.assertNotIn("budget", notes)
