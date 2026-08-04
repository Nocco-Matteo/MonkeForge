"""Tests for FINAL-020 batch 1: Discord narration formatter + milestones
default + bot/webhook readiness handshake (C1–C21) + docs + tests.

Covers the conformance checklist items 1–39:
  - ``discord_format.py``: pure formatter, no ``discord`` import; four-beat
    titles for the milestone kinds; ``humanize_error`` strips tracebacks;
    ``_monke_name`` never raises on an unknown role.
  - ``events.py``: ``NOTIFY_LEVEL`` defaults to ``milestones``; ``MILESTONES``
    includes ``agent_start``/``agent_end``; ``_should_notify`` gains a
    ``step`` parameter and suppresses ``escalate`` step noise + ``run_paused``
    when the bot is alive; ``_bot_alive`` checks pid + sentinel; ``emit``
    routes error kinds through ``humanize_error`` and uses
    ``format_discord_line``.
  - ``run.py``: three ``PIPELINE_NOTIFY_LEVEL`` defaults are ``milestones``;
    ``_ensure_bot`` clears ``.bot.ready`` before ``Popen`` and passes
    ``PIPELINE_DOCS_DIR`` in the child env.
  - ``bot/bot.py``: singleton ``_poller_task``, ``on_ready`` guard,
    pidfile at ``EVENTS_LOG.parent``, readiness sentinel, C21 first-iteration
    suppression, embed field order, ``/help`` command.

Discord-dependent tests (anything that imports ``bot/bot.py``) are each
preceded by ``pytest.importorskip("discord")`` so the suite stays green on
a host without ``discord.py`` (the formatter / events / run.py tests run
unconditionally).
"""
import importlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from pipeline_graph import config as C
from pipeline_graph import events as ev
from pipeline_graph import discord_format as df


# ---------------------------------------------------------------------------
# Formatter tests (no discord import needed)
# ---------------------------------------------------------------------------

class TestFormatDiscordLine:
    def test_format_four_beats(self):
        """run_start / agent_start / agent_end / run_end produce four-beat
        narration titles — never the raw ``TASK-{t} · {step or kind}`` form."""
        # format_discord_line(kind, task, role, step, msg, **extra)
        cases = [
            ("run_start", "007", "COUNCIL", "", "go", {}),
            ("agent_start", "007", "PROPOSER", "plan", "convening", {}),
            ("agent_end", "007", "PROPOSER", "plan", "done", {"duration_ms": 42000}),
            ("run_end", "007", "COUNCIL", "wrap_up", "complete", {}),
        ]
        for kind, task, role, step, msg, extra in cases:
            title, desc = df.format_discord_line(kind, task, role, step, msg, **extra)
            raw = f"TASK-{task} · {step or kind}"
            assert title != raw, f"{kind}: title is still raw {raw!r}"
            assert isinstance(title, str) and isinstance(desc, str)
            assert title  # non-empty

    def test_format_unknown_role_no_raise(self):
        """An unrecognized role renders as the raw role string, never raises."""
        # format_discord_line(kind, task, role, step, msg)
        title, desc = df.format_discord_line(
            "agent_start", "007", "BOGUS_ROLE", "plan", "convening")
        assert "BOGUS_ROLE" in title  # _monke_name falls back to the role itself
        # A known role resolves to the monke name, not the raw role.
        title2, _ = df.format_discord_line(
            "agent_start", "007", "PROPOSER", "plan", "convening")
        assert "PROPOSER" not in title2  # "Wise Orangutan" instead

    def test_format_elapsed(self):
        """agent_end surfaces elapsed seconds from duration_ms in the description."""
        _, desc = df.format_discord_line(
            "agent_end", "007", "PROPOSER", "plan", "done",
            duration_ms=65000)
        assert "65s" in desc
        # Without duration_ms, no elapsed suffix is appended.
        _, desc2 = df.format_discord_line(
            "agent_end", "007", "PROPOSER", "plan", "done")
        assert "65s" not in desc2

    def test_agent_end_title_includes_verdict_and_blockers(self):
        """Return beat title carries VERDICT / blocker count when present."""
        title, desc = df.format_discord_line(
            "agent_end", "024", "PLAN_REVIEWER", "debate",
            "PLAN_REVIEWER/claude exit=0 in 12s, 80 bytes, health=ok"
            " — REJECT, 2 blocker(s)",
            duration_ms=12000, verdict="REJECT", blockers=2,
        )
        assert "REJECT" in title
        assert "2 blocker(s)" in title
        # Summary description stays count-only (no claim bullets).
        assert "•" not in desc
        title_ok, _ = df.format_discord_line(
            "agent_end", "024", "PLAN_REVIEWER", "debate",
            "… health=ok — APPROVE",
            verdict="APPROVE", blockers=0,
        )
        assert "APPROVE" in title_ok
        assert "blocker" not in title_ok


