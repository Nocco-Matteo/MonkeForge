"""DRY_RUN smoke test: build the graph and walk every node with mocked agents.

This verifies the full graph compiles, every node function is callable, and
the routing functions return valid node names for representative states.
No agent subprocess is spawned — run_agent is patched to return a canned
APPROVE verdict.
"""

from unittest.mock import patch

import pytest

from pipeline_graph import config as C
from pipeline_graph import nodes as N
from pipeline_graph.graph import ESCALATION_RETURNS, build_graph


def _dry_state(**overrides) -> dict:
    """Minimal state for DRY_RUN, enough for every node to not KeyError."""
    base = {
        "task_id": "smoke",
        "request": "test request",
        "branch": "test-branch",
        "auto": True,
        "interview": False,
        "intake_done": True,
        "intake_round": 0,
        "brief_path": "",
        "debate_round": 0,
        "batch_idx": 0,
        "fix_cycle": 0,
        "test_fix_attempt": 0,
        "tests_waived": False,
        "baseline_batch_n": 0,
        "batch_test_baseline": [],
        "ux_render_cycle": 0,
        "visual_blockers": 0,
        "visual_shipped_blocked": False,
        "prev_visual_blockers": None,
        "visual_no_progress": 0,
        "render_cycle": 0,
        "render_blockers": 0,
        "prev_render_blockers": None,
        "render_no_progress": 0,
        "escalation": "",
        "degradations": [],
        "journal": [],
        "has_ui": False,
        "has_perf": False,
        "batches": [],
        "tech_limits": [],
        "reviewer_verdict": "",
        "open_blockers": 0,
        "ux_verdict": "",
        "ux_blockers": 0,
        "code_verdict": "",
        "not_met": [],
        "render_verdict": "",
        "render_facts": "{}",
        "visual_verdict": "",
    }
    base.update(overrides)
    return base


_CANNED_OUTPUT = "VERDICT: APPROVE\nNo issues found.\n"


def _patched_run_agent(*args, **kw):
    return 0, _CANNED_OUTPUT


class TestGraphCompiles:
    def test_build_graph_no_errors(self):
        g = build_graph()
        assert g is not None

    def test_build_graph_with_checkpointer(self):
        from langgraph.checkpoint.memory import InMemorySaver

        checkpointer = InMemorySaver()
        g = build_graph(checkpointer=checkpointer)
        assert g is not None


class TestEscalationReturns:
    def test_set_is_non_empty(self):
        assert len(ESCALATION_RETURNS) > 0

    def test_all_are_strings(self):
        for name in ESCALATION_RETURNS:
            assert isinstance(name, str)

    def test_all_correspond_to_registered_nodes(self):
        g = build_graph()
        # The graph's nodes are accessible via .nodes after compile
        node_names = set(g.nodes.keys())
        for name in ESCALATION_RETURNS:
            assert name in node_names, (
                f"ESCALATION_RETURNS contains '{name}' but it is not a registered node"
            )


