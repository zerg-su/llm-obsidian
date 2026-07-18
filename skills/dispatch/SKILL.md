---
name: dispatch
metadata:
  version: 1.2.0
description: Spawn an isolated Claude/Codex task worktree in cmux and hand it an approved plan; requires cmux.
allowed-tools: Read Write Edit Glob Grep Bash AskUserQuestion
---

# /dispatch — spawn a parallel task in cmux
Keeps the "wiki as backbone" paradigm but moves execution into a separate cmux task split. The wiki agent (this session, Claude or Codex) does **not** execute the task — it spawns a task split and keeps dispatching other tasks in parallel. When the task is done — `/reap` it into the wiki.

The prompt may select either runtime; otherwise dispatch inherits the current
session route. Claude and Codex can dispatch each other.

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
- `runtime` — `claude | codex`. From text: "for Codex / для кодекса" → `codex`; "for Claude / для клода" → `claude`. Default = current agent (`CODEX_THREAD_ID` → `codex`, otherwise `claude`).
- `model` / `effort` — resolve through `scripts/model_routing.py`. The default
  is the exact current-session route; a named model or effort remains an
  explicit per-run override. Unknown models without an explicit runtime fail.
- `plan_ref` — approved-plan handoff mode (resolved in Phase 1.4b): nothing said → auto-resolve the latest plan of the current session; "hand off plan <slug|path>" → that file; "no plan" → classic-mode forced.
- `interaction_policy` — approved-plan dispatch defaults to `unattended`;
  explicit "interactive" preserves compatibility gates.

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

### 1.2b Resolve the Codex skill environment

For `runtime=codex`, do **not** rely on the parent process' `CODEX_HOME`.
Installed Codex plugins are scoped to `CODEX_HOME`, so inheriting the wrong home
mixes work and personal skills. Resolve the task agent's Codex environment
before echo-confirm:

1. Prefer `<repo-path>/.codex/dispatch-env.toml` if the target repo provides it.
2. Otherwise use `<vault-root>/.codex/dispatch-env.toml` from the repo where
   `/dispatch` is running.
3. If neither exists, inherit the current `CODEX_HOME` only as a fallback and
   mark it clearly in the echo-confirm.

Supported TOML shape:

```toml
[codex_dispatch]
codex_home = "~/.codex"
profile = "llm-obsidian-mcp"
reap_skill = "$llm-obsidian:reap"
reap_send_skill = "$llm-obsidian:reap-send"
review_skill = "$llm-obsidian:review-dispatch"
review_send_skill = "$llm-obsidian:review-send"
# Model and effort defaults live only in config/model-routing.toml.
interaction_policy = "unattended"
review_mode = "light"
max_verify_iterations = 2
auto_close_surfaces = true
default_reap_type = "session"
watchdog_enabled = true
watchdog_poll_seconds = 30
watchdog_warn_after_seconds = 900
watchdog_alert_after_seconds = 1200
```

- `codex_home` is expanded with `~` and must already exist before spawning.
- `profile` is optional and is passed as `--profile <name>` inside that
  `CODEX_HOME`.
- Reap/review command keys are persisted into `.wiki-reap-command` and
  `.task-*-skill` handoffs so neither runtime guesses plugin namespaces.
- Reviewer defaults come from `config/model-routing.toml`; explicit task
  metadata or CLI overrides remain authoritative.
- The remaining keys define bounded review/reap/close plus an observer-only
  15/20-minute watchdog; old metadata without the policy stays disabled.

If `codex_home` is configured but the directory is missing — stop before
creating the split and tell the user to bootstrap that Codex home. Do not create
or install plugins into a Codex home implicitly during dispatch.

### 1.2c Resolve and snapshot the child route
Before echo-confirm, refresh the current session snapshot with
`model_routing.py capture-session`, then resolve `dispatch` for
`$(./scripts/current-session-id.sh)`. Codex reads only model fields from that
exact local session record; other hosts provide them through hook/env fields.
If unavailable, capture host-visible values; never guess. Store session and
effective results in `.task-meta.json.routing` and pass explicit overrides to
the resolver so `source` records them.
### 1.3 Resolve the branch

- An existing branch was named → `git -C <repo> branch --list <branch>` to verify. Not local — `git -C <repo> branch -a --list "*<branch>*"` to check remotes. Not anywhere — ask.
- `new` or unspecified → `task_name` → branch name `task/<task_name>`. Base — `git -C <repo> symbolic-ref refs/remotes/origin/HEAD | sed 's|^refs/remotes/origin/||'` (usually `main` or `master`).

