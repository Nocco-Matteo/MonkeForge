import unittest

import run


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
