---
type: repo
title: "LLM Obsidian 2.6 paired fix post-change final"
address: c-000084
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
  - fix-unicode
  - fix-separators
  - fix-original-suite
  - fix-scope
residual_gap_pointers:
related:
  - "[[Cross-model review — abcce802-c019-4d78-a31f-d41cf7beed37 — 7f80cf39fdbf]]"
---

# LLM Obsidian 2.6 paired fix post-change final

Implemented and committed the frozen paired-fixture Unicode label fix at `c0d027f82e14e83b4d4368fec2a7e640d75d8cac`. The accepted typed evidence reproduces the pre-fix loss (`Résumé Plan` -> `r-sum-plan`; `Москва 42` -> `42`), isolates the ASCII-only `[a-z0-9]` allow-list as root cause, and proves the new observable regression red before the fix. The implementation now joins Unicode-aware alphanumeric runs while excluding underscore, so Unicode letters remain lowercase, unsafe separator runs collapse to one hyphen, boundaries are trimmed, and inputs without letters or digits return `untitled`. The original ASCII script and new behavioral script pass together; `make test-harness`, `make test-model-routing`, and `git diff --check` also pass. The base-to-HEAD diff contains only `evals/paired-v2.6.0/fix/label_normalizer.py` and `evals/paired-v2.6.0/fix/test_label_normalizer_behavior.py`; no harness, skill, schema, dependency, manifest, or other fixture changed.

Review archive: [[Cross-model review — abcce802-c019-4d78-a31f-d41cf7beed37 — 7f80cf39fdbf]]

## Outcome

Outcome disposition: `achieved`

Outcome evidence IDs: `fix-unicode`, `fix-separators`, `fix-original-suite`, `fix-scope`

Residual gaps:
- none
