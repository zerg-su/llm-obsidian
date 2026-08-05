---
name: review
description: Harness-owned intent, implementation, or release review with simple, adaptive deep, and explicit full presets. Use for outcome, code, architecture, security, or specs.
---

# Review

Use after self-review, before finalization. The harness owns routing, sessions,
identity, callbacks, and budgets. Reviewers are product read-only; only the
executor resolves findings.

Summary/reap stay locked until approval matches exact HEAD/profile and v4
summary bytes. Only `--no-review` persists a typed bypass.

## Presets

- `review`: one holistic session on the selected model;
- `review --deep`: by default, independent Anthropic and OpenAI holistic
  sessions at
  `xhigh`; with an explicit alias-backed `--runtime` or `--model`, independent
  intent and engineering sessions on that selected model only;
- `review --full`: only when explicitly requested, the four-lane
  `{Anthropic, OpenAI} × {intent, engineering}` grid at `xhigh`;
- `--cross-model`: for the one-route Simple preset, select the opposite
  runtime. Default Deep and explicit Full already use both providers, so the
  flag does not change their topology; explicit runtime/model overrides remain
  authoritative.

Deep/Full use `review_profiles.deep`; model overrides accept only routing-config
aliases. `--deep --full` is invalid. Full is never inferred and rejects a
runtime/model override before launch, recommending single-model Deep.
Public lane IDs use stable `anthropic-*` and `openai-*` prefixes. Concrete
runtime/model values remain separate operation metadata; registered routing
aliases never become lane identity.

Standalone Deep remains the public preset above: default Anthropic+OpenAI
holistic sessions and explicit single-model Deep intent+engineering sessions.
Finalization uses a separate frozen matrix. Its cycles 1–3 select only
`finalization-primary`; cycles 4–5 may add `finalization-independent` only when
policy permits it and fresh typed availability proves it. An explicit
single-model selection always wins. These aliases never reinterpret standalone
Deep.

## Finalization boundary

Reserve every finalization cycle atomically in `FinalizationLedger` before a
review effect. One cycle owns one fresh exact-HEAD attempt and one immutable
terminal result. A changed HEAD closes the old reviewers and requires the next
cycle; it never rearms, resumes, or rebinds their sessions. After a fifth
unsuccessful result the lineage is `finalization-budget-exhausted`; a sixth
cycle creates no model call, session, or ledger entry.

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
inspection and check non-goals for scope creep. Holistic/engineering lanes apply
`docs/skill-references/engineering-quality-contract.md`; repository-specific
standards override its heuristics. Transport, clean diffs, and local green are
not outcome proof. Add
no hidden lane, model call, severity cap, reranking, vote, average, or loop.

For engineering review, read
[`engineering-quality-contract.md`](../../docs/skill-references/engineering-quality-contract.md)
completely. Verify findings against code; rejection requires technical evidence.

## Flow

1. For a dispatched v3/v4 task, run
   `task-review-runner.py run --worktree <worktree>`;
   `plan --worktree <checkout> --plan <plan>` for plans (add exact `--base`
   unless a single-parent HEAD changes that plan); otherwise `current --worktree
   <checkout>` with requested preset/aliases and compatible purpose/boundary.
   The facade starts, resumes, or returns a receipt; `review-runner.py` is low-level.
2. Keep ContextPacket/outbox in owner-only scratch and product read-only. Submit
   axis JSON only through its generated `harness/review_submit.py` command.
3. Keep lanes independent. Material findings persist `awaiting-resolution`.
   Standalone operations retain their preset budget; a finalization finding is
   terminal for its exact HEAD and any corrected HEAD uses a fresh cycle.
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
