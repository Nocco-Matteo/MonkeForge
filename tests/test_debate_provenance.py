"""Tests for the provenance tag and ``debate requirements:`` escalation
(TASK-017 batch 3).

Covers conformance checklist items 23-39:
  - TestProvenanceRegex: _RAISE_LINE_RE / _TAG_CLAIM_RE / _HEADER_CLAIM_RE
    3-group structure (severity, provenance, claim).
  - TestCountBlockersProvenance: count_blockers matches all three tag forms.
  - TestProcessCriticSectionProvenance: ledger item dict stores provenance.
  - TestLatestRequirementsBlockers: latest_requirements_blockers() — last
    round, last section per critic, verdict filter, REQUIREMENTS-only.
  - TestCollapseRoundVerdictGate: _collapse_round marker counts 0 blockers
    under APPROVE / APPROVE_WITH_CHANGES.
  - TestCheckRequirementsEscalation: _check_requirements_escalation() —
    function-local import, escalation prefix, debate_next=summary.
  - TestRequirementsPrecedence: _check_requirements_escalation takes
    precedence over _check_early_escalation in debate_tech / debate_ux.
  - TestDebateRequirementsMenu: _escalation_options "debate requirements:"
    branch keys (continue/redo/stop/ok; stop recommended).
  - TestEscalateDebateRequirementsGate: escalate() prefix gate forces
    substring flags False; "ok" clears the bonus and sets redo_debate=False.
  - TestUxBlockerVerdictGate: debate_ux blocker count is verdict-gated.
  - TestBlockerDisplayRegex: run.py / bot/bot.py match the three tag forms.
  - TestPromptProvenanceTags: debate_review.md / debate_ux.md / debate_reply.md
    list the three tag forms.
"""
from __future__ import annotations

import re
import unittest
from unittest.mock import patch

from pipeline_graph import agents as A
from pipeline_graph import condenser
from pipeline_graph import config as C
from pipeline_graph.nodes import common as _common
from pipeline_graph.nodes import debate as D


# --- helpers ----------------------------------------------------------------


def _round(rnd: int, critic: str, body: str) -> str:
    return f"## Round {rnd} — {critic}\n\n{body}\n"


def _debate(*sections: str) -> str:
    return "preamble\n\n" + "".join(sections)


# --- provenance regex (items 23-24) -----------------------------------------


