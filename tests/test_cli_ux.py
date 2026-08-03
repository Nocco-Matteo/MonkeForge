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
import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timezone

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
