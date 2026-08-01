---
type: plan
title: "LLM Obsidian 2.6 dogfood RT1 — callback watchdog architecture"
address: c-000079
session_id: 019fab00-3160-7380-8920-4b20183afb76
sessions:
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-02
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-02
source_cwd: "/Users/zak/Projects/llm-obsidian"
status: executed
created: 2026-08-02
updated: 2026-08-02
tags:
  - plan
  - manual-save
  - dogfood
  - v2.6
---

# LLM Obsidian 2.6 dogfood RT1 — callback watchdog architecture

## Outcome Contract

```json
{"schema_version":1,"purpose":"Проверить clarify/design на реальной неоднозначной lifecycle-задаче, не добавляя ещё один orchestration authority.","desired_outcome":"Получить проверенное архитектурное решение для дешёвого резервного контроля зависших provider callbacks, которое использует существующий harness ownership, не вызывает модель без необходимости и однозначно задаёт границы liveness, recovery и user-visible progress.","success_evidence":[{"evidence_id":"rt1-decision","observable":"Durable ADR-style документ фиксирует выбранный вариант, отвергнутые альтернативы и точные ownership boundaries."},{"evidence_id":"rt1-state-flow","observable":"Документ сопоставляет callback receipt, provider liveness, deadlines, cleanup и recovery с существующими OperationStore и harness transitions."},{"evidence_id":"rt1-cost-bound","observable":"Решение объясняет, как резервная проверка остаётся code-owned и не создаёт периодические model calls или новые provider effects."},{"evidence_id":"rt1-test-seams","observable":"Определены детерминированные test seams, rollout/rollback и критерии остановки после повторяющегося architecture-level failure."}],"non_goals":["Реализация production-кода в этой задаче.","Новый scheduler, pipeline engine или model-owned watchdog.","Изменение review budgets, permission policy или cmux public API."]}
```

## Контекст

Это первая из четырёх real-task проверок плана [[2026-08-01-134355-llm-obsidian-2-6-0-edinyy-reliz-technical-foundation-i|LLM Obsidian 2.6.0 — единый релиз technical foundation и skill intelligence]]. Пользователь хочет резервный механизм, который редко и дёшево проверяет зависшую модель, когда callback не пришёл, но не тратит токены и не размножает окна.

## Работа

1. Через `clarify` сначала собрать факты из текущих harness, OperationStore, callback broker, liveness/reconcile и cmux adapter.
2. Через `design` разделить минимум три варианта: passive deadline reconciliation, event-driven provider exit/progress и bounded host heartbeat.
3. Выбрать один тонкий вариант, описать control/data flow, ownership, failure recovery, security boundary, rollout и rollback.
4. Сохранить результат в `docs/acceptance/v2.6-dogfood-rt1-callback-watchdog.md`; production code не менять.
5. Прогнать simple review по Outcome Contract и сохранить established/missing/contradicted evidence.

## Проверка

- Документ не предлагает новый model call в детерминированном переходе.
- Каждый новый state/effect имеет одного code-owned владельца.
- Решение совместимо с существующими dispatch/review/research flows и не требует нового pipeline DSL.

Результат: [[LLM Obsidian 2.6 dogfood RT1 callback watchdog architecture]] (reaped 2026-08-02)