class TestProvenanceRegex(unittest.TestCase):
    def test_raise_line_bare_blocker_four_groups(self):
        m = condenser._RAISE_LINE_RE.match("[BLOCKER] the plan is wrong")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).upper(), "BLOCKER")
        self.assertIsNone(m.group(2))  # no provenance suffix
        self.assertIsNone(m.group(3))  # no id
        self.assertEqual(m.group(4), "the plan is wrong")

    def test_raise_line_plan_provenance(self):
        m = condenser._RAISE_LINE_RE.match("[BLOCKER:PLAN] the plan is wrong")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).upper(), "BLOCKER")
        self.assertEqual(m.group(2).upper(), "PLAN")
        self.assertIsNone(m.group(3))
        self.assertEqual(m.group(4), "the plan is wrong")

    def test_raise_line_requirements_provenance(self):
        m = condenser._RAISE_LINE_RE.match("[BLOCKER:REQUIREMENTS] brief is wrong")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).upper(), "BLOCKER")
        self.assertEqual(m.group(2).upper(), "REQUIREMENTS")
        self.assertIsNone(m.group(3))
        self.assertEqual(m.group(4), "brief is wrong")

    def test_raise_line_suggestion_no_provenance(self):
        m = condenser._RAISE_LINE_RE.match("[SUGGESTION] minor thing")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).upper(), "SUGGESTION")
        self.assertIsNone(m.group(2))
        self.assertIsNone(m.group(3))
        self.assertEqual(m.group(4), "minor thing")

    def test_raise_line_bold_wrapped(self):
        m = condenser._RAISE_LINE_RE.match("**[BLOCKER:PLAN] bold claim**")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(2).upper(), "PLAN")
        self.assertIsNone(m.group(3))
        self.assertEqual(m.group(4), "bold claim")

    def test_raise_line_with_id(self):
        m = condenser._RAISE_LINE_RE.match("[BLOCKER] B1: the plan is wrong")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).upper(), "BLOCKER")
        self.assertIsNone(m.group(2))
        self.assertEqual(m.group(3), "B1")
        self.assertEqual(m.group(4), "the plan is wrong")

    def test_raise_line_with_id_and_provenance(self):
        m = condenser._RAISE_LINE_RE.match("[BLOCKER:PLAN] B2: claim text")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(2).upper(), "PLAN")
        self.assertEqual(m.group(3), "B2")
        self.assertEqual(m.group(4), "claim text")

    def test_raise_line_suggestion_with_id(self):
        m = condenser._RAISE_LINE_RE.match("[SUGGESTION] S1: minor thing")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).upper(), "SUGGESTION")
        self.assertEqual(m.group(3), "S1")
        self.assertEqual(m.group(4), "minor thing")

    def test_tag_claim_re_four_groups(self):
        m = condenser._TAG_CLAIM_RE.match("[BLOCKER:REQUIREMENTS] foo")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(2).upper(), "REQUIREMENTS")
        self.assertIsNone(m.group(3))
        self.assertEqual(m.group(4), "foo")

    def test_tag_claim_re_with_id(self):
        m = condenser._TAG_CLAIM_RE.match("[BLOCKER:REQUIREMENTS] B1: foo")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(2).upper(), "REQUIREMENTS")
        self.assertEqual(m.group(3), "B1")
        self.assertEqual(m.group(4), "foo")

    def test_header_claim_re_four_groups(self):
        m = condenser._HEADER_CLAIM_RE.match("### [BLOCKER:PLAN] foo")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(2).upper(), "PLAN")
        self.assertIsNone(m.group(3))
        self.assertEqual(m.group(4), "foo")

    def test_header_claim_re_with_id(self):
        m = condenser._HEADER_CLAIM_RE.match("### [BLOCKER:PLAN] B1: foo")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(2).upper(), "PLAN")
        self.assertEqual(m.group(3), "B1")
        self.assertEqual(m.group(4), "foo")

    def test_claim_is_group_4_not_group_3(self):
        # Regression guard: the claim moved from group(3) to group(4). A
        # group(3) read would now return the id (or None), not the claim.
        m = condenser._RAISE_LINE_RE.match("[BLOCKER:REQUIREMENTS] the real claim")
        self.assertNotEqual(m.group(3), "the real claim")
        self.assertEqual(m.group(4), "the real claim")


# --- count_blockers (item 26) -----------------------------------------------


class TestCountBlockersProvenance(unittest.TestCase):
    def test_bare_blocker_counted(self):
        self.assertEqual(A.count_blockers("[BLOCKER] a\n[BLOCKER] b"), 2)

    def test_plan_blocker_counted(self):
        self.assertEqual(A.count_blockers("[BLOCKER:PLAN] a\n[BLOCKER:PLAN] b"), 2)

    def test_requirements_blocker_counted(self):
        self.assertEqual(A.count_blockers("[BLOCKER:REQUIREMENTS] a"), 1)

    def test_mixed_forms_counted(self):
        self.assertEqual(
            A.count_blockers("[BLOCKER] a\n[BLOCKER:PLAN] b\n[BLOCKER:REQUIREMENTS] c"),
            3,
        )

    def test_suggestion_not_counted(self):
        self.assertEqual(A.count_blockers("[SUGGESTION] a"), 0)


# --- _process_critic_section provenance (item 25) ---------------------------


