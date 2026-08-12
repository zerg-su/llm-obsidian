---
name: reap
metadata:
  version: 1.4.0
description: Collect and file a completed dispatch task's typed Wiki Summary, archive its review, and close the approved lifecycle.
allowed-tools: Read Write Edit Glob Grep Bash AskUserQuestion
---

# /reap — finalize a dispatched task

Reap validates one task's typed summary/review, files it through the canonical
vault transaction, closes the authorized lifecycle, then arms process exit.
A review round is not task completion; reap follows final readiness.

## Normal v4 unattended path

Use the code-owned runner exactly once:

```bash
python3 scripts/reap-runner.py \
  --worktree <absolute-task-worktree> \
  --vault-root <absolute-coordinator-vault> \
  --current-session "$(./scripts/current-session-id.sh)"
```

New v4 tasks require validated metadata and Wiki Summary v2. Active unattended
v3 tasks use the same runner with their frozen metadata/Summary v1 contract.
Require the exact caller session; never infer focused cmux. The runner:

1. Validate Wiki Summary v2 disposition/evidence/gaps and handoff
   (`task_contract.py check-handoff`), plan/outcome hashes and state, route, and
   wikilinks before mutation; never rewrite the desired outcome.
2. Require approved review evidence for exact HEAD/profile/summary bytes, then
   archive all rounds, including failed cycles. Parsing attaches validated
   review links; never invent or duplicate them.
3. Call `cmux_surface_lifecycle.py prepare-reap`, binding the exact result path,
   plan, task, session, and recovery marker.
4. Apply one `vault-write.py` transaction: `final` closes its source plan only
   when the dispatch-time source digest still matches; `shared` retains the
   master plan. A concurrent pending source edit is preserved and reported as
   a plan-close conflict while the task result is recorded independently.
   Updates use `expected_sha256`; new pages receive a real DragonScale address.
5. Reindex, run `validate-vault.py --summary`, then call `complete-reap`.
6. Call `request-exit` for the exact task. The lifecycle wrapper sends graceful
   agent exit and closes the surface only after process exit. Do not close the
   cmux surface directly.

The JSON result contains `status`, exact `result_path`, `result_link`, and
`duration_ms`. Show the filed link and completion state. Do not emit a second
vault write, review archive, `/reap`, or close command.

## Safety and recovery boundary

- The coordinator vault is mutated only by `vault-write.py`; never Edit/Write a
  wiki page, log, hot list, plan, or manifest directly.
- The final title/type must match the approved task metadata. Existing content
  is updated only for supported service/repo routes and only optimistically.
- An invalid immutable plan/amendment identity, unresolved wikilink, dirty task
  product state, missing/changed review archive, session mismatch, or ambiguous
  result path fails closed before finalization. A mutable pending source-plan
  hash mismatch is an optimistic close conflict, not authority drift.
- An executed `final` plan requires exact prepared recovery; `shared` rejects a
  closed plan.
- Mechanism failures follow the central repair contract. Do not turn a product
  validation rejection into an auto-repair.
- Push, publish, deploy, task worktree deletion, and branch deletion are never part of reap.

## Interactive/legacy compatibility

For legacy v1/v2, preview, interactive filing, or diagnosis, load [compatibility.md](references/compatibility.md). <!-- context:conditional -->

Legacy mode must preserve the same typed summary, optimistic write, provenance,
review, exact-surface, and user-confirmation boundaries. It must never silently
substitute for an unattended v3 final reap.
