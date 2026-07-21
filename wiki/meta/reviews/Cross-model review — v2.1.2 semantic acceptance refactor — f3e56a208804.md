---
type: review
title: "Cross-model review — v2.1.2 semantic acceptance refactor — f3e56a208804"
address: c-000021
created: 2026-07-21
updated: 2026-07-21
tags:
  - review
  - cross-model
status: resolved
sessions:
  - "019f6ddd-d07e-7a30-b018-f6358753fb91"
review_id: "437e43d1-2973-4681-b95c-1ae14b673823"
reviewer_runtime: "claude"
reviewer_model: "fable"
reviewer_effort: "high"
review_mode: "full"
rounds: 2
verdict: approve
related:
  - "[[2026-07-21-085654-llm-obsidian-v2-1-2-semantic-acceptance-refactor]]"
---

# Cross-model review — v2.1.2 semantic acceptance refactor — f3e56a208804

> [!abstract] Outcome
> **Task:** v2.1.2 semantic acceptance refactor
> **Final verdict:** `approve`
> **Reviewer:** claude · fable · effort `high`
> **Executor:** codex
> **Mode:** `full` · **rounds:** 2
> **Started:** 2026-07-21T06:01:14Z
> **Updated:** 2026-07-21T06:22:04Z
> **Plan:** [[2026-07-21-085654-llm-obsidian-v2-1-2-semantic-acceptance-refactor]]

## Review request

Review the implementation for **v2.1.2 semantic acceptance refactor** in `task/v2.1.2-acceptance-refactor` against `main` using the `full` cross-model gate.

> [!quote] Original task request
> Review the decision-complete implementation plan at:
>
> `wiki/plans/2026-07-21-085654-llm-obsidian-v2-1-2-semantic-acceptance-refactor.md`
>
> The executor must not begin product implementation until this plan review is approved. Review the plan against the current committed repository at `d17ffc9` and the user-approved decisions captured in the plan.
>
> Focus critically on:
>
> - whether the proposed per-cell semantic fingerprint is sound without global unknown-path invalidation;
> - whether explicit dependencies plus a generated verification lock avoid both under- and over-invalidation;
> - whether data-layer isolation for `wiki/` and `.vault-meta/` is safe and deterministic;
> - whether the adapter/harness split removes historical-code execution and manual behavior tables without changing prompts or user-visible contracts;
> - whether bounded retries, activity-based timeout, exact cmux cleanup, and content-free telemetry are implementable without hidden loops;
> - whether the review/test/release gates are sufficient to ship one fresh 58-cell matrix with Terra/Sonnet at medium effort;
> - whether any plan item adds unnecessary complexity or leaves an implementation decision unresolved.
>
> This is a plan review. The reviewer is product/vault read-only and must not edit files. Return typed findings with precise plan sections and a clear verdict. Blocking findings must identify the missing decision and a concrete safe resolution. Warnings should distinguish pre-matrix requirements from deferrable debt.
Approved scope and rationale: [[2026-07-21-085654-llm-obsidian-v2-1-2-semantic-acceptance-refactor]].

## Round 1 — changes-requested

- Phase: `initial-review`
- Run ID: `961cb9be-5d5d-4440-be13-4c43b0f95804`
- Received: 2026-07-21T06:07:31Z

### Findings

#### 1. blocking — Dependency checker scoped to Python/import, subprocess, and shell edges leaves non-code runtime inputs uncovered once the unknown-path net is removed.

