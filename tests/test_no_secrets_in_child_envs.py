"""Guard (2026-09-26): no test hands a child process a copy of os.environ minus a deny-list.

A failing subprocess test prints its locals; a deny-listed copy of the environment carries every
API key in the developer's shell into that output. Children get tests/_child_env.child_env()."""
from __future__ import annotations

import re
from pathlib import Path

_DENY_COPY = re.compile(r"os\.environ\.items\(\)\s*(?:\n\s*)?if\s+not\s+k\.startswith")


def test_no_child_env_is_a_deny_listed_copy_of_the_environment() -> None:
    here = Path(__file__).parent
    offenders = [p.name for p in sorted(here.glob("*.py"))
                 if p.name != Path(__file__).name and _DENY_COPY.search(p.read_text())]
    assert not offenders, (
        f"{offenders} build a child env as os.environ minus a deny-list; a failure then prints "
        "every secret in the shell. Use tests/_child_env.child_env() (an allow-list) instead.")
