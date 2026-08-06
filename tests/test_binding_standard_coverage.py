"""Binding-standard coverage rules + judge residual-checklist rule.

Reads the live prompt text (intake.md / judge.md) and the binding-standard
fixtures on every run, so a future prompt edit that silently drops the `## 3b`
rule or the residual-checklist HARD RULE is caught here, not in production.

Selectable with ``-k`` substrings: ``intake_binding``, ``fixture``,
``judge_residual``, ``enumeration``, ``global_flag``, ``reg``.
"""
from __future__ import annotations

import re
from pathlib import Path

from pipeline_graph import config as C
from pipeline_graph.intake_materialize import (
    is_contract_brief,
    missing_contract_sections,
)

PROMPTS_DIR = C.TEMPLATES
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "binding_standard"


def _intake_text() -> str:
    return (PROMPTS_DIR / "intake.md").read_text()


def _judge_text() -> str:
    return (PROMPTS_DIR / "judge.md").read_text()


def _norm(text: str) -> str:
    """Collapse all whitespace runs (including newlines) to single spaces.

    Prompt text is prose and may wrap key phrases across lines; substring
    checks against the normalized form catch the phrase regardless of
    wrapping, while position-based checks (placement guard) use the raw text.
    """
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# 1. intake.md binding-standard rule block + placement guard
# ---------------------------------------------------------------------------


def test_intake_binding_standard_rule_block_and_placement():
    """Asserts (a) trigger phrases, (b) the conditional ## 3b table description
    and omit branch, (c) the read-or-stage-before-COMPLETE duty, and (d) the
    placement guard (## 3. < ## 3b. < ## 4. in the OUTPUT (B) region, and no
    ### 3b anywhere in that region)."""
    text = _intake_text()
    lower = _norm(text).lower()

    # (a) trigger phrases — case-insensitive.
    assert "binding standard:" in lower, "intake.md must name 'binding standard:' trigger"
    assert "binding:" in lower, "intake.md must name 'binding:' trigger"
    assert "as the binding" in lower, "intake.md must name 'follow ... as the binding' trigger"

    # (b) conditional ## 3b table description + omit branch.
    assert "## 3b. binding-standard coverage" in lower, (
        "intake.md OUTPUT (B) must contain the ## 3b. Binding-standard coverage header"
    )
    assert "in/out/deferred" in lower, "## 3b block must describe the 3-column table"
    assert "standard bullet" in lower, "## 3b block must name the 'Standard bullet' column"
    assert "dod id or rationale" in lower, (
        "## 3b block must name the 'DoD id or rationale' column"
    )
    assert "omit" in lower and "no empty/n/a table" in lower, (
        "## 3b block must state the omit-entirely branch (no empty/N/A table)"
    )

    # (c) read-or-stage-before-COMPLETE duty.
    assert "before intake: complete" in lower, (
        "intake.md must state the before-COMPLETE duty to read the standard"
    )
    assert "--ref" in lower, "intake.md must prefer a staged --ref for the standard"
    assert "do not invent" in lower, (
        "intake.md must warn against inventing bullets you did not read"
    )

    # (d) placement guard — ## 3. < ## 3b. < ## 4. in the OUTPUT (B) region,
    #     and no ### 3b (nested heading) in that region. Uses RAW text for
    #     exact positional checks (normalization would destroy line structure).
    raw = text
    b_start = text.find("(B) Nothing material is unresolved")
    assert b_start != -1, "intake.md must have an OUTPUT (B) region"
    # The OUTPUT (B) region ends where the RULES section begins.
    rules_start = text.find("\nRULES\n", b_start)
    if rules_start == -1:
        rules_start = len(text)
    region = text[b_start:rules_start]

    idx_3 = region.find("## 3. Rules / domain data")
    idx_3b = region.find("## 3b. Binding-standard coverage")
    idx_4 = region.find("## 4. Codebase anchors")
    assert idx_3 != -1, "OUTPUT (B) must contain '## 3. Rules / domain data'"
    assert idx_3b != -1, "OUTPUT (B) must contain '## 3b. Binding-standard coverage'"
    assert idx_4 != -1, "OUTPUT (B) must contain '## 4. Codebase anchors'"
    assert idx_3 < idx_3b < idx_4, (
        f"## 3b must sit strictly between ## 3 and ## 4 "
        f"(got 3={idx_3}, 3b={idx_3b}, 4={idx_4})"
    )
    assert "### 3b" not in region, (
        "OUTPUT (B) must not contain a nested '### 3b' heading — "
        "## 3b is a top-level section, not a subsection of ## 3"
    )


# ---------------------------------------------------------------------------
# 2. fixtures: existence, ≥4 bullets, seed names mini-standard, EXPECTED parse
# ---------------------------------------------------------------------------


