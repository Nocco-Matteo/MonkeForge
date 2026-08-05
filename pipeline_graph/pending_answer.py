"""Shared Discord ↔ CLI answer handoff for an in-session pause.

When ``./run.py`` is blocked in the interactive session loop, Discord must not
spawn a second ``resume`` driver against the same checkpoint. Instead the bot
writes a small JSON file here; the CLI polls it and resumes in-process.

Path: ``{METRICS}/pending-answer-{task_id}.json``
Payload: ``{"answer": "<key>", "source": "discord", "ts": "<iso>"}``
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import config as C


def pending_answer_path(task_id: str) -> Path:
    return C.METRICS / f"pending-answer-{task_id}.json"


def write_pending_answer(task_id: str, answer: str, *, source: str = "discord") -> Path:
    """Atomically write a pending answer for ``task_id``. Overwrites any prior."""
    C.METRICS.mkdir(parents=True, exist_ok=True)
    path = pending_answer_path(task_id)
    payload = {
        "answer": str(answer),
        "source": source,
        "ts": datetime.now(timezone.utc).isoformat(),
        "task": str(task_id),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)
    return path


def take_pending_answer(task_id: str) -> str | None:
    """Read and clear a pending answer. Returns the answer string, or None."""
    path = pending_answer_path(task_id)
    try:
        raw = path.read_text()
    except OSError:
        return None
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
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
    That PID owns the interactive session loop (or an in-flight step) and
    Discord should hand off via the pending-answer file instead of spawning
    a second ``run.py resume``.
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
