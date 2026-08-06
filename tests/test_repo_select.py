"""Target-repo resolution: --repo / env / yaml repos: (no git-cwd default)."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline_graph import repo_select as RS


@pytest.fixture
def mf_root(tmp_path):
    root = tmp_path / "mf"
    root.mkdir()
    return root


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()
    (path / "README").write_text("x")
    return path


def _write_yaml(mf_root: Path, repos_block: str) -> Path:
    y = mf_root / "monkeforge.yaml"
    y.write_text(repos_block)
    return y


class TestLoadRepos:
    def test_string_and_mapping(self, mf_root):
        a = _git_repo(mf_root / "apps" / "a")
        b = _git_repo(mf_root / "apps" / "b")
        y = _write_yaml(mf_root, f"""
repos:
  - apps/a
  - path: apps/b
    label: bee
""")
        repos = RS.load_repos(y, mf_root=mf_root)
        assert [r.label for r in repos] == ["a", "bee"]
        assert repos[0].path == a.resolve()
        assert repos[1].path == b.resolve()


class TestEnsure:
    def test_env_wins(self, mf_root, monkeypatch):
        target = _git_repo(mf_root / "only")
        monkeypatch.setenv("PIPELINE_REPO", str(target))
        y = _write_yaml(mf_root, "repos: []\n")
        got = RS.ensure_pipeline_repo(
            yaml_path=y, mf_root=mf_root, interactive=False,
        )
        assert got == target.resolve()
        assert os.environ["PIPELINE_REPO"] == str(target.resolve())

    def test_repo_flag_by_index(self, mf_root, monkeypatch):
        monkeypatch.delenv("PIPELINE_REPO", raising=False)
        a = _git_repo(mf_root / "apps" / "a")
        _git_repo(mf_root / "apps" / "b")
        y = _write_yaml(mf_root, """
repos:
  - path: apps/a
    label: aaa
  - path: apps/b
    label: bbb
""")
        got = RS.ensure_pipeline_repo(
            yaml_path=y, mf_root=mf_root, repo_flag="1", interactive=False,
        )
        assert got == a.resolve()

    def test_repo_flag_by_label(self, mf_root, monkeypatch):
        monkeypatch.delenv("PIPELINE_REPO", raising=False)
        _git_repo(mf_root / "apps" / "a")
        b = _git_repo(mf_root / "apps" / "b")
        y = _write_yaml(mf_root, """
repos:
  - path: apps/a
    label: aaa
  - path: apps/b
    label: bbb
""")
        got = RS.ensure_pipeline_repo(
            yaml_path=y, mf_root=mf_root, repo_flag="bbb", interactive=False,
        )
        assert got == b.resolve()

    def test_single_auto(self, mf_root, monkeypatch, capsys):
        monkeypatch.delenv("PIPELINE_REPO", raising=False)
        a = _git_repo(mf_root / "apps" / "a")
        y = _write_yaml(mf_root, """
repos:
  - path: apps/a
    label: aaa
""")
        got = RS.ensure_pipeline_repo(
            yaml_path=y, mf_root=mf_root, interactive=False,
        )
        assert got == a.resolve()
        err = capsys.readouterr().err
        assert "using repo: aaa" in err

    def test_zero_raises(self, mf_root, monkeypatch):
        monkeypatch.delenv("PIPELINE_REPO", raising=False)
        y = _write_yaml(mf_root, "repos: []\n")
        with pytest.raises(RS.RepoSelectError) as ei:
            RS.ensure_pipeline_repo(
                yaml_path=y, mf_root=mf_root, interactive=False,
            )
        assert "no target repo configured" in ei.value.cli_message()

    def test_multi_noninteractive_raises(self, mf_root, monkeypatch):
        monkeypatch.delenv("PIPELINE_REPO", raising=False)
        _git_repo(mf_root / "apps" / "a")
        _git_repo(mf_root / "apps" / "b")
        y = _write_yaml(mf_root, """
repos:
  - apps/a
  - apps/b
""")
        with pytest.raises(RS.RepoSelectError) as ei:
            RS.ensure_pipeline_repo(
                yaml_path=y, mf_root=mf_root, interactive=False,
            )
        msg = ei.value.cli_message()
        assert "2 repos" in msg
        assert "--repo" in msg

    def test_multi_interactive_pick(self, mf_root, monkeypatch):
        monkeypatch.delenv("PIPELINE_REPO", raising=False)
        _git_repo(mf_root / "apps" / "a")
        b = _git_repo(mf_root / "apps" / "b")
        y = _write_yaml(mf_root, """
repos:
  - path: apps/a
    label: aaa
  - path: apps/b
    label: bbb
""")
        with patch.object(RS.sys.stdin, "isatty", return_value=True), \
             patch.object(RS.sys.stdin, "readline", return_value="2\n"):
            got = RS.ensure_pipeline_repo(
                yaml_path=y, mf_root=mf_root, interactive=True,
            )
        assert got == b.resolve()

    def test_missing_git_raises(self, mf_root, monkeypatch):
        monkeypatch.delenv("PIPELINE_REPO", raising=False)
        bare = mf_root / "apps" / "nogit"
        bare.mkdir(parents=True)
        y = _write_yaml(mf_root, """
repos:
  - apps/nogit
""")
        with pytest.raises(RS.RepoSelectError) as ei:
            RS.ensure_pipeline_repo(
                yaml_path=y, mf_root=mf_root, interactive=False,
            )
        assert "not a git repo" in ei.value.cli_message()

    def test_pipeline_repo_scalar_fallback(self, mf_root, monkeypatch):
        # pipeline.repo scalar is the last-resort fallback when no --repo,
        # no PIPELINE_REPO env, and no repos: list.
        monkeypatch.delenv("PIPELINE_REPO", raising=False)
        target = _git_repo(mf_root / "myapp")
        y = _write_yaml(mf_root, f"""
pipeline:
  repo: {target}
""")
        got = RS.ensure_pipeline_repo(
            yaml_path=y, mf_root=mf_root, interactive=False,
        )
        assert got == target.resolve()

    def test_pipeline_repo_relative_to_mf_root(self, mf_root, monkeypatch):
        monkeypatch.delenv("PIPELINE_REPO", raising=False)
        target = _git_repo(mf_root / "relapp")
        y = _write_yaml(mf_root, """
pipeline:
  repo: relapp
""")
        got = RS.ensure_pipeline_repo(
            yaml_path=y, mf_root=mf_root, interactive=False,
        )
        assert got == target.resolve()

    def test_repos_list_wins_over_pipeline_repo(self, mf_root, monkeypatch):
        # repos: list takes precedence over pipeline.repo scalar.
        monkeypatch.delenv("PIPELINE_REPO", raising=False)
        a = _git_repo(mf_root / "apps" / "a")
        _git_repo(mf_root / "apps" / "b")
        y = _write_yaml(mf_root, """
repos:
  - path: apps/a
    label: aaa
pipeline:
  repo: apps/b
""")
        got = RS.ensure_pipeline_repo(
            yaml_path=y, mf_root=mf_root, interactive=False,
        )
        assert got == a.resolve()


class TestEarlyFlag:
    def test_parse(self):
        assert RS.early_repo_flag(["--repo", "x", "start"]) == "x"
        assert RS.early_repo_flag(["--repo=y", "start"]) == "y"
        assert RS.early_repo_flag(["start", "001"]) is None
