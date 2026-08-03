"""Step 2: debate nodes (tech critic, UX critic, reply, decision)."""

from __future__ import annotations

import os
import re
import time

from .. import config as C
from .. import events as ev
from ..agents import classify_output, count_blockers, parse_verdict, read_if_exists, run_agent
from ..state import Conversation
from .common import _file_or_stdout, _recover_artifact, _save, _trust_output

TECH_LIMIT_RE = re.compile(
    r"^\s*TECH-LIMIT\s+VERIFIED\s*:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE
)

_SECTION_HEADER_RE = re.compile(r"^##\s+Round\s+\d+\s+—\s+(\w+)", re.MULTILINE)

PLAN_MARKER_RE = re.compile(
    r"===\s*PLAN\s*START\s*===\s*\n(.*?)\n===\s*PLAN\s*END\s*===",
    re.DOTALL | re.IGNORECASE,
)

# TASK-023: section-replace plan patches. The proposer prints one
# ``=== PLAN PATCH START ===`` … ``=== PLAN PATCH END ===`` envelope per reply,
# containing one or more ``@@@ REPLACE section: "<title>"`` … ``@@@ END``
# blocks. Each block's body replaces the named plan section's span (header
# line through the line before the next numbered header) verbatim. The body's
# first line MUST be the section header itself — the applier replaces the
# header-through-next-header span wholesale with what is printed here, so
# omitting the header deletes that section's anchor from the plan.
PLAN_PATCH_RE = re.compile(
    r"===\s*PLAN\s*PATCH\s*START\s*===\s*\n(.*?)\n===\s*PLAN\s*PATCH\s*END\s*===",
    re.DOTALL | re.IGNORECASE,
)

SECTION_REPLACE_RE = re.compile(
    r'@@@\s+REPLACE\s+section:\s*"([^"]+)"\s*\n(.*?)@@@\s+END',
    re.DOTALL | re.IGNORECASE,
)

# Numbered plan section header. Real PLAN-*.md files use ATX markdown
# (``## 1. Goal``); tests/fixtures may use the bare ``1. Goal`` form from
# ``plan.md``. Group 1 = optional ``## `` prefix, group 2 = number, group 3 =
# title text after ``N. ``.
_SECTION_HEADER_LINE_RE = re.compile(r"^(#{1,6}\s+)?(\d+)\.\s+(.+)$")
# Bare numbered line (no ``#``) — used only to reject list items under a bare
# ``N. Title`` header. Markdown ATX headers (``## N.``) are never rejected
# for a following bare ``1. item`` list — that was the TASK-023 ship-blocker
# against real plans (``## 5. File-by-file`` + ``1. do X`` looked like a list).
_BARE_NUMBERED_RE = re.compile(r"^\d+\.\s+")


def _plan_uses_atx_section_headers(lines: list[str]) -> bool:
    """True when any line looks like a production ``## N. Title`` anchor."""
    for ln in lines:
        m = _SECTION_HEADER_LINE_RE.match(ln)
        if m and m.group(1):
            return True
    return False


def _is_plan_section_header(lines: list[str], i: int) -> bool:
    """True when ``lines[i]`` is a plan section header.

    Accepts ``## N. Title`` (production PLAN-*.md) and bare ``N. Title``
    (fixture / plan.md style).

    Rules:
    - Markdown ATX ``## N.`` lines are always section anchors.
    - When the plan already uses ATX section headers, bare ``N.`` lines are
      never anchors (they are list items under ``## 5. File-by-file`` etc.).
    - Bare-only plans: a ``N.`` line is an anchor unless it sits in a
      contiguous bare-numbered run (next or previous line also bare-numbered).
    """
    if i < 0 or i >= len(lines):
        return False
    m = _SECTION_HEADER_LINE_RE.match(lines[i])
    if not m:
        return False
    if m.group(1):
        return True  # ## N. Title — always a section header
    if _plan_uses_atx_section_headers(lines):
        return False
    # Bare N. Title in a bare-only plan: reject list runs.
    if i + 1 < len(lines) and _BARE_NUMBERED_RE.match(lines[i + 1]):
        return False
    if i > 0 and _BARE_NUMBERED_RE.match(lines[i - 1]):
        return False
    return True


def _section_title_keys(line: str) -> set[str]:
    """Match keys for a header line against a ``@@@ REPLACE section: "..."`` title.

    A proposer may cite ``Goal``, ``1. Goal``, or ``## 1. Goal`` — all must
    resolve to the same ``## 1. Goal`` line in a real plan.
    """
    stripped = (line or "").strip()
    keys = {stripped}
    m = _SECTION_HEADER_LINE_RE.match(stripped)
    if not m:
        return keys
    num, rest = m.group(2), m.group(3).strip()
    keys.add(rest)
    keys.add(f"{num}. {rest}")
    keys.add(f"## {num}. {rest}")
    return keys


