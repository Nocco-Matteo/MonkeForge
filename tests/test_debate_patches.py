"""Integration tests for the TASK-023 section-replace plan-patch contract.

Covers the conformance checklist items 38-40 (and the ownership/byte-exact
guarantees from rulings 1 + 3):

  - TestDebateReplyPatchApply: a well-formed ``=== PLAN PATCH START/END ===``
    envelope with a ``@@@ REPLACE section: "..."`` block rewrites the named
    section in ``PLAN-{tid}.md`` and sets ``plan_snapshot`` to the pre-reply
    plan.
  - TestMalformedPatchEscalates: a patch envelope whose REPLACE block cites a
    non-existent section (apply returns None) escalates with the literal
    ``plan patch apply failed`` prefix and restores ``PLAN-{tid}.md`` to the
    pre-reply snapshot exactly (not a stale/unauthorized edit).
  - TestMalformedEnvelopeEscalates: a reply with ``=== PLAN PATCH START ===``
    and a ``@@@ REPLACE`` block but no closing ``=== PLAN PATCH END ===``
    returns an escalation containing
    ``plan patch apply failed: malformed plan patch envelope``.
  - TestApplyFailureRestoresPrePlan: an unauthorized direct edit to
    ``PLAN-{tid}.md`` during the mocked ``run_agent`` call is overwritten by
    the pre-reply snapshot on an apply-failure path.
  - TestNoMarkersRevertsEvenWithEmptyPrePlan: with ``pre_plan == ""`` and a
    simulated unauthorized direct edit during the run, a no-marker reply
    leaves ``PLAN-{tid}.md`` reading back as ``""`` (not the unauthorized
    content), journaled as reverted-to-empty.
  - TestNoMarkersByteExact: ``pre_plan`` ending in ``"\\n"``; after a
    no-marker reply, ``PLAN-{tid}.md`` bytes equal ``pre_plan`` exactly (no
    extra trailing newline).
  - TestNoMarkersRevertsDirectEdit: the nonempty no-marker case restores
    ``PLAN-{tid}.md`` to ``pre_plan`` exactly even when a direct edit
    happened during the run.
  - TestLegacyFullPlanMarkers: the legacy ``=== PLAN START/END ===`` envelope
    still applies, records the ``debate reply used full-plan markers (legacy)``
    degradation, and sets ``plan_snapshot`` to the pre-reply plan.
  - TestDebateReplyNeverCallsRecoverArtifact: a source-level guard that
    ``debate_reply`` does not call ``_recover_artifact`` (ruling 1 / item 15).
  - TestBuildPlanViewLeanPath: ``_build_plan_view`` returns the full plan for
    round < 2 / small plans, the condenser diff for round >= 2 with a
    differing snapshot, and the full plan + a ``sent full plan`` journal line
    when the diff is empty / the snapshot is absent.
"""
from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline_graph import config as C
from pipeline_graph.nodes import debate as D


# --- helpers ----------------------------------------------------------------


def _state(tid: str = "pt", rnd: int = 1, **over) -> dict:
    base = {
        "task_id": tid,
        "request": "test request",
        "debate_round": rnd,
        "tech_limits": [],
        "journal": [],
        "effort": "troop-monke",
    }
    base.update(over)
    return base


def _plan_text() -> str:
    return (
        "1. First Section\n"
        "first body line\n"
        "second body line\n"
        "2. Second Section\n"
        "second section body\n"
        "3. Third Section\n"
        "third body\n"
    )


def _patch_reply(new_body: str = "rewritten second body") -> str:
    return (
        "[BLOCKER] foo\nACCEPTED — fixing now\nRESOLVED\n\n"
        "=== PLAN PATCH START ===\n"
        '@@@ REPLACE section: "Second Section"\n'
        "2. Second Section\n"
        f"{new_body}\n"
        "@@@ END\n"
        "=== PLAN PATCH END ===\n"
    )


