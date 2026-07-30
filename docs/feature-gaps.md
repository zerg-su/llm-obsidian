# Feature gaps

## Task-session registry garbage collection

The 2.3.0 harness and task-session adapter intentionally have no automatic
age-based deletion or similarity-based reconstruction. Final reap removes
persistent runtime homes and leaves bounded archived audit metadata. A future
explicit maintenance command may list and delete selected archived tasks, but
it must never infer task ownership or silently attach archived context to a new
task.

## Non-cmux and Windows UI parity

Persistent visible lanes require cmux anchored-split and typed-resume support.
Local script workflows still work without those UI features, but there is no
alternate terminal-tab implementation. macOS is the supported target, Linux is
basic/hermetic, and Windows remains unsupported.

## Provider checkpoint portability

Checkpoints are local provider/session identifiers. They are not exported,
synced between machines, or reconstructed after registry loss. Failure remains
visible and falls back to a fresh full-packet session in the exact lane.

## Provider-backed live acceptance evidence

The historical skill-by-runtime matrix is removed. The repo-owned in-process
driver starts four bounded lifecycle cells through `RuntimeSessionManager`;
external shell drivers and environment-selected replacements remain
unsupported. Live evidence is intentionally local and SHA-bound rather than
committed: maintainers need working Claude/Codex subscriptions plus cmux, and a
release cannot publish without a schema-v2 pass on the exact clean release SHA.