def _extract_plan_or_patch(text: str) -> tuple[str | None, str | None]:
    """Classify a proposer reply into one of four outcomes.

    Returns:
      - ``("patch", body)``: a well-formed ``=== PLAN PATCH START/END ===``
        envelope was found; ``body`` is the text between the markers (one or
        more ``@@@ REPLACE section: "..." … @@@ END`` blocks).
      - ``("legacy", full_plan)``: a legacy ``=== PLAN START/END ===`` envelope
        was found (the proposer printed the whole plan); ``full_plan`` is the
        plan text. Discouraged — the caller records a degradation.
      - ``("malformed", None)``: neither regex matched, but the raw text
        contains (case-insensitive) ``=== PLAN PATCH START ===``,
        ``=== PLAN PATCH END ===``, or any ``@@@`` token — a broken/partial
        envelope or a stray patch token outside a well-formed block. The
        caller escalates rather than silently no-op'ing.
      - ``(None, None)``: a genuine no-patch, per-item-only reply (or a
        direct-edit-only reply) — none of the patch/legacy/malformed markers
        are present.
    """
    text = text or ""
    m = PLAN_PATCH_RE.search(text)
    if m:
        # Reject stray patch markers / ``@@@`` tokens OUTSIDE the matched
        # envelope — a second envelope, a dangling START/END, or a bare
        # ``@@@`` block would otherwise be silently ignored while the
        # matched block mutates the plan (partial application / bypassed
        # escalation).
        outside = text[:m.start()] + text[m.end():]
        lower_out = outside.lower()
        if (
            "=== plan patch start ===" in lower_out
            or "=== plan patch end ===" in lower_out
            or "@@@" in lower_out
        ):
            return ("malformed", None)
        return ("patch", m.group(1))
    m = PLAN_MARKER_RE.search(text)
    if m:
        return ("legacy", m.group(1).strip())
    lower = text.lower()
    if (
        "=== plan patch start ===" in lower
        or "=== plan patch end ===" in lower
        or "@@@" in lower
    ):
        return ("malformed", None)
    return (None, None)


def _apply_section_patch(plan: str, patch_body: str) -> str | None:
    """Apply a section-replace patch body to ``plan``.

    The patch body contains one or more ``@@@ REPLACE section: "<title>"`` …
    ``@@@ END`` blocks. Each block's body replaces the named plan section's
    span (the header line through the line before the next numbered section
    header) verbatim — the body's first line MUST be the section header
    itself, because the applier deletes the original header line as part of
    the span.

    Returns:
      - the new plan string on success.
      - the plan unchanged if ``patch_body`` contains no ``@@@`` token (a
        no-op patch body — nothing to apply).
      - ``None`` if ``patch_body`` contains ``@@@`` tokens but zero valid
        ``@@@ REPLACE … @@@ END`` blocks, or if a cited section title is not
        found among the headers identified by ``_is_plan_section_header``.
    """
    if "@@@" not in patch_body:
        return plan
    blocks = list(SECTION_REPLACE_RE.finditer(patch_body))
    if not blocks:
        return None
    # Reject stray ``@@@`` tokens that are not part of a valid
    # ``@@@ REPLACE … @@@ END`` block — otherwise a malformed block would
    # be ignored while a valid one mutates the plan (partial application).
    for am in re.finditer(r"@@@", patch_body):
        if not any(b.start() <= am.start() < b.end() for b in blocks):
            return None
    result_lines = (plan or "").split("\n")
    for m in blocks:
        title = m.group(1).strip()
        body = m.group(2)
        # Strip exactly one trailing newline (the regex captures up to but
        # not including ``@@@ END``, so a body ending in ``\n`` carries one
        # artifact newline that would otherwise become a spurious blank line).
        if body.endswith("\n"):
            body = body[:-1]
        body_lines = body.split("\n")
        # Re-scan headers on the current result after every replacement, since
        # earlier replacements shift the line indices of later sections.
        headers = [
            i for i in range(len(result_lines)) if _is_plan_section_header(result_lines, i)
        ]
        target_idx = None
        title_key = title.strip()
        for h in headers:
            if title_key in _section_title_keys(result_lines[h]):
                target_idx = h
                break
        if target_idx is None:
            return None
        next_idx = len(result_lines)
        for h in headers:
            if h > target_idx:
                next_idx = h
                break
        result_lines = (
            result_lines[:target_idx] + body_lines + result_lines[next_idx:]
        )
    return "\n".join(result_lines)


def _build_plan_view(state, conv, rnd: int) -> tuple[str, list[str]]:
    """Build the plan input sent to a critic round.

    Returns ``(plan_view, journal_lines)``.

    - ``rnd < 2`` or the plan is smaller than
      ``C.LEAN_PLAN_FULL_THRESHOLD`` → the full ``conv.plan`` verbatim (early
      rounds and small plans always see the whole plan).
    - a non-empty ``plan_snapshot`` (the pre-reply plan captured by the
      previous ``debate_reply``) differs from ``conv.plan`` →
      ``condenser.plan_diff(snapshot, plan)`` (the lean view: only the
      changed sections).
    - the computed diff is empty (a no-op reply, or the snapshot equals the
      plan) or ``plan_snapshot`` is absent → the full plan, plus a journal
      line containing ``sent full plan`` so the lean-path fallback is
      observable.

    The ``condenser`` import is function-local to avoid the
    ``condenser``↔``nodes.debate`` import cycle (condenser imports
    ``TECH_LIMIT_RE`` from this module at load time).
    """
    plan = conv.plan or ""
    snapshot = state.get("plan_snapshot", "") if isinstance(state, dict) else ""
    if rnd < 2 or len(plan) < C.LEAN_PLAN_FULL_THRESHOLD:
        return plan, []
    if snapshot and snapshot != plan:
        from ..condenser import plan_diff
        diff = plan_diff(snapshot, plan)
        if diff:
            return diff, []
    return plan, [f"debate r{rnd}: sent full plan (no lean diff available)"]


