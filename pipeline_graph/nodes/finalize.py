"""Steps 3-4, 7-8: summary, judge, checkpoint, queue_scope, final_check, wrap_up."""

from __future__ import annotations

import json
import os
import re

from langgraph.types import interrupt

import pipeline_graph.nodes as _N

from .. import config as C
from .. import events as ev
from .. import test_runner as tr
from ..agents import MIN_OUTPUT_BYTES, classify_output, parse_not_met
from ..state import Conversation
from .common import (
    _db_note,
    _extract_json,
    _file_or_stdout,
    _recover_artifact,
    _save,
    _strip_batches_block,
    _trust_output,
    _write_progress,
    validate_batches_schema,
)


_HAS_UI_ONLY_RE = re.compile(
    r"^\s*HAS_UI\s*:\s*(YES|NO)\s*$", re.IGNORECASE | re.DOTALL
)


def _is_has_ui_trailer_only(out: str) -> bool:
    """True when stdout is only the judge.md trailing ``HAS_UI: YES|NO`` line.

    Tool-using judges often Write FINAL/BATCHES to disk and print only that
    trailer; treating it as near-empty must not clobber or discard on-disk
    artifacts (TASK-027: 12-byte stdout, valid files already present).
    """
    return bool(_HAS_UI_ONLY_RE.match((out or "").strip()))


def _is_noop_judge_escalate(reason: str) -> bool:
    """True when the judge wrote ``ESCALATE:`` but meant "I am not escalating".

    Exact denylist plus prefix forms like ``none — both contested items…``
    (TASK-022: first-line ``ESCALATE: none — …`` was treated as a real
    escalation because only the bare token ``none`` was ignored). Bare
    ``no <arbitrary>`` is NOT a noop — that can be a real reason
    ("no way to verify without a product call").
    """
    r = (reason or "").strip().lower()
    if not r or r in ("none", "no", "n/a", "na", "nothing", "ok", "false"):
        return True
    if re.match(r"^(none|n/a|na|nothing)\b", r):
        return True
    if re.match(r"^no(\s+issues?|\s+escalation|\s+problems?)\b", r):
        return True
    return False

# --- Step 3 ---------------------------------------------------------------


def summary(state):
    tid = state["task_id"]
    conv = Conversation.from_state(state)
    code, out = _N.run_agent("SUMMARIZER", conv, "summary", template="summary")
    health, _signal = classify_output(code, out)
    if not _trust_output(code, out, health):
        return {
            "escalation": f"summary produced untrustworthy output — refusing to write it (see journal for diagnostics)",
            "journal": [
                f"summary: UNTRUSTWORTHY output — health={health}, exit={code}, "
                f"{len(out)} bytes"
            ],
        }
    summary_path = C.DEBATES / f"SUMMARY-{tid}.md"
    _file_or_stdout(summary_path, out)
    if not summary_path.exists():
        _recover_artifact(tid, f"SUMMARY-{tid}.md", summary_path)
    return {"journal": ["summary: written"]}


# --- Step 4 ---------------------------------------------------------------


