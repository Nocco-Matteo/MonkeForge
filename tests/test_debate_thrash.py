"""Tests for the deterministic debate-thrashing detection (TASK-022 batch 1).

Covers the conformance checklist items 2-26:
  - TestThrashingReport: condenser.thrashing_report() — active-round filter,
    blocker_counts (per-critic independent sum), repeated/new, the C15
    ``len(active_window) < 2 → mode="unknown"`` override, and the classification
    order (stuck → latest==0 → thrashing → strictly-decreasing → unknown).
  - TestCheckThrashingEscalation: debate._check_thrashing_escalation() —
    function-local import, None when mode != thrashing, escalation prefix,
    debate_next=summary, triage key.
  - TestEscalatePayload: escalate() reads state triage/hint, includes both in
    the interrupt payload, passes triage into _escalation_options.
  - TestEscalationOptionsTriage: _escalation_options "debate thrashing:" branch
    keys; the "debate exhausted" label rewrite fires ONLY when
    triage["mode"] == "thrashing".
  - TestResolutionWipe: the base delta in escalate() clears triage/hint
    unconditionally; the redo branch does not re-add them.
  - TestDebateNodeWiring: debate_tech/debate_ux precedence chain
    (requirements > stuck > thrashing > _debate_decision) and debate_text in
    delta.
  - TestBotEmbedTriage: bot/bot.py adds triage embed fields inside an
    ``if rec.get("triage")`` guard.
  - TestRunPausedEmitLegacy: run.py's run_paused emit omits the triage key
    entirely (not null) when there is no triage — legacy records stay
    byte-identical.
  - TestExhaustedPathTriageWiring (Delta B): _debate_decision's exhausted
    branch attaches a triage derived from the actual debate-text fixture, not
    a value pre-set on the input state.
"""
from __future__ import annotations

import importlib
import os
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from pipeline_graph import config as C
from pipeline_graph import condenser
from pipeline_graph.nodes import common as _common
from pipeline_graph.nodes import debate as D


# --- helpers ----------------------------------------------------------------

_REJECT = "VERDICT: REJECT\n[BLOCKER] the plan is wrong\n"
_APPROVE = "VERDICT: APPROVE\n"


def _round(rnd: int, critic: str, body: str) -> str:
    return f"## Round {rnd} — {critic}\n\n{body}\n"


def _debate(*sections: str) -> str:
    return "preamble\n\n" + "".join(sections)


# --- TestThrashingReport (items 2-6) ----------------------------------------


