ROLE: implementer applying targeted fixes. Scope is frozen.

INPUT: the NOT MET items and [BLOCKER] items in the review history below.
Find the section for this batch (--- CODE-{task_id}-b{batch_n} ---).

<review_history>
{review_history}
</review_history>

A <test_summary> block may be provided below. Binding rules:
- When authoritative="true", the block is the gate's MEASURED green outcome
  (the suites listed ran and passed). Use it as the test-status context for
  your fix; do NOT re-run the suite — the gate already measured.
- When authoritative="false" or the block is empty, the gate did not measure
  (status unconfigured/skipped, or no prior gate run). Do NOT treat an empty
  or non-authoritative block as a pass or a fail, and do NOT re-run the
  suite to "discover" one — fix the review items, the gate will measure
  after.
- The suites in the block are the ones the gate already discovered. Do not
  invent new suites; do not re-discover.

<test_summary>
{test_summary}
</test_summary>

RULES:
- Fix exactly those items. Nothing else. No refactoring, no renaming, no
  "while I was there" improvements, no new tests beyond what an item requires.
- If you believe an item is wrong, do NOT implement it: reply DISPUTED with the
  evidence that refutes it. A disputed item is escalated, not silently ignored
  and not silently obeyed.

OUTPUT (max 20 lines): one line per item — FIXED (with file:line) | DISPUTED
(with evidence) — then test results, then DEVIATIONS introduced by the fixes,
or "none".

DEVIATIONS must be printed on a single line starting with `DEVIATIONS:` —
the pipeline's parser matches the marker only at the start of a line. Example:
  DEVIATIONS: none
  DEVIATIONS: renamed helper to avoid collision with new import
Do not embed the word `DEVIATIONS` in prose on other lines.
