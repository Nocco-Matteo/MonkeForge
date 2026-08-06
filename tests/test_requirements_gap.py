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
        # TASK-033: second call with an active gap file returns the parsed
        # claims (the file is the source of truth once materialized); a later
        # escalation re-merges via write_requirements_gap, not via this helper.
        debate.write_text(
            "## Round 1 — Reviewer\nVERDICT: REJECT\n"
            "[BLOCKER:REQUIREMENTS] B1: different claim.\n"
        )
        again = RG.ensure_gap_from_debate_file("031")
        self.assertTrue(any("prune" in c for c in again))
        self.assertIn("prune", RG.read_requirements_gap("031"))

    def test_structured_items_merge_by_normalized_claim(self):
        # First write seeds one gap.
        RG.write_requirements_gap("031", ["prune vs branch collision"])
        # Second write with the SAME normalized claim (whitespace differs) but
        # now carrying Evidence/Impact merges — does NOT blind-overwrite, does
        # NOT duplicate the gap entry.
        RG.write_requirements_gap(
            "031",
            [{"claim": "prune  vs  branch collision",
              "evidence": "PLAN §2", "impact": "wt/branch drift"}],
        )
        body = RG.read_requirements_gap("031")
        # One gap entry (merge by normalized claim — whitespace collapse).
        self.assertEqual(body.count("## Gap "), 1)
        self.assertIn("Evidence: PLAN §2", body)
        self.assertIn("Impact: wt/branch drift", body)
        # A genuinely new claim is added (union), not merged away.
        RG.write_requirements_gap("031", ["a different brief hole"])
        body = RG.read_requirements_gap("031")
        self.assertEqual(body.count("## Gap "), 2)

    def test_gap_status_set_gap_status_round_trip(self):
        RG.write_requirements_gap("031", ["B1: active gap"])
        self.assertEqual(RG.gap_status("031"), "active")
        RG.suspend_requirements_gap("031")
        self.assertEqual(RG.gap_status("031"), "suspended")
        RG.waive_requirements_gap("031")
        self.assertEqual(RG.gap_status("031"), "waived")
        # set_gap_status back to active reactivates in place.
        RG.set_gap_status("031", "active")
        self.assertEqual(RG.gap_status("031"), "active")

    def test_gap_block_for_prompt_sentinel_when_suspended_or_waived(self):
        RG.write_requirements_gap("031", ["B1: active gap"])
        self.assertIn("active gap", RG.gap_block_for_prompt("031"))
        RG.suspend_requirements_gap("031")
        self.assertIn("(none", RG.gap_block_for_prompt("031"))
        RG.set_gap_status("031", "active")
        RG.waive_requirements_gap("031")
        self.assertIn("(none", RG.gap_block_for_prompt("031"))

    def test_ensure_gap_from_debate_file_reactivates_suspended_to_active(self):
        # Seed an active gap, then suspend it; ensure reactivates → active and
        # returns the parsed claims.
        RG.write_requirements_gap("031", ["B1: prune vs branch collision"])
        RG.suspend_requirements_gap("031")
        self.assertEqual(RG.gap_status("031"), "suspended")
        claims = RG.ensure_gap_from_debate_file("031")
        self.assertEqual(RG.gap_status("031"), "active")
        self.assertTrue(any("prune" in c for c in claims))

    def test_ensure_gap_from_debate_file_waived_returns_empty(self):
        RG.write_requirements_gap("031", ["B1: waived gap"])
        RG.waive_requirements_gap("031")
        self.assertEqual(RG.ensure_gap_from_debate_file("031"), [])

    def test_evidence_impact_lines_present_when_critic_block_emits_them(self):
        debate = self.debates / "DEBATE-031.md"
        debate.write_text(
            "## Round 1 — Reviewer\n"
            "VERDICT: REJECT\n"
            "[BLOCKER:REQUIREMENTS] B1: prune vs branch collision.\n"
            "Evidence: PLAN §2\n"
            "Impact: wt/branch drift\n"
        )
        RG.ensure_gap_from_debate_file("031")
        body = RG.read_requirements_gap("031")
        self.assertIn("Evidence: PLAN §2", body)
        self.assertIn("Impact: wt/branch drift", body)


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
