"""Live acceptance run — the whole AM..AM6 story against the REAL June engine.

Not a unit test: this drives run_tool with a real JuneClient over HTTP against
june_service.serve (JUNE_PAGES=1), plus the real CLI modes and real git — the
same wire the desktop app and every agent host will use. Asserts on returned
receipts AND engine truth. Prints one PASS line per step; exits nonzero on any
failure.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

import httpx
from june_client import JuneClient
from june_mcp.tools import _docs_reset, configure_docs, run_tool

BASE = "http://127.0.0.1:8000"
KEY = "june_sk_dev_admin"
steps: list[str] = []


def ok(msg: str) -> None:
    steps.append(msg)
    print(f"PASS {len(steps):02d}  {msg}")


def main() -> int:
    http = httpx.Client(base_url=BASE, timeout=30.0)
    boot = JuneClient(BASE, KEY, client=http)
    work = str(boot.create_canvas("work")["canvas_id"])
    client = boot.for_canvas(work)
    ok(f"engine reachable; 'work' canvas created ({work[:8]}…)")

    # ── AM3/AM4: first save creates the system canvas + seeds manual & posture ──
    out = run_tool("june_doc_save", client, {
        "name": "house-rules", "kind": "doc", "pinned": True,
        "text": "# House rules\n\n- fix the class, not the instance\n- read before write"})
    assert out["created"] and out["canvas_name"] == "agent_docs", out
    assert "seeded" in out["_notes"] and "docs_canvas_created" in out["_notes"], out
    ok("first doc_save auto-created agent_docs AND seeded the guide + june-first")

    listing = run_tool("june_doc_list", client, {})
    names = sorted(d["name"] for d in listing["docs"])
    assert names == ["agent-memory-guide", "house-rules", "june-first"], names
    ok(f"registry over live pages routes: {names}")

    guide = run_tool("june_doc_get", client, {"name": "agent-memory-guide"})
    assert "SYSTEM CANVAS" in guide["body"] and guide["when_to_use"], guide["when_to_use"]
    ok("operating manual readable from the live store (sentinel stripped)")

    # ── skill + learn ────────────────────────────────────────────────────────
    out = run_tool("june_doc_save", client, {
        "name": "fix-class", "kind": "skill", "when_to_use": "before fixing any bug",
        "text": "1. fix instance at cause\n2. name the class\n3. sweep siblings\n4. add a tripwire"})
    assert out["created"], out
    out = run_tool("june_learn", client, {"text": "live acceptance: engine + connector agree"})
    assert out["appended"] == 1 and out["created_doc"], out
    try:
        run_tool("june_learn", client, {"text": "x", "doc": "fix-class"})
        raise AssertionError("learn into a skill must refuse")
    except ValueError:
        pass
    ok("skill saved; learn appended (and refused a curated doc)")

    # ── digest + injection on the live engine ────────────────────────────────
    digest = run_tool("june_docs_refresh", client, {})
    pinned = {p["name"] for p in digest["pinned"]}
    assert pinned == {"house-rules", "june-first"}, pinned
    assert any(s["name"] == "fix-class" for s in digest["skills"]), digest["skills"]
    _docs_reset()
    configure_docs(enabled=True)
    ans = run_tool("june_search", client, {"query": "house rules"})
    assert "standing_docs" in ans and "june-first" in {
        p["name"] for p in ans["standing_docs"]["pinned"]}, ans.get("standing_docs")
    ans2 = run_tool("june_search", client, {"query": "house rules"})
    assert "standing_docs" not in ans2
    _docs_reset()
    ok("digest correct; injection fired on first live call, quiet on second")

    # ── revision guard against the real engine ───────────────────────────────
    stale = run_tool("june_doc_save", client, {
        "name": "house-rules", "text": "stale overwrite", "expected_updated_at": "bogus"})
    assert stale.get("refused") == "doc_changed_since_read", stale
    ok("stale save refused by the live revision guard (nothing applied)")

    # ── AM2: export → git → drift gate → import roundtrip ───────────────────
    with tempfile.TemporaryDirectory() as td:
        repo = pathlib.Path(td)
        for c in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", td, *c], check=True)
        (repo / "KNOWHOW.md").write_text("# KNOWHOW\n\nHuman wisdom stays.\n")
        os.environ["JUNE_EXPORT_ROOT"] = td
        os.environ["JUNE_EXPORT_GIT"] = "1"
        import dataclasses

        import june_mcp.tools as tm
        swapped = {}
        for i, t in enumerate(tm.TOOLS):
            if t.name in ("june_docs_export", "june_page_export", "june_page_import"):
                swapped[i] = t
                tm.TOOLS[i] = dataclasses.replace(t, available=True)
                tm._BY_NAME[t.name] = tm.TOOLS[i]
        try:
            exp = run_tool("june_docs_export", client, {})
            assert len(exp["written"]) == 5 and exp["git"] == "committed", exp   # 3 saved + guide + june-first… plus learnings = 5 with the log
            log = subprocess.run(["git", "-C", td, "log", "--oneline"],
                                 capture_output=True, text=True, check=True).stdout
            assert "june-export" in log
            ok(f"5 docs exported + committed ({exp['commit']}) to a real git repo")

            env = {**os.environ, "JUNE_BASE_URL": BASE, "JUNE_API_KEY": KEY,
                   "JUNE_CANVAS": work}
            chk = subprocess.run([sys.executable, "-m", "june_mcp", "--export-check"],
                                 capture_output=True, text=True, env=env, check=False)
            assert chk.returncode == 0, chk.stderr
            run_tool("june_doc_save", client, {"name": "house-rules", "pinned": True,
                                               "text": "# House rules v2\n\nrevised"})
            chk2 = subprocess.run([sys.executable, "-m", "june_mcp", "--export-check"],
                                  capture_output=True, text=True, env=env, check=False)
            assert chk2.returncode == 1 and "drift" in chk2.stderr, chk2.stderr
            exp2 = subprocess.run([sys.executable, "-m", "june_mcp", "--export"],
                                  capture_output=True, text=True, env=env, check=False)
            assert exp2.returncode == 0, exp2.stderr
            chk3 = subprocess.run([sys.executable, "-m", "june_mcp", "--export-check"],
                                  capture_output=True, text=True, env=env, check=False)
            assert chk3.returncode == 0, chk3.stderr
            ok("CI drift gate: clean→0, June edit→1 (drift named), --export heals→0")

            f = repo / "docs/agent/fix-class.md"
            meta_text = f.read_text()
            f.write_text(meta_text.replace("sweep siblings", "sweep ALL siblings"))
            imp = run_tool("june_page_import", client, {"path": "docs/agent/fix-class.md"})
            assert imp["imported"], imp
            got = run_tool("june_doc_get", client, {"name": "fix-class"})
            assert "sweep ALL siblings" in got["body"], got["body"]
            assert got["when_to_use"] == "before fixing any bug"     # identity survived
            ok("repo edit imported back into the live page; skill identity intact")

            sec = run_tool("june_page_export", client, {
                "page_id": got["page_id"], "path": "KNOWHOW.md",
                "section": "june-skill", "canvas": "agent_docs"})
            assert sec["mode"] == "section" and sec["status"] in ("create", "update"), sec
            know = (repo / "KNOWHOW.md").read_text()
            assert "Human wisdom stays." in know and "sweep ALL siblings" in know
            ok("managed section spliced into human KNOWHOW.md (human text untouched)")

            claude_dir = repo / ".claude"; claude_dir.mkdir()
            ins = subprocess.run([sys.executable, "-m", "june_mcp",
                                  "--install-instructions", ".claude/CLAUDE.md"],
                                 capture_output=True, text=True, env=env, check=False)
            assert ins.returncode == 0, ins.stderr
            assert "june_docs_refresh" in (claude_dir / "CLAUDE.md").read_text()
            ok("--install-instructions wrote the host hook (the desktop-app spawn path)")
        finally:
            for i, t in swapped.items():
                tm.TOOLS[i] = t
                tm._BY_NAME[t.name] = t
            os.environ.pop("JUNE_EXPORT_ROOT", None)
            os.environ.pop("JUNE_EXPORT_GIT", None)

    # ── the engine's own audit ledger is ground truth ────────────────────────
    audit = http.get("/v1/audit", headers={"X-API-Key": KEY}).json()
    rows = audit if isinstance(audit, list) else audit.get("entries") or audit.get("rows") or []
    bad = [r for r in rows if isinstance(r, dict) and int(r.get("status", 200)) >= 500]
    assert not bad, bad[:3]
    ok(f"engine audit ledger: {len(rows)} mutations, zero 5xx")

    print(f"\nLIVE ACCEPTANCE: {len(steps)}/{len(steps)} steps passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
