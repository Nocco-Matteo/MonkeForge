"""Unit tests for the deterministic debate_ledger builder (TASK-014).

Covers the conformance checklist for FINAL-014 batch 1:
  - TestEmpty: empty/None/no-headers → "".
  - TestDedup / TestDedupHeaderless: one item restated N times as RESOLVED in
    Reply sections → one ledger line, RESOLVED, first-raise round.
  - TestFloodInvariant / TestFloodInvariantHeaderless: 20+ restatements → at
    most one line (S/N invariant).
  - TestRaisedToResolvedAcrossRounds: raise → reply RESOLVED → RESOLVED.
  - TestStatusDefaultOpen: raise, no reply → OPEN.
  - TestSuggestionParsing: [SUGGESTION] parsing.
  - TestTechLimit: TECH-LIMIT VERIFIED → RESOLVED at extraction.
  - TestTechLimitNotDoubleCounted: TECH-LIMIT VERIFIED: [BLOCKER] foo → one
    TECH-LIMIT entry, no phantom BLOCKER.
  - TestTechLimitRejectedNotDoubleCounted: TECH-LIMIT REJECTED: [BLOCKER] foo
    → zero entries.
  - TestSeverityDistinguishesSameClaim / TestCriticDistinguishesSameClaim:
    dedup key includes severity and critic.
  - TestReplyResolvesAllCritics / Headerless: Reply resolution targets all
    critics' entries for that claim/severity.
  - TestReRaiseReopens: re-raise in a later round reopens the entry.
  - TestUxResolutionIndexed / TestUxResolutionIndexOutOfRange: UX
    RESOLVED <n>:/STILL OPEN <n>: indexing.
  - TestLastSignalWins: last signal in file order wins.
  - TestNoResolutionWithoutDelimiter: bare RESOLVED without ACCEPTED/REJECTED/
    PARTIAL → OPEN.
  - TestParaphrasedClaimStaysOpen: no tag → OPEN (no substring fallback).
  - TestShortClaimNotFalselyResolved: "foo" not matched in "food parsing".
  - TestSortOrder: oldest-raise → newest.
  - TestHeader: ledger header text.
  - TestClaimNormalization: markdown/whitespace stripping.
  - TestTrailingResolvedStrippedFromRaiseLine: raise-line `— RESOLVED` suffix
    is not a resolution signal.
  - TestCondensedRoundsOmittedFromLedger: collapsed rounds produce no items.
  - TestPlanPlaceholderVerbatim: render_prompt single-pass re.sub preserves
    {debate_ledger} inside plan content verbatim.
"""
from __future__ import annotations

from pipeline_graph import config as C
from pipeline_graph.agents import render_prompt
from pipeline_graph.condenser import debate_ledger
from pipeline_graph.state import Conversation


# --- helpers ----------------------------------------------------------------


def _conv(**overrides) -> Conversation:
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


def _round_reviewer(n: int, body: str) -> str:
    return f"\n\n## Round {n} — Reviewer\n\n{body}\n"


def _round_ux(n: int, body: str) -> str:
    return f"\n\n## Round {n} — UX\n\n{body}\n"


def _round_reply(n: int, body: str) -> str:
    return f"\n\n## Round {n} — Reply\n\n{body}\n"


def _ledger_lines(text: str) -> list[str]:
    """Return the ledger body lines (without the header)."""
    out = debate_ledger(text)
    if not out:
        return []
    lines = out.strip().split("\n")
    # Drop the header line.
    return [l for l in lines if l and not l.startswith("## ")]


# --- empty input ------------------------------------------------------------


class TestEmpty:
    def test_empty_string(self):
        assert debate_ledger("") == ""

    def test_none(self):
        assert debate_ledger(None) == ""

    def test_no_headers_preamble(self):
        assert debate_ledger("just a flat preamble with no round headers\n") == ""


# --- dedup ------------------------------------------------------------------


class TestDedup:
    def test_one_item_repeated_resolved_in_replies(self):
        text = _round_reviewer(1, "VERDICT: REJECT\n[BLOCKER] foo")
        for n in range(1, 6):
            text += _round_reply(
                n,
                "### [BLOCKER] foo\nACCEPTED  — the claim holds...\nRESOLVED",
            )
        lines = _ledger_lines(text)
        foo_lines = [l for l in lines if "foo" in l]
        assert len(foo_lines) == 1
        assert "[R1 · REVIEWER · BLOCKER · RESOLVED] foo" in lines


