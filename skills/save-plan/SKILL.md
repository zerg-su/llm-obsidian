---
name: save-plan
description: >
  File a plan from the current conversation into the Obsidian wiki at wiki/plans/.
  Triggers on: "/save-plan", "save this plan", "save plan",
  "запиши план", "сохрани план", "зафайл план", "файлы план в вики".
  Use when the user wants to persist a plan WITHOUT executing it.
  Orthogonal to ExitPlanMode (which is auto-captured by the plan-capture hook).
allowed-tools: Read Write Bash Glob
---

# save-plan: Save A Discussed Plan To The Wiki

Files a plan that was discussed in the current conversation as a permanent page in `wiki/plans/` (this vault — the current project root). Use when the plan **did not** go through Claude Code's `ExitPlanMode` tool (those are captured automatically by the `.claude/hooks/plan-capture.sh` hook).

The vault is the durable home for plans across sessions.

## When to use

User triggers this with one of:

- `/save-plan`
- "save this plan", "save plan", "save the plan"
- "запиши план", "сохрани план", "зафайл план", "файлы план"
- "save plan to wiki", "файлы план в вики"

**Do not** invoke when user says "save plan and start executing" — that's an ExitPlanMode workflow. Instead, suggest entering plan mode.

**Do not** invoke for general "save this conversation" — that's `/save`.

## Steps

### 1. Identify the plan content

Look back in the conversation for the most recent block that looks like a plan. Markers:

- Headers like `## План:`, `# План`, `## Plan:`
- Numbered "Шаги" / "Steps" sections
- Sections "Цель", "Goal", "Риски", "Risks", "Открытые вопросы"
- A coherent block of markdown with checklist or numbered actions

If multiple candidates exist, ask the user which one to save.

If no obvious plan block exists, ask the user to paste the plan or specify what to save.

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

- **title** — text of the plan's first H1/H2 with `#` symbols stripped, or first non-empty line. Keep human formatting (allow cyrillic, spaces, punctuation). Used in frontmatter and body verbatim.
- **slug** — derived from title by **transliterating cyrillic → latin first** (simplified GOST: а→a, б→b, в→v, г→g, д→d, е→e, ё→yo, ж→zh, з→z, и→i, й→y, к→k, л→l, м→m, н→n, о→o, п→p, р→r, с→s, т→t, у→u, ф→f, х→kh, ц→ts, ч→ch, ш→sh, щ→shch, ъ→drop, ы→y, ь→drop, э→e, ю→yu, я→ya), then lowercase, replace any non-`[a-z0-9 ]` with space, collapse spaces to single `-`, trim leading/trailing `-`, cut to 60 chars. Result is pure latin ASCII (filename-friendly, FS-safe, grep-able). If empty after cleanup → `untitled-plan`. Title in frontmatter retains original cyrillic — only the filename is transliterated.

Reference shell impl (matches `plan-capture.sh` hook for consistency):

```bash
slug=$(printf '%s' "$title" \
  | tr '[:upper:]' '[:lower:]' \
  | sed -E '
      s/ё/yo/g; s/щ/shch/g; s/ю/yu/g; s/я/ya/g; s/ж/zh/g; s/х/kh/g;
      s/ц/ts/g; s/ч/ch/g; s/ш/sh/g; s/й/y/g;
      s/а/a/g; s/б/b/g; s/в/v/g; s/г/g/g; s/д/d/g; s/е/e/g;
      s/з/z/g; s/и/i/g; s/к/k/g; s/л/l/g; s/м/m/g; s/н/n/g;
      s/о/o/g; s/п/p/g; s/р/r/g; s/с/s/g; s/т/t/g; s/у/u/g;
      s/ф/f/g; s/ы/y/g; s/э/e/g;
      s/[ъь]//g;
    ' \
  | sed -E 's/[^a-z0-9 ]+/ /g; s/[[:space:]]+/-/g; s/-+/-/g; s/^-+//; s/-+$//' \
  | cut -c1-60)
```

Example: «Создать hello.txt в текущем каталоге» → `sozdat-hello-txt-v-tekushchem-kataloge`.

### 4. Compose the page

```markdown
---
type: plan
title: "<title>"
address: <c-NNNNNN>
session_id: <session>
source_cwd: "<cwd>"
status: pending
created: <date>
updated: <date>
tags:
  - plan
  - manual-save
---

# <title>

<plan content verbatim, exactly as discussed in chat>
```

### 5. Write the file

Filename: `wiki/plans/<ts>-<slug>.md` (relative to the project root).

If file already exists (rare same-second collision), append `-1`, `-2`, etc.

Use the **Write tool** (not Bash redirect — Write triggers the vault's autocommit hook).

### 6. Confirm to user

One short line:

```
Plan saved → wiki/plans/<filename>
Address: <c-NNNNNN>, session: <session-prefix>...
```

Do NOT update `wiki/log.md` here — plan saves should not flood the operations log; the `plans.base` view in Obsidian indexes them dynamically.

Do NOT update `wiki/hot.md` — plans are not "recent context" worth caching.

## Conventions

- Tag `manual-save` distinguishes manual saves from hook-captured (`ExitPlanMode`-approved). Both have `type: plan`.
- `status: pending` always at creation. User updates manually to `executed` / `abandoned` later via Obsidian.
- `session_id` is the Claude Code session UUID, suitable for grep-lookup in `~/.claude/projects/<encoded-cwd>/sessions/<id>.jsonl` for full transcript.
- Plans are not auto-committed by this skill; the vault's Stop-hook autocommit handles it at the end of the turn.

## Edge cases

| Situation | Action |
|---|---|
| No plan discussed in this conversation | Ask user to paste / describe what to save. Do not invent content. |
| Multiple plan candidates | Ask user which one (offer first H1 of each). |
| current session id is `unknown` | Use `manual-<YYYYMMDDHHMMSS>` so the field is never empty. |
| `wiki/plans/` directory missing | Create it (`mkdir -p wiki/plans`) and proceed. |
| `allocate-address.sh` fails | Write file without `address:` field; tell user; wiki lint will flag it later. |
| Plan content empty (e.g., user said "save plan" with nothing discussed) | Refuse — ask user to provide plan first. |

## Schema reference

Canonical frontmatter schema lives in `wiki/plans/_index.md`. Keep this skill in sync if schema changes (e.g., new required fields).
