"""Refuse implement/close_batch when HEAD != state["branch"]; wrap_up tells the truth."""
from __future__ import annotations

import importlib
from unittest.mock import patch

from pipeline_graph import config as C
from pipeline_graph import nodes as N
from pipeline_graph.nodes import common as CM

# ``from pipeline_graph.nodes import implement`` binds the *function* (re-export).
IMP = importlib.import_module("pipeline_graph.nodes.implement")
FIN = importlib.import_module("pipeline_graph.nodes.finalize")


def _batch_state(**kw):
    st = {
        "task_id": "031",
        "branch": "feature/task-031",
        "batch_idx": 0,
        "batches": [
            {
                "n": 1,
                "scope": "wt",
                "status": "PENDING",
                "outcome": "",
                "deviations": "",
                "checklist": [1],
            }
        ],
        "code_verdict": "APPROVE",
        "fix_cycle": 0,
        "test_fix_attempt": 0,
        "test_fix_failures": [],
        "test_fix_summary": "",
        "escalation": "",
        "journal": [],
    }
    st.update(kw)
    return st


class TestBranchMismatchReason:
    def test_match_is_none(self, monkeypatch):
        monkeypatch.setattr(C, "DRY_RUN", False)
        monkeypatch.setattr(C, "NO_GIT", False)
        with patch.object(CM, "git_identity", return_value={
            "repo": "/tmp/r", "branch": "feature/task-031", "sha": "abc",
        }):
            assert CM.branch_mismatch_reason("feature/task-031") is None

    def test_mismatch_message(self, monkeypatch):
        monkeypatch.setattr(C, "DRY_RUN", False)
        monkeypatch.setattr(C, "NO_GIT", False)
        with patch.object(CM, "git_identity", return_value={
            "repo": "/tmp/MonkeForge", "branch": "main", "sha": "deadbeefcafe",
        }):
            reason = CM.branch_mismatch_reason("feature/task-031")
        assert reason is not None
        assert "git branch mismatch" in reason
        assert "main" in reason
        assert "feature/task-031" in reason
        assert "/tmp/MonkeForge" in reason

    def test_dry_run_skips(self, monkeypatch):
        monkeypatch.setattr(C, "DRY_RUN", True)
        monkeypatch.setattr(C, "NO_GIT", False)
        assert CM.branch_mismatch_reason("feature/task-031") is None


class TestCloseBatchGuard:
    def test_mismatch_escalates_without_commit(self, monkeypatch):
        monkeypatch.setattr(C, "DRY_RUN", False)
        monkeypatch.setattr(C, "NO_GIT", False)
        state = _batch_state()
        with patch.object(IMP, "branch_mismatch_reason",
                          return_value="git branch mismatch: HEAD is 'main'"), \
                patch.object(IMP, "_write_progress") as wp, \
                patch.object(IMP.subprocess, "run") as run, \
                patch.object(N.ev, "emit"):
            out = IMP.close_batch(state)
        assert out.get("escalation", "").startswith("git branch mismatch")
        assert "batch_idx" not in out
        wp.assert_not_called()
        run.assert_not_called()

    def test_match_commits_and_reports_where(self, monkeypatch, tmp_path):
        monkeypatch.setattr(C, "DRY_RUN", True)  # skip real git commit
        monkeypatch.setattr(C, "NO_GIT", False)
        monkeypatch.setattr(C, "RAW", tmp_path)
        state = _batch_state()
        with patch.object(IMP, "branch_mismatch_reason", return_value=None), \
                patch.object(IMP, "_write_progress"), \
                patch.object(IMP, "git_identity", return_value={
                    "repo": "/tmp/MonkeForge",
                    "branch": "feature/task-031",
                    "sha": "abc123def456",
                }), \
                patch.object(N.ev, "emit") as emit:
            out = IMP.close_batch(state)
        assert out.get("escalation") in ("", None) or "escalation" not in out
        assert out["batch_idx"] == 1
        assert "feature/task-031" in out["journal"][0]
        assert "/tmp/MonkeForge" in out["journal"][0]
        msg = emit.call_args[0][3]
        assert "feature/task-031" in msg
        assert "/tmp/MonkeForge" in msg


class TestWrapUpTruth:
    def test_wrap_up_uses_git_identity(self, monkeypatch, tmp_path):
        monkeypatch.setattr(C, "DRY_RUN", True)
        monkeypatch.setattr(C, "FINAL", tmp_path)
        monkeypatch.setattr(C, "ensure_dirs", lambda: None)
        monkeypatch.setattr(C, "sync_back_docs", lambda: None)
        state = {
            "task_id": "031",
            "branch": "feature/task-031",
            "batches": [{"n": 1}],
            "degradations": [],
            "journal": [],
        }
        with patch.object(FIN, "git_identity", return_value={
            "repo": "/tmp/MonkeForge",
            "branch": "main",
            "sha": "abc123def456",
        }), patch.object(N.ev, "emit") as emit:
            out = FIN.wrap_up(state)
        report = (tmp_path / "REPORT-031.md").read_text()
        assert "repo=/tmp/MonkeForge" in report
        assert "git_branch=main" in report
        assert "state_branch=feature/task-031" in report
        assert "WARNING: git HEAD" in report
        assert "git_branch=main" in emit.call_args[0][3]
        assert out["finished"] is True
