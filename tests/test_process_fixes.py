"""Process fixes from the TASK-010 run.

- The final gate is baseline-aware: it tolerates failures present at task start
  and only auto-fixes NEW ones. (A frontend-only task must never send the fixer
  to delete backend code to turn a pre-existing red test green.)
- The lint gate parses eslint JSON, counting errors AND warnings, line-
  insensitive so a shifted pre-existing violation still matches its baseline.
"""
import json
import sys
import unittest
from unittest.mock import patch

from pipeline_graph import nodes as N, test_runner as tr
from pipeline_graph.state import Conversation

_finalize = sys.modules["pipeline_graph.nodes.finalize"]


def _conv(tid):
    """Minimal Conversation for `_final_test_fix_loop` tests: only `task_id`
    is read by the helper (for the `degraded` event); the rest is unused."""
    return Conversation(
        task_id=tid,
        request="",
        brief="",
        plan="",
        debate_history="",
        debate_ledger="",
        batch_context="{}",
        review_history="",
        final="",
        progress="",
        summary="",
        visual_review="",
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
        # Only baseline failures remain → no fixer runs, gate passes (green).
        from pipeline_graph import config as C
        with patch.object(C, "DRY_RUN", False), \
             patch.object(C, "TEST_SUITES", [C.TestSuite(label="s", cwd="", runner="pytest")]), \
             patch.object(N.tr, "run_repo_tests_detailed",
                          return_value=(0, {"A", "B"}, "s", 1)), \
             patch.object(N, "run_agent") as ra:
            out = N._final_test_fix_loop(_conv("t"), True, {"A", "B"})
        self.assertIsInstance(out, dict)
        self.assertEqual(out["status"], "green")
        self.assertEqual(out["ran_count"], 1)
        self.assertEqual(out["summary"], "s")
        self.assertEqual(out["new_failures"], [])
        ra.assert_not_called()

    def test_new_failure_is_fixed(self):
        # C is new vs baseline {A}; after one fix attempt it is gone → green.
        from pipeline_graph import config as C
        with patch.object(C, "DRY_RUN", False), \
             patch.object(C, "TEST_SUITES", [C.TestSuite(label="s", cwd="", runner="pytest")]), \
             patch.object(N.tr, "run_repo_tests_detailed",
                          side_effect=[(0, {"A", "C"}, "s1", 1),
                                       (0, {"A"}, "s2", 1)]), \
             patch.object(N, "run_agent", return_value=(0, "")) as ra, \
             patch.object(N, "_stage_all"), patch.object(N, "_git"), \
             patch.object(N.ev, "emit"):
            out = N._final_test_fix_loop(_conv("t"), True, {"A"})
        self.assertIsInstance(out, dict)
        self.assertEqual(out["status"], "green")
        self.assertEqual(out["ran_count"], 1)
        self.assertEqual(out["summary"], "s2")
        self.assertEqual(out["new_failures"], [])
        ra.assert_called_once()

    def test_unfixable_new_failure_escalates(self):
        from pipeline_graph import config as C
        with patch.object(C, "DRY_RUN", False), \
             patch.object(C, "TEST_SUITES", [C.TestSuite(label="s", cwd="", runner="pytest")]), \
             patch.object(N.tr, "run_repo_tests_detailed",
                          return_value=(0, {"A", "C"}, "s", 1)), \
             patch.object(N, "run_agent", return_value=(0, "")), \
             patch.object(N, "_stage_all"), patch.object(N, "_git"), \
             patch.object(N.ev, "emit"):
            out = N._final_test_fix_loop(_conv("t"), True, {"A"})
        self.assertIsInstance(out, dict)
        self.assertEqual(out["status"], "red")
        self.assertIn("escalation", out)
        self.assertIn("NEW", out["escalation"])
        self.assertIn("ran_count", out)
        self.assertEqual(out["summary"], "s")
        self.assertEqual(out["new_failures"], ["C"])

    def test_dry_run_returns_skipped_dict(self):
        # C13/B3: pre-gate dry-run → status="skipped", ran_count=0.
        from pipeline_graph import config as C
        with patch.object(C, "DRY_RUN", True):
            out = N._final_test_fix_loop(_conv("t"), True, set())
        self.assertIsInstance(out, dict)
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(out["ran_count"], 0)

    def test_db_down_returns_skipped_dict(self):
        from pipeline_graph import config as C
        with patch.object(C, "DRY_RUN", False):
            out = N._final_test_fix_loop(_conv("t"), False, set())
        self.assertIsInstance(out, dict)
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(out["ran_count"], 0)

    def test_empty_test_suites_returns_unconfigured(self):
        # Post-resolution empty TEST_SUITES → unconfigured (not skipped/green).
        from pipeline_graph import config as C
        with patch.object(C, "DRY_RUN", False), \
             patch.object(C, "TEST_SUITES", []), \
             patch.object(N.tr, "run_repo_tests_detailed",
                          return_value=(0, set(), "no suites", 0)), \
             patch.object(N, "run_agent") as ra:
            out = N._final_test_fix_loop(_conv("t"), True, set())
        self.assertIsInstance(out, dict)
        self.assertEqual(out["status"], "unconfigured")
        self.assertEqual(out["ran_count"], 0)
        ra.assert_not_called()

    def test_nonempty_suites_but_zero_ran_returns_skipped(self):
        # Non-empty TEST_SUITES but ran_count==0 (every suite skipped/
        # synthetic-failed) → skipped, NOT green.
        from pipeline_graph import config as C
        with patch.object(C, "DRY_RUN", False), \
             patch.object(C, "TEST_SUITES", [C.TestSuite(label="s", cwd="nope", runner="pytest")]), \
             patch.object(N.tr, "run_repo_tests_detailed",
                          return_value=(0, set(), "skipped", 0)), \
             patch.object(N, "run_agent") as ra:
            out = N._final_test_fix_loop(_conv("t"), True, set())
        self.assertIsInstance(out, dict)
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(out["ran_count"], 0)
        ra.assert_not_called()

    def test_post_fix_zero_ran_returns_skipped_not_green(self):
        # In-loop ran_count==0 (every suite skipped/synthetic-failed after a
        # fix attempt) → skipped, NOT green/red.
        from pipeline_graph import config as C
        with patch.object(C, "DRY_RUN", False), \
             patch.object(C, "TEST_SUITES", [C.TestSuite(label="s", cwd="", runner="pytest")]), \
             patch.object(N.tr, "run_repo_tests_detailed",
                          side_effect=[(0, {"A", "C"}, "s1", 1),
                                       (0, set(), "skipped", 0)]), \
             patch.object(N, "run_agent", return_value=(0, "")) as ra, \
             patch.object(N, "_stage_all"), patch.object(N, "_git"), \
             patch.object(N.ev, "emit"):
            out = N._final_test_fix_loop(_conv("t"), True, {"A"})
        self.assertIsInstance(out, dict)
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(out["ran_count"], 0)
        ra.assert_called_once()

    def test_every_exit_dict_carries_int_ran_count(self):
        # Every exit path returns a dict with an int ran_count key.
        from pipeline_graph import config as C
        cases = []
        # dry-run
        with patch.object(C, "DRY_RUN", True):
            cases.append(N._final_test_fix_loop(_conv("t"), True, set()))
        # db down
        with patch.object(C, "DRY_RUN", False):
            cases.append(N._final_test_fix_loop(_conv("t"), False, set()))
        # unconfigured
        with patch.object(C, "DRY_RUN", False), \
             patch.object(C, "TEST_SUITES", []), \
             patch.object(N.tr, "run_repo_tests_detailed",
                          return_value=(0, set(), "", 0)):
            cases.append(N._final_test_fix_loop(_conv("t"), True, set()))
        # green
        with patch.object(C, "DRY_RUN", False), \
             patch.object(C, "TEST_SUITES", [C.TestSuite(label="s", cwd="", runner="pytest")]), \
             patch.object(N.tr, "run_repo_tests_detailed",
                          return_value=(0, set(), "ok", 2)):
            cases.append(N._final_test_fix_loop(_conv("t"), True, set()))
        # skipped (ran_count==0 with non-empty suites)
        with patch.object(C, "DRY_RUN", False), \
             patch.object(C, "TEST_SUITES", [C.TestSuite(label="s", cwd="nope", runner="pytest")]), \
             patch.object(N.tr, "run_repo_tests_detailed",
                          return_value=(0, set(), "skipped", 0)):
            cases.append(N._final_test_fix_loop(_conv("t"), True, set()))
        # red
        with patch.object(C, "DRY_RUN", False), \
             patch.object(C, "TEST_SUITES", [C.TestSuite(label="s", cwd="", runner="pytest")]), \
             patch.object(N.tr, "run_repo_tests_detailed",
                          return_value=(0, {"A", "C"}, "s", 1)), \
             patch.object(N, "run_agent", return_value=(0, "")), \
             patch.object(N, "_stage_all"), patch.object(N, "_git"), \
             patch.object(N.ev, "emit"):
            cases.append(N._final_test_fix_loop(_conv("t"), True, {"A"}))
        for i, out in enumerate(cases):
            self.assertIsInstance(out, dict, f"case {i} not a dict")
            self.assertIn("ran_count", out, f"case {i} missing ran_count")
            self.assertIsInstance(out["ran_count"], int, f"case {i} ran_count not int")


class FinalCheckTestSummary(unittest.TestCase):
    """Items 46-50: final_check's test_summary kwarg authoritative flag."""

    def _state(self, **kw):
        st = {
            "task_id": "fc",
            "batch_idx": 0,
            "batches": [{"n": 1, "scope": "s", "checklist": []}],
            "journal": [],
            "task_baseline": [],
        }
        st.update(kw)
        return st

    def test_green_path_test_summary_is_authoritative(self):
        from pipeline_graph import config as C
        captured = {}

        def _capture(*a, **kw):
            captured.update(kw)
            return 0, "VERDICT: APPROVE\n" * 5

        with patch.object(C, "DRY_RUN", False), \
             patch.object(C, "TEST_SUITES", [C.TestSuite(label="s", cwd="", runner="pytest")]), \
             patch.object(_finalize, "_final_test_fix_loop",
                          return_value={"status": "green", "ran_count": 2, "summary": "ok"}), \
             patch.object(_finalize, "_db_note", return_value=(True, "")), \
             patch.object(N, "run_agent", side_effect=_capture), \
             patch.object(N.ev, "emit"):
            _finalize.final_check(self._state())
        ts = captured.get("test_summary", "")
        self.assertIn('authoritative="true"', ts)
        self.assertIn('status="green"', ts)

    def test_skipped_path_test_summary_is_non_authoritative(self):
        from pipeline_graph import config as C
        captured = {}

        def _capture(*a, **kw):
            captured.update(kw)
            return 0, "VERDICT: APPROVE\n" * 5

        with patch.object(C, "DRY_RUN", False), \
             patch.object(C, "TEST_SUITES", [C.TestSuite(label="s", cwd="", runner="pytest")]), \
             patch.object(_finalize, "_final_test_fix_loop",
                          return_value={"status": "skipped", "ran_count": 0, "summary": ""}), \
             patch.object(_finalize, "_db_note", return_value=(True, "")), \
             patch.object(N, "run_agent", side_effect=_capture), \
             patch.object(N.ev, "emit"):
            _finalize.final_check(self._state())
        ts = captured.get("test_summary", "")
        self.assertIn('authoritative="false"', ts)

    def test_waived_path_returns_cleanly_without_raising(self):
        # final_tests_waived=True → test_result is None, no _final_test_fix_loop
        # call, no escalation; final_check proceeds to the LLM review.
        from pipeline_graph import config as C
        captured = {}

        def _capture(*a, **kw):
            captured.update(kw)
            return 0, "VERDICT: APPROVE\n" * 5

        with patch.object(C, "DRY_RUN", False), \
             patch.object(_finalize, "_final_test_fix_loop",
                          side_effect=AssertionError("should not run")) as _loop, \
             patch.object(_finalize, "_db_note", return_value=(True, "")), \
             patch.object(N, "run_agent", side_effect=_capture), \
             patch.object(N.ev, "emit"):
            out = _finalize.final_check(self._state(final_tests_waived=True))
        _loop.assert_not_called()
        # test_summary is empty string on the waived path.
        self.assertEqual(captured.get("test_summary"), "")
        # No escalation on a clean waived path (the key may be present but
        # empty — the normal "0 not met" case).
        self.assertFalse(out.get("escalation"))

    def test_unconfigured_path_yields_non_authoritative_zero_ran(self):
        from pipeline_graph import config as C
        captured = {}

        def _capture(*a, **kw):
            captured.update(kw)
            return 0, "VERDICT: APPROVE\n" * 5

        with patch.object(C, "DRY_RUN", False), \
             patch.object(C, "TEST_SUITES", [C.TestSuite(label="s", cwd="", runner="pytest")]), \
             patch.object(_finalize, "_final_test_fix_loop",
                          return_value={"status": "unconfigured", "ran_count": 0, "summary": ""}), \
             patch.object(_finalize, "_db_note", return_value=(True, "")), \
             patch.object(N, "run_agent", side_effect=_capture), \
             patch.object(N.ev, "emit"):
            _finalize.final_check(self._state())
        ts = captured.get("test_summary", "")
        self.assertIn('status="unconfigured"', ts)
        self.assertIn('authoritative="false"', ts)

    def test_all_skipped_path_yields_skipped_non_authoritative_zero_ran(self):
        from pipeline_graph import config as C
        captured = {}

        def _capture(*a, **kw):
            captured.update(kw)
            return 0, "VERDICT: APPROVE\n" * 5

        with patch.object(C, "DRY_RUN", False), \
             patch.object(C, "TEST_SUITES", [C.TestSuite(label="s", cwd="", runner="pytest")]), \
             patch.object(_finalize, "_final_test_fix_loop",
                          return_value={"status": "skipped", "ran_count": 0, "summary": "skipped"}), \
             patch.object(_finalize, "_db_note", return_value=(True, "")), \
             patch.object(N, "run_agent", side_effect=_capture), \
             patch.object(N.ev, "emit"):
            _finalize.final_check(self._state())
        ts = captured.get("test_summary", "")
        self.assertIn('status="skipped"', ts)
        self.assertIn('authoritative="false"', ts)


if __name__ == "__main__":
    unittest.main()
