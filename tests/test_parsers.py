"""Unit tests for parser functions in agents.py, nodes/common.py,
nodes/debate.py, and test_runner.py.
"""

from pipeline_graph.agents import (
    classify_output,
    count_blockers,
    parse_disputed,
    parse_not_met,
    parse_verdict,
)
from pipeline_graph.nodes.common import _extract_json
from pipeline_graph.nodes.debate import (
    _apply_section_patch,
    _extract_plan_or_patch,
    _is_plan_section_header,
    _latest_section,
)
from pipeline_graph.test_runner import (
    new_failures_since_baseline,
    parse_eslint_errors,
    parse_tsc_errors,
    parse_vitest_failures,
)

# --- parse_verdict ---------------------------------------------------------


class TestParseVerdict:
    def test_approve(self):
        assert parse_verdict("VERDICT: APPROVE") == "APPROVE"

    def test_approve_with_changes(self):
        assert parse_verdict("VERDICT: APPROVE_WITH_CHANGES") == "APPROVE_WITH_CHANGES"

    def test_reject(self):
        assert parse_verdict("VERDICT: REJECT") == "REJECT"

    def test_bold_verdict(self):
        # The regex matches VERDICT: optionally preceded by #/* but not **
        assert parse_verdict("* VERDICT: APPROVE") == "APPROVE"

    def test_hash_verdict(self):
        assert parse_verdict("# VERDICT: APPROVE") == "APPROVE"

    def test_last_verdict_wins(self):
        text = "VERDICT: REJECT\n...\nVERDICT: APPROVE"
        assert parse_verdict(text) == "APPROVE"

    def test_unknown(self):
        assert parse_verdict("no verdict here") == "UNKNOWN"

    def test_empty(self):
        assert parse_verdict("") == "UNKNOWN"

    def test_none(self):
        assert parse_verdict(None) == "UNKNOWN"


# --- count_blockers --------------------------------------------------------


class TestCountBlockers:
    def test_zero(self):
        assert count_blockers("no blockers") == 0

    def test_one(self):
        assert count_blockers("[BLOCKER] something") == 1

    def test_three(self):
        assert count_blockers("[BLOCKER] a\n[BLOCKER] b\n[BLOCKER] c") == 3

    def test_empty(self):
        assert count_blockers("") == 0

    def test_none(self):
        assert count_blockers(None) == 0


# --- parse_not_met ---------------------------------------------------------


class TestParseNotMet:
    def test_one(self):
        assert parse_not_met("1: NOT MET — reason") == ["1"]

    def test_multiple(self):
        text = "1: NOT MET — a\n2: NOT MET — b\n3: MET"
        assert parse_not_met(text) == ["1", "2"]

    def test_none_met(self):
        assert parse_not_met("1: MET\n2: MET") == []

    def test_empty(self):
        assert parse_not_met("") == []


# --- parse_disputed --------------------------------------------------------


class TestParseDisputed:
    def test_one(self):
        # (?:item\s*)? strips optional 'item' prefix, then (\S+) captures '1:'
        result = parse_disputed("item1: DISPUTED")
        assert len(result) == 1
        assert "1" in result[0]

    def test_multiple(self):
        text = "a: DISPUTED\nb: DISPUTED"
        result = parse_disputed(text)
        assert len(result) == 2

    def test_with_item_prefix(self):
        result = parse_disputed("item foo: DISPUTED")
        assert len(result) == 1
        assert "foo" in result[0]

    def test_case_insensitive(self):
        result = parse_disputed("x: disputed")
        assert len(result) == 1
        assert "x" in result[0]

    def test_empty(self):
        assert parse_disputed("") == []


# --- classify_output -------------------------------------------------------


