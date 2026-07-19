# Reap interactive and legacy compatibility

Load only when the task is not an unattended v3 final operation, the user asks
for a preview, or the canonical runner needs read-only diagnosis.

## Preview and selection

Resolve the task from its exact worktree/task metadata, not focused panes.
Read `.task-summary.json` first. A historical screen summary is a last-resort
read-only source and must be parsed by `scripts/parse-wiki-summary.py`; do not
manually scrape prose. Show the exact title/type/path, create-vs-update mode,
plan close, review state, and cleanup policy before any classic write.

Session mismatch, several candidates, missing canonical summary, unresolved
links, or dirty tracked product work require visible selection or repair. Never
guess task identity, title, page route, surface, or review approval.

## Classic write

Classic interactive filing still uses one `scripts/vault-write.py` JSON
transaction with optimistic `expected_sha256` for updates, allocated address,
frontmatter/provenance, log entry, hot bullet, and plan close. Reindex and run
the full vault validator afterward. Dry-run performs no write and no cleanup.

Final cleanup is graceful and exact: validate the handoff, arm lifecycle exit,
send `/exit` to the agent only through the lifecycle wrapper, wait for process
exit, then close only the recorded task surface. Preserve dirty worktrees and
report them; never delete branches/worktrees automatically.

Executable truth is `scripts/reap-runner.py`, `scripts/reap-send-runner.py`,
`scripts/cmux_surface_lifecycle.py`, `scripts/vault-write.py`, and their tests.
