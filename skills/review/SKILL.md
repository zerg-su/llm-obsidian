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

## Purpose boundaries

Keep review inside one explicit purpose:

- `intent`: Outcome Contract, plan/design digests, dispositions and evidence;
- `implementation`: exact product HEAD plus independent verification;
- `release`: integration HEAD, evidence map, deviations and merge drift;
  approval-or-stop, never a hidden late fix loop.

`review-program.py` derives risk and receipts from terminal gate bytes. Receipts
are additive; digest drift stales them. Evidence paths are repo-relative.

Current purpose review adds `--purpose <intent|implementation|release>
--boundary-input <json>`; ContextPacket, identity, question and budget bind it.

## Outcome-first judgment

In every v4 initial/verification round, implementer summaries and reports are
unverified claims. Every holistic or intent lane checks the Outcome Contract
before mechanics: classify every success-evidence item `established`, `missing`,
or `contradicted` from inspected evidence and check every non-goal for scope creep.
Every holistic or engineering lane applies the authoritative
`docs/skill-references/engineering-quality-contract.md`; repository-specific
standards override its heuristics. These judgments drive the existing
verdict/findings; callbacks, clean diffs, and local green are not outcome proof.
Specialist lanes stay within their assigned responsibility. Add no hidden lane,
model call, severity cap, reranking, vote, average, or loop.

For engineering review, read
[`engineering-quality-contract.md`](../../docs/skill-references/engineering-quality-contract.md)
completely. Verify findings against code; rejection requires technical evidence.

## Flow

1. Run the state facade. For a dispatched v3/v4 task:
   `python3 <vault-root>/scripts/task-review-runner.py run --worktree
   <worktree>`. For a current/non-dispatched checkout:
   `python3 <checkout>/scripts/task-review-runner.py current --worktree
   <checkout>`, adding requested `--deep`, explicit `--full`, `--cross-model`,
   alias-backed overrides, and approved `--plan`. The idempotent facade starts,
   resumes, or returns a receipt; `review-runner.py` stays low-level.
2. Keep ContextPacket/outbox in owner-only scratch and product read-only. Each
   lane has one parent session and deterministic one-shot child round; submit
   axis JSON only through its generated `harness/review_submit.py` command.
3. Keep every lane independent. Material findings persist
   `awaiting-resolution`; a changed HEAD continues the same parent
   checkpoint/surface, once for simple and twice per Deep or Full lane. Every
   parent verifies a shared new HEAD; minor findings do not force a round.
4. The executor records typed rulings/checks and escalates protected boundaries.
5. After accepted receipts, terminal approval exits the provider before closing
   only its surface. Archive only exact operation/worktree/HEAD/profile evidence.
6. One explicit changed scope/context boundary permits one compact
   re-evaluation; a second restart or exhausted budget is `attention-required`.

Never edit product, open a second verification surface, rerank, push, publish,
or broaden scope. Dispatch paths come from `.task-meta.json`, never a generic
root; current review uses derived harness state and external owner-only scratch.
