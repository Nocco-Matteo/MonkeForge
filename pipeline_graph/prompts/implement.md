ROLE: implementer of BATCH {batch_n} only.

SPEC (implement batch {batch_n} exactly — nothing from other batches, nothing
not in the spec):
<final>
{final}
</final>
Batch scope: {batch_scope}
Checklist items for this batch: {checklist_items}

ARCHITECTURE — READ BEFORE WRITING (not optional; violating these produces
misaligned code the reviewers will reject). Read the project's architecture
index (e.g. CLAUDE.md, AGENTS.md, ARCHITECTURE.md — whichever exist at the
repo root), then the docs relevant to what THIS batch touches (plus any the
brief names) from the project's configured architecture docs:
{arch_docs}
For UI work, also read any repo-root UI guide (e.g. AGENTS.md) that exists and
the guide it points to before using any framework API. Build within the
architecture that already exists; do not invent your own structure.

PRIOR STATE: before writing anything, read what previous batches actually
produced — `git log --oneline` for this branch, the Deviations recorded in the
progress below, and the current source of every module you depend on. Where the
code and the plan disagree, THE CODE IS THE GROUND TRUTH
for signatures, names and interfaces: the plan describes intent, the code
describes reality. If a dependency you need does not exist as the plan
describes it, report the discrepancy instead of silently adapting the spec or
rewriting the dependency.

PLAN_DISCREPANCY: <what and where> — emit a single line whose trimmed text
starts with `PLAN_DISCREPANCY:` ONLY when you hit a genuine contradiction
between the spec and the real code (a dependency the plan names does not
exist, a signature the plan assumes is wrong, a file the plan says to modify
is absent). Describe what contradicts and where. When there is no
contradiction: do NOT emit the marker at all — not even
`PLAN_DISCREPANCY: none` / `n/a` / `no`. Those still match the prefix and
block the run. Proceed with the work and report normally.

<progress>
{progress}
</progress>

TEST INFRASTRUCTURE: {db_note}

IF THIS IS A RETRY, the failing tests from your previous attempt are below — fix them. If empty, this is your first attempt.
FAILING TESTS: {failures}
TEST SUMMARY: {summary}

A <test_summary> block is provided below. Binding rules:
- When authoritative="true", the block is the gate's MEASURED outcome from
  the previous attempt (red = the listed failures are the NEW regressions
  the gate measured). Trust the failure list as ground truth; do NOT re-run
  the suite to "discover" a different set — fix the listed failures.
- When authoritative="false", the block is context-only (status
  unconfigured on a first attempt, or skipped on a retry whose prior
  attempt was not measured — dry run, DB down, or tests waived). Do NOT
  treat it as a pass or a fail; do NOT re-run the suite to "discover" one.
- The suites in the block are the ones the gate already discovered and ran
  (or attempted). Do not invent new suites; do not re-discover.

<test_summary>
{test_summary}
</test_summary>

DO NOT COMMIT. Leave every change UNCOMMITTED in the working tree (staged is
fine). The pipeline commits the batch itself AFTER the review passes. If you run
`git commit`, the reviewer's diff comes back empty and REJECTS your correct work
as "missing" — a wasted cycle. Do not run `git commit`, `git reset`, or
`git checkout`; do not create or switch branches.

ACCEPTANCE CRITERIA (you are done only when all hold):
- Every CONFORMANCE CHECKLIST item for this batch is MET.
- The test suite runs and no new failure is introduced.
- No file outside the batch's declared scope is modified.
- The architecture docs above are followed (folder structure, layer
  responsibilities, anti-patterns, the Next.js guidance) — not just CLAUDE.md.
- Your work is left uncommitted (the pipeline commits after review).

MANDATORY SELF-CHECK before reporting: walk the checklist for this batch and
for each item output `<n>: MET — <file path proving it>` or
`<n>: NOT MET — <why>`. Verify by opening the artifact, not from memory.
Reporting MET without having looked is the worst failure mode here. If an item
cannot be met, stop and report NOT MET with the reason — do NOT silently
descope and do NOT report the batch complete.

FINAL OUTPUT (max 30 lines): files changed; the checklist self-check; test
results (counts + any failure); DEVIATIONS — any difference between what the
plan specified and what you actually built, or "none"; anything you could not
do and why. If and only if you hit a genuine plan/code contradiction, precede
this section with a single `PLAN_DISCREPANCY: <what and where>` line (see the
PRIOR STATE rule above); otherwise emit no such line.

DEVIATIONS must be printed on a single line starting with `DEVIATIONS:` —
the pipeline's parser matches the marker only at the start of a line. Example:
  DEVIATIONS: none
  DEVIATIONS: skipped test X because the DB fixture was missing
Do not embed the word `DEVIATIONS` in prose on other lines.
