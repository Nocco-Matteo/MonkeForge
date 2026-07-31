#!/usr/bin/env python3
"""CLI for the LangGraph pipeline.

  ./run.py start 004 "add CSV export to the orders dashboard" [--auto]
  ./run.py start 004 --file task.txt [--auto]
  ./run.py resume 004 [--answer "ok"]
  ./run.py reset 004        # delete checkpoint state so the task can start fresh
  ./run.py status 004
  ./run.py graph            # print the graph as mermaid
"""
from __future__ import annotations
import argparse, contextlib, json, os, shutil, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

# Load monkeforge.yaml (or .env as fallback) into os.environ.
# Priority: real env vars > yaml > .env > defaults in config.py.
_MF_ROOT = Path(__file__).resolve().parent
_yaml_file = _MF_ROOT / "monkeforge.yaml"
_env_file = _MF_ROOT / ".env"

def _envstr(val) -> str:
    """Render a YAML value for an env var. Booleans go to lowercase "true"/"false":
    Python's str(True) is "True", but CLIs that read a raw env value case-sensitively
    (gemini's GEMINI_CLI_TRUST_WORKSPACE only accepts "true") reject the capitalised
    form and silently refuse to run."""
    if isinstance(val, bool):
        return "true" if val else "false"
    return str(val)


def _load_yaml_to_env(path: Path) -> None:
    import yaml as _yaml
    data = _yaml.safe_load(path.read_text()) or {}

    # pipeline: -> PIPELINE_*
    for key, val in (data.get("pipeline") or {}).items():
        if key == "arch_docs":
            val = ";".join(val) if isinstance(val, list) else str(val)
        elif key == "dry_run":
            val = "1" if val else ""
        os.environ.setdefault(f"PIPELINE_{key.upper()}", _envstr(val))

    # effort: -> PIPELINE_EFFORT_JSON (a top-level dict is JSON-serialised; the
    # config module reads it at import time to override the effort presets).
    effort = data.get("effort")
    if isinstance(effort, dict):
        os.environ.setdefault("PIPELINE_EFFORT_JSON", json.dumps(effort))

    # agents: -> PIPELINE_MODEL_<ROLE>, PIPELINE_CMD_<ROLE>
    for role, cfg in (data.get("agents") or {}).items():
        if "model" in cfg:
            os.environ.setdefault(f"PIPELINE_MODEL_{role}", str(cfg["model"]))
        if "cmd" in cfg:
            os.environ.setdefault(f"PIPELINE_CMD_{role}", str(cfg["cmd"]))

    # tools: -> direct env var names (with explicit mapping)
    _tool_keys = {"gemini_trust_workspace": "GEMINI_CLI_TRUST_WORKSPACE"}
    for key, val in (data.get("tools") or {}).items():
        env_key = _tool_keys.get(key, key.upper())
        os.environ.setdefault(env_key, _envstr(val))

    # notifications: -> PIPELINE_NOTIFY_*
    for key, val in (data.get("notifications") or {}).items():
        os.environ.setdefault(f"PIPELINE_NOTIFY_{key.upper()}", _envstr(val))

    # discord: -> DISCORD_* (with special cases)
    _discord_keys = {"bot_autostart": "PIPELINE_BOT_AUTOSTART"}
    for key, val in (data.get("discord") or {}).items():
        env_key = _discord_keys.get(key, f"DISCORD_{key.upper()}")
        os.environ.setdefault(env_key, _envstr(val))

if _yaml_file.exists():
    _load_yaml_to_env(_yaml_file)
elif _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

from langgraph.types import Command

from pipeline_graph import config as C, events as ev
from pipeline_graph.graph import build_graph, open_checkpointer


