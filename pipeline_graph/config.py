"""Configuration: roles -> agent CLIs, paths, timeouts. Single source of truth."""
from __future__ import annotations
import json, os, shlex, shutil, subprocess, sys
from pathlib import Path

REPO = Path(
    os.environ.get("PIPELINE_REPO")
    or subprocess.run(["git", "rev-parse", "--show-toplevel"],
                      capture_output=True, text=True).stdout.strip()
    or "."
).resolve()

# MonkeForge root: pipeline_graph/config.py -> pipeline_graph/ -> MonkeForge/
MF_ROOT = Path(__file__).resolve().parents[1]


# Per-repo docs: MonkeForge/docs/<repo-name>/...  Override with PIPELINE_DOCS_DIR.
_repo_slug = REPO.name
DOCS      = Path(os.environ.get("PIPELINE_DOCS_DIR") or (MF_ROOT / "docs" / _repo_slug))
PLANS     = DOCS / "plans"
DEBATES   = DOCS / "debates"
FINAL     = DOCS / "final"
REVIEWS   = DOCS / "reviews"
PROMPTS   = DOCS / "prompts"
QUEUE     = DOCS / "queue" / "pending"
TASKS     = DOCS / "tasks"
METRICS   = DOCS / "metrics"
RAW       = METRICS / "raw"
RUNS_LOG  = METRICS / "runs.jsonl"
CHECKPOINT_DB = METRICS / "graph-checkpoints.sqlite"

# {docs_dir} injected into every agent prompt. Two modes, and NOTHING is copied
# into the repo in either one — the working tree only ever holds real task code:
#   - docs INSIDE the repo (PIPELINE_DOCS_DIR=REPO/docs): repo-relative path;
#     they're tracked and INIT_DIRTY_OK_PREFIXES keeps them from blocking init.
#   - docs OUTSIDE the repo (default): ABSOLUTE path. Agents reach it through each
#     CLI's external-dir flag (claude --add-dir {docs_dir}, gemini
#     --include-directories {docs_dir}, devin reads absolute paths natively).
#     No .pipeline-docs mirror, no sync-back.
if DOCS.is_relative_to(REPO):
    DOCS_REL = str(DOCS.relative_to(REPO))
else:
    DOCS_REL = str(DOCS)
DOCS_ORIG = None  # kept for API stability; no in-repo mirror to sync back anymore

# Dirty paths that do not block `init` in interactive mode (pipeline/task docs only).
# When docs live inside the repo (PIPELINE_DOCS_DIR=REPO/docs), pipeline artifacts
# must not block init. When docs live outside the repo (the default), this is empty
# and nothing extra is ignored — the repo's own docs/ (if any) is not ours.
_docs_rel = str(DOCS.relative_to(REPO)) + "/" if DOCS.is_relative_to(REPO) else ""
INIT_DIRTY_OK_PREFIXES = tuple(
    _docs_rel + sub for sub in ("tasks/", "metrics/", "prompts/", "queue/")
) if _docs_rel else ()

TEMPLATES = Path(__file__).parent / "prompts"

# Architecture docs the agents must read (repo-relative, ";"-separated).
# Configured per-repo in monkeforge.yaml (pipeline.arch_docs) or PIPELINE_ARCH_DOCS.
# No hardcoded defaults — MonkeForge is standalone and doesn't assume any repo.
def arch_docs_block() -> str:
    """The architecture docs that actually exist, as a bullet list for prompts.

    Filters to existing files so a doc listed-but-not-yet-created is skipped and
    appears automatically once you add it."""
    raw = os.environ.get("PIPELINE_ARCH_DOCS", "")
    paths = [p.strip() for p in raw.replace("\n", ";").split(";") if p.strip()]
    present = [p for p in paths if (REPO / p).exists()]
    return "\n".join(f"- {p}" for p in present) or "- (none configured)"

