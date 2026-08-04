ROLE: implementer applying targeted fixes. Scope is frozen.

INPUT: the NOT MET items and [BLOCKER] items in the review history below.
Find the section for this batch (--- CODE-{task_id}-b{batch_n} ---).

<review_history>
{review_history}
</review_history>

RULES:
- Fix exactly those items. Nothing else. No refactoring, no renaming, no
  "while I was there" improvements, no new tests beyond what an item requires.
- If you believe an item is wrong, do NOT implement it: reply DISPUTED with the
  evidence that refutes it. A disputed item is escalated, not silently ignored
  and not silently obeyed.
- Re-run the test suite.

OUTPUT (max 20 lines): one line per item — FIXED (with file:line) | DISPUTED
(with evidence) — then test results, then DEVIATIONS introduced by the fixes,
or "none".

DEVIATIONS must be printed on a single line starting with `DEVIATIONS:` —
the pipeline's parser matches the marker only at the start of a line. Example:
  DEVIATIONS: none
  DEVIATIONS: renamed helper to avoid collision with new import
Do not embed the word `DEVIATIONS` in prose on other lines.
