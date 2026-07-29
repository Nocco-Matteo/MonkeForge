"""Agent invocation: subprocess, metrics logging, verdict parsing.

No Bash-tool ceiling here: a batch may take 40 minutes and we simply wait.
"""
from __future__ import annotations
import json, os, re, subprocess, time
from datetime import datetime, timezone
from pathlib import Path

from . import config as C, events as ev

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


def classify_output(code: int, output: str) -> tuple[str, str]:
    """Return ('ok' | 'transient' | 'hard', matched_signal).

    Order matters: a transient network/rate signal is worth a retry; a fatal
    model/tool signal is not; a non-zero exit or near-empty output is hard.
    """
    low = (output or "").lower()
    for sig in TRANSIENT_SIGNATURES:
        if sig in low:
            return "transient", sig
    for sig in FATAL_SIGNATURES:
        if sig in low:
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


def render_prompt(template: str, **kw) -> str:
    """Load prompts/<template>.md and substitute {placeholders}."""
    text = (C.TEMPLATES / f"{template}.md").read_text()
    for key, value in kw.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def run_agent(role: str, task_id: str, step: str, prompt: str,
              timeout: int | None = None) -> tuple[int, str]:
    """Dispatch a prompt to the agent bound to `role`. Returns (exit_code, output).

    Output is streamed to the log file line by line while the agent works, and
    docs/metrics/current.json is kept up to date, so watch-pipeline.sh,
    pipeline-status.sh and diagnose.sh all keep working live.
    """
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
            cmd = C.role_cmd(role, prompt_file, prompt)
            chunks: list[str] = []
            last_beat = time.time()
            with out_file.open("w") as sink:
                proc = subprocess.Popen(cmd, cwd=C.REPO, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, bufsize=1)
                assert proc.stdout is not None
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
    return len(re.findall(r"\[BLOCKER\]", text or ""))


def parse_not_met(text: str) -> list[str]:
    """Checklist lines shaped '<n>: NOT MET — reason'."""
    return re.findall(r"^\s*(\d+)\s*:\s*NOT MET\b", text or "", re.MULTILINE)


def parse_disputed(text: str) -> list[str]:
    return re.findall(r"^\s*(?:item\s*)?(\S+)\s*:?\s*DISPUTED\b", text or "",
                      re.MULTILINE | re.IGNORECASE)


def read_if_exists(path: Path) -> str:
    return path.read_text() if path.exists() else ""
