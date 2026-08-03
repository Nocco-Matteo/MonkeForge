"""Typed state carried through the graph and persisted by the checkpointer."""
from __future__ import annotations
import dataclasses
import json
from pathlib import Path
from typing import Annotated, Literal, TypedDict
import operator

from . import config as C


def read_if_exists(path: Path) -> str:
    """Local copy of `agents.read_if_exists` to avoid an `agents`->`state` import
    cycle (`agents.py` imports `Conversation` from here)."""
    return path.read_text() if path.exists() else ""


class Batch(TypedDict):
    n: int
    scope: str
    status: Literal["PENDING", "DONE"]
    outcome: str
    deviations: str


class PipelineState(TypedDict, total=False):
    # --- inputs
    task_id: str
    request: str
    auto: bool
    branch: str

    interview: bool                # force the intake interview even under --auto

    # --- intake
    intake_round: int              # interview rounds consumed
    intake_done: bool              # the brief is final
    intake_unanswered: bool        # resumed without new answers: ask again, don't spend a round
    brief_path: str                # docs/tasks/TASK-<id>-brief.md

    # --- debate (two critics in parallel: technical + UX)
    debate_round: int
    debate_round_bonus: int        # extra rounds added by a human "continue" answer on a debate-cap escalation
    reviewer_verdict: str          # PLAN_REVIEWER (technical): APPROVE | APPROVE_WITH_CHANGES | REJECT
    open_blockers: int             # technical blockers still open
    ux_verdict: str                # UX_REVIEWER: APPROVE | APPROVE_WITH_CHANGES | REJECT
    ux_blockers: int               # UX blockers still open (0 = designer satisfied)
    tech_limits: list[str]         # UX items the reviewer verified as blocked by a real tech constraint
    debate_next: str               # "summary" | "reply" — where the round router should go

    # --- verdict / plan
    batches: list[Batch]
    batch_idx: int                 # index into batches of the batch being built
    has_ui: bool                   # set from the brief's UI-SURFACE marker, before the debate
    ux_shipped_blocked: bool       # a UX escalation was overridden; blockers shipped
    # --- visual review (the "eyes": renders the built UI, critiques screenshots)
    ux_render_cycle: int           # render→critique→fix cycles consumed
    visual_verdict: str            # VISUAL_REVIEWER on the screenshots
    visual_blockers: int           # unresolved visual blockers
    prev_visual_blockers: int      # blocker count last cycle (plateau detection)
    visual_no_progress: int        # consecutive cycles with no blocker reduction
    render_facts: str              # deterministic signals (overflow, mode-diff, empty space)
    visual_shipped_blocked: bool   # visual blockers overridden at the cap
    # --- render gate (perf: counts re-renders per subtree, catches regressions)
    has_perf: bool                 # set from the brief's PERF-SURFACE marker (opt-in)
    render_verdict: str            # PASS | REGRESSED | BASELINE | SKIPPED (deterministic)
    render_blockers: int           # subtrees re-rendering MORE than baseline
    render_cycle: int              # measure→fix cycles consumed
    prev_render_blockers: int      # regression count last cycle (plateau detection)
    render_no_progress: int        # consecutive cycles with no regression reduction
    discovered_scope: str
    trusted_context: str

    # --- per-batch loop
    fix_cycle: int
    test_fix_attempt: int
    test_fix_failures: list[str]    # failing-test identifiers from the previous implement attempt (retry prompt)
    test_fix_summary: str           # one-line test summary from the previous implement attempt (retry prompt)
    tests_waived: bool              # human waived the in-graph test gate for this batch
    final_tests_waived: bool        # human waived the final test gate (ship with known failures)
    baseline_batch_n: int           # batch ``n`` for which batch_test_baseline was captured
    batch_test_baseline: list[str]  # vitest FAIL keys at first implement entry (pre-agent)
    task_baseline: list[str]        # failures present at task start — tolerated by the FINAL gate
    batch_base_ref: str             # git HEAD before this batch — the diff base for code_review
    code_verdict: str
    not_met: list[str]
    disputed: list[str]

    # --- adaptive effort (TASK-011)
    effort: str                    # "scout-monke" | "troop-monke" | "barrel-monke"
    effort_forced: bool            # effort set via --effort (skip the effort checkpoint)
    effort_hint_signals: dict      # signals _extract_effort_signals produced (for the hint)
    effort_checkpoint_shown: bool  # the effort checkpoint already ran (suppress re-show)

    # --- control
    db_degraded: bool              # last test run had the e2e DB unreachable
    escalation: str                # non-empty -> escalate node
    retry_judge: bool              # human answered ok to a judge escalation — re-run judge
    finished: bool

    # --- append-only degradation ledger (every compromise, unified for wrap_up)
    degradations: Annotated[list[str], operator.add]

    # --- append-only journal (each node adds one line; survives checkpointing)
    journal: Annotated[list[str], operator.add]


