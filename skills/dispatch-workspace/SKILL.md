---
name: dispatch-workspace
description: Spawn an approved task in a separate, unfocused cmux workspace anchored to the coordinator's exact window. Use when the user explicitly asks for a workspace/tab dispatch instead of the standard right-hand split.
---

# Dispatch Workspace

Read the normal [dispatch contract](../dispatch/SKILL.md) first. Reuse its model
routing, worktree isolation, review gate, and reap lifecycle; change only the
placement.

## Workflow

1. Require the same approved plan and dispatch inputs as `$llm-obsidian:dispatch`.
2. Build the normal dispatch request and set exactly `"placement": "workspace"`.
3. Validate once, then start once with `scripts/dispatch-runner.py`.
4. Report the created task workspace and return the coordinator to idle. Do not
   wait for the task or move focus to the new workspace.

The runner owns exact origin-window anchoring, worktree creation, task metadata,
supervision, and cleanup. Never call `cmux new-workspace` manually and never infer
the target window from current focus.

Use the standard `$llm-obsidian:dispatch` skill when the user did not explicitly
request a separate workspace.
