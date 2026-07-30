"""arch_docs_block() feeds the plan/implement/code_review prompts the list of
architecture docs the agents must follow. It must be env-overridable and must
skip docs that do not exist yet (so a planned-but-uncreated doc appears
automatically once added rather than pointing agents at a missing file)."""
import os
import unittest
from unittest import mock

from pipeline_graph import config as C


class ArchDocsBlockTest(unittest.TestCase):
    def test_env_override_replaces_default_and_filters_missing(self):
        # The one contract a refactor could silently break: PIPELINE_ARCH_DOCS
        # replaces the default list, accepts both separators, skips missing files.
        env = {"PIPELINE_ARCH_DOCS": "README.md;does/not/exist.md\nrequirements.txt"}
        with mock.patch.dict(os.environ, env, clear=False):
            block = C.arch_docs_block()
        self.assertEqual(block, "- README.md\n- requirements.txt")


if __name__ == "__main__":
    unittest.main()
