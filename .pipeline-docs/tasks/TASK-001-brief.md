Ripgrep is not available. Falling back to GrepTool.
Error executing tool read_file: Path not in workspace: Attempted path "/home/nocco/Documenti/progetti/MonkeForge/docs/MonkeForge-clone/tasks/TASK-001-intake.md" resolves outside the allowed workspace directories: /home/nocco/Documenti/progetti/MonkeForge-clone or the project temp directory: /home/nocco/.gemini/tmp/monkeforge-clone
UI-SURFACE: no

# Task Brief: TASK-001 - Metrics Aggregation and Summary Report Generator

---

### 1. Goal
Provide structured performance analytics, bottleneck detection, and cross-task tracking by parsing the existing event stream (`events.jsonl`) to generate per-task Markdown reports and a cross-task summary report via a new CLI subcommand `./run.py metrics`.

---

### 2. Corrections to the Request
1. **Cost Per Task Exclusion**: The seed request mentions "cost per task" as a current gap. However, the event logs do not track token usage, prompt/completion lengths, or LLM pricing models. Therefore, computing cost is **corrected to be out of scope**.
   * *Evidence*: `pipeline_graph/events.py` does not log any token counts or pricing data in the event stream schema, nor does `pipeline_graph/agents.py` capture this information during agent invocation.
2. **Events Path Source of Truth**: The seed request hardcodes the metrics path as `docs/<repo>/metrics/events.jsonl`. This path is actually dynamically configured in `pipeline_graph/config.py` as `C.METRICS` (defaulting to `docs/MonkeForge-clone/metrics`), and in `pipeline_graph/events.py` as `EVENTS_LOG = C.METRICS / "events.jsonl"`. The parser must import and respect these configurations and any `PIPELINE_DOCS_DIR` environment overrides instead of hardcoding paths.
   * *Evidence*: Configured in `pipeline_graph/config.py` and referenced as `EVENTS_LOG` in `pipeline_graph/events.py`.
3. **Missing/Non-existent Architecture Documentation**: The seed request refers to `backend/ARCHITECTURE.md`. This file does not exist in the workspace, and there is no `backend` folder in the codebase. The primary documentation governing the pipeline structure and commands is the root `README.md`.
   * *Evidence*: `README.md` is verified to be the only architecture/operational documentation in the repository.

---

### 3. Rules / Domain Data

The metrics parser must read `events.jsonl` and map specific event types (`kind`) to calculate performance indicators. Below is the domain mapping for the event stream schema:

| Event Type (`kind`) | Key Fields for Analysis | Calculation / Aggregation Rule |
| :--- | :--- | :--- |
| `run_start` | `ts`, `task`, `msg` | Marks a task execution starting, resuming, or redoing. Used to establish the first event timestamp. |
| `run_paused` | `ts` | Marks the task pausing for human intervention. |
| `run_stalled` | `ts` | Marks the task stalling unexpectedly. |
| `run_end` | `ts` | Marks task completion. |
| `step_end` | `ms` (duration), `outcome` | Tracks graph node execution time. `ms` is mapped to node duration. `outcome` flags degradations/failures. |
| `step_error` | `step`, `msg` | Marks node exception or process crash. Used to count step-level crashes. |
| `agent_end` | `duration_ms` (duration), `health` | Tracks agent execution time and outcomes. `duration_ms` is mapped to agent runtimes. |
| `agent_unhealthy` | `health`, `msg` | Used to count LLM agent retries/failures. |
| `escalation_open` | `task` | Counted to track total escalation count. |
| `degraded` | `task` | Counted to track total degradation count. |

#### Metric Computation Formulae:
- **Total Duration of the Task**: 
  - *Elapsed Wall-Clock Time*: Difference between the last event's timestamp (`ts`) and the first event's timestamp (`ts`) for that task ID.
  - *Active Running Time*: Sum of `ms` fields from all `step_end` events of that task ID (excludes human pause/idle times).
