"""Phase AM2 — repo sync: June pages and agent docs ⇄ files under a fenced repo root.

The repo stays current with what June knows: agent docs mirror to a docs tree,
any page (Ship Ops, Dev Practices, a design page) exports to a chosen file or a
MANAGED SECTION inside an existing file (KNOWHOW.md, CHANGELOG.md), a managed
file edited in the repo imports BACK into its June page, and a manifest makes
"is the repo current?" a checkable CI gate instead of a hope.

Safety posture (fenced commit, never push — Bhuvan's call 2026-08-24):

* **Operator opt-in only.** Nothing exists without ``JUNE_EXPORT_ROOT`` — the
  same consent shape as ``JUNE_FILES_ROOT``. Every path must resolve inside the
  root (symlinks followed BEFORE the containment check).
* **Never overwrite a human's file.** A whole-file write touches only files
  that carry our frontmatter marker (or don't exist yet); a section write
  touches only the text between our begin/end markers, appending the section
  when the file has none. Nothing here can delete a file.
* **Git is commit-only and pathspec-limited.** ``JUNE_EXPORT_GIT=1`` runs
  ``git add/commit`` restricted to exactly the files this call wrote — never
  ``add -A``, never push, never anyone else's staged work. A missing repo or a
  failed commit degrades to "files written, commit skipped/failed" — visibly.
* **Deterministic bytes.** Rendered files contain no export timestamps, so an
  unchanged doc re-exports to identical bytes and git sees no churn.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

ENV_EXPORT_ROOT = "JUNE_EXPORT_ROOT"     # the fenced repo directory (gate: unset ⇒ feature absent)
ENV_EXPORT_GIT = "JUNE_EXPORT_GIT"       # "1" ⇒ commit written files (never push)
ENV_EXPORT_DIR = "JUNE_EXPORT_DIR"       # agent-docs subtree inside the root (default docs/agent)
DEFAULT_EXPORT_DIR = "docs/agent"
MANIFEST_NAME = ".june-export.json"
FRONTMATTER_KEY = "june_export"
FRONTMATTER_VERSION = "v1"
GIT_TIMEOUT = 15.0
MAX_STDERR = 300                          # git failure detail is truncated, never dumped

_SECTION_BEGIN = "<!-- june:begin name={name} -->"
_SECTION_END = "<!-- june:end name={name} -->"
_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


def export_root(env=os.environ) -> Path | None:
    raw = env.get(ENV_EXPORT_ROOT, "").strip()
    return Path(raw).expanduser().resolve() if raw else None


def git_enabled(env=os.environ) -> bool:
    return env.get(ENV_EXPORT_GIT, "").strip().lower() in {"1", "true", "yes", "on"}


def export_dir(env=os.environ) -> str:
    return env.get(ENV_EXPORT_DIR, "").strip().strip("/") or DEFAULT_EXPORT_DIR


def fenced(root: Path, rel: str) -> Path:
    """Resolve ``rel`` inside ``root`` or raise ValueError. TWO fences, both
    required: a LEXICAL one first (os.path.normpath strips ``..`` segments
    without touching the filesystem — a ``..`` hidden under a not-yet-existing
    directory would slip past any stat-based walk, since stat fails on the
    missing component before ever seeing the dots), then a SYMLINK one (the
    deepest existing ancestor is resolved, so a link inside the root cannot
    point writes outside it — the JUNE_FILES_ROOT rule)."""
    rel = str(rel or "").strip()
    if not rel:
        raise ValueError("a target path is required")
    p = Path(rel)
    p = (p if p.is_absolute() else root / p)
    norm = Path(os.path.normpath(str(p)))          # lexical fence
    if norm != root and root not in norm.parents:
        raise ValueError(f"path escapes {ENV_EXPORT_ROOT} — refused")
    probe = norm                                    # symlink fence
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    resolved_base = probe.resolve()
    if resolved_base != root and root not in resolved_base.parents:
        raise ValueError(f"path escapes {ENV_EXPORT_ROOT} (via symlink) — refused")
    return norm


def slugify(text: str) -> str:
    s = _SLUG_RE.sub("-", (text or "").strip().lower()).strip("-._")
    return s[:64] or "untitled"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── frontmatter (our managed-file marker; stdlib-only, key: value lines) ─────
def render_frontmatter(meta: dict) -> str:
    lines = ["---", f"{FRONTMATTER_KEY}: {FRONTMATTER_VERSION}"]
    for k in ("kind", "name", "title", "page_id", "canvas", "v", "updated_at",
              "when_to_use", "pinned"):
        if meta.get(k) not in (None, "", False):
            lines.append(f"{k}: {meta[k]}")
    lines.append("---")
    return "\n".join(lines)


def parse_frontmatter(text: str) -> tuple[dict | None, str]:
    """(meta, body) when ``text`` starts with our frontmatter; (None, text)
    otherwise. Only files WE wrote parse — that is the point of the marker."""
    if not (text or "").startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return None, text
    meta: dict = {}
    for line in text[4:end].splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    if meta.get(FRONTMATTER_KEY) != FRONTMATTER_VERSION:
        return None, text
    return meta, text[end + 5:].lstrip("\n")


def render_managed_file(meta: dict, body: str) -> str:
    return render_frontmatter(meta) + "\n\n" + body.rstrip("\n") + "\n"


def is_managed(text: str) -> bool:
    return parse_frontmatter(text)[0] is not None


# ── managed sections (surgical writes inside human-owned files) ──────────────
def section_replace(existing: str | None, name: str, body: str) -> str:
    """Replace (or append) the ``name`` managed section. ONLY text between the
    markers is ever touched; a file without markers gains the section at EOF;
    a missing file becomes just the section. Duplicate begin markers fail loudly
    rather than guessing."""
    begin = _SECTION_BEGIN.format(name=name)
    end = _SECTION_END.format(name=name)
    block = f"{begin}\n{body.rstrip()}\n{end}"
    if existing is None:
        return block + "\n"
    if existing.count(begin) > 1:
        raise ValueError(f"file has duplicate section markers for {name!r} — fix the file first")
    if begin in existing:
        head, _, rest = existing.partition(begin)
        _, sep, tail = rest.partition(end)
        if not sep:
            raise ValueError(f"begin marker for {name!r} has no matching end marker — fix the file first")
        return head + block + tail
    joiner = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    return existing + joiner + block + "\n"


def section_extract(text: str, name: str) -> str | None:
    begin = _SECTION_BEGIN.format(name=name)
    end = _SECTION_END.format(name=name)
    if begin not in text or end not in text:
        return None
    return text.partition(begin)[2].partition(end)[0].strip("\n")


# ── manifest (what is managed, for the drift gate) ───────────────────────────
def manifest_load(root: Path) -> dict:
    p = root / MANIFEST_NAME
    if not p.is_file():
        return {"june_export_manifest": FRONTMATTER_VERSION, "files": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {"june_export_manifest": FRONTMATTER_VERSION, "files": {}}
    data.setdefault("files", {})
    return data


def manifest_save(root: Path, manifest: dict) -> str:
    """Deterministic bytes (sorted keys) so an unchanged state commits nothing."""
    p = root / MANIFEST_NAME
    p.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return MANIFEST_NAME


def manifest_note(manifest: dict, relpath: str, *, mode: str, page_id: str,
                  canvas: str, kind: str, updated_at: str | None,
                  content_hash: str, section: str | None = None) -> None:
    entry = {"mode": mode, "page_id": page_id, "canvas": canvas, "kind": kind,
             "updated_at": updated_at or "", "sha256": content_hash}
    if section:
        entry["section"] = section
    manifest["files"][relpath] = entry


# ── git (commit-only, pathspec-limited, never push) ──────────────────────────
def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, timeout=GIT_TIMEOUT,
                          check=False)


def git_commit(root: Path, rel_paths: list[str], message: str) -> dict:
    """Commit exactly ``rel_paths`` (nothing else, ever) → a status dict that is
    ALWAYS informational: files are already on disk whatever happens here."""
    if not rel_paths:
        return {"git": "nothing to commit"}
    if not (root / ".git").exists():
        return {"git": "not a git repository — files written, commit skipped"}
    try:
        add = _git(root, "add", "--", *rel_paths)
        if add.returncode != 0:
            return {"git": f"add failed — files written, not committed: "
                           f"{(add.stderr or '').strip()[:MAX_STDERR]}"}
        commit = _git(root, "commit", "-m", message, "--", *rel_paths)
        if commit.returncode != 0:
            detail = ((commit.stdout or "") + (commit.stderr or "")).strip()
            if "nothing to commit" in detail or "no changes added" in detail:
                return {"git": "clean — nothing to commit"}
            return {"git": f"commit failed — files written, not committed: "
                           f"{detail[:MAX_STDERR]}"}
        head = _git(root, "rev-parse", "--short", "HEAD")
        return {"git": "committed", "commit": (head.stdout or "").strip() or None}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"git": f"git unavailable ({type(exc).__name__}) — files written, not committed"}


# ── write planning (shared by the tools and the CLI) ─────────────────────────
def plan_write(root: Path, relpath: str, content: str) -> dict:
    """What writing ``content`` at ``relpath`` would do — WITHOUT doing it.
    status: create | update | unchanged | refused_unmanaged. The refusal is the
    never-overwrite-a-human-file fence; it names the file and the fix."""
    target = fenced(root, relpath)
    if not target.exists():
        return {"status": "create", "path": relpath, "target": target}
    existing = target.read_text(encoding="utf-8")
    if not is_managed(existing):
        return {"status": "refused_unmanaged", "path": relpath, "target": target,
                "reason": (f"{relpath} exists but was not written by june-mcp (no "
                           "frontmatter marker) — refusing to overwrite a human "
                           "file. Move it, or export to a different path.")}
    if existing == content:
        return {"status": "unchanged", "path": relpath, "target": target}
    return {"status": "update", "path": relpath, "target": target}


def apply_write(plan: dict, content: str) -> None:
    target: Path = plan["target"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


__all__ = [
    "DEFAULT_EXPORT_DIR", "ENV_EXPORT_DIR", "ENV_EXPORT_GIT", "ENV_EXPORT_ROOT",
    "FRONTMATTER_KEY", "FRONTMATTER_VERSION", "MANIFEST_NAME",
    "apply_write", "export_dir", "export_root", "fenced", "git_commit",
    "git_enabled", "is_managed", "manifest_load", "manifest_note",
    "manifest_save", "parse_frontmatter", "plan_write", "render_frontmatter",
    "render_managed_file", "section_extract", "section_replace", "sha256_text",
    "slugify",
]
