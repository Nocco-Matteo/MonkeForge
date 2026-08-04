"""The single choke point for everything observable: logs and notifications.

Every step boundary, every agent call, every crash and every escalation goes
through `emit()`. That is the whole design: with one entry point a step can no
longer be logged but not notified, or notified with a wording that does not
match what the log says.

Four sinks:
  docs/metrics/events.jsonl    machine-readable, one JSON object per event
  docs/metrics/pipeline.log    human, `tail -f`-friendly
  docs/metrics/journal-<t>.log per-task narrative, written the instant it
                               happens — unlike the graph journal, which only
                               materialises when a node returns and is
                               therefore blind for the whole duration of a
                               40-minute step
  ntfy                         push, via the notify daemon (socket) or spool fallback

The journal file is the answer to "I resume an escalation and cannot see what
it is doing": checkpointed state updates at node boundaries, this does not.
"""
from __future__ import annotations

import json
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import config as C
from .discord_format import format_discord_line, humanize_error
from .event_types import Event

EVENTS_LOG = C.METRICS / "events.jsonl"
PIPELINE_LOG = C.METRICS / "pipeline.log"

# silent    — never push (logs still written)
# milestones— only what a human must act on (DEFAULT: the safe direction is
#             not to flood a phone with step_start/step_end noise)
# all       — every step boundary too (debug opt-in: you asked to see each one)
NOTIFY_LEVEL = os.environ.get("PIPELINE_NOTIFY_LEVEL", "milestones").lower()

# Events that always push, whatever the level (except `silent`): either the run
# needs a human, or it has stopped being a run. ``agent_start`` / ``agent_end``
# are milestones (C2): the four-beat narration (convene → send → return → close)
# is what a human wants on the phone, not per-step noise.
MILESTONES = {
    "run_start", "run_end", "run_paused", "run_stalled",
    "step_error", "escalation_open", "escalation_resolved",
    "batch_done", "intake_questions", "intake_complete",
    "agent_unhealthy", "agent_start", "agent_end",
    # ``degraded`` (e.g. debate condenser) stays in events/journal and still
    # pushes when NOTIFY_LEVEL=all — not a phone milestone. Condenser notes are
    # folded into agent_start description instead.
}

PRIORITY = {
    "run_start": "default",
    "run_end": "high",
    "run_paused": "high",
    "run_stalled": "urgent",
    "step_error": "urgent",
    "escalation_open": "urgent",
    "escalation_resolved": "default",
    "batch_done": "default",
    "degraded": "high",
    "intake_questions": "high",     # the run is blocked until you answer
    "intake_complete": "default",
    "agent_unhealthy": "high",      # an agent errored in-band (exit 0, bad output)
    "step_start": "low",
    "step_end": "low",
    "agent_start": "low",
    "agent_end": "low",
}

_KIND_ROLE: dict[str, str] = {
    "step_start": "COUNCIL",
    "step_end": "COUNCIL",
    "step_error": "COUNCIL",
    "escalation_open": "ESCALATION",
    "escalation_resolved": "COUNCIL",
    "batch_done": "COUNCIL",
    "degraded": "COUNCIL",
    "intake_questions": "INTAKE",
    "intake_complete": "INTAKE",
    "run_end": "COUNCIL",
    "agent_unhealthy": "COUNCIL",
    "agent_start": "COUNCIL",
    "agent_end": "COUNCIL",
}


def _journal_file(task: str) -> Path:
    return C.METRICS / f"journal-{task}.log"


def _bot_alive() -> bool:
    """True only if the Discord bot is BOTH running AND ready.

    Checks ``os.kill(pid, 0)`` on the pid read from ``C.METRICS / ".bot.pid"``
    AND that the readiness sentinel ``C.METRICS / ".bot.ready"`` exists. The
    sentinel is written by the bot's poller only after its first catch-up
    iteration commits the cursor (C17) — so a bot that has spawned but not
    yet drained the log is NOT alive from the webhook's perspective, and the
    webhook keeps posting ``run_paused`` itself (safe direction: over-notify).

    Any OSError / ValueError (missing pidfile, stale pid, unreadable file)
    maps to ``False`` — the webhook posts, never silently drops.
    """
    try:
        pid = int((C.METRICS / ".bot.pid").read_text())
        os.kill(pid, 0)
        return (C.METRICS / ".bot.ready").exists()
    except (OSError, ValueError):
        return False


