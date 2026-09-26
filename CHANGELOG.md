# Changelog

Starts at 0.4.2. Earlier releases are in the git history and on PyPI.

## 0.7.0 — 2026-09-26 — Wave 6 · S9, the instruction channel

### Security
- **Only what the user approved is a standing instruction.** A pinned doc or a skill is a
  request until the person approves it in the Junê app; the engine seals the exact text
  (`instruction.state` on a page read) and any later edit by anyone else un-approves it.
  `is_instruction` is the one test every surface uses. Before this, any read-write connection
  — or a prompt injection driving one — could pin a doc that every other agent was then told to
  follow, every 12 calls.
- **Fail closed** on an engine that cannot report approval: no standing instructions, and the
  digest/handshake say why.
- The periodic digest carries **no words from an unapproved doc** (name and kind only).
- The posture names the connection key's role and scopes (from `/v1/whoami`) and warns when it
  is the June app key, which can approve instructions.

### Changed
- Approved always-on bodies ride the **handshake** (4,000 chars; overflow named, never cut
  mid-rule) and `june_docs_refresh` (`instructions[{name, body}]`). The digest becomes
  `{note, instructions_version, instructions[names], skills, requested, docs, as_of}`; its cap now
  covers the posture and notes (I1); the compact alias map moved to the handshake.
- `june-first` is built-in handshake text and no longer seeded; the untouched pre-0.7 seed is
  hidden.
- `june_doc_save` says `approval: requested` for a pinned doc or skill (and that a save changed
  an approved one); `june_doc_list` / `june_doc_get` report `instruction` and `approval`.
- Descriptions and the host instructions no longer tell agents to follow `standing_docs` or
  pinned bodies unconditionally.

### Fixed
- `june_canvas_current`'s description had a stray "canvas." (I3); `june_canvas_use` /
  `june_canvas_create` results no longer repeat the default-canvas note twice.

## 0.6.0 — 2026-09-25

Waves 1, 2, 4 and 5 of the structural fix plan (june-mcp canvas, page 48145480), in one release.
New page verbs place, reorder, rename, pin and restore blocks without resending the page; one page
vocabulary is checked on every write and says what it changed; page settings are owned by the
engine; time budgets are set by class; failure messages state only what is known.

Measured before release on the compact surface (124 scenarios × 3 runs, frozen desktop engine):
Claude Code, GPT-5.4 and Codex all pass the description gate — the new verbs at 0.972–1.000 task
success (bar 0.90), and the existing scenarios not worse than 0.4.2 (0.997 / 0.987 / 0.947 against
1.000 / 0.983 / 0.940). No unexpected removals on any arm.

### Added — page verbs that do not resend the page (S8, Wave 5)

- **`june_page_insert`** puts blocks after a named block (`after: <block id>`), or at the top
  (`after: null`), and **`june_page_move`** reorders existing blocks by id. Text, type, styling and
  ids are unchanged; the engine places them between their neighbours and renumbers only when
  there is no room. Both need an engine that advertises the `positions` page feature; on an older
  engine they refuse and write nothing, and say how to do it with a read and a full write.
- **`june_page_rename`** and **`june_page_meta`** (pinned / group) change a page's title and its
  place in your pages list without touching its blocks.
- **`june_page_removed`** lists what a page has lost, and **`june_page_restore`** brings blocks
  back with their original ids and positions — an undo, not a retype.
- **`june_backlinks`** answers "what links to this node" (the incoming edges).
- On the compact surface these are ops of the existing families — `june_page_edit` gains
  `insert`, `move`, `rename`, `meta`, `restore`; `june_page_read` gains `removed`; `june_graph`
  gains `backlinks` — so it still lists **20 tools**. `JUNE_TOOL_PROFILE=full` lists **37**
  (was 30).

### Changed — page writes (S8)

- **`june_page_write` is guarded by the page's `revision`** (`expected_revision`, from the read
  it grew out of). `expected_updated_at` is still accepted.
- **`june_page_update` may omit `text`.** An update that only changes a block's type keeps its
  text; it used to blank it (FX N6).
- A block `order` that would have rearranged the blocks, and a `title` sent to a content write,
  are named in the result instead of silently ignored (FX C3, C1).
- The undo note on a write's receipt names `june_page_restore` instead of a raw engine path (B1).
- `june_learn` shows the entries just before the new one, so a repeat is visible (L10).

### Changed — one page vocabulary, checked on every write (S2, Wave 5)

- The block types, style keys, colours, covers, illustrations and control grammar now come from
  one generated vocabulary shared with the engine and the app, so the connector cannot teach or
  accept a value the app will not render.
- **On engines that advertise `vocab`**, the connector sends the agent's values as written and
  passes back the engine's `coerced` list — every value it changed or dropped, with the reason, in
  the agent's own block positions. On older engines the connector builds the same list itself.
  Nothing is refused for a bad value; it is reported.