# --- Role -> {model, command} mapping. Single concept: each role knows which
# model to use and how to invoke it. Override either from .env:
#   PIPELINE_MODEL_PROPOSER=glm-5.3
#   PIPELINE_CMD_PROPOSER=cursor-agent --model {model} --trust -p {prompt}
# Placeholders in cmd: {model}, {prompt}, {prompt_file}. First token = binary.
_DEFAULT_ROLE_CONFIG = {
    "INTERVIEWER": {
        "model": "gemini-3.1-pro-preview",
        "cmd":   "gemini -m {model} --include-directories {docs_dir} -p {prompt}",
    },
    "PROPOSER": {
        "model": "glm-5.2",
        "cmd":   "stdbuf -oL devin --model {model} --prompt-file {prompt_file} -p --permission-mode dangerous --respect-workspace-trust false",
    },
    "PLAN_REVIEWER": {
        "model": "codex",
        "cmd":   "stdbuf -oL devin --model {model} --prompt-file {prompt_file} -p --permission-mode dangerous --respect-workspace-trust false",
    },
    "IMPLEMENTER": {
        "model": "glm-5.2",
        "cmd":   "stdbuf -oL devin --model {model} --prompt-file {prompt_file} -p --permission-mode dangerous --respect-workspace-trust false",
    },
    "CODE_REVIEWER": {
        "model": "codex",
        "cmd":   "stdbuf -oL devin --model {model} --prompt-file {prompt_file} -p --permission-mode dangerous --respect-workspace-trust false",
    },
    # gemini-3.6-flash, not 3.1-pro-preview: pro failed in-band on the file-read
    # tool call (malformed tool call) on 008/009; flash passed two smoke tests
    # including reading UX-MANIFESTO.md and extracting P1 correctly. Faster too.
    # (composer was the plan but cursor-agent is out of usage.)
    "UX_REVIEWER": {
        "model": "gemini-3.6-flash",
        "cmd":   "gemini -m {model} --include-directories {docs_dir} -p {prompt}",
    },
    # Reviews RENDERED screenshots, so it must reliably read image files — claude
    # does (its Read tool renders PNGs); gemini botches the tool call. This is the
    # pipeline's only gate that looks at pixels, not text.
    "VISUAL_REVIEWER": {
        "model": "sonnet",
        "cmd":   "claude -p {prompt} --model {model} --add-dir {docs_dir}",
    },
    # FIXES visual blockers — and must SEE the screenshots to do it, or it plays
    # whack-a-mole (fix combat, break explore, blind to its own result). claude
    # both reads the PNGs and edits the frontend, so the fixer finally has eyes.
    "VISUAL_FIXER": {
        "model": "sonnet",
        "cmd":   "claude -p {prompt} --model {model} --add-dir {docs_dir}",
    },
    "SUMMARIZER": {
        "model": "glm-5.2",
        "cmd":   "stdbuf -oL devin --model {model} --prompt-file {prompt_file} -p --permission-mode dangerous --respect-workspace-trust false",
    },
    "JUDGE": {
        "model": "sonnet",
        "cmd":   "claude -p {prompt} --model {model} --add-dir {docs_dir}",
    },
}

ROLE_CONFIG: dict[str, dict[str, str]] = {}
for _role, _cfg in _DEFAULT_ROLE_CONFIG.items():
    _model = os.environ.get(f"PIPELINE_MODEL_{_role}", _cfg["model"])
    _cmd   = os.environ.get(f"PIPELINE_CMD_{_role}",  _cfg["cmd"])
    ROLE_CONFIG[_role] = {"model": _model, "cmd": _cmd}

# Line-buffering / process wrappers that precede the real agent CLI in a
# command. `stdbuf -oL devin …` must resolve to `devin`, not `stdbuf`, or
# preflight checks the wrong binary and logs name the wrong tool.
_WRAPPERS = {"stdbuf", "nohup", "setsid", "env", "time", "nice", "ionice"}


def role_binary(role: str) -> str | None:
    """The real CLI executable for a role, skipping any wrapper prefix.

    Walks past known wrappers (`stdbuf`) and their option flags, so
    `stdbuf -oL devin …` yields `devin`. Used by preflight to check the agent
    is actually installed — checking `stdbuf` would always pass.
    """
    cfg = ROLE_CONFIG.get(role)
    if cfg is None:
        return None
    tokens = shlex.split(cfg["cmd"])
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _WRAPPERS:
            i += 1
            while i < len(tokens) and tokens[i].startswith("-"):  # wrapper flags
                i += 1
        elif "=" in tok and not tok.startswith("-"):              # env VAR=val
            i += 1
        else:
            return tok
    return tokens[0] if tokens else None


def role_label(role: str) -> str:
    """Human-facing agent id for logs and filenames: the model, not the binary.

    PROPOSER (glm-5.2) and CODE_REVIEWER (codex) both run through `devin`, so the
    binary can't tell them apart — the model is the identity that matters.
    """
    cfg = ROLE_CONFIG.get(role)
    return cfg["model"] if cfg else "unknown"