class TestDedupHeaderless:
    def test_one_item_repeated_resolved_headerless(self):
        text = _round_reviewer(1, "VERDICT: REJECT\n[BLOCKER] foo")
        for n in range(1, 6):
            text += _round_reply(
                n,
                "[BLOCKER] foo\nACCEPTED  — the claim holds...\nRESOLVED",
            )
        lines = _ledger_lines(text)
        foo_lines = [l for l in lines if "foo" in l]
        assert len(foo_lines) == 1
        assert "[R1 · REVIEWER · BLOCKER · RESOLVED] foo" in lines


# --- flood invariant --------------------------------------------------------


class TestFloodInvariant:
    def test_20_restatements_one_line(self):
        text = _round_reviewer(1, "VERDICT: REJECT\n[BLOCKER] foo")
        for n in range(1, 21):
            text += _round_reply(
                n,
                "### [BLOCKER] foo\nACCEPTED  — ...\nRESOLVED",
            )
        lines = _ledger_lines(text)
        foo_lines = [l for l in lines if "foo" in l]
        assert len(foo_lines) == 1


class TestFloodInvariantHeaderless:
    def test_20_restatements_one_line_headerless(self):
        text = _round_reviewer(1, "VERDICT: REJECT\n[BLOCKER] foo")
        for n in range(1, 21):
            text += _round_reply(
                n,
                "[BLOCKER] foo\nACCEPTED  — ...\nRESOLVED",
            )
        lines = _ledger_lines(text)
        foo_lines = [l for l in lines if "foo" in l]
        assert len(foo_lines) == 1


# --- raised → resolved ------------------------------------------------------


class TestRaisedToResolvedAcrossRounds:
    def test_raise_then_reply_resolved(self):
        text = (
            _round_reviewer(1, "VERDICT: REJECT\n[BLOCKER] foo")
            + _round_reply(1, "### [BLOCKER] foo\nACCEPTED  — ...\nRESOLVED")
        )
        lines = _ledger_lines(text)
        assert "[R1 · REVIEWER · BLOCKER · RESOLVED] foo" in lines


# --- default open -----------------------------------------------------------


class TestStatusDefaultOpen:
    def test_raise_no_reply_open(self):
        text = _round_reviewer(1, "VERDICT: REJECT\n[BLOCKER] foo")
        lines = _ledger_lines(text)
        assert "[R1 · REVIEWER · BLOCKER · OPEN] foo" in lines


# --- suggestion parsing -----------------------------------------------------


class TestSuggestionParsing:
    def test_suggestion_open(self):
        text = _round_reviewer(1, "VERDICT: APPROVE_WITH_CHANGES\n[SUGGESTION] bar")
        lines = _ledger_lines(text)
        assert "[R1 · REVIEWER · SUGGESTION · OPEN] bar" in lines


# --- tech-limit -------------------------------------------------------------


class TestTechLimit:
    def test_tech_limit_verified_resolved(self):
        text = _round_reviewer(1, "VERDICT: APPROVE\nTECH-LIMIT VERIFIED: baz")
        lines = _ledger_lines(text)
        assert "[R1 · REVIEWER · TECH-LIMIT · RESOLVED] baz" in lines


class TestTechLimitNotDoubleCounted:
    def test_verified_with_blocker_tag_one_entry(self):
        text = _round_reviewer(1, "VERDICT: APPROVE\nTECH-LIMIT VERIFIED: [BLOCKER] foo")
        lines = _ledger_lines(text)
        # Exactly one entry for that line: a TECH-LIMIT entry.
        assert "[R1 · REVIEWER · TECH-LIMIT · RESOLVED] [BLOCKER] foo" in lines
        # No separate BLOCKER entry.
        blocker_foo = [l for l in lines if "[BLOCKER] foo" in l and "TECH-LIMIT" not in l]
        assert blocker_foo == []


