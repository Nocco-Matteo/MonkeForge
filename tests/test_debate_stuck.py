"""Tests for stuck-claim detection and the ``debate stuck:`` escalation (TASK-017 batch 1).

Covers conformance checklist items 1-18:
  - TestStuckClaims: stuck_claims() — k<1, BLOCKER-only, last-section-per-critic,
    verdict filter, intersection across k rounds.
  - TestDebateStuckRoundsConfig: DEBATE_STUCK_ROUNDS parse/clamp behaviour.
  - TestCheckEarlyEscalation: _check_early_escalation() — function-local import,
    escalation prefix, debate_next=summary.
  - TestDebateStuckMenu: _escalation_options "debate stuck:" branch keys.
  - TestEscalateDebateStuckGate: escalate() prefix gate forces substring flags
    False, gates final_test/crash, and the redo handler resets the bonus.
  - TestDebateNodesImportNoCycle: condenser↔nodes.debate import cycle guard.
"""
from __future__ import annotations

import importlib
import sys
import unittest
from unittest.mock import patch

from pipeline_graph import config as C
from pipeline_graph import condenser
from pipeline_graph.nodes import common as _common
from pipeline_graph.nodes import debate as D


# --- helpers ----------------------------------------------------------------

_REJECT_R1 = "VERDICT: REJECT\n[BLOCKER] the plan is wrong\n"
_REJECT_R2 = "VERDICT: REJECT\n[BLOCKER] the plan is wrong\n"
_APPROVE_R2 = "VERDICT: APPROVE\n"


def _round(rnd: int, critic: str, body: str) -> str:
    return f"## Round {rnd} — {critic}\n\n{body}\n"


def _debate(*sections: str) -> str:
    return "preamble\n\n" + "".join(sections)


# --- stuck_claims (items 1-4) ------------------------------------------------


