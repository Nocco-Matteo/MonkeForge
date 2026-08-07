"""Tests for the F3 CLI council-log / live-run UX (FINAL-016 batch 3, extended
by FINAL-021 batch 1).

Covers the conformance checklist items 27–39 (FINAL-016) plus the FINAL-021
in-place progress / section-header / colour-gate / synchronous-step-hook /
crash-safe-shutdown / sanitisation coverage:
  - No ``[?]`` placeholder in ``_drive`` output (the debug stream mode that
    printed ``[<node>] ...`` — and ``[?]`` when a debug chunk lacked a name —
    is gone; council-log rendering resolves role names via AGENT_IDENTITIES).
  - ``step_end`` return lines: manual events without ``outcome`` are dropped
    and nested ``step_start`` re-entry is suppressed (D8) — one dispatch +
    one return per instrumented outer step.
  - Event-cursor baseline: historical events from earlier runs on the same
    task id are skipped — only events emitted by THIS drive render.
  - Unknown-role fallback: a node/role not in ``NODE_TO_ROLE`` /
    ``AGENT_IDENTITIES`` renders as the raw role string, never a bare ``?``.
  - Council-log / progress / pause chrome / ``=== FINISHED ===`` / stall line
    all print to ``sys.stderr`` (C1); machine output stays on stdout (C2).
  - TTY stderr ⇒ one in-place ``\\r`` progress line (C3); non-TTY ⇒ no ``\\r``
    and no ANSI ESC (C4/C5).
  - Colour gated on stderr TTY / ``NO_COLOR`` (presence) / ``--no-color`` /
    ``TERM=dumb`` (C5/D3); every human string sanitised when colour off / non-
    TTY (D10/Delta A).
  - Coarse-phase section headers + blank lines (C7/D4).
  - Synchronous step-event hook: dispatch before the agent blocks (C8/D9) and
    return for N before dispatch for N+1 (C6/D9); cursor-efficient tail read
    (D13/Delta B).
  - Crash-safe shutdown: clear progress, human one-liner + path, no traceback,
    exit 2, pipe-safe against ``BrokenPipeError``/``OSError``/``ValueError``
    (C10/D11).
"""
import argparse
import contextlib
import io
import json
import os
import re
import sys
import time
import tomllib
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline_graph import config as C, events as ev
from pipeline_graph import test_runner as tr

import run as run_mod


# --- helpers ----------------------------------------------------------------

class _TtyStringIO(io.StringIO):
    """A ``StringIO`` whose ``isatty()`` is configurable per-instance.

    Tests set ``err._tty = True/False`` on the instance that ``redirect_stderr``
    actually installs — never ``monkeypatch.setattr(sys.stderr, "isatty", ...)``,
    which would patch the original object that ``redirect_stderr`` has already
    swapped out. ``_use_color(..., stream=sys.stderr)`` then reads the right
    flag because ``sys.stderr`` IS this instance for the duration of the drive.
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._tty = False

    def isatty(self):
        return self._tty


class _FakeSnap:
    """Minimal stand-in for a finished langgraph StateSnapshot."""
    created_at = "2026-01-01T00:00:00Z"
    next = []
    interrupts = []
    values = {}


class _FakeSnapPause:
    """Snapshot with a pending interrupt — ``_drive`` renders the pause block."""
    created_at = "2026-01-01T00:00:00Z"
    next = []
    values = {}

    def __init__(self, data):
        self.interrupts = [_Interrupt(data)]


class _Interrupt:
    def __init__(self, value):
        self.value = value


class _FakeGraph:
    """A graph whose ``stream`` emits step_start/step_end events (via
    ``ev.emit``) around each yielded update, mirroring how the real
    instrumented nodes write events to events.jsonl mid-stream.

    ``steps`` is a list of ``(node, delta)`` tuples; for each, step_start and
    step_end events are emitted, then the update chunk is yielded. The
    synchronous step-event hook (D9) renders dispatch/return as the events
    are emitted, before the chunk yields.
    """

    def __init__(self, steps, task_id):
        self._steps = steps
        self._task_id = task_id

    def stream(self, payload, cfg, stream_mode=None):
        # The driver must pass stream_mode=["updates"] (no "debug").
        assert stream_mode == ["updates"], (
            f"expected stream_mode=['updates'], got {stream_mode!r}")
        for node, delta in self._steps:
            ev.emit("step_start", self._task_id, node, f"starting {node}")
            ev.emit("step_end", self._task_id, node,
                    f"[ok] {node} done [1s]", outcome="ok")
            yield ("updates", {node: delta})

    def get_state(self, cfg):
        return _FakeSnap()


class _FakeGraphEvents:
    """Graph that emits an arbitrary sequence of step events (in order) then
    yields one update chunk. Each event is a ``(kind, node, msg, extra)``
    tuple; ``extra`` is a dict of extra emit fields (e.g. ``outcome``). Used
    to exercise D8 nested/manual events and D6/D10 rendering paths directly.
    """

    def __init__(self, events, task_id, delta=None, snap=None):
        self._events = events
        self._task_id = task_id
        self._delta = delta if delta is not None else {"journal": ["x"]}
        self._snap = snap if snap is not None else _FakeSnap()

    def stream(self, payload, cfg, stream_mode=None):
        assert stream_mode == ["updates"]
        for kind, node, msg, extra in self._events:
            ev.emit(kind, self._task_id, node, msg, **extra)
        yield ("updates", {"plan": self._delta})

    def get_state(self, cfg):
        return self._snap


class _FakeGraphSlow:
    """Graph that writes a ``current.json`` heartbeat, emits ``step_start``,
    sleeps (simulating the agent blocking), then emits ``step_end`` and marks
    the phase ``agent done``. Drives the UI thread's in-place progress line."""

    def __init__(self, task_id, sleep=0.6):
        self._task_id = task_id
        self._sleep = sleep

    def stream(self, payload, cfg, stream_mode=None):
        assert stream_mode == ["updates"]
        cur = {"task": self._task_id, "phase": "running", "role": "PROPOSER",
               "step": "plan",
               "started": datetime.now(timezone.utc).isoformat(),
               "pid": os.getpid()}
        (C.METRICS / "current.json").write_text(json.dumps(cur))
        ev.emit("step_start", self._task_id, "plan", "starting plan")
        time.sleep(self._sleep)  # the agent blocks; UI thread refreshes \r
        ev.emit("step_end", self._task_id, "plan", "[ok] plan done [1s]",
                outcome="ok")
        (C.METRICS / "current.json").write_text(
            json.dumps({"task": self._task_id, "phase": "agent done"}))
        yield ("updates", {"plan": {"journal": ["p"]}})

    def get_state(self, cfg):
        return _FakeSnap()


class _FakeGraphCrash:
    """Graph whose ``stream`` raises — exercises the driver crash path (D11)."""

    def __init__(self, exc, task_id):
        self._exc = exc
        self._task_id = task_id

    def stream(self, payload, cfg, stream_mode=None):
        assert stream_mode == ["updates"]
        raise self._exc

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
    # No current.json → the UI thread renders no progress line during the
    # short-lived test drive (and is stopped in the driver's finally anyway).
    monkeypatch.setattr(run_mod, "_mark_idle", lambda *a, **k: None)
    # Ensure no leaked step hook from a prior test interferes with this one.
    ev.set_step_hook(None)
    return metrics


