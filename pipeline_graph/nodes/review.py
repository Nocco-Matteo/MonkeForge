"""Step 6: code review, fix, and verify nodes."""

from __future__ import annotations

from .. import config as C
from .. import test_runner as tr
from ..agents import (
    classify_output,
    count_blockers,
    parse_disputed,
    parse_not_met,
    parse_verdict,
    read_if_exists,
    run_agent,
)
from ..state import Conversation
from .common import _current_batch, _file_or_stdout, _git, _recover_artifact, _stage_all, _trust_output, parse_verify_statuses


def code_review(state):
    tid = state["task_id"]
    b = _current_batch(state)
    base = state.get("batch_base_ref") or "HEAD"
    conv = Conversation.from_state(state)
    code, out = run_agent(
        "CODE_REVIEWER",
        conv,
        f"cr-b{b['n']}",
        template="code_review",
        batch_n=b["n"],
        batch_scope=b["scope"],
        diff_base=base,
        checklist_items=", ".join(map(str, b.get("checklist", []))),
        trusted_context=state.get("trusted_context", ""),
        arch_docs=C.arch_docs_block(),
    )
    health, _signal = classify_output(code, out)
    if not _trust_output(code, out, health):
        return {
            "escalation": f"code review for batch {b['n']} produced untrustworthy output — refusing to act on it (see journal for diagnostics)",
            "journal": [
                f"cr b{b['n']}: UNTRUSTWORTHY output — health={health}, exit={code}, "
                f"{len(out)} bytes"
            ],
        }
    review_path = C.REVIEWS / f"CODE-{tid}-b{b['n']}.md"
    review = _file_or_stdout(review_path, out)
    if not review.strip():
        _recover_artifact(tid, f"CODE-{tid}-b{b['n']}.md", review_path)
        review = read_if_exists(review_path)
    verdict, not_met = parse_verdict(review), parse_not_met(review)
    blockers = count_blockers(review)

    # Sanity guard against a review that read the wrong diff (the failure that
    # cost task-007 a whole fix cycle: reviewer on `main...HEAD` saw an empty
    # diff and marked ~everything NOT MET). #1 fixed the base; this catches a
    # regression cheaply. If the batch has a real staged diff but the reviewer
    # rejected almost every item, that is a tooling problem, not code to fix —
    # escalate with the hypothesis instead of running fix cycles at it.
    n_items = len(b.get("checklist", []))
    changed = [] if C.DRY_RUN else _git("diff", "--name-only", base).splitlines()
    changed = [p for p in changed if p.strip()]
    review_l = (review or "").lower()
    empty_review = (
        not changed
        and (
            verdict in ("UNKNOWN", "")
            or "not applicable" in review_l
            or "empty diff" in review_l
        )
    )
    if empty_review and not C.DRY_RUN:
        return {
            "code_verdict": verdict or "UNKNOWN",
            "not_met": not_met,
            "open_blockers": max(blockers, 1),
            "fix_cycle": 0,
            "escalation": (
                f"code review for batch {b['n']}: empty diff vs "
                f"{base[:12] if len(base) > 12 else base} — refusing to "
                f"close/approve (implement wrote nothing in PIPELINE_REPO, "
                f"or reviewer saw no changes)"
            ),
            "journal": [
                f"cr b{b['n']}: EMPTY_BATCH_DIFF (verdict={verdict or 'UNKNOWN'})"
            ],
        }
    if n_items >= 3 and len(not_met) >= n_items - 1 and changed:
        return {
            "code_verdict": verdict,
            "not_met": not_met,
            "open_blockers": blockers,
            "fix_cycle": 0,
            "escalation": f"code review marked {len(not_met)}/{n_items} items NOT "
            f"MET, but batch {b['n']} has a real staged diff ({len(changed)} "
            "files). Likely the reviewer read the wrong diff, not a genuine "
            "failure — check CODE-{tid}-b{n}.md before running fix cycles.".replace(
                "{tid}", tid
            ).replace("{n}", str(b["n"])),
            "journal": [
                f"cr b{b['n']}: {len(not_met)}/{n_items} NOT MET vs "
                f"{len(changed)}-file diff — suspected review mismatch"
            ],
        }

    return {
        "code_verdict": verdict,
        "not_met": not_met,
        "open_blockers": blockers,
        "fix_cycle": 0,
        "journal": [f"cr b{b['n']}: {verdict}, {blockers} blockers, {len(not_met)} not met"],
    }


