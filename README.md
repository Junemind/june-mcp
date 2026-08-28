# june-mcp

<!-- mcp-name: io.github.Junemind/june-mcp -->

**Give your agent a memory.** `june-mcp` is the official [MCP](https://modelcontextprotocol.io)
server for [Junê](https://june.januraine.ai) — it connects any MCP host (Claude Desktop, Claude
Code, and friends) to a June knowledge graph, so your agent can *ask*, *search*, and *remember*
against a shared, cited, tenant-isolated memory.

This package is a thin, zero-logic connector: all retrieval, graph assembly, and answering happen
on the June endpoint you point it at. No engine code lives here — which is why it's small enough
to read in one sitting.

```
Claude Desktop / Claude Code  ──stdio──▶  june-mcp  ──HTTPS──▶  your June endpoint
                                                                 (graph · retrieval · answers)
```

## Install

```bash
pip install june-mcp          # just the connector   (or: pipx install june-mcp)
pip install june-ai           # umbrella: june-mcp + june-bench (the benchmark suite)
pip install "june-bench[mcp]" # the bench, with the connector as an extra
```

## Point it at a June endpoint

`june-mcp` speaks to any June service. Three ways to have one:

1. **Junê desktop app (local-first).** Run the [Junê app](https://github.com/Junemind/June_releases)
   and connect to its local engine — your files, graph, and keys stay on your machine.
2. **Your own June service.** Pro/Team customers running the `june-local` engine package point
   `JUNE_BASE_URL` at their own server.
3. **Hosted (Team).** Point at your hosted June workspace endpoint with the API key from your
   console.

## Configure

The server is **fail-closed**: it refuses to start unless it knows where to connect and as whom,
and tells you *everything* that's missing in one message (not one error at a time).

| env | required | meaning |
|---|---|---|
| `JUNE_BASE_URL` | ✅ | Your June endpoint, e.g. `http://localhost:8000` |
| `JUNE_CANVAS` | ✅ | The canvas (workspace) to bind this connection to — a **name** (`work`) or a canvas id. Names resolve to the id at startup; ambiguous names fail closed |
| `JUNE_CANVAS_CREATE` | optional | `1` creates the named canvas on first run if it doesn't exist yet (refused in read-only mode) |
| `JUNE_API_KEY` | ✅ | Your June API key (`JUNE_ALLOW_ANON=1` explicitly opts out for keyless local setups) |
| `JUNE_LLM_KEY` | optional | **Bring-your-own LLM key** for cited answers — forwarded per-request as a header, never logged, never stored on the service |
| `JUNE_READONLY` | optional | `1` hides + refuses all write tools (memory becomes read-only) |
| `JUNE_FILES_ROOT` | optional | Opt-in directory agents may upload files from via `june_ingest_file` — unset ⇒ that tool doesn't exist |
| `JUNE_TIMEOUT_READ` / `JUNE_TIMEOUT_ANSWER` | optional | Per-verb timeouts (defaults 15 s / 120 s) |
| `JUNE_TOOL_CONCURRENCY` | optional | Max tool calls executing at once on this connection (default 8). Hosts pipeline requests over one stream; this is the explicit ceiling — excess calls queue, never stampede |
| `JUNE_DOCS_CANVAS` | optional | Canvas holding the **agent docs** (standing instructions/skills — see *Agent memory* below). Default `agent_docs`; created on the first `june_doc_save` |
| `JUNE_DOCS_REFRESH` | optional | `0` disables the periodic `standing_docs` digest (default **on** — it's the anti-forgetting safety net) |
| `JUNE_DOCS_REFRESH_CALLS` / `JUNE_DOCS_REFRESH_MINUTES` | optional | Digest cadence: due every N tool calls (default 12) **or** M minutes (default 10), whichever comes first |
| `JUNE_DOCS_DIGEST_CHARS` | optional | Serialized digest size cap (default 2000) |
| `JUNE_EXPORT_ROOT` | optional | Opt-in repo directory the agent may **export** June pages/docs into as files (see *Repo sync* below) — unset ⇒ the three repo-sync tools don't exist |
| `JUNE_EXPORT_GIT` | optional | `1` commits exactly the files each export wrote (pathspec-limited, **never pushes**) |
| `JUNE_EXPORT_DIR` | optional | Agent-docs subtree inside the root (default `docs/agent`) |
| `JUNE_LOG_LEVEL` | optional | Logging is stderr-only by design — stdout is the MCP wire |

## Check it before your agent does

```bash
JUNE_BASE_URL=http://localhost:8000 JUNE_API_KEY=... JUNE_CANVAS=work june-mcp --doctor
```

The doctor verifies, in order: config → service reachable → canvas resolution (your canvas
*name* → its id, e.g. `name "work" → 9147bee6-…`) → search seam healthy → tool manifest, and
prints PASS/FAIL per check with a mapped hint (e.g. a missing name lists the canvases that DO
exist and points at `JUNE_CANVAS_CREATE=1`). The doctor exits `0` only when every check passes
(`1` otherwise); the server itself exits `2` on a config error instead of starting half-wired.
Run the doctor first; it catches every common misconfiguration before your agent ever sees the server.

## Wire it into Claude

**Claude Desktop** — merge into `claude_desktop_config.json` (Settings → Developer):

```json
{
  "mcpServers": {
    "june": {
      "command": "june-mcp",
      "env": {
        "JUNE_BASE_URL": "http://localhost:8000",
        "JUNE_API_KEY": "your-key",
        "JUNE_CANVAS": "work",
        "JUNE_LLM_KEY": "your-llm-provider-key"
      }
    }
  }
}
```

**Claude Code:**

```bash
claude mcp add june -e JUNE_BASE_URL=http://localhost:8000 \
  -e JUNE_API_KEY=your-key -e JUNE_CANVAS=work \
  -e JUNE_LLM_KEY=your-llm-provider-key -- june-mcp
```

Fully restart the host (Cmd+Q on macOS), then check the server shows **29 tools**
(30 when you opt into `june_ingest_file` via `JUNE_FILES_ROOT`).

## The tools

| tool | what your agent gets |
|---|---|
| `june_answer` | A grounded, **cited** answer from the graph — abstains rather than guesses |
| `june_search` | Ranked evidence for a query (supports multi-hop) |
| `june_context` | An assembled context pack under a token budget |
| `june_neighborhood` | The graph around one node |
| `june_subgraph` | A bounded subgraph export |
| `june_remember` | Write a fact/note into the graph (becomes retrievable + citable immediately) |
| `june_ingest` | Structured node/edge ingestion |
| `june_enumerate` | EVERY node matching a predicate — recall-complete "list ALL X" (not top-k) |
| `june_ingest_file` | Upload one local file (pdf/docx/xlsx/csv/html/md/images/audio) from the operator-approved folder — *only exists when you set `JUNE_FILES_ROOT`* |
| `june_enrich` | **Pro:** background re-extraction of the canvas with the richer engine (idempotent; job + poll; 403 on free) |
| `june_resolve` | Maintenance: merge duplicate entities via reversible `same_as` edges (runs server-side; `strong_only=false` unlocks the semantic tier on Pro) |
| `june_docs_refresh` / `june_doc_list` / `june_doc_get` | Read the agent's **standing docs** — full digest, registry listing, one doc's body |
| `june_doc_save` / `june_doc_delete` / `june_learn` | Write them — create/replace a doc or skill, two-phase delete, append one dated lesson |

Descriptions are written for the agent (what → when → returns), and every clamped input is
*visibly* noted back to the agent instead of silently truncated.

## Agent memory — docs, skills, and the anti-forgetting digest

Long sessions forget: instructions an agent read at session start (its CLAUDE.md, your
conventions) lose force thousands of tokens later. `june-mcp` fixes this structurally.

Agents save **standing docs** into June — `kind='doc'` for durable instructions
(`pinned=true` = always in effect), `kind='skill'` for named procedures with a one-line
`when_to_use` trigger (bodies load lazily, like skills should), `kind='learnings'` for an
append-only dated log written via `june_learn`. Each doc is an **ordinary June page** in the
docs canvas (`JUNE_DOCS_CANVAS`, default `agent_docs`), marked by a small metadata block —
so you can open your agent's memory in the Junê app, read it, and edit it; the agent picks
your edits up on its next refresh.

The anti-forgetting half: on the **first tool call of every session**, and then every
12 calls or 10 minutes (tunable), the connector attaches a compact `standing_docs` digest to
an ordinary tool result — pinned bodies in full, skill trigger lines, doc one-liners. Tool
results always re-enter the model's fresh context, so the instructions can't decay the way a
system prompt does, in any MCP host, with no host cooperation. A digest that can't be built
(service busy, canvas missing) is silently skipped — it never costs the carrying call
anything. Set `JUNE_DOCS_REFRESH=0` to turn the digest off; the doc tools keep working.

**June teaches agents how to use it — from inside itself.** The first save creates the docs
canvas and seeds **`agent-memory-guide`**: the operating manual (what belongs in the system
canvas vs a workstream canvas, the three kinds and when to use each, naming, what to pin,
revision discipline, repo sync). It's listed in every registry and digest, agents read it with
`june_doc_get('agent-memory-guide')` whenever unsure — and it's an ordinary page, so edit it
and your agents follow *your* version. Before anything is saved, empty states return a `setup`
walkthrough instead of a shrug, and the `june_memory_setup` prompt has the agent interview you
and save your conventions as the first docs.

## Making June automatic — the agent depends on it without being told

"Use June" should never need saying. Three mechanisms stack to make usage automatic, each
covering the previous one's blind spot:

1. **The host hook (closes the cold start).** A server can't speak until the agent's first
   call — so install June's standing instructions into the file your host loads natively
   every session:

   ```bash
   JUNE_EXPORT_ROOT=/path/to/project june-mcp --install-instructions            # → CLAUDE.md
   JUNE_EXPORT_ROOT=/path/to/project june-mcp --install-instructions AGENTS.md  # other agents
   ```

   It's written as a managed section (your own content is never touched; re-runs update it in
   place), and it puts the june-first posture — check June before claiming ignorance, remember
   facts unprompted, learn lessons as they happen — into the system prompt itself.
2. **Proactive tool descriptions (never decay).** The core verbs' descriptions tell the model
   *when to reach for them unasked* — and descriptions are re-read on every single turn, in
   every MCP host, with no cooperation needed.
3. **The pinned `june-first` doc (re-asserts all session).** Seeded alongside the guide, it
   rides every `standing_docs` digest, so the posture is repeated mid-session exactly where
   long-context drift would otherwise erode it. Like everything seeded, it's an ordinary page —
   edit it and your agents follow your version.

What no MCP server can do — honestly — is force a host to act: an agent whose host hides
`SERVER_INSTRUCTIONS` *and* has no instruction file *and* never makes one June call stays
cold. Mechanism 1 exists precisely so that case never occurs in practice.

## Repo sync — the repo stays current with what June knows

Opt in with `JUNE_EXPORT_ROOT=<your repo>` and three more tools appear:

| tool | what it does |
|---|---|
| `june_docs_export` | Mirror every agent doc to `docs/agent/<name>.md` — the repo always holds the current standing instructions |
| `june_page_export` | Export any page to a managed file, **or** into a *managed section* spliced between markers inside an existing file (`path=KNOWHOW.md section=june-learnings`) — only the marked region is ever touched |
| `june_page_import` | The reverse: edit an exported file in your editor and import it back into its June page — agent docs keep their identity, and a stale file is **refused** rather than allowed to clobber newer knowledge |

Safety rules, all enforced in code and pinned by tests: every path is fenced inside the root
(lexical `..` check **and** symlink resolution); a file not written by june-mcp is never
overwritten; nothing is ever deleted; and with `JUNE_EXPORT_GIT=1` each export commits exactly
the files it wrote — pathspec-limited, so your staged work is never swept in, and **push never
happens**. Exported files carry frontmatter and are byte-deterministic, so an unchanged doc
re-exports to an identical file and git stays quiet.

The manifest (`.june-export.json`) makes currency checkable — two CLI modes for CI:

```bash
june-mcp --export         # sync agent docs + every managed page/section, commit if enabled
june-mcp --export-check   # write NOTHING; exit 1 if the repo has drifted from June
```

`--export-check` in CI turns "are the docs up to date?" from a hope into a failing build.

## Free vs Pro — the `june-pro` tag

`june-mcp` is one package for everyone; there is no separate "pro build". **Pro is a property
of the endpoint**, not the connector: connect to a Pro-activated June (a Pro license in the
app, a Pro key on a hosted workspace) and the same tools carry Pro-grade results: every
`june_remember` and `june_ingest_file` write runs the richer entity/edge engines automatically
(the result reports which `engine` ran), `june_resolve` upgrades to semantic matching, and
`june_enrich` backfills memories that were written on the free floor before you upgraded. The
terminal shows which world you're in: `--doctor` prints an `edition` line and the server's
startup banner tags the connection —

```
june-mcp: connected http://localhost:8000 canvas name "work" → 11d2… [june-pro]
```

The tag is read from the service's own `/v1/whoami` (the same entitlement state that gates
Pro routes server-side), so it can't disagree with what you actually get — and it's
display-only: entitlements are enforced on the service no matter what any client prints.
Older services without `/v1/whoami` simply show no tag.

## Security model

The tool surface exposes **no canvas/workspace parameter** — the workspace is bound server-side
from your connection's context, fail-closed. A cross-tenant read isn't a permission check that
could fail open; it's *unrepresentable* from the client. `JUNE_READONLY=1` adds a second fence
for read-only deployments. Your BYO LLM key rides each answer request as a header and is never
persisted or logged by the service.

## Errors

Every upstream failure maps to a typed, redacted error payload (built from exception type +
HTTP status only — never from response bodies), so the server survives anything the endpoint
throws and your agent sees a clean, actionable message.

## License

MIT. The Junê engine itself is a separate, closed-source product — this connector is the open
part, by design.
