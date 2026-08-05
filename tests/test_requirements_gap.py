"""REQUIREMENTS gap handoff: debate → re-intake gets the concrete claims."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline_graph import requirements_gap as RG
from pipeline_graph.nodes import debate as D


class TestRequirementsGapFile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.tasks = self.root / "tasks"
        self.tasks.mkdir()
        self.debates = self.root / "debates"
        self.debates.mkdir()
        self._patchers = [
            mock.patch.object(RG.C, "TASKS", self.tasks),
            mock.patch.object(RG.C, "DEBATES", self.debates),
            mock.patch.object(RG.C, "ensure_dirs", lambda: None),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self._tmp.cleanup()

    def test_write_read_clear(self):
        path = RG.write_requirements_gap("031", ["B1: stale wt vs branch"])
        self.assertTrue(path.exists())
        body = RG.read_requirements_gap("031")
        self.assertIn("## Gap 1", body)
        self.assertIn("stale wt vs branch", body)
        self.assertIn("stale wt", RG.gap_block_for_prompt("031"))
        RG.clear_requirements_gap("031")
        self.assertEqual(RG.read_requirements_gap("031"), "")
        self.assertIn("(none", RG.gap_block_for_prompt("031"))

    def test_empty_claims_no_file(self):
        self.assertIsNone(RG.write_requirements_gap("031", []))
        self.assertFalse(RG.requirements_gap_path("031").exists())

    def test_ensure_from_debate_file(self):
        debate = self.debates / "DEBATE-031.md"
        debate.write_text(
            "## Round 1 — Reviewer\n"
            "VERDICT: REJECT\n"
            "[BLOCKER:REQUIREMENTS] B1: prune vs branch collision.\n"
        )
        claims = RG.ensure_gap_from_debate_file("031")
        self.assertTrue(any("prune" in c for c in claims))
        self.assertIn("prune", RG.read_requirements_gap("031"))
        # Second call does not overwrite when file exists.
        debate.write_text(
            "## Round 1 — Reviewer\nVERDICT: REJECT\n"
            "[BLOCKER:REQUIREMENTS] B1: different claim.\n"
        )
        self.assertEqual(RG.ensure_gap_from_debate_file("031"), [])
        self.assertIn("prune", RG.read_requirements_gap("031"))


class TestCheckRequirementsWritesGap(unittest.TestCase):
    def test_writes_when_task_id_set(self):
        text = (
            "## Round 1 — Reviewer\n"
            "VERDICT: REJECT\n"
            "[BLOCKER:REQUIREMENTS] B1: brief acceptance contradicts guard.\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            tasks = Path(tmp) / "tasks"
            tasks.mkdir()
            with mock.patch("pipeline_graph.requirements_gap.C.TASKS", tasks), \
                 mock.patch("pipeline_graph.requirements_gap.C.ensure_dirs", lambda: None):
                out = D._check_requirements_escalation(text, task_id="x")
                self.assertIsNotNone(out)
                gap = tasks / "TASK-x-requirements-gap.md"
                self.assertTrue(gap.exists())
                self.assertIn("acceptance contradicts", gap.read_text())


if __name__ == "__main__":
    unittest.main()
