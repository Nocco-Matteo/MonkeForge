"""Deterministic debate-file condenser (pure string-in/string-out).

Collapses older ``## Round N — <Critic>`` sections of a ``DEBATE-{task_id}.md``
file to compact one-line markers, keeping the last ``keep_recent`` rounds
verbatim. No LLM calls, no third-party token libs, no file IO, no event
emission — those live in ``agents.py::run_agent()`` (see D1 in PLAN-003).

The only module-level imports are ``re`` and ``parse_verdict`` from
``agents``; ``count_blockers`` is imported function-locally (item 2).
``agents.py`` imports this module function-locally inside ``run_agent()`` to
avoid an import-time cycle (D2).
"""
from __future__ import annotations

import difflib
import re

from .agents import parse_verdict
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
# optionally carrying a provenance suffix (`[BLOCKER:PLAN]` / `[BLOCKER:REQUIREMENTS]`)
# between the severity tag and the claim, optionally carrying a blocker/suggestion
# id (`B1:` / `S1:`) between the tag and the claim, and optionally wrapped in
# markdown bold (`**[BLOCKER] foo**`). Group 1 = severity, group 2 = provenance
# (optional, None when absent — callers default to "PLAN"), group 3 = id
# (optional `[BS]\d+`, None when absent), group 4 = claim.
_RAISE_LINE_RE = re.compile(
    r"^\s*\*{0,2}\[(BLOCKER|SUGGESTION)(?::(PLAN|REQUIREMENTS))?\]\s*"
    r"(?:([BS]\d+):\s*)?(.+?)\s*\*{0,2}\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# `### [SEVERITY] <claim>` header in Reply/Proposer sections (explicit preferred
# form). Same 4-group structure as _RAISE_LINE_RE (group 2 = optional provenance,
# group 3 = optional id, group 4 = claim).
_HEADER_CLAIM_RE = re.compile(
    r"^##+\s*\[(BLOCKER|SUGGESTION)(?::(PLAN|REQUIREMENTS))?\]\s*"
    r"(?:([BS]\d+):\s*)?(.+?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# `[SEVERITY] <claim>` inline tag line in Reply/Proposer sections (primary
# matcher). Same 4-group structure as _RAISE_LINE_RE (group 2 = optional
# provenance, group 3 = optional id, group 4 = claim).
_TAG_CLAIM_RE = re.compile(
    r"^\s*\*{0,2}\[(BLOCKER|SUGGESTION)(?::(PLAN|REQUIREMENTS))?\]\s*"
    r"(?:([BS]\d+):\s*)?(.+?)\s*\*{0,2}\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# ACCEPTED / REJECTED / PARTIAL markers that delimit reply item blocks.
# Matches both the bare form (`ACCEPTED — …`) and the id-qualified form
# (`B1 (Reviewer): ACCEPTED — …`).
_REPLY_MARKER_RE = re.compile(
    r"^\s*(?:[BS]\d+\s+\(\w+\)\s*:\s*)?(ACCEPTED|REJECTED|PARTIAL)\b",
    re.MULTILINE | re.IGNORECASE,
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

# Id-qualified reply resolution: `B1 (Reviewer): ACCEPTED — ...` or
# `S1 (UX): REJECTED — ...`. Group 1 = id (`[BS]\d+`), group 2 = critic
# (`Reviewer` or `UX`). A reply block carrying an id WITHOUT a critic qualifier
# (bare `B1:`) resolves nothing — the critic must be explicit to avoid
# false cross-critic resolution (D6).
_REPLY_ID_RE = re.compile(
    r"^\s*([BS]\d+)\s+\((Reviewer|UX)\)\s*:",
    re.MULTILINE | re.IGNORECASE,
)

# Id-based RESOLVED marker for UX re-review rubber-stamp guard: a line like
# `B1: RESOLVED — ...` or `S1: STILL OPEN — ...` proves the designer walked the
# prior blockers by id, even when the bare word "RESOLVED" is absent. A bare
# `B1: <claim>` without a RESOLVED/STILL OPEN keyword does NOT count — the
# designer must explicitly rule on each prior blocker by id.
_UX_ID_RESOLVED_RE = re.compile(
    r"^\s*[BS]\d+\s*:\s*(?:RESOLVED|STILL\s+OPEN)\b",
    re.MULTILINE | re.IGNORECASE,
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

    lines = []
    for critic in order:
        verdict = parse_verdict(last_body[critic])
        # Item 28: verdict-gate the blocker count. Under APPROVE /
        # APPROVE_WITH_CHANGES the critic is shipping, so any [BLOCKER] token
        # in that section references a resolved or prior item — counting it
        # would inflate the condensed marker with now-resolved blockers
        # (mirrors nodes.debate._open_blocker_count and the stuck_claims
        # verdict filter). This was a pre-existing display bug that Batch 3's
        # extension of count_blockers to match [BLOCKER:PLAN]/[BLOCKER:REQUIREMENTS]
        # would otherwise make worse (counting provenance-tagged, resolved
        # blockers as open).
        from .agents import count_blockers  # function-local (item 2)
        n_blockers = (
            0 if verdict in ("APPROVE", "APPROVE_WITH_CHANGES")
            else count_blockers(last_body[critic])
        )
        lines.append(
            f"[Round {round_num} — {critic}: {verdict}, "
            f"{n_blockers} blockers, {res} — condensed]"
        )
    return "\n".join(lines) + "\n\n"


def stuck_claims(debate_text: str, k: int) -> list[str]:
    """Detect BLOCKER claims repeated across the last ``k`` consecutive rounds.

    A claim is "stuck" when it is raised as a ``[BLOCKER]`` in each of the last
    ``k`` rounds (after the verdict filter — a critic who APPROVE/APPROVE_WITH_CHANGES
    is shipping, so any ``[BLOCKER]`` token in that section references a resolved
    or prior item and is excluded from the stuck scan, mirroring
    ``nodes.debate._open_blocker_count``).

    Within each round, only the LAST section per critic name is scanned (matches
    ``_collapse_round``'s "last wins" philosophy — a reviewer re-critiquing in
    the same round supersedes the earlier pass). ``[SUGGESTION]`` raises are
    never considered (item 2).

    Returns ``[]`` when ``k < 1`` or when fewer than ``k`` rounds exist.
    """
    if k < 1:
        return []
    text = debate_text or ""
    if not text.strip():
        return []
    _preamble, rounds = _parse_rounds(text)
    if len(rounds) < k:
        return []

    recent = rounds[-k:]
    per_round_claims: list[set[str]] = []
    for _rn, sections in recent:
        # Last section per critic name within this round (item 3).
        last_body: dict[str, str] = {}
        for critic, body in sections:
            if critic in _CRITICS:
                last_body[critic] = body
        round_claims: set[str] = set()
        for critic, body in last_body.items():
            # Item 4: exclude APPROVE / APPROVE_WITH_CHANGES sections.
            if parse_verdict(body) in ("APPROVE", "APPROVE_WITH_CHANGES"):
                continue
            # Item 2: only BLOCKER raises (skip SUGGESTION).
            for m in _RAISE_LINE_RE.finditer(body):
                if m.group(1).upper() == "BLOCKER":
                    # D2: per-round ID identity — when an id is present, use the
                    # critic-qualified single-token form `f"{critic}{id}"` (e.g.
                    # "ReviewerB1") so a Reviewer B1 and a UX B1 are distinct.
                    # When no id, fall back to the normalized claim text.
                    rid = m.group(3)
                    if rid:
                        token = f"{critic}{rid.upper()}"
                    else:
                        token = _normalize_claim(_clean_claim(m.group(4)))
                    if token:
                        round_claims.add(token)
        per_round_claims.append(round_claims)

    if not per_round_claims:
        return []
    stuck = per_round_claims[0]
    for claims in per_round_claims[1:]:
        stuck = stuck & claims
    return sorted(stuck)


def thrashing_report(debate_text: str, k: int) -> dict:
    """Deterministic thrashing triage over the last ``k`` ACTIVE debate rounds.

    A round is "active" when at least one of its critic sections (last section
    per critic name, matching ``_collapse_round`` / ``stuck_claims``'s "last
    wins" philosophy) has a verdict of ``REJECT`` or ``UNKNOWN`` — a round where
    every critic ``APPROVE``/``APPROVE_WITH_CHANGES`` is shipping, so any
    ``[BLOCKER]`` token in it references a resolved or prior item and the round
    is excluded from the trend (mirrors ``stuck_claims``'s verdict filter and
    ``_open_blocker_count``).

    Returns a dict with:

    - ``mode``: ``"thrashing"`` | ``"stuck"`` | ``"converging"`` | ``"unknown"``.
    - ``blocker_counts``: one int per active round in the window — the sum of
      ``count_blockers`` over each critic's last section INDEPENDENTLY (a claim
      raised by both Reviewer and UX counts twice, by design — the trend tracks
      total raised work, not deduplicated themes).
    - ``repeated``: BLOCKER claims present in EVERY active round of the window
      (intersection of per-round claim sets, each set deduped ACROSS critics so
      a claim raised by both Reviewer and UX in the same round counts once).
    - ``new``: BLOCKER claims in the latest active round that were NOT in the
      previous active round (empty when the window has fewer than 2 rounds).

    C15 override — ``len(active_window) < 2`` ⇒ ``mode="unknown"`` regardless of
    ``k``. This intentionally diverges from ``stuck_claims``' own behaviour at
    ``k=1`` (where a single round can still produce a non-empty intersection):
    a single active round carries no trend information, so classifying it as
    ``stuck``/``converging``/``thrashing`` would be a guess. ``blocker_counts``,
    ``repeated`` and ``new`` are still populated from the available active
    rounds so the caller can render them, but the mode stays ``"unknown"``. Do
    NOT "fix" this as an inconsistency with ``stuck_claims`` — the divergence is
    load-bearing (it is what resolves the contradiction the debate just
    settled).

    Classification order (when ``len(active_window) >= 2``):

    1. ``stuck_claims(debate_text, k)`` non-empty → ``"stuck"`` (a claim repeated
       across every one of the last ``k`` rounds — the debate is not moving).
    2. latest ``blocker_counts`` entry ``== 0`` → ``"converging"`` (the last
       active round raised no blockers — the debate is closing).
    3. NOT strictly decreasing at every step AND ``len(new) >= 1`` →
       ``"thrashing"`` (blockers are not going down AND new ones appear — the
       debate is churning).
    4. strictly decreasing at every step → ``"converging"`` (blockers strictly
       drop each active round — the debate is closing).
    5. else → ``"unknown"`` (e.g. not strictly decreasing but no new claims —
       the debate is stuck on the same set without growing, which is neither
       converging nor thrashing).

    Empty/None input, ``k < 1``, or no rounds → ``mode="unknown"`` with empty
    lists. ``count_blockers`` is imported function-locally (item 2) to keep
    the module-level surface to ``parse_verdict`` only.
    """
    text = debate_text or ""
    empty = {"mode": "unknown", "blocker_counts": [], "repeated": [], "new": [],
             "prior": []}
    if not text.strip() or k < 1:
        return empty
    from .agents import count_blockers  # function-local (item 2)
    _preamble, rounds = _parse_rounds(text)
    if not rounds:
        return empty

    # Build per-active-round (blocker_count, claim_set). A round is active iff
    # at least one critic's last section verdict is REJECT or UNKNOWN.
    active: list[tuple[int, set[str]]] = []
    for _rn, sections in rounds:
        last_body: dict[str, str] = {}
        order: list[str] = []
        for critic, body in sections:
            if critic in _CRITICS:
                if critic not in last_body:
                    order.append(critic)
                last_body[critic] = body
        if not order:
            continue
        any_active = any(
            parse_verdict(last_body[critic]) not in ("APPROVE", "APPROVE_WITH_CHANGES")
            for critic in order
        )
        if not any_active:
            continue
        # blocker_counts sums count_blockers per critic INDEPENDENTLY (a claim
        # raised by both Reviewer and UX counts twice — by design).
        bc = sum(count_blockers(last_body[critic]) for critic in order)
        # claim set deduped ACROSS critics for repeated/new.
        claims: set[str] = set()
        for critic in order:
            if parse_verdict(last_body[critic]) in ("APPROVE", "APPROVE_WITH_CHANGES"):
                continue
            for m in _RAISE_LINE_RE.finditer(last_body[critic]):
                if m.group(1).upper() == "BLOCKER":
                    # D2: critic-qualified single-token when id present, else
                    # normalized claim text (same form as stuck_claims).
                    rid = m.group(3)
                    if rid:
                        token = f"{critic}{rid.upper()}"
                    else:
                        token = _normalize_claim(_clean_claim(m.group(4)))
                    if token:
                        claims.add(token)
        active.append((bc, claims))

    window = active[-k:] if k else active
    blocker_counts = [bc for bc, _ in window]

    if window:
        repeated = set(window[0][1])
        for _, claims in window[1:]:
            repeated &= claims
        repeated = sorted(repeated)
    else:
        repeated = []
    if len(window) >= 2:
        new = sorted(window[-1][1] - window[-2][1])
    else:
        new = []

    # C15: a single active round carries no trend → unknown, regardless of k.
    if len(window) < 2:
        return {"mode": "unknown", "blocker_counts": blocker_counts,
                "repeated": repeated, "new": new, "prior": []}

    def _strictly_decreasing(seq: list[int]) -> bool:
        return all(seq[i] < seq[i - 1] for i in range(1, len(seq)))

    stuck = stuck_claims(text, k)
    if stuck:
        mode = "stuck"
    elif blocker_counts[-1] == 0:
        mode = "converging"
    elif len(new) >= 1 and not _strictly_decreasing(blocker_counts):
        mode = "thrashing"
    elif _strictly_decreasing(blocker_counts):
        mode = "converging"
    else:
        mode = "unknown"
    return {"mode": mode, "blocker_counts": blocker_counts,
            "repeated": repeated, "new": new,
            "prior": sorted(window[-2][1]) if len(window) >= 2 else []}


# --- thrashing-refinement policy (TASK-024) ---------------------------------
#
# Theme-overlap helpers that classify whether the "new" BLOCKER claims in a
# thrashing window are refinements of the prior round's themes (the proposer is
# iterating on the same surface) or genuinely fresh surface area (more rounds
# will not help). Pure string-in/string-out — no LLM, no IO, no events (C1).


# Splits a claim into alphanumeric tokens. Length-1 tokens are dropped (a bare
# "a" or "1" carries no theme information and would inflate the token set with
# noise). Reused by claim_theme_overlap.
_TOKEN_SPLIT_RE = re.compile(r"[^a-zA-Z0-9]+")


def claim_theme_overlap(a: str, b: str) -> float:
    """Jaccard overlap between the theme-token sets of two claims.

    Both inputs are normalized via ``_normalize_claim(_clean_claim(x))`` before
    tokenizing, so smart-quote/whitespace differences do not deflate the
    overlap. Tokenization splits on non-alphanumeric runs and drops empty and
    length-1 tokens. Returns ``0.0`` when either resulting token set is empty
    (no division by zero), ``1.0`` for identical token sets, and a value
    strictly in ``(0, 1)`` for partial overlap.
    """
    ta = {t for t in _TOKEN_SPLIT_RE.split(_normalize_claim(_clean_claim(a or ""))) if len(t) > 1}
    tb = {t for t in _TOKEN_SPLIT_RE.split(_normalize_claim(_clean_claim(b or ""))) if len(t) > 1}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def majority_new_refine_prior(new, prior, threshold: float) -> bool:
    """True iff a majority of ``new`` claims refine a ``prior`` claim's theme.

    A claim ``n`` in ``new`` "refines" the prior when its max theme overlap
    with any claim in ``prior`` is ``>= threshold`` (``0.0`` when ``prior`` is
    empty — nothing to refine against). Returns ``True`` iff the count of
    refining claims is ``>= ceil(len(new)/2)``. Returns ``False`` when ``new``
    is empty (no claims to classify) or when ``prior`` is empty (every claim is
    fresh surface area by definition, so the majority cannot be refining).

    ``prior`` may be a ``list[str]`` or ``set[str]``; it is iterated, not
    indexed.
    """
    new_list = list(new or [])
    if not new_list:
        return False
    prior_list = list(prior or [])
    if not prior_list:
        return False
    import math as _math
    refining = 0
    for n in new_list:
        best = max((claim_theme_overlap(n, p) for p in prior_list), default=0.0)
        if best >= threshold:
            refining += 1
    return refining >= _math.ceil(len(new_list) / 2)


def latest_requirements_blockers(debate_text: str) -> list[str]:
    """Return the claims from ``[BLOCKER:REQUIREMENTS]`` lines in the last round.

    Item 27: a REQUIREMENTS-provenanced blocker is one the critic believes
    belongs to the brief, not the plan — the debate cannot fix it by iterating
    on the plan, so the run should escalate to a human (amend the brief, then
    redo from plan) instead of burning more rounds.

    Within the last round, only the LAST section per critic name is scanned
    (matches ``_collapse_round``'s / ``stuck_claims``'s "last wins" philosophy).
    Sections whose verdict is ``APPROVE`` or ``APPROVE_WITH_CHANGES`` are
    skipped: a shipping critic's ``[BLOCKER:REQUIREMENTS]`` token references a
    resolved or prior item, not a live requirements blocker (mirrors
    ``stuck_claims``'s verdict filter and ``_open_blocker_count``).

    Returns the cleaned claims in first-seen order. Empty/None input or no
    rounds → ``[]``.
    """
    text = debate_text or ""
    if not text.strip():
        return []
    _preamble, rounds = _parse_rounds(text)
    if not rounds:
        return []

    _rn, sections = rounds[-1]
    last_body: dict[str, str] = {}
    order: list[str] = []
    for critic, body in sections:
        if critic in _CRITICS:
            if critic not in last_body:
                order.append(critic)
            last_body[critic] = body

    claims: list[str] = []
    seen: set[str] = set()
    for critic in order:
        body = last_body[critic]
        if parse_verdict(body) in ("APPROVE", "APPROVE_WITH_CHANGES"):
            continue
        for m in _RAISE_LINE_RE.finditer(body):
            if m.group(1).upper() != "BLOCKER":
                continue
            provenance = (m.group(2) or "PLAN").upper()
            if provenance != "REQUIREMENTS":
                continue
            claim = _clean_claim(m.group(4))
            norm = _normalize_claim(claim)
            if norm and norm not in seen:
                seen.add(norm)
                claims.append(claim)
    return claims


def _section_has_blockers_without_ids(body: str) -> bool:
    """Item 12: return True iff the section body contains at least one
    ``[BLOCKER]`` raise line WITHOUT an id (``B1:``/``S1:``).

    Used by the UX rubber-stamp guard (D5): when the latest UX section's
    blockers all carry ids, the designer walked the prior ledger and a bare
    APPROVE is a real sign-off; when any blocker lacks an id, the bare
    APPROVE is treated as a rubber stamp and the round is forced to reply.
    """
    for m in _RAISE_LINE_RE.finditer(body or ""):
        if m.group(1).upper() == "BLOCKER" and not m.group(3):
            return True
    return False


def _raises_missing_ids(body: str) -> list[str]:
    """Item 13: return the list of claim texts for raise lines in the section
    body that lack an id (``B1:``/``S1:``). Used by the F1d escalation to
    surface which claims the critic failed to id.

    Only ``[BLOCKER]`` and ``[SUGGESTION]`` raise lines are considered
    (TECH-LIMIT lines are not raise lines).
    """
    missing: list[str] = []
    for m in _RAISE_LINE_RE.finditer(body or ""):
        if m.group(3):
            continue
        claim = _clean_claim(m.group(4))
        if claim:
            missing.append(claim)
    return missing


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


# Fold typographic quotes into ASCII so raise/reply claim keys match when an
# LLM (or paste) uses curly apostrophes/quotes in one place and straight in
# another. Seen on TASK-022: critic raised `C8's` (U+2019) and proposer
# resolved `C8's` (ASCII) → ledger left the blocker OPEN through verification.
_CLAIM_QUOTE_FOLD = str.maketrans({
    "\u2018": "'",  # ‘ left single quotation mark
    "\u2019": "'",  # ’ right single quotation mark
    "\u201a": "'",  # ‚ single low-9 quotation mark
    "\u2032": "'",  # ′ prime
    "\u00b4": "'",  # ´ acute accent
    "\u201c": '"',  # “ left double quotation mark
    "\u201d": '"',  # ” right double quotation mark
    "\u201e": '"',  # „ double low-9 quotation mark
    "\u2033": '"',  # ″ double prime
})


def _normalize_claim(claim: str) -> str:
    r"""Strip surrounding ``*``/``\````, fold smart quotes, collapse whitespace.

    Matching key only — display text keeps the first-raise wording. Quote
    folding is load-bearing for ledger RESOLVED status when critics and
    proposers disagree on apostrophe glyphs.
    """
    norm = claim.strip().strip("*`").strip().translate(_CLAIM_QUOTE_FOLD)
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
        sev, _prov, _id, claim = headers[-1]
        return _clean_claim(claim), sev.upper()
    # (ii) inline tag in preceding text
    tags = _TAG_CLAIM_RE.findall(preceding)
    if tags:
        sev, _prov, _id, claim = tags[-1]
        return _clean_claim(claim), sev.upper()
    # Also check the block itself (tag at/preceding block start).
    tags_in_block = _TAG_CLAIM_RE.findall(block)
    if tags_in_block:
        sev, _prov, _id, claim = tags_in_block[0]
        return _clean_claim(claim), sev.upper()
    return None, None


def _process_reply_section(body: str, items: dict) -> None:
    """Scan a Reply/Proposer section for tag-based or id-based resolution blocks.

    Splits at ACCEPTED/REJECTED/PARTIAL markers; a block is a RESOLVED signal
    iff it contains the standalone ``RESOLVED`` marker. Resolution is attempted
    in two modes (D6):

    (i) Id-qualified: when the block (or its preceding text) carries a
        ``B1 (Reviewer):`` / ``S1 (UX):`` id+critic prefix, only the matching
        ``(critic, id, sev)`` ledger entry is resolved. A bare ``B1:`` with no
        critic qualifier resolves NOTHING — the critic must be explicit to
        avoid false cross-critic resolution. When an id is present, there is
        NO fallback to the tag-based path for that block.

    (ii) Legacy tag-based: when no id is present at all, the claim is
        identified via :func:`_match_claim_in_block` and the resolution
        targets ALL critics' entries for that ``(normalized_claim, severity)``
        (the pre-id behaviour, preserved unchanged).

    TECH-LIMIT items are exempt (severity ``TECH-LIMIT`` is never matched by
    ``[BLOCKER]``/``[SUGGESTION]`` tags).
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

        # (i) Id-qualified resolution — check preceding text and block.
        id_match = _REPLY_ID_RE.search(preceding) or _REPLY_ID_RE.search(block)
        if id_match:
            rid = id_match.group(1).upper()
            critic = id_match.group(2).upper()
            # Resolve only the matching (critic, id, sev) entry.
            for key in list(items.keys()):
                if key[0] == critic and key[1] == rid:
                    items[key]["status"] = "RESOLVED"
            # An id was present (with critic) — no fallback to tag-based.
            continue

        # Check for a bare id (no critic qualifier) — resolves nothing.
        # _REPLY_ID_RE requires the (Critic) group, so a bare `B1:` would not
        # match it. We check for any [BS]\d+: line prefix to detect a bare id
        # and skip tag-based fallback for that block (D6).
        bare_id = re.match(r"^\s*[BS]\d+\s*:", block.strip(), re.IGNORECASE)
        if bare_id:
            continue

        # (ii) Legacy tag-based resolution — no id present.
        claim, sev = _match_claim_in_block(preceding, block)
        if claim is None:
            continue
        norm = _normalize_claim(claim)
        for key in list(items.keys()):
            # New key format: (critic, id_or_norm, sev). Legacy resolution
            # matches on id_or_norm == norm (items without ids) and sev.
            if key[1] == norm and key[2] == sev:
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

    # key = (critic, id_or_norm, severity_kind) → item dict. When an id is
    # present, id_or_norm is the id (e.g. "B1"); otherwise it is the normalized
    # claim text. This keeps id-keyed items distinct from text-keyed items and
    # from cross-critic same-id items (D2).
    items: dict[tuple[str, str, str], dict] = {}
    items_order: list[tuple[str, str, str]] = []

    for round_num, sections in rounds:
        # UX-sourced [BLOCKER] items from rounds < round_num, in ledger order.
        # <n> in RESOLVED <n>:/STILL OPEN <n>: indexes this set (1-based).
        ux_blocker_keys = [
            k for k in items_order if k[0] == "UX" and k[2] == "BLOCKER"
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
        critic, _id_or_norm, sev = key
        # Item 8: prefix the claim with `f"{id}: "` when the item's id is set.
        claim_display = (
            f"{item['id']}: {item['claim']}" if item.get("id") else item["claim"]
        )
        lines.append(
            f"[R{item['round']} · {critic} · {sev} · {item['status']}] {claim_display}"
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
                key = ("REVIEWER", norm, "TECH-LIMIT")
                if key not in items:
                    items[key] = {
                        "round": round_num,
                        "claim": claim,
                        "status": "RESOLVED",
                        "id": None,
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
            # Item 25: provenance from group(2), defaulting to "PLAN" when the
            # optional :PLAN/:REQUIREMENTS suffix is absent.
            provenance = (m_raise.group(2) or "PLAN").upper()
            # D2: id from group(3) (optional), claim from group(4).
            rid = m_raise.group(3)
            rid = rid.upper() if rid else None
            claim = _clean_claim(m_raise.group(4))
            norm = _normalize_claim(claim)
            # Item 7: key on (critic, id_or_norm, sev) — id takes precedence
            # over text.
            id_or_norm = rid if rid else norm
            key = (critic_upper, id_or_norm, sev)
            if key not in items:
                items[key] = {
                    "round": round_num,
                    "claim": claim,
                    "status": "OPEN",
                    "provenance": provenance,
                    "id": rid,
                }
                items_order.append(key)
            else:
                items[key]["status"] = "OPEN"


# --- TASK-023: plan diff / section titles -----------------------------------
#
# Lean plan-view helpers for the critic rounds. ``plan_diff`` produces a
# unified diff between the snapshot the proposer last replied to and the
# current plan, so a round-2+ critic sees only what changed instead of the
# whole (possibly large) plan. ``plan_section_titles`` lists the numbered
# section headers (``N. <title>``) the section-patch applier targets.


def plan_diff(old: str, new: str) -> str:
    """Unified diff between two plan snapshots.

    Returns ``""`` when ``old == new`` or either side is empty — the caller
    (``_build_plan_view``) treats an empty diff as "send the full plan
    instead" so a no-op reply does not starve the critic of context. Pure
    string-in/string-out, no IO.
    """
    old = old or ""
    new = new or ""
    if not old or not new or old == new:
        return ""
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile="plan-snapshot",
        tofile="plan",
        lineterm="",
    )
    return "".join(diff)


def plan_section_titles(plan: str) -> list[str]:
    """Return the numbered section header titles of a plan.

    Accepts ATX markdown (``## 1. Goal``, production PLAN-*.md) and bare
    ``1. Goal`` (fixture / plan.md style). Titles are the text after
    ``N. `` (e.g. ``Goal``), in first-seen order. Every matching header line
    is included — adjacent numbered sections are real anchors, not list items.
    """
    plan = plan or ""
    titles: list[str] = []
    for line in plan.split("\n"):
        m = re.match(r"^(#{1,6}\s+)?(\d+)\.\s+(.+)$", line)
        if not m:
            continue
        titles.append(m.group(3).strip())
    return titles

