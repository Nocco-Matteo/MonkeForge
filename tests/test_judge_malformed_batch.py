"""F2: judge() must escalate cleanly (no KeyError/AttributeError) on a
malformed BATCHES item — a non-dict element, a missing ``n``, or a non-int
``n``. A well-formed BATCHES array is the regression guard.

Each case patches ``C.FINAL`` to ``tmp_path`` and uses the real ``_save`` (no
stub) so the BATCHES file is actually written and read back through the same
path production uses. ``_extract_json`` is patched to return the test's
BATCHES array directly (the malformed shapes are the unit under test, not
extraction).
"""
import sys
from unittest.mock import patch

from pipeline_graph import config as C
from pipeline_graph import nodes as N

_finalize = sys.modules["pipeline_graph.nodes.finalize"]


def _state(tid: str = "mb") -> dict:
    return {
        "task_id": tid,
        "batch_idx": 0,
        "batches": [],
        "journal": [],
        "escalation": "",
    }


def _run_judge(monkeypatch, tmp_path, batches_json, tid="mb"):
    monkeypatch.setattr(C, "FINAL", tmp_path)
    # Long enough to clear MIN_OUTPUT_BYTES; the BATCHES block is what judge parses.
    out = "Judge report preamble — verdict and reasoning follow.\nHAS_UI: NO\n"
    state = _state(tid)
    with patch.object(N, "run_agent", return_value=(0, out)), \
            patch.object(_finalize, "_extract_json", return_value=batches_json), \
            patch.object(_finalize, "_write_progress"):
        return _finalize.judge(state)


class TestJudgeMalformedBatch:
    def test_missing_n_escalates(self, monkeypatch, tmp_path):
        d = _run_judge(monkeypatch, tmp_path, [{"scope": "s", "checklist": [1]}])
        assert "malformed batch" in d["escalation"]
        assert "n" in d["escalation"]

    def test_non_int_n_escalates(self, monkeypatch, tmp_path):
        d = _run_judge(monkeypatch, tmp_path, [{"n": "1", "scope": "s"}])
        assert "malformed batch" in d["escalation"]

    def test_non_dict_element_escalates(self, monkeypatch, tmp_path):
        d = _run_judge(monkeypatch, tmp_path, [{"n": 1, "scope": "s"}, "bad"])
        assert "malformed batch" in d["escalation"]
        assert "not an object" in d["escalation"]

    def test_valid_batches_regresses_cleanly(self, monkeypatch, tmp_path):
        batches_json = [
            {"n": 1, "scope": "first", "checklist": [1, 2]},
            {"n": 2, "scope": "second", "checklist": [3]},
        ]
        d = _run_judge(monkeypatch, tmp_path, batches_json)
        assert not d.get("escalation"), f"valid batches must not escalate: {d.get('escalation')}"
        assert d["batch_idx"] == 0
        assert [b["n"] for b in d["batches"]] == [1, 2]


class TestJudgeEscalateNoneIsNoop:
    """TASK-022: ``ESCALATE: none — …`` must not pause the run."""

    def _run(self, monkeypatch, tmp_path, first_line: str):
        monkeypatch.setattr(C, "FINAL", tmp_path)
        batches = [{"n": 1, "scope": "s", "checklist": [1]}]
        out = (
            f"{first_line}\n\n"
            "Judge report body with rulings.\n"
            "HAS_UI: NO\n"
        )
        state = _state("je")
        with patch.object(N, "run_agent", return_value=(0, out)), \
                patch.object(_finalize, "_extract_json", return_value=batches), \
                patch.object(_finalize, "_write_progress"):
            return _finalize.judge(state)

    def test_none_emdash_is_noop(self, monkeypatch, tmp_path):
        d = self._run(
            monkeypatch, tmp_path,
            "ESCALATE: none — both contested items are verifiable from the repo",
        )
        assert not d.get("escalation"), d.get("escalation")
        assert len(d.get("batches", [])) == 1

    def test_bare_none_is_noop(self, monkeypatch, tmp_path):
        d = self._run(monkeypatch, tmp_path, "ESCALATE: none")
        assert not d.get("escalation"), d.get("escalation")

    def test_no_issues_is_noop(self, monkeypatch, tmp_path):
        d = self._run(monkeypatch, tmp_path, "ESCALATE: no issues")
        assert not d.get("escalation"), d.get("escalation")

    def test_real_escalate_still_fires(self, monkeypatch, tmp_path):
        d = self._run(
            monkeypatch, tmp_path,
            "ESCALATE: product trade-off on notify defaults — needs human",
        )
        assert d.get("escalation", "").startswith("judge escalated:")
        assert "product trade-off" in d["escalation"]


