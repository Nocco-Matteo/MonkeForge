"""Typed event model: one `@dataclass` per event kind in `events.py`'s `PRIORITY`.

The legacy `emit(kind, task, step, msg, notify, prio, **extra)` API stays the
single entry point (see `events.py`); these dataclasses are an opt-in typed
constructor for the same record. `emit()` accepts an `Event` instance as its
first positional argument and unpacks it into the same locals the string path
uses, so every sink (JSONL, pipeline.log, journal, ntfy) stays byte-identical.

Design notes:
- `_UNSET` sentinel (not `None`) defaults for extra fields, so `to_record()`
  can tell "caller never set this" (omit, matching an absent `**extra` key)
  from "caller passed `None`" (keep, matching `emit(..., role=None)` which
  writes `"role": None` verbatim — `events.py:134-135`).
- `KIND: ClassVar[str]` (not a plain dataclass field) so the dataclass
  machinery excludes it from `dataclasses.fields(self)` and it never leaks
  into `to_record()` output; it is read explicitly via `self.KIND` for the
  `"kind"` record key only.
- `notify`/`prio` are control params read by name by `emit()`/`_push`; they
  are never serialized, so `to_record()` excludes them by name.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, ClassVar


# Sentinel for "caller never set this field". Distinct from an explicit `None`,
# which legacy `**extra` writes verbatim (`events.py:134-135`).
_UNSET: Any = object()


@dataclass
class Event:
    """Base event. Subclasses set `KIND` and add extra record fields."""

    task: str = "?"
    step: str = ""
    msg: str = ""
    notify: bool | None = None
    prio: str | None = None

    KIND: ClassVar[str] = ""

    def to_record(self) -> dict[str, Any]:
        """Return the JSONL record dict (without `ts`, which `emit()` injects).

        Includes `kind` (from `self.KIND`), the base `task`/`step`/`msg`, and
        every subclass extra field whose value `is not _UNSET`. Excludes
        `notify`/`prio` (control params, never serialized) and `KIND` (a
        ClassVar, never a dataclass field). A field explicitly set to `None`
        IS included with value `None`, matching legacy `**extra`.
        """
        record: dict[str, Any] = {
            "kind": self.KIND,
            "task": self.task,
            "step": self.step,
            "msg": self.msg,
        }
        for f in fields(self):
            if f.name in ("notify", "prio"):
                continue
            value = getattr(self, f.name)
            if value is _UNSET:
                continue
            record[f.name] = value
        return record


@dataclass
class RunStart(Event):
    KIND: ClassVar[str] = "run_start"


@dataclass
class RunEnd(Event):
    KIND: ClassVar[str] = "run_end"
    degraded: bool | None = _UNSET
    degradations: list[str] | None = _UNSET


@dataclass
class RunPaused(Event):
    KIND: ClassVar[str] = "run_paused"
    answers: dict | None = _UNSET
    options: list | None = _UNSET
    router_error: bool | None = _UNSET
    hint: str | None = _UNSET
    context: str | None = _UNSET
    blockers: str | None = _UNSET
    screens: str | None = _UNSET


@dataclass
class RunStalled(Event):
    KIND: ClassVar[str] = "run_stalled"


@dataclass
class StepStart(Event):
    KIND: ClassVar[str] = "step_start"


@dataclass
class StepEnd(Event):
    KIND: ClassVar[str] = "step_end"
    ms: int | None = _UNSET
    outcome: str | None = _UNSET


@dataclass
class StepError(Event):
    KIND: ClassVar[str] = "step_error"
    ms: int | None = _UNSET
    traceback: str | None = _UNSET


@dataclass
class AgentStart(Event):
    KIND: ClassVar[str] = "agent_start"
    agent: str | None = _UNSET
    role: str | None = _UNSET
    output_file: str | None = _UNSET


@dataclass
class AgentEnd(Event):
    KIND: ClassVar[str] = "agent_end"
    agent: str | None = _UNSET
    role: str | None = _UNSET
    exit_code: int | None = _UNSET
    duration_ms: int | None = _UNSET
    health: str | None = _UNSET


@dataclass
class AgentUnhealthy(Event):
    KIND: ClassVar[str] = "agent_unhealthy"
    agent: str | None = _UNSET
    role: str | None = _UNSET
    health: str | None = _UNSET
    signal: str | None = _UNSET
    output_file: str | None = _UNSET


@dataclass
class Degraded(Event):
    KIND: ClassVar[str] = "degraded"


@dataclass
class EscalationOpen(Event):
    KIND: ClassVar[str] = "escalation_open"
    context: str | None = _UNSET
    journal: list[str] | None = _UNSET


@dataclass
class EscalationResolved(Event):
    KIND: ClassVar[str] = "escalation_resolved"


@dataclass
class BatchDone(Event):
    KIND: ClassVar[str] = "batch_done"


@dataclass
class IntakeQuestions(Event):
    KIND: ClassVar[str] = "intake_questions"
    round: int | None = _UNSET


@dataclass
class IntakeComplete(Event):
    KIND: ClassVar[str] = "intake_complete"


EVENT_CLASSES: dict[str, type[Event]] = {
    cls.KIND: cls
    for cls in (
        RunStart, RunEnd, RunPaused, RunStalled,
        StepStart, StepEnd, StepError,
        AgentStart, AgentEnd, AgentUnhealthy,
        Degraded,
        EscalationOpen, EscalationResolved,
        BatchDone,
        IntakeQuestions, IntakeComplete,
    )
}