class TestPackBlockerBodies:
    def test_empty(self):
        assert df.pack_blocker_bodies([]) == []

    def test_single_fits(self):
        bodies = df.pack_blocker_bodies(["missing tests"])
        assert bodies == ["1. missing tests"]

    def test_numbers_before_blocker_ids(self):
        bodies = df.pack_blocker_bodies([
            "B4: Top-level --no-input still allows…",
            "B5: The session loop cannot detect…",
        ])
        assert len(bodies) == 1
        assert bodies[0].startswith("1. B4:")
        assert "\n2. B5:" in bodies[0]

    def test_packs_until_max_then_new_message(self):
        # Two claims that individually fit but together exceed a tiny max.
        a = "A" * 20
        b = "B" * 20
        bodies = df.pack_blocker_bodies([a, b], max_len=40)
        assert len(bodies) == 2
        assert bodies[0] == f"1. {a}"
        assert bodies[1] == f"2. {b}"

    def test_many_small_claims_one_body(self):
        claims = [f"c{i}" for i in range(5)]
        bodies = df.pack_blocker_bodies(claims, max_len=500)
        assert len(bodies) == 1
        for i, c in enumerate(claims, 1):
            assert f"{i}. {c}" in bodies[0]

    def test_never_truncates_claim_mid_text(self):
        claim = "exact claim text that must survive intact"
        bodies = df.pack_blocker_bodies([claim, "other"], max_len=60)
        joined = "\n".join(bodies)
        assert claim in joined
        assert "exact claim text that must sur…" not in joined

    def test_detail_title_no_part_when_single(self):
        t = df.format_blocker_detail_title(
            "PLAN_REVIEWER", "027", "debate-r5-tech", 1, 1)
        assert t.startswith("📋 ")
        assert "blockers" in t
        assert "part " not in t
        assert "(1/1)" not in t
        assert "TASK-027/debate-r5-tech" in t

    def test_detail_title_part_i_of_k_when_split(self):
        t = df.format_blocker_detail_title(
            "PLAN_REVIEWER", "024", "debate-r3-tech", 2, 3)
        assert t.startswith("📋 ")
        assert "part 2/3" in t
        assert "Skeptical Baboon" in t


class TestHumanizeError:
    def test_stalled_no_traceback(self):
        """run_stalled / agent_unhealthy / step_error messages have traceback
        lines stripped and residual markers truncated."""
        raw = ("agent claude crashed\n"
               "Traceback (most recent call last):\n"
               '  File "/foo/bar.py", line 42, in run\n'
               "    raise ValueError(\"bad\")\n"
               "ValueError: bad thing\n"
               "some useful context here")
        cleaned = df.humanize_error(raw)
        assert "Traceback" not in cleaned
        assert 'File "' not in cleaned
        # The standalone "ValueError: bad thing" line (exception-class line
        # start) is dropped.
        assert "\nValueError: bad thing" not in cleaned
        assert "some useful context here" in cleaned

    def test_residual_marker_truncated(self):
        """A Traceback fragment that wrapped onto a continued line is cut."""
        raw = "useful info Traceback (most recent call last) more stuff"
        cleaned = df.humanize_error(raw)
        assert "more stuff" not in cleaned
        assert "useful info" in cleaned

    def test_empty_msg(self):
        assert df.humanize_error("") == ""
        assert df.humanize_error(None) == ""


