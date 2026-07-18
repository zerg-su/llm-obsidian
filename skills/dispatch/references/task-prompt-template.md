# .task-prompt.md template (dispatch Phase 2.2)

The full task-prompt template generated into the worktree by /dispatch. Substitute the <placeholders> from the Phase 1 resolve. `<vault-root>` is the absolute path to this vault repo (the project where /dispatch ran).

**The plan section is conditional** — dispatch renders EXACTLY ONE of the two branches:
- **Branch A "Approved plan"** — if Phase 1.4b produced a plan file (plan-mode);
- **Branch B "plan-first workflow"** — if there is no plan (classic-mode).

Both branches are below in the template body, marked `<!-- BRANCH A -->` / `<!-- BRANCH B -->`.

### 2.2 Generate `.task-prompt.md` in the worktree

```markdown
# Task: <task_name>

## Task description

<description from user, multi-line ok>

## Wiki context (pre-loaded)

- [[<wiki-page-1>]] — <one-line summary>
- [[<wiki-page-2>]] — ...
- [[<wiki-page-3>]] — ...

## Suggested sub-agents (optional, hint)

<this block is generated only if Phase 1.6 found matches. Note: agent_rules
ships empty in this repo, so with 0 matches the whole section is omitted —
that is the default for a fresh install.>

This task falls into the scope of the following specialized sub-agents.
You may delegate audit / deep-dive work to them via the Agent tool — each
has its own context window and focused tool scope:

- Agent("<name1>") — <hint1>
- Agent("<name2>") — <hint2>

A hint, not a command. If the task is simpler than a "full audit" — handle
it yourself without delegation, that is fine.

## Wiki access (read-only, live as you go)

The full knowledge vault lives in `<vault-root>/wiki/`.

When you need extra context during the task — look there:

1. First `<vault-root>/wiki/hot.md` (~500 words of fresh context)
2. Then `<vault-root>/wiki/index.md` (the catalog)
3. Then `<vault-root>/wiki/<domain>/_index.md`
   (concepts/, entities/, sources/, questions/, runbooks/, decisions/, ...)
4. Only then — individual pages.

Page names are unique across the vault; search via Glob/Grep:
  `Glob <vault-root>/wiki/**/<name>*.md`

Do not read the wiki for general programming questions — only when you need
vault-specific context (prior decisions, research notes, runbooks).

**READ-ONLY.** Do not Edit/Write anything under `<vault-root>/`.
Any vault change goes through the wiki agent (`/reap` after your summary)
— otherwise you race the vault's Stop-hook autocommit.

## Working rules

- CWD: <worktree-path>
- Target repo: <repo-path>
- Base branch: <base-branch>, your branch: task/<task_name>
- Codex skill environment: <codex-home/profile or inherited>
- Wiki reap RPC: <wiki-reap-command>
- Review gate: <review-skill>
- Review send: <review-send-skill>
- Review defaults come from `config/model-routing.toml`; explicit task
  overrides are preserved
- Commit as you go, as usual. Never push, deploy, publish, delete the worktree
  or branch, or expand scope under this task mandate.
- Commit messages — in this repo's style (`git log --oneline -15` for a sample).

<!-- BRANCH A: rendered ONLY in plan-mode (instead of branch B) -->

## Approved plan (already reviewed — execute)

The plan was shaped and approved in the wiki window: <absolute path to wiki/plans/<file>.md>.

Read it as your FIRST action. Print a short echo (goal + steps, ≤10 lines)
and start executing immediately, WITHOUT waiting for approval — the plan was
already confirmed twice (ExitPlanMode in the wiki window + the dispatch
echo-confirm).

Read `.task-meta.json` and validate it with
`python3 <vault-root>/scripts/task_contract.py validate`. When
`interaction_policy=unattended`, this approval is the complete execution
mandate: do not ask again for plan, review, verify, finish, or reap-send.
Treat `.task-meta.json` as read-only; changing its policies is contract drift.

For unattended tasks, `watchdog_policy` observes the task and reviewer cmux
viewports. It only sends staged coordinator notifications after prolonged
visible inactivity; it never sends you input, cancels work, or closes a surface.
The supervisor may press Enter once only when the exact native Claude/Codex
workspace-trust dialog appears on this approved task surface during startup.
It does not answer any other prompt.

Stop conditions still apply: a fork that materially changes the plan / a new
risk / an out-of-scope item → call `python3 <vault-root>/scripts/task_escalation.py
raise --category <category> --reason "..." --question "..."`, then remain
paused until the coordinator relays a decision. Never wait on a permission
prompt in this background pane. Small tactical choices are your call.

If a repository-owned pipeline script, hook, skill, instruction, schema,
callback, or runtime adapter appears defective, contain the state and perform
read-only diagnosis only. Then raise category `mechanism-failure`; state the
failed stage, whether the intended mutation started/rolled back/completed/is
pending recovery, and request coordinator classification. Remain paused. Do
not self-repair or use repeated retries. The coordinator may authorize a narrow
repo-owned/local/reproducible/reversible repair automatically, but must ask the
user for any permission, dependency, security, public-interface, migration,
destructive, external-effect, scope, or ambiguous-state boundary. Resume only
after the coordinator relays its explicit resolution. Follow
`<vault-root>/docs/skill-references/failure-repair-contract.md`.

For a Claude executor in `auto` mode, treat the first classifier denial as a
`permission` escalation: do not retry the blocked action. Repeated denials can
make Claude fall back to interactive prompts, so one denial must pause and
relay to the coordinator immediately.

The plan file in the vault is read-only — do NOT edit it; record deviations
from the plan in your final ## Wiki Summary.

<!-- END BRANCH A -->

<!-- BRANCH B: rendered ONLY in classic-mode (instead of branch A) -->

## IMPORTANT: plan-first workflow

**Your first and only action on start — show the user a work plan and wait
for their confirmation.** Before approval do NOT:

- any Edit / Write / NotebookEdit;
- any git commands with side effects (`git checkout -b`, `git commit`, `git stash`);
- any state-changing MCP / Bash actions (create/update/delete calls, deploys,
  publications to external systems).

What IS allowed without approval:
- Read of any files in the target repo, the worktree, the vault (read-only).
- Bash for purely read-only commands: `git log/status/diff`, `ls`, `find`, `grep`,
  `git -C <repo> branch --list`, dry-run / `--check` / `--diff` modes.
- Read-only MCP calls (get/list/search).
- TaskCreate for your own tracking (optional).

After gathering context (usually 3-7 read-only calls) — **stop and publish
the plan in a single message** in this format:

```
## Plan: <task_name>

