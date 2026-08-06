"""Defaults scrubbed of nexus-vtt/monorepo runtime assumptions (TASK-029).

MonkeForge is standalone: every render/UX/e2e/lint/ambient knob defaults to
empty/None so the pipeline ships with the visual + render + e2e gates OFF and
no hardcoded lint-debt / ambient-pattern lists. Opt in via the matching
yaml key. These tests import config against a minimal baseline yaml (no
product knobs) so they observe the §3e defaults, not whatever the developer's
real monkeforge.yaml happens to have set. Non-allowlisted PIPELINE_* env is
ignored by config (§3c); the two allowlisted E2E keys that would affect
defaults are popped per-test where needed.
"""
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

from tests._yaml_fixture import _write_baseline_yaml


def _with_baseline_config(env, body, *, extra: str = ""):
    """Run ``body(config_module)`` against a freshly imported config built with
    ``env`` and a minimal baseline yaml (no product knobs), then restore the
    process-wide module/env state so other tests that use
    ``importlib.reload(C)`` keep working. ``extra`` is appended to the baseline
    yaml (add pipeline:/etc. blocks per test)."""
    saved_env = os.environ.copy()
    saved_modules = sys.modules.copy()
    # Drop every pipeline_graph submodule so the fresh import re-runs their
    # top-level code (config.py reads env/yaml at import time).
    for _mod in [m for m in sys.modules if m.startswith("pipeline_graph")]:
        del sys.modules[_mod]
    os.environ.clear()
    os.environ.update(env)
    # Point PIPELINE_WT_YAML at a minimal baseline yaml (agents: only, no
    # product knobs) so config.py imports cleanly and every §3e default is
    # observed.
    with tempfile.TemporaryDirectory() as td:
        yaml_path = _write_baseline_yaml(Path(td) / "monkeforge.yaml", extra=extra)
        os.environ["PIPELINE_WT_YAML"] = str(yaml_path)
        try:
            from pipeline_graph import config as C  # noqa: WPS433
            return body(C)
        finally:
            os.environ.clear()
            os.environ.update(saved_env)
            # Restore the original module registry exactly. Anything we imported
            # under the baseline env is dropped; anything that was there before is
            # put back. This keeps ``importlib.reload(C)`` in other tests valid.
            for _mod in [m for m in sys.modules if m.startswith("pipeline_graph")]:
                del sys.modules[_mod]
            sys.modules.update(saved_modules)


class TestScrubbedDefaults(unittest.TestCase):
    def test_render_cmd_defaults_empty(self):
        def body(C):
            self.assertEqual(C.UX_RENDER_CMD, "")
            self.assertEqual(C.RENDER_CMD, "")
            self.assertEqual(C.UX_RENDER_CWD, "")
            self.assertEqual(C.RENDER_CWD, "")
        _with_baseline_config(dict(os.environ), body)

    def test_seed_and_up_script_default_none(self):
        def body(C):
            self.assertIsNone(C.UX_SEED_SCRIPT)
            self.assertIsNone(C.E2E_UP_SCRIPT)
        _with_baseline_config(dict(os.environ), body)

    def test_e2e_strings_default_empty(self):
        # Pop the two allowlisted E2E keys so the §3e defaults are observed.
        env = dict(os.environ)
        env.pop("PIPELINE_E2E_DB_PORT", None)
        env.pop("PIPELINE_E2E_DATABASE_URL", None)

        def body(C):
            self.assertEqual(C.E2E_DB_CONTAINER, "")
            self.assertEqual(C.E2E_PROJECT, "")
            self.assertEqual(C.E2E_DATABASE_URL, "")
        _with_baseline_config(env, body)

    def test_debt_and_ambient_default_empty_tuple(self):
        def body(C):
            self.assertEqual(C.LINT_DEBT_RULES, ())
            self.assertEqual(C.TEST_AMBIENT_PATTERNS, ())
        _with_baseline_config(dict(os.environ), body)

    def test_no_nexus_substring_in_e2e_url(self):
        # Belt-and-suspenders: the default must not leak the old project name.
        env = dict(os.environ)
        env.pop("PIPELINE_E2E_DB_PORT", None)
        env.pop("PIPELINE_E2E_DATABASE_URL", None)

        def body(C):
            self.assertNotIn("nexus", C.E2E_DATABASE_URL.lower())
        _with_baseline_config(env, body)

    def test_e2e_url_empty_when_both_unset(self):
        # Both PORT and URL unset → URL stays "" (preserves the standalone
        # default pinned by test_e2e_strings_default_empty).
        env = dict(os.environ)
        env.pop("PIPELINE_E2E_DB_PORT", None)
        env.pop("PIPELINE_E2E_DATABASE_URL", None)

        def body(C):
            self.assertEqual(C.E2E_DATABASE_URL, "")
        _with_baseline_config(env, body)

    def test_e2e_url_empty_when_port_unset_only(self):
        # Restatement of the both-unset case for clarity: pop URL, pop PORT.
        env = dict(os.environ)
        env.pop("PIPELINE_E2E_DB_PORT", None)
        env.pop("PIPELINE_E2E_DATABASE_URL", None)

        def body(C):
            self.assertEqual(C.E2E_DATABASE_URL, "")
        _with_baseline_config(env, body)


class TestE2eUrlFromPort(unittest.TestCase):
    def test_e2e_url_from_port_default(self):
        # PIPELINE_E2E_DB_PORT set explicitly + URL unset → URL derived from
        # the explicit port (the wt-run child-env partial-inheritance case).
        env = dict(os.environ)
        env.pop("PIPELINE_E2E_DATABASE_URL", None)
        env["PIPELINE_E2E_DB_PORT"] = "5435"

        def body(C):
            self.assertTrue(C.E2E_DATABASE_URL.endswith(":5435/yourdb?schema=public"))
            self.assertEqual(C.E2E_DB_PORT, 5435)
        _with_baseline_config(env, body)


class TestOptInReenable(unittest.TestCase):
    def test_ux_render_cmd_yaml_reenables(self):
        # UX_RENDER_CMD now reads from yaml pipeline.ux_render_cmd (§3e).
        # A non-empty yaml value re-enables the visual gate.
        env = dict(os.environ)

        def body(C):
            self.assertTrue(C.UX_RENDER_CMD.strip())
        _with_baseline_config(env, body, extra="pipeline:\n  ux_render_cmd: echo ok\n")


if __name__ == "__main__":
    unittest.main()