- **Per-Node Duration**: Sum of `ms` fields from `step_end` events for each unique node in that task ID.
- **Per-Agent Duration**: Sum of `duration_ms` from `agent_end` events for each unique agent/role (e.g. role + agent binary) in that task ID.
- **Completion Status**:
  Parsed chronologically (oldest to newest):
  - `FINISHED`: If a `run_end` event exists for that task ID.
  - `PAUSED`: If the latest run status event for that task ID is `run_paused` or `escalation_open`.
  - `STALLED`: If the latest run status event for that task ID is `run_stalled`.
  - `RUNNING`: If a `run_start`/`resume` exists for that task ID without a subsequent pause, stall, or end event.

---

### 4. Codebase Anchors
Code is the ground truth for signatures, while this brief is the ground truth for intent:
- `pipeline_graph/events.py`: Contains `EVENTS_LOG` path and the `read_events` function. Read-only event emission and log parsing logic.
- `pipeline_graph/config.py`: Contains `METRICS` directory configuration and preflight rules.
- `run.py`: The main command line interface parsing arguments and running command handlers. A new `metrics` subcommand must be registered here.
- `tests/test_metrics.py`: The destination file for the new metrics test suite.

---

### 4b. Architecture Docs to Follow
The implementer must read:
- `README.md` (governing core pipeline operations and offline tool structure).

---

### 5. Definition of Done
- `./run.py metrics <task-id>` generates `report-<task-id>.md` in `C.METRICS` and outputs it to stdout with: Total Duration (Wall-clock and Active), Per-Node Durations, Per-Agent Durations, Failure/Retry count, Escalations, and Degradations.
- `./run.py metrics --all` generates `summary.md` in `C.METRICS` and outputs it to stdout with: Task List, Completion Status per task, Average Duration per graph node, Most Common Failure Reasons, and Agents ranked by total runtime.
- Generated reports are written in standard, clean Markdown containing structured tables.
- All existing tests pass without regressions.
- A new test suite `tests/test_metrics.py` is added to cover event parsing, duration calculations, status resolution, and report file creation under various event conditions (using mocked/simulated `events.jsonl` inputs).
- The metrics aggregation process handles empty/missing `events.jsonl` gracefully by outputting a clear message ("no metrics recorded yet") and exiting with status 0.

---

### 6. Scope: In / Out
- **IN**:
  - Event log parsing and metrics aggregator logic.
  - Markdown report generation and output to the standard directory dynamically resolved from `C.METRICS`.
  - CLI parser subcommand `metrics` implementation.
  - Exhaustive unit/integration tests for calculations and CLI routing under `tests/test_metrics.py`.
- **OUT**:
  - Web dashboards or graphical plotting (HTML, JS, CSS, PNG).
  - Database schema modifications or database storage for metrics.
  - Active daemon or background process for real-time monitoring.
  - Cost tracking involving LLM tokens and pricing lookup tables.

---

### 7. Manual Acceptance
1. Execute `./run.py metrics --all` and verify a complete list of past tasks is printed with correct status (FINISHED, PAUSED, STALLED, RUNNING).
2. Execute `./run.py metrics <task-id>` for a finished task and compare the active durations and failure/retry counts with the live journal (`journal-<task-id>.log`) to ensure absolute parity.
3. Validate that both generated markdown files reside in the configured `C.METRICS` folder.
4. Simulate or trigger an empty or missing `events.jsonl` file and verify that `./run.py metrics --all` outputs "no metrics recorded yet" cleanly and returns exit code 0.

---

### 8. Unverified Assumptions
- We assume that `events.jsonl` is the sole source of truth for the event history. Any legacy logs or formats do not need to be supported.
- We assume that `ts` timestamps in `events.jsonl` are in standard ISO 8601 string format (e.g., `2026-07-30T10:14:10.123456Z` or similar) and can be parsed using `datetime.fromisoformat()`.
- Since `CLAUDE.md` and `backend/ARCHITECTURE.md` do not exist in the workspace, we assume `README.md` is the primary and comprehensive resource for architectural details and implementation guidelines.
