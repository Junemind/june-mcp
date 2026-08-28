"""The Tool.available capability fence — general infrastructure, mechanism-tested.

Since june_resolve moved server-side (POST /v1/resolve) every shipped tool is
universal, so no registry entry uses `available=False` today. The FENCE stays:
a capability-absent tool must be (a) hidden from the surface and (b) refused
with a clear message when addressed directly — the same two-fence shape as the
read-only posture. Tested against a synthetic tool so the mechanism can't rot
while unused.
"""
from __future__ import annotations

import unittest

try:
    import june_mcp.tools as tools_mod
    from june_mcp.tools import TOOLS, Tool, run_tool, visible_tools
    _IMPORT_OK, _IMPORT_ERR = True, ""
except Exception as exc:  # pragma: no cover
    _IMPORT_OK, _IMPORT_ERR = False, repr(exc)


@unittest.skipUnless(_IMPORT_OK, f"june_mcp unavailable: {_IMPORT_ERR}")
class TestCapabilityFence(unittest.TestCase):
    def setUp(self) -> None:
        self._ghost = Tool("june_ghost", "synthetic capability-absent tool for the fence test",
                           lambda c, a: {"never": "reached"}, {"type": "object", "properties": {}},
                           available=False)
        TOOLS.append(self._ghost)
        tools_mod._BY_NAME[self._ghost.name] = self._ghost

    def tearDown(self) -> None:
        TOOLS.remove(self._ghost)
        tools_mod._BY_NAME.pop(self._ghost.name, None)

    def test_absent_capability_is_hidden(self) -> None:
        self.assertNotIn("june_ghost", {t.name for t in visible_tools()})
        self.assertNotIn("june_ghost", {t.name for t in visible_tools(readonly=True)})

    def test_absent_capability_is_refused_directly(self) -> None:
        with self.assertRaises(KeyError) as ctx:
            run_tool("june_ghost", client=None)
        msg = str(ctx.exception)
        self.assertIn("unavailable", msg)
        self.assertNotIn("Traceback", msg)

    def test_availability_ledger(self) -> None:
        # Regression tripwire: every availability decision is DELIBERATE and listed
        # here with its reason. june_ingest_file is the one operator-opt-in tool
        # (JUNE_FILES_ROOT allowlist — agent-driven local file reads need consent);
        # everything else is universal. Count: 11 core verbs + 7 page verbs
        # (list/get/create/write/append/update/delete — update is CX12) + 6 canvas
        # verbs (2026-08-14: list/current/use/create + two-phase clear/delete) + 6
        # Phase-AM doc verbs (docs_refresh/doc_list/doc_get/doc_save/two-phase
        # doc_delete/learn) + 3 Phase-AM2 repo-sync verbs (docs_export/
        # page_export/page_import — operator-opt-in via JUNE_EXPORT_ROOT, the
        # JUNE_FILES_ROOT consent shape: agent-driven local file writes need
        # consent) = 33. The six doc verbs are universal: they ride the ordinary
        # pages routes, so no install lacks the capability.
        import os
        shipped = [t for t in TOOLS if t.name != "june_ghost"]
        self.assertEqual(len(shipped), 33)
        for t in shipped:
            if t.name == "june_ingest_file":
                self.assertEqual(t.available,
                                 bool(os.environ.get("JUNE_FILES_ROOT", "").strip()))
            elif t.name in ("june_docs_export", "june_page_export", "june_page_import"):
                self.assertEqual(t.available,
                                 bool(os.environ.get("JUNE_EXPORT_ROOT", "").strip()))
            else:
                self.assertTrue(t.available, f"{t.name} must be universal")
