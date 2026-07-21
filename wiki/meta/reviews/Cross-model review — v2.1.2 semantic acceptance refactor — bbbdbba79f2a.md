---
type: review
title: "Cross-model review — v2.1.2 semantic acceptance refactor — bbbdbba79f2a"
address: c-000024
created: 2026-07-21
updated: 2026-07-21
tags:
  - review
  - cross-model
status: resolved
sessions:
  - "019f6ddd-d07e-7a30-b018-f6358753fb91"
review_id: "5fdad4d3-59e2-49bc-a91a-f0c0de35ec66"
reviewer_runtime: "claude"
reviewer_model: "fable"
reviewer_effort: "high"
review_mode: "light"
rounds: 1
verdict: approve
related:
  - "[[2026-07-21-085654-llm-obsidian-v2-1-2-semantic-acceptance-refactor]]"
---

# Cross-model review — v2.1.2 semantic acceptance refactor — bbbdbba79f2a

> [!abstract] Outcome
> **Task:** v2.1.2 semantic acceptance refactor
> **Final verdict:** `approve`
> **Reviewer:** claude · fable · effort `high`
> **Executor:** codex
> **Mode:** `light` · **rounds:** 1
> **Started:** 2026-07-21T08:28:12Z
> **Updated:** 2026-07-21T08:30:34Z
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
- Run ID: `157f15d3-872b-4a30-acab-aefd8dac78d3`
- Received: 2026-07-21T08:30:34Z

### Findings

No findings.
### Executor resolution

No resolution was required or recorded for this round.

### Verification gaps

- Light pass scoped to the single new commit 8a84fad on top of the previously approved bb04fec; I re-ran tests/test_release_acceptance.py (all green, including the new 'acceptance environment ignores ephemeral cmux CLI shims' check) and did not re-run the remaining suites this round.
- The real-world PATH layouts inside live cmux surfaces (shim directory naming, ~/.local/bin fallback correctness for the actual claude/codex installs) can only be confirmed in the executor's live environment; the regression covers the resolution logic against a synthetic PATH.

### Residual risks

- Environment version probing now assumes stable installs are visible outside shim directories (or in ~/.local/bin); an unconventional install location would degrade the environment value to 'unavailable' without failing the run.
- Prior standing risks are unchanged: cmux CLI syntax correctness is provable only live, and the fresh 58-cell matrix remains the gate for launcher/lifecycle behavior.

### Notes for executor

- The 8a84fad change is sound: command_version now walks PATH explicitly, skips any entry under a cmux-cli-shims directory, appends a ~/.local/bin fallback, and probes the resolved stable binary — so environment_sha256 no longer churns between shim-injected surface contexts and plain shells. This matches the supervisor's existing 'excludes ephemeral cmux CLI shims' discipline, and the failure direction is safe: evidence recorded under previously shim-resolved versions falls out of exact-fingerprint reuse and reruns rather than being reused stale.
- Degenerate case worth knowing: if a runtime binary is reachable only through a shim PATH entry and absent from both the remaining PATH and ~/.local/bin, the version records 'unavailable' — stable but uninformative; the scoped environment contract still hashes it deterministically.

## Archive boundary

This page keeps validated review findings, executor resolutions, and final verification. Dedicated raw prompts, compressed callback payloads, command logs, sockets, and cmux identifiers are intentionally excluded. Validated findings and executor resolutions are retained as review evidence.
