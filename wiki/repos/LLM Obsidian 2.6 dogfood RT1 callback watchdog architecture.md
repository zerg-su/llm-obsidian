---
type: repo
title: "LLM Obsidian 2.6 dogfood RT1 callback watchdog architecture"
address: c-000088
created: 2026-08-02
updated: 2026-08-02
tags:
  - reap
  - repo
status: active
sessions:
  - "019fab00-3160-7380-8920-4b20183afb76"
executor_runtime: codex
executor_model: "gpt-5.6-terra"
outcome_disposition: achieved
outcome_evidence_ids:
  - rt1-decision
  - rt1-state-flow
  - rt1-cost-bound
  - rt1-test-seams
residual_gap_pointers:
related:
  - "[[Cross-model review — ed86d0ff-dc24-4ac8-80c9-f3592d8feed9 — 22410647c507]]"
---

# LLM Obsidian 2.6 dogfood RT1 callback watchdog architecture

## Outcome

Committed the RT1 ADR-style callback-watchdog decision in `docs/acceptance/v2.6-dogfood-rt1-callback-watchdog.md`, finalized at `59e347d603f66daeaef37d6160e06de122cf3858`.

The selected callback fallback is passive deadline reconciliation through the existing runtime worker, OperationStore, CallbackBroker, and exact-owner reconcile path. It creates typed attention on expiry and adds no scheduler, model polling/calls, provider input, cancellation, surface closure, permission changes, or lifecycle authority.

## Review and verification resolution

Both automatic-review authority-boundary findings were applied on the final HEAD: the ADR distinguishes bounded LivenessController nudge/restart authority and reviewer surface-loss broker recovery from the passive callback path. The callback-recovery seam was also made explicit and its focused deterministic test passed before the identity-bound verification resubmission.

## Verification

- Initial scoped profile passed: `make test-harness`, `make test-model-routing`, and `git diff --check`.
- Review resolution checks passed: callback, liveness, task lifecycle, and review-resolution tests.
- Post-attention focused check passed: `python3 tests/harness/test_runtime_task_summary.py` and `git diff --check`.

## Evidence

All four evidence items are established on the final HEAD: decision/ownership alternatives, state flow, code-only callback-path cost bound, and deterministic seams with rollout, rollback, and a repeated architecture-failure stop criterion.

Review archive: [[Cross-model review — ed86d0ff-dc24-4ac8-80c9-f3592d8feed9 — 22410647c507]]

## Outcome

Outcome disposition: `achieved`

Outcome evidence IDs: `rt1-decision`, `rt1-state-flow`, `rt1-cost-bound`, `rt1-test-seams`

Residual gaps:
- none