### 1.4 Collect wiki context

Find 3-5 relevant pages for the pre-prompt:

1. `wiki/hot.md` — Read, extract mentions of the target repo / task topic.
2. `Glob wiki/**/*.md` by keywords from the description (via Grep `-l`).
3. `wiki/repos/<repo>.md` if it exists.

Do not chase exhaustive context — 3-5 [[links]] with a one-line description each is enough. The task agent will read the rest itself via live vault access.

### 1.4b Resolve the approved plan (plan-mode vs classic-mode)

Every approved plan is auto-saved by the plan-capture hook (`.claude/hooks/plan-capture.sh`) into `wiki/plans/<TS>-<slug>.md` (`type: plan`, `session_id:`, `status: pending`). Target workflow: the wiki agent gathers context and shapes the plan; the split executes — the plan is handed off as a file, no re-planning.

Default — auto-resolve the latest plan of the current session:

```bash
SESSION_ID=$(./scripts/current-session-id.sh)
PLAN_FILE=$(grep -l "session_id: $SESSION_ID" wiki/plans/*.md 2>/dev/null | sort | tail -1)
```

(file names start with a timestamp, so sort by name = sort by time).

- Found → **plan-mode**: hash the plan and write v2 unattended metadata.
- Not found → do not spawn by default. Shape and approve the plan in the
  coordinator, then file it with `/save-plan` (or the runtime equivalent).
  Only explicit `interactive` / `no plan` uses classic-mode v1.
- The user explicitly named a plan → take it from `wiki/plans/`, even if it belongs to another session (cross-window is valid — note it in the echo).
- "No plan" → classic-mode forced.

### 1.6 Resolve sub-agents for the task scope

`/dispatch` does **not** pick agents inside the task split itself — that would duplicate the router hook's logic. Instead: read `.claude/skill-rules.json` (single source of truth, already used by the UserPromptSubmit hook), match `description` against `agent_rules.patterns`, pick the top-2 candidates by match count.

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

The result — 0-2 lines of `<agent_name>\t<hint>`. Goes into `.task-meta.json` (see Phase 2.3) and is mentioned in the pre-prompt as "Suggested agents for deep audit-type work". The task agent **may** delegate to them via the Agent tool if the runtime supports it, it is **not obliged** — a hint, not a command.

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
  runtime:     codex (explicit)
  model:       gpt-5-codex
  codex home:  ~/.codex
  codex profile: llm-obsidian-mcp
  wiki reap:   $llm-obsidian:reap
  review:      $llm-obsidian:review-dispatch
  review send: $llm-obsidian:review-send
  interaction: unattended (one approved mandate)
  review policy: light, max verify 2; blocking escalates
  reap policy: final session → "<approved result title>"
  surfaces:    close reviewer/task after armed process exit
  watchdog:    observe every 30s; notify at 15m/20m; never interrupt
  plan:        wiki/plans/2026-07-03-113935-<slug>.md (approved in this session)
  wiki context:
    - [[Blog]] — main page about the blog setup
    - [[Dark Mode Research]] — notes from an earlier comparison
    - [[CSS Conventions]] — styling rules used across projects
Spawn the task split?
```

The plan, exact result type/title, review depth, forbidden actions, and surface
policy are part of this single approval. A wrong target is corrected here
instead of interrupting the background task later.

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
- the **vault is read-only** for the task agent — vault edits happen only via /reap (otherwise a race with the vault's Stop-hook autocommit);
- **Suggested sub-agents** — a hint, not a command;
- finalization — `/reap-send`, fallback = print the `## Wiki Summary` block into the chat.

### 2.3 Spawn a split surface in the current workspace

For Codex, run `codex-sync --apply --only-profile <dispatch-profile>` after the
upfront approval and before `new-split`. The scoped sync unions only already
trusted global hook hashes into that profile, never changes other Codex
profiles, and never accepts a new/conflicting hash.
If Codex still presents hook trust, stop at startup and surface that single new
trust decision immediately instead of letting the task appear active.

Paradigm: the task agent lives as a **split surface to the right** of the wiki agent in the very same cmux workspace, **not** in a separate workspace or window. The user sees both sessions at once, switches between them as panes, and parallel tasks accumulate as extra surfaces in the same split stack.