class TestClassifyOutput:
    def test_ok(self):
        assert classify_output(0, "A" * 100) == ("ok", "")

    def test_rate_limit_is_transient(self):
        health, sig = classify_output(0, "rate limit exceeded")
        assert health == "transient"
        assert "rate limit" in sig

    def test_malformed_is_hard(self):
        health, sig = classify_output(0, "malformed tool call")
        assert health == "hard"

    def test_signal_death_is_transient(self):
        health, sig = classify_output(-9, "")
        assert health == "transient"
        assert "signal 9" in sig

    def test_nonzero_exit_is_hard(self):
        health, sig = classify_output(1, "some output")
        assert health == "hard"

    def test_near_empty_is_hard(self):
        health, sig = classify_output(0, "ab")
        assert health == "hard"

    def test_transient_beats_fatal(self):
        """A rate limit in the head wins over a fatal signature."""
        health, _ = classify_output(0, "rate limit and malformed tool call")
        assert health == "transient"

    def test_body_citation_of_rate_limit_is_ok(self):
        """Product text citing rate limits must not look like a CLI 429.

        Seen on TASK-020: plan/debate replies quote 'Webhook rate limits…'
        and were misclassified as transient after a long successful run.
        """
        body = (
            "ACCEPTED — fixed the Discord narration.\n\n"
            + ("x" * 2500)
            + "\nWebhook rate limits remain acceptable at milestone default.\n"
            + ("y" * 200)
        )
        assert classify_output(0, body) == ("ok", "")

    def test_body_citation_of_out_of_usage_is_ok(self):
        """Fatal signatures already head-scoped; keep the regression explicit."""
        body = ("z" * 2500) + "\nExample edge case: out of usage for the CLI.\n"
        assert classify_output(0, body) == ("ok", "")


# --- _extract_json ---------------------------------------------------------


class TestExtractJson:
    def test_json_code_block(self):
        text = 'Here:\n```json\n[{"n": 1}]\n```\nDone.'
        result = _extract_json(text)
        assert result == [{"n": 1}]

    def test_plain_code_block_array(self):
        text = "```\n[1, 2, 3]\n```"
        result = _extract_json(text)
        assert result == [1, 2, 3]

    def test_bare_array(self):
        text = 'Result: [{"a": "b"}]'
        result = _extract_json(text)
        assert result == [{"a": "b"}]

    def test_json_block_no_newline_before_fence(self):
        text = '```json\n[{"n": 1, "scope": "test", "checklist": [1, 2]}]```'
        result = _extract_json(text)
        assert result == [{"n": 1, "scope": "test", "checklist": [1, 2]}]

    def test_bare_array_with_brackets_in_prose(self):
        text = (
            "Ruling on [BLOCKER]: fix needed.\n\n"
            '[{"n": 1, "scope": "test", "checklist": [1, 2]}]\n'
            "HAS_UI: NO"
        )
        result = _extract_json(text)
        assert result == [{"n": 1, "scope": "test", "checklist": [1, 2]}]

    def test_nested_arrays_in_json(self):
        text = '```json\n[{"n": 1, "checklist": [1, 2, 3], "allow": ["a > b"]}]```'
        result = _extract_json(text)
        assert result == [{"n": 1, "checklist": [1, 2, 3], "allow": ["a > b"]}]

    def test_brackets_inside_json_strings(self):
        text = '[{"n": 1, "scope": "fix [foo] bar"}]'
        result = _extract_json(text)
        assert result == [{"n": 1, "scope": "fix [foo] bar"}]

    def test_no_json(self):
        assert _extract_json("no json here") is None

    def test_empty(self):
        assert _extract_json("") is None


# --- _extract_plan_or_patch (TASK-023) -------------------------------------


