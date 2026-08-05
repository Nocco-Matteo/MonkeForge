"""Handoff of ``[BLOCKER:REQUIREMENTS]`` claims from debate → re-intake.

When the plan debate flags a brief-level gap, recommending
``./run.py redo <id> --from intake`` is useless unless the interviewer sees
*those* claims. This module persists them under ``TASKS/`` so re-intake is
targeted, not a gamble that the interviewer rediscovers the same hole.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from . import config as C


def requirements_gap_path(task_id: str) -> Path:
    return C.TASKS / f"TASK-{task_id}-requirements-gap.md"


def write_requirements_gap(
    task_id: str,
    claims: list[str],
    *,
    source: str = "debate requirements escalation",
) -> Path | None:
    """Write (or overwrite) the gap file. Empty claims → no write, returns None."""
    cleaned = [c.strip() for c in claims if c and str(c).strip()]
    if not cleaned:
        return None
    C.ensure_dirs()
    path = requirements_gap_path(task_id)
    ts = datetime.now(timezone.utc).isoformat()
    lines = [
        f"# REQUIREMENTS gaps from plan debate — TASK-{task_id}",
        "",
        "The plan debate raised these as `[BLOCKER:REQUIREMENTS]`. They are",
        "brief/intent gaps the proposer cannot invent. On re-intake the",
        "interviewer MUST ask the human about **each** gap before",
        "`INTAKE: COMPLETE`, then fold the answers into the brief contract.",
        "",
        f"Source: {source}",
        f"Recorded: {ts}",
        "",
    ]
    for i, claim in enumerate(cleaned, 1):
        lines += [f"## Gap {i}", "", claim, ""]
    path.write_text("\n".join(lines))
    return path


def read_requirements_gap(task_id: str) -> str:
    """Return gap file body, or empty string if absent."""
    path = requirements_gap_path(task_id)
    try:
        return path.read_text()
    except OSError:
        return ""


def clear_requirements_gap(task_id: str) -> None:
    try:
        requirements_gap_path(task_id).unlink(missing_ok=True)
    except OSError:
        pass


def gap_block_for_prompt(task_id: str) -> str:
    """Text for the intake prompt ``{requirements_gaps}`` placeholder."""
    body = read_requirements_gap(task_id).strip()
    if not body:
        return (
            "(none — first interview, or no REQUIREMENTS blockers were handed "
            "off from debate)"
        )
    return body


def ensure_gap_from_debate_file(task_id: str) -> list[str]:
    """If no gap file yet, extract REQUIREMENTS claims from the live debate.

    Used by ``redo --from intake`` *before* deleting debate artifacts so a
    stop→redo path still carries the claims even if the escalation node did
    not persist them (older runs / race).
    """
    existing = read_requirements_gap(task_id).strip()
    if existing:
        return []
    debate = C.DEBATES / f"DEBATE-{task_id}.md"
    full = C.DEBATES / f"DEBATE-{task_id}-full.md"
    text = ""
    for path in (debate, full):
        try:
            text = path.read_text()
            if text.strip():
                break
        except OSError:
            continue
    if not text.strip():
        return []
    from .condenser import latest_requirements_blockers

    claims = latest_requirements_blockers(text)
    if claims:
        write_requirements_gap(
            task_id,
            claims,
            source="extracted from debate file at redo --from intake",
        )
    return claims
