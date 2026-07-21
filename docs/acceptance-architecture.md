# Release acceptance architecture

Release acceptance is a 58-cell live matrix: every installed skill runs once
through Claude and once through Codex. The release gate is macOS + cmux; Linux
retains basic script support and Windows is not a target.

## Behavioral boundary

`scripts/live-acceptance-runner.py` is a compatibility wrapper. The
implementation lives in `scripts/acceptance/`:

- `contracts.py` validates typed requests, outboxes, and content-free heartbeat
  records;
- `sandbox.py` creates a clone of the pinned source commit, replaces `wiki/`
  and `.vault-meta/` with `evals/acceptance/seed/`, and commits that seed with a
  fixed identity and timestamp;
- `launchers.py` owns runtime argv/env, visible cmux surfaces, inactivity
  detection, and exact cleanup;
- `prompting.py` is the versioned common prompt renderer;
- `skill_adapters.py` and `scenario_adapters.py` own scoped fixture, proof, and
  cleanup behavior;
- `runner.py` composes those pieces and retains the public CLI.

`evals/acceptance/prompt-baseline-v2.1.1.json` hashes all rendered prompts on
fixed placeholders. Unit tests require all 58 hashes to remain identical. A
real prompt correction therefore needs an explicit reviewed baseline change.

## Dependency lock and fingerprints

`config/acceptance-cells.toml` is the reviewed source of truth.
`scripts/acceptance_dependencies.py --write` produces
`config/acceptance-dependencies.lock.json` without importing or executing
product code. The checker follows static Python imports, constant repo-relative
code/data paths, shell and registration references, and exact declarations for
dynamic `ROOT / ...` prefixes. Any graph drift blocks before a model is opened.

A cell fingerprint includes its exact skill and scenario registry fragments,
skill files, scoped scenario dependencies, registration surfaces, canonical
seed, behavioral ABI, semantic adapter fragments, compatible runtime versions,
and the major generation of the model actually selected after acceptance
overrides. It excludes `wiki/`, `.vault-meta/`, test files, reviewed packaging
metadata, retry/checkpoint scheduling, effort, and aliases inside one registered
model generation. Reports still retain the exact launch model and provenance.

Evidence epoch 3 has no migration path from v2.1.1. A row is reused only when
its typed integrity proof and current semantic fingerprint match. Unknown files
do not trigger a global rerun; a missing runtime edge instead makes the
dependency-lock check fail closed.

## Execution and recovery

`make acceptance-live` resumes in two owned cmux workspaces with five active
cells per workspace. `make acceptance-live-restart` explicitly discards prior
evidence. Limits are five cells per workspace and ten workspaces only via an
explicit override.

Each final row is checkpointed immediately. A cell retries at most twice after
the first attempt, and only for the closed set of explicit cmux launch/surface
allocation or agent capacity/rate-limit transients. Product assertions,
permission failures, contract failures, and unknown errors are durable
`fail`/`blocked` results.

Screen changes and lifecycle files update a content-free heartbeat containing
only stage, status, counters, and numeric time. Active work has no wall-clock
cutoff. Unchanged work receives a visible status probe after 15 minutes and is
stopped at the configured inactivity boundary (20 minutes by default). Cleanup
uses stored UUIDs, closes only owned surfaces/workspaces, and fails the release
gate if reconciliation finds an orphan.

For the v2.1.2 release gate, use Sonnet and `gpt-5.6-terra` at medium effort:

```bash
LLM_OBSIDIAN_ACCEPTANCE_CLAUDE_MODEL=sonnet \
LLM_OBSIDIAN_ACCEPTANCE_CODEX_MODEL=gpt-5.6-terra \
LLM_OBSIDIAN_ACCEPTANCE_EFFORT=medium \
make acceptance-live-restart
```
