---
name: agenda
description: >-
  Scan unfinished daily plans and reminders, carry them forward, maintain
  a monthly live report. Use for agenda scan/collect/report,
  собрать/перенести незавершённое, or monthly unfinished review.
---

# Agenda

Use `scripts/agenda.py`; do not rewrite daily pages directly. The script parses
Obsidian Tasks syntax, assigns stable block IDs, and sends every source change,
the target occurrence, and the monthly report through one optimistic
`vault-write.py` transaction.

## Workflow

1. Preview before mutation:

   ```bash
   python3 scripts/agenda.py scan --date YYYY-MM-DD --json
   ```

   Add `--since YYYY-MM-DD` only when the user explicitly wants a bounded
   history window. By default, scan every prior daily page.

2. Summarize the count and warnings. Explain `legacy_identity_merged` as an
   ambiguous text-based merge. Explain `nested_subtree_skipped` as a deliberate
   fail-safe: v1 never moves only part of a nested checklist. Explain
   `section_missing` as a noncanonical daily page that was skipped only for the
   absent Plans or Reminders section; other conforming pages still proceed.
   `target_section_created` means collect restored only the required canonical
   target heading before placing the carried item.

3. On a carry-forward request, run:

   ```bash
   python3 scripts/agenda.py collect --date YYYY-MM-DD
   ```

   Use `--dry-run` for a requested preview. A successful collect marks old
   occurrences `[>]`, adds `#agenda/migrated ↪ [[target]]`, creates exactly one
   open target occurrence per identity, and refreshes the monthly report.

4. To refresh only the declarative monthly page:

   ```bash
   python3 scripts/agenda.py report --month YYYY-MM
   ```

## Status contract

- `[ ]` open
- `[/]` in progress
- `[x] ... ✅ YYYY-MM-DD` done
- `[-]` cancelled
- `[>] ... #agenda/migrated ↪ [[YYYY-MM-DD]]` migrated

Preserve Tasks metadata and `^agenda-...` IDs. Treat same text with different
explicit IDs as different work. Never silently resolve `identity_conflict`,
`duplicate_target_identity`, or `target_identity_terminal`; report the warning
and leave those occurrences unchanged.

The Obsidian Tasks plugin is a UI layer, not a correctness dependency. The CLI
remains authoritative and works when the plugin is unavailable.
