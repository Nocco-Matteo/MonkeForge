"""F1: fail-loud trust guard on decision nodes + plain-language crash reasons +
broadened crash predicate.

Every decision node that reads an agent's verdict must refuse to act on
untrustworthy output (non-zero exit, near-empty stdout, or a non-OK health
classification) and escalate instead.  The escalation string carries a
plain-language reason (no ``health=``/``exit=`` diagnostics — those stay in
the journal).  A crash inside a node is converted to a plain-language
escalation (``step crashed in <name>``) with the exception type kept in the
journal and the ``step_error`` event only.  The crash-predicate in
``_escalation_options`` and the ``crash_escalation`` branch in ``escalate()``
cover signal-killed implementers (OOM / daemon death) with a concise ok/stop
menu that never force-closes a batch.
"""
import unittest
from unittest.mock import patch, MagicMock

from pipeline_graph import nodes as N
from pipeline_graph.nodes import common as _common
from pipeline_graph.nodes import review as _review
from pipeline_graph.nodes import debate as _debate
from pipeline_graph.nodes import finalize as _finalize
from pipeline_graph.nodes import quality_gates as _qg


# ≥40 bytes so classify_output returns "ok" on the healthy path.
_HEALTHY = "VERDICT: APPROVE\nNo issues found. All checks passed.\n"
_UNTRUSTWORTHY = (1, "boom — something went wrong here")


def _base_state(**kw):
    st = {
        "task_id": "fl",
        "batch_idx": 0,
        "batches": [{"n": 1, "scope": "s", "checklist": [1]}],
        "fix_cycle": 0,
        "debate_round": 0,
        "has_ui": False,
        "tech_limits": [],
        "ux_render_cycle": 0,
        "final_tests_waived": True,
        "escalation": "",
        "journal": [],
    }
    st.update(kw)
    return st


# ---------------------------------------------------------------------------
# Items 2-3: code_review / code_verify
# ---------------------------------------------------------------------------


class CodeReviewGuard(unittest.TestCase):
    def test_untrustworthy_output_escalates(self):
        st = _base_state()
        with patch.object(_review, "run_agent", return_value=_UNTRUSTWORTHY), \
             patch.object(N.ev, "emit"):
            d = _review.code_review(st)
        self.assertIn("escalation", d)
        self.assertNotIn("health=", d["escalation"])
        self.assertNotIn("exit=", d["escalation"])
        self.assertIn("health=", d["journal"][-1])
        self.assertIn("exit=", d["journal"][-1])

    def test_healthy_output_passes_through(self):
        st = _base_state()
        with patch.object(_review, "run_agent", return_value=(0, _HEALTHY)), \
             patch.object(N.ev, "emit"):
            d = _review.code_review(st)
        self.assertNotIn("escalation", d)


class CodeVerifyGuard(unittest.TestCase):
    def test_untrustworthy_output_escalates_before_not_fixed_check(self):
        st = _base_state(fix_cycle=1)
        with patch.object(_review, "run_agent", return_value=_UNTRUSTWORTHY), \
             patch.object(N.ev, "emit"):
            d = _review.code_verify(st)
        self.assertIn("escalation", d)
        self.assertNotIn("health=", d["escalation"])
        self.assertIn("health=", d["journal"][-1])

    def test_healthy_output_passes_through(self):
        st = _base_state(fix_cycle=1)
        with patch.object(_review, "run_agent", return_value=(0, _HEALTHY)), \
             patch.object(N.ev, "emit"):
            d = _review.code_verify(st)
        # No NOT_FIXED in healthy output → confirmed, no escalation.
        self.assertNotIn("escalation", d)


# ---------------------------------------------------------------------------
# Items 4-6: summary / judge / final_check
# ---------------------------------------------------------------------------


class SummaryGuard(unittest.TestCase):
    def test_untrustworthy_output_escalates(self):
        st = _base_state()
        with patch.object(N, "run_agent", return_value=_UNTRUSTWORTHY), \
             patch.object(N.ev, "emit"):
            d = _finalize.summary(st)
        self.assertIn("escalation", d)
        self.assertNotIn("health=", d["escalation"])
        self.assertIn("health=", d["journal"][-1])

    def test_healthy_output_passes_through(self):
        st = _base_state()
        with patch.object(N, "run_agent", return_value=(0, _HEALTHY)), \
             patch.object(N.ev, "emit"):
            d = _finalize.summary(st)
        self.assertNotIn("escalation", d)


