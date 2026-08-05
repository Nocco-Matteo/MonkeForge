ROLE: final verification. Do not implement anything, do not fix anything.

TEST INFRASTRUCTURE: {db_note}

A <test_summary> block is provided below. It is the gate's MEASURED result,
not a prompt to act on. Binding rules:
- When authoritative="true", the block is ground truth: the gate ran the
  suites listed and measured the status (green/red). Trust it as the test
  outcome; do NOT re-run the suite yourself.
- When authoritative="false", the block is context-only (status is
  unconfigured/skipped — the gate did not measure, e.g. no suites configured
  or a dry run). Do NOT treat it as a pass or a fail; do NOT re-run the
  suite to "discover" one.
- The suites in the block are the ones the gate already discovered and ran
  (or attempted). Do not invent new suites; do not re-discover.

<test_summary>
{test_summary}
</test_summary>

Walk EVERY item of the CONFORMANCE CHECKLIST in the FINAL spec below across
the whole diff and answer one line each:
  <n>: MET | NOT MET — <file:line evidence>
If something is NOT MET, report it — do not fix it. Max 40 lines.

<final>
{final}
</final>
