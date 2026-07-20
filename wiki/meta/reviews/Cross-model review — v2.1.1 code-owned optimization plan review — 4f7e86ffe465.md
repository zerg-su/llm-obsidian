---
type: review
title: "Cross-model review — v2.1.1 code-owned optimization plan review — 4f7e86ffe465"
address: c-000013
created: 2026-07-19
updated: 2026-07-19
tags:
  - review
  - cross-model
status: resolved
sessions:
  - "019f6ddd-d07e-7a30-b018-f6358753fb91"
review_id: "0f9c5151-d3d4-4bc6-80c1-6ae1db6b385a"
reviewer_runtime: "claude"
reviewer_model: "fable"
reviewer_effort: "high"
review_mode: "full"
rounds: 2
verdict: approve
---

# Cross-model review — v2.1.1 code-owned optimization plan review — 4f7e86ffe465

> [!abstract] Outcome
> **Task:** v2.1.1 code-owned optimization plan review
> **Final verdict:** `approve`
> **Reviewer:** claude · fable · effort `high`
> **Executor:** codex
> **Mode:** `full` · **rounds:** 2
> **Started:** 2026-07-19T22:14:37Z
> **Updated:** 2026-07-19T22:24:33Z

## Review request

Review the implementation for **v2.1.1 code-owned optimization plan review** in `task/v2.1.1-code-owned-optimizations` against `v2.1.0` using the `full` cross-model gate.

> [!quote] Original task request
> # Fable review: possible v2.1.1 code-owned optimizations
>
> Review only the committed plan at
> `docs/plans/v2.1.1-code-owned-optimization-plan.md` (commit `7c4ea8d`).
> Do not review v2.1.0 as an implementation release gate and do not propose or
> edit product code.
>
> The user has frozen v2.1.0 at `94cc6cf`. The question is whether the proposed
> v2.1.1 optimizations move all safely deterministic work into code while
> preserving or improving result quality:
>
> 1. provider-native, content-free message/request telemetry for Claude Code and
>    Codex plus stage latency aggregation;
> 2. content-addressed acceptance-cell reuse across commits;
> 3. runner-first skill compaction with enforceable body budgets.
>
> Critically inspect the plan for:
>
> - any quality regression, lost context, stale evidence, or unsafe cache reuse;
> - whether built-in Claude/Codex OpenTelemetry supports the proposed data and
>   configuration path without parsing conversation text;
> - privacy, local attack surface, config ownership, installer reversibility,
>   portability, and fail-open observability boundaries;
> - missing dependencies, schemas, migrations, compatibility constraints, unit
>   tests, live tests, or rollback conditions;
> - which tasks should remain semantic/model-owned rather than code-owned;
> - unnecessary complexity or a simpler design with equivalent evidence;
> - whether the three workstreams should be reordered, split, or narrowed.
>
> Return actionable findings against plan lines. A blocking finding means the
> plan must change before implementation. Warnings and nits should explain a
> concrete quality-preserving improvement. Approve only if the plan is safe,
> implementable, and sufficiently testable.

## Round 1 — changes-requested

- Phase: `initial-review`
- Run ID: `0f9c5151-d3d4-4bc6-80c1-6ae1db6b385a`
- Received: 2026-07-19T22:20:45Z

### Findings

#### 1. warning — Dropping the freshness bound leaves reused live evidence unguarded against silent hosted-model drift

- File: `docs/plans/v2.1.1-code-owned-optimization-plan.md:69`
- Evidence:
> The committed revision required a bounded freshness window precisely because hosted aliases like gpt-5.6-sol have no immutable snapshot identifier; this revision states 'There is no TTL' and removes both the guard and its rationale. The generation snapshot only detects local routing-config changes, so a provider-side silent update to the model behind an unchanged alias never invalidates old pass rows, and reuse is unbounded in age. Runtime/cmux version churn in the fingerprint bounds age only incidentally.
- Recommendation:
> Either restore a generous freshness bound for reuse eligibility, or state the accepted risk explicitly in the plan and require the schema-2 report and selection explanation to surface each reused row's evidence age so the final release gate shows how old reused evidence is.

