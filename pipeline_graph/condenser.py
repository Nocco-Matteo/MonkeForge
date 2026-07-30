"""Deterministic debate-file condenser.

Pure string-in/string-out module: no `config`/`events`/`nodes` imports, no
filesystem IO, no LLM calls. The integration point is `run_agent()` in
`agents.py`, which reads `DEBATE-{task_id}.md`, calls `condense()`, and writes
the result back in place when a role's token budget is exceeded.

Older debate rounds are collapsed to compact one-line-per-critic markers
(verdict, blocker count, resolution); the last `keep_recent` rounds are
re-emitted verbatim (byte-identical bodies, headers included).

Imports are deliberately limited to `re` and the two parser helpers from
`agents.py`. `agents.py` imports this module function-locally inside
`run_agent()` (not at module top) to avoid an import-time cycle: `agents.py`
defines `parse_verdict`/`count_blockers` *after* `run_agent`, so a top-level
`from .condenser import ...` in `agents.py` would re-enter `condenser.py`
before those names exist.
"""
from __future__ import annotations

import re

from .agents import parse_verdict, count_blockers

# Verbatim from nodes/debate.py:18 / brief §3. Duplicated (not imported) so this
# module stays pure — pulling nodes/debate.py would drag the whole nodes
# subpackage and its deps in (see plan D3).
_SECTION_HEADER_RE = re.compile(r"^##\s+Round\s+\d+\s+—\s+(\w+)", re.MULTILINE)

# Pulls the integer out of a header like "## Round 12 — Reviewer".
_ROUND_NUM_RE = re.compile(r"Round\s+(\d+)")

# Critics worth a marker line. Reply/Proposer sections are the resolution
# signal, not critics — they are inspected for "RESOLVED" but emit no marker.
_CRITICS = ("Reviewer", "UX")
_RESOLUTION_SECTIONS = ("Reply", "Proposer")


def estimate_tokens(text: str) -> int:
    """Cheap token estimate: 4 chars per token. None/empty → 0."""
    return len(text or "") // 4


def _parse_rounds(
    text: str,
) -> tuple[str, list[tuple[int, list[tuple[str, str]]]]]:
    """Split a debate file into (preamble, rounds).

    `rounds` is a list of `(round_num, [(critic, body), ...])` grouped by round
    number in first-seen order. Each section body runs from its header's start
    to the next header's start (or EOF), so it includes the header line itself
    and is byte-identical to the input slice. Duplicate headers for the same
    round (real `DEBATE-001.md` has two `## Round 1 — Reviewer`) all land in
    that round's section list, preserving first-seen order.
    """
    matches = list(_SECTION_HEADER_RE.finditer(text))
    if not matches:
        return text, []
    preamble = text[: matches[0].start()]
    # Boundaries: each section spans [match.start(), next_match.start() | EOF).
    bounds = [m.start() for m in matches] + [len(text)]
    rounds: list[tuple[int, list[tuple[str, str]]]] = []
    index: dict[int, int] = {}
    for i, m in enumerate(matches):
        rn_match = _ROUND_NUM_RE.search(m.group(0))
        if rn_match is None:
            continue  # header matched the section regex but has no round number — skip
        round_num = int(rn_match.group(1))
        critic = m.group(1)
        body = text[bounds[i] : bounds[i + 1]]
        if round_num in index:
            rounds[index[round_num]][1].append((critic, body))
        else:
            index[round_num] = len(rounds)
            rounds.append((round_num, [(critic, body)]))
    return preamble, rounds


def _collapse_round(round_num: int, sections: list[tuple[str, str]]) -> str:
    """Render one old round as one marker line per critic section.

    Keeps only the **last** section per critic name when a round has duplicate
    headers for the same critic (matches `_latest_section`'s "last wins"
    philosophy in nodes/debate.py). Resolution is inferred from any
    Reply/Proposer section in this round containing "RESOLVED" (case-insensitive).

    If the round has no Reviewer/UX section at all, emits a single deterministic
    `[Round N — (no critic section) — condensed]` line so the condensed file
    never has an empty round. Returns the marker block joined by `\n` plus a
    trailing blank line, so concatenation with the next round's block separates
    them by a blank line.
    """
    # Last section per critic name (last wins on duplicates).
    last_per_critic: dict[str, str] = {}
    for critic, body in sections:
        if critic in _CRITICS:
            last_per_critic[critic] = body

    # Resolution signal: any Reply/Proposer section body containing RESOLVED.
    resolved = any(
        "RESOLVED" in (body or "").upper()
        for critic, body in sections
        if critic in _RESOLUTION_SECTIONS
    )
    res = "all RESOLVED" if resolved else "unresolved"

    if not last_per_critic:
        return f"[Round {round_num} — (no critic section) — condensed]\n\n"

    lines: list[str] = []
    # Stable order: critic first-seen order in this round, restricted to _CRITICS.
    seen: list[str] = []
    for critic, _ in sections:
        if critic in _CRITICS and critic not in seen:
            seen.append(critic)
    for critic in seen:
        body = last_per_critic[critic]
        verdict = parse_verdict(body)
        blockers = count_blockers(body)
        lines.append(
            f"[Round {round_num} — {critic}: {verdict}, {blockers} blockers, {res} — condensed]"
        )
    return "\n".join(lines) + "\n\n"


def condense(text: str, keep_recent: int) -> str:
    """Collapse older debate rounds to markers; keep the last `keep_recent` verbatim.

    No-op (returns `text` unchanged) when the round count is `<= keep_recent`.
    `keep_recent == 0` collapses every round; `keep_recent < 0` is clamped to 0
    here as defense-in-depth (the config-level clamp already prevents negatives
    in normal operation). The last `keep_recent` rounds are re-emitted
    byte-identically: each section body is the exact input slice from its header
    to the next header, joined back without modification.
    """
    if keep_recent < 0:
        keep_recent = 0
    preamble, rounds = _parse_rounds(text)
    if len(rounds) <= keep_recent:
        return text
    # Explicit branch for keep_recent == 0: rounds[:-0] is [] in Python, not
    # "all but zero", so we must not use the negative-slice form here.
    if keep_recent == 0:
        old = rounds
        recent: list[tuple[int, list[tuple[str, str]]]] = []
    else:
        old = rounds[:-keep_recent]
        recent = rounds[-keep_recent:]
    collapsed = "".join(_collapse_round(n, secs) for n, secs in old)
    verbatim = "".join(body for _, secs in recent for _, body in secs)
    return preamble + collapsed + verbatim
