"""Tests for the verbatim debate archive (TASK-013 batch 4 / F4).

Covers the conformance checklist items 32–37:
  - Archive creation (first condensation writes verbatim) and accumulation
    (subsequent condensations append with a UTC-timestamped snapshot header).
  - The `degraded` event message names the archive path.
  - The PROGRESS pointer appears immediately after condensation, without a
    subsequent `_write_progress` call.
  - The `_write_progress` backstop re-adds the pointer after a full rewrite.
  - `run.py status` prints the archive pointer line when the archive exists.
"""
import contextlib
import io
import json
import os
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline_graph import agents as A
from pipeline_graph import config as C
from pipeline_graph import events as ev
from pipeline_graph import condenser
from pipeline_graph.nodes import common as N
from pipeline_graph.state import Conversation

import run as run_mod


def _conv(tid, **overrides):
    """Minimal Conversation for condenser-block tests (mirrors test_condenser)."""
    defaults = dict(
        task_id=tid,
        request="",
        brief="",
        plan="",
        debate_history="",
        batch_context="{}",
        review_history="",
        final="",
        progress="",
        summary="",
        visual_review="",
        journal=(),
    )
    defaults.update(overrides)
    return Conversation(**defaults)


def _round(n, critic="Reviewer", verdict="APPROVE", blockers=0,
           pad="x" * 1800, reply=None):
    """Build one round's section(s) with a distinct, findable body."""
    lines = [f"## Round {n} — {critic}", "", f"VERDICT: {verdict}"]
    for i in range(blockers):
        lines.append(f"[BLOCKER] issue {i}")
    lines.append(f"PAD-{n}-{pad}")
    section = "\n".join(lines) + "\n"
    if reply is not None:
        section += f"\n## Round {n} — Reply\n\n{reply}\n"
    return section


def _setup_env(monkeypatch, tmp_path):
    """Redirect every disk sink run_agent touches into tmp_path."""
    metrics = tmp_path / "metrics"
    metrics.mkdir()
    prompts = tmp_path / "prompts"
    raw = tmp_path / "raw"
    debates = tmp_path / "debates"
    final = tmp_path / "final"
    debates.mkdir()
    final.mkdir()
    monkeypatch.setattr(C, "METRICS", metrics)
    monkeypatch.setattr(C, "RUNS_LOG", metrics / "runs.jsonl")
    monkeypatch.setattr(C, "PROMPTS", prompts)
    monkeypatch.setattr(C, "RAW", raw)
    monkeypatch.setattr(C, "DEBATES", debates)
    monkeypatch.setattr(C, "FINAL", final)
    monkeypatch.setattr(C, "DRY_RUN", True)
    monkeypatch.setattr(ev, "EVENTS_LOG", metrics / "events.jsonl")
    monkeypatch.setattr(ev, "PIPELINE_LOG", metrics / "pipeline.log")
    monkeypatch.setattr(ev, "_push", lambda *a, **k: None)
    return debates, final


def _degraded_events():
    if not ev.EVENTS_LOG.exists():
        return []
    out = []
    for line in ev.EVENTS_LOG.read_text().splitlines():
        if not line:
            continue
        rec = json.loads(line)
        if rec.get("kind") == "degraded":
            out.append(rec)
    return out


# --- archive creation / accumulation (items 32, 33) ------------------------


