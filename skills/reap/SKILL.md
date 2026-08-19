---
name: reap
metadata:
  version: 1.4.0
description: Collect and file a completed dispatch task's typed Wiki Summary, archive its review, and close the approved lifecycle.
allowed-tools: Read Write Edit Glob Grep Bash AskUserQuestion
---

# /reap — finalize a dispatched task

Reap validates one task's summary/review, files it through the canonical vault
transaction, closes its lifecycle, then arms exit. Review is not completion;
reap follows final readiness.

## Normal v4 unattended path

Use the code-owned runner exactly once:

```bash
python3 scripts/reap-runner.py \
  --worktree <absolute-task-worktree> \
  --vault-root <absolute-coordinator-vault> \
  --current-session "$(./scripts/current-session-id.sh)"
```

New v4 tasks require validated metadata and Summary v2. Active unattended v3 tasks use the same runner with frozen metadata/Summary v1. Require the exact
caller session; never infer focused cmux. The runner:

1. Validate Summary v2 disposition/evidence/gaps and `task_contract.py
   check-handoff`, plan/outcome identity, route, and wikilinks before mutation;
   never rewrite the desired outcome.
2. Require approved review evidence for exact HEAD/profile/summary bytes, then
   archive all rounds, including failed cycles. Parsing attaches validated
   review links; never invent or duplicate them.
3. Call `cmux_surface_lifecycle.py prepare-reap`, binding the exact result path,
   plan, task, session, and recovery marker.
4. Apply one `vault-write.py` transaction: `final` closes its source plan only
   when its dispatch digest still matches; `shared` retains the master plan.
   Preserve concurrent pending edits as plan-close conflicts while recording
   the result independently.
   Updates use `expected_sha256`; new pages receive a real DragonScale address.
5. Reindex, run `validate-vault.py --summary`, then call `complete-reap`.
6. `request-exit` the exact task. After process exit and empty provider ownership,
   close its exact surface and primary workspace/window UUID (and root dashboard), then persist an idempotent receipt. Already-gone succeeds; identity drift or live ownership retains it fail-closed. Never close cmux separately.

The JSON result contains `status`, exact `result_path`, `result_link`,
`plan_close_status` (`closed`, `conflict`, or `retained`), content-free
`warnings`, and `duration_ms`. A `plan-close-conflict` warning means the result
was filed while the concurrently edited pending plan was preserved. Show the
filed link and both completion states. Do not emit a second vault write, review
archive, `/reap`, or close command.

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