def role_cmd(role: str, prompt_file: Path, prompt: str) -> list[str]:
    """Build the CLI invocation for a role from its config.

    The command template is split with shlex *before* interpolation, so a
    prompt containing unbalanced quotes becomes a single argv element
    instead of crashing shlex.split on the fully rendered string.
    """
    cfg = ROLE_CONFIG.get(role)
    if cfg is None:
        raise ValueError(
            f"unknown role: {role} "
            f"(add it to _DEFAULT_ROLE_CONFIG or set PIPELINE_CMD_{role})")
    tokens = shlex.split(cfg["cmd"])
    subs = {"model": cfg["model"], "prompt": prompt, "prompt_file": str(prompt_file), "docs_dir": str(DOCS)}
    return [tok.format(**subs) if "{" in tok else tok for tok in tokens]


def token_budget(role: str) -> int | None:
    """Per-role token budget for the debate condenser, or None if unset.

    ``PIPELINE_TOKEN_BUDGET_<ROLE>`` unset/blank -> None (condenser is a no-op
    for that role, the default backward-compatible state). A non-integer value
    is treated as unset with a stderr warning rather than raising — a config
    typo must not kill the whole pipeline process (P4).
    """
    raw = os.environ.get(f"PIPELINE_TOKEN_BUDGET_{role}")
    if not raw or not raw.strip():
        return None
    try:
        return int(raw)
    except ValueError:
        print(f"PIPELINE_TOKEN_BUDGET_{role}={raw!r} not an int; treating as unset",
              file=sys.stderr)
        return None

# No timeout by default: this is the whole point of leaving the Bash-tool ceiling behind.
AGENT_TIMEOUT = int(os.environ.get("PIPELINE_AGENT_TIMEOUT", "0")) or None

MAX_DEBATE_ROUNDS = int(os.environ.get("PIPELINE_MAX_DEBATE_ROUNDS", "2"))
MAX_FIX_CYCLES    = int(os.environ.get("PIPELINE_MAX_FIX_CYCLES", "2"))
MAX_TEST_FIXES    = int(os.environ.get("PIPELINE_MAX_TEST_FIXES", "2"))
MAX_INTAKE_ROUNDS = int(os.environ.get("PIPELINE_MAX_INTAKE_ROUNDS", "4"))

# --- Adaptive effort presets (TASK-011) -----------------------------------
# Three effort levels. `troop-monke` is byte-equivalent to the pre-existing
# MAX_DEBATE_ROUNDS / MAX_FIX_CYCLES (C10): the default behaviour is unchanged
# when no effort is selected. `scout-monke` skips the debate (route straight to
# summary) and disables the visual/render gates; `barrel-monke` runs more
# debate rounds and fix cycles on changes that touch critical paths.
_EFFORT_GATE_MODES = frozenset(("off", "standard", "full"))
_EFFORT_LEVELS_HARDCODED: dict[str, dict] = {
    "scout-monke":  {"debate_rounds": 0, "gates": "off", "fix_cycles": 1},
    "troop-monke":  {"debate_rounds": MAX_DEBATE_ROUNDS, "gates": "standard",
                     "fix_cycles": MAX_FIX_CYCLES},
    "barrel-monke": {"debate_rounds": max(MAX_DEBATE_ROUNDS, 3), "gates": "full",
                     "fix_cycles": max(MAX_FIX_CYCLES, 3)},
}

# PIPELINE_EFFORT_JSON (set by the YAML `effort:` key, or by hand) overrides the
# hardcoded presets. A parse failure or a structurally invalid shape degrades
# silently to the hardcoded default so a malformed env var never breaks an
# existing run that never opted in.
_EFFORT_REQUIRED_KEYS = ("debate_rounds", "gates", "fix_cycles")


