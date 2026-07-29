"""Typed state carried through the graph and persisted by the checkpointer."""
from __future__ import annotations
from typing import Annotated, Literal, TypedDict
import operator


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
    tests_waived: bool              # human waived the in-graph test gate for this batch
    final_tests_waived: bool        # human waived the final test gate (ship with known failures)
    baseline_batch_n: int           # batch ``n`` for which batch_test_baseline was captured
    baseline_failures: int          # tests already failing on the branch before any batch
    batch_test_baseline: list[str]  # vitest FAIL keys at first implement entry (pre-agent)
    task_baseline: list[str]        # failures present at task start — tolerated by the FINAL gate
    batch_base_ref: str             # git HEAD before this batch — the diff base for code_review
    code_verdict: str
    not_met: list[str]
    disputed: list[str]

    # --- control
    db_degraded: bool              # last test run had the e2e DB unreachable
    escalation: str                # non-empty -> escalate node
    finished: bool

    # --- append-only degradation ledger (every compromise, unified for wrap_up)
    degradations: Annotated[list[str], operator.add]

    # --- append-only journal (each node adds one line; survives checkpointing)
    journal: Annotated[list[str], operator.add]
