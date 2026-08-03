"""Agent invocation: subprocess, metrics logging, verdict parsing.

No Bash-tool ceiling here: a batch may take 40 minutes and we simply wait.
"""
from __future__ import annotations
import dataclasses
import json, os, re, subprocess, time
from datetime import datetime, timezone
from pathlib import Path

from . import config as C, events as ev
from .state import Conversation

HEARTBEAT_EVERY_S = int(os.environ.get("PIPELINE_HEARTBEAT_INTERVAL_S", "10"))

# These agents fail IN-BAND: they exit 0 and put the error in stdout (gemini's
# "empty response or malformed tool call" is the recurring one). Exit code is
# useless as a health signal, so we read the output text instead.
MIN_OUTPUT_BYTES = int(os.environ.get("PIPELINE_MIN_OUTPUT_BYTES", "40"))
MAX_TRANSIENT_RETRIES = int(os.environ.get("PIPELINE_AGENT_TRANSIENT_RETRIES", "1"))
TRANSIENT_BACKOFF_S = int(os.environ.get("PIPELINE_AGENT_BACKOFF_S", "8"))

# Infrastructure hiccups worth an automatic retry with backoff.
TRANSIENT_SIGNATURES = (
    "rate limit", "rate_limit", "429 too many", "overloaded",
    "econnrefused", "connection refused", "503 service", "502 bad gateway",
    "temporarily unavailable", "service unavailable",
)
# Non-transient: retrying the same prompt won't help; flag and let the node deal.
# Includes exhausted usage/quota — an immediate retry just burns more of nothing
# (cursor-agent's "You're out of usage. Increase limits" is this, at exit 0).
FATAL_SIGNATURES = (
    "malformed tool call", "empty response", "invalid stream:",
    "maximum context length", "context_length_exceeded",
    "out of usage", "increase limits", "usage limit", "quota exceeded",
    "actionrequirederror",
)
# Terminal markers that are a complete, valid answer despite being far below
# MIN_OUTPUT_BYTES: the intake interviewer writes the brief to a file and prints
# only "INTAKE: COMPLETE"; a reviewer whose false-positive filter deletes every
# item prints only "VERDICT: APPROVE". These are the exact outputs the prompts
# mandate, so the length heuristic must not read them as a failure.
TERMINAL_MARKERS = frozenset({
    "INTAKE: COMPLETE",
    "INTAKE: QUESTIONS",
    "VERDICT: APPROVE",
    "VERDICT: REJECT",
    "VERDICT: APPROVE_WITH_CHANGES",
})


def classify_output(code: int, output: str) -> tuple[str, str]:
    """Return ('ok' | 'transient' | 'hard', matched_signal).

    Order matters: a transient network/rate signal is worth a retry; a fatal
    model/tool signal is not; a non-zero exit or near-empty output is hard.

    Fatal signatures are matched only in the first ~2KB of output: real CLI
    errors are short and print the error at the top, while a 46KB plan that
    *cites* "out of usage" as an example in an edge-case section must not be
    flagged as a hard failure.
    """
    low = (output or "").lower()
    for sig in TRANSIENT_SIGNATURES:
        if sig in low:
            return "transient", sig
    # Fatal signatures: only scan the head of the output. A real CLI error
    # (quota exhausted, malformed tool call, context overflow) is short and
    # prints at the top; a long agent output that merely quotes one of these
    # strings as an example is not a failure.
    head = low[:2048]
    for sig in FATAL_SIGNATURES:
        if sig in head:
            return "hard", sig
    # A negative exit code is Popen's convention for "killed by signal -code":
    # SIGPIPE (-13) when the agent daemon dies mid-call, SIGKILL (-9) on OOM,
    # SIGTERM (-15) on a suspend. The agent never failed on its own merits — the
    # environment killed it — so it is an infrastructure hiccup worth a backed-off
    # retry, not a "hard" give-up. (Checked after the output signatures so a
    # daemon that still printed a fatal message is classified by that message.)
    if code < 0:
        return "transient", f"killed by signal {-code}"
    if code != 0:
        return "hard", f"exit {code}"
    # Checked before the length heuristic (and after the exit-code checks, so a
    # marker printed by a failing process is still hard): a bare terminal marker
    # is a complete answer, not a near-empty one. Without this the health signal
    # is wrong at the source and every clean intake completion or filtered-clean
    # approval emits a spurious `agent_unhealthy` event on a correct run.
    if (output or "").strip().upper() in TERMINAL_MARKERS:
        return "ok", ""
    if len((output or "").strip()) < MIN_OUTPUT_BYTES:
        return "hard", f"near-empty output ({len((output or '').strip())}b)"
    return "ok", ""


