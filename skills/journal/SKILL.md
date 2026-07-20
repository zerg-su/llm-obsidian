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

Resolve the user's date and intent, then call `scripts/journal-write.py`. All page
creation and mutation goes through one optimistic `vault-write.py` transaction; never
use Write/Edit or shell redirection on `wiki/`.

## Modes

```bash
# Create/open the canonical page (idempotent)
python3 scripts/journal-write.py ensure --date YYYY-MM-DD

# Create/open today and return a read-only unfinished-item count
python3 scripts/journal-write.py today --date YYYY-MM-DD

# Capture
python3 scripts/journal-write.py append --date YYYY-MM-DD --section plans --text "task"
python3 scripts/journal-write.py append --date YYYY-MM-DD --section reminders --text "FYI"
python3 scripts/journal-write.py append --date YYYY-MM-DD --section notes --text "scratch text"

# Complete one uniquely matching plan or reminder and add ✅ YYYY-MM-DD
python3 scripts/journal-write.py check --date YYYY-MM-DD --match "substring"
python3 scripts/journal-write.py check --date YYYY-MM-DD --section reminders --match "substring"

# Apply ordered appends/checks to one date page in one optimistic transaction
python3 scripts/journal-write.py batch --date YYYY-MM-DD --operations-json '[{"op":"append","section":"reminders","text":"FYI"},{"op":"append","section":"plans","text":"task"},{"op":"check","section":"plans","match":"task"}]'

# Compatibility command: atomically migrate source plans via agenda
python3 scripts/journal-write.py carryover --source YYYY-MM-DD --target YYYY-MM-DD

# Refresh deterministic session navigation
python3 scripts/journal-write.py sessions --date YYYY-MM-DD
```

Plans and reminders are Tasks-compatible checkboxes with stable `^agenda-...` block
IDs; exact text deduplicates within a section across every status. Notes are free-form and append.
Use `batch` when one user request contains several mutations that must commit atomically;
it accepts 1–20 ordered `append`/`check` operations and still invokes `vault-write.py` once.
`check` refuses zero or multiple matches; ask the user to disambiguate and retry with a
narrower match. Carry-over delegates to `scripts/agenda.py collect`, closes the source
as `[>]`, and creates one open target occurrence in the same optimistic transaction.

## Date resolution

- today/сегодня: current date; tomorrow/завтра: +1 day; послезавтра: +2 days.
- A weekday means the next occurrence; confirm only when the phrase could mean today.
- Validate absolute `YYYY-MM-DD`. For day+month without a year, choose the next
  non-past occurrence and ask only when that changes likely intent.
- Back-dating is allowed; mention it once.

For `today`, call the `today` mode. It ensures the page and scans all prior daily pages
read-only. Show the unfinished count and offer `agenda collect`; do not mutate old pages
automatically. Use the `agenda` skill for full preview, carry-forward, and monthly review.
Print a compact glance of plans, reminders, and incidents. Open Obsidian only when
explicitly requested.

The canonical layout comes from `_templates/daily.md`; daily pages are address-exempt.
`/journal` never synthesizes or edits `## Сделано`—that belongs to `/daily`.
