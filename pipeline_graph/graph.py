"""Graph wiring: nodes, conditional edges, checkpointing."""
from __future__ import annotations

import functools
import traceback

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import GraphBubbleUp

from . import config as C, events as ev, nodes as N
from .nodes.common import _set_router_error
from .state import PipelineState


# --- routers ---------------------------------------------------------------

def _safe_router(fn):
    """Wrap a router so a crash fails into escalation instead of killing the run.

    A router is a pure function of state that returns the next node name. If it
    raises, langgraph has no edge for the exception and the whole run dies with
    a traceback on stdout that nothing pushes to a human. Instead, stash a
    plain-language reason (no exception class name — that stays in the journal
    and the ``step_error`` event) via ``_set_router_error`` and return
    ``"escalate"`` so the run pauses for a human with a resumable stop path.
    ``GraphBubbleUp`` (interrupt/Command control flow) is re-raised untouched.
    """
    @functools.wraps(fn)
    def wrapper(state):
        try:
            return fn(state)
        except GraphBubbleUp:
            raise
        except Exception as exc:
            tid = state.get("task_id", "?")
            name = fn.__name__
            reason = (
                f"routing failed in {name} — the pipeline could not choose "
                "the next step (see journal for the exception)"
            )
            _set_router_error(tid, reason)
            ev.emit(
                "step_error",
                tid,
                name,
                f"{type(exc).__name__}: {exc}",
                traceback=traceback.format_exc()[-2000:],
            )
            return "escalate"
    return wrapper


def after(node_result_key: str = "escalation"):
    """Any node may set `escalation`; that always wins."""
    @_safe_router
    def router(state):
        return "escalate" if state.get("escalation") else "continue"
    return router


@_safe_router
def route_after_init(state):
    if state.get("escalation"):
        return "escalate"
    return "intake" if N.intake_enabled(state) and not state.get("intake_done") \
        else "plan"


@_safe_router
def route_intake(state):
    """After the interviewer ran: done, or go wait for the human."""
    if state.get("escalation"):
        return "escalate"
    return "plan" if state.get("intake_done") else "wait"


@_safe_router
def route_intake_wait(state):
    """After the human answered: done, ask again, or wait once more.

    Resuming without having written any new answer must not burn an interviewer
    round — it bounces straight back to the gate instead.
    """
    if state.get("escalation"):
        return "escalate"
    if state.get("intake_done"):
        return "plan"
    return "wait" if state.get("intake_unanswered") else "ask"


@_safe_router
def route_after_tech(state):
    """After the technical critic: get the UX critique too (if any), else decide."""
    if state.get("escalation"):
        return "escalate"
    # A UI task with no render command configured has its visual review disabled
    # — skip the UX critic the same way a non-UI task does, so has_ui defaulting
    # True can't drag a backend repo into a designer critique that renders nothing.
    if state.get("has_ui") and C.UX_RENDER_CMD.strip():
        return "ux"
    return state.get("debate_next", "reply")   # no UX critic: tech node already decided


@_safe_router
def route_debate(state):
    """After both critiques are in (set by _debate_decision in the last critic)."""
    if state.get("escalation"):
        return "escalate"
    return state.get("debate_next", "reply")


@_safe_router
def route_after_checkpoint_effort(state):
    """After the effort checkpoint: scout skips the debate, troop/barrel enter it.

    Returns ``summary`` only when the effective effort is ``scout-monke`` (C20).
    """
    if state.get("escalation"):
        return "escalate"
    if C._effort_for(state) == "scout-monke":
        return "summary"
    return "debate_tech"


@_safe_router
def route_implement(state):
    if state.get("escalation"):
        return "escalate"
    if state.get("test_fix_attempt", 0) > 0:
        return "implement"          # retry after failing tests
    return "code_review"


@_safe_router
def route_code_review(state):
    if state.get("escalation"):
        return "escalate"
    if state.get("not_met") or state.get("open_blockers", 0):
        return "code_fix"
    return "close_batch"