class TestArchiveCreationAccumulation:
    def test_first_condensation_writes_verbatim_archive(self, monkeypatch, tmp_path):
        debates, final = _setup_env(monkeypatch, tmp_path)
        monkeypatch.setenv("PIPELINE_TOKEN_BUDGET_PLAN_REVIEWER", "2000")
        monkeypatch.setattr(C, "CONDENSER_KEEP_RECENT", 2)
        text = "".join("\n\n" + _round(n) for n in range(1, 6))
        (debates / "DEBATE-t4.md").write_text(text)

        A.run_agent("PLAN_REVIEWER", _conv("t4", debate_history=text), "step",
                    template="debate_review")

        archive = debates / "DEBATE-t4-full.md"
        assert archive.exists(), "archive was not created on first condensation"
        archive_text = archive.read_text()
        # First condensation: verbatim, no snapshot header.
        assert "=== pre-condensation snapshot" not in archive_text
        # The full pre-condensation content is preserved (all PAD markers).
        for n in range(1, 6):
            assert f"PAD-{n}-" in archive_text

    def test_second_condensation_appends_with_snapshot_header(self, monkeypatch, tmp_path):
        debates, final = _setup_env(monkeypatch, tmp_path)
        monkeypatch.setenv("PIPELINE_TOKEN_BUDGET_PLAN_REVIEWER", "2000")
        monkeypatch.setattr(C, "CONDENSER_KEEP_RECENT", 2)
        text = "".join("\n\n" + _round(n) for n in range(1, 6))
        (debates / "DEBATE-t4.md").write_text(text)

        # First condensation.
        A.run_agent("PLAN_REVIEWER", _conv("t4", debate_history=text), "step1",
                    template="debate_review")
        archive = debates / "DEBATE-t4-full.md"
        first_archive = archive.read_text()
        # Working file is now condensed.
        condensed_1 = (debates / "DEBATE-t4.md").read_text()

        # Grow the debate back over budget by appending new full rounds.
        grown = condensed_1 + "".join("\n\n" + _round(n) for n in range(6, 11))
        (debates / "DEBATE-t4.md").write_text(grown)

        # Second condensation.
        A.run_agent("PLAN_REVIEWER", _conv("t4", debate_history=grown), "step2",
                    template="debate_review")

        archive_text = archive.read_text()
        # The first-condensation verbatim content is still there.
        assert first_archive in archive_text
        # A snapshot header was appended.
        assert "=== pre-condensation snapshot at" in archive_text
        assert "UTC ===" in archive_text
        # The appended snapshot contains the pre-condensation working file
        # content (the condensed_1 + new rounds state).
        assert "PAD-6-" in archive_text
        assert "PAD-10-" in archive_text
        # Only one snapshot header (one append).
        assert archive_text.count("=== pre-condensation snapshot") == 1

    def test_degraded_event_names_archive_path(self, monkeypatch, tmp_path):
        debates, final = _setup_env(monkeypatch, tmp_path)
        monkeypatch.setenv("PIPELINE_TOKEN_BUDGET_PLAN_REVIEWER", "2000")
        monkeypatch.setattr(C, "CONDENSER_KEEP_RECENT", 2)
        text = "".join("\n\n" + _round(n) for n in range(1, 6))
        (debates / "DEBATE-t4.md").write_text(text)

        A.run_agent("PLAN_REVIEWER", _conv("t4", debate_history=text), "step",
                    template="debate_review")

        degraded = _degraded_events()
        assert len(degraded) == 1
        msg = degraded[0]["msg"]
        assert "DEBATE-t4-full.md" in msg


# --- PROGRESS pointer at archive-creation time (item 34) -------------------


class TestProgressPointer:
    def test_pointer_appears_immediately_after_condensation(self, monkeypatch, tmp_path):
        """The PROGRESS pointer is written at archive-creation time, without
        a subsequent _write_progress call."""
        debates, final = _setup_env(monkeypatch, tmp_path)
        monkeypatch.setenv("PIPELINE_TOKEN_BUDGET_PLAN_REVIEWER", "2000")
        monkeypatch.setattr(C, "CONDENSER_KEEP_RECENT", 2)
        text = "".join("\n\n" + _round(n) for n in range(1, 6))
        (debates / "DEBATE-t4.md").write_text(text)
        # Pre-create PROGRESS so the pointer can be appended.
        progress = final / "PROGRESS-t4.md"
        progress.write_text("# PROGRESS-t4\n\n| Batch | Scope |\n|---|---|\n| 1 | x |\n")

        A.run_agent("PLAN_REVIEWER", _conv("t4", debate_history=text), "step",
                    template="debate_review")

        progress_text = progress.read_text()
        assert "Verbatim debate archive: DEBATE-t4-full.md" in progress_text
        # The original table content is preserved.
        assert "# PROGRESS-t4" in progress_text

    def test_pointer_not_duplicated_on_second_condensation(self, monkeypatch, tmp_path):
        debates, final = _setup_env(monkeypatch, tmp_path)
        monkeypatch.setenv("PIPELINE_TOKEN_BUDGET_PLAN_REVIEWER", "2000")
        monkeypatch.setattr(C, "CONDENSER_KEEP_RECENT", 2)
        text = "".join("\n\n" + _round(n) for n in range(1, 6))
        (debates / "DEBATE-t4.md").write_text(text)
        progress = final / "PROGRESS-t4.md"
        progress.write_text("# PROGRESS-t4\n\n| Batch | Scope |\n|---|---|\n| 1 | x |\n")

        A.run_agent("PLAN_REVIEWER", _conv("t4", debate_history=text), "step1",
                    template="debate_review")
        condensed_1 = (debates / "DEBATE-t4.md").read_text()
        grown = condensed_1 + "".join("\n\n" + _round(n) for n in range(6, 11))
        (debates / "DEBATE-t4.md").write_text(grown)
        A.run_agent("PLAN_REVIEWER", _conv("t4", debate_history=grown), "step2",
                    template="debate_review")

        progress_text = progress.read_text()
        assert progress_text.count("Verbatim debate archive: DEBATE-t4-full.md") == 1


