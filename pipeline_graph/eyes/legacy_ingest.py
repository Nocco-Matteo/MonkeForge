"""Legacy subprocess facts → ``monkeforge.eyes.facts/v1`` ingest map.

Called from ``quality_gates.ux_render`` on the compat-subprocess path BEFORE
facts reach ``ux_visual_review`` so ``render_facts`` is always v1. Unmapped
keys pass through (top level or under ``screens.legacy.extra``); per-screen
nesting uses synthetic name ``legacy`` when the subprocess emits one flat
object.
"""
from __future__ import annotations

import json

_SCHEMA = "monkeforge.eyes.facts/v1"


def normalize_legacy_facts(raw: dict) -> dict:
    """Map a legacy subprocess facts dict into the v1 shape.

    Map (brief §3 "Legacy → generic ingest map"):
    - ``overflow_x`` (truthy / >0) or legacy ``overflow`` →
      ``screens.legacy.has_overflow`` = True
    - ``page_scroll_y`` → pass-through; if >0 may set ``has_overflow`` when
      ``overflow_x`` absent
    - ``mode_identical`` → ``screens.legacy.is_stable`` (bool) AND keep
      ``mode_identical`` pass-through
    - ``board_coverage`` → pass-through only
    - ``layout_shifts`` / ``cls`` if present → ``screens.legacy.layout_shifts``

    Unmapped keys pass through at top level or under ``screens.legacy.extra``.
    Do not drop legacy keys after mapping. Sets ``schema`` to v1.
    """
    if not isinstance(raw, dict):
        raw = {}
    # Start from the raw dict (unmapped keys pass through at top level).
    out: dict = dict(raw)
    out["schema"] = _SCHEMA

    # Ensure global lists exist in v1 shape.
    out.setdefault("console_errors", [])
    out.setdefault("failed_requests", [])

    # Per-screen nesting: synthetic name "legacy" for flat subprocess output.
    screen: dict = {}
    extra: dict = {}

    # overflow_x / overflow → has_overflow
    has_overflow = False
    overflow_x = raw.get("overflow_x")
    overflow = raw.get("overflow")
    if (overflow_x is not None and (overflow_x is True or (
            isinstance(overflow_x, (int, float)) and overflow_x > 0))):
        has_overflow = True
    elif overflow is not None and (overflow is True or (
            isinstance(overflow, (int, float)) and overflow > 0)):
        has_overflow = True
    # page_scroll_y pass-through; set has_overflow when >0 and overflow_x absent
    page_scroll_y = raw.get("page_scroll_y")
    if page_scroll_y is not None:
        extra["page_scroll_y"] = page_scroll_y
        if not has_overflow and isinstance(page_scroll_y, (int, float)) and page_scroll_y > 0:
            has_overflow = True
    screen["has_overflow"] = has_overflow

    # mode_identical → is_stable (bool) + pass-through
    mode_identical = raw.get("mode_identical")
    if mode_identical is not None:
        # is_stable is the boolean view; mode_identical stays as pass-through.
        screen["is_stable"] = bool(mode_identical)
        extra["mode_identical"] = mode_identical
    else:
        screen["is_stable"] = None

    # board_coverage pass-through
    board_coverage = raw.get("board_coverage")
    if board_coverage is not None:
        extra["board_coverage"] = board_coverage

    # layout_shifts / cls → screens.legacy.layout_shifts (float)
    layout_shifts = raw.get("layout_shifts")
    cls = raw.get("cls")
    if layout_shifts is not None:
        try:
            screen["layout_shifts"] = float(layout_shifts)
        except (TypeError, ValueError):
            extra["layout_shifts"] = layout_shifts
    elif cls is not None:
        try:
            screen["layout_shifts"] = float(cls)
        except (TypeError, ValueError):
            extra["cls"] = cls
    else:
        screen["layout_shifts"] = 0.0

    # v1 per-screen keys with defaults.
    screen.setdefault("page_loaded", True)
    screen.setdefault("screenshot_empty", False)
    screen.setdefault("interactive_count", 0)

    if extra:
        screen["extra"] = extra

    screens = out.get("screens")
    if isinstance(screens, dict) and screens:
        # If the legacy output already has per-screen nesting, merge our
        # synthetic screen under "legacy" without dropping existing keys.
        screens.setdefault("legacy", screen)
        out["screens"] = screens
    else:
        out["screens"] = {"legacy": screen}

    return out


def normalize_legacy_facts_json(raw_json: str) -> str:
    """Parse + normalize a raw JSON string, returning v1 JSON."""
    try:
        raw = json.loads(raw_json) if raw_json else {}
    except (json.JSONDecodeError, TypeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return json.dumps(normalize_legacy_facts(raw), indent=2)
