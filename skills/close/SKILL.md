---
name: close
metadata:
  version: 1.4.0
description: >-
  Save this session, then close its Orca chat tab or exit its Claude/Codex
  terminal agent while keeping the cmux surface. Use for save-and-close.
allowed-tools: Skill Read Write Edit Glob Grep AskUserQuestion Bash
---

# close — save, then close this session

In an Orca native chat, save and close this chat tab. In cmux, save and queue
`/exit` to this exact agent, leaving its surface at a shell prompt.

1. Invoke `save`; it owns inference, deduplication, provenance and vault writes.
   If nothing is worth saving, say so and continue.
2. For a reusable shell procedure (ten non-trivial successful commands) without
   a runbook, offer `distill-runbook` and wait for one answer before closing.
3. Report uncommitted changes outside `wiki/`, `.raw/`, `.vault-meta/` and
   `.claude-memory/`; turn-end does not commit them.
4. Run `python3 scripts/queue-session-exit.py`. It flushes turn-end synchronously
   before any close action. Follow its receipt:
   - `blocked`: keep the session open. Report `turn_end.reason`; repair under the
     failure-to-repair contract. No close action is authorized until it passes.
   - `closed`: the exact Orca chat tab is absent after a clean turn-end.
     End the turn; no further repository work.
   - `unknown`: inspect the exact tab read-only; never repeat a blind close.
   - `manual`: report that no automatic close occurred. In Orca, ask the user to
     close this tab after a clean turn-end; never prescribe
     `/exit` or `/archive` in the native chat.
   - `queued`: end the turn immediately; no further tools. The cmux runner uses
     only `CMUX_SURFACE_ID`, never focus/ancestry; Claude queues direct `/exit`,
     Codex uses bounded backspace + Tab. It never closes a cmux surface.

State what was saved and what turn-end actually did. Only `turn_end.status: clean`
means the flush succeeded; it need not have created a new commit. For cmux Codex,
include manual `/exit` because injection is best-effort (`/archive` outside cmux).
For native Orca, the runner uses the version-checked tab API; see
[Orca chat close](references/orca-chat.md). Never infer closure from one RPC receipt.
