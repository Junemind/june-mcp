"""N4 (0.4.2): instructions are generated per posture — a connection is never taught a tool it
cannot call — and the default posture is byte-identical to the literal text."""
from __future__ import annotations

from june_mcp.instructions import NEEDS, mentions, paragraphs, render
from june_mcp.prompts import SERVER_INSTRUCTIONS, SERVER_INSTRUCTIONS_LEAN
from june_mcp.server import instructions_for
from june_mcp.tools import visible_tools


def test_default_posture_is_the_literal_text_byte_for_byte() -> None:
    names = [t.name for t in visible_tools()]
    # the export paragraph needs an opt-in tool; with every tool visible the text is the literal
    everything = names + ["june_docs_export", "june_page_export", "june_page_import"]
    assert render(everything) == SERVER_INSTRUCTIONS
    assert instructions_for(profile="lean") == SERVER_INSTRUCTIONS_LEAN


def test_tags_match_the_text() -> None:
    paras = paragraphs()
    assert len(paras) == len(NEEDS) == 9
    known = {t.name for t in visible_tools()} | {"june_docs_export", "june_page_export", "june_page_import"}
    for p, needs in zip(paras, NEEDS):
        assert needs <= known
        assert mentions(p) <= known, mentions(p) - known
    # the paragraphs that teach page authoring are the tagged ones, and no untagged paragraph does
    for p, needs in zip(paras, NEEDS):
        if mentions(p) & {"june_page_write", "june_page_append", "june_page_update", "june_page_create"}:
            assert needs, p[:60]


def test_no_posture_is_taught_a_tool_it_cannot_call() -> None:
    for readonly in (False, True):
        for pro in (True, False):
            for profile in ("full", "lean"):
                names = {t.name for t in visible_tools(readonly=readonly, pro=pro, profile=profile)}
                text = instructions_for(readonly=readonly, pro=pro, profile=profile)
                for para in text.split("\n\n"):
                    taught = mentions(para)
                    # a paragraph may name a hidden tool only if it also teaches a visible one
                    assert not taught or taught & names, (readonly, pro, profile, sorted(taught - names))


def test_readonly_and_free_lose_the_page_authoring_paragraphs() -> None:
    full = instructions_for()
    ro = instructions_for(readonly=True)
    free = instructions_for(pro=False)
    assert len(ro) < len(full) and len(free) < len(full)
    assert "june_page_write" not in ro and "june_page_write" not in free
    assert "READ BEFORE YOU WRITE" in full and "Canvas targeting is per call" in ro


def test_no_pages_engine_loses_the_docs_paragraph_too() -> None:
    from june_mcp.capabilities import NEEDS_PAGES
    text = instructions_for(absent=NEEDS_PAGES)
    assert "june_docs_refresh" not in text and "june_learn" not in text
    assert "june_answer" in text


def test_display_formatter_rewrites_every_member_name() -> None:
    text = render([t.name for t in visible_tools()], display=lambda n: f"<{n}>")
    assert "<june_page_get>" in text and "june_sync__" in text
