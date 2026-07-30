"""Test-wide safety net: no test may send a real notification.

Several tests call `events.emit(...)` for real (parity checks, node smoke tests)
without mocking the push path. With the notify daemon running and a webhook
configured, those emits fired live Discord messages on every `pytest` run
(the `TASK-t · s` spam). This autouse fixture makes `_push` a no-op for the
whole session; tests that assert on `_push` re-patch it locally and still work.
"""
import pytest

from pipeline_graph import events as ev


@pytest.fixture(autouse=True)
def _silence_notifications(monkeypatch):
    monkeypatch.setattr(ev, "_push", lambda *a, **k: None)
