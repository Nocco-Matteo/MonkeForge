"""Graph nodes. Each node does one protocol step and returns a state delta."""
from __future__ import annotations
import functools, json, os, re, shlex, subprocess, time, traceback
from pathlib import Path

from langgraph.errors import GraphBubbleUp
from langgraph.types import interrupt

from . import config as C, events as ev
from .agents import (run_agent, render_prompt, parse_verdict, count_blockers,
                     parse_not_met, parse_disputed, read_if_exists)
from . import test_runner as tr
from .intake_materialize import materialize_intake_output


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=C.REPO, capture_output=True,
                          text=True).stdout.strip()


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
        cwd=C.REPO, capture_output=True, text=True,
    ).stdout.splitlines()
    untracked = subprocess.run(
        ["git", "ls-files", "-o", "--exclude-standard"],
        cwd=C.REPO, capture_output=True, text=True,
    ).stdout.splitlines()
    return [p.strip() for p in tracked + untracked
            if p.strip() and not _ignorable_dirty_path(p.strip())]


def _ignorable_dirty_path(path: str) -> bool:
    """Bytecode and caches never block init."""
    return "/__pycache__/" in path or path.endswith(".pyc")


def _dirty_blocks_interactive_init(paths: list[str]) -> bool:
    """True if a non-auto start must escalate (src/backend/frontend dirty)."""
    if not paths:
        return False
    for path in paths:
        if not any(path.startswith(p) for p in C.INIT_DIRTY_OK_PREFIXES):
            return True
    return False


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
            ev.emit("step_error", tid, name, f"{type(exc).__name__}: {exc}",
                    ms=ms, traceback=traceback.format_exc()[-2000:])
            ev.emit("step_end", tid, name,
                    f"[failed] CRASHED — {type(exc).__name__}: {exc} [{ms // 1000}s]",
                    ms=ms, outcome="failed")
            return {"escalation": f"{name} crashed: {type(exc).__name__}: {exc}",
                    "journal": [f"{name}: CRASHED — {type(exc).__name__}: {exc}"]}

        ms = int((time.time() - t0) * 1000)
        lines = delta.get("journal") or []
        summary = lines[-1] if lines else "no journal line"
        outcome = _step_outcome(delta)
        if delta.get("escalation"):
            summary = f"-> escalating: {delta['escalation']}"
        # Prefix the outcome so `grep '\[blocked\]' pipeline.log` finds what needs
        # a human, `grep '\[degraded\]'` what limped through — the blocking vs
        # non-blocking split at a glance.
        ev.emit("step_end", tid, name, f"[{outcome}] {summary} [{ms // 1000}s]",
                ms=ms, outcome=outcome)
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


# --- Step 0 ---------------------------------------------------------------

def brief_file(task_id: str) -> Path:
    return C.TASKS / f"TASK-{task_id}-brief.md"


def intake_file(task_id: str) -> Path:
    return C.TASKS / f"TASK-{task_id}-intake.md"


def refs_dir(task_id: str) -> Path:
    return C.TASKS / f"TASK-{task_id}-refs"


ANSWER_RE = re.compile(r"^\s*\*\*A:\*\*\s*(.+?)\s*$", re.MULTILINE)


def intake_answers(task_id: str) -> list[str]:
    """Non-empty answers currently written in the intake file."""
    path = intake_file(task_id)
    if not path.exists():
        return []
    return [a for a in ANSWER_RE.findall(path.read_text()) if a.strip()]


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
            "transcript is later and wins.", "",
            "## Request as submitted", "", request, "",
            "## Interview transcript", "",
            intake_file(task_id).read_text().strip(), "",
        ]
    else:
        parts += ["Not interviewed: this is the request as submitted.", "",
                  request, ""]
    path.write_text("\n".join(parts))
    return path


def intake_enabled(state) -> bool:
    """Interview by default; --auto skips it unless --interview forces it back on."""
    return bool(state.get("interview")) or not state.get("auto")


def _init_intake(state) -> dict:
    """Intake fields for init's delta, seeding the brief when no interview runs."""
    tid = state["task_id"]
    if intake_enabled(state):
        return {"intake_round": 0, "intake_done": False,
                "brief_path": str(brief_file(tid))}
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
        return {"branch": current, "debate_round": 0, "batch_idx": 0,
                "fix_cycle": 0, "test_fix_attempt": 0, "tests_waived": False,
                "baseline_batch_n": 0, "batch_test_baseline": [],
                "ux_render_cycle": 0, "visual_blockers": 0, "visual_shipped_blocked": False,
                "prev_visual_blockers": None, "visual_no_progress": 0,
                "escalation": "", "degradations": [], **_init_intake(state),
                "journal": [f"init: DRY RUN, git untouched (still on {current})"]}

    dirty_paths = _dirty_paths()
    if dirty_paths and _dirty_blocks_interactive_init(dirty_paths) and not state.get("auto"):
        blocking = [p for p in dirty_paths
                    if not any(p.startswith(x) for x in C.INIT_DIRTY_OK_PREFIXES)]
        hint = blocking[0] if blocking else dirty_paths[0]
        return {"escalation": "working tree is not clean; commit or stash first "
                              "(docs/tasks, docs/metrics, docs/prompts, docs/queue, "
                              "lg/ may stay dirty in interactive mode)",
                "journal": [f"init: dirty tree ({hint}), escalating"]}
    if dirty_paths:
        # Commit on the task branch, not on whatever branch is checked out now:
        # the WIP snapshot belongs to the task that caused it.
        subprocess.run(["git", "checkout", "-B", branch], cwd=C.REPO,
                       capture_output=True)
        _git("add", "-A")
        _git("commit", "-m", f"WIP: pre-task-{task_id} working tree")
    elif _git("rev-parse", "--abbrev-ref", "HEAD") != branch:
        subprocess.run(["git", "checkout", "-B", branch], cwd=C.REPO,
                       capture_output=True)

    return {"branch": branch, "debate_round": 0, "batch_idx": 0,
            "fix_cycle": 0, "test_fix_attempt": 0, "tests_waived": False,
            "baseline_batch_n": 0, "batch_test_baseline": [],
            "ux_render_cycle": 0, "visual_blockers": 0, "visual_shipped_blocked": False,
                "prev_visual_blockers": None, "visual_no_progress": 0,
            "escalation": "", "degradations": [], **_init_intake(state),
            "journal": [f"init: on {branch}"]}


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
    ref_list = (", ".join(sorted(p.name for p in refs.iterdir()))
                if refs.is_dir() else "none")

    prompt = render_prompt(
        "intake", task_id=tid, round=rnd, max_rounds=C.MAX_INTAKE_ROUNDS,
        request=state.get("request", ""),
        brief_path=str(brief_file(tid).relative_to(C.REPO)),
        intake_path=str(intake_file(tid).relative_to(C.REPO)),
        refs_path=str(refs.relative_to(C.REPO)), refs_list=ref_list)

    # A brief or intake file left behind by an earlier run of the same task id
    # must not be mistaken for this round's output. Only a file actually written
    # during this round counts.
    before = brief_file(tid).stat().st_mtime if brief_file(tid).exists() else None
    intake_before = intake_file(tid).stat().st_mtime if intake_file(tid).exists() else None

    code, out = run_agent("INTERVIEWER", tid, f"intake-r{rnd}", prompt)
    if code != 0:
        return {"intake_round": rnd,
                "escalation": f"intake round {rnd} failed (exit {code})",
                "journal": [f"intake r{rnd}: agent failed"]}

    # Gemini prints to stdout only; GLM/Cursor may edit files directly — fill gaps.
    materialize_intake_output(
        tid, rnd, out,
        intake_path=intake_file(tid),
        brief_path=brief_file(tid),
    )

    bf = brief_file(tid)
    now = bf.stat().st_mtime if bf.exists() else None
    fresh_brief = now is not None and now != before

    if "INTAKE: COMPLETE" in out.upper():
        if not fresh_brief:
            stale = " (a brief exists but this round did not write it)" if bf.exists() else ""
            return {"intake_round": rnd,
                    "escalation": f"interviewer reported COMPLETE but wrote no brief{stale}",
                    "journal": [f"intake r{rnd}: no brief written"]}
        has_brief = True
    elif "INTAKE: QUESTIONS" in out.upper():
        # It says it asked: the questions file must actually exist and have grown.
        if not intake_file(tid).exists():
            return {"intake_round": rnd,
                    "escalation": "interviewer reported QUESTIONS but wrote no "
                                  "questions file",
                    "journal": [f"intake r{rnd}: no questions written"]}
        has_brief = False
    elif fresh_brief:
        has_brief = True            # brief written this round, marker forgotten
    elif intake_file(tid).exists() and intake_file(tid).stat().st_mtime != intake_before:
        has_brief = False           # questions written this round, marker forgotten
    else:
        return {"intake_round": rnd,
                "escalation": "interviewer produced neither questions nor a brief",
                "journal": [f"intake r{rnd}: no output file"]}

    if has_brief:
        ev.emit("intake_complete", tid, "intake_ask",
                f"brief written after {rnd} round(s): "
                f"{brief_file(tid).relative_to(C.REPO)}")
        # The brief replaces the seed request as what `plan` works from.
        return {"intake_round": rnd, "intake_done": True,
                "brief_path": str(brief_file(tid)),
                "journal": [f"intake r{rnd}: brief complete"]}

    if rnd >= C.MAX_INTAKE_ROUNDS:
        return {"intake_round": rnd,
                "escalation": f"intake still unresolved after {C.MAX_INTAKE_ROUNDS} "
                              "rounds; answer 'skip' to plan from the brief as it "
                              "stands, or stop and rewrite the request",
                "journal": [f"intake r{rnd}: round cap reached"]}

    ev.emit("intake_questions", tid, "intake_ask",
            f"round {rnd}: questions waiting in "
            f"{intake_file(tid).relative_to(C.REPO)} — fill in the A: lines, then "
            f"./run.py resume {tid} --answer ok", round=rnd)
    return {"intake_round": rnd,
            "journal": [f"intake r{rnd}: questions written, waiting for answers"]}


