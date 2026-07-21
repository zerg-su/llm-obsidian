---
type: plan
title: "LLM Obsidian v2.1.2 — semantic acceptance refactor"
address: c-000019
session_id: 019f6ddd-d07e-7a30-b018-f6358753fb91
sessions:
  - id: 019f6ddd-d07e-7a30-b018-f6358753fb91
    date: 2026-07-21
source_cwd: "/Users/zak/Projects/worktrees/llm-obsidian-v2.1.2-acceptance-refactor"
status: pending
created: 2026-07-21
updated: 2026-07-21
tags:
  - plan
  - manual-save
---

# LLM Obsidian v2.1.2 — semantic acceptance refactor

## Summary

Зафиксировать 2.1.1 как невыпускаемый Git-checkpoint и подготовить локальный релиз `v2.1.2`. Рефакторинг не добавляет пользовательских функций: он заменяет хрупкую глобальную инвалидацию точными semantic fingerprints, разбивает монолитный live-runner и делает матрицу конечной, наблюдаемой и безопасной для повторного запуска.

Точный сохранённый plan-файл передаётся Fable/high на формальное ревью до реализации. После реализации Fable проверяет код до live-матрицы и подтверждает финальный результат после неё.

## Implementation

1. **Checkpoint и ветка**
   - Регенерировать и проверить производный `tag-index`, завершить текущий advisory-review и зафиксировать 2.1.1 без тега и публикации.
   - Создать `task/v2.1.2-acceptance-refactor` в отдельном worktree от checkpoint.
   - Сразу интегрировать текущий `main`, сохранив vault-историю и возможность финального fast-forward.
   - Сохранить этот план через `vault-write.py` в `wiki/plans/`, провалидировать и закоммитить его.
   - Объединить невыпущенную секцию 2.1.1 в CHANGELOG 2.1.2; перевести plugin manifests сразу на `2.1.2`.

2. **Новая acceptance-архитектура**
   - Оставить `scripts/live-acceptance-runner.py` тонким совместимым CLI-wrapper; перенести реализацию в `scripts/acceptance/`.
   - Разделить ядро на harness, runtime launchers, общий versioned prompt template, scenario adapters и skill adapters.
   - Skill adapter владеет skill-specific prompt/fixture/proof/cleanup; scenario adapter — только общей логикой сценария. Оба runtime используют один adapter, различия Claude/Codex остаются в launcher.
   - Сохранить документированные и реально используемые CLI/env-интерфейсы. Неиспользуемые внутренние compatibility paths удалять только после поиска call sites и тестового доказательства.
   - Полностью удалить прежние `LIVE_RUNNER_*` tables, исполнение исторического runner-кода, старую AST-классификацию как источник истины и миграцию evidence 2.1.1.

3. **Semantic fingerprints и data isolation**
   - Начать новый evidence/runner contract epoch; старые 2.1.1 reports не мигрировать и не переиспользовать.
   - Fingerprint ячейки строить только из её skill/scenario adapters, `SKILL.md`, общего behavioral ABI/template, точных runtime dependencies, fixture seed и крупного поколения модели.
   - Не включать effort, model aliases одного поколения, orchestration/scheduling, packaging metadata, `wiki/`, `.vault-meta/` и несвязанные изменения.
   - Sol/Terra считать `codex:5.6`; Claude-модели различать по зарегистрированному крупному поколению.
   - Удалить правило `unknown changed path → rerun all`. Необъявленная runtime-связь должна останавливать model-free dependency check, а не запускать 58 ячеек.
   - Оставить существующий TOML-manifest источником истины; генерируемый dependency lock и checker подтверждают Python/import, subprocess и shell edges. Динамическая неразрешимая связь требует явной декларации и fail-closed.
   - Создать минимальный канонический seed-vault; каждый adapter работает в отдельной копии и добавляет только свои fixtures. Рабочий vault никогда не является входом live-теста.

4. **Конечный supervisor и телеметрия**
   - Default: два workspace по пять активных ячеек; максимум пять ячеек на workspace и десять workspace только по явному override.
   - Не более трёх попыток на ячейку и только для типизированных transient/capacity ошибок; assertion/product failures не ретраить.
   - Успешные ячейки сохранять немедленно; новый запуск продолжает только непройденные/blocked и никогда сам не начинает полную матрицу заново.
   - Timeout считать по отсутствию активности: heartbeat от screen changes, tool/model events и lifecycle events; status probe после 15 минут, контролируемое завершение и `blocked` после повторной проверки к 20 минутам.
   - Хранить exact process/surface/workspace IDs. После процесса закрывать точную surface, затем пустой workspace; выполнить финальную reconciliation только owned IDs. Любой orphan — release failure.
   - Записывать content-free timing по setup/model-wait/proof/cleanup, retries, fingerprint/reuse reason, runtime/model generation/effort и cleanup outcome.

## Review and Test Gates

1. Сохранённый план проходит full Fable/high review. Замечания разрешаются и подтверждаются в той же task/lane до начала реализации.
2. До удаления старого пути сохранить эталон всех 58 rendered prompts. После переноса требовать побайтовое совпадение; исправление реального prompt-дефекта допускается только отдельным reviewed diff и regression-тестом.
3. Добавить model-free проверки точного adapter coverage, dependency lock, semantic invalidation, data-layer reuse, model generations, evidence epoch, seed isolation, retries, heartbeat и orphan cleanup.
4. Прогнать targeted acceptance suites, затем полный `make test`.
5. Провести full implementation review Fable/high и same-session verification всех исправлений до live-матрицы.
6. На чистом committed candidate выполнить один полный fresh-прогон 58 ячеек: Claude `sonnet`, Codex `gpt-5.6-terra`, effort `medium`, concurrency 2×5.
7. При дефекте сначала сделать минимальную правку и regression-тест, затем Fable-review; повторять только ячейки с изменившимся semantic fingerprint. Полный повтор допустим лишь для критичного изменения общего behavioral contract.
8. Release gate: 58/58 pass, model-free suite green, zero owned orphan surfaces/workspaces, валидный telemetry/report, чистое дерево и финальное подтверждение Fable.

## Release

- Критичные correctness/security/data-loss/reuse/cleanup находки исправлять даже ценой полного прогона. Некритичный долг без влияния на результат, требующий широкой инвалидации, записывать в backlog.
- Обновить документацию новой архитектуры и отметить plan как `executed` через canonical vault writer.
- Интегрировать свежий `main`; data-only изменения должны переиспользовать evidence, product changes — затронуть только свои fingerprints.
- Локально fast-forward `main` на утверждённый release commit и создать annotated tag `v2.1.2`.
- Не выполнять push и не создавать GitHub Release. Передать пользователю точные команды `git push origin main v2.1.2` и `gh release create v2.1.2 --verify-tag --notes-from-tag --title "llm-obsidian v2.1.2"`.

## Assumptions

- macOS + cmux остаются основной поддерживаемой платформой; Linux допускает базовый fallback, Windows не входит в release gate.
- Production defaults остаются Sol/high и Opus/high; Terra/Sonnet/medium используются только внутри изолированной acceptance-матрицы.
- Fable/high применяется только для review изменений, не для массовых acceptance-ячеек.
- При внешней блокировке, credentials/dependency approval или конфликте с посторонней пользовательской работой состояние сохраняется без разрушительных действий; вся остальная работа продолжается автономно.
