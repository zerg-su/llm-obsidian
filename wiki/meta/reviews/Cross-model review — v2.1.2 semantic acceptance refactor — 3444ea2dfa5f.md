---
type: review
title: "Cross-model review — v2.1.2 semantic acceptance refactor — 3444ea2dfa5f"
address: c-000029
created: 2026-07-21
updated: 2026-07-21
tags:
  - review
  - cross-model
status: resolved
sessions:
  - "019f6ddd-d07e-7a30-b018-f6358753fb91"
review_id: "9062dd9d-4402-472e-8a37-cf88011c20d1"
reviewer_runtime: "claude"
reviewer_model: "opus"
reviewer_effort: "high"
review_mode: "full"
rounds: 1
verdict: approve
related:
  - "[[2026-07-21-085654-llm-obsidian-v2-1-2-semantic-acceptance-refactor]]"
---

# Cross-model review — v2.1.2 semantic acceptance refactor — 3444ea2dfa5f

> [!abstract] Outcome
> **Task:** v2.1.2 semantic acceptance refactor
> **Final verdict:** `approve`
> **Reviewer:** claude · opus · effort `high`
> **Executor:** codex
> **Mode:** `full` · **rounds:** 1
> **Started:** 2026-07-21T12:52:26Z
> **Updated:** 2026-07-21T12:57:35Z
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
- Run ID: `1fa012a6-f66c-42c5-a7f0-ae1b97c06f00`
- Received: 2026-07-21T12:57:35Z

### Findings

No findings.
### Executor resolution

No resolution was required or recorded for this round.

### Verification gaps

- Gate 2 (byte-for-byte prompt preservation): the passing test at tests/test_live_acceptance_runner.py:219-221 compares the 58 freshly rendered prompts against the committed evals/acceptance/prompt-baseline-v2.1.2.json. Because the old runner is already deleted on this branch, static read-only review cannot independently confirm that this baseline was captured from the pre-refactor runner rather than regenerated from the new prompting.py. If the baseline was regenerated from the new code, the check is self-referential and would not catch a silent prompt drift introduced during the port. Executor should confirm the baseline was frozen from the historical runner before deletion (per plan Review/Test Gate 2).
- Gate 6-8 (live 58-cell matrix on Terra/Sonnet, medium, 2x5): this is a runtime execution gate that cannot be exercised in a product-read-only static review. Approval covers code correctness only; the fresh 58/58 pass, zero owned orphan surfaces/workspaces, and valid telemetry/report must still be demonstrated before the release fast-forward and v2.1.2 tag.
- Could not run scripts/acceptance_dependencies.py verify or `make test` directly (not in the pre-approved command set; both denied). Lock currency is instead confirmed indirectly via tests/test_release_acceptance.py:538-542 (verify_dependency_lock regenerates and equals the committed lock), which passed.

### Residual risks

- This branch bundles the unreleased v2.1.1 work plus the v2.1.2 acceptance refactor into one main...HEAD delta (~23.8k insertions across 152 files). The acceptance refactor itself is well-tested; the non-acceptance v2.1.1 surface (e.g. large research-isolation.py, model_routing, task_sessions changes) was reviewed only for test-green status here and relies on its own archived v2.1.1 review. Ensure the release notes/tag scope reflect the combined delta.
- environment_contract()/command_version() fold local cmux/claude/codex --version strings into the cell fingerprint via scoped_environment_contract; host toolchain upgrades will (correctly) invalidate evidence and force reruns. Expected behavior, but worth noting for anyone surprised by broad reruns after a CLI update.

### Notes for executor

- The dispatched .task-prompt.md still describes the pre-implementation PLAN review against d17ffc9; the branch already carries the full implementation. I reviewed it as an implementation review against main per the review-meta phase (initial-review, full). Flagging in case the stale prompt matters for reap bookkeeping.
- Confirmed the approved-plan deliverables are present and test-backed: acceptance/ package split with thin scripts/live-acceptance-runner.py wrapper (no LIVE_RUNNER_* tables remain), semantic per-cell fingerprints with orchestration allowlist, generated fail-closed dependency lock incl. dynamic-prefix declarations, seed isolation with deterministic synthetic commit (scripts/acceptance/sandbox.py), bounded typed-transient retries, activity heartbeat timeout, and exact-surface orphan cleanup.
- prompting.py cleanup_contract branches for the close and autoresearch skills use an f-string prefix with no interpolation (lines ~27 and ~32). Purely cosmetic; does not affect rendered bytes or the baseline test. Left out of findings as non-material.

## Archive boundary

This page keeps validated review findings, executor resolutions, and final verification. Dedicated raw prompts, compressed callback payloads, command logs, sockets, and cmux identifiers are intentionally excluded. Validated findings and executor resolutions are retained as review evidence.
