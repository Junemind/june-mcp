# Changelog

Starts at 0.4.2. Earlier releases are in the git history and on PyPI.

## 0.4.3 — 2026-09-18

Diagnostic fix. No change to what any connection is served.

### Fixed

- **`june-mcp --manifest` reported the wrong default surface.** 0.4.2 moved the deployment default
  to `compact` but left `or "full"` hardcoded in the manifest command, so it answered 30 tools under
  their member names for a connection that serves 20 folded ones. The default now lives in one
  place, `runtime.DEFAULT_TOOL_PROFILE`, and every reader takes it from there. The wire was never
  affected — the stdio tests assert 20 by default and 30 under `JUNE_TOOL_PROFILE=full` — but
  `--manifest` is what an operator runs to see what their agent will get.
- **Two conditions read the profile against a literal.** The startup banner announced the profile on
  every ordinary connection and went silent on the unusual one, and the doctor's manifest label did
  the same. Both compare against the default now.

### Added

- A test that runs the CLI in a subprocess with no `JUNE_TOOL_PROFILE` and asserts its answer equals
  `build_surface(DEFAULT_TOOL_PROFILE)` name for name, then pins `full` and `lean` through the same
  path. Every prior test asked the library, which was correct throughout; nothing asked the CLI.

## 0.4.2 — 2026-09-18

The tool surface an agent sees is now **generated from the registry** instead of being a
hand-written list, and the default surface is **compact**: 20 tools instead of 30, with seven of
them grouping related operations behind an `op` argument. Every call is dispatched to the same
member through the same chokepoint, so gates, canvas rules, receipts, the standing-docs digest and
the two-phase confirm protocol are unchanged by construction.

### The default surface changed

`JUNE_TOOL_PROFILE` unset now means `compact`. `JUNE_TOOL_PROFILE=full` lists the 30 members under
their own names — the same 0.4.2 code, one name each — and `lean` is unchanged.

Measured before the switch, on the desktop engine, 100 scenarios × 3 runs per arm, against the
**patched** full surface so that the bug-class fixes below are not credited to the fold:

| arm | 0.4.1 full | 0.4.2 compact |
|---|---|---|
| Claude Code (Sonnet 5), tool search off | 0.987 | **1.000** |
| Claude Code (Sonnet 5), tool search on | 0.987 | **1.000** |
| GPT-5.4, direct tool loop | 0.957 | **0.983** |
| Codex (gpt-5.4), like-for-like | 0.763 | **0.922** |
| oracle (no model) | 1.000 | 1.000 |

Tool errors per task fell from 0.27 to 0.05 on Claude Code and from 0.91 to 0.17 on Codex; calls per
task from 1.80 to 1.52 and from 6.68 to 3.41. Unsafe erases and unexpected removals were 0 on every
arm in every run, on both surfaces. Prompt cost on a Pro read-write connection: 13,236 tokens → 11,359.

Migration is automatic for saved agent docs: the standing-docs digest carries an old-name →
new-name map, and a call to a folded name is refused with its exact replacement, so an agent that
guesses the old spelling self-corrects on the next call. Across 600 desktop runs no agent ever
tried an old name.

### Fixed

- **Entitlement gating listed tools no engine could serve.** `pro` was decided by `tier == "pro"`,
  so paying `pro-trial`, `power` and `team` keys lost agent page authoring. Capabilities are now
  resolved once at startup from `/v1/whoami` capabilities, then entitlement, then a single
  `GET /v1/pages?limit=1` probe — and an engine that serves no pages (`/v1/pages` → 404) hides the
  16 page-backed tools instead of advertising tools that could only fail. Fails open throughout:
  only an explicit signal removes a tool.
- **Array arguments had no `items`.** Google's function-calling API rejects an entire tool list over
  one such array, and models invent element shapes when none is declared — 103 of 119 baseline tool
  errors were rows sent to `june_ingest` in the wrong shape. Every array now declares `items`, and
  the ingest rows declare the engine's own `NodeIn` / `EdgeProposalIn` fields. `direction` became an
  enum. Two arguments the handlers read but never declared are declared.
- **Failed calls are flagged as errors.** A tool-execution failure now returns `isError: true` as the
  spec requires, so hosts can count and style it and models self-correct. Refusals are not errors: a
  refusal is the tool working, and returns a normal result carrying `refused`.
- **Tools carry `title` and annotations.** Without them the MCP defaults present every tool as
  non-read-only and destructive. `readOnlyHint` / `destructiveHint` / `idempotentHint` are derived
  from one `effect` field per tool, never hand-written.
- **Server instructions are generated per posture.** A read-only or free connection is no longer
  taught tools it cannot call — those postures went from 1,920 instruction tokens to 557. The
  full Pro read-write text is byte-identical to 0.4.1 by construction.
- **`june_page_delete` refuses a page this connection has never read.** A delete must be preceded by
  `june_page_get` (or the create that made the page), so an agent can always name what it is
  removing. Nothing is deleted on refusal.
- **Two-phase deletes teach their own protocol.** The `confirm` argument says to pass back the exact
  `confirm_token` from the first call, never the user's words, and the pending warning no longer
  tells an agent to re-ask a user who has already asked in plain words.
- **`--manifest` and `--install-instructions` honour the profile.** Both emit agent-facing text, and
  both now render it through the surface rather than the raw registry — so a repo's `CLAUDE.md` no
  longer instructs agents to call tool names that do not exist on this connection.
- **The surface alias map is per connection.** It travelled as module state for one patch series,
  which meant two servers built in one process shared it last-write-wins. It is now an argument,
  like every other posture fact.

### Notes for operators

- Nothing about the importable API moved: `visible_tools()`, `build_surface()`, `tool_manifest()`
  and `build_server()` still default to `profile="full"`. The default changed for the **server**,
  which is what `JUNE_TOOL_PROFILE` configures.
- If you pin the old shape, set `JUNE_TOOL_PROFILE=full` and re-run
  `june-mcp --install-instructions` so any managed `CLAUDE.md` section matches your surface.
- The block grammar for rich pages moved out of `june_page_create`'s description on the compact
  surface and is fetched on demand with `june_page_read(op='grammar')`. The table rule stays inline.
