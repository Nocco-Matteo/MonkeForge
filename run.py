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
import argparse, contextlib, io, json, os, re, shutil, subprocess, sys, threading, tomllib
from datetime import datetime, timezone
from pathlib import Path

# Load monkeforge.yaml (or .env as fallback) into os.environ.
# Priority: real env vars > yaml > .env > code defaults (where any remain).
# agents.*.model: yaml-only, required — no built-in model defaults.
# Exceptions (read directly by config.py from yaml — no env bridge):
#   agents: role model/cmd; condenser: keep_recent + per-role token budgets;
#   test_suites: gate suite list.
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
    _PIPELINE_LIST_KEYS = frozenset(("arch_docs", "lint_debt_rules", "test_ambient_patterns"))
    for key, val in (data.get("pipeline") or {}).items():
        if key in _PIPELINE_LIST_KEYS:
            val = ";".join(val) if isinstance(val, list) else str(val)
        elif key == "dry_run":
            val = "1" if val else ""
        os.environ.setdefault(f"PIPELINE_{key.upper()}", _envstr(val))

    # effort: -> PIPELINE_EFFORT_JSON (a top-level dict is JSON-serialised; the
    # config module reads it at import time to override the effort presets).
    effort = data.get("effort")
    if isinstance(effort, dict):
        os.environ.setdefault("PIPELINE_EFFORT_JSON", json.dumps(effort))

    # agents: / condenser: / test_suites: read directly by config.py from
    # monkeforge.yaml — NOT bridged into env here. Legacy PIPELINE_TEST_SUITES
    # remains a debug-only override for suites only.

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
from rich.console import Console
from rich.markup import escape as _rich_escape
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

# Config import can raise AgentsConfigError (missing agents:/model:). That is
# an expected operator mistake — print a clig-style `error:` and exit 2, never
# a traceback (https://clig.dev/#errors).
try:
    from pipeline_graph import config as C, events as ev
    from pipeline_graph.graph import build_graph, open_checkpointer
    from pipeline_graph import test_runner as tr
except Exception as _cfg_exc:
    _cli = getattr(_cfg_exc, "cli_message", None)
    if callable(_cli):
        print(_cli(), file=sys.stderr)
        raise SystemExit(2) from None
    raise


# --- Rich TTY rendering (TASK-027) ------------------------------------------
# ``_rich_console`` builds a ``rich.console.Console`` whose ``stderr`` flag is
# always a real bool — never a stream object passed to the ``stderr=`` kwarg
# (which would raise ``TypeError: 'StringIO' is not a valid boolean``). When
# ``stream`` is None the console writes to stderr (``stderr=True``); when a
# stream is given it writes to that stream (``stderr=False``, ``file=stream``).
# Colour is gated by ``_use_color(args, stream=...)`` so ``--no-color`` /
# ``NO_COLOR`` / ``TERM=dumb`` / non-TTY all suppress ANSI in Rich output too.
def _rich_console(args, *, stream=None) -> Console:
    """Build a colour-gated ``rich.console.Console``.

    ``stream=None``  → ``Console(stderr=True, …)``  (writes to stderr).
    ``stream=<f>``   → ``Console(file=f, stderr=False, …)``.
    """
    target = stream if stream is not None else sys.stderr
    color = _use_color(args, stream=target)
    if stream is None:
        return Console(stderr=True, no_color=not color, highlight=False,
                       soft_wrap=True)
    return Console(file=stream, stderr=False, no_color=not color,
                   highlight=False, soft_wrap=True)


# --- Council-log role mapping ----------------------------------------------
# Maps graph node names to the agent-identity role key the council log should
# render for that node. Nodes with no agent (checkpoints, render, wrap-up) map
# to "COUNCIL"; the escalate node maps to "ESCALATION". An unknown node falls
# back to its own name as the role key (see ``_role_display_name``) so the live
# log never shows a bare ``?`` placeholder for a node added without an entry.
NODE_TO_ROLE: dict[str, str] = {
    "init":               "COUNCIL",
    "intake_ask":         "INTERVIEWER",
    "intake_wait":        "INTAKE",
    "plan":               "PROPOSER",
    "checkpoint_effort":  "COUNCIL",
    "debate_tech":        "PLAN_REVIEWER",
    "debate_ux":          "UX_REVIEWER",
    "debate_reply":       "PROPOSER",
    "summary":            "SUMMARIZER",
    "judge":              "JUDGE",
    "checkpoint_plan":    "COUNCIL",
    "implement":          "IMPLEMENTER",
    "code_review":        "CODE_REVIEWER",
    "code_fix":           "IMPLEMENTER",
    "code_verify":        "CODE_REVIEWER",
    "close_batch":        "COUNCIL",
    "ux_render":          "COUNCIL",
    "ux_visual_review":   "VISUAL_REVIEWER",
    "ux_visual_fix":      "VISUAL_FIXER",
    "render_measure":     "COUNCIL",
    "render_review":      "COUNCIL",
    "render_fix":         "IMPLEMENTER",
    "final_check":        "IMPLEMENTER",
    "wrap_up":            "COUNCIL",
    "escalate":           "ESCALATION",
}


def _role_name_for_role(role: str) -> str:
    """Resolve a role key to its council-log display name.

    Calls ``AGENT_IDENTITIES.get(role)`` directly (never
    ``notify_daemon._identity_for``) and falls back to the raw role string
    when the role is not registered, so an unknown role renders as itself
    rather than a bare ``?``.
    """
    from pipeline_graph.notify_daemon import AGENT_IDENTITIES
    name, _avatar = AGENT_IDENTITIES.get(role, (role, ""))
    return name


def _role_display_name(node: str) -> str:
    """Render a node's council-log display name via ``AGENT_IDENTITIES``.

    Looks up the node's role key in ``NODE_TO_ROLE`` (falling back to the raw
    node name when the node is unknown), then resolves that role through
    ``_role_name_for_role`` (which falls back to the raw role string when the
    role is not registered). Both fallbacks ensure an unknown node never
    renders as a bare ``?`` — the live council log stays readable as the
    graph evolves.
    """
    return _role_name_for_role(NODE_TO_ROLE.get(node, node))


# --- Council-log coarse-phase section headers (D4) -------------------------
# Ground truth enumerated (not prefix-derived): ``init``/``escalate`` map to
# ``""`` (no header). ``summary``/``judge``/``checkpoint_plan`` are ``verdict``
# so a post-stuck ``ok`` does not keep printing under ``── debate ──``.
NODE_TO_PHASE: dict[str, str] = {
    "intake_ask":         "intake",
    "intake_wait":        "intake",
    "plan":               "plan",
    "checkpoint_effort":  "plan",
    "debate_tech":        "debate",
    "debate_ux":          "debate",
    "debate_reply":       "debate",
    "summary":            "verdict",
    "judge":              "verdict",
    "checkpoint_plan":    "verdict",
    "implement":          "implement",
    "code_review":        "implement",
    "code_fix":           "implement",
    "code_verify":        "implement",
    "close_batch":        "implement",
    "ux_render":          "visual",
    "ux_visual_review":   "visual",
    "ux_visual_fix":      "visual",
    "render_measure":     "visual",
    "render_review":      "visual",
    "render_fix":         "visual",
    "final_check":        "final",
    "wrap_up":            "final",
    "init":               "",
    "escalate":           "",
}

_PHASE_HEADER: dict[str, str] = {
    "intake":    "── intake ──",
    "plan":      "── plan ──",
    "debate":    "── debate ──",
    "verdict":   "── verdict ──",
    "implement": "── implement ──",
    "visual":    "── visual ──",
    "final":     "── final ──",
}

# --- ANSI colour helpers (D5) ----------------------------------------------
# One place keeps the byte-stability contract testable: ``_c`` returns text
# unchanged when colour is off, so a colour-off capture never sees an ESC.
_ANSI: dict[str, str] = {
    "reset":  "\x1b[0m",
    "dim":    "\x1b[2m",
    "cyan":   "\x1b[36m",
    "green":  "\x1b[32m",
    "yellow": "\x1b[33m",
    "red":    "\x1b[31m",
    "bold":   "\x1b[1m",
}

_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_BARE_ESC_RE = re.compile(r"\x1b")
# C0 controls except \t (0x09) and \n (0x0a); \r is stripped separately.
_C0_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _c(text: str, code: str, *, color: bool) -> str:
    """Wrap ``text`` in an ANSI code when colour is on; return it unchanged
    when colour is off so colour-off captures stay ESC-free."""
    if not color:
        return text
    return f"{_ANSI[code]}{text}{_ANSI['reset']}"


def _strip_ansi(text: str) -> str:
    """Remove CSI sequences and bare ESC bytes from ``text``."""
    return _BARE_ESC_RE.sub("", _ANSI_CSI_RE.sub("", str(text)))


def _sanitize_text(text, *, color: bool, tty: bool) -> str:
    """The single choke point for human strings printed to stderr (D10).

    When colour is on AND the stream is a TTY, text is returned unchanged
    (coloured TTY mode). Otherwise — colour off OR non-TTY — CSI sequences,
    bare ESC bytes, embedded ``\\r`` and other C0 controls (except ``\\n`` /
    ``\\t``) are stripped so redirected / ``NO_COLOR`` / non-TTY captures stay
    ESC-free and ``\\r``-free (C4/C5). Applied to every human-facing field:
    event ``msg``, role names, pause ``reason``/``context``/``plan``/``final``
    /option keys+labels/batch lines, crash one-liner, journal snippets.
    """
    s = str(text)
    if color and tty:
        return s
    s = _ANSI_CSI_RE.sub("", s)
    s = _BARE_ESC_RE.sub("", s)
    s = s.replace("\r", "")
    s = _C0_RE.sub("", s)
    return s


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


