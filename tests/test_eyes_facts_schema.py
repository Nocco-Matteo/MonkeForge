"""Facts schema and legacy ingest tests for the eyes runner (TASK-012).

Validates the ``monkeforge.eyes.facts/v1`` shape and the legacy subprocess → v1
ingest map (alias mapping, pass-through, no key drops).
"""
import json

import pytest

from pipeline_graph.eyes.legacy_ingest import (
    normalize_legacy_facts,
    normalize_legacy_facts_json,
)


_SCHEMA = "monkeforge.eyes.facts/v1"


class TestNormalizeLegacyFacts:
    def test_empty_input_produces_v1_skeleton(self):
        out = normalize_legacy_facts({})
        assert out["schema"] == _SCHEMA
        assert out["console_errors"] == []
        assert out["failed_requests"] == []
        assert "legacy" in out["screens"]

    def test_non_dict_input_handled(self):
        out = normalize_legacy_facts("not a dict")
        assert out["schema"] == _SCHEMA

    def test_overflow_x_maps_to_has_overflow(self):
        out = normalize_legacy_facts({"overflow_x": 3})
        assert out["screens"]["legacy"]["has_overflow"] is True

    def test_overflow_x_zero_no_overflow(self):
        out = normalize_legacy_facts({"overflow_x": 0})
        assert out["screens"]["legacy"]["has_overflow"] is False

    def test_overflow_truthy_maps_to_has_overflow(self):
        out = normalize_legacy_facts({"overflow": True})
        assert out["screens"]["legacy"]["has_overflow"] is True

    def test_page_scroll_y_pass_through_and_overflow(self):
        out = normalize_legacy_facts({"page_scroll_y": 150})
        assert out["screens"]["legacy"]["has_overflow"] is True
        assert out["screens"]["legacy"]["extra"]["page_scroll_y"] == 150

    def test_page_scroll_y_zero_no_overflow(self):
        out = normalize_legacy_facts({"page_scroll_y": 0})
        assert out["screens"]["legacy"]["has_overflow"] is False

    def test_mode_identical_maps_to_is_stable(self):
        out = normalize_legacy_facts({"mode_identical": True})
        assert out["screens"]["legacy"]["is_stable"] is True
        assert out["screens"]["legacy"]["extra"]["mode_identical"] is True

    def test_mode_identical_false(self):
        out = normalize_legacy_facts({"mode_identical": False})
        assert out["screens"]["legacy"]["is_stable"] is False

    def test_mode_identical_absent_is_stable_none(self):
        out = normalize_legacy_facts({})
        assert out["screens"]["legacy"]["is_stable"] is None

    def test_board_coverage_pass_through(self):
        out = normalize_legacy_facts({"board_coverage": 0.75})
        assert out["screens"]["legacy"]["extra"]["board_coverage"] == 0.75

    def test_layout_shifts_maps_to_float(self):
        out = normalize_legacy_facts({"layout_shifts": "0.2"})
        assert out["screens"]["legacy"]["layout_shifts"] == 0.2

    def test_cls_maps_to_layout_shifts(self):
        out = normalize_legacy_facts({"cls": 0.15})
        assert out["screens"]["legacy"]["layout_shifts"] == 0.15

    def test_no_layout_shifts_defaults_zero(self):
        out = normalize_legacy_facts({})
        assert out["screens"]["legacy"]["layout_shifts"] == 0.0

    def test_unmapped_keys_pass_through_top_level(self):
        out = normalize_legacy_facts({"custom_key": "value"})
        assert out["custom_key"] == "value"

    def test_console_errors_pass_through(self):
        out = normalize_legacy_facts({"console_errors": ["err1", "err2"]})
        assert out["console_errors"] == ["err1", "err2"]

    def test_failed_requests_pass_through(self):
        out = normalize_legacy_facts({"failed_requests": ["req1"]})
        assert out["failed_requests"] == ["req1"]

    def test_v1_per_screen_defaults(self):
        out = normalize_legacy_facts({})
        s = out["screens"]["legacy"]
        assert s["page_loaded"] is True
        assert s["screenshot_empty"] is False
        assert s["interactive_count"] == 0

    def test_existing_screens_dict_merged(self):
        out = normalize_legacy_facts({"screens": {"existing": {"page_loaded": True}}})
        assert "existing" in out["screens"]
        assert "legacy" in out["screens"]

    def test_interactive_count_pass_through(self):
        out = normalize_legacy_facts({"interactive_count": 5})
        # interactive_count is not in the legacy map — it passes through at
        # top level (unmapped key).
        assert out.get("interactive_count") == 5


class TestNormalizeLegacyFactsJson:
    def test_valid_json(self):
        raw = json.dumps({"overflow_x": 1, "mode_identical": True})
        out = json.loads(normalize_legacy_facts_json(raw))
        assert out["schema"] == _SCHEMA
        assert out["screens"]["legacy"]["has_overflow"] is True
        assert out["screens"]["legacy"]["is_stable"] is True

    def test_empty_string(self):
        out = json.loads(normalize_legacy_facts_json(""))
        assert out["schema"] == _SCHEMA

    def test_invalid_json(self):
        out = json.loads(normalize_legacy_facts_json("not json"))
        assert out["schema"] == _SCHEMA


class TestFactsSchemaV1:
    def test_schema_string(self):
        assert _SCHEMA == "monkeforge.eyes.facts/v1"

    def test_v1_screenshot_keys(self):
        out = normalize_legacy_facts({})
        s = out["screens"]["legacy"]
        # The v1 per-screen keys per the brief.
        for key in ("page_loaded", "screenshot_empty", "has_overflow",
                    "layout_shifts", "is_stable", "interactive_count"):
            assert key in s, f"missing v1 per-screen key: {key}"

    def test_v1_top_level_keys(self):
        out = normalize_legacy_facts({})
        for key in ("schema", "console_errors", "failed_requests", "screens"):
            assert key in out, f"missing v1 top-level key: {key}"
