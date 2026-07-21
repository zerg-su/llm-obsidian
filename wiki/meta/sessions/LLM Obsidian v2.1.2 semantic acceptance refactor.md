---
type: session
title: "LLM Obsidian v2.1.2 semantic acceptance refactor"
address: c-000030
created: 2026-07-21
updated: 2026-07-21
tags:
  - reap
  - session
status: active
sessions:
  - "019f6ddd-d07e-7a30-b018-f6358753fb91"
executor_runtime: codex
executor_model: "gpt-5.6-sol"
related:
  - "[[Cross-model review — v2.1.2-acceptance-final-fixes — 2bb75362ff9c]]"
  - "[[Cross-model review — v2.1.2 semantic acceptance refactor — f3e56a208804]]"
  - "[[Cross-model review — v2.1.2 semantic acceptance refactor — 6a12aeccde78]]"
  - "[[Cross-model review — v2.1.2 semantic acceptance refactor — 60b927ff61a1]]"
  - "[[Cross-model review — v2.1.2 semantic acceptance refactor — bbbdbba79f2a]]"
  - "[[Cross-model review — v2.1.2 semantic acceptance refactor — 34cd03f732ac]]"
  - "[[Cross-model review — v2.1.2 semantic acceptance refactor — 8648d54f453c]]"
  - "[[Cross-model review — v2.1.2-acceptance-final-fixes — 03ced8bcc75e]]"
  - "[[Cross-model review — v2.1.2 semantic acceptance refactor — 22c7eb2777c5]]"
  - "[[Cross-model review — v2.1.2 semantic acceptance refactor — 3444ea2dfa5f]]"
---

# LLM Obsidian v2.1.2 semantic acceptance refactor

## Result

Prepared the local v2.1.2 release candidate by folding the unreleased v2.1.1 work into a semantic, finite acceptance pipeline. The former monolithic live runner is now a thin compatibility entrypoint over code-owned contracts, launchers, prompt rendering, sandbox construction, scenario adapters, and skill adapters. Per-cell fingerprints are derived from exact behavioral dependencies, resolved major model generation, the deterministic seed vault, and a generated fail-closed dependency lock; wiki and runtime-derived data no longer cause global invalidation.

The supervisor checkpoints each completed row, reuses only integrity-verified matching evidence, bounds retries to typed transients, tracks activity without storing screen content, and reconciles exact owned cmux surfaces/workspaces. Runtime routing records the actual launched model while keeping production defaults independent from cheaper acceptance models. The final daily defect was fixed by rejecting the observed legacy envelope and allowing one validator-guided correction in the same Codex agent thread, without fallback or a second model.

## Verification

- Full hermetic `make test` passed after the final product change, including 167 review-dispatch tests and the acceptance dependency-lock regeneration/equality gate.
- Selective final live acceptance at committed HEAD used Claude Sonnet and Codex gpt-5.6-terra at medium effort: 58/58 passed, with 56 integrity-verified rows reused and only the two changed daily rows executed. The Codex row reproduced the legacy first response, rejected it, corrected it in the same agent thread, then passed apply, rerun, replacement, and cleanup proofs.
- The final report is complete with zero failed rows and no owned orphan acceptance surfaces/workspaces.
- Historical prompt audit independently rendered all 58 prompts from the pre-refactor runner. The initial frozen baseline preserved 48 byte-identically; every intentional later baseline delta is attributable to separately reviewed fixture/lifecycle fixes.
- Final full cross-model review ran on Opus 4.8/high after Fable repeatedly failed before its first token; Opus approved with no findings. Its runtime verification gaps are satisfied by the green suite, 58/58 live report, clean tree, and historical prompt audit.

## Release state

Release metadata and changelog are set to 2.1.2. The task branch is clean and contains the reviewed candidate at commit `2aade48`. No push, publication, deployment, worktree deletion, or branch deletion was performed. The remaining authorized coordinator steps are the atomic plan close/result filing, local fast-forward of `main`, and an annotated local `v2.1.2` tag; the user will push and create the GitHub release manually.

Review archive: [[Cross-model review — v2.1.2-acceptance-final-fixes — 2bb75362ff9c]]

Review archive: [[Cross-model review — v2.1.2 semantic acceptance refactor — f3e56a208804]]

Review archive: [[Cross-model review — v2.1.2 semantic acceptance refactor — 6a12aeccde78]]

Review archive: [[Cross-model review — v2.1.2 semantic acceptance refactor — 60b927ff61a1]]

Review archive: [[Cross-model review — v2.1.2 semantic acceptance refactor — bbbdbba79f2a]]

Review archive: [[Cross-model review — v2.1.2 semantic acceptance refactor — 34cd03f732ac]]

Review archive: [[Cross-model review — v2.1.2 semantic acceptance refactor — 8648d54f453c]]

Review archive: [[Cross-model review — v2.1.2-acceptance-final-fixes — 03ced8bcc75e]]

Review archive: [[Cross-model review — v2.1.2 semantic acceptance refactor — 22c7eb2777c5]]

Review archive: [[Cross-model review — v2.1.2 semantic acceptance refactor — 3444ea2dfa5f]]
