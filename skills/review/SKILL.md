---
name: review
description: Review outcomes, code, architecture, security, or specs through harness-owned Simple, Deep, or Full presets. Use before finalization.
---

# Review

Use after self-review, before finalization. Harness owns routing, sessions,
identity, callbacks, and budgets. Reviewers are product read-only; the executor
resolves findings.

Summary/reap unlock only when approval matches exact HEAD/profile and v4 summary
bytes. Only `--no-review` persists a typed bypass.

## Presets

- `review`: one holistic session on the selected model;
- `review --deep`: independent Anthropic/OpenAI holistic sessions at `xhigh` by
  default; an alias-backed `--runtime` or `--model` instead selects independent
  intent/engineering sessions on that model only;
- `review --full`: only when explicit, the four-lane
  `{Anthropic, OpenAI} × {intent, engineering}` grid at `xhigh`;
- `--cross-model`: Simple selects the opposite runtime. Deep/Full already use
  both providers; explicit overrides remain authoritative.

Deep/Full use `review_profiles.deep`; overrides require routing aliases. Full
is never inferred, rejects overrides, and cannot combine with `--deep`. Lane
IDs use `anthropic-*`/`openai-*`; concrete routes stay metadata.

One review program owns one exact review workspace. Its first reviewer creates
the workspace; every later Simple/Deep/Full lane splits from the first exact
surface and must prove the same workspace/window UUID before provider start.
Review finalization and structural pivot stay there. Executor fixes,
verification, recovery, and other non-review continuations stay in the primary
task workspace. A later review cycle creates a fresh review workspace.

Standalone Deep is unchanged. Finalization cycles 1–3 use only
`finalization-primary`; the third material failure freezes a read-only pivot packet;
cycles 4–5 add `finalization-independent` only after its accepted receipt,
without an availability probe. Explicit single-model always wins.

## Purpose boundaries

Use one purpose:

- `intent`: Outcome Contract, plan/design digests, dispositions and evidence;
- `implementation`: exact product HEAD plus independent verification;
- `release`: integration HEAD, evidence map, deviations and merge drift;
  approval-or-stop, never a hidden late fix loop.

`review-program.py` binds risk/receipts to terminal gate bytes; digest drift
stales them. Purpose review binds `--purpose` and `--boundary-input`. Plans use
`plan --plan <repo-plan>` (never legacy `current --plan`) to select `intent`,
compile protected artifacts, and resolve exact OIDs before launch.

## Outcome-first judgment

Implementer summaries/reports are claims. Holistic/intent lanes classify each
success-evidence item `established`, `missing`, or `contradicted` by inspection
and check non-goals for scope creep. Holistic/engineering lanes read
[`engineering-quality-contract.md`](../../docs/skill-references/engineering-quality-contract.md)
completely and report its whole six-section review denominator, even when a
section is clean; repository-specific standards override its heuristics.
Transport, clean diffs, and local green are not outcome proof. Verify findings
against code; rejection requires technical evidence. Add no hidden lane, model
call, severity cap, reranking, vote, average, or loop.

Keep task evidence reviewer-observable at verdict;
[implementation-plan](../implementation-plan/SKILL.md) puts later evidence in
parent-owned `Post-review coordinator acceptance` outside its Outcome Contract.
The strict missing-evidence policy must not be weakened for a circular task
contract; return it for amendment.

## Flow

1. Dispatched v3/v4 tasks use `task-review-runner.py run --worktree <worktree>`.
   Plans use `plan --worktree <checkout> --plan <plan>` (add exact `--base`
   unless one parent changes it); otherwise use `current --worktree <checkout>`
   with compatible preset, purpose, and boundary. Facade starts/resumes/
   returns a receipt; `review-runner.py` is low-level.
2. Keep ContextPacket/outbox owner-only and product read-only. Submit
   axis JSON only through its generated `harness/review_submit.py` command.
3. Keep lanes independent. Before effect, `FinalizationLedger` reserves each
   fresh exact-HEAD attempt and immutable terminal result. Material
   `changes-requested`/`approved` consumes a product cycle; mechanism outcomes
   release the slot into a bounded receipt. Changed HEAD advances the
   cycle; cycle 4 needs the accepted pivot receipt. A fifth material failure
   exhausts the lineage; a sixth cycle has zero effect. Standalone keeps preset
   budgets.
4. The executor records typed rulings/checks and escalates protected boundaries.
   A plan finding may rebind retained lanes only when the exact Git delta changes
   the design artifact alone; Outcome, dispositions, or evidence-map changes
   require an amendment and fresh boundary.
5. Lane cleanup exits each provider and closes only its exact surface. After
   every required callback and finalization is accepted and all lanes are
   resource-free, close and verify the program's exact workspace once. Retain
   it on blocked, attention, incomplete-cleanup, or unprovable identity paths.
   The third material failure retains it through structural pivot; close it
   after the pivot is accepted and before cycle 4 opens a fresh workspace.
   Archive only exact operation/worktree/HEAD/profile evidence.
6. One explicit changed scope/context boundary permits one compact
   re-evaluation; a second restart or exhausted budget is `attention-required`.

Never edit product, open a second verification surface, rerank, push, publish,
or broaden scope. Dispatch paths come from `.task-meta.json`, never a generic
root; current review uses derived harness state and external owner-only scratch.