def _pause_answers(data: dict) -> dict | list:
    """Normalize interrupt choices for logs and the Discord control bot.

    Reads the structured ``options`` list (Batch 1 escalation shape) when
    present and non-empty, falling back to the legacy ``answers`` dict (effort
    level / intake pauses) or the ``levels`` list. Truthiness — not mere
    presence — guards against an empty ``options`` list silently shadowing a
    populated legacy ``answers`` dict.
    """
    options = data.get("options")
    if isinstance(options, list) and options:
        return options
    answers = data.get("answers")
    if isinstance(answers, dict) and answers:
        return answers
    levels = data.get("levels")
    if isinstance(levels, list):
        return {str(level): "select this effort level" for level in levels}
    return {}


def _options_from_data(data: dict) -> list[dict]:
    """Build a uniform ``options``-shaped list from any interrupt payload.

    Reads the structured ``options`` list (Batch 1 escalation shape) when
    present and non-empty, falling back to the legacy ``answers`` dict
    (effort/intake pauses) or the ``levels`` list, synthesizing option dicts
    in the same ``key``/``label``/``free_text`` shape. Returns ``[]`` for a
    free-text pause (plan approval) — the caller treats an empty list as
    "any non-empty answer is valid".
    """
    options = data.get("options")
    if isinstance(options, list) and options:
        return options
    answers = data.get("answers")
    if isinstance(answers, dict) and answers:
        return [{"key": k, "label": str(v), "free_text": False}
                for k, v in answers.items()]
    levels = data.get("levels")
    if isinstance(levels, list):
        return [{"key": str(l), "label": "select this effort level", "free_text": False}
                for l in levels]
    # Plan approval or other free-text pause: no fixed option menu.
    return []


def _print_pause(data: dict, task_id: str, *, color: bool = False,
                 in_session: bool = False) -> None:
    """Print a pause as instructions instead of an opaque JSON blob.

    Renders from a single ``options``-shaped list — the structured escalation
    ``options`` when present, otherwise synthesized from the legacy
    ``answers``/``levels`` shape — so every pause type prints the same uniform
    menu. The ``./run.py resume <id> --answer ...`` action line is always
    printed last, after the choices, so a human scanning upward sees the
    action they must take immediately above the prompt.

    TASK-027 items 25-27:
      * ``in_session=True`` (the in-process pause loop) omits the
        ``action: ./run.py resume …`` line and instead prints an in-session
        pick hint — the human answers inline, not via a separate ``resume``
        invocation.
      * When ``color`` is on, the pause is rendered via a rich ``Panel`` /
        ``Table`` with every human-facing field wrapped in ``Text(...)`` (or
        passed through ``rich.markup.escape``) so markup characters in the
        fields are escaped (no accidental rich markup interpretation).
      * When ``color`` is off, the existing line-by-line ``sys.stderr.write``
        calls run unchanged (item 28) — byte-identical to the pre-TASK-027
        behaviour, so the existing colour-off pause tests stay green.

    All output goes to ``sys.stderr`` (C1/C9). Every human-facing field is run
    through ``_sanitize_text`` (D10/Delta A) so colour-off / non-TTY captures
    stay ESC-free and ``\\r``-free: ``stage``, ``reason``, ``context``,
    ``plan``, ``final``, batch lines, option ``key``/``label``.
    """
    if color:
        _print_pause_rich(data, task_id, in_session=in_session)
        return
    tty = sys.stderr.isatty()
    stage = _sanitize_text(str(data.get("stage", "")), color=color, tty=tty)
    sys.stderr.write(f"\n{_c('=== PAUSED:', 'bold', color=color)} "
                     f"{_c(stage, 'cyan', color=color)} "
                     f"{_c('===', 'bold', color=color)}\n")
    sys.stderr.write(
        f"  {_c('what to do:', 'dim', color=color)} "
        f"{_sanitize_text(_pause_reason(data), color=color, tty=tty)}\n")
    if data.get("context"):
        sys.stderr.write(
            f"  {_c('context:', 'dim', color=color)} "
            f"{_sanitize_text(data['context'], color=color, tty=tty)}\n")
    # Plan / verdict artifacts the human should read before answering.
    if data.get("plan"):
        sys.stderr.write(
            f"  {_c('plan:', 'dim', color=color)} "
            f"{_sanitize_text(data['plan'], color=color, tty=tty)}\n")
    if data.get("final"):
        sys.stderr.write(
            f"  {_c('final:', 'dim', color=color)} "
            f"{_sanitize_text(data['final'], color=color, tty=tty)}\n")
    batches = data.get("batches")
    if isinstance(batches, list) and batches:
        sys.stderr.write(f"  {_c('batches:', 'dim', color=color)}\n")
        for b in batches:
            sys.stderr.write(
                f"    - {_sanitize_text(b, color=color, tty=tty)}\n")
    # TASK-022 item 19: triage block (mode/blocker trend/repeated/new/
    # recommended+rationale) — only when data["triage"] is a dict. Prints
    # "no active rounds" wording when blocker_counts is empty.
    triage = data.get("triage")
    if isinstance(triage, dict):
        mode = _sanitize_text(str(triage.get("mode", "")), color=color, tty=tty)
        sys.stderr.write(f"  {_c('triage:', 'dim', color=color)}\n")
        sys.stderr.write(f"    {_c('mode:', 'dim', color=color)} {mode}\n")
        bc = triage.get("blocker_counts")
        if isinstance(bc, list) and bc:
            trend = _sanitize_text(" → ".join(str(n) for n in bc),
                                   color=color, tty=tty)
        else:
            trend = _c("no active rounds", "dim", color=color)
        sys.stderr.write(f"    {_c('blockers:', 'dim', color=color)} {trend}\n")
        repeated = triage.get("repeated") or []
        new = triage.get("new") or []
        sys.stderr.write(
            f"    {_c('repeated/new:', 'dim', color=color)} "
            f"{len(repeated)} / {len(new)}\n")
        recommended = str(triage.get("recommended", "") or "").strip()
        rationale = _sanitize_text(str(triage.get("rationale", "") or ""),
                                   color=color, tty=tty).strip()
        if recommended or rationale:
            parts = []
            if recommended:
                parts.append(_c(f"recommended: {recommended}", "cyan", color=color))
            if rationale:
                parts.append(rationale)
            sys.stderr.write(f"    {_sanitize_text(' · '.join(parts), color=color, tty=tty)}\n")
    options = _options_from_data(data)
    if options:
        sys.stderr.write(f"  {_c('choices:', 'dim', color=color)}\n")
        hint = data.get("hint")
        for opt in options:
            key = _sanitize_text(opt.get("key", ""), color=color, tty=tty)
            label = _sanitize_text(opt.get("label", ""), color=color, tty=tty)
            marker = (_c("  (recommended)", "dim", color=color)
                      if opt.get("key") == hint else "")
            sys.stderr.write(
                f"    {_c(key, 'cyan', color=color)}{marker} — {label}\n")
    if in_session:
        sys.stderr.write(
            f"  {_c('pick an option above (or type a key) and press Enter:', 'dim', color=color)}\n")
        sys.stderr.write(
            f"  {_c('Discord buttons also work — they answer this pause in-place.', 'dim', color=color)}\n")
    else:
        sys.stderr.write(
            f"  {_c('action:', 'dim', color=color)} "
            f"./run.py resume {task_id} --answer \"<choice>\"\n")


def _print_pause_rich(data: dict, task_id: str, *, in_session: bool = False) -> None:
    """Rich-rendered pause (TASK-027 item 27). Every human-facing field is
    wrapped in ``Text(...)`` (or passed through ``rich.markup.escape``) before
    being placed into a ``Panel``/``Table`` so markup characters in the fields
    are escaped. Output goes to stderr via a stderr-backed console (C1)."""
    console = Console(stderr=True, no_color=False)
    stage = Text(str(data.get("stage", "")))
    reason = Text(_pause_reason(data))
    title = Text("=== PAUSED: ") + stage + Text(" ===")
    body_lines: list[Text] = []
    body_lines.append(Text("what to do: ") + reason)
    if data.get("context"):
        body_lines.append(Text("context: ") + Text(str(data["context"])))
    if data.get("plan"):
        body_lines.append(Text("plan: ") + Text(str(data["plan"])))
    if data.get("final"):
        body_lines.append(Text("final: ") + Text(str(data["final"])))
    batches = data.get("batches")
    if isinstance(batches, list) and batches:
        body_lines.append(Text("batches:"))
        for b in batches:
            body_lines.append(Text("    - ") + Text(str(b)))
    triage = data.get("triage")
    if isinstance(triage, dict):
        body_lines.append(Text("triage:"))
        body_lines.append(Text("    mode: ") + Text(str(triage.get("mode", ""))))
        bc = triage.get("blocker_counts")
        if isinstance(bc, list) and bc:
            trend = Text(" → ".join(str(n) for n in bc))
        else:
            trend = Text("no active rounds")
        body_lines.append(Text("    blockers: ") + trend)
        repeated = triage.get("repeated") or []
        new = triage.get("new") or []
        body_lines.append(Text(f"    repeated/new: {len(repeated)} / {len(new)}"))
        recommended = str(triage.get("recommended", "") or "").strip()
        rationale = str(triage.get("rationale", "") or "").strip()
        if recommended or rationale:
            parts = []
            if recommended:
                parts.append(f"recommended: {recommended}")
            if rationale:
                parts.append(rationale)
            body_lines.append(Text("    ") + Text(" · ".join(parts)))
    options = _options_from_data(data)
    if options:
        body_lines.append(Text("choices:"))
        hint = data.get("hint")
        for opt in options:
            key = Text(str(opt.get("key", "")))
            label = Text(str(opt.get("label", "")))
            marker = Text("  (recommended)") if opt.get("key") == hint else Text("")
            body_lines.append(Text("    ") + key + marker + Text(" — ") + label)
    if in_session:
        body_lines.append(Text("pick an option above (or type a key) and press Enter:"))
        body_lines.append(Text("Discord buttons also work — they answer this pause in-place."))
    else:
        body_lines.append(Text(f"action: ./run.py resume {task_id} --answer \"<choice>\""))
    panel = Panel(Text("\n").join(body_lines), title=title, border_style="cyan")
    console.print(panel)


