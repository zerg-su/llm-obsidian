---
type: review
title: "Cross-model review — v2.1.2 semantic acceptance refactor — 6a12aeccde78"
address: c-000022
created: 2026-07-21
updated: 2026-07-21
tags:
  - review
  - cross-model
status: resolved
sessions:
  - "019f6ddd-d07e-7a30-b018-f6358753fb91"
review_id: "4b469dbc-09a6-4451-bbff-d09d8be5a5fd"
reviewer_runtime: "claude"
reviewer_model: "fable"
reviewer_effort: "high"
review_mode: "full"
rounds: 2
verdict: approve
related:
  - "[[2026-07-21-085654-llm-obsidian-v2-1-2-semantic-acceptance-refactor]]"
---

# Cross-model review — v2.1.2 semantic acceptance refactor — 6a12aeccde78

> [!abstract] Outcome
> **Task:** v2.1.2 semantic acceptance refactor
> **Final verdict:** `approve`
> **Reviewer:** claude · fable · effort `high`
> **Executor:** codex
> **Mode:** `full` · **rounds:** 2
> **Started:** 2026-07-21T10:05:48Z
> **Updated:** 2026-07-21T10:18:30Z
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

## Round 1 — approve

- Phase: `initial-review`
- Run ID: `09388b8d-c2e8-4233-a595-20e3fe4abb4a`
- Received: 2026-07-21T10:12:34Z

### Findings

#### 1. nit — Stale release-notes claim: 'All 58 v2.1.1 prompts remain byte-identical on pinned inputs' is no longer true after the reviewed v2.1.2 fixture revision.

- File: `CHANGELOG.md:28`
- Evidence:
> Commit 3607013 deliberately revised five skill fixtures (backlog, distill-runbook, learn, reap, wiki-query), changing exactly those 10 prompt hashes and re-baselining to evals/acceptance/prompt-baseline-v2.1.2.json (verified hash-by-hash against the prior v2.1.1 baseline; the other 48 are identical). The changelog still asserts full byte-parity with v2.1.1 and contains no entry documenting the fixture revision, so the published release notes would make a false claim about the release gate's parity invariant.
- Recommendation:
> Reword line 28 to state that the refactor itself preserved byte-parity and add one line documenting the subsequent reviewed five-fixture revision under the v2.1.2 baseline (operational clarifications; expected contracts unchanged).

### Executor resolution

> [!note] Resolution snapshot
> # Resolution — v2.1.2 lifecycle and fixture review
>
> 1. **Applied — release-note accuracy.** `CHANGELOG.md` now distinguishes the
>    byte-identical acceptance refactor from the later reviewed corrections to
>    five fixture pairs. It records that the expected contracts remain unchanged
>    and that the other 48 rendered prompts retain v2.1.1 parity.
> 2. **Confirmed — exact workspace lifecycle.** The executor ran the production
>    helpers against live cmux: a temporary workspace was created, its short ref
>    was bound to exact workspace/window UUIDs, the exact workspace UUID was
>    closed, and a subsequent workspace listing proved it absent.
> 3. **Confirmed — executor verification.** `make test` passed completely,
>    including 166/166 review-dispatch checks, on commit `3607013` before this
>    documentation-only correction. `git diff --check` passes afterward.
> 4. **Repaired — exact resolution handoff.** The first deterministic drive
>    exposed a repo-owned compatibility defect: a stale legacy unscoped
>    resolution was compared with the current operation-scoped UUID resolution
>    and blocked the exact handoff. The resolver now prefers an existing exact
>    UUID file and consults the generic fallback only when the exact file is
>    absent. A regression preserves legacy fallback and ambiguity rejection while
>    proving a conflicting stale generic cannot override the exact operation.
>
> The lifecycle and fixture code reviewed above is unchanged. The narrow
> review-dispatch repair changes only the already-invalidated
> dispatch/review/reap scenario and has passed all 166 review-dispatch tests.

### Verification gaps

- The known environment-dependent codex-review-supervisor-valid check in tests/test_review_dispatch.sh still fails in this cmux-less reviewer sandbox (165/166; identical to every prior round and previously confirmed green in the executor environment); everything else passes here.
- cmux workspace container close/bind behavior is validated against mocked cmux plumbing; the real CLI behavior is only provable in the live gate, where the dispatch-workspace cells exercise it.

### Residual risks