def _parse_expected_comment(seed_text: str) -> dict[str, str]:
    """Parse the `# EXPECTED: rows=4; BS-2=Out; BS-3=Deferred` comment.

    Returns a dict like {"rows": "4", "BS-2": "Out", "BS-3": "Deferred"}.
    Raises AssertionError if the comment is missing or malformed.
    """
    m = re.search(r"#\s*EXPECTED:\s*(.+)$", seed_text, re.IGNORECASE)
    assert m, "cherry_picked_seed.md must contain a '# EXPECTED:' comment"
    body = m.group(1).strip()
    out: dict[str, str] = {}
    for part in body.split(";"):
        part = part.strip()
        if not part:
            continue
        assert "=" in part, f"EXPECTED entry {part!r} must be 'key=value'"
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    assert out, "EXPECTED comment parsed to nothing — malformed"
    return out


def test_fixture_files_and_expected_comment():
    """Asserts the two fixture files exist, ≥4 bullet headings in
    mini_standard.md, the seed names the mini-standard, the expected row count
    equals 4, and BS-2→Out / BS-3→Deferred are asserted by identity and status
    parsed from the # EXPECTED: comment."""
    mini = FIXTURES_DIR / "mini_standard.md"
    seed = FIXTURES_DIR / "cherry_picked_seed.md"
    assert mini.exists(), f"fixture missing: {mini}"
    assert seed.exists(), f"fixture missing: {seed}"

    mini_text = mini.read_text()
    bullet_ids = re.findall(r"^##\s+(BS-\d+)\b", mini_text, re.MULTILINE)
    assert len(bullet_ids) >= 4, (
        f"mini_standard.md must have ≥4 named bullet headings (BS-1..BS-4); "
        f"found {bullet_ids}"
    )
    # Distinct ids.
    assert len(set(bullet_ids)) >= 4, (
        f"mini_standard.md bullet ids must be distinct; got {bullet_ids}"
    )

    seed_text = seed.read_text()
    assert "tests/fixtures/binding_standard/mini_standard.md" in seed_text, (
        "cherry_picked_seed.md must name the mini_standard.md as its binding standard"
    )

    expected = _parse_expected_comment(seed_text)
    # Row count == 4.
    assert expected.get("rows") == "4", (
        f"EXPECTED rows must be 4; got {expected.get('rows')!r}"
    )
    # BS-2 → Out, BS-3 → Deferred — asserted by identity AND status.
    assert expected.get("BS-2") == "Out", (
        f"EXPECTED BS-2 must be 'Out'; got {expected.get('BS-2')!r}"
    )
    assert expected.get("BS-3") == "Deferred", (
        f"EXPECTED BS-3 must be 'Deferred'; got {expected.get('BS-3')!r}"
    )

    # Cross-check: the seed's 3b table actually lists BS-1 and BS-4 as In,
    # and BS-2/BS-3 with the same statuses the EXPECTED comment encodes.
    table_rows = re.findall(
        r"^\|\s*(BS-\d+)\s*\|\s*(In|Out|Deferred)\s*\|", seed_text, re.MULTILINE
    )
    table_map = {bs: status for bs, status in table_rows}
    assert table_map.get("BS-2") == "Out", (
        f"3b table BS-2 must be Out; got {table_map.get('BS-2')!r}"
    )
    assert table_map.get("BS-3") == "Deferred", (
        f"3b table BS-3 must be Deferred; got {table_map.get('BS-3')!r}"
    )
    assert table_map.get("BS-1") == "In", "3b table BS-1 must be In"
    assert table_map.get("BS-4") == "In", "3b table BS-4 must be In"
    assert len(table_map) == 4, f"3b table must have 4 rows; got {len(table_map)}"


# ---------------------------------------------------------------------------
# 3. judge.md residual-checklist HARD RULE
# ---------------------------------------------------------------------------


def test_judge_residual_obligations_hard_rule():
    """Asserts the judge.md HARD RULE text (residual split: checklist vs.
    plan-delta) and its non-trigger list."""
    text = _judge_text()
    lower = _norm(text).lower()

    assert "hard rule" in lower, "judge.md must contain a HARD RULE"
    assert "residual obligations" in lower, (
        "judge.md HARD RULE must name 'residual obligations'"
    )
    assert "scope-narrowing" in lower or "narrows the task's scope" in lower, (
        "judge.md HARD RULE must describe scope-narrowing rulings"
    )
    # Residual split: in-scope → checklist; deferred/out → plan-delta.
    assert "conformance checklist" in lower, (
        "judge.md must route in-scope residuals to CONFORMANCE CHECKLIST items"
    )
    assert "consolidated-plan delta" in lower or "consolidated plan delta" in lower, (
        "judge.md must route deferred/out residuals to a Consolidated-plan delta"
    )
    # Non-triggers: batch reordering + test-spec corrections.
    assert "batch reordering" in lower, (
        "judge.md must list 'batch reordering' as a non-trigger"
    )
    assert "test-spec corrections" in lower, (
        "judge.md must list 'test-spec corrections' as a non-trigger"
    )


