"""Engagement gate tests for the eyes runner (TASK-012).

Validates ``eyes_engaged``, ``eyes_new_runner_eligible``,
``resolved_eyes_gate_mode``, ``_eyes_config_minimum``, and
``validate_ui_config`` — the config-level helpers that replaced the bare
``UX_RENDER_CMD.strip()`` checks across the graph.
"""
import pytest
from unittest.mock import patch

from pipeline_graph import config as C


class TestEyesConfigMinimum:
    def test_empty_dict(self):
        assert C._eyes_config_minimum({}) is False

    def test_non_dict(self):
        assert C._eyes_config_minimum("not a dict") is False
        assert C._eyes_config_minimum(None) is False

    def test_missing_type(self):
        assert C._eyes_config_minimum({"url": "http://x"}) is False

    def test_empty_type(self):
        assert C._eyes_config_minimum({"type": "", "url": "http://x"}) is False

    def test_url_set(self):
        assert C._eyes_config_minimum({"type": "web", "url": "http://x"}) is True

    def test_start_ready_set(self):
        assert C._eyes_config_minimum({
            "type": "web", "start": "npm run dev",
            "ready": "http://127.0.0.1:3000/"}) is True

    def test_start_only(self):
        assert C._eyes_config_minimum({
            "type": "web", "start": "npm run dev"}) is False

    def test_ready_only(self):
        assert C._eyes_config_minimum({
            "type": "web", "ready": "http://x"}) is False

    def test_screens_optional(self):
        # screens absent is fine — minimum is type + (url OR (start + ready))
        assert C._eyes_config_minimum({
            "type": "web", "url": "http://x"}) is True


class TestEyesEngaged:
    def test_empty_state_no_signals(self):
        with patch.object(C, "UI_CONFIG", {}), \
             patch.object(C, "UX_RENDER_CMD", ""):
            assert C.eyes_engaged({}) is False

    def test_has_ui_alone_not_engaged(self):
        with patch.object(C, "UI_CONFIG", {}), \
             patch.object(C, "UX_RENDER_CMD", ""):
            assert C.eyes_engaged({"has_ui": True}) is False

    def test_render_cmd_alone_not_engaged(self):
        # PIPELINE_RENDER_CMD-only (not UX_RENDER_CMD) → not engaged
        with patch.object(C, "UI_CONFIG", {}), \
             patch.object(C, "UX_RENDER_CMD", ""):
            assert C.eyes_engaged({"has_ui": True}) is False

    def test_has_ui_with_render_cmd_engaged(self):
        with patch.object(C, "UI_CONFIG", {}), \
             patch.object(C, "UX_RENDER_CMD", "npx playwright test"):
            assert C.eyes_engaged({"has_ui": True}) is True

    def test_render_cmd_without_has_ui_engaged(self):
        # Per README: any non-empty PIPELINE_UX_RENDER_CMD engages eyes,
        # regardless of has_ui.
        with patch.object(C, "UI_CONFIG", {}), \
             patch.object(C, "UX_RENDER_CMD", "npx playwright test"):
            assert C.eyes_engaged({"has_ui": False}) is True

    def test_yaml_ui_engages(self):
        with patch.object(C, "UI_CONFIG",
                          {"type": "web", "url": "http://x"}), \
             patch.object(C, "UX_RENDER_CMD", ""):
            assert C.eyes_engaged({"has_ui": False}) is True

    def test_checkpointed_ui_config_engages(self):
        with patch.object(C, "UI_CONFIG", {}), \
             patch.object(C, "UX_RENDER_CMD", ""):
            assert C.eyes_engaged({
                "ui_config": {"type": "web", "url": "http://x"}}) is True

    def test_cli_flag_engages(self):
        with patch.object(C, "UI_CONFIG", {}), \
             patch.object(C, "UX_RENDER_CMD", ""):
            assert C.eyes_engaged({"eyes_engaged": True}) is True

    def test_yaml_wins_over_empty_state(self):
        with patch.object(C, "UI_CONFIG",
                          {"type": "web", "url": "http://x"}), \
             patch.object(C, "UX_RENDER_CMD", ""):
            assert C.eyes_engaged({}) is True


class TestEyesNewRunnerEligible:
    def test_yaml_eligible(self):
        with patch.object(C, "UI_CONFIG",
                          {"type": "web", "url": "http://x"}):
            assert C.eyes_new_runner_eligible({}) is True

    def test_state_ui_config_eligible(self):
        with patch.object(C, "UI_CONFIG", {}):
            assert C.eyes_new_runner_eligible({
                "ui_config": {"type": "web", "url": "http://x"}}) is True

    def test_render_cmd_only_not_eligible(self):
        # Legacy env alone → NOT new-runner eligible (compat subprocess path)
        with patch.object(C, "UI_CONFIG", {}), \
             patch.object(C, "UX_RENDER_CMD", "npx playwright test"):
            assert C.eyes_new_runner_eligible({"has_ui": True}) is False

    def test_yaml_wins_over_state(self):
        with patch.object(C, "UI_CONFIG",
                          {"type": "web", "url": "http://yaml"}):
            assert C.eyes_new_runner_eligible(
                {"ui_config": {"type": "web", "url": "http://state"}}) is True


