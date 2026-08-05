"""TASK-031 §3h test matrix for scripts/wt.py.

Covers: child-env setdefault strong + negative control, fail-fast pre-exec
assert, {e2e_db} gating (wt-level), E2E PORT+URL coupling, two-slug docs
isolation, slug/branch collision, pruned-not-alive, DOCS_DIR ban (resolved +
yaml pipeline.docs_dir), overlap merge-base range, land (dirty refuse +
ff-only abort via -y), brief file-copy, id validation, remove-branch cleanup
(survives manual dir delete + non-default prefix), branch_prefix yaml/env
resolution (incl. empty-env-wins), --help import-safety (no PIPELINE_REPO /
no yaml), list/empty PIPELINE_REPO target resolution, run --cwd override, and
the common.py DB_OK/DB_DOWN note tracking C.E2E_DB_PORT.
"""
from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Load pipeline_graph/worktree_runtime.py (scripts/wt.py is a thin re-export).
_WT_PATH = Path(__file__).resolve().parents[1] / "pipeline_graph" / "worktree_runtime.py"
_spec = importlib.util.spec_from_file_location("wt_cli", _WT_PATH)
wt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wt)

import run  # noqa: E402  — for _load_yaml_to_env in the setdefault tests
from pipeline_graph import config as C  # noqa: E402
from pipeline_graph.nodes import common  # noqa: E402


