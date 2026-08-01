---
name: review
description: Run harness-owned simple/deep review, same-model or cross-model. Use after implementation for code, architecture, security, or spec review.
---

# Review

Use after self-review, before finalization. The harness owns routing, provider
sessions, OperationSpec identity, callbacks, and budgets. The reviewer is
product read-only; only the executor resolves findings.

Completed dispatch tasks enter simple review automatically. Summary/reap stay
locked until durable approval matches the exact HEAD/profile and, for v4, the
reviewed implementer-summary bytes. Only `--no-review` persists a typed bypass.

## Presets

- `review`: one same-model holistic session;
- `review --deep`: independent Fable spec and Sol
  standards/correctness/architecture/security lanes at `xhigh`;
- `--cross-model`: run the selected preset on the opposite runtime.

Deep mode uses `review_profiles.deep`. Model overrides accept only
`config/model-routing.toml` aliases; mismatches fail closed.

## Outcome-first judgment

In every v4 initial/verification round, implementer summaries and reports are
unverified claims. The existing holistic lane, or Fable spec lane in deep mode,
checks the Outcome Contract before mechanics: classify every success-evidence
item `established`, `missing`, or `contradicted` from inspected evidence and
check every non-goal for scope creep. These judgments drive the existing
verdict/findings; callbacks, clean diffs, and local green are not outcome proof.
The standards axis stays independent. Add no lane, surface, model call,
severity cap, reranking, or loop.

## Flow

1. Run the state facade. For a dispatched v4 task:
   `python3 <vault-root>/scripts/task-review-runner.py run --worktree
   <worktree>`. For a current/non-dispatched checkout:
   `python3 <checkout>/scripts/task-review-runner.py current --worktree
   <checkout>`, adding `--deep`, `--cross-model`, or an alias-backed
   `--runtime`/`--model`/`--effort` only when requested, plus `--plan <path>`
   when approved. Do not re-confirm. The idempotent facade starts/drives lanes,
   continues resolved findings on the same session/new HEAD, or returns receipt;
   `review-runner.py` remains a low-level primitive.
2. Keep ContextPacket/outbox in owner-only scratch and product read-only. Each
   lane has one parent session and deterministic one-shot child round; submit
   axis JSON only through its generated `harness/review_submit.py` command.
3. Keep deep axes independent. Material findings persist
   `awaiting-resolution`; a changed HEAD continues the same parent
   checkpoint/surface, once for simple and twice per deep axis. Both deep
   parents verify a shared new HEAD; minor findings do not force a round.
4. The executor records typed applied/rejected/out-of-scope rulings and checks.
   Escalate security, migration, public-interface, permission, dependency,
   external-effect, or scope changes.
5. After accepted receipts, terminal approval exits the provider before closing
   only its surface. Archive only exact operation/worktree/HEAD/profile evidence.
6. One explicit changed scope/context boundary permits one compact
   re-evaluation; a second restart or exhausted budget is `attention-required`.

Never edit product, open a second verification surface, rerank axes, push,
publish, or broaden scope. For dispatch, derive paths from v4 `.task-meta.json`
(`vault_root`, worktree, task UUID, preset/profile, origin surface), never the
generic product root. Current review uses one derived `.vault-meta/harness`
pointer and owner-only scratch outside the checkout.
