"""Step 1: plan node, effort checkpoint, and UI/perf surface detection."""

from __future__ import annotations

import re
from pathlib import Path

from langgraph.types import interrupt

from .. import config as C
from ..agents import read_if_exists, run_agent
from ..state import Conversation
from .common import _file_or_stdout, _recover_artifact
from .intake import _seed_brief


def plan(state):
    tid = state["task_id"]
    # Never hand the proposer a path that is not there: an interview that
    # escalated at the round cap can reach this node with no brief written.
    brief = _seed_brief(tid, state["request"])
    conv = Conversation.from_state(state)
    # `request` is also a Conversation field but passing it in extra_kw is
    # harmless (same value); kept explicit for parity with the old call so the
    # rendered text is byte-identical.
    code, out = run_agent(
        "PROPOSER",
        conv,
        "plan",
        template="plan",
        request=state["request"],
        arch_docs=C.arch_docs_block(),
        docs_dir=C.DOCS_REL,
    )
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
    # Extract effort hint signals from the plan + brief for the effort checkpoint
    # to consume. `plan` does NOT set `effort` itself — that is the checkpoint's
    # job (or the --effort flag, which short-circuits it).
    plan_text = read_if_exists(plan_path)
    brief_text = read_if_exists(brief)
    signals = _extract_effort_signals(plan_text, brief_text, has_ui, has_perf)
    return {
        "has_ui": has_ui,
        "has_perf": has_perf,
        "effort_hint_signals": signals,
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


# --- Adaptive effort (TASK-011) --------------------------------------------

# Tokens that look like file paths in a plan: a path component with a dot and a
# short extension. Backticks are optional; we strip them.
_EFFORT_FILE_RE = re.compile(
    r"`?([A-Za-z0-9_./\-]+\.[A-Za-z]{1,6})`?"
)

# File extensions we treat as real source files (filters out version numbers,
# sentence fragments, etc. that the regex catches incidentally).
_EFFORT_FILE_EXTS = frozenset(
    ".py .ts .tsx .js .jsx .mjs .cjs .json .yaml .yml .toml .css .scss .html .vue .svelte "
    .split()
)


def _extract_effort_signals(plan_text: str, brief: str, has_ui: bool, has_perf: bool) -> dict:
    """Distil the plan + brief into the signals `_recommend_effort` consumes.

    Pure: no I/O, no interrupt. Returns a dict with exactly the keys
    ``files``, ``file_count``, ``critical_path_hits``, ``cross_layer``,
    ``plan_chars``, ``has_ui``, ``has_perf``.
    """
    text = (plan_text or "") + "\n" + (brief or "")
    raw_files = _EFFORT_FILE_RE.findall(text)
    files = sorted({
        f for f in raw_files
        if Path(f).suffix.lower() in _EFFORT_FILE_EXTS and len(f) <= 200
    })
    # critical_path_hits: count files whose path matches a configured critical
    # path (by exact, suffix, or basename match).
    critical_hits = 0
    for f in files:
        for cp in C.EFFORT_CRITICAL_PATHS:
            if f == cp or f.endswith("/" + cp) or Path(f).name == Path(cp).name:
                critical_hits += 1
                break
    # cross_layer: files span more than one top-level directory (e.g. both
    # `pipeline_graph/` and `frontend/`).
    top_dirs = set()
    for f in files:
        parts = f.split("/", 1)
        if len(parts) > 1:
            top_dirs.add(parts[0])
        else:
            top_dirs.add("(root)")
    cross_layer = len(top_dirs) > 1
    return {
        "files": files,
        "file_count": len(files),
        "critical_path_hits": critical_hits,
        "cross_layer": cross_layer,
        "plan_chars": len(plan_text or ""),
        "has_ui": has_ui,
        "has_perf": has_perf,
    }


def _recommend_effort(signals: dict) -> str:
    """Pick an effort level from the plan signals (pure: no I/O, no interrupt).

    Rule order — barrel first, then scout, then troop (the default):
      * barrel-monke: touches a critical path, spans layers, or is a perf task.
      * scout-monke:  small, single-layer, no UI/perf, short plan.
      * troop-monke:  everything else (the default behaviour).
    """
    # barrel-monke — maximum effort for risky changes.
    if (signals.get("critical_path_hits", 0) > 0
            or signals.get("cross_layer")
            or signals.get("has_perf")):
        return "barrel-monke"
    # scout-monke — minimum effort for small, contained changes.
    if (signals.get("file_count", 0) <= 3
            and not signals.get("has_ui")
            and signals.get("plan_chars", 0) < 4000
            and signals.get("critical_path_hits", 0) == 0
            and not signals.get("cross_layer")):
        return "scout-monke"
    # troop-monke — the default.
    return "troop-monke"


def checkpoint_effort(state):
    """Human gate between plan and debate: pick an effort level.

    Three paths (checked in order):
      1. ``effort`` already set to a known level (--effort flag or a resume):
         accept it and move on — no interrupt (C12).
      2. ``auto`` mode: take the computed hint silently — no interrupt (C13).
      3. Interactive: ask the human; ``ok``/empty/unrecognized → the hint (C14).
    """
    # 1. Already set: short-circuit.
    if state.get("effort") in C.EFFORT_LEVELS:
        return {"journal": [f"effort: already set to {state['effort']}"]}
    # Compute the hint from the signals `plan` stored (re-extract as a fallback
    # for a resume from a pre-feature checkpoint that has none).
    signals = state.get("effort_hint_signals")
    if not signals:
        signals = _extract_effort_signals("", "", state.get("has_ui", False),
                                          state.get("has_perf", False))
    hint = _recommend_effort(signals)
    # 2. Auto: take the hint silently.
    if state.get("auto"):
        return {"effort": hint, "journal": [f"effort: auto, took hint {hint}"]}
    # 3. Interactive: ask the human.
    decision = interrupt(
        {
            "stage": "effort level",
            "task": state["task_id"],
            "hint": hint,
            "signals": signals,
            "levels": list(C.EFFORT_LEVELS),
        }
    )
    answer = str(decision).strip().lower()
    if answer in C.EFFORT_LEVELS:
        chosen = answer
    else:
        # "ok", empty, or unrecognized → take the hint.
        chosen = hint
    return {
        "effort": chosen,
        "effort_checkpoint_shown": True,
        "effort_forced": False,
        "journal": [f"effort: {chosen} (hint was {hint})"],
    }
