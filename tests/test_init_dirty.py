import unittest
from unittest.mock import patch

from pipeline_graph import config as C, nodes as N


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
