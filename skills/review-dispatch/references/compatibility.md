# Review-dispatch compatibility and diagnostics

Load only for legacy/in-flight operations, recovery, or security diagnosis.

Legacy state may live at worktree-root `.review-*`; v3 state lives only in its
exact broker operation directory. Never mix the two or copy callbacks between
operations. Legacy payload-b64 and relay transport remain accepted only for
operations created by older versions.

The supervisor owns reviewer argv/env, trusted PATH, Claude subscription check,
Codex scratch validation, outbox polling, callback receive, watchdog, and
close-after-exit. Reviewers receive no product write authority and no cmux
socket. Test entrypoints and DCG smoke remain explicitly bounded.

When an active operation appears orphaned, verify recorded process/surface state
read-only. Use only the exact recovery command printed by the tool after the
coordinator confirms the launcher/surface is gone. Never clear a lane, reuse a
focused surface, or create a replacement operation by inference.

Legacy interactive tasks may show results and wait; unattended tasks use
`drive --apply-action`. Verification always snapshots the executor resolution
and preserves the same reviewer/mode. Finish is allowed only after typed
approval and executor self-review.

Executable truth is `scripts/spawn_review.py`, `scripts/archive_review.py`,
`scripts/cmux_agent_supervisor.py`, `scripts/cmux_surface_lifecycle.py`,
`references/review-prompt-template.md`, and their contract tests.
