"""intake_ask must not accept INTAKE: COMPLETE with a non-contract brief."""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline_graph import config as C
from pipeline_graph.nodes import intake as intake_mod


CONTRACT = """UI-SURFACE: no

# TASK-gate: contract

## 1. Goal

Done means X.

## 2. Corrections to the request

none

## 3. Rules / domain data

none

## 4. Codebase anchors

- `run.py`

## 4b. Architecture docs to follow

none

## 5. Definition of done

| ID | Criterion |
|----|-----------|
| F1 | X — verified by reading the file |

## 6. Scope: in / out

### In
- X

### Out
- Y

## 7. Manual acceptance

1. Read the brief.

## 8. Unverified assumptions

none
"""

STATUS = """Round 1 complete. Seed looks fine.

The contract brief is at docs/tasks/TASK-gate-brief.md.

INTAKE: COMPLETE
"""


@pytest.fixture
def tasks_dir(tmp_path, monkeypatch):
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    monkeypatch.setattr(C, "TASKS", tasks)
    monkeypatch.setattr(C, "METRICS", tmp_path / "metrics")
    C.METRICS.mkdir(exist_ok=True)
    return tasks


def test_complete_status_summary_does_not_clobber_seed(tasks_dir, monkeypatch):
    tid = "gate"
    seed = CONTRACT
    brief = tasks_dir / f"TASK-{tid}-brief.md"
    brief.write_text(seed)

    def fake_run_agent(*a, **k):
        return 0, STATUS

    monkeypatch.setattr(intake_mod, "run_agent", fake_run_agent)
    # Conversation.from_state needs minimal state — stub it.
    class _Conv:
        @classmethod
        def from_state(cls, state):
            return cls()

    monkeypatch.setattr(intake_mod, "Conversation", _Conv)

    delta = intake_mod.intake_ask(
        {"task_id": tid, "intake_round": 0, "request": seed, "interview": True}
    )

    assert brief.read_text() == seed, "seed must not be overwritten by status chat"
    assert delta.get("intake_done") is not True
    assert "COMPLETE but wrote no brief" in delta.get("escalation", "")


def test_complete_with_contract_stdout_accepts(tasks_dir, monkeypatch):
    tid = "gate2"
    brief = tasks_dir / f"TASK-{tid}-brief.md"

    def fake_run_agent(*a, **k):
        return 0, CONTRACT + "\n\nINTAKE: COMPLETE\n"

    monkeypatch.setattr(intake_mod, "run_agent", fake_run_agent)

    class _Conv:
        @classmethod
        def from_state(cls, state):
            return cls()

    monkeypatch.setattr(intake_mod, "Conversation", _Conv)

    delta = intake_mod.intake_ask(
        {"task_id": tid, "intake_round": 0, "request": "do x", "interview": True}
    )

    assert delta.get("intake_done") is True
    written = Path(delta["brief_path"]).read_text()
    assert "UI-SURFACE: no" in written
    assert "## 1. Goal" in written