- Create and append say when blocks past the 2,000-block page limit were not written, and
  `june_page_write` refuses instead of cutting the tail off a page (FX N15).
- An argument that only another op takes is named in the result instead of dropped (FX N14).


### Changed — time budgets by class (S6, Wave 4)

- **`june_context` has its own budget: `JUNE_TIMEOUT_RETRIEVAL`, default 90 s.** It ran on the 15 s
  read budget, but its engine path reranks every candidate the canvas yields whatever
  `token_budget` or `max_items` asks for; 23 measured passes took 17.7–69.7 s. So it timed out on
  large canvases and never on small ones, and narrowing the request could not help.
- **Every tool declares a budget class** — fast read, retrieval, answer or write — in the tool
  facts table, next to its effect. A test drives each tool and checks its requests carry that
  class's timeout, so a verb cannot quietly run on the wrong clock again. A write can never be on
  a read clock (checked at import).
- **A timeout names the variable that sets its budget.** For `june_context` it no longer advises
  narrowing the request; it names `JUNE_TIMEOUT_RETRIEVAL` and `june_search`, which does not rerank.
- SDK: `JuneClient(retrieval_timeout=…)` and `context(timeout=…)`. Unset, the transport default
  applies exactly as before.

From the structural fix plan (june-mcp canvas, page 48145480): two data-safety fixes (Wave 1)
and the connector half of "the engine owns page settings" (Wave 2, S1).

### Changed — on engines that own page settings (advertise the `attrs` and `view` page features)

- **Page reads return content only.** `june_page_get` reads `GET /v1/pages/{id}/view`: blocks no
  longer include the hidden style/layout blocks, and the page's `style` and `layout` come back as
  fields (FX G3). Live views are content and are still returned as blocks.
- **A styled or laid-out write is one save.** `june_page_create` and `june_page_write` send the look
  as `style` / `layout` merge patches whose block references are positions in the same call, so
  there is no second save keyed on ids from the first — and no "written but unstyled" state
  (`StylingConflict`) left to report. A block the call styles has its known style keys replaced;
  blocks it does not style, and the page's stored look, are kept (`keep_attrs`). The style
  vocabulary is validated by the same builders as before.
- **Receipts come from the engine.** `layout.mode`, `cards`, `styled`, the new `page_style_keys`,
  `blocks_written`, `blocks_before` and an append's `blocks_total` are the engine's own numbers for
  what the page holds, and count content only (FX C5, C6, R2).
- **`june_page_append` styles what it appends** (per-block `variant`/`flag`/colour/`icon`/`space`),
  where it used to drop them (FX C2, block half).
- If such an engine answers a styled write without confirming it (no `attrs`), the receipt says the
  content was written and the look may not have been, and the capability is asked again next time.

Engines that do not advertise those features get exactly the previous behaviour, and an append
that asked for styling now says it was appended unstyled (`_notes.styling_ignored`) instead of
dropping it silently. The capability is asked of the engine (`GET /v1/pages/health`), cached for
five minutes and shared by every canvas view; if it cannot be asked, the previous behaviour is
used. A styled round trip no longer leaves a stale style on ANY connector version once the engine
is S1 — the engine merges duplicates itself.

### Changed — `june_enumerate` says whether a result is complete (S7)

- The description no longer promises "EVERY node". It states the rule the engine now reports:
  only `exhaustive: true` proves the list is complete. An empty or short result without it (a
  regex scan can stop at its limit) does not prove nothing else matches (FX E4).
- New optional `source_app` predicate. It is sent only when given, and items carry `source_app`
  on engines that return it (FX E3).

### Changed — messages state only what is known (S5)

- **One module, `june_mcp/explain.py`, writes failure text from facts in hand:** the tool, the
  timeout phase and its budget, the HTTP status, the engine's own reason (with credentials and
  URLs cut), the ids the agent sent, and the canvas. When the cause is not known, possible causes
  are listed as possibilities. A scan test fails the build if any other file composes a causal
  claim such as "vanish", "may be busy" or "engine restarted".
- **Timeouts name their phase** (connect, write, pool or read) and budget. A write that timed out
  reading "may have been APPLIED", and the message gives the read that settles it before any
  retry. A connect or pool timeout says nothing reached the engine (FX N11).
- **A failed call shows the engine's own reason** instead of a generic hint for the status code
  (FX N10). A page 404 names the page and canvas and suggests `june_page_list` with that canvas
  (FX L4).
- **`june_remember(job_id=…)` on an unknown job** lists the possible causes, instead of asserting
  that the engine restarted (FX G4).
- **`june_usage` says "could not tell" when the health beacon cannot be read,** instead of
  "receipts are off", and names both possibilities when the engine has no beacon. A summary with
  zero calls says what it covered, and the new `scope: "all"` sums every canvas you own. The engine
  already supported it; the connector and SDK now pass it through (FX L5).
