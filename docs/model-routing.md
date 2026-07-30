# Model routing

`config/model-routing.toml` is the single tracked source of concrete runtime,
model-alias, and role-specific model/effort defaults. Simple profiles use
Sol/high and Claude Opus 5/high (`claude-opus-5`); deep profiles use Sol/xhigh
and Fable/xhigh. A user may
add the gitignored `config/model-routing.local.toml`; the SessionStart preflight
makes that override visible. Native Codex configs are derived copies checked by
`scripts/model_routing.py check`.

## Resolution contract

Precedence is strict: explicit per-run override → captured current session →
local override → tracked default. The result records runtime, model, effort,
source steps, local-override state, and a configuration fingerprint. Invalid
effort, a provider/model mismatch, or an unregistered model without an explicit
runtime fails closed. There is no silent alias substitution or effort coercion.

| Role | Default behavior |
| --- | --- |
| Dispatch | Inherit the exact current runtime/model/effort. |
| Daily | Inherit current runtime/model; use the configured daily effort. |
| Simple review | The public `review` runner requests same-model and inherits the exact current route by default; `--cross-model` selects the opposite runtime's simple profile. The lower-level resolver keeps its explicit `--same-model` switch. |
| Deep review | The public runner uses the current runtime's deep profile by default; `--cross-model` selects the opposite runtime's deep profile. The lower-level resolver behaves analogously through `--same-model`. Spec and standards/correctness/architecture/security remain independent axes. |
| Protected research | Stay Codex-isolated. From Codex inherit current model/effort; from Claude use the central Codex route. |
| Unsafe research | After an explicit unsafe request, inherit the full current route and security context; warn once and do not run a second synthesis. |
| Other deep work | Inherit runtime/model and use the configured deep effort. |

Review model overrides accept only the registered aliases `sol`, `terra`,
`opus`, and `fable`; runtime/alias mismatches fail closed. Legacy task/review
records remain readable. Concrete top-level model/effort fields in old metadata
are treated as explicit historical overrides. New task metadata carries both
`routing.session` and `routing.effective`.

The tracked `opus` alias resolves to the versioned `claude-opus-5` target
instead of the host's moving `opus` alias, so older Claude Code installations
cannot silently route a 2.3 review to Opus 4.8.

## Session snapshots

SessionStart writes the fixed route to
`.vault-meta/session-routing/<session-id>.json`. Child sessions use that snapshot
until restart. When the host explicitly changes the active model or effort,
recapture the same session id; only later children see the new route and already
running children remain unchanged.

The snapshot records how the route was discovered. A host-confirmed route is
required for dispatch, daily, unsafe research, deep work, same-model review, and
Codex-origin protected research. If a host exposes no current model metadata,
SessionStart reports `session-routing` degradation and stores only a visibly
labelled `tracked-default` snapshot; exact-inheritance roles then fail closed
until the host-visible route is captured.

```bash
python3 scripts/model_routing.py capture-session \
  --session-id "$(./scripts/current-session-id.sh)" \
  --runtime codex --model '<host-visible-model>' --effort high

python3 scripts/model_routing.py resolve \
  --role dispatch --session-id "$(./scripts/current-session-id.sh)"
```

Environment integrations may instead set all three
`LLM_OBSIDIAN_SESSION_RUNTIME`, `LLM_OBSIDIAN_SESSION_MODEL`, and
`LLM_OBSIDIAN_SESSION_EFFORT`. Partial triples are rejected.

## Drift, update, and migration

Safe read-only checks:

```bash
python3 scripts/model_routing.py check
python3 scripts/session-preflight.py
python3 scripts/model-literal-lint.py
```

Synchronize generated native Codex files after deliberately changing the
central config. Reviewer-role defaults are consumed directly and are not copied
into native host defaults:

```bash
python3 scripts/model_routing.py sync-native --apply
```

Before a 2.3.0 overlay, finish or cancel every active task, review, research, or
harness operation with the installed version. Run the gate before mutation and
again after the overlay, before starting a replacement session:

```bash
python3 scripts/upgrade-preflight.py
```

The gate refuses every active harness kind, unreaped worktree, unfinished
legacy review/research run, and non-archived broker task. A customized legacy
`.codex/dispatch-env.toml` reviewer route is migrated into the matching
reviewer-profile override only after explicit confirmation; it never changes
the ordinary runtime default:

```bash
python3 scripts/upgrade-preflight.py \
  --confirm-routing-migration --apply
```

The migration writes the gitignored local override and never overwrites an
existing one. Historical wiki pages, archived reviews, and evaluation fixtures
are not rewritten when defaults change.

## Verification

```bash
python3 tests/test_model_routing.py
python3 tests/test_session_preflight.py
python3 tests/test_upgrade_preflight.py
python3 tests/harness/test_review_vertical.py
make test-model-routing test-session-preflight test-upgrade-preflight test-harness
```
