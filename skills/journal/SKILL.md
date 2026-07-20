---
name: journal
metadata:
  version: 1.2.0
description: >-
  Open or update date-keyed journal plans, reminders, notes, completed tasks,
  carry-over, and session maps. Use for /journal, дневник, на завтра, напомни на
  дату, отметь сделанным, or session map; use daily for EOD synthesis.
allowed-tools: Read Glob Grep Bash AskUserQuestion
---

# journal: deterministic date-page operations

Resolve date and intent, then call `scripts/journal-write.py`. It routes mutations
through optimistic `vault-write.py`; never Write/Edit or redirect into `wiki/`.

## Modes

```bash
# Canonical page; today also reports a read-only unfinished count
python3 scripts/journal-write.py ensure --date YYYY-MM-DD
python3 scripts/journal-write.py today --date YYYY-MM-DD

# Capture or complete one unique plan/reminder match
python3 scripts/journal-write.py append --date YYYY-MM-DD --section plans --text "task"
python3 scripts/journal-write.py append --date YYYY-MM-DD --section reminders --text "FYI"
python3 scripts/journal-write.py append --date YYYY-MM-DD --section notes --text "scratch text"
python3 scripts/journal-write.py check --date YYYY-MM-DD --match "substring"
python3 scripts/journal-write.py check --date YYYY-MM-DD --section reminders --match "substring"

# Batch 1–20 ordered append/check operations in one transaction
python3 scripts/journal-write.py batch --date YYYY-MM-DD --operations-json '[{"op":"append","section":"plans","text":"task"}]'

# Carry over through agenda; refresh session navigation
python3 scripts/journal-write.py carryover --source YYYY-MM-DD --target YYYY-MM-DD
python3 scripts/journal-write.py sessions --date YYYY-MM-DD
```

Plans and reminders are Tasks-compatible checkboxes with stable `^agenda-...` block
IDs; exact text deduplicates per section across statuses. Notes append freely. Use
`batch` for multi-mutation requests. `check` refuses zero/multiple matches; ask for a
narrower match. Carry-over delegates to `agenda.py collect`, closes the source as `[>]`,
and creates one open target in the same transaction.

## Date resolution

- today/сегодня: current date; tomorrow/завтра: +1; послезавтра: +2 days.
- A weekday means next occurrence; confirm only when it could mean today.
- Validate `YYYY-MM-DD`; day+month means next non-past occurrence unless ambiguous.
- Back-dating is allowed; mention it once.

`today` ensures the page and scans prior daily pages read-only. Show its unfinished
count and offer `agenda collect`; use the agenda skill for preview/carry-forward/monthly
review. Print a compact plans/reminders/incidents glance. Open Obsidian only on request.

The canonical layout comes from `_templates/daily.md`; daily pages are address-exempt.
`/journal` never synthesizes or edits `## Сделано`—that belongs to `/daily`.
