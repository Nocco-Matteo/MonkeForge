"""Tests for the adaptive effort feature (TASK-011).

Covers: recommendation rules, resolver fallbacks, router gating, the
checkpoint_effort node, checkpoint_plan suppression, YAML→env loading, and
the --effort CLI flag.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline_graph import config as C
from pipeline_graph import graph as G
from pipeline_graph import nodes as N
from pipeline_graph.nodes import planning as P
from pipeline_graph.nodes import finalize as F


# --- helpers ---------------------------------------------------------------

def _signals(**kw):
    base = {
        "files": [], "file_count": 0, "critical_path_hits": 0,
        "cross_layer": False, "plan_chars": 0,
        "has_ui": False, "has_perf": False,
    }
    base.update(kw)
    return base


# --- recommendation rules --------------------------------------------------

class TestRecommendEffort(unittest.TestCase):
    def test_small_change_recommends_scout(self):
        sig = _signals(file_count=2, plan_chars=500)
        self.assertEqual(P._recommend_effort(sig), "scout-monke")

    def test_critical_path_recommends_barrel(self):
        sig = _signals(file_count=1, plan_chars=100, critical_path_hits=1)
        self.assertEqual(P._recommend_effort(sig), "barrel-monke")

    def test_cross_layer_recommends_barrel(self):
        sig = _signals(file_count=2, plan_chars=100, cross_layer=True)
        self.assertEqual(P._recommend_effort(sig), "barrel-monke")

    def test_perf_recommends_barrel(self):
        sig = _signals(file_count=1, plan_chars=100, has_perf=True)
        self.assertEqual(P._recommend_effort(sig), "barrel-monke")

    def test_ui_recommends_troop(self):
        # UI task: not small enough for scout (has_ui=True), not risky enough for barrel.
        sig = _signals(file_count=2, plan_chars=500, has_ui=True)
        self.assertEqual(P._recommend_effort(sig), "troop-monke")

    def test_large_plan_recommends_troop(self):
        # No critical path, no UI, but plan is long → troop (not scout).
        sig = _signals(file_count=2, plan_chars=5000)
        self.assertEqual(P._recommend_effort(sig), "troop-monke")

    def test_many_files_recommends_troop(self):
        sig = _signals(file_count=5, plan_chars=500)
        self.assertEqual(P._recommend_effort(sig), "troop-monke")

    def test_barrel_wins_over_scout(self):
        # Critical path AND small → barrel (barrel checked first).
        sig = _signals(file_count=1, plan_chars=100, critical_path_hits=1)
        self.assertEqual(P._recommend_effort(sig), "barrel-monke")

    def test_returns_only_known_levels(self):
        for sig in [
            _signals(), _signals(critical_path_hits=5),
            _signals(has_ui=True), _signals(has_perf=True),
            _signals(file_count=10, plan_chars=99999),
        ]:
            self.assertIn(P._recommend_effort(sig), C.EFFORT_LEVELS)


class TestExtractEffortSignals(unittest.TestCase):
    def test_returns_all_required_keys(self):
        sig = P._extract_effort_signals("plan", "brief", True, False)
        for key in ("files", "file_count", "critical_path_hits",
                    "cross_layer", "surface_known", "plan_chars", "has_ui", "has_perf"):
            self.assertIn(key, sig)

    def test_plan_chars_is_len_of_plan_text(self):
        sig = P._extract_effort_signals("hello world", "", False, False)
        self.assertEqual(sig["plan_chars"], 11)

    def test_has_ui_and_has_perf_passthrough(self):
        sig = P._extract_effort_signals("", "", True, True)
        self.assertTrue(sig["has_ui"])
        self.assertTrue(sig["has_perf"])

    def test_detects_modified_config_py_as_critical_path(self):
        plan = "MODIFY: pipeline_graph/config.py -> add a flag."
        sig = P._extract_effort_signals(plan, "", False, False)
        self.assertGreater(sig["critical_path_hits"], 0)

    def test_read_critical_path_is_not_counted(self):
        plan = "READ: pipeline_graph/config.py -> inspect existing settings."
        sig = P._extract_effort_signals(plan, "", False, False)
        self.assertEqual(sig["files"], [])
        self.assertEqual(sig["critical_path_hits"], 0)

    def test_prose_and_negative_mentions_are_not_counted(self):
        plan = (
            "Do not modify `pipeline_graph/config.py`. It is only an anchor.\n"
            "The implementation changes one small helper."
        )
        sig = P._extract_effort_signals(plan, "", False, False)
        self.assertEqual(sig["files"], [])
        self.assertEqual(sig["critical_path_hits"], 0)
        self.assertFalse(sig["surface_known"])

    def test_read_anchor_does_not_inflate_file_count(self):
        plan = (
            "MODIFY: backend/main.py -> add endpoint.\n"
            "READ: frontend/src/App.tsx -> understand the caller.\n"
            "READ: pipeline_graph/config.py -> inspect configuration."
        )
        sig = P._extract_effort_signals(plan, "", False, False)
        self.assertEqual(sig["files"], ["backend/main.py"])
        self.assertEqual(sig["file_count"], 1)
        self.assertFalse(sig["cross_layer"])

    def test_cross_layer_uses_changed_files_only(self):
        plan = (
            "MODIFY: frontend/src/App.tsx -> update the UI.\n"
            "READ: backend/main.py -> inspect the API."
        )
        sig = P._extract_effort_signals(plan, "", False, False)
        self.assertFalse(sig["cross_layer"])

    def test_cross_layer_detected(self):
        plan = (
            "MODIFY: frontend/src/App.tsx -> update the UI.\n"
            "NEW: backend/main.py -> add the API."
        )
        sig = P._extract_effort_signals(plan, "", False, False)
        self.assertTrue(sig["cross_layer"])

    def test_single_layer_not_cross(self):
        plan = (
            "MODIFY: pipeline_graph/config.py -> add a flag.\n"
            "MODIFY: pipeline_graph/state.py -> store it."
        )
        sig = P._extract_effort_signals(plan, "", False, False)
        self.assertFalse(sig["cross_layer"])

    def test_brief_paths_do_not_become_change_surface(self):
        sig = P._extract_effort_signals(
            "MODIFY: small.py -> make the change.",
            "The brief references pipeline_graph/config.py and frontend/App.tsx.",
            False,
            False,
        )
        self.assertEqual(sig["files"], ["small.py"])
        self.assertEqual(sig["critical_path_hits"], 0)
        self.assertFalse(sig["cross_layer"])

    def test_unstructured_plan_defaults_to_troop(self):
        sig = P._extract_effort_signals(
            "Edit `small.py`; config.py is an anchor.", "", False, False
        )
        self.assertFalse(sig["surface_known"])
        self.assertEqual(P._recommend_effort(sig), "troop-monke")


# --- resolver fallbacks ----------------------------------------------------

class TestEffortForFallback(unittest.TestCase):
    def test_no_effort_key_returns_default(self):
        self.assertEqual(C._effort_for({}), C.EFFORT_DEFAULT)

    def test_unknown_effort_returns_default(self):
        self.assertEqual(C._effort_for({"effort": "nonsense"}), C.EFFORT_DEFAULT)

    def test_known_effort_returns_it(self):
        self.assertEqual(C._effort_for({"effort": "scout-monke"}), "scout-monke")
        self.assertEqual(C._effort_for({"effort": "barrel-monke"}), "barrel-monke")

    def test_empty_string_returns_default(self):
        self.assertEqual(C._effort_for({"effort": ""}), C.EFFORT_DEFAULT)


class TestResolvers(unittest.TestCase):
    def test_resolved_debate_rounds_default(self):
        self.assertEqual(C.resolved_debate_rounds({}), C.MAX_DEBATE_ROUNDS)

    def test_resolved_fix_cycles_default(self):
        self.assertEqual(C.resolved_fix_cycles({}), C.MAX_FIX_CYCLES)

    def test_resolved_gate_mode_default(self):
        self.assertEqual(C.resolved_gate_mode({}), "standard")
        self.assertTrue(C.resolved_gates_enabled({}))

    def test_scout_has_off_gates(self):
        self.assertEqual(C.resolved_gate_mode({"effort": "scout-monke"}), "off")
        self.assertFalse(C.resolved_gates_enabled({"effort": "scout-monke"}))

    def test_troop_has_standard_gates(self):
        self.assertEqual(C.resolved_gate_mode({"effort": "troop-monke"}), "standard")
        self.assertTrue(C.resolved_gates_enabled({"effort": "troop-monke"}))

    def test_barrel_has_full_gates(self):
        self.assertEqual(C.resolved_gate_mode({"effort": "barrel-monke"}), "full")
        self.assertTrue(C.resolved_gates_enabled({"effort": "barrel-monke"}))

    def test_troop_byte_equivalent(self):
        # C10: troop-monke's debate_rounds and fix_cycles == the pre-existing constants.
        self.assertEqual(C.EFFORT_LEVELS["troop-monke"]["debate_rounds"], C.MAX_DEBATE_ROUNDS)
        self.assertEqual(C.EFFORT_LEVELS["troop-monke"]["fix_cycles"], C.MAX_FIX_CYCLES)

    def test_unknown_effort_falls_back_to_troop_values(self):
        # An unknown effort → default (troop) → troop's values.
        self.assertEqual(C.resolved_debate_rounds({"effort": "???"}), C.MAX_DEBATE_ROUNDS)

    def test_gate_mode_validation_accepts_new_values(self):
        levels = {
            "scout": {"debate_rounds": 0, "gates": "off", "fix_cycles": 1},
            "troop": {"debate_rounds": 1, "gates": "standard", "fix_cycles": 2},
            "barrel": {"debate_rounds": 3, "gates": "full", "fix_cycles": 3},
        }
        self.assertTrue(C._is_valid_effort_levels(levels))
        self.assertEqual(C._normalize_effort_levels(levels), levels)

    def test_legacy_boolean_gate_modes_are_normalized(self):
        levels = {
            "old-off": {"debate_rounds": 0, "gates": False, "fix_cycles": 1},
            "old-on": {"debate_rounds": 1, "gates": True, "fix_cycles": 2},
        }
        normalized = C._normalize_effort_levels(levels)
        self.assertEqual(normalized["old-off"]["gates"], "off")
        self.assertEqual(normalized["old-on"]["gates"], "standard")

    def test_invalid_gate_mode_is_rejected(self):
        levels = {"custom": {"debate_rounds": 1, "gates": "sometimes", "fix_cycles": 1}}
        self.assertFalse(C._is_valid_effort_levels(levels))


class TestResolverWithMonkeypatchedLevels(unittest.TestCase):
    def test_monkeypatched_levels_take_effect(self):
        fake = {
            "custom": {"debate_rounds": 7, "gates": "off", "fix_cycles": 9},
        }
        with patch.object(C, "EFFORT_LEVELS", fake), \
             patch.object(C, "EFFORT_DEFAULT", "custom"):
            self.assertEqual(C._effort_for({}), "custom")
            self.assertEqual(C.resolved_debate_rounds({}), 7)
            self.assertEqual(C.resolved_fix_cycles({}), 9)
            self.assertFalse(C.resolved_gates_enabled({}))

    def test_monkeypatched_levels_unknown_still_falls_back(self):
        fake = {"custom": {"debate_rounds": 7, "gates": "standard", "fix_cycles": 9}}
        with patch.object(C, "EFFORT_LEVELS", fake), \
             patch.object(C, "EFFORT_DEFAULT", "custom"):
            # An effort not in the patched levels → default.
            self.assertEqual(C._effort_for({"effort": "troop-monke"}), "custom")


# --- route_after_checkpoint_effort -----------------------------------------

class TestRouteAfterCheckpointEffort(unittest.TestCase):
    def test_scout_goes_to_summary(self):
        self.assertEqual(
            G.route_after_checkpoint_effort({"effort": "scout-monke"}), "summary")

    def test_troop_goes_to_debate(self):
        self.assertEqual(
            G.route_after_checkpoint_effort({"effort": "troop-monke"}), "debate_tech")

    def test_barrel_goes_to_debate(self):
        self.assertEqual(
            G.route_after_checkpoint_effort({"effort": "barrel-monke"}), "debate_tech")

    def test_no_effort_key_defaults_to_debate(self):
        # No effort key → default (troop) → debate_tech.
        self.assertEqual(G.route_after_checkpoint_effort({}), "debate_tech")

    def test_escalation_wins(self):
        self.assertEqual(
            G.route_after_checkpoint_effort(
                {"effort": "scout-monke", "escalation": "oops"}),
            "escalate")


# --- route_next_batch gating -----------------------------------------------

class TestRouteNextBatchGating(unittest.TestCase):
    def _batches_done(self, **kw):
        st = {"batch_idx": 1, "batches": [{"n": 1}], "has_ui": False, "has_perf": False}
        st.update(kw)
        return st

    def test_gates_disabled_skips_visual_gate(self):
        # scout-monke: gates disabled → final_check even on a UI task.
        st = self._batches_done(has_ui=True, effort="scout-monke")
        with patch.object(C, "UX_RENDER_CMD", "npx playwright test"):
            self.assertEqual(G.route_next_batch(st), "final_check")

    def test_gates_enabled_ui_task_enters_visual(self):
        st = self._batches_done(has_ui=True, effort="troop-monke")
        with patch.object(C, "UX_RENDER_CMD", "npx playwright test"):
            self.assertEqual(G.route_next_batch(st), "ux_render")

    def test_gates_disabled_skips_render_gate(self):
        st = self._batches_done(has_perf=True, effort="scout-monke")
        self.assertEqual(G.route_next_batch(st), "final_check")

    def test_default_effort_still_runs_gates(self):
        # No effort key → default (troop) → gates enabled.
        st = self._batches_done(has_ui=True)
        with patch.object(C, "UX_RENDER_CMD", "npx playwright test"):
            self.assertEqual(G.route_next_batch(st), "ux_render")


# --- route_escalation_return gating ----------------------------------------

class TestRouteEscalationReturnGating(unittest.TestCase):
    def _batches_built(self, **kw):
        st = {"branch": "b", "intake_done": True, "has_ui": True,
              "batches": [{"n": 1}], "batch_idx": 1}
        st.update(kw)
        return st

    def test_scout_skips_visual_reentry(self):
        # scout-monke: gates disabled → no ux_render re-entry, go to final_check.
        st = self._batches_built(visual_verdict="", visual_blockers=0,
                                 visual_shipped_blocked=False, effort="scout-monke")
        with patch.object(C, "UX_RENDER_CMD", "npx playwright test"):
            self.assertEqual(G.route_escalation_return(st), "final_check")

    def test_troop_reenters_visual(self):
        st = self._batches_built(visual_verdict="", visual_blockers=0,
                                 visual_shipped_blocked=False, effort="troop-monke")
        with patch.object(C, "UX_RENDER_CMD", "npx playwright test"):
            self.assertEqual(G.route_escalation_return(st), "ux_render")

    def test_scout_skips_render_reentry(self):
        st = self._batches_built(has_ui=False, has_perf=True, effort="scout-monke",
                                 render_verdict="", render_blockers=1)
        self.assertEqual(G.route_escalation_return(st), "final_check")

    def test_no_new_return_literal_introduced(self):
        # The function's return literals must still be a subset of ESCALATION_RETURNS.
        import ast, inspect
        tree = ast.parse(inspect.getsource(G.route_escalation_return))
        literals = {n.value.value for n in ast.walk(tree)
                    if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str)}
        self.assertTrue(literals.issubset(G.ESCALATION_RETURNS),
                        f"new return literal(s) {literals - G.ESCALATION_RETURNS} "
                        "not in ESCALATION_RETURNS")

    def test_checkpoint_effort_not_in_escalation_returns(self):
        self.assertNotIn("checkpoint_effort", G.ESCALATION_RETURNS)


# --- checkpoint_effort node ------------------------------------------------

class TestCheckpointEffort(unittest.TestCase):
    def test_already_set_short_circuits(self):
        # effort already a known level → no interrupt, journal-only.
        delta = N.checkpoint_effort({"task_id": "t", "effort": "scout-monke"})
        self.assertNotIn("effort", delta)
        self.assertIn("journal", delta)

    def test_auto_takes_hint_silently(self):
        signals = _signals(file_count=1, plan_chars=100)
        delta = N.checkpoint_effort(
            {"task_id": "t", "auto": True, "effort_hint_signals": signals})
        self.assertEqual(delta["effort"], "scout-monke")
        self.assertNotIn("effort_checkpoint_shown", delta)

    def test_auto_barrel_hint_on_critical_path(self):
        signals = _signals(critical_path_hits=1, plan_chars=100)
        delta = N.checkpoint_effort(
            {"task_id": "t", "auto": True, "effort_hint_signals": signals})
        self.assertEqual(delta["effort"], "barrel-monke")

    def test_interactive_valid_choice(self):
        signals = _signals(file_count=1, plan_chars=100)
        with patch.object(P, "interrupt", return_value="barrel-monke"):
            delta = N.checkpoint_effort(
                {"task_id": "t", "auto": False, "effort_hint_signals": signals})
        self.assertEqual(delta["effort"], "barrel-monke")
        self.assertTrue(delta["effort_checkpoint_shown"])
        self.assertFalse(delta["effort_forced"])

    def test_interactive_ok_takes_hint(self):
        signals = _signals(file_count=1, plan_chars=100)
        with patch.object(P, "interrupt", return_value="ok"):
            delta = N.checkpoint_effort(
                {"task_id": "t", "auto": False, "effort_hint_signals": signals})
        self.assertEqual(delta["effort"], "scout-monke")

    def test_interactive_empty_takes_hint(self):
        signals = _signals(critical_path_hits=1)
        with patch.object(P, "interrupt", return_value=""):
            delta = N.checkpoint_effort(
                {"task_id": "t", "auto": False, "effort_hint_signals": signals})
        self.assertEqual(delta["effort"], "barrel-monke")

    def test_interactive_nonsense_takes_hint(self):
        signals = _signals(file_count=5, plan_chars=100)
        with patch.object(P, "interrupt", return_value="banana"):
            delta = N.checkpoint_effort(
                {"task_id": "t", "auto": False, "effort_hint_signals": signals})
        self.assertEqual(delta["effort"], "troop-monke")

    def test_no_signals_falls_back_to_reextract(self):
        # A pre-feature checkpoint resume: no signals stored → re-extract (empty).
        with patch.object(P, "interrupt", return_value="ok"):
            delta = N.checkpoint_effort({"task_id": "t", "auto": False})
        # An empty re-extraction has unknown surface → conservative troop-monke.
        self.assertEqual(delta["effort"], "troop-monke")

    def test_interactive_payload_has_actionable_effort_choices(self):
        signals = _signals(file_count=1, plan_chars=100)
        with patch.object(P, "interrupt", return_value="ok") as pause:
            N.checkpoint_effort({
                "task_id": "t", "auto": False, "effort_hint_signals": signals,
            })
        payload = pause.call_args.args[0]
        self.assertEqual(payload["stage"], "effort level")
        self.assertIn("recommended: scout-monke", payload["reason"])
        self.assertEqual(set(payload["answers"]), set(C.EFFORT_LEVELS))
        self.assertTrue(all(payload["answers"].values()))


# --- checkpoint_plan suppression -------------------------------------------

class TestCheckpointPlanSuppression(unittest.TestCase):
    def test_auto_skips(self):
        delta = N.checkpoint_plan({"task_id": "t", "auto": True})
        self.assertNotIn("escalation", delta)
        self.assertIn("journal", delta)

    def test_shown_not_forced_skips(self):
        delta = N.checkpoint_plan({
            "task_id": "t", "auto": False,
            "effort_checkpoint_shown": True, "effort_forced": False,
        })
        self.assertNotIn("escalation", delta)
        self.assertIn("journal", delta)

    def test_forced_still_asks(self):
        with patch.object(F, "interrupt", return_value="ok"):
            delta = N.checkpoint_plan({
                "task_id": "t", "auto": False,
                "effort_forced": True, "effort_checkpoint_shown": False,
            })
        self.assertIn("journal", delta)

    def test_not_shown_not_forced_asks(self):
        with patch.object(F, "interrupt", return_value="ok"):
            delta = N.checkpoint_plan({
                "task_id": "t", "auto": False,
                "effort_forced": False, "effort_checkpoint_shown": False,
            })
        self.assertIn("journal", delta)

    def test_forced_and_shown_still_asks(self):
        # Edge case: forced=True AND shown=True → not skipped (forced path asks).
        with patch.object(F, "interrupt", return_value="ok"):
            delta = N.checkpoint_plan({
                "task_id": "t", "auto": False,
                "effort_forced": True, "effort_checkpoint_shown": True,
            })
        self.assertIn("journal", delta)


# --- YAML → PIPELINE_EFFORT_JSON loading -----------------------------------

class TestYamlEffortLoading(unittest.TestCase):
    def test_effort_dict_sets_env_var(self):
        import run
        with tempfile.TemporaryDirectory() as td:
            yaml_path = Path(td) / "monkeforge.yaml"
            yaml_path.write_text(
                "effort:\n  scout-monke:\n    debate_rounds: 0\n    gates: false\n"
                "    fix_cycles: 1\n")
            old = os.environ.pop("PIPELINE_EFFORT_JSON", None)
            try:
                run._load_yaml_to_env(yaml_path)
                raw = os.environ.get("PIPELINE_EFFORT_JSON")
                self.assertIsNotNone(raw)
                parsed = json.loads(raw)
                self.assertIn("scout-monke", parsed)
            finally:
                os.environ.pop("PIPELINE_EFFORT_JSON", None)
                if old is not None:
                    os.environ["PIPELINE_EFFORT_JSON"] = old

    def test_no_effort_key_does_not_set_env_var(self):
        import run
        with tempfile.TemporaryDirectory() as td:
            yaml_path = Path(td) / "monkeforge.yaml"
            yaml_path.write_text("pipeline:\n  dry_run: true\n")
            old = os.environ.pop("PIPELINE_EFFORT_JSON", None)
            try:
                run._load_yaml_to_env(yaml_path)
                self.assertIsNone(os.environ.get("PIPELINE_EFFORT_JSON"))
            finally:
                if old is not None:
                    os.environ["PIPELINE_EFFORT_JSON"] = old

    def test_effort_not_dict_does_not_set_env_var(self):
        import run
        with tempfile.TemporaryDirectory() as td:
            yaml_path = Path(td) / "monkeforge.yaml"
            yaml_path.write_text("effort: scout-monke\n")
            old = os.environ.pop("PIPELINE_EFFORT_JSON", None)
            try:
                run._load_yaml_to_env(yaml_path)
                self.assertIsNone(os.environ.get("PIPELINE_EFFORT_JSON"))
            finally:
                if old is not None:
                    os.environ["PIPELINE_EFFORT_JSON"] = old


# --- --effort argparse wiring ----------------------------------------------

class TestEffortArgparse(unittest.TestCase):
    def _parse(self, *argv):
        import run
        p = run.main.__code__  # just to confirm the module is importable
        # Re-create the parser the same way main() does.
        parser = _build_parser()
        return parser.parse_args(argv)

    def test_start_accepts_effort(self):
        args = self._parse("start", "001", "do something", "--effort", "scout-monke")
        self.assertEqual(args.effort, "scout-monke")

    def test_start_effort_default_none(self):
        args = self._parse("start", "001", "do something")
        self.assertIsNone(args.effort)

    def test_start_rejects_invalid_effort(self):
        import argparse, pytest
        with self.assertRaises(SystemExit):
            self._parse("start", "001", "do something", "--effort", "ninja-monke")

    def test_redo_accepts_effort(self):
        args = self._parse("redo", "001", "--from", "debate", "--effort", "barrel-monke")
        self.assertEqual(args.effort, "barrel-monke")

    def test_redo_effort_default_none(self):
        args = self._parse("redo", "001", "--from", "debate")
        self.assertIsNone(args.effort)

    def test_resume_has_no_effort(self):
        args = self._parse("resume", "001")
        self.assertFalse(hasattr(args, "effort"))


def _build_parser():
    """A standalone replica of run.py's argparse setup (just the subparsers)."""
    import argparse
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start"); s.add_argument("task_id")
    s.add_argument("request", nargs="?", default=None)
    s.add_argument("--file", dest="file", default=None)
    s.add_argument("--auto", action="store_true")
    s.add_argument("--interview", action="store_true")
    s.add_argument("--ref", dest="refs", action="append", default=[])
    s.add_argument("--effort", dest="effort", default=None,
                   choices=["scout-monke", "troop-monke", "barrel-monke"])
    r = sub.add_parser("resume"); r.add_argument("task_id"); r.add_argument("--answer", default="ok")
    rd = sub.add_parser("redo")
    rd.add_argument("task_id")
    rd.add_argument("--from", dest="from_phase",
                    choices=["plan", "debate", "visual"], default="debate")
    rd.add_argument("--effort", dest="effort", default=None,
                    choices=["scout-monke", "troop-monke", "barrel-monke"])
    return p


# --- graph compilation -----------------------------------------------------

class TestGraphCompilesWithEffort(unittest.TestCase):
    def test_build_graph_compiles(self):
        g = G.build_graph()
        self.assertIsNotNone(g)

    def test_checkpoint_effort_is_a_node(self):
        g = G.build_graph()
        self.assertIn("checkpoint_effort", g.nodes)

    def test_plan_continue_targets_checkpoint_effort(self):
        g = G.build_graph()
        edges = g.get_graph().edges
        targets = {e.target for e in edges if e.source == "plan"}
        self.assertIn("checkpoint_effort", targets)

    def test_checkpoint_effort_has_three_targets(self):
        g = G.build_graph()
        edges = g.get_graph().edges
        targets = {e.target for e in edges if e.source == "checkpoint_effort"}
        self.assertEqual(targets, {"summary", "debate_tech", "escalate"})


if __name__ == "__main__":
    unittest.main()
