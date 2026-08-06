"""Repo-agnostic test-suite gate for the implement/finalize gates — not LLM stdout.

Each suite is a ``config.TestSuite`` dispatched through a runner registry
(``_RUNNERS``). Runners parse stable failure keys prefixed ``label|`` and
append a synthetic ``label|<runner> exit N`` key when the subprocess exits
non-zero with zero parsed failures — so a crash (bad Node version, corrupted
``node_modules``, tool config error) can never silently yield a green gate.
``resolve_test_suites`` discovers candidates and (on a TTY) asks the user,
persisting the choice to ``monkeforge.yaml``; a module-level sentinel ensures
discovery/ask runs at most once per process.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from . import config as C
from . import events as ev

FAIL_LINE_RE = re.compile(r"^\s*FAIL\s+(.+)$", re.MULTILINE)
SUMMARY_FAILED_PASSED_RE = re.compile(
    r"Tests\s+(\d+)\s+failed\s*\|\s*(\d+)\s+passed",
    re.IGNORECASE,
)
SUMMARY_PASSED_ONLY_RE = re.compile(
    r"Tests\s+(\d+)\s+passed(?:\s*\|\s*(\d+)\s+skipped)?",
    re.IGNORECASE,
)
TSC_ERROR_RE = re.compile(r"^(.+?)\((\d+),(\d+)\):\s*error\s+(TS\d+):\s*(.+)$", re.MULTILINE)
# pytest ``FAILED <nodeid> - <message>`` lines. ``re.MULTILINE`` is mandatory:
# without it ``^``/``$`` anchor only to the start/end of the entire captured
# string, so ``findall``/``finditer`` against real multi-line pytest stdout
# would match zero ``FAILED`` lines in the common case — silently degrading
# every pytest failure into only the synthetic exit key (masking which test
# failed). Mirrors ``FAIL_LINE_RE`` above, which uses the same flag for the
# identical whole-output-string-anchored-per-line pattern shape.
PYTEST_FAIL_RE = re.compile(r"^FAILED\s+(.+?)(?:\s+-\s+.+)?$", re.MULTILINE)

# Truncation tail length for synthetic exit-key summaries (keeps the journal
# readable while still surfacing the relevant subprocess output tail).
_TAIL_LIMIT = 500

# --- <test_summary> block formatter + sanitizer ----------------------------
# The block is injected into final_check/preflight_fix/implement/code_fix
# prompts so the agent sees the in-graph gate's authoritative result instead
# of re-running tests blind. Three sanitizer rules (applied to every
# interpolated field — raw, each new_failure key, each suite label) close the
# injection vector a partial sanitizer would re-open:
#   1. escape `</` → `<\/` so a stray `</...` in raw output can't close the
#      wrapping block early (an escaped `</test_summary>` survives as
#      `<\/test_summary>` — visible but inert);
#   2. strip un-escaped `<test_summary>`/`</test_summary>` tokens
#      (case-insensitive) so a stale opening token in interpolated content
#      cannot nest a second block (escaped closing tokens are left intact,
#      already rendered inert by step 1);
#   3. cap at 600 chars with an ellipsis marker so a runaway log line can't
#      blow out the prompt.
_SUMMARY_BLOCK_CHAR_CAP = 600
_SUMMARY_OMIT = "…[truncated]"


def _sanitize_summary_text(text: str) -> str:
    """Apply the three sanitizer rules to one interpolated summary field."""
    s = text or ""
    # 1. escape `</` so interpolated content cannot close the block early.
    s = s.replace("</", "<\\/")
    # 2. strip un-escaped <test_summary>/</test_summary> tokens (the escaped
    #    variants from step 1 no longer match the regex, so they survive inert).
    s = re.sub(r"</?test_summary\s*>", "", s, flags=re.IGNORECASE)
    # 3. 600-char cap with ellipsis marker.
    if len(s) > _SUMMARY_BLOCK_CHAR_CAP:
        s = s[:_SUMMARY_BLOCK_CHAR_CAP] + _SUMMARY_OMIT
    return s


def format_test_summary_block(
    status: str,
    suites: list[str],
    new_failures: list[str],
    raw: str,
    authoritative: bool = True,
) -> str:
    """Render an XML-ish ``<test_summary ...>`` block for prompt injection.

    ``status`` is the gate's verdict (``green``/``red``/``skipped``/
    ``unconfigured``). ``suites`` are the resolved suite labels, ``new_failures``
    the baseline-aware new failure keys, ``raw`` the one-line runner summary.
    ``authoritative`` flags whether the block is ground truth (a measured run)
    vs. a placeholder for an unconfigured/skipped gate.

    Every interpolated field is run through ``_sanitize_summary_text`` so a
    malicious or noisy test output line can never forge a closing tag, close
    the block early, or blow out the prompt. The ``suites``/``new_failures``/
    ``raw`` lines are always emitted (with ``(none)`` placeholders when empty)
    so the block shape is stable for prompt-contract grep assertions.
    """
    auth = "true" if authoritative else "false"
    safe_suites = [_sanitize_summary_text(s) for s in (suites or [])]
    safe_failures = [_sanitize_summary_text(f) for f in (new_failures or [])]
    safe_raw = _sanitize_summary_text(raw or "")
    safe_status = _sanitize_summary_text(status)
    lines = [
        f'<test_summary status="{safe_status}" authoritative="{auth}">',
        f"suites: {', '.join(safe_suites) if safe_suites else '(none)'}",
    ]
    if safe_failures:
        lines.append("new_failures:")
        for f in safe_failures:
            lines.append(f"  - {f}")
    else:
        lines.append("new_failures: (none)")
    lines.append(f"raw: {safe_raw}")
    lines.append("</test_summary>")
    return "\n".join(lines)


# Makefile target discovery: matches `test:` or `check:` at the start of a
# line (allowing optional whitespace before the colon). ``re.MULTILINE`` so
# ``^`` anchors per-line against the whole Makefile body.
_MAKE_TARGET_RE = re.compile(r"^(test|check)\s*:", re.MULTILINE)


def _makefile_targets(path: Path) -> set[str]:
    """Return the set of ``test``/``check`` targets defined in a Makefile.

    Returns an empty set when the file is missing or unreadable. Used by
    ``discover_test_suites`` to decide whether a Makefile yields a
    ``script``-runner candidate (``make test`` preferred, ``make check``
    fallback).
    """
    try:
        body = path.read_text()
    except OSError:
        return set()
    return set(_MAKE_TARGET_RE.findall(body))


def parse_vitest_failures(output: str) -> set[str]:
    """Stable keys: everything after ``FAIL`` on each vitest result line."""
    return {line.strip() for line in FAIL_LINE_RE.findall(output or "") if line.strip()}


def parse_tsc_errors(output: str) -> set[str]:
    """Stable keys: file(line,col): error TSxxxx: message."""
    return {
        f"TYPE_ERROR|{m.group(1)}:{m.group(2)}:{m.group(3)} — {m.group(4)}: {m.group(5)}"
        for m in TSC_ERROR_RE.finditer(output or "")
    }


def parse_eslint_errors(output: str) -> set[str]:
    """Parse `eslint --format json`. Counts BOTH errors and warnings (severity
    1 or 2) — a new warning is a regression too. Key is file|rule|message with NO
    line number, so a pre-existing violation that a batch shifted down the file
    still matches its baseline entry instead of looking new. Degrades to empty on
    unparseable output (never invents failures)."""
    try:
        data = json.loads(output[output.index("["):])
    except (ValueError, json.JSONDecodeError):
        return set()
    keys: set[str] = set()
    for f in data if isinstance(data, list) else []:
        fp = f.get("filePath", "")
        for m in f.get("messages", []):
            if m.get("severity", 0) >= 1:
                keys.add(f"LINT|{fp}|{m.get('ruleId') or 'syntax'}|{m.get('message', '')}")
    return keys


def parse_pytest_failures(output: str) -> set[str]:
    """Stable keys: the pytest nodeid of each ``FAILED`` line."""
    return {m.group(1).strip() for m in PYTEST_FAIL_RE.finditer(output or "")}


def is_allowlisted(failure_key: str, allowlist: list[str]) -> bool:
    """Match if any allowlist entry is a substring of the failure key (or exact)."""
    for entry in allowlist:
        pat = (entry or "").strip()
        if not pat:
            continue
        if pat in failure_key or failure_key.startswith(pat):
            return True
    return False


# LINT failure keys look like "label|LINT|/path/file.ts|ruleId|message".
# The rule segment is the 4th |-delimited field (index 3 within the label-prefixed
# key, i.e. index 2 after stripping the leading "label|LINT|").
_LINT_RULE_RE = re.compile(r"\|LINT\|[^|]*\|([^|]+)\|")


def _baseline_lint_rules(baseline: set[str]) -> set[str]:
    """Extract the set of eslint rule IDs present in the baseline (any file)."""
    rules: set[str] = set()
    for key in baseline:
        m = _LINT_RULE_RE.search(key)
        if m:
            rules.add(m.group(1))
    return rules


def _is_ambient_test_failure(failure_key: str, ambient_patterns: tuple[str, ...]) -> bool:
    """True if a vitest FAIL key matches a known ambient-sensitive test pattern."""
    for pat in ambient_patterns:
        if pat and pat in failure_key:
            return True
    return False


def new_failures_since_baseline(
    current: set[str],
    baseline: set[str],
    allowlist: list[str],
    lint_debt_rules: tuple[str, ...] = (),
    ambient_patterns: tuple[str, ...] = (),
) -> set[str]:
    """Failures in ``current`` not in ``baseline``, minus allowlisted entries.

    Two false-positive suppressions (TASK-016):
      * ``lint_debt_rules``: a LINT failure whose rule was already present in
        baseline (in any file) is not new — it is pre-existing debt that
        surfaced or was relocated. A rule NOT in baseline is still a regression.
      * ``ambient_patterns``: a vitest failure whose key contains a known
        ambient-sensitive pattern (DB-gated, network) is not new — it was
        skipped in baseline (env down) and fails when the env comes up.
    """
    raw = current - baseline
    debt_rules = _baseline_lint_rules(baseline) if lint_debt_rules else set()
    result: set[str] = set()
    for f in raw:
        if is_allowlisted(f, allowlist):
            continue
        # LINT debt: rule already in baseline → not a regression.
        if lint_debt_rules:
            m = _LINT_RULE_RE.search(f)
            if m and m.group(1) in debt_rules and m.group(1) in lint_debt_rules:
                continue
        # Ambient test: DB/network-gated failure → not a batch regression.
        if ambient_patterns and _is_ambient_test_failure(f, ambient_patterns):
            continue
        result.add(f)
    return result


def _summarize_vitest_output(combined: str, exit_code: int, n_fail_lines: int) -> str:
    m = SUMMARY_FAILED_PASSED_RE.search(combined)
    if m:
        return f"{m.group(2)} passed, {m.group(1)} failed"
    m = SUMMARY_PASSED_ONLY_RE.search(combined)
    if m:
        skipped = m.group(2)
        base = f"{m.group(1)} passed, 0 failed"
        return f"{base}, {skipped} skipped" if skipped else base
    return f"exit {exit_code}, {n_fail_lines} FAIL lines parsed"


def _run_cmd(cmd: list[str], cwd: Path, env: dict[str, str], timeout: int) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"runner error: {exc}"
    return proc.returncode, (proc.stdout or "") + "\n" + (proc.stderr or "")


def _suite_env(suite: "C.TestSuite") -> dict[str, str]:
    """Env for a suite subprocess: orchestrator yaml, then suite.env overrides.

    Self-host gates run pytest inside ``wt-task-*`` where ``monkeforge.yaml``
    is intentionally absent (gitignored operator config). Point
    ``PIPELINE_WT_YAML`` at the orchestrator's yaml so ``config.py`` import
    in the product tree still resolves agents — without writing into the wt.
    """
    env = os.environ.copy()
    orch = (os.environ.get("PIPELINE_WT_YAML") or "").strip() or str(
        C.MF_ROOT / "monkeforge.yaml"
    )
    orch_path = Path(orch).expanduser()
    if orch_path.exists():
        env.setdefault("PIPELINE_WT_YAML", str(orch_path.resolve()))
    env.update(suite.env or {})
    return env


def _tail(text: str, limit: int = _TAIL_LIMIT) -> str:
    """Human-readable snippet of runner output for synthetic exit summaries.

    Prefer an ``error:`` banner when present (e.g. AgentsConfigError). Strip
    pytest's ``E``-prefixed exception dump lines and collapse whitespace so
    the pause panel does not show a wall of ``E`` / mid-word truncations.
    """
    text = text or ""
    m = re.search(r"(?m)^error:\s+.+", text)
    chunk = text[m.start():] if m else text
    lines: list[str] = []
    for line in chunk.splitlines():
        s = line.strip()
        # pytest formats exception details as "E   message"
        if re.match(r"^E(\s|$)", s) and not s.startswith("error:"):
            s = re.sub(r"^E\s*", "", s).strip()
        if s:
            lines.append(s)
    flat = re.sub(r"\s+", " ", " ".join(lines)).strip()
    if len(flat) <= limit:
        return flat
    # Head is actionable for error: banners; otherwise keep the log tail.
    if flat.startswith("error:"):
        return flat[: max(0, limit - 1)] + "…"
    return "…" + flat[-(limit - 1):]


# --- Runner registry -------------------------------------------------------
# Each runner returns (exit_code, failures, summary). ``failures`` is a set of
# ``label|<key>`` strings. Every runner guards the exit-code-can't-yield-green
# invariant: a non-zero exit with zero parsed failures appends a synthetic
# ``label|<runner> exit N`` key so the gate cannot silently pass a crash.


def _run_npm_vitest(suite: C.TestSuite, timeout: int) -> tuple[int, set[str], str]:
    """Run tsc --noEmit then vitest (then lint). Former ``_run_suite`` body.

    Reads ``suite.label``/``suite.cwd``/``suite.env``. The exit-code guard
    counts typecheck + vitest failures only — lint intentionally ignores its
    exit code (eslint returns non-zero on warnings in some configs), per the
    existing ``_run_suite`` comment.
    """
    label = suite.label
    cwd = C.REPO / suite.cwd if suite.cwd else C.REPO
    env = _suite_env(suite)
    failures: set[str] = set()
    parts: list[str] = []

    # Phase 1: type check
    tc_code, tc_out = _run_cmd(["npm", "run", "typecheck"], cwd, env, timeout)
    tc_fails: set[str] = set()
    if tc_code != 0:
        tc_fails = parse_tsc_errors(tc_out)
        failures |= {f"{label}|{f}" for f in tc_fails}
        n_errs = len(tc_fails)
        parts.append(f"{n_errs} type error(s)")
    else:
        parts.append("typecheck ok")

    # Phase 2: vitest (always run, even if tsc failed — to get full picture)
    vt_code, vt_out = _run_cmd(["npm", "test"], cwd, env, timeout)
    vt_fails: set[str] = set()
    if vt_out.startswith("runner error:"):
        parts.append(vt_out)
    else:
        vt_fails = parse_vitest_failures(vt_out)
        failures |= {f"{label}|{line}" for line in vt_fails}
        parts.append(_summarize_vitest_output(vt_out, vt_code, len(vt_fails)))

    # Exit-code guard: a non-zero exit (crash, bad Node, corrupted node_modules,
    # tool config error, or the runner-error branch) with zero parsed
    # typecheck+vitest failures must NOT silently yield a green gate.
    combined_code = max(tc_code, vt_code)
    if combined_code != 0 and len(tc_fails) + len(vt_fails) == 0:
        synth = f"{label}|npm exit {combined_code}"
        failures.add(synth)
        tail_src = tc_out if tc_code != 0 else vt_out
        parts.append(f"synthetic: {synth} [{_tail(tail_src)}]")

    # Phase 3: lint. eslint exits 0 on warnings-only, so we read the parsed
    # output, not the exit code. Both errors and warnings feed the failure set,
    # which the baseline comparison then reduces to NEW-only. A suite without a
    # `lint` script is skipped, not failed.
    ln_code, ln_out = _run_cmd(["npm", "run", "lint", "--", "--format", "json"],
                               cwd, env, timeout)
    low = ln_out.lower()
    if ln_out.startswith("runner error:") or "missing script" in low:
        parts.append("lint: n/a")
    else:
        ln_fails = parse_eslint_errors(ln_out)
        failures |= {f"{label}|{k}" for k in ln_fails}
        parts.append(f"{len(ln_fails)} lint issue(s)" if ln_fails else "lint ok")

    return combined_code, failures, ", ".join(parts)


def _run_pytest(suite: C.TestSuite, timeout: int) -> tuple[int, set[str], str]:
    """Run ``python -m pytest -q`` in ``C.REPO/suite.cwd`` and parse FAILED lines."""
    label = suite.label
    cwd = C.REPO / suite.cwd if suite.cwd else C.REPO
    env = _suite_env(suite)
    # Prefer the interpreter running the pipeline — bare ``python`` is often
    # missing on Debian/Ubuntu (only ``python3``), which produced a synthetic
    # gate failure and a silent hang-adjacent RED baseline on TASK-027.
    code, out = _run_cmd([sys.executable, "-m", "pytest", "-q"], cwd, env, timeout)
    fails = parse_pytest_failures(out)
    failures = {f"{label}|{f}" for f in fails}
    parts: list[str] = [f"{len(fails)} failed"]
    # Exit-code guard: non-zero exit with zero parsed FAILED lines (crash,
    # collection error, bad pytest config) must surface through the gate.
    if code != 0 and not fails:
        synth = f"{label}|pytest exit {code}"
        failures.add(synth)
        parts.append(f"synthetic: {synth} [{_tail(out)}]")
    return code, failures, ", ".join(parts)


def _run_script(suite: C.TestSuite, timeout: int) -> tuple[int, set[str], str]:
    """Run ``suite.cmd`` and surface a non-zero exit as a synthetic failure key."""
    label = suite.label
    cwd = C.REPO / suite.cwd if suite.cwd else C.REPO
    env = _suite_env(suite)
    cmd = suite.cmd or []
    code, out = _run_cmd(cmd, cwd, env, timeout)
    failures: set[str] = set()
    parts: list[str] = []
    if code != 0:
        synth = f"{label}|script exit {code}"
        failures.add(synth)
        parts.append(f"synthetic: {synth} [{_tail(out)}]")
    else:
        parts.append("ok")
    return code, failures, ", ".join(parts)


_RUNNERS = {
    "npm-vitest": _run_npm_vitest,
    "pytest": _run_pytest,
    "script": _run_script,
}


# --- Discovery & resolution ------------------------------------------------
# Module-level sentinel: discovery/ask runs at most once per process. Every
# exit path of ``resolve_test_suites`` sets it True, and ``run_repo_tests``
# only calls ``resolve_test_suites`` when it is still False — so the 4
# per-task ``run_repo_tests()`` calls never re-prompt.
_suites_resolved: bool = False


def discover_test_suites(repo: Path | None = None) -> list[C.TestSuite]:
    """Scan ``repo`` root (depth 0) and immediate subdirectories (depth 1).

    Returns runnable candidates only (``npm-vitest``/``pytest``/``script``).
    A dir with a ``Cargo.toml``/``go.mod`` is detected but omitted (no runner
    yet) — ``_detect_unsupported_manifests`` surfaces those to the user as
    "Detected, unsupported" in the interactive ask. ``repo`` is resolved
    inside the body (not as a default-arg value) so a test that patches
    ``C.REPO`` after import picks up the new value.
    """
    repo = C.REPO if repo is None else repo
    candidates: list[C.TestSuite] = []

    def _has_test_script(pkg_json: Path) -> bool:
        try:
            data = json.loads(pkg_json.read_text())
        except (OSError, json.JSONDecodeError):
            return False
        scripts = data.get("scripts") or {}
        return bool(scripts.get("test"))

    def _makefile_suite(label: str, cwd: str, mk: Path) -> C.TestSuite | None:
        """Build a ``script``-runner TestSuite from a Makefile, or None.

        Prefers a ``test:`` target (``cmd=["make", "test"]``); falls back to
        ``check:`` (``cmd=["make", "check"]``) when only ``check`` is present;
        returns None when neither target is defined. Delegates target parsing
        to the module-level ``_makefile_targets`` helper.
        """
        targets = _makefile_targets(mk)
        if "test" in targets:
            cmd = ["make", "test"]
        elif "check" in targets:
            cmd = ["make", "check"]
        else:
            return None
        return C.TestSuite(label=label, cwd=cwd, runner="script", cmd=cmd)

    # Depth 0: repo root.
    root_pkg = repo / "package.json"
    if root_pkg.is_file() and _has_test_script(root_pkg):
        candidates.append(C.TestSuite(label=repo.name, cwd="", runner="npm-vitest"))
    if (repo / "pytest.ini").is_file() or (repo / "pyproject.toml").is_file() \
            or (repo / "conftest.py").is_file():
        candidates.append(C.TestSuite(label=repo.name, cwd="", runner="pytest"))
    for mk_name in ("Makefile", "makefile"):
        mk = repo / mk_name
        if mk.is_file():
            suite = _makefile_suite(repo.name, "", mk)
            if suite is not None:
                candidates.append(suite)
            break

    # Depth 1: immediate subdirectories.
    for child in sorted(repo.iterdir()):
        if not child.is_dir():
            continue
        pkg = child / "package.json"
        if pkg.is_file() and _has_test_script(pkg):
            candidates.append(C.TestSuite(label=child.name, cwd=child.name,
                                          runner="npm-vitest"))
        if (child / "pytest.ini").is_file() or (child / "pyproject.toml").is_file() \
                or (child / "conftest.py").is_file():
            candidates.append(C.TestSuite(label=child.name, cwd=child.name,
                                          runner="pytest"))
        for mk_name in ("Makefile", "makefile"):
            mk = child / mk_name
            if mk.is_file():
                suite = _makefile_suite(child.name, child.name, mk)
                if suite is not None:
                    candidates.append(suite)
                break
        # Detected but omitted: no runner registered yet.
        if (child / "Cargo.toml").is_file() or (child / "go.mod").is_file():
            # Cargo/go projects are recognized but produce no candidate until a
            # runner is registered in TEST_SUITE_RUNNERS.
            pass

    return candidates


def _detect_unsupported_manifests(repo: Path | None = None) -> list[tuple[str, str]]:
    """Scan root + depth-1 for cargo/go manifests the gate cannot run yet.

    Returns a list of ``(label, kind)`` pairs where ``kind`` is ``cargo`` or
    ``go``. Used by the interactive ask to surface "Detected, unsupported"
    suites so the user knows the gate saw them and deliberately omitted them
    (not a silent miss).
    """
    repo = C.REPO if repo is None else repo
    out: list[tuple[str, str]] = []
    if (repo / "Cargo.toml").is_file():
        out.append((repo.name, "cargo"))
    if (repo / "go.mod").is_file():
        out.append((repo.name, "go"))
    for child in sorted(repo.iterdir()):
        if not child.is_dir():
            continue
        if (child / "Cargo.toml").is_file():
            out.append((child.name, "cargo"))
        if (child / "go.mod").is_file():
            out.append((child.name, "go"))
    return out


def _persist_suites_to_yaml(chosen: list[C.TestSuite]) -> None:
    """Write ``chosen`` as the ``test_suites:`` key in monkeforge.yaml.

    Preserves existing top-level keys (e.g. ``agents:``). Creates the file if
    absent. Round-trips through yaml.safe_dump so comments are lost (the file
    is the canonical config, not a commented template).
    """
    import yaml as _yaml
    path = C.MF_ROOT / "monkeforge.yaml"
    data: dict = {}
    if path.exists():
        try:
            data = _yaml.safe_load(path.read_text()) or {}
        except _yaml.YAMLError:
            data = {}
    if not isinstance(data, dict):
        data = {}
    data["test_suites"] = [
        {
            "label": s.label,
            "cwd": s.cwd,
            "runner": s.runner,
            **({"cmd": s.cmd} if s.cmd else {}),
            **({"env": s.env} if s.env else {}),
        }
        for s in chosen
    ]
    path.write_text(_yaml.safe_dump(data, sort_keys=False, default_flow_style=False))


def _ask_suites(
    candidates: list[C.TestSuite],
    unsupported: list[tuple[str, str]] | None = None,
) -> tuple[list[C.TestSuite], bool]:
    """Interactive multi-select on stderr/stdin. Returns (chosen, save_toggle).

    Offers a numbered multi-select (comma-separated indices), a skip option,
    and a save toggle (whether to persist the choice to monkeforge.yaml).
    When ``unsupported`` is non-empty, a "Detected, unsupported" block is
    written to stderr BEFORE the numbered candidate list so the user sees the
    cargo/go manifests the gate recognized but cannot run yet.
    """
    if unsupported:
        sys.stderr.write("Detected, unsupported (no runner registered yet):\n")
        for label, kind in unsupported:
            sys.stderr.write(f"  - {label} ({kind})\n")
        sys.stderr.write(
            "These were omitted from the candidates below. Register a runner "
            "in TEST_SUITE_RUNNERS to enable them.\n\n"
        )
    sys.stderr.write("Discovered test-suite candidates:\n")
    for i, s in enumerate(candidates, 1):
        sys.stderr.write(f"  {i}. {s.label} ({s.runner}, cwd={s.cwd or '.'})\n")
    sys.stderr.write("  0. skip (no test gate)\n")
    sys.stderr.write(
        "Enter comma-separated indices (or 0 to skip), then a save toggle "
        "(y/n) to persist to monkeforge.yaml:\n> ")
    raw = sys.stdin.readline().strip()
    save = False
    if "|" in raw:
        sel, save_str = raw.split("|", 1)
        save = save_str.strip().lower() in ("y", "yes", "1", "true")
    else:
        sel = raw
    chosen: list[C.TestSuite] = []
    for tok in sel.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            idx = int(tok)
        except ValueError:
            continue
        if idx == 0:
            return [], save
        if 1 <= idx <= len(candidates):
            chosen.append(candidates[idx - 1])
    return chosen, save



def _effective_task_id(task_id: str | None) -> str:
    """Prefer the caller-supplied task id; fall back to live ``current.json``.

    ``resume`` does not call ``resolve_test_suites`` pre-drive, so the first
    resolve often happens inside ``run_repo_tests``. Without a fallback the
    journal shows ``TASK-? · test-gate`` even though a live task owns the run.
    """
    tid = str(task_id or "").strip()
    if tid and tid != "?":
        return tid
    try:
        data = json.loads((C.METRICS / "current.json").read_text())
    except (OSError, json.JSONDecodeError):
        return tid or "?"
    live = str(data.get("task") or "").strip()
    return live or tid or "?"


def resolve_test_suites(no_input: bool | None = None, task_id: str = "?") -> list[C.TestSuite]:
    """Resolve the test suites to run, discovering + asking if unconfigured.

    Idempotent via the ``_suites_resolved`` sentinel: a second call returns
    ``C.TEST_SUITES`` immediately. When ``C.TEST_SUITES`` is already non-empty
    (yaml/env configured), short-circuits with a ``configured`` journal note
    and never calls ``discover_test_suites``.

    ``no_input`` resolution order: explicit arg → ``PIPELINE_NO_INPUT`` env →
    ``not sys.stdin.isatty()``. Non-interactive: 1 candidate → auto-pick;
    0 → unconfigured; >1 → unconfigured (multiple candidates). Interactive:
    empty candidates → skip cleanly (no ``_ask_suites`` call); non-empty →
    ``_ask_suites`` result is assigned unconditionally (even without save,
    even when ``[]`` for skip).
    """
    global _suites_resolved
    task_id = _effective_task_id(task_id)
    if _suites_resolved:
        return C.TEST_SUITES
    if C.TEST_SUITES:
        _suites_resolved = True
        ev.emit("note", task_id, "test-gate",
                f"configured ({len(C.TEST_SUITES)} suite(s)); skipping discovery")
        return C.TEST_SUITES

    if no_input is None:
        env_ni = os.environ.get("PIPELINE_NO_INPUT")
        if env_ni is not None:
            no_input = env_ni.strip() in ("1", "true", "yes")
        else:
            no_input = not sys.stdin.isatty()

    candidates = discover_test_suites()
    if no_input:
        if len(candidates) == 1:
            C.TEST_SUITES = [candidates[0]]
            ev.emit("note", task_id, "test-gate",
                    f"auto-picked {candidates[0].label}")
        elif len(candidates) == 0:
            C.TEST_SUITES = []
            ev.emit("note", task_id, "test-gate",
                    "unconfigured (no candidates)")
        else:
            labels = ", ".join(s.label for s in candidates)
            C.TEST_SUITES = []
            ev.emit("note", task_id, "test-gate",
                    f"unconfigured (multiple candidates; {labels})")
        _suites_resolved = True
        return C.TEST_SUITES

    # Interactive branch.
    unsupported = _detect_unsupported_manifests()
    if not candidates:
        if unsupported:
            sys.stderr.write("Detected, unsupported (no runner registered yet):\n")
            for label, kind in unsupported:
                sys.stderr.write(f"  - {label} ({kind})\n")
            sys.stderr.write(
                "No runnable test-suite candidates were discovered. Register a "
                "runner in TEST_SUITE_RUNNERS to enable the unsupported suites, "
                "or skip the test gate.\n"
            )
        C.TEST_SUITES = []
        _suites_resolved = True
        ev.emit("note", task_id, "test-gate",
                "unconfigured (no candidates)")
        return C.TEST_SUITES

    chosen, save = _ask_suites(candidates, unsupported=unsupported)
    C.TEST_SUITES = chosen
    if save:
        _persist_suites_to_yaml(chosen)
    ev.emit("note", task_id, "test-gate",
            f"selected {len(chosen)} suite(s)" + (" (persisted)" if save else ""))
    _suites_resolved = True
    return C.TEST_SUITES


# --- Main entry point ------------------------------------------------------
def run_repo_tests_detailed(task_id: str = "?") -> tuple[int, set[str], str, int]:
    """Run all configured test suites via the runner registry; return a 4-tuple.

    Resolves suites (discovery + ask) on the first call only (sentinel-guarded).
    Each suite dispatches to ``_RUNNERS[suite.runner]``. A ``cwd`` that resolves
    outside ``C.REPO`` (escape/misconfiguration) appends a synthetic failure key
    to the returned set — it cannot silently pass the gate. A ``cwd`` that is a
    valid in-repo path but simply not a directory is skipped cleanly (summary
    line only, no failure key).

    Returns ``(max_exit, all_failures, summary, ran_count)`` where ``ran_count``
    is the number of suites that ACTUALLY executed a runner — it excludes
    missing-``cwd``-skipped suites and cwd-escape/unknown-runner synthetic-failure
    suites. Callers that distinguish "gate measured nothing" from "gate measured
    green" read ``ran_count``; the legacy 3-tuple callers use ``run_repo_tests``.

    ``task_id`` is forwarded to ``resolve_test_suites`` so the journal note is
    attributed to the live task (not ``?``) when resolve happens mid-resume.
    """
    if not _suites_resolved:
        resolve_test_suites(task_id=task_id)
    timeout = int(os.environ.get("PIPELINE_TEST_TIMEOUT", "900"))

    all_failures: set[str] = set()
    summaries: list[str] = []
    max_exit = 0
    ran_count = 0

    for suite in C.TEST_SUITES:
        cwd = (C.REPO / suite.cwd if suite.cwd else C.REPO).resolve()
        # cwd-escape guard: a resolved cwd outside the repo is a
        # misconfiguration that must surface through the (int, set[str], str)
        # contract the gate reads — not a silent pass. Counts as a synthetic
        # failure, NOT a ran suite.
        if not cwd.is_relative_to(C.REPO):
            all_failures.add(f"{suite.label}|cwd escapes repo: {cwd}")
            summaries.append(f"{suite.label}: cwd escapes repo ({cwd})")
            continue
        if not cwd.is_dir():
            # Valid in-repo path that simply isn't a directory — skip cleanly,
            # distinct from the escape case above. Does NOT count as ran.
            summaries.append(f"{suite.label}: skipped (dir not found: {cwd})")
            continue
        runner = _RUNNERS.get(suite.runner)
        if runner is None:
            all_failures.add(f"{suite.label}|unknown runner: {suite.runner}")
            summaries.append(f"{suite.label}: unknown runner {suite.runner!r}")
            continue
        code, fails, summary = runner(suite, timeout)
        all_failures |= fails
        summaries.append(f"{suite.label}: {summary}")
        max_exit = max(max_exit, code)
        ran_count += 1

    return max_exit, all_failures, "; ".join(summaries), ran_count


def run_repo_tests(task_id: str = "?") -> tuple[int, set[str], str]:
    """Back-compat 3-tuple wrapper around ``run_repo_tests_detailed``.

    Existing 3-tuple callers (``_capture_test_baseline`` etc.) are unchanged;
    new gate-state callers use the detailed 4-tuple to read ``ran_count``.
    """
    max_exit, all_failures, summary, _ran_count = run_repo_tests_detailed(task_id=task_id)
    return max_exit, all_failures, summary


# Back-compat alias
run_backend_tests = run_repo_tests