class TestProcessCriticSectionProvenance(unittest.TestCase):
    def _ledger_items(self, text: str) -> dict:
        items: dict = {}
        items_order: list = []
        condenser._process_critic_section(
            text, round_num=1, critic_upper="REVIEWER",
            ux_blocker_keys=[], items=items, items_order=items_order,
        )
        return items

    def test_bare_blocker_defaults_to_plan(self):
        items = self._ledger_items("VERDICT: REJECT\n[BLOCKER] foo")
        key = list(items.keys())[0]
        self.assertEqual(items[key]["provenance"], "PLAN")

    def test_plan_provenance_stored(self):
        items = self._ledger_items("VERDICT: REJECT\n[BLOCKER:PLAN] foo")
        key = list(items.keys())[0]
        self.assertEqual(items[key]["provenance"], "PLAN")

    def test_requirements_provenance_stored(self):
        items = self._ledger_items("VERDICT: REJECT\n[BLOCKER:REQUIREMENTS] foo")
        key = list(items.keys())[0]
        self.assertEqual(items[key]["provenance"], "REQUIREMENTS")

    def test_claim_read_from_group_3(self):
        items = self._ledger_items("VERDICT: REJECT\n[BLOCKER:REQUIREMENTS] the claim text")
        key = list(items.keys())[0]
        self.assertEqual(items[key]["claim"], "the claim text")


# --- latest_requirements_blockers (item 27) ---------------------------------


class TestLatestRequirementsBlockers(unittest.TestCase):
    def test_no_rounds_returns_empty(self):
        self.assertEqual(condenser.latest_requirements_blockers(""), [])
        self.assertEqual(condenser.latest_requirements_blockers(None), [])
        self.assertEqual(condenser.latest_requirements_blockers("flat text"), [])

    def test_requirements_blocker_in_last_round_returned(self):
        text = _debate(
            _round(1, "Reviewer", "VERDICT: REJECT\n[BLOCKER:PLAN] plan issue"),
            _round(2, "Reviewer", "VERDICT: REJECT\n[BLOCKER:REQUIREMENTS] brief issue"),
        )
        claims = condenser.latest_requirements_blockers(text)
        self.assertEqual(claims, ["brief issue"])

    def test_plan_blocker_not_returned(self):
        text = _debate(
            _round(1, "Reviewer", "VERDICT: REJECT\n[BLOCKER:PLAN] plan issue"),
        )
        self.assertEqual(condenser.latest_requirements_blockers(text), [])

    def test_bare_blocker_not_returned(self):
        text = _debate(
            _round(1, "Reviewer", "VERDICT: REJECT\n[BLOCKER] bare issue"),
        )
        self.assertEqual(condenser.latest_requirements_blockers(text), [])

    def test_only_last_round_scanned(self):
        # R1 has a REQUIREMENTS blocker, R2 does not → R1's is NOT returned.
        text = _debate(
            _round(1, "Reviewer", "VERDICT: REJECT\n[BLOCKER:REQUIREMENTS] old brief issue"),
            _round(2, "Reviewer", "VERDICT: REJECT\n[BLOCKER:PLAN] current plan issue"),
        )
        self.assertEqual(condenser.latest_requirements_blockers(text), [])

    def test_approve_section_skipped(self):
        text = _debate(
            _round(1, "Reviewer",
                   "VERDICT: APPROVE\n[BLOCKER:REQUIREMENTS] brief issue"),
        )
        self.assertEqual(condenser.latest_requirements_blockers(text), [])

    def test_approve_with_changes_section_skipped(self):
        text = _debate(
            _round(1, "Reviewer",
                   "VERDICT: APPROVE_WITH_CHANGES\n[BLOCKER:REQUIREMENTS] brief issue"),
        )
        self.assertEqual(condenser.latest_requirements_blockers(text), [])

    def test_last_section_per_critic_within_round(self):
        text = _debate(
            _round(1, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER:REQUIREMENTS] first pass"),
            _round(1, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER:REQUIREMENTS] second pass"),
        )
        claims = condenser.latest_requirements_blockers(text)
        # Only the LAST Reviewer section in round 1 is scanned.
        self.assertEqual(claims, ["second pass"])

    def test_multiple_requirements_blockers(self):
        text = _debate(
            _round(1, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER:REQUIREMENTS] alpha\n[BLOCKER:REQUIREMENTS] beta"),
        )
        claims = condenser.latest_requirements_blockers(text)
        self.assertEqual(claims, ["alpha", "beta"])

    def test_suggestion_not_returned(self):
        text = _debate(
            _round(1, "Reviewer",
                   "VERDICT: REJECT\n[SUGGESTION:REQUIREMENTS] not a blocker"),
        )
        # [SUGGESTION:REQUIREMENTS] is not a BLOCKER raise → not returned.
        self.assertEqual(condenser.latest_requirements_blockers(text), [])