def _drive_capturing(graph, task_id, payload=None, args=None, tty=False):
    """Run ``_drive`` and return its stderr text.

    ``tty`` sets the ``isatty()`` flag of the stderr capture buffer so colour
    / in-place-progress gating can be simulated per test. Council-log
    narration, progress, pause chrome, ``=== FINISHED ===`` and the stall line
    all print to stderr (C1)."""
    err = _TtyStringIO()
    err._tty = tty
    with redirect_stderr(err):
        run_mod._drive(graph, task_id, payload, args=args)
    return err.getvalue()


def _drive_capturing_both(graph, task_id, payload=None, args=None, tty=False):
    """Run ``_drive`` and return ``(stdout, stderr)`` using ``_TtyStringIO``
    for stderr — used where a test must assert stdout stays clean (C2)."""
    out = io.StringIO()
    err = _TtyStringIO()
    err._tty = tty
    with redirect_stdout(out), redirect_stderr(err):
        run_mod._drive(graph, task_id, payload, args=args)
    return out.getvalue(), err.getvalue()


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
        # Council-log rendering must not call notify_daemon._identity_for. The
        # rendering helpers must resolve names via AGENT_IDENTITIES directly,
        # with no call to _identity_for. The FINAL-021 renderer helpers
        # (_emit_dispatch / _emit_return / _drain_events) replace the old
        # _render_council_events and are inspected here.
        import inspect
        src = (inspect.getsource(run_mod._role_name_for_role)
               + inspect.getsource(run_mod._role_display_name)
               + inspect.getsource(run_mod._emit_dispatch)
               + inspect.getsource(run_mod._emit_return)
               + inspect.getsource(run_mod._drain_events))
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
        err = _drive_capturing(graph, "tq")
        # The debug-stream "[<node>] ..." line (and its "[?]" fallback when a
        # debug chunk lacked a name) is gone; council-log lines use role names.
        assert "[?]" not in err
        # Role names render, not bare node names with a "?".
        assert "Wise Orangutan" in err  # plan → PROPOSER
        assert "Diligent Drill" in err  # implement → IMPLEMENTER


# --- step_end dedup: manual events without outcome dropped (D8) -------------