def _latest_section(debate_text: str, critic: str) -> str:
    """Extract only the last '## Round N — <critic>' section from the debate file.

    The debate file accumulates all rounds (reviewer, UX, proposer).  Counting
    [BLOCKER] across the whole file picks up resolved blockers that the
    proposer restated in its reply (e.g. '**[BLOCKER] … — RESOLVED**').  This
    helper returns just the text of the most recent section for the given
    critic ('Reviewer' or 'UX'), so blocker counts reflect only the current
    round.
    """
    if not debate_text:
        return ""
    headers = list(_SECTION_HEADER_RE.finditer(debate_text))
    last_start = None
    next_start = len(debate_text)
    for i, m in enumerate(headers):
        if m.group(1).lower() == critic.lower():
            last_start = m.start()
            # Bound the section at the NEXT header, not end-of-file: otherwise it
            # swallows the following proposer/UX section, whose reply restates the
            # blocker as "[BLOCKER] … — RESOLVED" and inflates count_blockers
            # (the 2-vs-1 miscount that escalated a single-blocker debate).
            next_start = headers[i + 1].start() if i + 1 < len(headers) else len(debate_text)
    if last_start is None:
        return ""
    return debate_text[last_start:next_start]


def _open_blocker_count(verdict: str, out: str, latest_tech: str) -> int:
    """[BLOCKER] tokens count as OPEN blockers only when the critic actually
    blocked (VERDICT: REJECT).

    Under APPROVE / APPROVE_WITH_CHANGES the reviewer is shipping, so any
    [BLOCKER] token in that output is a reference to a resolved or prior item —
    counting it (or letting the stale-round `latest_tech` fallback resurrect an
    earlier REJECT's count) is what falsely escalated "N blocker(s) confirmed"
    after rounds 2-3 had downgraded the theme to a SUGGESTION with
    APPROVE_WITH_CHANGES. Trust the verdict over the loose severity marker.

    UNKNOWN stays conservative (still counts), so a review we could not parse
    can't silently converge as if it had approved.
    """
    if verdict in ("APPROVE", "APPROVE_WITH_CHANGES"):
        return 0
    return count_blockers(out) or count_blockers(latest_tech)


def _check_early_escalation(debate_text: str, rnd: int) -> dict | None:
    """Detect a stuck debate and escalate before the round cap is wasted.

    Scans the last ``C.DEBATE_STUCK_ROUNDS`` rounds for BLOCKER claims repeated
    in every round (via ``condenser.stuck_claims``). When the same claim
    persists across that many consecutive rounds, the debate is not converging
    — escalate early with a ``"debate stuck:"`` prefix so the human gets the
    continue/redo/stop/ok menu instead of burning the remaining rounds.

    The ``condenser`` import is function-local to avoid the
    ``condenser``↔``nodes.debate`` import cycle (condenser imports
    ``TECH_LIMIT_RE`` from this module at load time).
    """
    from ..condenser import stuck_claims

    stuck = stuck_claims(debate_text, C.DEBATE_STUCK_ROUNDS)
    if not stuck:
        return None
    claims = "; ".join(stuck)
    triage = _build_triage(debate_text, "debate stuck")
    return {
        "escalation": (
            f"debate stuck: {len(stuck)} blocker claim(s) repeated across "
            f"{C.DEBATE_STUCK_ROUNDS} consecutive rounds (round {rnd}) — "
            f"the debate is not converging: {claims}"
        ),
        "debate_next": "summary",
        "triage": triage,
        "hint": triage.get("recommended", ""),
        "journal": [
            f"debate r{rnd}: STUCK — {len(stuck)} blocker(s) repeated across "
            f"{C.DEBATE_STUCK_ROUNDS} rounds: {claims}"
        ],
    }


def _in_debate_grace(state: dict, rnd: int) -> bool:
    """True while a human ``continue`` grace window still covers this round.

    ``escalate()`` sets ``debate_grace_until = debate_round + 2`` on continue so
    stuck/thrashing early-escalation cannot steal the promised +2 rounds
    (TASK-023: continue → immediate re-thrash on the next critic pass).
    Requirements early-escalation ignores this window.
    """
    try:
        until = int(state.get("debate_grace_until") or 0)
    except (TypeError, ValueError):
        until = 0
    return until > 0 and rnd <= until


