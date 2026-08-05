"""Shared Discord ↔ CLI answer handoff for an in-session pause.

When ``./run.py`` is blocked in the interactive session loop, Discord must not
spawn a second ``resume`` driver against the same checkpoint. Instead the bot
writes a small JSON file here; the CLI polls it and resumes in-process.

CRITICAL: if ``live_session_pid(task)`` is set, Discord must NEVER spawn
``run.py resume`` — that causes a dual-driver race on the checkpoint (TASK-030
incident). Timeout → report failure to the operator, leave the CLI in charge.

Paths under ``{METRICS}/``:
  - ``pending-answer-{task_id}.json`` — Discord → CLI payload
  - ``pause-wait-{task_id}.json`` — CLI marks when it starts listening (gates
    out answers written for a previous pause)
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import config as C


def pending_answer_path(task_id: str) -> Path:
    return C.METRICS / f"pending-answer-{task_id}.json"


def pause_wait_path(task_id: str) -> Path:
    return C.METRICS / f"pause-wait-{task_id}.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def begin_pause_wait(task_id: str) -> str:
    """Mark that the CLI is listening for a Discord answer for this pause.

    Drops any pending-answer whose ``ts`` is *before* this mark (stale from a
    previous pause) so a late Discord click cannot apply to the wrong gate.
    Answers written *after* the mark are kept (Discord won the race into the
    wait — that is the desired synergy path).
    """
    C.METRICS.mkdir(parents=True, exist_ok=True)
    since = _now_iso()
    path = pause_wait_path(task_id)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"task": str(task_id), "since": since}))
    tmp.replace(path)

    pending = pending_answer_path(task_id)
    try:
        data = json.loads(pending.read_text())
        ans_ts = str(data.get("ts") or "")
        if ans_ts and ans_ts < since:
            pending.unlink(missing_ok=True)
    except (OSError, json.JSONDecodeError):
        pass
    return since


def end_pause_wait(task_id: str) -> None:
    try:
        pause_wait_path(task_id).unlink(missing_ok=True)
    except OSError:
        pass


def write_pending_answer(task_id: str, answer: str, *, source: str = "discord") -> Path:
    """Atomically write a pending answer for ``task_id``. Overwrites any prior."""
    C.METRICS.mkdir(parents=True, exist_ok=True)
    path = pending_answer_path(task_id)
    payload = {
        "answer": str(answer),
        "source": source,
        "ts": _now_iso(),
        "task": str(task_id),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)
    return path


def take_pending_answer(task_id: str) -> str | None:
    """Read and clear a pending answer. Returns the answer string, or None.

    If a pause-wait gate exists, answers with ``ts`` older than the gate are
    discarded (stale).
    """
    path = pending_answer_path(task_id)
    try:
        raw = path.read_text()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None

    since = None
    try:
        since = json.loads(pause_wait_path(task_id).read_text()).get("since")
    except (OSError, json.JSONDecodeError):
        pass
    ans_ts = str(data.get("ts") or "")
    if since and ans_ts and ans_ts < str(since):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None

    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    ans = data.get("answer")
    if ans is None:
        return None
    text = str(ans).strip()
    return text or None


def clear_pending_answer(task_id: str) -> None:
    try:
        pending_answer_path(task_id).unlink(missing_ok=True)
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    return True


def live_session_pid(task_id: str) -> int | None:
    """Return the PID of a live pipeline process for ``task_id``, or None.

    Uses ``current.json``: matching ``task``, non-idle, pid still alive.
    Any such PID owns the task — Discord must not spawn a second driver.
    """
    path = C.METRICS / "current.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("idle"):
        return None
    if str(data.get("task", "")) != str(task_id):
        return None
    try:
        pid = int(data.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    if not _pid_alive(pid):
        return None
    return pid


def live_session_step(task_id: str) -> str | None:
    """Current step label for a live session, or None if none."""
    path = C.METRICS / "current.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("idle"):
        return None
    if str(data.get("task", "")) != str(task_id):
        return None
    try:
        pid = int(data.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    if not _pid_alive(pid):
        return None
    return str(data.get("step") or "")
