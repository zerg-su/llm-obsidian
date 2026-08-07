---
name: vault-repair
description: Repair a blocked Stop pipeline with bounded local recovery. Use after COMMIT_BLOCKED or explicit vault-repair; not for wiki cleanup.
---

# Vault Repair

Invoke as `$llm-obsidian:vault-repair` in Codex or `/vault-repair` in Claude.
This is a bounded entry into the existing pipeline, not another writer.

1. From the vault root, read `.vault-meta/stop-hook-last.log` if present. Record
   `git rev-parse HEAD` and inspect `git status --short` without changing the
   index. On `TASK_SPLIT_STOP_SKIPPED`, stop: the coordinator owns repair.
2. Recover an interrupted writer transaction exactly once:

   ```bash
   python3 scripts/vault-write.py --recover
   ```

3. Run the deterministic validator and classify its exact failure:

   ```bash
   python3 scripts/validate-vault.py --summary
   ```

   Never edit `wiki/log.md`, `wiki/hot.md`, `.raw/.manifest.json`, or derived
   `.vault-meta/` indexes directly or broaden into wiki cleanup. Stop may repair
   one unambiguous wikilink through its planner and optimistic writer. Ambiguous
   links, malformed prose, credentials, and unrelated schema failures remain
   fail-closed with their exact paths.
4. Make one repair attempt by rerunning the existing Stop owner:

   ```bash
   LLM_OBSIDIAN_ALLOW_CLAUDE_HOOKS=1 ./.claude/hooks/stop.sh
   ```

5. Rerun the validator; inspect the log and status; run `git rev-parse HEAD`.
   Report the new exact SHA, or `no-change` when green with unchanged HEAD. On
   `COMMIT_BLOCKED`, stop with the exact diagnostic and dirty paths. Never loop,
   use network/models, add dependencies, stage broadly, or discard other work.
