"""Tests for the frozen `Conversation` snapshot (TASK-005).

Covers the conformance checklist for FINAL-005 batch 1:
  - TestImmutability: `frozen=True` blocks reassignment; `journal: tuple` blocks
    in-place `append` (AttributeError).
  - TestFromStateMapping: `from_state` field-by-field mapping — task_id/request
    from state, brief/plan/debate_history from disk, batch_context as JSON,
    review_history with `--- STEM ---` separators, journal copied as a tuple.
  - TestRenderPromptFlatMapping: `render_prompt` flat-maps `asdict(conversation)`
    + `extra_kw` (extra_kw wins), and renders list/tuple values as
    newline-joined clean text (P1 template safety).
  - TestCallSiteSignatures: AST-scans every `run_agent(`/`_N.run_agent(` call in
    `pipeline_graph/nodes/*.py` and asserts <=3 positional args and a non-string
    2nd positional arg — closes the signature-safety gap left by the smoke test
    for `_N.run_agent` nodes (whose patch swallows any signature).
"""
from __future__ import annotations

import ast
import dataclasses
import json
from pathlib import Path

import pytest

from pipeline_graph import config as C
from pipeline_graph.agents import render_prompt
from pipeline_graph.state import Conversation


def _conv(**overrides) -> Conversation:
    """Build a Conversation with all-empty defaults; override per-test fields."""
    defaults = dict(
        task_id="t",
        request="",
        brief="",
        plan="",
        debate_history="",
        debate_ledger="",
        batch_context="{}",
        review_history="",
        final="",
        progress="",
        summary="",
        visual_review="",
        journal=(),
    )
    defaults.update(overrides)
    return Conversation(**defaults)


# --- immutability -----------------------------------------------------------


class TestImmutability:
    def test_frozen_assignment_raises(self):
        conv = _conv()
        with pytest.raises(dataclasses.FrozenInstanceError):
            conv.task_id = "x"

    def test_frozen_list_field_assignment_raises(self):
        conv = _conv(journal=("a", "b"))
        with pytest.raises(dataclasses.FrozenInstanceError):
            conv.journal = ()

    def test_journal_is_tuple_and_in_place_mutation_unavailable(self):
        conv = _conv(journal=("a", "b"))
        assert isinstance(conv.journal, tuple)
        assert conv.journal == ("a", "b")
        # Tuples have no `append` — the read-only-for-agents contract holds
        # against in-place mutation, not just reassignment.
        with pytest.raises(AttributeError):
            conv.journal.append("x")


# --- from_state field mapping -----------------------------------------------