# ---------------------------------------------------------------------------
# events.py: NOTIFY_LEVEL default, MILESTONES, _should_notify, _bot_alive
# ---------------------------------------------------------------------------

class TestNotifyLevelDefault:
    def test_default_level_milestones(self, monkeypatch):
        """With no PIPELINE_NOTIFY_LEVEL in the env, events.NOTIFY_LEVEL is
        'milestones' (not the old 'all')."""
        monkeypatch.delenv("PIPELINE_NOTIFY_LEVEL", raising=False)
        fresh = importlib.reload(ev)
        try:
            assert fresh.NOTIFY_LEVEL == "milestones"
        finally:
            importlib.reload(ev)  # restore for other tests


class TestShouldNotify:
    def test_all_level_pushes_step_start(self, monkeypatch):
        """In 'all' mode, step_start pushes (it is not in MILESTONES but
        NOTIFY_LEVEL == 'all' lets it through)."""
        monkeypatch.setattr(ev, "NOTIFY_LEVEL", "all")
        monkeypatch.setattr(ev, "_bot_alive", lambda: False)
        assert ev._should_notify("step_start", None, step="plan") is True

    def test_escalate_step_suppressed(self, monkeypatch):
        """step_start/step_end with step == 'escalate' is suppressed at every
        non-silent level — the human-facing surface is the run_paused that
        follows, not the escalate node's own bookkeeping."""
        for level in ("milestones", "all"):
            monkeypatch.setattr(ev, "NOTIFY_LEVEL", level)
            assert ev._should_notify("step_start", None, step="escalate") is False
            assert ev._should_notify("step_end", None, step="escalate") is False

    def test_milestones_suppresses_step_start(self, monkeypatch):
        """In 'milestones' mode, step_start (not in MILESTONES) does not push."""
        monkeypatch.setattr(ev, "NOTIFY_LEVEL", "milestones")
        monkeypatch.setattr(ev, "_bot_alive", lambda: False)
        assert ev._should_notify("step_start", None, step="plan") is False

    def test_milestones_pushes_agent_start(self, monkeypatch):
        """agent_start is in MILESTONES (C2) — pushes in milestones mode."""
        monkeypatch.setattr(ev, "NOTIFY_LEVEL", "milestones")
        monkeypatch.setattr(ev, "_bot_alive", lambda: False)
        assert ev._should_notify("agent_start", None, step="plan") is True

    def test_degraded_not_a_milestone(self, monkeypatch):
        """Condenser ``degraded`` stays journal-only under milestones (folded
        into agent_start description); notify=False also hard-suppresses."""
        assert "degraded" not in ev.MILESTONES
        monkeypatch.setattr(ev, "NOTIFY_LEVEL", "milestones")
        monkeypatch.setattr(ev, "_bot_alive", lambda: False)
        assert ev._should_notify("degraded", None, step="debate") is False
        assert ev._should_notify("degraded", False, step="debate") is False
        monkeypatch.setattr(ev, "NOTIFY_LEVEL", "all")
        # Explicit notify=False still wins over ``all``.
        assert ev._should_notify("degraded", False, step="debate") is False


