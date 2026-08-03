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


def _extract_plan(text: str) -> str | None:
    """Extract the updated plan from debate_reply stdout (between markers).

    The proposer prints per-item notes AND the complete updated plan between
    ``=== PLAN START ===`` / ``=== PLAN END ===`` markers.  This returns only the
    plan text, or None if the markers are absent (backward compat).
    """
    m = PLAN_MARKER_RE.search(text or "")
    return m.group(1).strip() if m else None


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
    code, out = run_agent(
        "PLAN_REVIEWER",
        conv,
        f"debate-r{rnd}-tech",
        template="debate_review",
        round=rnd,
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
        "journal": [f"debate r{rnd} ux: {verdict}, {blockers} blockers{delta_note}"],
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
    tid = state["task_id"]
    rnd = state["debate_round"]
    conv = Conversation.from_state(state)
    _, out = run_agent(
        "PROPOSER",
        conv,
        f"debate-r{rnd}-reply",
        template="debate_reply",
        round=rnd,
        tech_limits="; ".join(state.get("tech_limits", [])) or "none",
    )
    debate_path = C.DEBATES / f"DEBATE-{tid}.md"
    # Extract the updated plan from between the markers; the per-item notes
    # (everything outside the markers) go into the debate file as the reply.
    plan_text = _extract_plan(out)
    if plan_text:
        _save(C.PLANS / f"PLAN-{tid}.md", plan_text + "\n")
        reply = PLAN_MARKER_RE.sub("", out).strip()
    else:
        # No markers: the agent may have updated the plan file directly in the
        # target repo instead of printing it. Try to recover it.
        plan_path = C.PLANS / f"PLAN-{tid}.md"
        _recover_artifact(tid, f"PLAN-{tid}.md", plan_path)
        reply = out.strip()
    if reply:
        _save(debate_path, f"\n\n## Round {rnd} — Reply\n\n{reply}\n", append=True)
    return {"journal": [f"debate r{rnd}: proposer replied to both critics"]}
