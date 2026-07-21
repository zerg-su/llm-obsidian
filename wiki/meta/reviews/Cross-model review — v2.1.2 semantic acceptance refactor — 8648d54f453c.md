---
type: review
title: "Cross-model review — v2.1.2 semantic acceptance refactor — 8648d54f453c"
address: c-000026
created: 2026-07-21
updated: 2026-07-21
tags:
  - review
  - cross-model
status: resolved
sessions:
  - "019f6ddd-d07e-7a30-b018-f6358753fb91"
review_id: "a90bb6e7-2988-4e90-b4e6-3b89510f6008"
reviewer_runtime: "claude"
reviewer_model: "fable"
reviewer_effort: "high"
review_mode: "full"
rounds: 2
verdict: approve
related:
  - "[[2026-07-21-085654-llm-obsidian-v2-1-2-semantic-acceptance-refactor]]"
---

# Cross-model review — v2.1.2 semantic acceptance refactor — 8648d54f453c

> [!abstract] Outcome
> **Task:** v2.1.2 semantic acceptance refactor
> **Final verdict:** `approve`
> **Reviewer:** claude · fable · effort `high`
> **Executor:** codex
> **Mode:** `full` · **rounds:** 2
> **Started:** 2026-07-21T07:33:30Z
> **Updated:** 2026-07-21T08:09:52Z
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
- Run ID: `a8f5e5d5-afa9-40af-b065-153f9e927ea3`
- Received: 2026-07-21T07:46:55Z

### Findings

#### 1. blocking — The canonical seed tree is not a cell fingerprint input, violating the amended plan decision and the new architecture doc.

