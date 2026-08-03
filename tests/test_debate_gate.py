"""Debate-gate fixes from the TASK-010 run.

1. _latest_section must bound the current round's critic block at the NEXT
   header, not end-of-file — otherwise it swallows the proposer's reply, whose
   "[BLOCKER] … — RESOLVED" restatement double-counts a single blocker (the
   2-vs-1 miscount that escalated a one-blocker debate).
2. _debate_decision at the verification round must CONVERGE when zero blockers
   survived, even if the verdict is still APPROVE_WITH_CHANGES because a critic
   kept a non-blocking SUGGESTION open — not escalate on the verdict alone.
3. route_escalation_return sends a debate-cap escalation (empty batches,
   debate already ran) to summary→judge; the redo reset relies on this by
   clearing the previous run's batches so the judge regenerates FINAL/BATCHES.
"""
import unittest

from langgraph.graph import END

from pipeline_graph import nodes as N, agents as A
from pipeline_graph import graph as G
from pipeline_graph.nodes import debate as D


class OpenBlockerCount(unittest.TestCase):
    """A [BLOCKER] token counts as an OPEN blocker only under VERDICT: REJECT.

    Regression for the TASK-002 false escalation: rounds 2-3 downgraded the theme
    to SUGGESTION with APPROVE_WITH_CHANGES, but the count resurrected the Round 1
    REJECT's 2 blockers and escalated "2 technical blocker(s) confirmed".
    """
    REJECT_OUT = "VERDICT: REJECT\n[BLOCKER] a is wrong.\n[BLOCKER] b is wrong.\n"
    STALE_R1 = "VERDICT: REJECT\n[BLOCKER] a is wrong.\n[BLOCKER] b is wrong.\n"

    def test_reject_counts_blockers(self):
        self.assertEqual(D._open_blocker_count("REJECT", self.REJECT_OUT, ""), 2)

    def test_approve_with_changes_zeroes_blockers(self):
        # Same [BLOCKER] text, but the reviewer is shipping → not blocking.
        self.assertEqual(
            D._open_blocker_count("APPROVE_WITH_CHANGES", self.REJECT_OUT, ""), 0)

    def test_approve_zeroes_blockers(self):
        self.assertEqual(D._open_blocker_count("APPROVE", self.REJECT_OUT, ""), 0)

    def test_clean_round_does_not_resurrect_stale_reject(self):
        # Current round clean (0 blockers) + APPROVE_WITH_CHANGES must NOT fall
        # back to a stale earlier Reviewer section that still had blockers.
        clean_out = "VERDICT: APPROVE_WITH_CHANGES\n[SUGGESTION] minor thing.\n"
        self.assertEqual(
            D._open_blocker_count("APPROVE_WITH_CHANGES", clean_out, self.STALE_R1), 0)

    def test_reject_falls_back_to_latest_section_when_out_empty(self):
        # Recovery path preserved: a REJECT with unreadable `out` still counts
        # from the filed section.
        self.assertEqual(D._open_blocker_count("REJECT", "", self.STALE_R1), 2)

    def test_unknown_stays_conservative(self):
        # An unparseable verdict must not silently converge — still counts.
        self.assertEqual(D._open_blocker_count("UNKNOWN", self.REJECT_OUT, ""), 2)


DEBATE_OUT_OF_ORDER = """\
## Round 1 — Reviewer
VERDICT: APPROVE_WITH_CHANGES
[BLOCKER] first thing is wrong.

## Round 1 — Proposer
1. [BLOCKER] first thing — RESOLVED. Fixed it.

## Round 2 — Reviewer
VERDICT: APPROVE_WITH_CHANGES
[BLOCKER] second thing is wrong.

## Round 2 — Proposer
1. [BLOCKER] second thing — RESOLVED. Fixed it too.
"""


class LatestSectionBounding(unittest.TestCase):
    def test_reviewer_section_excludes_following_proposer_reply(self):
        section = N._latest_section(DEBATE_OUT_OF_ORDER, "Reviewer")
        # The last Reviewer block has exactly one [BLOCKER]; the proposer reply
        # that follows restates it as RESOLVED and must NOT be counted.
        self.assertEqual(A.count_blockers(section), 1)
        self.assertIn("second thing is wrong", section)
        self.assertNotIn("RESOLVED", section)

    def test_missing_critic_returns_empty(self):
        self.assertEqual(N._latest_section(DEBATE_OUT_OF_ORDER, "UX"), "")


class DebateVerificationConvergence(unittest.TestCase):
    def _decide(self, verdict, blockers, is_verification):
        state = {"has_ui": False, "reviewer_verdict": verdict,
                 "open_blockers": blockers}
        return N._debate_decision(state, is_verification=is_verification)

    def test_verification_zero_blockers_converges(self):
        out = self._decide("APPROVE_WITH_CHANGES", 0, is_verification=True)
        self.assertEqual(out["debate_next"], "summary")
        self.assertNotIn("escalation", out)

    def test_verification_with_blockers_escalates(self):
        out = self._decide("APPROVE_WITH_CHANGES", 2, is_verification=True)
        self.assertIn("escalation", out)
        self.assertIn("2", out["escalation"])

    def test_clean_approve_converges_without_verification(self):
        out = self._decide("APPROVE", 0, is_verification=False)
        self.assertEqual(out["debate_next"], "summary")
        self.assertNotIn("escalation", out)

    def test_open_blockers_midround_go_to_reply(self):
        out = self._decide("APPROVE_WITH_CHANGES", 1, is_verification=False)
        self.assertEqual(out["debate_next"], "reply")


class EscalationReturnToJudge(unittest.TestCase):
    def test_debate_cap_with_no_batches_returns_to_summary(self):
        # After the redo clears batches, a debate escalation resolved with "ok"
        # must route to summary (→ judge regenerates the decomposition).
        state = {"branch": "b", "intake_done": True, "batches": [],
                 "debate_round": 2}
        self.assertEqual(G.route_escalation_return(state), "summary")

    def test_stale_batches_would_skip_summary(self):
        # Documents the bug the redo reset fixes: with batches still present the
        # router goes to implement, NOT summary — so redo must clear them.
        state = {"branch": "b", "intake_done": True, "debate_round": 2,
                 "batches": [{"n": 1}], "batch_idx": 0}
        self.assertNotEqual(G.route_escalation_return(state), "summary")

    def test_finished_after_stop_routes_to_end(self):
        """stop sets finished=True; redo must clear it or resume ok goes to END."""
        state = {"finished": True, "branch": "b", "intake_done": True,
                 "batches": [], "debate_round": 6}
        self.assertEqual(G.route_escalation_return(state), END)

    def test_retry_judge_routes_to_judge(self):
        state = {"retry_judge": True, "branch": "b", "intake_done": True,
                 "batches": [], "debate_round": 6}
        self.assertEqual(G.route_escalation_return(state), "judge")


if __name__ == "__main__":
    unittest.main()
