---
type: meta
title: "Operation Log"
created: 2026-07-05
updated: 2026-07-21
tags:
  - meta
  - log
status: evergreen
related:
  - "[[index]]"
  - "[[hot]]"
  - "[[overview]]"
sessions:
  - "public-template-v2"
---

# Operation Log

Navigation: [[index]] | [[hot]] | [[overview]]

Append-only. Новые записи добавляются СВЕРХУ. Прошлые записи не редактируются.

Формат записи: `## [YYYY-MM-DD] operation | Title`

Парсинг недавних записей: `grep "^## \[" wiki/log.md | head -10`

---

## [2026-07-21] review | v2.1.2 bilingual documentation

`c-000031` [[Cross-model review — v2.1.2 bilingual documentation — 421315369f47]]. 1 round(s), final verdict `approve`; reviewer claude/opus.

## [2026-07-21] reap | v2.1.2 semantic acceptance refactor

`c-000030` [[LLM Obsidian v2.1.2 semantic acceptance refactor]]. ## Result

Prepared the local v2.1.2 release candidate by folding the unreleased v2.1.1 work into a semantic, finite acceptance pipeline. The former monolithic live runner is now a thin compatibility entrypoint over code-owned contracts, launchers, prompt rendering, sandbox construction, scenario adapters, and skill adapters. Per-cell fingerprints are derived from exact behavioral dependencies, resolved major model generation, the deterministic seed vault, and a generated fail-closed dependency

## [2026-07-21] review | v2.1.2 semantic acceptance refactor

`c-000029` [[Cross-model review — v2.1.2 semantic acceptance refactor — 3444ea2dfa5f]]. 1 round(s), final verdict `approve`; reviewer claude/opus.

## [2026-07-21] review | v2.1.2 semantic acceptance refactor

`c-000028` [[Cross-model review — v2.1.2 semantic acceptance refactor — 22c7eb2777c5]]. 1 round(s), final verdict `approve`; reviewer claude/fable.

## [2026-07-21] review | v2.1.2-acceptance-final-fixes

`c-000027` [[Cross-model review — v2.1.2-acceptance-final-fixes — 03ced8bcc75e]]. 1 round(s), final verdict `approve`; reviewer claude/fable.

## [2026-07-21] review | v2.1.2 semantic acceptance refactor

`c-000026` [[Cross-model review — v2.1.2 semantic acceptance refactor — 8648d54f453c]]. 2 round(s), final verdict `approve`; reviewer claude/fable.

## [2026-07-21] review | v2.1.2 semantic acceptance refactor

`c-000025` [[Cross-model review — v2.1.2 semantic acceptance refactor — 34cd03f732ac]]. 1 round(s), final verdict `approve`; reviewer claude/fable.

## [2026-07-21] review | v2.1.2 semantic acceptance refactor

`c-000024` [[Cross-model review — v2.1.2 semantic acceptance refactor — bbbdbba79f2a]]. 1 round(s), final verdict `approve`; reviewer claude/fable.

## [2026-07-21] review | v2.1.2 semantic acceptance refactor

`c-000023` [[Cross-model review — v2.1.2 semantic acceptance refactor — 60b927ff61a1]]. 1 round(s), final verdict `approve`; reviewer claude/fable.

## [2026-07-21] review | v2.1.2 semantic acceptance refactor

`c-000022` [[Cross-model review — v2.1.2 semantic acceptance refactor — 6a12aeccde78]]. 2 round(s), final verdict `approve`; reviewer claude/fable.

## [2026-07-21] review | v2.1.2 semantic acceptance refactor

`c-000021` [[Cross-model review — v2.1.2 semantic acceptance refactor — f3e56a208804]]. 2 round(s), final verdict `approve`; reviewer claude/fable.

## [2026-07-21] review | v2.1.2-acceptance-final-fixes

`c-000020` [[Cross-model review — v2.1.2-acceptance-final-fixes — 2bb75362ff9c]]. 1 round(s), final verdict `approve`; reviewer claude/fable.

## [2026-07-21] review | v2.1.1 final implementation review

`c-000018` [[Cross-model review — v2.1.1 final implementation review — 1bae885ecfdf]]. 2 round(s), final verdict `approve`; reviewer claude/fable.

## [2026-07-20] review | v2.1.1 final implementation review

