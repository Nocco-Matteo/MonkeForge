"""Regressions for batch integrity + e2e-not-configured suite gate (033/012)."""
from __future__ import annotations

import importlib
import re
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


def test_escape_relevant_ignores_docs():
    from pipeline_graph import agents as A
    paths = {
        "docs/wt-task-033/prompts/x.md",
        "pipeline_graph/agents.py",
        ".pytest_cache/v/cache",
        "tests/test_task_033_reintake.py",
    }
    got = A._escape_relevant(paths)
    assert got == {"pipeline_graph/agents.py", "tests/test_task_033_reintake.py"}


def test_write_escape_escalates_without_retry(monkeypatch):
    """WRITE_ESCAPE must escalate immediately (not burn test-fix retries)."""
    monkeypatch.setattr(C, "DRY_RUN", False)
    monkeypatch.setattr(C, "NO_GIT", True)
    monkeypatch.setattr(C, "MAX_TEST_FIXES", 3)
    monkeypatch.setattr(C, "TEST_SUITES", [])
    monkeypatch.setattr(C, "arch_docs_block", lambda: "")

    escape = (
        "did stuff\n"
        f"{I_mod._N.WRITE_ESCAPE_MARKER} agent wrote outside PIPELINE_REPO "
        "onto MF_ROOT during implement: pipeline_graph/x.py\n"
    )

    monkeypatch.setattr(
        I_mod._N, "run_agent",
        lambda *a, **k: (I_mod._N.WRITE_ESCAPE_EXIT, escape),
    )
    monkeypatch.setattr(I_mod, "_db_note", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(I_mod, "_capture_test_baseline", lambda *a, **k: {})
    monkeypatch.setattr(I_mod, "branch_mismatch_reason", lambda *a, **k: None)
    monkeypatch.setattr(I_mod, "_git", lambda *a, **k: "abc123")
    monkeypatch.setattr(
        I_mod.tr, "format_test_summary_block",
        lambda *a, **k: "",
    )
    monkeypatch.setattr(
        I_mod, "Conversation",
        mock.Mock(from_state=lambda s: mock.Mock(task_id="033")),
    )

    state = {
        "task_id": "033",
        "batch_idx": 0,
        "batches": [{"n": 1, "scope": "x", "checklist": [1]}],
        "batch_base_ref": "abc123",
        "branch": "feature/task-033",
        "test_fix_attempt": 0,
        "trusted_context": "",
    }
    delta = I_mod.implement(state)
    assert delta.get("escalation")
    assert "WRITE_ESCAPE" in delta["escalation"]
    assert "WRITE_ESCAPE" in delta["journal"][0]
    assert "test_fix_attempt" not in delta


def test_pause_sections_split_baseline_unmeasurable():
    import run as run_mod

    reason = (
        "test baseline unmeasurable for batch 1 — suite did not produce a real "
        "failure list (MonkeForge: 0 failed, synthetic: MonkeForge|pytest exit 4 "
        "[ole needs BOTH `model:` and `cmd:` — there are no\n"
        "E     built-in agent defaults\n"
        "E     Fix: copy the `agents:` section from /tmp/example.yaml\n"
        "]). Fix test config / monkeforge.yaml agents / runner, then resume."
    )
    secs = run_mod._pause_sections({"stage": "escalation", "reason": reason})
    assert secs["what"] == "test baseline unmeasurable for batch 1"
    assert secs["why"].startswith("suite did not produce")
    assert secs["detail"] == "MonkeForge: 0 failed"
    assert secs["synthetic"] == "MonkeForge|pytest exit 4"
    assert "cause" not in secs  # Fix: dumped into action, not duplicated
    assert secs["action"].startswith("Fix test config")
    assert "E " not in str(secs)
    assert "ole needs" not in str(secs)


def test_suite_env_injects_orchestrator_yaml(monkeypatch, tmp_path):
    """Gate subprocesses must see PIPELINE_WT_YAML even when cwd is a bare wt."""
    from pipeline_graph import config as C
    from pipeline_graph import test_runner as TR

    orch = tmp_path / "monkeforge.yaml"
    orch.write_text("agents: {}\n")  # existence matters for inject; parse is separate
    monkeypatch.setattr(C, "MF_ROOT", tmp_path)
    monkeypatch.delenv("PIPELINE_WT_YAML", raising=False)
    suite = C.TestSuite(label="x", runner="pytest", cwd="", env={"FOO": "1"})
    env = TR._suite_env(suite)
    assert env["PIPELINE_WT_YAML"] == str(orch.resolve())
    assert env["FOO"] == "1"


def test_suite_env_suite_override_wins(monkeypatch, tmp_path):
    from pipeline_graph import config as C
    from pipeline_graph import test_runner as TR

    orch = tmp_path / "monkeforge.yaml"
    orch.write_text("x: 1\n")
    monkeypatch.setattr(C, "MF_ROOT", tmp_path)
    monkeypatch.delenv("PIPELINE_WT_YAML", raising=False)
    suite = C.TestSuite(
        label="x", runner="pytest", cwd="",
        env={"PIPELINE_WT_YAML": "/intentional.yaml"},
    )
    env = TR._suite_env(suite)
    assert env["PIPELINE_WT_YAML"] == "/intentional.yaml"
    assert I_mod._is_synthetic_failure_key("MonkeForge|pytest exit 4")
    assert I_mod._is_synthetic_failure_key("x|cwd escapes repo: /tmp")
    assert not I_mod._is_synthetic_failure_key(
        "MonkeForge|tests/test_condenser_archive.py::TestStatusOutput::"
        "test_status_prints_archive_line_when_archive_exists"
    )


def test_tail_prefers_error_banner_strips_pytest_e():
    """Pause chrome must not dump pytest 'E' prefixes / mid-word cuts."""
    from pipeline_graph import test_runner as TR

    raw = "\n".join([
        "======= ERRORS =======",
        "E   ImportError while importing",
        "E",
        "error: /tmp/wt/monkeforge.yaml: missing required top-level `agents:` block",
        "",
        "  Every role needs BOTH `model:` and `cmd:` — there are no",
        "  built-in agent defaults in code (not models, not commands).",
        "",
        "  Fix: copy the `agents:` section from",
        "    /tmp/wt/monkeforge.example.yaml",
        "  into /tmp/wt/monkeforge.yaml, then set the models/CLIs you want to run.",
    ])
    out = TR._tail(raw, limit=200)
    assert not re.search(r"(^|\s)E\s", out)
    assert out.startswith("error:")
    assert "agents:" in out
    assert "Every role needs BOTH" in out  # head kept, not "...ole needs"
    assert out.endswith("…")


def test_baseline_unmeasurable_escalates(monkeypatch):
    """Synthetic-only baseline must not be recorded — escalate infrastructure."""
    monkeypatch.setattr(C, "DRY_RUN", False)

    def fake_detailed(*, task_id=None):
        return (
            4,
            {"MonkeForge|pytest exit 4"},
            "MonkeForge: 0 failed, synthetic: MonkeForge|pytest exit 4 [ole…]",
            1,
        )

    monkeypatch.setattr(I_mod.tr, "run_repo_tests_detailed", fake_detailed)
    monkeypatch.setattr(I_mod.ev, "emit", lambda *a, **k: None)

    state = {
        "task_id": "019",
        "batches": [{"n": 1, "scope": "x"}],
    }
    delta = I_mod._capture_test_baseline(state, state["batches"][0], db_ok=True)
    assert delta.get("escalation")
    assert "UNMEASURABLE" in delta["journal"][0]
    assert "batch_test_baseline" not in delta


def test_baseline_keeps_real_drops_synthetic(monkeypatch):
    monkeypatch.setattr(C, "DRY_RUN", False)
    real = (
        "MonkeForge|tests/test_condenser_archive.py::TestStatusOutput::"
        "test_status_prints_archive_line_when_archive_exists"
    )

    def fake_detailed(*, task_id=None):
        return (
            1,
            {"MonkeForge|pytest exit 4", real},
            "mixed",
            1,
        )

    monkeypatch.setattr(I_mod.tr, "run_repo_tests_detailed", fake_detailed)
    monkeypatch.setattr(I_mod.ev, "emit", lambda *a, **k: None)

    state = {
        "task_id": "019",
        "batches": [{"n": 1, "scope": "x"}],
    }
    delta = I_mod._capture_test_baseline(state, state["batches"][0], db_ok=True)
    assert not delta.get("escalation")
    assert delta["batch_test_baseline"] == [real]
    assert delta["task_baseline"] == [real]


def test_implement_prompt_retry_overrides_scope():
    from pathlib import Path
    text = (
        Path(__file__).resolve().parents[1]
        / "pipeline_graph" / "prompts" / "implement.md"
    ).read_text(encoding="utf-8")
    assert "HARD OVERRIDE" in text
    assert "OVERRIDE batch scope" in text
    assert "EXCEPT on a" in text and "test-fix retry" in text

