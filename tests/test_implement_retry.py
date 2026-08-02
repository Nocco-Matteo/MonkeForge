"""F2: the implement retry prompt carries the previous attempt's failing tests
and summary, and the ``test_fix_failures`` / ``test_fix_summary`` state fields
are cleared on every exit path that leaves the batch loop or re-enters a fresh
attempt (tests_waived, success, close_batch, escalate forced, escalate redo).

The retry prompt is the single highest-risk item in this batch: if ``{failures}``
/ ``{summary}`` are not real ``{key}`` tokens matching the ``extra_kw`` passed by
``implement()``, the retry prompt silently ships broken content forever (no
exception, no test failure unless explicitly asserted).  These tests catch that
class of regression.
"""
import unittest
from unittest.mock import patch, MagicMock

from pipeline_graph import agents as A, config as C
from pipeline_graph import nodes as N
from pipeline_graph.nodes import common as _common
from pipeline_graph.nodes.common import DB_OK_NOTE
from pipeline_graph.state import Conversation

# The nodes package re-exports `implement` (the function), shadowing the
# submodule of the same name. To patch module-level helpers like
# `_in_graph_test_gate` we need the actual module object from sys.modules.
import sys
_impl = sys.modules["pipeline_graph.nodes.implement"]


def _conv(task_id: str = "retry") -> Conversation:
    """Minimal frozen Conversation for render_prompt (all fields required)."""
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


def _base_state(**kw) -> dict:
    st = {
        "task_id": "retry",
        "batch_idx": 0,
        "batches": [{"n": 1, "scope": "s", "checklist": [1]}],
        "fix_cycle": 0,
        "test_fix_attempt": 0,
        "test_fix_failures": [],
        "test_fix_summary": "",
        "escalation": "",
        "journal": [],
    }
    st.update(kw)
    return st


# ---------------------------------------------------------------------------
# Item 22 + 17: the rendered prompt carries failures/summary on retry, empty on
# first attempt.


class TestRenderedPrompt:
    def test_retry_prompt_contains_failures_and_summary(self):
        failures = ["tests/foo.test.ts > foo > bar", "tests/baz.test.ts > baz"]
        summary = "2 failed, 3 passed, 5 total"
        prompt = A.render_prompt(
            "implement", _conv(),
            batch_n=1, batch_scope="s", db_note=DB_OK_NOTE,
            arch_docs="", checklist_items="1",
            failures=failures, summary=summary,
        )
        # The failing-test identifiers appear verbatim...
        for f in failures:
            assert f in prompt, f"failure identifier missing from prompt: {f}"
        # ...and the summary line appears verbatim.
        assert summary in prompt, "test summary missing from prompt"
        assert "FAILING TESTS:" in prompt
        assert "TEST SUMMARY:" in prompt

    def test_first_attempt_prompt_has_empty_failures_and_summary(self):
        prompt = A.render_prompt(
            "implement", _conv(),
            batch_n=1, batch_scope="s", db_note=DB_OK_NOTE,
            arch_docs="", checklist_items="1",
            failures=[], summary="",
        )
        # The placeholder lines are present but the values are empty.
        assert "FAILING TESTS:" in prompt
        assert "TEST SUMMARY:" in prompt
        # No stale {failures}/{summary} literal tokens survive substitution.
        assert "{failures}" not in prompt
        assert "{summary}" not in prompt

    def test_implement_md_has_no_settled_prose(self):
        """The template must not carry the corrupted ## Settled debate block."""
        text = (C.TEMPLATES / "implement.md").read_text()
        assert "## Settled" not in text, "implement.md contains ## Settled prose"
        assert "F3 routing" not in text
        assert "debate-settlement" not in text.lower()


# ---------------------------------------------------------------------------
# Item 17: implement() threads failures=/summary= into run_agent from state.


class TestImplementThreadsKwargs:
    def test_run_agent_receives_state_failures_and_summary(self):
        captured = {}

        def _capture(*args, **kw):
            captured.update(kw)
            return 0, "VERDICT: APPROVE\n" * 5

        state = _base_state(
            test_fix_attempt=1,
            test_fix_failures=["tests/x.test.ts > x > y"],
            test_fix_summary="1 failed, 2 passed",
        )
        with patch.object(C, "DRY_RUN", True), \
             patch.object(N, "run_agent", side_effect=_capture), \
             patch.object(_impl, "_in_graph_test_gate",
                          return_value=(True, [], "all passed")), \
             patch.object(N.ev, "emit"):
            N.implement(state)
        assert captured.get("failures") == ["tests/x.test.ts > x > y"]
        assert captured.get("summary") == "1 failed, 2 passed"

    def test_first_attempt_passes_empty_failures_and_summary(self):
        captured = {}

        def _capture(*args, **kw):
            captured.update(kw)
            return 0, "VERDICT: APPROVE\n" * 5

        state = _base_state(test_fix_attempt=0)
        with patch.object(C, "DRY_RUN", True), \
             patch.object(N, "run_agent", side_effect=_capture), \
             patch.object(_impl, "_in_graph_test_gate",
                          return_value=(True, [], "all passed")), \
             patch.object(N.ev, "emit"):
            N.implement(state)
        assert captured.get("failures") == []
        assert captured.get("summary") == ""