class TestBotAlive:
    def test_run_paused_suppressed_when_bot_ready(self, monkeypatch, tmp_path):
        """run_paused is suppressed when _bot_alive() is True (pid alive +
        .bot.ready sentinel exists)."""
        metrics = tmp_path / "metrics"
        metrics.mkdir()
        monkeypatch.setattr(C, "METRICS", metrics)
        # Write a pidfile with our own pid (alive) and a ready sentinel.
        (metrics / ".bot.pid").write_text(str(os.getpid()))
        (metrics / ".bot.ready").write_text("")
        assert ev._bot_alive() is True
        monkeypatch.setattr(ev, "NOTIFY_LEVEL", "milestones")
        assert ev._should_notify("run_paused", None, step="escalate") is False

    def test_run_paused_posted_during_bot_startup(self, monkeypatch, tmp_path):
        """run_paused is posted when the bot has spawned (pidfile exists) but
        has NOT yet written .bot.ready — the webhook stays as the urgent
        surface during the bot's catch-up iteration (C17/C21)."""
        metrics = tmp_path / "metrics"
        metrics.mkdir()
        monkeypatch.setattr(C, "METRICS", metrics)
        (metrics / ".bot.pid").write_text(str(os.getpid()))
        # No .bot.ready sentinel.
        assert ev._bot_alive() is False
        monkeypatch.setattr(ev, "NOTIFY_LEVEL", "milestones")
        assert ev._should_notify("run_paused", None, step="escalate") is True

    def test_bot_alive_false_when_pid_dead(self, monkeypatch, tmp_path):
        """A stale pid (process gone) → _bot_alive False even if .bot.ready
        exists."""
        metrics = tmp_path / "metrics"
        metrics.mkdir()
        monkeypatch.setattr(C, "METRICS", metrics)
        (metrics / ".bot.pid").write_text("999999999")
        (metrics / ".bot.ready").write_text("")
        assert ev._bot_alive() is False

    def test_bot_alive_false_when_no_pidfile(self, monkeypatch, tmp_path):
        metrics = tmp_path / "metrics"
        metrics.mkdir()
        monkeypatch.setattr(C, "METRICS", metrics)
        assert ev._bot_alive() is False


# ---------------------------------------------------------------------------
# emit() integration: humanize_error routing + format_discord_line usage
# ---------------------------------------------------------------------------

class TestEmitRouting:
    def _setup(self, monkeypatch, tmp_path):
        metrics = tmp_path / "metrics"
        metrics.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(C, "METRICS", metrics)
        monkeypatch.setattr(ev, "EVENTS_LOG", metrics / "events.jsonl")
        monkeypatch.setattr(ev, "PIPELINE_LOG", metrics / "pipeline.log")
        monkeypatch.setattr(ev, "NOTIFY_LEVEL", "milestones")
        monkeypatch.setattr(ev, "_bot_alive", lambda: False)
        pushes = []
        monkeypatch.setattr(ev, "_push", lambda *a, **k: pushes.append((a, k)))
        return pushes

    def test_stalled_no_traceback_in_push(self, monkeypatch, tmp_path):
        """emit('run_stalled') routes msg through humanize_error before
        formatting — the pushed description has no Traceback lines."""
        pushes = self._setup(monkeypatch, tmp_path)
        ev.emit("run_stalled", "007", "plan",
                "agent crashed\nTraceback (most recent call last):\n"
                '  File "/x.py", line 1, in run\n'
                "ValueError: boom")
        assert len(pushes) == 1
        (args, _kw) = pushes[0]
        # _push(title, desc, prio, role=role) — role is keyword.
        title, desc, _prio = args
        assert "Traceback" not in desc
        assert 'File "' not in desc

    def test_continue_resume_one_post(self, monkeypatch, tmp_path):
        """A continue/resume answer yields exactly one escalation_resolved
        push (not zero, not two) — the webhook posts the resolution once."""
        pushes = self._setup(monkeypatch, tmp_path)
        ev.emit("escalation_resolved", "007", "escalate",
                "answered 'continue'; was: debate stuck")
        assert len(pushes) == 1

    def test_emit_uses_format_discord_line(self, monkeypatch, tmp_path):
        """emit() calls _push with a title from format_discord_line, not the
        old literal ``TASK-{t} · {step or kind}``."""
        pushes = self._setup(monkeypatch, tmp_path)
        ev.emit("agent_start", "007", "plan", "convening", role="PROPOSER")
        assert len(pushes) == 1
        (args, _kw) = pushes[0]
        title = args[0]
        assert title != "TASK-007 · plan"  # formatted, not raw
        assert "Wise Orangutan" in title  # PROPOSER's monke name

    def test_agent_end_reject_pushes_detail_then_summary(
            self, monkeypatch, tmp_path):
        """REJECT with blocker_claims → detail (📋) then summary (📨)."""
        pushes = self._setup(monkeypatch, tmp_path)
        claims = ["B4: missing tests for gate", "B5: vague acceptance criteria"]
        ev.emit(
            "agent_end", "027", "debate-r5-tech",
            "PLAN_REVIEWER/claude exit=0 — REJECT, 2 blocker(s)",
            role="PLAN_REVIEWER",
            verdict="REJECT",
            blockers=2,
            blocker_claims=claims,
            duration_ms=12000,
        )
        assert len(pushes) == 2  # 1 detail + summary
        detail_title = pushes[0][0][0]
        assert detail_title.startswith("📋 ")
        assert "blockers" in detail_title
        assert "part " not in detail_title  # single part → no i/k
        detail_body = pushes[0][0][1]
        assert "1. B4:" in detail_body
        assert "2. B5:" in detail_body
        summary_title = pushes[1][0][0]
        assert summary_title.startswith("📨 ")
        assert "REJECT" in summary_title
        assert "•" not in pushes[1][0][1]
        assert "1. B4" not in pushes[1][0][1]

    def test_agent_end_approve_no_detail(self, monkeypatch, tmp_path):
        pushes = self._setup(monkeypatch, tmp_path)
        ev.emit(
            "agent_end", "024", "debate-r3-tech",
            "… — APPROVE",
            role="PLAN_REVIEWER",
            verdict="APPROVE",
            blockers=0,
        )
        assert len(pushes) == 1
        assert pushes[0][0][0].startswith("📨 ")

    def test_agent_end_detail_splits_with_part_i_of_k(self, monkeypatch, tmp_path):
        """When claims cannot fit one embed, detail titles use part i/k."""
        pushes = self._setup(monkeypatch, tmp_path)
        big = "X" * 2000
        ev.emit(
            "agent_end", "024", "debate-r3-tech",
            "… — REJECT, 2 blocker(s)",
            role="PLAN_REVIEWER",
            verdict="REJECT",
            blockers=2,
            blocker_claims=[big, big],
        )
        # 2 detail parts + summary
        assert len(pushes) == 3
        assert pushes[0][0][0].startswith("📋 ")
        assert "part 1/2" in pushes[0][0][0]
        assert "part 2/2" in pushes[1][0][0]
        assert pushes[2][0][0].startswith("📨 ")
        assert "1. " in pushes[0][0][1]
        assert "2. " in pushes[1][0][1]


