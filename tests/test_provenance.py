"""Provenance: the connector a child interpreter runs is the connector this suite tests.

Every subprocess test here spawns ``sys.executable -m june_mcp``. The parent process gets
this repo's ``src/`` first on sys.path (pytest's rootdir config); the child inherits none of
that and resolves ``june_mcp`` by plain import order. Whenever those two disagree, every
subprocess assertion runs against the wrong artifact — and it fails in a way that looks like
a code bug (2026-09-06: "unrecognized arguments: --install-instructions", because the engine
repo's editable install put a stale ``src/june_mcp`` ahead of this one; four earlier
sightings of the class in test_mc1/mc3 were pinned around rather than named).

This test names it. It is deliberately the FIRST thing to look at when a subprocess test
fails: a provenance failure means the environment is wrong, not the code.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

import june_mcp

_HERE = Path(june_mcp.__file__).resolve()
_SRC = _HERE.parent.parent  # <repo>/src


def _child_resolution(env: dict) -> Path:
    proc = subprocess.run(
        [sys.executable, "-c", "import june_mcp, sys; sys.stdout.write(june_mcp.__file__)"],
        capture_output=True, text=True, timeout=60, env=env, check=False)
    assert proc.returncode == 0, proc.stderr
    return Path(proc.stdout.strip()).resolve()


class TestProvenance(unittest.TestCase):
    def test_this_suite_imports_from_the_repo_source(self) -> None:
        self.assertEqual(_SRC.name, "src", f"june_mcp imported from {_HERE}, not <repo>/src")
        self.assertTrue((_SRC.parent / "pyproject.toml").exists(),
                        f"{_SRC.parent} is not the connector repo root")

    def test_a_child_interpreter_resolves_the_same_package(self) -> None:
        """The exact environment the subprocess tests use: JUNE_* stripped, PYTHONPATH as-is."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("JUNE_")}
        child = _child_resolution(env)
        self.assertEqual(child, _HERE, (
            f"\n  this suite tests : {_HERE}"
            f"\n  `-m june_mcp` runs: {child}"
            f"\n\nA different june_mcp shadows this repo in the child interpreter — usually an"
            f" editable install of another project whose src/ carries a copy (its .pth sorts"
            f" first), or a stale wheel in site-packages. Fix the environment (remove the copy,"
            f" `pip install -e .` here), do not pin around it: every subprocess test in this"
            f" suite would otherwise be reporting on the shadow."))

    def test_the_cli_entry_is_the_one_under_test(self) -> None:
        """--manifest must come from THIS package's argparse (a shadow that predates the flag
        surface fails here with the shadow's path, not with an argparse error downstream)."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("JUNE_")}
        proc = subprocess.run([sys.executable, "-m", "june_mcp", "--manifest"],
                              capture_output=True, text=True, timeout=60, env=env, check=False)
        self.assertEqual(proc.returncode, 0,
                         f"--manifest failed; child resolves june_mcp to "
                         f"{_child_resolution(env)}\n{proc.stderr}")


if __name__ == "__main__":
    unittest.main()
