1. Goal
Refactor agent invocations to use an immutable Conversation object instead of the mutable PipelineState dict. Currently, nodes receive `state` (a TypedDict), mutate it, and return a delta dict that LangGraph merges. Agents receive rendered prompt strings with no structured conversation history. An immutable Conversation object would: give agents structured access to prior turns, enable the condenser (TASK-003) to work on conversation history, and make the state transition explicit and debuggable.

2. Corrections to the request
- None. This is an architectural refactor.

3. Rules / domain data
- `Conversation` is an immutable dataclass (or Pydantic model) containing: task_id, request, brief, plan, debate_history, batch_context, review_history, journal.
- Nodes receive `PipelineState` (unchanged for LangGraph compatibility) but construct a `Conversation` snapshot to pass to agents.
- Agents receive a `Conversation` instead of a raw prompt string — they render their own prompt from it using their template.
- The `Conversation` is read-only for agents — they return output, the node updates state.
- Must not change the LangGraph state schema (PipelineState TypedDict stays).

4. Codebase anchors
- `pipeline_graph/state.py`: PipelineState TypedDict — unchanged, but add Conversation class.
- `pipeline_graph/agents.py`: `run_agent()` currently takes (role, task_id, step, prompt). Refactor to accept Conversation.
- `pipeline_graph/nodes/*.py`: Each node builds a Conversation from state before calling run_agent.
- `pipeline_graph/prompts/*.md`: Templates may need to accept Conversation fields instead of flat kwargs.

5. Definition of done
- `Conversation` class defined in `state.py` (or a new `conversation.py`).
- `run_agent()` accepts a `Conversation` and renders the prompt internally.
- All nodes construct a Conversation from state before calling run_agent.
- Existing tests pass (prompt contracts test may need updating to match new rendering).
- New tests verify Conversation immutability and correct field mapping.

6. Scope: in / out
- **IN**: Conversation class, refactor of run_agent, refactor of all node call sites, tests.
- **OUT**: Changing PipelineState schema, changing LangGraph graph structure, multi-turn agent conversations.

7. Manual acceptance
1. Run a dry-run task — verify the same prompt text is produced (diff against pre-refactor).
2. Run a real task — verify agent output is identical in quality.
3. Verify Conversation objects are logged in events for debugging.

8. Unverified assumptions
- We assume the current prompt templates can be rendered from a structured Conversation without losing information.
- We assume the overhead of constructing Conversation objects per node is negligible.
