DEBATE-016 — requirements loop regression fixture

TASK-016: add a CSV export endpoint at GET /api/exports.csv returning the
current user's records as a downloadable CSV with columns id, name, created_at.

BRIEF (inlined — no external docs/ reference):

UI-SURFACE: no

1. Goal — GET /api/exports.csv returns the signed-in user's records as CSV
   with header row id,name,created_at, one record per line, UTF-8, and a
   Content-Disposition: attachment header.
2. Corrections to the request — none.
3. Rules / domain data — records live in the `records` table; the exporter
   must respect the per-user row filter already applied by `list_records`.
4. Codebase anchors — `backend/routes/exports.py` (new), `backend/models/records.py`.
5. Definition of done — observable, one line each:
   - GET /api/exports.csv returns 200 with text/csv — verified by `curl -i localhost:8000/api/exports.csv`.
   - Header row is exactly id,name,created_at — verified by `head -1` of the response.
   - Content-Disposition: attachment; filename="exports.csv" present — verified by `curl -i` header grep.
6. Scope: in / out — in: the route + serializer. out: streaming for >10k rows, auth changes.
7. Manual acceptance — log in, hit the endpoint, open the downloaded file in a spreadsheet.
8. Unverified assumptions — the existing auth middleware sets req.user.

================================================================================
DEBATE TRANSCRIPT (regression fixture for the requirements-loop escalation).
The same REQUIREMENTS-provenanced blocker is raised by the Reviewer in two
consecutive rounds, exercising both the `debate stuck:` path (repeated BLOCKER
across k=2 rounds) and the `debate requirements:` path (a [BLOCKER:REQUIREMENTS]
tag in the last round). Brief inlined above; no external docs/ path referenced.


## Round 1 — Reviewer

The plan adds `backend/routes/exports.py` and wires it into the router. It
serializes records via a hand-rolled CSV builder. The DoD lists three items,
each with a verification method (curl / head / header grep). So far so good.

However, the brief's section 5 (Definition of done) lists verification methods,
but the brief never states what makes a record "exportable" — the brief says
"the signed-in user's records" without defining which records are visible to
the user when `list_records` is called with no filter. The proposer cannot
prove the endpoint returns the *right* rows, only that it returns *some* rows.
This is a requirements gap, not a plan gap: the plan is correct relative to the
brief, but the brief is underspecified.

VERDICT: REJECT
[BLOCKER:REQUIREMENTS] brief section 3 does not define which records are visible to a user — the DoD "returns the signed-in user's records" is unverifiable without it


## Round 1 — Reply

The proposer concedes the brief is underspecified on record visibility but
argues the plan can proceed by reusing `list_records`'s existing filter, which
the brief already anchors. The proposer asks the Reviewer to confirm whether
reusing that filter satisfies the requirement.

VERDICT: APPROVE_WITH_CHANGES


## Round 2 — Reviewer

The Reply does not resolve the requirements gap. Reusing `list_records`'s
filter is a plan-level decision, but the *brief* still does not state the
visibility rule, so the DoD item "returns the signed-in user's records" cannot
be verified — `curl` proves rows come back, not that they are the correct rows.
The same blocker stands, unchanged, across two rounds. This is a requirements
loop, not a plan disagreement: the plan is fine, the brief is the source of
the stuck claim.

VERDICT: REJECT
[BLOCKER:REQUIREMENTS] brief section 3 does not define which records are visible to a user — the DoD "returns the signed-in user's records" is unverifiable without it


## Round 2 — Reply

The proposer cannot fix the brief from inside the plan debate — the brief is
owned by intake. The proposer requests escalation so the human can amend the
brief's section 3 with the visibility rule.

VERDICT: APPROVE_WITH_CHANGES