#### 2. warning — Workstream 1 does not state that the turn marker and Stop emission must go through the shared runtime adapter to cover Codex

- File: `docs/plans/v2.1.1-code-owned-optimization-plan.md:33`
- Evidence:
> CLAUDE.md says prompt/session hooks are Claude-specific and guard-exit under Codex; Codex coverage of UserPromptSubmit exists only via the shared runtime adapter (docs/runtime-capabilities.md line 18, tests/test_runtime_hooks.py line 38). An implementation placed in the Claude-only hook layer would silently record zero Codex turns while every listed test still passes, contradicting the original ask of telemetry for both runtimes; verification item 4 names no Codex-turn case.
- Recommendation:
> Name both runtimes explicitly in Workstream 1 item 2 (marker and model-turn emission via the shared runtime adapter) and add an explicit Codex UserPromptSubmit-to-Stop turn fixture to the verification item 4 test list.

#### 3. warning — Generation recorded by override-run Claude live cells is ambiguous and could either mislabel evidence or make the matrix unsatisfiable

- File: `docs/plans/v2.1.1-code-owned-optimization-plan.md:63`
- Evidence:
> Verification item 7 runs live cells with Claude Sonnet while production routing is opus/fable, and item 3 says Claude generations are registered large-release names. Codex is covered by the sol/terra-to-codex:5.6 canonicalization, but the plan never says what generation a Sonnet-override evidence row carries: recording the production generation labels evidence with a generation that never executed, while recording 'sonnet' means the row can never match the required production-generation fingerprint and the release matrix cannot be satisfied by the planned live runs.
- Recommendation:
> State explicitly that acceptance model overrides are ignored for the fingerprint generation (the row carries the effective production generation) while the actually executed model stays in provenance, and name this exact Claude-override case in the verification item 5 'ignored effort and test overrides' tests.

#### 4. warning — Unrelated uncommitted product-code changes are riding on the plan-only branch

- File: `scripts/task\_sessions.py:784`
- Evidence:
> The branch's committed diff against v2.1.0 is docs-only, and the task prompt forbids editing product code, yet the working tree carries unstaged changes to scripts/task_sessions.py (a cmux workspace-retry fallback in spawn_right plus surface_workspace) and tests/test_task_sessions.py. The code is sound and python3 tests/test_task_sessions.py passes (50 checks), but it is unrelated to the plan and currently unowned by any commit, so it risks being amended into the plan commit or lost.
- Recommendation:
> Confirm ownership (it looks like coordinator dispatch-plumbing repair for the current cmux version), commit it separately with its own message and test evidence, and keep it out of the plan-revision commit; revert it if it was not intended on this branch.

#### 5. warning — The plan revision under review exists only as unstaged edits

- File: `docs/plans/v2.1.1-code-owned-optimization-plan.md:3`
- Evidence:
> The task prompt points at commit 7c4ea8d, HEAD is f3f5823, and the revision actually proposed for this renewed review (147-line condensed plan) is uncommitted working-tree state. An approval gate that references no stable SHA cannot later prove which plan text was accepted before implementation.
- Recommendation:
> Commit the revised plan before or at acceptance so the approved revision has a stable commit, and record that SHA as the implementation baseline in the plan status line.

#### 6. nit — Unknown-changed-path detection does not define its comparison baseline

- File: `docs/plans/v2.1.1-code-owned-optimization-plan.md:58`
- Evidence:
> Workstream 2 item 2 says unknown changed paths invalidate all live cells, but not what 'changed' is measured against. Reuse is cross-commit within a worktree, so the safe set must include staged, unstaged, and untracked paths relative to each evidence row's provenance commit; otherwise dirty-tree edits to undeclared files could be reused past.
- Recommendation:
> Specify that the changed-path set is computed against the evidence row's provenance commit and includes staged, unstaged, and untracked paths, and add a dirty-tree case to the verification item 5 unknown-dependency tests.