**First** — capture the wiki surface UUID **before** the new-split (in case focus shifts). Do not persist short `surface:N` refs in handoff files: refs are convenient for humans, but UUIDs are safer when tabs/surfaces are moved or closed.

```bash
CMUX_UUID_RE='[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}'
SURFACES=$(cmux --id-format both list-pane-surfaces 2>&1)
WIKI_SURFACE=$(printf '%s\n' "$SURFACES" | awk '/^\*/ {print; exit}' | grep -oE "$CMUX_UUID_RE" | head -1)
WIKI_SURFACE_REF=$(echo "$SURFACES" | awk '/^\*/ {print $2; exit}')
```

The `*` prefix marks the selected surface — that is the wiki agent (we are in it right now). If the UUID cannot be determined — report and stop (the RPC mode in /reap-send does not work without it). `WIKI_SURFACE_REF` is only for human-readable echo/log.

```bash
SURFACE_LINE=$(cmux --id-format both new-split right --focus false 2>&1)
# Output can be "OK surface:N <UUID> ..." or "OK surface:N (<UUID>) ...".
# Persist UUID, ref only for echo/log.
SURFACE_ID=$(printf '%s\n' "$SURFACE_LINE" | grep -oE "$CMUX_UUID_RE" | head -1)
SURFACE_REF=$(echo "$SURFACE_LINE" | grep -oE 'surface:[0-9]+' | head -1)
```

If `cmux new-split` fails (no active workspace / pane cannot split) — report and stop. Do not fall back to `new-workspace`.

Save **both** surface UUIDs into the worktree (backwards-compat with reap / reap-send; `cmux --surface` accepts UUIDs):

```bash
WIKI_RUNTIME=claude
[ -n "${CODEX_THREAD_ID:-}" ] && WIKI_RUNTIME=codex
echo "$SURFACE_ID" > <worktree-path>/.task-cmux-surface    # task agent (right split)
echo "$WIKI_SURFACE" > <worktree-path>/.wiki-cmux-surface   # wiki agent (left split, for /reap-send RPC)
echo "$WIKI_RUNTIME" > <worktree-path>/.wiki-agent-runtime  # claude|codex, for the right RPC command
echo "<reap_skill without task argument>" > <worktree-path>/.wiki-reap-command
echo "<reap_send_skill>" > <worktree-path>/.task-reap-send-skill
echo "<review_skill>" > <worktree-path>/.task-review-skill
echo "<review_send_skill>" > <worktree-path>/.task-review-send-skill
```

`.task-cmux-surface` is needed by `/reap` (wiki-side fetch via read-screen, fallback when `.task-summary.md` is missing).
`.wiki-cmux-surface` and `.wiki-agent-runtime` are needed by `/reap-send` (task-side RPC trigger: Claude receives `/reap`; Codex receives the command stored in `.wiki-reap-command`).
`.wiki-reap-command` is the environment-specific Codex skill command to send
back to the wiki split, for example `$llm-obsidian:reap`. This avoids
guessing from `WIKI_RUNTIME=codex`, which is not enough to distinguish plugin
namespaces.
`.task-review-skill` stores the environment-specific review command, for
example `$llm-obsidian:review-dispatch`, so the task agent can run
cross-model review before `/reap-send`.
`.task-review-send-skill` stores the reviewer-side callback command, for
example `$llm-obsidian:review-send`, so the review split can return
findings to the executor split.
`.task-agent-command.json` is the shell-free argv/env handoff consumed by the
cmux supervisor; it is runtime state, never a commit target.

**Additionally** — approved-plan mode writes v2 `.task-meta.json` with the
origin binding and unattended mandate. Classic interactive mode keeps v1.

