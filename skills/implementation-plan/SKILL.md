---
name: implementation-plan
description: Plan multi-file work as owned TDD slices. Use for file responsibility, interfaces, and evidence.
---

# Implementation Plan

Stay read-only. Consume the approved Outcome Contract and design; unresolved
architecture returns to `design` or `codebase-design`. Read
[engineering-quality-contract.md](../../docs/skill-references/engineering-quality-contract.md)
when module shape or extraction is involved.

Write vertical, independently reviewable slices. Every slice must name:

- `files/responsibility`: exact owned files or module and one change reason;
- `consumes`: approved interface, state, or evidence available at entry;
- `produces`: observable behavior, interface, artifact, or durable state;
- failing evidence: the test/repro that is red for the intended reason;
- minimal green: the smallest production change that can satisfy it;
- refactor seam: cleanup allowed only while green;
- focused verification and the exact Outcome Contract outcome evidence covered.

Order slices by dependency and keep slow/external adapters behind late bounded
checks. Include rollback/recovery only where the change has persistent or
external effects. Mark parallel slices only when their files, interfaces, and
produced evidence do not overlap.

Apply YAGNI: omit every unrequired feature, compatibility layer, extension
point, and abstraction that lacks current Outcome Contract evidence. Record a
non-goal instead of implementing speculative scope.

Self-review the whole plan for every uncovered requirement, contradictory
interface, placeholder, circular dependency, missing evidence ID, and task that
concentrates unrelated responsibilities. Obtain approval before code. Complete
only when every requirement/evidence item maps to at least one slice and every
slice has a clear owner and runnable verification.