def _pending_options(snap) -> list[dict] | None:
    """Extract the pending interrupt's option list from a graph snapshot.

    Returns a list of structured option dicts (``key``/``label``/``free_text``)
    for the pending interrupt, or ``None`` if there is no pending interrupt.
    Synthesizes options for effort/intake pauses that use the legacy
    ``answers``/``levels`` shape so ``_validate_answer`` has a uniform list to
    check against. For free-text pauses (plan approval), returns an empty list
    — the caller passes ``free_text_allowed=True`` to ``_validate_answer`` so
    any non-empty answer is accepted.
    """
    if not snap.interrupts:
        return None
    return _options_from_data(snap.interrupts[0].value)


def _validate_answer(answer, options, *, free_text_allowed=False,
                     router_error=False, intake=False) -> str | None:
    """Validate a ``resume --answer`` against the pending interrupt's options.

    Returns ``None`` if the answer is valid, or an error string explaining why
    not (printed to the user before the run is aborted). Validation rules:

    * An empty/whitespace-only answer is always invalid (the CLI edge for the
      plan pause per the corrected test (o)).
    * Universal stop keys (``stop``, ``no``, ``abort``, ``cancel``) are always
      accepted — every pause type honours them.
    * Router errors accept any non-empty answer (there is no domain-specific
      menu to enforce; any non-stop answer retries the router).
    * Intake pauses accept ``INTAKE_END_ANSWERS`` + ``INTAKE_SUBMIT_ANSWERS``.
    * Free-text-allowed pauses (plan approval) accept any non-empty answer —
      ``"yes"`` approves, any other non-empty string is a rejection reason.
    * Otherwise the answer must match (canonically) one of the option keys.
    """
    from pipeline_graph.nodes.common import _canonical_key

    ans = str(answer).strip() if answer is not None else ""
    if not ans:
        return ("error: --answer is empty; provide one of the listed choices "
                "(or a universal stop key: stop, no, abort, cancel)")
    canonical = _canonical_key(ans)

    # Universal stop keys are always valid for every pause type.
    if canonical in ("stop", "no", "abort", "cancel"):
        return None

    if router_error:
        return None  # any non-stop answer retries the router

    if intake:
        from pipeline_graph.nodes.intake import INTAKE_END_ANSWERS, INTAKE_SUBMIT_ANSWERS
        if canonical in INTAKE_END_ANSWERS or canonical in INTAKE_SUBMIT_ANSWERS:
            return None

    if free_text_allowed:
        return None  # any non-empty answer is valid (approve or reject-with-reason)

    valid_keys = {_canonical_key(o["key"]) for o in (options or [])}
    if canonical in valid_keys:
        return None

    valid_str = ", ".join(sorted(valid_keys)) if valid_keys else "(no options)"
    return (f"error: answer {answer!r} is not one of the valid choices "
            f"({valid_str}); pick one, or use a universal stop key "
            f"(stop, no, abort, cancel)")


def _read_answer_line(*, eof_raises: bool = False,
                      pending_task_id: str | None = None) -> str:
    """Read one answer line from stdin and/or a Discord pending-answer file.

    When ``pending_task_id`` is set (interactive session loop), poll the
    shared pending-answer file so a Discord button can unblock this process
    without spawning a second ``run.py resume``. Stdin still wins when the
    human types first.
    """
    import select
    import time
    from pipeline_graph import pending_answer as PA

    def _from_pending() -> str | None:
        if not pending_task_id:
            return None
        return PA.take_pending_answer(pending_task_id)

    def _consume_stdin() -> str | None:
        """Return stripped line, ``""`` for empty Enter, or raise EOFError."""
        if eof_raises:
            raw = sys.stdin.readline()
            if raw == "":
                raise EOFError
            return raw.strip()
        try:
            return sys.stdin.readline().strip()
        except EOFError:
            return ""

    # Fast path: Discord already wrote before we entered the wait.
    taken = _from_pending()
    if taken is not None:
        sys.stderr.write(f"  (answered via Discord: {taken})\n")
        sys.stderr.flush()
        return taken

    fd = None
    if pending_task_id:
        try:
            fd = sys.stdin.fileno()
        except (AttributeError, io.UnsupportedOperation, ValueError):
            fd = None

    if pending_task_id and fd is not None:
        while True:
            taken = _from_pending()
            if taken is not None:
                sys.stderr.write(f"  (answered via Discord: {taken})\n")
                sys.stderr.flush()
                return taken
            ready, _, _ = select.select([sys.stdin], [], [], 0.25)
            if not ready:
                continue
            return _consume_stdin()

    # No live Discord poll (tests / non-session): one blocking readline.
    # If a pending file was written just before the call, the fast path above
    # already returned it.
    if pending_task_id and fd is None:
        # Fake stdin (unit tests): spin briefly so a concurrent writer can land.
        for _ in range(4):
            taken = _from_pending()
            if taken is not None:
                sys.stderr.write(f"  (answered via Discord: {taken})\n")
                sys.stderr.flush()
                return taken
            time.sleep(0.05)
    return _consume_stdin()


def _tty_pick(options: list[dict], data: dict, *, color: bool = False,
              eof_raises: bool = False, render: bool = True,
              pending_task_id: str | None = None) -> str:
    """Interactive picker: list the pending options and read a choice from stdin.

    Returns the chosen option's canonical key. Falls back to the hint (or
    ``"ok"``) on an empty line so a bare Enter takes the recommended path.

    TASK-027 items 38-41:
      * ``eof_raises=True`` — when the raw ``sys.stdin.readline()`` returns
        ``""`` (EOF), raise ``EOFError`` BEFORE ``.strip()`` is applied, so the
        in-session pause loop can distinguish "human hit Ctrl-D" from "human
        pressed Enter on an empty line" (the latter is a valid "take the
        hint" answer). The existing ``except EOFError: line = ""`` fallbacks
        are conditioned on ``not eof_raises`` so they stay behaviour-identical
        for the ``resume`` call site.
      * ``render=False`` — skip every ``sys.stderr.write`` call (heading,
        reason, choices, prompt). Used by the in-session pause loop, which
        renders the pause chrome via ``_print_pause(..., in_session=True)``
        first and then calls ``_tty_pick(..., render=False, eof_raises=True)``
        to read ONLY the answer line — no double render (item 41 / S5).
      * ``pending_task_id`` — when set, also accept a Discord answer written
        to ``pending-answer-{id}.json`` (CLI ↔ Discord synergy).

    The prompt is written to ``sys.stderr`` (C9 — not via ``input()`` to
    stdout) and stdin is read via ``sys.stdin.readline()`` so the prompt and
    the answer stay on the same stream. Every human-facing field is run through
    ``_sanitize_text`` (D10/Delta A): ``stage``, ``reason``, option
    ``key``/``label``. Colour is gated by the ``color`` flag threaded in from
    ``main()`` so ``resume --no-color`` / ``NO_COLOR`` / ``TERM=dumb`` / non-TTY
    stderr all suppress ANSI in the pause chrome (C5/C9).
    """
    from pipeline_graph.nodes.common import _canonical_key

    tty = sys.stderr.isatty()
    hint = str(data.get("hint", "")).strip()
    if render:
        stage = _sanitize_text(str(data.get("stage", "?")), color=color, tty=tty)
        reason = _sanitize_text(str(data.get("reason", "")).strip(),
                                color=color, tty=tty)
        sys.stderr.write(f"\n{_c('=== PAUSED:', 'bold', color=color)} "
                         f"{_c(stage, 'cyan', color=color)} "
                         f"{_c('===', 'bold', color=color)}\n")
        if reason:
            sys.stderr.write(f"  {reason}\n")
        if not options:
            # Free-text pause (plan approval): read a raw line.
            sys.stderr.write(
                f"  {_c('type your answer and press Enter (empty = approve):', 'dim', color=color)}\n")
        else:
            sys.stderr.write(f"  {_c('choices:', 'dim', color=color)}\n")
            for i, opt in enumerate(options, 1):
                key = _sanitize_text(opt.get("key", "?"), color=color, tty=tty)
                label = _sanitize_text(opt.get("label", ""), color=color, tty=tty)
                marker = (_c("  (recommended)", "dim", color=color)
                          if opt.get("key") == hint else "")
                sys.stderr.write(
                    f"    {_c(f'[{i}]', 'dim', color=color)} "
                    f"{_c(key, 'cyan', color=color)}{marker} — {label}\n")
            sys.stderr.write(
                f"  {_c(f'pick [1-{len(options)}] or type a key:', 'dim', color=color)} ")
        if pending_task_id:
            sys.stderr.write(
                f"\n  {_c('(Discord buttons also work — they answer this pause)', 'dim', color=color)}\n")
    if not options:
        if render:
            sys.stderr.write("> ")
        line = _read_answer_line(eof_raises=eof_raises,
                                 pending_task_id=pending_task_id)
        return line or "ok"
    line = _read_answer_line(eof_raises=eof_raises,
                             pending_task_id=pending_task_id)
    if not line:
        return _canonical_key(hint) if hint else "ok"
    if line.isdigit() and 1 <= int(line) <= len(options):
        return _canonical_key(options[int(line) - 1]["key"])
    return _canonical_key(line)


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
    # Item 36: match the three provenance tag forms — bare [BLOCKER],
    # [BLOCKER:PLAN], [BLOCKER:REQUIREMENTS] — so a provenance-tagged blocker
    # surfaces in the Discord card the same as a bare one.
    blocker_re = re.compile(r"\[BLOCKER(?::(?:PLAN|REQUIREMENTS))?\]", re.IGNORECASE)
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
        if in_last_section and blocker_re.search(line):
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


