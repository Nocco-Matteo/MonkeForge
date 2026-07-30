"""Metrics aggregation and rendering for `./run.py metrics`.

Pure functions over an in-memory event list — no file I/O here. `run.py`'s
`_metrics` helper is responsible for reading `events.jsonl` (via
`events.read_events` / `events.read_all_events`) and for writing the rendered
report to disk; this module only turns a chronological list of event dicts into
a `TaskMetrics` and then into human-readable text.

The retry count follows the convention established in
`nodes/debate.py`/`nodes/quality_gates.py`: a retried step is re-emitted with a
`-retry{N}` suffix on the `step` field of `agent_end` (and `step_error` on a
crash during a retry). The generic transient-retry loop in `run_agent`
(`agent_unhealthy`, no step suffix) is intentionally NOT counted here — that is
a separate, plan-stated scope limit.
"""
from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class TaskMetrics:
    task_id: str
    wall_clock_s: float = 0.0
    active_s: float = 0.0
    per_node_ms: dict[str, int] = field(default_factory=dict)
    per_agent_ms: dict[str, int] = field(default_factory=dict)
    failures: int = 0
    retries: int = 0
    escalations: int = 0
    degradations: int = 0
    status: str = "UNKNOWN"
    failure_reasons: list[str] = field(default_factory=list)


def _parse_ts(rec: dict) -> float:
    """Parse the isoformat `ts` field of an event record to a unix timestamp.

    Returns 0.0 for missing or unparseable values so a malformed line never
    breaks aggregation — it just sorts to the front, which is harmless for the
    "latest of" status resolution.
    """
    ts = rec.get("ts")
    if not ts:
        return 0.0
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (ValueError, TypeError):
        return 0.0


def resolve_status(events: list[dict]) -> str:
    """Resolve the run status from the chronological event sequence.

    The latest (by timestamp) of:
      run_end        -> FINISHED
      run_paused     -> PAUSED
      escalation_open-> PAUSED
      run_stalled    -> STALLED
      run_start      -> RUNNING
    Returns "UNKNOWN" if none of these kinds appear.
    """
    order = {
        "run_start": "RUNNING",
        "run_stalled": "STALLED",
        "escalation_open": "PAUSED",
        "run_paused": "PAUSED",
        "run_end": "FINISHED",
    }
    latest = "UNKNOWN"
    latest_ts = -1.0
    for e in events:
        kind = e.get("kind")
        if kind not in order:
            continue
        t = _parse_ts(e)
        if t >= latest_ts:
            latest_ts = t
            latest = order[kind]
    return latest


def aggregate_task(events: list[dict], task_id: str = "") -> TaskMetrics:
    """Aggregate a chronological event list for one task into a `TaskMetrics`.

    `events` is expected to already be filtered to a single task (e.g. by
    `events.read_events`); `task_id` is taken from the argument or, failing
    that, from the first record's `task` field.
    """
    tid = task_id or (events[0].get("task") if events else "?")
    m = TaskMetrics(task_id=tid)
    if not events:
        return m

    starts = [e for e in events if e.get("kind") == "run_start"]
    ends = [e for e in events if e.get("kind") == "run_end"]
    t_start = _parse_ts(starts[0]) if starts else _parse_ts(events[0])
    t_end = _parse_ts(ends[-1]) if ends else _parse_ts(events[-1])
    m.wall_clock_s = max(0.0, t_end - t_start)

    per_node: dict[str, int] = defaultdict(int)
    per_agent: dict[str, int] = defaultdict(int)
    for e in events:
        kind = e.get("kind")
        step = e.get("step", "") or ""
        if kind == "step_end":
            ms = int(e.get("ms") or 0)
            per_node[step] += ms
            m.active_s += ms / 1000.0
        elif kind == "agent_end":
            agent = e.get("agent") or e.get("role") or step
            per_agent[agent] += int(e.get("duration_ms") or 0)

        # Retries: only the UX/visual-review -retry{N} convention, carried on
        # agent_end (the normal retry path) and step_error (a crash during a
        # retry). step_end never carries the suffix — see FINAL-001 ruling 1.
        if kind in ("agent_end", "step_error") and "-retry" in step:
            m.retries += 1

        if kind in ("step_error", "agent_unhealthy"):
            m.failures += 1
            reason = e.get("msg") or ""
            if reason:
                m.failure_reasons.append(reason)
        if kind == "escalation_open":
            m.escalations += 1
        if kind == "degraded":
            m.degradations += 1

    m.per_node_ms = dict(per_node)
    m.per_agent_ms = dict(per_agent)
    m.status = resolve_status(events)
    return m