class TestThrashingReport(unittest.TestCase):
    def test_empty_text_returns_unknown(self):
        r = condenser.thrashing_report("", 2)
        self.assertEqual(r["mode"], "unknown")
        self.assertEqual(r["blocker_counts"], [])
        self.assertEqual(r["repeated"], [])
        self.assertEqual(r["new"], [])

    def test_none_text_returns_unknown(self):
        r = condenser.thrashing_report(None, 2)
        self.assertEqual(r["mode"], "unknown")

    def test_k_below_1_returns_unknown(self):
        text = _debate(_round(1, "Reviewer", _REJECT),
                       _round(2, "Reviewer", _REJECT))
        r = condenser.thrashing_report(text, 0)
        self.assertEqual(r["mode"], "unknown")

    def test_no_rounds_returns_unknown(self):
        r = condenser.thrashing_report("preamble only\n", 2)
        self.assertEqual(r["mode"], "unknown")

    def test_single_active_round_is_unknown_regardless_of_k(self):
        # C15: len(active_window) < 2 → mode="unknown" regardless of k.
        text = _debate(_round(1, "Reviewer", _REJECT))
        r = condenser.thrashing_report(text, 1)
        self.assertEqual(r["mode"], "unknown")
        # blocker_counts is still populated from the available active round.
        self.assertEqual(r["blocker_counts"], [1])

    def test_approve_only_rounds_are_not_active(self):
        # A round where every critic APPROVES is not active — excluded from
        # the trend.
        text = _debate(_round(1, "Reviewer", _REJECT),
                       _round(2, "Reviewer", _APPROVE))
        r = condenser.thrashing_report(text, 2)
        # Only round 1 is active → len(active_window) == 1 → unknown.
        self.assertEqual(r["mode"], "unknown")
        self.assertEqual(r["blocker_counts"], [1])

    def test_thrashing_not_decreasing_with_new_claim(self):
        # 3 → 2 → 2 with a new claim and NO claim repeated across all 3 rounds
        # (so stuck_claims is empty and thrashing fires). Claims rotate so the
        # intersection across all 3 rounds is empty.
        text = _debate(
            _round(1, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha\n[BLOCKER] beta\n[BLOCKER] gamma\n"),
            _round(2, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha\n[BLOCKER] beta\n"),
            _round(3, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] gamma\n[BLOCKER] delta\n"),
        )
        r = condenser.thrashing_report(text, 3)
        self.assertEqual(r["mode"], "thrashing")
        self.assertEqual(r["blocker_counts"], [3, 2, 2])
        self.assertIn("delta", r["new"])

    def test_strictly_decreasing_is_converging(self):
        # 3 → 2 → 1 strictly decreasing, no claim repeated across the last
        # k=2 rounds (the default DEBATE_THRASH_ROUNDS), so stuck_claims is
        # empty and converging fires.
        text = _debate(
            _round(1, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha\n[BLOCKER] beta\n[BLOCKER] gamma\n"),
            _round(2, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] delta\n[BLOCKER] beta\n"),
            _round(3, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] epsilon\n"),
        )
        r = condenser.thrashing_report(text, 3)
        self.assertEqual(r["mode"], "converging")
        self.assertEqual(r["blocker_counts"], [3, 2, 1])

    def test_latest_zero_is_converging(self):
        text = _debate(
            _round(1, "Reviewer", _REJECT),
            _round(2, "Reviewer", _APPROVE),
        )
        # Round 2 is APPROVE → not active. Only round 1 active → unknown.
        # Use a case where the latest active round has 0 blockers but is REJECT.
        text2 = _debate(
            _round(1, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha\n"),
            _round(2, "Reviewer",
                   "VERDICT: REJECT\n[SUGGESTION] minor\n"),
        )
        r = condenser.thrashing_report(text2, 2)
        self.assertEqual(r["mode"], "converging")
        self.assertEqual(r["blocker_counts"], [1, 0])

    def test_stuck_takes_precedence_over_thrashing(self):
        # Same claim repeated across all rounds → stuck (checked first).
        text = _debate(
            _round(1, "Reviewer", _REJECT),
            _round(2, "Reviewer", _REJECT),
        )
        r = condenser.thrashing_report(text, 2)
        self.assertEqual(r["mode"], "stuck")

    def test_blocker_counts_sums_per_critic_independently(self):
        # A claim raised by both Reviewer and UX counts twice.
        text = _debate(
            _round(1, "Reviewer", "VERDICT: REJECT\n[BLOCKER] alpha\n"),
            _round(1, "UX", "VERDICT: REJECT\n[BLOCKER] alpha\n"),
            _round(2, "Reviewer", "VERDICT: REJECT\n[BLOCKER] alpha\n"),
            _round(2, "UX", "VERDICT: REJECT\n[BLOCKER] alpha\n"),
        )
        r = condenser.thrashing_report(text, 2)
        # Each round: Reviewer(1) + UX(1) = 2.
        self.assertEqual(r["blocker_counts"], [2, 2])

    def test_repeated_deduped_across_critics(self):
        # A claim raised by both Reviewer and UX in the same round counts
        # once in the per-round claim set (for repeated/new).
        text = _debate(
            _round(1, "Reviewer", "VERDICT: REJECT\n[BLOCKER] alpha\n"),
            _round(1, "UX", "VERDICT: REJECT\n[BLOCKER] alpha\n"),
            _round(2, "Reviewer", "VERDICT: REJECT\n[BLOCKER] alpha\n"),
            _round(2, "UX", "VERDICT: REJECT\n[BLOCKER] alpha\n"),
        )
        r = condenser.thrashing_report(text, 2)
        self.assertEqual(r["repeated"], ["alpha"])

    def test_new_is_latest_minus_previous(self):
        text = _debate(
            _round(1, "Reviewer", "VERDICT: REJECT\n[BLOCKER] alpha\n"),
            _round(2, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha\n[BLOCKER] beta\n"),
        )
        r = condenser.thrashing_report(text, 2)
        self.assertEqual(r["new"], ["beta"])

    def test_not_decreasing_no_new_is_unknown(self):
        # 2 → 2 with no new claim → not thrashing (no new), not strictly
        # decreasing → unknown.
        text = _debate(
            _round(1, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha\n[BLOCKER] beta\n"),
            _round(2, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha\n[BLOCKER] beta\n"),
        )
        r = condenser.thrashing_report(text, 2)
        # stuck_claims non-empty (alpha, beta repeated) → stuck takes precedence.
        self.assertEqual(r["mode"], "stuck")

    def test_window_takes_last_k_active_rounds(self):
        # 4 active rounds, k=2 → only the last 2 are in the window. Claims in
        # the last 2 rounds share no common claim so stuck_claims is empty.
        text = _debate(
            _round(1, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha\n[BLOCKER] beta\n[BLOCKER] gamma\n"),
            _round(2, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha\n[BLOCKER] beta\n"),
            _round(3, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] gamma\n[BLOCKER] beta\n[BLOCKER] delta\n"),
            _round(4, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] zeta\n[BLOCKER] epsilon\n"),
        )
        r = condenser.thrashing_report(text, 2)
        # Window = rounds 3,4 → blocker_counts [3, 2], strictly decreasing.
        self.assertEqual(r["blocker_counts"], [3, 2])
        self.assertEqual(r["mode"], "converging")


# --- TestCheckThrashingEscalation (item 8) ----------------------------------


class TestCheckThrashingEscalation(unittest.TestCase):
    def test_grace_window_covers_promised_rounds(self):
        self.assertTrue(D._in_debate_grace({"debate_grace_until": 6}, 5))
        self.assertTrue(D._in_debate_grace({"debate_grace_until": 6}, 6))
        self.assertFalse(D._in_debate_grace({"debate_grace_until": 6}, 7))
        self.assertFalse(D._in_debate_grace({}, 4))
        self.assertFalse(D._in_debate_grace({"debate_grace_until": 0}, 1))

    def test_non_thrashing_returns_none(self):
        text = _debate(_round(1, "Reviewer", _REJECT))
        self.assertIsNone(D._check_thrashing_escalation(text, 1))

    def test_thrashing_returns_escalation_with_prefix(self):
        # Claims rotate so no claim is in all 3 rounds (stuck_claims empty).
        text = _debate(
            _round(1, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha\n[BLOCKER] beta\n[BLOCKER] gamma\n"),
            _round(2, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha\n[BLOCKER] beta\n"),
            _round(3, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] gamma\n[BLOCKER] delta\n"),
        )
        result = D._check_thrashing_escalation(text, 3)
        self.assertIsNotNone(result)
        self.assertTrue(result["escalation"].startswith("debate thrashing:"))
        self.assertEqual(result["debate_next"], "summary")
        self.assertIn("triage", result)
        self.assertIsInstance(result["triage"], dict)
        self.assertIn("hint", result)

    def test_function_local_import(self):
        import inspect
        source = inspect.getsource(D)
        lines = source.split("\n")
        found_top_level = False
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith("from ..condenser import") and not line.startswith("    "):
                found_top_level = True
        self.assertFalse(found_top_level,
                         "nodes/debate.py must not import condenser at module top level")

    def test_escalation_includes_round_number(self):
        # Claims rotate so no claim is in all 3 rounds (stuck_claims empty).
        text = _debate(
            _round(1, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha\n[BLOCKER] beta\n[BLOCKER] gamma\n"),
            _round(2, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha\n[BLOCKER] beta\n"),
            _round(3, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] gamma\n[BLOCKER] delta\n"),
        )
        result = D._check_thrashing_escalation(text, 3)
        self.assertIn("round 3", result["escalation"])

    def test_triage_has_six_keys(self):
        # Claims rotate so no claim is in all 3 rounds (stuck_claims empty).
        text = _debate(
            _round(1, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha\n[BLOCKER] beta\n[BLOCKER] gamma\n"),
            _round(2, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha\n[BLOCKER] beta\n"),
            _round(3, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] gamma\n[BLOCKER] delta\n"),
        )
        result = D._check_thrashing_escalation(text, 3)
        triage = result["triage"]
        for key in ("mode", "blocker_counts", "repeated", "new",
                    "recommended", "rationale"):
            self.assertIn(key, triage)
        self.assertEqual(triage["mode"], "thrashing")
        self.assertEqual(triage["recommended"], "ok")


# --- TestEscalatePayload (item 15) ------------------------------------------


class TestEscalatePayload(unittest.TestCase):
    def _escalate(self, answer, reason, state=None):
        st = state or {"task_id": "ep", "escalation": reason, "journal": []}
        with patch.object(_common, "interrupt", return_value=answer), \
             patch.object(_common.ev, "emit"), \
             patch.object(_common.ev, "open_escalation", return_value=True), \
             patch.object(_common.ev, "close_escalation"):
            return _common.escalate(st)

    def test_triage_and_hint_included_in_interrupt_payload(self):
        triage = {"mode": "thrashing", "blocker_counts": [3, 2, 2],
                  "repeated": ["alpha"], "new": ["delta"],
                  "recommended": "ok", "rationale": "churning"}
        state = {"task_id": "ep", "escalation": "debate thrashing: x",
                 "journal": [], "triage": triage, "hint": "ok"}
        captured = {}

        def _capture(payload):
            captured.update(payload)
            return "ok"

        with patch.object(_common, "interrupt", side_effect=_capture), \
             patch.object(_common.ev, "emit"), \
             patch.object(_common.ev, "open_escalation", return_value=True), \
             patch.object(_common.ev, "close_escalation"):
            _common.escalate(state)
        self.assertIn("triage", captured)
        self.assertEqual(captured["triage"], triage)
        self.assertIn("hint", captured)
        self.assertEqual(captured["hint"], "ok")

    def test_triage_passed_into_escalation_options(self):
        triage = {"mode": "thrashing", "blocker_counts": [3, 2, 2],
                  "repeated": [], "new": ["delta"],
                  "recommended": "ok", "rationale": "churning"}
        state = {"task_id": "ep", "escalation": "debate exhausted 5 rounds",
                 "journal": [], "triage": triage, "hint": "ok"}
        captured = {}

        orig = _common._escalation_options

        def _spy(reason, *, triage=None, state=None):
            captured["triage"] = triage
            return orig(reason, triage=triage, state=state)

        with patch.object(_common, "_escalation_options", side_effect=_spy), \
             patch.object(_common, "interrupt", return_value="ok"), \
             patch.object(_common.ev, "emit"), \
             patch.object(_common.ev, "open_escalation", return_value=True), \
             patch.object(_common.ev, "close_escalation"):
            _common.escalate(state)
        self.assertEqual(captured["triage"], triage)

    def test_no_triage_in_state_passes_none_to_options(self):
        state = {"task_id": "ep", "escalation": "debate exhausted 5 rounds",
                 "journal": []}
        captured = {}

        orig = _common._escalation_options

        def _spy(reason, *, triage=None, state=None):
            captured["triage"] = triage
            return orig(reason, triage=triage, state=state)

        with patch.object(_common, "_escalation_options", side_effect=_spy), \
             patch.object(_common, "interrupt", return_value="ok"), \
             patch.object(_common.ev, "emit"), \
             patch.object(_common.ev, "open_escalation", return_value=True), \
             patch.object(_common.ev, "close_escalation"):
            _common.escalate(state)
        self.assertIsNone(captured["triage"])


# --- TestEscalationOptionsTriage (items 13-14) ------------------------------


class TestEscalationOptionsTriage(unittest.TestCase):
    def test_debate_thrashing_prefix_returns_continue_redo_stop_ok(self):
        opts = _common._escalation_options(
            "debate thrashing: blockers not decreasing")
        self.assertEqual({o["key"] for o in opts},
                         {"continue", "redo", "stop", "ok"})

    def test_debate_thrashing_labels_warn_about_churning(self):
        opts = _common._escalation_options(
            "debate thrashing: blockers not decreasing")
        labels = " ".join(o["label"] for o in opts)
        self.assertIn("churning", labels.lower())
        continue_label = next(o["label"] for o in opts if o["key"] == "continue")
        self.assertIn("suppressed", continue_label.lower())

    def test_exhausted_default_labels_without_triage(self):
        opts = _common._escalation_options("debate exhausted 5 rounds")
        ok_label = next(o["label"] for o in opts if o["key"] == "ok")
        continue_label = next(o["label"] for o in opts if o["key"] == "continue")
        self.assertNotIn("churning", ok_label.lower())
        self.assertNotIn("churning", continue_label.lower())

    def test_exhausted_stuck_mode_keeps_default_labels(self):
        # A mode="stuck" exhausted escalation must keep the default labels.
        triage = {"mode": "stuck"}
        opts = _common._escalation_options("debate exhausted 5 rounds",
                                           triage=triage)
        ok_label = next(o["label"] for o in opts if o["key"] == "ok")
        self.assertNotIn("churning", ok_label.lower())

    def test_exhausted_thrashing_mode_rewrites_labels(self):
        triage = {"mode": "thrashing"}
        opts = _common._escalation_options("debate exhausted 5 rounds",
                                           triage=triage)
        ok_label = next(o["label"] for o in opts if o["key"] == "ok")
        continue_label = next(o["label"] for o in opts if o["key"] == "continue")
        self.assertIn("churning", ok_label.lower())
        self.assertIn("churning", continue_label.lower())

    def test_debate_thrashing_prefix_gates_substring_flags(self):
        # A reason starting with "debate thrashing:" must not fire the ux,
        # render, final-test, crash, or intake branches.
        d = self._escalate("ok", "debate thrashing: UX blocker cannot render final test gate crashed in intake")
        self.assertNotIn("ux_shipped_blocked", d)
        self.assertNotIn("visual_shipped_blocked", d)
        self.assertNotIn("final_tests_waived", d)
        self.assertNotIn("code_verdict", d)
        self.assertNotIn("intake_done", d)

    def _escalate(self, answer, reason, state=None):
        st = state or {"task_id": "eo", "escalation": reason, "journal": []}
        with patch.object(_common, "interrupt", return_value=answer), \
             patch.object(_common.ev, "emit"), \
             patch.object(_common.ev, "open_escalation", return_value=True), \
             patch.object(_common.ev, "close_escalation"):
            return _common.escalate(st)


# --- TestResolutionWipe (item 17 / C14) -------------------------------------


class TestResolutionWipe(unittest.TestCase):
    def _escalate(self, answer, reason, state=None):
        st = state or {"task_id": "rw", "escalation": reason, "journal": []}
        with patch.object(_common, "interrupt", return_value=answer), \
             patch.object(_common.ev, "emit"), \
             patch.object(_common.ev, "open_escalation", return_value=True), \
             patch.object(_common.ev, "close_escalation"):
            return _common.escalate(st)

    def test_ok_clears_triage_and_hint(self):
        state = {"task_id": "rw", "escalation": "debate thrashing: x",
                 "journal": [], "triage": {"mode": "thrashing"}, "hint": "ok"}
        d = self._escalate("ok", "debate thrashing: x", state=state)
        self.assertIn("triage", d)
        self.assertIsNone(d["triage"])
        self.assertIn("hint", d)
        self.assertEqual(d["hint"], "")

    def test_continue_clears_triage_and_hint(self):
        state = {"task_id": "rw", "escalation": "debate thrashing: x",
                 "journal": [], "triage": {"mode": "thrashing"}, "hint": "ok"}
        d = self._escalate("continue", "debate thrashing: x", state=state)
        self.assertIsNone(d["triage"])
        self.assertEqual(d["hint"], "")

    def test_continue_sets_grace_until_current_round_plus_two(self):
        """continue must suppress stuck/thrashing for the promised +2 rounds."""
        state = {
            "task_id": "rw",
            "escalation": "debate thrashing: x",
            "journal": [],
            "triage": {"mode": "thrashing"},
            "hint": "ok",
            "debate_round": 4,
            "debate_round_bonus": 2,
        }
        d = self._escalate("continue", "debate thrashing: x", state=state)
        self.assertEqual(d.get("debate_grace_until"), 6)
        self.assertEqual(d.get("debate_round_bonus"), 4)

    def test_ok_clears_grace_until(self):
        state = {
            "task_id": "rw",
            "escalation": "debate thrashing: x",
            "journal": [],
            "triage": {"mode": "thrashing"},
            "hint": "ok",
            "debate_grace_until": 6,
        }
        d = self._escalate("ok", "debate thrashing: x", state=state)
        self.assertEqual(d.get("debate_grace_until"), 0)

    def test_redo_clears_triage_and_hint(self):
        state = {"task_id": "rw", "escalation": "debate thrashing: x",
                 "journal": [], "triage": {"mode": "thrashing"}, "hint": "ok"}
        d = self._escalate("redo", "debate thrashing: x", state=state)
        self.assertIsNone(d["triage"])
        self.assertEqual(d["hint"], "")

    def test_stop_clears_triage_and_hint(self):
        state = {"task_id": "rw", "escalation": "debate thrashing: x",
                 "journal": [], "triage": {"mode": "thrashing"}, "hint": "ok"}
        d = self._escalate("stop", "debate thrashing: x", state=state)
        self.assertIsNone(d["triage"])
        self.assertEqual(d["hint"], "")

    def test_non_debate_escalation_clears_triage_and_hint(self):
        # A non-debate escalation must also wipe a stale triage/hint.
        state = {"task_id": "rw", "escalation": "tests still failing",
                 "journal": [], "triage": {"mode": "thrashing"}, "hint": "ok"}
        d = self._escalate("ok", "tests still failing", state=state)
        self.assertIsNone(d["triage"])
        self.assertEqual(d["hint"], "")


# --- TestDebateNodeWiring (items 10-12) -------------------------------------


class TestDebateNodeWiring(unittest.TestCase):
    def test_build_triage_thrashing_recommends_ok(self):
        # TASK-024 item 31: split into three cases — low-Jaccard/bonus-0 → ok;
        # high-Jaccard/bonus-0 → continue; high-Jaccard/bonus>0 → ok. Each
        # passes debate_round_bonus= explicitly to _build_triage.
        # Low-Jaccard fixture: new claims (gamma, delta) share no theme tokens
        # with the prior round's claims (alpha, beta) → fresh surface → ok.
        low_jaccard = _debate(
            _round(1, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha\n[BLOCKER] beta\n[BLOCKER] gamma\n"),
            _round(2, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha\n[BLOCKER] beta\n"),
            _round(3, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] gamma\n[BLOCKER] delta\n"),
        )
        triage = D._build_triage(low_jaccard, "debate thrashing",
                                 debate_round_bonus=0)
        self.assertEqual(triage["mode"], "thrashing")
        self.assertEqual(triage["recommended"], "ok")

        # High-Jaccard fixture: new claims refine the prior round's themes
        # (alpha theme variant shares tokens with alpha theme one) → continue.
        high_jaccard = _debate(
            _round(1, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha theme one\n"
                   "[BLOCKER] beta theme two\n[BLOCKER] gamma theme three\n"),
            _round(2, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha theme one\n"
                   "[BLOCKER] beta theme two\n"),
            _round(3, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha theme variant\n"
                   "[BLOCKER] gamma theme three\n"),
        )
        triage_refine = D._build_triage(high_jaccard, "debate thrashing",
                                        debate_round_bonus=0)
        self.assertEqual(triage_refine["mode"], "thrashing")
        self.assertEqual(triage_refine["recommended"], "continue")

        # Same high-Jaccard fixture but bonus > 0 → ok (human already extended).
        triage_bonus = D._build_triage(high_jaccard, "debate thrashing",
                                       debate_round_bonus=2)
        self.assertEqual(triage_bonus["mode"], "thrashing")
        self.assertEqual(triage_bonus["recommended"], "ok")

    def test_build_triage_stuck_recommends_ok(self):
        text = _debate(
            _round(1, "Reviewer", _REJECT),
            _round(2, "Reviewer", _REJECT),
        )
        triage = D._build_triage(text, "debate stuck")
        self.assertEqual(triage["mode"], "stuck")
        self.assertEqual(triage["recommended"], "ok")

    def test_build_triage_requirements_forces_mode_and_reintake_below_max(self):
        # TASK-033: below MAX the requirements triage recommends re-intake
        # (the in-graph recovery), not stop. At MAX it recommends ok.
        text = _debate(
            _round(1, "Reviewer", _REJECT),
            _round(2, "Reviewer", _REJECT),
        )
        triage = D._build_triage(text, "debate requirements",
                                 reintake_count=0, max_reintakes=2)
        self.assertEqual(triage["mode"], "requirements")
        self.assertEqual(triage["recommended"], "re-intake")
        # At MAX the recommendation flips to ok.
        triage_max = D._build_triage(text, "debate requirements",
                                     reintake_count=2, max_reintakes=2)
        self.assertEqual(triage_max["recommended"], "ok")
        # Legacy callers (max_reintakes=None) keep the below-MAX recommendation.
        triage_legacy = D._build_triage(text, "debate requirements")
        self.assertEqual(triage_legacy["recommended"], "re-intake")

    def test_build_triage_exhausted_converging_recommends_continue(self):
        # 3 → 2 → 1 strictly decreasing, no claim repeated across the last
        # k=2 rounds (the default DEBATE_THRASH_ROUNDS), so stuck_claims is
        # empty and converging fires.
        text = _debate(
            _round(1, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha\n[BLOCKER] beta\n[BLOCKER] gamma\n"),
            _round(2, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] delta\n[BLOCKER] beta\n"),
            _round(3, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] epsilon\n"),
        )
        triage = D._build_triage(text, "debate exhausted")
        self.assertEqual(triage["mode"], "converging")
        self.assertEqual(triage["recommended"], "continue")

    def test_build_triage_exhausted_thrashing_recommends_ok(self):
        # TASK-024 item 32: the exhausted thrashing branch applies the same
        # refinement policy as the early thrashing branch. Low-Jaccard/bonus-0
        # → ok (fresh surface); refine+bonus-0 → continue; refine+bonus>0 → ok.
        # Low-Jaccard fixture: new claims (gamma, delta) share no theme tokens
        # with the prior round's claims (alpha, beta) → fresh surface → ok.
        low_jaccard = _debate(
            _round(1, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha\n[BLOCKER] beta\n[BLOCKER] gamma\n"),
            _round(2, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha\n[BLOCKER] beta\n"),
            _round(3, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] gamma\n[BLOCKER] delta\n"),
        )
        triage = D._build_triage(low_jaccard, "debate exhausted",
                                 debate_round_bonus=0)
        self.assertEqual(triage["mode"], "thrashing")
        self.assertEqual(triage["recommended"], "ok")

        # High-Jaccard fixture: new claims refine the prior round's themes.
        high_jaccard = _debate(
            _round(1, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha theme one\n"
                   "[BLOCKER] beta theme two\n[BLOCKER] gamma theme three\n"),
            _round(2, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha theme one\n"
                   "[BLOCKER] beta theme two\n"),
            _round(3, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha theme variant\n"
                   "[BLOCKER] gamma theme three\n"),
        )
        triage_refine = D._build_triage(high_jaccard, "debate exhausted",
                                        debate_round_bonus=0)
        self.assertEqual(triage_refine["mode"], "thrashing")
        self.assertEqual(triage_refine["recommended"], "continue")

        triage_bonus = D._build_triage(high_jaccard, "debate exhausted",
                                       debate_round_bonus=2)
        self.assertEqual(triage_bonus["mode"], "thrashing")
        self.assertEqual(triage_bonus["recommended"], "ok")

    def test_debate_tech_delta_includes_debate_text(self):
        # debate_tech sets "debate_text" in its delta (item 11). We verify
        # the source contains the key rather than running the full node
        # (which would invoke a real agent).
        import inspect
        source = inspect.getsource(D.debate_tech)
        self.assertIn('"debate_text": text', source)

    def test_debate_ux_delta_includes_debate_text(self):
        import inspect
        source = inspect.getsource(D.debate_ux)
        self.assertIn('"debate_text": debate_text', source)

    def test_debate_tech_precedence_chain_includes_thrashing(self):
        import inspect
        source = inspect.getsource(D.debate_tech)
        # The precedence chain: requirements > early(stuck) > thrashing > decision
        self.assertIn("_check_thrashing_escalation", source)
        self.assertIn("thrash", source)

    def test_debate_ux_precedence_chain_includes_thrashing(self):
        import inspect
        source = inspect.getsource(D.debate_ux)
        self.assertIn("_check_thrashing_escalation", source)
        self.assertIn("thrash", source)

    def test_check_early_escalation_sets_triage_and_hint(self):
        text = _debate(
            _round(1, "Reviewer", _REJECT),
            _round(2, "Reviewer", _REJECT),
        )
        result = D._check_early_escalation(text, 2)
        self.assertIn("triage", result)
        self.assertIn("hint", result)
        self.assertEqual(result["hint"], result["triage"]["recommended"])

    def test_check_requirements_escalation_sets_triage_and_hint(self):
        text = _debate(
            _round(1, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER:REQUIREMENTS] brief is wrong\n"),
        )
        # TASK-033: below MAX the hint is re-intake (in-graph recovery).
        result = D._check_requirements_escalation(text)
        self.assertIsNotNone(result)
        self.assertIn("triage", result)
        self.assertIn("hint", result)
        self.assertEqual(result["hint"], "re-intake")
        # At MAX the hint flips to ok.
        result_max = D._check_requirements_escalation(
            text, reintake_count=2, max_reintakes=2)
        self.assertEqual(result_max["hint"], "ok")

    # --- TASK-024 items 39-41: caller-wiring regression tests ---------------

    def test_debate_tech_threads_debate_round_bonus(self):
        # Item 39: debate_tech passes debate_round_bonus=state.get(...) into
        # its _check_thrashing_escalation call. Verified via inspect.getsource
        # so a caller cannot bypass the kwarg while staying green.
        import inspect
        source = inspect.getsource(D.debate_tech)
        self.assertIn("_check_thrashing_escalation", source)
        self.assertIn("debate_round_bonus=state.get(\"debate_round_bonus\")", source)

    def test_debate_ux_threads_debate_round_bonus(self):
        # Item 40: debate_ux passes debate_round_bonus=state.get(...) into
        # its _check_thrashing_escalation call.
        import inspect
        source = inspect.getsource(D.debate_ux)
        self.assertIn("_check_thrashing_escalation", source)
        self.assertIn("debate_round_bonus=state.get(\"debate_round_bonus\")", source)

    def test_debate_decision_threads_debate_round_bonus(self):
        # Item 41: _debate_decision's exhausted branch passes
        # debate_round_bonus=state.get(...) into its _build_triage call.
        import inspect
        source = inspect.getsource(D._debate_decision)
        self.assertIn("_build_triage", source)
        self.assertIn("debate_round_bonus=state.get(\"debate_round_bonus\")", source)


# --- TestBotEmbedTriage (item 21) -------------------------------------------


class TestBotEmbedTriage(unittest.TestCase):
    """discord.py is not installed in the test env, so we read the source
    directly to verify the triage embed fields are present inside an
    ``if rec.get("triage")`` guard."""

    def test_bot_py_has_triage_guard_and_fields(self):
        bot_path = Path(__file__).resolve().parent.parent / "bot" / "bot.py"
        src = bot_path.read_text()
        # The guard.
        self.assertIn("rec.get(\"triage\")", src)
        # The embed fields.
        self.assertIn("triage mode", src)
        self.assertIn("blocker trend", src)
        self.assertIn("repeated/new", src)
        self.assertIn("rationale", src)
        # The "no active rounds" wording for an empty blocker_counts.
        self.assertIn("no active rounds", src)


# --- TestRunPausedEmitLegacy (item 20 / C8) ---------------------------------


class TestRunPausedEmitLegacy(unittest.TestCase):
    """The run_paused emit must omit the triage key entirely (not null) when
    there is no triage, so legacy records stay byte-identical."""

    def test_emit_uses_conditional_spread_for_triage(self):
        run_path = Path(__file__).resolve().parent.parent / "run.py"
        src = run_path.read_text()
        # The conditional spread keyed on isinstance(data.get("triage"), dict).
        self.assertIn("isinstance(data.get(\"triage\"), dict)", src)
        self.assertIn("**({\"triage\": data[\"triage\"]}", src)

    def test_print_pause_renders_triage_block(self):
        run_path = Path(__file__).resolve().parent.parent / "run.py"
        src = run_path.read_text()
        self.assertIn("triage:", src)
        self.assertIn("no active rounds", src)
        self.assertIn("repeated/new:", src)


# --- TestExhaustedPathTriageWiring (Delta B / item 24) ----------------------


class TestExhaustedPathTriageWiring(unittest.TestCase):
    """Delta B: _debate_decision's exhausted branch attaches a triage derived
    from the ACTUAL debate-text fixture, not a value pre-set on the input
    state. This exercises the real _build_triage(debate_text, "debate
    exhausted") construction call inside _debate_decision's exhausted
    branch."""

    def test_exhausted_triage_derived_from_debate_text_not_state(self):
        # A churning ledger: 3 → 2 → 2 + new claim, no claim repeated across
        # all 3 rounds (stuck_claims empty). The exhausted branch must build a
        # triage whose mode is "thrashing" (from the fixture), NOT whatever
        # was pre-set on state["triage"].
        debate_text = _debate(
            _round(1, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha\n[BLOCKER] beta\n[BLOCKER] gamma\n"),
            _round(2, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha\n[BLOCKER] beta\n"),
            _round(3, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] gamma\n[BLOCKER] delta\n"),
        )
        # Seed state with a WRONG triage to prove _debate_decision ignores it.
        state = {
            "task_id": "ex",
            "reviewer_verdict": "REJECT",
            "open_blockers": 2,
            "ux_verdict": "APPROVE",
            "ux_blockers": 0,
            "has_ui": False,
            "debate_text": debate_text,
            "triage": {"mode": "stuck", "recommended": "stop"},  # wrong on purpose
            "hint": "stop",
            "debate_round": 4,
            "debate_round_bonus": 0,
            "effort": "troop-monke",
        }
        delta = D._debate_decision(state, is_verification=True)
        self.assertIn("triage", delta)
        triage = delta["triage"]
        # The triage must be derived from the fixture (thrashing), not the
        # pre-set "stuck" on state.
        self.assertEqual(triage["mode"], "thrashing")
        self.assertEqual(triage["recommended"], "ok")
        self.assertIn("hint", delta)
        self.assertEqual(delta["hint"], "ok")
        # The escalation must be the exhausted one.
        self.assertTrue(delta["escalation"].startswith("debate exhausted"))

    def test_exhausted_triage_converging_from_fixture(self):
        # A converging ledger: 3 → 2 → 1 strictly decreasing, no claim
        # repeated across the last k=2 rounds (the default
        # DEBATE_THRASH_ROUNDS), so stuck_claims is empty.
        debate_text = _debate(
            _round(1, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha\n[BLOCKER] beta\n[BLOCKER] gamma\n"),
            _round(2, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] delta\n[BLOCKER] beta\n"),
            _round(3, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] epsilon\n"),
        )
        state = {
            "task_id": "ex2",
            "reviewer_verdict": "REJECT",
            "open_blockers": 1,
            "ux_verdict": "APPROVE",
            "ux_blockers": 0,
            "has_ui": False,
            "debate_text": debate_text,
            "debate_round": 4,
            "debate_round_bonus": 0,
            "effort": "troop-monke",
        }
        delta = D._debate_decision(state, is_verification=True)
        self.assertEqual(delta["triage"]["mode"], "converging")
        self.assertEqual(delta["triage"]["recommended"], "continue")

    def test_exhausted_zero_blockers_no_triage(self):
        # The 0-blocker convergence branch returns no triage (it converges,
        # does not escalate).
        state = {
            "task_id": "ex3",
            "reviewer_verdict": "APPROVE_WITH_CHANGES",
            "open_blockers": 0,
            "ux_verdict": "APPROVE",
            "ux_blockers": 0,
            "has_ui": False,
            "debate_text": "",
            "debate_round": 4,
            "debate_round_bonus": 0,
            "effort": "troop-monke",
        }
        delta = D._debate_decision(state, is_verification=True)
        self.assertNotIn("triage", delta)
        self.assertEqual(delta["debate_next"], "summary")


# --- TASK-024: theme-overlap / refinement-policy tests (items 33-38) --------


class TestClaimThemeOverlap(unittest.TestCase):
    """Item 33: claim_theme_overlap — identical → 1.0, disjoint → 0.0,
    partial → strictly between 0 and 1."""

    def test_identical_claims_return_one(self):
        self.assertEqual(condenser.claim_theme_overlap(
            "the plan is wrong", "the plan is wrong"), 1.0)

    def test_disjoint_claims_return_zero(self):
        self.assertEqual(condenser.claim_theme_overlap(
            "alpha beta", "gamma delta"), 0.0)

    def test_partial_overlap_is_strictly_between_zero_and_one(self):
        val = condenser.claim_theme_overlap(
            "alpha theme variant", "alpha theme one")
        self.assertGreater(val, 0.0)
        self.assertLess(val, 1.0)

    def test_empty_input_returns_zero(self):
        self.assertEqual(condenser.claim_theme_overlap("", "alpha beta"), 0.0)
        self.assertEqual(condenser.claim_theme_overlap("alpha beta", ""), 0.0)


class TestMajorityNewRefinePrior(unittest.TestCase):
    """Item 34: majority_new_refine_prior — refine-majority → True,
    fresh-majority → False, empty new → False, empty prior → False."""

    def test_refine_majority_returns_true(self):
        # Both new claims refine prior themes at threshold 0.35.
        new = ["alpha theme variant", "beta theme tweak"]
        prior = ["alpha theme one", "beta theme two"]
        self.assertTrue(condenser.majority_new_refine_prior(new, prior, 0.35))

    def test_fresh_majority_returns_false(self):
        new = ["gamma delta", "epsilon zeta"]
        prior = ["alpha beta"]
        self.assertFalse(condenser.majority_new_refine_prior(new, prior, 0.35))

    def test_empty_new_returns_false(self):
        self.assertFalse(condenser.majority_new_refine_prior(
            [], ["alpha"], 0.35))

    def test_empty_prior_returns_false(self):
        self.assertFalse(condenser.majority_new_refine_prior(
            ["alpha"], [], 0.35))

    def test_prior_as_set_accepted(self):
        # prior may be a set, not just a list.
        new = ["alpha theme variant"]
        prior = {"alpha theme one"}
        self.assertTrue(condenser.majority_new_refine_prior(new, prior, 0.35))


class TestThrashingReportPrior(unittest.TestCase):
    """Item 35: thrashing_report['prior'] == sorted(window[-2] claims) on a
    2-active-round fixture, and == [] on a 1-active-round fixture."""

    def test_prior_is_sorted_window_minus_two_claims_on_two_rounds(self):
        text = _debate(
            _round(1, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha\n[BLOCKER] beta\n"),
            _round(2, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER] alpha\n[BLOCKER] gamma\n"),
        )
        r = condenser.thrashing_report(text, 2)
        # window = [round1, round2]; window[-2] = round1 claims = {alpha, beta}.
        self.assertEqual(r["prior"], ["alpha", "beta"])

    def test_prior_is_empty_on_single_active_round(self):
        text = _debate(_round(1, "Reviewer", _REJECT))
        r = condenser.thrashing_report(text, 1)
        self.assertEqual(r["prior"], [])

    def test_prior_is_empty_on_empty_input(self):
        r = condenser.thrashing_report("", 2)
        self.assertEqual(r["prior"], [])


class TestEscalationOptionsRefineLabels(unittest.TestCase):
    """Items 36-37: the "recommended" highlight keys off
    triage["recommended"] on both thrashing menu surfaces."""

    # Item 36: debate thrashing: branch.

    def test_thrashing_continue_recommended_drops_ok_recommended(self):
        triage = {"mode": "thrashing", "recommended": "continue"}
        opts = _common._escalation_options(
            "debate thrashing: blockers not decreasing", triage=triage)
        ok_label = next(o["label"] for o in opts if o["key"] == "ok")
        continue_label = next(o["label"] for o in opts if o["key"] == "continue")
        self.assertNotIn("recommended", ok_label.lower())
        self.assertIn("recommended", continue_label.lower())

    def test_thrashing_ok_recommended_retains_ok_recommended(self):
        triage = {"mode": "thrashing", "recommended": "ok"}
        opts = _common._escalation_options(
            "debate thrashing: blockers not decreasing", triage=triage)
        ok_label = next(o["label"] for o in opts if o["key"] == "ok")
        continue_label = next(o["label"] for o in opts if o["key"] == "continue")
        self.assertIn("recommended", ok_label.lower())
        self.assertNotIn("recommended", continue_label.lower())

    # Item 37: debate exhausted thrashing_exhausted branch.

    def test_exhausted_thrashing_continue_recommended_drops_ok_recommended(self):
        triage = {"mode": "thrashing", "recommended": "continue"}
        opts = _common._escalation_options("debate exhausted 5 rounds",
                                           triage=triage)
        ok_label = next(o["label"] for o in opts if o["key"] == "ok")
        continue_label = next(o["label"] for o in opts if o["key"] == "continue")
        self.assertNotIn("recommended", ok_label.lower())
        self.assertIn("recommended", continue_label.lower())

    def test_exhausted_thrashing_ok_recommended_retains_ok_recommended(self):
        triage = {"mode": "thrashing", "recommended": "ok"}
        opts = _common._escalation_options("debate exhausted 5 rounds",
                                           triage=triage)
        ok_label = next(o["label"] for o in opts if o["key"] == "ok")
        continue_label = next(o["label"] for o in opts if o["key"] == "continue")
        self.assertIn("recommended", ok_label.lower())
        self.assertNotIn("recommended", continue_label.lower())


class TestDebateThrashThemeJaccardYaml(unittest.TestCase):
    """Item 38: DEBATE_THRASH_THEME_JACCARD yaml parse — 0.35 for unset,
    'abc', 'nan', 'inf', '0', '1.5'; 0.5 for '0.5'."""

    @staticmethod
    def _with_yaml(pipeline: dict | None = None):
        @contextmanager
        def _cm():
            import tempfile, yaml as _yaml
            from pathlib import Path
            from tests._yaml_fixture import _baseline_yaml
            saved_wt = os.environ.get("PIPELINE_WT_YAML")
            td = tempfile.TemporaryDirectory()
            try:
                ypath = Path(td.name) / "monkeforge.yaml"
                data = _baseline_yaml()
                if pipeline is not None:
                    dumped = _yaml.dump(pipeline, default_flow_style=False, indent=2)
                    data += "\npipeline:\n" + "\n".join("  " + l for l in dumped.splitlines()) + "\n"
                ypath.write_text(data)
                os.environ["PIPELINE_WT_YAML"] = str(ypath)
                importlib.reload(C)
                yield
            finally:
                if saved_wt is not None:
                    os.environ["PIPELINE_WT_YAML"] = saved_wt
                else:
                    os.environ.pop("PIPELINE_WT_YAML", None)
                td.cleanup()
                importlib.reload(C)
        return _cm()

    def test_unset_defaults_to_0_35(self):
        with self._with_yaml():
            self.assertEqual(C.DEBATE_THRASH_THEME_JACCARD, 0.35)

    def test_non_float_falls_back_to_0_35(self):
        with self._with_yaml({"debate_thrash_theme_jaccard": "abc"}):
            self.assertEqual(C.DEBATE_THRASH_THEME_JACCARD, 0.35)

    def test_nan_falls_back_to_0_35(self):
        with self._with_yaml({"debate_thrash_theme_jaccard": "nan"}):
            self.assertEqual(C.DEBATE_THRASH_THEME_JACCARD, 0.35)

    def test_inf_falls_back_to_0_35(self):
        with self._with_yaml({"debate_thrash_theme_jaccard": "inf"}):
            self.assertEqual(C.DEBATE_THRASH_THEME_JACCARD, 0.35)

    def test_zero_falls_back_to_0_35(self):
        with self._with_yaml({"debate_thrash_theme_jaccard": 0}):
            self.assertEqual(C.DEBATE_THRASH_THEME_JACCARD, 0.35)

    def test_above_one_falls_back_to_0_35(self):
        with self._with_yaml({"debate_thrash_theme_jaccard": 1.5}):
            self.assertEqual(C.DEBATE_THRASH_THEME_JACCARD, 0.35)

    def test_valid_value_is_used(self):
        with self._with_yaml({"debate_thrash_theme_jaccard": 0.5}):
            self.assertEqual(C.DEBATE_THRASH_THEME_JACCARD, 0.5)


# --- F1d: missing-id journal line -------------------------------------------


class TestJournalMissingIds(unittest.TestCase):
    """F1d: _journal_missing_ids records a journal line when a critic raises
    blockers without ids, with round-dedup against state journal."""

    def test_missing_ids_journal_line(self):
        delta = {}
        state = {}
        section = "VERDICT: REJECT\n[BLOCKER] foo\n[BLOCKER] bar\n"
        D._journal_missing_ids(delta, state, section, 1, "t1", "tech")
        self.assertIn("journal", delta)
        self.assertTrue(any("debate ids missing" in j for j in delta["journal"]))
        self.assertNotIn("degradations", delta)

    def test_all_with_ids_no_journal_line(self):
        delta = {}
        state = {}
        section = "VERDICT: REJECT\n[BLOCKER] B1: foo\n[BLOCKER] B2: bar\n"
        D._journal_missing_ids(delta, state, section, 1, "t1", "tech")
        self.assertNotIn("journal", delta)

    def test_round_dedup_via_state_journal(self):
        """Re-journaling the same (round, critic) pair does not duplicate."""
        delta1 = {}
        state = {}
        section = "VERDICT: REJECT\n[BLOCKER] foo\n"
        D._journal_missing_ids(delta1, state, section, 1, "t1", "tech")
        self.assertEqual(len(delta1["journal"]), 1)

        # Second call with the same line already in state journal — no dup.
        delta2 = {}
        state2 = {"journal": list(delta1["journal"])}
        D._journal_missing_ids(delta2, state2, section, 1, "t1", "tech")
        self.assertNotIn("journal", delta2)

    def test_different_critic_same_round_both_journal(self):
        delta = {}
        state = {}
        section = "VERDICT: REJECT\n[BLOCKER] foo\n"
        D._journal_missing_ids(delta, state, section, 1, "t1", "tech")
        D._journal_missing_ids(delta, state, section, 1, "t1", "ux")
        missing_lines = [j for j in delta.get("journal", []) if "debate ids missing" in j]
        self.assertEqual(len(missing_lines), 2)

    def test_empty_section_no_journal_line(self):
        delta = {}
        state = {}
        D._journal_missing_ids(delta, state, "", 1, "t1", "tech")
        self.assertNotIn("journal", delta)


# --- D5: critic-qualified thrash with blocker IDs ---------------------------


class TestCriticQualifiedThrashIds(unittest.TestCase):
    """D2: when blockers carry ids, the thrashing/stuck token is
    critic-qualified (``ReviewerB1``, ``UXB1``) so a Reviewer B1 and a UX B1
    are distinct claims. A B1 raised by Reviewer in r1 and a B1 raised by UX
    in r2 must NOT be "stuck" (different tokens), while the same Reviewer B1
    across rounds IS stuck."""

    def test_same_reviewer_b1_across_rounds_is_stuck(self):
        text = _debate(
            _round(1, "Reviewer", "VERDICT: REJECT\n[BLOCKER] B1: alpha\n"),
            _round(2, "Reviewer", "VERDICT: REJECT\n[BLOCKER] B1: alpha\n"),
        )
        r = condenser.thrashing_report(text, 2)
        self.assertEqual(r["mode"], "stuck")

    def test_reviewer_b1_and_ux_b1_are_distinct(self):
        # Reviewer B1 in r1, UX B1 in r2 — different critic-qualified tokens,
        # so the intersection is empty and this is NOT stuck.
        text = _debate(
            _round(1, "Reviewer", "VERDICT: REJECT\n[BLOCKER] B1: alpha\n"),
            _round(2, "UX", "VERDICT: REJECT\n[BLOCKER] B1: alpha\n"),
        )
        r = condenser.thrashing_report(text, 2)
        self.assertNotEqual(r["mode"], "stuck")

    def test_reviewer_b1_and_reviewer_b2_are_distinct(self):
        # Different ids from the same critic are distinct tokens.
        text = _debate(
            _round(1, "Reviewer", "VERDICT: REJECT\n[BLOCKER] B1: alpha\n"),
            _round(2, "Reviewer", "VERDICT: REJECT\n[BLOCKER] B2: beta\n"),
        )
        r = condenser.thrashing_report(text, 2)
        self.assertNotEqual(r["mode"], "stuck")


# --- D5: UX rubber-stamp widened for id-based RESOLVED ----------------------


class TestUxRubberStampWidened(unittest.TestCase):
    """D5: the rubber-stamp guard accepts id-based RESOLVED markers
    (B1: RESOLVED — …) as proof the designer walked prior blockers."""

    def test_id_resolved_marker_prevents_rubber_stamp(self):
        """A bare APPROVE with B1: RESOLVED lines should NOT be treated as
        a rubber stamp — the designer walked the prior blockers by id."""
        import inspect
        source = inspect.getsource(D.debate_ux)
        # The widened check uses _UX_ID_RESOLVED_RE from condenser.
        self.assertIn("_UX_ID_RESOLVED_RE", source)
        self.assertIn("has_resolution_signal", source)

    def test_bare_id_without_resolved_does_not_bypass_guard(self):
        """A bare ``B1: <claim>`` without RESOLVED/STILL OPEN must NOT count
        as a resolution signal — the designer must explicitly rule on each
        prior blocker by id."""
        from pipeline_graph.condenser import _UX_ID_RESOLVED_RE
        # B1: RESOLVED matches.
        self.assertTrue(_UX_ID_RESOLVED_RE.search("B1: RESOLVED — fixed it\n"))
        # S1: STILL OPEN matches.
        self.assertTrue(_UX_ID_RESOLVED_RE.search("S1: STILL OPEN — not done\n"))
        # Bare B1: <claim> does NOT match.
        self.assertFalse(_UX_ID_RESOLVED_RE.search("B1: this is still a problem\n"))
        # Bare B1: with no keyword does NOT match.
        self.assertFalse(_UX_ID_RESOLVED_RE.search("B1: alpha\n"))


if __name__ == "__main__":
    unittest.main()