# --- Council-log rendering infrastructure (D2/D6/D8/D9/D12/D13) ------------
# A single module-level lock serialises every stderr write that touches the
# in-place progress line: the UI thread's heartbeat refresh, the synchronous
# step-event hook's dispatch/return rendering, the crash path's finish+print,
# and the permanent-line emission (pause/finished/stall). The lock is a plain
# (non-reentrant) ``threading.Lock``; the rendering helpers below are
# lock-HELD — they never acquire ``_progress_lock`` themselves, the caller
# must already hold it (D12). This avoids the self-deadlock a reentrant
# helper would mask.
_progress_lock = threading.Lock()

# Mutable state shared between the main thread, the synchronous step-event
# hook (which runs on the graph thread), and the UI daemon thread.
#   dispatched_node : the node a dispatch line was last printed for (D8
#                     nested-suppression guard), or None after a real return.
#   last_phase      : the last coarse phase a section header was emitted for.
#   events_offset   : shared byte cursor into events.jsonl (D13 tail read).
#   disabled        : set True at shutdown so a late UI iteration cannot print
#                     after === FINISHED === (D11).
#   color / tty     : cached gate values for the current drive.
ui_state: dict = {
    "dispatched_node": None,
    "last_phase": "",
    # Last council-log monke display name — used to insert a blank line between
    # different agents so dispatch/return blocks are scannable.
    "last_monke": "",
    "events_offset": 0,
    "disabled": False,
    "color": False,
    "tty": False,
}

# Drop the instrument's ``[outcome] `` prefix from a return msg in favour of
# the ✓/⛔ symbol rendered by ``_emit_return``.
_OUTCOME_PREFIX_RE = re.compile(r"^\[(ok|blocked|failed|degraded)\]\s*")


def _finish_progress(*, color: bool, tty: bool) -> None:
    """Clear the in-place progress line (D6). Caller MUST hold ``_progress_lock``.

    TTY + colour on ⇒ ``\\r\\x1b[K``; TTY + colour off ⇒ ``\\r`` + space-padding
    to a fixed width then ``\\r`` (no ``\\x1b``); non-TTY ⇒ nothing.
    """
    if not tty:
        return
    if color:
        sys.stderr.write("\r\x1b[K")
    else:
        sys.stderr.write("\r" + " " * 80 + "\r")


def _emit_dispatch(node: str, *, msg: str, color: bool) -> None:
    """Render a dispatch (step_start) line (D4/D8). Caller MUST hold
    ``_progress_lock`` (D12) — this helper never acquires it.

    Suppresses nested re-entry: a ``step_start`` for a node already equal to
    ``dispatched_node`` (the implement.py test-baseline nested emit) prints
    nothing. Otherwise finishes the progress line, emits a section header on a
    real phase transition, prints the dispatch line, and records the node.
    """
    if ui_state["disabled"] or node == ui_state["dispatched_node"]:
        return
    tty = ui_state["tty"]
    _finish_progress(color=color, tty=tty)
    name = _sanitize_text(_role_display_name(node), color=color, tty=tty)
    phase = NODE_TO_PHASE.get(node, "")
    phase_changed = bool(phase and phase != ui_state["last_phase"])
    if phase_changed:
        sys.stderr.write("\n")
        sys.stderr.write(_c(_PHASE_HEADER[phase], "dim", color=color) + "\n")
        ui_state["last_phase"] = phase
    elif ui_state["last_monke"] and ui_state["last_monke"] != name:
        # Blank line between different monkes inside the same phase (e.g.
        # Drill → Vervet under ``── implement ──``).
        sys.stderr.write("\n")
    body = _sanitize_text(msg, color=color, tty=tty)
    sys.stderr.write(f"  {_c(name, 'cyan', color=color)} · {body}\n")
    ui_state["dispatched_node"] = node
    ui_state["last_monke"] = name


def _emit_return(node: str, msg: str, outcome: str, *, color: bool) -> None:
    """Render a return (step_end) line (D8). Caller MUST hold ``_progress_lock``
    (D12) — this helper never acquires it.

    Only called for events carrying an ``outcome`` field (the instrument always
    sets it; manual emits do not and are dropped by ``_drain_events``). Drops
    the ``[outcome] `` prefix from ``msg`` in favour of ✓/⛔.
    """
    if ui_state["disabled"]:
        return
    tty = ui_state["tty"]
    _finish_progress(color=color, tty=tty)
    name = _sanitize_text(_role_display_name(node), color=color, tty=tty)
    body = _sanitize_text(msg, color=color, tty=tty)
    body = _OUTCOME_PREFIX_RE.sub("", body)
    if outcome == "ok":
        sym, code = "✓", "green"
    elif outcome == "failed":
        sym, code = "⛔", "red"
    else:  # blocked / degraded / unknown → yellow
        sym, code = "⛔", "yellow"
    sys.stderr.write(f"  {_c(sym, code, color=color)} "
                     f"{_c(name, 'cyan', color=color)} · {body}\n")
    ui_state["last_monke"] = name


def _drain_events(task_id: str) -> None:
    """Read new events since the shared cursor and render them in true
    chronological order (D9/D13). Caller MUST hold ``_progress_lock`` (D12).

    Uses ``ev.read_events_since`` (a tail byte-read, O(new lines)) so a hook
    firing on every ``step_start``/``step_end`` cannot grow dispatch latency
    with log size (Delta B). Applies D8 nested filtering: a ``step_start`` for
    a node already dispatched is suppressed; a ``step_end`` without an
    ``outcome`` field (manual auto-fix / test-baseline events) is silently
    dropped; a real ``step_end`` clears ``dispatched_node``.
    """
    color = ui_state["color"]
    events, new_offset = ev.read_events_since(task_id, ui_state["events_offset"])
    ui_state["events_offset"] = new_offset
    for e in events:
        kind = e.get("kind")
        node = e.get("step", "")
        if not node:
            continue
        if kind == "step_start":
            _emit_dispatch(node, msg=e.get("msg", ""), color=color)
        elif kind == "step_end":
            if "outcome" in e:
                _emit_return(node, e.get("msg", ""), e.get("outcome", "ok"),
                             color=color)
                ui_state["dispatched_node"] = None


def _refresh_progress(task_id: str) -> None:
    """Refresh the in-place progress line from the ``current.json`` heartbeat
    (D2). Caller MUST hold ``_progress_lock`` (D12). TTY-only: non-TTY stderr
    emits no ``\\r`` animation (C4). A partial/non-atomic ``current.json`` write
    raises ``ValueError`` (``JSONDecodeError``) — caught and skipped so one bad
    read cannot kill progress for the rest of the run (D7).
    """
    color = ui_state["color"]
    tty = ui_state["tty"]
    if not tty:
        return
    cur_path = C.METRICS / "current.json"
    if not cur_path.exists():
        return
    try:
        cur = json.loads(cur_path.read_text())
    except (OSError, ValueError):
        return  # D7: partial write — skip this iteration, keep the thread alive
    if cur.get("task") != task_id or cur.get("phase") == "agent done":
        return
    started = cur.get("started")
    if not started:
        return
    try:
        started_dt = datetime.fromisoformat(started)
    except ValueError:
        return
    elapsed = int((datetime.now(timezone.utc) - started_dt).total_seconds())
    role = cur.get("role", "")
    name = _sanitize_text(_role_name_for_role(role), color=color, tty=tty)
    step = _sanitize_text(cur.get("step", ""), color=color, tty=tty)
    body = f"{_c('…', 'dim', color=color)} {_c(name, 'cyan', color=color)} · {step} · {elapsed}s"
    if color:
        sys.stderr.write(f"\r\x1b[K{body}")
    else:
        sys.stderr.write(f"\r{body.ljust(80)}")


def _start_ui_thread(task_id: str, stop_event: threading.Event
                     ) -> threading.Thread:
    """Start the UI daemon thread (D2).

    One thread does both jobs: refresh the in-place progress line from
    ``current.json`` (~10 Hz) and, as a safety net, drain any ``events.jsonl``
    events the synchronous step-event hook has not already rendered. The sync
    hook (D9) advances the shared cursor on every ``step_start``/``step_end``,
    so the poller's event read is normally a no-op; the poller's real job is
    the heartbeat. The thread checks ``stop_event`` at the TOP of its loop,
    BEFORE acquiring ``_progress_lock``, so a crash path that sets
    ``stop_event`` then joins (without holding the lock) lets the thread exit
    without contending. Every per-iteration stderr write is wrapped in
    ``try/except (BrokenPipeError, OSError, ValueError)`` (D7) so a closed
    pipe or a partial ``current.json`` cannot kill the thread.
    """
    def _loop():
        while not stop_event.is_set():
            try:
                with _progress_lock:
                    if not ui_state["disabled"]:
                        _refresh_progress(task_id)
                        _drain_events(task_id)
            except (BrokenPipeError, OSError, ValueError):
                pass
            stop_event.wait(0.1)

    t = threading.Thread(target=_loop, name="mf-ui", daemon=True)
    t.start()
    return t


def _step_event_hook(task_id: str, step: str, msg: str, **extra) -> None:
    """Synchronous step-event callback (D9): render dispatch/return
    immediately on the calling thread. Registered on ``ev`` via
    ``ev.set_step_hook``; fires inside ``ev.emit`` right after the event is
    persisted to ``events.jsonl`` and before the notify push, so dispatch
    prints before the agent blocks (C8) and return for N prints before
    dispatch for N+1 (C6). Drains from the shared cursor in event order.
    """
    with _progress_lock:
        _drain_events(task_id)


