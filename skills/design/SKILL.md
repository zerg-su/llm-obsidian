---
name: design
description: Shape read-only architecture, domain boundaries, alternatives, and ADR candidates. Use before ambiguous design work.
---

# Design

Stay read-only. Ask one question per material unknown.

Start with ownership boundaries and owned test seams. Preserve the incoming
Outcome Contract—optional `purpose`, `desired_outcome`, `success_evidence`, and
`non_goals`—without semantic drift.

Produce problem/non-goals, invariants, 2-3 alternatives/tradeoffs,
recommendation/interfaces, data/control flow, recovery,
rollout/rollback, testable acceptance criteria per evidence ID/seam, ADRs.

Separate fact/assumption/decision; distinguish Unresolved fog from explicit
out-of-scope. No placeholders/undefined interfaces; name owners/contracts.

Default to vertical slices; use expand-contract only for wide migration.
Apply YAGNI: choose the smallest design that establishes declared evidence;
leave unrequired features, compatibility, and extension points as non-goals.

Get approval before implementation or interface/migration/dependency/security/
external-effect change.
