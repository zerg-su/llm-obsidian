# Failure-to-repair contract

This contract applies to Claude Code, Codex CLI, interactive skills, and
unattended task splits. It turns a repository-owned pipeline defect into an
explicit, user-approved repair loop without weakening fail-closed behavior.

## 1. Contain before asking

When a pipeline stage fails, first establish with read-only checks:

- the failed stage and observable error;
- whether the intended mutation never started, rolled back, completed, or is
  left pending behind an existing recovery mechanism;
- whether secrets or untrusted source bodies could appear in diagnostics.

Do not repeatedly retry, edit the mechanism, broaden permissions, or bypass an
invariant before consent. A documented idempotent recovery command may be used
only to make state safe; it is not permission to change implementation code.

## 2. Classify the failure

A **mechanism failure** means a repository-owned script, hook, skill,
instruction, schema, callback, or runtime adapter fails to deliver its
documented behavior, including a reproducible workflow/instruction defect.

The following are not mechanism failures by themselves:

- a validation or optimistic-concurrency check correctly rejects bad/stale
  input;
- a product test correctly exposes a bug in the user's requested change;
- missing user input, credentials, dependency approval, or external authority;
- a user cancellation, provider outage, rate limit, or transient network
  failure without evidence of a repository defect.

Handle those through their existing retry, clarification, or escalation path.
If classification is uncertain, say so; do not present a guess as a confirmed
pipeline defect.

## 3. Ask once, concretely

For a probable mechanism failure, pause the original workflow and ask:

> Пайплайн остановился на `<stage>`: `<short observable>`. Исходная операция:
> `<not started | rolled back | completed | pending recovery>`. Похоже на
> дефект `<mechanism>`. Хотите, чтобы я сейчас диагностировал и починил сам
> механизм, проверил исправление и затем продолжил исходную задачу?

The question must make clear that repair changes pipeline implementation and
is separate from merely completing the original task. Before an explicit yes,
only containment and read-only diagnosis are authorized.

## 4. Repair after consent

After the user agrees:

1. Treat mechanism repair as an approved subtask in the same chat.
2. Reproduce or narrow the failure with the smallest safe probe.
3. Fix the narrowest repository-owned layer that caused it; preserve unrelated
   worktree changes.
4. Add or update a regression test that fails for the observed case.
5. Run the focused test, then the relevant contract/integration suite.
6. Re-run the failed stage or an equivalent dry-run.
7. Resume the original workflow from the last safe boundary without repeating
   already committed external effects.
8. Report the cause, changed files, verification, and original-task outcome.

If repair requires a new external effect, broader scope, credentials, security
trade-off, destructive action, or public-interface change, ask separately;
the first consent does not authorize it.

If the user declines, do not change the mechanism. Offer a documented safe
workaround only when it stays inside the original scope, otherwise stop with
the preserved-state summary and recovery command.

## 5. Background and turn-end failures

An unattended task split must raise a coordinator escalation with category
`mechanism-failure`, include the stage/state classification and the repair
question, then remain paused. It must not ask inside the background pane or
self-repair.

A Stop hook cannot start an interactive repair after the agent turn has ended.
It remains fail-closed, preserves dirty/recoverable state, and emits its normal
actionable diagnostic. At the next interactive contact, the coordinator applies
this contract before changing the failed mechanism. No hook may silently edit
its own implementation.

## 6. Logging boundary

Keep durable telemetry content-free. Do not put prompts, page bodies, source
text, commands, secrets, or raw error output into `pipeline-events.jsonl`.
Human-visible chat diagnostics should be short and sanitized; detailed local
evidence stays in the working context or existing gitignored diagnostic files.
