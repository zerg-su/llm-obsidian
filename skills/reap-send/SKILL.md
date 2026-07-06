---
name: reap-send
description: >
  Runs inside a task-split (created via /dispatch). The task agent assembles a
  `## Wiki Summary` block (type/title/body), writes it to ./.task-summary.md in
  the current CWD (the worktree), reads ./.wiki-cmux-surface for the surface ID
  of the wiki agent on the left, reads ./.wiki-agent-runtime when present, and
  sends the runtime-specific reap command there via `cmux send` — the
  wiki agent wakes up automatically and files the summary into the vault. One
  call in the task-split = a full handoff to the wiki. Requires cmux; works from
  any CWD that contains a `.wiki-cmux-surface` file.
  Triggers on: "/reap-send", "reap-send", "сэндь в вики", "отправь summary в вики",
  "хэндофф в вики", "rip-send".
allowed-tools: Read Write Edit Bash
---

# /reap-send — task-side handoff to the wiki

The symmetric counterpart to /dispatch (wiki-side spawn) and /reap (wiki-side ingest). Runs from the task worktree — any CWD that contains the `.wiki-cmux-surface` file placed there by /dispatch. Requires cmux (the same dependency as /dispatch). It does not close cmux surfaces; task-agent exit is a separate `/exit`.

## Context: how this works in the pipeline

```
wiki agent (vault repo)                task agent (worktree)
─────────────────────────────────      ──────────────────────
/dispatch <task>                  ──►  spawn new-split right
                                       .wiki-cmux-surface  ◄── written by dispatch
                                       .task-cmux-surface  ◄── written by dispatch
                                       .task-prompt.md     ◄── pre-loaded context

                                       <task agent works>
                                       <code, tests, commits>

                                       /reap-send  ◄── this skill
                                       ├─ assemble the summary
                                       ├─ write .task-summary.md
                                       └─ cmux send --surface <wiki> "/reap" or "$llm-obsidian:reap"
                                                              │
/reap (triggers automatically)  ◄──────────────────────────────┘
├─ Read .task-summary.md
├─ Routing → wiki/<folder>/
├─ Write page + bookkeeping via vault-write
└─ Echo cleanup proposal
```

## Input

```
/reap-send
```

No arguments. The CWD must be a worktree with `.wiki-cmux-surface` and `.task-prompt.md` files (placed by `/dispatch`). New `/dispatch` also writes `.wiki-agent-runtime`; if it is absent, fallback = `claude` for old worktrees.

---

## Phase 1: Prerequisites

### 1.1 Verify CWD

```bash
test -f .task-prompt.md || echo "ERROR: not in a task-worktree (no .task-prompt.md)"
test -f .wiki-cmux-surface || echo "ERROR: no .wiki-cmux-surface — was this worktree spawned by /dispatch?"
```

If the files are missing — stop. Tell the user either the CWD is wrong, or /dispatch did not write the wiki-surface ID (a /dispatch older than the RPC mode). `.wiki-agent-runtime` is optional; when absent, assume the wiki agent is Claude so old task worktrees keep working.

### 1.2 Commit pending code changes

The task agent should have committed already, but verify. If there are uncommitted changes:

```bash
git status --short
```

If non-empty — ask the user:
```
The worktree has unsaved edits:
  <git status output>

Commit before reap-send? [y/N]
```

Do not `git add .` silently — the user may intentionally keep something untracked (like `.task-prompt.md` itself, for that matter).

### 1.3 Synthesize the Wiki Summary block

If the task agent has **already** printed a `## Wiki Summary` block in the conversation — take it from context. Otherwise synthesize it now per the rules in `.task-prompt.md` (the "Finalization" section):

```
## Wiki Summary

type: <session|decision|runbook|incident|service-update|repo-touch>
title: <Note Title>
session: <the SESSION_ID from <vault-root>/scripts/current-session-id.sh for this task session>

<body in declarative present tense, with [[wikilinks]] to adjacent pages>
```

