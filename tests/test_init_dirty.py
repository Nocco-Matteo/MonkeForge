import importlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline_graph import config as C, nodes as N


class DocsAddressing(unittest.TestCase):
    """Agents reach external docs by absolute path, never through an in-repo mirror."""

    def _config_for(self, repo: Path):
        with patch.dict("os.environ", {"PIPELINE_REPO": str(repo)}, clear=False):
            return importlib.reload(C)

    def test_external_docs_are_absolute_and_not_mirrored(self):
        try:
            with tempfile.TemporaryDirectory() as td:
                repo = Path(td)
                subprocess.run(["git", "init", "-q", str(repo)], check=True)
                cfg = self._config_for(repo)
                self.assertFalse(cfg.DOCS.is_relative_to(repo))
                self.assertTrue(Path(cfg.DOCS_REL).is_absolute())
                self.assertIsNone(cfg.DOCS_ORIG)
                self.assertEqual(cfg.INIT_DIRTY_OK_PREFIXES, ())
                self.assertFalse((repo / ".pipeline-docs").exists())
        finally:
            importlib.reload(C)  # restore module state for the rest of the suite

    def test_gemini_command_grants_external_docs_dir(self):
        cmd = C.role_cmd("UX_REVIEWER", Path("/x/p.md"), "prompt")
        self.assertIn("--include-directories", cmd)
        self.assertIn(str(C.DOCS), cmd)


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