def _check_thrashing_escalation(debate_text: str, rnd: int) -> dict | None:
    """Detect a churning debate and escalate before the round cap is wasted.

    Uses ``condenser.thrashing_report`` over the last
    ``C.DEBATE_THRASH_ROUNDS`` rounds. When the trend is ``mode == "thrashing"``
    (blockers not strictly decreasing AND new claims appearing), the debate is
    churning, not converging — escalate early with a ``"debate thrashing:"``
    prefix so the human gets the same continue/redo/stop/ok menu as a stuck
    debate, plus a triage block (mode/blocker trend/repeated/new/recommended).

    Returns ``None`` when the mode is anything other than ``"thrashing"``
    (stuck/converging/unknown are handled by their own branches or by
    ``_debate_decision``).

    The ``condenser`` import is function-local to avoid the
    ``condenser``↔``nodes.debate`` import cycle (same load-bearing pattern as
    ``_check_early_escalation``).
    """
    from ..condenser import thrashing_report

    report = thrashing_report(debate_text, C.DEBATE_THRASH_ROUNDS)
    if report.get("mode") != "thrashing":
        return None
    triage = _build_triage(debate_text, "debate thrashing")
    trend = " → ".join(str(n) for n in report.get("blocker_counts", []))
    return {
        "escalation": (
            f"debate thrashing: blockers not decreasing ({trend}) with "
            f"{len(report.get('new', []))} new claim(s) across the last "
            f"{C.DEBATE_THRASH_ROUNDS} active rounds (round {rnd}) — the debate "
            f"is churning, not converging"
        ),
        "debate_next": "summary",
        "triage": triage,
        "hint": triage.get("recommended", ""),
        "journal": [
            f"debate r{rnd}: THRASHING — blockers {trend}, "
            f"{len(report.get('new', []))} new claim(s)"
        ],
    }


# Recommendation table for _build_triage, keyed by reason_prefix.
# thrashing/stuck → "ok" (proceed to the verdict — more rounds will not help).
# exhausted → mode-mapped (converging → "continue" to let it close; else "ok").
# requirements → "stop" (the blocker is in the brief, not the plan).
_EXHAUSTED_RECOMMENDED = {
    "thrashing": "ok",
    "stuck": "ok",
    "converging": "continue",
    "unknown": "ok",
}

_RATIONALE = {
    "thrashing": "blockers are not decreasing and new claims keep appearing — "
                 "more rounds will not converge",
    "stuck": "the same blocker has persisted across rounds — the debate is "
             "not moving",
    "converging": "blockers are strictly decreasing — the debate is closing",
    "unknown": "no clear trend — proceed to the verdict",
    "requirements": "the blocker is in the brief (REQUIREMENTS), not the plan "
                     "— amend the brief and regenerate the plan",
}


def _build_triage(debate_text: str, reason_prefix: str) -> dict:
    """Build the triage dict attached to a debate escalation.

    Always calls ``condenser.thrashing_report`` first to populate
    mode/blocker_counts/repeated/new, then adds ``recommended`` and
    ``rationale`` according to ``reason_prefix``:

    - ``"debate thrashing"`` → recommended ``"ok"`` (proceed to the verdict).
    - ``"debate stuck"`` → recommended ``"ok"`` (proceed to the verdict).
    - ``"debate exhausted"`` → recommended mode-mapped via
      ``_EXHAUSTED_RECOMMENDED`` (converging → ``"continue"``; else ``"ok"``).
    - ``"debate requirements"`` → forces ``mode="requirements"`` and
      ``recommended="stop"`` (the blocker is in the brief, not the plan), while
      still calling ``thrashing_report`` first so blocker_counts/repeated/new
      are populated for the CLI/bot rendering.

    The returned dict always has the six keys
    mode/blocker_counts/repeated/new/recommended/rationale.
    """
    from ..condenser import thrashing_report

    report = thrashing_report(debate_text, C.DEBATE_THRASH_ROUNDS)
    mode = report.get("mode", "unknown")
    if reason_prefix == "debate requirements":
        recommended = "stop"
        rationale = _RATIONALE["requirements"]
        mode = "requirements"
    elif reason_prefix == "debate exhausted":
        recommended = _EXHAUSTED_RECOMMENDED.get(mode, "ok")
        rationale = _RATIONALE.get(mode, _RATIONALE["unknown"])
    elif reason_prefix in ("debate thrashing", "debate stuck"):
        recommended = "ok"
        rationale = _RATIONALE.get(mode, _RATIONALE["unknown"])
    else:
        recommended = "ok"
        rationale = _RATIONALE.get(mode, _RATIONALE["unknown"])
    return {
        "mode": mode,
        "blocker_counts": report.get("blocker_counts", []),
        "repeated": report.get("repeated", []),
        "new": report.get("new", []),
        "recommended": recommended,
        "rationale": rationale,
    }


def _check_requirements_escalation(debate_text: str) -> dict | None:
    """Detect a REQUIREMENTS-provenanced blocker in the latest round.

    Item 29-31: a ``[BLOCKER:REQUIREMENTS]`` tag means the critic believes the
    issue lives in the brief, not the plan — the debate cannot fix it by
    iterating on the plan, so escalate immediately with a
    ``"debate requirements:"`` prefix. The human gets the stop/ok menu (item
    33): ``ok`` proceeds to the verdict (clearing the bonus so a later redo
    starts from the default cap), ``stop`` ends the run so the brief can be
    amended and the plan regenerated.

    The ``condenser`` import is function-local to avoid the
    ``condenser``↔``nodes.debate`` import cycle (condenser imports
    ``TECH_LIMIT_RE`` from this module at load time) — the same load-bearing
    pattern as ``_check_early_escalation``.
    """
    from ..condenser import latest_requirements_blockers

    claims = latest_requirements_blockers(debate_text)
    if not claims:
        return None
    joined = "; ".join(claims)
    triage = _build_triage(debate_text, "debate requirements")
    return {
        "escalation": (
            f"debate requirements: {len(claims)} blocker(s) tagged as "
            f"belonging to the REQUIREMENTS (the brief), not the plan — "
            f"the debate cannot fix them by iterating on the plan: {joined}"
        ),
        "debate_next": "summary",
        "triage": triage,
        "hint": triage.get("recommended", ""),
        "journal": [
            f"debate: REQUIREMENTS blocker(s) — {len(claims)} item(s): {joined}"
        ],
    }


