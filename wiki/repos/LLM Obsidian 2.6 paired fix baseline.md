---
type: repo
title: "LLM Obsidian 2.6 paired fix baseline"
address: c-000072
created: 2026-08-01
updated: 2026-08-01
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
  - "[[Cross-model review — 182326f6-532b-4cfa-a638-c230fdadf18a — 87f543018833]]"
---

# LLM Obsidian 2.6 paired fix baseline

## Result

Added an observable red regression and the smallest Unicode-aware normalization fix in the frozen paired fixture. `normalize_label` now preserves lowercase Unicode letters and digits, collapses unsafe runs through `[\W_]+`, trims boundary separators, and retains the punctuation-only `untitled` fallback.

## Outcome evidence

- `fix-unicode`: `Résumé Plan` produces `résumé-plan`; `Москва 42` produces `москва-42` without transliteration or letter loss.
- `fix-separators`: the regression covers repeated whitespace, underscore, slash, punctuation, and unsafe boundary separators in one composite case.
- `fix-original-suite`: the original three ASCII assertions and all new behavioral cases pass together via `python3 evals/paired-v2.6.0/fix/test_label_normalizer.py`.
- `fix-scope`: the complete product diff contains only `evals/paired-v2.6.0/fix/label_normalizer.py` and `evals/paired-v2.6.0/fix/test_label_normalizer.py`; no harness, skill, schema, dependency, manifest, or other fixture changed.

## Verification and commits

Red evidence at `1b5bd38` reported both required Unicode mismatches. Green implementation is `41dcf10`. The exact scoped profile passed at HEAD `41dcf1058f8df409b631ab0af9492808656df525`: `make test-harness`, `make test-model-routing`, and `git diff --check`. The harness suite passed on the coordinator-authorized single unchanged retry after a classified nondeterministic verification failure; no product repair followed that failure.

Review archive: [[Cross-model review — 182326f6-532b-4cfa-a638-c230fdadf18a — 87f543018833]]

## Outcome

Outcome disposition: `achieved`

Outcome evidence IDs: `fix-unicode`, `fix-separators`, `fix-original-suite`, `fix-scope`

Residual gaps:
- none