def _drive(graph, task_id, payload, args=None) -> int:
    """Run until the graph ends or hits an interrupt; print what happened.

    ``args`` is the parsed CLI namespace (or ``None`` when called directly from
    tests) so ``--no-color`` / ``NO_COLOR`` / ``TERM=dumb`` / non-TTY stderr all
    gate colour. Council-log narration, progress, pause chrome, the finished
    banner and the stall line all print to ``sys.stderr`` (C1); machine output
    (``status --json``, help, ``--version``, ``metrics``) stays on stdout (C2).
    """
    cfg = _thread(task_id)
    color = _use_color(args, stream=sys.stderr)
    try:
        tty = sys.stderr.isatty()
    except (AttributeError, ValueError, OSError):
        tty = False
    # Reset the shared UI state for this drive.
    ui_state["dispatched_node"] = None
    ui_state["last_phase"] = ""
    ui_state["last_monke"] = ""
    ui_state["disabled"] = False
    ui_state["color"] = color
    ui_state["tty"] = tty
    # Event-cursor baseline (D13): capture the current byte offset so historical
    # events from earlier runs on the same task id are skipped — only events
    # emitted by THIS drive are rendered.
    ui_state["events_offset"] = (
        ev.EVENTS_LOG.stat().st_size if ev.EVENTS_LOG.exists() else 0)
    stop_event = threading.Event()
    ui_thread = _start_ui_thread(task_id, stop_event)
    ev.set_step_hook(_step_event_hook)
    try:
        # TASK-027 items 29-37: the stream+pause dispatch is a ``while True``
        # loop nested inside this outer try. Per Amendment A, the
        # ``except BrokenPipeError`` is scoped around ONLY the inner
        # ``for mode, chunk in graph.stream(...)`` loop (step 1), so steps
        # (2)-(4) always run in the same iteration whether or not the pipe
        # closed — guaranteeing every branch reaches an explicit
        # ``return <int>`` (C14/item 31). The outer ``except
        # KeyboardInterrupt`` (item 32: no bare raise — emits run_stalled,
        # writes the Ctrl+C/resume hint, returns 130) and ``except Exception``
        # (the crash path, returns 2) wrap the entire while loop; the
        # ``finally`` cleans up the UI thread.
        while True:
            seen_updates_iter = False
            # Step (1): the stream. BrokenPipeError is scoped HERE ONLY.
            try:
                # stream_mode is updates-only — the "debug" mode that printed
                # bare ``[<node>] ...`` lines (and a ``[?]`` placeholder when a
                # debug chunk lacked a name) is gone. Dispatch/return are
                # rendered synchronously by the step-event hook as events are
                # emitted, NOT after the chunk yields.
                for mode, chunk in graph.stream(payload, cfg,
                                                stream_mode=["updates"]):
                    if mode == "updates":
                        for node, delta in chunk.items():
                            if node == "__interrupt__":
                                continue
                            seen_updates_iter = True
            except BrokenPipeError:
                # stderr pipe closed (parent terminal died during a background
                # run). Redirect BOTH stdout and stderr to /dev/null so
                # remaining prints (and any traceback on stderr) don't re-raise,
                # then fall through to steps (2)-(4) in THIS iteration — the
                # pipeline state is still valid (Amendment A).
                try:
                    sys.stdout = open(os.devnull, "w")
                except OSError:
                    pass
                try:
                    sys.stderr = open(os.devnull, "w")
                except OSError:
                    pass
            # Step (2): final event drain + disable UI rendering.
            with _progress_lock:
                _drain_events(task_id)
            ui_state["disabled"] = True
            # Step (3): snapshot + the "resuming" hint.
            snap = graph.get_state(cfg)
            if not seen_updates_iter and payload is None and snap.next:
                sys.stderr.write(
                    f"  (resuming — next node: {snap.next[0]})\n")
            # Step (4): branch on the snapshot state.
            if snap.interrupts:
                data = snap.interrupts[0].value
                with _progress_lock:
                    _finish_progress(color=color, tty=tty)
                # Carry the answer menu (and, for a visual escalation, where
                # the screenshots are) in the event so the optional Discord bot
                # can build one button per valid answer without querying the
                # graph internals. Item 33: run_paused is emitted exactly once
                # per observed interrupt, here in the shared code BEFORE the
                # 4a/4b branch split.
                reason = _pause_reason(data)
                answers = _pause_answers(data)
                options = data.get("options") or []
                router_error = bool(data.get("router_error", False))
                blockers = ""
                if "debate" in reason.lower():
                    blockers = _extract_debate_blockers(task_id)
                ev.emit("run_paused", task_id, str(data.get("stage", "?")),
                        reason,
                        answers=answers if isinstance(answers, dict) else None,
                        options=options or None,
                        router_error=router_error,
                        hint=data.get("hint", ""),
                        context=data.get("context", ""), blockers=blockers,
                        plan=data.get("plan", ""),
                        final=data.get("final", ""),
                        batches=data.get("batches") or [],
                        screens=str(C.SCREENS / f"task-{task_id}")
                                if "screenshot" in reason.lower()
                                or "visual" in reason.lower() else "",
                        # TASK-022 item 20 (C8): pass triage ONLY via a
                        # conditional spread keyed on isinstance(triage, dict).
                        **({"triage": data["triage"]}
                           if isinstance(data.get("triage"), dict) else {}))
                if args is not None and _interactive_gate(args):
                    # Step (4a): interactive in-session pause loop.
                    # Item 34: stop the UI thread under the lock, then a
                    # separate acquire/release barrier so the UI thread
                    # (which checks stop_event at the TOP of its loop before
                    # re-acquiring the lock) has exited its iteration before
                    # we render pause chrome.
                    with _progress_lock:
                        stop_event.set()
                        ui_state["disabled"] = True
                        _finish_progress(color=color, tty=tty)
                    with _progress_lock:
                        pass  # barrier
                    try:
                        ui_thread.join(timeout=1.0)
                    except Exception:
                        pass
                    # Item 35: _print_pause(in_session=True) BEFORE
                    # _tty_pick(render=False, eof_raises=True) — no double
                    # render (S5/item 41).
                    _print_pause(data, task_id, color=color, in_session=True)
                    _opts = _options_from_data(data)
                    # Gate Discord answers to THIS pause (drops stale files
                    # from a previous pause; keeps answers written after the
                    # gate — never wipe a just-arrived Discord click).
                    from pipeline_graph import pending_answer as _PA
                    _PA.begin_pause_wait(task_id)
                    try:
                        answer = _tty_pick(_opts, data, color=color,
                                           render=False, eof_raises=True,
                                           pending_task_id=task_id)
                    except (EOFError, KeyboardInterrupt):
                        # Human bailed (Ctrl-D / Ctrl-C) at the in-session
                        # picker. The task is at a clean pause point — mark
                        # it idle so status doesn't call it dead, and return
                        # 130 (interrupted, not a crash). The run_paused
                        # event was already emitted above (item 33).
                        _PA.end_pause_wait(task_id)
                        _PA.clear_pending_answer(task_id)
                        _mark_idle(task_id, "paused")
                        sys.stderr.write(
                            "\ninterrupted — the run is paused; resume with "
                            f"./run.py resume {task_id}\n")
                        return 130
                    _PA.end_pause_wait(task_id)
                    # Item 36: on a valid in-session answer, create a new
                    # stop_event + new ui_thread, re-enable UI rendering, set
                    # payload = Command(resume=answer), and continue the loop.
                    stop_event = threading.Event()
                    ui_thread = _start_ui_thread(task_id, stop_event)
                    ui_state["disabled"] = False
                    payload = Command(resume=answer)
                    continue
                else:
                    # Step (4b): non-interactive. Item 37: _mark_idle +
                    # _print_pause(in_session=False) + return 0, with NO
                    # second run_paused emit (it was emitted above, item 33).
                    _mark_idle(task_id, "paused")
                    _print_pause(data, task_id, color=color, in_session=False)
                    return 0
            elif snap.next:
                # Neither finished nor waiting for anyone: this is a stall.
                with _progress_lock:
                    _finish_progress(color=color, tty=tty)
                    sys.stderr.write(f"\nstopped at: {snap.next}\n")
                ev.emit("run_stalled", task_id, str(snap.next[0]),
                        f"run stopped at {snap.next} without finishing or "
                        f"asking anything")
                _mark_idle(task_id, "stalled")
                return 1
            else:
                with _progress_lock:
                    _finish_progress(color=color, tty=tty)
                    sys.stderr.write("\n=== FINISHED ===\n")
                _mark_idle(task_id, "finished")
                return 0
    except KeyboardInterrupt:
        # Item 32: no bare raise. Emit run_stalled, write the Ctrl+C/resume
        # hint to stderr, return 130. The finally block cleans up the UI
        # thread before this return takes effect.
        try:
            ev.emit("run_stalled", task_id, "driver",
                    "interrupted from the keyboard")
        except Exception:
            pass
        try:
            sys.stderr.write(
                "\ninterrupted (Ctrl+C) — the run is paused; resume with "
                f"./run.py resume {task_id}\n")
        except (BrokenPipeError, OSError, ValueError):
            pass
        return 130
    except Exception as exc:
        # Something outside any node — the checkpointer, the stream itself.
        # instrument() cannot see this, so it is caught and announced here.
        # D11/C10: FIRST stop+join the UI thread WITHOUT holding the lock, THEN
        # under the lock clear the in-place line and print a human one-liner +
        # journal/raw path on stderr (no traceback body). The report itself is
        # wrapped in try/except (BrokenPipeError, OSError, ValueError): a
        # closed/invalid stderr can raise ValueError as well as pipe errors —
        # catch all three, skip the print, still return 2 / still attempt the
        # emit in its own try. The reporter must never raise and must never let
        # Python print a traceback.
        stop_event.set()
        try:
            ui_thread.join(timeout=1.0)
        except Exception:
            pass
        ui_state["disabled"] = True
        try:
            with _progress_lock:
                _finish_progress(color=color, tty=tty)
                one_liner = _sanitize_text(
                    f"driver crashed: {type(exc).__name__}: {exc}",
                    color=color, tty=tty)
                sys.stderr.write(f"\n{one_liner}\n")
                sys.stderr.write(
                    f"  journal: {C.METRICS / f'journal-{task_id}.log'}\n")
                sys.stderr.write(f"  events: {ev.EVENTS_LOG}\n")
        except (BrokenPipeError, OSError, ValueError):
            pass
        try:
            ev.emit("run_stalled", task_id, "driver",
                    f"driver crashed: {type(exc).__name__}: {exc}")
        except Exception:
            pass
        return 2
    finally:
        stop_event.set()
        ev.set_step_hook(None)
        try:
            ui_thread.join(timeout=1.0)
        except Exception:
            pass


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
    # TASK-027 item 42: route to stderr so a captured stdout stream stays clean.
    print(f"  note: task id {task_id} already has files from an earlier run:",
          file=sys.stderr)
    for p in leftovers:
        try:
            label = p.relative_to(C.REPO)
        except ValueError:
            label = p
        print(f"    {label}", file=sys.stderr)
    print("    the interview appends to the intake file and will not accept the "
          "old brief as its own; delete them first for a clean start.",
          file=sys.stderr)


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
    level = os.environ.get("PIPELINE_NOTIFY_LEVEL", "milestones").lower()
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

    Known limitation (FINAL-020 §1 item 1): the check-then-act pattern below
    (``os.kill`` read → ``Popen`` → pidfile write) has no interprocess lock,
    so concurrent ``start``/``resume`` invocations against the same repo can
    both pass the liveness check and both spawn a bot. The impact is bounded
    (duplicate escalation cards, not silent drops — both processes tail the
    same ``events.jsonl`` faithfully). Do not fire concurrent auto-starting
    invocations against the same repo; if this surfaces in practice the fix
    is an ``O_EXCL`` lockfile around the check-Popen-write sequence.

    The ``.bot.ready`` sentinel is cleared BEFORE the spawn so a stale
    sentinel from a previous bot does not make the webhook suppress
    ``run_paused`` pushes while the new bot is still in its catch-up
    iteration (C15/C17). The new bot's poller writes the sentinel itself
    once its first iteration commits the cursor.

    ``PIPELINE_DOCS_DIR`` (resolved absolute) is passed explicitly in the
    child env so a bot auto-started by ``run.py`` reads the same per-repo
    docs directory the pipeline is using, even when the operator's shell
    did not export it (C14). Without this, the bot could resolve
    ``DOCS`` to a different repo's docs and never see the events the
    pipeline writes.
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
    # Clear any stale readiness sentinel BEFORE spawning so the webhook does
    # not suppress run_paused pushes while the new bot is still catching up
    # on its first poller iteration (C15/C17). The bot's poller writes the
    # sentinel itself once its first iteration's cursor commit completes.
    (C.METRICS / ".bot.ready").unlink(missing_ok=True)
    log = (C.METRICS / "bot.log").open("a")
    child_env = os.environ.copy()
    child_env["PIPELINE_DOCS_DIR"] = str(C.DOCS.resolve())
    proc = subprocess.Popen([sys.executable, str(bot_py)], cwd=str(_MF_ROOT),
                            stdout=log, stderr=log, start_new_session=True,
                            env=child_env)
    pidfile.write_text(str(proc.pid))
    print(f"  bot: launched detached (pid {proc.pid}, log {C.METRICS / 'bot.log'}")


