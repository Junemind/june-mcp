"""B1/B2 — the advertised surface can follow the tier, and moving it is one act.

WHY. `pro` was resolved once from /v1/whoami at startup and the surface computed once from
it, so a tier bought DURING a session stayed invisible until the connector process was
replaced. Five things hang off `pro` — the surface, the display names, the compact alias
map, the listed prompts, and the `pro=` handed to run_tool — and rebuilding them separately
is how they come to disagree: a tool listed but not callable, or callable but not listed.

DEFAULT OFF. The surface is read on the CX8 hot path, so the re-resolution is opt-in
(JUNE_SURFACE_REFRESH_SECS). With it unset these tests pin 0.4.3's exact behaviour.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from june_mcp.server import _ProDerived, _refresh_secs


def _derived(pro: bool, profile: str = "compact") -> _ProDerived:
    return _ProDerived(profile=profile, readonly=False, absent=frozenset(),
                       lean=False, compact=(profile == "compact"), pro=pro)


class TheKnob(unittest.TestCase):
    def test_off_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_refresh_secs(), 0.0,
                             "unset must mean never — 0.4.3's frozen surface, unchanged")

    def test_a_value_is_read(self):
        with mock.patch.dict(os.environ, {"JUNE_SURFACE_REFRESH_SECS": "900"}, clear=True):
            self.assertEqual(_refresh_secs(), 900.0)

    def test_garbage_falls_back_to_off_rather_than_raising(self):
        # compose names these as ${VAR:-} and float("") raised on the box 2026-09-16.
        for bad in ("", "  ", "soon", "9e"):
            with mock.patch.dict(os.environ, {"JUNE_SURFACE_REFRESH_SECS": bad}, clear=True):
                self.assertEqual(_refresh_secs(), 0.0, bad)

    def test_a_negative_cadence_is_off_not_every_call(self):
        with mock.patch.dict(os.environ, {"JUNE_SURFACE_REFRESH_SECS": "-5"}, clear=True):
            self.assertEqual(_refresh_secs(), 0.0)


class TheHolder(unittest.TestCase):
    def test_a_free_surface_differs_from_a_pro_one(self):
        free, pro = _derived(False), _derived(True)
        self.assertNotEqual({t.name for t in free.surface}, {t.name for t in pro.surface},
                            "premise: pro must actually change what is advertised")

    def test_setting_the_same_tier_reports_no_change(self):
        d = _derived(True)
        self.assertFalse(d.set_pro(True), "no change ⇒ no tools/list_changed is owed")

    def test_flipping_the_tier_reports_a_change(self):
        d = _derived(False)
        self.assertTrue(d.set_pro(True))

    def test_flipping_rebuilds_the_surface(self):
        d = _derived(False)
        before = {t.name for t in d.surface}
        d.set_pro(True)
        self.assertNotEqual(before, {t.name for t in d.surface})

    def test_every_derived_piece_moves_together(self):
        # The point of the holder: the five pieces can never describe different tiers.
        d, ref = _derived(False), _derived(True)
        d.set_pro(True)
        self.assertEqual({t.name for t in d.surface}, {t.name for t in ref.surface})
        # `disp` is a closure, so compare what it DOES, not which object it is.
        names = sorted(t.name for t in ref.surface)
        self.assertEqual([d.disp(n) for n in names], [ref.disp(n) for n in names])
        self.assertEqual(d.aliases, ref.aliases)
        self.assertEqual({p.name for p in d.prompts}, {p.name for p in ref.prompts})
        self.assertEqual(d.pro, ref.pro)

    def test_the_tier_handed_to_run_tool_follows(self):
        d = _derived(False)
        self.assertFalse(d.pro)
        d.set_pro(True)
        self.assertTrue(d.pro, "run_tool is passed derived.pro — it must not lag the surface")

    def test_it_round_trips_back_to_free(self):
        d = _derived(True)
        before = {t.name for t in d.surface}
        self.assertTrue(d.set_pro(False))
        self.assertTrue(d.set_pro(True))
        self.assertEqual(before, {t.name for t in d.surface},
                         "a pro→free→pro cycle must land exactly where it started")

    def test_truthy_values_are_normalised(self):
        d = _derived(True)
        self.assertFalse(d.set_pro(1), "1 and True are the same tier — no spurious notification")


if __name__ == "__main__":
    unittest.main()
