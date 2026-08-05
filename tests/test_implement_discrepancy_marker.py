"""F1: the implement() escalation contract is now an explicit
`PLAN_DISCREPANCY:` line-prefix marker, not the old `"discrepancy"+"plan"`
substring pair. Prose mentioning both words must NOT escalate; a genuine
marker line must.

Both tests set ``C.DRY_RUN=True`` so the implement.py git block (the
``git add -A`` + WIP commit at first entry) is a no-op and the real working
tree is never mutated (C10).
"""
import sys
from unittest.mock import patch

from pipeline_graph import config as C
from pipeline_graph import nodes as N

_impl = sys.modules["pipeline_graph.nodes.implement"]


def _base_state(**kw) -> dict:
    st = {
        "task_id": "disc",
        "batch_idx": 0,
        "batches": [{"n": 1, "scope": "s", "checklist": [1]}],
        "fix_cycle": 0,
        "test_fix_attempt": 0,
        "test_fix_failures": [],
        "test_fix_summary": "",
        "escalation": "",
        "journal": [],
    }
    st.update(kw)
    return st


class TestPlanDiscrepancyMarker:
    def test_no_discrepancy_phrase_does_not_escalate(self, monkeypatch):
        monkeypatch.setattr(C, "DRY_RUN", True)
        # Prose that mentions both "discrepancy" and "plan" but emits no
        # PLAN_DISCREPANCY: marker line must NOT escalate under the new contract.
        out = (
            "I reviewed the plan and found no discrepancy with the code. "
            "Proceeding with the batch as specified.\n"
            "VERDICT: APPROVE\n" * 5
        )
        state = _base_state()
        with patch.object(N, "run_agent", return_value=(0, out)), \
                patch.object(_impl, "_in_graph_test_gate",
                             return_value=(True, [], "all passed", 0)), \
                patch.object(N.ev, "emit"):
            d = N.implement(state)
        assert "escalation" not in d or d.get("escalation") == "", \
            "prose mentioning 'discrepancy'+'plan' must not escalate without the marker"

    def test_plan_discrepancy_marker_escalates(self, monkeypatch):
        monkeypatch.setattr(C, "DRY_RUN", True)
        marker = "PLAN_DISCREPANCY: pipeline_graph/nodes/x.py does not exist (plan says MODIFY it)"
        out = f"Some preamble.\n{marker}\nVERDICT: APPROVE\n" * 3
        state = _base_state()
        with patch.object(N, "run_agent", return_value=(0, out)), \
                patch.object(_impl, "_in_graph_test_gate",
                             return_value=(True, [], "all passed", 0)), \
                patch.object(N.ev, "emit"):
            d = N.implement(state)
        assert d.get("escalation"), "PLAN_DISCREPANCY: marker must escalate"
        assert "batch 1" in d["escalation"]
        assert marker in d["escalation"]
        assert any("PLAN_DISCREPANCY" in line for line in d.get("journal", [])), \
            "journal must record the PLAN_DISCREPANCY escalation"

    def test_noop_discrepancy_marker_does_not_escalate(self, monkeypatch):
        """Agents sometimes emit ``PLAN_DISCREPANCY: none``; that must not pause."""
        monkeypatch.setattr(C, "DRY_RUN", True)
        out = (
            "Batch complete.\n"
            "PLAN_DISCREPANCY: none\n"
            "1: MET — scripts/wt.py\n"
            "VERDICT: APPROVE\n" * 3
        )
        state = _base_state()
        with patch.object(N, "run_agent", return_value=(0, out)), \
                patch.object(_impl, "_in_graph_test_gate",
                             return_value=(True, [], "all passed", 0)), \
                patch.object(N.ev, "emit"):
            d = N.implement(state)
        assert "escalation" not in d or d.get("escalation") == "", \
            "PLAN_DISCREPANCY: none must not escalate"
