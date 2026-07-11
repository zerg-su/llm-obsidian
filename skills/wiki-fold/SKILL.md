---
name: wiki-fold
metadata:
  version: 2.0.0
description: "Deterministic rollup of the oldest unprocessed operation-log entries. Entry content hashes replace the fragile fold counter; existing fold pages are the processed-ID ledger. Dry-run by default, transactional commit on explicit request. Triggers: fold the log, run a fold, wiki-fold, log rollup."
allowed-tools: Read Bash
---

# wiki-fold: deterministic log rollup

Delegate selection, IDs, rendering, and commit to `scripts/fold-log.py`. Do not summarize entries by hand and do not edit `wiki/log.md`, `wiki/index.md`, fold pages, or `.vault-meta/` directly.

## Invariants

- Every `## [...] operation | title` block has a SHA-256 ID over its canonical text.
- Existing `wiki/folds/*.md` pages declare `entry_ids`; their union is the processed set.
- A batch is the oldest `2^k` unprocessed eligible entries. Default `k=6` means 64.
- `operation == fold` entries are never children of another flat fold.
- The fold ID contains the first and last boundary hashes, so the same input range has the same path.
- Children remain in `wiki/log.md`; folding is additive.
- There is no mutable `last-fold-count.txt` counter.

## Workflow

1. Inspect machine-derived status:

   ```bash
   python3 scripts/fold-log.py status --json
   ```

   If `ready` is false, report the unprocessed count and stop.

2. Run the default dry-run:

   ```bash
   python3 scripts/fold-log.py
   ```

   An explicit exponent is allowed for maintenance/testing: `--k 4` means 16 entries. Do not silently lower `k` merely to produce a partial fold.

3. Show the derived fold ID, date range, boundary IDs, and child count. The dry-run is stdout-only.

4. Commit only after the user explicitly asks to commit the reviewed fold:

   ```bash
   python3 scripts/fold-log.py --commit
   ```

   The helper sends the fold page and its new fold log entry through one `vault-write.py` transaction. Exit 4 means a concurrent session created the same fold; re-run status instead of forcing an overwrite.

## Output

The generated page contains strict frontmatter, the complete `entry_ids` ledger, a deterministic child table, operation counts, and integrity boundaries. Extracts are taken from each log entry's first meaningful line; the script invents no themes or facts.

## Reversal

Revert the single scoped commit. Child log entries were never changed. Do not delete a fold page alone: doing so intentionally makes its IDs unprocessed and eligible again.

## Do not

- Do not use Write/Edit for fold artifacts.
- Do not include a partial batch.
- Do not use dates alone in fold IDs.
- Do not reintroduce a fold counter.
- Do not include fold-operation log entries as children.
- Do not update `wiki/hot.md` for a fold.
