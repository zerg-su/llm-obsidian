---
name: reap-send
description: >-
  From a dispatch task split, validate and send its typed wiki summary back to
  the coordinator over cmux. Requires .wiki-cmux-surface; not for normal saves.
allowed-tools: Read Write Edit Bash
---

# /reap-send — task-side handoff to the wiki

The symmetric counterpart to /dispatch (wiki-side spawn) and /reap (wiki-side ingest). Runs from the task worktree — any CWD that contains the `.wiki-cmux-surface` file placed there by /dispatch. Requires cmux. It never closes a surface directly; approved unattended final reap later arms task `/exit` and close-on-process-return.

For v3 unattended tasks, callback delivery is code-owned. After writing and
rendering the typed summary, run exactly once:

```bash
python3 <vault-root>/skills/reap-send/scripts/send_reap.py --worktree <exact-worktree>
```

The sender validates task metadata, summary schema, approved type/title, and
the exact coordinator surface. It sends one operation-bound `reap-runner.py`
command, so the coordinator does not rediscover the task or reproduce reap
phases. Do not send a separate `/reap`, call `finish`, or resend after success.
The remaining RPC section is the legacy/interactive compatibility reference.

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
                                       ├─ write .task-summary.json
                                       ├─ render .task-summary.md
                                       └─ cmux send --surface <wiki> "/reap" or .wiki-reap-command
                                                              │
/reap (triggers automatically)  ◄──────────────────────────────┘
├─ Read .task-summary.json
├─ Routing → wiki/<folder>/
├─ Write page + bookkeeping via vault-write
└─ Echo cleanup proposal
```

## Input

```
/reap-send
```

No arguments. The CWD must be a worktree with `.wiki-cmux-surface` and `.task-prompt.md` files (placed by `/dispatch`). New `/dispatch` also writes `.wiki-agent-runtime` and `.wiki-reap-command`; if they are absent, fallback = `claude` for old worktrees.

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

For unattended v2 tasks, ignore `.task-*`, `.review-*`, `.wiki-*`, and
`.obsidian/workspace*.json`. If other changes remain, inspect them: explicitly
stage and commit approved in-scope work; escalate ambiguity or scope drift to
the coordinator. Do not wait on a routine commit prompt in the background pane.

For legacy/interactive tasks, if non-empty — ask the user:
```
The worktree has unsaved edits:
  <git status output>

Commit before reap-send? [y/N]
```

Do not `git add .` silently — the user may intentionally keep something untracked (like `.task-prompt.md` itself, for that matter).

### 1.3 Synthesize the typed Wiki Summary

Create this canonical object from the completed task and `.task-prompt.md`:

```json
{"schema_version":1,"type":"session|decision|runbook|incident|service-update|repo-touch","title":"Note Title","session":"executor SESSION_ID","body":"declarative Markdown with [[wikilinks]]"}
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

Read `.task-meta.json`. For v2 `interaction_policy=unattended`, validate it
with `scripts/task_contract.py validate`, require the summary type/title to
match `reap_policy`, and skip this echo-confirm: the upfront plan already
authorized the handoff. Any drift stops and escalates to the coordinator.

For legacy/interactive tasks, keep the gate:

```
Ready to send:
  task:    <task-name>          ◄── from the first line of .task-prompt.md ("# Task: <name>")
  type:    <type>
  title:   <Title>
  file:    <CWD>/.task-summary.json (+ derived .md)
  RPC:     cmux send --surface <wiki-surface-id> "<runtime-specific reap command>"

After this the wiki agent in the left split wakes up automatically,
reads `.task-summary.json`, and files it into the vault by the /reap rules.

Send?
```

Wait for "yes" only in interactive mode.

**Why the task name is explicit**: between the dispatch and our reap-send, other dispatches / saves may have landed in `wiki/log.md`. Without an argument, `/reap` in the wiki split resolves the task name from the latest `dispatch | <name>` entry in `log.md` — often not our task. Passing `<task-name>` explicitly guarantees the wiki agent goes to the right worktree.

### 2.2 Write canonical JSON and render Markdown

Write `./.task-summary.json`, then validate and render it:

```bash
if [ -f .review-archive.json ]; then
  python3 <vault-root>/scripts/parse-wiki-summary.py \
    --json-file .task-summary.json --render-markdown \
    --review-archive-marker .review-archive.json > .task-summary.md
else
  python3 <vault-root>/scripts/parse-wiki-summary.py \
    --json-file .task-summary.json --render-markdown > .task-summary.md
fi
```

Use the marker only when that coordinator-generated file already exists.
Normally task-side `finish` defers archival
and `/reap` appends the link later; never invent or edit the marker here.

