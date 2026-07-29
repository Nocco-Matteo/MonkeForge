import tempfile
import unittest
from pathlib import Path

from pipeline_graph.intake_materialize import (
    extract_before_marker,
    materialize_intake_output,
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


if __name__ == "__main__":
    unittest.main()
