"""F5: routers fail into escalation instead of crashing the run.

A router is a pure function of state that returns the next node name. If it
raises, langgraph has no edge for the exception and the whole run dies with a
traceback on stdout that nothing pushes to a human. ``_safe_router`` wraps every
router so a crash stashes a plain-language reason (no exception class name —
that stays in the ``step_error`` event and the journal) via ``_set_router_error``
and returns ``"escalate"``, pausing the run for a human. ``escalate()`` picks
the reason up via ``_get_router_error`` and offers a resumable stop path.
"""
import unittest
from unittest.mock import patch

from langgraph.errors import GraphBubbleUp

from pipeline_graph import graph as G
from pipeline_graph.nodes import common as _common


# Every route_* the spec requires to be wrapped, plus the inner router of after()
# (exercised separately via after()()).
_WRAPPED_ROUTES = [
    "route_after_init",
    "route_intake",
    "route_intake_wait",
    "route_after_tech",
    "route_debate",
    "route_after_checkpoint_effort",
    "route_implement",
    "route_code_review",
    "route_code_verify",
    "route_visual",
    "route_render_review",
    "route_next_batch",
    "route_escalation_return",
]


class SafeRouterWrapper(unittest.TestCase):
    def setUp(self):
        _common._router_errors.clear()

    def test_raising_router_returns_escalate(self):
        @G._safe_router
        def boom(state):
            raise RuntimeError("secret internal detail")

        with patch.object(G.ev, "emit"):
            result = boom({"task_id": "rs"})
        self.assertEqual(result, "escalate")

    def test_graphbubbleup_is_reraised(self):
        @G._safe_router
        def boom(state):
            raise GraphBubbleUp("control flow, not an error")

        with patch.object(G.ev, "emit"):
            with self.assertRaises(GraphBubbleUp):
                boom({"task_id": "rs"})

    def test_stored_reason_is_plain_language(self):
        @G._safe_router
        def boom(state):
            raise ValueError("boom")

        with patch.object(G.ev, "emit"):
            boom({"task_id": "rs2"})
        reason = _common._get_router_error("rs2")
        self.assertIn("routing failed", reason)
        # The exception class name must NOT leak into the stored reason — it
        # stays in the step_error event and the journal only.
        self.assertNotIn("ValueError", reason)

    def test_get_router_error_is_non_destructive_peek(self):
        @G._safe_router
        def boom(state):
            raise ValueError("boom")

        with patch.object(G.ev, "emit"):
            boom({"task_id": "rs3"})
        first = _common._get_router_error("rs3")
        second = _common._get_router_error("rs3")
        self.assertEqual(first, second)
        self.assertTrue(first)  # still present after a second read

    def test_step_error_event_carries_exception_type_and_message(self):
        @G._safe_router
        def boom(state):
            raise ValueError("a specific message")

        with patch.object(G.ev, "emit") as mock_emit:
            boom({"task_id": "rs4"})
        step_error_msgs = [c.args[3] for c in mock_emit.call_args_list
                           if c.args and c.args[0] == "step_error"]
        self.assertTrue(any("ValueError" in m for m in step_error_msgs),
                        f"step_error event must carry the exception type; got {step_error_msgs}")
        self.assertTrue(any("a specific message" in m for m in step_error_msgs),
                        f"step_error event must carry the exception message; got {step_error_msgs}")

    def test_every_listed_route_is_wrapped(self):
        for name in _WRAPPED_ROUTES:
            fn = getattr(G, name)
            self.assertTrue(
                hasattr(fn, "__wrapped__"),
                f"{name} is not wrapped by _safe_router (no __wrapped__ attribute)",
            )

    def test_after_inner_router_is_wrapped(self):
        router = G.after()
        self.assertTrue(
            hasattr(router, "__wrapped__"),
            "the inner router returned by after() is not wrapped by _safe_router",
        )

    def test_wrapped_real_router_returns_escalate_on_crash(self):
        # route_next_batch indexes state["batch_idx"] — a state missing it
        # raises KeyError, which _safe_router converts to "escalate".
        with patch.object(G.ev, "emit"), \
                patch.object(_common, "_set_router_error"):
            result = G.route_next_batch({"task_id": "rs5"})  # no batch_idx
        self.assertEqual(result, "escalate")


