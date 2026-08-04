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


# --- Blocker detail packing (multi-message Discord report) -----------------
# Discord embed description hard-limit is 4096; the notify daemon clamps at
# 4000. Pack under a safer ceiling so a packed body never relies on mid-string
# truncation. Unit of packing = one full numbered claim line (never mid-claim).
# Detail messages use 📋; the return summary keeps 📨 so the two beats differ.

EMBED_DESC_SAFE = 3900


def number_blocker_claims(claims: list[str]) -> list[str]:
    """``1. <claim>``, ``2. <claim>``, … — readable index before any B# id."""
    return [f"{i}. {c}" for i, c in enumerate(claims, 1) if str(c).strip()]


def _split_oversized_line(line: str, max_len: int) -> list[str]:
    """Split one line that alone exceeds ``max_len`` into whole-line chunks.

    Prefers newline boundaries; falls back to hard character chunks with a
    ``(cont.)`` marker. Pathological only — debate prompts keep claims short.
    """
    body_max = max(32, max_len - len(" (cont.)"))
    # Preserve a leading ``N. `` prefix on continuation chunks when present.
    prefix = ""
    body = line
    m = re.match(r"^(\d+\.\s+)", line or "")
    if m:
        prefix = m.group(1)
        body = line[m.end():]
        body_max = max(32, max_len - len(prefix) - len(" (cont.)"))
    lines = (body or "").splitlines() or [body or ""]
    chunks: list[str] = []
    buf = ""
    for piece in lines:
        candidate = f"{buf}\n{piece}" if buf else piece
        if len(prefix) + len(candidate) <= body_max:
            buf = candidate
            continue
        if buf:
            chunks.append(buf)
            buf = piece
        else:
            text = piece
            while len(prefix) + len(text) > body_max:
                take = body_max - len(prefix)
                chunks.append(text[:take])
                text = text[take:]
            buf = text
    if buf:
        chunks.append(buf)
    out: list[str] = []
    for i, piece in enumerate(chunks):
        suffix = " (cont.)" if i < len(chunks) - 1 else ""
        out.append(f"{prefix}{piece}{suffix}")
    return out or [line or "(empty)"]


def pack_blocker_bodies(claims: list[str],
                        max_len: int = EMBED_DESC_SAFE) -> list[str]:
    """Pack numbered blocker lines into embed bodies under ``max_len``.

    Numbers claims as ``1. …``, ``2. …`` then packs: if appending the next
    line would exceed ``max_len``, flush and start a new body. Never truncates
    mid-claim except the oversized-single-line path.

    Returns a list of body strings (empty when there are no claims).
    """
    lines = number_blocker_claims([str(c) for c in (claims or [])])
    if not lines:
        return []
    parts: list[str] = []
    cur = ""
    for line in lines:
        if len(line) > max_len:
            if cur:
                parts.append(cur)
                cur = ""
            parts.extend(_split_oversized_line(line, max_len))
            continue
        candidate = f"{cur}\n{line}" if cur else line
        if cur and len(candidate) > max_len:
            parts.append(cur)
            cur = line
        else:
            cur = candidate
    if cur:
        parts.append(cur)
    return parts


def format_blocker_detail_title(role: str, task: str, step: str,
                                index: int, total: int) -> str:
    """Detail-beat title (📋). Show ``part i/k`` only when split across msgs."""
    name = _monke_name(role)
    if total > 1:
        return (
            f"📋 {name} — blockers · part {index}/{total} "
            f"· TASK-{task}/{step or '?'}"
        )
    return f"📋 {name} — blockers · TASK-{task}/{step or '?'}"


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
        # Prefer VERDICT in the title when present (return beat is the place
        # humans look for REJECT / blocker counts — not a later Council step_end).
        verdict = extra.get("verdict")
        if isinstance(verdict, str) and verdict and verdict != "UNKNOWN":
            title = f"📨 {name} returns — {verdict} · TASK-{task}/{step or '?'}"
            blockers = extra.get("blockers")
            if verdict == "REJECT" and isinstance(blockers, int) and blockers > 0:
                title = (
                    f"📨 {name} returns — {verdict}, {blockers} blocker(s) "
                    f"· TASK-{task}/{step or '?'}"
                )
        else:
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