**Goal**: <one line>

**Steps**:
1. <concrete step, the files we will touch, the commands we will run>
2. ...
N. <final step including smoke-test / verification>

**Open questions**:
- <question for the user if any, otherwise "none">

**Risks / out of scope**: <short list>

Ready to start? If something needs adjusting — say so.
```

Then **wait for an explicit "yes / go / ok / start" from the user**.
"Good" / "interesting" / "I see" is NOT approval — it is a reaction; keep waiting.

After approval — work as usual, commit as you go, then use the cross-model
review gate below before `/reap-send`.

If a fork appears mid-work that materially changes the plan (a new
out-of-scope item, an unexpected risk, a big design choice) — stop, explain,
wait for a new decision. Small tactical choices (variable names, step order
within one task) — your call.

If the fork is a probable defect in a repository-owned pipeline mechanism,
apply `<vault-root>/docs/skill-references/failure-repair-contract.md`: contain
and diagnose read-only, then classify it against the coordinator auto-repair
boundary. Repair routine repo-owned/local/reproducible/reversible defects in
scope; ask once when permissions, dependencies, security, public interfaces,
migrations, destructive actions, external effects, or ambiguous state are
involved. Then regression-test, re-run the failed stage, and resume from the
last safe boundary.

<!-- END BRANCH B -->

## Cross-model review gate

Read `interaction_policy` and `review_policy` from `.task-meta.json`.

For `interactive`, keep the compatibility gate after implementation:

```text
Implementation done. Run cross-model review before reap-send? Default: light. Options: light / full / skip.
```

If the user answers simply "yes / ok / run review", run light mode:
`<review-skill>` with `--light` through the skill, or directly:
`python3 <vault-root>/skills/review-dispatch/scripts/spawn_review.py start --light`.
If the user explicitly asks for full/deep/high-risk review, public API review,
security-sensitive review, or operationally risky review, run full mode without
`--light`.

The reviewer opens in a neighboring cmux split on the opposite model:
Codex executor -> central Claude default, Claude executor -> central Codex
default. The reviewer
writes `.task-review.md`, invokes
`<review-send-skill>`, and stays open.

For `unattended`, start the configured `light|full` review automatically. Do
not ask the user unless the deterministic review action is `escalate`.
The reviewer watchdog follows the same observer-only policy and stops with the
reviewer process.

When the callback returns:

1. Read `.task-review.md`.
2. Write `.task-review-resolution.md`: mark each finding as `applied`,
   `rejected`, or `out-of-scope`, with a short reason.
3. Run `python3 <vault-root>/scripts/task_contract.py review-action --review
   <active-review-json> --iteration <completed-verify-count>`. For `resolve`,
   apply clearly correct in-scope fixes or reject with evidence. For
   `escalate`, use `task_escalation.py raise` and pause: blocking findings,
   public behavior, migrations, security boundaries, external effects, scope
   changes, or exhausted verify iterations always require the user.
4. After any `changes-requested` resolution, call `<review-skill> verify` through the
   skill, or directly:
   `python3 <vault-root>/skills/review-dispatch/scripts/spawn_review.py verify`.
   This sends the implementation back to the SAME reviewer split; do not open a
   new cmux split. Obey the configured `max_verify_iterations` limit.
5. Wait for `.task-review-verify.md`, evaluate unresolved items, and do your own
   final review with the other model's opinion in mind.
6. After reviewer approve/verify, commit any remaining non-handoff changes. Use
   the repo's normal commit discipline: explicit file list only; do not commit
   `.task-*`, `.wiki-*`, `.review-*`, `.obsidian/workspace*.json`, or UI/runtime
   state. Never push. If there is nothing to commit, record
   `Commit: no changes` in the summary.
7. In `interactive`, show the result and wait for acceptance. In `unattended`,
   an `approve` verdict authorizes `<review-skill> finish` immediately; its
   close-on-exit wrapper closes only the reviewer surface after process return.
   `finish` archives directly only in coordinator context; from this task
   worktree it leaves a typed archive request for coordinator `/reap`.
8. Proceed to `/reap-send` after finish. Never push, deploy, publish, delete a
   worktree/branch, or expand scope under the unattended mandate.

## Finalization

If review already passed, invoke `/reap-send` (Claude) or `<reap-send-skill>` /
natural trigger `reap-send` (Codex). It assembles the
typed summary, validates `./.task-summary.json`, renders the legacy
`./.task-summary.md` view, and via
`cmux send` triggers `/reap final` in unattended mode (plain `/reap` in
interactive mode) or the exact command stored in
`./.wiki-reap-command` in the left wiki split — the wiki agent automatically
picks it up and files it into the vault.

The canonical JSON format:

```json
{{
  "schema_version": 1,
  "type": "session|decision|runbook|incident|service-update|repo-touch",
  "title": "the exact wiki page title",
  "session": "SESSION_ID from <vault-root>/scripts/current-session-id.sh",
  "body": "declarative Markdown with [[wikilinks]]"
}}
```

If cross-model review ran, add one `body` line:
`Cross-model review: <not run | passed | fixes applied | blocked>`.
Coordinator `/reap` appends the exact `Review archive: [[...]]` link after it
files the validated review history; do not invent that title in the task split.
If a post-review commit was created, add:
`Commit: <hash> <message>`; if everything was already committed,
`Commit: no changes`.

Types:
- `session` — general summary of what the task accomplished, filed in `wiki/meta/sessions/`
- `decision` — an architectural decision was made, filed in `wiki/decisions/`
- `runbook` — a step-by-step procedure, filed in `wiki/runbooks/`
- `incident` — a post-mortem, filed in `wiki/incidents/`
- `service-update` — updated the status of an existing service page, target `wiki/services/<title>.md`
- `repo-touch` — updated a repo page (or creates a stub), target `wiki/repos/<title>.md`

Fallback: if `/reap-send` / `<reap-send-skill>` is unavailable — just print the
block into the chat as markdown. The user switches to the wiki split and says
the command from `./.wiki-reap-command` by hand, with the task name appended;
the wiki agent reads it via `cmux read-screen --surface <id>` from `.task-cmux-surface`.
```

(Generate via the Write tool, not echo through Bash — the template is long.)
