"""Step 6b/6c: visual review gate and render (perf) gate."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path

import pipeline_graph.nodes as _N

from .. import config as C
from ..agents import count_blockers, parse_verdict, read_if_exists
from ..state import Conversation
from .common import _db_note, _stage_all


def _screens_dir(tid: str) -> Path:
    return C.SCREENS / f"task-{tid}"


def ux_render(state):
    tid = state["task_id"]
    cyc = state.get("ux_render_cycle", 0)
    if C.DRY_RUN or not C.UX_RENDER_CMD.strip():
        return {"render_facts": "{}", "journal": ["ux render: skipped (dry run / disabled)"]}

    # The render drives the real frontend+backend: the e2e stack must be up.
    db_ok, _ = _db_note(tid, "ux_render")
    if not db_ok:
        return {
            "escalation": "cannot render the UI: the e2e stack is not reachable",
            "journal": ["ux render: e2e stack down"],
        }

    # Ensure the render fixtures exist (idempotent upsert). Without this the gate
    # breaks silently whenever the e2e DB was re-seeded: the fixed character ids
    # the spec navigates to would not exist and every render would time out.
    if C.UX_SEED_SCRIPT.exists():
        seed = subprocess.run(
            ["bash", str(C.UX_SEED_SCRIPT)], cwd=C.REPO, capture_output=True, text=True
        )
        if seed.returncode != 0:
            return {
                "escalation": "cannot render the UI: seeding the render fixtures "
                f"failed — {(seed.stderr or seed.stdout or '')[-300:]}",
                "journal": ["ux render: fixture seed failed"],
            }

    out_dir = _screens_dir(tid)
    out_dir.mkdir(parents=True, exist_ok=True)
    for p in list(out_dir.glob("*.png")):
        p.unlink()
    (out_dir / "facts.json").unlink(missing_ok=True)

    env = os.environ.copy()
    env["UX_RENDER_OUT"] = str(out_dir)
    env["E2E_REUSE_SERVER"] = "1"
    log = C.RAW / f"{tid}-ux-render-c{cyc}-{int(time.time())}.log"
    try:
        proc = subprocess.run(
            shlex.split(C.UX_RENDER_CMD),
            cwd=C.REPO / C.UX_RENDER_CWD,
            env=env,
            capture_output=True,
            text=True,
            timeout=C.UX_RENDER_TIMEOUT,
        )
        log.write_text((proc.stdout or "") + "\n--- stderr ---\n" + (proc.stderr or ""))
    except subprocess.TimeoutExpired:
        return {
            "escalation": f"UI render timed out after {C.UX_RENDER_TIMEOUT}s "
            "(fixture creation is slow on a cold cache; raise "
            "PIPELINE_UX_RENDER_TIMEOUT or warm the fixtures)",
            "journal": ["ux render: timed out"],
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "escalation": f"UI render command failed to run: {exc}",
            "journal": ["ux render: command error"],
        }

    shots = sorted(out_dir.glob("*.png"))
    facts = read_if_exists(out_dir / "facts.json") or "{}"
    if not shots:
        return {
            "escalation": f"could not render the UI: no screenshots produced "
            f"(render exited {proc.returncode}); see {log.name}",
            "journal": ["ux render: no screenshots"],
        }
    return {
        "render_facts": facts,
        "journal": [f"ux render: {len(shots)} screenshot(s), facts captured"],
    }


def ux_visual_review(state):
    tid = state["task_id"]
    cyc = state.get("ux_render_cycle", 0)
    shots = _screens_dir(tid)
    conv = Conversation.from_state(state)

    review, verdict = "", "UNKNOWN"
    for attempt in range(2):
        step = f"visual-c{cyc}" + (f"-retry{attempt}" if attempt else "")
        _, out = _N.run_agent(
            "VISUAL_REVIEWER",
            conv,
            step,
            template="visual_review",
            screens_dir=str(shots.relative_to(C.REPO)) if shots.is_relative_to(C.REPO) else str(shots),
            render_facts=state.get("render_facts", "{}"),
        )
        verdict = parse_verdict(out)
        if verdict != "UNKNOWN":
            review = out
            break

    if verdict == "UNKNOWN":
        return {
            "visual_verdict": "UNKNOWN",
            "visual_blockers": 0,
            "escalation": "visual reviewer produced no usable review — inspect "
            f"the screenshots in {shots} by hand",
            "journal": [f"visual c{cyc}: FAILED — no verdict"],
        }

    (C.REVIEWS / f"VISUAL-{tid}.md").write_text(review)
    blockers = count_blockers(review)
    rel = shots.relative_to(C.REPO) if shots.is_relative_to(C.REPO) else shots

    # Plateau detection: the fix loop can oscillate — the blocker COUNT stays flat
    # while the specific issues rotate between modes (task-009: 4→2→2→2→2). Track
    # consecutive non-improving cycles and escalate EARLY instead of burning every
    # cycle on a problem the loop cannot converge (usually a design decision).
    prev = state.get("prev_visual_blockers")
    no_progress = state.get("visual_no_progress", 0)
    if prev is not None and blockers and blockers >= prev:
        no_progress += 1
    else:
        no_progress = 0
    delta = {
        "visual_verdict": verdict,
        "visual_blockers": blockers,
        "prev_visual_blockers": blockers,
        "visual_no_progress": no_progress,
        "journal": [f"visual c{cyc}: {verdict}, {blockers} blockers"],
    }

    if blockers and no_progress >= C.PLATEAU_THRESHOLD:
        delta["escalation"] = (
            f"the visual fix loop made no progress over {no_progress + 1} cycles "
            f"({blockers} blockers, oscillating between modes) — this usually needs a "
            f"design decision or a targeted manual fix, not more cycles. Screenshots "
            f"in {rel}; accept and ship, or stop and fix, then redo --from visual"
        )
    elif blockers and cyc >= C.MAX_UX_RENDER_CYCLES:
        delta["escalation"] = (
            f"visual issues remain after {C.MAX_UX_RENDER_CYCLES} render/fix cycle(s) — "
            f"screenshots in {rel}; fix them or accept and ship"
        )
    return delta


def ux_visual_fix(state):
    tid = state["task_id"]
    cyc = state.get("ux_render_cycle", 0) + 1
    shots = _screens_dir(tid)
    # VISUAL_FIXER (claude), not the blind IMPLEMENTER: it OPENS the screenshots,
    # so a fix to one mode does not silently break the other (the whack-a-mole
    # that plateaued task-009 at 2 blockers across 4 cycles).
    conv = Conversation.from_state(state)
    _N.run_agent(
        "VISUAL_FIXER",
        conv,
        f"visual-fix-c{cyc}",
        template="visual_fix",
        screens_dir=str(shots.relative_to(C.REPO)) if shots.is_relative_to(C.REPO) else str(shots),
        render_facts=state.get("render_facts", "{}"),
        docs_dir=C.DOCS_REL,
    )
    _stage_all()
    return {
        "ux_render_cycle": cyc,
        "journal": [f"visual fix c{cyc}: applied (with eyes), re-rendering"],
    }


# --- Step 6c: render gate (the perf analog of the visual gate) -------------
# Deterministic: drives a scripted interaction, counts re-renders per subtree
# (window.__RENDER_LOG__ via <Profiler>), compares to a baseline. Any subtree
# re-rendering MORE than baseline is a regression → block. No LLM critic.


def _renders_dir(tid: str) -> Path:
    return C.RENDERS / f"task-{tid}"


def render_measure(state):
    """Run the render-profile spec (same e2e stack + seed as ux_render); it writes
    render-facts.json. Facts live on disk; render_review reads and judges them."""
    tid = state["task_id"]
    cyc = state.get("render_cycle", 0)
    if C.DRY_RUN or not C.RENDER_CMD.strip():
        return {"journal": ["render measure: skipped (dry run / disabled)"]}

    db_ok, _ = _db_note(tid, "render_measure")
    if not db_ok:
        return {
            "escalation": "cannot profile renders: the e2e stack is not reachable",
            "journal": ["render measure: e2e stack down"],
        }
    if C.UX_SEED_SCRIPT.exists():
        seed = subprocess.run(
            ["bash", str(C.UX_SEED_SCRIPT)], cwd=C.REPO, capture_output=True, text=True
        )
        if seed.returncode != 0:
            return {
                "escalation": "cannot profile renders: seeding the fixtures failed — "
                f"{(seed.stderr or seed.stdout or '')[-300:]}",
                "journal": ["render measure: fixture seed failed"],
            }

    out_dir = _renders_dir(tid)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "render-facts.json").unlink(missing_ok=True)

    env = os.environ.copy()
    env["RENDER_PROFILE_OUT"] = str(out_dir)
    env["E2E_REUSE_SERVER"] = "1"
    env["NEXT_PUBLIC_PROFILE"] = "1"  # activates the app's <Profiler> collector
    log = C.RAW / f"{tid}-render-measure-c{cyc}-{int(time.time())}.log"
    try:
        proc = subprocess.run(
            shlex.split(C.RENDER_CMD),
            cwd=C.REPO / C.RENDER_CWD,
            env=env,
            capture_output=True,
            text=True,
            timeout=C.RENDER_TIMEOUT,
        )
        log.write_text((proc.stdout or "") + "\n--- stderr ---\n" + (proc.stderr or ""))
    except subprocess.TimeoutExpired:
        return {
            "escalation": f"render profiling timed out after {C.RENDER_TIMEOUT}s "
            "(raise PIPELINE_RENDER_TIMEOUT or warm the fixtures)",
            "journal": ["render measure: timed out"],
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "escalation": f"render profiling command failed to run: {exc}",
            "journal": ["render measure: command error"],
        }

    if not (out_dir / "render-facts.json").exists():
        return {
            "escalation": f"could not profile renders: no render-facts.json "
            f"(spec exited {proc.returncode}); see {log.name}",
            "journal": ["render measure: no facts"],
        }
    return {"journal": [f"render measure c{cyc}: facts captured"]}


def render_review(state):
    """Deterministic gate: compare re-render counts to the baseline. Any subtree
    re-rendering MORE than baseline is a regression → block. Pure arithmetic."""
    tid = state["task_id"]
    cyc = state.get("render_cycle", 0)
    raw = read_if_exists(_renders_dir(tid) / "render-facts.json")
    if not raw:
        return {
            "render_verdict": "SKIPPED",
            "render_blockers": 0,
            "journal": ["render review: no facts (measure skipped)"],
        }
    try:
        facts = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "escalation": "render gate: render-facts.json is not valid JSON",
            "journal": ["render review: bad facts json"],
        }

    if not facts.get("instrumented"):
        # No <Profiler> hooks yet — degrade, do not block. The first perf task's
        # instrumentation batch adds them, then the gate goes live.
        return {
            "render_verdict": "SKIPPED",
            "render_blockers": 0,
            "degradations": [
                "render gate skipped: app not instrumented "
                "(no <Profiler>/window.__RENDER_LOG__ yet)"
            ],
            "journal": ["render review: SKIPPED — app not instrumented"],
        }

    current = facts.get("renders", {}) or {}
    baseline_path = C.RENDERS / f"baseline-{tid}.json"
    baseline_raw = read_if_exists(baseline_path)
    if not baseline_raw:
        # First measurement establishes the baseline (captured on the PRE-change
        # tree per the brief); nothing to compare against yet.
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps(facts, indent=2))
        return {
            "render_verdict": "BASELINE",
            "render_blockers": 0,
            "journal": [
                f"render review: baseline established "
                f"({sum(current.values())} re-renders on "
                f"'{facts.get('interaction', '?')}') — re-run after changes"
            ],
        }

    baseline = json.loads(baseline_raw).get("renders", {}) or {}
    regressions = {k: (baseline.get(k, 0), v) for k, v in current.items() if v > baseline.get(k, 0)}
    improvements = {
        k: (baseline.get(k, 0), v) for k, v in current.items() if v < baseline.get(k, 0)
    }
    blockers = len(regressions)
    lines = [f"# Render delta vs baseline — {facts.get('interaction', '?')}", ""]
    lines += [f"REGRESSION  {k}: {b} -> {c}" for k, (b, c) in sorted(regressions.items())]
    lines += [f"improved    {k}: {b} -> {c}" for k, (b, c) in sorted(improvements.items())]
    (_renders_dir(tid) / "render-delta.md").write_text("\n".join(lines) + "\n")

    prev = state.get("prev_render_blockers")
    no_progress = state.get("render_no_progress", 0)
    if prev is not None and blockers and blockers >= prev:
        no_progress += 1
    else:
        no_progress = 0
    delta = {
        "render_verdict": "REGRESSED" if blockers else "PASS",
        "render_blockers": blockers,
        "prev_render_blockers": blockers,
        "render_no_progress": no_progress,
        "journal": [
            f"render review c{cyc}: {blockers} regression(s), {len(improvements)} improvement(s)"
        ],
    }
    delta_file = _renders_dir(tid) / "render-delta.md"
    if blockers and no_progress >= C.PLATEAU_THRESHOLD:
        delta["escalation"] = (
            f"render fix loop made no progress over {no_progress + 1} cycles "
            f"({blockers} re-render regression(s)) — see {delta_file}; "
            "accept and ship, or fix manually then redo"
        )
    elif blockers and cyc >= C.MAX_RENDER_CYCLES:
        delta["escalation"] = (
            f"re-render regressions remain after {C.MAX_RENDER_CYCLES} cycle(s) — "
            f"see {delta_file}; fix them or accept and ship"
        )
    return delta


def render_fix(state):
    """The implementer fixes the re-render regressions with the numeric delta in
    hand (no pixels to open, unlike the visual fixer — the metric IS the report)."""
    tid = state["task_id"]
    cyc = state.get("render_cycle", 0) + 1
    delta = read_if_exists(_renders_dir(tid) / "render-delta.md") or "(no delta file)"
    conv = Conversation.from_state(state)
    _N.run_agent(
        "IMPLEMENTER",
        conv,
        f"render-fix-c{cyc}",
        template="render_fix",
        render_delta=delta,
    )
    _stage_all()
    return {"render_cycle": cyc, "journal": [f"render fix c{cyc}: applied, re-profiling"]}
