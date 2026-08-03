"""Tests for `./run.py metrics` and the `pipeline_graph.metrics` module.

Covers the conformance checklist for batch 1 (FINAL-001):
  - aggregate_task: retries count agent_end/step_error with `-retry` in step
    (NOT step_end); failures count step_error/agent_unhealthy unconditionally.
  - resolve_status: latest of run_end/run_paused/escalation_open/run_stalled/
    run_start.
  - render_task_report / render_summary: required section headers present.
  - _metrics CLI: argument validation before empty-log check (returns 2 when
    neither task_id nor --all); empty log returns 0 with "no metrics recorded
    yet"; write failure still prints to stdout, errors to stderr, returns 1;
    monkeypatches ev.EVENTS_LOG directly.
"""
import io
import json
import os
import tempfile
import types
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from pipeline_graph import events as ev
from pipeline_graph import metrics as M
from pipeline_graph import config as C

import run as run_mod


def _ev(kind, task, step="", msg="", ts=None, **extra):
    """Build an event dict matching the shape `events.emit` writes."""
    if ts is None:
        ts = datetime.now(timezone.utc)
    rec = {"ts": ts.isoformat(), "kind": kind, "task": task,
           "step": step, "msg": msg, **extra}
    return rec


def _write_events(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r, default=str) + "\n")


# --- aggregate_task: retries ----------------------------------------------

class TestRetryCounts(unittest.TestCase):
    def test_retries_count_agent_end_with_retry_suffix(self):
        """agent_end events whose step contains '-retry' count as retries."""
        events = [
            _ev("run_start", "t1", step="start", msg="go"),
            _ev("agent_end", "t1", step="debate-retry1",
                msg="reviewer exit=0", agent="claude", role="REVIEWER",
                duration_ms=1000, health="ok"),
            _ev("agent_end", "t1", step="debate-retry2",
                msg="reviewer exit=0", agent="claude", role="REVIEWER",
                duration_ms=2000, health="ok"),
            _ev("agent_end", "t1", step="debate",
                msg="reviewer exit=0", agent="claude", role="REVIEWER",
                duration_ms=3000, health="ok"),
            _ev("run_end", "t1", step="wrap_up", msg="done"),
        ]
        m = M.aggregate_task(events, task_id="t1")
        self.assertEqual(m.retries, 2)

    def test_retries_count_step_error_during_retry(self):
        """A crash during a retry (step_error with -retry suffix) counts too."""
        events = [
            _ev("run_start", "t1", step="start"),
            _ev("step_error", "t1", step="visual-retry3",
                msg="ValueError: boom", ms=500),
            _ev("run_end", "t1", step="wrap_up"),
        ]
        m = M.aggregate_task(events, task_id="t1")
        self.assertEqual(m.retries, 1)

    def test_step_end_with_retry_suffix_does_not_count(self):
        """step_end never carries the marker in production (FINAL-001 ruling 1);
        even if a synthetic step_end had it, it must NOT be counted."""
        events = [
            _ev("run_start", "t1", step="start"),
            _ev("step_end", "t1", step="debate-retry1", msg="[ok] done", ms=1000),
            _ev("run_end", "t1", step="wrap_up"),
        ]
        m = M.aggregate_task(events, task_id="t1")
        self.assertEqual(m.retries, 0)

    def test_agent_unhealthy_does_not_count_as_retry(self):
        """The generic transient-retry loop emits agent_unhealthy with no step
        suffix; it is a failure, not a counted retry (plan scope limit)."""
        events = [
            _ev("run_start", "t1", step="start"),
            _ev("agent_unhealthy", "t1", step="implement",
                msg="transient failure", agent="claude", role="IMPLEMENTER",
                health="transient", signal="rate limit"),
            _ev("run_end", "t1", step="wrap_up"),
        ]
        m = M.aggregate_task(events, task_id="t1")
        self.assertEqual(m.retries, 0)
        self.assertEqual(m.failures, 1)


# --- aggregate_task: failures ---------------------------------------------

class TestFailureCounts(unittest.TestCase):
    def test_failures_count_step_error_and_agent_unhealthy(self):
        events = [
            _ev("run_start", "t1", step="start"),
            _ev("step_error", "t1", step="implement", msg="crash", ms=10),
            _ev("agent_unhealthy", "t1", step="debate", msg="bad output",
                agent="claude", role="REVIEWER", health="hard", signal="empty"),
            _ev("run_end", "t1", step="wrap_up"),
        ]
        m = M.aggregate_task(events, task_id="t1")
        self.assertEqual(m.failures, 2)
        self.assertEqual(len(m.failure_reasons), 2)

    def test_failures_unconditional_on_step_content(self):
        """failures counts step_error/agent_unhealthy regardless of step text."""
        events = [
            _ev("run_start", "t1", step="start"),
            _ev("step_error", "t1", step="visual-retry2", msg="boom", ms=10),
            _ev("run_end", "t1", step="wrap_up"),
        ]
        m = M.aggregate_task(events, task_id="t1")
        # The -retry suffix makes it a retry too, but it is still one failure.
        self.assertEqual(m.failures, 1)
        self.assertEqual(m.retries, 1)


