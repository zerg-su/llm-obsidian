---
type: meta
title: "Operation Log"
created: 2026-07-05
updated: 2026-08-01
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

## [2026-08-01] research | Superpowers vs Matt Pocock Skills
- Refreshed the upstream skill-library comparison against the byte-verified pins in `references/upstream-skills/`
- Pages created: [[Superpowers vs Matt Pocock Skills]]
- Repo guidance: `docs/upstream-skills-comparison.md`, corrected the ambiguous mattpocock version in the pin record
- Limitation: live-upstream drift unverified; protected research fetch failed three times with exit 127 (cmux wrapper vs sanitized RESEARCH_PATH), filed in `docs/feature-gaps.md`

## [2026-08-01] reap | df250-real-rt04-invalid-review-callbacks

`c-000051` [[2.5 real dogfood RT04 invalid review callbacks]]. ## RT04 — four invalid review callbacks: classified, one cause class repaired

Final HEAD `300250e305a2ad9325668ddd661164e32f3907f1`. Three commits: `d670419` (regression coverage), `0c2566f` (minimal fix),
`300250e` (all five review findings resolved).

### Forensic classification (durable content-free evidence only)

The four callbacks live in exactly one channel: `.vault-meta/pipeline-events.jsonl`, as `op=review-round`
events whose `counts.invalid_callbacks` sum to exactly 4 (vs 115 valid).

## [2026-08-01] reap | df250-real-rt06-runtime-neutral-telemetry

`c-000049` [[2.5 real dogfood RT06 runtime-neutral telemetry]]. ## RT06 — runtime-neutral / evidence-bounded skill telemetry

Final HEAD `54bfac8` (initial `b6c60a4` + review resolutions). All five review
findings applied, none rejected, no escalation required.

**Defect.** `scripts/pipeline-stats.py` counts skill invocations from Claude-only
sources (`~/.claude/history.jsonl`, Claude transcripts), then printed an
unconditional `## Dead-weight candidates (N of M installed, 0 invocations ...)`
over every installed skill. Codex is skill-capable but leaves no t

## [2026-08-01] reap | df250-real-rt05-auto-close-ownership

`c-000047` [[2.5 real dogfood RT05 auto-close ownership]]. ## Outcome

RT05 reproduced the observed `auto_close_expected=1,left_open=1` record and proved it was a pre-v2.3 telemetry false positive, not a current cmux leak or duplicate RT02 cleanup exposure. A resumable provider return was labeled from static unattended policy; the same task later completed and closed its exact surface. Current v3 dispatch keeps surface ownership on the parent operation while typed child phases own no surface.

## Scoped changes

- `a3d55a8` adds a deterministic ownershi

## [2026-08-01 01:39] dispatch | df250-real-rt04-invalid-review-callbacks

Spawned an approved unattended task session (cmux `13549CD9-F554-4520-B6B3-F2FCD69F5087`, runtime claude, model claude-opus-5) in split placement in worktree `/Users/zak/Projects/worktrees/df250-real-rt04-invalid-review-callbacks`. Target repo `/Users/zak/Projects/llm-obsidian`, branch `task/df250-real-rt04-invalid-review-callbacks` from `dogfood/2.5-real-10`. Plan: `/Users/zak/Projects/llm-obsidian/wiki/plans/2026-07-31-223011-llm-obsidian-2-5-10-real-task-dogfood.md`. Pre-loaded context: [[Unattended Pipeline]]. Awaiting typed review and final reap.

## [2026-08-01 01:39] dispatch | df250-real-rt06-runtime-neutral-telemetry

Spawned an approved unattended task session (cmux `D875A819-4554-4344-8A87-A327912C26FD`, runtime claude, model claude-opus-5) in split placement in worktree `/Users/zak/Projects/worktrees/df250-real-rt06-runtime-neutral-telemetry`. Target repo `/Users/zak/Projects/llm-obsidian`, branch `task/df250-real-rt06-runtime-neutral-telemetry` from `dogfood/2.5-real-10`. Plan: `/Users/zak/Projects/llm-obsidian/wiki/plans/2026-07-31-223011-llm-obsidian-2-5-10-real-task-dogfood.md`. Pre-loaded context: [[daily-pipeline-guide]]. Awaiting typed review and final reap.

