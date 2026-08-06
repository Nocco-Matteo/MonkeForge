"""Defaults scrubbed of nexus-vtt/monorepo runtime assumptions (TASK-029).

MonkeForge is standalone: every render/UX/e2e/lint/ambient knob defaults to
empty/None so the pipeline ships with the visual + render + e2e gates OFF and
no hardcoded lint-debt / ambient-pattern lists. Opt in via the matching
yaml key. These tests import config with a scrubbed PIPELINE_* environment
AND a minimal baseline yaml (no product knobs) so they observe the defaults,
not whatever the developer's shell or real monkeforge.yaml happens to have set.
"""
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

from tests._yaml_fixture import _write_baseline_yaml


# PIPELINE_* env vars whose default we are scrubbing. Anything else (model
# config, docs dir, etc.) is left alone so the import stays valid.
_SCRUB = [
    "PIPELINE_UX_RENDER_CMD",
    "PIPELINE_UX_RENDER_CWD",
    "PIPELINE_UX_SEED_SCRIPT",
    "PIPELINE_RENDER_CMD",
    "PIPELINE_RENDER_CWD",
    "PIPELINE_E2E_DB_CONTAINER",
    "PIPELINE_E2E_PROJECT",
    "PIPELINE_E2E_UP_SCRIPT",
    "PIPELINE_E2E_DB_PORT",
    "PIPELINE_E2E_DATABASE_URL",
    "PIPELINE_LINT_DEBT_RULES",
    "PIPELINE_TEST_AMBIENT_PATTERNS",
    "PIPELINE_WT_YAML",
]


def _scrub_env():
    return {k: v for k, v in os.environ.items()
            if not (k.startswith("PIPELINE_") and k in _SCRUB)}


def _with_scrubbed_config(env, body, *, extra: str = ""):
    """Run ``body(config_module)`` against a freshly imported config built with
    ``env`` and a minimal baseline yaml, then restore the process-wide
    module/env state so other tests that use ``importlib.reload(C)`` keep
    working. ``extra`` is appended to the baseline yaml (add pipeline:/etc.
    blocks per test)."""
    saved_env = os.environ.copy()
    saved_modules = sys.modules.copy()
    # Drop every pipeline_graph submodule so the scrubbed import re-runs their
    # top-level code (config.py reads env at import time).
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
            # under the scrubbed env is dropped; anything that was there before is
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
        _with_scrubbed_config(_scrub_env(), body)

    def test_seed_and_up_script_default_none(self):
        def body(C):
            self.assertIsNone(C.UX_SEED_SCRIPT)
            self.assertIsNone(C.E2E_UP_SCRIPT)
        _with_scrubbed_config(_scrub_env(), body)

    def test_e2e_strings_default_empty(self):
        def body(C):
            self.assertEqual(C.E2E_DB_CONTAINER, "")
            self.assertEqual(C.E2E_PROJECT, "")
            self.assertEqual(C.E2E_DATABASE_URL, "")
        _with_scrubbed_config(_scrub_env(), body)

    def test_debt_and_ambient_default_empty_tuple(self):
        def body(C):
            self.assertEqual(C.LINT_DEBT_RULES, ())
            self.assertEqual(C.TEST_AMBIENT_PATTERNS, ())
        _with_scrubbed_config(_scrub_env(), body)

    def test_no_nexus_substring_in_e2e_url(self):
        # Belt-and-suspenders: the default must not leak the old project name.
        def body(C):
            self.assertNotIn("nexus", C.E2E_DATABASE_URL.lower())
        _with_scrubbed_config(_scrub_env(), body)

    def test_e2e_url_empty_when_both_unset(self):
        # Both PORT and URL unset → URL stays "" (preserves the standalone
        # default pinned by test_e2e_strings_default_empty).
        env = _scrub_env()
        env.pop("PIPELINE_E2E_DB_PORT", None)
        env.pop("PIPELINE_E2E_DATABASE_URL", None)

        def body(C):
            self.assertEqual(C.E2E_DATABASE_URL, "")
        _with_scrubbed_config(env, body)

    def test_e2e_url_empty_when_port_unset_only(self):
        # Restatement of the both-unset case for clarity: scrub URL, scrub PORT.
        env = _scrub_env()
        env.pop("PIPELINE_E2E_DB_PORT", None)
        env.pop("PIPELINE_E2E_DATABASE_URL", None)

        def body(C):
            self.assertEqual(C.E2E_DATABASE_URL, "")
        _with_scrubbed_config(env, body)


class TestE2eUrlFromPort(unittest.TestCase):
    def test_e2e_url_from_port_default(self):
        # PIPELINE_E2E_DB_PORT set explicitly + URL unset → URL derived from
        # the explicit port (the wt-run child-env partial-inheritance case).
        env = _scrub_env()
        env.pop("PIPELINE_E2E_DATABASE_URL", None)
        env["PIPELINE_E2E_DB_PORT"] = "5435"

        def body(C):
            self.assertTrue(C.E2E_DATABASE_URL.endswith(":5435/yourdb?schema=public"))
            self.assertEqual(C.E2E_DB_PORT, 5435)
        _with_scrubbed_config(env, body)


class TestOptInReenable(unittest.TestCase):
    def test_ux_render_cmd_yaml_reenables(self):
        # UX_RENDER_CMD now reads from yaml pipeline.ux_render_cmd (§3e).
        # A non-empty yaml value re-enables the visual gate.
        env = _scrub_env()

        def body(C):
            self.assertTrue(C.UX_RENDER_CMD.strip())
        _with_scrubbed_config(env, body, extra="pipeline:\n  ux_render_cmd: echo ok\n")


if __name__ == "__main__":
    unittest.main()