def _load_wt():
    """Re-exec the wt module fresh (used by help/import-safety tests)."""
    spec = importlib.util.spec_from_file_location("wt_cli_fresh", _WT_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _make_repo(parent: Path, name: str = "target") -> Path:
    repo = parent / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    return repo


def _write_yaml(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


class TestChildEnvSetdefault(unittest.TestCase):
    def test_child_env_setdefault_strong(self):
        wt_path = Path("/tmp/wt-task-031")
        child = wt.build_child_env(wt_path, 5435, parent_env={})
        self.assertEqual(child["PIPELINE_REPO"], str(wt_path))
        self.assertEqual(child["PIPELINE_BOT_AUTOSTART"], "0")
        self.assertNotIn("PIPELINE_DOCS_DIR", child)
        saved = os.environ.copy()
        os.environ.clear()
        os.environ.update(child)
        try:
            with tempfile.TemporaryDirectory() as td:
                adv = _write_yaml(Path(td) / "adv.yaml",
                                  "pipeline:\n  repo: /main/clone\n"
                                  "discord:\n  bot_autostart: true\n")
                run._load_yaml_to_env(adv)
            # setdefault must NOT override the child's pre-set values.
            self.assertEqual(os.environ["PIPELINE_REPO"], str(wt_path))
            self.assertEqual(os.environ["PIPELINE_BOT_AUTOSTART"], "0")
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_child_env_setdefault_negative(self):
        # Same adversarial fixture WITHOUT child overrides → env gets main/"true".
        saved = os.environ.copy()
        os.environ.clear()
        os.environ.pop("PIPELINE_REPO", None)
        os.environ.pop("PIPELINE_BOT_AUTOSTART", None)
        try:
            with tempfile.TemporaryDirectory() as td:
                adv = _write_yaml(Path(td) / "adv.yaml",
                                  "pipeline:\n  repo: /main/clone\n"
                                  "discord:\n  bot_autostart: true\n")
                run._load_yaml_to_env(adv)
            self.assertEqual(os.environ.get("PIPELINE_REPO"), "/main/clone")
            self.assertEqual(os.environ.get("PIPELINE_BOT_AUTOSTART"), "true")
        finally:
            os.environ.clear()
            os.environ.update(saved)


class TestChildEnvPortUrlCoupled(unittest.TestCase):
    def _both(self, parent):
        return wt.build_child_env(Path("/tmp/wt-task-x"), 5435, parent_env=parent)

    def test_parent_has_neither_assigns_both(self):
        env = self._both({})
        self.assertEqual(env["PIPELINE_E2E_DB_PORT"], "5435")
        self.assertTrue(env["PIPELINE_E2E_DATABASE_URL"].endswith(":5435/yourdb?schema=public"))

    def test_parent_has_url_only_assigns_both(self):
        env = self._both({"PIPELINE_E2E_DATABASE_URL": "postgresql://x:9999/db"})
        self.assertEqual(env["PIPELINE_E2E_DB_PORT"], "5435")
        self.assertTrue(env["PIPELINE_E2E_DATABASE_URL"].endswith(":5435/yourdb?schema=public"))

    def test_parent_has_port_only_assigns_both(self):
        env = self._both({"PIPELINE_E2E_DB_PORT": "9999"})
        self.assertEqual(env["PIPELINE_E2E_DB_PORT"], "5435")
        self.assertTrue(env["PIPELINE_E2E_DATABASE_URL"].endswith(":5435/yourdb?schema=public"))

    def test_parent_has_both_leaves_both(self):
        env = self._both({"PIPELINE_E2E_DB_PORT": "9999",
                          "PIPELINE_E2E_DATABASE_URL": "postgresql://x:9999/db"})
        self.assertEqual(env["PIPELINE_E2E_DB_PORT"], "9999")
        self.assertEqual(env["PIPELINE_E2E_DATABASE_URL"], "postgresql://x:9999/db")


class TestFailFast(unittest.TestCase):
    def test_fail_fast_missing_repo(self):
        # child dict missing PIPELINE_REPO → assert raises before any subprocess.
        with self.assertRaises(SystemExit):
            wt.assert_child_env({}, Path("/tmp/target"))


class TestIdValidation(unittest.TestCase):
    def _expect_fail(self, id_):
        with self.assertRaises(SystemExit):
            wt.validate_id(id_)

    def test_dotdot_rejected(self):
        self._expect_fail("..")

    def test_slash_rejected(self):
        self._expect_fail("a/b")

    def test_leading_dot_rejected(self):
        self._expect_fail(".hidden")

    def test_leading_dash_rejected(self):
        self._expect_fail("-dash")

    def test_dotdot_substring_rejected(self):
        self._expect_fail("a..b")

    def test_too_long_rejected(self):
        self._expect_fail("a" * 65)

    def test_valid_accepted(self):
        self.assertEqual(wt.validate_id("031"), "031")
        self.assertEqual(wt.validate_id("smoke"), "smoke")


class TestBranchPrefix(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.copy()
        os.environ.pop("PIPELINE_BRANCH_PREFIX", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def test_default_when_unset(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["PIPELINE_WT_YAML"] = str(Path(td) / "none.yaml")
            self.assertEqual(wt.resolve_branch_prefix(), "feature/task-")

    def test_yaml_win_when_env_unset(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["PIPELINE_WT_YAML"] = str(_write_yaml(
                Path(td) / "y.yaml", "pipeline:\n  branch_prefix: 'task/'\n"))
            self.assertEqual(wt.resolve_branch_prefix(), "task/")

    def test_env_wins_over_yaml(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["PIPELINE_WT_YAML"] = str(_write_yaml(
                Path(td) / "y.yaml", "pipeline:\n  branch_prefix: 'task/'\n"))
            os.environ["PIPELINE_BRANCH_PREFIX"] = "feat/"
            self.assertEqual(wt.resolve_branch_prefix(), "feat/")

    def test_empty_env_wins_over_yaml(self):
        # Explicitly-empty env value preserved as "" (setdefault-faithful).
        with tempfile.TemporaryDirectory() as td:
            os.environ["PIPELINE_WT_YAML"] = str(_write_yaml(
                Path(td) / "y.yaml", "pipeline:\n  branch_prefix: 'task/'\n"))
            os.environ["PIPELINE_BRANCH_PREFIX"] = ""
            self.assertEqual(wt.resolve_branch_prefix(), "")


class TestHelpAndTargetResolution(unittest.TestCase):
    def test_help_without_pipeline_repo(self):
        saved = os.environ.copy()
        os.environ.pop("PIPELINE_REPO", None)
        os.environ["PIPELINE_WT_YAML"] = "/nonexistent.yaml"
        try:
            with patch("sys.stdout", new_callable=io.StringIO) as out:
                with self.assertRaises(SystemExit) as cm:
                    wt.main(["--help"])
            self.assertEqual(cm.exception.code, 0)
            text = out.getvalue()
            for cmd in wt._SUBCOMMANDS:
                self.assertIn(cmd, text)
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_help_without_yaml_in_process(self):
        # In-process: point the yaml helper at a missing file, invoke --help.
        saved = os.environ.copy()
        os.environ.pop("PIPELINE_REPO", None)
        os.environ["PIPELINE_WT_YAML"] = "/nonexistent.yaml"
        try:
            m = _load_wt()
            with patch("sys.stdout", new_callable=io.StringIO) as out:
                with self.assertRaises(SystemExit) as cm:
                    m.main(["--help"])
            self.assertEqual(cm.exception.code, 0)
            for cmd in m._SUBCOMMANDS:
                self.assertIn(cmd, out.getvalue())
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_list_requires_target(self):
        saved = os.environ.copy()
        os.environ.pop("PIPELINE_REPO", None)
        with tempfile.TemporaryDirectory() as td:
            os.environ["PIPELINE_WT_YAML"] = str(_write_yaml(
                Path(td) / "y.yaml", "pipeline:\n  dry_run: true\n"))
            try:
                with self.assertRaises(SystemExit) as cm:
                    wt.main(["list"])
                self.assertNotEqual(cm.exception.code, 0)
            finally:
                os.environ.clear()
                os.environ.update(saved)

    def test_empty_pipeline_repo_fails(self):
        saved = os.environ.copy()
        os.environ["PIPELINE_REPO"] = "   "
        os.environ["PIPELINE_WT_YAML"] = "/nonexistent.yaml"
        try:
            with self.assertRaises(SystemExit):
                wt.resolve_target_repo(None)
        finally:
            os.environ.clear()
            os.environ.update(saved)


class _GitTestBase(unittest.TestCase):
    """Common fixture: a temp target git repo + a temp MF_ROOT (patched)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.mfroot = tmp / "mfroot"
        self.mfroot.mkdir()
        self.target = _make_repo(tmp, "target")
        self._mf_patch = patch.object(wt, "MF_ROOT", self.mfroot)
        self._mf_patch.start()
        self._saved_env = os.environ.copy()
        os.environ.pop("PIPELINE_REPO", None)
        os.environ.pop("PIPELINE_DOCS_DIR", None)
        os.environ.pop("PIPELINE_BRANCH_PREFIX", None)
        os.environ["PIPELINE_WT_YAML"] = str(self.mfroot / "monkeforge.yaml")

    def tearDown(self):
        self._mf_patch.stop()
        os.environ.clear()
        os.environ.update(self._saved_env)
        self._tmp.cleanup()

    def _write_brief(self, id_: str):
        src = self.mfroot / "docs" / self.target.name / "tasks" / f"TASK-{id_}-brief.md"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(f"brief for {id_}\n")
        return src

    def _ensure(self, id_: str, base: str = "main"):
        args = type("A", (), {"repo": str(self.target), "id": id_, "base": base})()
        wt.cmd_ensure(args)


class TestEnsureAndGuards(_GitTestBase):
    def test_ensure_creates_worktree_branch_brief(self):
        self._write_brief("031")
        self._ensure("031")
        wt_path = self.target.parent / "wt-task-031"
        self.assertTrue(wt_path.exists())
        self.assertTrue((self.mfroot / "docs" / "wt-task-031" / "tasks"
                         / "TASK-031-brief.md").exists())
        # branch exists
        out = wt._git(["show-ref", "--verify", "--quiet", "refs/heads/feature/task-031"],
                      cwd=self.target, check=False)
        self.assertEqual(out.returncode, 0)

    def test_slug_collision_fails(self):
        self._write_brief("031")
        self._ensure("031")
        with self.assertRaises(SystemExit):
            self._ensure("031")

    def test_branch_collision_after_manual_delete(self):
        # Bare directory delete leaves the branch ref → ensure must keep failing.
        self._write_brief("031")
        self._ensure("031")
        import shutil
        shutil.rmtree(self.target.parent / "wt-task-031")
        wt._git(["worktree", "prune"], cwd=self.target, check=False)
        # slug reusable (path gone), but branch still there → ensure fails.
        with self.assertRaises(SystemExit):
            self._ensure("031")

    def test_pruned_not_alive_allows_slug_reuse_after_branch_delete(self):
        self._write_brief("031")
        self._ensure("031")
        import shutil
        shutil.rmtree(self.target.parent / "wt-task-031")
        wt._git(["worktree", "prune"], cwd=self.target, check=False)
        wt._git(["branch", "-D", "feature/task-031"], cwd=self.target, check=False)
        # Now both slug and branch are free → ensure succeeds again.
        self._ensure("031")

    def test_brief_is_copy_not_symlink(self):
        self._write_brief("031")
        self._ensure("031")
        dst = self.mfroot / "docs" / "wt-task-031" / "tasks" / "TASK-031-brief.md"
        self.assertTrue(dst.exists())
        self.assertFalse(os.path.islink(dst))

    def test_docs_dir_ban_resolved_canonical(self):
        self._write_brief("031")
        os.environ["PIPELINE_DOCS_DIR"] = str(self.mfroot / "docs" / self.target.name)
        (self.mfroot / "docs" / self.target.name).mkdir(parents=True, exist_ok=True)
        with self.assertRaises(SystemExit):
            self._ensure("031")

    def test_docs_dir_yaml_pipeline_docs_dir_rejected(self):
        self._write_brief("031")
        _write_yaml(self.mfroot / "monkeforge.yaml",
                    "pipeline:\n  docs_dir: /shared/docs\n")
        with self.assertRaises(SystemExit):
            self._ensure("031")

    def test_isolation_two_slugs(self):
        self._write_brief("a")
        self._write_brief("b")
        self._ensure("a")
        self._ensure("b")
        d1 = self.mfroot / "docs" / "wt-task-a"
        d2 = self.mfroot / "docs" / "wt-task-b"
        self.assertNotEqual(d1.resolve(), d2.resolve())
        self.assertTrue(d1.exists() and d2.exists())


class TestRemove(_GitTestBase):
    def test_remove_deletes_branch_after_manual_dir_delete(self):
        self._write_brief("031")
        self._ensure("031")
        import shutil
        shutil.rmtree(self.target.parent / "wt-task-031")
        # stale porcelain entry remains; wt remove must still delete the branch.
        args = type("A", (), {"repo": str(self.target), "id": "031"})()
        wt.cmd_remove(args)
        out = wt._git(["show-ref", "--verify", "--quiet", "refs/heads/feature/task-031"],
                      cwd=self.target, check=False)
        self.assertNotEqual(out.returncode, 0)

    def test_remove_uses_resolved_prefix(self):
        # With a non-default PIPELINE_BRANCH_PREFIX, remove must delete that ref.
        os.environ["PIPELINE_BRANCH_PREFIX"] = "task/"
        self._write_brief("031")
        args = type("A", (), {"repo": str(self.target), "id": "031", "base": "main"})()
        wt.cmd_ensure(args)
        import shutil
        shutil.rmtree(self.target.parent / "wt-task-031")
        rm_args = type("A", (), {"repo": str(self.target), "id": "031"})()
        wt.cmd_remove(rm_args)
        out = wt._git(["show-ref", "--verify", "--quiet", "refs/heads/task/031"],
                      cwd=self.target, check=False)
        self.assertNotEqual(out.returncode, 0)


class TestOverlap(unittest.TestCase):
    def test_overlap_uses_merge_base_range(self):
        captured: list = []
        # A real target repo so resolve_target_repo's _validate_repo passes.
        repo_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(repo_tmp.cleanup)
        target = _make_repo(Path(repo_tmp.name), "target")
        # Keep the fake wt dirs alive for the whole test so Path.exists() holds.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p1 = Path(tmp.name) / "wt-task-a"
        p2 = Path(tmp.name) / "wt-task-b"
        p1.mkdir()
        p2.mkdir()

        def fake_run(cmd, *a, **k):
            captured.append(cmd)

            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            r = R()
            if isinstance(cmd, list) and "worktree" in cmd and "list" in cmd:
                r.stdout = (
                    f"worktree {p1}\nHEAD 0000000000000000000000000000000000000000\n"
                    f"branch refs/heads/feature/task-a\n\n"
                    f"worktree {p2}\nHEAD 0000000000000000000000000000000000000000\n"
                    f"branch refs/heads/feature/task-b\n\n"
                )
            elif isinstance(cmd, str) and "diff --name-only" in cmd:
                if "wt-task-a" in cmd:
                    r.stdout = "src/a.py\nsrc/shared.py\n"
                else:
                    r.stdout = "src/b.py\nsrc/shared.py\n"
            return r

        with patch.object(wt.subprocess, "run", side_effect=fake_run):
            args = type("A", (), {"repo": str(target)})()
            with patch("sys.stdout", new_callable=io.StringIO):
                wt.cmd_overlap(args)
        diff_calls = [c for c in captured if isinstance(c, str) and "diff --name-only" in c]
        self.assertTrue(diff_calls, "no diff call captured")
        for c in diff_calls:
            self.assertIn("merge-base", c)
            self.assertIn("...", c)
            self.assertNotIn("HEAD~", c)


class TestLand(unittest.TestCase):
    def test_land_dirty_refuse(self):
        with tempfile.TemporaryDirectory() as td:
            target = _make_repo(Path(td), "target")
            # dirty the working tree
            (target / "f.txt").write_text("dirty\n")
            args = type("A", (), {"repo": str(target), "id": "031", "yes": True})()
            with self.assertRaises(SystemExit):
                wt.cmd_land(args)

    def test_land_ff_only_abort_path(self):
        # Patch _git so rebase/checkout succeed but merge --ff-only fails.
        with tempfile.TemporaryDirectory() as td:
            target = _make_repo(Path(td), "target")
            wt_path = target.parent / "wt-task-031"
            wt_path.mkdir()
            calls: list = []

            def fake_git(args, cwd=None, check=True, capture=True):
                calls.append(args)
                class R:
                    returncode = 0
                    stdout = ""
                    stderr = ""
                r = R()
                if args[:2] == ["status", "--porcelain"]:
                    r.stdout = ""  # clean main
                elif args[:1] == ["rebase"]:
                    r.returncode = 0
                elif args[:1] == ["checkout"]:
                    r.returncode = 0
                elif args[:2] == ["merge", "--ff-only"]:
                    r.returncode = 1
                    r.stderr = "fatal: Not possible to fast-forward, aborting.\n"
                return r

            with patch.object(wt, "_git", side_effect=fake_git):
                with self.assertRaises(SystemExit) as cm:
                    args = type("A", (), {"repo": str(target), "id": "031",
                                          "yes": True})()
                    wt.cmd_land(args)
            self.assertNotEqual(cm.exception.code, 0)


class TestRunCwd(unittest.TestCase):
    def test_run_cwd_override(self):
        with tempfile.TemporaryDirectory() as td:
            target = _make_repo(Path(td), "target")
            wt_path = target.parent / "wt-task-031"
            wt_path.mkdir()
            # Make it a live worktree so assert_child_env passes.
            subprocess.run(["git", "-C", str(target), "worktree", "add",
                            str(wt_path), "-b", "feature/task-031"], check=True)
            captured: dict = {}

            def fake_run(cmd, *a, **k):
                captured["cmd"] = cmd
                captured["cwd"] = k.get("cwd")
                captured["env"] = k.get("env")

                class R:
                    returncode = 0
                    stdout = ""
                    stderr = ""
                r = R()
                # live_worktrees' porcelain probe must list wt_path as live.
                if isinstance(cmd, list) and "worktree" in cmd and "list" in cmd:
                    r.stdout = (
                        f"worktree {wt_path}\n"
                        f"HEAD 0000000000000000000000000000000000000000\n"
                        f"branch refs/heads/feature/task-031\n\n"
                    )
                return r

            cwd_arg = str(target.parent / "customcwd")
            with patch.object(wt.subprocess, "run", side_effect=fake_run):
                args = type("A", (), {
                    "repo": str(target), "id": "031", "notify_level": None,
                    "cwd": cwd_arg, "cmd": ["echo", "hi"],
                })()
                with self.assertRaises(SystemExit):
                    wt.cmd_run(args)
            self.assertEqual(captured.get("cwd"), cwd_arg)
            self.assertEqual(captured["env"]["PIPELINE_REPO"], str(wt_path.resolve()))
            self.assertEqual(captured["env"]["PIPELINE_BOT_AUTOSTART"], "0")
            self.assertNotIn("PIPELINE_DOCS_DIR", captured["env"])
            # cleanup
            subprocess.run(["git", "-C", str(target), "worktree", "remove",
                            "--force", str(wt_path)], check=False)
            subprocess.run(["git", "-C", str(target), "branch", "-D",
                            "feature/task-031"], check=False)


class TestDbNote(unittest.TestCase):
    def test_db_note_tracks_e2e_port(self):
        # Construct config + common with a NON-default port so the note is
        # observably derived from C.E2E_DB_PORT (not a hardcoded :5433).
        saved_env = os.environ.copy()
        saved_modules = sys.modules.copy()
        for _m in [m for m in sys.modules if m.startswith("pipeline_graph")]:
            del sys.modules[_m]
        os.environ.clear()
        os.environ["PIPELINE_REPO"] = str(Path(__file__).resolve().parents[1])
        os.environ["PIPELINE_E2E_DB_PORT"] = "5435"
        try:
            from pipeline_graph import config as C2
            from pipeline_graph.nodes import common as common2
            self.assertEqual(C2.E2E_DB_PORT, 5435)
            self.assertIn(":5435", common2.DB_OK_NOTE)
            self.assertNotIn(":5433", common2.DB_OK_NOTE)
            self.assertIn(":5435", common2.DB_DOWN_NOTE)
            self.assertNotIn(":5433", common2.DB_DOWN_NOTE)
        finally:
            os.environ.clear()
            os.environ.update(saved_env)
            for _m in [m for m in sys.modules if m.startswith("pipeline_graph")]:
                del sys.modules[_m]
            sys.modules.update(saved_modules)


if __name__ == "__main__":
    unittest.main()
