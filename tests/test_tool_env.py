"""tool_env_from_yaml() per-spawn env hygiene (TASK-032 §3e).

Verifies that agent subprocesses get a clean env: allowlisted PIPELINE_* keys
are kept, non-allowlisted product-knob PIPELINE_* keys are popped (so a stale
shell env does not leak into the agent and override the yaml value).
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from pipeline_graph import config as C
from pipeline_graph.agents import tool_env_from_yaml


class TestToolEnvFromYaml(unittest.TestCase):
    def test_allowlisted_keys_preserved(self):
        env = dict(os.environ)
        env["PIPELINE_REPO"] = "/some/repo"
        env["PIPELINE_DRY_RUN"] = "1"
        env["PIPELINE_WT_YAML"] = "/some/yaml.yaml"
        env["PIPELINE_ISOLATED"] = "1"
        with patch.dict(os.environ, env, clear=False):
            result = tool_env_from_yaml()
        self.assertEqual(result["PIPELINE_REPO"], "/some/repo")
        self.assertEqual(result["PIPELINE_DRY_RUN"], "1")
        self.assertEqual(result["PIPELINE_WT_YAML"], "/some/yaml.yaml")
        self.assertEqual(result["PIPELINE_ISOLATED"], "1")

    def test_non_allowlisted_pipeline_keys_popped(self):
        env = dict(os.environ)
        env["PIPELINE_MAX_DEBATE_ROUNDS"] = "99"
        env["PIPELINE_AGENT_TIMEOUT"] = "999"
        env["PIPELINE_BRANCH_PREFIX"] = "stale/"
        env["PIPELINE_NO_GIT"] = "1"
        env["PIPELINE_UX_RENDER_CMD"] = "stale-cmd"
        env["PIPELINE_RENDER_CMD"] = "stale-render"
        env["PIPELINE_LINT_DEBT_RULES"] = "stale-rule"
        env["PIPELINE_TEST_AMBIENT_PATTERNS"] = "stale-pattern"
        env["PIPELINE_HEARTBEAT_INTERVAL_S"] = "1"
        env["PIPELINE_TEST_TIMEOUT"] = "1"
        env["PIPELINE_RECURSION_LIMIT"] = "1"
        env["PIPELINE_E2E_DB_CONTAINER"] = "stale-container"
        with patch.dict(os.environ, env, clear=False):
            result = tool_env_from_yaml()
        self.assertNotIn("PIPELINE_MAX_DEBATE_ROUNDS", result)
        self.assertNotIn("PIPELINE_AGENT_TIMEOUT", result)
        self.assertNotIn("PIPELINE_BRANCH_PREFIX", result)
        self.assertNotIn("PIPELINE_NO_GIT", result)
        self.assertNotIn("PIPELINE_UX_RENDER_CMD", result)
        self.assertNotIn("PIPELINE_RENDER_CMD", result)
        self.assertNotIn("PIPELINE_LINT_DEBT_RULES", result)
        self.assertNotIn("PIPELINE_TEST_AMBIENT_PATTERNS", result)
        self.assertNotIn("PIPELINE_HEARTBEAT_INTERVAL_S", result)
        self.assertNotIn("PIPELINE_TEST_TIMEOUT", result)
        self.assertNotIn("PIPELINE_RECURSION_LIMIT", result)
        self.assertNotIn("PIPELINE_E2E_DB_CONTAINER", result)

    def test_non_pipeline_env_preserved(self):
        # Non-PIPELINE env vars (PATH, HOME, etc.) must survive.
        with patch.dict(os.environ, {"MY_CUSTOM_VAR": "hello"}, clear=False):
            result = tool_env_from_yaml()
        self.assertEqual(result.get("MY_CUSTOM_VAR"), "hello")

    def test_returns_dict_not_environ(self):
        result = tool_env_from_yaml()
        self.assertIsInstance(result, dict)
        # Mutating the result must not affect os.environ.
        result["PIPELINE_REPO"] = "/mutated"
        self.assertNotEqual(os.environ.get("PIPELINE_REPO"), "/mutated")


if __name__ == "__main__":
    unittest.main()
