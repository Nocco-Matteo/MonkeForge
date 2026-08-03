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
