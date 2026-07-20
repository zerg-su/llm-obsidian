---
type: review
title: "Cross-model review — v2.1.1 final implementation review — 6fb4143a11f2"
address: c-000016
created: 2026-07-20
updated: 2026-07-20
tags:
  - review
  - cross-model
status: resolved
sessions:
  - "019f6ddd-d07e-7a30-b018-f6358753fb91"
review_id: "f1223405-8dec-4afd-bb60-a936f93ce7d1"
reviewer_runtime: "claude"
reviewer_model: "fable"
reviewer_effort: "high"
review_mode: "full"
rounds: 2
verdict: approve
---

# Cross-model review — v2.1.1 final implementation review — 6fb4143a11f2

> [!abstract] Outcome
> **Task:** v2.1.1 final implementation review
> **Final verdict:** `approve`
> **Reviewer:** claude · fable · effort `high`
> **Executor:** codex
> **Mode:** `full` · **rounds:** 2
> **Started:** 2026-07-20T14:24:48Z
> **Updated:** 2026-07-20T14:45:00Z

## Review request

Review the implementation for **v2.1.1 final implementation review** in `task/v2.1.1-code-owned-optimizations` against `v2.1.0` using the `full` cross-model gate.

> [!quote] Original task request
> Perform a full, critical release-gate review of the committed implementation on
> `task/v2.1.1-code-owned-optimizations` against `v2.1.0`. The approved plan is
> `docs/plans/v2.1.1-code-owned-optimization-plan.md`; the current review target
> ends at commit `cd14a20`.
>
> The implementation moves deterministic orchestration into repo-owned code while
> preserving model-owned semantic decisions. Review the complete branch, including:
>
> - content-free per-turn and per-stage timing;
> - content-addressed, fail-closed acceptance evidence and selective reuse;
> - production-generation versus cheaper live-test-model provenance;
> - compact runner-first skill paths and their compatibility references;
> - exact cmux surface lifecycle and direct `/close` live acceptance;
> - deterministic dispatch/reap/review mechanics and recovery behavior;
> - the final exact non-behavioral release-file allowlist.
> - post-review hardening for task/reviewer surface closure, broker completion,
>   pending drive/reap transitions, classifier-safe callbacks, unique local
>   operation handoffs, and canonical acceptance checkpoint retention.
> - the post-approval acceptance invalidation refactor in `cd14a20`: code-owned
>   `tests/` classification, explicit `.gitignore` behavior dependency, separate
>   orchestration contract version, shared cmux support primitives, and
>   scenario-scoped supervisor/lifecycle dependencies.
>
> Critically check correctness, security, privacy, stale-evidence risk, task/lane
> identity, surface cleanup, failure recovery, backward compatibility, missing
> tests, and any place where moving work into code could reduce result quality.
> Unknown changed paths must remain fail-closed. Both exact and prefix
> non-behavioral classifications must be code-restricted and unable to hide
> product changes. Confirm that compatible orchestration-only changes reuse live
> evidence while an orchestration version mismatch fails closed, and that moving
> shared cmux functions does not alter runtime behavior.
>
> Fable previously approved the complete branch through `d27da21`. The executor
> then implemented `cd14a20` after diagnosing over-broad live-evidence
> invalidation. The complete hermetic `make test` suite passes at `cd14a20`, as do
> focused acceptance, live-runner, supervisor/lifecycle, review-dispatch, and
> instruction tests. Six live rows from the prior fingerprint contract were
> discarded rather than guessed; the expensive matrix remains paused until this
> review. Independently inspect the complete branch, with special attention to
> `d27da21..cd14a20`, and run relevant pre-approved tests. Return actionable
> file/line findings. Approve only if the narrowed dependency graph cannot reuse
> stale behavior and is ready for final acceptance without quality loss.

## Round 1 — changes-requested

- Phase: `initial-review`
- Run ID: `f1223405-8dec-4afd-bb60-a936f93ce7d1`
- Received: 2026-07-20T14:34:57Z

### Findings

#### 1. blocking — Declared-elsewhere transitive imports let live cells reuse stale evidence

