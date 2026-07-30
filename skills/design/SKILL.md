---
name: design
description: Shape read-only architecture, domain boundaries, alternatives, and ADR candidates. Use before ambiguous design work.
---

# Design

Stay read-only. Gather the current constraints, users, failure modes, and
authoritative seams. Ask one question at a time only when a material choice
cannot be resolved from available evidence.

Produce:

- the problem and non-goals;
- invariants and ownership boundaries;
- two or three credible alternatives with operational tradeoffs;
- the recommended design and why it wins here;
- data/control flow, failure recovery, rollout, and rollback;
- testable acceptance criteria;
- ADR candidates for decisions that should remain durable.

Separate facts, assumptions, and decisions. Prefer the smallest architecture
that satisfies the constraints. Stop for approval before implementation,
public-interface change, migration, new dependency, security boundary, or
external effect.