class TestStuckClaims(unittest.TestCase):
    def test_k_below_1_returns_empty(self):
        # Item 1: k < 1 → [].
        text = _debate(_round(1, "Reviewer", _REJECT_R1), _round(2, "Reviewer", _REJECT_R2))
        self.assertEqual(condenser.stuck_claims(text, 0), [])
        self.assertEqual(condenser.stuck_claims(text, -1), [])

    def test_fewer_rounds_than_k_returns_empty(self):
        text = _debate(_round(1, "Reviewer", _REJECT_R1))
        self.assertEqual(condenser.stuck_claims(text, 2), [])

    def test_repeated_blocker_across_k_rounds_is_stuck(self):
        text = _debate(
            _round(1, "Reviewer", _REJECT_R1),
            _round(2, "Reviewer", _REJECT_R2),
        )
        stuck = condenser.stuck_claims(text, 2)
        self.assertEqual(stuck, ["the plan is wrong"])

    def test_suggestion_only_not_stuck(self):
        # Item 2: [SUGGESTION] lines are excluded from consideration.
        text = _debate(
            _round(1, "Reviewer", "VERDICT: REJECT\n[SUGGESTION] minor thing\n"),
            _round(2, "Reviewer", "VERDICT: REJECT\n[SUGGESTION] minor thing\n"),
        )
        self.assertEqual(condenser.stuck_claims(text, 2), [])

    def test_blocker_in_one_round_only_not_stuck(self):
        text = _debate(
            _round(1, "Reviewer", _REJECT_R1),
            _round(2, "Reviewer", "VERDICT: REJECT\n[BLOCKER] a different issue\n"),
        )
        self.assertEqual(condenser.stuck_claims(text, 2), [])

    def test_last_section_per_critic_within_round(self):
        # Item 3: when a critic header repeats within a round, only the LAST
        # section is scanned.
        text = _debate(
            _round(1, "Reviewer", _REJECT_R1),
            _round(2, "Reviewer", "VERDICT: REJECT\n[BLOCKER] first pass\n"),
            _round(2, "Reviewer", _REJECT_R2),  # supersedes the first R2 pass
        )
        stuck = condenser.stuck_claims(text, 2)
        # The last R2 section has "the plan is wrong", matching R1.
        self.assertEqual(stuck, ["the plan is wrong"])

    def test_approve_section_excluded_from_scan(self):
        # Item 4: APPROVE sections are excluded — even if they contain a
        # [BLOCKER] token (which references a resolved/prior item).
        text = _debate(
            _round(1, "Reviewer", _REJECT_R1),
            _round(2, "Reviewer", "VERDICT: APPROVE\n[BLOCKER] the plan is wrong\n"),
        )
        self.assertEqual(condenser.stuck_claims(text, 2), [])

    def test_approve_with_changes_section_excluded(self):
        text = _debate(
            _round(1, "Reviewer", _REJECT_R1),
            _round(2, "Reviewer",
                   "VERDICT: APPROVE_WITH_CHANGES\n[BLOCKER] the plan is wrong\n"),
        )
        self.assertEqual(condenser.stuck_claims(text, 2), [])

    def test_multiple_stuck_claims(self):
        text = _debate(
            _round(1, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha\n[BLOCKER] beta\n"),
            _round(2, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha\n[BLOCKER] beta\n"),
        )
        self.assertEqual(condenser.stuck_claims(text, 2), ["alpha", "beta"])

    def test_partial_overlap_not_stuck(self):
        # alpha in both, beta only in R1 → only alpha is stuck.
        text = _debate(
            _round(1, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha\n[BLOCKER] beta\n"),
            _round(2, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha\n"),
        )
        self.assertEqual(condenser.stuck_claims(text, 2), ["alpha"])

    def test_empty_text_returns_empty(self):
        self.assertEqual(condenser.stuck_claims("", 2), [])
        self.assertEqual(condenser.stuck_claims(None, 2), [])

    def test_k3_across_three_rounds(self):
        text = _debate(
            _round(1, "Reviewer", _REJECT_R1),
            _round(2, "Reviewer", _REJECT_R2),
            _round(3, "Reviewer", _REJECT_R1),
        )
        self.assertEqual(condenser.stuck_claims(text, 3), ["the plan is wrong"])

    def test_k3_missing_from_one_round_not_stuck(self):
        text = _debate(
            _round(1, "Reviewer", _REJECT_R1),
            _round(2, "Reviewer", "VERDICT: REJECT\n[BLOCKER] other\n"),
            _round(3, "Reviewer", _REJECT_R2),
        )
        self.assertEqual(condenser.stuck_claims(text, 3), [])

    def test_stuck_with_id_uses_critic_qualified_token(self):
        """D2: when ids are present, stuck_claims uses `f"{critic}{id}"` tokens
        so Reviewer B1 and UX B1 are distinct."""
        text = _debate(
            _round(1, "Reviewer", "VERDICT: REJECT\n[BLOCKER] B1: foo\n"),
            _round(2, "Reviewer", "VERDICT: REJECT\n[BLOCKER] B1: foo\n"),
        )
        stuck = condenser.stuck_claims(text, 2)
        self.assertEqual(stuck, ["ReviewerB1"])

    def test_stuck_same_id_different_critics_not_stuck(self):
        """Reviewer B1 and UX B1 are distinct tokens — not stuck across critics."""
        text = _debate(
            _round(1, "Reviewer", "VERDICT: REJECT\n[BLOCKER] B1: foo\n"),
            _round(2, "UX", "VERDICT: REJECT\n[BLOCKER] B1: bar\n"),
        )
        stuck = condenser.stuck_claims(text, 2)
        self.assertEqual(stuck, [])


# --- DEBATE_STUCK_ROUNDS config (items 5-7) ----------------------------------


class TestDebateStuckRoundsConfig(unittest.TestCase):
    """The config clamps are import-time — test via module reload with env."""

    @staticmethod
    def _with_env(env: dict[str, str | None]):
        """Context manager: set env vars, reload C, restore env + C on exit."""
        import os
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            saved = {k: os.environ.get(k) for k in env}
            try:
                for k, v in env.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
                importlib.reload(C)
                yield
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
                importlib.reload(C)

        return _cm()

    def test_default_is_2(self):
        # Without the env var, the default is 2.
        with self._with_env({"PIPELINE_DEBATE_STUCK_ROUNDS": None}):
            self.assertEqual(C.DEBATE_STUCK_ROUNDS, 2)

    def test_non_integer_falls_back_to_default(self):
        # Item 5: try/except fallback on parse failure.
        with self._with_env({"PIPELINE_DEBATE_STUCK_ROUNDS": "not-a-number"}):
            self.assertEqual(C.DEBATE_STUCK_ROUNDS, 2)

    def test_below_1_clamps_to_1(self):
        # Item 6: clamps to minimum 1 with a stderr warning.
        with self._with_env({"PIPELINE_DEBATE_STUCK_ROUNDS": "0"}):
            self.assertEqual(C.DEBATE_STUCK_ROUNDS, 1)

    def test_negative_clamps_to_1(self):
        with self._with_env({"PIPELINE_DEBATE_STUCK_ROUNDS": "-3"}):
            self.assertEqual(C.DEBATE_STUCK_ROUNDS, 1)

    def test_clamps_to_condenser_keep_recent_when_smaller(self):
        # Item 7: when keep_recent < stuck, clamp down (yaml keep_recent, not env).
        self.assertEqual(C._clamp_stuck_to_keep_recent(5, 3), 3)

    def test_condenser_keep_recent_0_forces_stuck_to_0(self):
        # Item 7: keep_recent == 0 → stuck window 0 (guard disabled).
        self.assertEqual(C._clamp_stuck_to_keep_recent(2, 0), 0)

    def test_within_bounds_unchanged(self):
        self.assertEqual(C._clamp_stuck_to_keep_recent(3, 5), 3)


# --- _check_early_escalation (items 8-12) ------------------------------------


class TestCheckEarlyEscalation(unittest.TestCase):
    def test_no_stuck_returns_none(self):
        text = _debate(_round(1, "Reviewer", _REJECT_R1))
        self.assertIsNone(D._check_early_escalation(text, 1))

    def test_stuck_returns_escalation_with_prefix(self):
        # Item 12: escalation starts with "debate stuck:" and debate_next=summary.
        text = _debate(
            _round(1, "Reviewer", _REJECT_R1),
            _round(2, "Reviewer", _REJECT_R2),
        )
        result = D._check_early_escalation(text, 2)
        self.assertIsNotNone(result)
        self.assertTrue(result["escalation"].startswith("debate stuck:"))
        self.assertEqual(result["debate_next"], "summary")

    def test_function_local_import_only(self):
        # Item 8/9: no module-top-level import of condenser.stuck_claims.
        import inspect
        source = inspect.getsource(D)
        # The only "from ..condenser import" must be inside a function body.
        lines = source.split("\n")
        found_top_level = False
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith("from ..condenser import") and not line.startswith("    "):
                found_top_level = True
        self.assertFalse(found_top_level,
                         "nodes/debate.py must not import condenser at module top level")

    def test_escalation_includes_round_number(self):
        text = _debate(
            _round(1, "Reviewer", _REJECT_R1),
            _round(2, "Reviewer", _REJECT_R2),
        )
        result = D._check_early_escalation(text, 2)
        self.assertIn("round 2", result["escalation"])


# --- _escalation_options "debate stuck:" branch (items 13-14) ----------------


class TestDebateStuckMenu(unittest.TestCase):
    def test_debate_stuck_prefix_returns_correct_keys(self):
        # Item 14: exactly continue, redo, stop, ok (no skip).
        opts = _common._escalation_options("debate stuck: 1 blocker(s) repeated across 2 rounds")
        self.assertEqual({o["key"] for o in opts}, {"continue", "redo", "stop", "ok"})

    def test_debate_stuck_tested_before_intake_branch(self):
        # Item 13: the "debate stuck:" prefix is tested before the intake branch.
        # A reason that starts with "debate stuck:" but also contains "intake"
        # must still route to the debate-stuck menu, not the intake menu.
        opts = _common._escalation_options(
            "debate stuck: intake-related blocker repeated across 2 rounds"
        )
        keys = {o["key"] for o in opts}
        self.assertIn("continue", keys)
        self.assertNotIn("skip / done", keys)

    def test_case_insensitive_prefix(self):
        opts = _common._escalation_options("Debate Stuck: something")
        self.assertEqual({o["key"] for o in opts}, {"continue", "redo", "stop", "ok"})


# --- escalate() prefix gate (items 15-17) ------------------------------------


class TestEscalateDebateStuckGate(unittest.TestCase):
    """The prefix gate in escalate() forces substring flags False and gates
    final_test/crash, so a 'debate stuck:' escalation only fires the
    continue/redo/stop/ok menu — not a partial substring collision."""

    def _escalate(self, answer, reason, state=None):
        st = state or {"task_id": "ds", "escalation": reason, "journal": []}
        with patch.object(_common, "interrupt", return_value=answer), \
             patch.object(_common.ev, "emit"), \
             patch.object(_common.ev, "open_escalation", return_value=True), \
             patch.object(_common.ev, "close_escalation"):
            return _common.escalate(st)

    def test_ok_proceeds_to_verdict(self):
        # "ok" → generic else branch, escalation cleared, no forced keys.
        d = self._escalate("ok", "debate stuck: 1 blocker(s) repeated")
        self.assertEqual(d.get("escalation"), "")
        self.assertEqual(d.get("debate_round_bonus"), 0)
        self.assertEqual(d.get("redo_debate"), False)
        self.assertNotIn("ux_shipped_blocked", d)
        self.assertNotIn("degradations", d)

    def test_ok_after_prior_continue_clears_bonus(self):
        # Leftover bonus from an earlier "continue" must not survive "ok",
        # or route_escalation_return re-enters debate instead of summary.
        state = {
            "task_id": "ds",
            "escalation": "debate exhausted 5 rounds + verification: 1 technical blocker(s)",
            "journal": [],
            "debate_round_bonus": 2,
        }
        d = self._escalate(
            "ok",
            "debate exhausted 5 rounds + verification: 1 technical blocker(s)",
            state=state,
        )
        self.assertEqual(d.get("debate_round_bonus"), 0)
        self.assertEqual(d.get("redo_debate"), False)
        self.assertIn("proceeding to the verdict", d["journal"][0])

    def test_continue_extends_debate(self):
        d = self._escalate("continue", "debate stuck: 1 blocker(s) repeated")
        self.assertEqual(d.get("escalation"), "")
        self.assertIn("debate_round_bonus", d)
        self.assertEqual(d["debate_round_bonus"], 2)

    def test_stop_stops_run(self):
        d = self._escalate("stop", "debate stuck: 1 blocker(s) repeated")
        self.assertTrue(d.get("finished"))

    def test_redo_resets_debate(self):
        d = self._escalate("redo", "debate stuck: 1 blocker(s) repeated")
        self.assertTrue(d.get("redo_debate"))
        self.assertEqual(d.get("debate_round"), 0)

    def test_redo_resets_bonus_to_zero(self):
        # Item 18: the redo handler includes "debate_round_bonus": 0.
        d = self._escalate("redo", "debate stuck: 1 blocker(s) repeated")
        self.assertIn("debate_round_bonus", d)
        self.assertEqual(d["debate_round_bonus"], 0)

    def test_redo_after_continue_resets_bonus(self):
        # Simulate a prior "continue" that set a bonus, then a "redo" from a
        # debate-stuck escalation. The redo must reset the bonus to 0.
        state = {
            "task_id": "ds",
            "escalation": "debate stuck: 1 blocker(s) repeated",
            "journal": [],
            "debate_round_bonus": 4,
        }
        d = self._escalate("redo", "debate stuck: 1 blocker(s) repeated", state=state)
        self.assertEqual(d["debate_round_bonus"], 0)

    def test_debate_stuck_does_not_fire_ux_branch(self):
        # The reason contains "blocker" but the prefix gate forces ux_escalation
        # False, so "ok" must NOT set ux_shipped_blocked.
        d = self._escalate("ok", "debate stuck: UX blocker repeated across rounds")
        self.assertNotIn("ux_shipped_blocked", d)

    def test_debate_stuck_does_not_fire_render_branch(self):
        # A reason containing "render" substrings but starting with "debate
        # stuck:" must not fire the render/visual branch.
        d = self._escalate("ok", "debate stuck: cannot render the UI blocker")
        self.assertNotIn("visual_shipped_blocked", d)

    def test_debate_stuck_does_not_fire_final_test_branch(self):
        # Item 17: final_test_escalation is gated. A reason containing "final
        # test gate" but starting with "debate stuck:" must not waive tests.
        d = self._escalate("ok", "debate stuck: final test gate blocker repeated")
        self.assertNotIn("final_tests_waived", d)

    def test_debate_stuck_does_not_fire_crash_branch(self):
        # Item 17: crash_escalation is gated. A reason containing "crashed" but
        # starting with "debate stuck:" must not fire the crash retry branch.
        d = self._escalate("ok", "debate stuck: step crashed in debate_tech")
        # The crash branch would set code_verdict=None; the generic else does not.
        self.assertNotIn("code_verdict", d)

    def test_debate_stuck_does_not_fire_intake_branch(self):
        # The reason contains "intake" but the prefix gate forces
        # intake_escalation False, so "ok" must NOT set intake_done.
        d = self._escalate("ok", "debate stuck: intake blocker repeated")
        self.assertNotIn("intake_done", d)


# --- import cycle guard (items 8-9) ------------------------------------------


class TestDebateNodesImportNoCycle(unittest.TestCase):
    """condenser imports TECH_LIMIT_RE from nodes.debate at module top level,
    and nodes.debate imports stuck_claims from condenser function-locally.
    Both import orders must succeed without an ImportError."""

    def test_condenser_first_then_debate(self):
        # Purge both from sys.modules and import condenser first.
        for mod in list(sys.modules):
            if mod.startswith("pipeline_graph.condenser") or \
               mod.startswith("pipeline_graph.nodes.debate"):
                del sys.modules[mod]
        import pipeline_graph.condenser as _c  # noqa: F401
        import pipeline_graph.nodes.debate as _d  # noqa: F401
        self.assertTrue(hasattr(_c, "stuck_claims"))
        self.assertTrue(hasattr(_d, "_check_early_escalation"))

    def test_debate_first_then_condenser(self):
        for mod in list(sys.modules):
            if mod.startswith("pipeline_graph.condenser") or \
               mod.startswith("pipeline_graph.nodes.debate"):
                del sys.modules[mod]
        import pipeline_graph.nodes.debate as _d  # noqa: F401
        import pipeline_graph.condenser as _c  # noqa: F401
        self.assertTrue(hasattr(_c, "stuck_claims"))
        self.assertTrue(hasattr(_d, "_check_early_escalation"))


# --- helper functions (items 12-13) ----------------------------------------


class TestSectionHasBlockersWithoutIds(unittest.TestCase):
    """Item 12: _section_has_blockers_without_ids — True when any [BLOCKER]
    raise line lacks an id, False when all carry ids or no blockers."""

    def test_blocker_without_id_returns_true(self):
        body = "VERDICT: REJECT\n[BLOCKER] foo\n"
        self.assertTrue(condenser._section_has_blockers_without_ids(body))

    def test_blocker_with_id_returns_false(self):
        body = "VERDICT: REJECT\n[BLOCKER] B1: foo\n"
        self.assertFalse(condenser._section_has_blockers_without_ids(body))

    def test_mixed_id_and_no_id_returns_true(self):
        body = "VERDICT: REJECT\n[BLOCKER] B1: foo\n[BLOCKER] bar\n"
        self.assertTrue(condenser._section_has_blockers_without_ids(body))

    def test_all_with_ids_returns_false(self):
        body = "VERDICT: REJECT\n[BLOCKER] B1: foo\n[BLOCKER] B2: bar\n"
        self.assertFalse(condenser._section_has_blockers_without_ids(body))

    def test_suggestion_only_returns_false(self):
        body = "VERDICT: APPROVE_WITH_CHANGES\n[SUGGESTION] foo\n"
        self.assertFalse(condenser._section_has_blockers_without_ids(body))

    def test_empty_returns_false(self):
        self.assertFalse(condenser._section_has_blockers_without_ids(""))
        self.assertFalse(condenser._section_has_blockers_without_ids(None))


class TestRaisesMissingIds(unittest.TestCase):
    """Item 13: _raises_missing_ids — list of claim texts for raise lines
    without ids."""

    def test_blocker_without_id_listed(self):
        body = "VERDICT: REJECT\n[BLOCKER] foo\n"
        self.assertEqual(condenser._raises_missing_ids(body), ["foo"])

    def test_blocker_with_id_not_listed(self):
        body = "VERDICT: REJECT\n[BLOCKER] B1: foo\n"
        self.assertEqual(condenser._raises_missing_ids(body), [])

    def test_mixed_returns_only_missing(self):
        body = "VERDICT: REJECT\n[BLOCKER] B1: foo\n[BLOCKER] bar\n[SUGGESTION] S1: baz\n[SUGGESTION] qux\n"
        result = condenser._raises_missing_ids(body)
        self.assertIn("bar", result)
        self.assertIn("qux", result)
        self.assertNotIn("foo", result)
        self.assertNotIn("baz", result)

    def test_empty_returns_empty(self):
        self.assertEqual(condenser._raises_missing_ids(""), [])
        self.assertEqual(condenser._raises_missing_ids(None), [])


if __name__ == "__main__":
    unittest.main()
