"""DT-A5b — the `supersedes` argument is TAUGHT, not just accepted.

Things to Remember, "the teaching surfaces": a capability a connected agent cannot see
does not exist. `june_remember` gains an argument whose whole purpose is that an agent
reaches for it unprompted when a write replaces an older record — so the tool description
and the argument's own description have to say when to use it and what it does NOT do.
This test greps that teaching, so removing it fails CI rather than quietly making the
feature invisible (the mechanism the vocabulary checklist calls surface 7).
"""
from __future__ import annotations

import unittest

from june_mcp.tools import TOOLS


def _remember():
    return next(t for t in TOOLS if t.name == "june_remember")


class TheArgumentExists(unittest.TestCase):
    def test_june_remember_accepts_supersedes(self):
        props = _remember().input_schema["properties"]
        self.assertIn("supersedes", props)
        self.assertEqual(props["supersedes"]["type"], "array")
        self.assertEqual(props["supersedes"]["items"]["type"], "string")

    def test_it_is_optional(self):
        self.assertNotIn("supersedes", _remember().input_schema.get("required", []))


class TheTeachingIsThere(unittest.TestCase):
    def test_the_tool_says_WHEN_to_reach_for_it(self):
        d = _remember().description.lower()
        self.assertIn("supersedes", d)
        self.assertIn("replaces", d, "the description must say what the argument is FOR")

    def test_the_tool_says_nothing_is_deleted(self):
        # The fear this argument raises in a cautious agent is "will I lose the old note?".
        # If the description does not answer it, the argument goes unused.
        d = _remember().description.lower()
        self.assertIn("nothing is deleted", d)

    def test_the_argument_says_where_the_ids_come_from(self):
        arg = _remember().input_schema["properties"]["supersedes"]["description"].lower()
        self.assertTrue("citation" in arg or "search" in arg or "answer" in arg,
                        "an agent needs to know where a node id comes from")

    def test_the_argument_says_a_bad_id_is_an_error(self):
        arg = _remember().input_schema["properties"]["supersedes"]["description"].lower()
        self.assertIn("error", arg)


if __name__ == "__main__":                                   # pragma: no cover
    unittest.main()