def debate_tech(state):
    """The technical critic. Critiques the plan AND certifies TECH-LIMIT claims.

    A round begins here; it re-critiques the proposer's latest plan, so this pass
    is also the verification of the previous reply — no separate verify node.
    It does NOT decide whether a UX blocker is acceptable (that is the designer's
    call in debate_ux); it only rules whether a claimed technical constraint is
    real, citing the code.

    Rounds 1..MAX_DEBATE_ROUNDS are full cycles: critique → reply → next.
    Round MAX_DEBATE_ROUNDS+1 is a verification-only pass: the critics re-check
    the proposer's last reply, but there is NO reply after it.  If the
    verification is clean → summary; if blockers remain → escalate with FRESH
    counts (not the stale pre-reply numbers that caused false escalations).
    """
    tid = state["task_id"]
    rnd = state.get("debate_round", 0) + 1
    is_verification = rnd > C.resolved_debate_rounds(state)
    conv = Conversation.from_state(state)
    # TASK-023: send a lean plan_view (only the changed sections) to round-2+
    # critics once the plan is large; round 1 and small plans get the full plan.
    plan_view, plan_view_journal = _build_plan_view(state, conv, rnd)
    code, out = run_agent(
        "PLAN_REVIEWER",
        conv,
        f"debate-r{rnd}-tech",
        template="debate_review",
        round=rnd,
        plan_view=plan_view,
    )
    health, _signal = classify_output(code, out)
    # A bare "VERDICT: APPROVE" (17 bytes) is the expected output format when
    # the reviewer's false-positive filter deletes all items — the prompt
    # explicitly says "If nothing survives the filter: VERDICT: APPROVE". The
    # near-empty-output guard must not fire on a parseable verdict; only
    # UNKNOWN (no verdict found) with bad health is untrustworthy.
    verdict_pre = parse_verdict(out)
    if verdict_pre == "UNKNOWN" and not _trust_output(code, out, health):
        return {
            "debate_round": rnd,
            "escalation": f"debate tech round {rnd} produced untrustworthy output — refusing to act on it (see journal for diagnostics)",
            "journal": [
                f"debate r{rnd} tech: UNTRUSTWORTHY output — health={health}, exit={code}, "
                f"{len(out)} bytes"
            ],
        }
    debate_path = C.DEBATES / f"DEBATE-{tid}.md"
    text = _file_or_stdout(
        debate_path, out, content=f"\n\n## Round {rnd} — Reviewer\n\n{out.strip()}\n", append=True
    )
    if not text.strip():
        _recover_artifact(tid, f"DEBATE-{tid}.md", debate_path)
        text = read_if_exists(debate_path)
    latest_tech = _latest_section(text, "Reviewer")
    verdict = parse_verdict(out) if parse_verdict(out) != "UNKNOWN" else parse_verdict(text)
    blockers = _open_blocker_count(verdict, out, latest_tech)
    limits = sorted(set(TECH_LIMIT_RE.findall(out) + TECH_LIMIT_RE.findall(latest_tech)))
    delta = {
        "debate_round": rnd,
        "reviewer_verdict": verdict,
        "open_blockers": blockers,
        "tech_limits": limits,
        "redo_debate": False,
        "debate_text": text,
        "journal": [
            *plan_view_journal,
            f"debate r{rnd} tech: {verdict}, {blockers} blockers, "
            f"{len(limits)} tech-limit(s) verified"
        ],
    }
    if not state.get("has_ui") or not C.UX_RENDER_CMD.strip():
        # No UX critic on this task: decide the round here. This also covers a
        # UI task whose repo has no render command configured (UX_RENDER_CMD
        # empty) — the visual review is disabled, so the designer never weighs
        # in and the round is decided on the technical critique alone.
        bypass_note = None
        if state.get("has_ui") and not C.UX_RENDER_CMD.strip():
            bypass_note = "visual review disabled — no render command configured for this repo"
            delta.setdefault("journal", []).append(bypass_note)
        # Precedence chain (TASK-022): requirements > early(stuck) > thrashing
        # > _debate_decision. Each early-escalation branch that fires sets
        # delta["triage"] and delta["hint"] via _build_triage so the CLI/bot
        # render the triage block and the recommended-answer highlight.
        # Item 30: _check_requirements_escalation takes precedence over
        # _check_early_escalation when both fire — a REQUIREMENTS blocker is
        # unfixable by debate iteration, so the "debate requirements:" menu
        # (stop/ok) is the one the human needs, not the continue/redo/stop/ok
        # stuck menu.
        # Grace window: after human continue, skip stuck/thrashing early-stop
        # until debate_grace_until (requirements still fire).
        grace = _in_debate_grace({**state, **delta}, rnd)
        early = None if grace else _check_early_escalation(text, rnd)
        thrash = None if grace else _check_thrashing_escalation(text, rnd)
        req = _check_requirements_escalation(text)
        if req:
            decision = req
        elif early:
            decision = early
        elif thrash:
            decision = thrash
        else:
            # _debate_decision can return its own journal on verification paths
            # (zero-blocker convergence or blocker-confirmed escalation); merge
            # instead of letting delta.update drop the bypass note (item 28).
            decision = _debate_decision({**state, **delta}, is_verification=is_verification)
        if bypass_note and "journal" in decision:
            decision["journal"] = [bypass_note, *decision["journal"]]
        delta.update(decision)
    return delta