def _normalize_effort_levels(obj) -> dict | None:
    """Validate and normalize effort levels, including the old bool gate form.

    ``false`` maps to ``off`` and ``true`` maps to ``standard`` so an existing
    YAML does not silently lose its custom effort values during this migration.
    ``full`` currently shares the enabled routing behavior with ``standard``;
    keeping the mode explicit leaves room for stricter full-gate policy later.
    """
    if not isinstance(obj, dict) or not obj:
        return None
    normalized: dict[str, dict] = {}
    for name, cfg in obj.items():
        if not isinstance(name, str) or not isinstance(cfg, dict):
            return None
        if not all(k in cfg for k in _EFFORT_REQUIRED_KEYS):
            return None
        if not isinstance(cfg["debate_rounds"], int) or cfg["debate_rounds"] < 0:
            return None
        if not isinstance(cfg["fix_cycles"], int) or cfg["fix_cycles"] < 0:
            return None
        gates = cfg["gates"]
        if isinstance(gates, bool):
            gates = "standard" if gates else "off"
        if gates not in _EFFORT_GATE_MODES:
            return None
        normalized[name] = {
            "debate_rounds": cfg["debate_rounds"],
            "gates": gates,
            "fix_cycles": cfg["fix_cycles"],
        }
    return normalized


def _is_valid_effort_levels(obj) -> bool:
    """True iff ``obj`` is a valid effort-level mapping."""
    return _normalize_effort_levels(obj) is not None


_effort_json_raw = os.environ.get("PIPELINE_EFFORT_JSON")
if _effort_json_raw:
    try:
        _parsed = json.loads(_effort_json_raw)
    except (json.JSONDecodeError, ValueError):
        _parsed = None
    EFFORT_LEVELS = _normalize_effort_levels(_parsed) or _EFFORT_LEVELS_HARDCODED
else:
    EFFORT_LEVELS = _EFFORT_LEVELS_HARDCODED

# Validate the env-chosen default; an unknown value would otherwise crash every
# resolver that indexes EFFORT_LEVELS via _effort_for. Fall back to the
# hardcoded default so a bad env var can't break routing/resume.
_effort_default_raw = os.environ.get("PIPELINE_EFFORT_DEFAULT", "troop-monke")
EFFORT_DEFAULT = _effort_default_raw if _effort_default_raw in EFFORT_LEVELS else "troop-monke"

# Paths whose modification marks a change as critical (→ barrel-monke hint).
_DEFAULT_CRITICAL_PATHS = "config.py;graph.py;state.py;run.py"
EFFORT_CRITICAL_PATHS = tuple(
    p.strip() for p in
    os.environ.get("PIPELINE_EFFORT_CRITICAL_PATHS", _DEFAULT_CRITICAL_PATHS).split(";")
    if p.strip()
)


def _effort_for(state) -> str:
    """The effective effort level for a state, with a safe fallback.

    Pre-feature checkpoints replay state as-is, so an in-flight run that
    predates this feature has no ``effort`` key — indexing ``EFFORT_LEVELS``
    directly would raise ``KeyError``. This returns the default instead of
    crashing, and every resolver/router funnels through it so the fallback
    lives in one place (C11).
    """
    effort = state.get("effort") if isinstance(state, dict) else None
    return effort if effort in EFFORT_LEVELS else EFFORT_DEFAULT


def resolved_debate_rounds(state) -> int:
    return EFFORT_LEVELS[_effort_for(state)]["debate_rounds"]


def resolved_fix_cycles(state) -> int:
    return EFFORT_LEVELS[_effort_for(state)]["fix_cycles"]


def resolved_gate_mode(state) -> str:
    """Return ``off``, ``standard`` or ``full`` for the effective effort."""
    return EFFORT_LEVELS[_effort_for(state)]["gates"]


def resolved_gates_enabled(state) -> bool:
    """Backward-compatible boolean view of the gate mode."""
    return resolved_gate_mode(state) != "off"


def effort_levels() -> dict:
    """Snapshot of the effort presets (for the effort checkpoint prompt)."""
    return EFFORT_LEVELS

# 3, not 2: a complex board needs more than two auto-fix passes (task-009 went
# 4→4→2 blockers and escalated with real issues still open). Override per-run
# with PIPELINE_MAX_UX_RENDER_CYCLES for simpler UI (down) or stuck ones (up).
MAX_UX_RENDER_CYCLES = int(os.environ.get("PIPELINE_MAX_UX_RENDER_CYCLES", "3"))

