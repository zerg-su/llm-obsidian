---
type: decision
title: "LLM Obsidian 2.6.3 — manual parallel task documentation amendment"
address: c-000102
status: active
created: 2026-08-04
updated: 2026-08-04
tags:
  - decision
  - llm-obsidian
  - v2-6-3
  - documentation
  - parallel-tasks
sessions:
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-04
related:
  - "[[2026-08-03-041641-llm-obsidian-2-7-project-scoped-task-programs|LLM Obsidian 2.7 — project-scoped task programs]]"
---

# LLM Obsidian 2.6.3 — manual parallel task documentation amendment

## Решение

Русский handbook 2.6.3 должен практически объяснять уже доступный ручной fan-out крупной цели: декомпозицию на несколько independently approved plan-файлов, отдельный dispatch каждого plan в собственный task/worktree, параллельное наблюдение и последующий coordinator-owned join через terminal review/evidence и отдельную integration boundary.

## Граница

Изменение только документационное и тестовое. Оно не добавляет автоматический task graph, parallel/join primitive, scheduler или Project Spaces. Каждый task продолжает проходить собственный полный последовательный pipeline; автоматическая декомпозиция и deterministic join остаются целью [[2026-08-03-041641-llm-obsidian-2-7-project-scoped-task-programs|LLM Obsidian 2.7 — project-scoped task programs]].

## Обязательное содержание

- критерии независимости slices: непересекающийся file ownership, frozen consumes/produces, отсутствие скрытых зависимостей;
- несколько отдельных plan-файлов и явные task names;
- одновременный dispatch из одной coordinator session, exact task IDs и read-only harness status;
- `final` reap для отдельных планов и явно выбранный `shared` mode только для неизменного master-plan sibling workflow;
- coordinator join не равен reap: объединённый HEAD получает отдельные integration tests и review;
- rate-limit/provider fallback, conflict, failed sibling и scope-drift recovery;
- честная граница: автоматического one-plan fan-out/join в 2.6.3 нет.

## Evidence

Новая guide page должна входить в required-page/guide-section gate, быть достижима из index, planning, sessions/tasks и cookbook, а deterministic tokens должны предотвращать возврат к обзорному упоминанию без runnable walkthrough и recovery.

## Связь с frozen задачей

Approved plan и Outcome Contract остаются байт-в-байт неизменными. Эта coordinator-owned запись расширяет только handbook coverage после явного запроса пользователя. Финальный task summary и review обязаны привязаться к новому exact HEAD; уже запущенный review прежнего HEAD не является terminal approval новой страницы.