def debate_ux(state):
    """The designer. Authority on the user experience for this task.

    Its findings are followed; it re-reviews the plan each round and decides what
    to do about any verified TECH-LIMIT — accept it, accept it while proposing an
    alternative that meets the UX intent within the constraint, or dispute it.
    Convergence is ux_blockers == 0: the designer is satisfied.
    """
    tid = state["task_id"]
    rnd = state.get("debate_round", 0)
    conv = Conversation.from_state(state)
    # TASK-023: lean plan_view for the designer too, keyed on the current
    # debate_round (the round the UX critic is re-reviewing).
    plan_view, plan_view_journal = _build_plan_view(state, conv, rnd)

    # The UX reviewer is print-only (gemini): it prints the review, WE file it.
    # Asking it to write files itself makes its CLI attempt a tool call and
    # return an empty/errored response — which then got saved as the "review",
    # reading back as UNKNOWN/0-blockers and silently voiding the designer.
    # Gemini sometimes needs a few seconds to recover from a "malformed tool
    # call" state before a retry can succeed — an immediate re-call hits the
    # same error (observed on task-008: two identical 137-byte failures 7s
    # apart, while a 74s gap on the earlier run produced a 1319-byte review).
    UX_REVIEW_RETRIES = int(os.environ.get("PIPELINE_UX_REVIEW_RETRIES", "3"))
    UX_RETRY_BACKOFF_S = int(os.environ.get("PIPELINE_UX_REVIEW_BACKOFF_S", "10"))
    review, verdict = "", "UNKNOWN"
    for attempt in range(UX_REVIEW_RETRIES):
        step = f"debate-r{rnd}-ux" + (f"-retry{attempt}" if attempt else "")
        code, out = run_agent(
            "UX_REVIEWER",
            conv,
            step,
            template="debate_ux",
            round=rnd,
            tech_limits="; ".join(state.get("tech_limits", [])) or "none",
            docs_dir=C.DOCS_REL,
            plan_view=plan_view,
        )
        verdict = parse_verdict(out)
        if verdict != "UNKNOWN":
            health, _signal = classify_output(code, out)
            # A bare "VERDICT: APPROVE" is valid when the UX filter deletes all
            # items — _trust_output whitelists terminal markers, so this guard
            # only fires on genuinely untrustworthy output (bad health, no
            # terminal marker, not just short).
            if not _trust_output(code, out, health):
                return {
                    "ux_verdict": "UNKNOWN",
                    "ux_blockers": 0,
                    "ux_shipped_blocked": True,
                    "escalation": f"debate UX round {rnd} produced untrustworthy output despite a parseable verdict — refusing to act on it (see journal for diagnostics)",
                    "degradations": [
                        "UX critic produced untrustworthy output — shipped without a designer critique"
                    ],
                    "journal": [
                        f"debate r{rnd} ux: UNTRUSTWORTHY output — health={health}, exit={code}, "
                        f"{len(out)} bytes"
                    ],
                }
            review = out
            break
        if attempt < UX_REVIEW_RETRIES - 1:
            time.sleep(UX_RETRY_BACKOFF_S)

    if verdict == "UNKNOWN":
        # A UI task with no usable design review must not proceed silently as if
        # UX passed. Stop and tell the human; resuming proceeds to the verdict
        # WITHOUT a UX critique (fix the UX agent/prompt and rerun for a real one).
        return {
            "ux_verdict": "UNKNOWN",
            "ux_blockers": 0,
            "ux_shipped_blocked": True,
            "escalation": f"UX critic produced no usable review after "
            f"{UX_REVIEW_RETRIES} tries (round {rnd}) — the designer "
            "never weighed in on a UI task. Resuming proceeds to the "
            "verdict without UX; to get a real critique, fix the UX "
            "agent and rerun.",
            "degradations": [
                "UX critic produced no usable review — shipped without a designer critique"
            ],
            "journal": [f"debate r{rnd} ux: FAILED — no verdict in output"],
        }

    # Materialise the critique ourselves — never depend on the agent writing it.
    (C.REVIEWS / f"UX-{tid}.md").write_text(review)
    debate_path = C.DEBATES / f"DEBATE-{tid}.md"
    with debate_path.open("a") as f:
        f.write(f"\n\n## Round {rnd} — UX\n\n{review.strip()}\n")

    blockers = 0 if verdict in ("APPROVE", "APPROVE_WITH_CHANGES") else count_blockers(review)

    # Rubber-stamp guard: on a re-review, a bare APPROVE that does not walk the
    # blockers it raised before is the failure we saw on 009 (a 168-byte
    # "APPROVE, all compliant" right after a 3-blocker REJECT). The prompt now
    # demands RESOLVED/STILL OPEN lines; if a clean APPROVE has none while prior
    # blockers existed, don't trust it — keep the round open so the designer must
    # actually verify (or the cap escalates honestly) instead of false-converging.
    prior_blockers = state.get("ux_blockers", 0)
    if (
        rnd > 1
        and prior_blockers > 0
        and verdict == "APPROVE"
        and blockers == 0
        and "RESOLVED" not in review.upper()
    ):
        verdict = "APPROVE_WITH_CHANGES"
        blockers = prior_blockers
        ev.emit(
            "degraded",
            tid,
            "debate_ux",
            f"round {rnd}: bare APPROVE after {prior_blockers} prior blocker(s) "
            "with no RESOLVED lines — treated as unverified, not converged",
        )
        delta_note = " (rubber-stamp APPROVE rejected — prior blockers not walked)"
    else:
        delta_note = ""

    # Read the debate file AFTER the UX section was appended above, so the
    # just-filed UX critique is in the scanned text. Used both for the delta
    # (item 11) and for the early-escalation checks below.
    debate_text = read_if_exists(debate_path)
    delta = {
        "ux_verdict": verdict,
        "ux_blockers": blockers,
        "debate_text": debate_text,
        "journal": [
            *plan_view_journal,
            f"debate r{rnd} ux: {verdict}, {blockers} blockers{delta_note}",
        ],
    }
    is_verification = rnd > C.resolved_debate_rounds(state)
    # Precedence chain (TASK-022): requirements > early(stuck) > thrashing
    # > _debate_decision. Checked after the debate file append (so the
    # just-filed UX section is in the scanned text) and before _debate_decision.
    # Each early-escalation branch that fires sets delta["triage"] and
    # delta["hint"] via _build_triage.
    # Item 30: _check_requirements_escalation takes precedence over
    # _check_early_escalation when both fire (same precedence rule as
    # debate_tech).
    # Grace window: after human continue, skip stuck/thrashing early-stop
    # until debate_grace_until (requirements still fire).
    grace = _in_debate_grace(state, rnd)
    early = None if grace else _check_early_escalation(debate_text, rnd)
    thrash = None if grace else _check_thrashing_escalation(debate_text, rnd)
    req = _check_requirements_escalation(debate_text)
    if req:
        delta.update(req)
    elif early:
        delta.update(early)
    elif thrash:
        delta.update(thrash)
    else:
        delta.update(_debate_decision({**state, **delta}, is_verification=is_verification))
    return delta


