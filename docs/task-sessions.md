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
