---
name: save-plan
description: >
  File a plan from the current conversation into the Obsidian wiki at wiki/plans/.
  Triggers on: "/save-plan", "save this plan", "save plan",
  "запиши план", "сохрани план", "зафайл план", "файлы план в вики".
  Use when the user wants to persist a plan WITHOUT executing it.
  Orthogonal to ExitPlanMode (which is auto-captured by the plan-capture hook).
allowed-tools: Read Bash Glob
---

# save-plan: Save A Discussed Plan To The Wiki

File a discussed plan under this vault's `wiki/plans/` only when it did not use
`ExitPlanMode`; `.claude/hooks/plan-capture.sh` captures that path.

## When to use

Use the explicit triggers in the description. For "save and execute", suggest
plan mode; for a general conversation save, use `/save`.

## Steps

### 1. Identify the plan content

Before metadata, apply the normative language and required-plan-shape contract
from [implementation-plan](../implementation-plan/SKILL.md). If an
implementation plan fails it without an explicit user language override, stop
and ask for an amended approved plan; do not silently translate evidence
identity.

Find the latest coherent plan using its Plan/План heading, steps, goal, risks,
open questions, or checklist. If none exists, ask for it; if several exist, ask
which one to save.

Find the approved Outcome Contract in the same conversation. The page needs
exactly one canonical Outcome Contract JSON block with
`schema_version: 1`, `desired_outcome`, `success_evidence`, and `non_goals`,
plus optional `purpose`, without semantic drift.

Confirm that every success-evidence item is reviewer-observable before the
configured review verdict. Preserve any later evidence under the separately
named `Post-review coordinator acceptance` section outside the canonical block;
do not invent a second Outcome Contract or copy review callback, reap, release,
or terminal-cleanup gates into task success evidence.

If a required field is missing, duplicated, or materially ambiguous, stop and
ask before metadata or writing. Do not infer or invent contract values. An
existing block must match and render once. Do not create a second goal artifact.

Before Step 2, render that block once in the selected plan and validate it with
`scripts/outcome_contract.py`'s `extract_from_plan`. Stop on any JSON, schema,
bounds, or identity rejection; validation precedes address allocation.

### 2. Resolve metadata

Run a single batched `Bash` call to gather:

```bash
echo "session=$(./scripts/current-session-id.sh)"
echo "cwd=$PWD"
echo "ts=$(date '+%Y-%m-%d-%H%M%S')"
echo "date=$(date '+%Y-%m-%d')"
./scripts/allocate-address.sh
```

- `session` — from `./scripts/current-session-id.sh` (`CLAUDE_CODE_SESSION_ID` in Claude Code, `CODEX_THREAD_ID` in Codex); fallback `unknown`.
- `cwd` — current working directory of this Claude session.
- `ts` / `date` — timestamps.
- DragonScale address — last line of stdout (`c-NNNNNN`).

### 3. Derive title + slug

- **title** — first H1/H2 or non-empty line, preserved verbatim.
- **slug** — use the exact code-owned `slug=$(...)` transliteration block in
  `.claude/hooks/plan-capture.sh`: lowercase Cyrillic-to-Latin, ASCII
  alphanumerics/hyphens, 60 characters; empty becomes `untitled-plan`. Only the
  filename is transliterated.

### 4. Compose the page

After the title, render `## Outcome Contract` and the actual approved `json`
block once; then preserve the remaining discussed plan verbatim.

```markdown
---
type: plan
title: "<title>"
address: <c-NNNNNN>
session_id: <session>
sessions:
  - id: <session>
    date: <date>
source_cwd: "<cwd>"
status: pending
created: <date>
updated: <date>
tags:
  - plan
  - manual-save
---

# <title>

## Outcome Contract

<exactly one approved and validated JSON block>

<remaining plan content verbatim, exactly as discussed in chat>
```

### 5. Create through the vault writer

Filename: `wiki/plans/<ts>-<slug>.md` (relative to the project root).

If file already exists (rare same-second collision), append `-1`, `-2`, etc.

Before writing, revalidate final Markdown with `scripts/outcome_contract.py`'s
`extract_from_plan`; stop on rejection. Send one `pages:[{op:"create", ...}]`
payload to `scripts/vault-write.py`, `actor:"save-plan"`, with the full page and
contract in the same successful `vault-write.py` transaction. On collision exit
4, increment the suffix and retry unchanged content. Do not use Write/Edit on
the page directly.

### 6. Confirm to user

One short line:

```
Plan saved → wiki/plans/<filename>
Address: <c-NNNNNN>, session: <session-prefix>...
```

Do NOT update `wiki/log.md` here — plan saves should not flood the operations log; the `plans.base` view in Obsidian indexes them dynamically.

Do NOT update `wiki/hot.md` — plans are not "recent context" worth caching.

## Conventions

- `manual-save` distinguishes this from hook capture; both are `type: plan`.
- Create as `pending`; the user later chooses `executed` or `abandoned`.
- `session_id` supports transcript lookup; Stop owns the scoped commit.

## Edge cases

| Situation | Action |
|---|---|
| No plan discussed in this conversation | Ask user to paste / describe what to save. Do not invent content. |
| Multiple plan candidates | Ask user which one (offer first H1 of each). |
| current session id is `unknown` | Use `manual-<YYYYMMDDHHMMSS>` so the field is never empty. |
| `wiki/plans/` directory missing | Create it (`mkdir -p wiki/plans`) and proceed. |
| `allocate-address.sh` fails | Stop and report; strict schema forbids creating a post-rollout plan without its reserved address. |
| Plan content empty (e.g., user said "save plan" with nothing discussed) | Refuse — ask user to provide plan first. |

## Schema reference

Canonical frontmatter authority is `scripts/vault_schema.py`, enforced by
`scripts/vault-write.py`; Outcome Contract authority is
`scripts/outcome_contract.py`. `wiki/plans/_index.md` is generated listing only.
