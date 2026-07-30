1. Goal
Add a plugin system for agents so new agent backends can be added without modifying config.py or agents.py. Currently adding a new agent type requires: editing ROLES in config.py, adding env vars, editing the command template, and potentially modifying agents.py. A plugin system would allow dropping a `.py` file in `pipeline_graph/plugins/` that registers a new agent backend.

2. Corrections to the request
- None. This is a new subsystem.

3. Rules / domain data
- Plugins live in `pipeline_graph/plugins/` (one file per plugin).
- Each plugin exports a `register()` function that returns agent metadata: name, default model, command template, env var prefix.
- Plugins are auto-discovered at import time (no manual registration).
- The existing ROLES dict in config.py becomes the default, plugins extend it.
- `PIPELINE_PLUGIN_DIR` env var can override the plugin directory (default: `pipeline_graph/plugins/`).
- Must not break existing role configuration — env vars still override plugin defaults.

4. Codebase anchors
- `pipeline_graph/config.py`: ROLES dict, model/command env var resolution. Add plugin loading.
- `pipeline_graph/agents.py`: `run_agent()` uses ROLES to resolve the command. No change needed if ROLES is populated by plugins.
- `pipeline_graph/nodes/*.py`: Nodes call `run_agent("ROLE_NAME", ...)` — agnostic to how the role was registered.

5. Definition of done
- `pipeline_graph/plugins/` directory exists with at least one example plugin (e.g. `claude_code.py` registering the CLAUDE agent backend).
- `config.py` auto-discovers and loads plugins at import time.
- A new agent backend can be added by dropping a single file in `plugins/` — no edits to config.py or agents.py.
- Existing role configuration (env vars, ROLES dict) continues to work unchanged.
- Existing tests pass. New tests verify plugin discovery and registration.

6. Scope: in / out
- **IN**: Plugin directory, plugin loader in config.py, one example plugin, tests.
- **OUT**: Changing the agent invocation protocol, changing prompt rendering, web UI for plugins.

7. Manual acceptance
1. Drop a new plugin file in `pipeline_graph/plugins/` — verify it appears in ROLES without restarting anything other than the Python process.
2. Set the env var for the new plugin's model — verify it overrides the plugin default.
3. Run a dry-run task using the new plugin's role — verify the correct command is invoked.

8. Unverified assumptions
- We assume Python's importlib can reliably discover and load plugins from a directory without complex dependency management.
- We assume plugins only need to provide metadata (name, model, command) — not custom invocation logic.