@_safe_router
def route_code_verify(state):
    if state.get("escalation"):
        return "escalate"
    if state.get("not_met") or state.get("open_blockers", 0):
        return "code_fix"
    return "close_batch"


def _render_passed(state) -> bool:
    return (state.get("render_verdict") in ("PASS", "BASELINE", "SKIPPED")
            and not state.get("render_blockers", 0))


def _after_ui_gate(state):
    """Once the visual gate (if any) is done: the render gate if this task targets
    perf and it hasn't passed yet, else the final check."""
    if state.get("has_perf") and not _render_passed(state):
        return "render_measure"
    return "final_check"


@_safe_router
def route_next_batch(state):
    if state.get("escalation"):
        return "escalate"
    if state["batch_idx"] < len(state.get("batches", [])):
        return "implement"
    # Gates disabled (e.g. scout-monke): skip the visual/render gates straight
    # to the final check. Placed before the has_ui check so a scout-monke UI task
    # does not enter the visual phase (C21).
    if not C.resolved_gates_enabled(state):
        return "final_check"
    # All batches built. A UI task earns the visual gate first; then (or, for a
    # non-UI task, directly) the render gate if this is a perf task; else final.
    # An empty UX_RENDER_CMD disables the whole visual phase (a repo with no
    # frontend, e.g. MonkeForge itself): honour it here so has_ui defaulting True
    # can't drag a backend task into ux_render → visual review → an oscillating
    # fix loop that renders nothing and escalates on phantom blockers.
    if state.get("has_ui") and C.UX_RENDER_CMD.strip():
        return "ux_render"
    return _after_ui_gate(state)


@_safe_router
def route_visual(state):
    """After the vision reviewer: clean → render gate or final; blockers → re-render."""
    if state.get("escalation"):
        return "escalate"
    return "ux_visual_fix" if state.get("visual_blockers", 0) else _after_ui_gate(state)


@_safe_router
def route_render_review(state):
    """After the deterministic render review: regressions → fix and re-profile;
    clean → final gate."""
    if state.get("escalation"):
        return "escalate"
    return "render_fix" if state.get("render_blockers", 0) else "final_check"


# Every node `route_escalation_return` can hand control back to. The escalate
# edge map is built from this set, and test_escalation_returns.py asserts the
# function's actual `return` literals match it — so adding a branch without
# listing it here (the drift that left "summary" unmapped) fails a test instead
# of crashing that one path at runtime. END is handled separately (not a node).
ESCALATION_RETURNS = frozenset({
    "init", "plan", "intake_ask", "intake_wait", "summary",
    "implement", "code_review", "close_batch", "ux_render", "render_measure",
    "final_check", "debate_tech",
    # Self-loop: route_escalation_return is itself a _safe_router, so a crash
    # inside it returns "escalate" and the escalate node needs an edge to itself.
    "escalate",
})


