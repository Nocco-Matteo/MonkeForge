# Mini Binding Standard

A minimal binding standard used by the binding-standard coverage tests. Each
bullet is a named, checkable rule.

## BS-1: trigger phrases are case-insensitive
The intake interviewer must detect binding-standard trigger phrases
case-insensitively (`binding standard:`, `binding:`, `follow ... as the
binding`).

## BS-2: read before COMPLETE
The interviewer must read the standard (preferring a staged `--ref`) before
ending with INTAKE: COMPLETE; a URL alone is not sufficient.

## BS-3: every In row maps to a DoD id
Every `In` row in the `## 3b` coverage table must map to at least one §5
Definition-of-done item by DoD id.

## BS-4: Out/Deferred rows carry a rationale
Every `Out` or `Deferred` row in the `## 3b` table must carry a one-line
rationale; an empty rationale cell is a failure.
