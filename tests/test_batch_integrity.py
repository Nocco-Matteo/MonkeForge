"""Regressions for batch integrity + e2e-not-configured suite gate (033/012)."""
from __future__ import annotations

import importlib
from unittest import mock

from pipeline_graph import config as C
from pipeline_graph.nodes import common as NC
from pipeline_graph.nodes import finalize as F

I_mod = importlib.import_module("pipeline_graph.nodes.implement")
R_mod = importlib.import_module("pipeline_graph.nodes.review")


def test_e2e_required_false_without_script(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "E2E_UP_SCRIPT", None)
    assert C.e2e_required() is False
    monkeypatch.setattr(C, "E2E_UP_SCRIPT", tmp_path / "missing.sh")
    assert C.e2e_required() is False
    script = tmp_path / "e2e-up.sh"
    script.write_text("#!/bin/bash\n")
    monkeypatch.setattr(C, "E2E_UP_SCRIPT", script)
    assert C.e2e_required() is True


def test_db_note_skips_probe_when_e2e_not_required(monkeypatch):
    monkeypatch.setattr(C, "DRY_RUN", False)
    monkeypatch.setattr(C, "e2e_required", lambda: False)
    probed = {"n": 0}

    def boom():
        probed["n"] += 1
        raise AssertionError("must not probe")

    monkeypatch.setattr(C, "db_reachable", boom)
    ok, note = NC._db_note("012", "final_check")
    assert ok is True
    assert probed["n"] == 0
    assert "not configured" in note.lower()


def test_final_test_fix_loop_runs_suite_when_db_ok(monkeypatch):
    """When db_ok True (e2e not required), do not short-circuit to skipped."""
    called = {"n": 0}

    def fake_run(*, task_id=None):
        called["n"] += 1
        return 0, [], "1 passed", 1

    monkeypatch.setattr(F.tr, "run_repo_tests_detailed", fake_run)
    monkeypatch.setattr(
        F.tr, "new_failures_since_baseline",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(C, "TEST_SUITES", [mock.Mock(label="MonkeForge")])
    monkeypatch.setattr(C, "DRY_RUN", False)
    monkeypatch.setattr(C, "LINT_DEBT_RULES", [])
    monkeypatch.setattr(C, "TEST_AMBIENT_PATTERNS", [])
    conv = mock.Mock(task_id="012")
    out = F._final_test_fix_loop(conv, db_ok=True, baseline=set())
    assert called["n"] == 1
    assert out["status"] == "green"
    assert out["ran_count"] == 1


def test_checkpoint_plan_answers_single_canonical():
    """UI lists only ok; aliases remain accepted by the validator set."""
    import inspect
    src = inspect.getsource(F.checkpoint_plan)
    assert '"yes": "same as ok"' not in src
    assert '"approve": "same as ok"' not in src
    assert '"ok": "approve the plan and start implement"' in src


def test_code_review_empty_diff_escalates(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "DRY_RUN", False)
    monkeypatch.setattr(C, "REVIEWS", tmp_path)
    monkeypatch.setattr(C, "arch_docs_block", lambda: "")
    review = "git diff is empty.\n\nVERDICT: NOT APPLICABLE — empty diff\n"
    (tmp_path / "CODE-t-b1.md").write_text(review)

    monkeypatch.setattr(R_mod, "run_agent", lambda *a, **k: (0, review))
    monkeypatch.setattr(R_mod, "classify_output", lambda *a, **k: ("ok", None))
    monkeypatch.setattr(R_mod, "_trust_output", lambda *a, **k: True)
    monkeypatch.setattr(R_mod, "_git", lambda *a, **k: "")
    monkeypatch.setattr(
        R_mod, "Conversation", mock.Mock(from_state=lambda s: mock.Mock()),
    )

    state = {
        "task_id": "t",
        "batch_idx": 0,
        "batches": [{"n": 1, "scope": "x", "checklist": [1, 2, 3]}],
        "batch_base_ref": "abc123",
        "trusted_context": "",
    }
    delta = R_mod.code_review(state)
    assert delta.get("escalation")
    assert "EMPTY" in delta["journal"][0]


def test_close_batch_refuses_empty_diff(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "DRY_RUN", False)
    monkeypatch.setattr(C, "NO_GIT", False)
    monkeypatch.setattr(C, "REPO", tmp_path)
    monkeypatch.setattr(C, "RAW", tmp_path)
    monkeypatch.setattr(C, "FINAL", tmp_path)
    monkeypatch.setattr(I_mod, "branch_mismatch_reason", lambda *a, **k: None)
    monkeypatch.setattr(I_mod, "_write_progress", lambda *a, **k: None)
    monkeypatch.setattr(I_mod, "git_identity", lambda: {
        "sha": "abc123", "branch": "feature/task-t", "repo": str(tmp_path),
    })

    def fake_git(*args, **k):
        if args[:2] == ("rev-parse", "HEAD"):
            return "abc123"
        return ""

    monkeypatch.setattr(I_mod, "_git", fake_git)

    class _Ls:
        stdout = ""
        returncode = 0
        stderr = ""

    monkeypatch.setattr(I_mod.subprocess, "run", lambda *a, **k: _Ls())

    state = {
        "task_id": "t",
        "batch_idx": 0,
        "batches": [{"n": 1, "scope": "x", "checklist": [1], "status": "DOING",
                     "deviations": ""}],
        "batch_base_ref": "abc123",
        "code_verdict": "APPROVE",
        "branch": "feature/task-t",
    }
    delta = I_mod.close_batch(state)
    assert delta.get("escalation")
    assert "empty diff" in delta["journal"][0]
    assert state["batches"][0]["status"] == "DOING"