INTAKE_END_ANSWERS = ("skip", "done", "stop", "enough")
INTAKE_SUBMIT_ANSWERS = ("ok", "yes", "submit", "continue", "proceed")


def intake_wait(state):
    """Pure human gate: no agent, so replaying it on resume costs nothing."""
    tid = state["task_id"]
    answered = len(intake_answers(tid))
    answer = interrupt({
        "stage": "intake",
        "task": tid,
        "round": state.get("intake_round", 0),
        "reason": f"answer the questions in "
                  f"{intake_file(tid).relative_to(C.REPO)}, then resume",
        "edit": str(intake_file(tid)),
        "answers_filled_in": answered,
        "hint": "resume with --answer ok when done, or --answer skip to stop "
                "interviewing and plan from what is there",
    })
    ans = str(answer).strip().lower()

    if ans in INTAKE_END_ANSWERS:
        # _seed_brief carries the transcript in, so ending early never discards
        # answers the human already wrote.
        path = _seed_brief(tid, state.get("request", ""))
        return {"intake_done": True, "intake_unanswered": False,
                "brief_path": str(path),
                "journal": [f"intake: ended early by user ({answer})"]}

    # Re-reading after the resume: the human edits the file between the
    # interrupt and the answer, so the count taken before it is stale.
    # On LangGraph replay, `answered` equals the current count again — treat
    # an explicit submit with answers on disk as consent to spend a round.
    n_now = len(intake_answers(tid))
    if ans in INTAKE_SUBMIT_ANSWERS and n_now > 0:
        return {"intake_unanswered": False,
                "journal": [f"intake: submitted ({n_now} answers, {answer})"]}

    if n_now <= answered:
        return {"intake_unanswered": True,
                "journal": ["intake: no new answers in the file, not spending "
                            "an interviewer round"]}

    return {"intake_unanswered": False,
            "journal": [f"intake: answers submitted ({answer})"]}


# --- Step 1 ---------------------------------------------------------------

def plan(state):
    tid = state["task_id"]
    # Never hand the proposer a path that is not there: an interview that
    # escalated at the round cap can reach this node with no brief written.
    brief = _seed_brief(tid, state["request"])
    prompt = render_prompt("plan", task_id=tid, request=state["request"],
                           brief_path=str(brief.relative_to(C.REPO)),
                           arch_docs=C.arch_docs_block(),
                           docs_dir=str(C.DOCS))
    code, _ = run_agent("PROPOSER", tid, "plan", prompt)
    if code != 0:
        return {"escalation": f"plan step failed (exit {code})",
                "journal": ["plan: failed"]}
    if not (C.PLANS / f"PLAN-{tid}.md").exists():
        return {"escalation": "proposer did not write the plan file",
                "journal": ["plan: no output file"]}
    # Decide has_ui here, before the debate, so the UX critic joins only when
    # there is a user surface. The analyst (brief) is the authority; fall back to
    # a keyword scan, and when in doubt run the UX critic (a wasted pass beats a
    # missed one). This is what the old post-judge HAS_UI gate becomes.
    has_ui = _detect_has_ui(brief, tid)
    has_perf = _detect_has_perf(brief, tid)
    return {"has_ui": has_ui, "has_perf": has_perf, "debate_round": 0, "tech_limits": [],
            "journal": [f"plan: written, has_ui={has_ui}, has_perf={has_perf}"]}


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


# --- Step 2 ---------------------------------------------------------------

TECH_LIMIT_RE = re.compile(r"^\s*TECH-LIMIT\s+VERIFIED\s*:\s*(.+?)\s*$",
                           re.MULTILINE | re.IGNORECASE)

_SECTION_HEADER_RE = re.compile(r"^##\s+Round\s+\d+\s+—\s+(\w+)", re.MULTILINE)


def _latest_section(debate_text: str, critic: str) -> str:
    """Extract only the last '## Round N — <critic>' section from the debate file.

    The debate file accumulates all rounds (reviewer, UX, proposer).  Counting
    [BLOCKER] across the whole file picks up resolved blockers that the
    proposer restated in its reply (e.g. '**[BLOCKER] … — RESOLVED**').  This
    helper returns just the text of the most recent section for the given
    critic ('Reviewer' or 'UX'), so blocker counts reflect only the current
    round.
    """
    if not debate_text:
        return ""
    headers = list(_SECTION_HEADER_RE.finditer(debate_text))
    last_start = None
    next_start = len(debate_text)
    for i, m in enumerate(headers):
        if m.group(1).lower() == critic.lower():
            last_start = m.start()
            # Bound the section at the NEXT header, not end-of-file: otherwise it
            # swallows the following proposer/UX section, whose reply restates the
            # blocker as "[BLOCKER] … — RESOLVED" and inflates count_blockers
            # (the 2-vs-1 miscount that escalated a single-blocker debate).
            next_start = headers[i + 1].start() if i + 1 < len(headers) else len(debate_text)
    if last_start is None:
        return ""
    return debate_text[last_start:next_start]