## [2026-08-01 01:39] dispatch | df250-real-rt05-auto-close-ownership

Spawned an approved unattended task session (cmux `00EE3DD5-A274-4307-AEF2-3D9FABE537B3`, runtime codex, model gpt-5.6-sol) in split placement in worktree `/Users/zak/Projects/worktrees/df250-real-rt05-auto-close-ownership`. Target repo `/Users/zak/Projects/llm-obsidian`, branch `task/df250-real-rt05-auto-close-ownership` from `dogfood/2.5-real-10`. Plan: `/Users/zak/Projects/llm-obsidian/wiki/plans/2026-07-31-223011-llm-obsidian-2-5-10-real-task-dogfood.md`. Pre-loaded context: [[2.5 real dogfood RT02 exact cmux cleanup]], [[Unattended Pipeline]]. Awaiting typed review and final reap.

## [2026-08-01] reap | df250-real-rt01-stale-operations

`c-000045` [[2.5 real dogfood RT01 stale operation reconciliation]]. ## Outcome

Committed `530f020c427ea330ae6bc025fd008b629bf638cb` (`fix: classify terminal legacy operations as stale`). Upgrade preflight now uses exact authoritative harness identity to exclude only same-ID legacy worktree and broker mirrors proven terminal, effect-settled, resource-free, and lane-free. It performs no lifecycle mutation.

## Diagnosis

Cancellation correctly terminalized the two old dispatches and cleared their owned provider, supervisor, and cmux resources, but successful reap

## [2026-08-01] reap | df250-real-rt02-cmux-cleanup

`c-000043` [[2.5 real dogfood RT02 exact cmux cleanup]]. # RT02 — exact cmux surface cleanup miss (backlog `cmux-acceptance-surface-cleanup`)

Reproduced, diagnosed, regression-covered and repaired the backlog miss where the live acceptance run abandoned its exactly-owned cmux surface. Three commits on `task/df250-real-rt02-cmux-cleanup`; final HEAD `46f9738`.

## Root cause

`runtime_sessions.start` binds one owned surface per operation before the provider launches, but release was wired **only** to the success path (`_await_cleanup`). The per-operat

## [2026-07-31] reap | df250-real-rt03-review-delta-prototype

`c-000041` [[2.5 real dogfood RT03 review delta prototype]]. ## Outcome

Tested whether same-session review verification can replace its rebuilt packet with a machine-built delta packet. The bounded prototype proved structural completeness and deterministic identity, but not live-model review-quality equivalence. Production review behavior therefore remains unchanged.

## Evidence

- Added `prototypes/rt03-review-delta-packet.py` and ADR `docs/decisions/rt03-same-session-review-delta-packet.md` in commit `99d56159ab66ed8b07e141ac8b42e26e0a8f7270`.
- All s

## [2026-07-31 22:35] dispatch | df250-real-rt02-cmux-cleanup

Spawned an approved unattended task session (cmux `92800B96-B764-453D-8C8C-C5A78399B911`, runtime claude, model claude-opus-5) in split placement in worktree `/Users/zak/Projects/worktrees/df250-real-rt02-cmux-cleanup`. Target repo `/Users/zak/Projects/llm-obsidian`, branch `task/df250-real-rt02-cmux-cleanup` from `dogfood/2.5-real-10`. Plan: `/Users/zak/Projects/llm-obsidian/wiki/plans/2026-07-31-223011-llm-obsidian-2-5-10-real-task-dogfood.md`. Pre-loaded context: [[Unattended Pipeline]]. Awaiting typed review and final reap.

## [2026-07-31 22:35] dispatch | df250-real-rt01-stale-operations