@_safe_router
def route_escalation_return(state):
    """After a human resolves an escalation, resume where it makes sense.

    Every string this returns must be listed in ESCALATION_RETURNS above.
    """
    if state.get("finished"):
        return END
    if not state.get("branch"):
        return "init"
    if not state.get("intake_done") and N.intake_enabled(state):
        tid = state.get("task_id", "")
        # Questions already on disk (agent stdout materialized or human recovery):
        # go to the human gate, do not re-run the interviewer.
        if tid and N.intake_file(tid).exists() and state.get("intake_round", 0) > 0:
            return "intake_wait"
        if state.get("intake_round", 0) < C.MAX_INTAKE_ROUNDS:
            return "intake_ask"
        return "plan"
    if state.get("redo_debate"):
        # Human answered "redo" to a debate escalation: re-enter the debate
        # from round 1, reusing the existing plan.
        return "debate_tech"
    if not state.get("batches"):
        # A debate-cap escalation resolved with "proceed": the plan and the whole
        # debate already exist, so continue to summary/judge — do not re-plan from
        # scratch. debate_round==0 means the debate never ran (e.g. plan failed),
        # so those still restart at plan.
        if state.get("debate_round", 0) > 0:
            return "summary"
        return "plan"
    if state["batch_idx"] < len(state["batches"]):
        # Human force-closed the batch: go to close_batch.
        if state.get("code_verdict") == "APPROVE" and not state.get("open_blockers", 0):
            return "close_batch"
        # If the current batch was already implemented (fix_cycle > 0 or
        # code_verdict exists), go to code_review instead of re-implementing
        # from scratch — otherwise we loop forever on unfixable blockers.
        if state.get("tests_waived"):
            return "code_review"
        if state.get("fix_cycle", 0) > 0 or state.get("code_verdict"):
            return "code_review"
        return "implement"
    # All batches built. We are in (or past) the visual gate. If it hasn't passed
    # and hasn't been explicitly waived, a resolved escalation retries the render
    # — a render-failure escalation resolved with "ok" must re-render, not skip to
    # the final gate (the bug that silently dropped the visual review).
    visual_passed = state.get("visual_verdict") == "APPROVE" and not state.get("visual_blockers", 0)
    if (state.get("has_ui") and not visual_passed
            and not state.get("visual_shipped_blocked")
            and C.UX_RENDER_CMD.strip()           # disabled gate → never re-enter it
            and C.resolved_gates_enabled(state)):  # scout-monke → no gates
        return "ux_render"
    # Perf task whose render gate has not passed: a resolved escalation re-profiles
    # (never silently skip the gate — the same fix the visual gate got).
    if state.get("has_perf") and not _render_passed(state) and C.resolved_gates_enabled(state):
        return "render_measure"
    return "final_check"


# --- build -----------------------------------------------------------------