def _debate_decision(state, is_verification: bool = False) -> dict:
    """Both critiques are in for this round: converge, loop to reply, or escalate.

    Normal rounds: if blockers remain, route to reply (the proposer gets a
    chance to respond).  Verification round (round > MAX_DEBATE_ROUNDS): no
    reply is allowed — if blockers remain, escalate with the FRESH counts from
    this round's critiques, not the stale pre-reply numbers.
    """
    tech_ok = state.get("reviewer_verdict") == "APPROVE" and not state.get("open_blockers", 0)
    # A UI task with no render command configured has its visual review
    # disabled (debate_tech skips the UX critic in that case), so treat the UX
    # gate as satisfied — same as a non-UI task — instead of stalling on a
    # verdict that will never arrive.
    ux_ok = (not state.get("has_ui") or not C.UX_RENDER_CMD.strip()) or (
        state.get("ux_verdict") == "APPROVE" and not state.get("ux_blockers", 0)
    )
    if tech_ok and ux_ok:
        return {"debate_next": "summary"}
    if is_verification:
        tech_b = state.get("open_blockers", 0)
        ux_b = state.get("ux_blockers", 0)
        n = tech_b + ux_b
        if n == 0:
            # Zero blockers survived verification. The only reason we are not in
            # the clean-APPROVE branch above is that a critic kept a SUGGESTION
            # open, so the verdict stayed APPROVE_WITH_CHANGES instead of a plain
            # APPROVE. No blocker means it is safe to ship — converge, do not
            # escalate on the verdict string alone (the spurious 0-blocker
            # escalation). Unresolved suggestions stay in the plan for implement.
            return {
                "debate_next": "summary",
                "journal": [
                    "debate verification: 0 blockers (unresolved suggestions only) — converged"
                ],
            }
        who = (
            ("technical" if tech_b else "")
            + (" and " if tech_b and ux_b else "")
            + ("UX" if ux_b else "")
        )
        # TASK-022 item 12: the exhausted branch attaches a triage block (via
        # _build_triage with reason_prefix "debate exhausted", reading
        # debate_text from state) and a hint so the CLI/bot render the trend
        # and the recommended-answer highlight. debate_text is set on the
        # delta by debate_tech/debate_ux (item 11) and threaded into state via
        # the {**state, **delta} merge at the call site.
        debate_text = state.get("debate_text", "")
        triage = _build_triage(debate_text, "debate exhausted")
        return {
            "debate_next": "summary",
            "escalation": f"debate exhausted {C.resolved_debate_rounds(state)} rounds + "
            f"verification: {n} {who} blocker(s) confirmed by the "
            f"critics after the proposer's final reply",
            "triage": triage,
            "hint": triage.get("recommended", ""),
            "journal": [f"debate verification: {n} {who} blocker(s) confirmed"],
        }
    return {"debate_next": "reply"}


