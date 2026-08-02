---
name: review
description: Harness-owned intent, implementation, or release review with simple/deep and same/cross-model presets. Use for outcome, code, architecture, security, or specs.
---

# Review

Use after self-review, before finalization. The harness owns routing, sessions,
identity, callbacks, and budgets. Reviewers are product read-only; only the
executor resolves findings.

Dispatch completion enters simple review. Summary/reap stay locked until
approval matches exact HEAD/profile and v4 summary bytes. Only `--no-review`
persists a typed bypass.

## Presets

- `review`: one same-model holistic session;
- `review --deep`: independent Fable spec and Sol
  standards/correctness/architecture/security lanes at `xhigh`;
- `--cross-model`: run the selected preset on the opposite runtime.

Deep mode uses `review_profiles.deep`. Model overrides accept only
`config/model-routing.toml` aliases; mismatches fail closed.

## Purpose boundaries

For approved multi-stage work, keep simple/deep inside one explicit purpose:

- `intent`: Outcome Contract, plan/design digests, dispositions and evidence;
- `implementation`: exact product HEAD plus independent verification;
- `release`: integration HEAD, evidence map, deviations and merge drift;
  approval-or-stop, never a hidden late fix loop.

`review-program.py` derives risk from plan metadata and receipts from terminal gate bytes.
Small reversible work collapses intent; standard uses intent+implementation;
architecture, migration, release and skill-integration require all three.
Receipts are additive; digest drift stales them. Evidence paths are repo-relative; the runner verifies and packets exact bytes.

For current review add `--purpose <intent|implementation|release>
--boundary-input <json>` to `task-review-runner.py current`. ContextPacket,
identity, question and budget bind the input. No flags preserves legacy
implementation-review behavior for compiled tasks.

## Outcome-first judgment

In every v4 initial/verification round, implementer summaries and reports are
unverified claims. The existing holistic lane, or Fable spec lane in deep mode,
checks the Outcome Contract before mechanics: classify every success-evidence
item `established`, `missing`, or `contradicted` from inspected evidence and
check every non-goal for scope creep. These judgments drive the existing
verdict/findings; callbacks, clean diffs, and local green are not outcome proof.
The standards axis stays independent. Add no lane, surface, model call,
severity cap, reranking, or loop.

For standards/correctness/architecture review, read
[`engineering-quality-contract.md`](../../docs/skill-references/engineering-quality-contract.md)
completely. Repository-specific standards override its heuristics, but their
absence never suppresses maintainability or test-quality judgment. Verify each
finding against code reality; reasoned rejection requires technical evidence,
not deference or wording preference.

## Flow

1. Run the state facade. For a dispatched v3/v4 task:
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
publish, or broaden scope. For dispatch, derive paths from `.task-meta.json`
(`vault_root`, worktree, task UUID, preset/profile, origin surface), never the
generic product root. Current review uses one derived `.vault-meta/harness`
pointer and owner-only scratch outside the checkout.
