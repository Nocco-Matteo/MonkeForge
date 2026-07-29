ROLE: fix re-render REGRESSIONS in TASK-{task_id}. The render gate profiled a
scripted interaction and found subtrees that now re-render MORE than the baseline.
Your job is to bring those counts back down without changing behaviour.

THE REGRESSIONS (measured, `baseline -> current` update-count per subtree):
{render_delta}

METHOD:
1. For each `REGRESSION` line, open that component and find WHY the interaction
   re-renders it now when it did not before. The usual causes, in order:
   - state lifted too high → a `setState` re-renders a subtree that does not
     consume that state. Fix: move the state DOWN to the component that uses it
     (the "pass-through parents" anti-pattern in frontend/ARCHITECTURE.md).
   - a broad context whose value changes on the interaction → every consumer
     re-renders. Fix: split the context, or narrow what the provider's value
     depends on (memoize it).
   - a new unstable prop/callback/object identity passed across a memo boundary,
     defeating a `React.memo`. Fix: stabilise it (`useCallback`/`useMemo`) ONLY
     where it actually gates a memo — not as blanket noise.
2. Do NOT change what the component renders or does — this is behaviour-preserving.
   The test gate (typecheck + tests) must stay green; a functional regression is
   a worse failure than the re-render one you are fixing.
3. Do NOT touch the `<Profiler>` instrumentation or the interaction — they are the
   measurement, not the target. Do NOT "fix" the number by removing a Profiler.

ARCHITECTURE: follow frontend/ARCHITECTURE.md (state colocation, no pass-through
parents, no god components). Read it before moving state around.

Leave your changes UNCOMMITTED (staged is fine) — the pipeline re-profiles and
commits. Do NOT run `git commit`.

OUTPUT (max 15 lines): per regression, the component and the one-line cause +
fix you applied; then the test result (counts) and any behaviour risk, or "none".
