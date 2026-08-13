#!/usr/bin/env bash
# release.sh — publish `june-mcp` to PyPI, safely.
#
# Same shape as the sibling packages' scripts/release.sh (june-adk, june-langgraph,
# june-openai-agents) and scripts/release_bench.sh in june-brain: a HERMETIC preflight
# that proves the artifact BEFORE anything is published, then the upload.
#
# june-mcp-specific guards the siblings don't need — each one is a bug this repo has
# actually shipped or nearly shipped:
#   • VERSION LOCKSTEP: the version lives in THREE places (pyproject + server.json twice).
#     A mismatch makes the MCP registry advertise a version that isn't on PyPI.
#   • NAME MATCH: README's `<!-- mcp-name: … -->` must equal server.json's `name`, casing
#     included — a casing mismatch is the entire reason 0.1.3 exists.
#   • METADATA PROOF: the registry validates ownership by reading the README of the
#     PUBLISHED ARTIFACT, so the mcp-name comment must survive into the wheel METADATA.
#   • STALE DIST: dist/ keeps the previous release's files; `twine upload dist/*` would
#     try to re-upload them and fail. Always rebuilt from clean here.
#
#   bash scripts/release.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VER="$(python3 -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")"
echo "• june-mcp version: $VER"

# ── Guard 1: version lockstep across the three fields ──────────────────────────────
python3 - "$VER" <<'PY'
import json, sys
ver = sys.argv[1]
s = json.load(open("server.json"))
bad = []
if s.get("version") != ver:
    bad.append(f"server.json .version = {s.get('version')!r}")
for i, p in enumerate(s.get("packages", [])):
    if p.get("version") != ver:
        bad.append(f"server.json .packages[{i}].version = {p.get('version')!r}")
if bad:
    print(f"✗ VERSION MISMATCH — pyproject says {ver!r} but:", file=sys.stderr)
    for b in bad:
        print(f"    {b}", file=sys.stderr)
    print("  The MCP registry would advertise a version that does not exist on PyPI.", file=sys.stderr)
    raise SystemExit(1)
print(f"✓ version lockstep: pyproject + server.json (×2) all {ver}")
PY

# ── Guard 2: README mcp-name == server.json name (exact, casing included) ──────────
python3 - <<'PY'
import json, re, sys
name = json.load(open("server.json"))["name"]
m = re.search(r"<!--\s*mcp-name:\s*(\S+)\s*-->", open("README.md").read())
if not m:
    print("✗ README.md has no <!-- mcp-name: … --> comment — the registry cannot verify ownership.", file=sys.stderr)
    raise SystemExit(1)
if m.group(1) != name:
    print(f"✗ NAME MISMATCH — README {m.group(1)!r} vs server.json {name!r} (casing counts).", file=sys.stderr)
    raise SystemExit(1)
print(f"✓ mcp-name matches server.json: {name}")
PY

# ── Guard 3: clean tree, and everything pushed (the published source must be public) ──
if [[ -n "$(git status --porcelain)" ]]; then
  echo "✗ working tree not clean — commit first; PyPI must match what's on GitHub." >&2
  git status --short; exit 1
fi
if [[ -n "$(git log --oneline @{u}..HEAD 2>/dev/null)" ]]; then
  echo "✗ unpushed commits — push before publishing, or PyPI ships source nobody can see:" >&2
  git log --oneline '@{u}..HEAD' >&2; exit 1
fi
echo "✓ tree clean and pushed"

# Public index only — a dev machine set up for the SP phase points pip at the private
# `june` runtime index, which carries neither build nor twine.
PYPI_PUBLIC_INDEX="${PYPI_PUBLIC_INDEX:-https://pypi.org/simple}"
export PIP_INDEX_URL="$PYPI_PUBLIC_INDEX" PIP_EXTRA_INDEX_URL=""

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
echo "• creating preflight venv …"
python3 -m venv "$WORK/venv"
PY="$WORK/venv/bin/python"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet "mcp[cli]>=1.0,<2" "httpx>=0.27" pytest pytest-asyncio build twine

# ── Preflight 1: the suite (stdio tests assert the exact tool COUNT + read-only set) ──
echo "• running tests …"
PYTHONPATH=src "$PY" -m pytest tests -q

# ── Preflight 2: hermetic build from CLEAN artifacts ──────────────────────────────
echo "• build + twine check (from clean) …"
rm -rf dist build ./*.egg-info
"$PY" -m build
"$PY" -m twine check dist/*

# ── Preflight 3: the ownership comment survived into the published metadata ────────
echo "• verifying mcp-name in the built artifact …"
"$PY" - <<PY
import glob, zipfile, sys, tarfile
whl = glob.glob("dist/*.whl")[0]
z = zipfile.ZipFile(whl)
meta = next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))
if b"mcp-name:" not in z.read(meta):
    print(f"✗ mcp-name comment MISSING from {whl} METADATA — the registry check would fail.", file=sys.stderr)
    raise SystemExit(1)
sd = glob.glob("dist/*.tar.gz")[0]
t = tarfile.open(sd)
pkg = next(m for m in t.getnames() if m.endswith("/PKG-INFO"))
if b"mcp-name:" not in t.extractfile(pkg).read():
    print(f"✗ mcp-name comment MISSING from {sd} PKG-INFO.", file=sys.stderr)
    raise SystemExit(1)
print("✓ mcp-name present in BOTH the wheel METADATA and the sdist PKG-INFO")
PY

# ── Preflight 4: the wheel installs clean and the console script works ─────────────
echo "• wheel smoke (fresh venv) …"
python3 -m venv "$WORK/smoke"
"$WORK/smoke/bin/pip" install --quiet --index-url "$PYPI_PUBLIC_INDEX" dist/*.whl
TOOLS="$("$WORK/smoke/bin/june-mcp" --manifest | "$PY" -c 'import sys,json; d=json.load(sys.stdin); print(len(d)); print(" ".join(t["name"] for t in d))')"
echo "  tools: $(echo "$TOOLS" | head -1)"
echo "  $(echo "$TOOLS" | tail -1)"
echo "$TOOLS" | grep -q "june_search" || { echo "✗ installed wheel does not expose june_search" >&2; exit 1; }
echo "✓ wheel installs; console script answers"

# ── Confirm — this publishes to PUBLIC PyPI, and a version can never be re-uploaded ──
echo
read -r -p "Publish june-mcp $VER to PyPI? [y/N] " ans
[[ "$ans" == "y" || "$ans" == "Y" ]] || { echo "aborted (artifacts left in dist/)."; exit 0; }
"$PY" -m twine upload dist/*

git tag "v$VER" 2>/dev/null && git push origin "v$VER" 2>/dev/null && echo "✓ tagged v$VER" || echo "  (tag skipped)"

cat <<EOF

✓ june-mcp $VER on PyPI.

Still to do — the registry does NOT update itself:
    mcp-publisher login github        # as bhuone2345-art
    mcp-publisher publish             # from the repo root, where server.json lives
    curl "https://registry.modelcontextprotocol.io/v0/servers?search=june"

Verify from a clean env:
    pipx install --force june-mcp==$VER && june-mcp --manifest | head -5
EOF