class TestFromStateMapping:
    def test_task_id_and_request_taken_from_state(self):
        conv = Conversation.from_state({"task_id": "t1", "request": "r"})
        assert conv.task_id == "t1"
        assert conv.request == "r"

    def test_brief_read_from_disk(self, monkeypatch, tmp_path):
        monkeypatch.setattr(C, "TASKS", tmp_path)
        (tmp_path / "TASK-t1-brief.md").write_text("the brief body")
        conv = Conversation.from_state({"task_id": "t1", "request": ""})
        assert conv.brief == "the brief body"

    def test_brief_missing_is_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(C, "TASKS", tmp_path)
        conv = Conversation.from_state({"task_id": "t1", "request": ""})
        assert conv.brief == ""

    def test_plan_read_from_disk(self, monkeypatch, tmp_path):
        monkeypatch.setattr(C, "PLANS", tmp_path)
        (tmp_path / "PLAN-t1.md").write_text("the plan body")
        conv = Conversation.from_state({"task_id": "t1", "request": ""})
        assert conv.plan == "the plan body"

    def test_debate_history_read_from_disk(self, monkeypatch, tmp_path):
        monkeypatch.setattr(C, "DEBATES", tmp_path)
        (tmp_path / "DEBATE-t1.md").write_text("the debate body")
        conv = Conversation.from_state({"task_id": "t1", "request": ""})
        assert conv.debate_history == "the debate body"

    def test_debate_ledger_read_from_disk(self, monkeypatch, tmp_path):
        """debate_ledger is populated from the same disk read as debate_history
        (C5/C7 regression): non-empty, starts with the ledger header, contains
        each raised item once; debate_history still equals the raw file body."""
        monkeypatch.setattr(C, "DEBATES", tmp_path)
        debate_body = (
            "## Round 1 — Reviewer\n\n"
            "VERDICT: REJECT\n"
            "[BLOCKER] foo\n"
            "Evidence: file:1\n\n"
            "## Round 1 — Reply\n\n"
            "[BLOCKER] foo\n"
            "ACCEPTED  — the claim holds, fixing now\n"
            "RESOLVED\n\n"
            "## Round 2 — Reviewer\n\n"
            "VERDICT: APPROVE\n"
        )
        (tmp_path / "DEBATE-t1.md").write_text(debate_body)
        conv = Conversation.from_state({"task_id": "t1", "request": ""})
        # debate_history is the raw file body, verbatim.
        assert conv.debate_history == debate_body
        # debate_ledger is non-empty, starts with the header, contains foo once.
        assert conv.debate_ledger != ""
        assert conv.debate_ledger.startswith(
            "## Debate ledger (prior rounds, deduplicated)\n"
        )
        assert conv.debate_ledger.count("foo") == 1
        assert "[R1 · REVIEWER · BLOCKER · RESOLVED] foo" in conv.debate_ledger

    def test_final_read_from_disk(self, monkeypatch, tmp_path):
        monkeypatch.setattr(C, "FINAL", tmp_path)
        (tmp_path / "FINAL-t1.md").write_text("the final spec")
        conv = Conversation.from_state({"task_id": "t1", "request": ""})
        assert conv.final == "the final spec"

    def test_final_missing_is_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(C, "FINAL", tmp_path)
        conv = Conversation.from_state({"task_id": "t1", "request": ""})
        assert conv.final == ""

    def test_progress_read_from_disk(self, monkeypatch, tmp_path):
        monkeypatch.setattr(C, "FINAL", tmp_path)
        (tmp_path / "PROGRESS-t1.md").write_text("the progress log")
        conv = Conversation.from_state({"task_id": "t1", "request": ""})
        assert conv.progress == "the progress log"

    def test_progress_missing_is_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(C, "FINAL", tmp_path)
        conv = Conversation.from_state({"task_id": "t1", "request": ""})
        assert conv.progress == ""

    def test_summary_read_from_disk(self, monkeypatch, tmp_path):
        monkeypatch.setattr(C, "DEBATES", tmp_path)
        (tmp_path / "SUMMARY-t1.md").write_text("the summary body")
        conv = Conversation.from_state({"task_id": "t1", "request": ""})
        assert conv.summary == "the summary body"

    def test_summary_missing_is_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(C, "DEBATES", tmp_path)
        conv = Conversation.from_state({"task_id": "t1", "request": ""})
        assert conv.summary == ""

    def test_visual_review_read_from_disk(self, monkeypatch, tmp_path):
        monkeypatch.setattr(C, "REVIEWS", tmp_path)
        (tmp_path / "VISUAL-t1.md").write_text("visual blockers here")
        conv = Conversation.from_state({"task_id": "t1", "request": ""})
        assert conv.visual_review == "visual blockers here"

    def test_visual_review_missing_is_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(C, "REVIEWS", tmp_path)
        conv = Conversation.from_state({"task_id": "t1", "request": ""})
        assert conv.visual_review == ""

    def test_batch_context_is_json(self):
        conv = Conversation.from_state({"batch_idx": 2, "batches": [{"n": 1}]})
        assert json.loads(conv.batch_context) == {"batch_idx": 2, "batches": [{"n": 1}]}

    def test_review_history_concatenates_code_ux_visual_with_separators(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(C, "REVIEWS", tmp_path)
        (tmp_path / "CODE-t1-b1.md").write_text("code review 1")
        (tmp_path / "CODE-t1-b2.md").write_text("code review 2")
        (tmp_path / "UX-t1.md").write_text("ux review")
        (tmp_path / "VISUAL-t1.md").write_text("visual review")
        conv = Conversation.from_state({"task_id": "t1", "request": ""})
        # Each body appears.
        assert "code review 1" in conv.review_history
        assert "code review 2" in conv.review_history
        assert "ux review" in conv.review_history
        assert "visual review" in conv.review_history
        # Structural `--- STEM ---` headers (P5) for each file present.
        assert "--- CODE-t1-b1 ---" in conv.review_history
        assert "--- CODE-t1-b2 ---" in conv.review_history
        assert "--- UX-t1 ---" in conv.review_history
        assert "--- VISUAL-t1 ---" in conv.review_history
        # CODE files appear in sorted order.
        assert conv.review_history.index("--- CODE-t1-b1 ---") < conv.review_history.index(
            "--- CODE-t1-b2 ---"
        )

    def test_review_history_missing_files_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(C, "REVIEWS", tmp_path)
        conv = Conversation.from_state({"task_id": "t1", "request": ""})
        assert conv.review_history == ""

    def test_review_history_skips_empty_files(self, monkeypatch, tmp_path):
        monkeypatch.setattr(C, "REVIEWS", tmp_path)
        (tmp_path / "CODE-t1-b1.md").write_text("")
        (tmp_path / "UX-t1.md").write_text("ux review")
        conv = Conversation.from_state({"task_id": "t1", "request": ""})
        # The empty CODE file is skipped; only UX contributes.
        assert "ux review" in conv.review_history
        assert "--- CODE-t1-b1 ---" not in conv.review_history

    def test_journal_copied_from_state_as_tuple(self):
        state = {"journal": ["a", "b"]}
        conv = Conversation.from_state(state)
        assert conv.journal == ("a", "b")
        assert isinstance(conv.journal, tuple)
        # Defensive copy + freeze: the snapshot's journal is not the state's list.
        assert conv.journal is not state["journal"]

    def test_journal_defaults_to_empty_tuple(self):
        conv = Conversation.from_state({})
        assert conv.journal == ()
        assert isinstance(conv.journal, tuple)


# --- render_prompt flat-mapping ---------------------------------------------


class TestRenderPromptFlatMapping:
    def test_conversation_fields_and_extra_kw_both_substituted(self, monkeypatch, tmp_path):
        monkeypatch.setattr(C, "TEMPLATES", tmp_path)
        (tmp_path / "x.md").write_text("{task_id} {request} {round}")
        conv = _conv(task_id="t1", request="do thing")
        assert render_prompt("x", conv, round=3) == "t1 do thing 3"

    def test_extra_kw_overrides_conversation_field(self, monkeypatch, tmp_path):
        monkeypatch.setattr(C, "TEMPLATES", tmp_path)
        (tmp_path / "x.md").write_text("{request}")
        conv = _conv(request="original")
        assert render_prompt("x", conv, request="override") == "override"

    def test_journal_renders_as_clean_text(self, monkeypatch, tmp_path):
        monkeypatch.setattr(C, "TEMPLATES", tmp_path)
        (tmp_path / "x.md").write_text("{journal}")
        conv = _conv(journal=("log1", "log2"))
        # Newline-joined clean text, NOT Python repr ("('log1', 'log2')").
        assert render_prompt("x", conv) == "log1\nlog2"

    def test_list_typed_value_renders_as_clean_text(self, monkeypatch, tmp_path):
        # `dataclasses.asdict` converts the tuple field into a plain list;
        # `_fmt` must handle both list and tuple.
        monkeypatch.setattr(C, "TEMPLATES", tmp_path)
        (tmp_path / "x.md").write_text("{journal}")
        conv = _conv(journal=("a", "b", "c"))
        assert render_prompt("x", conv) == "a\nb\nc"


# --- call-site signature guard ----------------------------------------------


NODES_DIR = Path(__file__).resolve().parent.parent / "pipeline_graph" / "nodes"


def _run_agent_calls():
    """Yield (file_path, ast.Call) for every `run_agent(...)` or `_N.run_agent(...)`
    call in `pipeline_graph/nodes/*.py`."""
    for src in sorted(NODES_DIR.glob("*.py")):
        if src.name == "__init__.py":
            continue
        tree = ast.parse(src.read_text(), filename=str(src))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == "run_agent":
                yield src, node
            elif (
                isinstance(func, ast.Attribute)
                and func.attr == "run_agent"
                and isinstance(func.value, ast.Name)
                and func.value.id == "_N"
            ):
                yield src, node


class TestCallSiteSignatures:
    def test_no_run_agent_call_passes_more_than_three_positional_args(self):
        offenders = []
        for src, call in _run_agent_calls():
            n_pos = len(call.args)
            if n_pos > 3:
                offenders.append(f"{src.name}:{call.lineno} has {n_pos} positional args")
        assert not offenders, (
            "run_agent/_N.run_agent calls must have <=3 positional args "
            "(role, conversation, step). Offenders:\n  " + "\n  ".join(offenders)
        )

    def test_run_agent_2nd_positional_arg_is_not_a_string_literal(self):
        offenders = []
        for src, call in _run_agent_calls():
            if len(call.args) < 2:
                continue
            second = call.args[1]
            if isinstance(second, ast.Constant) and isinstance(second.value, str):
                offenders.append(
                    f"{src.name}:{call.lineno} passes a string literal "
                    f"({second.value!r}) as the 2nd positional arg (should be a Conversation)"
                )
        assert not offenders, (
            "run_agent/_N.run_agent 2nd positional arg must be a Conversation, "
            "not a raw string (the old `tid` literal). Offenders:\n  "
            + "\n  ".join(offenders)
        )
