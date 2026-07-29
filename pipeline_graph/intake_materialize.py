"""Persist intake interviewer output when the CLI only prints to stdout (Gemini)."""
from __future__ import annotations

import re
from pathlib import Path

INTAKE_DOC_START = re.compile(
    r"^(##\s+Round\s+\d|#\s+TASK-\d+|\d+\.\s+Goal\s+)",
    re.MULTILINE | re.IGNORECASE,
)


def extract_before_marker(text: str, marker: str) -> str:
    idx = (text or "").upper().rfind(marker.upper())
    if idx == -1:
        return (text or "").strip()
    return text[:idx].strip()


def sanitize_agent_doc(text: str) -> str:
    """Drop Gemini tool preamble; keep from first intake section header."""
    m = INTAKE_DOC_START.search(text or "")
    if m:
        return text[m.start() :].strip()
    return (text or "").strip()


def materialize_intake_output(
    tid: str,
    rnd: int,
    out: str,
    *,
    intake_path: Path,
    brief_path: Path,
) -> bool:
    """Write intake.md or brief.md from agent stdout. Returns True if a file was written."""
    upper = (out or "").upper()
    if "INTAKE: COMPLETE" in upper:
        body = sanitize_agent_doc(extract_before_marker(out, "INTAKE: COMPLETE"))
        if not body:
            return False
        brief_path.write_text(body + "\n")
        return True
    if "INTAKE: QUESTIONS" in upper:
        body = sanitize_agent_doc(extract_before_marker(out, "INTAKE: QUESTIONS"))
        if not body:
            return False
        if intake_path.exists():
            intake_path.write_text(intake_path.read_text().rstrip() + "\n\n" + body + "\n")
        else:
            intake_path.write_text(body + "\n")
        return True
    return False