# --- resolve_status -------------------------------------------------------

class TestResolveStatus(unittest.TestCase):
    def _seq(self, kinds):
        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        out = []
        for i, k in enumerate(kinds):
            out.append(_ev(k, "t1", step="s", msg="m",
                           ts=base + timedelta(seconds=i)))
        return out

    def test_finished(self):
        self.assertEqual(M.resolve_status(self._seq(["run_start", "run_end"])), "FINISHED")

    def test_paused_by_run_paused(self):
        self.assertEqual(M.resolve_status(self._seq(["run_start", "run_paused"])), "PAUSED")

    def test_paused_by_escalation_open(self):
        self.assertEqual(M.resolve_status(self._seq(["run_start", "escalation_open"])), "PAUSED")

    def test_stalled(self):
        self.assertEqual(M.resolve_status(self._seq(["run_start", "run_stalled"])), "STALLED")

    def test_running(self):
        self.assertEqual(M.resolve_status(self._seq(["run_start"])), "RUNNING")

    def test_unknown_when_empty(self):
        self.assertEqual(M.resolve_status([]), "UNKNOWN")

    def test_latest_wins(self):
        """run_end after run_paused -> FINISHED (latest by timestamp)."""
        evs = self._seq(["run_start", "run_paused", "run_end"])
        self.assertEqual(M.resolve_status(evs), "FINISHED")

    def test_escalation_then_pause_then_resume(self):
        """escalation_open (PAUSED) then run_start (RUNNING) -> RUNNING."""
        evs = self._seq(["escalation_open", "run_start"])
        self.assertEqual(M.resolve_status(evs), "RUNNING")


# --- render_task_report / render_summary headers --------------------------

class TestRenderHeaders(unittest.TestCase):
    def setUp(self):
        self.m = M.TaskMetrics(
            task_id="t1", wall_clock_s=120.0, active_s=60.0,
            per_node_ms={"debate": 30000, "plan": 10000},
            per_agent_ms={"claude": 40000}, failures=2, retries=1,
            escalations=1, degradations=1, status="FINISHED",
            failure_reasons=["boom", "bang"],
        )

    def test_task_report_has_seven_headers(self):
        out = M.render_task_report(self.m)
        for h in ("Total Duration", "Per-Node Durations", "Per-Agent Durations",
                  "Failure count", "Retry count", "Escalations", "Degradations"):
            self.assertIn(h, out, f"missing header: {h}")

    def test_summary_has_five_headers(self):
        out = M.render_summary({"t1": self.m})
        for h in ("Task List", "Completion Status",
                  "Average Duration per graph node",
                  "Most Common Failure Reasons",
                  "Agents ranked by total runtime"):
            self.assertIn(h, out, f"missing header: {h}")


# --- group_by_task --------------------------------------------------------

class TestGroupByTask(unittest.TestCase):
    def test_groups_preserve_order(self):
        events = [
            _ev("run_start", "a", step="start"),
            _ev("run_start", "b", step="start"),
            _ev("run_end", "a", step="wrap_up"),
            _ev("run_end", "b", step="wrap_up"),
        ]
        groups = M.group_by_task(events)
        self.assertEqual(sorted(groups), ["a", "b"])
        self.assertEqual([e["kind"] for e in groups["a"]], ["run_start", "run_end"])


# --- CLI: _metrics --------------------------------------------------------

class _Args:
    """Minimal stand-in for an argparse Namespace for _metrics."""
    def __init__(self, cmd="metrics", task_id=None, all=False):
        self.cmd = cmd
        self.task_id = task_id
        self.all = all