def debate_tech(state):
    """The technical critic. Critiques the plan AND certifies TECH-LIMIT claims.

    A round begins here; it re-critiques the proposer's latest plan, so this pass
    is also the verification of the previous reply — no separate verify node.
    It does NOT decide whether a UX blocker is acceptable (that is the designer's
    call in debate_ux); it only rules whether a claimed technical constraint is
    real, citing the code.

    Rounds 1..MAX_DEBATE_ROUNDS are full cycles: critique → reply → next.
    Round MAX_DEBATE_ROUNDS+1 is a verification-only pass: the critics re-check
    the proposer's last reply, but there is NO reply after it.  If the
    verification is clean → summary; if blockers remain → escalate with FRESH
    counts (not the stale pre-reply numbers that caused false escalations).
    """
    tid = state["task_id"]
    rnd = state.get("debate_round", 0) + 1
    is_verification = rnd > C.MAX_DEBATE_ROUNDS
    prompt = render_prompt("debate_review", task_id=tid, round=rnd,
                           docs_dir=str(C.DOCS))
    _, out = run_agent("PLAN_REVIEWER", tid, f"debate-r{rnd}-tech", prompt)
    text = read_if_exists(C.DEBATES / f"DEBATE-{tid}.md") or out
    latest_tech = _latest_section(text, "Reviewer")
    verdict = parse_verdict(out) if parse_verdict(out) != "UNKNOWN" else parse_verdict(text)
    blockers = count_blockers(out) or count_blockers(latest_tech)
    limits = sorted(set(TECH_LIMIT_RE.findall(out) + TECH_LIMIT_RE.findall(latest_tech)))
    delta = {"debate_round": rnd, "reviewer_verdict": verdict,
             "open_blockers": blockers, "tech_limits": limits,
             "redo_debate": False,
             "journal": [f"debate r{rnd} tech: {verdict}, {blockers} blockers, "
                         f"{len(limits)} tech-limit(s) verified"]}
    if not state.get("has_ui"):
        # No UX critic on this task: decide the round here.
        delta.update(_debate_decision({**state, **delta}, is_verification=is_verification))
    return delta


def debate_ux(state):
    """The designer. Authority on the user experience for this task.

    Its findings are followed; it re-reviews the plan each round and decides what
    to do about any verified TECH-LIMIT — accept it, accept it while proposing an
    alternative that meets the UX intent within the constraint, or dispute it.
    Convergence is ux_blockers == 0: the designer is satisfied.
    """
    tid = state["task_id"]
    rnd = state.get("debate_round", 0)
    prompt = render_prompt("debate_ux", task_id=tid, round=rnd,
                           tech_limits="; ".join(state.get("tech_limits", [])) or "none",
                           docs_dir=str(C.DOCS))

    # The UX reviewer is print-only (gemini): it prints the review, WE file it.
    # Asking it to write files itself makes its CLI attempt a tool call and
    # return an empty/errored response — which then got saved as the "review",
    # reading back as UNKNOWN/0-blockers and silently voiding the designer.
    # Gemini sometimes needs a few seconds to recover from a "malformed tool
    # call" state before a retry can succeed — an immediate re-call hits the
    # same error (observed on task-008: two identical 137-byte failures 7s
    # apart, while a 74s gap on the earlier run produced a 1319-byte review).
    UX_REVIEW_RETRIES = int(os.environ.get("PIPELINE_UX_REVIEW_RETRIES", "3"))
    UX_RETRY_BACKOFF_S = int(os.environ.get("PIPELINE_UX_REVIEW_BACKOFF_S", "10"))
    review, verdict = "", "UNKNOWN"
    for attempt in range(UX_REVIEW_RETRIES):
        step = f"debate-r{rnd}-ux" + (f"-retry{attempt}" if attempt else "")
        _, out = run_agent("UX_REVIEWER", tid, step, prompt)
        verdict = parse_verdict(out)
        if verdict != "UNKNOWN":
            review = out
            break
        if attempt < UX_REVIEW_RETRIES - 1:
            time.sleep(UX_RETRY_BACKOFF_S)

    if verdict == "UNKNOWN":
        # A UI task with no usable design review must not proceed silently as if
        # UX passed. Stop and tell the human; resuming proceeds to the verdict
        # WITHOUT a UX critique (fix the UX agent/prompt and rerun for a real one).
        return {"ux_verdict": "UNKNOWN", "ux_blockers": 0, "ux_shipped_blocked": True,
                "escalation": f"UX critic produced no usable review after "
                              f"{UX_REVIEW_RETRIES} tries (round {rnd}) — the designer "
                              "never weighed in on a UI task. Resuming proceeds to the "
                              "verdict without UX; to get a real critique, fix the UX "
                              "agent and rerun.",
                "degradations": ["UX critic produced no usable review — shipped without a designer critique"],
                "journal": [f"debate r{rnd} ux: FAILED — no verdict in output"]}

    # Materialise the critique ourselves — never depend on the agent writing it.
    (C.REVIEWS / f"UX-{tid}.md").write_text(review)
    debate_path = C.DEBATES / f"DEBATE-{tid}.md"
    with debate_path.open("a") as f:
        f.write(f"\n\n## Round {rnd} — UX\n\n{review.strip()}\n")

    blockers = count_blockers(review)

    # Rubber-stamp guard: on a re-review, a bare APPROVE that does not walk the
    # blockers it raised before is the failure we saw on 009 (a 168-byte
    # "APPROVE, all compliant" right after a 3-blocker REJECT). The prompt now
    # demands RESOLVED/STILL OPEN lines; if a clean APPROVE has none while prior
    # blockers existed, don't trust it — keep the round open so the designer must
    # actually verify (or the cap escalates honestly) instead of false-converging.
    prior_blockers = state.get("ux_blockers", 0)
    if (rnd > 1 and prior_blockers > 0 and verdict == "APPROVE"
            and blockers == 0 and "RESOLVED" not in review.upper()):
        verdict = "APPROVE_WITH_CHANGES"
        blockers = prior_blockers
        ev.emit("degraded", tid, "debate_ux",
                f"round {rnd}: bare APPROVE after {prior_blockers} prior blocker(s) "
                "with no RESOLVED lines — treated as unverified, not converged")
        delta_note = " (rubber-stamp APPROVE rejected — prior blockers not walked)"
    else:
        delta_note = ""

    delta = {"ux_verdict": verdict, "ux_blockers": blockers,
             "journal": [f"debate r{rnd} ux: {verdict}, {blockers} blockers{delta_note}"]}
    is_verification = rnd > C.MAX_DEBATE_ROUNDS
    delta.update(_debate_decision({**state, **delta}, is_verification=is_verification))
    return delta


