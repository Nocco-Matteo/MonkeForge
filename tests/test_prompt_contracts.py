"""Prompt contract tests: every prompt .md exists, is non-empty, and has the
required markers/placeholders that the node code expects.

If a prompt is renamed or a placeholder is removed, the node silently renders
``{task_id}`` literally into the agent prompt.  These tests catch that drift.
"""

import pytest

from pipeline_graph import config as C

PROMPTS_DIR = C.TEMPLATES

# Every prompt the codebase calls render_prompt() with, extracted from graph.py
# and nodes/*.py.  If a prompt is added or renamed, update this set.
EXPECTED_PROMPTS = {
    "intake",
    "plan",
    "debate_review",
    "debate_ux",
    "debate_reply",
    "summary",
    "judge",
    "implement",
    "code_review",
    "code_fix",
    "code_verify",
    "final_check",
    "preflight_fix",
    "visual_review",
    "visual_fix",
    "render_fix",
}


def _prompt_names() -> set[str]:
    return {p.stem for p in PROMPTS_DIR.glob("*.md")}


# --- existence -------------------------------------------------------------


class TestPromptExistence:
    def test_all_expected_prompts_exist(self):
        missing = EXPECTED_PROMPTS - _prompt_names()
        assert not missing, f"Missing prompt files: {missing}"

    def test_no_extra_prompts(self):
        extra = _prompt_names() - EXPECTED_PROMPTS
        # Not an error per se, but worth surfacing so we update EXPECTED_PROMPTS.
        if extra:
            pytest.fail(f"Prompt files not referenced in code: {extra}")

    def test_all_prompts_non_empty(self):
        for p in PROMPTS_DIR.glob("*.md"):
            assert p.stat().st_size > 0, f"Empty prompt: {p.name}"


# --- placeholder contracts -------------------------------------------------

# Map prompt name → set of placeholders the code passes to render_prompt.
# If a prompt loses a placeholder, the literal {name} survives into the agent
# prompt — a silent bug.  These tests catch that.
PROMPT_PLACEHOLDERS = {
    "intake": {
        "task_id",
        "round",
        "max_rounds",
        "request",
        "brief_path",
        "intake_path",
        "refs_path",
        "refs_list",
    },
    "plan": {"request", "brief", "arch_docs", "docs_dir"},
    "debate_review": {"plan_view", "debate_ledger", "round"},
    "debate_ux": {"plan_view", "debate_ledger", "round", "tech_limits", "docs_dir"},
    "debate_reply": {"plan", "debate_history", "round", "tech_limits"},
    "summary": {"debate_history"},
    "judge": {"summary", "plan", "debate_history", "docs_dir"},
    "implement": {
        "batch_n",
        "batch_scope",
        "final",
        "progress",
        "db_note",
        "arch_docs",
        "checklist_items",
        "failures",
        "summary",
    },
    "code_review": {
        "batch_n",
        "batch_scope",
        "diff_base",
        "checklist_items",
        "trusted_context",
        "final",
    },
    "code_fix": {"task_id", "batch_n", "review_history"},
    "code_verify": {"task_id", "batch_n", "review_history"},
    "final_check": {"db_note", "final"},
    "preflight_fix": {"failures", "summary", "remaining_label"},
    "visual_review": {"screens_dir", "render_facts"},
    "visual_fix": {"screens_dir", "render_facts", "visual_review"},
    "render_fix": {"task_id", "render_delta"},
}