def _ensure_notify_daemon() -> None:
    """Auto-start the notify daemon if it is not already running.

    Same detached-singleton pattern as _ensure_bot(): the daemon must outlive
    the start/resume process, so it is launched with start_new_session.
    Idempotent via the heartbeat file's pid — repeated calls don't spawn
    duplicates. No-op if no webhook is configured.
    """
    level = os.environ.get("PIPELINE_NOTIFY_LEVEL", "milestones").lower()
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
    level = os.environ.get("PIPELINE_NOTIFY_LEVEL", "milestones").lower()
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
        print("usage: ./run.py metrics <task_id> | --all", file=sys.stderr)
        print("error: provide a task id or --all", file=sys.stderr)
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


def _use_color(args, *, stream=None) -> bool:
    """Return True iff colour output should be emitted on ``stream``.

    ``stream`` defaults to ``sys.stderr`` — the council-log stream — so the
    gate tests the TTY-ness of the stream the human messaging actually goes
    to, not stdout. Colour is gated on ALL of:

      * ``stream`` is a TTY (no colour when piped/redirected), AND
      * ``--no-color`` was not passed on the CLI, AND
      * the ``NO_COLOR`` environment variable is unset (tested by *presence*
        via ``"NO_COLOR" in os.environ`` so an empty-but-set ``NO_COLOR=``
        disables colour per https://no-color.org), AND
      * ``TERM`` is not ``dumb``.

    This is the single gate every colour-aware council-log / pause-chrome
    print consults — keeping the contract
    (``--no-color`` / ``NO_COLOR`` / non-TTY / ``TERM=dumb`` ⇒ plain text)
    in one place.
    """
    if stream is None:
        stream = sys.stderr
    try:
        if not stream.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    if getattr(args, "no_color", False):
        return False
    if "NO_COLOR" in os.environ:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return True


def _status_json(args) -> int:
    """``status --json``: emit a single JSON object on stdout.

    Wraps the entire checkpointer-open / graph-build / state-read sequence
    in a try/except so a failure (missing/corrupt DB, graph build error)
    surfaces as a JSON ``{"error": ...}`` object on stdout with a non-zero
    exit code — never a traceback. The "no run found" case is also a JSON
    error object, not the plain-text ``print("no run found for this task")``
    the non-``--json`` branch uses. Exactly one ``json.dumps(...)`` call
    reaches stdout on every path here; nothing else prints to stdout.
    """
    task_id = args.task_id
    try:
        with open_checkpointer() as cp:
            graph = build_graph(cp)
            snap = graph.get_state(_thread(task_id))
    except Exception as exc:  # noqa: BLE001 — surface any failure as JSON
        print(json.dumps({"error": f"could not read state: {exc}",
                          "task": task_id}))
        return 1
    if not snap.created_at:
        print(json.dumps({"error": "no run found", "task": task_id}))
        return 1
    v = snap.values
    payload = {
        "next": snap.next or "END",
        "batches": [
            {"n": b.get("n"), "status": b.get("status"),
             "scope": b.get("scope")}
            for b in v.get("batches", [])
        ],
        "paused": bool(snap.interrupts),
        "options": _pending_options(snap) or [],
    }
    print(json.dumps(payload))
    return 0


