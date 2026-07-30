"""Process fixes from the TASK-010 run.

- The final gate is baseline-aware: it tolerates failures present at task start
  and only auto-fixes NEW ones. (A frontend-only task must never send the fixer
  to delete backend code to turn a pre-existing red test green.)
- The lint gate parses eslint JSON, counting errors AND warnings, line-
  insensitive so a shifted pre-existing violation still matches its baseline.
"""
import json
import unittest
from unittest.mock import patch

from pipeline_graph import nodes as N, test_runner as tr
from pipeline_graph.state import Conversation


def _conv(tid):
    """Minimal Conversation for `_final_test_fix_loop` tests: only `task_id`
    is read by the helper (for the `degraded` event); the rest is unused."""
    return Conversation(
        task_id=tid,
        request="",
        brief="",
        plan="",
        debate_history="",
        batch_context="{}",
        review_history="",
        journal=(),
    )


class EslintParsing(unittest.TestCase):
    def _out(self, messages):
        return "> lint\n> eslint --format json\n\n" + json.dumps(
            [{"filePath": "/repo/src/a.tsx", "messages": messages}])

    def test_counts_errors_and_warnings(self):
        keys = tr.parse_eslint_errors(self._out([
            {"ruleId": "no-unused-vars", "severity": 2, "message": "x unused", "line": 3},
            {"ruleId": "eqeqeq", "severity": 1, "message": "use ===", "line": 9},
        ]))
        self.assertEqual(len(keys), 2)

    def test_severity_zero_ignored(self):
        keys = tr.parse_eslint_errors(self._out([
            {"ruleId": "off-rule", "severity": 0, "message": "n/a", "line": 1}]))
        self.assertEqual(keys, set())

    def test_key_is_line_insensitive(self):
        a = tr.parse_eslint_errors(self._out([
            {"ruleId": "eqeqeq", "severity": 1, "message": "use ===", "line": 9}]))
        b = tr.parse_eslint_errors(self._out([
            {"ruleId": "eqeqeq", "severity": 1, "message": "use ===", "line": 42}]))
        self.assertEqual(a, b)  # same file|rule|message despite the shifted line

    def test_garbage_degrades_to_empty(self):
        self.assertEqual(tr.parse_eslint_errors("npm ERR! something broke"), set())
        self.assertEqual(tr.parse_eslint_errors(""), set())


class FinalGateBaseline(unittest.TestCase):
    def test_preexisting_failures_are_tolerated(self):
        # Only baseline failures remain → no fixer runs, gate passes.
        with patch.object(N.tr, "run_repo_tests", return_value=(0, {"A", "B"}, "s")), \
             patch.object(N, "run_agent") as ra:
            out = N._final_test_fix_loop(_conv("t"), True, {"A", "B"})
        self.assertIsNone(out)
        ra.assert_not_called()

    def test_new_failure_is_fixed(self):
        # C is new vs baseline {A}; after one fix attempt it is gone → green.
        with patch.object(N.tr, "run_repo_tests",
                          side_effect=[(0, {"A", "C"}, "s1"), (0, {"A"}, "s2")]), \
             patch.object(N, "run_agent", return_value=(0, "")) as ra, \
             patch.object(N, "_stage_all"), patch.object(N, "_git"), \
             patch.object(N.ev, "emit"):
            out = N._final_test_fix_loop(_conv("t"), True, {"A"})
        self.assertIsNone(out)
        ra.assert_called_once()

    def test_unfixable_new_failure_escalates(self):
        with patch.object(N.tr, "run_repo_tests", return_value=(0, {"A", "C"}, "s")), \
             patch.object(N, "run_agent", return_value=(0, "")), \
             patch.object(N, "_stage_all"), patch.object(N, "_git"), \
             patch.object(N.ev, "emit"):
            out = N._final_test_fix_loop(_conv("t"), True, {"A"})
        self.assertIsNotNone(out)
        self.assertIn("escalation", out)
        self.assertIn("NEW", out["escalation"])


if __name__ == "__main__":
    unittest.main()
