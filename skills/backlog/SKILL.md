---
name: backlog
version: 1.1.0
description: >-
  Append-only capture inbox wiki/backlog.md, one line per item. Modes: add / list (>60d WARN) / promote (→ /save as a goal/question/decision page, or drop). Not for: content that already deserves a full wiki page (/save directly). Triggers: не забыть, в TODO, бэклог, backlog add/list/promote, remind me to, было бы хорошо.
allowed-tools: Read Write Edit Glob Grep Bash AskUserQuestion
---

# /backlog — capture / list / promote

Lightweight capture inbox. Single file `wiki/backlog.md`, append-only, one line per item. Three modes: `add` / `list` / `promote`.

## Input

```
/backlog add <natural-language description>
/backlog list [filter]
/backlog promote <slug>
/backlog                       # no argument — pre-flight asks which mode
```

If the argument contains an explicit `add` / `list` / `promote` — the mode is pre-resolved. Otherwise infer from natural language (contains a slug? promote. Contains "show me"? list. Default — add).

---

## Mode: add

### Phase A.0: Pre-flight (lightweight)

At most 2 questions (skip any that are already clear from the prompt):

1. **Slug suggestion** — Claude generates a kebab-case slug from the summary (`blog-theme-upgrade`), user confirms / overrides.
2. **Context link** — a related wiki page (`[[<page>]]`) or an external URL, if any. Optional.

NO scope/audience/mode questions — this is lightweight capture, not a write-heavy skill.

### Phase A.1: Parse

```
input: "remind me to upgrade the blog theme to v3 when I have time"
       ↓
slug: blog-theme-upgrade
summary: upgrade the blog theme to v3
context: (optional) — Claude greps wiki/ for a likely related page
```

### Phase A.2: Append

```bash
echo "- [$(date +%Y-%m-%d)] <slug> — <summary> — context: <link or ->" >> wiki/backlog.md
```

Or via Edit (append mode):

```markdown
- [2026-05-23] blog-theme-upgrade — upgrade the blog theme to v3 — context: [[Blog]]
```

Format is strict — the list-mode parser depends on it:
- Date in `[YYYY-MM-DD]` brackets.
- Slug in kebab-case.
- Em-dash separator (` — `) between slug / summary / context.
- Context link is either `[[<page>]]`, or a URL, or `-` if none.

### Phase A.3: Log

```markdown
# wiki/log.md (prepend on top)
## [YYYY-MM-DD HH:MM] backlog | add — <slug>
```

Confirm in chat: `✓ added <slug>: <summary>` (one line).

---

## Mode: list

### Phase B.1: Read

```python
Read('wiki/backlog.md')
```

### Phase B.2: Filter

If a filter argument is supplied — match by:
- keyword in slug / summary / context (case-insensitive)
- age range (`> 30d`, `< 7d`)
- context link (`context:reading` — items linked to reading-related pages)

If no filter — show all.

### Phase B.3: Render

Table format (markdown):

```markdown
## Backlog (N items)

| Date | Slug | Summary | Context | Age |
|---|---|---|---|---|
| 2026-05-23 | blog-theme-upgrade | upgrade the blog theme to v3 | [[Blog]] | 0d |
| 2026-05-20 | vector-db-notes | write up vector database research | - | 3d |
| 2026-03-12 | backup-rotation | set up offsite backup rotation | [[Backups]] | ⚠ 72d |
```

Anti-rot: mark items with `age > 60d` with a `⚠` suffix + footer:

```
⚠ 1 item > 60 days. Consider: /backlog promote <slug> OR /backlog drop <slug>.
```

NEVER auto-drop. The user decides explicitly.

---

## Mode: promote

### Phase C.0: Pre-flight (mandatory — AskUserQuestion)

Where to promote — single question, 4 options:

1. **Wiki goal** — chain into `/save type=goal <slug>`. Pre-loads summary + context.
2. **Wiki question** — chain into `/save type=question <slug>`.
3. **Wiki decision** — chain into `/save type=decision <slug>`.
4. **Drop** — the item is no longer relevant; remove from the backlog without filing.

### Phase C.1: Read + locate item

```bash
grep -n "^- \[[0-9-]+\] <slug>" wiki/backlog.md
```

If the slug is not found — error, suggest `/backlog list` to see the current state.

### Phase C.2: Chain

For wiki targets → invoke `/save` with the scope pre-loaded from the backlog summary + context.

After a successful chain completion (wiki page filed):

### Phase C.3: Remove from backlog

```python
Edit('wiki/backlog.md', old=<line>, new='')  # remove line
```

Prepend to `wiki/log.md`:

```markdown
## [YYYY-MM-DD HH:MM] backlog | promote — <slug> → <target>
```

Where `<target>` = `[[<wiki-page>]]` / `drop`.

NEVER duplicate state: after promote the item disappears from the backlog. To find what happened to it — `wiki/log.md` keeps the history.

---

## File format reference

`wiki/backlog.md` — single-file canonical inbox. Frontmatter + entries.

```markdown
---
type: backlog
title: "Personal Capture Inbox"
address: c-NNNNNN
created: 2026-05-23
updated: 2026-05-23
status: living
tags: [backlog, todo, capture]
sessions:
  - id: <SESSION_ID>
    date: 2026-05-23
---

# Backlog

Lightweight capture inbox. Append-only via /backlog skill. One line per item.
Format: `- [YYYY-MM-DD] <slug> — <summary> — context: <link or ->`

Promoted items disappear from here; tracking moves to a wiki page or drop (see wiki/log.md).

## Entries

(items here — append at the bottom for chronological ordering; list mode flips it)
```

---

## Reuse pointers

- `wiki/backlog.md` — canonical inbox file (initialized per the spec above on first use).
- `wiki/log.md` — promote/add log entries.
- Conventions: `sessions:` provenance in frontmatter; promote mode requires pre-flight; chains are suggestions, never auto-invoked silently.
- Chain skill: `/save`.

---

## Anti-patterns

- ❌ Duplicate state: leaving an item in the backlog after promote — `wiki/log.md` loses authoritative tracking.
- ❌ Auto-dropping items > 60d — the user decides explicitly.
- ❌ Pre-flight questions on `add` — it is lightweight capture, not write-heavy.
- ❌ Violating the strict format in backlog.md — the list-mode parser breaks.
- ❌ Skipping the log.md entry on promote — the audit trail is lost.
- ❌ Slug conflicts (two items with the same slug) — Phase A.1 must check + auto-suffix (`-2`).
- ❌ Duplicating already-tracked work items into the backlog — the backlog captures ideas before they get a page or tracker entry, it does not mirror existing tracking.
