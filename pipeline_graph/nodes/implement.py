"""Step 6: implement node, test-fix loop, and close_batch."""

from __future__ import annotations

import subprocess

import pipeline_graph.nodes as _N

from .. import config as C
from .. import events as ev
from .. import test_runner as tr
from ..agents import render_prompt
from .common import _current_batch, _db_note, _git, _stage_all, _write_progress


def _capture_test_baseline(state, b: dict, db_ok: bool) -> dict:
    """Snapshot failing tests before the implementer edits (once per batch)."""
    if state.get("baseline_batch_n") == b["n"] or not db_ok or C.DRY_RUN:
        return {}
    _, failures, summary = tr.run_repo_tests()
    ev.emit(
        "step_start",
        state["task_id"],
        "implement",
        f"test baseline batch {b['n']}: {len(failures)} failing ({summary})",
    )
    ev.emit("step_end", state["task_id"], "implement", "test baseline captured")
    delta = {"baseline_batch_n": b["n"], "batch_test_baseline": sorted(failures)}
    if not state.get("task_baseline"):
        # The very first capture (batch 1, before any edits) is the failures
        # present at task START. The FINAL gate tolerates these — they predate
        # the task — instead of sending the fixer to "fix" them (which, out of
        # scope, gutted the backend on TASK-010). Never overwritten by later batches.
        delta["task_baseline"] = sorted(failures)
    # The gate only forbids NEW failures, so a red base is silently tolerated for
    # the whole task — regressions already on the branch get laundered as
    # "pre-existing". Surface it once, loudly, and carry it to the final report.
    if failures and b["n"] == state.get("batches", [{}])[0].get("n", 1):
        ev.emit(
            "degraded",
            state["task_id"],
            "implement",
            f"branch starts RED: {len(failures)} test(s) already failing before "
            "any batch. New failures are still blocked, but these are tolerated "
            "as baseline — fix them or they hide behind the gate.",
        )
        delta["baseline_failures"] = len(failures)
        delta["degradations"] = [
            f"branch started RED: {len(failures)} test(s) already failing "
            "before any batch — tolerated as baseline"
        ]
    return delta


def _in_graph_test_gate(state, b: dict, db_ok: bool) -> tuple[bool, list[str], str]:
    if state.get("tests_waived"):
        return True, [], "tests waived"
    if not db_ok or C.DRY_RUN:
        return True, [], "test gate skipped (db down or dry run)"

    _, current, summary = tr.run_repo_tests()
    baseline = set(state.get("batch_test_baseline") or [])
    allow = b.get("test_failure_allowlist") or []
    new = sorted(tr.new_failures_since_baseline(current, baseline, allow))
    return len(new) == 0, new, summary


