---
name: journal
metadata:
  version: 1.1.0
description: >-
  Open or update date-keyed journal plans, reminders, notes, completed tasks,
  carry-over, and session maps. Use for /journal, дневник, на завтра, напомни на
  дату, отметь сделанным, or session map; use daily for EOD synthesis.
allowed-tools: Read Glob Grep Bash AskUserQuestion
---

# journal: deterministic date-page operations

Resolve the user's date and intent, then call `scripts/journal-write.py`. All page
creation and mutation goes through one optimistic `vault-write.py` transaction; never
use Write/Edit or shell redirection on `wiki/`.

## Modes

```bash
# Create/open the canonical page (idempotent)
python3 scripts/journal-write.py ensure --date YYYY-MM-DD

# Capture
python3 scripts/journal-write.py append --date YYYY-MM-DD --section plans --text "task"
python3 scripts/journal-write.py append --date YYYY-MM-DD --section reminders --text "FYI"
python3 scripts/journal-write.py append --date YYYY-MM-DD --section notes --text "scratch text"

# Complete one uniquely matching unchecked plan
python3 scripts/journal-write.py check --date YYYY-MM-DD --match "substring"

# Copy pending plans forward without rewriting history
python3 scripts/journal-write.py carryover --source YYYY-MM-DD --target YYYY-MM-DD

# Refresh deterministic session navigation
python3 scripts/journal-write.py sessions --date YYYY-MM-DD
```

Plans/reminders deduplicate exact text. Notes are free-form and append. `check` refuses
zero or multiple matches; ask the user to disambiguate and retry with a narrower match.
Carry-over copies unchecked tasks and leaves the source page unchanged.

## Date resolution

- today/сегодня: current date; tomorrow/завтра: +1 day; послезавтра: +2 days.
- A weekday means the next occurrence; confirm only when the phrase could mean today.
- Validate absolute `YYYY-MM-DD`. For day+month without a year, choose the next
  non-past occurrence and ask only when that changes likely intent.
- Back-dating is allowed; mention it once.

For `today`, after `ensure`, scan up to five prior date pages for unchecked plans. Show
the candidate list and ask once before calling `carryover`. Print a compact glance of
plans, reminders, and incidents. Open Obsidian only when explicitly requested.

The canonical layout comes from `_templates/daily.md`; daily pages are address-exempt.
`/journal` never synthesizes or edits `## Сделано`—that belongs to `/daily`.
