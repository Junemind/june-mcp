"""Phase AM2 tests — repo sync: the pure export module (frontmatter, sections,
fencing, manifest), the three tools against a fake June + a real temp repo, the
never-overwrite-a-human-file fence, round-trip import with the revision guard,
and commit-only pathspec-limited git. Same no-network posture as every suite;
git tests run against a real ``git init`` tmpdir and skip if git is absent.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_am_agent_docs import FakeJune, _client

from june_mcp import export as export_mod
from june_mcp.runtime import ToolInputError
from june_mcp.tools import run_tool

_GIT = shutil.which("git") is not None


_EXPORT_TOOLS = ("june_docs_export", "june_page_export", "june_page_import")


class _ExportEnv(unittest.TestCase):
    """Temp JUNE_EXPORT_ROOT around each test (the spawn-env contract, like
    JUNE_FILES_ROOT). Availability is decided at import from that env — the
    spawn contract — so, mirroring the capability-fence ghost test, the Tool
    entries are swapped to available=True here and restored after; handlers
    still re-read the env at call time (the second fence stays live)."""

    def setUp(self) -> None:
        import dataclasses

        import june_mcp.tools as tm
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self._old = {k: os.environ.get(k) for k in
                     (export_mod.ENV_EXPORT_ROOT, export_mod.ENV_EXPORT_GIT,
                      export_mod.ENV_EXPORT_DIR)}
        os.environ[export_mod.ENV_EXPORT_ROOT] = str(self.root)
        os.environ.pop(export_mod.ENV_EXPORT_GIT, None)
        os.environ.pop(export_mod.ENV_EXPORT_DIR, None)
        self._swapped = {}
        for i, t in enumerate(tm.TOOLS):
            if t.name in _EXPORT_TOOLS:
                self._swapped[i] = t
                enabled = dataclasses.replace(t, available=True)
                tm.TOOLS[i] = enabled
                tm._BY_NAME[t.name] = enabled

    def tearDown(self) -> None:
        import june_mcp.tools as tm
        for i, t in self._swapped.items():
            tm.TOOLS[i] = t
            tm._BY_NAME[t.name] = t
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()


class TestPureExport(unittest.TestCase):
    def test_frontmatter_roundtrip_keeps_doc_identity(self) -> None:
        meta = {"kind": "skill", "name": "fix-class", "title": "Fix the class",
                "page_id": "p1", "canvas": "c1", "v": 3, "updated_at": "t9",
                "when_to_use": "before fixing any bug", "pinned": True}
        text = export_mod.render_managed_file(meta, "# Body\n\nsteps")
        got, body = export_mod.parse_frontmatter(text)
        self.assertEqual(got["name"], "fix-class")
        self.assertEqual(got["when_to_use"], "before fixing any bug")
        self.assertEqual(got["pinned"], "True")
        self.assertEqual(body, "# Body\n\nsteps\n")
        self.assertTrue(export_mod.is_managed(text))
        self.assertFalse(export_mod.is_managed("# just a readme"))
        self.assertFalse(export_mod.is_managed("---\nauthor: bhuvan\n---\nprose"))

    def test_section_replace_touches_only_the_marked_region(self) -> None:
        human = "# KNOWHOW\n\nHand-written wisdom.\n"
        v1 = export_mod.section_replace(human, "june-learnings", "- lesson one")
        self.assertIn("Hand-written wisdom.", v1)
        self.assertIn("- lesson one", v1)
        v2 = export_mod.section_replace(v1, "june-learnings", "- lesson one\n- lesson two")
        self.assertIn("Hand-written wisdom.", v2)
        self.assertIn("- lesson two", v2)
        self.assertEqual(v2.count("june:begin"), 1)      # replaced, not duplicated
        self.assertEqual(export_mod.section_extract(v2, "june-learnings"),
                         "- lesson one\n- lesson two")

    def test_section_marker_damage_fails_loudly(self) -> None:
        begin = "<!-- june:begin name=x -->"
        with self.assertRaises(ValueError):
            export_mod.section_replace(f"{begin}\na\n{begin}\n", "x", "b")
        with self.assertRaises(ValueError):                # begin without end
            export_mod.section_replace(f"{begin}\na\n", "x", "b")

    def test_fenced_refuses_escapes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            for evil in ("../x", "/etc/passwd", "a/../../x", ""):
                with self.assertRaises(ValueError):
                    export_mod.fenced(root, evil)
            ok = export_mod.fenced(root, "docs/agent/new.md")   # not-yet-existing is fine
            self.assertTrue(str(ok).startswith(str(root)))

    def test_fenced_refuses_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside:
            root = Path(td).resolve()
            (root / "link").symlink_to(outside)
            with self.assertRaises(ValueError) as ctx:
                export_mod.fenced(root, "link/x.md")
            self.assertIn("symlink", str(ctx.exception))

    def test_manifest_bytes_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            m = export_mod.manifest_load(root)
            export_mod.manifest_note(m, "b.md", mode="file", page_id="p2", canvas="c",
                                     kind="page", updated_at="t", content_hash="h2")
            export_mod.manifest_note(m, "a.md", mode="file", page_id="p1", canvas="c",
                                     kind="doc", updated_at="t", content_hash="h1")
            export_mod.manifest_save(root, m)
            first = (root / export_mod.MANIFEST_NAME).read_bytes()
            export_mod.manifest_save(root, export_mod.manifest_load(root))
            self.assertEqual(first, (root / export_mod.MANIFEST_NAME).read_bytes())


class TestDocsExport(_ExportEnv):
    def test_exports_docs_then_idempotent(self) -> None:
        fake = FakeJune()
        fake.add_canvas("agent_docs")
        fake.add_doc_page("agent_docs", "rules", pinned=True, body="Always read first.")
        fake.add_doc_page("agent_docs", "fix-class", kind="skill", when="on bugs")
        out = run_tool("june_docs_export", _client(fake), {})
        self.assertEqual(sorted(out["written"]),
                         ["docs/agent/fix-class.md", "docs/agent/rules.md"])
        text = (self.root / "docs/agent/rules.md").read_text()
        meta, body = export_mod.parse_frontmatter(text)
        self.assertEqual((meta["name"], meta["kind"], meta["pinned"]),
                         ("rules", "doc", "True"))
        self.assertEqual(body.strip(), "Always read first.")
        self.assertTrue((self.root / export_mod.MANIFEST_NAME).is_file())
        again = run_tool("june_docs_export", _client(fake), {})
        self.assertEqual(again["written"], [])              # deterministic bytes
        self.assertEqual(again["unchanged"], 2)

    def test_never_overwrites_a_human_file(self) -> None:
        fake = FakeJune()
        fake.add_canvas("agent_docs")
        fake.add_doc_page("agent_docs", "rules")
        target = self.root / "docs/agent/rules.md"
        target.parent.mkdir(parents=True)
        target.write_text("# Bhuvan's own notes\n")
        out = run_tool("june_docs_export", _client(fake), {})
        self.assertEqual(out["written"], [])
        self.assertEqual(len(out["refused"]), 1)
        self.assertIn("rules.md", out["refused"][0])
        self.assertEqual(target.read_text(), "# Bhuvan's own notes\n")  # untouched

    def test_check_mode_reports_drift_and_writes_nothing(self) -> None:
        fake = FakeJune()
        fake.add_canvas("agent_docs")
        fake.add_doc_page("agent_docs", "rules")
        out = run_tool("june_docs_export", _client(fake), {"check": True})
        self.assertFalse(out["current"])
        self.assertEqual(out["drift"][0]["doc"], "rules")
        self.assertFalse((self.root / "docs/agent/rules.md").exists())

    def test_disabled_without_root(self) -> None:
        os.environ.pop(export_mod.ENV_EXPORT_ROOT, None)
        with self.assertRaises(ToolInputError) as ctx:
            from june_mcp.tools import _docs_export
            _docs_export(_client(FakeJune()), {})
        self.assertIn("JUNE_EXPORT_ROOT", str(ctx.exception))


class TestPageExportAndSections(_ExportEnv):
    def test_whole_file_export_default_path(self) -> None:
        fake = FakeJune()
        fake.add_canvas("agent_docs")
        pid = fake.add_plain_page("work", "Ship Ops", "step one")
        out = run_tool("june_page_export", _client(fake), {"page_id": pid})
        self.assertEqual(out["path"], "docs/agent/pages/ship-ops.md")
        self.assertEqual(out["status"], "create")
        meta, body = export_mod.parse_frontmatter(
            (self.root / out["path"]).read_text())
        self.assertEqual(meta["page_id"], pid)
        self.assertEqual(body.strip(), "step one")

    def test_section_export_into_human_knowhow(self) -> None:
        fake = FakeJune()
        pid = fake.add_plain_page("work", "Agent learnings", "- [2026-08-24] a lesson")
        knowhow = self.root / "KNOWHOW.md"
        knowhow.write_text("# KNOWHOW\n\nHuman wisdom stays.\n")
        out = run_tool("june_page_export", _client(fake), {
            "page_id": pid, "path": "KNOWHOW.md", "section": "june-learnings"})
        self.assertEqual(out["mode"], "section")
        text = knowhow.read_text()
        self.assertIn("Human wisdom stays.", text)          # never touched
        self.assertIn("a lesson", text)
        again = run_tool("june_page_export", _client(fake), {
            "page_id": pid, "path": "KNOWHOW.md", "section": "june-learnings"})
        self.assertEqual(again["status"], "unchanged")

    def test_unmanaged_whole_file_target_is_refused_as_result(self) -> None:
        fake = FakeJune()
        pid = fake.add_plain_page("work", "Notes", "x")
        (self.root / "README.md").write_text("hands off\n")
        out = run_tool("june_page_export", _client(fake),
                       {"page_id": pid, "path": "README.md"})
        self.assertEqual(out["refused"], "would_overwrite_unmanaged_file")
        self.assertEqual((self.root / "README.md").read_text(), "hands off\n")


class TestImportRoundTrip(_ExportEnv):
    def test_edit_file_then_import_updates_june_and_keeps_identity(self) -> None:
        fake = FakeJune()
        fake.add_canvas("agent_docs")
        fake.add_doc_page("agent_docs", "rules", pinned=True, body="old body")
        client = _client(fake)
        run_tool("june_docs_export", client, {})
        f = self.root / "docs/agent/rules.md"
        meta, _body = export_mod.parse_frontmatter(f.read_text())
        f.write_text(export_mod.render_managed_file(
            {k: v for k, v in meta.items() if k != export_mod.FRONTMATTER_KEY},
            "edited in my editor"))
        out = run_tool("june_page_import", client, {"path": "docs/agent/rules.md"})
        self.assertTrue(out["imported"])
        got = run_tool("june_doc_get", client, {"name": "rules"})   # registry intact
        self.assertEqual(got["body"], "edited in my editor")
        self.assertTrue(got["pinned"])                       # identity survived
        self.assertGreater(got["v"], 1)                      # revision bumped
        # And the repo file's revision token was refreshed for the NEXT import.
        meta2, _ = export_mod.parse_frontmatter(f.read_text())
        self.assertEqual(meta2["updated_at"], got["updated_at"])

    def test_stale_file_cannot_clobber_newer_june_content(self) -> None:
        fake = FakeJune()
        fake.add_canvas("agent_docs")
        fake.add_doc_page("agent_docs", "rules", body="old body")
        client = _client(fake)
        run_tool("june_docs_export", client, {})
        # June moves on AFTER the export…
        run_tool("june_doc_save", client, {"name": "rules", "text": "newer in June"})
        # …then someone imports the now-stale file.
        f = self.root / "docs/agent/rules.md"
        f.write_text(f.read_text().replace("old body", "stale edit"))
        out = run_tool("june_page_import", client, {"path": "docs/agent/rules.md"})
        self.assertEqual(out["refused"], "page_changed_in_june")
        got = run_tool("june_doc_get", client, {"name": "rules"})
        self.assertEqual(got["body"], "newer in June")       # nothing clobbered

    def test_unmanaged_file_refused(self) -> None:
        (self.root / "notes.md").write_text("mine")
        with self.assertRaises(ToolInputError):
            run_tool("june_page_import", _client(FakeJune()), {"path": "notes.md"})


@unittest.skipUnless(_GIT, "git not installed")
class TestGitCommit(_ExportEnv):
    def _init_repo(self) -> None:
        subprocess.run(["git", "-C", str(self.root), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "t@t"],
                       check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "t"],
                       check=True)

    def _log(self) -> str:
        return subprocess.run(["git", "-C", str(self.root), "log", "--oneline"],
                              capture_output=True, text=True, check=True).stdout

    def test_commit_is_pathspec_limited_and_never_touches_other_work(self) -> None:
        self._init_repo()
        (self.root / "wip.py").write_text("# Bhuvan's uncommitted work\n")
        os.environ[export_mod.ENV_EXPORT_GIT] = "1"
        fake = FakeJune()
        fake.add_canvas("agent_docs")
        fake.add_doc_page("agent_docs", "rules")
        out = run_tool("june_docs_export", _client(fake), {})
        self.assertEqual(out["git"], "committed")
        self.assertTrue(out["commit"])
        self.assertIn("june-export", self._log())
        status = subprocess.run(["git", "-C", str(self.root), "status", "--porcelain"],
                                capture_output=True, text=True, check=True).stdout
        self.assertIn("wip.py", status)                      # untouched, uncommitted
        # Idempotent second run: no empty commit.
        again = run_tool("june_docs_export", _client(fake), {})
        self.assertIn(again["git"], ("nothing to commit", "clean — nothing to commit"))

    def test_not_a_repo_degrades_to_files_written(self) -> None:
        os.environ[export_mod.ENV_EXPORT_GIT] = "1"
        fake = FakeJune()
        fake.add_canvas("agent_docs")
        fake.add_doc_page("agent_docs", "rules")
        out = run_tool("june_docs_export", _client(fake), {})
        self.assertIn("not a git repository", out["git"])
        self.assertTrue((self.root / "docs/agent/rules.md").is_file())

    def test_git_off_reports_commit_is_yours(self) -> None:
        fake = FakeJune()
        fake.add_canvas("agent_docs")
        fake.add_doc_page("agent_docs", "rules")
        out = run_tool("june_docs_export", _client(fake), {})
        self.assertIn("commit is yours", out["git"])


class TestCliModes(unittest.TestCase):
    def test_export_without_config_fails_closed_exit_2(self) -> None:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("JUNE_")}
        proc = subprocess.run([sys.executable, "-m", "june_mcp", "--export"],
                              capture_output=True, text=True, timeout=60, env=env,
                              check=False)
        self.assertEqual(proc.returncode, 2, proc.stderr)


if __name__ == "__main__":
    unittest.main()


class TestInstallInstructions(unittest.TestCase):
    """--install-instructions closes the cold-start gap: June's standing
    instructions enter the file the host loads natively every session, as a
    managed section that respects everything humans wrote around it."""

    def _run(self, *args: str, root: str | None) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if not k.startswith("JUNE_")}
        if root:
            env["JUNE_EXPORT_ROOT"] = root
        return subprocess.run([sys.executable, "-m", "june_mcp",
                               "--install-instructions", *args],
                              capture_output=True, text=True, timeout=60,
                              env=env, check=False)

    def test_installs_into_existing_claude_md_preserving_human_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            claude_md = Path(td) / "CLAUDE.md"
            claude_md.write_text("# My project\n\nHuman rules stay.\n")
            proc = self._run(root=td)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            text = claude_md.read_text()
            self.assertIn("Human rules stay.", text)
            self.assertIn("june_docs_refresh", text)
            self.assertIn("june-integration", text)
            # Idempotent re-run: no duplicate section, clean exit.
            proc2 = self._run(root=td)
            self.assertEqual(proc2.returncode, 0)
            self.assertIn("nothing to do", proc2.stderr)
            self.assertEqual(claude_md.read_text().count("june:begin"), 1)

    def test_creates_named_file_and_refuses_escapes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            proc = self._run("AGENTS.md", root=td)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("june_remember", (Path(td) / "AGENTS.md").read_text())
            bad = self._run("../outside.md", root=td)
            self.assertEqual(bad.returncode, 2)
            self.assertIn("refused", bad.stderr)

    def test_without_root_fails_closed(self) -> None:
        proc = self._run(root=None)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("JUNE_EXPORT_ROOT", proc.stderr)

    def test_host_instructions_teach_real_vocabulary(self) -> None:
        from june_mcp.prompts import HOST_INSTRUCTIONS
        from june_mcp.server import tool_manifest
        names = {t["name"] for t in tool_manifest()}
        for verb in ("june_docs_refresh", "june_answer", "june_search",
                     "june_remember", "june_learn", "june_doc_save"):
            self.assertIn(verb, HOST_INSTRUCTIONS)
            self.assertIn(verb, names)               # taught verbs must be real
        self.assertIn("without", HOST_INSTRUCTIONS)     # the unasked-usage posture
        self.assertIn("agent-memory-guide", HOST_INSTRUCTIONS)
