# Release acceptance architecture

Version 2.3.0 replaces the historical skill-by-runtime matrix with four
provider-backed harness cells on one frozen Git SHA:

1. Claude: open → callback → same-session continue → exit → exact close.
2. Codex: the same lifecycle.
3. Cross-runtime dispatch → simple review → reap composition.
4. Deep review: independent spec and correctness/architecture/security
   sessions → bounded callbacks → terminal cleanup.

`config/acceptance-cells.toml` is the reviewed contract.
`scripts/release-acceptance.py check` validates the exact cell set, public
clean cut, safe dependency paths, and per-cell dependency hashes without
starting a model.

`scripts/live-acceptance-runner.py run` imports the repository-owned,
in-process cell driver only after binding execution to the exact clean checkout.
External shell or environment-selected drivers are not accepted. Each result
must contain the unique operation ID, exact runtime/role assignment, complete
ordered lifecycle observations, current dependency fingerprint, and zero
remaining owned resources. The runner rechecks the clean SHA and dependency
closure before persisting each atomic checkpoint. Global route/capability
preflight completes before the first cell creates an operation or external
effect. Its content-free capability artifact is retained in the exact-SHA
schema-v3 state and report instead of being discarded; individual cell evidence
remains schema v2.

A green cell is reused only when the source SHA and its dependency fingerprint
still match. The runner persists at most one content-free failure classified as
`runtime-contract` or `mechanism-failure`. The next resume invocation executes
only that classified cell; after it recovers, a later invocation may continue
the remaining cells. Green cells are not rerun unless their dependency
fingerprint changes. Reports are accepted only when all four cells pass on the
current exact SHA and the failure list is empty.

The composition cell calls the production
`workflows.dispatch.start_dispatch`, automatic simple `ReviewGateController`,
task-finalization authorization, and `workflows.reap.run_reap` facades in that
order. Reap finalizes the original dispatch ledger record, so the report has
two provider operations (`dispatch` and `simple-review`) plus the required
three-step lifecycle trace (`dispatch`, `simple-review`, `reap`), rather than a
fabricated third provider session.

The live driver must use Swarm's harness adapters and operation ledger. It may
not invoke a second dispatch/worktree/hook system, infer ownership from focus or
titles, or close an unrecorded process/surface. Prompts, transcripts, screen
text, commands, queries, and page bodies are excluded from acceptance state.
The coordinator-only `.task-origin-session` marker is likewise excluded from
the clean-bootstrap behavioral dirt check.
