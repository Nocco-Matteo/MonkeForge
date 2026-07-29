import unittest
from unittest.mock import patch

from pipeline_graph import nodes as N


class DirtyPathsInit(unittest.TestCase):
    def test_pipeline_docs_do_not_block(self):
        paths = [
            "docs/tasks/TASK-006-play-mode.md",
            "docs/metrics/graph-checkpoints.sqlite",
            "docs/metrics/notify.log",
        ]
        self.assertFalse(N._dirty_blocks_interactive_init(paths))

    def test_src_blocks(self):
        self.assertTrue(N._dirty_blocks_interactive_init(["frontend/src/App.tsx"]))


if __name__ == "__main__":
    unittest.main()
