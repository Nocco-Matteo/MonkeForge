"""Tests for the debate-file condenser (TASK-003).

Five sections mirroring the plan:
- TestEstimateTokens: the len//4 token estimate.
- TestParseRounds: header grouping, preamble, duplicate-header handling.
- TestCondense: verbatim preservation, marker format, no-op cases, the
  keep_recent==0 explicit branch, the reply-only-round fallback.
- TestConfigValidation: env-var parsing fallbacks (reload-based).
- TestRunAgentIntegration: the real run_agent() condenser block, DRY_RUN-stubbed.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from pipeline_graph import agents as agents_mod
from pipeline_graph import config as C
from pipeline_graph.condenser import (
    _collapse_round,
    _parse_rounds,
    condense,
    estimate_tokens,
)


# --- TestEstimateTokens ----------------------------------------------------


class TestEstimateTokens:
    def test_hundred_chars_is_twenty_five_tokens(self):
        assert estimate_tokens("a" * 100) == 25

    def test_empty(self):
        assert estimate_tokens("") == 0

    def test_none(self):
        assert estimate_tokens(None) == 0

    def test_truncates_remainder(self):
        # 10 chars // 4 = 2 (remainder dropped, not rounded).
        assert estimate_tokens("a" * 10) == 2


# --- TestParseRounds -------------------------------------------------------


def _round_header(n: int, critic: str) -> str:
    return f"## Round {n} — {critic}\n"


class TestParseRounds:
    def test_two_rounds_multiple_critics_grouped_by_round(self):
        text = (
            "Preamble line.\n"
            + _round_header(1, "Reviewer") + "r1 body\n"
            + _round_header(1, "UX") + "r1 ux body\n"
            + _round_header(2, "Reviewer") + "r2 body\n"
        )
        preamble, rounds = _parse_rounds(text)
        assert preamble == "Preamble line.\n"
        assert [rn for rn, _ in rounds] == [1, 2]
        r1_critics = [c for c, _ in rounds[0][1]]
        assert r1_critics == ["Reviewer", "UX"]
        # Body includes the header line itself, byte-identical to the slice.
        assert rounds[0][1][0][1] == _round_header(1, "Reviewer") + "r1 body\n"

    def test_no_headers_returns_whole_text_as_preamble(self):
        text = "just prose, no rounds here\n"
        preamble, rounds = _parse_rounds(text)
        assert preamble == text
        assert rounds == []

    def test_duplicate_reviewer_headers_group_under_same_round(self):
        # Real DEBATE-001.md has two `## Round 1 — Reviewer` headers.
        text = (
            _round_header(1, "Reviewer") + "first reviewer body\n"
            + _round_header(1, "Reviewer") + "second reviewer body\n"
        )
        _, rounds = _parse_rounds(text)
        assert len(rounds) == 1
        rn, secs = rounds[0]
        assert rn == 1
        assert len(secs) == 2
        assert secs[0][1] == _round_header(1, "Reviewer") + "first reviewer body\n"
        assert secs[1][1] == _round_header(1, "Reviewer") + "second reviewer body\n"


# --- TestCondense ----------------------------------------------------------


def _make_round(n: int, critic: str, body: str = "body text here\n") -> str:
    return _round_header(n, critic) + body


class TestCondense:
    def test_last_two_rounds_verbatim_first_three_collapsed(self):
        text = "PREAMBLE\n" + "".join(
            _make_round(i, "Reviewer", f"round {i} body\n") for i in range(1, 6)
        )
        out = condense(text, keep_recent=2)
        # Last two rounds' bodies are byte-identical to the input slices.
        assert _make_round(4, "Reviewer", "round 4 body\n") in out
        assert _make_round(5, "Reviewer", "round 5 body\n") in out
        # First three rounds are gone as verbatim bodies; markers present.
        assert "round 1 body" not in out
        assert "round 3 body" not in out
        assert "[Round 1 — Reviewer:" in out
        assert "[Round 3 — Reviewer:" in out
        # Preamble preserved verbatim.
        assert out.startswith("PREAMBLE\n")

    def test_keep_recent_ge_round_count_returns_input_unchanged(self):
        text = _make_round(1, "Reviewer") + _make_round(2, "Reviewer")
        assert condense(text, keep_recent=5) == text
        assert condense(text, keep_recent=2) == text

    def test_keep_recent_zero_collapses_all_rounds(self):
        text = _make_round(1, "Reviewer") + _make_round(2, "Reviewer")
        out = condense(text, keep_recent=0)
        # No verbatim round bodies remain; only markers.
        assert "## Round" not in out
        assert "[Round 1 — Reviewer:" in out
        assert "[Round 2 — Reviewer:" in out

    def test_marker_format_resolved_reviewer_with_two_blockers(self):
        round_text = (
            _round_header(1, "Reviewer")
            + "VERDICT: APPROVE_WITH_CHANGES\n[BLOCKER] a\n[BLOCKER] b\n"
            + _round_header(1, "Reply") + "all blockers RESOLVED in this reply\n"
        )
        _, rounds = _parse_rounds(round_text)
        marker = _collapse_round(rounds[0][0], rounds[0][1])
        assert (
            "[Round 1 — Reviewer: APPROVE_WITH_CHANGES, 2 blockers, all RESOLVED — condensed]"
            in marker
        )

    def test_reviewer_and_ux_emit_two_marker_lines(self):
        round_text = (
            _round_header(1, "Reviewer") + "VERDICT: REJECT\n"
            + _round_header(1, "UX") + "VERDICT: APPROVE\n"
            + _round_header(1, "Reply") + "RESOLVED\n"
        )
        _, rounds = _parse_rounds(round_text)
        marker = _collapse_round(rounds[0][0], rounds[0][1])
        assert "[Round 1 — Reviewer: REJECT, 0 blockers, all RESOLVED — condensed]" in marker
        assert "[Round 1 — UX: APPROVE, 0 blockers, all RESOLVED — condensed]" in marker

    def test_no_reply_section_means_unresolved(self):
        round_text = _round_header(1, "Reviewer") + "VERDICT: APPROVE_WITH_CHANGES\n[BLOCKER] x\n"
        _, rounds = _parse_rounds(round_text)
        marker = _collapse_round(rounds[0][0], rounds[0][1])
        assert "unresolved" in marker
        assert "RESOLVED" not in marker

    def test_reply_without_resolved_keyword_means_unresolved(self):
        round_text = (
            _round_header(1, "Reviewer") + "VERDICT: APPROVE_WITH_CHANGES\n"
            + _round_header(1, "Reply") + "still open, no resolution\n"
        )
        _, rounds = _parse_rounds(round_text)
        marker = _collapse_round(rounds[0][0], rounds[0][1])
        assert "unresolved" in marker

    def test_reply_only_round_emits_no_critic_section_marker(self):
        round_text = _round_header(1, "Reply") + "RESOLVED everything\n"
        _, rounds = _parse_rounds(round_text)
        marker = _collapse_round(rounds[0][0], rounds[0][1])
        assert marker.strip() == "[Round 1 — (no critic section) — condensed]"

    def test_duplicate_reviewer_keeps_last_only(self):
        round_text = (
            _round_header(1, "Reviewer") + "VERDICT: REJECT\n[BLOCKER] old\n"
            + _round_header(1, "Reviewer") + "VERDICT: APPROVE_WITH_CHANGES\n[BLOCKER] new\n"
            + _round_header(1, "Reply") + "RESOLVED\n"
        )
        _, rounds = _parse_rounds(round_text)
        marker = _collapse_round(rounds[0][0], rounds[0][1])
        # Last Reviewer section wins: APPROVE_WITH_CHANGES, 1 blocker.
        assert "[Round 1 — Reviewer: APPROVE_WITH_CHANGES, 1 blockers, all RESOLVED — condensed]" in marker
        assert "REJECT" not in marker

    def test_negative_keep_recent_clamped_to_zero(self):
        text = _make_round(1, "Reviewer") + _make_round(2, "Reviewer")
        out_neg = condense(text, keep_recent=-1)
        out_zero = condense(text, keep_recent=0)
        assert out_neg == out_zero
        assert "## Round" not in out_neg


# --- TestConfigValidation --------------------------------------------------


class TestConfigValidation:
    """Reloads pipeline_graph.config under a controlled env to exercise the
    module-top parse. Each case restores env via monkeypatch.delenv so the next
    reload sees a clean state."""

    def _reload(self, monkeypatch):
        # monkeypatch auto-undoes setenv at teardown, so each test starts clean.
        # Tests set the env they need BEFORE calling this; reload re-runs the
        # module-top parse against the current os.environ.
        return importlib.reload(C)

    def test_non_integer_keep_recent_falls_back_to_default(self, monkeypatch, capfd):
        monkeypatch.setenv("PIPELINE_CONDENSER_KEEP_RECENT", "abc")
        self._reload(monkeypatch)
        assert C.CONDENSER_KEEP_RECENT == 3
        captured = capfd.readouterr()
        assert "not an int" in captured.err

    def test_negative_keep_recent_clamped_to_zero(self, monkeypatch, capfd):
        monkeypatch.setenv("PIPELINE_CONDENSER_KEEP_RECENT", "-1")
        self._reload(monkeypatch)
        assert C.CONDENSER_KEEP_RECENT == 0
        captured = capfd.readouterr()
        assert "clamping to 0" in captured.err

    def test_non_integer_token_budget_returns_none(self, monkeypatch, capfd):
        monkeypatch.setenv("PIPELINE_TOKEN_BUDGET_PLAN_REVIEWER", "abc")
        self._reload(monkeypatch)
        assert C.token_budget("PLAN_REVIEWER") is None
        captured = capfd.readouterr()
        assert "not an int" in captured.err

    def test_unset_token_budget_returns_none(self, monkeypatch):
        self._reload(monkeypatch)
        assert C.token_budget("PLAN_REVIEWER") is None

    def test_valid_token_budget_returns_int(self, monkeypatch):
        monkeypatch.setenv("PIPELINE_TOKEN_BUDGET_PLAN_REVIEWER", "2000")
        self._reload(monkeypatch)
        assert C.token_budget("PLAN_REVIEWER") == 2000


# --- TestRunAgentIntegration -----------------------------------------------


def _big_debate(rounds: int = 5, body_chars: int = 1600) -> str:
    """A debate file big enough to exceed a 2000-token budget (~8000 chars)."""
    parts = ["DEBATE preamble\n"]
    for i in range(1, rounds + 1):
        parts.append(_round_header(i, "Reviewer"))
        parts.append(f"VERDICT: APPROVE_WITH_CHANGES\n[BLOCKER] b{i}\n")
        parts.append("X" * body_chars + "\n")
        parts.append(_round_header(i, "Reply"))
        parts.append(f"reply round {i} RESOLVED\n")
    return "".join(parts)


class TestRunAgentIntegration:
    @pytest.fixture(autouse=True)
    def _dry_run(self, monkeypatch):
        # _run_once's DRY_RUN branch stubs the agent CLI so no real subprocess runs.
        monkeypatch.setattr(C, "DRY_RUN", True)

    def test_condenses_when_over_budget(self, monkeypatch, tmp_path):
        monkeypatch.setattr(C, "DEBATES", tmp_path)
        monkeypatch.setenv("PIPELINE_TOKEN_BUDGET_PLAN_REVIEWER", "2000")
        monkeypatch.setattr(C, "CONDENSER_KEEP_RECENT", 2)
        debate = tmp_path / "DEBATE-t3.md"
        original = _big_debate()
        debate.write_text(original)

        emitted: list[tuple] = []
        monkeypatch.setattr(agents_mod.ev, "emit", lambda *a, **kw: emitted.append((a, kw)))

        agents_mod.run_agent("PLAN_REVIEWER", "t3", "step", "prompt")

        on_disk = debate.read_text()
        # Last 2 rounds verbatim.
        assert _round_header(4, "Reviewer") in on_disk
        assert _round_header(5, "Reviewer") in on_disk
        # Older rounds collapsed to markers.
        assert "[Round 1 — Reviewer:" in on_disk
        # A degraded event was emitted with the required fields.
        degraded = [kw for _, kw in emitted if _is_degraded(*_)]
        assert degraded, f"expected a degraded event, got {emitted}"
        ev_kw = degraded[0]
        assert ev_kw.get("original_size") == len(original)
        assert ev_kw.get("condensed_size") == len(on_disk)
        assert ev_kw.get("role") == "PLAN_REVIEWER"

    def test_no_budget_means_noop(self, monkeypatch, tmp_path):
        monkeypatch.setattr(C, "DEBATES", tmp_path)
        monkeypatch.delenv("PIPELINE_TOKEN_BUDGET_PLAN_REVIEWER", raising=False)
        debate = tmp_path / "DEBATE-t3.md"
        original = _big_debate()
        debate.write_text(original)

        emitted: list[tuple] = []
        monkeypatch.setattr(agents_mod.ev, "emit", lambda *a, **kw: emitted.append((a, kw)))

        agents_mod.run_agent("PLAN_REVIEWER", "t3", "step", "prompt")

        assert debate.read_text() == original
        assert not [kw for _, kw in emitted if _is_degraded(*_)]

    def test_under_budget_means_noop(self, monkeypatch, tmp_path):
        monkeypatch.setattr(C, "DEBATES", tmp_path)
        monkeypatch.setenv("PIPELINE_TOKEN_BUDGET_PLAN_REVIEWER", "100000")
        monkeypatch.setattr(C, "CONDENSER_KEEP_RECENT", 2)
        debate = tmp_path / "DEBATE-t3.md"
        original = _big_debate()
        debate.write_text(original)

        emitted: list[tuple] = []
        monkeypatch.setattr(agents_mod.ev, "emit", lambda *a, **kw: emitted.append((a, kw)))

        agents_mod.run_agent("PLAN_REVIEWER", "t3", "step", "prompt")

        assert debate.read_text() == original
        assert not [kw for _, kw in emitted if _is_degraded(*_)]

    def test_missing_debate_file_no_crash_no_event(self, monkeypatch, tmp_path):
        monkeypatch.setattr(C, "DEBATES", tmp_path)
        monkeypatch.setenv("PIPELINE_TOKEN_BUDGET_PLAN_REVIEWER", "2000")
        monkeypatch.setattr(C, "CONDENSER_KEEP_RECENT", 2)
        # No DEBATE-t3.md written.

        emitted: list[tuple] = []
        monkeypatch.setattr(agents_mod.ev, "emit", lambda *a, **kw: emitted.append((a, kw)))

        # Must not raise.
        agents_mod.run_agent("PLAN_REVIEWER", "t3", "step", "prompt")
        assert not [kw for _, kw in emitted if _is_degraded(*_)]


def _is_degraded(*args) -> bool:
    # The first positional arg to ev.emit is `kind`.
    return bool(args) and args[0] == "degraded"
