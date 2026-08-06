"""Configuration: roles -> agent CLIs, paths, timeouts. Single source of truth."""
from __future__ import annotations
import json, math, os, shlex, shutil, subprocess, sys
from dataclasses import dataclass
from pathlib import Path

# MonkeForge root: pipeline_graph/config.py -> pipeline_graph/ -> MonkeForge/
MF_ROOT = Path(__file__).resolve().parents[1]

# Target git repo. MUST come from PIPELINE_REPO (set by run.py via --repo /
# env / yaml repos: picker). No git-cwd / MF_ROOT fallback — that silently
# pointed lab runs at the pipeline itself.
_repo_env = os.environ.get("PIPELINE_REPO", "").strip()
if not _repo_env:
    from .repo_select import RepoSelectError
    raise RepoSelectError(
        "error: PIPELINE_REPO is not set\n"
        "\n"
        "  There is no default target repo (cwd / git root is NOT used).\n"
        "  Use ./run.py (it resolves --repo / env / yaml repos:), or\n"
        "  export PIPELINE_REPO=/abs/path/to/app before importing config."
    )
REPO = Path(_repo_env).expanduser().resolve()



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

# --- Role -> {model, command} mapping ---------------------------------------
# BOTH ``model:`` and ``cmd:`` are REQUIRED under ``agents:`` in
# monkeforge.yaml for every role. There are NO built-in defaults in code —
# not for models, not for invocation templates. Missing/empty → fail at
# import with a clig-style ``error:`` (no traceback).
# Placeholders the operator may use in cmd: {model}, {prompt}, {prompt_file},
# {docs_dir}.
REQUIRED_ROLES: tuple[str, ...] = (
    "INTERVIEWER",
    "PROPOSER",
    "PLAN_REVIEWER",
    "IMPLEMENTER",
    "CODE_REVIEWER",
    "UX_REVIEWER",
    "VISUAL_REVIEWER",
    "VISUAL_FIXER",
    "SUMMARIZER",
    "JUDGE",
)


class AgentsConfigError(Exception):
    """Missing/invalid ``agents:`` in monkeforge.yaml (expected, not a crash).

    Carry a human CLI message (clig.dev): catch in ``run.py``, print to
    stderr, exit 2 — never dump a traceback for this.
    """

    def __init__(
        self,
        *,
        kind: str,
        missing: list[str] | None = None,
        yaml_path: Path | None = None,
        example_path: Path | None = None,
    ):
        self.kind = kind
        self.missing = list(missing or [])
        self.yaml_path = yaml_path
        self.example_path = example_path
        super().__init__(self.cli_message())

    def cli_message(self) -> str:
        """Multi-line ``error:`` text for humans (stderr, no traceback)."""
        ypath = str(self.yaml_path) if self.yaml_path else "monkeforge.yaml"
        epath = (
            str(self.example_path)
            if self.example_path
            else "monkeforge.example.yaml"
        )
        if self.kind == "missing_block":
            return "\n".join([
                f"error: {ypath}: missing required top-level `agents:` block",
                "",
                "  Every role needs BOTH `model:` and `cmd:` — there are no",
                "  built-in agent defaults in code (not models, not commands).",
                "",
                "  Roles:",
                "    " + ", ".join(REQUIRED_ROLES),
                "",
                "  Fix: copy the `agents:` section from",
                f"    {epath}",
                f"  into {ypath}, then set the models/CLIs you want to run.",
            ])
        listed = "\n".join(f"    - {r}" for r in self.missing) or "    (none)"
        sample = self.missing[0].split(" ", 1)[0] if self.missing else "JUDGE"
        return "\n".join([
            f"error: {ypath}: `agents:` incomplete for "
            f"{len(self.missing)} role(s)",
            "",
            listed,
            "",
            "  Each role needs BOTH keys. Example:",
            "",
            "    agents:",
            f"      {sample}:",
            "        model: <your-model>",
            "        cmd: \"<your-cli> --model {model} ...\"",
            "",
            "  Placeholders you may use in cmd: {model} {prompt}",
            "  {prompt_file} {docs_dir}",
            "",
            f"  Full template: {epath}",
        ])


def build_role_config(
    yaml_agents: dict | None,
    *,
    required_roles: tuple[str, ...] | None = None,
    yaml_path: Path | None = None,
    example_path: Path | None = None,
) -> dict[str, dict[str, str]]:
    """Build ROLE_CONFIG from yaml ``agents:``.

    Raises ``AgentsConfigError`` if any role lacks ``model:`` or ``cmd:``.
    Nothing is invented here — yaml is the only source.
    """
    roles = required_roles if required_roles is not None else REQUIRED_ROLES
    if not isinstance(yaml_agents, dict) or not yaml_agents:
        raise AgentsConfigError(
            kind="missing_block",
            yaml_path=yaml_path,
            example_path=example_path,
        )
    missing: list[str] = []
    out: dict[str, dict[str, str]] = {}
    for role in roles:
        over = yaml_agents.get(role, {})
        if not isinstance(over, dict):
            over = {}
        model = str(over.get("model") or "").strip()
        cmd = str(over.get("cmd") or "").strip()
        gaps = []
        if not model:
            gaps.append("model")
        if not cmd:
            gaps.append("cmd")
        if gaps:
            missing.append(f"{role} (missing {', '.join(gaps)})")
            continue
        out[role] = {"model": model, "cmd": cmd}
    if missing:
        raise AgentsConfigError(
            kind="missing_fields",
            missing=missing,
            yaml_path=yaml_path,
            example_path=example_path,
        )
    return out


# For agents: and condenser:, the yaml is the only source (no env bridge).
_yaml_file = MF_ROOT / "monkeforge.yaml"
_example_yaml = MF_ROOT / "monkeforge.example.yaml"
_yaml_root: dict = {}
if _yaml_file.exists():
    import yaml as _yaml
    _loaded = _yaml.safe_load(_yaml_file.read_text()) or {}
    if isinstance(_loaded, dict):
        _yaml_root = _loaded

