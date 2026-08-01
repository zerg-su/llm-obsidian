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

## Protected research under the cmux surface wrapper

Open defect. The protected `research` workflow cannot launch its provider when
the runtime worker runs inside a cmux surface, so protected outbound research is
currently unavailable and callers must treat it as failing rather than slow.
Observed three times, always as fetch-stage `exit_code` 127 with no
`artifact.json` and no fetched sources.

The launch resolves through a chain that ends outside repository ownership:

1. The worker runs inside the cmux research surface, so `CMUX_SURFACE_ID` matches
   the operation's `surface_id` and `provider_argv` takes the cmux wrapper-shim
   branch, replacing `argv[0]` with that surface's ephemeral shim.
2. The shim's shebang is `#!/usr/bin/env bash`, so the pinned Node interpreter
   recorded in `launch.json` no longer matches the interpreter being resolved and
   the pin does not apply on this path.
3. The shim executes cmux's own `cmux-codex-wrapper`, which resolves the real
   `codex` by scanning `PATH` and exits 127 when it finds none.
4. `RESEARCH_PATH` is deliberately `/usr/bin:/bin:/usr/sbin:/sbin` and the real
   binary is outside it, so that scan cannot succeed by design.

The incompatibility is structural rather than environmental: protected research
sanitizes `PATH` on purpose, and the third-party wrapper requires the provider on
`PATH`. Widening `RESEARCH_PATH` would weaken the isolation the flow exists to
provide. Routing a protected runtime through the cmux wrapper also injects cmux
hooks into an environment that is supposed to be isolated, which is a second
reason the branch is wrong for this flow rather than merely unlucky.

The direct provider invocation works under full sanitization: running the pinned
interpreter against the Codex JS entrypoint under `env -i` with
`PATH=RESEARCH_PATH` and `HOME`/`CODEX_HOME` set to the runtime home returns 0.
The bounded repair is therefore to skip the wrapper-shim branch for the
`research-fetch` and `research-synth` callback modes and use the direct argv with
the pinned interpreter. That change touches the shared provider launch path used
by dispatch and review, so it needs its own task, its own regressions, and a
deliberate decision about whether any other callback mode should stop taking the
wrapper.