class TestCliMetrics(unittest.TestCase):
    def setUp(self):
        # Point ev.EVENTS_LOG at a temp file we control; restore on teardown.
        self._tmp = tempfile.TemporaryDirectory()
        self._log = Path(self._tmp.name) / "events.jsonl"
        self._orig_log = ev.EVENTS_LOG
        ev.EVENTS_LOG = self._log

    def tearDown(self):
        ev.EVENTS_LOG = self._orig_log
        self._tmp.cleanup()

    def test_missing_task_id_returns_2_with_no_log(self):
        """task_id=None, all=False, no events.jsonl -> return 2 (FINAL-001
        ruling 4: validate args before empty-log check)."""
        assert not self._log.exists()
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = run_mod._metrics(_Args(task_id=None, all=False))
        self.assertEqual(rc, 2)
        self.assertIn("usage", buf.getvalue().lower())

    def test_missing_task_id_returns_2_even_with_log(self):
        """Arg validation must come before empty-log check regardless of log."""
        _write_events(self._log, [_ev("run_start", "t1", step="start")])
        rc = run_mod._metrics(_Args(task_id=None, all=False))
        self.assertEqual(rc, 2)

    def test_empty_log_all_returns_0(self):
        rc = run_mod._metrics(_Args(all=True))
        self.assertEqual(rc, 0)

    def test_empty_log_single_task_returns_0(self):
        _write_events(self._log, [_ev("run_start", "other", step="start")])
        rc = run_mod._metrics(_Args(task_id="missing"))
        self.assertEqual(rc, 0)

    def test_single_task_report_prints_and_writes(self):
        events = [
            _ev("run_start", "t1", step="start"),
            _ev("step_end", "t1", step="debate", msg="[ok] done", ms=1500),
            _ev("agent_end", "t1", step="debate", msg="ok",
                agent="claude", role="REVIEWER", duration_ms=1200, health="ok"),
            _ev("run_end", "t1", step="wrap_up"),
        ]
        _write_events(self._log, events)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = run_mod._metrics(_Args(task_id="t1"))
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("## Total Duration", out)
        self.assertIn("## Per-Node Durations", out)
        report = C.METRICS / "report-t1.md"
        self.assertTrue(report.exists())
        self.assertIn("## Total Duration", report.read_text())

    def test_all_summary_prints_and_writes(self):
        events = [
            _ev("run_start", "t1", step="start"),
            _ev("run_end", "t1", step="wrap_up"),
            _ev("run_start", "t2", step="start"),
            _ev("run_end", "t2", step="wrap_up"),
        ]
        _write_events(self._log, events)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = run_mod._metrics(_Args(all=True))
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("## Task List", out)
        self.assertIn("## Agents ranked by total runtime", out)
        summary = C.METRICS / "summary.md"
        self.assertTrue(summary.exists())

    def test_write_failure_prints_to_stdout_errors_to_stderr_returns_1(self):
        """On OSError writing the report: stdout still has the report, stderr
        has the failing path, return code is 1 (FINAL-001 ruling 3 / item 22)."""
        events = [
            _ev("run_start", "t1", step="start"),
            _ev("run_end", "t1", step="wrap_up"),
        ]
        _write_events(self._log, events)

        out_buf = io.StringIO()
        err_buf = io.StringIO()
        # Force the write to fail by patching Path.write_text on the metrics
        # module's Path type to raise OSError.
        real_write_text = Path.write_text

        def boom(self, *a, **kw):
            raise OSError(13, "Permission denied", str(self))

        with patch.object(Path, "write_text", boom), \
                redirect_stdout(out_buf), redirect_stderr(err_buf):
            rc = run_mod._metrics(_Args(task_id="t1"))
        self.assertEqual(rc, 1)
        self.assertIn("## Total Duration", out_buf.getvalue())
        self.assertIn("report-t1.md", err_buf.getvalue())


# --- read_all_events ------------------------------------------------------

class TestReadAllEvents(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._log = Path(self._tmp.name) / "events.jsonl"
        self._orig = ev.EVENTS_LOG
        ev.EVENTS_LOG = self._log

    def tearDown(self):
        ev.EVENTS_LOG = self._orig
        self._tmp.cleanup()

    def test_returns_all_records_oldest_first(self):
        _write_events(self._log, [
            _ev("run_start", "a", step="start"),
            _ev("run_start", "b", step="start"),
            _ev("run_end", "a", step="wrap_up"),
        ])
        out = ev.read_all_events()
        self.assertEqual(len(out), 3)
        self.assertEqual([r["task"] for r in out], ["a", "b", "a"])

    def test_missing_log_returns_empty(self):
        self._log.unlink(missing_ok=True)
        self.assertEqual(ev.read_all_events(), [])

    def test_skips_malformed_lines(self):
        with self._log.open("w") as f:
            f.write(json.dumps(_ev("run_start", "a", step="start")) + "\n")
            f.write("not json\n")
            f.write(json.dumps(_ev("run_end", "a", step="wrap_up")) + "\n")
        out = ev.read_all_events()
        self.assertEqual(len(out), 2)


if __name__ == "__main__":
    unittest.main()
