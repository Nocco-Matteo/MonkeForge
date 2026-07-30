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
from pipeline_graph.nodes.debate import _extract_plan, _latest_section
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
        """A rate limit in output wins over a fatal signature."""
        health, _ = classify_output(0, "rate limit and malformed tool call")
        assert health == "transient"


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

    def test_no_json(self):
        assert _extract_json("no json here") is None

    def test_empty(self):
        assert _extract_json("") is None


# --- _extract_plan ---------------------------------------------------------


class TestExtractPlan:
    def test_with_markers(self):
        text = "Some notes\n=== PLAN START ===\n# My Plan\nbody\n=== PLAN END ===\nmore"
        result = _extract_plan(text)
        assert result is not None
        assert "# My Plan" in result
        assert "body" in result

    def test_no_markers(self):
        assert _extract_plan("just text") is None

    def test_none_input(self):
        assert _extract_plan(None) is None

    def test_case_insensitive(self):
        text = "=== plan start ===\ncontent\n=== plan end ==="
        result = _extract_plan(text)
        assert result is not None
        assert "content" in result

    def test_strips_whitespace(self):
        text = "=== PLAN START ===\n  trimmed  \n=== PLAN END ==="
        result = _extract_plan(text)
        assert result == "trimmed"


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
