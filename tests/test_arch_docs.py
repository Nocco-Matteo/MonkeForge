"""arch_docs_block() feeds the plan/implement/code_review prompts the list of
architecture docs the agents must follow. It must be env-overridable and must
skip docs that do not exist yet (so a planned-but-uncreated doc appears
automatically once added rather than pointing agents at a missing file)."""
import os
import unittest
from unittest import mock

from pipeline_graph import config as C


class ArchDocsBlockTest(unittest.TestCase):
    def test_default_lists_only_existing_docs(self):
        block = C.arch_docs_block()
        # Every rendered path must actually exist under the repo.
        for line in block.splitlines():
            path = line.lstrip("- ").strip()
            self.assertTrue((C.REPO / path).exists(), f"listed missing doc: {path}")

    def test_env_override_replaces_default_and_filters_missing(self):
        env = {"PIPELINE_ARCH_DOCS": "CLAUDE.md;does/not/exist.md;lg/README.md"}
        with mock.patch.dict(os.environ, env, clear=False):
            block = C.arch_docs_block()
        self.assertIn("- CLAUDE.md", block)
        self.assertIn("- lg/README.md", block)
        self.assertNotIn("does/not/exist.md", block)

    def test_newlines_accepted_as_separators(self):
        env = {"PIPELINE_ARCH_DOCS": "CLAUDE.md\nlg/README.md"}
        with mock.patch.dict(os.environ, env, clear=False):
            block = C.arch_docs_block()
        self.assertIn("- CLAUDE.md", block)
        self.assertIn("- lg/README.md", block)

    def test_empty_config_reports_none(self):
        with mock.patch.dict(os.environ, {"PIPELINE_ARCH_DOCS": "  "}, clear=False):
            self.assertEqual(C.arch_docs_block(), "- (none configured)")


if __name__ == "__main__":
    unittest.main()
