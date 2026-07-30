1. Goal
Add parallel agent execution where independent agents can run concurrently. Currently the debate phase runs two critics (PLAN_REVIEWER and UX_REVIEWER) but they are sequential in the graph. Code review and implementation of different batches could also be parallelized. Parallel execution would reduce wall-clock time for tasks with independent work.

2. Corrections to the request
- None. This is a performance optimization.

3. Rules / domain data
- LangGraph supports parallel branches (fan-out / fan-in).
- The debate phase (tech critic + UX critic) should run as two parallel branches that merge before the decision node.
- Batch implementation could parallelize when batches are independent (no shared files), but this requires the judge to explicitly mark batches as parallelizable.
- Parallel agents must not write to the same files (git conflicts).
- Max concurrency configurable: `PIPELINE_MAX_PARALLEL_AGENTS=2` (default: 2).
- Must preserve all quality gates — parallel branches merge before any gate.

4. Codebase anchors
- `pipeline_graph/graph.py`: `build_graph()` — rewire debate nodes as parallel branches.
- `pipeline_graph/nodes/debate.py`: `debate_tech()` and `debate_ux()` — no change to logic, only to graph wiring.
- `pipeline_graph/state.py`: May need fields to track parallel branch completion.
- `pipeline_graph/nodes/finalize.py`: `judge()` — may need to handle parallel batch results.

5. Definition of done
- Debate phase runs PLAN_REVIEWER and UX_REVIEWER in parallel branches.
- The decision node waits for both branches to complete before proceeding.
- No file conflicts between parallel agents (verified by git status after merge).
- Max concurrency is enforced (no more than N agents running simultaneously).
- Existing tests pass. New tests verify parallel execution and merge behavior.
- Wall-clock time for debate phase is reduced (verifiable with metrics from TASK-001).

6. Scope: in / out
- **IN**: Parallel debate branches, concurrency config, state tracking for parallel completion, tests.
- **OUT**: Parallel batch implementation (future task), parallel code review, distributed execution.

7. Manual acceptance
1. Run a task with debate — verify both critics run in parallel (check timestamps in events.jsonl).
2. Verify the decision node receives both verdicts correctly.
3. Verify no git conflicts after the debate phase.
4. Set `PIPELINE_MAX_PARALLEL_AGENTS=1` — verify debate runs sequentially (backward compatible).

8. Unverified assumptions
- We assume LangGraph's parallel branch support is sufficient for this use case (fan-out with merge).
- We assume the two debate critics never need to write to the same file (they write to different review files).
