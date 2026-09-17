"""Server instructions generated PER POSTURE and PER SURFACE (design §8.4; N4).

0.4.1 sent the same 7.6k-char text to every connection, including paragraphs that teach
``june_page_write`` to read-only and free connections that cannot call it. Here the text is a list
of paragraphs, each tagged with the tools it teaches; a paragraph is rendered only when at least one
of those tools is on the connection's surface, and every tool name inside it is spelled through one
``display`` formatter (``june_page_get`` on full; ``june_page_read(op='get')`` on compact).

The paragraphs ARE ``prompts.SERVER_INSTRUCTIONS`` split on blank lines, so the full/Pro/rw
rendering is byte-identical to the literal by construction (a test pins it); nothing is copied.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from june_mcp.prompts import SERVER_INSTRUCTIONS, SERVER_INSTRUCTIONS_LEAN

_TOOL_RE = re.compile(r"june_[a-z_]+")
_PAGE_AUTHORING = frozenset({"june_page_create", "june_page_write", "june_page_append",
                             "june_page_update"})
_DOCS = frozenset({"june_docs_refresh", "june_doc_get", "june_doc_save", "june_learn"})
_REPO_SYNC = frozenset({"june_docs_export", "june_page_export", "june_page_import"})

# Paragraph index → tools that must be on the surface for the paragraph to be sent (ANY of them).
# An empty set means "always". Kept next to the text's structure: a test asserts the paragraph
# count and that every tool name a paragraph mentions is consistent with its tag.
NEEDS: tuple[frozenset[str], ...] = (
    frozenset(),          # 0 what June is; the read verbs
    _PAGE_AUTHORING,      # 1 composing pages: controls, media, views, layouts
    _PAGE_AUTHORING,      # 2 be proactive — offer to build a page
    _PAGE_AUTHORING,      # 3 quality bar for pages
    _PAGE_AUTHORING,      # 4 semantic styling
    _PAGE_AUTHORING,      # 5 READ BEFORE YOU WRITE (page_write / append / update)
    frozenset(),          # 6 canvas targeting is per call (every surface)
    _DOCS,                # 7 standing docs and skills
    _REPO_SYNC,           # 8 repo sync (opt-in tools)
)


def paragraphs() -> list[str]:
    return SERVER_INSTRUCTIONS.split("\n\n")


def mentions(text: str) -> set[str]:
    """Tool names a paragraph spells out (``june_sync__`` is a sentinel, not a tool)."""
    return {m for m in _TOOL_RE.findall(text) if not m.endswith("__")}


def render(visible: Iterable[str], *, display: Callable[[str], str] | None = None,
           profile: str = "full", extra: str = "") -> str:
    """The instructions for a surface: ``visible`` = tool names on it; ``display`` rewrites a
    member name to how it is called on this surface; ``extra`` is appended as its own paragraph
    (the compact surface's alias note)."""
    vis = set(visible)
    if profile == "lean":
        text = SERVER_INSTRUCTIONS_LEAN
    else:
        paras = paragraphs()
        if len(paras) != len(NEEDS):  # pragma: no cover - the text was edited without its tags
            raise RuntimeError(f"SERVER_INSTRUCTIONS has {len(paras)} paragraphs, NEEDS has {len(NEEDS)}")
        keep = [p for p, needs in zip(paras, NEEDS) if not needs or (needs & vis)]
        text = "\n\n".join(keep)
    if display is not None:
        text = _TOOL_RE.sub(lambda m: m.group(0) if m.group(0).endswith("__") else display(m.group(0)), text)
    if extra:
        text = text + "\n\n" + extra
    return text


__all__ = ["NEEDS", "mentions", "paragraphs", "render"]