# Condenser: how many trailing debate rounds to keep verbatim when a role's
# token budget is exceeded. Older rounds collapse to one-line markers. Validated
# parse: a non-integer falls back to the default (3) with a stderr warning, and a
# negative value clamps to 0 (an unclamped negative inverts `rounds[:-keep_recent]`
# slice semantics — a silent-corruption class of bug). Read once at import time;
# tests that need a different value use `monkeypatch.setattr(C, ...)` rather than
# `setenv` (which would silently leave the import-time default).
_raw_keep_recent = os.environ.get("PIPELINE_CONDENSER_KEEP_RECENT", "3")
try:
    CONDENSER_KEEP_RECENT = int(_raw_keep_recent)
except ValueError:
    print(f"PIPELINE_CONDENSER_KEEP_RECENT={_raw_keep_recent!r} not an int; using default 3",
          file=sys.stderr)
    CONDENSER_KEEP_RECENT = 3
if CONDENSER_KEEP_RECENT < 0:
    print(f"PIPELINE_CONDENSER_KEEP_RECENT={CONDENSER_KEEP_RECENT} negative; clamping to 0",
          file=sys.stderr)
    CONDENSER_KEEP_RECENT = 0

# Consecutive non-improving cycles before the visual/render gates escalate early
# (plateau detection — the fix loop is oscillating, not converging).
PLATEAU_THRESHOLD = int(os.environ.get("PIPELINE_PLATEAU_THRESHOLD", "2"))

# The "eyes": a Playwright spec renders the built UI to screenshots + a facts
# JSON. The render node runs this command in the frontend; it must honour
# UX_RENDER_OUT (output dir). Disable the whole visual phase with an empty value.
SCREENS = DOCS / "reviews" / "screens"
# Idempotent upsert of the fixed-id render fixtures; ux_render runs it before
# rendering so the visual gate survives an e2e DB re-seed.
UX_SEED_SCRIPT = Path(os.environ.get("PIPELINE_UX_SEED_SCRIPT")
                      or (REPO / "scripts" / "e2e-seed-ux-fixtures.sh"))
UX_RENDER_CMD = os.environ.get(
    "PIPELINE_UX_RENDER_CMD",
    "npx playwright test tests/e2e/ux-render.spec.ts --project=chromium")
UX_RENDER_CWD = os.environ.get("PIPELINE_UX_RENDER_CWD", "frontend")
# Subprocess kill for the render command. Must exceed the spec's own
# test.setTimeout (600s cold-path for fixture creation) with margin.
UX_RENDER_TIMEOUT = int(os.environ.get("PIPELINE_UX_RENDER_TIMEOUT", "720"))

# --- Render gate (the perf analog of the visual gate) ----------------------
# Drives a scripted interaction and counts re-renders per instrumented subtree
# (window.__RENDER_LOG__, fed by <Profiler> hooks) into render-facts.json, then
# compares to a baseline to catch re-render REGRESSIONS. Deterministic — the
# review is numeric, no LLM critic. Reuses the ux-render seed fixtures.
RENDERS = DOCS / "reviews" / "renders"
RENDER_CMD = os.environ.get(
    "PIPELINE_RENDER_CMD",
    "npx playwright test tests/e2e/render-profile.spec.ts --project=chromium")
RENDER_CWD = os.environ.get("PIPELINE_RENDER_CWD", "frontend")
RENDER_TIMEOUT = int(os.environ.get("PIPELINE_RENDER_TIMEOUT", "300"))
MAX_RENDER_CYCLES = int(os.environ.get("PIPELINE_MAX_RENDER_CYCLES", "3"))

BRANCH_PREFIX = os.environ.get("PIPELINE_BRANCH_PREFIX", "feature/task-")

DRY_RUN = os.environ.get("PIPELINE_DRY_RUN") == "1"

# Observe-only git: run the REAL agents but make every history-mutating step a
# no-op (no branch checkout, no WIP/batch/final commits). Unlike DRY_RUN — which
# also stubs the agents — this leaves a true end-to-end smoke that never adds a
# commit or a branch. The agents still edit the working tree (and the review gate
# still stages it so it can see the diff), so nothing is committed but the tree
# is left dirty/staged: discard with `git reset --hard && git clean -fd`, or run
# in a sacrificial `git worktree` you delete afterwards.
NO_GIT = os.environ.get("PIPELINE_NO_GIT") == "1"

