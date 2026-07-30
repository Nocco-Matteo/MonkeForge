1. Goal
Make quality gates configurable instead of hardcoded in the graph. Currently the visual gate (ux_render, ux_visual_review, ux_visual_fix) and render gate (render_measure, render_review, render_fix) are always present in the graph if the relevant env vars are set. A configurable system would allow enabling/disabling gates and defining custom gates via a YAML config file.

2. Corrections to the request
- None. This is a refactor of the graph structure.

3. Rules / domain data
- Gate config file: `pipeline_graph/gates.yaml` (or env: `PIPELINE_GATES_CONFIG`).
- Each gate has: name, enabled (bool), nodes (list of node names), condition (when to run), max_cycles, plateau_threshold.
- Built-in gates: `visual`, `render`, `test`, `code_review`. All enabled by default.
- Custom gates can reference new node functions defined in `pipeline_graph/nodes/`.
- The graph builder in `graph.py` reads the config and wires gates accordingly.
- Must produce the same graph as before when using default config.

4. Codebase anchors
- `pipeline_graph/graph.py`: `build_graph()` — currently hardcodes all nodes and edges. Refactor to read gate config.
- `pipeline_graph/config.py`: Add gate config loading.
- `pipeline_graph/nodes/quality_gates.py`: Visual and render gate nodes — no change to node logic, only to how they're wired.
- `pipeline_graph/nodes/implement.py`: Test gate — same.
- `pipeline_graph/nodes/review.py`: Code review gate — same.

5. Definition of done
- `gates.yaml` defines all built-in gates with their default config.
- `build_graph()` reads the config and constructs the graph dynamically.
- Disabling a gate in YAML removes it from the graph (no dead nodes).
- Custom gates can be added by defining node functions + adding a gate entry in YAML.
- `./run.py graph` output reflects the configured gates.
- Existing tests pass. New tests verify graph construction from config.

6. Scope: in / out
- **IN**: gates.yaml, refactor of build_graph(), config loading, tests.
- **OUT**: Changing node logic, changing state schema, web UI for gate config.

7. Manual acceptance
1. Run `./run.py graph` with default config — verify output is identical to current graph.
2. Disable the `visual` gate in YAML — verify `./run.py graph` no longer shows visual nodes.
3. Run a dry-run task with a gate disabled — verify the pipeline skips that gate.
4. Add a custom gate — verify it appears in the graph.

8. Unverified assumptions
- We assume all gates can be expressed as a linear sequence of nodes with a cycle condition (no complex branching within a gate).
- We assume the graph builder can handle dynamic node wiring without hitting LangGraph limitations.
