1. Goal
Add a zero-code workflow definition system so new pipeline workflows can be defined in YAML without editing Python code. Currently adding a new workflow or changing the pipeline structure requires editing graph.py (node definitions, edges, conditional routing). A YAML-based system would allow defining workflows as a sequence of steps with conditions, making it accessible to non-programmers.

2. Corrections to the request
- None. This is a major architectural addition, inspired by ChatDev 2.0's zero-code approach.

3. Rules / domain data
- Workflow definition file: `pipeline_graph/workflows/default.yaml` (or env: `PIPELINE_WORKFLOW`).
- Each step has: name, node_function, next (conditional routing), condition (Python expression evaluated on state).
- The graph builder reads the YAML and constructs the LangGraph StateGraph dynamically.
- The current hardcoded graph in `build_graph()` becomes the `default.yaml` workflow.
- Custom workflows can be added as additional YAML files.
- `./run.py graph --workflow custom.yaml` prints the graph for a custom workflow.
- Must produce the same graph as before with the default workflow.

4. Codebase anchors
- `pipeline_graph/graph.py`: `build_graph()` — refactor to read YAML and construct graph dynamically.
- `pipeline_graph/config.py`: Add workflow config loading.
- `pipeline_graph/nodes/__init__.py`: Node registry — map node names to functions.
- `pipeline_graph/nodes/*.py`: Node functions — no change to logic, only to how they're referenced.

5. Definition of done
- `workflows/default.yaml` defines the entire current pipeline as a sequence of steps with conditions.
- `build_graph()` reads the YAML and constructs the graph dynamically.
- `./run.py graph` output is identical to the current hardcoded graph.
- A custom workflow YAML can be loaded and produces a valid graph.
- Conditional routing (debate convergence, escalation routing, batch looping) is expressed in YAML.
- Existing tests pass. New tests verify graph construction from YAML.

6. Scope: in / out
- **IN**: Workflow YAML schema, default.yaml, refactor of build_graph(), node registry, tests.
- **OUT**: Visual workflow editor, workflow marketplace, runtime workflow switching.

7. Manual acceptance
1. Run `./run.py graph` — verify output is identical to current graph.
2. Create a minimal workflow YAML with 3 steps — verify `./run.py graph --workflow minimal.yaml` shows the correct graph.
3. Run a dry-run task with the default workflow — verify behavior is identical to before.
4. Modify a condition in the YAML — verify the routing changes accordingly.

8. Unverified assumptions
- We assume all conditional routing in the current graph can be expressed as Python expressions on state (no complex logic that can't be serialized).
- We assume LangGraph's StateGraph API supports dynamic construction from a config file without limitations.