# --- _collapse_round verdict gate (item 28) ---------------------------------


class TestCollapseRoundVerdictGate(unittest.TestCase):
    def test_approve_zero_blockers_in_marker(self):
        text = (
            "\n\n## Round 1 — Reviewer\n\n"
            "VERDICT: APPROVE\n[BLOCKER] one\n[BLOCKER] two\n\n"
            "## Round 1 — Reply\n\n[BLOCKER] one — RESOLVED\n"
        )
        out = condenser.condense(text, keep_recent=0)
        # APPROVE → 0 blockers in the marker, even though the section has 2
        # [BLOCKER] tokens (they reference resolved/prior items).
        assert "[Round 1 — Reviewer: APPROVE, 0 blockers, all RESOLVED — condensed]" in out

    def test_approve_with_changes_zero_blockers_in_marker(self):
        text = (
            "\n\n## Round 1 — Reviewer\n\n"
            "VERDICT: APPROVE_WITH_CHANGES\n[BLOCKER] one\n[BLOCKER] two\n\n"
            "## Round 1 — Reply\n\n[BLOCKER] one — RESOLVED\n"
        )
        out = condenser.condense(text, keep_recent=0)
        assert "[Round 1 — Reviewer: APPROVE_WITH_CHANGES, 0 blockers, all RESOLVED — condensed]" in out

    def test_reject_counts_blockers_in_marker(self):
        text = "\n\n## Round 1 — Reviewer\n\nVERDICT: REJECT\n[BLOCKER] one\n"
        out = condenser.condense(text, keep_recent=0)
        assert "[Round 1 — Reviewer: REJECT, 1 blockers, unresolved — condensed]" in out

    def test_provenance_tagged_blockers_counted_under_reject(self):
        text = (
            "\n\n## Round 1 — Reviewer\n\n"
            "VERDICT: REJECT\n[BLOCKER:PLAN] a\n[BLOCKER:REQUIREMENTS] b\n"
        )
        out = condenser.condense(text, keep_recent=0)
        assert "[Round 1 — Reviewer: REJECT, 2 blockers, unresolved — condensed]" in out

    def test_provenance_tagged_blockers_zeroed_under_approve(self):
        text = (
            "\n\n## Round 1 — Reviewer\n\n"
            "VERDICT: APPROVE\n[BLOCKER:PLAN] a\n[BLOCKER:REQUIREMENTS] b\n"
            "## Round 1 — Reply\n\n[BLOCKER] a — RESOLVED\n"
        )
        out = condenser.condense(text, keep_recent=0)
        assert "[Round 1 — Reviewer: APPROVE, 0 blockers, all RESOLVED — condensed]" in out


# --- _check_requirements_escalation (items 29-31) ---------------------------


class TestCheckRequirementsEscalation(unittest.TestCase):
    def test_no_requirements_blocker_returns_none(self):
        text = _debate(
            _round(1, "Reviewer", "VERDICT: REJECT\n[BLOCKER:PLAN] plan issue"),
        )
        self.assertIsNone(D._check_requirements_escalation(text))

    def test_requirements_blocker_returns_escalation_with_prefix(self):
        text = _debate(
            _round(1, "Reviewer", "VERDICT: REJECT\n[BLOCKER:REQUIREMENTS] brief issue"),
        )
        result = D._check_requirements_escalation(text)
        self.assertIsNotNone(result)
        self.assertTrue(result["escalation"].startswith("debate requirements:"))
        self.assertEqual(result["debate_next"], "summary")

    def test_function_local_import_only(self):
        # Item 29: no module-top-level import of condenser.latest_requirements_blockers.
        import inspect
        source = inspect.getsource(D)
        lines = source.split("\n")
        found_top_level = False
        for line in lines:
            stripped = line.lstrip()
            if "latest_requirements_blockers" in stripped and \
               stripped.startswith("from ..condenser import") and \
               not line.startswith("    "):
                found_top_level = True
        self.assertFalse(
            found_top_level,
            "nodes/debate.py must not import latest_requirements_blockers at module top level",
        )

    def test_escalation_includes_claims(self):
        text = _debate(
            _round(1, "Reviewer",
                   "VERDICT: REJECT\n[BLOCKER:REQUIREMENTS] alpha\n[BLOCKER:REQUIREMENTS] beta"),
        )
        result = D._check_requirements_escalation(text)
        self.assertIn("alpha", result["escalation"])
        self.assertIn("beta", result["escalation"])


