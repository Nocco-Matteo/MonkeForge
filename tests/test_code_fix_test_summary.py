"""TASK-030 item 64: ``code_fix``'s ``test_summary`` kwarg is non-empty on the
green+non-skip-summary ``last_gate_*`` state, and empty on the
skipped/empty/skip-sentinel state.
"""
import sys
import unittest
from unittest.mock import patch

from pipeline_graph import config as C
from pipeline_graph.state import Conversation
import pipeline_graph.nodes.review  # noqa: F401 — ensure module is loaded

_review = sys.modules["pipeline_graph.nodes.review"]


def _conv(task_id: str = "cf") -> Conversation:
    return Conversation(
        task_id=task_id,
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


def _state(**kw) -> dict:
    st = {
        "task_id": "cf",
        "batch_idx": 0,
        "batches": [{"n": 1, "scope": "s", "checklist": []}],
        "fix_cycle": 0,
        "journal": [],
    }
    st.update(kw)
    return st


class TestCodeFixTestSummary(unittest.TestCase):
    def _capture(self):
        captured = {}

        def _cap(*args, **kw):
            captured.update(kw)
            return 0, "FIXED x.ts:1\nDEVIATIONS: none"
        return captured, _cap

    def test_green_non_skip_summary_yields_non_empty_test_summary(self):
        captured, _cap = self._capture()
        state = _state(
            last_gate_status="green",
            last_gate_summary="5 passed, 0 failed",
            last_gate_failures=[],
        )
        with patch.object(C, "TEST_SUITES",
                          [C.TestSuite(label="s", cwd="", runner="pytest")]), \
             patch.object(_review, "run_agent", side_effect=_cap), \
             patch.object(_review, "_stage_all"):
            _review.code_fix(state)
        ts = captured.get("test_summary", "")
        assert ts != "", "green+non-skip-summary state must yield a non-empty test_summary"
        assert 'status="green"' in ts
        assert 'authoritative="true"' in ts

    def test_skipped_status_yields_empty_test_summary(self):
        captured, _cap = self._capture()
        state = _state(
            last_gate_status="skipped",
            last_gate_summary="",
            last_gate_failures=[],
        )
        with patch.object(C, "TEST_SUITES",
                          [C.TestSuite(label="s", cwd="", runner="pytest")]), \
             patch.object(_review, "run_agent", side_effect=_cap), \
             patch.object(_review, "_stage_all"):
            _review.code_fix(state)
        assert captured.get("test_summary", "sentinel") == ""

    def test_empty_summary_yields_empty_test_summary(self):
        captured, _cap = self._capture()
        state = _state(
            last_gate_status="green",
            last_gate_summary="",
            last_gate_failures=[],
        )
        with patch.object(C, "TEST_SUITES",
                          [C.TestSuite(label="s", cwd="", runner="pytest")]), \
             patch.object(_review, "run_agent", side_effect=_cap), \
             patch.object(_review, "_stage_all"):
            _review.code_fix(state)
        assert captured.get("test_summary", "sentinel") == ""

    def test_skip_sentinel_summary_yields_empty_test_summary(self):
        captured, _cap = self._capture()
        state = _state(
            last_gate_status="green",
            last_gate_summary="tests waived",
            last_gate_failures=[],
        )
        with patch.object(C, "TEST_SUITES",
                          [C.TestSuite(label="s", cwd="", runner="pytest")]), \
             patch.object(_review, "run_agent", side_effect=_cap), \
             patch.object(_review, "_stage_all"):
            _review.code_fix(state)
        assert captured.get("test_summary", "sentinel") == ""


if __name__ == "__main__":
    unittest.main()
