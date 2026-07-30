"""Parity tests for the typed event model (`pipeline_graph/event_types.py`)
against the legacy string-typed `emit()` in `pipeline_graph/events.py`.

Guards the conformance checklist for FINAL-002 batch 1:
  - all 16 kinds have a typed class; `EVENT_CLASSES` keys match `PRIORITY`.
  - `KIND` is a `ClassVar`, not a dataclass field (no leak into `to_record()`).
  - `to_record()` omits `notify`/`prio`/`KIND` and any `_UNSET`-defaulted field,
    but preserves an explicitly-passed `None` (matches legacy `**extra`).
  - `emit()` typed and legacy paths write identical JSONL (modulo `ts`),
    byte-identical `pipeline.log` and `journal-<task>.log` lines (modulo clock),
    and resolve `_push` role/prio identically.
  - the legacy positional and fully-keyword signatures are unchanged.
"""
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline_graph import config as C
from pipeline_graph import events as ev
from pipeline_graph.event_types import (
    EVENT_CLASSES,
    AgentEnd,
    AgentUnhealthy,
    EscalationOpen,
    StepEnd,
    _UNSET,
)


class EventTypesTests(unittest.TestCase):
    def setUp(self):
        # Redirect all three sinks to a temp dir. EVENTS_LOG / PIPELINE_LOG are
        # bound at import time, so patch the module attrs; _journal_file reads
        # C.METRICS at call time, so patch that too.
        self._tmp = tempfile.TemporaryDirectory()
        self._metrics = C.METRICS
        self._events_log = ev.EVENTS_LOG
        self._pipeline_log = ev.PIPELINE_LOG
        C.METRICS = Path(self._tmp.name)
        ev.EVENTS_LOG = C.METRICS / "events.jsonl"
        ev.PIPELINE_LOG = C.METRICS / "pipeline.log"

    def tearDown(self):
        ev.EVENTS_LOG = self._events_log
        ev.PIPELINE_LOG = self._pipeline_log
        C.METRICS = self._metrics
        self._tmp.cleanup()

    # --- checklist 16 -------------------------------------------------------
    def test_all_16_kinds_have_class(self):
        expected = set(ev.MILESTONES) | set(ev.PRIORITY)
        self.assertEqual(set(EVENT_CLASSES), expected)
        self.assertEqual(len(EVENT_CLASSES), 16)

    # --- checklist 17 -------------------------------------------------------
    def test_kind_attr_matches(self):
        for kind, cls in EVENT_CLASSES.items():
            self.assertEqual(cls.KIND, kind)

    # --- checklist 18 -------------------------------------------------------
    def test_kind_is_classvar_not_field(self):
        for cls in EVENT_CLASSES.values():
            field_names = {f.name for f in dataclasses.fields(cls)}
            self.assertNotIn("KIND", field_names,
                             f"{cls.__name__}: KIND leaked into dataclass fields")

    # --- checklist 19 -------------------------------------------------------
    def test_to_record_omits_unset_and_control_fields(self):
        rec = AgentEnd(task="t", step="s", msg="m", agent="claude",
                       role="REVIEWER", exit_code=0, duration_ms=1000,
                       health="ok").to_record()
        self.assertEqual(rec, {
            "kind": "agent_end", "task": "t", "step": "s", "msg": "m",
            "agent": "claude", "role": "REVIEWER", "exit_code": 0,
            "duration_ms": 1000, "health": "ok",
        })
        self.assertNotIn("notify", rec)
        self.assertNotIn("prio", rec)

    # --- checklist 20 -------------------------------------------------------
    def test_to_record_omits_unset_extras(self):
        rec = StepEnd(task="t", step="s", msg="m").to_record()
        self.assertEqual(rec, {"kind": "step_end", "task": "t",
                               "step": "s", "msg": "m"})
        self.assertNotIn("ms", rec)
        self.assertNotIn("outcome", rec)

    # --- checklist 21 -------------------------------------------------------
    def test_to_record_preserves_explicit_none(self):
        rec = AgentEnd(task="t", step="s", msg="m", agent="claude",
                       role=None).to_record()
        self.assertEqual(rec, {"kind": "agent_end", "task": "t", "step": "s",
                               "msg": "m", "agent": "claude", "role": None})

    # --- checklist 22 -------------------------------------------------------
    def test_emit_typed_writes_identical_jsonl(self):
        ev.emit(AgentEnd(task="t", step="s", msg="m", agent="claude",
                        role="REVIEWER", exit_code=0, duration_ms=1000,
                        health="ok"))
        ev.emit("agent_end", "t", "s", "m", agent="claude", role="REVIEWER",
                exit_code=0, duration_ms=1000, health="ok")
        lines = ev.EVENTS_LOG.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        typed = json.loads(lines[0])
        legacy = json.loads(lines[1])
        self.assertEqual({k: v for k, v in typed.items() if k != "ts"},
                         {k: v for k, v in legacy.items() if k != "ts"})

    # --- checklist 23 -------------------------------------------------------
    def test_emit_typed_writes_identical_pipeline_log_and_journal(self):
        ev.emit(StepEnd(task="t", step="s", msg="m", ms=10, outcome="ok"))
        ev.emit("step_end", "t", "s", "m", ms=10, outcome="ok")

        def strip_clock(line: str) -> str:
            # `HH:MM:SS ` prefix is the only non-deterministic part.
            return line.split(" ", 1)[1] if line[:8].count(":") == 2 else line

        for path in (ev.PIPELINE_LOG, C.METRICS / "journal-t.log"):
            lines = path.read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(strip_clock(lines[0]), strip_clock(lines[1]))

    # --- checklist 24 -------------------------------------------------------
    def test_emit_typed_pushes_with_role(self):
        with patch.object(ev, "_push") as mock_push:
            ev.emit(AgentUnhealthy(task="t", step="s", msg="m", agent="claude",
                                   role="REVIEWER", health="hard",
                                   signal="exit 1", output_file="/x",
                                   notify=True))
        self.assertTrue(mock_push.called)
        _, kwargs = mock_push.call_args
        self.assertEqual(kwargs.get("role"), "REVIEWER")
        args = mock_push.call_args.args
        self.assertEqual(args[2], ev.PRIORITY["agent_unhealthy"])

    # --- checklist 25 -------------------------------------------------------
    def test_emit_typed_falls_back_to_kind_role(self):
        with patch.object(ev, "_push") as mock_push:
            ev.emit(EscalationOpen(task="t", step="escalate", msg="r",
                                   context="c", journal=[], notify=True))
        self.assertTrue(mock_push.called)
        _, kwargs = mock_push.call_args
        self.assertEqual(kwargs.get("role"), ev._KIND_ROLE["escalation_open"])
        self.assertEqual(ev._KIND_ROLE["escalation_open"], "ESCALATION")

    # --- checklist 26 -------------------------------------------------------
    def test_legacy_signature_unchanged(self):
        ev.emit("step_end", "t", "s", "m", ms=10, outcome="ok")
        ev.emit(kind="step_end", task="t", step="s", msg="m",
                ms=10, outcome="ok")
        lines = ev.EVENTS_LOG.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        positional = json.loads(lines[0])
        keyword = json.loads(lines[1])
        self.assertIn("ms", positional)
        self.assertIn("outcome", positional)
        self.assertEqual({k: v for k, v in positional.items() if k != "ts"},
                         {k: v for k, v in keyword.items() if k != "ts"})


if __name__ == "__main__":
    unittest.main()
