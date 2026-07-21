---
type: plan
title: "LLM Obsidian v2.1.2 — semantic acceptance refactor"
address: c-000019
session_id: 019f6ddd-d07e-7a30-b018-f6358753fb91
sessions:
  - id: 019f6ddd-d07e-7a30-b018-f6358753fb91
    date: 2026-07-21
  - id: 019f6ddd-d07e-7a30-b018-f6358753fb91
    date: 2026-07-21
source_cwd: "/Users/zak/Projects/worktrees/llm-obsidian-v2.1.2-acceptance-refactor"
status: executed
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
   - Fingerprint ячейки строить только из её skill/scenario adapters, `SKILL.md`, общего behavioral ABI/template, launcher/harness-кода, формирующего sandbox/agent invocation/outbox contract, точных runtime dependencies, fixture seed и крупного поколения фактически запускаемой модели после acceptance overrides.
   - Evidence хранит точные resolved launch model и canonical generation. Sol/Terra считать `codex:5.6`; Claude-модели различать по зарегистрированному крупному поколению. Evidence Sonnet-generation нельзя переиспользовать для Opus-generation и наоборот.
   - Behavioral ABI включает runtime launcher и только тот общий harness-код, который формирует sandbox, agent argv/env/permission mode, prompt или result/outbox contract. Scheduling, retries, checkpointing и evidence selection входят в закрытый code-owned orchestration allowlist; model-free test требует точного совпадения исключённого множества с allowlist.
   - Не включать effort, model aliases одного поколения, orchestration allowlist, packaging-only metadata, `wiki/`, `.vault-meta/` и несвязанные изменения.
   - Удалить правило `unknown changed path → rerun all`. Необъявленная runtime-связь должна останавливать model-free dependency check, а не запускать 58 ячеек.
   - Оставить существующий TOML-manifest источником истины. Генерируемый dependency lock и checker обходят runtime-reachable Python/import, subprocess и shell edges, а также константные repo-relative data-path literals любого расширения. Динамически построенная repo-path или иная неразрешимая связь требует явной декларации и fail-closed.
   - Регистрационные поверхности, исполняемые каждым live-sandbox (`hooks/hooks.json`, зарегистрированные `.claude/hooks/**`, `.claude/skill-rules.json`, behavioral registration fields plugin manifests и `.codex` agent/runtime config), вместе со всеми транзитивно зарегистрированными scripts/data являются глобальными behavioral dependencies. Packaging/version-поля тех же manifests нормализуются и остаются non-behavioral. Model-free test сверяет, что каждая ссылка registration surface покрыта lock.
   - Хранить минимальный канонический seed-vault как committed fixture tree. Sandbox builder заменяет `wiki/` и `.vault-meta/` этим seed и создаёт локальный синтетический commit, поэтому рабочее дерево остаётся чистым, а commit-based proofs детерминированы; fingerprint включает hash seed tree. Каждый adapter затем добавляет только свои fixtures. Рабочий vault никогда не является входом live-теста.

4. **Конечный supervisor и телеметрия**
   - Default: два workspace по пять активных ячеек; максимум пять ячеек на workspace и десять workspace только по явному override.
   - Не более трёх попыток на ячейку и только для закрытого типизированного множества transient/capacity ошибок: cmux launch/surface-allocation transient и явные agent-CLI capacity/rate responses. Неизвестные ошибки, assertion/product failures и permission/contract failures не ретраить: завершать `fail`/`blocked` без скрытого цикла.
   - Успешные ячейки сохранять немедленно; новый запуск продолжает только непройденные/blocked и никогда сам не начинает полную матрицу заново.
   - Timeout считать по отсутствию активности: heartbeat от screen changes, tool/model events и lifecycle events; status probe после 15 минут, контролируемое завершение и `blocked` после повторной проверки к 20 минутам.
   - Хранить exact process/surface/workspace IDs. После процесса закрывать точную surface, затем пустой workspace; выполнить финальную reconciliation только owned IDs. Любой orphan — release failure.
   - Записывать content-free timing по setup/model-wait/proof/cleanup, retries, fingerprint/reuse reason, runtime/model generation/effort и cleanup outcome. Screen heartbeat сохраняет только факт изменения и числовые времена, никогда не сохраняет текст экрана.

## Review and Test Gates

1. Сохранённый план проходит full Fable/high review. Замечания разрешаются и подтверждаются в той же task/lane до начала реализации.
   - Принятый reviewer amendment становится новым approved contract и заменяет исходную baseline. Если amendment рационален и остаётся в границе задачи — применить его; если меняет scope/permission/public interface — остановиться на явном coordinator decision. Не восстанавливать исходный вариант как содержательное решение.
2. До удаления старого пути сохранить эталон всех 58 rendered prompts на фиксированных placeholder inputs (sandbox/outbox paths, model, effort, commit и fixture strings). После переноса рендерить с теми же inputs и требовать побайтовое совпадение; исправление реального prompt-дефекта допускается только отдельным reviewed diff и regression-тестом.
3. Добавить model-free проверки точного adapter coverage, code/data/registration dependency lock, semantic invalidation, data-layer reuse, resolved launch model generations, behavioral-vs-orchestration allowlist, evidence epoch, seed isolation/clean synthetic commit, retries, heartbeat и orphan cleanup.
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

Результат: [[LLM Obsidian v2.1.2 semantic acceptance refactor]] (reaped 2026-07-21)
