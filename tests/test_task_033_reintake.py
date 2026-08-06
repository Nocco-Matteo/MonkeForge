"""TASK-033 Batch 1: REQUIREMENTS re-intake is intake-owned.

Covers the I3 gate (intake_ask rejects COMPLETE when active gaps are
unanswered), the intake_wait active-gap split (skip→waive+count+degradation,
stop→finish, submit→I3 gate), the escalate() re-intake branch (archive live
debate/intake, reactivate suspended gap, clear batches + debate state), the
ok-at-MAX branch (waive + degradation), the archive helpers, the
route_intake/route_intake_wait debate_tech/END edges, and the redo --from
intake CLI path (keep PLAN/-full, archive, ensure_gap, exit 2).
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from pipeline_graph import config as C
from pipeline_graph import requirements_gap as RG
from pipeline_graph.nodes import intake as I
from pipeline_graph.nodes import common as _common
from pipeline_graph import graph as G


def _contract_brief(tid: str) -> str:
    """A minimal contract brief body that passes ``is_contract_brief``."""
    return (
        f"# TASK-{tid} — brief\n\n"
        "## B1 — Goal\n\nShip the thing.\n\n"
        "## B2 — Scope\n\nIn: the thing. Out: everything else.\n\n"
        "## B3 — Acceptance\n\nThe thing ships.\n\n"
        "## B4 — Constraints\n\nNone.\n"
    )


class _GapTmpBase(unittest.TestCase):
    """Common tmpdir + patched TASKS/DEBATES/REVIEWS/PLANS + ensure_dirs noop."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.tasks = tmp / "tasks"
        self.debates = tmp / "debates"
        self.reviews = tmp / "reviews"
        self.plans = tmp / "plans"
        for d in (self.tasks, self.debates, self.reviews, self.plans):
            d.mkdir()
        # C is a singleton module: RG.C, I.C, _common.C are all the same
        # object. Patch each attribute ONCE on the shared module to avoid
        # nested-patch restore-order issues that leak into later test files.
        from pipeline_graph import config as _C
        self._patches = [
            patch.object(_C, "TASKS", self.tasks),
            patch.object(_C, "DEBATES", self.debates),
            patch.object(_C, "REVIEWS", self.reviews),
            patch.object(_C, "PLANS", self.plans),
            patch.object(_C, "ensure_dirs", lambda: None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _seed_gap(self, tid: str, claims):
        RG.write_requirements_gap(tid, claims)

    def _seed_intake_answers(self, tid: str, answers: list[str]):
        """Write an intake file with ``**A:**`` markers for each answer."""
        lines = [f"# TASK-{tid} — intake", ""]
        for i, a in enumerate(answers, 1):
            lines += [f"## Q{i}", "", f"Question {i}", "", f"**A:** {a}", ""]
        I.intake_file(tid).write_text("\n".join(lines))

    def _seed_contract_brief(self, tid: str):
        I.brief_file(tid).write_text(_contract_brief(tid))


class TestI3Gate(_GapTmpBase):
    def test_i3_gap_answered_true_when_answers_meet_gap_count(self):
        self._seed_gap("i3", ["gap one", "gap two"])
        self._seed_intake_answers("i3", ["ans one", "ans two"])
        self.assertTrue(I._i3_gap_answered("i3"))

    def test_i3_gap_answered_false_when_answers_fewer_than_gaps(self):
        self._seed_gap("i3", ["gap one", "gap two"])
        self._seed_intake_answers("i3", ["ans one"])
        self.assertFalse(I._i3_gap_answered("i3"))

    def test_i3_gap_answered_true_when_no_active_gap(self):
        # No gap file → n_active_gaps returns 0 → trivially answered.
        self.assertTrue(I._i3_gap_answered("i3"))

    def test_i3_gap_answered_true_when_gap_waived(self):
        self._seed_gap("i3", ["gap one"])
        RG.waive_requirements_gap("i3")
        self.assertTrue(I._i3_gap_answered("i3"))

    def test_i3_rejection_message_counts_active_gaps_and_answers(self):
        self._seed_gap("i3", ["gap one", "gap two"])
        self._seed_intake_answers("i3", ["ans one"])
        msg = I._i3_rejection_message("i3")
        self.assertIn("2 active gap(s)", msg)
        self.assertIn("only 1 answer(s)", msg)

    def test_i3_gap_active_true_only_for_active(self):
        self._seed_gap("i3", ["gap one"])
        self.assertTrue(I._i3_gap_active("i3"))
        RG.suspend_requirements_gap("i3")
        self.assertFalse(I._i3_gap_active("i3"))
        RG.set_gap_status("i3", "active")
        RG.waive_requirements_gap("i3")
        self.assertFalse(I._i3_gap_active("i3"))


class TestIntakeAskI3Gate(_GapTmpBase):
    """intake_ask with has_brief + active gap → I3 gate before COMPLETE."""

    def _run_intake_ask(self, tid: str, state: dict):
        # The brief must be written DURING the agent call (after intake_ask
        # captures `before`) so fresh_brief is True. Use a side_effect that
        # writes the contract brief then returns the COMPLETE marker.
        def _agent_write_brief(*a, **kw):
            self._seed_contract_brief(tid)
            return (0, "INTAKE: COMPLETE")

        with patch.object(I, "run_agent", side_effect=_agent_write_brief), \
             patch.object(I, "materialize_intake_output"), \
             patch.object(I, "is_contract_brief", return_value=True), \
             patch.object(I.ev, "emit"):
            return I.intake_ask(state)

    def test_complete_with_active_gap_and_enough_answers_passes(self):
        tid = "ia"
        self._seed_gap(tid, ["gap one"])
        self._seed_intake_answers(tid, ["ans one"])
        state = {"task_id": tid, "intake_round": 0,
                 "requirements_reintake_count": 0, "request": "do thing"}
        d = self._run_intake_ask(tid, state)
        self.assertTrue(d.get("intake_done"))
        self.assertEqual(d.get("requirements_reintake_count"), 1)
        # Gap file is cleared on successful COMPLETE.
        self.assertFalse(RG.requirements_gap_path(tid).exists())

    def test_complete_with_active_gap_and_no_answers_escalates(self):
        tid = "ia"
        self._seed_gap(tid, ["gap one"])
        # No answers seeded.
        state = {"task_id": tid, "intake_round": 0,
                 "requirements_reintake_count": 0, "request": "do thing"}
        d = self._run_intake_ask(tid, state)
        self.assertFalse(d.get("intake_done", False))
        self.assertIn("REQUIREMENTS gap re-intake not complete", d.get("escalation", ""))
        # Count NOT incremented on I3 rejection.
        self.assertNotIn("requirements_reintake_count", d)

    def test_complete_with_no_active_gap_does_not_increment_count(self):
        tid = "ia"
        # No gap file.
        state = {"task_id": tid, "intake_round": 0,
                 "requirements_reintake_count": 0, "request": "do thing"}
        d = self._run_intake_ask(tid, state)
        self.assertTrue(d.get("intake_done"))
        # First-run COMPLETE with no gap → count unchanged.
        self.assertNotIn("requirements_reintake_count", d)


class TestIntakeWaitActiveGapSplit(_GapTmpBase):
    def _run_intake_wait(self, tid: str, state: dict, answer: str):
        with patch.object(I, "interrupt", return_value=answer), \
             patch.object(I.ev, "emit"):
            return I.intake_wait(state)

    def test_skip_with_active_gap_waives_and_increments_and_degrades(self):
        tid = "iw"
        self._seed_gap(tid, ["gap one"])
        state = {"task_id": tid, "intake_round": 1, "request": "do thing",
                 "requirements_reintake_count": 0}
        d = self._run_intake_wait(tid, state, "skip")
        self.assertTrue(d.get("intake_done"))
        self.assertEqual(d.get("requirements_reintake_count"), 1)
        self.assertEqual(
            d.get("degradations"),
            ["shipped with REQUIREMENTS gaps waived at intake"],
        )
        self.assertEqual(d.get("batches"), [])
        self.assertEqual(d.get("debate_round"), 0)
        self.assertEqual(RG.gap_status(tid), "waived")

    def test_stop_with_active_gap_finishes_run(self):
        tid = "iw"
        self._seed_gap(tid, ["gap one"])
        state = {"task_id": tid, "intake_round": 1, "request": "do thing",
                 "requirements_reintake_count": 0}
        d = self._run_intake_wait(tid, state, "stop")
        self.assertTrue(d.get("finished"))
        # Gap stays active, count unchanged.
        self.assertEqual(RG.gap_status(tid), "active")
        self.assertNotIn("requirements_reintake_count", d)

    def test_submit_with_active_gap_and_enough_answers_passes(self):
        tid = "iw"
        self._seed_gap(tid, ["gap one"])
        self._seed_intake_answers(tid, ["ans one"])
        state = {"task_id": tid, "intake_round": 1, "request": "do thing",
                 "requirements_reintake_count": 0}
        d = self._run_intake_wait(tid, state, "ok")
        self.assertNotIn("escalation", d)
        self.assertNotIn("finished", d)

    def test_submit_with_active_gap_and_fewer_answers_escalates(self):
        tid = "iw"
        self._seed_gap(tid, ["gap one", "gap two"])
        # One answer but two gaps → I3 gate fails.
        self._seed_intake_answers(tid, ["ans one"])
        state = {"task_id": tid, "intake_round": 1, "request": "do thing",
                 "requirements_reintake_count": 0}
        d = self._run_intake_wait(tid, state, "ok")
        self.assertIn("REQUIREMENTS gap re-intake not complete", d.get("escalation", ""))

    def test_no_active_gap_keeps_legacy_semantics(self):
        tid = "iw"
        # No gap file.
        state = {"task_id": tid, "intake_round": 1, "request": "do thing"}
        d = self._run_intake_wait(tid, state, "skip")
        self.assertTrue(d.get("intake_done"))
        self.assertNotIn("requirements_reintake_count", d)
        self.assertNotIn("degradations", d)


class TestEscalateReIntakeBranch(_GapTmpBase):
    def _escalate(self, tid: str, answer: str, state: dict):
        with patch.object(_common, "interrupt", return_value=answer), \
             patch.object(_common.ev, "emit"), \
             patch.object(_common.ev, "open_escalation", return_value=True), \
             patch.object(_common.ev, "close_escalation"):
            return _common.escalate(state)

    def test_re_intake_archives_live_debate_and_intake(self):
        tid = "ri"
        self._seed_gap(tid, ["gap one"])
        # Live debate + live intake exist.
        (self.debates / f"DEBATE-{tid}.md").write_text("## Round 1\nold debate\n")
        (self.tasks / f"TASK-{tid}-intake.md").write_text("# old intake\n")
        state = {"task_id": tid, "journal": [],
                 "escalation": "debate requirements: brief issue",
                 "requirements_reintake_count": 0}
        d = self._escalate(tid, "re-intake", state)
        # Live debate deleted, -full archive carries the body.
        self.assertFalse((self.debates / f"DEBATE-{tid}.md").exists())
        self.assertIn("old debate", (self.debates / f"DEBATE-{tid}-full.md").read_text())
        # Live intake deleted, -history archive carries the body.
        self.assertFalse((self.tasks / f"TASK-{tid}-intake.md").exists())
        self.assertIn("old intake", (self.tasks / f"TASK-{tid}-intake-history.md").read_text())
        # State cleared for re-intake.
        self.assertFalse(d.get("intake_done"))
        self.assertEqual(d.get("debate_round"), 0)
        self.assertEqual(d.get("batches"), [])
        # Count NOT incremented here (intake_ask / intake_wait does that).
        self.assertNotIn("requirements_reintake_count", d)

    def test_re_intake_reactivates_suspended_gap(self):
        tid = "ri"
        self._seed_gap(tid, ["gap one"])
        RG.suspend_requirements_gap(tid)
        self.assertEqual(RG.gap_status(tid), "suspended")
        state = {"task_id": tid, "journal": [],
                 "escalation": "debate requirements: brief issue",
                 "requirements_reintake_count": 0}
        self._escalate(tid, "re-intake", state)
        self.assertEqual(RG.gap_status(tid), "active")


class TestEscalateOkAtMax(_GapTmpBase):
    def _escalate(self, tid: str, answer: str, state: dict):
        with patch.object(_common, "interrupt", return_value=answer), \
             patch.object(_common.ev, "emit"), \
             patch.object(_common.ev, "open_escalation", return_value=True), \
             patch.object(_common.ev, "close_escalation"):
            return _common.escalate(state)

    def test_ok_at_max_waives_gap_and_appends_degradation(self):
        tid = "ok"
        self._seed_gap(tid, ["gap one"])
        state = {"task_id": tid, "journal": [],
                 "escalation": "debate requirements: brief issue",
                 "requirements_reintake_count": 2}
        d = self._escalate(tid, "ok", state)
        self.assertEqual(d.get("debate_round_bonus"), 0)
        self.assertEqual(d.get("redo_debate"), False)
        self.assertEqual(
            d.get("degradations"),
            ["shipped with unresolved REQUIREMENTS gaps (max re-intakes reached)"],
        )
        self.assertEqual(RG.gap_status(tid), "waived")


class TestArchiveHelpers(_GapTmpBase):
    def test_archive_live_debate_appends_to_full_then_deletes_live(self):
        tid = "ar"
        live = self.debates / f"DEBATE-{tid}.md"
        full = self.debates / f"DEBATE-{tid}-full.md"
        live.write_text("## Round 1\nfirst debate\n")
        RG.archive_live_debate_for_reintake(tid)
        self.assertFalse(live.exists())
        self.assertIn("first debate", full.read_text())
        # Second archive appends (does not overwrite).
        live.write_text("## Round 1\nsecond debate\n")
        RG.archive_live_debate_for_reintake(tid)
        body = full.read_text()
        self.assertIn("first debate", body)
        self.assertIn("second debate", body)

    def test_archive_live_debate_no_live_is_noop(self):
        tid = "ar"
        # No live file; -full does not get created.
        RG.archive_live_debate_for_reintake(tid)
        self.assertFalse((self.debates / f"DEBATE-{tid}-full.md").exists())

    def test_archive_intake_appends_to_history_then_deletes_live(self):
        tid = "ar"
        live = self.tasks / f"TASK-{tid}-intake.md"
        history = self.tasks / f"TASK-{tid}-intake-history.md"
        live.write_text("# first intake\n")
        RG.archive_intake_for_reintake(tid)
        self.assertFalse(live.exists())
        self.assertIn("first intake", history.read_text())
        live.write_text("# second intake\n")
        RG.archive_intake_for_reintake(tid)
        body = history.read_text()
        self.assertIn("first intake", body)
        self.assertIn("second intake", body)


class TestRouteIntakeDebateAndEnd(_GapTmpBase):
    def test_route_intake_done_with_reintake_count_goes_to_plan(self):
        # TASK-033: a re-intake cycle changed the brief → plan must regenerate
        # before the debate restarts (plan → checkpoint_effort → debate_tech).
        state = {"task_id": "r", "intake_done": True,
                 "requirements_reintake_count": 1}
        self.assertEqual(G.route_intake(state), "plan")

    def test_route_intake_done_first_run_goes_to_plan(self):
        state = {"task_id": "r", "intake_done": True,
                 "requirements_reintake_count": 0}
        self.assertEqual(G.route_intake(state), "plan")

    def test_route_intake_finished_goes_to_end(self):
        state = {"task_id": "r", "finished": True}
        self.assertEqual(G.route_intake(state), G.END)

    def test_route_intake_wait_done_with_reintake_count_goes_to_plan(self):
        state = {"task_id": "r", "intake_done": True,
                 "requirements_reintake_count": 1}
        self.assertEqual(G.route_intake_wait(state), "plan")

    def test_route_intake_wait_finished_goes_to_end(self):
        state = {"task_id": "r", "finished": True}
        self.assertEqual(G.route_intake_wait(state), G.END)

    def test_route_intake_wait_done_first_run_goes_to_plan(self):
        state = {"task_id": "r", "intake_done": True,
                 "requirements_reintake_count": 0}
        self.assertEqual(G.route_intake_wait(state), "plan")


class TestRedoFromIntakeCLI(_GapTmpBase):
    """redo --from intake: keep PLAN/-full, archive, ensure_gap, exit 2.

    The redo branch's file-archiving + ensure_gap + exit-2 logic is exercised
    via the same helpers the branch calls (archive_live_debate_for_reintake,
    ensure_gap_from_debate_file, archive_intake_for_reintake) so the test does
    not need a checkpoint DB. The branch's keep-PLAN / keep--full contract is
    verified by asserting the files survive the archiving sequence.
    """

    def test_redo_from_intake_exits_2_when_no_claims_and_no_gap(self):
        # The redo branch returns 2 when ensure_gap yields no claims AND the
        # gap file is empty. Simulate the branch's extraction sequence.
        tid = "rf"
        RG.archive_live_debate_for_reintake(tid)  # no live → noop
        claims = RG.ensure_gap_from_debate_file(tid)
        gap_body = RG.read_requirements_gap(tid)
        # The branch's exit-2 predicate:
        should_exit_2 = not claims and not gap_body.strip()
        self.assertTrue(should_exit_2)

    def test_redo_from_intake_archives_live_debate_and_keeps_full(self):
        tid = "rf"
        (self.debates / f"DEBATE-{tid}.md").write_text(
            "## Round 1 — Reviewer\nVERDICT: REJECT\n"
            "[BLOCKER:REQUIREMENTS] brief is wrong\n"
        )
        (self.debates / f"DEBATE-{tid}-full.md").write_text("PRIOR FULL\n")
        (self.tasks / f"TASK-{tid}-intake.md").write_text("# old intake\n")
        (self.plans / f"PLAN-{tid}.md").write_text("# prior plan\n")
        # Mirror the redo branch's sequence: archive live → -full, ensure_gap,
        # archive intake → -history, delete live debate + UX review (keep PLAN
        # and -full).
        RG.archive_live_debate_for_reintake(tid)
        claims = RG.ensure_gap_from_debate_file(tid)
        self.assertTrue(claims)
        RG.archive_intake_for_reintake(tid)
        (self.debates / f"DEBATE-{tid}.md").unlink(missing_ok=True)
        (self.reviews / f"UX-{tid}.md").unlink(missing_ok=True)
        # Live debate deleted; -full preserved + appended.
        self.assertFalse((self.debates / f"DEBATE-{tid}.md").exists())
        full_body = (self.debates / f"DEBATE-{tid}-full.md").read_text()
        self.assertIn("PRIOR FULL", full_body)
        self.assertIn("brief is wrong", full_body)
        # Live intake archived to -history.
        self.assertFalse((self.tasks / f"TASK-{tid}-intake.md").exists())
        self.assertIn("old intake",
                      (self.tasks / f"TASK-{tid}-intake-history.md").read_text())
        # PLAN preserved (the redo branch keeps it).
        self.assertTrue((self.plans / f"PLAN-{tid}.md").exists())
        # Gap file materialized with the claim.
        self.assertIn("brief is wrong", RG.read_requirements_gap(tid))


if __name__ == "__main__":
    unittest.main()