ROLE_CONFIG: dict[str, dict[str, str]] = build_role_config(
    _yaml_root.get("agents"),
    yaml_path=_yaml_file if _yaml_file.exists() else MF_ROOT / "monkeforge.yaml",
    example_path=_example_yaml,
)

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
            f"(declare it under agents: in monkeforge.yaml)")
    tokens = shlex.split(cfg["cmd"])
    subs = {"model": cfg["model"], "prompt": prompt, "prompt_file": str(prompt_file), "docs_dir": str(DOCS)}
    return [tok.format(**subs) if "{" in tok else tok for tok in tokens]


# Prompts above this size are passed via stdin instead of as a CLI argument,
# to avoid OSError [Errno 7] Argument list too long on roles that use
# {prompt} inline (cursor-agent, claude). Roles that use {prompt_file}
# (devin) are unaffected — the file path is always short.
PROMPT_STDIN_THRESHOLD = 32_000


def role_cmd_with_stdin(role: str, prompt_file: Path, prompt: str) -> tuple[list[str], str | None]:
    """Build the CLI invocation, falling back to stdin for large prompts.

    Returns (cmd, stdin_text). When the cmd template uses {prompt} inline and
    the rendered prompt exceeds PROMPT_STDIN_THRESHOLD, the {prompt} placeholder
    is replaced with an empty string and the prompt is returned as stdin_text
    for the caller to pipe to the process. This avoids the OS arg-limit crash
    (Errno 7) on long debate/plan prompts passed to cursor-agent/claude.

    Roles that use {prompt_file} (devin) never hit this — the file path is
    always short and the agent reads the file directly.
    """
    cfg = ROLE_CONFIG.get(role)
    if cfg is None:
        raise ValueError(
            f"unknown role: {role} "
            f"(declare it under agents: in monkeforge.yaml)")
    template = cfg["cmd"]
    if "{prompt}" in template and len(prompt) > PROMPT_STDIN_THRESHOLD:
        tokens = shlex.split(template)
        subs = {"model": cfg["model"], "prompt": "", "prompt_file": str(prompt_file), "docs_dir": str(DOCS)}
        cmd = [tok.format(**subs) if "{" in tok else tok for tok in tokens]
        # Remove trailing empty-string args (the emptied {prompt} placeholder)
        cmd = [t for t in cmd if t != ""]
        return cmd, prompt
    return role_cmd(role, prompt_file, prompt), None