def _error_snippet(output: str, limit: int = 200) -> str:
    """The most error-looking line of the output, for the event message."""
    for line in (output or "").splitlines():
        low = line.lower()
        if any(s in low for s in ("error", "malformed", "rate", "quota",
                                  "refused", "unavailable", "invalid")):
            return line.strip()[:limit]
    return (output or "").strip().replace("\n", " ")[:limit]


def _log(event: dict) -> None:
    C.METRICS.mkdir(parents=True, exist_ok=True)
    with C.RUNS_LOG.open("a") as f:
        f.write(json.dumps(event) + "\n")


def _write_current(payload: dict) -> None:
    """Persist the run's live position, always stamped with pid + heartbeat.

    The pid is the run process itself: `status` can then tell a live long step
    from a dead run (killed by suspend, a closed tmux, OOM) — both used to look
    identical, a frozen current.json. `idle` payloads mark clean stops.
    """
    C.METRICS.mkdir(parents=True, exist_ok=True)
    stamped = {"pid": os.getpid(),
               "heartbeat": datetime.now(timezone.utc).isoformat(), **payload}
    (C.METRICS / "current.json").write_text(json.dumps(stamped))


def _fmt(value) -> str:
    """Render a substitution value as clean text.

    Sequence-typed values (`list`/`tuple`) are newline-joined so a future
    `{journal}` (or any other sequence attribute) renders as clean text rather
    than Python repr (`('a', 'b')`) — P1 template safety. `dataclasses.asdict`
    converts `Conversation.journal`'s `tuple` into a plain `list`, so both
    container types are handled here.
    """
    if isinstance(value, (list, tuple)):
        return "\n".join(map(str, value))
    return str(value)


def _ensure_progress_archive_pointer(task_id: str, archive_path: Path) -> None:
    """Append the verbatim-archive pointer to PROGRESS-{task_id}.md if missing.

    Called at archive-creation time (inside the condensation block) so a crash
    between condensation and the next ``_write_progress`` call still leaves the
    pointer in place. ``_write_progress`` is the backstop for a full rewrite.
    """
    progress_path = C.FINAL / f"PROGRESS-{task_id}.md"
    if not progress_path.exists():
        return
    pointer = f"Verbatim debate archive: DEBATE-{task_id}-full.md"
    existing = progress_path.read_text()
    if pointer not in existing:
        with progress_path.open("a") as f:
            f.write(pointer + "\n")


def render_prompt(template: str, conversation: "Conversation", **kw) -> str:
    """Load prompts/<template>.md and substitute {placeholders}.

    The substitution map is `dataclasses.asdict(conversation)` flat-mapped
    alongside `kw` (per-node dynamic vars like `round`, `batch_n`...); `kw`
    wins on key collision (e.g. the condenser overrides `debate_history` with
    the condensed version). This keeps the existing `{task_id}`, `{request}`,
    `{docs_dir}` etc. placeholders working without rewriting the 16 templates.
    """
    text = (C.TEMPLATES / f"{template}.md").read_text()
    subs = dataclasses.asdict(conversation)
    subs.update(kw)
    # Single-pass re.sub: replacement strings returned by the callable are NOT
    # re-scanned, so no substituted value (plan, ledger, or any other) can
    # cascade into a remaining placeholder (C6 — verbatim-plan invariant).
    return re.sub(
        r"\{(\w+)\}",
        lambda m: _fmt(subs[m.group(1)]) if m.group(1) in subs else m.group(0),
        text,
    )


