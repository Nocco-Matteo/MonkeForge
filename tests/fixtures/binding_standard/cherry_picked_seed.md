UI-SURFACE: no

# TASK-bs: cherry-picked seed brief

Binding standard: tests/fixtures/binding_standard/mini_standard.md

## 1. Goal

Cover BS-1 and BS-4 in the intake brief; the remaining bullets are out of
scope for this task.

## 2. Corrections to the request

none

## 3. Rules / domain data

Transcribed from tests/fixtures/binding_standard/mini_standard.md.

## 3b. Binding-standard coverage

| Standard bullet | In/Out/Deferred | DoD id or rationale |
|-----------------|-----------------|---------------------|
| BS-1            | In              | D1                  |
| BS-2            | Out             | not blocking this task |
| BS-3            | Deferred        | deferred to a follow-up task |
| BS-4            | In              | D2                  |

## 4. Codebase anchors

- `pipeline_graph/prompts/intake.md`

## 4b. Architecture docs to follow

none

## 5. Definition of done

| ID | Criterion |
|----|-----------|
| D1 | BS-1 covered — verified by reading intake.md trigger phrases |
| D2 | BS-4 covered — verified by reading the 3b table rationale column |

## 6. Scope: in / out

### In
- BS-1, BS-4

### Out
- BS-2, BS-3

## 7. Manual acceptance

1. Read the 3b table.

## 8. Unverified assumptions

none

# EXPECTED: rows=4; BS-2=Out; BS-3=Deferred
