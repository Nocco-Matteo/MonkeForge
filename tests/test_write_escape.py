"""Agent-spawn containment: the signals that let TASK-032 batch 1 land on MF_ROOT.

The spawn had ``cwd=PIPELINE_REPO`` and ``PIPELINE_ISOLATED=1`` set correctly
the whole time. What leaked was (a) an inherited ``PWD`` still pointing at the
orchestrator checkout and (b) an absolute MF_ROOT path quoted into the prompt
from an earlier judge's output — the only absolute anchor the agent could see.
"""
from __future__ import annotations

import os
from pathlib import Path

from pipeline_graph import agents as A
from pipeline_graph import config as C


class TestAgentEnvPwd:
    def test_pwd_matches_repo_not_orchestrator(self, monkeypatch):
        monkeypatch.setenv("PWD", "/home/someone/MonkeForge")
        monkeypatch.setenv("OLDPWD", "/elsewhere")
        env = A._agent_env()
        assert env["PWD"] == str(Path(C.REPO).resolve())
        assert "OLDPWD" not in env

    def test_pwd_set_even_when_absent_from_parent(self, monkeypatch):
        monkeypatch.delenv("PWD", raising=False)
        assert A._agent_env()["PWD"] == str(Path(C.REPO).resolve())


class TestSanitizePromptPaths:
    def test_noop_when_not_isolated(self, monkeypatch):
        monkeypatch.delenv("PIPELINE_ISOLATED", raising=False)
        text = f"Deliverables written to {C.MF_ROOT}/docs/wt-task-032/final/"
        assert A._sanitize_prompt_paths(text) == text

    def test_mf_root_anchor_removed_when_isolated(self, monkeypatch):
        monkeypatch.setenv("PIPELINE_ISOLATED", "1")
        mf = Path("/home/u/MonkeForge")
        monkeypatch.setattr(C, "MF_ROOT", mf)
        monkeypatch.setattr(C, "REPO", Path("/home/u/wt-task-032"))
        out = A._sanitize_prompt_paths(
            f"Deliverables written to {mf}/docs/wt-task-032/final/"
        )
        assert str(mf) not in out
        assert out.endswith("<orchestrator>/docs/wt-task-032/final/")

    def test_worktree_paths_survive(self, monkeypatch):
        """Only the orchestrator anchor goes; the agent still needs its own."""
        monkeypatch.setenv("PIPELINE_ISOLATED", "1")
        mf = Path("/home/u/MonkeForge")
        repo = Path("/home/u/wt-task-032")
        monkeypatch.setattr(C, "MF_ROOT", mf)
        monkeypatch.setattr(C, "REPO", repo)
        out = A._sanitize_prompt_paths(f"edit {repo}/run.py, not {mf}/run.py")
        assert f"{repo}/run.py" in out
        assert f"{mf}/run.py" not in out

    def test_nested_worktree_prefix_not_mangled(self, monkeypatch):
        """A worktree under MF_ROOT must not have its own prefix rewritten."""
        monkeypatch.setenv("PIPELINE_ISOLATED", "1")
        mf = Path("/home/u/MonkeForge")
        repo = mf / "wt-task-032"
        monkeypatch.setattr(C, "MF_ROOT", mf)
        monkeypatch.setattr(C, "REPO", repo)
        out = A._sanitize_prompt_paths(f"{repo}/run.py and {mf}/docs/x.md")
        assert f"{repo}/run.py" in out
        assert "<orchestrator>/docs/x.md" in out

    def test_same_root_is_noop(self, monkeypatch):
        """Non-isolated dogfooding where REPO == MF_ROOT must not be rewritten."""
        monkeypatch.setenv("PIPELINE_ISOLATED", "1")
        mf = Path("/home/u/MonkeForge")
        monkeypatch.setattr(C, "MF_ROOT", mf)
        monkeypatch.setattr(C, "REPO", mf)
        text = f"{mf}/run.py"
        assert A._sanitize_prompt_paths(text) == text
