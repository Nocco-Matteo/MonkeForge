"""Isolation always-on: prepare_task_isolation + run.py bootstrap helpers."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_RT_PATH = Path(__file__).resolve().parents[1] / "pipeline_graph" / "worktree_runtime.py"
_spec = importlib.util.spec_from_file_location("wt_runtime", _RT_PATH)
RT = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(RT)


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


class TestPrepareIsolation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.mfroot = self.root / "MF"
        self.mfroot.mkdir()
        self.target = _make_repo(self.root, "product")
        (self.mfroot / "monkeforge.yaml").write_text("pipeline:\n  dry_run: true\n")
        self._mf_patch = patch.object(RT, "MF_ROOT", self.mfroot)
        self._mf_patch.start()
        self._saved = os.environ.copy()
        os.environ.pop("PIPELINE_REPO", None)
        os.environ.pop("PIPELINE_DOCS_DIR", None)
        os.environ.pop(RT.ISOLATED_ENV, None)
        os.environ["PIPELINE_WT_YAML"] = str(self.mfroot / "monkeforge.yaml")

    def tearDown(self):
        self._mf_patch.stop()
        os.environ.clear()
        os.environ.update(self._saved)
        self._tmp.cleanup()

    def _brief(self, id_: str = "033", *, src: Path | None = None) -> Path:
        if src is None:
            src = self.mfroot / "docs" / self.target.name / "tasks" / f"TASK-{id_}-brief.md"
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text("brief\n")
        return src

    def test_prepare_creates_and_reuses(self):
        self._brief("033")
        info1 = RT.prepare_task_isolation("033", repo_flag=str(self.target), create=True)
        self.assertTrue(info1["created"])
        self.assertTrue(info1["wt"].exists())
        info2 = RT.prepare_task_isolation("033", repo_flag=str(self.target), create=True)
        self.assertFalse(info2["created"])
        self.assertEqual(info1["wt"], info2["wt"])

    def test_prepare_resume_requires_wt(self):
        with self.assertRaises(SystemExit):
            RT.prepare_task_isolation("033", repo_flag=str(self.target), create=False)

    def test_prepare_accepts_external_brief(self):
        alt = self.root / "elsewhere" / "TASK-033-brief.md"
        alt.parent.mkdir(parents=True)
        alt.write_text("from --file\n")
        info = RT.prepare_task_isolation(
            "033", repo_flag=str(self.target), brief_src=alt, create=True,
        )
        dst = info["brief"]
        self.assertTrue(dst.exists())
        self.assertEqual(dst.read_text(), "from --file\n")

    def test_child_env_sets_isolated_flag(self):
        self._brief("033")
        info = RT.prepare_task_isolation("033", repo_flag=str(self.target))
        env = RT.isolation_child_env(info, parent_env={})
        self.assertEqual(env[RT.ISOLATED_ENV], "1")
        self.assertEqual(env["PIPELINE_BOT_AUTOSTART"], "0")
        self.assertTrue(Path(env["PIPELINE_REPO"]).name.startswith("wt-task-"))

    def test_early_cmd_and_task_id(self):
        self.assertEqual(
            RT.early_cmd_and_task_id(["start", "033", "--file", "x"]),
            ("start", "033"),
        )
        self.assertEqual(
            RT.early_cmd_and_task_id(["--repo", "p", "resume", "033"]),
            ("resume", "033"),
        )
        self.assertEqual(RT.early_cmd_and_task_id(["status"]), ("status", None))

    def test_bootstrap_noop_when_no_isolate(self):
        # Must not execve / raise when --no-isolate.
        RT.bootstrap_run_isolation(
            ["start", "033", "--file", "x", "--no-isolate"],
            run_py=self.mfroot / "run.py",
        )

    def test_banner_mentions_paths(self):
        self._brief("033")
        info = RT.prepare_task_isolation("033", repo_flag=str(self.target))
        text = RT.format_isolation_banner(info)
        self.assertIn("code:", text)
        self.assertIn("docs:", text)
        self.assertIn("feature/task-033", text)


if __name__ == "__main__":
    unittest.main()