def _should_notify(kind: str, override: bool | None,
                   step: str = "") -> bool:
    if override is not None:
        return override and NOTIFY_LEVEL != "silent"
    if NOTIFY_LEVEL == "silent":
        return False
    # The internal ``escalate`` node's own step_start/step_end is bookkeeping
    # noise — the human-facing surface is the ``run_paused`` that follows.
    if kind in ("step_start", "step_end") and step == "escalate":
        return False
    # If the interactive bot is up and ready, it posts the escalation card
    # with buttons — the webhook's ``run_paused`` push would be a duplicate
    # card with no buttons. Suppress the webhook side (C9). The bot is the
    # single urgent surface when it is alive; the webhook stays as the
    # fallback when it is not.
    if kind == "run_paused" and _bot_alive():
        return False
    if kind in MILESTONES:
        return True
    return NOTIFY_LEVEL == "all"


def _push(title: str, msg: str, prio: str, role: str = "") -> None:
    """Send a notification: try the daemon socket, fall back to spool.
    A failed push must never take the pipeline down."""
    payload = json.dumps({"title": title, "msg": msg, "prio": prio, "role": role})
    # 1. Try the notify daemon via unix socket.
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(str(C.NOTIFY_SOCKET))
            s.sendall(payload.encode("utf-8"))
        return
    except OSError:
        pass  # daemon not running — fall through

    # 2. Spool to disk: the daemon will pick it up on next startup.
    spool_dir = C.METRICS / "notify.spool"
    try:
        if spool_dir.exists() and not spool_dir.is_dir():
            spool_dir.unlink()
        spool_dir.mkdir(parents=True, exist_ok=True)
        spool_file = spool_dir / f"{int(time.time() * 1000)}.json"
        spool_file.write_text(payload)
        return
    except OSError:
        pass

    # 3. Nothing more we can do — the daemon is down and spool failed.
    # The event is already in events.jsonl; the daemon will pick up the spool
    # on next startup.


def emit(kind: str, task: str = "?", step: str = "", msg: str = "",
         notify: bool | None = None, prio: str | None = None, **extra) -> None:
    """Record one event to every sink. Never raises."""
    if isinstance(kind, Event):
        ev = kind
        kind, task, step, msg = ev.KIND, ev.task, ev.step, ev.msg
        notify, prio = ev.notify, ev.prio
        extra = {k: v for k, v in ev.to_record().items()
                 if k not in ("kind", "task", "step", "msg")}
    now = datetime.now(timezone.utc)
    record = {"ts": now.isoformat(), "kind": kind, "task": task,
              "step": step, "msg": msg, **extra}
    clock = now.astimezone().strftime("%H:%M:%S")
    line = f"{clock} {kind:<18} task-{task} {step:<20} {msg}"

    try:
        C.METRICS.mkdir(parents=True, exist_ok=True)
        with EVENTS_LOG.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")
        with PIPELINE_LOG.open("a") as f:
            f.write(line + "\n")
        with _journal_file(task).open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass

    # Synchronous step-event hook (D9): fires immediately after the event is
    # persisted to events.jsonl, BEFORE the (potentially 2s-blocking) notify
    # push, so dispatch/return rendering cannot be delayed by the socket
    # timeout. The hook drains events.jsonl from a shared cursor and renders
    # in true chronological order. A broken UI hook must never fail the graph.
    if kind in ("step_start", "step_end") and _step_hook is not None:
        try:
            _step_hook(task, step, msg, **extra)
        except Exception:
            pass

    if _should_notify(kind, notify, step=step):
        role = extra.get("role", "")
        if not role:
            role = _KIND_ROLE.get(kind, "")
        display_msg = msg
        if kind in ("run_stalled", "agent_unhealthy", "step_error"):
            display_msg = humanize_error(msg)
        # Pass extra without ``role`` (already extracted above) so
        # format_discord_line does not get a duplicate ``role`` kwarg.
        fmt_extra = {k: v for k, v in extra.items() if k != "role"}
        title, description = format_discord_line(
            kind, task, role, step, display_msg, **fmt_extra)
        _push(title, description,
              prio or PRIORITY.get(kind, "default"),
              role=role)


