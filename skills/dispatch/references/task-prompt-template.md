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
- Review defaults: Claude reviewer `opus`, Codex reviewer `gpt-5.5`
- Commit as you go, as usual. If push is blocked by permission rules — that
  is intended; do not try to bypass it with `--no-verify` or other hacks.
- Commit messages — in this repo's style (`git log --oneline -15` for a sample).

<!-- BRANCH A: rendered ONLY in plan-mode (instead of branch B) -->

## Approved plan (already reviewed — execute)

The plan was shaped and approved in the wiki window: <absolute path to wiki/plans/<file>.md>.

Read it as your FIRST action. Print a short echo (goal + steps, ≤10 lines)
and start executing immediately, WITHOUT waiting for approval — the plan was
already confirmed twice (ExitPlanMode in the wiki window + the dispatch
echo-confirm).

Stop conditions still apply: a fork that materially changes the plan / a new
risk / an out-of-scope item → stop, explain, wait for a decision. Small
tactical choices (variable names, step ordering) — your call.

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

<!-- END BRANCH B -->

## Cross-model review gate

When the task is complete, local checks have passed, and you have done your own
self-review, ask the user:

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
Codex executor -> Claude reviewer (`opus`), Claude executor -> Codex reviewer
(`gpt-5.5`). The reviewer writes `.task-review.md`, invokes
`<review-send-skill>`, and stays open.

When the callback returns:

1. Read `.task-review.md`.
2. Write `.task-review-resolution.md`: mark each finding as `applied`,
   `rejected`, or `out-of-scope`, with a short reason.
3. Apply only clearly correct, in-scope fixes. Ask before changing public
   behavior, data migrations, operational contracts, or anything out of scope.
4. If you applied reviewer findings, call `<review-skill> verify` through the
   skill, or directly:
   `python3 <vault-root>/skills/review-dispatch/scripts/spawn_review.py verify`.
   This sends the implementation back to the SAME reviewer split; do not open a
   new cmux split. In light mode with no applied findings, verify is optional.
5. Wait for `.task-review-verify.md`, evaluate unresolved items, and do your own
   final review with the other model's opinion in mind.
6. After reviewer approve/verify, commit any remaining non-handoff changes. Use
   the repo's normal commit discipline: explicit file list only; do not commit
   `.task-*`, `.wiki-*`, `.review-*`, `.obsidian/workspace*.json`, or UI/runtime
   state. Never push. If there is nothing to commit, record
   `Commit: no changes` in the summary.
7. Show the user a summary: what the other model found, what you applied or
   rejected, checks run, and the commit hash or `Commit: no changes`. Do not
   close the reviewer split yet.
8. Only after the user confirms the result is acceptable, run
   `<review-skill> finish`, then proceed to `/reap-send`.

## Finalization

If the user skipped review or review already passed, invoke `/reap-send`
(Claude) or `<reap-send-skill>` / natural trigger `reap-send` (Codex). It assembles the
`## Wiki Summary` block, writes it to `./.task-summary.md`, and via
`cmux send` triggers `/reap` or the exact command stored in
`./.wiki-reap-command` in the left wiki split — the wiki agent automatically
picks it up and files it into the vault.

The block format:

```
## Wiki Summary

type: <session|decision|runbook|incident|service-update|repo-touch>
title: <the wiki page title, exactly as it will appear in the filename>
session: <your SESSION_ID from <vault-root>/scripts/current-session-id.sh;
          the wiki agent appends it to the provenance of the result page and plan page>

<content in declarative present tense, with [[wikilinks]] to adjacent pages>
```

If cross-model review ran, add one body line:
`Cross-model review: <not run | passed | fixes applied | blocked>`.
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
