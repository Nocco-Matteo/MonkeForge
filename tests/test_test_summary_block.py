"""TASK-030: ``format_test_summary_block`` block shapes + sanitizer.

Covers items 60-63:
  * green/red/skipped/unconfigured block shapes and the authoritative flag
    true/false (item 60).
  * sanitizer ``</`` → ``<\\/`` applied to ``raw``, a ``new_failures`` entry,
    and a suite label — three distinct cases (item 61).
  * ``<test_summary>``/``</test_summary>`` tokens stripped from interpolated
    text (item 62).
  * 600-char truncation with ellipsis for an over-long ``raw``/failure-key/
    suite-label (item 63).
"""
import unittest

from pipeline_graph import test_runner as tr


class TestBlockShapes(unittest.TestCase):
    def test_green_block_is_authoritative(self):
        block = tr.format_test_summary_block(
            "green", ["fe", "be"], [], "5 passed, 0 failed",
            authoritative=True,
        )
        self.assertIn('status="green"', block)
        self.assertIn('authoritative="true"', block)
        self.assertIn("fe, be", block)
        self.assertIn("new_failures: (none)", block)
        self.assertTrue(block.startswith("<test_summary"))
        self.assertTrue(block.rstrip().endswith("</test_summary>"))

    def test_red_block_lists_new_failures(self):
        block = tr.format_test_summary_block(
            "red", ["fe"], ["tests/a.test.ts > a"], "1 failed",
            authoritative=True,
        )
        self.assertIn('status="red"', block)
        self.assertIn('authoritative="true"', block)
        self.assertIn("tests/a.test.ts > a", block)
        self.assertIn("new_failures:", block)

    def test_skipped_block_is_non_authoritative(self):
        block = tr.format_test_summary_block(
            "skipped", ["fe"], [], "", authoritative=False,
        )
        self.assertIn('status="skipped"', block)
        self.assertIn('authoritative="false"', block)

    def test_unconfigured_block_is_non_authoritative(self):
        block = tr.format_test_summary_block(
            "unconfigured", [], [], "", authoritative=False,
        )
        self.assertIn('status="unconfigured"', block)
        self.assertIn('authoritative="false"', block)
        self.assertIn("(none)", block)


class TestSanitizerEscaping(unittest.TestCase):
    """Item 61: ``</`` → ``<\\/`` applied to raw, a new_failures entry, and a
    suite label — three distinct cases so a hostile runner output cannot close
    the wrapping <test_summary> element early."""

    def test_raw_closing_tag_is_escaped(self):
        block = tr.format_test_summary_block(
            "green", ["fe"], [], "</test_summary>INJECTED",
            authoritative=True,
        )
        # The injected closing tag must be escaped, not interpreted.
        self.assertNotIn("</test_summary>INJECTED", block)
        self.assertIn("<\\/test_summary>INJECTED", block)

    def test_new_failure_entry_closing_tag_is_escaped(self):
        block = tr.format_test_summary_block(
            "red", ["fe"], ["tests/x > </test_summary>break"], "1 failed",
            authoritative=True,
        )
        self.assertNotIn("</test_summary>break", block)
        self.assertIn("<\\/test_summary>break", block)

    def test_suite_label_closing_tag_is_escaped(self):
        block = tr.format_test_summary_block(
            "green", ["</test_summary>evil"], [], "ok",
            authoritative=True,
        )
        self.assertNotIn("</test_summary>evil", block)
        self.assertIn("<\\/test_summary>evil", block)


class TestSanitizerTokenStripping(unittest.TestCase):
    """Item 62: ``<test_summary>``/``</test_summary>`` tokens stripped from
    interpolated text so a stale block carried in raw stdout cannot nest."""

    def test_tokens_stripped_from_raw(self):
        block = tr.format_test_summary_block(
            "green", ["fe"], [],
            "<test_summary>stale</test_summary> real",
            authoritative=True,
        )
        # The stale opening/closing tokens are stripped from the interpolated
        # raw text (the wrapping block's own tags remain).
        # Count: the only <test_summary> / </test_summary> tokens in the output
        # are the wrapper's own (one open, one close).
        opens = block.count("<test_summary")
        closes = block.count("</test_summary>")
        self.assertEqual(opens, 1)
        self.assertEqual(closes, 1)
        self.assertIn("stale", block)
        self.assertIn("real", block)

    def test_tokens_stripped_case_insensitive(self):
        block = tr.format_test_summary_block(
            "green", ["fe"], [], "<TEST_SUMMARY>stale</TEST_SUMMARY>",
            authoritative=True,
        )
        opens = block.lower().count("<test_summary")
        closes = block.lower().count("</test_summary>")
        self.assertEqual(opens, 1)
        self.assertEqual(closes, 1)


class TestSanitizerTruncation(unittest.TestCase):
    """Item 63: 600-char truncation with ellipsis for an over-long raw /
    failure-key / suite-label."""

    def test_overlong_raw_is_truncated(self):
        long_raw = "x" * 1000
        block = tr.format_test_summary_block(
            "green", ["fe"], [], long_raw, authoritative=True,
        )
        self.assertIn("…[truncated]", block)
        # The raw line is capped: 600 chars + ellipsis marker.
        raw_line = [ln for ln in block.splitlines() if ln.startswith("raw: ")][0]
        # "raw: " (5) + 600 + len("…[truncated]")
        self.assertLessEqual(len(raw_line), 5 + 600 + len("…[truncated]"))

    def test_overlong_failure_key_is_truncated(self):
        long_fail = "f" * 1000
        block = tr.format_test_summary_block(
            "red", ["fe"], [long_fail], "1 failed", authoritative=True,
        )
        self.assertIn("…[truncated]", block)
        fail_line = [ln for ln in block.splitlines() if ln.startswith("  - ")][0]
        self.assertLessEqual(len(fail_line), 4 + 600 + len("…[truncated]"))

    def test_overlong_suite_label_is_truncated(self):
        long_label = "s" * 1000
        block = tr.format_test_summary_block(
            "green", [long_label], [], "ok", authoritative=True,
        )
        self.assertIn("…[truncated]", block)
        suites_line = [ln for ln in block.splitlines() if ln.startswith("suites: ")][0]
        self.assertLessEqual(len(suites_line), 8 + 600 + len("…[truncated]"))


if __name__ == "__main__":
    unittest.main()