@dataclasses.dataclass(frozen=True)
class Conversation:
    """Read-only snapshot of `PipelineState` + on-disk artifacts handed to agents.

    Built once per node via `from_state`; agents render prompts from it instead
    of receiving an opaque pre-rendered string. `frozen=True` blocks
    reassignment and `journal` is a `tuple` so in-place mutation
    (`conv.journal.append(...)`) raises `AttributeError` — the read-only
    contract from the brief holds against both reassignment and in-place edits.
    """

    task_id: str
    request: str
    brief: str
    plan: str
    debate_history: str
    debate_ledger: str
    batch_context: str
    review_history: str
    final: str
    progress: str
    summary: str
    visual_review: str
    journal: tuple[str, ...]

    @classmethod
    def from_state(cls, state: "PipelineState") -> "Conversation":
        """Snapshot `state` + on-disk brief/plan/debate/reviews into a frozen
        `Conversation`. Single construction site so field derivations live in
        one place. Disk reads are accepted by the brief (no caching layer)."""
        task_id = state.get("task_id", "")
        request = state.get("request", "")
        brief = read_if_exists(C.TASKS / f"TASK-{task_id}-brief.md")
        plan = read_if_exists(C.PLANS / f"PLAN-{task_id}.md")
        debate_history = read_if_exists(C.DEBATES / f"DEBATE-{task_id}.md")
        # Function-local import to avoid an import cycle (condenser imports
        # from agents, agents imports Conversation from state — same D2 pattern
        # as run_agent's function-local condenser import).
        from .condenser import debate_ledger as _debate_ledger
        debate_ledger = _debate_ledger(debate_history)
        batch_context = json.dumps(
            {
                "batch_idx": state.get("batch_idx", 0),
                "batches": state.get("batches", []),
            }
        )
        # Fixed order: CODE-* (sorted lexicographically — deterministic, the
        # brief only requires "concatenated text"; b1, b10, b2 ordering is
        # stable and documented), then UX, then VISUAL. Each non-empty file
        # contributes a `--- STEM ---` header (P5 structural separator).
        review_parts: list[str] = []
        for path in sorted(C.REVIEWS.glob(f"CODE-{task_id}-b*.md")):
            body = read_if_exists(path)
            if body:
                review_parts.append(f"--- {path.stem} ---\n\n{body}")
        for name in (f"UX-{task_id}.md", f"VISUAL-{task_id}.md"):
            path = C.REVIEWS / name
            body = read_if_exists(path)
            if body:
                review_parts.append(f"--- {path.stem} ---\n\n{body}")
        review_history = "\n\n".join(review_parts)
        final = read_if_exists(C.FINAL / f"FINAL-{task_id}.md")
        progress = read_if_exists(C.FINAL / f"PROGRESS-{task_id}.md")
        summary = read_if_exists(C.DEBATES / f"SUMMARY-{task_id}.md")
        visual_review = read_if_exists(C.REVIEWS / f"VISUAL-{task_id}.md")
        # Copy + freeze: tuple(state.get(...)) so neither reassignment nor
        # in-place mutation can corrupt the snapshot.
        journal = tuple(state.get("journal", []))
        return cls(
            task_id=task_id,
            request=request,
            brief=brief,
            plan=plan,
            debate_history=debate_history,
            debate_ledger=debate_ledger,
            batch_context=batch_context,
            review_history=review_history,
            final=final,
            progress=progress,
            summary=summary,
            visual_review=visual_review,
            journal=journal,
        )