Exit 2 means the model-produced contract is invalid: fix the JSON before any
callback. JSON is the source of truth; Markdown is a deterministic compatibility
view for old reap flows and humans.

The code-owned sender also resolves one safe model-output mismatch before
delivery: prose wikilinks whose targets do not exist are rendered as their plain
display text in both canonical JSON and derived Markdown. Existing links and
wikilink-looking examples inside inline/fenced code remain unchanged. The
coordinator-side reap runner still rejects any unresolved link that bypasses
this preparation, before vault mutation.

### 2.3 Read the wiki-surface ID, reap command, and task name (compatibility only)

```bash
WIKI_SURFACE=$(cat .wiki-cmux-surface)
WIKI_RUNTIME=$(cat .wiki-agent-runtime 2>/dev/null || true)
WIKI_REAP_COMMAND=$(cat .wiki-reap-command 2>/dev/null || true)
[ -n "$WIKI_RUNTIME" ] || WIKI_RUNTIME=claude
TASK_NAME=$(head -1 .task-prompt.md | sed -n 's/^# Task: *//p')
INTERACTION_POLICY=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("interaction_policy", "interactive"))' .task-meta.json 2>/dev/null || true)
[ -n "$INTERACTION_POLICY" ] || INTERACTION_POLICY=interactive
```

`.task-prompt.md` was placed by `/dispatch` and its first line is `# Task: <task-name>`. That is the authoritative source for the task name (the worktree folder name does not always match: dispatch adds a `<repo>-` prefix).

If `WIKI_SURFACE` or `TASK_NAME` is empty — stop, tell the user that `.task-prompt.md` is broken / `.wiki-cmux-surface` is missing.

### 2.4 RPC: cmux send the /reap command into the wiki split (compatibility only)

```bash
if [ -n "$WIKI_REAP_COMMAND" ]; then
  REAP_BASE="$WIKI_REAP_COMMAND"
elif [ "$WIKI_RUNTIME" = "codex" ]; then
  REAP_BASE="\$llm-obsidian:reap"
else
  REAP_BASE="/reap"
fi
# v2 unattended policy always requests the already-approved final mode.
if [ "$INTERACTION_POLICY" = unattended ]; then
  RPC="$REAP_BASE final $TASK_NAME"
else
  RPC="$REAP_BASE $TASK_NAME"
fi
cmux send --surface "$WIKI_SURFACE" "$RPC"
sleep 0.2
cmux send-key --surface "$WIKI_SURFACE" Enter
```

`cmux send` types text into the pty terminal of the split. The agent there interprets it as user input. The slash/skill command `/reap <task-name>` or the plugin-qualified command from `.wiki-reap-command` is handled by the client and launches the skill with an explicit argument — the wiki agent will NOT resolve via `wiki/log.md` (which could land on the wrong task if more dispatches / saves happened after ours).

**Gotcha**: if the wiki agent is mid-turn on other work, `/reap` lands in the input buffer and fires after the current turn. That is normal — but if the user is actively chatting with the wiki agent, better warn them.

### 2.5 Final message to the user

```
Sent:
  .task-summary.json validated; .task-summary.md rendered
  <RPC> sent to wiki split <wiki-surface-id>

The coordinator will validate the contract and file it. Unattended final reap
then validates the vault, arms task exit, and closes the exact task surface
after the process returns. Worktree and branch remain.
```

Stop; do not attempt anything in the vault yourself (it is off-limits from the task CWD by the rules).

---

## Edge cases

1. **`.wiki-cmux-surface` missing** — a /dispatch older than the RPC mode. Stop with a message: "update /dispatch (skills/dispatch/SKILL.md in the vault repo) to the RPC mode, or do it manually: switch to the wiki agent and say `/reap`".
2. **`cmux send` not responding** — the daemon is down. Report; do not attempt workarounds.
3. **The wiki-surface ID is stale** (the user accidentally closed the wiki split) — `cmux send --surface <id>` returns `not_found`. Stop, report.
4. **The task agent has not produced a summary yet and `.task-prompt.md` gives no structure hints** — ask the user interactively (type? title?), then synthesize.
5. **Several task splits at once** — each has its own `.task-summary.json` in its own worktree; no conflict.

---

## Do not

- Do NOT write into the vault's `wiki/` directly (it breaks the single-writer invariant — a race with the vault's Stop-hook autocommit).
- Do NOT delete `.task-summary.json` or its derived Markdown after sending.
- Do NOT close `.task-cmux-surface` directly — reap-send only sends RPC. Final
  reap owns the armed exit/close decision.
- Do NOT try to open the vault repo if the CWD is not there — the task agent must live in the worktree.
- Do NOT send other slash commands via cmux send (like an automatic `/save` or `/commit` in the wiki) — only `/reap`; everything else the wiki agent decides itself.
