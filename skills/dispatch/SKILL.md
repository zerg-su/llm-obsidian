---
name: dispatch
version: 1.2.0
description: |
  Spawn a parallel cmux right-split worktree: resolve repo, create worktree, pick sub-agents, launch split Claude with --model (default opus), hand off the latest approved plan from wiki/plans/ (plan-mode: no re-approval). Pre-flight mandatory. Requires cmux.
  Use when: a task benefits from isolation, or a ready plan should run on a cheaper model.
  Triggers (EN): dispatch, spawn task, parallel task, task split, new worktree.
  Triggers (RU): сделай диспатч, запусти параллельную задачу, worktree для, задиспатчь на опусе, передай план в сплит.
allowed-tools: Read Write Edit Glob Grep Bash AskUserQuestion
---

# /dispatch — spawn a parallel task in cmux

Keeps the "wiki as backbone" paradigm but moves execution into a separate cmux task split. The wiki Claude (this session) does **not** execute the task — it spawns a task split and keeps dispatching other tasks in parallel. When the task is done — `/reap` it into the wiki.

## Phase 0: cmux availability check

`/dispatch` depends on cmux (a terminal multiplexer CLI). First thing, before any parsing:

```bash
command -v cmux >/dev/null 2>&1 || echo "NO_CMUX"
```

If cmux is not installed — stop with a friendly message:

```
/dispatch requires cmux (terminal multiplexer) to spawn a parallel split.
See the "Parallel tasks" section of README.md for setup.

Alternative: I can run this task right here in the current session instead —
say the word and describe the task.
```

Do not attempt workarounds (background Bash, tmux, etc.).

## Input

```
/dispatch <free-form description>
```

The description is free text, spoken or typed. From "add a dark-mode toggle to the blog, repo `my-blog`, new branch" parse:

- `task_name` — kebab-case from the essence (`blog-dark-mode`). Short, ASCII, no slashes.
- `target_repo` — repo name (`my-blog`).
- `branch_intent` — `new` (create `task/<task_name>` from the default branch) or the name of a specific existing branch.
- `description` — the substantive part for the task prompt (what exactly to do and why).
- `model` — model alias for the task Claude in the split. Default **`opus`**. From text: "on sonnet / на сонете" → `sonnet`, "on fable / на фейбле" → `fable`, "on opus / на опусе" → `opus`. Valid values: `opus | sonnet | fable | haiku`.
- `plan_ref` — approved-plan handoff mode (resolved in Phase 1.4b): nothing said → auto-resolve the latest plan of the current session; "hand off plan <slug|path>" → that file; "no plan" → classic-mode forced.

If something cannot be extracted from the text — ask with a single question, do not dance around it.

---

## Phase 1: Parse + Resolve (no writes, no spawn)

### 1.1 Parse

Extract `task_name`, `target_repo`, `branch_intent`, `description` from the input.

### 1.2 Resolve the repo via a fallback chain

**a) wiki/repos** (optional — only if this vault keeps repo pages) — exact or fuzzy match:
```
Glob wiki/repos/*<target_repo>*.md
```
If found — Read the frontmatter; if it records a local `path:`, use it. Otherwise fall through to (b).

**b) Local search** under a configurable projects root:
```bash
PROJECTS_ROOT="${LLM_OBSIDIAN_PROJECTS_ROOT:-$HOME/Projects}"
find "$PROJECTS_ROOT" -maxdepth 4 -name <target_repo> -type d 2>/dev/null | head -5
```
Exactly one hit — take that path. Several — list the candidates and wait for a choice.

**c) Ask the user** — if nothing was found locally, ask for the absolute path to the repo (or offer to cancel). Never clone anything without an explicit path and an explicit "yes".

**d) Ambiguous** — list candidates, wait for the user's choice.

### 1.3 Resolve the branch

- An existing branch was named → `git -C <repo> branch --list <branch>` to verify. Not local — `git -C <repo> branch -a --list "*<branch>*"` to check remotes. Not anywhere — ask.
- `new` or unspecified → `task_name` → branch name `task/<task_name>`. Base — `git -C <repo> symbolic-ref refs/remotes/origin/HEAD | sed 's|^refs/remotes/origin/||'` (usually `main` or `master`).

### 1.4 Collect wiki context

Find 3-5 relevant pages for the pre-prompt:

