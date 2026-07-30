"""Shared helpers used across all node modules."""

from __future__ import annotations

import functools
import json
import subprocess
import time
import traceback
from pathlib import Path

from langgraph.errors import GraphBubbleUp
from langgraph.types import interrupt

from .. import config as C
from .. import events as ev
from ..agents import read_if_exists


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=C.REPO, capture_output=True, text=True).stdout.strip()


def _rel(path: Path) -> Path:
    """Relative to REPO if inside it, otherwise absolute."""
    try:
        return path.relative_to(C.REPO)
    except ValueError:
        return path


def _save(path: Path, content: str, append: bool = False) -> None:
    """Write agent stdout to the correct pipeline docs path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if append:
        with path.open("a") as f:
            f.write(content)
    else:
        path.write_text(content)


def _file_or_stdout(
    expected_path: Path, out: str, *, content: str | None = None, append: bool = False
) -> str:
    """Soft transition: use the file if the agent wrote it, otherwise persist stdout.

    Returns the content to read back: the file's text if it exists and is non-empty,
    else ``out``.  ``content`` is what gets saved (defaults to ``out``); use it to
    wrap stdout in section headers for append-mode debate files.
    """
    file_text = read_if_exists(expected_path)
    if file_text:
        return file_text
    save_text = content if content is not None else out
    if save_text.strip():
        _save(expected_path, save_text, append=append)
    return out


def _recover_artifact(tid: str, filename: str, expected_path: Path) -> bool:
    """Safety net: if the expected file is missing, search C.DOCS/** for
    ``filename`` and move it to ``expected_path``.

    Catches an agent that disobeyed the "print to stdout" instruction and wrote
    the file somewhere in the docs tree instead of the expected path.  The
    recovery is logged as a degradation (``notify=False`` — it is self-correcting,
    not worth a push).
    """
    if expected_path.exists():
        return False
    search_root = C.DOCS
    if not search_root.is_dir():
        return False
    for candidate in search_root.rglob(filename):
        if candidate.resolve() == expected_path.resolve():
            continue
        expected_path.parent.mkdir(parents=True, exist_ok=True)
        candidate.replace(expected_path)
        ev.emit(
            "degraded",
            tid,
            "recover",
            f"recovered {filename} from {_rel(candidate)} -> {_rel(expected_path)}",
            notify=False,
        )
        return True
    return False


def _extract_json(text: str) -> list | dict | None:
    """Find and parse a JSON array or object from agent stdout."""
    import re as _re

    for pattern in (r"```json\s*\n(.*?)\n```", r"```\s*\n(\[.*?\])\n```", r"(\[.*\])"):
        m = _re.search(pattern, text, _re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
    return None


def _stage_all() -> None:
    """Stage the working tree so the batch under review is a real, diffable object.

    The reviewer reads `git diff HEAD`. Without staging, that command omits new
    (untracked) files — a new test file reads as "absent" and the reviewer marks
    it NOT MET. `close_batch` still produces the single per-batch commit; this
    only makes the in-flight work visible between implement and commit.
    """
    if C.DRY_RUN:
        return
    subprocess.run(["git", "add", "-A"], cwd=C.REPO, capture_output=True)


def _dirty_paths() -> list[str]:
    """Repo-relative paths that differ from HEAD (modified, deleted, or untracked)."""
    tracked = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=C.REPO,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    untracked = subprocess.run(
        ["git", "ls-files", "-o", "--exclude-standard"],
        cwd=C.REPO,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    return [
        p.strip() for p in tracked + untracked if p.strip() and not _ignorable_dirty_path(p.strip())
    ]


def _ignorable_dirty_path(path: str) -> bool:
    """Bytecode and caches never block init."""
    return "/__pycache__/" in path or path.endswith(".pyc")


def _dirty_blocks_interactive_init(paths: list[str]) -> bool:
    """True if a non-auto start must escalate (src/backend/frontend dirty)."""
    if not paths:
        return False
    return any(not any(path.startswith(p) for p in C.INIT_DIRTY_OK_PREFIXES) for path in paths)


def _context(state, node: str = "") -> str:
    """Where the run is, in one phrase — batch, fix cycle, debate round.

    `node` is the node about to run (from instrument). debate_tech increments
    debate_round inside the node, so before it runs the display is off by one;
    bump it here for that node only.
    """
    bits = []
    batches = state.get("batches") or []
    idx = state.get("batch_idx", 0)
    if batches and idx < len(batches):
        bits.append(f"batch {batches[idx]['n']}/{len(batches)}")
    dr = state.get("debate_round", 0)
    if node == "debate_tech":
        dr += 1
    if dr:
        bits.append(f"debate round {dr}")
    if state.get("fix_cycle"):
        bits.append(f"fix cycle {state['fix_cycle']}")
    if state.get("test_fix_attempt"):
        bits.append(f"test retry {state['test_fix_attempt']}")
    return ", ".join(bits)


def instrument(name: str, fn):
    """Wrap a node so its start, end, and crash are always recorded.

    Applied centrally in graph.py rather than as a decorator on each node: a new
    node then cannot be added without instrumentation, which is exactly the
    failure this is meant to prevent.

    A crash is converted into an escalation delta instead of killing the run.
    Every node already routes on `escalation`, so the graph pauses for a human
    with the traceback recorded, rather than dying with a stack trace on stdout
    that nothing pushes to your phone.
    """

    @functools.wraps(fn)
    def wrapper(state):
        C.ensure_dirs()
        tid = state.get("task_id", "?")
        ctx = _context(state, name)
        ev.emit("step_start", tid, name, ctx or "starting")
        t0 = time.time()
        try:
            delta = fn(state) or {}
        except GraphBubbleUp:
            # interrupt()/Command control flow — not an error, must propagate
            # untouched or the graph can never pause.
            raise
        except Exception as exc:
            ms = int((time.time() - t0) * 1000)
            ev.emit(
                "step_error",
                tid,
                name,
                f"{type(exc).__name__}: {exc}",
                ms=ms,
                traceback=traceback.format_exc()[-2000:],
            )
            ev.emit(
                "step_end",
                tid,
                name,
                f"[failed] CRASHED — {type(exc).__name__}: {exc} [{ms // 1000}s]",
                ms=ms,
                outcome="failed",
            )
            return {
                "escalation": f"{name} crashed: {type(exc).__name__}: {exc}",
                "journal": [f"{name}: CRASHED — {type(exc).__name__}: {exc}"],
            }

        ms = int((time.time() - t0) * 1000)
        lines = delta.get("journal") or []
        summary = lines[-1] if lines else "no journal line"
        outcome = _step_outcome(delta)
        if delta.get("escalation"):
            summary = f"-> escalating: {delta['escalation']}"
        # Prefix the outcome so `grep '\[blocked\]' pipeline.log` finds what needs
        # a human, `grep '\[degraded\]'` what limped through — the blocking vs
        # non-blocking split at a glance.
        ev.emit(
            "step_end", tid, name, f"[{outcome}] {summary} [{ms // 1000}s]", ms=ms, outcome=outcome
        )
        return delta

    return wrapper


def _step_outcome(delta: dict) -> str:
    """ok | degraded | blocked — classify a node's own result (crash=failed is
    handled separately). `blocked` means it wants a human; `degraded` means it
    proceeded with a known compromise (it recorded one in the ledger)."""
    if delta.get("escalation"):
        return "blocked"
    if delta.get("degradations"):
        return "degraded"
    return "ok"


# --- shared across implement / review / quality_gates / finalize -------------


def _current_batch(state) -> dict:
    return state["batches"][state["batch_idx"]]


DB_OK_NOTE = "The e2e Postgres (:5433) is up; run the full suite, DB-backed tests included."
DB_DOWN_NOTE = (
    "INFRASTRUCTURE NOTE: the e2e Postgres (:5433) is NOT reachable and could not "
    "be started. Run the suite anyway with the DB-backed tests skipped, and say so "
    "explicitly in your report (which tests were skipped and why). Do NOT try to "
    "start Docker yourself, do NOT treat the skipped tests as failures, and do NOT "
    "report the work complete as if they had passed."
)


def _db_note(tid: str, step: str) -> tuple[bool, str]:
    """Ensure the e2e DB is up. Never blocks the graph: degrade with a note instead."""
    if C.DRY_RUN:
        return True, DB_OK_NOTE
    if C.db_reachable():
        return True, DB_OK_NOTE
    ev.emit(
        "step_start", tid, step, f"e2e DB down — starting the stack (up to {C.E2E_UP_TIMEOUT}s)"
    )
    ok = C.ensure_e2e_stack()
    if ok:
        ev.emit("step_end", tid, step, "e2e stack up, DB reachable")
        return True, DB_OK_NOTE
    ev.emit(
        "degraded",
        tid,
        step,
        "e2e Postgres unreachable and could not be started — running with "
        "DB-gated tests SKIPPED (not passed). The run continues.",
    )
    return False, DB_DOWN_NOTE


def _write_progress(task_id: str, batches: list[dict]) -> None:
    lines = [
        f"# PROGRESS-{task_id}",
        "",
        "| Batch | Scope | Status | Outcome | Deviations |",
        "|---|---|---|---|---|",
    ]
    for b in batches:
        lines.append(
            f"| {b['n']} | {b['scope']} | {b['status']} | "
            f"{b.get('outcome', '')} | {b.get('deviations', '')} |"
        )
    (C.FINAL / f"PROGRESS-{task_id}.md").write_text("\n".join(lines) + "\n")


# --- escalation -------------------------------------------------------------


def _escalation_options(reason: str) -> dict:
    """What the valid answers mean for THIS escalation — the payload's menu.

    'ok' vs 'skip' do different things per escalation type; advertising them
    stops the human from rubber-stamping 'ok' without knowing what it does.
    """
    r = reason.lower()
    if "intake" in r or "interviewer" in r:
        return {
            "ok": "answer the questions in the intake file, then resume",
            "skip / done": "stop interviewing; plan from the brief as it stands",
        }
    if "tests still failing" in r:
        return {
            "<any text>": "waive the in-graph test gate for this batch and continue",
            "skip": "force-close this batch (mark it approved) and move on",
        }
    if ("ux" in r or "designer" in r) and "blocker" in r:
        return {
            "ok": "PROCEED past the designer, shipping the UX blockers unresolved "
            "(recorded in the report) — prefer fixing them or accepting a "
            "verified technical limit",
            "skip": "same as ok here — proceed past the debate",
            "redo": "re-run the debate from round 1, reusing the existing plan "
            "(e.g. after fixing an agent or the UX reviewer prompt)",
        }
    if "debate hit the round cap" in r:
        return {
            "ok": "proceed to the verdict with the plan as it stands",
            "skip": "same — proceed past the debate",
            "redo": "re-run the debate from round 1, reusing the existing plan",
        }
    if "could not render" in r or "render command" in r or "render timed out" in r:
        return {
            "ok": "re-render (after fixing the browser/stack/fixtures)",
            "skip / force": "give up on the visual gate; ship without it",
        }
    if "visual issues remain" in r or "visual reviewer produced no" in r:
        return {
            "any answer": "SHIPS the remaining visual blockers to the final gate "
            "(recorded in the report); the auto-fix cycles are spent. "
            "To fix more: stop, edit the UI, then "
            "./run.py redo <id> --from visual"
        }
    if "final test gate" in r:
        return {
            "retry": "re-run the fix loop (e.g. after manual fixes)",
            "ok": "ship with the known failures (recorded in the report)",
            "stop": "stop the run — fix the failing tests manually, then restart",
        }
    return {
        "ok": "retry / continue from here",
        "skip / close / force": "force-close the current batch (approve, clear blockers)",
    }


def escalate(state):
    tid = state.get("task_id", "?")
    reason = state.get("escalation", "unknown")

    # This node re-executes from the top when the run is resumed: interrupt()
    # replays. Without the marker every resume re-sends the same urgent push,
    # and nothing at all marks the moment you answered.
    if ev.open_escalation(tid, reason):
        ev.emit(
            "escalation_open",
            tid,
            "escalate",
            reason,
            context=_context(state),
            journal=state.get("journal", [])[-5:],
        )

    answer = interrupt(
        {
            "stage": "escalation",
            "task": tid,
            "reason": reason,
            "context": _context(state),
            "answers": _escalation_options(reason),
            "journal": state.get("journal", [])[-10:],
        }
    )

    ev.close_escalation(tid)
    delta = {"escalation": "", "test_fix_attempt": 0}
    ans = str(answer).strip().lower()
    forced = ans in ("skip", "close", "force close", "force")
    test_escalation = "tests still failing" in reason.lower()
    intake_escalation = "intake" in reason.lower() or "interviewer" in reason.lower()
    r_low = reason.lower()
    ux_escalation = ("ux" in r_low or "designer" in r_low) and "blocker" in r_low
    debate_escalation = "debate" in r_low
    # A render that could not run is retryable (fix the browser/stack, re-render);
    # a visual review that still has blockers is not — it ships or it doesn't.
    render_failed = (
        "render the ui" in r_low
        or "render command" in r_low
        or "no screenshots" in r_low
        or "cannot render" in r_low
    )
    visual_blocked = "visual issues remain" in r_low or "visual reviewer produced no" in r_low

    if ans == "redo" and debate_escalation:
        # Reset debate state so the fresh debate starts from round 1, reusing
        # the existing plan. Clear debate artifacts so the new debate does not
        # read the old rounds. Drop stale batches so summary→judge regenerate
        # FINAL/BATCHES from the fresh debate outcome.
        for path in (C.DEBATES / f"DEBATE-{tid}.md", C.REVIEWS / f"UX-{tid}.md"):
            path.unlink(missing_ok=True)
        delta.update(
            {
                "debate_round": 0,
                "reviewer_verdict": "",
                "open_blockers": 0,
                "ux_verdict": "",
                "ux_blockers": 0,
                "tech_limits": [],
                "debate_next": "",
                "ux_shipped_blocked": False,
                "batches": [],
                "batch_idx": 0,
                "code_verdict": "",
                "fix_cycle": 0,
                "test_fix_attempt": 0,
                "redo_debate": True,
                "degradations": [],
                "journal": [
                    f"escalation resolved: {answer} "
                    "(redoing the debate from round 1, reusing the plan)"
                ],
            }
        )
        ev.emit(
            "escalation_resolved",
            tid,
            "escalate",
            f"answered {answer!r} — redoing debate; was: {reason}",
        )
        return delta

    if intake_escalation:
        # `skip` here means "stop interviewing", not "force the batch closed":
        # there are no batches yet, and marking code_verdict=APPROVE would leave
        # a booby trap that sends the *next* escalation straight to close_batch.
        # Lazy import to avoid circular dependency (intake imports from common).
        from .intake import INTAKE_END_ANSWERS, _seed_brief, intake_file

        if forced or ans in INTAKE_END_ANSWERS:
            path = _seed_brief(tid, state.get("request", ""))
            delta["intake_done"] = True
            delta["brief_path"] = str(path)
            delta["journal"] = [
                f"escalation resolved: {answer} "
                "(intake ended, planning from the brief as it stands)"
            ]
        elif intake_file(tid).exists() and state.get("intake_round", 0) > 0:
            delta["journal"] = [
                f"escalation resolved: {answer} "
                "(intake questions on disk — answer them, then resume)"
            ]
        else:
            delta["journal"] = [f"escalation resolved: {answer} (retrying the interview)"]
        ev.emit("escalation_resolved", tid, "escalate", f"answered {answer!r}; was: {reason}")
        return delta

    if render_failed or visual_blocked:
        # Handled before the generic `forced` branch (batches are already built,
        # so force-closing a batch is meaningless here). route_escalation_return
        # sends this back to ux_render UNLESS visual_shipped_blocked is set.
        if visual_blocked:
            delta["visual_shipped_blocked"] = True
            delta["degradations"] = [
                "shipped with unresolved RENDERED-UI blockers (see reviews/screens)"
            ]
            note = "proceeding with RENDERED-UI blockers — see screenshots"
        elif forced:
            delta["visual_shipped_blocked"] = True
            delta["degradations"] = ["shipped without a visual review (render unfixable)"]
            note = "render unfixable — shipping without a visual review"
        else:
            note = "retrying the render (fix the browser/stack first)"
        delta["journal"] = [f"escalation resolved: {answer} ({note})"]
        ev.emit("escalation_resolved", tid, "escalate", f"answered {answer!r}; was: {reason}")
        return delta

    final_test_escalation = "final test gate" in r_low

    if final_test_escalation:
        if ans in ("stop", "no", "abort", "cancel"):
            delta["finished"] = True
            delta["journal"] = [f"escalation resolved: {answer} (run stopped by human)"]
        elif ans in ("retry", "again", "fix"):
            delta["journal"] = [f"escalation resolved: {answer} (re-running fix loop)"]
        else:
            delta["baseline_failures"] = True
            delta["final_tests_waived"] = True
            delta["degradations"] = ["shipped with known failing tests at the final gate"]
            delta["journal"] = [
                f"escalation resolved: {answer} (shipping with known test failures)"
            ]
    elif forced:
        delta["not_met"] = []
        delta["open_blockers"] = 0
        delta["code_verdict"] = "APPROVE"
        delta["degradations"] = [
            "a batch was force-closed with unresolved blockers / NOT-MET items"
        ]
        delta["journal"] = [f"escalation resolved: {answer} (force close batch)"]
    elif test_escalation:
        delta["tests_waived"] = True
        delta["degradations"] = ["in-graph test gate waived for a batch"]
        delta["journal"] = [f"escalation resolved: {answer} (tests waived for batch)"]
    elif ux_escalation:
        # Proceeding past a UX escalation ships the unresolved blockers. Record
        # it in the ledger so wrap_up/doctor report it instead of it vanishing.
        delta["ux_shipped_blocked"] = True
        delta["degradations"] = ["shipped with unresolved UX (designer) blockers"]
        delta["journal"] = [
            f"escalation resolved: {answer} (proceeding with UX blockers UNRESOLVED)"
        ]
    else:
        delta["journal"] = [f"escalation resolved: {answer}"]
    ev.emit(
        "escalation_resolved",
        tid,
        "escalate",
        f"answered {answer!r}" + (" — forcing batch closed" if forced else "") + f"; was: {reason}",
    )
    return delta
