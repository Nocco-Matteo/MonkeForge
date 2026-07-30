1. Goal
Add a context management / condenser system to MonkeForge. Currently, the full prompt for each agent is built from templates with all context injected at once. For long tasks with many batches, fix cycles, and debate rounds, prompts can grow unbounded. A condenser would: truncate or summarize older context, keep recent context verbatim, and enforce a configurable token budget per agent invocation.

2. Corrections to the request
- None. This is a new subsystem.

3. Rules / domain data
- Token budget is configurable per role via env: `PIPELINE_TOKEN_BUDGET_<ROLE>` (default: no limit, preserving current behavior).
- Condenser runs after prompt rendering but before passing to the agent CLI.
- Must preserve the most recent N items verbatim (configurable: `PIPELINE_CONDENSER_KEEP_RECENT=3`).
- Older items are summarized using a lightweight LLM call or truncated with a marker.
- The condenser must be a no-op when the prompt is under budget.
- Must log when condensation occurs (as a degradation event).

4. Codebase anchors
- `pipeline_graph/agents.py`: `render_prompt()` and `run_agent()` — the condenser hooks in here.
- `pipeline_graph/config.py`: Add token budget config per role.
- `pipeline_graph/prompts/*.md`: Prompt templates — no changes needed, condenser operates on rendered output.
- `pipeline_graph/nodes/common.py`: `_context()` function builds context strings.

5. Definition of done
- A `Condenser` class in a new `pipeline_graph/condenser.py` that takes a rendered prompt + token budget and returns a condensed prompt.
- `run_agent()` optionally applies the condenser before invoking the agent CLI.
- When condensation occurs, a `degraded` event is emitted with the original and condensed sizes.
- Token counting uses `tiktoken` (or a simple heuristic if tiktoken is unavailable).
- Configurable via env vars, defaults to no-op (backward compatible).
- Existing tests pass. New tests cover condensation logic.

6. Scope: in / out
- **IN**: Condenser module, token counting, integration with run_agent, config, tests.
- **OUT**: Changing prompt templates, changing agent CLIs, multi-turn conversation management.

7. Manual acceptance
1. Set `PIPELINE_TOKEN_BUDGET_IMPLEMENTER=4000` and run a task with large batches — verify the implementer prompt is condensed.
2. Verify the condensed prompt still produces valid agent output.
3. Verify a `degraded` event is logged with size before/after.
4. Run without token budget — verify behavior is identical to before.

8. Unverified assumptions
- We assume tiktoken is available or a heuristic (chars/4) is sufficient for token estimation.
- We assume the condenser can use a lightweight LLM call (e.g. gemini-flash) for summarization without significant cost or latency.
