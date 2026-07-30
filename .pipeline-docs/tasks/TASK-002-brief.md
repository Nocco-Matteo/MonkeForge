1. Goal
Replace the current string-based event system in events.py with typed event classes (similar to OpenHands' Action/Observation model). Currently events are emitted as string types ("step_start", "step_end", "agent_start", etc.) with kwargs. Typed events would enable: IDE autocompletion, static analysis, structured serialization, and easier filtering/consumption by external tools.

2. Corrections to the request
- None. This is a refactor that preserves all existing behavior.

3. Rules / domain data
- Event types must map 1:1 to existing string types (no new events, no removed events).
- The JSONL format must remain backward-compatible (same field names, same structure).
- The human-readable pipeline.log format must remain identical.
- The journal format must remain identical.
- ntfy notifications must remain identical.
- Must not break any existing test.

4. Codebase anchors
- `pipeline_graph/events.py`: The core module to refactor. Currently uses `emit(event_type, task_id, node, detail, **kwargs)`.
- `pipeline_graph/nodes/common.py`: `instrument()` wrapper calls `ev.emit()` for step_start/step_end/step_error.
- `pipeline_graph/agents.py`: Calls `ev.emit()` for agent_start/agent_end.
- `pipeline_graph/nodes/*.py`: Various nodes call `ev.emit()` for degradations, escalations, etc.
- `run.py`: Reads events for status display and escalation handling.
- `bot/bot.py`: Reads events.jsonl for Discord notifications.
- `tests/test_*.py`: Tests that mock or assert on `ev.emit()` calls.

5. Definition of done
- All event types are defined as dataclasses or Pydantic models in a new `pipeline_graph/event_types.py`.
- `events.py` `emit()` accepts either a typed event or (for backward compat) the old string-based signature.
- JSONL output is byte-identical to before.
- All 168 existing tests pass.
- New tests verify typed event construction and serialization.

6. Scope: in / out
- **IN**: Event type definitions, refactor of emit(), backward-compatible string API, tests.
- **OUT**: Changing the JSONL schema, changing log formats, adding new event types.

7. Manual acceptance
1. Run a dry-run task and verify events.jsonl is identical to a pre-refactor run (diff the two files).
2. Verify `tail -f docs/<repo>/metrics/pipeline.log` output is unchanged.
3. Run the Discord bot and verify escalation cards are unchanged.

8. Unverified assumptions
- We assume all existing `ev.emit()` call sites use a finite, enumerable set of event types (no dynamic strings).
- We assume the kwargs passed to emit() are consistent per event type (can be typed).
