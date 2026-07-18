---
type: session
title: "LLM Obsidian v2.0.8 RD upstream audit"
address: c-000010
created: 2026-07-18
updated: 2026-07-18
tags:
  - pipeline
  - release
  - v2-0-8
status: active
sessions:
  - 019f72c4-816e-7200-a399-505adaa350e0
  - 019f6ddd-d07e-7a30-b018-f6358753fb91
executor_runtime: codex
related:
  - "[[daily-pipeline-guide]]"
  - "[[Cross-model review — v2.0.8-rd-upstream-audit — db9313c2eba2]]"
---

# LLM Obsidian v2.0.8 RD upstream audit

## Outcome

Prepared and committed a coherent local v2.0.8 release candidate after auditing the unique patch from `origin/test` and every commit from `origin/upstream-sync/rd-fixes`. No source branch was merged wholesale. The final candidate is `4b49f50bc7495958e27409425c2440da31750525` on `task/v2.0.8-rd-upstream-audit`.

## Patch-source audit

| Candidate | Disposition | Evidence |
| --- | --- | --- |
| `a4d9516` | Rejected (superseded) | Its `scripts/with-timeout`, regression, and Makefile intent already exist in the v2 base with richer integration since `f371c50`; the old pre-v2 branch was not merged or replayed. |
| `d08d84c` | Adapted | Kept reviewer startup isolation and the exact canonical-coordinator scratch exception. Removed the broad executor-profile fallback and duplicated changelog material. |
| `b769ed7` | Adapted | Preserved explicit Codex reviewer effort after `--model`, aligned generation with supervisor validation, and extended the current default to `gpt-5.6-sol` high. |
| `f201232` | Adapted | Added the explicit `/clarify` workflow, EN/RU routing, attribution, README parity, and the daily guide update through the vault writer; avoided wholesale or duplicate release text. |
| `534f977` | Adapted | Implemented the user-approved narrow security policy: base DCG allows `git commit --amend`, explicitly blocks rebase and dangerous lifecycle/history operations, while task worktrees retain amend and rebase allowances. |
| `6b26a4b` | Adapted | Kept plain-text clarification fallback and the nonredundant authorization/confirmation semantics in the coherent clarify implementation. |

## Release and runtime surfaces

- Bumped v2.0.8 in `CHANGELOG.md`, both Claude plugin/marketplace manifests, and the generated Codex plugin manifest.
- Added `/clarify`, router rules/tests, RU/EN documentation, corrected Matt Pocock `grill-me` attribution, and updated [[daily-pipeline-guide]] transactionally.
- Hardened review startup/profile isolation, metadata precedence, Codex effort ordering, canonical scratch validation, and supervisor fail-closed checks.
- Set repository defaults to Codex `gpt-5.6-sol` high and Claude `fable` high across canonical runtime config, dispatch/review defaults, skills, generated adapters, docs, and tests. Preserved historical records and explicit task overrides. Documented intentional exceptions: Codex deep is `max`; the bounded daily summarizer is Codex `gpt-5.6-terra` low or Claude `sonnet` low.
- Updated the six coordinator-applied `.codex` product paths in the explicit release commit; the temporary `.task-codex-defaults.patch` was not committed and was removed after application.
- Updated DCG base/task policy and regression coverage for the approved amend/rebase boundary.

## Verification

- Full `make test`: passed.
- `scripts/validate-vault.py --summary`: passed.
- Live DCG smoke matrix: 150/150 passed; DCG asset suite: 21/21 passed.
- Review-dispatch suite: 138/138 passed; task lifecycle suite: passed.
- Codex adapter/default suite: 20/20 passed; `python3 scripts/codex-adapter.py --check`: no changes.
- Skill router: 44/44 passed; skill budget, research-isolation, with-timeout (6/6), instruction lint, manifest/version consistency, and `git diff --check`: passed.
- After the reviewer-requested documentation fixes, instruction lint, adapter check, vault validation, skill-budget tests, and diff checks passed again.

## Cross-model review

Full Claude Fable review at high effort completed under review `156defb8-518e-4d32-9347-e9d14c39aedf`. The initial verdict was approve with three nits: restore the verified upstream `grill-me` attribution path, document the Claude Sonnet/low daily exception, and remove the applied coordinator patch artifact. All three were applied in `4b49f50`. The same reviewer verified the fixes in run `a95a0066-ce78-4c1f-aba0-6e1acfbb2dee` and returned approve with no findings.

Residual risks accepted by the reviewer and executor: the approved base rebase expression also blocks rebase-shaped invocations such as `git pull --rebase`; dispatched Codex tasks now receive pinned repository model/effort defaults unless explicit task metadata overrides them. Both behaviors are intentional and documented.

Cross-model review: fixes applied.

## Safety and final state

Final commit: `4b49f50bc7495958e27409425c2440da31750525`. Commits preserved in order: `aa82db7`, `511f665`, `1305003`, `4b49f50`. No push, tag, publish, deploy, worktree deletion, or branch deletion occurred.

Review archive: [[Cross-model review — v2.0.8-rd-upstream-audit — db9313c2eba2]]