class TestValidateBatchesSchema:
    """F2: validate_batches_schema — extracted from finalize.judge."""

    def test_valid_batches_returned(self):
        from pipeline_graph.nodes.common import validate_batches_schema
        raw = [{"n": 1, "scope": "s", "checklist": [1, 2]}]
        batches, err = validate_batches_schema(raw)
        assert err is None
        assert len(batches) == 1
        assert batches[0]["n"] == 1
        assert batches[0]["status"] == "PENDING"

    def test_non_dict_element_rejected(self):
        from pipeline_graph.nodes.common import validate_batches_schema
        batches, err = validate_batches_schema([{"n": 1}, "bad"])
        assert batches is None
        assert "not an object" in err

    def test_missing_n_rejected(self):
        from pipeline_graph.nodes.common import validate_batches_schema
        batches, err = validate_batches_schema([{"scope": "s"}])
        assert batches is None
        assert "missing or non-integer n" in err

    def test_non_int_n_rejected(self):
        from pipeline_graph.nodes.common import validate_batches_schema
        batches, err = validate_batches_schema([{"n": "1"}])
        assert batches is None
        assert "missing or non-integer n" in err

    def test_bool_n_rejected(self):
        from pipeline_graph.nodes.common import validate_batches_schema
        batches, err = validate_batches_schema([{"n": True}])
        assert batches is None
        assert "missing or non-integer n" in err

    def test_non_list_rejected(self):
        from pipeline_graph.nodes.common import validate_batches_schema
        batches, err = validate_batches_schema({"n": 1})
        assert batches is None
        assert "not a list of objects" in err

    def test_list_of_scalars_rejected(self):
        from pipeline_graph.nodes.common import validate_batches_schema
        batches, err = validate_batches_schema(["baseline_failures"])
        assert batches is None
        assert "not a list of objects" in err

    def test_empty_list_accepted(self):
        from pipeline_graph.nodes.common import validate_batches_schema
        batches, err = validate_batches_schema([])
        assert err is None
        assert batches == []

    def test_defaults_filled(self):
        from pipeline_graph.nodes.common import validate_batches_schema
        batches, _ = validate_batches_schema([{"n": 1}])
        assert batches[0]["scope"] == ""
        assert batches[0]["checklist"] == []
        assert batches[0]["test_failure_allowlist"] == []

    def test_non_string_scope_rejected(self):
        from pipeline_graph.nodes.common import validate_batches_schema
        batches, err = validate_batches_schema([{"n": 1, "scope": 123}])
        assert batches is None
        assert "bad scope" in err

    def test_non_list_checklist_rejected(self):
        from pipeline_graph.nodes.common import validate_batches_schema
        batches, err = validate_batches_schema([{"n": 1, "checklist": "not a list"}])
        assert batches is None
        assert "bad checklist" in err

    def test_non_list_allowlist_rejected(self):
        from pipeline_graph.nodes.common import validate_batches_schema
        batches, err = validate_batches_schema([{"n": 1, "test_failure_allowlist": "not a list"}])
        assert batches is None
        assert "bad allowlist" in err

    def test_non_string_allowlist_element_rejected(self):
        from pipeline_graph.nodes.common import validate_batches_schema
        batches, err = validate_batches_schema([{"n": 1, "test_failure_allowlist": [123]}])
        assert batches is None
        assert "bad allowlist" in err


class TestParseVerifyStatuses:
    """F3: parse_verify_statuses — returns a list of line-anchored status
    markers (CONFIRMED/NOT_FIXED)."""

    def test_standalone_not_fixed_detected(self):
        from pipeline_graph.nodes.common import parse_verify_statuses
        assert "NOT_FIXED" in parse_verify_statuses("item 1: NOT_FIXED — still broken")

    def test_not_fixed_in_prose_not_detected(self):
        from pipeline_graph.nodes.common import parse_verify_statuses
        # The word NOT_FIXED embedded in prose (not as a status marker) —
        # the old substring check would false-positive here.
        assert "NOT_FIXED" not in parse_verify_statuses("The previous NOT_FIXED was resolved")

    def test_clean_output_not_detected(self):
        from pipeline_graph.nodes.common import parse_verify_statuses
        statuses = parse_verify_statuses("item 1: CONFIRMED\nitem 2: CONFIRMED")
        assert "NOT_FIXED" not in statuses
        assert "CONFIRMED" in statuses

    def test_empty_not_detected(self):
        from pipeline_graph.nodes.common import parse_verify_statuses
        assert parse_verify_statuses("") == []
        assert parse_verify_statuses(None) == []

    def test_case_insensitive(self):
        from pipeline_graph.nodes.common import parse_verify_statuses
        assert "NOT_FIXED" in parse_verify_statuses("not_fixed")
        assert "NOT_FIXED" in parse_verify_statuses("Not_Fixed")

    def test_returns_list_not_bool(self):
        from pipeline_graph.nodes.common import parse_verify_statuses
        result = parse_verify_statuses("item 1: NOT_FIXED — broken\nitem 2: CONFIRMED\n")
        assert isinstance(result, list)
        assert "NOT_FIXED" in result
        assert "CONFIRMED" in result