def _debate_decision(state, is_verification: bool = False) -> dict:
    """Both critiques are in for this round: converge, loop to reply, or escalate.

    Normal rounds: if blockers remain, route to reply (the proposer gets a
    chance to respond).  Verification round (round > MAX_DEBATE_ROUNDS): no
    reply is allowed — if blockers remain, escalate with the FRESH counts from
    this round's critiques, not the stale pre-reply numbers.
    """
    tech_ok = state.get("reviewer_verdict") == "APPROVE" and not state.get("open_blockers", 0)
    ux_ok = (not state.get("has_ui")) or (
        state.get("ux_verdict") == "APPROVE" and not state.get("ux_blockers", 0))
    if tech_ok and ux_ok:
        return {"debate_next": "summary"}
    if is_verification:
        tech_b = state.get("open_blockers", 0)
        ux_b = state.get("ux_blockers", 0)
        n = tech_b + ux_b
        if n == 0:
            # Zero blockers survived verification. The only reason we are not in
            # the clean-APPROVE branch above is that a critic kept a SUGGESTION
            # open, so the verdict stayed APPROVE_WITH_CHANGES instead of a plain
            # APPROVE. No blocker means it is safe to ship — converge, do not
            # escalate on the verdict string alone (the spurious 0-blocker
            # escalation). Unresolved suggestions stay in the plan for implement.
            return {"debate_next": "summary",
                    "journal": ["debate verification: 0 blockers "
                                "(unresolved suggestions only) — converged"]}
        who = ("technical" if tech_b else "") + (" and " if tech_b and ux_b else "") \
              + ("UX" if ux_b else "")
        return {"debate_next": "summary",
                "escalation": f"debate exhausted {C.MAX_DEBATE_ROUNDS} rounds + "
                              f"verification: {n} {who} blocker(s) confirmed by the "
                              f"critics after the proposer's final reply",
                "journal": [f"debate verification: {n} {who} blocker(s) confirmed"]}
    return {"debate_next": "reply"}


def debate_reply(state):
    tid = state["task_id"]
    rnd = state["debate_round"]
    prompt = render_prompt("debate_reply", task_id=tid, round=rnd,
                           tech_limits="; ".join(state.get("tech_limits", [])) or "none",
                           docs_dir=str(C.DOCS))
    run_agent("PROPOSER", tid, f"debate-r{rnd}-reply", prompt)
    return {"journal": [f"debate r{rnd}: proposer replied to both critics"]}


# --- Step 3 ---------------------------------------------------------------

def summary(state):
    tid = state["task_id"]
    prompt = render_prompt("summary", task_id=tid,
                           docs_dir=str(C.DOCS))
    run_agent("SUMMARIZER", tid, "summary", prompt)
    return {"journal": ["summary: written"]}


# --- Step 4 ---------------------------------------------------------------

def judge(state):
    tid = state["task_id"]
    prompt = render_prompt("judge", task_id=tid,
                           docs_dir=str(C.DOCS))
    code, out = run_agent("JUDGE", tid, "verdict", prompt)

    if out.strip().startswith("ESCALATE:") or "\nESCALATE:" in out:
        reason = out.split("ESCALATE:", 1)[1].strip().splitlines()[0]
        return {"escalation": f"judge escalated: {reason}",
                "journal": ["judge: escalated"]}

    batches_file = C.FINAL / f"BATCHES-{tid}.json"
    if not batches_file.exists():
        return {"escalation": "judge did not produce BATCHES json",
                "journal": ["judge: no batch file"]}
    try:
        raw = json.loads(batches_file.read_text())
    except json.JSONDecodeError as exc:
        return {"escalation": f"BATCHES json invalid: {exc}",
                "journal": ["judge: bad batch json"]}

    batches = [{"n": b["n"], "scope": b.get("scope", ""), "status": "PENDING",
                "outcome": "", "deviations": "",
                "checklist": b.get("checklist", []),
                "test_failure_allowlist": b.get("test_failure_allowlist", [])}
               for b in raw]
    # has_ui was decided at plan-time (before the debate) and is not the judge's
    # to reset; the UX critique already happened during the debate.
    _write_progress(tid, batches)
    return {"batches": batches, "batch_idx": 0,
            "journal": [f"judge: {len(batches)} batches"]}


def _write_progress(task_id: str, batches: list[dict]) -> None:
    lines = [f"# PROGRESS-{task_id}", "",
             "| Batch | Scope | Status | Outcome | Deviations |",
             "|---|---|---|---|---|"]
    for b in batches:
        lines.append(f"| {b['n']} | {b['scope']} | {b['status']} | "
                     f"{b.get('outcome','')} | {b.get('deviations','')} |")
    (C.FINAL / f"PROGRESS-{task_id}.md").write_text("\n".join(lines) + "\n")


def checkpoint_plan(state):
    """Human gate after the verdict (skipped in AUTO)."""
    if state.get("auto"):
        return {"journal": ["checkpoint plan: auto, skipped"]}
    decision = interrupt({
        "stage": "plan approved?",
        "task": state["task_id"],
        "batches": [f"{b['n']}: {b['scope']}" for b in state.get("batches", [])],
        "final": str(C.FINAL / f"FINAL-{state['task_id']}.md"),
    })
    if str(decision).strip().lower() not in ("ok", "yes", "approve", "y"):
        return {"escalation": f"user rejected the plan: {decision}",
                "journal": ["checkpoint plan: rejected"]}
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


# --- Step 6 ---------------------------------------------------------------

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
    ev.emit("step_start", tid, step, "e2e DB down — starting the stack "
            f"(up to {C.E2E_UP_TIMEOUT}s)")
    ok = C.ensure_e2e_stack()
    if ok:
        ev.emit("step_end", tid, step, "e2e stack up, DB reachable")
        return True, DB_OK_NOTE
    ev.emit("degraded", tid, step,
            "e2e Postgres unreachable and could not be started — running with "
            "DB-gated tests SKIPPED (not passed). The run continues.")
    return False, DB_DOWN_NOTE