class JudgeGuard(unittest.TestCase):
    def test_untrustworthy_output_escalates_after_escalate_check(self):
        st = _base_state()
        with patch.object(N, "run_agent", return_value=_UNTRUSTWORTHY), \
             patch.object(N.ev, "emit"):
            d = _finalize.judge(st)
        self.assertIn("escalation", d)
        self.assertNotIn("health=", d["escalation"])
        self.assertIn("health=", d["journal"][-1])

    def test_healthy_output_proceeds_to_json_parse(self):
        st = _base_state()
        with patch.object(N, "run_agent", return_value=(0, _HEALTHY)), \
             patch.object(_finalize, "_extract_json", return_value=[{"n": 1, "scope": "s"}]), \
             patch.object(N.ev, "emit"):
            d = _finalize.judge(st)
        # Healthy output with patched JSON → batches produced, no guard escalation.
        self.assertNotIn("escalation", d)
        self.assertIn("batches", d)


class FinalCheckGuard(unittest.TestCase):
    def test_untrustworthy_output_escalates_before_parse_not_met(self):
        st = _base_state(final_tests_waived=True)
        with patch.object(N, "run_agent", return_value=_UNTRUSTWORTHY), \
             patch.object(N.ev, "emit"):
            d = _finalize.final_check(st)
        self.assertIn("escalation", d)
        self.assertNotIn("health=", d["escalation"])
        self.assertIn("health=", d["journal"][-1])

    def test_healthy_output_passes_through(self):
        st = _base_state(final_tests_waived=True)
        with patch.object(N, "run_agent", return_value=(0, _HEALTHY)), \
             patch.object(N.ev, "emit"):
            d = _finalize.final_check(st)
        # final_check always sets "escalation" (empty when clean); the guard
        # must not have fired, so it must be empty, not an untrustworthy-output
        # message.
        self.assertFalse(d.get("escalation"))


# ---------------------------------------------------------------------------
# Items 7-8: debate_tech / debate_ux
# ---------------------------------------------------------------------------


class DebateTechGuard(unittest.TestCase):
    def test_untrustworthy_output_escalates(self):
        st = _base_state(debate_round=0)
        with patch.object(_debate, "run_agent", return_value=_UNTRUSTWORTHY), \
             patch.object(N.ev, "emit"):
            d = _debate.debate_tech(st)
        self.assertIn("escalation", d)
        self.assertNotIn("health=", d["escalation"])
        self.assertIn("health=", d["journal"][-1])

    def test_healthy_output_passes_through(self):
        st = _base_state(debate_round=0, has_ui=False)
        with patch.object(_debate, "run_agent", return_value=(0, _HEALTHY)), \
             patch.object(N.ev, "emit"):
            d = _debate.debate_tech(st)
        self.assertNotIn("escalation", d)


class DebateUxGuard(unittest.TestCase):
    def test_untrustworthy_output_with_verdict_escalates(self):
        # Output has a parseable VERDICT but is too short → untrustworthy.
        short_with_verdict = (0, "VERDICT: APPROVE\n")
        st = _base_state(debate_round=1, has_ui=True)
        with patch.object(_debate, "run_agent", return_value=short_with_verdict), \
             patch.object(N.ev, "emit"):
            d = _debate.debate_ux(st)
        self.assertIn("escalation", d)
        self.assertNotIn("health=", d["escalation"])
        self.assertIn("health=", d["journal"][-1])

    def test_healthy_output_passes_through(self):
        st = _base_state(debate_round=1, has_ui=True)
        with patch.object(_debate, "run_agent", return_value=(0, _HEALTHY)), \
             patch.object(N.ev, "emit"):
            d = _debate.debate_ux(st)
        self.assertNotIn("escalation", d)


# ---------------------------------------------------------------------------
# Item 9: ux_visual_review
# ---------------------------------------------------------------------------


class UxVisualReviewGuard(unittest.TestCase):
    def test_untrustworthy_output_with_verdict_escalates(self):
        short_with_verdict = (0, "VERDICT: APPROVE\n")
        st = _base_state(ux_render_cycle=0)
        with patch.object(N, "run_agent", return_value=short_with_verdict), \
             patch.object(N.ev, "emit"):
            d = _qg.ux_visual_review(st)
        self.assertIn("escalation", d)
        self.assertNotIn("health=", d["escalation"])
        self.assertIn("health=", d["journal"][-1])

    def test_healthy_output_passes_through(self):
        st = _base_state(ux_render_cycle=0)
        with patch.object(N, "run_agent", return_value=(0, _HEALTHY)), \
             patch.object(N.ev, "emit"):
            d = _qg.ux_visual_review(st)
        self.assertNotIn("escalation", d)


