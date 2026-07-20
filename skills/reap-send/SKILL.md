---
name: reap-send
metadata:
  version: 1.3.0
description: Validate and send a completed dispatch task's typed Wiki Summary to its exact coordinator; task-side only.
allowed-tools: Read Write Edit Glob Grep Bash
---

# /reap-send — task-side final handoff

Use only in a dispatched task worktree. It sends one typed summary to the exact
coordinator; it neither writes its vault nor closes the task.

## Normal unattended path

1. Confirm the current directory contains `.task-meta.json` and that its
   `interaction_policy=unattended`, plan/task/session identities, review/reap
   policy, and exact coordinator surface are intact. Treat metadata as
   read-only. A pending escalation or unapproved review blocks final handoff.
2. Finish scoped implementation and tests, commit requested product changes,
   and ensure no unrelated tracked/untracked product changes remain. Never push,
   publish, deploy, delete the worktree/branch, or expand scope.
3. Synthesize `.task-summary.json` with the typed contract: schema version,
   exact approved title/type, bounded body, task/session identity, commit/test
   evidence, and final status. Do not invent wikilinks or review links.
4. Validate and deliver exactly once through code:

   ```bash
   python3 <vault-root>/skills/reap-send/scripts/send_reap.py --worktree "$PWD"
   ```

   The runner validates `task_contract.py`, canonical `.task-summary.json`,
   review readiness, exact callback identity, and duplicate safety. For v3 it
   sends one bounded callback containing the exact `reap-runner.py` command to
   the idle coordinator.
5. Report that the typed summary was delivered, then return idle. Do not send a separate `/reap`,
   poll the coordinator, arm exit, or close any surface. The
   coordinator owns archive/write/validation/final lifecycle.

If the command reports an already-delivered matching callback, treat it as
idempotent success. A mismatched prior summary, missing coordinator, dirty
product state, unresolved escalation, invalid review, or contract drift fails
closed and stays visible.

## Compatibility

For legacy v1/v2 metadata or explicit interactive recovery only, load
[compatibility.md](references/compatibility.md). <!-- context:conditional -->

Legacy transport may read `.wiki-cmux-surface` and `.wiki-reap-command`, but it
must preserve exact task/coordinator identity and typed summary validation. It
must not become a fallback for unattended v3.
