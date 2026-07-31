---
type: plan
title: "LLM Obsidian 2.4 Dogfood 01 — stale reap cache lint"
address: c-000036
session_id: 019fab00-3160-7380-8920-4b20183afb76
sessions:
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-07-31
source_cwd: "/Users/zak/Projects/llm-obsidian"
status: pending
created: 2026-07-31
updated: 2026-07-31
tags:
  - plan
  - dogfood
  - v2-4
  - fix
related:
  - "[[2026-07-30-224926-llm-obsidian-2-4-typed-pipeline-composition]]"
---

# LLM Obsidian 2.4 Dogfood 01 — stale reap cache lint

## Goal

Fix the reproducible release-test defect where an ignored legacy
`skills/reap-send/**/__pycache__/*.pyc` directory makes
`tests/test_instruction_lint.py` fail even though no legacy skill source is
tracked or shipped.

## Constraints

- Use the repository `debug` and `tdd` skill semantics.
- Reproduce first, then add a regression test, then make the smallest fix.
- Do not restore the removed `reap-send` public skill or weaken the clean-cut
  2.3 contract.
- Do not change dispatch, review, research, reap, cmux, permissions, or public
  runtime behavior.
- Preserve unrelated user work. No push, publish, deploy, branch/worktree
  deletion, dependency changes, or scope expansion.

## Verification

- The regression fixture proves an ignored bytecode-only legacy directory does
  not fail instruction lint.
- A real tracked/source file under `skills/reap-send` still fails closed.
- Run focused instruction-lint tests, `make test-instruction-lint`, and
  `git diff --check`.
- Run the automatic simple review and resolve material findings in the same
  reviewer session.

## Dogfood evidence

Capture the compiled `lifecycle/default` preview emitted by `dispatch validate`,
the exact runtime/model route, callback completion, review result, reap result,
and exact cmux cleanup. This is engineering profile `fix`, dogfood task 1/10.