def _capture_test_baseline(state, b: dict, db_ok: bool) -> dict:
    """Snapshot failing tests before the implementer edits (once per batch)."""
    if state.get("baseline_batch_n") == b["n"] or not db_ok or C.DRY_RUN:
        return {}
    _, failures, summary = tr.run_repo_tests()
    ev.emit("step_start", state["task_id"], "implement",
            f"test baseline batch {b['n']}: {len(failures)} failing ({summary})")
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
        ev.emit("degraded", state["task_id"], "implement",
                f"branch starts RED: {len(failures)} test(s) already failing before "
                "any batch. New failures are still blocked, but these are tolerated "
                "as baseline — fix them or they hide behind the gate.")
        delta["baseline_failures"] = len(failures)
        delta["degradations"] = [f"branch started RED: {len(failures)} test(s) already failing before any batch — tolerated as baseline"]
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
        return {"test_fix_attempt": 0, "fix_cycle": 0,
                "journal": [f"impl b{b['n']}: skipped (tests waived)"]}

    db_ok, db_note = _db_note(tid, "implement")
    attempt = state.get("test_fix_attempt", 0)
    baseline_delta = _capture_test_baseline(state, b, db_ok)
    # Record the batch's base commit (HEAD before any edits) so code_review diffs
    # against it, not HEAD — the batch's work then shows whether the implementer
    # left it staged OR committed it (the false-REJECT that hit TASK-010 b1/b5).
    if attempt == 0 and not C.DRY_RUN:
        baseline_delta = {**baseline_delta, "batch_base_ref": _git("rev-parse", "HEAD").strip()}
    merged = {**state, **baseline_delta}

    prompt = render_prompt("implement", task_id=tid, batch_n=b["n"],
                           batch_scope=b["scope"], db_note=db_note,
                           arch_docs=C.arch_docs_block(),
                           checklist_items=", ".join(map(str, b.get("checklist", []))),
                           docs_dir=str(C.DOCS))
    step = f"impl-b{b['n']}"
    if attempt:
        step += f"-fix{attempt}"
    code, out = run_agent("IMPLEMENTER", tid, step, prompt)

    if "discrepancy" in out.lower() and "plan" in out.lower():
        return {**baseline_delta,
                "escalation": f"implementer reported a plan/code discrepancy (batch {b['n']})",
                "journal": [f"impl b{b['n']}: discrepancy"]}

    if code < 0:
        # Killed by a signal: run_agent already retried this transient death with
        # backoff (classify_output maps code<0 to transient), so a persistent
        # negative code means the environment is still broken — OOM, or the agent
        # daemon crashed (SIGPIPE/-13). A "fix" prompt is meaningless when the
        # agent produced nothing, so escalate as infrastructure without spending a
        # test-fix cycle; the human fixes the environment and resumes with `ok`.
        return {**baseline_delta,
                "escalation": f"implementer process killed by signal {-code} "
                              f"(infrastructure — likely OOM or agent-daemon crash), "
                              f"batch {b['n']}",
                "journal": [f"impl b{b['n']}: agent killed by signal {-code} "
                            f"(infra, not a code fault), escalating"]}

    if code != 0:
        if attempt + 1 > C.MAX_TEST_FIXES:
            return {**baseline_delta,
                    "escalation": f"implementer failed after {C.MAX_TEST_FIXES} attempts (batch {b['n']})",
                    "journal": [f"impl b{b['n']}: agent exit {code}, giving up"]}
        return {**baseline_delta, "test_fix_attempt": attempt + 1,
                "journal": [f"impl b{b['n']}: agent exit {code}, retry {attempt + 1}"]}

    ok, new_fails, summary = _in_graph_test_gate(merged, b, db_ok)
    if not ok:
        detail = f"{len(new_fails)} new failure(s): {new_fails[0][:120]}" if new_fails else summary
        if attempt + 1 > C.MAX_TEST_FIXES:
            return {**baseline_delta,
                    "escalation": f"tests still failing after {C.MAX_TEST_FIXES} attempts (batch {b['n']})",
                    "journal": [f"impl b{b['n']}: {detail}, giving up"]}
        return {**baseline_delta, "test_fix_attempt": attempt + 1,
                "journal": [f"impl b{b['n']}: {detail}, retry {attempt + 1}"]}

    # Stage so the reviewer's `git diff HEAD` sees this batch, new files included.
    _stage_all()
    suffix = "" if db_ok else " (DB-gated tests skipped)"
    test_note = f", {summary}, 0 new failures" if db_ok and not C.DRY_RUN else suffix
    delta = {**baseline_delta, "test_fix_attempt": 0, "fix_cycle": 0, "db_degraded": not db_ok,
             "journal": [f"impl b{b['n']}: done{test_note}"]}
    if not db_ok:
        delta["degradations"] = ["e2e DB unreachable — DB-backed tests were skipped, not passed"]
    return delta


def code_review(state):
    tid = state["task_id"]
    b = _current_batch(state)
    base = state.get("batch_base_ref") or "HEAD"
    prompt = render_prompt("code_review", task_id=tid, batch_n=b["n"],
                           batch_scope=b["scope"], diff_base=base,
                           checklist_items=", ".join(map(str, b.get("checklist", []))),
                           trusted_context=state.get("trusted_context", ""),
                           docs_dir=str(C.DOCS))
    _, out = run_agent("CODE_REVIEWER", tid, f"cr-b{b['n']}", prompt)
    review = read_if_exists(C.REVIEWS / f"CODE-{tid}-b{b['n']}.md") or out
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
    if n_items >= 3 and len(not_met) >= n_items - 1 and changed:
        return {"code_verdict": verdict, "not_met": not_met, "open_blockers": blockers,
                "fix_cycle": 0,
                "escalation": f"code review marked {len(not_met)}/{n_items} items NOT "
                    f"MET, but batch {b['n']} has a real staged diff ({len(changed)} "
                    "files). Likely the reviewer read the wrong diff, not a genuine "
                    "failure — check CODE-{tid}-b{n}.md before running fix cycles."
                    .replace("{tid}", tid).replace("{n}", str(b["n"])),
                "journal": [f"cr b{b['n']}: {len(not_met)}/{n_items} NOT MET vs "
                            f"{len(changed)}-file diff — suspected review mismatch"]}

    return {"code_verdict": verdict,
            "not_met": not_met,
            "open_blockers": blockers,
            "fix_cycle": 0,
            "journal": [f"cr b{b['n']}: {verdict}, {blockers} blockers, "
                        f"{len(not_met)} not met"]}


def code_fix(state):
    tid = state["task_id"]
    b = _current_batch(state)
    cycle = state.get("fix_cycle", 0) + 1
    prompt = render_prompt("code_fix", task_id=tid, batch_n=b["n"],
                           docs_dir=str(C.DOCS))
    _, out = run_agent("IMPLEMENTER", tid, f"cr-b{b['n']}-fix{cycle}", prompt)
    disputed = parse_disputed(out)
    if disputed:
        return {"fix_cycle": cycle, "disputed": disputed,
                "escalation": f"implementer disputed items in batch {b['n']}: {disputed}",
                "journal": [f"cr b{b['n']} fix{cycle}: DISPUTED"]}
    # Re-stage: the fix added more working-tree changes for code_verify to see.
    _stage_all()
    return {"fix_cycle": cycle, "journal": [f"cr b{b['n']} fix{cycle}: applied"]}


def code_verify(state):
    tid = state["task_id"]
    b = _current_batch(state)
    cycle = state.get("fix_cycle", 1)
    prompt = render_prompt("code_verify", task_id=tid, batch_n=b["n"],
                           docs_dir=str(C.DOCS))
    _, out = run_agent("CODE_REVIEWER", tid, f"cr-b{b['n']}-verify{cycle}", prompt)
    if "NOT_FIXED" in out:
        if cycle >= C.MAX_FIX_CYCLES:
            return {"escalation": f"batch {b['n']}: blockers still unfixed after "
                                  f"{C.MAX_FIX_CYCLES} cycles",
                    "journal": [f"cr b{b['n']} verify{cycle}: NOT_FIXED, giving up"]}
        return {"journal": [f"cr b{b['n']} verify{cycle}: NOT_FIXED, another cycle"]}
    return {"not_met": [], "open_blockers": 0,
            "journal": [f"cr b{b['n']} verify{cycle}: confirmed"]}


def close_batch(state):
    tid = state["task_id"]
    batches = [dict(b) for b in state["batches"]]
    idx = state["batch_idx"]
    b = batches[idx]
    b["status"] = "DONE"
    b["outcome"] = f"checklist met; review {state.get('code_verdict','')}"

    impl_log = sorted(C.RAW.glob(f"{tid}-impl-b{b['n']}-*.log"))
    if impl_log:
        text = impl_log[-1].read_text()
        marker = "DEVIATIONS"
        b["deviations"] = (text.split(marker, 1)[1].strip().splitlines() or ["none"])[0][:200] \
            if marker in text else "none"
    else:
        b["deviations"] = b["deviations"] or "none"

    _write_progress(tid, batches)
    subprocess.run(["git", "add", "-A"], cwd=C.REPO, capture_output=True)
    subprocess.run(["git", "commit", "-m",
                    f"task-{tid}: batch {b['n']} — {b['scope']}"],
                   cwd=C.REPO, capture_output=True)
    ev.emit("batch_done", tid, "close_batch",
            f"batch {b['n']}/{len(batches)} committed — next: "
            f"{'batch ' + str(b['n']+1) if idx + 1 < len(batches) else 'final gate'}")
    return {"batches": batches, "batch_idx": idx + 1, "fix_cycle": 0,
            "tests_waived": False, "baseline_batch_n": 0, "batch_test_baseline": [],
            "journal": [f"batch {b['n']}: committed"]}


