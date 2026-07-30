"""Configuration: roles -> agent CLIs, paths, timeouts. Single source of truth."""
from __future__ import annotations
import os, shlex, shutil, subprocess
from pathlib import Path

REPO = Path(
    os.environ.get("PIPELINE_REPO")
    or subprocess.run(["git", "rev-parse", "--show-toplevel"],
                      capture_output=True, text=True).stdout.strip()
    or "."
).resolve()

# MonkeForge root: pipeline_graph/config.py -> pipeline_graph/ -> MonkeForge/
MF_ROOT = Path(__file__).resolve().parents[1]


def _git_common_dir(repo: Path) -> Path | None:
    """Absolute path to the repo's .git common dir (handles worktrees/submodules).

    In a linked worktree `.git` is a file pointing elsewhere, so we can't assume
    `repo/.git`; ask git for the common dir where info/exclude actually lives.
    """
    out = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=repo, capture_output=True, text=True,
    ).stdout.strip()
    if not out:
        return None
    p = Path(out)
    return p if p.is_absolute() else (repo / p).resolve()


def _ensure_git_excludes(repo: Path, *entries: str) -> None:
    """Add local-only ignore rules via .git/info/exclude, not the tracked .gitignore.

    This keeps pipeline artifacts that live inside the repo invisible to git:
    `git status`, `git add -A`, and `git ls-files -o --exclude-standard` all honour
    info/exclude. So the working tree stays clean and the per-batch commits carry
    only real code — without dirtying a committed .gitignore. Best-effort: on any
    filesystem hiccup we silently fall back to the dirty-check tolerance.
    """
    common = _git_common_dir(repo)
    if common is None:
        return
    try:
        info = common / "info"
        info.mkdir(parents=True, exist_ok=True)
        exclude = info / "exclude"
        existing = exclude.read_text().splitlines() if exclude.exists() else []
        missing = [e for e in entries if e not in existing]
        if not missing:
            return
        with exclude.open("a") as fh:
            if existing and existing[-1].strip():
                fh.write("\n")
            fh.write("# MonkeForge pipeline artifacts (local, never committed)\n")
            for e in missing:
                fh.write(f"{e}\n")
    except OSError:
        pass

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

# Repo-relative path to docs for agent prompts.  When docs live inside the
# repo this is just the relative path.  When they live outside (the default),
# we copy them into .pipeline-docs/ inside the repo and repoint DOCS there so
# all agents (including claude, which blocks symlinks) can read/write within
# their working directory.  The original DOCS is kept for sync-back.
#
# .pipeline-docs/ is registered in .git/info/exclude (a local, uncommitted
# ignore) the moment we create it, so git never sees it: the working tree stays
# clean, `init` doesn't escalate/WIP-commit on it, and per-batch commits carry
# only real code.  That's why INIT_DIRTY_OK_PREFIXES stays empty here.
if DOCS.is_relative_to(REPO):
    DOCS_REL = str(DOCS.relative_to(REPO))
    DOCS_ORIG = None
else:
    import shutil as _sh
    _local = REPO / ".pipeline-docs"
    _ensure_git_excludes(REPO, ".pipeline-docs/")
    if _local.exists() and not _local.is_dir():
        _local.unlink()
    if not _local.exists():
        _local.mkdir(parents=True)
    for _sub in _local.iterdir():
        if _sub.is_dir():
            _sh.rmtree(_sub)
        else:
            _sub.unlink()
    for _item in DOCS.iterdir():
        if _item.is_dir():
            _sh.copytree(_item, _local / _item.name, dirs_exist_ok=True,
                         ignore=_sh.ignore_patterns('*.sock', 'notify.spool*'))
        elif _item.is_socket() or not _item.is_file():
            continue
        else:
            _sh.copy2(_item, _local / _item.name)
    DOCS_ORIG = DOCS
    DOCS = _local
    DOCS_REL = ".pipeline-docs"
    # Re-derive sub-paths from the repointed DOCS
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
        "cmd":   "gemini -m {model} -p {prompt}",
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
        "cmd":   "gemini -m {model} -p {prompt}",
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

# No timeout by default: this is the whole point of leaving the Bash-tool ceiling behind.
AGENT_TIMEOUT = int(os.environ.get("PIPELINE_AGENT_TIMEOUT", "0")) or None

MAX_DEBATE_ROUNDS = int(os.environ.get("PIPELINE_MAX_DEBATE_ROUNDS", "2"))
MAX_FIX_CYCLES    = int(os.environ.get("PIPELINE_MAX_FIX_CYCLES", "2"))
MAX_TEST_FIXES    = int(os.environ.get("PIPELINE_MAX_TEST_FIXES", "2"))
MAX_INTAKE_ROUNDS = int(os.environ.get("PIPELINE_MAX_INTAKE_ROUNDS", "4"))
# 3, not 2: a complex board needs more than two auto-fix passes (task-009 went
# 4→4→2 blockers and escalated with real issues still open). Override per-run
# with PIPELINE_MAX_UX_RENDER_CYCLES for simpler UI (down) or stuck ones (up).
MAX_UX_RENDER_CYCLES = int(os.environ.get("PIPELINE_MAX_UX_RENDER_CYCLES", "3"))

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
    """Copy .pipeline-docs/ back to the original DOCS location after a run."""
    if not DOCS_ORIG:
        return
    import shutil as _sh
    DOCS_ORIG.mkdir(parents=True, exist_ok=True)
    for _item in DOCS.iterdir():
        _dest = DOCS_ORIG / _item.name
        if _item.is_dir():
            _sh.copytree(_item, _dest, dirs_exist_ok=True,
                         ignore=_sh.ignore_patterns('*.sock', 'notify.spool*'))
        elif _item.is_socket() or not _item.is_file():
            continue
        else:
            _sh.copy2(_item, _dest)
