---
name: implementation-plan
description: Plan multi-file work as owned TDD slices. Use for file responsibility, interfaces, and evidence.
---

# Implementation Plan

Stay read-only. Consume the approved Outcome Contract and design; unresolved
architecture returns to `design` or `codebase-design`. Read
[engineering-quality-contract.md](../../docs/skill-references/engineering-quality-contract.md)
when module shape or extraction is involved.

## Input and upstream authority gate

Valid input is ONE bounded delivery outcome. The carrier may be one accepted
Work Item, or the existing approved Outcome Contract + design path. When the
request contains multiple independent outcomes, stop and return to `decompose`;
do not hide them in one oversized implementation plan.

For a Work Item input, read
[the architecture artifact contract](../../docs/skill-references/architecture-artifacts.md)
completely before planning. Read the durable Work Item and every consumed
upstream page, then validate all of these before any file/TDD planning:

- the Work Item itself is accepted;
- project artifacts are accepted; decisions are active or accepted;
- a superseded artifact or decision is never authoritative;
- `upstream` and `upstream_pins` form a total well-formed pin mapping;
- derived freshness is current, never needs-review or stale;
- no recovery journal affects the project and the Work Graph/WI projection is
  neither partial nor inconsistent.

Malformed authority, unresolved architecture/spec/contract concerns, or any
failed check emits the canonical Upstream Gap and stops planning:

- source artifact/decision;
- why downstream work cannot proceed;
- affected downstream artifacts/work;
- required owner/action.

Implementation-plan never resolves the gap. Address the gap to the owning
`architecture`, `design`, or other upstream carrier; do not re-route the whole
planning request or answer as it. `implementation-plan` remains the response
carrier: a failed gate does not transfer the user's planning request; emit the
gap and stop file/TDD planning. The legacy Outcome Contract + design path below
remains unchanged; this branch adds no mandatory decompose step.

Write normative plan prose in English unless the user explicitly requests
another language. This includes the Outcome Contract, goal, evidence
identifiers, slice definitions, stop rules, and verification instructions.
Keep user-facing conversation in the user's language. Return a non-English
approved Outcome Contract for amendment instead of silently translating its
evidence identity.

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

Every task success-evidence item must be reviewer-observable before the
configured review verdict. Evidence created later by the review callback,
reap, release promotion, or terminal cleanup belongs in a separately named
`Post-review coordinator acceptance` section owned by the parent coordinator,
outside the canonical Outcome Contract. For example, put “focused regression
is green” in task success evidence, but put “review callback accepted and reap
left empty resources” in that post-review gate.

Apply YAGNI: omit every unrequired feature, compatibility layer, extension
point, and abstraction that lacks current Outcome Contract evidence. Record a
non-goal instead of implementing speculative scope.

For a stateful finalization slice, name the durable `FinalizationLedger`, the
lineage key, and the exact HEAD owned by each terminal attempt. Plan atomic
reservation for cycles 1–5 and prove that a sixth reservation has zero model,
session, and ledger effect. Keep the optional PipelineSpec v1
`finalization_policy` additive, with aliases and ceilings validated before any
effect. Treat standalone `review --deep` as a non-goal: its default
dual-provider topology is not the finalization route matrix.

Self-review the whole plan for every uncovered requirement, contradictory
interface, placeholder, circular dependency, missing evidence ID, and task that
concentrates unrelated responsibilities. Obtain approval before code. Complete
only when every requirement/evidence item maps to at least one slice and every
slice has a clear owner and runnable verification.
