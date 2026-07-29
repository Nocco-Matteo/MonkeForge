"""A subprocess killed by a signal (Popen returncode < 0) is an INFRASTRUCTURE
death, not the agent failing on its merits: SIGPIPE (-13) when the agent daemon
crashes mid-call, SIGKILL (-9) on OOM, SIGTERM (-15) on suspend. This is exactly
what took down TASK-010 batch 1 — the Devin IDE was OOM-killed, its CLI lost the
daemon and died with -13 producing zero output, and the implement node burned its
whole fix budget re-prompting a dead process before escalating with a misleading
"implementer failed" message.

Two guarantees:
1. classify_output maps a negative code to 'transient' (so run_agent retries it
   with backoff instead of giving up), while a positive non-zero stays 'hard'.
2. The implement node, once run_agent's transient retries are exhausted and the
   code is still negative, escalates as infrastructure WITHOUT spending a
   test-fix cycle (no fix prompt makes sense for a process that produced nothing).
"""
import unittest
from unittest.mock import patch

from pipeline_graph import agents as A, nodes as N, config as C


class ClassifySignalDeath(unittest.TestCase):
    def test_sigpipe_is_transient(self):
        health, sig = A.classify_output(-13, "")
        self.assertEqual(health, "transient")
        self.assertIn("13", sig)

    def test_sigkill_oom_is_transient(self):
        self.assertEqual(A.classify_output(-9, "")[0], "transient")

    def test_sigterm_suspend_is_transient(self):
        self.assertEqual(A.classify_output(-15, "")[0], "transient")

    def test_positive_nonzero_stays_hard(self):
        # A clean non-zero exit is a real failure, not a signal death.
        self.assertEqual(A.classify_output(1, "boom")[0], "hard")

    def test_clean_zero_with_output_is_ok(self):
        self.assertEqual(A.classify_output(0, "VERDICT: APPROVE\n" * 4)[0], "ok")

    def test_fatal_message_wins_over_signal(self):
        # A daemon that printed a fatal signature before being signalled is
        # classified by the message (no point retrying "out of usage").
        health, sig = A.classify_output(-13, "You're out of usage. Increase limits")
        self.assertEqual(health, "hard")


class ImplementEscalatesOnSignalDeath(unittest.TestCase):
    def _state(self, attempt=0):
        return {"task_id": "sigtest", "batch_idx": 0, "test_fix_attempt": attempt,
                "batches": [{"n": 1, "scope": "s", "checklist": []}]}

    def test_signal_death_escalates_as_infra(self):
        with patch.object(C, "DRY_RUN", True), \
             patch.object(N, "run_agent", return_value=(-13, "")), \
             patch.object(N.ev, "emit"):
            out = N.implement(self._state())
        self.assertIn("escalation", out)
        self.assertIn("killed by signal 13", out["escalation"])
        self.assertIn("infrastructure", out["escalation"].lower())

    def test_signal_death_does_not_spend_a_fix_cycle(self):
        # Even mid-way through the fix budget, an infra death escalates instead of
        # advancing test_fix_attempt (which would burn the budget on a dead agent).
        with patch.object(C, "DRY_RUN", True), \
             patch.object(N, "run_agent", return_value=(-9, "")), \
             patch.object(N.ev, "emit"):
            out = N.implement(self._state(attempt=1))
        self.assertIn("escalation", out)
        self.assertNotIn("test_fix_attempt", out)

    def test_positive_exit_still_uses_fix_cycle(self):
        # A normal non-zero exit keeps the retry-with-fix behaviour.
        with patch.object(C, "DRY_RUN", True), \
             patch.object(N, "run_agent", return_value=(1, "some output")), \
             patch.object(N.ev, "emit"):
            out = N.implement(self._state(attempt=0))
        self.assertNotIn("escalation", out)
        self.assertEqual(out.get("test_fix_attempt"), 1)


if __name__ == "__main__":
    unittest.main()