def run_agent(role: str, conversation: "Conversation", step: str,
              template: str | None = None, timeout: int | None = None,
              **extra_kw) -> tuple[int, str]:
    """Dispatch a prompt to the agent bound to `role`. Returns (exit_code, output).

    The prompt is rendered internally from `template` (defaulting to `step`)
    plus `extra_kw` against the frozen `conversation` snapshot; `task_id` is
    read from `conversation.task_id`, not passed separately.

    Output is streamed to the log file line by line while the agent works, and
    docs/metrics/current.json is kept up to date, so watch-pipeline.sh,
    pipeline-status.sh and diagnose.sh all keep working live.
    """
    task_id = conversation.task_id
    tpl = template or step

    # Token-budget condenser: collapse older debate rounds BEFORE rendering
    # the prompt, so the agent receives the condensed debate inline. The
    # Conversation is frozen, so the condensed string is passed as an
    # extra_kw override to render_prompt (kw wins over conversation fields).
    # No-op unless a budget is set for the role (default backward-compatible).
    # Function-local import per D2: condenser.py does `from .agents import
    # ...` at top, so a top-level import here would create an import-time cycle.
    from .condenser import condense, estimate_tokens
    budget = C.token_budget(role)
    debate_history = conversation.debate_history
    if budget is not None and debate_history:
        if estimate_tokens(debate_history) > budget:
            condensed = condense(debate_history, C.CONDENSER_KEEP_RECENT)
            # Guard against the steady-state where the debate has already
            # stabilised (cannot be collapsed further without losing the
            # verbatim guarantee): skip the write+emit so we don't churn
            # identical bytes or spam duplicate `degraded` records.
            if condensed != debate_history:
                debate_path = C.DEBATES / f"DEBATE-{task_id}.md"
                archive_path = C.DEBATES / f"DEBATE-{task_id}-full.md"
                # Snapshot the verbatim pre-condensation working file into an
                # accumulating archive BEFORE overwriting it, so a late or
                # failed condensation never loses the original debate text.
                # First condensation writes the archive verbatim; later ones
                # append with a UTC-timestamped snapshot header.
                pre_condensation = (
                    debate_path.read_text() if debate_path.exists()
                    else conversation.debate_history
                )
                if archive_path.exists():
                    snapshot_header = (
                        f"\n\n=== pre-condensation snapshot at "
                        f"{datetime.now(timezone.utc).isoformat()} UTC ===\n\n"
                    )
                    with archive_path.open("a") as af:
                        af.write(snapshot_header + pre_condensation)
                else:
                    archive_path.write_text(pre_condensation)
                # Refresh the PROGRESS pointer at archive-creation time, so a
                # crash between here and the next _write_progress call still
                # leaves the pointer in place.
                _ensure_progress_archive_pointer(task_id, archive_path)
                debate_history = condensed
                extra_kw["debate_history"] = condensed
                # Write back so future from_state reads the condensed version.
                if debate_path.exists():
                    debate_path.write_text(condensed)
                ev.emit("degraded", task_id, step,
                        f"condensed debate_history for {role}: "
                        f"{estimate_tokens(conversation.debate_history)} -> "
                        f"{estimate_tokens(condensed)} est-tokens (budget {budget}); "
                        f"verbatim archive at DEBATE-{task_id}-full.md",
                        original_size=len(conversation.debate_history),
                        condensed_size=len(condensed),
                        role=role)

    prompt = render_prompt(tpl, conversation, **extra_kw)

    # One debug snapshot per agent invocation (D6): the single choke point
    # already used for agent_start/agent_end, so every invocation logs exactly
    # one snapshot without 16 node edits duplicating the logic.
    ev.emit(
        "conversation_snapshot",
        task_id,
        step,
        "conversation built",
        brief_len=len(conversation.brief),
        plan_len=len(conversation.plan),
        debate_len=len(debate_history),
        review_len=len(conversation.review_history),
        final_len=len(conversation.final),
        progress_len=len(conversation.progress),
        summary_len=len(conversation.summary),
        visual_review_len=len(conversation.visual_review),
        journal_entries=len(conversation.journal),
    )

    # Label logs and filenames by the model, not the command's first token:
    # `stdbuf -oL devin …` used to log as "stdbuf" for four different roles.
    binary = C.role_label(role)
    cfg = C.ROLE_CONFIG[role]
    C.ensure_dirs()

    prompt_file = C.PROMPTS / f"{task_id}-{step}.md"
    prompt_file.write_text(prompt)

    out_file = C.RAW / f"{task_id}-{step}-{binary}-{int(time.time())}.log"
    started = datetime.now(timezone.utc).isoformat()
    t0 = time.time()

    _log({"ts": started, "event": "start", "task": task_id, "step": step,
          "agent": binary, "role": role, "output_file": str(out_file)})
    _write_current({"task": task_id, "step": step, "agent": binary, "role": role,
                    "started": started, "output_file": str(out_file)})
    ev.emit("agent_start", task_id, step, f"{role} -> {binary}; log: {out_file.name}",
            agent=binary, role=role, output_file=str(out_file))

    def _run_once() -> tuple[int, str]:
        """One invocation. A process-level crash re-raises (instrument turns it
        into a blocked escalation); an in-band failure returns normally."""
        try:
            if C.DRY_RUN:
                out = f"[DRY RUN] {role}/{binary} would run step {step}\nVERDICT: APPROVE\n"
                out_file.write_text(out)
                time.sleep(0.05)
                return 0, out
            cmd, stdin_text = C.role_cmd_with_stdin(role, prompt_file, prompt)
            chunks: list[str] = []
            last_beat = time.time()
            with out_file.open("w") as sink:
                proc = subprocess.Popen(cmd, cwd=C.REPO, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, bufsize=1,
                                        stdin=subprocess.PIPE if stdin_text else subprocess.DEVNULL)
                assert proc.stdout is not None
                if stdin_text:
                    proc.stdin.write(stdin_text)
                    proc.stdin.close()
                for line in proc.stdout:          # streams live to disk
                    sink.write(line); sink.flush(); chunks.append(line)
                    if time.time() - last_beat >= HEARTBEAT_EVERY_S:
                        _write_current({"task": task_id, "step": step, "agent": binary,
                                        "role": role, "started": started,
                                        "output_file": str(out_file)})
                        last_beat = time.time()
                rc = proc.wait(timeout=timeout if timeout is not None else C.AGENT_TIMEOUT)
            return rc, "".join(chunks)
        except BaseException as exc:
            ev.emit("step_error", task_id, step,
                    f"agent {binary} crashed: {type(exc).__name__}: {exc}",
                    agent=binary, role=role, output_file=str(out_file))
            raise

    try:
        for attempt in range(MAX_TRANSIENT_RETRIES + 1):
            code, output = _run_once()
            health, signal = classify_output(code, output)
            if health == "transient" and attempt < MAX_TRANSIENT_RETRIES:
                ev.emit("agent_unhealthy", task_id, step,
                        f"{binary}: transient failure ({signal}) — retry "
                        f"{attempt + 1}/{MAX_TRANSIENT_RETRIES} in {TRANSIENT_BACKOFF_S}s",
                        agent=binary, role=role, health="transient", signal=signal,
                        notify=False)
                time.sleep(TRANSIENT_BACKOFF_S)
                continue
            break
    finally:
        # The step is still the graph's current position until the node returns;
        # blanking it here is what made long non-agent work look stalled.
        _write_current({"task": task_id, "step": step, "agent": binary, "role": role,
                        "started": started, "phase": "agent done",
                        "output_file": str(out_file)})

    duration_ms = int((time.time() - t0) * 1000)
    _log({"ts": datetime.now(timezone.utc).isoformat(), "event": "end",
          "task": task_id, "step": step, "agent": binary, "role": role,
          "duration_ms": duration_ms, "exit_code": code, "health": health,
          "output_bytes": len(output), "output_file": str(out_file)})
    ev.emit("agent_end", task_id, step,
            f"{role}/{binary} exit={code} in {duration_ms // 1000}s, "
            f"{len(output)} bytes, health={health}",
            agent=binary, role=role, exit_code=code, duration_ms=duration_ms,
            health=health)

    if health != "ok":
        # The real failure, surfaced in pipeline.log with the actual error line —
        # not buried in the raw log where nobody looks until it is too late.
        ev.emit("agent_unhealthy", task_id, step,
                f"{binary} produced {health} output ({signal}): "
                f"{_error_snippet(output)}",
                agent=binary, role=role, health=health, signal=signal,
                output_file=str(out_file))

    return code, output


