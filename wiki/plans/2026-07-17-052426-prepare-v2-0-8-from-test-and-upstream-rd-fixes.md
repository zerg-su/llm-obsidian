---
type: plan
title: "Prepare v2.0.8 from test and upstream RD fixes"
address: c-000008
session_id: 019f6ddd-d07e-7a30-b018-f6358753fb91
sessions:
  - id: 019f6ddd-d07e-7a30-b018-f6358753fb91
    date: 2026-07-17
source_cwd: "/Users/zak/Projects/llm-obsidian"
status: pending
created: 2026-07-17
updated: 2026-07-17
tags:
  - plan
  - manual-save
---

# Prepare v2.0.8 from test and upstream RD fixes

## Goal

Prepare a committed local v2.0.8 release candidate from current `main` after a critical, evidence-based audit of `origin/test` and `origin/upstream-sync/rd-fixes`. Treat both branches as patch sources, not merge targets.

## Scope and decisions

1. Create `task/v2.0.8-rd-upstream-audit` from current `main` in a separate cmux worktree.
2. Do not merge `origin/test`: it is based on the pre-v2 commit `c32e4be`. Audit only its unique commit `a4d9516`, especially the portable `scripts/with-timeout` helper, integration documentation, Makefile wiring, and regression test. Port or reimplement only parts that fit current v2.0.7 contracts.
3. Audit every commit on `origin/upstream-sync/rd-fixes` separately:
   - `d08d84c`: isolated reviewer MCP profile and sanctioned coordinator scratch.
   - `b769ed7`: explicit Codex reviewer effort preservation.
   - `f201232` and `6b26a4b`: clarify/alignment skill and interactive fallback.
   - `534f977`: practical local git operations in DCG policy.
4. For each candidate, document accepted, adapted, or rejected with current-pipeline evidence. Be especially skeptical of broader DCG allowances, new user-interaction semantics, profile fallback behavior, attribution/version wording, and duplicated changelog fragments.
5. Prepare v2.0.8 release surfaces consistently: implementation, regression tests, plugin manifests/marketplace, CHANGELOG, README/docs where warranted, generated Codex adapter outputs if canonical tooling requires them. Do not tag, push, publish, deploy, delete branches/worktrees, or change external state.
6. Preserve unrelated work and runtime/derived state. Do not hand-edit `.vault-meta/`, `wiki/log.md`, or `wiki/hot.md`. Vault mutations, if truly needed, must follow repository transaction rules.
7. Verify proportionately:
   - run all targeted tests for accepted patches;
   - run `make test`;
   - run `scripts/validate-vault.py --summary`;
   - run adapter/manifest consistency checks and any release-specific checks found in the repository.
8. Before final acceptance, run a full `review-dispatch` cycle with a Claude reviewer using the `opus` alias (Opus 4.8), medium effort. The reviewer is advisory/read-only. Resolve all findings explicitly and reuse the same reviewer for bounded verification, up to two verify iterations. Never auto-resolve a blocking, scope, public-interface, migration, security, or external-effect decision.
9. Finish with a clean, committed task branch. The final summary must include:
   - accepted/adapted/rejected table by source commit;
   - tests and validation evidence;
   - release version surfaces changed;
   - cross-model review verdict and any residual risks;
   - exact commit hash;
   - confirmation that no push/tag/publish/deploy occurred.

## Acceptance criteria

- v2.0.8 is internally consistent and installable as a local plugin candidate.
- No wholesale merge from either source branch.
- Every accepted behavior has regression coverage appropriate to risk.
- Full Claude Opus 4.8 review passes, including verification after changes-requested.
- The result is committed on `task/v2.0.8-rd-upstream-audit` and returned through `reap-send` as a final session titled “LLM Obsidian v2.0.8 RD upstream audit”.