- File: `config/acceptance-cells.toml:26`
- Evidence:
> load_resume_results (scripts/release-acceptance.py:257) computes 'declared' as the union of all cells' dependencies, so a changed path declared in ANY scenario never triggers the unknown-path fail-closed invalidation; only cells whose own fingerprint includes it re-run. Three cross-cutting modules are declared in one scenario but imported by scripts declared in others: (1) scripts/lifecycle_telemetry.py is declared only under conversation-readonly, yet cmux_agent_supervisor.py:20, cmux_surface_lifecycle.py:19, dispatch-runner.py:42, reap-runner.py:20, and spawn_review.py:38 import it, and its read_object is the control-flow metadata reader for .task-meta.json/.review-meta.json/operation.json (cmux_surface_lifecycle.py:107,364-368,412) — so a lifecycle_telemetry change re-runs only conversation-readonly cells while dispatch-review-reap and cmux-lifecycle cells reuse evidence whose actual behavior changed. (2) scripts/task_contract.py is declared only under dispatch-review-reap, yet cmux-lifecycle declares cmux_agent_supervisor.py and cmux_surface_lifecycle.py which both import it (normalize/validate_handoff/v3_session_is_bound), so a task_contract change reuses cmux-lifecycle (close) evidence. (3) scripts/turn_telemetry.py and lifecycle_telemetry.py are imported by hooks/run-hook.py (run-hook.py:17-18), which unsafe-web declares without either module, so unsafe-web rows reuse across a run-hook dependency change. This directly violates the gate 'the narrowed dependency graph cannot reuse stale behavior'; the sibling modules vault_schema.py/plan_lifecycle.py/lib_sanitize.py/cmux_task_watchdog.py stay safe today only because they are declared nowhere and therefore hit the unknown-path fail-closed branch — the first scenario that declares one of them opens the identical hole for every other consumer.
- Recommendation:
> Add scripts/lifecycle_telemetry.py, scripts/task_contract.py, and scripts/turn_telemetry.py to global_dependencies (they are imported by nearly every declared runner, and the existing cd14a20 matrix assertions still hold), or complete each scenario's declared closure. Then add a hermetic test that computes the local-module import closure of every declared dependency script (including hooks/run-hook.py and skills/*/scripts) and asserts each imported repo module is either present in the dependencies of every cell that (transitively) declares the importer, or absent from the entire declared set so it stays fail-closed. That locks the invariant against future single-scenario declarations.

#### 2. nit — generation\_routes.exclude is validated but never applied

- File: `config/acceptance-cells.toml:47`
- Evidence:
> production_generations (scripts/acceptance_fingerprints.py:224-237) requires include to equal both production runtimes and exclude to be a list, but exclude's contents are never consulted; generations come solely from config.runtime_default. The populated exclude = \["roles.review.codex", "roles.review.claude"\] reads as if editing it changes fingerprint scope, which could mislead a maintainer of the exact file that governs evidence invalidation.
- Recommendation:
> Either enforce exclude semantics in code (reject unknown entries and document that review roles never contribute generations) or replace the field with a comment so the manifest carries no dead configuration.

### Executor resolution

> [!note] Resolution snapshot
> # Review resolution
>
> ## Blocking: transitive imports could reuse stale cell evidence
>
> Resolved. `scripts/lifecycle_telemetry.py`, `scripts/task_contract.py`, and
> `scripts/turn_telemetry.py` are now global behavioral dependencies because
> they are shared by declared runners in multiple scenarios. A hermetic
> regression walks every cell's declared Python dependencies, computes their
> transitive local-module import closure, and fails if a module declared anywhere
> is missing from a consuming cell. Modules declared nowhere retain the existing
> unknown-path fail-closed behavior.
>
> ## Nit: generation_routes.exclude was dead configuration
>
> Resolved. The unused field was removed. `generation_routes` now accepts only
> the exact two production runtime defaults, and the fast `check` command
> validates this contract. A regression proves unknown/dead route fields fail
> closed.
>
> ## Verification
>
> - `python3 tests/test_release_acceptance.py` — passed.
> - `make test` — passed, including review-dispatch 159/159.

### Verification gaps

- Live acceptance evidence at cd14a20 does not exist yet: six live rows from the prior fingerprint contract were discarded and the expensive matrix is paused pending this review, so only hermetic suites prove the branch today. The affected live cells (and the production-routing proof for Opus/Fable and Sol) must run before the final release gate, per plan step 7.
- No automated test asserts that scenario dependency declarations cover the local import closure of declared scripts; the blocking finding was found by manual import mapping and can silently regress.

### Residual risks

- tests/ as a non-behavioral prefix cannot hide installed product changes, but a tests/-only change does alter what a live nested reviewer may execute (pre-approved python3 tests/test_*.py) inside dispatch-review-reap cells, so reused evidence can differ from a fresh run's verification depth. Acceptable given the code-restricted allowlist, but worth remembering when interpreting reused rows.
- The generation snapshot has no TTL by design, so silent provider drift behind an unchanged hosted alias is accepted; evidence age is visible in schema-2 rows and --explain-selection as the plan requires.
- Compatible orchestration-only changes to release-acceptance.py/acceptance_fingerprints.py reuse live evidence without a version bump by design; correctness of the 'compatible' judgment rests on review discipline plus the explicit orchestration_contract_version, not on code.