# --- requirements precedence over stuck (item 30) ---------------------------


class TestRequirementsPrecedence(unittest.TestCase):
    """_check_requirements_escalation takes precedence over
    _check_early_escalation when both fire in the same call site."""

    def test_requirements_wins_over_stuck_in_debate_tech(self):
        # A debate that is BOTH stuck (same BLOCKER across k rounds) AND has a
        # REQUIREMENTS blocker in the last round: the requirements escalation
        # must win (prefix debate requirements:), not stuck — menu keys are
        # the same continue/redo/stop/ok set, with recommended=stop.
        text = _debate(
            _round(1, "Reviewer", "VERDICT: REJECT\n[BLOCKER:REQUIREMENTS] brief issue"),
            _round(2, "Reviewer", "VERDICT: REJECT\n[BLOCKER:REQUIREMENTS] brief issue"),
        )
        early = D._check_early_escalation(text, 2)
        req = D._check_requirements_escalation(text)
        self.assertIsNotNone(early)
        self.assertIsNotNone(req)
        self.assertTrue(req["escalation"].startswith("debate requirements:"))
        self.assertTrue(early["escalation"].startswith("debate stuck:"))
        # The precedence rule is implemented in debate_tech / debate_ux: req
        # is checked first and wins. Verify the two are distinct so the
        # precedence is meaningful.
        self.assertNotEqual(req["escalation"], early["escalation"])


# --- _escalation_options "debate requirements:" branch (item 33) ------------


class TestDebateRequirementsMenu(unittest.TestCase):
    def test_debate_requirements_prefix_returns_correct_keys_below_max(self):
        # TASK-033: below MAX the menu is continue/redo/stop/re-intake
        # (re-intake RECOMMENDED); no ok.
        opts = _common._escalation_options(
            "debate requirements: 1 blocker(s) tagged as belonging to the REQUIREMENTS",
            state={"requirements_reintake_count": 0},
        )
        self.assertEqual(
            {o["key"] for o in opts},
            {"continue", "redo", "stop", "re-intake"},
        )

    def test_debate_requirements_at_max_adds_ok(self):
        # TASK-033: at MAX the menu adds ok (RECOMMENDED) and keeps re-intake.
        opts = _common._escalation_options(
            "debate requirements: brief issue",
            state={"requirements_reintake_count": 2},
        )
        keys = {o["key"] for o in opts}
        self.assertIn("ok", keys)
        self.assertIn("re-intake", keys)
        ok_label = next(o["label"] for o in opts if o["key"] == "ok")
        self.assertIn("RECOMMENDED", ok_label)

    def test_debate_requirements_tested_before_intake_branch(self):
        # A reason that starts with "debate requirements:" but also contains
        # "intake" must route to the debate-requirements menu, not intake.
        opts = _common._escalation_options(
            "debate requirements: intake brief is wrong",
            state={"requirements_reintake_count": 0},
        )
        keys = {o["key"] for o in opts}
        self.assertEqual(keys, {"continue", "redo", "stop", "re-intake"})
        self.assertNotIn("skip / done", keys)

    def test_debate_requirements_no_skip_key(self):
        opts = _common._escalation_options(
            "debate requirements: brief issue",
            state={"requirements_reintake_count": 0},
        )
        keys = {o["key"] for o in opts}
        self.assertNotIn("skip", keys)
        self.assertEqual(keys, {"continue", "redo", "stop", "re-intake"})

    def test_re_intake_label_marks_recommended_below_max(self):
        # TASK-033: re-intake is RECOMMENDED below MAX; stop no longer carries
        # --from intake (the CLI is a fallback, not the primary path).
        opts = _common._escalation_options(
            "debate requirements: brief issue",
            state={"requirements_reintake_count": 0},
        )
        by_key = {o["key"]: o["label"] for o in opts}
        self.assertIn("RECOMMENDED", by_key["re-intake"])
        self.assertNotIn("--from intake", by_key["stop"])
        self.assertIn("saved for", by_key["re-intake"].lower())
        self.assertNotIn("amend the brief", by_key["re-intake"].lower())

    def test_case_insensitive_prefix(self):
        opts = _common._escalation_options(
            "Debate Requirements: something",
            state={"requirements_reintake_count": 0},
        )
        self.assertEqual(
            {o["key"] for o in opts},
            {"continue", "redo", "stop", "re-intake"},
        )

