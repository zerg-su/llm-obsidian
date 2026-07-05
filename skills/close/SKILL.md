---
name: close
version: 1.0.0
description: |
  Save the session into the wiki (delegates to the save skill as-is), then gracefully /exit THIS Claude Code session. Not a kill: types /exit into the current cmux surface only, so the Stop hook still commits and other open sessions are never touched.
  Use when: done for now — file the session and close Claude in one step.
  Triggers (EN): /close, save and close, save and exit, close the session, wrap up and exit.
  Triggers (RU): закрой сессию, сохрани и закрой, сохрани и выйди, заверши сессию, сейв и клоуз.
allowed-tools: Skill Read Write Edit Glob Grep AskUserQuestion Bash
---

# close: Save the session, then gracefully exit

`/close` = `/save` + a clean `/exit` of the current session. One command to wrap up.

It is a thin wrapper. The only state-mutating write is delegated to the `save`
skill, which owns its smart fast-path pre-flight. `/close` adds one terminal
action — queuing `/exit` into THIS session's own cmux surface — which the user
opted into by invoking `/close`. No extra clarification battery here.

## Why this is a graceful exit and not a kill

There is no supported way for the model to quit Claude Code mid-turn, and killing
the process mid-turn would skip the `Stop` hook (reindex + memory-backup + git
autocommit) — losing the commit. So `/close` never kills anything. Instead it
*types* `/exit` into its own terminal surface via `cmux send`. Input typed
mid-turn is queued and runs only after the turn fully ends — i.e. after the
`Stop` hook has already committed. Net sequence:

```
/close turn:  run /save  →  queue "/exit" into $CMUX_SURFACE_ID  →  end turn
Stop hook:    reindex → memory-backup → git commit            (work saved)
idle:         queued /exit runs → SessionEnd → graceful shutdown
```

Targeting `$CMUX_SURFACE_ID` means only THIS session closes. There is no shared
sentinel file, so other sessions open on the same vault are never affected. This
is the same `cmux send` mechanism the `/reap` pipeline already relies on.

## Workflow

### Step 1 — Save (delegate to /save)

Invoke the `save` skill (Skill tool, `skill: save`) and let it run its normal
workflow: it infers note type/folder/title via its smart fast-path, batch-writes
the page plus index/log/hot, and confirms. Do **not** reimplement save here — run
it as-is so behaviour never drifts. If save escalates to one clarifying question
(genuinely ambiguous), let that resolve normally; the exit in Step 2 happens only
**after** save has actually filed the page.

If the conversation has nothing worth saving (per save's "What to Save vs Skip"),
still proceed to Step 2 — `/close` may exit without filing a page — but say so in
the final message.

Runbook last-chance check: if the session ran a meaningful shell procedure
(≥10 non-trivial successful commands in `.vault-meta/command-log.jsonl` for this
session) and no runbook was filed, say «Была содержательная shell-процедура —
/distill-runbook сделает из неё ранбук» and wait for one user reply before
Step 2 — after /exit the session's command trail loses its context.

### Step 2 — Graceful exit (this cmux surface only)

1. Confirm we are inside cmux:
   ```bash
   test -n "$CMUX_SURFACE_ID" && command -v cmux >/dev/null 2>&1 && echo IN_CMUX || echo NO_CMUX
   ```
2. **If IN_CMUX** — queue `/exit` into our own surface. `cmux send` interprets
   `\n` as Enter (see `cmux send --help`):
   ```bash
   cmux send --surface "$CMUX_SURFACE_ID" "/exit\n"
   ```
   Because this runs mid-turn, the `/exit` is buffered and executes after the turn
   ends and the Stop hook has committed. Make this the **last tool call** of the
   turn — do not run further tool calls after it; the next thing that should happen
   is turn-end → commit → queued `/exit`.
3. **If NO_CMUX** — do **not** attempt a kill. Print:
   `Сохранено. Сессия не в cmux — закрой её вручную через /exit.` and stop.

### Step 3 — Final message (then end the turn)

Emit one short confirmation as the turn's final text, e.g.:

```
Сохранено как [[Note Title]]. Закрываю сессию — /exit поставлен в очередь, выполнится после автокоммита Stop-хуком.
```

Surface uncommitted work **outside** `wiki/` so the user knows it stays on disk
(the Stop hook only commits wiki/.raw/.vault-meta/.claude-memory):

```bash
git status --porcelain 2>/dev/null | grep -vE '^.{2} (wiki/|\.raw/|\.vault-meta/|\.claude-memory/)' | head
```

If that prints anything, add one line: `Незакоммиченные изменения вне wiki/
остаются на диске (не потеряны, но и не закоммичены).`

Then end the turn. The Stop hook commits; the queued `/exit` closes the session.

## Notes / edge cases

- **`/exit` vs closing the tab**: `/exit` ends Claude and leaves the surface at a
  shell prompt (standard `/exit` behaviour). Closing the whole surface is a
  separate `cmux` action and out of scope — `/close` only closes Claude.
- **Re-entrancy**: a second queued `/exit` is harmless (session already exiting).
- **Not in cmux** (plain terminal, CI, another multiplexer): falls back to the
  manual-`/exit` message. No process killing, ever.