def group_by_task(events: list[dict]) -> dict[str, list[dict]]:
    """Group all events by `task`, preserving chronological order within each."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        tid = e.get("task") or "?"
        groups[tid].append(e)
    return dict(groups)


def render_task_report(m: TaskMetrics) -> str:
    """Render a single-task report with the seven required section headers."""
    lines = [
        f"# Task {m.task_id} — metrics report",
        "",
        "## Total Duration",
        f"wall clock: {m.wall_clock_s:.1f}s   "
        f"active (sum of step durations): {m.active_s:.1f}s   "
        f"status: {m.status}",
        "",
        "## Per-Node Durations",
    ]
    if m.per_node_ms:
        for node, ms in sorted(m.per_node_ms.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {node:<20} {ms / 1000:.1f}s")
    else:
        lines.append("  (none)")
    lines += ["", "## Per-Agent Durations"]
    if m.per_agent_ms:
        for agent, ms in sorted(m.per_agent_ms.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {agent:<20} {ms / 1000:.1f}s")
    else:
        lines.append("  (none)")
    lines += [
        "",
        "## Failure count",
        f"  {m.failures}",
        "",
        "## Retry count",
        f"  {m.retries}",
        "",
        "## Escalations",
        f"  {m.escalations}",
        "",
        "## Degradations",
        f"  {m.degradations}",
    ]
    if m.failure_reasons:
        lines += ["", "### Failure reasons"]
        for r in m.failure_reasons:
            lines.append(f"  - {r}")
    return "\n".join(lines) + "\n"


def render_summary(metrics_by_task: dict[str, TaskMetrics]) -> str:
    """Render the cross-task summary with the five required section headers."""
    lines = ["# Pipeline metrics — summary", "", "## Task List"]
    if metrics_by_task:
        for tid in sorted(metrics_by_task):
            m = metrics_by_task[tid]
            lines.append(
                f"  {tid:<10} status={m.status:<10} wall={m.wall_clock_s:.1f}s "
                f"failures={m.failures} retries={m.retries}"
            )
    else:
        lines.append("  (none)")

    lines += ["", "## Completion Status"]
    counts: dict[str, int] = defaultdict(int)
    for m in metrics_by_task.values():
        counts[m.status] += 1
    if counts:
        for status in sorted(counts):
            lines.append(f"  {status:<10} {counts[status]}")
    else:
        lines.append("  (none)")

    lines += ["", "## Average Duration per graph node"]
    node_samples: dict[str, list[int]] = defaultdict(list)
    for m in metrics_by_task.values():
        for node, ms in m.per_node_ms.items():
            node_samples[node].append(ms)
    if node_samples:
        for node in sorted(node_samples, key=lambda n: -sum(node_samples[n])):
            vals = node_samples[node]
            avg = sum(vals) / len(vals)
            lines.append(
                f"  {node:<20} avg {avg / 1000:.1f}s over {len(vals)} task(s)"
            )
    else:
        lines.append("  (none)")

    lines += ["", "## Most Common Failure Reasons"]
    reason_counts: dict[str, int] = defaultdict(int)
    for m in metrics_by_task.values():
        for r in m.failure_reasons:
            reason_counts[r] += 1
    if reason_counts:
        for r, c in sorted(reason_counts.items(), key=lambda kv: -kv[1])[:10]:
            lines.append(f"  {c:>3}  {r}")
    else:
        lines.append("  (none)")

    lines += ["", "## Agents ranked by total runtime"]
    agent_totals: dict[str, int] = defaultdict(int)
    for m in metrics_by_task.values():
        for agent, ms in m.per_agent_ms.items():
            agent_totals[agent] += ms
    if agent_totals:
        for agent, ms in sorted(agent_totals.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {agent:<20} {ms / 1000:.1f}s")
    else:
        lines.append("  (none)")
    return "\n".join(lines) + "\n"
