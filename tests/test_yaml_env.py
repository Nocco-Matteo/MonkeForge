import unittest

import run


class PauseInstructions(unittest.TestCase):
    def test_effort_pause_answers_use_levels(self):
        import run
        data = {
            "stage": "effort level",
            "hint": "troop-monke",
            "levels": ["scout-monke", "troop-monke", "barrel-monke"],
        }
        self.assertEqual(run._pause_reason(data),
                         "choose an effort level (recommended: troop-monke)")
        self.assertEqual(run._pause_answers(data), {
            "scout-monke": "select this effort level",
            "troop-monke": "select this effort level",
            "barrel-monke": "select this effort level",
        })

    def test_pause_answers_preserve_descriptions(self):
        import run
        answers = {"ok": "continue"}
        self.assertEqual(run._pause_answers({"answers": answers}), answers)


class EnvStr(unittest.TestCase):
    """YAML booleans must reach env vars as lowercase 'true'/'false'.

    Regression: str(True) == 'True', but gemini's GEMINI_CLI_TRUST_WORKSPACE
    only accepts 'true' and refuses to run in an untrusted dir otherwise — which
    silently broke every gemini agent step (INTERVIEWER, UX_REVIEWER)."""

    def test_bool_true_is_lowercase(self):
        self.assertEqual(run._envstr(True), "true")

    def test_bool_false_is_lowercase(self):
        self.assertEqual(run._envstr(False), "false")

    def test_non_bool_passthrough(self):
        self.assertEqual(run._envstr("gemini-3.6-flash"), "gemini-3.6-flash")
        self.assertEqual(run._envstr(30), "30")


if __name__ == "__main__":
    unittest.main()