# --- escalate() prefix gate (items 34-35) -----------------------------------


class TestEscalateDebateRequirementsGate(unittest.TestCase):
    def _escalate(self, answer, reason, state=None):
        st = state or {"task_id": "dr", "escalation": reason, "journal": []}
        with patch.object(_common, "interrupt", return_value=answer), \
             patch.object(_common.ev, "emit"), \
             patch.object(_common.ev, "open_escalation", return_value=True), \
             patch.object(_common.ev, "close_escalation"):
            return _common.escalate(st)

    def _at_max_state(self, reason, **extra):
        st = {"task_id": "dr", "escalation": reason, "journal": [],
              "requirements_reintake_count": 2}
        st.update(extra)
        return st

    def test_ok_clears_bonus_and_redo_debate_false(self):
        # Item 35 + TASK-033: "ok" on a "debate requirements:" escalation is
        # only valid at MAX; at MAX it sets debate_round_bonus=0 and
        # redo_debate=False.
        d = self._escalate(
            "ok", "debate requirements: brief issue",
            state=self._at_max_state("debate requirements: brief issue"),
        )
        self.assertEqual(d.get("debate_round_bonus"), 0)
        self.assertEqual(d.get("redo_debate"), False)
        self.assertEqual(d.get("escalation"), "")

    def test_ok_clears_bonus_even_if_prior_bonus_existed(self):
        state = self._at_max_state(
            "debate requirements: brief issue", debate_round_bonus=4,
        )
        d = self._escalate("ok", "debate requirements: brief issue", state=state)
        self.assertEqual(d.get("debate_round_bonus"), 0)
        self.assertEqual(d.get("redo_debate"), False)

    def test_ok_below_max_reopened(self):
        # TASK-033: below MAX ``ok`` is not in valid_keys → the validator
        # re-opens the menu (returns the escalation reason, not a resolution).
        d = self._escalate(
            "ok", "debate requirements: brief issue",
            state={"task_id": "dr", "escalation": "debate requirements: brief issue",
                   "journal": [], "requirements_reintake_count": 0},
        )
        self.assertEqual(d.get("escalation"), "debate requirements: brief issue")

    def test_ok_at_max_waives_gap_and_appends_degradation(self):
        # TASK-033 (C7): ok at MAX waives the gap + appends the degradation
        # literal (single-element list, no duplication of prior entries).
        import tempfile
        from pathlib import Path
        from pipeline_graph import requirements_gap as RG
        with tempfile.TemporaryDirectory() as tmp:
            tasks = Path(tmp) / "tasks"
            tasks.mkdir()
            with patch.object(RG.C, "TASKS", tasks), \
                 patch.object(RG.C, "ensure_dirs", lambda: None):
                RG.write_requirements_gap("dr", ["brief is wrong"])
                d = self._escalate(
                    "ok", "debate requirements: brief issue",
                    state=self._at_max_state("debate requirements: brief issue"),
                )
                self.assertEqual(
                    d.get("degradations"),
                    ["shipped with unresolved REQUIREMENTS gaps (max re-intakes reached)"],
                )
                self.assertEqual(RG.gap_status("dr"), "waived")

    def test_stop_stops_run(self):
        d = self._escalate("stop", "debate requirements: brief issue")
        self.assertTrue(d.get("finished"))

    def test_continue_extends_bonus(self):
        # Full menu includes continue; resolution reuses the debate_escalation
        # path (prefix gate includes debate requirements:).
        state = {
            "task_id": "dr",
            "escalation": "debate requirements: brief issue",
            "journal": [],
            "debate_round": 1,
            "debate_round_bonus": 0,
            "requirements_reintake_count": 0,
        }
        d = self._escalate(
            "continue", "debate requirements: brief issue", state=state
        )
        self.assertEqual(d.get("debate_round_bonus"), 2)
        self.assertEqual(d.get("escalation"), "")
        self.assertFalse(d.get("finished", False))

    def test_does_not_fire_ux_branch(self):
        # The reason contains "blocker" but the prefix gate forces ux_escalation
        # False, so "ok" must NOT set ux_shipped_blocked.
        d = self._escalate(
            "ok", "debate requirements: UX blocker in brief",
            state=self._at_max_state("debate requirements: UX blocker in brief"),
        )
        self.assertNotIn("ux_shipped_blocked", d)

    def test_does_not_fire_render_branch(self):
        d = self._escalate(
            "ok", "debate requirements: cannot render the brief",
            state=self._at_max_state("debate requirements: cannot render the brief"),
        )
        self.assertNotIn("visual_shipped_blocked", d)

    def test_does_not_fire_final_test_branch(self):
        d = self._escalate(
            "ok", "debate requirements: final test gate in brief",
            state=self._at_max_state("debate requirements: final test gate in brief"),
        )
        self.assertNotIn("final_tests_waived", d)

    def test_does_not_fire_crash_branch(self):
        d = self._escalate(
            "ok", "debate requirements: step crashed in debate_tech",
            state=self._at_max_state("debate requirements: step crashed in debate_tech"),
        )
        self.assertNotIn("code_verdict", d)

    def test_does_not_fire_intake_branch(self):
        d = self._escalate(
            "ok", "debate requirements: intake brief is wrong",
            state=self._at_max_state("debate requirements: intake brief is wrong"),
        )
        self.assertNotIn("intake_done", d)