class TestTechLimitRejectedNotDoubleCounted:
    def test_rejected_with_blocker_tag_zero_entries(self):
        text = _round_reviewer(
            1,
            "VERDICT: APPROVE\nTECH-LIMIT REJECTED: [BLOCKER] foo — the constraint is not real (file:line)",
        )
        lines = _ledger_lines(text)
        # Zero TECH-LIMIT entries (REJECTED is not captured).
        tech_limit_entries = [l for l in lines if "TECH-LIMIT ·" in l]
        assert tech_limit_entries == []
        # Zero BLOCKER entries from that line.
        blocker_foo = [l for l in lines if "BLOCKER ·" in l and "foo" in l]
        assert blocker_foo == []


# --- severity / critic distinguishes ----------------------------------------


class TestSeverityDistinguishesSameClaim:
    def test_blocker_and_suggestion_same_claim_two_lines(self):
        text = (
            _round_reviewer(1, "VERDICT: REJECT\n[BLOCKER] foo")
            + _round_reviewer(2, "VERDICT: APPROVE_WITH_CHANGES\n[SUGGESTION] foo")
        )
        lines = _ledger_lines(text)
        assert "[R1 · REVIEWER · BLOCKER · OPEN] foo" in lines
        assert "[R2 · REVIEWER · SUGGESTION · OPEN] foo" in lines


class TestCriticDistinguishesSameClaim:
    def test_reviewer_and_ux_same_claim_two_lines(self):
        text = (
            _round_reviewer(1, "VERDICT: REJECT\n[BLOCKER] foo")
            + _round_ux(1, "VERDICT: REJECT\n[BLOCKER] foo")
            + _round_ux(2, "VERDICT: APPROVE\nRESOLVED 1: foo fixed")
        )
        lines = _ledger_lines(text)
        assert "[R1 · REVIEWER · BLOCKER · OPEN] foo" in lines
        assert "[R1 · UX · BLOCKER · RESOLVED] foo" in lines


# --- reply resolves all critics ---------------------------------------------


class TestReplyResolvesAllCritics:
    def test_reply_resolves_both_critics(self):
        text = (
            _round_reviewer(1, "VERDICT: REJECT\n[BLOCKER] foo")
            + _round_ux(1, "VERDICT: REJECT\n[BLOCKER] foo")
            + _round_reply(1, "### [BLOCKER] foo\nACCEPTED  — ...\nRESOLVED")
        )
        lines = _ledger_lines(text)
        assert "[R1 · REVIEWER · BLOCKER · RESOLVED] foo" in lines
        assert "[R1 · UX · BLOCKER · RESOLVED] foo" in lines


class TestReplyResolvesAllCriticsHeaderless:
    def test_reply_resolves_both_critics_headerless(self):
        text = (
            _round_reviewer(1, "VERDICT: REJECT\n[BLOCKER] foo")
            + _round_ux(1, "VERDICT: REJECT\n[BLOCKER] foo")
            + _round_reply(1, "[BLOCKER] foo\nACCEPTED  — ...\nRESOLVED")
        )
        lines = _ledger_lines(text)
        assert "[R1 · REVIEWER · BLOCKER · RESOLVED] foo" in lines
        assert "[R1 · UX · BLOCKER · RESOLVED] foo" in lines


# --- re-raise reopens -------------------------------------------------------


class TestReRaiseReopens:
    def test_reraise_after_resolution_reopens(self):
        text = (
            _round_reviewer(1, "VERDICT: REJECT\n[BLOCKER] foo")
            + _round_reply(1, "### [BLOCKER] foo\nACCEPTED  — ...\nRESOLVED")
            + _round_reviewer(2, "VERDICT: APPROVE")
            + _round_reviewer(3, "VERDICT: REJECT\n[BLOCKER] foo")
        )
        lines = _ledger_lines(text)
        foo_line = [l for l in lines if "foo" in l]
        assert len(foo_line) == 1
        assert "[R1 · REVIEWER · BLOCKER · OPEN] foo" in lines


# --- UX indexed resolution --------------------------------------------------


class TestUxResolutionIndexed:
    def test_indexed_resolution(self):
        text = (
            _round_ux(1, "VERDICT: REJECT\n[BLOCKER] a\n[BLOCKER] b")
            + _round_ux(2, "VERDICT: APPROVE_WITH_CHANGES\nRESOLVED 1: a fixed\nSTILL OPEN 2: b missing")
        )
        lines = _ledger_lines(text)
        assert "[R1 · UX · BLOCKER · RESOLVED] a" in lines
        assert "[R1 · UX · BLOCKER · OPEN] b" in lines


