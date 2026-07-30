"""Step 1: plan node and UI/perf surface detection."""

from __future__ import annotations

import re
from pathlib import Path

from .. import config as C
from ..agents import read_if_exists, render_prompt, run_agent
from .common import _file_or_stdout, _recover_artifact, _rel
from .intake import _seed_brief


def plan(state):
    tid = state["task_id"]
    # Never hand the proposer a path that is not there: an interview that
    # escalated at the round cap can reach this node with no brief written.
    brief = _seed_brief(tid, state["request"])
    prompt = render_prompt(
        "plan",
        task_id=tid,
        request=state["request"],
        brief_path=str(_rel(brief)),
        arch_docs=C.arch_docs_block(),
        docs_dir=C.DOCS_REL,
    )
    code, out = run_agent("PROPOSER", tid, "plan", prompt)
    if code != 0:
        return {"escalation": f"plan step failed (exit {code})", "journal": ["plan: failed"]}
    plan_path = C.PLANS / f"PLAN-{tid}.md"
    _file_or_stdout(plan_path, out)
    if not plan_path.exists():
        _recover_artifact(tid, f"PLAN-{tid}.md", plan_path)
    if not plan_path.exists():
        return {"escalation": "proposer produced no plan output", "journal": ["plan: no output"]}
    # Decide has_ui here, before the debate, so the UX critic joins only when
    # there is a user surface. The analyst (brief) is the authority; fall back to
    # a keyword scan, and when in doubt run the UX critic (a wasted pass beats a
    # missed one). This is what the old post-judge HAS_UI gate becomes.
    has_ui = _detect_has_ui(brief, tid)
    has_perf = _detect_has_perf(brief, tid)
    return {
        "has_ui": has_ui,
        "has_perf": has_perf,
        "debate_round": 0,
        "tech_limits": [],
        "journal": [f"plan: written, has_ui={has_ui}, has_perf={has_perf}"],
    }


UI_SURFACE_RE = re.compile(r"^\s*UI-SURFACE\s*:\s*(yes|no)\b", re.MULTILINE | re.IGNORECASE)
PERF_SURFACE_RE = re.compile(r"^\s*PERF-SURFACE\s*:\s*(yes|no)\b", re.MULTILINE | re.IGNORECASE)


def _detect_has_ui(brief_path: Path, tid: str) -> bool:
    """The brief's UI-SURFACE marker decides; without one, default to running the
    UX critic — a wasted pass on a backend-only task beats skipping UX on a task
    that turns out to have a surface."""
    text = read_if_exists(brief_path) + "\n" + read_if_exists(C.PLANS / f"PLAN-{tid}.md")
    m = UI_SURFACE_RE.search(text)
    return m.group(1).lower() == "yes" if m else True


def _detect_has_perf(brief_path: Path, tid: str) -> bool:
    """The render gate is OPT-IN: only a brief that explicitly marks
    `PERF-SURFACE: yes` runs it. Unlike UI-SURFACE it defaults to no — the render
    gate needs <Profiler> instrumentation and a scripted interaction, so it must
    not fire on a task that never asked for it."""
    text = read_if_exists(brief_path) + "\n" + read_if_exists(C.PLANS / f"PLAN-{tid}.md")
    m = PERF_SURFACE_RE.search(text)
    return m.group(1).lower() == "yes" if m else False
