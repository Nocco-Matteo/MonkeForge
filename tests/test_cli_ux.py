"""Tests for the F3 CLI council-log / live-run UX (FINAL-016 batch 3).

Covers the conformance checklist items 27–39:
  - No ``[?]`` placeholder in ``_drive`` output (the debug stream mode that
    printed ``[<node>] ...`` — and ``[?]`` when a debug chunk lacked a name —
    is gone; council-log rendering resolves role names via AGENT_IDENTITIES).
  - ``step_end`` return lines are deduped to one per node name: buffered by
    node name and emitted on the next ``step_start`` or at stream end.
  - Event-cursor baseline: historical events from earlier runs on the same
    task id are skipped — only events emitted by THIS drive render.
  - Unknown-role fallback: a node/role not in ``NODE_TO_ROLE`` /
    ``AGENT_IDENTITIES`` renders as the raw role string, never a bare ``?``.
"""
import argparse
import contextlib
import io
import json
import re
import tomllib
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline_graph import config as C, events as ev

import run as run_mod


# --- helpers ----------------------------------------------------------------

class _FakeSnap:
    """Minimal stand-in for a finished langgraph StateSnapshot."""
    created_at = "2026-01-01T00:00:00Z"
    next = []
    interrupts = []
    values = {}


class _FakeGraph:
    """A graph whose ``stream`` emits step_start/step_end events (via
    ``ev.emit``) around each yielded update, mirroring how the real
    instrumented nodes write events to events.jsonl mid-stream.

    ``steps`` is a list of ``(node, delta)`` tuples; for each, step_start and
    step_end events are emitted, then the update chunk is yielded.
    """

    def __init__(self, steps, task_id):
        self._steps = steps
        self._task_id = task_id

    def stream(self, payload, cfg, stream_mode=None):
        # Item 27: the driver must pass stream_mode=["updates"] (no "debug").
        assert stream_mode == ["updates"], (
            f"expected stream_mode=['updates'], got {stream_mode!r}")
        for node, delta in self._steps:
            ev.emit("step_start", self._task_id, node, f"starting {node}")
            ev.emit("step_end", self._task_id, node,
                    f"[ok] {node} done [1s]", outcome="ok")
            yield ("updates", {node: delta})

    def get_state(self, cfg):
        return _FakeSnap()


def _setup_env(monkeypatch, tmp_path):
    """Redirect every disk sink ``_drive`` / ``ev`` touches into tmp_path."""
    metrics = tmp_path / "metrics"
    metrics.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(C, "METRICS", metrics)
    monkeypatch.setattr(C, "RUNS_LOG", metrics / "runs.jsonl")
    monkeypatch.setattr(ev, "EVENTS_LOG", metrics / "events.jsonl")
    monkeypatch.setattr(ev, "PIPELINE_LOG", metrics / "pipeline.log")
    # No current.json → the working-line thread renders nothing during the
    # short-lived test drive (and is stopped in the driver's finally anyway).
    monkeypatch.setattr(run_mod, "_mark_idle", lambda *a, **k: None)
    return metrics