@contextlib.contextmanager
def _sleep_inhibitor():
    """Prevent system sleep while the pipeline is running.

    Uses systemd-inhibit if available; no-op otherwise. The inhibitor is
    released when the context exits (even on crash), so the system can
    sleep again once the pipeline pauses or finishes.
    """
    proc = None
    try:
        proc = subprocess.Popen(
            ["systemd-inhibit", "--what=sleep",
             "--why=Pipeline agent running", "--mode=block",
             "sleep", "infinity"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except (OSError, FileNotFoundError):
        pass  # systemd-inhibit not available — silently skip
    try:
        yield
    finally:
        if proc:
            proc.terminate()


def _pause_reason(data: dict) -> str:
    """Return an actionable human summary for a LangGraph interrupt."""
    reason = str(data.get("reason", "")).strip()
    if reason:
        return reason
    if data.get("stage") == "effort level":
        hint = data.get("hint", "troop-monke")
        return f"choose an effort level (recommended: {hint})"
    return "waiting for a human"


def _pause_answers(data: dict) -> dict:
    """Normalize interrupt choices for logs and the Discord control bot."""
    answers = data.get("answers")
    if isinstance(answers, dict) and answers:
        return answers
    levels = data.get("levels")
    if isinstance(levels, list):
        return {str(level): "select this effort level" for level in levels}
    return {}


def _print_pause(data: dict, task_id: str) -> None:
    """Print a pause as instructions instead of an opaque JSON blob."""
    stage = str(data.get("stage", "?"))
    print(f"\n=== PAUSED: {stage} ===")
    print(f"  what to do: {_pause_reason(data)}")
    if data.get("context"):
        print(f"  context: {data['context']}")
    answers = _pause_answers(data)
    if answers:
        print("  choices:")
        for key, meaning in answers.items():
            marker = "  (recommended)" if key == data.get("hint") else ""
            print(f"    {key}{marker} — {meaning}")
    print(f"  action: ./run.py resume {task_id} --answer \"<choice>\"")


def _extract_debate_blockers(task_id: str) -> str:
    """Pull the [BLOCKER] lines from the latest round of the debate file.

    Returns a compact string for the Discord card, or "" if no debate file
    or no blockers found.
    """
    debate = C.DEBATES / f"DEBATE-{task_id}.md"
    if not debate.exists():
        return ""
    lines = debate.read_text().splitlines()
    # Find the last "## Round N — Reviewer" or "## Round N — UX" section
    # and collect [BLOCKER] lines from there until the next section header.
    blockers: list[str] = []
    in_last_section = False
    for line in lines:
        if line.startswith("## Round ") and ("Reviewer" in line or "UX" in line):
            blockers = []  # reset: start of a new review section
            in_last_section = True
            continue
        if in_last_section and line.startswith("## "):
            in_last_section = False
            continue
        if in_last_section and "[BLOCKER]" in line:
            blockers.append(line.strip())
    return "\n".join(blockers[:8])  # cap at 8 for Discord embed limits


def _thread(task_id: str) -> dict:
    return {"configurable": {"thread_id": f"task-{task_id}"},
            "recursion_limit": int(os.environ.get("PIPELINE_RECURSION_LIMIT", "200"))}


def _mark_idle(task_id: str, why: str) -> None:
    """Record that the run stopped on purpose, so status won't call it dead."""
    try:
        (C.METRICS / "current.json").write_text(json.dumps(
            {"idle": True, "why": why, "task": task_id,
             "at": datetime.now(timezone.utc).isoformat()}))
    except OSError:
        pass


def _drive(graph, task_id, payload):
    """Run until the graph ends or hits an interrupt; print what happened."""
    cfg = _thread(task_id)
    seen_updates = False
    try:
        for mode, chunk in graph.stream(payload, cfg,
                                        stream_mode=["updates", "debug"]):
            if mode == "debug" and chunk.get("type") == "task":
                node = chunk.get("name", "?")
                print(f"  [{node}] ...", flush=True)
            elif mode == "updates":
                for node, delta in chunk.items():
                    if node == "__interrupt__":
                        continue
                    seen_updates = True
                    if isinstance(delta, dict):
                        for line in delta.get("journal", []):
                            print(f"  [{node}] {line}", flush=True)
    except KeyboardInterrupt:
        ev.emit("run_stalled", task_id, "driver", "interrupted from the keyboard")
        raise
    except BrokenPipeError:
        # stdout pipe closed (parent terminal died during a background run).
        # Redirect to /dev/null so remaining prints don't re-raise, then continue
        # to the post-stream logic — the pipeline state is still valid.
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
    except Exception as exc:
        # Something outside any node — the checkpointer, the stream itself.
        # instrument() cannot see this, so it is caught and announced here.
        ev.emit("run_stalled", task_id, "driver",
                f"driver crashed: {type(exc).__name__}: {exc}")
        raise

    snap = graph.get_state(cfg)
    if not seen_updates and payload is None and snap.next:
        print(f"  (resuming — next node: {snap.next[0]})")
    if snap.interrupts:
        data = snap.interrupts[0].value
        _print_pause(data, task_id)
        # Carry the answer menu (and, for a visual escalation, where the
        # screenshots are) in the event so the optional Discord bot can build
        # one button per valid answer without querying the graph internals.
        reason = _pause_reason(data)
        answers = _pause_answers(data)
        blockers = ""
        if "debate" in reason.lower():
            blockers = _extract_debate_blockers(task_id)
        ev.emit("run_paused", task_id, str(data.get("stage", "?")), reason,
                answers=answers, hint=data.get("hint", ""),
                context=data.get("context", ""), blockers=blockers,
                screens=str(C.SCREENS / f"task-{task_id}")
                        if "screenshot" in reason.lower()
                        or "visual" in reason.lower() else "")
        _mark_idle(task_id, "paused")
    elif snap.next:
        # Neither finished nor waiting for anyone: this is a stall, and it used
        # to be reported with the same quiet one-liner as a clean pause.
        print(f"\nstopped at: {snap.next}")
        ev.emit("run_stalled", task_id, str(snap.next[0]),
                f"run stopped at {snap.next} without finishing or asking anything")
        _mark_idle(task_id, "stalled")
    else:
        print("\n=== FINISHED ===")
        _mark_idle(task_id, "finished")


def _warn_stale_task_files(task_id: str) -> None:
    """Say so when a task id is being reused over leftovers from a previous run.

    The graph already refuses to treat a stale brief as this round's output, but
    an appended interview and a half-written brief are still confusing enough to
    be worth naming before the run starts.
    """
    leftovers = [p for p in (C.TASKS / f"TASK-{task_id}-brief.md",
                             C.TASKS / f"TASK-{task_id}-intake.md")
                 if p.exists()]
    if not leftovers:
        return
    print(f"  note: task id {task_id} already has files from an earlier run:")
    for p in leftovers:
        try:
            label = p.relative_to(C.REPO)
        except ValueError:
            label = p
        print(f"    {label}")
    print("    the interview appends to the intake file and will not accept the "
          "old brief as its own; delete them first for a clean start.")


def _doctor(graph, task_id: str) -> None:
    """One-shot 'what went wrong' — reads events.jsonl instead of making the
    human dig through raw agent logs. Surfaces the three failure classes plus
    the two things that silently kill a run: a dead process and off notifications.
    """
    snap = graph.get_state(_thread(task_id))
    where = "END" if not snap.next else snap.next[0]
    if snap.interrupts:
        iv = snap.interrupts[0].value
        where = f"PAUSED at {iv.get('stage')}: {iv.get('reason', '')}"
    print(f"task {task_id} — {where}")

    # The degradation ledger: every compromise the run shipped with, in one list.
    ledger = (snap.values or {}).get("degradations", [])
    if ledger:
        print(f"\n  DEGRADATIONS SHIPPED ({len(ledger)}):")
        for d in ledger:
            print(f"    • {d}")

    events = ev.read_events(task_id)
    if not events:
        print("  no events recorded for this task")
        return

    def show(title, kinds, fmt):
        rows = [e for e in events if e.get("kind") in kinds]
        if rows:
            print(f"\n  {title} ({len(rows)}):")
            for e in rows:
                print(f"    {e.get('ts','')[11:19]}  {fmt(e)}")

    show("CRASHES", {"step_error"},
         lambda e: f"{e.get('step')}: {e.get('msg')}")
    show("UNHEALTHY AGENTS (in-band failures)", {"agent_unhealthy"},
         lambda e: f"{e.get('step')} [{e.get('health')}] {e.get('msg')}")
    # Non-ok step outcomes come from step_end's structured `outcome` field.
    bad_steps = [e for e in events
                 if e.get("kind") == "step_end" and e.get("outcome") in ("blocked", "degraded", "failed")]
    if bad_steps:
        print(f"\n  STEP OUTCOMES ({len(bad_steps)}):")
        for e in bad_steps:
            # e['msg'] already carries the [outcome] prefix (set in instrument).
            print(f"    {e.get('ts','')[11:19]}  {e.get('step'):<16} {e.get('msg')}")
    show("ESCALATIONS", {"escalation_open"},
         lambda e: e.get("msg", ""))
    show("DEGRADED", {"degraded"},
         lambda e: f"{e.get('step')}: {e.get('msg')}")

    # current.json is a single global file; only trust it here if it is this task.
    cur_path = C.METRICS / "current.json"
    if cur_path.exists():
        try:
            if json.loads(cur_path.read_text()).get("task") == task_id:
                dead = _liveness_warning()
                if dead:
                    print(f"\n  ⚠ LIVENESS: {dead}")
        except (OSError, ValueError):
            pass
    level = os.environ.get("PIPELINE_NOTIFY_LEVEL", "all").lower()
    has_webhook = bool(os.environ.get("DISCORD_WEBHOOK")) or (C.REPO / ".discord-webhook").exists()
    if level != "silent" and not has_webhook:
        print("\n  ⚠ NOTIFICATIONS OFF: no webhook — this run pushed nothing.")

    clean = not any(e.get("kind") in ("step_error", "agent_unhealthy", "escalation_open")
                    or (e.get("kind") == "step_end" and e.get("outcome") in ("blocked", "failed"))
                    for e in events)
    if clean:
        print("\n  no failures, escalations, or unhealthy agents recorded ✓")


def _ensure_bot() -> None:
    """Make the optional Discord bot a singleton that OUTLIVES this process.

    A start/resume process exits at the next pause, so the bot cannot be its
    child — it must be detached (start_new_session) or it would die exactly when
    an escalation needs relaying. Idempotent via a pidfile so repeated
    start/resume calls don't spawn duplicates. Opt-in and a no-op unless
    configured.
    """
    if os.environ.get("PIPELINE_BOT_AUTOSTART", "").strip().lower() not in ("1", "true", "yes"):
        return
    if not os.environ.get("DISCORD_BOT_TOKEN"):
        return                                  # bot not configured
    bot_py = _MF_ROOT / "bot" / "bot.py"
    if not bot_py.exists():
        return
    pidfile = C.METRICS / ".bot.pid"
    try:                                        # already running?
        os.kill(int(pidfile.read_text()), 0)
        return
    except (OSError, ValueError):
        pass
    C.METRICS.mkdir(parents=True, exist_ok=True)
    log = (C.METRICS / "bot.log").open("a")
    proc = subprocess.Popen([sys.executable, str(bot_py)], cwd=str(_MF_ROOT),
                            stdout=log, stderr=log, start_new_session=True)
    pidfile.write_text(str(proc.pid))
    print(f"  bot: launched detached (pid {proc.pid}, log {C.METRICS / 'bot.log'}")


def _ensure_notify_daemon() -> None:
    """Auto-start the notify daemon if it is not already running.

    Same detached-singleton pattern as _ensure_bot(): the daemon must outlive
    the start/resume process, so it is launched with start_new_session.
    Idempotent via the heartbeat file's pid — repeated calls don't spawn
    duplicates. No-op if no webhook is configured.
    """
    level = os.environ.get("PIPELINE_NOTIFY_LEVEL", "all").lower()
    if level == "silent":
        return
    has_webhook = bool(os.environ.get("DISCORD_WEBHOOK")) \
        or (C.REPO / ".discord-webhook").exists()
    if not has_webhook:
        return

    hb_path = C.METRICS / "notify.heartbeat"
    if hb_path.exists():
        try:
            hb = json.loads(hb_path.read_text())
            pid = hb.get("pid")
            if pid and _pid_alive(int(pid)):
                return  # already running
        except (OSError, ValueError):
            pass

    C.METRICS.mkdir(parents=True, exist_ok=True)
    log = (C.METRICS / "notify.log").open("a")
    proc = subprocess.Popen(
        [sys.executable, "-m", "pipeline_graph.notify_daemon"],
        cwd=str(_MF_ROOT),
        stdout=log, stderr=log,
        start_new_session=True,
    )
    print(f"  notify-daemon: launched detached (pid {proc.pid}, log {C.METRICS / 'notify.log'}")


def _warn_if_notifications_off() -> None:
    """Say so at startup when a run would push nothing — the failure that let a
    whole run go by with no phone alerts because .env (the webhook) was gone.

    .env is already loaded into os.environ by the time this runs.
    """
    level = os.environ.get("PIPELINE_NOTIFY_LEVEL", "all").lower()
    if level == "silent":
        return
    has_webhook = bool(os.environ.get("DISCORD_WEBHOOK")) \
        or (C.REPO / ".discord-webhook").exists()
    if not has_webhook:
        print("  ⚠ notifications OFF: no DISCORD_WEBHOOK (.env missing?) and no "
              ".discord-webhook file. This run will push nothing; logs still written.")


def _stage_refs(task_id: str, paths: list[str]) -> list[str]:
    """Copy reference documents where the interviewer can read them.

    A PDF handed to an agent as a path is a coin flip, so a text extraction is
    written alongside it when pdftotext is available. Failures are reported and
    do not stop the run: the interviewer records unreadable references under
    Unverified assumptions.
    """
    if not paths:
        return []
    dest = C.TASKS / f"TASK-{task_id}-refs"
    dest.mkdir(parents=True, exist_ok=True)
    staged: list[str] = []
    for raw in paths:
        src = Path(raw).expanduser()
        if not src.is_file():
            print(f"  warning: --ref not found, skipped: {src}")
            continue
        target = dest / src.name
        shutil.copy2(src, target)
        staged.append(src.name)
        if src.suffix.lower() == ".pdf":
            if shutil.which("pdftotext") is None:
                print(f"  warning: pdftotext not installed; {src.name} stays "
                      "a PDF and may be unreadable to the interviewer")
                continue
            txt = target.with_suffix(".txt")
            subprocess.run(["pdftotext", "-layout", str(target), str(txt)],
                           capture_output=True)
            if txt.exists():
                staged.append(txt.name)
    return staged


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # exists, owned by someone else — still alive
    return True


def _liveness_warning() -> str | None:
    """If current.json points at a running step whose process is gone, say so.

    This is what turns a silent death — suspend, closed tmux, OOM — into a
    visible one: a live 40-minute step and a dead run used to look identical.
    """
    path = C.METRICS / "current.json"
    if not path.exists():
        return None
    try:
        cur = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if cur.get("idle"):
        return None
    pid, step = cur.get("pid"), cur.get("step", "?")
    beat = cur.get("heartbeat", cur.get("started", "?"))
    if pid and not _pid_alive(int(pid)):
        return (f"run process (pid {pid}) is NOT running but current.json still "
                f"shows step '{step}' active (last heartbeat {beat}). The run "
                "died mid-step — resume it.")
    return None


def _metrics(args) -> int:
    """`./run.py metrics <ID> | --all`: aggregate events.jsonl into a report.

    Reads events via `events.read_events` / `events.read_all_events`, renders
    with `pipeline_graph.metrics`, prints to stdout, then writes the rendered
    text to `C.METRICS / "report-<id>.md"` (or `summary.md` for `--all`). A
    write failure prints the failing path to stderr and returns 1 — but only
    AFTER the report has already been printed, so the user always sees it.
    """
    from pipeline_graph import metrics as M

    # Validate arguments before touching the log: a fresh checkout has no
    # events.jsonl, so checking "empty log" first would mask the usage error
    # (FINAL-001 ruling 4).
    if not args.all and args.task_id is None:
        print("usage: ./run.py metrics <task_id> | --all")
        print("error: provide a task id or --all")
        return 2

    if args.all:
        events = ev.read_all_events()
        if not events:
            print("no metrics recorded yet")
            return 0
        by_task = M.group_by_task(events)
        metrics_by_task = {tid: M.aggregate_task(evts, task_id=tid)
                           for tid, evts in by_task.items()}
        rendered = M.render_summary(metrics_by_task)
        print(rendered)
        out_path = C.METRICS / "summary.md"
        try:
            C.METRICS.mkdir(parents=True, exist_ok=True)
            out_path.write_text(rendered)
        except OSError as exc:
            print(f"error: could not write {out_path}: {exc}", file=sys.stderr)
            return 1
        return 0

    events = ev.read_events(args.task_id)
    if not events:
        print("no metrics recorded yet")
        return 0
    m = M.aggregate_task(events, task_id=args.task_id)
    rendered = M.render_task_report(m)
    print(rendered)
    out_path = C.METRICS / f"report-{args.task_id}.md"
    try:
        C.METRICS.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered)
    except OSError as exc:
        print(f"error: could not write {out_path}: {exc}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start"); s.add_argument("task_id")
    s.add_argument("request", nargs="?", default=None)
    s.add_argument("--file", dest="file", default=None, help="read request from a file")
    s.add_argument("--auto", action="store_true")
    s.add_argument("--interview", action="store_true",
                   help="run the intake interview even under --auto")
    s.add_argument("--ref", dest="refs", action="append", default=[],
                   metavar="PATH",
                   help="reference document for the interviewer (repeatable)")
    s.add_argument("--effort", dest="effort", default=None,
                   choices=["scout-monke", "troop-monke", "barrel-monke"],
                   help="force an effort level (skips the effort checkpoint)")
    r = sub.add_parser("resume"); r.add_argument("task_id"); r.add_argument("--answer", default="ok")
    rd = sub.add_parser("redo", help="re-run a phase reusing existing artifacts "
                                     "(e.g. redo the debate after fixing an agent)")
    rd.add_argument("task_id")
    rd.add_argument("--from", dest="from_phase",
                    choices=["plan", "debate", "visual"], default="debate",
                    help="debate: reuse brief+plan, redo the debate. "
                         "plan: reuse brief, redo plan then debate. "
                         "visual: reuse the built UI, redo the render+visual gate.")
    rd.add_argument("--effort", dest="effort", default=None,
                    choices=["scout-monke", "troop-monke", "barrel-monke"],
                    help="force an effort level for the redo (plan/debate only)")
    rs = sub.add_parser("reset", help="delete the checkpoint state for a task "
                                     "so it can be started fresh again")
    rs.add_argument("task_id")
    st = sub.add_parser("status"); st.add_argument("task_id")
    dr = sub.add_parser("doctor", help="what went wrong: failures, degradations, "
                                       "unhealthy agents, notification & liveness")
    dr.add_argument("task_id")
    sub.add_parser("graph")
    nd = sub.add_parser("notify-daemon", help="persistent notification daemon "
                                      "(priority queue + rate limiter)")
    nd.add_argument("--status", action="store_true", help="check daemon heartbeat + queue")
    nd.add_argument("--stop", action="store_true", help="send SIGTERM for clean shutdown")
    mt = sub.add_parser("metrics", help="aggregate events.jsonl into a metrics report "
                                        "(durations, failures, retries, escalations)")
    mt.add_argument("task_id", nargs="?", default=None,
                    help="task id to report on; omit with --all for a cross-task summary")
    mt.add_argument("--all", dest="all", action="store_true",
                    help="aggregate every task into a single summary")
    args = p.parse_args()

    if args.cmd == "notify-daemon":
        from pipeline_graph import notify_daemon as nd_mod
        if args.stop:
            return nd_mod.stop()
        if args.status:
            return nd_mod.status()
        return nd_mod.run_daemon()

    if args.cmd == "metrics":
        return _metrics(args)

    problems = C.preflight()
    if problems and args.cmd in ("start", "resume"):
        print("preflight failed:")
        for x in problems:
            print(" -", x)
        return 2

    if args.cmd in ("start", "resume", "redo"):
        _warn_if_notifications_off()
        _ensure_notify_daemon()
        _ensure_bot()

    if args.cmd == "reset":
        import sqlite3 as _sqlite3
        thread = f"task-{args.task_id}"
        if not C.CHECKPOINT_DB.exists():
            print(f"no checkpoint DB found at {C.CHECKPOINT_DB}")
            return 0
        conn = _sqlite3.connect(str(C.CHECKPOINT_DB))
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (thread,))
            count = cur.fetchone()[0]
            if count == 0:
                print(f"task {args.task_id}: no checkpoint state found")
                return 0
            conn.execute("DELETE FROM writes WHERE thread_id = ?", (thread,))
            conn.execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread,))
            conn.commit()
            print(f"task {args.task_id}: deleted {count} checkpoint(s) — "
                  "ready for a fresh start")
        finally:
            conn.close()
        return 0

    if args.cmd == "graph":
        print(build_graph().get_graph().draw_mermaid())
        return 0

    inhibit = _sleep_inhibitor() if args.cmd in ("start", "resume", "redo") \
        else contextlib.nullcontext()
    with inhibit, open_checkpointer() as cp:
        graph = build_graph(cp)
        cfg = _thread(args.task_id)

        if args.cmd == "status":
            snap = graph.get_state(cfg)
            if not snap.created_at:
                print("no run found for this task")
                return 0
            v = snap.values
            print(f"task {args.task_id} — next: {snap.next or 'END'}")
            for b in v.get("batches", []):
                print(f"  batch {b['n']}: {b['status']:<8} {b['scope']}")
            if snap.interrupts:
                iv = snap.interrupts[0].value
                print(f"  PAUSED at {iv.get('stage')}: {iv.get('reason', '')}")
            dead = _liveness_warning()
            if dead:
                print(f"  ⚠ {dead}")
            # The live journal, not the checkpointed one: state only updates
            # when a node returns, so during a 40-minute step the checkpointed
            # journal shows the step before this one.
            live = ev.read_journal(args.task_id, 15)
            if live:
                print(f"  live log ({ev.PIPELINE_LOG}):")
                for line in live:
                    print("   ", line)
            else:
                print("  journal:")
                for line in v.get("journal", [])[-10:]:
                    print("   ", line)
            return 0

        if args.cmd == "doctor":
            _doctor(graph, args.task_id)
            return 0

        if args.cmd == "redo":
            snap = graph.get_state(cfg)
            if not snap.created_at:
                print("no run found for this task")
                return 0
            # Reposition the graph as if the upstream node had just produced this
            # state, so it re-enters the chosen phase reusing existing artifacts.
            if args.from_phase == "visual":
                # Reuse the built + committed UI; redo only render + visual gate.
                reset = {"escalation": "", "ux_render_cycle": 0,
                         "visual_verdict": "", "visual_blockers": 0,
                         "render_facts": "{}", "visual_shipped_blocked": False}
                as_node, nxt = "close_batch", "ux_render"
                reuse = "the built UI"
            else:
                reset = {"escalation": "", "debate_round": 0,
                         "reviewer_verdict": "", "open_blockers": 0,
                         "ux_verdict": "", "ux_blockers": 0,
                         "tech_limits": [], "debate_next": "",
                         "ux_shipped_blocked": False,
                         # Re-planning/re-debating invalidates the judge's
                         # decomposition, so drop the old batches: summary→judge
                         # must regenerate FINAL/BATCHES from the fresh plan.
                         # Without this the stale batches survive, and on a debate
                         # escalation resolved with "ok" route_escalation_return
                         # sees batches present, assumes the plan is already
                         # judged, and skips summary+judge straight to implement —
                         # running the new plan against the PREVIOUS run's FINAL.
                         "batches": [], "batch_idx": 0, "code_verdict": "",
                         "fix_cycle": 0, "test_fix_attempt": 0}
                if args.from_phase == "plan":
                    reset["intake_done"] = True
                    as_node, nxt, reuse = "init", "plan", "the brief"
                else:
                    as_node, nxt, reuse = "plan", "debate_tech", "the brief and plan"
                # --effort forces a level for the redo (plan/debate only); the
                # visual redo reuses the already-built UI and must not touch
                # effort state (C28).
                if args.effort:
                    reset["effort"] = args.effort
                    reset["effort_forced"] = True
                # Clear debate artifacts so the fresh debate does not read the
                # rounds of the run being redone (the plan is preserved).
                for path in (C.DEBATES / f"DEBATE-{args.task_id}.md",
                             C.REVIEWS / f"UX-{args.task_id}.md"):
                    path.unlink(missing_ok=True)
            graph.update_state(cfg, reset, as_node=as_node)
            ev.emit("run_start", args.task_id, "redo",
                    f"re-running from {args.from_phase} (reusing {reuse})")
            print(f"  re-entering at {nxt}, reusing {reuse}")
            payload = None
            _drive(graph, args.task_id, payload)
            return 0

        if args.cmd == "start":
            request = args.request
            if args.file:
                with open(args.file, "r", encoding="utf-8") as f:
                    request = f.read().strip()
            if not request:
                print("error: provide a request as argument or via --file <path>")
                return 2
            _warn_stale_task_files(args.task_id)
            staged = _stage_refs(args.task_id, args.refs)
            payload = {"task_id": args.task_id, "request": request,
                       "auto": args.auto, "interview": args.interview,
                       "journal": []}
            if args.effort:
                payload["effort"] = args.effort
                payload["effort_forced"] = True
            mode = "auto" if args.auto else "interactive"
            if args.interview:
                mode += "+interview"
            ev.emit("run_start", args.task_id, "start",
                    f"{mode}: {request[:160]}"
                    + (f" [refs: {', '.join(staged)}]" if staged else ""))
        else:
            cfg = _thread(args.task_id)
            snap = graph.get_state(cfg)
            if snap.interrupts:
                iv = snap.interrupts[0].value
                payload = Command(resume=args.answer)
                ev.emit("run_start", args.task_id, "resume",
                        f"answering {args.answer!r} to {iv.get('stage', '?')}: "
                        f"{iv.get('reason', '')}")
            else:
                payload = None
                ev.emit("run_start", args.task_id, "resume",
                        f"no open question; continuing at "
                        f"{snap.next[0] if snap.next else 'END'}")

        _drive(graph, args.task_id, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
