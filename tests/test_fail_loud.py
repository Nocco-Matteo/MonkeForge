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
        # File-primary rescue (TASK-027) prefers on-disk BATCHES+FINAL when
        # stdout is untrusted. Seed a valid BATCHES but no usable FINAL so
        # rescue cannot succeed — hard untrustworthy escalate must fire.
        import json
        import tempfile
        from pathlib import Path
        from pipeline_graph import config as C
        from pipeline_graph.agents import MIN_OUTPUT_BYTES

        st = _base_state()
        with tempfile.TemporaryDirectory() as td:
            final = Path(td)
            (final / "BATCHES-fl.json").write_text(json.dumps([
                {"n": 1, "scope": "s", "checklist": [1]},
            ]))
            # FINAL missing or below MIN_OUTPUT_BYTES → no rescue.
            (final / "FINAL-fl.md").write_text("x" * max(0, MIN_OUTPUT_BYTES - 1))
            with patch.object(C, "FINAL", final), \
                 patch.object(N, "run_agent", return_value=_UNTRUSTWORTHY), \
                 patch.object(N.ev, "emit"):
                d = _finalize.judge(st)
        self.assertIn("escalation", d)
        self.assertIn("untrustworthy", d["escalation"])
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
    def test_bare_verdict_marker_is_trusted(self):
        # A bare "VERDICT: APPROVE" (16 bytes) is the expected output when the
        # UX filter deletes all items — _trust_output whitelists terminal
        # markers, so the guard must NOT fire.
        short_with_verdict = (0, "VERDICT: APPROVE\n")
        st = _base_state(debate_round=1, has_ui=True)
        with patch.object(_debate, "run_agent", return_value=short_with_verdict), \
             patch.object(N.ev, "emit"):
            d = _debate.debate_ux(st)
        self.assertNotIn("escalation", d)

    def test_short_garbage_without_marker_escalates(self):
        # Short output with no terminal marker and no parseable verdict →
        # still untrustworthy (the guard fires).
        st = _base_state(debate_round=1, has_ui=True)
        with patch.object(_debate, "run_agent", return_value=(0, "???\n")), \
             patch.object(N.ev, "emit"):
            d = _debate.debate_ux(st)
        self.assertIn("escalation", d)

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
    def test_bare_verdict_marker_is_trusted(self):
        # A bare "VERDICT: APPROVE" is a valid terminal marker — the guard
        # must NOT fire (_trust_output whitelists it).
        short_with_verdict = (0, "VERDICT: APPROVE\n")
        st = _base_state(ux_render_cycle=0)
        with patch.object(N, "run_agent", return_value=short_with_verdict), \
             patch.object(N.ev, "emit"):
            d = _qg.ux_visual_review(st)
        self.assertNotIn("escalation", d)

    def test_short_garbage_without_marker_escalates(self):
        st = _base_state(ux_render_cycle=0)
        with patch.object(N, "run_agent", return_value=(0, "???\n")), \
             patch.object(N.ev, "emit"):
            d = _qg.ux_visual_review(st)
        self.assertIn("escalation", d)

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
        self.assertEqual({o["key"] for o in opts}, {"ok", "stop"})

    def test_killed_by_signal_matches(self):
        opts = self._options("implementer process killed by signal 9 (infrastructure)")
        self.assertEqual({o["key"] for o in opts}, {"ok", "stop"})

    def test_agent_daemon_crash_matches(self):
        opts = self._options("implementer process killed by signal 13 (agent-daemon crash), batch 1")
        self.assertEqual({o["key"] for o in opts}, {"ok", "stop"})

    def test_no_skip_close_force_keys(self):
        opts = self._options("step crashed in something")
        for banned in ("skip", "close", "force"):
            self.assertNotIn(banned, " ".join(o["key"] for o in opts))

    def test_non_crash_does_not_match_crash_branch(self):
        # A generic escalation must NOT get the concise ok/stop menu.
        opts = self._options("some random escalation reason")
        self.assertIn("skip / close / force", " ".join(o["key"] for o in opts))


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


class EscalateStopIsUniversal(unittest.TestCase):
    """"stop" is accepted by the answer validator for every escalation, so it
    must actually stop the run — not fall through to the generic tail and
    proceed (the silent-approval class of bug)."""

    DEBATE = "debate exhausted 3 rounds + verification: 2 technical blocker(s) confirmed"
    UX = "the designer raised 2 UX blocker(s)"
    VISUAL = "visual issues remain after 3 fix cycles"

    def _escalate(self, answer, reason):
        st = {"task_id": "fl", "escalation": reason, "journal": []}
        with patch.object(_common, "interrupt", return_value=answer), \
             patch.object(_common.ev, "emit"), \
             patch.object(_common.ev, "open_escalation", return_value=True), \
             patch.object(_common.ev, "close_escalation"):
            return _common.escalate(st)

    def test_stop_stops_debate_escalation(self):
        d = self._escalate("stop", self.DEBATE)
        self.assertTrue(d.get("finished"))

    def test_stop_stops_ux_escalation(self):
        d = self._escalate("stop", self.UX)
        self.assertTrue(d.get("finished"))
        # Must not record a "shipped with unresolved blockers" degradation:
        # nothing shipped, the human stopped.
        self.assertNotIn("degradations", d)

    def test_stop_stops_visual_escalation(self):
        d = self._escalate("stop", self.VISUAL)
        self.assertTrue(d.get("finished"))

    def test_stop_aliases_stop_the_run(self):
        for alias in ("no", "abort", "cancel", "STOP", " stop "):
            with self.subTest(alias=alias):
                self.assertTrue(self._escalate(alias, self.DEBATE).get("finished"))

    def test_debate_menu_offers_the_requirements_escape(self):
        """A debate that cannot converge because the BRIEF is wrong has no
        in-graph fix; the menu must say so instead of only offering answers
        that replay the same plan."""
        opts = _common._escalation_options(self.DEBATE)
        keys = {o["key"] for o in opts}
        self.assertIn("stop", keys)
        stop_label = next(o["label"] for o in opts if o["key"] == "stop")
        self.assertIn("--from plan", stop_label)

    def test_intake_stop_ends_interview_not_the_run(self):
        """Exempt: for the intake interview "stop" is an INTAKE_END_ANSWERS
        member meaning "stop interviewing"."""
        d = self._escalate("stop", "intake still unresolved after 3 rounds")
        self.assertFalse(d.get("finished"))

    def test_non_stop_answer_still_proceeds(self):
        d = self._escalate("ok", self.DEBATE)
        self.assertFalse(d.get("finished"))

    def test_judge_escalation_ok_retries_judge(self):
        reason = "judge escalated: none — false positive"
        d = self._escalate("ok", reason)
        self.assertTrue(d.get("retry_judge"))
        self.assertFalse(d.get("finished"))

    def test_judge_escalation_stop_ends_run(self):
        reason = "judge escalated: scope change needed"
        d = self._escalate("stop", reason)
        self.assertTrue(d.get("finished"))
        self.assertFalse(d.get("retry_judge"))


class JudgeUnlinkMissingOk(unittest.TestCase):
    """F2: judge uses unlink(missing_ok=True) so a missing file does not raise."""

    def test_unlink_missing_ok_does_not_raise(self, ):
        """The unlink calls in judge use missing_ok=True so a retry that
        already cleared the file does not crash on the second unlink."""
        import inspect
        source = inspect.getsource(_finalize.judge)
        self.assertIn("unlink(missing_ok=True)", source)


if __name__ == "__main__":
    unittest.main()
