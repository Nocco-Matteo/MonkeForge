"""Tests for the debate condenser (TASK-003).

Covers the conformance checklist for FINAL-003 batch 1:
  - TestEstimateTokens: len//4 estimate, empty/None -> 0.
  - TestParseRounds: round/critic grouping, no-headers, duplicated header.
  - TestCondense: verbatim recent rounds, no-op cases, keep_recent==0, marker
    format, two-critic round, reply-only fallback, resolution flag.
  - TestConfigValidation: non-integer / negative PIPELINE_CONDENSER_KEEP_RECENT
    and non-integer PIPELINE_TOKEN_BUDGET_<ROLE> fall back gracefully.
  - TestRunAgentIntegration: real run_agent() with DRY_RUN exercises the
    condenser block — over-budget rewrite + degraded event, no-budget no-op,
    under-budget no-op, missing-file no-op, and the stabilized-file no-rewrite.
"""
import importlib
import json
import os

from pipeline_graph import agents as A
from pipeline_graph import condenser
from pipeline_graph import config as C
from pipeline_graph import events as ev
from pipeline_graph.state import Conversation


def _conv(tid, **overrides):
    """Minimal Conversation for condenser-block tests: only `task_id` matters
    (the condenser keys off it + the debate file on disk); prompt content is
    irrelevant. `journal=()` matches the new tuple field type."""
    defaults = dict(
        task_id=tid,
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

# --- estimate_tokens --------------------------------------------------------


class TestEstimateTokens:
    def test_hundred_chars_is_25(self):
        assert condenser.estimate_tokens("a" * 100) == 25

    def test_empty(self):
        assert condenser.estimate_tokens("") == 0

    def test_none(self):
        assert condenser.estimate_tokens(None) == 0

    def test_non_multiple_of_four_truncates(self):
        # 10 chars // 4 == 2
        assert condenser.estimate_tokens("abcdefghij") == 2


# --- _parse_rounds ----------------------------------------------------------


class TestParseRounds:
    def test_multi_round_multi_critic_grouping(self):
        text = (
            "preamble line\n\n"
            "## Round 1 — Reviewer\n\nbody1r\n\n"
            "## Round 1 — UX\n\nbody1u\n\n"
            "## Round 1 — Reply\n\nreply1\n\n"
            "## Round 2 — Reviewer\n\nbody2r\n\n"
            "## Round 2 — Reply\n\nreply2\n"
        )
        preamble, rounds = condenser._parse_rounds(text)
        assert preamble == "preamble line\n\n"
        assert [n for n, _ in rounds] == [1, 2]
        r1_critics = [c for c, _ in rounds[0][1]]
        assert r1_critics == ["Reviewer", "UX", "Reply"]
        r2_critics = [c for c, _ in rounds[1][1]]
        assert r2_critics == ["Reviewer", "Reply"]
        # Each body starts with its own header and is bounded by the next.
        assert rounds[0][1][0][1] == "## Round 1 — Reviewer\n\nbody1r\n\n"
        assert rounds[1][1][1][1] == "## Round 2 — Reply\n\nreply2\n"

    def test_no_headers_returns_whole_text_as_preamble(self):
        text = "just a flat debate with no round headers\n"
        preamble, rounds = condenser._parse_rounds(text)
        assert preamble == text
        assert rounds == []

    def test_duplicated_header_within_one_round_groups_both_bodies(self):
        # Real DEBATE-001.md has `## Round 1 — Reviewer` twice; both bodies
        # must land under round 1's section list (last wins in _collapse_round).
        text = (
            "## Round 1 — Reviewer\n\nfirst review\n\n"
            "## Round 1 — Reviewer\n\nsecond review\n\n"
            "## Round 1 — Reply\n\nreply\n"
        )
        preamble, rounds = condenser._parse_rounds(text)
        assert preamble == ""
        assert [n for n, _ in rounds] == [1]
        bodies = [b for _, b in rounds[0][1]]
        assert bodies == [
            "## Round 1 — Reviewer\n\nfirst review\n\n",
            "## Round 1 — Reviewer\n\nsecond review\n\n",
            "## Round 1 — Reply\n\nreply\n",
        ]

    def test_empty_text(self):
        preamble, rounds = condenser._parse_rounds("")
        assert preamble == ""
        assert rounds == []


# --- condense ---------------------------------------------------------------


def _round(n, critic="Reviewer", verdict="APPROVE", blockers=0,
           pad="x" * 1800, reply=None):
    """Build one round's section(s) with a distinct, findable body."""
    lines = [f"## Round {n} — {critic}", "", f"VERDICT: {verdict}"]
    for i in range(blockers):
        lines.append(f"[BLOCKER] issue {i}")
    lines.append(f"PAD-{n}-{pad}")
    section = "\n".join(lines) + "\n"
    if reply is not None:
        section += f"\n## Round {n} — Reply\n\n{reply}\n"
    return section


class TestCondense:
    def test_last_keep_recent_rounds_verbatim_others_collapsed(self):
        text = "".join("\n\n" + _round(n) for n in range(1, 6))
        out = condenser.condense(text, keep_recent=2)
        # Last two rounds' bodies survive verbatim (headers included).
        assert "PAD-4-" + "x" * 1800 in out
        assert "PAD-5-" + "x" * 1800 in out
        # Older rounds' padding is gone, replaced by markers.
        assert "PAD-1-" not in out
        assert "PAD-2-" not in out
        assert "PAD-3-" not in out
        assert out.count("[Round ") >= 3  # collapsed markers for rounds 1-3

    def test_keep_recent_ge_round_count_is_noop(self):
        text = "\n\n" + _round(1) + "\n\n" + _round(2)
        assert condenser.condense(text, keep_recent=5) == text
        assert condenser.condense(text, keep_recent=2) == text

    def test_keep_recent_zero_collapses_all(self):
        text = "\n\n" + _round(1) + "\n\n" + _round(2)
        out = condenser.condense(text, keep_recent=0)
        # No verbatim round body survives.
        assert "PAD-1-" not in out
        assert "PAD-2-" not in out
        # Both rounds collapsed to markers.
        assert "[Round 1 — Reviewer:" in out
        assert "[Round 2 — Reviewer:" in out

    def test_negative_keep_recent_treated_as_zero(self):
        text = "\n\n" + _round(1)
        out = condenser.condense(text, keep_recent=-3)
        assert "PAD-1-" not in out
        assert "[Round 1 — Reviewer:" in out

    def test_marker_format_with_resolved_reply(self):
        text = (
            "\n\n## Round 1 — Reviewer\n\n"
            "VERDICT: APPROVE_WITH_CHANGES\n"
            "[BLOCKER] one\n[BLOCKER] two\n\n"
            "## Round 1 — Reply\n\n"
            "[BLOCKER] one — RESOLVED by doing X\n"
        )
        out = condenser.condense(text, keep_recent=0)
        marker = ("[Round 1 — Reviewer: APPROVE_WITH_CHANGES, 2 blockers, "
                  "all RESOLVED — condensed]")
        assert marker in out

    def test_marker_format_unresolved_when_no_reply(self):
        text = "\n\n## Round 1 — Reviewer\n\nVERDICT: REJECT\n[BLOCKER] one\n"
        out = condenser.condense(text, keep_recent=0)
        assert "[Round 1 — Reviewer: REJECT, 1 blockers, unresolved — condensed]" in out

    def test_marker_format_unresolved_when_reply_without_resolved(self):
        text = (
            "\n\n## Round 1 — Reviewer\n\nVERDICT: REJECT\n[BLOCKER] one\n\n"
            "## Round 1 — Reply\n\nI disagree, the blocker stands.\n"
        )
        out = condenser.condense(text, keep_recent=0)
        assert "[Round 1 — Reviewer: REJECT, 1 blockers, unresolved — condensed]" in out

    def test_two_critic_round_emits_two_marker_lines(self):
        text = (
            "\n\n## Round 1 — Reviewer\n\nVERDICT: APPROVE_WITH_CHANGES\n[BLOCKER] a\n\n"
            "## Round 1 — UX\n\nVERDICT: REJECT\n[BLOCKER] b\n\n"
            "## Round 1 — Reply\n\n[BLOCKER] a — RESOLVED\n"
        )
        out = condenser.condense(text, keep_recent=0)
        reviewer_marker = ("[Round 1 — Reviewer: APPROVE_WITH_CHANGES, 1 blockers, "
                           "all RESOLVED — condensed]")
        ux_marker = "[Round 1 — UX: REJECT, 1 blockers, all RESOLVED — condensed]"
        assert reviewer_marker in out
        assert ux_marker in out
        # Both marker lines present, one per critic.
        assert out.count("[Round 1 — ") == 2

    def test_reply_only_round_fallback_marker(self):
        text = "\n\n## Round 1 — Reply\n\nSome reply text, no critic section.\n"
        out = condenser.condense(text, keep_recent=0)
        assert "[Round 1 — (no critic section) — condensed]" in out

    def test_duplicated_critic_header_keeps_last(self):
        # Two Reviewer passes in one round: the second supersedes the first
        # (matches nodes/debate.py::_latest_section "last wins").
        text = (
            "\n\n## Round 1 — Reviewer\n\nVERDICT: REJECT\n[BLOCKER] first\n\n"
            "## Round 1 — Reviewer\n\nVERDICT: APPROVE\n\n"
            "## Round 1 — Reply\n\n[BLOCKER] first — RESOLVED\n"
        )
        out = condenser.condense(text, keep_recent=0)
        # Only one Reviewer marker line, reflecting the LAST Reviewer pass.
        assert out.count("[Round 1 — Reviewer:") == 1
        assert "[Round 1 — Reviewer: APPROVE, 0 blockers, all RESOLVED — condensed]" in out
        assert "REJECT" not in out

    def test_preamble_preserved_verbatim(self):
        text = "PRE-AMBLE-MARKER\n\n## Round 1 — Reviewer\n\nVERDICT: APPROVE\n"
        out = condenser.condense(text, keep_recent=0)
        assert out.startswith("PRE-AMBLE-MARKER")


# --- config validation ------------------------------------------------------


class TestConfigValidation:
    def _with_env(self, env: dict[str, str]):
        """Set env vars; return a context manager that restores + reloads config.

        config.py parses CONDENSER_KEEP_RECENT at import time, so each case
        reloads the module with the bad value set, asserts, then restores the
        env and reloads back to the clean default state so no test pollutes the
        module for the rest of the suite.
        """
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            saved = {k: os.environ.get(k) for k in env}
            try:
                for k, v in env.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
                importlib.reload(C)
                yield
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
                importlib.reload(C)

        return _cm()

    def test_non_integer_keep_recent_falls_back_to_default(self):
        with self._with_env({"PIPELINE_CONDENSER_KEEP_RECENT": "abc"}):
            assert C.CONDENSER_KEEP_RECENT == 3

    def test_negative_keep_recent_clamped_to_zero(self):
        with self._with_env({"PIPELINE_CONDENSER_KEEP_RECENT": "-1"}):
            assert C.CONDENSER_KEEP_RECENT == 0

    def test_non_integer_token_budget_treated_as_unset(self):
        with self._with_env({"PIPELINE_TOKEN_BUDGET_PLAN_REVIEWER": "abc"}):
            # token_budget reads env at call time; must return None, not raise.
            assert C.token_budget("PLAN_REVIEWER") is None

    def test_blank_token_budget_is_unset(self):
        assert C.token_budget("PLAN_REVIEWER") is None  # not set in this env

    def test_valid_token_budget_returns_int(self):
        with self._with_env({"PIPELINE_TOKEN_BUDGET_PLAN_REVIEWER": "2000"}):
            assert C.token_budget("PLAN_REVIEWER") == 2000


# --- run_agent integration --------------------------------------------------


class TestRunAgentIntegration:
    def _setup_env(self, monkeypatch, tmp_path):
        """Redirect every disk sink run_agent touches into tmp_path."""
        metrics = tmp_path / "metrics"
        metrics.mkdir()
        prompts = tmp_path / "prompts"
        raw = tmp_path / "raw"
        debates = tmp_path / "debates"
        debates.mkdir()
        monkeypatch.setattr(C, "METRICS", metrics)
        monkeypatch.setattr(C, "RUNS_LOG", metrics / "runs.jsonl")
        monkeypatch.setattr(C, "PROMPTS", prompts)
        monkeypatch.setattr(C, "RAW", raw)
        monkeypatch.setattr(C, "DEBATES", debates)
        monkeypatch.setattr(C, "DRY_RUN", True)
        monkeypatch.setattr(ev, "EVENTS_LOG", metrics / "events.jsonl")
        monkeypatch.setattr(ev, "PIPELINE_LOG", metrics / "pipeline.log")
        # Avoid real ntfy socket attempts during the test.
        monkeypatch.setattr(ev, "_push", lambda *a, **k: None)
        return debates

    def _degraded_events(self):
        if not ev.EVENTS_LOG.exists():
            return []
        out = []
        for line in ev.EVENTS_LOG.read_text().splitlines():
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("kind") == "degraded":
                out.append(rec)
        return out

    def test_over_budget_rewrites_and_emits_degraded(self, monkeypatch, tmp_path):
        debates = self._setup_env(monkeypatch, tmp_path)
        monkeypatch.setenv("PIPELINE_TOKEN_BUDGET_PLAN_REVIEWER", "2000")
        monkeypatch.setattr(C, "CONDENSER_KEEP_RECENT", 2)
        # 5 rounds, ~1845 chars each -> ~9235 chars -> ~2308 est-tokens > 2000.
        text = "".join("\n\n" + _round(n) for n in range(1, 6))
        (debates / "DEBATE-t3.md").write_text(text)

        A.run_agent("PLAN_REVIEWER", _conv("t3", debate_history=text), "step", template="debate_review")

        rewritten = (debates / "DEBATE-t3.md").read_text()
        assert rewritten != text
        # Last 2 rounds verbatim.
        assert "PAD-4-" + "x" * 1800 in rewritten
        assert "PAD-5-" + "x" * 1800 in rewritten
        # Older rounds collapsed.
        assert "PAD-1-" not in rewritten
        # One degraded event with the size + role fields.
        degraded = self._degraded_events()
        assert len(degraded) == 1
        rec = degraded[0]
        assert rec["role"] == "PLAN_REVIEWER"
        assert rec["original_size"] == len(text)
        assert rec["condensed_size"] == len(rewritten)
        assert rec["condensed_size"] < rec["original_size"]

    def test_over_budget_prompt_receives_condensed_debate(self, monkeypatch, tmp_path):
        """The prompt file must contain the CONDENSED debate, not the full one.
        This is the integration test that was missing: the old condenser
        rewrote the file after render_prompt, so the agent still got the full
        debate inline."""
        debates = self._setup_env(monkeypatch, tmp_path)
        # Need a template that inlines {debate_history}.
        templates = tmp_path / "templates"
        templates.mkdir()
        monkeypatch.setattr(C, "TEMPLATES", templates)
        (templates / "debate_review.md").write_text("<debate>{debate_history}</debate>")
        monkeypatch.setenv("PIPELINE_TOKEN_BUDGET_PLAN_REVIEWER", "2000")
        monkeypatch.setattr(C, "CONDENSER_KEEP_RECENT", 2)
        text = "".join("\n\n" + _round(n) for n in range(1, 6))
        (debates / "DEBATE-t3.md").write_text(text)

        A.run_agent("PLAN_REVIEWER", _conv("t3", debate_history=text), "step", template="debate_review")

        # The prompt file is written by run_agent.
        prompt_files = list(tmp_path.glob("prompts/*.md"))
        assert prompt_files, "prompt file was not written"
        prompt_text = prompt_files[0].read_text()
        # Condensed: old rounds collapsed, PAD-1 gone from prompt.
        assert "PAD-1-" not in prompt_text
        # Last 2 rounds still present in prompt.
        assert "PAD-4-" + "x" * 1800 in prompt_text
        assert "PAD-5-" + "x" * 1800 in prompt_text

    def test_no_budget_is_noop(self, monkeypatch, tmp_path):
        debates = self._setup_env(monkeypatch, tmp_path)
        monkeypatch.delenv("PIPELINE_TOKEN_BUDGET_PLAN_REVIEWER", raising=False)
        text = "".join("\n\n" + _round(n) for n in range(1, 6))
        (debates / "DEBATE-t3.md").write_text(text)

        A.run_agent("PLAN_REVIEWER", _conv("t3", debate_history=text), "step", template="debate_review")

        assert (debates / "DEBATE-t3.md").read_text() == text
        assert self._degraded_events() == []

    def test_under_budget_is_noop(self, monkeypatch, tmp_path):
        debates = self._setup_env(monkeypatch, tmp_path)
        # Budget huge relative to the file -> not over budget -> no rewrite.
        monkeypatch.setenv("PIPELINE_TOKEN_BUDGET_PLAN_REVIEWER", "100000")
        monkeypatch.setattr(C, "CONDENSER_KEEP_RECENT", 2)
        text = "".join("\n\n" + _round(n) for n in range(1, 3))
        (debates / "DEBATE-t3.md").write_text(text)

        A.run_agent("PLAN_REVIEWER", _conv("t3", debate_history=text), "step", template="debate_review")

        assert (debates / "DEBATE-t3.md").read_text() == text
        assert self._degraded_events() == []

    def test_missing_debate_is_noop(self, monkeypatch, tmp_path):
        debates = self._setup_env(monkeypatch, tmp_path)
        monkeypatch.setenv("PIPELINE_TOKEN_BUDGET_PLAN_REVIEWER", "2000")
        monkeypatch.setattr(C, "CONDENSER_KEEP_RECENT", 2)
        # No debate file written, no debate_history in conversation.
        assert not (debates / "DEBATE-t3.md").exists()

        A.run_agent("PLAN_REVIEWER", _conv("t3"), "step", template="debate_review")

        assert not (debates / "DEBATE-t3.md").exists()
        assert self._degraded_events() == []

    def test_stabilized_file_no_rewrite_no_event(self, monkeypatch, tmp_path):
        debates = self._setup_env(monkeypatch, tmp_path)
        monkeypatch.setenv("PIPELINE_TOKEN_BUDGET_PLAN_REVIEWER", "1")
        monkeypatch.setattr(C, "CONDENSER_KEEP_RECENT", 2)
        # Already-fully-condensed file: only marker lines, no `## Round N —`
        # headers left, but still over the tiny budget. condense() cannot
        # collapse further -> condensed == original -> no write, no event.
        marker = "[Round 1 — (no critic section) — condensed]\n\n"
        stabilized = marker * 10  # ~700 chars -> ~175 est-tokens > 1
        assert condenser.estimate_tokens(stabilized) > 1
        (debates / "DEBATE-t3.md").write_text(stabilized)

        before_mtime = os.stat(debates / "DEBATE-t3.md").st_mtime_ns

        A.run_agent("PLAN_REVIEWER", _conv("t3", debate_history=stabilized), "step1", template="debate_review")
        A.run_agent("PLAN_REVIEWER", _conv("t3", debate_history=stabilized), "step2", template="debate_review")

        after = (debates / "DEBATE-t3.md").read_text()
        assert after == stabilized
        assert os.stat(debates / "DEBATE-t3.md").st_mtime_ns == before_mtime
        assert self._degraded_events() == []