# --- Step 6b: visual review (the "eyes") -----------------------------------
# The only gate that looks at pixels, not text. After the UI is built, render it
# to screenshots, hand them to a vision reviewer with deterministic facts
# (overflow, mode-identical, empty space), and loop fixes on the RENDERED result
# — the thing a text/diff review structurally cannot see.

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
        return {"escalation": "cannot render the UI: the e2e stack is not reachable",
                "journal": ["ux render: e2e stack down"]}

    # Ensure the render fixtures exist (idempotent upsert). Without this the gate
    # breaks silently whenever the e2e DB was re-seeded: the fixed character ids
    # the spec navigates to would not exist and every render would time out.
    if C.UX_SEED_SCRIPT.exists():
        seed = subprocess.run(["bash", str(C.UX_SEED_SCRIPT)], cwd=C.REPO,
                              capture_output=True, text=True)
        if seed.returncode != 0:
            return {"escalation": "cannot render the UI: seeding the render fixtures "
                                  f"failed — {(seed.stderr or seed.stdout or '')[-300:]}",
                    "journal": ["ux render: fixture seed failed"]}

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
        proc = subprocess.run(shlex.split(C.UX_RENDER_CMD), cwd=C.REPO / C.UX_RENDER_CWD,
                              env=env, capture_output=True, text=True,
                              timeout=C.UX_RENDER_TIMEOUT)
        log.write_text((proc.stdout or "") + "\n--- stderr ---\n" + (proc.stderr or ""))
    except subprocess.TimeoutExpired:
        return {"escalation": f"UI render timed out after {C.UX_RENDER_TIMEOUT}s "
                              "(fixture creation is slow on a cold cache; raise "
                              "PIPELINE_UX_RENDER_TIMEOUT or warm the fixtures)",
                "journal": ["ux render: timed out"]}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"escalation": f"UI render command failed to run: {exc}",
                "journal": ["ux render: command error"]}

    shots = sorted(out_dir.glob("*.png"))
    facts = read_if_exists(out_dir / "facts.json") or "{}"
    if not shots:
        return {"escalation": f"could not render the UI: no screenshots produced "
                              f"(render exited {proc.returncode}); see {log.name}",
                "journal": ["ux render: no screenshots"]}
    return {"render_facts": facts,
            "journal": [f"ux render: {len(shots)} screenshot(s), facts captured"]}


def ux_visual_review(state):
    tid = state["task_id"]
    cyc = state.get("ux_render_cycle", 0)
    shots = _screens_dir(tid)
    prompt = render_prompt("visual_review", task_id=tid,
                           screens_dir=str(shots.relative_to(C.REPO)) if shots.is_relative_to(C.REPO) else str(shots),
                           render_facts=state.get("render_facts", "{}"))

    review, verdict = "", "UNKNOWN"
    for attempt in range(2):
        step = f"visual-c{cyc}" + (f"-retry{attempt}" if attempt else "")
        _, out = run_agent("VISUAL_REVIEWER", tid, step, prompt)
        verdict = parse_verdict(out)
        if verdict != "UNKNOWN":
            review = out
            break

    if verdict == "UNKNOWN":
        return {"visual_verdict": "UNKNOWN", "visual_blockers": 0,
                "escalation": "visual reviewer produced no usable review — inspect "
                              f"the screenshots in {shots} by hand",
                "journal": [f"visual c{cyc}: FAILED — no verdict"]}

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
    delta = {"visual_verdict": verdict, "visual_blockers": blockers,
             "prev_visual_blockers": blockers, "visual_no_progress": no_progress,
             "journal": [f"visual c{cyc}: {verdict}, {blockers} blockers"]}

    if blockers and no_progress >= C.PLATEAU_THRESHOLD:
        delta["escalation"] = (
            f"the visual fix loop made no progress over {no_progress + 1} cycles "
            f"({blockers} blockers, oscillating between modes) — this usually needs a "
            f"design decision or a targeted manual fix, not more cycles. Screenshots "
            f"in {rel}; accept and ship, or stop and fix, then redo --from visual")
    elif blockers and cyc >= C.MAX_UX_RENDER_CYCLES:
        delta["escalation"] = (
            f"visual issues remain after {C.MAX_UX_RENDER_CYCLES} render/fix cycle(s) — "
            f"screenshots in {rel}; fix them or accept and ship")
    return delta


def ux_visual_fix(state):
    tid = state["task_id"]
    cyc = state.get("ux_render_cycle", 0) + 1
    shots = _screens_dir(tid)
    # VISUAL_FIXER (claude), not the blind IMPLEMENTER: it OPENS the screenshots,
    # so a fix to one mode does not silently break the other (the whack-a-mole
    # that plateaued task-009 at 2 blockers across 4 cycles).
    prompt = render_prompt("visual_fix", task_id=tid,
                           screens_dir=str(shots.relative_to(C.REPO)) if shots.is_relative_to(C.REPO) else str(shots),
                           render_facts=state.get("render_facts", "{}"),
                           docs_dir=str(C.DOCS))
    run_agent("VISUAL_FIXER", tid, f"visual-fix-c{cyc}", prompt)
    _stage_all()
    return {"ux_render_cycle": cyc,
            "journal": [f"visual fix c{cyc}: applied (with eyes), re-rendering"]}


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
        return {"escalation": "cannot profile renders: the e2e stack is not reachable",
                "journal": ["render measure: e2e stack down"]}
    if C.UX_SEED_SCRIPT.exists():
        seed = subprocess.run(["bash", str(C.UX_SEED_SCRIPT)], cwd=C.REPO,
                              capture_output=True, text=True)
        if seed.returncode != 0:
            return {"escalation": "cannot profile renders: seeding the fixtures failed — "
                                  f"{(seed.stderr or seed.stdout or '')[-300:]}",
                    "journal": ["render measure: fixture seed failed"]}

    out_dir = _renders_dir(tid)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "render-facts.json").unlink(missing_ok=True)

    env = os.environ.copy()
    env["RENDER_PROFILE_OUT"] = str(out_dir)
    env["E2E_REUSE_SERVER"] = "1"
    env["NEXT_PUBLIC_PROFILE"] = "1"   # activates the app's <Profiler> collector
    log = C.RAW / f"{tid}-render-measure-c{cyc}-{int(time.time())}.log"
    try:
        proc = subprocess.run(shlex.split(C.RENDER_CMD), cwd=C.REPO / C.RENDER_CWD,
                              env=env, capture_output=True, text=True,
                              timeout=C.RENDER_TIMEOUT)
        log.write_text((proc.stdout or "") + "\n--- stderr ---\n" + (proc.stderr or ""))
    except subprocess.TimeoutExpired:
        return {"escalation": f"render profiling timed out after {C.RENDER_TIMEOUT}s "
                              "(raise PIPELINE_RENDER_TIMEOUT or warm the fixtures)",
                "journal": ["render measure: timed out"]}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"escalation": f"render profiling command failed to run: {exc}",
                "journal": ["render measure: command error"]}

    if not (out_dir / "render-facts.json").exists():
        return {"escalation": f"could not profile renders: no render-facts.json "
                              f"(spec exited {proc.returncode}); see {log.name}",
                "journal": ["render measure: no facts"]}
    return {"journal": [f"render measure c{cyc}: facts captured"]}


