---
type: repo
title: "LLM Obsidian 2.6 dogfood RT2 fresh review packet identity"
address: c-000090
created: 2026-08-02
updated: 2026-08-02
tags:
  - reap
  - repo
status: active
sessions:
  - "019fab00-3160-7380-8920-4b20183afb76"
executor_runtime: codex
executor_model: "gpt-5.6-sol"
outcome_disposition: achieved
outcome_evidence_ids:
  - rt2-red-repro
  - rt2-identity-binding
  - rt2-stale-rejection
  - rt2-suite
residual_gap_pointers:
related:
  - "[[Cross-model review — 0af457a5-5e5a-4b7b-a359-35d03ab5a7a4 — 79b318806913]]"
---

# LLM Obsidian 2.6 dogfood RT2 fresh review packet identity

Reproduced the stale `.task-review.json` symptom on the preserved base and traced it to fresh review transport retaining only the stable dispatch identity. Added committed red regressions, then fixed the boundary narrowly: authorized fresh review invalidates prior transient decision/response files; accepted resolution state and the executor packet bind the exact review operation, round/run, callback digest, findings, and reviewed HEAD; pre-intake rejects stale identities. Automatic review finding `HOL-001` was applied in `d1a2c37`: executor responses now carry a canonical digest over the active review operation and exact accepted callbacks, and both runtime intake and controller continuation require exact equality. Same-HEAD and same-finding stale-boundary regressions cover both seams. Commits: `c11f185` (red regression), `2c7b596` (minimal fix), and `d1a2c37` (review resolution). Verification passed at `d1a2c372b71ecf3838c3b2de8c8254e47a2aff25`: `make test-harness`, `make test-model-routing`, focused review-resolution/review-gate/runtime-task-summary tests, `git diff --check`, Python compilation, and task-contract validation. Review budgets, lanes, severity semantics, dependencies, provider effects, public interfaces, and legacy archive formats were unchanged.

Review archive: [[Cross-model review — 0af457a5-5e5a-4b7b-a359-35d03ab5a7a4 — 79b318806913]]

## Outcome

Outcome disposition: `achieved`

Outcome evidence IDs: `rt2-red-repro`, `rt2-identity-binding`, `rt2-stale-rejection`, `rt2-suite`

Residual gaps:
- none
