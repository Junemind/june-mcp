# Releasing june-mcp

The procedure actually used for 0.1.2 (2026-08-05) and 0.1.3 (2026-08-05), reconstructed from
the commits, the artifacts left in `dist/`, and `MCP_DIRECTORY_SUBMISSIONS.md`. Three surfaces
get updated per release: **PyPI**, the **MCP registry**, and (passively) **Glama**.

## Where the work happens

Work directly in `june_ai/june-mcp/` — it is a full clone of `Junemind/june-mcp` (public), and
it is **gitignored by june-brain** (`.gitignore:123`), so it never rides along with a june-brain
commit or a `desktop-v*` tag. `sync.sh` does NOT cover it (that script only pushes june-brain +
june-site) — push this repo separately.

> `MCP_DIRECTORY_SUBMISSIONS.md` (written 2026-07-31) describes syncing to a *sibling* checkout
> at `~/Documents/Claude/Projects/june-mcp`. That flow is **superseded**: 0.1.2 and 0.1.3 were
> committed directly in `june_ai/june-mcp`, and its `dist/` holds the artifacts that were
> actually published. If that sibling checkout still exists it is stale — GitHub is the source
> of truth; pull it or delete it rather than publishing from it.

## Version lives in THREE places — keep them in lockstep

A mismatch means the MCP registry advertises a version that doesn't exist on PyPI.

| file | field |
|---|---|
| `pyproject.toml` | `version` |
| `server.json` | top-level `"version"` |
| `server.json` | `packages[0].version` (the pypi entry) |

The 0.1.3 commit touched exactly these (plus the README name line) — 3 files, 6 lines.

## The `mcp-name` ownership proof

`README.md` line 3 carries `<!-- mcp-name: io.github.Junemind/june-mcp -->`, and it **must match
`server.json`'s `name` exactly** (casing included — that mismatch is the entire reason 0.1.3
exists). The registry validates ownership by reading the README **of the published PyPI
artifact**, so the comment has to survive into the package metadata, not just the GitHub page.
Verify it in the built artifact before uploading — see below.

## The one command

```bash
cd ~/Documents/Claude/Projects/june_ai/june-mcp
bash scripts/release.sh
```

Added 2026-08-13, mirroring `scripts/release.sh` in june-adk / june-langgraph /
june-openai-agents (june-mcp predates that pattern and had none). It runs every check below in
a hermetic venv and refuses to publish if any fails: version lockstep across the three fields,
README `mcp-name` vs `server.json` name, clean+pushed tree, tests, a build from CLEAN artifacts,
the mcp-name comment surviving into the wheel METADATA *and* sdist PKG-INFO, and a fresh-venv
install whose `june-mcp --manifest` really lists the tools. Then it asks before uploading, and
prints the registry step — which it deliberately does NOT do for you.

The manual sequence it automates, if you ever need to run it by hand:

```bash
cd ~/Documents/Claude/Projects/june_ai/june-mcp

# 1. tests (the stdio tests assert the exact tool COUNT and the read-only set —
#    adding a tool means updating them, as 0.1.4 did)
PYTHONPATH=src python -m pytest tests -q

# 2. bump the three version fields (above), commit, push
git push origin main

# 3. build — CLEAN FIRST. dist/ still holds the previous release's artifacts, and
#    `twine upload dist/*` would try to re-upload them and fail on "already exists".
rm -rf dist build
python -m build
python -m twine check dist/*

# 4. prove the ownership comment survived into the metadata (registry depends on it)
unzip -p dist/june_mcp-<VER>-py3-none-any.whl '*.dist-info/METADATA' | grep mcp-name

# 5. publish to PyPI
python -m twine upload dist/*

# 6. update the MCP registry (from the repo root, where server.json lives)
brew install mcp-publisher        # first time only
mcp-publisher login github        # as bhuone2345-art
mcp-publisher publish
curl "https://registry.modelcontextprotocol.io/v0/servers?search=june"   # verify
```

Any 3.11+ interpreter works — this is a pure-Python wheel, so the `.venv-ship` portability rule
that governs the desktop freezes does **not** apply here.

Glama needs nothing per release: it auto-indexes the public repo and reads `glama.json`.

## 0.2.2 — conventions teaching + 18-color palette

* **Teaching:** SERVER_INSTRUCTIONS + tool docs now teach the 0.0.12 page conventions —
  `[progress:]` bars/rings, `[date:]` chips (ISO), `$…$`/`$$…$$` math — and the in-cell
  `\|` escape rule. New `tests/test_teaching_content.py` pins all of it (the palette pin
  is DERIVED from `_STYLE_COLORS`, so an accepted-but-untaught color fails CI).
* **Palette:** `_STYLE_COLORS` 8 → 18 in lockstep with the app's `block_style.COLOR_KEYS`
  (gray, brown, rose, orange, yellow, lime, cyan, sky, indigo, fuchsia join).
* Note: PyPI 0.2.1 was published from the mid-day tree (escape fix only) before the
  evening teaching/palette commits landed — same-version re-upload is impossible, hence
  this release. No tool-surface change (still 23/24). Not breaking.

## 0.2.1 — in-table dropdown pipe escaping

* **Fix:** dropdowns (`[select: …]` / `[multi: …]`) written inside Markdown-table cells
  with raw `|` separators were sliced into ghost columns by the renderer and could lose
  cells on a UI round-trip. The connector now auto-escapes pipes inside select spans when
  the block holds a real GFM table — standalone and mid-prose selects are untouched, the
  canonical `\|` form passes through unchanged (idempotent). Applied on create/write/
  append/update alike.
* **Teaching:** SERVER_INSTRUCTIONS now states the `\|` form for in-cell dropdowns.
* No tool surface change (still 23 default / 24 with the file-upload opt-in). Not a
  breaking release.

## 0.2.0 (released 2026-08-21) — CX3–CX6: per-call canvas over an immutable default — BREAKING

The canvas-isolation release (Phase CX plan v2, D1/D2). Minor bump because it breaks two
public surfaces, both deliberately:

1. **`JuneClient.canvas` is now a READ-ONLY property.** Assignment raises `AttributeError`
   naming the replacements (`canvas=...` per call, or `for_canvas(...)` for a bound view).
   `june_client` ships inside this distribution and may be imported by third parties — this
   is the recorded breaking change. Why: one mutable attribute on one process-wide client
   let any conversation silently retarget every other conversation (observed live
   2026-08-17/18/19). Every request method now accepts `canvas: str | None` per call.
2. **`june_canvas_use` switches nothing.** It resolves and returns
   `{canvas_id, name, canvas_handle, default_canvas_id, switched: false}`. Agents act in a
   canvas by passing `canvas=<name | id | canvas_handle>` on the individual call; omission
   always targets the immutable startup default (JUNE_CANVAS). `june_canvas_create` likewise
   no longer switches (its `use` argument is gone). Result keys `default_canvas_id` /
   `default` ship alongside deprecated aliases `active_canvas_id` / `active` for ONE release.

Also in this release:

* `canvas_handle` (CX4): `jch1.<process-epoch>.<canvas-id>` — an unsigned CORRECTNESS token
  (not a credential). Stale (pre-restart) or malformed handles are refused loudly, never
  reinterpreted or silently redirected; valid handles resolve with zero network calls while
  ownership/existence stay enforced server-side per call.
* Optional `canvas` argument on every canvas-scoped tool (CX5), injected once with one shared
  description. Measured manifest cost: +4,813 bytes (~1.2k tokens) across 17 tools.
* `JUNE_CANVAS_STRICT=1` (CX5): refuses canvas-scoped calls that name no canvas — a posture
  for multi-canvas operators; safety does not depend on it.
* Every canvas-scoped result echoes the EFFECTIVE canvas (`canvas`, `canvas_name` when known
  traffic-free) — reads and writes alike (CX6 supersedes the 2026-08-14 write-only echo).
* Deleting the connection's default canvas is refused for the process lifetime (it can no
  longer be "switched away from" — restart with a different JUNE_CANVAS instead).
* CX7 client half: `append_blocks` now calls the engine's `POST /v1/pages/{id}/blocks:append`
  (server-assigned order, no document round-trip — a lost update is unrepresentable); the
  guarded client-side read→token→save survives ONLY as a capability-gated fallback for engines
  that 404 the route, and dies with them. Callers' smuggled `order`/`id` fields are stripped.
* CX10: confirm tokens are process-scoped by construction (restart ⇒ every pending
  confirmation dies; cross-epoch consume refuses loudly) — now pinned by test.
* CX8: tool execution is offloaded to worker threads behind a bounded
  `CapacityLimiter` — one stdio stream now carries concurrent in-flight calls
  (A5 measured the pre-CX8 state: pipelined requests fully serialized, and a slow
  tool froze even list_tools/pings). `JUNE_TOOL_CONCURRENCY` (default 8, whole
  number ≥ 1, fail-closed validation) is the explicit ceiling — backpressure,
  never a stampede. Enforced by `tests/test_cx8_concurrency.py`.
* CX9 (connector half): per-call `canvas=<name|id>` resolution is served from a
  bounded-TTL cache (60 s) holding POSITIVE, UNAMBIGUOUS resolutions only —
  misses/ambiguities always re-check live; our own `june_canvas_delete` and any
  canvas-scoped 404 evict the canvas's entries. Cuts the extra `GET /v1/canvases`
  per addressed call; never a correctness input (the server still enforces
  ownership/existence per call). Enforced by `tests/test_cx9_canvas_cache.py`.
  (Engine half — the two-level per-key + per-effective-canvas rate limit — ships
  in june-brain, derived automatically whenever rate limiting is engaged.)
* CX12: NEW tool `june_page_update` (Pro, writes) + `JuneClient.update_blocks` —
  edit NAMED existing blocks in place via the engine's `POST …/blocks:update`
  (also CX12). The small-payload safe edit: the server updates exactly the named
  blocks under the page row lock, preserves positions, never creates or deletes,
  refuses atomically on any unknown id, and takes the same shared replace guard
  as the full save (`expected_revision` → 409 on stale; `force` audited under
  its own `blocks:update#force` path). Deliberately NO fallback for pre-CX12
  engines — a 404 is loud, because degrading to the full-page save would
  reintroduce exactly the transport-the-document shape this verb removes.
  Enforced by `tests/test_cx12_page_update.py` (connector) and
  `tests/test_cx12_block_update.py` + the extended server-side-guard
  architecture scan (engine).
* Enforcement: `tests/test_no_shared_canvas_state.py` (AST: nothing assigns `.canvas`;
  tools.py module-mutable state allowlisted) + `tests/test_cx3_canvas_isolation.py`
  (interleaved + fan-out isolation, handle refusals, strict posture, truth echoes).
* SERVER_INSTRUCTIONS and every canvas tool description re-taught in the same change
  (the lockstep rule — a capability nobody is taught is a bug wearing a feature's clothes).

## Desktop app is a separate channel

The desktop app bundles its own **frozen** copy of this source (`june-desktop/mcp/build_mcp.sh`
→ `resources/mcp/june-mcp`), so app users get new tools from the app build, not from PyPI. PyPI
serves `uvx june-mcp` users and the registry/directory listings. Both need doing.

## 0.1.4 (2026-08-13) — what was verified

- 77 tests pass.
- `python -m build` produced both artifacts; `twine check` PASSED on each.
- Clean install of the wheel exposed all **16** tools + the `june-mcp` console entry point.
- `mcp-name` comment confirmed present in the wheel METADATA **and** the sdist PKG-INFO.
- `License-Expression: MIT`, `Requires-Python: >=3.11`.