```bash
cat > <worktree-path>/.task-meta.json <<EOF
{
  "version": 2,
  "task_name": "<task_name>",
  "wiki_runtime": "$WIKI_RUNTIME",
  "executor_runtime": "<claude|codex>",
  "runtime": "<claude|codex>",
  "origin_session": "$(./scripts/current-session-id.sh)",
  "spawned_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "wiki_surface": "$WIKI_SURFACE",
  "wiki_surface_ref": "$WIKI_SURFACE_REF",
  "task_surface": "$SURFACE_ID",
  "task_surface_ref": "$SURFACE_REF",
  "target_repo": "<repo-path>",
  "vault_root": "<absolute coordinator vault path>",
  "branch": "task/<task_name>",
  "base_branch": "<base-branch>",
  "codex_home": "<absolute CODEX_HOME path or null>",
  "codex_profile": "<profile name or null>",
  "wiki_reap_command": "<reap_skill without task argument>",
  "reap_send_skill": "<reap_send_skill>",
  "review_skill": "<review_skill>",
  "review_send_skill": "<review_send_skill>",
  "routing": {
    "schema_version": 1,
    "session": {"runtime": "<current>", "model": "<current>", "effort": "<current>"},
    "effective": {"runtime": "<resolved>", "model": "<resolved>", "effort": "<resolved>", "source": ["session"], "config_sha256": "<sha256>"}
  },
  "model": "<explicit override only, otherwise omit>",
  "effort": "<explicit override only, otherwise omit>",
  "plan_file": "/abs/path/to/wiki/plans/<file>.md",
  "approved_plan_sha256": "<sha256 captured before echo-confirm>",
  "interaction_policy": "unattended",
  "review_policy": {
    "mode": "light|full|skip",
    "max_verify_iterations": 2,
    "auto_resolve_severities": ["warning", "nit"],
    "escalate_severities": ["blocking"]
  },
  "reap_policy": {
    "mode": "final",
    "auto_file": true,
    "allowed_types": ["session"],
    "title": "<exact approved wiki title>"
  },
  "surface_policy": {"auto_close": true},
  "watchdog_policy": {"enabled": true, "poll_seconds": 30, "warn_after_seconds": 900, "alert_after_seconds": 1200},
  "forbidden_actions": ["push", "deploy", "publish", "delete-worktree", "delete-branch", "expand-scope"],
  "suggested_agents": [<from Phase 1.6, JSON array of {"name", "hint"}>]
}
EOF
```

Why `.task-meta.json`:
- `origin_session` — `/reap` compares it with current `./scripts/current-session-id.sh`; on mismatch (the wiki agent restarted between dispatch and reap, or another session runs reap) — WARNING and an explicit confirm before filing. Does not block, but stays visible. See Edge case 6.
- `vault_root` — binds review archives, callbacks, lifecycle telemetry, and reap
  to the canonical coordinator vault even when this repository reviews a linked
  worktree copy of itself. Older v2 metadata derives the same root from
  `plan_file`; script-location inference is only a legacy fallback.
- `wiki_runtime` and `wiki_reap_command` — `/reap-send` selects the correct RPC command: `/reap` for Claude, or the plugin-qualified command stored in `.wiki-reap-command` for Codex.
- `executor_runtime` / `runtime` / `model` — `/reap` preserves executor provenance in result-page frontmatter and echo output (`runtime` stays as a legacy alias for old consumers).
- `suggested_agents` — `/reap` can add them to the saved page's frontmatter or use them for cross-refs.
- `approved_plan_sha256` prevents post-approval drift; validate it with
  `scripts/task_contract.py validate` before task work.
- `reap_policy` binds unattended filing to one exact type/title; drift,
  collision, or session mismatch escalates instead of filing.
- `watchdog_policy` only observes/alerts; it never sends input, kills, or closes.

Prepare the shell-free task argv, then send only the short supervisor command.
`prepare-task` pins Codex to `-a never -s workspace-write` and adds only the
validated Git common directory, enough for linked-worktree commits. Its proxy allows client
connections only to `localhost`, `127.0.0.1`, `::1`, and the exact user-owned
cmux Unix socket from `CMUX_SOCKET_PATH`; loopback binding is allowed, while
external domains, non-loopback proxying, upstream proxies, arbitrary Unix
sockets, and SOCKS remain disabled. The
supervisor pins a trusted `PATH` with Python, Homebrew, Docling, Git, cmux,
Claude, and Codex, plus the standalone task profile through `DCG_CONFIG`.
That profile permits local rebase/amend/cherry-pick/staging but blocks push,
hard reset, file discard, worktree/branch deletion, repository-wide history
rewriting, deployment, publication, and the other destructive packs.
Claude keeps `auto` while using the same trusted `PATH` and task DCG profile.
The supervisor validates the command/environment contract, refuses drift,
appends one prompt argv, runs the watchdog, and calls lifecycle after exit.

