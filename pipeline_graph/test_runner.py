"""Monorepo vitest + tsc runner for the implement gate — not LLM stdout."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from . import config as C

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


def _run_suite(label: str, cwd: Path, env: dict[str, str], timeout: int) -> tuple[int, set[str], str]:
    """Run tsc --noEmit then vitest. Returns (exit_code, failures, summary)."""
    failures: set[str] = set()
    parts: list[str] = []

    # Phase 1: type check
    tc_code, tc_out = _run_cmd(["npm", "run", "typecheck"], cwd, env, timeout)
    if tc_code != 0:
        tc_fails = parse_tsc_errors(tc_out)
        failures |= {f"{label}|{f}" for f in tc_fails}
        n_errs = len(tc_fails)
        parts.append(f"{n_errs} type error(s)")
    else:
        parts.append("typecheck ok")

    # Phase 2: vitest (always run, even if tsc failed — to get full picture)
    vt_code, vt_out = _run_cmd(["npm", "test"], cwd, env, timeout)
    if vt_out.startswith("runner error:"):
        parts.append(vt_out)
    else:
        vt_fails = parse_vitest_failures(vt_out)
        failures |= {f"{label}|{line}" for line in vt_fails}
        parts.append(_summarize_vitest_output(vt_out, vt_code, len(vt_fails)))

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

    return max(tc_code, vt_code), failures, ", ".join(parts)


def run_repo_tests() -> tuple[int, set[str], str]:
    """Run all configured test suites. Each suite runs `npm run typecheck` then `npm test`."""
    timeout = int(os.environ.get("PIPELINE_TEST_TIMEOUT", "900"))

    all_failures: set[str] = set()
    summaries: list[str] = []
    max_exit = 0

    for label, subdir, env in C.TEST_SUITES:
        cwd = C.REPO / subdir if subdir else C.REPO
        if not cwd.is_dir():
            summaries.append(f"{label}: skipped (dir not found: {cwd})")
            continue
        code, fails, summary = _run_suite(label, cwd, env, timeout)
        all_failures |= fails
        summaries.append(f"{label}: {summary}")
        max_exit = max(max_exit, code)

    return max_exit, all_failures, "; ".join(summaries)


# Back-compat alias
run_backend_tests = run_repo_tests