1. `wiki/hot.md` — Read, extract mentions of the target repo / task topic.
2. `Glob wiki/**/*.md` by keywords from the description (via Grep `-l`).
3. `wiki/repos/<repo>.md` if it exists.

Do not chase exhaustive context — 3-5 [[links]] with a one-line description each is enough. The task Claude will read the rest itself via live vault access.

### 1.4b Resolve the approved plan (plan-mode vs classic-mode)

Every approved plan is auto-saved by the plan-capture hook (`.claude/hooks/plan-capture.sh`) into `wiki/plans/<TS>-<slug>.md` (`type: plan`, `session_id:`, `status: pending`). Target workflow: the wiki Claude (expensive model) gathers context and shapes the plan; the split (cheaper model) executes — the plan is handed off as a file, no re-planning.

Default — auto-resolve the latest plan of the current session:

```bash
PLAN_FILE=$(grep -l "session_id: $CLAUDE_CODE_SESSION_ID" wiki/plans/*.md 2>/dev/null | sort | tail -1)
```

(file names start with a timestamp, so sort by name = sort by time).

- Found → **plan-mode**: the absolute path goes into `.task-prompt.md` (Approved plan section, see Phase 2.2) and `.task-meta.json`.
- Not found → **classic-mode** (plan-first inside the split, the original behavior). No questions asked.
- The user explicitly named a plan → take it from `wiki/plans/`, even if it belongs to another session (cross-window is valid — note it in the echo).
- "No plan" → classic-mode forced.

### 1.6 Resolve sub-agents for the task scope

`/dispatch` does **not** pick agents for the task Claude itself — that would duplicate the router hook's logic. Instead: read `.claude/skill-rules.json` (single source of truth, already used by the UserPromptSubmit hook), match `description` against `agent_rules.patterns`, pick the top-2 candidates by match count.

```bash
# pseudo-code; real script inline:
matches=$(python3 -c "
import json, re, sys
rules = json.load(open('.claude/skill-rules.json'))
desc = sys.stdin.read()
scored = []
for rule in rules.get('agent_rules', []):
    hits = sum(1 for p in rule.get('patterns', []) if re.search(p, desc))
    if hits > 0:
        scored.append((hits, rule['agent'], rule.get('hint') or ''))
scored.sort(reverse=True)
for hits, name, hint in scored[:2]:
    print(f'{name}\t{hint}')
" <<< "$description")
```

The result — 0-2 lines of `<agent_name>\t<hint>`. Goes into `.task-meta.json` (see Phase 2.3) and is mentioned in the pre-prompt as "Suggested agents for deep audit-type work". The task Claude **may** delegate to them via the Agent tool, it is **not obliged** — a hint, not a command.

Note: `agent_rules` ships as an empty array in this repo — with 0 matches the section is omitted from the pre-prompt entirely (this is the default for a fresh install; populate `agent_rules` if you add custom agents).

### 1.5 Echo-confirm

Show the user the plan **in a single block**:

```
Parsing as:
  task:        blog-dark-mode
  repo:        ~/Projects/my-blog
  base branch: main
  new branch:  task/blog-dark-mode
  worktree:    ~/Projects/worktrees/my-blog-blog-dark-mode
  model:       opus (default)
  plan:        wiki/plans/2026-07-03-113935-<slug>.md (approved in this session)
  wiki context:
    - [[Blog]] — main page about the blog setup
    - [[Dark Mode Research]] — notes from an earlier comparison
    - [[CSS Conventions]] — styling rules used across projects
Spawn the task split?
```

The `plan:` line shows either the resolved file (marked "approved in this session" / "from another session, named explicitly"), or `none — plan-first in the split`. A wrong auto-resolve gets caught exactly here.

Wait for "yes / no / edit". Do not proceed to Phase 2 without explicit consent.

---

## Phase 2: Spawn (batched Bash)

### 2.1 Worktree

```bash
WORKTREES_DIR="${LLM_OBSIDIAN_WORKTREES:-$HOME/Projects/worktrees}"
mkdir -p "$WORKTREES_DIR"
```

For a new branch:
```bash
git -C <repo-path> worktree add -b task/<task_name> \
    "$WORKTREES_DIR/<repo>-<task_name>" <base-branch>
```

For an existing branch:
```bash
git -C <repo-path> worktree add \
    "$WORKTREES_DIR/<repo>-<task_name>" <existing-branch>
```

If the worktree already exists (`already used by worktree at`) — tell the user, do not force it.

