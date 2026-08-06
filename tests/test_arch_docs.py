"""arch_docs_block() feeds the plan/implement/code_review prompts the list of
architecture docs the agents must follow. It must be yaml-configurable and must
skip docs that do not exist yet (so a planned-but-uncreated doc appears
automatically once added rather than pointing agents at a missing file)."""
import importlib
import os
import tempfile
import unittest
from pathlib import Path

from tests._yaml_fixture import _write_baseline_yaml
from pipeline_graph import config as C


class ArchDocsBlockTest(unittest.TestCase):
    def setUp(self):
        self._saved_wt = os.environ.get("PIPELINE_WT_YAML")
        self._saved_env = os.environ.copy()
        self._td = tempfile.TemporaryDirectory()
        self._yaml_path = Path(self._td.name) / "monkeforge.yaml"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)
        if self._saved_wt is not None:
            os.environ["PIPELINE_WT_YAML"] = self._saved_wt
        else:
            os.environ.pop("PIPELINE_WT_YAML", None)
        importlib.reload(C)
        self._td.cleanup()

    def _reload(self):
        os.environ["PIPELINE_WT_YAML"] = str(self._yaml_path)
        importlib.reload(C)
        return C

    def test_yaml_list_replaces_default_and_filters_missing(self):
        # The one contract a refactor could silently break: pipeline.arch_docs
        # (yaml list) replaces the default list and skips missing files.
        _write_baseline_yaml(self._yaml_path, extra=(
            "pipeline:\n"
            "  arch_docs:\n"
            "    - README.md\n"
            "    - does/not/exist.md\n"
            "    - requirements.txt\n"
        ))
        C2 = self._reload()
        self.assertEqual(C2.arch_docs_block(), "- README.md\n- requirements.txt")

    def test_yaml_string_with_semicolons(self):
        # pipeline.arch_docs as a ";"-separated string (legacy shape).
        _write_baseline_yaml(self._yaml_path, extra=(
            "pipeline:\n"
            "  arch_docs: 'README.md;does/not/exist.md;requirements.txt'\n"
        ))
        C2 = self._reload()
        self.assertEqual(C2.arch_docs_block(), "- README.md\n- requirements.txt")

    def test_yaml_string_with_newlines(self):
        # pipeline.arch_docs as a newline-separated string (literal block).
        _write_baseline_yaml(self._yaml_path, extra=(
            "pipeline:\n"
            "  arch_docs: |\n"
            "    README.md\n"
            "    does/not/exist.md\n"
            "    requirements.txt\n"
        ))
        C2 = self._reload()
        self.assertEqual(C2.arch_docs_block(), "- README.md\n- requirements.txt")

    def test_missing_key_returns_none_configured(self):
        # No arch_docs key in yaml → the "(none configured)" placeholder.
        _write_baseline_yaml(self._yaml_path)
        C2 = self._reload()
        self.assertEqual(C2.arch_docs_block(), "- (none configured)")


if __name__ == "__main__":
    unittest.main()
