import tempfile
import unittest
from pathlib import Path

from pipeline_graph.intake_materialize import (
    extract_before_marker,
    is_contract_brief,
    materialize_intake_output,
    missing_contract_sections,
    sanitize_agent_doc,
)


SAMPLE_QUESTIONS = """tool noise here
Error executing tool

## Round 1

### Q1. Ambito MVP
Evidence: seed
Why it matters: scope
**A:**

INTAKE: QUESTIONS 1
"""

SAMPLE_CONTRACT = """UI-SURFACE: yes

# TASK-006: example

## 1. Goal

Ship the thing.

## 2. Corrections to the request

none

## 3. Rules / domain data

none

## 4. Codebase anchors

- `run.py`

## 4b. Architecture docs to follow

none

## 5. Definition of done

| ID | Criterion |
|----|-----------|
| F1 | Works — verified by `pytest` |

## 6. Scope: in / out

### In

- the thing

### Out

- the other thing

## 7. Manual acceptance

1. Run it.

## 8. Unverified assumptions

none

INTAKE: COMPLETE
"""

# The 018 failure mode: agent claims COMPLETE with a chat summary.
SAMPLE_STATUS_COMPLETE = """Round 1 complete. I read the seed brief, confirmed all six gaps against `run.py`.

The contract brief is at `docs/MonkeForge-clone/tasks/TASK-018-brief.md`. Four design choices are locked as defaults.

INTAKE: COMPLETE
"""


class IntakeMaterialize(unittest.TestCase):
    def test_sanitize_drops_preamble(self):
        body = sanitize_agent_doc(SAMPLE_QUESTIONS)
        self.assertTrue(body.startswith("## Round 1"))

    def test_materialize_questions_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            intake_p = Path(tmp) / "intake.md"
            brief_p = Path(tmp) / "brief.md"
            ok = materialize_intake_output(
                "006", 1, SAMPLE_QUESTIONS,
                intake_path=intake_p, brief_path=brief_p,
            )
            self.assertTrue(ok)
            self.assertTrue(intake_p.exists())
            self.assertIn("Q1. Ambito MVP", intake_p.read_text())
            self.assertNotIn("INTAKE: QUESTIONS", intake_p.read_text())

    def test_extract_before_marker(self):
        self.assertEqual(
            extract_before_marker("hello\n\nINTAKE: COMPLETE", "INTAKE: COMPLETE"),
            "hello",
        )

    def test_is_contract_brief_accepts_structured(self):
        body = extract_before_marker(SAMPLE_CONTRACT, "INTAKE: COMPLETE")
        self.assertTrue(is_contract_brief(body))
        self.assertEqual(missing_contract_sections(body), [])

    def test_is_contract_brief_rejects_status_summary(self):
        body = extract_before_marker(SAMPLE_STATUS_COMPLETE, "INTAKE: COMPLETE")
        self.assertFalse(is_contract_brief(body))
        missing = missing_contract_sections(body)
        self.assertIn("UI-SURFACE", missing)
        self.assertIn("Goal", missing)

    def test_materialize_complete_writes_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            intake_p = Path(tmp) / "intake.md"
            brief_p = Path(tmp) / "brief.md"
            ok = materialize_intake_output(
                "006", 1, SAMPLE_CONTRACT,
                intake_path=intake_p, brief_path=brief_p,
            )
            self.assertTrue(ok)
            text = brief_p.read_text()
            self.assertIn("UI-SURFACE: yes", text)
            self.assertIn("## 1. Goal", text)
            self.assertNotIn("INTAKE: COMPLETE", text)

    def test_materialize_complete_does_not_clobber_seed_with_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            intake_p = Path(tmp) / "intake.md"
            brief_p = Path(tmp) / "brief.md"
            seed = extract_before_marker(SAMPLE_CONTRACT, "INTAKE: COMPLETE") + "\n"
            brief_p.write_text(seed)
            ok = materialize_intake_output(
                "018", 1, SAMPLE_STATUS_COMPLETE,
                intake_path=intake_p, brief_path=brief_p,
            )
            self.assertFalse(ok)
            self.assertEqual(brief_p.read_text(), seed)

    def test_materialize_questions_skips_when_tool_already_wrote(self):
        """Cursor Write + stdout QUESTIONS must not double-append Round 1."""
        with tempfile.TemporaryDirectory() as tmp:
            intake_p = Path(tmp) / "intake.md"
            brief_p = Path(tmp) / "brief.md"
            # Simulate pre-agent: no file (mtime_before=None), agent Write:
            body = sanitize_agent_doc(
                extract_before_marker(SAMPLE_QUESTIONS, "INTAKE: QUESTIONS")
            )
            intake_p.write_text(body + "\n")
            before = None  # did not exist before the agent
            ok = materialize_intake_output(
                "033", 1, SAMPLE_QUESTIONS,
                intake_path=intake_p, brief_path=brief_p,
                intake_mtime_before=before,
            )
            self.assertFalse(ok)
            text = intake_p.read_text()
            self.assertEqual(text.count("## Round 1"), 1)

    def test_materialize_questions_still_appends_when_untouched(self):
        """Gemini stdout-only on an existing prior-round file still appends."""
        with tempfile.TemporaryDirectory() as tmp:
            intake_p = Path(tmp) / "intake.md"
            brief_p = Path(tmp) / "brief.md"
            intake_p.write_text("## Round 1\n\n### Q1. old\n**A:** x\n")
            before = intake_p.stat().st_mtime
            # Keep mtime stable relative to snapshot (no tool write).
            ok = materialize_intake_output(
                "033", 2, SAMPLE_QUESTIONS,
                intake_path=intake_p, brief_path=brief_p,
                intake_mtime_before=before,
            )
            self.assertTrue(ok)
            text = intake_p.read_text()
            self.assertIn("### Q1. old", text)
            self.assertIn("Q1. Ambito MVP", text)
            self.assertEqual(text.count("## Round 1"), 2)


if __name__ == "__main__":
    unittest.main()
