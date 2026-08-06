"""arch_docs_block() feeds the plan/implement/code_review prompts the list of
architecture docs the agents must follow. It must be yaml-configurable and must
skip docs that do not exist yet (so a planned-but-uncreated doc appears
automatically once added rather than pointing agents at a missing file)."""
import unittest
from unittest.mock import patch

from pipeline_graph import config as C


class ArchDocsBlockTest(unittest.TestCase):
    def test_yaml_list_replaces_default_and_filters_missing(self):
        # The one contract a refactor could silently break: pipeline.arch_docs
        # (yaml list) replaces the default list and skips missing files.
        with patch.object(C, "_pipeline", {
            "arch_docs": ["README.md", "does/not/exist.md", "requirements.txt"],
        }):
            block = C.arch_docs_block()
        self.assertEqual(block, "- README.md\n- requirements.txt")

    def test_yaml_string_with_semicolons(self):
        # pipeline.arch_docs as a ";"-separated string (legacy shape).
        with patch.object(C, "_pipeline", {
            "arch_docs": "README.md;does/not/exist.md;requirements.txt",
        }):
            block = C.arch_docs_block()
        self.assertEqual(block, "- README.md\n- requirements.txt")

    def test_yaml_string_with_newlines(self):
        # pipeline.arch_docs as a newline-separated string.
        with patch.object(C, "_pipeline", {
            "arch_docs": "README.md\ndoes/not/exist.md\nrequirements.txt",
        }):
            block = C.arch_docs_block()
        self.assertEqual(block, "- README.md\n- requirements.txt")


if __name__ == "__main__":
    unittest.main()
