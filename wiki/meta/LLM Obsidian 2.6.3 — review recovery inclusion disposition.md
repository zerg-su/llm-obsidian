---
type: decision
title: "LLM Obsidian 2.6.3 — review recovery inclusion disposition"
address: c-000101
created: 2026-08-04
updated: 2026-08-04
tags:
  - llm-obsidian
  - v2-6-3
  - review-recovery
  - capability-decision
status: active
decision_date: 2026-08-04
sessions:
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-04
related:
  - "[[2026-08-04-044240-llm-obsidian-2-6-3-russkaya-tekhnicheskaya-dokumentatsiya|LLM Obsidian 2.6.3 — русская техническая документация]]"
  - "[[2026-08-04-112015-llm-obsidian-2-6-4-unattended-callback-submit-watchdog|LLM Obsidian 2.6.4 — unattended callback-submit watchdog]]"
---

# LLM Obsidian 2.6.3 — review recovery inclusion disposition

## Решение

Пользователь явно изменил release boundary: уже реализованные узкие repairs D-264-06 и D-264-08 не откладываются только в 2.6.4, а входят в 2.6.3. Причина — работа над ними уже начата, regressions зелёные, а откат оставил бы документационный релиз зависимым от внешнего непоставляемого recovery facade.

В product branch включены commits `68a13ef` и `f3212cd`; release evidence уточнён отдельным commit `b812693`. Они не меняют handbook outcome, provider routing, permission policy, PipelineSpec DSL или review budgets.

## Разрешённое поведение

- Mixed-HEAD gate в `awaiting-resolution` может войти в существующую fresh boundary только после exact coordinator authorization.
- Все retained review parents и current rounds должны быть terminal, resource-free и без pending effect; `final_results` пуст, fresh reevaluation ещё не использован.
- Verification budget новой boundary равен нулю; automatic fix iteration запрещена.
- Recovery-only adapter принимает historical terminal round только когда единственное различие immutable spec — отсутствующий pre-schema `parent_operation_id`; durable record не переписывается.
- Live ownership, другое identity drift, повторный provider effect и ручная gate/store mutation запрещены.

## Связь с frozen планом

Approved plan и Outcome Contract остаются байт-в-байт неизменными:

- plan SHA-256: `db4037cac1967b0907dbf1b6fd5850eefa2bfc5173080d2aa811a239fb36b8dc`;
- Outcome Contract SHA-256: `2c9728dc7c7fa3bc108ffb6ce5085bb41fcd9ba16310157e76276c6967b5bf5f`;
- dispatch operation: `8596fe76-7baa-4f73-b20c-23f33c0ba120`.

Эта coordinator-owned запись является authoritative amendment к исходному non-goal о минимальных runtime changes. Расширение ограничено двумя уже regression-covered recovery repairs и не разрешает включать остальные D-264 defects в 2.6.3 без нового решения.

## Evidence

Обязательные проверки:

- `tests/harness/test_task_review_mechanism_recovery.py`;
- `tests/harness/test_review_gate.py`;
- `tests/harness/test_task_review_flow_units.py`;
- `make test-harness`;
- `make test`;
- exact-HEAD Deep review без fix-loop.
