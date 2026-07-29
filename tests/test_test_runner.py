import unittest

from pipeline_graph import test_runner as tr


class TestRunnerParsing(unittest.TestCase):
    SAMPLE = """
 ✓ src/foo.test.ts (1 test) 1ms
 FAIL  src/a.test.ts > suite A > case one
 FAIL  src/b.test.ts > suite B > case two

 Test Files  2 failed | 1 passed (3)
      Tests  2 failed | 5 passed (7)
"""

    ALL_PASSED = """
 Test Files  30 passed (30)
      Tests  367 passed | 6 skipped (373)
"""

    def test_parse_vitest_failures(self):
        got = tr.parse_vitest_failures(self.SAMPLE)
        self.assertEqual(
            got,
            {
                "src/a.test.ts > suite A > case one",
                "src/b.test.ts > suite B > case two",
            },
        )

    def test_new_failures_since_baseline(self):
        current = {
            "backend|src/a.test.ts > suite A > case one",
            "backend|src/c.test.ts > suite C > new",
        }
        baseline = {"backend|src/a.test.ts > suite A > case one"}
        self.assertEqual(
            tr.new_failures_since_baseline(current, baseline, ["src/c.test.ts > suite C"]),
            set(),
        )
        self.assertEqual(
            tr.new_failures_since_baseline(current, baseline, []),
            {"backend|src/c.test.ts > suite C > new"},
        )

    def test_summarize_zero_failures(self):
        s = tr._summarize_vitest_output(self.ALL_PASSED, 0, 0)
        self.assertIn("367 passed", s)
        self.assertIn("0 failed", s)

    def test_parse_tsc_errors(self):
        sample = (
            "src/modules/character/services/characterService.ts(254,18): "
            "error TS2352: Conversion of type 'InventoryEntry[]' to type "
            "'InputJsonValue' may be a mistake.\n"
            "src/modules/foo.ts(10,5): error TS2304: Cannot find name 'bar'.\n"
        )
        got = tr.parse_tsc_errors(sample)
        self.assertEqual(len(got), 2)
        self.assertTrue(any("TS2352" in f for f in got))
        self.assertTrue(any("TS2304" in f for f in got))
        self.assertTrue(any("characterService.ts:254:18" in f for f in got))


if __name__ == "__main__":
    unittest.main()
