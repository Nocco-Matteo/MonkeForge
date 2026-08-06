"""Trace validator tests for the eyes runner (TASK-012).

Validates the closed action allowlist, required params, ``wait_for.state`` /
``press.key`` enums, and fail-loud behavior. Pure — no browser, no I/O.
"""
import pytest

from pipeline_graph.eyes.trace_validator import validate_trace


class TestValidateTraceAllowlist:
    def test_empty_screens_rejected(self):
        with pytest.raises(ValueError, match="non-empty list"):
            validate_trace([])

    def test_non_list_rejected(self):
        with pytest.raises(ValueError, match="non-empty list"):
            validate_trace("not a list")

    def test_unknown_action_rejected(self):
        screens = [{"name": "home", "actions": [{"action": "eval", "script": "x"}]}]
        with pytest.raises(ValueError, match="unknown action 'eval'"):
            validate_trace(screens)

    def test_known_actions_accepted(self):
        screens = [{"name": "home", "actions": [
            {"action": "goto", "url": "http://127.0.0.1:3000/"},
            {"action": "click", "selector": "#btn"},
            {"action": "fill", "selector": "#inp", "text": "hi"},
            {"action": "select", "selector": "#sel", "value": "opt"},
            {"action": "press", "selector": "#inp", "key": "Enter"},
            {"action": "hover", "selector": "#el"},
            {"action": "scroll", "selector": "#el"},
            {"action": "wait_for", "selector": "#el"},
            {"action": "wait_ms", "ms": "500"},
            {"action": "screenshot", "name": "home"},
        ]}]
        validate_trace(screens)  # no raise

    def test_missing_name_rejected(self):
        screens = [{"actions": [{"action": "goto", "url": "x"}]}]
        with pytest.raises(ValueError, match="missing 'name'"):
            validate_trace(screens)

    def test_empty_actions_rejected(self):
        screens = [{"name": "home", "actions": []}]
        with pytest.raises(ValueError, match="non-empty list"):
            validate_trace(screens)

    def test_unknown_screen_key_rejected(self):
        screens = [{"name": "home", "actions": [{"action": "goto", "url": "x"}],
                     "extra": "bad"}]
        with pytest.raises(ValueError, match="unknown keys"):
            validate_trace(screens)


class TestValidateTraceRequiredParams:
    def test_goto_missing_url(self):
        screens = [{"name": "home", "actions": [{"action": "goto"}]}]
        with pytest.raises(ValueError, match="missing required param 'url'"):
            validate_trace(screens)

    def test_click_missing_selector(self):
        screens = [{"name": "home", "actions": [{"action": "click"}]}]
        with pytest.raises(ValueError, match="missing required param 'selector'"):
            validate_trace(screens)

    def test_fill_missing_text(self):
        screens = [{"name": "home", "actions": [
            {"action": "fill", "selector": "#i"}]}]
        with pytest.raises(ValueError, match="missing required param 'text'"):
            validate_trace(screens)

    def test_screenshot_missing_name(self):
        screens = [{"name": "home", "actions": [{"action": "screenshot"}]}]
        with pytest.raises(ValueError, match="missing required param 'name'"):
            validate_trace(screens)

    def test_unknown_param_rejected(self):
        screens = [{"name": "home", "actions": [
            {"action": "goto", "url": "x", "bad": "extra"}]}]
        with pytest.raises(ValueError, match="unknown params"):
            validate_trace(screens)


class TestValidateTraceWaitForState:
    def test_valid_states_accepted(self):
        for state in ("visible", "hidden", "attached", "detached"):
            screens = [{"name": "s", "actions": [
                {"action": "wait_for", "selector": "#el", "state": state}]}]
            validate_trace(screens)

    def test_unknown_state_rejected(self):
        screens = [{"name": "s", "actions": [
            {"action": "wait_for", "selector": "#el", "state": "enabled"}]}]
        with pytest.raises(ValueError, match="unknown state 'enabled'"):
            validate_trace(screens)

    def test_default_state_visible(self):
        # state is optional; defaults to "visible" — no raise.
        screens = [{"name": "s", "actions": [
            {"action": "wait_for", "selector": "#el"}]}]
        validate_trace(screens)


class TestValidateTracePressKey:
    def test_valid_keys_accepted(self):
        for key in ("Enter", "Tab", "Escape", "ArrowUp", "Space"):
            screens = [{"name": "s", "actions": [
                {"action": "press", "selector": "#el", "key": key}]}]
            validate_trace(screens)

    def test_unknown_key_rejected(self):
        screens = [{"name": "s", "actions": [
            {"action": "press", "selector": "#el", "key": "Ctrl+A"}]}]
        with pytest.raises(ValueError, match="unknown key 'Ctrl.A'"):
            validate_trace(screens)


class TestValidateTraceOptionalParams:
    def test_click_timeout_ms_accepted(self):
        screens = [{"name": "s", "actions": [
            {"action": "click", "selector": "#el", "timeout_ms": "5000"}]}]
        validate_trace(screens)

    def test_scroll_xy_accepted(self):
        screens = [{"name": "s", "actions": [
            {"action": "scroll", "selector": "#el", "x": "0", "y": "100"}]}]
        validate_trace(screens)

    def test_screenshot_full_page_accepted(self):
        screens = [{"name": "s", "actions": [
            {"action": "screenshot", "name": "s", "full_page": True}]}]
        validate_trace(screens)

    def test_wait_for_timeout_ms_accepted(self):
        screens = [{"name": "s", "actions": [
            {"action": "wait_for", "selector": "#el", "timeout_ms": "5000"}]}]
        validate_trace(screens)