def render_review(state):
    """Deterministic gate: compare re-render counts to the baseline. Any subtree
    re-rendering MORE than baseline is a regression → block. Pure arithmetic."""
    tid = state["task_id"]
    cyc = state.get("render_cycle", 0)
    raw = read_if_exists(_renders_dir(tid) / "render-facts.json")
    if not raw:
        return {"render_verdict": "SKIPPED", "render_blockers": 0,
                "journal": ["render review: no facts (measure skipped)"]}
    try:
        facts = json.loads(raw)
    except json.JSONDecodeError:
        return {"escalation": "render gate: render-facts.json is not valid JSON",
                "journal": ["render review: bad facts json"]}

    if not facts.get("instrumented"):
        # No <Profiler> hooks yet — degrade, do not block. The first perf task's
        # instrumentation batch adds them, then the gate goes live.
        return {"render_verdict": "SKIPPED", "render_blockers": 0,
                "degradations": ["render gate skipped: app not instrumented "
                                 "(no <Profiler>/window.__RENDER_LOG__ yet)"],
                "journal": ["render review: SKIPPED — app not instrumented"]}

    current = facts.get("renders", {}) or {}
    baseline_path = C.RENDERS / f"baseline-{tid}.json"
    baseline_raw = read_if_exists(baseline_path)
    if not baseline_raw:
        # First measurement establishes the baseline (captured on the PRE-change
        # tree per the brief); nothing to compare against yet.
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps(facts, indent=2))
        return {"render_verdict": "BASELINE", "render_blockers": 0,
                "journal": [f"render review: baseline established "
                            f"({sum(current.values())} re-renders on "
                            f"'{facts.get('interaction','?')}') — re-run after changes"]}

    baseline = (json.loads(baseline_raw).get("renders", {}) or {})
    regressions = {k: (baseline.get(k, 0), v) for k, v in current.items()
                   if v > baseline.get(k, 0)}
    improvements = {k: (baseline.get(k, 0), v) for k, v in current.items()
                    if v < baseline.get(k, 0)}
    blockers = len(regressions)
    lines = [f"# Render delta vs baseline — {facts.get('interaction','?')}", ""]
    lines += [f"REGRESSION  {k}: {b} -> {c}" for k, (b, c) in sorted(regressions.items())]
    lines += [f"improved    {k}: {b} -> {c}" for k, (b, c) in sorted(improvements.items())]
    (_renders_dir(tid) / "render-delta.md").write_text("\n".join(lines) + "\n")

    prev = state.get("prev_render_blockers")
    no_progress = state.get("render_no_progress", 0)
    if prev is not None and blockers and blockers >= prev:
        no_progress += 1
    else:
        no_progress = 0
    delta = {"render_verdict": "REGRESSED" if blockers else "PASS",
             "render_blockers": blockers, "prev_render_blockers": blockers,
             "render_no_progress": no_progress,
             "journal": [f"render review c{cyc}: {blockers} regression(s), "
                         f"{len(improvements)} improvement(s)"]}
    delta_file = _renders_dir(tid) / "render-delta.md"
    if blockers and no_progress >= C.PLATEAU_THRESHOLD:
        delta["escalation"] = (
            f"render fix loop made no progress over {no_progress + 1} cycles "
            f"({blockers} re-render regression(s)) — see {delta_file}; "
            "accept and ship, or fix manually then redo")
    elif blockers and cyc >= C.MAX_RENDER_CYCLES:
        delta["escalation"] = (
            f"re-render regressions remain after {C.MAX_RENDER_CYCLES} cycle(s) — "
            f"see {delta_file}; fix them or accept and ship")
    return delta


def render_fix(state):
    """The implementer fixes the re-render regressions with the numeric delta in
    hand (no pixels to open, unlike the visual fixer — the metric IS the report)."""
    tid = state["task_id"]
    cyc = state.get("render_cycle", 0) + 1
    delta = read_if_exists(_renders_dir(tid) / "render-delta.md") or "(no delta file)"
    prompt = render_prompt("render_fix", task_id=tid, render_delta=delta)
    run_agent("IMPLEMENTER", tid, f"render-fix-c{cyc}", prompt)
    _stage_all()
    return {"render_cycle": cyc,
            "journal": [f"render fix c{cyc}: applied, re-profiling"]}


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
    ev.emit("degraded", tid, "final_check",
            f"{n0} NEW test failure(s) after all batches ({summary}; "
            f"{len(all_failures) - n0} pre-existing tolerated). "
            f"Attempting auto-fix ({FINAL_FIX_ATTEMPTS} attempts).")

    for attempt in range(1, FINAL_FIX_ATTEMPTS + 1):
        batch = sorted(failures)[:FINAL_FIX_MAX_FAILURES_PER_ATTEMPT]
        fail_list = "\n".join(batch)
        remaining = len(failures) - len(batch)
        remaining_label = (f"({remaining} more failure(s) will be handled in "
                           f"later attempts — ignore them for now.)"
                           if remaining else "")
        prompt = render_prompt("preflight_fix", task_id=tid,
                               failures=fail_list, summary=summary,
                               remaining_label=remaining_label)
        _, out = run_agent("IMPLEMENTER", tid,
                           f"final-fix-{attempt}", prompt,
                           timeout=FINAL_FIX_TIMEOUT)
        _, all_failures, summary = tr.run_repo_tests()
        failures = tr.new_failures_since_baseline(all_failures, baseline, [])
        if not failures:
            ev.emit("step_end", tid, "final_check",
                    f"auto-fix attempt {attempt}: no new failures ({summary})")
            _stage_all()
            _git("commit", "-m", f"final gate: fix {n0} new test failure(s)")
            return None
        ev.emit("step_end", tid, "final_check",
                f"auto-fix attempt {attempt}: {len(failures)} new still failing ({summary})")

    n = len(failures)
    return {"escalation": f"final test gate: {n} NEW test(s) still failing after "
                          f"{FINAL_FIX_ATTEMPTS} auto-fix attempt(s) "
                          f"({summary}). Proceed and ship with known failures, "
                          f"or stop and fix manually.",
            "journal": f"final check: {n0} new initially, {n} still failing "
                       f"after {FINAL_FIX_ATTEMPTS} fix attempt(s)"}


def final_check(state):
    tid = state["task_id"]
    db_ok, db_note = _db_note(tid, "final_check")

    # Test gate: run the full suite and auto-fix any failures before the
    # LLM checklist review.  This catches both pre-existing baseline
    # failures that passed through the per-batch gate, and any regressions
    # that a degraded DB let through.
    test_result = (None if state.get("final_tests_waived")
                   else _final_test_fix_loop(tid, db_ok, set(state.get("task_baseline") or [])))
    if test_result:
        suffix = "" if db_ok else " (DB-gated tests skipped: e2e Postgres unreachable)"
        delta = {"not_met": [], "db_degraded": not db_ok,
                "escalation": test_result["escalation"],
                "journal": [test_result["journal"] + suffix]}
        if not db_ok:
            delta["degradations"] = ["e2e DB unreachable at final check — DB-backed tests were skipped, not passed"]
        return delta

    prompt = render_prompt("final_check", task_id=tid, db_note=db_note,
                           docs_dir=str(C.DOCS))
    _, out = run_agent("IMPLEMENTER", tid, "final-check", prompt)
    not_met = parse_not_met(out)
    suffix = "" if db_ok else " (DB-gated tests skipped: e2e Postgres unreachable)"
    delta = {"not_met": not_met, "db_degraded": not db_ok,
            "escalation": f"final gate: {len(not_met)} checklist items NOT MET" if not_met else "",
            "journal": [f"final check: {len(not_met)} not met" + suffix]}
    if not db_ok:
        delta["degradations"] = ["e2e DB unreachable at final check — DB-backed tests were skipped, not passed"]
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
    lines = [f"TASK-{tid} complete on {state.get('branch')}",
             f"batches: {len(state.get('batches', []))}",
             f"degradations: {len(degradations)}" if degradations else "degradations: none",
             *warnings,
             *state.get("journal", [])[-12:]]
    report = "\n".join(lines)
    (C.FINAL / f"REPORT-{tid}.md").write_text(report + "\n")
    ev.emit("run_end", tid, "wrap_up",
            f"all {len(state.get('batches', []))} batches done on "
            f"{state.get('branch')}, branch ready for review"
            + (f" — {len(degradations)} degradation(s), see the report"
               if degradations else ""),
            degraded=bool(degradations),
            degradations=degradations)
    return {"finished": True, "journal": ["wrap up: report written"]}