class TestNodeCallabilityDryRun:
    """Every node function can be called with a DRY_RUN state without crashing."""

    @pytest.fixture(autouse=True)
    def _dry(self):
        with (
            patch.object(C, "DRY_RUN", True),
            patch.object(N, "run_agent", side_effect=_patched_run_agent),
            patch.object(N, "render_prompt", return_value="mock prompt"),
            patch.object(N.ev, "emit"),
        ):
            yield

    def test_init(self):
        delta = N.init(_dry_state())
        assert "branch" in delta

    def test_plan(self):
        # plan reads the brief file; in DRY_RUN the file may not exist,
        # but _seed_brief creates it.
        delta = N.plan(_dry_state())
        assert "has_ui" in delta

    def test_debate_tech(self):
        delta = N.debate_tech(_dry_state(debate_round=0))
        assert "debate_round" in delta

    def test_debate_reply(self):
        delta = N.debate_reply(_dry_state(debate_round=1))
        assert "journal" in delta

    def test_summary(self):
        delta = N.summary(_dry_state())
        assert "journal" in delta

    def test_judge(self):
        # judge needs BATCHES json; patch _extract_json in the finalize module
        # where it's actually imported, not on the re-export N._extract_json.
        from pipeline_graph.nodes import finalize as _fin

        with patch.object(_fin, "_extract_json", return_value=[{"n": 1, "scope": "test"}]):
            delta = N.judge(_dry_state())
        assert "batches" in delta

    def test_close_batch(self):
        state = _dry_state(
            batches=[
                {
                    "n": 1,
                    "scope": "test",
                    "status": "PENDING",
                    "outcome": "",
                    "deviations": "",
                    "checklist": [],
                }
            ],
            batch_idx=0,
        )
        delta = N.close_batch(state)
        assert delta["batch_idx"] == 1

    def test_wrap_up(self):
        delta = N.wrap_up(_dry_state(batches=[{"n": 1}]))
        assert delta.get("finished") is True


class TestRoutingFunctions:
    """Routing functions return valid node names for representative states."""

    from pipeline_graph import graph as G

    def test_route_after_init_clean(self):
        state = _dry_state(intake_done=True)
        assert self.G.route_after_init(state) == "plan"

    def test_route_after_init_escalation(self):
        state = _dry_state(escalation="oops")
        assert self.G.route_after_init(state) == "escalate"

    def test_route_implement_clean(self):
        state = _dry_state(test_fix_attempt=0)
        assert self.G.route_implement(state) == "code_review"

    def test_route_implement_retry(self):
        state = _dry_state(test_fix_attempt=1)
        assert self.G.route_implement(state) == "implement"

    def test_route_code_review_clean(self):
        state = _dry_state(not_met=[], open_blockers=0)
        assert self.G.route_code_review(state) == "close_batch"

    def test_route_code_review_blockers(self):
        state = _dry_state(not_met=["1"], open_blockers=2)
        assert self.G.route_code_review(state) == "code_fix"

    def test_route_next_batch_more_batches(self):
        state = _dry_state(
            batches=[{"n": 1}, {"n": 2}],
            batch_idx=0,
        )
        assert self.G.route_next_batch(state) == "implement"

    def test_route_next_batch_done_no_ui(self):
        state = _dry_state(batches=[{"n": 1}], batch_idx=1, has_ui=False)
        assert self.G.route_next_batch(state) == "final_check"

    def test_route_next_batch_done_with_ui(self):
        state = _dry_state(batches=[{"n": 1}], batch_idx=1, has_ui=True)
        # Visual gate enabled (non-empty render cmd) → a UI task enters it.
        with patch.object(C, "UX_RENDER_CMD", "npx playwright test"):
            assert self.G.route_next_batch(state) == "ux_render"

    def test_route_next_batch_ui_but_visual_gate_disabled(self):
        # A repo with no frontend (empty UX_RENDER_CMD) disables the whole visual
        # phase: has_ui defaulting True must NOT drag it into ux_render.
        state = _dry_state(batches=[{"n": 1}], batch_idx=1, has_ui=True, has_perf=False)
        with patch.object(C, "UX_RENDER_CMD", ""):
            assert self.G.route_next_batch(state) == "final_check"

    def test_route_visual_clean(self):
        state = _dry_state(visual_blockers=0, has_perf=False)
        assert self.G.route_visual(state) == "final_check"

    def test_route_visual_blockers(self):
        state = _dry_state(visual_blockers=3)
        assert self.G.route_visual(state) == "ux_visual_fix"

    def test_route_escalation_return_finished(self):
        from langgraph.graph import END

        state = _dry_state(finished=True)
        assert self.G.route_escalation_return(state) == END

    def test_route_escalation_return_no_branch(self):
        state = _dry_state(branch=None)
        assert self.G.route_escalation_return(state) == "init"
