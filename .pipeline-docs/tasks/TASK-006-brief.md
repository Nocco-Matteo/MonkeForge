1. Goal
Add a security analyzer that assesses the risk of agent actions before they are executed. Currently agents run with `--permission-mode dangerous` and can execute any command on the system. A security analyzer would: inspect the agent's planned commands, classify them by risk level (safe, moderate, dangerous), and escalate dangerous actions for human approval before execution.

2. Corrections to the request
- None. This is a new subsystem.

3. Rules / domain data
- Risk levels: `safe` (read-only: ls, cat, grep, git status), `moderate` (write within repo: git add, git commit, file edits), `dangerous` (system-level: rm -rf, sudo, apt install, network operations, chmod).
- The analyzer runs as a pre-execution hook in the agent invocation pipeline.
- `safe` actions execute automatically. `moderate` actions execute but are logged. `dangerous` actions trigger an escalation.
- Risk classification rules are configurable via a YAML file: `pipeline_graph/security_rules.yaml`.
- `PIPELINE_SECURITY_MODE` env var: `off` (no analysis, current behavior), `log` (analyze and log but don't block), `enforce` (analyze and block dangerous).
- Default: `off` to preserve backward compatibility.

4. Codebase anchors
- `pipeline_graph/agents.py`: `run_agent()` — add pre-execution hook.
- `pipeline_graph/config.py`: Add security mode config.
- `pipeline_graph/nodes/common.py`: Escalation handling — add security escalation type.
- `pipeline_graph/events.py`: Add security event types.

5. Definition of done
- `SecurityAnalyzer` class in a new `pipeline_graph/security.py`.
- Analyzes agent commands by parsing the prompt and predicted actions.
- Classifies actions into safe/moderate/dangerous.
- In `enforce` mode, dangerous actions trigger an escalation with the command and reason.
- Security rules are configurable via YAML.
- Existing tests pass. New tests cover classification logic and escalation behavior.
- In `off` mode, zero performance overhead (analyzer not instantiated).

6. Scope: in / out
- **IN**: SecurityAnalyzer, security rules YAML, config, integration with run_agent, tests.
- **OUT**: Docker sandbox (separate task), real-time command interception, network-level blocking.

7. Manual acceptance
1. Set `PIPELINE_SECURITY_MODE=enforce` and run a task — verify dangerous commands trigger escalation.
2. Set `PIPELINE_SECURITY_MODE=log` — verify dangerous commands are logged but executed.
3. Set `PIPELINE_SECURITY_MODE=off` — verify behavior is identical to before.
4. Add a custom rule to the YAML — verify it takes effect.

8. Unverified assumptions
- We assume agent commands can be predicted by parsing the prompt (agents may execute commands not mentioned in the prompt).
- We assume a YAML rules file is sufficient for classification (no need for ML-based detection).
