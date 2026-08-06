"""F3: the UX critic is gated on ``eyes_engaged`` at both the router
(``route_after_tech``) and the debate-decision layer (``debate_tech`` /
``_debate_decision``).

A UI task whose repo has no eyes engagement signal (no usable ``ui:`` yaml, no
checkpointed ``ui_config``, no legacy ``UX_RENDER_CMD``) must skip the designer
critique the same way a non-UI task does — otherwise ``has_ui`` defaulting True
drags a backend repo into a UX critic that renders nothing and escalates on
phantom blockers. The bypass journal copy is the plain-language "visual review
disabled — eyes not engaged for this task", not the bare "UX_RENDER_CMD empty".
"""
import unittest

from pipeline_graph import config as C
from pipeline_graph import graph as G
from pipeline_graph.nodes import debate as D


class RouteAfterTech(unittest.TestCase):
    def _state(self, **kw):
        base = {"has_ui": True, "debate_next": "reply"}
        base.update(kw)
        return base

    def test_has_ui_with_empty_render_cmd_skips_ux(self):
        prev = C.UX_RENDER_CMD
        try:
            C.UX_RENDER_CMD = "   "
            self.assertEqual(G.route_after_tech(self._state()), "reply")
        finally:
            C.UX_RENDER_CMD = prev

    def test_has_ui_with_set_render_cmd_routes_to_ux(self):
        prev = C.UX_RENDER_CMD
        try:
            C.UX_RENDER_CMD = "npx playwright test"
            self.assertEqual(G.route_after_tech(self._state()), "ux")
        finally:
            C.UX_RENDER_CMD = prev

    def test_no_has_ui_with_render_cmd_routes_to_ux(self):
        # Per README: any non-empty PIPELINE_UX_RENDER_CMD engages eyes,
        # regardless of has_ui. So has_ui=False + render_cmd set → UX.
        prev = C.UX_RENDER_CMD
        try:
            C.UX_RENDER_CMD = "npx playwright test"
            self.assertEqual(G.route_after_tech(self._state(has_ui=False)), "ux")
        finally:
            C.UX_RENDER_CMD = prev

    def test_escalation_wins_over_ui_gate(self):
        prev = C.UX_RENDER_CMD
        try:
            C.UX_RENDER_CMD = "npx playwright test"
            self.assertEqual(
                G.route_after_tech(self._state(escalation="boom")), "escalate")
        finally:
            C.UX_RENDER_CMD = prev


class DebateTechSkip(unittest.TestCase):
    """debate_tech's UX-skip condition and bypass journal copy.

    The full node shells out to the reviewer agent; we exercise the skip branch
    by stubbing ``run_agent`` and ``classify_output`` so the trust guard passes
    and the decision path runs.
    """
    def _run_debate_tech(self, has_ui, render_cmd):
        prev_cmd = C.UX_RENDER_CMD
        prev_run = D.run_agent
        prev_classify = D.classify_output
        prev_trust = D._trust_output
        try:
            C.UX_RENDER_CMD = render_cmd
            D.run_agent = lambda *a, **k: (0, "VERDICT: APPROVE\nall good here, shipping it now")
            D.classify_output = lambda code, out: ("ok", None)
            D._trust_output = lambda code, out, health: True
            state = {
                "task_id": "T-gate",
                "debate_round": 0,
                "has_ui": has_ui,
                "reviewer_verdict": "APPROVE",
                "open_blockers": 0,
            }
            return D.debate_tech(state)
        finally:
            C.UX_RENDER_CMD = prev_cmd
            D.run_agent = prev_run
            D.classify_output = prev_classify
            D._trust_output = prev_trust

    def test_has_ui_empty_cmd_skips_ux_and_journals_bypass(self):
        out = self._run_debate_tech(has_ui=True, render_cmd="   ")
        # debate_tech decided the round itself (no UX critic) → summary.
        self.assertEqual(out.get("debate_next"), "summary")
        journal = "\n".join(out.get("journal", []))
        self.assertIn(
            "visual review disabled — eyes not engaged for this task",
            journal,
        )
        # The old bare string must NOT appear.
        self.assertNotIn("UX_RENDER_CMD empty", journal)

    def test_has_ui_set_cmd_does_not_skip_ux(self):
        out = self._run_debate_tech(has_ui=True, render_cmd="npx playwright test")
        # UX critic should run → debate_tech does NOT decide the round itself,
        # so debate_next is absent (set later by debate_ux / _debate_decision).
        self.assertNotIn("debate_next", out)
        journal = "\n".join(out.get("journal", []))
        self.assertNotIn(
            "visual review disabled — eyes not engaged for this task",
            journal,
        )

    def test_no_has_ui_with_render_cmd_does_not_skip_ux(self):
        # Per README: any non-empty UX_RENDER_CMD engages eyes regardless of
        # has_ui, so the UX critic runs and debate_tech does NOT decide the
        # round itself.
        out = self._run_debate_tech(has_ui=False, render_cmd="npx playwright test")
        self.assertNotIn("debate_next", out)
        journal = "\n".join(out.get("journal", []))
        self.assertNotIn(
            "visual review disabled — eyes not engaged for this task",
            journal,
        )


class DebateDecisionUxOk(unittest.TestCase):
    def _decide(self, has_ui, render_cmd, ux_verdict="APPROVE", ux_blockers=0):
        prev = C.UX_RENDER_CMD
        try:
            C.UX_RENDER_CMD = render_cmd
            state = {
                "has_ui": has_ui,
                "reviewer_verdict": "APPROVE",
                "open_blockers": 0,
                "ux_verdict": ux_verdict,
                "ux_blockers": ux_blockers,
            }
            return D._debate_decision(state, is_verification=False)
        finally:
            C.UX_RENDER_CMD = prev

    def test_has_ui_empty_cmd_treats_ux_as_satisfied(self):
        # Tech APPROVE + UX gate disabled → converge, even though ux_verdict
        # was never set (UNKNOWN) and ux_blockers default 0.
        out = self._decide(has_ui=True, render_cmd="   ", ux_verdict="UNKNOWN")
        self.assertEqual(out["debate_next"], "summary")
        self.assertNotIn("escalation", out)

    def test_has_ui_set_cmd_requires_ux_approve(self):
        # Render cmd set → UX gate is real; UNKNOWN verdict must NOT converge.
        out = self._decide(has_ui=True, render_cmd="npx playwright test",
                           ux_verdict="UNKNOWN")
        self.assertNotEqual(out["debate_next"], "summary")

    def test_has_ui_set_cmd_converges_on_ux_approve(self):
        out = self._decide(has_ui=True, render_cmd="npx playwright test",
                           ux_verdict="APPROVE", ux_blockers=0)
        self.assertEqual(out["debate_next"], "summary")

    def test_no_has_ui_with_render_cmd_requires_ux_approve(self):
        # Per README: any non-empty UX_RENDER_CMD engages eyes regardless of
        # has_ui, so the UX verdict must be APPROVE to converge. UNKNOWN must
        # NOT converge (the UX critic ran and has not weighed in yet).
        out = self._decide(has_ui=False, render_cmd="npx playwright test",
                           ux_verdict="UNKNOWN")
        self.assertNotEqual(out["debate_next"], "summary")


if __name__ == "__main__":
    unittest.main()
