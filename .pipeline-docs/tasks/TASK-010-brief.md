1. Goal
Add a web UI / dashboard for MonkeForge, inspired by OpenHands' Agent Canvas. Currently monitoring is via CLI (`./run.py status`), file logs (`tail -f`), and Discord bot. A web dashboard would provide: real-time pipeline status, task history, agent metrics visualization, escalation management with approve/reject buttons, and log streaming.

2. Corrections to the request
- None. This is a new frontend application.

3. Rules / domain data
- Web server: FastAPI (Python, no separate Node.js process needed).
- Frontend: React + TailwindCSS, served by FastAPI as static files.
- Real-time updates: WebSocket for live event streaming from events.jsonl.
- Data source: events.jsonl, runs.jsonl, graph-checkpoints.sqlite, current.json.
- Escalation management: WebSocket + REST endpoint to resume/answer escalations (wraps `run.py resume`).
- Authentication: simple token via env `PIPELINE_UI_TOKEN` (no OAuth for now).
- Must not require a database — all data from existing file-based sources.
- `./run.py ui` starts the server (default port 8765, configurable via `PIPELINE_UI_PORT`).

4. Codebase anchors
- `run.py`: Add `ui` subcommand that starts the FastAPI server.
- `pipeline_graph/events.py`: WebSocket integration — emit events to connected clients.
- `pipeline_graph/config.py`: UI config (port, token, enabled).
- New directory: `pipeline_graph/ui/` — FastAPI app, static files, WebSocket handler.
- New directory: `pipeline_graph/ui/frontend/` — React app (built to static files).

5. Definition of done
- `./run.py ui` starts a web server on port 8765.
- Dashboard shows: current task status (node, batch, fix cycle), task history list, agent metrics (duration, failures).
- WebSocket streams events in real-time (step_start, step_end, agent_start, agent_end, escalation).
- Escalation cards have approve/reject buttons that call `run.py resume` via REST.
- Log viewer shows pipeline.log and per-task journal with auto-scroll.
- Authentication via token (query param or header).
- Existing tests pass. New tests cover the REST API endpoints.

6. Scope: in / out
- **IN**: FastAPI server, React frontend, WebSocket streaming, escalation management, log viewer, tests.
- **OUT**: Mobile app, multi-user collaboration, workflow editor (covered by TASK-009), agent configuration UI.

7. Manual acceptance
1. Run `./run.py ui` and open `http://localhost:8765` — verify dashboard loads.
2. Start a task via CLI — verify the dashboard updates in real-time.
3. Trigger an escalation — verify the escalation card appears with approve/reject buttons.
4. Click approve — verify the pipeline resumes.
5. View the log tab — verify pipeline.log streams in real-time.
6. Check the metrics tab — verify agent durations and failure counts match events.jsonl.

8. Unverified assumptions
- We assume FastAPI + WebSocket is sufficient for real-time updates without a separate message broker.
- We assume the React frontend can be built and served as static files without a Node.js runtime in production.
- We assume token-based authentication is sufficient for a local/self-hosted deployment.