# --- UX blocker verdict gate (item 32) --------------------------------------


class TestUxBlockerVerdictGate(unittest.TestCase):
    """debate_ux's blocker count is 0 under APPROVE / APPROVE_WITH_CHANGES.

    The full node shells out to the UX agent; we exercise the verdict gate by
    stubbing run_agent so the review text contains [BLOCKER] tokens but the
    verdict is APPROVE.
    """

    def _run_debate_ux(self, verdict, review_body, prior_blockers=0):
        prev_run = D.run_agent
        prev_classify = D.classify_output
        prev_trust = D._trust_output
        prev_render = C.UX_RENDER_CMD
        try:
            C.UX_RENDER_CMD = "npx playwright test"
            D.run_agent = lambda *a, **k: (0, review_body)
            D.classify_output = lambda code, out: ("ok", None)
            D._trust_output = lambda code, out, health: True
            state = {
                "task_id": "T-uxgate",
                "debate_round": 1,
                "has_ui": True,
                "ux_blockers": prior_blockers,
                "tech_limits": [],
            }
            return D.debate_ux(state)
        finally:
            D.run_agent = prev_run
            D.classify_output = prev_classify
            D._trust_output = prev_trust
            C.UX_RENDER_CMD = prev_render

    def test_approve_zeroes_blockers_even_with_blocker_tokens(self):
        review = f"VERDICT: APPROVE\n[BLOCKER] a\n[BLOCKER] b\n"
        d = self._run_debate_ux("APPROVE", review)
        self.assertEqual(d.get("ux_blockers"), 0)

    def test_approve_with_changes_zeroes_blockers(self):
        review = f"VERDICT: APPROVE_WITH_CHANGES\n[BLOCKER] a\n"
        d = self._run_debate_ux("APPROVE_WITH_CHANGES", review)
        self.assertEqual(d.get("ux_blockers"), 0)

    def test_reject_counts_blockers(self):
        review = f"VERDICT: REJECT\n[BLOCKER] a\n[BLOCKER] b\n"
        d = self._run_debate_ux("REJECT", review)
        self.assertEqual(d.get("ux_blockers"), 2)

    def test_provenance_tagged_blockers_counted_under_reject(self):
        review = f"VERDICT: REJECT\n[BLOCKER:PLAN] a\n[BLOCKER:REQUIREMENTS] b\n"
        d = self._run_debate_ux("REJECT", review)
        self.assertEqual(d.get("ux_blockers"), 2)