class _DebatePatchTests(unittest.TestCase):
    """Shared fixture: redirect C.PLANS / C.DEBATES to a temp dir per test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._plans = Path(self._tmp.name) / "plans"
        self._debates = Path(self._tmp.name) / "debates"
        self._plans.mkdir(parents=True, exist_ok=True)
        self._debates.mkdir(parents=True, exist_ok=True)
        self._orig_plans = C.PLANS
        self._orig_debates = C.DEBATES
        C.PLANS = self._plans
        C.DEBATES = self._debates

    def tearDown(self):
        C.PLANS = self._orig_plans
        C.DEBATES = self._orig_debates
        self._tmp.cleanup()

    def _plan_path(self, tid: str = "pt") -> Path:
        return self._plans / f"PLAN-{tid}.md"

    def _debate_path(self, tid: str = "pt") -> Path:
        return self._debates / f"DEBATE-{tid}.md"

    def _write_plan(self, tid: str, content: str) -> None:
        self._plan_path(tid).write_text(content)

    def _run_debate_reply(self, tid: str, rnd: int, agent_output: str,
                          on_run_agent=None, **state_over):
        """Call D.debate_reply with run_agent mocked to return agent_output.

        ``on_run_agent`` is an optional callable invoked INSIDE the mocked
        run_agent (after the proposer "ran"), so a test can simulate an
        unauthorized direct edit to the plan file during the agent call.
        """
        def _fake_run_agent(role, conv, step, template=None, timeout=None, **kw):
            if on_run_agent is not None:
                on_run_agent()
            return 0, agent_output

        with patch.object(D, "run_agent", side_effect=_fake_run_agent):
            return D.debate_reply(_state(tid=tid, rnd=rnd, **state_over))


# --- patch apply ------------------------------------------------------------


class TestDebateReplyPatchApply(_DebatePatchTests):
    def test_patch_rewrites_section_and_sets_snapshot(self):
        self._write_plan("pt", _plan_text())
        delta = self._run_debate_reply("pt", 1, _patch_reply("rewritten second body"))
        # The section body was replaced.
        new_plan = self._plan_path("pt").read_text()
        assert "rewritten second body" in new_plan
        assert "second section body" not in new_plan
        # Other sections are intact.
        assert "1. First Section" in new_plan
        assert "3. Third Section" in new_plan
        # plan_snapshot is the PRE-reply plan (ruling 14).
        assert delta["plan_snapshot"] == _plan_text()
        # No escalation on a successful apply.
        assert "escalation" not in delta

    def test_patch_appends_reply_to_debate_file(self):
        self._write_plan("pt", _plan_text())
        self._run_debate_reply("pt", 1, _patch_reply("rewritten second body"))
        debate = self._debate_path("pt").read_text()
        assert "## Round 1 — Reply" in debate
        assert "[BLOCKER] foo" in debate


# --- malformed patch (missing title / section not found) --------------------


class TestMalformedPatchEscalates(_DebatePatchTests):
    def test_missing_title_escalates_and_restores_pre_plan(self):
        self._write_plan("pt", _plan_text())
        # A REPLACE block citing a section that does not exist in the plan.
        reply = (
            "=== PLAN PATCH START ===\n"
            '@@@ REPLACE section: "Nonexistent Section"\n'
            "Nonexistent Section\nbody\n@@@ END\n"
            "=== PLAN PATCH END ===\n"
        )
        delta = self._run_debate_reply("pt", 1, reply)
        assert "escalation" in delta
        assert "plan patch apply failed" in delta["escalation"]
        # The plan file is restored to the pre-reply snapshot exactly.
        assert self._plan_path("pt").read_text() == _plan_text()
        # plan_snapshot is the pre-reply plan.
        assert delta["plan_snapshot"] == _plan_text()


# --- malformed envelope (unmatched START / END) -----------------------------


class TestMalformedEnvelopeEscalates(_DebatePatchTests):
    def test_unmatched_patch_start_escalates(self):
        self._write_plan("pt", _plan_text())
        # === PLAN PATCH START === with a REPLACE block but NO closing END.
        reply = (
            "=== PLAN PATCH START ===\n"
            '@@@ REPLACE section: "Second Section"\n'
            "2. Second Section\nnew body\n@@@ END\n"
            "(no closing envelope marker here)"
        )
        delta = self._run_debate_reply("pt", 1, reply)
        assert "escalation" in delta
        assert "plan patch apply failed: malformed plan patch envelope" in delta["escalation"]
        # Plan restored to pre-reply snapshot.
        assert self._plan_path("pt").read_text() == _plan_text()
        assert delta["plan_snapshot"] == _plan_text()

    def test_bare_replace_block_no_envelope_escalates(self):
        self._write_plan("pt", _plan_text())
        reply = (
            "Some notes\n"
            '@@@ REPLACE section: "Second Section"\n'
            "2. Second Section\nnew body\n@@@ END\n"
            "more notes"
        )
        delta = self._run_debate_reply("pt", 1, reply)
        assert "plan patch apply failed: malformed plan patch envelope" in delta["escalation"]


# --- apply failure restores pre_plan (overwrites unauthorized direct edit) ---


class TestApplyFailureRestoresPrePlan(_DebatePatchTests):
    def test_apply_failure_overwrites_direct_edit(self):
        self._write_plan("pt", _plan_text())

        def _direct_edit_during_run():
            # Simulate the proposer illegally editing the plan file directly
            # during its run (before the pipeline applies the patch).
            self._plan_path("pt").write_text("UNAUTHORIZED DIRECT EDIT\n")

        # A patch citing a non-existent section -> apply returns None.
        reply = (
            "=== PLAN PATCH START ===\n"
            '@@@ REPLACE section: "Missing Section"\n'
            "Missing Section\nbody\n@@@ END\n"
            "=== PLAN PATCH END ===\n"
        )
        delta = self._run_debate_reply(
            "pt", 1, reply, on_run_agent=_direct_edit_during_run
        )
        assert "plan patch apply failed" in delta["escalation"]
        # The plan file reads back exactly pre_plan — NOT the unauthorized
        # edit, NOT the escalating patch's target.
        assert self._plan_path("pt").read_text() == _plan_text()
        assert delta["plan_snapshot"] == _plan_text()


# --- no-marker path: empty pre_plan ----------------------------------------


class TestNoMarkersRevertsEvenWithEmptyPrePlan(_DebatePatchTests):
    def test_no_markers_with_empty_pre_plan_reverts_direct_edit(self):
        # pre_plan == "" (no prior plan file).
        assert not self._plan_path("pt").exists()

        def _direct_edit_during_run():
            self._plan_path("pt").write_text("UNAUTHORIZED EDIT\n")

        # A no-marker reply (no @@@, no envelope markers).
        delta = self._run_debate_reply(
            "pt", 1, "[BLOCKER] foo\nACCEPTED\nRESOLVED\n",
            on_run_agent=_direct_edit_during_run,
        )
        # The plan file reads back as "" (reverted to empty), not the
        # unauthorized edit.
        assert self._plan_path("pt").read_text() == ""
        # Journaled as reverted-to-empty.
        joined = " ".join(delta["journal"])
        assert "reverted to empty" in joined
        # plan_snapshot is the (empty) pre-reply plan.
        assert delta["plan_snapshot"] == ""


# --- no-marker path: byte-exact restoration --------------------------------


class TestNoMarkersByteExact(_DebatePatchTests):
    def test_no_markers_preserves_trailing_newline(self):
        # pre_plan ends in a trailing newline; the restore must write it
        # verbatim (no extra appended "\n").
        pre_plan = "1. Section\nbody\n"
        self._write_plan("pt", pre_plan)
        delta = self._run_debate_reply("pt", 1, "[BLOCKER] foo\nACCEPTED\nRESOLVED\n")
        on_disk = self._plan_path("pt").read_text()
        assert on_disk == pre_plan
        # Byte-for-byte: no extra trailing newline.
        assert on_disk.endswith("\n")
        assert not on_disk.endswith("\n\n")
        assert delta["plan_snapshot"] == pre_plan


# --- no-marker path: nonempty, reverts direct edit -------------------------


class TestNoMarkersRevertsDirectEdit(_DebatePatchTests):
    def test_no_markers_nonempty_reverts_direct_edit(self):
        pre_plan = _plan_text()
        self._write_plan("pt", pre_plan)

        def _direct_edit_during_run():
            self._plan_path("pt").write_text("UNAUTHORIZED EDIT\n")

        delta = self._run_debate_reply(
            "pt", 1, "[BLOCKER] foo\nACCEPTED\nRESOLVED\n",
            on_run_agent=_direct_edit_during_run,
        )
        assert self._plan_path("pt").read_text() == pre_plan
        joined = " ".join(delta["journal"])
        assert "restored to pre-reply snapshot" in joined
        assert delta["plan_snapshot"] == pre_plan


# --- legacy full-plan markers ----------------------------------------------


class TestLegacyFullPlanMarkers(_DebatePatchTests):
    def test_legacy_markers_apply_and_degrade(self):
        self._write_plan("pt", _plan_text())
        new_full_plan = (
            "1. First Section\nnew first body\n"
            "2. Second Section\nnew second body\n"
            "3. Third Section\nthird body\n"
        )
        reply = (
            "[BLOCKER] foo\nACCEPTED\nRESOLVED\n\n"
            "=== PLAN START ===\n"
            f"{new_full_plan}"
            "=== PLAN END ===\n"
        )
        delta = self._run_debate_reply("pt", 1, reply)
        # The legacy branch strips the extracted plan text and writes it with
        # a single trailing newline.
        assert self._plan_path("pt").read_text() == new_full_plan.strip() + "\n"
        # The degradation is recorded.
        assert "degradations" in delta
        assert "debate reply used full-plan markers (legacy)" in delta["degradations"]
        # plan_snapshot is still the PRE-reply plan (ruling 14).
        assert delta["plan_snapshot"] == _plan_text()
        # No escalation on legacy apply.
        assert "escalation" not in delta


# --- debate_reply never calls _recover_artifact ----------------------------


class TestDebateReplyNeverCallsRecoverArtifact(unittest.TestCase):
    def test_no_recover_artifact_call_in_debate_reply(self):
        src = inspect.getsource(D.debate_reply)
        assert "_recover_artifact" not in src, (
            "debate_reply must not call _recover_artifact (ruling 1 / item 15): "
            "the pipeline owns the plan file and unconditionally restores the "
            "pre-reply snapshot on every non-apply path."
        )

    def test_no_pre_plan_newline_concat_in_debate_reply(self):
        """No call site writes pre_plan + '\\n' (or any concatenation) to
        PLAN-{tid}.md — restore writes pass pre_plan verbatim (ruling 3 /
        item 12)."""
        src = inspect.getsource(D.debate_reply)
        assert 'pre_plan + "\\n"' not in src
        assert 'pre_plan + "\\n\\n"' not in src

    def test_no_emptiness_guard_around_no_marker_restore(self):
        """The (None, None) branch must call _save(plan_path, pre_plan)
        unconditionally — no `if pre_plan:` / emptiness guard around the
        WRITE (item 11). The `if pre_plan:` that selects the journal line
        text is allowed; only the _save must not be nested inside it."""
        src = inspect.getsource(D.debate_reply)
        lines = src.split("\n")
        # Find the no-marker branch's unconditional _save(plan_path, pre_plan).
        # It must be at function-body indentation (4 spaces under `def
        # debate_reply`), not nested inside an `if pre_plan:` block.
        save_lines = [
            i for i, ln in enumerate(lines)
            if "_save(plan_path, pre_plan)" in ln and "if " not in ln
        ]
        assert save_lines, (
            "debate_reply must call _save(plan_path, pre_plan) on the no-marker "
            "path; no such call found in source."
        )
        # None of those save calls may be immediately preceded (skipping
        # blank lines) by an `if pre_plan:` at a strictly lesser indent —
        # that would mean the save is nested inside the emptiness guard.
        for idx in save_lines:
            # Walk backwards to the nearest non-blank, non-comment line and
            # confirm it is not an `if pre_plan:` line at a smaller indent.
            j = idx - 1
            while j >= 0 and (not lines[j].strip() or lines[j].strip().startswith("#")):
                j -= 1
            if j < 0:
                continue
            prev = lines[j]
            prev_indent = len(prev) - len(prev.lstrip())
            cur_indent = len(lines[idx]) - len(lines[idx].lstrip())
            # If the previous line is `if pre_plan:`, the save must be at a
            # DEEPER indent (nested) — which is exactly what we forbid for
            # the no-marker restore. So assert the previous line is NOT
            # `if pre_plan:` when the save is at the same/deeper indent than
            # the function body but the if is at the function-body indent.
            if "if pre_plan" in prev and prev_indent < cur_indent:
                # Nested inside the guard — forbidden for the restore write.
                raise AssertionError(
                    f"_save(plan_path, pre_plan) at line {idx+1} is nested "
                    f"inside an `if pre_plan:` emptiness guard — the no-marker "
                    f"restore must be unconditional (item 11)."
                )

    def test_every_return_path_has_plan_snapshot(self):
        """Every return path of debate_reply includes plan_snapshot: pre_plan
        (item 14)."""
        src = inspect.getsource(D.debate_reply)
        # Count return statements inside debate_reply and the number that
        # carry plan_snapshot. Every return must include it.
        import re
        returns = re.findall(r"return \{[^}]*\}", src, re.DOTALL)
        assert len(returns) >= 4, (
            f"expected at least 4 return paths in debate_reply, got {len(returns)}"
        )
        for r in returns:
            assert "plan_snapshot" in r, (
                f"a return path of debate_reply is missing plan_snapshot: {r!r}"
            )


# --- _build_plan_view lean path --------------------------------------------


class TestBuildPlanViewLeanPath(unittest.TestCase):
    def _conv(self, plan: str):
        # A minimal stand-in with the .plan attribute _build_plan_view reads.
        from types import SimpleNamespace
        return SimpleNamespace(plan=plan)

    def test_round_below_2_returns_full_plan(self):
        conv = self._conv("x" * 20000)
        view, journal = D._build_plan_view({"plan_snapshot": "y" * 20000}, conv, 1)
        assert view == conv.plan
        assert journal == []

    def test_small_plan_returns_full_plan(self):
        conv = self._conv("small plan")
        view, journal = D._build_plan_view({"plan_snapshot": "different"}, conv, 5)
        assert view == "small plan"
        assert journal == []

    def test_large_plan_with_differing_snapshot_returns_diff(self):
        plan = "1. Section\n" + ("body line\n" * 2000) + "2. Other\nmore\n"
        snapshot = "1. Section\nold body\n2. Other\nmore\n"
        conv = self._conv(plan)
        view, journal = D._build_plan_view({"plan_snapshot": snapshot}, conv, 3)
        # The lean path returns the diff, not the full plan.
        assert view != plan
        assert "body line" in view  # diff content present
        assert journal == []

    def test_large_plan_empty_diff_returns_full_plan_and_journals(self):
        # snapshot == plan -> diff empty -> full plan + journal line.
        plan = "1. Section\n" + ("body line\n" * 2000)
        conv = self._conv(plan)
        view, journal = D._build_plan_view({"plan_snapshot": plan}, conv, 3)
        assert view == plan
        assert len(journal) == 1
        assert "sent full plan" in journal[0]

    def test_large_plan_no_snapshot_returns_full_plan_and_journals(self):
        plan = "1. Section\n" + ("body line\n" * 2000)
        conv = self._conv(plan)
        view, journal = D._build_plan_view({}, conv, 3)
        assert view == plan
        assert len(journal) == 1
        assert "sent full plan" in journal[0]


if __name__ == "__main__":
    unittest.main()