def code_fix(state):
    tid = state["task_id"]
    b = _current_batch(state)
    cycle = state.get("fix_cycle", 0) + 1
    conv = Conversation.from_state(state)
    # Build a conditional <test_summary> block from the last in-graph gate
    # outcome. Non-empty ONLY when the gate measured green (last_gate_status
    # == "green") AND the summary is a real measurement (non-empty and not a
    # skip-sentinel string like "tests waived" / "test gate skipped"). On a
    # skipped/empty/skip-sentinel state, pass an empty string so the prompt
    # does not treat a non-measurement as a green signal.
    last_status = state.get("last_gate_status", "")
    last_summary = state.get("last_gate_summary", "")
    last_failures = state.get("last_gate_failures", []) or []
    _SKIP_SENTINELS = ("tests waived", "test gate skipped", "skipped")
    is_skip_sentinel = (
        not last_summary
        or any(last_summary.startswith(s) for s in _SKIP_SENTINELS)
    )
    if last_status == "green" and not is_skip_sentinel:
        suite_labels = [s.label for s in C.TEST_SUITES]
        test_summary_block = tr.format_test_summary_block(
            "green", suite_labels, last_failures, last_summary,
            authoritative=True,
        )
    else:
        test_summary_block = ""
    _, out = run_agent(
        "IMPLEMENTER",
        conv,
        f"cr-b{b['n']}-fix{cycle}",
        template="code_fix",
        batch_n=b["n"],
        test_summary=test_summary_block,
    )
    disputed = parse_disputed(out)
    if disputed:
        return {
            "fix_cycle": cycle,
            "disputed": disputed,
            "escalation": f"implementer disputed items in batch {b['n']}: {disputed}",
            "journal": [f"cr b{b['n']} fix{cycle}: DISPUTED"],
        }
    # Re-stage: the fix added more working-tree changes for code_verify to see.
    _stage_all()
    return {"fix_cycle": cycle, "journal": [f"cr b{b['n']} fix{cycle}: applied"]}


def code_verify(state):
    tid = state["task_id"]
    b = _current_batch(state)
    cycle = state.get("fix_cycle", 1)
    conv = Conversation.from_state(state)
    code, out = run_agent(
        "CODE_REVIEWER",
        conv,
        f"cr-b{b['n']}-verify{cycle}",
        template="code_verify",
        batch_n=b["n"],
    )
    health, _signal = classify_output(code, out)
    if not _trust_output(code, out, health):
        return {
            "escalation": f"code verify for batch {b['n']} produced untrustworthy output — refusing to act on it (see journal for diagnostics)",
            "journal": [
                f"cr b{b['n']} verify{cycle}: UNTRUSTWORTHY output — health={health}, "
                f"exit={code}, {len(out)} bytes"
            ],
        }
    if "NOT_FIXED" in parse_verify_statuses(out):
        if cycle >= C.resolved_fix_cycles(state):
            return {
                "escalation": f"batch {b['n']}: blockers still unfixed after "
                f"{C.resolved_fix_cycles(state)} cycles",
                "journal": [f"cr b{b['n']} verify{cycle}: NOT_FIXED, giving up"],
            }
        return {"journal": [f"cr b{b['n']} verify{cycle}: NOT_FIXED, another cycle"]}
    return {
        "not_met": [],
        "open_blockers": 0,
        "journal": [f"cr b{b['n']} verify{cycle}: confirmed"],
    }