def read_journal(task: str, n: int = 20) -> list[str]:
    """Tail of the live journal — what `status` should show while a step runs."""
    path = _journal_file(task)
    if not path.exists():
        return []
    return path.read_text().splitlines()[-n:]


def read_events(task: str, kinds: set[str] | None = None) -> list[dict]:
    """Structured events for one task, oldest first — the basis of `doctor`."""
    if not EVENTS_LOG.exists():
        return []
    out = []
    for line in EVENTS_LOG.read_text().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("task") != task:
            continue
        if kinds and rec.get("kind") not in kinds:
            continue
        out.append(rec)
    return out


# --- synchronous step-event hook (D9) --------------------------------------
# A process-local callback invoked inside ``emit()`` for ``step_start`` /
# ``step_end`` events, immediately after the event is persisted to
# events.jsonl and before the notify push. The driver registers a hook that
# drains events.jsonl from a shared cursor and renders dispatch/return lines
# synchronously on the calling thread — so dispatch prints before the agent
# blocks (C8) and return for N prints before dispatch for N+1 (C6).
_step_hook: Callable | None = None


def set_step_hook(cb: Callable | None) -> None:
    """Register (or clear with ``None``) the synchronous step-event hook."""
    global _step_hook
    _step_hook = cb


def read_events_since(task: str, offset: int) -> tuple[list[dict], int]:
    """Tail-read events.jsonl from byte ``offset`` for one task.

    Returns ``(events, new_offset)`` where ``new_offset`` is the byte offset
    just past the last byte read (suitable for the next call). Only the new
    tail bytes are parsed — O(new lines), not O(file) (D13) — so a synchronous
    hook firing on every ``step_start``/``step_end`` cannot grow dispatch
    latency with log size. Lines that fail to parse (partial write mid-append)
    are skipped via ``ValueError``.
    """
    if not EVENTS_LOG.exists():
        return [], 0
    out: list[dict] = []
    new_offset = offset
    try:
        with EVENTS_LOG.open("rb") as f:
            f.seek(offset)
            data = f.read()
            new_offset = f.tell()
    except OSError:
        return [], offset
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("task") != task:
            continue
        out.append(rec)
    return out, new_offset


def read_all_events(kinds: set[str] | None = None) -> list[dict]:
    """Structured events for every task, oldest first — the basis of `metrics`.

    Same JSONL-parse-with-skip-on-ValueError pattern as `read_events`, but with
    no task filter: returns every record in the log. Used by `./run.py metrics
    --all` to aggregate across tasks.
    """
    if not EVENTS_LOG.exists():
        return []
    out = []
    for line in EVENTS_LOG.read_text().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if kinds and rec.get("kind") not in kinds:
            continue
        out.append(rec)
    return out


# --- open-escalation marker ------------------------------------------------
# A node that interrupts re-executes from the top when the run is resumed, so a
# naive notify() inside `escalate` fires the same urgent push twice: once when
# it opens, once when you answer it. This marker file makes the second entry
# recognisable as a resume.

def _marker(task: str) -> Path:
    return C.METRICS / f".escalation-{task}.json"


def open_escalation(task: str, reason: str) -> bool:
    """Record an escalation. Returns True if it is newly opened, False on replay."""
    path = _marker(task)
    if path.exists():
        try:
            if json.loads(path.read_text()).get("reason") == reason:
                return False
        except (OSError, ValueError):
            pass
    try:
        path.write_text(json.dumps(
            {"reason": reason, "opened": datetime.now(timezone.utc).isoformat()}))
    except OSError:
        pass
    return True


def close_escalation(task: str) -> None:
    try:
        _marker(task).unlink(missing_ok=True)
    except OSError:
        pass
