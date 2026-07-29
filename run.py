#!/usr/bin/env python3
"""CLI for the LangGraph pipeline.

  ./run.py start 004 "add CSV export to the orders dashboard" [--auto]
  ./run.py start 004 --file task.txt [--auto]
  ./run.py resume 004 [--answer "ok"]
  ./run.py status 004
  ./run.py graph            # print the graph as mermaid
"""
from __future__ import annotations
import argparse, contextlib, json, os, shutil, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

# Load lg/.env into os.environ so CLI runs match LangGraph Studio behaviour.
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
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


def _extract_debate_blockers(task_id: str) -> str:
    """Pull the [BLOCKER] lines from the latest round of the debate file.

    Returns a compact string for the Discord card, or "" if no debate file
    or no blockers found.
    """
    debate = C.REPO / "docs" / "debates" / f"DEBATE-{task_id}.md"
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
        print("\n=== PAUSED ===")
        print(json.dumps(data, indent=2, ensure_ascii=False))
        print(f"\nresume with:  ./run.py resume {task_id} --answer \"<your answer>\"")
        # Carry the answer menu (and, for a visual escalation, where the
        # screenshots are) in the event so the optional Discord bot can build
        # one button per valid answer without querying the graph internals.
        blockers = ""
        if "debate" in str(data.get("reason", "")).lower():
            blockers = _extract_debate_blockers(task_id)
        ev.emit("run_paused", task_id, str(data.get("stage", "?")),
                str(data.get("reason", "waiting for a human")),
                answers=data.get("answers", {}), context=data.get("context", ""),
                blockers=blockers,
                screens=f"docs/reviews/screens/task-{task_id}"
                        if "screenshot" in str(data.get("reason", "")).lower()
                        or "visual" in str(data.get("reason", "")).lower() else "")
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
        print(f"    {p.relative_to(C.REPO)}")
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
    bot_py = C.REPO / "lg" / "bot" / "bot.py"
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
    proc = subprocess.Popen([sys.executable, str(bot_py)], cwd=str(C.REPO),
                            stdout=log, stderr=log, start_new_session=True)
    print(f"  bot: launched detached (pid {proc.pid}, log docs/metrics/bot.log)")


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
        cwd=str(C.REPO / "lg"),
        stdout=log, stderr=log,
        start_new_session=True,
    )
    print(f"  notify-daemon: launched detached (pid {proc.pid}, log docs/metrics/notify.log)")


def _warn_if_notifications_off() -> None:
    """Say so at startup when a run would push nothing — the failure that let a
    whole run go by with no phone alerts because lg/.env (the webhook) was gone.

    lg/.env is already loaded into os.environ by the time this runs.
    """
    level = os.environ.get("PIPELINE_NOTIFY_LEVEL", "all").lower()
    if level == "silent":
        return
    has_webhook = bool(os.environ.get("DISCORD_WEBHOOK")) \
        or (C.REPO / ".discord-webhook").exists()
    if not C.NOTIFY_SCRIPT.exists():
        print(f"  ⚠ notifications OFF: {C.NOTIFY_SCRIPT} not found. Logs still written.")
    elif not has_webhook:
        print("  ⚠ notifications OFF: no DISCORD_WEBHOOK (lg/.env missing?) and no "
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
    r = sub.add_parser("resume"); r.add_argument("task_id"); r.add_argument("--answer", default="ok")
    rd = sub.add_parser("redo", help="re-run a phase reusing existing artifacts "
                                     "(e.g. redo the debate after fixing an agent)")
    rd.add_argument("task_id")
    rd.add_argument("--from", dest="from_phase",
                    choices=["plan", "debate", "visual"], default="debate",
                    help="debate: reuse brief+plan, redo the debate. "
                         "plan: reuse brief, redo plan then debate. "
                         "visual: reuse the built UI, redo the render+visual gate.")
    st = sub.add_parser("status"); st.add_argument("task_id")
    dr = sub.add_parser("doctor", help="what went wrong: failures, degradations, "
                                       "unhealthy agents, notification & liveness")
    dr.add_argument("task_id")
    sub.add_parser("graph")
    nd = sub.add_parser("notify-daemon", help="persistent notification daemon "
                                      "(priority queue + rate limiter)")
    nd.add_argument("--status", action="store_true", help="check daemon heartbeat + queue")
    nd.add_argument("--stop", action="store_true", help="send SIGTERM for clean shutdown")
    args = p.parse_args()

    if args.cmd == "notify-daemon":
        from pipeline_graph import notify_daemon as nd_mod
        if args.stop:
            return nd_mod.stop()
        if args.status:
            return nd_mod.status()
        return nd_mod.run_daemon()

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
                print("  live log (docs/metrics/pipeline.log):")
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
