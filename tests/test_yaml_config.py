"""YAML-direct config reads (TASK-032 §3e) — product knobs from monkeforge.yaml.

Tests that config.py reads product knobs directly from the yaml file (not via
the PIPELINE_* env bridge), that the §3a precedence (allowlisted env > yaml >
default) holds, and that stale non-allowlisted PIPELINE_* env is ignored.
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

from tests._yaml_fixture import _baseline_yaml, _write_baseline_yaml


def _reload_config_with_yaml(yaml_path: Path):
    """Reload config with PIPELINE_WT_YAML pointing at yaml_path. Returns C."""
    os.environ["PIPELINE_WT_YAML"] = str(yaml_path)
    from pipeline_graph import config as C
    importlib.reload(C)
    return C


def _restore_config(saved_wt: str | None):
    if saved_wt is not None:
        os.environ["PIPELINE_WT_YAML"] = saved_wt
    else:
        os.environ.pop("PIPELINE_WT_YAML", None)
    from pipeline_graph import config as C
    importlib.reload(C)


class TestAllowlistEnv(unittest.TestCase):
    """The §3b allowlist is an exact frozenset — no wildcards."""

    def test_is_frozenset(self):
        from pipeline_graph import config as C
        self.assertIsInstance(C._ALLOWLIST_ENV, frozenset)

    def test_contains_runtime_and_secret_keys(self):
        from pipeline_graph import config as C
        for k in ("PIPELINE_REPO", "PIPELINE_DOCS_DIR", "PIPELINE_DRY_RUN",
                  "PIPELINE_NOTIFY_LEVEL", "PIPELINE_WT_YAML", "PIPELINE_ISOLATED",
                  "PIPELINE_BOT_AUTOSTART", "PIPELINE_E2E_DB_PORT",
                  "PIPELINE_E2E_DATABASE_URL", "PIPELINE_NO_INPUT"):
            self.assertIn(k, C._ALLOWLIST_ENV, f"{k} must be in allowlist")

    def test_no_product_knob_in_allowlist(self):
        from pipeline_graph import config as C
        # Product knobs must NOT be in the allowlist (they're yaml-only).
        for k in ("PIPELINE_MAX_DEBATE_ROUNDS", "PIPELINE_AGENT_TIMEOUT",
                  "PIPELINE_BRANCH_PREFIX", "PIPELINE_NO_GIT",
                  "PIPELINE_UX_RENDER_CMD", "PIPELINE_RENDER_CMD",
                  "PIPELINE_E2E_DB_CONTAINER", "PIPELINE_E2E_PROJECT",
                  "PIPELINE_E2E_UP_SCRIPT", "PIPELINE_LINT_DEBT_RULES",
                  "PIPELINE_TEST_AMBIENT_PATTERNS", "PIPELINE_PLATEAU_THRESHOLD",
                  "PIPELINE_DEBATE_STUCK_ROUNDS", "PIPELINE_HEARTBEAT_INTERVAL_S",
                  "PIPELINE_TEST_TIMEOUT", "PIPELINE_RECURSION_LIMIT"):
            self.assertNotIn(k, C._ALLOWLIST_ENV, f"{k} must NOT be in allowlist")


class TestEnvOrYamlPrecedence(unittest.TestCase):
    """§3a: allowlisted env > yaml > default."""

    def setUp(self):
        self._saved_wt = os.environ.get("PIPELINE_WT_YAML")
        self._saved_env = os.environ.copy()
        self._td = tempfile.TemporaryDirectory()
        self._yaml_path = _write_baseline_yaml(Path(self._td.name) / "monkeforge.yaml")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)
        _restore_config(self._saved_wt)
        self._td.cleanup()

    def test_env_wins_over_yaml(self):
        # PIPELINE_E2E_DB_PORT is allowlisted — env wins over yaml.
        os.environ["PIPELINE_E2E_DB_PORT"] = "5555"
        _write_baseline_yaml(self._yaml_path,
                             extra="pipeline:\n  e2e_db_port: 6666\n")
        C = _reload_config_with_yaml(self._yaml_path)
        self.assertEqual(C.E2E_DB_PORT, 5555)

    def test_yaml_wins_over_default(self):
        # No env set → yaml wins over the 5433 default.
        os.environ.pop("PIPELINE_E2E_DB_PORT", None)
        _write_baseline_yaml(self._yaml_path,
                             extra="pipeline:\n  e2e_db_port: 7777\n")
        C = _reload_config_with_yaml(self._yaml_path)
        self.assertEqual(C.E2E_DB_PORT, 7777)

    def test_default_when_both_unset(self):
        os.environ.pop("PIPELINE_E2E_DB_PORT", None)
        _write_baseline_yaml(self._yaml_path)
        C = _reload_config_with_yaml(self._yaml_path)
        self.assertEqual(C.E2E_DB_PORT, 5433)

    def test_empty_env_treated_as_unset(self):
        # An empty PIPELINE_BOT_AUTOSTART= does not clobber yaml.
        os.environ["PIPELINE_BOT_AUTOSTART"] = ""
        _write_baseline_yaml(self._yaml_path,
                             extra="discord:\n  bot_autostart: true\n")
        C = _reload_config_with_yaml(self._yaml_path)
        self.assertTrue(C.BOT_AUTOSTART)

    def test_cast_failure_falls_through(self):
        # A non-int env value falls through to yaml, then default.
        os.environ["PIPELINE_E2E_DB_PORT"] = "not-a-port"
        _write_baseline_yaml(self._yaml_path,
                             extra="pipeline:\n  e2e_db_port: 8888\n")
        C = _reload_config_with_yaml(self._yaml_path)
        self.assertEqual(C.E2E_DB_PORT, 8888)


class TestStaleEnvIgnored(unittest.TestCase):
    """Non-allowlisted PIPELINE_* env is ignored for product knobs (§3c)."""

    def setUp(self):
        self._saved_wt = os.environ.get("PIPELINE_WT_YAML")
        self._saved_env = os.environ.copy()
        self._td = tempfile.TemporaryDirectory()
        self._yaml_path = _write_baseline_yaml(Path(self._td.name) / "monkeforge.yaml")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)
        _restore_config(self._saved_wt)
        self._td.cleanup()

    def test_stale_max_debate_rounds_ignored(self):
        os.environ["PIPELINE_MAX_DEBATE_ROUNDS"] = "99"
        _write_baseline_yaml(self._yaml_path,
                             extra="pipeline:\n  max_debate_rounds: 3\n")
        C = _reload_config_with_yaml(self._yaml_path)
        self.assertEqual(C.MAX_DEBATE_ROUNDS, 3)

    def test_stale_branch_prefix_ignored(self):
        os.environ["PIPELINE_BRANCH_PREFIX"] = "stale/"
        _write_baseline_yaml(self._yaml_path,
                             extra="pipeline:\n  branch_prefix: 'feat/'\n")
        C = _reload_config_with_yaml(self._yaml_path)
        self.assertEqual(C.BRANCH_PREFIX, "feat/")

    def test_stale_no_git_ignored(self):
        os.environ["PIPELINE_NO_GIT"] = "1"
        _write_baseline_yaml(self._yaml_path)
        C = _reload_config_with_yaml(self._yaml_path)
        self.assertFalse(C.NO_GIT)

    def test_stale_render_cmd_ignored(self):
        os.environ["PIPELINE_RENDER_CMD"] = "stale-cmd"
        _write_baseline_yaml(self._yaml_path)
        C = _reload_config_with_yaml(self._yaml_path)
        self.assertEqual(C.RENDER_CMD, "")

    def test_stale_agent_timeout_ignored(self):
        os.environ["PIPELINE_AGENT_TIMEOUT"] = "999"
        _write_baseline_yaml(self._yaml_path,
                             extra="pipeline:\n  agent_timeout: 600\n")
        C = _reload_config_with_yaml(self._yaml_path)
        self.assertEqual(C.AGENT_TIMEOUT, 600)


class TestYamlDirectReads(unittest.TestCase):
    """Product knobs read directly from yaml sub-dicts."""

    def setUp(self):
        self._saved_wt = os.environ.get("PIPELINE_WT_YAML")
        self._saved_env = os.environ.copy()
        self._td = tempfile.TemporaryDirectory()
        self._yaml_path = Path(self._td.name) / "monkeforge.yaml"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)
        _restore_config(self._saved_wt)
        self._td.cleanup()

    def test_pipeline_dict_reads(self):
        _write_baseline_yaml(self._yaml_path, extra=(
            "pipeline:\n"
            "  max_debate_rounds: 5\n"
            "  max_fix_cycles: 4\n"
            "  max_intake_rounds: 8\n"
            "  agent_timeout: 1200\n"
            "  max_ux_render_cycles: 6\n"
            "  branch_prefix: 'task/'\n"
            "  no_git: true\n"
            "  plateau_threshold: 3\n"
        ))
        C = _reload_config_with_yaml(self._yaml_path)
        self.assertEqual(C.MAX_DEBATE_ROUNDS, 5)
        self.assertEqual(C.MAX_FIX_CYCLES, 4)
        self.assertEqual(C.MAX_INTAKE_ROUNDS, 8)
        self.assertEqual(C.AGENT_TIMEOUT, 1200)
        self.assertEqual(C.MAX_UX_RENDER_CYCLES, 6)
        self.assertEqual(C.BRANCH_PREFIX, "task/")
        self.assertTrue(C.NO_GIT)
        self.assertEqual(C.PLATEAU_THRESHOLD, 3)

    def test_notifications_dict_reads(self):
        _write_baseline_yaml(self._yaml_path, extra=(
            "notifications:\n"
            "  level: all\n"
            "  rate: 15\n"
            "  window: 45\n"
        ))
        C = _reload_config_with_yaml(self._yaml_path)
        self.assertEqual(C.NOTIFY_LEVEL, "all")
        self.assertEqual(C.NOTIFY_RATE, 15)
        self.assertEqual(C.NOTIFY_WINDOW, 45)

    def test_discord_dict_reads(self):
        _write_baseline_yaml(self._yaml_path, extra=(
            "discord:\n"
            "  bot_name: Test Bot\n"
            "  bot_avatar: https://example.com/a.png\n"
            "  bot_poll_seconds: 10\n"
            "  resume_timeout: 7200\n"
        ))
        C = _reload_config_with_yaml(self._yaml_path)
        self.assertEqual(C.BOT_NAME, "Test Bot")
        self.assertEqual(C.BOT_AVATAR, "https://example.com/a.png")
        self.assertEqual(C.BOT_POLL_SECONDS, 10)
        self.assertEqual(C.BOT_RESUME_TIMEOUT, 7200)

    def test_agent_tuning_reads(self):
        _write_baseline_yaml(self._yaml_path, extra=(
            "pipeline:\n"
            "  heartbeat_interval_s: 5\n"
            "  min_output_bytes: 80\n"
            "  agent_transient_retries: 2\n"
            "  agent_backoff_s: 4\n"
            "  test_timeout: 600\n"
            "  recursion_limit: 150\n"
            "  ux_review_retries: 2\n"
            "  ux_review_backoff_s: 5\n"
            "  final_fix_timeout: 300\n"
        ))
        C = _reload_config_with_yaml(self._yaml_path)
        self.assertEqual(C.HEARTBEAT_INTERVAL_S, 5)
        self.assertEqual(C.MIN_OUTPUT_BYTES, 80)
        self.assertEqual(C.AGENT_TRANSIENT_RETRIES, 2)
        self.assertEqual(C.AGENT_BACKOFF_S, 4)
        self.assertEqual(C.TEST_TIMEOUT, 600)
        self.assertEqual(C.RECURSION_LIMIT, 150)
        self.assertEqual(C.UX_REVIEW_RETRIES, 2)
        self.assertEqual(C.UX_REVIEW_BACKOFF_S, 5)
        self.assertEqual(C.FINAL_FIX_TIMEOUT, 300)

    def test_effort_reads_from_yaml(self):
        _write_baseline_yaml(self._yaml_path, extra=(
            "effort:\n"
            "  scout-monke:\n"
            "    debate_rounds: 0\n"
            "    gates: off\n"
            "    fix_cycles: 1\n"
        ))
        C = _reload_config_with_yaml(self._yaml_path)
        self.assertIn("scout-monke", C.EFFORT_LEVELS)
        self.assertEqual(C.EFFORT_LEVELS["scout-monke"]["debate_rounds"], 0)

    def test_lint_debt_rules_yaml_list(self):
        _write_baseline_yaml(self._yaml_path, extra=(
            "pipeline:\n"
            "  lint_debt_rules:\n"
            "    - no-unused-vars\n"
            "    - no-explicit-any\n"
        ))
        C = _reload_config_with_yaml(self._yaml_path)
        self.assertEqual(C.LINT_DEBT_RULES, ("no-unused-vars", "no-explicit-any"))

    def test_lint_debt_rules_yaml_string(self):
        _write_baseline_yaml(self._yaml_path, extra=(
            "pipeline:\n"
            "  lint_debt_rules: 'no-unused-vars;no-explicit-any'\n"
        ))
        C = _reload_config_with_yaml(self._yaml_path)
        self.assertEqual(C.LINT_DEBT_RULES, ("no-unused-vars", "no-explicit-any"))


if __name__ == "__main__":
    unittest.main()