```bash
WORKTREE="<worktree-path>"
SUPERVISOR="<vault-root>/scripts/cmux_agent_supervisor.py"
WORKTREE_Q=$(printf '%q' "$WORKTREE")
SUPERVISOR_Q=$(printf '%q' "$SUPERVISOR")
python3 "$SUPERVISOR" prepare-task --worktree "$WORKTREE" --surface "$SURFACE_ID"
cmux send --surface "$SURFACE_ID" "python3 $SUPERVISOR_Q run --worktree $WORKTREE_Q --kind task --surface $SURFACE_ID"
sleep 0.2
cmux send-key --surface "$SURFACE_ID" Enter
```

### 2.4 Verify

```bash
cmux read-screen --surface "$SURFACE_ID" --lines 1 >/dev/null
```

The surface must be addressable by its persisted UUID. Optionally, after 1-2 seconds:

```bash
cmux read-screen --surface "$SURFACE_ID" --lines 20
```

If the output shows a running `claude` or `codex` process — success.

---

## Phase 3: Log (vault-write, log-only)

Payload goes through the dispatcher script (NOT a direct Edit):

```bash
python3 scripts/vault-write.py <<'PAYLOAD'
{"log_entry": "## [YYYY-MM-DD HH:MM] dispatch | <task_name>\n\nSpawned a task split (cmux `<surface-id>`, runtime <claude|codex>, model <model-or-default>) to the right of the wiki agent in worktree `<worktree-path>` for: <one-line description>. Target repo <[[repo-page]] or path>, branch `task/<task_name>` from `<base-branch>`. Plan: <`wiki/plans/<file>.md` handed to the split | none (plan-first in the split)>. Pre-loaded context: [[X]], [[Y]], [[Z]]. Surface ID in `<worktree>/.task-cmux-surface` for /reap. Awaiting `## Wiki Summary`, then `/reap`."}
PAYLOAD
```

Do **not** touch `wiki/hot.md` — dispatches are not save events and must not clutter Recent Changes. Only the result lands in hot.md, via /reap.

### Final message to the user

```
Spawned task split:
  worktree:  <worktree-path>
  branch:    task/<task_name>
  cmux:      <surface-id> (split right of the wiki agent)

The approved task now runs unattended. It returns only for a blocking/scope
escalation; otherwise review, verify, final reap, and exact task/reviewer
surface closure happen automatically. Branch and worktree remain local.
```

---

## Edge cases

1. **Worktree already exists** — `git worktree add` fails. Report; never silently delete someone else's worktree.
2. **Repo not found after the fallback chain** — ask for the path by hand or offer to cancel.
3. **cmux not responding** — check `cmux ping` or `cmux list-workspaces`. If the cmux daemon is down — ask the user to bring it up.
4. **Duplicate task_name** — `<worktree-path>/.task-cmux-surface` already exists. Report, do not overwrite (suggest `/reap`-ing the old task or picking another name).
5. **Several parallel tasks** — each /dispatch adds another split surface on the right. If 3-4 splits make one workspace cramped — that is a signal to reap the old ones, not to spawn more. cmux does not forbid it, but usability suffers.
6. **Cross-session reap** — the wiki agent restarted between dispatch and reap (or another session runs /reap). `.task-meta.json.origin_session` != current `./scripts/current-session-id.sh`. Reap must WARN (explicit prompt: "dispatch came from session A 2 hours ago, you are session B — continue filing?") and never file silently. Does not block, but stays visible.
7. **Stale .task-meta.json without .task-cmux-surface** (or vice versa) — the worktree was partially created, dispatch crashed. Reap cannot find the surface for the read-screen fallback or the meta for the origin check. Report, do not guess.
8. **Plan named explicitly but the file is missing / `status:` is not pending** — show today's candidates from `wiki/plans/` (`ls -t wiki/plans/ | head -5`), ask which to take (or classic-mode).
9. **Plan from another session, named explicitly** — valid (cross-window: planned in another window/session). Do not block; just note "plan from another session, named explicitly" in the echo-confirm.

---

## Do not

- Do NOT clone anything without an explicit "yes" from the user.
- Do NOT automatically delete/overwrite an existing worktree.
- Do NOT write into `wiki/hot.md` (log.md only, via vault-write).
- Do NOT try to execute the task from the wiki agent — that is the task split's job.
- Do NOT create `$WORKTREES_DIR/<repo>-<task_name>` if it already contains a `.git` file (worktree marker).
- Do NOT use `cmux new-workspace` (old paradigm) — a split surface gives the side-by-side view, which is what the user wants.
