---
type: repo
title: "LLM Obsidian 2.6 common dogfood fixes"
address: c-000092
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
  - paired-contract-stability
  - semantic-drift-detection
  - typed-reap-disposition
  - legacy-isolation
residual_gap_pointers:
related:
  - "[[Cross-model review — e8ae73da-1c19-4247-9780-75fdf79c8b83 — 21981f374360]]"
---

# LLM Obsidian 2.6 common dogfood fixes

Committed `b42cff664352ca81d8124af590288a749243ae34`. RT3 archive finalization now rejects stale terminal resolution HEADs and broken ordered per-axis chains while preserving valid history; v4 `partially-achieved` summaries remain callback eligible under explicit regression coverage. The general design skill now requires immutable decision identity, atomic effect reservation, and crash/replay acceptance seams for restart, recovery, or other effectful actions. RT1 acceptance wording now states that reviewer surface loss transitions the current exact reviewer operation before launching its validated queued successor.

Verification passed: focused review-transport, runtime-summary, and workstream-A tests; `make test-harness`; `make test-skill-workstreams`; `make test-improve-skills`; and `git diff --check`. APIs, permissions, security boundaries, lifecycle/review budgets, and unrelated state were preserved.

Review archive: [[Cross-model review — e8ae73da-1c19-4247-9780-75fdf79c8b83 — 21981f374360]]

## Outcome

Outcome disposition: `achieved`

Outcome evidence IDs: `paired-contract-stability`, `semantic-drift-detection`, `typed-reap-disposition`, `legacy-isolation`

Residual gaps:
- none
