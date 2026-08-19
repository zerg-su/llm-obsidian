---
name: review
description: Review outcomes, code, architecture, security, or specs through harness-owned Simple, Deep, or Full presets. Use before finalization.
---

# Review

Use before finalization. Harness owns routing, identity, callbacks, sessions,
and budgets. Reviewers are product read-only; the executor resolves findings.
Summary/reap require exact HEAD/profile and v4 bytes; only `--no-review`
persists a typed bypass.

## Presets

- `review`: one selected-model holistic session;
- `review --deep`: independent Anthropic/OpenAI holistic sessions at `xhigh`,
  or alias-backed intent/engineering sessions for an explicit model;
- explicit `review --full`: `{Anthropic, OpenAI} × {intent, engineering}` at
  `xhigh`;
- `--cross-model`: Simple selects the opposite runtime. Deep/Full already use
  both; explicit overrides win.

Deep/Full use `review_profiles.deep`; overrides require routing aliases. Full
is never inferred or combined with `--deep`, and rejects overrides. Lane IDs
use `anthropic-*`/`openai-*`; concrete routes stay metadata.

One program owns one exact review workspace: its first reviewer creates it;
later lanes split there and prove its workspace/window UUID before provider
start. Finalization and pivot stay there; non-review work stays in the primary
task workspace. Each later cycle gets a fresh workspace.

Cycles 1–3 use only `finalization-primary`; the third material failure freezes
a read-only pivot packet. Cycles 4–5 add `finalization-independent` only after
its accepted receipt, without an availability probe. Explicit single-model
wins; standalone Deep is unchanged.

## Purpose boundaries

Use one purpose:

- `intent`: Outcome Contract, plan/design digests, dispositions and evidence;
- `implementation`: exact product HEAD plus independent verification;
- `release`: integration HEAD, evidence map, deviations and merge drift;
  approval-or-stop, never a hidden late fix loop.

`review-program.py` binds receipts to terminal bytes; drift stales them.
Purpose review binds `--purpose` and `--boundary-input`. Plans use `plan
--plan <repo-plan>` (never `current --plan`) for `intent`, protected artifacts,
and exact pre-launch OIDs.

## Outcome-first judgment

Implementer reports are claims. Holistic/intent lanes classify each evidence
item `established`, `missing`, or `contradicted` and check scope creep.
Holistic/engineering lanes read
[`engineering-quality-contract.md`](../../docs/skill-references/engineering-quality-contract.md)
completely and report its whole six-section review denominator, even when a
section is clean; repository-specific standards override its heuristics.
Verify code: transport, clean diffs, and local green are not outcome proof.
Rejection needs technical evidence. Add no hidden lane, model call, severity
cap, reranking, vote, average, or loop.

Keep evidence reviewer-observable at verdict. [Implementation planning](../implementation-plan/SKILL.md)
puts later evidence in parent-owned `Post-review coordinator acceptance`
outside its Outcome Contract. The missing-evidence policy must not be weakened
for a circular task contract; return it for amendment.

## Flow

1. Dispatched v3/v4 tasks use `task-review-runner.py run --worktree <worktree>`.
   Plans use `plan --worktree <checkout> --plan <plan>` (plus exact `--base`
   unless one parent changes it); otherwise use `current --worktree <checkout>`.
   The facade starts/resumes/returns receipts; `review-runner.py` is low-level.
2. Keep ContextPacket/outbox owner-only and product read-only. Submit
   axis JSON only through its generated `harness/review_submit.py` command.
3. Keep lanes independent. Before effect, `FinalizationLedger` reserves each
   fresh exact-HEAD attempt and terminal result. Material `changes-requested` or
   `approved` consumes a cycle; mechanism outcomes release it via bounded
   receipt. Changed HEAD advances the cycle; cycle 4 needs the pivot receipt.
   A fifth material failure exhausts lineage; the sixth cycle has zero effect.
4. The executor records typed rulings/checks and escalates protected boundaries.
   A plan finding may rebind retained lanes only when the Git delta changes the
   design artifact alone; Outcome/disposition/evidence changes require an
   amendment and fresh boundary.
5. Lane cleanup exits each provider and closes only its exact surface. After
   accepted callbacks and resource-free lanes, close the exact workspace once.
   Retain it on blocked, attention, incomplete cleanup, or unprovable identity.
   Failure 3 retains it through pivot; close
   after accepted pivot and before cycle 4. Archive exact identity evidence.
6. One explicit changed scope/context boundary permits one compact
   re-evaluation; another restart or exhausted budget is `attention-required`.

Never edit product, open a second verification surface, push, publish, or
broaden scope. Dispatch paths come from `.task-meta.json`; current review
uses derived harness state and external owner-only scratch.
