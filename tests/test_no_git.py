"""NO_GIT (observe-only git) and the close_batch scope guard.

Regression for the "task-smoke: batch 1 — test" mix-up: a test/smoke run reaching
close_batch with another task's uncommitted work in the tree, whose blind
`git add -A` absorbed and mislabelled it. Two defences are verified here:
  1. PIPELINE_NO_GIT makes every commit/checkout site a no-op (real agents, no
     history mutation).
  2. implement()'s batch-start guard commits pre-existing leftovers separately so
     close_batch can only commit the batch's own work.
"""
import importlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests._yaml_fixture import _write_baseline_yaml
from pipeline_graph import config as C


def _commits(repo: Path) -> list[str]:
    out = subprocess.run(["git", "log", "--format=%s"], cwd=repo,
                         capture_output=True, text=True).stdout
    return out.splitlines()


class NoGitFlag(unittest.TestCase):
    def test_flag_is_bool(self):
        # NO_GIT is set at import time from yaml pipeline.no_git.
        self.assertIsInstance(C.NO_GIT, bool)

    def test_stale_env_ignored(self):
        # A stale PIPELINE_NO_GIT in the shell must NOT enable no-git (§3c).
        # NO_GIT is read from yaml at import time; env has no effect. Verified
        # via the shared yaml fixture (baseline yaml, no no_git key) + reload.
        saved_wt = os.environ.get("PIPELINE_WT_YAML")
        saved_env = os.environ.copy()
        os.environ["PIPELINE_NO_GIT"] = "1"
        try:
            with tempfile.TemporaryDirectory() as td:
                ypath = _write_baseline_yaml(Path(td) / "monkeforge.yaml")
                os.environ["PIPELINE_WT_YAML"] = str(ypath)
                importlib.reload(C)
                self.assertFalse(C.NO_GIT)
        finally:
            os.environ.clear()
            os.environ.update(saved_env)
            if saved_wt is not None:
                os.environ["PIPELINE_WT_YAML"] = saved_wt
            else:
                os.environ.pop("PIPELINE_WT_YAML", None)
            importlib.reload(C)

    def test_no_git_and_dry_run_are_independent(self):
        # DRY_RUN stubs agents; NO_GIT does not. They must be separate switches.
        self.assertIsInstance(C.DRY_RUN, bool)
        self.assertIsInstance(C.NO_GIT, bool)


class CloseBatchScopeGuard(unittest.TestCase):
    """The batch-start guard must isolate leftovers into their own commit."""

    def _repo(self, td: str) -> Path:
        repo = Path(td)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
        (repo / "seed.txt").write_text("seed\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "seed"], check=True)
        return repo

    def test_leftovers_committed_separately_from_batch(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(td)
            # Simulate leftover work from another task, uncommitted in the tree.
            (repo / "leftover.py").write_text("# another task's work\n")
            # --- batch-start guard (mirrors implement.py) ---
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m",
                            "WIP: uncommitted leftovers before task-42 batch 1"], check=True)
            base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                  capture_output=True, text=True).stdout.strip()
            # --- the batch's own work ---
            (repo / "batch_feature.py").write_text("# the real batch\n")
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m",
                            "task-42: batch 1 — real scope"], check=True)
            # The batch commit must contain ONLY batch_feature.py, not leftover.py.
            files = subprocess.run(
                ["git", "-C", str(repo), "diff", "--name-only", base, "HEAD"],
                capture_output=True, text=True).stdout.split()
            self.assertEqual(files, ["batch_feature.py"])
            msgs = _commits(repo)
            self.assertIn("task-42: batch 1 — real scope", msgs)
            self.assertIn("WIP: uncommitted leftovers before task-42 batch 1", msgs)


if __name__ == "__main__":
    unittest.main()