class EscalateRouterErrorBranch(unittest.TestCase):
    def setUp(self):
        _common._router_errors.clear()

    def _escalate(self, answer, *, state=None, stored_reason=None):
        tid = "rs-esc"
        if stored_reason is not None:
            _common._set_router_error(tid, stored_reason)
        st = state if state is not None else {"task_id": tid, "journal": []}
        with patch.object(_common, "interrupt", return_value=answer), \
                patch.object(_common.ev, "emit"), \
                patch.object(_common.ev, "open_escalation", return_value=True), \
                patch.object(_common.ev, "close_escalation"):
            return _common.escalate(st)

    def test_stop_answer_sets_finished(self):
        d = self._escalate("stop", stored_reason="routing failed in route_x — see journal")
        self.assertTrue(d.get("finished"))

    def test_ok_answer_does_not_set_finished(self):
        d = self._escalate("ok", stored_reason="routing failed in route_x — see journal")
        self.assertFalse(d.get("finished"))

    def test_ok_clears_escalation_only(self):
        d = self._escalate("ok", stored_reason="routing failed in route_x — see journal")
        self.assertEqual(d.get("escalation"), "")
        # No batch-semantics keys are set on a routing failure.
        for banned in ("code_verdict", "open_blockers", "not_met",
                       "degradations", "tests_waived", "baseline_failures",
                       "final_tests_waived", "ux_shipped_blocked"):
            self.assertNotIn(banned, d, f"router_error branch must not set {banned!r}")

    def test_skip_answer_does_not_force_close_batch(self):
        # "skip" would force-close a batch on a normal escalation; for a routing
        # failure it must only clear the escalation (no code_verdict=APPROVE).
        d = self._escalate("skip", stored_reason="routing failed in route_intake — see journal")
        self.assertEqual(d.get("escalation"), "")
        self.assertNotEqual(d.get("code_verdict"), "APPROVE")
        self.assertNotIn("degradations", d)

    def test_reason_never_unknown_and_uses_fallback(self):
        # No escalation in state AND no stored router error → the plain-language
        # fallback, never the literal "unknown".
        captured = {}

        def fake_interrupt(payload):
            captured["reason"] = payload["reason"]
            captured["answers"] = payload["answers"]
            return "stop"

        with patch.object(_common, "interrupt", side_effect=fake_interrupt), \
                patch.object(_common.ev, "emit"), \
                patch.object(_common.ev, "open_escalation", return_value=True), \
                patch.object(_common.ev, "close_escalation"):
            _common.escalate({"task_id": "rs-esc", "journal": []})
        self.assertNotEqual(captured["reason"], "unknown")
        self.assertIn("routing failed", captured["reason"])
        # The fallback reason routes into the "routing failed" options branch.
        self.assertEqual(set(captured["answers"].keys()), {"ok", "stop"})

    def test_routing_failed_options_branch(self):
        opts = _common._escalation_options("routing failed in route_x — see journal")
        self.assertEqual(set(opts.keys()), {"ok", "stop"})

    def test_state_escalation_takes_precedence_over_router_error(self):
        # When a node set state["escalation"], that wins over a stale stored
        # router error — the router_error branch must NOT fire.
        _common._set_router_error("rs-esc", "routing failed in route_x — stale")
        d = self._escalate(
            "skip",
            state={"task_id": "rs-esc", "escalation": "tests still failing after 3 attempts",
                   "journal": [], "batches": [{"n": 1}], "batch_idx": 0},
        )
        # "skip" on a tests-still-failing escalation hits the `forced` branch
        # (force-close the batch) — NOT the router_error branch, which would
        # only clear the escalation and set no batch-semantics keys.
        self.assertEqual(d.get("code_verdict"), "APPROVE")
        self.assertFalse(d.get("finished"))


if __name__ == "__main__":
    unittest.main()
