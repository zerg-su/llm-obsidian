---
name: close
metadata:
  version: 1.2.0
description: >-
  Save the current session, then exit its Claude/Codex agent process without
  closing the cmux surface. Use for save-and-close; not for closing tabs.
allowed-tools: Skill Read Write Edit Glob Grep AskUserQuestion Bash
---

# close — save, then gracefully exit

`close` means save the useful session result, commit it, then queue `/exit` to
this exact agent process. Never kill a process or close a cmux surface/tab.

## Workflow

1. Invoke the `save` skill and let it own inference, deduplication, provenance,
   and `vault-write.py`. Do not reimplement save. If nothing is worth saving,
   say so and continue.
2. If this session contains a meaningful reusable shell procedure (at least ten
   non-trivial successful commands) with no runbook, offer `distill-runbook` and
   wait for one answer before exit.
3. Surface any uncommitted changes outside `wiki/`, `.raw/`, `.vault-meta/`, and
   `.claude-memory/`; they remain on disk and are not committed at turn end.
4. As the final tool call, run:

   ```bash
   python3 scripts/queue-session-exit.py
   ```

   The runner owns turn-end: `/exit` beats the Stop hook, so it commits the
   vault itself and reports that under `turn_end` before queueing anything. It
   detects Codex only from Codex env vars, requires the exact `CMUX_SURFACE_ID`,
   and never uses focused/ancestry state. Claude queues direct `/exit`; Codex
   performs the bounded backspace + Tab best-effort path. It never calls
   `cmux close-surface`.
5. `status: blocked` means the vault stayed dirty and nothing was queued — a
   failure, a busy lock, or a deliberate no-commit mode. Do not retry and do not
   exit: report `turn_end.reason` and repair under the failure-to-repair
   contract; only the user ends such a session, with a plain `/exit`. Any
   `turn_end.status` other than `clean` means no commit happened; never claim one.
6. Otherwise end the turn immediately. Do not call another tool after the queue
   runner.

## Final message

State what was saved, what turn-end did with it, and that graceful exit was
queued. If runner status is `manual`, tell the user to enter `/exit`
manually. For Codex always include that fallback (and `/archive` when outside
cmux), because injected slash commands are best-effort. If status is `blocked`,
say the session stays open and why. Mention any uncommitted non-vault changes.

`/exit` ends only the agent and leaves the surface at a shell prompt. Closing a
tab/surface is a separate explicit action and outside this skill.
