---
name: review
description: Use for outcome, code, architecture, security, or spec review through advisory Light or harness-owned Simple, Deep, and Full modes.
allowed-tools: Read Glob Grep Bash Agent
---

# Review

Use after self-review and before finalization. Reviewers are product read-only;
the executor resolves findings. Harness owns Lifecycle routing, identity,
callbacks, budgets, and archive authority. Only `--no-review` persists a typed
bypass.

Choose one mode and read its reference completely before acting:

- `review --light`: one same-runtime native subagent, advisory only. Read
  [light-review.md](references/light-review.md). <!-- context:conditional -->
- `review`, `review --deep`, or explicit `review --full`: approval-capable
  Lifecycle Review. Read [lifecycle-review.md](references/lifecycle-review.md). <!-- context:conditional -->

Never silently substitute the other mode.

Light uses `native-subagent`; `TOOL_ROOT` is never an automatic coordinator vault.
Validate through `light_review_contract.py`, allowing one correction in
the same child thread only.

## Outcome-first judgment

Implementer reports are claims, not proof. Holistic/intent lanes classify every
success-evidence item `established`, `missing`, or `contradicted` by inspection
and check non-goals for scope creep. Holistic/engineering lanes must read
[engineering-quality-contract.md](../../docs/skill-references/engineering-quality-contract.md)
completely and report its whole six-section review denominator, even when a
section is clean; repository-specific standards override its heuristics.

Transport, clean diffs, and local green are not outcome proof. Verify findings
against code. Add no hidden lane, model call, severity cap, reranking, vote,
average, or loop.

Keep task evidence reviewer-observable at verdict. The
[implementation-plan](../implementation-plan/SKILL.md) keeps later evidence in
parent-owned `Post-review coordinator acceptance` outside its Outcome Contract.
The strict missing-evidence policy must not be weakened for a circular task
contract; return that contract for amendment.

## Lifecycle essentials

Summary/reap unlock only when approval matches exact HEAD/profile and v4 summary
bytes. Purpose-bound checkpoints are:

- `intent`: Outcome Contract, design, dispositions, and evidence;
- `implementation`: exact product HEAD plus independent verification;
- `release`: integration HEAD, evidence map, deviations, and merge drift;
  approval-or-stop, never a hidden late fix loop.

`review-program.py` binds terminal evidence. Pass `--purpose` and
`--boundary-input`; plan review uses `plan --plan <repo-plan>`.

Dispatched v3/v4 tasks use `task-review-runner.py run --worktree <worktree>`.
External/current review uses `current --target <checkout> --vault-root
<coordinator> --plan <approved-plan>`. A first approval-capable current review
requires an explicit plan whose Outcome Contract includes behavior-bound
success evidence, expressed as paired `evidence_kind: behavior` and a bounded
`subject`; generic or unbound scope/correctness evidence fails before ownership
or provider effects. Generated callbacks reuse the stored plan and omit `--plan`.
For a multi-commit task, pass explicit `--base <ref>`; omitted base
intentionally reviews only HEAD. It requires a clean target lease and keeps
Harness state and resolution input owner-only. Never stash, commit, reset,
create a worktree, push, publish, or edit the product on the reviewer's behalf.

The executor records typed rulings and checks. Material findings consume the
bounded product-cycle lineage; mechanism failures do not. After accepted
receipts, terminal approval closes only its exact surface and archives only
exact operation/worktree/HEAD/profile evidence. A second restart, exhausted
budget, or unresolved drift becomes `attention-required`.

Standalone Deep keeps its default dual-provider topology and is not a
finalization route. Finalization cycles 1–3 use
`finalization-primary`; cycles 4–5 add `finalization-independent` only after its
accepted pivot receipt. Explicit single-model always wins. Reserve every fresh
exact-HEAD attempt before provider effect; a fifth material failure exhausts
the lineage and a sixth cycle has zero effect.
