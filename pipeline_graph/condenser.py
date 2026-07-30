"""Deterministic debate-file condenser (pure string-in/string-out).

Collapses older ``## Round N — <Critic>`` sections of a ``DEBATE-{task_id}.md``
file to compact one-line markers, keeping the last ``keep_recent`` rounds
verbatim. No LLM calls, no third-party token libs, no file IO, no event
emission — those live in ``agents.py::run_agent()`` (see D1 in PLAN-003).

The only imports are ``re`` and the two verdict/blocker parsers from
``agents``; ``agents.py`` imports this module function-locally inside
``run_agent()`` to avoid an import-time cycle (D2).
"""
from __future__ import annotations

import re

from .agents import count_blockers, parse_verdict

# Verbatim from PLAN-003 §3 / nodes/debate.py:18. Group 1 = critic name
# ("Reviewer", "UX", "Reply", "Proposer"). Redefined here rather than imported
# from nodes/debate.py to keep this module free of the nodes subpackage (D3).
_SECTION_HEADER_RE = re.compile(r"^##\s+Round\s+\d+\s+—\s+(\w+)", re.MULTILINE)

# Pulls the integer round number out of a header match's full text.
_ROUND_NUM_RE = re.compile(r"Round\s+(\d+)")

# Critic sections worth a marker line. Reply/Proposer are the resolution
# signal, not critics — they are skipped when building markers but read for
# the "RESOLVED" resolution flag.
_CRITICS = ("Reviewer", "UX")
_REPLY_LIKE = ("Reply", "Proposer")


def estimate_tokens(text: str) -> int:
    """Cheap token estimate: ~4 chars per token. None/empty -> 0."""
    return len(text or "") // 4


def _parse_rounds(text: str) -> tuple[str, list[tuple[int, list[tuple[str, str]]]]]:
    """Split a debate file into (preamble, rounds).

    ``preamble`` is everything before the first ``## Round N —`` header (or the
    whole text if there are no headers). ``rounds`` is a list of
    ``(round_num, [(critic, body), ...])`` in first-seen order; sections that
    share a round number (e.g. a duplicated ``## Round 1 — Reviewer`` header, as
    in real DEBATE-001.md) are grouped together under that round, preserving
    encounter order. Each ``body`` is the verbatim slice from this header's
    start to the next header's start (or EOF), header line included.
    """
    text = text or ""
    matches = list(_SECTION_HEADER_RE.finditer(text))
    if not matches:
        return text, []
    preamble = text[: matches[0].start()]
    rounds: list[tuple[int, list[tuple[str, str]]]] = []
    round_index: dict[int, int] = {}
    for i, m in enumerate(matches):
        critic = m.group(1)
        rm = _ROUND_NUM_RE.search(m.group(0))
        assert rm is not None  # _SECTION_HEADER_RE matched `\d+`, so this always does
        rn = int(rm.group(1))
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.start() : end]
        if rn not in round_index:
            round_index[rn] = len(rounds)
            rounds.append((rn, []))
        rounds[round_index[rn]][1].append((critic, body))
    return preamble, rounds


def _collapse_round(round_num: int, sections: list[tuple[str, str]]) -> str:
    """Build the marker block for one collapsed round.

    One marker line per critic section (Reviewer/UX), keeping only the LAST
    section per critic name when a critic header repeats within the round
    (matches ``nodes/debate.py::_latest_section``'s "last wins" philosophy — a
    reviewer re-critiquing in the same round supersedes the earlier pass).
    Reply/Proposer sections are skipped as markers but scanned for the
    ``RESOLVED`` resolution flag.

    If the round has no critic section at all (only Reply/Proposer, or empty),
    emit a single deterministic ``[Round N — (no critic section) — condensed]``
    fallback line so the collapsed round is never silently empty.
    """
    last_body: dict[str, str] = {}
    order: list[str] = []
    for critic, body in sections:
        if critic in _CRITICS:
            if critic not in last_body:
                order.append(critic)
            last_body[critic] = body

    res = "unresolved"
    for critic, body in sections:
        if critic in _REPLY_LIKE and "RESOLVED" in body.upper():
            res = "all RESOLVED"
            break

    if not order:
        return f"[Round {round_num} — (no critic section) — condensed]\n\n"

    lines = [
        f"[Round {round_num} — {critic}: {parse_verdict(last_body[critic])}, "
        f"{count_blockers(last_body[critic])} blockers, {res} — condensed]"
        for critic in order
    ]
    return "\n".join(lines) + "\n\n"


def condense(text: str, keep_recent: int) -> str:
    """Collapse older rounds of a debate file, keeping the last ``keep_recent`` verbatim.

    - ``len(rounds) <= keep_recent`` -> return ``text`` unchanged (nothing to collapse).
    - ``keep_recent <= 0`` -> collapse ALL rounds, keep none verbatim (handled
      explicitly; ``rounds[:-0]`` would silently yield ``[]`` in Python).
    - Negative ``keep_recent`` is treated as ``0`` (defense-in-depth; config
      already clamps to 0, but the pure core must not corrupt on a negative).
    """
    preamble, rounds = _parse_rounds(text)
    if len(rounds) <= keep_recent:
        return text
    if keep_recent <= 0:
        old = rounds
        recent: list[tuple[int, list[tuple[str, str]]]] = []
    else:
        old = rounds[:-keep_recent]
        recent = rounds[-keep_recent:]

    parts: list[str] = [preamble]
    for n, secs in old:
        parts.append(_collapse_round(n, secs))
    for _n, secs in recent:
        for _critic, body in secs:
            parts.append(body)
    return "".join(parts)