# ---------------------------------------------------------------------------
# Item 10: instrument() plain-language crash escalation
# ---------------------------------------------------------------------------


class InstrumentCrashLanguage(unittest.TestCase):
    def test_crash_escalation_is_plain_language(self):
        def boom(state):
            raise RuntimeError("secret internal detail")

        wrapped = _common.instrument("test_node", boom)
        with patch.object(N.ev, "emit"):
            d = wrapped({"task_id": "fl"})
        self.assertIn("escalation", d)
        self.assertIn("step crashed", d["escalation"])
        self.assertNotIn("RuntimeError", d["escalation"])

    def test_journal_keeps_exception_type(self):
        def boom(state):
            raise RuntimeError("secret internal detail")

        wrapped = _common.instrument("test_node", boom)
        with patch.object(N.ev, "emit"):
            d = wrapped({"task_id": "fl"})
        self.assertIn("RuntimeError", d["journal"][-1])

    def test_step_error_event_keeps_exception_type(self):
        def boom(state):
            raise RuntimeError("secret internal detail")

        wrapped = _common.instrument("test_node", boom)
        with patch.object(N.ev, "emit") as mock_emit:
            wrapped({"task_id": "fl"})
        step_error_msgs = [c.args[3] for c in mock_emit.call_args_list
                           if c.args and c.args[0] == "step_error"]
        self.assertTrue(any("RuntimeError" in m for m in step_error_msgs),
                        f"step_error event must carry the exception type; got {step_error_msgs}")


# ---------------------------------------------------------------------------
# Item 11: _escalation_options crash branch
# ---------------------------------------------------------------------------


class EscalationOptionsCrashBranch(unittest.TestCase):
    def _options(self, reason):
        return _common._escalation_options(reason)

    def test_crashed_matches(self):
        opts = self._options("step crashed in code_review")
        self.assertEqual(set(opts.keys()), {"ok", "stop"})

    def test_killed_by_signal_matches(self):
        opts = self._options("implementer process killed by signal 9 (infrastructure)")
        self.assertEqual(set(opts.keys()), {"ok", "stop"})

    def test_agent_daemon_crash_matches(self):
        opts = self._options("implementer process killed by signal 13 (agent-daemon crash), batch 1")
        self.assertEqual(set(opts.keys()), {"ok", "stop"})

    def test_no_skip_close_force_keys(self):
        opts = self._options("step crashed in something")
        for banned in ("skip", "close", "force"):
            self.assertNotIn(banned, " ".join(opts.keys()))

    def test_non_crash_does_not_match_crash_branch(self):
        # A generic escalation must NOT get the concise ok/stop menu.
        opts = self._options("some random escalation reason")
        self.assertIn("skip / close / force", " ".join(opts.keys()))


# ---------------------------------------------------------------------------
# Item 12: escalate() crash_escalation branch
# ---------------------------------------------------------------------------


class EscalateCrashBranch(unittest.TestCase):
    def _escalate(self, answer, reason):
        st = {"task_id": "fl", "escalation": reason, "journal": []}
        with patch.object(_common, "interrupt", return_value=answer), \
             patch.object(_common.ev, "emit"), \
             patch.object(_common.ev, "open_escalation", return_value=True), \
             patch.object(_common.ev, "close_escalation"):
            return _common.escalate(st)

    def test_stop_sets_finished(self):
        d = self._escalate("stop", "step crashed in code_review")
        self.assertTrue(d.get("finished"))

    def test_ok_clears_stale_verdicts(self):
        d = self._escalate("ok", "step crashed in code_review")
        self.assertEqual(d.get("escalation"), "")
        self.assertIsNone(d.get("code_verdict"))
        self.assertEqual(d.get("open_blockers"), 0)
        self.assertEqual(d.get("not_met"), [])

    def test_ok_sets_no_batch_force_close_keys(self):
        d = self._escalate("ok", "step crashed in code_review")
        # The forced branch sets code_verdict="APPROVE" and degradations —
        # the crash branch must NOT.
        self.assertNotEqual(d.get("code_verdict"), "APPROVE")
        self.assertNotIn("degradations", d)

    def test_killed_by_signal_triggers_crash_branch(self):
        d = self._escalate("ok", "implementer killed by signal 9, batch 1")
        self.assertEqual(d.get("escalation"), "")
        self.assertIsNone(d.get("code_verdict"))


if __name__ == "__main__":
    unittest.main()
