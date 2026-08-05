"""CLI ↔ Discord pending-answer handoff."""
from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pipeline_graph import config as C
from pipeline_graph import pending_answer as PA


@pytest.fixture
def metrics(tmp_path, monkeypatch):
    m = tmp_path / "metrics"
    m.mkdir()
    monkeypatch.setattr(C, "METRICS", m)
    return m


class TestPendingAnswerFile:
    def test_write_take_roundtrip(self, metrics):
        PA.write_pending_answer("042", "ok", source="discord")
        path = metrics / "pending-answer-042.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["answer"] == "ok"
        assert data["source"] == "discord"
        assert PA.take_pending_answer("042") == "ok"
        assert not path.exists()
        assert PA.take_pending_answer("042") is None

    def test_clear(self, metrics):
        PA.write_pending_answer("042", "skip")
        PA.clear_pending_answer("042")
        assert PA.take_pending_answer("042") is None

    def test_live_session_pid_matches_alive(self, metrics):
        (metrics / "current.json").write_text(json.dumps({
            "task": "042", "pid": os.getpid(), "step": "escalate",
        }))
        assert PA.live_session_pid("042") == os.getpid()
        assert PA.live_session_pid("099") is None

    def test_live_session_pid_ignores_idle(self, metrics):
        (metrics / "current.json").write_text(json.dumps({
            "task": "042", "pid": os.getpid(), "idle": True, "why": "paused",
        }))
        assert PA.live_session_pid("042") is None

    def test_live_session_pid_ignores_dead(self, metrics):
        (metrics / "current.json").write_text(json.dumps({
            "task": "042", "pid": 2_000_000_001, "step": "escalate",
        }))
        assert PA.live_session_pid("042") is None

    def test_begin_pause_wait_drops_stale_keeps_fresh(self, metrics):
        import time
        PA.write_pending_answer("042", "stale")
        time.sleep(0.01)
        PA.begin_pause_wait("042")
        assert PA.take_pending_answer("042") is None  # stale dropped
        PA.write_pending_answer("042", "fresh")
        assert PA.take_pending_answer("042") == "fresh"
        PA.end_pause_wait("042")
        assert not PA.pause_wait_path("042").exists()


class TestTtyPickPending:
    def test_tty_pick_takes_pending_before_stdin(self, metrics, monkeypatch):
        import run as run_mod
        from pipeline_graph.nodes.common import _canonical_key

        PA.write_pending_answer("pa1", "skip")
        # Empty stdin would EOF — pending must win first.
        monkeypatch.setattr("sys.stdin", MagicMock(fileno=MagicMock(
            side_effect=ValueError("no fd")), readline=MagicMock(return_value="")))
        opts = [{"key": "ok", "label": "go"}, {"key": "skip", "label": "stop"}]
        data = {"hint": "ok", "stage": "x"}
        ans = run_mod._tty_pick(opts, data, pending_task_id="pa1",
                                eof_raises=True, render=False)
        assert _canonical_key(ans) == "skip"


class TestBotDeliverAnswer:
    def test_deliver_to_live_session_skips_cli(self, metrics, monkeypatch):
        pytest.importorskip("discord")
        import bot.bot as bot

        (metrics / "current.json").write_text(json.dumps({
            "task": "042", "pid": os.getpid(), "step": "escalate",
        }))
        # Align bot's view of metrics with pipeline_graph.config.METRICS.
        # pending_answer uses C.METRICS from pipeline_graph; already patched.

        called = []

        async def fake_cli(*args):
            called.append(args)
            return "should-not-run"

        monkeypatch.setattr(bot, "_cli", fake_cli)

        async def _consume():
            await asyncio.sleep(0.1)
            assert PA.take_pending_answer("042") == "ok"

        async def _main():
            consumer = asyncio.create_task(_consume())
            out = await bot._deliver_answer("042", "ok")
            await consumer
            return out

        out = asyncio.run(_main())
        assert called == []
        assert "delivered to live CLI" in out

    def test_deliver_without_session_calls_resume(self, metrics, monkeypatch):
        pytest.importorskip("discord")
        import bot.bot as bot

        (metrics / "current.json").write_text(json.dumps({
            "idle": True, "task": "042", "why": "paused",
        }))

        async def fake_cli(*args):
            return "resumed-ok"

        monkeypatch.setattr(bot, "_cli", fake_cli)
        out = asyncio.run(bot._deliver_answer("042", "ok"))
        assert out == "resumed-ok"
        assert not PA.pending_answer_path("042").exists()

    def test_deliver_live_timeout_never_spawns_resume(self, metrics, monkeypatch):
        """Regression: TASK-030 dual-driver. Live PID + unread file → refuse."""
        pytest.importorskip("discord")
        import bot.bot as bot

        (metrics / "current.json").write_text(json.dumps({
            "task": "042", "pid": os.getpid(), "step": "escalate",
        }))
        called = []

        async def fake_cli(*args):
            called.append(args)
            return "MUST-NOT-RUN"

        monkeypatch.setattr(bot, "_cli", fake_cli)
        # Shrink wait loop so the test finishes quickly.
        real_sleep = asyncio.sleep

        async def fast_sleep(_):
            await real_sleep(0)

        monkeypatch.setattr(bot.asyncio, "sleep", fast_sleep)
        out = asyncio.run(bot._deliver_answer("042", "approve"))
        assert called == [], f"spawned resume despite live PID: {called}"
        assert "REFUSED second driver" in out
        assert not PA.pending_answer_path("042").exists()

    def test_deliver_after_session_dies_may_resume(self, metrics, monkeypatch):
        pytest.importorskip("discord")
        import bot.bot as bot

        # Start as live, then flip to idle mid-wait → safe to resume once.
        (metrics / "current.json").write_text(json.dumps({
            "task": "042", "pid": os.getpid(), "step": "escalate",
        }))
        called = []

        async def fake_cli(*args):
            called.append(args)
            return "resume-after-death"

        monkeypatch.setattr(bot, "_cli", fake_cli)
        real_sleep = asyncio.sleep
        n = {"i": 0}

        async def die_then_sleep(_):
            n["i"] += 1
            if n["i"] == 2:
                (metrics / "current.json").write_text(json.dumps({
                    "idle": True, "task": "042", "why": "paused",
                }))
            await real_sleep(0)

        monkeypatch.setattr(bot.asyncio, "sleep", die_then_sleep)
        out = asyncio.run(bot._deliver_answer("042", "ok"))
        assert called == [("resume", "042", "--answer", "ok")]
        assert out == "resume-after-death"
