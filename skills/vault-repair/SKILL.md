---
name: vault-repair
description: Repair a blocked Stop pipeline with bounded local recovery. Use after COMMIT_BLOCKED or explicit vault-repair; not for wiki cleanup.
---

# Vault Repair

Use this skill as `$llm-obsidian:vault-repair` in Codex or `/vault-repair` in
Claude Code. It is a bounded manual entry into the same pipeline that owns
automatic recovery; it does not introduce another writer or scheduler.

1. Work from the vault root. Read `.vault-meta/stop-hook-last.log` when it
   exists, record the current commit with `git rev-parse HEAD`, then inspect
   `git status --short` without changing the index. If the log says
   `TASK_SPLIT_STOP_SKIPPED`, stop and report that the coordinator owns vault
   maintenance; never bypass that boundary from the dispatched worktree.
2. Recover an interrupted writer transaction exactly once:

   ```bash
   python3 scripts/vault-write.py --recover
   ```

3. Run the deterministic validator and classify its exact failure:

   ```bash
   python3 scripts/validate-vault.py --summary
   ```

   Do not edit `wiki/log.md`, `wiki/hot.md`, `.raw/.manifest.json`, or derived
   `.vault-meta/` indexes directly. Do not broaden the task to semantic wiki
   cleanup. An unambiguous unresolved wikilink is owned by the Stop pipeline's
   one-shot planner and optimistic `vault-write.py` transaction. Any ambiguous
   link, malformed prose, credential finding, or unrelated schema failure stays
   fail-closed and is reported with its exact path.
4. Make one repair attempt by rerunning the existing Stop owner:

   ```bash
   LLM_OBSIDIAN_ALLOW_CLAUDE_HOOKS=1 ./.claude/hooks/stop.sh
   ```

5. Rerun `python3 scripts/validate-vault.py --summary`, inspect
   `.vault-meta/stop-hook-last.log` and `git status --short`, then run
   `git rev-parse HEAD` again. If HEAD changed, report the exact resulting commit
   SHA. If validation succeeded but HEAD stayed unchanged, report `no-change`
   explicitly instead of claiming a commit. If `COMMIT_BLOCKED` remains, stop
   with the exact diagnostic and dirty paths. Never loop, access the network,
   add dependencies, stage broadly, discard unrelated work, or invoke a
   background model.
