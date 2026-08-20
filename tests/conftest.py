"""Shared test infrastructure.

CX9 introduced a module-global canvas-resolution cache in ``june_mcp.tools``
(``_RESOLVE``). Tests that count wire traffic (MockTransport ``seen`` dicts)
assume each test starts cold — without a reset, a resolution cached by one test
silently satisfies the next test's lookup and its traffic assertions read the
wrong world (found live 2026-08-20: two green tests went red the moment the
cache landed, for the RIGHT reason). Reset it around every test, once, here —
instead of teaching every traffic-counting test about the cache.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _cold_canvas_cache():
    try:
        from june_mcp import tools as _tools
    except Exception:                    # collection without the package importable
        yield
        return
    _tools._cache_reset()
    yield
    _tools._cache_reset()
