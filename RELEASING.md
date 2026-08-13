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

## Release steps

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