class TestResolvedEyesGateMode:
    def test_cli_override_wins(self):
        state = {"eyes_gate_mode": "full", "effort": "scout-monke"}
        assert C.resolved_eyes_gate_mode(state) == "full"

    def test_invalid_override_falls_back(self):
        state = {"eyes_gate_mode": "invalid", "effort": "troop-monke"}
        assert C.resolved_eyes_gate_mode(state) == "standard"

    def test_no_override_uses_resolved_gate_mode(self):
        state = {"effort": "barrel-monke"}
        assert C.resolved_eyes_gate_mode(state) == "full"

    def test_empty_override_falls_back(self):
        state = {"eyes_gate_mode": "", "effort": "troop-monke"}
        assert C.resolved_eyes_gate_mode(state) == "standard"


class TestValidateUiConfig:
    def test_valid_minimal(self):
        cfg = {"type": "web", "url": "http://127.0.0.1:3000/"}
        out = C.validate_ui_config(cfg)
        assert out["type"] == "web"

    def test_auto_resolves_to_web(self):
        cfg = {"type": "auto", "url": "http://x"}
        out = C.validate_ui_config(cfg)
        assert out["type"] == "web"

    def test_electron_raises(self):
        cfg = {"type": "electron", "url": "http://x"}
        with pytest.raises(ValueError, match="electron is not supported"):
            C.validate_ui_config(cfg)

    def test_invalid_type_raises(self):
        cfg = {"type": "mobile", "url": "http://x"}
        with pytest.raises(ValueError, match="not valid"):
            C.validate_ui_config(cfg)

    def test_unknown_top_key_raises(self):
        cfg = {"type": "web", "url": "http://x", "bad_key": "x"}
        with pytest.raises(ValueError, match="unknown keys"):
            C.validate_ui_config(cfg)

    def test_valid_screens(self):
        cfg = {
            "type": "web", "url": "http://x",
            "screens": [
                {"name": "home", "actions": [
                    {"action": "goto", "url": "http://x"},
                    {"action": "screenshot", "name": "home"}]}]}
        out = C.validate_ui_config(cfg)
        assert len(out["screens"]) == 1

    def test_screen_missing_name_raises(self):
        cfg = {
            "type": "web", "url": "http://x",
            "screens": [{"actions": [{"action": "goto", "url": "http://x"}]}]}
        with pytest.raises(ValueError, match="missing 'name'"):
            C.validate_ui_config(cfg)

    def test_unknown_action_raises(self):
        cfg = {
            "type": "web", "url": "http://x",
            "screens": [{"name": "s", "actions": [
                {"action": "eval", "script": "x"}]}]}
        with pytest.raises(ValueError, match="unknown action"):
            C.validate_ui_config(cfg)

    def test_wait_for_unknown_state_raises(self):
        cfg = {
            "type": "web", "url": "http://x",
            "screens": [{"name": "s", "actions": [
                {"action": "wait_for", "selector": "#el", "state": "enabled"}]}]}
        with pytest.raises(ValueError, match="unknown state"):
            C.validate_ui_config(cfg)

    def test_press_unknown_key_raises(self):
        cfg = {
            "type": "web", "url": "http://x",
            "screens": [{"name": "s", "actions": [
                {"action": "press", "selector": "#el", "key": "Ctrl+A"}]}]}
        with pytest.raises(ValueError, match="unknown key"):
            C.validate_ui_config(cfg)

    def test_auth_hooks_unknown_key_raises(self):
        cfg = {"type": "web", "url": "http://x", "auth_hooks": {"bad": True}}
        with pytest.raises(ValueError, match="auth_hooks.*unknown keys"):
            C.validate_ui_config(cfg)

    def test_auth_hooks_valid(self):
        cfg = {"type": "web", "url": "http://x",
               "auth_hooks": {"seed_script": "bash s.sh", "require_e2e_db": True}}
        out = C.validate_ui_config(cfg)
        assert out["auth_hooks"]["seed_script"] == "bash s.sh"

    def test_viewport_valid(self):
        cfg = {"type": "web", "url": "http://x",
               "viewport": {"width": 1920, "height": 1080}}
        out = C.validate_ui_config(cfg)
        assert out["viewport"]["width"] == 1920

    def test_viewport_invalid_raises(self):
        cfg = {"type": "web", "url": "http://x", "viewport": {"w": 100}}
        with pytest.raises(ValueError, match="viewport must be"):
            C.validate_ui_config(cfg)

    def test_cwd_escape_raises(self):
        cfg = {"type": "web", "url": "http://x", "cwd": "../../../etc"}
        with pytest.raises(ValueError, match="resolves outside repo"):
            C.validate_ui_config(cfg)

    def test_cwd_valid_relative(self):
        cfg = {"type": "web", "url": "http://x", "cwd": "frontend"}
        out = C.validate_ui_config(cfg)
        assert out["cwd"] == "frontend"