class TestUxResolutionIndexOutOfRange:
    def test_out_of_range_stays_open(self):
        text = (
            _round_ux(1, "VERDICT: REJECT\n[BLOCKER] a\n[BLOCKER] b")
            + _round_ux(2, "VERDICT: APPROVE\nRESOLVED 5: nonsense")
        )
        lines = _ledger_lines(text)
        # Both stay OPEN (index 5 out of range for 2 items).
        assert "[R1 · UX · BLOCKER · OPEN] a" in lines
        assert "[R1 · UX · BLOCKER · OPEN] b" in lines


# --- last signal wins -------------------------------------------------------


class TestLastSignalWins:
    def test_reply_resolved_then_ux_still_open(self):
        text = (
            _round_ux(1, "VERDICT: REJECT\n[BLOCKER] foo")
            + _round_reply(1, "### [BLOCKER] foo\nACCEPTED  — ...\nRESOLVED")
            + _round_ux(2, "VERDICT: APPROVE_WITH_CHANGES\nSTILL OPEN 1: foo still missing")
        )
        lines = _ledger_lines(text)
        # Reply RESOLVED resolved the UX entry, but UX STILL OPEN 1: is a later
        # UX-specific OPEN signal that reopens the UX entry (last signal wins).
        assert "[R1 · UX · BLOCKER · OPEN] foo" in lines
        foo_lines = [l for l in lines if "foo" in l]
        assert len(foo_lines) == 1


# --- no resolution without delimiter ----------------------------------------


class TestNoResolutionWithoutDelimiter:
    def test_bare_resolved_no_delimiter_stays_open(self):
        text = (
            _round_reviewer(1, "VERDICT: REJECT\n[BLOCKER] foo")
            + _round_reply(1, "RESOLVED")
        )
        lines = _ledger_lines(text)
        assert "[R1 · REVIEWER · BLOCKER · OPEN] foo" in lines


# --- paraphrase stays open --------------------------------------------------


class TestParaphrasedClaimStaysOpen:
    def test_no_tag_stays_open(self):
        text = (
            _round_reviewer(1, "VERDICT: REJECT\n[BLOCKER] the settlement prose is stale")
            + _round_reply(1, "ACCEPTED  — the claim holds...\nRESOLVED")
        )
        lines = _ledger_lines(text)
        assert "[R1 · REVIEWER · BLOCKER · OPEN] the settlement prose is stale" in lines


# --- short claim not falsely resolved ---------------------------------------


class TestShortClaimNotFalselyResolved:
    def test_foo_not_matched_in_food_parsing(self):
        text = (
            _round_reviewer(1, "VERDICT: REJECT\n[BLOCKER] foo")
            + _round_reply(1, "ACCEPTED  — food parsing is fine\nRESOLVED")
        )
        lines = _ledger_lines(text)
        assert "[R1 · REVIEWER · BLOCKER · OPEN] foo" in lines


# --- sort order -------------------------------------------------------------


class TestSortOrder:
    def test_sorted_by_first_raise_round(self):
        text = (
            _round_reviewer(3, "VERDICT: REJECT\n[BLOCKER] ccc")
            + _round_reviewer(1, "VERDICT: REJECT\n[BLOCKER] aaa")
            + _round_reviewer(2, "VERDICT: REJECT\n[BLOCKER] bbb")
        )
        lines = _ledger_lines(text)
        # Ordered R1, R2, R3 by first-raise round.
        assert lines[0].startswith("[R1 ·")
        assert lines[1].startswith("[R2 ·")
        assert lines[2].startswith("[R3 ·")


# --- header -----------------------------------------------------------------


class TestHeader:
    def test_header_present(self):
        text = _round_reviewer(1, "VERDICT: REJECT\n[BLOCKER] foo")
        out = debate_ledger(text)
        assert out.startswith("## Debate ledger (prior rounds, deduplicated)\n\n")


# --- claim normalization ----------------------------------------------------


