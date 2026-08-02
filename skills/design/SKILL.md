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
When a design authorizes restart, recovery, or another effectful action, bind
that authorization to one immutable decision snapshot/identity and place an
atomic reservation boundary before the effect. Acceptance must cover crash
before reservation, crash after reservation but before the effect, crash after
the effect but before its durable receipt, and prove replay does not duplicate
the effect. Any persisted budget or idempotency fact survives unrelated later
effects; a replaceable last-effect slot is not durable history. Acceptance
also covers interleaving an unrelated effect before the next recovery decision.
When a decision spans multiple durable records or identities, map each record
separately, including resource ownership, durable history, current target,
revision, and generation. Under the owning lock, re-read every participating
record before reservation; never collapse them into one identity or keep
session-wide budgets on a retargetable child.
If an effect consumes mutable effect input outside those records, bind its
immutable identity or digest into the decision and reservation, then revalidate
it immediately before execution. Hand only a newly reserved action to the
executor; a crash-recovered pending reservation is reconciliation-only until
its prior effect outcome is proven.

Get approval before implementation or interface/migration/dependency/security/
external-effect change.
