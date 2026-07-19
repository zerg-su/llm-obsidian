---
name: reap
metadata:
  version: 1.4.0
description: Collect and file a completed dispatch task's typed Wiki Summary, archive its review, and close the approved lifecycle.
allowed-tools: Read Write Edit Glob Grep Bash AskUserQuestion
---

# /reap — finalize a dispatched task

Reap is the coordinator-side inverse of dispatch. It validates one task's typed
summary and approved review history, files the result through the canonical
vault transaction, closes the plan/lifecycle, then arms process exit. A review
round is not task completion; reap happens only after the task declares final
readiness.

## Normal v3 unattended path

Use the code-owned runner exactly once:

```bash
python3 scripts/reap-runner.py \
  --worktree <absolute-task-worktree> \
  --vault-root <absolute-coordinator-vault> \
  --current-session "$(./scripts/current-session-id.sh)"
```

The worktree must contain validated v3 `.task-meta.json` and canonical
`.task-summary.json`. The caller session must be exact; never infer it from
focused cmux state. The runner performs these fail-closed stages:

1. Validate the typed summary and handoff (`task_contract.py check-handoff`),
   approved plan hash/state, result route, and every wikilink before mutation.
2. Archive every review round and require at least one durable approved review.
   Failed cycles remain accounted for. Summary parsing attaches validated
   review links; do not invent or duplicate them.
3. Call `cmux_surface_lifecycle.py prepare-reap`, binding the exact result path,
   plan, task, session, and recovery marker.
4. Create/update the result plus plan close in one `vault-write.py` transaction.
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
- A pending plan hash mismatch, unresolved wikilink, dirty task product state,
  missing/changed review archive, session mismatch, or ambiguous result path
  fails closed before finalization.
- An executed plan is accepted only as exact recovery of a prior prepared
  transaction with matching closed-plan hash and existing result page.
- Mechanism failures follow the central repair contract. Do not turn a product
  validation rejection into an auto-repair.
- Push, publish, deploy, task worktree deletion, and branch deletion are never
  part of reap.

## Interactive/legacy compatibility

For dry-run preview, legacy v1/v2 task metadata, explicit interactive filing, or read-only diagnosis, load [compatibility.md](references/compatibility.md). <!-- context:conditional -->

Legacy mode must preserve the same typed summary, optimistic write, provenance,
review, exact-surface, and user-confirmation boundaries. It must never silently
substitute for an unattended v3 final reap.