The `session:` field is mandatory — it is the **executor** session (distinct from both the planning session and the wiki agent's dispatch/reap sessions). The wiki agent appends it to the `sessions:` of the result page and the plan page (the provenance chain plan -> execution).

Routing help:
- `session` — general summary of what was done (the default for most tasks)
- `decision` — an architectural decision was made → `wiki/decisions/`
- `runbook` — a step-by-step procedure → `wiki/runbooks/`
- `incident` — a post-mortem → `wiki/incidents/`
- `service-update` — updated a service page → `wiki/services/<title>.md`
- `repo-touch` — updated a repo page → `wiki/repos/<title>.md`

---

## Phase 2: Write the summary file + RPC trigger

### 2.1 Echo-confirm to the user

```
Ready to send:
  task:    <task-name>          ◄── from the first line of .task-prompt.md ("# Task: <name>")
  type:    <type>
  title:   <Title>
  file:    <CWD>/.task-summary.md
  RPC:     cmux send --surface <wiki-surface-id> "<runtime-specific reap command>"

After this the wiki agent in the left split wakes up automatically,
reads .task-summary.md, and files it into the vault by the /reap rules.

Send?
```

Wait for "yes". Do not send without confirmation.

**Why the task name is explicit**: between the dispatch and our reap-send, other dispatches / saves may have landed in `wiki/log.md`. Without an argument, `/reap` in the wiki split resolves the task name from the latest `dispatch | <name>` entry in `log.md` — often not our task. Passing `<task-name>` explicitly guarantees the wiki agent goes to the right worktree.

### 2.2 Write `.task-summary.md`

`Write ./.task-summary.md` with the full block (including the `## Wiki Summary` header). The file is the single source of truth for the wiki agent. If the file already exists (a repeated /reap-send) — overwrite silently (the latest version always wins).

### 2.3 Read the wiki-surface ID and the task name

```bash
WIKI_SURFACE=$(cat .wiki-cmux-surface)
WIKI_RUNTIME=$(cat .wiki-agent-runtime 2>/dev/null || true)
[ -n "$WIKI_RUNTIME" ] || WIKI_RUNTIME=claude
TASK_NAME=$(head -1 .task-prompt.md | sed -n 's/^# Task: *//p')
```

`.task-prompt.md` was placed by `/dispatch` and its first line is `# Task: <task-name>`. That is the authoritative source for the task name (the worktree folder name does not always match: dispatch adds a `<repo>-` prefix).

If `WIKI_SURFACE` or `TASK_NAME` is empty — stop, tell the user that `.task-prompt.md` is broken / `.wiki-cmux-surface` is missing.

### 2.4 RPC: cmux send the /reap command into the wiki split

```bash
if [ "$WIKI_RUNTIME" = "codex" ]; then
  RPC="\$llm-obsidian:reap $TASK_NAME"
else
  RPC="/reap $TASK_NAME"
fi
cmux send --surface "$WIKI_SURFACE" "$RPC"
cmux send-key --surface "$WIKI_SURFACE" Enter
```

`cmux send` types text into the pty terminal of the split. The agent there interprets it as user input. The slash/skill command `/reap <task-name>` or `$llm-obsidian:reap <task-name>` is handled by the client and launches the skill with an explicit argument — the wiki agent will NOT resolve via `wiki/log.md` (which could land on the wrong task if more dispatches / saves happened after ours).

**Gotcha**: if the wiki agent is mid-turn on other work, `/reap` lands in the input buffer and fires after the current turn. That is normal — but if the user is actively chatting with the wiki agent, better warn them.

### 2.5 Final message to the user

```
Sent:
  .task-summary.md written (<size> bytes)
  <RPC> sent to wiki split <wiki-surface-id>

Switch to the left split — the wiki agent will pick up
.task-summary.md in a moment, run its echo-confirm, and file it. After
that it will offer file/worktree cleanup; the cmux surface is not closed by reap.
```

Stop; do not attempt anything in the vault yourself (it is off-limits from the task CWD by the rules).

---

## Edge cases

1. **`.wiki-cmux-surface` missing** — a /dispatch older than the RPC mode. Stop with a message: "update /dispatch (skills/dispatch/SKILL.md in the vault repo) to the RPC mode, or do it manually: switch to the wiki agent and say `/reap`".
2. **`cmux send` not responding** — the daemon is down. Report; do not attempt workarounds.
3. **The wiki-surface ID is stale** (the user accidentally closed the wiki split) — `cmux send --surface <id>` returns `not_found`. Stop, report.
4. **The task agent has not produced a summary yet and `.task-prompt.md` gives no structure hints** — ask the user interactively (type? title?), then synthesize.
5. **Several task splits at once** — each has its own `.task-summary.md` in its own worktree; no conflict.

---

## Do not

- Do NOT write into the vault's `wiki/` directly (it breaks the single-writer invariant — a race with the vault's Stop-hook autocommit).
- Do NOT delete `.task-summary.md` after sending — the wiki agent will read it.
- Do NOT close `.task-cmux-surface` and do NOT call `cmux close-surface` — reap-send only sends RPC. Worktree cleanup remains with the wiki agent /reap and the user.
- Do NOT try to open the vault repo if the CWD is not there — the task agent must live in the worktree.
- Do NOT send other slash commands via cmux send (like an automatic `/save` or `/commit` in the wiki) — only `/reap`; everything else the wiki agent decides itself.
