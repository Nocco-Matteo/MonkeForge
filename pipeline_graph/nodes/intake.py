"""Step 0 + 0b: init and intake interview nodes."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from langgraph.types import interrupt

from .. import config as C
from .. import events as ev
from ..agents import run_agent
from ..intake_materialize import (
    is_contract_brief,
    materialize_intake_output,
    missing_contract_sections,
)
from ..requirements_gap import (
    clear_requirements_gap,
    gap_block_for_prompt,
    gap_status,
    n_active_gaps,
    waive_requirements_gap,
)
from ..state import Conversation
from .common import _dirty_blocks_interactive_init, _dirty_paths, _git, _rel


def _gap_block(task_id: str) -> str:
    return gap_block_for_prompt(task_id)

def brief_file(task_id: str) -> Path:
    return C.TASKS / f"TASK-{task_id}-brief.md"


def intake_file(task_id: str) -> Path:
    return C.TASKS / f"TASK-{task_id}-intake.md"


def intake_history_file(task_id: str) -> Path:
    """TASK-033: append-only archive of prior re-intake cycles' transcripts.

    ``archive_intake_for_reintake`` appends the live intake body here then
    deletes the live file, so I3 sees only the fresh file's answers (no false
    pass from a prior cycle's ``**A:**`` markers).
    """
    return C.TASKS / f"TASK-{task_id}-intake-history.md"


def refs_dir(task_id: str) -> Path:
    return C.TASKS / f"TASK-{task_id}-refs"


ANSWER_RE = re.compile(r"^\s*\*\*A:\*\*\s*(.+?)\s*$", re.MULTILINE)


def intake_answers(task_id: str) -> list[str]:
    """Non-empty answers currently written in the intake file."""
    path = intake_file(task_id)
    if not path.exists():
        return []
    return [a for a in ANSWER_RE.findall(path.read_text()) if a.strip()]


# --- TASK-033: I3 (REQUIREMENTS gap answer) gate helpers --------------------


def _i3_gap_answered(task_id: str) -> bool:
    """True iff every active REQUIREMENTS gap has a matching ``**A:**`` line.

    The I3 gate compares the count of ``## Gap N`` entries in the gap file
    against the count of ``**A:**`` answer markers in the *live* intake file
    (the history archive is excluded — only this cycle's answers count). A
    suspended/waived gap (``n_active_gaps`` returns 0) passes trivially.
    """
    n_gaps = n_active_gaps(task_id)
    if n_gaps <= 0:
        return True
    n_answers = len(intake_answers(task_id))
    return n_answers >= n_gaps


def _i3_rejection_message(task_id: str) -> str:
    """The escalation reason when I3 fails (active gap, not enough answers)."""
    n_gaps = n_active_gaps(task_id)
    n_answers = len(intake_answers(task_id))
    return (
        f"REQUIREMENTS gap re-intake not complete: {n_gaps} active gap(s) but "
        f"only {n_answers} answer(s) in the intake file — answer each gap "
        f"with a **A:** line, then resume (or 'skip' to waive the gaps and "
        f"ship with a degradation)"
    )


def _i3_gap_active(task_id: str) -> bool:
    """True iff a REQUIREMENTS gap is active (so I3 / the active-gap split apply)."""
    return gap_status(task_id) == "active"


def _seed_brief(task_id: str, request: str) -> Path:
    """Materialise a brief when no interview produced one.

    Everything downstream reads one path, whether or not an interview happened,
    so this must never leave that path missing. When the interview was cut short
    after the human had already answered, those answers are carried into the
    brief verbatim: seeding from the bare request would silently throw away the
    only part of the interview that had value.
    """
    path = brief_file(task_id)
    if path.exists():
        return path
    answered = intake_answers(task_id)
    parts = [f"# TASK-{task_id} — brief", ""]
    if answered:
        parts += [
            "The interview was ended before the interviewer wrote a brief. What "
            "follows is the request as submitted, then the interview transcript "
            "as it stood. Both are authoritative; where they disagree, the "
            "transcript is later and wins.",
            "",
            "## Request as submitted",
            "",
            request,
            "",
            "## Interview transcript",
            "",
            intake_file(task_id).read_text().strip(),
            "",
        ]
    else:
        parts += ["Not interviewed: this is the request as submitted.", "", request, ""]
    path.write_text("\n".join(parts))
    return path


def intake_enabled(state) -> bool:
    """Interview by default; --auto skips it unless --interview forces it back on."""
    return bool(state.get("interview")) or not state.get("auto")


def _init_intake(state) -> dict:
    """Intake fields for init's delta, seeding the brief when no interview runs."""
    tid = state["task_id"]
    if intake_enabled(state):
        return {"intake_round": 0, "intake_done": False, "brief_path": str(brief_file(tid))}
    path = _seed_brief(tid, state.get("request", ""))
    return {"intake_round": 0, "intake_done": True, "brief_path": str(path)}


def init(state):
    task_id = state["task_id"]
    C.ensure_dirs()
    branch = f"{C.BRANCH_PREFIX}{task_id}"

    if C.DRY_RUN:
        # A dry run exercises the graph, never the repository. This used to
        # commit the working tree and switch branch for real, so testing the
        # graph with a throwaway task id left a "WIP: pre-task-999" commit on
        # whatever branch you happened to be on.
        current = _git("rev-parse", "--abbrev-ref", "HEAD")
        return {
            "branch": current,
            "debate_round": 0,
            "batch_idx": 0,
            "fix_cycle": 0,
            "test_fix_attempt": 0,
            "tests_waived": False,
            "baseline_batch_n": 0,
            "batch_test_baseline": [],
            "ux_render_cycle": 0,
            "visual_blockers": 0,
            "visual_shipped_blocked": False,
            "prev_visual_blockers": None,
            "visual_no_progress": 0,
            "escalation": "",
            "degradations": [],
            **_init_intake(state),
            "journal": [f"init: DRY RUN, git untouched (still on {current})"],
        }

    dirty_paths = _dirty_paths()
    if dirty_paths and _dirty_blocks_interactive_init(dirty_paths) and not state.get("auto"):
        blocking = [
            p for p in dirty_paths if not any(p.startswith(x) for x in C.INIT_DIRTY_OK_PREFIXES)
        ]
        hint = blocking[0] if blocking else dirty_paths[0]
        return {
            "escalation": "working tree is not clean; commit or stash first "
            "(docs/tasks, docs/metrics, docs/prompts, docs/queue "
            "may stay dirty in interactive mode)",
            "journal": [f"init: dirty tree ({hint}), escalating"],
        }
    if dirty_paths and not C.DRY_RUN and not C.NO_GIT:
        # Commit on the task branch, not on whatever branch is checked out now:
        # the WIP snapshot belongs to the task that caused it.
        subprocess.run(["git", "checkout", "-B", branch], cwd=C.REPO, capture_output=True)
        _git("add", "-A")
        _git("commit", "-m", f"WIP: pre-task-{task_id} working tree")
    elif not C.DRY_RUN and not C.NO_GIT and _git("rev-parse", "--abbrev-ref", "HEAD") != branch:
        subprocess.run(["git", "checkout", "-B", branch], cwd=C.REPO, capture_output=True)

    return {
        "branch": branch,
        "debate_round": 0,
        "batch_idx": 0,
        "fix_cycle": 0,
        "test_fix_attempt": 0,
        "tests_waived": False,
        "baseline_batch_n": 0,
        "batch_test_baseline": [],
        "ux_render_cycle": 0,
        "visual_blockers": 0,
        "visual_shipped_blocked": False,
        "prev_visual_blockers": None,
        "visual_no_progress": 0,
        "escalation": "",
        "degradations": [],
        **_init_intake(state),
        "journal": [f"init: on {branch}"],
    }


# --- Step 0b: intake interview ---------------------------------------------
# Split in two nodes on purpose. LangGraph re-executes an interrupted node from
# the top on resume, so an agent call and an interrupt() cannot share a node:
# every resume would re-run the interviewer before it could read the answers.
# `intake_ask` calls the agent and never interrupts; `intake_wait` only
# interrupts and is therefore safe to replay. Same seam as `escalate`.


def intake_ask(state):
    tid = state["task_id"]
    rnd = state.get("intake_round", 0) + 1
    refs = refs_dir(tid)
    ref_list = ", ".join(sorted(p.name for p in refs.iterdir())) if refs.is_dir() else "none"

    # A brief or intake file left behind by an earlier run of the same task id
    # must not be mistaken for this round's output. Only a file actually written
    # during this round counts.
    before = brief_file(tid).stat().st_mtime if brief_file(tid).exists() else None
    intake_before = intake_file(tid).stat().st_mtime if intake_file(tid).exists() else None

    conv = Conversation.from_state(state)
    code, out = run_agent(
        "INTERVIEWER",
        conv,
        f"intake-r{rnd}",
        template="intake",
        round=rnd,
        max_rounds=C.MAX_INTAKE_ROUNDS,
        request=state.get("request", ""),
        brief_path=str(_rel(brief_file(tid))),
        intake_path=str(_rel(intake_file(tid))),
        refs_path=str(_rel(refs)),
        refs_list=ref_list,
        arch_docs=C.arch_docs_block(),
        requirements_gaps=_gap_block(tid),
    )
    if code != 0:
        return {
            "intake_round": rnd,
            "escalation": f"intake round {rnd} failed (exit {code})",
            "journal": [f"intake r{rnd}: agent failed"],
        }

    # Gemini prints to stdout only; GLM/Cursor may edit files directly — fill gaps.
    # materialize refuses non-contract COMPLETE bodies so a chat summary cannot
    # clobber a seed brief the agent already wrote (or left untouched).
    # Pass pre-agent mtimes so a tool-written intake is not appended again from
    # the same stdout (duplicate "## Round N" blocks).
    materialize_intake_output(
        tid,
        rnd,
        out,
        intake_path=intake_file(tid),
        brief_path=brief_file(tid),
        intake_mtime_before=intake_before,
        brief_mtime_before=before,
    )

    bf = brief_file(tid)
    now = bf.stat().st_mtime if bf.exists() else None
    fresh_brief = now is not None and now != before
    brief_text = bf.read_text() if bf.exists() else ""
    contract_ok = is_contract_brief(brief_text)

    if "INTAKE: COMPLETE" in out.upper():
        if not fresh_brief:
            stale = " (a brief exists but this round did not write it)" if bf.exists() else ""
            return {
                "intake_round": rnd,
                "escalation": f"interviewer reported COMPLETE but wrote no brief{stale}",
                "journal": [f"intake r{rnd}: no brief written"],
            }
        if not contract_ok:
            missing = ", ".join(missing_contract_sections(brief_text)) or "required sections"
            return {
                "intake_round": rnd,
                "escalation": (
                    "interviewer reported COMPLETE but the brief is not a contract "
                    f"brief (missing: {missing}) — rewrite "
                    f"{_rel(brief_file(tid))} with the intake (B) sections, then resume"
                ),
                "journal": [f"intake r{rnd}: brief failed contract check"],
            }
        has_brief = True
    elif "INTAKE: QUESTIONS" in out.upper():
        # It says it asked: the questions file must actually exist and have grown.
        if not intake_file(tid).exists():
            return {
                "intake_round": rnd,
                "escalation": "interviewer reported QUESTIONS but wrote no questions file",
                "journal": [f"intake r{rnd}: no questions written"],
            }
        has_brief = False
    elif fresh_brief:
        # Marker forgotten, but a file write happened — still require contract shape
        # so a tool Write of a status note cannot sneak past as intake_done.
        if not contract_ok:
            missing = ", ".join(missing_contract_sections(brief_text)) or "required sections"
            return {
                "intake_round": rnd,
                "escalation": (
                    "interviewer wrote a brief that is not a contract brief "
                    f"(missing: {missing}) — rewrite "
                    f"{_rel(brief_file(tid))} with the intake (B) sections, then resume"
                ),
                "journal": [f"intake r{rnd}: brief failed contract check"],
            }
        has_brief = True
    elif intake_file(tid).exists() and intake_file(tid).stat().st_mtime != intake_before:
        has_brief = False  # questions written this round, marker forgotten
    else:
        return {
            "intake_round": rnd,
            "escalation": "interviewer produced neither questions nor a brief",
            "journal": [f"intake r{rnd}: no output file"],
        }

    if has_brief:
        # TASK-033 (I3): if a REQUIREMENTS gap is active, the interviewer MUST
        # have answered each gap (one **A:** line per ``## Gap N``) before
        # COMPLETE is accepted. Without this gate a re-intake that wrote a
        # contract brief but skipped the gaps would silently pass and the run
        # would ship with the brief unchanged on the flagged holes.
        if _i3_gap_active(tid) and not _i3_gap_answered(tid):
            return {
                "intake_round": rnd,
                "escalation": _i3_rejection_message(tid),
                "journal": [f"intake r{rnd}: I3 gate failed — gaps unanswered"],
            }
        ev.emit(
            "intake_complete",
            tid,
            "intake_ask",
            f"brief written after {rnd} round(s): {_rel(brief_file(tid))}",
        )
        # Re-intake handoff consumed: drop the gap file so a later unrelated
        # redo does not re-ask stale debate claims.
        was_active = _i3_gap_active(tid)
        clear_requirements_gap(tid)
        # TASK-033: increment the re-intake counter only when a gap was active
        # (a first-run COMPLETE with no gap file does not count as a re-intake).
        delta = {
            "intake_round": rnd,
            "intake_done": True,
            "brief_path": str(brief_file(tid)),
            "journal": [f"intake r{rnd}: brief complete"],
        }
        if was_active:
            delta["requirements_reintake_count"] = (
                int(state.get("requirements_reintake_count", 0)) + 1
            )
        return delta

    if rnd >= C.MAX_INTAKE_ROUNDS:
        return {
            "intake_round": rnd,
            "escalation": f"intake still unresolved after {C.MAX_INTAKE_ROUNDS} "
            "rounds; answer 'skip' to plan from the brief as it "
            "stands, or stop and rewrite the request",
            "journal": [f"intake r{rnd}: round cap reached"],
        }

    ev.emit(
        "intake_questions",
        tid,
        "intake_ask",
        f"round {rnd}: questions waiting in "
        f"{_rel(intake_file(tid))} — fill in the A: lines, then "
        f"./run.py resume {tid} --answer ok",
        round=rnd,
    )
    return {
        "intake_round": rnd,
        "journal": [f"intake r{rnd}: questions written, waiting for answers"],
    }


INTAKE_END_ANSWERS = ("skip", "done", "stop", "enough", "no", "abort", "cancel")
INTAKE_SUBMIT_ANSWERS = ("ok", "yes", "submit", "continue", "proceed")


def intake_wait(state):
    """Pure human gate: no agent, so replaying it on resume costs nothing.

    TASK-033: when a REQUIREMENTS gap is active, the human-gate answers split:
    - ``ok``/``submit``/``continue``/``proceed``/``yes`` with answers on disk →
      run the I3 gate; pass → intake_done + count increment; fail → escalate
      (the operator answers the gaps or skips);
    - ``skip``/``done``/``enough`` → waive the gap + count increment +
      degradation + clear batches + debate_round=0 (route_escalation_return
      sends intake_done=True to debate_tech);
    - ``stop``/``no``/``abort``/``cancel`` → finish the run (gap stays active,
      count unchanged);
    - no active gap → today's semantics (end/submit/no-new-answers).
    """
    tid = state["task_id"]
    answered = len(intake_answers(tid))
    answer = interrupt(
        {
            "stage": "intake",
            "task": tid,
            "round": state.get("intake_round", 0),
            "reason": f"answer the questions in {_rel(intake_file(tid))}, then resume",
            "edit": str(intake_file(tid)),
            "answers_filled_in": answered,
            "hint": "resume with --answer ok when done, or --answer skip to stop "
            "interviewing and plan from what is there",
        }
    )
    ans = str(answer).strip().lower()
    active_gap = _i3_gap_active(tid)

    if active_gap and ans in ("stop", "no", "abort", "cancel"):
        # TASK-033: stop with an active gap finishes the run; the gap stays
        # active so a later CLI redo --from intake or fresh run still finds it.
        return {
            "finished": True,
            "journal": [
                f"intake: stopped by user ({answer}) — run finished, "
                "REQUIREMENTS gap stays active"
            ],
        }

    if active_gap and ans in ("skip", "done", "enough"):
        # TASK-033: waive the gap + count increment + degradation + clear
        # batches + debate_round=0. route_escalation_return sees intake_done
        # and routes to debate_tech (the plan regenerates from the waived
        # brief; the degradation is recorded).
        waive_requirements_gap(tid)
        path = _seed_brief(tid, state.get("request", ""))
        return {
            "intake_done": True,
            "intake_unanswered": False,
            "brief_path": str(path),
            "requirements_reintake_count": (
                int(state.get("requirements_reintake_count", 0)) + 1
            ),
            "degradations": [
                "shipped with REQUIREMENTS gaps waived at intake"
            ],
            "batches": [],
            "batch_idx": 0,
            "code_verdict": "",
            "fix_cycle": 0,
            "test_fix_attempt": 0,
            "test_fix_failures": [],
            "test_fix_summary": "",
            "debate_round": 0,
            "journal": [
                f"intake: ended early by user ({answer}) — REQUIREMENTS gaps "
                "waived, count incremented, planning from the brief as it stands"
            ],
        }

    if ans in INTAKE_END_ANSWERS:
        # _seed_brief carries the transcript in, so ending early never discards
        # answers the human already wrote.
        path = _seed_brief(tid, state.get("request", ""))
        return {
            "intake_done": True,
            "intake_unanswered": False,
            "brief_path": str(path),
            "journal": [f"intake: ended early by user ({answer})"],
        }

    # Re-reading after the resume: the human edits the file between the
    # interrupt and the answer, so the count taken before it is stale.
    # On LangGraph replay, `answered` equals the current count again — treat
    # an explicit submit with answers on disk as consent to spend a round.
    n_now = len(intake_answers(tid))
    if ans in INTAKE_SUBMIT_ANSWERS and n_now > 0:
        # TASK-033 (I3): with an active gap, a submit with answers runs the
        # I3 gate. Pass → intake_done + count increment (the interviewer's
        # next intake_ask will write the brief; but if the brief is already
        # present and contract-shaped, treat this as the COMPLETE path). Fail
        # → escalate so the operator answers the gaps or skips.
        if active_gap and not _i3_gap_answered(tid):
            return {
                "escalation": _i3_rejection_message(tid),
                "intake_unanswered": False,
                "journal": [
                    f"intake: submitted ({n_now} answers, {answer}) — I3 gate "
                    "failed, gaps unanswered"
                ],
            }
        return {
            "intake_unanswered": False,
            "journal": [f"intake: submitted ({n_now} answers, {answer})"],
        }

    if n_now <= answered:
        return {
            "intake_unanswered": True,
            "journal": ["intake: no new answers in the file, not spending an interviewer round"],
        }

    return {"intake_unanswered": False, "journal": [f"intake: answers submitted ({answer})"]}