class TestParseDeviationsLine:
    """F3: parse_deviations_line — line-anchored DEVIATIONS: parsing.
    Returns "none" when no DEVIATIONS: line is found."""

    def test_deviations_line_extracted(self):
        from pipeline_graph.nodes.common import parse_deviations_line
        text = "some output\nDEVIATIONS: skipped test X\nmore output"
        assert parse_deviations_line(text) == "skipped test X"

    def test_deviations_none(self):
        from pipeline_graph.nodes.common import parse_deviations_line
        text = "some output\nDEVIATIONS: none\nmore"
        assert parse_deviations_line(text) == "none"

    def test_no_deviations_line_returns_none(self):
        from pipeline_graph.nodes.common import parse_deviations_line
        assert parse_deviations_line("no markers here") == "none"

    def test_deviations_in_prose_not_extracted(self):
        from pipeline_graph.nodes.common import parse_deviations_line
        # The word DEVIATIONS in prose (not at line start) — old substring
        # approach would grab everything after it.
        text = "We discussed DEVIATIONS from the plan and found none."
        assert parse_deviations_line(text) == "none"

    def test_empty_input(self):
        from pipeline_graph.nodes.common import parse_deviations_line
        assert parse_deviations_line("") == "none"
        assert parse_deviations_line(None) == "none"

    def test_case_insensitive(self):
        from pipeline_graph.nodes.common import parse_deviations_line
        assert parse_deviations_line("deviations: test") == "test"
        assert parse_deviations_line("Deviations: test") == "test"


class TestBatchesUnlinkOnCorrupt:
    """F2: a corrupt BATCHES-{tid}.json file is unlinked before escalating."""

    def test_corrupt_json_unlinked_and_escalates(self, monkeypatch, tmp_path):
        batches_file = tmp_path / "BATCHES-mb.json"
        batches_file.write_text("{not valid json")
        monkeypatch.setattr(C, "FINAL", tmp_path)
        out = "Judge report — no BATCHES in stdout.\nHAS_UI: NO\n"
        state = _state("mb")
        with patch.object(N, "run_agent", return_value=(0, out)), \
                patch.object(_finalize, "_write_progress"):
            d = _finalize.judge(state)
        assert "BATCHES json invalid" in d["escalation"]
        # The corrupt file was unlinked.
        assert not batches_file.exists()

    def test_bad_schema_unlinked_and_escalates(self, monkeypatch, tmp_path):
        # A valid JSON file with a bad schema (missing n) — must be unlinked.
        batches_file = tmp_path / "BATCHES-mb.json"
        batches_file.write_text('[{"scope": "s"}]')
        monkeypatch.setattr(C, "FINAL", tmp_path)
        out = "Judge report — no BATCHES in stdout.\nHAS_UI: NO\n"
        state = _state("mb")
        with patch.object(N, "run_agent", return_value=(0, out)), \
                patch.object(_finalize, "_write_progress"):
            d = _finalize.judge(state)
        assert "malformed batch" in d["escalation"]
        # The bad-schema file was unlinked.
        assert not batches_file.exists()


class TestJudgeFilePrimaryRescue:
    """Near-empty stdout (HAS_UI trailer only) must not discard on-disk artifacts."""

    def test_has_ui_only_rescues_valid_files(self, monkeypatch, tmp_path):
        monkeypatch.setattr(C, "FINAL", tmp_path)
        batches_file = tmp_path / "BATCHES-027.json"
        final_path = tmp_path / "FINAL-027.md"
        batches_file.write_text(
            '[{"n": 1, "scope": "cli", "checklist": [1, 2], '
            '"test_failure_allowlist": []}]\n'
        )
        final_body = (
            "# FINAL-027\n\n## 1. Rulings\n\n"
            "- B1 — RULED FOR REVIEWER with enough text to clear MIN_OUTPUT_BYTES.\n"
        )
        assert len(final_body) >= 40
        final_path.write_text(final_body)
        state = _state("027")
        with patch.object(N, "run_agent", return_value=(0, "HAS_UI: YES\n")), \
                patch.object(_finalize, "_write_progress"):
            d = _finalize.judge(state)
        assert not d.get("escalation"), d
        assert d["batches"][0]["n"] == 1
        assert "rescued on-disk" in d["journal"][0]
        # Must not clobber FINAL with the HAS_UI trailer.
        assert final_path.read_text() == final_body

    def test_has_ui_only_without_final_still_escalates(self, monkeypatch, tmp_path):
        monkeypatch.setattr(C, "FINAL", tmp_path)
        (tmp_path / "BATCHES-027.json").write_text(
            '[{"n": 1, "scope": "cli", "checklist": [1]}]\n'
        )
        state = _state("027")
        with patch.object(N, "run_agent", return_value=(0, "HAS_UI: YES\n")), \
                patch.object(_finalize, "_write_progress"), \
                patch.object(_finalize, "_recover_artifact"):
            d = _finalize.judge(state)
        assert "untrustworthy" in d.get("escalation", "")