# --- E2E / test infrastructure
E2E_DB_PORT = int(os.environ.get("PIPELINE_E2E_DB_PORT", "5433"))
E2E_DB_CONTAINER = os.environ.get("PIPELINE_E2E_DB_CONTAINER", "nexus_vtt_e2e_db")
E2E_PROJECT = os.environ.get("PIPELINE_E2E_PROJECT", "nexus-vtt-e2e")
E2E_UP_SCRIPT = Path(os.environ.get("PIPELINE_E2E_UP_SCRIPT") or (REPO / "scripts" / "e2e-up.sh"))
E2E_UP_TIMEOUT = int(os.environ.get("PIPELINE_E2E_UP_TIMEOUT", "660"))

# Host-side URL for vitest when the e2e Postgres container maps 5433→5432.
# Override with PIPELINE_E2E_DATABASE_URL; never read backend/.env implicitly.
E2E_DATABASE_URL = os.environ.get(
    "PIPELINE_E2E_DATABASE_URL",
    "postgresql://postgres:postgrespassword@localhost:5433/nexusvtt?schema=public",
)

# Notify daemon (persistent rate-limited notification dispatcher).
NOTIFY_RATE = int(os.environ.get("PIPELINE_NOTIFY_RATE", "30"))
NOTIFY_WINDOW = int(os.environ.get("PIPELINE_NOTIFY_WINDOW", "60"))
NOTIFY_SOCKET = Path(os.environ.get("PIPELINE_NOTIFY_SOCKET")
                     or (METRICS / "notify.sock"))

# Test suites to run for the in-graph test gate. Each entry is (label, subdir, env).
# Format: "label:subdir:ENV_VAR=val,ENV2=val2;label2:subdir2:"
# Use {e2e_db} in env values to interpolate E2E_DATABASE_URL.
# An empty PIPELINE_TEST_SUITES disables the test gate entirely.
_DEFAULT_TEST_SUITES = "backend:backend:DATABASE_URL={e2e_db};frontend:frontend:"
_TEST_SUITES_RAW = os.environ.get("PIPELINE_TEST_SUITES", _DEFAULT_TEST_SUITES)
TEST_SUITES: list[tuple[str, str, dict[str, str]]] = []
if _TEST_SUITES_RAW.strip():
    for entry in _TEST_SUITES_RAW.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":", 2)
        label = parts[0]
        subdir = parts[1] if len(parts) > 1 else ""
        env_str = parts[2] if len(parts) > 2 else ""
        suite_env = os.environ.copy()
        for pair in env_str.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                suite_env[k.strip()] = v.format(e2e_db=E2E_DATABASE_URL)
        TEST_SUITES.append((label, subdir, suite_env))

def db_reachable() -> bool:
    """Check if the e2e Postgres is accepting connections on its mapped port."""
    import socket
    try:
        with socket.create_connection(("localhost", E2E_DB_PORT), timeout=2):
            return True
    except OSError:
        return False

def ensure_e2e_stack() -> bool:
    """Bring the e2e stack up via scripts/e2e-up.sh. Returns True if the DB answers.

    The script's default path is idempotent (`up -d` + wait-on, no `down -v`,
    no `--build`), so calling it as an inline precondition is cheap when the
    stack is already running. The destructive rebuild is opt-in (`--fresh`) and
    is never what the pipeline wants.
    """
    if db_reachable():
        return True
    if not E2E_UP_SCRIPT.exists():
        return False
    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = E2E_PROJECT
    try:
        subprocess.run(["bash", str(E2E_UP_SCRIPT)], cwd=REPO, env=env,
                       capture_output=True, timeout=E2E_UP_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    return db_reachable()

def preflight() -> list[str]:
    """Return a list of problems; empty means we are good to go."""
    problems = []
    if not (REPO / ".git").exists():
        problems.append(f"{REPO} is not a git repository")
    if not DRY_RUN:
        for role in ROLE_CONFIG:
            binary = role_binary(role)
            if binary is None or shutil.which(binary) is None:
                problems.append(f"CLI not found in PATH: {binary} (role '{role}')")
    return problems

def ensure_dirs() -> None:
    for d in (PLANS, DEBATES, FINAL, REVIEWS, PROMPTS, QUEUE, TASKS, METRICS, RAW):
        d.mkdir(parents=True, exist_ok=True)

def sync_back_docs() -> None:
    """No-op: DOCS is written in place now (no in-repo mirror to copy back).

    Kept so existing call sites (e.g. finalize) stay valid. Agents read DOCS
    directly via their external-dir flags, and the Python orchestrator writes
    artifacts straight to DOCS, so there is nothing to synchronise after a run.
    """
    return
