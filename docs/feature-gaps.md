# Feature gaps

## Task-session registry garbage collection

v2.1.0 intentionally has no automatic age-based deletion or similarity-based
reconstruction for `.vault-meta/task-sessions/`. Final reap removes persistent
runtime homes and leaves bounded archived audit metadata. A future explicit
maintenance command may list and delete selected archived tasks, but it must
never infer task ownership or silently attach archived context to a new task.

## Non-cmux and Windows UI parity

Persistent visible lanes require cmux anchored-split and typed-resume support.
Local script workflows still work without those UI features, but there is no
alternate terminal-tab implementation. macOS is the supported target, Linux is
basic/hermetic, and Windows remains unsupported.

## Provider checkpoint portability

Checkpoints are local provider/session identifiers. They are not exported,
synced between machines, or reconstructed after registry loss. Failure remains
visible and falls back to a fresh full-packet session in the exact lane.

## Live acceptance duration

The full Claude/Codex skill matrix now has a repo-shipped interactive runner,
but it is intentionally sequential and uncached: each cell preserves its own
first-failure and cleanup evidence. A complete release pass is therefore
expensive. Parallel scheduling may be added only with isolated disposable
clones and independent exact cmux surfaces; a shared mutable fixture or cached
model verdict would weaken the acceptance contract.