def judge(state):
    tid = state["task_id"]
    conv = Conversation.from_state(state)
    code, out = _N.run_agent("JUDGE", conv, "verdict", template="judge", docs_dir=C.DOCS_REL)

    # The judge may print "ESCALATE: <reason>" to signal a problem. Match it
    # only on the first non-blank line (a real escalation is at the top of the
    # output, not buried in prose). Ignore empty/none/"no issues" reasons —
    # the judge sometimes writes "ESCALATE: none — …" as a self-check, which
    # is NOT an escalation (see ``_is_noop_judge_escalate``).
    first_line = next((ln for ln in out.splitlines() if ln.strip()), "")
    if first_line.strip().startswith("ESCALATE:"):
        reason = first_line.split("ESCALATE:", 1)[1].strip()
        if not _is_noop_judge_escalate(reason):
            return {"escalation": f"judge escalated: {reason}", "journal": ["judge: escalated"]}

    health, _signal = classify_output(code, out)
    stdout_ok = _trust_output(code, out, health)

    batches_file = C.FINAL / f"BATCHES-{tid}.json"
    final_path = C.FINAL / f"FINAL-{tid}.md"

    # F2: file-primary. If a valid BATCHES file already exists, use it and do
    # NOT overwrite it with stdout — a decoy stdout (prose false positive) must
    # never clobber a real file. Only extract from stdout when no file exists.
    if not batches_file.exists() and stdout_ok:
        batches_json = _extract_json(out)
        if batches_json:
            _save(batches_file, json.dumps(batches_json, indent=2))

    # Save FINAL from stdout only when it looks like a real report. A bare
    # ``HAS_UI: YES`` trailer (or other near-empty stdout) must not clobber a
    # FINAL the judge already Wrote via tools.
    if (
        stdout_ok
        and out.strip()
        and not _is_has_ui_trailer_only(out)
    ):
        report_text = _strip_batches_block(out, None)
        _save(final_path, report_text)
    if not final_path.exists():
        _recover_artifact(tid, f"FINAL-{tid}.md", final_path)
    if not batches_file.exists():
        _recover_artifact(tid, f"BATCHES-{tid}.json", batches_file)
        return {
            "escalation": "judge did not produce BATCHES json",
            "journal": ["judge: no batch file"],
        }

    # F2: file-primary load with unlink-on-corrupt. A corrupt or invalid
    # BATCHES file is unlinked BEFORE escalating so a retry_judge re-entry
    # does not re-read the same corrupt file and trap permanently.
    try:
        raw = json.loads(batches_file.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        batches_file.unlink(missing_ok=True)
        return {
            "escalation": f"BATCHES json invalid: {exc}",
            "journal": ["judge: bad batch json — file unlinked before retry"],
        }

    batches, schema_err = validate_batches_schema(raw)
    if batches is None:
        batches_file.unlink(missing_ok=True)
        shape_note = ""
        if isinstance(raw, list):
            shape_note = (
                f" (type={type(raw).__name__}, len={len(raw)})"
            )
        else:
            shape_note = f" (type={type(raw).__name__})"
        return {
            "escalation": schema_err,
            "journal": [
                f"judge: BATCHES json rejected — {schema_err}{shape_note} — file unlinked before retry"
            ],
        }

    # Stdout may be untrusted (near-empty / HAS_UI-only) while the judge still
    # Wrote valid BATCHES + FINAL via tools. Prefer on-disk artifacts when they
    # clear schema + a non-trivial FINAL; otherwise keep the hard escalate.
    if not stdout_ok:
        final_ok = (
            final_path.exists()
            and final_path.stat().st_size >= MIN_OUTPUT_BYTES
        )
        if not final_ok:
            return {
                "escalation": (
                    "judge produced untrustworthy output — refusing to parse it "
                    "(see journal for diagnostics)"
                ),
                "journal": [
                    f"judge: UNTRUSTWORTHY output — health={health}, exit={code}, "
                    f"{len(out)} bytes; no usable FINAL on disk"
                ],
            }
        journal = [
            f"judge: {len(batches)} batches "
            f"(rescued on-disk artifacts; stdout health={health}, "
            f"{len(out)} bytes)"
        ]
    else:
        journal = [f"judge: {len(batches)} batches"]

    # has_ui was decided at plan-time (before the debate) and is not the judge's
    # to reset; the UX critique already happened during the debate.
    _write_progress(tid, batches)
    return {"batches": batches, "batch_idx": 0, "retry_judge": False,
            "journal": journal}


def checkpoint_plan(state):
    """Human gate after the verdict (skipped only in AUTO).

    Effort choice and plan approval are different questions:
      - ``checkpoint_effort`` (post-plan): how hard should the council work?
      - ``checkpoint_plan`` (post-judge): may we implement these batches?

    A prior interactive effort pick must NOT suppress this gate — that was the
    018 failure mode where implement started without a human "plan approved?".
    ``--effort`` / ``effort_forced`` also leave this gate on (it is then the
    only post-verdict confirmation).
    """
    if state.get("auto"):
        return {"journal": ["checkpoint plan: auto, skipped"]}
    tid = state["task_id"]
    decision = interrupt(
        {
            "stage": "plan approved?",
            "task": tid,
            "reason": "review FINAL + batches, then approve to start implement",
            "batches": [f"{b['n']}: {b['scope']}" for b in state.get("batches", [])],
            "plan": str(C.PLANS / f"PLAN-{tid}.md"),
            "final": str(C.FINAL / f"FINAL-{tid}.md"),
            "answers": {
                "ok": "approve the plan and start implement",
                "yes": "same as ok",
                "approve": "same as ok",
            },
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


def _final_test_fix_loop(conv: "Conversation", db_ok: bool, baseline: set[str]) -> dict | None:
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
    tid = conv.task_id
    if C.DRY_RUN or not db_ok:
        return None

    _, all_failures, summary = tr.run_repo_tests(task_id=tid)
    failures = tr.new_failures_since_baseline(
        all_failures, baseline, [],
        lint_debt_rules=C.LINT_DEBT_RULES,
        ambient_patterns=C.TEST_AMBIENT_PATTERNS,
    )
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
        _, out = _N.run_agent(
            "IMPLEMENTER",
            conv,
            f"final-fix-{attempt}",
            template="preflight_fix",
            timeout=FINAL_FIX_TIMEOUT,
            failures=fail_list,
            summary=summary,
            remaining_label=remaining_label,
        )
        _, all_failures, summary = tr.run_repo_tests(task_id=tid)
        failures = tr.new_failures_since_baseline(
            all_failures, baseline, [],
            lint_debt_rules=C.LINT_DEBT_RULES,
            ambient_patterns=C.TEST_AMBIENT_PATTERNS,
        )
        if not failures:
            ev.emit(
                "step_end",
                tid,
                "final_check",
                f"auto-fix attempt {attempt}: no new failures ({summary})",
            )
            _N._stage_all()
            if not C.DRY_RUN and not C.NO_GIT:
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
    conv = Conversation.from_state(state)

    # Test gate: run the full suite and auto-fix any failures before the
    # LLM checklist review.  This catches both pre-existing baseline
    # failures that passed through the per-batch gate, and any regressions
    # that a degraded DB let through.
    test_result = (
        None
        if state.get("final_tests_waived")
        else _final_test_fix_loop(conv, db_ok, set(state.get("task_baseline") or []))
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

    code, out = _N.run_agent(
        "IMPLEMENTER",
        conv,
        "final-check",
        template="final_check",
        db_note=db_note,
    )
    health, _signal = classify_output(code, out)
    if not _trust_output(code, out, health):
        return {
            "escalation": f"final check produced untrustworthy output — refusing to parse it (see journal for diagnostics)",
            "journal": [
                f"final check: UNTRUSTWORTHY output — health={health}, exit={code}, "
                f"{len(out)} bytes"
            ],
        }
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
    C.ensure_dirs()
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
    C.sync_back_docs()
    return {"finished": True, "journal": ["wrap up: report written"]}
