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


class TestGateFalsePositiveSuppression(unittest.TestCase):
    """The three false-positive classes that hit TASK-016 batches 1-2."""

    # --- LINT debt: rule already in baseline (any file) → not new -------------

    def test_lint_debt_rule_in_baseline_not_new(self):
        """A LINT rule present in baseline (any file) is not new in another file."""
        baseline = {
            "frontend|LINT|/repo/old.ts|@typescript-eslint/no-explicit-any|Unexpected any",
        }
        current = {
            "frontend|LINT|/repo/old.ts|@typescript-eslint/no-explicit-any|Unexpected any",
            "frontend|LINT|/repo/newStore.ts|@typescript-eslint/no-explicit-any|Unexpected any",
        }
        new = tr.new_failures_since_baseline(
            current, baseline, [],
            lint_debt_rules=("@typescript-eslint/no-explicit-any",),
        )
        self.assertEqual(new, set())

    def test_lint_debt_rule_not_in_baseline_is_new(self):
        """A LINT rule NOT in baseline in any file is a real regression."""
        baseline = {
            "frontend|LINT|/repo/old.ts|react-refresh/only-export-components|Fast refresh",
        }
        current = baseline | {
            "frontend|LINT|/repo/new.ts|@typescript-eslint/no-explicit-any|Unexpected any",
        }
        new = tr.new_failures_since_baseline(
            current, baseline, [],
            lint_debt_rules=("@typescript-eslint/no-explicit-any",),
        )
        self.assertEqual(new, {
            "frontend|LINT|/repo/new.ts|@typescript-eslint/no-explicit-any|Unexpected any",
        })

    def test_lint_debt_rule_not_in_allowlist_passes_through(self):
        """If a rule is not in lint_debt_rules, it is not suppressed even if in baseline."""
        baseline = {
            "frontend|LINT|/repo/old.ts|@typescript-eslint/no-explicit-any|Unexpected any",
        }
        current = baseline | {
            "frontend|LINT|/repo/new.ts|@typescript-eslint/no-explicit-any|Unexpected any",
        }
        new = tr.new_failures_since_baseline(
            current, baseline, [],
            lint_debt_rules=(),  # debt suppression disabled
        )
        self.assertEqual(new, {
            "frontend|LINT|/repo/new.ts|@typescript-eslint/no-explicit-any|Unexpected any",
        })

    # --- Ambient test: DB-gated failure → not new -----------------------------

    def test_ambient_test_failure_suppressed(self):
        """A vitest failure matching an ambient pattern is not new."""
        baseline = set()  # test was skipped in baseline (DB down)
        current = {
            "backend|src/tests/magicAutoGrant.integration.test.ts > "
            "magic auto-grant: class domain & patron spells (real compendium)",
        }
        new = tr.new_failures_since_baseline(
            current, baseline, [],
            ambient_patterns=("magic auto-grant: class domain & patron spells",),
        )
        self.assertEqual(new, set())

    def test_non_ambient_test_failure_is_new(self):
        """A vitest failure NOT matching any ambient pattern is a real regression."""
        baseline = set()
        current = {
            "backend|src/tests/characterService.test.ts > should create character",
        }
        new = tr.new_failures_since_baseline(
            current, baseline, [],
            ambient_patterns=("magic auto-grant",),
        )
        self.assertEqual(new, current)

    # --- Backward compat: no new args → original behaviour --------------------

    def test_no_suppression_args_preserves_original_behaviour(self):
        """Without lint_debt_rules/ambient_patterns, the function is unchanged."""
        baseline = {"backend|src/a.test.ts > suite A > case one"}
        current = baseline | {"backend|src/c.test.ts > suite C > new"}
        new = tr.new_failures_since_baseline(current, baseline, [])
        self.assertEqual(new, {"backend|src/c.test.ts > suite C > new"})


class TestNpmVitestLabelPrefix(unittest.TestCase):
    """Regression (TASK-026): npm-vitest failure keys stay prefixed ``label|``.

    The refactored ``_run_npm_vitest`` (former ``_run_suite`` body) must keep
    prefixing every failure key with the suite label, so the baseline
    comparison and the judge's allowlist substring match keep working.
    """

    def test_npm_vitest_label_prefix(self):
        from unittest.mock import patch
        from pathlib import Path
        from pipeline_graph import config as C
        suite = C.TestSuite(label="frontend", cwd="frontend", runner="npm-vitest")
        vt_out = (
            "FAIL  src/a.test.ts > suite A > case one\n"
            "FAIL  src/b.test.ts > suite B > case two\n"
            "Tests  2 failed | 5 passed (7)\n"
        )
        with patch.object(tr, "_run_cmd",
                          side_effect=[(0, "typecheck ok"),
                                       (1, vt_out),
                                       (0, "[]")]), \
             patch.object(C, "REPO", Path("/tmp")):
            code, fails, summary = tr._run_npm_vitest(suite, 60)
        # Every failure key is prefixed "frontend|".
        self.assertTrue(fails, "expected non-empty failure set")
        for f in fails:
            self.assertTrue(f.startswith("frontend|"),
                            f"failure key not label-prefixed: {f!r}")
        # The two vitest FAIL lines are present (not just the synthetic exit key).
        self.assertTrue(any("case one" in f for f in fails))
        self.assertTrue(any("case two" in f for f in fails))


if __name__ == "__main__":
    unittest.main()
