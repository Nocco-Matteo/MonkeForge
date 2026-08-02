"""F4: judge() strips only the fenced BATCHES json block from FINAL-{tid}.md,
preserving unrelated fenced JSON in the judge prose. The BATCHES-{tid}.json
file is written unchanged.

Both cases patch ``C.FINAL`` to ``tmp_path`` and use the real ``_extract_json``
and real ``_save`` (no stubs) so the strip helper's value-equality match is
exercised against the same extraction production uses. The BATCHES fenced
block is placed BEFORE the unrelated example in stdout so the first-match
extraction order is the one under test.

Case 1: BATCHES in a ```json fence; the unrelated example is a second ```json
        block with different (object) content.
Case 2: BATCHES in a plain ``` fence; stdout contains NO ```json block at all,
        so the plain-fence array extraction path is exercised for real. The
        unrelated example is a plain ``` block with non-array content.
"""
import json
import sys
from unittest.mock import patch

from pipeline_graph import config as C
from pipeline_graph import nodes as N
from pipeline_graph.nodes.common import _extract_json

_finalize = sys.modules["pipeline_graph.nodes.finalize"]


def _state(tid: str = "strip") -> dict:
    return {
        "task_id": tid,
        "batch_idx": 0,
        "batches": [],
        "journal": [],
        "escalation": "",
    }


def _run_judge(monkeypatch, tmp_path, out, tid="strip"):
    monkeypatch.setattr(C, "FINAL", tmp_path)
    state = _state(tid)
    with patch.object(N, "run_agent", return_value=(0, out)), \
            patch.object(_finalize, "_write_progress"):
        return _finalize.judge(state)


BATCHES = [
    {"n": 1, "scope": "first", "checklist": [1, 2]},
    {"n": 2, "scope": "second", "checklist": [3]},
]


class TestStripsBatchesBlock:
    def test_strips_batches_json_fence_preserves_example(self, monkeypatch, tmp_path):
        batches_block = "```json\n" + json.dumps(BATCHES, indent=2) + "\n```"
        # A second, distinct ```json block (object, not the batches array) plus
        # the HAS_UI line must survive the strip.
        example_block = '```json\n{"example": true, "note": "kept"}\n```'
        out = (
            "Judge report — verdict and reasoning.\n"
            "HAS_UI: NO\n\n"
            f"{batches_block}\n\n"
            f"{example_block}\n"
        )
        d = _run_judge(monkeypatch, tmp_path, out)
        assert not d.get("escalation"), f"valid batches must not escalate: {d.get('escalation')}"

        final_text = (tmp_path / "FINAL-strip.md").read_text()
        # The BATCHES block is gone (no fenced block parses to BATCHES).
        assert json.dumps(BATCHES, indent=2) not in final_text, \
            "FINAL still contains the BATCHES json block"
        assert _extract_json(final_text) != BATCHES, \
            "FINAL still has a fenced block parsing to the BATCHES array"
        # The unrelated example and HAS_UI survive.
        assert '{"example": true, "note": "kept"}' in final_text
        assert "HAS_UI: NO" in final_text

        # BATCHES-*.json is unchanged (what judge wrote).
        saved = json.loads((tmp_path / "BATCHES-strip.json").read_text())
        assert saved == BATCHES

    def test_strips_batches_plain_fence_preserves_example(self, monkeypatch, tmp_path):
        # No ```json block at all — the plain-fence array extraction path is
        # exercised for real.
        batches_block = "```\n" + json.dumps(BATCHES) + "\n```"
        example_block = '```\nthis is a prose example, not json\n```'
        out = (
            "Judge report — verdict and reasoning.\n"
            "HAS_UI: NO\n\n"
            f"{batches_block}\n\n"
            f"{example_block}\n"
        )
        d = _run_judge(monkeypatch, tmp_path, out)
        assert not d.get("escalation"), f"valid batches must not escalate: {d.get('escalation')}"

        final_text = (tmp_path / "FINAL-strip.md").read_text()
        assert _extract_json(final_text) != BATCHES, \
            "FINAL still has a fenced block parsing to the BATCHES array"
        # The unrelated plain-fence example and HAS_UI survive.
        assert "this is a prose example, not json" in final_text
        assert "HAS_UI: NO" in final_text

        saved = json.loads((tmp_path / "BATCHES-strip.json").read_text())
        assert saved == BATCHES