### Notes for executor

- Independently verified the full branch, with focus on d27da21..cd14a20. The moved cmux primitives in scripts/cmux_agent_support.py are verbatim relative to the supervisor originals, and test_task_lifecycle.py asserts function identity, so the move cannot alter runtime behavior.
- All pre-approved suites pass locally: release-acceptance, live-acceptance-runner, task-lifecycle, runtime-hooks, task-sessions, queue-session-exit, pipeline-events, research-isolation, dispatch-resolver, reap-send-runner, review-operation-namespacing, session-preflight, skill-budget, review-dispatch (159), and lint-instructions.
- The fail-closed direction held everywhere else I probed: unknown/undeclared changed paths, non-ancestor prior commits, schema-1 reports, orchestration contract mismatch, tampered rows, dirty behavioral worktrees, and code-restricted non-behavioral allowlists (exact paths, tests/ prefix, orchestration set) all invalidate or reject rather than reuse.
- The close-skill compaction is faithful: scripts/queue-session-exit.py reproduces the previous inline shell exactly (env-only Codex detection, exact CMUX_SURFACE_ID, ctrl+u+/exit+Enter vs bounded-backspace+/exit+Tab, never close-surface) and is covered by tests and the Makefile pipeline-runner target.
- The two ERROR: lines in test_task_lifecycle.py output are stderr from intentional negative-path subprocess fixtures (cmux_surface_lifecycle die()); the suite itself passes.

## Round 2 — approve

- Phase: `verify-fixes`
- Run ID: `ccfa6351-31f8-49f8-8aa0-f4d791a1af1a`
- Received: 2026-07-20T14:45:00Z

### Findings

No findings.
### Executor resolution

No resolution was required or recorded for this round.

### Verification gaps

- Live acceptance evidence still does not exist for the release gate: f3c998d adds scripts/lifecycle_telemetry.py, scripts/task_contract.py, and scripts/turn_telemetry.py to every cell fingerprint, so no prior live row can be reused and the full matrix (plus the production-routing proof for Opus/Fable and Sol) must run fresh before final acceptance, per plan step 7. This is the planned next step, not a defect.

### Residual risks

- The closure test resolves and traverses only modules declared by some cell, so it would not see a chain where an undeclared bridge module (e.g. plan_lifecycle.py or vault_schema.py) later gains an import of a declared module; the bridge itself stays fail-closed but the imported declared module would not invalidate consumers through that chain. Today all such bridge modules import stdlib only (verified), so the graph is clean; this is a narrow future-regression vector, not a current defect.
- Carried from the initial review: tests/ non-behavioral reuse can differ from a fresh run's nested-reviewer verification depth; the generation snapshot has no TTL by design (evidence age stays visible); and compatible orchestration-only changes reuse evidence on review discipline plus the explicit orchestration_contract_version.

### Notes for executor

- Blocking finding resolved and independently verified. config/acceptance-cells.toml now declares the three cross-cutting modules in global_dependencies, and I re-walked the import map of every declared runner (supervisor, surface lifecycle, dispatch/reap runners, spawn_review, run-hook, research-isolation): no declared-somewhere-but-missing edge remains; modules declared nowhere (vault_schema.py, plan_lifecycle.py, lib_sanitize.py, cmux_task_watchdog.py) correctly retain unknown-path fail-closed invalidation.
- The new hermetic regression in tests/test_release_acceptance.py implements exactly the requested invariant: it AST-walks the transitive local import closure of every declared Python dependency per cell and fails when a module declared by any cell is missing from a consuming cell. Its detection logic would have flagged the pre-fix manifest (lifecycle_telemetry was declared by conversation-readonly rows and imported by dispatch-review-reap runners), so the pass is not vacuous.
- Nit resolved: generation_routes.exclude is removed, production_generations now rejects any shape other than exactly the two production runtime includes, the fast check command validates it (production_generations moved before the check early-return in release-acceptance.py), and a negative test covers a reintroduced dead field.
- Re-ran the relevant suites at f3c998d: test_release_acceptance.py (48 checks incl. the two new ones), test_live_acceptance_runner.py, test_session_preflight.py, and lint-instructions all pass; the working tree is clean.

## Archive boundary

This page keeps validated review findings, executor resolutions, and final verification. Dedicated raw prompts, compressed callback payloads, command logs, sockets, and cmux identifiers are intentionally excluded. Validated findings and executor resolutions are retained as review evidence.