def implement(state):
    tid = state["task_id"]
    b = _current_batch(state)
    if state.get("tests_waived"):
        return {
            "test_fix_attempt": 0,
            "fix_cycle": 0,
            "journal": [f"impl b{b['n']}: skipped (tests waived)"],
        }

    db_ok, db_note = _db_note(tid, "implement")
    attempt = state.get("test_fix_attempt", 0)
    baseline_delta = _capture_test_baseline(state, b, db_ok)
    # Record the batch's base commit (HEAD before any edits) so code_review diffs
    # against it, not HEAD — the batch's work then shows whether the implementer
    # left it staged OR committed it (the false-REJECT that hit TASK-010 b1/b5).
    if attempt == 0 and not C.DRY_RUN:
        baseline_delta = {**baseline_delta, "batch_base_ref": _git("rev-parse", "HEAD").strip()}
    merged = {**state, **baseline_delta}

    prompt = render_prompt(
        "implement",
        task_id=tid,
        batch_n=b["n"],
        batch_scope=b["scope"],
        db_note=db_note,
        arch_docs=C.arch_docs_block(),
        checklist_items=", ".join(map(str, b.get("checklist", []))),
        docs_dir=str(C.DOCS),
    )
    step = f"impl-b{b['n']}"
    if attempt:
        step += f"-fix{attempt}"
    code, out = _N.run_agent("IMPLEMENTER", tid, step, prompt)

    if "discrepancy" in out.lower() and "plan" in out.lower():
        return {
            **baseline_delta,
            "escalation": f"implementer reported a plan/code discrepancy (batch {b['n']})",
            "journal": [f"impl b{b['n']}: discrepancy"],
        }

    if code < 0:
        # Killed by a signal: run_agent already retried this transient death with
        # backoff (classify_output maps code<0 to transient), so a persistent
        # negative code means the environment is still broken — OOM, or the agent
        # daemon crashed (SIGPIPE/-13). A "fix" prompt is meaningless when the
        # agent produced nothing, so escalate as infrastructure without spending a
        # test-fix cycle; the human fixes the environment and resumes with `ok`.
        return {
            **baseline_delta,
            "escalation": f"implementer process killed by signal {-code} "
            f"(infrastructure — likely OOM or agent-daemon crash), "
            f"batch {b['n']}",
            "journal": [
                f"impl b{b['n']}: agent killed by signal {-code} "
                f"(infra, not a code fault), escalating"
            ],
        }

    if code != 0:
        if attempt + 1 > C.MAX_TEST_FIXES:
            return {
                **baseline_delta,
                "escalation": f"implementer failed after {C.MAX_TEST_FIXES} "
                f"attempts (batch {b['n']})",
                "journal": [f"impl b{b['n']}: agent exit {code}, giving up"],
            }
        return {
            **baseline_delta,
            "test_fix_attempt": attempt + 1,
            "journal": [f"impl b{b['n']}: agent exit {code}, retry {attempt + 1}"],
        }

    ok, new_fails, summary = _in_graph_test_gate(merged, b, db_ok)
    if not ok:
        detail = f"{len(new_fails)} new failure(s): {new_fails[0][:120]}" if new_fails else summary
        if attempt + 1 > C.MAX_TEST_FIXES:
            return {
                **baseline_delta,
                "escalation": f"tests still failing after {C.MAX_TEST_FIXES} "
                f"attempts (batch {b['n']})",
                "journal": [f"impl b{b['n']}: {detail}, giving up"],
            }
        return {
            **baseline_delta,
            "test_fix_attempt": attempt + 1,
            "journal": [f"impl b{b['n']}: {detail}, retry {attempt + 1}"],
        }

    # Stage so the reviewer's `git diff HEAD` sees this batch, new files included.
    _stage_all()
    suffix = "" if db_ok else " (DB-gated tests skipped)"
    test_note = f", {summary}, 0 new failures" if db_ok and not C.DRY_RUN else suffix
    delta = {
        **baseline_delta,
        "test_fix_attempt": 0,
        "fix_cycle": 0,
        "db_degraded": not db_ok,
        "journal": [f"impl b{b['n']}: done{test_note}"],
    }
    if not db_ok:
        delta["degradations"] = ["e2e DB unreachable — DB-backed tests were skipped, not passed"]
    return delta


def close_batch(state):
    tid = state["task_id"]
    batches = [dict(b) for b in state["batches"]]
    idx = state["batch_idx"]
    b = batches[idx]
    b["status"] = "DONE"
    b["outcome"] = f"checklist met; review {state.get('code_verdict', '')}"

    impl_log = sorted(C.RAW.glob(f"{tid}-impl-b{b['n']}-*.log"))
    if impl_log:
        text = impl_log[-1].read_text()
        marker = "DEVIATIONS"
        b["deviations"] = (
            (text.split(marker, 1)[1].strip().splitlines() or ["none"])[0][:200]
            if marker in text
            else "none"
        )
    else:
        b["deviations"] = b["deviations"] or "none"

    _write_progress(tid, batches)
    subprocess.run(["git", "add", "-A"], cwd=C.REPO, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", f"task-{tid}: batch {b['n']} — {b['scope']}"],
        cwd=C.REPO,
        capture_output=True,
    )
    ev.emit(
        "batch_done",
        tid,
        "close_batch",
        f"batch {b['n']}/{len(batches)} committed — next: "
        f"{'batch ' + str(b['n'] + 1) if idx + 1 < len(batches) else 'final gate'}",
    )
    return {
        "batches": batches,
        "batch_idx": idx + 1,
        "fix_cycle": 0,
        "tests_waived": False,
        "baseline_batch_n": 0,
        "batch_test_baseline": [],
        "journal": [f"batch {b['n']}: committed"],
    }