class TestExtractPlanOrPatch:
    def test_patch_envelope_extracted(self):
        text = (
            "Some notes\n"
            "=== PLAN PATCH START ===\n"
            "@@@ REPLACE section: \"X\"\nX\nbody\n@@@ END\n"
            "=== PLAN PATCH END ===\n"
            "more notes"
        )
        kind, body = _extract_plan_or_patch(text)
        assert kind == "patch"
        assert body is not None
        assert "@@@ REPLACE section:" in body

    def test_legacy_full_plan_markers_extracted(self):
        text = "=== PLAN START ===\n# My Plan\nbody\n=== PLAN END ==="
        kind, body = _extract_plan_or_patch(text)
        assert kind == "legacy"
        assert body is not None
        assert "# My Plan" in body

    def test_no_markers_no_patch_tokens(self):
        # Text with no @@@ and no envelope markers at all -> (None, None).
        kind, body = _extract_plan_or_patch("just text, no patch tokens")
        assert kind is None
        assert body is None

    def test_none_input(self):
        kind, body = _extract_plan_or_patch(None)
        assert kind is None
        assert body is None

    def test_case_insensitive_patch_envelope(self):
        text = "=== plan patch start ===\n@@@ REPLACE section: \"X\"\nX\n@@@ END\n=== plan patch end ==="
        kind, body = _extract_plan_or_patch(text)
        assert kind == "patch"

    def test_malformed_unmatched_patch_start(self):
        # An unmatched === PLAN PATCH START === with no closing END -> malformed.
        text = "=== PLAN PATCH START ===\n@@@ REPLACE section: \"X\"\nX\n@@@ END\nno closing marker"
        kind, body = _extract_plan_or_patch(text)
        assert kind == "malformed"
        assert body is None

    def test_malformed_bare_replace_block_no_envelope(self):
        # A bare @@@ REPLACE … @@@ END block with no surrounding envelope.
        text = "Some notes\n@@@ REPLACE section: \"X\"\nX\nbody\n@@@ END\nmore notes"
        kind, body = _extract_plan_or_patch(text)
        assert kind == "malformed"
        assert body is None

    def test_malformed_stray_at_at_at_token(self):
        # A stray @@@ token in prose (no envelope, no REPLACE block).
        kind, body = _extract_plan_or_patch("prose mentioning @@@ accidentally")
        assert kind == "malformed"
        assert body is None

    def test_malformed_unmatched_patch_end(self):
        # A closing === PLAN PATCH END === with no opening START -> malformed.
        text = "notes\n=== PLAN PATCH END ===\nmore"
        kind, body = _extract_plan_or_patch(text)
        assert kind == "malformed"
        assert body is None


# --- _is_plan_section_header (TASK-023) ------------------------------------


class TestIsPlanSectionHeader:
    def test_numbered_header_with_body(self):
        lines = ["1. Title", "body line", "2. Next"]
        assert _is_plan_section_header(lines, 0) is True

    def test_numbered_header_followed_by_another_numbered_is_not_header(self):
        # A numbered line immediately followed by another numbered line is not
        # a section header (it's a numbered list item, not a section anchor).
        lines = ["1. item one", "2. item two", "body"]
        assert _is_plan_section_header(lines, 0) is False

    def test_non_numbered_line_is_not_header(self):
        lines = ["plain text", "more text"]
        assert _is_plan_section_header(lines, 0) is False

    def test_last_line_numbered_is_header(self):
        lines = ["body", "3. Last Section"]
        assert _is_plan_section_header(lines, 1) is True

    def test_out_of_range(self):
        assert _is_plan_section_header(["x"], -1) is False
        assert _is_plan_section_header(["x"], 5) is False

    def test_markdown_atx_header_with_body(self):
        # Production PLAN-*.md: ``## 1. Goal`` followed by prose.
        lines = ["## 1. Goal", "Cut LLM token burn", "## 2. Constraints"]
        assert _is_plan_section_header(lines, 0) is True
        assert _is_plan_section_header(lines, 2) is True

    def test_markdown_header_followed_by_numbered_list_is_still_header(self):
        # Ship-blocker: ``## 5. File-by-file`` then ``1. do X`` must stay a
        # section header (the following bare numbered lines are list items).
        lines = [
            "## 5. File-by-file changes",
            "1. do X",
            "2. do Y",
            "## 6. Edge cases",
        ]
        assert _is_plan_section_header(lines, 0) is True
        assert _is_plan_section_header(lines, 1) is False
        assert _is_plan_section_header(lines, 2) is False
        assert _is_plan_section_header(lines, 3) is True


# --- _apply_section_patch (TASK-023) ---------------------------------------


