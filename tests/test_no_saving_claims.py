"""Screen-string guard (metrics widget step 6): nothing the connector puts in front of an agent — tool
descriptions in every profile, the handshake instructions, host instructions, prompt starters, or the
per-call receipt footer — may claim a token saving. A saving exists only on a receipt that carries a
measured provider-reported pair (`saved_measured`, which june_usage returns from the engine), and
the only sentence allowed to say so names that basis."""
from __future__ import annotations

import re
import unittest

try:
    from june_mcp.prompts import HOST_INSTRUCTIONS, PROMPTS, SERVER_INSTRUCTIONS, SERVER_INSTRUCTIONS_LEAN, render_prompt
    from june_mcp.server import tool_manifest
    from june_mcp.tools import receipt_footer
    _IMPORT_OK, _IMPORT_ERR = True, ""
except Exception as exc:  # pragma: no cover
    _IMPORT_OK, _IMPORT_ERR = False, repr(exc)

# "save" as in STORE is the connector's everyday verb (june_remember saves a note); what is banned is
# a saving CLAIM: tokens saved, saved N, saves you N, N× fewer, fewer tokens, cheaper than…
_CLAIM = re.compile(
    r"(tokens?\s+saved|saved\s+\d|sav(?:es|ed|ing)\s+(?:you\s+)?\d|\d+(?:\.\d+)?\s*[x×]\s*fewer|fewer\s+(?:context\s+)?tokens"
    r"|cheaper\s+than|cuts?\s+(?:your\s+)?(?:token|context)|saves?\s+(?:you\s+)?(?:tokens|money|context))",
    re.I)
# the one measured phrase the june_usage description is allowed to carry
_ALLOWED = ("saved_measured ONLY over calls that were really measured",)


def _violations(text: str) -> list[str]:
    t = text
    for ok in _ALLOWED:
        t = t.replace(ok, "")
    return [m.group(0) for m in _CLAIM.finditer(t)]


@unittest.skipUnless(_IMPORT_OK, f"june_mcp unavailable: {_IMPORT_ERR}")
class TestNoSavingClaims(unittest.TestCase):
    def test_tool_descriptions_every_profile(self):
        for profile in ("full", "lean"):
            for readonly in (False, True):
                for t in tool_manifest(profile=profile, readonly=readonly):
                    self.assertEqual(_violations(t["description"]), [], f"{t['name']} ({profile})")

    def test_instructions_and_prompts(self):
        for name, txt in (("SERVER_INSTRUCTIONS", SERVER_INSTRUCTIONS), ("SERVER_INSTRUCTIONS_LEAN", SERVER_INSTRUCTIONS_LEAN),
                          ("HOST_INSTRUCTIONS", HOST_INSTRUCTIONS)):
            self.assertEqual(_violations(txt), [], name)
        for p in PROMPTS:
            self.assertEqual(_violations(p.description), [], p.name)
            args = {a.name: "x" for a in p.arguments}
            self.assertEqual(_violations(render_prompt(p.name, args)), [], p.name)

    def test_receipt_footer_never_claims(self):
        for rec in ({"id": "r_1", "tokens": 812, "counter": "tiktoken:cl100k_base", "verified": True, "blocks": 3, "docs": 2, "rereads": 1},
                    {"id": "r_2", "tokens": 5, "counter": "unverified:chars/4", "verified": False, "blocks": 1, "docs": 1, "rereads": 0},
                    {"id": "r_3", "tokens": 0, "counter": "", "verified": False, "blocks": 0, "docs": 0, "rereads": 0}):
            line = receipt_footer(rec)
            self.assertEqual(_violations(line), [], line)
            self.assertNotIn("saved", line.lower())

    def test_the_regex_catches_what_it_should(self):
        for bad in ("June saved you 4,193 tokens", "tokens saved: 12", "3.2× fewer context tokens", "uses fewer tokens",
                    "cheaper than re-reading", "cuts your token bill", "saves context"):
            self.assertTrue(_violations(bad), bad)
        for fine in ("Save new information into the shared graph", "a stale save is refused", "what June actually SERVED"):
            self.assertEqual(_violations(fine), [], fine)


if __name__ == "__main__":
    unittest.main()
