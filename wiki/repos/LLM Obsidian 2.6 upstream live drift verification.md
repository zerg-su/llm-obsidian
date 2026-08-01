---
type: repo
title: "LLM Obsidian 2.6 upstream live drift verification"
address: c-000066
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
related:
  - "[[Cross-model review — 868a768d-1b9d-408b-b13b-4d0e69b1b5a0 — f2f5bdad3872]]"
---

# LLM Obsidian 2.6 upstream live drift verification

## Outcome

Protected research on 2026-08-01 proved that `obra/superpowers` `main` remains `44c9b2d6e889982ac18c27d05a19fefe335194e1` and `mattpocock/skills` `main` remains `2ab958093e83e0ec752e6c1c5932da465bf23e0c`, exactly matching both retained pins. The post-pin range is empty, so every plan-approved general-practice judgement remains unchanged. Current release context is recorded with official citations in `docs/upstream-skills-comparison.md`: Superpowers `v6.2.0` and Matt Pocock Skills `v1.1.0`.

## Commits

- `d4c7829` records the durable cited no-drift proof. Snapshot bytes and `references/upstream-skills/manifest.json` were intentionally not rewritten.
- `1331d3e` and `4210bb9` are coordinator-authorized mechanism repairs discovered during protected research. They recover only accepted `research-fetch` and `research-synth` callbacks after successful exact exit requests exceed their deadlines, require exact identity and the protected research profile, and fail closed on mismatch.

## Verification

- `references/upstream-skills/verify_snapshots.py`: Matt 141 files / 397020 bytes / `ee4511e5...`; Superpowers 95 files / 1174353 bytes / `1db2d421...`.
- Focused docs/manifest checks: `tests/test_improve_skills.py` and `git diff --check` passed.
- Configured `scoped` profile passed at `d4c7829`: `make test-harness`, `make test-model-routing`, `git diff --check`.
- Self-review found no material or minor issue; the diff from `release/2.6.0` is limited to the cited comparison and the authorized protected-research cleanup repair plus tests.

## Scope

No foreign installer, GitHub/issue tracker, worktree, lifecycle/orchestration framework, engineering-skill semantics, Outcome Contract, versioning, or Swarm material was imported or changed. The approved shared release plan remains unchanged.

Review archive: [[Cross-model review — 868a768d-1b9d-408b-b13b-4d0e69b1b5a0 — f2f5bdad3872]]