#### 7. nit — Stop entry with no marker at all is unspecified

- File: `docs/plans/v2.1.1-code-owned-optimization-plan.md:34`
- Evidence:
> Item 4 covers a stale marker at session start or new prompt, and item 3 covers a missing session ID, but not Stop firing with no marker present (hook installed mid-session, marker cleaned up after a crash). It should be a silent no-op rather than an incomplete event, or incomplete counts will be inflated.
- Recommendation:
> Add one sentence: Stop with no marker for the exact session is a silent no-op, and include that case in the verification item 4 tests.

#### 8. nit — Rollback wording overstates v2.1.0 tolerance of a schema-2 report

- File: `docs/plans/v2.1.1-code-owned-optimization-plan.md:145`
- Evidence:
> v2.1.0 load_resume_results raises AcceptanceError ('use --restart') on any report whose schema_version is not 1 (scripts/release-acceptance.py:176-185); it does not silently ignore schema-2 reuse. Recovery exists but requires an operator action.
- Recommendation:
> Say that after rollback the first v2.1.0 acceptance run needs --restart or deletion of the derived .vault-meta/acceptance report, which is acceptable because it is regenerable derived state.

### Executor resolution

> [!note] Resolution snapshot
> # Executor resolution
>
> Reviewed commit: `ef3f55f` (with separate mechanism repair `a418abb`).
>
> 1. **Hosted-model freshness — applied with the user-approved no-TTL policy.**
>    The plan now states the accepted silent-drift risk and requires evidence age
>    in every reused schema-2 row and selection explanation.
> 2. **Shared runtime adapter — applied.** Turn markers and Stop emission now
>    explicitly target the shared Claude/Codex adapter, with a Codex turn fixture.
> 3. **Sonnet override generation — applied.** The fingerprint carries production
>    generation while actual Sonnet/Terra execution remains in provenance; the
>    exact Claude case is required in tests.
> 4. **Unowned cmux repair — applied.** The exact-workspace compatibility fix and
>    regression test are isolated in commit `a418abb`; focused test passes 50/50.
> 5. **Uncommitted review target — applied.** The reviewed plan is committed as
>    `ef3f55f`; the plan explains that validated review history binds the exact
>    implementation baseline without a self-referential embedded SHA.
> 6. **Changed-path baseline — applied.** It is relative to each evidence row's
>    provenance commit and includes staged, unstaged, and non-ignored untracked
>    paths, with required dirty-tree tests.
> 7. **Stop without marker — applied.** It is a silent no-op with a regression
>    fixture.
> 8. **Rollback wording — applied.** The plan now requires v2.1.0 `--restart` or
>    deletion of the derived schema-2 report.
>
> The optional integrity note is also applied through `row_integrity_sha256` over
> the canonical typed result, fingerprint, and provenance. Skill savings are
> explicitly deterministic size/token estimates rather than provider token data.

### Verification gaps

- The no-TTL and no-collector decisions are attributed to a user clarification I cannot see; I reviewed them on their merits and flagged the residual drift risk rather than assuming the clarification's exact scope.

### Residual risks

- Canonicalizing gpt-5.6-sol and gpt-5.6-terra as one codex:5.6 generation means Terra live evidence stands in for Sol production behavior; intra-generation variant differences are unguarded by design, with only a separate routing-selection proof.
- Turn telemetry from product-read-only reviewer sessions will likely be absent (no writable vault meta), so reviewer-role percentiles may systematically under-report; acceptable under the best-effort invariant but worth remembering when reading reports.
- Runtime and cmux version churn in the fingerprint incidentally limits evidence age but is not a freshness guarantee against server-side model drift.

### Notes for executor

