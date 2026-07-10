# june-mcp

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
| `JUNE_CANVAS` | ✅ | The canvas (workspace) **UUID** to bind this connection to — must already exist |
| `JUNE_API_KEY` | ✅ | Your June API key (`JUNE_ALLOW_ANON=1` explicitly opts out for keyless local setups) |
| `JUNE_LLM_KEY` | optional | **Bring-your-own LLM key** for cited answers — forwarded per-request as a header, never logged, never stored on the service |
| `JUNE_READONLY` | optional | `1` hides + refuses all write tools (memory becomes read-only) |
| `JUNE_FILES_ROOT` | optional | Opt-in directory agents may upload files from via `june_ingest_file` — unset ⇒ that tool doesn't exist |
| `JUNE_TIMEOUT_READ` / `JUNE_TIMEOUT_ANSWER` | optional | Per-verb timeouts (defaults 15 s / 120 s) |
| `JUNE_LOG_LEVEL` | optional | Logging is stderr-only by design — stdout is the MCP wire |

## Check it before your agent does

```bash
JUNE_BASE_URL=http://localhost:8000 JUNE_API_KEY=... JUNE_CANVAS=<uuid> june-mcp --doctor
```

The doctor verifies, in order: config → service reachable → search seam healthy → tool manifest,
and prints PASS/FAIL per check with a mapped hint (e.g. a 404 tells you `JUNE_CANVAS` must be an
*existing* canvas id, and how to create one). The doctor exits `0` only when every check passes
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
        "JUNE_CANVAS": "your-canvas-uuid",
        "JUNE_LLM_KEY": "your-llm-provider-key"
      }
    }
  }
}
```

**Claude Code:**

```bash
claude mcp add june -e JUNE_BASE_URL=http://localhost:8000 \
  -e JUNE_API_KEY=your-key -e JUNE_CANVAS=your-canvas-uuid \
  -e JUNE_LLM_KEY=your-llm-provider-key -- june-mcp
```

Fully restart the host (Cmd+Q on macOS), then check the server shows **10 tools**
(11 when you opt into `june_ingest_file` via `JUNE_FILES_ROOT`).

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

Descriptions are written for the agent (what → when → returns), and every clamped input is
*visibly* noted back to the agent instead of silently truncated.

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
june-mcp: connected http://localhost:8000 canvas=11d2… [june-pro]
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
