"""Step 6: implement node, test-fix loop, and close_batch."""

from __future__ import annotations

import subprocess

import pipeline_graph.nodes as _N

from .. import config as C
from .. import events as ev
from .. import test_runner as tr
from ..state import Conversation
from .common import (
    _current_batch,
    _db_note,
    _dirty_paths,
    _git,
    _stage_all,
    _write_progress,
    branch_mismatch_reason,
    git_identity,
    parse_deviations_line,
)


def _capture_test_baseline(state, b: dict, db_ok: bool) -> dict:
    """Snapshot failing tests before the implementer edits (once per batch)."""
    if state.get("baseline_batch_n") == b["n"] or not db_ok or C.DRY_RUN:
        return {}
    _, failures, summary = tr.run_repo_tests(task_id=state["task_id"])
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
        delta["degradations"] = [
            f"branch started RED: {len(failures)} test(s) already failing "
            "before any batch — tolerated as baseline"
        ]
    return delta


def _in_graph_test_gate(
    state, b: dict, db_ok: bool,
) -> tuple[bool, list[str], str, int]:
    """Run the gate; return ``(ok, new_fails, summary, ran_count)``.

    ``ran_count`` is the number of suites that actually executed a runner —
    0 on the waived/dry-run/DB-down paths so the caller can distinguish
    "gate measured green" from "gate measured nothing". The 4th element is
    threaded up to ``implement`` so the green-path delta can set
    ``last_gate_status`` correctly (``green`` when measured, ``skipped``
    when not).
    """
    if state.get("tests_waived"):
        return True, [], "tests waived", 0
    if not db_ok or C.DRY_RUN:
        return True, [], "test gate skipped (db down or dry run)", 0

    _, current, summary, ran_count = tr.run_repo_tests_detailed(task_id=state["task_id"])
    baseline = set(state.get("batch_test_baseline") or [])
    allow = b.get("test_failure_allowlist") or []
    new = sorted(tr.new_failures_since_baseline(
        current, baseline, allow,
        lint_debt_rules=C.LINT_DEBT_RULES,
        ambient_patterns=C.TEST_AMBIENT_PATTERNS,
    ))
    return len(new) == 0, new, summary, ran_count