class TestApplySectionPatch:
    PLAN = (
        "1. First Section\n"
        "first body line\n"
        "second body line\n"
        "2. Second Section\n"
        "second section body\n"
        "3. Third Section\n"
        "third body\n"
    )

    # Production shape (PLAN-*.md from the plan node): ATX + numbered title.
    MD_PLAN = (
        "# TASK-023 — Lean debate plan updates\n"
        "\n"
        "## 1. Goal\n"
        "Cut LLM token burn.\n"
        "\n"
        "## 2. Constraints\n"
        "- C1: keep per-item blocks\n"
        "\n"
        "## 5. File-by-file changes\n"
        "1. MODIFY debate.py\n"
        "2. MODIFY condenser.py\n"
        "\n"
        "## 6. Edge cases considered\n"
        "Some edge.\n"
    )

    def test_no_at_at_at_returns_plan_unchanged(self):
        # A patch body with no @@@ token is a no-op: return the plan unchanged.
        result = _apply_section_patch(self.PLAN, "no patch tokens here")
        assert result == self.PLAN

    def test_at_at_at_with_no_valid_blocks_returns_none(self):
        # @@@ tokens present but no valid REPLACE block -> None (apply failure).
        result = _apply_section_patch(self.PLAN, "@@@ garbage\n@@@ also garbage")
        assert result is None

    def test_missing_section_title_returns_none(self):
        patch = (
            '@@@ REPLACE section: "Nonexistent Section"\n'
            "Nonexistent Section\nnew body\n@@@ END\n"
        )
        result = _apply_section_patch(self.PLAN, patch)
        assert result is None

    def test_replaces_target_section_body(self):
        # The patch body passed to _apply_section_patch is the text BETWEEN the
        # envelope markers (as extracted by _extract_plan_or_patch).
        body = (
            '@@@ REPLACE section: "Second Section"\n'
            "2. Second Section\n"
            "completely new body\n"
            "@@@ END\n"
        )
        result = _apply_section_patch(self.PLAN, body)
        assert result is not None
        assert "completely new body" in result
        # The other sections are untouched.
        assert "1. First Section" in result
        assert "3. Third Section" in result
        # The old second-section body is gone.
        assert "second section body" not in result

    def test_patch_preserves_section_header(self):
        # A patch whose body's first line is the target section's own header
        # line results in that header still present verbatim in the result.
        body = (
            '@@@ REPLACE section: "First Section"\n'
            "1. First Section\n"
            "rewritten first body\n"
            "@@@ END\n"
        )
        result = _apply_section_patch(self.PLAN, body)
        assert result is not None
        # The header line survives verbatim.
        assert "1. First Section" in result
        # The new body is present.
        assert "rewritten first body" in result
        # The old body is gone.
        assert "first body line" not in result

    def test_multiple_blocks_in_one_patch(self):
        body = (
            '@@@ REPLACE section: "First Section"\n'
            "1. First Section\nnew first\n@@@ END\n"
            '@@@ REPLACE section: "Third Section"\n'
            "3. Third Section\nnew third\n@@@ END\n"
        )
        result = _apply_section_patch(self.PLAN, body)
        assert result is not None
        assert "new first" in result
        assert "new third" in result
        assert "2. Second Section" in result

    def test_markdown_plan_replace_by_short_title(self):
        body = (
            '@@@ REPLACE section: "Goal"\n'
            "## 1. Goal\n"
            "New goal text.\n"
            "@@@ END\n"
        )
        result = _apply_section_patch(self.MD_PLAN, body)
        assert result is not None
        assert "New goal text." in result
        assert "## 2. Constraints" in result
        assert "Cut LLM token burn." not in result

    def test_markdown_plan_replace_by_full_header(self):
        body = (
            '@@@ REPLACE section: "## 5. File-by-file changes"\n'
            "## 5. File-by-file changes\n"
            "- only condenser.py\n"
            "@@@ END\n"
        )
        result = _apply_section_patch(self.MD_PLAN, body)
        assert result is not None
        assert "- only condenser.py" in result
        # Numbered list under section 5 must not truncate the replace.
        assert "1. MODIFY debate.py" not in result
        assert "## 6. Edge cases considered" in result
        assert "## 1. Goal" in result

    def test_markdown_plan_replace_by_numbered_title(self):
        body = (
            '@@@ REPLACE section: "2. Constraints"\n'
            "## 2. Constraints\n"
            "- C1 rewritten\n"
            "@@@ END\n"
        )
        result = _apply_section_patch(self.MD_PLAN, body)
        assert result is not None
        assert "- C1 rewritten" in result
        assert "- C1: keep per-item blocks" not in result

    def test_shortened_numbered_title_matches_parenthetical_header(self):
        # Live TASK-020 failure: plan has ``## 2. Constraints (each testable)``
        # but the proposer cited ``2. Constraints`` (dropped the parenthetical).
        # Same section number + body first-line must still apply.
        plan = (
            "## 1. Goal\n"
            "goal body\n"
            "## 2. Constraints (each testable)\n"
            "- old C1\n"
            "## 3. Architecture decisions\n"
            "arch body\n"
        )
        body = (
            '@@@ REPLACE section: "2. Constraints"\n'
            "## 2. Constraints (each testable)\n"
            "- new C1\n"
            "@@@ END\n"
        )
        result = _apply_section_patch(plan, body)
        assert result is not None
        assert "- new C1" in result
        assert "- old C1" not in result
        assert "## 1. Goal" in result
        assert "## 3. Architecture decisions" in result


