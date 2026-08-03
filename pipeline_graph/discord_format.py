"""Discord narration formatter — pure functions, no ``discord`` import.

This module is the single place that turns a pipeline event into the
``(title, description)`` pair pushed to Discord. It deliberately imports
nothing from ``discord``: the pipeline graph must be able to format
notifications without pulling the bot's dependencies into the graph, and
the bot imports this module too (it posts the same titles the webhook
pushes, so the two channels look the same to a human watching Discord).

The four-beat narration (convene → send → return → close) is produced by
``format_discord_line`` for the milestone kinds:

  - ``run_start``   → 🚀 run started            (close of the previous run /
                                                  open of this one)
  - ``agent_start`` → 📞 {monke} convenes       (convene beat)
  - ``agent_end``   → 📨 {monke} returns        (return beat, with elapsed)
  - ``run_paused``  → ⛔ needs you              (the run is waiting for a human)
  - ``run_end``     → ✅ complete               (close beat)

For every other kind the title falls back to the legacy
``TASK-{task} · {step or kind}`` shape — only the five milestone kinds
above are required by C1 to move away from that raw form.
"""
from __future__ import annotations

import re

from .notify_daemon import AGENT_IDENTITIES


# --- Per-agent identity lookup --------------------------------------------

def _monke_name(role: str) -> str:
    """Human-facing monke name for a role, never raising on an unknown role.

    Uses a ``.get(role, (role, ""))``-style lookup against
    ``AGENT_IDENTITIES``: an unrecognized role renders as the raw role string
    (its own name), not a bare ``?`` and never an exception.
    """
    name, _avatar = AGENT_IDENTITIES.get(role, (role, ""))
    return name or role


# --- Error humanisation ----------------------------------------------------

# Lines that start a Python traceback frame or an exception class line.
# Leading whitespace is allowed — traceback ``File "..."`` lines are indented.
_TRACEBACK_LINE_RE = re.compile(r"^\s*(Traceback \(most recent call last\)|Traceback)")
_FILE_LINE_RE = re.compile(r'^\s*File "')
# An exception-class line start: ``ValueError: ...``, ``TypeError: ...``,
# ``socket.error: ...``, etc. Matches CamelCase / dotted class names ending
# in ``Error`` or ``Exception`` (or ``Warning``) followed by ``:``.
_ERROR_LINE_START_RE = re.compile(
    r"^\s*[A-Za-z_][\w.]*(Error|Exception|Warning):\s"
)

_RESIDUAL_MARKERS = ("Traceback", 'File "')


def humanize_error(msg: str) -> str:
    """Strip traceback noise from an agent/step error message.

    Drops whole lines that START with ``Traceback``, ``File "``, or an
    exception-class line-start pattern (``ValueError: ...``), then
    additionally truncates any residual ``Traceback`` / ``File "`` substring
    found anywhere in the remaining text — a traceback that wrapped onto a
    continued line is cut at the marker rather than shown raw.

    Returns the cleaned text. A falsy ``msg`` is returned as-is (the caller
    decides what an empty error looks like).
    """
    if not msg:
        return msg or ""
    kept: list[str] = []
    for line in msg.splitlines():
        if _TRACEBACK_LINE_RE.match(line):
            continue
        if _FILE_LINE_RE.match(line):
            continue
        if _ERROR_LINE_START_RE.match(line):
            continue
        kept.append(line)
    text = "\n".join(kept)
    # Truncate at the first residual traceback / file marker anywhere in the
    # surviving text — a mid-line traceback fragment is noise, not signal.
    cut = len(text)
    for marker in _RESIDUAL_MARKERS:
        idx = text.find(marker)
        if 0 <= idx < cut:
            cut = idx
    if cut < len(text):
        text = text[:cut].rstrip() + " […truncated]"
    return text


# --- Title/description formatter ------------------------------------------

# Kinds whose titles MUST NOT be the raw ``TASK-{task} · {step or kind}``
# form (C1). Every other kind may keep the legacy shape.
_FOUR_BEAT_KINDS = frozenset(
    {"run_start", "agent_start", "agent_end", "run_paused", "run_end"}
)


def format_discord_line(kind: str, task: str, role: str, step: str,
                        msg: str, **extra) -> tuple[str, str]:
    """Return ``(title, description)`` for one pipeline event.

    The title is a human-readable one-liner; the description is the body of
    the Discord embed / push notification. Neither field is ever exactly
    ``f"TASK-{task} · {step or kind}"`` for the five four-beat kinds.

    ``**extra`` carries the rest of the event record (``duration_ms``,
    ``exit_code``, ``health``, …) so the formatter can surface elapsed time
    and other context without the caller having to pre-format it.
    """
    fallback_desc = msg or kind
    if kind == "run_start":
        title = f"🚀 TASK-{task} — run started"
        return title, fallback_desc
    if kind == "agent_start":
        name = _monke_name(role)
        title = f"📞 {name} convenes — TASK-{task}/{step or '?'}"
        return title, fallback_desc
    if kind == "agent_end":
        name = _monke_name(role)
        title = f"📨 {name} returns — TASK-{task}/{step or '?'}"
        duration_ms = extra.get("duration_ms")
        if isinstance(duration_ms, (int, float)) and duration_ms >= 0:
            seconds = int(duration_ms) // 1000
            desc = f"{fallback_desc} ({seconds}s)"
        else:
            desc = fallback_desc
        return title, desc
    if kind == "run_paused":
        title = f"⛔ TASK-{task} needs you — {step or 'paused'}"
        return title, fallback_desc
    if kind == "run_end":
        title = f"✅ TASK-{task} complete — {step or 'done'}"
        return title, fallback_desc
    # Non-four-beat kinds keep the legacy title shape.
    return f"TASK-{task} · {step or kind}", fallback_desc
