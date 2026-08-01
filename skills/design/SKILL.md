---
name: design
description: Shape read-only architecture, domain boundaries, alternatives, and ADR candidates. Use before ambiguous design work.
---

# Design

Stay read-only. Gather the current constraints, users, failure modes, and
authoritative seams. Ask one question at a time only when a material choice
cannot be resolved from available evidence.

Start with ownership boundaries and owned test seams. Preserve the incoming
Outcome Contract without semantic drift: keep its `desired_outcome`,
`success_evidence`, `non_goals`, and optional `purpose` unchanged.

Produce:

- the problem and non-goals;
- invariants and ownership boundaries;
- two or three credible alternatives with operational tradeoffs;
- the recommended design and why it wins here;
- defined interfaces plus data/control flow, failure recovery, rollout, and
  rollback;
- testable acceptance criteria that trace each declared evidence ID to an
  owned seam;
- ADR candidates for decisions that should remain durable.

Separate facts, assumptions, and decisions. Label Unresolved fog that still
needs a decision separately from work explicitly closed as out of scope. Do not
leave placeholders or undefined interfaces; name their owner and observable
contract.

Use vertical slices by default. Use expand-contract only for a wide migration
that cannot be safely delivered as vertical slices. Prefer the smallest
architecture that satisfies the constraints. Stop for approval before
implementation, public-interface change, migration, new dependency, security
boundary, or external effect.
