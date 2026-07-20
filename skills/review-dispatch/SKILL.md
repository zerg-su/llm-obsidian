---
name: review-dispatch
description: >
  Stateful cross-model full/light review for dispatch tasks: typed findings,
  same-session verification, escalation, and safe reviewer close. Triggers:
  review-dispatch, cross-model review, Codex/Claude review, ревью другой моделью.
allowed-tools: Read Write Edit Bash AskUserQuestion
---

# Review Dispatch

Run the independent review gate after implementation/self-review and before
final reap. The reviewer is product/vault read-only; the executor owns edits,
judgment, commits, and escalation.

## Route and mode

- Default to the opposite runtime family and the reviewer role in
  `config/model-routing.toml` (Claude Fable/high, Codex Sol/high). Explicit
  runtime/model/effort overrides remain exact and fail closed if unregistered.
- Use `light` for bounded routine correctness/regression/test/security findings;
  use `full` for the complete gate. Task metadata is authoritative.
- Generic same-model work uses an internal agent. Use `--same-model` only when
  the user explicitly asks for a visible persistent reviewer window.
- One task/model/domain maps to one lane and resumable context. A review approval
  completes a round, not the task. A new task/domain or different model uses a
  distinct lane/surface.

## Start

```bash
python3 <vault-root>/skills/review-dispatch/scripts/spawn_review.py start \
  --worktree <worktree> [--light|--mode full] [--reviewer-runtime claude|codex] \
  [--model <registered-model>] [--effort <level>]
```

For the canonical primary checkout only, explicit coordinator review may add
`--coordinator-review --vault-root <vault-root>`; linked worktrees are rejected.
`spawn` is a legacy alias.

For v3 manual commands, use the printed task-local `--operation-file`; callbacks
use one-shot handoffs. Both bind exact IDs, so parallel lanes cannot overwrite.
Start opens one split
anchored right of the caller and hands cmux only a short supervised command.
Preparation/spawn/send failures mark the exact operation failed; never guess or
silently create another lane.

Security is code-owned. Claude runs interactive `dontAsk` with bounded reads/tests
and only `.review-outbox.json` writable. Codex uses owner-only scratch,
`approval=never`, no product writes, external network, or hooks. The supervisor
validates outbox, baseline, schema, operation, and callback. Never use Claude
print mode, expose cmux, or relax this profile.

Do not interrupt visible reviewer activity. Wait 15 minutes without progress;
by 20 minutes inspect and ask for a concise status rather than cancelling.

## Receive and decide

The trusted supervisor normally validates and receives the callback before the
executor notification. When the message says it was auto-received, do **not**
run `receive` again. Read the named `.task-review.md` or
`.task-review-verify.md` and follow `recommended_action`.

For legacy/in-flight callbacks only, run the exact provided relay command:

```bash
python3 <vault-root>/skills/review-dispatch/scripts/spawn_review.py receive \
  --worktree <worktree> --operation-dir <exact-operation> \
  --relay-file <exact-.review-callback.json>
```

Classify every finding in `.task-review-resolution.md` as `applied`, `rejected`,
or `out-of-scope`, with evidence. Unattended warning/nit findings may be
resolved within approved scope. Blocking, security, scope, migration,
public-interface, permission, dependency, or external-effect changes escalate;
never auto-resolve them.

After the required executor self-review, resolution, tests, and explicit-file
commit (never runtime handoffs; never push), apply the mechanical decision:

```bash
python3 <vault-root>/skills/review-dispatch/scripts/spawn_review.py drive \
  --worktree <worktree> --action-file <exact-task-local-handoff> --apply-action
```

`approve` finishes the reviewer, `resolve` requires a non-empty resolution and
reuses the same reviewer, and `escalate` stops visibly. Respect the configured
verification limit.

## Verify in the same lane

When fixes/rejections require another round:

```bash
python3 <vault-root>/skills/review-dispatch/scripts/spawn_review.py verify \
  --worktree <worktree> --operation-file <exact-task-local-handoff>
```

This must reuse the exact reviewer lane/surface and original mode, snapshot the
resolution, and send a short file-reference prompt. It must not open a second
same-model session. Repeat receive/decision, then do final executor self-review.

## Finish and archive

`drive --apply-action` is normal. Direct `finish` is interactive/diagnostic:

```bash
python3 <vault-root>/skills/review-dispatch/scripts/spawn_review.py finish \
  --worktree <worktree> --operation-file <exact-task-local-handoff>
```

Finish records the resumable lane checkpoint, gracefully exits the agent, and
closes only the exact surface after process return. Missing/corrupt checkpoint
is reported; later work starts fresh with a full packet rather than guessing.
Task worktrees emit an archive request; coordinator reap performs the authorized
`vault-write.py` archive transaction. Primary coordinator review may archive
directly. Preserve task request, typed rounds, resolutions, gaps, residual
risks, route/mode, and verdict—never raw prompts, command logs, sockets, or IDs.

## Invariants

- Reviewer never edits product/vault files, commits, tickets, or external state.
- Baseline drift outside handoff files blocks callback.
- One stable review ID contains distinct initial/verification run IDs.
- Successful review leaves accepted product changes committed before finish/
  reap-send unless there are no changes.
- Final summary records `Cross-model review: <state>`.
- Surface close follows agent process exit; a failed close/registry transition
  remains visible and retryable for that exact operation.

For legacy v1/v2 metadata, relay compatibility, archive recovery, or detailed
security diagnostics, load [compatibility.md](references/compatibility.md). <!-- context:conditional -->
