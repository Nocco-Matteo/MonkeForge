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
  ntfy                         push, via the notify daemon (socket) or notify.sh fallback

The journal file is the answer to "I resume an escalation and cannot see what
it is doing": checkpointed state updates at node boundaries, this does not.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config as C

EVENTS_LOG = C.METRICS / "events.jsonl"
PIPELINE_LOG = C.METRICS / "pipeline.log"

# silent    — never push (logs still written)
# milestones— only what a human must act on
# all       — every step boundary too (default: you asked to see each one)
NOTIFY_LEVEL = os.environ.get("PIPELINE_NOTIFY_LEVEL", "all").lower()

# Events that always push, whatever the level (except `silent`): either the run
# needs a human, or it has stopped being a run.
MILESTONES = {
    "run_start", "run_end", "run_paused", "run_stalled",
    "step_error", "escalation_open", "escalation_resolved",
    "batch_done", "degraded", "intake_questions", "intake_complete",
    "agent_unhealthy",
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


def _journal_file(task: str) -> Path:
    return C.METRICS / f"journal-{task}.log"


def _should_notify(kind: str, override: bool | None) -> bool:
    if override is not None:
        return override and NOTIFY_LEVEL != "silent"
    if NOTIFY_LEVEL == "silent":
        return False
    if kind in MILESTONES:
        return True
    return NOTIFY_LEVEL == "all"


def _push(title: str, msg: str, prio: str, role: str = "") -> None:
    """Send a notification: try the daemon socket, fall back to spool, then
    to the legacy notify.sh script. A failed push must never take the pipeline
    down."""
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

    # 3. Legacy: invoke notify.sh directly (subprocess, fire-and-forget).
    script = C.NOTIFY_SCRIPT
    if not script.exists():
        return
    try:
        subprocess.run([str(script), title, msg, prio], cwd=C.REPO,
                       capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        pass


def emit(kind: str, task: str = "?", step: str = "", msg: str = "",
         notify: bool | None = None, prio: str | None = None, **extra) -> None:
    """Record one event to every sink. Never raises."""
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

    if _should_notify(kind, notify):
        role = extra.get("role", "")
        _push(f"TASK-{task} · {step or kind}",
              msg or kind, prio or PRIORITY.get(kind, "default"),
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
