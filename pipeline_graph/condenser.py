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
from .nodes.debate import TECH_LIMIT_RE

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

# --- debate_ledger helpers (TASK-014) ---------------------------------------

# Matches BOTH TECH-LIMIT VERIFIED and TECH-LIMIT REJECTED lines, used to
# exclude those line spans from the [BLOCKER]/[SUGGESTION] scan so a
# `[BLOCKER]` tag embedded in a certification/rejection line does not produce
# a phantom BLOCKER entry. (TECH_LIMIT_RE — VERIFIED only — is reused from
# nodes.debate for ledger item extraction.)
TECH_LIMIT_LINE_RE = re.compile(
    r"^\s*TECH-LIMIT\s+(?:VERIFIED|REJECTED)\s*:", re.MULTILINE | re.IGNORECASE
)

# Raise lines in critic sections: `[BLOCKER] <claim>` or `[SUGGESTION] <claim>`,
# optionally wrapped in markdown bold (`**[BLOCKER] foo**`).
_RAISE_LINE_RE = re.compile(
    r"^\s*\*{0,2}\[(BLOCKER|SUGGESTION)\]\s*(.+?)\s*\*{0,2}\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# `### [SEVERITY] <claim>` header in Reply/Proposer sections (explicit preferred form).
_HEADER_CLAIM_RE = re.compile(
    r"^##+\s*\[(BLOCKER|SUGGESTION)\]\s*(.+?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# `[SEVERITY] <claim>` inline tag line in Reply/Proposer sections (primary matcher).
_TAG_CLAIM_RE = re.compile(
    r"^\s*\*{0,2}\[(BLOCKER|SUGGESTION)\]\s*(.+?)\s*\*{0,2}\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# ACCEPTED / REJECTED / PARTIAL markers that delimit reply item blocks.
_REPLY_MARKER_RE = re.compile(
    r"^\s*(ACCEPTED|REJECTED|PARTIAL)\b", re.MULTILINE | re.IGNORECASE
)

# Standalone RESOLVED marker (word-boundary, case-insensitive); rejects
# substrings such as UNRESOLVED.
_RESOLVED_STANDALONE_RE = re.compile(r"\bRESOLVED\b", re.IGNORECASE)

# UX re-review indexed resolution/open lines.
_UX_RESOLVED_RE = re.compile(
    r"^\s*RESOLVED\s+(\d+)\s*:", re.MULTILINE | re.IGNORECASE
)
_UX_STILL_OPEN_RE = re.compile(
    r"^\s*STILL\s+OPEN\s+(\d+)\s*:", re.MULTILINE | re.IGNORECASE
)

_LEDGER_HEADER = "## Debate ledger (prior rounds, deduplicated)\n"


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


# --- debate_ledger (TASK-014) ----------------------------------------------
#
# A deterministic, deduplicated summary of every item raised in the debate,
# one line per unique (normalized_claim, severity_kind, critic), with round +
# status. Pure text-in/string-out — no LLM, no IO, no events (C1).


def _normalize_claim(claim: str) -> str:
    r"""Strip surrounding ``*``/``\```` and collapse internal whitespace."""
    norm = claim.strip().strip("*`").strip()
    return re.sub(r"\s+", " ", norm).strip()


def _clean_claim(raw: str) -> str:
    """Clean a raw claim extracted from a raise line or reply tag.

    Strips surrounding markdown, collapses whitespace, and strips a trailing
    ``— RESOLVED`` suffix (which is not a resolution signal on a raise line).
    """
    claim = raw.strip().strip("*`").strip()
    # Strip trailing `— RESOLVED…` (em-dash or double-hyphen); the raise-line
    # suffix is NOT a resolution signal in a critic section.
    claim = re.sub(r"\s*(?:—|--)\s*RESOLVED\b.*$", "", claim, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", claim).strip()


def _match_claim_in_block(preceding: str, block: str) -> tuple[str | None, str | None]:
    """Identify the claim in a Reply/Proposer resolution block.

    Two match levels, no substring fallback (§6 signal (b)):
    (i)  ``### [SEVERITY] <claim>`` header in the text preceding the
         ACCEPTED/REJECTED/PARTIAL marker (explicit, preferred);
    (ii) ``[SEVERITY] <claim>`` inline tag in the preceding text (primary
         matcher — the producer contract guarantees this is present).

    If neither is present the block is skipped → item stays OPEN (conservative,
    no false resolution). Returns (claim, severity) or (None, None).
    """
    # (i) header
    headers = _HEADER_CLAIM_RE.findall(preceding)
    if headers:
        sev, claim = headers[-1]
        return _clean_claim(claim), sev.upper()
    # (ii) inline tag in preceding text
    tags = _TAG_CLAIM_RE.findall(preceding)
    if tags:
        sev, claim = tags[-1]
        return _clean_claim(claim), sev.upper()
    # Also check the block itself (tag at/preceding block start).
    tags_in_block = _TAG_CLAIM_RE.findall(block)
    if tags_in_block:
        sev, claim = tags_in_block[0]
        return _clean_claim(claim), sev.upper()
    return None, None


def _process_reply_section(body: str, items: dict) -> None:
    """Scan a Reply/Proposer section for tag-based resolution blocks.

    Splits at ACCEPTED/REJECTED/PARTIAL markers; a block is a RESOLVED signal
    iff it contains the standalone ``RESOLVED`` marker. The claim is identified
    via :func:`_match_claim_in_block`. A resolution resolves ALL critics'
    entries for that ``(normalized_claim, severity)`` (deterministic target for
    critic-less replies). TECH-LIMIT items are exempt (severity ``TECH-LIMIT``
    is never matched by ``[BLOCKER]``/``[SUGGESTION]`` tags).
    """
    markers = list(_REPLY_MARKER_RE.finditer(body))
    for i, m in enumerate(markers):
        block_start = m.start()
        block_end = markers[i + 1].start() if i + 1 < len(markers) else len(body)
        block = body[block_start:block_end]
        if not _RESOLVED_STANDALONE_RE.search(block):
            continue
        search_start = markers[i - 1].start() if i > 0 else 0
        preceding = body[search_start:block_start]
        claim, sev = _match_claim_in_block(preceding, block)
        if claim is None:
            continue
        norm = _normalize_claim(claim)
        for key in list(items.keys()):
            if key[0] == norm and key[1] == sev:
                items[key]["status"] = "RESOLVED"


def debate_ledger(debate_text: str) -> str:
    """Build a deterministic, deduplicated debate ledger from raw debate text.

    Each unique ``(normalized_claim, severity_kind, critic)`` item appears at
    most once, with its first-raise round and final status (OPEN/RESOLVED).
    Lines are sorted oldest-raise → newest. Pure text transform — no LLM, no
    IO, no events (C1). Empty/None input → ``""`` (C4).

    See FINAL-014 §3/D5 and §6 for the status resolution model.
    """
    text = debate_text or ""
    if not text.strip():
        return ""
    _preamble, rounds = _parse_rounds(text)
    if not rounds:
        return ""

    # key = (normalized_claim, severity_kind, critic) → item dict
    items: dict[tuple[str, str, str], dict] = {}
    items_order: list[tuple[str, str, str]] = []

    for round_num, sections in rounds:
        # UX-sourced [BLOCKER] items from rounds < round_num, in ledger order.
        # <n> in RESOLVED <n>:/STILL OPEN <n>: indexes this set (1-based).
        ux_blocker_keys = [
            k for k in items_order if k[1] == "BLOCKER" and k[2] == "UX"
        ]

        for critic, body in sections:
            critic_upper = critic.upper()
            if critic in _CRITICS:
                _process_critic_section(
                    body, round_num, critic_upper,
                    ux_blocker_keys, items, items_order,
                )
            elif critic in _REPLY_LIKE:
                _process_reply_section(body, items)

    if not items_order:
        return ""

    # Sort by first-raise round (oldest → newest); stable sort preserves
    # encounter order for items raised in the same round.
    sorted_keys = sorted(items_order, key=lambda k: items[k]["round"])
    lines = [_LEDGER_HEADER]
    for key in sorted_keys:
        item = items[key]
        _norm, sev, critic = key
        lines.append(
            f"[R{item['round']} · {critic} · {sev} · {item['status']}] {item['claim']}"
        )
    return "\n".join(lines) + "\n"


def _process_critic_section(
    body: str,
    round_num: int,
    critic_upper: str,
    ux_blocker_keys: list[tuple[str, str, str]],
    items: dict,
    items_order: list,
) -> None:
    """Extract raises + UX indexed signals from one critic section body.

    Signals are processed in file order (last signal wins). TECH-LIMIT lines
    (VERIFIED|REJECTED) are excluded from the [BLOCKER]/[SUGGESTION] scan via
    :data:`TECH_LIMIT_LINE_RE`. TECH-LIMIT VERIFIED items are extracted as
    RESOLVED at extraction time (REVIEWER only); REJECTED lines produce no
    ledger item.
    """
    lines = body.split("\n")
    # Identify TECH-LIMIT line indices (VERIFIED|REJECTED) for exclusion.
    tech_limit_lines: set[int] = set()
    for i, line in enumerate(lines):
        if TECH_LIMIT_LINE_RE.match(line):
            tech_limit_lines.add(i)

    # Extract TECH-LIMIT VERIFIED items (REVIEWER only) — RESOLVED at extraction.
    if critic_upper == "REVIEWER":
        for i, line in enumerate(lines):
            if i not in tech_limit_lines:
                continue
            m = TECH_LIMIT_RE.search(line)  # VERIFIED only
            if m:
                claim = _clean_claim(m.group(1))
                norm = _normalize_claim(claim)
                key = (norm, "TECH-LIMIT", "REVIEWER")
                if key not in items:
                    items[key] = {
                        "round": round_num,
                        "claim": claim,
                        "status": "RESOLVED",
                    }
                    items_order.append(key)

    # Process signals in file order.
    for i, line in enumerate(lines):
        if i in tech_limit_lines:
            continue

        # UX indexed signals (RESOLVED <n>: / STILL OPEN <n>:).
        if critic_upper == "UX":
            m_res = _UX_RESOLVED_RE.match(line)
            if m_res:
                n = int(m_res.group(1))
                if 1 <= n <= len(ux_blocker_keys):
                    items[ux_blocker_keys[n - 1]]["status"] = "RESOLVED"
                continue
            m_open = _UX_STILL_OPEN_RE.match(line)
            if m_open:
                n = int(m_open.group(1))
                if 1 <= n <= len(ux_blocker_keys):
                    items[ux_blocker_keys[n - 1]]["status"] = "OPEN"
                continue

        # Raise lines [BLOCKER]/[SUGGESTION] — OPEN signal (re-raise reopens).
        m_raise = _RAISE_LINE_RE.match(line)
        if m_raise:
            sev = m_raise.group(1).upper()
            claim = _clean_claim(m_raise.group(2))
            norm = _normalize_claim(claim)
            key = (norm, sev, critic_upper)
            if key not in items:
                items[key] = {
                    "round": round_num,
                    "claim": claim,
                    "status": "OPEN",
                }
                items_order.append(key)
            else:
                items[key]["status"] = "OPEN"