def _parse_token_budget_value(role: str, raw) -> int | None:
    """Validate one condenser budget. Non-int → None + stderr warning (P4)."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        print(f"condenser.{role}={raw!r} not an int; treating as unset",
              file=sys.stderr)
        return None
    if isinstance(raw, int):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        print(f"condenser.{role}={raw!r} not an int; treating as unset",
              file=sys.stderr)
        return None


def _load_token_budgets(condenser: dict) -> dict[str, int]:
    """Build role→int map from the yaml ``condenser:`` mapping (sans keep_recent)."""
    out: dict[str, int] = {}
    for key, val in condenser.items():
        if key == "keep_recent":
            continue
        role = str(key).upper()
        parsed = _parse_token_budget_value(role, val)
        if parsed is not None:
            out[role] = parsed
    return out


# Per-role budgets from yaml ``condenser:`` (not env). Mutable for tests.
_yaml_condenser = _yaml_root.get("condenser") or {}
if not isinstance(_yaml_condenser, dict):
    _yaml_condenser = {}
_TOKEN_BUDGETS: dict[str, int] = _load_token_budgets(_yaml_condenser)


def token_budget(role: str) -> int | None:
    """Per-role token budget for the debate condenser, or None if unset.

    Read from ``monkeforge.yaml`` ``condenser.<ROLE>`` only (same policy as
    ``agents:``). Unset → None (condenser no-op for that role).
    """
    return _TOKEN_BUDGETS.get(str(role).upper())


# No timeout by default: this is the whole point of leaving the Bash-tool ceiling behind.
AGENT_TIMEOUT = int(os.environ.get("PIPELINE_AGENT_TIMEOUT", "0")) or None

MAX_DEBATE_ROUNDS = int(os.environ.get("PIPELINE_MAX_DEBATE_ROUNDS", "2"))
MAX_FIX_CYCLES    = int(os.environ.get("PIPELINE_MAX_FIX_CYCLES", "2"))
MAX_TEST_FIXES    = int(os.environ.get("PIPELINE_MAX_TEST_FIXES", "2"))
MAX_INTAKE_ROUNDS = int(os.environ.get("PIPELINE_MAX_INTAKE_ROUNDS", "4"))

# TASK-033: cap on REQUIREMENTS re-intake cycles. Below MAX the
# "debate requirements:" menu offers re-intake/continue/redo/stop (re-intake
# RECOMMENDED); at MAX it adds ok (RECOMMENDED) so the run can ship with the
# gaps recorded as a degradation. Validation mirrors _parse_condenser_keep_recent
# style: non-int → stderr warn + default 2; negative → clamp 0 (escape hatch:
# MAX at count 0 → first menu offers ok immediately).
def _parse_max_requirements_reintakes(raw, *, default: int = 2) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool):
        print(f"PIPELINE_MAX_REQUIREMENTS_REINTAKES={raw!r} not an int; "
              f"using default {default}", file=sys.stderr)
        return default
    if isinstance(raw, int):
        val = raw
    else:
        try:
            val = int(str(raw).strip())
        except ValueError:
            print(f"PIPELINE_MAX_REQUIREMENTS_REINTAKES={raw!r} not an int; "
                  f"using default {default}", file=sys.stderr)
            return default
    if val < 0:
        print(f"PIPELINE_MAX_REQUIREMENTS_REINTAKES={val} negative; "
              f"clamping to 0", file=sys.stderr)
        return 0
    return val


MAX_REQUIREMENTS_REINTAKES = _parse_max_requirements_reintakes(
    os.environ.get("PIPELINE_MAX_REQUIREMENTS_REINTAKES")
)

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
    base = EFFORT_LEVELS[_effort_for(state)]["debate_rounds"]
    # A human "continue" answer on a debate-cap escalation extends the cap
    # without losing the existing debate history (unlike "redo" which resets
    # to round 1). The bonus is additive and persists in state across resumes.
    bonus = (state.get("debate_round_bonus") if isinstance(state, dict) else None) or 0
    return base + bonus


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

_EFFORT_DESCRIPTIONS = {
    "scout-monke": "leggero: implementazione + review, senza dibattito né gate",
    "troop-monke": "medio: implementazione + review + dibattito standard",
    "barrel-monke": "pesante: dibattito esteso, fix extra e gate completi",
}


def effort_choices() -> dict[str, str]:
    """Human-readable choices for CLI/Discord effort checkpoints."""
    choices = {}
    for name, preset in EFFORT_LEVELS.items():
        description = _EFFORT_DESCRIPTIONS.get(name, "configurato dall'utente")
        choices[name] = (
            f"{description} (dibattito {preset['debate_rounds']} round, "
            f"gate {preset['gates']}, fix {preset['fix_cycles']})"
        )
    return choices

# 3, not 2: a complex board needs more than two auto-fix passes (task-009 went
# 4→4→2 blockers and escalated with real issues still open). Override per-run
# with PIPELINE_MAX_UX_RENDER_CYCLES for simpler UI (down) or stuck ones (up).
MAX_UX_RENDER_CYCLES = int(os.environ.get("PIPELINE_MAX_UX_RENDER_CYCLES", "3"))

# Condenser: how many trailing debate rounds to keep verbatim when a role's
# token budget is exceeded. Older rounds collapse to one-line markers.
# Source: yaml ``condenser.keep_recent`` only.
# Non-integer → default 3 + stderr warning; negative → clamp 0 (unclamped
# negative inverts `rounds[:-keep_recent]` — silent-corruption class).
def _parse_condenser_keep_recent(raw, *, default: int = 3) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool):
        print(f"condenser.keep_recent={raw!r} not an int; using default {default}",
              file=sys.stderr)
        return default
    if isinstance(raw, int):
        val = raw
    else:
        try:
            val = int(str(raw).strip())
        except ValueError:
            print(f"condenser.keep_recent={raw!r} not an int; using default {default}",
                  file=sys.stderr)
            return default
    if val < 0:
        print(f"condenser.keep_recent={val} negative; clamping to 0", file=sys.stderr)
        return 0
    return val


CONDENSER_KEEP_RECENT = _parse_condenser_keep_recent(
    _yaml_condenser.get("keep_recent") if isinstance(_yaml_condenser, dict) else None
)

# Stuck-claim detection (TASK-017 batch 1): how many consecutive trailing rounds
# to scan for a repeated BLOCKER claim before declaring the debate "stuck" and
# escalating early (before hitting the round cap). Validated parse mirrors
# CONDENSER_KEEP_RECENT: a non-integer falls back to the default (2) with a
# stderr warning. Clamped to a minimum of 1 (with a stderr warning) so a
# misconfigured 0 never disables the guard silently. Then clamped to
# CONDENSER_KEEP_RECENT: the stuck scan reads the last k rounds verbatim, so k
# must not exceed the number of rounds the condenser keeps verbatim — when
# CONDENSER_KEEP_RECENT is 0 (condense-all), DEBATE_STUCK_ROUNDS is forced to 0
# too (the guard is disabled because there are no verbatim rounds to scan).
def _clamp_stuck_to_keep_recent(stuck: int, keep_recent: int) -> int:
    """Clamp stuck-round window to the condenser verbatim window.

    When ``keep_recent`` is 0 (condense-all) this returns 0 — the stuck guard
    is disabled because there are no verbatim rounds to scan.
    """
    if keep_recent < stuck:
        return keep_recent
    return stuck


_raw_stuck = os.environ.get("PIPELINE_DEBATE_STUCK_ROUNDS", "2")
try:
    DEBATE_STUCK_ROUNDS = int(_raw_stuck)
except ValueError:
    print(f"PIPELINE_DEBATE_STUCK_ROUNDS={_raw_stuck!r} not an int; using default 2",
          file=sys.stderr)
    DEBATE_STUCK_ROUNDS = 2
if DEBATE_STUCK_ROUNDS < 1:
    print(f"PIPELINE_DEBATE_STUCK_ROUNDS={DEBATE_STUCK_ROUNDS} below 1; clamping to 1",
          file=sys.stderr)
    DEBATE_STUCK_ROUNDS = 1
_stuck_before_clamp = DEBATE_STUCK_ROUNDS
DEBATE_STUCK_ROUNDS = _clamp_stuck_to_keep_recent(
    DEBATE_STUCK_ROUNDS, CONDENSER_KEEP_RECENT
)
if DEBATE_STUCK_ROUNDS != _stuck_before_clamp:
    print(
        f"PIPELINE_DEBATE_STUCK_ROUNDS={_stuck_before_clamp} exceeds "
        f"CONDENSER_KEEP_RECENT={CONDENSER_KEEP_RECENT}; clamping to "
        f"{DEBATE_STUCK_ROUNDS}",
        file=sys.stderr,
    )

# Thrashing detection (TASK-022): how many consecutive trailing rounds the
# deterministic thrashing_report scans for a churning trend (blockers not
# strictly decreasing AND new claims appearing). Validated parse mirrors
# DEBATE_STUCK_ROUNDS: a non-integer falls back to the default
# (DEBATE_STUCK_ROUNDS) with a stderr warning. Clamped to [1,
# CONDENSER_KEEP_RECENT] — the report reads the last k rounds verbatim, so k
# must not exceed the number of rounds the condenser keeps verbatim, and a
# thrashing trend needs at least 2 active rounds to be meaningful (the
# thrashing_report itself returns mode="unknown" when fewer than 2 active
# rounds are in the window, but the clamp keeps the scan window sane).
_raw_thrash = os.environ.get("PIPELINE_DEBATE_THRASH_ROUNDS")
if _raw_thrash is None or not _raw_thrash.strip():
    DEBATE_THRASH_ROUNDS = DEBATE_STUCK_ROUNDS
else:
    try:
        DEBATE_THRASH_ROUNDS = int(_raw_thrash)
    except ValueError:
        print(f"PIPELINE_DEBATE_THRASH_ROUNDS={_raw_thrash!r} not an int; "
              f"using default {DEBATE_STUCK_ROUNDS}", file=sys.stderr)
        DEBATE_THRASH_ROUNDS = DEBATE_STUCK_ROUNDS
if DEBATE_THRASH_ROUNDS < 1:
    print(f"PIPELINE_DEBATE_THRASH_ROUNDS={DEBATE_THRASH_ROUNDS} below 1; "
          f"clamping to 1", file=sys.stderr)
    DEBATE_THRASH_ROUNDS = 1
if CONDENSER_KEEP_RECENT < DEBATE_THRASH_ROUNDS:
    # Clamp to CONDENSER_KEEP_RECENT but never below the lower bound of 1
    # (item 7): with CONDENSER_KEEP_RECENT=0 the bare clamp would disable
    # thrashing detection (k=0), violating the required minimum.
    _thrash_ceiling = max(CONDENSER_KEEP_RECENT, 1)
    print(
        f"PIPELINE_DEBATE_THRASH_ROUNDS={DEBATE_THRASH_ROUNDS} exceeds "
        f"CONDENSER_KEEP_RECENT={CONDENSER_KEEP_RECENT}; clamping to "
        f"{_thrash_ceiling}",
        file=sys.stderr,
    )
    DEBATE_THRASH_ROUNDS = _thrash_ceiling

# Thrashing-refinement policy (TASK-024): the theme-Jaccard threshold above
# which a "new" BLOCKER claim in a thrashing window is considered a refinement
# of an existing (prior) claim's theme rather than fresh surface area. When a
# majority of the latest round's new claims refine prior themes, the triage
# recommends "continue" (the proposer is iterating on the same themes, not
# churning onto unrelated ones); otherwise it recommends "ok" (proceed to the
# verdict — the new claims are genuinely fresh and more rounds will not help).
# Validated parse mirrors _raw_thrash: a non-float, NaN/Inf, or out-of-(0, 1]
# value falls back to the default (0.35) with a stderr warning.
_raw_theme_jaccard = os.environ.get("PIPELINE_DEBATE_THRASH_THEME_JACCARD")
if _raw_theme_jaccard is None or not _raw_theme_jaccard.strip():
    DEBATE_THRASH_THEME_JACCARD = 0.35
else:
    try:
        _val_theme_jaccard = float(_raw_theme_jaccard)
    except ValueError:
        print(
            f"PIPELINE_DEBATE_THRASH_THEME_JACCARD={_raw_theme_jaccard!r} not a float; "
            f"using default 0.35",
            file=sys.stderr,
        )
        _val_theme_jaccard = 0.35
    if math.isnan(_val_theme_jaccard) or math.isinf(_val_theme_jaccard):
        print(
            f"PIPELINE_DEBATE_THRASH_THEME_JACCARD={_raw_theme_jaccard!r} is NaN/Inf; "
            f"using default 0.35",
            file=sys.stderr,
        )
        _val_theme_jaccard = 0.35
    if _val_theme_jaccard <= 0 or _val_theme_jaccard > 1:
        print(
            f"PIPELINE_DEBATE_THRASH_THEME_JACCARD={_raw_theme_jaccard!r} out of range "
            f"(0, 1]; using default 0.35",
            file=sys.stderr,
        )
        _val_theme_jaccard = 0.35
    DEBATE_THRASH_THEME_JACCARD = _val_theme_jaccard

# Consecutive non-improving cycles before the visual/render gates escalate early
# (plateau detection — the fix loop is oscillating, not converging).
PLATEAU_THRESHOLD = int(os.environ.get("PIPELINE_PLATEAU_THRESHOLD", "2"))

# TASK-023: lean plan-view threshold for the critic rounds. When the current
# plan is at least this many bytes AND the round is >= 2 AND a non-empty
# plan_snapshot differs from the current plan, the critic is sent
# condenser.plan_diff(snapshot, plan) (only the changed sections) instead of
# the full plan. Below the threshold, or with no usable diff, the full plan is
# sent. Validated parse mirrors CONDENSER_KEEP_RECENT: a non-integer falls back
# to the default (8192) with a stderr warning.
_raw_lean = os.environ.get("PIPELINE_LEAN_PLAN_FULL_THRESHOLD", "8192")
try:
    LEAN_PLAN_FULL_THRESHOLD = int(_raw_lean)
except ValueError:
    print(
        f"PIPELINE_LEAN_PLAN_FULL_THRESHOLD={_raw_lean!r} not an int; "
        f"using default 8192",
        file=sys.stderr,
    )
    LEAN_PLAN_FULL_THRESHOLD = 8192

# The "eyes": a Playwright spec renders the built UI to screenshots + a facts
# JSON. The render node runs this command in the frontend; it must honour
# UX_RENDER_OUT (output dir). Disable the whole visual phase with an empty value.
SCREENS = DOCS / "reviews" / "screens"
# Idempotent upsert of the fixed-id render fixtures; ux_render runs it before
# rendering so the visual gate survives an e2e DB re-seed. None when unset —
# MonkeForge is standalone and ships no seed script by default; opt in via
# PIPELINE_UX_SEED_SCRIPT (repo-relative or absolute path).
_ux_seed_raw = os.environ.get("PIPELINE_UX_SEED_SCRIPT", "").strip()
UX_SEED_SCRIPT: Path | None = (Path(_ux_seed_raw) if _ux_seed_raw else None)
UX_RENDER_CMD = os.environ.get("PIPELINE_UX_RENDER_CMD", "")
UX_RENDER_CWD = os.environ.get("PIPELINE_UX_RENDER_CWD", "")
# Subprocess kill for the render command. Must exceed the spec's own
# test.setTimeout (600s cold-path for fixture creation) with margin.
UX_RENDER_TIMEOUT = int(os.environ.get("PIPELINE_UX_RENDER_TIMEOUT", "720"))

# --- Eyes runner (TASK-012) -------------------------------------------------
# The generalized "eyes" runner: declarative ``ui:`` yaml → checkpointed
# ``ui_config`` → human interrupt when engaged, validated Playwright traces,
# ``monkeforge.eyes.facts/v1`` facts + screenshots, and the first real
# ``standard`` ≠ ``full`` gate split. Legacy subprocess compat retained.

# ``ui:`` is read directly from ``monkeforge.yaml`` (like ``agents:`` /
# ``condenser:`` / ``test_suites:``), NOT bridged via ``PIPELINE_*`` env.
# Parsed raw — no eager raise. Missing minimum just means "not usable"; the
# engagement helpers (``eyes_engaged`` / ``eyes_new_runner_eligible``) test
# "minimum fields present" without raising. Full structural validation
# (``validate_ui_config``) runs lazily inside the runner / ``_run_eyes`` on
# the SELECTED config, immediately before browser launch.
UI_CONFIG: dict = _yaml_root.get("ui") or {}
if not isinstance(UI_CONFIG, dict):
    UI_CONFIG = {}

# Default viewport when ``ui.viewport`` absent.
EYES_DEFAULT_VIEWPORT = (1280, 720)

# Bounded same-origin discovery caps (when ``ui.screens`` absent).
EYES_DISCOVERY_MAX_PAGES = 12
EYES_DISCOVERY_MAX_DEPTH = 2
EYES_DISCOVERY_TIMEOUT_S = 90
EYES_DISCOVERY_MAX_LINKS = 40

# Ready-healthcheck polling.
EYES_READY_POLL_INTERVAL_MS = 500


def _eyes_config_minimum(cfg: dict) -> bool:
    """True when ``cfg`` has the minimum fields for a usable ``ui:`` config.

    Minimum: ``type`` + (``url`` OR (``start`` AND ``ready``)). ``screens`` is
    optional. No raise — a missing minimum just means "not usable" so the
    brief §3 fallback chain proceeds to checkpointed state, then interrupt.
    """
    if not isinstance(cfg, dict) or not cfg:
        return False
    if not str(cfg.get("type") or "").strip():
        return False
    if str(cfg.get("url") or "").strip():
        return True
    if str(cfg.get("start") or "").strip() and str(cfg.get("ready") or "").strip():
        return True
    return False


def eyes_engaged(state) -> bool:
    """The *engagement gate*: should the visual phase run at all?

    True for usable ``ui:`` yaml, checkpointed ``ui_config`` (minimum fields),
    legacy ``PIPELINE_UX_RENDER_CMD`` non-empty (engages regardless of
    ``has_ui``, per README §"Il cancello visivo"), or an explicit ``./run.py
    eyes`` CLI flag (``state["eyes_engaged"]`` set by the CLI leg). False for
    ``has_ui``-alone or ``PIPELINE_RENDER_CMD``-only. A valid ``ui:`` yaml
    engages even when ``UI_SURFACE_RE`` left ``has_ui=False``. Consulted by
    ``route_after_tech``, ``route_next_batch``, ``route_escalation_return``,
    ``debate_tech``, ``_debate_decision`` — replacing the bare
    ``C.UX_RENDER_CMD.strip()`` checks.
    """
    if not isinstance(state, dict):
        state = {}
    # Explicit CLI flag (set by _run_eyes / the eyes subparser).
    if state.get("eyes_engaged"):
        return True
    # Usable ui: yaml (checked first — yaml wins over state/env; engages even
    # when has_ui=False, per the brief).
    if _eyes_config_minimum(UI_CONFIG):
        return True
    # Checkpointed ui_config on this task's PipelineState.
    if _eyes_config_minimum(state.get("ui_config") or {}):
        return True
    # Legacy PIPELINE_UX_RENDER_CMD non-empty — engages regardless of has_ui
    # (per README: any non-empty UX_RENDER_CMD engages eyes).
    if UX_RENDER_CMD.strip():
        return True
    return False


def eyes_new_runner_eligible(state) -> bool:
    """The *dispatch discriminator* inside ``ux_render``: True ONLY when usable
    ``ui:`` yaml OR checkpointed ``state.ui_config`` minimum is present — NOT
    legacy env alone. Yaml wins over state. ``ux_render`` branches:
    ``if not eyes_engaged: skip; elif eyes_new_runner_eligible: new-runner;
    else: compat-subprocess``.
    """
    if not isinstance(state, dict):
        state = {}
    if _eyes_config_minimum(UI_CONFIG):
        return True
    if _eyes_config_minimum(state.get("ui_config") or {}):
        return True
    return False


def resolved_eyes_gate_mode(state) -> str:
    """The gate mode for the eyes runner. Defaults to ``resolved_gate_mode``;
    a CLI override (``--gate``/``--mode`` via ``state["eyes_gate_mode"]``) wins
    when set to ``off``/``standard``/``full``.
    """
    if not isinstance(state, dict):
        state = {}
    override = str(state.get("eyes_gate_mode") or "").strip()
    if override in _EFFORT_GATE_MODES:
        return override
    return resolved_gate_mode(state)


def eyes_config_pause_path(task_id: str) -> Path:
    """Side file marking an eyes config pause (mirrors
    ``pending_answer.pending_answer_path``). Written by ``_run_eyes`` when it
    pauses for minimum fields; checked by the ``resume`` preflight BEFORE
    ``snap.interrupts`` so a config-pause dispatches to ``_run_eyes`` instead
    of ``_drive``.
    """
    return METRICS / f"eyes-config-pause-{task_id}.json"


# --- Eyes config validation (lazy, full structural) ------------------------
# Called by the runner / ``_run_eyes`` on the SELECTED config immediately
# before browser launch. Missing minimum is NOT a raise here (it's handled by
# the engagement helpers as "not usable"). Unknown keys / invalid ``type`` /
# ``wait_for.state`` / ``press.key`` / action → ``ValueError``. ``ui.type:
# auto``→``web``; ``electron``→error.

_EYES_ACTION_ALLOWLIST = frozenset({
    "goto", "click", "fill", "select", "press", "hover",
    "scroll", "wait_for", "wait_ms", "screenshot",
})
_EYES_WAIT_FOR_STATES = frozenset({
    "visible", "hidden", "attached", "detached",
})
_EYES_PRESS_KEYS = frozenset({
    "Enter", "Tab", "Escape", "Backspace", "Delete",
    "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
    "Home", "End", "PageUp", "PageDown", "Space",
})
_EYES_SCREEN_KEYS = frozenset({"name", "actions"})
_EYES_AUTH_HOOK_KEYS = frozenset({"seed_script", "require_e2e_db"})
_EYES_UI_KEYS = frozenset({
    "type", "url", "start", "ready", "ready_timeout_s", "cwd",
    "viewport", "manifesto", "screens", "auth_hooks",
})
_EYES_ACTION_REQUIRED: dict[str, tuple[str, ...]] = {
    "goto": ("url",),
    "click": ("selector",),
    "fill": ("selector", "text"),
    "select": ("selector", "value"),
    "press": ("selector", "key"),
    "hover": ("selector",),
    "scroll": ("selector",),
    "wait_for": ("selector",),
    "wait_ms": ("ms",),
    "screenshot": ("name",),
}
_EYES_ACTION_OPTIONAL: dict[str, frozenset[str]] = {
    "goto": frozenset(),
    "click": frozenset({"timeout_ms"}),
    "fill": frozenset(),
    "select": frozenset(),
    "press": frozenset(),
    "hover": frozenset(),
    "scroll": frozenset({"x", "y"}),
    "wait_for": frozenset({"state", "timeout_ms"}),
    "wait_ms": frozenset(),
    "screenshot": frozenset({"full_page"}),
}


def validate_ui_config(cfg: dict) -> dict:
    """Full structural validation of a selected ``ui:`` config.

    Raises ``ValueError`` on unknown keys, invalid ``type`` / ``wait_for.state``
    / ``press.key`` / action, ``electron``→error. Normalizes ``type: auto``→
    ``web`` in the returned dict. Called LAZILY by the runner / ``_run_eyes``
    on the selected config before browser launch, NOT at module import.
    Missing minimum is NOT a raise here (handled by engagement helpers).
    """
    if not isinstance(cfg, dict):
        raise ValueError("ui: config must be a mapping")
    out = dict(cfg)
    # Unknown top-level keys.
    unknown = set(out.keys()) - _EYES_UI_KEYS
    if unknown:
        raise ValueError(f"ui: unknown keys: {sorted(unknown)}")
    # type resolution: auto→web; electron→error; web stays.
    ui_type = str(out.get("type") or "").strip()
    if ui_type == "auto":
        out["type"] = "web"
    elif ui_type == "electron":
        raise ValueError(
            "ui.type: electron is not supported in v1 (deferred). "
            "Use type: web (or auto, which resolves to web).")
    elif ui_type not in ("web",):
        raise ValueError(
            f"ui.type: {ui_type!r} is not valid (expected web, auto, or electron)")
    # cwd escape check (relative to REPO unless absolute; must stay inside).
    cwd = str(out.get("cwd") or "").strip()
    if cwd:
        cwd_path = (REPO / cwd).resolve() if not Path(cwd).is_absolute() else Path(cwd).resolve()
        if not cwd_path.is_relative_to(REPO):
            raise ValueError(f"ui.cwd: {cwd!r} resolves outside repo {REPO}")
    # viewport shape.
    vp = out.get("viewport")
    if vp is not None:
        if not isinstance(vp, dict) or "width" not in vp or "height" not in vp:
            raise ValueError("ui.viewport must be a mapping with width and height")
    # auth_hooks allowlist.
    ah = out.get("auth_hooks")
    if ah is not None:
        if not isinstance(ah, dict):
            raise ValueError("ui.auth_hooks must be a mapping")
        unknown_ah = set(ah.keys()) - _EYES_AUTH_HOOK_KEYS
        if unknown_ah:
            raise ValueError(
                f"ui.auth_hooks: unknown keys {sorted(unknown_ah)} "
                f"(allowlist: {sorted(_EYES_AUTH_HOOK_KEYS)})")
    # screens validation.
    screens = out.get("screens")
    if screens is not None:
        if not isinstance(screens, list) or not screens:
            raise ValueError("ui.screens must be a non-empty list")
        for i, screen in enumerate(screens):
            if not isinstance(screen, dict):
                raise ValueError(f"ui.screens[{i}]: not a mapping")
            unknown_s = set(screen.keys()) - _EYES_SCREEN_KEYS
            if unknown_s:
                raise ValueError(f"ui.screens[{i}]: unknown keys {sorted(unknown_s)}")
            if not str(screen.get("name") or "").strip():
                raise ValueError(f"ui.screens[{i}]: missing 'name'")
            actions = screen.get("actions")
            if not isinstance(actions, list) or not actions:
                raise ValueError(f"ui.screens[{i}]: 'actions' must be a non-empty list")
            for j, act in enumerate(actions):
                if not isinstance(act, dict):
                    raise ValueError(f"ui.screens[{i}].actions[{j}]: not a mapping")
                action = str(act.get("action") or "").strip()
                if action not in _EYES_ACTION_ALLOWLIST:
                    raise ValueError(
                        f"ui.screens[{i}].actions[{j}]: unknown action {action!r} "
                        f"(allowlist: {sorted(_EYES_ACTION_ALLOWLIST)})")
                required = _EYES_ACTION_REQUIRED[action]
                for req in required:
                    if req not in act or str(act.get(req) or "").strip() == "":
                        raise ValueError(
                            f"ui.screens[{i}].actions[{j}] ({action}): "
                            f"missing required param {req!r}")
                allowed_keys = frozenset({"action"}) | set(required) | _EYES_ACTION_OPTIONAL[action]
                unknown_a = set(act.keys()) - allowed_keys
                if unknown_a:
                    raise ValueError(
                        f"ui.screens[{i}].actions[{j}] ({action}): "
                        f"unknown params {sorted(unknown_a)}")
                if action == "wait_for":
                    state_val = str(act.get("state") or "visible").strip()
                    if state_val not in _EYES_WAIT_FOR_STATES:
                        raise ValueError(
                            f"ui.screens[{i}].actions[{j}] (wait_for): "
                            f"unknown state {state_val!r} "
                            f"(expected one of {sorted(_EYES_WAIT_FOR_STATES)})")
                if action == "press":
                    key = str(act.get("key") or "").strip()
                    if key not in _EYES_PRESS_KEYS:
                        raise ValueError(
                            f"ui.screens[{i}].actions[{j}] (press): "
                            f"unknown key {key!r} "
                            f"(expected one of {sorted(_EYES_PRESS_KEYS)})")
    return out

# --- Render gate (the perf analog of the visual gate) ----------------------
# Drives a scripted interaction and counts re-renders per instrumented subtree
# (window.__RENDER_LOG__, fed by <Profiler> hooks) into render-facts.json, then
# compares to a baseline to catch re-render REGRESSIONS. Deterministic — the
# review is numeric, no LLM critic. Reuses the ux-render seed fixtures.
RENDERS = DOCS / "reviews" / "renders"
RENDER_CMD = os.environ.get("PIPELINE_RENDER_CMD", "")
RENDER_CWD = os.environ.get("PIPELINE_RENDER_CWD", "")
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
E2E_DB_CONTAINER = os.environ.get("PIPELINE_E2E_DB_CONTAINER", "")
E2E_PROJECT = os.environ.get("PIPELINE_E2E_PROJECT", "")
# None when unset — MonkeForge is standalone and ships no e2e-up script by
# default; opt in via PIPELINE_E2E_UP_SCRIPT (repo-relative or absolute path).
_e2e_up_raw = os.environ.get("PIPELINE_E2E_UP_SCRIPT", "").strip()
E2E_UP_SCRIPT: Path | None = (Path(_e2e_up_raw) if _e2e_up_raw else None)
E2E_UP_TIMEOUT = int(os.environ.get("PIPELINE_E2E_UP_TIMEOUT", "660"))

# Host-side URL for vitest when the e2e Postgres container maps 5433→5432.
# Override with PIPELINE_E2E_DATABASE_URL; never read backend/.env implicitly.
E2E_DATABASE_URL = os.environ.get("PIPELINE_E2E_DATABASE_URL", "")
# When the URL is unset/empty BUT the port was set EXPLICITLY by the operator
# (presence of PIPELINE_E2E_DB_PORT in os.environ — not the 5433 fallback),
# derive the URL from that port so db_reachable() (probes E2E_DB_PORT) and the
# suites (connect via E2E_DATABASE_URL) stay on the same host port. Both env
# vars unset → stays "" (preserves the standalone default). Partial inheritance
# (port set, URL unset) is the wt-run child-env case this fixes.
if not E2E_DATABASE_URL and "PIPELINE_E2E_DB_PORT" in os.environ:
    E2E_DATABASE_URL = (
        f"postgresql://postgres:postgrespassword@localhost:{E2E_DB_PORT}"
        f"/yourdb?schema=public"
    )

# Notify daemon (persistent rate-limited notification dispatcher).
NOTIFY_RATE = int(os.environ.get("PIPELINE_NOTIFY_RATE", "30"))
NOTIFY_WINDOW = int(os.environ.get("PIPELINE_NOTIFY_WINDOW", "60"))
NOTIFY_SOCKET = Path(os.environ.get("PIPELINE_NOTIFY_SOCKET")
                     or (METRICS / "notify.sock"))

# --- Test-gate tuning (false-positive suppression) -------------------------
# Two mechanisms that stop pre-existing debt from looking like a batch
# regression. Both are ;-separated lists, overridable via env or YAML.
#
# LINT_DEBT_RULES: eslint rule IDs whose violations are "known debt". If the
#   SAME rule was present in the baseline (in any file), new occurrences of
#   that rule in other files are NOT counted as new failures. This covers two
#   false positives observed on TASK-016:
#     (a) a rule that was invisible in baseline because type errors disabled
#         type-aware analysis, then "appears" after the batch fixes the types;
#     (b) debt relocated verbatim from a deleted file to a new file not in
#         baseline.
#   A rule NOT in baseline in any file is still a real regression.
#
# TEST_AMBIENT_PATTERNS: substrings of vitest FAIL keys that are
#   environment-sensitive (DB-gated, network, external services). A failure
#   whose key contains any of these patterns is NOT counted as new — it is
#   treated as ambient noise (e.g. a describe.skipIf(!hasDb) test that was
#   skipped in baseline but runs and fails when the DB comes up mid-batch).
_DEFAULT_LINT_DEBT_RULES = ""
LINT_DEBT_RULES = tuple(
    r.strip() for r in
    os.environ.get("PIPELINE_LINT_DEBT_RULES", _DEFAULT_LINT_DEBT_RULES).split(";")
    if r.strip()
)

_DEFAULT_TEST_AMBIENT_PATTERNS = ""
TEST_AMBIENT_PATTERNS = tuple(
    p.strip() for p in
    os.environ.get("PIPELINE_TEST_AMBIENT_PATTERNS", _DEFAULT_TEST_AMBIENT_PATTERNS).split(";")
    if p.strip()
)

# --- Test suites (repo-agnostic test gate) ---------------------------------
# Read directly from the top-level `test_suites:` key in monkeforge.yaml
# (mirroring `_yaml_agents`), NOT via the PIPELINE_* env bridge — run.py's
# `_load_yaml_to_env` deliberately does not bridge this key. The legacy
# `PIPELINE_TEST_SUITES` env var remains as a debug-only override of the
# `label:subdir:ENV=val` shape (parsed into npm-vitest TestSuite objects).
# An absent yaml key AND an absent/empty env var both yield TEST_SUITES == []
# (no hardcoded backend/frontend default) — empty triggers discovery in
# test_runner.resolve_test_suites, it does NOT silently disable the gate.
TEST_SUITE_RUNNERS = frozenset({"npm-vitest", "pytest", "script"})


@dataclass(frozen=True)
class TestSuite:
    """One runnable test suite for the in-graph test gate.

    ``env`` is a per-suite OVERRIDE map applied on top of ``os.environ.copy()``
    by each runner — NOT a full env replacement. ``cmd`` is required for the
    ``script`` runner and ignored by the others. ``cwd`` is repo-relative and
    must resolve inside ``C.REPO`` (validated at load time).
    """

    label: str
    cwd: str
    runner: str
    cmd: list[str] | None = None
    env: dict[str, str] | None = None


def _validate_test_suite(label: str, entry: dict) -> TestSuite:
    """Validate one yaml ``test_suites`` entry and build a TestSuite.

    Raises ValueError on an invalid entry (unknown runner, ``script`` without
    ``cmd``, or a ``cwd`` that resolves outside ``C.REPO``) so a misconfigured
    file fails loudly at config load instead of silently skipping the suite.
    """
    if not isinstance(entry, dict):
        raise ValueError(f"test_suites entry {label!r}: not a mapping")
    runner = str(entry.get("runner") or "").strip()
    if runner not in TEST_SUITE_RUNNERS:
        raise ValueError(
            f"test_suites entry {label!r}: unknown runner {runner!r} "
            f"(expected one of {sorted(TEST_SUITE_RUNNERS)})")
    cwd = str(entry.get("cwd") or "").strip()
    cmd = entry.get("cmd")
    if runner == "script":
        if not cmd or (isinstance(cmd, list) and not any(str(c).strip() for c in cmd)):
            raise ValueError(
                f"test_suites entry {label!r}: runner 'script' requires a non-empty 'cmd'")
    if isinstance(cmd, list):
        cmd = [str(c) for c in cmd]
    elif cmd is not None:
        cmd = [str(cmd)]
    env = entry.get("env") or {}
    if not isinstance(env, dict):
        raise ValueError(f"test_suites entry {label!r}: env must be a mapping")
    env = {
        str(k): (str(v).format(e2e_db=E2E_DATABASE_URL) if "{e2e_db}" in str(v) else str(v))
        for k, v in env.items()
    }
    # cwd must resolve inside the repo (guard against escape/misconfiguration).
    cwd_path = (REPO / cwd).resolve() if cwd else REPO
    if not cwd_path.is_relative_to(REPO):
        raise ValueError(
            f"test_suites entry {label!r}: cwd {cwd!r} resolves outside repo {REPO}")
    return TestSuite(label=label, cwd=cwd, runner=runner, cmd=cmd, env=env)


def _parse_legacy_test_suites(raw: str) -> list[TestSuite]:
    """Parse the legacy ``PIPELINE_TEST_SUITES`` env string into TestSuites.

    Format: ``label:subdir:ENV_VAR=val,ENV2=val2;label2:subdir2:``. Each entry
    becomes a TestSuite with ``runner='npm-vitest'``. ``{e2e_db}`` interpolates
    ``E2E_DATABASE_URL``. An empty/whitespace string yields ``[]``.
    """
    suites: list[TestSuite] = []
    if not raw or not raw.strip():
        return suites
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":", 2)
        label = parts[0]
        subdir = parts[1] if len(parts) > 1 else ""
        env_str = parts[2] if len(parts) > 2 else ""
        env: dict[str, str] = {}
        for pair in env_str.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                env[k.strip()] = v.format(e2e_db=E2E_DATABASE_URL)
        suites.append(TestSuite(label=label, cwd=subdir, runner="npm-vitest",
                                cmd=None, env=env))
    return suites


def _load_yaml_test_suites(yaml_file: Path) -> list[TestSuite]:
    """Load + validate the top-level ``test_suites:`` key from a yaml file.

    Returns TestSuites for a valid list. An absent key or missing file yields
    ``[]`` (discovery). A present key that is not a list, or an invalid entry,
    raises ValueError so a misconfigured file fails loudly at config load
    instead of silently falling back to discovery.
    """
    if not yaml_file.exists():
        return []
    import yaml as _yaml_ts
    raw = (_yaml_ts.safe_load(yaml_file.read_text()) or {}).get("test_suites")
    # An absent key (None) → discovery. A present key MUST be a list; any other
    # top-level shape (scalar/mapping) is a misconfiguration that fails loudly
    # instead of silently falling back to discovery.
    if raw is not None and not isinstance(raw, list):
        raise ValueError(f"test_suites must be a list, got {type(raw).__name__}")
    suites: list[TestSuite] = []
    for _entry in (raw or []):
        if isinstance(_entry, dict):
            _lbl = str(_entry.get("label") or "").strip()
            if not _lbl:
                raise ValueError("test_suites entry: missing 'label'")
            suites.append(_validate_test_suite(_lbl, _entry))
        else:
            raise ValueError(f"test_suites entry: not a mapping ({_entry!r})")
    return suites


TEST_SUITES: list[TestSuite] = []
_legacy_test_suites_raw = os.environ.get("PIPELINE_TEST_SUITES", "")
if _legacy_test_suites_raw.strip():
    # Debug-only override: env wins over yaml, parsed into npm-vitest suites.
    TEST_SUITES = _parse_legacy_test_suites(_legacy_test_suites_raw)
else:
    TEST_SUITES = _load_yaml_test_suites(_yaml_file)
# Absent yaml key AND absent env → TEST_SUITES stays [] (discovery, not gate-off).

def db_reachable() -> bool:
    """Check if the e2e Postgres is accepting connections on its mapped port."""
    import socket
    try:
        with socket.create_connection(("localhost", E2E_DB_PORT), timeout=2):
            return True
    except OSError:
        return False


def e2e_required() -> bool:
    """True when this product actually expects an e2e Postgres for the test gate.

    Isolation may still inject ``PIPELINE_E2E_DB_PORT`` / URL into the child
    env for products that use them; that alone must NOT skip the whole suite
    on standalone hosts (e.g. MonkeForge pytest-only with no ``e2e-up`` script).
    """
    return E2E_UP_SCRIPT is not None and E2E_UP_SCRIPT.exists()


def ensure_e2e_stack() -> bool:
    """Bring the e2e stack up via scripts/e2e-up.sh. Returns True if the DB answers.

    The script's default path is idempotent (`up -d` + wait-on, no `down -v`,
    no `--build`), so calling it as an inline precondition is cheap when the
    stack is already running. The destructive rebuild is opt-in (`--fresh`) and
    is never what the pipeline wants.
    """
    if db_reachable():
        return True
    if E2E_UP_SCRIPT is None:
        return db_reachable()
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
