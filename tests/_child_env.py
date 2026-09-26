"""The environment a test hands a child process — an ALLOW-list, never a copy of os.environ.

Found 2026-09-26: five subprocess tests built the child env as ``os.environ`` minus ``JUNE_*``.
When one failed, pytest printed that dict as a local in the traceback — every API key in the
developer's shell, in full, in the terminal and in anything the output was pasted into. A test's
failure output must not be a place secrets can reach, so a child gets only what an interpreter
needs to start and find this package; a test adds what it is testing.
"""
from __future__ import annotations

import os

_PREFIXES = ("PATH", "PYTHON", "LANG", "LC_", "HOME", "TMPDIR", "SYSTEMROOT")


def child_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.startswith(_PREFIXES)}
    env.update(extra)
    return env