- **A wrong `confirm` on canvas clear/delete and `june_doc_delete` is now a refusal RESULT**
  (`refused: confirm_mismatch`), not an error. It says whether the token minted earlier is still
  valid: an unknown value burns nothing (FX L9).
- **The digest's posture counts the tools `tools/list` actually serves.** On compact, it no
  longer tells every agent that its list is stale (FX N7).
- **`tool_aliases` is no longer respelled,** so its left side keeps the old names it translates
  (FX N8).
- **A 404 for a stale or deleted default canvas no longer reads as "this engine serves no pages"**
  and no longer hides the page tools (FX N9). S5 engines mark it with `X-June-Error:
  canvas_not_found`; older ones are recognised by their exact detail text.

### Added (SDK)

- `JuneClient.enumerate(source_app=…)` and `usage_summary(scope=…)` (each sent only when given).
- `JuneClient.pages_features()`, `forget_pages_features()`, `view_page()`; `style` / `layout` on
  `save_blocks`, `append_blocks` and `update_blocks`, and `keep_attrs` on `save_blocks`. Omitted
  means not sent (`june_client.client.UNSET`); `None` is sent and removes that attribute. A styled
  append that meets a pre-CX7 engine raises instead of falling back to an unstyled save.

### Fixed

- **0.5.0's write budgets never reached a running connector.** The serving client is a canvas
  view (`for_canvas`), and `for_canvas` rebuilt the client from a hand-written argument list that
  omitted `write_timeout`. Every write therefore still ran on the 15 s read timeout — the
  "timed out while the engine committed" shape 0.5.0 set out to close — and an agent that retried
  an append could duplicate blocks. A view is now a shallow copy with an explicit list of what it
  must NOT inherit (`_VIEW_RESETS`), so a field added later is carried by construction. A test
  drives the production path (`make_client` → `for_canvas`) and fails on the old client.
- **`june_page_write` no longer writes unguarded when its pre-read fails.** The read the connector
  makes before a replace is what the 10-block removal guard and the revision check stand on; when
  it failed (a timeout, a 5xx) the write degraded to a silent force save. It now returns a
  `refused: page_unreadable` result naming the error type, and nothing is written. `force: true`
  remains the deliberate override.

## 0.5.0 — 2026-09-20

The connector can say what a write replaces, follow a tier that changes mid-session, state what
it believes it is serving, and finish a write that takes longer than a read.

### Added

- **`june_remember(supersedes=[…])`.** A write can name the records it replaces, and the engine
  emits one `supersedes` edge per id in the same atomic write. An id that does not resolve is a
  400 and nothing is written. Omitted, the request body is byte-identical to before the field
  existed.
- **Posture on the standing-docs digest.** Every firing now carries `tools_advertised`, `profile`,
  `pro`, `readonly`, `engine_absent` and a `check` line. `tools_advertised` comes from the same
  function that builds the tool list, so the posture cannot drift into a second opinion. This is
  the one channel a host cannot cache: on 2026-09-19 a host served a tool list from before
  `supersedes` existed, the argument was stripped in transit, and the write still returned a normal
  success receipt. An agent comparing this count against the `june_*` tools it actually holds turns
  that into a one-line check.
- **`JUNE_SURFACE_REFRESH_SECS`** (default 0, never). Re-resolves the connection's tier on a
  cadence and sends `tools/list_changed`, which is now declared at the handshake.
- **`JUNE_PRO_GRACE`** (default off). Holds a pro-to-free reading for one refresh interval before
  applying it; a second free reading applies it; an upgrade is never delayed. For the whoami that
  answers, and answers wrong.
- **`JUNE_TIMEOUT_WRITE`** (default 85s). The ceiling for a write, not its budget — each write is
  scaled to its payload and capped here.

### Fixed

- **The advertised surface could not follow the tier.** `pro` was resolved once from `/v1/whoami`
  and the surface computed once from it, so a tier bought mid-session stayed invisible until the
  process was replaced. All five pro-derived pieces — surface, display names, alias map, listed
  prompts, and the `pro=` handed to `run_tool` — are now rebuilt as one act and cannot describe
  different tiers.
- **Every write ran on the read-verb timeout.** One transport was built with `read=timeout_read`
  (15s) and every verb shared it. Of 21 client methods that POST/PUT/DELETE, three carried a
  timeout and eighteen did not, including all four page writes. A 63-block append does not extract
  and embed in 15s, so the connector reported "timed out" while the engine committed all 63 blocks.
  Writes now scale with payload on the same curve as `remember_budget`, and a source-derived test
  fails if any write call site omits a budget. The same failure was found on a 40k-char
  `june_remember` on 2026-09-05 and fixed for that verb alone; this closes the class.

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
