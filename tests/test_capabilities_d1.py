"""D1 (0.4.2): tools are listed by what the engine SERVES and what the key is ENTITLED to.

N1 walked every engine tier: 0.4.1 gated agent page authoring on ``tier == "pro"`` and locked
out pro-trial / power / team. Hosted lists 11 page-backed tools that can never succeed there.
These tests pin the resolver's contract with a fake client, tier by tier, signal by signal.
"""
from __future__ import annotations

import unittest

import httpx

from june_mcp.capabilities import NEEDS_PAGES, PRO_TIERS, Capabilities, resolve
from june_mcp.tools import TOOLS, _PRO_ONLY, run_tool, visible_tools


class _Client:
    def __init__(self, who=None, pages_status: int | None = 200, whoami_raises: bool = False):
        self._who, self._pages_status, self._raises = who or {}, pages_status, whoami_raises

    def whoami(self, canvas=None):
        if self._raises:
            raise httpx.ConnectError("down")
        return dict(self._who)

    def list_pages(self, *, limit=200, offset=0, canvas=None):
        if self._pages_status is None:
            raise httpx.ConnectError("down")
        if self._pages_status == 200:
            return {"pages": []}
        req = httpx.Request("GET", "http://x/v1/pages")
        raise httpx.HTTPStatusError("x", request=req,
                                    response=httpx.Response(self._pages_status, request=req))


# Mirrors entitlements.KNOWN_TIERS on the engine (free, pro, pro-trial, power, team).
ENGINE_TIERS = ("free", "pro", "pro-trial", "power", "team")
PRO_FEATURES = ["entities_ml", "llm_edges", "llm_extract"]


class TestEntitlement(unittest.TestCase):
    def test_every_paid_engine_tier_is_pro(self) -> None:
        for tier in ENGINE_TIERS:
            with self.subTest(tier=tier):
                caps = resolve(_Client({"tier": tier, "features": []}))
                self.assertEqual(caps.pro, tier != "free", tier)
        self.assertEqual(PRO_TIERS, frozenset(t for t in ENGINE_TIERS if t != "free"))

    def test_a_pro_feature_grants_pro_whatever_the_tier_string_says(self) -> None:
        caps = resolve(_Client({"tier": "custom-plan", "features": PRO_FEATURES}))
        self.assertTrue(caps.pro)

    def test_explicit_free_with_no_features_is_not_pro(self) -> None:
        caps = resolve(_Client({"tier": "free", "features": [], "edition_tag": "june-free"}))
        self.assertFalse(caps.pro)
        self.assertEqual(caps.tier, "free")

    def test_unreachable_whoami_fails_open(self) -> None:
        caps = resolve(_Client(whoami_raises=True))
        self.assertTrue(caps.pro); self.assertTrue(caps.pages)
        self.assertEqual(caps.source, "fail-open"); self.assertEqual(caps.absent, frozenset())

    def test_legacy_whoami_without_tier_fails_open_on_pro(self) -> None:
        self.assertTrue(resolve(_Client({})).pro)


class TestServedSurface(unittest.TestCase):
    def test_pages_404_hides_every_page_backed_tool(self) -> None:
        caps = resolve(_Client({"tier": "pro", "features": PRO_FEATURES}, pages_status=404))
        self.assertFalse(caps.pages); self.assertEqual(caps.source, "probe")
        self.assertEqual(caps.absent, NEEDS_PAGES)
        names = {t.name for t in visible_tools(pro=caps.pro, absent=caps.absent)}
        self.assertFalse(names & NEEDS_PAGES)
        self.assertIn("june_answer", names); self.assertIn("june_remember", names)
        self.assertEqual(len(names), 37 - len(NEEDS_PAGES & {t.name for t in visible_tools()}))  # S8 (2026-09-24): +7 — insert/move/rename/meta/restore, page_removed, backlinks

    def test_pages_403_or_500_still_counts_as_served(self) -> None:
        for status in (401, 403, 500):
            with self.subTest(status=status):
                self.assertTrue(resolve(_Client({"tier": "pro"}, pages_status=status)).pages)

    def test_probe_transport_failure_fails_open(self) -> None:
        caps = resolve(_Client({"tier": "pro"}, pages_status=None))
        self.assertTrue(caps.pages); self.assertEqual(caps.source, "entitlement")

    def test_engine_capabilities_list_wins_when_present(self) -> None:
        caps = resolve(_Client({"tier": "free", "features": [], "capabilities": ["pages", "agent_pages"]},
                               pages_status=404))
        self.assertTrue(caps.pro); self.assertTrue(caps.pages); self.assertEqual(caps.source, "capabilities")

    def test_absent_tools_are_refused_when_addressed_directly(self) -> None:
        with self.assertRaises(KeyError) as ctx:
            run_tool("june_page_list", client=None, absent=NEEDS_PAGES)
        self.assertIn("does not serve pages", str(ctx.exception))

    def test_needs_pages_covers_exactly_the_page_backed_registry(self) -> None:
        page_backed = {t.name for t in TOOLS
                       if t.name.startswith(("june_page_", "june_doc", "june_learn"))}
        self.assertEqual(NEEDS_PAGES, page_backed)
        self.assertTrue(_PRO_ONLY <= NEEDS_PAGES)


class TestBanner(unittest.TestCase):
    def test_banner_says_what_was_decided_and_why(self) -> None:
        self.assertIn("NO pages", Capabilities(pages=False, source="probe").banner())
        self.assertIn("not Pro", Capabilities(pro=False).banner())
