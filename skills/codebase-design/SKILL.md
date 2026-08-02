---
name: codebase-design
description: Design module boundaries. Use for tangled/oversized code, durable interfaces, and test seams.
---

# Codebase Design

Stay read-only until the user approves implementation. Preserve the incoming
Outcome Contract and read
[engineering-quality-contract.md](../../docs/skill-references/engineering-quality-contract.md)
completely.

For every touched area, map one coherent responsibility/change reason to a
small durable caller interface and owned test seam. Name hidden decisions,
dependency direction, state ownership, error contract, and real adapters.
Compare 2–3 shapes when the boundary is material; prefer a deep module with
high leverage and local reasoning.

For domain-heavy work, establish shared domain language; distinguish entities
with identity/lifecycle from immutable value objects; place aggregate invariants
with their owner; use a domain service only when no entity/value owns the
behavior. Add domain errors or events only when the Outcome Contract requires
those semantics.

Use roughly 200 physical lines as a review signal, not a universal limit. A
larger cohesive module may be valid; a small mixed-responsibility module may
not. Reject pass-through splitting: delete a proposed abstraction mentally and
keep it only when callers become meaningfully simpler or variation is real.

For existing tangled code, characterize public behavior first. Extract one
pure policy/validation seam at a time while preserving durable identities,
side effects, and interfaces. After three failed behavior-preserving attempts
at one seam, stop and revise the architecture.

Complete with an approved module map: responsibility, interface, dependencies,
test seam, Outcome Contract evidence, extraction sequence, and any explicit
cohesion exception. Do not implement, migrate, or change a public interface
without approval.