# --- _write_progress backstop (item 35) -------------------------------------


class TestWriteProgressBackstop:
    def test_backstop_adds_pointer_when_archive_exists(self, monkeypatch, tmp_path):
        debates, final = _setup_env(monkeypatch, tmp_path)
        # Create the archive file.
        (debates / "DEBATE-t5-full.md").write_text("verbatim debate content\n")

        batches = [{"n": 1, "scope": "test scope", "status": "DONE",
                    "outcome": "ok", "deviations": ""}]
        N._write_progress("t5", batches)

        progress = final / "PROGRESS-t5.md"
        text = progress.read_text()
        assert "Verbatim debate archive: DEBATE-t5-full.md" in text
        # The table is still intact.
        assert "| 1 | test scope | DONE | ok |  |" in text

    def test_backstop_no_pointer_when_archive_absent(self, monkeypatch, tmp_path):
        debates, final = _setup_env(monkeypatch, tmp_path)
        # No archive file.
        assert not (debates / "DEBATE-t6-full.md").exists()

        batches = [{"n": 1, "scope": "test scope", "status": "DONE",
                    "outcome": "ok", "deviations": ""}]
        N._write_progress("t6", batches)

        progress = final / "PROGRESS-t6.md"
        text = progress.read_text()
        assert "Verbatim debate archive" not in text

    def test_backstop_does_not_duplicate_pointer(self, monkeypatch, tmp_path):
        debates, final = _setup_env(monkeypatch, tmp_path)
        (debates / "DEBATE-t7-full.md").write_text("verbatim debate content\n")

        batches = [{"n": 1, "scope": "x", "status": "DONE",
                    "outcome": "", "deviations": ""}]
        N._write_progress("t7", batches)
        N._write_progress("t7", batches)

        progress = final / "PROGRESS-t7.md"
        text = progress.read_text()
        assert text.count("Verbatim debate archive: DEBATE-t7-full.md") == 1


# --- run.py status output (item 36) -----------------------------------------


class _FakeSnap:
    """Minimal stand-in for a langgraph StateSnapshot."""
    created_at = "2026-01-01T00:00:00Z"
    next = None
    interrupts = []
    values = {}


class _FakeGraph:
    def get_state(self, cfg):
        return _FakeSnap()


class TestStatusOutput:
    def test_status_prints_archive_line_when_archive_exists(self, monkeypatch, tmp_path):
        debates, final = _setup_env(monkeypatch, tmp_path)
        # Create the archive file.
        (debates / "DEBATE-t8-full.md").write_text("verbatim debate content\n")
        # Ensure no current.json so _liveness_warning returns None.
        # (C.METRICS is already tmp_path/metrics, which is empty of current.json.)

        monkeypatch.setattr(sys, "argv", ["run.py", "status", "t8"])
        monkeypatch.setattr(run_mod, "open_checkpointer",
                            lambda: contextlib.nullcontext(None))
        monkeypatch.setattr(run_mod, "build_graph", lambda cp=None: _FakeGraph())
        monkeypatch.setattr(run_mod, "_liveness_warning", lambda: None)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = run_mod.main()

        assert rc == 0
        out = buf.getvalue()
        assert "verbatim debate archive: DEBATE-t8-full.md" in out

    def test_status_no_archive_line_when_archive_absent(self, monkeypatch, tmp_path):
        debates, final = _setup_env(monkeypatch, tmp_path)
        # No archive file.
        assert not (debates / "DEBATE-t9-full.md").exists()

        monkeypatch.setattr(sys, "argv", ["run.py", "status", "t9"])
        monkeypatch.setattr(run_mod, "open_checkpointer",
                            lambda: contextlib.nullcontext(None))
        monkeypatch.setattr(run_mod, "build_graph", lambda cp=None: _FakeGraph())
        monkeypatch.setattr(run_mod, "_liveness_warning", lambda: None)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = run_mod.main()

        assert rc == 0
        out = buf.getvalue()
        assert "verbatim debate archive" not in out
