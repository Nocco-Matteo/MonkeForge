"""Steps 3-4, 7-8: summary, judge, checkpoint, queue_scope, final_check, wrap_up."""

from __future__ import annotations

import json
import os

from langgraph.types import interrupt

import pipeline_graph.nodes as _N

from .. import config as C
from .. import events as ev
from .. import test_runner as tr
from ..agents import parse_not_met, render_prompt
from .common import (
    _db_note,
    _extract_json,
    _file_or_stdout,
    _recover_artifact,
    _save,
    _write_progress,
)

# --- Step 3 ---------------------------------------------------------------


def summary(state):
    tid = state["task_id"]
    prompt = render_prompt("summary", task_id=tid, docs_dir=str(C.DOCS))
    _, out = _N.run_agent("SUMMARIZER", tid, "summary", prompt)
    summary_path = C.DEBATES / f"SUMMARY-{tid}.md"
    _file_or_stdout(summary_path, out)
    if not summary_path.exists():
        _recover_artifact(tid, f"SUMMARY-{tid}.md", summary_path)
    return {"journal": ["summary: written"]}


# --- Step 4 ---------------------------------------------------------------


def judge(state):
    tid = state["task_id"]
    prompt = render_prompt("judge", task_id=tid, docs_dir=str(C.DOCS))
    code, out = _N.run_agent("JUDGE", tid, "verdict", prompt)

    if out.strip().startswith("ESCALATE:") or "\nESCALATE:" in out:
        reason = out.split("ESCALATE:", 1)[1].strip().splitlines()[0]
        return {"escalation": f"judge escalated: {reason}", "journal": ["judge: escalated"]}

    batches_file = C.FINAL / f"BATCHES-{tid}.json"
    # Try to parse BATCHES json from agent stdout first
    batches_json = _extract_json(out)
    if batches_json:
        _save(batches_file, json.dumps(batches_json, indent=2))
    # Save the FINAL report from stdout (everything before the BATCHES json)
    final_path = C.FINAL / f"FINAL-{tid}.md"
    if out.strip():
        _save(final_path, out)
    if not final_path.exists():
        _recover_artifact(tid, f"FINAL-{tid}.md", final_path)
    if not batches_file.exists():
        _recover_artifact(tid, f"BATCHES-{tid}.json", batches_file)
        return {
            "escalation": "judge did not produce BATCHES json",
            "journal": ["judge: no batch file"],
        }
    try:
        raw = json.loads(batches_file.read_text())
    except json.JSONDecodeError as exc:
        return {"escalation": f"BATCHES json invalid: {exc}", "journal": ["judge: bad batch json"]}

    batches = [
        {
            "n": b["n"],
            "scope": b.get("scope", ""),
            "status": "PENDING",
            "outcome": "",
            "deviations": "",
            "checklist": b.get("checklist", []),
            "test_failure_allowlist": b.get("test_failure_allowlist", []),
        }
        for b in raw
    ]
    # has_ui was decided at plan-time (before the debate) and is not the judge's
    # to reset; the UX critique already happened during the debate.
    _write_progress(tid, batches)
    return {"batches": batches, "batch_idx": 0, "journal": [f"judge: {len(batches)} batches"]}


def checkpoint_plan(state):
    """Human gate after the verdict (skipped in AUTO)."""
    if state.get("auto"):
        return {"journal": ["checkpoint plan: auto, skipped"]}
    decision = interrupt(
        {
            "stage": "plan approved?",
            "task": state["task_id"],
            "batches": [f"{b['n']}: {b['scope']}" for b in state.get("batches", [])],
            "final": str(C.FINAL / f"FINAL-{state['task_id']}.md"),
        }
    )
    if str(decision).strip().lower() not in ("ok", "yes", "approve", "y"):
        return {
            "escalation": f"user rejected the plan: {decision}",
            "journal": ["checkpoint plan: rejected"],
        }
    return {"journal": ["checkpoint plan: approved"]}


# --- Step 5: (UX review folded into the debate; no post-judge UX nodes) ------


def queue_scope(state):
    """Record discovered-but-out-of-scope work instead of doing it."""
    tid = state["task_id"]
    note = state.get("discovered_scope", "").strip()
    if not note:
        return {"journal": []}
    path = C.QUEUE / f"from-task-{tid}-{int(__import__('time').time())}.md"
    path.write_text(f"# Discovered during TASK-{tid}\n\n{note}\n")
    return {"discovered_scope": "", "journal": [f"queued new scope: {path.name}"]}


# --- Steps 7-8 -------------------------------------------------------------

FINAL_FIX_ATTEMPTS = 2
FINAL_FIX_MAX_FAILURES_PER_ATTEMPT = 5
FINAL_FIX_TIMEOUT = int(os.environ.get("PIPELINE_FINAL_FIX_TIMEOUT", "600")) or None