- Fixture wording changes are validated structurally, not behaviorally, until the affected cells rerun in the live matrix; the five revised cells (×2 runtimes) plus dispatch-review-reap cells will rerun under their new fingerprints.
- Prior standing risks unchanged: live cmux CLI behavior is provable only in the gate, and the Claude matrix half validates the sonnet generation per the user-approved trade-off.

### Notes for executor

- The prompt change passes the plan's gate-2 exception correctly: prompting.py and all expected contracts are unchanged; the 10 changed hashes trace exactly to the five fixture clarifications, each carrying a new model-free regression ('wiki-query fixture provisions the normal dense path before degradation', 'distill-runbook fixture satisfies the skill's meaningful-history floor', 'learn fixture refreshes derived state before whole-vault validation', plus the strengthened backlog/reap fixture checks), and both the parity test and docs/acceptance-architecture.md reference the v2.1.2 baseline. The new fixture instructions create no undeclared runtime edges — scripts/reindex.py is already in every cell's closure via the hooks/hooks.json → stop.sh → stop-hook.py chain, and retrieve.py is a declared retrieval-scenario dependency.
- The workspace-task lifecycle fix is sound and correctly scoped: scripts/cmux_workspace_lifecycle.py binds exact workspace/window UUIDs from the child surface with drift checks, closes the workspace container with proof it disappeared from its window (bounded two-attempt loop, fail-closed), and close_task_container falls back to exact-surface close for split placement; dispatch-runner persists the container ownership in task metadata and cmux_surface_lifecycle closes the container on task completion. The module is registered as a dispatch-review-reap scenario dependency, so exactly those cells re-fingerprint and rerun.
- Verified in this session on 3607013: tests/test_release_acceptance.py, tests/test_live_acceptance_runner.py (58/58 against the v2.1.2 baseline), tests/test_task_sessions.py (65), tests/test_dispatch_runner.py (including 'workspace dispatch persists exact container ownership'), tests/test_task_lifecycle.py, tests/test_daily_pipeline.py, tests/test_contract_schemas.py, and scripts/lint-instructions.py — all green; working tree clean.

## Round 2 — approve

- Phase: `verify-fixes`
- Run ID: `bada85ab-4075-4f72-923e-1ae001755931`
- Received: 2026-07-21T10:18:30Z

### Findings

No findings.
### Executor resolution

No resolution was required or recorded for this round.

### Verification gaps

- The standing environment-dependent codex-review-supervisor-valid check fails in this cmux-less reviewer sandbox (165/166, identical to every prior round); the executor records 166/166 on their side and additionally confirmed the workspace bind/close helpers against live cmux, which closes my prior live-CLI verification gap for the supervisor path.

### Residual risks

- Fixture wording changes remain structurally validated until the affected cells rerun in the live matrix under their new fingerprints.
- The Claude half of the release matrix validates the sonnet generation at medium effort per the user-approved trade-off recorded in the plan assumptions.

### Notes for executor

- The CHANGELOG nit is resolved as recommended (commit 0713404): lines 26-29 now scope the byte-parity claim to the refactor itself, and lines 30-33 document the reviewed five-fixture revision under the v2.1.2 baseline with expected contracts unchanged and the other 48 prompts noted as v2.1.1-identical.
- The additional narrow repair (commit 3a7b30a, prepare_drive_resolution in skills/review-dispatch/scripts/spawn_review.py:1841-1877) is correct and in scope: when the exact UUID-scoped resolution file exists it is used exclusively, so a conflicting stale legacy generic can no longer block or override the exact operation handoff; the generic path remains a fallback only when the exact file is absent and exactly one active drive handoff exists, preserving the ambiguity rejection. The regression at tests/test_review_dispatch.sh:596-638 proves all three behaviors (legacy fallback with cleanup, exact-over-stale-generic precedence, ambiguous generic rejected). This falls inside the failure-to-repair auto-repair boundary and only touches the dispatch-review-reap scenario that commit 3607013 had already re-fingerprinted.
- Re-verified in this session on 3a7b30a: tests/test_release_acceptance.py (including 'generated code/data/registration dependency lock is current' after the spawn_review change), tests/test_review_dispatch.sh (165/166 with only the known environment-dependent check), and scripts/lint-instructions.py — all green; working tree clean.

## Archive boundary

This page keeps validated review findings, executor resolutions, and final verification. Dedicated raw prompts, compressed callback payloads, command logs, sockets, and cmux identifiers are intentionally excluded. Validated findings and executor resolutions are retained as review evidence.
