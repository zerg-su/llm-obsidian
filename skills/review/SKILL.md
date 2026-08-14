---
name: review
description: Review outcomes, code, architecture, security, or specs through harness-owned Simple, Deep, or Full presets. Use before finalization.
---

# Review

Use after self-review, before finalization. Harness owns routing, sessions,
identity, callbacks, and budgets. Reviewers are product read-only; the executor
resolves findings.

Summary/reap stay locked until approval matches exact HEAD/profile and v4
summary bytes. Only `--no-review` persists a typed bypass.

## Presets

- `review`: one holistic session;
- `review --deep`: independent Anthropic and OpenAI holistic sessions at
  `xhigh` by default; with an alias-backed `--runtime` or `--model`, independent
  intent and engineering sessions on that selected model only;
- `review --full`: the explicit four-lane
  `{Anthropic, OpenAI} × {intent, engineering}` grid at `xhigh`;
- `--cross-model`: Simple selects the opposite runtime. Deep/Full already use
  both providers; explicit overrides remain authoritative.

Deep/Full use `review_profiles.deep`; overrides accept routing aliases only.
Full is explicit, rejects overrides, and cannot combine with `--deep`. Lane IDs
use `anthropic-*`/`openai-*`; concrete routes stay metadata.

Standalone Deep is unchanged. Finalization cycles 1–3 use
`finalization-primary`; cycles 4–5 add `finalization-independent` only after an
accepted third-failure pivot receipt. Explicit single-model always wins.

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

Task evidence must be reviewer-observable at verdict. Put review callback,
reap, release, and terminal cleanup under parent-owned
`Post-review coordinator acceptance`, outside the canonical Outcome Contract.
The missing-evidence policy must not be weakened for a circular task contract;
amend it instead.

## Flow

1. For a dispatched v3/v4 task, run `task-review-runner.py run --worktree
   <worktree>`. Plans use `plan --worktree <checkout> --plan <plan>` (add exact
   `--base` unless one parent changes it); otherwise use `current --worktree
   <checkout>` with compatible preset, purpose, and boundary. The facade
   starts/resumes/returns a receipt; `review-runner.py` is low-level.
2. Keep ContextPacket/outbox in owner-only scratch and product read-only. Submit
   axis JSON only through its generated `harness/review_submit.py` command.
3. Keep lanes independent. Before effect, finalization reserves
   `FinalizationLedger`; each cycle owns one fresh exact-HEAD attempt/result.
   Material `changes-requested`/`approved` consumes a product cycle; mechanism
   outcomes release into a bounded attempt receipt. Changed HEAD advances the
   cycle; cycle 4 needs the pivot, the fifth failure exhausts, and cycle 6 has
   zero effect. Standalone keeps preset budgets.
4. The executor records typed rulings/checks and escalates protected boundaries.
   A plan finding may rebind retained lanes only when the exact Git delta changes
   the design artifact alone; Outcome, dispositions, or evidence-map changes
   require an amendment and fresh boundary.
5. After accepted receipts, terminal approval exits the provider before closing
   only its surface. Archive only exact operation/worktree/HEAD/profile evidence.
6. One explicit changed scope/context boundary permits one compact
   re-evaluation; a second restart or exhausted budget is `attention-required`.

Never edit product, open a second verification surface, rerank, push, publish,
or broaden scope. Use dispatch paths from `.task-meta.json`; current review uses
derived harness state and owner-only scratch.
