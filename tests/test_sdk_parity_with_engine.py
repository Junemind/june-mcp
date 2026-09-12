"""Parity: this connector's `june_client` is byte-identical to the engine's copy.

`june_client` has two homes for now — here (the connector installs on its own from PyPI)
and the engine repo (which ships the SDK with the service). THIS copy is canonical: edit
here, copy to the engine in the same change. Kept by hand until 2026-09-06, when the drift
(~240 lines: CX3, pages, PageRevisionConflict) turned out to have hidden a stale connector
fossil in the engine repo for a month. The engine's twin gate is
tests/architecture/test_sdk_parity_with_connector.py; this one runs when the connector is
checked out nested inside the engine (the normal dev layout) and skips elsewhere.
"""
from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MINE = _ROOT / "src" / "june_client"
_ENGINE = _ROOT.parent / "src" / "june_client"          # nested layout: <engine>/june-mcp
_FILES = ("__init__.py", "client.py")


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class TestSdkParity(unittest.TestCase):
    def test_engine_copy_matches_this_one(self) -> None:
        if not (_ENGINE.exists() and (_ROOT.parent / "pyproject.toml").exists()):
            self.skipTest("not nested inside the engine checkout — nothing to compare")
        drift = [f for f in _FILES if _sha(_MINE / f) != _sha(_ENGINE / f)]
        self.assertEqual(drift, [], (
            f"june_client drifted from the engine's copy in {drift}. This copy is canonical: "
            f"copy it over (cp src/june_client/{{__init__,client}}.py ../src/june_client/) in "
            f"the same change. Do not edit the engine's copy directly."))


if __name__ == "__main__":
    unittest.main()