### 2.2 Generate `.task-prompt.md` in the worktree

**Read** `references/task-prompt-template.md` — the full template (task description, pre-loaded wiki context, suggested sub-agents, wiki access rules, plan section, finalization via /reap-send). Substitute the placeholders from Phase 1 and write via the Write tool (not echo/heredoc through Bash — the template is long).

The plan section is **conditional** — render exactly one branch:
- **plan-mode** (Phase 1.4b produced a file) → branch A "Approved plan": absolute path to the plan, echo + immediate execution;
- **classic-mode** → branch B "plan-first workflow" (as before).

Key template invariants (preserve them if you edit the reference):
- **plan-first** is mandatory ONLY in classic-mode; in plan-mode the Approved plan replaces it (plan echo, start without approval, stop conditions at forks remain);
- the **vault is read-only** for the task Claude — vault edits happen only via /reap (otherwise a race with the vault's Stop-hook autocommit);
- **Suggested sub-agents** — a hint, not a command;
- finalization — `/reap-send`, fallback = print the `## Wiki Summary` block into the chat.

### 2.3 Spawn a split surface in the current workspace

Paradigm: the task Claude lives as a **split surface to the right** of the wiki Claude in the very same cmux workspace, **not** in a separate workspace or window. The user sees both sessions at once, switches between them as panes, and parallel tasks accumulate as extra surfaces in the same split stack.

**First** — capture the wiki surface ID **before** the new-split (in case focus shifts):

```bash
WIKI_SURFACE=$(cmux list-pane-surfaces 2>&1 | grep '^\*' | grep -oE 'surface:[0-9]+' | head -1)
```

The `*` prefix marks the selected surface — that is the wiki Claude (we are in it right now). If it cannot be determined — report and stop (the RPC mode in /reap-send does not work without it).

```bash
SURFACE_LINE=$(cmux new-split right --focus false 2>&1)
# Output format "OK surface:N workspace:M" — parse out the surface ID
SURFACE_ID=$(echo "$SURFACE_LINE" | grep -oE 'surface:[0-9]+' | head -1)
```

If `cmux new-split` fails (no active workspace / pane cannot split) — report and stop. Do not fall back to `new-workspace`.

Save **both** surface IDs into the worktree (backwards-compat with reap / reap-send):

```bash
echo "$SURFACE_ID" > <worktree-path>/.task-cmux-surface   # task Claude (right split)
echo "$WIKI_SURFACE" > <worktree-path>/.wiki-cmux-surface  # wiki Claude (left split, for /reap-send RPC)
```

`.task-cmux-surface` is needed by `/reap` (wiki-side fetch via read-screen, fallback when `.task-summary.md` is missing).
`.wiki-cmux-surface` is needed by `/reap-send` (task-side RPC trigger via `cmux send --surface <wiki> "/reap"`).

**Additionally** — write `<worktree-path>/.task-meta.json` with the **origin-session binding** for multi-session safety:

```bash
cat > <worktree-path>/.task-meta.json <<EOF
{
  "version": 1,
  "task_name": "<task_name>",
  "origin_session": "${CLAUDE_CODE_SESSION_ID:-unknown}",
  "spawned_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "wiki_surface": "$WIKI_SURFACE",
  "task_surface": "$SURFACE_ID",
  "target_repo": "<repo-path>",
  "branch": "task/<task_name>",
  "base_branch": "<base-branch>",
  "model": "<model>",
  "plan_file": <"/abs/path/to/wiki/plans/<file>.md" | null>,
  "suggested_agents": [<from Phase 1.6, JSON array of {"name", "hint"}>]
}
EOF
```

Why `.task-meta.json`:
- `origin_session` — `/reap` compares it with the current `$CLAUDE_CODE_SESSION_ID`; on mismatch (the wiki Claude restarted between dispatch and reap, or another session runs reap) — WARNING and an explicit confirm before filing. Does not block, but stays visible. See Edge case 6.
- `suggested_agents` — `/reap` can add them to the saved page's frontmatter or use them for cross-refs.
- `plan_file` — on mode=final `/reap` closes the plan via `plan_close` (vault-write): `status: executed` + a link to the result + the exec session (closing the loop plan → execution → result).
- `spawned_at` — for usage analysis / log rollups later.

Launch the task Claude in the split with the pre-prompt. `new-split` does not accept `--cwd`, so `cd` is the first command:

```bash
cmux send --surface "$SURFACE_ID" "cd <worktree-path>; clear; claude --model <model> \"\$(cat .task-prompt.md)\""
cmux send-key --surface "$SURFACE_ID" Enter
```

NB: `--model` takes exactly one value — a positional prompt after it is safe (unlike variadic flags such as `--mcp-config`, which would swallow the prompt).

(`clear` wipes the shell prompt left by `new-split`; otherwise it stays in the scrollback and pollutes a future `read-screen` in `/reap`.)

### 2.4 Verify

```bash
cmux list-pane-surfaces 2>&1 | grep -E "surface:${SURFACE_ID#surface:}\b"
```

The surface must be in the list. Optionally, after 1-2 seconds:

```bash
cmux read-screen --surface "$SURFACE_ID" --lines 20
```

If the output shows a running `claude` process — success.

---

## Phase 3: Log (vault-write, log-only)

Payload goes through the dispatcher script (NOT a direct Edit):

```bash
python3 scripts/vault-write.py <<'PAYLOAD'
{"log_entry": "## [YYYY-MM-DD HH:MM] dispatch | <task_name>\n\nSpawned a task split (cmux `<surface-id>`, model <model>) to the right of the wiki Claude in worktree `<worktree-path>` for: <one-line description>. Target repo <[[repo-page]] or path>, branch `task/<task_name>` from `<base-branch>`. Plan: <`wiki/plans/<file>.md` handed to the split | none (plan-first in the split)>. Pre-loaded context: [[X]], [[Y]], [[Z]]. Surface ID in `<worktree>/.task-cmux-surface` for /reap. Awaiting `## Wiki Summary`, then `/reap`."}
PAYLOAD
```

Do **not** touch `wiki/hot.md` — dispatches are not save events and must not clutter Recent Changes. Only the result lands in hot.md, via /reap.

### Final message to the user

```
Spawned task split:
  worktree:  <worktree-path>
  branch:    task/<task_name>
  cmux:      <surface-id> (split right of the wiki Claude)

Switch to the right split and continue there. When the task is done —
ask the task Claude for a `## Wiki Summary` block and come back to the
wiki Claude with `/reap <task_name>`.
```

---

## Edge cases

1. **Worktree already exists** — `git worktree add` fails. Report; never silently delete someone else's worktree.
2. **Repo not found after the fallback chain** — ask for the path by hand or offer to cancel.
3. **cmux not responding** — check `cmux ping` or `cmux list-workspaces`. If the cmux daemon is down — ask the user to bring it up.
4. **Duplicate task_name** — `<worktree-path>/.task-cmux-surface` already exists. Report, do not overwrite (suggest `/reap`-ing the old task or picking another name).
5. **Several parallel tasks** — each /dispatch adds another split surface on the right. If 3-4 splits make one workspace cramped — that is a signal to reap the old ones, not to spawn more. cmux does not forbid it, but usability suffers.
6. **Cross-session reap** — the wiki Claude restarted between dispatch and reap (or another session runs /reap). `.task-meta.json.origin_session` != current `$CLAUDE_CODE_SESSION_ID`. Reap must WARN (explicit prompt: "dispatch came from session A 2 hours ago, you are session B — continue filing?") and never file silently. Does not block, but stays visible.
7. **Stale .task-meta.json without .task-cmux-surface** (or vice versa) — the worktree was partially created, dispatch crashed. Reap cannot find the surface for the read-screen fallback or the meta for the origin check. Report, do not guess.
8. **Plan named explicitly but the file is missing / `status:` is not pending** — show today's candidates from `wiki/plans/` (`ls -t wiki/plans/ | head -5`), ask which to take (or classic-mode).
9. **Plan from another session, named explicitly** — valid (cross-window: planned in another window/session). Do not block; just note "plan from another session, named explicitly" in the echo-confirm.

---

## Do not

- Do NOT clone anything without an explicit "yes" from the user.
- Do NOT automatically delete/overwrite an existing worktree.
- Do NOT write into `wiki/hot.md` (log.md only, via vault-write).
- Do NOT try to execute the task from the wiki Claude — that is the task split's job.
- Do NOT create `$WORKTREES_DIR/<repo>-<task_name>` if it already contains a `.git` file (worktree marker).
- Do NOT use `cmux new-workspace` (old paradigm) — a split surface gives the side-by-side view, which is what the user wants.
