ROLE: proposer. Produce an implementation plan. Do not write code.

TASK, in one line: {request}

AUTHORITATIVE STATEMENT OF THE TASK: {brief_path}
Read it first and in full. It is the contract — where it and the line above
disagree, the brief wins. If the brief names a file or symbol that does not
exist, report the discrepancy rather than adapting silently.

METHOD (do this before writing):
1. Read the code you intend to change. Every file path, symbol name and API you
   mention must be one you have actually seen in this repo.
2. Read the ARCHITECTURE the plan must respect, and design within it — do not
   invent structure. CLAUDE.md and its index, then the docs relevant to what you
   touch (plus any the brief names), from the project's architecture docs:
   {arch_docs}
3. If the work touches user-facing UI, read docs/UX-MANIFESTO.md and design the
   flows to satisfy it — it is a hard constraint, not advice.

OUTPUT -> print the plan to stdout (the pipeline files it automatically), with these sections exactly:
  1. Goal (max 3 lines)
  2. Constraints (from CLAUDE.md + the request; each one testable)
  3. Architecture decisions (decision, alternative rejected, why — max 3 lines each)
  4. Batches — if this cannot ship in one pass, split into numbered sequential
     batches; each batch must be independently reviewable and leave the repo
     green. If one batch suffices, say "single batch" and explain why.
     No batch may depend on an artifact produced by a later batch.
  5. File-by-file changes (path -> what changes; new files marked NEW)
  6. Edge cases considered (and how each is handled)
  7. Test strategy (what proves this works)
  8. Out of scope (explicit non-goals)

ACCEPTANCE CRITERIA for this plan:
- Every path/symbol referenced exists in the repo, or is marked NEW.
- No step says "refactor as needed", "etc." or "and so on" — enumerate.
- Anything you could not verify goes under "Unverified assumptions".
- Max 400 lines. Do not restate the request back to me.
