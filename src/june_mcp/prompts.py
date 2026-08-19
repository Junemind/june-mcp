"""June MCP prompts + the server instructions — how an agent turns a vague ask into a page.

Pure and testable like ``tools.py``: no MCP runtime here. ``server.py`` wraps ``SERVER_INSTRUCTIONS``
into the initialize handshake and ``PROMPTS`` into the prompt protocol.

Why this exists. The page tools let an agent BUILD, but a user usually says only "make me a
dashboard for project X" or "turn these notes into a page". Two things bridge that gap:

* **Instructions** (``SERVER_INSTRUCTIONS``) — a STANDING invitation, sent once at connect, that
  tells any agent it may proactively offer to build pages and how to act on a vague request
  (choose sensible defaults and say so, OR present 2-4 options and build the chosen one — the user
  never makes a tool call, and never has to specify block-by-block).
* **Prompts** (``PROMPTS``) — host-surfaced one-click starters. Each expands a layman request into
  a concrete build recipe (which block types, when to use a live view vs. static text, when to lay
  out a canvas), so the agent produces something genuinely useful, not a wall of paragraphs.

Neither can FORCE a host to act — proactivity is the host's call — but for an agent that reads
these strings (Claude does), they are exactly the levers that turn "I have page tools" into
"I offered to build the user a dashboard and did."
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


SERVER_INSTRUCTIONS = (
    "June is the user's local knowledge graph. Everything they've saved — notes, documents, "
    "people, decisions, meetings — lives here, and you can both READ it (june_answer / "
    "june_search / june_context / june_enumerate) and BUILD on it.\n"
    "\n"
    "You can compose rich PAGES in the user's current canvas: documents AND live dashboards. "
    "Pages can hold INTERACTIVE CONTROLS as plain text: '[select: A | *B | C]' renders a "
    "single-choice dropdown, '[multi: *a | b]' a multi-select ('*' = selected), and a table cell "
    "starting '[] task' a real checkbox — use them for status fields and per-row tracking. "
    "Inline markdown renders styled in block text (app 0.0.11+): **bold**, *italic*, `code`, "
    "~~strike~~, [links](url) — write naturally marked-up prose. A "
    "page holds headings, lists, to-dos, quotes, callouts, code, Markdown tables, DISPLAY-ONLY "
    "media (images/links that render in the doc but never enter the graph — so you can generate an "
    "image and drop it in), and LIVE VIEWS (a query rendered as a table/board/calendar that stays "
    "current as the graph changes). Blocks can be laid out as positioned cards to make a real "
    "dashboard. Build with june_page_create / june_page_write / june_page_append.\n"
    "\n"
    "Be proactive. When the user's material could become a page — meeting notes, a plan, a "
    "reading list, a project dashboard, a summary — OFFER to build it ('I can turn this into a "
    "dashboard / a formatted note / a checklist in June — want me to?'). When they say yes but "
    "are vague, do NOT interrogate them block-by-block: either pick a sensible structure yourself "
    "and state what you chose, or present 2-4 concrete options ('a live dashboard of open items, "
    "or a written summary?') and build the one they pick. The user only chooses or describes in "
    "plain words — YOU make every tool call.\n"
    "\n"
    "Quality bar: give pages a clear title, lead with a heading, use live views for anything that "
    "should stay current (open tasks, people on a project, recent documents) rather than freezing "
    "it as text, use to-dos for action items, and reach for a canvas layout when the content is a "
    "set of parallel cards rather than a linear read. Read the graph first (june_search / "
    "june_enumerate) so the page is grounded in what the user actually has.\n"
    "\n"
    "Make it readable at a glance with SEMANTIC styling, not decoration: give a callout a `variant` "
    "(warning reads red, a confirmation/success green, info blue, tip/note neutral) so risk and "
    "reassurance are visible without reading; flag key to-dos (high/blocked); and set a page `theme` "
    "colour to match the topic. Each carries a colour AND an icon AND a label, so it still reads in "
    "greyscale — lean on meaning (warning/success/danger), and don't colour everything.\n"
    "\n"
    "READ BEFORE YOU WRITE. You cannot see what you did not write: other agent sessions, other "
    "hosts and the user's own editor write to these pages, so a page rebuilt from memory does "
    "not overwrite their work — it omits it, and omission is deletion. That is why "
    "june_page_write REQUIRES `expected_updated_at`, the token june_page_get returns: read the "
    "page in the same turn, build the blocks from what came back, and pass the token through. A "
    "write from a stale read is refused rather than applied. Prefer june_page_append — it cannot "
    "delete a block. Use june_page_write only when the user asked to replace, rewrite or "
    "restructure; 'update' usually means add. Canvas targeting is per call (CX3): your "
    "connection has an immutable default canvas, and any call can act in another canvas by "
    "passing canvas=<name | id | canvas_handle from june_canvas_use> — nothing you do can "
    "redirect other conversations, and nothing they do can redirect you. Every canvas-scoped "
    "result echoes the canvas it landed in; check that receipt when it matters."
)


@dataclass(frozen=True, slots=True)
class PromptArg:
    name: str
    description: str
    required: bool = False


@dataclass(frozen=True, slots=True)
class Prompt:
    name: str
    description: str
    render: Callable[[dict], str]
    arguments: list[PromptArg] = field(default_factory=list)


def _arg(args: dict, key: str, default: str = "") -> str:
    v = args.get(key) if isinstance(args, dict) else None
    return str(v).strip() if v is not None and str(v).strip() else default


def _render_page(a: dict) -> str:
    topic = _arg(a, "topic", "the topic the user named")
    audience = _arg(a, "audience")
    notes = _arg(a, "notes")
    aud = f" It's for {audience}." if audience else ""
    src = (f"\n\nRaw material to work from:\n{notes}" if notes else
           "\n\nFirst read June for what it already holds on this (june_search / june_enumerate), "
           "then build from that.")
    return (
        f"Build a well-structured June page about: {topic}.{aud}\n\n"
        "Compose it with june_page_create: a clear title, a leading heading, then the right mix of "
        "headings, bulleted/numbered lists, callouts for key points, to-dos for any action items, "
        "and a Markdown table where a comparison helps. If part of it should stay current as the "
        "user's knowledge changes (e.g. related people, documents, or open items), add a LIVE VIEW "
        "block instead of freezing it as text. Choose the structure yourself and tell the user what "
        "you built; don't ask them to specify it block-by-block." + src
    )


def _render_dashboard(a: dict) -> str:
    topic = _arg(a, "topic", "the project or topic the user named")
    focus = _arg(a, "focus")
    foc = f" Emphasize: {focus}." if focus else ""
    return (
        f"Build a LIVE dashboard in June for: {topic}.{foc}\n\n"
        "This should be a canvas of cards, not a linear document. Steps: (1) read the graph "
        "(june_search / june_enumerate) to see what's actually there; (2) call june_page_create "
        "with a title and a set of blocks that are mostly LIVE VIEWS — e.g. a table of related "
        "entities, a board of decisions, a calendar of dated artifacts — plus a short heading/"
        "callout giving context; (3) pass a `layout` of mode:'canvas' with a card per block, "
        "arranged in a sensible grid with titles. Pick which views matter yourself, or offer the "
        "user a couple of layouts and build the one they choose. The views stay current as the "
        "graph changes — that's the point of a dashboard versus a snapshot."
    )


def _render_meeting_notes(a: dict) -> str:
    notes = _arg(a, "notes")
    title = _arg(a, "title", "the meeting")
    body = (f"\n\nRaw notes:\n{notes}" if notes else
            "\n\nAsk the user to paste the raw notes if they haven't, then build.")
    return (
        f"Turn these meeting notes into a clean June page titled after: {title}.\n\n"
        "Structure with june_page_create: a title, then headings for Summary, Decisions, and "
        "Action items — render each action item as a to-do block so it's checkable, and attribute "
        "owners where the notes name them. Add a callout for anything time-sensitive. If the "
        "meeting references people or projects June already knows, consider a live view linking to "
        "them. Keep the user's meaning; don't invent decisions that aren't in the notes." + body
    )


PROMPTS: list[Prompt] = [
    Prompt(
        "june_new_page",
        "Turn a topic or some material into a well-structured June page (document).",
        _render_page,
        [PromptArg("topic", "What the page is about", required=True),
         PromptArg("audience", "Who it's for (optional — shapes tone/depth)"),
         PromptArg("notes", "Raw material to build from (optional)")],
    ),
    Prompt(
        "june_dashboard",
        "Build a LIVE dashboard (a canvas of view-cards over the graph) for a project or topic.",
        _render_dashboard,
        [PromptArg("topic", "The project or topic to build a dashboard for", required=True),
         PromptArg("focus", "What to emphasize (optional)")],
    ),
    Prompt(
        "june_meeting_notes",
        "Turn raw meeting notes into a structured page (summary, decisions, checkable actions).",
        _render_meeting_notes,
        [PromptArg("notes", "The raw meeting notes"),
         PromptArg("title", "What the meeting was (optional)")],
    ),
]

_BY_NAME = {p.name: p for p in PROMPTS}


def render_prompt(name: str, arguments: dict | None = None) -> str:
    """Render one prompt's text by name (the path both the server and tests use)."""
    p = _BY_NAME.get(name)
    if p is None:
        raise KeyError(f"unknown prompt {name!r}; known: {sorted(_BY_NAME)}")
    return p.render(arguments or {})


__all__ = ["SERVER_INSTRUCTIONS", "PROMPTS", "Prompt", "PromptArg", "render_prompt"]
