"""Phase AM — agent memory: the docs/skills/learnings registry and the refresh digest.

June becomes the store for an agent's STANDING INSTRUCTIONS: docs (CLAUDE.md-style
standing text), skills (named procedures with a when-to-use trigger line), and
learnings (append-only dated notes). Everything here is pure and testable — no MCP
runtime, no module-level client state — the same posture as ``tools.py``.

Design invariants (AGENT_MEMORY_DESIGN.md, 2026-08-24):

* **Agent docs are ordinary June pages.** A page is an agent doc iff its FIRST
  content block is a paragraph whose exact text is the ``__june_agent_doc__``
  sentinel JSON — the same mechanism as ``__june_view__``/``__june_layout__``.
  The registry is DERIVED from the page list + that sentinel, never stored
  separately, so the connector stays a zero-logic thin client and the user can
  read and edit their agent's memory in the June app like any page.
* **The digest is the anti-forgetting channel.** Instructions read once at
  session start decay in a long chat; tool results re-enter fresh context every
  time. ``build_digest`` produces a compact, char-capped summary — pinned doc
  bodies in full (the CLAUDE.md role), skill trigger lines, doc one-liners —
  that ``tools.run_tool`` piggybacks onto results on a call/time cadence.
* **A digest failure must never hurt the carrying call.** Cadence state
  (:class:`RefreshState`) marks failures for a short retry and the tool layer
  swallows every digest exception; the carrying result is returned untouched.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass

SENTINEL_KEY = "__june_agent_doc__"
DOC_KINDS = ("doc", "skill", "learnings")

DEFAULT_DOCS_CANVAS = "agent_docs"
DEFAULT_REFRESH_CALLS = 12
DEFAULT_REFRESH_MINUTES = 10.0
DEFAULT_DIGEST_CHARS = 2000
MIN_DIGEST_CHARS = 200            # below this a digest can't even carry one doc name
RETRY_SECONDS = 60.0              # after a failed build: try again soon, not next interval
MAX_REGISTRY_PAGES = 50           # pages scanned per derivation (noted when exceeded)
DIGEST_BUILD_BUDGET_SECONDS = 6.0  # wall-clock cap for the INJECTION-path registry scan:
#                                    the digest rides someone else's tool call, so its
#                                    build must never stall that call behind N slow
#                                    page reads. Partial scans are NOTED, never silent.

MAX_DOC_NAME = 64
MAX_WHEN_TO_USE = 200
MAX_DOC_CHARS = 24_000            # body cap per doc (well under the page-save budget)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
# Any block whose text is one of the frontend sentinels (view/layout/style/agent-doc)
# is structure, not prose — never part of a doc body.
_ANY_SENTINEL_RE = re.compile(r'^\s*\{\s*"__june_[a-z_]+__"')

DIGEST_NOTE = (
    "Standing instructions from June (re-shown periodically so they stay in "
    "effect through long sessions). Follow them. `pinned` bodies apply always; "
    "`skills` are procedures — when a when_to_use matches the task, read the "
    "full body with june_doc_get(name); `docs` are available the same way.")

# ── the self-hosting manual (Phase AM3 — June teaches agents how to use it) ──
# The conventions for OPERATING the memory live IN the memory: this guide doc is
# seeded automatically the moment the docs canvas is first created, so it shows
# up in every registry listing and digest from day one, survives any host, and
# the user can edit it in the app like any page. Handshake instructions decay in
# long sessions — a doc in the store does not. Not pinned (its one-liner rides
# each digest; the body loads on demand, keeping the digest budget for the
# user's own pinned rules).
GUIDE_DOC_NAME = "agent-memory-guide"
GUIDE_DOC_WHEN = ("read this FIRST when unsure how to use June's agent memory — "
                  "where things go, what to save, naming, pinning, repo sync")
GUIDE_DOC_BODY = """# How agents use June as memory (the operating manual)