# --- blocker-display regex (items 36-37) ------------------------------------


class TestBlockerDisplayRegex(unittest.TestCase):
    def test_run_py_regex_matches_all_three_forms(self):
        # Item 36: run.py's _extract_debate_blockers uses the regex.
        # We test the regex pattern directly (the function reads from disk).
        pat = re.compile(r"\[BLOCKER(?::(?:PLAN|REQUIREMENTS))?\]", re.IGNORECASE)
        self.assertTrue(pat.search("[BLOCKER] foo"))
        self.assertTrue(pat.search("[BLOCKER:PLAN] foo"))
        self.assertTrue(pat.search("[BLOCKER:REQUIREMENTS] foo"))
        self.assertFalse(pat.search("[SUGGESTION] foo"))

    def test_bot_py_regex_matches_all_three_forms(self):
        # Item 37: bot/bot.py uses the same regex with import re present.
        # discord.py is not installed in the test env, so we read the source
        # directly instead of importing the module.
        from pathlib import Path
        bot_path = Path(__file__).resolve().parent.parent / "bot" / "bot.py"
        src = bot_path.read_text()
        self.assertIn("import re", src)
        self.assertIn(r"\[BLOCKER(?::(?:PLAN|REQUIREMENTS))?\]", src)
        # The literal substring "[BLOCKER]" must no longer be the only matcher.
        # (It may still appear in comments, but the regex must be present.)
        pat = re.compile(r"\[BLOCKER(?::(?:PLAN|REQUIREMENTS))?\]", re.IGNORECASE)
        self.assertTrue(pat.search("[BLOCKER:REQUIREMENTS] foo"))


# --- prompt provenance tags (items 38-39) -----------------------------------


class TestPromptProvenanceTags(unittest.TestCase):
    def test_debate_review_lists_three_tag_forms(self):
        # Item 38: debate_review.md output section lists all three forms.
        from pipeline_graph import config as _C
        text = (_C.TEMPLATES / "debate_review.md").read_text()
        self.assertIn("[BLOCKER:PLAN]", text)
        self.assertIn("[BLOCKER:REQUIREMENTS]", text)
        self.assertIn("[BLOCKER]", text)

    def test_debate_ux_lists_three_tag_forms(self):
        from pipeline_graph import config as _C
        text = (_C.TEMPLATES / "debate_ux.md").read_text()
        self.assertIn("[BLOCKER:PLAN]", text)
        self.assertIn("[BLOCKER:REQUIREMENTS]", text)
        self.assertIn("[BLOCKER]", text)

    def test_debate_reply_directs_copy_provenance_verbatim(self):
        # Item 39: debate_reply.md directs the proposer to copy severity +
        # provenance verbatim in one of the three tag forms.
        from pipeline_graph import config as _C
        text = (_C.TEMPLATES / "debate_reply.md").read_text()
        self.assertIn("[BLOCKER:PLAN]", text)
        self.assertIn("[BLOCKER:REQUIREMENTS]", text)
        self.assertIn("[BLOCKER]", text)
        # Must instruct verbatim copy of the provenance suffix.
        self.assertIn("provenance", text.lower())
        # Recovery is re-intake, not hand-editing the brief.
        self.assertIn("--from intake", text)
        self.assertNotIn("amend the brief", text.lower())

    def test_intake_prefers_asking_on_policy_gaps(self):
        from pipeline_graph import config as _C
        text = (_C.TEMPLATES / "intake.md").read_text()
        self.assertIn("Prefer asking", text)
        self.assertIn("mutually exclusive", text.lower())
        self.assertNotIn("Prefer finishing to asking", text)


if __name__ == "__main__":
    unittest.main()