`c-000017` [[Cross-model review — v2.1.1 final implementation review — 06aeda67d29d]]. 2 round(s), final verdict `approve`; reviewer claude/fable.

## [2026-07-20] review | v2.1.1 final implementation review

`c-000016` [[Cross-model review — v2.1.1 final implementation review — 6fb4143a11f2]]. 2 round(s), final verdict `approve`; reviewer claude/fable.

## [2026-07-20] review | v2.1.1 final implementation review

`c-000015` [[Cross-model review — v2.1.1 final implementation review — 43a447bc1b02]]. 2 round(s), final verdict `approve`; reviewer claude/fable.

## [2026-07-20] review | v2.1.1 final implementation review

`c-000014` [[Cross-model review — v2.1.1 final implementation review — ab4803b6000c]]. 2 round(s), final verdict `approve`; reviewer claude/fable.

## [2026-07-19] review | v2.1.1 code-owned optimization plan review

`c-000013` [[Cross-model review — v2.1.1 code-owned optimization plan review — 4f7e86ffe465]]. 2 round(s), final verdict `approve`; reviewer claude/fable.

## [2026-07-20 03:48] backlog | add — review-verify-delta-context

## [2026-07-19] review | v2.1.1 code-owned optimization plan review

`c-000012` [[Cross-model review — v2.1.1 code-owned optimization plan review — 18cb05f65030]]. 3 round(s), final verdict `approve`; reviewer claude/fable.

## [2026-07-19 05:00] backlog | add — cmux-acceptance-surface-cleanup

## [2026-07-18] reap | v2.0.8-rd-upstream-audit

`c-000010` [[LLM Obsidian v2.0.8 RD upstream audit]]. Подготовлен локальный релиз-кандидат v2.0.8 после критического аудита `origin/test` и `origin/upstream-sync/rd-fixes`: устаревший timeout-патч отклонён, остальные изменения адаптированы под текущий пайплайн. Политика DCG теперь разрешает amend, блокирует rebase в базовом профиле и сохраняет рабочие разрешения task-worktree; дефолты закреплены как Codex `gpt-5.6-sol` high и Claude `fable` high. Полный Fable/high review и повторная проверка исправлений прошли; история сохранена в [[Cross-model review — v2.0.8-rd-upstream-audit — db9313c2eba2]], связанный [[daily-pipeline-guide]] обновлён.

## [2026-07-18] review | v2.0.8-rd-upstream-audit

`c-000009` [[Cross-model review — v2.0.8-rd-upstream-audit — db9313c2eba2]]. 2 round(s), final verdict `approve`; reviewer claude/fable.

## [2026-07-17 05:28] dispatch | v2.0.8-rd-upstream-audit

Spawned an unattended Codex task split (cmux `6915E188-1195-47DB-8853-FC6140133345`, configured default `gpt-5.6-sol`) in worktree `/Users/zak/Projects/worktrees/llm-obsidian-v2.0.8-rd-upstream-audit` on branch `task/v2.0.8-rd-upstream-audit` from `main` to critically audit `origin/test` and `origin/upstream-sync/rd-fixes`, prepare local v2.0.8, and require full Claude Opus 4.8 review. Plan: `wiki/plans/2026-07-17-052426-prepare-v2-0-8-from-test-and-upstream-rd-fixes.md`. Context: [[Unattended Pipeline]], [[daily-pipeline-guide]], [[Hot Cache]]. Awaiting final `## Wiki Summary` and `$llm-obsidian:reap`.

## [2026-07-11] release | llm-obsidian v2.0.0

Public template upgraded to the universal Claude Code and Codex pipeline. Personal notes, runtime sessions, derived indexes, workspace state, and credentials are not part of the release.

## [2026-07-06] reap | dispatch-reap-live-smoke-20260706030148

`c-000004` [[Dispatch Reap Live Smoke gpt-5.5]]. Final reap filed the live Codex dispatch smoke for `llm-obsidian`: the task split ran on model `gpt-5.5`, used branch `task/dispatch-reap-live-smoke-20260706030148`, produced `.task-summary.md` through manual `$llm-obsidian:reap-send`, and confirmed the file-first [[reap]] path without direct vault writes from the task split.

## [2026-07-05] init | Vault initialized from the llm-obsidian template

Вольт создан из шаблона llm-obsidian v1.0.0. Демо-страницы concepts/entities/sources можно оставить как справочник по механикам или снести после онбординга. Дальше: [[getting-started]].