# --- parsing helpers -------------------------------------------------------

VERDICT_RE = re.compile(r"^\s*(?:[#*\s]*\s*)?VERDICT:\s*\**\s*(APPROVE_WITH_CHANGES|APPROVE|REJECT)",
                        re.MULTILINE)


def parse_verdict(text: str) -> str:
    matches = VERDICT_RE.findall(text or "")
    return matches[-1] if matches else "UNKNOWN"


def count_blockers(text: str) -> int:
    # Item 26: match the three tag forms — bare [BLOCKER], [BLOCKER:PLAN],
    # [BLOCKER:REQUIREMENTS] — so a provenance-tagged blocker counts the same
    # as a bare one.
    return len(re.findall(r"\[BLOCKER(?::(?:PLAN|REQUIREMENTS))?\]", text or ""))


def parse_not_met(text: str) -> list[str]:
    """Checklist lines shaped '<n>: NOT MET — reason'."""
    return re.findall(r"^\s*(\d+)\s*:\s*NOT MET\b", text or "", re.MULTILINE)


def parse_disputed(text: str) -> list[str]:
    return re.findall(r"^\s*(?:item\s*)?(\S+)\s*:?\s*DISPUTED\b", text or "",
                      re.MULTILINE | re.IGNORECASE)


def read_if_exists(path: Path) -> str:
    return path.read_text() if path.exists() else ""