class TestStepEndDedup:
    def test_manual_step_end_without_outcome_is_dropped(
            self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        # A manual step_end WITHOUT outcome (finalize.py:273-288 auto-fix shape)
        # must NOT render a return line — only an outcome-bearing step_end does
        # (D8). The instrument always sets outcome; manual emits do not.
        events = [
            ("step_start", "plan", "starting plan", {}),
            ("step_end", "plan", "manual auto-fix attempt", {}),  # no outcome
            ("step_end", "plan", "[ok] plan done [1s]", {"outcome": "ok"}),
        ]
        graph = _FakeGraphEvents(events, "td")
        err = _drive_capturing(graph, "td")
        # Exactly one return line for plan (the outcome-bearing one); the
        # manual "auto-fix attempt" line is silently dropped.
        assert err.count("plan done") == 1
        assert "auto-fix attempt" not in err

    def test_return_line_emitted_before_next_dispatch(self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        graph = _FakeGraph(
            [("plan", {"journal": ["p"]}),
             ("implement", {"journal": ["i"]})],
            "tn",
        )
        err = _drive_capturing(graph, "tn")
        # The plan return line must appear BEFORE the implement dispatch line
        # — the synchronous step-event hook drains events.jsonl in order, so
        # step_end for plan (rendered when emitted) precedes step_start for
        # implement (C6/D9).
        plan_ret = err.index("plan done")
        impl_start = err.index("starting implement")
        assert plan_ret < impl_start


# --- event-cursor baseline skips historical events (item 29 / 39) -----------

class TestEventCursorBaseline:
    def test_historical_events_are_not_rendered(self, monkeypatch, tmp_path):
        metrics = _setup_env(monkeypatch, tmp_path)
        # Pre-populate events.jsonl with HISTORICAL events for the same task
        # id — these must be below the byte-offset baseline and never rendered.
        ev.emit("step_start", "tb", "old_node", "historical start")
        ev.emit("step_end", "tb", "old_node", "[ok] historical done [1s]",
                outcome="ok")
        baseline = len(ev.read_events("tb"))
        assert baseline == 2

        graph = _FakeGraph([("plan", {"journal": ["p"]})], "tb")
        err = _drive_capturing(graph, "tb")
        # The historical events are below the baseline → not rendered.
        assert "historical" not in err
        assert "old_node" not in err
        # The new drive's events ARE rendered.
        assert "starting plan" in err
        assert "plan done" in err


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

    Isolation is reported as already done: these are CLI-behaviour tests, and
    the real ``start`` path would otherwise create a worktree and a branch in
    the developer's own repo. That made ``start <id>`` tests pass on a fresh
    clone and fail on any machine that had ever run task <id> ("branch
    collision: feature/task-001 already exists" → rc 2, before the assertions
    under test). ``prepare_task_isolation`` itself is covered by
    ``tests/test_run_isolation.py`` against a throwaway target repo.
    """
    monkeypatch.setattr(run_mod._WT, "already_isolated", lambda: True)
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


def _main_capturing(argv, tty=False):
    """Run ``main(argv)`` and return ``(rc, stdout, stderr)``.

    Catches ``SystemExit`` (raised by ``--version``) and uses its code as rc.
    ``stderr`` is a ``_TtyStringIO`` so the colour / in-place-progress gate can
    be simulated; it defaults to non-TTY (matching a piped/redirected stderr).
    """
    out = io.StringIO()
    err = _TtyStringIO()
    err._tty = tty
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
        # === FINISHED === is a council-log permanent line → stderr (C1).
        assert "=== FINISHED ===" in err
        assert "=== FINISHED ===" not in out

    def test_stalled_run_returns_nonzero(self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        _mock_side_effects(monkeypatch, mock_drive=False, mock_emit=False)
        graph = _FakeGraphStall()
        monkeypatch.setattr(run_mod, "build_graph", lambda cp: graph)
        rc, out, err = _main_capturing(["start", "001", "test request"])
        assert rc == 1
        # The stall line is a council-log permanent line → stderr (C1).
        assert "stopped at:" in err


# ---------------------------------------------------------------------------
# FINAL-021 batch 1: in-place progress, section headers, colour gate,
# synchronous step-event hook, crash-safe shutdown, sanitisation.
# ---------------------------------------------------------------------------

def _no_color_env(monkeypatch):
    """Clear every colour-disabling env var so a TTY-sim test gets colour on."""
    for k in ("NO_COLOR",):
        monkeypatch.delenv(k, raising=False)
    if os.environ.get("TERM") == "dumb":
        monkeypatch.setenv("TERM", "xterm")


# --- F1: in-place progress on TTY (C3) --------------------------------------

class TestInProgress:
    def test_tty_stderr_shows_inplace_progress_line(self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        _no_color_env(monkeypatch)
        graph = _FakeGraphSlow("tp", sleep=0.6)
        err = _drive_capturing(graph, "tp", tty=True)
        # TTY ⇒ the UI thread refreshes ONE line in place via \r (C3).
        assert "\r" in err
        # Not N distinct elapsed lines: the progress is in-place, so the
        # newline count stays small (dispatch + return + header — no per-tick
        # newlines). The progress text shares one prefix across \r refreshes.
        segments = [s for s in err.split("\r") if "· plan ·" in s and "s" in s]
        assert segments, "expected at least one in-place progress segment"
        # Task id leads the progress line (A UX) and stays stable across \r ticks.
        import re as _re
        plain = [_re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s) for s in segments]
        assert all("tp ·" in s for s in plain), plain[:2]
        prefixes = {seg.rsplit("·", 1)[0] for seg in plain}
        assert len(prefixes) == 1, (
            f"expected one progress prefix, got {prefixes!r}")


class TestProgressNonTty:
    def test_non_tty_stderr_has_no_carriage_return_or_ansi(
            self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        _no_color_env(monkeypatch)
        graph = _FakeGraphSlow("tn", sleep=0.6)
        err = _drive_capturing(graph, "tn", tty=False)
        # Non-TTY ⇒ no \r animation and no ANSI ESC (C4/C5).
        assert "\r" not in err
        assert "\x1b" not in err
        # No repeated elapsed working lines — at most the dispatch + return.
        assert err.count("· plan ·") <= 1


# --- F2: section headers + dispatch/return ordering (C6/C7/D4/D8/D9) --------

class TestSectionHeaders:
    def test_section_headers_in_order_with_blank_line_before(
            self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        events = [
            ("step_start", "intake_ask", "starting intake", {}),
            ("step_end", "intake_ask", "[ok] intake done [1s]",
             {"outcome": "ok"}),
            ("step_start", "plan", "starting plan", {}),
            ("step_end", "plan", "[ok] plan done [1s]", {"outcome": "ok"}),
            ("step_start", "debate_tech", "starting debate", {}),
            ("step_end", "debate_tech", "[ok] debate done [1s]",
             {"outcome": "ok"}),
            ("step_start", "implement", "starting implement", {}),
            # Nested test-baseline events (implement.py:21-27 shape): a manual
            # step_start for the SAME node (suppressed per D8) and a manual
            # step_end WITHOUT outcome (dropped per D8).
            ("step_start", "implement", "test baseline batch 1", {}),
            ("step_end", "implement", "test baseline captured", {}),
            ("step_end", "implement", "[ok] implement done [1s]",
             {"outcome": "ok"}),
        ]
        graph = _FakeGraphEvents(events, "ts")
        err = _drive_capturing(graph, "ts")
        # Section headers appear in order, each preceded by a blank line (C7).
        # Task id is baked in so parallel terminals stay identifiable (A/C UX).
        for header in ("── ts · intake ──", "── ts · plan ──",
                       "── ts · debate ──", "── ts · implement ──"):
            assert f"\n{header}\n" in err, f"missing blank-line + header {header!r}"
        last = -1
        for header in ("── ts · intake ──", "── ts · plan ──",
                       "── ts · debate ──", "── ts · implement ──"):
            idx = err.index(header)
            assert idx > last, f"header {header!r} out of order"
            last = idx
        # Return for N precedes dispatch for N+1 (C6/D9).
        assert err.index("plan done") < err.index("starting debate")
        assert err.index("debate done") < err.index("starting implement")
        # D8: exactly ONE dispatch + ONE return for implement (the nested
        # manual pair is suppressed/dropped).
        assert err.count("starting implement") == 1
        assert err.count("implement done") == 1
        assert "test baseline" not in err

    def test_blank_line_between_different_monkes(self, monkeypatch, tmp_path):
        """Within a phase, a blank line separates Drill → Vervet blocks."""
        _setup_env(monkeypatch, tmp_path)
        events = [
            ("step_start", "implement", "batch 1/1", {}),
            ("step_end", "implement", "[ok] impl done [1s]", {"outcome": "ok"}),
            ("step_start", "code_review", "batch 1/1", {}),
            ("step_end", "code_review", "[ok] cr done [1s]", {"outcome": "ok"}),
        ]
        graph = _FakeGraphEvents(events, "tm")
        err = _drive_capturing(graph, "tm")
        # Drill return, blank line, Vervet dispatch.
        drill = "Diligent Drill"
        vervet = "Vigilant Vervet"
        assert drill in err and vervet in err
        # After the Drill return line there is a blank line before Vervet.
        assert f"impl done [1s]\n\n  {vervet}" in err or \
               f"impl done [1s]\n\n  \x1b[" in err  # colour-off path preferred
        # Colour is off in _drive_capturing by default — plain form:
        assert "impl done [1s]\n\n  Vigilant Vervet" in err

    def test_verdict_phase_after_debate(self, monkeypatch, tmp_path):
        """summary/judge/checkpoint_plan sit under ``── verdict ──``, not debate."""
        _setup_env(monkeypatch, tmp_path)
        events = [
            ("step_start", "debate_tech", "debate round 1", {}),
            ("step_end", "debate_tech", "[ok] debate done [1s]", {"outcome": "ok"}),
            ("step_start", "summary", "starting summary", {}),
            ("step_end", "summary", "[ok] summary done [1s]", {"outcome": "ok"}),
            ("step_start", "judge", "starting judge", {}),
            ("step_end", "judge", "[ok] judge done [1s]", {"outcome": "ok"}),
            ("step_start", "checkpoint_plan", "plan approved?", {}),
            ("step_end", "checkpoint_plan", "[ok] approved [0s]", {"outcome": "ok"}),
        ]
        graph = _FakeGraphEvents(events, "tv")
        err = _drive_capturing(graph, "tv")
        assert "── tv · debate ──" in err
        assert "── tv · verdict ──" in err
        assert err.index("── tv · debate ──") < err.index("── tv · verdict ──")
        # summary must not appear under a second debate header after verdict.
        after_verdict = err[err.index("── tv · verdict ──"):]
        assert "── tv · debate ──" not in after_verdict
        assert "── debate ──" not in after_verdict  # bare form must not reappear


# --- F3 / F3b: colour gate (C5/D3) ------------------------------------------

class TestColorOn:
    def test_color_on_tty_stderr_emits_ansi_escape(self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        _no_color_env(monkeypatch)
        graph = _FakeGraph([("plan", {"journal": ["p"]})], "tc")
        err = _drive_capturing(graph, "tc", tty=True)
        # TTY stderr, no NO_COLOR, TERM not dumb ⇒ ANSI ESC present on a role
        # line (F3).
        assert "\x1b[" in err


class TestColorOff:
    @pytest.mark.parametrize("variant", [
        "no_color_1", "no_color_empty", "no_color_flag",
        "term_dumb", "non_tty",
    ])
    def test_no_ansi_escape_under_each_disabling_condition(
            self, monkeypatch, tmp_path, variant):
        _setup_env(monkeypatch, tmp_path)
        if variant == "no_color_1":
            monkeypatch.setenv("NO_COLOR", "1")
            _no_color_env_except(monkeypatch, keep_no_color=True)
            args = None
            tty = True
        elif variant == "no_color_empty":
            monkeypatch.setenv("NO_COLOR", "")
            _no_color_env_except(monkeypatch, keep_no_color=True)
            args = None
            tty = True
        elif variant == "no_color_flag":
            _no_color_env(monkeypatch)
            args = argparse.Namespace(no_color=True)
            tty = True
        elif variant == "term_dumb":
            monkeypatch.setenv("TERM", "dumb")
            for k in ("NO_COLOR",):
                monkeypatch.delenv(k, raising=False)
            args = None
            tty = True
        else:  # non_tty
            _no_color_env(monkeypatch)
            args = None
            tty = False
        graph = _FakeGraph([("plan", {"journal": ["p"]}),
                            ("implement", {"journal": ["i"]})], "tco")
        err = _drive_capturing(graph, "tco", args=args, tty=tty)
        # No ANSI ESC anywhere — including the progress clear (C5).
        assert "\x1b" not in err, f"variant {variant!r} leaked ANSI: {err!r}"
        # Unicode structure marks (✓/──) may remain.
        assert "── tc · plan ──" in err or "plan" in err

    def test_resume_pause_path_no_ansi_under_no_color_flag(
            self, monkeypatch, tmp_path):
        # The interactive picker (_tty_pick) and _print_pause must honour the
        # colour gate: under --no-color the pause chrome contains no \x1b and
        # no embedded \r (C5/C9/Delta A).
        _setup_env(monkeypatch, tmp_path)
        _no_color_env(monkeypatch)
        data = {
            "stage": "plan approval",
            "reason": "review the \x1b[1mplan\x1b[0m and \r decide",
            "plan": "step \x1b[31mone\r\nthen two",
            "final": "verdict\x1b[0m",
            "options": [{"key": "ok", "label": "approve\x1b[2m now\r"}],
            "hint": "ok",
        }
        err = _TtyStringIO()
        err._tty = True  # TTY but --no-color ⇒ still no ANSI
        with redirect_stderr(err):
            run_mod._print_pause(data, "tp", color=False)
        text = err.getvalue()
        assert "\x1b" not in text
        assert "\r" not in text
        assert "approve now" in text  # label survived, control bytes stripped


def _no_color_env_except(monkeypatch, *, keep_no_color=False):
    """Clear TERM=dumb for the NO_COLOR variants (which disable colour via
    NO_COLOR, not TERM); leave NO_COLOR set when ``keep_no_color`` is True."""
    if os.environ.get("TERM") == "dumb":
        monkeypatch.setenv("TERM", "xterm")
    if not keep_no_color:
        monkeypatch.delenv("NO_COLOR", raising=False)


# --- F4: stderr is the UI (C1/C2) -------------------------------------------

class TestStderrCouncil:
    def test_stderr_council_lines_on_stderr_not_stdout(
            self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        _no_color_env(monkeypatch)
        graph = _FakeGraph([("plan", {"journal": ["p"]})], "ts")
        out, err = _drive_capturing_both(graph, "ts", tty=False)
        # Council narration (dispatch + return) is on stderr (C1)...
        assert "starting plan" in err
        assert "plan done" in err
        # ...and NOT on stdout (C2 — machine output only).
        assert "starting plan" not in out
        assert "plan done" not in out


# --- F4b: driver crash (C10/D11) --------------------------------------------

class TestDriverCrash:
    def test_driver_crash_clears_progress_and_prints_one_liner_no_traceback(
            self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        _no_color_env(monkeypatch)
        graph = _FakeGraphCrash(RuntimeError("boom-from-stream"), "dc")
        err = _drive_capturing(graph, "dc", tty=True)
        # Human one-liner on stderr, no traceback body, exit 2 (C10/D11).
        assert "driver crashed: RuntimeError: boom-from-stream" in err
        assert "Traceback" not in err
        # Points at the journal / events log.
        assert "journal:" in err
        assert "events:" in err

    def test_driver_crash_returns_2(self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        _no_color_env(monkeypatch)
        graph = _FakeGraphCrash(RuntimeError("boom"), "dc2")
        err = _TtyStringIO()
        err._tty = True
        rc = None
        with redirect_stderr(err):
            rc = run_mod._drive(graph, "dc2", None)
        assert rc == 2


class TestCrashBroken:
    def test_crash_broken_pipe_and_value_error_return_2_no_traceback(
            self, monkeypatch, tmp_path):
        # A closed/invalid stderr during the crash report can raise
        # BrokenPipeError OR ValueError ("I/O operation on closed file") —
        # the reporter must catch all three, skip the print, still return 2,
        # and never let Python print a traceback (D11/C10).
        _setup_env(monkeypatch, tmp_path)
        _no_color_env(monkeypatch)

        class _BadStream(io.StringIO):
            def write(self, s):
                raise ValueError("I/O operation on closed file")

        for exc_type in (BrokenPipeError, OSError, ValueError):
            graph = _FakeGraphCrash(RuntimeError(f"boom-{exc_type.__name__}"),
                                    f"cb-{exc_type.__name__}")
            bad = _TtyStringIO()
            bad._tty = True

            def _raise_write(s, _et=exc_type):
                raise _et("closed")

            bad.write = _raise_write
            rc = None
            raised = []
            try:
                with redirect_stderr(bad):
                    rc = run_mod._drive(
                        graph, f"cb-{exc_type.__name__}", None)
            except Exception as e:  # noqa: BLE001 — the reporter must NOT raise
                raised.append(e)
            assert rc == 2, f"{exc_type.__name__}: expected rc==2, got {rc!r}"
            assert not raised, (
                f"{exc_type.__name__}: reporter raised {raised!r}")


# --- C8/D9: dispatch before the agent blocks --------------------------------

class TestDispatchBeforeBlock:
    def test_dispatch_before_block_visible_before_sleep_ends(
            self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        _no_color_env(monkeypatch)
        # A graph that emits step_start, then sleeps (the agent blocks), then
        # step_end. The sync hook fires inside ev.emit("step_start") BEFORE
        # the sleep, so dispatch is visible immediately (C8/D9).
        seen = []

        class _Graph:
            def stream(self, payload, cfg, stream_mode=None):
                assert stream_mode == ["updates"]
                ev.emit("step_start", "db", "plan", "starting plan")
                # Capture stderr NOW — dispatch must already be rendered.
                seen.append(run_mod.ui_state["dispatched_node"])
                time.sleep(0.2)
                ev.emit("step_end", "db", "plan", "[ok] plan done [1s]",
                        outcome="ok")
                yield ("updates", {"plan": {"journal": ["p"]}})

            def get_state(self, cfg):
                return _FakeSnap()

        err = _drive_capturing(_Graph(), "db", tty=False)
        # The dispatch was rendered synchronously before the sleep: the hook
        # set dispatched_node before the graph body ran on.
        assert seen == ["plan"]
        assert "starting plan" in err


# --- C6/D9: return for N before dispatch for N+1 ----------------------------

class TestReturnBeforeNextDispatch:
    def test_return_before_next_dispatch(self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        _no_color_env(monkeypatch)
        graph = _FakeGraph(
            [("plan", {"journal": ["p"]}),
             ("implement", {"journal": ["i"]}),
             ("final_check", {"journal": ["f"]})],
            "rn",
        )
        err = _drive_capturing(graph, "rn", tty=False)
        # Return for each node precedes the next node's dispatch (C6/D9).
        assert err.index("plan done") < err.index("starting implement")
        assert err.index("implement done") < err.index("starting final_check")


# --- D7: UI thread survives a truncated current.json ------------------------

class TestPartialCurrent:
    def test_partial_current_survives_truncated_json(self, monkeypatch, tmp_path):
        metrics = _setup_env(monkeypatch, tmp_path)
        _no_color_env(monkeypatch)
        # Write a truncated (invalid) current.json before the drive; the UI
        # thread's per-iteration read catches ValueError and skips, so the
        # drive completes and returns 0 (D7).
        (metrics / "current.json").write_text('{"task": "pc", "phase": "run')
        graph = _FakeGraphSlow("pc", sleep=0.4)
        err = _TtyStringIO()
        err._tty = True
        rc = None
        with redirect_stderr(err):
            rc = run_mod._drive(graph, "pc", None)
        assert rc == 0
        # The drive still rendered dispatch/return despite the bad heartbeat.
        assert "starting plan" in err.getvalue()
        assert "plan done" in err.getvalue()


# --- D8: nested step events suppressed --------------------------------------

class TestNestedStep:
    def test_nested_step_start_and_manual_step_end_suppressed(
            self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        _no_color_env(monkeypatch)
        events = [
            ("step_start", "implement", "starting implement", {}),
            ("step_start", "implement", "test baseline batch 1", {}),  # nested
            ("step_end", "implement", "test baseline captured", {}),   # no outcome
            ("step_end", "implement", "[ok] implement done [1s]",
             {"outcome": "ok"}),
        ]
        graph = _FakeGraphEvents(events, "ns")
        err = _drive_capturing(graph, "ns", tty=False)
        # Exactly one dispatch + one return for implement (D8).
        assert err.count("starting implement") == 1
        assert err.count("implement done") == 1
        assert "test baseline" not in err


# --- D10/C5: ANSI stripped from event msg when colour off -------------------

class TestStripAnsi:
    def test_strip_ansi_from_event_msg_when_color_off(
            self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        _no_color_env(monkeypatch)
        # An event msg carrying an ANSI ESC (e.g. a subprocess slice from
        # quality_gates) must be stripped when colour is off (D10/C5).
        events = [
            ("step_start", "plan", "starting \x1b[1mplan\x1b[0m review", {}),
            ("step_end", "plan", "[ok] \x1b[32mdone\x1b[0m [1s]",
             {"outcome": "ok"}),
        ]
        graph = _FakeGraphEvents(events, "sa")
        err = _drive_capturing(graph, "sa", tty=False)
        assert "\x1b" not in err
        assert "plan review" in err
        assert "done" in err


# --- D10/Delta A: pause fields sanitised when colour off --------------------

class TestSanitizePause:
    def test_sanitize_pause_fields_when_color_off(self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        _no_color_env(monkeypatch)
        data = {
            "stage": "effort\x1b[0m level",
            "reason": "choose\r\nan effort level",
            "context": "ctx\x1b[31mred\r",
            "plan": "step \x1b[1mone",
            "final": "verdict\x1b[0m\r\n",
            "batches": ["batch\x1b[32m one\r"],
            "options": [{"key": "troop-monke", "label": "troop\x1b[0m\r"}],
            "hint": "troop-monke",
        }
        snap = _FakeSnapPause(data)
        graph = _FakeGraphEvents([], "sp", snap=snap)
        err = _drive_capturing(graph, "sp", tty=False)
        assert "\x1b" not in err, f"ESC leaked: {err!r}"
        assert "\r" not in err, f"\\r leaked: {err!r}"
        # The action line is last in the pause block (C9).
        assert err.rstrip().endswith(
            'action: ./run.py resume sp --answer "<choice>"')
        # Field content survived sanitisation (control bytes stripped only).
        assert "troop" in err
        assert "batch one" in err


# --- D9: hook fires before the (slow) notify push ---------------------------

class TestHookBeforePush:
    def test_hook_before_push_dispatch_immediate_despite_slow_push(
            self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        _no_color_env(monkeypatch)
        # Slow _push (2s socket timeout simulated) must NOT delay dispatch:
        # the hook fires inside emit() BEFORE _should_notify/_push (D9).
        import pipeline_graph.events as evmod
        slow_calls = {"n": 0}

        def _slow_push(title, msg, prio, role=""):
            slow_calls["n"] += 1
            time.sleep(0.4)

        monkeypatch.setattr(evmod, "_push", _slow_push)
        # Force notify on for step events so _push is actually invoked.
        monkeypatch.setattr(evmod, "NOTIFY_LEVEL", "all")

        class _Graph:
            def stream(self, payload, cfg, stream_mode=None):
                assert stream_mode == ["updates"]
                ev.emit("step_start", "hp", "plan", "starting plan")
                ev.emit("step_end", "hp", "plan", "[ok] plan done [1s]",
                        outcome="ok")
                yield ("updates", {"plan": {"journal": ["p"]}})

            def get_state(self, cfg):
                return _FakeSnap()

        err = _drive_capturing(_Graph(), "hp", tty=False)
        # Dispatch was rendered despite the slow push (the hook ran first).
        assert "starting plan" in err
        assert "plan done" in err
        assert slow_calls["n"] >= 1


# --- TASK-022: pause triage block (item 19/26) ------------------------------


class TestPauseTriageBlock:
    """TASK-022 item 26: _print_pause renders a triage block (mode/blocker
    trend/repeated-new/recommended+rationale) only when data["triage"] is a
    dict, and prints "no active rounds" wording when blocker_counts is empty."""

    def test_populated_blocker_counts_renders_trend(self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        _no_color_env(monkeypatch)
        data = {
            "stage": "escalation",
            "reason": "debate thrashing: churning",
            "options": [{"key": "ok", "label": "proceed"}],
            "hint": "ok",
            "triage": {
                "mode": "thrashing",
                "blocker_counts": [3, 2, 2],
                "repeated": ["alpha", "beta"],
                "new": ["delta"],
                "recommended": "ok",
                "rationale": "churning — more rounds will not converge",
            },
        }
        err = _TtyStringIO()
        err._tty = True
        with redirect_stderr(err):
            run_mod._print_pause(data, "pt", color=False)
        text = err.getvalue()
        assert "triage:" in text
        assert "mode:" in text
        assert "thrashing" in text
        # The trend is joined by →.
        assert "3 → 2 → 2" in text
        assert "blockers:" in text
        assert "repeated/new:" in text
        assert "2 / 1" in text
        assert "recommended: ok" in text
        assert "churning" in text

    def test_empty_blocker_counts_renders_no_active_rounds(
            self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        _no_color_env(monkeypatch)
        data = {
            "stage": "escalation",
            "reason": "debate thrashing: churning",
            "options": [{"key": "ok", "label": "proceed"}],
            "hint": "ok",
            "triage": {
                "mode": "unknown",
                "blocker_counts": [],
                "repeated": [],
                "new": [],
                "recommended": "",
                "rationale": "",
            },
        }
        err = _TtyStringIO()
        err._tty = True
        with redirect_stderr(err):
            run_mod._print_pause(data, "pt", color=False)
        text = err.getvalue()
        assert "triage:" in text
        assert "no active rounds" in text

    def test_no_triage_key_renders_no_triage_block(self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        _no_color_env(monkeypatch)
        data = {
            "stage": "escalation",
            "reason": "tests still failing",
            "options": [{"key": "ok", "label": "waive"}],
        }
        err = _TtyStringIO()
        err._tty = True
        with redirect_stderr(err):
            run_mod._print_pause(data, "pt", color=False)
        text = err.getvalue()
        assert "triage:" not in text


# ---------------------------------------------------------------------------
# TASK-027 batch 1: Rich TTY start wizard + in-process session loop +
# top-help stdout/no-color threading + BrokenPipe-safe _drive restructuring.
# ---------------------------------------------------------------------------

class _TtyStdin(io.StringIO):
    """A ``StringIO`` whose ``isatty()`` is configurable, for simulating a
    TTY stdin in the wizard / session-loop tests."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._tty = False

    def isatty(self):
        return self._tty


class _FakeGraphSession:
    """Graph that pauses on the first stream call, then finishes on the
    second (after ``Command(resume=answer)``). Drives the in-session pause
    loop: first ``get_state`` returns a pause snapshot, second returns a
    finished snapshot."""

    def __init__(self, task_id, pause_data):
        self._task_id = task_id
        self._pause_data = pause_data
        self._stream_calls = 0

    def stream(self, payload, cfg, stream_mode=None):
        assert stream_mode == ["updates"]
        self._stream_calls += 1
        if self._stream_calls == 1:
            ev.emit("step_start", self._task_id, "plan", "starting plan")
            ev.emit("step_end", self._task_id, "plan", "[ok] plan done [1s]",
                    outcome="ok")
            yield ("updates", {"plan": {"journal": ["p"]}})
        else:
            ev.emit("step_start", self._task_id, "implement",
                    "starting implement")
            ev.emit("step_end", self._task_id, "implement",
                    "[ok] impl done [1s]", outcome="ok")
            yield ("updates", {"implement": {"journal": ["i"]}})

    def get_state(self, cfg):
        if self._stream_calls == 1:
            return _FakeSnapPause(self._pause_data)
        return _FakeSnap()


class _FakeGraphKbdInt:
    """Graph whose ``stream`` raises ``KeyboardInterrupt`` — exercises the
    item-32 no-bare-raise path (returns 130)."""
    def stream(self, payload, cfg, stream_mode=None):
        assert stream_mode == ["updates"]
        raise KeyboardInterrupt
        yield  # noqa: unreachable — generator marker

    def get_state(self, cfg):
        return _FakeSnap()


def _clear_no_input_env(monkeypatch):
    """Clear PIPELINE_NO_INPUT and reset _EXTERNAL_NO_INPUT so a test starts
    from a clean interactive-gate state."""
    monkeypatch.delenv("PIPELINE_NO_INPUT", raising=False)
    monkeypatch.setattr(run_mod, "_EXTERNAL_NO_INPUT", None)


# --- TestStartWizard --------------------------------------------------------

class TestStartWizard:
    def test_wizard_fills_missing_fields(self, monkeypatch):
        _clear_no_input_env(monkeypatch)
        ns = argparse.Namespace(task_id=None, request=None, file=None,
                                effort=None, auto=False, interview=False,
                                no_color=False)
        # Prompt order: task_id, request, effort, auto (Confirm), interview.
        stdin = _TtyStdin("005\nmy request\ntroop-monke\n\n\n")
        stdin._tty = True
        monkeypatch.setattr(sys, "stdin", stdin)
        result = run_mod._run_start_wizard(ns)
        assert result is ns
        assert ns.task_id == "005"
        assert ns.request == "my request"
        assert ns.effort == "troop-monke"


# --- TestWizardSkipsProvidedFlags -------------------------------------------

class TestWizardSkipsProvidedFlags:
    def test_wizard_skips_provided_effort(self, monkeypatch):
        _clear_no_input_env(monkeypatch)
        ns = argparse.Namespace(task_id="005", request="do thing", file=None,
                                effort="barrel-monke", auto=False,
                                interview=False, no_color=False)
        # stdin has only the auto + interview answers (effort is skipped).
        stdin = _TtyStdin("\n\n")
        stdin._tty = True
        monkeypatch.setattr(sys, "stdin", stdin)
        result = run_mod._run_start_wizard(ns)
        assert result is ns
        assert ns.effort == "barrel-monke"  # unchanged — not prompted

    def test_wizard_skips_provided_task_id_and_request(self, monkeypatch):
        _clear_no_input_env(monkeypatch)
        ns = argparse.Namespace(task_id="010", request="build it", file=None,
                                effort=None, auto=True, interview=False,
                                no_color=False)
        # task_id, request, auto already set → prompts only for effort +
        # interview.
        stdin = _TtyStdin("scout-monke\n\n")
        stdin._tty = True
        monkeypatch.setattr(sys, "stdin", stdin)
        result = run_mod._run_start_wizard(ns)
        assert result is ns
        assert ns.task_id == "010"
        assert ns.request == "build it"
        assert ns.auto is True  # already true — not prompted


# --- TestWizardNoInput ------------------------------------------------------

class TestWizardNoInput:
    def test_start_no_input_does_not_run_wizard(self, monkeypatch):
        _mock_side_effects(monkeypatch, mock_drive=True)
        monkeypatch.setattr(run_mod, "build_graph",
                            lambda *a, **k: _FakeGraphNoState())
        called = []
        monkeypatch.setattr(run_mod, "_run_start_wizard",
                            lambda *a, **k: called.append(1) or
                            argparse.Namespace())
        rc, out, err = _main_capturing(["start", "--no-input"])
        assert rc == 2  # incomplete start, non-interactive → error
        assert called == []  # wizard NOT called


# --- TestWizardPipelineNoInput ----------------------------------------------

class TestWizardPipelineNoInput:
    def test_pipeline_no_input_env_disables_wizard(self, monkeypatch):
        _mock_side_effects(monkeypatch, mock_drive=True)
        monkeypatch.setattr(run_mod, "build_graph",
                            lambda *a, **k: _FakeGraphNoState())
        monkeypatch.setenv("PIPELINE_NO_INPUT", "1")
        called = []
        monkeypatch.setattr(run_mod, "_run_start_wizard",
                            lambda *a, **k: called.append(1) or
                            argparse.Namespace())
        rc, out, err = _main_capturing(["start"])
        assert rc == 2  # incomplete, non-interactive
        assert called == []


# --- TestWizardTopLevelNoInput ----------------------------------------------

class TestWizardTopLevelNoInput:
    def test_top_level_no_input_disables_wizard(self, monkeypatch):
        _mock_side_effects(monkeypatch, mock_drive=True)
        monkeypatch.setattr(run_mod, "build_graph",
                            lambda *a, **k: _FakeGraphNoState())
        called = []
        monkeypatch.setattr(run_mod, "_run_start_wizard",
                            lambda *a, **k: called.append(1) or
                            argparse.Namespace())
        rc, out, err = _main_capturing(["--no-input", "start"])
        assert rc == 2  # incomplete, non-interactive
        assert called == []


# --- TestResumeTopLevelNoInput ----------------------------------------------

class TestResumeTopLevelNoInput:
    def test_top_level_no_input_resume_requires_answer(self, monkeypatch):
        _mock_side_effects(monkeypatch, mock_drive=True)
        monkeypatch.setattr(C, "CHECKPOINT_DB", Path(__file__))
        # A graph with a pending interrupt so the resume guard checks
        # for --answer.
        pause_data = {"stage": "effort", "reason": "choose",
                      "options": [{"key": "ok", "label": "proceed"}]}
        class _GraphWithInterrupt:
            def stream(self, payload, cfg, stream_mode=None):
                yield from []
            def get_state(self, cfg):
                return _FakeSnapPause(pause_data)
        monkeypatch.setattr(run_mod, "build_graph",
                            lambda cp: _GraphWithInterrupt())
        rc, out, err = _main_capturing(["--no-input", "resume", "001"])
        assert rc == 2
        assert "--answer is required" in err


# --- TestTopHelpNonTty ------------------------------------------------------

class TestTopHelpNonTty:
    def test_top_help_non_tty_uses_plain_print(self, monkeypatch):
        # Non-TTY stdout ⇒ _use_color returns False ⇒ plain print() branch.
        # _main_capturing uses io.StringIO for stdout (not a TTY).
        ns = argparse.Namespace()
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            run_mod._print_top_help(ns)
        text = out.getvalue()
        assert "MonkeForge pipeline CLI — examples:" in text
        assert "  ./run.py start 005" in text
        # No rich Panel border characters.
        assert "╭" not in text and "╰" not in text


# --- TestTopHelpRich --------------------------------------------------------

class TestTopHelpRich:
    def test_top_help_rich_branch_on_tty_stdout(self, monkeypatch):
        _no_color_env(monkeypatch)
        ns = argparse.Namespace(no_color=False)
        # Force _use_color to return True for stdout by patching.
        monkeypatch.setattr(run_mod, "_use_color",
                            lambda args, stream=None: True)
        out = _TtyStdin()
        out._tty = True
        with redirect_stdout(out):
            run_mod._print_top_help(ns)
        text = out.getvalue()
        # The examples text is still present (Rich renders it).
        assert "MonkeForge pipeline CLI — examples:" in text
        assert "./run.py start 005" in text


# --- TestSessionLoop --------------------------------------------------------

class TestSessionLoop:
    def test_session_loop_resumes_after_inline_answer(
            self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        _no_color_env(monkeypatch)
        _clear_no_input_env(monkeypatch)
        pause_data = {"stage": "effort level", "reason": "choose effort",
                      "options": [{"key": "ok", "label": "proceed"}],
                      "hint": "ok"}
        graph = _FakeGraphSession("sl", pause_data)
        stdin = _TtyStdin("ok\n")
        stdin._tty = True
        monkeypatch.setattr(sys, "stdin", stdin)
        args = argparse.Namespace(no_input=False, no_color=False)
        err = _TtyStringIO()
        err._tty = False
        rc = None
        with redirect_stderr(err):
            rc = run_mod._drive(graph, "sl", None, args=args)
        assert rc == 0  # finished after resume
        text = err.getvalue()
        # The in-session pause rendered (in_session=True ⇒ pick hint, no
        # action line).
        assert "pick an option" in text
        assert "action:" not in text
        # The second iteration's dispatch rendered.
        assert "starting implement" in text


# --- TestSessionLoopNoInput -------------------------------------------------

class TestSessionLoopNoInput:
    def test_no_input_disables_session_loop(self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        _no_color_env(monkeypatch)
        _clear_no_input_env(monkeypatch)
        pause_data = {"stage": "effort level", "reason": "choose effort",
                      "options": [{"key": "ok", "label": "proceed"}],
                      "hint": "ok"}
        graph = _FakeGraphSession("slni", pause_data)
        args = argparse.Namespace(no_input=True, no_color=False)
        err = _TtyStringIO()
        rc = None
        with redirect_stderr(err):
            rc = run_mod._drive(graph, "slni", None, args=args)
        assert rc == 0  # paused → exit 0 (non-interactive)
        text = err.getvalue()
        # Non-interactive ⇒ action line present, no pick hint.
        assert "action:" in text
        assert "pick an option" not in text


# --- TestSessionLoopPipelineNoInput -----------------------------------------

class TestSessionLoopPipelineNoInput:
    def test_pipeline_no_input_disables_session_loop(
            self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        _no_color_env(monkeypatch)
        monkeypatch.setenv("PIPELINE_NO_INPUT", "1")
        monkeypatch.setattr(run_mod, "_EXTERNAL_NO_INPUT", "1")
        pause_data = {"stage": "effort level", "reason": "choose effort",
                      "options": [{"key": "ok", "label": "proceed"}],
                      "hint": "ok"}
        graph = _FakeGraphSession("slpni", pause_data)
        args = argparse.Namespace(no_input=False, no_color=False)
        err = _TtyStringIO()
        rc = None
        with redirect_stderr(err):
            rc = run_mod._drive(graph, "slpni", None, args=args)
        assert rc == 0  # paused → exit 0
        text = err.getvalue()
        assert "action:" in text
        assert "pick an option" not in text


# --- TestSessionLoopInvalidAnswer -------------------------------------------

class TestSessionLoopInvalidAnswer:
    def test_non_option_answer_still_continues_loop(
            self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        _no_color_env(monkeypatch)
        _clear_no_input_env(monkeypatch)
        pause_data = {"stage": "effort level", "reason": "choose effort",
                      "options": [{"key": "ok", "label": "proceed"}],
                      "hint": "ok"}
        graph = _FakeGraphSession("slia", pause_data)
        # "bogus" is not in the options, but the session loop passes it
        # through to Command(resume="bogus") — the graph handles validation.
        stdin = _TtyStdin("bogus\n")
        stdin._tty = True
        monkeypatch.setattr(sys, "stdin", stdin)
        args = argparse.Namespace(no_input=False, no_color=False)
        err = _TtyStringIO()
        rc = None
        with redirect_stderr(err):
            rc = run_mod._drive(graph, "slia", None, args=args)
        assert rc == 0  # finished — the fake graph finishes on 2nd stream
        assert "starting implement" in err.getvalue()


# --- TestSessionLoopPickerEof -----------------------------------------------

class TestSessionLoopPickerEof:
    def test_eof_in_picker_returns_130(self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        _no_color_env(monkeypatch)
        _clear_no_input_env(monkeypatch)
        pause_data = {"stage": "effort level", "reason": "choose effort",
                      "options": [{"key": "ok", "label": "proceed"}],
                      "hint": "ok"}
        graph = _FakeGraphSession("sleof", pause_data)
        # Empty stdin ⇒ readline returns "" ⇒ EOFError (eof_raises=True).
        stdin = _TtyStdin("")
        stdin._tty = True
        monkeypatch.setattr(sys, "stdin", stdin)
        args = argparse.Namespace(no_input=False, no_color=False)
        err = _TtyStringIO()
        rc = None
        with redirect_stderr(err):
            rc = run_mod._drive(graph, "sleof", None, args=args)
        assert rc == 130  # interrupted
        assert "interrupted" in err.getvalue()


# --- TestSessionLoopNoDoubleRender ------------------------------------------

class TestSessionLoopNoDoubleRender:
    def test_tty_pick_called_with_render_false(
            self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        _no_color_env(monkeypatch)
        _clear_no_input_env(monkeypatch)
        pause_data = {"stage": "effort level", "reason": "choose effort",
                      "options": [{"key": "ok", "label": "proceed"}],
                      "hint": "ok"}
        graph = _FakeGraphSession("slndr", pause_data)
        calls = []
        real_tty_pick = run_mod._tty_pick

        def spy(*a, **kw):
            calls.append(kw)
            return real_tty_pick(*a, **kw)

        monkeypatch.setattr(run_mod, "_tty_pick", spy)
        stdin = _TtyStdin("ok\n")
        stdin._tty = True
        monkeypatch.setattr(sys, "stdin", stdin)
        args = argparse.Namespace(no_input=False, no_color=False)
        err = _TtyStringIO()
        with redirect_stderr(err):
            run_mod._drive(graph, "slndr", None, args=args)
        # _tty_pick was called with render=False (no double render).
        assert calls, "expected _tty_pick to be called"
        assert calls[0].get("render") is False
        assert calls[0].get("eof_raises") is True


# --- TestPauseActionTty -----------------------------------------------------

class TestPauseActionTty:
    def test_tty_pause_uses_in_session_no_action_line(
            self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        _no_color_env(monkeypatch)
        _clear_no_input_env(monkeypatch)
        pause_data = {"stage": "effort level", "reason": "choose effort",
                      "options": [{"key": "ok", "label": "proceed"}],
                      "hint": "ok"}
        graph = _FakeGraphSession("pat", pause_data)
        stdin = _TtyStdin("ok\n")
        stdin._tty = True
        monkeypatch.setattr(sys, "stdin", stdin)
        args = argparse.Namespace(no_input=False, no_color=False)
        err = _TtyStringIO()
        with redirect_stderr(err):
            run_mod._drive(graph, "pat", None, args=args)
        text = err.getvalue()
        # in_session=True ⇒ "pick an option" hint, NO "action:" line.
        assert "pick an option" in text
        assert "action: ./run.py resume" not in text


# --- TestPauseActionNonTty --------------------------------------------------

class TestPauseActionNonTty:
    def test_non_tty_pause_uses_action_line(self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        _no_color_env(monkeypatch)
        _clear_no_input_env(monkeypatch)
        pause_data = {"stage": "effort level", "reason": "choose effort",
                      "options": [{"key": "ok", "label": "proceed"}],
                      "hint": "ok"}
        graph = _FakeGraphSession("pant", pause_data)
        # stdin is NOT a TTY (default _TtyStdin._tty=False) — but
        # _FakeGraphSession pauses on first stream; non-interactive ⇒ 4b.
        args = argparse.Namespace(no_input=False, no_color=False)
        err = _TtyStringIO()
        rc = None
        with redirect_stderr(err):
            rc = run_mod._drive(graph, "pant", None, args=args)
        text = err.getvalue()
        assert rc == 0
        # in_session=False ⇒ "action:" line present, no "pick an option".
        assert "action: ./run.py resume" in text
        assert "pick an option" not in text


# --- TestRichNoColor --------------------------------------------------------

class TestRichNoColor:
    def test_color_false_uses_plain_branch(self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        _no_color_env(monkeypatch)
        data = {"stage": "test", "reason": "choose",
                "options": [{"key": "ok", "label": "proceed"}], "hint": "ok"}
        err = _TtyStringIO()
        with redirect_stderr(err):
            run_mod._print_pause(data, "rnc", color=False)
        text = err.getvalue()
        # Plain branch: no rich Panel border characters.
        assert "╭" not in text and "╰" not in text
        assert "=== PAUSED:" in text
        assert "action:" in text


# --- TestRichEscaping -------------------------------------------------------

class TestRichEscaping:
    def test_rich_branch_escapes_markup_characters(self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        _no_color_env(monkeypatch)
        # A field containing rich markup syntax "[bold]" must be rendered
        # literally (Text wrapping escapes it), not interpreted as bold.
        data = {"stage": "[bold]not bold[/bold]", "reason": "choose",
                "options": [{"key": "ok", "label": "proceed"}], "hint": "ok"}
        err = _TtyStringIO()
        err._tty = True
        with redirect_stderr(err):
            run_mod._print_pause(data, "re", color=True)
        text = err.getvalue()
        # The literal "[bold]" text appears (escaped), not interpreted.
        assert "[bold]" in text


# --- TestKeyboardInterrupt --------------------------------------------------

class TestKeyboardInterrupt:
    def test_kbdint_returns_130_no_bare_raise(self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        _no_color_env(monkeypatch)
        graph = _FakeGraphKbdInt()
        args = argparse.Namespace(no_color=False)
        err = _TtyStringIO()
        rc = None
        raised = []
        try:
            with redirect_stderr(err):
                rc = run_mod._drive(graph, "ki", None, args=args)
        except KeyboardInterrupt:
            raised.append("bare-raise")
        assert rc == 130
        assert not raised, "KeyboardInterrupt must NOT re-raise (item 32)"
        assert "interrupted" in err.getvalue()
        assert "resume" in err.getvalue().lower()


# --- TestWizardMissingFile --------------------------------------------------

class TestWizardMissingFile:
    def test_wizard_missing_file_returns_error_sentinel(self, monkeypatch):
        _clear_no_input_env(monkeypatch)
        ns = argparse.Namespace(task_id="005", request=None,
                                file="/nonexistent/path/file.txt",
                                effort=None, auto=False, interview=False,
                                no_color=False)
        stdin = _TtyStdin("\n")
        stdin._tty = True
        monkeypatch.setattr(sys, "stdin", stdin)
        result = run_mod._run_start_wizard(ns)
        assert result is run_mod._WIZARD_ERROR


# --- TestStaleWarningStderr -------------------------------------------------

class TestStaleWarningStderr:
    def test_stale_warning_goes_to_stderr(self, monkeypatch, tmp_path):
        _setup_env(monkeypatch, tmp_path)
        # Create stale task files so _warn_stale_task_files has something to
        # report.
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        (tasks_dir / "TASK-stale-brief.md").write_text("stale")
        monkeypatch.setattr(C, "TASKS", tasks_dir)
        monkeypatch.setattr(C, "REPO", tmp_path)
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            run_mod._warn_stale_task_files("stale")
        assert "already has files" in err.getvalue()
        assert "already has files" not in out.getvalue()


# --- TestWizardAbort --------------------------------------------------------

class TestWizardAbort:
    def test_wizard_eof_returns_none(self, monkeypatch):
        _clear_no_input_env(monkeypatch)
        ns = argparse.Namespace(task_id=None, request=None, file=None,
                                effort=None, auto=False, interview=False,
                                no_color=False)
        # Empty stdin ⇒ Prompt.ask raises EOFError ⇒ wizard returns None.
        stdin = _TtyStdin("")
        stdin._tty = True
        monkeypatch.setattr(sys, "stdin", stdin)
        result = run_mod._run_start_wizard(ns)
        assert result is None


# --- TestTopLevelNoInputSuiteResolution -------------------------------------

class TestTopLevelNoInputSuiteResolution:
    def test_top_level_no_input_sets_env_and_resolves(self, monkeypatch):
        _mock_side_effects(monkeypatch, mock_drive=True)
        monkeypatch.setattr(run_mod, "build_graph",
                            lambda *a, **k: _FakeGraphNoState())
        called = []
        monkeypatch.setattr(tr, "resolve_test_suites",
                            lambda *a, **k: called.append((a, k)))
        _main_capturing(["--no-input", "start", "005", "do thing"])
        assert os.environ.get("PIPELINE_NO_INPUT") == "1"
        assert len(called) == 1
        assert called[0][1].get("task_id") == "005"


# --- TestExternalPipelineNoInputSuiteResolution -----------------------------

class TestExternalPipelineNoInputSuiteResolution:
    def test_external_pipeline_no_input_survives_bridge(self, monkeypatch):
        _mock_side_effects(monkeypatch, mock_drive=True)
        monkeypatch.setattr(run_mod, "build_graph",
                            lambda *a, **k: _FakeGraphNoState())
        called = []
        monkeypatch.setattr(tr, "resolve_test_suites",
                            lambda *a, **k: called.append((a, k)))
        # Simulate an external caller that set PIPELINE_NO_INPUT before
        # exec'ing run.py.
        monkeypatch.setenv("PIPELINE_NO_INPUT", "1")
        _main_capturing(["start", "005", "do thing"])
        # The bridge must NOT pop the externally-set value.
        assert os.environ.get("PIPELINE_NO_INPUT") == "1"
        assert len(called) == 1