def _drive_capturing(graph, task_id, payload=None):
    """Run ``_drive`` and return its stdout text."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        run_mod._drive(graph, task_id, payload)
    return buf.getvalue()


# --- unknown-role fallback (item 32) ----------------------------------------

class TestUnknownRoleFallback:
    def test_unknown_node_falls_back_to_raw_role_string(self):
        # A node not in NODE_TO_ROLE → role = node name → not in
        # AGENT_IDENTITIES → display name = the raw role string (the node
        # name), never a bare "?".
        assert run_mod._role_display_name("totally_unknown_node") == \
            "totally_unknown_node"

    def test_known_node_renders_identity_name(self):
        # plan → PROPOSER → "Wise Orangutan" (from AGENT_IDENTITIES).
        assert run_mod._role_display_name("plan") == "Wise Orangutan"

    def test_role_name_for_role_falls_back_to_raw_role(self):
        assert run_mod._role_name_for_role("NO_SUCH_ROLE") == "NO_SUCH_ROLE"

    def test_no_identity_for_call_in_rendering(self):
        # Item 32: council-log rendering must not call
        # notify_daemon._identity_for. The rendering helpers must resolve names
        # via AGENT_IDENTITIES directly, with no call to _identity_for.
        import inspect
        src = inspect.getsource(run_mod._role_name_for_role) \
            + inspect.getsource(run_mod._role_display_name) \
            + inspect.getsource(run_mod._render_council_events)
        assert "_identity_for(" not in src
        assert "AGENT_IDENTITIES" in src


# --- no [?] output (item 28 / 39) -------------------------------------------

class TestNoQuestionMarkOutput:
    def test_drive_output_has_no_questionmark_placeholder(self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        graph = _FakeGraph(
            [("plan", {"journal": ["plan line"]}),
             ("implement", {"journal": ["impl line"]})],
            "tq",
        )
        out = _drive_capturing(graph, "tq")
        # The debug-stream "[<node>] ..." line (and its "[?]" fallback when a
        # debug chunk lacked a name) is gone; council-log lines use role names.
        assert "[?]" not in out
        # Role names render, not bare node names with a "?".
        assert "Wise Orangutan" in out  # plan → PROPOSER
        assert "Diligent Drill" in out  # implement → IMPLEMENTER


# --- step_end dedup to one return line per node (item 30 / 39) --------------

class TestStepEndDedup:
    def test_two_step_ends_for_one_node_emit_one_return_line(
            self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        # Emit two step_end events for the same node before the next
        # step_start: the buffer is keyed by node name, so the second
        # overwrites the first and only ONE return line is printed at stream
        # end.
        monkeypatch.setattr(run_mod, "_render_council_events",
                            lambda tid, cursor, buf: _emit_two_step_ends(
                                tid, cursor, buf))
        graph = _FakeGraph([("plan", {"journal": ["x"]})], "td")
        out = _drive_capturing(graph, "td")
        # Exactly one "[ok] plan done" return line (the second message wins).
        assert out.count("[ok] plan done") == 1
        assert "first-done" not in out
        assert "second-done" in out

    def test_return_line_emitted_on_next_step_start(self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        graph = _FakeGraph(
            [("plan", {"journal": ["p"]}),
             ("implement", {"journal": ["i"]})],
            "tn",
        )
        out = _drive_capturing(graph, "tn")
        # The plan return line must appear BEFORE the implement step_start
        # line (it is flushed when implement starts), not inline with plan's
        # own step_end event.
        plan_ret = out.index("[ok] plan done")
        impl_start = out.index("starting implement")
        assert plan_ret < impl_start


def _emit_two_step_ends(task_id, cursor, step_end_buffer):
    """Replacement for _render_council_events that writes a step_start, then
    TWO step_end events for the same node, so the dedup-by-node-name path is
    exercised directly."""
    ev.emit("step_start", task_id, "plan", "starting plan")
    ev.emit("step_end", task_id, "plan", "[ok] first-done [1s]", outcome="ok")
    ev.emit("step_end", task_id, "plan", "[ok] second-done [1s]", outcome="ok")
    # Mirror the real renderer: step_start flushes the buffer, step_end
    # buffers by node name (overwriting).
    step_end_buffer.clear()
    for e in ev.read_events(task_id)[cursor:]:
        if e.get("kind") == "step_start":
            for _n, line in step_end_buffer.items():
                print(line, flush=True)
            step_end_buffer.clear()
            print(f"  [{run_mod._role_display_name(e['step'])}] {e['msg']}",
                  flush=True)
        elif e.get("kind") == "step_end":
            step_end_buffer[e["step"]] = (
                f"  [{run_mod._role_display_name(e['step'])}] {e['msg']}")
    return len(ev.read_events(task_id))


# --- event-cursor baseline skips historical events (item 29 / 39) -----------

class TestEventCursorBaseline:
    def test_historical_events_are_not_rendered(self, monkeypatch, tmp_path):
        metrics = _setup_env(monkeypatch, tmp_path)
        # Pre-populate events.jsonl with HISTORICAL events for the same task
        # id — these must be below the baseline and never rendered.
        ev.emit("step_start", "tb", "old_node", "historical start")
        ev.emit("step_end", "tb", "old_node", "[ok] historical done [1s]",
                outcome="ok")
        baseline = len(ev.read_events("tb"))
        assert baseline == 2

        graph = _FakeGraph([("plan", {"journal": ["p"]})], "tb")
        out = _drive_capturing(graph, "tb")
        # The historical events are below the baseline → not rendered.
        assert "historical" not in out
        assert "old_node" not in out
        # The new drive's events ARE rendered.
        assert "starting plan" in out
        assert "[ok] plan done" in out


# ---------------------------------------------------------------------------
# FINAL-018 batch 1: --no-color / --version / help / stderr routing /
# resume guard / _drive exit codes
# ---------------------------------------------------------------------------

def _open_checkpointer_cm():
    """Shared callable returning a nullcontext, used everywhere
    ``run_mod.open_checkpointer`` is monkeypatched in the new test classes."""
    return contextlib.nullcontext(None)


class _FakeSnapNoState:
    """Snapshot with no created_at — simulates a task with no checkpoint."""
    created_at = None
    next = []
    interrupts = []
    values = {}


class _FakeSnapStall:
    """Snapshot with non-empty next and no interrupts — a stall."""
    created_at = "2026-01-01T00:00:00Z"
    next = ["stalled_node"]
    interrupts = []
    values = {}


class _FakeGraphNoState:
    """Graph whose ``get_state`` returns a snapshot with ``created_at=None``."""
    def stream(self, payload, cfg, stream_mode=None):
        yield from []

    def get_state(self, cfg):
        return _FakeSnapNoState()


class _FakeGraphStall:
    """Graph whose ``get_state`` returns a snapshot with non-empty ``next``
    and empty ``interrupts`` — a stall that ``_drive`` must report as such."""
    def stream(self, payload, cfg, stream_mode=None):
        assert stream_mode == ["updates"]
        yield from []

    def get_state(self, cfg):
        return _FakeSnapStall()


def _mock_side_effects(monkeypatch, *, mock_drive=False, mock_emit=True):
    """Mock run.py side-effect functions that must not fire during tests.

    Mocks ``_sleep_inhibitor``, ``open_checkpointer``, the notify/bot daemon
    starters, ``_warn_if_notifications_off``, ``_warn_stale_task_files``, and
    ``C.preflight``.  When ``mock_drive`` is True, ``_drive`` is replaced with
    a no-op returning 0.  When ``mock_emit`` is True, ``ev.emit`` is silenced.
    """
    monkeypatch.setattr(run_mod, "_sleep_inhibitor",
                        lambda: contextlib.nullcontext())
    monkeypatch.setattr(run_mod, "open_checkpointer", _open_checkpointer_cm)
    monkeypatch.setattr(run_mod, "_ensure_notify_daemon", lambda: None)
    monkeypatch.setattr(run_mod, "_ensure_bot", lambda: None)
    monkeypatch.setattr(run_mod, "_warn_if_notifications_off", lambda: None)
    monkeypatch.setattr(run_mod, "_warn_stale_task_files", lambda *a: None)
    monkeypatch.setattr(C, "preflight", lambda: [])
    if mock_drive:
        monkeypatch.setattr(run_mod, "_drive", lambda *a, **k: 0)
    if mock_emit:
        monkeypatch.setattr(ev, "emit", lambda *a, **k: None)


def _main_capturing(argv):
    """Run ``main(argv)`` and return ``(rc, stdout, stderr)``.

    Catches ``SystemExit`` (raised by ``--version``) and uses its code as rc.
    """
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = run_mod.main(argv)
        except SystemExit as e:
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


def _spy_parse_args(monkeypatch):
    """Spy on ``argparse.ArgumentParser.parse_args`` to capture the namespace."""
    captured = {}
    real = argparse.ArgumentParser.parse_args

    def spy(self, argv=None):
        ns = real(self, argv)
        captured["ns"] = ns
        return ns

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", spy)
    return captured


# --- --no-color before/after subcommand (items 31-32) ----------------------

class TestNoColorAfterSubcommand:
    def _setup(self, monkeypatch):
        _mock_side_effects(monkeypatch, mock_drive=True)
        monkeypatch.setattr(run_mod, "build_graph",
                            lambda *a, **k: _FakeGraphNoState())
        return _spy_parse_args(monkeypatch)

    def test_no_color_after_start(self, monkeypatch):
        captured = self._setup(monkeypatch)
        _main_capturing(["start", "001", "req", "--no-color"])
        assert getattr(captured["ns"], "no_color", False) is True

    def test_no_color_before_start(self, monkeypatch):
        captured = self._setup(monkeypatch)
        _main_capturing(["--no-color", "start", "001", "req"])
        assert getattr(captured["ns"], "no_color", False) is True

    def test_no_color_absent_without_flag(self, monkeypatch):
        captured = self._setup(monkeypatch)
        _main_capturing(["start", "001", "req"])
        assert not hasattr(captured["ns"], "no_color")


# --- --version flag (item 33) -----------------------------------------------

class TestVersionFlag:
    def test_version_flag_exits_zero_with_version(self):
        rc, out, _ = _main_capturing(["--version"])
        assert rc == 0
        assert re.match(r"monkeforge \d+\.\d+\.\d+", out.strip())

    def test_version_matches_pyproject(self):
        with open(run_mod._MF_ROOT / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        expected = data["project"]["version"]
        rc, out, _ = _main_capturing(["--version"])
        assert rc == 0
        assert expected in out


# --- help subcommand (items 34-35) ------------------------------------------

class TestHelpSubcommand:
    def test_help_no_topic_prints_examples(self):
        rc, out, err = _main_capturing(["help"])
        assert rc == 0
        assert "MonkeForge pipeline CLI — examples:" in out
        assert run_mod._SUPPORT_URL in out
        assert "https://github.com/Nocco-Matteo/MonkeForge" in out

    def test_help_known_topic_prints_subparser_help(self):
        rc, out, err = _main_capturing(["help", "resume"])
        assert rc == 0
        assert "usage:" in out
        assert "resume" in out

    def test_help_unknown_topic_returns_2(self):
        rc, out, err = _main_capturing(["help", "bogus"])
        assert rc == 2
        assert "unknown help topic" in err
        assert "bogus" in err


# --- errors routed to stderr (items 36-38) ----------------------------------

class TestErrorsToStderr:
    def test_metrics_no_arg_error_on_stderr(self):
        rc, out, err = _main_capturing(["metrics"])
        assert rc == 2
        assert "usage" in err
        assert "provide a task id" in err
        assert "usage" not in out

    def test_start_no_request_error_on_stderr(self, monkeypatch):
        _mock_side_effects(monkeypatch, mock_drive=True)
        rc, out, err = _main_capturing(["start", "001"])
        assert rc == 2
        assert "provide a request" in err
        assert "provide a request" not in out

    def test_redo_unknown_task_error_on_stderr(self, monkeypatch):
        _mock_side_effects(monkeypatch, mock_drive=True)
        monkeypatch.setattr(C, "CHECKPOINT_DB", Path(__file__))
        monkeypatch.setattr(run_mod, "build_graph", lambda cp: _FakeGraphNoState())
        rc, out, err = _main_capturing(["redo", "999"])
        assert rc == 1
        assert "no run found for this task" in err
        assert "no run found for this task" not in out


# --- resume unknown task guard (item 39) ------------------------------------

class TestResumeUnknownTask:
    def test_resume_unknown_task_no_traceback(self, monkeypatch):
        _mock_side_effects(monkeypatch, mock_drive=True)
        monkeypatch.setattr(C, "CHECKPOINT_DB", Path(__file__))
        monkeypatch.setattr(run_mod, "build_graph", lambda cp: _FakeGraphNoState())
        rc, out, err = _main_capturing(["resume", "999"])
        assert rc == 1
        assert "no run found for task 999" in err
        assert "EmptyInputError" not in out
        assert "=== FINISHED ===" not in out


# --- finished/stalled exit codes (item 40) ----------------------------------

class TestFinishedRunExitZero:
    def test_finished_run_returns_zero(self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        _mock_side_effects(monkeypatch, mock_drive=False, mock_emit=False)
        graph = _FakeGraph([("plan", {"journal": ["p"]})], "tf")
        monkeypatch.setattr(run_mod, "build_graph", lambda cp: graph)
        rc, out, err = _main_capturing(["start", "001", "test request"])
        assert rc == 0
        assert "=== FINISHED ===" in out

    def test_stalled_run_returns_nonzero(self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        _mock_side_effects(monkeypatch, mock_drive=False, mock_emit=False)
        graph = _FakeGraphStall()
        monkeypatch.setattr(run_mod, "build_graph", lambda cp: graph)
        rc, out, err = _main_capturing(["start", "001", "test request"])
        assert rc == 1
