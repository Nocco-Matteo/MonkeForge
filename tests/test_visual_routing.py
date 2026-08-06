"""The visual gate's routing: render → review → fix loop, and how a resolved
escalation re-enters it. Regression cover for the bug where a render-failure
escalation skipped the visual gate straight to the final check.
"""
import unittest
from unittest.mock import patch

from pipeline_graph import graph as G
from pipeline_graph import nodes as N
from pipeline_graph import config as C


_REVIEW_2 = "VERDICT: REJECT\n[BLOCKER] a is broken\n[BLOCKER] b is broken\n"


def _ensure_png(tid: str):
    """Create a dummy non-empty PNG so ux_visual_review proceeds past the
    zero-PNG skip (TASK-012) to the plateau-detection logic under test."""
    shots = C.SCREENS / f"task-{tid}"
    shots.mkdir(parents=True, exist_ok=True)
    png = shots / "home.png"
    if not png.exists() or png.stat().st_size == 0:
        png.write_bytes(b"\x89PNG\r\n\x1a\n")


def _review(state):
    _ensure_png(state.get("task_id", "p"))
    with patch.object(N, "run_agent", return_value=(0, _REVIEW_2)), \
         patch.object(N, "render_prompt", return_value="x"), \
         patch.object(N.ev, "emit"):
        return N.ux_visual_review(state)


def _batches_built(**kw):
    """State with all batches built (batch_idx == len), a UI task, in the visual phase."""
    st = {"branch": "b", "intake_done": True, "has_ui": True,
          "batches": [{"n": 1}], "batch_idx": 1}
    st.update(kw)
    return st


class RouteVisual(unittest.TestCase):
    def test_clean_goes_to_final(self):
        self.assertEqual(G.route_visual({"visual_blockers": 0}), "final_check")

    def test_blockers_go_to_fix(self):
        self.assertEqual(G.route_visual({"visual_blockers": 2}), "ux_visual_fix")

    def test_escalation_wins(self):
        self.assertEqual(G.route_visual({"escalation": "x", "visual_blockers": 0}), "escalate")


class VisualEscalationReturn(unittest.TestCase):
    def test_render_failure_retries_render(self):
        # Resolved with "ok" (not shipped): re-enter the render, do NOT skip to final.
        st = _batches_built(visual_verdict="", visual_blockers=0,
                            visual_shipped_blocked=False)
        with patch.object(G.C, "UX_RENDER_CMD", "npx playwright test"):
            self.assertEqual(G.route_escalation_return(st), "ux_render")

    def test_disabled_gate_skips_to_final(self):
        # A repo with no frontend (empty UX_RENDER_CMD): resolving a visual
        # escalation must NOT re-enter ux_render (the oscillating loop) — go final.
        st = _batches_built(visual_verdict="", visual_blockers=2,
                            visual_shipped_blocked=False, has_perf=False)
        with patch.object(G.C, "UX_RENDER_CMD", ""):
            self.assertEqual(G.route_escalation_return(st), "final_check")

    def test_shipped_goes_to_final(self):
        # Human accepted/gave up: visual_shipped_blocked set → final check.
        st = _batches_built(visual_shipped_blocked=True)
        self.assertEqual(G.route_escalation_return(st), "final_check")

    def test_passed_goes_to_final(self):
        st = _batches_built(visual_verdict="APPROVE", visual_blockers=0)
        self.assertEqual(G.route_escalation_return(st), "final_check")

    def test_non_ui_task_goes_to_final(self):
        st = _batches_built(has_ui=False)
        self.assertEqual(G.route_escalation_return(st), "final_check")

    def test_ux_render_is_a_declared_return(self):
        self.assertIn("ux_render", G.ESCALATION_RETURNS)


class PlateauDetection(unittest.TestCase):
    def test_no_progress_two_cycles_escalates_early(self):
        # Blockers stuck at 2 for a second non-improving cycle → escalate before
        # the cap, naming the oscillation.
        st = {"task_id": "p", "ux_render_cycle": 1,
              "prev_visual_blockers": 2, "visual_no_progress": 1}
        d = _review(st)
        self.assertTrue(d.get("escalation"))
        self.assertIn("no progress", d["escalation"])
        self.assertEqual(d["visual_no_progress"], 2)

    def test_progress_resets_the_counter(self):
        # Was 4, now 2 → improvement → counter resets, no escalation.
        st = {"task_id": "p", "ux_render_cycle": 1,
              "prev_visual_blockers": 4, "visual_no_progress": 1}
        d = _review(st)
        self.assertFalse(d.get("escalation"))
        self.assertEqual(d["visual_no_progress"], 0)

    def test_first_review_never_plateaus(self):
        # No prior count yet → cannot be "no progress".
        st = {"task_id": "p", "ux_render_cycle": 0}
        d = _review(st)
        self.assertFalse(d.get("escalation"))
        self.assertEqual(d["visual_no_progress"], 0)


if __name__ == "__main__":
    unittest.main()
