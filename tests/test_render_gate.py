"""Render gate — the perf analog of the visual gate. Deterministic: it counts
re-renders per subtree on a scripted interaction and BLOCKS any subtree that
re-renders more than baseline (a regression). No LLM in the review.
"""
import json
import shutil
import unittest
from unittest.mock import patch

from pipeline_graph import nodes as N, config as C, graph as G

TID = "rgtest"


def _write_facts(renders, instrumented=True, interaction="click_x"):
    d = N._renders_dir(TID)
    d.mkdir(parents=True, exist_ok=True)
    (d / "render-facts.json").write_text(json.dumps({
        "interaction": interaction, "instrumented": instrumented,
        "renders": renders, "total_commits": sum(renders.values())}))


def _write_baseline(renders):
    C.RENDERS.mkdir(parents=True, exist_ok=True)
    (C.RENDERS / f"baseline-{TID}.json").write_text(json.dumps({"renders": renders}))


def _cleanup():
    (C.RENDERS / f"baseline-{TID}.json").unlink(missing_ok=True)
    d = N._renders_dir(TID)
    if d.exists():
        shutil.rmtree(d)


class RenderReview(unittest.TestCase):
    def setUp(self):
        _cleanup()

    def tearDown(self):
        _cleanup()

    def test_not_instrumented_degrades_not_blocks(self):
        _write_facts({}, instrumented=False)
        out = N.render_review({"task_id": TID})
        self.assertEqual(out["render_verdict"], "SKIPPED")
        self.assertEqual(out["render_blockers"], 0)
        self.assertTrue(out.get("degradations"))

    def test_first_run_establishes_baseline(self):
        _write_facts({"ActionCockpit": 3})
        out = N.render_review({"task_id": TID})
        self.assertEqual(out["render_verdict"], "BASELINE")
        self.assertEqual(out["render_blockers"], 0)
        self.assertTrue((C.RENDERS / f"baseline-{TID}.json").exists())

    def test_regression_blocks(self):
        _write_baseline({"ActionCockpit": 1})
        _write_facts({"ActionCockpit": 3})
        out = N.render_review({"task_id": TID})
        self.assertEqual(out["render_verdict"], "REGRESSED")
        self.assertEqual(out["render_blockers"], 1)
        delta = (N._renders_dir(TID) / "render-delta.md").read_text()
        self.assertIn("REGRESSION  ActionCockpit: 1 -> 3", delta)

    def test_new_rerendering_subtree_is_a_regression(self):
        _write_baseline({"ActionCockpit": 1})
        _write_facts({"ActionCockpit": 1, "IdentityColumn": 2})
        out = N.render_review({"task_id": TID})
        self.assertEqual(out["render_blockers"], 1)  # IdentityColumn: 0 -> 2

    def test_improvement_passes(self):
        _write_baseline({"ActionCockpit": 5})
        _write_facts({"ActionCockpit": 2})
        out = N.render_review({"task_id": TID})
        self.assertEqual(out["render_verdict"], "PASS")
        self.assertEqual(out["render_blockers"], 0)
        self.assertNotIn("escalation", out)

    def test_plateau_escalates(self):
        _write_baseline({"A": 1, "B": 1})
        _write_facts({"A": 3, "B": 3})  # 2 regressions
        out = N.render_review({"task_id": TID, "prev_render_blockers": 2,
                               "render_no_progress": 1})
        self.assertIn("escalation", out)


class RenderRouting(unittest.TestCase):
    def test_render_passed_states(self):
        for v in ("PASS", "BASELINE", "SKIPPED"):
            self.assertTrue(G._render_passed({"render_verdict": v, "render_blockers": 0}))
        self.assertFalse(G._render_passed({"render_verdict": "REGRESSED", "render_blockers": 1}))
        self.assertFalse(G._render_passed({"render_verdict": "PASS", "render_blockers": 2}))

    def test_route_render_review(self):
        self.assertEqual(G.route_render_review({"render_blockers": 2}), "render_fix")
        self.assertEqual(G.route_render_review({"render_blockers": 0}), "final_check")
        self.assertEqual(G.route_render_review({"escalation": "x"}), "escalate")

    def test_after_ui_gate_routes_to_render_when_perf(self):
        with patch.object(G.C, "RENDER_CMD", "npx playwright test"):
            self.assertEqual(G._after_ui_gate({"has_perf": True, "render_verdict": ""}),
                             "render_measure")
            self.assertEqual(G._after_ui_gate({"has_perf": True, "render_verdict": "PASS"}),
                             "final_check")
        self.assertEqual(G._after_ui_gate({"has_perf": False}), "final_check")

    def test_after_ui_gate_skips_render_when_cmd_empty(self):
        # RENDER_CMD empty (default opt-off) → a perf task goes straight to final.
        with patch.object(G.C, "RENDER_CMD", ""):
            self.assertEqual(
                G._after_ui_gate({"has_perf": True, "render_verdict": ""}),
                "final_check")

    def test_non_perf_task_skips_render_gate(self):
        # A backend/UI task without has_perf goes straight to final_check.
        state = {"batch_idx": 1, "batches": [{"n": 1}], "has_ui": False, "has_perf": False}
        self.assertEqual(G.route_next_batch(state), "final_check")

    def test_escalation_return_reprofiles_when_perf_not_passed(self):
        state = {"branch": "b", "intake_done": True, "batches": [{"n": 1}],
                 "batch_idx": 1, "has_ui": False, "has_perf": True,
                 "render_verdict": "", "render_blockers": 0}
        with patch.object(G.C, "RENDER_CMD", "npx playwright test"):
            self.assertEqual(G.route_escalation_return(state), "render_measure")

    def test_escalation_return_skips_render_when_cmd_empty(self):
        # RENDER_CMD empty (default opt-off) → a perf task escalation resolves
        # to final_check, not render_measure.
        state = {"branch": "b", "intake_done": True, "batches": [{"n": 1}],
                 "batch_idx": 1, "has_ui": False, "has_perf": True,
                 "render_verdict": "", "render_blockers": 0}
        with patch.object(G.C, "RENDER_CMD", ""):
            self.assertEqual(G.route_escalation_return(state), "final_check")

    def test_render_measure_empty_cmd_returns_skipped(self):
        # render_measure with RENDER_CMD="" returns a SKIPPED verdict, not
        # journal-only — so render_review sees a passed state.
        with patch.object(G.C, "RENDER_CMD", ""), \
             patch.object(G.C, "DRY_RUN", False):
            out = N.render_measure({"task_id": TID})
        self.assertEqual(out["render_verdict"], "SKIPPED")
        self.assertEqual(out["render_blockers"], 0)

    def test_render_measure_in_escalation_returns(self):
        self.assertIn("render_measure", G.ESCALATION_RETURNS)


if __name__ == "__main__":
    unittest.main()
