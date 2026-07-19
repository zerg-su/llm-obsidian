# Persistent task sessions

v2.1.0 replaces worktree-global reviewer state with an owner-only broker under
`.vault-meta/task-sessions/`. This directory is authoritative local runtime
state, not a derived index. It is gitignored and has no automatic garbage
collector; final `reap` is the normal archive/cleanup boundary.

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
- Domains are `normal`, `review`, `secure-fetch`, and `secure-synth`. A
  read-only reviewer never resumes a writable task lane; untrusted fetch
  context never resumes synthesis or normal context.

Task-meta v3 carries `project_id` and `task_id`. v1/v2 remain readable and keep
their exact legacy origin-session and worktree-local artifact rules. Running
legacy sessions are never adopted during an overlay.

## Deterministic dispatch start

`scripts/dispatch-resolver.py` performs the read-only Phase 1 inventory. It
deduplicates repo candidates from `wiki/repos` and the configured projects
root, finds pending current-session/explicit plans, and ranks up to five
existing wiki pages through the canonical hybrid retriever in read-only mode.
When the dense index or local embedding service is unavailable, retrieval
reports the degradation and falls back to sparse search without blocking. Zero or multiple repo/plan matches
return `needs-selection`; the script never chooses by recency across unrelated
sessions or turns a fuzzy context score into authorization.

`scripts/dispatch-runner.py` owns the approved-plan setup boundary. The
coordinator still resolves the repository, context, exact result title, and
user approval; after approval it passes one ignored typed request to `start`.
The runner captures the exact current session route, claims the request UUID,
creates one branch/worktree/task binding, renders and validates task-meta v3,
opens one split anchored to the caller, launches the supervisor, verifies the
surface, and writes the dispatch log transaction.

The run claim is persistent under `.vault-meta/dispatch-runs/<request-id>.json`.
A completed request replays its original typed result. A preparing or failed
request cannot be started again implicitly, so coordinator retries cannot open
duplicate surfaces. If launch fails after a blank child split was created, only
that exact not-yet-running split is closed; an already launched task is never
rolled back or duplicated. `validate --spec ...` performs the same fail-closed
request, plan, context, route, and prompt checks without creating a worktree or
surface.

`scripts/reap-runner.py` owns the symmetric first finalization of a v3
unattended task. Given the exact worktree, it validates the summary/handoff,
archives all review operations, renders the provenance page, prepares and
commits the collision-safe result plus plan close in one transaction, validates
the vault, archives the broker task, and arms exact-surface exit. Legacy,
interactive, ambiguous, conflicted, and already-executed recovery cases stay
visible and use the diagnostic contract rather than an implicit retry.

## Layout and concurrency

Canonical state is:

```text
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

Once a launcher claims an operation, every pre-supervisor failure in review,
secure-fetch, or secure-synth transitions that exact operation to `failed`
before propagating the error. A retry of an already-active operation reports
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

Review callbacks always include the exact operation directory. Reviewers write
only one outbox in their owner-only runtime; the trusted relay validates the
run/mode/baseline and publishes the canonical callback into that operation.
This prevents two coordinators or reviewer models in one checkout from sharing
metadata, results, watchdog locks, or close sentinels.

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
sentinel remains in place and the exact `after-exit` command can be retried.
Protected-research `status` likewise retries a pending post-close broker
transition. Both paths print the exact `fail-operation` fallback above if the
registry cannot be repaired automatically.

Protected research follows the same model with persistent `secure-fetch` and
`secure-synth` lanes. Cross-operation context is retained only inside the exact
task/domain. Scratch is new per operation; fetch remains vaultless and synth
remains networkless. An unrelated topic needs a new task ID.

## Lifecycle and upgrade

Final reap requires no active or queued operations, archives every exact review
cycle, validates all archive links in the result, archives the broker task, and
removes persistent lane runtimes and the worktree binding pointer. Bounded audit
metadata remains. Archived tasks are not automatically attached to later work.

`scripts/upgrade-preflight.py` blocks an overlay if any unreaped worktree,
unfinished legacy review/research run, or non-archived broker task exists.
Restart after the overlay; do not upgrade a live session in place.

The supported UI target is macOS with cmux. Linux receives hermetic/basic
script coverage. Windows is unsupported.
