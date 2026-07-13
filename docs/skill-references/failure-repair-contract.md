# Failure-to-repair contract

This contract applies to Claude Code, Codex CLI, interactive coordinators, and
unattended task splits. It turns a repository-owned pipeline defect into a
bounded repair loop without making the user approve routine local maintenance.

## 1. Contain before classification

When a pipeline stage fails, first establish with read-only checks:

- the failed stage and observable error;
- whether the intended mutation never started, rolled back, completed, or is
  left pending behind an existing recovery mechanism;
- whether the failure is reproducible and belongs to a repository-owned
  script, hook, skill, instruction, schema, callback, or runtime adapter;
- whether secrets or untrusted source bodies could appear in diagnostics.

Do not repeatedly retry, broaden permissions, bypass an invariant, or repeat a
possibly completed external effect. A documented idempotent recovery command
may make state safe; it is not blanket permission to change implementation.

## 2. Classify the failure

A **mechanism failure** means a repository-owned pipeline component fails to
deliver its documented behavior, including a reproducible workflow or
instruction defect.

The following are not mechanism failures by themselves:

- a validation or optimistic-concurrency check correctly rejects bad/stale
  input;
- a product test correctly exposes a bug in the requested product change;
- missing user input, credentials, dependency approval, or external authority;
- a user cancellation, provider outage, rate limit, or transient network
  failure without evidence of a repository defect.

Handle those through their existing retry, clarification, or escalation path.
If classification is uncertain, do not present a guess as a confirmed defect.

## 3. Coordinator auto-repair boundary

The owning interactive coordinator may repair a mechanism without another user
prompt only when **all** of these statements are true:

1. The broken mechanism is repository-owned and inside the current repository.
2. The defect is local and reproducible (or narrowed by a deterministic test).
3. The repair is narrow, reversible, and inside the already approved task.
4. Unrelated dirty work can be preserved without overlapping edits.
5. The failed operation did not create an ambiguous or repeatable external
   effect: it never started, rolled back, or has a documented idempotent
   recovery boundary.
6. The repair needs no new permission, credential, dependency install/upgrade,
   security-policy change, public-interface change, schema/data migration,
   destructive action, deployment, publication, or other external effect.

If any statement is false or uncertain, ask the user once with the failed
stage, preserved state, exact boundary, and proposed next action. An earlier
approval of the product task does not authorize that expanded boundary.

This is permission to repair the mechanism, not to silently broaden the product
request. The coordinator records the classification in working context and
resolves the background escalation explicitly.

## 4. Execute the repair

For an eligible auto-repair, or after the user approves a boundary-crossing
repair:

1. Treat mechanism repair as a scoped subtask in the same coordinator session.
2. Reproduce or narrow the failure with the smallest safe probe.
3. Fix the narrowest repository-owned layer; preserve unrelated changes.
4. Add or update a regression test for the observed case.
5. Run the focused test, then the relevant contract/integration suite.
6. Re-run the failed stage or an equivalent dry-run.
7. Resume from the last safe boundary without repeating completed external
   effects.
8. Report the cause, changed files, verification, and original-task outcome.

If the user declines a required expansion, do not change the mechanism. Offer
a documented safe workaround only when it remains inside the original scope.

## 5. Background and turn-end failures

An unattended task split contains and diagnoses a probable mechanism failure
read-only, raises coordinator category `mechanism-failure`, and remains paused.
It does not decide that its own repair is safe. The coordinator immediately
classifies the report against section 3, then either repairs/authorizes it or
asks the user once. The task resumes only after an explicit coordinator
resolution.

A Stop hook cannot start an interactive repair after the turn has ended. It
remains fail-closed, preserves dirty/recoverable state, and emits its normal
actionable diagnostic. At the next interactive contact, the coordinator applies
this contract. No hook may silently edit its own implementation.

## 6. Logging boundary

Keep durable telemetry content-free. Do not put prompts, page bodies, source
text, commands, secrets, or raw error output into `pipeline-events.jsonl`.
Human-visible diagnostics should be short and sanitized; detailed local
evidence stays in working context or existing gitignored diagnostic files.
