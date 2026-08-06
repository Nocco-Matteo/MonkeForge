"""Shared yaml fixture helpers for TASK-032 batch 1 tests.

Provides a minimal baseline yaml (with the required ``agents:`` block so
``config.py`` imports cleanly) and a writer that drops it into a temp dir and
returns the path. Tests that need to scrub the real ``monkeforge.yaml`` set
``PIPELINE_WT_YAML`` to the fixture path so config.py loads a known-clean yaml
instead of the operator's real one.
"""
from __future__ import annotations

from pathlib import Path


# A minimal agents: block with every required role. Models/cmds are stubs —
# tests that need real agent invocation patch run_agent directly.
_BASELINE_AGENTS = """
agents:
  INTERVIEWER:    {model: stub, cmd: "echo {prompt}"}
  PROPOSER:       {model: stub, cmd: "echo {prompt}"}
  PLAN_REVIEWER:  {model: stub, cmd: "echo {prompt}"}
  IMPLEMENTER:    {model: stub, cmd: "echo {prompt}"}
  CODE_REVIEWER:  {model: stub, cmd: "echo {prompt}"}
  UX_REVIEWER:    {model: stub, cmd: "echo {prompt}"}
  VISUAL_REVIEWER: {model: stub, cmd: "echo {prompt}"}
  VISUAL_FIXER:   {model: stub, cmd: "echo {prompt}"}
  SUMMARIZER:     {model: stub, cmd: "echo {prompt}"}
  JUDGE:          {model: stub, cmd: "echo {prompt}"}
"""


def _baseline_yaml() -> str:
    """A minimal yaml string with the required ``agents:`` block and no product
    knobs (so every §3e default is observed)."""
    return _BASELINE_AGENTS


def _write_baseline_yaml(path: Path, *, extra: str = "") -> Path:
    """Write the baseline yaml to ``path`` (a .yaml file path) and return it.

    ``extra`` is appended verbatim — use it to add ``pipeline:``, ``notifications:``,
    ``discord:``, ``effort:`` blocks per test.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_baseline_yaml() + extra)
    return path