# ---------------------------------------------------------------------------
# run.py: _ensure_bot ready-unlink + env alignment
# ---------------------------------------------------------------------------

class TestEnsureBot:
    def _setup(self, monkeypatch, tmp_path):
        metrics = tmp_path / "metrics"
        metrics.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(C, "METRICS", metrics)
        monkeypatch.setattr(C, "DOCS", tmp_path / "docs" / "myrepo")
        monkeypatch.setenv("PIPELINE_BOT_AUTOSTART", "1")
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake")
        import run as run_mod
        monkeypatch.setattr(run_mod, "_MF_ROOT", tmp_path)
        bot_py = tmp_path / "bot" / "bot.py"
        bot_py.parent.mkdir(parents=True, exist_ok=True)
        bot_py.write_text("# stub")
        return run_mod, metrics

    def test_stale_ready_cleared_before_spawn(self, monkeypatch, tmp_path):
        """_ensure_bot unlinks .bot.ready BEFORE calling Popen — a stale
        sentinel from a previous bot does not survive into the new bot's
        catch-up window (C15)."""
        run_mod, metrics = self._setup(monkeypatch, tmp_path)
        ready = metrics / ".bot.ready"
        ready.write_text("stale")
        call_order = []

        class FakeProc:
            pid = 12345

        def fake_popen(*args, **kwargs):
            call_order.append(("popen", ready.exists()))
            return FakeProc()

        monkeypatch.setattr(run_mod.subprocess, "Popen", fake_popen)
        run_mod._ensure_bot()
        assert ("popen", False) in call_order  # ready was gone before Popen
        assert not ready.exists()

    def test_relative_docs_dir_aligned(self, monkeypatch, tmp_path):
        """_ensure_bot passes PIPELINE_DOCS_DIR (resolved absolute) in the
        child env so the bot reads the same docs dir the pipeline uses (C14)."""
        run_mod, metrics = self._setup(monkeypatch, tmp_path)
        captured_env = {}

        class FakeProc:
            pid = 12345

        def fake_popen(*args, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            return FakeProc()

        monkeypatch.setattr(run_mod.subprocess, "Popen", fake_popen)
        run_mod._ensure_bot()
        assert "PIPELINE_DOCS_DIR" in captured_env
        expected = str((tmp_path / "docs" / "myrepo").resolve())
        assert captured_env["PIPELINE_DOCS_DIR"] == expected


# ---------------------------------------------------------------------------
# bot/bot.py tests (Discord-dependent — each skips if discord.py is absent)
# ---------------------------------------------------------------------------

def _import_bot():
    """Import bot/bot.py, ensuring the MonkeForge root is on sys.path."""
    mf_root = Path(__file__).resolve().parents[1]
    if str(mf_root) not in sys.path:
        sys.path.insert(0, str(mf_root))
    import bot.bot as bot_mod
    return bot_mod


class TestOnReadySingleton:
    def test_on_ready_global_poller_task(self):
        pytest.importorskip("discord")
        bot = _import_bot()
        # Module-level _poller_task exists and is None (or a task) — and
        # on_ready's first statement is `global _poller_task`.
        import inspect
        src = inspect.getsource(bot.on_ready)
        lines = [l.strip() for l in src.splitlines() if l.strip()
                 and not l.strip().startswith("#")]
        # First statement after the docstring/signature is `global _poller_task`.
        # Find the first non-decorator, non-def, non-docstring line.
        body_started = False
        first_stmt = None
        for line in lines:
            if line.startswith("async def on_ready"):
                body_started = True
                continue
            if not body_started:
                continue
            if line.startswith('"""') or line.startswith("'''"):
                continue
            first_stmt = line
            break
        assert first_stmt == "global _poller_task"
        assert hasattr(bot, "_poller_task")

    def test_reconnect_does_not_clear_ready_or_spawn_second_poller(
            self, monkeypatch, tmp_path):
        """On a reconnect (on_ready fires again with _poller_task still
        alive), .bot.ready is NOT unlinked and create_task is NOT called
        again (C16/C19)."""
        pytest.importorskip("discord")
        bot = _import_bot()
        metrics = tmp_path / "metrics"
        metrics.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(bot.C, "EVENTS_LOG", metrics / "events.jsonl")
        monkeypatch.setattr(bot.C, "STATE_FILE", metrics / ".discord-bot-offset")
        ready = metrics / ".bot.ready"
        ready.write_text("alive")

        # Simulate a live poller task (not done).
        fake_task = MagicMock()
        fake_task.done.return_value = False
        monkeypatch.setattr(bot, "_poller_task", fake_task)

        create_called = False
        monkeypatch.setattr(bot.client, "loop", MagicMock(create_task=MagicMock()))

        async def fake_sync():
            pass
        monkeypatch.setattr(bot.tree, "sync", fake_sync)

        import asyncio
        asyncio.run(bot.on_ready())
        assert ready.exists(), "reconnect must NOT clear .bot.ready"
        assert bot._poller_task is fake_task, "reconnect must NOT replace the poller task"

    def test_poller_relaunch_after_early_exit(self, monkeypatch, tmp_path):
        """If the previous poller task is done (early exit), on_ready relaunches
        a new one and clears .bot.ready before spawning (C16)."""
        pytest.importorskip("discord")
        bot = _import_bot()
        metrics = tmp_path / "metrics"
        metrics.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(bot.C, "EVENTS_LOG", metrics / "events.jsonl")
        monkeypatch.setattr(bot.C, "STATE_FILE", metrics / ".discord-bot-offset")
        ready = metrics / ".bot.ready"
        ready.write_text("stale")

        # Previous task is done.
        done_task = MagicMock()
        done_task.done.return_value = True
        monkeypatch.setattr(bot, "_poller_task", done_task)

        new_task = MagicMock()
        fake_loop = MagicMock()
        fake_loop.create_task.return_value = new_task
        monkeypatch.setattr(bot.client, "loop", fake_loop)

        async def fake_sync():
            pass
        monkeypatch.setattr(bot.tree, "sync", fake_sync)

        import asyncio
        asyncio.run(bot.on_ready())
        assert not ready.exists(), "stale .bot.ready must be cleared before relaunch"
        assert bot._poller_task is new_task

    def test_stale_ready_cleared_on_restart_or_poller_exit(self, monkeypatch, tmp_path):
        """When _poller_task is None (fresh start) or done (poller exited),
        on_ready clears .bot.ready before spawning (C15/C17)."""
        pytest.importorskip("discord")
        bot = _import_bot()
        metrics = tmp_path / "metrics"
        metrics.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(bot.C, "EVENTS_LOG", metrics / "events.jsonl")
        monkeypatch.setattr(bot.C, "STATE_FILE", metrics / ".discord-bot-offset")
        ready = metrics / ".bot.ready"
        ready.write_text("stale")

        monkeypatch.setattr(bot, "_poller_task", None)
        new_task = MagicMock()
        fake_loop = MagicMock()
        fake_loop.create_task.return_value = new_task
        monkeypatch.setattr(bot.client, "loop", fake_loop)

        async def fake_sync():
            pass
        monkeypatch.setattr(bot.tree, "sync", fake_sync)

        import asyncio
        asyncio.run(bot.on_ready())
        assert not ready.exists()


class TestEscalationEmbed:
    def test_escalation_embed_fields(self, monkeypatch, tmp_path):
        """_post_escalation's embed field order is:
        description(reason) → task-id → where/context → optional
        hint/plan/final/batches → blockers → triage sub-fields → options →
        resume command. The options field is named 'options' (not 'answers')
        and the resume field contains './run.py resume {tid} --answer'.
        """
        pytest.importorskip("discord")
        bot = _import_bot()

        captured = {}

        class FakeEmbed:
            def __init__(self, **kw):
                captured["title"] = kw.get("title", "")
                captured["description"] = kw.get("description", "")
                self.fields = []

            def add_field(self, name, value, inline=False):
                self.fields.append((name, value, inline))

        class FakeView:
            def __init__(self, *a, **k):
                pass

        class FakeChannel:
            async def send(self, embed=None, view=None, files=None):
                captured["embed"] = embed

        monkeypatch.setattr(bot.discord, "Embed", FakeEmbed)
        monkeypatch.setattr(bot, "AnswerView", FakeView)

        rec = {
            "task": "042",
            "step": "escalate",
            "msg": "debate stuck: round cap reached",
            "options": [{"key": "continue", "label": "extend debate"},
                        {"key": "redo", "label": "redo from debate"}],
            "context": "debate round 2",
            "hint": "continue",
            "plan": "PLAN-042.md",
            "final": "FINAL-042.md",
            "batches": ["batch 1", "batch 2"],
            "blockers": "[BLOCKER] thing",
            "triage": {"mode": "stuck", "blocker_counts": [3, 2],
                       "repeated": ["a"], "new": ["b"],
                       "rationale": "churning"},
        }
        import asyncio
        asyncio.run(bot._post_escalation(FakeChannel(), rec))

        embed = captured["embed"]
        field_names = [f[0] for f in embed.fields]
        # Required order: task → where → recommended → plan → final → batches
        # → blockers → triage mode → blocker trend → repeated/new → rationale
        # → options → resume
        expected_order = [
            "task", "where", "recommended", "plan", "final", "batches",
            "blockers", "triage mode", "blocker trend", "repeated/new",
            "rationale", "options", "resume",
        ]
        assert field_names == expected_order, f"field order: {field_names}"
        # 'options' not 'answers'
        assert "answers" not in field_names
        # resume field contains ./run.py resume with --answer
        resume_field = [f for f in embed.fields if f[0] == "resume"][0]
        assert "./run.py resume 042" in resume_field[1]
        assert "--answer" in resume_field[1]
        # description is the reason
        assert captured["description"] == "debate stuck: round cap reached"


class TestBotHelp:
    def test_bot_help_has_url_and_examples(self, monkeypatch):
        """/help response contains the support URL and at least two of:
        resume, --answer, status."""
        pytest.importorskip("discord")
        bot = _import_bot()

        captured = {}

        class FakeResponse:
            async def send_message(self, text, ephemeral=False):
                captured["text"] = text

        class FakeInteraction:
            response = FakeResponse()

        # Find the /help command handler in the tree.
        # The handler is registered via @tree.command(name="help", ...).
        # @tree.command wraps the function in an app_commands.Command, so
        # reach the underlying coroutine via .callback to invoke it directly.
        help_fn = bot._help.callback
        import asyncio
        asyncio.run(help_fn(FakeInteraction()))
        text = captured["text"]
        assert "https://github.com/Nocco-Matteo/MonkeForge" in text
        found = sum(1 for sub in ("resume", "--answer", "status") if sub in text)
        assert found >= 2, f"only {found} of resume/--answer/status found"


class TestPollerC21:
    def test_first_iter_does_not_repost_paused(self, monkeypatch, tmp_path):
        """C21: during the sentinel-establishing catch-up iteration, a
        run_paused record is NOT re-posted by the poller (the webhook already
        posted it because _bot_alive() was false for the whole window).
        run_end IS still posted (dual-channel, D2)."""
        pytest.importorskip("discord")
        bot = _import_bot()

        metrics = tmp_path / "metrics"
        metrics.mkdir(parents=True, exist_ok=True)
        events_log = metrics / "events.jsonl"
        state_file = metrics / ".discord-bot-offset"
        ready_sentinel = metrics / ".bot.ready"

        # Write a log with a run_paused and a run_end that the first
        # iteration will read from the saved offset.
        records = [
            {"ts": "2026-01-01T00:00:00Z", "kind": "run_paused", "task": "042",
             "step": "escalate", "msg": "need you", "options": []},
            {"ts": "2026-01-01T00:00:01Z", "kind": "run_end", "task": "042",
             "step": "wrap_up", "msg": "done"},
        ]
        with events_log.open("w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        # Start from offset 0 so the first iteration reads both records.
        state_file.write_text("0")

        monkeypatch.setattr(bot.C, "EVENTS_LOG", events_log)
        monkeypatch.setattr(bot.C, "STATE_FILE", state_file)
        monkeypatch.setattr(bot.C, "CHANNEL_ID", 123)
        monkeypatch.setattr(bot.C, "POLL_SECONDS", 0)

        posted_escalations = []
        posted_ends = []

        async def fake_post_escalation(channel, rec):
            posted_escalations.append(rec)

        async def fake_post_end(channel, rec):
            posted_ends.append(rec)

        monkeypatch.setattr(bot, "_post_escalation", fake_post_escalation)
        monkeypatch.setattr(bot, "_post_end", fake_post_end)

        class FakeChannel:
            async def send(self, *a, **k):
                pass

        # Make client.is_closed() return True after one iteration so the
        # poller loop exits after processing once.
        call_count = [0]

        def fake_is_closed():
            call_count[0] += 1
            return call_count[0] > 1

        monkeypatch.setattr(bot.client, "is_closed", fake_is_closed)
        monkeypatch.setattr(bot.client, "wait_until_ready", AsyncMock())
        monkeypatch.setattr(bot.client, "get_channel", lambda _id: FakeChannel())
        monkeypatch.setattr(bot.client, "guilds", [MagicMock()])

        import asyncio
        asyncio.run(bot._poller())

        # C21: run_paused was NOT posted during the first iteration.
        assert len(posted_escalations) == 0, \
            "first iteration must not re-post run_paused (C21)"
        # run_end IS posted (not suppressed by C21).
        assert len(posted_ends) == 1, \
            "run_end must still be posted during first iteration (D2)"
        # The sentinel was written after the first iteration.
        assert ready_sentinel.exists(), \
            "sentinel must be written after the first iteration commits the cursor"