def build_graph(checkpointer=None):
    g = StateGraph(PipelineState)

    def add(name, fn):
        """Register a node already wrapped in start/end/crash logging."""
        g.add_node(name, N.instrument(name, fn))

    add("init", N.init)
    add("intake_ask", N.intake_ask)
    add("intake_wait", N.intake_wait)
    add("plan", N.plan)
    add("checkpoint_effort", N.checkpoint_effort)  # effort checkpoint (TASK-011)
    add("debate_tech", N.debate_tech)      # technical critic + TECH-LIMIT certification
    add("debate_ux", N.debate_ux)          # the designer — authority on UX
    add("debate_reply", N.debate_reply)
    add("summary", N.summary)
    add("judge", N.judge)
    add("checkpoint_plan", N.checkpoint_plan)
    add("implement", N.implement)
    add("code_review", N.code_review)
    add("code_fix", N.code_fix)
    add("code_verify", N.code_verify)
    add("close_batch", N.close_batch)
    add("ux_render", N.ux_render)           # render the built UI to screenshots
    add("ux_visual_review", N.ux_visual_review)  # vision critic on the screenshots
    add("ux_visual_fix", N.ux_visual_fix)   # implementer fixes visual blockers
    add("render_measure", N.render_measure)  # profile re-renders (perf gate)
    add("render_review", N.render_review)    # deterministic regression check
    add("render_fix", N.render_fix)          # implementer fixes re-render regressions
    add("final_check", N.final_check)
    add("wrap_up", N.wrap_up)
    add("escalate", N.escalate)

    g.add_edge(START, "init")
    g.add_conditional_edges("init", route_after_init,
                            {"intake": "intake_ask", "plan": "plan",
                             "escalate": "escalate"})
    g.add_conditional_edges("intake_ask", route_intake,
                            {"plan": "plan", "wait": "intake_wait",
                             "escalate": "escalate"})
    # The loop: answers go back to the interviewer, which may drill down again.
    g.add_conditional_edges("intake_wait", route_intake_wait,
                            {"plan": "plan", "ask": "intake_ask",
                             "wait": "intake_wait", "escalate": "escalate"})
    g.add_conditional_edges("plan", after(),
                            {"continue": "checkpoint_effort", "escalate": "escalate"})
    # The effort checkpoint routes to summary (scout) or debate_tech (troop/barrel).
    g.add_conditional_edges("checkpoint_effort", route_after_checkpoint_effort,
                            {"summary": "summary", "debate_tech": "debate_tech",
                             "escalate": "escalate"})

    # A round: technical critic → (UX critic, if the task has a surface) →
    # decide. debate_reply loops back to debate_tech, so a round always ends on a
    # critique (its own verification) — no separate verify node.
    g.add_conditional_edges("debate_tech", route_after_tech,
                            {"ux": "debate_ux", "reply": "debate_reply",
                             "summary": "summary", "escalate": "escalate"})
    g.add_conditional_edges("debate_ux", route_debate,
                            {"reply": "debate_reply", "summary": "summary",
                             "escalate": "escalate"})
    g.add_conditional_edges("debate_reply", after(),
                            {"continue": "debate_tech", "escalate": "escalate"})

    g.add_conditional_edges("summary", after(), {"continue": "judge", "escalate": "escalate"})
    g.add_conditional_edges("judge", after(), {"continue": "checkpoint_plan", "escalate": "escalate"})
    g.add_conditional_edges("checkpoint_plan", after(), {"continue": "implement", "escalate": "escalate"})

    g.add_conditional_edges("implement", route_implement,
                            {"implement": "implement", "code_review": "code_review",
                             "escalate": "escalate"})
    g.add_conditional_edges("code_review", route_code_review,
                            {"code_fix": "code_fix", "close_batch": "close_batch",
                             "escalate": "escalate"})
    g.add_conditional_edges("code_fix", after(), {"continue": "code_verify", "escalate": "escalate"})
    g.add_conditional_edges("code_verify", route_code_verify,
                            {"code_fix": "code_fix", "close_batch": "close_batch",
                             "escalate": "escalate"})
    g.add_conditional_edges("close_batch", route_next_batch,
                            {"implement": "implement", "ux_render": "ux_render",
                             "render_measure": "render_measure",
                             "final_check": "final_check", "escalate": "escalate"})
    # The visual loop: render → review → (fix → re-render)* → render gate / final.
    g.add_conditional_edges("ux_render", after(),
                            {"continue": "ux_visual_review", "escalate": "escalate"})
    g.add_conditional_edges("ux_visual_review", route_visual,
                            {"ux_visual_fix": "ux_visual_fix", "render_measure": "render_measure",
                             "final_check": "final_check", "escalate": "escalate"})
    g.add_conditional_edges("ux_visual_fix", after(),
                            {"continue": "ux_render", "escalate": "escalate"})
    # The render loop: measure → review → (fix → re-measure)* → final gate.
    g.add_conditional_edges("render_measure", after(),
                            {"continue": "render_review", "escalate": "escalate"})
    g.add_conditional_edges("render_review", route_render_review,
                            {"render_fix": "render_fix", "final_check": "final_check",
                             "escalate": "escalate"})
    g.add_conditional_edges("render_fix", after(),
                            {"continue": "render_measure", "escalate": "escalate"})
    g.add_conditional_edges("final_check", after(), {"continue": "wrap_up", "escalate": "escalate"})
    g.add_edge("wrap_up", END)

    # Built from ESCALATION_RETURNS so the map cannot drift from the function.
    g.add_conditional_edges("escalate", route_escalation_return,
                            {name: name for name in ESCALATION_RETURNS} | {END: END})

    return g.compile(checkpointer=checkpointer)


def open_checkpointer():
    C.METRICS.mkdir(parents=True, exist_ok=True)
    return SqliteSaver.from_conn_string(str(C.CHECKPOINT_DB))