# --- _latest_section -------------------------------------------------------


class TestLatestSection:
    def test_finds_last_reviewer(self):
        text = (
            "## Round 1 — Reviewer\n[BLOCKER] a\n"
            "## Round 1 — Reply\nresolved\n"
            "## Round 2 — Reviewer\n[BLOCKER] b"
        )
        result = _latest_section(text, "Reviewer")
        assert "[BLOCKER] b" in result
        assert "[BLOCKER] a" not in result

    def test_finds_ux(self):
        text = "## Round 1 — Reviewer\nstuff\n## Round 1 — UX\n[BLOCKER] ux1"
        result = _latest_section(text, "UX")
        assert "[BLOCKER] ux1" in result

    def test_no_section(self):
        assert _latest_section("no sections", "Reviewer") == ""

    def test_empty(self):
        assert _latest_section("", "Reviewer") == ""

    def test_bounded_by_next_header(self):
        text = "## Round 1 — Reviewer\n[BLOCKER] a\n## Round 1 — Reply\n[BLOCKER] resolved"
        result = _latest_section(text, "Reviewer")
        assert "resolved" not in result


# --- test_runner parsers ---------------------------------------------------


class TestParseVitestFailures:
    def test_basic(self):
        output = "FAIL src/a.test.ts\nFAIL src/b.test.ts\nPASS src/c.test.ts"
        result = parse_vitest_failures(output)
        assert result == {"src/a.test.ts", "src/b.test.ts"}

    def test_empty(self):
        assert parse_vitest_failures("") == set()


class TestParseTscErrors:
    def test_basic(self):
        output = "src/a.ts(10,5): error TS2304: Cannot find name 'foo'."
        result = parse_tsc_errors(output)
        assert len(result) == 1
        assert "TS2304" in list(result)[0]

    def test_empty(self):
        assert parse_tsc_errors("") == set()


class TestParseEslintErrors:
    def test_basic(self):
        output = (
            '[{"filePath":"src/a.ts","messages":'
            '[{"severity":2,"ruleId":"no-unused-vars","message":"x is unused"}]}]'
        )
        result = parse_eslint_errors(output)
        assert len(result) == 1
        assert "no-unused-vars" in list(result)[0]

    def test_empty_array(self):
        assert parse_eslint_errors("[]") == set()

    def test_garbage(self):
        assert parse_eslint_errors("not json") == set()


class TestNewFailuresSinceBaseline:
    def test_new_only(self):
        current = {"A", "B", "C"}
        baseline = {"A"}
        result = new_failures_since_baseline(current, baseline, [])
        assert result == {"B", "C"}

    def test_allowlist_filters(self):
        current = {"A", "B", "C"}
        baseline = set()
        result = new_failures_since_baseline(current, baseline, ["B"])
        assert result == {"A", "C"}

    def test_empty(self):
        assert new_failures_since_baseline(set(), set(), []) == set()
