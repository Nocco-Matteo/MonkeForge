"""Persist intake interviewer output when the CLI only prints to stdout (Gemini)."""
from __future__ import annotations

import re
from pathlib import Path

INTAKE_DOC_START = re.compile(
    r"^(UI-SURFACE:|##\s+Round\s+\d|#\s+TASK-\d+|##\s*1\.\s*Goal)",
    re.MULTILINE | re.IGNORECASE,
)

# Contract-brief shape required by pipeline_graph/prompts/intake.md (B).
# A status chat that ends with INTAKE: COMPLETE must not overwrite a seed.
_CONTRACT_CHECKS: list[tuple[str, re.Pattern[str]]] = [
    ("UI-SURFACE", re.compile(r"(?im)^UI-SURFACE:\s*(yes|no)\s*$")),
    ("Goal", re.compile(r"(?im)^##\s*1\.\s*Goal\b")),
    ("Definition of done", re.compile(r"(?im)^##\s*5\.\s*Definition of done\b")),
    ("Scope", re.compile(r"(?im)^##\s*6\.\s*Scope\b")),
]


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


def missing_contract_sections(text: str) -> list[str]:
    """Return names of required contract-brief sections absent from ``text``."""
    body = text or ""
    return [name for name, pat in _CONTRACT_CHECKS if not pat.search(body)]


def is_contract_brief(text: str) -> bool:
    """True when ``text`` looks like an intake (B) contract brief, not a chat note."""
    return not missing_contract_sections(text)


def _path_touched_since(path: Path, before_mtime: float | None) -> bool:
    """True if ``path`` exists and was created/updated after ``before_mtime``.

    ``before_mtime is None`` means the path did not exist before the agent ran;
    any existing file then counts as written this round (tool Write).
    """
    if not path.exists():
        return False
    now = path.stat().st_mtime
    if before_mtime is None:
        return True
    return now != before_mtime


def materialize_intake_output(
    tid: str,
    rnd: int,
    out: str,
    *,
    intake_path: Path,
    brief_path: Path,
    intake_mtime_before: float | None = None,
    brief_mtime_before: float | None = None,
) -> bool:
    """Write intake.md or brief.md from agent stdout. Returns True if a file was written.

    For ``INTAKE: COMPLETE``, the stdout body is written only when it passes
    :func:`is_contract_brief`. A status summary must never clobber an existing
    seed or a brief the agent already wrote via tools.

    When Cursor/GLM already wrote the intake/brief this round (mtime advanced
    vs the pre-agent snapshot), stdout is **not** applied — otherwise QUESTIONS
    get appended twice (tool Write + materialize append). Gemini-only stdout
    still materializes when the file was not touched.
    """
    upper = (out or "").upper()
    if "INTAKE: COMPLETE" in upper:
        if _path_touched_since(brief_path, brief_mtime_before):
            return False
        body = sanitize_agent_doc(extract_before_marker(out, "INTAKE: COMPLETE"))
        if not body or not is_contract_brief(body):
            return False
        brief_path.write_text(body + "\n")
        return True
    if "INTAKE: QUESTIONS" in upper:
        if _path_touched_since(intake_path, intake_mtime_before):
            return False
        body = sanitize_agent_doc(extract_before_marker(out, "INTAKE: QUESTIONS"))
        if not body:
            return False
        if intake_path.exists():
            intake_path.write_text(intake_path.read_text().rstrip() + "\n\n" + body + "\n")
        else:
            intake_path.write_text(body + "\n")
        return True
    return False
