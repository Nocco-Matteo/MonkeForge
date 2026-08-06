"""Trace validation for the eyes runner — closed action allowlist, required
params, ``wait_for.state`` / ``press.key`` enums. Pure, no I/O.

Called by the runner immediately before browser launch so an invalid trace
fails loud and early (acceptance 3 / brief §3 "Evals, custom scripts, and
non-allowlisted actions → fail-loud before browser start").
"""
from __future__ import annotations

from .. import config as C


def validate_trace(screens: list) -> None:
    """Validate a list of screen objects against the closed action allowlist.

    Raises ``ValueError`` on:
    - non-list / empty screens
    - unknown screen keys (only ``name`` + ``actions`` allowed)
    - missing ``name`` or empty ``actions``
    - unknown action (not in the allowlist)
    - missing required params per action
    - unknown action params
    - unknown ``wait_for.state``
    - unknown ``press.key``

    Pure — no browser, no I/O. The action/state/key enums are sourced from
    ``config._EYES_ACTION_ALLOWLIST`` / ``_EYES_WAIT_FOR_STATES`` /
    ``_EYES_PRESS_KEYS`` so there is one source of truth shared with
    ``validate_ui_config``.
    """
    if not isinstance(screens, list) or not screens:
        raise ValueError("screens must be a non-empty list")
    for i, screen in enumerate(screens):
        if not isinstance(screen, dict):
            raise ValueError(f"ui.screens[{i}]: not a mapping")
        unknown = set(screen.keys()) - C._EYES_SCREEN_KEYS
        if unknown:
            raise ValueError(f"ui.screens[{i}]: unknown keys {sorted(unknown)}")
        if not str(screen.get("name") or "").strip():
            raise ValueError(f"ui.screens[{i}]: missing 'name'")
        actions = screen.get("actions")
        if not isinstance(actions, list) or not actions:
            raise ValueError(f"ui.screens[{i}]: 'actions' must be a non-empty list")
        for j, act in enumerate(actions):
            if not isinstance(act, dict):
                raise ValueError(f"ui.screens[{i}].actions[{j}]: not a mapping")
            action = str(act.get("action") or "").strip()
            if action not in C._EYES_ACTION_ALLOWLIST:
                raise ValueError(
                    f"ui.screens[{i}].actions[{j}]: unknown action {action!r} "
                    f"(allowlist: {sorted(C._EYES_ACTION_ALLOWLIST)})")
            required = C._EYES_ACTION_REQUIRED[action]
            for req in required:
                if req not in act or str(act.get(req) or "").strip() == "":
                    raise ValueError(
                        f"ui.screens[{i}].actions[{j}] ({action}): "
                        f"missing required param {req!r}")
            allowed = frozenset({"action"}) | set(required) | C._EYES_ACTION_OPTIONAL[action]
            unknown_a = set(act.keys()) - allowed
            if unknown_a:
                raise ValueError(
                    f"ui.screens[{i}].actions[{j}] ({action}): "
                    f"unknown params {sorted(unknown_a)}")
            if action == "wait_for":
                st = str(act.get("state") or "visible").strip()
                if st not in C._EYES_WAIT_FOR_STATES:
                    raise ValueError(
                        f"ui.screens[{i}].actions[{j}] (wait_for): "
                        f"unknown state {st!r} "
                        f"(expected one of {sorted(C._EYES_WAIT_FOR_STATES)})")
            if action == "press":
                key = str(act.get("key") or "").strip()
                if key not in C._EYES_PRESS_KEYS:
                    raise ValueError(
                        f"ui.screens[{i}].actions[{j}] (press): "
                        f"unknown key {key!r} "
                        f"(expected one of {sorted(C._EYES_PRESS_KEYS)})")
