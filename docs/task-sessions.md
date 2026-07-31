# Persistent task sessions

Version 2.3.0 puts the public lifecycle behind the owner-scoped harness ledger
under `.vault-meta/harness/`. The task-session broker under
`.vault-meta/task-sessions/` remains an internal adapter for persistent
project/task/model/domain lanes. Both are authoritative gitignored runtime
state, not derived indexes. Neither has a broad automatic garbage collector;
terminal harness cleanup and final `reap` are the normal boundaries.

## Identity

- `project_id` is an opaque UUID stored at
  `<git-common-dir>/llm-obsidian/project-id`. Linked worktrees share it.
- `task_id` is an opaque UUID. A provider session is lazily bound through
  `scripts/task_sessions.py ensure-session-task`, or explicitly attached by
  exact ID. Names, branches, paths, recency, and “only candidate” matching are
  never used.
- One coordinator provider session may explicitly own several concurrent task
  IDs, including tasks in the same project. Bindings are task-scoped rather
  than a single mutable session slot. Implicit reuse succeeds only when exactly
  one active task is bound; multiple active tasks fail closed and require an
  explicit `task_id`.
- A lane key hashes `project_id + task_id + permission_domain + runtime +
  pinned_model`. Effort is an operation launch parameter, not lane identity.
- Active 2.3 task-session domains are `normal` and `review`.
  `secure-fetch` and `secure-synth` are legacy 2.2 values retained only for
  upgrade/preflight recognition; protected research now uses harness child
  operations. A read-only reviewer never resumes a writable task lane.
- Every harness `OperationSpec` adds exact owner, operation, idempotency, lane,
  and run identity. A callback must match the complete tuple and legal current
  state; a duplicate accepted callback is a no-op, while wrong-run and late
  terminal callbacks are rejected.

Task-meta v3 carries `project_id` and `task_id`. v1/v2 remain readable and keep
their exact legacy origin-session and worktree-local artifact rules. Running
legacy sessions are never adopted during an overlay.

## Deterministic dispatch and reap

`scripts/dispatch-resolver.py` performs the read-only Phase 1 inventory. It
deduplicates repo candidates from `wiki/repos` and the configured projects
root, finds pending current-session/explicit plans, and ranks up to five
existing wiki pages through the canonical hybrid retriever in read-only mode.
When the dense index or local embedding service is unavailable, retrieval
reports the degradation and falls back to sparse search without blocking. Zero or multiple repo/plan matches
return `needs-selection`; the script never chooses by recency across unrelated
sessions or turns a fuzzy context score into authorization.

The harness dispatch workflow owns the approved-plan setup boundary and calls
the existing resolver/runner only as internal adapters. The coordinator still
resolves the repository, context, exact result title, and user approval; after
approval it submits one typed request. The operation captures the exact current
session route, claims the request UUID, creates one branch/worktree/task
binding, renders and validates task-meta v3, opens one split anchored to the
caller, launches the supervisor, verifies the surface, and writes the dispatch
log transaction.

The run claim is persistent under `.vault-meta/dispatch-runs/<request-id>.json`.
A completed request replays its original typed result. A preparing or failed
request cannot be started again implicitly, so coordinator retries cannot open
duplicate surfaces. If launch fails after a blank child split was created, only
that exact not-yet-running split is closed; an already launched task is never
rolled back or duplicated. `validate --spec ...` performs the same fail-closed
request, plan, context, route, and prompt checks without creating a worktree or
surface.

The harness reap workflow owns the symmetric first finalization and uses
`scripts/reap-runner.py` as its vault-facing adapter. Given the exact worktree,
it validates the summary/handoff, archives all review operations, renders the
provenance page, prepares and commits the collision-safe result plus plan close
in one transaction, validates the vault, archives the broker task, and arms
exact-surface exit. Legacy, interactive, ambiguous, conflicted, and
already-executed recovery cases stay visible and use the diagnostic contract
rather than an implicit retry.

The provider worker validates `.task-summary.json` against the task contract
and delivers it through the internal callback broker. A v3 unattended handoff
never asks the coordinator to resolve the task from `wiki/log.md` or
reconstruct reap phases from prose.

## Layout and concurrency

Canonical operation state and the persistent lane adapter are:

```text
.vault-meta/harness/owners/<owner_id>/
  operations/<operation_id>.json

.vault-meta/task-sessions/projects/<project_id>/tasks/<task_id>/
  task.json
  lanes/<lane_id>/
    lane.json
    runtime/
    operations/<operation_id>/
      operation.json
      prompt/result/callback/baseline and bounded lifecycle artifacts
```

Task, session-binding, and lane transitions use short `fcntl` locks plus atomic JSON replacement.
Duplicate operation IDs are idempotent. One lane runs one operation and drains
new work FIFO; different tasks/models/domains can run concurrently. Enqueue is
serialized against `reap`, which changes `active -> archiving -> archived`
before enumerating lanes.

Once a launcher claims a review operation, every pre-supervisor failure
transitions that exact operation to `failed` before propagating the error. The
same containment remains for legacy 2.2 `secure-fetch` and `secure-synth`
records during upgrade recovery. A retry of an already-active operation reports
the active identity and recovery guidance; it is never described as healthy
queued work. Terminal transitions are repairable when process loss occurs
between the operation JSON replacement and the lane JSON replacement.

If the automatic transition itself cannot be persisted, the coordinator may
release only the confirmed exact active operation:

```bash
python3 scripts/task_sessions.py --vault-root <vault> fail-operation \
  --project-id <uuid> --task-id <uuid> --lane-id <lane-id> \
  --operation-id <uuid> --reason "confirmed launcher/runtime failure"
```

The command is idempotent for the same already-failed operation, rejects a
queued/foreign/complete operation, leaves FIFO entries intact, and prints the
next queued operation ID. It never guesses identity or launches queued work.

Review callbacks include the exact owner, operation, lane, run, and operation
directory. Reviewers write only one outbox in their owner-only runtime; the
trusted relay validates identity, state, mode, baseline, HEAD, and verification
profile before publishing the canonical callback. This prevents two
coordinators or reviewer models in one checkout from sharing metadata, results,
watchdog locks, or close sentinels.

## Surfaces and resume

All visible workflow splits use:

```bash
cmux --id-format both new-split right --surface <captured-origin-uuid> --focus false
```

There is no selected-tab or new-workspace fallback. SessionStart reports
missing anchored-split or typed `surface resume get/set/show/clear` support.

Review approval completes a cycle, not the task. Initial and verify stay in one
live surface. `finish` captures typed provider checkpoint metadata, exits the
agent, and closes only the armed UUID after the process returns. A later review
of the exact lane reconstructs a validated Claude/Codex resume command and
reapplies model, effort, cwd, and permission envelope. Stored shell commands
are ignored. Missing/corrupt checkpoints are visible and fall back to a fresh
full-packet session without asking the absent user.

If the surface closes but the broker terminal write fails, the reviewer close
sentinel remains in place and the exact `after-exit` command can be retried. It
prints the exact `fail-operation` fallback above if the registry cannot be
repaired automatically.

Protected research no longer uses `TaskSessionStore` or persistent
`secure-fetch`/`secure-synth` task lanes. In 2.3 it is an owner-scoped harness
root operation with fresh fetch and synth child operations. Fetch remains
vaultless, synth remains networkless, and only the coordinator can perform a
vault write. The old lane records are recognized only by upgrade preflight so
an unfinished 2.2 operation cannot be silently adopted.

## Lifecycle and upgrade

Final reap requires no active or queued operations, archives every exact review
cycle, validates all archive links in the result, archives the broker task, and
removes persistent lane runtimes and the worktree binding pointer. Bounded audit
metadata remains. Archived tasks are not automatically attached to later work.

`scripts/upgrade-preflight.py` blocks an overlay for every nonterminal harness
operation, every terminal harness record that retains a pending effect or exact
owned resource, and unmatched unreaped worktree, legacy review/research, or
non-archived broker state. One narrow exception applies when a canonical,
same-ID terminal dispatch proves all effects settled and all provider,
supervisor, and cmux ownership released: its matching v3 worktree mirror and
active lane-free broker mirror are stale rather than live and do not block.

Finish or cancel live operations with the installed runtime. A terminal record
that retains an effect or resource requires exact ownership inspection and
reconciliation, not another finish/cancel request. Then rerun preflight. Exact
per-class doctor guidance is deferred to the diagnostic seam; never upgrade a
live or uncertain session in place.

The supported UI target is macOS with cmux. Linux receives hermetic/basic
script coverage. Windows is unsupported.