- File: `wiki/plans/2026-07-21-085654-llm-obsidian-v2-1-2-semantic-acceptance-refactor.md:48`
- Evidence:
> Plan lines 47-48 delete the 'unknown changed path -> rerun all' rule (today at scripts/release-acceptance.py:384-388) and rely on a lock/checker that confirms only code edges. Two concrete classes in the current tree are invisible to that checker: (1) data-file reads by declared dependencies, e.g. .claude/hooks/skill-router.py:33 reads .claude/skill-rules.json, so a rules change would invalidate nothing and stop nothing; (2) agent-runtime hook registration: hooks/hooks.json triggers .claude/hooks/stop.sh and peers inside every live sandbox (the reason disable_acceptance_autocommit exists at scripts/live-acceptance-runner.py:301), but no Python/subprocess/shell edge from any declared dependency reaches those scripts, and neither hooks/hooks.json nor the hook scripts are in global_dependencies (config/acceptance-cells.toml:29-41). Today the global unknown-path rule is the only thing that catches both; removing it as planned creates silent stale-evidence reuse, defeating the refactor's core correctness goal.
- Recommendation:
> Add the missing decision to the plan: (a) the lock generator must also extract constant repo-relative path literals of any extension (not just .py/.sh) from runtime-reachable code in declared dependencies and treat them as behavioral edges, failing closed on dynamically constructed repo paths exactly as planned for dynamic code edges; (b) agent-runtime registration surfaces executed in every live sandbox (hooks/hooks.json, .claude/hooks/**, .claude/skill-rules.json, plugin manifests that register agents/hooks, .codex agent configs) plus the scripts they register must be declared global behavioral dependencies of every cell, with a model-free test asserting the registration files' referenced scripts are all covered by the lock.

#### 2. warning — Pre-matrix: fingerprint model generation must come from the actually-launched cell model, not production routing defaults.

- File: `wiki/plans/2026-07-21-085654-llm-obsidian-v2-1-2-semantic-acceptance-refactor.md:46`
- Evidence:
> The plan says Claude models are distinguished by registered major generation, but current code derives the fingerprint generation from production routing defaults (production_generations in scripts/acceptance_fingerprints.py:537-549 reads config/model-routing.toml: opus/gpt-5.6-sol), while the matrix will actually launch sonnet/gpt-5.6-terra via sandbox overrides (install_acceptance_model_overrides, scripts/live-acceptance-runner.py:310). Carrying the current source over verbatim would tag Sonnet-produced evidence as claude:opus-4.8, silently violating the plan's own generation-distinction rule and enabling cross-generation reuse. sonnet and opus are different registered generations (config/acceptance-cells.toml:43-48), so this directly affects the validity of the planned 58-cell release evidence.
- Recommendation:
> Pin in the plan/implementation that the fingerprint's generation input is canonical_generation(resolved per-cell launch model after acceptance overrides), record both the actual model and its generation in evidence, and add a model-free test that evidence produced under a sonnet-generation launch is not reusable for a cell that would launch an opus-generation model.

#### 3. warning — Pre-matrix: behavioral vs orchestration classification of the new scripts/acceptance/ modules is not pinned; launchers are behavioral but absent from the fingerprint input list.

- File: `wiki/plans/2026-07-21-085654-llm-obsidian-v2-1-2-semantic-acceptance-refactor.md:44`
- Evidence:
> The fingerprint input list (plan lines 44-45) names skill/scenario adapters, SKILL.md, shared behavioral ABI/template, exact runtime dependencies, fixture seed, and model generation, and excludes orchestration/scheduling. Runtime launchers (agent argv, env, permission mode, sandbox construction) directly shape cell behavior and are behavior-hashed today via LIVE_RUNNER_COMMON_FUNCTIONS (scripts/acceptance_fingerprints.py:41-51: agent_argv, run_agent_process, create_sandbox, install_acceptance_model_overrides). After deleting the AST tables, nothing in the plan states which new modules land inside the fingerprint and which in the excluded orchestration set, so a behavioral launcher change could be misclassified as orchestration and reuse stale evidence.
- Recommendation:
> State the module-level split explicitly: launcher modules and any harness code that constructs the agent invocation, sandbox, or outbox/result contract are behavioral ABI inputs (hashed per cell, per runtime); only scheduling, retries, checkpointing, and evidence selection stay in a code-owned orchestration allowlist (successor to ALLOWED_ORCHESTRATION_DEPENDENCIES), enforced by a model-free test that the excluded set exactly equals that allowlist.

#### 4. warning — Pre-matrix: seed-vault construction inside a git-clone sandbox is undecided.

- File: `wiki/plans/2026-07-21-085654-llm-obsidian-v2-1-2-semantic-acceptance-refactor.md:49`
- Evidence:
> wiki/ and .vault-meta/ are tracked, the current sandbox is a clean detached git clone at HEAD (scripts/live-acceptance-runner.py:279-298), and the runner requires a clean source checkout. Replacing the tracked vault trees with a minimal seed makes the clone dirty, which interacts with in-sandbox validation, Stop-hook behavior, and cells whose proofs reason about commit state (e.g. the daily fixture requires exactly one evidence commit). The plan says each adapter works in its own copy of a canonical seed vault but not how that copy is deterministically materialized against the committed product code.
- Recommendation:
> Decide the mechanism in the plan: keep the canonical seed as a committed fixture tree (e.g. under evals/acceptance/seed/), have the sandbox builder create a local synthetic commit that swaps wiki//.vault-meta for the seed so the sandbox tree stays clean and commit-based proofs remain deterministic, and hash the seed tree as the 'fixture seed' fingerprint input.

#### 5. nit — Prompt byte-equality gate should specify pinned placeholder render inputs.

- File: `wiki/plans/2026-07-21-085654-llm-obsidian-v2-1-2-semantic-acceptance-refactor.md:62`
- Evidence:
> Rendered prompts embed run-specific values (sandbox paths, outbox path, model, commit). Byte-for-byte comparison of the 58-prompt baseline is only meaningful if both sides render with identical fixed placeholders, as the current behavior fingerprint already does (scripts/acceptance_fingerprints.py:398-408).
- Recommendation:
> State that the pre-refactor baseline and the post-refactor render both use the same fixed placeholder inputs (sandbox/outbox paths, model, effort, commit, fixture strings) so the equality check is deterministic.

### Executor resolution

> [!note] Resolution snapshot
> # Executor resolution — plan review 437e43d1
>
> Coordinator decision `3d9d910c-c42d-4c22-91f4-dc0050f23afa` explicitly approved all findings within the existing v2.1.2 refactor boundary.
>
> - **Applied — blocking dependency closure.** The plan now requires the generated lock to traverse code edges and constant repo-relative data paths of every extension, fail closed on dynamic paths, and cover runtime registration surfaces plus transitively registered scripts/data. Packaging-only manifest fields remain normalized out; behavioral registration fields are global dependencies.
> - **Applied — actual launched model.** Fingerprints and evidence use the resolved per-cell model after acceptance overrides and its canonical generation. Cross-generation Sonnet/Opus reuse is forbidden and model-free tested.
> - **Applied — behavioral module boundary.** Runtime launchers and harness code constructing sandbox, argv/env/permissions, prompts, and result/outbox contracts are behavioral ABI. Only the exact code-owned scheduling/retry/checkpoint/evidence-selection allowlist is excluded and tested.
> - **Applied — clean seed materialization.** The canonical seed is a committed fixture tree, installed into each sandbox through a local synthetic commit; its tree hash is fingerprinted.
> - **Applied — deterministic prompt parity.** Pre/post refactor prompt rendering uses identical fixed placeholder inputs.
> - **Applied — review notes.** Retry errors are a closed typed set, unknown failures are not retried, screen heartbeat telemetry is content-free, and pre-epoch evidence is rejected rather than migrated.
>
> The plan also records the coordinator-approved lifecycle invariant: an accepted reviewer amendment supersedes the prior baseline; a material boundary change pauses for a decision instead of silently reverting content.
>
> Validation: canonical vault transaction succeeded, reindex completed, `validate-vault.py --summary` passed, `git diff --check` passed, and the amended plan hash is bound in `.task-meta.json` as `a6106320674c7f2d72f9348a97cb49ba153b91f09ce238fe10262e0e04637c5e`.

### Verification gaps

- This is a plan-only review: the branch adds one plan page on top of the v2.1.1 checkpoint, so no product tests were applicable to the reviewed change itself.

### Residual risks

- The Claude half of the release matrix validates the sonnet generation while the production default remains opus/high; this is an explicit user-approved cost trade-off recorded in the plan's assumptions, but the 58/58 gate is evidence for the matrix models, not the production defaults.
- Even with a sound lock, live cells depend on external agent CLIs and network-touching scenarios (protected-web, unsafe-web); bounded retries reduce but cannot eliminate flake-driven blocked verdicts in the single fresh run.

### Notes for executor

- Define the closed enumeration of typed transient/capacity retry errors (cmux launch failure, surface allocation, agent-CLI capacity/rate responses) at implementation time; anything unclassified must map to fail/blocked without retry to honor the no-hidden-loops constraint.
- The activity heartbeat may observe screen changes, but telemetry must stay content-free per AGENTS.md: record only that the screen changed and timing counters, never screen text.
- Plan item 1's checkpoint/branch/plan-commit steps are already done at d17ffc9; CHANGELOG merge into 2.1.2 and manifest version bumps remain, and both stay non-behavioral under config/acceptance-cells.toml so they will not perturb fingerprints.
- Removing build_resume_migration_metadata (git-show + exec of historical runner source, scripts/release-acceptance.py:229-331) together with the new evidence epoch cleanly satisfies the 'no historical-code execution' goal; ensure resume rejects pre-epoch reports fail-closed rather than attempting compatibility.

## Round 2 — approve

- Phase: `verify-fixes`
- Run ID: `5bb1a0c2-b347-4041-ad5f-2e440dfcc817`
- Received: 2026-07-21T06:22:04Z

### Findings

No findings.
### Executor resolution

No resolution was required or recorded for this round.

### Verification gaps

- Could not independently recompute the amended plan file's SHA-256 in this locked-down runtime (shell hashing denied); verified instead by reading the full amended plan content at commit 5002701 and confirming .task-meta.json:17 now binds approved_plan_sha256 a6106320674c7f2d72f9348a97cb49ba153b91f09ce238fe10262e0e04637c5e as the executor stated.

### Residual risks

- The Claude half of the release matrix validates the sonnet generation while the production default remains opus/high; this remains the explicit user-approved cost trade-off recorded in the plan's assumptions.
- Even with the strengthened lock, live cells depend on external agent CLIs and network-touching scenarios; the bounded typed-retry policy reduces but cannot eliminate flake-driven blocked verdicts in the single fresh 58-cell run.

### Notes for executor

- Finding 1 (blocking, dependency lock scope) is resolved: plan line 49 extends the generated lock to constant repo-relative data-path literals of any extension with fail-closed handling of dynamically built or unresolvable paths, and line 50 declares runtime registration surfaces (hooks/hooks.json, registered .claude/hooks/**, .claude/skill-rules.json, behavioral registration fields of plugin manifests and .codex agent/runtime config) plus transitively registered scripts/data as global behavioral dependencies, with a model-free test that every registration reference is lock-covered. The field-level split of plugin manifests into behavioral registration fields vs normalized packaging/version fields is coherent with the existing non_behavioral_paths design.
- Finding 2 is resolved: plan lines 44-45 pin the fingerprint generation to the actually-launched per-cell model after acceptance overrides, store resolved launch model plus canonical generation in evidence, and forbid Sonnet/Opus cross-generation reuse; gate 3 adds the matching model-free check.
- Finding 3 is resolved: plan lines 44 and 46-47 include launchers and invocation/sandbox/outbox-shaping harness code in the behavioral ABI, confine scheduling/retries/checkpointing/evidence-selection to a closed code-owned orchestration allowlist excluded from fingerprints, and require a model-free exact-match test of the excluded set.
- Finding 4 is resolved: plan line 51 commits the canonical seed as a fixture tree, installs it via a local synthetic commit replacing wiki/ and .vault-meta/ so the sandbox tree stays clean and commit-based proofs stay deterministic, and fingerprints the seed tree hash.
- Finding 5 (nit) is resolved: gate 2 (plan line 65) now specifies identical fixed placeholder inputs (sandbox/outbox paths, model, effort, commit, fixture strings) for the pre- and post-refactor prompt renders.
- The prior review notes were also incorporated: closed typed retry set with no retry for unknown/assertion/permission failures (line 55), content-free screen heartbeat (line 59), and pre-epoch evidence rejected rather than migrated (line 43, gate 3).
- The new gate-1 amendment lifecycle invariant (line 64) matches the coordinator decision quoted in the resolution and stays within the task boundary; no objection.
- Untracked .task-review-* files in the worktree are review-orchestration noise, as expected for this phase.

## Archive boundary

This page keeps validated review findings, executor resolutions, and final verification. Dedicated raw prompts, compressed callback payloads, command logs, sockets, and cmux identifiers are intentionally excluded. Validated findings and executor resolutions are retained as review evidence.
