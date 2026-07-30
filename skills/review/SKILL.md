---
name: review
description: Run harness-owned simple/deep review, same-model or cross-model. Use after implementation for code, architecture, security, or spec review.
---

# Review

Use after self-review, before finalization. The harness owns routing, provider
sessions, OperationSpec identity, callbacks, and budgets. The reviewer is
product read-only; only the executor resolves findings.

Completed dispatch tasks enter the simple preset automatically. Final summary
and reap remain locked until the gate has durable approval evidence for the
exact HEAD and verification profile. `--no-review` is the only explicit bypass;
it persists a typed skip instead of silently omitting the gate.

## Presets

- `review`: one same-model holistic session.
- `review --deep`: independent Fable spec and Sol
  standards/correctness/architecture/security lanes at `xhigh`.
- `review --cross-model`: simple review on the opposite runtime.
- `review --deep --cross-model`: deep axes on the opposite runtime.

Deep mode uses `review_profiles.deep`. Model overrides accept only
`config/model-routing.toml` aliases; mismatches fail closed.

## Flow

1. Run the state-driven facade. For a dispatched v3 task:
   `python3 <vault-root>/scripts/task-review-runner.py run --worktree
   <worktree>`. For the current checkout, including work that was not
   dispatched:
   `python3 <checkout>/scripts/task-review-runner.py current --worktree
   <checkout>`, adding `--deep`, `--cross-model`, or an alias-backed
   `--runtime`/`--model`/`--effort` override only when requested. Add
   `--plan <path>` when an approved plan exists. Do not ask the user before
   running it. The same idempotent command starts missing review lanes, drives
   exact ready callbacks, continues a resolved finding on the same session at
   the new HEAD, or returns the terminal receipt. `review-runner.py` is a
   low-level harness primitive, not the user-facing ad-hoc workflow.
2. The runner creates owner-only scratch outside the product and starts
   `reviewer-callback`: the ContextPacket and writable outbox remain in
   coordinator-owned scratch while the generic product worktree is read-only.
3. Each lane gets a parent session operation plus a deterministic one-shot
   child round in the same lane. Submit the axis JSON through the exact
   `harness/review_submit.py` command in its generated prompt.
4. Keep deep axes independent. Material findings first persist
   `awaiting-resolution`; only a changed exact HEAD continues the same parent
   checkpoint/surface with a new child receipt: once for simple, twice per deep
   axis. When either deep axis finds a material issue, keep both parents alive
   so both independently verify the shared new HEAD. Minor findings do not
   force another round.
5. The authorized executor records applied/rejected/out-of-scope resolutions
   and checks. Security, migration, public-interface, permission, dependency,
   external-effect, or scope changes require user escalation.
6. Complete each accepted child receipt. Terminal approval requests provider
   exit, waits for process exit, then closes only the exact parent surface.
   Archive only when operation, worktree, HEAD, and profile evidence match.
7. If an explicit scope/context boundary changes the ContextPacket, the
   controller may start one fresh compact re-evaluation. A second restart or an
   exhausted verification budget becomes durable `attention-required`.

Do not edit product files from the reviewer, open a second verification
surface, rerank one deep axis over another, push, publish, or broaden scope.
Do not construct dispatched-task review paths from the generic product root:
v3 `.task-meta.json` binds the coordinator `vault_root`, exact worktree, task
UUID, review preset, verification profile, and origin surface. Current-checkout
review instead persists one derived active pointer under `.vault-meta/harness`
and keeps its ContextPacket, prompts, and callbacks in owner-only scratch
outside the checkout.