def _final_test_fix_loop(tid: str, db_ok: bool, baseline: set[str]) -> dict | None:
    """Run the full suite after all batches; auto-fix only NEW failures.

    Baseline-aware, like the per-batch gate: failures already present at task
    start (`baseline`) predate the task and are TOLERATED — the final gate does
    NOT send the fixer to "fix" them. Fixing pre-existing failures out of scope
    is how TASK-010, a frontend-only task, ended up deleting backend combat
    actions to turn a pre-existing red test green. Only regressions this task
    introduced are auto-fixed.

    Returns None if there are no new failures (green, or only baseline remains,
    or DB down / dry run), or a dict with an escalation if new failures remain
    after FINAL_FIX_ATTEMPTS.
    """
    if C.DRY_RUN or not db_ok:
        return None

    _, all_failures, summary = tr.run_repo_tests()
    failures = tr.new_failures_since_baseline(all_failures, baseline, [])
    if not failures:
        return None

    n0 = len(failures)
    ev.emit(
        "degraded",
        tid,
        "final_check",
        f"{n0} NEW test failure(s) after all batches ({summary}; "
        f"{len(all_failures) - n0} pre-existing tolerated). "
        f"Attempting auto-fix ({FINAL_FIX_ATTEMPTS} attempts).",
    )

    for attempt in range(1, FINAL_FIX_ATTEMPTS + 1):
        batch = sorted(failures)[:FINAL_FIX_MAX_FAILURES_PER_ATTEMPT]
        fail_list = "\n".join(batch)
        remaining = len(failures) - len(batch)
        remaining_label = (
            f"({remaining} more failure(s) will be handled in "
            f"later attempts — ignore them for now.)"
            if remaining
            else ""
        )
        prompt = render_prompt(
            "preflight_fix",
            task_id=tid,
            failures=fail_list,
            summary=summary,
            remaining_label=remaining_label,
        )
        _, out = _N.run_agent(
            "IMPLEMENTER", tid, f"final-fix-{attempt}", prompt, timeout=FINAL_FIX_TIMEOUT
        )
        _, all_failures, summary = tr.run_repo_tests()
        failures = tr.new_failures_since_baseline(all_failures, baseline, [])
        if not failures:
            ev.emit(
                "step_end",
                tid,
                "final_check",
                f"auto-fix attempt {attempt}: no new failures ({summary})",
            )
            _N._stage_all()
            _N._git("commit", "-m", f"final gate: fix {n0} new test failure(s)")
            return None
        ev.emit(
            "step_end",
            tid,
            "final_check",
            f"auto-fix attempt {attempt}: {len(failures)} new still failing ({summary})",
        )

    n = len(failures)
    return {
        "escalation": f"final test gate: {n} NEW test(s) still failing after "
        f"{FINAL_FIX_ATTEMPTS} auto-fix attempt(s) "
        f"({summary}). Proceed and ship with known failures, "
        f"or stop and fix manually.",
        "journal": f"final check: {n0} new initially, {n} still failing "
        f"after {FINAL_FIX_ATTEMPTS} fix attempt(s)",
    }


def final_check(state):
    tid = state["task_id"]
    db_ok, db_note = _db_note(tid, "final_check")

    # Test gate: run the full suite and auto-fix any failures before the
    # LLM checklist review.  This catches both pre-existing baseline
    # failures that passed through the per-batch gate, and any regressions
    # that a degraded DB let through.
    test_result = (
        None
        if state.get("final_tests_waived")
        else _final_test_fix_loop(tid, db_ok, set(state.get("task_baseline") or []))
    )
    if test_result:
        suffix = "" if db_ok else " (DB-gated tests skipped: e2e Postgres unreachable)"
        delta = {
            "not_met": [],
            "db_degraded": not db_ok,
            "escalation": test_result["escalation"],
            "journal": [test_result["journal"] + suffix],
        }
        if not db_ok:
            delta["degradations"] = [
                "e2e DB unreachable at final check — DB-backed tests were skipped, not passed"
            ]
        return delta

    prompt = render_prompt("final_check", task_id=tid, db_note=db_note, docs_dir=str(C.DOCS))
    _, out = _N.run_agent("IMPLEMENTER", tid, "final-check", prompt)
    not_met = parse_not_met(out)
    suffix = "" if db_ok else " (DB-gated tests skipped: e2e Postgres unreachable)"
    delta = {
        "not_met": not_met,
        "db_degraded": not db_ok,
        "escalation": f"final gate: {len(not_met)} checklist items NOT MET" if not_met else "",
        "journal": [f"final check: {len(not_met)} not met" + suffix],
    }
    if not db_ok:
        delta["degradations"] = [
            "e2e DB unreachable at final check — DB-backed tests were skipped, not passed"
        ]
    return delta


def wrap_up(state):
    tid = state["task_id"]
    # One source of truth: the degradation ledger. Every "shipped with a
    # compromise" appended to it during the run (DB skipped, tests waived, UX /
    # visual blockers shipped, red baseline, force-closed batch) surfaces here,
    # so the human reviews the list at the end instead of being interrupted for
    # each one mid-run.
    degradations = state.get("degradations", [])
    warnings = [f"DEGRADED: {d}" for d in degradations]
    lines = [
        f"TASK-{tid} complete on {state.get('branch')}",
        f"batches: {len(state.get('batches', []))}",
        f"degradations: {len(degradations)}" if degradations else "degradations: none",
        *warnings,
        *state.get("journal", [])[-12:],
    ]
    report = "\n".join(lines)
    (C.FINAL / f"REPORT-{tid}.md").write_text(report + "\n")
    ev.emit(
        "run_end",
        tid,
        "wrap_up",
        f"all {len(state.get('batches', []))} batches done on "
        f"{state.get('branch')}, branch ready for review"
        + (f" — {len(degradations)} degradation(s), see the report" if degradations else ""),
        degraded=bool(degradations),
        degradations=degradations,
    )
    return {"finished": True, "journal": ["wrap up: report written"]}