def debate_reply(state):
    """The proposer replies to both critics and updates the plan.

    TASK-023 contract: the proposer prints per-item notes AND a plan patch
    enclosed in ``=== PLAN PATCH START/END ===`` markers, containing one or
    more ``@@@ REPLACE section: "<title>" … @@@ END`` blocks. The pipeline
    applies the patch to ``PLAN-{tid}.md`` and never lets the proposer edit
    the plan file directly — any direct edit is reverted.

    Ownership rule (ruling 1): every branch that is NOT a successful
    ``"patch"`` or ``"legacy"`` apply ends with ``_save(PLAN-{tid}.md,
    pre_plan)`` — the file is always exactly what it was before this reply's
    ``run_agent`` call unless a validated apply changed it. ``pre_plan`` is
    captured BEFORE ``run_agent`` so a direct edit during the call is
    overwritten on every non-apply path (no-marker, malformed envelope, and
    apply-failure). The restore writes ``pre_plan`` verbatim (no appended
    ``"\\n"``) so the file is byte-exact (ruling 3).

    Every return path carries ``plan_snapshot: pre_plan`` so the next critic
    round can diff the snapshot against the (possibly patched) current plan
    and send only the changed sections via ``_build_plan_view``.
    """
    tid = state["task_id"]
    rnd = state["debate_round"]
    conv = Conversation.from_state(state)
    plan_path = C.PLANS / f"PLAN-{tid}.md"
    # Capture the pre-reply plan BEFORE run_agent so a direct edit during the
    # agent call is overwritten on every non-apply path (rulings 1 + 3).
    pre_plan = read_if_exists(plan_path)
    _, out = run_agent(
        "PROPOSER",
        conv,
        f"debate-r{rnd}-reply",
        template="debate_reply",
        round=rnd,
        tech_limits="; ".join(state.get("tech_limits", [])) or "none",
    )
    debate_path = C.DEBATES / f"DEBATE-{tid}.md"
    kind, body = _extract_plan_or_patch(out)

    if kind == "patch":
        new_plan = _apply_section_patch(pre_plan, body)
        if new_plan is None:
            # Apply failed (missing title or section not found): restore the
            # pre-reply plan verbatim and escalate. The literal "plan patch
            # apply failed" prefix lets the router/escalate path recognize it.
            _save(plan_path, pre_plan)
            reply = out.strip()
            if reply:
                _save(debate_path, f"\n\n## Round {rnd} — Reply\n\n{reply}\n", append=True)
            return {
                "escalation": (
                    "plan patch apply failed: could not apply section patch "
                    "(missing section title or cited section not found in plan)"
                ),
                "plan_snapshot": pre_plan,
                "journal": [
                    f"debate r{rnd}: plan patch apply failed — "
                    f"plan restored to pre-reply snapshot"
                ],
            }
        _save(plan_path, new_plan)
        reply = out.strip()
        if reply:
            _save(debate_path, f"\n\n## Round {rnd} — Reply\n\n{reply}\n", append=True)
        return {
            "plan_snapshot": pre_plan,
            "journal": [f"debate r{rnd}: proposer replied with a section patch"],
        }

    if kind == "legacy":
        # Discouraged: the proposer printed the whole plan instead of a patch.
        # Save it (it is the new plan) and record a degradation so the legacy
        # fallback is observable. plan_snapshot is still pre_plan (ruling 14).
        _save(plan_path, body + "\n")
        reply = PLAN_MARKER_RE.sub("", out).strip()
        if reply:
            _save(debate_path, f"\n\n## Round {rnd} — Reply\n\n{reply}\n", append=True)
        return {
            "plan_snapshot": pre_plan,
            "degradations": ["debate reply used full-plan markers (legacy)"],
            "journal": [
                f"debate r{rnd}: proposer replied with full-plan markers (legacy, degraded)"
            ],
        }

    if kind == "malformed":
        # A partial/unmatched envelope or a stray @@@ token: restore the
        # pre-reply plan and escalate. Per C3, any @@@/envelope token outside
        # a well-formed block is an apply failure, not a silent no-op.
        _save(plan_path, pre_plan)
        reply = out.strip()
        if reply:
            _save(debate_path, f"\n\n## Round {rnd} — Reply\n\n{reply}\n", append=True)
        return {
            "escalation": (
                "plan patch apply failed: malformed plan patch envelope "
                "(unmatched PLAN PATCH START/END or stray @@@ token)"
            ),
            "plan_snapshot": pre_plan,
            "journal": [
                f"debate r{rnd}: malformed plan patch envelope — "
                f"plan restored to pre-reply snapshot"
            ],
        }

    # (None, None): a genuine no-patch, per-item-only (or direct-edit-only)
    # reply. Unconditionally restore pre_plan (rulings 1 + 3) — even when it
    # is empty — so a direct edit during run_agent is always reverted.
    _save(plan_path, pre_plan)
    reply = out.strip()
    if reply:
        _save(debate_path, f"\n\n## Round {rnd} — Reply\n\n{reply}\n", append=True)
    if pre_plan:
        journal_line = (
            f"debate r{rnd}: no plan patch markers — plan restored to pre-reply snapshot"
        )
    else:
        journal_line = (
            f"debate r{rnd}: no plan patch markers — no prior plan to restore (reverted to empty)"
        )
    return {
        "plan_snapshot": pre_plan,
        "journal": [journal_line],
    }
