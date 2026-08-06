"""Shared helpers used across all node modules."""

from __future__ import annotations

import functools
import json
import re
import subprocess
import time
import traceback
from pathlib import Path

from langgraph.errors import GraphBubbleUp
from langgraph.types import interrupt

from .. import config as C
from .. import events as ev
from ..agents import TERMINAL_MARKERS as _TERMINAL_MARKERS
from ..agents import read_if_exists


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=C.REPO, capture_output=True, text=True).stdout.strip()


def git_identity() -> dict[str, str]:
    """Live git facts for the current ``C.REPO`` (not pipeline state).

    Returns ``repo``, ``branch``, ``sha``. Empty strings when git is unavailable
    or ``NO_GIT`` / missing ``.git``. Used so wrap_up / batch_done never claim
    ``state["branch"]`` as if it were HEAD.
    """
    repo = str(C.REPO.resolve())
    if C.NO_GIT or C.DRY_RUN:
        return {"repo": repo, "branch": "", "sha": ""}
    if not (C.REPO / ".git").exists():
        return {"repo": repo, "branch": "", "sha": ""}
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    sha = _git("rev-parse", "HEAD")
    return {"repo": repo, "branch": branch, "sha": sha}


def branch_mismatch_reason(expected: str | None) -> str | None:
    """If HEAD is not ``expected``, return an escalation reason; else None.

    Skipped under DRY_RUN / NO_GIT. Empty ``expected`` skips (init has not set
    the task branch yet). Detached HEAD (``HEAD``) always mismatches a named
    branch.
    """
    if C.DRY_RUN or C.NO_GIT:
        return None
    expected = (expected or "").strip()
    if not expected:
        return None
    ident = git_identity()
    actual = ident["branch"]
    if actual == expected:
        return None
    sha = ident["sha"][:12] if ident["sha"] else "?"
    return (
        f"git branch mismatch: HEAD is {actual!r} at {sha} but this task "
        f"expects {expected!r} (repo={ident['repo']}). Checkout the task "
        f"branch in that repo (or fix PIPELINE_REPO / --repo), then resume "
        f"with ok — refusing to commit on the wrong branch."
    )


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

    In append mode the content is ALWAYS appended — the "use the file if the agent
    wrote it" shortcut only applies to one-shot writes (where the agent may have
    written the file itself and we'd duplicate it). For append-mode debate files,
    skipping the append when the file already exists silently drops round 2+
    reviewer output from the debate history, leaving the proposer blind to new
    blockers.
    """
    if append:
        save_text = content if content is not None else out
        if save_text.strip():
            _save(expected_path, save_text, append=True)
        return read_if_exists(expected_path) or out
    file_text = read_if_exists(expected_path)
    if file_text:
        return file_text
    save_text = content if content is not None else out
    if save_text.strip():
        _save(expected_path, save_text, append=False)
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

    # Fenced ```json block — relaxed closing (no \n required before ```)
    m = _re.search(r"```json\s*\n?(.*?)```", text, _re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # Fenced ``` block containing a JSON array
    m = _re.search(r"```\s*\n(\[.*?\])\s*```", text, _re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # Raw JSON array — bracket-match from each `[`, return first valid parse
    # that is an array of dicts (objects). The BATCHES json is always
    # [{"n": 1, ...}] — an array of objects. Arrays of scalars like
    # ["baseline_failures"] are prose false positives (e.g. delta["key"] in
    # code-discussion text) and must be skipped. If no array-of-dicts is
    # found, fall back to the first valid array of any shape (for callers
    # that use _extract_json for non-BATCHES purposes).
    first_array_any: list | None = None
    for start in range(len(text)):
        if text[start] != "[":
            continue
        depth = 0
        in_string = False
        escape = False
        for end in range(start, len(text)):
            c = text[end]
            if escape:
                escape = False
                continue
            if c == "\\":
                escape = True
                continue
            if c == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start : end + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, list):
                        if first_array_any is None:
                            first_array_any = parsed
                        # Prefer arrays of dicts (BATCHES shape) over arrays
                        # of scalars (prose false positives like ["key"]).
                        if parsed and all(isinstance(item, dict) for item in parsed):
                            return parsed
                    break
    return first_array_any


def _strip_batches_block(text: str, batches_json: list | dict | None) -> str:
    """Remove the fenced BATCHES json block from ``text`` (judge prose → FINAL).

    Mirrors ``_extract_json``'s two fence patterns exactly (```json fence, then
    plain-fence array) so the span removed is the span ``_extract_json`` actually
    matched. Only the first fence whose ``json.loads`` equals ``batches_json`` is
    removed; any other fenced JSON in the prose (examples, illustrative blocks)
    survives. Raw unfenced arrays are never stripped. No-op when
    ``batches_json`` is falsy (judge produced no BATCHES — nothing to strip).
    """
    import re as _re

    if not batches_json:
        return text

    patterns = (
        r"```json\s*\n?(.*?)```",        # ```json fence (relaxed closing)
        r"```\s*\n(\[.*?\])\s*```",      # plain-fence array
    )
    for pat in patterns:
        for m in _re.finditer(pat, text, _re.DOTALL):
            try:
                parsed = json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
            if parsed == batches_json:
                return text[: m.start()] + text[m.end() :]
    return text


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
    """True if a non-auto start must escalate (non-pipeline dirty paths)."""
    if not paths:
        return False
    return any(not any(path.startswith(p) for p in C.INIT_DIRTY_OK_PREFIXES) for path in paths)


# Nodes whose step_start context may show ``debate round N``. Everywhere else
# (summary/judge/implement/…) keeping the stale counter made ``ok``-after-stuck
# look like the debate had restarted (TASK-026 council-log).
_DEBATE_CONTEXT_NODES = frozenset({"debate_tech", "debate_ux", "debate_reply"})


def _context(state, node: str = "") -> str:
    """Where the run is, in one phrase — batch, fix cycle, debate round.

    `node` is the node about to run (from instrument). debate_tech increments
    debate_round inside the node, so before it runs the display is off by one;
    bump it here for that node only.

    ``debate round N`` is only shown on debate critic/reply nodes (and on
    escalate pauses while batches are not yet built). Implement/verify keep
    batch + fix-cycle only.
    """
    bits = []
    batches = state.get("batches") or []
    idx = state.get("batch_idx", 0)
    if batches and idx < len(batches):
        bits.append(f"batch {batches[idx]['n']}/{len(batches)}")
    dr = state.get("debate_round", 0)
    if node == "debate_tech":
        dr += 1
    show_debate_round = (
        node in _DEBATE_CONTEXT_NODES
        or (not node and dr and not batches)  # escalate payload mid-debate
    )
    if show_debate_round and dr:
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
                "escalation": f"step crashed in {name} — see the journal for the exception detail",
                "journal": [f"{name}: CRASHED — {type(exc).__name__}: {exc}"],
            }

        ms = int((time.time() - t0) * 1000)
        lines = delta.get("journal") or []
        summary = _step_summary(lines)
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


# Prefer debate/review outcome lines over trailing notes (e.g. "visual review
# disabled…") so Discord step_end / council-log show VERDICT + blocker counts.
_DEBATE_OUTCOME_RE = re.compile(
    r"^debate r\d+ (?:tech|ux):\s*"
    r"(APPROVE_WITH_CHANGES|APPROVE|REJECT|UNKNOWN)\b",
    re.IGNORECASE,
)


def _step_summary(lines: list[str]) -> str:
    """Pick the journal line that best summarises a node for step_end notify."""
    if not lines:
        return "no journal line"
    for line in reversed(lines):
        if _DEBATE_OUTCOME_RE.match((line or "").strip()):
            return line
    for line in reversed(lines):
        s = line or ""
        if "blocker" in s.lower() and any(
            v in s for v in ("REJECT", "APPROVE", "APPROVE_WITH_CHANGES")
        ):
            return line
    return lines[-1]


def _trust_output(code: int, out: str, health: str) -> bool:
    """True only when the agent produced trustworthy output: a healthy
    classification, a zero exit, and non-empty content. Any failure → False,
    so the caller escalates instead of acting on garbage (the silent-APPROVE
    rubber-stamp that let a crashed/empty reviewer ship a batch).

    `health` is the value returned by ``agents.classify_output``; the caller
    computes it once and passes it in so this guard does not re-import agents.

    A bare terminal marker (``INTAKE: COMPLETE``, ``VERDICT: APPROVE`` etc.) is
    trustworthy: these are the exact signals the prompt mandates when the agent
    has nothing else to say (the intake interviewer wrote the brief to a file;
    the debate reviewer's filter deleted all items). ``classify_output`` already
    classifies them ``ok``; the check is kept here so a caller that computed
    ``health`` some other way still cannot false-fire on a clean approval or a
    file-backed intake completion.
    """
    if code == 0 and bool(out and out.strip()):
        stripped = out.strip().upper()
        if stripped in _TERMINAL_MARKERS:
            return True
    return health == "ok" and code == 0 and bool(out and out.strip())


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


DB_OK_NOTE = f"The e2e Postgres (:{C.E2E_DB_PORT}) is up; run the full suite, DB-backed tests included."
DB_DOWN_NOTE = (
    f"INFRASTRUCTURE NOTE: the e2e Postgres (:{C.E2E_DB_PORT}) is NOT reachable and could not "
    "be started. Run the suite anyway with the DB-backed tests skipped, and say so "
    "explicitly in your report (which tests were skipped and why). Do NOT try to "
    "start Docker yourself, do NOT treat the skipped tests as failures, and do NOT "
    "report the work complete as if they had passed."
)


def _db_note(tid: str, step: str) -> tuple[bool, str]:
    """Ensure the e2e DB is up when the product requires it.

    Never blocks the graph: degrade with a note instead. If e2e is not
    configured for this product (no ``E2E_UP_SCRIPT`` on disk), return ok and
    let the normal suite run — do not skip pytest solely because an injected
    isolation port is closed (TASK-012 false final-gate escalation).
    """
    if C.DRY_RUN:
        return True, DB_OK_NOTE
    if not C.e2e_required():
        return True, (
            "E2E Postgres is not configured for this product; run the full "
            "configured test suite (no DB-gated skip)."
        )
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
    # Backstop: a full rewrite here would drop the archive pointer that the
    # condensation block appends at archive-creation time. Re-add it if a
    # verbatim debate archive exists.
    archive_path = C.DEBATES / f"DEBATE-{task_id}-full.md"
    if archive_path.exists():
        progress_path = C.FINAL / f"PROGRESS-{task_id}.md"
        pointer = f"Verbatim debate archive: DEBATE-{task_id}-full.md"
        existing = progress_path.read_text()
        if pointer not in existing:
            with progress_path.open("a") as f:
                f.write(pointer + "\n")


# --- escalation -------------------------------------------------------------


# Routers fail into escalation via ``_safe_router`` (graph.py). A router is a
# pure function of state that returns the next node name; it cannot set
# ``state["escalation"]`` (it only returns a string the graph maps to a node),
# so the plain-language reason is stashed here, keyed by task_id, for
# ``escalate()`` to pick up. Peek, not pop: a resume re-runs ``escalate()`` and
# the reason must still be present for the ``open_escalation`` marker check.
# Cross-process resume (fresh process, empty dict) falls back to the
# plain-language string below plus the ``step_error`` journal event.
_router_errors: dict[str, str] = {}


def _set_router_error(tid: str, reason: str) -> None:
    _router_errors[tid] = reason


def _get_router_error(tid: str) -> str:
    """Non-destructive peek — never pops the stored entry. ``''`` if none."""
    return _router_errors.get(tid, "")


def _canonical_key(key: str) -> str:
    """Reduce a composite option key to its single canonical answer token.

    Display keys like ``"skip / done"`` or ``"skip / force"`` advertise several
    names to the human, but the behaviour they trigger is the first token
    (``"skip"``). Passing the literal composite as the answer never matched the
    ``forced`` / ``INTAKE_END_ANSWERS`` checks below, so intake's ``skip / done``
    and the visual gate's ``skip / force`` silently failed to skip/force. Keys
    without ``" / "`` are returned unchanged (already canonical).
    """
    return str(key).split(" / ")[0].strip().lower()


def _opt(key: str, label: str, *, free_text: bool = False) -> dict:
    """Build one structured escalation option entry.

    ``key`` is the answer string passed to ``resume --answer`` (or ``"ok"`` for
    free-text wildcards, where any typed text waives). ``label`` is the
    human-readable description shown in the CLI pause and on the Discord button.
    ``free_text`` marks wildcard options that accept arbitrary typed input.
    """
    return {"key": key, "label": label, "free_text": free_text}


def _escalation_options(reason: str, *, triage: dict | None = None) -> list[dict]:
    """What the valid answers mean for THIS escalation — the payload's menu.

    Returns a list of structured option dicts (``key``/``label``/``free_text``)
    so the CLI and the Discord bot render identical labels from a single source
    of truth. 'ok' vs 'skip' do different things per escalation type; advertising
    them stops the human from rubber-stamping 'ok' without knowing what it does.

    ``triage`` is the thrashing/stuck/exhausted/requirements triage dict
    attached to a debate escalation (TASK-022). It is used only by the
    ``"debate exhausted"`` branch to rewrite the ``continue``/``ok`` labels with
    a thrashing-specific warning when the exhausted debate was actually
    churning (``triage["mode"] == "thrashing"``) — a ``mode="stuck"``
    exhausted escalation keeps the default exhausted labels (item 14).
    """
    r = reason.lower()
    if r.startswith("debate stuck:"):
        # Stuck-claim early escalation (item 13/14): the debate is not
        # converging — the same BLOCKER persists across k consecutive rounds.
        # No "skip": force-closing a batch here would rubber-stamp a plan the
        # critics keep blocking. "ok" proceeds to the verdict with the plan as
        # it stands; "continue" extends the cap; "redo" restarts from round 1;
        # "stop" ends the run (the blockers may be in the REQUIREMENTS).
        return [
            _opt("continue",
                 "extend the debate by 2 more rounds, keeping the existing "
                 "history (the proposer keeps iterating on the stuck blocker)"),
            _opt("redo",
                 "re-run the debate from round 1 on the SAME plan (use when the "
                 "debate itself derailed — it cannot fix a plan that is wrong)"),
            _opt("stop",
                 "stop the run — the stuck blocker may be in the REQUIREMENTS, "
                 "not the plan: ./run.py redo <id> --from intake so the "
                 "interviewer re-asks; then plan/debate regenerate"),
            _opt("ok",
                 "proceed to the verdict with the plan as it stands (the stuck "
                 "blocker is recorded in the report) — WARNING: implement will "
                 "not get a plan patch for this claim; only use when you accept "
                 "shipping the hole or will fix it out-of-band"),
        ]
    if r.startswith("debate thrashing:"):
        # TASK-022: thrashing early escalation — the debate is churning
        # (blockers not decreasing AND new claims appearing). Same 4 keys as
        # "debate stuck:" (continue/redo/stop/ok) with thrashing-specific
        # labels. continue raises the cap by 2 AND sets debate_grace_until so
        # stuck/thrashing early-stop cannot re-fire until those rounds run.
        # TASK-024: the "recommended" highlight keys off triage["recommended"]
        # — "ok" when the new claims are fresh surface area (or the human
        # already extended once), "continue" when a majority of the new claims
        # refine the prior round's themes. The churning WARNING text stays on
        # the continue label in both cases.
        thrashing_continue_recommended = (
            triage is not None
            and isinstance(triage, dict)
            and triage.get("recommended") == "continue"
        )
        continue_label = (
            "extend the debate by 2 more rounds (early thrashing/stuck "
            "pause suppressed for those rounds) — WARNING: the trend "
            "shows the debate is churning (blockers not decreasing, new "
            "claims appearing), so more rounds may still not converge"
        )
        if thrashing_continue_recommended:
            continue_label = (
                "extend the debate by 2 more rounds — recommended: the new "
                "blockers refine the prior round's themes, so the proposer is "
                "iterating on the same surface and more rounds may converge "
                "(WARNING: the trend shows the debate is churning — blockers "
                "not decreasing, new claims appearing)"
            )
        ok_label = (
            "proceed to the verdict with the plan as it stands (the "
            "thrashing trend is recorded in the report) — WARNING: "
            "unresolved blockers are not plan-patched before implement"
        )
        if not thrashing_continue_recommended:
            ok_label = (
                ok_label + " — recommended when the debate is churning "
                "without converging"
            )
        return [
            _opt("continue", continue_label),
            _opt("redo",
                 "re-run the debate from round 1 on the SAME plan (use when the "
                 "debate itself derailed — a fresh start may break the churn)"),
            _opt("stop",
                 "stop the run — the churning blockers may be in the "
                 "REQUIREMENTS, not the plan: ./run.py redo <id> --from intake "
                 "so the interviewer re-asks; then plan/debate regenerate"),
            _opt("ok", ok_label),
        ]
    if r.startswith("debate requirements:"):
        # Same keys as stuck/thrashing (continue/redo/stop/ok). A REQUIREMENTS
        # tag means the *recommended* action is stop + re-intake, but the
        # debate may also carry PLAN blockers that more rounds can fix — so
        # continue/redo stay available. Hint/recommended stays "stop".
        return [
            _opt("continue",
                 "extend the debate by 2 more rounds (keeps history) — useful "
                 "when PLAN blockers remain that more rounds can fix; will NOT "
                 "clear a REQUIREMENTS/brief issue by itself"),
            _opt("redo",
                 "re-run the debate from round 1 on the SAME plan (fresh critic "
                 "pass; still cannot invent a missing requirement)"),
            _opt("stop",
                 "RECOMMENDED — stop the run: the REQUIREMENTS blocker is in "
                 "the brief, not the plan. The flagged claims are saved for "
                 "re-intake — ./run.py redo <id> --from intake (interviewer "
                 "gets those gaps; do not hand-edit the brief)"),
            _opt("ok",
                 "proceed to the verdict with the plan as it stands (the "
                 "REQUIREMENTS blocker is recorded in the report) — the bonus "
                 "is cleared so a later redo starts from the default cap"),
        ]
    if "intake" in r or "interviewer" in r:
        return [
            _opt("ok", "answer the questions in the intake file, then resume"),
            _opt("skip / done", "stop interviewing; plan from the brief as it stands"),
        ]
    if "tests still failing" in r:
        return [
            _opt("ok",
                 "waive the in-graph test gate for this batch and continue",
                 free_text=True),
            _opt("skip", "force-close this batch (mark it approved) and move on"),
        ]
    if ("ux" in r or "designer" in r) and "blocker" in r:
        return [
            _opt("ok",
                 "PROCEED past the designer, shipping the UX blockers unresolved "
                 "(recorded in the report) — prefer fixing them or accepting a "
                 "verified technical limit"),
            _opt("skip", "same as ok here — proceed past the debate"),
            _opt("redo",
                 "re-run the debate from round 1, reusing the existing plan "
                 "(e.g. after fixing an agent or the UX reviewer prompt)"),
        ]
    if "debate hit the round cap" in r or "debate exhausted" in r:
        # TASK-022 item 14: rewrite the continue/ok labels with a thrashing
        # warning ONLY when triage is present AND triage["mode"] == "thrashing".
        # A mode="stuck" (or unknown/converging) exhausted escalation keeps the
        # default exhausted labels — the warning is specific to a churning
        # trend, not to every exhausted debate.
        # TASK-024: within the thrashing_exhausted branch, the "recommended"
        # highlight keys off triage["recommended"] — "continue" when a majority
        # of the new claims refine the prior round's themes (the proposer is
        # iterating, not churning onto unrelated surface), "ok" otherwise. The
        # churning WARNING text stays on the continue label in both cases.
        thrashing_exhausted = (
            triage is not None
            and isinstance(triage, dict)
            and triage.get("mode") == "thrashing"
        )
        if thrashing_exhausted:
            exhausted_continue_recommended = triage.get("recommended") == "continue"
            if exhausted_continue_recommended:
                continue_label = (
                    "extend the debate by 2 more rounds — recommended: the new "
                    "blockers refine the prior round's themes, so the proposer "
                    "is iterating on the same surface and more rounds may "
                    "converge (WARNING: the trend shows the debate is churning "
                    "— blockers not decreasing, new claims appearing)"
                )
            else:
                continue_label = (
                    "extend the debate by 2 more rounds — WARNING: the trend "
                    "shows the debate is churning (blockers not decreasing, "
                    "new claims appearing), so more rounds may not converge"
                )
            ok_label = (
                "proceed to the verdict with the plan as it stands (the "
                "thrashing trend is recorded in the report)"
            )
            if not exhausted_continue_recommended:
                ok_label = (
                    ok_label + " — recommended when the debate is churning "
                    "without converging"
                )
        else:
            continue_label = (
                "extend the debate by 2 more rounds, keeping the existing "
                "history (the proposer keeps iterating on the remaining blockers)"
            )
            ok_label = "proceed to the verdict with the plan as it stands"
        return [
            _opt("ok", ok_label),
            _opt("skip", "same — proceed past the debate"),
            _opt("continue", continue_label),
            _opt("redo",
                 "re-run the debate from round 1 on the SAME plan (use when the "
                 "debate itself derailed — it cannot fix a plan that is wrong)"),
            _opt("stop",
                 "stop the run — the blockers are in the REQUIREMENTS, not the "
                 "plan: ./run.py redo <id> --from intake so the interviewer "
                 "re-asks (no number of debate rounds can invent a missing "
                 "requirement; do not hand-edit the brief as the recovery path)"),
        ]
    # Item 5: the render predicate matches the same 4 substrings as the
    # ``render_failed`` flag in escalate() — "render the ui", "render command",
    # "no screenshots", "cannot render" — NOT "render timed out" / "could not
    # render", which are not retryable render failures.
    if ("render the ui" in r or "render command" in r
            or "no screenshots" in r or "cannot render" in r):
        return [
            _opt("ok", "re-render (after fixing the browser/stack/fixtures)"),
            _opt("skip / force", "give up on the visual gate; ship without it"),
        ]
    if "visual issues remain" in r or "visual reviewer produced no" in r:
        return [
            _opt("ok",
                 "SHIPS the remaining visual blockers to the final gate "
                 "(recorded in the report); the auto-fix cycles are spent. "
                 "To fix more: stop, edit the UI, then "
                 "./run.py redo <id> --from visual",
                 free_text=True),
        ]
    if "final test gate" in r:
        return [
            _opt("retry", "re-run the fix loop (e.g. after manual fixes)"),
            _opt("ok", "ship with the known failures (recorded in the report)"),
            _opt("stop", "stop the run — fix the failing tests manually, then restart"),
        ]
    # A crash (OOM, signal kill, daemon death) is infrastructure, not a code
    # fault — the only sane options are retry or stop. Force-closing a batch
    # on a crash would rubber-stamp work the agent never produced.
    # NB: string-matching heuristic — any future escalation reason that
    # happens to contain one of these substrings non-crash-related would be
    # mis-routed here. No collision exists today (verified).
    if "crashed" in r or "killed by signal" in r or "agent-daemon crash" in r:
        return [
            _opt("ok",
                 "retry the step (the crash was infrastructure — OOM, signal, daemon death)"),
            _opt("stop", "stop the run — fix the environment, then resume"),
        ]
    # A router crash is infrastructure, not a code fault — the only sane options
    # are retry or stop. Force-closing a batch on a routing failure would
    # rubber-stamp work the router never got to adjudicate.
    if "routing failed" in r:
        return [
            _opt("ok", "retry the routing (the failure was infrastructure — see journal)"),
            _opt("stop", "stop the run — fix the issue, then resume"),
        ]
    if "judge escalated" in r or "judge did not produce" in r or "batches json" in r:
        return [
            _opt("ok", "retry the judge (re-parse the verdict output, or re-run if needed)"),
            _opt("stop", "stop the run — inspect the verdict log, then resume or redo"),
        ]
    if "git branch mismatch" in r:
        return [
            _opt("ok",
                 "retry after checking out the expected task branch in PIPELINE_REPO "
                 "(see the escalation reason for repo path + expected name)"),
            _opt("stop", "stop the run — fix repo/branch, then resume or redo"),
        ]
    return [
        _opt("ok", "retry / continue from here"),
        _opt("skip / close / force", "force-close the current batch (approve, clear blockers)"),
    ]


def button_specs(options: list[dict]) -> list[dict]:
    """Translate structured escalation options into Discord button specs.

    Each returned spec carries the short ``key`` for the button face, the long
    ``label`` for the tooltip / embed field, and the ``answer`` string to pass
    to ``resume --answer``. Free-text wildcard options (``free_text=True``)
    collapse to ``"ok"`` — any typed text waives, and the escalate() validator
    accepts ``"ok"`` for those branches.

    This is a pure-data function with no Discord import so it can live in the
    pipeline package and be imported by ``bot/bot.py`` without pulling the
    bot's dependencies into the graph.
    """
    specs: list[dict] = []
    for opt in options:
        key = opt.get("key", "ok")
        specs.append({
            "key": key,
            "label": opt.get("label", ""),
            # Composite display keys ("skip / done", "skip / force") collapse to
            # their canonical first token so the answer sent to ``resume`` actually
            # triggers the skip/force branch in escalate() instead of falling
            # through as an unrecognized literal.
            "answer": "ok" if opt.get("free_text") else _canonical_key(key),
            "free_text": bool(opt.get("free_text", False)),
        })
    return specs


def escalate(state):
    tid = state.get("task_id", "?")
    # A router crash reaches escalate() with state["escalation"] empty (the
    # router cannot set state — it only returns "escalate"); the plain-language
    # reason is stashed by _safe_router via _set_router_error. Cross-process
    # resume (fresh process, empty bridge) falls back to the generic string.
    # Never "unknown": that gave the human a menu with no context.
    reason = (
        state.get("escalation")
        or _get_router_error(tid)
        or "routing failed — pipeline could not choose the next step (see journal for the exception)"
    )

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

    # router_error is computed here (before the interrupt) so it can be
    # included in the interrupt payload — the CLI pause renderer and the
    # Discord bot read it to decide whether to enforce the option menu.
    router_error = not state.get("escalation")
    # TASK-022 item 15: read the triage/hint a debate escalation attached to
    # state (None/"" when this is not a debate escalation or the debate is not
    # thrashing/stuck/exhausted/requirements). Both are included in the
    # interrupt payload so the CLI/bot render the triage block and the
    # recommended-answer highlight, and triage is passed into
    # _escalation_options so the "debate exhausted" branch can rewrite its
    # labels when the exhausted debate was actually churning.
    triage = state.get("triage")
    hint = state.get("hint", "")
    options = _escalation_options(reason, triage=triage)
    answer = interrupt(
        {
            "stage": "escalation",
            "task": tid,
            "reason": reason,
            "context": _context(state),
            "options": options,
            "router_error": router_error,
            "journal": state.get("journal", [])[-10:],
            "triage": triage,
            "hint": hint,
        }
    )

    ev.close_escalation(tid)
    # TASK-022 item 17 (C14): clear triage/hint unconditionally in the BASE
    # delta so every resolution branch (router/stop/continue/ok/redo/intake/
    # generic tail) wipes them. Without this, a stale thrashing recommendation
    # would silently reappear on an unrelated later pause (e.g. a UX/render
    # escalation showing a leftover "recommended: ok" button highlight). The
    # redo branch's later delta.update(...) does NOT re-add either key.
    delta = {
        "escalation": "",
        "test_fix_attempt": 0,
        "triage": None,
        "hint": "",
        # Cleared on every resolution except continue (which sets it below).
        "debate_grace_until": 0,
    }
    # Canonicalize composite answers ("skip / done" → "skip") so the behavioural
    # branches below (forced / INTAKE_END_ANSWERS / visual force) fire on the
    # first token rather than the literal composite, which never matched.
    ans = _canonical_key(str(answer).strip())
    test_escalation = "tests still failing" in reason.lower()
    intake_escalation = "intake" in reason.lower() or "interviewer" in reason.lower()
    r_low = reason.lower()
    # Item 15: the "debate stuck:" prefix boolean is computed immediately after
    # r_low is assigned, ahead of its use in any other flag below. When true,
    # it forces the substring-based flags False (item 16) so the prefix-gate
    # menu (continue/redo/stop/ok) is the only one that fires — a partial gate
    # would reproduce the Round 3 substring-collision blocker.
    # Item 34: the same prefix gate also matches "debate requirements:" — a
    # REQUIREMENTS-provenanced escalation must suppress the same substring
    # flags so only the stop/ok menu fires.
    debate_stuck = r_low.startswith("debate stuck:")
    debate_requirements = r_low.startswith("debate requirements:")
    # TASK-022 item 16: the prefix gate also matches "debate thrashing:" so a
    # thrashing early-escalation suppresses the same substring flags and only
    # the continue/redo/stop/ok menu fires (same gate as debate stuck).
    debate_thrashing = r_low.startswith("debate thrashing:")
    debate_prefix = debate_stuck or debate_requirements or debate_thrashing
    ux_escalation = (
        ("ux" in r_low or "designer" in r_low) and "blocker" in r_low
    ) and not debate_prefix
    debate_escalation = "debate" in r_low
    # A render that could not run is retryable (fix the browser/stack, re-render);
    # a visual review that still has blockers is not — it ships or it doesn't.
    render_failed = (
        "render the ui" in r_low
        or "render command" in r_low
        or "no screenshots" in r_low
        or "cannot render" in r_low
    ) and not debate_prefix
    visual_blocked = (
        "visual issues remain" in r_low or "visual reviewer produced no" in r_low
    ) and not debate_prefix
    # Item 16/34: when a debate prefix is true, force the remaining substring
    # flags False together (intake_escalation and test_escalation were computed
    # above from reason.lower() — re-gate them here so the prefix gate is total).
    if debate_prefix:
        intake_escalation = False
        test_escalation = False
    # A router crash is the only path into escalate() with state["escalation"]
    # empty (the router cannot set state — it returns "escalate" and the graph
    # routes here). Checked first so the plain-language router reason (which may
    # contain a router name like "route_intake") is not mis-routed into the
    # intake / debate / render branches below on a substring collision.
    # (router_error was computed above, before the interrupt, so it could be
    # included in the interrupt payload.)
    # Validate the answer against the known options for this escalation. A
    # fat-fingered or unrecognized answer must NOT default to "proceed" — it
    # re-opens the escalation so the human gets the menu again. Router errors
    # are exempt (any non-stop answer retries the router — there is no
    # domain-specific menu to enforce).
    if not router_error:
        valid_keys = {_canonical_key(o["key"]) for o in options}
        if options and ans not in valid_keys and ans not in (
            "stop", "no", "abort", "cancel",  # universal stop keys
        ):
            ev.emit(
                "escalation_reopened",
                tid,
                "escalate",
                f"answer {answer!r} did not match any option; re-showing menu",
            )
            return {
                "escalation": reason,
                "journal": [
                    f"escalation: answer {answer!r} not recognized — "
                    f"valid options: {', '.join(sorted(valid_keys))}"
                ],
            }
    forced = ans in ("skip", "close", "force close", "force")

    if router_error:
        # A routing failure is infrastructure. A stop-like answer ends the run;
        # any other answer clears the escalation only and lets the graph retry
        # the router. No batch-semantics keys are set — the router never got to
        # adjudicate a batch, so force-closing one would rubber-stamp nothing.
        if ans in ("stop", "no", "abort", "cancel"):
            delta["finished"] = True
            delta["journal"] = [f"escalation resolved: {answer} (run stopped after routing failure)"]
        else:
            delta["journal"] = [f"escalation resolved: {answer} (retrying after routing failure)"]
        ev.emit("escalation_resolved", tid, "escalate", f"answered {answer!r}; was: {reason}")
        return delta

    # "stop" means stop the run. Only the router / final-gate / crash branches
    # honoured it, so a debate, UX or visual escalation answered "stop" — a key
    # the validator above explicitly accepts — fell through to the generic tail
    # and PROCEEDED, the same silent-approval class as an unvalidated answer.
    # The intake interview is exempt: there "stop" is one of INTAKE_END_ANSWERS
    # and means "stop interviewing", handled by its own branch below.
    if ans in ("stop", "no", "abort", "cancel") and not intake_escalation:
        delta["finished"] = True
        delta["journal"] = [f"escalation resolved: {answer} (run stopped by human)"]
        ev.emit(
            "escalation_resolved",
            tid,
            "escalate",
            f"answered {answer!r} — run stopped; was: {reason}",
        )
        return delta

    if ans == "continue" and debate_escalation:
        # Extend the debate cap by 2 more rounds without losing the existing
        # history. Unlike "redo" (which resets to round 1 and wipes the debate
        # file), "continue" keeps all prior rounds so the proposer can iterate
        # on the remaining blockers with full context. The bonus is additive
        # and persists in state across resumes.
        #
        # Also set debate_grace_until so stuck/thrashing early-escalation cannot
        # re-fire on the very next critic pass (TASK-023: continue promised +2
        # rounds but thrashing paused again after a single tech round).
        # Requirements early-escalation is NOT suppressed — brief blockers are
        # still unfixable by more plan rounds.
        bonus = state.get("debate_round_bonus") or 0
        cur_round = int(state.get("debate_round") or 0)
        delta["debate_round_bonus"] = bonus + 2
        delta["debate_grace_until"] = cur_round + 2
        delta["escalation"] = ""
        delta["journal"] = [
            f"escalation resolved: {answer} "
            f"(extending debate by 2 rounds, cap now "
            f"{C.resolved_debate_rounds({**state, 'debate_round_bonus': bonus + 2})}; "
            f"early thrash/stuck suppressed through round {cur_round + 2})"
        ]
        ev.emit(
            "escalation_resolved",
            tid,
            "escalate",
            f"answered {answer!r} — extending debate; was: {reason}",
        )
        return delta

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
                "debate_round_bonus": 0,
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
                "test_fix_failures": [],
                "test_fix_summary": "",
                "redo_debate": True,
                # Item 18: reset the cap bonus so a redo after a prior "continue"
                # always starts from the unmodified default, regardless of which
                # escalation triggered it (debate-cap or debate-stuck).
                "debate_round_bonus": 0,
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

    final_test_escalation = "final test gate" in r_low and not debate_prefix
    crash_escalation = (
        "crashed" in r_low or "killed by signal" in r_low or "agent-daemon crash" in r_low
    ) and not debate_prefix

    if final_test_escalation:
        if ans in ("stop", "no", "abort", "cancel"):
            delta["finished"] = True
            delta["journal"] = [f"escalation resolved: {answer} (run stopped by human)"]
        elif ans in ("retry", "again", "fix"):
            delta["journal"] = [f"escalation resolved: {answer} (re-running fix loop)"]
        else:
            delta["final_tests_waived"] = True
            delta["degradations"] = ["shipped with known failing tests at the final gate"]
            delta["journal"] = [
                f"escalation resolved: {answer} (shipping with known test failures)"
            ]
    elif crash_escalation:
        # A crash is infrastructure (OOM, signal, daemon death). A stop-like
        # answer ends the run; an ok-like answer retries the step with stale
        # verdicts cleared so the retried node is not rubber-stamped. No
        # batch-force-close keys are set — the batch was never completed.
        if ans in ("stop", "no", "abort", "cancel"):
            delta["finished"] = True
            delta["journal"] = [f"escalation resolved: {answer} (run stopped after crash)"]
        else:
            delta["escalation"] = ""
            delta["code_verdict"] = None
            delta["open_blockers"] = 0
            delta["not_met"] = []
            delta["journal"] = [f"escalation resolved: {answer} (retrying after crash)"]
        ev.emit("escalation_resolved", tid, "escalate", f"answered {answer!r}; was: {reason}")
        return delta
    elif forced:
        delta["not_met"] = []
        delta["open_blockers"] = 0
        delta["code_verdict"] = "APPROVE"
        delta["test_fix_failures"] = []
        delta["test_fix_summary"] = ""
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
    elif (
        "judge escalated" in r_low
        or "judge did not produce" in r_low
        or "batches json" in r_low
    ):
        if ans in ("stop", "no", "abort", "cancel"):
            delta["finished"] = True
            delta["journal"] = [
                f"escalation resolved: {answer} (run stopped at judge gate)"
            ]
        else:
            delta["retry_judge"] = True
            delta["journal"] = [
                f"escalation resolved: {answer} (retrying judge)"
            ]
        ev.emit(
            "escalation_resolved",
            tid,
            "escalate",
            f"answered {answer!r}; was: {reason}",
        )
        return delta
    else:
        # Proceed-to-verdict answers for any debate escalation must clear a
        # leftover debate_round_bonus from an earlier "continue". Otherwise
        # route_escalation_return sees a truthy bonus and re-enters debate_tech
        # instead of summary — the 018 failure mode where "ok" after a prior
        # "continue" restarted the debate.
        # Also covers "debate requirements:" (item 35): clear bonus + redo_debate
        # so the graph routes to the verdict rather than restarting.
        if debate_escalation and ans in ("ok", "skip"):
            delta["debate_round_bonus"] = 0
            delta["redo_debate"] = False
            if debate_requirements:
                delta["journal"] = [
                    f"escalation resolved: {answer} "
                    "(proceeding to the verdict with the REQUIREMENTS blocker "
                    "recorded; debate bonus cleared)"
                ]
            else:
                delta["journal"] = [
                    f"escalation resolved: {answer} "
                    "(proceeding to the verdict with the plan as it stands; "
                    "debate bonus cleared)"
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


# --- F2/F3: line-anchored parsers and batch schema validation ----------------


# NOT_FIXED as a line-anchored status marker: either a standalone line
# (``NOT_FIXED``) or after a colon (``item 1: NOT_FIXED — reason``). A
# bare word ``NOT_FIXED`` embedded in prose (``The previous NOT_FIXED was
# resolved``) is NOT a status marker — the old ``"NOT_FIXED" in out``
# substring check false-positived on it.
_NOT_FIXED_LINE_RE = re.compile(
    r"^\s*(?:\S.*:\s*)?NOT_FIXED\s*(?:[——-]|$)", re.MULTILINE | re.IGNORECASE
)
# F3: line-anchored status markers — CONFIRMED and NOT_FIXED. Each line whose
# trimmed content is a standalone status (optionally preceded by an item ref
# and colon) is parsed. A bare word embedded in prose is NOT a marker.
_STATUS_LINE_RE = re.compile(
    r"^\s*(?:\S.*:\s*)?(CONFIRMED|NOT_FIXED)\s*(?:[——-]|$)",
    re.MULTILINE | re.IGNORECASE,
)
_DEVIATIONS_LINE_RE = re.compile(
    r"^\s*DEVIATIONS\s*:\s*(.*)$", re.MULTILINE | re.IGNORECASE
)


def parse_verify_statuses(text: str) -> list[str]:
    """F3: return the list of line-anchored status markers (CONFIRMED/NOT_FIXED)
    found in the text, in order of appearance.

    The old ``"NOT_FIXED" in out`` substring check false-positived on prose
    quoting the word (e.g. a reviewer writing "the previous NOT_FIXED was
    resolved"). Line-anchoring ensures only a real status marker — a line
    whose trimmed content is exactly ``CONFIRMED`` or ``NOT_FIXED`` — is
    returned. The caller checks ``"NOT_FIXED" in statuses`` to decide the
    retry/give-up branch in ``code_verify``.
    """
    return [m.group(1).upper() for m in _STATUS_LINE_RE.finditer(text or "")]


def parse_deviations_line(text: str) -> str:
    """F3: extract the deviations text from a line-anchored ``DEVIATIONS:`` line.

    The old ``text.split("DEVIATIONS", 1)[1]`` approach grabbed everything
    after the first occurrence of the substring — including unrelated prose
    that happened to mention the word. Line-anchoring matches only a line
    whose trimmed content starts with ``DEVIATIONS:`` and returns the text
    after the colon on that same line (trimmed, capped at 200 chars by the
    caller). Returns ``"none"`` when no ``DEVIATIONS:`` line is found.
    """
    m = _DEVIATIONS_LINE_RE.search(text or "")
    return m.group(1).strip() if m else "none"


def validate_batches_schema(raw) -> tuple[list[dict] | None, str | None]:
    """F2: validate a raw BATCHES json list and return (batches, None) on
    success or (None, escalation_message) on failure.

    Extracted from ``finalize.judge`` so the file-primary load path and the
    stdout-extraction path share one validation routine. Each element must be
    a dict with an integer ``n``; non-dict elements and missing/non-int ``n``
    produce a specific ``"malformed batch"`` escalation message. A raw value
    that is not a list, or a list with no dict elements at all (prose false
    positive), is rejected with a shape error.
    """
    if not isinstance(raw, list) or (
        raw and not any(isinstance(b, dict) for b in raw)
    ):
        return None, (
            "BATCHES json is not a list of objects — judge output may contain "
            "a prose false positive"
        )
    batches: list[dict] = []
    for b in raw:
        if not isinstance(b, dict):
            return None, (
                f"malformed batch (no n) — BATCHES item is not an object: {b!r}"
            )
        n = b.get("n")
        if not isinstance(n, int) or isinstance(n, bool):
            return None, (
                f"malformed batch (no n) — missing or non-integer n: {b!r}"
            )
        scope = b.get("scope", "")
        if not isinstance(scope, str):
            return None, (
                f"malformed batch (bad scope) — scope must be a string: {b!r}"
            )
        checklist = b.get("checklist", [])
        if not isinstance(checklist, list):
            return None, (
                f"malformed batch (bad checklist) — checklist must be a list: {b!r}"
            )
        allowlist = b.get("test_failure_allowlist", [])
        if not isinstance(allowlist, list):
            return None, (
                f"malformed batch (bad allowlist) — test_failure_allowlist must be a list: {b!r}"
            )
        for item in allowlist:
            if not isinstance(item, str):
                return None, (
                    f"malformed batch (bad allowlist) — test_failure_allowlist elements must be strings: {b!r}"
                )
        batches.append({
            "n": n,
            "scope": scope,
            "status": "PENDING",
            "outcome": "",
            "deviations": "",
            "checklist": checklist,
            "test_failure_allowlist": allowlist,
        })
    return batches, None