# ---------------------------------------------------------------------------
# 4. intake.md §5 universal-quantifier enumeration rule (C3)
# ---------------------------------------------------------------------------


def test_enumeration_rule_in_section_5():
    """Asserts intake.md §5 names `all`, `every`, `no remaining`, and
    `zero ... left` and requires enumeration."""
    text = _intake_text()
    # Isolate §5 (Definition of done) — from its header to the next ## header.
    # Headers in the OUTPUT (B) template are indented with 4 spaces, so allow
    # leading whitespace. Match either "## 5." or "    ## 5.".
    m = re.search(
        r"^[\t ]*##\s+5\.\s*Definition of done(.*?)(?=^[\t ]*##\s+\d+\b)",
        text,
        re.DOTALL | re.MULTILINE,
    )
    assert m, "intake.md must have a §5 Definition of done section"
    section5 = _norm(m.group(1)).lower()

    for phrase in ("all", "every", "no remaining", "zero ... left"):
        assert phrase in section5, (
            f"intake.md §5 must name the universal quantifier {phrase!r}"
        )
    assert "enumerate" in section5, (
        "intake.md §5 must require enumeration of universal-quantifier DoD items"
    )


# ---------------------------------------------------------------------------
# 5. intake.md §7 global-flag dual-position rule (C4)
# ---------------------------------------------------------------------------


def test_global_flag_dual_position_rule_in_section_7():
    """Asserts intake.md §7's dual-position rule and its omit branch."""
    text = _intake_text()
    m = re.search(
        r"^[\t ]*##\s+7\.\s*Manual acceptance(.*?)(?=^[\t ]*##\s+\d+\b)",
        text,
        re.DOTALL | re.MULTILINE,
    )
    assert m, "intake.md must have a §7 Manual acceptance section"
    section7 = _norm(m.group(1)).lower()

    assert "--flag <subcmd>" in section7, (
        "§7 must show the pre-subcommand position './run.py --flag <subcmd> ...'"
    )
    assert "<subcmd> ... --flag" in section7, (
        "§7 must show the post-subcommand position './run.py <subcmd> ... --flag'"
    )
    assert "both" in section7, "§7 must require BOTH positions"
    # Omit branch when no global flag is claimed.
    assert "no global cli flag" in section7 or "no global flag" in section7, (
        "§7 must state the omit branch when no global CLI flag is in scope"
    )


# ---------------------------------------------------------------------------
# 6. regression: non-binding briefs must NOT require a 3b section
# ---------------------------------------------------------------------------


# Reuse the existing CONTRACT fixture from the contract-gate test module so the
# regression test stays in lockstep with the real contract-brief shape.
from tests.test_intake_contract_gate import CONTRACT as _NO_3B_CONTRACT  # noqa: E402


def test_reg_no_3b_required_for_non_binding_briefs():
    """is_contract_brief / missing_contract_sections must NOT require a `3b`
    section — enforcement is prose-only in the prompt, not programmatic. Uses
    the existing CONTRACT fixture (which has no 3b) plus a local no-3b brief."""
    # The shared CONTRACT fixture has no ## 3b section.
    assert "## 3b" not in _NO_3B_CONTRACT, (
        "precondition: the shared CONTRACT fixture must not contain ## 3b"
    )
    # It must still be recognised as a valid contract brief.
    assert is_contract_brief(_NO_3B_CONTRACT), (
        "is_contract_brief must accept a brief with no 3b section"
    )
    assert missing_contract_sections(_NO_3B_CONTRACT) == [], (
        f"missing_contract_sections must be [] for a valid no-3b brief; "
        f"got {missing_contract_sections(_NO_3B_CONTRACT)}"
    )

    # A local no-3b brief (binding standard NOT mentioned) is also accepted.
    local_brief = (
        "UI-SURFACE: no\n\n"
        "## 1. Goal\nDo X.\n\n"
        "## 2. Corrections to the request\nnone\n\n"
        "## 3. Rules / domain data\nnone\n\n"
        "## 4. Codebase anchors\n- run.py\n\n"
        "## 4b. Architecture docs to follow\nnone\n\n"
        "## 5. Definition of done\n| F1 | X — verified by reading |\n\n"
        "## 6. Scope: in / out\n### In\n- X\n\n"
        "## 7. Manual acceptance\n1. Read.\n\n"
        "## 8. Unverified assumptions\nnone\n"
    )
    assert "## 3b" not in local_brief, "local brief must not contain ## 3b"
    assert is_contract_brief(local_brief), (
        "is_contract_brief must accept a local no-3b, non-binding brief"
    )
    assert missing_contract_sections(local_brief) == [], (
        f"missing_contract_sections must be [] for the local no-3b brief; "
        f"got {missing_contract_sections(local_brief)}"
    )