class TestClaimNormalization:
    def test_markdown_and_whitespace_collapsed(self):
        text = (
            _round_reviewer(1, "VERDICT: REJECT\n**[BLOCKER] foo **")
            + _round_reviewer(2, "VERDICT: REJECT\n[BLOCKER]  foo ")
        )
        lines = _ledger_lines(text)
        foo_lines = [l for l in lines if "foo" in l]
        assert len(foo_lines) == 1
        # Displayed claim is trimmed.
        assert foo_lines[0].endswith(" foo")

    def test_curly_apostrophe_matches_ascii_resolution(self):
        """Critic curly quote vs proposer ASCII must still resolve (TASK-022)."""
        # U+2019 RIGHT SINGLE QUOTATION MARK in the raise; ASCII in the reply.
        raise_claim = "defeats C8\u2019s legacy-record guarantee"
        reply_claim = "defeats C8's legacy-record guarantee"
        text = (
            _round_reviewer(1, f"VERDICT: REJECT\n[BLOCKER] {raise_claim}")
            + _round_reply(
                1, f"[BLOCKER] {reply_claim}\nACCEPTED  — fixed\nRESOLVED"
            )
        )
        lines = _ledger_lines(text)
        assert len(lines) == 1
        assert "RESOLVED" in lines[0]
        assert "OPEN" not in lines[0]


# --- trailing — RESOLVED on raise line --------------------------------------


class TestTrailingResolvedStrippedFromRaiseLine:
    def test_critic_section_trailing_resolved_stays_open(self):
        text = _round_reviewer(1, "VERDICT: REJECT\n[BLOCKER] foo — RESOLVED")
        lines = _ledger_lines(text)
        assert "[R1 · REVIEWER · BLOCKER · OPEN] foo" in lines

    def test_reply_section_no_delimiter_stays_open(self):
        text = (
            _round_reviewer(1, "VERDICT: REJECT\n[BLOCKER] foo")
            + _round_reply(1, "[BLOCKER] foo — RESOLVED")
        )
        lines = _ledger_lines(text)
        assert "[R1 · REVIEWER · BLOCKER · OPEN] foo" in lines


# --- condensed rounds omitted -----------------------------------------------


class TestCondensedRoundsOmittedFromLedger:
    def test_collapsed_round_no_items(self):
        text = (
            "[Round 1 — UX: REJECT, 2 blockers, unresolved — condensed]\n\n"
            + _round_ux(2, "VERDICT: REJECT\n[BLOCKER] bar")
        )
        lines = _ledger_lines(text)
        # Only R2 UX bar appears; collapsed R1 is absent.
        assert len(lines) == 1
        assert "[R2 · UX · BLOCKER · OPEN] bar" in lines


# --- plan placeholder verbatim (render_prompt) ------------------------------


class TestPlanPlaceholderVerbatim:
    def test_plan_with_debate_ledger_placeholder_preserved(self, monkeypatch, tmp_path):
        """A plan that literally contains {debate_ledger} must NOT be expanded
        by render_prompt — the single-pass re.sub fix (C6)."""
        templates = tmp_path / "templates"
        templates.mkdir()
        monkeypatch.setattr(C, "TEMPLATES", templates)
        (templates / "debate_review.md").write_text(
            "<plan>{plan}</plan>\n<debate_ledger>{debate_ledger}</debate_ledger>"
        )
        conv = _conv(
            plan="some plan with {debate_ledger} inside it",
            debate_ledger="[R1 · REVIEWER · BLOCKER · OPEN] foo",
        )
        out = render_prompt("debate_review", conv, round=1)
        # The template's {debate_ledger} IS replaced.
        assert "[R1 · REVIEWER · BLOCKER · OPEN] foo" in out
        # The plan's literal {debate_ledger} is preserved verbatim.
        assert "some plan with {debate_ledger} inside it" in out

    def test_ledger_claim_with_plan_placeholder_preserved(self, monkeypatch, tmp_path):
        """A ledger claim that literally contains {plan} must NOT be expanded."""
        templates = tmp_path / "templates"
        templates.mkdir()
        monkeypatch.setattr(C, "TEMPLATES", templates)
        (templates / "debate_review.md").write_text(
            "<plan>{plan}</plan>\n<debate_ledger>{debate_ledger}</debate_ledger>"
        )
        conv = _conv(
            plan="the real plan",
            debate_ledger="[R1 · REVIEWER · BLOCKER · OPEN] claim with {plan} inside",
        )
        out = render_prompt("debate_review", conv, round=1)
        # The {plan} inside the ledger claim is preserved verbatim.
        assert "claim with {plan} inside" in out
        # The template's {plan} IS replaced.
        assert "<plan>the real plan</plan>" in out
