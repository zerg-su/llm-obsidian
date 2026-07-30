# Unattended harness operations

The public chain is `dispatch` → `review` → `reap`. Skills define triggers,
reasoning discipline, authorization boundaries, and completion criteria.
Deterministic lifecycle mechanics live in `scripts/harness/`.

## Operation lifecycle

Each operation has an immutable typed spec, owner-scoped ledger record, exact
surface/process handles, bounded callback and retry state, verification
evidence, and a compact handoff. `scripts/harness-cli.py` exposes:

```text
status
inspect <operation-id>
resume <operation-id>
reconcile
cancel <operation-id>
close <operation-id>
doctor
```

The cmux adapter is the only production perimeter for open/send/read/status/
close. Runtime drivers pin model, effort, permission profile, cwd, and callback
transport. Unknown prompts, ownership, callbacks, or resource state become
`attention-required`; uncertain effects reconcile before any retry.

## Review and reap

Simple review uses one holistic session. Deep review keeps independent spec and
correctness/architecture/security axes. Reviewer sessions are product
read-only; the executor resolves findings. Verification reuses the exact
reviewer lane and surface within the configured round bound.

The task writes canonical `.task-summary.json`. Its supervisor validates and
delivers that value through the internal callback broker. The coordinator owns
the one `reap-runner.py` vault transaction, review archival, plan close,
validation, task archival, armed exit, and exact terminal cleanup.

## Recovery and upgrade

The operation ledger and write-ahead effect marker are authoritative after an
interruption. Read-only probes may retry within their small budget; model start,
send, callback, or close does not repeat until reconciliation proves the prior
effect.

`scripts/upgrade-preflight.py` rejects every active harness kind plus live
legacy task/review/research state before mutation. Finish or cancel the listed
operations with the installed runtime, then rerun the preflight. Live 2.2.x
state is never silently adopted.