- I reviewed the working-tree (unstaged) plan revision as the target, since it is explicitly marked 'proposed for renewed Fable/high review'; the task prompt's pointer to commit 7c4ea8d is stale relative to the tree.
- Verified against the repo that every mechanism the plan builds on exists: pipeline-events.jsonl fixed schema and rotation, Codex UserPromptSubmit/Stop parity in tests/test_runtime_hooks.py, release-acceptance.py checkpoint/matrix_fingerprint/--restart, the ignored sandbox-local acceptance model overrides in live-acceptance-runner.py, and check-skill-budget.py as the existing description budget checker.
- Ran python3 tests/test_task_sessions.py (50 checks passed) and python3 scripts/lint-instructions.py (OK).
- Consider one more plan sentence defining how schema-2 detects 'tampered/manually edited' rows (e.g., a row integrity hash over fingerprint, verdict, and evidence), since reuse eligibility depends on it.
- The dropped provider-native token/cost telemetry means skill-compaction savings are now evidenced by deterministic size estimates rather than measured tokens; that is coherent with the revised scope, just make sure the before/after evidence in verification item 6 is framed as estimates.

## Round 2 — approve

- Phase: `verify-fixes`
- Run ID: `aab40813-d6f6-4084-bed8-052ff11b4885`
- Received: 2026-07-19T22:24:33Z

### Findings

No findings.
### Executor resolution

No resolution was required or recorded for this round.

### Verification gaps

- The per-commit file split between ef3f55f and a418abb is not directly inspectable with the pre-approved git commands; I verified it indirectly: the working tree is clean, the cumulative v2.1.0...HEAD diff is exactly the plan (168 lines) plus the previously reviewed task_sessions.py (+50) and test (+29) changes, and both commits carry matching dedicated messages.

### Residual risks

- No-TTL reuse against mutable hosted aliases remains an accepted, now-documented risk; evidence age visibility mitigates but does not remove it.
- Canonicalizing gpt-5.6-sol and gpt-5.6-terra as one codex:5.6 generation means Terra live evidence stands in for Sol production behavior; intra-generation variant differences remain unguarded by design, with only the separate production-routing proof.
- Turn telemetry from product-read-only reviewer sessions may be systematically absent under the best-effort invariant, so reviewer-role percentiles can under-report.

### Notes for executor

- All eight prior findings are resolved in the committed plan at HEAD ef3f55f. 1) The no-TTL decision now states the accepted silent-drift risk and requires evidence age in every reused schema-2 row and in --explain-selection (plan lines 78-81, 84, 141). 2) Workstream 1 item 2 names the shared Claude/Codex runtime adapter and verification item 4 adds the explicit Codex UserPromptSubmit-to-Stop fixture (lines 35-37, 136-137). 3) Workstream 2 item 3 states that Sonnet/Terra overrides are excluded from fingerprint generation, with the actual model in provenance, and the exact Claude case is in the verification item 5 tests (lines 72-75, 140). 4) The cmux workspace-retry repair is isolated in commit a418abb with its own regression test; python3 tests/test_task_sessions.py passes 50 checks against the committed state. 5) The reviewed plan is committed as ef3f55f; binding the approved SHA through validated review history rather than a self-referential embedded SHA is sound — this verification applies to HEAD ef3f55f. 6) The changed-path baseline is the evidence row's provenance commit including staged, unstaged, and non-ignored untracked paths, with dirty-tree tests (lines 64-66, 141-142). 7) Stop with no marker is a silent no-op with a test case (lines 46-47, 134). 8) Rollback now correctly requires v2.1.0 --restart or deletion of the derived schema-2 report (lines 164-168).
- The optional suggestions were also applied: row_integrity_sha256 with loader-side recomputation (lines 84-87) backs the tampered/manually-edited exclusion, and skill savings are framed as deterministic size/token estimates (lines 145-146).
- python3 scripts/lint-instructions.py passes (OK). No new issues were introduced by the edits; the plan is safe, implementable, and sufficiently testable, and Workstream 1 item 2 through verification item 4 now close the only silent-coverage gap I had flagged.

## Archive boundary

This page keeps validated review findings, executor resolutions, and final verification. Dedicated raw prompts, compressed callback payloads, command logs, sockets, and cmux identifiers are intentionally excluded. Validated findings and executor resolutions are retained as review evidence.
