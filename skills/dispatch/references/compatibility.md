# Dispatch compatibility and recovery

Load this file only for explicit classic interactive dispatch, legacy metadata,
or diagnosis after the canonical runner failed.

## Classic resolution

- Repo order: matching `wiki/repos/*.md` frontmatter `path`, then a bounded
  search below `${LLM_OBSIDIAN_PROJECTS_ROOT:-$HOME/Projects}`, then ask for an
  absolute path. Several hits require user selection. Never clone implicitly.
- Existing branch names must be verified locally/remotely. A new task uses
  `task/<task_name>` from the explicitly confirmed base.
- Current-session pending plans are preferred. An explicit plan may belong to
  another session. Missing/non-pending/ambiguous plans require selection.
- Suggested internal agents may be derived from `.claude/skill-rules.json`;
  they are hints, never required delegation.

Classic mode may reproduce legacy v1 artifacts only when the user explicitly
requested interactive/no-plan compatibility. It is not resumable and must not
become an unattended fallback. Keep the same exact surface, route, worktree,
review/reap, and forbidden-action boundaries as v3.

## Recovery

- Existing worktree or task identity: report it; never overwrite or delete it.
- Missing/duplicate repo or plan: show bounded candidates and ask once.
- cmux unavailable/daemon down: report the exact dependency; do not fall back
  to an untracked background process.
- Partial `.task-meta.json`/surface state: diagnose read-only and use the
  central failure-repair contract. Never infer a replacement surface.
- Cross-session reap is visible and proceeds only through reap's typed contract.
- A failed runner may close only a blank exact child created by that operation.

The executable compatibility contract lives in `scripts/dispatch-runner.py`,
`scripts/task_sessions.py`, `scripts/cmux_agent_supervisor.py`,
`docs/task-sessions.md`, and their tests. Treat those as authoritative over
historical hand-written command sequences.