def _escalation_options(reason: str) -> dict:
    """What the valid answers mean for THIS escalation — the payload's menu.

    'ok' vs 'skip' do different things per escalation type; advertising them
    stops the human from rubber-stamping 'ok' without knowing what it does.
    """
    r = reason.lower()
    if "intake" in r or "interviewer" in r:
        return {"ok": "answer the questions in the intake file, then resume",
                "skip / done": "stop interviewing; plan from the brief as it stands"}
    if "tests still failing" in r:
        return {"<any text>": "waive the in-graph test gate for this batch and continue",
                "skip": "force-close this batch (mark it approved) and move on"}
    if ("ux" in r or "designer" in r) and "blocker" in r:
        return {"ok": "PROCEED past the designer, shipping the UX blockers unresolved "
                      "(recorded in the report) — prefer fixing them or accepting a "
                      "verified technical limit",
                "skip": "same as ok here — proceed past the debate",
                "redo": "re-run the debate from round 1, reusing the existing plan "
                        "(e.g. after fixing an agent or the UX reviewer prompt)"}
    if "debate hit the round cap" in r:
        return {"ok": "proceed to the verdict with the plan as it stands",
                "skip": "same — proceed past the debate",
                "redo": "re-run the debate from round 1, reusing the existing plan"}
    if "could not render" in r or "render command" in r or "render timed out" in r:
        return {"ok": "re-render (after fixing the browser/stack/fixtures)",
                "skip / force": "give up on the visual gate; ship without it"}
    if "visual issues remain" in r or "visual reviewer produced no" in r:
        return {"any answer": "SHIPS the remaining visual blockers to the final gate "
                              "(recorded in the report); the auto-fix cycles are spent. "
                              "To fix more: stop, edit the UI, then "
                              "./run.py redo <id> --from visual"}
    if "final test gate" in r:
        return {"retry": "re-run the fix loop (e.g. after manual fixes)",
                "ok": "ship with the known failures (recorded in the report)",
                "stop": "stop the run — fix the failing tests manually, then restart"}
    return {"ok": "retry / continue from here",
            "skip / close / force": "force-close the current batch (approve, clear blockers)"}


def escalate(state):
    tid = state.get("task_id", "?")
    reason = state.get("escalation", "unknown")

    # This node re-executes from the top when the run is resumed: interrupt()
    # replays. Without the marker every resume re-sends the same urgent push,
    # and nothing at all marks the moment you answered.
    if ev.open_escalation(tid, reason):
        ev.emit("escalation_open", tid, "escalate", reason,
                context=_context(state), journal=state.get("journal", [])[-5:])

    answer = interrupt({"stage": "escalation", "task": tid, "reason": reason,
                        "context": _context(state),
                        "answers": _escalation_options(reason),
                        "journal": state.get("journal", [])[-10:]})

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
    render_failed = ("render the ui" in r_low or "render command" in r_low
                     or "no screenshots" in r_low or "cannot render" in r_low)
    visual_blocked = ("visual issues remain" in r_low
                      or "visual reviewer produced no" in r_low)

    if ans == "redo" and debate_escalation:
        # Reset debate state so the fresh debate starts from round 1, reusing
        # the existing plan. Clear debate artifacts so the new debate does not
        # read the old rounds. Drop stale batches so summary→judge regenerate
        # FINAL/BATCHES from the fresh debate outcome.
        for path in (C.DEBATES / f"DEBATE-{tid}.md",
                     C.REVIEWS / f"UX-{tid}.md"):
            path.unlink(missing_ok=True)
        delta.update({
            "debate_round": 0, "reviewer_verdict": "", "open_blockers": 0,
            "ux_verdict": "", "ux_blockers": 0, "tech_limits": [],
            "debate_next": "", "ux_shipped_blocked": False,
            "batches": [], "batch_idx": 0, "code_verdict": "",
            "fix_cycle": 0, "test_fix_attempt": 0,
            "redo_debate": True,
            "degradations": [],
            "journal": [f"escalation resolved: {answer} "
                        "(redoing the debate from round 1, reusing the plan)"],
        })
        ev.emit("escalation_resolved", tid, "escalate",
                f"answered {answer!r} — redoing debate; was: {reason}")
        return delta

    if intake_escalation:
        # `skip` here means "stop interviewing", not "force the batch closed":
        # there are no batches yet, and marking code_verdict=APPROVE would leave
        # a booby trap that sends the *next* escalation straight to close_batch.
        if forced or ans in INTAKE_END_ANSWERS:
            path = _seed_brief(tid, state.get("request", ""))
            delta["intake_done"] = True
            delta["brief_path"] = str(path)
            delta["journal"] = [f"escalation resolved: {answer} "
                                "(intake ended, planning from the brief as it stands)"]
        elif intake_file(tid).exists() and state.get("intake_round", 0) > 0:
            delta["journal"] = [f"escalation resolved: {answer} "
                                "(intake questions on disk — answer them, then resume)"]
        else:
            delta["journal"] = [f"escalation resolved: {answer} (retrying the interview)"]
        ev.emit("escalation_resolved", tid, "escalate",
                f"answered {answer!r}; was: {reason}")
        return delta

    if render_failed or visual_blocked:
        # Handled before the generic `forced` branch (batches are already built,
        # so force-closing a batch is meaningless here). route_escalation_return
        # sends this back to ux_render UNLESS visual_shipped_blocked is set.
        if visual_blocked:
            delta["visual_shipped_blocked"] = True
            delta["degradations"] = ["shipped with unresolved RENDERED-UI blockers "
                                     "(see docs/reviews/screens)"]
            note = "proceeding with RENDERED-UI blockers — see screenshots"
        elif forced:
            delta["visual_shipped_blocked"] = True
            delta["degradations"] = ["shipped without a visual review (render unfixable)"]
            note = "render unfixable — shipping without a visual review"
        else:
            note = "retrying the render (fix the browser/stack first)"
        delta["journal"] = [f"escalation resolved: {answer} ({note})"]
        ev.emit("escalation_resolved", tid, "escalate",
                f"answered {answer!r}; was: {reason}")
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
            delta["journal"] = [f"escalation resolved: {answer} "
                                "(shipping with known test failures)"]
    elif forced:
        delta["not_met"] = []
        delta["open_blockers"] = 0
        delta["code_verdict"] = "APPROVE"
        delta["degradations"] = ["a batch was force-closed with unresolved blockers / NOT-MET items"]
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
        delta["journal"] = [f"escalation resolved: {answer} "
                            "(proceeding with UX blockers UNRESOLVED)"]
    else:
        delta["journal"] = [f"escalation resolved: {answer}"]
    ev.emit("escalation_resolved", tid, "escalate",
            f"answered {answer!r}" + (" — forcing batch closed" if forced else "")
            + f"; was: {reason}")
    return delta