class TestPromptPlaceholders:
    @pytest.mark.parametrize("prompt_name", sorted(EXPECTED_PROMPTS))
    def test_placeholders_present(self, prompt_name):
        path = PROMPTS_DIR / f"{prompt_name}.md"
        if not path.exists():
            pytest.skip(f"{prompt_name}.md not found (covered by TestPromptExistence)")
        text = path.read_text()
        expected = PROMPT_PLACEHOLDERS.get(prompt_name, set())
        missing = set()
        for ph in expected:
            if "{" + ph + "}" not in text:
                missing.add(ph)
        assert not missing, (
            f"{prompt_name}.md is missing placeholders: {missing}. "
            f"The node passes these to render_prompt(); a missing placeholder "
            f"means the literal {{{{name}}}} ends up in the agent prompt."
        )
        # Regression guard: the two critic templates must NOT still contain the
        # old {debate_history} placeholder (catches a partial-swap regression).
        if prompt_name in ("debate_review", "debate_ux"):
            assert "{debate_history}" not in text, (
                f"{prompt_name}.md still contains the old {{debate_history}} "
                f"placeholder — the critic templates must use {{debate_ledger}}."
            )
            # TASK-023: the critic templates must use {plan_view} (the lean
            # plan input), not the raw {plan} placeholder.
            assert "{plan_view}" in text, (
                f"{prompt_name}.md must reference the {{plan_view}} placeholder "
                f"(the lean plan input computed by _build_plan_view)."
            )
            assert "{plan}" not in text, (
                f"{prompt_name}.md must not reference the raw {{plan}} "
                f"placeholder — use {{plan_view}} instead."
            )

    def test_debate_reply_has_severity_verbatim_prefix(self):
        """debate_reply.md must instruct the proposer to begin each item with
        the reviewer's exact [SEVERITY] <claim> tag — the producer-side anchor
        for the tag-based ledger parser (§6 signal (b)(ii))."""
        text = (PROMPTS_DIR / "debate_reply.md").read_text()
        assert "[SEVERITY]" in text or "[BLOCKER]" in text, (
            "debate_reply.md must reference the [SEVERITY]/[BLOCKER] verbatim "
            "prefix instruction for the tag-based parser."
        )

    def test_judge_uses_write_primary_not_stdout(self):
        """judge.md must use WRITE-primary wording, not PRINT-to-stdout, to
        reflect the file-primary BATCHES load path."""
        text = (PROMPTS_DIR / "judge.md").read_text()
        assert "WRITE" in text, (
            "judge.md must use WRITE-primary wording for the FINAL/BATCHES "
            "content, not PRINT-to-stdout."
        )
        # The old "PRINT ... to stdout" channel wording must be gone.
        assert "PRINT the FINAL" not in text, (
            "judge.md still uses 'PRINT the FINAL' stdout wording."
        )
        assert "PRINT the BATCHES" not in text, (
            "judge.md still uses 'PRINT the BATCHES' stdout wording."
        )


# --- structural markers ----------------------------------------------------

# Prompts that instruct the agent to print between markers must contain those
# markers in the prompt text itself, or the agent will not know to use them.
MARKER_CONTRACTS = {
    "debate_reply": ["PLAN PATCH START", "PLAN PATCH END"],
    "implement": ["PLAN_DISCREPANCY:"],
}


class TestPromptMarkers:
    @pytest.mark.parametrize("prompt_name,markers", [(n, ms) for n, ms in MARKER_CONTRACTS.items()])
    def test_markers_present(self, prompt_name, markers):
        path = PROMPTS_DIR / f"{prompt_name}.md"
        if not path.exists():
            pytest.skip(f"{prompt_name}.md not found")
        text = path.read_text()
        for marker in markers:
            assert marker in text, (
                f"{prompt_name}.md is missing marker '{marker}'. "
                f"The node expects the agent to print between === {marker} === markers."
            )


# --- verification pass (TASK-022) -------------------------------------------


class TestVerificationPassSection:
    """TASK-022 item 22/F3: both critic prompts must contain a VERIFICATION
    PASS section with the three F3 rules, including the literal strings
    ``residual audit`` and ``[SUGGESTION]``."""

    @pytest.mark.parametrize("prompt_name", ["debate_review", "debate_ux"])
    def test_verification_pass_section_present(self, prompt_name):
        path = PROMPTS_DIR / f"{prompt_name}.md"
        if not path.exists():
            pytest.skip(f"{prompt_name}.md not found")
        text = path.read_text()
        assert "VERIFICATION PASS" in text, (
            f"{prompt_name}.md is missing the VERIFICATION PASS section."
        )
        assert "residual audit" in text, (
            f"{prompt_name}.md is missing the 'residual audit' rule."
        )
        assert "[SUGGESTION]" in text, (
            f"{prompt_name}.md is missing the '[SUGGESTION]' rule."
        )