# ---------------------------------------------------------------------------
# Item 19: the not-ok retry delta carries the new failures + summary.


class TestRetryDeltaCarriesFailures:
    def test_not_ok_retry_sets_failures_and_summary(self):
        new_fails = ["tests/a.test.ts > a", "tests/b.test.ts > b"]
        summary = "2 failed, 1 passed"
        state = _base_state(test_fix_attempt=0)
        with patch.object(C, "DRY_RUN", True), \
             patch.object(N, "run_agent",
                          return_value=(0, "VERDICT: APPROVE\n" * 5)), \
             patch.object(_impl, "_in_graph_test_gate",
                          return_value=(False, new_fails, summary)), \
             patch.object(N.ev, "emit"):
            out = N.implement(state)
        assert out["test_fix_failures"] == new_fails
        assert out["test_fix_summary"] == summary
        assert out["test_fix_attempt"] == 1


# ---------------------------------------------------------------------------
# Items 18 / 20 / 21: clearing on tests_waived, success, close_batch.


class TestClearingOnExitPaths:
    def test_item_18_tests_waived_clears_fields(self):
        state = _base_state(
            tests_waived=True,
            test_fix_failures=["stale"],
            test_fix_summary="stale summary",
        )
        with patch.object(N.ev, "emit"):
            out = N.implement(state)
        assert out["test_fix_failures"] == []
        assert out["test_fix_summary"] == ""

    def test_item_20_success_clears_fields(self):
        state = _base_state(
            test_fix_attempt=1,
            test_fix_failures=["tests/old.test.ts > old"],
            test_fix_summary="1 failed before",
        )
        with patch.object(C, "DRY_RUN", True), \
             patch.object(N, "run_agent",
                          return_value=(0, "VERDICT: APPROVE\n" * 5)), \
             patch.object(_impl, "_in_graph_test_gate",
                          return_value=(True, [], "all passed")), \
             patch.object(N.ev, "emit"):
            out = N.implement(state)
        assert out["test_fix_failures"] == []
        assert out["test_fix_summary"] == ""

    def test_item_21_close_batch_clears_fields(self):
        state = _base_state(
            test_fix_failures=["stale"],
            test_fix_summary="stale",
            batches=[{"n": 1, "scope": "s", "status": "PENDING",
                      "outcome": "", "deviations": "", "checklist": []}],
            code_verdict="APPROVE",
        )
        with patch.object(C, "DRY_RUN", True), \
             patch.object(_impl, "_write_progress"), \
             patch.object(_impl.subprocess, "run"), \
             patch.object(N.ev, "emit"):
            out = _impl.close_batch(state)
        assert out["test_fix_failures"] == []
        assert out["test_fix_summary"] == ""


# ---------------------------------------------------------------------------
# Items 23 / 24: clearing on escalate() forced and redo branches.


class TestEscalateClearing:
    def _state_with_failures(self, reason: str) -> dict:
        return {
            "task_id": "esc",
            "escalation": reason,
            "test_fix_failures": ["tests/stale.test.ts > stale"],
            "test_fix_summary": "stale summary",
            "journal": [],
            "batches": [{"n": 1, "scope": "s", "checklist": []}],
            "batch_idx": 0,
        }

    def _escalate(self, answer: str, reason: str) -> dict:
        from langgraph.types import interrupt as _interrupt
        state = self._state_with_failures(reason)
        with patch.object(_common, "interrupt", return_value=answer), \
             patch.object(_common.ev, "open_escalation", return_value=True), \
             patch.object(_common.ev, "close_escalation"), \
             patch.object(_common.ev, "emit"):
            return _common.escalate(state)

    def test_item_23_forced_clears_fields(self):
        out = self._escalate("skip", "tests still failing after 3 attempts (batch 1)")
        assert out["test_fix_failures"] == []
        assert out["test_fix_summary"] == ""

    def test_item_24_redo_clears_fields(self):
        out = self._escalate("redo", "debate hit the round cap (round 5)")
        assert out["test_fix_failures"] == []
        assert out["test_fix_summary"] == ""


if __name__ == "__main__":
    unittest.main()
