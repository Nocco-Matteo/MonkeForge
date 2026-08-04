"""Step 1 — the unified degradation ledger. Every compromise the run ships with
lands in one append-only list that wrap_up reports and doctor surfaces, so the
human reviews it at the end instead of being interrupted per-bump.
"""
import unittest
from unittest.mock import patch

from pipeline_graph import nodes as N, config as C


class StepOutcome(unittest.TestCase):
    def test_ledger_entry_is_degraded(self):
        self.assertEqual(N._step_outcome({"degradations": ["x"]}), "degraded")

    def test_escalation_beats_degraded(self):
        self.assertEqual(N._step_outcome({"escalation": "e", "degradations": ["x"]}), "blocked")

    def test_clean_is_ok(self):
        self.assertEqual(N._step_outcome({"journal": ["done"]}), "ok")


class StepSummary(unittest.TestCase):
    def test_prefers_debate_outcome_over_trailing_bypass_note(self):
        from pipeline_graph.nodes import common as NC

        lines = [
            "debate r2 tech: REJECT, 3 open blocker(s)",
            "visual review disabled — no render command configured for this repo",
        ]
        self.assertEqual(NC._step_summary(lines), lines[0])

    def test_falls_back_to_last_line(self):
        from pipeline_graph.nodes import common as NC

        self.assertEqual(NC._step_summary(["a", "b"]), "b")
        self.assertEqual(NC._step_summary([]), "no journal line")


class ContextLabel(unittest.TestCase):
    def test_debate_round_only_on_debate_nodes(self):
        from pipeline_graph.nodes import common as NC

        st = {"debate_round": 8, "batches": [{"n": 1}], "batch_idx": 0}
        self.assertIn("debate round 9", NC._context(st, "debate_tech"))
        self.assertIn("debate round 8", NC._context(st, "debate_reply"))
        # Implement / verify keep batch (+ fix cycle), not the stale debate counter.
        self.assertEqual(NC._context(st, "implement"), "batch 1/1")
        self.assertEqual(NC._context(st, "code_review"), "batch 1/1")
        self.assertNotIn("debate round", NC._context(st, "summary"))
        self.assertNotIn("debate round", NC._context(st, "checkpoint_plan"))

    def test_escalate_pause_still_shows_debate_round_pre_batches(self):
        from pipeline_graph.nodes import common as NC

        st = {"debate_round": 8, "batches": []}
        self.assertEqual(NC._context(st, ""), "debate round 8")


class WrapUpReport(unittest.TestCase):
    def test_report_lists_every_degradation(self):
        st = {"task_id": "ledgertest", "branch": "b", "batches": [{"n": 1}],
              "degradations": ["e2e DB skipped", "shipped with UX blockers"],
              "journal": ["done"]}
        with patch.object(N.ev, "emit"):
            N.wrap_up(st)
        report = (C.FINAL / "REPORT-ledgertest.md").read_text()
        self.assertIn("degradations: 2", report)
        self.assertIn("DEGRADED: e2e DB skipped", report)
        self.assertIn("DEGRADED: shipped with UX blockers", report)
        (C.FINAL / "REPORT-ledgertest.md").unlink(missing_ok=True)

    def test_clean_run_says_none(self):
        st = {"task_id": "ledgertest", "branch": "b", "batches": [{"n": 1}],
              "degradations": [], "journal": ["done"]}
        with patch.object(N.ev, "emit"):
            N.wrap_up(st)
        report = (C.FINAL / "REPORT-ledgertest.md").read_text()
        self.assertIn("degradations: none", report)
        (C.FINAL / "REPORT-ledgertest.md").unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