This canvas is the SYSTEM CANVAS: it holds standing instructions, not project
knowledge. Facts, decisions and session context belong in the WORKSTREAM'S own
canvas (june_remember with canvas=<that workstream>); durable instructions that
should shape every future session belong here, as agent docs.

## The three kinds — and when to reach for each

- kind=doc — standing instructions ("always write tests first", the team's
conventions). Set pinned=true ONLY for rules that must apply in every single
session: pinned bodies ride every standing_docs digest, so keep the pinned set
small and tight. Everything else stays unpinned and loads on demand.
- kind=skill — a named procedure with a one-line when_to_use trigger ("before
fixing any bug — fix the class"). The trigger rides every digest; the body
loads only when the trigger matches the task. Write the body so a cold session
can follow it: purpose, steps in order, checks that prove it worked, the
mistakes it prevents. A skill is a procedure, not a changelog — a few worked
examples at most.
- kind=learnings — an append-only dated log, written via june_learn the moment
something worth keeping happens (a fix that worked, a gotcha, a failed
approach, a decision). Save DURING the session, not when asked. Promoting a
lesson into a doc or skill is a deliberate june_doc_save, never automatic.

## Naming and hygiene

Names are lowercase slugs (a-z 0-9 . _ -) agents can address forever: pick the
concept, not the date ("release-checklist", not "notes-aug-24"). One subject
per doc. When a doc grows stale, revise it — do not create "-v2" siblings.

## The discipline that keeps this safe

- Session start: june_docs_refresh, and FOLLOW what comes back. Mid-session,
`standing_docs` fields on ordinary June results are the same instructions,
re-shown on purpose — treat them as current, never as noise.
- Never rewrite a doc from memory: june_doc_get first, revise against the
current body, pass its updated_at through as expected_updated_at. A refused
save means the doc moved — read again and merge; do not force or resend.
- These docs are pages the user reads and edits in the June app. Write for
human eyes; expect human edits to appear.

## Repo sync (when the operator enabled JUNE_EXPORT_ROOT)

After saving or revising docs: june_docs_export, so the repository's
docs tree stays current. To place a page into the repo (a runbook, KNOWHOW, a
changelog): june_page_export — with section=<name> it maintains ONLY a marked
region inside an existing human file. If the user edits an exported file,
june_page_import brings it back; a stale file is refused — re-export, merge,
import. CI can run `june-mcp --export-check` to fail the build when the repo
drifts from June.

*(Seeded by june-mcp when this canvas was created. Edit it freely — it is
yours; agents will follow what it says.)*"""

# The JUNE-FIRST posture (Phase AM4) — seeded PINNED alongside the guide, so
# "depend on June by default, without being asked" is not a hope pinned to the
# handshake but a standing instruction that rides EVERY standing_docs digest,
# all session long. Deliberately tiny (pinned bodies spend digest budget) and,
# like everything seeded, an ordinary editable page the user owns.
JUNE_FIRST_NAME = "june-first"
JUNE_FIRST_WHEN = "the default operating posture — applies to every task, every session"
JUNE_FIRST_BODY = """Use June by DEFAULT, without the user asking:
- Before answering anything about the user's work, people, projects or past
decisions: june_answer or june_search FIRST — never claim ignorance or guess
from your own memory when the graph may know.
- The user states a fact, decision or preference → june_remember it into the
workstream's canvas, in the same turn.
- You learn something worth keeping (a fix, a gotcha, a failed approach) →
june_learn it the moment it happens, not when asked.
- A lasting convention or reusable procedure emerges → june_doc_save it
(doc/skill). Follow `standing_docs` on June results as current instructions.
The user should never have to say "use June" — using it IS the job."""

# Compact setup guidance for the empty states (no docs canvas / no docs yet) —
# the moment an agent most needs to be taught and previously got one thin line.
SETUP_NOTE = (
    "SETUP — nothing saved yet. To give agents durable memory here: (1) "
    "june_doc_save your first standing rule (kind='doc'; pinned=true only for "
    "always-on rules) — the first save creates the docs canvas AND seeds "
    "'agent-memory-guide', the operating manual future sessions read; (2) save "
    "reusable procedures as kind='skill' with a one-line when_to_use trigger; "
    "(3) from then on, june_learn lessons as they happen and call "
    "june_docs_refresh at every session start. Ask the user what conventions "
    "they want remembered — that is the best first doc.")


def valid_name(name: str) -> bool:
    """A doc name is a stable slug agents can address: lowercase, starts
    alphanumeric, then ``a-z 0-9 . _ -``, at most 64 chars."""
    return bool(_NAME_RE.match(name or ""))


def make_sentinel(name: str, kind: str, when_to_use: str = "",
                  pinned: bool = False, v: int = 1) -> str:
    """The agent-doc marker block's exact text. Compact JSON, key order stable so
    a byte-diff of two sentinels is meaningful."""
    body: dict = {SENTINEL_KEY: 1, "name": name, "kind": kind}
    if when_to_use:
        body["when_to_use"] = when_to_use
    if pinned:
        body["pinned"] = True
    body["v"] = int(v)
    return json.dumps(body)


def parse_sentinel(text: str) -> dict | None:
    """The metadata dict from a sentinel block's text, or None when the text is
    not an agent-doc sentinel. Strict on shape (must be a JSON object carrying
    the key, with a valid slug name), forgiving on extras: unknown fields are
    ignored, an unknown kind normalizes to "doc" rather than hiding the page."""
    t = (text or "").strip()
    if SENTINEL_KEY not in t:
        return None
    try:
        body = json.loads(t)
    except (ValueError, TypeError):
        return None
    if not isinstance(body, dict) or not body.get(SENTINEL_KEY):
        return None
    name = str(body.get("name") or "").strip()
    if not valid_name(name):
        return None
    kind = str(body.get("kind") or "doc").strip().lower()
    if kind not in DOC_KINDS:
        kind = "doc"
    try:
        v = max(1, int(body.get("v", 1)))
    except (TypeError, ValueError):
        v = 1
    return {"name": name, "kind": kind,
            "when_to_use": str(body.get("when_to_use") or "")[:MAX_WHEN_TO_USE],
            "pinned": bool(body.get("pinned", False)), "v": v}


# ── body ⇄ blocks (deliberately small; structure is best-effort, TEXT is exact) ──
_MD_BLOCK_PREFIX = {
    "heading_1": "# ", "heading_2": "## ", "heading_3": "### ",
    "bulleted": "- ", "numbered": "1. ", "todo": "- [ ] ", "todo_done": "- [x] ",
    "quote": "> ",
}
_LIST_TYPES = {"bulleted", "numbered", "todo", "todo_done"}


def blocks_to_markdown(blocks: list[dict]) -> str:
    """A page's blocks → the doc body as markdown, sentinel/structure blocks
    removed. The inverse of :func:`markdown_to_blocks` for everything that
    function emits, so save→get→save round-trips are stable."""
    rows = sorted((b for b in blocks or [] if isinstance(b, dict)),
                  key=lambda b: float(b.get("order") or 0.0))
    out: list[str] = []
    last_listy: str | None = None                  # consecutive list items join tight
    for b in rows:
        text = str(b.get("text") or "")
        if _ANY_SENTINEL_RE.match(text):
            last_listy = None
            continue                              # metadata/layout/view — not prose
        bt = str(b.get("block_type") or "paragraph")
        if bt == "divider":
            out.append("---")
            last_listy = None
        elif bt == "code":
            out.append(f"```\n{text}\n```")
            last_listy = None
        elif bt in _MD_BLOCK_PREFIX:
            prefix = _MD_BLOCK_PREFIX[bt]
            line = "\n".join(prefix + ln for ln in text.splitlines()) or prefix.rstrip()
            listy = bt in _LIST_TYPES
            if listy and last_listy in _LIST_TYPES and out:
                out[-1] = out[-1] + "\n" + line    # one markdown list, one blank-free run
            else:
                out.append(line)
            last_listy = bt if listy else None
        elif text.strip():
            out.append(text)
            last_listy = None
    return "\n\n".join(out)


_LINE_TYPES = (
    ("### ", "heading_3"), ("## ", "heading_2"), ("# ", "heading_1"),
    ("- [x] ", "todo_done"), ("- [X] ", "todo_done"), ("- [ ] ", "todo"),
    ("- ", "bulleted"), ("* ", "bulleted"), ("> ", "quote"),
)
_NUMBERED_RE = re.compile(r"^\d+[.)]\s+")


def markdown_to_blocks(text: str) -> list[dict]:
    """Doc body markdown → page blocks ``[{block_type, text}]`` (no order — the
    caller assigns it after the sentinel). Recognizes headings, list/to-do/quote
    lines (one block per line, matching how the editor stores them), fenced code
    and ``---`` dividers; everything else groups into paragraphs on blank lines.
    Unrecognized structure degrades to paragraph text — the words always survive."""
    blocks: list[dict] = []
    para: list[str] = []
    code: list[str] | None = None

    def flush_para() -> None:
        if para:
            blocks.append({"block_type": "paragraph", "text": "\n".join(para)})
            para.clear()

    for line in (text or "").splitlines():
        if code is not None:                       # inside a fence
            if line.strip().startswith("```"):
                blocks.append({"block_type": "code", "text": "\n".join(code)})
                code = None
            else:
                code.append(line)
            continue
        stripped = line.strip()
        if stripped.startswith("```"):
            flush_para()
            code = []
            continue
        if not stripped:
            flush_para()
            continue
        if stripped == "---":
            flush_para()
            blocks.append({"block_type": "divider", "text": ""})
            continue
        for prefix, bt in _LINE_TYPES:
            if stripped.startswith(prefix):
                flush_para()
                blocks.append({"block_type": bt, "text": stripped[len(prefix):]})
                break
        else:
            if _NUMBERED_RE.match(stripped):
                flush_para()
                blocks.append({"block_type": "numbered",
                               "text": _NUMBERED_RE.sub("", stripped, count=1)})
            else:
                para.append(stripped)
    if code is not None:                           # unclosed fence: keep the text
        blocks.append({"block_type": "code", "text": "\n".join(code)})
    flush_para()
    return blocks


# ── registry (derived, never stored) ─────────────────────────────────────────
@dataclass(frozen=True)
class DocInfo:
    name: str
    kind: str
    title: str
    when_to_use: str
    pinned: bool
    page_id: str
    updated_at: str | None
    body: str
    v: int = 1


def doc_from_page(page_row: dict, detail: dict) -> DocInfo | None:
    """One listed page + its fetched detail → a :class:`DocInfo`, or None when the
    page is not an agent doc (no valid sentinel in its FIRST content block)."""
    blocks = sorted((b for b in (detail.get("blocks") or []) if isinstance(b, dict)),
                    key=lambda b: float(b.get("order") or 0.0))
    first = next((b for b in blocks if str(b.get("text") or "").strip()), None)
    if first is None:
        return None
    meta = parse_sentinel(str(first.get("text") or ""))
    if meta is None:
        return None
    return DocInfo(
        name=meta["name"], kind=meta["kind"],
        title=str(page_row.get("title") or detail.get("title") or meta["name"]),
        when_to_use=meta["when_to_use"], pinned=meta["pinned"],
        page_id=str(page_row.get("page_id") or detail.get("page_id") or ""),
        updated_at=detail.get("updated_at") or page_row.get("updated_at"),
        body=blocks_to_markdown(blocks), v=meta["v"])


def derive_registry(client, *, max_pages: int = MAX_REGISTRY_PAGES,
                    budget_seconds: float | None = None, clock=time.monotonic,
                    dedupe: bool = True) -> tuple[list[DocInfo], dict[str, str]]:
    """Scan the docs canvas ``client`` is bound to → (agent docs, notes).

    Notes surface anything silently bounded (the no-silent-caps rule): a canvas
    with more pages than ``max_pages`` says so instead of pretending coverage,
    and a scan that hits ``budget_seconds`` says how far it got. Wire cost: one
    ``list_pages`` + one ``get_page`` per listed page — bounded by ``max_pages``,
    by ``budget_seconds`` when given (the injection path passes
    DIGEST_BUILD_BUDGET_SECONDS so a digest can never stall the tool call it
    rides on), and by the refresh cadence.

    ``dedupe=False`` returns EVERY sentinel page including same-name duplicates —
    the collision-resolution path in ``june_doc_save``/``june_learn`` needs the
    full set to pick the deterministic winner (lowest page_id) that every racing
    session independently agrees on."""
    notes: dict[str, str] = {}
    deadline = None if budget_seconds is None else clock() + float(budget_seconds)
    listing = client.list_pages(limit=max_pages) or {}
    pages = listing.get("pages") or []
    if listing.get("has_more"):
        notes["truncated"] = (f"docs canvas holds more than {max_pages} pages — only "
                              f"the first {max_pages} were scanned for agent docs")
    docs: list[DocInfo] = []
    seen: set[str] = set()
    for scanned, row in enumerate(pages):
        if deadline is not None and clock() >= deadline:
            notes["budget"] = (f"registry scan stopped at its {budget_seconds:g}s time "
                               f"budget after {scanned} of {len(pages)} pages — the "
                               "digest covers what was read; june_docs_refresh runs "
                               "a full scan")
            break
        pid = str(row.get("page_id") or "")
        if not pid:
            continue
        info = doc_from_page(row, client.get_page(pid) or {})
        if info is None:
            continue
        if dedupe and info.name in seen:           # first one wins; duplicates noted
            notes.setdefault("duplicates", "")
            notes["duplicates"] = (notes["duplicates"] + " " + info.name).strip()
            continue
        seen.add(info.name)
        docs.append(info)
    return docs, notes


# ── the digest ───────────────────────────────────────────────────────────────
def _one_liner(d: DocInfo) -> str:
    if d.when_to_use:
        return d.when_to_use
    first = next((ln.strip() for ln in d.body.splitlines() if ln.strip()), "")
    return first[:120]


def _truncated_body(body: str, cap: int, name: str) -> str:
    if len(body) <= cap:
        return body
    return (body[:max(0, cap)]
            + f"… [truncated — read june_doc_get('{name}') for the rest]")


def build_digest(docs: list[DocInfo], *, cap_chars: int = DEFAULT_DIGEST_CHARS,
                 now: float | None = None) -> dict | None:
    """The ``standing_docs`` payload, or None when there are no agent docs (an
    install that never saved one gets zero noise). Hard-capped at ``cap_chars``
    of serialized JSON: pinned bodies shrink first (each keeps a tail naming the
    full read), then doc/skill rows drop from the end — the digest degrades by
    getting shorter, never by lying about what it holds."""
    if not docs:
        return None
    cap = max(MIN_DIGEST_CHARS, int(cap_chars))
    pinned = [d for d in docs if d.pinned]
    skills = [d for d in docs if d.kind == "skill" and not d.pinned]
    others = [d for d in docs if d.kind != "skill" and not d.pinned]
    as_of = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                          time.gmtime(time.time() if now is None else now))

    def render(body_cap: int, rows: int) -> dict:
        return {
            "note": DIGEST_NOTE,
            "pinned": [{"name": d.name,
                        "body": _truncated_body(d.body, body_cap, d.name)}
                       for d in pinned],
            "skills": [{"name": d.name, "when_to_use": _one_liner(d)}
                       for d in skills[:rows]],
            "docs": [{"name": d.name, "one_liner": _one_liner(d)}
                     for d in others[:rows]],
            "as_of": as_of,
        }

    body_cap, rows = MAX_DOC_CHARS, max(len(skills), len(others))
    digest = render(body_cap, rows)
    # Shrink pinned bodies (halving), then drop listing rows, until under cap.
    while len(json.dumps(digest)) > cap and body_cap > 100:
        body_cap //= 2
        digest = render(body_cap, rows)
    while len(json.dumps(digest)) > cap and rows > 3:
        rows -= 1
        digest = render(body_cap, rows)
    return digest


# ── cadence (per-process = per-session for a stdio server) ───────────────────
@dataclass(frozen=True)
class DocsConfig:
    """The agent-memory posture for this process. ``enabled`` gates ONLY the
    periodic injection — the six doc tools work regardless. Defaults to
    disabled here so tests and library callers opt in explicitly; the stdio
    server enables it from the environment (JUNE_DOCS_REFRESH, default on)."""
    enabled: bool = False
    canvas: str = DEFAULT_DOCS_CANVAS
    calls: int = DEFAULT_REFRESH_CALLS
    minutes: float = DEFAULT_REFRESH_MINUTES
    digest_chars: int = DEFAULT_DIGEST_CHARS


class RefreshState:
    """When is a digest due? Fires on the FIRST counted call (session bootstrap),
    then whenever EITHER threshold has passed since the last successful build:
    ``calls`` counted tool calls, or ``minutes`` of wall time. Failure schedules
    a short retry (RETRY_SECONDS) instead of a full quiet interval.

    Thread-safe under the CX8 worker-thread offload: the counter is a
    read-modify-write, so unlike the GIL-atomic ``_NAMES`` memo it takes a real
    lock, and an in-flight flag ensures concurrent calls can't stampede N
    digest builds out of one due moment. ``tick()`` returning True RESERVES the
    build — the caller MUST then call exactly one of ``fired()`` / ``failed()``."""

    def __init__(self, calls: int = DEFAULT_REFRESH_CALLS,
                 minutes: float = DEFAULT_REFRESH_MINUTES,
                 clock=time.monotonic) -> None:
        self.calls = max(1, int(calls))
        self.seconds = max(1.0, float(minutes) * 60.0)
        self._clock = clock
        self._lock = threading.Lock()
        self._count = 0
        self._last: float | None = None            # None → never fired → fire now
        self._building = False

    def tick(self) -> bool:
        """Count one tool call; True iff a digest build is due (and now reserved)."""
        with self._lock:
            self._count += 1
            if self._building:
                return False
            due = (self._last is None
                   or self._count >= self.calls
                   or self._clock() - self._last >= self.seconds)
            if due:
                self._building = True
            return due

    def fired(self) -> None:
        """A digest was built (or the feature is legitimately idle): full quiet interval."""
        with self._lock:
            self._count = 0
            self._last = self._clock()
            self._building = False

    def failed(self, retry_seconds: float = RETRY_SECONDS) -> None:
        """The build failed: retry soon, without hammering every call."""
        with self._lock:
            self._count = 0
            self._last = self._clock() - self.seconds + max(1.0, retry_seconds)
            self._building = False


__all__ = [
    "DEFAULT_DIGEST_CHARS",
    "DEFAULT_DOCS_CANVAS",
    "DEFAULT_REFRESH_CALLS",
    "DEFAULT_REFRESH_MINUTES",
    "DIGEST_BUILD_BUDGET_SECONDS",
    "DOC_KINDS",
    "GUIDE_DOC_BODY",
    "GUIDE_DOC_NAME",
    "GUIDE_DOC_WHEN",
    "JUNE_FIRST_BODY",
    "JUNE_FIRST_NAME",
    "JUNE_FIRST_WHEN",
    "SETUP_NOTE",
    "MAX_DOC_CHARS",
    "MAX_DOC_NAME",
    "MAX_REGISTRY_PAGES",
    "MAX_WHEN_TO_USE",
    "RETRY_SECONDS",
    "SENTINEL_KEY",
    "DocInfo",
    "DocsConfig",
    "RefreshState",
    "blocks_to_markdown",
    "build_digest",
    "derive_registry",
    "doc_from_page",
    "make_sentinel",
    "markdown_to_blocks",
    "parse_sentinel",
    "valid_name",
]
