---
name: close
version: 1.0.0
description: |
  Save the session into the wiki (delegates to the save skill as-is), then gracefully /exit THIS agent process. Claude in cmux sends /exit+Enter directly. Codex in cmux makes a best-effort queued /exit (Tab) and ALWAYS prints a manual /exit fallback, because the Codex TUI does not reliably run injected slash commands. Never closes the cmux surface/tab. Non-cmux falls back to manual /exit or /archive.
  Use when: done for now — file the session and close the agent session.
  Triggers (EN): /close, save and close, save and exit, close the session, wrap up and exit.
  Triggers (RU): закрой сессию, сохрани и закрой, сохрани и выйди, заверши сессию, сейв и клоуз.
allowed-tools: Skill Read Write Edit Glob Grep AskUserQuestion Bash
---

# close: Save the session, then gracefully exit

`/close` = `/save` + a clean `/exit` of the current session. One command to wrap up.

**Codex note:** Codex CLI slash commands are not Claude slash commands. Invoke this
skill as `$llm-obsidian:close` or through `/skills`; `/close` by itself is not a
Codex command. Codex is detected by env
(`CODEX_THREAD_ID` / `CODEX_CI` / `CODEX_MANAGED_BY_NPM`) — env-only, because the
hooks' ancestry detector self-matches when invoked from a skill's `bash -c`
wrapper (see Step 2.0). On Codex-under-cmux the exit is **best-effort**: after
Step 1, queue `/exit` into the current `$CMUX_SURFACE_ID` (type `/exit`, press
`Tab`) as the final tool call, and **always** print a manual fallback, because
the Codex TUI does not reliably run injected slash commands. Do **not** close the
cmux surface for any runtime. If Codex is not in cmux:
`Сохранено. Codex-сессия не в cmux — закрой её вручную через /exit или /archive.`

It is a thin wrapper. The only state-mutating write is delegated to the `save`
skill, which owns its smart fast-path pre-flight. `/close` adds one terminal
action — queuing `/exit` into the agent process in THIS session's cmux surface — which the user
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

Targeting `$CMUX_SURFACE_ID` means only THIS agent process exits. There is no shared
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

### Step 2 — Graceful agent exit (inside this cmux surface)

0. **Detect runtime** — check the Codex env vars, env-only on purpose:
   ```bash
   [ -n "${CODEX_THREAD_ID:-}${CODEX_CI:-}${CODEX_MANAGED_BY_NPM:-}" ] && echo CODEX || echo NOT_CODEX
   ```
   Do **not** use the hooks' ancestry detector (`scripts/detect-runtime.sh`) here.
   That detector is safe for the hooks because the hook runner invokes them with a
   clean argv, but a skill runs its Bash inside a `bash -c '<command>'` wrapper
   whose argv is the whole command text — and `ps` reports argv, so if the command
   mentions "codex" the ancestry walk self-matches and reports Codex in *every*
   runtime, sending Claude down the Codex path and breaking its `/close`. The env
   check has no ancestry walk, so it is immune. It is sufficient because the exit
   is **best-effort**: on a misdetect the manual fallback in Step 3 still closes
   the session.

   On Codex the exit is a Tab-queued `/exit` plus that manual fallback, because
   Codex does not reliably execute injected slash commands mid-turn. On Claude it
   is a direct `/exit\n`. If Codex is not in cmux, use the manual fallback in step 3.

1. Confirm we are inside cmux:
   ```bash
   test -n "$CMUX_SURFACE_ID" && command -v cmux >/dev/null 2>&1 && echo IN_CMUX || echo NO_CMUX
   ```
2. **If IN_CMUX** — queue `/exit` into the agent process running in our own surface. `cmux send` interprets
   `\n` as Enter (see `cmux send --help`).

   For Claude, immediate send is fine:
   ```bash
   cmux send --surface "$CMUX_SURFACE_ID" "/exit\n"
   ```

   For Codex, make a **best-effort** queued `/exit` as the final tool call, then
   rely on the manual fallback in Step 3. Codex queues active-turn slash commands
   with `Tab` (not Enter), and `ctrl+u` does not clear its prompt, so do a light
   bounded backspace clear first — this is best-effort, not a guarantee for
   arbitrarily long queued text, which is exactly why Step 3 always prints the
   manual `/exit` fallback.
   ```bash
   CMUX_CLI="${CMUX_BUNDLED_CLI_PATH:-$(command -v cmux)}"
   i=0
   while [ "$i" -lt 40 ]; do
     "$CMUX_CLI" send-key --surface "$CMUX_SURFACE_ID" backspace >/dev/null 2>&1 || true
     i=$((i + 1))
   done
   "$CMUX_CLI" send --surface "$CMUX_SURFACE_ID" "/exit"
   "$CMUX_CLI" send-key --surface "$CMUX_SURFACE_ID" tab
   ```
   Make this the **last tool call** of the turn — do not run further tool calls
   after it; the next thing that should happen is turn-end, then Codex may run the
   queued `/exit` in this surface only. Do not call `cmux close-surface`; `/close`
   exits the agent process and leaves the tab/surface itself alive.
3. **If NO_CMUX** — do **not** attempt a kill. Print:
   `Сохранено. Сессия не в cmux — закрой её вручную через /exit.` For Codex, also
   mention `/archive` as an alternative.

### Step 3 — Final message (then end the turn)

Emit one short confirmation as the turn's final text.

For Claude:

```
Сохранено как [[Note Title]]. Закрываю сессию — /exit поставлен в очередь, выполнится после автокоммита Stop-хуком.
```

For Codex, the queued exit is best-effort, so **always** include the manual
fallback:

```
Сохранено как [[Note Title]]. Отправил /exit в очередь Codex (Tab) — выполнится после автокоммита Stop-хуком. Если сессия не закроется сама, введи /exit вручную (Codex TUI не всегда принимает queued-команды).
```

Surface uncommitted work **outside** `wiki/` so the user knows it stays on disk
(the Stop hook only commits wiki/.raw/.vault-meta/.claude-memory):

```bash
git status --porcelain 2>/dev/null | grep -vE '^.{2} (wiki/|\.raw/|\.vault-meta/|\.claude-memory/)' | head
```

If that prints anything, add one line: `Незакоммиченные изменения вне wiki/
остаются на диске (не потеряны, но и не закоммичены).`

Then end the turn. The Stop hook commits; the queued `/exit` exits the agent process.

## Notes / edge cases

- **`/exit` vs closing the tab**: `/exit` ends the agent and leaves the surface at a
  shell prompt (standard `/exit` behaviour). Closing the whole surface is a
  separate `cmux` action and out of scope — `/close` never calls `cmux close-surface`.
- **Re-entrancy**: a second queued `/exit` is harmless (session already exiting).
- **Not in cmux** (plain terminal, CI, another multiplexer): falls back to the
  manual-`/exit` message. No process killing, ever.