def implement(state):
    tid = state["task_id"]
    b = _current_batch(state)
    if state.get("tests_waived"):
        return {
            "test_fix_attempt": 0,
            "fix_cycle": 0,
            "test_fix_failures": [],
            "test_fix_summary": "",
            "last_gate_status": "skipped",
            "last_gate_summary": "",
            "last_gate_failures": [],
            "test_fix_measured": False,
            "journal": [f"impl b{b['n']}: skipped (tests waived)"],
        }

    db_ok, db_note = _db_note(tid, "implement")
    attempt = state.get("test_fix_attempt", 0)
    baseline_delta = _capture_test_baseline(state, b, db_ok)
    # Record the batch's base commit (HEAD before any edits) so code_review diffs
    # against it, not HEAD — the batch's work then shows whether the implementer
    # left it staged OR committed it (the false-REJECT that hit TASK-010 b1/b5).
    if attempt == 0 and not C.DRY_RUN:
        mismatch = branch_mismatch_reason(state.get("branch"))
        if mismatch:
            return {
                **baseline_delta,
                "escalation": mismatch,
                "journal": [f"impl b{b['n']}: refused — git branch mismatch"],
            }
        # Scope guard: the tree must be clean before the implementer edits, so
        # close_batch's `git add -A` commits ONLY this batch's work. Any leftover
        # (a crashed run, a smoke sharing the working copy) gets its own clearly
        # labelled commit instead of being absorbed into — and mislabelled as —
        # this batch. This is the fix for the "task-smoke: batch 1" mix-up.
        if not C.NO_GIT and _dirty_paths():
            _git("add", "-A")
            _git("commit", "-m",
                 f"WIP: uncommitted leftovers before task-{tid} batch {b['n']}")
        baseline_delta = {**baseline_delta, "batch_base_ref": _git("rev-parse", "HEAD").strip()}
    merged = {**state, **baseline_delta}

    conv = Conversation.from_state(state)
    step = f"impl-b{b['n']}"
    if attempt:
        step += f"-fix{attempt}"
    # F2: thread the previous attempt's failing tests + summary into the retry
    # prompt so the implementer fixes them instead of re-running blind. Empty
    # on the first attempt (state fields default to [] / "").
    failures = state.get("test_fix_failures", [])
    summary = state.get("test_fix_summary", "")
    # Build the <test_summary> block for the prompt. Three regimes:
    #   * attempt == 0: no prior gate result — non-authoritative "unconfigured"
    #     sentinel so the implementer knows the gate has not measured yet.
    #   * retry with test_fix_measured=True: the prior attempt's failures were
    #     MEASURED by a real gate run — authoritative "red" block sourced from
    #     test_fix_failures so the implementer fixes the right tests.
    #   * retry with test_fix_measured=False: the prior attempt's failures were
    #     NOT measured (dry-run/DB-down/waived) — non-authoritative "skipped"
    #     sentinel so the implementer does not treat stale failures as ground
    #     truth.
    suite_labels = [s.label for s in C.TEST_SUITES]
    if attempt == 0:
        test_summary_block = tr.format_test_summary_block(
            "unconfigured", suite_labels, [], "",
            authoritative=False,
        )
    elif state.get("test_fix_measured"):
        test_summary_block = tr.format_test_summary_block(
            "red", suite_labels, failures, summary,
            authoritative=True,
        )
    else:
        test_summary_block = tr.format_test_summary_block(
            "skipped", suite_labels, [], summary,
            authoritative=False,
        )
    code, out = _N.run_agent(
        "IMPLEMENTER",
        conv,
        step,
        template="implement",
        batch_n=b["n"],
        batch_scope=b["scope"],
        db_note=db_note,
        arch_docs=C.arch_docs_block(),
        checklist_items=", ".join(map(str, b.get("checklist", []))),
        failures=failures,
        summary=summary,
        test_summary=test_summary_block,
    )

    # F1: explicit PLAN_DISCREPANCY: marker contract — the implementer emits a
    # line whose trimmed text starts with `PLAN_DISCREPANCY:` only on a genuine
    # plan/code contradiction. The old `"discrepancy"+"plan"` substring pair
    # false-positived on prose mentioning both words.
    # No-op bodies ("none", "n/a", empty, …) must NOT escalate — agents still
    # emit `PLAN_DISCREPANCY: none` despite the prompt saying not to.
    _noop_bodies = frozenset({
        "", "none", "n/a", "na", "no", "false", "ok", "-", "—", ".",
        "no discrepancy", "none.", "n/a.",
    })

    def _is_real_discrepancy(line: str) -> bool:
        s = line.strip()
        if not s.startswith("PLAN_DISCREPANCY:"):
            return False
        body = s[len("PLAN_DISCREPANCY:"):].strip().lower()
        return body not in _noop_bodies

    matched_line = next(
        (ln for ln in out.splitlines() if _is_real_discrepancy(ln)),
        None,
    )
    if matched_line is not None:
        return {
            **baseline_delta,
            "escalation": f"implementer reported a plan/code discrepancy (batch {b['n']}): {matched_line}",
            "journal": [f"impl b{b['n']}: PLAN_DISCREPANCY"],
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

    ok, new_fails, summary, ran_count = _in_graph_test_gate(merged, b, db_ok)
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
            "test_fix_failures": new_fails,
            "test_fix_summary": summary,
            "test_fix_measured": ran_count > 0,
            "journal": [f"impl b{b['n']}: {detail}, retry {attempt + 1}"],
        }

    # Stage so the reviewer's `git diff HEAD` sees this batch, new files included.
    _stage_all()
    suffix = "" if db_ok else " (DB-gated tests skipped)"
    test_note = f", {summary}, 0 new failures" if db_ok and not C.DRY_RUN else suffix
    # Green-path gate-state threading: when the gate actually measured
    # (ran_count > 0), record the green outcome + summary so code_fix can
    # build an authoritative test_summary block. When the gate was
    # waived/dry-run/DB-down (ran_count == 0), record a skipped sentinel
    # with an empty summary so code_fix does not treat a non-measurement as
    # a green signal.
    if ran_count > 0:
        last_gate_status = "green"
        last_gate_summary = summary
        last_gate_failures: list[str] = []
    else:
        last_gate_status = "skipped"
        last_gate_summary = ""
        last_gate_failures = []
    delta = {
        **baseline_delta,
        "test_fix_attempt": 0,
        "fix_cycle": 0,
        "test_fix_failures": [],
        "test_fix_summary": "",
        "test_fix_measured": False,
        "last_gate_status": last_gate_status,
        "last_gate_summary": last_gate_summary,
        "last_gate_failures": last_gate_failures,
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
        dev = parse_deviations_line(text)
        b["deviations"] = dev[:200] if dev else "none"
    else:
        b["deviations"] = b["deviations"] or "none"

    mismatch = branch_mismatch_reason(state.get("branch"))
    if mismatch:
        # Do not mark the batch DONE on disk / progress until commit lands on
        # the expected branch — leave progress write for the successful path.
        return {
            "escalation": mismatch,
            "journal": [f"batch {b['n']}: refused commit — git branch mismatch"],
        }

    _write_progress(tid, batches)
    sha = ""
    if not C.DRY_RUN and not C.NO_GIT:
        subprocess.run(["git", "add", "-A"], cwd=C.REPO, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"task-{tid}: batch {b['n']} — {b['scope']}"],
            cwd=C.REPO,
            capture_output=True,
        )
        sha = _git("rev-parse", "HEAD")
    ident = git_identity()
    # Prefer post-commit sha; fall back to identity helper (DRY_RUN → empty).
    if sha:
        ident = {**ident, "sha": sha}
    short = (ident["sha"][:12] if ident.get("sha") else "dry")
    branch = ident.get("branch") or state.get("branch") or "?"
    repo = ident.get("repo") or str(C.REPO)
    next_bit = (
        f"batch {b['n'] + 1}" if idx + 1 < len(batches) else "final gate"
    )
    where = f"{short} on {branch} in {repo}"
    ev.emit(
        "batch_done",
        tid,
        "close_batch",
        f"batch {b['n']}/{len(batches)} committed — {where} — next: {next_bit}",
        repo=repo,
        branch=branch,
        sha=ident.get("sha") or "",
        state_branch=state.get("branch") or "",
    )
    return {
        "batches": batches,
        "batch_idx": idx + 1,
        "fix_cycle": 0,
        "tests_waived": False,
        "test_fix_failures": [],
        "test_fix_summary": "",
        "test_fix_measured": False,
        "last_gate_summary": "",
        "last_gate_failures": [],
        "last_gate_status": "",
        "baseline_batch_n": 0,
        "batch_test_baseline": [],
        "journal": [f"batch {b['n']}: committed — {where}"],
    }
