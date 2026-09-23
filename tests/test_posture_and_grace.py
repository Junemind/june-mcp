"""B3 (the server states what it advertises) and B4 (a downgrade waits one interval).

B3 exists because of a specific, measured failure. On 2026-09-19 the frozen connector
advertised `june_remember(supersedes=...)` and the HOST served a tool list cached from
before the freeze, so the argument was dropped in transit and the write still returned a
normal success receipt. A connector toggle did not clear it; neither did an app restart.
No server-side notification can fix that, because the stale copy is on the other side —
but the digest is rebuilt server-side on every firing, so it CAN carry what this server
believes it serves, and an agent can compare.

B4 is the opposite risk from B1's. B1 fails open when whoami is unreachable. B4 covers the
whoami that answers, and answers wrong — a billing write, a token refresh or replica lag
reporting "free" for one reading and stripping a paying user's tools mid-session.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from june_mcp.server import _pro_grace, grace_decision
from june_mcp.surfaces import build_surface
from june_mcp.tools import _posture


class ThePosture(unittest.TestCase):
    def _p(self, **kw):
        base = dict(readonly=False, pro=True, profile="compact", absent=frozenset())
        return _posture(**{**base, **kw})

    def test_it_reports_what_the_server_actually_advertises(self):
        p = self._p()
        # FX N7 (S5): the list is build_surface — on compact, families fold their members. This
        # assertion used to compare with visible_tools (members), which pinned the bug.
        self.assertEqual(p["tools_advertised"],
                         len(build_surface("compact", readonly=False, pro=True,
                                           absent=frozenset())),
                         "the count must come from the same function the list does, or the "
                         "posture becomes a second opinion that can drift")

    def test_a_free_connection_advertises_fewer_than_a_pro_one(self):
        self.assertLess(self._p(pro=False)["tools_advertised"],
                        self._p(pro=True)["tools_advertised"])

    def test_readonly_advertises_fewer(self):
        self.assertLess(self._p(readonly=True)["tools_advertised"],
                        self._p(readonly=False)["tools_advertised"])

    def test_the_tier_and_posture_are_stated_not_implied(self):
        p = self._p(pro=False, readonly=True, profile="lean")
        self.assertIs(p["pro"], False)
        self.assertIs(p["readonly"], True)
        self.assertEqual(p["profile"], "lean")

    def test_engine_absent_appears_only_when_something_is_absent(self):
        self.assertNotIn("engine_absent", self._p())
        self.assertEqual(self._p(absent=frozenset({"june_page_read"}))["engine_absent"],
                         ["june_page_read"])

    def test_the_check_names_the_failure_and_the_remedy(self):
        # The whole value of B3 is that an agent reading this knows what a mismatch MEANS.
        check = self._p()["check"].lower()
        for phrase in ("tools_advertised", "cach", "silently", "reconnect"):
            self.assertIn(phrase, check, phrase)


class TheGraceFlag(unittest.TestCase):
    def test_off_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(_pro_grace(), "B4 is behaviour-changing; it ships at 0")

    def test_the_usual_truthy_spellings(self):
        for v in ("1", "true", "TRUE", "yes", "on"):
            with mock.patch.dict(os.environ, {"JUNE_PRO_GRACE": v}, clear=True):
                self.assertTrue(_pro_grace(), v)

    def test_anything_else_is_off(self):
        for v in ("", "  ", "0", "false", "maybe", "grace"):
            with mock.patch.dict(os.environ, {"JUNE_PRO_GRACE": v}, clear=True):
                self.assertFalse(_pro_grace(), repr(v))


class TheGraceStateMachine(unittest.TestCase):
    def test_grace_off_downgrades_immediately(self):
        self.assertEqual(
            grace_decision(current_pro=True, reported_pro=False, held=False, grace=False),
            (True, False), "with the flag off this must be today's behaviour exactly")

    def test_the_first_free_reading_is_held(self):
        self.assertEqual(
            grace_decision(current_pro=True, reported_pro=False, held=False, grace=True),
            (False, True))

    def test_a_second_free_reading_applies_the_downgrade(self):
        self.assertEqual(
            grace_decision(current_pro=True, reported_pro=False, held=True, grace=True),
            (True, False), "a held state that never clears would strand a real downgrade")

    def test_recovering_to_pro_clears_the_hold(self):
        self.assertEqual(
            grace_decision(current_pro=True, reported_pro=True, held=True, grace=True),
            (True, False))

    def test_an_upgrade_is_never_delayed(self):
        # The asymmetry is the design: delaying a tier someone just paid for protects
        # against nothing.
        self.assertEqual(
            grace_decision(current_pro=False, reported_pro=True, held=False, grace=True),
            (True, False))

    def test_a_free_connection_staying_free_is_not_held(self):
        self.assertEqual(
            grace_decision(current_pro=False, reported_pro=False, held=False, grace=True),
            (True, False), "there is nothing to grant grace to")

    def test_the_hold_costs_at_most_one_interval(self):
        # Walk the machine the way the refresh loop actually does. `current_pro` must
        # follow set_pro: once the downgrade applies the connection IS free, and that is
        # what stops a second hold. Holding it True here (as a first draft of this test
        # did) makes the machine oscillate hold/apply/hold/apply forever — which is a real
        # failure mode, just one the live loop cannot reach.
        current, held, applied = True, False, []
        for _ in range(4):
            apply_now, held = grace_decision(current_pro=current, reported_pro=False,
                                             held=held, grace=True)
            applied.append(apply_now)
            if apply_now:
                current = False
        self.assertEqual(applied, [False, True, True, True],
                         "exactly one interval of grace, not an indefinite reprieve")


if __name__ == "__main__":
    unittest.main()