def _read_version() -> str:
    try:
        with open(_MF_ROOT / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        return data["project"]["version"]
    except (OSError, KeyError, ValueError, TypeError):
        return "0.0.0"


_VERSION = _read_version()
_SUPPORT_URL = "https://github.com/Nocco-Matteo/MonkeForge"

# Shared parent parser carrying --no-color (with ``default=argparse.SUPPRESS``
# so the attribute is absent when the flag is not passed — ``_use_color`` then
# treats absence as "colour allowed"). Inherited by both the top-level parser
# and every subparser so ``--no-color`` works before OR after the subcommand.
_no_color_parent = argparse.ArgumentParser(add_help=False)
_no_color_parent.add_argument("--no-color", dest="no_color", action="store_true",
                              default=argparse.SUPPRESS,
                              help="disable ANSI colour output (also disabled when "
                                   "NO_COLOR is set or stdout is not a TTY)")


# --- TASK-027: interactive gate, wizard sentinel, start wizard ---------------
# ``_EXTERNAL_NO_INPUT`` captures ``PIPELINE_NO_INPUT`` as it was when ``main``
# started (before the env-bridge block mutates ``os.environ``). The env-bridge
# ``else`` branch (``elif not _EXTERNAL_NO_INPUT``) must NOT pop a value an
# external caller deliberately set — only one the bridge itself set this run.
_EXTERNAL_NO_INPUT: str | None = None

# Sentinel returned by ``_run_start_wizard`` on a hard validation failure (e.g.
# ``--file`` path missing). Distinct from ``None`` (EOF/Ctrl-C abort) so the
# ``main`` invocation block can branch on ``is None`` vs ``is _WIZARD_ERROR``
# without the ``... or args`` pattern that silently masks a failed wizard.
_WIZARD_ERROR = object()


def _interactive_gate(args) -> bool:
    """Return True iff the CLI is allowed to prompt the human interactively.

    Combines four signals (TASK-027):
      * ``sys.stdin.isatty()`` — no prompting on a non-TTY stdin (piped/closed),
      * ``getattr(args, "no_input", False)`` — the subcommand's own ``--no-input``
        (``dest="no_input"``, ``default=False``),
      * ``getattr(args, "no_input_top", False)`` — the TOP-LEVEL ``--no-input``
        (``dest="no_input_top"``, ``default=argparse.SUPPRESS`` so absent when
        not passed),
      * ``os.environ.get("PIPELINE_NO_INPUT")`` / ``_EXTERNAL_NO_INPUT`` — an
        external caller (CI, a wrapper pipeline) forcing non-interactive mode.

    Any of the four disabling ⇒ no prompting. ``_EXTERNAL_NO_INPUT`` is read
    alongside the live env var so a wrapper that set ``PIPELINE_NO_INPUT``
    before exec'ing ``run.py`` is honoured even before the env-bridge block
    runs.
    """
    try:
        if not sys.stdin.isatty():
            return False
    except (AttributeError, ValueError, OSError):
        return False
    if getattr(args, "no_input", False):
        return False
    if getattr(args, "no_input_top", False):
        return False
    env_ni = os.environ.get("PIPELINE_NO_INPUT")
    if env_ni is None:
        env_ni = _EXTERNAL_NO_INPUT
    if env_ni is not None and env_ni.strip() in ("1", "true", "yes"):
        return False
    return True


def _run_start_wizard(args):
    """Interactive start wizard (TASK-027): prompt for the missing start fields.

    Walks ``task_id`` → ``request``/``--file`` → ``effort`` → ``auto`` →
    ``interview``, prompting ONLY for fields not already present/true on the
    incoming ``args`` (so ``./run.py start 005 "req" --effort troop-monke``
    skips every prompt it can). Every ``Prompt.ask`` / ``Confirm.ask`` call
    passes ``console=_rich_console(args)`` so the prompts honour the colour
    gate and go to stderr (C1).

    Returns:
      * the resolved ``Namespace`` on success (mutated in place — the same
        object passed in, with ``task_id``/``request``/``file``/``effort``/
        ``auto``/``interview`` filled in),
      * ``None`` on an EOF/Ctrl-C abort (the human bailed out),
      * ``_WIZARD_ERROR`` on a hard validation failure (``--file`` path
        missing) — distinct from ``None`` so ``main`` can branch cleanly.

    Never calls ``_warn_stale_task_files`` — that is ``main``'s job, after the
    wizard has resolved a real ``task_id``.
    """
    console = _rich_console(args)
    try:
        # task_id
        if not getattr(args, "task_id", None):
            args.task_id = Prompt.ask("[bold]task id[/bold]", console=console,
                                      default="").strip() or None
            if not args.task_id:
                return _WIZARD_ERROR
        # request / --file
        if not getattr(args, "request", None) and not getattr(args, "file", None):
            source = Prompt.ask(
                "[bold]request source[/bold] — [dim]type the request, or "
                "[cyan]file:<path>[/cyan] to read from a file[/dim]",
                console=console, default="")
            if source.startswith("file:"):
                args.file = source[len("file:"):].strip()
            else:
                args.request = source.strip() or None
        # If --file was provided (or just set), verify the path exists BEFORE
        # opening it — a missing path is a hard failure, not an abort.
        if getattr(args, "file", None):
            if not Path(args.file).is_file():
                print(f"--file not found: {args.file}", file=sys.stderr)
                return _WIZARD_ERROR
        # effort
        if not getattr(args, "effort", None):
            effort = Prompt.ask(
                "[bold]effort level[/bold] — [dim]scout-monke / troop-monke / "
                "barrel-monke (empty = let the graph ask at the checkpoint)[/dim]",
                console=console, default="")
            effort = effort.strip() or None
            if effort and effort in ("scout-monke", "troop-monke", "barrel-monke"):
                args.effort = effort
            elif effort:
                print(f"ignoring unknown effort {effort!r}; "
                      f"the graph will ask at the checkpoint", file=sys.stderr)
        # auto
        if not getattr(args, "auto", False):
            args.auto = Confirm.ask("[bold]--auto[/bold] (skip human checkpoints)?",
                                    console=console, default=False)
        # interview
        if not getattr(args, "interview", False):
            args.interview = Confirm.ask(
                "[bold]--interview[/bold] (run the intake interview even under --auto)?",
                console=console, default=False)
    except (EOFError, KeyboardInterrupt):
        return None
    return args


def _print_top_help(args=None) -> None:
    """Print the concise, examples-first top-level help (no-args + ``help``).

    Amendment B/D (TASK-027): takes an explicit ``args`` namespace so the
    colour gate ``_use_color(args, stream=sys.stdout)`` reaches the top-level
    help. The *channel* stays stdout (clig.dev "primary output to stdout" +
    TASK-027-brief §7 item 7 + the unmodified
    ``tests/test_cli_ux.py::TestHelpSubcommand::test_help_no_topic_prints_examples``
    all agree); only the *colour gate* becomes args-aware.

    When colour is off (non-TTY stdout / ``NO_COLOR`` / ``--no-color`` /
    ``TERM=dumb``) the existing ``print()`` branch runs verbatim, byte-for-byte.
    When colour is on, the same example strings are wrapped in ``Text(...)`` and
    rendered via a stdout-backed ``_rich_console`` so markup characters in the
    examples are escaped (no accidental rich markup interpretation).
    """
    if not _use_color(args, stream=sys.stdout):
        # Plain branch — byte-for-byte the original print() sequence.
        print("MonkeForge pipeline CLI — examples:")
        print()
        print("  ./run.py start 005 \"rendere pubblicabile la classe Mystic\"")
        print("  ./run.py start 006 --file docs/tasks/TASK-006-brief.md --auto")
        print("  ./run.py resume 005                       # resume after crash/suspend")
        print("  ./run.py resume 005 --answer ok           # answer a pending pause")
        print("  ./run.py redo   005 --from debate         # redo a phase, reuse artifacts")
        print("  ./run.py status 005                       # current node / batch / pause")
        print("  ./run.py status 005 --json | jq           # machine-readable status")
        print("  ./run.py doctor 005                       # failures, degradations, liveness")
        print("  ./run.py graph                            # print the graph definition")
        print("  ./run.py metrics 005                      # durations, retries, escalations")
        print()
        print("Run `./run.py <command> -h` for per-command options.")
        print(f"Source & issues: {_SUPPORT_URL}")
        return
    # Rich branch — stdout-backed console, every example wrapped in Text(...) so
    # markup characters in the examples are escaped (no accidental rich markup).
    console = _rich_console(args, stream=sys.stdout)
    console.print(Text("MonkeForge pipeline CLI — examples:", style="bold"))
    console.print()
    console.print(Text("  ./run.py start 005 \"rendere pubblicabile la classe Mystic\""))
    console.print(Text("  ./run.py start 006 --file docs/tasks/TASK-006-brief.md --auto"))
    console.print(Text("  ./run.py resume 005                       # resume after crash/suspend"))
    console.print(Text("  ./run.py resume 005 --answer ok           # answer a pending pause"))
    console.print(Text("  ./run.py redo   005 --from debate         # redo a phase, reuse artifacts"))
    console.print(Text("  ./run.py status 005                       # current node / batch / pause"))
    console.print(Text("  ./run.py status 005 --json | jq           # machine-readable status"))
    console.print(Text("  ./run.py doctor 005                       # failures, degradations, liveness"))
    console.print(Text("  ./run.py graph                            # print the graph definition"))
    console.print(Text("  ./run.py metrics 005                      # durations, retries, escalations"))
    console.print()
    console.print(Text("Run `./run.py <command> -h` for per-command options.",
                       style="dim"))
    console.print(Text(f"Source & issues: {_SUPPORT_URL}", style="dim"))


def main(argv=None) -> int:
    global _EXTERNAL_NO_INPUT
    _EXTERNAL_NO_INPUT = os.environ.get("PIPELINE_NO_INPUT")
    p = argparse.ArgumentParser(parents=[_no_color_parent])
    p.add_argument("--version", action="version",
                   version=f"monkeforge {_VERSION}")
    # TOP-LEVEL --no-input (TASK-027 item 12): dest="no_input_top" with
    # ``default=argparse.SUPPRESS`` so the attribute is ABSENT when the flag is
    # not passed — ``_interactive_gate`` then treats absence as "prompting
    # allowed". Separate from each subparser's own --no-input (dest="no_input",
    # default=False), which remain textually unchanged below.
    p.add_argument("--no-input", dest="no_input_top", action="store_true",
                   default=argparse.SUPPRESS,
                   help="never prompt (top-level; applies to every subcommand)")
    sub = p.add_subparsers(dest="cmd", required=False)

    s = sub.add_parser("start", parents=[_no_color_parent])
    s.add_argument("task_id", nargs="?", default=None)
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
    s.add_argument("--no-input", action="store_true",
                   help="never prompt; resolve test suites non-interactively")
    r = sub.add_parser("resume", parents=[_no_color_parent],
                       help="resume a paused or interrupted run, optionally "
                            "answering the pending pause")
    r.add_argument("task_id")
    r.add_argument("--answer",
                   help="answer to the pending pause (required on non-TTY / --no-input)")
    r.add_argument("--no-input", action="store_true",
                   help="never prompt; --answer is required")
    rd = sub.add_parser("redo", parents=[_no_color_parent],
                        help="re-run a phase reusing existing artifacts "
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
    rd.add_argument("--no-input", action="store_true",
                    help="never prompt; resolve test suites non-interactively")
    rs = sub.add_parser("reset", parents=[_no_color_parent],
                        help="delete the checkpoint state for a task "
                        "so it can be started fresh again")
    rs.add_argument("task_id")
    st = sub.add_parser("status", parents=[_no_color_parent]); st.add_argument("task_id")
    st.add_argument("--json", dest="json", action="store_true",
                    help="emit a single JSON object on stdout "
                         "(next/batches/paused/options); errors surface as "
                         "a JSON {\"error\": ...} object with a non-zero exit")
    dr = sub.add_parser("doctor", parents=[_no_color_parent],
                        help="what went wrong: failures, degradations, "
                        "unhealthy agents, notification & liveness")
    dr.add_argument("task_id")
    sub.add_parser("graph", parents=[_no_color_parent])
    nd = sub.add_parser("notify-daemon", parents=[_no_color_parent],
                        help="persistent notification daemon "
                        "(priority queue + rate limiter)")
    nd.add_argument("--status", action="store_true", help="check daemon heartbeat + queue")
    nd.add_argument("--stop", action="store_true", help="send SIGTERM for clean shutdown")
    mt = sub.add_parser("metrics", parents=[_no_color_parent],
                        help="aggregate events.jsonl into a metrics report "
                        "(durations, failures, retries, escalations)")
    mt.add_argument("task_id", nargs="?", default=None,
                    help="task id to report on; omit with --all for a cross-task summary")
    mt.add_argument("--all", dest="all", action="store_true",
                    help="aggregate every task into a single summary")
    hp = sub.add_parser("help", parents=[_no_color_parent],
                        help="show help for a command or the examples")
    hp.add_argument("topic", nargs="?", default=None,
                    help="command name to show help for")
    args = p.parse_args(argv)

    # No subcommand: print concise, examples-first help.
    # ``required=False`` on the subparsers lets ``./run.py`` with no args
    # reach here instead of argparse erroring out — friendlier entry point.
    # TASK-027: on a TTY with no --no-input/PIPELINE_NO_INPUT, enter the start
    # wizard instead of just printing help. Amendment C: the wizard's start
    # namespace is built by re-parsing an empty ``start`` argv INTO the
    # already-parsed ``args`` (``sub.choices["start"].parse_args([], namespace=args)``)
    # so ``no_color`` and ``no_input_top`` (set by the top-level parse) carry
    # over — a bare ``argparse.Namespace()`` would silently drop them.
    if args.cmd is None:
        if _interactive_gate(args):
            sub.choices["start"].parse_args([], namespace=args)
            wizard_ns = _run_start_wizard(args)
            if wizard_ns is None:
                return 0  # EOF/Ctrl-C — human bailed out of the wizard.
            if wizard_ns is _WIZARD_ERROR:
                return 2  # hard validation failure (--file missing, etc.).
            args = wizard_ns
            args.cmd = "start"
        else:
            _print_top_help(args)
            return 0

    if args.cmd == "help":
        if args.topic is None:
            _print_top_help(args)
            return 0
        if args.topic in sub.choices:
            sub.choices[args.topic].print_help()
            return 0
        print(f"error: unknown help topic {args.topic!r}", file=sys.stderr)
        return 2

    if args.cmd == "notify-daemon":
        from pipeline_graph import notify_daemon as nd_mod
        if args.stop:
            return nd_mod.stop()
        if args.status:
            return nd_mod.status()
        return nd_mod.run_daemon()

    if args.cmd == "metrics":
        return _metrics(args)

    # Early DB guard + answer validation for resume/redo (items 19-21).
    # The checkpointer must not be opened on a missing DB, and the resume
    # answer is validated (or picked on a TTY) before preflight and before
    # the notify/bot daemons are started — no point launching sidecar
    # processes for an answer that will be rejected at the CLI edge.
    if args.cmd == "resume":
        if not C.CHECKPOINT_DB.exists():
            print(f"no checkpoint DB found at {C.CHECKPOINT_DB}", file=sys.stderr)
            return 1
        # Compute the colour gate once for the resume pause-handling block so
        # ``resume --no-color`` / ``NO_COLOR`` / ``TERM=dumb`` / non-TTY stderr
        # all suppress ANSI in the interactive picker chrome (C5/C9).
        _resume_color = _use_color(args, stream=sys.stderr)
        with open_checkpointer() as _cp:
            _graph = build_graph(_cp)
            _snap = _graph.get_state(_thread(args.task_id))
        if _snap.interrupts:
            _data = _snap.interrupts[0].value
            _options = _pending_options(_snap)
            _router_error = bool(_data.get("router_error", False))
            _stage = str(_data.get("stage", "")).lower()
            _reason = str(_data.get("reason", "")).lower()
            _intake = "intake" in _stage or "interviewer" in _reason
            _free_text = not _options and not _intake and not _router_error
            if args.answer is not None:
                _err = _validate_answer(
                    args.answer, _options,
                    free_text_allowed=_free_text,
                    router_error=_router_error, intake=_intake)
                if _err:
                    print(_err, file=sys.stderr)
                    return 2
            elif not _interactive_gate(args):
                print("error: --answer is required on a non-TTY (or with --no-input); "
                      "rerun with an answer from the pause menu", file=sys.stderr)
                return 2
            else:
                args.answer = _tty_pick(_options or [], _data, color=_resume_color)
        # No pending interrupt: args.answer stays None; the resume handler
        # below treats that as "continue at the next node".

    if args.cmd == "redo":
        if not C.CHECKPOINT_DB.exists():
            print(f"no checkpoint DB found at {C.CHECKPOINT_DB}", file=sys.stderr)
            return 1

    # status --json: handle before preflight / open_checkpointer so its own
    # try/except wraps the entire checkpointer-open + graph-build + state-read
    # sequence (item 41) and it never touches the plain-text status branch
    # (item 44 — that branch is left unchanged further below).
    if args.cmd == "status" and getattr(args, "json", False):
        return _status_json(args)

    problems = C.preflight()
    if problems and args.cmd in ("start", "resume"):
        # Preflight diagnostics are setup-time problems, not run output —
        # route them to stderr so a captured stdout stream stays clean
        # (item 46). The non-JSON status branch is NOT touched here.
        print("preflight failed:", file=sys.stderr)
        for x in problems:
            print(" -", x, file=sys.stderr)
        return 2

    if args.cmd in ("start", "resume", "redo"):
        _warn_if_notifications_off()
        _ensure_notify_daemon()
        _ensure_bot()

    # --- Test-suite resolution (pre-_drive) ---------------------------------
    # `start` and `redo` resolve test suites before driving so the gate is
    # configured by the time the implement/finalize nodes call run_repo_tests.
    # `redo --from visual` skips resolution: it reuses the built UI and only
    # redoes render + visual gate, so the test-suite config from the original
    # run must stand (re-resolving could re-prompt and change the gate mid-run).
    # `resume` NEVER resolves pre-_drive (R6 B1): it re-enters at an arbitrary
    # interrupt stage where the suites may already have been used, so the
    # sentinel + existing config from the original start/redo must hold. The
    # PIPELINE_NO_INPUT env bridge is still set on resume so any later
    # resolution (e.g. a fresh run_repo_tests on a cold process) honours it.
    # TASK-027 item 13: the bridge ORs the subcommand's ``no_input`` with the
    # top-level ``no_input_top`` so ``./run.py --no-input start 005`` works;
    # the ``else`` pops only when an EXTERNAL caller did not set
    # ``PIPELINE_NO_INPUT`` (``_EXTERNAL_NO_INPUT``), so a wrapper's value
    # survives the bridge.
    if args.cmd in ("start", "resume", "redo"):
        if getattr(args, "no_input", False) or getattr(args, "no_input_top", False):
            os.environ["PIPELINE_NO_INPUT"] = "1"
        elif not _EXTERNAL_NO_INPUT:
            os.environ.pop("PIPELINE_NO_INPUT", None)

    # --- TASK-027: start wizard + incomplete-start non-interactive guard -----
    # The wizard runs ONLY for an explicit ``start`` subcommand that is missing
    # task_id/request/file AND the CLI is interactive. It is positioned BEFORE
    # ``tr.resolve_test_suites`` (item 22) so a wizard-resolved task_id reaches
    # resolution, and so the incomplete-start error path (item 23) — which
    # requires a non-interactive CLI — fires before resolution too.
    if args.cmd == "start":
        _start_incomplete = (not getattr(args, "task_id", None)
                             or (not getattr(args, "request", None)
                                 and not getattr(args, "file", None)))
        if _start_incomplete and _interactive_gate(args):
            wizard_ns = _run_start_wizard(args)
            if wizard_ns is None:
                return 0  # EOF/Ctrl-C — human bailed out of the wizard.
            if wizard_ns is _WIZARD_ERROR:
                return 2  # hard validation failure (--file missing, etc.).
            # ``_run_start_wizard`` mutates ``args`` in place and returns it,
            # so ``wizard_ns is args``; no ``... or args`` pattern (item 21).
            assert wizard_ns is args
        elif _start_incomplete and not _interactive_gate(args):
            # Incomplete start on a non-interactive CLI: cannot prompt, cannot
            # proceed. Item 23: positioned above ``tr.resolve_test_suites``.
            print("error: provide a request as argument or via --file <path>",
                  file=sys.stderr)
            return 2

    if args.cmd == "start":
        tr.resolve_test_suites(task_id=args.task_id)
    elif args.cmd == "redo" and args.from_phase != "visual":
        tr.resolve_test_suites(task_id=args.task_id)

    if args.cmd == "reset":
        import sqlite3 as _sqlite3
        thread = f"task-{args.task_id}"
        if not C.CHECKPOINT_DB.exists():
            print(f"no checkpoint DB found at {C.CHECKPOINT_DB}", file=sys.stderr)
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
            # Standing pointer to the verbatim debate archive, if one exists
            # (created by the condenser when a debate was over budget).
            archive = C.DEBATES / f"DEBATE-{args.task_id}-full.md"
            if archive.exists():
                print(f"  verbatim debate archive: DEBATE-{args.task_id}-full.md")
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
                print("no run found for this task", file=sys.stderr)
                return 1
            # Reposition the graph as if the upstream node had just produced this
            # state, so it re-enters the chosen phase reusing existing artifacts.
            if args.from_phase == "visual":
                # Reuse the built + committed UI; redo only render + visual gate.
                reset = {"escalation": "", "finished": False, "ux_render_cycle": 0,
                         "visual_verdict": "", "visual_blockers": 0,
                         "render_facts": "{}", "visual_shipped_blocked": False}
                as_node, nxt = "close_batch", "ux_render"
                reuse = "the built UI"
            else:
                reset = {"escalation": "", "finished": False, "debate_round": 0,
                         "debate_round_bonus": 0, "reviewer_verdict": "", "open_blockers": 0,
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
                         "fix_cycle": 0, "test_fix_attempt": 0,
                         "test_fix_failures": [], "test_fix_summary": ""}
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
            return _drive(graph, args.task_id, payload, args=args)

        if args.cmd == "start":
            request = args.request
            if args.file:
                with open(args.file, "r", encoding="utf-8") as f:
                    request = f.read().strip()
            if not request:
                print("error: provide a request as argument or via --file <path>",
                      file=sys.stderr)
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
            if not snap.created_at:
                print(f"error: no run found for task {args.task_id}", file=sys.stderr)
                return 1
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

        return _drive(graph, args.task_id, payload, args=args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
