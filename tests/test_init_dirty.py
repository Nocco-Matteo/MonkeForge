import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline_graph import config as C, nodes as N


class GitExcludes(unittest.TestCase):
    def _init_repo(self, path: Path) -> None:
        subprocess.run(["git", "init", "-q", str(path)], check=True)

    def test_exclude_registered_and_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            self._init_repo(repo)
            C._ensure_git_excludes(repo, ".pipeline-docs/")
            exclude = repo / ".git" / "info" / "exclude"
            self.assertIn(".pipeline-docs/", exclude.read_text().splitlines())
            # Second call must not duplicate the entry.
            C._ensure_git_excludes(repo, ".pipeline-docs/")
            self.assertEqual(
                exclude.read_text().count(".pipeline-docs/"), 1)

    def test_excluded_path_is_invisible_to_git(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            self._init_repo(repo)
            C._ensure_git_excludes(repo, ".pipeline-docs/")
            (repo / ".pipeline-docs").mkdir()
            (repo / ".pipeline-docs" / "plan.md").write_text("x")
            untracked = subprocess.run(
                ["git", "ls-files", "-o", "--exclude-standard"],
                cwd=repo, capture_output=True, text=True).stdout
            self.assertEqual(untracked.strip(), "")


class DirtyPathsInit(unittest.TestCase):
    def test_src_blocks(self):
        self.assertTrue(N._dirty_blocks_interactive_init(["frontend/src/App.tsx"]))

    def test_pipeline_docs_inside_repo_do_not_block(self):
        """When PIPELINE_DOCS_DIR points inside the repo, pipeline artifacts
        in docs/ must not block init."""
        with patch.object(C, "INIT_DIRTY_OK_PREFIXES",
                          ("docs/tasks/", "docs/metrics/", "docs/prompts/", "docs/queue/")):
            paths = [
                "docs/tasks/TASK-006-play-mode.md",
                "docs/metrics/graph-checkpoints.sqlite",
                "docs/metrics/notify.log",
            ]
            self.assertFalse(N._dirty_blocks_interactive_init(paths))


if __name__ == "__main__":
    unittest.main()