- File: `scripts/acceptance\_fingerprints.py:520`
- Evidence:
> The approved amended plan (wiki/plans/...-semantic-acceptance-refactor.md:51) requires 'fingerprint включает hash seed tree' and docs/acceptance-architecture.md:37-39 claims the fingerprint includes the 'canonical seed', but cell_metadata's payload contains no seed input: acceptance_seed_sha256 (scripts/acceptance/sandbox.py:19) is written only into the sandbox marker, and expanded_dependencies' common roots (global/registration/behavioral_abi) exclude evals/acceptance/seed/**. Seed files enter the committed lock only as roots (scripts/acceptance_dependencies.py:146-150); they appear as edges of other files purely by accident of basename-unique token resolution (wiki/ is not a SCAN_PREFIX, so text mentioning 'wiki/hot.md' resolves to the seed copy). Concretely, evals/acceptance/seed/.vault-meta/last-fold-count.txt has no inbound edge in config/acceptance-dependencies.lock.json (it appears only in roots and as an empty edges key), so editing it — or seed pages not textually referenced by a given cell's dependencies — changes every sandbox's data layer while all 58 fingerprints stay identical and load_resume_results reuses stale evidence. Every sandbox is built from the full seed (materialize_seed_commit), so all seed files are inputs of every cell. No test covers seed-change invalidation (grep 'seed' in tests/test_release_acceptance.py: no matches). During the gate-7 defect loop a seed fix would silently not rerun any cell.
- Recommendation:
> Add the seed to every cell's fingerprint: either include acceptance_seed_sha256() as an explicit payload field in cell_metadata, or add all evals/acceptance/seed/** files to the common dependency roots so they are content-hashed per cell. Add a model-free regression test that mutating one seed byte changes every cell fingerprint and forces a full rerun, and that reverting restores reuse.

#### 2. warning — Adapter binding surfaces are unfingerprinted: adapters.py re-exports and fragment-module import statements escape invalidation.

- File: `config/acceptance-cells.toml:34`
- Evidence:
> scripts/acceptance/runner.py imports every adapter callable through scripts/acceptance/adapters.py, but that module is excluded from cell dependencies as a semantic boundary (expanded_dependencies in scripts/acceptance_fingerprints.py:217) and is absent from behavioral_abi_dependencies — its content is hashed nowhere; tests/test_release_acceptance.py:454 even asserts the exclusion. Re-pointing a re-export (e.g. binding dispatch_acceptance_proof to a stub) changes live proof behavior with zero invalidation. Similarly _fragment_payload (scripts/acceptance_fingerprints.py:287-337) hashes function and assignment AST but not module-level import statements, so rebinding 'from .sandbox import run_checked' or 'from .scenario_adapters import is_disposable_bookkeeping' in skill_adapters.py changes fragment behavior while every fragment hash stays identical.
- Recommendation:
> Add scripts/acceptance/adapters.py to behavioral_abi_dependencies (it is tiny and stable, so over-invalidation cost is negligible), include ast dumps of Import/ImportFrom statements of the fragment module in each fragment payload, update the test at tests/test_release_acceptance.py:449-460 accordingly, and add a model-free check that changing a re-export or a fragment-module import changes the affected fingerprints.

#### 3. warning — Routing normalization erases cross-generation review-role model changes from fingerprints.

- File: `scripts/acceptance\_fingerprints.py:247`
- Evidence:
> _strip_routing_values removes every 'model'/'effort' key and the model_registry from config/model-routing.toml and the .codex routing TOMLs before hashing (ROUTING_KEYS at scripts/acceptance_fingerprints.py:48-56), while generation capture (launch_generations, generation_routes) covers only the two runtime defaults. Changing roles.review.claude from fable to a model of a different registered generation alters live opposite-model review behavior inside every dispatch-review-reap cell, yet invalidates nothing. The plan excludes only same-generation aliases and effort (plan line 47); cross-generation model changes are exactly what the fingerprint is supposed to distinguish.
- Recommendation:
> In _strip_routing_values, map each 'model' value to its registered canonical_generation instead of deleting the key (keep deleting effort and the registry), or include role-model generations as an explicit fingerprint input for lifecycle scenarios; add a model-free test that a cross-generation review-role change invalidates dispatch-review-reap cells while a same-generation alias change does not.

#### 4. warning — Pre-approved suite fails in the review environment: codex-review-supervisor-valid got exit 2, want 0.

- File: `tests/test\_review\_dispatch.sh:991`
- Evidence:
> Running the exact pre-approved command 'bash tests/test_review_dispatch.sh' here yields 164 passed, 1 failed: the positive-path 'cmux_agent_supervisor.py validate --kind reviewer' check. Validation requires the spec PATH to resolve cmux/codex from trusted stable directories (scripts/cmux_agent_supervisor.py:353-365), which this isolated product-read-only sandbox intentionally lacks, so the failure is plausibly environmental. However, the adjacent negative-path checks (codex-review-rejects-add-dir and peers) pass vacuously whenever validate fails for any reason, so an environmental break in this section can mask a real regression. All other suites pass here, including tests/test_release_acceptance.py (81 checks), tests/test_live_acceptance_runner.py (with the 58-prompt byte-parity gate), tests/test_task_lifecycle.py, tests/test_contract_schemas.py, and scripts/lint-instructions.py.
- Recommendation:
> Confirm tests/test_review_dispatch.sh is green in the executor environment on this exact commit and record that in the resolution; if it is red there, fix before the live matrix. Consider asserting a distinguishable validate failure message in the positive-path check (or skipping explicitly when cmux is unresolvable) so the negative-path checks cannot pass vacuously in cmux-less environments.

#### 5. nit — Merged 2.1.2 changelog contradicts itself about unknown-path invalidation.

- File: `CHANGELOG.md:46`
- Evidence:
> Lines 45-47 (merged from unreleased 2.1.1 text) state 'every unknown changed path continues to invalidate the matrix fail-closed', which contradicts the epoch-3 behavior described at lines 29-32 and the tested semantics ('committed unknown data path does not invalidate semantic evidence' in tests/test_release_acceptance.py).
- Recommendation:
> Rewrite the merged sentence to the current semantics: unregistered runtime edges stop the model-free dependency-lock check, and unknown non-runtime paths neither invalidate nor block.

### Executor resolution

> [!note] Resolution snapshot
> # Resolution — v2.1.2 implementation review
>
> Existing authority: coordinator decision `3d9d910c-c42d-4c22-91f4-dc0050f23afa`
> approved all Fable plan-review recommendations for this refactor, including the
> seed, registration-edge, launcher/harness, and actual-model-generation
> requirements. These changes close omissions inside that approved scope.
>
> 1. **Applied — canonical seed fingerprint.** Every cell payload now contains
>    the canonical seed-tree SHA-256. A model-free regression mutates one byte in
>    an otherwise unreferenced seed file, proves all 58 fingerprints change,
>    proves stale evidence reuse returns zero rows, and proves reverting the byte
>    restores the original fingerprints.
> 2. **Applied — adapter and import bindings.** `scripts/acceptance/adapters.py`
>    is a common behavioral ABI dependency. Fragment payloads include every
>    module-level `Import`/`ImportFrom` AST. Regressions prove a re-export change
>    invalidates the common ABI and a skill-adapter import rebinding invalidates
>    only cells using that fragment module.
> 3. **Applied — routing generation normalization.** Routing model values are
>    normalized to their registered canonical major generation; effort and the
>    registry remain excluded. A regression proves Sol→Terra preserves the
>    fingerprint while Fable→Sonnet changes it.
> 4. **Confirmed in executor environment.** `bash tests/test_review_dispatch.sh`
>    passes 165/165 on commit `d3ab3bd`, including
>    `codex-review-supervisor-valid`. The reviewer failure was caused by its
>    intentionally restricted PATH. In addition, UUID-scoped operation handoffs
>    are now strictly recognized and gitignored by both review-dispatch and
>    review-send; malformed names remain product drift.
> 5. **Applied — changelog consistency.** The unknown-path statement now matches
>    epoch-3 semantics: unregistered runtime edges fail the lock check, while
>    unknown non-runtime paths do not invalidate evidence.
>
> Additional review note applied: removed the dead duplicated runner constants
> while retaining compatibility imports used by the runner test surface.
>
> Verification:
>
> - `python3 scripts/acceptance_dependencies.py` — pass
> - `make acceptance-check` — 29 skills × 2 runtimes
> - `python3 tests/test_release_acceptance.py` — pass
> - `python3 tests/test_live_acceptance_runner.py` — pass, including 58/58 prompt parity
> - `bash tests/test_review_dispatch.sh` — 165/165
> - `make test` — pass
> - `git diff --check` — pass

### Verification gaps

- No durable record of the executor's own full 'make test' run is visible from the diff; I independently ran the two acceptance suites, task lifecycle, contract schemas, instruction lint (all green here) and test_review_dispatch.sh (one environment-suspect failure, filed as a finding).
- The live 58-cell matrix, cmux workspace supervision, and real agent launches cannot be exercised from this product-read-only session; correctness there rests on the model-free suites plus the gate-6 fresh run.

### Residual risks

- The lock's dynamic-path detection recognizes the module-global 'ROOT / ...' idiom and declared prefixes; dynamically composed repo paths built through other idioms are covered only by the constant-literal and basename-unique token scans, so a novel dynamic invocation pattern could still evade the lock until reviewed.
- The Claude half of the release matrix validates the sonnet generation at medium effort while production defaults remain opus/high — the explicit user-approved trade-off recorded in the plan assumptions.
- Live cells depend on external agent CLIs and network scenarios; bounded typed retries reduce but cannot eliminate flake-driven blocked verdicts in the fresh 58-cell run.

### Notes for executor

- The refactor otherwise implements the amended plan faithfully and verifiably: live-acceptance-runner.py is an 8-line wrapper over scripts/acceptance/; the LIVE_RUNNER_* tables, exec() of runner source, git-show historical migration, and the unknown-path global rerun are all gone; reuse is exact-fingerprint-only under evidence epoch 3 with fail-closed lock verification on every invocation; retries are capped at 3 for the closed typed transient set; the inactivity timeout, 15-minute probe, capacity detection, exact owned surface/workspace cleanup, and content-free heartbeats match the plan; launch_generations derives generation from the actually-launched model with alias/generation tests; the seed commit is deterministic (fixed date/identity, clean-tree verified); the supervisor validates shard evidence including launch_model and fails on owned orphans.
- The prompt byte-parity gate is genuinely enforced: evals/acceptance/prompt-baseline-v2.1.1.json holds 58 hashes on pinned placeholders and tests/test_live_acceptance_runner.py compares all of them ('all 58 refactored prompts are byte-identical to v2.1.1' passes here).
- scripts/acceptance/runner.py retains dead duplicated constants (SURFACE_RE, OUTBOX_* limits, VAULT_REINDEX_SCENARIOS, DISPOSABLE_VAULT_BOOKKEEPING, AUTORESEARCH_OUTPUT_LIMIT) that now live in launchers/prompting/scenario_adapters; harmless since runner.py is ABI-hashed, but worth removing with the blocking fix's ABI churn.
- Narrow gate interaction to be aware of: .claude-plugin/plugin.json is both a registration dependency (agents field semantically hashed) and an allowed-dirty non-behavioral path, so a dirty edit to its agents field passes the committed-state gate while changing fingerprints; evidence then binds to a source commit that lacks the change. Consider excluding registration-normalized files from the allowed-dirty set or checking that their behavioral fields are clean.

## Round 2 — approve

- Phase: `verify-fixes`
- Run ID: `9351dec8-042f-48f7-b46d-c4e927abc789`
- Received: 2026-07-21T08:09:52Z

### Findings

No findings.
### Executor resolution

No resolution was required or recorded for this round.

### Verification gaps

- The single codex-review-supervisor-valid failure in tests/test_review_dispatch.sh still reproduces in this restricted reviewer sandbox (164/165 here); attribution to the sandbox's intentionally restricted PATH is consistent with the Claude-side review-supervisor-valid passing here and with the executor's recorded 165/165 run on d3ab3bd, but I could not reproduce the green codex check myself.
- As before, the live 58-cell matrix and real cmux workspace supervision are not exercisable from this product-read-only session; those rest on the now-green model-free suites plus the gate-6 fresh run.

### Residual risks

- The lock's dynamic-path detection still recognizes the module-global 'ROOT / ...' idiom plus declared prefixes and constant/basename token scans; a novel dynamic invocation idiom could evade the lock until reviewed.
- The Claude half of the release matrix validates the sonnet generation at medium effort while production defaults remain opus/high — the explicit user-approved trade-off recorded in the plan assumptions.
- Live cells depend on external agent CLIs and network scenarios; bounded typed retries reduce but cannot eliminate flake-driven blocked verdicts in the fresh 58-cell run.

### Notes for executor

- Finding 1 (blocking, seed) is resolved exactly as recommended: cell_metadata now computes acceptance_seed_sha256 and embeds it in the fingerprint payload and returned metadata (scripts/acceptance_fingerprints.py:556-587), and the new regression 'one canonical seed byte invalidates every cell and blocks stale reuse' (tests/test_release_acceptance.py:505-569) mutates the exact previously-unreferenced file (.vault-meta/last-fold-count.txt in a seed copy), proves all 58 fingerprints change, proves load_resume_results returns zero reused rows, and proves reverting restores the original fingerprints. Passes here.
- Finding 2 (adapter/import bindings) is resolved: scripts/acceptance/adapters.py is now a behavioral ABI dependency (config/acceptance-cells.toml:37), _fragment_payload includes every module-level Import/ImportFrom AST dump (scripts/acceptance_fingerprints.py:347-351), live_runner_behavior_sha256 gained a binding-adapter override path, and both regressions pass ('adapter re-export changes invalidate the common behavioral ABI'; 'fragment hashes include module-level import bindings' shows the rebinding invalidates only cells using that fragment module). The prior exclusion assertion was updated to 'every cell carries registration and adapter binding surfaces'.
- Finding 3 (routing generations) is resolved: _strip_routing_values now maps every routing 'model' value to its registered canonical generation instead of deleting it, while effort keys and the registry stay excluded (scripts/acceptance_fingerprints.py:248-263); the regression 'routing hashes ignore same-generation aliases but retain generation changes' proves Sol→Terra preserves the fingerprint and fable→sonnet changes it. Passes here.
- Finding 4 (suite confirmation) is resolved by the executor's recorded 165/165 run on d3ab3bd; my rerun reproduces only the same single environment-dependent codex validate failure. The optional hardening (making the positive-path check fail distinguishably when cmux is unresolvable so adjacent negative checks cannot pass vacuously in cmux-less environments) was not adopted and remains reasonable follow-up debt.
- Finding 5 (CHANGELOG) is resolved: lines 45-48 now state the epoch-3 semantics (unregistered runtime edges fail the lock check; unknown non-runtime paths neither invalidate evidence nor bypass the graph).
- The extra changes in d3ab3bd are sound and in scope: dead duplicated runner constants were removed while keeping the compatibility imports the test surface uses, and review-dispatch/review-send now strictly recognize UUID-scoped operation handoff files (OPERATION_HANDOFF_RX) and add them to worktree git excludes (OPERATION_HANDOFF_GIT_EXCLUDES), covered by the operation-scoped handoff checks; the working tree is clean.
- Verified in this session on d3ab3bd: tests/test_release_acceptance.py (85 checks, including the four new invalidation regressions), tests/test_live_acceptance_runner.py (including 58/58 prompt byte-parity), tests/test_task_lifecycle.py, tests/test_contract_schemas.py, and scripts/lint-instructions.py — all green.

## Archive boundary

This page keeps validated review findings, executor resolutions, and final verification. Dedicated raw prompts, compressed callback payloads, command logs, sockets, and cmux identifiers are intentionally excluded. Validated findings and executor resolutions are retained as review evidence.
