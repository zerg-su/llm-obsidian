---
name: resolve-conflict
description: Resolve one exact conflicted Git worktree using intent evidence. Use for existing merge/rebase/cherry-pick conflicts.
---

# Resolve Conflict

Bind the exact worktree and current conflict operation. The harness gathers
BASE/ours/theirs, unmerged paths, relevant tests, and approved intent; the model
decides semantic resolution.

For each conflicted path:

1. Explain both sides and the intended combined behavior.
2. Resolve only conflict-related content.
3. Check for leftover markers and run the conflict verification profile.
4. Report the proposed exact stage list and remaining risks.

Stop for authorization before `git add`, continue, abort, or commit. Escalate
when intent is ambiguous, the conflict exposes a public/migration/security
choice, unrelated dirty state overlaps, or ownership cannot be proven. Never
use broad staging or destructive reset.
