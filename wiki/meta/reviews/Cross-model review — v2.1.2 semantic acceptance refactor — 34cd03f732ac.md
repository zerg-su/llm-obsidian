---
type: review
title: "Cross-model review — v2.1.2 semantic acceptance refactor — 34cd03f732ac"
address: c-000025
created: 2026-07-21
updated: 2026-07-21
tags:
  - review
  - cross-model
status: resolved
sessions:
  - "019f6ddd-d07e-7a30-b018-f6358753fb91"
review_id: "9042fe05-6b3b-4a44-b6f7-e130ae781022"
reviewer_runtime: "claude"
reviewer_model: "fable"
reviewer_effort: "high"
review_mode: "light"
rounds: 1
verdict: approve
related:
  - "[[2026-07-21-085654-llm-obsidian-v2-1-2-semantic-acceptance-refactor]]"
---

# Cross-model review — v2.1.2 semantic acceptance refactor — 34cd03f732ac

> [!abstract] Outcome
> **Task:** v2.1.2 semantic acceptance refactor
> **Final verdict:** `approve`
> **Reviewer:** claude · fable · effort `high`
> **Executor:** codex
> **Mode:** `light` · **rounds:** 1
> **Started:** 2026-07-21T08:18:41Z
> **Updated:** 2026-07-21T08:21:52Z
> **Plan:** [[2026-07-21-085654-llm-obsidian-v2-1-2-semantic-acceptance-refactor]]

## Review request

Review the implementation for **v2.1.2 semantic acceptance refactor** in `task/v2.1.2-acceptance-refactor` against `main` using the `light` cross-model gate.

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
- Run ID: `e28cf6a3-c18b-4918-b758-7e8f1c4d8e50`
- Received: 2026-07-21T08:21:52Z

### Findings

No findings.
### Executor resolution

No resolution was required or recorded for this round.

### Verification gaps

- The corrected cmux invocations ('workspace create/list --json/close' with short-ref fallback) cannot be validated against a real cmux from this product-read-only sandbox; the two new regressions exercise them only against mocked subprocess output, so the syntax correctness rests on the executor's live environment where this defect was presumably observed.
- Light pass scoped to the single new commit bb04fec on top of the previously approved d3ab3bd; I re-ran tests/test_release_acceptance.py (all green, including the new 'workspace creation resolves current cmux short ref to exact UUID' and 'unresolved created workspace is contained' checks) and did not re-run the full remaining suite this round.

### Residual risks

- If the release-gate cmux version drops the legacy 'new-workspace' subcommand, dispatch-workspace cells will fail live and need the same noun-verb migration in task_sessions.py; the matrix will surface this as blocked cells rather than silent misbehavior.

### Notes for executor

- The bb04fec change is sound: create_workspace now prefers an exact UUID from the create output, falls back to resolving a short 'workspace:N' ref through the JSON inventory requiring exactly one match, and — correctly treating the create as an already-performed external effect — closes the just-created workspace by ref before failing when exact ownership cannot be established. workspace_is_open switched from substring matching of CLI text to exact-ID comparison against the JSON inventory, which strengthens the owned-orphan reconciliation and stays fail-closed when the inventory is unavailable.
- Because scripts/acceptance-workspace-supervisor.py is on the code-owned orchestration allowlist, this fix intentionally does not invalidate live evidence — consistent with the 'compatible orchestration change reuses live evidence' contract.
- Consistency question to confirm during the live gate: scripts/task_sessions.py:1003 (spawn_workspace, used by dispatch-workspace placement) still uses the legacy 'cmux --id-format both new-workspace --window ...' subcommand while the supervisor now uses the noun-verb 'workspace create'. spawn_workspace already tolerates short refs via parse_workspace and the tree layout, and the dispatch-workspace acceptance cells will exercise it, so any breakage is self-catching in the matrix — but if the current cmux deprecates one style, consider unifying both call sites on the same interface afterwards.

## Archive boundary

This page keeps validated review findings, executor resolutions, and final verification. Dedicated raw prompts, compressed callback payloads, command logs, sockets, and cmux identifiers are intentionally excluded. Validated findings and executor resolutions are retained as review evidence.