Spawned an approved unattended task session (cmux `1BF629C4-5312-4FA2-9C6E-43227EA40095`, runtime codex, model gpt-5.6-sol) in split placement in worktree `/Users/zak/Projects/worktrees/df250-real-rt01-stale-operations`. Target repo `/Users/zak/Projects/llm-obsidian`, branch `task/df250-real-rt01-stale-operations` from `dogfood/2.5-real-10`. Plan: `/Users/zak/Projects/llm-obsidian/wiki/plans/2026-07-31-223011-llm-obsidian-2-5-10-real-task-dogfood.md`. Pre-loaded context: [[Unattended Pipeline]]. Awaiting typed review and final reap.

## [2026-07-31 22:35] dispatch | df250-real-rt03-review-delta-prototype

Spawned an approved unattended task session (cmux `723DFC04-256C-46B7-9D0D-27A887F815C5`, runtime codex, model gpt-5.6-sol) in split placement in worktree `/Users/zak/Projects/worktrees/df250-real-rt03-review-delta-prototype`. Target repo `/Users/zak/Projects/llm-obsidian`, branch `task/df250-real-rt03-review-delta-prototype` from `dogfood/2.5-real-10`. Plan: `/Users/zak/Projects/llm-obsidian/wiki/plans/2026-07-31-223011-llm-obsidian-2-5-10-real-task-dogfood.md`. Pre-loaded context: [[Unattended Pipeline]]. Awaiting typed review and final reap.

## [2026-07-31] release | LLM Obsidian 2.5.0 implementation
- Custom PipelineSpec/compiler/runtime, liveness recovery и live Claude/Codex dogfood завершены; финальный Fable + Sol review остаётся release gate. ([[LLM Obsidian 2.5.0 implementation]])

## [2026-07-31] plan-close | LLM Obsidian 2.4 typed pipeline composition

`c-000037` [[LLM Obsidian 2.4 typed pipeline composition result]]. Functional 2.4 scope completed; the user explicitly waived ten real-product tasks for transitional v2.4.1 while retaining eleven-task mechanism dogfood as release evidence.

## [2026-07-31 02:00] dispatch | dogfood-2-4-engineering-change-profile

Spawned an approved unattended task session (cmux `10C4E8D6-6508-428C-902D-B09CE2C147DA`, runtime codex, model gpt-5.6-sol) in split placement in worktree `/Users/zak/Projects/worktrees/llm-obsidian-dogfood-2-4-engineering-change-profile`. Target repo `/Users/zak/Projects/llm-obsidian`, branch `task/dogfood-2-4-engineering-change-profile` from `main`. Plan: `/Users/zak/Projects/llm-obsidian/wiki/plans/2026-07-30-224926-llm-obsidian-2-4-typed-pipeline-composition.md`. Pre-loaded context: [[Unattended Pipeline]]. Awaiting typed review and final reap.

## [2026-07-31 00:34] dispatch | dogfood-2-4-fix-stale-reap-cache

Spawned an approved unattended task session (cmux `D294279A-94FB-4C89-B39A-2F4F5286340E`, runtime codex, model gpt-5.6-sol) in split placement in worktree `/Users/zak/Projects/worktrees/llm-obsidian-dogfood-2-4-fix-stale-reap-cache`. Target repo `/Users/zak/Projects/llm-obsidian`, branch `task/dogfood-2-4-fix-stale-reap-cache` from `main`. Plan: `/Users/zak/Projects/llm-obsidian/wiki/plans/2026-07-31-llm-obsidian-2-4-dogfood-01-stale-reap-cache.md`. Pre-loaded context: none. Awaiting typed review and final reap.

## [2026-07-29] review | LLM Obsidian v2.1.3 final release

`c-000033` [[Cross-model review — LLM Obsidian v2.1.3 final release — b5385c24fe5b]]. 2 round(s), final verdict `approve`; reviewer claude/fable.

## [2026-07-29] review | LLM Obsidian 2.1.3 lifecycle fixes

`c-000032` [[Cross-model review — LLM Obsidian 2.1.3 lifecycle fixes — 1ace771c7718]]. 3 round(s), final verdict `approve`; reviewer claude/fable.

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
