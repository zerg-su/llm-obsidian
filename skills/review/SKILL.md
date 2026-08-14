---
name: review
description: Review outcomes, code, architecture, security, or specs through harness-owned Simple, Deep, or Full presets. Use before finalization.
---

# Review

Use after self-review, before finalization. The harness owns routing, sessions,
identity, callbacks, and budgets. Reviewers are product read-only; only the
executor resolves findings.

Summary/reap stay locked until approval matches exact HEAD/profile and v4
summary bytes. Only `--no-review` persists a typed bypass.

## Presets

- `review`: one holistic session on the selected model;
- `review --deep`: independent Anthropic and OpenAI holistic sessions at
  `xhigh` by default; with an alias-backed `--runtime` or `--model`, independent
  intent and engineering sessions on that selected model only;
- `review --full`: only when explicitly requested, the four-lane
  `{Anthropic, OpenAI} × {intent, engineering}` grid at `xhigh`;
- `--cross-model`: for the one-route Simple preset, select the opposite
  runtime. Default Deep and explicit Full already use both providers, so
  the flag does not change their topology; explicit overrides stay
  authoritative.

Deep/Full use `review_profiles.deep`; overrides accept routing aliases only.
`--deep --full` is invalid. Full is never inferred and rejects model/runtime
overrides. Lane IDs use `anthropic-*`/`openai-*`; concrete routes stay metadata.

Standalone Deep remains unchanged. Finalization is separate: cycles 1–3 use
only `finalization-primary`; the third material failure freezes a read-only
pivot packet; cycles 4–5 add `finalization-independent` only after its
accepted receipt, without an availability probe. Explicit single-model always wins.

## Purpose boundaries

Keep review inside one explicit purpose:

- `intent`: Outcome Contract, plan/design digests, dispositions and evidence;
- `implementation`: exact product HEAD plus independent verification;
- `release`: integration HEAD, evidence map, deviations and merge drift;
  approval-or-stop, never a hidden late fix loop.

`review-program.py` derives risk/receipts from terminal gate bytes; digest drift
stales receipts. Current purpose review binds `--purpose` and `--boundary-input`.
Plans use `plan --plan <repo-plan>` (never legacy `current --plan`): it selects
`intent`, compiles protected artifacts, and resolves exact OIDs before launch.

## Outcome-first judgment

Implementer summaries/reports are claims. Holistic/intent lanes first classify
each success-evidence item `established`, `missing`, or `contradicted` from
inspection and check non-goals for scope creep. Holistic/engineering lanes read
[`engineering-quality-contract.md`](../../docs/skill-references/engineering-quality-contract.md)
completely and report its whole six-section review denominator, even when a
section is clean; repository-specific standards override its heuristics.
Transport, clean diffs, and local green are not outcome proof. Verify findings
against code; rejection requires technical evidence. Add no hidden lane, model
call, severity cap, reranking, vote, average, or loop.

Task success evidence must be reviewer-observable at the verdict boundary.
Evidence from the review callback, reap, release, or terminal cleanup belongs
under a parent-owned `Post-review coordinator acceptance` gate outside the
canonical Outcome Contract. The strict missing-evidence policy must not be
weakened to compensate for a circular task contract; return that contract for
amendment instead.

## Flow

1. For a dispatched v3/v4 task, run
   `task-review-runner.py run --worktree <worktree>`;
   `plan --worktree <checkout> --plan <plan>` for plans (add exact `--base`
   unless a single-parent HEAD changes that plan); otherwise `current --worktree
   <checkout>` with requested preset/aliases and compatible purpose/boundary.
   The facade starts, resumes, or returns a receipt; `review-runner.py` is low-level.
2. Keep ContextPacket/outbox in owner-only scratch and product read-only. Submit
   axis JSON only through its generated `harness/review_submit.py` command.
3. Keep lanes independent. Before effect, finalization reserves
   `FinalizationLedger`; each cycle owns one fresh exact-HEAD attempt and an
   immutable terminal result. Material `changes-requested`/`approved`
   consumes a product cycle; mechanism outcomes release the slot into a
   bounded attempt receipt. A changed HEAD uses the next cycle; cycle 4
   needs the accepted pivot receipt. A fifth material failure exhausts the lineage; a
   sixth cycle has zero effect. Standalone keeps preset budgets.
4. The executor records typed rulings/checks and escalates protected boundaries.
   A plan finding may rebind retained lanes only when the exact Git delta changes
   the design artifact alone; Outcome, dispositions, or evidence-map changes
   require an amendment and fresh boundary.
5. After accepted receipts, terminal approval exits the provider before closing
   only its surface. Archive only exact operation/worktree/HEAD/profile evidence.
6. One explicit changed scope/context boundary permits one compact
   re-evaluation; a second restart or exhausted budget is `attention-required`.

Never edit product, open a second verification surface, rerank, push, publish,
or broaden scope. Dispatch paths come from `.task-meta.json`, never a generic
root; current review uses derived harness state and external owner-only scratch.
