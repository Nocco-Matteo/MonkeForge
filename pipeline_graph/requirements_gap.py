"""Handoff of ``[BLOCKER:REQUIREMENTS]`` claims from debate → re-intake.

When the plan debate flags a brief-level gap, recommending
``./run.py redo <id> --from intake`` is useless unless the interviewer sees
*those* claims. This module persists them under ``TASKS/`` so re-intake is
targeted, not a gamble that the interviewer rediscovers the same hole.

TASK-033: the gap file is now lifecycle-managed. A ``Status:`` line inside the
markdown file (``active``/``suspended``/``waived``) tracks the gap state so one
path + one parser serves every caller (no ``*.waived.md`` rename). Re-intake
archiving helpers (live debate → ``-full`` append-only, live intake →
``-history`` append-only) live here because the module already owns the
re-intake handoff contract and the ``-full``/intake paths.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from . import config as C


def requirements_gap_path(task_id: str) -> Path:
    return C.TASKS / f"TASK-{task_id}-requirements-gap.md"


# TASK-033: lifecycle status line inside the gap file. ``active`` is the
# default for legacy files written before this task (no Status line → treated
# as active for backward compat with `gap_block_for_prompt` callers).
_STATUS_LINE_RE = re.compile(
    r"^Status:\s*(active|suspended|waived)\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# A ``## Gap N`` block header — used to count active gaps (the I3 answer-count
# threshold) and to delimit claim/evidence/impact blocks when re-reading.
_GAP_HEADER_RE = re.compile(r"^##\s+Gap\s+\d+", re.MULTILINE)


def _normalize_item(item) -> dict:
    """Coerce a legacy string item or a structured dict into ``{claim, ...}``."""
    if isinstance(item, dict):
        return {
            "claim": str(item.get("claim", "")).strip(),
            "evidence": str(item.get("evidence", "")).strip(),
            "impact": str(item.get("impact", "")).strip(),
        }
    return {"claim": str(item).strip(), "evidence": "", "impact": ""}


def _parse_existing_gaps(body: str) -> list[dict]:
    """Read claim/evidence/impact out of an existing gap file body.

    Each ``## Gap N`` block contributes one item; the claim is the first
    non-blank line after the header, ``Evidence:``/``Impact:`` lines (when
    present) fill the matching fields. Returns items in file order.
    """
    items: list[dict] = []
    blocks = re.split(r"^##\s+Gap\s+\d+", body, flags=re.MULTILINE)
    # The first split is the preamble (header + Status); skip it.
    for block in blocks[1:]:
        claim = ""
        evidence = ""
        impact = ""
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.lower().startswith("evidence:"):
                evidence = stripped.split(":", 1)[1].strip()
                continue
            if stripped.lower().startswith("impact:"):
                impact = stripped.split(":", 1)[1].strip()
                continue
            if not claim:
                claim = stripped
        if claim:
            items.append({"claim": claim, "evidence": evidence, "impact": impact})
    return items


def write_requirements_gap(
    task_id: str,
    items,
    *,
    source: str = "debate requirements escalation",
) -> Path | None:
    """Write (or merge into) the gap file. Empty claims → no write, returns None.

    TASK-033: ``items`` accepts ``list[str]`` (legacy) or ``list[dict]`` with
    keys ``claim``/``evidence``/``impact``. Items are merged by normalized claim
    (union with existing entries; latest Evidence/Impact wins) so a later
    escalation never blind-overwrites prior gaps. The file always carries a
    ``Status: active`` line so a suspended/waived gap re-escalation reactivates
    it in place.
    """
    normalized = [_normalize_item(it) for it in (items or [])]
    cleaned = [it for it in normalized if it["claim"]]
    if not cleaned:
        return None
    C.ensure_dirs()
    path = requirements_gap_path(task_id)
    # Function-local import to avoid the condenser↔requirements_gap cycle
    # (condenser imports from nodes.debate; this module is imported widely).
    from .condenser import _normalize_claim

    # Merge with existing entries (union by normalized claim).
    by_norm: dict[str, dict] = {}
    order: list[str] = []
    existing_body = ""
    try:
        existing_body = path.read_text()
    except OSError:
        pass
    if existing_body.strip():
        for it in _parse_existing_gaps(existing_body):
            norm = _normalize_claim(it["claim"])
            if norm and norm not in by_norm:
                by_norm[norm] = it
                order.append(norm)
    for it in cleaned:
        norm = _normalize_claim(it["claim"])
        if not norm:
            continue
        if norm in by_norm:
            # Latest Evidence/Impact wins (non-empty overrides empty / prior).
            prior = by_norm[norm]
            if it["evidence"]:
                prior["evidence"] = it["evidence"]
            if it["impact"]:
                prior["impact"] = it["impact"]
            # Keep the latest claim wording for display.
            prior["claim"] = it["claim"]
        else:
            by_norm[norm] = dict(it)
            order.append(norm)
    if not order:
        return None

    ts = datetime.now(timezone.utc).isoformat()
    lines = [
        f"# REQUIREMENTS gaps from plan debate — TASK-{task_id}",
        "",
        "The plan debate raised these as `[BLOCKER:REQUIREMENTS]`. They are",
        "brief/intent gaps the proposer cannot invent. On re-intake the",
        "interviewer MUST ask the human about **each** gap before",
        "`INTAKE: COMPLETE`, then fold the answers into the brief contract.",
        "",
        "Status: active",
        f"Source: {source}",
        f"Recorded: {ts}",
        "",
    ]
    for i, norm in enumerate(order, 1):
        it = by_norm[norm]
        lines += [f"## Gap {i}", "", it["claim"], ""]
        if it["evidence"]:
            lines += [f"Evidence: {it['evidence']}"]
        if it["impact"]:
            lines += [f"Impact: {it['impact']}"]
        if it["evidence"] or it["impact"]:
            lines.append("")
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


def gap_status(task_id: str) -> str:
    """Return ``active``/``suspended``/``waived``/``""``.

    ``""`` (no Status line) is treated as ``active`` by callers for backward
    compat with gap files written before this task — see ``gap_block_for_prompt``.
    """
    body = read_requirements_gap(task_id)
    if not body.strip():
        return ""
    m = _STATUS_LINE_RE.search(body)
    if not m:
        return ""
    return m.group(1).lower()


def set_gap_status(task_id: str, status: str) -> None:
    """Rewrite the gap file toggling the ``Status:`` line (no-op if absent).

    A file with no prior Status line gets one inserted after the header block
    (so a legacy active gap can be suspended/waived in place).
    """
    body = read_requirements_gap(task_id)
    if not body.strip():
        return
    status_l = status.lower()
    if _STATUS_LINE_RE.search(body):
        body = _STATUS_LINE_RE.sub(f"Status: {status_l}", body)
    else:
        # Insert the Status line after the first paragraph (the descriptive
        # header), before the Source/Recorded lines if present.
        lines = body.splitlines()
        out: list[str] = []
        inserted = False
        for line in lines:
            out.append(line)
            if not inserted and line.lower().startswith("interviewer must ask"):
                out.append(f"Status: {status_l}")
                inserted = True
        if not inserted:
            # Fallback: insert after the first non-empty line that is not the title.
            out = []
            seen_title = False
            for line in lines:
                out.append(line)
                if not inserted and line.strip() and seen_title and not line.startswith("#"):
                    out.append(f"Status: {status_l}")
                    inserted = True
                if line.startswith("#"):
                    seen_title = True
            if not inserted:
                out.insert(1, f"Status: {status_l}")
        body = "\n".join(out)
    requirements_gap_path(task_id).write_text(body)


def waive_requirements_gap(task_id: str) -> None:
    set_gap_status(task_id, "waived")


def suspend_requirements_gap(task_id: str) -> None:
    set_gap_status(task_id, "suspended")


def gap_block_for_prompt(task_id: str) -> str:
    """Text for the intake prompt ``{requirements_gaps}`` placeholder.

    Returns the body only when the gap is active (or the file is a legacy
    active file with no Status line); otherwise the ``(none …)`` sentinel so
    the interviewer does not re-ask waived/suspended gaps.
    """
    status = gap_status(task_id)
    if status in ("waived", "suspended"):
        return (
            "(none — first interview, or no REQUIREMENTS blockers were handed "
            "off from debate)"
        )
    body = read_requirements_gap(task_id).strip()
    if not body:
        return (
            "(none — first interview, or no REQUIREMENTS blockers were handed "
            "off from debate)"
        )
    return body


def n_active_gaps(task_id: str) -> int:
    """Count of ``## Gap N`` entries when the gap is active, else 0.

    A legacy file with no Status line (``gap_status`` returns ``""``) is
    treated as active for backward compat with gap files written before this
    task; ``suspended``/``waived`` gaps contribute 0 (they are not live
    blockers the interviewer must answer).
    """
    status = gap_status(task_id)
    if status in ("suspended", "waived"):
        return 0
    body = read_requirements_gap(task_id)
    if not body.strip():
        return 0
    # status "" (legacy active) or "active" → count gaps.
    return len(_GAP_HEADER_RE.findall(body))


def ensure_gap_from_debate_file(task_id: str) -> list[str]:
    """Reconcile the gap file with the live/``-full`` debate and return claims.

    TASK-033 lifecycle rules:
    - file exists, status ``suspended`` → reactivate (``set_gap_status(active)``)
      and return the parsed claim strings;
    - file exists, status ``active`` → return the parsed claim strings;
    - file exists, status ``waived`` → return ``[]`` (waived is terminal; the
      operator must re-escalate from debate rather than silently re-open intake);
    - no file → extract via ``latest_requirements_blockers_structured`` from the
      live/``-full`` debate, ``write_requirements_gap`` with the structured
      items, return the claim strings (or ``[]`` when nothing is extractable).

    Used by ``redo --from intake`` *before* archiving debate artifacts so a
    stop→redo path still carries the claims even if the escalation node did
    not persist them (older runs / race).
    """
    body = read_requirements_gap(task_id)
    if body.strip():
        status = gap_status(task_id)
        if status == "waived":
            return []
        if status == "suspended":
            set_gap_status(task_id, "active")
        claims = [it["claim"] for it in _parse_existing_gaps(body)]
        return [c for c in claims if c.strip()]

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
    from .condenser import latest_requirements_blockers_structured

    structured = latest_requirements_blockers_structured(text)
    if not structured:
        return []
    write_requirements_gap(
        task_id,
        structured,
        source="extracted from debate file at redo --from intake",
    )
    return [d["claim"] for d in structured]


# --- re-intake archiving helpers (TASK-033) --------------------------------


def _archive_snapshot_header(label: str) -> str:
    return (
        f"\n\n=== {label} snapshot at "
        f"{datetime.now(timezone.utc).isoformat()} UTC ===\n\n"
    )


def archive_live_debate_for_reintake(task_id: str) -> None:
    """Append the live debate body to ``DEBATE-{id}-full.md`` then delete live.

    Append-only (mirrors ``agents.py`` 231–239 pre-condensation snapshot
    pattern): the ``-full`` archive accumulates every re-intake cycle's debate
    so a later stop/redo can still find the claims. Never touches ``-full``.
    """
    live = C.DEBATES / f"DEBATE-{task_id}.md"
    full = C.DEBATES / f"DEBATE-{task_id}-full.md"
    if not live.exists():
        return
    body = live.read_text()
    if not body.strip():
        live.unlink(missing_ok=True)
        return
    if full.exists():
        with full.open("a") as af:
            af.write(_archive_snapshot_header("re-intake") + body)
    else:
        full.write_text(body)
    live.unlink(missing_ok=True)


def archive_intake_for_reintake(task_id: str) -> None:
    """Append the live intake body to ``TASK-{id}-intake-history.md`` then delete.

    Append-only: the history archive accumulates every re-intake cycle's
    interview transcript so I3 sees only the fresh file's answers (no false
    pass from a prior cycle's ``**A:**`` markers).
    """
    live = C.TASKS / f"TASK-{task_id}-intake.md"
    history = C.TASKS / f"TASK-{task_id}-intake-history.md"
    if not live.exists():
        return
    body = live.read_text()
    if not body.strip():
        live.unlink(missing_ok=True)
        return
    if history.exists():
        with history.open("a") as af:
            af.write(_archive_snapshot_header("re-intake") + body)
    else:
        history.write_text(body)
    live.unlink(missing_ok=True)
