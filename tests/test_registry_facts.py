"""The registry facts (tool-surface design §8.1/§8.4, 0.4.2): every tool carries effect, family,
title and the derived annotations; every list and gate is derived from them; the order is fixed.
"""
from __future__ import annotations

import unittest

from june_mcp.tools import (LEAN_PROFILE, TOOLS, _DOCS_TOOL_NAMES, _PRO_ONLY, _RECEIPTED_TOOLS,
                            visible_tools)

EFFECTS = {"read", "write", "remove", "erase"}
REMOVERS = {"june_page_write", "june_page_delete", "june_doc_delete"}
ERASERS = {"june_canvas_clear", "june_canvas_delete"}
FAMILIES = {"graph": {"june_neighborhood", "june_subgraph"},
            "maintain": {"june_enrich", "june_resolve"},
            "page_read": {"june_page_list", "june_page_get"},
            "page_edit": {"june_page_create", "june_page_append", "june_page_update"},
            "canvas_read": {"june_canvas_list", "june_canvas_current", "june_canvas_use"},
            "canvas_erase": {"june_canvas_clear", "june_canvas_delete"},
            "docs_read": {"june_docs_refresh", "june_doc_list", "june_doc_get"}}


class TestFacts(unittest.TestCase):
    def test_every_tool_has_a_valid_effect_and_a_title(self) -> None:
        for t in TOOLS:
            self.assertIn(t.effect, EFFECTS, t.name)
            self.assertTrue(t.title and len(t.title) <= 40, t.name)
            self.assertEqual(t.op, t.name[len("june_"):])

    def test_effects_agree_with_the_write_fence(self) -> None:
        for t in TOOLS:
            if t.effect == "read":
                self.assertFalse(t.writes, f"{t.name}: a read must not be hidden on read-only")
        self.assertEqual({t.name for t in TOOLS if t.effect == "remove"}, REMOVERS)
        self.assertEqual({t.name for t in TOOLS if t.effect == "erase"}, ERASERS)

    def test_families_are_exactly_b_prime(self) -> None:
        got: dict[str, set[str]] = {}
        for t in TOOLS:
            if t.family:
                got.setdefault(t.family, set()).add(t.name)
        self.assertEqual(got, FAMILIES)
        # R1: one effect class per family; R2: same gates; R3: same canvas rule
        for fam, members in FAMILIES.items():
            ts = [t for t in TOOLS if t.name in members]
            self.assertEqual(len({t.effect for t in ts}), 1, fam)
            self.assertEqual(len({(t.writes, t.pro_only, t.available) for t in ts}), 1, fam)
            self.assertEqual(len({t.canvas_scoped for t in ts}), 1, fam)
            self.assertEqual(len({(t.receipted, t.docs_tool) for t in ts}), 1, fam)   # R4
        # B-prime count on full Pro rw: 7 families + singles
        full = visible_tools()
        fams = {t.family for t in full if t.family}
        singles = [t for t in full if not t.family]
        self.assertEqual(len(fams) + len(singles), 20)

    def test_derived_membership_fields_mirror_the_literal_sets(self) -> None:
        self.assertEqual({t.name for t in TOOLS if t.pro_only}, set(_PRO_ONLY))
        self.assertEqual({t.name for t in TOOLS if t.receipted}, set(_RECEIPTED_TOOLS))
        self.assertEqual({t.name for t in TOOLS if t.docs_tool}, set(_DOCS_TOOL_NAMES))
        self.assertTrue(LEAN_PROFILE <= {t.name for t in TOOLS})

    def test_annotations_derive_from_effect(self) -> None:
        for t in TOOLS:
            a = t.annotations
            self.assertEqual(a["readOnlyHint"], t.effect == "read", t.name)
            self.assertEqual(a["destructiveHint"], t.effect in ("remove", "erase"), t.name)
            self.assertFalse(a["openWorldHint"], t.name)
            self.assertEqual(a["title"], t.title)
            if t.idempotent:
                self.assertEqual(t.effect, "read", f"{t.name}: only reads are idempotent here")

    def test_tool_order_is_deterministic_and_flagship_first(self) -> None:
        # The spec recommends a stable order for prompt-cache hits; the Claude arms of B0 read
        # ~41k cached tokens per run on the strength of it. Pin the order, not just the set.
        names = [t.name for t in visible_tools()]
        self.assertEqual(names[:5], ["june_answer", "june_search", "june_enumerate", "june_context",
                                     "june_usage"])
        self.assertEqual(names, [t.name for t in visible_tools()])
        self.assertEqual(len(names), 30)
